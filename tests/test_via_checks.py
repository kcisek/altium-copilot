"""Via inventory and fab review (check_vias): parser fields, checks and the tool."""

import json
import struct
from pathlib import Path

import pytest

import server.main as main_mod
from server.parsers.pcb_doc import (
    BoardInfo,
    DiffPair,
    DrillPair,
    PcbClass,
    PcbComponent,
    PcbData,
    PcbLayer,
    PcbRule,
    PolygonPour,
    Primitive,
    ViaStack,
    decode_board,
    decode_polygons,
    decode_vias,
)
from server.parsers.prj_pcb import VariantDefinition, VariantState
from server.services.pcb_index import PcbIndex
from server.services.pcb_rules import rule_values
from server.services.via_checks import (
    DEFAULT_MIN_RING_MIL,
    run_via_check,
)
from tests.test_pcb_index import decoded_arc

NETS = ["SIG", "SIG2", "GND", "DP_P", "DP_N", "OTHER"]
N = {name: number for number, name in enumerate(NETS)}

# Six copper layers. Faces (top, bottom) from the top copper's top face, mil:
# Top 0-1.4, L2 5.4-6.6, L3 16.6-17.8, L4 47.8-49.0, L5 59.0-60.2, Bottom 64.2-65.6.
THICKNESS = 65.6


def _layer(v6, name, order, kind="copper", copper=None, dielectric=None):
    return PcbLayer(
        v6_id=v6, name=name, kind=kind, stack_order=order,
        copper_thick_mil=copper, diel_height_mil=dielectric,
    )


def _board(drill_pairs=(), heights=True):
    copper = 1.4 if heights else None
    inner = 1.2 if heights else None
    layers = [
        _layer(1, "Top Layer", 0, copper=copper),
        _layer(None, "D1", 1, kind="dielectric", dielectric=4.0),
        _layer(2, "L2", 2, copper=inner),
        _layer(None, "D2", 3, kind="dielectric", dielectric=10.0),
        _layer(3, "L3", 4, copper=inner),
        _layer(None, "D3", 5, kind="dielectric", dielectric=30.0),
        _layer(4, "L4", 6, copper=inner),
        _layer(None, "D4", 7, kind="dielectric", dielectric=10.0),
        _layer(5, "L5", 8, copper=inner),
        _layer(None, "D5", 9, kind="dielectric", dielectric=4.0),
        _layer(32, "Bottom Layer", 10, copper=copper),
    ]
    return BoardInfo(
        origin_x=0.0, origin_y=0.0, sheet_x=0.0, sheet_y=0.0,
        sheet_width=5000.0, sheet_height=5000.0, display_unit="mil",
        stackup_source="v9", layers=layers, drill_pairs=list(drill_pairs),
    )


THROUGH = DrillPair(low="TOP", high="BOTTOM")
LAYERS = {"top layer": 1, "l2": 2, "l3": 3, "l4": 4, "l5": 5, "bottom layer": 32,
          "top": 1, "bottom": 32}
UNTENTED = ViaStack(False, False, False, False)


def _via(net, x, y, layer_from=1, layer_to=32, diameter=16.0, hole=8.0, stack=None,
         component=None):
    return Primitive(
        kind="via", layer=layer_from, layer_to=layer_to,
        net=N[net] if net else None, component=component,
        x1=x, y1=y, x2=x, y2=y, width=diameter, hole=hole,
        via=stack or ViaStack(True, True, False, False),
    )


def _track(net, layer, x1, y1, x2, y2, width=5.0):
    return Primitive(kind="track", layer=layer, net=N[net], component=None,
                     x1=x1, y1=y1, x2=x2, y2=y2, width=width)


def _pad(net, x, y, size=20.0, layer=1, component=0, name="1"):
    return Primitive(
        kind="pad", layer=layer, net=N[net] if net else None, component=component,
        x1=x, y1=y, x2=x, y2=y, width=size, height=size, shape=2, hole=0.0,
        pad_name=name,
    )


def _rule(kind, priority=1, scope1="All", name=None, **props):
    return PcbRule(
        name=name or kind, kind=kind, kind_id=0, enabled=True, priority=priority,
        scope1=scope1, scope2="All", net_scope="AnyNet", comment="", unique_id=None,
        from_schematic=False, properties=props,
    )


def _index(primitives, rules=(), drill_pairs=(THROUGH,), pours=(), classes=(),
           pairs=(), heights=True):
    components = [
        PcbComponent(index=0, refdes="U1", source_uid_tail=None, channel_path=None,
                     x=0.0, y=0.0, rotation=0.0, side="top", footprint="QFN",
                     designator="U1"),
    ]
    pcb = PcbData(
        board=_board(drill_pairs, heights), nets=list(NETS), components=components,
        primitives=list(primitives), pours=list(pours), layer_name_to_v6=dict(LAYERS),
        rules=list(rules), classes=list(classes), diff_pairs=list(pairs), xsignals=[],
    )
    return PcbIndex(pcb, None)


def _findings(result, check, severity=None):
    return [
        finding for finding in result["findings"]
        if finding["check"] == check and (severity is None or finding["severity"] == severity)
    ]


def _type(result, hole):
    return next(entry for entry in result["via_types"] if entry["hole_mil"] == hole)


# ---------- parser ----------


def _via_record(payload: bytes) -> bytes:
    return bytes([3]) + struct.pack("<I", len(payload)) + payload


def _via_payload(length=321, flags1=0x6C, flags2=0, mode=0, diameters=None, removed=(),
                 mask=None, mask_bottom=None, from_hole=False):
    payload = bytearray(length)
    payload[0] = 74
    payload[1] = flags1
    payload[2] = flags2
    struct.pack_into("<HH", payload, 3, 0, 0xFFFF)
    struct.pack_into("<HHH", payload, 7, 0xFFFF, 0xFFFF, 0xFFFF)
    struct.pack_into("<iiii", payload, 13, 10_000_000, 20_000_000, 200_000, 100_000)
    payload[29], payload[30] = 1, 32
    if length >= 203:
        payload[74] = mode
        struct.pack_into("<32i", payload, 75, *(diameters or [200_000] * 32))
    for layer in removed:
        payload[208 + layer] = 1
    if mask is not None:  # manual solder mask expansion, 1e-4 mil units
        struct.pack_into("<i", payload, 54, mask)
        payload[66] = 0x02
        payload[67] = 0x01 if from_hole else 0x00
        if mask_bottom is None:
            payload[241] = 0x01
        else:
            struct.pack_into("<i", payload, 242, mask_bottom)
    return bytes(payload)


