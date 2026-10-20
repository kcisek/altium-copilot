import math
import struct

import pytest

from server.parsers.pcb_doc import (
    NO_NET,
    PAD_SHAPE_ROUNDRECT,
    PcbData,
    decode_arcs,
    decode_board,
    decode_components,
    decode_nets,
    decode_pads,
    decode_polygons,
    decode_tracks,
    decode_vias,
    decode_xsignals,
    iter_binary_records,
    parse_dim_mil,
    decode_classes,
    decode_comments,
    decode_designators,
    decode_diff_pairs,
    decode_drc_rule_kinds,
    decode_drc_options,
    DrcOptions,
    decode_rules,
    parse_streams,
    read_kind_prefixed_text_records,
    read_text_records,
    decode_wide_strings,
)
from server.parsers.pcb_doc import _build_layer_aliases


def _text_record(props: dict[str, str]) -> bytes:
    body = (
        ("|" + "|".join(f"{k}={v}" for k, v in props.items())).encode("latin-1")
        + b"\x00"
    )
    return struct.pack("<I", len(body)) + body


def test_read_text_records_parses_props():
    data = _text_record({"NAME": "GND"}) + _text_record({"NAME": "3V3", "X": "5"})
    recs = read_text_records(data)
    assert recs == [{"NAME": "GND"}, {"NAME": "3V3", "X": "5"}]


def test_read_text_records_stops_on_truncated_record():
    data = _text_record({"NAME": "GND"}) + struct.pack("<I", 9999) + b"|A=B"
    assert read_text_records(data) == [{"NAME": "GND"}]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("5mil", 5.0),
        ("2.7559mil", 2.7559),
        ("0.127mm", 5.0),
        ("1000", 1000.0),
        (" 1.80000000000000E+0002", 180.0),
    ],
)
def test_parse_dim_mil(raw, expected):
    assert parse_dim_mil(raw) == pytest.approx(expected, abs=1e-3)


def test_parse_dim_mil_invalid_returns_none():
    assert parse_dim_mil("") is None
    assert parse_dim_mil("abc") is None
    assert parse_dim_mil(None) is None


def _bin_record(record_type: int, payload: bytes) -> bytes:
    return bytes([record_type]) + struct.pack("<I", len(payload)) + payload


def _track_payload(
    layer=1,
    net=0,
    component=0xFFFF,
    x1=1000_0000,
    y1=1000_0000,
    x2=1050_0000,
    y2=1000_0000,
    width=6_0000,
) -> bytes:
    payload = bytearray(49)
    payload[0] = layer
    struct.pack_into("<H", payload, 3, net)
    struct.pack_into("<H", payload, 7, component)
    struct.pack_into("<iiiii", payload, 13, x1, y1, x2, y2, width)
    return bytes(payload)


def _via_payload(layer_from=1, layer_to=32) -> bytes:
    payload = bytearray(80)
    struct.pack_into("<H", payload, 3, 5)
    struct.pack_into("<H", payload, 7, 0xFFFF)
    struct.pack_into(
        "<iiii",
        payload,
        13,
        200_0000,
        300_0000,
        20_0000,
        8_0000,
    )
    payload[29], payload[30] = layer_from, layer_to
    return bytes(payload)


def _arc_payload(layer=1, radius=1_0000) -> bytes:
    payload = bytearray(45)
    payload[0] = layer
    struct.pack_into("<H", payload, 3, 2)
    struct.pack_into("<ii", payload, 13, 100_0000, 100_0000)
    struct.pack_into("<i", payload, 21, radius)
    struct.pack_into("<dd", payload, 25, 0.0, 90.0)
    struct.pack_into("<i", payload, 41, 2000)
    return bytes(payload)


def test_iter_binary_records_frames_correctly():
    data = _bin_record(4, b"\x01" * 49) + _bin_record(4, b"\x02" * 49)
    records = list(iter_binary_records(data))
    assert len(records) == 2
    assert records[0] == (4, b"\x01" * 49)


def test_iter_binary_records_stops_on_truncation():
    data = _bin_record(4, b"\x01" * 49) + b"\x04" + struct.pack("<I", 9999)
    assert len(list(iter_binary_records(data))) == 1


def test_decode_tracks():
    data = _bin_record(4, _track_payload(layer=3, net=7, width=6_0000))
    primitives, skipped = decode_tracks(data)
    assert skipped == 0
    track = primitives[0]
    assert (track.kind, track.layer, track.net, track.width) == (
        "track",
        3,
        7,
        6.0,
    )
    assert (track.x1, track.y1, track.x2, track.y2) == (
        1000.0,
        1000.0,
        1050.0,
        1000.0,
    )


def test_decode_tracks_no_net_and_short_payload_skipped():
    good = _bin_record(4, _track_payload(net=NO_NET))
    short = _bin_record(4, b"\x01" * 10)
    primitives, skipped = decode_tracks(good + short)
    assert primitives[0].net is None
    assert skipped == 1


def test_decode_tracks_out_of_range_layer_skipped():
    primitives, skipped = decode_tracks(_bin_record(4, _track_payload(layer=0)))
    assert primitives == []
    assert skipped == 1


def test_decode_tracks_rejects_wrong_type_and_nonpositive_width():
    wrong_type = _bin_record(3, _track_payload())
    negative_width = _bin_record(4, _track_payload(width=-1))
    primitives, skipped = decode_tracks(wrong_type + negative_width)
    assert primitives == []
    assert skipped == 2


