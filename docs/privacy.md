# Privacy: what goes where

The [README](../README.md#privacy-and-security) has the short version. This page lists
everything the integration sends, fetches and keeps.

## To TVCanvas Cloud

- The pairing code, once, in exchange for a token for this Home Assistant (never what pairs
  your phone for photos: that stays in your home). The token renews itself before it expires; disconnecting in the app
  revokes it.
- A check for changed pins every minute.
- For each refresh, a status: the TV's TVCanvas id, success or failure, the short status the
  app shows, and the time. **Never** the detailed error, so never your TVs' IP addresses or
  other details of your network.
- For board pins, the board and its settings, so the cloud can render it (this uses
  credits). For collection images the app marked to go through TVCanvas, the image's id.

It doesn't send your phone's photos, camera images, the images shown, your Home Assistant
entities or anything else from Home Assistant.

TVCanvas Cloud stores the pins the app writes, including each TV's name and local IP
address, because Home Assistant needs them to find the TV. The TVCanvas app's own privacy
policy is at [tvcanvas.app/privacy](https://tvcanvas.app/privacy).

## From your phone to Home Assistant

Once paired, the phone sends the photos in your art groups directly to Home Assistant, over
your home network (as the same ≤ 3840 × 2160 JPEG it sends a TV). The phone proves it is
paired with a key Home Assistant stores only as a hash. The cloud only learns each photo's
fingerprint and name. Only devices on your home network can pair or send photos: requests
from the internet (port forwarding, a reverse proxy, Home Assistant Cloud's remote access)
get "not found".

## From Home Assistant to your TVs

The image to show, your phone's photos among them, and only to a TV on your home network.
TVCanvas Cloud tells Home Assistant each TV's address, so Home Assistant checks it itself: it
connects only to private and link-local addresses and reports
`tv host is not on the home network` for any other. A Frame can ask for the image at another
address, or pass the connection on; those addresses are checked the same way.

When a TV gets a new address from your router, Home Assistant follows it only after checking
it is the same TV, and a notification tells you where it went.

## What Home Assistant fetches itself

Webcam images, custom image URLs and the other collection images, from the addresses in your
pins. Only public `https://` addresses are allowed, on every redirect too, and a name must
resolve only to public internet addresses: Home Assistant won't fetch from your own network
for a pin. A `user:password@` in an address, and the part after `?` or `#` (often an API
key), are left out of error messages, logs, the sensor's attributes and diagnostics.

## What Home Assistant keeps

- In a private file in Home Assistant's `.storage` folder: your last pins (so it keeps
  working when the cloud is down), each TV's place in its rotation, the Frames' permission
  tokens and the ids of the images it uploaded.
- In a second private file: the paired phones' names and hashed keys.
- In `config/tvcanvas/images/`: the photos your phone sent. **They are part of Home
  Assistant's backups**, including cloud backups (for example Home Assistant Cloud).

Removing the integration deletes all of it.
