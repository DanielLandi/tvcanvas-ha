"""A Cast TV whose DHCP address changed is found again by the identity HA learned.

The spec names the TV by its address only (the cloud's schema has no field for a Cast
id), so every image shown at the spec's host records the TV's Cast UUID and the MAC the
HA host's neighbour table holds for it. An address the TV moved to counts only when its
eureka_info names the UUID AND the neighbour table names the MAC.
"""

from __future__ import annotations

import logging
import sys
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.core_config import async_process_ha_core_config
from pychromecast.error import RequestTimeout
from pychromecast.socket_client import CONNECTION_STATUS_CONNECTED, ConnectionStatus

from custom_components.tvcanvas import locate, locate_cast
from custom_components.tvcanvas.display_cast import (
    CastDisplay,
    CastLoadError,
    CastUnreachable,
    _play_sync,
)
from custom_components.tvcanvas.display_view import ImageCache
from custom_components.tvcanvas.displays import DisplayError
from custom_components.tvcanvas.locate_cast import (
    CastFinder,
    CastIdentity,
    cast_uuid,
    normalize_uuid,
)
from custom_components.tvcanvas.models import parse_spec
from custom_components.tvcanvas.runner import TVRunner
from custom_components.tvcanvas.storage import StateStore

TV_ID = "cast-living-room"
UUID = "8bc271d5c20a05b183ba8d59eaaf9701"
OTHER_UUID = "0123456789abcdef0123456789abcdef"
MAC = "1cf29a6404b1"
OTHER = "aabbccddeeff"
OLD, NEW, SPOOF = "10.20.30.58", "10.20.30.61", "10.20.30.66"
PLAY_SYNC = "custom_components.tvcanvas.display_cast._play_sync"
NOTICE = f"tvcanvas_moved_{TV_ID}"
IDENTITY = {"uuid": UUID, "mac": MAC, "host": OLD}  # learned at the spec host OLD


@pytest.fixture
async def store(hass):
    await async_process_ha_core_config(hass, {"internal_url": "http://10.20.30.10:8123"})
    s = StateStore(hass, "e")
    await s.async_load()
    return s


class Lan:
    """What discovery, eureka_info and the HA host's neighbour table say."""

    def __init__(self) -> None:
        self.discovered: list[str] = [NEW]
        self.uuids: dict[str, str] = {OLD: UUID, NEW: UUID}  # address -> UUID it reports
        self.arp: dict[str, str] | None = {OLD: MAC, NEW: MAC}  # None: table unreadable
        self.asked: list[str] = []

    async def cast_uuid(self, _hass, address):
        self.asked.append(address)
        return self.uuids.get(address)

    def read_neighbours(self, path=None):
        if self.arp is None:
            raise PermissionError(13, "Permission denied")
        return dict(self.arp)


@pytest.fixture
def lan():
    net = Lan()
    with (
        patch.object(
            locate_cast, "ssdp_addresses", AsyncMock(side_effect=lambda _h, _u: net.discovered)
        ),
        patch.object(locate_cast, "mdns_addresses", AsyncMock(return_value=[])),
        patch.object(locate_cast, "cast_uuid", side_effect=net.cast_uuid),
        patch.object(locate, "read_neighbours", side_effect=net.read_neighbours),
    ):
        yield net


class TV:
    """_play_sync for a Cast TV that accepts connections at one address only."""

    def __init__(self, at: str) -> None:
        self.at, self.calls = at, []

    def play_sync(self, host, uid, url):
        self.calls.append(host)
        if host != self.at:
            raise CastUnreachable("no connection to the TV")


def display(hass, store, host=OLD):
    return CastDisplay(hass, ImageCache(), TV_ID, host, store=store, finder=CastFinder(hass))


def notice(hass) -> str | None:
    from homeassistant.components import persistent_notification

    found = persistent_notification._async_get_or_create_notifications(hass).get(NOTICE)
    return found["message"] if found else None


async def tick(disp, tv) -> None:
    """What the runner does: ready(), then show()."""
    with patch(PLAY_SYNC, side_effect=tv.play_sync):
        assert await disp.ready() is None
        await disp.show(b"J")


