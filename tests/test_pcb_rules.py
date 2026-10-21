"""Scope evaluation, rule values and rule resolution (services/pcb_rules)."""

from dataclasses import replace

import pytest

from server.parsers.pcb_doc import PcbRule
from server.services.pcb_rules import (
    MULTI_LAYER,
    ObjectFacts,
    ObjectResolver,
    RuleContext,
    RuleTarget,
    ScopeSyntaxError,
    evaluate_scope,
    parse_scope,
    resolve_kind,
    rule_header,
    rule_values,
    scope_name,
    stale_references,
)


@pytest.fixture
def ctx():
    return RuleContext(
        net_classes={"clocks": {"clk_a", "clk_b"}, "power": {"3v3"}},
        pair_classes={"hs": {"clk"}},
        component_classes={"ctrl": {"u3"}},
        pad_classes={"testpads": {"u3-1"}},
        superclasses={
            "net": {"all nets"},
            "diff_pair": {"all differential pairs"},
        },
        pairs={"clk": ("CLK", "CLK_A", "CLK_B")},
        net_names={"clk_a": "CLK_A", "clk_b": "CLK_B", "gnd": "GND", "3v3": "3V3"},
        rooms={"rf"},
    )


def _net(name, pairs=(), components=(), pads=(), footprints=()):
    return RuleTarget(
        kind="net",
        name=name,
        nets={name.lower()},
        pairs=set(pairs),
        components=set(components),
        footprints=set(footprints),
        pads=set(pads),
    )


CLK_A = _net("CLK_A", pairs={"clk"}, components={"u3"}, pads={"u3-1"},
             footprints={"QFN32"})
GND = _net("GND", components={"r1"}, pads={"r1-1"}, footprints={"R0603"})
PAIR = RuleTarget(kind="diff_pair", name="CLK", nets={"clk_a", "clk_b"},
                  pairs={"clk"})


def _matches(scope, target, ctx):
    return evaluate_scope(scope, target, ctx)["matches"]


@pytest.mark.parametrize("scope, expected", [
    ("All", "yes"),
    ("InNet('CLK_A')", "yes"),
    ("InNet('clk_a')", "yes"),
    ("InNet('GND')", "no"),
    ("InNet('GND', 'CLK_A')", "yes"),
    ("InNet('CLK_*')", "yes"),
    ("InNet('CL?_A')", "yes"),
    ("InNetClass('Clocks')", "yes"),
    ("innetclass('clocks') and not inpolygon", "yes"),
    ("InNetClass('All Nets')", "yes"),
    ("InNetClass('Power')", "no"),
    ("InDifferentialPair('CLK')", "yes"),
    ("InDifferentialPairClass('HS')", "yes"),
    ("InDifferentialPairClass('All Differential Pairs')", "yes"),
    ("InAnyDifferentialPair", "yes"),
    ("Not InNet('GND') && Not InNet('3V3')", "yes"),
    ("IsBGA", "no"),
    ("IsSOIC", "no"),
    ("(ObjectKind = 'Text') And (StringText = 'Made in X')", "no"),
    ("WithinRoom('Missing')", "no"),
    ("InComponent('U9')", "no"),
    ("HasFootprint('SOT*')", "no"),
    ("False", "no"),
    ("InNet('X') or InNet('CLK_A') and InNet('Y')", "no"),
    ("(InNet('X') or InNet('CLK_A')) and InNetClass('Clocks')", "yes"),
])
def test_scope_on_net(ctx, scope, expected):
    assert _matches(scope, CLK_A, ctx) == expected


@pytest.mark.parametrize("scope, condition", [
    ("IsVia", "IsVia"),
    ("InNetClass('Clocks') and IsVia", "IsVia"),
    ("OnLayer('Top Layer')", "OnLayer('Top Layer')"),
    ("WithinRoom('RF')", "WithinRoom('RF')"),
    ("InComponent('U3')", "InComponent('U3')"),
    ("InComponentClass('Ctrl')", "InComponentClass('Ctrl')"),
    ("HasFootprint('QFN*')", "HasFootprint('QFN*')"),
    ("InPadClass('TestPads')", "InPadClass('TestPads')"),
    ("(CompPinCount < 5)", "CompPinCount < 5"),
    ("SomeFutureKeyword('x')", "SomeFutureKeyword('x')"),
])
def test_scope_conditional_names_the_condition(ctx, scope, condition):
    result = evaluate_scope(scope, CLK_A, ctx)
    assert result["matches"] == "conditional"
    assert condition in result["conditions"]


def test_not_of_condition_stays_conditional(ctx):
    result = evaluate_scope("Not IsVia", CLK_A, ctx)
    assert result == {"matches": "conditional", "conditions": ["Not IsVia"]}


def test_or_with_true_side_is_decided(ctx):
    assert _matches("IsVia or InNet('CLK_A')", CLK_A, ctx) == "yes"
    assert _matches("IsVia and InNet('GND')", CLK_A, ctx) == "no"


def test_comparison_without_components_is_no(ctx):
    bare = _net("ORPHAN")
    assert _matches("(CompPinCount < 5)", bare, ctx) == "no"


def test_unquoted_and_digit_leading_arguments(ctx):
    target = _net("3V3")
    assert _matches("InNet (DDR4_ODT1) or InNet (3V3)", target, ctx) == "yes"


