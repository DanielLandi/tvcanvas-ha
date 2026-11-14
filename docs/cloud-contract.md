# TVCanvas Cloud contract: how HA reacts

The HTTP contract between this integration and TVCanvas Cloud (pairing by code,
`/ha/claim`, `/ha/agent/*`, `/board/{type_id}`, `/collections/proxy/{id}`, and
the sync spec with its validation rules) is maintained with TVCanvas Cloud, which
is not open source. In this repository, `custom_components/tvcanvas/api.py` and
`models.py` are the client side of it, and `scripts/fake_cloud.py` (with
`tests/test_fake_cloud.py`) is a runnable stand-in for the cloud's HA endpoints.

A contract change lands on the cloud first, then here: the integration and
`scripts/fake_cloud.py` change together. This file keeps only how HA itself
reacts to the cloud's answers; it does not restate the contract.

## How HA reacts to cloud answers

Code: `custom_components/tvcanvas/api.py` (status → exception),
`coordinator.py` (sync, refresh), `runner.py` (per-TV tick), `sources.py`
(boards and images), `config_flow.py` (claim).

| Call | Answer | What HA does |
|---|---|---|
| `POST /ha/claim` | (not sent) | A code that isn't 8 or 12 Crockford characters after normalising is refused before any call: `code_format` on the code field ("enter the 12 characters the app shows"). Only the first 8 are ever sent; the last 4 of a 12-character code are the phone's LAN setup code and never leave the home ([`lan-api.md`](lan-api.md) §3) |
| `POST /ha/claim` | 400 | `invalid_code` on the code field: wrong, used or expired (codes last 10 minutes); get a new one in the app |
| `POST /ha/claim` | 401, 403, 404, 405, any 3xx, or a 2xx that isn't a token | `wrong_server`: "This server doesn't accept Home Assistant codes — check the server URL". `/ha/claim` needs no auth, so these mean the URL points elsewhere, or at a TVCanvas server without the HA endpoints (it answers 401 "Invalid or missing API key") |
| `POST /ha/claim` | 429 | `too_many_attempts`: wait a few minutes |
| `POST /ha/claim` | 5xx | `server_error`: try again later |
| `POST /ha/claim` | no answer (DNS, refused, TLS, timeout) | `cannot_connect`: check HA's internet connection and the server URL |
| `POST /ha/claim` | any other status | `unexpected_answer`: check the URL and that the integration is up to date |
| (config flow) | a server URL that isn't `https://` (or `http://` to a local host), or has a path, `?`, `#` or `user@` | `invalid_url` / `url_not_base`, before any call. Every claim failure is logged at INFO with the status, never the code |
| `GET /ha/agent/sync` | 304 | Keeps the stored specs and ETag |
| `GET /ha/agent/sync` | 429, 5xx, no answer | Keeps running the last stored specs; logs one warning until the cloud answers again |
| `POST /ha/agent/refresh` | 429, 5xx, no answer | Keeps the current token and tries again after 15 minutes, then hourly (the cloud allows 5 an hour; the token has 7 days of room); logs once |
| `GET /board/{type_id}`, `GET /collections/proxy/{id}` | 429 | Skips this tick without a status report or backoff |
| `GET /board/{type_id}` | 5xx, non-image, no answer | Reports `image fetch failed` |
| `GET /collections/proxy/{id}` | 400, 404, 451, 502, non-image | Skips that image and tries the next (at most 5 per tick); reports `image fetch failed` if none works |
| `GET /collections/proxy/{id}` | no answer | Asks the cloud once per tick, keeps showing direct images, and retries that proxied image first next tick |
| `GET /board/{type_id}` for a group item | 402 | A group with webcams or links tries the next item in the same tick and skips its boards for 30 min, showing the rest at the pin's interval; reports `out of credits` until a board renders again. A group of only boards pauses the whole TV for 30 min, as for a board pin |
| `GET /board/{type_id}` for a group item | 404, 5xx, non-image | Skips that item and tries the next (at most 5 per tick); reports `image fetch failed` if none works |
| `GET /board/{type_id}` for a group item | no answer | Asks the cloud once per tick, keeps showing the group's webcams and links, and retries that board first next tick |
| any call | 401, 403 | Stops every TV and starts re-authentication (see the cloud doc) |
| any call | 3xx other than 304 | Error; redirects to the cloud are never followed |
| any call | a body over 4 MB (JSON) or 25 MB (`/board`, `/collections/proxy`) | Refused as it streams, never held whole: a sync keeps the last stored specs, an image is `image fetch failed` |
| `GET /ha/agent/sync` | more than 20 specs | Runs the first 20 and logs one warning (the cloud allows 10 TVs) |

## HA-side status details

- HA sends the cloud only the generic category from the cloud doc's table; the
  full error (which can name LAN hosts or URLs) stays in the TV's sensor
  attributes in HA.
- `TV unreachable` backoff: `max(interval, min(900, 60 · 2^(failures − 1)))`
  seconds. While backing off, HA probes the TV's port (Frame 8002, Cast 8009)
  every interval and ticks as soon as a probe succeeds after one failed.
