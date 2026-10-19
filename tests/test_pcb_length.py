"""Length engine (pad-to-pad paths, via barrels, xSignals) and length rules."""

import math
from copy import deepcopy

import pytest

from server.parsers.pcb_doc import (
    BoardInfo,
    DiffPair,
    PcbClass,
    PcbComponent,
    PcbData,
    PcbLayer,
    PcbRule,
    Primitive,
    XSignal,
)
from server.services import pcb_length
from server.services.pcb_index import PcbIndex
from server.services.pcb_length import (
    check_length_rules,
    net_graph,
    net_length,
    pad_to_pad,
    pair_lengths,
    stack_z,
    xsignal_length,
)

MULTI = 74
NETS = [
    "STRAIGHT", "WIGGLE", "VIA_L3", "VIA_FULL", "TEE", "GAP", "ARC",
    "SER_A", "SER_B", "G100", "G97", "G90", "DP_P", "DP_N", "FLY", "VIP",
    "DIAG", "NOCOPPER",
]
N = {name: number for number, name in enumerate(NETS)}
COMPONENTS = ["U1", "U2", "U3", "R1", "J1"]
C = {name: number for number, name in enumerate(COMPONENTS)}

# z of copper centres: Top 0.7, L2 7.0, L3 48.2, Bottom 54.5.
Z_TOP_TO_L3 = 47.5
Z_TOP_TO_BOTTOM = 53.8


def _layer(v6, name, order, kind="copper", copper=None, dielectric=None):
    return PcbLayer(
        v6_id=v6, name=name, kind=kind, stack_order=order,
        copper_thick_mil=copper, diel_height_mil=dielectric,
    )


def _board():
    return BoardInfo(
        origin_x=0.0, origin_y=0.0, sheet_x=0.0, sheet_y=0.0,
        sheet_width=5000.0, sheet_height=5000.0, display_unit="mil",
        stackup_source="v9",
        layers=[
            _layer(1, "Top Layer", 0, copper=1.4),
            _layer(None, "Dielectric 1", 1, kind="dielectric", dielectric=5.0),
            _layer(2, "L2", 2, copper=1.2),
            _layer(None, "Dielectric 2", 3, kind="dielectric", dielectric=40.0),
            _layer(3, "L3", 4, copper=1.2),
            _layer(None, "Dielectric 3", 5, kind="dielectric", dielectric=5.0),
            _layer(32, "Bottom Layer", 6, copper=1.4),
        ],
    )


def _track(net, x1, y1, x2, y2, layer=1, width=4.0, length=None, kind="track"):
    return Primitive(
        kind=kind, layer=layer, net=N[net], component=None,
        x1=x1, y1=y1, x2=x2, y2=y2, width=width, length=length,
    )


def _pad(net, component, name, x, y, layer=1, size=10.0, shape=2, hole=0.0):
    return Primitive(
        kind="pad", layer=layer, net=N[net], component=C[component],
        x1=x, y1=y, x2=x, y2=y, width=size, height=size, shape=shape,
        hole=hole, pad_name=name,
    )


def _via(net, x, y, layer_from=1, layer_to=32, diameter=20.0):
    return Primitive(
        kind="via", layer=layer_from, layer_to=layer_to, net=N[net],
        component=None, x1=x, y1=y, x2=x, y2=y, width=diameter, hole=10.0,
    )


def _arc_chords(net, cx, cy, radius, start_deg, end_deg, steps):
    sweep = end_deg - start_deg
    true = radius * math.radians(sweep) / steps
    points = [
        (
            cx + radius * math.cos(math.radians(start_deg + sweep * k / steps)),
            cy + radius * math.sin(math.radians(start_deg + sweep * k / steps)),
        )
        for k in range(steps + 1)
    ]
    return [
        _track(net, a[0], a[1], b[0], b[1], width=2.0, length=true, kind="arc")
        for a, b in zip(points, points[1:])
    ]


def _straight_net(net, y, length, pin):
    return [
        _pad(net, "U1", pin, 0.0, y),
        _pad(net, "U2", pin, length, y),
        _track(net, 0.0, y, length, y),
    ]