def test_quoted_names_may_contain_keywords_and_parens(ctx):
    node = parse_scope("InNetClass('Address and Command (g2)') Or All")
    assert node[0] == "or"
    assert node[1][2] == ["Address and Command (g2)"]


def test_doubled_quote_escape():
    node = parse_scope("InNet('it''s')")
    assert node[2] == ["it's"]


def test_parse_error_is_reported_not_raised(ctx):
    with pytest.raises(ScopeSyntaxError):
        parse_scope("InNet('A'")
    result = evaluate_scope("InNet('A'", CLK_A, ctx)
    assert result["matches"] == "unknown"
    assert "parse_error" in result


def test_scope_on_pair(ctx):
    assert _matches("InDifferentialPairClass('HS')", PAIR, ctx) == "yes"
    assert _matches("InDifferentialPair('CLK')", PAIR, ctx) == "yes"
    assert _matches("IsVia", PAIR, ctx) == "no"
    # The pair object is not a net, but its member nets match InNet.
    assert _matches("InNet('CLK_A')", PAIR, ctx) == "conditional"
    assert _matches("InNet('GND')", PAIR, ctx) == "no"


def test_net_not_in_pair(ctx):
    assert _matches("InAnyDifferentialPair", GND, ctx) == "no"
    assert _matches("InDifferentialPairClass('All Differential Pairs')", GND, ctx) == "no"


def test_stale_references(ctx):
    scope = "InNetClass('Gone') or InNet('CLK_A') or InDifferentialPair('Old_*') or WithinRoom('RF')"
    assert stale_references(scope, ctx) == [
        "innetclass('Gone')",
        "indifferentialpair('Old_*')",
    ]
    assert stale_references("All", ctx) == []
    assert stale_references("InNetClass('All Nets')", ctx) == []


# ---------- values ----------

def _rule(kind, priority=1, scope1="All", scope2="All", enabled=True, name=None, **props):
    return PcbRule(
        name=name or f"{kind}_{priority}",
        kind=kind,
        kind_id=0,
        enabled=enabled,
        priority=priority,
        scope1=scope1,
        scope2=scope2,
        net_scope="AnyNet",
        comment="",
        unique_id=None,
        from_schematic=False,
        properties=props,
    )


SIGNAL_LAYERS = {1: "Top", 3: "Mid2", 32: "Bottom"}


def test_width_values_with_sparse_per_layer_overrides():
    rule = _rule(
        "Width",
        MINLIMIT="5mil", PREFEREDWIDTH="8mil", MAXLIMIT="20mil",
        TOPLAYER_MINWIDTH="6mil", MIDLAYER9_MINWIDTH="4mil",
        BOTTOMLAYER_MINWIDTH="7mil",
        MINIMP="50.000000", FAVIMP="50.000000", MAXIMP="50.000000",
    )
    values = rule_values(rule, SIGNAL_LAYERS)
    assert values["layers_mil"]["Top"] == {
        "min_width": 6.0, "preferred_width": 8.0, "max_width": 20.0,
    }
    assert values["layers_mil"]["Bottom"]["min_width"] == 7.0
    # Absent per-layer keys fall back to the base; slots not on the board drop.
    assert values["layers_mil"]["Mid2"]["min_width"] == 5.0
    assert set(values["layers_mil"]) == {"Top", "Mid2", "Bottom"}
    # With a per-layer table the base numbers are not repeated at the top,
    # and the legacy 50 ohm default keys are not an impedance target.
    assert "min_width_mil" not in values
    assert not any("impedance" in key for key in values)


def test_width_impedance_profile_is_reported_only_when_driven():
    driven = rule_values(
        _rule("Width", MINLIMIT="5mil", IMPEDANCEPROFILEDRIVEN="TRUE",
              IMPEDANCEPROFILEVALUE="40.000000"),
        SIGNAL_LAYERS,
    )
    assert driven["impedance_profile_ohm"] == 40.0
    assert driven["min_width_mil"] == 5.0
    not_driven = rule_values(
        _rule("Width", MINLIMIT="5mil", IMPEDANCEPROFILEDRIVEN="FALSE",
              IMPEDANCEPROFILEVALUE="50.000000"),
        SIGNAL_LAYERS,
    )
    assert "impedance_profile_ohm" not in not_driven


def test_width_without_layer_overrides_has_no_layer_table():
    values = rule_values(_rule("Width", MINLIMIT="5mil"), SIGNAL_LAYERS)
    assert "layers_mil" not in values


def test_diff_pair_values_per_layer_with_gap_fallback():
    rule = _rule(
        "DiffPairsRouting",
        MINLIMIT="4mil", MOSTFREQGAP="5mil", MAXLIMIT="6mil",
        MAXUNCOUPLEDLENGTH="500mil",
        TOPLAYER_MINWIDTH="4mil", TOPLAYER_PREFWIDTH="4.5mil", TOPLAYER_MAXWIDTH="5mil",
        TOPLAYER_PREFGAP="7mil",
        MIDLAYER2_MINWIDTH="3mil",
        MIDLAYER20_MINWIDTH="9mil",
    )
    values = rule_values(rule, SIGNAL_LAYERS)
    assert values["max_uncoupled_length_mil"] == 500.0
    assert "preferred_gap_mil" not in values
    top = values["layers_mil"]["Top"]
    assert top["preferred_gap"] == 7.0
    assert top["min_gap"] == 4.0
    assert top["preferred_width"] == 4.5
    mid = values["layers_mil"]["Mid2"]
    assert mid["min_width"] == 3.0
    # Gaps missing on a layer fall back to MINLIMIT / MOSTFREQGAP / MAXLIMIT.
    assert (mid["min_gap"], mid["preferred_gap"], mid["max_gap"]) == (4.0, 5.0, 6.0)
    assert "Mid20" not in str(values)


