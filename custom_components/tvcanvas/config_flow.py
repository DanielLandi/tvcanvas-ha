"""Pair with TVCanvas Cloud by the code shown in the app.

The app shows 8 characters (the cloud code) or 12: the cloud code plus a 4-character
LAN setup code the phone generated itself. Only the first 8 go to the cloud; on a
successful claim the last 4 open photo pairing for the phone (docs/lan-api.md §3),
held in memory only."""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.data_entry_flow import section
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    CloudClient,
    CloudError,
    CloudServerError,
    CloudUnreachable,
    InvalidCode,
    RateLimited,
    TokenInfo,
    WrongServer,
    split_code,
)
from .const import (
    CONF_ADVANCED,
    CONF_CLOUD_URL,
    CONF_CODE,
    CONF_TOKEN,
    CONF_TOKEN_EXPIRES_AT,
    DEFAULT_CLOUD_URL,
    DOMAIN,
)
from .lan_pairing import stash_setup_code
from .urlsafety import redact

_LOGGER = logging.getLogger(__name__)

# hassfest refuses URLs in strings.json: the errors name the default server through this.
_PLACEHOLDERS = {"default_url": DEFAULT_CLOUD_URL}


def _is_local_host(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost" or host.endswith((".local", ".lan", ".home.arpa"))
    return ip.is_loopback or ip.is_private or ip.is_link_local


def _cloud_url_error(url: str) -> str | None:
    """Why *url* can't be the cloud's base URL (an error key), or None if it can.

    https, or http to a local development server; only scheme, host and port: the
    integration adds /ha/claim etc. itself, so a path (or ?, #, user@) means the user
    pasted something other than the server's address."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        return "invalid_url"
    if not host:
        return "invalid_url"
    if not (parts.scheme == "https" or (parts.scheme == "http" and _is_local_host(host))):
        return "invalid_url"
    if parts.path not in ("", "/") or parts.query or parts.fragment or "@" in parts.netloc:
        return "url_not_base"
    return None


# What the user sees for each way a claim can fail (strings.json config.error). The
# cloud's 400 is the code's fault, so it goes on the code field; the rest on the form.
_CLAIM_ERRORS: tuple[tuple[type[Exception], str], ...] = (
    (CloudUnreachable, "cannot_connect"),  # DNS, refused, TLS, timeout: no HTTP answer
    (WrongServer, "wrong_server"),  # 401/403/404/405, a redirect, not a token
    (RateLimited, "too_many_attempts"),  # 429
    (CloudServerError, "server_error"),  # 5xx
    (CloudError, "unexpected_answer"),  # any other status
)


class TVCanvasConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _claim(
        self, cloud_url: str, raw_code: str
    ) -> tuple[TokenInfo | None, dict[str, str]]:
        """Claim the cloud code; on success, hand any LAN setup suffix to the entry's
        setup (async_setup_entry opens it as the live photo pairing code). The
        suffix is never stored, logged or sent to the cloud."""
        parts = split_code(raw_code)
        if parts is None:
            return None, {CONF_CODE: "code_format"}
        code, setup_suffix = parts
        try:
            info = await CloudClient(async_get_clientsession(self.hass), cloud_url).claim(code)
        except InvalidCode:
            return None, {CONF_CODE: "invalid_code"}
        except CloudError as err:
            # The reason, for the log; never the code.
            _LOGGER.info("TVCanvas: claiming the code failed: %s", redact(f"{cloud_url}: {err}"))
            key = next(key for kind, key in _CLAIM_ERRORS if isinstance(err, kind))
            return None, {"base": key}
        except Exception:  # noqa: BLE001 - show "unknown" in the form, not a crashed flow
            _LOGGER.exception("TVCanvas: unexpected error while claiming the code")
            return None, {"base": "unknown"}
        if setup_suffix is not None:
            # Both steps end in (re)loading the entry, whose setup takes it from here.
            stash_setup_code(self.hass, setup_suffix)
        return info, {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            advanced = user_input.get(CONF_ADVANCED) or {}
            cloud_url = advanced.get(CONF_CLOUD_URL, DEFAULT_CLOUD_URL).strip().rstrip("/")
            if url_error := _cloud_url_error(cloud_url):
                # A field error inside a section isn't shown by the frontend; base is.
                info, errors = None, {"base": url_error}
            else:
                info, errors = await self._claim(cloud_url, user_input[CONF_CODE])
            if info:
                return self.async_create_entry(
                    title="TVCanvas",
                    data={
                        CONF_TOKEN: info.token,
                        CONF_TOKEN_EXPIRES_AT: info.expires_at.isoformat(),
                        CONF_CLOUD_URL: cloud_url,
                    },
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_CODE): str,
                vol.Required(CONF_ADVANCED): section(
                    vol.Schema({vol.Optional(CONF_CLOUD_URL, default=DEFAULT_CLOUD_URL): str}),
                    {"collapsed": True},
                ),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders=_PLACEHOLDERS,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            info, errors = await self._claim(entry.data[CONF_CLOUD_URL], user_input[CONF_CODE])
            if info:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_TOKEN: info.token,
                        CONF_TOKEN_EXPIRES_AT: info.expires_at.isoformat(),
                    },
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_CODE): str}),
            errors=errors,
            description_placeholders=_PLACEHOLDERS,
        )
