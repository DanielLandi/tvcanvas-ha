from __future__ import annotations

import aiohttp
import pytest

from custom_components.tvcanvas.api import CloudError, CloudUnreachable, OutOfCredits
from custom_components.tvcanvas.models import parse_spec
from custom_components.tvcanvas.sources import (
    BoardCredits,
    SourceError,
    next_image,
    runs_without_credits,
)


def collection_spec(n: int, shuffle: bool = False, orientation=None, proxied=(1,)):
    return parse_spec(
        {
            "version": 1,
            "tv": {"id": "t", "kind": "frame", "host": "10.0.0.2", "name": "T"},
            "interval_seconds": 60,
            "source": {
                "type": "collection",
                "shuffle": shuffle,
                "orientation": orientation,
                "images": [
                    {"url": f"https://img/{i}.jpg", "title": str(i), "proxied": i in proxied}
                    for i in range(n)
                ],
            },
        }
    )


def group_spec(items, shuffle=False):
    return parse_spec(
        {
            "version": 1,
            "tv": {"id": "t", "kind": "frame", "host": "10.0.0.2", "name": "T"},
            "interval_seconds": 60,
            "source": {"type": "group", "group": {"items": items}, "shuffle": shuffle},
        }
    )


def board_item(type_id, **params):
    return {"type": "board", "board": {"type_id": type_id, "params": params}}


def link_item(kind, n):
    return {"type": kind, "url": f"https://img/{kind}{n}.jpg", "name": f"{kind}{n}"}


class FakeClient:
    def __init__(self, board=b"BOARD", fail=None, proxied_fail=None):
        self.board_bytes, self.fail, self.proxied = board, fail, []
        self.proxied_fail = proxied_fail
        self.boards = []

    async def board(self, type_id, params):
        self.boards.append((type_id, params))
        if self.fail:
            raise self.fail
        return self.board_bytes

    async def proxied_image(self, image_id):
        self.proxied.append(image_id)
        if self.proxied_fail:
            raise self.proxied_fail
        return f"P:{image_id}".encode()


async def identity_fit(data, orientation):
    return data


@pytest.fixture
def fetched(monkeypatch):
    calls = []

    async def fake_fetch(session, url):
        calls.append(url)
        return url.encode()

    monkeypatch.setattr("custom_components.tvcanvas.sources.fetch_image", fake_fetch)
    return calls


async def test_collection_rotates_and_wraps(fetched) -> None:
    spec, rot = collection_spec(3), {}
    labels = [(await next_image(spec, rot, FakeClient(), None, identity_fit))[1] for _ in range(4)]
    assert labels == ["0", "1", "2", "0"]
    assert rot["pos"] == 1


async def test_proxied_items_go_through_cloud(fetched) -> None:
    spec, rot = collection_spec(2), {"key": None}
    client = FakeClient()
    await next_image(spec, rot, client, None, identity_fit)
    data, _ = await next_image(spec, rot, client, None, identity_fit)
    assert data == b"P:https://img/1.jpg" and client.proxied == ["https://img/1.jpg"]


async def test_new_item_list_resets_position(fetched) -> None:
    rot = {}
    await next_image(collection_spec(3), rot, FakeClient(), None, identity_fit)
    await next_image(collection_spec(3), rot, FakeClient(), None, identity_fit)
    _, label = await next_image(collection_spec(4), rot, FakeClient(), None, identity_fit)
    assert label == "0"


async def test_shuffle_order_is_stable(fetched) -> None:
    spec = collection_spec(6, shuffle=True)
    a, b = {}, {}
    seq_a = [(await next_image(spec, a, FakeClient(), None, identity_fit))[1] for _ in range(6)]
    seq_b = [(await next_image(spec, b, FakeClient(), None, identity_fit))[1] for _ in range(6)]
    assert seq_a == seq_b and sorted(seq_a) == [str(i) for i in range(6)]


async def test_skips_items_the_fitter_rejects(fetched) -> None:
    async def only_two(data, orientation):
        return data if data.endswith(b"2.jpg") else None

    _, label = await next_image(collection_spec(4), {}, FakeClient(), None, only_two)
    assert label == "2"


