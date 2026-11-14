"""A Frame whose DHCP address changed is found again by its MAC (2026-11-11 incident).

The spec said 192.168.0.20; at 22:44 the TV took a new lease (.21) and every tick
after that failed with "TV unreachable: [Errno 113] Host is unreachable" until the phone
sent the new address, which it never did overnight.

An address counts as the TV only when its /api/v2/ AND the HA host's neighbour table
both name the TV's MAC; a device that only claims the MAC over HTTP gets nothing.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.tvcanvas import locate
from custom_components.tvcanvas.display_frame import FrameDisplay
from custom_components.tvcanvas.displays import DisplayError, HostRefused
from custom_components.tvcanvas.locate import (
    Check,
    FrameFinder,
    find_frame,
    mac_from_tv_id,
    read_neighbours,
    reported_mac,
)
from custom_components.tvcanvas.models import parse_spec
from custom_components.tvcanvas.runner import TVRunner
from custom_components.tvcanvas.storage import StateStore

TV_ID = "samsung:00:00:5e:00:53:01"
MAC = "00005e005301"
OTHER = "aabbccddeeff"
OLD, NEW, SPOOF = "192.168.0.20", "192.168.0.21", "192.168.0.66"
MODE_SYNC = "custom_components.tvcanvas.display_frame._mode_sync"
SHOW_SYNC = "custom_components.tvcanvas.display_frame._show_sync"
NOTICE = f"tvcanvas_moved_{TV_ID}"


@pytest.fixture
async def store(hass):
    s = StateStore(hass, "e")
    await s.async_load()
    return s


class Lan:
    """What discovery, /api/v2/ and the HA host's neighbour table say."""

    def __init__(self) -> None:
        self.discovered: list[str] = [NEW]
        self.http: dict[str, str] = {NEW: MAC}  # address -> MAC it claims over HTTP
        self.arp: dict[str, str] | None = {NEW: MAC}  # None: table unreadable
        self.power: dict[str, str] = {}  # address -> PowerState; none: older firmware
        self.asked: list[str] = []

    async def power_state(self, _hass, address):
        return self.power.get(address)

    async def reported_mac(self, _hass, address):
        self.asked.append(address)
        return self.http.get(address)

    def read_neighbours(self, path=None):
        if self.arp is None:
            raise PermissionError(13, "Permission denied")
        return dict(self.arp)


@pytest.fixture
def lan():
    net = Lan()
    with (
        patch.object(locate, "ssdp_addresses", AsyncMock(side_effect=lambda _h: net.discovered)),
        patch.object(locate, "mdns_addresses", AsyncMock(return_value=[])),
        patch.object(locate, "reported_mac", side_effect=net.reported_mac),
        patch.object(locate, "power_state", side_effect=net.power_state),
        patch.object(locate, "read_neighbours", side_effect=net.read_neighbours),
    ):
        yield net


class TV:
    """_mode_sync / _show_sync for a Frame that answers at one address only."""

    def __init__(self, at: str) -> None:
        self.at, self.calls = at, []

    def mode_sync(self, host, token, delete_in_art):
        self.calls.append((host, token))
        if host != self.at:
            raise OSError(113, "Host is unreachable")
        return "tvcanvas", None, []


def display(hass, store, host=OLD, tv_id=TV_ID):
    return FrameDisplay(hass, store, tv_id, host, True, lambda _: None, finder=FrameFinder(hass))


def notice(hass) -> str | None:
    from homeassistant.components import persistent_notification

    found = persistent_notification._async_get_or_create_notifications(hass).get(NOTICE)
    return found["message"] if found else None


async def test_frame_with_a_new_dhcp_address_is_followed_and_updated(hass, store, lan) -> None:
    store.set_frame_token(OLD, "SAVED")
    disp, tv = display(hass, store), TV(NEW)
    with patch(MODE_SYNC, side_effect=tv.mode_sync):
        assert await disp.ready() is None  # asked again at the new address, same tick
    assert tv.calls == [(OLD, "SAVED"), (NEW, None)]  # the saved token stays with OLD
    assert disp.address == NEW
    assert store.frame(TV_ID)["moved"] == {"from": OLD, "to": NEW}
    assert "moved from 192.168.0.20 to 192.168.0.21" in notice(hass)

    with patch(SHOW_SYNC, return_value=("MY_F0001", "NEWTOKEN", [], [])) as show:
        await disp.show(b"J")
    assert show.call_args[0][:2] == (NEW, None)
    assert store.frame_token(NEW) == "NEWTOKEN" and store.frame_token(OLD) == "SAVED"


