"""A Frame in standby gets no renders and no uploads (2026-11-13).

A long press on the Frame's power button darkens the screen but leaves the TV on the
network: its WebSocket still answers and `http://<tv>:8001/api/v2/` reports
`device.PowerState: "standby"` ("on" in Art Mode or with the TV on). HA kept rendering a
group of boards every interval for a screen nobody could see, at 0.02 credits a render.
"""

from __future__ import annotations

import re
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.tvcanvas.api import CloudClient
from custom_components.tvcanvas.const import FRAME_STANDBY_POLL_SECONDS
from custom_components.tvcanvas.display_frame import FrameDisplay
from custom_components.tvcanvas.displays import STANDBY, DisplayError
from custom_components.tvcanvas.locate import FrameFinder, power_state
from custom_components.tvcanvas.models import parse_spec
from custom_components.tvcanvas.runner import TVRunner
from custom_components.tvcanvas.storage import StateStore

from .common import URL
from .conftest import PUBLIC_IP

HOST = "10.0.0.2"
API_V2 = f"http://{HOST}:8001/api/v2/"
MODE_SYNC = "custom_components.tvcanvas.display_frame._mode_sync"
SHOW_SYNC = "custom_components.tvcanvas.display_frame._show_sync"
BOARDS = re.compile(re.escape(URL) + r"/board/")


def board(type_id: str, **params: str) -> dict:
    return {"type": "board", "board": {"type_id": type_id, "params": params}}


def group_of_boards(interval: int = 180):
    """The owner's pin, without its phone photos: four boards, shuffled by the phone."""
    return parse_spec(
        {
            "version": 1,
            "tv": {"id": "tv-1", "kind": "frame", "host": HOST, "name": "Living room"},
            "enabled": True,
            "interval_seconds": interval,
            "source": {
                "type": "group",
                "group": {
                    "items": [
                        board("flights", airport="AAA"),
                        board("weather"),
                        board("flights", airport="BBB"),
                        board("weather_cards"),
                    ]
                },
            },
        }
    )


@pytest.fixture
async def store(hass):
    s = StateStore(hass, "e")
    await s.async_load()
    return s


class Finder:
    """FrameFinder's power read; this TV's id carries no MAC, so it is never followed."""

    def __init__(self, power: str | None) -> None:
        self.power, self.asked = power, []

    async def power_state(self, address):
        self.asked.append(address)
        return self.power


def frame(hass, store, finder) -> FrameDisplay:
    return FrameDisplay(hass, store, "tv-1", HOST, True, lambda _: None, finder=finder)


class Display:
    """ready() says whatever the test sets; show() records what was sent."""

    def __init__(self, waiting: str | None = STANDBY) -> None:
        self.waiting, self.shown = waiting, []
        self.power_state = "standby" if waiting == STANDBY else "on"

    async def ready(self):
        return self.waiting

    async def show(self, jpeg):
        self.shown.append(jpeg)


@pytest.fixture
def runner(hass, store, monkeypatch):
    rendered: list[str] = []

    async def source(spec, rotation, client, session, fit, credits=None, photos=None):
        rendered.append("board")
        return b"IMG", "board"

    monkeypatch.setattr("custom_components.tvcanvas.runner.next_image", source)
    client = AsyncMock()

    def make(display, spec=None):
        r = TVRunner(
            hass,
            client,
            None,
            store,
            spec or group_of_boards(),
            display,
            lambda: None,
            lambda: None,
        )
        r.rendered, r.client = rendered, client
        return r

    return make


# --- the runner ------------------------------------------------------------------------


async def test_standby_renders_and_uploads_nothing_and_is_not_an_error(runner) -> None:
    disp = Display(STANDBY)
    r = runner(disp)
    r.status.last_error = "TV unreachable: x"  # from before it went to standby
    await r.tick()
    assert r.rendered == [] and disp.shown == []
    assert r.status.last_ok is None  # HA's sensor keeps the last image actually shown
    assert r.status.last_error is None and r.status.last_tick is not None
    assert r.tv_state == "standby"
    # The contract has no standby status: ok with no error, so the phone shows none.
    tv_id, ok, error, _at = r.client.agent_status.call_args[0]
    assert (tv_id, ok, error) == ("tv-1", True, None)
    # No backoff: asked again soon, not after the 180 s interval.
    assert r.next_delay() == FRAME_STANDBY_POLL_SECONDS


async def test_standby_to_on_pushes_on_that_tick(runner) -> None:
    disp = Display(STANDBY)
    r = runner(disp)
    await r.tick()
    await r.tick()
    assert r.rendered == [] and r.next_delay() == FRAME_STANDBY_POLL_SECONDS
    disp.waiting, disp.power_state = None, "on"  # the owner pressed power again
    await r.tick()
    assert r.rendered == ["board"] and disp.shown == [b"IMG"]
    assert r.status.last_ok is not None and r.tv_state == "on"
    assert r.next_delay() == 180  # back to the pin's interval


