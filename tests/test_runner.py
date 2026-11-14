from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from custom_components.tvcanvas.api import AuthError, OutOfCredits, RateLimited
from custom_components.tvcanvas.displays import DisplayError
from custom_components.tvcanvas.models import parse_spec
from custom_components.tvcanvas.runner import TVRunner
from custom_components.tvcanvas.sources import SourceError
from custom_components.tvcanvas.storage import StateStore


def board_spec(interval=300, enabled=True, type_id="weather"):
    return parse_spec(
        {
            "version": 1,
            "tv": {"id": "tv-1", "kind": "frame", "host": "10.0.0.2", "name": "T"},
            "enabled": enabled,
            "interval_seconds": interval,
            "source": {"type": "board", "board": {"type_id": type_id, "params": {}}},
        }
    )


BOARD_ITEM = {"type": "board", "board": {"type_id": "weather"}}
CAM_ITEM = {"type": "webcam", "url": "https://cams.example.com/a.jpg", "name": "Cam"}


def group_runner_spec(interval=300, items=(BOARD_ITEM, CAM_ITEM)):
    return parse_spec(
        {
            "version": 1,
            "tv": {"id": "tv-1", "kind": "frame", "host": "10.0.0.2", "name": "T"},
            "interval_seconds": interval,
            "source": {"type": "group", "group": {"items": list(items)}},
        }
    )


class FakeDisplay:
    def __init__(self, exc=None, delay=0, waiting=None, ready_exc=None):
        self.shown, self.exc, self.delay = [], exc, delay
        self.waiting, self.ready_exc, self.ready_calls = waiting, ready_exc, 0

    async def ready(self):
        self.ready_calls += 1
        if self.ready_exc:
            raise self.ready_exc
        return self.waiting

    async def show(self, jpeg):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        self.shown.append(jpeg)


@pytest.fixture
async def runner_factory(hass, monkeypatch):
    store = StateStore(hass, "e")
    await store.async_load()
    client = AsyncMock()
    auth_errors = []

    def make(spec, display, source=None):
        async def default_source(spec, rotation, client, session, fit, credits=None, photos=None):
            return (b"IMG:" + spec.source.board.type_id.encode(), "label")

        monkeypatch.setattr(
            "custom_components.tvcanvas.runner.next_image", source or default_source
        )
        return TVRunner(
            hass,
            client,
            None,
            store,
            spec,
            display,
            on_auth_error=lambda: auth_errors.append(1),
            on_update=lambda: None,
        )

    make.client, make.auth_errors = client, auth_errors
    return make


async def test_success_shows_and_reports_ok(runner_factory) -> None:
    disp = FakeDisplay()
    r = runner_factory(board_spec(), disp)
    await r.tick()
    assert disp.shown == [b"IMG:weather"]
    assert r.status.last_ok is not None and r.status.last_error is None
    args = runner_factory.client.agent_status.call_args[0]
    assert args[0] == "tv-1" and args[1] is True


async def test_disabled_spec_does_nothing(runner_factory) -> None:
    disp = FakeDisplay()
    r = runner_factory(board_spec(enabled=False), disp)
    await r.tick()
    assert disp.shown == [] and not runner_factory.client.agent_status.called


async def test_out_of_credits_pauses_board_for_30_min(runner_factory, freezer) -> None:
    calls = []

    async def no_credits(*a):
        calls.append(1)
        raise OutOfCredits()

    r = runner_factory(board_spec(), FakeDisplay(), no_credits)
    await r.tick()
    await r.tick()
    assert len(calls) == 1 and r.status.last_error == "out of credits"
    freezer.tick(1801)
    await r.tick()
    assert len(calls) == 2


async def test_rate_limited_is_silent(runner_factory) -> None:
    async def limited(*a):
        raise RateLimited()

    r = runner_factory(board_spec(), FakeDisplay(), limited)
    await r.tick()
    assert not runner_factory.client.agent_status.called and r.status.last_error is None


async def test_auth_error_escalates(runner_factory) -> None:
    async def revoked(*a):
        raise AuthError()

    r = runner_factory(board_spec(), FakeDisplay(), revoked)
    await r.tick()
    assert runner_factory.auth_errors == [1]


async def test_source_error_keeps_tv_and_reports(runner_factory) -> None:
    async def broken(*a):
        raise SourceError("no usable images (3 tried)")

    disp = FakeDisplay()
    r = runner_factory(board_spec(), disp, broken)
    await r.tick()
    assert disp.shown == [] and r.status.last_error == "no usable images (3 tried)"
    assert runner_factory.client.agent_status.call_args[0][1] is False


