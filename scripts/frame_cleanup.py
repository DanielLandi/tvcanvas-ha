#!/usr/bin/env python3
"""Delete the copies TVCanvas left in a Frame's My Photos, and nothing else.

A pin that reached Home Assistant with Replace previous off kept every image HA showed,
so My Photos filled with copies. This lists My Photos and picks only images it can prove
are HA's:

1. ids HA recorded as its own (`--ledger`: the TV's `frame_uploaded` map from HA's
   TVCanvas store, written since that record exists), and
2. images whose `image_date` falls just before a time HA's "Last update" sensor says it
   showed an image (`--history`: HA's /api/history answer for that sensor). HA stamps
   `image_date` with its own clock when the upload starts, and the sensor records the end
   of that same update, so each update owns at most ONE image dated in the window before
   it. An update with two images in its window (the user uploaded at the same moment) is
   ambiguous and neither is touched. Only 3840 x 2160 images without a mat can be HA's.

The clock offset between `image_date` (HA's local time) and the sensor (UTC) is read
from `--anchor`: the image HA shows now (`current` in the store), uploaded by the
sensor's last update. Without `--apply` nothing is deleted: it prints what it would do.

    .venv/bin/python scripts/frame_cleanup.py --host 192.168.0.x \\
        --history history.json --anchor MY_F0123            # dry run
    .venv/bin/python scripts/frame_cleanup.py ... --apply   # delete

It connects as "TVCanvas-HA", the name the TV already allowed for Home Assistant, so the
TV does not ask again; set FRAME_TOKEN to the token HA holds for that address if it has
one. Run it while the Frame is on; never while Home Assistant also runs a pin on it with
Replace previous off (turn the pin's "Run on Home Assistant" off first).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

MY_PHOTOS = "MY-C0002"
HA_SIZE = (3840, 2160)
WINDOW_BEFORE = timedelta(seconds=120)  # upload start .. sensor update (fetch, send, show)
WINDOW_AFTER = timedelta(seconds=2)  # the sensor's second vs the TV's second
QUARTER_HOUR = 15 * 60


def image_date(item: dict[str, Any]) -> datetime | None:
    """The TV's `image_date` ("2026:11:13 18:41:39"), naive, as HA's clock wrote it."""
    digits = re.sub(r"\D", "", str(item.get("image_date") or ""))
    if len(digits) < 14:
        return None
    try:
        return datetime.strptime(digits[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None


def history_times(history: Any) -> list[datetime]:
    """Every distinct time HA's Last update sensor says it showed an image (UTC)."""
    times: set[datetime] = set()
    for series in history if isinstance(history, list) else []:
        for state in series if isinstance(series, list) else []:
            value = state.get("state") if isinstance(state, dict) else None
            try:
                when = datetime.fromisoformat(str(value))
            except ValueError:
                continue  # "unknown", "unavailable"
            if when.tzinfo is not None:
                times.add(when.astimezone(UTC).replace(tzinfo=None))
    return sorted(times)


def clock_offset(items: Iterable[dict[str, Any]], anchor: str, times: list[datetime]) -> timedelta:
    """HA's local time minus UTC, from the image HA shows now (uploaded by the last update),
    rounded to a quarter hour (every real time zone)."""
    dated = {i.get("content_id"): image_date(i) for i in items}
    local = dated.get(anchor)
    if local is None:
        raise SystemExit(f"anchor {anchor} is not in My Photos (or has no image_date)")
    if not times:
        raise SystemExit("the history holds no update times")
    seconds = (local - times[-1]).total_seconds()
    offset = timedelta(seconds=round(seconds / QUARTER_HOUR) * QUARTER_HOUR)
    if not -WINDOW_BEFORE <= local - offset - times[-1] <= WINDOW_AFTER:
        raise SystemExit(
            f"anchor {anchor} was not uploaded by the last update in the history "
            f"({times[-1].isoformat()}Z); pass the id HA shows now"
        )
    return offset


def could_be_has(item: dict[str, Any]) -> bool:
    """HA uploads 3840 x 2160 JPEGs with no mat; anything else is the user's."""
    size = (item.get("width"), item.get("height"))
    if None not in size and tuple(int(v) for v in size) != HA_SIZE:
        return False
    matte = item.get("matte_id")
    return matte in (None, "", "none")


@dataclass
class Plan:
    delete: dict[str, str] = field(default_factory=dict)  # id -> why
    ambiguous: dict[str, str] = field(default_factory=dict)
    kept: list[str] = field(default_factory=list)  # the user's art, and --keep
    updates_without_image: int = 0


