from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType, section

from custom_components.tvcanvas.const import DEFAULT_CLOUD_URL, DOMAIN

from .common import entry

CLAIM = f"{DEFAULT_CLOUD_URL}/ha/claim"


async def test_user_flow_claims_code(hass, aioclient_mock) -> None:
    aioclient_mock.post(CLAIM, json={"token": "fc_ha_x", "expires_at": "2026-11-10T00:00:00Z"})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    with patch("custom_components.tvcanvas.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": "abcd-2345", "advanced": {}}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["token"] == "fc_ha_x"
    assert result["data"]["cloud_url"] == DEFAULT_CLOUD_URL
    assert aioclient_mock.mock_calls[0][2] == {"code": "ABCD2345"}


async def test_malformed_code_is_rejected_without_a_request(hass, aioclient_mock) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "abc", "advanced": {}}
    )
    assert result["errors"] == {"code": "code_format"} and aioclient_mock.call_count == 0


async def test_rejected_and_unreachable(hass, aioclient_mock) -> None:
    aioclient_mock.post(CLAIM, status=400)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "ABCD2345", "advanced": {}}
    )
    assert result["errors"] == {"code": "invalid_code"}
    aioclient_mock.clear_requests()
    aioclient_mock.post(CLAIM, exc=TimeoutError())
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "ABCD2345", "advanced": {}}
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_cloud_url_sits_in_a_collapsed_advanced_section(hass, caplog) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    advanced = result["data_schema"].schema
    key = next(k for k in advanced if str(k) == "advanced")
    assert isinstance(advanced[key], section)
    assert advanced[key].options["collapsed"] is True
    assert "show_advanced_options" not in caplog.text


async def test_advanced_section_allows_cloud_url(hass, aioclient_mock) -> None:
    aioclient_mock.post(
        "https://sandbox.tvcanvas.app/ha/claim",
        json={"token": "t", "expires_at": "2026-11-10T00:00:00Z"},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch("custom_components.tvcanvas.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"code": "ABCD2345", "advanced": {"cloud_url": "https://sandbox.tvcanvas.app/"}},
        )
    assert result["data"]["cloud_url"] == "https://sandbox.tvcanvas.app"


async def test_single_instance(hass) -> None:
    entry(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "single_instance_allowed"


async def test_reauth_updates_token(hass, aioclient_mock) -> None:
    e = entry(hass)
    aioclient_mock.post(
        "https://cloud.test/ha/claim",
        json={"token": "fc_ha_new", "expires_at": "2026-12-01T00:00:00Z"},
    )
    result = await e.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    with patch("custom_components.tvcanvas.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": "ABCD2345"}
        )
    assert result["reason"] == "reauth_successful" and e.data["token"] == "fc_ha_new"


async def _advanced(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_insecure_remote_cloud_url_rejected_without_request(hass, aioclient_mock) -> None:
    result = await _advanced(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"code": "ABCD2345", "advanced": {"cloud_url": "http://cloud.example.com"}},
    )
    assert result["errors"] == {"base": "invalid_url"} and aioclient_mock.call_count == 0


async def test_cloud_url_is_trimmed(hass, aioclient_mock) -> None:
    aioclient_mock.post(
        "https://sandbox.tvcanvas.app/ha/claim",
        json={"token": "t", "expires_at": "2026-11-10T00:00:00Z"},
    )
    result = await _advanced(hass)
    with patch("custom_components.tvcanvas.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"code": "ABCD2345", "advanced": {"cloud_url": " https://sandbox.tvcanvas.app/ "}},
        )
    assert result["data"]["cloud_url"] == "https://sandbox.tvcanvas.app"


async def test_http_lan_cloud_url_is_allowed(hass, aioclient_mock) -> None:
    aioclient_mock.post(
        "http://192.168.0.10:8765/ha/claim",
        json={"token": "t", "expires_at": "2026-11-10T00:00:00Z"},
    )
    result = await _advanced(hass)
    with patch("custom_components.tvcanvas.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"code": "ABCD2345", "advanced": {"cloud_url": "http://192.168.0.10:8765"}},
        )
    assert result["data"]["cloud_url"] == "http://192.168.0.10:8765"
    assert aioclient_mock.call_count == 1


# --- 2026-11-09 review ------------------------------------------------------


async def test_unexpected_claim_error_shows_unknown_not_a_crash(hass, caplog) -> None:
    result = await _advanced(hass)
    with patch(
        "custom_components.tvcanvas.config_flow.CloudClient.claim",
        side_effect=RuntimeError("boom"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": "ABCD2345", "advanced": {}}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}
    assert "boom" in caplog.text


@pytest.mark.parametrize("url", ["https://[::1", "https://", "ftp://cloud.example.com"])
async def test_unparseable_or_hostless_cloud_url_is_rejected(hass, aioclient_mock, url) -> None:
    result = await _advanced(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "ABCD2345", "advanced": {"cloud_url": url}}
    )
    assert result["errors"] == {"base": "invalid_url"} and aioclient_mock.call_count == 0


async def test_reauth_with_a_wrong_code_asks_again(hass, aioclient_mock) -> None:
    e = entry(hass)
    aioclient_mock.post("https://cloud.test/ha/claim", status=400)
    result = await e.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "ABCD2345"})
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"code": "invalid_code"} and e.data["token"] == "fc_ha_t"


