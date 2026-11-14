from __future__ import annotations

import copy

import pytest

from custom_components.tvcanvas.models import SpecError, parse_spec

BASE = {
    "version": 1,
    "tv": {"id": "tv-1", "kind": "frame", "host": "192.168.0.21", "name": "Living room"},
    "enabled": True,
    "interval_seconds": 300,
    "source": {"type": "board", "board": {"type_id": "weather", "params": {"location": "Lisbon"}}},
    "frame": {"replace_previous": True},
}


def spec(**changes):
    raw = copy.deepcopy(BASE)
    for path, value in changes.items():
        node = raw
        keys = path.split("__")
        for k in keys[:-1]:
            node = node[k]
        if value is ...:
            node.pop(keys[-1], None)
        else:
            node[keys[-1]] = value
    return raw


def test_parses_board_spec() -> None:
    s = parse_spec(BASE)
    assert s.tv.kind == "frame"
    assert s.source.board.type_id == "weather"
    assert s.source.board.params == {"location": "Lisbon"}
    assert s.replace_previous is True
    assert s.source.items_key == ""


def test_collection_items_key_changes_with_urls() -> None:
    raw = spec(
        source={
            "type": "collection",
            "images": [
                {"url": "https://a/1.jpg", "title": "One", "artist": None, "proxied": False},
                {"url": "met:paint:abc:def", "title": "Two", "proxied": True},
            ],
            "shuffle": True,
            "orientation": "landscape",
        }
    )
    s1 = parse_spec(raw)
    raw["source"]["images"][0]["url"] = "https://a/9.jpg"
    s2 = parse_spec(raw)
    assert s1.source.images[1].proxied is True
    assert s1.source.items_key and s1.source.items_key != s2.source.items_key


def test_specs_are_comparable() -> None:
    assert parse_spec(BASE) == parse_spec(copy.deepcopy(BASE))
    assert parse_spec(BASE) != parse_spec(spec(interval_seconds=600))


def test_frame_block_defaults_replace_previous_true() -> None:
    assert parse_spec(spec(frame=...)).replace_previous is True


@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"tv__kind": "roku"},
        {"source__type": "photos_album"},
        {"interval_seconds": 59},
        {"interval_seconds": 86401},
        {"tv__host": "not-an-ip"},
        {"tv__host": "127.0.0.1"},
        {"tv__id": ""},
    ],
)
def test_rejects_unsupported_or_invalid(change) -> None:
    with pytest.raises(SpecError):
        parse_spec(spec(**change))


def test_custom_url_must_be_https() -> None:
    with pytest.raises(SpecError):
        parse_spec(spec(source={"type": "custom_url", "custom_url": "http://x/y.jpg"}))
    assert (
        parse_spec(
            spec(source={"type": "custom_url", "custom_url": "https://x/y.jpg"})
        ).source.custom_url
        == "https://x/y.jpg"
    )


def test_empty_collection_rejected() -> None:
    with pytest.raises(SpecError):
        parse_spec(spec(source={"type": "collection", "images": []}))


def test_non_proxied_collection_url_must_be_https() -> None:
    with pytest.raises(SpecError):
        parse_spec(
            spec(
                source={
                    "type": "collection",
                    "images": [{"url": "http://a/1.jpg", "title": "x", "proxied": False}],
                }
            )
        )


MALFORMED = [
    {"tv": "x"},
    {"source": "x"},
    {"frame": "x"},
    {"source": {"type": "board", "board": "x"}},
    {"source": {"type": "collection", "images": ["x"]}},
    {"source": {"type": "webcam", "webcams": ["x"]}},
    {"source": {"type": "collection", "images": 5}},
    {"source": {"type": "collection", "images": "abc"}},
    {"source": {"type": "collection", "images": {"a": 1}}},
    {"source": {"type": "webcam", "webcams": 5}},
]


@pytest.mark.parametrize("change", MALFORMED)
def test_malformed_shapes_raise_spec_error(change) -> None:
    with pytest.raises(SpecError):
        parse_spec(spec(**change))


@pytest.mark.parametrize(
    "change",
    [
        {
            "source": {
                "type": "collection",
                "images": [{"url": "met:x", "title": "t", "proxied": "false"}],
            }
        },
        {"source": {"type": "board", "board": {"type_id": "weather"}, "shuffle": "false"}},
        {"enabled": "false"},
        {"frame": {"replace_previous": "no"}},
    ],
)
def test_flags_must_be_real_bools(change) -> None:
    with pytest.raises(SpecError):
        parse_spec(spec(**change))


