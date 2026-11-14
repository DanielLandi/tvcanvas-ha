"""The phone → HA photo API on HA's own HTTP server (docs/lan-api.md §4).

HA's port may be reachable from the internet, so these views answer only clients
on the home network (anyone else gets an empty 404, as if they didn't exist),
authenticate themselves (`X-TVCanvas-LAN-Key`), cap every body while streaming
it, accept a path segment only if it is 64 lowercase hex, and never log keys or
codes.
"""

from __future__ import annotations

import asyncio
import base64
import errno
import functools
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from ipaddress import IPv6Address, ip_address
from typing import Any

from aiohttp import web
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers import instance_id
from homeassistant.helpers.network import is_cloud_connection
from homeassistant.util.network import is_local

from .const import (
    LAN_API_VERSION,
    LAN_BODY_READ_SECONDS,
    LAN_BUSY_RETRY_SECONDS,
    LAN_DECODE_WAIT_SECONDS,
    LAN_IMAGE_BODY_MAX,
    LAN_KEY_HEADER,
    LAN_PAIR_BODY_MAX,
    LAN_PROVE_BODY_MAX,
    LAN_UPLOADS_AT_ONCE,
    LAN_UPLOADS_PER_PHONE,
    PHOTOS_BYTES_CAP,
    PHOTOS_FILES_CAP,
)
from .imaging import DecodeBusy, ImageError, check_upload, decode_gate
from .lan_pairing import LanPairing, Locked, PairError, Phone, prove
from .models import SHA256_HEX
from .photos import PhotosFull, PhotoStore

_LOGGER = logging.getLogger(__name__)
_NO_STORE = {"Cache-Control": "no-store"}


@dataclass
class LanHub:
    """What the views serve. Views can't be unregistered, so they are registered
    once per HA run and read the loaded entry's state from here (None: not set up)."""

    photos: PhotoStore | None = None
    pairing: LanPairing | None = None
    # #16: PUTs in progress per phone (by key id), and uploads waiting for or in a decode.
    uploading: dict[str, int] = field(default_factory=dict)
    decoding: int = 0


class _TooLarge(Exception):
    pass


def _json(data: Any, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None):
    return web.json_response(data, status=status, headers={**_NO_STORE, **(headers or {})})


def _error(status: int, error: str, **extra: Any) -> web.Response:
    return _json({"error": error, **extra}, status)


def _unauthorized() -> web.Response:
    return _error(HTTPStatus.UNAUTHORIZED, "unauthorized")


def _busy() -> web.Response:
    """HA is decoding other images: the phone's existing 429 handling retries."""
    return _error(HTTPStatus.TOO_MANY_REQUESTS, "rate_limited", retry_after=LAN_BUSY_RETRY_SECONDS)


def _not_set_up() -> web.Response:
    return web.Response(status=HTTPStatus.NOT_FOUND, headers=_NO_STORE)


def from_home(request: web.Request) -> bool:
    """The caller is on the home network.

    `request.remote` is the address HA resolved: the peer, or the client named in
    X-Forwarded-For when HA's `http:` config trusts that proxy. A request through
    Home Assistant Cloud's remote UI arrives from a local tunnel, so it is refused
    explicitly."""
    hass: HomeAssistant = request.app[KEY_HASS]
    if is_cloud_connection(hass):
        return False
    try:
        return is_local(ip_address(request.remote or ""))
    except ValueError:
        return False


def _home_only(
    handler: Callable[..., Awaitable[web.StreamResponse]],
) -> Callable[..., Awaitable[web.StreamResponse]]:
    @functools.wraps(handler)
    async def wrapped(self, request: web.Request, *args, **kwargs) -> web.StreamResponse:
        if not from_home(request):
            return web.Response(status=HTTPStatus.NOT_FOUND)  # empty: nothing to see here
        return await handler(self, request, *args, **kwargs)

    return wrapped


async def _read_capped(request: web.Request, cap: int) -> bytes:
    """The whole body, refusing anything over *cap* bytes without reading it all."""
    declared = request.content_length
    if declared is not None and declared > cap:
        raise _TooLarge
    buf = bytearray()
    async for chunk in request.content.iter_chunked(65536):
        buf += chunk
        if len(buf) > cap:
            raise _TooLarge
    return bytes(buf)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _LanView(HomeAssistantView):
    requires_auth = False  # the phone has no HA login; the LAN key is checked below

    def __init__(self, hub: LanHub) -> None:
        self._hub = hub

    def _phone(self, request: web.Request) -> tuple[LanHub, Phone] | None:
        hub = self._hub
        if hub.pairing is None or hub.photos is None:
            return None
        phone = hub.pairing.authenticate(request.headers.get(LAN_KEY_HEADER))
        return (hub, phone) if phone is not None else None