def plan(
    items: list[dict[str, Any]],
    times: list[datetime],
    offset: timedelta,
    ledger: Iterable[str] = (),
    keep: Iterable[str] = (),
) -> Plan:
    out = Plan()
    keep, ledger = set(keep), set(ledger)
    by_update: dict[datetime, list[str]] = {}
    for item in items:
        cid = item.get("content_id")
        if not cid:
            continue
        if cid in keep:
            out.kept.append(cid)
            continue
        if cid in ledger:
            out.delete[cid] = "recorded by Home Assistant"
            continue
        local = image_date(item)
        if local is None or not could_be_has(item):
            out.kept.append(cid)
            continue
        utc = local - offset
        owner = next((t for t in times if t - WINDOW_BEFORE <= utc <= t + WINDOW_AFTER), None)
        if owner is None:
            out.kept.append(cid)
        else:
            by_update.setdefault(owner, []).append(cid)
    for when, ids in by_update.items():
        why = f"uploaded by the update at {when.isoformat()}Z"
        if len(ids) == 1:
            out.delete[ids[0]] = why
        else:
            for cid in ids:
                out.ambiguous[cid] = why + f" with {len(ids) - 1} other image(s)"
    out.updates_without_image = len(times) - len(by_update)
    return out


def delete(art: Any, ids: list[str], batch: int = 50) -> list[str]:
    """Delete in batches, one at a time where a batch fails; return what was deleted."""
    done: list[str] = []
    for start in range(0, len(ids), batch):
        chunk = ids[start : start + batch]
        try:
            if art.delete_list(chunk):
                done.extend(chunk)
                continue
        except Exception as err:  # noqa: BLE001 - fall back to one id at a time
            print(f"  batch failed ({err}); one at a time", file=sys.stderr)
        for cid in chunk:
            try:
                if art.delete(cid):
                    done.append(cid)
            except Exception as err:  # noqa: BLE001
                print(f"  {cid}: {err}", file=sys.stderr)
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", required=True, help="the Frame's address now")
    parser.add_argument("--history", type=Path, required=True, help="HA /api/history JSON")
    parser.add_argument("--anchor", required=True, help="the id HA shows now (store `current`)")
    parser.add_argument("--ledger", type=Path, help="JSON map of ids HA recorded as its own")
    parser.add_argument("--keep", action="append", default=[], help="never delete this id")
    parser.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    parser.add_argument("--verbose", action="store_true", help="also list the art kept")
    args = parser.parse_args(argv)

    from samsungtvws import SamsungTVArt  # the dev venv has it (requirements_test.txt)

    times = history_times(json.loads(args.history.read_text()))
    ledger = list(json.loads(args.ledger.read_text())) if args.ledger else []
    art = SamsungTVArt(
        args.host,
        token=os.environ.get("FRAME_TOKEN") or None,
        port=8002,
        timeout=60,
        name="TVCanvas-HA",
    )
    try:
        items = art.available(MY_PHOTOS)
        offset = clock_offset(items, args.anchor, times)
        result = plan(items, times, offset, ledger, keep=[args.anchor, *args.keep])
        dates = {i.get("content_id"): i.get("image_date") for i in items}
        print(f"My Photos: {len(items)} images; HA updates in the history: {len(times)}")
        print(f"HA clock = UTC{offset.total_seconds() / 3600:+.2f} h (from {args.anchor})")
        print(f"\nHA's copies to delete: {len(result.delete)}")
        for cid, why in sorted(result.delete.items(), key=lambda kv: str(dates.get(kv[0]))):
            print(f"  {cid}  {dates.get(cid)}  {why}")
        print(f"\nAmbiguous, kept (check these by hand): {len(result.ambiguous)}")
        for cid, why in result.ambiguous.items():
            print(f"  {cid}  {dates.get(cid)}  {why}")
        print(f"\nKept (your art, and {args.anchor} on screen): {len(result.kept)}")
        if args.verbose:
            for cid in result.kept:
                print(f"  {cid}  {dates.get(cid)}")
        print(f"HA updates with no image left on the TV: {result.updates_without_image}")
        if not args.apply:
            print("\nDry run: nothing deleted. Add --apply to delete the list above.")
            return 0
        done = delete(art, sorted(result.delete))
        left = {i.get("content_id") for i in art.available(MY_PHOTOS)} & set(result.delete)
        print(f"\nDeleted {len(done)}; still on the TV: {len(left)} {sorted(left)}")
        return 0 if not left else 1
    finally:
        art.close()


if __name__ == "__main__":
    sys.exit(main())
