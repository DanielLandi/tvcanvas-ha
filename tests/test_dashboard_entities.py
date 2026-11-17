"""The per-TV dashboard entities: the "Now showing" select and the "Current image" image."""

from __future__ import annotations

from http import HTTPStatus
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import UNDEFINED
from homeassistant.util import dt as dt_util

from custom_components.tvcanvas import sources
from custom_components.tvcanvas.image import TVImage
from custom_components.tvcanvas.items import (
    OPTION_MAX,
    Item,
    items_of,
    option_names,
    rotation_order,
)
from custom_components.tvcanvas.models import parse_spec
from custom_components.tvcanvas.runner import NowShowing, TVRunner
from custom_components.tvcanvas.storage import StateStore

from .common import URL, entry

SHA = "a" * 64
SHA2 = "b" * 64


def spec_dict(source: dict, tv_id: str = "tv-1", kind: str = "cast", enabled: bool = True):
    return {
        "version": 1,
        "tv": {"id": tv_id, "kind": kind, "host": "10.0.0.3", "name": "Living room"},
        "enabled": enabled,
        "interval_seconds": 300,
        "source": source,
    }


def collection(n: int = 3, shuffle: bool = False) -> dict:
    return {
        "type": "collection",
        "shuffle": shuffle,
        "images": [
            {"url": f"https://img.example.com/{i}.jpg", "title": f"Art {i}", "artist": "A"}
            for i in range(n)
        ],
    }


GROUP = {
    "type": "group",
    "group": {
        "items": [
            {"type": "board", "board": {"type_id": "weather", "params": {"location": "Lisbon"}}},
            {"type": "webcam", "url": "https://cams.example.com/a.jpg", "name": "Beach"},
            {"type": "local", "sha256": SHA, "name": "IMG_0042"},
            {"type": "custom_url", "url": "https://x.example.com/p.jpg?key=s3cret", "name": "Link"},
        ]
    },
}


# --- items --------------------------------------------------------------------


def test_items_name_every_kind_of_source() -> None:
    board = parse_spec(
        spec_dict({"type": "board", "board": {"type_id": "flights", "params": {"a": "LIS"}}})
    )
    assert items_of(board.source) == [Item(0, "board", "Flight Board")]
    link = parse_spec(
        spec_dict({"type": "custom_url", "custom_url": "https://x.example.com/?token=s3cret"})
    )
    assert [i.name for i in items_of(link.source)] == ["Custom link"]  # never the URL
    coll = parse_spec(spec_dict(collection(2)))
    assert items_of(coll.source)[1] == Item(1, "collection", "Art 1", artist="A")
    group = items_of(parse_spec(spec_dict(GROUP)).source)
    assert [(i.type, i.name, i.photo) for i in group] == [
        ("board", "Weather Dashboard", False),
        ("webcam", "Beach", False),
        ("local", "IMG_0042", True),
        ("custom_url", "Link", False),
    ]


@pytest.mark.parametrize(
    ("type_id", "params", "name"),
    [
        ("flights", {"airport": "LIS", "type": "arrivals"}, "Flight Board LIS"),
        ("flights", {"airport": "my-home-airfield"}, "Flight Board"),
        ("sports", {"league": "ucl", "team": "SLB"}, "Sports Scores Champions League"),
        ("sports", {"league": "secret-league"}, "Sports Scores"),
        ("weather", {"location": "38.7223,-9.1393", "units": "metric"}, "Weather Dashboard"),
        ("transit", {"stop_id": "s-home-stop"}, "Transit Departures"),
        ("future_board", {"q": "x"}, "future_board"),
        ("Bad Id!", {}, "Board"),
    ],
)
def test_board_names_show_only_public_labels(type_id, params, name) -> None:
    spec = parse_spec(spec_dict({"type": "board", "board": {"type_id": type_id, "params": params}}))
    assert items_of(spec.source)[0].name == name