class LanInfoView(_LanView):
    url = "/api/tvcanvas/lan/info"
    name = "api:tvcanvas:lan:info"

    @_home_only
    async def get(self, request: web.Request) -> web.Response:
        pairing = self._hub.pairing
        if pairing is None:
            return _not_set_up()
        body: dict[str, Any] = {"api": LAN_API_VERSION, "pairing_open": pairing.pairing_open}
        if self._phone(request) is not None:  # HA's identity only for a paired phone
            body.update(await _identity(request.app[KEY_HASS]))
        return _json(body)


async def _identity(hass: HomeAssistant) -> dict[str, str]:
    return {"name": hass.config.location_name, "instance_id": await instance_id.async_get(hass)}


class LanPairView(_LanView):
    url = "/api/tvcanvas/lan/pair"
    name = "api:tvcanvas:lan:pair"

    @_home_only
    async def post(self, request: web.Request) -> web.Response:
        pairing = self._hub.pairing
        if pairing is None:
            return _not_set_up()
        try:
            raw = await _read_capped(request, LAN_PAIR_BODY_MAX)
        except _TooLarge:
            return _error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large")
        # A web page open at home can POST text/plain or a form cross-site without a CORS
        # preflight, and 5 wrong codes kill the live one (#17): only a JSON body is a guess.
        if request.content_type != "application/json":
            return _error(HTTPStatus.BAD_REQUEST, "bad_request")
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return _error(HTTPStatus.BAD_REQUEST, "bad_request")
        try:
            key = await pairing.async_pair(
                body.get("code"), body.get("device_name"), request.remote or ""
            )
        except Locked as err:
            return _error(
                HTTPStatus.TOO_MANY_REQUESTS,
                "locked",
                retry_after=err.retry_after,
            )
        except PairError:
            return _error(HTTPStatus.FORBIDDEN, "bad_code")
        _LOGGER.info("TVCanvas: paired a phone for photos")
        return _json({"key": key, **(await _identity(request.app[KEY_HASS]))})

    @_home_only
    async def delete(self, request: web.Request) -> web.Response:
        found = self._phone(request)
        if found is None:
            return _unauthorized() if self._hub.pairing is not None else _not_set_up()
        hub, phone = found
        await hub.pairing.async_forget(phone)
        return web.Response(status=HTTPStatus.NO_CONTENT, headers=_NO_STORE)


class LanHaveView(_LanView):
    url = "/api/tvcanvas/lan/have"
    name = "api:tvcanvas:lan:have"

    @_home_only
    async def get(self, request: web.Request) -> web.Response:
        found = self._phone(request)
        if found is None:
            return _unauthorized() if self._hub.pairing is not None else _not_set_up()
        photos = found[0].photos
        return _json(
            {
                "sha256": photos.hashes,
                "bytes_used": photos.bytes_used,
                "bytes_cap": PHOTOS_BYTES_CAP,
                "files_cap": PHOTOS_FILES_CAP,
            }
        )


