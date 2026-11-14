from __future__ import annotations

from datetime import timedelta

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tvcanvas.const import (
    CONF_CLOUD_URL,
    CONF_TOKEN,
    CONF_TOKEN_EXPIRES_AT,
    DOMAIN,
)

URL = "https://cloud.test"


def frame_spec(tv_id="tv-1", host="10.0.0.2", type_id="weather", interval=300):
    return {
        "version": 1,
        "tv": {"id": tv_id, "kind": "frame", "host": host, "name": f"TV {tv_id}"},
        "enabled": True,
        "interval_seconds": interval,
        "source": {"type": "board", "board": {"type_id": type_id, "params": {}}},
    }


def entry(hass, days_left=20):
    e = MockConfigEntry(
        domain=DOMAIN,
        title="TVCanvas",
        data={
            CONF_TOKEN: "fc_ha_t",
            CONF_TOKEN_EXPIRES_AT: (dt_util.utcnow() + timedelta(days=days_left)).isoformat(),
            CONF_CLOUD_URL: URL,
        },
    )
    e.add_to_hass(hass)
    return e
