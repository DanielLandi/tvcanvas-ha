"""Per TV: the image HA last sent to it ("Current image") and the preview ("Preview
image"), for a dashboard picture card.

The bytes stay in memory (TVRunner.now_showing, TVRunner.preview) and leave HA only
through HA's own image proxy (`/api/image_proxy/<entity_id>`), which needs an HA login
or the entity's short-lived access token. Neither entity fetches a URL (no `image_url`).

An image made from a phone photo (either entity) is served only to a request from the
home network that does not come through Home Assistant Cloud's remote UI: the same rule
as the phone's LAN API (`lan_view.from_home`). Photos never leave the home (trust model,
docs/lan-api.md); boards, webcams and collection images are served to any logged-in
viewer."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.http import current_request

from . import TVCanvasConfigEntry
from .coordinator import TVCanvasCoordinator
from .entity import TVEntity, per_tv
from .lan_view import from_home
from .runner import NowShowing, TVRunner


async def async_setup_entry(
    hass: HomeAssistant, entry: TVCanvasConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    per_tv(
        coordinator,
        lambda runner: TVImage(coordinator, entry.entry_id, runner),
        async_add_entities,
    )
    per_tv(
        coordinator,
        lambda runner: TVPreviewImage(coordinator, entry.entry_id, runner),
        async_add_entities,
    )


# Headers some reverse proxies add that HA's `http:` config does not read. Behind such a
# proxy `request.remote` may be the proxy's own (local) address, so HA can't tell where
# the viewer is: deny.
UNTRUSTED_PROXY_HEADERS = (
    "Forwarded",
    "X-Real-IP",
    "CF-Connecting-IP",
    "True-Client-IP",
    "X-Client-IP",
    "Fastly-Client-IP",
)


def viewer_at_home() -> bool:
    """The image is being fetched by an HTTP request from the home network, not via
    HA Cloud. Anything else (no request at all, e.g. a service writing a snapshot or
    handing the image to another integration) is not.

    The address is `request.remote` as HA resolved it: the peer, or the client in
    X-Forwarded-For only when HA's `http:` config trusts the proxy that sent it (HA
    answers 400 to that header from anyone else). Never a header read here."""
    request = current_request.get()
    if request is None:
        return False
    if any(h in request.headers for h in UNTRUSTED_PROXY_HEADERS):
        return False
    return from_home(request)


class _KeptImage(TVEntity, ImageEntity):
    """An image the runner keeps in memory (what the TV shows, or the preview)."""

    _attr_content_type = "image/jpeg"

    def __init__(self, coordinator: TVCanvasCoordinator, entry_id: str, runner: TVRunner) -> None:
        TVEntity.__init__(self, coordinator, entry_id, runner)
        ImageEntity.__init__(self, coordinator.hass)
        self._attr_image_last_updated = self._kept_at()

    def _kept(self, runner: TVRunner) -> NowShowing | None:
        raise NotImplementedError

    def _kept_at(self) -> datetime | None:
        runner = self._runner
        kept = self._kept(runner) if runner is not None else None
        return kept.at if kept is not None else None

    @callback
    def _handle_coordinator_update(self) -> None:
        self._attr_image_last_updated = self._kept_at()
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        runner = self._runner
        kept = self._kept(runner) if runner is not None else None
        if kept is None:
            return None
        if kept.item.photo:
            photos = self.coordinator.photos
            if photos is None or kept.item.sha256 not in photos:  # deleted since
                runner.forget_shown(photos_only=True, held=photos or ())
                return None
            if not viewer_at_home():
                return None
        return kept.image


class TVImage(_KeptImage):
    _attr_translation_key = "current_image"

    def _kept(self, runner: TVRunner) -> NowShowing | None:
        return runner.now_showing


class TVPreviewImage(_KeptImage):
    """The "Preview" select's image: rendered only on a pick, never sent to the TV here."""

    _attr_translation_key = "preview_image"

    def _kept(self, runner: TVRunner) -> NowShowing | None:
        return runner.preview