def test_substack_keys_are_kept_apart():
    rule = _rule(
        "Width",
        MINLIMIT="5mil", TOPLAYER_MINWIDTH="6mil",
        **{"TOPLAYER_{AB-12}_MINWIDTH": "3mil", "SUBSTACK1": "{AB-12}"},
    )
    values = rule_values(rule, SIGNAL_LAYERS)
    assert values["substack_layers_mil"] == {"{AB-12}": {"Top": {"min_width": 3.0}}}
    named = rule_values(rule, SIGNAL_LAYERS, [
        {"id": "{RIGID}", "name": "Rigid", "is_flex": False},
        {"id": "{ab-12}", "name": "Flex", "is_flex": True},
    ])
    assert named["substack_layers_mil"] == {"Flex": {"Top": {"min_width": 3.0}}}
    assert named["layers_substack"] == "Rigid"


def test_clearance_object_matrix_uses_raw_units():
    rule = _rule(
        "Clearance",
        GAP="6mil", GENERICCLEARANCE="6mil",
        IGNOREPADTOPADCLEARANCEINFOOTPRINT="TRUE",
        OBJECTCLEARANCES=(
            "ClearanceObj_Track-ClearanceObj_SMDPad:35433;"
            "ClearanceObj_Via-ClearanceObj_Hole:0"
        ),
    )
    values = rule_values(rule, SIGNAL_LAYERS)
    assert values["clearance_mil"] == 6.0
    assert values["ignore_pad_to_pad_in_footprint"] is True
    assert values["object_clearances_mil"] == {"Track-SMDPad": 3.5433, "Via-Hole": 0.0}
    assert "gap_mil" not in values and "other_mil" not in values


def test_unitless_numbers_are_not_lengths():
    rule = _rule(
        "HoleSize",
        MINLIMIT="1mil", MAXLIMIT="100mil", MINPERCENT="20.000",
        RELIEFENTRIES="4", ANGLE=" 4.50000000000000E+0001",
    )
    values = rule_values(rule, SIGNAL_LAYERS)
    assert values["min_percent"] == 20.0
    assert values["other"] == {"reliefentries": 4, "angle": 45.0}
    assert "other_mil" not in values


def test_unknown_kind_keeps_every_value():
    rule = _rule("FutureKind", SOMEGAP="3mil", MODE="Fast", FLAG="TRUE")
    values = rule_values(rule, SIGNAL_LAYERS)
    assert values == {
        "other_mil": {"somegap": 3.0},
        "other": {"mode": "Fast", "flag": True},
    }


def test_routing_layers_filtered_to_board():
    rule = _rule(
        "RoutingLayers",
        **{"TOP LAYER_V5": "TRUE", "MID LAYER 2_V5": "FALSE",
           "MID LAYER 7_V5": "TRUE", "BOTTOM LAYER_V5": "FALSE"},
    )
    values = rule_values(rule, SIGNAL_LAYERS)
    assert values["routing_layers"] == {"Top": True, "Mid2": False, "Bottom": False}


def test_room_geometry():
    rule = _rule(
        "RoomDefinition",
        LAYER="BOTTOM", CONFINEMENTSTYLE="ConfineIn",
        VX0="0mil", VY0="0mil", VX1="100mil", VY1="0mil",
        VX2="100mil", VY2="50mil", KIND0="0",
    )
    values = rule_values(rule, SIGNAL_LAYERS)
    assert values["side"] == "bottom"
    assert values["vertex_count"] == 3
    assert values["bbox_mil"]["maxx_mil"] == 100.0
    assert values["other"] == {"confinementstyle": "ConfineIn", "layer": "BOTTOM"}


# ---------- resolution ----------

def _describe(rule):
    return rule_header(rule)


def test_unary_highest_priority_match_wins(ctx):
    rules = [
        _rule("Width", 3, "All", name="Default"),
        _rule("Width", 1, "InNet('GND')", name="Gnd"),
        _rule("Width", 2, "InNetClass('Clocks')", name="Clocks"),
    ]
    result = resolve_kind(rules, CLK_A, ctx, _describe)
    assert result["effective"]["name"] == "Clocks"
    assert "overridden_for_some_objects_by" not in result


def test_disabled_rules_are_skipped_and_named_when_nothing_applies(ctx):
    rules = [_rule("NetAntennae", 1, "All", enabled=False, name="Off")]
    result = resolve_kind(rules, CLK_A, ctx, _describe)
    assert result["effective"] is None
    assert result["disabled_rules"] == ["Off"]


def test_conditional_higher_priority_rules_are_listed(ctx):
    rules = [
        _rule("Width", 1, "InNet('CLK_A') and OnLayer('Top')", name="TopOnly"),
        _rule("Width", 2, "All", name="Default"),
    ]
    result = resolve_kind(rules, CLK_A, ctx, _describe)
    assert result["effective"]["name"] == "Default"
    override = result["overridden_for_some_objects_by"][0]
    assert override["name"] == "TopOnly"
    assert override["conditions"] == ["OnLayer('Top')"]