def test_decode_vias_reads_tenting_stack_and_removed_pads():
    sizes = [200_000] + [0] * 30 + [180_000]
    data = _via_record(_via_payload(flags1=0x2C, flags2=0x01, mode=2, diameters=sizes,
                                    removed=(32,)))
    (via,), skipped = decode_vias(data)
    assert skipped == 0 and via.width == 20.0 and via.hole == 10.0
    stack = via.via
    assert stack.tent_top and not stack.tent_bottom
    assert not stack.test_fab_top and stack.test_fab_bottom
    assert stack.mode == 2 and stack.removed_pads == frozenset({32})
    assert stack.layer_diameters[0] == 20.0 and stack.layer_diameters[31] == 18.0
    assert stack.pad_diameter(1, via.width) == 20.0
    assert stack.pad_diameter(5, via.width) is None  # no pad in the full stack
    assert stack.pad_diameter(32, via.width) is None  # removed


def test_decode_short_and_simple_via_records():
    (via,), _ = decode_vias(_via_record(_via_payload(length=31, flags1=0x4C)))
    assert via.via.tent_bottom and not via.via.tent_top
    assert via.via.mode == 0 and via.via.layer_diameters is None
    assert via.via.removed_pads == frozenset()
    assert via.via.pad_diameter(3, 20.0) == 20.0
    # Simple stacks keep no per-layer sizes; unknown modes use the nominal size.
    (simple,), _ = decode_vias(_via_record(_via_payload(mode=0)))
    assert simple.via.layer_diameters is None
    odd = ViaStack(True, True, False, False, mode=7, layer_diameters=(0.0,) * 32)
    assert odd.pad_diameter(5, 20.0) == 20.0


def test_manual_mask_expansion_that_covers_the_ring_counts_as_tented():
    # Pad 20 mil, hole 10 mil, untented flags.
    covered = _via_payload(flags1=0x0C, mask=-60_000)  # 20 - 12 = 8 <= 10 both sides
    (via,), _ = decode_vias(_via_record(covered))
    assert via.via.tent_top and via.via.tent_bottom
    split = _via_payload(flags1=0x0C, mask=-60_000, mask_bottom=20_000)
    (via,), _ = decode_vias(_via_record(split))
    assert via.via.tent_top and not via.via.tent_bottom
    from_hole = _via_payload(flags1=0x0C, mask=-10_000, from_hole=True)  # 10 - 2 = 8
    (via,), _ = decode_vias(_via_record(from_hole))
    assert via.via.tent_top
    open_mask = _via_payload(flags1=0x0C, mask=40_000)
    (via,), _ = decode_vias(_via_record(open_mask))
    assert not via.via.tent_top and not via.via.tent_bottom


def test_pad_diameter_top_middle_bottom():
    sizes = (20.0, 16.0) + (0.0,) * 29 + (18.0,)
    stack = ViaStack(True, True, False, False, mode=1, layer_diameters=sizes)
    assert stack.pad_diameter(1, 20.0) == 20.0
    assert stack.pad_diameter(7, 20.0) == 16.0
    assert stack.pad_diameter(32, 20.0) == 18.0


def test_decode_board_reads_drill_pairs_and_back_drills():
    board = decode_board([{
        "LAYERPAIR0LOW": "TOP", "LAYERPAIR0HIGH": "BOTTOM",
        "LAYERPAIR0DRILLGUIDE": "FALSE",
        "LAYERPAIR1LOW": "MID4", "LAYERPAIR1HIGH": "BOTTOM",
        "LAYERPAIR1BACKDRILL": "TRUE", "LAYERPAIR1DRILLPAIRTYPE": "2",
    }])
    assert board.drill_pairs == [
        DrillPair("TOP", "BOTTOM"),
        DrillPair("MID4", "BOTTOM", back_drill=True, pair_type=2),
    ]


def test_stack_layer_ids_come_from_layerid_not_repeated_names():
    """Boards keep stale LAYERnNAME entries (old plane slots) repeating a
    signal layer's name; the stack's LAYERID decides the v6 id."""
    board = decode_board([{
        "LAYER1NAME": "Top", "LAYER2NAME": "GND", "LAYER32NAME": "Bottom",
        "LAYER39NAME": "GND", "LAYER40NAME": "PWR",
        "V9_STACK_LAYER0_NAME": "Top", "V9_STACK_LAYER0_LAYERID": "16777217",
        "V9_STACK_LAYER0_COPTHICK": "1.4mil",
        "V9_STACK_LAYER1_NAME": "Core", "V9_STACK_LAYER1_DIELHEIGHT": "10mil",
        "V9_STACK_LAYER2_NAME": "GND", "V9_STACK_LAYER2_LAYERID": str(0x01000002),
        "V9_STACK_LAYER2_COPTHICK": "1.2mil",
        "V9_STACK_LAYER3_NAME": "PWR", "V9_STACK_LAYER3_LAYERID": str(0x01010002),
        "V9_STACK_LAYER3_COPTHICK": "1.2mil",
        "V9_STACK_LAYER4_NAME": "Bottom", "V9_STACK_LAYER4_LAYERID": str(0x0100FFFF),
        "V9_STACK_LAYER4_COPTHICK": "1.4mil",
    }])
    copper = {layer.name: layer.v6_id for layer in board.layers if layer.kind == "copper"}
    assert copper == {"Top": 1, "GND": 2, "PWR": 40, "Bottom": 32}
    assert not [layer for layer in board.layers if layer.v6_id == 39]