def test_decode_vias():
    primitives, skipped = decode_vias(_bin_record(3, _via_payload()))
    via = primitives[0]
    assert (via.kind, via.net, via.layer, via.layer_to) == ("via", 5, 1, 32)
    assert (via.x1, via.y1, via.width) == (200.0, 300.0, 20.0)
    assert via.hole == 8.0
    assert skipped == 0


def test_decode_vias_out_of_range_layer_skipped():
    primitives, skipped = decode_vias(
        _bin_record(3, _via_payload(layer_from=200))
    )
    assert primitives == []
    assert skipped == 1


def test_decode_arcs_explodes_to_segments():
    primitives, skipped = decode_arcs(_bin_record(1, _arc_payload()))
    assert skipped == 0
    assert all(segment.kind == "arc" for segment in primitives)
    assert {segment.source_id for segment in primitives} == {0}
    assert len(primitives) >= 2
    first, last = primitives[0], primitives[-1]
    assert (first.x1, first.y1) == (
        pytest.approx(101.0),
        pytest.approx(100.0),
    )
    assert (last.x2, last.y2) == (
        pytest.approx(100.0),
        pytest.approx(101.0),
    )


def test_decode_arcs_out_of_range_layer_skipped():
    primitives, skipped = decode_arcs(_bin_record(1, _arc_payload(layer=200)))
    assert primitives == []
    assert skipped == 1


def test_decode_arcs_skips_nan_record_without_losing_healthy_records():
    malformed = bytearray(_arc_payload())
    struct.pack_into("<d", malformed, 25, float("nan"))
    data = _bin_record(1, bytes(malformed)) + _bin_record(1, _arc_payload())
    primitives, skipped = decode_arcs(data)
    assert skipped == 1
    assert primitives


def test_decode_arcs_adapts_chords_for_large_radius_length_accuracy():
    primitives, skipped = decode_arcs(
        _bin_record(1, _arc_payload(radius=1000_0000))
    )
    length = sum(
        math.hypot(segment.x2 - segment.x1, segment.y2 - segment.y1)
        for segment in primitives
    )
    assert skipped == 0
    assert length == pytest.approx(math.pi * 500.0, abs=0.5)


def _pad_record(
    name="1",
    layer=1,
    net=3,
    component=5,
    x=150_0000,
    y=250_0000,
    sx=2_4000,
    sy=3_5000,
    hole=0,
    rotation=90.0,
    shape=2,
    extra_sub=b"",
    mode=0,
    bottom_size=None,
    bottom_shape=None,
    mid_size=None,
    mid_shape=None,
) -> bytes:
    name_bytes = name.encode("latin-1")
    name_blob = bytes([len(name_bytes)]) + name_bytes
    main = bytearray(185)
    main[0] = layer
    struct.pack_into("<H", main, 3, net)
    struct.pack_into("<H", main, 7, component)
    struct.pack_into("<ii", main, 13, x, y)
    bottom_x, bottom_y = bottom_size or (sx, sy)
    mid_x, mid_y = mid_size or (sx, sy)
    struct.pack_into(
        "<iiiiiii", main, 21, sx, sy, mid_x, mid_y, bottom_x, bottom_y, hole
    )
    main[49] = shape
    main[50] = shape if mid_shape is None else mid_shape
    main[51] = shape if bottom_shape is None else bottom_shape
    struct.pack_into("<d", main, 52, rotation)
    main[62] = mode
    subrecords = [b"\x00", b"\x00" * 5, b"\x00", bytes(main), extra_sub]
    body = struct.pack("<I", len(name_blob)) + name_blob
    for subrecord in subrecords:
        body += struct.pack("<I", len(subrecord)) + subrecord
    return b"\x02" + body


def test_decode_pads():
    data = _pad_record(name="A7", net=3, component=5, rotation=90.0)
    primitives, skipped = decode_pads(data)
    assert skipped == 0
    pad = primitives[0]
    assert (pad.kind, pad.pad_name, pad.layer, pad.net, pad.component) == (
        "pad",
        "A7",
        1,
        3,
        5,
    )
    assert (pad.x1, pad.y1) == (150.0, 250.0)
    assert (pad.width, pad.height) == (2.4, 3.5)
    assert (pad.shape, pad.rotation) == (2, 90.0)
    assert pad.hole == 0.0


def test_decode_pads_with_extended_subrecord():
    data = _pad_record(extra_sub=b"\x00" * 651) + _pad_record(name="2")
    primitives, skipped = decode_pads(data)
    assert [pad.pad_name for pad in primitives] == ["1", "2"]
    assert skipped == 0


def test_decode_pads_short_main_subrecord_skipped():
    name_blob = b"\x011"
    body = struct.pack("<I", len(name_blob)) + name_blob
    for subrecord in [b"\x00", b"\x00" * 5, b"\x00", b"\x00" * 20, b""]:
        body += struct.pack("<I", len(subrecord)) + subrecord
    primitives, skipped = decode_pads(b"\x02" + body)
    assert primitives == []
    assert skipped == 1


def test_decode_pads_out_of_range_layer_skipped():
    primitives, skipped = decode_pads(_pad_record(layer=200))
    assert primitives == []
    assert skipped == 1


def _size_and_shape(alt=None, radius=None, length=651, inner=None) -> bytes:
    """A size-and-shape subrecord: {slot: alt shape} at 532 and {slot:
    corner radius percent} at 564 (slot 0 top, 31 bottom), and {k: (x, y,
    shape)} of mid layer k + 2 (x at 4k, y at 116 + 4k, shape at 232 + k)."""
    record = bytearray(length)
    for k, (size_x, size_y, shape) in (inner or {}).items():
        struct.pack_into("<i", record, 4 * k, size_x)
        struct.pack_into("<i", record, 116 + 4 * k, size_y)
        record[232 + k] = shape
    for slot, value in (alt or {}).items():
        record[532 + slot] = value
    for slot, value in (radius or {}).items():
        record[564 + slot] = value
    return bytes(record)


