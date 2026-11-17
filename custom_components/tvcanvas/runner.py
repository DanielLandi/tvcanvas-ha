"""One TV's refresh loop."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable, Container
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api import AuthError, CloudClient, CloudError, OutOfCredits, RateLimited
from .const import (
    CAST_PORT,
    CREDITS_RETRY_SECONDS,
    FRAME_STANDBY_POLL_SECONDS,
    FRAME_WS_PORT,
    MAX_BACKOFF_SECONDS,
    PREVIEW_BOARD_SECONDS,
    PREVIEW_OTHER_SECONDS,
    PROBE_TIMEOUT_SECONDS,
    SHOWING_REPORT_MIN_SECONDS,
)
from .displays import STANDBY, Display, DisplayError, HostRefused, tv_address
from .imaging import decode_gate, fit_to_tv
from .items import Item, items_of, option_names, rotates, rotation_order, source_key
from .models import Source, SyncSpec
from .photos import PhotoStore
from .sources import (
    BoardCredits,
    SourceError,
    WaitingForPhone,
    load_item,
    next_image,
    runs_without_credits,
)
from .storage import StateStore
from .urlsafety import TV_HOST_REFUSED, redact

_LOGGER = logging.getLogger(__name__)


async def tcp_reachable(
    host: str, port: int, connect_seconds: float = PROBE_TIMEOUT_SECONDS
) -> bool:
    """True when the TV accepts a TCP connection. Sends no data; never raises."""
    try:
        async with asyncio.timeout(connect_seconds):
            _, writer = await asyncio.open_connection(host, port)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with suppress(Exception):
        await writer.wait_closed()
    return True


@dataclass
class TVStatus:
    last_ok: datetime | None = None
    last_error: str | None = None
    last_tick: datetime | None = None


@dataclass(frozen=True)
class NowShowing:
    """What HA last sent to the TV: kept in memory only, for the dashboard entities.

    image is the fitted JPEG the TV got. It is never written to disk or sent anywhere
    but the TV and HA's own authenticated image proxy (image.py)."""

    source: Source  # the spec source it came from; a later spec edit makes `item` stale
    item: Item
    image: bytes
    at: datetime


@dataclass(frozen=True)
class _Manual:
    """A tick someone asked for (a pick, "Next image", "Show preview on TV"): it runs
    even while the rotation is paused. *image*: show these bytes (a preview) instead of
    loading the item again, so a previewed board costs no second credit."""

    index: int | None = None
    image: bytes | None = None


class PreviewRefused(Exception):
    """Why a preview was not made; the message is for the user."""


