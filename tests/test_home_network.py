"""Home network only (#14, #15): HA sends images, and the phone's photos among them, only
to a TV on the home network, and the Cast image view answers only the home network.

Trust model: a hacked TVCanvas Cloud must never get at the phone's photos. The cloud names
each TV's address, so HA decides for itself whether that address is in the home. If a test
here fails because a display grew a new way to connect, that path needs the owner's review
against the trust model, not a quick fix to the test.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components import persistent_notification
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.setup import async_setup_component
from PIL import Image
from samsungtvws import SamsungTVArt

from custom_components.tvcanvas.display_cast import CastDisplay, _play_sync
from custom_components.tvcanvas.display_frame import FrameDisplay, _art, _show_sync
from custom_components.tvcanvas.display_view import ImageCache, ImageView
from custom_components.tvcanvas.displays import DisplayError, HostRefused, home_literal, tv_address
from custom_components.tvcanvas.models import SpecError, parse_spec
from custom_components.tvcanvas.runner import TVRunner
from custom_components.tvcanvas.storage import StateStore
from custom_components.tvcanvas.urlsafety import (
    TV_HOST_REFUSED,
    NotHomeNetwork,
    home_address,
    resolve_home_address,
)

from .common import URL, entry

PUBLIC_V4 = "93.184.215.14"
PUBLIC_V6 = "2001:4860:4860::8888"
PUBLIC = [PUBLIC_V4, "8.8.8.8", PUBLIC_V6]
FRAME_CTOR = "custom_components.tvcanvas.display_frame.SamsungTVArt"
CAST_CTOR = "custom_components.tvcanvas.display_cast.pychromecast.get_chromecast_from_host"


# --- what counts as the home network ---------------------------------------------------


@pytest.mark.parametrize(
    ("host", "address"),
    [
        ("10.0.0.2", "10.0.0.2"),  # RFC 1918 private
        ("172.16.0.1", "172.16.0.1"),
        ("172.31.255.254", "172.31.255.254"),
        ("192.168.0.21", "192.168.0.21"),
        ("169.254.1.1", "169.254.1.1"),  # IPv4 link-local
        ("fe80::1", "fe80::1"),  # IPv6 link-local
        ("fe80::1%eth0", "fe80::1%eth0"),
        ("fd12:3456:789a::5", "fd12:3456:789a::5"),  # IPv6 unique local
        ("fc00::1", "fc00::1"),
        ("::ffff:192.168.1.5", "192.168.1.5"),  # IPv4-mapped: judged and used as IPv4
    ],
)
def test_private_link_local_and_unique_local_addresses_are_home(host, address) -> None:
    assert home_address(host) == address
    assert home_literal(host) == address


@pytest.mark.parametrize(
    "host",
    [
        *PUBLIC,
        "::ffff:8.8.8.8",  # a public IPv4 address inside an IPv6 one
        "2002:c0a8:101::",  # 6to4
        "64:ff9b::a00:2",  # NAT64
        "127.0.0.1",  # loopback: HA itself, never a TV
        "::1",
        "::ffff:127.0.0.1",
        "0.0.0.0",
        "::",
        "100.64.0.1",  # shared space (CGNAT, Tailscale)
        "172.32.0.1",  # just outside 172.16/12
        "192.0.2.1",  # documentation and benchmark ranges: not public, not a home either
        "198.18.0.1",
        "224.0.0.1",
        "ff02::1",
        "255.255.255.255",
        "tv.example.com",  # a name is not an address: it has to be resolved first
        "192.168.1.5:8002",
        "",
        None,
        8,
    ],
)
def test_everything_else_is_not_home(host) -> None:
    assert home_address(host) is None
    with pytest.raises(HostRefused, match=TV_HOST_REFUSED):
        home_literal(host)


@pytest.mark.parametrize("host", ["10.0.0.2", "169.254.1.1", "fe80::1", "fd00::5"])
async def test_a_home_literal_needs_no_lookup(host, fake_dns) -> None:
    assert await resolve_home_address(host) == host
    assert await tv_address(host) == host
    assert fake_dns.lookups == []


@pytest.mark.parametrize("host", [*PUBLIC, "127.0.0.1", "100.64.0.1"])
async def test_a_literal_outside_the_home_is_refused_without_a_lookup(host, fake_dns) -> None:
    with pytest.raises(NotHomeNetwork):
        await resolve_home_address(host)
    with pytest.raises(HostRefused, match=TV_HOST_REFUSED):
        await tv_address(host)
    assert fake_dns.lookups == []


async def test_a_host_name_is_pinned_to_the_home_address_it_resolves_to(fake_dns) -> None:
    fake_dns.answers["frame.lan"] = ["192.168.1.50", "fd00::50"]
    assert await tv_address("frame.lan") == "192.168.1.50"


@pytest.mark.parametrize(
    "answers",
    [
        [PUBLIC_V4],
        [PUBLIC_V6],
        ["192.168.1.50", PUBLIC_V4],  # ANY answer outside the home refuses the name
        [PUBLIC_V6, "192.168.1.50"],
        ["127.0.0.1"],
        [],
    ],
)
async def test_a_host_name_resolving_outside_the_home_is_refused(fake_dns, answers) -> None:
    fake_dns.answers["tv.evil.example"] = answers
    with pytest.raises(HostRefused, match=TV_HOST_REFUSED):
        await tv_address("tv.evil.example")


async def test_a_host_name_that_does_not_resolve_is_refused(monkeypatch) -> None:
    class Failing:
        async def resolve(self, host, port=0, family=0):
            raise OSError("no such host")

        async def close(self) -> None:
            pass

    monkeypatch.setattr("custom_components.tvcanvas.urlsafety.ThreadedResolver", Failing)
    with pytest.raises(HostRefused, match=TV_HOST_REFUSED):
        await tv_address("gone.lan")


def test_the_spec_parser_still_wants_an_ip_literal() -> None:
    """Names never reach a display today (the cloud contract: tv.host is an IP literal);
    the lookup above is the second line of defence if that ever changes."""
    raw = frame_spec("tv-1", "frame.lan")
    with pytest.raises(SpecError, match="IP literal"):
        parse_spec(raw)


# --- Samsung Frame -----------------------------------------------------------------------


@pytest.fixture
async def store(hass):
    s = StateStore(hass, "entry1")
    await s.async_load()
    return s


def frame_display(hass, store, host, notices=None):
    notices = notices if notices is not None else []
    return FrameDisplay(hass, store, "tv-1", host, True, notices.append)


@pytest.mark.parametrize("host", PUBLIC)
async def test_a_frame_on_a_public_address_is_never_paired_with_or_sent_anything(
    hass, store, host
) -> None:
    notices: list[bool] = []
    disp = frame_display(hass, store, host, notices)
    with patch(FRAME_CTOR) as ctor:
        with pytest.raises(HostRefused, match=TV_HOST_REFUSED):
            await disp.ready()
        with pytest.raises(HostRefused, match=TV_HOST_REFUSED):
            await disp.show(b"PHOTO")
    ctor.assert_not_called()
    assert notices == []  # no "Approve TVCanvas-HA on your TV" for an address HA refuses
    assert store.frame_token(host) is None


async def test_release_never_connects_to_a_public_address(hass, store) -> None:
    store.frame("tv-1").update(current="MY_F0001")  # as if stored before this rule existed
    disp = frame_display(hass, store, PUBLIC_V4)
    with patch(FRAME_CTOR) as ctor:
        disp.release()
        await hass.async_block_till_done()
    ctor.assert_not_called()


@pytest.mark.parametrize("host", [*PUBLIC, "127.0.0.1", "frame.lan"])
def test_no_frame_connection_can_be_built_for_anything_but_a_home_address(host) -> None:
    """`_art` is where every Frame connection starts; it takes an address, never a name."""
    with patch(FRAME_CTOR) as ctor, pytest.raises(HostRefused):
        _art(host, None)
    ctor.assert_not_called()


async def test_a_frame_host_name_is_connected_to_by_its_checked_address(
    hass, store, fake_dns
) -> None:
    # A second lookup would answer a public address (DNS rebinding): there is none.
    fake_dns.answers["frame.lan"] = [["192.168.1.50"], [PUBLIC_V4]]
    art = MagicMock()
    art.token = "T1"
    art.upload.return_value = "MY_F0002"
    with patch(FRAME_CTOR, return_value=art) as ctor:
        await frame_display(hass, store, "frame.lan").show(b"PHOTO")
    assert [c.args[0] for c in ctor.call_args_list] == ["192.168.1.50"]
    assert fake_dns.lookups == ["frame.lan"]


async def test_a_frame_host_name_that_rebinds_to_a_public_address_is_refused(
    hass, store, fake_dns
) -> None:
    fake_dns.answers["frame.lan"] = [["192.168.1.50"], [PUBLIC_V4]]
    art = MagicMock()
    art.token = "T1"
    art.upload.return_value = "MY_F0002"
    disp = frame_display(hass, store, "frame.lan")
    with patch(FRAME_CTOR, return_value=art) as ctor:
        await disp.show(b"PHOTO")
        with pytest.raises(HostRefused):
            await disp.show(b"PHOTO")
    assert [c.args[0] for c in ctor.call_args_list] == ["192.168.1.50"]


class FakeWebSocket:
    """What websocket-client hands samsungtvws once it is connected, after any redirects:
    a socket to *peer* on which the TV says it is connected and ready."""

    connected = True

    def __init__(self, peer: str) -> None:
        self.sock = MagicMock()
        self.sock.getpeername.return_value = (peer, 8002)
        self.frames = [
            json.dumps({"event": "ms.channel.connect", "data": {"token": "T9"}}),
            json.dumps({"event": "ms.channel.ready"}),
        ]
        self.sent: list[object] = []
        self.closed = False

    def recv(self) -> str:
        return self.frames.pop(0)

    def send(self, payload: object) -> None:
        self.sent.append(payload)

    send_binary = send

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("peer", [*PUBLIC, "127.0.0.1"])
def test_a_frame_connection_redirected_outside_the_home_is_dropped_unused(peer) -> None:
    """websocket-client follows redirects, so the peer isn't always the host asked for."""
    sockets: list[FakeWebSocket] = []

    def connect(url, *args, **kwargs):
        assert "//192.168.1.50:8002/" in url  # HA itself asked for the home TV
        sockets.append(FakeWebSocket(peer))
        return sockets[-1]

    art = _art("192.168.1.50", "T0")
    with patch("samsungtvws.connection.websocket.create_connection", side_effect=connect):
        with pytest.raises(HostRefused, match="moved the connection"):
            art.get_artmode()  # any request opens the connection first
        with pytest.raises(HostRefused, match="moved the connection"):
            art.upload(b"PHOTO", matte="none", portrait_matte="none", file_type="jpg")
    assert len(sockets) == 2 and art.connection is None
    assert all(ws.sent == [] and ws.closed for ws in sockets)


