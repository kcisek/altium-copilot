"""Per-object rule compliance checks (services/pcb_compliance)."""

import asyncio
import re
from copy import deepcopy
from dataclasses import replace

import pytest

import server.main as main_mod
from server.parsers.pcb_doc import (
    PAD_INNER_LAYERS,
    PAD_SHAPE_ROUND,
    DiffPair,
    PadCopper,
    PcbClass,
    PcbRule,
    Primitive,
)
from server.services.pcb_compliance import (
    CLEARANCE_MAX_NETS,
    check_compliance,
    resolve_clearance_rule,
)
from server.services.pcb_index import PcbIndex
from server.services.pcb_rules import MULTI_LAYER, ObjectFacts
from tests.test_pcb_index import NETLIST, pcb_data  # noqa: F401


def _rule(kind, priority, scope1="All", scope2="All", name=None, enabled=True,
          net_scope=None, kind_id=0, **props):
    return PcbRule(
        name=name or kind,
        kind=kind,
        kind_id=kind_id,
        enabled=enabled,
        priority=priority,
        scope1=scope1,
        scope2=scope2,
        net_scope=net_scope or ("DifferentNets" if kind == "Clearance" else "AnyNet"),
        comment="",
        unique_id=None,
        from_schematic=False,
        properties=props,
    )


def _track(net, layer, x1, y1, x2, y2, width):
    return Primitive(kind="track", layer=layer, net=net, component=None,
                     x1=x1, y1=y1, x2=x2, y2=y2, width=width)


def _via(net, x, y, diameter=20.0, hole=8.0, component=None):
    return Primitive(kind="via", layer=1, layer_to=32, net=net, component=component,
                     x1=x, y1=y, x2=x, y2=y, width=diameter, hole=hole)


def _index(pcb_data, rules, primitives=None, pairs=(), classes=(), drc=None):
    data = deepcopy(pcb_data)
    data.rules = list(rules)
    data.diff_pairs = list(pairs)
    data.classes = list(classes)
    data.drc_rule_kinds = drc
    if primitives is not None:
        data.primitives = list(primitives)
    return PcbIndex(data, NETLIST)


def _kind(result, kind):
    return next(entry for entry in result["kinds"] if entry["kind"] == kind)


CLK_PAIR = DiffPair("CLK", "CLK_A", "CLK_B", None)
WIDTH_ALL = _rule("Width", 9, name="Width_All", kind_id=2, MINLIMIT="8mil",
                  PREFEREDWIDTH="10mil", MAXLIMIT="20mil")
# Fixture tracks: CLK_A 6 mil on Top and on L3-SIG (v6 3 = MIDLAYER2), CLK_B
# 6 mil on Top; one CLK_A via (20 / 8 mil).


def test_pair_nets_are_checked_by_diff_pairs_routing_not_width(pcb_data):
    rules = [
        WIDTH_ALL,
        _rule("DiffPairsRouting", 1, "InDifferentialPair('CLK')", name="DP_CLK",
              MINLIMIT="5mil", MOSTFREQGAP="6mil", MAXLIMIT="8mil",
              TOPLAYER_MINWIDTH="5mil", TOPLAYER_MAXWIDTH="7mil",
              MIDLAYER2_MINWIDTH="5mil", MIDLAYER2_MAXWIDTH="7mil"),
    ]
    index = _index(pcb_data, rules, pairs=[CLK_PAIR])
    result = check_compliance(index, kinds=["Width", "DiffPairsRouting"])
    width = _kind(result, "Width")
    pairs = _kind(result, "DiffPairsRouting")
    assert width["objects_checked"] == 0
    assert width["violation_count"] == 0
    assert pairs["objects_checked"] == 3
    assert pairs["by_rule"] == [
        {"rule": "DP_CLK", "priority": 1, "objects": 3, "violations": 0}
    ]
    assert result["violation_count"] == 0

    # Without the pair the same 6 mil tracks fail the Width rule: the pair
    # routing is what keeps them out of Width.
    plain = check_compliance(_index(pcb_data, rules), kinds=["Width"])
    assert _kind(plain, "Width")["violation_count"] == 3


def test_pair_width_violation_names_the_pair_and_layer(pcb_data):
    rules = [
        _rule("DiffPairsRouting", 1, name="DP", TOPLAYER_MINWIDTH="7mil",
              TOPLAYER_MAXWIDTH="9mil", MIDLAYER2_MINWIDTH="5mil",
              MIDLAYER2_MAXWIDTH="7mil"),
    ]
    index = _index(pcb_data, rules, pairs=[CLK_PAIR])
    pairs = _kind(check_compliance(index, kinds=["DiffPairsRouting"]), "DiffPairsRouting")
    assert pairs["violation_count"] == 2
    assert {(v["net"], v["layer"]) for v in pairs["violations"]} == {
        ("CLK_A", "Top Layer"), ("CLK_B", "Top Layer"),
    }
    violation = pairs["violations"][0]
    assert violation["diff_pair"] == "CLK"
    assert violation["min_mil"] == 7.0 and violation["actual_mil"] == 6.0
    assert violation["side"] == "below_min" and violation["margin_mil"] == 1.0


