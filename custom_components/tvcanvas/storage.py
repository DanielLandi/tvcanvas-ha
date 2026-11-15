"""Everything TVCanvas must remember across HA restarts."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORE_VERSION


class StateStore:
    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry_id}", private=True
        )
        self._data: dict[str, Any] = {}

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {}
        for key, default in (
            ("etag", None),
            ("specs", []),
            ("rotation", {}),
            ("frame", {}),
            ("frame_tokens", {}),
            ("frame_uploaded", {}),
            ("cast", {}),
        ):
            self._data.setdefault(key, default)

    @property
    def etag(self) -> str | None:
        return self._data["etag"]

    @etag.setter
    def etag(self, value: str | None) -> None:
        self._data["etag"] = value
        self.schedule_save()

    @property
    def specs(self) -> list[dict[str, Any]]:
        return self._data["specs"]

    @specs.setter
    def specs(self, value: list[dict[str, Any]]) -> None:
        self._data["specs"] = value
        self.schedule_save()

    def rotation(self, tv_id: str) -> dict[str, Any]:
        return self._data["rotation"].setdefault(tv_id, {})

    def frame(self, tv_id: str) -> dict[str, Any]:
        return self._data["frame"].setdefault(tv_id, {"current": None, "stale": []})

    def frame_uploaded(self, tv_id: str) -> dict[str, str]:
        """Every image HA put in a Frame's My Photos and has not deleted: content id ->
        when (UTC, ISO), oldest first. The only proof of which images are HA's, so it
        outlives the pin (`forget_tv`): with Replace previous off HA leaves its images on
        the TV, and a later cleanup must never touch the user's own art."""
        return self._data["frame_uploaded"].setdefault(tv_id, {})

    def cast(self, tv_id: str) -> dict[str, Any]:
        """A Cast TV's learned identity and, after a DHCP move, where HA followed it."""
        return self._data["cast"].setdefault(tv_id, {})

    def frame_token(self, host: str) -> str | None:
        return self._data["frame_tokens"].get(host)

    def set_frame_token(self, host: str, token: str) -> None:
        self._data["frame_tokens"][host] = token
        self.schedule_save()

    def forget_tv(self, tv_id: str) -> None:
        self._data["rotation"].pop(tv_id, None)
        self._data["frame"].pop(tv_id, None)
        self._data["cast"].pop(tv_id, None)
        self.schedule_save()

    def schedule_save(self) -> None:
        self._store.async_delay_save(lambda: self._data, 10)

    async def async_save_now(self) -> None:
        await self._store.async_save(self._data)

    async def async_remove(self) -> None:
        """Delete the file (Frame pairing tokens, TV IPs and MACs, URLs) for good."""
        await self._store.async_remove()
