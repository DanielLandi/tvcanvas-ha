"""Google Cast via the Default Media Receiver."""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any

import pychromecast
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError, get_url
from pychromecast.error import RequestTimeout
from pychromecast.socket_client import (
    CONNECTION_STATUS_CONNECTED,
    ConnectionStatus,
    ConnectionStatusListener,
)

from .const import CAST_LOAD_TIMEOUT_SECONDS, CAST_PORT, IMAGE_VIEW_URL
from .display_view import ImageCache
from .displays import DisplayError, home_literal, tv_address
from .follow import FollowsTV
from .locate import Check, Found
from .locate_cast import CastFinder, CastIdentity
from .storage import StateStore

_LOGGER = logging.getLogger(__name__)


class CastLoadError(Exception):
    """The TV did not confirm it loaded the image."""


class CastUnreachable(Exception):
    """No connection to the TV at that address: refused, no route, or not connected in
    time. Only this makes HA look for the TV elsewhere; a TV that connected and then
    answered slowly, or not at all, is still where it was."""


_NS = uuid.UUID("6f1b6f52-5c55-4c7e-9f0e-7a5c0f2b6d11")


def _load_errored(response: dict[str, Any]) -> bool:
    return any(
        isinstance(st, dict) and st.get("playerState") == "IDLE" and st.get("idleReason") == "ERROR"
        for st in response.get("status") or []
    )


class _Connected(ConnectionStatusListener):
    """Remembers whether the socket to the TV ever connected."""

    def __init__(self) -> None:
        self.event = threading.Event()

    def new_connection_status(self, status: ConnectionStatus) -> None:
        if status.status == CONNECTION_STATUS_CONNECTED:
            self.event.set()


def _play_sync(host: str, uid: uuid.UUID, url: str) -> None:
    # Every connection to a Cast TV starts here: an address that is not on the home
    # network is refused before pychromecast gets it (#14).
    cast = pychromecast.get_chromecast_from_host(
        (home_literal(host), CAST_PORT, uid, None, None), tries=1, timeout=10
    )
    try:
        # pychromecast connects in its own thread, started by wait(): a connection that
        # fails or never completes shows up only as wait()'s timeout.
        connected = _Connected()
        cast.socket_client.register_connection_listener(connected)
        try:
            cast.wait(timeout=10)
        except RequestTimeout as err:
            if not connected.event.is_set():
                raise CastUnreachable("no connection to the TV") from err
            raise
        mc = cast.media_controller
        done = threading.Event()
        result: dict[str, Any] = {}

        def on_load(sent: bool, response: dict | None) -> None:
            result.update(sent=sent, response=response or {})
            done.set()

        # The LOAD reply (MEDIA_STATUS, or LOAD_FAILED / INVALID_REQUEST) answers
        # this request; a session left over from an earlier cast says nothing.
        mc.play_media(url, "image/jpeg", callback_function=on_load)
        if not done.wait(CAST_LOAD_TIMEOUT_SECONDS):
            raise CastLoadError("the TV did not confirm the image")
        response = result["response"]
        if not result["sent"]:
            raise CastLoadError("the image could not be sent")
        if response.get("type") != "MEDIA_STATUS":
            raise CastLoadError(f"the TV answered {response.get('type') or 'nothing'}")
        status = mc.status
        if _load_errored(response) or (
            status is not None and status.player_state == "IDLE" and status.idle_reason == "ERROR"
        ):
            raise CastLoadError("the TV could not open the image")
    finally:
        cast.disconnect(timeout=5)