def test_unverified_fallback_to_width_when_no_pair_rule_applies(pcb_data):
    rules = [
        WIDTH_ALL,
        _rule("DiffPairsRouting", 1, "InDifferentialPairClass('Other')", name="DP_Other",
              TOPLAYER_MINWIDTH="5mil", TOPLAYER_MAXWIDTH="7mil"),
    ]
    classes = [PcbClass("Other", "diff_pair", ["NOPE"], False, False, None)]
    index = _index(pcb_data, rules, pairs=[CLK_PAIR], classes=classes)
    result = check_compliance(index, kinds=["Width", "DiffPairsRouting"])
    width = _kind(result, "Width")
    assert width["unverified_fallback_objects"] == 3
    assert width["violation_count"] == 3
    assert all(v["unverified_fallback"] for v in width["violations"])
    assert all(v["diff_pair"] == "CLK" for v in width["violations"])
    # DiffPairsRouting counts the pair tracks no pair rule applies to.
    pairs = _kind(result, "DiffPairsRouting")
    assert (pairs["objects_checked"], pairs["no_rule_objects"]) == (3, 3)
    assert any("unverified_fallback" in note for note in result["approximations"])
    # Asked alone, DiffPairsRouting still shows them instead of dropping them.
    alone = _kind(check_compliance(index, kinds=["DiffPairsRouting"]), "DiffPairsRouting")
    assert (alone["objects_checked"], alone["no_rule_objects"]) == (3, 3)


def test_room_scoped_rule_is_resolved_per_object(pcb_data):
    rules = [
        _rule("RoomDefinition", 1, name="RF", LAYER="TOP", VX0="0mil", VY0="400mil",
              VX1="300mil", VY1="400mil", VX2="300mil", VY2="600mil",
              VX3="0mil", VY3="600mil"),
        _rule("Width", 1, "WithinRoom('RF')", name="Width_RF", MINLIMIT="4mil",
              MAXLIMIT="6mil"),
        _rule("Width", 2, name="Width_All", MINLIMIT="8mil", MAXLIMIT="20mil"),
    ]
    primitives = [
        _track(0, 1, 100, 500, 200, 500, 5.0),  # inside the room
        _track(0, 32, 100, 520, 200, 520, 5.0),  # inside, other side: rooms span all layers
        _track(0, 1, 600, 500, 700, 500, 5.0),  # outside
    ]
    index = _index(pcb_data, rules, primitives=primitives)
    width = _kind(check_compliance(index, kinds=["Width"]), "Width")
    assert {(e["rule"], e["objects"], e["violations"]) for e in width["by_rule"]} == {
        ("Width_RF", 2, 0), ("Width_All", 1, 1),
    }
    (violation,) = width["violations"]
    assert violation["rule"] == "Width_All"
    assert (violation["x_mil"], violation["y_mil"]) == (650.0, 500.0)


def test_per_layer_width_values(pcb_data):
    rules = [
        _rule("Width", 1, name="Width_All", MINLIMIT="5mil", MAXLIMIT="20mil",
              MIDLAYER2_MINWIDTH="7mil"),
    ]
    width = _kind(check_compliance(_index(pcb_data, rules), kinds=["Width"]), "Width")
    assert width["objects_checked"] == 3
    (violation,) = width["violations"]
    assert violation["layer"] == "L3-SIG"
    assert (violation["min_mil"], violation["max_mil"]) == (7.0, 20.0)
    assert violation["object"] == "track"


def test_via_hole_percent_mode_routing_vias_and_annular_ring(pcb_data):
    rules = [
        _rule("HoleSize", 1, name="Holes", ABSOLUTEVALUES="FALSE", MINPERCENT="50",
              MAXPERCENT="80", MINLIMIT="1mil", MAXLIMIT="100mil"),
        _rule("RoutingVias", 1, name="Vias", MINWIDTH="22mil", WIDTH="24mil",
              MAXWIDTH="30mil", MINHOLEWIDTH="6mil", HOLEWIDTH="8mil",
              MAXHOLEWIDTH="7mil"),
        _rule("MinimumAnnularRing", 1, name="Ring", MINIMUMRING="7mil"),
    ]
    result = check_compliance(
        _index(pcb_data, rules),
        kinds=["HoleSize", "RoutingVias", "MinimumAnnularRing"],
    )
    (hole,) = _kind(result, "HoleSize")["violations"]
    assert hole["mode"] == "percent_of_diameter"
    assert (hole["actual_mil"], hole["min_mil"], hole["max_mil"]) == (8.0, 10.0, 16.0)
    assert hole["span"] == "Top Layer -> Bottom Layer"
    (via,) = _kind(result, "RoutingVias")["violations"]
    assert via["problems"] == ["diameter_below_min", "hole_above_max"]
    assert via["margin_mil"] == 2.0
    (ring,) = _kind(result, "MinimumAnnularRing")["violations"]
    assert (ring["actual_mil"], ring["min_mil"]) == (6.0, 7.0)

    absolute = [replace(rules[0], properties={"ABSOLUTEVALUES": "TRUE",
                                              "MINLIMIT": "1mil", "MAXLIMIT": "100mil"})]
    passing = check_compliance(_index(pcb_data, absolute), kinds=["HoleSize"])
    assert _kind(passing, "HoleSize")["violation_count"] == 0
    assert _kind(passing, "HoleSize")["objects_checked"] == 1