def test_a_frame_connection_to_the_home_tv_is_used() -> None:
    ws = FakeWebSocket("192.168.1.50")
    art = _art("192.168.1.50", "T0")
    with patch("samsungtvws.connection.websocket.create_connection", return_value=ws):
        assert art.open() is ws
    assert not ws.closed and art.token == "T9"


def test_a_frame_connection_whose_peer_cannot_be_read_is_dropped() -> None:
    ws = FakeWebSocket("192.168.1.50")
    ws.sock.getpeername.side_effect = OSError("not connected")
    art = _art("192.168.1.50", "T0")
    with (
        patch("samsungtvws.connection.websocket.create_connection", return_value=ws),
        pytest.raises(OSError, match="could not read the address"),
    ):
        art.open()
    assert ws.sent == [] and ws.closed


def real_art(conn_ip: object):
    """samsungtvws' own SamsungTVArt, as `_art` builds it, with the websocket answers
    scripted: the TV says "send the image to conn_ip"."""
    art = _art("192.168.1.50", "T0")
    conn_info = {"ip": conn_ip, "port": 51234, "key": "k", "secured": False}
    art.get_api_version = MagicMock(return_value="4.3.4.0")
    art._send_art_request = MagicMock(return_value={"conn_info": json.dumps(conn_info)})
    art._wait_for_d2d = MagicMock(return_value={"content_id": "MY_F0002"})
    return art


