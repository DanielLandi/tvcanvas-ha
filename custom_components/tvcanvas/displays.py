"""What a display is."""

from __future__ import annotations

from typing import Protocol

from .urlsafety import TV_HOST_REFUSED, NotHomeNetwork, home_address, resolve_home_address


class DisplayError(Exception):
    """The TV did not show the image."""


class HostRefused(DisplayError):
    """HA will not connect to this address: it is not on the home network (#14).

    Photos go phone → HA → TV inside the home only, and the cloud names the TV's address,
    so an address anywhere else is never paired with, connected to, probed or sent
    anything."""

    def __init__(self, message: str = TV_HOST_REFUSED) -> None:
        super().__init__(message)


async def tv_address(host: str) -> str:
    """The address to connect to for the TV at *host* (a host name is resolved, checked
    and pinned to one address); HostRefused unless it is on the home network.

    Every display awaits this before it does anything for a TV."""
    try:
        return await resolve_home_address(host)
    except NotHomeNetwork as err:
        raise HostRefused from err


def home_literal(address: object) -> str:
    """*address* if it is an IP literal on the home network, else HostRefused.

    The last check before a socket is opened, in the thread that opens it: what the TV
    libraries are given is always an address, never a name they would resolve again."""
    home = home_address(address)
    if home is None:
        raise HostRefused
    return home


# What ready() returns for a TV in standby (a Frame after a long press on power: screen
# dark, still on the network). The runner renders and uploads nothing, reports no error,
# and asks again every FRAME_STANDBY_POLL_SECONDS, so the TV gets a fresh image as soon
# as it is back on.
STANDBY = "standby"


class Display(Protocol):
    async def show(self, jpeg: bytes) -> None: ...


# Optional, checked with getattr by the runner:
#   async def ready(self) -> str | None
#       Called before the image is fetched. None runs the tick; a string (the
#       status category, e.g. "waiting: Watch TV") skips it. Raise DisplayError
#       when the TV can't be reached. STANDBY: the TV sleeps (see above).
#   power_state: str | None
#       What the TV last said about its power ("on", "standby"); None when unknown.
#   async def release(self) -> None
#       The spec was removed: delete what HA left on the TV. Never raises.
