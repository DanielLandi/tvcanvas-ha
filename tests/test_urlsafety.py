"""#7: image fetches stay off the LAN; URL credentials never reach logs, state or diagnostics."""

from __future__ import annotations

import asyncio
import io
import ipaddress
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from PIL import Image

from custom_components.tvcanvas.diagnostics import async_get_config_entry_diagnostics
from custom_components.tvcanvas.sources import SourceError, fetch_image
from custom_components.tvcanvas.urlsafety import (
    NonPublicAddress,
    PublicOnlyResolver,
    create_image_session,
    is_public_address,
    redact,
)

from .common import URL, entry
from .conftest import PUBLIC_IP

NON_PUBLIC = ["192.168.1.10", "10.0.0.5", "127.0.0.1", "::1", "fe80::1", "100.64.0.1"]
JPEG = {"Content-Type": "image/jpeg"}


@pytest.mark.parametrize(
    ("address", "public"),
    [
        (PUBLIC_IP, True),
        ("2606:4700::1111", True),
        *[(a, False) for a in NON_PUBLIC],
        ("fe80::1%eth0", False),
        ("172.16.0.1", False),
        ("169.254.169.254", False),
        ("0.0.0.0", False),
        ("224.0.0.1", False),
        ("fc00::1", False),
        ("::ffff:192.168.1.1", False),
        ("2002:c0a8:101::1", False),  # 6to4 for 192.168.1.1
        ("64:ff9b::c0a8:101", False),  # NAT64 for 192.168.1.1
        ("64:ff9b::808:808", True),  # NAT64 for 8.8.8.8
    ],
)
def test_is_public_address(address, public) -> None:
    assert is_public_address(ipaddress.ip_address(address)) is public


# --- fetch_image resolves every hop -----------------------------------------


@pytest.mark.parametrize("address", NON_PUBLIC)
async def test_name_resolving_to_a_non_public_address_is_refused(
    hass, aioclient_mock, fake_dns, address
) -> None:
    fake_dns.answers["lan.example"] = [address]
    aioclient_mock.get("https://lan.example/a.jpg", content=b"SECRET", headers=JPEG)
    session = async_get_clientsession(hass)
    with pytest.raises(SourceError, match="non-public"):
        await fetch_image(session, "https://lan.example/a.jpg")
    assert aioclient_mock.call_count == 0


async def test_any_non_public_answer_refuses_the_host(hass, aioclient_mock, fake_dns) -> None:
    fake_dns.answers["mixed.example"] = [PUBLIC_IP, "192.168.1.10"]
    aioclient_mock.get("https://mixed.example/a.jpg", content=b"SECRET", headers=JPEG)
    with pytest.raises(SourceError, match="non-public"):
        await fetch_image(async_get_clientsession(hass), "https://mixed.example/a.jpg")
    assert aioclient_mock.call_count == 0


async def test_name_resolving_to_public_addresses_is_fetched(
    hass, aioclient_mock, fake_dns
) -> None:
    fake_dns.answers["cam.example"] = [PUBLIC_IP, "2606:4700::1111"]
    aioclient_mock.get("https://cam.example/a.jpg", content=b"IMG", headers=JPEG)
    session = async_get_clientsession(hass)
    assert await fetch_image(session, "https://cam.example/a.jpg") == b"IMG"
    assert fake_dns.lookups == ["cam.example"]


async def test_redirect_to_a_lan_resolving_host_is_refused(hass, aioclient_mock, fake_dns) -> None:
    fake_dns.answers["lan.example"] = ["192.168.1.10"]
    aioclient_mock.get(
        "https://cam.example/a.jpg", status=302, headers={"Location": "https://lan.example/x"}
    )
    aioclient_mock.get("https://lan.example/x", content=b"SECRET", headers=JPEG)
    with pytest.raises(SourceError, match="non-public"):
        await fetch_image(async_get_clientsession(hass), "https://cam.example/a.jpg")
    assert [str(c[1]) for c in aioclient_mock.mock_calls] == ["https://cam.example/a.jpg"]
    assert fake_dns.lookups == ["cam.example", "lan.example"]


