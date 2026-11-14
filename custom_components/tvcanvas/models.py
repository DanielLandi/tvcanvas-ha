"""The sync spec the phone writes and HA runs (spec §4)."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

import yarl

from .const import MAX_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS, SPEC_VERSION
from .urlsafety import is_public_address

KINDS = ("frame", "cast")
SOURCE_TYPES = ("board", "collection", "webcam", "custom_url", "group")
GROUP_ITEM_TYPES = ("board", "webcam", "custom_url", "local")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
LOCAL_ITEM_KEYS = frozenset({"type", "sha256", "name"})
ORIENTATIONS = ("landscape", "portrait")

_LEGACY_LABEL = re.compile(r"[0-9]+|0[xX][0-9a-fA-F]*")
MAX_IMAGES = 500
MAX_WEBCAMS = 50
MAX_GROUP_ITEMS = 100
MAX_PARAMS = 20
MAX_PARAM_KEY = 64
MAX_PARAM_VALUE = 512


class SpecError(ValueError):
    """The spec can't be run by this version of the integration."""


@dataclass(frozen=True)
class TV:
    id: str
    kind: Literal["frame", "cast"]
    host: str
    name: str


@dataclass(frozen=True)
class BoardSource:
    type_id: str
    params: dict[str, str]

    def __hash__(self) -> int:  # dict field: hash by sorted items
        return hash((self.type_id, tuple(sorted(self.params.items()))))


@dataclass(frozen=True)
class CollectionImage:
    url: str
    title: str
    artist: str | None
    proxied: bool


@dataclass(frozen=True)
class Webcam:
    url: str
    name: str


@dataclass(frozen=True)
class GroupItem:
    """One item of an art group: a board HA renders, a public https image (a
    webcam or a custom link) HA fetches, or a photo the phone pushed to HA over
    the LAN (`local`, named by the SHA-256 of its bytes; docs/lan-api.md)."""

    type: Literal["board", "webcam", "custom_url", "local"]
    board: BoardSource | None = None
    url: str | None = None
    name: str | None = None
    sha256: str | None = None

    @property
    def label(self) -> str:
        return self.board.type_id if self.board is not None else (self.name or self.url or "")

    @property
    def key(self) -> str:
        if self.board is not None:
            params = json.dumps(self.board.params, sort_keys=True, separators=(",", ":"))
            return f"board\t{self.board.type_id}\t{params}"
        if self.sha256 is not None:
            return f"local\t{self.sha256}"
        return f"{self.type}\t{self.url}"


@dataclass(frozen=True)
class Source:
    type: str
    board: BoardSource | None
    images: tuple[CollectionImage, ...]
    webcams: tuple[Webcam, ...]
    custom_url: str | None
    shuffle: bool
    orientation: str | None
    # type == "group": the items in the order the phone shows them, its
    # shuffle already applied. HA never reorders them.
    items: tuple[GroupItem, ...] = ()

    @property
    def local_hashes(self) -> frozenset[str]:
        """The phone photos this source shows (a group's `local` items)."""
        return frozenset(i.sha256 for i in self.items if i.sha256 is not None)

    @property
    def items_key(self) -> str:
        urls = (
            [i.url for i in self.images]
            or [w.url for w in self.webcams]
            or [g.key for g in self.items]
        )
        if not urls:
            return ""
        return hashlib.sha256("\n".join(urls).encode()).hexdigest()


@dataclass(frozen=True)
class SyncSpec:
    version: int
    tv: TV
    enabled: bool
    interval_seconds: int
    source: Source
    replace_previous: bool