async def test_all_items_rejected_raises_once_without_spinning(fetched) -> None:
    async def none(data, orientation):
        return None

    rot, client = {}, FakeClient()
    with pytest.raises(SourceError, match="no usable images"):
        await next_image(collection_spec(3), rot, client, None, none)
    # each item tried exactly once (item 1 is proxied, so it goes via the cloud)
    assert len(fetched) + len(client.proxied) == 3


async def test_tick_tries_at_most_five_items_then_moves_on(fetched) -> None:
    async def none(data, orientation):
        return None

    rot, client = {}, FakeClient()
    with pytest.raises(SourceError, match="no usable image in this batch"):
        await next_image(collection_spec(12), rot, client, None, none)
    assert len(fetched) + len(client.proxied) == 5
    assert rot["pos"] == 5
    with pytest.raises(SourceError, match="no usable image in this batch"):
        await next_image(collection_spec(12), rot, client, None, none)
    assert fetched[-5:] == [f"https://img/{i}.jpg" for i in range(5, 10)]
    assert rot["pos"] == 10


async def test_cloud_unreachable_reports_when_nothing_else_shows(fetched) -> None:
    async def only_proxied(data, orientation):
        return data if data.startswith(b"P:") else None

    rot = {}
    client = FakeClient(proxied_fail=CloudUnreachable("cloud unreachable: boom"))
    with pytest.raises(SourceError, match="cloud unreachable"):
        await next_image(collection_spec(4), rot, client, None, only_proxied)
    # item 1 (proxied) hit a dead cloud; the direct items were still tried
    assert fetched == ["https://img/0.jpg", "https://img/2.jpg", "https://img/3.jpg"]
    assert client.proxied == ["https://img/1.jpg"]
    assert rot["pos"] == 1  # the proxied item is retried first next tick


async def test_cloud_down_shows_the_next_direct_image(fetched) -> None:
    client = FakeClient(proxied_fail=CloudUnreachable("cloud unreachable: boom"))
    spec = collection_spec(3)
    rot = {"key": spec.source.items_key, "pos": 1}
    _, label = await next_image(spec, rot, client, None, identity_fit)
    assert label == "2" and client.proxied == ["https://img/1.jpg"]
    assert rot["pos"] == 0


async def test_cloud_down_is_asked_once_per_tick(fetched) -> None:
    client = FakeClient(proxied_fail=CloudUnreachable("cloud unreachable: boom"))
    spec = collection_spec(4, proxied=(0, 1, 2))
    rot = {"key": spec.source.items_key, "pos": 0}
    _, label = await next_image(spec, rot, client, None, identity_fit)
    assert label == "3" and client.proxied == ["https://img/0.jpg"]
    assert fetched == ["https://img/3.jpg"]


async def test_other_cloud_errors_keep_trying(fetched) -> None:
    client = FakeClient(proxied_fail=CloudError("cloud answered 404"))
    spec = collection_spec(3)
    rot = {"key": spec.source.items_key, "pos": 1}
    _, label = await next_image(spec, rot, client, None, identity_fit)
    assert label == "2" and client.proxied == ["https://img/1.jpg"]


async def test_board_errors_propagate() -> None:
    spec = parse_spec(
        {
            "version": 1,
            "tv": {"id": "t", "kind": "cast", "host": "10.0.0.3", "name": "T"},
            "interval_seconds": 300,
            "source": {"type": "board", "board": {"type_id": "weather", "params": {}}},
        }
    )
    with pytest.raises(OutOfCredits):
        await next_image(spec, {}, FakeClient(fail=OutOfCredits()), None, identity_fit)
    data, label = await next_image(spec, {}, FakeClient(), None, identity_fit)
    assert data == b"BOARD" and label == "weather"


# --- group pins ---------------------------------------------------------------


async def test_group_steps_through_items_in_the_phones_order(fetched) -> None:
    items = [
        board_item("weather", location="Lisbon"),
        link_item("webcam", 1),
        link_item("custom_url", 2),
    ]
    # shuffle=True: the phone already applied it, HA must not reorder.
    spec = group_spec(items, shuffle=True)
    client = FakeClient()
    rot, labels = {}, []
    for _ in range(4):
        data, label = await next_image(spec, rot, client, None, identity_fit)
        labels.append(label)
    assert labels == ["weather", "webcam1", "custom_url2", "weather"]
    assert client.boards == [("weather", {"location": "Lisbon"})] * 2
    assert fetched == ["https://img/webcam1.jpg", "https://img/custom_url2.jpg"]
    assert rot["pos"] == 1