def test_decode_pads_rounded_rectangle():
    data = _pad_record(
        sx=40_0000,
        sy=60_0000,
        shape=1,
        extra_sub=_size_and_shape(alt={0: 9}, radius={0: 25}),
    )
    (pad,), skipped = decode_pads(data)
    assert skipped == 0
    assert (pad.shape, pad.width, pad.height) == (
        PAD_SHAPE_ROUNDRECT,
        40.0,
        60.0,
    )
    # 25 % of half the shorter side.
    assert pad.corner_radius == pytest.approx(5.0)


@pytest.mark.parametrize(
    "shape, size_and_shape",
    [
        (1, _size_and_shape(alt={0: 1})),
        # Only a round main shape becomes a rounded rectangle (as in KiCad).
        (2, _size_and_shape(alt={0: 9}, radius={0: 25})),
        # Too short to hold the alt shapes.
        (1, _size_and_shape(alt={0: 9}, radius={0: 25}, length=595)),
        (1, b""),
    ],
    ids=["round", "rect", "short-subrecord", "no-subrecord"],
)
def test_decode_pads_without_rounded_corners(shape, size_and_shape):
    (pad,), _ = decode_pads(
        _pad_record(shape=shape, extra_sub=size_and_shape)
    )
    assert (pad.shape, pad.corner_radius) == (shape, None)


def test_decode_pads_corner_radius_capped_at_oblong():
    data = _pad_record(
        sx=40_0000,
        sy=60_0000,
        shape=1,
        extra_sub=_size_and_shape(alt={0: 9}, radius={0: 150}),
    )
    (pad,), _ = decode_pads(data)
    assert pad.corner_radius == pytest.approx(20.0)


def test_decode_pads_full_corner_radius_is_exactly_half_the_short_side():
    data = _pad_record(
        sx=78,
        sy=100,
        shape=1,
        extra_sub=_size_and_shape(alt={0: 9}, radius={0: 100}),
    )
    (pad,), _ = decode_pads(data)
    assert pad.corner_radius == pad.width / 2.0


def test_decode_pads_bottom_pad_of_full_stack_uses_bottom_slot():
    # Altium leaves the top slot of such a pad empty: it was dropped before.
    data = _pad_record(
        layer=32,
        sx=0,
        sy=0,
        shape=1,
        mode=2,
        bottom_size=(40_0000, 60_0000),
        bottom_shape=1,
        extra_sub=_size_and_shape(
            alt={0: 1, 31: 9}, radius={31: 50}, length=596
        ),
    )
    (pad,), skipped = decode_pads(data)
    assert skipped == 0
    assert (pad.layer, pad.width, pad.height, pad.shape) == (
        32,
        40.0,
        60.0,
        PAD_SHAPE_ROUNDRECT,
    )
    assert pad.corner_radius == pytest.approx(10.0)


def _stack_pad(layer, mode, sx=2_4000, sy=3_5000, bottom_size=(40_0000, 60_0000)):
    """A pad whose top slot is a 2.4 x 3.5 mil round pad and whose other
    slots are rounded rectangles: mid 20 x 30 (50 %), a full stack's layer 4
    (mid layer 3) 50 x 70 (20 %), bottom 40 x 60 (50 %)."""
    return _pad_record(
        layer=layer,
        sx=sx,
        sy=sy,
        shape=1,
        mode=mode,
        mid_size=(20_0000, 30_0000),
        bottom_size=bottom_size,
        extra_sub=_size_and_shape(
            alt={0: 1, 1: 9, 3: 9, 31: 9},
            radius={1: 50, 3: 20, 31: 50},
            inner={1: (50_0000, 70_0000, 1)},
        ),
    )


@pytest.mark.parametrize(
    "layer, mode, expected",
    [
        (32, 1, (40.0, 60.0, PAD_SHAPE_ROUNDRECT, 10.0)),
        (32, 2, (40.0, 60.0, PAD_SHAPE_ROUNDRECT, 10.0)),
        # Only modes 1 and 2 keep per-layer copper.
        (32, 0, (2.4, 3.5, 1, None)),
        (32, 7, (2.4, 3.5, 1, None)),
        (1, 2, (2.4, 3.5, 1, None)),
        (74, 1, (2.4, 3.5, 1, None)),
        # Inner layers: mid slot (alt slot 1) in mode 1 and for mid layer 1.
        (5, 1, (20.0, 30.0, PAD_SHAPE_ROUNDRECT, 5.0)),
        (2, 2, (20.0, 30.0, PAD_SHAPE_ROUNDRECT, 5.0)),
        # Mid layer 3 of a full stack: inner size 1, alt slot 3.
        (4, 2, (50.0, 70.0, PAD_SHAPE_ROUNDRECT, 5.0)),
    ],
)
def test_decode_pads_per_layer_stack(layer, mode, expected):
    (pad,), skipped = decode_pads(_stack_pad(layer, mode))
    assert skipped == 0
    assert (pad.width, pad.height, pad.shape) == pytest.approx(expected[:3])
    if expected[3] is None:
        assert pad.corner_radius is None
    else:
        assert pad.corner_radius == pytest.approx(expected[3])