def test_routing_layer_violation(pcb_data):
    rules = [
        _rule("RoutingLayers", 1, name="Layers", **{
            "TOP LAYER_V5": "TRUE", "MID LAYER 1_V5": "TRUE",
            "MID LAYER 2_V5": "FALSE", "BOTTOM LAYER_V5": "TRUE",
        }),
    ]
    layers = _kind(check_compliance(_index(pcb_data, rules), kinds=["RoutingLayers"]),
                   "RoutingLayers")
    assert layers["objects_checked"] == 3
    (violation,) = layers["violations"]
    assert (violation["net"], violation["layer"]) == ("CLK_A", "L3-SIG")
    assert violation["allowed_layers"] == ["Top Layer", "L2-GND", "Bottom Layer"]


def test_violations_are_grouped_and_limited(pcb_data):
    arc_chords = [
        Primitive(kind="arc", layer=1, net=0, component=None, x1=x1, y1=0, x2=x2,
                  y2=0, width=3.0, source_id=7, length=10.0)
        for x1, x2 in ((0, 10), (10, 20), (20, 30))
    ]
    primitives = (
        [_track(0, 1, 0, y, 50, y, 3.0) for y in (100, 200, 300)]
        + [_track(0, 1, 0, 400, 50, 400, 2.0)]
        + [_track(1, 1, 0, 500, 50, 500, 3.0)]
        + arc_chords
    )
    index = _index(pcb_data, [WIDTH_ALL], primitives=primitives)
    width = _kind(check_compliance(index, kinds=["Width"], limit=2), "Width")
    # An arc is one object however many chords it is drawn with.
    assert width["objects_checked"] == 6
    assert width["violation_count"] == 6
    assert width["violation_groups"] == 3
    assert width["has_more"] is True
    worst, second = width["violations"]
    assert (worst["net"], worst["actual_mil"], worst["count"]) == ("GND", 2.0, 1)
    assert (second["net"], second["actual_mil"], second["count"]) == ("GND", 3.0, 4)


def test_undecided_objects_are_counted_not_checked(pcb_data):
    rules = [
        _rule("Width", 1, "InFromTo('A')", name="Width_FromTo", MINLIMIT="1mil"),
        WIDTH_ALL,
    ]
    width = _kind(check_compliance(_index(pcb_data, rules), nets=["CLK_A"],
                                   kinds=["Width"]), "Width")
    assert width["undecided"]["count"] == 2
    assert width["violation_count"] == 0
    assert width["undecided"]["examples"] == [
        {"net": "CLK_A", "rules": [{"rule": "Width_FromTo",
                                    "conditions": ["InFromTo('A')"]}]}
    ]


def test_batch_drc_flag(pcb_data):
    rules = [WIDTH_ALL, _rule("RoutingVias", 1, name="Vias", kind_id=11)]
    flagged = check_compliance(_index(pcb_data, rules, drc={2, 42}),
                               kinds=["Width", "RoutingVias", "HoleSize"])
    assert _kind(flagged, "Width")["checked_by_batch_drc"] is True
    assert _kind(flagged, "RoutingVias")["checked_by_batch_drc"] is False
    assert _kind(flagged, "HoleSize")["checked_by_batch_drc"] is True
    unknown = check_compliance(_index(pcb_data, rules), kinds=["Width"])
    assert _kind(unknown, "Width")["checked_by_batch_drc"] is None


def test_length_rule_uses_the_measure(pcb_data):
    rules = [
        _rule("Length", 1, "InNet('CLK_A') or InNet('CLK_B')", name="Len_CLK",
              MINLIMIT="500mil", MAXLIMIT="600mil"),
    ]
    lengths = {
        "CLK_A": {"length_mil": 450.0, "status": "ok", "measure": "signal_path"},
        "CLK_B": {"length_mil": None, "status": "no_path", "measure": "signal_path"},
    }
    result = check_compliance(_index(pcb_data, rules), kinds=["Length"],
                              length_of=lambda net: lengths.get(net))
    length = _kind(result, "Length")
    assert length["approximate"] is True
    assert length["objects_checked"] == 2
    assert length["unmeasured"] == 1
    (violation,) = length["violations"]
    assert violation == {
        "net": "CLK_A", "rule": "Len_CLK", "actual_mil": 450.0, "min_mil": 500.0,
        "max_mil": 600.0, "side": "below_min", "measure": "signal_path",
        "margin_mil": 50.0, "count": 1,
    }
    # Default measure: routed track length (CLK_A 200 mil, CLK_B 100 mil).
    default = _kind(check_compliance(_index(pcb_data, rules), kinds=["Length"]), "Length")
    assert sorted(v["actual_mil"] for v in default["violations"]) == [100.0, 200.0]


