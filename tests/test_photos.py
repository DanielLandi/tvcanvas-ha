"""Phone photos (`local` group items): spec parsing, the store, the rotation, the status."""

from __future__ import annotations

import hashlib
import io
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image

from custom_components.tvcanvas.models import SpecError, parse_spec, referenced_photos
from custom_components.tvcanvas.photos import PhotosFull, PhotoStore, photos_dir
from custom_components.tvcanvas.runner import TVRunner
from custom_components.tvcanvas.sources import SourceError, WaitingForPhone, next_image
from custom_components.tvcanvas.storage import StateStore

from .common import URL, entry, frame_spec


def image_bytes(color=(10, 20, 30), fmt="JPEG", size=(8, 8)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, fmt)
    return out.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def local(data_or_hash, name="IMG"):
    h = data_or_hash if isinstance(data_or_hash, str) else sha(data_or_hash)
    return {"type": "local", "sha256": h, "name": name}


CAM = {"type": "webcam", "url": "https://img/cam.jpg", "name": "Cam"}
BOARD = {"type": "board", "board": {"type_id": "weather", "params": {}}}


def group_raw(items, tv_id="t"):
    return {
        "version": 1,
        "tv": {"id": tv_id, "kind": "frame", "host": "10.0.0.2", "name": "T"},
        "interval_seconds": 60,
        "source": {"type": "group", "group": {"items": items}},
    }


def group(items):
    return parse_spec(group_raw(items))


A, B, C = image_bytes((255, 0, 0)), image_bytes((0, 255, 0)), image_bytes((0, 0, 255))


@pytest.fixture
async def photos(hass) -> PhotoStore:
    store = PhotoStore(hass)
    await store.async_load()
    return store


# --- the spec -------------------------------------------------------------------


def test_local_item_parses_and_a_group_may_be_all_local() -> None:
    spec = group([local(A, "IMG_0042"), local(B, "IMG_0043")])
    first = spec.source.items[0]
    assert first.type == "local" and first.sha256 == sha(A) and first.label == "IMG_0042"
    assert spec.source.local_hashes == {sha(A), sha(B)}


@pytest.mark.parametrize(
    "item",
    [
        {"type": "local", "sha256": "A" * 64, "name": "x"},  # uppercase
        {"type": "local", "sha256": "a" * 63, "name": "x"},
        {"type": "local", "sha256": "a" * 64 + "\n", "name": "x"},
        {"type": "local", "sha256": "../" + "a" * 61, "name": "x"},
        {"type": "local", "sha256": 7, "name": "x"},
        {"type": "local", "sha256": "a" * 64},  # no name
        {"type": "local", "sha256": "a" * 64, "name": ""},
        {"type": "local", "sha256": "a" * 64, "name": "x" * 257},
        {"type": "local", "sha256": "a" * 64, "name": "x", "url": "https://img/a.jpg"},
    ],
)
def test_bad_local_items_reject_the_spec(item) -> None:
    with pytest.raises(SpecError):
        group([item])


def test_local_items_count_toward_the_100_cap() -> None:
    group([CAM] + [local(f"{i:064x}") for i in range(99)])
    with pytest.raises(SpecError, match="at most 100"):
        group([CAM] + [local(f"{i:064x}") for i in range(100)])


def test_local_items_change_the_rotation_key() -> None:
    assert group([local(A)]).source.items_key != group([local(B)]).source.items_key


def test_referenced_photos_reads_raw_specs_even_unparseable_ones() -> None:
    broken = group_raw([local(C), {"type": "hologram"}])
    raws = [group_raw([local(A), CAM]), broken, frame_spec(), "junk", {"source": None}]
    assert referenced_photos(raws) == {sha(A), sha(C)}


# --- the store ------------------------------------------------------------------


async def test_put_writes_atomically_named_by_hash(hass, photos) -> None:
    assert await photos.async_put(sha(A), A) is True
    assert await photos.async_put(sha(A), A) is False  # already had it
    path = photos_dir(hass) / f"{sha(A)}.jpg"
    assert path.read_bytes() == A
    assert [p.name for p in photos_dir(hass).iterdir()] == [path.name]  # no temp files left
    assert photos.hashes == [sha(A)] and photos.bytes_used == len(A) and photos.files == 1
    assert await photos.async_read(sha(A)) == A