def test_secret_looking_board_params_never_appear_in_names() -> None:
    secrets = {
        "api_key": "KEY-123",
        "token": "TOK-456",
        "secret": "SEC-789",
        "url": "https://private.example.com/cal.ics",
        "ical_url": "aHR0cHM6Ly9zZWNyZXQ=",
        "location": "1 Home Street",
        "airport": "KEY",  # an allowlisted param holding a non-airport value is still 3 letters
    }
    items = [
        {"type": "board", "board": {"type_id": t, "params": dict(secrets)}}
        for t in ("flights", "weather", "sports", "stocks", "transit", "news", "calendar")
    ]
    spec = parse_spec(spec_dict({"type": "group", "group": {"items": items}}))
    names = option_names(items_of(spec.source))
    for value in list(secrets.values())[:-1]:
        assert all(value not in n for n in names), value
    assert names == [
        "Flight Board KEY",
        "Weather Dashboard",
        "Sports Scores",
        "Stock Ticker",
        "Transit Departures",
        "Positive News",
        "calendar",
    ]
    twice = parse_spec(spec_dict({"type": "group", "group": {"items": items[1:2] * 2}}))
    assert option_names(items_of(twice.source)) == ["Weather Dashboard", "Weather Dashboard 2"]


def test_option_names_are_unique_short_and_single_line() -> None:
    items = [
        Item(0, "collection", "Sunset"),
        Item(1, "collection", "Sunset"),
        Item(2, "collection", "Sunset 2"),
        Item(3, "collection", "x" * 300),
        Item(4, "collection", "two\nlines"),
    ]
    names = option_names(items)
    assert names[:3] == ["Sunset", "Sunset 2", "Sunset 2 2"]
    assert len(names[3]) == OPTION_MAX and names[4] == "two lines"
    assert len(set(names)) == len(names)


def test_rotation_order_is_the_rotations_own_order() -> None:
    plain = parse_spec(spec_dict(collection(5)))
    assert rotation_order(plain.source) == [0, 1, 2, 3, 4]
    shuffled = parse_spec(spec_dict(collection(5, shuffle=True)))
    order = rotation_order(shuffled.source)
    assert sorted(order) == [0, 1, 2, 3, 4]
    assert order == rotation_order(shuffled.source)  # seeded by the item list: stable
    grouped = parse_spec(spec_dict({**GROUP, "shuffle": True}))
    assert rotation_order(grouped.source) == [0, 1, 2, 3]  # the phone already shuffled it


# --- runner -------------------------------------------------------------------


class Display:
    def __init__(self) -> None:
        self.shown: list[bytes] = []

    async def show(self, jpeg: bytes) -> None:
        self.shown.append(jpeg)


@pytest.fixture
async def make_runner(hass, monkeypatch):
    store = StateStore(hass, "e")
    await store.async_load()

    async def fetch(session, url):
        return url.encode()

    monkeypatch.setattr(sources, "fetch_image", fetch)

    def make(spec, photos=None):
        client = AsyncMock()
        client.board = AsyncMock(return_value=b"BOARD")
        display = Display()
        runner = TVRunner(
            hass, client, None, store, spec, display, lambda: None, lambda: None, photos
        )
        runner._fit = AsyncMock(side_effect=lambda data, orientation: data)
        runner.display = display
        return runner

    return make


@pytest.mark.parametrize("shuffle", [False, True])
async def test_show_item_shows_that_item_then_rotates_on(hass, make_runner, shuffle) -> None:
    spec = parse_spec(spec_dict(collection(5, shuffle=shuffle)))
    runner = make_runner(spec)
    with patch.object(TVRunner, "start", lambda self: None):
        await runner.show_item(3)
    await runner.tick()
    assert runner.display.shown == [b"https://img.example.com/3.jpg"]
    assert runner.now_showing.item == Item(3, "collection", "Art 3", artist="A")
    await runner.tick()
    order = rotation_order(spec.source)
    after = order[(order.index(3) + 1) % 5]
    assert runner.display.shown[-1] == f"https://img.example.com/{after}.jpg".encode()
    assert runner.now_showing.item.index == after


