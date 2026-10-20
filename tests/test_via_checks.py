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
    decode_vias,
)
from server.parsers.prj_pcb import VariantDefinition, VariantState
from server.services.pcb_index import PcbIndex
from server.services.pcb_rules import rule_values
from server.services.via_checks import (
    DEFAULT_MIN_RING_MIL,
    run_via_check,
)

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
        kind="pad", layer=layer, net=N[net], component=component,
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


def _via_payload(length=321, flags1=0x6C, flags2=0, mode=0, diameters=None, removed=()):
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


def test_decode_short_via_record_is_a_simple_stack():
    (via,), _ = decode_vias(_via_record(_via_payload(length=31, flags1=0x4C)))
    assert via.via.tent_bottom and not via.via.tent_top
    assert via.via.mode == 0 and via.via.layer_diameters is None
    assert via.via.removed_pads == frozenset()
    assert via.via.pad_diameter(3, 20.0) == 20.0


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
    assert result["board"] == {"copper_layers": 6, "thickness_mil": THICKNESS}
    summary = result["summary"]
    assert summary["vias"] == 6 and summary["types"] == 4
    assert (summary["through"], summary["blind"], summary["buried"]) == (4, 1, 1)
    assert summary["microvias"] == 1 and summary["no_net"] == 1
    through = _type(result, 6.0)
    assert through["span"] == "Top Layer -> Bottom Layer" and through["kind"] == "through"
    assert through["count"] == 2 and through["nets"] == 1
    assert through["depth_mil"] == THICKNESS
    assert through["aspect_ratio"] == pytest.approx(10.93, abs=0.01)
    assert through["min_annular_ring_mil"] == 4.0 and through["tented"] == {"both": 2}
    assert _type(result, 12.0)["no_net"] == 1
    micro = _type(result, 4.0)
    assert micro["microvia"] and micro["kind"] == "blind" and micro["drill_pair"] is True
    assert micro["depth_mil"] == 4.0 and micro["aspect_ratio"] == 1.0  # dielectric / hole
    buried = _type(result, 8.0)
    assert buried["span"] == "L3 -> L4" and buried["kind"] == "buried"
    assert buried["depth_mil"] == pytest.approx(32.4)
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
        _via("SIG", 300, 0, layer_from=5, layer_to=32, hole=3.0, diameter=8.0),
    ], drill_pairs=[THROUGH, DrillPair("MID4", "BOTTOM")])
    result = run_via_check(index, checks=["aspect_ratio"])
    warnings = _findings(result, "aspect_ratio", "warning")
    assert {(f["hole_mil"], f["aspect_ratio"]) for f in warnings} == {(6.0, 10.93), (3.0, 1.33)}
    (note,) = _findings(result, "aspect_ratio", "info")
    assert note["hole_mil"] == 8.0 and note["aspect_ratio"] == 8.2
    relaxed = run_via_check(index, checks=["aspect_ratio"], max_aspect_ratio=12,
                            max_microvia_aspect_ratio=1.5)
    assert relaxed["findings"] == []
    assert result["checks_run"] == ["aspect_ratio"]


def test_no_stack_heights_skips_depths():
    result = run_via_check(_index([_via("SIG", 0, 0, hole=6.0)], heights=False))
    assert result["board"]["thickness_mil"] is None
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


def test_via_style_sizes_and_style():
    rule = _rule("RoutingVias", name="Vias", VIASTYLE="Through Hole", MINWIDTH="18mil",
                 WIDTH="20mil", MAXWIDTH="30mil", MINHOLEWIDTH="8mil", HOLEWIDTH="10mil",
                 MAXHOLEWIDTH="14mil")
    index = _index([
        _via("SIG", 0, 0, diameter=14.0, hole=6.0),
        _via("SIG", 100, 0, diameter=20.0, hole=10.0),
        _via("SIG", 200, 0, layer_from=1, layer_to=2, diameter=20.0, hole=10.0),
    ], rules=[rule], drill_pairs=[THROUGH, DrillPair("TOP", "MID1")])
    result = run_via_check(index, checks=["via_style"])
    (size,) = _findings(result, "via_style", "warning")
    assert size["problems"] == ["diameter_below_min", "hole_below_min"]
    assert size["min_diameter_mil"] == 18.0 and size["rule"] == "Vias"
    (style,) = _findings(result, "via_style", "info")
    assert style["span"] == "Top Layer -> L2" and style["via_style"] == "Through Hole"


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
        _via("SIG", 300, 100),
        _via("OTHER", 500, 100),
        _via("SIG", 700, 100),  # beside the pad
        _pad("SIG", 100, 100, name="EP"),
        _pad("SIG", 300, 100, name="2"),
        _pad("SIG", 500, 100, name="3"),
        _pad("SIG", 690, 100, size=10.0, name="4"),  # hole edge 4 mil from the pad edge
    ])
    result = run_via_check(index, checks=["via_in_pad"])
    assert result["summary"]["in_pad"] == 3
    (short,) = _findings(result, "via_in_pad", "warning")
    assert short["pads"] == ["3"] and "another net" in short["issue"]
    notes = _findings(result, "via_in_pad", "info")
    assert sum(note["count"] for note in notes) == 2
    assert {note["designator"] for note in notes} == {"U1"}
    assert any(note.get("footprint_vias") == 1 for note in notes)
    forbidden = run_via_check(
        _index(index.pcb.primitives, rules=[_rule("ViasUnderSMD", name="NoVip",
                                                  ALLOWED="FALSE")]),
        checks=["via_in_pad"],
    )
    rules = {f.get("rule") for f in _findings(forbidden, "via_in_pad", "warning")}
    assert rules == {"NoVip"}


