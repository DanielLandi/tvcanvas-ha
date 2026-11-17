"""Per TV: "Pause rotation". While on, HA keeps the TV's current item and doesn't touch
the TV; off, the rotation resumes at the next tick. Kept in HA's store, so it outlives a
restart. A pick in "Now showing", "Next image" or "Show preview on TV" still shows that
item, and the rotation stays paused on it."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TVCanvasConfigEntry
from .entity import TVEntity, per_tv


async def async_setup_entry(
    hass: HomeAssistant, entry: TVCanvasConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    per_tv(
        coordinator,
        lambda runner: TVPauseSwitch(coordinator, entry.entry_id, runner),
        async_add_entities,
    )


class TVPauseSwitch(TVEntity, SwitchEntity):
    _attr_translation_key = "pause_rotation"

    @property
    def is_on(self) -> bool | None:
        runner = self._runner
        return runner.paused if runner is not None else None

    def _set(self, paused: bool) -> None:
        runner = self._runner
        if runner is None:
            raise HomeAssistantError("Home Assistant doesn't run this TV now")
        runner.set_paused(paused)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._set(False)