def test_only_conditional_rules_are_not_called_overrides(ctx):
    rules = [_rule("PlaneConnect", 1, "IsVia", name="ViaConnect")]
    result = resolve_kind(rules, CLK_A, ctx, _describe)
    assert result["effective"] is None
    assert result["applicable_to_some_objects"][0]["name"] == "ViaConnect"


def test_matched_lengths_collects_every_match(ctx):
    rules = [
        _rule("MatchedLengths", 1, "InNetClass('Clocks')", name="A"),
        _rule("MatchedLengths", 2, "All", name="B"),
    ]
    result = resolve_kind(rules, CLK_A, ctx, _describe)
    assert [rule["name"] for rule in result["applicable"]] == ["A", "B"]


def test_binary_rule_lists_candidates_by_priority(ctx):
    rules = [
        _rule("Clearance", 3, "All", "All", name="Default"),
        _rule("Clearance", 1, "InNet('GND')", "InNet('3V3')", name="Unrelated"),
        _rule("Clearance", 2, "All", "InNetClass('Clocks')", name="ToClocks"),
    ]
    result = resolve_kind(rules, CLK_A, ctx, _describe)
    names = [candidate["name"] for candidate in result["candidates"]]
    assert names == ["ToClocks", "Default"]
    # CLK_A is in Clocks, so ToClocks covers CLK_A against anything.
    assert result["candidates"][0]["orientations"] == [
        {"target_side": "scope2", "other_side": "All"}
    ]
    # GND is not: ToClocks covers GND only against clock objects.
    gnd = resolve_kind(rules, GND, ctx, _describe)
    to_clocks = next(c for c in gnd["candidates"] if c["name"] == "ToClocks")
    assert to_clocks["orientations"] == [
        {"target_side": "scope1", "other_side": "InNetClass('Clocks')"}
    ]


def test_binary_rule_reports_both_orientations(ctx):
    rules = [_rule("Clearance", 1, "IsVia", "All", name="ViaClr")]
    result = resolve_kind(rules, CLK_A, ctx, _describe)
    assert result["candidates"][0]["orientations"] == [
        {"target_side": "scope1", "other_side": "All", "conditions": ["IsVia"]},
        {"target_side": "scope2", "other_side": "IsVia"},
    ]


def test_binary_rule_keeps_conditions(ctx):
    rules = [_rule("Clearance", 1, "WithinRoom('RF')", "InNetClass('Power')", name="RF")]
    candidate = resolve_kind(rules, CLK_A, ctx, _describe)["candidates"][0]
    assert candidate["orientations"] == [
        {"target_side": "scope1", "other_side": "InNetClass('Power')",
         "conditions": ["WithinRoom('RF')"]}
    ]


def test_binary_connective_checking(ctx):
    rule = _rule("Clearance", 1, "All", "All", name="SameNet")
    rule.net_scope = "SameNet"
    candidate = resolve_kind([rule], CLK_A, ctx, _describe)["candidates"][0]
    assert candidate["connective_checking"] == "same net only"
    assert candidate["orientations"][0]["conditions"] == [
        "only between objects of the same net"
    ]


def test_binary_pour_copper_view(ctx):
    target = _net("CLK_A", components={"u3"})
    target.has_pours = True
    rules = [
        _rule("Clearance", 1, "InPolygon and InNetClass('Clocks')", "All", name="PourClr"),
        _rule("Clearance", 2, "All", "All", name="Default"),
    ]
    result = resolve_kind(rules, target, ctx, _describe)
    pour = result["candidates"][0]
    assert pour["name"] == "PourClr"
    assert pour["orientations"][0]["conditions"] == ["only the net's polygon pour copper"]
    # Without pours only the other orientation remains: this net's routing
    # against the clock nets' pour copper.
    plain = resolve_kind(rules, _net("CLK_A"), ctx, _describe)
    assert plain["candidates"][0]["orientations"] == [
        {"target_side": "scope2", "other_side": "InPolygon and InNetClass('Clocks')"}
    ]


def test_unary_stops_at_first_match(ctx):
    rules = [
        _rule("Width", 2, "InNetClass('Clocks')", name="Clocks"),
        _rule("Width", 3, "IsVia", name="ViaOnly"),
    ]
    result = resolve_kind(rules, CLK_A, ctx, _describe)
    assert result["effective"]["name"] == "Clocks"
    assert "overridden_for_some_objects_by" not in result


def test_or_with_false_and_unknown_is_unknown(ctx):
    assert _matches("InNet('GND') or IsVia", CLK_A, ctx) == "conditional"
    assert _matches("IsVia or InNet('GND')", CLK_A, ctx) == "conditional"


@pytest.mark.parametrize("scope, expected", [
    # && and || share the loosest level and read left to right.
    ("InNet('CLK_A') || InNet('GND') && IsVia", "conditional"),
    ("InNet('GND') && IsVia || InNet('CLK_A')", "yes"),
    ("InNet('X') || InNet('Y') && InNet('CLK_A')", "no"),
    # And binds tighter than Or.
    ("InNet('CLK_A') or InNet('X') and InNet('Y')", "yes"),
])
def test_operator_precedence(ctx, scope, expected):
    assert _matches(scope, CLK_A, ctx) == expected