def test_rule_values_name_back_drilling_and_via_rules():
    values = rule_values(
        _rule("BackDrilling", MAXSTUBLENGTH="11.811mil", HOLEOVERSIZE="5mil",
              HOLETOLPLUS="0mil", HOLETOLMINUS="0mil", USETOPBACKDRILL="TRUE",
              USEBOTTOMBACKDRILL="FALSE"),
        {1: "Top Layer", 32: "Bottom Layer"},
    )
    assert values["max_stub_length_mil"] == 11.811 and values["oversize_mil"] == 5.0
    assert values["use_top_back_drill"] is True
    assert values["use_bottom_back_drill"] is False
    assert rule_values(_rule("ViasUnderSMD", ALLOWED="FALSE"), {})["allowed"] is False
    assert rule_values(_rule("LayerPairs", ENFORCE="TRUE"), {})["enforce"] is True
    stacked = rule_values(_rule("HoleToHoleClearance", GAP="10mil",
                                ALLOWSTACKEDMICROVIAS="TRUE"), {})
    assert stacked == {"clearance_mil": 10.0, "allow_stacked_microvias": True}


# ---------- inventory ----------


def test_inventory_types_spans_depth_and_classes():
    index = _index(
        [
            _via("SIG", 0, 0, hole=6.0, diameter=14.0),
            _via("SIG", 100, 0, hole=6.0, diameter=14.0),
            _via("GND", 200, 0, hole=12.0, diameter=24.0),
            _via(None, 300, 0, hole=12.0, diameter=24.0),
            _via("SIG2", 400, 0, layer_from=1, layer_to=2, hole=4.0, diameter=10.0),
            _via("SIG2", 500, 0, layer_from=4, layer_to=3, hole=8.0, diameter=16.0),
        ],
        drill_pairs=[THROUGH, DrillPair("TOP", "MID1"), DrillPair("MID2", "MID3")],
        classes=[PcbClass("HS", "net", ["SIG", "SIG2"], False, False, None)],
    )
    result = run_via_check(index)
    assert result["board"] == {"copper_layers": 6, "stack_thickness_mil": THICKNESS}
    summary = result["summary"]
    assert summary["vias"] == 6 and summary["types"] == 4
    assert (summary["through"], summary["blind"], summary["buried"]) == (4, 1, 1)
    assert summary["microvias"] == 1 and summary["no_net"] == 1
    assert summary["tented"] == 5  # the buried via has no outer face
    through = _type(result, 6.0)
    assert through["span"] == "Top Layer -> Bottom Layer" and through["kind"] == "through"
    assert through["count"] == 2 and through["nets"] == 1
    assert through["depth_mil"] == THICKNESS
    assert through["aspect_ratio"] == pytest.approx(10.93, abs=0.01)
    assert through["min_annular_ring_mil"] == 4.0 and through["tented"] == {"tented": 2}
    assert _type(result, 12.0)["no_net"] == 1
    micro = _type(result, 4.0)
    assert micro["microvia"] and micro["kind"] == "blind" and micro["drill_pair"] is True
    # Capture foil (1.4) plus dielectric (4.0) over the hole.
    assert micro["depth_mil"] == 5.4 and micro["aspect_ratio"] == 1.35
    buried = _type(result, 8.0)
    assert buried["span"] == "L3 -> L4" and buried["kind"] == "buried"
    assert "microvia" not in buried  # 31.2 mil deep: deeper than 0.25 mm
    assert buried["depth_mil"] == pytest.approx(32.4) and buried["tented"] == {"buried": 1}
    assert buried["drill_pair"] is True
    (hs,) = result["net_classes"]
    assert hs["class"] == "HS" and hs["vias"] == 4
    assert sum(hs["types"].values()) == 4
    pairs = {(entry["from_layer"], entry["to_layer"]): entry["vias"]
             for entry in result["drill_pairs"]}
    assert pairs == {("Top Layer", "Bottom Layer"): 4, ("Top Layer", "L2"): 1,
                     ("L3", "L4"): 1}


def test_full_stack_ring_uses_layers_that_keep_a_pad():
    sizes = (20.0,) + (0.0,) * 30 + (14.0,)
    stack = ViaStack(True, True, False, False, mode=2, layer_diameters=sizes)
    result = run_via_check(_index([_via("SIG", 0, 0, diameter=20.0, hole=8.0, stack=stack)]))
    entry = result["via_types"][0]
    assert entry["min_annular_ring_mil"] == 3.0 and entry["pad_stack"] == {"full stack": 1}


# ---------- checks ----------


def test_aspect_ratio_limits():
    index = _index([
        _via("SIG", 0, 0, hole=6.0, diameter=14.0),      # 10.93:1
        _via("SIG", 100, 0, hole=8.0, diameter=16.0),    # 8.2:1
        _via("SIG", 200, 0, hole=12.0, diameter=24.0),   # 5.47:1
        _via("SIG", 300, 0, hole=6.55, diameter=14.0),   # 10.02:1, judged 10.0:1
        _via("SIG", 400, 0, layer_from=5, layer_to=32, hole=3.0, diameter=8.0),
        _via("SIG", 500, 0, layer_from=1, layer_to=2, hole=6.0, diameter=12.0),
    ], drill_pairs=[THROUGH, DrillPair("MID4", "BOTTOM"), DrillPair("TOP", "MID1")])
    result = run_via_check(index, checks=["aspect_ratio"])
    warnings = _findings(result, "aspect_ratio", "warning")
    # The L5-Bottom microvia: 1.4 foil + 4.0 dielectric over 3.0.
    assert {(f["hole_mil"], f["aspect_ratio"]) for f in warnings} == {(6.0, 10.93), (3.0, 1.8)}
    notes = {(f["hole_mil"], f["aspect_ratio"]) for f in _findings(result, "aspect_ratio", "info")}
    assert notes == {(8.0, 8.2), (6.55, 10.02), (6.0, 0.9)}  # microvia above 0.8:1
    relaxed = run_via_check(index, checks=["aspect_ratio"], max_aspect_ratio=12,
                            max_microvia_aspect_ratio=2)
    assert relaxed["findings"] == []
    assert result["checks_run"] == ["aspect_ratio"]


def test_requested_aspect_limit_is_compared_as_shown():
    # 65.6 / 6.574 = 9.979: shown 9.98, judged 10.0 against the defaults.
    index = _index([_via("SIG", 0, 0, hole=6.574, diameter=14.0)])
    assert run_via_check(index, checks=["aspect_ratio"],
                         max_aspect_ratio=9.98)["findings"] == []
    (warning,) = run_via_check(index, checks=["aspect_ratio"],
                               max_aspect_ratio=9.97)["findings"]
    assert warning["severity"] == "warning" and warning["aspect_ratio"] == 9.98


