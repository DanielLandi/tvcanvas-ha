from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.tvcanvas import async_remove_config_entry_device, async_unload_entry
from custom_components.tvcanvas.const import DOMAIN, SYNC_INTERVAL
from custom_components.tvcanvas.display_cast import CastDisplay

from .common import URL, entry, frame_spec


def patch_runner():
    """Keep real reconcile logic but stop runners from touching TVs."""
    return patch("custom_components.tvcanvas.runner.TVRunner.start")


async def setup(hass, aioclient_mock, specs, etag='"e1"', status=200, days_left=20):
    if status == 200:
        aioclient_mock.get(f"{URL}/ha/agent/sync", json={"specs": specs}, headers={"ETag": etag})
    else:
        aioclient_mock.get(f"{URL}/ha/agent/sync", status=status)
    e = entry(hass, days_left)
    with patch_runner():
        await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    return e


async def test_setup_starts_one_runner_per_valid_tv(hass, aioclient_mock) -> None:
    bad = frame_spec("tv-2")
    bad["version"] = 2  # Review focus 5: from a newer cloud
    e = await setup(
        hass,
        aioclient_mock,
        [
            frame_spec("tv-1"),
            bad,
            {**frame_spec("tv-3"), "tv": {**frame_spec("tv-3")["tv"], "kind": "roku"}},
        ],
    )
    assert e.state is ConfigEntryState.LOADED
    assert set(e.runtime_data.runners) == {"tv-1"}


async def test_reconcile_add_change_remove(hass, aioclient_mock) -> None:
    e = await setup(hass, aioclient_mock, [frame_spec("tv-1"), frame_spec("tv-2")])
    coord = e.runtime_data
    first_runner = coord.runners["tv-1"]
    seen = []
    coord.async_add_runner_listener(seen.append)
    seen.clear()

    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{URL}/ha/agent/sync",
        json={"specs": [frame_spec("tv-1", type_id="flights"), frame_spec("tv-3")]},
        headers={"ETag": '"e2"'},
    )
    with (
        patch_runner(),
        patch("custom_components.tvcanvas.runner.TVRunner.update", new=AsyncMock()) as upd,
        patch("custom_components.tvcanvas.runner.TVRunner.stop", new=AsyncMock()) as stop,
    ):
        await coord.async_refresh()
    assert set(coord.runners) == {"tv-1", "tv-3"} and coord.runners["tv-1"] is first_runner
    assert seen == [coord.runners["tv-3"]]
    assert upd.await_count == 1 and stop.await_count == 1


async def test_304_keeps_specs(hass, aioclient_mock) -> None:
    e = await setup(hass, aioclient_mock, [frame_spec("tv-1")])
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/ha/agent/sync", status=304)
    with patch_runner():
        await e.runtime_data.async_refresh()
    assert set(e.runtime_data.runners) == {"tv-1"}
    assert aioclient_mock.mock_calls[0][3]["If-None-Match"] == '"e1"'


async def test_cloud_down_at_startup_uses_persisted_specs(
    hass, aioclient_mock, hass_storage
) -> None:
    """Review focus 3."""
    e = await setup(hass, aioclient_mock, [frame_spec("tv-1")])
    await e.runtime_data._store.async_save_now()
    await hass.config_entries.async_unload(e.entry_id)

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/ha/agent/sync", exc=TimeoutError())
    with patch_runner():
        await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    assert e.state is ConfigEntryState.LOADED
    assert set(e.runtime_data.runners) == {"tv-1"}


async def test_revoked_token_starts_reauth(hass, aioclient_mock) -> None:
    e = await setup(hass, aioclient_mock, [], status=401)
    assert e.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert flows and flows[0]["context"]["source"] == "reauth"


async def test_token_refreshed_when_close_to_expiry(hass, aioclient_mock) -> None:
    aioclient_mock.post(
        f"{URL}/ha/agent/refresh", json={"token": "fc_ha_new", "expires_at": "2030-01-01T00:00:00Z"}
    )
    e = await setup(hass, aioclient_mock, [], days_left=3)
    assert e.data["token"] == "fc_ha_new"
    assert e.data["token_expires_at"].startswith("2030-01-01T00:00:00")
    assert e.runtime_data._client.token == "fc_ha_new"


