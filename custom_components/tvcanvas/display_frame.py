"""Samsung Frame Art Mode, through samsungtvws."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from samsungtvws import SamsungTVArt
from samsungtvws.exceptions import ResponseError

from .const import (
    FRAME_CLIENT_NAME,
    FRAME_DELETE_MAX_ATTEMPTS,
    FRAME_LOST_SLACK_SECONDS,
    FRAME_MY_PHOTOS,
    FRAME_PAIR_TIMEOUT_SECONDS,
    FRAME_STALE_MAX,
    FRAME_UPLOADED_MAX,
    FRAME_WS_PORT,
)
from .displays import STANDBY, DisplayError, HostRefused, home_literal, tv_address
from .follow import FollowsTV
from .locate import Found, FrameFinder, mac_from_tv_id
from .storage import StateStore

_LOGGER = logging.getLogger(__name__)


class UploadedNotShown(Exception):
    """The image reached the TV's storage but select_image failed."""

    def __init__(self, content_id: str, token: str | None) -> None:
        super().__init__(content_id)
        self.content_id, self.token = content_id, token


class UploadLost(Exception):
    """upload() failed; the TV may still have stored the image under an id HA never got."""

    def __init__(
        self, before: list[str], sent: datetime, failed: datetime, token: str | None
    ) -> None:
        super().__init__("upload lost")
        self.before, self.sent, self.failed, self.token = before, sent, failed, token


def _now() -> datetime:
    """Local wall-clock time to the second: what samsungtvws stamps as image_date."""
    return datetime.now().replace(microsecond=0)


def _image_date(item: dict[str, Any]) -> datetime | None:
    digits = re.sub(r"\D", "", str(item.get("image_date") or ""))
    if len(digits) < 14:
        return None
    try:
        return datetime(
            int(digits[0:4]),
            int(digits[4:6]),
            int(digits[6:8]),
            int(digits[8:10]),
            int(digits[10:12]),
            int(digits[12:14]),
        )
    except ValueError:
        return None


def _match_lost(
    items: Iterable[dict[str, Any]], lost: dict[str, Any], tracked: set[str]
) -> tuple[list[str], list[str]]:
    """Find the image a failed upload left behind; return (adopted, ambiguous).

    A candidate is in My Photos now, was not in the snapshot taken before the
    failed upload, is not an id HA already tracks, and is dated inside the failed
    upload's window. Exactly one candidate is HA's; with more, the user uploaded
    in the same window and none is adopted: HA never deletes what it can't prove
    it uploaded.
    """
    slack = timedelta(seconds=FRAME_LOST_SLACK_SECONDS)
    low = datetime.fromisoformat(lost["sent"]) - slack
    high = datetime.fromisoformat(lost["failed"]) + slack
    before = set(lost["before"])
    candidates = []
    for item in items:
        cid = item.get("content_id")
        if not cid or cid in before or cid in tracked:
            continue
        when = _image_date(item)
        if when is not None and low <= when <= high:
            candidates.append(cid)
    if len(candidates) == 1:
        return candidates, []
    return [], candidates


def _art(host: str, token: str | None) -> SamsungTVArt:
    """Every connection to a Frame starts here, so this is where an address that is not
    on the home network is refused (#14): pairing, reading, uploading and deleting."""
    art = SamsungTVArt(
        home_literal(host),
        token=token,
        port=FRAME_WS_PORT,
        timeout=FRAME_PAIR_TIMEOUT_SECONDS,
        name=FRAME_CLIENT_NAME,
    )
    _keep_websocket_at_home(art)
    _keep_upload_at_home(art)
    return art


_MOVED = "the TV moved the connection to an address that is not on the home network"
_UPLOAD_REFUSED = "the TV asked for the image at an address that is not on the home network"
_UPLOAD_UNCHECKABLE = (
    "upload refused: this samsungtvws version does not let Home Assistant check that the "
    "image goes to an address on the home network"
)
_upload_hook_missing_logged = False