async def test_group_shows_items_unfiltered(fetched) -> None:
    seen = []

    async def fit(data, orientation):
        seen.append(orientation)
        return data

    spec = group_spec([board_item("weather"), link_item("custom_url", 1)])
    rot = {}
    await next_image(spec, rot, FakeClient(), None, fit)
    await next_image(spec, rot, FakeClient(), None, fit)
    assert seen == [None, None]


async def test_group_board_out_of_credits_propagates_without_a_credits_tracker(fetched) -> None:
    spec = group_spec([board_item("weather"), link_item("webcam", 1)])
    rot = {}
    with pytest.raises(OutOfCredits):
        await next_image(spec, rot, FakeClient(fail=OutOfCredits()), None, identity_fit)
    assert rot["pos"] == 0  # the board is retried first once credits are back


async def test_all_boards_group_out_of_credits_still_propagates(fetched) -> None:
    # Nothing else to show: the runner pauses the whole TV, as for a board pin.
    spec = group_spec([board_item("weather"), board_item("crypto")])
    rot, credits = {}, BoardCredits()
    with pytest.raises(OutOfCredits):
        await next_image(spec, rot, FakeClient(fail=OutOfCredits()), None, identity_fit, credits)
    assert rot["pos"] == 0 and not credits.hit


async def test_group_402_moves_on_to_the_next_link_in_the_same_tick(fetched) -> None:
    spec = group_spec(
        [
            board_item("weather"),
            board_item("crypto"),
            link_item("webcam", 1),
            link_item("custom_url", 2),
        ]
    )
    client, rot, credits = FakeClient(fail=OutOfCredits()), {}, BoardCredits()
    _, label = await next_image(spec, rot, client, None, identity_fit, credits)
    # One 402 is enough: the group's other boards are skipped in the same tick.
    assert label == "webcam1" and client.boards == [("weather", {})]
    assert credits.hit and not credits.board_shown
    assert rot["pos"] == 3


async def test_group_with_boards_paused_skips_them_without_asking(fetched) -> None:
    # Six boards before the webcam: paused boards don't use up the 5 tries.
    items = [board_item(f"b{i}") for i in range(6)] + [link_item("webcam", 1)]
    spec = group_spec(items)
    client, rot, credits = FakeClient(), {}, BoardCredits(paused=True)
    _, label = await next_image(spec, rot, client, None, identity_fit, credits)
    assert label == "webcam1" and client.boards == [] and fetched == ["https://img/webcam1.jpg"]
    assert not credits.hit and not credits.board_shown
    assert rot["pos"] == 0


async def test_group_with_boards_paused_and_its_links_failing(monkeypatch) -> None:
    async def down(session, url):
        raise SourceError(f"{url} answered 503")

    monkeypatch.setattr("custom_components.tvcanvas.sources.fetch_image", down)
    spec = group_spec([board_item("weather"), link_item("webcam", 1)])
    client = FakeClient()
    with pytest.raises(SourceError, match=r"no usable images \(1 tried\).*503"):
        await next_image(spec, {}, client, None, identity_fit, BoardCredits(paused=True))
    assert client.boards == []


async def test_group_records_when_a_board_was_shown(fetched) -> None:
    spec = group_spec([board_item("weather"), link_item("webcam", 1)])
    rot = {}
    credits = BoardCredits()
    await next_image(spec, rot, FakeClient(), None, identity_fit, credits)
    assert credits.board_shown
    credits = BoardCredits()
    await next_image(spec, rot, FakeClient(), None, identity_fit, credits)
    assert not credits.board_shown


async def test_paused_credits_do_not_skip_a_board_pin() -> None:
    # Board pins pause in the runner (no tick at all); next_image never skips them.
    spec = parse_spec(
        {
            "version": 1,
            "tv": {"id": "t", "kind": "frame", "host": "10.0.0.2", "name": "T"},
            "interval_seconds": 60,
            "source": {"type": "board", "board": {"type_id": "weather", "params": {}}},
        }
    )
    credits = BoardCredits(paused=True)
    data, _ = await next_image(spec, {}, FakeClient(), None, identity_fit, credits)
    assert data == b"BOARD" and credits.board_shown