class CastDisplay(FollowsTV):
    """Shows images on a Cast TV and follows it to a new DHCP address (follow.py).

    The first image shown at the spec's host records the TV's identity, bound to that
    host (locate_cast.py); when the TV stops accepting connections there, it is looked
    for by that identity."""

    KIND = "Cast TV"
    NOUN = "Cast TV"

    def __init__(
        self,
        hass: HomeAssistant,
        cache: ImageCache,
        tv_id: str,
        host: str,
        store: StateStore | None = None,
        finder: CastFinder | None = None,
    ) -> None:
        self._hass, self._cache, self._tv_id, self._host = hass, cache, tv_id, host
        self._uid = uuid.uuid5(_NS, tv_id)
        self._store, self._finder = store, finder
        self._init_follow()
        # The address `ready()` checked this tick; `show()` connects only there.
        self._verified: str | None = None

    # FollowsTV

    def _follow_state(self) -> dict[str, Any]:
        return self._store.cast(self._tv_id) if self._store is not None else {}

    def _can_follow(self) -> bool:
        return self._store is not None and self._finder is not None

    def _identity(self) -> CastIdentity | None:
        """The identity learned at the spec's host, unless a device there has since
        reported a different one. One learned at another host (before the app sent this
        one) is not used: trust is bound to the host the app named."""
        state = self._follow_state()
        if self._conflict(state):
            return None
        learned = state.get("identity")
        if not isinstance(learned, dict) or learned.get("host") != self._host:
            return None
        return CastIdentity.from_dict(learned)

    def _conflict(self, state: dict[str, Any]) -> bool:
        return state.get("conflict") == self._host

    def _why_not_search(self) -> str | None:
        if self._conflict(self._follow_state()):
            return (
                f"the device at {self._host} reported a different identity than the TV it "
                "learned there, so it follows no move until the TVCanvas app sends the "
                "TV's address again"
            )
        if self._identity() is None:
            return (
                "it has not shown an image on this TV at this address yet, so it doesn't "
                "know how to recognise it"
            )
        return None

    async def _find(self, skip: str) -> Found | None:
        identity = self._identity()
        assert self._finder is not None and identity is not None  # checked before
        return await self._finder.find(identity, skip)

    async def _check(self, address: str) -> Found:
        identity = self._identity()
        assert self._finder is not None  # _followed checked
        if identity is None:
            return Found(address, Check.NOT_IT)
        return await self._finder.check(address, identity)

    def _store_changed(self) -> None:
        if self._store is not None:
            self._store.schedule_save()

    # The display

    async def ready(self) -> None:
        """Every tick starts here: a followed address is re-checked (and dropped when the
        TV is back at the spec's host or no longer checks out there)."""
        self._verified = None
        self._verified = await self._checked_address()

    async def show(self, jpeg: bytes) -> None:
        # The address ready() checked this tick, else checked now: never an unchecked one.
        address, self._verified = self._verified or await self._checked_address(), None
        host = await tv_address(address)  # first: nothing is cached for a refused host
        try:
            base = get_url(
                self._hass, allow_external=False, allow_cloud=False, prefer_external=False
            )
        except NoURLAvailableError as err:
            raise DisplayError(
                "Home Assistant has no local URL a Cast TV can reach — set one in "
                "Settings → System → Network"
            ) from err
        name = self._cache.put(self._tv_id, jpeg)
        url = base + IMAGE_VIEW_URL.format(name=name)
        try:
            await self._play(host, url)
        except CastUnreachable as err:
            # Nothing answers at that address: the TV may have a new DHCP lease. When it
            # is found (and checked) elsewhere, the same image goes there at once.
            if not await self._follow():
                raise DisplayError(f"TV unreachable: {err}") from err
            address = self.address  # just checked by _follow()
            host = await tv_address(address)
            try:
                await self._play(host, url)
            except CastUnreachable as again:
                raise DisplayError(f"TV unreachable: {again}") from again
        if address == self._host:
            await self._learn(host)

    async def _play(self, host: str, url: str) -> None:
        try:
            await self._hass.async_add_executor_job(_play_sync, host, self._uid, url)
        except (CastUnreachable, DisplayError):
            raise
        except CastLoadError as err:
            raise DisplayError(f"TV did not load the image: {err}") from err
        except Exception as err:  # pychromecast raises RequestTimeout, OSError, ...
            raise DisplayError(f"TV unreachable: {err}") from err

    async def _learn(self, host: str) -> None:
        """The TV just showed an image at the spec's host. Trust on first use, bound to
        that host: the identity is recorded only when none is stored for this host (the
        app sending a new host is what resets it). A device at the same host that later
        reports a different identity never replaces it: HA warns, notifies, and follows
        no move for this TV until the app sends another host. Never fails the tick."""
        if not self._can_follow():
            return
        assert self._finder is not None
        state = self._follow_state()
        try:
            identity = await self._finder.learn(host)
        except Exception as err:  # noqa: BLE001 - best effort: the image is shown
            _LOGGER.debug("Cast TV %s: could not read its identity: %s", self._tv_id, err)
            return
        if identity is None:
            _LOGGER.debug("Cast TV %s: could not read its identity at %s", self._tv_id, host)
            return
        if self._conflict(state):
            return  # already reported; only a new host from the app clears it
        learned = state.get("identity")
        if isinstance(learned, dict) and learned.get("host") == self._host:
            known = CastIdentity.from_dict(learned)
            if known == identity:
                persistent_notification.async_dismiss(self._hass, self._notice)
                return
            what = " and ".join(
                part
                for part, differs in (
                    ("Cast UUID", known is None or known.uuid != identity.uuid),
                    ("MAC address", known is None or known.mac != identity.mac),
                )
                if differs
            )
            state["conflict"] = self._host
            self._store_changed()
            _LOGGER.warning(
                "Cast TV %s: the device at %s reports a different %s than the TV Home "
                "Assistant learned there; it keeps the learned identity and follows no "
                "move for this TV until the TVCanvas app sends another address",
                self._tv_id,
                self._host,
                what,
            )
            self._notify(
                f"The device at {self._host} reports a different {what} than the Cast TV "
                "Home Assistant learned at that address. Home Assistant keeps sending images "
                "to the address the TVCanvas app named, but it will not follow this TV to a "
                "new address until the app sends the TV's address again. Open the TVCanvas "
                "app near the TV to update it, or reserve the TV's address on your router."
            )
            return
        # No identity for this host yet (first use, or the app sent a new host).
        state.pop("conflict", None)
        state["identity"] = {**identity.as_dict(), "host": self._host}
        self._store_changed()
        persistent_notification.async_dismiss(self._hass, self._notice)
        _LOGGER.debug("Cast TV %s: identity recorded at %s", self._tv_id, host)
