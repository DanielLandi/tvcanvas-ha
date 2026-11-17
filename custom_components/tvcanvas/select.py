"""Per TV: what the pin rotates through, a way to show one item now, and a preview.

Options are the spec's items (a board, a collection's images, webcams, a group's
boards, links and phone photos) by name.

- "Now showing": the state is the item on the TV. Picking one shows it at once and the
  rotation carries on after it (or stays on it while "Pause rotation" is on), until the
  phone edits the pin.
- "Preview": picking one renders it for the "Preview image" entity without touching the
  TV; "Show preview on TV" then sends it. A board preview renders through TVCanvas Cloud
  and uses a credit, as on the phone."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TVCanvasConfigEntry
from .entity import TVEntity, per_tv
from .runner import PreviewRefused

PREVIEW_NOTE = (
    "Previewing a board renders it through TVCanvas Cloud, which uses 1 credit as on the "
    "phone. Phone photos, webcams, links and collection images are free."
)


async def async_setup_entry(
    hass: HomeAssistant, entry: TVCanvasConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    per_tv(
        coordinator,
        lambda runner: TVItemSelect(coordinator, entry.entry_id, runner),
        async_add_entities,
    )
    per_tv(
        coordinator,
        lambda runner: TVPreviewSelect(coordinator, entry.entry_id, runner),
        async_add_entities,
    )


class TVItemSelect(TVEntity, SelectEntity):
    _attr_translation_key = "now_showing"
    # Up to 500 items with titles: shown on the entity, never written to the recorder.
    _unrecorded_attributes = frozenset({"items"})

    @property
    def options(self) -> list[str]:
        return self._items()[1]

    @property
    def current_option(self) -> str | None:
        runner = self._runner
        held = runner.held() if runner is not None else None
        if held is None:
            return None
        names = self._items()[1]
        index = held[0].index
        return names[index] if index < len(names) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        runner = self._runner
        if runner is None:
            return {}
        items, names = self._items()
        photos = self.coordinator.photos
        listed: list[dict[str, Any]] = []
        for item, name in zip(items, names, strict=True):
            entry: dict[str, Any] = {"name": name, "type": item.type}
            if item.artist:
                entry["artist"] = item.artist
            if item.photo:
                # A phone photo HA doesn't hold yet is skipped until the phone pushes it.
                entry["waiting"] = photos is None or item.sha256 not in photos
            listed.append(entry)
        held = runner.held()
        return {
            "source": runner.spec.source.type,
            "interval_seconds": runner.spec.interval_seconds,
            "shuffle": runner.spec.source.shuffle,
            "paused": runner.paused,
            "shown_at": held[1].isoformat() if held else None,
            "items": listed,
        }

    async def async_select_option(self, option: str) -> None:
        runner = self._runner
        names = self._items()[1]
        if runner is None or option not in names:
            raise ServiceValidationError(f"{option!r} is not one of this TV's items")
        if (why := runner.blocked()) is not None:
            raise HomeAssistantError(f"{runner.spec.tv.name} can't change now: {why}")
        await runner.show_item(names.index(option))


class TVPreviewSelect(TVEntity, SelectEntity):
    """Never renders on its own: only a pick (a person, or an automation they wrote)
    makes a preview. HA restores no state for it after a restart."""

    _attr_translation_key = "preview"
    _unrecorded_attributes = frozenset({"items", "note"})

    @property
    def options(self) -> list[str]:
        return self._items()[1]

    @property
    def current_option(self) -> str | None:
        preview = self._previewed()
        if preview is None:
            return None
        names = self._items()[1]
        return names[preview.item.index] if preview.item.index < len(names) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._runner is None:
            return {}
        items, names = self._items()
        preview = self._previewed()
        return {
            "previewed_at": preview.at.isoformat() if preview else None,
            "note": PREVIEW_NOTE,
            "items": [
                {"name": name, "type": item.type, "credits": 1 if item.costs_credit else 0}
                for item, name in zip(items, names, strict=True)
            ],
        }

    async def async_select_option(self, option: str) -> None:
        runner = self._runner
        names = self._items()[1]
        if runner is None or option not in names:
            raise ServiceValidationError(f"{option!r} is not one of this TV's items")
        try:
            await runner.preview_item(names.index(option))
        except PreviewRefused as err:
            raise HomeAssistantError(f"No preview for {runner.spec.tv.name}: {err}") from err
