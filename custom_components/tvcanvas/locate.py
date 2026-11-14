"""Find a TV that moved to a new address on the home network.

The shared pieces (the HA host's neighbour table, what a candidate proves, the search
over discovered addresses) serve both TV kinds; the rest of this module finds a Samsung
Frame, and locate_cast.py a Google Cast TV.

The cloud spec names a TV by its address, which only the phone updates. When the router
hands the TV a new DHCP lease while the phone is not around, the spec's address goes
dead and the TV stops updating. A Frame's id carries its MAC (`samsung:<mac>`), every
Frame answers SSDP (HA's own scanner caches it) and announces itself over mDNS
(`_samsungmsf._tcp`), and it reports its MAC at `http://<address>:8001/api/v2/`: that is
enough to find it again without the phone.

Only addresses on the home network are ever asked (#14). What a device says over HTTP is
only a claim, so an address counts as the TV only when the MAC it reports in `/api/v2/`
AND the MAC the HA host's own neighbour (ARP) table holds for that address both match
the MAC in the TV's id (`check`). The ARP entry is layer-2 evidence an HTTP answer can't
forge; a device that changes its own MAC to the TV's still could, which is why the token
HA saved for the TV is never sent to a followed address (display_frame.py).

The Frame's wss:8002 certificate can't help: every Samsung TV presents the same
self-signed `CN=SmartViewSDK` certificate (2016-2036), so pinning it proves nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    FRAME_INFO_BODY_MAX,
    FRAME_INFO_PORT,
    FRAME_INFO_TIMEOUT_SECONDS,
    FRAME_MDNS_TYPE,
    FRAME_POWER_TIMEOUT_SECONDS,
    FRAME_SSDP_TYPES,
    LOCATE_MAX_CANDIDATES,
    MDNS_BROWSE_SECONDS,
)
from .urlsafety import home_address

_LOGGER = logging.getLogger(__name__)

NEIGHBOUR_TABLE = "/proc/net/arp"  # HA OS runs core with host networking: the host's table

_MAC = re.compile(r"^[0-9a-f]{2}([:-]?)[0-9a-f]{2}(\1[0-9a-f]{2}){4}$")


def normalize_mac(value: object) -> str | None:
    """`aa:bb:cc:dd:ee:ff` (any case, `:`/`-`/no separator) as 12 lower-case hex digits."""
    text = str(value or "").strip().lower()
    if not _MAC.match(text):
        return None
    return re.sub(r"[:-]", "", text)


def mac_from_tv_id(tv_id: str) -> str | None:
    """The MAC in a Frame id (`samsung:<mac>`); None when the id carries an IP instead."""
    _, sep, key = tv_id.partition(":")
    return normalize_mac(key) if sep else None


def url_host(url: object) -> str | None:
    try:
        return urlsplit(url).hostname if isinstance(url, str) else None
    except ValueError:
        return None


async def ssdp_addresses(hass: HomeAssistant) -> list[str]:
    """Addresses of the TVs and media renderers in HA's SSDP cache (no network traffic:
    HA's scanner searches every 10 minutes and listens for announcements)."""
    from homeassistant.components import ssdp  # noqa: PLC0415 - loaded only when needed

    addresses: list[str] = []
    for st in FRAME_SSDP_TYPES:
        for info in await ssdp.async_get_discovery_info_by_st(hass, st):
            if host := url_host(getattr(info, "ssdp_location", None)):
                addresses.append(host)
    return list(dict.fromkeys(addresses))