def rfc3339(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class TVRunner:
    def __init__(
        self,
        hass: HomeAssistant,
        client: CloudClient,
        session: aiohttp.ClientSession | None,
        store: StateStore,
        spec: SyncSpec,
        display: Display,
        on_auth_error: Callable[[], None],
        on_update: Callable[[], None],
        photos: PhotoStore | None = None,
    ) -> None:
        self._hass, self._client, self._session, self._store = hass, client, session, store
        self._photos = photos
        self.spec, self._display = spec, display
        self._on_auth_error, self._on_update = on_auth_error, on_update
        self.status = TVStatus()
        self._failures = 0
        self._unreachable = False
        self._sleep = asyncio.sleep
        self._probe = tcp_reachable
        self._credits_retry_at: datetime | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._auth_failed = False
        self._refused_logged = False
        self._standby = False
        self._standby_reported: datetime | None = None
        self.now_showing: NowShowing | None = None
        # The "Preview" select's image: made only when someone picks a preview, never sent
        # to the TV unless "Show preview on TV" is pressed. In memory only, like now_showing.
        self.preview: NowShowing | None = None
        self._previewing = False
        self._preview_next: dict[bool, float] = {}  # costs a credit -> monotonic time
        self._manual: _Manual | None = None
        self._last_report: tuple[bool, str | None] | None = None
        self._reported_at = -math.inf  # monotonic time of the last status report
        self._reports = 0  # status reports sent so far
        self._changed_after = 0  # how many reports were sent when `showing` last changed
        self._change_task: asyncio.Task[None] | None = None

    async def _fit(self, data: bytes, orientation: str | None) -> bytes | None:
        # One decode at a time across every TV and phone upload (#16).
        return await decode_gate(self._hass).run(self._hass, fit_to_tv, data, orientation)

    @property
    def boards_paused_until(self) -> datetime | None:
        """When boards are next tried after a 402 (in the past: retrying, not yet rendered).

        A board pin or an all-board group pauses the whole TV until then; a group
        with webcams or links keeps showing those and skips its boards.
        """
        return self._credits_retry_at

    @property
    def address(self) -> str:
        """Where the TV is reached: the spec's host, or where its display followed it."""
        address = getattr(self._display, "address", None)
        return address if isinstance(address, str) and address else self.spec.tv.host

    @property
    def tv_state(self) -> str | None:
        """What the TV said about its power this tick ("on", "standby"); None when
        unknown (a Cast TV, a Frame firmware without PowerState, or no answer)."""
        if self._standby:
            return STANDBY
        state = getattr(self._display, "power_state", None)
        return state if isinstance(state, str) else None

    @property
    def paused(self) -> bool:
        """The "Pause rotation" switch: kept in the store, so it outlives a restart."""
        return self._store.paused(self.spec.tv.id)

    def set_paused(self, paused: bool) -> None:
        """Hold the current item (no tick touches the TV) or resume at the next tick."""
        if paused == self.paused:
            return
        self._store.set_paused(self.spec.tv.id, paused)
        _LOGGER.debug("TVCanvas: %s %s", self.spec.tv.name, "paused" if paused else "resumed")
        self._on_update()
        self._showing_changed()

    def _skipping_boards(self) -> bool:
        return self._credits_retry_at is not None and runs_without_credits(self.spec)

    def next_delay(self) -> int:
        interval = self.spec.interval_seconds
        if self._standby:  # asked again soon, so the TV wakes to a fresh image
            return min(interval, FRAME_STANDBY_POLL_SECONDS)
        if self._failures == 0:
            return interval
        return max(interval, min(MAX_BACKOFF_SECONDS, 60 * 2 ** (self._failures - 1)))

    async def _wait(self) -> None:
        """Sleep until the next tick.

        While backing off after "TV unreachable", probe the TV's port every
        interval and end the wait as soon as it comes back. A success counts only
        after a failed probe since the last tick, so a TV whose port answers while
        show() keeps failing still gets the full backoff.
        """
        delay = self.next_delay()
        if not (self._unreachable and self._failures):
            await self._sleep(delay)
            return
        try:
            host = await tv_address(self.address)
        except HostRefused:  # never probe an address HA must not connect to
            await self._sleep(delay)
            return
        port = FRAME_WS_PORT if self.spec.tv.kind == "frame" else CAST_PORT
        seen_down = not await self._probe(host, port)
        remaining = delay
        while remaining > 0:
            chunk = min(self.spec.interval_seconds, remaining)
            await self._sleep(chunk)
            remaining -= chunk
            if remaining <= 0:
                return
            if await self._probe(host, port):
                if seen_down:
                    _LOGGER.debug("TVCanvas: %s is reachable again", self.spec.tv.name)
                    return
            else:
                seen_down = True

    async def tick(self) -> None:
        if not self.spec.enabled or self._auth_failed:
            return
        manual, self._manual = self._manual, None
        now = dt_util.utcnow()
        if manual is None and self.paused:
            await self._set_paused(now)
            return
        boards_paused = self._credits_retry_at is not None and now < self._credits_retry_at
        if boards_paused and not runs_without_credits(self.spec):
            return  # a board pin or an all-board group: the whole TV pauses
        self.status.last_tick = now
        was_standby, self._standby = self._standby, False
        credits = BoardCredits(paused=boards_paused)
        try:
            # Before anything is fetched, read or sent: the TV must be on the home
            # network (#14). The displays check again where they connect.
            await tv_address(self.spec.tv.host)
            waiting = await self._ready()
            if waiting is not None and manual is not None:
                # A pick (or "Next", or a preview) the TV can't show yet: kept for the
                # tick that can, also while paused.
                self._manual = manual
            if waiting == STANDBY:
                # A Frame in standby: nothing is rendered (no board credits), uploaded or
                # backed off; it is asked again every FRAME_STANDBY_POLL_SECONDS.
                self._failures, self._unreachable = 0, False
                self._standby = True
                await self._set_standby(now, was_standby)
                return
            if waiting:
                # Reachable but not showing TVCanvas (a Frame in Watch TV or Art):
                # no fetch, so no board credits, and no backoff.
                self._failures, self._unreachable = 0, False
                await self._set(ok=False, error=waiting, category=waiting)
                return
            if manual is not None and manual.image is not None and manual.index is not None:
                jpeg, label = self._previewed(manual.index, manual.image, credits)
            else:
                try:
                    jpeg, label = await next_image(
                        self.spec,
                        self._store.rotation(self.spec.tv.id),
                        self._client,
                        self._session,
                        self._fit,
                        credits,
                        self._photos,
                    )
                finally:
                    if credits.hit:  # a mixed group's board got 402: skip its boards 30 min
                        self._credits_retry_at = now + timedelta(seconds=CREDITS_RETRY_SECONDS)
            self._store.schedule_save()
            await self._show(jpeg)
            self._remember(jpeg)
        except AuthError:
            self._auth_failed = True
            self._on_auth_error()
            return
        except RateLimited:
            return
        except OutOfCredits:
            self._credits_retry_at = now + timedelta(seconds=CREDITS_RETRY_SECONDS)
            await self._set(ok=False, error="out of credits", category="out of credits")
            return
        except WaitingForPhone:
            if self._skipping_boards():
                await self._set(ok=False, error="out of credits", category="out of credits")
                return
            # Nothing to show yet: keep the TV's image, no backoff (as a Frame in Watch TV).
            self._failures, self._unreachable = 0, False
            # (A photo that landed mid-tick still counts once: it shows next tick.)
            waiting = self._waiting_for_photos() or "waiting: photos 1"
            await self._set(ok=False, error=waiting, category=waiting)
            return
        except SourceError as err:
            if self._skipping_boards():
                # Credits are what the user can fix from the phone: say that first.
                await self._set(ok=False, error=f"out of credits; {err}", category="out of credits")
                return
            await self._set(ok=False, error=str(err), category="image fetch failed")
            return
        except HostRefused as err:
            # Not a TV in this home: nothing was sent, and no backoff probe will touch it.
            self._failures, self._unreachable = 0, False
            if not self._refused_logged:
                self._refused_logged = True
                _LOGGER.warning(
                    "TVCanvas: %s is not updated: %s. Home Assistant only sends images "
                    "to private and link-local addresses",
                    self.spec.tv.name,
                    err,
                )
            await self._set(ok=False, error=str(err), category=TV_HOST_REFUSED)
            return
        except DisplayError as err:
            self._failures += 1
            self._unreachable = True
            await self._set(ok=False, error=str(err), category="TV unreachable")
            return
        self._failures = 0
        self._unreachable = False
        if credits.board_shown:
            self._credits_retry_at = None
        _LOGGER.debug("TVCanvas: %s now shows %s", self.spec.tv.name, label)
        if self._skipping_boards():
            # The TV updated, but the group's boards are out of credits: HA's sensor
            # records the update, the phone keeps seeing "out of credits".
            await self._set(
                ok=False,
                error="out of credits: showing the group's webcams and links only",
                category="out of credits",
                shown=True,
            )
            return
        if waiting := self._waiting_for_photos():
            # The TV updated, but some phone photos haven't arrived: the cloud keeps
            # an error only with ok=false, so this is a `waiting: …` report.
            await self._set(ok=False, error=waiting, category=waiting, shown=True)
            return
        await self._set(ok=True, error=None)

    def _previewed(self, index: int, jpeg: bytes, credits: BoardCredits) -> tuple[bytes, str]:
        """ "Show preview on TV": the preview's bytes take this tick's place in the rotation."""
        src = self.spec.source
        item = items_of(src)[index]
        if rotates(src):
            rotation = self._store.rotation(self.spec.tv.id)
            order = rotation_order(src)
            rotation.update(
                key=src.items_key, pos=(order.index(index) + 1) % len(order), shown=index
            )
        credits.board_shown = item.costs_credit
        return jpeg, f"the preview of item {index}"

    def _remember(self, jpeg: bytes) -> None:
        src = self.spec.source
        rotation = self._store.rotation(self.spec.tv.id)
        index = rotation.get("shown", 0) if rotates(src) else 0
        items = items_of(src)
        if not 0 <= index < len(items):
            return
        at = dt_util.utcnow()
        self.now_showing = NowShowing(src, items[index], jpeg, at)
        # What the TV shows outlives a restart (the image doesn't): the select, and the
        # phone's `showing`, still know the item while the rotation is paused.
        rotation.update(held=index, held_src=source_key(src), held_at=at.isoformat())
        self._store.schedule_save()

    def held(self) -> tuple[Item, datetime] | None:
        """The spec item on the TV and since when: from memory, or after a restart from
        the store, while it still belongs to the TV's current pin."""
        src = self.spec.source
        shown = self.now_showing
        if shown is not None:
            return (shown.item, shown.at) if shown.source == src else None
        rotation = self._store.rotation(self.spec.tv.id)
        if rotation.get("held_src") != source_key(src):
            return None
        items = items_of(src)
        index = rotation.get("held", 0)
        try:
            at = datetime.fromisoformat(rotation["held_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if not isinstance(index, int) or not 0 <= index < len(items):
            return None
        return items[index], at if at.tzinfo else at.replace(tzinfo=UTC)

    def showing(self) -> dict[str, Any] | None:
        """The status report's `showing`: what the TV shows, for the phone (the cloud
        contract's rules are checked in api.valid_showing). The name is the "Now showing"
        option (a catalog board name, a title, a webcam's or photo's name), never a URL or
        a key; `index` is the item's place in the rotation (shuffled or not), from 0."""
        held = self.held()
        if held is None:
            return None
        item, at = held
        src = self.spec.source
        names = option_names(items_of(src))
        order = rotation_order(src)
        return {
            "name": names[item.index],
            "kind": item.kind,
            "index": order.index(item.index),  # its position in the rotation, from 0
            "count": len(names),
            "since": rfc3339(at),
            "paused": self.paused,
        }

    def forget_shown(self, photos_only: bool = False, held: Container[str] | None = None) -> bool:
        """Drop the images kept for the dashboard (what the TV shows and the preview): all
        of them, or only a phone photo's, or only a phone photo HA no longer *held*.
        True when something was dropped."""
        dropped = False
        for attr in ("now_showing", "preview"):
            kept: NowShowing | None = getattr(self, attr)
            if kept is None or (photos_only and not kept.item.photo):
                continue
            if held is not None and kept.item.sha256 in held:
                continue
            setattr(self, attr, None)
            dropped = True
        return dropped

    def blocked(self) -> str | None:
        """Why a tick started now would not show anything, or None."""
        if not self.spec.enabled:
            return "the phone turned this TV off"
        if self._auth_failed:
            return "TVCanvas needs to be reconnected"
        retry = self._credits_retry_at
        if retry is not None and dt_util.utcnow() < retry and not runs_without_credits(self.spec):
            return "out of credits"
        return None

    async def show_item(self, index: int, image: bytes | None = None) -> None:
        """Show the spec's item *index* now; the rotation then carries on after it.

        Runs a tick at once (as a spec edit does), also while the rotation is paused,
        which then stays paused on that item. What it shows follows the tick's usual
        rules: a Frame not in TVCanvas mode, a photo HA doesn't hold yet, a board out
        of credits or an image that fails moves on as on any tick. *image*: the
        preview's bytes, shown instead of loading the item again."""
        async with self._lock:
            src = self.spec.source
            if not 0 <= index < len(items_of(src)):
                raise ValueError(f"no item {index}")
            await self._stop()
            if rotates(src):
                rotation = self._store.rotation(self.spec.tv.id)
                if rotation.get("key") != src.items_key:
                    rotation.clear()
                rotation.update(key=src.items_key, pos=rotation_order(src).index(index))
                self._store.schedule_save()
            self._manual = _Manual(index, image)
            self.start()

    async def advance(self) -> None:
        """ "Next image": the next tick now (the rotation's next item; a board pin renders
        its board again). Also while paused, which then stays paused on the new item."""
        async with self._lock:
            await self._stop()
            self._manual = _Manual()
            self.start()

    async def show_preview(self) -> None:
        """ "Show preview on TV": the previewed item, with the preview's own image."""
        preview = self.preview
        if preview is None or preview.source != self.spec.source:
            raise PreviewRefused("pick an item in Preview first")
        await self.show_item(preview.item.index, preview.image)

    async def _preview_refused(self, item: Item) -> str | None:
        if self._auth_failed:
            return "TVCanvas needs to be reconnected"
        if not self.spec.enabled:
            return "the phone turned this TV off"
        if not item.costs_credit:
            return None
        retry = self._credits_retry_at
        if retry is not None and dt_util.utcnow() < retry:
            return "out of credits"
        asleep = getattr(self._display, "asleep", None)
        if self._standby or (asleep is not None and await asleep()):
            return "the TV is in standby, and a board preview uses a credit"
        return None

    async def preview_item(self, index: int) -> None:
        """Make the "Preview image" of the spec's item *index*, without touching the TV.

        Only ever called for a user's pick in the "Preview" select. A board renders
        through the cloud and uses a credit, as on the phone, so it is refused while the
        TV is in standby or out of credits, and allowed once per PREVIEW_BOARD_SECONDS;
        anything else once per PREVIEW_OTHER_SECONDS. Raises PreviewRefused."""
        src = self.spec.source
        items = items_of(src)
        if not 0 <= index < len(items):
            raise ValueError(f"no item {index}")
        item = items[index]
        if self._previewing:
            raise PreviewRefused("a preview is still loading")
        self._previewing = True
        try:
            if (why := await self._preview_refused(item)) is not None:
                raise PreviewRefused(why)
            now = time.monotonic()
            wait = self._preview_next.get(item.costs_credit, -math.inf) - now
            if wait > 0:
                what = "board preview" if item.costs_credit else "preview"
                raise PreviewRefused(f"one {what} at a time: try again in {math.ceil(wait)} s")
            every = PREVIEW_BOARD_SECONDS if item.costs_credit else PREVIEW_OTHER_SECONDS
            self._preview_next[item.costs_credit] = now + every
            try:
                jpeg = await load_item(
                    self.spec, index, self._client, self._session, self._fit, self._photos
                )
            except AuthError as err:
                self._auth_failed = True
                self._on_auth_error()
                raise PreviewRefused("TVCanvas needs to be reconnected") from err
            except OutOfCredits as err:
                raise PreviewRefused("out of credits") from err
            except RateLimited as err:
                raise PreviewRefused("TVCanvas Cloud is busy: try again in a minute") from err
            except SourceError as err:
                raise PreviewRefused(redact(str(err))) from err
        finally:
            self._previewing = False
        if self.spec.source != src:
            return  # the phone changed the pin meanwhile: the preview belongs to the old one
        self.preview = NowShowing(src, item, jpeg, dt_util.utcnow())
        self._on_update()

    def _waiting_for_photos(self) -> str | None:
        """`waiting: photos N` while N of the group's phone photos are missing."""
        local = self.spec.source.local_hashes
        if not local:
            return None
        missing = self._photos.missing(self.spec) if self._photos is not None else len(local)
        return f"waiting: photos {missing}" if missing else None

    async def _ready(self) -> str | None:
        """Ask the display whether to run this tick; a string is why it waits."""
        ready = getattr(self._display, "ready", None)
        return await ready() if ready is not None else None

    async def _set_paused(self, now: datetime) -> None:
        """A tick while the rotation is paused: the TV is not touched at all (no power or
        mode check, no fetch, no upload, no credits). The cloud has no paused status, so
        it gets `ok: true` (as for standby) with `showing.paused`, once per interval."""
        self._failures, self._unreachable = 0, False
        self._standby, self._standby_reported = False, None
        self.status.last_error = None
        self._on_update()
        await self._report(True, None, now)

    async def _set_standby(self, now: datetime, was_standby: bool) -> None:
        """Record standby: not an error, and HA's last_ok stays at the last image shown.

        The cloud has no standby status (ha-sync.md), so it gets `ok: true`, the same as
        an update, which keeps the phone from showing an error or "hasn't checked in".
        Sent when standby starts and then once per interval, not on every 30 s poll."""
        if was_standby and self._standby_reported is not None:
            if now - self._standby_reported < timedelta(seconds=self.spec.interval_seconds):
                return
        else:
            _LOGGER.debug("TVCanvas: %s is in standby; paused until it is on", self.spec.tv.name)
        self._standby_reported = now
        self.status.last_error = None
        self._on_update()
        await self._report(True, None, now)

    async def _set(
        self, ok: bool, error: str | None, category: str | None = None, shown: bool = False
    ) -> None:
        """Record the result. HA keeps the detailed error; the cloud (and so the
        phone) only gets the generic category, never LAN probing detail.

        shown: the TV did update although the tick reports a problem (a group
        skipping its boards), so HA's last_ok moves; the cloud's can't (ok=false).
        """
        now = dt_util.utcnow()
        if ok:
            self.status.last_ok, self.status.last_error = now, None
        else:
            if shown:
                self.status.last_ok = now
            self.status.last_error = error
        self._on_update()
        await self._report(ok, None if ok else category, now)

    async def _report(self, ok: bool, category: str | None, at: datetime) -> None:
        self._last_report, self._reported_at = (ok, category), time.monotonic()
        self._reports += 1
        try:
            await self._client.agent_status(self.spec.tv.id, ok, category, at, self.showing())
        except AuthError:
            self._auth_failed = True
            self._on_auth_error()
        except CloudError as err:
            _LOGGER.debug("TVCanvas: status report failed: %s", err)

    def _showing_changed(self) -> None:
        """Tell the phone now (not at the next tick) that `showing` changed: one report,
        SHOWING_REPORT_MIN_SECONDS after the TV's last one at the earliest. Changes in
        between ride on that one report, and a tick's own report in between replaces it."""
        self._changed_after = self._reports
        if self._last_report is None or self._change_task is not None:
            return  # nothing reported yet (the first tick will), or a report is due
        if self._auth_failed:
            return
        delay = max(0.0, self._reported_at + SHOWING_REPORT_MIN_SECONDS - time.monotonic())
        self._change_task = self._hass.async_create_background_task(
            self._report_change(delay), f"tvcanvas showing {self.spec.tv.id}"
        )

    async def _report_change(self, delay: float) -> None:
        try:
            if delay:
                await asyncio.sleep(delay)
            if self._reports != self._changed_after or self._last_report is None:
                return  # a tick reported since the last change
            ok, category = self._last_report
            await self._report(ok, category, dt_util.utcnow())
        finally:
            if self._change_task is asyncio.current_task():
                self._change_task = None

    async def _show(self, jpeg: bytes) -> None:
        """Run display.show to completion even if the tick is cancelled."""
        show = asyncio.ensure_future(self._display.show(jpeg))
        try:
            await asyncio.shield(show)
        except asyncio.CancelledError:
            with suppress(Exception):
                await show
            raise

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                _LOGGER.exception("TVCanvas: unexpected error for %s", self.spec.tv.name)
                self._failures += 1
                self._unreachable = False
                try:
                    await self._set(ok=False, error="internal error", category="internal error")
                except Exception:
                    _LOGGER.exception("TVCanvas: status update failed")
            await self._wait()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = self._hass.async_create_background_task(
                self._loop(), f"tvcanvas runner {self.spec.tv.id}"
            )

    async def _stop(self) -> None:
        change, self._change_task = self._change_task, None
        if change is not None:
            change.cancel()
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except Exception:
            _LOGGER.exception("TVCanvas: runner task for %s ended with an error", self.spec.tv.id)

    def release(self) -> None:
        """The spec was removed: let the display clean up what HA left on the TV."""
        release = getattr(self._display, "release", None)
        if release is not None:
            release()

    async def stop(self) -> None:
        async with self._lock:
            await self._stop()

    async def update(self, spec: SyncSpec, display: Display) -> None:
        async with self._lock:
            await self._stop()
            if spec.source != self.spec.source:
                self._credits_retry_at = None
            self.spec, self._display = spec, display
            self.forget_shown()  # its item may have left the list; the next tick refills it
            self._manual = None  # a pick or preview of the old list
            self._refused_logged = False
            self._failures = 0
            self._unreachable = False
            self._auth_failed = False
            self._standby, self._standby_reported = False, None
            self.start()