# --- one-step setup: the 12-character code (docs/lan-api.md §3) ------------------

SUFFIX = "P4NB"  # never in a token, a key hash (lowercase hex) or a config entry


async def _set_up_with(hass, aioclient_mock, code: str):
    far = "2099-01-01T00:00:00Z"  # no token refresh during setup
    aioclient_mock.post(CLAIM, json={"token": "fc_ha_x", "expires_at": far})
    aioclient_mock.get(f"{DEFAULT_CLOUD_URL}/ha/agent/sync", json={"specs": []})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": code, "advanced": {}}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    [e] = hass.config_entries.async_entries(DOMAIN)
    return e


async def _flush_config_entries(hass) -> None:
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()


async def test_12_characters_claim_8_and_open_the_last_4_for_the_phone(
    hass, aioclient_mock, hass_storage, caplog
) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    e = await _set_up_with(hass, aioclient_mock, "k7qm-2xra-p4nb")
    claims = [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/ha/claim")]
    assert [c[2] for c in claims] == [{"code": "K7QM2XRA"}]  # the cloud never sees the suffix
    pairing = e.runtime_data.pairing
    assert pairing.pairing_open and pairing._code == SUFFIX
    # In memory only: not in the entry, not in .storage, not in the log.
    await _flush_config_entries(hass)
    assert "core.config_entries" in hass_storage
    for text in (repr(e.data), repr(e.options), repr(hass_storage), caplog.text):
        assert SUFFIX not in text
    assert DOMAIN in hass.data and "pending_lan_setup_code" not in hass.data[DOMAIN]
    await hass.config_entries.async_unload(e.entry_id)


async def test_8_characters_still_connect_and_open_no_lan_code(hass, aioclient_mock) -> None:
    e = await _set_up_with(hass, aioclient_mock, "K7QM-2XRA")
    assert aioclient_mock.mock_calls[0][2] == {"code": "K7QM2XRA"}
    assert not e.runtime_data.pairing.pairing_open
    await hass.config_entries.async_unload(e.entry_id)


@pytest.mark.parametrize(
    "code", ["K7QM2XR", "K7QM2XRAP", "K7QM2XRAP4", "K7QM2XRAP4N", "K7QM2XRAP4NBZ"]
)
async def test_other_lengths_are_rejected_without_a_request(hass, aioclient_mock, code) -> None:
    result = await _advanced(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": code, "advanced": {}}
    )
    assert result["errors"] == {"code": "code_format"} and aioclient_mock.call_count == 0


async def test_a_refused_claim_opens_no_lan_code(hass, aioclient_mock) -> None:
    aioclient_mock.post(CLAIM, status=400)
    result = await _advanced(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "K7QM-2XRA-P4NB", "advanced": {}}
    )
    assert result["errors"] == {"code": "invalid_code"}
    assert "pending_lan_setup_code" not in hass.data.get(DOMAIN, {})


