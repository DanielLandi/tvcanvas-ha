"""The phone → HA photo API (docs/lan-api.md §3–4), pairing by setup code and cleanup."""

from __future__ import annotations

import hashlib
import io
import json
from unittest.mock import patch

import pytest
from homeassistant.components import persistent_notification
from homeassistant.components.http import KEY_HASS
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import instance_id
from homeassistant.setup import async_setup_component
from PIL import Image

from custom_components.tvcanvas.const import DOMAIN
from custom_components.tvcanvas.diagnostics import async_get_config_entry_diagnostics
from custom_components.tvcanvas.photos import photos_dir

from .common import URL, entry

KEY = "X-TVCanvas-LAN-Key"


def image_bytes(color=(10, 20, 30), fmt="JPEG", size=(8, 8)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, fmt)
    return out.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _lan(hass, aioclient_mock, hass_client_no_auth, http_config=None):
    if http_config is not None:
        assert await async_setup_component(hass, "http", {"http": http_config})
    aioclient_mock.get(f"{URL}/ha/agent/sync", json={"specs": []})
    e = entry(hass)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    pairing = e.runtime_data.pairing
    pairing._clock = clock = Clock()
    client = await hass_client_no_auth()

    class Lan:
        pass

    lan = Lan()
    lan.entry, lan.pairing, lan.clock, lan.client = e, pairing, clock, client
    lan.photos = e.runtime_data.photos

    fresh = iter(range(1, 10_000))

    async def open_code():
        """Open a fresh live code, as claiming a 12-character code in the config flow does."""
        code = f"{next(fresh):04d}"
        pairing.open_setup_code(code)
        return code

    async def pair(name="Alex's iPhone"):
        code = await open_code()
        resp = await client.post("/api/tvcanvas/lan/pair", json={"code": code, "device_name": name})
        assert resp.status == 200
        return (await resp.json())["key"]

    lan.open_code, lan.pair = open_code, pair
    return lan


@pytest.fixture
async def lan(hass, aioclient_mock, hass_client_no_auth):
    lan = await _lan(hass, aioclient_mock, hass_client_no_auth)
    yield lan
    if hass.config_entries.async_get_entry(lan.entry.entry_id) and lan.entry.state.recoverable:
        await hass.config_entries.async_unload(lan.entry.entry_id)


@pytest.fixture
async def proxied_lan(hass, aioclient_mock, hass_client_no_auth):
    """HA behind a reverse proxy on 127.0.0.1 that it trusts for X-Forwarded-For."""
    config = {"use_x_forwarded_for": True, "trusted_proxies": ["127.0.0.1"]}
    lan = await _lan(hass, aioclient_mock, hass_client_no_auth, config)
    yield lan
    await hass.config_entries.async_unload(lan.entry.entry_id)


# --- info -----------------------------------------------------------------------


async def test_info_without_a_key_says_only_api_and_whether_pairing_is_open(hass, lan) -> None:
    resp = await lan.client.get("/api/tvcanvas/lan/info")
    assert resp.status == 200
    assert await resp.json() == {"api": 1, "pairing_open": False}
    bad = await lan.client.get("/api/tvcanvas/lan/info", headers={KEY: "wrong"})
    assert await bad.json() == {"api": 1, "pairing_open": False}
    await lan.open_code()
    assert (await (await lan.client.get("/api/tvcanvas/lan/info")).json())["pairing_open"]
    lan.clock.now += 601
    assert not (await (await lan.client.get("/api/tvcanvas/lan/info")).json())["pairing_open"]


async def test_info_with_a_key_also_names_this_ha(hass, lan) -> None:
    key = await lan.pair()
    resp = await lan.client.get("/api/tvcanvas/lan/info", headers={KEY: key})
    assert await resp.json() == {
        "api": 1,
        "pairing_open": False,
        "name": hass.config.location_name,
        "instance_id": await instance_id.async_get(hass),
    }


# --- the code -------------------------------------------------------------------


async def test_no_pairing_button_and_no_notification(hass, lan) -> None:
    """The 12-character code pairs the phone; HA has no button or code of its own (#21)."""
    registry = er.async_get(hass)
    ours = er.async_entries_for_config_entry(registry, lan.entry.entry_id)
    assert ours and not [x for x in ours if x.domain == "button"]
    assert registry.async_get_entity_id("button", DOMAIN, f"{lan.entry.entry_id}_lan_pair") is None
    assert not hass.states.async_entity_ids("button")
    code = await lan.open_code()
    await lan.client.post("/api/tvcanvas/lan/pair", json={"code": code, "device_name": "x"})
    assert not persistent_notification._async_get_or_create_notifications(hass)


async def test_upgrading_removes_the_retired_pairing_button(
    hass, aioclient_mock, hass_storage
) -> None:
    """An install from before #21 has the button in its entity registry; setup drops it
    and keeps everything else, including what the user renamed."""
    aioclient_mock.get(f"{URL}/ha/agent/sync", json={"specs": []})
    e = entry(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "button",
        DOMAIN,
        f"{e.entry_id}_lan_pair",
        config_entry=e,
        translation_key="pair_phone",
        original_name="Pair a phone for photos",
    )
    phones = registry.async_get_or_create(
        "sensor", DOMAIN, f"{e.entry_id}_lan_phones", config_entry=e
    )
    registry.async_update_entity(phones.entity_id, name="My phones")
    others = registry.async_get_or_create("button", "demo", f"{e.entry_id}_lan_pair")
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(old.entity_id) is None
    assert hass.states.get(old.entity_id) is None
    assert registry.async_get(phones.entity_id).name == "My phones"
    assert hass.states.get(phones.entity_id).state == "0"
    assert registry.async_get(others.entity_id) is not None  # not ours: untouched
    # A restart after that finds nothing to remove and sets up as usual.
    await hass.config_entries.async_reload(e.entry_id)
    await hass.async_block_till_done()
    assert e.state.name == "LOADED"
    assert not [
        x for x in er.async_entries_for_config_entry(registry, e.entry_id) if x.domain == "button"
    ]
    await hass.config_entries.async_unload(e.entry_id)


