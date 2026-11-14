"""One status sensor per TV."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import TVCanvasConfigEntry
from .const import DOMAIN
from .coordinator import TVCanvasCoordinator
from .lan_pairing import LanPairing
from .runner import TVRunner
from .urlsafety import redact


def hub_device(entry_id: str) -> DeviceInfo:
    """The integration's own device, beside the TVs' devices."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="TVCanvas",
        manufacturer="TVCanvas",
        entry_type=DeviceEntryType.SERVICE,
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: TVCanvasConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data

    added: set[str] = set()

    @callback
    def add(runner: TVRunner) -> None:
        if runner.spec.tv.id in added:
            return
        added.add(runner.spec.tv.id)
        async_add_entities([TVStatusSensor(coordinator, entry.entry_id, runner.spec.tv.id)])

    coordinator.async_add_runner_listener(add)
    if coordinator.pairing is not None:
        async_add_entities([PairedPhonesSensor(entry.entry_id, coordinator.pairing)])


class PairedPhonesSensor(SensorEntity):
    """How many phones may push photos; names and dates (never keys) as an attribute."""

    _attr_has_entity_name = True
    _attr_translation_key = "paired_phones"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, entry_id: str, pairing: LanPairing) -> None:
        self._pairing = pairing
        self._attr_unique_id = f"{entry_id}_lan_phones"
        self._attr_device_info = hub_device(entry_id)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._pairing.async_add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> int:
        return len(self._pairing.phones)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"phones": [p.public() for p in self._pairing.phones]}


class TVStatusSensor(CoordinatorEntity[TVCanvasCoordinator], SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_has_entity_name = True
    _attr_translation_key = "last_update"

    def __init__(self, coordinator: TVCanvasCoordinator, entry_id: str, tv_id: str) -> None:
        super().__init__(coordinator)
        self._tv_id = tv_id
        self._attr_unique_id = f"{entry_id}_{tv_id}"
        runner = coordinator.runners[tv_id]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{tv_id}")},
            name=runner.spec.tv.name,
            manufacturer="TVCanvas",
            model="Samsung Frame" if runner.spec.tv.kind == "frame" else "Google Cast",
        )

    @property
    def _runner(self) -> TVRunner | None:
        return self.coordinator.runners.get(self._tv_id)

    @property
    def available(self) -> bool:
        return self._runner is not None

    @property
    def native_value(self) -> datetime | None:
        return self._runner.status.last_ok if self._runner else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        r = self._runner
        if r is None:
            return {}
        return {
            "kind": r.spec.tv.kind,
            "source": r.spec.source.type,
            "last_error": r.status.last_error and redact(r.status.last_error),
            "last_tick": r.status.last_tick.isoformat() if r.status.last_tick else None,
            "tv_state": r.tv_state,
            "boards_paused_until": (
                r.boards_paused_until.isoformat() if r.boards_paused_until else None
            ),
        }
