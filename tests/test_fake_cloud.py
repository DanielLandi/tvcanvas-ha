from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from custom_components.tvcanvas.models import parse_spec

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("fake_cloud", ROOT / "scripts" / "fake_cloud.py")
fake_cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fake_cloud)


async def test_contract_round_trip(socket_enabled, aiohttp_client, tmp_path) -> None:
    specs = tmp_path / "specs.json"
    specs.write_text(json.dumps([{"version": 1, "tv": {"id": "tv-1"}}]))
    client = await aiohttp_client(fake_cloud.make_app(specs, None))

    assert (await client.post("/ha/claim", json={"code": "WRONG000"})).status == 400
    claim = await (await client.post("/ha/claim", json={"code": "ABCD2345"})).json()
    auth = {"Authorization": f"Bearer {claim['token']}"}

    assert (await client.get("/ha/agent/sync")).status == 401
    first = await client.get("/ha/agent/sync", headers=auth)
    assert (await first.json())["specs"][0]["tv"]["id"] == "tv-1"
    again = await client.get(
        "/ha/agent/sync", headers={**auth, "If-None-Match": first.headers["ETag"]}
    )
    assert again.status == 304

    board = await client.get("/board/weather", headers=auth)
    assert board.status == 200 and board.headers["Content-Type"] == "image/jpeg"
    proxied = await client.get("/collections/proxy/met:paintings:abc:def", headers=auth)
    assert proxied.status == 200 and proxied.headers["Content-Type"].startswith("image/")
    status = await client.post(
        "/ha/agent/status",
        headers=auth,
        json={"tv_id": "tv-1", "ok": True, "error": None, "at": "2026-10-11T10:00:00+00:00"},
    )
    assert status.status == 204


async def test_fail_board_returns_configured_status(
    socket_enabled, aiohttp_client, tmp_path
) -> None:
    specs = tmp_path / "specs.json"
    specs.write_text("[]")
    client = await aiohttp_client(fake_cloud.make_app(specs, 402))
    claim = await (await client.post("/ha/claim", json={"code": "ABCD2345"})).json()
    auth = {"Authorization": f"Bearer {claim['token']}"}
    assert (await client.get("/board/weather", headers=auth)).status == 402


def test_example_specs_parse() -> None:
    raw = json.loads((ROOT / "examples" / "specs.example.json").read_text())
    assert len(raw) == 3
    types = [parse_spec(element).source.type for element in raw]
    assert types == ["board", "collection", "group"]


async def test_token_survives_fake_cloud_restart(socket_enabled, aiohttp_client, tmp_path) -> None:
    specs = tmp_path / "specs.json"
    specs.write_text("[]")
    first = await aiohttp_client(fake_cloud.make_app(specs, None))
    claim = await (await first.post("/ha/claim", json={"code": "ABCD2345"})).json()
    auth = {"Authorization": f"Bearer {claim['token']}"}

    second = await aiohttp_client(fake_cloud.make_app(specs, None))
    assert (await second.get("/ha/agent/sync", headers=auth)).status == 200
    assert (
        await second.get("/ha/agent/sync", headers={"Authorization": "Bearer nope"})
    ).status == 401
