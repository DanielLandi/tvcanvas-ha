"""Pair a phone for photos: a live code, traded once for a LAN key.

The live code is the 4-character setup code the phone generated and the user typed
into the config flow as the last 4 of the 12-character code (docs/lan-api.md §3);
there is no other way to open one. Keys are stored only as SHA-256 hashes;
codes and keys are never logged, and the setup code is never stored at all.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
import unicodedata
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import CROCKFORD, SETUP_SUFFIX_LEN, crockford_clean
from .const import (
    DOMAIN,
    LAN_CODE_TTL_SECONDS,
    LAN_DEVICE_NAME_MAX,
    LAN_LOCKOUT_SECONDS,
    LAN_MAX_PHONES,
    LAN_MAX_WRONG_CODES,
    LAN_PROVE_CONTEXT,
    LAN_PROVES_PER_MINUTE,
    LAN_PUTS_PER_MINUTE,
)

KEYS_STORE_VERSION = 1
DEFAULT_DEVICE_NAME = "Phone"
_PENDING_SETUP_CODE = "pending_lan_setup_code"


def keys_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, KEYS_STORE_VERSION, f"{DOMAIN}.{entry_id}.lan_keys", private=True)


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()


def key_id(key_hash: str) -> str:
    """The key's public handle: first 16 hex of SHA-256 of the ASCII `key_hash` hex."""
    return hashlib.sha256(key_hash.encode("ascii")).hexdigest()[:16]