def _obj(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SpecError(f"{field}: expected an object")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise SpecError(f"{field}: expected a list")
    return value


def _bool(d: dict[str, Any], key: str, default: bool) -> bool:
    value = d.get(key, default)
    if not isinstance(value, bool):
        raise SpecError(f"{key}: expected true or false")
    return value


def _str(d: dict[str, Any], key: str, *, required: bool = True, max_len: int = 2048) -> str | None:
    value = d.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise SpecError(f"{key}: expected a non-empty string ≤ {max_len} chars")
    return value


def check_public_https(url: str) -> str:
    """Public wrapper for runtime URL checks."""
    return _public_https(url, "url")


def _public_https(url: str, field: str) -> str:
    """An https URL HA may fetch directly: no private, loopback, link-local, etc. host.

    Only the literal host: `sources.fetch_image` checks what a host name resolves to."""
    public_https_url(url, field)
    return url


def public_https_url(url: str | yarl.URL, field: str = "url") -> yarl.URL:
    """Validate and return the parsed URL, parsed the way aiohttp will request it."""
    try:
        parsed = yarl.URL(url) if isinstance(url, str) else url
        scheme = parsed.scheme
        host = (parsed.host or "").rstrip(".").lower()
    except Exception as err:  # noqa: BLE001 - any parse failure is a rejection
        raise SpecError(f"{field}: must be an https URL") from err
    if scheme != "https" or not host:
        raise SpecError(f"{field}: must be an https URL")
    if host == "localhost" or host.endswith(".localhost"):
        raise SpecError(f"{field}: host not allowed")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # A DNS name. All-numeric / 0x-hex labels are legacy IPv4 forms
        # (2130706433, 0x7f000001, 0x7f.0.0.1, 127.1).
        labels = host.split(".")
        if labels[-1].isdigit() or all(_LEGACY_LABEL.fullmatch(x) for x in labels):
            raise SpecError(f"{field}: host not allowed") from None
        return parsed
    if not is_public_address(ip):
        raise SpecError(f"{field}: host not allowed")
    return parsed


def _board(b: dict[str, Any]) -> BoardSource:
    params = _obj(b.get("params") or {}, "board.params")
    if len(params) > MAX_PARAMS:
        raise SpecError(f"board.params: at most {MAX_PARAMS} keys")
    for k, v in params.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise SpecError("board.params must be a string map")
        if len(k) > MAX_PARAM_KEY or len(v) > MAX_PARAM_VALUE:
            raise SpecError("board.params: key or value too long")
    return BoardSource(type_id=_str(b, "type_id", max_len=64), params=dict(params))


def _group_items(group: dict[str, Any]) -> tuple[GroupItem, ...]:
    items = _list(group.get("items") or [], "group.items")
    if not items:
        raise SpecError("group has no items")
    if len(items) > MAX_GROUP_ITEMS:
        raise SpecError(f"group.items: at most {MAX_GROUP_ITEMS} items")
    parsed: list[GroupItem] = []
    for item in items:
        item = _obj(item, "group.items[]")
        itype = item.get("type")
        if itype not in GROUP_ITEM_TYPES:
            raise SpecError(f"group item type {itype!r} unsupported")
        if itype == "local":
            if set(item) != LOCAL_ITEM_KEYS:
                raise SpecError("group local item: exactly type, sha256 and name")
            sha = item.get("sha256")
            if not isinstance(sha, str) or not SHA256_HEX.fullmatch(sha):
                raise SpecError("group local item: sha256 must be 64 lowercase hex characters")
            parsed.append(GroupItem(type="local", sha256=sha, name=_str(item, "name", max_len=256)))
        elif itype == "board":
            if "url" in item or "name" in item:
                raise SpecError("group board item: only type and board")
            parsed.append(GroupItem(type="board", board=_board(_obj(item.get("board"), "board"))))
        else:
            if "board" in item:
                raise SpecError(f"group {itype} item: no board block")
            parsed.append(
                GroupItem(
                    type=itype,
                    url=_public_https(_str(item, "url"), "group.items[].url"),
                    name=_str(item, "name", max_len=256),
                )
            )
    return tuple(parsed)


def _host(value: str) -> str:
    """The shape of tv.host only (the cloud contract's rule). Whether the address is on
    the home network is decided where it is used (`displays.tv_address`, #14), so a TV
    HA refuses still gets a status the user can see instead of a skipped spec."""
    try:
        ip = ipaddress.ip_address(value)
    except ValueError as err:
        raise SpecError("tv.host: must be an IP literal") from err
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_link_local:
        raise SpecError("tv.host: loopback, unspecified, multicast and link-local not allowed")
    return str(ip)


def parse_spec(raw: dict[str, Any]) -> SyncSpec:
    raw = _obj(raw, "spec")
    if raw.get("version") != SPEC_VERSION:
        raise SpecError(f"version {raw.get('version')!r} unsupported (want {SPEC_VERSION})")

    tv_raw = _obj(raw.get("tv") or {}, "tv")
    kind = tv_raw.get("kind")
    if kind not in KINDS:
        raise SpecError(f"tv.kind {kind!r} unsupported")
    tv = TV(
        id=_str(tv_raw, "id", max_len=64),
        kind=kind,
        host=_host(_str(tv_raw, "host", max_len=64)),
        name=_str(tv_raw, "name", max_len=64),
    )

    interval = raw.get("interval_seconds")
    if (
        not isinstance(interval, int)
        or not MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS
    ):
        raise SpecError("interval_seconds out of range")

    src = _obj(raw.get("source") or {}, "source")
    stype = src.get("type")
    if stype not in SOURCE_TYPES:
        raise SpecError(f"source.type {stype!r} unsupported")
    orientation = src.get("orientation")
    if orientation is not None and orientation not in ORIENTATIONS:
        raise SpecError("source.orientation invalid")

    board = None
    images: tuple[CollectionImage, ...] = ()
    webcams: tuple[Webcam, ...] = ()
    custom_url = None
    group_items: tuple[GroupItem, ...] = ()
    if stype == "board":
        board = _board(_obj(src.get("board") or {}, "board"))
    elif stype == "group":
        if orientation is not None:
            raise SpecError("source.orientation: not allowed for a group")
        group_items = _group_items(_obj(src.get("group") or {}, "group"))
    elif stype == "collection":
        items = _list(src.get("images") or [], "images")
        if not items:
            raise SpecError("collection has no images")
        if len(items) > MAX_IMAGES:
            raise SpecError(f"images: at most {MAX_IMAGES} items")
        parsed = []
        for item in items:
            item = _obj(item, "images[]")
            proxied = _bool(item, "proxied", False)
            url = _str(item, "url")
            if not proxied:
                _public_https(url, "images[].url")
            parsed.append(
                CollectionImage(
                    url=url,
                    title=_str(item, "title", max_len=256),
                    artist=_str(item, "artist", required=False, max_len=256),
                    proxied=proxied,
                )
            )
        images = tuple(parsed)
    elif stype == "webcam":
        items = _list(src.get("webcams") or [], "webcams")
        if not items:
            raise SpecError("webcam list is empty")
        if len(items) > MAX_WEBCAMS:
            raise SpecError(f"webcams: at most {MAX_WEBCAMS} items")
        parsed_cams = []
        for w in items:
            w = _obj(w, "webcams[]")
            parsed_cams.append(
                Webcam(
                    url=_public_https(_str(w, "url"), "webcams[].url"),
                    name=_str(w, "name", max_len=256),
                )
            )
        webcams = tuple(parsed_cams)
    else:
        custom_url = _public_https(_str(src, "custom_url"), "custom_url")

    frame = _obj(raw.get("frame") or {}, "frame")
    return SyncSpec(
        version=SPEC_VERSION,
        tv=tv,
        enabled=_bool(raw, "enabled", True),
        interval_seconds=interval,
        source=Source(
            type=stype,
            board=board,
            images=images,
            webcams=webcams,
            custom_url=custom_url,
            shuffle=_bool(src, "shuffle", False),
            orientation=orientation,
            items=group_items,
        ),
        replace_previous=_bool(frame, "replace_previous", True),
    )


def referenced_photos(raw_specs: list[Any]) -> set[str]:
    """Every `local` hash in the raw specs, parseable or not.

    Photo garbage collection keeps these: a spec this HA can't run today (say, a
    newer field) still owns its photos."""
    found: set[str] = set()
    for raw in raw_specs:
        try:
            items = raw["source"]["group"]["items"]
        except (KeyError, TypeError):
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get("type") == "local":
                sha = item.get("sha256")
                if isinstance(sha, str) and SHA256_HEX.fullmatch(sha):
                    found.add(sha)
    return found