async def test_show_item_restarts_the_loop_for_an_immediate_tick(hass, make_runner) -> None:
    runner = make_runner(parse_spec(spec_dict(collection(3))))
    calls: list[str] = []
    with (
        patch.object(TVRunner, "_stop", AsyncMock(side_effect=lambda: calls.append("stop"))),
        patch.object(TVRunner, "start", lambda self: calls.append("start")),
    ):
        await runner.show_item(2)
        with pytest.raises(ValueError):
            await runner.show_item(3)
    assert calls == ["stop", "start"]


async def test_board_pin_and_group_items_are_remembered(hass, make_runner) -> None:
    board = make_runner(
        parse_spec(spec_dict({"type": "board", "board": {"type_id": "weather", "params": {}}}))
    )
    await board.tick()
    assert (
        board.now_showing.item.name == "Weather Dashboard" and board.now_showing.image == b"BOARD"
    )
    group = make_runner(parse_spec(spec_dict(GROUP)))
    await group.tick()
    await group.tick()
    assert group.now_showing.item == Item(1, "webcam", "Beach")
    assert group.now_showing.source == group.spec.source


async def test_nothing_is_remembered_when_nothing_is_shown(hass, make_runner) -> None:
    runner = make_runner(parse_spec(spec_dict(collection(2))))
    sources_fail = AsyncMock(side_effect=sources.SourceError("nope"))
    with patch("custom_components.tvcanvas.runner.next_image", sources_fail):
        await runner.tick()
    assert runner.now_showing is None


async def test_blocked_says_why_a_pick_would_show_nothing(hass, make_runner, freezer) -> None:
    assert make_runner(parse_spec(spec_dict(collection(2)))).blocked() is None
    off = make_runner(parse_spec(spec_dict(collection(2), enabled=False)))
    assert "off" in off.blocked()
    board = make_runner(
        parse_spec(spec_dict({"type": "board", "board": {"type_id": "weather", "params": {}}}))
    )
    board._client.board = AsyncMock(side_effect=sources.OutOfCredits())
    await board.tick()
    assert board.blocked() == "out of credits"
    freezer.tick(1801)
    assert board.blocked() is None
    group = make_runner(parse_spec(spec_dict(GROUP)))
    group._credits_retry_at = dt_util.utcnow().replace(year=2100)
    assert group.blocked() is None  # a mixed group keeps showing its webcams and links


# --- entities -----------------------------------------------------------------


async def _setup(hass, aioclient_mock, specs):
    aioclient_mock.get(f"{URL}/ha/agent/sync", json={"specs": specs}, headers={"ETag": '"e"'})
    e = entry(hass)
    with patch("custom_components.tvcanvas.runner.TVRunner.start"):
        await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    return e


def _entity_id(hass, e, domain, key, tv_id="tv-1"):
    return er.async_get(hass).async_get_entity_id(domain, "tvcanvas", f"{e.entry_id}_{tv_id}_{key}")


async def _show(hass, e, index, image=b"JPEG", tv_id="tv-1"):
    runner = e.runtime_data.runners[tv_id]
    item = items_of(runner.spec.source)[index]
    if item.photo:  # HA holds the phone photo the image was made from
        await e.runtime_data.photos.async_put(item.sha256, b"original")
    runner.now_showing = NowShowing(runner.spec.source, item, image, dt_util.utcnow())
    e.runtime_data.async_update_listeners()
    await hass.async_block_till_done()


