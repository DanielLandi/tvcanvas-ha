"""One TV's refresh loop."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta

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
    PROBE_TIMEOUT_SECONDS,
)
from .displays import STANDBY, Display, DisplayError, HostRefused, tv_address
from .imaging import decode_gate, fit_to_tv
from .models import SyncSpec
from .photos import PhotoStore
from .sources import (
    BoardCredits,
    SourceError,
    WaitingForPhone,
    next_image,
    runs_without_credits,
)
from .storage import StateStore
from .urlsafety import TV_HOST_REFUSED

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
        now = dt_util.utcnow()
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
                if credits.hit:  # a mixed group's board got 402: skip its boards for 30 min
                    self._credits_retry_at = now + timedelta(seconds=CREDITS_RETRY_SECONDS)
            self._store.schedule_save()
            await self._show(jpeg)
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
        try:
            await self._client.agent_status(self.spec.tv.id, ok, category, at)
        except AuthError:
            self._auth_failed = True
            self._on_auth_error()
        except CloudError as err:
            _LOGGER.debug("TVCanvas: status report failed: %s", err)

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
            self._refused_logged = False
            self._failures = 0
            self._unreachable = False
            self._auth_failed = False
            self._standby, self._standby_reported = False, None
            self.start()