@pytest.mark.parametrize("mode", [1, 2])
@pytest.mark.parametrize(
    "top_shape, bottom_shape, expected",
    [(2, 1, PAD_SHAPE_ROUNDRECT), (1, 2, 2)],
)
def test_decode_pads_bottom_pad_takes_the_bottom_shape(
    mode, top_shape, bottom_shape, expected
):
    data = _pad_record(
        layer=32,
        shape=top_shape,
        mode=mode,
        bottom_size=(40_0000, 60_0000),
        bottom_shape=bottom_shape,
        extra_sub=_size_and_shape(alt={0: 9, 31: 9}, radius={0: 50, 31: 50}),
    )
    (pad,), _ = decode_pads(data)
    assert pad.shape == expected


def test_decode_pads_per_layer_stack_with_empty_slot_takes_a_filled_one():
    (bottom,), _ = decode_pads(_stack_pad(32, 1, bottom_size=(0, 0)))
    assert (bottom.width, bottom.height, bottom.corner_radius) == (
        2.4,
        3.5,
        None,
    )
    (multi,), skipped = decode_pads(_stack_pad(74, 2, sx=0, sy=0))
    assert skipped == 0
    assert (multi.width, multi.height, multi.shape) == (
        20.0,
        30.0,
        PAD_SHAPE_ROUNDRECT,
    )
    assert multi.corner_radius == pytest.approx(5.0)


def test_decode_pads_bottom_pad_of_simple_stack_uses_top_slot():
    data = _pad_record(
        layer=32,
        sx=40_0000,
        sy=60_0000,
        shape=1,
        bottom_size=(1, 1),
        bottom_shape=2,
        extra_sub=_size_and_shape(alt={0: 9, 31: 1}, radius={0: 50}),
    )
    (pad,), _ = decode_pads(data)
    assert (pad.width, pad.height, pad.shape) == (
        40.0,
        60.0,
        PAD_SHAPE_ROUNDRECT,
    )
    assert pad.corner_radius == pytest.approx(10.0)


def _pad_record_with_main(main_length: int, pin_package: int | None) -> bytes:
    """A pad record whose main subrecord has main_length bytes; pin_package
    (internal units) is written at offset 158 when given."""
    main = bytearray(main_length)
    main[0] = 1
    struct.pack_into("<H", main, 3, 3)
    struct.pack_into("<H", main, 7, 5)
    struct.pack_into("<ii", main, 13, 150_0000, 250_0000)
    struct.pack_into("<iiiiiii", main, 21, 2_4000, 3_5000, 0, 0, 0, 0, 0)
    main[49] = 2
    if pin_package is not None:
        struct.pack_into("<i", main, 158, pin_package)
    name_blob = b"\x011"
    body = struct.pack("<I", len(name_blob)) + name_blob
    for subrecord in [b"\x00", b"\x00" * 5, b"\x00", bytes(main), b""]:
        body += struct.pack("<I", len(subrecord)) + subrecord
    return b"\x02" + body


def test_decode_pads_pin_package_length():
    # 194- and 202-byte main subrecords: int32 at offset 158, 1e-4 mil.
    for main_length in (194, 202):
        primitives, skipped = decode_pads(_pad_record_with_main(main_length, 1234_5000))
        assert skipped == 0
        assert primitives[0].pin_package == pytest.approx(1234.5)
    # Unset is 0; the 185-byte layout carries the field too.
    assert decode_pads(_pad_record())[0][0].pin_package == 0.0


def test_decode_pads_pin_package_absent_or_invalid():
    # Too short to hold the field, or a negative (meaningless) value.
    short, _ = decode_pads(_pad_record_with_main(120, None))
    assert short[0].pin_package is None
    negative, _ = decode_pads(_pad_record_with_main(194, -5))
    assert negative[0].pin_package is None


BOARD_PROPS = {
    "ORIGINX": "7283.4648mil",
    "ORIGINY": "7775.5913mil",
    "SHEETX": "1000mil",
    "SHEETY": "1000mil",
    "SHEETWIDTH": "10000mil",
    "SHEETHEIGHT": "8000mil",
    "DISPLAYUNIT": "1",
    "LAYER1NAME": "Top Layer",
    "LAYER2NAME": "L2-GND",
    "LAYER32NAME": "Bottom Layer",
    "LAYER33NAME": "Top Overlay",
    "V9_STACK_LAYER0_NAME": "Top Layer",
    "V9_STACK_LAYER0_LAYERID": "16777217",
    "V9_STACK_LAYER0_COPTHICK": "2.7559mil",
    "V9_STACK_LAYER1_NAME": "Dielectric1",
    "V9_STACK_LAYER1_LAYERID": "17039361",
    "V9_STACK_LAYER1_DIELTYPE": "1",
    "V9_STACK_LAYER1_DIELCONST": "3.800",
    "V9_STACK_LAYER1_DIELHEIGHT": "5mil",
    "V9_STACK_LAYER1_DIELMATERIAL": "Isola FR370HR",
    "V9_STACK_LAYER2_NAME": "L2-GND",
    "V9_STACK_LAYER2_LAYERID": "16777218",
    "V9_STACK_LAYER2_COPTHICK": "2.7559mil",
}