async def test_display_failure_backs_off_then_resets(runner_factory) -> None:
    disp = FakeDisplay(exc=DisplayError("TV unreachable: x"))
    r = runner_factory(board_spec(interval=60), disp)
    delays = []
    for _ in range(6):
        await r.tick()
        delays.append(r.next_delay())
    assert delays == [60, 120, 240, 480, 900, 900]
    disp.exc = None
    await r.tick()
    assert r.next_delay() == 60


async def test_backoff_never_shorter_than_interval(runner_factory) -> None:
    r = runner_factory(board_spec(interval=3600), FakeDisplay(exc=DisplayError("x")))
    await r.tick()
    assert r.next_delay() == 3600


async def test_status_report_failure_is_ignored(runner_factory) -> None:
    from custom_components.tvcanvas.api import CloudError

    runner_factory.client.agent_status.side_effect = CloudError("down")
    r = runner_factory(board_spec(), FakeDisplay())
    await r.tick()
    assert r.status.last_ok is not None


async def test_update_mid_tick_shows_new_source_only(hass, runner_factory) -> None:
    """Review focus 2: a spec edit during a slow upload wins, nothing leaks."""
    slow = FakeDisplay(delay=0.2)
    r = runner_factory(board_spec(type_id="weather"), slow)
    r.start()
    await asyncio.sleep(0.05)  # first tick is mid-show
    fast = FakeDisplay()
    await r.update(board_spec(type_id="flights"), fast)
    await asyncio.sleep(0.05)
    await r.stop()
    # in-flight I/O is never abandoned (superseded by the review: it finishes first)
    assert slow.shown == [b"IMG:weather"]
    assert fast.shown == [b"IMG:flights"]


async def test_loop_survives_unexpected_exception(runner_factory) -> None:
    calls = []

    async def boom(*a):
        calls.append(1)
        raise RuntimeError("Session is closed")

    r = runner_factory(board_spec(type_id="weather"), FakeDisplay(), boom)
    r.next_delay = lambda: 0.01  # type: ignore[method-assign]
    r.start()
    await asyncio.sleep(0.1)
    assert len(calls) >= 2
    assert r.status.last_error == "internal error"
    args = runner_factory.client.agent_status.call_args[0]
    assert args[0] == "tv-1" and args[1] is False and args[2] == "internal error"
    fast = FakeDisplay()
    await r.update(board_spec(type_id="flights"), fast)
    assert r.spec.source.board.type_id == "flights"
    await r.stop()


async def test_stop_tolerates_task_that_died(runner_factory) -> None:
    r = runner_factory(board_spec(), FakeDisplay())

    async def dead():
        raise RuntimeError("x")

    r._task = asyncio.ensure_future(dead())
    await asyncio.sleep(0)
    await r.stop()


class BlockingDisplay:
    def __init__(self, hass, events, name):
        self.hass, self.events, self.name = hass, events, name

    async def show(self, jpeg):
        def work():
            time.sleep(0.2)
            self.events.append((self.name, jpeg))

        await self.hass.async_add_executor_job(work)


async def test_update_waits_for_in_flight_display_io(hass, runner_factory) -> None:
    events: list = []
    r = runner_factory(board_spec(type_id="weather"), BlockingDisplay(hass, events, "slow"))
    r.start()
    await asyncio.sleep(0.05)
    fast = BlockingDisplay(hass, events, "fast")
    fast.show = lambda jpeg: _record(events, "fast", jpeg)
    await r.update(board_spec(type_id="flights"), fast)
    await asyncio.sleep(0.05)
    await r.stop()
    assert events == [("slow", b"IMG:weather"), ("fast", b"IMG:flights")]
    assert not _runner_tasks()


async def _record(events, name, jpeg):
    events.append((name, jpeg))


def _runner_tasks():
    return [
        t for t in asyncio.all_tasks() if "tvcanvas runner tv-1" in t.get_name() and not t.done()
    ]


async def test_concurrent_updates_leak_no_task(runner_factory) -> None:
    r = runner_factory(board_spec(), FakeDisplay())
    r.start()
    await asyncio.sleep(0)
    await asyncio.gather(
        r.update(board_spec(type_id="a"), FakeDisplay()),
        r.update(board_spec(type_id="b"), FakeDisplay()),
    )
    await r.stop()
    await asyncio.sleep(0)
    assert not _runner_tasks()


