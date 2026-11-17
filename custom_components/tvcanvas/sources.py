"""Where each tick's image comes from."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiohttp
import yarl

from .api import AuthError, CloudClient, CloudError, CloudUnreachable, OutOfCredits, RateLimited
from .const import (
    FETCH_TIMEOUT_SECONDS,
    MAX_ATTEMPTS_PER_TICK,
    MAX_IMAGE_BYTES,
    MAX_REDIRECTS,
)
from .imaging import ImageError
from .items import rotation_order
from .models import SyncSpec, public_https_url
from .photos import PhotoStore
from .urlsafety import check_resolves_public
from .urlsafety import redact as _scrub  # errors never carry URL credentials or queries

Fitter = Callable[[bytes, str | None], Awaitable[bytes | None]]
_PASSTHROUGH = (AuthError, OutOfCredits, RateLimited)


class SourceError(Exception):
    """This tick has no image to show."""


class WaitingForPhone(SourceError):
    """Nothing to show: every item left is a phone photo HA doesn't hold yet."""


@dataclass
class BoardCredits:
    """A tick's view of board credits, shared between the runner and the rotation.

    paused (in): a mixed group's boards got a 402 in the last 30 min, so skip them.
    hit (out): a group board answered 402 this tick and the group moved on.
    board_shown (out): this tick showed a board, so credits are back.
    """

    paused: bool = False
    hit: bool = False
    board_shown: bool = False


def runs_without_credits(spec: SyncSpec) -> bool:
    """A group with webcams, links or phone photos keeps showing them while its
    boards are out of credits."""
    src = spec.source
    return src.type == "group" and any(i.board is None for i in src.items)


def _validated(url: str | yarl.URL) -> yarl.URL:
    """Validate, and return the exact URL object that will be requested."""
    try:
        return yarl.URL(str(public_https_url(url)), encoded=True)
    except Exception as err:  # noqa: BLE001 - any validation failure refuses the hop
        raise SourceError(_scrub(f"{url}: {err}")) from err


async def _check_resolves_public(url: yarl.URL) -> None:
    try:
        await check_resolves_public(url.raw_host or "", url.port)
    except OSError as err:
        raise SourceError(_scrub(f"{url}: {err.strerror or err}")) from err


# Some publishers (GeoNet's volcano cams, for one) label real JPEGs as a generic binary
# download or send no type at all. Those are accepted only when the bytes are an image.
_UNLABELLED_TYPES = ("", "application/octet-stream", "binary/octet-stream")
_SNIFF_BYTES = 12


def _media_type(headers: Any) -> str:
    return headers.get("Content-Type", "").split(";", 1)[0].strip().lower()


def looks_like_image(data: bytes) -> bool:
    """True when *data* starts with JPEG, PNG, WebP or GIF magic bytes."""
    return (
        data.startswith(b"\xff\xd8\xff")
        or data.startswith(b"\x89PNG\r\n\x1a\n")
        or data.startswith((b"GIF87a", b"GIF89a"))
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
    )


def _declared_length(headers: Any) -> int:
    try:
        return int(headers.get("Content-Length", 0))
    except ValueError:
        return 0