def _keep_websocket_at_home(art: SamsungTVArt) -> None:
    """websocket-client follows HTTP redirects while it connects (up to 3), so whatever
    answers at the TV's address can move the connection elsewhere. Check the address the
    socket is really connected to before anything is asked of it or sent to it."""
    open_websocket = getattr(art, "open", None)
    if open_websocket is None:  # only a test double; open() is samsungtvws' public API
        return

    def checked() -> Any:
        connection = open_websocket()
        try:
            peer = connection.sock.getpeername()[0]
        except (AttributeError, OSError, TypeError, IndexError) as err:
            art.close()
            raise OSError("could not read the address the TV connection goes to") from err
        try:
            home_literal(peer)
        except HostRefused:
            art.close()
            raise HostRefused(_MOVED) from None
        return connection

    art.open = checked


def _keep_upload_at_home(art: SamsungTVArt) -> None:
    """upload() sends the image over a second socket, to the address the TV names in its
    reply (`conn_info`), not to the host HA connected to. Refuse one off the home network.

    `_open_d2d_socket` is private to samsungtvws (3.0.5); tests/test_home_network.py runs
    the library's real upload() through this guard, so a release that moves it fails CI.
    A release without the hook fails closed: every upload is refused (HostRefused, so the
    runner reports it and never probes), while reading the mode and deleting still work."""
    open_socket = getattr(art, "_open_d2d_socket", None)
    if open_socket is None:
        global _upload_hook_missing_logged
        if not _upload_hook_missing_logged:
            _upload_hook_missing_logged = True
            _LOGGER.error(
                "TVCanvas: this samsungtvws version has no _open_d2d_socket, so the address "
                "a Frame names for an upload cannot be checked; uploads to Frames are refused"
            )

        def refused(*_args: Any, **_kwargs: Any) -> Any:
            raise HostRefused(_UPLOAD_UNCHECKABLE)

        art.upload = refused
        return

    def checked(conn_info: Any) -> Any:
        named = conn_info.get("ip") if isinstance(conn_info, dict) else None
        try:
            home_literal(named)
        except HostRefused:
            raise HostRefused(_UPLOAD_REFUSED) from None
        return open_socket(conn_info)

    art._open_d2d_socket = checked


def _recover_sync(
    host: str, token: str | None, lost: dict[str, Any], tracked: set[str]
) -> tuple[list[str], list[str], str | None]:
    """List My Photos once and match the image a failed upload left behind."""
    art = _art(host, token)
    try:
        items = art.available(FRAME_MY_PHOTOS)
        adopted, ambiguous = _match_lost(items, lost, tracked)
        return adopted, ambiguous, art.token
    finally:
        art.close()


WAITING = {"watch_tv": "waiting: Watch TV", "art": "waiting: Art"}


def _slideshow_active(status: Any) -> bool:
    value = status.get("value") if isinstance(status, dict) else None
    try:
        return int(str(value)) > 0
    except ValueError:
        return False


def _mode_sync(
    host: str, token: str | None, delete_in_art: list[str]
) -> tuple[str, str | None, list[str]]:
    """Read the Frame's mode the way the TVCanvas app does.

    Art Mode off is "watch_tv"; on with the TV's own slideshow running is "art";
    on with it stopped is "tvcanvas". A firmware that doesn't answer these
    requests counts as "tvcanvas", so HA is never blocked by it. In Art, HA's
    own images are deleted so they don't join the user's slideshow.
    """
    art = _art(host, token)
    try:
        try:
            artmode = art.get_artmode()
        except ResponseError:
            return "tvcanvas", art.token, []
        if artmode == "off":
            return "watch_tv", art.token, []
        if artmode != "on":
            return "tvcanvas", art.token, []
        try:
            active = _slideshow_active(art.get_slideshow_status())
        except ResponseError:
            active = False
        if not active:
            return "tvcanvas", art.token, []
        deleted, _ = _cleanup(art, host, delete_in_art)
        return "art", art.token, deleted
    finally:
        art.close()


def _release_sync(host: str, token: str | None, ids: list[str]) -> list[str]:
    art = _art(host, token)
    try:
        deleted, _ = _cleanup(art, host, ids)
        return deleted
    finally:
        art.close()


