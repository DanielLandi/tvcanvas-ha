"""Serve the current Cast image from HA at an unguessable URL, to the home network only."""

from __future__ import annotations

import secrets
from collections import OrderedDict

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .const import CAST_IMAGES_KEPT, IMAGE_VIEW_URL
from .lan_view import from_home


class ImageCache:
    def __init__(self) -> None:
        self._by_tv: dict[str, OrderedDict[str, bytes]] = {}

    def put(self, tv_id: str, data: bytes) -> str:
        name = f"{secrets.token_hex(16)}.jpg"
        images = self._by_tv.setdefault(tv_id, OrderedDict())
        images[name] = data
        while len(images) > CAST_IMAGES_KEPT:
            images.popitem(last=False)
        return name

    def get(self, name: str) -> bytes | None:
        for images in self._by_tv.values():
            if name in images:
                return images[name]
        return None

    def drop_tv(self, tv_id: str) -> None:
        self._by_tv.pop(tv_id, None)


class ImageView(HomeAssistantView):
    url = IMAGE_VIEW_URL
    name = "api:tvcanvas:image"
    requires_auth = False  # Cast devices can't authenticate; the 128-bit name is the secret

    def __init__(self, cache: ImageCache) -> None:
        self._cache = cache

    async def get(self, request: web.Request, name: str) -> web.Response:
        # HA's port may be reachable from the internet and the image may be a phone photo:
        # only the home network, where Cast TVs are, gets one (#15). Anyone else gets the
        # empty 404 an unknown name gets, as on /api/tvcanvas/lan/*.
        data = self._cache.get(name) if from_home(request) else None
        if data is None:
            return web.Response(status=404)
        return web.Response(
            body=data, content_type="image/jpeg", headers={"Cache-Control": "no-store"}
        )