async def test_unresolvable_host_is_a_source_error(hass, aioclient_mock, fake_dns) -> None:
    fake_dns.answers["gone.example"] = []
    with pytest.raises(SourceError, match="did not resolve"):
        await fetch_image(async_get_clientsession(hass), "https://gone.example/a.jpg")


# --- the image session only connects to vetted addresses --------------------


@pytest.fixture
def connections(monkeypatch):
    """Record where the image session's connector would open sockets; open none."""
    attempts: list[list[str]] = []

    async def start_connection(addr_infos, **kwargs):
        attempts.append([info[4][0] for info in addr_infos])
        raise OSError("test: no sockets")

    monkeypatch.setattr("aiohttp.connector.aiohappyeyeballs.start_connection", start_connection)
    return attempts


async def test_dns_rebinding_between_check_and_connect_is_refused(
    hass, fake_dns, connections
) -> None:
    # First lookup (fetch_image's check) is public, the connector's lookup is the LAN.
    fake_dns.answers["rebind.example"] = [[PUBLIC_IP], ["192.168.1.10"]]
    session = create_image_session()
    try:
        with pytest.raises(SourceError, match="non-public"):
            await fetch_image(session, "https://rebind.example/a.jpg")
    finally:
        await session.close()
    assert fake_dns.lookups == ["rebind.example", "rebind.example"]
    assert connections == []


async def test_image_session_connects_only_to_the_vetted_addresses(
    hass, fake_dns, connections
) -> None:
    fake_dns.answers["cam.example"] = [PUBLIC_IP, "2606:4700::1111"]
    session = create_image_session()
    try:
        with pytest.raises(SourceError, match="Cannot connect"):
            await fetch_image(session, "https://cam.example/a.jpg")
    finally:
        await session.close()
    assert connections == [[PUBLIC_IP, "2606:4700::1111"]]


async def test_image_session_refuses_a_lan_answer_on_its_own(fake_dns, connections) -> None:
    fake_dns.answers["lan.example"] = ["10.0.0.5"]
    session = create_image_session()
    try:
        with pytest.raises(aiohttp.ClientConnectorError):
            await session.get("https://lan.example/")
    finally:
        await session.close()
    assert connections == []


async def test_resolver_uses_the_loops_getaddrinfo(monkeypatch) -> None:
    """The real path: the event loop's getaddrinfo (off-loop, in the executor)."""
    import socket

    from aiohttp.resolver import ThreadedResolver

    monkeypatch.setattr("custom_components.tvcanvas.urlsafety.ThreadedResolver", ThreadedResolver)
    calls = []

    async def getaddrinfo(host, port, **kwargs):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", port))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)
    resolver = PublicOnlyResolver()
    with pytest.raises(NonPublicAddress):
        await resolver.resolve("lan.example", 443)
    await resolver.close()
    assert calls == ["lan.example"]


# --- redaction ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://user:hunter2@cam.example/a.jpg", "https://cam.example/a.jpg"),
        ("https://user@cam.example:8443/a.jpg", "https://cam.example:8443/a.jpg"),
        ("https://u:p@ss@cam.example/a.jpg", "https://cam.example/a.jpg"),
        ("https://cam.example/a.jpg?token=T&sig=S#key=K", "https://cam.example/a.jpg"),
        (
            "https://u:pw@cam.example/a.jpg?key=K answered 404",
            "https://cam.example/a.jpg answered 404",
        ),
        ("url=URL('https://u:pw@cam.example/a?k=v')", "url=URL('https://cam.example/a')"),
        (
            "https://a.example/1?x=1 -> https://b@b.example/2",
            "https://a.example/1 -> https://b.example/2",
        ),
        ("https://cdn.example/img@2x.png", "https://cdn.example/img@2x.png"),
        (
            "TV unreachable: Unable to connect to 10.0.0.2:8002",
            "TV unreachable: Unable to connect to 10.0.0.2:8002",
        ),
    ],
)
def test_redact(text, expected) -> None:
    assert redact(text) == expected