async def test_unload_stops_runners(hass, aioclient_mock) -> None:
    e = await setup(hass, aioclient_mock, [frame_spec("tv-1")])
    with patch("custom_components.tvcanvas.runner.TVRunner.stop", new=AsyncMock()) as stop:
        assert await hass.config_entries.async_unload(e.entry_id)
    assert stop.await_count == 1


async def test_polling_continues_without_entities(hass, aioclient_mock, freezer) -> None:
    """A coordinator with no listeners stops polling; setup must keep one."""
    e = await setup(hass, aioclient_mock, [])
    assert e.runtime_data.runners == {}
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{URL}/ha/agent/sync", json={"specs": [frame_spec("tv-1")]}, headers={"ETag": '"e2"'}
    )
    freezer.tick(SYNC_INTERVAL + timedelta(seconds=5))
    with patch_runner():
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert set(e.runtime_data.runners) == {"tv-1"}


async def test_malformed_spec_elements_are_skipped(hass, aioclient_mock) -> None:
    e = await setup(
        hass,
        aioclient_mock,
        [
            None,
            "x",
            5,
            {"version": 1, "tv": "abc"},
            {"version": 1, "tv": {"id": 3}},
            frame_spec("tv-1"),
        ],
    )
    assert e.state is ConfigEntryState.LOADED
    assert set(e.runtime_data.runners) == {"tv-1"}


async def test_no_reconcile_after_shutdown(hass, aioclient_mock) -> None:
    e = await setup(hass, aioclient_mock, [frame_spec("tv-1")])
    coord = e.runtime_data
    await coord.async_shutdown_runners()
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{URL}/ha/agent/sync",
        json={"specs": [frame_spec("tv-1", type_id="flights"), frame_spec("tv-2")]},
        headers={"ETag": '"e2"'},
    )
    with (
        patch("custom_components.tvcanvas.runner.TVRunner.start") as start,
        patch("custom_components.tvcanvas.runner.TVRunner.update", new=AsyncMock()) as upd,
    ):
        await coord.async_refresh()
    assert not start.called and upd.await_count == 0
    assert set(coord.runners) == {"tv-1"}


async def test_shutdown_during_reconcile_starts_nothing_new(hass, aioclient_mock) -> None:
    e = await setup(hass, aioclient_mock, [frame_spec("tv-1"), frame_spec("tv-2")])
    coord = e.runtime_data
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{URL}/ha/agent/sync",
        json={
            "specs": [
                frame_spec("tv-1", type_id="flights"),
                frame_spec("tv-2", type_id="flights"),
                frame_spec("tv-3"),
            ]
        },
        headers={"ETag": '"e2"'},
    )

    async def parked_update(spec, display):
        # unload/reauth lands while the reconcile is parked in this update
        await coord.async_shutdown_runners()

    with (
        patch("custom_components.tvcanvas.runner.TVRunner.start") as start,
        patch(
            "custom_components.tvcanvas.runner.TVRunner.update",
            new=AsyncMock(side_effect=parked_update),
        ) as upd,
    ):
        await coord.async_refresh()
    assert upd.await_count == 1
    assert not start.called
    assert set(coord.runners) == {"tv-1", "tv-2"}


async def test_unparseable_expiry_triggers_refresh(hass, aioclient_mock) -> None:
    aioclient_mock.post(
        f"{URL}/ha/agent/refresh",
        json={"token": "fc_ha_new", "expires_at": "2030-01-01T00:00:00Z"},
    )
    aioclient_mock.get(f"{URL}/ha/agent/sync", json={"specs": []}, headers={"ETag": '"e1"'})
    e = entry(hass)
    hass.config_entries.async_update_entry(e, data={**e.data, "token_expires_at": "garbage"})
    with patch_runner():
        await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    assert e.state is ConfigEntryState.LOADED
    assert e.data["token"] == "fc_ha_new"


async def test_naive_expiry_is_treated_as_utc(hass, aioclient_mock) -> None:
    aioclient_mock.get(f"{URL}/ha/agent/sync", json={"specs": []}, headers={"ETag": '"e1"'})
    e = entry(hass)
    naive = (dt_util.utcnow() + timedelta(days=20)).replace(tzinfo=None).isoformat()
    hass.config_entries.async_update_entry(e, data={**e.data, "token_expires_at": naive})
    with patch_runner():
        await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    assert e.state is ConfigEntryState.LOADED
    assert e.data["token"] == "fc_ha_t"