def test_decode_board_stackup_and_origin():
    board = decode_board([BOARD_PROPS])
    assert board.origin_x == pytest.approx(7283.4648)
    assert board.display_unit == "mil"
    top = next(layer for layer in board.layers if layer.name == "Top Layer")
    assert (top.v6_id, top.kind, top.stack_order) == (1, "copper", 0)
    assert top.copper_thick_mil == pytest.approx(2.7559)
    dielectric = next(
        layer for layer in board.layers if layer.name == "Dielectric1"
    )
    assert (
        dielectric.kind,
        dielectric.diel_const,
        dielectric.material,
    ) == ("dielectric", 3.8, "Isola FR370HR")
    assert dielectric.diel_height_mil == pytest.approx(5.0)
    ground = next(layer for layer in board.layers if layer.name == "L2-GND")
    assert (ground.v6_id, ground.stack_order) == (2, 2)
    overlay = next(
        layer for layer in board.layers if layer.name == "Top Overlay"
    )
    assert (overlay.v6_id, overlay.kind, overlay.stack_order) == (
        33,
        "non-stack",
        None,
    )


def test_decode_board_legacy_fallback():
    board = decode_board(
        [{"LAYER1NAME": "Top Layer", "LAYER2NAME": "Bottom Layer"}]
    )
    assert board.stackup_source == "legacy"
    assert [layer.name for layer in board.layers][:2] == [
        "Top Layer",
        "Bottom Layer",
    ]


def test_decode_board_outline_vertices():
    board = decode_board([
        {
            **BOARD_PROPS,
            "VX0": "10mil",
            "VY0": "20mil",
            "VX1": "110mil",
            "VY1": "20mil",
            "VX2": "110mil",
            "VY2": "220mil",
        }
    ])
    assert board.outline_vertices == [
        (10.0, 20.0),
        (110.0, 20.0),
        (110.0, 220.0),
    ]


def test_decode_nets():
    assert decode_nets([{"NAME": "GND"}, {"NAME": "3V3"}]) == ["GND", "3V3"]


def test_decode_components():
    components = decode_components(
        [
            {
                "SOURCEDESIGNATOR": "U3",
                "SOURCEUNIQUEID": "\\TTMAPHZK\\HBSRZYDI\\NWGGFMAY",
                "SOURCEHIERARCHICALPATH": "top_sheet\\Voltage Sense\\Top",
                "X": "12952.756mil",
                "Y": "13425.1969mil",
                "ROTATION": " 1.80000000000000E+0002",
                "LAYER": "TOP",
            },
            {
                "LAYER": "BOTTOM",
            },
        ]
    )
    u3 = components[0]
    assert (u3.index, u3.refdes, u3.side, u3.rotation) == (
        0,
        "U3",
        "top",
        180.0,
    )
    assert u3.source_uid_tail == "NWGGFMAY"
    assert u3.channel_path == "top_sheet\\Voltage Sense\\Top"
    assert u3.x == pytest.approx(12952.756)
    assert (components[1].refdes, components[1].side) == (None, "bottom")


def test_decode_polygons():
    pours, edges = decode_polygons(
        [
            {
                "LAYER": "TOP",
                "NET": "5",
                "HATCHSTYLE": "Solid",
                "VX0": "0mil",
                "VY0": "0mil",
                "VX1": "100mil",
                "VY1": "0mil",
                "VX2": "100mil",
                "VY2": "50mil",
            }
        ],
        layer_name_to_v6={"top layer": 1, "top": 1},
    )
    assert pours[0].net == 5
    assert pours[0].layer == 1
    assert pours[0].vertices == [
        (0.0, 0.0),
        (100.0, 0.0),
        (100.0, 50.0),
    ]
    assert len(edges) == 3
    assert all(
        edge.kind == "track" and edge.pour_id == 0 and edge.width == 0.0
        for edge in edges
    )
    assert (edges[-1].x1, edges[-1].y1, edges[-1].x2, edges[-1].y2) == (
        100.0,
        50.0,
        0.0,
        0.0,
    )


def test_build_layer_aliases_resolves_mid_layer_positional_names():
    # Polygons6 (and other text streams) reference inner copper layers by
    # Altium's fixed positional alias (MID1, MID2, ...), not by LAYERnNAME —
    # regression test for a board where an L2-GND pour's layer failed to
    # resolve because only "top"/"bottom" fallbacks existed.
    board = decode_board([BOARD_PROPS])
    aliases = _build_layer_aliases(board)
    assert aliases["mid1"] == 2
    assert aliases["l2-gnd"] == 2
    assert aliases["mid1"] == aliases["l2-gnd"]
    assert aliases["mid30"] == 31


def test_decode_polygons_resolves_inner_layer_via_mid_alias():
    board = decode_board([BOARD_PROPS])
    aliases = _build_layer_aliases(board)
    pours, edges = decode_polygons(
        [
            {
                "LAYER": "MID1",
                "NET": "0",
                "VX0": "0mil", "VY0": "0mil",
                "VX1": "10mil", "VY1": "0mil",
                "VX2": "10mil", "VY2": "10mil",
            }
        ],
        layer_name_to_v6=aliases,
    )
    assert pours[0].layer == 2
    assert len(edges) == 3  # proximity edges must be built, not silently dropped


def _stream_text(records: list[dict[str, str]]) -> bytes:
    return b"".join(_text_record(record) for record in records)