@pytest.mark.parametrize(
    ("arp", "why"),
    [
        ({NEW: OTHER}, "another device answers at that address"),
        ({}, "the address is not in the network's table"),
        (None, "the network's address table can't be read"),
    ],
)
async def test_device_that_only_claims_the_mac_gets_nothing(hass, store, lan, arp, why) -> None:
    """Spoofed /api/v2/: no connection, no token, no image; the owner is told."""
    lan.arp = arp
    store.set_frame_token(OLD, "SAVED")
    disp, tv = display(hass, store), TV(NEW)
    with patch(MODE_SYNC, side_effect=tv.mode_sync), pytest.raises(DisplayError):
        await disp.ready()
    assert tv.calls == [(OLD, "SAVED")]  # never connected to NEW
    assert "moved" not in store.frame(TV_ID)
    assert disp.address == OLD
    message = notice(hass)
    assert "seems to have moved to 192.168.0.21" in message and why in message


async def test_the_checked_tv_wins_over_an_earlier_spoofer(hass, store, lan) -> None:
    lan.discovered = [SPOOF, NEW]
    lan.http = {SPOOF: MAC, NEW: MAC}
    lan.arp = {SPOOF: OTHER, NEW: MAC}
    disp, tv = display(hass, store), TV(NEW)
    with patch(MODE_SYNC, side_effect=tv.mode_sync):
        assert await disp.ready() is None
    assert disp.address == NEW
    assert SPOOF not in [host for host, _ in tv.calls]


async def test_followed_address_is_rechecked_and_dropped_when_it_stops_checking_out(
    hass, store, lan
) -> None:
    """Something else took the followed address (its HTTP still claims the MAC)."""
    store.frame(TV_ID)["moved"] = {"from": OLD, "to": NEW}
    lan.arp = {NEW: OTHER}
    disp, tv = display(hass, store), TV(NEW)
    with patch(MODE_SYNC, side_effect=tv.mode_sync), pytest.raises(DisplayError):
        await disp.ready()
    assert NEW not in [host for host, _ in tv.calls]
    assert "moved" not in store.frame(TV_ID)


async def test_followed_address_is_dropped_when_the_original_answers_again(
    hass, store, lan
) -> None:
    store.frame(TV_ID)["moved"] = {"from": OLD, "to": NEW}
    lan.http, lan.arp = {OLD: MAC, NEW: MAC}, {OLD: MAC, NEW: MAC}
    disp, tv = display(hass, store), TV(OLD)
    with patch(MODE_SYNC, side_effect=tv.mode_sync):
        assert await disp.ready() is None
    assert tv.calls == [(OLD, None)]
    assert "moved" not in store.frame(TV_ID) and disp.address == OLD


async def test_followed_address_that_checks_out_survives_a_restart(hass, store, lan) -> None:
    store.frame(TV_ID)["moved"] = {"from": OLD, "to": NEW}
    disp, tv = display(hass, store), TV(NEW)
    with patch(MODE_SYNC, side_effect=tv.mode_sync):
        assert await disp.ready() is None
    assert tv.calls == [(NEW, None)]
    assert lan.asked == [OLD, NEW]  # both re-checked, no discovery needed


async def test_an_address_from_the_app_drops_the_record(hass, store, lan) -> None:
    store.frame(TV_ID)["moved"] = {"from": OLD, "to": "192.168.0.77"}
    disp, tv = display(hass, store, host=NEW), TV(NEW)
    assert disp.address == NEW
    with patch(MODE_SYNC, side_effect=tv.mode_sync):
        assert await disp.ready() is None
    assert "moved" not in store.frame(TV_ID)
    assert lan.asked == []


