"""Shared base for the per-TV dashboard entities (selects, images, switch, buttons)."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TVCanvasCoordinator
from .items import Item, items_of, option_names
from .models import SyncSpec
from .runner import NowShowing, TVRunner


def tv_device(entry_id: str, spec: SyncSpec) -> DeviceInfo:
    """The TV's device; the same identifiers as the status sensor's (sensor.py)."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_{spec.tv.id}")},
        name=spec.tv.name,
        manufacturer="TVCanvas",
        model="Samsung Frame" if spec.tv.kind == "frame" else "Google Cast",
    )


def per_tv(
    coordinator: TVCanvasCoordinator, make: Callable[[TVRunner], object], add: Callable
) -> None:
    """Add one entity per TV, now and for every TV the phone hands over later."""
    added: set[str] = set()

    @callback
    def on_runner(runner: TVRunner) -> None:
        if runner.spec.tv.id in added:
            return
        added.add(runner.spec.tv.id)
        add([make(runner)])

    coordinator.async_add_runner_listener(on_runner)


class TVEntity(CoordinatorEntity[TVCanvasCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: TVCanvasCoordinator, entry_id: str, runner: TVRunner) -> None:
        super().__init__(coordinator)
        self._tv_id = runner.spec.tv.id
        self._attr_unique_id = f"{entry_id}_{self._tv_id}_{self._attr_translation_key}"
        self._attr_device_info = tv_device(entry_id, runner.spec)

    @property
    def _runner(self) -> TVRunner | None:
        return self.coordinator.runners.get(self._tv_id)

    @property
    def available(self) -> bool:
        return self._runner is not None

    def _items(self) -> tuple[list[Item], list[str]]:
        runner = self._runner
        if runner is None:
            return [], []
        items = items_of(runner.spec.source)
        return items, option_names(items)

    def _current(self) -> NowShowing | None:
        """What HA last showed, while it still belongs to the TV's current spec."""
        runner = self._runner
        shown = runner.now_showing if runner is not None else None
        if shown is None or shown.source != runner.spec.source:
            return None
        return shown

    def _previewed(self) -> NowShowing | None:
        """The preview, while it still belongs to the TV's current spec."""
        runner = self._runner
        preview = runner.preview if runner is not None else None
        if preview is None or preview.source != runner.spec.source:
            return None
        return preview