async def test_store_reloads_from_disk_and_drops_stray_files(hass, photos) -> None:
    await photos.async_put(sha(A), A)
    folder = photos_dir(hass)
    (folder / ".upload-abc").write_bytes(b"half")
    (folder / "notes.txt").write_bytes(b"x")
    again = PhotoStore(hass)
    await again.async_load()
    assert again.hashes == [sha(A)]
    assert not (folder / ".upload-abc").exists()


async def test_caps(photos, monkeypatch) -> None:
    monkeypatch.setattr("custom_components.tvcanvas.photos.PHOTOS_FILES_CAP", 2)
    await photos.async_put(sha(A), A)
    await photos.async_put(sha(B), B)
    with pytest.raises(PhotosFull):
        await photos.async_put(sha(C), C)
    monkeypatch.setattr("custom_components.tvcanvas.photos.PHOTOS_FILES_CAP", 400)
    monkeypatch.setattr(
        "custom_components.tvcanvas.photos.PHOTOS_BYTES_CAP", len(A) + len(B) + len(C) - 1
    )
    with pytest.raises(PhotosFull):
        await photos.async_put(sha(C), C)


async def test_paths_are_only_ever_built_from_a_hash(photos) -> None:
    with pytest.raises(ValueError):
        await photos.async_put("../../secrets", A)
    assert await photos.async_read("../../secrets") is None
    assert "../x" not in photos


async def test_gc_deletes_unreferenced_photos_only_once_10_minutes_old(
    hass, photos, freezer
) -> None:
    await photos.async_put(sha(A), A)
    await photos.async_put(sha(B), B)
    freezer.tick(599)
    assert await photos.async_gc({sha(A)}) == 0  # B is too young
    await photos.async_put(sha(C), C)
    freezer.tick(2)
    assert await photos.async_gc({sha(A)}) == 1
    assert photos.hashes == sorted([sha(A), sha(C)])
    assert not (photos_dir(hass) / f"{sha(B)}.jpg").exists()
    # Pushing it again restarts its clock.
    freezer.tick(598)
    await photos.async_put(sha(C), C)
    freezer.tick(5)
    assert await photos.async_gc(set()) == 1  # only A
    assert photos.hashes == [sha(C)]


# --- the rotation ---------------------------------------------------------------


@pytest.fixture
def fetched(monkeypatch):
    calls = []

    async def fake_fetch(session, url):
        calls.append(url)
        return url.encode()

    monkeypatch.setattr("custom_components.tvcanvas.sources.fetch_image", fake_fetch)
    return calls


async def identity_fit(data, orientation):
    assert orientation is None  # group items are never filtered
    return data


async def test_rotation_shows_present_photos_and_skips_missing_ones(photos, fetched) -> None:
    await photos.async_put(sha(A), A)
    spec = group([local(A, "a"), local(B, "b"), CAM])
    rot, shown = {}, []
    for _ in range(4):
        data, label = await next_image(spec, rot, None, None, identity_fit, None, photos)
        shown.append(label)
    assert shown == ["a", "Cam", "a", "Cam"]


async def test_a_photo_that_arrives_joins_the_rotation_in_place(photos, fetched) -> None:
    await photos.async_put(sha(A), A)
    spec = group([local(A, "a"), local(B, "b"), CAM])
    rot = {}
    assert (await next_image(spec, rot, None, None, identity_fit, None, photos))[1] == "a"
    await photos.async_put(sha(B), B)
    data, label = await next_image(spec, rot, None, None, identity_fit, None, photos)
    assert (data, label) == (B, "b")


async def test_all_local_group_with_nothing_present_waits(photos) -> None:
    spec = group([local(A), local(B)])
    rot = {}
    with pytest.raises(WaitingForPhone):
        await next_image(spec, rot, None, None, identity_fit, None, photos)
    assert rot["pos"] == 0


async def test_without_a_store_every_photo_is_missing(fetched) -> None:
    with pytest.raises(WaitingForPhone):
        await next_image(group([local(A)]), {}, None, None, identity_fit)
    _, label = await next_image(group([local(A), CAM]), {}, None, None, identity_fit)
    assert label == "Cam"


async def test_missing_photos_do_not_use_up_the_five_tries(photos, fetched) -> None:
    items = [local(f"{i:064x}") for i in range(8)] + [CAM]
    _, label = await next_image(group(items), {}, None, None, identity_fit, None, photos)
    assert label == "Cam"


async def test_a_real_failure_wins_over_waiting(photos, monkeypatch) -> None:
    async def down(session, url):
        raise SourceError("cam answered 503")

    monkeypatch.setattr("custom_components.tvcanvas.sources.fetch_image", down)
    with pytest.raises(SourceError) as err:
        await next_image(group([local(A), CAM]), {}, None, None, identity_fit, None, photos)
    assert not isinstance(err.value, WaitingForPhone)


