from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from samsungtvws.exceptions import ConnectionFailure, ResponseError

from custom_components.tvcanvas.display_frame import (
    FrameDisplay,
    UploadedNotShown,
    UploadLost,
    _match_lost,
    _mode_sync,
    _show_sync,
)
from custom_components.tvcanvas.displays import DisplayError
from custom_components.tvcanvas.storage import StateStore


def fake_art(upload_id="MY_F0002", token="T1", delete_ok=True, upload_exc=None):
    art = MagicMock()
    art.token = token
    art.upload.side_effect = upload_exc or (lambda *a, **k: upload_id)
    art.delete_list.return_value = delete_ok
    return art


def test_show_sync_order_and_args() -> None:
    art = fake_art()
    with patch("custom_components.tvcanvas.display_frame.SamsungTVArt", return_value=art) as ctor:
        cid, token, deleted, failed = _show_sync("10.0.0.2", None, b"JPEG", ["MY_F0001"])
    ctor.assert_called_once_with("10.0.0.2", token=None, port=8002, timeout=60, name="TVCanvas-HA")
    names = [c[0] for c in art.method_calls]
    assert names[:3] == ["upload", "select_image", "delete_list"]
    art.upload.assert_called_once_with(
        b"JPEG", matte="none", portrait_matte="none", file_type="jpg"
    )
    art.select_image.assert_called_once_with("MY_F0002", show=True)
    art.delete_list.assert_called_once_with(["MY_F0001"])
    assert (cid, token, deleted, failed) == ("MY_F0002", "T1", ["MY_F0001"], [])
    art.close.assert_called_once()


def test_show_sync_never_deletes_the_new_image_and_tolerates_delete_failure() -> None:
    art = fake_art(upload_id="MY_F0001", delete_ok=False)
    art.delete.return_value = False
    with patch("custom_components.tvcanvas.display_frame.SamsungTVArt", return_value=art):
        cid, _, deleted, failed = _show_sync("10.0.0.2", "T0", b"J", ["MY_F0001", "MY_F0000"])
    art.delete_list.assert_called_once_with(["MY_F0000"])
    art.delete.assert_called_once_with("MY_F0000")
    assert cid == "MY_F0001" and deleted == [] and failed == ["MY_F0000"]


@pytest.mark.parametrize("batch", [False, ResponseError("unknown id")])
def test_failed_batch_delete_falls_back_to_per_id(batch) -> None:
    art = fake_art(upload_id="MY_F0009")
    if isinstance(batch, Exception):
        art.delete_list.side_effect = batch
    else:
        art.delete_list.return_value = batch

    def delete(cid):
        if cid == "GONE":
            raise ResponseError("no such content")
        return cid != "BAD"

    art.delete.side_effect = delete
    with patch("custom_components.tvcanvas.display_frame.SamsungTVArt", return_value=art):
        _, _, deleted, failed = _show_sync("10.0.0.2", "T0", b"J", ["A", "BAD", "GONE", "C"])
    assert deleted == ["A", "C"] and failed == ["BAD", "GONE"]


def test_connection_loss_during_cleanup_is_not_counted_as_failure() -> None:
    art = fake_art(upload_id="MY_F0009", delete_ok=False)
    art.delete.side_effect = [True, ConnectionFailure("socket closed")]
    with patch("custom_components.tvcanvas.display_frame.SamsungTVArt", return_value=art):
        _, _, deleted, failed = _show_sync("10.0.0.2", "T0", b"J", ["A", "B", "C"])
    assert deleted == ["A"] and failed == []
    assert art.delete.call_count == 2


def test_select_failure_reports_the_uploaded_id() -> None:
    art = fake_art(upload_id="MY_F0002", token="T1")
    art.select_image.side_effect = ResponseError("select failed")
    with (
        patch("custom_components.tvcanvas.display_frame.SamsungTVArt", return_value=art),
        pytest.raises(UploadedNotShown) as info,
    ):
        _show_sync("10.0.0.2", None, b"J", ["MY_F0001"])
    assert info.value.content_id == "MY_F0002" and info.value.token == "T1"
    art.delete_list.assert_not_called()
    art.close.assert_called_once()


@pytest.fixture
async def store(hass):
    s = StateStore(hass, "e")
    await s.async_load()
    return s


