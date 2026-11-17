"""TVCanvas for Home Assistant."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CloudClient
from .const import CONF_CLOUD_URL, CONF_TOKEN, DOMAIN
from .coordinator import TVCanvasCoordinator
from .display_view import ImageCache, ImageView
from .lan_pairing import LanPairing, keys_store, take_setup_code
from .lan_view import LanHub, register_views
from .photos import PhotoStore, async_remove_all
from .storage import StateStore

PLATFORMS = [Platform.BUTTON, Platform.IMAGE, Platform.SELECT, Platform.SENSOR, Platform.SWITCH]
# The "Pair a phone for photos" button and its 6-digit code, removed (tvcanvas-ha#21):
# the 12-character code pairs the phone. Its unique id, for removal on upgrade.
_RETIRED_BUTTON_ID = "{entry_id}_lan_pair"
type TVCanvasConfigEntry = ConfigEntry[TVCanvasCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: TVCanvasConfigEntry) -> bool:
    domain = hass.data.setdefault(DOMAIN, {})
    if "cache" not in domain:
        domain["cache"] = ImageCache()
        hass.http.register_view(ImageView(domain["cache"]))
    if "lan" not in domain:
        domain["lan"] = LanHub()
        register_views(hass, domain["lan"])

    _remove_retired_entities(hass, entry)  # even if the cloud is down and setup retries

    client = CloudClient(
        async_get_clientsession(hass), entry.data[CONF_CLOUD_URL], entry.data[CONF_TOKEN]
    )
    store = StateStore(hass, entry.entry_id)
    await store.async_load()
    photos = PhotoStore(hass)
    await photos.async_load()
    pairing = LanPairing(hass, entry.entry_id)
    await pairing.async_load()

    coordinator = TVCanvasCoordinator(
        hass, entry, client, store, domain["cache"], photos=photos, pairing=pairing
    )
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    # Without a listener the coordinator stops polling (no entities on first run).
    entry.async_on_unload(coordinator.async_add_listener(lambda: None))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    lan: LanHub = domain["lan"]
    lan.photos, lan.pairing = photos, pairing  # the phone API answers from here on
    # The config flow's 12-character code: its last 4 open photo pairing now (taken
    # only once setup has succeeded, so a setup retry still finds it).
    if (setup := take_setup_code(hass)) is not None:
        pairing.open_setup_code(*setup)

    def detach() -> None:
        lan.photos = lan.pairing = None

    entry.async_on_unload(detach)
    return True


def _remove_retired_entities(hass: HomeAssistant, entry: TVCanvasConfigEntry) -> None:
    """Drop the registry entry of the retired pairing button, so an upgraded install
    doesn't keep an orphaned, unavailable "Pair a phone for photos"."""
    registry = er.async_get(hass)
    unique_id = _RETIRED_BUTTON_ID.format(entry_id=entry.entry_id)
    if entity_id := registry.async_get_entity_id(Platform.BUTTON, DOMAIN, unique_id):
        registry.async_remove(entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: TVCanvasConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_unload()
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: TVCanvasConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Let the user delete a TV's device once the phone has taken that TV back."""
    running = {(DOMAIN, f"{entry.entry_id}_{tv_id}") for tv_id in entry.runtime_data.runners}
    running.add((DOMAIN, entry.entry_id))  # the integration's own device (Paired phones)
    return not device.identifiers & running


async def async_remove_entry(hass: HomeAssistant, entry: TVCanvasConfigEntry) -> None:
    """The private stores and the phones' photos outlive the entry otherwise."""
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is not None:  # normally already dropped at unload; never keep a copy
        for runner in coordinator.runners.values():
            runner.forget_shown()
    await StateStore(hass, entry.entry_id).async_remove()
    await keys_store(hass, entry.entry_id).async_remove()
    await async_remove_all(hass)
    take_setup_code(hass)  # a setup code no setup consumed dies with the entry
