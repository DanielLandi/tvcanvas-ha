from __future__ import annotations

import io

import pytest
from PIL import Image

from custom_components.tvcanvas.imaging import ImageError, fit_to_tv


def make(w: int, h: int, fmt: str = "PNG", mode: str = "RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, (w, h), "red").save(buf, fmt)
    return buf.getvalue()


@pytest.mark.parametrize(("w", "h"), [(800, 600), (4000, 1000), (500, 2000), (3840, 2160)])
def test_output_is_tv_sized_jpeg(w, h) -> None:
    out = fit_to_tv(make(w, h), None)
    img = Image.open(io.BytesIO(out))
    assert img.format == "JPEG" and img.size == (3840, 2160)


def test_orientation_filter() -> None:
    assert fit_to_tv(make(500, 2000), "landscape") is None
    assert fit_to_tv(make(2000, 500), "portrait") is None
    assert fit_to_tv(make(2000, 500), "landscape") is not None


def test_alpha_and_palette_images_convert() -> None:
    assert fit_to_tv(make(100, 100, "PNG", "RGBA"), None)
    assert fit_to_tv(make(100, 100, "PNG", "P"), None)


def test_garbage_raises() -> None:
    with pytest.raises(ImageError):
        fit_to_tv(b"not an image", None)


def test_truncated_jpeg_raises() -> None:
    buf = io.BytesIO()
    Image.new("RGB", (800, 600), "red").save(buf, "JPEG")
    data = buf.getvalue()
    with pytest.raises(ImageError):
        fit_to_tv(data[: len(data) // 2], None)


def test_corrupted_png_header_raises_image_error() -> None:
    # Byte 11 is the IHDR chunk length; zeroing it makes Pillow raise ValueError,
    # which must surface as ImageError, not escape the sources tick.
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), "red").save(buf, "PNG")
    data = bytearray(buf.getvalue())
    data[11] = 0
    with pytest.raises(ImageError):
        fit_to_tv(bytes(data), None)


def test_corrupted_png_midstream_never_escapes_as_other_exception() -> None:
    buf = io.BytesIO()
    Image.effect_noise((256, 256), 64).convert("RGB").save(buf, "PNG")
    data = bytearray(buf.getvalue())
    mid = len(data) // 2
    data[mid : mid + 64] = b"\xff" * 64
    try:
        out = fit_to_tv(bytes(data), None)
    except ImageError:
        return
    img = Image.open(io.BytesIO(out))
    assert img.format == "JPEG" and img.size == (3840, 2160)


def test_oversized_source_declared_in_header_raises() -> None:
    # 10000 x 5000 = 50 MP, tiny on disk because the image is 1-bit and uniform.
    buf = io.BytesIO()
    Image.new("1", (10000, 5000), 0).save(buf, "PNG")
    assert len(buf.getvalue()) < 1_000_000
    with pytest.raises(ImageError):
        fit_to_tv(buf.getvalue(), None)


def test_module_does_not_mutate_pillow_pixel_limit() -> None:
    # Pillow's default; the module must not override it process-wide.
    assert Image.MAX_IMAGE_PIXELS == 89_478_485


def test_exif_orientation_is_applied_before_orientation_check() -> None:
    # Stored as 200x100 landscape pixels, but EXIF orientation 6 rotates it to portrait.
    exif = Image.Exif()
    exif[0x0112] = 6
    buf = io.BytesIO()
    Image.new("RGB", (200, 100), "red").save(buf, "JPEG", exif=exif.tobytes())
    data = buf.getvalue()
    assert fit_to_tv(data, "landscape") is None
    assert fit_to_tv(data, "portrait") is not None


# --- fetched images: common web formats only (#17 item 6) --------------------------------


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP", "GIF"])
def test_common_web_formats_are_fitted(fmt) -> None:
    out = fit_to_tv(make(64, 48, fmt), None)
    assert Image.open(io.BytesIO(out)).size == (3840, 2160)


def test_a_camera_multi_picture_jpeg_is_fitted() -> None:
    """Cameras and phones write MPO files; Pillow opens them through its JPEG plugin."""
    buf = io.BytesIO()
    first, second = Image.new("RGB", (64, 48), "red"), Image.new("RGB", (32, 24), "blue")
    first.save(buf, "MPO", save_all=True, append_images=[second])
    assert Image.open(io.BytesIO(buf.getvalue())).format == "MPO"
    assert fit_to_tv(buf.getvalue(), None)


@pytest.mark.parametrize("fmt", ["BMP", "TIFF", "PCX", "SGI", "TGA", "PPM", "ICO", "DDS", "IM"])
def test_rarely_used_decoders_are_never_reached(fmt) -> None:
    """Bytes from any URL in a spec reach the decoder: only well-used ones may run."""
    data = make(64, 48, fmt)
    assert Image.open(io.BytesIO(data)).format == fmt  # Pillow itself would decode it
    with pytest.raises(ImageError):
        fit_to_tv(data, None)