def _primitives():
    prims = []
    prims += _straight_net("STRAIGHT", 0.0, 1000.0, "1")
    # In-pad wiggle: 10 extra mil of copper inside the 40 mil pad.
    prims += [
        _pad("WIGGLE", "U1", "2", 0.0, 100.0, size=40.0),
        _pad("WIGGLE", "U2", "2", 1000.0, 100.0),
        _track("WIGGLE", 0.0, 100.0, 0.0, 105.0),
        _track("WIGGLE", 0.0, 105.0, 10.0, 105.0),
        _track("WIGGLE", 10.0, 105.0, 10.0, 100.0),
        _track("WIGGLE", 10.0, 100.0, 1000.0, 100.0),
    ]
    # Top -> through via -> L3 -> through-hole pad: barrel only Top..L3.
    prims += [
        _pad("VIA_L3", "U1", "3", 0.0, 200.0),
        _track("VIA_L3", 0.0, 200.0, 100.0, 200.0),
        _via("VIA_L3", 100.0, 200.0),
        _track("VIA_L3", 100.0, 200.0, 300.0, 200.0, layer=3),
        _pad("VIA_L3", "J1", "1", 300.0, 200.0, layer=MULTI, size=30.0, shape=1, hole=15.0),
    ]
    # Top -> through via -> Bottom SMD pad: the full span.
    prims += [
        _pad("VIA_FULL", "U1", "4", 0.0, 300.0),
        _track("VIA_FULL", 0.0, 300.0, 100.0, 300.0),
        _via("VIA_FULL", 100.0, 300.0),
        _track("VIA_FULL", 100.0, 300.0, 300.0, 300.0, layer=32),
        _pad("VIA_FULL", "U3", "1", 300.0, 300.0, layer=32),
    ]
    # One long segment with a stub branching from its middle (T-junction).
    prims += [
        _pad("TEE", "U1", "5", 0.0, 400.0),
        _pad("TEE", "U2", "5", 1000.0, 400.0),
        _pad("TEE", "U3", "5", 500.0, 700.0),
        _track("TEE", 0.0, 400.0, 1000.0, 400.0),
        _track("TEE", 500.0, 400.0, 500.0, 700.0),
    ]
    # Unrouted middle: 200 mil in X and 30 mil in Y missing.
    prims += [
        _pad("GAP", "U1", "6", 0.0, 800.0),
        _pad("GAP", "U2", "6", 1000.0, 830.0),
        _track("GAP", 0.0, 800.0, 400.0, 800.0),
        _track("GAP", 600.0, 830.0, 1000.0, 830.0),
    ]
    # A 90 degree r = 20 arc stored as 3 chords carrying the true length.
    prims += [
        _pad("ARC", "U1", "7", 0.0, 900.0, size=4.0),
        _pad("ARC", "U2", "7", 20.0, 920.0, size=4.0),
        *_arc_chords("ARC", 0.0, 920.0, 20.0, -90.0, 0.0, 3),
    ]
    # Series resistor R1 between SER_A and SER_B.
    prims += [
        _pad("SER_A", "U1", "8", 0.0, 1000.0),
        _pad("SER_A", "R1", "1", 200.0, 1000.0),
        _track("SER_A", 0.0, 1000.0, 200.0, 1000.0),
        _pad("SER_B", "R1", "2", 240.0, 1000.0),
        _pad("SER_B", "U2", "8", 540.0, 1000.0),
        _track("SER_B", 240.0, 1000.0, 540.0, 1000.0),
    ]
    prims += _straight_net("G100", 1100.0, 100.0, "9")
    prims += _straight_net("G97", 1200.0, 97.0, "10")
    prims += _straight_net("G90", 1300.0, 90.0, "11")
    prims += _straight_net("DP_P", 1400.0, 500.0, "12")
    prims += _straight_net("DP_N", 1450.0, 494.5, "13")
    # A track running straight over a pad (no endpoint in it).
    prims += [
        _pad("FLY", "U1", "14", 0.0, 1500.0),
        _pad("FLY", "U2", "14", 1000.0, 1500.0),
        _pad("FLY", "U3", "14", 500.0, 1500.0, size=20.0),
        _track("FLY", 0.0, 1500.0, 1000.0, 1500.0),
    ]
    # Blind vias (Top..L3) in SMD pads, routed on L3.
    prims += [
        _pad("VIP", "U1", "15", 0.0, 1600.0, size=20.0),
        _via("VIP", 0.0, 1600.0, layer_to=3),
        _track("VIP", 0.0, 1600.0, 200.0, 1600.0, layer=3),
        _via("VIP", 200.0, 1600.0, layer_to=3),
        _pad("VIP", "U2", "15", 200.0, 1600.0, size=20.0),
    ]
    # A long diagonal with a branch landing mid-way (grid-hashed split).
    prims += [
        _pad("DIAG", "U1", "16", 0.0, 2000.0),
        _pad("DIAG", "U2", "16", 3000.0, 5000.0),
        _pad("DIAG", "U3", "16", 1500.0, 3700.0),
        _track("DIAG", 0.0, 2000.0, 3000.0, 5000.0),
        _track("DIAG", 1500.0, 3500.0, 1500.0, 3700.0),
    ]
    prims += [
        _pad("NOCOPPER", "U1", "17", 0.0, 2100.0),
        _pad("NOCOPPER", "U2", "17", 400.0, 2100.0),
    ]
    return prims


