"""Per TV: "Next image" (the next tick now) and "Show preview on TV" (send the item the
"Preview" select rendered, with that same image: a previewed board costs no second
credit). Both also work while "Pause rotation" is on, which then stays on."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TVCanvasConfigEntry
from .entity import TVEntity, per_tv
from .runner import PreviewRefused, TVRunner


async def async_setup_entry(
    hass: HomeAssistant, entry: TVCanvasConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    per_tv(
        coordinator,
        lambda runner: TVNextButton(coordinator, entry.entry_id, runner),
        async_add_entities,
    )
    per_tv(
        coordinator,
        lambda runner: TVShowPreviewButton(coordinator, entry.entry_id, runner),
        async_add_entities,
    )


class _TVButton(TVEntity, ButtonEntity):
    def _ready_runner(self) -> TVRunner:
        runner = self._runner
        if runner is None:
            raise HomeAssistantError("Home Assistant doesn't run this TV now")
        if (why := runner.blocked()) is not None:
            raise HomeAssistantError(f"{runner.spec.tv.name} can't change now: {why}")
        return runner


class TVNextButton(_TVButton):
    _attr_translation_key = "next_image"

    async def async_press(self) -> None:
        await self._ready_runner().advance()


class TVShowPreviewButton(_TVButton):
    _attr_translation_key = "show_preview"

    async def async_press(self) -> None:
        runner = self._ready_runner()
        try:
            await runner.show_preview()
        except PreviewRefused as err:
            raise HomeAssistantError(f"{runner.spec.tv.name}: {err}") from err