def test_tenting_skips_test_points():
    index = _index([
        _via("SIG", 0, 0, stack=ViaStack(False, False, False, False)),
        _via("SIG", 100, 0, stack=ViaStack(True, False, False, False)),
        _via("SIG", 200, 0, stack=ViaStack(False, False, True, False)),
        _via("SIG", 300, 0),
    ])
    result = run_via_check(index, checks=["tenting"])
    assert {(f["tented"], f["count"]) for f in result["findings"]} == {("none", 1), ("top", 1)}
    entry = result["via_types"][0]
    assert entry["tented"] == {"none": 2, "top_only": 1, "both": 1}
    assert entry["test_points"] == 1
    summary = result["summary"]
    assert (summary["tented"], summary["one_side_tented"], summary["untented"]) == (1, 1, 2)


# ---------- back drilling and stubs ----------

BACK_DRILL = DrillPair("MID2", "BOTTOM", back_drill=True, pair_type=2)


def _back_drill_rule(**props):
    values = {"MAXSTUBLENGTH": "10mil", "HOLEOVERSIZE": "4mil", "USETOPBACKDRILL": "TRUE",
              "USEBOTTOMBACKDRILL": "TRUE"}
    values.update(props)
    return _rule("BackDrilling", scope1="InNet('SIG')", name="BD", **values)


def _signal_via(net="SIG", inner=3):
    """A through via joined to tracks on Top and an inner layer."""
    return [
        _via(net, 0, 0),
        _track(net, 1, -100, 0, 0, 0),
        _track(net, inner, 0, 0, 100, 0),
    ]


def test_back_drill_applied_leaves_no_stub():
    index = _index(_signal_via(), rules=[_back_drill_rule()],
                   drill_pairs=[THROUGH, BACK_DRILL])
    result = run_via_check(index)
    assert result["summary"]["back_drilled"] == 1
    assert result["via_types"][0]["back_drilled"] == 1
    assert not _findings(result, "back_drill")
    (pair,) = result["back_drill"]["pairs"]
    assert pair["from_layer"] == "Bottom" and pair["stops_before"] == "L3"
    assert pair["drilled_layers"] == ["L4", "L5", "Bottom Layer"]
    assert pair["depth_mil"] == pytest.approx(THICKNESS - 17.8)
    assert pair["vias"] == 1 and pair["drill_diameters_mil"] == [16.0]
    (rule,) = result["back_drill"]["rules"]
    assert rule["candidate_vias"] == 1 and rule["back_drilled_vias"] == 1
    assert rule["max_stub_length_mil"] == 10.0


def test_back_drill_blocked_by_a_connection_in_the_drilled_layers():
    index = _index(_signal_via(inner=4), rules=[_back_drill_rule()],
                   drill_pairs=[THROUGH, BACK_DRILL])
    result = run_via_check(index, checks=["back_drill"])
    assert result["summary"]["back_drilled"] == 0
    (stub,) = _findings(result, "back_drill", "warning")
    assert stub["rule"] == "BD" and stub["side"] == "bottom"
    assert stub["connected_layer"] == "L4" and stub["barrel_end"] == "Bottom Layer"
    assert stub["stub_mil"] == pytest.approx(THICKNESS - 49.0)
    assert "inside the range" in stub["issue"]


def test_pour_counts_as_a_connection():
    pour = PolygonPour(0, N["SIG"], 4, None, [(-50, -50), (50, -50), (50, 50), (-50, 50)])
    primitives = [_via("SIG", 0, 0), _track("SIG", 1, -100, 0, 0, 0)]
    index = _index(primitives, rules=[_back_drill_rule()],
                   drill_pairs=[THROUGH, BACK_DRILL], pours=[pour])
    (stub,) = _findings(run_via_check(index, checks=["back_drill"]), "back_drill")
    assert stub["connected_layer"] == "L4"


def test_back_drill_rule_side_switch():
    index = _index(_signal_via(), rules=[_back_drill_rule(USEBOTTOMBACKDRILL="FALSE")],
                   drill_pairs=[THROUGH, BACK_DRILL])
    (stub,) = _findings(run_via_check(index, checks=["back_drill"]), "back_drill")
    assert "does not use bottom back drills" in stub["issue"]