@pytest.mark.parametrize("scope, expected", [
    ("InNetClass('Clocks')", "yes"),
    ("InNetClass('Power')", "no"),
    ("InNetClass('All Nets')", "yes"),
    ("InNet('CLK_A') or InNet('CLK_B')", "yes"),
    ("InNet('CLK_*')", "yes"),
    ("InAnyNet", "yes"),
    ("IsDifferentialPair And (Name = 'CLK')", "yes"),
    ("IsDifferentialPair And (Name = 'USB')", "no"),
    ("Name <> 'CLK'", "no"),
    ("Name Like 'CL*'", "yes"),
])
def test_pair_scopes_decided_on_member_nets_and_name(ctx, scope, expected):
    assert _matches(scope, PAIR, ctx) == expected


def test_net_class_rule_wins_for_pair(ctx):
    rules = [
        _rule("DiffPairsRouting", 1, "InNetClass('Clocks')", name="DP_Clocks"),
        _rule("DiffPairsRouting", 2, "All", name="DP_All"),
    ]
    result = resolve_kind(rules, PAIR, ctx, _describe)
    assert result["effective"]["name"] == "DP_Clocks"


# ---------- object-level and xSignal scopes ----------


@pytest.fixture
def obj_ctx(ctx):
    ctx.layer_ids = {"top layer": 1, "1_top": 1, "l3-sig": 3, "bottom layer": 32,
                     "multi-layer": MULTI_LAYER}
    ctx.rooms = {"rf"}
    ctx.room_shapes = {"rf": [(0, 0), (100, 0), (100, 100), (0, 100)]}
    ctx.xsignals = {"clk_pp1": {"clk_a"}, "data_pp1": {"gnd"}}
    ctx.xsignal_classes = {"clocks_x": {"clk_pp1"}}
    ctx.superclasses["xsignal"] = {"all xsignals"}
    return ctx


def _on(target, **facts):
    return replace(target, obj=ObjectFacts(**facts))


TRACK_TOP_IN_ROOM = dict(kind="track", layer=1, span=frozenset({1}),
                         points=((10, 10), (20, 10)))
TRACK_L3_OUTSIDE = dict(kind="track", layer=3, span=frozenset({3}),
                        points=((500, 500), (600, 500)))
VIA = dict(kind="via", layer=MULTI_LAYER, span=frozenset({1, 3, 32}),
           points=((50, 50),), hole=8.0)
PAD_U3 = dict(kind="pad", layer=1, span=frozenset({1}), points=((300, 300),),
              component="u3", footprint="QFN32", pad="u3-1")


@pytest.mark.parametrize("scope, facts, expected", [
    ("IsVia", VIA, "yes"),
    ("IsVia", TRACK_TOP_IN_ROOM, "no"),
    ("IsTrack and OnLayer('Top Layer')", TRACK_TOP_IN_ROOM, "yes"),
    ("OnLayer('1_Top')", TRACK_TOP_IN_ROOM, "yes"),
    ("OnLayer('Top Layer')", VIA, "no"),
    ("ExistsOnLayer('L3-SIG')", VIA, "yes"),
    ("OnMultiLayer", VIA, "yes"),
    ("OnTopLayer", TRACK_L3_OUTSIDE, "no"),
    ("OnMid", TRACK_L3_OUTSIDE, "yes"),
    ("OnSignal", VIA, "yes"),
    ("WithinRoom('RF')", TRACK_TOP_IN_ROOM, "yes"),
    ("WithinRoom('RF')", TRACK_L3_OUTSIDE, "no"),
    ("WithinRoom('RF')", VIA, "yes"),
    ("WithinRoom('Missing')", VIA, "no"),
    ("InRegion(0, 0, 100, 100)", VIA, "yes"),
    ("InComponent('U3')", PAD_U3, "yes"),
    ("InComponent('U3')", TRACK_TOP_IN_ROOM, "no"),
    ("HasFootprint('QFN*')", PAD_U3, "yes"),
    ("HasPad('U3-1')", PAD_U3, "yes"),
    ("InPadClass('TestPads')", PAD_U3, "yes"),
    ("InComponentClass('Ctrl')", PAD_U3, "yes"),
    ("IsSMTPin", PAD_U3, "yes"),
    ("IsThruPad", VIA, "no"),
    ("(ObjectKind = 'Pad') And (Name Like 'U3-*')", PAD_U3, "yes"),
    ("(ObjectKind = 'Pad') And (Name Like 'MH*')", PAD_U3, "no"),
    ("ObjectKind = 'Via'", VIA, "yes"),
    ("InNetClass('Clocks') and Not IsVia", TRACK_TOP_IN_ROOM, "yes"),
    ("InNetClass('Clocks') and Not IsVia", VIA, "no"),
    ("OnLayer('No Such Layer')", TRACK_TOP_IN_ROOM, "conditional"),
    # A hatched pour's track is in its polygon; routing is not.
    ("InPolygon", dict(TRACK_TOP_IN_ROOM, in_pour=True), "yes"),
    ("InNetClass('Clocks') and Not InPolygon", dict(TRACK_TOP_IN_ROOM, in_pour=True), "no"),
    ("InNetClass('Clocks') and Not InPolygon", TRACK_TOP_IN_ROOM, "yes"),
    # IsPolygon and IsNamedPolygon return the polygon object, not its copper.
    ("IsPolygon", dict(TRACK_TOP_IN_ROOM, in_pour=True), "no"),
    ("IsNamedPolygon('GND')", dict(TRACK_TOP_IN_ROOM, in_pour=True), "no"),
    ("IsPolygon", TRACK_TOP_IN_ROOM, "no"),
    # InNamedPolygon returns the copper of the pours it names.
    ("InNamedPolygon('GND')", dict(TRACK_TOP_IN_ROOM, in_pour=True, polygon="gnd"), "yes"),
    ("InNamedPolygon('VCC', 'G*')", dict(TRACK_TOP_IN_ROOM, in_pour=True, polygon="gnd"),
     "yes"),
    ("InNamedPolygon('VCC')", dict(TRACK_TOP_IN_ROOM, in_pour=True, polygon="gnd"), "no"),
    ("InNamedPolygon('GND')", dict(TRACK_TOP_IN_ROOM, in_pour=True), "conditional"),
    ("InNamedPolygon('GND')", TRACK_TOP_IN_ROOM, "no"),
])
def test_object_scopes_are_decided(obj_ctx, scope, facts, expected):
    assert _matches(scope, _on(CLK_A, **facts), obj_ctx) == expected