async def test_a_photo_deleted_behind_has_back_is_skipped(hass, photos, fetched) -> None:
    await photos.async_put(sha(A), A)
    (photos_dir(hass) / f"{sha(A)}.jpg").unlink()
    _, label = await next_image(group([local(A), CAM]), {}, None, None, identity_fit, None, photos)
    assert label == "Cam" and sha(A) not in photos


async def test_an_undecodable_photo_is_a_failure(photos) -> None:
    await photos.async_put(sha(A), A)

    async def broken_fit(data, orientation):
        from custom_components.tvcanvas.imaging import ImageError

        raise ImageError("bad")

    with pytest.raises(SourceError, match="undecodable"):
        await next_image(group([local(A)]), {}, None, None, broken_fit, None, photos)


# --- the runner's status ----------------------------------------------------------


class FakeDisplay:
    def __init__(self):
        self.shown = []

    async def show(self, jpeg):
        self.shown.append(jpeg)


@pytest.fixture
async def make_runner(hass, photos, fetched, monkeypatch):
    monkeypatch.setattr("custom_components.tvcanvas.runner.fit_to_tv", lambda d, o: d)
    store = StateStore(hass, "e")
    await store.async_load()

    def make(items):
        client = AsyncMock()
        display = FakeDisplay()
        runner = TVRunner(
            hass, client, None, store, group(items), display, lambda: None, lambda: None, photos
        )
        return runner, client, display

    return make


async def test_status_waits_for_missing_photos_while_showing_the_rest(photos, make_runner) -> None:
    await photos.async_put(sha(A), A)
    runner, client, display = make_runner([local(A), local(B), local(C)])
    await runner.tick()
    assert display.shown == [A]
    tv_id, ok, error, _, _showing = client.agent_status.call_args[0]
    assert (ok, error) == (False, "waiting: photos 2")
    assert runner.status.last_ok is not None  # HA's sensor records the update
    assert runner.status.last_error == "waiting: photos 2"
    assert runner.next_delay() == 60


async def test_all_photos_missing_keeps_the_image_and_does_not_back_off(
    photos, make_runner
) -> None:
    runner, client, display = make_runner([local(A), local(B)])
    for _ in range(3):
        await runner.tick()
    assert display.shown == []
    assert client.agent_status.call_args[0][1:3] == (False, "waiting: photos 2")
    assert runner.next_delay() == 60 and runner.status.last_ok is None


async def test_status_is_ok_once_every_photo_is_there(photos, make_runner) -> None:
    await photos.async_put(sha(A), A)
    runner, client, _ = make_runner([local(A), CAM])
    await runner.tick()
    await runner.tick()
    assert client.agent_status.call_args[0][1:3] == (True, None)


async def test_a_real_error_wins_over_waiting_in_the_status(make_runner, monkeypatch) -> None:
    async def down(session, url):
        raise SourceError("cam answered 503")

    monkeypatch.setattr("custom_components.tvcanvas.sources.fetch_image", down)
    runner, client, _ = make_runner([local(A), CAM])
    await runner.tick()
    assert client.agent_status.call_args[0][1:3] == (False, "image fetch failed")


async def test_out_of_credits_wins_over_waiting(make_runner) -> None:
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    runner, client, _ = make_runner([BOARD, local(A)])
    runner._credits_retry_at = dt_util.utcnow() + timedelta(minutes=30)
    await runner.tick()
    assert client.agent_status.call_args[0][1:3] == (False, "out of credits")
    assert not client.board.called


# --- the coordinator collects garbage after each refresh ---------------------------


async def test_coordinator_gc_keeps_what_specs_reference(hass, aioclient_mock, freezer) -> None:
    keep, broken = group_raw([local(A)], "tv-1"), group_raw([local(C), {"type": "x"}], "tv-2")
    aioclient_mock.get(f"{URL}/ha/agent/sync", json={"specs": [keep, broken]})
    e = entry(hass)
    with patch("custom_components.tvcanvas.runner.TVRunner.start"):
        await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    photos = e.runtime_data.photos
    for data in (A, B, C):
        await photos.async_put(sha(data), data)
    await e.runtime_data.async_refresh()
    assert photos.files == 3  # B is unreferenced but young
    freezer.tick(601)
    await e.runtime_data.async_refresh()
    assert photos.hashes == sorted([sha(A), sha(C)])
    await hass.config_entries.async_unload(e.entry_id)