async def test_select_lists_the_items_and_tracks_the_tv(hass, aioclient_mock) -> None:
    e = await _setup(hass, aioclient_mock, [spec_dict(GROUP)])
    sel = _entity_id(hass, e, "select", "now_showing")
    assert sel == "select.living_room_now_showing"
    state = hass.states.get(sel)
    assert state.state == "unknown"
    assert state.attributes["options"] == ["Weather Dashboard", "Beach", "IMG_0042", "Link"]
    assert state.attributes["source"] == "group"
    assert state.attributes["items"][2] == {"name": "IMG_0042", "type": "local", "waiting": True}
    assert "s3cret" not in str(state.attributes)
    await _show(hass, e, 1)
    state = hass.states.get(sel)
    assert state.state == "Beach" and state.attributes["shown_at"]

    # The phone edits the pin: what HA showed belongs to the old list until the next tick.
    runner = e.runtime_data.runners["tv-1"]
    runner.spec = parse_spec(spec_dict(collection(2)))
    e.runtime_data.async_update_listeners()
    await hass.async_block_till_done()
    state = hass.states.get(sel)
    assert state.state == "unknown" and state.attributes["options"] == ["Art 0", "Art 1"]


async def test_select_option_shows_that_item(hass, aioclient_mock) -> None:
    e = await _setup(hass, aioclient_mock, [spec_dict(collection(3))])
    sel = _entity_id(hass, e, "select", "now_showing")
    with patch.object(TVRunner, "show_item", AsyncMock()) as show:
        await hass.services.async_call(
            "select", "select_option", {"entity_id": sel, "option": "Art 2"}, blocking=True
        )
    show.assert_awaited_once_with(2)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "select", "select_option", {"entity_id": sel, "option": "Art 9"}, blocking=True
        )


async def test_select_refuses_while_the_tv_cannot_change(hass, aioclient_mock) -> None:
    e = await _setup(hass, aioclient_mock, [spec_dict(collection(3), enabled=False)])
    sel = _entity_id(hass, e, "select", "now_showing")
    with (
        patch.object(TVRunner, "show_item", AsyncMock()) as show,
        pytest.raises(HomeAssistantError, match="turned this TV off"),
    ):
        await hass.services.async_call(
            "select", "select_option", {"entity_id": sel, "option": "Art 1"}, blocking=True
        )
    show.assert_not_awaited()


async def test_image_shows_what_ha_last_sent(hass, aioclient_mock, hass_client) -> None:
    e = await _setup(hass, aioclient_mock, [spec_dict(collection(3))])
    img = _entity_id(hass, e, "image", "current_image")
    assert img == "image.living_room_current_image"
    assert hass.states.get(img).state == "unknown"
    await _show(hass, e, 0, image=b"\xff\xd8JPEG")
    state = hass.states.get(img)
    assert state.state != "unknown"
    client = await hass_client()
    resp = await client.get(f"/api/image_proxy/{img}")
    assert resp.status == HTTPStatus.OK
    assert await resp.read() == b"\xff\xd8JPEG"
    assert resp.content_type == "image/jpeg"


async def test_image_needs_an_ha_login_or_token(hass, aioclient_mock, hass_client_no_auth) -> None:
    e = await _setup(hass, aioclient_mock, [spec_dict(collection(3))])
    img = _entity_id(hass, e, "image", "current_image")
    await _show(hass, e, 0)
    client = await hass_client_no_auth()
    resp = await client.get(f"/api/image_proxy/{img}")
    assert resp.status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN)
    token = hass.states.get(img).attributes["access_token"]
    resp = await client.get(f"/api/image_proxy/{img}?token={token}")
    assert resp.status == HTTPStatus.OK  # the dashboard's short-lived signed URL


async def test_image_entity_never_fetches_a_url(hass, aioclient_mock) -> None:
    e = await _setup(hass, aioclient_mock, [spec_dict(GROUP)])
    entity = TVImage(e.runtime_data, e.entry_id, e.runtime_data.runners["tv-1"])
    assert entity.image_url is UNDEFINED  # no URL HA's image component would fetch
    assert entity.entity_picture.startswith("/api/image_proxy/")  # HA's authenticated proxy


