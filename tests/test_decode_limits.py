"""Image decoding is bounded (#16): one decode at a time for the whole integration, phone
uploads capped at what the phone sends, and a decode that is cancelled keeps its slot
until its thread ends.

Decoding one image can take 150 to 380 MB, and a small Home Assistant box has 1 to 4 GB.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import threading
import time
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from custom_components.tvcanvas.const import MAX_CONCURRENT_DECODES, MAX_UPLOAD_PIXELS
from custom_components.tvcanvas.imaging import (
    DecodeBusy,
    DecodeGate,
    ImageError,
    check_upload,
    decode_gate,
    fit_to_tv,
)
from custom_components.tvcanvas.models import parse_spec
from custom_components.tvcanvas.runner import TVRunner
from custom_components.tvcanvas.storage import StateStore

from .test_lan_api import KEY, lan  # noqa: F401 - the fixture

# --- the upload pixel cap ------------------------------------------------------------------


def one_bit_png(w: int, h: int) -> bytes:
    """Tiny on disk at any size: what a 112 KB upload costing 151 MB looks like."""
    buf = io.BytesIO()
    Image.new("1", (w, h), 0).save(buf, "PNG")
    return buf.getvalue()


def test_the_upload_cap_has_room_above_what_the_phone_sends() -> None:
    assert 3840 * 2160 < 4032 * 3024 <= MAX_UPLOAD_PIXELS <= 16_000_000
    assert MAX_CONCURRENT_DECODES == 1


@pytest.mark.parametrize(("w", "h"), [(3840, 2160), (2160, 3840), (4032, 3024)])
def test_an_upload_the_phone_could_send_passes(w, h) -> None:
    check_upload(one_bit_png(w, h))


@pytest.mark.parametrize(("w", "h"), [(5000, 4000), (7300, 5400), (16000, 1001)])
def test_an_upload_over_the_cap_is_refused_before_it_is_decoded(w, h) -> None:
    data = one_bit_png(w, h)
    assert len(data) < 200_000
    with pytest.raises(ImageError, match="pixel limit"):
        check_upload(data)


def test_a_fetched_image_keeps_the_larger_source_cap() -> None:
    """Images from links and collections may be large photos; only uploads are capped
    at what the phone sends."""
    out = fit_to_tv(one_bit_png(6000, 4000), None)
    assert Image.open(io.BytesIO(out)).size == (3840, 2160)


# --- the decode gate -----------------------------------------------------------------------


class Tracker:
    """A sync decode stand-in that records how many run at once."""

    def __init__(self, seconds: float = 0.05) -> None:
        self.seconds = seconds
        self.running = self.peak = self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, *args):
        with self._lock:
            self.running += 1
            self.calls += 1
            self.peak = max(self.peak, self.running)
        time.sleep(self.seconds)
        with self._lock:
            self.running -= 1
        return args


async def test_one_decode_at_a_time(hass) -> None:
    gate, work = DecodeGate(), Tracker()
    results = await asyncio.gather(*(gate.run(hass, work, i) for i in range(6)))
    assert results == [(i,) for i in range(6)]
    assert work.calls == 6 and work.peak == 1


async def test_the_gate_is_shared_by_the_whole_integration(hass) -> None:
    assert decode_gate(hass) is decode_gate(hass)


async def test_a_failed_decode_frees_its_slot(hass) -> None:
    gate = DecodeGate()

    def boom():
        raise ImageError("bad")

    with pytest.raises(ImageError):
        await gate.run(hass, boom)
    assert await gate.run(hass, lambda: "next") == "next"


async def test_a_cancelled_decode_keeps_its_slot_until_its_thread_ends(hass) -> None:
    """Cancelling the caller can't stop the thread: the memory is still in use."""
    gate, release, started = DecodeGate(), threading.Event(), threading.Event()
    work = Tracker(0)

    def slow():
        started.set()
        release.wait(5)
        return "slow"

    first = hass.async_create_task(gate.run(hass, slow))
    await hass.async_add_executor_job(started.wait, 5)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = hass.async_create_task(gate.run(hass, work))
    await asyncio.sleep(0.05)
    assert work.calls == 0  # still waiting for the cancelled decode's thread
    release.set()
    await second
    assert work.calls == 1