def test_mechanical_blind_via_above_one_to_one():
    # Top -> L3, 0.3 mm hole: 17.8 mil deep, too deep for a microvia.
    index = _index([_via("SIG", 0, 0, layer_from=1, layer_to=3, hole=12.0, diameter=24.0)],
                   drill_pairs=[THROUGH, DrillPair("TOP", "MID2")])
    (note,) = run_via_check(index, checks=["aspect_ratio"])["findings"]
    assert note["aspect_ratio"] == 1.48 and "sequential lamination" in note["issue"]


def test_stacked_microvias():
    index = _index([
        _via("SIG", 0, 0, layer_from=1, layer_to=2, hole=4.0, diameter=10.0),
        # L2 -> L3 is 11.2 mil deep (foil + 10 mil dielectric): not a microvia.
        _via("SIG", 0, 0, layer_from=2, layer_to=3, hole=8.0, diameter=16.0),
        _via("SIG", 100, 0, layer_from=1, layer_to=2, hole=4.0, diameter=10.0),
    ], drill_pairs=[THROUGH, DrillPair("TOP", "MID1"), DrillPair("MID1", "MID2")])
    result = run_via_check(index, checks=["aspect_ratio"], max_microvia_aspect_ratio=5)
    assert result["summary"]["microvias"] == 2 and result["summary"]["stacked_microvias"] == 1
    (stacked,) = result["findings"]
    assert stacked["severity"] == "info" and stacked["count"] == 1
    forbidden = run_via_check(
        _index(index.pcb.primitives, rules=[_rule("HoleToHoleClearance", GAP="5mil",
                                                  ALLOWSTACKEDMICROVIAS="FALSE")],
               drill_pairs=index.pcb.board.drill_pairs),
        checks=["aspect_ratio"], max_microvia_aspect_ratio=5,
    )
    assert forbidden["findings"][0]["severity"] == "warning"


def test_microvia_on_a_through_via_is_not_stacked():
    index = _index([
        _via("SIG", 0, 0, layer_from=1, layer_to=2, hole=4.0, diameter=10.0),
        _via("SIG", 0, 0),
    ], drill_pairs=[THROUGH, DrillPair("TOP", "MID1")])
    result = run_via_check(index, checks=["aspect_ratio"], max_microvia_aspect_ratio=5)
    assert "stacked_microvias" not in result["summary"]
    assert not [f for f in result["findings"] if "stacked" in f["issue"]]


def test_no_stack_heights_skips_depths():
    result = run_via_check(_index([_via("SIG", 0, 0, hole=6.0)], heights=False))
    assert result["board"]["stack_thickness_mil"] is None
    assert "missing" in result["board"]["stack_heights"]
    assert result["via_types"][0]["aspect_ratio"] is None
    assert not _findings(result, "aspect_ratio")


def test_annular_ring_rule_parameter_and_default():
    vias = [_via("SIG", 0, 0, diameter=16.0, hole=10.0)]  # ring 3 mil
    result = run_via_check(_index(vias), checks=["annular_ring"])
    (note,) = result["findings"]
    assert note["severity"] == "info" and note["annular_ring_mil"] == 3.0
    assert note["min_annular_ring_mil"] == pytest.approx(DEFAULT_MIN_RING_MIL, abs=1e-4)
    assert run_via_check(_index(vias), checks=["annular_ring"],
                         min_annular_ring_mil=2.5)["findings"] == []
    ruled = run_via_check(
        _index(vias, rules=[_rule("MinimumAnnularRing", MINIMUMRING="4mil", name="Ring")]),
        checks=["annular_ring"], min_annular_ring_mil=2.5,
    )
    (warning,) = ruled["findings"]
    assert warning["severity"] == "warning" and warning["rule"] == "Ring"
    assert warning["min_annular_ring_mil"] == 4.0 and warning["nets"] == ["SIG"]
    # A requested minimum stricter than the rule wins.
    stricter = run_via_check(
        _index(vias, rules=[_rule("MinimumAnnularRing", MINIMUMRING="2mil", name="Ring")]),
        checks=["annular_ring"], min_annular_ring_mil=3.5,
    )
    (warning,) = stricter["findings"]
    assert warning["min_annular_ring_mil"] == 3.5 and "rule" not in warning
    assert "requested minimum" in warning["issue"]


def test_via_style_sizes_and_style():
    rule = _rule("RoutingVias", name="Vias", VIASTYLE="Through Hole", MINWIDTH="18mil",
                 WIDTH="20mil", MAXWIDTH="30mil", MINHOLEWIDTH="8mil", HOLEWIDTH="10mil",
                 MAXHOLEWIDTH="14mil")
    index = _index([
        _via("SIG", 0, 0, diameter=14.0, hole=6.0),
        _via("SIG", 100, 0, diameter=20.0, hole=10.0),
        _via("SIG", 200, 0, layer_from=1, layer_to=2, diameter=20.0, hole=10.0),
        _via("GND", 300, 0, diameter=100.0, hole=60.0),  # a mounting hole drawn as a via
    ], rules=[rule], drill_pairs=[THROUGH, DrillPair("TOP", "MID1")])
    result = run_via_check(index, checks=["via_style"])
    (size,) = _findings(result, "via_style", "warning")
    assert size["problems"] == ["diameter_below_min", "hole_below_min"]
    assert size["min_diameter_mil"] == 18.0 and size["rule"] == "Vias"
    styles = _findings(result, "via_style", "info")
    assert {f.get("via_style") for f in styles} == {"Through Hole", None}
    (mounting,) = [f for f in styles if "problems" in f]
    assert "mounting hole" in mounting["issue"]
    assert _type(result, 60.0)["large_hole"] is True


def test_drill_pairs_flag_spans_without_a_pair():
    index = _index([
        _via("SIG", 0, 0, layer_from=3, layer_to=4),
        _via("SIG", 100, 0, layer_from=1, layer_to=2, hole=4.0, diameter=10.0),
    ], drill_pairs=[THROUGH, DrillPair("TOP", "MID1"), DrillPair("MID9", "BOTTOM")])
    result = run_via_check(index, checks=["drill_pairs"])
    (missing,) = _findings(result, "drill_pairs", "warning")
    assert missing["span"] == "L3 -> L4"
    (unknown,) = _findings(result, "drill_pairs", "info")
    assert "MID9" in unknown["issue"]
    assert {"from_layer": "MID9", "to_layer": "BOTTOM", "resolved": False} in \
        result["drill_pairs"]