async def test_pairing_returns_a_key_once(hass, lan, hass_storage) -> None:
    code = await lan.open_code()
    resp = await lan.client.post(
        "/api/tvcanvas/lan/pair", json={"code": code, "device_name": "Alex's iPhone", "x": 1}
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["instance_id"] == await instance_id.async_get(hass)
    assert body["name"] == hass.config.location_name
    key = body["key"]
    assert len(key) == 43 and "=" not in key  # 32 bytes, base64url, no padding
    again = await lan.client.post("/api/tvcanvas/lan/pair", json={"code": code})
    assert again.status == 403  # single use
    stored = hass_storage[f"{DOMAIN}.{lan.entry.entry_id}.lan_keys"]["data"]
    assert key not in repr(stored)
    assert stored["phones"][0]["key_hash"] == hashlib.sha256(key.encode()).hexdigest()
    assert stored["phones"][0]["device_name"] == "Alex's iPhone"


async def test_expired_code_is_refused(lan) -> None:
    code = await lan.open_code()
    lan.clock.now += 601
    resp = await lan.client.post("/api/tvcanvas/lan/pair", json={"code": code})
    assert resp.status == 403


async def wrong_codes(lan, code, n, ip=None):
    wrong = f"{(int(code) + 1) % 10_000:04d}"
    headers = {"X-Forwarded-For": ip} if ip else {}
    statuses = []
    for _ in range(n):
        resp = await lan.client.post(
            "/api/tvcanvas/lan/pair", json={"code": wrong}, headers=headers
        )
        statuses.append(resp.status)
    return statuses


async def test_five_wrong_codes_kill_the_code_and_lock_that_address(hass, lan) -> None:
    code = await lan.open_code()
    assert await wrong_codes(lan, code, 5) == [403] * 5
    assert not lan.pairing.pairing_open  # the code is dead
    resp = await lan.client.post("/api/tvcanvas/lan/pair", json={"code": code})
    assert resp.status == 429
    assert await resp.json() == {"error": "locked", "retry_after": 300}
    lan.clock.now += 120
    resp = await lan.client.post("/api/tvcanvas/lan/pair", json={"code": code})
    assert resp.status == 429 and (await resp.json())["retry_after"] == 180
    lan.clock.now += 181
    resp = await lan.client.post("/api/tvcanvas/lan/pair", json={"code": code})
    assert resp.status == 403  # unlocked, but that code is gone


async def test_lockout_is_per_address(proxied_lan) -> None:
    lan = proxied_lan
    code = await lan.open_code()
    stranger, phone = "192.168.1.66", "192.168.1.20"
    assert await wrong_codes(lan, code, 5, stranger) == [403] * 5
    assert await wrong_codes(lan, code, 1, stranger) == [429]
    # The stranger killed that code; a new one (connecting again) still locks them out
    # but not the owner's phone.
    fresh = await lan.open_code()
    resp = await lan.client.post(
        "/api/tvcanvas/lan/pair", json={"code": fresh}, headers={"X-Forwarded-For": stranger}
    )
    assert resp.status == 429
    assert await wrong_codes(lan, fresh, 2, phone) == [403, 403]
    resp = await lan.client.post(
        "/api/tvcanvas/lan/pair", json={"code": fresh}, headers={"X-Forwarded-For": phone}
    )
    assert resp.status == 200


async def test_one_address_is_locked_while_another_still_guesses(proxied_lan) -> None:
    lan = proxied_lan
    code = await lan.open_code()
    # Two addresses, 2 + 2 wrong: the code survives (4 < 5), nobody is locked.
    await wrong_codes(lan, code, 2, "192.168.1.66")
    await wrong_codes(lan, code, 2, "192.168.1.67")
    resp = await lan.client.post(
        "/api/tvcanvas/lan/pair", json={"code": code}, headers={"X-Forwarded-For": "192.168.1.68"}
    )
    assert resp.status == 200


async def test_attempts_without_a_live_code_are_not_counted(lan) -> None:
    for _ in range(10):
        resp = await lan.client.post("/api/tvcanvas/lan/pair", json={"code": "000000"})
        assert resp.status == 403
    code = await lan.open_code()
    lan.clock.now += 601  # expired: not counted either
    for _ in range(10):
        resp = await lan.client.post("/api/tvcanvas/lan/pair", json={"code": code})
        assert resp.status == 403
    fresh = await lan.open_code()
    assert (await lan.client.post("/api/tvcanvas/lan/pair", json={"code": fresh})).status == 200


@pytest.mark.parametrize("code", [123456, None, "", "12345", "1234567", ["123456"]])
async def test_odd_codes_are_bad_codes(lan, code) -> None:
    await lan.open_code()
    resp = await lan.client.post("/api/tvcanvas/lan/pair", json={"code": code})
    assert resp.status == 403


async def test_pair_body_limits(lan) -> None:
    code = await lan.open_code()
    big = {"code": code, "device_name": "x" * 5000}
    assert (await lan.client.post("/api/tvcanvas/lan/pair", json=big)).status == 413

    async def chunks():
        yield b'{"code": "' + code.encode() + b'", "device_name": "'
        yield b"x" * 5000
        yield b'"}'

    assert (await lan.client.post("/api/tvcanvas/lan/pair", data=chunks())).status == 413
    resp = await lan.client.post("/api/tvcanvas/lan/pair", data=b"not json")
    assert resp.status == 400
    assert (await lan.client.post("/api/tvcanvas/lan/pair", json=[code])).status == 400
    # None of that used up the code.
    assert (await lan.client.post("/api/tvcanvas/lan/pair", json={"code": code})).status == 200


@pytest.mark.parametrize(
    "content_type",
    [
        "text/plain",
        "text/plain; charset=utf-8",
        "application/x-www-form-urlencoded",
        "multipart/form-data; boundary=x",
        "application/octet-stream",
    ],
)
async def test_pair_needs_a_json_body_type_and_others_never_count_as_guesses(
    lan, content_type
) -> None:
    """#17 item 1: a web page open at home can POST these cross-site without a CORS
    preflight. Six wrong codes that way must not burn the owner's live code."""
    code = await lan.open_code()
    for _ in range(6):
        resp = await lan.client.post(
            "/api/tvcanvas/lan/pair",
            data=b'{"code": "000000"}',
            headers={"Content-Type": content_type},
        )
        assert resp.status == 400 and await resp.json() == {"error": "bad_request"}
    # A right code sent that way doesn't pair either.
    resp = await lan.client.post(
        "/api/tvcanvas/lan/pair",
        data=json.dumps({"code": code}).encode(),
        headers={"Content-Type": content_type},
    )
    assert resp.status == 400
    assert lan.pairing.pairing_open
    assert (await lan.client.post("/api/tvcanvas/lan/pair", json={"code": code})).status == 200


async def test_pair_accepts_json_with_a_charset(lan) -> None:
    code = await lan.open_code()
    resp = await lan.client.post(
        "/api/tvcanvas/lan/pair",
        data=json.dumps({"code": code}).encode(),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert resp.status == 200


async def test_device_names_are_cleaned(lan) -> None:
    await lan.pair("  Alex\x00's\n iPhone  " + "y" * 100)
    await lan.pair(None)
    names = [p.device_name for p in lan.pairing.phones]
    assert names[0].startswith("Alex's iPhone") and len(names[0]) == 64
    assert names[1] == "Phone"


async def test_a_sixth_phone_evicts_the_oldest(lan) -> None:
    keys = [await lan.pair(f"Phone {i}") for i in range(6)]
    assert [p.device_name for p in lan.pairing.phones] == [f"Phone {i}" for i in range(1, 6)]
    have = "/api/tvcanvas/lan/have"
    assert (await lan.client.get(have, headers={KEY: keys[0]})).status == 401
    assert (await lan.client.get(have, headers={KEY: keys[5]})).status == 200


async def test_keys_are_compared_in_constant_time_against_every_phone(lan) -> None:
    keys = [await lan.pair(f"Phone {i}") for i in range(3)]
    with patch(
        "custom_components.tvcanvas.lan_pairing.hmac.compare_digest", return_value=False
    ) as cmp:
        lan.pairing.authenticate(keys[0])
    assert cmp.call_count == 3  # no early exit on the first match


# --- auth -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/tvcanvas/lan/have"),
        ("put", f"/api/tvcanvas/lan/image/{'a' * 64}"),
        ("delete", "/api/tvcanvas/lan/pair"),
    ],
)
@pytest.mark.parametrize("headers", [{}, {KEY: ""}, {KEY: "wrong"}, {"Authorization": "x"}])
async def test_missing_or_unknown_key_is_401(lan, method, path, headers) -> None:
    await lan.pair()
    resp = await getattr(lan.client, method)(path, headers=headers, data=b"x")
    assert resp.status == 401 and await resp.json() == {"error": "unauthorized"}