def test_runs_without_credits() -> None:
    assert runs_without_credits(group_spec([board_item("weather"), link_item("webcam", 1)]))
    assert not runs_without_credits(group_spec([board_item("weather"), board_item("crypto")]))
    assert not runs_without_credits(collection_spec(2))


async def test_group_skips_a_failed_item_in_the_same_tick(fetched) -> None:
    spec = group_spec([board_item("weather"), link_item("webcam", 1)])
    rot = {}
    data, label = await next_image(
        spec, rot, FakeClient(fail=CloudError("cloud answered 404")), None, identity_fit
    )
    assert label == "webcam1" and rot["pos"] == 0


async def test_group_cloud_down_still_shows_direct_items(fetched) -> None:
    spec = group_spec([board_item("weather"), board_item("crypto"), link_item("webcam", 1)])
    client = FakeClient(fail=CloudUnreachable("cloud unreachable: boom"))
    rot = {}
    _, label = await next_image(spec, rot, client, None, identity_fit)
    # The cloud is asked once per tick: the second board is skipped.
    assert label == "webcam1" and client.boards == [("weather", {})]


async def test_group_of_boards_with_the_cloud_down(fetched) -> None:
    spec = group_spec([board_item("weather"), board_item("crypto")])
    client = FakeClient(fail=CloudUnreachable("cloud unreachable: boom"))
    rot = {"key": spec.source.items_key, "pos": 1}
    with pytest.raises(SourceError, match="cloud unreachable"):
        await next_image(spec, rot, client, None, identity_fit)
    assert rot["pos"] == 1


async def test_group_restarts_when_its_items_change(fetched) -> None:
    rot = {}
    spec = group_spec([link_item("webcam", 1), link_item("webcam", 2)])
    await next_image(spec, rot, FakeClient(), None, identity_fit)
    assert rot["pos"] == 1
    spec = group_spec([link_item("webcam", 1), link_item("webcam", 2), board_item("weather")])
    _, label = await next_image(spec, rot, FakeClient(), None, identity_fit)
    assert label == "webcam1"


async def test_fetch_image_limits(hass, aioclient_mock) -> None:
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from custom_components.tvcanvas.sources import fetch_image

    session = async_get_clientsession(hass)
    aioclient_mock.get("https://x/ok.jpg", content=b"IMG", headers={"Content-Type": "image/jpeg"})
    assert await fetch_image(session, "https://x/ok.jpg") == b"IMG"
    aioclient_mock.get("https://x/page", content=b"<html>", headers={"Content-Type": "text/html"})
    with pytest.raises(SourceError):
        await fetch_image(session, "https://x/page")
    aioclient_mock.get("https://x/missing.jpg", status=404)
    with pytest.raises(SourceError):
        await fetch_image(session, "https://x/missing.jpg")


async def test_fetch_image_rejects_non_public_url(hass, aioclient_mock) -> None:
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from custom_components.tvcanvas.sources import fetch_image

    session = async_get_clientsession(hass)
    with pytest.raises(SourceError):
        await fetch_image(session, "https://127.0.0.1/a.jpg")
    assert aioclient_mock.call_count == 0


async def test_redirect_to_private_host_is_refused(hass, aioclient_mock) -> None:
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from custom_components.tvcanvas.sources import fetch_image

    session = async_get_clientsession(hass)
    aioclient_mock.get(
        "https://x/a.jpg", status=302, headers={"Location": "https://127.0.0.1/secret"}
    )
    aioclient_mock.get(
        "https://127.0.0.1/secret", content=b"SECRET", headers={"Content-Type": "image/jpeg"}
    )
    with pytest.raises(SourceError):
        await fetch_image(session, "https://x/a.jpg")
    assert [str(c[1]) for c in aioclient_mock.mock_calls] == ["https://x/a.jpg"]


async def test_single_public_redirect_is_followed(hass, aioclient_mock) -> None:
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from custom_components.tvcanvas.sources import fetch_image

    session = async_get_clientsession(hass)
    aioclient_mock.get("https://x/a.jpg", status=302, headers={"Location": "/b.jpg"})
    aioclient_mock.get("https://x/b.jpg", content=b"IMG", headers={"Content-Type": "image/jpeg"})
    assert await fetch_image(session, "https://x/a.jpg") == b"IMG"