def test_parse_streams_assembles_pcbdata():
    streams = {
        "Board6/Data": _stream_text([BOARD_PROPS]),
        "Nets6/Data": _stream_text([{"NAME": "GND"}, {"NAME": "CLK"}]),
        "Components6/Data": _stream_text(
            [
                {
                    "SOURCEDESIGNATOR": "R1",
                    "X": "0mil",
                    "Y": "0mil",
                    "LAYER": "TOP",
                }
            ]
        ),
        "Tracks6/Data": _bin_record(
            4,
            _track_payload(layer=1, net=1, component=0),
        ),
        "Vias6/Data": b"",
        "Arcs6/Data": b"",
        "Pads6/Data": _pad_record(name="1", layer=1, net=0, component=0),
        "Polygons6/Data": _stream_text(
            [
                {
                    "LAYER": "TOP",
                    "NET": "0",
                    "VX0": "0mil",
                    "VY0": "0mil",
                    "VX1": "10mil",
                    "VY1": "0mil",
                    "VX2": "10mil",
                    "VY2": "10mil",
                }
            ]
        ),
    }
    pcb = parse_streams(streams)
    assert isinstance(pcb, PcbData)
    assert pcb.nets == ["GND", "CLK"]
    assert pcb.components[0].refdes == "R1"
    assert {primitive.kind for primitive in pcb.primitives} == {
        "track",
        "pad",
    }
    assert len(pcb.pours) == 1
    assert pcb.skipped_records == {}
    assert pcb.warnings == []


def test_parse_streams_missing_stream_warns_but_continues():
    streams = {
        "Board6/Data": _stream_text([BOARD_PROPS]),
        "Nets6/Data": _stream_text([{"NAME": "GND"}]),
    }
    pcb = parse_streams(streams)
    assert pcb.nets == ["GND"]
    assert any("Tracks6" in warning for warning in pcb.warnings)


def test_parse_streams_isolates_invalid_board_text_values():
    streams = {
        "Board6/Data": _stream_text([
            {
                **BOARD_PROPS,
                "V9_STACK_LAYER1_DIELCONST": "not-a-number",
            }
        ]),
        "Nets6/Data": _stream_text([{"NAME": "GND"}]),
        "Components6/Data": _stream_text([]),
    }
    pcb = parse_streams(streams)
    assert pcb.nets == ["GND"]
    assert any(
        "Board6 parse failed" in warning
        for warning in pcb.warnings
    )


def test_parse_streams_drops_out_of_range_net_references():
    streams = {
        "Board6/Data": _stream_text([BOARD_PROPS]),
        "Nets6/Data": _stream_text([{"NAME": "GND"}]),
        "Components6/Data": b"",
        "Tracks6/Data": _bin_record(4, _track_payload(net=9)),
        "Vias6/Data": b"",
        "Arcs6/Data": b"",
        "Pads6/Data": b"",
        "Polygons6/Data": b"",
    }
    pcb = parse_streams(streams)
    assert pcb.primitives == []
    assert pcb.skipped_records["net_range"] == 1


# ---------- Rules6 / Classes6 / SignalClasses / DifferentialPairs6 ----------

def _rule_record(props: dict[str, str], kind_id: int = 0) -> bytes:
    return struct.pack("<H", kind_id) + _text_record(props)


RULE_GENERIC = {
    "SELECTION": "FALSE", "LAYER": "TOP", "LOCKED": "FALSE",
    "POLYGONOUTLINE": "FALSE", "USERROUTED": "TRUE", "KEEPOUT": "FALSE",
    "UNIONINDEX": "0", "NETSCOPE": "AnyNet", "LAYERKIND": "SameLayer",
    "SCOPE2EXPRESSION": "All", "COMMENT": "", "UNIQUEID": "ABCDEFGH",
    "DEFINEDBYLOGICALDOCUMENT": "FALSE",
}


def _rule_props(**props) -> dict[str, str]:
    return {**RULE_GENERIC, **props}


def test_rules6_records_carry_a_kind_prefix():
    data = _rule_record(
        _rule_props(RULEKIND="Width", NAME="Width", SCOPE1EXPRESSION="All",
                    ENABLED="TRUE", PRIORITY="1", MINLIMIT="10mil"),
        kind_id=2,
    ) + _rule_record(
        _rule_props(RULEKIND="Clearance", NAME="Clr", ENABLED="FALSE",
                    PRIORITY="2", SCOPE1EXPRESSION="(ObjectKind = 'Text')"),
        kind_id=0,
    )
    # The plain reader takes kind+length as one huge length and gives up.
    assert read_text_records(data) == []
    records, trailing = read_kind_prefixed_text_records(data)
    assert trailing == 0
    assert [kind_id for kind_id, _ in records] == [2, 0]
    rules = decode_rules(records)
    width, clearance = rules
    assert (width.kind, width.priority, width.enabled) == ("Width", 1, True)
    assert width.properties == {"MINLIMIT": "10mil"}
    assert clearance.enabled is False
    # '=' inside a scope survives the KEY=VALUE split.
    assert clearance.scope1 == "(ObjectKind = 'Text')"


def test_decode_rules_keeps_room_side_and_defaults():
    rules = decode_rules([
        (22, {"RULEKIND": "RoomDefinition", "NAME": "RF", "LAYER": "BOTTOM",
              "PRIORITY": "x", "VX0": "0mil"}),
        (99, {"NAME": "Mystery"}),
    ])
    room, unknown = rules
    assert room.properties["LAYER"] == "BOTTOM"
    assert room.priority == 1_000_000
    assert room.scope1 == "All"
    assert unknown.kind == "Kind99"