def test_clearance_resolution_tries_both_orientations(pcb_data):
    index = _index(pcb_data, [])
    ctx = index.rule_context
    gnd, clk = index.rule_target("GND"), index.rule_target("CLK_A")
    rules = [
        _rule("Clearance", 1, "InNet('GND')", "InNet('CLK_A')", name="Gnd_Clk",
              GENERICCLEARANCE="12mil"),
        _rule("Clearance", 2, name="Default", GENERICCLEARANCE="6mil"),
    ]
    assert resolve_clearance_rule(rules, gnd, clk, ctx)[0].name == "Gnd_Clk"
    assert resolve_clearance_rule(rules, clk, gnd, ctx)[0].name == "Gnd_Clk"
    assert resolve_clearance_rule(rules, clk, index.rule_target("CLK_B"), ctx)[0].name == (
        "Default"
    )
    same_net_only = [replace(rules[0], net_scope="SameNet"), rules[1]]
    assert resolve_clearance_rule(same_net_only, gnd, clk, ctx)[0].name == "Default"
    # A net-level conditional (vias only) is undecided; with the object it is not.
    via_rules = [_rule("Clearance", 1, "IsVia", "All", name="Via_Clr"), rules[1]]
    rule, undecided = resolve_clearance_rule(via_rules, clk, gnd, ctx)
    assert rule.name == "Default"
    assert undecided == [{"rule": "Via_Clr", "conditions": ["IsVia"]}]
    via = ObjectFacts(kind="via", layer=MULTI_LAYER, span=frozenset({1, 32}),
                      points=((0, 0),), hole=8.0)
    rule, undecided = resolve_clearance_rule(
        via_rules, replace(gnd, obj=via), clk, ctx
    )
    assert rule.name == "Via_Clr" and undecided == []


def test_clearance_spot_check_applies_rule_and_object_matrix(pcb_data):
    rules = [
        _rule("Clearance", 1, "InNet('CLK_B')", "InNet('CLK_A')", name="Clk_Pair",
              GENERICCLEARANCE="2mil",
              OBJECTCLEARANCES="ClearanceObj_Track-ClearanceObj_Via:300000"),
        _rule("Clearance", 2, name="Default", GENERICCLEARANCE="10mil"),
    ]
    primitives = [
        _track(1, 1, 0, 0, 100, 0, 6.0),
        _track(2, 1, 0, 10, 100, 10, 6.0),  # 4 mil from CLK_A: meets Clk_Pair (2)
        _via(2, 130, 0, diameter=10.0, hole=4.0),  # 22 mil from the CLK_A track end
        _track(0, 1, 0, -12, 100, -12, 6.0),  # 6 mil from CLK_A: Default (10)
    ]
    index = _index(pcb_data, rules, primitives=primitives)
    result = check_compliance(index, nets=["CLK_A"], kinds=["Clearance"])
    clearance = _kind(result, "Clearance")
    assert clearance["approximate"] is True
    groups = {(v["other_net"], v["rule"], v["objects"]): v for v in clearance["violations"]}
    assert set(groups) == {("GND", "Default", "Track-Track"), ("CLK_B", "Clk_Pair", "Track-Via")}
    assert groups[("GND", "Default", "Track-Track")]["actual_mil"] == 6.0
    # Track-Via matrix cell (30 mil) of the rule matched in the reverse
    # orientation (scope1 = CLK_B), not its generic 2 mil.
    via_hit = groups[("CLK_B", "Clk_Pair", "Track-Via")]
    assert (via_hit["actual_mil"], via_hit["required_mil"]) == (22.0, 30.0)
    assert via_hit["margin_mil"] == 8.0 and via_hit["layer"] == "Top Layer"
    assert clearance["objects_checked"] == 3
    assert {(e["rule"], e["objects"]) for e in clearance["by_rule"]} == {
        ("Clk_Pair", 2), ("Default", 1),
    }


def _through_pad(net, x):
    """A Multi-Layer pad, 60 mil round on top, 40 mil round on the inner
    layers and the bottom (a top-middle-bottom stack)."""
    inner = PadCopper(40.0, 40.0, PAD_SHAPE_ROUND)
    return Primitive(kind="pad", layer=MULTI_LAYER, net=net, component=None,
                     x1=x, y1=0.0, x2=x, y2=0.0, width=60.0, height=60.0,
                     shape=PAD_SHAPE_ROUND, hole=30.0, pad_name="1",
                     layer_copper={PAD_INNER_LAYERS: inner, 32: inner})


