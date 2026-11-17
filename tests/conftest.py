from __future__ import annotations

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """A fresh config dir per test: phone photos land in <config>/tvcanvas/images."""
    return hass_tmp_config_dir


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let HA load custom_components/tvcanvas in every test."""
    yield


@pytest.fixture(autouse=True)
def fresh_showing_fallback():
    """A cloud's 422 to `showing` lasts until HA restarts (module state): reset per test."""
    from custom_components.tvcanvas import api

    api._NO_SHOWING.clear()
    yield
    api._NO_SHOWING.clear()


PUBLIC_IP = "93.184.215.14"


class FakeDNS:
    """Scripted DNS for image-fetch host names; no test resolves a real name.

    `dns.answers[host]` is a list of addresses, or a list of such lists that is used one
    lookup at a time (to simulate DNS rebinding). Unknown names resolve to PUBLIC_IP."""

    def __init__(self) -> None:
        self.answers: dict[str, list] = {}
        self.lookups: list[str] = []

    def addresses(self, host: str) -> list[str]:
        self.lookups.append(host)
        answer = self.answers.get(host, [PUBLIC_IP])
        if answer and isinstance(answer[0], list):
            return answer.pop(0) if len(answer) > 1 else answer[0]
        return answer


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    import socket

    dns = FakeDNS()

    class Resolver:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def resolve(self, host, port=0, family=socket.AF_INET):
            return [
                {
                    "hostname": host,
                    "host": addr,
                    "port": port,
                    "family": socket.AF_INET6 if ":" in addr else socket.AF_INET,
                    "proto": 0,
                    "flags": 0,
                }
                for addr in dns.addresses(host)
            ]

        async def close(self) -> None:
            pass

    monkeypatch.setattr("custom_components.tvcanvas.urlsafety.ThreadedResolver", Resolver)
    return dns