UNSAFE_URLS = [
    "https://127.0.0.1/x.jpg",
    "https://192.168.1.5/x.jpg",
    "https://[::1]/x.jpg",
    "https://169.254.169.254/x",
    "https://localhost/x.jpg",
    "https://api.localhost/x.jpg",
    "https://[fe80::1]/x.jpg",
    "https://[::ffff:127.0.0.1]/x.jpg",
    "https://224.0.0.1/x.jpg",
    "https://0.0.0.0/x",
    "https://10.0.0.1/x",
    "https://2130706433/x",
    "https://127.1/x",
    "https://user@127.0.0.1/x",
    "https:///x.jpg",
    "https://[bad/x",
    "http://images.example.org/x.jpg",
    "https://0x7f000001/x.jpg",
    "https://0x7f.0.0.1/x.jpg",
    "https://127.1/x.jpg",
    "https://evil.example\\@127.0.0.1/x.jpg",
    "https://100.100.100.100/x.jpg",  # CGNAT / Tailscale
    "https://[fd7a:115c:a1e0::1]/x.jpg",  # Tailscale ULA
    "https://192.0.2.1/x.jpg",  # documentation range
]


@pytest.mark.parametrize("url", UNSAFE_URLS)
def test_custom_url_rejects_unsafe_targets(url) -> None:
    with pytest.raises(SpecError):
        parse_spec(spec(source={"type": "custom_url", "custom_url": url}))


@pytest.mark.parametrize("url", ["https://8.8.8.8/x.jpg", "https://[2606:4700::1111]/x.jpg"])
def test_public_ip_literal_accepted(url) -> None:
    assert parse_spec(spec(source={"type": "custom_url", "custom_url": url}))


def test_public_https_host_accepted() -> None:
    s = parse_spec(
        spec(source={"type": "custom_url", "custom_url": "https://images.example.org/x.jpg"})
    )
    assert s.source.custom_url == "https://images.example.org/x.jpg"


@pytest.mark.parametrize("url", ["https://127.0.0.1/x.jpg", "https://localhost/x.jpg"])
def test_webcam_and_collection_urls_reject_unsafe_targets(url) -> None:
    with pytest.raises(SpecError):
        parse_spec(spec(source={"type": "webcam", "webcams": [{"url": url, "name": "n"}]}))
    with pytest.raises(SpecError):
        parse_spec(
            spec(
                source={
                    "type": "collection",
                    "images": [{"url": url, "title": "x", "proxied": False}],
                }
            )
        )


@pytest.mark.parametrize("host", ["224.0.0.1", "169.254.1.1"])
def test_tv_host_rejects_multicast_and_link_local(host) -> None:
    with pytest.raises(SpecError):
        parse_spec(spec(tv__host=host))


def test_tv_host_allows_lan_address() -> None:
    assert parse_spec(spec(tv__host="192.168.0.21")).tv.host == "192.168.0.21"


def test_collection_over_500_images_rejected() -> None:
    images = [
        {"url": f"https://a.example.org/{i}.jpg", "title": "t", "proxied": False}
        for i in range(501)
    ]
    with pytest.raises(SpecError):
        parse_spec(spec(source={"type": "collection", "images": images}))


def test_webcams_over_50_rejected() -> None:
    cams = [{"url": f"https://cam.example.org/{i}.jpg", "name": "n"} for i in range(51)]
    with pytest.raises(SpecError):
        parse_spec(spec(source={"type": "webcam", "webcams": cams}))


def test_board_params_over_20_keys_rejected() -> None:
    params = {f"k{i}": "v" for i in range(21)}
    with pytest.raises(SpecError):
        parse_spec(
            spec(source={"type": "board", "board": {"type_id": "weather", "params": params}})
        )


def test_board_param_length_limits() -> None:
    with pytest.raises(SpecError):
        parse_spec(
            spec(
                source={"type": "board", "board": {"type_id": "weather", "params": {"k" * 65: "v"}}}
            )
        )
    with pytest.raises(SpecError):
        parse_spec(
            spec(
                source={
                    "type": "board",
                    "board": {"type_id": "weather", "params": {"k": "v" * 513}},
                }
            )
        )


def test_cast_kind_with_webcam_source_parses() -> None:
    s = parse_spec(
        spec(
            tv__kind="cast",
            source={
                "type": "webcam",
                "webcams": [{"url": "https://cam.example.org/1.jpg", "name": "Harbour"}],
            },
        )
    )
    assert s.tv.kind == "cast"
    assert s.source.webcams[0].name == "Harbour"
    assert s.source.items_key


