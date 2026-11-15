"""Replace previous on a Frame, end to end against a fake My Photos (2026-11-14 incident).

The owner's Frame filled My Photos with hundreds of copies of the same seven images: a
group pin ran on Home Assistant every 180 s from 2026-11-12 18:41 to 2026-11-13 21:43
and none of its uploads was ever deleted. HA's delete path was never reached: the TVCanvas
app wrote the pin with `frame.replace_previous: false` (it sent its unrelated "Clear
existing art first" bulk-upload option, false for every group pin, as Replace previous),
and HA kept every image without a word and without recording which ones were its own.

These tests run the real FrameDisplay and samsungtvws call sequence against a fake Frame
that keeps a My Photos list, so what ends up on the TV is what is asserted.
"""

from __future__ import annotations

import logging
from datetime import datetime

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE

from custom_components.tvcanvas.display_frame import FrameDisplay
from custom_components.tvcanvas.displays import STANDBY, DisplayError
from custom_components.tvcanvas.locate import Check, Found
from custom_components.tvcanvas.storage import StateStore

TV_ID = "samsung:00:00:5e:00:53:01"
OLD, NEW = "192.168.0.20", "192.168.0.21"
ART = "custom_components.tvcanvas.display_frame.SamsungTVArt"
USER_ART = {"USER_0001": "2025:12:24 09:00:00", "USER_0002": "2026:11:12 18:41:39"}


class FakeFrame:
    """samsungtvws' SamsungTVArt for one Frame that answers at one address."""

    def __init__(self, at: str = OLD) -> None:
        self.at = at
        self.photos: dict[str, str] = dict(USER_ART)  # My Photos: content id -> image_date
        self.n = 2700
        self.uploaded: list[str] = []
        self.calls: list[tuple[str, str, object]] = []  # (address, request, argument)
        self.connected: list[str] = []
        self.refuse: set[str] = set()  # ids the TV will not delete
        self.drop_on_delete = False  # the connection breaks at the first delete
        self.artmode, self.slideshow = "on", "off"
        self.token = None
        self._host = at

    # samsungtvws.SamsungTVArt(host, token=..., port=..., timeout=..., name=...)
    def __call__(self, host: str, **_kwargs) -> FakeFrame:
        self.connected.append(host)
        if host != self.at:
            raise OSError(113, "Host is unreachable")
        self._host = host
        return self

    def _open_d2d_socket(self, conn_info):  # samsungtvws' upload socket; the guard wraps it
        raise AssertionError("not used by this fake")

    def available(self, category=None):
        self.calls.append((self._host, "available", category))
        return [{"content_id": c, "image_date": d} for c, d in self.photos.items()]

    def upload(self, data, **_kwargs) -> str:
        self.n += 1
        cid = f"MY_F{self.n}"
        self.calls.append((self._host, "upload", cid))
        # samsungtvws stamps image_date with the process clock, as _now() reads it
        self.photos[cid] = datetime.now().strftime("%Y:%m:%d %H:%M:%S")
        self.uploaded.append(cid)
        return cid

    def select_image(self, cid, show=True):
        self.calls.append((self._host, "select_image", cid))

    def delete_list(self, ids) -> bool:
        ids = list(ids)
        self.calls.append((self._host, "delete_list", ids))
        if self.drop_on_delete:
            raise ConnectionResetError("connection reset")
        if self.refuse & set(ids):
            return False
        for cid in ids:
            self.photos.pop(cid, None)
        return True

    def delete(self, cid) -> bool:
        self.calls.append((self._host, "delete", cid))
        if self.drop_on_delete:
            raise ConnectionResetError("connection reset")
        if cid in self.refuse:
            return False
        self.photos.pop(cid, None)
        return True

    def get_artmode(self):
        return self.artmode

    def get_slideshow_status(self):
        return {"value": self.slideshow}

    def close(self):
        return None

    # What the tests assert on.

    def ha_images(self) -> list[str]:
        return [c for c in self.photos if c not in USER_ART]

    def deletes(self) -> list[str]:
        out: list[str] = []
        for _, request, arg in self.calls:
            if request == "delete_list":
                out.extend(arg)  # type: ignore[arg-type]
            elif request == "delete":
                out.append(arg)  # type: ignore[arg-type]
        return out