async def test_show_tracks_owned_ids_and_token(hass, store) -> None:
    pairing = []
    disp = FrameDisplay(hass, store, "tv-1", "10.0.0.2", True, pairing.append)
    store.frame("tv-1").update(current="MY_F0001", stale=["MY_F0000"])
    with patch(
        "custom_components.tvcanvas.display_frame._show_sync",
        return_value=("MY_F0002", "T1", ["MY_F0001"], []),
    ) as sync:
        await disp.show(b"J")
    assert sync.call_args[0][3] == ["MY_F0000", "MY_F0001"]
    assert store.frame("tv-1") == {"current": "MY_F0002", "stale": ["MY_F0000"], "attempts": {}}
    assert store.frame_token("10.0.0.2") == "T1"
    assert pairing == [True, False]  # no stored token → pairing notice shown, then cleared


async def test_replace_previous_false_keeps_old_art(hass, store) -> None:
    disp = FrameDisplay(hass, store, "tv-1", "10.0.0.2", False, lambda _: None)
    store.frame("tv-1")["current"] = "MY_F0001"
    store.set_frame_token("10.0.0.2", "T0")
    with patch(
        "custom_components.tvcanvas.display_frame._show_sync",
        return_value=("MY_F0002", "T0", [], []),
    ) as sync:
        await disp.show(b"J")
    assert sync.call_args[0][3] == []
    assert store.frame("tv-1") == {"current": "MY_F0002", "stale": [], "attempts": {}}


async def test_new_ip_pairs_again(hass, store) -> None:
    """Review focus 1: token is per host, so a DHCP move re-pairs once."""
    store.set_frame_token("10.0.0.2", "OLD")
    pairing = []
    disp = FrameDisplay(hass, store, "tv-1", "10.0.0.9", True, pairing.append)
    with patch(
        "custom_components.tvcanvas.display_frame._show_sync",
        return_value=("MY_F0003", "NEW", [], []),
    ) as sync:
        await disp.show(b"J")
    assert sync.call_args[0][1] is None
    assert pairing == [True, False]
    assert store.frame_token("10.0.0.9") == "NEW"


async def test_unreachable_tv_becomes_display_error(hass, store) -> None:
    disp = FrameDisplay(hass, store, "tv-1", "10.0.0.2", True, lambda _: None)
    with (
        patch(
            "custom_components.tvcanvas.display_frame._show_sync", side_effect=OSError("timed out")
        ),
        pytest.raises(DisplayError, match="TV unreachable"),
    ):
        await disp.show(b"J")


async def test_select_failure_records_upload_as_stale(hass, store) -> None:
    disp = FrameDisplay(hass, store, "tv-1", "10.0.0.2", True, lambda _: None)
    store.frame("tv-1").update(current="MY_F0001", stale=["MY_F0000"])
    with (
        patch(
            "custom_components.tvcanvas.display_frame._show_sync",
            side_effect=UploadedNotShown("MY_F0002", "T1"),
        ),
        pytest.raises(DisplayError),
    ):
        await disp.show(b"J")
    state = store.frame("tv-1")
    assert state["current"] == "MY_F0001"
    assert state["stale"] == ["MY_F0000", "MY_F0002"]
    assert store.frame_token("10.0.0.2") == "T1"


class StubbornArt:
    """A Frame whose batch delete always fails and that never deletes BAD."""

    def __init__(self):
        self.n, self.token, self.deleted, self.tried = 0, "T", [], []

    def _open_d2d_socket(self, conn_info):  # samsungtvws' upload socket; the guard wraps it
        raise AssertionError("not used by this fake")

    def available(self, category=None):
        return []

    def upload(self, *a, **k):
        self.n += 1
        return f"MY_F{self.n:04d}"

    def select_image(self, *a, **k):
        return None

    def delete_list(self, ids):
        return False

    def delete(self, cid):
        self.tried.append(cid)
        if cid == "BAD":
            return False
        self.deleted.append(cid)
        return True

    def close(self):
        return None


async def test_undeletable_id_is_dropped_after_three_attempts(hass, store) -> None:
    art = StubbornArt()
    store.set_frame_token("10.0.0.2", "T")
    store.frame("tv-1").update(current=None, stale=["BAD"])
    disp = FrameDisplay(hass, store, "tv-1", "10.0.0.2", True, lambda _: None)
    with patch("custom_components.tvcanvas.display_frame.SamsungTVArt", return_value=art):
        for _ in range(4):
            await disp.show(b"J")
    # every previous upload was deleted despite BAD failing alongside it
    assert art.deleted == ["MY_F0001", "MY_F0002", "MY_F0003"]
    assert art.tried.count("BAD") == 3
    assert store.frame("tv-1") == {"current": "MY_F0004", "stale": [], "attempts": {}}