def test_clearance_measures_through_pads_on_the_shared_layer(pcb_data):
    rules = [_rule("Clearance", 1, name="Default", GENERICCLEARANCE="12mil")]
    primitives = [
        _through_pad(1, 0.0),
        _through_pad(2, 70.0),  # 10 mil apart on top, 30 mil on the others
        # 13 mil from the inner copper on L3-SIG (3 mil from the top size).
        _track(2, 3, -36.0, -50.0, -36.0, 50.0, 6.0),
    ]
    index = _index(pcb_data, rules, primitives=primitives)
    clearance = _kind(check_compliance(index, nets=["CLK_A"], kinds=["Clearance"]),
                      "Clearance")
    (violation,) = clearance["violations"]
    assert (violation["objects"], violation["actual_mil"]) == ("THPad-THPad", 10.0)
    # Only the top layer is that close.
    assert violation["layer"] == "Top Layer"
    # The track is beyond the 12 mil search distance on its layer.
    assert clearance["objects_checked"] == 1


def test_clearance_needs_a_small_net_set(pcb_data):
    rules = [_rule("Clearance", 1, name="Default", GENERICCLEARANCE="10mil")]
    index = _index(pcb_data, rules)
    result = check_compliance(index, kinds=["Clearance"])
    assert "skipped" in _kind(result, "Clearance")
    many = check_compliance(index, nets=["GND"] * (CLEARANCE_MAX_NETS + 1),
                            kinds=["Clearance"])
    assert "skipped" not in _kind(many, "Clearance")  # duplicates collapse
    default = check_compliance(index)
    assert all(entry["kind"] != "Clearance" for entry in default["kinds"])
    assert any(entry["kind"] == "Clearance" for entry in default["not_checked"])


def test_clearance_ignores_pour_outlines(pcb_data):
    # A GND pour's zero-width outline edge crosses the CLK_A track: the outline
    # is not copper, so neither net's check reports it.
    rules = [_rule("Clearance", 1, name="Default", GENERICCLEARANCE="10mil")]
    outline = replace(_track(0, 1, 50, -50, 50, 50, 0.0), pour_id=0)
    index = _index(pcb_data, rules, primitives=[_track(1, 1, 0, 0, 100, 0, 6.0), outline])
    for net in ("CLK_A", "GND"):
        result = check_compliance(index, nets=[net], kinds=["Clearance"])
        assert _kind(result, "Clearance")["violations"] == []


def _hatch(net, layer, x1, y1, x2, y2, width, pour=0):
    """A hatched pour's track: Tracks6 copper owned by pour `pour` (pcb_data's
    GND pour is index 0)."""
    return replace(_track(net, layer, x1, y1, x2, y2, width), poured_by=pour)


def test_width_tests_pour_copper_and_not_in_polygon_skips_it(pcb_data):
    # Altium's Width rule tests polygon primitives; rules leave them out with
    # Not InPolygon (as on the SL1 example board). One net, two objects: the
    # scope is decided per object, not once for the net.
    rules = [
        _rule("Width", 1, "InNet('GND') and not InPolygon", name="Width_GND",
              MINLIMIT="10mil", MAXLIMIT="20mil"),
        _rule("Width", 9, name="Width_All", MINLIMIT="5mil", MAXLIMIT="20mil"),
    ]
    primitives = [
        _track(0, 1, 0, 0, 100, 0, 6.0),  # routing: Width_GND, too thin
        _hatch(0, 1, 0, 50, 100, 50, 4.0),  # pour copper: Width_All, too thin
    ]
    result = check_compliance(_index(pcb_data, rules, primitives), kinds=["Width"])
    width = _kind(result, "Width")
    assert {(e["rule"], e["objects"], e["violations"]) for e in width["by_rule"]} == {
        ("Width_GND", 1, 1), ("Width_All", 1, 1),
    }
    assert width["pour_copper_objects"] == 1
    hatch = next(v for v in width["violations"] if v["rule"] == "Width_All")
    assert hatch["pour_copper"] is True and hatch["actual_mil"] == 4.0
    routed = next(v for v in width["violations"] if v["rule"] == "Width_GND")
    assert "pour_copper" not in routed
    assert any("pour_copper" in note for note in result["approximations"])
    # Pour copper is not a pair's routing, nor checked against RoutingLayers.
    layers = _rule("RoutingLayers", 1, name="Layers", **{"TOP LAYER_V5": "FALSE"})
    routing = _kind(
        check_compliance(_index(pcb_data, [layers], primitives), kinds=["RoutingLayers"]),
        "RoutingLayers",
    )
    assert (routing["objects_checked"], routing["violation_count"]) == (1, 1)


