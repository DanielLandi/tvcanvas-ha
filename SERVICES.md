# External services: tvcanvas-ha

| Service | Used for | Keys / config | Where it lives |
|---|---|---|---|
| TVCanvas Cloud (`api.tvcanvas.app`, `sandbox.tvcanvas.app`) | Pairing, sync specs, board renders, proxied collection images | `fc_ha_` token obtained by pairing code | Each user's HA config entry (no secrets in this repo) |
| GitHub Actions | CI (`test`, `hassfest`, `hacs`) | none | `.github/workflows/ci.yml` |

No other services. The integration talks to TVs only on the user's LAN.