@pytest.mark.parametrize("conn_ip", [*PUBLIC, "127.0.0.1", "tv.evil.example", None])
def test_the_upload_socket_is_never_opened_to_an_address_outside_the_home(conn_ip) -> None:
    """upload() sends the bytes over a second socket, to the address the TV names."""
    art = real_art(conn_ip)
    with (
        patch("samsungtvws.art.art.socket.socket") as sock,
        pytest.raises(HostRefused, match="not on the home network"),
    ):
        art.upload(b"PHOTO", matte="none", portrait_matte="none", file_type="jpg")
    sock.assert_not_called()
    art._wait_for_d2d.assert_not_called()


def test_the_upload_socket_opens_to_a_home_address() -> None:
    """Also proves samsungtvws still opens its upload socket through `_open_d2d_socket`:
    if a release stops doing so, this fails and the guard needs a new hook."""
    art = real_art("192.168.1.50")
    with patch("samsungtvws.art.art.socket.socket") as sock:
        cid = art.upload(b"PHOTO", matte="none", portrait_matte="none", file_type="jpg")
    assert cid == "MY_F0002"
    sock.return_value.connect.assert_called_once_with(("192.168.1.50", 51234))
    assert sock.return_value.sendall.call_args_list[-1].args == (b"PHOTO",)