class LanImageView(_LanView):
    url = "/api/tvcanvas/lan/image/{sha256}"
    name = "api:tvcanvas:lan:image"

    @_home_only
    async def put(self, request: web.Request, sha256: str) -> web.Response:
        found = self._phone(request)
        if found is None:
            return _unauthorized() if self._hub.pairing is not None else _not_set_up()
        hub, phone = found
        if SHA256_HEX.fullmatch(sha256) is None:  # also the only thing a path is built from
            return _error(HTTPStatus.BAD_REQUEST, "bad_hash")
        if not hub.pairing.allow_put(phone):
            return _error(HTTPStatus.TOO_MANY_REQUESTS, "rate_limited", retry_after=60)
        photos = hub.photos
        if sha256 not in photos and not photos.would_fit(0):
            return _error(HTTPStatus.INSUFFICIENT_STORAGE, "full")  # before reading 10 MB
        # Each upload holds its body (up to 10 MB) until it is stored: a phone gets at most
        # LAN_UPLOADS_PER_PHONE at once, counted before its body is read (#16). One slow or
        # stalled sender only ever holds its own phone's slots, and only for so long.
        uploading = hub.uploading.get(phone.key_id, 0)
        if uploading >= LAN_UPLOADS_PER_PHONE:
            return _busy()
        hub.uploading[phone.key_id] = uploading + 1
        try:
            return await self._receive(hub, request, sha256, photos)
        finally:
            left = hub.uploading.get(phone.key_id, 1) - 1
            if left > 0:
                hub.uploading[phone.key_id] = left
            else:
                hub.uploading.pop(phone.key_id, None)

    async def _receive(
        self, hub: LanHub, request: web.Request, sha256: str, photos: PhotoStore
    ) -> web.Response:
        try:
            async with asyncio.timeout(LAN_BODY_READ_SECONDS):
                data = await _read_capped(request, LAN_IMAGE_BODY_MAX)
        except TimeoutError:
            return _error(HTTPStatus.REQUEST_TIMEOUT, "timeout")
        except _TooLarge:
            return _error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large")
        hass: HomeAssistant = request.app[KEY_HASS]
        digest = await hass.async_add_executor_job(_sha256_hex, data)  # 10 MB: off the loop
        if digest != sha256:
            return _error(HTTPStatus.UNPROCESSABLE_ENTITY, "hash_mismatch")
        if sha256 not in photos:
            # The whole body is here: only now does it queue for the one decode slot.
            if hub.decoding >= LAN_UPLOADS_AT_ONCE:
                return _busy()
            hub.decoding += 1
            try:
                await decode_gate(hass).run(hass, check_upload, data, wait=LAN_DECODE_WAIT_SECONDS)
            except DecodeBusy:
                return _busy()
            except ImageError:
                return _error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "unsupported_image")
            finally:
                hub.decoding -= 1
        try:
            new = await photos.async_put(sha256, data)
        except PhotosFull:
            return _error(HTTPStatus.INSUFFICIENT_STORAGE, "full")
        except OSError as err:
            _LOGGER.warning("TVCanvas: could not store a photo: %s", err.strerror or err)
            if err.errno in (errno.ENOSPC, errno.EDQUOT):
                return _error(HTTPStatus.INSUFFICIENT_STORAGE, "full")
            return _error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal")
        return _json({"sha256": sha256}, HTTPStatus.CREATED if new else HTTPStatus.OK)


_KEY_ID = re.compile(r"[0-9a-f]{16}")
_NONCE = re.compile(r"[A-Za-z0-9_-]{43}")  # 32 bytes, base64url, no padding


def local_address(sockname: Any) -> tuple[str, int] | None:
    """The (ip, port) of the local socket a request arrived on, normalised as the
    contract says: IPv6 compressed without a zone, IPv4-mapped IPv6 as IPv4."""
    try:
        host, port = sockname[0], sockname[1]
        ip = ip_address(str(host).split("%", 1)[0])
    except (TypeError, IndexError, ValueError):
        return None
    if isinstance(ip, IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not isinstance(port, int) or not 0 < port < 65536:
        return None
    return str(ip), port


class LanProveView(_LanView):
    """HA proves it holds a phone's key hash before the phone trusts a new address."""

    url = "/api/tvcanvas/lan/prove"
    name = "api:tvcanvas:lan:prove"

    @_home_only
    async def post(self, request: web.Request) -> web.Response:
        pairing = self._hub.pairing
        if pairing is None:
            return _not_set_up()
        if not pairing.allow_prove(request.remote or ""):
            return _error(HTTPStatus.TOO_MANY_REQUESTS, "rate_limited", retry_after=60)
        try:
            raw = await _read_capped(request, LAN_PROVE_BODY_MAX)
        except _TooLarge:
            return _error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large")
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return _error(HTTPStatus.BAD_REQUEST, "bad_request")
        wanted, nonce = body.get("key_id"), body.get("nonce")
        if (
            not isinstance(wanted, str)
            or not isinstance(nonce, str)
            or _KEY_ID.fullmatch(wanted) is None
            or _NONCE.fullmatch(nonce) is None
            or len(base64.urlsafe_b64decode(nonce + "=")) != 32
        ):
            return _error(HTTPStatus.BAD_REQUEST, "bad_request")
        phone = pairing.phone_by_key_id(wanted)
        if phone is None:
            return _error(HTTPStatus.FORBIDDEN, "unknown_key")
        # The socket's own address, never the Host header (relay protection).
        transport = request.transport
        local = local_address(transport.get_extra_info("sockname") if transport else None)
        if local is None:
            return _error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal")
        ip, port = local
        return _json({"proof": prove(phone.key_hash, nonce, ip, port), "ip": ip, "port": port})


def register_views(hass: HomeAssistant, hub: LanHub) -> None:
    for view in (LanInfoView, LanPairView, LanHaveView, LanImageView, LanProveView):
        hass.http.register_view(view(hub))