async def test_delete_forgets_only_the_calling_key(lan) -> None:
    k1, k2 = await lan.pair("one"), await lan.pair("two")
    resp = await lan.client.delete("/api/tvcanvas/lan/pair", headers={KEY: k1})
    assert resp.status == 204
    assert (await lan.client.get("/api/tvcanvas/lan/have", headers={KEY: k1})).status == 401
    assert (await lan.client.get("/api/tvcanvas/lan/have", headers={KEY: k2})).status == 200
    assert [p.device_name for p in lan.pairing.phones] == ["two"]


# --- have / image -------------------------------------------------------------------


async def test_have_and_put(hass, lan) -> None:
    key = await lan.pair()
    h = {KEY: key}
    body = await (await lan.client.get("/api/tvcanvas/lan/have", headers=h)).json()
    assert body == {"sha256": [], "bytes_used": 0, "bytes_cap": 1073741824, "files_cap": 400}
    jpeg, png = image_bytes(), image_bytes((1, 2, 3), "PNG")
    for data in (jpeg, png):
        resp = await lan.client.put(
            f"/api/tvcanvas/lan/image/{sha(data)}",
            data=data,
            headers={**h, "Content-Type": "image/jpeg"},
        )
        assert resp.status == 201
    again = await lan.client.put(f"/api/tvcanvas/lan/image/{sha(jpeg)}", data=jpeg, headers=h)
    assert again.status == 200
    body = await (await lan.client.get("/api/tvcanvas/lan/have", headers=h)).json()
    assert body["sha256"] == sorted([sha(jpeg), sha(png)])
    assert body["bytes_used"] == len(jpeg) + len(png)
    assert (photos_dir(hass) / f"{sha(jpeg)}.jpg").read_bytes() == jpeg


async def test_hash_mismatch_is_422(lan) -> None:
    key = await lan.pair()
    data = image_bytes()
    resp = await lan.client.put(
        f"/api/tvcanvas/lan/image/{'0' * 64}", data=data, headers={KEY: key}
    )
    assert resp.status == 422 and await resp.json() == {"error": "hash_mismatch"}
    assert lan.photos.files == 0


