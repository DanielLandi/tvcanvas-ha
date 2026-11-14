"""Find a Google Cast TV that moved to a new address on the home network.

A Cast TV's id is the phone's, not the TV's, and the cloud's spec has no field for
anything else, so HA learns the TV's identity itself: the first time it shows an image at the
spec's host it records the TV's Cast UUID (`ssdp_udn` in `/setup/eureka_info`, the same
UUID as the `id` in its `_googlecast._tcp` mDNS TXT record and the UDN it answers SSDP
with) and the MAC the HA host's neighbour table holds for that address. Trust on first
use, bound to that host: a later device at the same host with another identity never
replaces it (display_cast.py), and only the app sending a new host resets it.

When the TV stops answering, candidates come from HA's SSDP cache (DIAL, by UDN) and an
mDNS browse (by TXT `id`). Those are claims anyone on the LAN can make, so an address
counts as the TV only when the device there reports the recorded UUID in its own
eureka_info AND the HA host's neighbour table holds the recorded MAC for it (locate.py).
Without a recorded identity nothing is looked for.

Cast holds no credential HA could leak to an impostor; a device that claims both the MAC
and the UUID could receive the TV's images, as ARP spoofing already allows today.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from functools import partial
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CAST_INFO_BODY_MAX,
    CAST_INFO_HTTP_PORT,
    CAST_INFO_HTTPS_PORT,
    CAST_INFO_PATH,
    CAST_INFO_TIMEOUT_SECONDS,
    CAST_MDNS_TYPE,
    CAST_SSDP_TYPE,
)
from .locate import (
    Check,
    Found,
    confirm_neighbour,
    ipv4_addresses,
    mdns_services,
    neighbour_mac,
    normalize_mac,
    search,
    url_host,
)
from .urlsafety import home_address

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CastIdentity:
    """What makes an address the Cast TV: its Cast UUID (32 hex digits) and its MAC."""

    uuid: str
    mac: str

    def as_dict(self) -> dict[str, str]:
        return {"uuid": self.uuid, "mac": self.mac}

    @classmethod
    def from_dict(cls, value: Any) -> CastIdentity | None:
        if not isinstance(value, dict):
            return None
        found, mac = normalize_uuid(value.get("uuid")), normalize_mac(value.get("mac"))
        return cls(found, mac) if found and mac else None


def normalize_uuid(value: object) -> str | None:
    """A Cast UUID (dashed, bare or `uuid:`-prefixed) as 32 lower-case hex digits."""
    text = str(value or "").strip().lower().removeprefix("uuid:")
    try:
        return uuid.UUID(text).hex if text else None
    except ValueError:
        return None


async def _eureka(hass: HomeAssistant, url: str, verify_ssl: bool) -> Any:
    session = async_get_clientsession(hass, verify_ssl=verify_ssl)
    async with asyncio.timeout(CAST_INFO_TIMEOUT_SECONDS):
        async with session.get(url, allow_redirects=False) as resp:
            if resp.status != 200:
                return None
            body = await resp.content.read(CAST_INFO_BODY_MAX)
    try:
        return json.loads(body)
    except ValueError:
        return None


async def cast_uuid(hass: HomeAssistant, address: str) -> str | None:
    """The Cast UUID the device at *address* reports; None when it does not answer.

    HTTPS on 8443 first (Cast devices present a self-signed certificate, so it is not
    verified: the UUID is a claim either way), plain HTTP on 8008 for older devices. A
    timeout is not retried: the address does not answer."""
    home = home_address(address)
    if home is None:
        return None
    if ":" in home:
        home = f"[{home}]"
    status: Any = None
    for url, verify_ssl in (
        (f"https://{home}:{CAST_INFO_HTTPS_PORT}{CAST_INFO_PATH}", False),
        (f"http://{home}:{CAST_INFO_HTTP_PORT}{CAST_INFO_PATH}", True),
    ):
        try:
            status = await _eureka(hass, url, verify_ssl)
        except TimeoutError:
            return None
        except (aiohttp.ClientError, OSError):
            continue
        if status is not None:
            break
    if not isinstance(status, dict):
        return None
    info = status.get("device_info")
    udn = info.get("ssdp_udn") if isinstance(info, dict) else None
    return normalize_uuid(udn or status.get("ssdp_udn"))


async def learn(hass: HomeAssistant, address: str) -> CastIdentity | None:
    """The identity of the Cast TV that just showed an image at *address*; None when
    either half can't be read (then nothing is recorded, and the TV can't be followed).

    The HTTP request comes first: it makes the host resolve the address, so the
    neighbour entry is fresh when it is read."""
    found_uuid = await cast_uuid(hass, address)
    if found_uuid is None or ":" in address:
        return None
    try:
        mac = await neighbour_mac(hass, address)
    except OSError as err:
        _LOGGER.debug("TVCanvas: neighbour table unreadable: %s", err)
        return None
    return CastIdentity(found_uuid, mac) if mac else None


async def check(hass: HomeAssistant, address: str, identity: CastIdentity) -> Found:
    """Is *address* the Cast TV with *identity*? Its eureka_info must name the UUID and
    the HA host's neighbour table the MAC."""
    if await cast_uuid(hass, address) != identity.uuid:
        return Found(address, Check.NOT_IT)
    return await confirm_neighbour(hass, address, identity.mac)


async def ssdp_addresses(hass: HomeAssistant, wanted: str) -> list[str]:
    """Addresses HA's SSDP cache holds for the DIAL server with UDN *wanted* (no
    network traffic)."""
    from homeassistant.components import ssdp  # noqa: PLC0415 - loaded only when needed

    addresses: list[str] = []
    for info in await ssdp.async_get_discovery_info_by_st(hass, CAST_SSDP_TYPE):
        udn = (
            getattr(info, "ssdp_udn", None)
            or str(getattr(info, "ssdp_usn", None) or "").partition("::")[0]
        )
        if normalize_uuid(udn) != wanted:
            continue
        if host := url_host(getattr(info, "ssdp_location", None)):
            addresses.append(host)
    return list(dict.fromkeys(addresses))


async def mdns_addresses(hass: HomeAssistant, wanted: str) -> list[str]:
    """IPv4 addresses of the `_googlecast._tcp` services whose TXT `id` is *wanted*."""
    addresses: list[str] = []
    for info in await mdns_services(hass, CAST_MDNS_TYPE):
        found_id = (info.properties or {}).get(b"id")
        if (
            isinstance(found_id, bytes)
            and normalize_uuid(found_id.decode(errors="replace")) == wanted
        ):
            addresses.extend(ipv4_addresses(info))
    return list(dict.fromkeys(addresses))


async def find_cast(
    hass: HomeAssistant, identity: CastIdentity, skip: str | None = None
) -> Found | None:
    """Where the Cast TV with *identity* answers now (locate.search): HA's SSDP cache
    first (free), then an mDNS browse."""

    async def ssdp(h: HomeAssistant) -> list[str]:
        return await ssdp_addresses(h, identity.uuid)

    async def mdns(h: HomeAssistant) -> list[str]:
        return await mdns_addresses(h, identity.uuid)

    return await search(hass, (ssdp, mdns), partial(check, hass, identity=identity), skip)


class CastFinder:
    """What a CastDisplay uses to learn, find and re-check a Cast TV (tests fake it)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def learn(self, address: str) -> CastIdentity | None:
        return await learn(self._hass, address)

    async def find(self, identity: CastIdentity, skip: str | None) -> Found | None:
        return await find_cast(self._hass, identity, skip)

    async def check(self, address: str, identity: CastIdentity) -> Found:
        return await check(self._hass, address, identity)
