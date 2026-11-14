from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import aiohttp
import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.tvcanvas.api import (
    AuthError,
    CloudClient,
    CloudError,
    CloudServerError,
    CloudUnreachable,
    InvalidCode,
    OutOfCredits,
    RateLimited,
    WrongServer,
    normalize_code,
    split_code,
)

URL = "https://cloud.test"
CONN = SimpleNamespace(host="cloud.test", port=443, ssl=True, is_ssl=True)
IMG = {"Content-Type": "image/jpeg"}


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("abcd-2345", "ABCD2345"),
        (" ABCD 2345 ", "ABCD2345"),
        ("OIL0-1234", "0110" + "1234"),
        ("ABCD-234", None),
        ("ABCD-234U", None),  # U is not Crockford
    ],
)
def test_normalize_code(raw, want) -> None:
    assert normalize_code(raw) == want


# --- the 12-character code: cloud code + the phone's LAN setup code (lan-api.md §3) ---


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("K7QM-2XRA", ("K7QM2XRA", None)),
        ("k7qm2xra", ("K7QM2XRA", None)),
        ("K7QM-2XRA-P4NB", ("K7QM2XRA", "P4NB")),
        (" k7qm 2xra p4nb ", ("K7QM2XRA", "P4NB")),
        ("K7QM2XRAP4NB", ("K7QM2XRA", "P4NB")),
        ("K7QM-2XRA-oIlL", ("K7QM2XRA", "0111")),  # lookalikes in the suffix
        ("OIL0-1234-ABCD", ("01101234", "ABCD")),
    ],
)
def test_split_code_accepts_8_or_12(raw, want) -> None:
    assert split_code(raw) == want


@pytest.mark.parametrize(
    "raw",
    [
        "K7QM2XR",  # 7
        "K7QM2XRAP",  # 9
        "K7QM2XRAP4",  # 10
        "K7QM2XRAP4N",  # 11
        "K7QM2XRAP4NBZ",  # 13
        "",
        "K7QM-2XRA-P4NU",  # U is not Crockford
        "K7QM-2XRA-P4N!",
        "K7QM_2XRA",
        "K7QM-2XRA-P4NÉ",
    ],
)
def test_split_code_rejects_other_lengths_and_letters(raw) -> None:
    assert split_code(raw) is None and normalize_code(raw) is None


@pytest.mark.parametrize("code", ["K7QM2XRAP4NB", "P4NB", "K7QM2XR"])
async def test_claim_never_sends_anything_but_8_characters(hass, aioclient_mock, code) -> None:
    aioclient_mock.post(
        f"{URL}/ha/claim", json={"token": "t", "expires_at": "2026-11-10T00:00:00Z"}
    )
    with pytest.raises(InvalidCode):
        await CloudClient(async_get_clientsession(hass), URL).claim(code)
    assert aioclient_mock.call_count == 0


async def test_claim_ok_and_invalid(hass, aioclient_mock) -> None:
    aioclient_mock.post(
        f"{URL}/ha/claim",
        json={"token": "fc_ha_x.y", "expires_at": "2026-11-10T00:00:00Z"},
    )
    client = CloudClient(async_get_clientsession(hass), URL)
    info = await client.claim("ABCD2345")
    assert info.token == "fc_ha_x.y"
    assert info.expires_at == datetime(2026, 11, 10, tzinfo=UTC)
    assert aioclient_mock.mock_calls[0][2] == {"code": "ABCD2345"}

    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{URL}/ha/claim", status=400, text="Invalid code")
    with pytest.raises(InvalidCode):
        await client.claim("ABCD2345")


async def test_agent_sync_etag_and_304(hass, aioclient_mock) -> None:
    client = CloudClient(async_get_clientsession(hass), URL, token="fc_ha_t")
    aioclient_mock.get(f"{URL}/ha/agent/sync", json={"specs": [{"a": 1}]}, headers={"ETag": '"e1"'})
    res = await client.agent_sync(None)
    assert res.specs == [{"a": 1}] and res.etag == '"e1"'
    assert aioclient_mock.mock_calls[0][3]["Authorization"] == "Bearer fc_ha_t"

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/ha/agent/sync", status=304)
    res = await client.agent_sync('"e1"')
    assert res.specs is None and res.etag == '"e1"'
    assert aioclient_mock.mock_calls[0][3]["If-None-Match"] == '"e1"'


