"""Never upload: HA never sends a phone photo's bytes, or anything derived from them,
to TVCanvas Cloud or to a URL from a spec (docs/lan-api.md §2, trust model).

Photos only ever travel phone → HA on the LAN, and HA → the TV. If a test here
fails because the code grew a new outbound path, that path needs the owner's review
against the trust model, not a quick fix to the test.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import io
import typing
from pathlib import Path

import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError
from PIL import Image

from custom_components.tvcanvas import sources
from custom_components.tvcanvas.api import CloudClient
from custom_components.tvcanvas.display_cast import CastDisplay
from custom_components.tvcanvas.display_view import ImageCache
from custom_components.tvcanvas.displays import DisplayError
from custom_components.tvcanvas.models import parse_spec
from custom_components.tvcanvas.photos import PhotoStore, photos_dir
from custom_components.tvcanvas.runner import TVRunner
from custom_components.tvcanvas.storage import StateStore
from custom_components.tvcanvas.urlsafety import TV_HOST_REFUSED

from .common import URL

# Every call HA makes to TVCanvas Cloud. None of them carries image bytes: a new
# method here must be checked against the trust model before it is added.
CLOUD_CALLS = {"claim", "agent_sync", "agent_status", "agent_refresh", "board", "proxied_image"}


def test_the_cloud_client_has_no_way_to_upload_bytes() -> None:
    public = {
        name
        for name, member in inspect.getmembers(CloudClient, inspect.isfunction)
        if not name.startswith("_")
    }
    assert public == CLOUD_CALLS
    for name in public:
        hints = typing.get_type_hints(getattr(CloudClient, name))
        hints.pop("return", None)
        for param, hint in hints.items():
            assert bytes not in {hint, *typing.get_args(hint)}, f"{name}({param}: bytes)"
    # Request bodies are JSON built from the arguments above; never raw data.
    source = inspect.getsource(CloudClient)
    assert "data=" not in source and "files=" not in source


def test_fetching_from_spec_urls_is_get_only() -> None:
    """Images from spec URLs are only ever downloaded (no body is sent)."""
    from custom_components.tvcanvas import urlsafety

    source = inspect.getsource(urlsafety) + inspect.getsource(sources)
    for verb in (".post(", ".put(", ".patch(", "data=", "json="):
        assert verb not in source, verb


def image_bytes() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 10, 30)).save(out, "JPEG")
    return out.getvalue()


def forms(data: bytes) -> list[str]:
    """Ways bytes could hide in a request we can see as text."""
    return [
        repr(data)[2:-1][:40],
        data.hex()[:40],
        base64.b64encode(data).decode()[:40],
        base64.urlsafe_b64encode(data).decode()[:40],
    ]


class RecordingDisplay:
    def __init__(self) -> None:
        self.shown: list[bytes] = []

    async def show(self, jpeg: bytes) -> None:
        self.shown.append(jpeg)


class NoSession:
    """The direct-image session: a local photo must never touch it."""

    def __getattr__(self, name):
        raise AssertionError(f"image session used: {name}")


# Home TVs, one per range HA counts as the home network (#14).
HOME_HOSTS = ["10.0.0.2", "172.16.5.4", "172.31.255.254", "192.168.1.50", "fd00::5"]
# Addresses a hacked cloud could name: public ones, and near misses of the home ranges
# that are not a home either (shared CGNAT space, just outside 172.16/12, documentation
# and benchmark ranges). The spec parser accepts every one; HA must still refuse them.
OUTSIDE_HOSTS = [
    "93.184.215.14",
    "8.8.8.8",
    "2001:4860:4860::8888",
    "::ffff:8.8.8.8",
    "100.64.0.1",
    "172.32.0.1",
    "172.15.255.254",
    "192.0.2.1",
    "198.18.0.1",
]


class Run:
    """One TV's sync with a phone photo in the group, ticked through every item."""

    photo: bytes
    fitted: bytes
    display: RecordingDisplay
    fetched: list[str]
    reads: list[str]