async def test_reauth_with_12_characters_opens_the_code_on_the_reloaded_entry(
    hass, aioclient_mock, hass_storage
) -> None:
    e = entry(hass)
    aioclient_mock.get("https://cloud.test/ha/agent/sync", json={"specs": []})
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    aioclient_mock.post(
        "https://cloud.test/ha/claim",
        json={"token": "fc_ha_new", "expires_at": "2099-01-01T00:00:00Z"},
    )
    result = await e.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "K7QM-2XRA-P4NB"}
    )
    await hass.async_block_till_done()  # the reload
    assert result["reason"] == "reauth_successful" and e.data["token"] == "fc_ha_new"
    claims = [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/ha/claim")]
    assert [c[2] for c in claims] == [{"code": "K7QM2XRA"}]
    pairing = e.runtime_data.pairing
    assert pairing.pairing_open and pairing._code == SUFFIX
    await _flush_config_entries(hass)
    for text in (repr(e.data), repr(e.options), repr(hass_storage)):
        assert SUFFIX not in text
    await hass.config_entries.async_unload(e.entry_id)


async def test_reauth_with_8_characters_opens_no_lan_code(hass, aioclient_mock) -> None:
    e = entry(hass)
    aioclient_mock.get("https://cloud.test/ha/agent/sync", json={"specs": []})
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    aioclient_mock.post(
        "https://cloud.test/ha/claim",
        json={"token": "fc_ha_new", "expires_at": "2099-01-01T00:00:00Z"},
    )
    result = await e.start_reauth_flow(hass)
    await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "K7QM2XRA"})
    await hass.async_block_till_done()
    assert not e.runtime_data.pairing.pairing_open
    await hass.config_entries.async_unload(e.entry_id)