async def test_too_many_redirects_refused(hass, aioclient_mock) -> None:
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from custom_components.tvcanvas.sources import fetch_image

    session = async_get_clientsession(hass)
    for i in range(5):
        aioclient_mock.get(f"https://x/{i}", status=302, headers={"Location": f"https://x/{i + 1}"})
    with pytest.raises(SourceError):
        await fetch_image(session, "https://x/0")
    assert aioclient_mock.call_count == 4


async def test_three_redirects_then_image_succeeds(hass, aioclient_mock) -> None:
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from custom_components.tvcanvas.sources import fetch_image

    session = async_get_clientsession(hass)
    for i in range(3):
        aioclient_mock.get(f"https://x/{i}", status=302, headers={"Location": f"https://x/{i + 1}"})
    aioclient_mock.get("https://x/3", content=b"IMG", headers={"Content-Type": "image/jpeg"})
    assert await fetch_image(session, "https://x/0") == b"IMG"


async def test_redirect_to_http_refused(hass, aioclient_mock) -> None:
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from custom_components.tvcanvas.sources import fetch_image

    session = async_get_clientsession(hass)
    aioclient_mock.get("https://x/a.jpg", status=302, headers={"Location": "http://x/b.jpg"})
    with pytest.raises(SourceError):
        await fetch_image(session, "https://x/a.jpg")
    assert aioclient_mock.call_count == 1


async def test_declared_oversize_refused_without_reading(hass, aioclient_mock, monkeypatch) -> None:
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from custom_components.tvcanvas.sources import fetch_image

    monkeypatch.setattr("custom_components.tvcanvas.sources.MAX_IMAGE_BYTES", 10)
    session = async_get_clientsession(hass)
    aioclient_mock.get(
        "https://x/big.jpg",
        content=b"x",
        headers={"Content-Type": "image/jpeg", "Content-Length": "11"},
    )
    with pytest.raises(SourceError, match="larger"):
        await fetch_image(session, "https://x/big.jpg")


@pytest.fixture
async def stream_server(monkeypatch, socket_enabled):
    """A real local server. 127.0.0.1 is rejected by the public-host check, so these tests
    replace that check with a pass-through; the streaming logic is what is under test."""
    import aiohttp
    import yarl
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    monkeypatch.setattr(
        "custom_components.tvcanvas.sources.public_https_url",
        lambda u: yarl.URL(u) if isinstance(u, str) else u,
    )

    async def image(request):
        total = int(request.query["size"])
        resp = web.StreamResponse(headers={"Content-Type": "image/jpeg"})
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        sent = 0
        while sent < total:
            piece = min(4096, total - sent)
            await resp.write(b"a" * piece)
            sent += piece
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_get("/img", image)
    server = TestServer(app)
    await server.start_server()
    async with aiohttp.ClientSession() as session:
        yield session, str(server.make_url("/img"))
    await server.close()


async def test_chunked_body_is_returned_complete(stream_server) -> None:
    from custom_components.tvcanvas.sources import fetch_image

    session, url = stream_server
    assert len(await fetch_image(session, f"{url}?size=1000000")) == 1_000_000


async def test_chunked_body_over_cap_refused(stream_server, monkeypatch) -> None:
    from custom_components.tvcanvas.sources import fetch_image

    monkeypatch.setattr("custom_components.tvcanvas.sources.MAX_IMAGE_BYTES", 100_000)
    session, url = stream_server
    with pytest.raises(SourceError, match="larger"):
        await fetch_image(session, f"{url}?size=1000000")


async def test_errors_never_carry_query_strings(hass, aioclient_mock) -> None:
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from custom_components.tvcanvas.sources import fetch_image

    session = async_get_clientsession(hass)
    url = "https://x.example/cam.jpg?key=SECRET"
    aioclient_mock.get(url, status=404)
    with pytest.raises(SourceError) as info:
        await fetch_image(session, url)
    assert "SECRET" not in str(info.value) and "x.example/cam.jpg" in str(info.value)

    aioclient_mock.clear_requests()
    aioclient_mock.get(url, exc=aiohttp.InvalidURL(url))
    with pytest.raises(SourceError) as info:
        await fetch_image(session, url)
    assert "SECRET" not in str(info.value)

    aioclient_mock.clear_requests()
    aioclient_mock.get(url, status=302, headers={"Location": "https://10.0.0.1/x?token=SECRET"})
    with pytest.raises(SourceError) as info:
        await fetch_image(session, url)
    assert "SECRET" not in str(info.value)