def test_via_in_pad():
    index = _index([
        _via("SIG", 100, 100, component=0),
        _via("SIG", 300, 100, stack=ViaStack(True, False, False, False)),
        _via("OTHER", 500, 100),
        _via("SIG", 500, 100),  # the same pad holds a via of its own net too
        _via("SIG", 700, 100),  # beside the pad
        _via(None, 900, 100),
        _pad("SIG", 100, 100, name="EP"),
        _pad("SIG", 300, 100, name="2"),
        _pad("SIG", 500, 100, name="3"),
        _pad("SIG", 690, 100, size=10.0, name="4"),  # hole edge 4 mil from the pad edge
        _pad(None, 900, 100, name="5"),  # no-net via in a no-net pad: not a short
    ])
    result = run_via_check(index, checks=["via_in_pad"])
    assert result["summary"]["in_pad"] == 5
    (short,) = _findings(result, "via_in_pad", "warning")
    assert short["pad_names"] == ["3"] and short["count"] == 2
    assert "another net" in short["issue"]
    notes = _findings(result, "via_in_pad", "info")
    assert sum(note["count"] for note in notes) == 3
    assert {note["designator"] for note in notes} == {"U1"}
    (thermal,) = [note for note in notes if note.get("thermal_pad")]
    assert thermal["pad_names"] == ["EP"]  # the footprint's own via
    plain = [note for note in notes if not note.get("thermal_pad")][0]
    assert plain["pad_names"] == ["2", "5"]
    assert plain["tented_on_other_side"] == "1 of 2"  # pad 2's via is open below
    assert "or plugged" not in plain["issue"]
    forbidden = run_via_check(
        _index(index.pcb.primitives, rules=[_rule("ViasUnderSMD", name="NoVip",
                                                  ALLOWED="FALSE")]),
        checks=["via_in_pad"],
    )
    rules = {f.get("rule") for f in _findings(forbidden, "via_in_pad", "warning")}
    assert rules == {"NoVip"}


def test_thermal_pad_via_array_is_one_note():
    vias = [_via("GND", 100 + dx, 100 + dy, hole=10.0, diameter=20.0)
            for dx in (-30, 30) for dy in (-30, 30)]
    pad = Primitive(kind="pad", layer=1, net=N["GND"], component=0, x1=100, y1=100,
                    x2=100, y2=100, width=150.0, height=150.0, shape=2, hole=0.0,
                    pad_name="EP")
    result = run_via_check(_index(vias + [pad]), checks=["via_in_pad"])
    (note,) = result["findings"]
    assert note["thermal_pad"] and note["count"] == 4 and note["pads"] == 1
    assert "thermal pad" in note["issue"]


def test_tenting():
    index = _index([
        _via("SIG", 0, 0, stack=UNTENTED),
        _via("SIG", 100, 0, stack=ViaStack(True, False, False, False)),
        _via("SIG", 200, 0, stack=ViaStack(False, False, True, False)),
        _via("SIG", 300, 0),
        _via("SIG", 400, 0, layer_from=3, layer_to=4, stack=UNTENTED),  # buried
        _via("SIG", 500, 0, layer_from=1, layer_to=2, hole=4.0, diameter=10.0,
             stack=ViaStack(True, False, False, False)),  # blind, tented on its face
        _via("GND", 600, 0, hole=30.0, diameter=50.0),  # tented 0.76 mm hole
    ], drill_pairs=[THROUGH, DrillPair("TOP", "MID1"), DrillPair("MID2", "MID3")])
    result = run_via_check(index, checks=["tenting"])
    found = {(f.get("tented"), f["count"], f.get("hole_mil")) for f in result["findings"]}
    assert found == {("none", 1, None), ("top", 1, None), (None, 1, 30.0)}
    entry = _type(result, 8.0)
    assert entry["tented"] == {"untented": 2, "top_only": 1, "tented": 1}
    assert entry["test_points"] == 1
    summary = result["summary"]
    assert (summary["tented"], summary["one_side_tented"], summary["untented"]) == (3, 1, 2)


def test_a_board_with_no_tented_via_gets_one_note():
    index = _index([_via("SIG", x * 100.0, 0, stack=UNTENTED, hole=6.0 + x)
                    for x in range(4)])
    (note,) = run_via_check(index, checks=["tenting"])["findings"]
    assert note["count"] == 4 and "None of the 4 vias checked" in note["issue"]


# ---------- back drilling and stubs ----------

BACK_DRILL = DrillPair("MID2", "BOTTOM", back_drill=True, pair_type=2)
TOP_BACK_DRILL = DrillPair("TOP", "MID2", back_drill=True, pair_type=2)


def _back_drill_rule(**props):
    values = {"MAXSTUBLENGTH": "35mil", "HOLEOVERSIZE": "4mil", "USETOPBACKDRILL": "TRUE",
              "USEBOTTOMBACKDRILL": "TRUE"}
    values.update(props)
    return _rule("BackDrilling", scope1="InNet('SIG')", name="BD", **values)


def _signal_via(net="SIG", inner=3, outer=1):
    """A through via joined to tracks on an outer and an inner layer."""
    return [
        _via(net, 0, 0),
        _track(net, outer, -100, 0, 0, 0),
        _track(net, inner, 0, 0, 100, 0),
    ]


def test_back_drill_applied():
    index = _index(_signal_via(), rules=[_back_drill_rule()],
                   drill_pairs=[THROUGH, BACK_DRILL])
    result = run_via_check(index)
    assert result["summary"]["back_drilled"] == 1
    assert result["via_types"][0]["back_drilled"] == 1
    assert all(
        stub.get("connected_layer") != "L4" for stub in _findings(result, "back_drill")
    )
    (pair,) = result["back_drill"]["pairs"]
    assert pair["from_layer"] == "Bottom" and pair["stops_before"] == "L3"
    assert pair["drilled_layers"] == 3
    assert pair["first_and_last_drilled"] == ["L4", "Bottom Layer"]
    assert pair["depth_to_stop_layer_mil"] == pytest.approx(THICKNESS - 17.8)
    # Hole 8 mil plus the 4 mil oversize, added once as Altium does.
    assert pair["vias"] == 1 and pair["drill_diameters_mil"] == [12.0]
    (rule,) = result["back_drill"]["rules"]
    assert rule["candidate_vias"] == 1 and rule["back_drilled_vias"] == 1
    assert rule["max_stub_length_mil"] == 35.0 and rule["oversize_mil"] == 4.0