async def test_refresh_auth_error_stops_runners_and_reauths(hass, aioclient_mock) -> None:
    e = await setup(hass, aioclient_mock, [frame_spec("tv-1")])
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{URL}/ha/agent/refresh", status=401)
    hass.config_entries.async_update_entry(
        e, data={**e.data, "token_expires_at": (dt_util.utcnow() + timedelta(days=1)).isoformat()}
    )
    with patch("custom_components.tvcanvas.runner.TVRunner.stop", new=AsyncMock()) as stop:
        await e.runtime_data.async_refresh()
        await hass.async_block_till_done()
    assert stop.await_count == 1
    flows = hass.config_entries.flow.async_progress()
    assert flows and flows[0]["context"]["source"] == "reauth"


async def test_handle_auth_error_is_idempotent(hass, aioclient_mock) -> None:
    e = await setup(hass, aioclient_mock, [frame_spec("tv-1")])
    coord = e.runtime_data
    with patch.object(e, "async_start_reauth") as reauth:
        coord.handle_auth_error()
        coord.handle_auth_error()
        await hass.async_block_till_done()
    assert reauth.call_count == 1


async def test_failed_platform_unload_keeps_runners(hass, aioclient_mock) -> None:
    e = await setup(hass, aioclient_mock, [frame_spec("tv-1")])
    with (
        patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)),
        patch("custom_components.tvcanvas.runner.TVRunner.stop", new=AsyncMock()) as stop,
    ):
        assert await async_unload_entry(hass, e) is False
    assert stop.await_count == 0


async def test_removed_tv_dismisses_pairing_notice(hass, aioclient_mock) -> None:
    e = await setup(hass, aioclient_mock, [frame_spec("tv-1"), frame_spec("tv-2")])
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{URL}/ha/agent/sync", json={"specs": [frame_spec("tv-1")]}, headers={"ETag": '"e2"'}
    )
    with (
        patch("custom_components.tvcanvas.runner.TVRunner.stop", new=AsyncMock()),
        patch(
            "custom_components.tvcanvas.coordinator.persistent_notification.async_dismiss"
        ) as dis,
    ):
        await e.runtime_data.async_refresh()
    assert [c.args[1] for c in dis.call_args_list] == [
        "tvcanvas_pair_tv-2",
        "tvcanvas_moved_tv-2",
    ]


async def test_unload_drops_cast_images_dismisses_and_flushes(
    hass, aioclient_mock, hass_storage
) -> None:
    e = await setup(hass, aioclient_mock, [frame_spec("tv-1")])
    cache = hass.data[DOMAIN]["cache"]
    with (
        patch.object(cache, "drop_tv") as drop,
        patch(
            "custom_components.tvcanvas.coordinator.persistent_notification.async_dismiss"
        ) as dis,
    ):
        assert await hass.config_entries.async_unload(e.entry_id)
    drop.assert_called_once_with("tv-1")
    assert [c.args[1] for c in dis.call_args_list] == ["tvcanvas_pair_tv-1"]
    assert any(e.entry_id in key for key in hass_storage)


async def test_removing_the_entry_deletes_the_private_store(
    hass, aioclient_mock, hass_storage
) -> None:
    e = await setup(hass, aioclient_mock, [frame_spec("tv-1")])
    e.runtime_data._store.set_frame_token("10.0.0.2", "PAIRING")
    assert await hass.config_entries.async_unload(e.entry_id)
    assert f"{DOMAIN}.{e.entry_id}" in hass_storage
    await hass.config_entries.async_remove(e.entry_id)
    await hass.async_block_till_done()
    assert f"{DOMAIN}.{e.entry_id}" not in hass_storage