async def test_stale_list_is_capped_dropping_oldest(hass, store) -> None:
    store.set_frame_token("10.0.0.2", "T")
    old = [f"MY_F{i:04d}" for i in range(30)]
    store.frame("tv-1").update(current=None, stale=old)
    disp = FrameDisplay(hass, store, "tv-1", "10.0.0.2", True, lambda _: None)
    with patch(
        "custom_components.tvcanvas.display_frame._show_sync",
        return_value=("MY_F0100", "T", [], []),
    ):
        await disp.show(b"J")
    assert store.frame("tv-1")["stale"] == old[-20:]


# --- #2: an upload that fails after the TV stored the image ------------------

T0 = datetime(2026, 10, 13, 19, 6, 40)


def photo(cid, when):
    return {"content_id": cid, "category_id": "MY-C0002", "image_date": when}


def lost(before=("USER_1",), sent=T0, failed=T0 + timedelta(seconds=47)):
    return {"before": list(before), "sent": sent.isoformat(), "failed": failed.isoformat()}


def test_match_lost_adopts_the_single_new_image_in_the_window() -> None:
    items = [photo("USER_1", "2026:01:01 10:00:00"), photo("MY_F1543", "2026:10:13 19:06:41")]
    assert _match_lost(items, lost(), tracked=set()) == (["MY_F1543"], [])


def test_match_lost_reads_dates_by_their_digits() -> None:
    items = [photo("MY_F1543", "2026-10-13 19:06:41")]
    assert _match_lost(items, lost(before=()), tracked=set()) == (["MY_F1543"], [])


def test_match_lost_two_new_images_adopts_none() -> None:
    items = [photo("MY_F1543", "2026:10:13 19:06:41"), photo("PHONE", "2026:10:13 19:07:00")]
    assert _match_lost(items, lost(), tracked=set()) == ([], ["MY_F1543", "PHONE"])


def test_match_lost_never_adopts_a_new_photo_outside_the_window() -> None:
    items = [photo("PHONE", "2026:10:13 19:30:00"), photo("NODATE", None)]
    assert _match_lost(items, lost(), tracked=set()) == ([], [])


def test_match_lost_skips_ids_in_the_snapshot_or_tracked() -> None:
    items = [photo("USER_1", "2026:10:13 19:06:41"), photo("MY_F1542", "2026:10:13 19:06:42")]
    assert _match_lost(items, lost(), tracked={"MY_F1542"}) == ([], [])


def test_snapshot_then_failed_upload_raises_upload_lost() -> None:
    art = fake_art(upload_exc=ConnectionResetError("reset"))
    art.available.return_value = [photo("USER_1", "x"), photo("MY_F0001", "y")]
    with (
        patch("custom_components.tvcanvas.display_frame.SamsungTVArt", return_value=art),
        pytest.raises(UploadLost) as info,
    ):
        _show_sync("10.0.0.2", "T0", b"J", [], snapshot=True)
    art.available.assert_called_once_with("MY-C0002")
    assert info.value.before == ["USER_1", "MY_F0001"]
    assert info.value.sent <= info.value.failed and info.value.token == "T1"
    art.close.assert_called_once()


def test_failed_snapshot_is_an_ordinary_failure() -> None:
    art = fake_art()
    art.available.side_effect = ConnectionFailure("gone")
    with (
        patch("custom_components.tvcanvas.display_frame.SamsungTVArt", return_value=art),
        pytest.raises(ConnectionFailure),
    ):
        _show_sync("10.0.0.2", "T0", b"J", [], snapshot=True)
    art.upload.assert_not_called()


class LeakyFrame:
    """Stores the image, then drops the connection before image_added."""

    def __init__(self, photos):
        self.photos, self.token, self.deleted, self.fail_next = photos, "T", [], True
        self.n = 1542

    def _open_d2d_socket(self, conn_info):  # samsungtvws' upload socket; the guard wraps it
        raise AssertionError("not used by this fake")

    def available(self, category=None):
        return list(self.photos)

    def upload(self, *a, **k):
        self.n += 1
        cid = f"MY_F{self.n}"
        self.photos.append(photo(cid, datetime.now().strftime("%Y:%m:%d %H:%M:%S")))
        if self.fail_next:
            self.fail_next = False
            raise ConnectionResetError("connection reset")
        return cid

    def select_image(self, *a, **k):
        return None

    def delete_list(self, ids):
        self.deleted.extend(ids)
        self.photos = [p for p in self.photos if p["content_id"] not in ids]
        return True

    def close(self):
        return None