class Finder:
    """FrameFinder: the TV checks out at `tv.at` only; PowerState is scripted."""

    def __init__(self, tv: FakeFrame) -> None:
        self.tv, self.power = tv, "on"

    async def find(self, mac, skip):
        return Found(self.tv.at, Check.OK) if self.tv.at != skip else None

    async def check(self, address, mac):
        return Found(address, Check.OK if address == self.tv.at else Check.NOT_IT)

    async def power_state(self, address):
        return self.power if address == self.tv.at else None


@pytest.fixture
async def store(hass):
    s = StateStore(hass, "e")
    await s.async_load()
    return s


@pytest.fixture
def tv(monkeypatch):
    frame = FakeFrame()
    monkeypatch.setattr(ART, frame)
    return frame


def display(hass, store, tv, replace=True, host=OLD):
    return FrameDisplay(hass, store, TV_ID, host, replace, lambda _: None, finder=Finder(tv))


async def tick(disp) -> str | None:
    """One runner tick: ready(), then show() when the Frame shows TVCanvas."""
    waiting = await disp.ready()
    if waiting is None:
        await disp.show(b"JPEG")
    return waiting


async def test_each_upload_deletes_the_previous_one_after_the_new_one_shows(
    hass, store, tv
) -> None:
    disp = display(hass, store, tv)
    for n in range(1, 8):
        await tick(disp)
        assert tv.ha_images() == [tv.uploaded[-1]], f"tick {n}: one HA image on the TV"
    for previous, new in zip(tv.uploaded, tv.uploaded[1:], strict=False):
        requests = [(r, a) for _, r, a in tv.calls]
        # the new image is shown before the one it replaces is deleted
        assert requests.index(("select_image", new)) < requests.index(("delete_list", [previous]))
    assert set(USER_ART) <= set(tv.photos)
    assert list(store.frame_uploaded(TV_ID)) == [tv.uploaded[-1]]


async def test_deletes_reach_a_frame_followed_to_a_new_address(hass, store, tv) -> None:
    disp = display(hass, store, tv)
    await tick(disp)
    first = tv.uploaded[-1]
    tv.at = NEW  # the router gave the TV a new lease
    await tick(disp)
    assert disp.address == NEW
    assert (NEW, "delete_list", [first]) in tv.calls
    assert tv.ha_images() == [tv.uploaded[-1]]
    await tick(disp)  # and again from the followed address, re-checked every tick
    assert tv.ha_images() == [tv.uploaded[-1]] and len(tv.uploaded) == 3


async def test_the_previous_image_is_deleted_after_a_restart(hass, store, tv, hass_storage) -> None:
    await tick(display(hass, store, tv))
    shown = tv.uploaded[-1]
    # HA stopping: Store flushes every pending delayed save
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()
    assert "tvcanvas.e" in hass_storage

    restarted = StateStore(hass, "e")
    await restarted.async_load()
    assert restarted.frame(TV_ID)["current"] == shown
    assert shown in restarted.frame_uploaded(TV_ID)
    await tick(display(hass, restarted, tv))
    assert tv.ha_images() == [tv.uploaded[-1]] and shown not in tv.photos


async def test_nothing_ha_did_not_upload_is_ever_deleted(hass, store, tv, caplog) -> None:
    disp = display(hass, store, tv)
    await tick(disp)
    # an upload drops the connection after the TV stored it, and the user uploads in the
    # same window: HA can't tell which new image is its own and must adopt neither
    real_upload = tv.upload

    def leaky(data, **kwargs):
        cid = real_upload(data, **kwargs)
        tv.photos["USER_0003"] = tv.photos[cid]
        raise ConnectionResetError("connection reset")

    tv.upload = leaky
    with pytest.raises(DisplayError):
        await tick(disp)
    tv.upload = real_upload
    await tick(disp)
    tv.slideshow = "3"  # the user starts the TV's own slideshow: HA removes its images
    assert await tick(disp) == "waiting: Art"
    tv.slideshow = "off"
    await tick(disp)
    disp.release()  # the phone takes the TV back
    await hass.async_block_till_done(wait_background_tasks=True)

    assert {"USER_0001", "USER_0002", "USER_0003"} <= set(tv.photos)
    assert set(tv.deletes()) <= set(tv.uploaded)
    assert "could not tell which" in caplog.text