async def test_a_stashed_setup_code_expires_10_minutes_after_the_claim(hass, monkeypatch) -> None:
    from types import SimpleNamespace

    from custom_components.tvcanvas import lan_pairing

    monkeypatch.setattr(lan_pairing, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    lan_pairing.stash_setup_code(hass, SUFFIX)
    monkeypatch.setattr(lan_pairing, "time", SimpleNamespace(monotonic=lambda: 1240.0))
    assert lan_pairing.take_setup_code(hass) == (SUFFIX, 360.0)  # the rest of its 10 minutes
    assert lan_pairing.take_setup_code(hass) is None  # taken once
    lan_pairing.stash_setup_code(hass, SUFFIX)  # stashed at 1240
    monkeypatch.setattr(lan_pairing, "time", SimpleNamespace(monotonic=lambda: 1840.0))
    assert lan_pairing.take_setup_code(hass) is None


async def test_the_code_field_says_to_type_the_whole_code() -> None:
    import json
    from pathlib import Path

    folder = Path(__file__).parents[1] / "custom_components" / "tvcanvas"
    for path in [folder / "strings.json", *(folder / "translations").glob("*.json")]:
        steps = json.loads(path.read_text())["config"]["step"]
        for step in ("user", "reauth_confirm"):
            assert steps[step]["data_description"]["code"], (path.name, step)


async def test_removing_the_entry_drops_an_unused_setup_code(hass, aioclient_mock) -> None:
    from custom_components.tvcanvas.lan_pairing import stash_setup_code

    e = entry(hass)
    aioclient_mock.get("https://cloud.test/ha/agent/sync", json={"specs": []})
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    stash_setup_code(hass, SUFFIX)
    await hass.config_entries.async_remove(e.entry_id)
    await hass.async_block_till_done()
    assert "pending_lan_setup_code" not in hass.data.get(DOMAIN, {})


# --- what went wrong, said clearly (fix/clearer-claim-errors) ---------------------

# What aiohttp's connector errors read when they print themselves.
CONN = SimpleNamespace(host="api.tvcanvas.app", port=443, ssl=True, is_ssl=True)
STRINGS = Path(__file__).parents[1] / "custom_components" / "tvcanvas" / "strings.json"

# (mock reply, the error shown). The cloud's 400 is about the code, so it sits on the
# code field; everything else is about the server or the network.
CLAIM_OUTCOMES = [
    ({"exc": TimeoutError()}, {"base": "cannot_connect"}),
    ({"exc": aiohttp.ClientConnectionError("refused")}, {"base": "cannot_connect"}),
    (
        {"exc": aiohttp.ClientConnectorDNSError(CONN, OSError("Name or service not known"))},
        {"base": "cannot_connect"},
    ),
    (
        {"exc": aiohttp.ClientSSLError(CONN, OSError("certificate verify failed"))},
        {"base": "cannot_connect"},
    ),
    # Production without the HA endpoints deployed answers like this (2026-11-11).
    ({"status": 401, "json": {"detail": "Invalid or missing API key"}}, {"base": "wrong_server"}),
    ({"status": 403}, {"base": "wrong_server"}),
    ({"status": 404, "json": {"detail": "Not Found"}}, {"base": "wrong_server"}),
    ({"status": 405}, {"base": "wrong_server"}),
    ({"status": 302, "headers": {"Location": "https://tvcanvas.app/"}}, {"base": "wrong_server"}),
    ({"status": 200, "text": "<!doctype html><title>TVCanvas</title>"}, {"base": "wrong_server"}),
    (
        {"status": 400, "json": {"detail": "Invalid or expired pairing code"}},
        {"code": "invalid_code"},
    ),
    (
        {"status": 429, "text": "Rate limit exceeded. Please slow down."},
        {"base": "too_many_attempts"},
    ),
    ({"status": 500}, {"base": "server_error"}),
    ({"status": 502}, {"base": "server_error"}),
    ({"status": 503}, {"base": "server_error"}),
    ({"status": 422}, {"base": "unexpected_answer"}),
    ({"status": 418}, {"base": "unexpected_answer"}),
]


@pytest.mark.parametrize(("reply", "errors"), CLAIM_OUTCOMES)
async def test_user_step_says_what_went_wrong(hass, aioclient_mock, caplog, reply, errors) -> None:
    aioclient_mock.post(CLAIM, **reply)
    result = await _advanced(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "K7QM-2XRA-P4NB", "advanced": {}}
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    assert result["errors"] == errors
    assert aioclient_mock.call_count == 1
    assert not hass.config_entries.async_entries(DOMAIN)
    # The log says why (for "Check the log"), never the code or its LAN part.
    for secret in ("K7QM", "2XRA", "P4NB"):
        assert secret not in caplog.text
    # The failed claim stashed no LAN setup code for a later setup to open.
    assert "pending_lan_setup_code" not in hass.data.get(DOMAIN, {})


@pytest.mark.parametrize(("reply", "errors"), CLAIM_OUTCOMES)
async def test_reauth_step_says_what_went_wrong(hass, aioclient_mock, reply, errors) -> None:
    e = entry(hass)
    aioclient_mock.post("https://cloud.test/ha/claim", **reply)
    result = await e.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "K7QM-2XRA-P4NB"}
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "reauth_confirm"
    assert result["errors"] == errors
    assert e.data["token"] == "fc_ha_t"


async def test_the_log_names_the_reason_and_status(hass, aioclient_mock, caplog) -> None:
    import logging

    caplog.set_level(logging.INFO)
    aioclient_mock.post(CLAIM, status=401, json={"detail": "Invalid or missing API key"})
    result = await _advanced(hass)
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "K7QM-2XRA-P4NB", "advanced": {}}
    )
    assert "401" in caplog.text and DEFAULT_CLOUD_URL in caplog.text
    assert "K7QM" not in caplog.text and "P4NB" not in caplog.text


