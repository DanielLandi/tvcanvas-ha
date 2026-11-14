from __future__ import annotations

import json
from pathlib import Path

MANIFEST = Path(__file__).parents[1] / "custom_components" / "tvcanvas" / "manifest.json"


def test_manifest_shape() -> None:
    data = json.loads(MANIFEST.read_text())
    assert data["domain"] == "tvcanvas"
    assert data["config_flow"] is True
    assert data["single_config_entry"] is True
    assert data["iot_class"] == "cloud_polling"
    assert data["dependencies"] == ["http"]
    assert sorted(data["loggers"]) == ["pychromecast", "samsungtvws"]


def test_requirements_stay_compatible_with_ha_core() -> None:
    """Floors, not exact pins: core's cast/samsungtv integrations share these
    packages, and an exact pin breaks (or downgrades them) on HA's next bump.
    Pillow ships with core and is not listed at all."""
    reqs = json.loads(MANIFEST.read_text())["requirements"]
    assert sorted(reqs) == sorted(["samsungtvws>=3.0.5", "PyChromecast>=14.0.10"])
    assert not any(r.lower().startswith("pillow") for r in reqs)


def test_english_translation_matches_strings() -> None:
    """HA loads translations/en.json for a custom integration; strings.json is the source."""
    folder = MANIFEST.parent
    strings = json.loads((folder / "strings.json").read_text())
    assert json.loads((folder / "translations" / "en.json").read_text()) == strings


def test_no_strings_for_the_retired_pairing_button() -> None:
    """The "Pair a phone for photos" button is gone (#21); so are its strings."""
    strings = json.loads((MANIFEST.parent / "strings.json").read_text())
    assert set(strings["entity"]) == {"sensor"}
    assert "pair_phone" not in json.dumps(strings)
    assert not (MANIFEST.parent / "button.py").exists()


def test_every_config_flow_error_has_a_translation() -> None:
    import re

    from custom_components.tvcanvas.config_flow import _CLAIM_ERRORS

    errors = json.loads((MANIFEST.parent / "strings.json").read_text())["config"]["error"]
    flow = (MANIFEST.parent / "config_flow.py").read_text()
    used = set(re.findall(r'"base": "(\w+)"|CONF_CODE: "(\w+)"|return "(\w+)"', flow))
    keys = {key for group in used for key in group if key} | {k for _, k in _CLAIM_ERRORS}
    assert {"code_format", "url_not_base", "wrong_server", "too_many_attempts"} <= keys
    assert keys <= set(errors)


def test_licence_metadata_matches_the_licence_file() -> None:
    """HACS needs an OSI licence GitHub can detect; pyproject must name the same one."""
    import tomllib

    root = MANIFEST.parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    first_line = (root / "LICENSE").read_text().splitlines()[0]
    spdx_titles = {"MIT": "MIT License", "Apache-2.0": "Apache License"}
    assert project["license"] in spdx_titles
    assert first_line.strip().startswith(spdx_titles[project["license"]])
    assert project["license-files"] == ["LICENSE"]