def test_decode_classes_members_superclass_and_kinds():
    records = [
        {"NAME": "All Nets", "KIND": "0", "SUPERCLASS": "TRUE"},
        {"NAME": "Clocks", "KIND": "0", "SUPERCLASS": "FALSE",
         **{f"M{i}": f"N{i}" for i in range(12)}, "M12": "N3"},
        {"NAME": "Top Side Components", "KIND": "1", "AUTOGENERATEDCLASS": "TRUE",
         "M0": "U1"},
        {"NAME": "Sig", "KIND": "10", "M0": "CK_PP1"},
        {"NAME": "Odd", "KIND": "42"},
    ]
    all_nets, clocks, top_side, xsignals, odd = decode_classes(records)
    assert all_nets.superclass and all_nets.members == []
    # M10 sorts after M9 (numeric order), and duplicates collapse.
    assert clocks.members == [f"N{i}" for i in range(12)]
    assert top_side.kind == "component" and top_side.auto_generated
    assert xsignals.kind == "xsignal"
    assert odd.kind == "unknown_42"


def test_decode_diff_pairs():
    pairs = decode_diff_pairs([
        {"NAME": "USB_D", "POSITIVENETNAME": "USB_D_P",
         "NEGATIVENETNAME": "USB_D_N", "UNIQUEID": "RNNVMXIH"},
        {"POSITIVENETNAME": "X"},
    ])
    assert len(pairs) == 1
    assert (pairs[0].positive_net, pairs[0].negative_net) == ("USB_D_P", "USB_D_N")


def _minimal_streams() -> dict[str, bytes]:
    return {
        "Board6/Data": _stream_text([BOARD_PROPS]),
        "Nets6/Data": _stream_text([{"NAME": "GND"}]),
        "Components6/Data": b"",
        "Tracks6/Data": b"",
        "Vias6/Data": b"",
        "Arcs6/Data": b"",
        "Pads6/Data": b"",
        "Polygons6/Data": b"",
    }


def test_parse_streams_rules_classes_pairs_are_optional():
    pcb = parse_streams(_minimal_streams())
    assert pcb.rules == [] and pcb.classes == [] and pcb.diff_pairs == []
    assert pcb.warnings == []


def test_parse_streams_reads_rules_classes_and_pairs():
    streams = _minimal_streams()
    streams["Rules6/Data"] = _rule_record(
        _rule_props(RULEKIND="Width", NAME="W", PRIORITY="1",
                    SCOPE1EXPRESSION="All", ENABLED="TRUE"),
        kind_id=2,
    )
    streams["Rules6/Header"] = struct.pack("<I", 3)
    streams["Classes6/Data"] = _stream_text([{"NAME": "HS", "KIND": "6", "M0": "USB_D"}])
    streams["SignalClasses/Data"] = _stream_text([{"NAME": "X", "KIND": "10"}])
    streams["DifferentialPairs6/Data"] = _stream_text([
        {"NAME": "USB_D", "POSITIVENETNAME": "USB_D_P", "NEGATIVENETNAME": "USB_D_N"}
    ])
    pcb = parse_streams(streams)
    assert [rule.name for rule in pcb.rules] == ["W"]
    assert [c.kind for c in pcb.classes] == ["diff_pair", "xsignal"]
    assert pcb.diff_pairs[0].name == "USB_D"
    # Header says 3 records but only 1 decoded.
    assert pcb.skipped_records["Rules6"] == 2
    assert pcb.warnings == []


def test_decode_components_keeps_footprint():
    components = decode_components([{"SOURCEDESIGNATOR": "U1", "PATTERN": "QFN32"}])
    assert components[0].footprint == "QFN32"


def _text6_record(component: int, text: str, designator: bool = True) -> bytes:
    first = bytearray(60)
    struct.pack_into("<H", first, 7, component)
    first[41] = 1 if designator else 0
    second = bytes([len(text)]) + text.encode("latin-1")
    return (
        b"\x05"
        + struct.pack("<I", len(first)) + bytes(first)
        + struct.pack("<I", len(second)) + second
    )


def test_decode_designators_reads_only_component_designators():
    data = (
        _text6_record(0, "U3")
        + _text6_record(1, "Comment text", designator=False)
        + _text6_record(0xFFFF, "Logo")
        + _text6_record(1, "R7")
    )
    assert decode_designators(data) == {0: "U3", 1: "R7"}


def test_parse_streams_attaches_pcb_designators():
    streams = _minimal_streams()
    streams["Components6/Data"] = _stream_text([
        {"SOURCEDESIGNATOR": "U1"}, {"SOURCEDESIGNATOR": "U2"},
    ])
    # Board-level annotation swapped the board designators.
    streams["Texts6/Data"] = _text6_record(0, "U2") + _text6_record(1, "U1")
    pcb = parse_streams(streams)
    assert [(c.refdes, c.designator) for c in pcb.components] == [
        ("U1", "U2"), ("U2", "U1"),
    ]
    assert pcb.warnings == []


def test_decode_board_reads_substacks():
    board = decode_board([{
        **BOARD_PROPS,
        "V9_SUBSTACK0_ID": "{A}", "V9_SUBSTACK0_NAME": "Rigid",
        "V9_SUBSTACK0_ISFLEX": "FALSE",
        "V9_SUBSTACK1_ID": "{B}", "V9_SUBSTACK1_NAME": "Flex",
        "V9_SUBSTACK1_ISFLEX": "TRUE",
    }])
    assert board.substacks == [
        {"id": "{A}", "name": "Rigid", "is_flex": False},
        {"id": "{B}", "name": "Flex", "is_flex": True},
    ]


def test_decode_xsignals_from_board_record():
    xsignals = decode_xsignals({
        "PINPAIRCOUNT": "2",
        "PINPAIRNAME0": "DQ0_PP1", "PINPAIRPINS0": "U1-W1, R4-1,R4-2,J1-3",
        "PINPAIRNAME1": "EMPTY", "PINPAIRPINS1": "",
    })
    assert [(x.name, x.pins) for x in xsignals] == [
        ("DQ0_PP1", ["U1-W1", "R4-1", "R4-2", "J1-3"])
    ]
    assert decode_xsignals({}) == []