async def test_identity_is_learned_on_the_first_success(hass, store, lan) -> None:
    disp, tv = display(hass, store), TV(OLD)
    await tick(disp, tv)
    assert tv.calls == [OLD]
    assert store.cast(TV_ID)["identity"] == IDENTITY
    assert lan.asked == [OLD]  # eureka_info first, then the neighbour table


async def test_cast_tv_with_a_new_dhcp_address_is_followed(hass, store, lan, caplog) -> None:
    store.cast(TV_ID)["identity"] = dict(IDENTITY)
    lan.uuids, lan.arp = {NEW: UUID}, {NEW: MAC}  # nothing answers at OLD any more
    disp, tv = display(hass, store), TV(NEW)
    with caplog.at_level(logging.WARNING):
        await tick(disp, tv)  # the same image goes to the new address at once
    assert tv.calls == [OLD, NEW]
    assert disp.address == NEW
    assert store.cast(TV_ID)["moved"] == {"from": OLD, "to": NEW}
    assert f"Cast TV {TV_ID} stopped answering at {OLD} and was found at {NEW}" in caplog.text
    assert f"moved from {OLD} to {NEW}" in notice(hass)

    # Next tick: the followed address is re-checked (original first), then used.
    lan.asked.clear()
    await tick(disp, tv)
    assert tv.calls[-1] == NEW and lan.asked == [OLD, NEW]
    assert store.cast(TV_ID)["identity"] == IDENTITY  # learned at the spec's host only


@pytest.mark.parametrize(
    "slow",
    [CastLoadError("the TV did not confirm the image"), RequestTimeout("wait", 10)],
)
async def test_a_slow_reply_is_not_a_move(hass, store, lan, slow) -> None:
    """The TV connected and then answered slowly (or not at all): it is still there."""
    store.cast(TV_ID)["identity"] = dict(IDENTITY)
    disp = display(hass, store)
    with patch(PLAY_SYNC, side_effect=slow), pytest.raises(DisplayError):
        await disp.show(b"J")
    locate_cast.ssdp_addresses.assert_not_awaited()
    assert "moved" not in store.cast(TV_ID) and notice(hass) is None


async def test_uuid_mismatch_is_not_followed(hass, store, lan) -> None:
    """Another Cast device took an address the TV was discovered at."""
    store.cast(TV_ID)["identity"] = dict(IDENTITY)
    lan.uuids[NEW] = OTHER_UUID
    disp, tv = display(hass, store), TV(NEW)
    with pytest.raises(DisplayError, match="TV unreachable"):
        await tick(disp, tv)
    assert tv.calls == [OLD]  # never connected to NEW
    assert "moved" not in store.cast(TV_ID) and disp.address == OLD


@pytest.mark.parametrize(
    ("arp", "discovered", "why"),
    [
        ({NEW: OTHER}, NEW, "another device answers at that address"),
        ({}, NEW, "the address is not in the network's table"),
        (None, NEW, "the network's address table can't be read"),
        ({}, "fd00::5", "IPv6 addresses can't be checked"),
    ],
)
async def test_a_device_that_only_claims_the_uuid_gets_nothing(
    hass, store, lan, arp, discovered, why
) -> None:
    """The UUID matches but the MAC can't be confirmed: no image; the owner is told."""
    store.cast(TV_ID)["identity"] = dict(IDENTITY)
    lan.arp, lan.discovered = arp, [discovered]
    lan.uuids[discovered] = UUID
    disp, tv = display(hass, store), TV(discovered)
    with pytest.raises(DisplayError):
        await tick(disp, tv)
    assert tv.calls == [OLD]
    assert "moved" not in store.cast(TV_ID)
    message = notice(hass)
    assert f"seems to have moved to {discovered}" in message and why in message


async def test_the_checked_tv_wins_over_an_earlier_spoofer(hass, store, lan) -> None:
    store.cast(TV_ID)["identity"] = dict(IDENTITY)
    lan.discovered = [SPOOF, NEW]
    lan.uuids[SPOOF] = UUID
    lan.arp = {SPOOF: OTHER, NEW: MAC}
    disp, tv = display(hass, store), TV(NEW)
    await tick(disp, tv)
    assert tv.calls == [OLD, NEW] and disp.address == NEW