def test_clearance_treats_pour_copper_as_poly(pcb_data):
    # Object matrix: pour copper is ClearanceObj_Poly, and an InPolygon scope
    # selects it (not the net's routing).
    rules = [
        _rule("Clearance", 1, "InPolygon", name="Poly_Clr", GENERICCLEARANCE="2mil"),
        _rule("Clearance", 2, name="Default", GENERICCLEARANCE="10mil",
              OBJECTCLEARANCES="ClearanceObj_Track-ClearanceObj_Poly:40000"),
    ]
    primitives = [
        _track(1, 1, 0, 0, 100, 0, 6.0),
        _hatch(0, 1, 0, 8, 100, 8, 4.0),  # 3 mil from CLK_A: Poly_Clr (2), passes
        _track(0, 1, 0, -8, 100, -8, 4.0),  # 3 mil, GND routing: Default (10)
    ]
    index = _index(pcb_data, rules, primitives=primitives)
    clearance = _kind(check_compliance(index, nets=["CLK_A"], kinds=["Clearance"]),
                      "Clearance")
    by_object = {(v["objects"], v["rule"]): v for v in clearance["violations"]}
    assert set(by_object) == {("Track-Track", "Default")}
    assert {(e["rule"], e["objects"]) for e in clearance["by_rule"]} == {
        ("Poly_Clr", 1), ("Default", 1),
    }
    # Without the InPolygon rule the matrix's Track-Poly cell (4 mil) applies.
    index = _index(pcb_data, rules[1:], primitives=primitives)
    clearance = _kind(check_compliance(index, nets=["CLK_A"], kinds=["Clearance"]),
                      "Clearance")
    poly = next(v for v in clearance["violations"] if v["objects"] == "Track-Poly")
    assert (poly["actual_mil"], poly["required_mil"]) == (3.0, 4.0)


def test_no_net_count_leaves_out_netless_pour_copper(pcb_data):
    primitives = [
        _track(None, 1, 0, 0, 100, 0, 6.0),
        _hatch(None, 1, 0, 50, 100, 50, 4.0),  # a pour without a net
    ]
    result = check_compliance(_index(pcb_data, [WIDTH_ALL], primitives), kinds=["Width"])
    (no_net,) = [entry for entry in result["not_checked"] if "no-net" in entry["kind"]]
    assert no_net["objects"] == 1


def test_selection_errors_and_pair_expansion(pcb_data):
    index = _index(pcb_data, [WIDTH_ALL], pairs=[CLK_PAIR])
    assert check_compliance(index, kinds=["Bogus"])["error"] == "invalid_kind"
    missing = check_compliance(index, nets=["NOPE"])
    assert missing["error"] == "net_not_found"
    result = check_compliance(index, nets=["CLK", "NOPE"], kinds=["Width"])
    assert result["nets"] == ["CLK_A", "CLK_B"]
    assert result["expanded_pairs"] == ["CLK"]
    assert result["unknown_nets"] == ["NOPE"]
    names = [entry["kind"] for entry in check_compliance(index)["not_checked"]]
    assert {"RoutingCorners", "NetAntennae", "MatchedLengths"} <= set(names)


def test_default_kinds_follow_the_board_rules(pcb_data):
    result = check_compliance(_index(pcb_data, [WIDTH_ALL]))
    assert [entry["kind"] for entry in result["kinds"]] == ["Width"]
    assert "RoutingVias" in result["kinds_without_rules"]


def test_output_converts_to_millimetres(pcb_data):
    rules = [_rule("Width", 1, name="Width_All", MINLIMIT="8mil", MAXLIMIT="20mil")]
    result = check_compliance(_index(pcb_data, rules), kinds=["Width"])
    converted = main_mod._convert_units(result, True)
    width = _kind(converted, "Width")
    violation = width["violations"][0]
    assert violation["actual_mm"] == round(6.0 * 0.0254, 4)
    assert violation["min_mm"] == round(8.0 * 0.0254, 4)
    assert "x_mm" in violation and "actual_mil" not in violation
    # Counts are never under *_mil keys, so they stay integers.
    assert (violation["count"], width["objects_checked"], width["violation_count"]) == (
        1, 3, 3,
    )


@pytest.mark.parametrize("kinds", [["width"], ["WIDTH", "Width"]])
def test_kind_names_are_case_insensitive(pcb_data, kinds):
    result = check_compliance(_index(pcb_data, [WIDTH_ALL]), kinds=kinds)
    assert [entry["kind"] for entry in result["kinds"]] == ["Width"]


def _by_rule(entry):
    return {(e["rule"], e["objects"], e["violations"]) for e in entry["by_rule"]}


def test_no_net_via_scopes_are_decided_on_the_via(pcb_data):
    # A no-net via in R1's footprint (a mounting-hole ring) and CLK_A's free via.
    vias = [_via(None, 300, 300, hole=11.0, component=2), _via(1, 200, 100)]
    footprint = [
        _rule("RoutingVias", 1, "IsVia and InAnyComponent", name="ViaInFootprint",
              MAXHOLEWIDTH="12mil"),
        _rule("RoutingVias", 2, name="Default", MAXHOLEWIDTH="10mil"),
    ]
    result = check_compliance(_index(pcb_data, footprint, primitives=vias),
                              kinds=["RoutingVias"])
    routing = _kind(result, "RoutingVias")
    assert _by_rule(routing) == {("ViaInFootprint", 1, 0), ("Default", 1, 0)}
    assert routing["violation_count"] == 0

    # InAnyNet does not select a no-net via (it falls through); Not InAnyNet does.
    any_net = [
        _rule("RoutingVias", 1, "InAnyNet", name="RV_AnyNet", MAXWIDTH="15mil"),
        _rule("RoutingVias", 2, name="RV_All", MAXWIDTH="30mil"),
    ]
    free = [_via(None, 300, 300)]
    routing = _kind(check_compliance(_index(pcb_data, any_net, primitives=free),
                                     kinds=["RoutingVias"]), "RoutingVias")
    assert _by_rule(routing) == {("RV_All", 1, 0)}
    not_any = [replace(any_net[0], scope1="Not InAnyNet", name="RV_NoNet"), any_net[1]]
    routing = _kind(check_compliance(_index(pcb_data, not_any, primitives=free),
                                     kinds=["RoutingVias"]), "RoutingVias")
    assert _by_rule(routing) == {("RV_NoNet", 1, 1)}