POUR_GND_TOP = dict(kind="polygon", layer=1, span=frozenset({1}),
                    points=((0, 0), (100, 0), (100, 100)), polygon="gnd_top")


@pytest.mark.parametrize("scope, facts, expected", [
    ("IsNamedPolygon('GND_TOP')", POUR_GND_TOP, "yes"),
    ("IsNamedPolygon('gnd_top')", POUR_GND_TOP, "yes"),
    ("IsNamedPolygon('GND_BOTTOM', 'GND_T*')", POUR_GND_TOP, "yes"),
    ("IsNamedPolygon('GND_BOTTOM')", POUR_GND_TOP, "no"),
    ("Not IsNamedPolygon('GND_BOTTOM')", POUR_GND_TOP, "yes"),
    # InNamedPolygon returns the parent pour too.
    ("InNamedPolygon('GND_TOP')", POUR_GND_TOP, "yes"),
    ("InNamedPolygon('VCC')", POUR_GND_TOP, "no"),
    ("Name = 'GND_TOP'", POUR_GND_TOP, "yes"),
    ("IsPolygon And (Name Like 'GND*')", POUR_GND_TOP, "yes"),
    ("Name Like 'VCC*'", POUR_GND_TOP, "no"),
    ("Name <> 'GND_TOP'", POUR_GND_TOP, "no"),
    ("Name <> 'VCC'", POUR_GND_TOP, "yes"),
    ("Name = 'gnd_top'", POUR_GND_TOP, "yes"),
    # = and <> compare the whole name: a * may be literal.
    ("Name = 'GND*'", POUR_GND_TOP, "conditional"),
    ("Name <> 'GND*'", POUR_GND_TOP, "conditional"),
    ("Name = 'VCC*'", POUR_GND_TOP, "no"),
    ("Name = 'GND*'", dict(POUR_GND_TOP, polygon="gnd*"), "yes"),
    ("Name > 'A'", POUR_GND_TOP, "conditional"),
    ("InPolygonClass('All Polygons')", POUR_GND_TOP, "yes"),
    ("InPolygonClass('TopPours')", POUR_GND_TOP, "yes"),
    ("InPolygonClass('Top*')", POUR_GND_TOP, "yes"),
    ("InPolygonClass('BottomPours')", POUR_GND_TOP, "no"),
    ("InPolygonClass('Missing')", POUR_GND_TOP, "no"),
    # A pour without NAME: only the superclass is known.
    ("IsNamedPolygon('GND_TOP')", dict(POUR_GND_TOP, polygon=None), "conditional"),
    ("InNamedPolygon('GND_TOP')", dict(POUR_GND_TOP, polygon=None), "conditional"),
    ("Name = 'GND_TOP'", dict(POUR_GND_TOP, polygon=None), "conditional"),
    ("InPolygonClass('TopPours')", dict(POUR_GND_TOP, polygon=None), "conditional"),
    ("InPolygonClass('All Polygons')", dict(POUR_GND_TOP, polygon=None), "yes"),
    # The pour terms select nothing else.
    ("IsNamedPolygon('GND_TOP')", VIA, "no"),
    ("InPolygonClass('All Polygons')", VIA, "no"),
])
def test_pour_name_and_class_scopes_on_a_pour(obj_ctx, scope, facts, expected):
    obj_ctx.polygon_classes = {"toppours": {"gnd_top", "vcc_top"}, "bottompours": {"gnd_bot"}}
    obj_ctx.superclasses["polygon"] = {"all polygons"}
    assert _matches(scope, _on(CLK_A, **facts), obj_ctx) == expected


@pytest.mark.parametrize("name, expected", [
    ("GND_Top", "gnd_top"),
    ("Masse øst", "masse øst"),  # Latin-1, as scope text is read
    ("Pour €", None),  # no scope can hold it
    ("G\U0001F600", None),
    (None, None),
])
def test_scope_name(name, expected):
    assert scope_name(name) == expected