async def test_removed_tv_releases_its_display_and_kept_ones_do_not(hass, aioclient_mock) -> None:
    e = await setup(hass, aioclient_mock, [frame_spec("tv-1"), frame_spec("tv-2")])
    coord = e.runtime_data
    released = []
    for tv_id, runner in coord.runners.items():
        runner._display.release = lambda tv_id=tv_id: released.append(tv_id)
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{URL}/ha/agent/sync", json={"specs": [frame_spec("tv-1")]}, headers={"ETag": '"e2"'}
    )
    with patch("custom_components.tvcanvas.runner.TVRunner.stop", new=AsyncMock()):
        await coord.async_refresh()
    assert released == ["tv-2"]


async def test_unload_releases_nothing(hass, aioclient_mock) -> None:
    e = await setup(hass, aioclient_mock, [frame_spec("tv-1")])
    released = []
    e.runtime_data.runners["tv-1"]._display.release = lambda: released.append(1)
    await hass.config_entries.async_unload(e.entry_id)
    assert released == []


# --- 2026-11-09 review ------------------------------------------------------


async def test_unparseable_spec_warns_once_not_every_poll(hass, aioclient_mock, caplog) -> None:
    """A spec from a newer app is re-parsed every 60 s poll: warn once, not each minute."""
    bad = frame_spec("tv-2")
    bad["version"] = 2
    e = await setup(hass, aioclient_mock, [frame_spec("tv-1"), bad])
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/ha/agent/sync", status=304)
    with patch_runner():
        await e.runtime_data.async_refresh()
        await e.runtime_data.async_refresh()
    assert caplog.text.count("skipping TV tv-2") == 1


async def test_fixed_then_broken_spec_warns_again(hass, aioclient_mock, caplog) -> None:
    bad = frame_spec("tv-1")
    bad["version"] = 2
    e = await setup(hass, aioclient_mock, [bad])
    for specs, etag in (([frame_spec("tv-1")], "e2"), ([bad], "e3")):
        aioclient_mock.clear_requests()
        aioclient_mock.get(
            f"{URL}/ha/agent/sync", json={"specs": specs}, headers={"ETag": f'"{etag}"'}
        )
        with (
            patch_runner(),
            patch("custom_components.tvcanvas.runner.TVRunner.stop", new=AsyncMock()),
        ):
            await e.runtime_data.async_refresh()
    assert caplog.text.count("skipping TV tv-1") == 2


async def test_cloud_down_warns_once_until_it_answers(hass, aioclient_mock, caplog) -> None:
    e = await setup(hass, aioclient_mock, [frame_spec("tv-1")])
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/ha/agent/sync", exc=TimeoutError())
    with patch_runner():
        await e.runtime_data.async_refresh()
        await e.runtime_data.async_refresh()
    assert caplog.text.count("running the last known specs") == 1
    assert set(e.runtime_data.runners) == {"tv-1"}


async def test_failed_token_refresh_keeps_the_token_and_warns_once(
    hass, aioclient_mock, caplog
) -> None:
    aioclient_mock.post(f"{URL}/ha/agent/refresh", status=500)
    e = await setup(hass, aioclient_mock, [], days_left=3)
    with patch_runner():
        await e.runtime_data.async_refresh()
    assert e.data["token"] == "fc_ha_t"
    assert caplog.text.count("token refresh failed") == 1


def refreshes(aioclient_mock) -> int:
    return sum(1 for _m, u, *_ in aioclient_mock.mock_calls if str(u).endswith("/ha/agent/refresh"))


async def test_a_failed_token_refresh_waits_15_minutes_then_an_hour(
    hass, aioclient_mock, freezer
) -> None:
    """#17 item 4: the cloud allows 5 refreshes an hour, and HA polls every minute."""
    aioclient_mock.post(f"{URL}/ha/agent/refresh", status=429)
    e = await setup(hass, aioclient_mock, [], days_left=3)
    coord = e.runtime_data
    assert refreshes(aioclient_mock) == 1

    async def poll(minutes: int) -> None:
        for _ in range(minutes):
            freezer.tick(timedelta(minutes=1))
            with patch_runner():
                await coord.async_refresh()

    await poll(14)
    assert refreshes(aioclient_mock) == 1
    await poll(1)  # 15 minutes after the first failure
    assert refreshes(aioclient_mock) == 2
    await poll(59)
    assert refreshes(aioclient_mock) == 2
    await poll(1)  # then once an hour
    assert refreshes(aioclient_mock) == 3
    await poll(60)
    assert refreshes(aioclient_mock) == 4
    assert e.data["token"] == "fc_ha_t"