def test_back_drill_rule_without_back_drills_and_the_reverse():
    rule_only = run_via_check(_index(_signal_via(), rules=[_back_drill_rule()]),
                              checks=["back_drill"])
    messages = [f["issue"] for f in _findings(rule_only, "back_drill", "warning")]
    assert any("no back drill" in message for message in messages)
    assert any("the stack has no back drill from the bottom" in message
               for message in messages)
    pairs_only = run_via_check(_index(_signal_via(), drill_pairs=[THROUGH, BACK_DRILL]),
                               checks=["back_drill"])
    (note,) = _findings(pairs_only, "back_drill", "info")
    assert "no enabled Max Via Stub Length" in note["issue"]


def test_diff_pair_stub_without_a_rule():
    pair = DiffPair("DP", "DP_P", "DP_N", None)
    index = _index(_signal_via("DP_P"), pairs=[pair])
    result = run_via_check(index, checks=["back_drill"])
    (stub,) = result["findings"]
    assert stub["severity"] == "info" and stub["pair"] == "DP" and stub["net"] == "DP_P"
    assert stub["stub_mil"] == pytest.approx(THICKNESS - 17.8)
    assert stub["max_stub_mil"] == 10.0
    assert result["back_drill"]["vias_checked_for_stubs"] == 1
    assert run_via_check(index, checks=["back_drill"], max_stub_mil=60)["findings"] == []
    # A via joined on both outer layers has no stub; a plain net is not checked.
    plain = _index(_signal_via("SIG"))
    assert run_via_check(plain, checks=["back_drill"])["findings"] == []
    selected = run_via_check(plain, checks=["back_drill"], nets=["SIG"])
    assert _findings(selected, "back_drill", "info")


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
        _via("SIG", x * 100.0, 0, stack=ViaStack(False, False, False, False),
             hole=6.0 + x * 0.5, diameter=30.0)
        for x in range(6)
    ]
    # Aspect 10.93 and 10.09 warn, 9.37, 8.75 and 8.2 are info; six untented types.
    result = run_via_check(_index(vias), checks=["tenting", "aspect_ratio"], limit=3)
    assert result["total"] == 11 and result["next_offset"] == 3 and result["has_more"]
    assert [f["severity"] for f in result["findings"][:2]] == ["warning", "warning"]
    last = run_via_check(_index(vias), checks=["tenting", "aspect_ratio"], offset=9, limit=3)
    assert not last["has_more"] and "next_offset" not in last


# ---------- tool ----------


@pytest.fixture
def board(monkeypatch, tmp_path: Path):
    primitives = _signal_via() + [
        _via("SIG", 500, 0, diameter=16.0, hole=10.0,
             stack=ViaStack(False, False, False, False)),
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
    assert result["board"]["thickness_mm"] == pytest.approx(THICKNESS * 0.0254, abs=1e-4)
    pair = result["back_drill"]["pairs"][0]
    assert pair["drill_diameters_mm"] == [pytest.approx(16.0 * 0.0254, abs=1e-4)]
    assert result["summary"]["back_drilled"] == 1
    ring = _findings(result, "annular_ring")[0]
    assert ring["annular_ring_mm"] == pytest.approx(3.0 * 0.0254, abs=1e-4)
    loose = json.loads(main_mod.check_vias(min_annular_ring=0.05, units="mm"))
    assert not _findings(loose, "annular_ring")
    mil = json.loads(main_mod.check_vias(min_annular_ring=2, units="mil"))
    assert mil["units"] == "mil" and not _findings(mil, "annular_ring")
    assert json.loads(main_mod.check_vias(units="inch"))["error"] == "invalid_units"
    assert json.loads(main_mod.check_vias(max_stub=-1))["error"] == "invalid_max_stub"
    assert json.loads(main_mod.check_vias(max_aspect_ratio=0))["error"] == \
        "invalid_max_aspect_ratio"
    assert json.loads(main_mod.check_vias(net="NOPE"))["error"] == "net_not_found"
    assert json.loads(main_mod.check_vias(checks=["x"]))["error"] == "invalid_checks"


def test_check_vias_tool_trims_to_the_size_limit(board, monkeypatch):
    for x in range(200):
        board.pcb.primitives.append(_via("SIG2", 1000.0 + x * 50, 0,
                                         stack=ViaStack(False, False, False, False),
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


def test_minipc_back_drill_matches_the_removed_pads():
    """Altium removes the pads on back-drilled layers; the recomputed back drill
    must select exactly those vias."""
    if not MINIPC.is_file():
        pytest.skip("Altium example boards not installed")
    from server.parsers.pcb_doc import parse_pcb_doc

    index = PcbIndex(parse_pcb_doc(str(MINIPC)), None)
    result = run_via_check(index, checks=["back_drill"])
    removed = [
        primitive for primitive in index.pcb.primitives
        if primitive.kind == "via" and primitive.via.removed_pads
    ]
    assert len(removed) == 16 == result["summary"]["back_drilled"]
    (pair,) = result["back_drill"]["pairs"]
    assert pair["stops_before"] == "12_int11_(sign)" and pair["vias"] == 16
    assert {layer for via in removed for layer in via.via.removed_pads} == {7, 10, 15, 32}
    assert not _findings(result, "back_drill", "warning")