async def test_standby_reports_to_the_cloud_once_per_interval(runner, freezer) -> None:
    r = runner(Display(STANDBY))
    for _ in range(180 // FRAME_STANDBY_POLL_SECONDS):  # one interval of 30 s polls
        await r.tick()
        freezer.tick(FRAME_STANDBY_POLL_SECONDS)
    assert r.client.agent_status.await_count == 1
    await r.tick()  # 180 s after standby began
    assert r.client.agent_status.await_count == 2


async def test_waking_into_watch_tv_waits_as_before(runner) -> None:
    disp = Display(STANDBY)
    r = runner(disp)
    await r.tick()
    disp.waiting, disp.power_state = "waiting: Watch TV", "on"
    await r.tick()
    assert r.rendered == [] and r.status.last_error == "waiting: Watch TV"
    assert r.tv_state == "on" and r.next_delay() == 180


async def test_unreachable_after_standby_backs_off_as_before(runner) -> None:
    class Gone(Display):
        async def ready(self):
            raise DisplayError("TV unreachable: x")

    disp = Display(STANDBY)
    r = runner(disp)
    await r.tick()
    r._display = gone = Gone(None)
    gone.power_state = None
    await r.tick()
    assert r.status.last_error == "TV unreachable: x" and r.tv_state is None
    assert r.next_delay() == 180  # max(interval, 60): the existing backoff


# --- the Frame display -------------------------------------------------------------------


async def test_frame_in_standby_is_never_opened(hass, store) -> None:
    finder = Finder("standby")
    with patch(MODE_SYNC) as mode:
        assert await frame(hass, store, finder).ready() == STANDBY
    mode.assert_not_called()  # no WebSocket, no pairing prompt, no cleanup
    assert finder.asked == [HOST]


@pytest.mark.parametrize("power", ["on", None, "something new"])
async def test_on_or_unknown_power_state_runs_the_tick_as_before(hass, store, power) -> None:
    disp = frame(hass, store, Finder(power))
    with patch(MODE_SYNC, return_value=("tvcanvas", None, [])) as mode:
        assert await disp.ready() is None
    mode.assert_called_once()
    assert disp.power_state == power


async def test_no_power_answer_and_no_websocket_is_still_unreachable(hass, store) -> None:
    """/api/v2/ silent too: the WebSocket decides, so the #26 follow path is unchanged."""
    with (
        patch(MODE_SYNC, side_effect=OSError(113, "Host is unreachable")),
        pytest.raises(DisplayError, match="TV unreachable"),
    ):
        await frame(hass, store, Finder(None)).ready()


async def test_power_state_reads_api_v2(hass, aioclient_mock) -> None:
    aioclient_mock.get(API_V2, json={"device": {"PowerState": "Standby", "wifiMac": "x"}})
    aioclient_mock.get("http://10.0.0.3:8001/api/v2/", json={"device": {"wifiMac": "x"}})
    aioclient_mock.get("http://10.0.0.4:8001/api/v2/", status=500)
    assert await power_state(hass, HOST) == "standby"
    assert await power_state(hass, "10.0.0.3") is None  # older firmware
    assert await power_state(hass, "10.0.0.4") is None
    assert await power_state(hass, PUBLIC_IP) is None  # never asked
    assert len(aioclient_mock.mock_calls) == 3


# --- credits: nothing reaches /board/* while the Frame sleeps ------------------------------


async def test_a_frame_in_standby_costs_no_board_credits(
    hass, aioclient_mock, store, monkeypatch, freezer
) -> None:
    """End to end with the real cloud client, Frame display and /api/v2/ read: the
    Frame's WebSocket still answers in standby (that was the bug), so only the power
    state stands between a tick and a charged render."""
    monkeypatch.setattr("custom_components.tvcanvas.runner.fit_to_tv", lambda data, _o: data)
    aioclient_mock.get(API_V2, json={"device": {"PowerState": "standby"}})
    aioclient_mock.post(f"{URL}/ha/agent/status", status=204)
    aioclient_mock.get(BOARDS, content=b"JPEG", headers={"Content-Type": "image/jpeg"})
    client = CloudClient(async_get_clientsession(hass), URL, token="fc_ha_t")
    disp = frame(hass, store, FrameFinder(hass))
    r = TVRunner(hass, client, None, store, group_of_boards(), disp, lambda: None, lambda: None)

    def board_calls() -> list[str]:
        return [str(url) for _m, url, *_ in aioclient_mock.mock_calls if BOARDS.match(str(url))]

    awake = ("tvcanvas", None, [])  # what the WebSocket says even in standby
    shown = ("MY_F0001", None, [], [])
    with patch(MODE_SYNC, return_value=awake), patch(SHOW_SYNC, return_value=shown) as show:
        for _ in range(3600 // FRAME_STANDBY_POLL_SECONDS):  # an hour asleep
            await r.tick()
            freezer.tick(timedelta(seconds=FRAME_STANDBY_POLL_SECONDS))
        assert board_calls() == []
        show.assert_not_called()

        aioclient_mock.clear_requests()
        aioclient_mock.get(API_V2, json={"device": {"PowerState": "on"}})
        aioclient_mock.post(f"{URL}/ha/agent/status", status=204)
        aioclient_mock.get(BOARDS, content=b"JPEG", headers={"Content-Type": "image/jpeg"})
        await r.tick()
    assert board_calls() == [f"{URL}/board/flights?airport=AAA"]
    show.assert_called_once()
