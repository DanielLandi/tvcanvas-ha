"""Per-TV controls: "Pause rotation", "Next image", previews ("Preview", "Preview image",
"Show preview on TV") and the status report's `showing` for the phone."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from custom_components.tvcanvas import runner as runner_mod
from custom_components.tvcanvas import sources
from custom_components.tvcanvas.api import CloudClient, valid_showing
from custom_components.tvcanvas.items import items_of, rotation_order
from custom_components.tvcanvas.models import parse_spec
from custom_components.tvcanvas.runner import NowShowing, PreviewRefused, TVRunner
from custom_components.tvcanvas.storage import StateStore

from .common import URL, entry
from .test_dashboard_entities import GROUP, SHA, collection, spec_dict

BOARD = {"type": "board", "board": {"type_id": "weather", "params": {"location": "x"}}}


class Display:
    def __init__(self, asleep: bool = False) -> None:
        self.shown: list[bytes] = []
        self.ready_calls = 0
        self._asleep = asleep

    async def ready(self) -> str | None:
        self.ready_calls += 1
        return None

    async def asleep(self) -> bool:
        return self._asleep

    async def show(self, jpeg: bytes) -> None:
        self.shown.append(jpeg)


class Photos:
    """A PhotoStore stand-in holding the given hashes."""

    def __init__(self, *held: str) -> None:
        self.held = set(held)

    def __contains__(self, sha: object) -> bool:
        return sha in self.held

    async def async_read(self, sha: str) -> bytes | None:
        return f"photo {sha[:4]}".encode() if sha in self.held else None

    def missing(self, spec) -> int:
        return len(spec.source.local_hashes - self.held)


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(runner_mod, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


@pytest.fixture
async def store(hass):
    s = StateStore(hass, "e")
    await s.async_load()
    return s


@pytest.fixture
def make_runner(hass, monkeypatch, store, clock):
    async def fetch(session, url):
        return url.encode()

    monkeypatch.setattr(sources, "fetch_image", fetch)

    def make(spec, photos=None, display=None):
        client = AsyncMock()
        client.board = AsyncMock(return_value=b"BOARD")
        client.proxied_image = AsyncMock(return_value=b"PROXIED")
        display = display or Display()
        runner = TVRunner(
            hass, client, None, store, spec, display, lambda: None, lambda: None, photos
        )
        runner._fit = AsyncMock(side_effect=lambda data, orientation: data)
        runner.display, runner.client = display, client
        return runner

    return make


def _no_restart():
    """show_item / advance restart the loop; the test runs the tick itself."""
    return patch.object(TVRunner, "start", lambda self: None)


def _last_status(runner) -> tuple:
    return runner.client.agent_status.call_args[0]


# --- Pause rotation ------------------------------------------------------------------


async def test_pause_persists_across_restarts_and_leaves_with_the_tv(hass, store) -> None:
    store.set_paused("tv-1", True)
    await store.async_save_now()
    again = StateStore(hass, "e")
    await again.async_load()
    assert again.paused("tv-1") and not again.paused("tv-2")
    again.set_paused("tv-1", False)
    assert not again.paused("tv-1")
    again.set_paused("tv-1", True)
    again.forget_tv("tv-1")  # the phone took the TV back: a later hand-over starts unpaused
    assert not again.paused("tv-1")


async def test_a_paused_tv_is_not_touched_and_reports_paused(hass, make_runner) -> None:
    r = make_runner(parse_spec(spec_dict(collection(3))))
    await r.tick()
    assert r.display.shown == [b"https://img.example.com/0.jpg"]
    r.set_paused(True)
    ready_calls = r.display.ready_calls
    for _ in range(3):
        await r.tick()
    assert r.display.shown == [b"https://img.example.com/0.jpg"]  # kept the current item
    assert r.display.ready_calls == ready_calls  # no power or mode check either
    tv_id, ok, error, _at, showing = _last_status(r)
    assert (tv_id, ok, error) == ("tv-1", True, None)
    assert showing["paused"] is True and showing["name"] == "Art 0"
    assert r.next_delay() == 300

    r.set_paused(False)  # resumes at the next tick, after the held item
    await r.tick()
    assert r.display.shown[-1] == b"https://img.example.com/1.jpg"
    assert _last_status(r)[4]["paused"] is False


async def test_a_pick_while_paused_shows_it_and_stays_paused(hass, make_runner) -> None:
    r = make_runner(parse_spec(spec_dict(collection(4))))
    r.set_paused(True)
    with _no_restart():
        await r.show_item(2)
    await r.tick()
    assert r.display.shown == [b"https://img.example.com/2.jpg"]
    assert r.paused
    await r.tick()  # paused again: no rotation
    assert r.display.shown == [b"https://img.example.com/2.jpg"]
    assert _last_status(r)[4] | {"since": None} == {
        "name": "Art 2",
        "kind": "collection",
        "index": 2,
        "count": 4,
        "since": None,
        "paused": True,
    }


async def test_switch_entity_pauses_and_resumes(hass, aioclient_mock) -> None:
    e = await _setup(hass, aioclient_mock, [spec_dict(collection(3))])
    sw = _entity_id(hass, e, "switch", "pause_rotation")
    assert sw == "switch.living_room_pause_rotation"
    assert hass.states.get(sw).state == "off"
    await hass.services.async_call("switch", "turn_on", {"entity_id": sw}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(sw).state == "on"
    assert e.runtime_data.runners["tv-1"].paused
    sel = _entity_id(hass, e, "select", "now_showing")
    assert hass.states.get(sel).attributes["paused"] is True
    await hass.services.async_call("switch", "turn_off", {"entity_id": sw}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(sw).state == "off"


# --- Next image ----------------------------------------------------------------------


@pytest.mark.parametrize("paused", [False, True])
async def test_next_advances_now_also_while_paused(hass, make_runner, paused) -> None:
    r = make_runner(parse_spec(spec_dict(collection(3))))
    await r.tick()
    r.set_paused(paused)
    with _no_restart():
        await r.advance()
    await r.tick()
    assert r.display.shown == [b"https://img.example.com/0.jpg", b"https://img.example.com/1.jpg"]
    assert r.paused is paused
    await r.tick()
    assert len(r.display.shown) == (2 if paused else 3)


async def test_next_restarts_the_loop_for_an_immediate_tick(hass, make_runner) -> None:
    r = make_runner(parse_spec(spec_dict(collection(3))))
    calls: list[str] = []
    with (
        patch.object(TVRunner, "_stop", AsyncMock(side_effect=lambda: calls.append("stop"))),
        patch.object(TVRunner, "start", lambda self: calls.append("start")),
    ):
        await r.advance()
    assert calls == ["stop", "start"]


async def test_next_button_presses_and_refuses_when_blocked(hass, aioclient_mock) -> None:
    e = await _setup(
        hass, aioclient_mock, [spec_dict(collection(3)), spec_dict(collection(2), "tv-2", **_OFF)]
    )
    btn = _entity_id(hass, e, "button", "next_image")
    assert btn == "button.living_room_next_image"
    with patch.object(TVRunner, "advance", AsyncMock()) as advance:
        await hass.services.async_call("button", "press", {"entity_id": btn}, blocking=True)
    advance.assert_awaited_once()
    off = _entity_id(hass, e, "button", "next_image", "tv-2")
    with (
        patch.object(TVRunner, "advance", AsyncMock()) as advance,
        pytest.raises(HomeAssistantError, match="turned this TV off"),
    ):
        await hass.services.async_call("button", "press", {"entity_id": off}, blocking=True)
    advance.assert_not_awaited()


_OFF = {"enabled": False}


# --- previews ------------------------------------------------------------------------


async def test_a_photo_preview_is_free_and_never_touches_the_tv(hass, make_runner) -> None:
    r = make_runner(parse_spec(spec_dict(GROUP)), photos=Photos(SHA))
    await r.preview_item(2)
    assert r.preview.image == f"photo {SHA[:4]}".encode() and r.preview.item.photo
    assert r.display.shown == [] and r.display.ready_calls == 0
    r.client.board.assert_not_awaited()  # no credit
    r.client.agent_status.assert_not_awaited()  # nothing reported either
    assert r.now_showing is None


async def test_a_board_preview_renders_once_and_uses_a_credit(hass, make_runner, clock) -> None:
    r = make_runner(parse_spec(spec_dict(GROUP)), photos=Photos(SHA))
    await r.preview_item(0)
    assert r.client.board.await_count == 1 and r.preview.image == b"BOARD"
    assert r.display.shown == []
    # At most one board preview per 30 s per TV …
    clock[0] += 29
    with pytest.raises(PreviewRefused, match="try again in 1 s"):
        await r.preview_item(0)
    assert r.client.board.await_count == 1
    # … which doesn't hold up free previews (those wait 5 s between each other).
    await r.preview_item(1)
    with pytest.raises(PreviewRefused, match="try again"):
        await r.preview_item(2)
    clock[0] += 5
    await r.preview_item(2)
    clock[0] += 1
    await r.preview_item(0)
    assert r.client.board.await_count == 2


async def test_a_collection_preview_goes_through_the_proxy(hass, make_runner) -> None:
    src = collection(2)
    src["images"][1]["proxied"] = True
    src["images"][1]["url"] = "img-123"
    r = make_runner(parse_spec(spec_dict(src)))
    await r.preview_item(1)
    r.client.proxied_image.assert_awaited_once_with("img-123")
    assert r.preview.image == b"PROXIED"


async def test_no_board_preview_while_the_tv_is_in_standby(hass, make_runner) -> None:
    r = make_runner(parse_spec(spec_dict(GROUP)), Photos(SHA), Display(asleep=True))
    with pytest.raises(PreviewRefused, match="standby"):
        await r.preview_item(0)
    r.client.board.assert_not_awaited()
    await r.preview_item(2)  # a phone photo costs nothing: fine in standby
    assert r.preview.item.photo
    # Known from the last tick too, without asking the TV.
    awake = make_runner(parse_spec(spec_dict(BOARD)), display=Display())
    awake._standby = True
    with pytest.raises(PreviewRefused, match="standby"):
        await awake.preview_item(0)
    awake.client.board.assert_not_awaited()


async def test_no_board_preview_out_of_credits_or_with_the_tv_off(hass, make_runner) -> None:
    r = make_runner(parse_spec(spec_dict(BOARD)))
    r._credits_retry_at = dt_util.utcnow() + timedelta(minutes=5)
    with pytest.raises(PreviewRefused, match="out of credits"):
        await r.preview_item(0)
    off = make_runner(parse_spec(spec_dict(collection(2), enabled=False)))
    with pytest.raises(PreviewRefused, match="turned this TV off"):
        await off.preview_item(0)
    r.client.board.assert_not_awaited()


async def test_a_402_on_a_board_preview_says_out_of_credits(hass, make_runner) -> None:
    r = make_runner(parse_spec(spec_dict(BOARD)))
    r.client.board = AsyncMock(side_effect=sources.OutOfCredits())
    with pytest.raises(PreviewRefused, match="out of credits"):
        await r.preview_item(0)
    assert r.preview is None


async def test_a_photo_ha_does_not_hold_yet_has_no_preview(hass, make_runner) -> None:
    r = make_runner(parse_spec(spec_dict(GROUP)), photos=Photos())
    with pytest.raises(PreviewRefused, match="doesn't have this photo"):
        await r.preview_item(2)


async def test_previews_never_run_by_themselves(hass, aioclient_mock) -> None:
    """Setup, ticks, a spec refresh and a restart make no preview and render no board."""
    e = await _setup(hass, aioclient_mock, [spec_dict(GROUP)])
    runner = e.runtime_data.runners["tv-1"]
    with patch.object(TVRunner, "preview_item", AsyncMock()) as preview:
        await e.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert await hass.config_entries.async_reload(e.entry_id)
        await hass.async_block_till_done()
    preview.assert_not_awaited()
    assert runner.preview is None
    sel = _entity_id(hass, e, "select", "preview")
    assert hass.states.get(sel).state == "unknown"


async def test_preview_select_and_image(hass, aioclient_mock, hass_client) -> None:
    e = await _setup(hass, aioclient_mock, [spec_dict(GROUP)])
    sel = _entity_id(hass, e, "select", "preview")
    img = _entity_id(hass, e, "image", "preview_image")
    assert (sel, img) == ("select.living_room_preview", "image.living_room_preview_image")
    attrs = hass.states.get(sel).attributes
    assert attrs["options"] == ["Weather Dashboard", "Beach", "IMG_0042", "Link"]
    assert [i["credits"] for i in attrs["items"]] == [1, 0, 0, 0]
    assert "1 credit" in attrs["note"]

    async def preview(self, index):
        self.preview = NowShowing(
            self.spec.source, items_of(self.spec.source)[index], b"WEBCAM", dt_util.utcnow()
        )

    with patch.object(TVRunner, "preview_item", preview):
        await hass.services.async_call(
            "select", "select_option", {"entity_id": sel, "option": "Beach"}, blocking=True
        )
    e.runtime_data.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(sel).state == "Beach"
    client = await hass_client()
    resp = await client.get(f"/api/image_proxy/{img}")
    assert resp.status == HTTPStatus.OK and await resp.read() == b"WEBCAM"
    # The TV and its "Current image" were not touched.
    assert e.runtime_data.runners["tv-1"].now_showing is None

    with (
        patch.object(TVRunner, "preview_item", AsyncMock(side_effect=PreviewRefused("nope"))),
        pytest.raises(HomeAssistantError, match="nope"),
    ):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": sel, "option": "Weather Dashboard"},
            blocking=True,
        )


@pytest.mark.parametrize(("at_home", "served"), [(True, True), (False, False)])
async def test_a_previewed_photo_is_shown_only_on_the_home_network(
    hass, aioclient_mock, hass_client, at_home, served
) -> None:
    e = await _setup(hass, aioclient_mock, [spec_dict(GROUP)])
    img = _entity_id(hass, e, "image", "preview_image")
    runner = e.runtime_data.runners["tv-1"]
    await e.runtime_data.photos.async_put(SHA, b"original")
    runner.preview = NowShowing(
        runner.spec.source, items_of(runner.spec.source)[2], b"PHOTO", dt_util.utcnow()
    )
    e.runtime_data.async_update_listeners()
    await hass.async_block_till_done()
    client = await hass_client()
    with patch("custom_components.tvcanvas.image.from_home", return_value=at_home):
        resp = await client.get(f"/api/image_proxy/{img}")
    body = await resp.read()
    assert (resp.status == HTTPStatus.OK and body == b"PHOTO") is served
    if not served:
        assert b"PHOTO" not in body
    from custom_components.tvcanvas.image import TVPreviewImage

    entity = TVPreviewImage(e.runtime_data, e.entry_id, runner)
    assert await entity.async_image() is None  # no request at all: no photo bytes


async def test_show_preview_sends_the_previewed_image_without_a_second_render(
    hass, make_runner
) -> None:
    r = make_runner(parse_spec(spec_dict(GROUP)), photos=Photos(SHA))
    with pytest.raises(PreviewRefused, match="pick an item"):
        await r.show_preview()
    await r.preview_item(0)  # the board: one render
    r.set_paused(True)
    with _no_restart():
        await r.show_preview()
    await r.tick()
    assert r.display.shown == [b"BOARD"]
    assert r.client.board.await_count == 1  # the preview's bytes, not a second credit
    assert r.now_showing.item.index == 0 and r.paused
    r.set_paused(False)
    await r.tick()  # the rotation carries on after the previewed item
    assert r.display.shown[-1] == b"https://cams.example.com/a.jpg"


async def test_show_preview_button(hass, aioclient_mock) -> None:
    e = await _setup(hass, aioclient_mock, [spec_dict(collection(3))])
    btn = _entity_id(hass, e, "button", "show_preview")
    assert btn == "button.living_room_show_preview_on_tv"
    with pytest.raises(HomeAssistantError, match="pick an item in Preview"):
        await hass.services.async_call("button", "press", {"entity_id": btn}, blocking=True)
    runner = e.runtime_data.runners["tv-1"]
    runner.preview = NowShowing(
        runner.spec.source, items_of(runner.spec.source)[1], b"ART1", dt_util.utcnow()
    )
    with patch.object(TVRunner, "show_item", AsyncMock()) as show:
        await hass.services.async_call("button", "press", {"entity_id": btn}, blocking=True)
    show.assert_awaited_once_with(1, b"ART1")


async def test_a_spec_change_drops_the_preview_and_a_pending_pick(hass, make_runner) -> None:
    r = make_runner(parse_spec(spec_dict(collection(3))))
    await r.preview_item(1)
    with _no_restart():
        await r.show_item(2)
        await r.update(parse_spec(spec_dict(collection(2))), r.display)
    assert r.preview is None and r._manual is None


async def test_unpairing_a_phone_drops_a_previewed_photo(hass, aioclient_mock) -> None:
    e = await _setup(hass, aioclient_mock, [spec_dict(GROUP)])
    runner = e.runtime_data.runners["tv-1"]
    pairing = e.runtime_data.pairing
    pairing.open_setup_code("ABCD")
    await pairing.async_pair("ABCD", "Phone", "10.0.0.9")
    src = runner.spec.source
    runner.preview = NowShowing(src, items_of(src)[2], b"PHOTO", dt_util.utcnow())
    runner.now_showing = NowShowing(src, items_of(src)[1], b"WEBCAM", dt_util.utcnow())
    await pairing.async_forget(pairing.phones[0])
    assert runner.preview is None and runner.now_showing.image == b"WEBCAM"


# --- `showing` in the status report --------------------------------------------------


async def test_showing_names_the_item_never_a_url(hass, make_runner) -> None:
    r = make_runner(parse_spec(spec_dict(GROUP)), photos=Photos(SHA))
    kinds = []
    for _ in range(4):
        await r.tick()
        showing = _last_status(r)[4]
        assert valid_showing(showing) == showing
        assert "://" not in showing["name"] and "s3cret" not in str(showing)
        kinds.append((showing["name"], showing["kind"], showing["index"], showing["count"]))
    assert kinds == [
        ("Weather Dashboard", "board", 0, 4),
        ("Beach", "webcam", 1, 4),
        ("IMG_0042", "photo", 2, 4),
        ("Link", "custom_url", 3, 4),
    ]
    since = datetime.fromisoformat(_last_status(r)[4]["since"].replace("Z", "+00:00"))
    assert since.tzinfo == UTC and _last_status(r)[4]["since"].endswith("Z")


async def test_showing_index_is_the_place_in_a_shuffled_rotation(hass, make_runner) -> None:
    spec = parse_spec(spec_dict(collection(5, shuffle=True)))
    r = make_runner(spec)
    await r.tick()
    showing = _last_status(r)[4]
    first = rotation_order(spec.source)[0]
    assert showing["name"] == f"Art {first}" and showing["index"] == 0


async def test_showing_survives_a_restart_while_paused(hass, make_runner, store) -> None:
    spec = parse_spec(spec_dict(collection(3)))
    r = make_runner(spec)
    await r.tick()
    await r.tick()
    r.set_paused(True)
    again = make_runner(spec)  # same store: HA restarted
    assert again.now_showing is None
    held = again.held()
    assert held is not None and held[0].name == "Art 1"
    await again.tick()
    assert _last_status(again)[4]["name"] == "Art 1" and _last_status(again)[4]["paused"]
    assert again.display.shown == []
    # A pin that changed while HA was down: the remembered item is not its item.
    changed = make_runner(parse_spec(spec_dict(collection(4))))
    assert changed.held() is None and changed.showing() is None


async def test_showing_is_null_when_nothing_is_on_the_tv(hass, make_runner) -> None:
    r = make_runner(parse_spec(spec_dict(collection(2))))
    with patch.object(runner_mod, "next_image", AsyncMock(side_effect=sources.SourceError("x"))):
        await r.tick()
    assert _last_status(r)[4] is None


async def test_a_pause_toggle_is_reported_once_rate_limited(hass, make_runner, monkeypatch) -> None:
    monkeypatch.setattr(runner_mod, "SHOWING_REPORT_MIN_SECONDS", 0.05)
    r = make_runner(parse_spec(spec_dict(collection(3))))
    r.set_paused(True)  # nothing reported yet: the first tick will
    assert r._change_task is None
    await r.tick()
    r.set_paused(False)
    await r.tick()
    count = r.client.agent_status.await_count
    r.set_paused(True)
    r.set_paused(False)
    r.set_paused(True)  # three toggles inside the window: one report
    await asyncio.sleep(0.2)
    await hass.async_block_till_done()
    assert r.client.agent_status.await_count == count + 1
    assert _last_status(r)[4]["paused"] is True
    r.set_paused(False)
    await r.tick()  # a tick reports first: the change report is not sent twice
    await asyncio.sleep(0.2)
    await hass.async_block_till_done()
    assert r.client.agent_status.await_count == count + 2


async def test_stopping_cancels_a_pending_change_report(hass, make_runner, monkeypatch) -> None:
    monkeypatch.setattr(runner_mod, "SHOWING_REPORT_MIN_SECONDS", 60)
    r = make_runner(parse_spec(spec_dict(collection(3))))
    await r.tick()
    r.set_paused(True)
    task = r._change_task
    assert task is not None
    await r.stop()
    await hass.async_block_till_done()
    assert task.cancelled() and r.client.agent_status.await_count == 1


# --- the cloud client: validation and the 422 fallback --------------------------------

GOOD = {
    "name": "Weather",
    "kind": "board",
    "index": 2,
    "count": 5,
    "since": "2026-11-08T12:00:00Z",
    "paused": False,
}


@pytest.mark.parametrize(
    "change",
    [
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 65},
        {"name": "tab\there"},
        {"name": "line\nbreak"},
        {"name": "see https://example.com"},
        {"name": None},
        {"kind": "group"},
        {"kind": "local"},
        {"index": 5},
        {"index": -1},
        {"index": 2.0},
        {"index": "2"},
        {"index": True},
        {"count": 0},
        {"paused": 0},
        {"paused": "false"},
        {"since": "2026-11-08 12:00:00"},
        {"since": "2026-11-08T12:00:00+01:00"},
        {"since": "2026-11-08T12:00:00"},
        {"since": 1731067200},
        {"since": "2026-11-08T12:00:00.1234567890Z"},
        {"extra": 1},
    ],
)
def test_valid_showing_refuses_what_the_cloud_refuses(change) -> None:
    assert valid_showing(GOOD) == GOOD
    assert valid_showing({**GOOD, **change}) is None


def test_valid_showing_trims_and_checks_every_key() -> None:
    assert valid_showing({**GOOD, "name": "  Weather  "})["name"] == "Weather"
    assert valid_showing({**GOOD, "since": "2026-11-08T12:00:00.123456789+00:00"}) is not None
    assert valid_showing({**GOOD, "name": "x" * 64}) is not None
    for key in GOOD:
        assert valid_showing({k: v for k, v in GOOD.items() if k != key}) is None
    soon = (datetime.now(UTC) + timedelta(minutes=4)).isoformat().replace("+00:00", "Z")
    late = (datetime.now(UTC) + timedelta(minutes=6)).isoformat().replace("+00:00", "Z")
    assert valid_showing({**GOOD, "since": soon}) is not None
    assert valid_showing({**GOOD, "since": late}) is None
    assert valid_showing(None) is None


async def test_status_carries_showing_or_null(hass, aioclient_mock) -> None:
    client = CloudClient(async_get_clientsession(hass), URL, token="t")
    at = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
    aioclient_mock.post(f"{URL}/ha/agent/status", status=204)
    await client.agent_status("tv-1", True, None, at, GOOD)
    await client.agent_status("tv-1", True, None, at, {**GOOD, "kind": "group"})  # broken
    await client.agent_status("tv-1", True, None, at)
    bodies = [call[2] for call in aioclient_mock.mock_calls]
    assert bodies[0]["showing"] == GOOD
    assert "showing" in bodies[1] and bodies[1]["showing"] is None  # never risk the tick
    assert bodies[2]["showing"] is None  # each report replaces it: null clears


async def test_a_422_to_showing_resends_without_it_until_restart(hass, aioclient_mock) -> None:
    async def older_cloud(method, url, data):
        return AiohttpClientMockResponse(method, url, status=422 if "showing" in data else 204)

    aioclient_mock.post(f"{URL}/ha/agent/status", side_effect=older_cloud)
    aioclient_mock.post("https://other.test/ha/agent/status", status=204)
    session = async_get_clientsession(hass)
    client = CloudClient(session, URL, token="t")
    at = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
    await client.agent_status("tv-1", True, None, at, GOOD)  # 422, then 204 without
    bodies = [call[2] for call in aioclient_mock.mock_calls]
    assert len(bodies) == 2 and bodies[0]["showing"] == GOOD and "showing" not in bodies[1]
    assert {k: bodies[1][k] for k in ("tv_id", "ok", "at")} == {
        "tv_id": "tv-1",
        "ok": True,
        "at": at.isoformat(),
    }
    await client.agent_status("tv-1", False, "TV unreachable", at, GOOD)
    assert len(aioclient_mock.mock_calls) == 3  # one call: showing left out from now on
    assert "showing" not in aioclient_mock.mock_calls[2][2]
    # A new client for the same cloud (an entry reload) remembers it too …
    await CloudClient(session, URL, token="t").agent_status("tv-1", True, None, at, GOOD)
    assert "showing" not in aioclient_mock.mock_calls[3][2]
    # … and another cloud URL still gets `showing`.
    await CloudClient(session, "https://other.test", token="t").agent_status(
        "tv-1", True, None, at, GOOD
    )
    assert aioclient_mock.mock_calls[4][2]["showing"] == GOOD


async def test_a_422_without_showing_is_an_error(hass, aioclient_mock) -> None:
    from custom_components.tvcanvas import api

    api._NO_SHOWING.add(URL)
    aioclient_mock.post(f"{URL}/ha/agent/status", status=422)
    client = CloudClient(async_get_clientsession(hass), URL, token="t")
    with pytest.raises(api.CloudError):
        await client.agent_status("tv-1", True, None, datetime.now(UTC), GOOD)
    assert len(aioclient_mock.mock_calls) == 1


# --- entity setup helpers ------------------------------------------------------------


async def _setup(hass, aioclient_mock, specs):
    aioclient_mock.get(f"{URL}/ha/agent/sync", json={"specs": specs}, headers={"ETag": '"e"'})
    aioclient_mock.post(f"{URL}/ha/agent/status", status=204)
    e = entry(hass)
    with patch("custom_components.tvcanvas.runner.TVRunner.start"):
        await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    return e


def _entity_id(hass, e, domain, key, tv_id="tv-1"):
    return er.async_get(hass).async_get_entity_id(domain, "tvcanvas", f"{e.entry_id}_{tv_id}_{key}")


async def test_a_pick_waits_for_a_frame_that_is_not_showing_tvcanvas(hass, make_runner) -> None:
    display = Display()
    r = make_runner(parse_spec(spec_dict(collection(3))), display=display)
    r.set_paused(True)
    with _no_restart():
        await r.show_item(2)
    display.ready = AsyncMock(return_value="waiting: Watch TV")
    await r.tick()
    assert display.shown == []
    display.ready = AsyncMock(return_value=None)  # back in Art Mode
    await r.tick()
    assert display.shown == [b"https://img.example.com/2.jpg"] and r.paused
    await r.tick()
    assert len(display.shown) == 1
