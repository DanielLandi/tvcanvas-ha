# Developing TVCanvas for Home Assistant

This page is for people working on the integration. To install and use it, see the
[README](../README.md).

## Setup

Python 3.14 and [uv](https://docs.astral.sh/uv/):

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements_test.txt
```

`requirements_test.txt` pins exact versions for the test venv
(`pytest-homeassistant-custom-component` brings the matching Home Assistant core).
`manifest.json` lists **floors**, never exact pins (`samsungtvws>=3.0.5`,
`PyChromecast>=14.0.10`): core's own `samsungtv` and `cast` integrations share these
packages, so an exact pin breaks on Home Assistant's next bump or downgrades them. Pillow
ships with core and is not listed. `tests/test_manifest.py` enforces both rules.

## The gate

```bash
scripts/check.sh
```

Ruff lint, ruff format check, then pytest. CI (`.github/workflows/ci.yml`) runs exactly this,
plus `hassfest` and the HACS action. The `hacs` job runs only in `DanielLandi/tvcanvas-ha`
(HACS validates public repositories only), so forks skip it.

Coverage is not part of the gate. To measure it, install `pytest-cov` into the venv only:

```bash
uv pip install --python .venv/bin/python pytest-cov
.venv/bin/python -m pytest -q --cov=custom_components/tvcanvas --cov-branch --cov-report=term-missing
```

## Local cloud stand-in

`scripts/fake_cloud.py` implements the HA side of the cloud contract with static specs and
generated test images, so the integration can run against real TVs without TVCanvas Cloud:

```bash
cp examples/specs.example.json scripts/specs.json     # set your TVs' IPs; the copy is gitignored
.venv/bin/python scripts/fake_cloud.py --specs scripts/specs.json [--port 8765] [--fail-board 401|402|429]
```

Specs are re-read on every sync, so editing `scripts/specs.json` is how you "change a pin on
the phone". The pairing code is `ABCD-2345`. In Home Assistant's **Add integration** dialog,
open the collapsed **Advanced** section and set the URL to `http://<your-mac-lan-ip>:8765`
(plain `http://` is accepted only for loopback and private-LAN hosts).

The contract itself is maintained with TVCanvas Cloud, which is not open source;
`fake_cloud.py` and `tests/test_fake_cloud.py` are its runnable reference here, and
[`docs/cloud-contract.md`](cloud-contract.md) records how HA reacts to each answer.

## Trying a build on your own Home Assistant

Copy `custom_components/tvcanvas` into your Home Assistant's `config/custom_components/`
(replacing the installed copy, if any), then run **Developer tools → YAML → Check
configuration** and restart Home Assistant. Users install through HACS or the Marketplace
instead (see the [README](../README.md)).

Before a release, test with a real Frame and a real Cast TV: pair by code, let a pin change
show on each TV, switch **Run on Home Assistant** off in the app and check the phone takes the
TV back, and look at the TV's "Last update" sensor and the integration's diagnostics for
errors.

## Layout

| Path | What it does |
|---|---|
| `custom_components/tvcanvas/__init__.py` | Entry setup and unload; registers the Cast image view once |
| `config_flow.py` | Pairing by code, re-authentication |
| `api.py` | TVCanvas Cloud client; maps HTTP answers to exceptions |
| `coordinator.py` | 60 s sync poll, token refresh, one runner per TV |
| `models.py` | Strict parser for the sync spec the phone writes |
| `runner.py` | One TV's loop: ready → fetch → fit → show → report |
| `sources.py` | Board, collection, webcam and custom-URL images |
| `urlsafety.py` | Direct image fetches reach public addresses only (resolved per hop, pinned at connect); TVs are on the home network only (`home_address`, `resolve_home_address`); URL redaction for logs, state and diagnostics |
| `imaging.py` | Cover-fit to 3840×2160 JPEG and the upload check; `DecodeGate`: one decode at a time for TV ticks and uploads (#16) |
| `display_frame.py` | Samsung Frame Art Mode: modes, upload, select, cleanup |
| `displays.py` | What a display is; `tv_address` / `home_literal`: the home-network check every display runs before it connects |
| `display_cast.py`, `display_view.py` | Google Cast via the Default Media Receiver; HA serves the image, to the home network only |
| `sensor.py`, `diagnostics.py` | The per-TV "Last update" sensor; redacted diagnostics |
| `storage.py` | Private store: last specs, rotation, Frame tokens and owned image ids |
| `brand/` | Icons Home Assistant 2026.9+ serves for a custom integration |

How photos travel from the phone to HA over the home network:
[`docs/lan-photo-transfer.md`](lan-photo-transfer.md) (design) and
[`docs/lan-api.md`](lan-api.md) (the API the phone calls).