@pytest.mark.parametrize(
    "data",
    [
        b"definitely not an image",
        image_bytes(fmt="GIF"),
        image_bytes(fmt="BMP"),
        image_bytes()[:-200],  # truncated JPEG
    ],
)
async def test_undecodable_or_other_formats_are_415(lan, data) -> None:
    key = await lan.pair()
    resp = await lan.client.put(
        f"/api/tvcanvas/lan/image/{sha(data)}", data=data, headers={KEY: key}
    )
    assert resp.status == 415 and lan.photos.files == 0


async def test_images_over_the_pixel_cap_are_415(lan, monkeypatch) -> None:
    monkeypatch.setattr("custom_components.tvcanvas.imaging.MAX_UPLOAD_PIXELS", 63)
    key = await lan.pair()
    data = image_bytes(size=(8, 8))
    resp = await lan.client.put(
        f"/api/tvcanvas/lan/image/{sha(data)}", data=data, headers={KEY: key}
    )
    assert resp.status == 415


async def test_oversized_body_is_413_declared_or_streamed(lan, monkeypatch) -> None:
    monkeypatch.setattr("custom_components.tvcanvas.lan_view.LAN_IMAGE_BODY_MAX", 1000)
    key = await lan.pair()
    data = image_bytes(size=(200, 200))
    assert len(data) > 1000
    url = f"/api/tvcanvas/lan/image/{sha(data)}"
    assert (await lan.client.put(url, data=data, headers={KEY: key})).status == 413

    async def chunks():
        for i in range(0, len(data), 100):
            yield data[i : i + 100]

    assert (await lan.client.put(url, data=chunks(), headers={KEY: key})).status == 413
    assert lan.photos.files == 0


async def test_full_store_is_507(lan, monkeypatch) -> None:
    key = await lan.pair()
    a, b = image_bytes((1, 1, 1)), image_bytes((2, 2, 2))
    await lan.client.put(f"/api/tvcanvas/lan/image/{sha(a)}", data=a, headers={KEY: key})
    monkeypatch.setattr("custom_components.tvcanvas.photos.PHOTOS_FILES_CAP", 1)
    resp = await lan.client.put(f"/api/tvcanvas/lan/image/{sha(b)}", data=b, headers={KEY: key})
    assert resp.status == 507 and await resp.json() == {"error": "full"}
    # One it already has is still fine.
    assert (
        await lan.client.put(f"/api/tvcanvas/lan/image/{sha(a)}", data=a, headers={KEY: key})
    ).status == 200
    monkeypatch.setattr("custom_components.tvcanvas.photos.PHOTOS_FILES_CAP", 400)
    monkeypatch.setattr("custom_components.tvcanvas.photos.PHOTOS_BYTES_CAP", len(a) + 10)
    resp = await lan.client.put(f"/api/tvcanvas/lan/image/{sha(b)}", data=b, headers={KEY: key})
    assert resp.status == 507


@pytest.mark.parametrize(
    "segment",
    [
        "A" * 64,
        "a" * 63,
        "a" * 65,
        "a" * 64 + ".jpg",
        "..%2F..%2Fconfiguration.yaml",
        "%2e%2e",
        "..",
        "a" * 62 + "%00",
        "a" * 63 + "%0a",
    ],
)
async def test_path_segment_must_be_64_lowercase_hex(hass, lan, segment) -> None:
    key = await lan.pair()
    data = image_bytes()
    resp = await lan.client.put(f"/api/tvcanvas/lan/image/{segment}", data=data, headers={KEY: key})
    assert resp.status in (400, 404, 405)
    assert lan.photos.files == 0
    assert list(photos_dir(hass).iterdir()) == []


async def test_120_puts_a_minute_per_key(lan, monkeypatch) -> None:
    monkeypatch.setattr("custom_components.tvcanvas.lan_pairing.LAN_PUTS_PER_MINUTE", 2)
    k1, k2 = await lan.pair("one"), await lan.pair("two")
    data = image_bytes()
    url = f"/api/tvcanvas/lan/image/{sha(data)}"
    assert (await lan.client.put(url, data=data, headers={KEY: k1})).status == 201
    assert (await lan.client.put(url, data=data, headers={KEY: k1})).status == 200
    resp = await lan.client.put(url, data=data, headers={KEY: k1})
    assert resp.status == 429
    assert (await lan.client.put(url, data=data, headers={KEY: k2})).status == 200
    lan.clock.now += 61
    assert (await lan.client.put(url, data=data, headers={KEY: k1})).status == 200


async def test_responses_are_not_cached(lan) -> None:
    resp = await lan.client.get("/api/tvcanvas/lan/info")
    assert resp.headers["Cache-Control"] == "no-store"


# --- visibility, unload and removal -------------------------------------------------


async def test_phones_sensor_and_diagnostics_show_names_and_dates_never_keys(hass, lan) -> None:
    key = await lan.pair("Alex's iPhone")
    ent = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{lan.entry.entry_id}_lan_phones"
    )
    state = hass.states.get(ent)
    assert state.state == "1"
    [phone] = state.attributes["phones"]
    assert phone["name"] == "Alex's iPhone" and phone["paired_at"]
    diag = await async_get_config_entry_diagnostics(hass, lan.entry)
    assert diag["lan_photos"]["paired_phones"] == [phone]
    hashed = hashlib.sha256(key.encode()).hexdigest()
    for text in (repr(diag), repr(state.attributes)):
        assert key not in text and hashed not in text