async def test_no_cloud_calls_after_auth_error(runner_factory) -> None:
    calls = []

    async def revoked(*a):
        calls.append(1)
        raise AuthError()

    r = runner_factory(board_spec(), FakeDisplay(), revoked)
    await r.tick()
    await r.tick()
    assert len(calls) == 1 and runner_factory.auth_errors == [1]
    await r.update(board_spec(), FakeDisplay())
    await r.stop()
    assert len(calls) == 2


async def test_status_report_auth_error_escalates(runner_factory) -> None:
    runner_factory.client.agent_status.side_effect = AuthError()
    r = runner_factory(board_spec(), FakeDisplay())
    await r.tick()
    assert runner_factory.auth_errors == [1]
    await r.tick()
    assert runner_factory.auth_errors == [1]


async def test_stop_does_not_swallow_caller_cancellation(runner_factory) -> None:
    r = runner_factory(board_spec(), FakeDisplay(delay=0.3))
    r.start()
    await asyncio.sleep(0.05)
    caller = asyncio.ensure_future(r.stop())
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await r.stop()


async def test_on_update_called_on_success(hass, runner_factory) -> None:
    updates = []
    r = runner_factory(board_spec(), FakeDisplay())
    r._on_update = lambda: updates.append(1)
    await r.tick()
    assert updates


async def test_out_of_credits_reports_failure(runner_factory) -> None:
    async def no_credits(*a):
        raise OutOfCredits()

    r = runner_factory(board_spec(), FakeDisplay(), no_credits)
    await r.tick()
    args = runner_factory.client.agent_status.call_args[0]
    assert args[1] is False and args[2] == "out of credits"


async def test_cloud_gets_generic_source_error_and_local_status_keeps_detail(
    runner_factory,
) -> None:
    detail = "https://192.168.1.9/cam.jpg: Cannot connect to host [Connection refused]"

    async def broken(*a):
        raise SourceError(detail)

    r = runner_factory(board_spec(), FakeDisplay(), broken)
    await r.tick()
    assert r.status.last_error == detail
    assert runner_factory.client.agent_status.call_args[0][2] == "image fetch failed"


async def test_cloud_gets_generic_display_error(runner_factory) -> None:
    detail = "TV unreachable: Cannot connect to 10.0.0.2:8002 [Connection refused]"
    r = runner_factory(board_spec(), FakeDisplay(exc=DisplayError(detail)))
    await r.tick()
    assert r.status.last_error == detail
    assert runner_factory.client.agent_status.call_args[0][2] == "TV unreachable"


async def _loop_once_with_internal_error(runner_factory):
    async def boom(*a):
        raise RuntimeError("Session is closed")

    r = runner_factory(board_spec(), FakeDisplay(), boom)
    r.next_delay = lambda: 0.01  # type: ignore[method-assign]
    r.start()
    await asyncio.sleep(0.05)
    await r.stop()
    return r


async def test_internal_error_report_swallows_cloud_error(runner_factory) -> None:
    from custom_components.tvcanvas.api import CloudError

    runner_factory.client.agent_status.side_effect = CloudError("down")
    r = await _loop_once_with_internal_error(runner_factory)
    assert r.status.last_error == "internal error"
    assert runner_factory.client.agent_status.await_count >= 2  # loop kept going


async def test_internal_error_report_honours_auth_error(runner_factory) -> None:
    runner_factory.client.agent_status.side_effect = AuthError()
    await _loop_once_with_internal_error(runner_factory)
    assert runner_factory.auth_errors == [1]
    assert runner_factory.client.agent_status.await_count == 1


# --- #3: a quick reachability probe during "TV unreachable" backoff -----------


def _backing_off(runner_factory, failures=5):
    r = runner_factory(board_spec(interval=60), FakeDisplay(exc=DisplayError("TV unreachable: x")))
    return r, failures


async def _fail_ticks(r, n):
    for _ in range(n):
        await r.tick()


def _scripted(r, probes):
    """Replace sleep and probe with recorders; probes is a list of results (last repeats)."""
    slept, probed = [], []

    async def sleep(seconds):
        slept.append(seconds)

    async def probe(host, port):
        probed.append((host, port))
        return probes[min(len(probed) - 1, len(probes) - 1)]

    r._sleep, r._probe = sleep, probe
    return slept, probed


async def test_tv_back_ends_the_backoff_early(runner_factory) -> None:
    r, n = _backing_off(runner_factory)
    await _fail_ticks(r, n)
    assert r.next_delay() == 900
    slept, probed = _scripted(r, [False, False, True])
    await r._wait()
    assert slept == [60, 60]
    assert probed[0] == ("10.0.0.2", 8002)