def _rule(name, kind, scope, priority=1, enabled=True, **props):
    return PcbRule(
        name=name, kind=kind, kind_id=3 if kind == "Length" else 4,
        enabled=enabled, priority=priority, scope1=scope, scope2="All",
        net_scope="AnyNet", comment="", unique_id=None, from_schematic=False,
        properties=props,
    )


def _matched(name, scope, tolerance="5mil", within=False, priority=1, **props):
    group = "FALSE" if within else "TRUE"
    return _rule(
        name, "MatchedLengths", scope, priority=priority,
        TOLERANCE=tolerance,
        CHECKNETSINDIFFPAIR="TRUE" if within else "FALSE",
        CHECKDIFFPAIRVSDIFFPAIR=group, CHECKXSIGNALS=group, CHECKOTHERS=group,
        **props,
    )


def _class(name, kind, members):
    return PcbClass(
        name=name, kind=kind, members=list(members), superclass=False,
        auto_generated=False, unique_id=None,
    )


@pytest.fixture
def length_data():
    return PcbData(
        board=_board(),
        nets=list(NETS),
        components=[
            PcbComponent(
                index=index, refdes=name, source_uid_tail=None,
                channel_path=None, x=0.0, y=0.0, rotation=0.0, side="top",
                designator=name,
            )
            for index, name in enumerate(COMPONENTS)
        ],
        primitives=_primitives(),
        pours=[],
        layer_name_to_v6={
            "top layer": 1, "l2": 2, "l3": 3, "bottom layer": 32,
            "top": 1, "bottom": 32,
        },
        xsignals=[
            XSignal("X100", ["U1-9", "U2-9"]),
            XSignal("X97", ["U1-10", "U2-10"]),
            XSignal("X90", ["U1-11", "U2-11"]),
            XSignal("XSER", ["U1-8", "R1-1", "R1-2", "U2-8"]),
        ],
        classes=[
            _class("XG", "xsignal", ["X100", "X97", "X90"]),
            _class("GN", "net", ["G100", "G97", "G90"]),
            _class("PC", "diff_pair", ["DP"]),
        ],
        diff_pairs=[DiffPair("DP", "DP_P", "DP_N", None)],
        drc_rule_kinds={3, 4},
    )


def _index(data, rules=()):
    data = deepcopy(data)
    data.rules = list(rules)
    return PcbIndex(data, None)


@pytest.fixture
def index(length_data):
    return _index(length_data)