@pytest.mark.parametrize(("at_home", "served"), [(True, True), (False, False)])
async def test_a_phone_photo_is_shown_only_on_the_home_network(
    hass, aioclient_mock, hass_client, at_home, served
) -> None:
    """Photos never leave the home: not through HA Cloud's remote UI, not to the internet."""
    e = await _setup(hass, aioclient_mock, [spec_dict(GROUP)])
    img = _entity_id(hass, e, "image", "current_image")
    await _show(hass, e, 2, image=b"PHOTO")  # the `local` item
    cloud_calls = aioclient_mock.call_count
    client = await hass_client()
    with patch("custom_components.tvcanvas.image.from_home", return_value=at_home):
        resp = await client.get(f"/api/image_proxy/{img}")
    if served:
        assert resp.status == HTTPStatus.OK and await resp.read() == b"PHOTO"
    else:
        assert resp.status != HTTPStatus.OK
        assert b"PHOTO" not in await resp.read()

    await _show(hass, e, 1, image=b"WEBCAM")  # not a photo: any logged-in viewer
    with patch("custom_components.tvcanvas.image.from_home", return_value=False):
        resp = await client.get(f"/api/image_proxy/{img}")
    assert resp.status == HTTPStatus.OK and await resp.read() == b"WEBCAM"
    assert aioclient_mock.call_count == cloud_calls  # showing it called nothing outside HA


async def test_a_phone_photo_is_not_handed_out_without_a_request(hass, aioclient_mock) -> None:
    """A service or another integration asking for the bytes (no HTTP request) gets none."""
    e = await _setup(hass, aioclient_mock, [spec_dict(GROUP)])
    entity = TVImage(e.runtime_data, e.entry_id, e.runtime_data.runners["tv-1"])
    await _show(hass, e, 2, image=b"PHOTO")
    assert await entity.async_image() is None
    await _show(hass, e, 0, image=b"BOARD")
    assert await entity.async_image() == b"BOARD"


async def test_each_tv_gets_its_own_entities_on_its_device(hass, aioclient_mock) -> None:
    e = await _setup(hass, aioclient_mock, [spec_dict(collection(2)), spec_dict(GROUP, "tv-2")])
    registry = er.async_get(hass)
    for tv_id in ("tv-1", "tv-2"):
        sensor = registry.async_get(
            registry.async_get_entity_id("sensor", "tvcanvas", f"{e.entry_id}_{tv_id}")
        )
        for domain, key in (("select", "now_showing"), ("image", "current_image")):
            ent = registry.async_get(_entity_id(hass, e, domain, key, tv_id))
            assert ent.device_id == sensor.device_id


async def test_the_real_home_check_sees_the_request(hass, aioclient_mock, hass_client) -> None:
    """Unpatched: the proxy request reaches `from_home` (the test client is on loopback)."""
    e = await _setup(hass, aioclient_mock, [spec_dict(GROUP)])
    img = _entity_id(hass, e, "image", "current_image")
    await _show(hass, e, 2, image=b"PHOTO")
    client = await hass_client()
    seen: list[str] = []
    from custom_components.tvcanvas import image as image_platform

    real = image_platform.from_home

    def spy(request):
        seen.append(request.remote)
        return real(request)

    with patch.object(image_platform, "from_home", spy):
        resp = await client.get(f"/api/image_proxy/{img}")
    assert resp.status == HTTPStatus.OK and await resp.read() == b"PHOTO"
    assert seen == ["127.0.0.1"]


# --- the kept image is dropped when it should be (security review, PR #23) ------


async def test_a_spec_change_drops_the_kept_image(hass, make_runner) -> None:
    runner = make_runner(parse_spec(spec_dict(GROUP)))
    await runner.tick()
    assert runner.now_showing is not None
    with patch.object(TVRunner, "start", lambda self: None):
        await runner.update(parse_spec(spec_dict(collection(2))), Display())
    assert runner.now_showing is None