def _cleanup(art: SamsungTVArt, host: str, targets: list[str]) -> tuple[list[str], list[str]]:
    """Delete ids HA uploaded; return (deleted, failed). Never raises."""
    if not targets:
        return [], []
    try:
        if art.delete_list(targets):
            return targets, []
    except Exception as err:  # noqa: BLE001 - fall back to one id at a time
        _LOGGER.debug("Frame %s: batch delete failed: %s", host, err)
    deleted: list[str] = []
    failed: list[str] = []
    for cid in targets:
        try:
            ok = art.delete(cid)
        except ResponseError:
            ok = False
        except Exception as err:  # noqa: BLE001 - connection gone: retry next tick, no strike
            _LOGGER.debug("Frame %s: delete stopped: %s", host, err)
            break
        (deleted if ok else failed).append(cid)
    return deleted, failed


def _show_sync(
    host: str, token: str | None, jpeg: bytes, to_delete: list[str], snapshot: bool = False
) -> tuple[str, str | None, list[str], list[str]]:
    art = _art(host, token)
    try:
        before = (
            [i["content_id"] for i in art.available(FRAME_MY_PHOTOS) if i.get("content_id")]
            if snapshot
            else None
        )
        sent = _now()
        try:
            content_id = art.upload(jpeg, matte="none", portrait_matte="none", file_type="jpg")
        except HostRefused:
            raise  # nothing was sent, so nothing is lost on the TV
        except Exception as err:
            if before is None:
                raise
            raise UploadLost(before, sent, _now(), art.token) from err
        try:
            art.select_image(content_id, show=True)
        except Exception as err:
            raise UploadedNotShown(content_id, art.token) from err
        deleted, failed = _cleanup(art, host, [cid for cid in to_delete if cid != content_id])
        return content_id, art.token, deleted, failed
    finally:
        art.close()