async def test_after_an_error_the_next_try_can_succeed(hass, aioclient_mock) -> None:
    aioclient_mock.post(CLAIM, status=503)
    result = await _advanced(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "ABCD2345", "advanced": {}}
    )
    assert result["errors"] == {"base": "server_error"}
    aioclient_mock.clear_requests()
    aioclient_mock.post(CLAIM, json={"token": "fc_ha_x", "expires_at": "2099-01-01T00:00:00Z"})
    with patch("custom_components.tvcanvas.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": "ABCD2345", "advanced": {}}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    "code",
    [
        "abc",  # too short
        "ABCD-234",  # 7
        "ABCD-2345-P4N",  # 11
        "ABCD-2345-P4NBX",  # 13
        "ABCU-2345-P4NB",  # U isn't a Crockford character
        "ABCD-2345-P4N!",
        "ABCD_2345_P4NB",
        "ÄBCD-2345-P4NB",
        "",
    ],
)
async def test_a_code_of_the_wrong_shape_asks_for_the_12_characters(
    hass, aioclient_mock, code
) -> None:
    result = await _advanced(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": code, "advanced": {}}
    )
    assert result["errors"] == {"code": "code_format"} and aioclient_mock.call_count == 0
    e = entry(hass)
    result = await e.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": code})
    assert result["errors"] == {"code": "code_format"} and aioclient_mock.call_count == 0


@pytest.mark.parametrize(
    "url",
    [
        "https://api.tvcanvas.app/ha",
        "https://api.tvcanvas.app/ha/claim",
        "https://api.tvcanvas.app/api/v1/",
        "https://api.tvcanvas.app?env=sandbox",
        "https://api.tvcanvas.app/#top",
        "https://user:secret@api.tvcanvas.app",
        "http://192.168.1.10:8000/ha",
    ],
)
async def test_a_cloud_url_with_a_path_asks_for_just_the_address(
    hass, aioclient_mock, caplog, url
) -> None:
    result = await _advanced(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "ABCD2345", "advanced": {"cloud_url": url}}
    )
    assert result["errors"] == {"base": "url_not_base"} and aioclient_mock.call_count == 0
    assert "secret" not in caplog.text


@pytest.mark.parametrize(
    "url",
    [
        "api.tvcanvas.app",  # no scheme
        "http://api.tvcanvas.app",  # http to a public host
        "https://api.tvcanvas.app:99999",  # no such port
        "https://api.tvcanvas.app:port",
        "tvcanvas",
    ],
)
async def test_a_cloud_url_that_isnt_https_asks_for_https(hass, aioclient_mock, url) -> None:
    result = await _advanced(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "ABCD2345", "advanced": {"cloud_url": url}}
    )
    assert result["errors"] == {"base": "invalid_url"} and aioclient_mock.call_count == 0


@pytest.mark.parametrize(
    "url",
    ["https://sandbox.tvcanvas.app/", "https://api.tvcanvas.app:443", "http://localhost:8000/"],
)
async def test_a_plain_server_address_is_accepted(hass, aioclient_mock, url) -> None:
    base = url.rstrip("/")
    aioclient_mock.post(
        f"{base}/ha/claim", json={"token": "t", "expires_at": "2099-01-01T00:00:00Z"}
    )
    result = await _advanced(hass)
    with patch("custom_components.tvcanvas.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": "ABCD2345", "advanced": {"cloud_url": url}}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["cloud_url"] == base


def test_the_messages_tell_the_user_what_to_do() -> None:
    errors = json.loads(STRINGS.read_text())["config"]["error"]
    # hassfest refuses URLs in strings.json; the form fills the default server in.
    assert "{default_url}" in errors["wrong_server"]
    assert not [k for k, v in errors.items() if "://api." in v]
    assert "Home Assistant codes" in errors["wrong_server"]
    assert "internet" in errors["cannot_connect"] and "URL" in errors["cannot_connect"]
    assert "expire" in errors["invalid_code"] and "new code" in errors["invalid_code"]
    assert "12 characters" in errors["code_format"]
    assert "Wait" in errors["too_many_attempts"]
    assert "Try again" in errors["server_error"]
    assert "without a path" in errors["url_not_base"]
    assert "https://" in errors["invalid_url"]
    # One message per cause: none of them repeats another.
    assert len(set(errors.values())) == len(errors)


async def test_the_forms_fill_in_the_default_server_for_the_errors(hass) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["description_placeholders"] == {"default_url": DEFAULT_CLOUD_URL}