def prove(key_hash: str, nonce: str, ip: str, port: int) -> str:
    """base64url-nopad(HMAC-SHA256(bytes.fromhex(key_hash), context + nonce:ip:port)).

    Shows the phone this HA holds its key's hash, without the phone sending the key
    (docs/lan-api.md §4 `prove`). *ip*/*port* are the local socket address the
    request arrived on, so a host relaying the request gets a proof for HA's
    address, not its own."""
    msg = f"{LAN_PROVE_CONTEXT}{nonce}:{ip}:{port}"
    mac = hmac.new(bytes.fromhex(key_hash), msg.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def _window_allows(window: deque[float], now: float, limit: int) -> bool:
    """A 60-second sliding window: record and allow, or refuse."""
    while window and window[0] <= now - 60:
        window.popleft()
    if len(window) >= limit:
        return False
    window.append(now)
    return True


def stash_setup_code(hass: HomeAssistant, code: str) -> None:
    """Hold the config flow's LAN setup code for async_setup_entry, in memory only.

    Keyed by the domain: the integration allows a single config entry, and the flow
    doesn't know the entry's id yet (first setup), or the entry is about to be
    reloaded (reauth). The 10 minutes count from now, the moment of the claim."""
    hass.data.setdefault(DOMAIN, {})[_PENDING_SETUP_CODE] = (code, time.monotonic())


def take_setup_code(hass: HomeAssistant) -> tuple[str, float] | None:
    """The stashed setup code and the seconds it has left, removing it; None if
    there is none or it has expired."""
    pending = hass.data.get(DOMAIN, {}).pop(_PENDING_SETUP_CODE, None)
    if pending is None:
        return None
    code, stashed_at = pending
    left = LAN_CODE_TTL_SECONDS - (time.monotonic() - stashed_at)
    return (code, left) if left > 0 else None


def _match_form(code: Any) -> bytes:
    """The bytes a sent code is compared as: Crockford-cleaned ASCII, or b"" (never
    matches) for anything else."""
    if not isinstance(code, str) or not code.isascii():
        return b""
    return crockford_clean(code).encode("ascii")


def clean_device_name(value: Any) -> str:
    """A display name only: printable, trimmed, ≤ 64 characters."""
    if not isinstance(value, str):
        return DEFAULT_DEVICE_NAME
    text = "".join(c for c in value if unicodedata.category(c)[0] != "C").strip()
    return text[:LAN_DEVICE_NAME_MAX].strip() or DEFAULT_DEVICE_NAME


class PairError(Exception):
    """Wrong or expired code."""


class Locked(Exception):
    """Too many wrong codes; retry after *retry_after* seconds."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("locked")
        self.retry_after = retry_after


@dataclass
class Phone:
    key_hash: str
    device_name: str
    paired_at: str
    key_id: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.key_id = key_id(self.key_hash)

    def public(self) -> dict[str, str]:
        """What HA may show: never the key or its hash."""
        return {"name": self.device_name, "paired_at": self.paired_at}


class LanPairing:
    """The live pairing code, the per-address lockouts, and the paired phones' keys.

    The live code is the config flow's 4-character setup code (open_setup_code);
    opening one replaces any earlier one.

    Guessing limits (docs/lan-api.md §3): 5 wrong codes against the live code kill
    it; 5 wrong codes from one address lock that address out for 5 minutes. With no
    live code nothing is counted. Opening a setup code lifts no lockout; only the
    5 minutes do."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._store = keys_store(hass, entry_id)
        self.phones: list[Phone] = []
        self._code: str | None = None
        self._code_expires = 0.0  # on self._clock (monotonic)
        self._wrong = 0  # against the live code
        self._wrong_by_ip: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}
        self._puts: dict[str, deque[float]] = {}
        self._proves: dict[str, deque[float]] = {}
        self._listeners: list[Callable[[], None]] = []
        self._clock = time.monotonic  # tests replace it

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        phones = []
        for raw in data.get("phones", []):
            try:
                phones.append(
                    Phone(str(raw["key_hash"]), str(raw["device_name"]), str(raw["paired_at"]))
                )
            except (KeyError, TypeError):
                continue
        self.phones = phones[-LAN_MAX_PHONES:]

    async def _save(self) -> None:
        await self._store.async_save(
            {
                "phones": [
                    {"key_hash": p.key_hash, "device_name": p.device_name, "paired_at": p.paired_at}
                    for p in self.phones
                ]
            }
        )
        for cb in list(self._listeners):
            cb()

    @callback
    def async_add_listener(self, cb: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    # --- the code ---------------------------------------------------------------

    @property
    def pairing_open(self) -> bool:
        return self._code is not None and self._clock() < self._code_expires

    def open_setup_code(self, code: str, ttl: float = LAN_CODE_TTL_SECONDS) -> None:
        """Make the config flow's 4-character setup code the live code for *ttl*
        seconds (at most 10 minutes); replaces any earlier code. It lifts no lockout:
        whoever typed the code into HA isn't known to be the one locked out."""
        code = crockford_clean(code)
        if len(code) != SETUP_SUFFIX_LEN or any(c not in CROCKFORD for c in code):
            raise ValueError("a setup code is 4 Crockford characters")
        self._code = code
        self._code_expires = self._clock() + min(ttl, LAN_CODE_TTL_SECONDS)
        self._wrong = 0  # counts guesses against the live code, which is a new one

    def _lockout_left(self, client: str, now: float) -> float:
        until = self._locked_until.get(client)
        if until is None:
            return 0
        if now >= until:
            del self._locked_until[client]
            return 0
        return until - now

    async def async_pair(self, code: Any, device_name: Any, client: str) -> str:
        """Trade the code for a new key (returned once, stored hashed).

        *client* is the caller's address, for the per-address lockout."""
        now = self._clock()
        if left := self._lockout_left(client, now):
            raise Locked(max(1, int(left + 0.999)))
        if not self.pairing_open:
            raise PairError  # nothing to guess: not counted
        live = self._code
        if not hmac.compare_digest(_match_form(code), live.encode("ascii")):
            self._wrong += 1
            if self._wrong >= LAN_MAX_WRONG_CODES:
                self._wrong, self._code = 0, None  # the code someone was guessing is dead
            wrong = self._wrong_by_ip.get(client, 0) + 1
            if wrong >= LAN_MAX_WRONG_CODES:
                self._wrong_by_ip.pop(client, None)
                self._locked_until[client] = now + LAN_LOCKOUT_SECONDS
            else:
                self._wrong_by_ip[client] = wrong
            raise PairError
        self._code, self._wrong = None, 0
        self._wrong_by_ip.pop(client, None)
        key = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
        self.phones.append(
            Phone(_hash_key(key), clean_device_name(device_name), dt_util.utcnow().isoformat())
        )
        del self.phones[:-LAN_MAX_PHONES]  # a 6th phone evicts the oldest
        await self._save()
        return key

    # --- keys -------------------------------------------------------------------

    def authenticate(self, key: str | None) -> Phone | None:
        """The phone *key* belongs to. Compares every stored hash, in constant time."""
        if not key:
            return None
        digest = _hash_key(key).encode()
        found: Phone | None = None
        for phone in self.phones:
            if hmac.compare_digest(digest, phone.key_hash.encode()):
                found = phone
        return found

    async def async_forget(self, phone: Phone) -> None:
        if phone in self.phones:
            self.phones.remove(phone)
            self._puts.pop(phone.key_hash, None)
            await self._save()

    def allow_put(self, phone: Phone) -> bool:
        """120 PUTs a minute per key (sliding window)."""
        window = self._puts.setdefault(phone.key_hash, deque())
        return _window_allows(window, self._clock(), LAN_PUTS_PER_MINUTE)

    def allow_prove(self, client: str) -> bool:
        """30 `prove` calls a minute per client address (sliding window)."""
        now = self._clock()
        for addr in [a for a, w in self._proves.items() if not w or w[-1] <= now - 60]:
            del self._proves[addr]  # forget idle addresses
        window = self._proves.setdefault(client, deque())
        return _window_allows(window, now, LAN_PROVES_PER_MINUTE)

    def phone_by_key_id(self, wanted: Any) -> Phone | None:
        """The phone whose key_id is *wanted*, compared in constant time against all."""
        if not isinstance(wanted, str):
            return None
        given = wanted.encode("utf-8", "surrogatepass")
        found: Phone | None = None
        for phone in self.phones:
            if hmac.compare_digest(given, phone.key_id.encode()):
                found = phone
        return found
