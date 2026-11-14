#!/usr/bin/env python3
"""Local stand-in for the TVCanvas Cloud HA contract (docs/cloud-contract.md).

Dev only. Specs are re-read from the JSON file on every sync, so editing the file
is how you "change a pin on the phone".
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiohttp import web
from PIL import Image, ImageDraw

PAIR_CODE = "ABCD2345"
# Stateless auth: any fake token is valid, so restarting the fake cloud does not force reauth.
TOKEN_PREFIX = "fc_ha_fake"


def _jpeg(label: str) -> bytes:
    img = Image.new("RGB", (3840, 2160), (20, 30, 60))
    draw = ImageDraw.Draw(img)
    text = f"{label}\n{datetime.now().strftime('%H:%M:%S')}"
    draw.multiline_text((200, 800), text, fill="white", font_size=220)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


def make_app(specs_path: Path, fail_board: int | None) -> web.Application:
    def authed(request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        return header.startswith("Bearer ") and header[7:].startswith(TOKEN_PREFIX)

    def issue() -> dict[str, str]:
        token = f"{TOKEN_PREFIX}{secrets.token_hex(8)}.sig"
        expires = (datetime.now(UTC) + timedelta(days=30)).isoformat().replace("+00:00", "Z")
        return {"token": token, "expires_at": expires}

    async def claim(request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("code") != PAIR_CODE:
            return web.Response(status=400, text="Invalid or expired code")
        return web.json_response(issue())

    async def sync(request: web.Request) -> web.Response:
        if not authed(request):
            return web.Response(status=401)
        raw = specs_path.read_bytes()
        etag = '"' + hashlib.sha256(raw).hexdigest()[:16] + '"'
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers={"ETag": etag})
        return web.json_response({"specs": json.loads(raw)}, headers={"ETag": etag})

    async def status(request: web.Request) -> web.Response:
        if not authed(request):
            return web.Response(status=401)
        print("STATUS", await request.json(), flush=True)
        return web.Response(status=204)

    async def refresh(request: web.Request) -> web.Response:
        if not authed(request):
            return web.Response(status=401)
        return web.json_response(issue())

    async def board(request: web.Request) -> web.Response:
        if not authed(request):
            return web.Response(status=401)
        if fail_board:
            return web.Response(status=fail_board)
        label = f"board {request.match_info['type_id']} {dict(request.query)}"
        return web.Response(body=_jpeg(label), content_type="image/jpeg")

    async def proxy(request: web.Request) -> web.Response:
        if not authed(request):
            return web.Response(status=401)
        return web.Response(
            body=_jpeg(f"proxy {request.match_info['image_id']}"), content_type="image/jpeg"
        )

    app = web.Application()
    app.add_routes(
        [
            web.post("/ha/claim", claim),
            web.get("/ha/agent/sync", sync),
            web.post("/ha/agent/status", status),
            web.post("/ha/agent/refresh", refresh),
            web.get("/board/{type_id}", board),
            web.get("/collections/proxy/{image_id}", proxy),
        ]
    )
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--specs", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--fail-board", type=int, choices=[401, 402, 429])
    args = parser.parse_args()
    print(
        f"Fake TVCanvas cloud (DEV ONLY, no real auth) listening on ALL interfaces, "
        f"port {args.port} - pairing code ABCD-2345",
        flush=True,
    )
    web.run_app(make_app(args.specs, args.fail_board), port=args.port)


if __name__ == "__main__":
    main()