def _floats_outside_mil_keys(value, inside_mil=False, path="$"):
    """Paths of float values that _convert_units would not convert."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            found += _floats_outside_mil_keys(
                item, inside_mil or key.endswith("_mil"), f"{path}.{key}"
            )
    elif isinstance(value, list):
        for position, item in enumerate(value):
            found += _floats_outside_mil_keys(item, inside_mil, f"{path}[{position}]")
    elif isinstance(value, float) and not inside_mil:
        found.append(path)
    return found


# ---------- stackup ----------


def test_stack_z_copper_centres(index):
    z = stack_z(index)
    assert z == pytest.approx({1: 0.7, 2: 7.0, 3: 48.2, 32: 54.5})


def test_stack_z_none_when_a_thickness_is_missing(length_data):
    length_data.board.layers[3].diel_height_mil = None
    index = _index(length_data)
    assert stack_z(index) is None
    result = pad_to_pad(index, "VIA_L3", "U1-3", "J1-1")
    assert result["status"] == "ok"
    assert result["via_barrel_mil"] is None
    assert result["length_mil"] == pytest.approx(300.0)
    rules = [_matched("M", "InNetClass('GN')")]
    report = check_length_rules(_index(length_data, rules))
    assert any("barrel" in item for item in report["unknown"])


def test_stack_z_none_for_legacy_stackup(length_data):
    for layer in length_data.board.layers:
        layer.stack_order = None
    assert stack_z(_index(length_data)) is None


# ---------- paths ----------


def test_straight_two_pad_net_path_equals_routed(index):
    result = net_length(index, "STRAIGHT")
    assert result["measure"] == "signal_path"
    assert result["length_mil"] == pytest.approx(1000.0)
    assert result["routed_mil"] == pytest.approx(1000.0)
    assert result["status"] == "ok"
    assert result["pads"] == ["U1-1", "U2-1"]


def test_in_pad_wiggle_is_ignored(index):
    result = net_length(index, "WIGGLE")
    assert result["routed_mil"] == pytest.approx(1010.0)
    assert result["length_mil"] == pytest.approx(1000.0)


def test_via_barrel_counts_only_the_traversed_span(index):
    result = pad_to_pad(index, "VIA_L3", "U1-3", "J1-1")
    assert result["via_barrel_mil"] == pytest.approx(Z_TOP_TO_L3)
    assert result["copper_mil"] == pytest.approx(300.0)
    assert result["length_mil"] == pytest.approx(300.0 + Z_TOP_TO_L3)
    assert result["vias"] == 1
    assert result["layers_mil"] == {"Top Layer": 100.0, "L3": 200.0}


def test_full_via_traversal_is_layers_between_plus_half_outer_copper(index):
    result = pad_to_pad(index, None, "U1-4", "U3-1")
    # 5 + 1.2 + 40 + 1.2 + 5 between, plus 0.7 + 0.7 of the outer copper.
    assert result["via_barrel_mil"] == pytest.approx(Z_TOP_TO_BOTTOM)
    assert result["net"] == "VIA_FULL"


def test_t_junction_stub_is_excluded_from_the_path(index):
    assert pad_to_pad(index, "TEE", "U1-5", "U2-5")["length_mil"] == pytest.approx(1000.0)
    assert pad_to_pad(index, "TEE", "U1-5", "U3-5")["length_mil"] == pytest.approx(800.0)
    whole = net_length(index, "TEE")
    assert whole["measure"] == "routed_plus_vias"
    assert whole["pad_count"] == 3
    assert whole["length_mil"] == pytest.approx(1300.0)


def test_t_junction_found_on_a_long_diagonal(index):
    # The branch lands 2121 mil along a 4243 mil diagonal.
    result = pad_to_pad(index, "DIAG", "U1-16", "U3-16")
    assert result["status"] == "ok"
    assert result["length_mil"] == pytest.approx(1500.0 * math.sqrt(2) + 200.0)


def test_no_path_reports_unrouted_manhattan(index):
    result = pad_to_pad(index, "GAP", "U1-6", "U2-6")
    assert result["status"] == "no_path"
    assert result["length_mil"] is None
    assert result["unrouted_manhattan_mil"] == pytest.approx(230.0)
    whole = net_length(index, "GAP")
    assert whole["status"] == "incomplete"
    assert whole["unrouted_manhattan_mil"] == pytest.approx(230.0)


def test_net_without_copper_is_unrouted(index):
    result = net_length(index, "NOCOPPER")
    assert result["status"] == "unrouted"
    assert result["unrouted_manhattan_mil"] == pytest.approx(400.0)


def test_arc_uses_true_length_not_chords(index):
    result = pad_to_pad(index, "ARC", "U1-7", "U2-7")
    assert result["length_mil"] == pytest.approx(10.0 * math.pi, abs=0.01)
    assert net_length(index, "ARC")["routed_mil"] == pytest.approx(31.42, abs=0.01)


def test_track_running_over_a_pad_connects_it(index):
    assert pad_to_pad(index, "FLY", "U1-14", "U3-14")["length_mil"] == pytest.approx(500.0)
    assert pad_to_pad(index, "FLY", "U3-14", "U2-14")["length_mil"] == pytest.approx(500.0)


def test_via_in_pad_connects_without_a_track(index):
    result = pad_to_pad(index, "VIP", "U1-15", "U2-15")
    assert result["status"] == "ok"
    assert result["vias"] == 2
    assert result["via_barrel_mil"] == pytest.approx(2 * Z_TOP_TO_L3)
    assert result["length_mil"] == pytest.approx(200.0 + 2 * Z_TOP_TO_L3)


def test_routed_plus_vias_counts_each_via_over_its_connected_span(length_data):
    # A third pad turns VIA_L3 into a multi-pad net.
    length_data.primitives.append(_pad("VIA_L3", "U2", "99", 0.0, 260.0))
    length_data.primitives.append(_track("VIA_L3", 0.0, 200.0, 0.0, 260.0))
    result = net_length(_index(length_data), "VIA_L3")
    assert result["measure"] == "routed_plus_vias"
    assert result["via_barrel_mil"] == pytest.approx(Z_TOP_TO_L3)
    assert result["length_mil"] == pytest.approx(360.0 + Z_TOP_TO_L3)
    assert result["vias"] == 1


def test_net_graph_is_cached_on_the_index(index):
    first = net_graph(index, index.net_id("STRAIGHT"))
    assert net_graph(index, index.net_id("STRAIGHT")) is first


# ---------- xSignals and pairs ----------


def test_xsignal_series_hop_counts_zero(index):
    result = xsignal_length(index, "xser")
    assert result["status"] == "ok"
    assert result["length_mil"] == pytest.approx(500.0)
    assert result["series_hops"] == ["R1"]
    assert [leg["net"] for leg in result["legs"]] == ["SER_A", "SER_B"]
    assert [leg["length_mil"] for leg in result["legs"]] == [200.0, 300.0]


def test_xsignal_pin_not_found_and_broken_chain(length_data):
    length_data.xsignals += [
        XSignal("XMISSING", ["U1-1", "U9-1"]),
        XSignal("XBROKEN", ["U1-1", "U2-9"]),
    ]
    index = _index(length_data)
    missing = xsignal_length(index, "XMISSING")
    assert missing["status"] == "pin_not_found"
    assert missing["missing_pins"] == ["U9-1"]
    assert missing["length_mil"] is None
    broken = xsignal_length(index, "XBROKEN")
    assert broken["status"] == "broken_chain"
    assert broken["broken_between"] == ["U1-1", "U2-9"]


def test_pair_lengths(index):
    result = pair_lengths(index, "dp")
    assert result["positive"]["length_mil"] == pytest.approx(500.0)
    assert result["negative"]["length_mil"] == pytest.approx(494.5)
    assert result["length_mil"] == pytest.approx(500.0)
    assert result["avg_mil"] == pytest.approx(497.25)
    assert result["skew_mil"] == pytest.approx(5.5)
    assert result["status"] == "ok"


def test_lookup_errors(index):
    assert net_length(index, "NOPE")["error"] == "net_not_found"
    assert xsignal_length(index, "X1")["error"] == "xsignal_not_found"
    assert pair_lengths(index, "NOPE")["error"] == "pair_not_found"
    assert pad_to_pad(index, None, "U1-1", "U9-9")["error"] == "pin_not_found"
    assert pad_to_pad(index, "STRAIGHT", "U1-1", "U2-2")["error"] == "pin_not_on_net"
    assert pad_to_pad(index, "NOPE", "U1-1", "U2-1")["error"] == "net_not_found"


# ---------- rules ----------


def _only_rule(report):
    assert report["rule_count"] == 1
    return report["rules"][0]


def _by_object(rows):
    return {row["object"]: row for row in rows}


def test_group_reference_is_the_longest_member(length_data):
    index = _index(length_data, [_matched("ML", "InxSignalClass('XG')")])
    rule = _only_rule(check_length_rules(index, rule="ml"))
    assert rule["mode"] == "group"
    assert rule["member_kinds"] == ["xsignal"]
    assert rule["reference"]["object"] == "X100"
    assert rule["reference"]["source"] == "longest_member"
    assert rule["reference"]["band_min_mil"] == pytest.approx(95.0)
    rows = _by_object(rule["members"])
    assert rows["X90"]["status"] == "too_short"
    assert rows["X90"]["delta_to_target_mil"] == pytest.approx(-10.0)
    assert rows["X90"]["outside_band_mil"] == pytest.approx(5.0)
    assert rows["X97"]["status"] == "ok"
    assert rule["summary"]["violations"] == 1
    assert rule["summary"]["spread_mil"] == pytest.approx(10.0)
    # Problems first.
    assert rule["members"][0]["object"] == "X90"


def test_group_target_source_band(length_data):
    rules = [_matched("ML", "InxSignalClass('XG')", TARGETSOURCENAME="X97")]
    rule = _only_rule(check_length_rules(_index(length_data, rules), rule="ML"))
    assert rule["reference"]["source"] == "target_source"
    assert rule["reference"]["band_min_mil"] == pytest.approx(92.0)
    assert rule["reference"]["band_max_mil"] == pytest.approx(102.0)
    rows = _by_object(rule["members"])
    assert rows["X90"]["status"] == "too_short"
    assert rows["X100"]["status"] == "ok"


def test_missing_target_source_falls_back_to_longest(length_data):
    rules = [_matched("ML", "InxSignalClass('XG')", TARGETSOURCENAME="GONE")]
    report = check_length_rules(_index(length_data, rules), rule="ML")
    rule = _only_rule(report)
    assert rule["reference"]["source"] == "longest_member"
    assert "targetsource('GONE')" in rule["stale_references"]
    assert any("GONE" in item for item in report["unknown"])


def test_group_of_nets_and_pairs(length_data):
    rules = [_matched("ML", "InNetClass('GN') or InDifferentialPairClass('PC')")]
    rule = _only_rule(check_length_rules(_index(length_data, rules), rule="ML"))
    assert rule["member_kinds"] == ["diff_pair", "net"]
    rows = _by_object(rule["members"])
    assert set(rows) == {"DP", "G100", "G97", "G90"}
    # The pair counts as max(P, N); its nets are not "other nets" again.
    assert rows["DP"]["length_mil"] == pytest.approx(500.0)
    assert rows["DP"]["avg_mil"] == pytest.approx(497.25)
    assert rule["reference"]["object"] == "DP"
    assert rows["G100"]["status"] == "too_short"


def test_within_pair_skew(length_data):
    rules = [_matched("WP", "InDifferentialPairClass('PC')", within=True)]
    rule = _only_rule(check_length_rules(_index(length_data, rules), rule="WP"))
    assert rule["mode"] == "within_pair"
    pair = rule["pairs"][0]
    assert pair["skew_mil"] == pytest.approx(5.5)
    assert pair["status"] == "skew_exceeded"
    assert pair["outside_band_mil"] == pytest.approx(0.5)
    assert pair["marginal"] is True
    assert rule["pair_summary"]["violations"] == 1


def test_delay_units_are_not_evaluated(length_data):
    rules = [
        _rule("LD", "Length", "InNet('STRAIGHT')", USEDELAYUNITS="TRUE",
              MINDELAY="100", MAXDELAY="200"),
    ]
    report = check_length_rules(_index(length_data, rules))
    rule = _only_rule(report)
    assert rule["status"] == "not_evaluated_delay_units"
    assert rule["member_count"] == 1
    assert any("delay" in item for item in report["unknown"])


def test_stale_scope_is_reported(length_data):
    rules = [_matched("ML", "InxSignalClass('Gone') or InNetClass('Old')")]
    rule = _only_rule(check_length_rules(_index(length_data, rules)))
    assert rule["status"] == "no_members"
    assert rule["stale_references"] == [
        "inxsignalclass('Gone')", "innetclass('Old')",
    ]


def test_length_rule_range_and_priority(length_data):
    rules = [
        _rule("L1", "Length", "InNet('STRAIGHT')", priority=1,
              MINLIMIT="900mil", MAXLIMIT="950mil"),
        _rule("L2", "Length", "InNetClass('GN') or InNet('STRAIGHT')",
              priority=2, MINLIMIT="95mil", MAXLIMIT="2000mil"),
        _rule("LX", "Length", "InxSignal('X100')", priority=3,
              MINLIMIT="99mil", MAXLIMIT="101mil"),
    ]
    report = check_length_rules(_index(length_data, rules), problems_only=False)
    rules_out = {rule["name"]: rule for rule in report["rules"]}
    straight = rules_out["L1"]["members"][0]
    assert straight["status"] == "too_long"
    assert straight["outside_band_mil"] == pytest.approx(50.0)
    second = rules_out["L2"]
    assert second["overridden"]["objects"] == [
        {"object": "STRAIGHT", "kind": "net", "by_rule": "L1"}
    ]
    assert set(_by_object(second["members"])) == {"G100", "G97", "G90"}
    assert _by_object(second["members"])["G90"]["status"] == "too_short"
    xsig = rules_out["LX"]
    assert xsig["member_kinds"] == ["xsignal"]
    assert xsig["members"][0]["status"] == "ok"


def test_pagination_and_problem_listing(length_data):
    index = _index(length_data, [_matched("ML", "InxSignalClass('XG')")])
    overview = _only_rule(check_length_rules(index))
    assert overview["listing"] == "problems_only"
    assert [row["object"] for row in overview["members"]] == ["X90"]
    assert overview["summary"]["members"] == 3
    first = _only_rule(check_length_rules(index, rule="ML", limit=2))
    assert first["members_page"] == {
        "total": 3, "offset": 0, "has_more": True, "next_offset": 2,
    }
    second = _only_rule(check_length_rules(index, rule="ML", offset=2, limit=2))
    assert len(second["members"]) == 1
    assert second["members_page"]["has_more"] is False


def test_target_filter(length_data):
    rules = [
        _matched("ML", "InxSignalClass('XG')"),
        _matched("WP", "InDifferentialPairClass('PC')", within=True),
    ]
    index = _index(length_data, rules)
    by_net = check_length_rules(index, target="G97")
    assert by_net["target"] == {"name": "G97", "kind": "net"}
    rule = _only_rule(by_net)
    assert rule["name"] == "ML"
    assert [row["object"] for row in rule["members"]] == ["X97"]
    by_pair = check_length_rules(index, target="DP_N")
    assert [rule["name"] for rule in by_pair["rules"]] == ["WP"]


def test_conditional_members_are_listed(length_data):
    rules = [_matched("ML", "InNetClass('GN') and OnLayer('Top Layer')")]
    rule = _only_rule(check_length_rules(_index(length_data, rules)))
    assert rule["status"] == "no_members"
    assert rule["conditional_count"] == 3
    assert rule["conditional"][0]["conditions"] == ["OnLayer('Top Layer')"]


def test_disabled_and_unknown_rules(length_data):
    rules = [_matched("OFF", "All", enabled=False)]
    index = _index(length_data, rules)
    assert _only_rule(check_length_rules(index))["status"] == "disabled"
    assert check_length_rules(index, rule="nope")["error"] == "rule_not_found"
    assert check_length_rules(index, target="nope")["error"] == "target_not_found"
    assert "note" in check_length_rules(_index(length_data))


def test_outputs_keep_lengths_under_mil_keys(length_data):
    rules = [
        _matched("ML", "InxSignalClass('XG') or InNetClass('GN')"),
        _matched("WP", "InDifferentialPairClass('PC')", within=True),
        _rule("L", "Length", "InNet('VIA_L3')", MINLIMIT="1mil", MAXLIMIT="2mil"),
    ]
    index = _index(length_data, rules)
    outputs = [
        check_length_rules(index, problems_only=False),
        net_length(index, "VIA_L3"),
        net_length(index, "TEE"),
        net_length(index, "GAP"),
        xsignal_length(index, "XSER"),
        pair_lengths(index, "DP"),
        pad_to_pad(index, "VIA_L3", "U1-3", "J1-1"),
    ]
    for output in outputs:
        assert _floats_outside_mil_keys(output) == []


def test_corpus_shaped_rule_values_are_interpreted(length_data):
    rules = [_matched("ML", "InxSignalClass('XG')", tolerance="3.937mil")]
    report = check_length_rules(_index(length_data, rules))
    rule = _only_rule(report)
    assert rule["tolerance_mil"] == pytest.approx(3.937)
    assert rule["group_checks"] == {"xsignals": True, "diff_pairs": True, "other_nets": True}
    assert rule["in_batch_drc"] is True
    assert report["unknown"] == []
    assert report["measure"] == pcb_length.MEASURE
