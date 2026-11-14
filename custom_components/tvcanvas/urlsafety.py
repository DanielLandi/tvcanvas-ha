"""Keep direct image fetches off the local network, TVs on it, and URL secrets out of
sight (#7, #14).

HA fetches webcam, custom-URL and non-proxied collection images itself, from inside the
user's network. `models.public_https_url` refuses literal non-public hosts when a spec is
parsed; this module covers host names, at fetch time, and redacts URLs before they reach
logs, state attributes or diagnostics.

TVs are the other way round: the cloud names each TV's address, and a TV is sent the
phone's photos, so a TV must be on the home network (`home_address`,
`resolve_home_address`).
"""

from __future__ import annotations

import errno
import ipaddress
import re
import socket
from typing import Final

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import ThreadedResolver
from homeassistant.helpers.aiohttp_client import SERVER_SOFTWARE
from homeassistant.util import ssl as ssl_util

_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def is_public_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """False for private, loopback, link-local, shared (CGNAT, Tailscale), reserved and
    multicast addresses, and for IPv6 addresses that carry one of those IPv4 addresses
    (IPv4-mapped, 6to4, NAT64)."""
    candidates = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            candidates.append(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            candidates.append(ip.sixtofour)
        if ip in _NAT64:
            candidates.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    # is_global also excludes shared space (100.64.0.0/10: CGNAT, Tailscale).
    return all(a.is_global and not a.is_multicast for a in candidates)


class NonPublicAddress(OSError):
    """A host name resolved to an address HA must not fetch images from."""

    def __init__(self, message: str) -> None:
        # With an errno, aiohttp's ClientConnectorDNSError shows the message (strerror).
        super().__init__(errno.EACCES, message)


class PublicOnlyResolver(AbstractResolver):
    """Resolve with the event loop's getaddrinfo (run in its executor, so the loop never
    blocks) and refuse the host if ANY answer is not a public address.

    As the image session's resolver this is also the DNS-rebinding guard: aiohttp connects
    only to the addresses its resolver returns, so the addresses checked are the addresses
    connected to. TLS still sends and verifies the host name (SNI, certificate)."""

    def __init__(self) -> None:
        self._inner = ThreadedResolver()

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        answers = await self._inner.resolve(host, port, family)
        if not answers:
            raise NonPublicAddress(f"{host} did not resolve")
        for answer in answers:
            try:
                public = is_public_address(ipaddress.ip_address(answer["host"]))
            except ValueError:
                public = False
            if not public:
                raise NonPublicAddress(f"{host} resolves to a non-public address")
        return answers

    async def close(self) -> None:
        await self._inner.close()


async def check_resolves_public(host: str, port: int | None) -> None:
    """Raise OSError unless every address *host* resolves to is public.

    IP literals pass through: `models.public_https_url` already checked them."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        resolver = PublicOnlyResolver()
        try:
            await resolver.resolve(host, port or 443, socket.AF_UNSPEC)
        finally:
            await resolver.close()


def create_image_session() -> aiohttp.ClientSession:
    """A session for direct image fetches: it only ever connects to public addresses.

    Not HA's shared session, whose connector (and resolver) can't be replaced. The owner
    closes it."""
    connector = aiohttp.TCPConnector(
        resolver=PublicOnlyResolver(),
        ssl=ssl_util.client_context(alpn_protocols=ssl_util.SSL_ALPN_HTTP11),  # as HA's
    )
    return aiohttp.ClientSession(connector=connector, headers={"User-Agent": SERVER_SOFTWARE})


TV_HOST_REFUSED: Final = "tv host is not on the home network"

# What counts as the home network for a TV: an allow-list, so loopback, shared space
# (CGNAT, Tailscale), reserved ranges and every public address are refused.
_HOME_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "10.0.0.0/8",  # RFC 1918 private
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",  # IPv4 link-local
        "fc00::/7",  # IPv6 unique local
        "fe80::/10",  # IPv6 link-local
    )
)


class NotHomeNetwork(Exception):
    """A TV's host is not on the home network: HA must not connect to it."""


def home_address(value: object) -> str | None:
    """*value* as the address to connect to, when it is an IP literal on the home network;
    otherwise None (a public or other address, or not an IP literal at all).

    An IPv4-mapped IPv6 address is judged, and returned, as the IPv4 address it carries."""
    try:
        ip = ipaddress.ip_address(value if isinstance(value, str) else "")
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if is_public_address(ip) or not any(ip in net for net in _HOME_NETWORKS):
        return None
    return str(ip)


async def resolve_home_address(host: str) -> str:
    """The address to connect to for the TV at *host*; NotHomeNetwork if HA must not.

    An IP literal is checked as it is. A host name is resolved here, where it is used, and
    refused if ANY answer is outside the home network (or there is none). The caller
    connects to the address returned, never to the name, so the address checked is the
    address connected to (DNS rebinding)."""
    address = home_address(host)
    if address is not None:
        return address
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise NotHomeNetwork(TV_HOST_REFUSED)
    resolver = ThreadedResolver()
    try:
        answers = await resolver.resolve(host, 0, socket.AF_UNSPEC)
    except OSError as err:
        raise NotHomeNetwork(TV_HOST_REFUSED) from err
    finally:
        await resolver.close()
    addresses = [home_address(answer["host"]) for answer in answers]
    if not addresses or None in addresses:
        raise NotHomeNetwork(TV_HOST_REFUSED)
    return addresses[0]


# scheme://, then the authority (user-info is everything up to its last "@"), the path,
# and an optional ?query and #fragment, ending at whitespace, quotes or angle brackets.
_URL = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/?#\s'\"<>]*)([^?#\s'\"<>]*)([?#][^\s'\"<>]*)?")


def _redact_match(m: re.Match[str]) -> str:
    authority = m.group(2).rpartition("@")[2]
    return f"{m.group(1)}{authority}{m.group(3)}"


def redact(text: object) -> str:
    """Text safe for logs, state attributes and diagnostics: every URL in it loses its
    user-info (user:password@), query string and fragment, where webcam credentials,
    tokens, keys and signatures live. Requests still use the real URL."""
    return _URL.sub(_redact_match, str(text))