@pytest.mark.parametrize(
    ("status", "exc"),
    [(401, AuthError), (403, AuthError), (402, OutOfCredits), (429, RateLimited)],
)
async def test_board_error_mapping(hass, aioclient_mock, status, exc) -> None:
    aioclient_mock.get(f"{URL}/board/weather", status=status)
    client = CloudClient(async_get_clientsession(hass), URL, token="t")
    with pytest.raises(exc):
        await client.board("weather", {"location": "Lisbon"})


async def test_board_returns_bytes_with_params(hass, aioclient_mock) -> None:
    aioclient_mock.get(f"{URL}/board/weather", content=b"JPEG", headers=IMG)
    client = CloudClient(async_get_clientsession(hass), URL, token="t")
    assert await client.board("weather", {"location": "Lisbon"}) == b"JPEG"
    assert str(aioclient_mock.mock_calls[0][1]).endswith("/board/weather?location=Lisbon")


async def test_proxied_image_asks_for_tv_size(hass, aioclient_mock) -> None:
    aioclient_mock.get(f"{URL}/collections/proxy/met:p:abc:def", content=b"IMG", headers=IMG)
    client = CloudClient(async_get_clientsession(hass), URL, token="t")
    assert await client.proxied_image("met:p:abc:def") == b"IMG"
    assert "width=3840" in str(aioclient_mock.mock_calls[0][1])
    assert "height=2160" in str(aioclient_mock.mock_calls[0][1])


async def test_status_ignores_404_but_raises_401(hass, aioclient_mock) -> None:
    client = CloudClient(async_get_clientsession(hass), URL, token="t")
    at = datetime(2026, 10, 11, 10, 2, tzinfo=UTC)
    aioclient_mock.post(f"{URL}/ha/agent/status", status=404)
    await client.agent_status("tv-1", False, "x" * 500, at)
    body = aioclient_mock.mock_calls[0][2]
    assert len(body["error"]) == 200 and body["at"] == "2026-10-11T10:02:00+00:00"

    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{URL}/ha/agent/status", status=401)
    with pytest.raises(AuthError):
        await client.agent_status("tv-1", True, None, at)


async def test_refresh(hass, aioclient_mock) -> None:
    aioclient_mock.post(
        f"{URL}/ha/agent/refresh",
        json={"token": "fc_ha_new", "expires_at": "2026-12-01T00:00:00Z"},
    )
    client = CloudClient(async_get_clientsession(hass), URL, token="fc_ha_old")
    info = await client.agent_refresh()
    assert info.token == "fc_ha_new"


def _client(hass, token="t"):
    return CloudClient(async_get_clientsession(hass), URL, token=token)


async def test_sync_without_etag_sends_no_if_none_match(hass, aioclient_mock) -> None:
    aioclient_mock.get(f"{URL}/ha/agent/sync", json={"specs": []})
    await _client(hass).agent_sync(None)
    assert "If-None-Match" not in aioclient_mock.mock_calls[0][3]


async def test_sync_non_json_200(hass, aioclient_mock) -> None:
    aioclient_mock.get(f"{URL}/ha/agent/sync", text="<html>")
    with pytest.raises(CloudError):
        await _client(hass).agent_sync(None)


async def test_sync_specs_string(hass, aioclient_mock) -> None:
    aioclient_mock.get(f"{URL}/ha/agent/sync", json={"specs": "abc"})
    with pytest.raises(CloudError):
        await _client(hass).agent_sync(None)


async def test_sync_non_dict_body(hass, aioclient_mock) -> None:
    aioclient_mock.get(f"{URL}/ha/agent/sync", json=[1])
    with pytest.raises(CloudError):
        await _client(hass).agent_sync(None)