async def run_group(hass, aioclient_mock, monkeypatch, host: str, with_others: bool) -> Run:
    run = Run()
    run.photo = photo = image_bytes()
    sha = hashlib.sha256(photo).hexdigest()
    photos = PhotoStore(hass)
    await photos.async_load()
    await photos.async_put(sha, photo)
    # Where the bytes come from: only <config>/tvcanvas/images/<sha256>.jpg.
    assert photos_dir(hass) == Path(hass.config.config_dir) / "tvcanvas" / "images"
    assert photos._path(sha) == photos_dir(hass) / f"{sha}.jpg"
    run.reads = []
    real_read = photos.async_read

    async def read(digest):
        run.reads.append(digest)
        return await real_read(digest)

    photos.async_read = read

    run.fetched = []

    async def fetch(session, url):  # a spec URL: recorded, and never given a body
        run.fetched.append(url)
        return image_bytes()

    monkeypatch.setattr(sources, "fetch_image", fetch)
    run.fitted = b"FITTED:" + photo  # stands in for the resized JPEG the TV gets
    monkeypatch.setattr(
        "custom_components.tvcanvas.runner.fit_to_tv",
        lambda data, orientation: b"FITTED:" + data,
    )
    aioclient_mock.post(f"{URL}/ha/agent/status", json={})
    aioclient_mock.get(
        f"{URL}/board/weather", content=b"BOARD", headers={"Content-Type": "image/jpeg"}
    )
    items = [{"type": "local", "sha256": sha, "name": "IMG_0042"}]
    if with_others:
        items += [
            {"type": "webcam", "url": "https://evil.example.com/cam.jpg", "name": "Cam"},
            {"type": "board", "board": {"type_id": "weather", "params": {}}},
        ]
    spec = parse_spec(
        {
            "version": 1,
            "tv": {"id": "tv-1", "kind": "frame", "host": host, "name": "T"},
            "interval_seconds": 60,
            "source": {"type": "group", "group": {"items": items}},
        }
    )
    store = StateStore(hass, "e")
    await store.async_load()
    client = CloudClient(async_get_clientsession(hass), URL, "fc_ha_t")
    run.display = RecordingDisplay()
    runner = TVRunner(
        hass, client, NoSession(), store, spec, run.display, lambda: None, lambda: None, photos
    )
    for _ in range(len(items)):
        await runner.tick()
    return run


def assert_no_photo_bytes_reach_the_cloud(aioclient_mock, run: Run, allowed: set) -> None:
    for method, url, body, _headers in aioclient_mock.mock_calls:
        assert str(url).startswith(URL)
        assert (method.upper(), str(url).removeprefix(URL)) in allowed
        text = repr(body) + repr(url)
        for data in (run.photo, run.fitted):
            for form in forms(data):
                assert form not in text


@pytest.mark.parametrize("host", HOME_HOSTS)
@pytest.mark.parametrize("with_others", [False, True])
async def test_a_local_photo_goes_only_from_the_photo_folder_to_the_tv(
    hass, aioclient_mock, monkeypatch, with_others, host
) -> None:
    run = await run_group(hass, aioclient_mock, monkeypatch, host, with_others)
    assert run.fitted in run.display.shown  # the TV got the photo
    assert_no_photo_bytes_reach_the_cloud(
        aioclient_mock, run, {("POST", "/ha/agent/status"), ("GET", "/board/weather")}
    )
    assert run.fetched == (["https://evil.example.com/cam.jpg"] if with_others else [])


@pytest.mark.parametrize("host", OUTSIDE_HOSTS)
@pytest.mark.parametrize("with_others", [False, True])
async def test_a_local_photo_is_never_read_for_a_tv_outside_the_home(
    hass, aioclient_mock, monkeypatch, with_others, host
) -> None:
    """#14: a spec with a public tv.host and a `local` item sends nothing. The photo is
    not even read from the folder, and no other item is fetched for that TV."""
    run = await run_group(hass, aioclient_mock, monkeypatch, host, with_others)
    assert run.display.shown == [] and run.reads == [] and run.fetched == []
    assert_no_photo_bytes_reach_the_cloud(aioclient_mock, run, {("POST", "/ha/agent/status")})
    errors = {body["error"] for _m, _u, body, _h in aioclient_mock.mock_calls}
    assert errors == {TV_HOST_REFUSED}


async def test_a_cast_tv_gets_the_photo_from_has_local_url_only(hass, monkeypatch) -> None:
    """A Cast TV fetches the image from HA itself; HA never picks a cloud or external URL."""
    asked: list[dict] = []

    def get_url(hass, **kwargs):
        asked.append(kwargs)
        raise NoURLAvailableError

    monkeypatch.setattr("custom_components.tvcanvas.display_cast.get_url", get_url)
    with pytest.raises(DisplayError):
        await CastDisplay(hass, ImageCache(), "tv-1", "10.0.0.3").show(b"JPEG")
    assert asked == [{"allow_external": False, "allow_cloud": False, "prefer_external": False}]