async def test_a_bounded_wait_gives_up_with_decode_busy(hass) -> None:
    gate, release, started = DecodeGate(), threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(5)

    first = hass.async_create_task(gate.run(hass, slow))
    await hass.async_add_executor_job(started.wait, 5)
    with pytest.raises(DecodeBusy):
        await gate.run(hass, lambda: None, wait=0.05)
    release.set()
    await first
    assert await gate.run(hass, lambda: "free", wait=0.05) == "free"


# --- TV ticks ------------------------------------------------------------------------------


def spec(tv_id: str) -> dict:
    return {
        "version": 1,
        "tv": {"id": tv_id, "kind": "frame", "host": "192.168.1.50", "name": tv_id},
        "interval_seconds": 60,
        "source": {"type": "custom_url", "custom_url": "https://example.com/a.jpg"},
    }


async def test_ten_tvs_fitting_at_once_decode_one_at_a_time(hass, monkeypatch) -> None:
    """Every TV ticks at once when HA starts: their fits queue up."""
    work = Tracker()
    monkeypatch.setattr("custom_components.tvcanvas.runner.fit_to_tv", work)
    store = StateStore(hass, "e")
    await store.async_load()
    runners = [
        TVRunner(
            hass,
            AsyncMock(),
            None,
            store,
            parse_spec(spec(f"tv-{i}")),
            AsyncMock(),
            lambda: None,
            lambda: None,
        )
        for i in range(10)
    ]
    await asyncio.gather(*(r._fit(b"IMG", None) for r in runners))
    assert work.calls == 10 and work.peak == 1


# --- phone uploads -------------------------------------------------------------------------


def jpeg(color) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(out, "JPEG")
    return out.getvalue()


def url(data: bytes) -> str:
    return f"/api/tvcanvas/lan/image/{hashlib.sha256(data).hexdigest()}"


async def test_an_upload_waits_for_a_tv_decode_and_then_is_stored(hass, lan) -> None:  # noqa: F811
    key = await lan.pair()
    gate = decode_gate(hass)
    await gate._slots.acquire()  # a TV is fitting a large image
    data = jpeg((1, 2, 3))
    put = hass.async_create_task(lan.client.put(url(data), data=data, headers={KEY: key}))
    await asyncio.sleep(0.1)
    assert not put.done() and lan.photos.files == 0
    gate._slots.release()
    assert (await put).status == 201 and lan.photos.files == 1


async def test_an_upload_that_waits_too_long_is_told_to_retry(
    hass,
    lan,  # noqa: F811
    monkeypatch,
) -> None:
    monkeypatch.setattr("custom_components.tvcanvas.lan_view.LAN_DECODE_WAIT_SECONDS", 0.05)
    key = await lan.pair()
    gate = decode_gate(hass)
    await gate._slots.acquire()
    data = jpeg((4, 5, 6))
    resp = await lan.client.put(url(data), data=data, headers={KEY: key})
    gate._slots.release()
    assert resp.status == 429
    assert await resp.json() == {"error": "rate_limited", "retry_after": 10}
    assert lan.photos.files == 0
    assert (await lan.client.put(url(data), data=data, headers={KEY: key})).status == 201


@pytest.fixture
def body_reads(monkeypatch):
    """Records each body read; a request with an `X-Stall` header is a sender that stops
    sending: its body never finishes arriving."""
    from custom_components.tvcanvas import lan_view

    reads: list[str] = []
    real_read = lan_view._read_capped

    async def read(request, cap):
        reads.append(request.headers.get("X-Stall", "body"))
        if "X-Stall" in request.headers:
            await asyncio.Event().wait()
        return await real_read(request, cap)

    monkeypatch.setattr(lan_view, "_read_capped", read)
    return reads


