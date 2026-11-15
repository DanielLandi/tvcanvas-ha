# TVCanvas for Home Assistant

The Home Assistant companion of [TVCanvas](https://tvcanvas.app), the iPhone app that puts
art, photos and live dashboards on your TV. It keeps your TVs showing your TVCanvas pins, day
and night, without your phone.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=DanielLandi&repository=tvcanvas-ha&category=integration)
[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=tvcanvas)

## How it works

![The TVCanvas app on your iPhone saves your pin in TVCanvas Cloud. Home Assistant checks the cloud every minute and sends each image to your TV over your home network. Photos go from the phone straight to Home Assistant and never go through TVCanvas Cloud.](docs/images/how-it-works.svg)

- In the TVCanvas iPhone app you pin a board, a collection, webcams, a custom image or an
  art group to a TV, and switch on **Run on Home Assistant**.
- Home Assistant picks the pin up from TVCanvas Cloud and keeps the TV updated over your
  home network, even when your phone is off or away.
- Photos from your phone go straight to Home Assistant over your home Wi-Fi, never through
  the cloud.
- You still choose what plays in the app, and it shows when Home Assistant last updated
  each TV.

## What you need

- **Home Assistant 2026.9 or newer**, with internet access.
- **The TVCanvas iPhone app**, signed in, with Home Assistant support
  (Settings → Home Assistant).
- **A Samsung The Frame** (Art Mode; tested on the 2022 model, 2024 and newer are best
  effort) **or a Google Cast TV** (Chromecast, Google TV, TVs with Cast built in), on the same
  network as Home Assistant.

Board pins use credits, as on the phone (a board refreshed every 5 minutes costs about 5.8
credits a day). Collections, webcams, custom images and photos don't.

## Set up in 3 steps

![The 12-character code from the app: Home Assistant sends the first 8 characters to TVCanvas Cloud to connect to your account. The last 4 stay in your home and pair your phone for photos.](docs/images/setup-code.svg)

1. **Install.** Click the first button above, select **Download**, then restart Home
   Assistant. On 2026.11 or newer it opens Settings → Marketplace; on 2026.9 or 2026.10,
   install [HACS](https://www.hacs.xyz/docs/use/download/download/) first.
2. **Type the code.** In the app, go to **Settings → Home Assistant → Connect Home
   Assistant**. It shows a 12-character code like `K7QM-2XRA-P4NB`, valid for 10 minutes.
   Click the second button above (or **Settings → Devices & services → Add integration →
   TVCanvas**) and type the code. This also pairs your phone for photos.
3. **Done.** In the app, open a TV's pin and switch on **Run on Home Assistant**. Home
   Assistant takes over within a minute.

**Samsung Frame, first time only:** the TV asks you to allow *TVCanvas-HA*. Press **Allow**
with the remote within 60 seconds.

**Google Cast:** the TV loads images from Home Assistant, so set Home Assistant's local
address under **Settings → System → Network**.

No button, or installing by hand? See
[Installing without the button](docs/troubleshooting.md#installing-without-the-button).

## What you get

- **A device per TV** with a **Last update** sensor: when the last image was shown, and the
  latest status in its `last_error` attribute.
- **A TVCanvas device** with a **Paired phones** sensor: the phones that may send photos.
- **On a Frame**, images go to My Photos with no mat. With *Replace previous* on, Home
  Assistant deletes the image it showed before, never your own art.
- **It waits politely:** while a Frame shows TV, runs its own slideshow or is in standby,
  nothing is uploaded and no credits are spent.
- **It keeps going** when TVCanvas Cloud is down (boards wait; everything else carries on)
  and picks a TV up again as soon as it's back.

There are no switches or settings in Home Assistant: you choose everything in the app.
Collages, picture-in-picture and overlays are drawn by the phone, so Home Assistant shows
the images without them; Photos albums stay on the phone
([what each pin shows](docs/troubleshooting.md#what-each-pin-shows)).

## Privacy and security

- **Your photos stay home.** They go from your phone to Home Assistant over your home
  network only. They are part of Home Assistant's backups.
- **Home Assistant never sends photos or images to TVCanvas Cloud**, nor your TVs'
  addresses: only a check for new pins and a short status per refresh.
- **Images go only to TVs on your home network.** Home Assistant refuses any TV address
  that isn't private, and its image and photo addresses answer only your home network.
- **A TV that moves to a new address** is followed only after Home Assistant has checked
  it's the same TV, and a notification tells you.
- **A Frame in standby pauses:** no board renders, so no credits are spent.
- **Removing the integration** deletes its pins, Frame permissions, phone keys and photos.

Everything that is sent, fetched and kept: [docs/privacy.md](docs/privacy.md).

## Troubleshooting

Check the TV's **Last update** sensor: its `last_error` attribute holds the latest status.

| You see | What to do |
|---|---|
| `waiting: Watch TV` | Nothing. The Frame updates when you're back in Art Mode |
| `waiting: Art` | Stop the Frame's own slideshow, or pick TVCanvas mode in the app |
| `waiting: photos N` | Open the app on your home Wi-Fi; it sends the missing photos. If the app says photos aren't set up, remove this Home Assistant in the app and connect again |
| `TV unreachable: …` | Check the TV's power and network. If its address changed, Home Assistant follows it once it has checked it's the same TV, and a notification says so. A fixed IP address in your router avoids this |
| `TV did not load the image` or `no local URL a Cast TV can reach` | Set Home Assistant's local address under Settings → System → Network |
| `out of credits` | Top up in the app. Boards retry every 30 minutes |
| *TVCanvas Cloud didn't accept this code* | Get a new code in the app (it works once, for 10 minutes) |
| *Could not reach TVCanvas Cloud* or *This server doesn't accept Home Assistant codes* | Check Home Assistant's internet connection, and that the server URL under **Advanced** is `https://api.tvcanvas.app` |
| Home Assistant asks to reconnect TVCanvas | Select **Reconfigure** and enter a new code from the app. Your pins stay |

Every status message, every message while connecting, Frame and Cast details, and how to
download diagnostics:
[docs/troubleshooting.md](docs/troubleshooting.md).

## Remove

1. In the app, switch **Run on Home Assistant** off for each TV. Home Assistant deletes its
   images from your Frames.
2. In Home Assistant: **Settings → Devices & services → TVCanvas → ⋮ → Delete**.
3. In the app, **Settings → Home Assistant**: swipe to disconnect this Home Assistant.
4. In HACS or the Marketplace: TVCanvas → ⋮ → **Remove**, then restart Home Assistant.

## Help

- Problems and ideas: [GitHub issues](https://github.com/DanielLandi/tvcanvas-ha/issues).
- Working on the integration: [docs/development.md](docs/development.md).

## Licence

[MIT](LICENSE).