async def test_claim_missing_token(hass, aioclient_mock) -> None:
    aioclient_mock.post(f"{URL}/ha/claim", json={"expires_at": "2026-11-10T00:00:00Z"})
    with pytest.raises(CloudError):
        await _client(hass).claim("ABCD2345")


async def test_claim_bad_expiry(hass, aioclient_mock) -> None:
    aioclient_mock.post(f"{URL}/ha/claim", json={"token": "x", "expires_at": "nope"})
    with pytest.raises(CloudError):
        await _client(hass).claim("ABCD2345")


async def test_naive_expiry_is_utc(hass, aioclient_mock) -> None:
    aioclient_mock.post(f"{URL}/ha/claim", json={"token": "x", "expires_at": "2026-11-10T00:00:00"})
    info = await _client(hass).claim("ABCD2345")
    assert info.expires_at == datetime(2026, 11, 10, tzinfo=UTC)


@pytest.mark.parametrize("err", [aiohttp.ClientPayloadError, TimeoutError])
async def test_board_body_read_failure(hass, aioclient_mock, err) -> None:
    aioclient_mock.get(f"{URL}/board/weather", exc=err)
    with pytest.raises(CloudError):
        await _client(hass).board("weather", {})


async def test_board_connection_error(hass, aioclient_mock) -> None:
    aioclient_mock.get(f"{URL}/board/weather", exc=aiohttp.ClientError)
    with pytest.raises(CloudError):
        await _client(hass).board("weather", {})


async def test_board_500_and_redirect(hass, aioclient_mock) -> None:
    aioclient_mock.get(f"{URL}/board/weather", status=500)
    with pytest.raises(CloudError):
        await _client(hass).board("weather", {})
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/board/weather", status=302)
    with pytest.raises(CloudError):
        await _client(hass).board("weather", {})


async def test_board_non_image(hass, aioclient_mock) -> None:
    aioclient_mock.get(f"{URL}/board/weather", text="<html>")
    with pytest.raises(CloudError):
        await _client(hass).board("weather", {})


async def test_proxied_image_non_image(hass, aioclient_mock) -> None:
    aioclient_mock.get(f"{URL}/collections/proxy/x", text="<html>")
    with pytest.raises(CloudError):
        await _client(hass).proxied_image("x")


async def test_board_type_is_quoted(hass, aioclient_mock) -> None:
    aioclient_mock.get(f"{URL}/board/a%2Fb", content=b"J", headers=IMG)
    await _client(hass).board("a/b", {})
    assert aioclient_mock.mock_calls[0][1].raw_path.endswith("/board/a%2Fb")


async def test_redirects_are_never_followed(hass, aioclient_mock) -> None:
    aioclient_mock.get(
        f"{URL}/board/weather", status=302, headers={"Location": "https://evil.test/x"}
    )
    aioclient_mock.get("https://evil.test/x", content=b"JPEG", headers=IMG)
    session = async_get_clientsession(hass)
    seen: list[object] = []
    real = session.request

    def spy(method, url, **kwargs):
        seen.append(kwargs.get("allow_redirects", True))
        return real(method, url, **kwargs)

    session.request = spy
    with pytest.raises(CloudError):
        await CloudClient(session, URL, "tok").board("weather", {})
    assert seen == [False]
    assert len(aioclient_mock.mock_calls) == 1


async def test_claim_non_string_token(hass, aioclient_mock) -> None:
    aioclient_mock.post(f"{URL}/ha/claim", json={"token": 7, "expires_at": "2026-11-10T00:00:00Z"})
    with pytest.raises(CloudError, match="malformed token"):
        await _client(hass).claim("ABCD2345")


# --- reply size caps (#17 item 2) ------------------------------------------------------


@pytest.fixture
def small_caps(monkeypatch):
    monkeypatch.setattr("custom_components.tvcanvas.api.MAX_IMAGE_BYTES", 1000)
    monkeypatch.setattr("custom_components.tvcanvas.api.MAX_JSON_BYTES", 100)