async def test_tv_found_nowhere_is_still_unreachable_and_searched_rarely(hass, store, lan) -> None:
    """A TV in standby answers nowhere: one search, then none for 10 minutes."""
    lan.http = {}
    disp = display(hass, store)
    now = [1000.0]
    disp._clock = lambda: now[0]
    with patch(MODE_SYNC, side_effect=TV("nowhere").mode_sync):
        with pytest.raises(DisplayError, match="TV unreachable"):
            await disp.ready()
        now[0] += 300
        with pytest.raises(DisplayError, match="TV unreachable"):
            await disp.ready()
        assert len(lan.asked) == 1
        now[0] += 301
        with pytest.raises(DisplayError, match="TV unreachable"):
            await disp.ready()
    assert len(lan.asked) == 2
    assert "moved" not in store.frame(TV_ID) and notice(hass) is None


async def test_id_without_a_mac_is_never_searched(hass, store, lan) -> None:
    disp = display(hass, store, tv_id="samsung:192.168.0.20")
    with patch(MODE_SYNC, side_effect=TV(NEW).mode_sync), pytest.raises(DisplayError):
        await disp.ready()
    assert lan.asked == []


async def test_refused_host_is_never_searched(hass, store, lan) -> None:
    disp = display(hass, store, host="93.184.215.14")
    with pytest.raises(HostRefused):
        await disp.ready()
    assert lan.asked == []


async def test_runner_probes_the_followed_address(hass, store) -> None:
    spec = parse_spec(
        {
            "version": 1,
            "tv": {"id": TV_ID, "kind": "frame", "host": OLD, "name": "Frame"},
            "interval_seconds": 180,
            "source": {"type": "board", "board": {"type_id": "weather", "params": {}}},
        }
    )
    store.frame(TV_ID)["moved"] = {"from": OLD, "to": NEW}
    runner = TVRunner(
        hass, AsyncMock(), None, store, spec, display(hass, store), lambda: None, lambda: None
    )
    assert runner.address == NEW
    probed: list[str] = []

    async def probe(host, port):
        probed.append(host)
        return True

    runner._probe, runner._sleep = probe, AsyncMock()
    runner._failures, runner._unreachable = 1, True
    await runner._wait()
    assert probed and set(probed) == {NEW}


def test_mac_from_tv_id() -> None:
    assert mac_from_tv_id(TV_ID) == MAC
    assert mac_from_tv_id("samsung:00-00-5E-00-53-01") == MAC
    assert mac_from_tv_id("samsung:192.168.0.20") is None
    assert mac_from_tv_id("tv-1") is None
    assert mac_from_tv_id("samsung:00:00:5e-00:53:01") is None  # mixed separators


def test_read_neighbours_keeps_complete_entries_only(tmp_path) -> None:
    table = tmp_path / "arp"
    table.write_text(
        "IP address       HW type     Flags       HW address            Mask     Device\n"
        "192.168.0.21   0x1         0x2         00:00:5e:00:53:01     *        end0\n"
        "192.168.0.20   0x1         0x0         00:00:00:00:00:00     *        end0\n"
        "192.168.0.66    0x1         0x6         AA:BB:CC:DD:EE:FF     *        end0\n"
    )
    assert read_neighbours(str(table)) == {NEW: MAC, SPOOF: OTHER}


async def test_check_asks_http_first_then_the_neighbour_table(hass, lan) -> None:
    assert (await locate.check(hass, NEW, MAC)).check is Check.OK
    assert (await locate.check(hass, OLD, MAC)).check is Check.NOT_IT
    lan.http["fd00::5"] = MAC
    found = await locate.check(hass, "fd00::5", MAC)
    assert found.check is Check.CLAIM_ONLY and "IPv6" in found.why


async def test_find_frame_asks_home_addresses_only(hass, lan) -> None:
    lan.discovered = ["93.184.215.14", OLD, "192.168.0.10", NEW]
    lan.http = {"192.168.0.10": OTHER, NEW: MAC}
    found = await find_frame(hass, MAC, skip=OLD)
    assert found.address == NEW and found.check is Check.OK
    assert lan.asked == ["192.168.0.10", NEW]  # never the public one, nor the failed one
    locate.mdns_addresses.assert_not_awaited()  # HA's SSDP cache had it: no mDNS traffic


async def test_find_frame_falls_back_to_mdns(hass, lan) -> None:
    lan.discovered = []
    locate.mdns_addresses.return_value = [NEW]
    assert (await find_frame(hass, MAC, skip=OLD)).address == NEW


async def test_find_frame_without_discovery_finds_nothing(hass, lan) -> None:
    lan.discovered = []
    locate.mdns_addresses.side_effect = OSError("no mdns")
    assert await find_frame(hass, MAC) is None