async def mdns_services(hass: HomeAssistant, service_type: str) -> list[Any]:
    """Every *service_type* instance announced over mDNS right now, resolved
    (zeroconf AsyncServiceInfo: addresses and TXT properties)."""
    from homeassistant.components import zeroconf  # noqa: PLC0415 - loaded only when needed
    from zeroconf import ServiceStateChange  # noqa: PLC0415
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo  # noqa: PLC0415

    aiozc = await zeroconf.async_get_async_instance(hass)
    names: set[str] = set()

    def on_change(
        zeroconf: Any, service_type: str, name: str, state_change: ServiceStateChange
    ) -> None:
        if state_change is not ServiceStateChange.Removed:
            names.add(name)

    browser = AsyncServiceBrowser(aiozc.zeroconf, [service_type], handlers=[on_change])
    try:
        await asyncio.sleep(MDNS_BROWSE_SECONDS)
    finally:
        await browser.async_cancel()
    found: list[Any] = []
    for name in sorted(names):
        info = AsyncServiceInfo(service_type, name)
        if await info.async_request(aiozc.zeroconf, 3000):
            found.append(info)
    return found


def ipv4_addresses(info: Any) -> list[str]:
    from zeroconf import IPVersion  # noqa: PLC0415 - loaded only when needed

    return list(info.parsed_addresses(IPVersion.V4Only))


async def mdns_addresses(hass: HomeAssistant) -> list[str]:
    """Every IPv4 address a Samsung TV announces over mDNS right now.

    Frames answer slowly and name their host `uuid:<…>.local.`, so the address in the
    `se` TXT record (`http://<address>:8001/api/v2/`) counts too."""
    addresses: list[str] = []
    for info in await mdns_services(hass, FRAME_MDNS_TYPE):
        addresses.extend(ipv4_addresses(info))
        se = (info.properties or {}).get(b"se")
        if host := url_host(se.decode(errors="replace") if isinstance(se, bytes) else None):
            addresses.append(host)
    return list(dict.fromkeys(addresses))


async def device_info(
    hass: HomeAssistant, address: str, wait_seconds: float = FRAME_INFO_TIMEOUT_SECONDS
) -> dict[str, Any] | None:
    """The `device` object the Samsung TV at *address* reports at `/api/v2/`; None when
    it does not answer (or *address* is not on the home network: never asked)."""
    home = home_address(address)
    if home is None:
        return None
    if ":" in home:
        home = f"[{home}]"
    session = async_get_clientsession(hass)
    try:
        async with asyncio.timeout(wait_seconds):
            async with session.get(
                f"http://{home}:{FRAME_INFO_PORT}/api/v2/", allow_redirects=False
            ) as resp:
                if resp.status != 200:
                    return None
                body = await resp.content.read(FRAME_INFO_BODY_MAX)
    except (aiohttp.ClientError, TimeoutError, OSError):
        return None
    try:
        device = json.loads(body).get("device") or {}
    except (ValueError, AttributeError):
        return None
    return device if isinstance(device, dict) else None


async def reported_mac(hass: HomeAssistant, address: str) -> str | None:
    """The MAC the Samsung TV at *address* reports; None when it does not answer."""
    device = await device_info(hass, address)
    return normalize_mac(device.get("wifiMac")) if device is not None else None


async def power_state(hass: HomeAssistant, address: str) -> str | None:
    """The Frame's `PowerState` at *address* (`"on"` in Art Mode or with the TV on,
    `"standby"` after a long press on power: screen dark, still on the network), lower
    case; None when it does not answer or its firmware does not report one."""
    device = await device_info(hass, address, FRAME_POWER_TIMEOUT_SECONDS)
    value = device.get("PowerState") if device is not None else None
    return (value.strip().lower() or None) if isinstance(value, str) else None


class Check(Enum):
    """What an address proves about being the TV with a given MAC."""

    OK = "ok"  # /api/v2/ and the neighbour table both name the MAC
    NOT_IT = "not it"  # no answer, or /api/v2/ names another MAC
    CLAIM_ONLY = "claim only"  # /api/v2/ names the MAC, the neighbour table does not


@dataclass(frozen=True)
class Found:
    address: str
    check: Check
    why: str | None = None  # for CLAIM_ONLY: why the claim could not be confirmed