@pytest.fixture
def no_upload_hook(monkeypatch):
    """A samsungtvws release that no longer opens its upload socket via `_open_d2d_socket`."""
    monkeypatch.delattr(SamsungTVArt, "_open_d2d_socket")
    monkeypatch.setattr(
        "custom_components.tvcanvas.display_frame._upload_hook_missing_logged", False
    )


def test_without_the_upload_hook_every_upload_is_refused_unsent(no_upload_hook, caplog) -> None:
    """Fail closed: if the address the TV names can't be checked, nothing is uploaded."""
    art = real_art("192.168.1.50")
    with (
        patch("samsungtvws.art.art.socket.socket") as sock,
        pytest.raises(HostRefused, match="does not let Home Assistant check"),
    ):
        art.upload(b"PHOTO", matte="none", portrait_matte="none", file_type="jpg")
    sock.assert_not_called()
    art._send_art_request.assert_not_called()  # the TV is not even asked for an address
    _art("192.168.1.50", "T0")
    assert caplog.text.count("uploads to Frames are refused") == 1  # logged once, not per TV


async def test_without_the_upload_hook_the_refusal_is_reported_and_no_upload_is_lost(
    hass, store, monkeypatch, no_upload_hook
) -> None:
    monkeypatch.setattr(SamsungTVArt, "available", lambda self, category=None: [])
    monkeypatch.setattr(SamsungTVArt, "close", lambda self: None)
    with patch("samsungtvws.art.art.socket.socket") as sock:
        with pytest.raises(HostRefused, match="does not let Home Assistant check"):
            _show_sync("192.168.1.50", "T0", b"PHOTO", [], snapshot=True)
        with pytest.raises(HostRefused, match="does not let Home Assistant check"):
            await frame_display(hass, store, "192.168.1.50").show(b"PHOTO")
    sock.assert_not_called()
    assert "lost" not in store.frame("tv-1")


async def test_a_refused_upload_address_is_reported_and_records_no_lost_upload(hass, store) -> None:
    art = MagicMock()
    art.token = "T1"
    art.available.return_value = []
    art.upload.side_effect = HostRefused("the TV asked for the image somewhere else")
    with patch(FRAME_CTOR, return_value=art), pytest.raises(HostRefused):
        _show_sync("192.168.1.50", "T0", b"PHOTO", [], snapshot=True)
    with patch(FRAME_CTOR, return_value=art), pytest.raises(HostRefused):
        await frame_display(hass, store, "192.168.1.50").show(b"PHOTO")
    assert "lost" not in store.frame("tv-1")


# --- Google Cast -------------------------------------------------------------------------


@pytest.mark.parametrize("host", PUBLIC)
async def test_a_cast_tv_on_a_public_address_is_never_connected_to(hass, host) -> None:
    await async_process_ha_core_config(hass, {"internal_url": "http://192.168.0.10:8123"})
    cache = ImageCache()
    disp = CastDisplay(hass, cache, "tv-1", host)
    with patch(CAST_CTOR) as get, pytest.raises(HostRefused, match=TV_HOST_REFUSED):
        await disp.show(b"PHOTO")
    get.assert_not_called()
    assert cache._by_tv == {}  # the photo isn't even put where the image view serves from


@pytest.mark.parametrize("host", [*PUBLIC, "127.0.0.1", "cast.lan"])
def test_no_cast_connection_can_be_built_for_anything_but_a_home_address(host) -> None:
    with patch(CAST_CTOR) as get, pytest.raises(HostRefused):
        _play_sync(host, uuid.UUID(int=1), "http://ha:8123/api/tvcanvas/image/x.jpg")
    get.assert_not_called()