def test_decode_drc_rule_kinds():
    assert decode_drc_rule_kinds([{"RULESETTOCHECK": "0,2,11,51"}]) == {0, 2, 11, 51}
    assert decode_drc_rule_kinds([{}]) is None


def test_decode_drc_options():
    options = decode_drc_options([{
        "MAXVIOLATIONCOUNT": "500", "RULESETTOCHECK": "0,15,16", "ONLINERULESETTOCHECK": "0,15",
        "DOMAKEDRCFILE": "TRUE", "INCLUDEPCBHEALTH": "FALSE", "DOSUBNETDETAILS": "TRUE",
    }])
    assert options == DrcOptions(
        max_violations=500, batch_kinds={0, 15, 16}, online_kinds={0, 15}, make_report=True,
        include_health=False, sub_net_details=True,
    )
    assert decode_drc_options([]) is None
    assert decode_drc_options([{}]) == DrcOptions()  # older files: settings unknown


def test_arc_chords_carry_true_arc_length():
    payload = bytearray(60)
    payload[0] = 1
    struct.pack_into("<H", payload, 3, 0xFFFF)
    struct.pack_into("<H", payload, 7, 0xFFFF)
    struct.pack_into("<ii", payload, 13, 0, 0)
    struct.pack_into("<i", payload, 21, 200000)
    struct.pack_into("<dd", payload, 25, 0.0, 90.0)
    struct.pack_into("<i", payload, 41, 50000)
    data = b"\x01" + struct.pack("<I", len(payload)) + bytes(payload)
    arcs, _ = decode_arcs(data)
    total = sum(arc.length for arc in arcs)
    assert total == pytest.approx(math.pi * 20 / 2, rel=1e-6)
    chords = sum(math.hypot(a.x2 - a.x1, a.y2 - a.y1) for a in arcs)
    assert chords < total


# ---------- component kind and comment (sync check) ----------

def test_decode_components_reads_kind():
    components = decode_components([
        {"SOURCEDESIGNATOR": "U1"},
        {"COMPONENTKIND": "1"},
        {"COMPONENTKIND": "0", "COMPONENTKINDVERSION2": "5"},  # Standard (No BOM)
        {"COMPONENTKIND": "4"},
        # Jumper: ComponentKindVersion3=6, written with or without the zeros before it.
        {"COMPONENTKIND": "0", "COMPONENTKINDVERSION2": "0", "COMPONENTKINDVERSION3": "6"},
        {"COMPONENTKINDVERSION3": "6"},
    ])
    assert [component.kind for component in components] == [0, 1, 5, 4, 6, 6]


def test_decode_components_keeps_the_full_unique_id_path():
    (component,) = decode_components([{"SOURCEUNIQUEID": "\\SHEETSYM\\PARTUID"}])
    assert (component.source_unique_id, component.source_uid_tail) == (
        "\\SHEETSYM\\PARTUID", "PARTUID",
    )


def _comment_record(component: int, text: bytes, wide_index: int | None = None) -> bytes:
    first = bytearray(120)
    struct.pack_into("<H", first, 7, component)
    first[40] = 1  # IsComment
    if wide_index is not None:
        struct.pack_into("<I", first, 115, wide_index)
    second = bytes([len(text)]) + text
    return (
        b"\x05"
        + struct.pack("<I", len(first)) + bytes(first)
        + struct.pack("<I", len(second)) + second
    )


def _wide(index: int, text: str) -> bytes:
    body = (text + "\0").encode("utf-16-le")
    return struct.pack("<II", index, len(body)) + body


def test_decode_comments_prefers_the_wide_text():
    wide = decode_wide_strings(_wide(7, "10µF 6.3V") + _wide(8, "100Ω") + _wide(9, "OTHER"))
    data = (
        _comment_record(0, "10µF 6.3V".encode("utf-8"), 7)  # recent files: UTF-8
        + _comment_record(1, b"100?", 8)                     # older: code page, Ω lost
        + _comment_record(2, b"BAV99", 9)                    # an index naming other text
        + _comment_record(3, b"LM358")                       # no wide string
        + _text6_record(4, "R7")                             # a designator, not a comment
    )
    assert decode_comments(data, wide) == {
        0: "10µF 6.3V", 1: "100Ω", 2: "BAV99", 3: "LM358",
    }


def test_parse_streams_attaches_comments():
    streams = _minimal_streams()
    streams["Components6/Data"] = _stream_text([{"SOURCEDESIGNATOR": "C1"}])
    streams["Texts6/Data"] = _comment_record(0, b"4.7?F", 3) + _text6_record(0, "C1")
    streams["WideStrings6/Data"] = _wide(3, "4.7µF")
    pcb = parse_streams(streams)
    assert (pcb.components[0].designator, pcb.components[0].comment) == ("C1", "4.7µF")


def test_decode_wide_strings_steps_over_a_bodyless_record():
    data = (
        _wide(1, "A")
        + struct.pack("<II", 2, 2)  # declared 2 bytes, but index 3's header follows
        + _wide(3, "C")
    )
    assert decode_wide_strings(data) == {1: "A", 2: "", 3: "C"}


def test_decode_wide_strings_stops_at_a_malformed_record():
    data = _wide(1, "A") + struct.pack("<II", 9, 0xFFFFFF) + b"junk"
    assert decode_wide_strings(data) == {1: "A"}