def test_residual_stub_after_back_drilling_is_a_range():
    # The tip may stop anywhere in the 30 mil between L3 and L4.
    index = _index(_signal_via(), rules=[_back_drill_rule(MAXSTUBLENGTH="10mil")],
                   drill_pairs=[THROUGH, BACK_DRILL])
    (stub,) = _findings(run_via_check(index, checks=["back_drill"]), "back_drill")
    assert stub["severity"] == "warning" and stub["stub_mil"] == pytest.approx(30.0)
    assert stub["stub_min_mil"] == 0.0 and "even when back drilled" in stub["issue"]


def test_back_drill_from_both_ends():
    primitives = [
        _via("SIG", 0, 0),
        _track("SIG", 3, -100, 0, 0, 0),
        _track("SIG", 4, 0, 0, 100, 0),
    ]
    index = _index(primitives, rules=[_back_drill_rule(MAXSTUBLENGTH="50mil")],
                   drill_pairs=[THROUGH, TOP_BACK_DRILL, DrillPair(
                       "MID3", "BOTTOM", back_drill=True, pair_type=2)])
    result = run_via_check(index, checks=["back_drill"])
    assert result["summary"]["back_drilled"] == 1
    assert {pair["vias"] for pair in result["back_drill"]["pairs"]} == {1}


def test_back_drill_blocked_by_a_connection_in_the_drilled_layers():
    index = _index(_signal_via(inner=4), rules=[_back_drill_rule(MAXSTUBLENGTH="10mil")],
                   drill_pairs=[THROUGH, BACK_DRILL])
    result = run_via_check(index, checks=["back_drill"])
    assert result["summary"]["back_drilled"] == 0
    (stub,) = _findings(result, "back_drill", "warning")
    assert stub["rule"] == "BD" and stub["side"] == "bottom"
    assert stub["connected_layer"] == "L4" and stub["barrel_end"] == "Bottom Layer"
    assert stub["stub_mil"] == pytest.approx(THICKNESS - 49.0)
    assert "inside the layers" in stub["issue"] and "past L4" in stub["issue"]


def test_pour_counts_as_a_connection():
    pour = PolygonPour(0, N["SIG"], 4, None, [(-50, -50), (50, -50), (50, 50), (-50, 50)])
    primitives = [_via("SIG", 0, 0), _track("SIG", 1, -100, 0, 0, 0)]
    index = _index(primitives, rules=[_back_drill_rule(MAXSTUBLENGTH="10mil")],
                   drill_pairs=[THROUGH, BACK_DRILL], pours=[pour])
    (stub,) = _findings(run_via_check(index, checks=["back_drill"]), "back_drill")
    assert stub["connected_layer"] == "L4"
    # A shelved pour has no copper: the via is back drilled.
    pour.shelved = True
    index = _index(primitives, rules=[_back_drill_rule(MAXSTUBLENGTH="10mil")],
                   drill_pairs=[THROUGH, BACK_DRILL], pours=[pour])
    result = run_via_check(index, checks=["back_drill"])
    assert result["summary"]["back_drilled"] == 1
    assert all(
        stub.get("connected_layer") != "L4" for stub in _findings(result, "back_drill")
    )


def test_pour_arc_edge_counts_for_a_connection():
    # The pour's right edge is a Polygons6 arc bulging out to x = 100, round the
    # via at x = 80 (outside the straight edge at x = 50).
    record = {
        "LAYER": "MID3", "NET": str(N["SIG"]), "HATCHSTYLE": "Solid",
        "VX0": "-50mil", "VY0": "-50mil", "VX1": "50mil", "VY1": "-50mil", "KIND1": "1",
        "CX1": "50mil", "CY1": "0mil", "R1": "50mil", "SA1": "270", "EA1": "90",
        "VX2": "50mil", "VY2": "50mil", "VX3": "-50mil", "VY3": "50mil",
    }
    (pour,), _ = decode_polygons([record], dict(LAYERS))
    primitives = [_via("SIG", 80, 0), _track("SIG", 1, -100, 0, 80, 0)]
    index = _index(primitives, rules=[_back_drill_rule(MAXSTUBLENGTH="10mil")],
                   drill_pairs=[THROUGH, BACK_DRILL], pours=[pour])
    (stub,) = _findings(run_via_check(index, checks=["back_drill"]), "back_drill")
    assert stub["connected_layer"] == "L4"


def test_arc_touching_the_via_pad_connects():
    # The arc's crown touches the 16 mil pad on L4; its chords stop ~0.2 mil short.
    arc = decoded_arc(N["SIG"], 0.0, -40.0, 30.0, 0.0, 180.0, 4.0, layer=4)
    primitives = [_via("SIG", 0, 0), _track("SIG", 1, -100, 0, 0, 0), *arc]
    index = _index(primitives, rules=[_back_drill_rule(MAXSTUBLENGTH="10mil")],
                   drill_pairs=[THROUGH, BACK_DRILL])
    (stub,) = _findings(run_via_check(index, checks=["back_drill"]), "back_drill")
    assert stub["connected_layer"] == "L4"


def test_full_stack_inner_pad_wider_than_the_record_diameter_connects():
    sizes = (16.0, 16.0, 16.0, 30.0) + (16.0,) * 28
    stack = ViaStack(True, True, False, False, mode=2, layer_diameters=sizes)
    primitives = [
        _via("SIG", 0, 0, stack=stack),
        _track("SIG", 1, -100, 0, 0, 0),
        _track("SIG", 3, -100, 0, 0, 0),
        _track("SIG", 4, 14, 0, 100, 0, width=4.0),  # touches only the 30 mil L4 pad
    ]
    index = _index(primitives, rules=[_back_drill_rule()],
                   drill_pairs=[THROUGH, BACK_DRILL])
    assert run_via_check(index)["summary"]["back_drilled"] == 0


