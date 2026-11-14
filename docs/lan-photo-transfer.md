# Phone → Home Assistant photo transfer over the LAN (feasibility note)

Status: **decided 2026-11-09, being built — contract in [`lan-api.md`](lan-api.md)**. Written 2026-11-08 with the group-pins
work (HA runs a group's boards, webcams and links; images stored on the phone are
skipped). Question from the owner: could the phone hand those images to HA directly
over the home network instead of through TVCanvas Cloud?

**Short answer: yes, it is feasible without the cloud ever carrying an image.
Recommendation: yes, but as its own milestone (about 1–1.5 weeks across iOS and
HA), and only after group pins without photos have run on HA for a while.** The
open decision for the owner is in [Decision needed](#decision-needed).

## What already exists

- HA already runs an HTTP server on the LAN, and this integration already
  registers a view on it: `display_view.py` serves Cast images at
  `/api/tvcanvas/image/{name}` (no HA login, 128-bit random name; since #15 it answers
  the home network only, with the `/api/tvcanvas/lan/*` check). An
  upload view is the same mechanism with a `post` handler.
- HA advertises itself over Bonjour as `_home-assistant._tcp` (its `zeroconf`
  integration, on by default), with `base_url`/`internal_url` and a `uuid` in
  the TXT record.
- The app already has the local-network permission and `NSAllowsLocalNetworking`,
  so plain `http://192.168.x.x:8123` works from the phone without ATS exceptions.
- The phone already resizes and JPEG-encodes images for the TVs, so what it would
  push is a ≤ 3840×2160 JPEG (typically 1–3 MB), not the original.

## Options

| | How the phone finds HA | How the phone proves it is allowed | Cloud change |
|---|---|---|---|
| **A. Bonjour + LAN pairing code** (preferred) | Browse `_home-assistant._tcp`; fall back to a URL the user types (Settings → Home Assistant) | HA shows a 6-digit code (config-flow option or a persistent notification); the phone POSTs it once to `/api/tvcanvas/lan/pair` and gets a 256-bit LAN key, stored in the Keychain | None for auth. The spec still needs a new group item kind (below) |
| B. Bonjour + key relayed in the spec | Same | The phone writes a random `lan_key` into the spec; the cloud relays it to HA like any other spec field | Spec field + validator; the cloud sees the key (it already holds the HA token, so no new trust) |
| C. HA reports its LAN URL to the cloud | HA sends `internal_url` in `/ha/agent/status`; the phone reads it from `GET /ha/sync` | As A or B | New status field; also tells the cloud the user's LAN layout, which we've avoided so far |

Images never pass through the cloud in any option. What the spec does need in
every option is a way to name the pushed images, so HA knows which ones belong
to the group and in what order:

```json
{ "type": "local", "sha256": "<64 hex>", "name": "IMG_0042" }
```

Content addressing makes the push idempotent and resumable: the phone asks HA
which hashes it already has (`GET /api/tvcanvas/lan/have`), pushes only the
missing ones (`PUT /api/tvcanvas/lan/image/{sha256}`, body verified against the
hash), and HA skips a `local` item whose bytes it doesn't hold yet (counted as
"waiting for the phone" in the status, never an error). This needs a small cloud
validator + contract change (a fifth item kind, hash-shaped, no URL), which is
metadata only. If "no cloud change at all" is a hard rule, HA would instead keep
the list per TV from a LAN manifest the phone pushes, and merge it with the
spec's items. That is doable but splits one pin's truth across two channels;
not recommended.

## What happens when…

- **The phone is away from home.** Nothing to push; HA keeps showing what it
  already has. A group edited while away is pushed the next time the app is open
  on the home network (foreground, or a `BGProcessingTask` that needs Wi-Fi).
  Until then HA skips the new `local` items and shows the rest.
- **Many photos.** 200 images ≈ 400 MB. Cap per TV (e.g. 300 images or 1 GB) and
  push in the foreground with a progress bar; a background push of hundreds of MB
  is not reliable on iOS.
- **Take back / unpin.** HA deletes that TV's images once the spec is gone (same
  path as today's `release()`), and garbage-collects hashes no spec references.
- **HA restarts or reinstalls.** Images live under `/config/tvcanvas/images/`
  (survives restarts); a reinstall loses them and the phone re-pushes on its next
  visit, because `have` comes back empty.

## Risks

- **Exposure.** HA's port is often reachable from the internet (Nabu Casa,
  port forwarding, reverse proxies). The upload endpoint must be authenticated
  (LAN key, constant-time compare), size-capped per request and per TV,
  rate-limited, and accept only JPEG/PNG that decodes under the existing pixel
  cap. Option A's pairing endpoint must lock out after a few wrong codes.
- **Discovery gaps.** mDNS doesn't cross SSIDs/VLANs (a home with a main and an IoT
  SSID is exactly this case), and some HA installs use HTTPS with a
  private cert or only a hostname. A typed-in URL fallback is required, plus
  certificate pinning to what the user accepted on first use for self-signed
  HTTPS.
- **Privacy and backups.** Personal photos now live on the HA box and in every
  HA backup (including Nabu Casa cloud backups). Say so in the app, and offer an
  "exclude from backups" location if HA allows it (`/media` is backed up
  separately).
- **Two copies of state.** If HA is reset or its disk fills, the TV quietly shows
  fewer images; the status line must say "N photos waiting for your phone".
- **Frames don't need it.** A Frame can already store a group's photos and run
  its own slideshow (the phone's Samsung upload path). The LAN transfer mostly
  helps Cast TVs and mixed groups.

## Effort (rough)

| Piece | Estimate |
|---|---|
| HA: LAN pair view, upload/have views, storage + GC, `local` items in the rotation, tests | 2–3 days |
| iOS: Bonjour browse + manual URL, Keychain key, push queue with resume + progress, status copy, l10n | 3–4 days |
| Cloud: `local` item kind in `ha_sync/spec.py` + contract doc + tests | 0.5 day |
| End-to-end on real TVs (Frame + Cast), split-SSID and HTTPS cases | 1 day |

## Decision needed

1. Is a **metadata-only** cloud change (the `local` item kind: a hash and a
   name, never image bytes) acceptable, or must the cloud not change at all?
   The note recommends the metadata-only change.
2. Should Frames use HA for photos at all, or keep using the Frame's own
   slideshow for photo-only groups and send only mixed groups through HA?
