"""Fit any image to the TV canvas. Sync: run it through `decode_gate(hass).run`."""

from __future__ import annotations

import asyncio
import io
import warnings
from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant
from PIL import Image, ImageOps

from .const import (
    DOMAIN,
    JPEG_QUALITY,
    MAX_CONCURRENT_DECODES,
    MAX_SOURCE_PIXELS,
    MAX_UPLOAD_PIXELS,
    TV_HEIGHT,
    TV_WIDTH,
)


class ImageError(Exception):
    """Bytes are not a decodable image."""


class DecodeBusy(Exception):
    """No decode slot came free within the wait."""


class DecodeGate:
    """At most MAX_CONCURRENT_DECODES images decode at once, for the whole integration (#16).

    Decoding one image can take 150 to 380 MB, and every TV fits its image on its own
    schedule (all at once when HA starts) while phones push photos: without a limit a
    small Home Assistant box runs out of memory. Callers queue for a slot; the decode
    itself runs in HA's executor."""

    def __init__(self, slots: int = MAX_CONCURRENT_DECODES) -> None:
        self._slots = asyncio.Semaphore(slots)

    async def run(
        self,
        hass: HomeAssistant,
        func: Callable[..., Any],
        *args: Any,
        wait: float | None = None,
    ) -> Any:
        """func(*args) in the executor once a slot is free; DecodeBusy if none came free
        within *wait* seconds (None: wait as long as it takes).

        A caller cancelled mid-decode can't stop the thread, which still holds the
        memory: its slot frees when the thread ends, not when the caller goes."""
        try:
            async with asyncio.timeout(wait):
                await self._slots.acquire()
        except TimeoutError as err:
            raise DecodeBusy from err
        job = hass.async_add_executor_job(func, *args)
        handed_over = False
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            if not job.done():
                job.add_done_callback(self._release_after)
                handed_over = True
            raise
        finally:
            if not handed_over:
                self._slots.release()

    def _release_after(self, job: asyncio.Future[Any]) -> None:
        if not job.cancelled():
            job.exception()  # nobody awaits it any more: don't log "never retrieved"
        self._slots.release()


def decode_gate(hass: HomeAssistant) -> DecodeGate:
    """The one gate TV ticks and phone uploads share."""
    domain: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    gate = domain.get("decode")
    if gate is None:
        gate = domain["decode"] = DecodeGate()
    return gate


def fit_to_tv(data: bytes, orientation: str | None) -> bytes | None:
    """Return 3840x2160 JPEG bytes, or None if the image mismatches orientation.

    Every failure inside the pipeline is reported as ImageError. Pillow raises
    many exception types for malformed input, and none of them may escape.
    """
    with warnings.catch_warnings():
        # Scoped to this call: a decompression-bomb warning becomes an error here only.
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            return _fit(data, orientation)
        except ImageError:
            raise
        except Exception as err:
            raise ImageError(f"cannot process image: {err}") from err


# Bytes from any URL in a spec reach the decoder: only well-used decoders may run, never
# Pillow's rarely used ones (PSD, FLI, PCX, SGI, DDS, EPS, ...; #17). A camera's
# multi-picture JPEG (MPO) still opens through the JPEG plugin.
FETCHED_FORMATS = ("JPEG", "PNG", "WEBP", "GIF")


def _fit(data: bytes, orientation: str | None) -> bytes | None:
    img = Image.open(io.BytesIO(data), formats=FETCHED_FORMATS)
    if img.width * img.height > MAX_SOURCE_PIXELS:
        raise ImageError("image dimensions exceed the source pixel limit")
    # In place: a copy would hold the whole decoded image twice (#16; a 24 MP photo
    # peaks at about 180 MB instead of 300 MB).
    ImageOps.exif_transpose(img, in_place=True)
    img.load()

    w, h = img.size
    if orientation == "landscape" and h > w:
        return None
    if orientation == "portrait" and w >= h:
        return None

    if img.mode != "RGB":
        img = img.convert("RGB")
    fitted = ImageOps.fit(
        img, (TV_WIDTH, TV_HEIGHT), Image.Resampling.LANCZOS, centering=(0.5, 0.5)
    )
    del img  # free the source before the encoder allocates its buffers
    out = io.BytesIO()
    fitted.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


UPLOAD_FORMATS = ("JPEG", "PNG")


def check_upload(data: bytes) -> None:
    """Raise ImageError unless *data* fully decodes as a JPEG or PNG within the upload
    pixel cap (what the phone sends, with room; refused before decoding).

    For photos the phone pushes (docs/lan-api.md); sync, run it through the decode gate."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(io.BytesIO(data), formats=UPLOAD_FORMATS) as img:
                if img.width * img.height > MAX_UPLOAD_PIXELS:
                    raise ImageError("image dimensions exceed the upload pixel limit")
                img.load()
        except ImageError:
            raise
        except Exception as err:
            raise ImageError(f"cannot decode image: {err}") from err