def test_candidate_without_a_connection_is_reported():
    index = _index([_via("SIG", 0, 0)], rules=[_back_drill_rule()],
                   drill_pairs=[THROUGH, BACK_DRILL])
    result = run_via_check(index, checks=["back_drill"])
    assert result["back_drill"]["rules"][0]["no_connection_found"] == 1
    assert any("no connection found" in note for note in result["notes"])


def test_back_drill_lists_are_capped():
    rules = [_rule("BackDrilling", priority=n, scope1="InNet('SIG')", name=f"BD{n}",
                   MAXSTUBLENGTH="10mil") for n in range(1, 9)]
    result = run_via_check(_index(_signal_via(), rules=rules), checks=["back_drill"])
    (finding,) = [f for f in _findings(result, "back_drill", "warning")
                  if "usable back drill" in f["issue"]]
    assert finding["rule"] == "BD1, BD2, BD3, BD4, BD5 and 3 more"


def test_back_drill_rule_side_switch():
    index = _index(_signal_via(), rules=[_back_drill_rule(USEBOTTOMBACKDRILL="FALSE")],
                   drill_pairs=[THROUGH, BACK_DRILL])
    (stub,) = _findings(run_via_check(index, checks=["back_drill"]), "back_drill")
    assert "does not use bottom back drills" in stub["issue"]


def test_back_drill_rule_without_back_drills_and_the_reverse():
    rule_only = run_via_check(_index(_signal_via(), rules=[_back_drill_rule()]),
                              checks=["back_drill"])
    messages = [f["issue"] for f in _findings(rule_only, "back_drill", "warning")]
    assert any("layer stack has no usable back drill" in message for message in messages)
    assert any("the stack has no back drill from the bottom" in message
               for message in messages)
    pairs_only = run_via_check(_index(_signal_via(), drill_pairs=[THROUGH, BACK_DRILL]),
                               checks=["back_drill"])
    (note,) = _findings(pairs_only, "back_drill", "info")
    assert "no enabled Max Via Stub Length" in note["issue"]


def test_diff_pair_stub_without_a_rule():
    pair = DiffPair("DP", "DP_P", "DP_N", None)
    index = _index(_signal_via("DP_P") + [
        _via("DP_N", 50, 0),
        _track("DP_N", 1, -100, 50, 50, 0),
        _track("DP_N", 3, 50, 0, 150, 0),
    ], pairs=[pair])
    result = run_via_check(index, checks=["back_drill"])
    (stub,) = result["findings"]  # one finding for the pair
    assert stub["severity"] == "info" and stub["pair"] == "DP"
    assert stub["nets"] == ["DP_N", "DP_P"] and stub["count"] == 2
    assert stub["stub_mil"] == pytest.approx(THICKNESS - 17.8)
    assert stub["max_stub_mil"] == 40.0
    # Quarter-wave resonance with Dk 4: 11.8 / (4 * 0.0478 * 2).
    assert stub["resonance_ghz"] == pytest.approx(30.9, abs=0.1)
    assert result["back_drill"]["vias_checked_for_stubs"] == 2
    assert run_via_check(index, checks=["back_drill"], max_stub_mil=60)["findings"] == []
    # A plain net is checked only when asked for.
    plain = _index(_signal_via("SIG"))
    assert run_via_check(plain, checks=["back_drill"])["findings"] == []
    selected = run_via_check(plain, checks=["back_drill"], nets=["SIG"])
    assert _findings(selected, "back_drill", "info")[0]["nets"] == ["SIG"]


def test_issue_text_uses_the_requested_units():
    vias = [_via("SIG", 0, 0, diameter=16.0, hole=10.0)]
    (note,) = run_via_check(_index(vias), checks=["annular_ring"], units="mm")["findings"]
    assert "0.076 mm" in note["issue"] and " mil" not in note["issue"]


# ---------- selection, errors, paging ----------


def test_net_selection_and_errors():
    pair = DiffPair("DP", "DP_P", "DP_N", None)
    index = _index(
        [_via("SIG", 0, 0), _via("DP_P", 100, 0), _via("DP_N", 200, 0), _via("GND", 300, 0)],
        pairs=[pair],
        classes=[PcbClass("PWR", "net", ["GND"], False, False, None)],
    )
    assert run_via_check(index, nets=["DP"])["summary"]["vias"] == 2
    assert run_via_check(index, nets=["pwr", "sig"])["summary"]["vias"] == 2
    missing = run_via_check(index, nets=["NOPE"])
    assert missing["error"] == "net_not_found" and "NOPE" in missing["message"]
    invalid = run_via_check(index, checks=["bogus"])
    assert invalid["error"] == "invalid_checks" and "tenting" in invalid["valid_checks"]


def test_findings_are_paged_warnings_first():
    vias = [
        _via("SIG", x * 100.0, 0, stack=UNTENTED, hole=6.0 + x * 0.5, diameter=30.0)
        for x in range(6)
    ]
    # 10.93 and 10.09 warn; 9.37, 8.75 and 8.2 are info; one board-wide tenting note.
    result = run_via_check(_index(vias), checks=["tenting", "aspect_ratio"], limit=3)
    assert result["total"] == 6 and result["next_offset"] == 3 and result["has_more"]
    assert [f["severity"] for f in result["findings"][:2]] == ["warning", "warning"]
    last = run_via_check(_index(vias), checks=["tenting", "aspect_ratio"], offset=3, limit=3)
    assert not last["has_more"] and "next_offset" not in last


def test_page_caps_types_per_net_class():
    vias = [_via("SIG", x * 100.0, 0, hole=6.0 + x * 0.1) for x in range(12)]
    index = _index(vias, classes=[PcbClass("HS", "net", ["SIG"], False, False, None)])
    result = run_via_check(index, type_limit=5)
    assert len(result["via_types"]) == 5 and result["via_types_total"] == 12
    (hs,) = result["net_classes"]
    assert len(hs["types"]) == 5 and hs["types_total"] == 12


# ---------- tool ----------


