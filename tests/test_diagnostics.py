from __future__ import annotations

from unittest.mock import patch

from custom_components.tvcanvas.diagnostics import async_get_config_entry_diagnostics

from .common import URL, entry, frame_spec


async def test_diagnostics_redacts_secrets(hass, aioclient_mock) -> None:
    aioclient_mock.get(
        f"{URL}/ha/agent/sync", json={"specs": [frame_spec("tv-1")]}, headers={"ETag": '"e"'}
    )
    e = entry(hass)
    with patch("custom_components.tvcanvas.runner.TVRunner.start"):
        await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, e)
    text = repr(diag)
    assert "fc_ha_t" not in text and "10.0.0.2" not in text
    assert diag["tvs"]["tv-1"]["kind"] == "frame"


async def test_diagnostics_scrubs_host_from_last_error(hass, aioclient_mock) -> None:
    aioclient_mock.get(
        f"{URL}/ha/agent/sync", json={"specs": [frame_spec("tv-1")]}, headers={"ETag": '"e"'}
    )
    e = entry(hass)
    with patch("custom_components.tvcanvas.runner.TVRunner.start"):
        await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    e.runtime_data.runners[
        "tv-1"
    ].status.last_error = "TV unreachable: Unable to connect to 10.0.0.2:8002"
    text = repr(await async_get_config_entry_diagnostics(hass, e))
    assert "10.0.0.2" not in text and "TV unreachable" in text