async def test_cloud_images_are_capped_like_direct_ones(hass, aioclient_mock, small_caps) -> None:
    aioclient_mock.get(f"{URL}/board/weather", content=b"x" * 1001, headers=IMG)
    aioclient_mock.get(f"{URL}/collections/proxy/met:1", content=b"x" * 1001, headers=IMG)
    with pytest.raises(CloudError, match="too large"):
        await _client(hass).board("weather", {})
    with pytest.raises(CloudError, match="too large"):
        await _client(hass).proxied_image("met:1")


async def test_cloud_images_up_to_the_cap_are_read_whole(hass, aioclient_mock, small_caps) -> None:
    aioclient_mock.get(f"{URL}/board/weather", content=b"x" * 1000, headers=IMG)
    assert await _client(hass).board("weather", {}) == b"x" * 1000


async def test_a_declared_length_over_the_cap_is_refused_unread(
    hass, aioclient_mock, small_caps
) -> None:
    headers = {**IMG, "Content-Length": "1001"}
    aioclient_mock.get(f"{URL}/board/weather", content=b"x" * 10, headers=headers)
    with pytest.raises(CloudError, match="too large"):
        await _client(hass).board("weather", {})


async def test_cloud_json_replies_are_capped(hass, aioclient_mock, small_caps) -> None:
    aioclient_mock.get(f"{URL}/ha/agent/sync", json={"specs": [{"pad": "x" * 100}]})
    with pytest.raises(CloudError, match="too large"):
        await _client(hass).agent_sync(None)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/ha/agent/sync", json={"specs": []})
    assert (await _client(hass).agent_sync(None)).specs == []


def test_the_json_cap_has_room_for_the_largest_sync_the_cloud_allows() -> None:
    """TVCanvas Cloud syncs at most 10 TVs, each spec ≤ 64 KB of compact JSON."""
    from custom_components.tvcanvas.api import MAX_JSON_BYTES

    assert MAX_JSON_BYTES >= 4 * 10 * 64 * 1024


# --- /ha/claim: one exception per outcome (the config flow's errors) -------------


@pytest.mark.parametrize(
    ("reply", "kind"),
    [
        ({"status": 400, "json": {"detail": "Invalid or expired pairing code"}}, InvalidCode),
        ({"status": 401, "json": {"detail": "Invalid or missing API key"}}, WrongServer),
        ({"status": 403}, WrongServer),
        ({"status": 404, "text": "Not Found"}, WrongServer),
        ({"status": 405}, WrongServer),
        ({"status": 301, "headers": {"Location": "https://x.test/"}}, WrongServer),
        ({"status": 308}, WrongServer),
        ({"status": 200, "text": "<html>a website</html>"}, WrongServer),
        ({"status": 200, "json": {"ok": True}}, WrongServer),
        ({"status": 200, "json": ["token"]}, WrongServer),
        ({"status": 429, "text": "Rate limit exceeded. Please slow down."}, RateLimited),
        ({"status": 500}, CloudServerError),
        ({"status": 502}, CloudServerError),
        ({"status": 503}, CloudServerError),
        ({"status": 402}, CloudError),
        ({"status": 422}, CloudError),
    ],
)
async def test_claim_outcomes(hass, aioclient_mock, reply, kind) -> None:
    aioclient_mock.post(f"{URL}/ha/claim", **reply)
    with pytest.raises(kind) as err:
        await _client(hass).claim("ABCD2345")
    assert type(err.value) is kind  # not a broader or narrower class
    assert "ABCD2345" not in str(err.value)


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError(),
        aiohttp.ClientConnectionError("connection refused"),
        aiohttp.ServerDisconnectedError(),
        aiohttp.ClientSSLError(CONN, OSError("certificate verify failed")),
        aiohttp.ClientConnectorDNSError(CONN, OSError("Name or service not known")),
    ],
)
async def test_claim_without_an_http_answer_is_unreachable(hass, aioclient_mock, exc) -> None:
    aioclient_mock.post(f"{URL}/ha/claim", exc=exc)
    with pytest.raises(CloudUnreachable):
        await _client(hass).claim("ABCD2345")