async def test_fetch_errors_carry_no_credentials(hass, aioclient_mock) -> None:
    url = "https://cam:hunter2@cam.example/snap.jpg?token=T0KEN"
    session = async_get_clientsession(hass)
    aioclient_mock.get(url, status=404)
    with pytest.raises(SourceError) as info:
        await fetch_image(session, url)
    assert str(info.value) == "https://cam.example/snap.jpg answered 404"

    aioclient_mock.clear_requests()
    aioclient_mock.get(url, exc=aiohttp.InvalidURL(url))
    with pytest.raises(SourceError) as info:
        await fetch_image(session, url)
    assert "hunter2" not in str(info.value) and "T0KEN" not in str(info.value)


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 36), "red").save(buf, "JPEG")
    return buf.getvalue()


async def test_credentials_never_reach_logs_attributes_or_diagnostics(
    hass, aioclient_mock, caplog
) -> None:
    secret_url = "https://cam:hunter2@cam.example/snap.jpg?token=T0KEN"
    spec = {
        "version": 1,
        "tv": {"id": "tv-1", "kind": "frame", "host": "10.0.0.2", "name": "TV"},
        "enabled": True,
        "interval_seconds": 300,
        "source": {"type": "custom_url", "custom_url": secret_url},
    }
    aioclient_mock.get(f"{URL}/ha/agent/sync", json={"specs": [spec]}, headers={"ETag": '"e"'})
    aioclient_mock.post(f"{URL}/ha/agent/status", json={})
    aioclient_mock.get(secret_url, status=404)
    caplog.set_level(logging.DEBUG, logger="custom_components.tvcanvas")
    e = entry(hass)
    with (
        patch("custom_components.tvcanvas.runner.TVRunner.start"),
        patch(
            "custom_components.tvcanvas.coordinator.create_image_session",
            lambda: aioclient_mock.create_session(hass.loop),
        ),
    ):
        await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    runner = e.runtime_data.runners["tv-1"]
    runner._display = SimpleNamespace(show=AsyncMock())  # no TV in tests

    await runner.tick()  # the camera answers 404
    await hass.async_block_till_done()
    assert runner.status.last_error == "https://cam.example/snap.jpg answered 404"
    ent_id = er.async_get(hass).async_get_entity_id("sensor", "tvcanvas", f"{e.entry_id}_tv-1")
    attributes = repr(hass.states.get(ent_id).attributes)
    diagnostics = repr(await async_get_config_entry_diagnostics(hass, e))

    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{URL}/ha/agent/status", json={})
    aioclient_mock.get(secret_url, content=_jpeg(), headers=JPEG)
    await runner.tick()  # shown: the debug log names the source
    assert runner.status.last_error is None and runner._display.show.await_count == 1
    assert "now shows https://cam.example/snap.jpg" in caplog.text

    for text in (runner.status.last_error or "", attributes, diagnostics, caplog.text):
        assert "hunter2" not in text and "T0KEN" not in text
    await hass.config_entries.async_unload(e.entry_id)


async def test_sensor_and_diagnostics_redact_whatever_last_error_holds(
    hass, aioclient_mock
) -> None:
    """Defence in depth: even an unredacted error never leaves through state or diagnostics."""
    from .common import frame_spec

    aioclient_mock.get(
        f"{URL}/ha/agent/sync", json={"specs": [frame_spec("tv-1")]}, headers={"ETag": '"e"'}
    )
    e = entry(hass)
    with patch("custom_components.tvcanvas.runner.TVRunner.start"):
        await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    e.runtime_data.runners["tv-1"].status.last_error = "https://u:hunter2@cam.example/a?k=T0KEN"
    e.runtime_data.async_update_listeners()
    await hass.async_block_till_done()
    ent_id = er.async_get(hass).async_get_entity_id("sensor", "tvcanvas", f"{e.entry_id}_tv-1")
    assert hass.states.get(ent_id).attributes["last_error"] == "https://cam.example/a"
    diag = await async_get_config_entry_diagnostics(hass, e)
    assert diag["tvs"]["tv-1"]["last_error"] == "https://cam.example/a"
    await hass.config_entries.async_unload(e.entry_id)