@pytest.mark.parametrize("pour_names, expected", [
    ({"gnd_top"}, "yes"),
    ({"gnd_bottom"}, "no"),
    ({"gnd_top", "gnd_bottom"}, "conditional"),  # only GND_TOP's copper
    ({"gnd_top", None}, "conditional"),  # a pour without NAME
    (set(), "conditional"),  # names not given
])
def test_named_pour_on_the_nets_pour_copper(obj_ctx, pour_names, expected):
    target = replace(CLK_A, has_pours=True, pour_names=pour_names, pour_view=True)
    assert _matches("InNamedPolygon('GND_TOP')", target, obj_ctx) == expected


def test_binary_rule_naming_another_pour_is_ruled_out(ctx):
    target = _net("CLK_A", components={"u3"})
    target.has_pours, target.pour_names = True, {"clk_pour"}
    rules = [
        _rule("Clearance", 1, "InNamedPolygon('GND_TOP')", "All", name="GndPour"),
        _rule("Clearance", 2, "InNamedPolygon('CLK*')", "All", name="ClkPour"),
    ]
    result = resolve_kind(rules, target, ctx, _describe)
    by_name = {candidate["name"]: candidate for candidate in result["candidates"]}
    # GndPour reaches CLK_A only as the other side (scope2 All).
    assert by_name["GndPour"]["orientations"] == [
        {"target_side": "scope2", "other_side": "InNamedPolygon('GND_TOP')"}
    ]
    assert by_name["ClkPour"]["orientations"][0] == {
        "target_side": "scope1", "other_side": "All",
        "conditions": ["only the net's polygon pour copper"],
    }


FREE_TH_PAD = dict(kind="pad", layer=MULTI_LAYER, span=frozenset({1, 3, 32}),
                   points=((700, 700),), hole=102.36)
FREE_SMD_PAD = dict(kind="pad", layer=1, span=frozenset({1}), points=((710, 700),))
TH_PIN_U3 = dict(kind="pad", layer=MULTI_LAYER, span=frozenset({1, 3, 32}),
                 points=((300, 300),), component="u3", pad="u3-2", hole=40.0)


@pytest.mark.parametrize("scope, facts, expected", [
    # Altium: IsThruPin = pad with a hole, IsSMTPin = pad without, whether or
    # not the pad belongs to a component (mounting holes, test points).
    ("IsThruPin", FREE_TH_PAD, "yes"),
    ("IsThruPin", TH_PIN_U3, "yes"),
    ("IsThruPin", FREE_SMD_PAD, "no"),
    ("IsThruPin", VIA, "no"),
    ("IsSMTPin", FREE_SMD_PAD, "yes"),
    ("IsSMTPin", FREE_TH_PAD, "no"),
    ("IsSMTPin", TRACK_TOP_IN_ROOM, "no"),
    # IsFree: primitives that are not part of a component.
    ("IsFree", TRACK_TOP_IN_ROOM, "yes"),
    ("IsFree", FREE_TH_PAD, "yes"),
    ("IsFree", PAD_U3, "no"),
])
def test_pad_type_and_free_scopes_per_object(obj_ctx, scope, facts, expected):
    assert _matches(scope, _on(CLK_A, **facts), obj_ctx) == expected


def test_is_free_is_decided_per_object_for_a_net(obj_ctx):
    # A net's routing is free, its component pads are not.
    assert _matches("IsFree", CLK_A, obj_ctx) == "conditional"
    assert _matches("IsFree", PAIR, obj_ctx) == "no"
    assert _matches("IsFree and IsKeepOut", CLK_A, obj_ctx) == "no"


NO_NET = RuleTarget(kind="net", name="", nets=set(), pairs=set())


@pytest.mark.parametrize("scope, expected", [
    ("InAnyNet", "no"),
    ("Not InAnyNet", "yes"),
    ("IsNet", "no"),
    ("ObjectKind = 'Net'", "no"),
    ("InNet('GND')", "no"),
    ("All", "yes"),
])
def test_target_without_nets_is_in_no_net(obj_ctx, scope, expected):
    assert _matches(scope, NO_NET, obj_ctx) == expected
    assert _matches(scope, _on(NO_NET, **VIA), obj_ctx) == expected


def test_real_nets_stay_in_any_net(obj_ctx):
    assert _matches("InAnyNet", CLK_A, obj_ctx) == "yes"
    assert _matches("Not InAnyNet", CLK_A, obj_ctx) == "no"
    assert _matches("IsNet", CLK_A, obj_ctx) == "yes"
    assert _matches("InAnyNet", PAIR, obj_ctx) == "yes"


def test_object_resolver_decides_no_net_objects_per_object(obj_ctx):
    rules = [
        _rule("RoutingVias", 1, "IsVia and InAnyComponent", name="FootprintVias"),
        _rule("RoutingVias", 2, "InAnyNet", name="NetVias"),
        _rule("RoutingVias", 3, "Not InAnyNet", name="NoNetVias"),
        _rule("RoutingVias", 4, "All", name="Default"),
    ]
    resolver = ObjectResolver(rules, NO_NET, obj_ctx)
    in_footprint, _ = resolver.resolve(
        ObjectFacts(**dict(VIA, component="u3", footprint="QFN32"))
    )
    free, undecided = resolver.resolve(ObjectFacts(**VIA))
    assert in_footprint.name == "FootprintVias"
    assert free.name == "NoNetVias" and undecided == []
    # A real net never takes the no-net rule.
    net_via, _ = ObjectResolver(rules, CLK_A, obj_ctx).resolve(ObjectFacts(**VIA))
    assert net_via.name == "NetVias"