async def test_api_is_gone_while_the_entry_is_unloaded(hass, lan) -> None:
    key = await lan.pair()
    await hass.config_entries.async_unload(lan.entry.entry_id)
    assert (await lan.client.get("/api/tvcanvas/lan/info")).status == 404
    assert (await lan.client.get("/api/tvcanvas/lan/have", headers={KEY: key})).status == 404
    resp = await lan.client.post("/api/tvcanvas/lan/pair", json={"code": "123456"})
    assert resp.status == 404


async def test_removing_the_entry_deletes_keys_and_photos(hass, lan, hass_storage) -> None:
    key = await lan.pair()
    data = image_bytes()
    await lan.client.put(f"/api/tvcanvas/lan/image/{sha(data)}", data=data, headers={KEY: key})
    assert photos_dir(hass).is_dir()
    assert f"{DOMAIN}.{lan.entry.entry_id}.lan_keys" in hass_storage
    await hass.config_entries.async_remove(lan.entry.entry_id)
    await hass.async_block_till_done()
    assert f"{DOMAIN}.{lan.entry.entry_id}.lan_keys" not in hass_storage
    assert not photos_dir(hass).exists() and not photos_dir(hass).parent.exists()


async def test_codes_and_keys_are_never_logged(hass, lan, caplog) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    code = await lan.open_code()
    key = await lan.pair()
    await lan.client.get("/api/tvcanvas/lan/have", headers={KEY: "wrong-key-value"})
    assert code not in caplog.text and key not in caplog.text
    assert "wrong-key-value" not in caplog.text


# --- home network only ------------------------------------------------------------

ENDPOINTS = [
    ("get", "/api/tvcanvas/lan/info"),
    ("post", "/api/tvcanvas/lan/pair"),
    ("delete", "/api/tvcanvas/lan/pair"),
    ("get", "/api/tvcanvas/lan/have"),
    ("put", f"/api/tvcanvas/lan/image/{'a' * 64}"),
    ("post", "/api/tvcanvas/lan/prove"),
]


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
@pytest.mark.parametrize("remote", ["8.8.8.8", "2001:4860:4860::8888", "100.64.0.1"])
async def test_clients_outside_the_home_network_get_an_empty_404(
    proxied_lan, method, path, remote
) -> None:
    lan = proxied_lan
    key = await lan.pair()
    code = await lan.open_code()
    headers = {"X-Forwarded-For": remote, KEY: key}
    resp = await getattr(lan.client, method)(path, headers=headers, json={"code": code})
    assert resp.status == 404 and await resp.read() == b""
    assert lan.pairing.pairing_open  # the code wasn't touched


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
async def test_home_clients_behind_a_trusted_proxy_are_served(proxied_lan, method, path) -> None:
    lan = proxied_lan
    key = await lan.pair()
    headers = {"X-Forwarded-For": "192.168.1.20", KEY: key}
    resp = await getattr(lan.client, method)(path, headers=headers, data=b"x")
    assert resp.status != 404


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
async def test_home_assistant_cloud_remote_ui_requests_get_404(lan, method, path) -> None:
    key = await lan.pair()
    with patch("custom_components.tvcanvas.lan_view.is_cloud_connection", return_value=True):
        resp = await getattr(lan.client, method)(path, headers={KEY: key}, data=b"x")
    assert resp.status == 404 and await resp.read() == b""


@pytest.mark.parametrize("remote", ["127.0.0.1", "10.1.2.3", "192.168.1.5", "fe80::1", "fd00::5"])
def test_from_home_accepts_local_addresses(hass, remote) -> None:
    from custom_components.tvcanvas.lan_view import from_home

    request = type("R", (), {"app": {KEY_HASS: hass}, "remote": remote})()
    assert from_home(request)


@pytest.mark.parametrize("remote", ["8.8.8.8", "", None, "not-an-ip", "100.64.0.1"])
def test_from_home_refuses_everything_else(hass, remote) -> None:
    from custom_components.tvcanvas.lan_view import from_home

    request = type("R", (), {"app": {KEY_HASS: hass}, "remote": remote})()
    assert not from_home(request)


# --- prove: HA shows it holds the key's hash (docs/lan-api.md §4) --------------------

# The reference vector in docs/lan-api.md, computed with the standard library only.
VECTOR_KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"  # bytes 0..31
VECTOR_NONCE = "ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8"  # bytes 32..63
VECTOR_KEY_HASH = "ea866a757e4c38babfa8127cbe9a409d3e1f93a00ff1488ff735fcf917afffd0"
VECTOR_KEY_ID = "050d5bee93d6b596"
VECTOR_IP, VECTOR_PORT = "192.168.1.10", 8123
VECTOR_PROOF = "nZiN6DaKoLJBq8-OdotogWt7ZuoGLGARYbhQdfe5FXQ"
VECTOR_IP6 = "fd00::10"  # same key, nonce and port
VECTOR_PROOF6 = "6JBksEVf6LvXwDAYtj89v-xbfIW-D18qj1BNUcm6G5s"
PROVE = "/api/tvcanvas/lan/prove"


def reference_proof(key: str, nonce: str, ip: str, port: int) -> tuple[str, str]:
    import base64
    import hmac

    key_hash = hashlib.sha256(key.encode()).hexdigest()
    msg = f"tvcanvas-lan-prove-v1:{nonce}:{ip}:{port}"
    mac = hmac.new(bytes.fromhex(key_hash), msg.encode(), hashlib.sha256)
    key_id = hashlib.sha256(key_hash.encode()).hexdigest()[:16]
    return key_id, base64.urlsafe_b64encode(mac.digest()).rstrip(b"=").decode()