async def test_a_token_refresh_that_works_again_is_used_and_resets_the_wait(
    hass, aioclient_mock, freezer
) -> None:
    aioclient_mock.post(f"{URL}/ha/agent/refresh", status=503)
    e = await setup(hass, aioclient_mock, [], days_left=3)
    coord = e.runtime_data
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/ha/agent/sync", json={"specs": []})
    aioclient_mock.post(
        f"{URL}/ha/agent/refresh", json={"token": "fc_ha_new", "expires_at": "2030-01-01T00:00:00Z"}
    )
    freezer.tick(timedelta(minutes=15))
    with patch_runner():
        await coord.async_refresh()
    assert e.data["token"] == "fc_ha_new"
    assert coord._refresh_retry_at is None


async def test_at_most_20_tvs_run_and_the_rest_are_warned_about_once(
    hass, aioclient_mock, caplog
) -> None:
    """#17 item 2: the cloud allows 10 TVs; a broken or hacked one may send thousands."""
    specs = [frame_spec(f"tv-{i}") for i in range(25)]
    e = await setup(hass, aioclient_mock, specs)
    assert set(e.runtime_data.runners) == {f"tv-{i}" for i in range(20)}
    with patch_runner():
        await e.runtime_data.async_refresh()
    assert caplog.text.count("runs at most 20 TVs") == 1


async def test_cast_spec_gets_a_cast_display(hass, aioclient_mock) -> None:
    cast = frame_spec("tv-c")
    cast["tv"]["kind"] = "cast"
    e = await setup(hass, aioclient_mock, [cast])
    assert isinstance(e.runtime_data.runners["tv-c"]._display, CastDisplay)


async def test_pairing_notice_is_shown_then_dismissed(hass, aioclient_mock) -> None:
    e = await setup(hass, aioclient_mock, [frame_spec("tv-1")])
    notify = e.runtime_data._pairing_notice(e.runtime_data.runners["tv-1"].spec.tv)
    with (
        patch("custom_components.tvcanvas.coordinator.persistent_notification.async_create") as cr,
        patch(
            "custom_components.tvcanvas.coordinator.persistent_notification.async_dismiss"
        ) as dis,
    ):
        notify(True)
        notify(False)
    assert "TV tv-1" in cr.call_args.args[1]
    assert cr.call_args.kwargs["notification_id"] == "tvcanvas_pair_tv-1"
    assert dis.call_args.args[1] == "tvcanvas_pair_tv-1"


async def test_only_a_device_for_a_tv_no_longer_run_can_be_removed(hass, aioclient_mock) -> None:
    """Stale devices (quality scale): the phone took tv-2 back, so the user may delete it."""
    e = await setup(hass, aioclient_mock, [frame_spec("tv-1"), frame_spec("tv-2")])
    devices = dr.async_get(hass)
    running = devices.async_get_device_by_identifier((DOMAIN, f"{e.entry_id}_tv-1"), e.entry_id)
    taken_back = devices.async_get_device_by_identifier((DOMAIN, f"{e.entry_id}_tv-2"), e.entry_id)
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{URL}/ha/agent/sync", json={"specs": [frame_spec("tv-1")]}, headers={"ETag": '"e2"'}
    )
    with patch("custom_components.tvcanvas.runner.TVRunner.stop", new=AsyncMock()):
        await e.runtime_data.async_refresh()
    assert await async_remove_config_entry_device(hass, e, running) is False
    assert await async_remove_config_entry_device(hass, e, taken_back) is True


async def test_odd_malformed_specs_warn_once_each(hass, aioclient_mock, caplog) -> None:
    """Ids can be missing or any JSON (even unhashable); each bad spec warns once."""
    bad = [{"version": 1, "tv": {"id": ["x"]}}, {"version": 9}, {"version": 1, "tv": "abc"}]
    e = await setup(hass, aioclient_mock, [*bad, frame_spec("tv-1")])
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/ha/agent/sync", status=304)
    with patch_runner():
        await e.runtime_data.async_refresh()
    assert caplog.text.count("skipping TV") == 3
    assert set(e.runtime_data.runners) == {"tv-1"}