async def test_ssdp_addresses_reads_hosts_from_has_cache(hass, monkeypatch) -> None:
    class Info:
        def __init__(self, location):
            self.ssdp_location = location

    cache = {
        "urn:dial-multiscreen-org:service:dial:1": [Info(f"http://{NEW}:7678/nservice/")],
        "urn:schemas-upnp-org:device:MediaRenderer:1": [
            Info(f"http://{NEW}:9197/dmr"),
            Info("http://192.168.0.40:60000/dd.xml"),
            Info(None),
        ],
    }

    async def by_st(_hass, st):
        return cache.get(st, [])

    # The test venv has no async_upnp_client, so HA's ssdp can't be imported here.
    fake_ssdp = types.ModuleType("homeassistant.components.ssdp")
    fake_ssdp.async_get_discovery_info_by_st = by_st
    monkeypatch.setitem(sys.modules, "homeassistant.components.ssdp", fake_ssdp)
    assert await locate.ssdp_addresses(hass) == [NEW, "192.168.0.40"]


async def test_reported_mac_reads_api_v2(hass, aioclient_mock) -> None:
    aioclient_mock.get(
        f"http://{NEW}:8001/api/v2/",
        json={"device": {"wifiMac": "00:00:5E:00:53:01", "PowerState": "on"}},
    )
    aioclient_mock.get(f"http://{OLD}:8001/api/v2/", status=404)
    assert await reported_mac(hass, NEW) == MAC
    assert await reported_mac(hass, OLD) is None
    assert await reported_mac(hass, "93.184.215.14") is None  # never asked
    assert [str(call[1]) for call in aioclient_mock.mock_calls] == [
        f"http://{NEW}:8001/api/v2/",
        f"http://{OLD}:8001/api/v2/",
    ]


RELEASE_SYNC = "custom_components.tvcanvas.display_frame._release_sync"


async def test_show_uses_the_address_ready_checked(hass, store, lan) -> None:
    store.frame(TV_ID)["moved"] = {"from": OLD, "to": NEW}
    disp, tv = display(hass, store), TV(NEW)
    with patch(MODE_SYNC, side_effect=tv.mode_sync):
        assert await disp.ready() is None
    checked = list(lan.asked)
    with patch(SHOW_SYNC, return_value=("MY_F0001", None, [], [])) as show:
        await disp.show(b"J")
    assert show.call_args[0][0] == NEW
    assert lan.asked == checked  # reused, not re-read


async def test_show_with_a_moved_record_that_fails_the_check_sends_nothing(
    hass, store, lan
) -> None:
    store.frame(TV_ID)["moved"] = {"from": OLD, "to": NEW}
    store.set_frame_token(NEW, "T-NEW")
    lan.arp = {NEW: OTHER}  # something else answers at the followed address now
    disp = display(hass, store)
    with patch(SHOW_SYNC) as show, pytest.raises(DisplayError):
        await disp.show(b"J")
    show.assert_not_called()
    assert "moved" not in store.frame(TV_ID)


@pytest.mark.parametrize("forget_first", [False, True])
async def test_release_with_a_moved_record_that_fails_the_check_sends_nothing(
    hass, store, lan, forget_first
) -> None:
    store.frame(TV_ID).update(current="MY_F0001", moved={"from": OLD, "to": NEW})
    store.set_frame_token(NEW, "T-NEW")
    lan.arp = {NEW: OTHER}
    disp = display(hass, store)
    with patch(RELEASE_SYNC, return_value=[]) as release:
        disp.release()
        if forget_first:  # what the coordinator does right after release()
            store.forget_tv(TV_ID)
        await hass.async_block_till_done()
    release.assert_not_called()


async def test_release_goes_to_the_checked_followed_address(hass, store, lan) -> None:
    store.frame(TV_ID).update(current="MY_F0001", moved={"from": OLD, "to": NEW})
    store.set_frame_token(OLD, "SAVED")
    store.set_frame_token(NEW, "T-NEW")
    disp = display(hass, store)
    with patch(RELEASE_SYNC, return_value=["MY_F0001"]) as release:
        disp.release()
        store.forget_tv(TV_ID)
        await hass.async_block_till_done()
    release.assert_called_once_with(NEW, "T-NEW", ["MY_F0001"])
