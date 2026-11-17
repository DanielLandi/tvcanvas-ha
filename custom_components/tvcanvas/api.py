"""TVCanvas Cloud client (contract: docs/cloud-contract.md)."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import aiohttp

from .const import MAX_IMAGE_BYTES, MAX_JSON_BYTES, STATUS_ERROR_MAX, TV_HEIGHT, TV_WIDTH

_LOGGER = logging.getLogger(__name__)

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_TIMEOUT = aiohttp.ClientTimeout(total=30)


class CloudError(Exception):
    """The cloud could not be reached or answered unexpectedly."""


class CloudUnreachable(CloudError):
    """No HTTP answer at all (connection failed or timed out)."""


class AuthError(CloudError):
    """Token rejected (401) or missing scope (403)."""


class OutOfCredits(CloudError):
    """402 from a charged call."""


class RateLimited(CloudError):
    """429."""


class InvalidCode(CloudError):
    """Pairing code wrong, expired or already used."""


class WrongServer(CloudError):
    """The address answered, but not the way TVCanvas Cloud's /ha/claim does: 401, 403,
    404 or 405, a redirect, or a 2xx that isn't a token. Usually a wrong server URL, or
    a TVCanvas server without the Home Assistant endpoints deployed."""


class CloudServerError(CloudError):
    """5xx: the server is there but failing."""


@dataclass(frozen=True)
class TokenInfo:
    token: str
    expires_at: datetime


@dataclass(frozen=True)
class SyncResult:
    specs: list[dict[str, Any]] | None
    etag: str | None


# /ha/claim needs no auth and exists on every TVCanvas Cloud with HA support, so these
# mean the URL points elsewhere (or at a server without the HA endpoints, which answers
# 401 "Invalid or missing API key").
_NOT_A_CLAIM_ENDPOINT = frozenset({401, 403, 404, 405})

CLOUD_CODE_LEN = 8
SETUP_SUFFIX_LEN = 4


def crockford_clean(code: str) -> str:
    """Uppercase, `-` and spaces removed, `O`→`0`, `I`/`L`→`1`. No validation."""
    cleaned = code.upper().replace("-", "").replace(" ", "")
    return cleaned.replace("O", "0").replace("I", "1").replace("L", "1")


def normalize_code(code: str) -> str | None:
    """The code as typed, cleaned: exactly 8 or 12 Crockford characters, else None.

    8 is the cloud pairing code; 12 is that code plus the phone's 4-character LAN
    setup code (docs/lan-api.md §3)."""
    cleaned = crockford_clean(code)
    if len(cleaned) not in (CLOUD_CODE_LEN, CLOUD_CODE_LEN + SETUP_SUFFIX_LEN):
        return None
    if any(c not in CROCKFORD for c in cleaned):
        return None
    return cleaned


def split_code(code: str) -> tuple[str, str | None] | None:
    """(cloud code, LAN setup suffix or None), or None for a malformed code.

    Only the 8-character cloud code may ever be sent to TVCanvas Cloud; the suffix
    stays in the home."""
    cleaned = normalize_code(code)
    if cleaned is None:
        return None
    return cleaned[:CLOUD_CODE_LEN], cleaned[CLOUD_CODE_LEN:] or None


def _raise_for(status: int) -> None:
    if status in (401, 403):
        raise AuthError(f"token rejected ({status})")
    if status == 402:
        raise OutOfCredits("out of credits")
    if status == 429:
        raise RateLimited("rate limited")
    if status >= 300:
        raise CloudError(f"cloud answered {status}")


# Cloud URLs that answered 422 to a status report carrying `showing`: HA leaves `showing`
# out for them until it restarts. (A cloud without `showing` ignores the key; this is a
# safety net.)
_NO_SHOWING: set[str] = set()
SHOWING_KINDS = frozenset({"board", "photo", "collection", "webcam", "custom_url"})
SHOWING_NAME_MAX = 64
_SHOWING_KEYS = frozenset({"name", "kind", "index", "count", "since", "paused"})
_SINCE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|\+00:00)")
_SINCE_AHEAD_MAX = timedelta(minutes=5)


def valid_showing(showing: Any) -> dict[str, Any] | None:
    """*showing* when it meets every rule of the cloud contract, else None (sent as
    `null`). The cloud answers 422 to the whole report for any broken rule, so a report
    is never risked on it: every key required and no other, `name` 1–64 printable
    characters after trimming and no `://`, `kind` one of SHOWING_KINDS, `index` and
    `count` ints with 0 <= index < count, `paused` a bool, `since` RFC 3339 UTC and not
    more than 5 minutes ahead."""
    if not isinstance(showing, dict) or set(showing) != _SHOWING_KEYS:
        return None
    name, index, count = showing["name"], showing["index"], showing["count"]
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not 1 <= len(name) <= SHOWING_NAME_MAX or not name.isprintable() or "://" in name:
        return None
    if showing["kind"] not in SHOWING_KINDS or not isinstance(showing["paused"], bool):
        return None
    if any(type(v) is not int for v in (index, count)) or not 0 <= index < count:
        return None
    since = showing["since"]
    if not isinstance(since, str) or not _SINCE.fullmatch(since):
        return None
    try:
        at = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        return None
    if at - datetime.now(UTC) > _SINCE_AHEAD_MAX:
        return None
    return {**showing, "name": name}


class CloudClient:
    def __init__(
        self, session: aiohttp.ClientSession, base_url: str, token: str | None = None
    ) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        self.token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def _request(
        self, method: str, path: str, cap: int | None = None, **kwargs: Any
    ) -> _Reply:
        """One call; a reply body over *cap* bytes (default: the JSON cap) is refused,
        never held whole (#17)."""
        cap = MAX_JSON_BYTES if cap is None else cap
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        try:
            async with self._session.request(
                method,
                f"{self._base}{path}",
                headers=headers,
                timeout=_TIMEOUT,
                allow_redirects=False,
                **kwargs,
            ) as resp:
                return _Reply(resp.status, resp.headers, await _read_capped(resp, cap))
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CloudUnreachable(f"cloud unreachable: {err}") from err

    async def claim(self, code: str) -> TokenInfo:
        """Trade the 8-character code for a token. Each outcome has its own exception,
        so the config flow can say what went wrong (docs/cloud-contract.md)."""
        if len(code) != CLOUD_CODE_LEN:  # never the LAN setup suffix (docs/lan-api.md §3)
            raise InvalidCode("invalid code")
        reply = await self._request("POST", "/ha/claim", json={"code": code})
        status = reply.status
        if status == 400:
            raise InvalidCode("invalid code")
        if status == 429:
            raise RateLimited("rate limited")
        if status >= 500:
            raise CloudServerError(f"server error ({status})")
        if status in _NOT_A_CLAIM_ENDPOINT or 300 <= status < 400:
            raise WrongServer(f"no Home Assistant pairing at this address ({status})")
        if not 200 <= status < 300:
            raise CloudError(f"cloud answered {status}")
        try:
            return _token_info(reply.json())
        except CloudError as err:
            raise WrongServer(f"the answer is not a token ({err})") from err

    async def agent_sync(self, etag: str | None) -> SyncResult:
        headers = {"If-None-Match": etag} if etag else {}
        reply = await self._request("GET", "/ha/agent/sync", headers=headers)
        if reply.status == 304:
            return SyncResult(specs=None, etag=etag)
        _raise_for(reply.status)
        specs = reply.json().get("specs", [])
        if not isinstance(specs, list):
            raise CloudError("specs is not a list")
        return SyncResult(specs=specs, etag=reply.headers.get("ETag"))

    async def agent_status(
        self,
        tv_id: str,
        ok: bool,
        error: str | None,
        at: datetime,
        showing: dict[str, Any] | None = None,
    ) -> None:
        """Report one TV's tick. *showing* is what the TV shows, for the phone. Each report
        replaces the cloud's copy, so it goes with every report: `null` when HA doesn't
        know, or when it breaks a rule (valid_showing). A cloud that answers 422 to a
        report with `showing` gets it again without, and none of it until HA restarts."""
        body: dict[str, Any] = {
            "tv_id": tv_id,
            "ok": ok,
            "error": error[:STATUS_ERROR_MAX] if error else None,
            "at": at.isoformat(),
        }
        with_showing = self._base not in _NO_SHOWING
        if with_showing:
            body["showing"] = valid_showing(showing)
        reply = await self._request("POST", "/ha/agent/status", json=body)
        if reply.status == 422 and with_showing:
            _NO_SHOWING.add(self._base)
            _LOGGER.info(
                "TVCanvas: this TVCanvas Cloud doesn't take `showing` in status reports; "
                "leaving it out until Home Assistant restarts"
            )
            body = {k: v for k, v in body.items() if k != "showing"}
            reply = await self._request("POST", "/ha/agent/status", json=body)
        if reply.status == 404:
            return
        _raise_for(reply.status)

    async def agent_refresh(self) -> TokenInfo:
        reply = await self._request("POST", "/ha/agent/refresh")
        _raise_for(reply.status)
        return _token_info(reply.json())

    async def board(self, type_id: str, params: dict[str, str]) -> bytes:
        reply = await self._request(
            "GET", f"/board/{quote(type_id, safe='')}", MAX_IMAGE_BYTES, params=params
        )
        _raise_for(reply.status)
        return reply.image()

    async def proxied_image(self, image_id: str) -> bytes:
        reply = await self._request(
            "GET",
            f"/collections/proxy/{quote(image_id, safe=':')}",
            MAX_IMAGE_BYTES,
            params={"width": str(TV_WIDTH), "height": str(TV_HEIGHT)},
        )
        _raise_for(reply.status)
        return reply.image()


class ReplyTooLarge(CloudError):
    """The cloud answered with more than HA reads (a broken or hacked cloud)."""


async def _read_capped(resp: aiohttp.ClientResponse, cap: int) -> bytes:
    """The whole body, refusing one over *cap* bytes without reading it all: 25 MB for an
    image, as for direct fetches, and 4 MB for JSON (the cloud's largest sync is 640 KB)."""
    try:
        declared = int(resp.headers.get("Content-Length", 0))
    except ValueError:
        declared = 0
    if declared > cap:
        raise ReplyTooLarge(f"cloud reply too large ({declared} bytes)")
    buf = bytearray()
    async for chunk in resp.content.iter_chunked(65536):
        buf += chunk
        if len(buf) > cap:
            raise ReplyTooLarge(f"cloud reply too large (over {cap} bytes)")
    return bytes(buf)


@dataclass(frozen=True)
class _Reply:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> dict[str, Any]:
        try:
            data = json.loads(self.body)
        except ValueError as err:
            raise CloudError("cloud answered with invalid JSON") from err
        if not isinstance(data, dict):
            raise CloudError("cloud answered with unexpected JSON")
        return data

    def image(self) -> bytes:
        if not self.headers.get("Content-Type", "").startswith("image/"):
            raise CloudError("cloud did not answer with an image")
        return self.body


def _token_info(data: dict[str, Any]) -> TokenInfo:
    try:
        token = data["token"]
        expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, AttributeError, ValueError) as err:
        raise CloudError("malformed token response") from err
    if not isinstance(token, str):
        raise CloudError("malformed token response")
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return TokenInfo(token=token, expires_at=expires_at)