def test_reference_vector_is_self_consistent() -> None:
    from custom_components.tvcanvas.lan_pairing import key_id, prove

    assert hashlib.sha256(VECTOR_KEY.encode()).hexdigest() == VECTOR_KEY_HASH
    assert reference_proof(VECTOR_KEY, VECTOR_NONCE, VECTOR_IP, VECTOR_PORT) == (
        VECTOR_KEY_ID,
        VECTOR_PROOF,
    )
    assert reference_proof(VECTOR_KEY, VECTOR_NONCE, VECTOR_IP6, VECTOR_PORT)[1] == VECTOR_PROOF6
    assert key_id(VECTOR_KEY_HASH) == VECTOR_KEY_ID
    assert prove(VECTOR_KEY_HASH, VECTOR_NONCE, VECTOR_IP, VECTOR_PORT) == VECTOR_PROOF
    assert prove(VECTOR_KEY_HASH, VECTOR_NONCE, VECTOR_IP6, VECTOR_PORT) == VECTOR_PROOF6
    # The address is bound: another ip or port gives another proof.
    assert prove(VECTOR_KEY_HASH, VECTOR_NONCE, "192.168.1.11", VECTOR_PORT) != VECTOR_PROOF
    assert prove(VECTOR_KEY_HASH, VECTOR_NONCE, VECTOR_IP, 8124) != VECTOR_PROOF


async def test_prove_answers_the_reference_vector(lan) -> None:
    from custom_components.tvcanvas.lan_pairing import Phone

    lan.pairing.phones.append(Phone(VECTOR_KEY_HASH, "Vector", "2026-11-09T00:00:00+00:00"))
    with patch(
        "custom_components.tvcanvas.lan_view.local_address",
        return_value=(VECTOR_IP, VECTOR_PORT),
    ):
        resp = await lan.client.post(PROVE, json={"key_id": VECTOR_KEY_ID, "nonce": VECTOR_NONCE})
    assert resp.status == 200
    assert await resp.json() == {"proof": VECTOR_PROOF, "ip": VECTOR_IP, "port": VECTOR_PORT}
    assert resp.headers["Cache-Control"] == "no-store"


