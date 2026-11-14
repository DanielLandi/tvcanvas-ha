"""Pull specs from the cloud and keep one runner per TV."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import aiohttp
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import AuthError, CloudClient, CloudError
from .const import (
    CONF_TOKEN,
    CONF_TOKEN_EXPIRES_AT,
    DOMAIN,
    MAX_TVS,
    SYNC_INTERVAL,
    TOKEN_REFRESH_BEFORE,
    TOKEN_REFRESH_RETRY,
)
from .display_cast import CastDisplay
from .display_frame import FrameDisplay
from .display_view import ImageCache
from .displays import Display
from .lan_pairing import LanPairing
from .locate import FrameFinder
from .locate_cast import CastFinder
from .models import TV, SpecError, SyncSpec, parse_spec, referenced_photos
from .photos import PhotoStore
from .runner import TVRunner
from .storage import StateStore
from .urlsafety import create_image_session

_LOGGER = logging.getLogger(__name__)


class TVCanvasCoordinator(DataUpdateCoordinator[dict[str, SyncSpec]]):
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: CloudClient,
        store: StateStore,
        cache: ImageCache,
        photos: PhotoStore | None = None,
        pairing: LanPairing | None = None,
    ) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=SYNC_INTERVAL
        )
        self._client, self._store, self._cache = client, store, cache
        self.photos, self.pairing = photos, pairing
        self.runners: dict[str, TVRunner] = {}
        self._runner_listeners: list[Callable[[TVRunner], None]] = []
        self._cloud_down_logged = False
        self._reauth_started = False
        self._refresh_failed_logged = False
        self._refresh_failures = 0
        self._refresh_retry_at: datetime | None = None
        self._too_many_logged = False
        self._shutdown = False
        self._image_session: aiohttp.ClientSession | None = None
        self._skipped: set[str] = set()  # "tv id: error" lines already warned about

    def async_add_runner_listener(self, cb: Callable[[TVRunner], None]) -> None:
        self._runner_listeners.append(cb)
        for runner in self.runners.values():
            cb(runner)

    async def _async_update_data(self) -> dict[str, SyncSpec]:
        await self._maybe_refresh_token()
        raw_specs = self._store.specs
        try:
            result = await self._client.agent_sync(self._store.etag)
        except AuthError as err:
            await self.async_shutdown_runners()
            raise ConfigEntryAuthFailed("TVCanvas token rejected") from err
        except CloudError as err:
            if not self._cloud_down_logged:
                _LOGGER.warning("TVCanvas cloud unreachable, running the last known specs: %s", err)
                self._cloud_down_logged = True
        else:
            self._cloud_down_logged = False
            if result.specs is not None:
                self._store.specs = raw_specs = result.specs
                self._store.etag = result.etag

        specs = self._parse(raw_specs)
        await self._reconcile(specs)
        await self._collect_photos(raw_specs)
        return specs

    async def _collect_photos(self, raw_specs: list[Any]) -> None:
        """Delete phone photos no spec references any more (10 minutes after upload)."""
        if self.photos is None or self._shutdown:
            return
        try:
            await self.photos.async_gc(referenced_photos(raw_specs))
        except OSError as err:
            _LOGGER.warning("TVCanvas: could not delete unused photos: %s", err.strerror or err)

    def _parse(self, raw_specs: list[Any]) -> dict[str, SyncSpec]:
        """Parse every spec; warn once per TV and error, not on every 60 s poll."""
        specs: dict[str, SyncSpec] = {}
        skipped: set[str] = set()
        too_many = False
        for raw in raw_specs:
            if len(specs) == MAX_TVS:
                # The cloud allows 10 TVs; a broken or hacked one may send thousands (#17).
                too_many = True
                if not self._too_many_logged:
                    _LOGGER.warning(
                        "TVCanvas: the cloud sent %d TVs; Home Assistant runs at most %d TVs "
                        "and skips the rest",
                        len(raw_specs),
                        MAX_TVS,
                    )
                    self._too_many_logged = True
                break
            try:
                spec = parse_spec(raw)
            except SpecError as err:
                line = f"{_raw_tv_id(raw)}: {err}"  # the id may be any JSON value
                if line not in self._skipped:
                    _LOGGER.warning("TVCanvas: skipping TV %s", line)
                skipped.add(line)
                continue
            specs[spec.tv.id] = spec
        self._skipped = skipped
        if not too_many:
            self._too_many_logged = False  # warn again if it happens again
        return specs

    async def _reconcile(self, specs: dict[str, SyncSpec]) -> None:
        # Shutdown (unload, reauth) can land during any await below: re-check
        # before every step so no runner is started or updated after it.
        for tv_id in list(self.runners):
            if self._shutdown:
                return
            if tv_id not in specs:
                removed = self.runners.pop(tv_id)
                await removed.stop()
                removed.release()  # the phone took this TV back: clean up what HA left
                persistent_notification.async_dismiss(self.hass, f"tvcanvas_pair_{tv_id}")
                persistent_notification.async_dismiss(self.hass, f"tvcanvas_moved_{tv_id}")
                self._store.forget_tv(tv_id)
                self._cache.drop_tv(tv_id)
        for tv_id, spec in specs.items():
            if self._shutdown:
                return
            runner = self.runners.get(tv_id)
            if runner is None:
                runner = TVRunner(
                    self.hass,
                    self._client,
                    self._direct_image_session(),
                    self._store,
                    spec,
                    self._display_for(spec),
                    self.handle_auth_error,
                    self.async_update_listeners,
                    self.photos,
                )
                self.runners[tv_id] = runner
                runner.start()
                for cb in self._runner_listeners:
                    cb(runner)
            elif runner.spec != spec:
                await runner.update(spec, self._display_for(spec))

    def _direct_image_session(self) -> aiohttp.ClientSession:
        """The session runners fetch webcam/custom-URL images with: public addresses only."""
        if self._image_session is None:
            self._image_session = create_image_session()
        return self._image_session

    async def async_shutdown(self) -> None:
        """Also close the image session (entry unload and HA stop both land here)."""
        await super().async_shutdown()
        if self._image_session is not None:
            await self._image_session.close()
            self._image_session = None

    def _display_for(self, spec: SyncSpec) -> Display:
        if spec.tv.kind == "cast":
            return CastDisplay(
                self.hass,
                self._cache,
                spec.tv.id,
                spec.tv.host,
                store=self._store,
                finder=CastFinder(self.hass),
            )
        return FrameDisplay(
            self.hass,
            self._store,
            spec.tv.id,
            spec.tv.host,
            spec.replace_previous,
            self._pairing_notice(spec.tv),
            finder=FrameFinder(self.hass),
        )

    def _pairing_notice(self, tv: TV) -> Callable[[bool], None]:
        notification_id = f"tvcanvas_pair_{tv.id}"

        def notify(active: bool) -> None:
            if active:
                persistent_notification.async_create(
                    self.hass,
                    f"Approve **TVCanvas-HA** on your TV ({tv.name})"
                    " — press Allow with the TV remote.",
                    title="TVCanvas",
                    notification_id=notification_id,
                )
            else:
                persistent_notification.async_dismiss(self.hass, notification_id)

        return notify

    async def _maybe_refresh_token(self) -> None:
        entry = self.config_entry
        try:
            expires: datetime | None = datetime.fromisoformat(entry.data[CONF_TOKEN_EXPIRES_AT])
        except (KeyError, TypeError, ValueError):
            expires = None
        if expires is not None:
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires - dt_util.utcnow() > TOKEN_REFRESH_BEFORE:
                return
        now = dt_util.utcnow()
        if self._refresh_retry_at is not None and now < self._refresh_retry_at:
            return  # the cloud allows 5 refreshes an hour; there are 7 days of room (#17)
        try:
            info = await self._client.agent_refresh()
        except AuthError as err:
            await self.async_shutdown_runners()
            raise ConfigEntryAuthFailed("TVCanvas token rejected") from err
        except CloudError as err:
            if not self._refresh_failed_logged:
                _LOGGER.warning("TVCanvas: token refresh failed, will retry: %s", err)
                self._refresh_failed_logged = True
            wait = TOKEN_REFRESH_RETRY[min(self._refresh_failures, len(TOKEN_REFRESH_RETRY) - 1)]
            self._refresh_failures += 1
            self._refresh_retry_at = now + wait
            return
        self._refresh_failed_logged = False
        self._refresh_failures, self._refresh_retry_at = 0, None
        self._client.token = info.token
        data: dict[str, Any] = {
            **entry.data,
            CONF_TOKEN: info.token,
            CONF_TOKEN_EXPIRES_AT: info.expires_at.isoformat(),
        }
        self.hass.config_entries.async_update_entry(entry, data=data)

    def handle_auth_error(self) -> None:
        if self._reauth_started:
            return
        self._reauth_started = True
        self.config_entry.async_create_background_task(
            self.hass, self.async_shutdown_runners(), "tvcanvas shutdown runners"
        )
        self.config_entry.async_start_reauth(self.hass)

    async def async_shutdown_runners(self) -> None:
        self._shutdown = True
        for runner in list(self.runners.values()):
            await runner.stop()
            persistent_notification.async_dismiss(self.hass, f"tvcanvas_pair_{runner.spec.tv.id}")

    async def async_unload(self) -> None:
        """Stop every TV, forget its Cast images and write the store now."""
        await self.async_shutdown_runners()
        for tv_id in self.runners:
            self._cache.drop_tv(tv_id)
        await self._store.async_save_now()


def _raw_tv_id(raw: Any) -> Any:
    tv = raw.get("tv") if isinstance(raw, dict) else None
    return tv.get("id") if isinstance(tv, dict) else None