def test_footprint_scopes_are_decided_per_object_for_net_vias(pcb_data):
    # CLK_A has no pad on R1, but this CLK_A via belongs to R1's footprint.
    rules = [
        _rule("HoleSize", 1, "IsVia and InComponent('R1')", name="Holes_R1",
              MINLIMIT="1mil", MAXLIMIT="10mil"),
        _rule("HoleSize", 2, name="Holes_All", MINLIMIT="1mil", MAXLIMIT="6mil"),
    ]
    vias = [_via(1, 300, 300, component=2), _via(1, 200, 100)]
    holes = _kind(check_compliance(_index(pcb_data, rules, primitives=vias),
                                   kinds=["HoleSize"]), "HoleSize")
    assert _by_rule(holes) == {("Holes_R1", 1, 0), ("Holes_All", 1, 1)}


DP_NARROW = _rule("DiffPairsRouting", 1, "InDifferentialPair('CLK')", name="DP_CLK",
                  TOPLAYER_MINWIDTH="7mil", TOPLAYER_MAXWIDTH="9mil",
                  MIDLAYER2_MINWIDTH="5mil", MIDLAYER2_MAXWIDTH="7mil")


def test_width_request_on_pair_nets_adds_diff_pairs_routing(pcb_data):
    index = _index(pcb_data, [WIDTH_ALL, DP_NARROW], pairs=[CLK_PAIR])
    for nets in (["CLK"], None):
        result = check_compliance(index, nets=nets, kinds=["Width"])
        assert [entry["kind"] for entry in result["kinds"]] == [
            "Width", "DiffPairsRouting",
        ]
        assert [entry["kind"] for entry in result["kinds_added"]] == ["DiffPairsRouting"]
        pairs = _kind(result, "DiffPairsRouting")
        assert (pairs["objects_checked"], pairs["violation_count"]) == (3, 2)
        assert _kind(result, "Width")["objects_checked"] == 0
    # No pair net selected, or no pair rule on the board: nothing is added.
    plain = check_compliance(index, nets=["GND"], kinds=["Width"])
    assert [entry["kind"] for entry in plain["kinds"]] == ["Width"]
    assert "kinds_added" not in plain
    no_rule = _index(pcb_data, [WIDTH_ALL], pairs=[CLK_PAIR])
    assert "kinds_added" not in check_compliance(no_rule, nets=["CLK"], kinds=["Width"])


def test_diff_pairs_routing_says_uncoupled_length_is_not_checked(pcb_data):
    index = _index(pcb_data, [WIDTH_ALL, DP_NARROW], pairs=[CLK_PAIR])
    result = check_compliance(index, kinds=["DiffPairsRouting"])
    assert _kind(result, "DiffPairsRouting")["checks"] == ["width"]
    (entry,) = [e for e in result["not_checked"] if e["kind"].startswith("DiffPairs")]
    assert "Max Uncoupled Length" in entry["reason"]
    widths = check_compliance(_index(pcb_data, [WIDTH_ALL]), kinds=["Width"])
    assert not any(e["kind"].startswith("DiffPairs") for e in widths["not_checked"])