def test_xsignal_scopes(obj_ctx):
    xsignal = RuleTarget(kind="xsignal", name="CLK_PP1", nets={"clk_a"}, pairs=set())
    assert _matches("InxSignalClass('Clocks_X')", xsignal, obj_ctx) == "yes"
    assert _matches("InxSignalClass('All xSignals')", xsignal, obj_ctx) == "yes"
    assert _matches("InxSignal('DATA_*')", xsignal, obj_ctx) == "no"
    assert _matches("IsxSignal", xsignal, obj_ctx) == "yes"
    assert _matches("IsxSignal", CLK_A, obj_ctx) == "no"
    # A net only carries part of an xSignal's copper.
    assert _matches("InxSignalClass('Clocks_X')", CLK_A, obj_ctx) == "conditional"
    assert _matches("InxSignalClass('Clocks_X')", GND, obj_ctx) == "no"
    assert _matches("InAnyxSignal", GND, obj_ctx) == "conditional"
    assert stale_references("InxSignalClass('Gone') or InxSignal('CLK_PP1')", obj_ctx) == [
        "inxsignalclass('Gone')"
    ]


def test_object_resolver_refines_only_conditional_rules(obj_ctx):
    rules = [
        _rule("Width", 1, "WithinRoom('RF')", name="InRoom"),
        _rule("Width", 2, "InNetClass('Clocks') and OnLayer('L3-SIG')", name="ClkL3"),
        _rule("Width", 3, "InNet('GND')", name="Gnd"),
        _rule("Width", 4, "All", name="Default"),
    ]
    resolver = ObjectResolver(rules, CLK_A, obj_ctx)
    in_room, _ = resolver.resolve(ObjectFacts(**TRACK_TOP_IN_ROOM))
    on_l3, _ = resolver.resolve(ObjectFacts(**TRACK_L3_OUTSIDE))
    assert (in_room.name, on_l3.name) == ("InRoom", "ClkL3")
    elsewhere, undecided = resolver.resolve(
        ObjectFacts(kind="track", layer=32, span=frozenset({32}),
                    points=((900, 900), (950, 900)))
    )
    assert elsewhere.name == "Default" and undecided == []


def test_object_resolver_reports_undecided_rules(obj_ctx):
    rules = [
        _rule("Width", 1, "InxSignalClass('Clocks_X')", name="XsigWidth"),
        _rule("Width", 2, "All", name="Default"),
    ]
    rule, undecided = ObjectResolver(rules, CLK_A, obj_ctx).resolve(
        ObjectFacts(**TRACK_TOP_IN_ROOM)
    )
    assert rule.name == "Default"
    assert undecided == [{"rule": "XsigWidth", "conditions": ["InxSignalClass('Clocks_X')"]}]


def test_matched_length_values_are_named():
    values = rule_values(
        _rule("MatchedLengths", TOLERANCE="3.937mil", CHECKNETSINDIFFPAIR="TRUE",
              CHECKDIFFPAIRVSDIFFPAIR="FALSE", CHECKXSIGNALS="FALSE",
              CHECKOTHERS="FALSE"),
        SIGNAL_LAYERS,
    )
    assert values == {
        "tolerance_mil": 3.937,
        "within_pair": True,
        "group_diff_pairs": False,
        "group_xsignals": False,
        "group_other_nets": False,
    }


def test_rule_values_decode_the_testpoint_rules():
    # The style rule as Altium saves its default (SIDE stays under other); the usage rule's
    # VALID: 0 required (the default rule's), 1 prohibited, 2 don't care; none: required.
    style = _rule(
        "FabricationTestpoint", SIDE="3", TESTPOINTUNDERCOMPONENT="TRUE", MINSIZE="40mil",
        MAXSIZE="100mil", PREFEREDSIZE="60mil", MINHOLESIZE="0mil", MAXHOLESIZE="40mil",
        PREFEREDHOLESIZE="32mil", TESTPOINTGRID="1mil", ALLOWSIDETOP="TRUE",
        ALLOWSIDEBOTTOM="FALSE", USEGRID="TRUE", GRIDTOLERANCE="0.01mil",
    )
    assert rule_values(style, SIGNAL_LAYERS) == {
        "allow_under_component": True, "min_size_mil": 40.0, "max_size_mil": 100.0,
        "preferred_size_mil": 60.0, "min_hole_mil": 0.0, "max_hole_mil": 40.0,
        "preferred_hole_mil": 32.0, "grid_mil": 1.0, "allow_top": True,
        "allow_bottom": False, "use_grid": True, "grid_tolerance_mil": 0.01,
        "other": {"side": 3},
    }
    assert rule_values(_rule("AssemblyTestpoint", MINSIZE="1mm"), {}) == {}  # not mil
    for valid, usage in (("0", "required"), ("1", "prohibited"), ("2", "dont_care"),
                         ("7", "unknown (7)")):
        rule = _rule("FabricationTestPointUsage", VALID=valid, ALLOWMULTIPLE="FALSE")
        assert rule_values(rule, {}) == {"usage": usage, "allow_multiple": False}
    assert rule_values(_rule("AssemblyTestPointUsage"), {}) == {"usage": "required"}
    assert rule_values(_rule("TestPointUsage", VALID="2"), {})["usage"] == "dont_care"