- A Frame whose firmware doesn't answer the Art Mode or slideshow queries counts
  as showing TVCanvas, so HA is never blocked by it.
- A Frame in standby (`PowerState: "standby"` at `http://<tv>:8001/api/v2/`,
  read over plain HTTP with a 3 s timeout before every tick, home addresses
  only) gets no board render, no proxied image and no upload, and no backoff. HA
  asks again every 30 s and, once the Frame says `on`, runs that same tick as
  usual: in Art Mode it renders and uploads at once, in Watch TV it waits. The contract has no standby status, so HA reports `ok: true`
  with no error when standby starts and then once per interval (not every 30 s):
  the phone shows no error and doesn't fall back to "hasn't checked in", though
  its `last_ok_at` moves. HA's own sensor keeps its value (the last image shown)
  and sets `tv_state: standby`. No PowerState in the answer (older firmware) runs
  the tick as before; no answer at all is left to the WebSocket, so an
  unreachable Frame takes the `TV unreachable` / follow path unchanged. Cast TVs
  have no reliable standby signal and are unchanged.
- A group (`source.type: "group"`) is stepped through one item per tick in
  the order the spec lists it; HA never reshuffles a group (the phone already
  applied its shuffle). The rotation restarts at the first item when the item
  list changes.
- Out of credits in a group with webcams or links ("show what it can"): the
  group's boards are skipped for 30 min without being asked (skipped boards
  don't count toward the 5 tries a tick), and its webcams and links keep
  rotating at the pin's interval. After the window the boards take their turn
  again; one more 402 restarts the window, and a board that renders ends it.
  Until then every tick reports `out of credits` to the cloud with `ok: false`
  (so the phone keeps its "Buy credits" line, but the cloud's `last_ok_at`
  doesn't move), even a tick whose links failed. In HA the sensor's value
  (last update) still moves, `last_error` says the boards are skipped, and the
  `boards_paused_until` attribute holds the end of the window (a past time
  means the boards are being retried). Any change to the group's source from
  the phone (items, order, names) ends the window, as editing a board pin
  does, so the next board's turn asks the cloud again. HA forgets the window
  on restart, as it does for a board pin.
- A group's `local` items are photos the phone pushed to HA over the LAN
  ([`lan-api.md`](lan-api.md)); the cloud only carries their hash and name. A
  missing photo is skipped without counting toward the 5 tries. While any are
  missing and nothing else failed, the tick reports `waiting: photos N` with
  `ok: false` (N = missing photos), like `waiting: Watch TV`; HA's sensor still
  records the update when something showed. A real error that tick (`image fetch
  failed`, `out of credits`) wins. A group with no photo present and nothing else
  to show keeps the TV's image and reports the same, without backoff. After every
  spec refresh HA deletes photos no stored spec references (parseable or not) once
  they are 10 minutes old.
- A spec HA can't parse is skipped for that TV with a warning in the HA log;
  the other TVs keep running.
- **A TV must be on the home network** (#14). The cloud names each TV's address
  and a TV is sent the phone's photos, so HA does not take the cloud's word for
  it. `tv.host` must be RFC 1918 private (`10/8`, `172.16/12`, `192.168/16`),
  link-local (`169.254/16`, `fe80::/10`) or IPv6 unique local (`fc00::/7`);
  an IPv4-mapped IPv6 address counts as its IPv4 address. Loopback, shared
  space (`100.64/10`: CGNAT, Tailscale), reserved ranges and every public
  address are refused. The parser still follows the cloud doc (an IP literal,
  not loopback, link-local, multicast or unspecified), so a link-local host
  never gets this far; a public one does. For a refused TV HA never pairs,
  connects, probes, reads a photo, fetches an image or spends a credit: every
  tick it only reports `tv host is not on the home network` with `ok: false`
  (in the sensor's `last_error` and to the cloud; **this string is not in the
  cloud doc's table yet**), logs one warning, and keeps running the other TVs.
  The check runs where the address is used (`displays.tv_address`, then again
  in the thread that opens the socket). A host name, should the contract ever
  allow one, is resolved there, refused if any answer is outside the home
  network, and connected to by the address that was checked.
- What answers at a Frame's address can still point HA elsewhere, so HA checks
  twice more, and for an address that is not on the home network reports the
  same category and records no lost upload: the websocket's real peer once it is
  connected (the websocket library follows HTTP redirects), before anything is
  asked of it; and the address the TV names for an upload (`conn_info`), which
  goes over a second socket, before that socket is opened. That second check
  hooks a private samsungtvws method (`_open_d2d_socket`, 3.0.5); if a release
  drops it, HA fails closed: every Frame upload is refused with a status that
  says so (same category), and an error is logged once.
- The Cast image view (`/api/tvcanvas/image/{name}`) answers only clients on the
  home network, with the check `/api/tvcanvas/lan/*` uses (#15): anyone else,
  and any request through Home Assistant Cloud's remote UI, gets the empty 404
  of an unknown name.