async def test_no_recorded_identity_is_never_searched(hass, store, lan) -> None:
    disp, tv = display(hass, store), TV(NEW)
    with pytest.raises(DisplayError, match="TV unreachable"):
        await tick(disp, tv)
    assert tv.calls == [OLD] and lan.asked == []
    locate_cast.ssdp_addresses.assert_not_awaited()
    assert "has not shown an image on this TV at this address yet" in notice(hass)


async def test_followed_address_is_dropped_when_the_original_answers_again(
    hass, store, lan
) -> None:
    store.cast(TV_ID).update(identity=dict(IDENTITY), moved={"from": OLD, "to": NEW})
    disp, tv = display(hass, store), TV(OLD)
    await tick(disp, tv)
    assert tv.calls == [OLD]
    assert "moved" not in store.cast(TV_ID) and disp.address == OLD


async def test_the_apps_host_wins_over_a_followed_address(hass, store, lan) -> None:
    store.cast(TV_ID).update(identity=dict(IDENTITY), moved={"from": OLD, "to": SPOOF})
    disp, tv = display(hass, store, host=NEW), TV(NEW)
    assert disp.address == NEW
    with patch(PLAY_SYNC, side_effect=tv.play_sync):
        assert await disp.ready() is None
        assert lan.asked == []  # the record is dropped without asking anyone
        await disp.show(b"J")
    assert tv.calls == [NEW] and "moved" not in store.cast(TV_ID)
    assert store.cast(TV_ID)["identity"] == {**IDENTITY, "host": NEW}  # re-learned there


async def test_a_later_device_at_the_same_host_does_not_replace_the_identity(
    hass, store, lan, caplog
) -> None:
    """Something briefly took the TV's address: it can't re-teach HA who the TV is."""
    store.cast(TV_ID)["identity"] = dict(IDENTITY)
    lan.uuids, lan.arp = {OLD: OTHER_UUID}, {OLD: OTHER}
    with caplog.at_level(logging.WARNING):
        await tick(display(hass, store), TV(OLD))  # the app's host still gets the image
    assert store.cast(TV_ID)["identity"] == IDENTITY
    assert store.cast(TV_ID)["conflict"] == OLD
    assert "reports a different Cast UUID and MAC address" in caplog.text
    assert "reports a different Cast UUID and MAC address" in notice(hass)

    # Even when the real TV answers there again, the block stays until the app acts.
    lan.uuids, lan.arp = {OLD: UUID}, {OLD: MAC}
    await tick(display(hass, store), TV(OLD))
    assert store.cast(TV_ID)["conflict"] == OLD and notice(hass) is not None


async def test_an_identity_conflict_blocks_following(hass, store, lan) -> None:
    store.cast(TV_ID).update(identity=dict(IDENTITY), conflict=OLD)
    lan.uuids, lan.arp = {NEW: UUID}, {NEW: MAC}
    disp, tv = display(hass, store), TV(NEW)
    with pytest.raises(DisplayError, match="TV unreachable"):
        await tick(disp, tv)
    assert tv.calls == [OLD] and lan.asked == []
    locate_cast.ssdp_addresses.assert_not_awaited()
    assert "follows no move until the TVCanvas app sends" in notice(hass)


async def test_an_identity_conflict_drops_a_followed_address(hass, store, lan) -> None:
    store.cast(TV_ID).update(identity=dict(IDENTITY), conflict=OLD, moved={"from": OLD, "to": NEW})
    disp, tv = display(hass, store), TV(NEW)
    with pytest.raises(DisplayError, match="no longer checks out"):
        await tick(disp, tv)
    assert tv.calls == [] and "moved" not in store.cast(TV_ID)


async def test_a_new_host_from_the_app_relearns_and_clears_a_conflict(hass, store, lan) -> None:
    store.cast(TV_ID).update(identity=dict(IDENTITY), conflict=OLD)
    lan.uuids, lan.arp = {NEW: OTHER_UUID}, {NEW: OTHER}  # e.g. the user replaced the TV
    await tick(display(hass, store, host=NEW), TV(NEW))
    assert store.cast(TV_ID)["identity"] == {"uuid": OTHER_UUID, "mac": OTHER, "host": NEW}
    assert "conflict" not in store.cast(TV_ID)


