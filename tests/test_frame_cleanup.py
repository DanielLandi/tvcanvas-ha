"""scripts/frame_cleanup.py picks only images it can prove Home Assistant uploaded."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Imported before the autouse fixtures patch it (conftest.fake_dns), as every test module does.
import custom_components.tvcanvas  # noqa: F401

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "frame_cleanup", ROOT / "scripts" / "frame_cleanup.py"
)
cleanup = importlib.util.module_from_spec(spec)
sys.modules["frame_cleanup"] = cleanup  # dataclasses look their module up there
spec.loader.exec_module(cleanup)

PST = timedelta(hours=-8)
FIRST = datetime(2026, 11, 13, 2, 41, 39)  # UTC, the sensor's first update of the run


def updates(n: int, every: int = 193) -> list[datetime]:
    return [FIRST + timedelta(seconds=every * k) for k in range(n)]


def history(times: list[datetime]) -> list:
    states = [{"state": "unknown"}] + [{"state": t.isoformat() + "+00:00"} for t in times]
    return [states]


def photo(cid: str, local: datetime | str | None, **extra) -> dict:
    when = local.strftime("%Y:%m:%d %H:%M:%S") if isinstance(local, datetime) else local
    return {"content_id": cid, "category_id": "MY-C0002", "image_date": when, **extra}


def ha_photo(n: int, update: datetime, seconds_before: int = 9) -> dict:
    local = update + PST - timedelta(seconds=seconds_before)
    return photo(f"MY_F{2000 + n}", local, width=3840, height=2160, matte_id="none")


def test_history_times_reads_timestamps_only() -> None:
    times = updates(3)
    assert cleanup.history_times(history(times) + [[{"state": "unavailable"}]]) == times


def test_clock_offset_is_read_from_the_image_on_screen() -> None:
    times = updates(4)
    items = [ha_photo(k, t) for k, t in enumerate(times)]
    assert cleanup.clock_offset(items, "MY_F2003", times) == PST


def test_clock_offset_refuses_an_anchor_from_another_update() -> None:
    times = updates(4)
    items = [ha_photo(k, t) for k, t in enumerate(times)]
    with pytest.raises(SystemExit, match="not uploaded by the last update"):
        cleanup.clock_offset(items, "MY_F2001", times)


def test_every_copy_is_found_and_the_users_art_is_kept() -> None:
    times = updates(50)
    copies = [ha_photo(k, t, seconds_before=3 + k % 40) for k, t in enumerate(times)]
    users = [
        photo("USER_OLD", "2025:12:24 09:00:00", width=3840, height=2160, matte_id="none"),
        # uploaded by the user between two of HA's updates
        photo("USER_BETWEEN", times[10] + PST + timedelta(seconds=60), width=3840, height=2160),
        # same moment as an HA upload, but a phone photo with a mat: never HA's
        photo("USER_MAT", times[20] + PST - timedelta(seconds=5), matte_id="shadowbox_polar"),
        photo("USER_SMALL", times[21] + PST - timedelta(seconds=5), width=1920, height=1080),
        photo("USER_NODATE", None),
    ]
    result = cleanup.plan(users + copies, times, PST, keep=["MY_F2049"])
    assert set(result.delete) == {c["content_id"] for c in copies[:-1]}
    assert set(result.kept) == {u["content_id"] for u in users} | {"MY_F2049"}
    assert result.ambiguous == {}


def test_two_images_in_one_updates_window_are_both_kept() -> None:
    times = updates(3)
    items = [ha_photo(k, t) for k, t in enumerate(times)]
    items.append(photo("USER_SAME_TIME", times[1] + PST - timedelta(seconds=30)))
    result = cleanup.plan(items, times, PST)
    assert set(result.ambiguous) == {"MY_F2001", "USER_SAME_TIME"}
    assert set(result.delete) == {"MY_F2000", "MY_F2002"}


def test_an_image_long_before_any_update_is_kept() -> None:
    times = updates(3)
    early = photo("USER_EARLY", FIRST + PST - timedelta(minutes=5))
    assert cleanup.plan([early], times, PST).kept == ["USER_EARLY"]


def test_recorded_ids_are_deleted_whatever_their_date() -> None:
    items = [photo("MY_F0001", "2020:01:01 00:00:00"), photo("USER_1", None)]
    result = cleanup.plan(items, [], PST, ledger=["MY_F0001"])
    assert result.delete == {"MY_F0001": "recorded by Home Assistant"}
    assert result.kept == ["USER_1"]


def test_dry_run_never_deletes(tmp_path, monkeypatch, capsys) -> None:
    times = updates(3)
    items = [ha_photo(k, t) for k, t in enumerate(times)] + [photo("USER_1", None)]
    art = MagicMock()
    art.available.return_value = items
    monkeypatch.setattr("samsungtvws.SamsungTVArt", MagicMock(return_value=art))
    (tmp_path / "h.json").write_text(json.dumps(history(times)))
    argv = ["--host", "192.168.0.20", "--history", str(tmp_path / "h.json")]
    assert cleanup.main([*argv, "--anchor", "MY_F2002"]) == 0
    art.delete_list.assert_not_called()
    art.delete.assert_not_called()
    out = capsys.readouterr().out
    assert "HA's copies to delete: 2" in out and "Dry run" in out

    art.delete_list.return_value = True
    art.available.side_effect = [items, [items[2], items[3]]]
    assert cleanup.main([*argv, "--anchor", "MY_F2002", "--apply"]) == 0
    art.delete_list.assert_called_once_with(["MY_F2000", "MY_F2001"])


def test_updates_29_seconds_apart_each_own_their_image() -> None:
    """Two updates 29 s apart (after HA restarted mid-run, 2026-11-14 01:51:56/01:52:25)."""
    first, second = FIRST, FIRST + timedelta(seconds=29)
    items = [ha_photo(0, first, seconds_before=20), ha_photo(1, second, seconds_before=8)]
    result = cleanup.plan(items, [first, second], PST)
    assert set(result.delete) == {"MY_F2000", "MY_F2001"} and result.ambiguous == {}
    assert "02:41:39" in result.delete["MY_F2000"] and "02:42:08" in result.delete["MY_F2001"]