async def test_a_refused_delete_is_logged_and_retried_on_the_next_tick(
    hass, store, tv, caplog
) -> None:
    disp = display(hass, store, tv)
    await tick(disp)
    first = tv.uploaded[-1]
    tv.refuse = {first}
    with caplog.at_level(logging.WARNING):
        await tick(disp)
    assert first in tv.photos and first in store.frame(TV_ID)["stale"]
    assert f"could not delete {first}" in caplog.text
    assert "next update" in caplog.text

    tv.refuse = set()
    await tick(disp)
    assert first not in tv.photos and tv.ha_images() == [tv.uploaded[-1]]
    assert first not in store.frame_uploaded(TV_ID)


async def test_a_delete_cut_off_by_the_connection_is_logged_and_retried(
    hass, store, tv, caplog
) -> None:
    disp = display(hass, store, tv)
    await tick(disp)
    first = tv.uploaded[-1]
    tv.drop_on_delete = True
    with caplog.at_level(logging.WARNING):
        await tick(disp)
    assert first in tv.photos and f"could not delete {first}" in caplog.text
    tv.drop_on_delete = False
    await tick(disp)
    assert tv.ha_images() == [tv.uploaded[-1]]


async def test_standby_pauses_without_touching_the_bookkeeping(hass, store, tv) -> None:
    finder_disp = display(hass, store, tv)
    await tick(finder_disp)
    shown = tv.uploaded[-1]
    before = dict(store.frame(TV_ID))
    connections = len(tv.connected)

    finder_disp._finder.power = "standby"
    for _ in range(3):
        assert await tick(finder_disp) == STANDBY
    assert len(tv.connected) == connections  # no WebSocket while the TV sleeps
    assert store.frame(TV_ID) == before and shown in store.frame_uploaded(TV_ID)

    finder_disp._finder.power = "on"
    assert await tick(finder_disp) is None
    assert shown not in tv.photos and tv.ha_images() == [tv.uploaded[-1]]


# --- Replace previous off -----------------------------------------------------


async def test_keep_previous_art_records_every_upload_and_says_so_once(
    hass, store, tv, caplog
) -> None:
    """The incident: a pin with Replace previous off fills My Photos. HA keeps the images
    (that is what the pin asks for), but it now says so and records which are its own."""
    disp = display(hass, store, tv, replace=False)
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            await tick(disp)
    assert tv.deletes() == []
    assert len(tv.ha_images()) == 5
    assert list(store.frame_uploaded(TV_ID)) == tv.uploaded
    assert caplog.text.count("Replace previous is off") == 1


async def test_the_record_of_kept_images_outlives_the_pin(hass, store, tv) -> None:
    disp = display(hass, store, tv, replace=False)
    for _ in range(3):
        await tick(disp)
    disp.release()
    store.forget_tv(TV_ID)  # what the coordinator does when the spec is removed
    await hass.async_block_till_done(wait_background_tasks=True)
    assert list(store.frame_uploaded(TV_ID)) == tv.uploaded
    assert tv.deletes() == []


async def test_release_clears_the_record_of_what_it_deleted(hass, store, tv) -> None:
    disp = display(hass, store, tv)
    for _ in range(2):
        await tick(disp)
    disp.release()
    store.forget_tv(TV_ID)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert tv.ha_images() == []
    assert store.frame_uploaded(TV_ID) == {}


async def test_the_record_is_capped_dropping_the_oldest(hass, store, tv, monkeypatch) -> None:
    monkeypatch.setattr("custom_components.tvcanvas.display_frame.FRAME_UPLOADED_MAX", 3)
    disp = display(hass, store, tv, replace=False)
    for _ in range(5):
        await tick(disp)
    assert list(store.frame_uploaded(TV_ID)) == tv.uploaded[-3:]