async def test_an_item_leaving_the_list_drops_the_kept_image(hass, aioclient_mock) -> None:
    e = await _setup(hass, aioclient_mock, [spec_dict(GROUP)])
    await _show(hass, e, 2, image=b"PHOTO")
    without_photo = {"type": "group", "group": {"items": GROUP["group"]["items"][:2]}}
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{URL}/ha/agent/sync", json={"specs": [spec_dict(without_photo)]}, headers={"ETag": '"2"'}
    )
    with patch.object(TVRunner, "start", lambda self: None):
        await e.runtime_data.async_refresh()
    assert e.runtime_data.runners["tv-1"].now_showing is None


async def test_a_removed_tv_drops_the_kept_image(hass, aioclient_mock) -> None:
    e = await _setup(hass, aioclient_mock, [spec_dict(GROUP)])
    runner = e.runtime_data.runners["tv-1"]
    await _show(hass, e, 2, image=b"PHOTO")
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/ha/agent/sync", json={"specs": []}, headers={"ETag": '"2"'})
    with patch.object(TVRunner, "stop", AsyncMock()):
        await e.runtime_data.async_refresh()
    assert runner.now_showing is None


async def test_a_deleted_photo_drops_the_kept_image(hass, aioclient_mock) -> None:
    e = await _setup(hass, aioclient_mock, [spec_dict(GROUP)])
    runner = e.runtime_data.runners["tv-1"]
    await _show(hass, e, 2, image=b"PHOTO")
    # Collected (the phone dropped it from the group), as after a spec refresh.
    e.runtime_data.photos._index.pop(SHA)
    e.runtime_data.forget_photo_images(missing_only=True)
    assert runner.now_showing is None

    # Deleted behind HA's back: the image entity notices on the next request.
    await _show(hass, e, 2, image=b"PHOTO")
    e.runtime_data.photos._index.pop(SHA)
    entity = TVImage(e.runtime_data, e.entry_id, runner)
    assert await entity.async_image() is None and runner.now_showing is None


async def test_gc_after_a_sync_drops_the_kept_photo(hass, aioclient_mock) -> None:
    e = await _setup(hass, aioclient_mock, [spec_dict(GROUP)])
    runner = e.runtime_data.runners["tv-1"]
    await _show(hass, e, 2, image=b"PHOTO")
    await _show(hass, e, 1, image=b"WEBCAM")
    runner.now_showing = NowShowing(
        runner.spec.source, items_of(runner.spec.source)[2], b"PHOTO", dt_util.utcnow()
    )
    e.runtime_data.photos._index.pop(SHA)
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{URL}/ha/agent/status", status=204)
    aioclient_mock.get(f"{URL}/ha/agent/sync", status=304, headers={"ETag": '"e"'})
    with patch.object(TVRunner, "start", lambda self: None):
        await e.runtime_data.async_refresh()
    assert runner.now_showing is None


@pytest.mark.parametrize("evicted", [False, True])
async def test_unpairing_a_phone_drops_kept_photos_only(hass, aioclient_mock, evicted) -> None:
    e = await _setup(hass, aioclient_mock, [spec_dict(GROUP), spec_dict(collection(2), "tv-2")])
    pairing = e.runtime_data.pairing
    pairing.open_setup_code("ABCD")
    await pairing.async_pair(pairing._code, "Phone", "10.0.0.9")
    await _show(hass, e, 2, image=b"PHOTO")
    await _show(hass, e, 0, image=b"ART", tv_id="tv-2")
    if evicted:  # five more phones push the first one out
        for i in range(5):
            pairing.open_setup_code("ABCD")
            await pairing.async_pair(pairing._code, f"P{i}", "10.0.0.9")
    else:
        await pairing.async_forget(pairing.phones[0])
    assert e.runtime_data.runners["tv-1"].now_showing is None
    assert e.runtime_data.runners["tv-2"].now_showing.image == b"ART"  # not a photo