async def test_a_cast_host_name_is_connected_to_by_its_checked_address(hass, fake_dns) -> None:
    await async_process_ha_core_config(hass, {"internal_url": "http://192.168.0.10:8123"})
    fake_dns.answers["cast.lan"] = [["192.168.1.51"], [PUBLIC_V4]]
    with patch(CAST_CTOR, return_value=fake_cast()) as get:
        await CastDisplay(hass, ImageCache(), "tv-1", "cast.lan").show(b"PHOTO")
    assert get.call_args[0][0][:2] == ("192.168.1.51", 8009)
    assert fake_dns.lookups == ["cast.lan"]


async def test_a_cast_host_name_resolving_to_a_public_address_is_refused(hass, fake_dns) -> None:
    await async_process_ha_core_config(hass, {"internal_url": "http://192.168.0.10:8123"})
    fake_dns.answers["cast.lan"] = [PUBLIC_V4]
    with patch(CAST_CTOR) as get, pytest.raises(HostRefused):
        await CastDisplay(hass, ImageCache(), "tv-1", "cast.lan").show(b"PHOTO")
    get.assert_not_called()


# --- the runner: status, and no probe ------------------------------------------------------


def image_bytes() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 10, 30)).save(out, "JPEG")
    return out.getvalue()


PHOTO = image_bytes()
SHA = hashlib.sha256(PHOTO).hexdigest()
FITTED = b"FITTED:" + PHOTO  # stands in for the resized JPEG a TV gets


def group_spec(tv_id: str, kind: str, host: str) -> dict:
    return {
        "version": 1,
        "tv": {"id": tv_id, "kind": kind, "host": host, "name": f"TV {tv_id}"},
        "interval_seconds": 60,
        "source": {
            "type": "group",
            "group": {"items": [{"type": "local", "sha256": SHA, "name": "IMG_0042"}]},
        },
    }


def frame_spec(tv_id: str, host: str) -> dict:
    return group_spec(tv_id, "frame", host)


