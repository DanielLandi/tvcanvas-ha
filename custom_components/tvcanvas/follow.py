"""Follow a TV to a new DHCP address: the rules both TV kinds share.

The spec names a TV by its address and only the phone updates it. When the router gives
the TV a new lease while the phone is away, the display looks for the TV (locate.py,
locate_cast.py) and keeps a "moved" record `{"from": <spec host>, "to": <address>}` in the
TV's stored state. The record:

- is used only while the spec still names the host it was made for (the app's newer
  host wins);
- is re-checked at the start of every tick, and dropped as soon as the TV checks out at
  the spec's host again or stops checking out at the followed address;
- is looked for at most every LOCATE_EVERY_SECONDS while the TV answers nowhere.

A candidate that only claims to be the TV gets a notification and nothing else.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, ClassVar

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant

from .const import LOCATE_EVERY_SECONDS
from .displays import DisplayError
from .locate import Check, Found

_LOGGER = logging.getLogger(__name__)

_UPDATE_IT = (
    "Open the TVCanvas app near the TV to update its address, or reserve the TV's "
    "address on your router."
)


class FollowsTV:
    """Mixin for a display whose TV may move to a new address (Frame and Cast)."""

    KIND: ClassVar[str]  # in logs: "Frame %s stopped answering ..."
    NOUN: ClassVar[str]  # in notifications: "Your Frame moved ..."
    MOVED_EXTRA: ClassVar[str] = ""  # appended to "... now sends its images there"

    _hass: HomeAssistant
    _tv_id: str
    _host: str
    _clock: Callable[[], float]
    _next_locate: float
    _notice: str

    def _init_follow(self) -> None:
        self._clock = time.monotonic
        self._next_locate = 0.0
        self._notice = f"tvcanvas_moved_{self._tv_id}"

    # What each display provides.

    def _follow_state(self) -> dict[str, Any]:
        """The TV's stored state that holds the "moved" record."""
        raise NotImplementedError

    def _can_follow(self) -> bool:
        """False when this TV is never looked for (no finder, or no identity in its id)."""
        raise NotImplementedError

    def _why_not_search(self) -> str | None:
        """Why the TV can't be looked for right now although it could be followed."""
        return None

    async def _find(self, skip: str) -> Found | None:
        raise NotImplementedError

    async def _check(self, address: str) -> Found:
        raise NotImplementedError

    # The shared rules.

    @property
    def address(self) -> str:
        """Where the TV is believed to be: the spec's host, or the address HA followed it
        to after a DHCP move. NOT checked: only for the runner's TCP probe (no data) and
        for logs. Everything that sends anything to the TV goes through
        `_checked_address()`. A spec with a different host (the phone sent it) wins."""
        return self._followed(self._follow_state()) or self._host

    def _followed(self, state: dict[str, Any]) -> str | None:
        if not self._can_follow():
            return None
        moved = state.get("moved")
        if isinstance(moved, dict) and moved.get("from") == self._host and moved.get("to"):
            return str(moved["to"])
        return None

    async def _follow(self) -> bool:
        """True when the TV was found, and checked, at another address (or at the spec's
        host again); the next connections go there.

        At most every LOCATE_EVERY_SECONDS (a TV in standby answers nowhere). An address
        counts only when it checks out (Check.OK); one that only claims to be the TV gets
        a notification and nothing else."""
        if not self._can_follow() or self._clock() < self._next_locate:
            return False
        self._next_locate = self._clock() + LOCATE_EVERY_SECONDS
        current = self.address
        if why := self._why_not_search():
            _LOGGER.warning(
                "%s %s stopped answering at %s; %s", self.KIND, self._tv_id, current, why
            )
            self._notify(
                f"Your {self.NOUN} stopped answering at {current}. If it moved to a new "
                f"address, Home Assistant can't look for it: {why}. {_UPDATE_IT}"
            )
            return False
        found = await self._find(current)
        if found is None or found.address == current:
            return False
        if found.check is not Check.OK:
            _LOGGER.warning(
                "%s %s stopped answering at %s; %s claims to be it but %s, so Home "
                "Assistant sends it nothing",
                self.KIND,
                self._tv_id,
                current,
                found.address,
                found.why,
            )
            self._notify(
                f"Your {self.NOUN} seems to have moved to {found.address}, but Home "
                f"Assistant could not confirm it is your TV ({found.why}), so it sends it "
                f"nothing. {_UPDATE_IT}"
            )
            return False
        state = self._follow_state()
        if found.address == self._host:
            self._drop_moved("it answers at its original address again")
            return True
        state["moved"] = {"from": self._host, "to": found.address}
        self._store_changed()
        _LOGGER.warning(
            "%s %s stopped answering at %s and was found at %s (new DHCP address); "
            "Home Assistant now sends its images there",
            self.KIND,
            self._tv_id,
            current,
            found.address,
        )
        self._notify(
            f"Your {self.NOUN} moved from {current} to {found.address} (a new address from "
            "your router). Home Assistant checked it is the same TV and now sends its "
            f"images there{self.MOVED_EXTRA}. {_UPDATE_IT}"
        )
        return True

    async def _checked_address(self, state: dict[str, Any] | None = None) -> str:
        """The only way to the address anything is sent to. A followed address is
        re-checked every time: dropped as soon as the TV answers at the spec's host
        again, or when the followed address no longer checks out (DisplayError). A record
        for another spec host (the app sent the TV's address) is dropped.

        *state*: the TV's state when the store no longer holds it (release)."""
        if state is None:
            state = self._follow_state()
        if state.get("moved") is None:
            return self._host
        address = self._followed(state)
        if address is None:
            self._drop_moved("the TVCanvas app sent its address", state)
            return self._host
        if (await self._check(self._host)).check is Check.OK:
            self._drop_moved("it answers at its original address again", state)
            return self._host
        if (await self._check(address)).check is not Check.OK:
            self._drop_moved(f"it no longer checks out at {address}", state)
            raise DisplayError(
                "TV unreachable: the TV no longer checks out at the address it was followed to"
            )
        return address

    def _drop_moved(self, why: str, state: dict[str, Any] | None = None) -> None:
        if state is None:
            state = self._follow_state()
        if state.pop("moved", None) is not None:
            self._store_changed()
            _LOGGER.info("%s %s: back to %s: %s", self.KIND, self._tv_id, self._host, why)
        persistent_notification.async_dismiss(self._hass, self._notice)

    def _notify(self, message: str) -> None:
        persistent_notification.async_create(
            self._hass, message, title="TVCanvas", notification_id=self._notice
        )

    def _store_changed(self) -> None:
        raise NotImplementedError