async def test_pairing_another_phone_keeps_the_kept_photo(hass, aioclient_mock) -> None:
    e = await _setup(hass, aioclient_mock, [spec_dict(GROUP)])
    await _show(hass, e, 2, image=b"PHOTO")
    pairing = e.runtime_data.pairing
    pairing.open_setup_code("ABCD")
    await pairing.async_pair(pairing._code, "Phone", "10.0.0.9")
    assert e.runtime_data.runners["tv-1"].now_showing.image == b"PHOTO"


async def test_unloading_and_removing_the_entry_drop_every_kept_image(hass, aioclient_mock) -> None:
    e = await _setup(hass, aioclient_mock, [spec_dict(GROUP)])
    runner = e.runtime_data.runners["tv-1"]
    await _show(hass, e, 1, image=b"WEBCAM")
    with patch.object(TVRunner, "stop", AsyncMock()):
        assert await hass.config_entries.async_unload(e.entry_id)
    assert runner.now_showing is None

    assert not hasattr(e, "runtime_data")  # HA keeps no path to the runners after unload

    # Removal also drops a kept image when it can still reach one (defensive).
    from types import SimpleNamespace

    from custom_components.tvcanvas import async_remove_entry

    runner.now_showing = NowShowing(
        runner.spec.source, items_of(runner.spec.source)[2], b"PHOTO", dt_util.utcnow()
    )
    coordinator = SimpleNamespace(runners={"tv-1": runner})
    await async_remove_entry(hass, SimpleNamespace(entry_id=e.entry_id, runtime_data=coordinator))
    assert runner.now_showing is None


# --- whose request it is (security review, PR #23) -----------------------------


def _request(peer: str, headers: dict[str, str]):
    from aiohttp.test_utils import make_mocked_request

    request = make_mocked_request("GET", "/api/image_proxy/x", headers=headers)
    return request.clone(remote=peer)  # what HA's forwarded middleware resolved


@pytest.mark.parametrize(
    ("peer", "headers", "home"),
    [
        ("192.168.1.20", {}, True),
        ("8.8.8.8", {}, False),
        # A spoofed header never makes an outside viewer local: only request.remote counts.
        ("8.8.8.8", {"X-Forwarded-For": "192.168.1.20"}, False),
        ("8.8.8.8", {"X-Real-IP": "192.168.1.20"}, False),
        # A local peer relaying for someone HA can't place (a proxy HA doesn't read): deny.
        ("172.30.33.2", {"CF-Connecting-IP": "203.0.113.9"}, False),
        ("127.0.0.1", {"Forwarded": "for=203.0.113.9"}, False),
        ("192.168.1.20", {"X-Real-IP": "192.168.1.20"}, False),
        ("not-an-ip", {}, False),
    ],
)
async def test_viewer_at_home_reads_only_the_resolved_peer(hass, peer, headers, home) -> None:
    from homeassistant.helpers.http import KEY_HASS, current_request

    from custom_components.tvcanvas.image import viewer_at_home

    request = _request(peer, headers)
    request.app[KEY_HASS] = hass
    token = current_request.set(request)
    try:
        assert viewer_at_home() is home
    finally:
        current_request.reset(token)


async def test_ha_refuses_a_forwarded_header_from_an_untrusted_peer(
    hass, aioclient_mock, hass_client
) -> None:
    """HA's own forwarded middleware (no `use_x_forwarded_for`) answers 400: the photo
    is never served on a header's say-so."""
    e = await _setup(hass, aioclient_mock, [spec_dict(GROUP)])
    img = _entity_id(hass, e, "image", "current_image")
    await _show(hass, e, 2, image=b"PHOTO")
    client = await hass_client()
    resp = await client.get(f"/api/image_proxy/{img}", headers={"X-Forwarded-For": "192.168.1.20"})
    assert resp.status == HTTPStatus.BAD_REQUEST
    assert b"PHOTO" not in await resp.read()