def _hints(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("message", "reason", "skipped", "next_page"):
                yield item
            else:
                yield from _hints(item)
    elif isinstance(value, list):
        for item in value:
            yield from _hints(item)


def test_hints_name_real_tools_and_parameters(pcb_data):
    tools = {tool.name: tool for tool in asyncio.run(main_mod.mcp.list_tools())}
    parameters = set(tools["check_rule_compliance"].inputSchema["properties"])
    rules = [WIDTH_ALL, DP_NARROW, _rule("Clearance", 1, GENERICCLEARANCE="10mil")]
    index = _index(pcb_data, rules, pairs=[CLK_PAIR])
    outputs = [
        check_compliance(index, limit=1),
        check_compliance(index, kinds=["Clearance"]),
        check_compliance(index, nets=[]),
    ]
    texts = [text for output in outputs for text in _hints(output)]
    assert any("clearance_spot_check=True" in text for text in texts)
    assert any("offset=" in text for text in texts)
    assert any("check_lengths" in text for text in texts)
    for text in texts:
        for tool in re.findall(r"\b(check_\w+)\b", text):
            assert tool in tools, text
        for name in re.findall(r"\b([a-z_]+)=", text):
            assert name in parameters, text


def test_unparseable_scope_leaves_the_object_undecided(pcb_data):
    broken = "InNet('CLK_A') and ("
    rules = [
        _rule("Length", 1, broken, name="Len_Broken", MAXLIMIT="10mil"),
        _rule("Length", 2, name="Len_All", MAXLIMIT="10000mil"),
        _rule("Width", 1, broken, name="Width_Broken", MAXLIMIT="1mil"),
        WIDTH_ALL,
    ]
    result = check_compliance(_index(pcb_data, rules), nets=["CLK_A"],
                              kinds=["Length", "Width"])
    # Length treats it as Width does: the lower Len_All rule is not certain.
    for kind, objects in (("Length", 1), ("Width", 2)):
        entry = _kind(result, kind)
        assert entry["by_rule"] == []
        assert entry["undecided"]["count"] == objects
        (example,) = entry["undecided"]["examples"]
        (undecided,) = example["rules"]
        assert undecided["rule"].endswith("_Broken")
        assert undecided["conditions"] == [broken]
    (example,) = _kind(result, "Length")["undecided"]["examples"]
    assert example["rules"][0]["parse_error"]


def _arc(net, chords, y=0.0, width=6.0, source_id=7):
    return [
        Primitive(kind="arc", layer=1, net=net, component=None, x1=10.0 * i, y1=y,
                  x2=10.0 * (i + 1), y2=y, width=width, source_id=source_id,
                  length=10.0)
        for i in range(chords)
    ]


def test_clearance_counts_an_arc_once(pcb_data):
    primitives = _arc(1, 8) + [_track(2, 1, 0, 12, 80, 12, 6.0)]  # 6 mil apart
    rules = [_rule("Clearance", 1, name="Default", GENERICCLEARANCE="10mil")]
    index = _index(pcb_data, rules, primitives=primitives)
    clearance = _kind(check_compliance(index, nets=["CLK_A"], kinds=["Clearance"]),
                      "Clearance")
    assert (clearance["objects_checked"], clearance["violation_count"]) == (1, 1)
    assert clearance["target_objects"] == 1
    (violation,) = clearance["violations"]
    assert (violation["count"], violation["actual_mil"]) == (1, 6.0)
    assert violation["objects"] == "Arc-Track"

    # The arc as the other object: its scope facts still come from the arc.
    arc_rules = [_rule("Clearance", 1, "IsArc", name="Arc_Clr",
                       GENERICCLEARANCE="8mil"), rules[0]]
    index = _index(pcb_data, arc_rules, primitives=primitives)
    clearance = _kind(check_compliance(index, nets=["CLK_B"], kinds=["Clearance"]),
                      "Clearance")
    assert _by_rule(clearance) == {("Arc_Clr", 1, 1)}
    (violation,) = clearance["violations"]
    assert (violation["count"], violation["required_mil"]) == (1, 8.0)


def test_clearance_leaves_arc_chord_sagitta_to_the_chords(pcb_data):
    # Chords lie up to 0.25 mil inside their arc: a gap short of the limit by
    # no more than that (per arc) cannot be told from a violation. Tracks get
    # no such margin.
    rules = [_rule("Clearance", 1, name="Default", GENERICCLEARANCE="10mil")]
    for other, expected in (
        (_track(2, 1, 0, 15.8, 80, 15.8, 6.0), (0, 1)),  # 9.8 mil from the arc
        (_track(2, 1, 0, 15.7, 80, 15.7, 6.0), (1, None)),  # 9.7 mil
    ):
        index = _index(pcb_data, rules, primitives=_arc(1, 8) + [other])
        clearance = _kind(check_compliance(index, nets=["CLK_A"], kinds=["Clearance"]),
                          "Clearance")
        assert (clearance["violation_count"],
                clearance.get("arc_chord_marginal_pairs")) == expected
    tracks = [_track(1, 1, 0, 0, 80, 0, 6.0), _track(2, 1, 0, 15.8, 80, 15.8, 6.0)]
    clearance = _kind(check_compliance(_index(pcb_data, rules, primitives=tracks),
                                       nets=["CLK_A"], kinds=["Clearance"]), "Clearance")
    assert clearance["violation_count"] == 1


def test_violation_groups_are_paged_with_offset(pcb_data):
    primitives = [_track(0, 1, 0, y, 50, y, width)
                  for y, width in ((100, 1.0), (200, 2.0), (300, 3.0))]
    index = _index(pcb_data, [WIDTH_ALL], primitives=primitives)
    first = check_compliance(index, kinds=["Width"], limit=2)
    width = _kind(first, "Width")
    assert [v["actual_mil"] for v in width["violations"]] == [1.0, 2.0]
    assert (width["has_more"], width["next_offset"]) == (True, 2)
    assert "check_rule_compliance" in first["next_page"]
    second = check_compliance(index, kinds=["Width"], limit=2, offset=2)
    width = _kind(second, "Width")
    assert [v["actual_mil"] for v in width["violations"]] == [3.0]
    assert (width["has_more"], width["offset"]) == (False, 2)
    assert "next_offset" not in width and "next_page" not in second
    assert width["violation_count"] == 3  # counts never depend on the page
