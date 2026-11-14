"""Diagnostics with secrets redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import TVCanvasConfigEntry
from .urlsafety import redact

TO_REDACT = {"token", "host"}


def _scrub(text: str | None, *hosts: str) -> str | None:
    """No TV address, and no URL credentials, tokens or keys."""
    if not text:
        return text
    text = redact(text)
    for host in hosts:
        if host:
            text = text.replace(host, "**REDACTED**")
    return text


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TVCanvasConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    photos, pairing = coordinator.photos, coordinator.pairing
    return {
        "lan_photos": {
            "paired_phones": [p.public() for p in pairing.phones] if pairing else [],
            "files": photos.files if photos else 0,
            "bytes_used": photos.bytes_used if photos else 0,
        },
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "tvs": {
            tv_id: {
                "kind": r.spec.tv.kind,
                "source": r.spec.source.type,
                "interval_seconds": r.spec.interval_seconds,
                "enabled": r.spec.enabled,
                "last_ok": r.status.last_ok.isoformat() if r.status.last_ok else None,
                "last_error": _scrub(r.status.last_error, r.spec.tv.host, r.address),
                "followed_to_new_address": r.address != r.spec.tv.host,
            }
            for tv_id, r in coordinator.runners.items()
        },
    }