async def test_leaked_upload_is_adopted_and_deleted_on_the_next_tick(hass, store) -> None:
    tv = LeakyFrame([photo("USER_1", "2025:12:24 09:00:00")])
    store.set_frame_token("10.0.0.2", "T")
    store.frame("tv-1").update(current="MY_F1542", stale=[])
    tv.photos.append(photo("MY_F1542", "2026:10:13 19:04:00"))
    disp = FrameDisplay(hass, store, "tv-1", "10.0.0.2", True, lambda _: None)
    with patch("custom_components.tvcanvas.display_frame.SamsungTVArt", return_value=tv):
        with pytest.raises(DisplayError, match="TV unreachable"):
            await disp.show(b"J")
        assert store.frame("tv-1")["lost"]["before"] == ["USER_1", "MY_F1542"]
        await disp.show(b"J")
    assert sorted(tv.deleted) == ["MY_F1542", "MY_F1543"]
    assert [p["content_id"] for p in tv.photos] == ["USER_1", "MY_F1544"]
    assert store.frame("tv-1") == {"current": "MY_F1544", "stale": [], "attempts": {}}


async def test_ambiguous_leak_adopts_none_and_logs_once(hass, store, caplog) -> None:
    tv = LeakyFrame([])
    store.set_frame_token("10.0.0.2", "T")
    disp = FrameDisplay(hass, store, "tv-1", "10.0.0.2", True, lambda _: None)
    with patch("custom_components.tvcanvas.display_frame.SamsungTVArt", return_value=tv):
        with pytest.raises(DisplayError):
            await disp.show(b"J")
        now = datetime.now().strftime("%Y:%m:%d %H:%M:%S")
        tv.photos.append(photo("PHONE_UPLOAD", now))  # the user uploaded in the same window
        await disp.show(b"J")
        await disp.show(b"J")
    assert "PHONE_UPLOAD" not in tv.deleted and "MY_F1543" not in tv.deleted
    assert caplog.text.count("could not tell which") == 1
    assert "lost" not in store.frame("tv-1")


async def test_recovery_while_tv_is_still_off_keeps_the_record(hass, store) -> None:
    store.set_frame_token("10.0.0.2", "T")
    store.frame("tv-1")["lost"] = lost()
    disp = FrameDisplay(hass, store, "tv-1", "10.0.0.2", True, lambda _: None)
    art = fake_art()
    art.available.side_effect = OSError("timed out")
    with (
        patch("custom_components.tvcanvas.display_frame.SamsungTVArt", return_value=art),
        pytest.raises(DisplayError, match="TV unreachable"),
    ):
        await disp.show(b"J")
    assert store.frame("tv-1")["lost"] == lost()
    art.upload.assert_not_called()


async def test_keep_previous_art_takes_no_snapshot_and_records_nothing(hass, store) -> None:
    tv = LeakyFrame([])
    tv.available = MagicMock(side_effect=AssertionError("no listing when keeping art"))
    store.set_frame_token("10.0.0.2", "T")
    disp = FrameDisplay(hass, store, "tv-1", "10.0.0.2", False, lambda _: None)
    with patch("custom_components.tvcanvas.display_frame.SamsungTVArt", return_value=tv):
        with pytest.raises(DisplayError):
            await disp.show(b"J")
    assert "lost" not in store.frame("tv-1")


async def test_switching_to_keep_previous_art_drops_a_pending_record(hass, store) -> None:
    store.set_frame_token("10.0.0.2", "T")
    store.frame("tv-1")["lost"] = lost()
    disp = FrameDisplay(hass, store, "tv-1", "10.0.0.2", False, lambda _: None)
    with patch(
        "custom_components.tvcanvas.display_frame._show_sync",
        return_value=("MY_F0002", "T", [], []),
    ):
        await disp.show(b"J")
    assert store.frame("tv-1") == {"current": "MY_F0002", "stale": [], "attempts": {}}