def hub(hass):
    from custom_components.tvcanvas.const import DOMAIN

    return hass.data[DOMAIN]["lan"]


async def test_a_phone_has_at_most_two_uploads_at_once_counted_before_reading(
    hass,
    lan,  # noqa: F811
    body_reads,
) -> None:
    """Each upload holds its body (up to 10 MB) until it is stored."""
    key = await lan.pair()
    body_reads.clear()  # pairing read its own body
    gate = decode_gate(hass)
    await gate._slots.acquire()
    held = [jpeg((i, i, i)) for i in (10, 20)]
    waiting = [
        hass.async_create_task(lan.client.put(url(d), data=d, headers={KEY: key})) for d in held
    ]
    await asyncio.sleep(0.1)
    extra = jpeg((30, 30, 30))
    resp = await lan.client.put(url(extra), data=extra, headers={KEY: key})
    assert resp.status == 429
    assert await resp.json() == {"error": "rate_limited", "retry_after": 10}
    assert len(body_reads) == 2  # the third body was never read
    gate._slots.release()
    assert [(await w).status for w in waiting] == [201, 201]
    assert (await lan.client.put(url(extra), data=extra, headers={KEY: key})).status == 201
    assert hub(hass).uploading == {} and hub(hass).decoding == 0


async def test_at_most_four_read_uploads_wait_for_the_decode_slot(
    hass,
    lan,  # noqa: F811
    monkeypatch,
) -> None:
    monkeypatch.setattr("custom_components.tvcanvas.lan_view.LAN_UPLOADS_AT_ONCE", 2)
    keys = [await lan.pair(f"phone {i}") for i in range(3)]
    gate = decode_gate(hass)
    await gate._slots.acquire()
    held = [jpeg((i, i, i)) for i in (10, 20)]
    waiting = [
        hass.async_create_task(lan.client.put(url(d), data=d, headers={KEY: k}))
        for d, k in zip(held, keys, strict=False)
    ]
    await asyncio.sleep(0.1)
    extra = jpeg((30, 30, 30))
    resp = await lan.client.put(url(extra), data=extra, headers={KEY: keys[2]})
    assert resp.status == 429
    gate._slots.release()
    assert [(await w).status for w in waiting] == [201, 201]
    assert (await lan.client.put(url(extra), data=extra, headers={KEY: keys[2]})).status == 201


async def test_a_stalled_sender_is_cut_off_and_never_blocks_other_phones(
    hass,
    lan,  # noqa: F811
    monkeypatch,
    body_reads,
) -> None:
    """Security review of this PR: a sender on the LAN that stops sending must not hold
    upload slots for ever. Its body read times out (408), it only ever held its own
    phone's slots, and other phones' uploads go through meanwhile."""
    monkeypatch.setattr("custom_components.tvcanvas.lan_view.LAN_BODY_READ_SECONDS", 3)
    slow, other = await lan.pair("slow"), await lan.pair("other")
    stalled_data = [jpeg((i, 0, 0)) for i in (40, 50)]
    stalled = [
        hass.async_create_task(lan.client.put(url(d), data=d, headers={KEY: slow, "X-Stall": "1"}))
        for d in stalled_data
    ]
    await asyncio.sleep(0.05)
    # The stalled phone's own third upload waits its turn...
    third = jpeg((60, 0, 0))
    assert (await lan.client.put(url(third), data=third, headers={KEY: slow})).status == 429
    # ...while every other phone uploads as usual, before the stalled ones time out.
    for i in range(5):
        data = jpeg((0, 40 * i, 200))
        resp = await lan.client.put(url(data), data=data, headers={KEY: other})
        assert resp.status == 201
    assert not any(t.done() for t in stalled)
    for task in stalled:
        resp = await task
        assert resp.status == 408 and await resp.json() == {"error": "timeout"}
    assert hub(hass).uploading == {} and hub(hass).decoding == 0
    # Its slots are free again.
    assert (await lan.client.put(url(third), data=third, headers={KEY: slow})).status == 201