async def test_identity_learned_at_another_host_is_not_used(hass, store, lan) -> None:
    """The app sent a new host: the old identity is not trusted for it."""
    store.cast(TV_ID)["identity"] = dict(IDENTITY)  # learned at OLD
    disp, tv = display(hass, store, host=SPOOF), TV(NEW)
    with pytest.raises(DisplayError, match="TV unreachable"):
        await tick(disp, tv)
    locate_cast.ssdp_addresses.assert_not_awaited()
    assert "at this address yet" in notice(hass)


async def test_followed_address_that_stops_checking_out_is_dropped(hass, store, lan) -> None:
    store.cast(TV_ID).update(identity=dict(IDENTITY), moved={"from": OLD, "to": NEW})
    lan.uuids = {NEW: UUID}
    lan.arp = {NEW: OTHER}
    disp, tv = display(hass, store), TV(NEW)
    with pytest.raises(DisplayError, match="no longer checks out"):
        await tick(disp, tv)
    assert tv.calls == [] and "moved" not in store.cast(TV_ID)


async def test_tv_found_nowhere_is_searched_rarely(hass, store, lan) -> None:
    """A TV that is off answers nowhere: one search, then none for 10 minutes."""
    store.cast(TV_ID)["identity"] = dict(IDENTITY)
    lan.uuids = {}
    disp, tv = display(hass, store), TV("nowhere")
    now = [1000.0]
    disp._clock = lambda: now[0]
    for step in (0, 300, 301):
        now[0] += step
        with pytest.raises(DisplayError, match="TV unreachable"):
            await tick(disp, tv)
    assert locate_cast.ssdp_addresses.await_count == 2
    assert "moved" not in store.cast(TV_ID) and notice(hass) is None


async def test_display_without_a_store_never_searches(hass, store, lan) -> None:
    disp, tv = CastDisplay(hass, ImageCache(), TV_ID, OLD), TV(NEW)
    with pytest.raises(DisplayError, match="TV unreachable"):
        await tick(disp, tv)
    assert lan.asked == [] and notice(hass) is None


async def test_learning_fails_closed(hass, store, lan) -> None:
    lan.arp = None
    await tick(display(hass, store), TV(OLD))
    assert "identity" not in store.cast(TV_ID)
    lan.arp, lan.uuids = {OLD: MAC}, {}
    await tick(display(hass, store), TV(OLD))
    assert "identity" not in store.cast(TV_ID)


async def test_runner_probes_the_followed_address(hass, store) -> None:
    spec = parse_spec(
        {
            "version": 1,
            "tv": {"id": TV_ID, "kind": "cast", "host": OLD, "name": "Living room TV"},
            "interval_seconds": 180,
            "source": {"type": "board", "board": {"type_id": "weather", "params": {}}},
        }
    )
    store.cast(TV_ID).update(identity=dict(IDENTITY), moved={"from": OLD, "to": NEW})
    runner = TVRunner(
        hass, AsyncMock(), None, store, spec, display(hass, store), lambda: None, lambda: None
    )
    assert runner.address == NEW
    probed: list[tuple[str, int]] = []

    async def probe(host, port):
        probed.append((host, port))
        return True

    runner._probe, runner._sleep = probe, AsyncMock()
    runner._failures, runner._unreachable = 1, True
    await runner._wait()
    assert probed and set(probed) == {(NEW, 8009)}


async def test_forget_tv_drops_the_cast_state(hass, store) -> None:
    store.cast(TV_ID).update(identity=dict(IDENTITY), moved={"from": OLD, "to": NEW})
    store.forget_tv(TV_ID)
    assert store.cast(TV_ID) == {}


# _play_sync: only a connection that never happened is "unreachable".


def _cast_that_times_out(connects: bool) -> MagicMock:
    cc = MagicMock()
    listeners = []
    cc.socket_client.register_connection_listener.side_effect = listeners.append

    def wait(timeout=None):
        if connects:
            for listener in listeners:
                listener.new_connection_status(
                    ConnectionStatus(CONNECTION_STATUS_CONNECTED, None, None)
                )
        raise RequestTimeout("wait", timeout)

    cc.wait.side_effect = wait
    return cc