async def fetch_image(session: aiohttp.ClientSession, url: str) -> bytes:
    """Fetch a public https image, validating the URL and every redirect hop.

    Every hop's host must resolve to public addresses only. *session* should come from
    `urlsafety.create_image_session`: its resolver repeats that check for the address it
    connects to, so a name that re-resolves to the LAN in between (DNS rebinding) is
    refused too.

    The answer must be labelled `image/*`, or be labelled as generic binary (or not at
    all) and start with JPEG, PNG, WebP or GIF magic bytes."""
    current = _validated(url)
    try:
        async with asyncio.timeout(FETCH_TIMEOUT_SECONDS):
            for _hop in range(MAX_REDIRECTS + 1):
                await _check_resolves_public(current)
                async with session.get(current, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location")
                        if not location:
                            raise SourceError(_scrub(f"{current} redirected without a Location"))
                        try:
                            nxt = current.join(yarl.URL(location))
                        except Exception as err:  # noqa: BLE001
                            raise SourceError(_scrub(f"{current}: bad redirect: {err}")) from err
                        current = _validated(nxt)
                        continue
                    if resp.status != 200:
                        raise SourceError(_scrub(f"{current} answered {resp.status}"))
                    media_type = _media_type(resp.headers)
                    sniff = not media_type.startswith("image/")
                    if sniff and media_type not in _UNLABELLED_TYPES:
                        raise SourceError(_scrub(f"{current} is not an image"))
                    if _declared_length(resp.headers) > MAX_IMAGE_BYTES:
                        raise SourceError(_scrub(f"{current} is larger than 25 MB"))
                    buf = bytearray()
                    async for chunk in resp.content.iter_chunked(65536):
                        buf += chunk
                        if len(buf) > MAX_IMAGE_BYTES:
                            raise SourceError(_scrub(f"{current} is larger than 25 MB"))
                        if sniff and len(buf) >= _SNIFF_BYTES:
                            if not looks_like_image(bytes(buf[:_SNIFF_BYTES])):
                                raise SourceError(_scrub(f"{current} is not an image"))
                            sniff = False
                    if sniff and not looks_like_image(bytes(buf)):
                        raise SourceError(_scrub(f"{current} is not an image"))
                    return bytes(buf)
    except (aiohttp.ClientError, TimeoutError) as err:
        raise SourceError(_scrub(f"{current}: {err}")) from err
    raise SourceError(_scrub(f"{current}: too many redirects"))


async def _fit(fit: Fitter, data: bytes, orientation: str | None) -> bytes | None:
    try:
        return await fit(data, orientation)
    except ImageError as err:
        raise SourceError(f"undecodable image: {err}") from err


async def next_image(
    spec: SyncSpec,
    rotation: dict[str, Any],
    client: CloudClient,
    session: aiohttp.ClientSession,
    fit: Fitter,
    credits: BoardCredits | None = None,
    photos: PhotoStore | None = None,
) -> tuple[bytes, str]:
    src = spec.source
    try:
        if src.type == "board":
            raw = await client.board(src.board.type_id, src.board.params)
            out = await _fit(fit, raw, None)
            if out is None:
                raise SourceError("board image rejected")
            if credits is not None:
                credits.board_shown = True
            return out, src.board.type_id
        if src.type == "custom_url":
            out = await _fit(fit, await fetch_image(session, src.custom_url), src.orientation)
            if out is None:
                raise SourceError("custom image does not match the orientation filter")
            return out, _scrub(src.custom_url)
        return await _rotate(spec, rotation, client, session, fit, credits, photos)
    except _PASSTHROUGH:
        raise
    except CloudError as err:
        raise SourceError(str(err)) from err


_Slot = tuple[Callable[[], Awaitable[bytes | None]], str, bool, str | None]


def _slots(spec, client, session, photos: PhotoStore | None) -> list[_Slot]:
    """Each rotation slot as (load, label, via_cloud, photo).

    via_cloud: the image comes from the cloud (a proxied collection image or a
    group's board), so a cloud outage skips it while direct images still show.
    photo: the sha256 of a group's phone photo; its load answers None while HA
    doesn't hold the file.
    """
    src = spec.source

    def direct(url: str) -> Callable[[], Awaitable[bytes]]:
        return lambda: fetch_image(session, url)

    def proxied(url: str) -> Callable[[], Awaitable[bytes]]:
        return lambda: client.proxied_image(url)

    def board(type_id: str, params: dict[str, str]) -> Callable[[], Awaitable[bytes]]:
        return lambda: client.board(type_id, params)

    def stored(sha: str) -> Callable[[], Awaitable[bytes | None]]:
        async def load() -> bytes | None:
            return await photos.async_read(sha) if photos is not None else None

        return load

    if src.type == "collection":
        return [
            (proxied(i.url) if i.proxied else direct(i.url), i.title, i.proxied, None)
            for i in src.images
        ]
    if src.type == "group":
        slots: list[_Slot] = []
        for g in src.items:
            if g.board is not None:
                slots.append((board(g.board.type_id, g.board.params), g.label, True, None))
            elif g.sha256 is not None:
                slots.append((stored(g.sha256), g.label, False, g.sha256))
            else:
                slots.append((direct(g.url), g.label, False, None))
        return slots
    return [(direct(w.url), w.name, False, None) for w in src.webcams]


async def _rotate(spec, rotation, client, session, fit, credits, photos) -> tuple[bytes, str]:
    src = spec.source
    items = _slots(spec, client, session, photos)
    # A group shows its items unfiltered, as the phone does.
    orientation = src.orientation if src.type == "collection" else None
    n = len(items)
    if rotation.get("key") != src.items_key:
        rotation.clear()
        rotation.update(key=src.items_key, pos=0)
    # A group arrives in the phone's order, its shuffle already applied.
    order = rotation_order(src)
    start = rotation.get("pos", 0) % n

    # Out of credits, a group with webcams or links keeps showing them (the
    # runner pauses a board pin or an all-board group instead).
    can_skip_boards = credits is not None and runs_without_credits(spec)
    skip_boards = can_skip_boards and credits.paused
    last_error: str | None = None
    cloud_down_at: int | None = None  # first slot that found the cloud down
    attempts = scanned = waiting = 0
    for step in range(n):
        if attempts == MAX_ATTEMPTS_PER_TICK:
            break
        scanned = step + 1
        slot = (start + step) % n
        load, label, via_cloud, photo = items[order[slot]]
        is_board = src.type == "group" and via_cloud
        if is_board and skip_boards:
            continue  # out of credits: not asked, and not one of the tick's tries
        if photo is not None and (photos is None or photo not in photos):
            waiting += 1  # the phone hasn't pushed it yet: skipped, never an error
            continue
        attempts += 1
        if via_cloud and cloud_down_at is not None:
            continue  # the cloud is down: ask it once per tick, keep showing direct images
        try:
            raw = await load()
            if raw is None:  # a phone photo deleted behind HA's back
                attempts, waiting = attempts - 1, waiting + 1
                continue
            out = await _fit(fit, raw, orientation)
        except OutOfCredits:
            if not can_skip_boards:
                raise
            credits.hit, skip_boards = True, True
            continue
        except _PASSTHROUGH:
            raise
        except CloudUnreachable:
            cloud_down_at = slot
            continue
        except (SourceError, CloudError) as err:
            last_error = str(err)
            continue
        if out is None:
            continue
        rotation["pos"] = (slot + 1) % n
        rotation["shown"] = order[slot]  # the spec item now on the TV (dashboard entities)
        if credits is not None:
            credits.board_shown = is_board
        return out, label
    if cloud_down_at is not None:
        rotation["pos"] = cloud_down_at  # nothing shown: retry the proxied item first next tick
        raise SourceError("cloud unreachable")
    if waiting and attempts == 0:
        # Only phone photos HA doesn't hold (and boards paused): keep the TV's
        # image and retry from the same item next tick.
        raise WaitingForPhone("waiting for the phone's photos")
    suffix = f": {last_error}" if last_error else ""
    if scanned < n:
        rotation["pos"] = (start + scanned) % n
        raise SourceError(f"no usable image in this batch{suffix}")
    raise SourceError(f"no usable images ({attempts} tried){suffix}")


async def load_item(
    spec: SyncSpec,
    index: int,
    client: CloudClient,
    session: aiohttp.ClientSession,
    fit: Fitter,
    photos: PhotoStore | None = None,
) -> bytes:
    """The fitted image of the spec's item *index* (spec order), for a preview.

    The same loaders as a tick, but one item only, no retry on another item and no
    rotation state touched. A board renders through the cloud (a credit)."""
    src = spec.source
    try:
        if src.type in ("board", "custom_url"):
            if index != 0:
                raise SourceError(f"no item {index}")
            if src.type == "board":
                out = await _fit(fit, await client.board(src.board.type_id, src.board.params), None)
                if out is None:
                    raise SourceError("board image rejected")
                return out
            out = await _fit(fit, await fetch_image(session, src.custom_url), src.orientation)
            if out is None:
                raise SourceError("custom image does not match the orientation filter")
            return out
        slots = _slots(spec, client, session, photos)
        if not 0 <= index < len(slots):
            raise SourceError(f"no item {index}")
        load, _label, _via_cloud, photo = slots[index]
        if photo is not None and (photos is None or photo not in photos):
            raise WaitingForPhone("Home Assistant doesn't have this photo from the phone yet")
        raw = await load()
        if raw is None:
            raise WaitingForPhone("Home Assistant doesn't have this photo from the phone yet")
        out = await _fit(fit, raw, src.orientation if src.type == "collection" else None)
        if out is None:
            raise SourceError("the image does not match the orientation filter")
        return out
    except _PASSTHROUGH:
        raise
    except CloudError as err:
        raise SourceError(str(err)) from err