async def test_tv_still_off_keeps_the_full_backoff(runner_factory) -> None:
    r, n = _backing_off(runner_factory)
    await _fail_ticks(r, n)
    slept, probed = _scripted(r, [False])
    await r._wait()
    assert sum(slept) == 900 and all(s == 60 for s in slept)
    assert len(probed) == 15  # one at the start, then after every chunk but the last


async def test_port_answering_all_along_does_not_cut_the_backoff(runner_factory) -> None:
    """The TV answers on its port but show() fails: no probe-driven retries."""
    r, n = _backing_off(runner_factory)
    await _fail_ticks(r, n)
    slept, _ = _scripted(r, [True])
    await r._wait()
    assert sum(slept) == 900


async def test_cast_probes_port_8009(runner_factory) -> None:
    spec = parse_spec(
        {
            "version": 1,
            "tv": {"id": "tv-2", "kind": "cast", "host": "10.0.0.3", "name": "C"},
            "enabled": True,
            "interval_seconds": 60,
            "source": {"type": "board", "board": {"type_id": "weather", "params": {}}},
        }
    )
    r = runner_factory(spec, FakeDisplay(exc=DisplayError("x")))
    await _fail_ticks(r, 3)
    _, probed = _scripted(r, [False, True])
    await r._wait()
    assert probed[0] == ("10.0.0.3", 8009)


async def test_no_probe_outside_unreachable_backoff(runner_factory) -> None:
    r = runner_factory(board_spec(interval=60), FakeDisplay())
    await r.tick()
    slept, probed = _scripted(r, [True])
    await r._wait()
    assert slept == [60] and probed == []


async def test_no_probe_after_an_internal_error(runner_factory) -> None:
    async def boom(*a):
        raise RuntimeError("x")

    r = runner_factory(board_spec(interval=60), FakeDisplay(), boom)
    r._failures = 3  # what _loop does after unexpected errors
    slept, probed = _scripted(r, [True])
    await r._wait()
    assert slept == [240] and probed == []


async def test_success_after_backoff_stops_probing(runner_factory) -> None:
    disp = FakeDisplay(exc=DisplayError("x"))
    r = runner_factory(board_spec(interval=60), disp)
    await _fail_ticks(r, 3)
    disp.exc = None
    await r.tick()
    slept, probed = _scripted(r, [True])
    await r._wait()
    assert slept == [60] and probed == []


async def test_tcp_reachable_against_real_sockets(socket_enabled) -> None:
    # The socket_enabled fixture, not the enable_socket marker: the marker is applied in
    # pytest-socket's setup hook, and pytest-homeassistant's own hook disables sockets again
    # when it runs later. Hook order follows plugin load order, which differs between
    # machines (it failed on GitHub's runner, passed on the Mac). Fixtures run after both.
    from custom_components.tvcanvas.runner import tcp_reachable

    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await tcp_reachable("127.0.0.1", port) is True
    finally:
        server.close()
        await server.wait_closed()
    assert await tcp_reachable("127.0.0.1", port) is False


async def test_tcp_reachable_times_out_quietly(monkeypatch) -> None:
    from custom_components.tvcanvas import runner as runner_mod

    async def hang(*a, **k):
        await asyncio.sleep(10)

    monkeypatch.setattr(runner_mod.asyncio, "open_connection", hang)
    t0 = time.monotonic()
    assert await runner_mod.tcp_reachable("10.0.0.2", 8002, connect_seconds=0.05) is False
    assert time.monotonic() - t0 < 1


# --- Frame mode: a Frame in Watch TV or Art is left alone ---------------------


async def test_waiting_tv_is_not_fetched_and_reports_waiting(runner_factory) -> None:
    fetched = []

    async def source(*a):
        fetched.append(1)
        return (b"IMG", "label")

    disp = FakeDisplay(waiting="waiting: Watch TV")
    r = runner_factory(board_spec(interval=60), disp, source)
    await r.tick()
    assert fetched == [] and disp.shown == []
    args = runner_factory.client.agent_status.call_args[0]
    assert args[0] == "tv-1" and args[1] is False and args[2] == "waiting: Watch TV"
    assert r.status.last_error == "waiting: Watch TV"
    assert r.next_delay() == 60  # waiting is not a failure: no backoff


