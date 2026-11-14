"""Group pins end to end through the coordinator (kept apart from test_coordinator.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from .common import URL, entry, frame_spec


def group_spec(tv_id="tv-1"):
    spec = frame_spec(tv_id)
    spec["source"] = {
        "type": "group",
        "group": {
            "items": [
                {"type": "board", "board": {"type_id": "weather", "params": {}}},
                {"type": "webcam", "url": "https://cams.example.com/a.jpg", "name": "Cam"},
            ]
        },
        "shuffle": True,
    }
    return spec


async def test_group_spec_runs_and_its_removal_releases_the_frame(hass, aioclient_mock) -> None:
    aioclient_mock.get(
        f"{URL}/ha/agent/sync",
        json={"specs": [group_spec("tv-1"), frame_spec("tv-2")]},
        headers={"ETag": '"e1"'},
    )
    e = entry(hass)
    with patch("custom_components.tvcanvas.runner.TVRunner.start"):
        await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    coord = e.runtime_data
    assert set(coord.runners) == {"tv-1", "tv-2"}
    assert coord.runners["tv-1"].spec.source.type == "group"
    released = []
    for tv_id, runner in coord.runners.items():
        runner._display.release = lambda tv_id=tv_id: released.append(tv_id)
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{URL}/ha/agent/sync", json={"specs": [frame_spec("tv-2")]}, headers={"ETag": '"e2"'}
    )
    with patch("custom_components.tvcanvas.runner.TVRunner.stop", new=AsyncMock()):
        await coord.async_refresh()
    assert released == ["tv-1"]


async def test_sensor_shows_until_when_a_groups_boards_are_skipped(hass, aioclient_mock) -> None:
    from datetime import UTC, datetime

    from homeassistant.helpers import entity_registry as er

    aioclient_mock.get(
        f"{URL}/ha/agent/sync", json={"specs": [group_spec("tv-1")]}, headers={"ETag": '"e1"'}
    )
    e = entry(hass)
    with patch("custom_components.tvcanvas.runner.TVRunner.start"):
        await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    ent_id = er.async_get(hass).async_get_entity_id("sensor", "tvcanvas", f"{e.entry_id}_tv-1")
    assert hass.states.get(ent_id).attributes["boards_paused_until"] is None
    runner = e.runtime_data.runners["tv-1"]
    runner._credits_retry_at = datetime(2026, 11, 9, 12, 30, tzinfo=UTC)
    e.runtime_data.async_update_listeners()
    await hass.async_block_till_done()
    attrs = hass.states.get(ent_id).attributes
    assert attrs["boards_paused_until"] == "2026-11-09T12:30:00+00:00"