def read_neighbours(path: str = NEIGHBOUR_TABLE) -> dict[str, str]:
    """IPv4 address -> MAC for every complete entry of the host's neighbour table.

    Raises OSError when the table can't be read (not Linux, or no host networking)."""
    table: dict[str, str] = {}
    with open(path, encoding="ascii", errors="replace") as file:
        next(file, None)  # header
        for line in file:
            fields = line.split()
            if len(fields) < 4 or fields[2] == "0x0":  # 0x0: incomplete
                continue
            mac = normalize_mac(fields[3])
            if mac and mac != "000000000000":
                table[fields[0]] = mac
    return table


async def neighbour_mac(hass: HomeAssistant, address: str) -> str | None:
    """The MAC the HA host's neighbour table holds for *address*; OSError if unreadable."""
    return (await hass.async_add_executor_job(read_neighbours)).get(address)


async def confirm_neighbour(hass: HomeAssistant, address: str, mac: str) -> Found:
    """*address* claimed to be the TV with *mac*: OK only when the HA host's neighbour
    table holds that MAC for it. Call it right after a request to *address*, which makes
    the host resolve the address, so the entry is fresh when it is read."""
    if ":" in address:
        return Found(address, Check.CLAIM_ONLY, "IPv6 addresses can't be checked")
    try:
        seen = await neighbour_mac(hass, address)
    except OSError as err:
        _LOGGER.debug("TVCanvas: neighbour table unreadable: %s", err)
        return Found(address, Check.CLAIM_ONLY, "the network's address table can't be read")
    if seen is None:
        return Found(address, Check.CLAIM_ONLY, "the address is not in the network's table")
    if seen != mac:
        return Found(address, Check.CLAIM_ONLY, "another device answers at that address")
    return Found(address, Check.OK)


async def check(hass: HomeAssistant, address: str, mac: str) -> Found:
    """Is *address* the Frame with *mac*? The HTTP request comes first: it is also what
    makes the host resolve the address, so the neighbour entry is fresh when it is read."""
    if await reported_mac(hass, address) != mac:
        return Found(address, Check.NOT_IT)
    return await confirm_neighbour(hass, address, mac)


Source = Callable[[HomeAssistant], Awaitable[list[str]]]


async def search(
    hass: HomeAssistant,
    sources: Iterable[Source],
    check_address: Callable[[str], Awaitable[Found]],
    skip: str | None,
) -> Found | None:
    """The first discovered address that checks out (Check.OK), else the first that only
    claims to be the TV (CLAIM_ONLY), else None.

    *sources* are asked in order; only home-network addresses are checked, at most
    LOCATE_MAX_CANDIDATES, and never *skip* (the address that just failed)."""
    asked: set[str] = set() if skip is None else {skip}
    claim: Found | None = None
    for source in sources:
        try:
            candidates = await source(hass)
        except Exception as err:  # noqa: BLE001 - discovery unavailable: try the next one
            _LOGGER.debug("TVCanvas: %s failed: %s", getattr(source, "__name__", source), err)
            continue
        for address in candidates:
            home = home_address(address)
            if home is None or home in asked:
                continue
            if len(asked) >= LOCATE_MAX_CANDIDATES:
                return claim
            asked.add(home)
            found = await check_address(home)
            if found.check is Check.OK:
                return found
            if found.check is Check.CLAIM_ONLY and claim is None:
                claim = found
    return claim


async def find_frame(hass: HomeAssistant, mac: str, skip: str | None = None) -> Found | None:
    """Where the Samsung TV with *mac* answers now (see `search`). HA's SSDP cache is
    asked first (free), then the TVs announcing themselves over mDNS."""
    return await search(
        hass, (ssdp_addresses, mdns_addresses), lambda address: check(hass, address, mac), skip
    )


class FrameFinder:
    """What a FrameDisplay asks over the network: where a moved Frame went, whether an
    address is it, and its power state (tests fake it)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def find(self, mac: str, skip: str | None) -> Found | None:
        return await find_frame(self._hass, mac, skip)

    async def check(self, address: str, mac: str) -> Found:
        return await check(self._hass, address, mac)

    async def power_state(self, address: str) -> str | None:
        return await power_state(self._hass, address)
