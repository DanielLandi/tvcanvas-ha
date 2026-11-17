# Phone → Home Assistant LAN API (photos)

Status: **contract, being built** (2026-11-09). Design and rationale:
[`lan-photo-transfer.md`](lan-photo-transfer.md). The owner's decisions on 2026-11-09:

1. A metadata-only cloud change is fine: the sync spec gains a `local` group item
   (hash + name). Image bytes never pass through TVCanvas Cloud.
2. Photo-only groups on a Samsung Frame: **the user decides**, when handing the pin
   to Home Assistant (see "Frame prompt" below).
3. Out of credits in a mixed group: show what it can (already shipped in #5).
4. Copy "images stored on this phone" is fine.

Three repos build against this file. Change it here first, then the code.

**Trust model** (the owner's, 2026-11-09):

- A hacked TVCanvas Cloud must never be able to get at users' photos.
- Photos only ever go directly from the app to HA on the LAN, never through the cloud.
- The user is responsible for their own HA.

Everything below follows from these three. In particular, the cloud never learns
anything that lets it pair a phone with HA: the LAN pairing code is generated on the
phone and typed into HA by the user (the setup code, §3), and is never sent to the
cloud.

## 1. Cloud: the `local` group item (TVCanvas Cloud's sync spec validator and its HA sync contract)

A fourth item kind inside `source.group.items`:

```json
{ "type": "local", "sha256": "<64 lowercase hex>", "name": "IMG_0042" }
```

- Exactly these three keys. `sha256`: `^[0-9a-f]{64}$`. `name`: 1–256 chars, same
  rules as webcam/link names (it is shown in HA's status, never used as a path).
- Counts toward the existing 1–100 item cap. Order rules unchanged (phone order,
  seeded shuffle already applied; HA never reorders).
- A group may now consist only of `local` items.
- Spec `version` stays 1 (additive). An HA without `local` support rejects the spec
  and skips the TV, so **HA ships before iOS**, as with groups.

## 2. HA: what it does with `local` items

- Bytes live in `/config/tvcanvas/images/<sha256>.jpg` (HA config dir, survives
  restarts; it is in HA backups, and the app says so).
- In the rotation a `local` item whose file exists is shown like any other image
  (no orientation filter, as for groups). A missing file is **skipped, never an
  error**. While any are missing and nothing else failed, the tick reports
  `"ok": false, "error": "waiting: photos N"` (N = missing count) to
  `/ha/agent/status`, following the existing `waiting: …` convention: the cloud
  keeps `error` only when `ok` is false, and the app shows `waiting: …` as a state,
  not an error ("N photos waiting for your phone"). A real error that tick wins
  over the waiting report. An all-`local` group with none present keeps the TV's
  current image and reports the same.
- Garbage collection: after every spec refresh, delete files no current spec
  references, once they are older than 10 minutes (so a push that lands just
  before its spec isn't deleted).
- Caps: 1 GB total under `images/`, 400 files total. Over the cap, uploads get
  `507`.
- **Never upload (invariant).** HA never sends a `local` photo's bytes, or anything
  derived from them (a resized or re-encoded copy, a thumbnail), to TVCanvas Cloud
  or to any URL taken from a spec. The render path for a `local` item reads only
  `/config/tvcanvas/images/<sha256>.jpg` and hands the fitted JPEG only to the TV:
  a Frame over its local WebSocket, or a Cast TV, which fetches it from HA's own
  local URL. What the cloud hears about a `local` item is what it already sent
  (the hash and name in the spec) plus the per-TV status (`waiting: photos N`).
  `tests/test_invariants.py` enforces this.

## 3. HA: pairing a phone for photos

The phone trades a short **live code** once for a LAN key. At most one code is live
at a time, for **10 minutes**, single use. **The 12-character setup code is the only
way to open one** (the owner's decision, 2026-11-11, tvcanvas-ha#21: the 12-character
code pairs everything; there is no manual pairing and no second phone):

- When connecting HA to the cloud, the app shows a **12-character** code,
  `K7QM-2XRA-P4NB`:
  - the first 8 characters (`K7QM2XRA`) are the cloud pairing code from
    `POST /ha/pair-code`;
  - the last 4 (`P4NB`) are Crockford characters the **phone generated locally**
    (`CROCKFORD = 0123456789ABCDEFGHJKMNPQRSTVWXYZ`, uniformly random). The phone
    **never sends them to TVCanvas Cloud.**
- The user types the whole code into HA's config flow (first setup or reauth).
  HA normalises it (uppercase; `-` and spaces removed; `O`→`0`, `I`/`L`→`1`), and
  accepts exactly 8 or 12 Crockford characters; any other length, or another
  character, is "invalid code" before any request. HA sends **only the first 8** to
  `/ha/claim`. Only when the claim succeeds does HA open the last 4 as the live
  code, for 10 minutes from the claim, replacing any earlier live code. The phone
  then pairs by itself over the home network:
  `POST /api/tvcanvas/lan/pair {"code": "P4NB", …}`. The 4 characters live in HA's
  memory only: never in the config entry, `.storage`, logs or diagnostics.
- An 8-character code (an older app) connects HA as before and opens no LAN code.
- **No other way in.** Pre-release builds of this integration (before #21) had a
  `button` entity "Pair a phone for photos" that showed a 6-digit code in a
  persistent notification. It is removed (no released app ever typed that code); on
  upgrade, setup deletes its entity (unique id `<entry_id>_lan_pair`) from the
  entity registry.
- **If the phone missed the 10 minutes** (HA not found, the phone off the home
  Wi-Fi, the code killed by wrong guesses): the user removes the Home Assistant in
  the app and connects it again, which shows a new 12-character code (the reauth
  step takes one too).

- **Matching:** the `code` the phone sends is normalised the same way (uppercase;
  `-` and spaces removed; `O`→`0`, `I`/`L`→`1`; a non-ASCII string never matches),
  then compared with the live code in constant time. So `p4nb` and `P4-NB` match
  `P4NB`. The phone should send the code exactly as generated (`P4NB`) anyway.
- **Guessing limits:** 5 wrong codes against the live code invalidate it (the user
  connects again for a new one). Attempts while no code is live are refused (`403`)
  and don't count. Lockout is **per client IP**: 5 wrong codes from one address →
  that address gets `429` for 5 minutes. Opening a new setup code does **not** clear
  lockouts (whoever typed it into HA isn't known to be the one locked out); only
  the 5 minutes do. A stranger's lockout is their own address's, so it never blocks
  the owner's phone. A device on the home network can still burn a live code with 5
  wrong guesses the moment `info` says it is open; that is an accepted LAN risk (the
  owner connects again for a new code; tvcanvas-ha#17). Only a
  `Content-Type: application/json` body counts as a guess, so a web page open in a
  browser at home can't do it with a cross-site form or `text/plain` POST.
- **Why 4 characters is enough:** 32⁴ ≈ 1 M codes, with the 5-guess kill and per-IP
  lockout, open only for 10 minutes after the user has just connected HA.
- **Cloud contract unchanged:** `/ha/pair-code` and `/ha/claim` still use the
  8-character code; the 4-character suffix never leaves the home. An HA without this
  change rejects a 12-character code ("invalid code"), so **HA ships before the app**
  shows 12-character codes.
- **Keys:** 32 random bytes, base64url, stored hashed (SHA-256) in the
  integration's `Store`. Up to 5 paired phones; a 6th pairing evicts the oldest.
  Each key has the phone's `device_name` and `paired_at`. Compare in constant time.
- Unpair from HA: the keys are listed as an attribute on a sensor or in
  diagnostics (names + dates only, never keys); removing the config entry deletes
  all keys and all images.

## 4. HA: HTTP endpoints (on HA's own server, `requires_auth = False`, own auth)

**Home network only.** HA's port is often reachable from the internet (Nabu Casa,
port forwarding, reverse proxies). Every `/api/tvcanvas/lan/*` endpoint answers
only clients whose address (as HA resolves it, honouring its `http:
use_x_forwarded_for`/`trusted_proxies` config) is local per
`homeassistant.util.network.is_local`; anyone else gets `404` with an empty body.
A request through Home Assistant Cloud's remote UI (`helpers.network.is_cloud_connection`)
reaches HA from a local tunnel address, so HA refuses it explicitly the same way.

Base: whatever URL the phone reached HA on (`http://homeassistant.local:8123`,
an IP, or a typed `https://…`). All JSON is UTF-8. Every endpoint except `pair`
needs the header `X-TVCanvas-LAN-Key: <key>`; a missing/unknown key → `401`
`{"error":"unauthorized"}`. Unknown fields in JSON bodies are ignored. Every
response carries `Cache-Control: no-store`. While the TVCanvas integration isn't
set up (or is reloading) every endpoint answers `404`, as an HA without it does.

### `GET /api/tvcanvas/lan/info` (key optional)

Lets the phone check it found a TVCanvas HA before asking for a code.

Without a key: `{ "api": 1, "pairing_open": true }`. With a valid key, also
`"name": "Home"` (HA's location name) and `"instance_id": "<HA's installation uuid>"`.

`instance_id` is HA's own installation id (`homeassistant.helpers.instance_id`,
also the `uuid` in its Bonjour TXT record). The phone stores the one the pair
response returns, to recognise the same HA when its address changes. Trust comes
from the code, which only someone looking at this HA can read; the cloud does not
know HA's LAN identity. `pairing_open` is true while a setup code is live (§3).

### `POST /api/tvcanvas/lan/pair` (no key)

Body `{"code": "P4NB", "device_name": "Alex's iPhone"}` (the setup code's last 4
characters; matching rules in §3) →
`200 {"key": "<base64url>", "instance_id": "…", "name": "Home"}`.
Wrong code, or no live code, `403 {"error":"bad_code"}`; this client locked out `429 {"error":"locked","retry_after": <s>}`.
Body > 4 KB → `413`; a body that isn't a JSON object, or whose `Content-Type` isn't
`application/json` (parameters such as `charset` are fine) → `400 {"error":"bad_request"}`
(neither counts as a wrong code). A missing or non-string `device_name` becomes
`"Phone"`; it is cut to 64 characters, control characters removed.

### `GET /api/tvcanvas/lan/have`

`200 {"sha256": ["<hex>", …], "bytes_used": 123, "bytes_cap": 1073741824, "files_cap": 400}`

### `PUT /api/tvcanvas/lan/image/{sha256}`

Body: the JPEG bytes (`Content-Type: image/jpeg`), ≤ 10 MB.
HA verifies SHA-256(body) == `{sha256}` (else `422 {"error":"hash_mismatch"}`),
that it decodes as JPEG/PNG of at most 16 MP (else `415`; the phone sends ≤ 8.3 MP), and the
caps (`507 {"error":"full"}`). Writes atomically (temp file + rename).
`201` new, `200` already had it, both `{"sha256": "<hex>"}`. Rate limit: 120
PUTs/minute per key (`429 {"error":"rate_limited","retry_after":60}`).
HA decodes one image at a time (TV ticks and uploads share it, tvcanvas-ha#16). A key
may have 2 PUTs in progress; a third gets `429 {"error":"rate_limited","retry_after":10}`
before its body is read. A body that hasn't fully arrived within 30 s gets
`408 {"error":"timeout"}`. Once read, an upload that can't start decoding within 20 s,
or that finds 4 others already waiting to decode, gets the same `429`.
HA implementation details the phone can rely on:

- `{sha256}` that isn't exactly 64 lowercase hex → `400 {"error":"bad_hash"}`.
- Body > 10 MB (declared or streamed) → `413 {"error":"too_large"}`.
- `415` body: `{"error":"unsupported_image"}`. `Content-Type` isn't checked; the
  bytes are.
- When the store is already at 400 files, `507` comes before the body is read.
- A `PUT` of a photo HA already has restarts its 10-minute GC clock.

### `POST /api/tvcanvas/lan/prove` (no key: HA proves it holds the key)

The phone must never send its key, or any photo, to an address it hasn't
verified. Before using an address other than the one it paired on (a Bonjour
result after the IP changed, a typed new URL), it asks that host to prove it is
the HA that holds the key's hash, which only the real HA stores:

- `key_hash` = SHA-256 of the key's UTF-8 bytes (what HA already stores, as hex).
- `key_id` = first 16 hex chars of SHA-256 of the ASCII `key_hash` hex string.
  Both sides compute it; it reveals nothing usable.
- Request `{"key_id": "<16 hex>", "nonce": "<32 random bytes, base64url, no padding>"}`
  (body ≤ 4 KB).
- Response `200 {"proof": "…", "ip": "<HA's local IP>", "port": <HA's local port>}`
  where `ip`/`port` are the **local socket address the request arrived on**
  (`request.transport.get_extra_info("sockname")`, never the Host header; IPv6
  in compressed lowercase form without a `%zone`, IPv4-mapped IPv6 unwrapped to
  IPv4; no socket address → `500 {"error":"internal"}`) and
  `proof = base64url_nopad(HMAC-SHA256(key = bytes.fromhex(key_hash),
  msg = "tvcanvas-lan-prove-v1:" + nonce + ":" + ip + ":" + str(port)))`.
  Binding the address stops a relay: a fake host that forwards the request to the
  real HA gets a proof for HA's address, not its own. Unknown `key_id` →
  `403 {"error":"unknown_key"}`; bad body → `400`. Rate limit 30/minute per client
  address → `429`.
- The phone checks that `ip`/`port` equal the remote address and port it actually
  connected to (`URLSessionTaskMetrics` transaction `remoteAddress`/`remotePort`),
  then verifies the proof over those values.
- The phone compares the proof in constant time; on mismatch or any error it does
  not use that address, keeps the saved one, and shows "Home Assistant not found".
  On success it saves the new address. It never moves from `https` to `http`.
- The nonce must be exactly 43 base64url characters (no padding); anything else,
  or a `key_id` that isn't 16 lowercase hex, is `400 {"error":"bad_request"}`.
  Every call counts toward the rate limit, `429 {"error":"rate_limited","retry_after":60}`.

Test vector (HA's `tests/test_lan_api.py` checks it; iOS should too):

| | |
|---|---|
| key (bytes `00…1f`, base64url) | `AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8` |
| `key_hash` | `ea866a757e4c38babfa8127cbe9a409d3e1f93a00ff1488ff735fcf917afffd0` |
| `key_id` | `050d5bee93d6b596` |
| nonce (bytes `20…3f`, base64url) | `ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8` |
| `ip`, `port` | `192.168.1.10`, `8123` |
| HMAC msg | `tvcanvas-lan-prove-v1:ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8:192.168.1.10:8123` |
| `proof` | `nZiN6DaKoLJBq8-OdotogWt7ZuoGLGARYbhQdfe5FXQ` |
| `proof` for `ip` `fd00::10`, same port | `6JBksEVf6LvXwDAYtj89v-xbfIW-D18qj1BNUcm6G5s` |

### `DELETE /api/tvcanvas/lan/pair`

Forgets the calling key. `204`.

## 5. iOS

- **One step (§3), no separate photos setup:** "Connect Home
  Assistant" shows `XXXX-XXXX-YYYY`: the 8-character cloud code plus 4 Crockford
  characters generated on the phone (never sent to the cloud). Once the app sees HA
  connected, it finds HA by Bonjour `_home-assistant._tcp` (TXT
  `internal_url`/`base_url`), calls `info`, and posts `{"code": "YYYY", …}` to
  `pair` within 10 minutes, without asking for a code. It stores the key, base URL
  and `instance_id` in the Keychain (on a later Bonjour match by `instance_id`, the
  base URL is updated). Needs `NSBonjourServices` `_home-assistant._tcp` in
  `project.yml`.
- If that fails (expired, HA not found), the HA's row says photos aren't set up and
  to remove the HA and connect again on the home Wi-Fi. There is no manual pairing.
- Removing the HA in the app also unpairs photos (`DELETE …/lan/pair`, after
  `prove`).
- **Spec:** with photos-over-Wi-Fi paired, a group's phone images become `local`
  items in their place in the phone's order (same seeded shuffle). Without it they
  are skipped, as today ("images stored on this phone" notice).
- **Bytes:** the same ≤ 3840×2160 JPEG the phone sends TVs. Encode once and cache
  on disk keyed by asset id + modification date, so the hash is stable across
  launches. The hash is over exactly the bytes pushed.
- **Push:** when a handed-over group has `local` items and HA is reachable: `have`
  → `PUT` the missing ones, foreground, with progress in the HA status row;
  resumes next time the app is active on the home network. The spec is written
  to the cloud regardless; HA shows photos as they arrive.
- **Frame prompt (decision 2):** turning on Run on Home Assistant for a **Samsung
  Frame** pin whose group has **only phone images** asks:
  "Keep the Frame's own slideshow" (the photos stay on the TV, the phone's existing
  Frame upload path, HA not used) or "Let Home Assistant run it" (photos over Wi-Fi;
  if not paired yet, start pairing). Remember the answer per pin; it can be
  changed from the same switch. Mixed groups go through HA without asking.
- **Address changes:** the saved base URL is used as is. If it stops answering,
  the phone may look for the same `instance_id` (Bonjour TXT `uuid`) but must pass
  `prove` there before sending the key or photos (§4); discovered hosts must be
  local-network addresses; never https → http.
- **Several HAs found:** the pick list under the HA rows shows each discovered HA's
  host and address and accepts only local-network addresses from Bonjour.
- **Residual risk (accepted, documented):** HA's LAN port is plain HTTP on most
  installs, so someone already on the home network who can sniff or actively
  intercept traffic at pairing time could capture or relay the key. The key only
  allows pushing photos to that HA and listing hashes; it is not an HA login.
  Fixing this needs TLS pinning or a PAKE; out of scope for v1. (HA in a
  container behind port mapping reports its container address, so `prove` fails
  there; the phone then keeps the saved address and asks the user to remove the
  HA and connect again. Acceptable for v1.)
- **Privacy copy:** the connect screen says photos are copied to the Home Assistant box
  and are included in its backups.