# --- Frame mode (Watch TV | Art | TVCanvas) and take-back --------------------


def mode_art(artmode="on", slideshow="off", artmode_exc=None):
    art = fake_art()
    if artmode_exc:
        art.get_artmode.side_effect = artmode_exc
    else:
        art.get_artmode.return_value = artmode
    art.get_slideshow_status.return_value = {"value": slideshow, "category_id": "MY-C0002"}
    return art


@pytest.mark.parametrize(
    ("artmode", "slideshow", "mode"),
    [
        ("off", "off", "watch_tv"),
        ("on", "off", "tvcanvas"),
        ("on", "0", "tvcanvas"),
        ("on", "3", "art"),
        ("weird", "off", "tvcanvas"),
    ],
)
def test_mode_follows_the_phone_rule(artmode, slideshow, mode) -> None:
    art = mode_art(artmode, slideshow)
    with patch("custom_components.tvcanvas.display_frame.SamsungTVArt", return_value=art):
        got, token, deleted = _mode_sync("10.0.0.2", "T0", ["MY_F0001"])
    assert got == mode and token == "T1"
    assert deleted == (["MY_F0001"] if mode == "art" else [])
    art.close.assert_called_once()


def test_firmware_without_artmode_status_is_not_blocked() -> None:
    art = mode_art(artmode_exc=ResponseError("unknown request"))
    with patch("custom_components.tvcanvas.display_frame.SamsungTVArt", return_value=art):
        assert _mode_sync("10.0.0.2", "T0", [])[0] == "tvcanvas"


def test_connection_failure_while_reading_mode_propagates() -> None:
    art = mode_art(artmode_exc=ConnectionFailure("gone"))
    with (
        patch("custom_components.tvcanvas.display_frame.SamsungTVArt", return_value=art),
        pytest.raises(ConnectionFailure),
    ):
        _mode_sync("10.0.0.2", "T0", [])


async def test_ready_in_watch_tv_waits_and_keeps_art(hass, store) -> None:
    store.set_frame_token("10.0.0.2", "T0")
    store.frame("tv-1").update(current="MY_F0001", stale=[])
    disp = FrameDisplay(hass, store, "tv-1", "10.0.0.2", True, lambda _: None)
    with patch(
        "custom_components.tvcanvas.display_frame._mode_sync",
        return_value=("watch_tv", "T0", []),
    ):
        assert await disp.ready() == "waiting: Watch TV"
    assert store.frame("tv-1")["current"] == "MY_F0001"


async def test_ready_in_art_deletes_has_images_from_the_slideshow(hass, store) -> None:
    store.set_frame_token("10.0.0.2", "T0")
    store.frame("tv-1").update(current="MY_F0002", stale=["MY_F0001"])
    disp = FrameDisplay(hass, store, "tv-1", "10.0.0.2", True, lambda _: None)
    with patch(
        "custom_components.tvcanvas.display_frame._mode_sync",
        return_value=("art", "T0", ["MY_F0001", "MY_F0002"]),
    ) as sync:
        assert await disp.ready() == "waiting: Art"
    assert sync.call_args[0][2] == ["MY_F0001", "MY_F0002"]
    assert store.frame("tv-1") == {"current": None, "stale": [], "attempts": {}}


async def test_ready_in_art_keeps_images_when_keeping_previous_art(hass, store) -> None:
    store.set_frame_token("10.0.0.2", "T0")
    store.frame("tv-1").update(current="MY_F0002", stale=[])
    disp = FrameDisplay(hass, store, "tv-1", "10.0.0.2", False, lambda _: None)
    with patch(
        "custom_components.tvcanvas.display_frame._mode_sync", return_value=("art", "T0", [])
    ) as sync:
        assert await disp.ready() == "waiting: Art"
    assert sync.call_args[0][2] == []


async def test_ready_in_tvcanvas_runs_the_tick(hass, store) -> None:
    pairing = []
    disp = FrameDisplay(hass, store, "tv-1", "10.0.0.2", True, pairing.append)
    with patch(
        "custom_components.tvcanvas.display_frame._mode_sync",
        return_value=("tvcanvas", "NEW", []),
    ):
        assert await disp.ready() is None
    assert store.frame_token("10.0.0.2") == "NEW" and pairing == [True, False]


