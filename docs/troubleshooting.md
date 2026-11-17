# Troubleshooting and reference

The [README](../README.md) covers setup and the most common problems. This page has every
status message and the details behind them.

- [Every status message](#every-status-message)
- [Connecting and reconnecting](#connecting-and-reconnecting)
- [Samsung Frame](#samsung-frame)
- [Google Cast](#google-cast)
- [Photos from your phone](#photos-from-your-phone)
- [When something is down](#when-something-is-down)
- [What each pin shows](#what-each-pin-shows)
- [Entities](#entities)
- [Installing without the button](#installing-without-the-button)
- [Diagnostics](#diagnostics)

## Every status message

Look at the TV's **Last update** sensor. Its `last_error` attribute holds the latest status.
The app shows only the short form.

| Status | What it means | What to do |
|---|---|---|
| `waiting: Watch TV` | The Frame is showing TV or an app | Nothing. It updates when you return to Art Mode |
| `waiting: Art` | The Frame is running its own slideshow | Stop the slideshow on the TV, or pick TVCanvas mode in the app |
| Frame in standby (`tv_state: standby`, `last_error` empty) | The Frame was switched off with a long press on power: the screen is dark but the TV is still on the network. Home Assistant pauses until it's back on: no board renders (so no credits), no uploads. It checks every 30 seconds and sends a fresh image as soon as the Frame is back on in Art Mode (switched on as a TV, it waits as for `waiting: Watch TV`). The app shows the TV as up to date meanwhile | Nothing |
| `waiting: photos N` | N photos of the group are still on the phone | Open the app on your home network; it sends them. If the app says photos aren't set up, remove this Home Assistant in the app and connect again on your home Wi-Fi |
| `TV unreachable: …` | The TV is unplugged or off the network, or its IP address changed. (A Frame in standby still answers.) Home Assistant looks for a Frame or a Cast TV whose address changed, at most every 10 minutes, and follows it once it has checked it is the same TV (a Cast TV only after Home Assistant has shown it an image once); a notification says where it went, or why it was not followed | Check the TV's power and network. Give the TV a fixed IP address in your router; if it changed, update the TV in the app |
| `tv host is not on the home network` | The TV's address in the pin is not a private or link-local address (`10.x`, `172.16–31.x`, `192.168.x`, `169.254.x`, IPv6 `fc00::/7` or `fe80::/10`). Home Assistant never connects to such an address: it would send your images, and your phone's photos, outside your home | Give the TV an address on your home network and update the TV in the app |
| `TV did not show the image: …` | The Frame stored the image but didn't display it | Usually passes on the next refresh. Home Assistant cleans up the stored image |
| `TV did not load the image: …` | The Cast device couldn't open the image | Check that the TV can reach Home Assistant's local address |
| `Home Assistant has no local URL a Cast TV can reach. Set one in Settings → System → Network` | No local address is set | Set one, as the message says |
| `out of credits` | TVCanvas Cloud refused a board render | Top up in the app. Boards retry every 30 minutes |
| `… answered 404`, `… is not an image`, `… is larger than 25 MB`, `… resolves to a non-public address`, `no usable images (N tried)`, `no usable image in this batch`, `undecodable image: …`, `cloud reply too large …`, `board image rejected`, `cloud unreachable` (the app shows `image fetch failed`) | The source didn't give a usable image | Open the URL or webcam in a browser. Images must be public `https://` addresses, at most 25 MB, in JPEG, PNG, WebP or GIF (a server that sends the file as a generic download, `application/octet-stream`, is fine when the file itself is one of these); a name that points into your own network (a camera on your LAN) is refused. Collections and webcams move on to the next item by themselves |
| `custom image does not match the orientation filter` | The image is portrait and the pin wants landscape, or the other way round | Change the image or the pin's orientation |
| `internal error` | A bug | Open an issue with the [diagnostics file](#diagnostics) and the log lines that mention TVCanvas |

## Connecting and reconnecting

- **The code:** in the app, **Settings → Home Assistant → Connect Home Assistant**. It shows a
  code like `K7QM-2XRA-P4NB` (older versions of the app show 8 characters, like `ABCD-2345`).
  It's valid for 10 minutes and works once. Type it with or without the dashes; case doesn't
  matter, and `O`, `I` and `L` are read as `0`, `1` and `1`.
- **One code does both:** it connects Home Assistant to your TVCanvas account and lets your
  phone pair with Home Assistant over your home network during the next 10 minutes.
  TVCanvas Cloud never learns what pairs your phone.
- Leave the **Advanced** section of the setup form alone. It's only for testing against a
  development server.

If connecting fails, the form says why:

| Home Assistant says | What to do |
|---|---|
| *That isn't a TVCanvas code* | Type the 12 characters the app shows (for example `K7QM-2XRA-P4NB`); dashes and spaces don't matter |
| *TVCanvas Cloud didn't accept this code* | The code is wrong, already used or older than 10 minutes. Get a new one in the app |
| *Could not reach TVCanvas Cloud* | Check Home Assistant's internet connection, and the server URL under **Advanced** (normally `https://api.tvcanvas.app`) |
| *This server doesn't accept Home Assistant codes* | The server URL under **Advanced** points somewhere else. Put back `https://api.tvcanvas.app` |
| *Too many attempts* | Wait a few minutes, then try again |
| *TVCanvas Cloud has a problem right now* | Try again in a few minutes |
| *Enter only the server's address, without a path* / *…starting with https://* | Under **Advanced**, enter just `https://` and the server's name, like `https://api.tvcanvas.app` |

The log (**Settings → System → Logs**, search TVCanvas) says what the server answered.

- **Home Assistant asks to reconnect TVCanvas** (Settings → Devices & services): TVCanvas
  Cloud no longer accepts this Home Assistant, for example because you disconnected it in the
  app. Select **Reconfigure**, get a new code in the app and enter it. Your pins stay as they
  were.
- **Nothing happens after switching on Run on Home Assistant:** give it a minute, then check
  **Settings → System → Logs** for lines mentioning TVCanvas. *TVCanvas: skipping TV …* means
  the app sent a pin this version can't run: update the integration.

## Samsung Frame

**First time only**, the TV asks you to allow a new device. Home Assistant shows the
notification *Approve TVCanvas-HA on your TV* while it waits (up to 60 seconds). Press
**Allow** with the TV remote. Home Assistant remembers the permission. If the TV's IP address
changes, the TV asks once more.

Home Assistant puts each image into the Frame's **My Photos** with no mat and shows it in Art
Mode, fitted to 3840 × 2160 and cropped from the centre. When the pin has **Replace
previous** on (the default), it deletes the image it showed before, so My Photos doesn't fill
up. **It only deletes images it uploaded itself, never your own art.** With **Replace
previous** off, every image stays in My Photos: Home Assistant logs a warning once
(`Replace previous is off for this pin`) and records which images are its own, deleting none.
A delete the TV refuses, or one cut off by a dropped connection, is logged once and tried
again on the next update.

**My Photos filled with copies?** Test builds of the TVCanvas app before November 2026
sent group pins (albums and board groups) to Home Assistant with Replace previous off, so a
fast group filled My Photos. To stop it, turn the pin's **Run on Home Assistant** off in the
app (the phone, which deletes its previous image, runs it instead). To remove only the copies,
[`scripts/frame_cleanup.py`](../scripts/frame_cleanup.py) (run from a clone of this
repository with its [test venv](development.md#setup)) lists My Photos and picks the images Home Assistant uploaded: the ids it recorded, and the images
dated just before a time its **Last update** sensor shows an image, at most one per update,
3840 × 2160 without a mat. It is a dry run unless you add `--apply`; the docstring at the
top of the script shows how to run it.

Like the phone, Home Assistant checks the Frame's mode before every refresh:

| The Frame is in | Home Assistant |
|---|---|
| **TVCanvas** (Art Mode on, the TV's own slideshow off) | Updates the image |
| **Watch TV** (Art Mode off: a channel or an app) | Waits. It downloads nothing and spends no credits. Status `waiting: Watch TV` |
| **Art** (Art Mode on, the TV's own slideshow running) | Waits. With **Replace previous** on it removes its images from My Photos so they don't join your slideshow. Status `waiting: Art` |
| **Standby** (switched off with a long press on power) | Pauses: no board renders, no uploads. Sends a fresh image when the Frame is back on in Art Mode |

If a Frame's firmware doesn't report its mode, Home Assistant treats it as TVCanvas mode.
Tested on the 2022 Frame; newer models (2024+) changed the Art Mode interface and are best
effort.

If an upload is interrupted (for example the TV drops off Wi-Fi), the TV can keep the image
without telling Home Assistant. On the next connection Home Assistant looks for exactly one
new image from that moment and cleans it up. If you uploaded something at the same moment, it
can't tell them apart, so it leaves both and logs a warning.

## Google Cast

Home Assistant serves the image itself and asks the TV to show it with Google's standard
media receiver. The TV has to reach Home Assistant's local address, so set one under
**Settings → System → Network** (an `http://` address on your home network). Each image sits
at a random, unguessable address, and Home Assistant keeps only the last three images per TV,
in memory. That address answers only devices on your home network: a request from the
internet (port forwarding, a reverse proxy, Home Assistant Cloud's remote access) gets "not
found". Behind a reverse proxy, set Home Assistant's `use_x_forwarded_for` and
`trusted_proxies` so it sees the TV's real address.

## Photos from your phone

An art group can include photos stored on your phone. The phone sends them to Home Assistant
directly over your home network, never through TVCanvas Cloud.

Connecting with the 12-character code (see [Connecting](#connecting-and-reconnecting)) pairs
the phone too: it finds Home Assistant on your home Wi-Fi within the next 10 minutes, and
there is nothing more to do. There is no separate pairing step and no code to type in Home
Assistant. If the phone missed those 10 minutes (it wasn't on your home Wi-Fi, for example),
the app says photos aren't set up: remove this Home Assistant in the app (**Settings → Home
Assistant**, swipe) and connect again with a new code.

The app then sends a group's photos whenever it is open on your home network. Until a photo
arrives, Home Assistant skips it and shows the rest of the group; the status says
`waiting: photos N`. A group of only photos keeps the TV's current image until the first one
arrives.

- Photos are stored in `config/tvcanvas/images/`, at most 1 GB and 400 photos in all. A photo
  no pin uses any more is deleted about 10 minutes later.
- **Photos are included in Home Assistant's backups**, including cloud backups (for example
  Home Assistant Cloud).
- Up to 5 phones can be paired; pairing a sixth unpairs the oldest. The **Paired phones**
  sensor lists their names and when they were paired.
- Five wrong codes cancel the code (connect again for a new one); five wrong codes from one
  device block that device for 5 minutes. Connecting again doesn't lift a block; only the 5
  minutes do.
- Only devices on your home network can send photos or pair. Behind a reverse proxy, set
  `use_x_forwarded_for` and `trusted_proxies` so Home Assistant sees the phone's real address.

## When something is down

- **TVCanvas Cloud unreachable:** Home Assistant keeps running your last pins, even after a
  restart. Collections, webcams and custom images keep working; boards wait for the cloud.
- **TV off or unreachable:** Home Assistant retries less and less often, down to every 15
  minutes. It checks quietly in between and refreshes as soon as the TV is back.
- **Out of credits:** board pins pause and retry every 30 minutes, or as soon as you change
  the pin. Other pins keep running.
- **Disconnected in the app:** every TV stops and Home Assistant asks you to
  [reconnect](#connecting-and-reconnecting).
- **You take a TV back** (Run on Home Assistant off in the app): Home Assistant stops that TV
  within a minute. On a Frame with **Replace previous** on, it deletes its images from My
  Photos in the background. If the TV is off at that moment, the log names the images to
  delete by hand.

## What each pin shows

Every minute Home Assistant checks TVCanvas Cloud for changes to your pins. Each TV then
refreshes at its pin's own interval (1 minute to 24 hours).

| Pin type in the app | On Home Assistant |
|---|---|
| Board (weather, flights, …) | Yes, rendered by TVCanvas Cloud. Uses credits, as on the phone: refreshed every 5 minutes, about 5.8 credits a day |
| Collection | Yes, in order or shuffled, with the pin's orientation filter |
| Webcams | Yes, one camera per refresh, in turn |
| Custom image URL | Yes |
| Art group, including photos from your phone | Yes (see [Photos from your phone](#photos-from-your-phone)) |
| Photos albums | No: the photos live on your phone |
| Collages, picture-in-picture, photo-info overlays, abstract art | No: the phone draws these. Home Assistant shows the pin's images without them |

## Entities

Each TV that Home Assistant runs becomes a device (manufacturer TVCanvas, model *Samsung
Frame* or *Google Cast*) with one sensor:

| Entity | State | Attributes |
|---|---|---|
| **Last update** (`sensor.<tv_name>_last_update`) | When the last image was shown successfully | `kind` (`frame` or `cast`), `source` (`board`, `collection`, `webcam`, `custom_url`, `group`), `last_error` (the latest status; empty after a success), `last_tick` (the last refresh attempt), `boards_paused_until`, `tv_state` (a Frame's power: `on`, `standby`; empty when unknown, and always for Cast) |

The integration also has a device of its own, **TVCanvas**, with:

| Entity | What it does |
|---|---|
| **Paired phones** (sensor) | How many phones may send photos; attribute `phones` lists each one's `name` and `paired_at` (never its key) |

The sensor is *unavailable* while Home Assistant doesn't run that TV. Once you've taken a TV
back for good, you can delete its device: open the device, ⋮ → **Delete**.

## Installing without the button

- **HACS:** open ⋮ → **Custom repositories**, add `https://github.com/DanielLandi/tvcanvas-ha`
  with type **Integration**, then search for TVCanvas. On Home Assistant 2026.9 or 2026.10,
  install [HACS](https://www.hacs.xyz/docs/use/download/download/) first; from 2026.11 it is
  built in as Settings → **Marketplace**.
- **Manually:** download the latest release from the
  [Releases page](https://github.com/DanielLandi/tvcanvas-ha/releases), copy the
  `custom_components/tvcanvas` folder into your Home Assistant configuration folder (so you
  end up with `config/custom_components/tvcanvas/manifest.json`; the File editor or Samba
  apps can do this without a terminal), and restart Home Assistant. You'll repeat this for
  every update.

## Diagnostics

Settings → Devices & services → TVCanvas → ⋮ → **Download diagnostics**. The file leaves out
your token and your TVs' IP addresses; read it before you attach it to an
[issue](https://github.com/DanielLandi/tvcanvas-ha/issues) anyway.
