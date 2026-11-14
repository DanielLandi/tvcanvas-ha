from __future__ import annotations

import re
import uuid
from unittest.mock import ANY, MagicMock, patch

import pytest
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.setup import async_setup_component

from custom_components.tvcanvas.display_cast import CastDisplay, CastLoadError, _play_sync
from custom_components.tvcanvas.display_view import ImageCache, ImageView
from custom_components.tvcanvas.displays import DisplayError


def test_cache_keeps_last_three_with_unguessable_names() -> None:
    cache = ImageCache()
    names = [cache.put("tv", bytes([i])) for i in range(4)]
    assert all(re.fullmatch(r"[0-9a-f]{32}\.jpg", n) for n in names)
    assert cache.get(names[0]) is None and cache.get(names[3]) == b"\x03"
    cache.drop_tv("tv")
    assert cache.get(names[3]) is None


async def test_view_serves_without_auth(hass, hass_client_no_auth) -> None:
    await async_process_ha_core_config(hass, {})
    await async_setup_component(hass, "http", {})
    cache = ImageCache()
    hass.http.register_view(ImageView(cache))
    name = cache.put("tv", b"JPEGDATA")
    client = await hass_client_no_auth()
    resp = await client.get(f"/api/tvcanvas/image/{name}")
    assert resp.status == 200 and await resp.read() == b"JPEGDATA"
    assert resp.headers["Content-Type"] == "image/jpeg"
    assert (await client.get("/api/tvcanvas/image/" + "0" * 32 + ".jpg")).status == 404


def fake_cast(response=None, sent=True, fire=True, player_state="PLAYING", idle_reason=None):
    """A chromecast whose LOAD is answered (or not) through the callback."""
    cc = MagicMock()
    mc = cc.media_controller
    mc.status.player_state, mc.status.idle_reason = player_state, idle_reason
    if response is None:
        response = {"type": "MEDIA_STATUS", "status": [{"playerState": "PLAYING"}]}

    def play_media(url, content_type, callback_function=None):
        if fire:
            callback_function(sent, response)

    mc.play_media.side_effect = play_media
    return cc


def run_play(cc) -> None:
    with patch(
        "custom_components.tvcanvas.display_cast.pychromecast.get_chromecast_from_host",
        return_value=cc,
    ):
        _play_sync("10.0.0.3", uuid.UUID(int=1), "http://ha:8123/api/tvcanvas/image/x.jpg")


def test_play_sync_calls_pychromecast() -> None:
    cc = fake_cast()
    with patch(
        "custom_components.tvcanvas.display_cast.pychromecast.get_chromecast_from_host",
        return_value=cc,
    ) as get:
        _play_sync("10.0.0.3", uuid.UUID(int=1), "http://ha:8123/api/tvcanvas/image/x.jpg")
    assert get.call_args[0][0][:2] == ("10.0.0.3", 8009)
    cc.wait.assert_called_once()
    cc.media_controller.play_media.assert_called_once_with(
        "http://ha:8123/api/tvcanvas/image/x.jpg", "image/jpeg", callback_function=ANY
    )
    cc.disconnect.assert_called_once()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"response": {"type": "LOAD_FAILED", "detailedErrorCode": 104}},
        {"response": {"type": "INVALID_REQUEST", "reason": "INVALID_COMMAND"}},
        {"sent": False, "response": {}},
        {
            "response": {
                "type": "MEDIA_STATUS",
                "status": [{"playerState": "IDLE", "idleReason": "ERROR"}],
            }
        },
        {"player_state": "IDLE", "idle_reason": "ERROR"},
    ],
)
def test_play_sync_load_failure_raises(kwargs) -> None:
    cc = fake_cast(**kwargs)
    with pytest.raises(CastLoadError):
        run_play(cc)
    cc.disconnect.assert_called_once()


def test_play_sync_unanswered_load_times_out(monkeypatch) -> None:
    monkeypatch.setattr("custom_components.tvcanvas.display_cast.CAST_LOAD_TIMEOUT_SECONDS", 0.05)
    cc = fake_cast(fire=False)
    with pytest.raises(CastLoadError, match="did not confirm"):
        run_play(cc)
    cc.disconnect.assert_called_once()


async def test_load_failure_is_display_error(hass) -> None:
    await async_process_ha_core_config(hass, {"internal_url": "http://192.168.0.10:8123"})
    disp = CastDisplay(hass, ImageCache(), "tv-1", "10.0.0.3")
    cc = fake_cast(response={"type": "LOAD_FAILED"})
    with (
        patch(
            "custom_components.tvcanvas.display_cast.pychromecast.get_chromecast_from_host",
            return_value=cc,
        ),
        pytest.raises(DisplayError, match="did not load the image"),
    ):
        await disp.show(b"J")


async def test_show_uses_internal_url(hass) -> None:
    await async_process_ha_core_config(hass, {"internal_url": "http://192.168.0.10:8123"})
    disp = CastDisplay(hass, ImageCache(), "tv-1", "10.0.0.3")
    with patch("custom_components.tvcanvas.display_cast._play_sync") as play:
        await disp.show(b"J")
    url = play.call_args[0][2]
    assert url.startswith("http://192.168.0.10:8123/api/tvcanvas/image/")


async def test_show_without_local_url_is_a_clear_error(hass) -> None:
    await async_process_ha_core_config(hass, {})
    disp = CastDisplay(hass, ImageCache(), "tv-1", "10.0.0.3")
    with (
        patch("custom_components.tvcanvas.display_cast.get_url", side_effect=NoURLAvailableError),
        pytest.raises(DisplayError, match="local URL"),
    ):
        await disp.show(b"J")


async def test_unreachable_cast_is_display_error(hass) -> None:
    await async_process_ha_core_config(hass, {"internal_url": "http://192.168.0.10:8123"})
    disp = CastDisplay(hass, ImageCache(), "tv-1", "10.0.0.3")
    with (
        patch(
            "custom_components.tvcanvas.display_cast._play_sync", side_effect=OSError("no route")
        ),
        pytest.raises(DisplayError, match="TV unreachable"),
    ):
        await disp.show(b"J")