async def test_ready_unreachable_is_a_display_error(hass, store) -> None:
    disp = FrameDisplay(hass, store, "tv-1", "10.0.0.2", True, lambda _: None)
    with (
        patch("custom_components.tvcanvas.display_frame._mode_sync", side_effect=OSError("x")),
        pytest.raises(DisplayError, match="TV unreachable"),
    ):
        await disp.ready()


async def test_release_deletes_has_images(hass, store) -> None:
    store.set_frame_token("10.0.0.2", "T0")
    store.frame("tv-1").update(current="MY_F0002", stale=["MY_F0001"])
    disp = FrameDisplay(hass, store, "tv-1", "10.0.0.2", True, lambda _: None)
    art = fake_art()
    with patch("custom_components.tvcanvas.display_frame.SamsungTVArt", return_value=art):
        disp.release()
        await hass.async_block_till_done(wait_background_tasks=True)
    art.delete_list.assert_called_once_with(["MY_F0001", "MY_F0002"])


async def test_release_with_nothing_tracked_does_not_connect(hass, store) -> None:
    disp = FrameDisplay(hass, store, "tv-1", "10.0.0.2", True, lambda _: None)
    with patch("custom_components.tvcanvas.display_frame.SamsungTVArt") as ctor:
        disp.release()
        await hass.async_block_till_done(wait_background_tasks=True)
    ctor.assert_not_called()


async def test_release_keeps_art_when_keeping_previous_art(hass, store) -> None:
    store.frame("tv-1").update(current="MY_F0002", stale=[])
    disp = FrameDisplay(hass, store, "tv-1", "10.0.0.2", False, lambda _: None)
    with patch("custom_components.tvcanvas.display_frame.SamsungTVArt") as ctor:
        disp.release()
        await hass.async_block_till_done(wait_background_tasks=True)
    ctor.assert_not_called()


async def test_release_on_an_unreachable_tv_warns_and_never_raises(hass, store, caplog) -> None:
    store.frame("tv-1").update(current="MY_F0002", stale=[])
    disp = FrameDisplay(hass, store, "tv-1", "10.0.0.2", True, lambda _: None)
    with patch(
        "custom_components.tvcanvas.display_frame.SamsungTVArt", side_effect=OSError("timed out")
    ):
        disp.release()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert "MY_F0002" in caplog.text


# --- 2026-11-09 review ------------------------------------------------------


def test_impossible_image_date_is_never_a_candidate() -> None:
    lost = {"before": [], "sent": "2026-11-08T10:00:00", "failed": "2026-11-08T10:00:30"}
    items = [{"content_id": "MY_F0009", "image_date": "2026:13:08 10:00:10"}]
    assert _match_lost(items, lost, set()) == ([], [])


def test_slideshow_status_unanswered_counts_as_tvcanvas() -> None:
    art = mode_art("on")
    art.get_slideshow_status.side_effect = ResponseError("unknown request")
    with patch("custom_components.tvcanvas.display_frame.SamsungTVArt", return_value=art):
        assert _mode_sync("10.0.0.2", "T0", ["MY_F0001"]) == ("tvcanvas", "T1", [])
    art.delete_list.assert_not_called()


async def test_select_failure_when_keeping_previous_art_records_nothing(hass, store) -> None:
    disp = FrameDisplay(hass, store, "tv-1", "10.0.0.2", False, lambda _: None)
    store.frame("tv-1")["current"] = "MY_F0001"
    with (
        patch(
            "custom_components.tvcanvas.display_frame._show_sync",
            side_effect=UploadedNotShown("MY_F0002", "T1"),
        ),
        pytest.raises(DisplayError, match="did not show"),
    ):
        await disp.show(b"J")
    assert store.frame("tv-1") == {"current": "MY_F0001", "stale": [], "attempts": {}}
    assert store.frame_token("10.0.0.2") == "T1"


async def test_ready_in_art_keeps_current_when_only_stale_was_deleted(hass, store) -> None:
    store.set_frame_token("10.0.0.2", "T0")
    store.frame("tv-1").update(current="MY_F0002", stale=["MY_F0001"])
    disp = FrameDisplay(hass, store, "tv-1", "10.0.0.2", True, lambda _: None)
    with patch(
        "custom_components.tvcanvas.display_frame._mode_sync",
        return_value=("art", "T0", ["MY_F0001"]),
    ):
        assert await disp.ready() == "waiting: Art"
    assert store.frame("tv-1")["current"] == "MY_F0002"
    assert store.frame("tv-1")["stale"] == []