async def test_prove_binds_the_sockets_address_not_the_host_header(lan) -> None:
    import secrets

    key = await lan.pair()
    nonce = secrets.token_urlsafe(32)
    server_port = lan.client.server.port
    resp = await lan.client.post(
        PROVE,
        json={"key_id": reference_proof(key, nonce, "", 0)[0], "nonce": nonce},
        headers={"Host": "evil.example:9999", "X-Forwarded-Host": "evil.example"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ip"] == "127.0.0.1" and body["port"] == server_port != 9999
    assert body["proof"] == reference_proof(key, nonce, "127.0.0.1", server_port)[1]
    assert body["proof"] != reference_proof(key, nonce, "evil.example", 9999)[1]


@pytest.mark.parametrize(
    ("sockname", "expected"),
    [
        (("192.168.1.10", 8123), ("192.168.1.10", 8123)),
        (("::ffff:192.168.1.10", 8123, 0, 0), ("192.168.1.10", 8123)),
        (("fd00:0:0:0:0:0:0:10", 8123, 0, 0), ("fd00::10", 8123)),
        (("FD00::10", 443, 0, 0), ("fd00::10", 443)),
        (("fe80::1%en0", 8123, 0, 4), ("fe80::1", 8123)),
        (None, None),
        ((), None),
        (("not-an-ip", 8123), None),
        (("192.168.1.10", 0), None),
        (("192.168.1.10", "8123"), None),
        ("/run/ha.sock", None),
    ],
)
def test_local_address_normalises_the_socket_address(sockname, expected) -> None:
    from custom_components.tvcanvas.lan_view import local_address

    assert local_address(sockname) == expected


async def test_prove_without_a_socket_address_is_500(lan) -> None:
    from custom_components.tvcanvas.lan_pairing import Phone

    lan.pairing.phones.append(Phone(VECTOR_KEY_HASH, "Vector", "2026-11-09T00:00:00+00:00"))
    with patch("custom_components.tvcanvas.lan_view.local_address", return_value=None):
        resp = await lan.client.post(PROVE, json={"key_id": VECTOR_KEY_ID, "nonce": VECTOR_NONCE})
    assert resp.status == 500 and "proof" not in await resp.json()


async def test_prove_for_a_freshly_paired_phone_needs_no_key_header(hass, lan) -> None:
    import secrets

    await lan.pair("first")
    key = await lan.pair("second")
    nonce = secrets.token_urlsafe(32)
    key_id, expected = reference_proof(key, nonce, "127.0.0.1", lan.client.server.port)
    resp = await lan.client.post(PROVE, json={"key_id": key_id, "nonce": nonce, "extra": 1})
    assert resp.status == 200 and (await resp.json())["proof"] == expected
    # The key_id survives a restart (derived on load).
    from custom_components.tvcanvas.lan_pairing import LanPairing

    again = LanPairing(hass, lan.entry.entry_id)
    await again.async_load()
    assert [p.key_id for p in again.phones] == [p.key_id for p in lan.pairing.phones]


async def test_prove_unknown_key_id_is_403(lan) -> None:
    await lan.pair()
    resp = await lan.client.post(PROVE, json={"key_id": "0" * 16, "nonce": VECTOR_NONCE})
    assert resp.status == 403 and await resp.json() == {"error": "unknown_key"}


async def test_prove_after_unpairing_is_403(lan) -> None:
    key = await lan.pair()
    await lan.client.delete("/api/tvcanvas/lan/pair", headers={KEY: key})
    key_id, _ = reference_proof(key, VECTOR_NONCE, "", 0)
    resp = await lan.client.post(PROVE, json={"key_id": key_id, "nonce": VECTOR_NONCE})
    assert resp.status == 403


@pytest.mark.parametrize(
    "body",
    [
        {"key_id": VECTOR_KEY_ID},
        {"nonce": VECTOR_NONCE},
        {"key_id": VECTOR_KEY_ID, "nonce": ""},
        {"key_id": VECTOR_KEY_ID, "nonce": VECTOR_NONCE[:-1]},  # 42 chars
        {"key_id": VECTOR_KEY_ID, "nonce": VECTOR_NONCE + "A"},  # 44 chars
        {"key_id": VECTOR_KEY_ID, "nonce": VECTOR_NONCE + "="},  # padded
        {"key_id": VECTOR_KEY_ID, "nonce": "+" + VECTOR_NONCE[1:]},  # not url-safe
        {"key_id": VECTOR_KEY_ID, "nonce": " " + VECTOR_NONCE[1:]},
        {"key_id": VECTOR_KEY_ID, "nonce": "é" + VECTOR_NONCE[1:]},
        {"key_id": VECTOR_KEY_ID, "nonce": 12345},
        {"key_id": VECTOR_KEY_ID, "nonce": [VECTOR_NONCE]},
        {"key_id": VECTOR_KEY_ID.upper(), "nonce": VECTOR_NONCE},
        {"key_id": VECTOR_KEY_ID[:15], "nonce": VECTOR_NONCE},
        {"key_id": VECTOR_KEY_HASH, "nonce": VECTOR_NONCE},
        {"key_id": None, "nonce": VECTOR_NONCE},
        [VECTOR_KEY_ID, VECTOR_NONCE],
        "nonce",
    ],
)
async def test_prove_bad_bodies_are_400(lan, body) -> None:
    from custom_components.tvcanvas.lan_pairing import Phone

    lan.pairing.phones.append(Phone(VECTOR_KEY_HASH, "Vector", "2026-11-09T00:00:00+00:00"))
    resp = await lan.client.post(PROVE, json=body)
    assert resp.status == 400 and await resp.json() == {"error": "bad_request"}


async def test_prove_non_json_and_oversized_bodies(lan) -> None:
    assert (await lan.client.post(PROVE, data=b"not json")).status == 400
    big = {"key_id": VECTOR_KEY_ID, "nonce": VECTOR_NONCE, "pad": "x" * 5000}
    assert (await lan.client.post(PROVE, json=big)).status == 413


async def test_prove_is_limited_to_30_a_minute_per_address(proxied_lan) -> None:
    lan = proxied_lan
    body = {"key_id": VECTOR_KEY_ID, "nonce": VECTOR_NONCE}
    one, other = {"X-Forwarded-For": "192.168.1.20"}, {"X-Forwarded-For": "192.168.1.21"}
    statuses = [(await lan.client.post(PROVE, json=body, headers=one)).status for _ in range(31)]
    assert statuses == [403] * 30 + [429]  # bad requests count too
    assert (await lan.client.post(PROVE, json=body, headers=other)).status == 403
    lan.clock.now += 61
    assert (await lan.client.post(PROVE, json=body, headers=one)).status == 403


@pytest.mark.parametrize("remote", ["8.8.8.8", "2001:4860:4860::8888"])
async def test_prove_refuses_clients_outside_the_home_network(proxied_lan, remote) -> None:
    lan = proxied_lan
    from custom_components.tvcanvas.lan_pairing import Phone

    lan.pairing.phones.append(Phone(VECTOR_KEY_HASH, "Vector", "2026-11-09T00:00:00+00:00"))
    body = {"key_id": VECTOR_KEY_ID, "nonce": VECTOR_NONCE}
    resp = await lan.client.post(PROVE, json=body, headers={"X-Forwarded-For": remote})
    assert resp.status == 404 and await resp.read() == b""


async def test_prove_refuses_home_assistant_cloud_remote_ui(lan) -> None:
    body = {"key_id": VECTOR_KEY_ID, "nonce": VECTOR_NONCE}
    with patch("custom_components.tvcanvas.lan_view.is_cloud_connection", return_value=True):
        resp = await lan.client.post(PROVE, json=body)
    assert resp.status == 404 and await resp.read() == b""


async def test_prove_is_404_while_unloaded(hass, lan) -> None:
    await hass.config_entries.async_unload(lan.entry.entry_id)
    body = {"key_id": VECTOR_KEY_ID, "nonce": VECTOR_NONCE}
    assert (await lan.client.post(PROVE, json=body)).status == 404


async def test_prove_never_logs_nonces_or_proofs(lan, caplog) -> None:
    import logging

    from custom_components.tvcanvas.lan_pairing import Phone

    caplog.set_level(logging.DEBUG)
    lan.pairing.phones.append(Phone(VECTOR_KEY_HASH, "Vector", "2026-11-09T00:00:00+00:00"))
    await lan.client.post(PROVE, json={"key_id": VECTOR_KEY_ID, "nonce": VECTOR_NONCE})
    await lan.client.post(PROVE, json={"key_id": "0" * 16, "nonce": VECTOR_NONCE})
    for secret in (VECTOR_NONCE, VECTOR_PROOF, VECTOR_KEY_HASH):
        assert secret not in caplog.text


# --- the setup code: the last 4 of the 12-character code (docs/lan-api.md §3) ------

SETUP = "P4NB"


async def post_code(lan, code, ip=None):
    headers = {"X-Forwarded-For": ip} if ip else {}
    return await lan.client.post(
        "/api/tvcanvas/lan/pair", json={"code": code, "device_name": "iPhone"}, headers=headers
    )


@pytest.mark.parametrize("sent", ["P4NB", "p4nb", "P4-NB", " p4 nb "])
async def test_the_setup_code_pairs_the_phone(hass, lan, hass_storage, sent) -> None:
    lan.pairing.open_setup_code(SETUP)
    info = await (await lan.client.get("/api/tvcanvas/lan/info")).json()
    assert info["pairing_open"] is True
    resp = await post_code(lan, sent)
    assert resp.status == 200
    key = (await resp.json())["key"]
    assert lan.pairing.authenticate(key) is not None
    assert not (await (await lan.client.get("/api/tvcanvas/lan/info")).json())["pairing_open"]
    assert (await post_code(lan, SETUP)).status == 403  # single use
    stored = hass_storage[f"{DOMAIN}.{lan.entry.entry_id}.lan_keys"]
    assert SETUP not in repr(stored) and key not in repr(stored)


@pytest.mark.parametrize(("opened", "sent"), [("10A0", "lOaO"), ("10A0", "IoAo"), ("1100", "iLoO")])
async def test_the_setup_code_accepts_lookalikes(lan, opened, sent) -> None:
    lan.pairing.open_setup_code(opened)
    assert (await post_code(lan, sent)).status == 200


@pytest.mark.parametrize("sent", ["P4N", "P4NBX", "P4NU", "ṗ4NB", "P4NB\x00", 1234, None])
async def test_near_misses_of_the_setup_code_are_wrong(lan, sent) -> None:
    lan.pairing.open_setup_code(SETUP)
    assert (await post_code(lan, sent)).status == 403
    assert lan.pairing._wrong == 1  # counted as a guess


@pytest.mark.parametrize("bad", ["P4N", "P4NBX", "P4NU", "", "123456", "1234567"])
def test_only_4_crockford_characters_open_as_a_setup_code(lan, bad) -> None:
    with pytest.raises(ValueError):
        lan.pairing.open_setup_code(bad)
    assert not lan.pairing.pairing_open


async def test_the_setup_code_expires_after_10_minutes(lan) -> None:
    lan.pairing.open_setup_code(SETUP)
    lan.clock.now += 599
    assert lan.pairing.pairing_open
    lan.clock.now += 2
    assert (await post_code(lan, SETUP)).status == 403


async def test_a_setup_code_never_lives_longer_than_10_minutes(lan) -> None:
    lan.pairing.open_setup_code(SETUP, ttl=10_000)
    lan.clock.now += 601
    assert not lan.pairing.pairing_open


async def test_a_new_setup_code_replaces_the_earlier_one(hass, lan) -> None:
    code = await lan.open_code()
    lan.pairing.open_setup_code(SETUP)
    assert (await post_code(lan, code)).status == 403
    assert (await post_code(lan, SETUP)).status == 200


async def test_five_wrong_guesses_kill_the_setup_code_and_lock_that_address(lan) -> None:
    lan.pairing.open_setup_code(SETUP)
    assert [(await post_code(lan, "ZZZZ")).status for _ in range(5)] == [403] * 5
    assert not lan.pairing.pairing_open  # the code is dead
    resp = await post_code(lan, SETUP)
    assert resp.status == 429 and await resp.json() == {"error": "locked", "retry_after": 300}
    lan.clock.now += 301
    assert (await post_code(lan, SETUP)).status == 403  # unlocked, but that code is gone


async def test_lockout_is_per_address_for_the_setup_code(proxied_lan) -> None:
    lan = proxied_lan
    stranger, phone = "192.168.1.66", "192.168.1.20"
    lan.pairing.open_setup_code(SETUP)
    assert [(await post_code(lan, "ZZZZ", stranger)).status for _ in range(4)] == [403] * 4
    assert (await post_code(lan, SETUP, phone)).status == 200


async def test_opening_a_setup_code_lifts_no_lockout(lan) -> None:
    code = await lan.open_code()
    await wrong_codes(lan, code, 5)  # this address is locked out
    lan.pairing.open_setup_code(SETUP)
    assert lan.pairing.pairing_open
    resp = await post_code(lan, SETUP)
    assert resp.status == 429  # only time lifts a lockout
    lan.clock.now += 301
    assert (await post_code(lan, SETUP)).status == 200


async def test_the_setup_code_is_never_logged_or_in_diagnostics(hass, lan, caplog) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    lan.pairing.open_setup_code(SETUP)
    await post_code(lan, "ZZZZ")
    diag = await async_get_config_entry_diagnostics(hass, lan.entry)
    assert (await post_code(lan, SETUP)).status == 200
    for text in (caplog.text, repr(diag), repr(lan.entry.data), repr(lan.entry.options)):
        assert SETUP not in text and "ZZZZ" not in text


async def test_one_step_setup_end_to_end(hass, aioclient_mock, hass_client_no_auth) -> None:
    """The user types K7QM-2XRA-P4NB into HA; the phone then pairs with P4NB."""
    from homeassistant import config_entries

    from custom_components.tvcanvas.const import DEFAULT_CLOUD_URL

    aioclient_mock.post(
        f"{DEFAULT_CLOUD_URL}/ha/claim",
        json={"token": "fc_ha_x", "expires_at": "2099-01-01T00:00:00Z"},
    )
    aioclient_mock.get(f"{DEFAULT_CLOUD_URL}/ha/agent/sync", json={"specs": []})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "K7QM-2XRA-P4NB", "advanced": {}}
    )
    await hass.async_block_till_done()
    assert aioclient_mock.mock_calls[0][2] == {"code": "K7QM2XRA"}
    [e] = hass.config_entries.async_entries(DOMAIN)
    client = await hass_client_no_auth()
    resp = await client.post("/api/tvcanvas/lan/pair", json={"code": "P4NB", "device_name": "x"})
    assert resp.status == 200
    key = (await resp.json())["key"]
    have = await client.get("/api/tvcanvas/lan/have", headers={KEY: key})
    assert have.status == 200
    await hass.config_entries.async_unload(e.entry_id)