class RecordingDisplay:
    def __init__(self, show_exc: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.show_exc = show_exc

    async def ready(self) -> None:
        self.calls.append("ready")

    async def show(self, jpeg: bytes) -> None:
        self.calls.append("show")
        if self.show_exc:
            raise self.show_exc


@pytest.fixture
async def make_runner(hass, monkeypatch):
    state = StateStore(hass, "e")
    await state.async_load()
    client = AsyncMock()
    fetched: list[str] = []

    async def source(spec, rotation, client, session, fit, credits=None, photos=None):
        fetched.append(spec.tv.id)
        return b"IMG", "label"

    monkeypatch.setattr("custom_components.tvcanvas.runner.next_image", source)

    def make(raw: dict, display) -> TVRunner:
        runner = TVRunner(
            hass, client, None, state, parse_spec(raw), display, lambda: None, lambda: None
        )
        runner.probes = []

        async def probe(host, port):
            runner.probes.append(host)
            return False

        runner._probe = probe
        runner._sleep = AsyncMock()
        return runner

    make.client, make.fetched = client, fetched
    return make


@pytest.mark.parametrize("kind", ["frame", "cast"])
@pytest.mark.parametrize("host", PUBLIC)
async def test_a_tv_on_a_public_address_reports_why_and_nothing_touches_it(
    make_runner, caplog, kind, host
) -> None:
    disp = RecordingDisplay()
    runner = make_runner(group_spec("tv-evil", kind, host), disp)
    for _ in range(3):
        await runner.tick()
        await runner._wait()
    assert disp.calls == [] and make_runner.fetched == []  # no image is even prepared
    assert runner.probes == []  # the backoff probe is a connection too
    assert runner.status.last_error == TV_HOST_REFUSED and runner.status.last_ok is None
    for call in make_runner.client.agent_status.call_args_list:
        assert call.args[:3] == ("tv-evil", False, TV_HOST_REFUSED)
    assert make_runner.client.agent_status.await_count == 3
    assert caplog.text.count("is not updated") == 1  # one warning, not one a minute


async def test_a_refusal_from_the_display_is_not_tv_unreachable_and_is_never_probed(
    make_runner,
) -> None:
    """A display's own check (a name that now resolves elsewhere, a Frame naming a public
    upload address) must not start the "TV unreachable" backoff, which probes the host."""
    disp = RecordingDisplay(show_exc=HostRefused("the TV asked for the image elsewhere"))
    runner = make_runner(frame_spec("tv-1", "192.168.1.50"), disp)
    await runner.tick()
    await runner._wait()
    assert disp.calls == ["ready", "show"]
    assert runner.status.last_error == "the TV asked for the image elsewhere"
    assert make_runner.client.agent_status.call_args.args[:3] == ("tv-1", False, TV_HOST_REFUSED)
    assert runner.probes == [] and runner.next_delay() == 60


async def test_an_unreachable_home_tv_is_still_probed(make_runner) -> None:
    disp = RecordingDisplay(show_exc=DisplayError("TV unreachable: no route"))
    runner = make_runner(frame_spec("tv-1", "192.168.1.50"), disp)
    await runner.tick()
    await runner._wait()
    assert runner.probes and set(runner.probes) == {"192.168.1.50"}


async def test_a_host_changed_to_a_public_address_is_never_probed(make_runner) -> None:
    runner = make_runner(frame_spec("tv-1", "192.168.1.50"), RecordingDisplay())
    runner._failures, runner._unreachable = 1, True  # backing off when the spec changes
    runner.spec = parse_spec(frame_spec("tv-1", PUBLIC_V4))
    await runner._wait()
    assert runner.probes == []


# --- the invariant: a phone photo never leaves the home -------------------------------------


def fake_art() -> MagicMock:
    art = MagicMock()
    art.token = "T1"
    art.get_artmode.return_value = "on"
    art.get_slideshow_status.return_value = {"value": "off"}
    art.available.return_value = []
    art.upload.return_value = "MY_F0002"
    return art


def fake_cast() -> MagicMock:
    cc = MagicMock()

    def play_media(url, content_type, callback_function=None):
        callback_function(True, {"type": "MEDIA_STATUS", "status": [{"playerState": "PLAYING"}]})

    cc.media_controller.play_media.side_effect = play_media
    return cc


def statuses(aioclient_mock) -> dict[str, set[tuple[bool, str | None]]]:
    seen: dict[str, set[tuple[bool, str | None]]] = {}
    for _method, url, body, _headers in aioclient_mock.mock_calls:
        if str(url).endswith("/ha/agent/status"):
            seen.setdefault(body["tv_id"], set()).add((body["ok"], body["error"]))
    return seen


async def test_a_local_photo_is_never_sent_to_a_public_host_and_home_tvs_still_run(
    hass, aioclient_mock, monkeypatch
) -> None:
    """One sync from a hacked cloud: two "TVs" at the attacker's addresses, each asking for
    the user's photo, next to the user's two real TVs."""
    await async_process_ha_core_config(hass, {"internal_url": "http://192.168.0.10:8123"})
    specs = [
        group_spec("evil-frame", "frame", PUBLIC_V4),
        group_spec("evil-cast", "cast", PUBLIC_V6),
        group_spec("home-frame", "frame", "192.168.1.50"),
        group_spec("home-cast", "cast", "192.168.1.51"),
    ]
    aioclient_mock.get(f"{URL}/ha/agent/sync", json={"specs": specs}, headers={"ETag": '"e1"'})
    aioclient_mock.post(f"{URL}/ha/agent/status", json={})
    # The home Frame's power, read over HTTP before each tick (the evil one is never asked).
    aioclient_mock.get(re.compile(r":8001/api/v2/$"), json={"device": {"PowerState": "on"}})
    monkeypatch.setattr(
        "custom_components.tvcanvas.runner.fit_to_tv", lambda data, orientation: b"FITTED:" + data
    )
    e = entry(hass)
    with patch("custom_components.tvcanvas.runner.TVRunner.start"):
        await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    coord = e.runtime_data
    await coord.photos.async_put(SHA, PHOTO)
    assert set(coord.runners) == {"evil-frame", "evil-cast", "home-frame", "home-cast"}

    art, cast = fake_art(), fake_cast()
    probed: list[str] = []

    async def probe(host, port):
        probed.append(host)
        return False

    with (
        patch(FRAME_CTOR, return_value=art) as frame_ctor,
        patch(CAST_CTOR, return_value=cast) as cast_ctor,
        patch.object(coord.photos, "async_read", wraps=coord.photos.async_read) as read,
    ):
        for runner in coord.runners.values():
            runner._probe, runner._sleep = probe, AsyncMock()
            for _ in range(2):
                await runner.tick()
                await runner._wait()
            await hass.async_block_till_done()

    # Every connection HA built went to one of the two home TVs.
    assert {c.args[0] for c in frame_ctor.call_args_list} == {"192.168.1.50"}
    assert {c.args[0][0] for c in cast_ctor.call_args_list} == {"192.168.1.51"}
    assert probed == []
    # The photo reached the real TVs, and only for them was it read from the photo folder.
    assert art.upload.call_args.args[0] == FITTED
    assert read.await_count == 4  # 2 home TVs × 2 ticks
    cache: ImageCache = hass.data["tvcanvas"]["cache"]
    assert set(cache._by_tv) == {"home-cast"}
    assert all(data == FITTED for data in cache._by_tv["home-cast"].values())
    # Nobody is asked to press Allow for the attacker's "Frame", and no token is kept.
    notes = persistent_notification._async_get_or_create_notifications(hass)
    assert "tvcanvas_pair_evil-frame" not in notes
    assert coord._store.frame_token(PUBLIC_V4) is None
    # Each TV's status, in HA and as sent to the cloud.
    assert coord.runners["evil-frame"].status.last_error == TV_HOST_REFUSED
    assert coord.runners["evil-cast"].status.last_error == TV_HOST_REFUSED
    assert coord.runners["home-frame"].status.last_error is None
    assert coord.runners["home-cast"].status.last_error is None
    assert statuses(aioclient_mock) == {
        "evil-frame": {(False, TV_HOST_REFUSED)},
        "evil-cast": {(False, TV_HOST_REFUSED)},
        "home-frame": {(True, None)},
        "home-cast": {(True, None)},
    }
    # The Frame's power state is read from the home Frame only.
    asked = {str(url) for _method, url, *_ in aioclient_mock.mock_calls if "/api/v2/" in str(url)}
    assert asked == {f"http://{coord.runners['home-frame'].spec.tv.host}:8001/api/v2/"}
    await hass.config_entries.async_unload(e.entry_id)


# --- the Cast image view (#15) ------------------------------------------------------------


@pytest.fixture
async def image_view(hass, hass_client_no_auth):
    """HA behind a reverse proxy on 127.0.0.1 that it trusts for X-Forwarded-For."""
    await async_process_ha_core_config(hass, {})
    config = {"use_x_forwarded_for": True, "trusted_proxies": ["127.0.0.1"]}
    assert await async_setup_component(hass, "http", {"http": config})
    cache = ImageCache()
    hass.http.register_view(ImageView(cache))
    name = cache.put("tv", b"JPEGDATA")
    client = await hass_client_no_auth()
    return client, f"/api/tvcanvas/image/{name}"


@pytest.mark.parametrize("remote", ["192.168.1.51", "10.1.2.3", "fe80::1", "fd00::5"])
async def test_image_view_serves_home_clients_behind_a_trusted_proxy(image_view, remote) -> None:
    client, path = image_view
    resp = await client.get(path, headers={"X-Forwarded-For": remote})
    assert resp.status == 200 and await resp.read() == b"JPEGDATA"
    assert resp.headers["Content-Type"] == "image/jpeg"


async def test_image_view_serves_a_client_on_the_lan_directly(image_view) -> None:
    client, path = image_view
    resp = await client.get(path)  # no proxy in between: the peer itself is local
    assert resp.status == 200 and await resp.read() == b"JPEGDATA"


@pytest.mark.parametrize("remote", ["8.8.8.8", PUBLIC_V6, "100.64.0.1"])
async def test_image_view_gives_public_clients_the_404_of_an_unknown_name(
    image_view, remote
) -> None:
    client, path = image_view
    resp = await client.get(path, headers={"X-Forwarded-For": remote})
    unknown = await client.get("/api/tvcanvas/image/" + "0" * 32 + ".jpg")
    assert resp.status == unknown.status == 404
    assert await resp.read() == await unknown.read() == b""
    assert resp.headers.get("Content-Type") == unknown.headers.get("Content-Type")


async def test_image_view_refuses_home_assistant_cloud_remote_requests(image_view) -> None:
    client, path = image_view
    with patch("custom_components.tvcanvas.lan_view.is_cloud_connection", return_value=True):
        resp = await client.get(path)
    assert resp.status == 404 and await resp.read() == b""
