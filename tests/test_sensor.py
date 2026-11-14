from __future__ import annotations

from unittest.mock import patch

from homeassistant.helpers import entity_registry as er

from .common import URL, entry, frame_spec


async def test_one_sensor_per_tv_tracks_status(hass, aioclient_mock) -> None:
    aioclient_mock.get(
        f"{URL}/ha/agent/sync", json={"specs": [frame_spec("tv-1")]}, headers={"ETag": '"e"'}
    )
    e = entry(hass)
    with patch("custom_components.tvcanvas.runner.TVRunner.start"):
        await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    ent_id = er.async_get(hass).async_get_entity_id("sensor", "tvcanvas", f"{e.entry_id}_tv-1")
    assert ent_id
    state = hass.states.get(ent_id)
    assert state.state == "unknown" and state.attributes["kind"] == "frame"
    assert state.attributes["tv_state"] is None  # not asked yet
    runner = e.runtime_data.runners["tv-1"]
    runner.status.last_error = "TV unreachable: x"
    e.runtime_data.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(ent_id).attributes["last_error"] == "TV unreachable: x"


async def _sync(hass, aioclient_mock, e, specs, etag) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/ha/agent/sync", json={"specs": specs}, headers={"ETag": f'"{etag}"'})
    with patch("custom_components.tvcanvas.runner.TVRunner.start"):
        await e.runtime_data.async_refresh()
    await hass.async_block_till_done()


def _sensor(hass, e, tv_id):
    ent_id = er.async_get(hass).async_get_entity_id("sensor", "tvcanvas", f"{e.entry_id}_{tv_id}")
    return ent_id and hass.states.get(ent_id)


async def test_late_tv_gets_sensor_removed_tv_unavailable_readded_reuses(
    hass, aioclient_mock
) -> None:
    aioclient_mock.get(
        f"{URL}/ha/agent/sync", json={"specs": [frame_spec("tv-1")]}, headers={"ETag": '"e"'}
    )
    e = entry(hass)
    with patch("custom_components.tvcanvas.runner.TVRunner.start"):
        await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    assert _sensor(hass, e, "tv-2") is None

    await _sync(hass, aioclient_mock, e, [frame_spec("tv-1"), frame_spec("tv-2")], "e2")
    assert _sensor(hass, e, "tv-2").state == "unknown"

    with patch("custom_components.tvcanvas.runner.TVRunner.stop"):
        await _sync(hass, aioclient_mock, e, [frame_spec("tv-1")], "e3")
    assert _sensor(hass, e, "tv-2").state == "unavailable"

    await _sync(hass, aioclient_mock, e, [frame_spec("tv-1"), frame_spec("tv-2")], "e4")
    assert _sensor(hass, e, "tv-2").state == "unknown"
    registry = er.async_get(hass)
    ours = [
        x
        for x in registry.entities.values()
        if x.config_entry_id == e.entry_id and x.translation_key == "last_update"
    ]
    assert len(ours) == 2  # one per TV, re-adding tv-2 reused its entity


async def test_sensor_has_no_attributes_once_its_tv_is_gone(hass, aioclient_mock) -> None:
    from custom_components.tvcanvas.sensor import TVStatusSensor

    aioclient_mock.get(
        f"{URL}/ha/agent/sync", json={"specs": [frame_spec("tv-1")]}, headers={"ETag": '"e"'}
    )
    e = entry(hass)
    with patch("custom_components.tvcanvas.runner.TVRunner.start"):
        await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    sensor = TVStatusSensor(e.runtime_data, e.entry_id, "tv-1")
    assert sensor.device_info["model"] == "Samsung Frame"
    e.runtime_data.runners.pop("tv-1")
    assert not sensor.available
    assert sensor.native_value is None and sensor.extra_state_attributes == {}