def test_invalid_orientation_rejected() -> None:
    with pytest.raises(SpecError):
        parse_spec(
            spec(
                source={"type": "board", "board": {"type_id": "weather"}, "orientation": "diagonal"}
            )
        )


def test_board_without_type_id_rejected() -> None:
    with pytest.raises(SpecError):
        parse_spec(spec(source={"type": "board", "board": {"params": {}}}))


# --- group pins ---------------------------------------------------------------

GROUP = {
    "type": "group",
    "group": {
        "items": [
            {"type": "board", "board": {"type_id": "weather", "params": {"location": "Lisbon"}}},
            {"type": "webcam", "url": "https://cams.example.com/a.jpg", "name": "Harbour"},
            {"type": "custom_url", "url": "https://example.com/pic.jpg", "name": "My link"},
        ]
    },
    "shuffle": True,
}


def group(**changes):
    raw = spec(source=copy.deepcopy(GROUP))
    for path, value in changes.items():
        node = raw["source"]
        keys = path.split("__")
        for k in keys[:-1]:
            node = node[int(k)] if isinstance(node, list) else node[k]
        if value is ...:
            node.pop(keys[-1], None)
        else:
            node[keys[-1]] = value
    return raw


def test_parses_group_in_the_phones_order() -> None:
    s = parse_spec(group())
    items = s.source.items
    assert [i.type for i in items] == ["board", "webcam", "custom_url"]
    assert items[0].board.type_id == "weather" and items[0].board.params == {"location": "Lisbon"}
    assert items[1].url == "https://cams.example.com/a.jpg" and items[1].label == "Harbour"
    assert items[0].label == "weather"
    assert s.source.shuffle is True and s.source.items_key


def test_group_items_key_changes_with_items_not_with_names() -> None:
    k1 = parse_spec(group()).source.items_key
    assert parse_spec(group(group__items__2__name="Renamed")).source.items_key == k1
    raw = group()
    raw["source"]["group"]["items"][0]["board"]["params"] = {"location": "Porto"}
    assert parse_spec(raw).source.items_key != k1
    raw = group()
    raw["source"]["group"]["items"].reverse()
    assert parse_spec(raw).source.items_key != k1


def test_group_sources_compare_by_value() -> None:
    assert parse_spec(group()).source == parse_spec(group()).source
    assert hash(parse_spec(group()).source) == hash(parse_spec(group()).source)


def test_group_item_cap() -> None:
    item = {"type": "board", "board": {"type_id": "weather"}}
    assert len(parse_spec(group(group__items=[item] * 100)).source.items) == 100
    with pytest.raises(SpecError):
        parse_spec(group(group__items=[item] * 101))
    with pytest.raises(SpecError):
        parse_spec(group(group__items=[]))
    with pytest.raises(SpecError):
        parse_spec(group(group=...))


@pytest.mark.parametrize(
    "item",
    [
        {"type": "image", "url": "https://example.com/a.jpg", "name": "IMG_1"},
        {"type": "collection", "images": []},
        {"type": "group", "group": {"items": []}},
        {"type": "board"},
        {"type": "board", "board": {"type_id": ""}},
        {"type": "board", "board": {"type_id": "weather"}, "url": "https://example.com/a.jpg"},
        {"type": "board", "board": {"type_id": "weather"}, "name": "Weather"},
        {"type": "board", "board": {"type_id": "weather", "params": {"a": 1}}},
        {"type": "webcam", "url": "https://example.com/a.jpg"},
        {"type": "webcam", "url": "https://example.com/a.jpg", "name": ""},
        {"type": "webcam", "url": "http://example.com/a.jpg", "name": "Cam"},
        {"type": "webcam", "url": "https://192.168.1.2/a.jpg", "name": "Cam"},
        {"type": "custom_url", "name": "Link"},
        {"type": "custom_url", "url": "https://localhost/a.jpg", "name": "Link"},
        {"type": "custom_url", "url": "https://x.org/a", "name": "L", "board": {"type_id": "w"}},
        "https://example.com/a.jpg",
        None,
    ],
)
def test_bad_group_items_rejected(item) -> None:
    raw = group()
    raw["source"]["group"]["items"][0] = item
    with pytest.raises(SpecError):
        parse_spec(raw)


@pytest.mark.parametrize("orientation", ["landscape", "portrait"])
def test_group_has_no_orientation(orientation) -> None:
    with pytest.raises(SpecError):
        parse_spec(group(orientation=orientation))
    assert parse_spec(group(orientation=None)).source.orientation is None