async def test_unreachable_before_fetch_backs_off_without_spending(runner_factory) -> None:
    fetched = []

    async def source(*a):
        fetched.append(1)
        return (b"IMG", "label")

    disp = FakeDisplay(ready_exc=DisplayError("TV unreachable: timed out"))
    r = runner_factory(board_spec(interval=60), disp, source)
    await r.tick()
    assert fetched == []
    assert runner_factory.client.agent_status.call_args[0][2] == "TV unreachable"
    assert r.next_delay() == 60 and r._failures == 1 and r._unreachable


async def test_ready_display_is_fetched_and_shown(runner_factory) -> None:
    disp = FakeDisplay()
    r = runner_factory(board_spec(), disp)
    await r.tick()
    assert disp.ready_calls == 1 and disp.shown == [b"IMG:weather"]


async def test_display_without_ready_still_works(runner_factory) -> None:
    class Plain:
        def __init__(self):
            self.shown = []

        async def show(self, jpeg):
            self.shown.append(jpeg)

    disp = Plain()
    r = runner_factory(board_spec(), disp)
    await r.tick()
    assert disp.shown == [b"IMG:weather"]


# --- group pins ---------------------------------------------------------------


async def test_all_boards_group_out_of_credits_pauses_the_tv_for_30_min(
    runner_factory, freezer
) -> None:
    calls = []

    async def no_credits(*a):
        calls.append(1)
        raise OutOfCredits()

    spec = group_runner_spec(items=(BOARD_ITEM, {"type": "board", "board": {"type_id": "fx"}}))
    r = runner_factory(spec, FakeDisplay(), no_credits)
    await r.tick()
    await r.tick()
    assert len(calls) == 1 and r.status.last_error == "out of credits"
    freezer.tick(1801)
    await r.tick()
    assert len(calls) == 2


@pytest.fixture
def real_group(runner_factory, monkeypatch):
    """A runner on the real group rotation: boards from the cloud mock, webcams answer CAM."""
    from custom_components.tvcanvas import sources

    fetched = []

    async def fake_fetch(session, url):
        fetched.append(url)
        return b"CAM:" + url.encode()

    monkeypatch.setattr(sources, "fetch_image", fake_fetch)

    def make(spec):
        disp = FakeDisplay()
        r = runner_factory(spec, disp, sources.next_image)
        r._fit = AsyncMock(side_effect=lambda data, orientation: data)
        return r, disp

    make.fetched = fetched
    return make


CAM = b"CAM:https://cams.example.com/a.jpg"


async def test_mixed_group_402_keeps_showing_links_and_reports_out_of_credits(
    runner_factory, real_group, freezer
) -> None:
    from homeassistant.util import dt as dt_util

    runner_factory.client.board = AsyncMock(side_effect=OutOfCredits())
    r, disp = real_group(group_runner_spec())
    start = dt_util.utcnow()
    await r.tick()
    # The 402 board is skipped and the webcam shows in the same tick.
    assert disp.shown == [CAM] and runner_factory.client.board.await_count == 1
    assert r.boards_paused_until == start + timedelta(seconds=1800)
    # The phone still sees "out of credits" (and its Buy credits button) ...
    args = runner_factory.client.agent_status.call_args[0]
    assert args[1] is False and args[2] == "out of credits"
    # ... while HA's own sensor shows the TV did update.
    assert r.status.last_ok is not None
    assert r.status.last_error.startswith("out of credits")
    freezer.tick(600)
    await r.tick()
    # Within the window the board isn't asked: no request, no charge.
    assert disp.shown == [CAM, CAM] and runner_factory.client.board.await_count == 1
    assert runner_factory.client.agent_status.call_args[0][2] == "out of credits"
    assert r.next_delay() == 300  # no backoff


async def test_mixed_group_retries_boards_after_the_window(
    runner_factory, real_group, freezer
) -> None:
    from homeassistant.util import dt as dt_util

    cam2 = {"type": "custom_url", "url": "https://cams.example.com/b.jpg", "name": "B"}
    runner_factory.client.board = AsyncMock(side_effect=OutOfCredits())
    r, disp = real_group(group_runner_spec(items=(BOARD_ITEM, CAM_ITEM, cam2)))
    await r.tick()  # board 402 -> Cam
    freezer.tick(1801)
    await r.tick()  # B: no board this tick, so still reported out of credits
    assert disp.shown[-1] == b"CAM:https://cams.example.com/b.jpg"
    assert runner_factory.client.agent_status.call_args[0][2] == "out of credits"
    assert runner_factory.client.board.await_count == 1
    await r.tick()  # the board's turn again: asked, 402 again -> the window restarts
    assert runner_factory.client.board.await_count == 2 and disp.shown[-1] == CAM
    assert r.boards_paused_until == dt_util.utcnow() + timedelta(seconds=1800)
    freezer.tick(1801)
    runner_factory.client.board = AsyncMock(return_value=b"BOARD")
    await r.tick()  # B
    await r.tick()  # the board renders: credits are back
    assert disp.shown[-1] == b"BOARD"
    assert r.boards_paused_until is None and r.status.last_error is None
    assert runner_factory.client.agent_status.call_args[0][1] is True


