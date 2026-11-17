"""The items a TV's spec rotates through, as the dashboard entities name them.

Built from the spec alone (no cloud call): a board pin or custom link is one item;
a collection, a webcam list or a group is one item per entry, in the spec's order.
"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass

from .models import Source

# A select's state is its current option (HA states are at most 255 characters), and the
# same name goes to the phone in the status report's `showing.name` (at most 64).
OPTION_MAX = 64
_SINGLE = ("board", "custom_url")


@dataclass(frozen=True)
class Item:
    index: int  # position in the spec's list (before any shuffle)
    type: str  # "board" | "custom_url" | "collection" | "webcam" | "local"
    name: str
    artist: str | None = None
    sha256: str | None = None  # a phone photo (`local`), by the hash of its bytes

    @property
    def photo(self) -> bool:
        """The bytes come from a photo the phone pushed over the LAN."""
        return self.sha256 is not None

    @property
    def kind(self) -> str:
        """The item's kind in the status report's `showing` (a phone photo is "photo")."""
        return "photo" if self.type == "local" else self.type

    @property
    def costs_credit(self) -> bool:
        """Showing or previewing it renders a board, which uses a credit."""
        return self.type == "board"


# Board names from TVCanvas Cloud's public board catalog.
BOARD_NAMES = {
    "flights": "Flight Board",
    "weather": "Weather Dashboard",
    "weather_cards": "Weather Cards",
    "sports": "Sports Scores",
    "stocks": "Stock Ticker",
    "surf": "Surf Conditions",
    "transit": "Transit Departures",
    "crypto": "Crypto Ticker",
    "news": "Positive News",
}
_SPORTS_LEAGUES = {
    "nfl": "NFL",
    "mlb": "MLB",
    "nba": "NBA",
    "nhl": "NHL",
    "mls": "MLS",
    "worldcup": "World Cup",
    "wwc": "Women's World Cup",
    "euros": "Euros",
    "copa": "Copa América",
    "ucl": "Champions League",
    "uel": "Europa League",
}
_IATA = re.compile(r"[A-Z]{3}")
_TYPE_ID = re.compile(r"[a-z][a-z0-9_]{0,31}")


def _label(type_id: str, params: dict[str, str]) -> str | None:
    """The one board setting shown in a name, and only when it is a public label checked
    against a fixed list or shape. Everything else stays out: a param can carry a key, a
    calendar URL, or a home address or coordinates (a weather `location`, a transit stop)."""
    if type_id == "flights" and _IATA.fullmatch(airport := params.get("airport", "")):
        return airport
    if type_id == "sports":
        return _SPORTS_LEAGUES.get(params.get("league", ""))
    return None


def _board_name(type_id: str, params: dict[str, str]) -> str:
    # A board newer than this list goes by its catalog id, when it looks like one.
    name = BOARD_NAMES.get(type_id) or (type_id if _TYPE_ID.fullmatch(type_id) else "Board")
    label = _label(type_id, params)
    return f"{name} {label}" if label else name


def items_of(src: Source) -> list[Item]:
    if src.type == "board":
        return [Item(0, "board", _board_name(src.board.type_id, src.board.params))]
    if src.type == "custom_url":
        return [Item(0, "custom_url", "Custom link")]  # never the URL: it may carry a key
    if src.type == "collection":
        return [
            Item(i, "collection", img.title, artist=img.artist) for i, img in enumerate(src.images)
        ]
    if src.type == "webcam":
        return [Item(i, "webcam", w.name) for i, w in enumerate(src.webcams)]
    items: list[Item] = []
    for i, g in enumerate(src.items):
        if g.board is not None:
            items.append(Item(i, "board", _board_name(g.board.type_id, g.board.params)))
        elif g.sha256 is not None:
            items.append(Item(i, "local", g.name or "Photo", sha256=g.sha256))
        else:
            items.append(Item(i, g.type, g.name or g.type))
    return items


def option_names(items: list[Item]) -> list[str]:
    """One unique, printable name per item, in order: "Sunset", "Sunset 2", …"""
    names: list[str] = []
    used: set[str] = set()
    for item in items:
        base = " ".join(item.name.split())[:OPTION_MAX].strip() or item.type
        name, count = base, 1
        while name in used:
            count += 1
            suffix = f" {count}"
            name = base[: OPTION_MAX - len(suffix)].rstrip() + suffix
        used.add(name)
        names.append(name)
    return names


def rotates(src: Source) -> bool:
    return src.type not in _SINGLE


def source_key(src: Source) -> str:
    """A stable id for the pin's content, kept in the store to tell whether a remembered
    item still belongs to it after a restart (never shown; it hashes URLs)."""
    if rotates(src):
        return src.items_key
    if src.type == "board":
        params = "\t".join(f"{k}={v}" for k, v in sorted(src.board.params.items()))
        raw = f"board\t{src.board.type_id}\t{params}"
    else:
        raw = f"custom_url\t{src.custom_url}\t{src.orientation}"
    return hashlib.sha256(raw.encode()).hexdigest()


def rotation_order(src: Source) -> list[int]:
    """The order the rotation visits the spec's items (`sources._rotate`).

    A shuffled collection or webcam list is visited in a fixed order seeded by its
    item list; a group arrives in the phone's order, already shuffled."""
    if not rotates(src):
        return [0]
    n = len(src.images) or len(src.webcams) or len(src.items)
    if src.shuffle and src.type != "group":
        return random.Random(src.items_key).sample(range(n), n)
    return list(range(n))