@pytest.mark.parametrize(("connects", "raised"), [(False, CastUnreachable), (True, RequestTimeout)])
def test_play_sync_tells_no_connection_from_a_slow_reply(connects, raised) -> None:
    cc = _cast_that_times_out(connects)
    with (
        patch(
            "custom_components.tvcanvas.display_cast.pychromecast.get_chromecast_from_host",
            return_value=cc,
        ),
        pytest.raises(raised),
    ):
        _play_sync(OLD, uuid.UUID(int=1), "http://ha:8123/api/tvcanvas/image/x.jpg")
    cc.disconnect.assert_called_once()


# What the TV and discovery say.


async def test_cast_uuid_reads_eureka_info(hass, aioclient_mock) -> None:
    path = "/setup/eureka_info?params=device_info,name"
    aioclient_mock.get(
        f"https://{NEW}:8443{path}",
        json={"device_info": {"ssdp_udn": "8bc271d5-c20a-05b1-83ba-8d59eaaf9701"}},
    )
    aioclient_mock.get(f"https://{OLD}:8443{path}", exc=aiohttp.ClientConnectionError())
    aioclient_mock.get(f"http://{OLD}:8008{path}", json={"ssdp_udn": UUID})
    aioclient_mock.get(f"https://{SPOOF}:8443{path}", exc=TimeoutError())
    assert await cast_uuid(hass, NEW) == UUID
    assert await cast_uuid(hass, OLD) == UUID  # plain HTTP on older devices
    assert await cast_uuid(hass, SPOOF) is None  # a timeout: not asked again over HTTP
    assert await cast_uuid(hass, "93.184.215.14") is None  # never asked
    assert [str(call[1]) for call in aioclient_mock.mock_calls] == [
        f"https://{NEW}:8443{path}",
        f"https://{OLD}:8443{path}",
        f"http://{OLD}:8008{path}",
        f"https://{SPOOF}:8443{path}",
    ]


def test_identity_from_the_store() -> None:
    assert CastIdentity.from_dict(IDENTITY) == CastIdentity(UUID, MAC)
    assert CastIdentity.from_dict({"uuid": UUID}) is None
    assert CastIdentity.from_dict({"uuid": "nope", "mac": MAC}) is None
    assert CastIdentity.from_dict(None) is None
    assert normalize_uuid("uuid:8BC271D5-C20A-05B1-83BA-8D59EAAF9701") == UUID


async def test_ssdp_addresses_keep_the_tvs_udn_only(hass, monkeypatch) -> None:
    class Info:
        def __init__(self, location, udn=None, usn=None):
            self.ssdp_location, self.ssdp_udn, self.ssdp_usn = location, udn, usn

    found = [
        Info(f"http://{NEW}:8008/ssdp/device-desc.xml", udn=f"uuid:{uuid.UUID(UUID)}"),
        Info(f"http://{SPOOF}:8008/ssdp/device-desc.xml", udn=f"uuid:{uuid.UUID(OTHER_UUID)}"),
        Info(f"http://{OLD}:8008/dd.xml", usn=f"uuid:{UUID}::urn:dial-multiscreen-org:service"),
    ]

    async def by_st(_hass, st):
        return found if st == "urn:dial-multiscreen-org:service:dial:1" else []

    fake_ssdp = types.ModuleType("homeassistant.components.ssdp")
    fake_ssdp.async_get_discovery_info_by_st = by_st
    monkeypatch.setitem(sys.modules, "homeassistant.components.ssdp", fake_ssdp)
    assert await locate_cast.ssdp_addresses(hass, UUID) == [NEW, OLD]


async def test_mdns_addresses_keep_the_tvs_id_only(hass) -> None:
    def service(found_id: bytes, address: str) -> MagicMock:
        info = MagicMock()
        info.properties = {b"id": found_id, b"fn": b"Living Room TV"}
        info.parsed_addresses.return_value = [address]
        return info

    services = [service(UUID.encode(), NEW), service(OTHER_UUID.encode(), SPOOF)]
    with patch.object(locate_cast, "mdns_services", AsyncMock(return_value=services)):
        assert await locate_cast.mdns_addresses(hass, UUID) == [NEW]
