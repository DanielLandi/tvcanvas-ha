from __future__ import annotations

from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.tvcanvas.storage import StateStore


async def test_round_trip(hass, hass_storage) -> None:
    store = StateStore(hass, "entry1")
    await store.async_load()
    assert store.specs == [] and store.etag is None
    store.specs = [{"a": 1}]
    store.etag = '"e"'
    store.rotation("tv-1")["pos"] = 2
    store.frame("tv-1")["current"] = "MY_F0001"
    store.set_frame_token("10.0.0.2", "12345")
    await store.async_save_now()

    again = StateStore(hass, "entry1")
    await again.async_load()
    assert again.specs == [{"a": 1}] and again.etag == '"e"'
    assert again.rotation("tv-1") == {"pos": 2}
    assert again.frame("tv-1")["current"] == "MY_F0001"
    assert again.frame_token("10.0.0.2") == "12345"

    again.forget_tv("tv-1")
    assert again.rotation("tv-1") == {} and again.frame("tv-1") == {"current": None, "stale": []}


async def test_store_is_private(hass, hass_storage) -> None:
    # Frame pairing tokens authorize control of the TV: the file must not be world-readable.
    store = StateStore(hass, "entry1")
    await store.async_load()
    assert store._store._private is True


async def test_debounced_save_persists_live_mutation(hass, hass_storage, freezer) -> None:
    store = StateStore(hass, "entry1")
    await store.async_load()
    store.rotation("tv-1")["pos"] = 7
    store.schedule_save()

    freezer.tick(11)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass_storage["tvcanvas.entry1"]["data"]["rotation"]["tv-1"] == {"pos": 7}


async def test_forget_tv_leaves_other_tvs_untouched(hass, hass_storage) -> None:
    store = StateStore(hass, "entry1")
    await store.async_load()
    store.rotation("tv-1")["pos"] = 1
    store.frame("tv-1")["current"] = "A_F0001"
    store.rotation("tv-2")["pos"] = 2
    store.frame("tv-2")["current"] = "B_F0002"
    store.frame("tv-2")["stale"].append("B_F0001")

    store.forget_tv("tv-1")

    assert store.rotation("tv-2") == {"pos": 2}
    assert store.frame("tv-2") == {"current": "B_F0002", "stale": ["B_F0001"]}


async def test_load_partial_payload_fills_defaults(hass, hass_storage) -> None:
    hass_storage["tvcanvas.entry1"] = {
        "version": 1,
        "minor_version": 1,
        "key": "tvcanvas.entry1",
        "data": {"etag": "x"},
    }
    store = StateStore(hass, "entry1")
    await store.async_load()

    assert store.etag == "x"
    assert store.specs == []
    assert store.rotation("tv-1") == {}
    assert store.frame("tv-1") == {"current": None, "stale": []}
    assert store.frame_token("10.0.0.2") is None


async def test_async_remove_deletes_the_file(hass, hass_storage) -> None:
    store = StateStore(hass, "entry1")
    await store.async_load()
    store.set_frame_token("10.0.0.2", "12345")
    await store.async_save_now()
    assert "tvcanvas.entry1" in hass_storage
    await store.async_remove()
    assert "tvcanvas.entry1" not in hass_storage