@pytest.fixture
def board(monkeypatch, tmp_path: Path):
    primitives = _signal_via() + [
        _via("SIG", 500, 0, diameter=16.0, hole=10.0, stack=UNTENTED),
    ]
    index = _index(primitives, rules=[_back_drill_rule()], drill_pairs=[THROUGH, BACK_DRILL])
    pcb_file = tmp_path / "Board.PcbDoc"
    pcb_file.write_bytes(b"fake")
    monkeypatch.setattr(main_mod, "_project", {
        "name": "Demo", "root_dir": str(tmp_path), "prj_pcb_path": str(tmp_path / "D.PrjPcb"),
        "sheets": [], "pcb_doc_paths": [str(pcb_file)],
    })
    monkeypatch.setattr(main_mod._altium, "_netlist", {"components": {}, "nets": {}})
    monkeypatch.setattr(main_mod, "_variant_state",
                        VariantState([VariantDefinition(name="Default")]))
    monkeypatch.setattr(main_mod._pcb_session, "_load", lambda path, netlist: index)
    main_mod._pcb_session._index = None
    return index


def test_check_vias_tool_units_and_limits(board):
    result = json.loads(main_mod.check_vias(units="mm"))
    assert result["pcb_doc"] == "Board.PcbDoc" and result["units"] == "mm"
    assert result["board"]["stack_thickness_mm"] == pytest.approx(THICKNESS * 0.0254, abs=1e-4)
    pair = result["back_drill"]["pairs"][0]
    assert pair["drill_diameters_mm"] == [pytest.approx(12.0 * 0.0254, abs=1e-4)]
    assert result["summary"]["back_drilled"] == 1
    ring = _findings(result, "annular_ring")[0]
    assert ring["annular_ring_mm"] == pytest.approx(3.0 * 0.0254, abs=1e-4)
    assert " mm" in ring["issue"]
    loose = json.loads(main_mod.check_vias(min_annular_ring=0.05, units="mm"))
    assert all(f["severity"] == "info" for f in _findings(loose, "annular_ring"))
    strict = json.loads(main_mod.check_vias(min_annular_ring=4, units="mil"))
    assert strict["units"] == "mil"
    assert _findings(strict, "annular_ring", "warning")[0]["min_annular_ring_mil"] == 4.0
    assert json.loads(main_mod.check_vias(units="inch"))["error"] == "invalid_units"
    assert json.loads(main_mod.check_vias(max_stub=-1))["error"] == "invalid_max_stub"
    assert "non-negative" in json.loads(main_mod.check_vias(min_annular_ring=-1))["message"]
    assert json.loads(main_mod.check_vias(max_aspect_ratio=0))["error"] == \
        "invalid_max_aspect_ratio"
    assert json.loads(main_mod.check_vias(net="NOPE"))["error"] == "net_not_found"
    assert json.loads(main_mod.check_vias(checks=["x"]))["error"] == "invalid_checks"


def test_check_vias_tool_trims_to_the_size_limit(board, monkeypatch):
    for x in range(200):
        board.pcb.primitives.append(_via("SIG2", 1000.0 + x * 50, 0, stack=UNTENTED,
                                         hole=4.0 + x * 0.01, diameter=40.0))
    index = PcbIndex(board.pcb, None)
    monkeypatch.setattr(main_mod._pcb_session, "_load", lambda path, netlist: index)
    main_mod._pcb_session._index = None
    result = json.loads(main_mod.check_vias(checks=["tenting"]))
    assert result["total"] == 201 and result["next_offset"] == 100
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 20000)
    trimmed = json.loads(main_mod.check_vias(checks=["tenting"]))
    assert trimmed["findings_limited_to"] in (40, 15, 5)
    assert len(trimmed["via_types"]) <= 20 and trimmed["via_types_total"] == 202
    assert len(json.dumps(trimmed, indent=2)) <= 20000


def test_check_vias_needs_a_project(monkeypatch):
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.check_vias())["error"] == "no_project"
    assert "next_offset" in main_mod.check_vias.__doc__


# ---------- Altium example board (skipped when absent) ----------

MINIPC = Path(
    r"C:\Users\Public\Documents\Altium\AD25\Examples\Mini PC"
    r"\Mini PC - Motherboard\MiniPC.PcbDoc"
)


@pytest.fixture(scope="module")
def minipc():
    if not MINIPC.is_file():
        pytest.skip("Altium example boards not installed")
    from server.parsers.pcb_doc import parse_pcb_doc

    return PcbIndex(parse_pcb_doc(str(MINIPC)), None)


def test_minipc_back_drill_matches_the_removed_pads(minipc):
    """Altium removes the pads on back-drilled layers; the recomputed back drill
    must select exactly those vias, and the removed layers must be the stack's
    layers below the stop layer."""
    index = minipc
    result = run_via_check(index, checks=["back_drill"])
    removed = [
        primitive for primitive in index.pcb.primitives
        if primitive.kind == "via" and primitive.via.removed_pads
    ]
    assert len(removed) == 16 == result["summary"]["back_drilled"]
    (pair,) = result["back_drill"]["pairs"]
    assert pair["stops_before"] == "12_int11_(sign)" and pair["vias"] == 16
    order = index._copper_layers_in_stack_order()
    below_stop = set(order[order.index(5) + 1:])
    assert {layer for via in removed for layer in via.via.removed_pads} == below_stop
    assert not _findings(result, "back_drill", "warning")
    assert len(order) == 16 and all(1 <= layer <= 32 for layer in order)


def test_minipc_back_drill_size_matches_altiums_drill_table(minipc):
    """The drill table Altium cached in MiniPC_Drill.PCBDwf lists the 16 back drills
    at 0.5 mm: the 0.2 mm hole plus the 0.3 mm oversize once, not twice as the radial
    wording in Altium's help suggests."""
    drawing = MINIPC.with_name("MiniPC_Drill.PCBDwf")
    if not drawing.is_file():
        pytest.skip("Altium example boards not installed")
    from server.parsers.pcb_dwf import parse_pcb_dwf

    (pair,) = run_via_check(minipc, checks=["back_drill"])["back_drill"]["pairs"]
    (row,) = [row for row in parse_pcb_dwf(str(drawing)).drill_table if row.get("backdrill")]
    assert row["count"] == pair["vias"] == 16
    assert pair["drill_diameters_mil"] == [pytest.approx(row["size_mm"] / 0.0254, abs=0.01)]