class FrameDisplay(FollowsTV):
    KIND = NOUN = "Frame"
    MOVED_EXTRA = "; the TV may ask you once to allow TVCanvas-HA"

    def __init__(
        self,
        hass: HomeAssistant,
        store: StateStore,
        tv_id: str,
        host: str,
        replace_previous: bool,
        on_pairing: Callable[[bool], None],
        finder: FrameFinder | None = None,
    ) -> None:
        self._hass, self._store, self._tv_id, self._host = hass, store, tv_id, host
        self._replace = replace_previous
        self._on_pairing = on_pairing
        # Finds and re-checks the TV by the MAC in its id after a DHCP move (locate.py).
        self._finder, self._mac = finder, mac_from_tv_id(tv_id)
        self._init_follow()
        # The address `_ready()` checked this tick; `show()` connects only there.
        self._verified: str | None = None
        # PowerState from /api/v2/ this tick ("on", "standby"); None when unknown.
        self.power_state: str | None = None
        # Logged once per pin: with Replace previous off every image stays in My Photos.
        self._kept_logged = False
        # Ids whose failed delete was already logged (the retry each tick is not).
        self._delete_warned: set[str] = set()

    # FollowsTV: a moved Frame is found, and checked, by the MAC in its id (locate.py).

    def _follow_state(self) -> dict:
        return self._store.frame(self._tv_id)

    def _can_follow(self) -> bool:
        return self._finder is not None and self._mac is not None

    async def _find(self, skip: str) -> Found | None:
        assert self._finder is not None and self._mac is not None  # _can_follow checked
        return await self._finder.find(self._mac, skip)

    async def _check(self, address: str) -> Found:
        assert self._finder is not None and self._mac is not None  # _followed checked
        return await self._finder.check(address, self._mac)

    def _store_changed(self) -> None:
        self._store.schedule_save()

    async def show(self, jpeg: bytes) -> None:
        # The address ready() checked this tick, else checked now: never an unchecked one.
        address, self._verified = self._verified or await self._checked_address(), None
        host = await tv_address(address)  # first: a refused host is never asked to pair
        state = self._store.frame(self._tv_id)
        attempts: dict[str, int] = state.setdefault("attempts", {})
        # Tokens are kept per address: one saved for the spec's host is never sent to an
        # address HA followed the TV to (a device there could be pretending to be it).
        token = self._store.frame_token(address)
        to_delete = self._tracked(state) if self._replace else []
        pairing = token is None
        if pairing:
            self._on_pairing(True)
        try:
            if self._replace and state.get("lost"):
                token = await self._recover(host, address, state, token)
                to_delete = self._tracked(state)  # now with the adopted image
            content_id, new_token, deleted, failed = await self._hass.async_add_executor_job(
                partial(_show_sync, host, token, jpeg, to_delete, snapshot=self._replace)
            )
        except UploadLost as err:
            self._save_token(err.token, token, address)
            state["lost"] = {
                "before": err.before,
                "sent": err.sent.isoformat(),
                "failed": err.failed.isoformat(),
            }
            self._store.schedule_save()
            raise DisplayError(f"TV unreachable: {err.__cause__}") from err
        except DisplayError:
            raise
        except UploadedNotShown as err:
            self._save_token(err.token, token, address)
            self._owned(err.content_id)
            if self._replace:  # on the TV but never shown: clean it up next time
                self._set_stale(state, [*state["stale"], err.content_id])
            self._store.schedule_save()
            raise DisplayError(f"TV did not show the image: {err.__cause__}") from err
        except Exception as err:  # samsungtvws raises OSError, websocket and its own errors
            raise DisplayError(f"TV unreachable: {err}") from err
        finally:
            if pairing:
                self._on_pairing(False)

        self._save_token(new_token, token, address)
        state["current"] = content_id
        self._owned(content_id)
        self._gone(deleted)
        if not self._replace:
            state["stale"], state["attempts"] = [], {}
            state.pop("lost", None)
            self._store.schedule_save()
            if not self._kept_logged:
                self._kept_logged = True
                _LOGGER.warning(
                    "Frame %s: Replace previous is off for this pin, so every image Home "
                    "Assistant shows stays in My Photos (%d so far); it records which are "
                    "its own and deletes none of them",
                    self._host,
                    len(self._store.frame_uploaded(self._tv_id)),
                )
            return
        for cid in failed:
            attempts[cid] = attempts.get(cid, 0) + 1
        given_up = [cid for cid in failed if attempts[cid] >= FRAME_DELETE_MAX_ATTEMPTS]
        if given_up:
            _LOGGER.warning(
                "Frame %s: could not delete %s after %d attempts; leaving it on the TV",
                self._host,
                ", ".join(given_up),
                FRAME_DELETE_MAX_ATTEMPTS,
            )
        left = [
            cid
            for cid in to_delete
            if cid not in deleted and cid != content_id and cid not in given_up
        ]
        # Refused by the TV, or not reached because the connection broke: both stay in
        # the stale list and are deleted on the next update. Said once per id.
        if unlogged := [cid for cid in left if cid not in self._delete_warned]:
            self._delete_warned.update(unlogged)
            _LOGGER.warning(
                "Frame %s: could not delete %s from My Photos; trying again on the next update",
                self._host,
                ", ".join(unlogged),
            )
        self._set_stale(state, left)
        self._store.schedule_save()

    def _owned(self, *cids: str) -> None:
        """Record images HA just put in My Photos (oldest first, capped)."""
        uploaded = self._store.frame_uploaded(self._tv_id)
        when = dt_util.utcnow().isoformat(timespec="seconds")
        for cid in cids:
            uploaded.setdefault(cid, when)
        while len(uploaded) > FRAME_UPLOADED_MAX:
            del uploaded[next(iter(uploaded))]
        self._store.schedule_save()

    def _gone(self, cids: Iterable[str]) -> None:
        """Forget images HA deleted from My Photos."""
        uploaded = self._store.frame_uploaded(self._tv_id)
        for cid in cids:
            uploaded.pop(cid, None)
            self._delete_warned.discard(cid)
        self._store.schedule_save()

    @staticmethod
    def _tracked(state: dict) -> list[str]:
        """Every id HA uploaded and still owns on this TV, stale first, no duplicates."""
        return list(
            dict.fromkeys([*state["stale"], *([state["current"]] if state["current"] else [])])
        )

    async def ready(self) -> str | None:
        """None when the Frame shows TVCanvas; otherwise the waiting status.

        Every tick starts here, so this is where a Frame that stopped answering is looked
        for at a new address (the router gave it a new DHCP lease) and, when found there,
        asked again at once."""
        self._verified, self.power_state = None, None
        try:
            return await self._ready()
        except HostRefused:
            raise
        except DisplayError:
            if not await self._follow():
                raise
        return await self._ready()

    async def _ready(self) -> str | None:
        address = await self._checked_address()
        host = await tv_address(address)  # first: a refused host is never asked to pair
        self._verified = address
        if self._finder is not None:
            # Over plain HTTP, before the WebSocket: no token, no pairing prompt. A Frame in
            # standby gets nothing until it is back on. No answer or no PowerState (older
            # firmware): carry on as before, and the WebSocket says if the TV is gone.
            self.power_state = await self._finder.power_state(host)
            if self.power_state == STANDBY:
                return STANDBY
        state = self._store.frame(self._tv_id)
        token = self._store.frame_token(address)
        delete_in_art = self._tracked(state) if self._replace else []
        pairing = token is None
        if pairing:
            self._on_pairing(True)
        try:
            mode, new_token, deleted = await self._hass.async_add_executor_job(
                _mode_sync, host, token, delete_in_art
            )
        except DisplayError:
            raise
        except Exception as err:  # samsungtvws raises OSError, websocket and its own errors
            raise DisplayError(f"TV unreachable: {err}") from err
        finally:
            if pairing:
                self._on_pairing(False)
        self._save_token(new_token, token, address)
        if deleted:
            self._gone(deleted)
            if state["current"] in deleted:
                state["current"] = None
            self._set_stale(state, [cid for cid in state["stale"] if cid not in deleted])
            self._store.schedule_save()
        return WAITING.get(mode)

    async def asleep(self) -> bool:
        """The Frame says it is in standby. Read-only: one plain-HTTP GET of /api/v2/ at
        the address HA last used, no WebSocket, no pairing prompt, nothing deleted."""
        if self._finder is None:
            return False
        try:
            host = await tv_address(self.address)
        except HostRefused:
            return False
        return await self._finder.power_state(host) == STANDBY

    def release(self) -> None:
        """The spec was removed: delete HA's images in the background, best effort."""
        if not self._replace:
            return
        # The coordinator forgets the TV's state right after this: keep it for the task.
        state = self._store.frame(self._tv_id)
        ids = self._tracked(state)
        if not ids:
            return
        self._hass.async_create_background_task(
            self._release(ids, state), f"tvcanvas release {self._tv_id}"
        )

    async def _release(self, ids: list[str], state: dict) -> None:
        try:
            address = await self._checked_address(state)  # before any token is read
            token = self._store.frame_token(address)
            host = await tv_address(address)
            deleted = await self._hass.async_add_executor_job(_release_sync, host, token, ids)
        except Exception as err:  # noqa: BLE001 - best effort; the TV may be off
            _LOGGER.debug("Frame %s: release failed: %s", self._host, err)
            deleted = []
        self._gone(deleted)
        left = [cid for cid in ids if cid not in deleted]
        if left:
            _LOGGER.warning(
                "Frame %s: TVCanvas no longer runs this TV but could not delete %s from "
                "My Photos; delete them on the TV if you don't want them",
                self._host,
                ", ".join(left),
            )

    async def _recover(self, host: str, address: str, state: dict, token: str | None) -> str | None:
        """Adopt the image a failed upload left behind (#2); return the token to use."""
        try:
            adopted, ambiguous, new_token = await self._hass.async_add_executor_job(
                _recover_sync, host, token, state["lost"], set(self._tracked(state))
            )
        except DisplayError:
            raise
        except Exception as err:  # still off: keep the record for the next tick
            raise DisplayError(f"TV unreachable: {err}") from err
        self._save_token(new_token, token, address)
        del state["lost"]
        if adopted:
            self._owned(*adopted)
            self._set_stale(state, [*state["stale"], *adopted])
        if ambiguous:
            _LOGGER.warning(
                "Frame %s: an upload failed and could not tell which new image is HA's "
                "(%s); leaving them all in My Photos",
                self._host,
                ", ".join(ambiguous),
            )
        self._store.schedule_save()
        return new_token or token

    def _save_token(self, new_token: str | None, old_token: str | None, address: str) -> None:
        if new_token and new_token != old_token:
            self._store.set_frame_token(address, new_token)

    @staticmethod
    def _set_stale(state: dict, stale: list[str]) -> None:
        stale = list(dict.fromkeys(stale))[-FRAME_STALE_MAX:]
        state["stale"] = stale
        state["attempts"] = {cid: n for cid, n in state.get("attempts", {}).items() if cid in stale}