async def test_mixed_group_with_boards_paused_and_links_failing_says_out_of_credits(
    runner_factory, real_group, monkeypatch
) -> None:
    from custom_components.tvcanvas import sources

    async def down(session, url):
        raise SourceError("https://cams.example.com/a.jpg answered 503")

    monkeypatch.setattr(sources, "fetch_image", down)
    runner_factory.client.board = AsyncMock(side_effect=OutOfCredits())
    r, disp = real_group(group_runner_spec())
    await r.tick()
    assert disp.shown == []
    # Credits are the problem the user can fix from the phone: report that, not the link.
    assert runner_factory.client.agent_status.call_args[0][2] == "out of credits"
    assert "out of credits" in r.status.last_error and "503" in r.status.last_error
    assert r.boards_paused_until is not None


async def test_mixed_group_cloud_down_is_unchanged(runner_factory, real_group) -> None:
    from custom_components.tvcanvas.api import CloudUnreachable

    runner_factory.client.board = AsyncMock(side_effect=CloudUnreachable("cloud unreachable"))
    r, disp = real_group(group_runner_spec())
    await r.tick()
    assert disp.shown == [CAM] and r.boards_paused_until is None
    assert runner_factory.client.agent_status.call_args[0][1] is True


async def test_group_of_boards_cloud_down_reports_image_fetch_failed(
    runner_factory, real_group
) -> None:
    from custom_components.tvcanvas.api import CloudUnreachable

    runner_factory.client.board = AsyncMock(side_effect=CloudUnreachable("cloud unreachable"))
    spec = group_runner_spec(items=(BOARD_ITEM, {"type": "board", "board": {"type_id": "fx"}}))
    r, disp = real_group(spec)
    await r.tick()
    assert disp.shown == [] and r.boards_paused_until is None
    assert runner_factory.client.agent_status.call_args[0][2] == "image fetch failed"


async def test_item_list_change_during_the_window_retries_boards(
    runner_factory, real_group
) -> None:
    runner_factory.client.board = AsyncMock(side_effect=OutOfCredits())
    r, _ = real_group(group_runner_spec())
    r.start = lambda: None
    await r.tick()
    assert r.boards_paused_until is not None
    # Same source, new interval: the window holds.
    await r.update(group_runner_spec(interval=600), r._display)
    assert r.boards_paused_until is not None
    # The phone edited the group: start afresh, the next board tick asks the cloud.
    cam2 = {"type": "custom_url", "url": "https://cams.example.com/b.jpg", "name": "B"}
    await r.update(group_runner_spec(items=(BOARD_ITEM, CAM_ITEM, cam2)), r._display)
    assert r.boards_paused_until is None
    await r.tick()
    assert runner_factory.client.board.await_count == 2


async def test_waiting_frame_does_not_render_a_groups_board(runner_factory) -> None:
    fetched = []

    async def source(*a):
        fetched.append(1)
        return (b"IMG", "label")

    disp = FakeDisplay(waiting="waiting: Art")
    r = runner_factory(group_runner_spec(interval=60), disp, source)
    await r.tick()
    assert fetched == [] and disp.shown == []
    assert runner_factory.client.agent_status.call_args[0][2] == "waiting: Art"
    assert r.next_delay() == 60


async def test_group_shows_through_the_real_rotation(runner_factory, monkeypatch) -> None:
    from custom_components.tvcanvas import sources

    async def fake_fetch(session, url):
        return b"CAM"

    monkeypatch.setattr(sources, "fetch_image", fake_fetch)
    disp = FakeDisplay()
    r = runner_factory(group_runner_spec(), disp, sources.next_image)
    runner_factory.client.board = AsyncMock(return_value=b"BOARD")
    r._fit = AsyncMock(side_effect=lambda data, orientation: data)
    await r.tick()
    await r.tick()
    await r.tick()
    assert disp.shown == [b"BOARD", b"CAM", b"BOARD"]
    runner_factory.client.board.assert_awaited_with("weather", {})
