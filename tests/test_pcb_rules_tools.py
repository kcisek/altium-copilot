"""MCP tools for design rules, classes and differential pairs."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

import server.main as main_mod
from server.parsers.pcb_doc import DiffPair, PcbClass, PcbRule
from server.parsers.prj_pcb import VariantDefinition, VariantState
from server.services.pcb_index import PcbIndex
from tests.test_pcb_index import NETLIST, pcb_data  # noqa: F401


def _rule(kind, priority, scope1="All", scope2="All", name=None, enabled=True, **props):
    return PcbRule(
        name=name or kind,
        kind=kind,
        kind_id=0,
        enabled=enabled,
        priority=priority,
        scope1=scope1,
        scope2=scope2,
        net_scope="DifferentNets" if kind == "Clearance" else "AnyNet",
        comment="",
        unique_id=None,
        from_schematic=False,
        properties=props,
    )


def _class(name, kind, members=(), superclass=False, auto_generated=False):
    return PcbClass(
        name=name,
        kind=kind,
        members=list(members),
        superclass=superclass,
        auto_generated=auto_generated,
        unique_id=None,
    )


@pytest.fixture
def rules_pcb(pcb_data, monkeypatch, tmp_path: Path):
    data = deepcopy(pcb_data)
    data.diff_pairs = [DiffPair("CLK", "CLK_A", "CLK_B", "PAIRUID")]
    data.classes = [
        _class("All Nets", "net", superclass=True),
        _class("Clocks", "net", ["CLK_A", "CLK_B", "MISSING_NET"]),
        _class("All Differential Pairs", "diff_pair", superclass=True),
        _class("HS", "diff_pair", ["CLK", "OLD_PAIR"]),
        _class("Ctrl", "component", ["U3", "R1", "X9"]),
        _class("Top Side Components", "component", ["U3"], auto_generated=True),
    ]
    data.rules = [
        _rule("Width", 1, "InNet('CLK_A') and OnLayer('Top Layer')", name="Width_TopClk",
              MINLIMIT="3mil", PREFEREDWIDTH="3mil", MAXLIMIT="3mil"),
        _rule("Width", 2, "InNetClass('Clocks')", name="Width_Clocks",
              MINLIMIT="4mil", PREFEREDWIDTH="5mil", MAXLIMIT="6mil"),
        _rule("Width", 3, "All", name="Width",
              MINLIMIT="5mil", PREFEREDWIDTH="8mil", MAXLIMIT="20mil",
              TOPLAYER_MINWIDTH="6mil", MIDLAYER5_MINWIDTH="4mil"),
        _rule("Width", 4, "InNetClass('Gone')", name="Width_Stale", MINLIMIT="9mil"),
        _rule("Clearance", 1, "InNetClass('Clocks')", "All", name="Clearance_Clk",
              GENERICCLEARANCE="10mil",
              OBJECTCLEARANCES="ClearanceObj_Track-ClearanceObj_Via:50000"),
        _rule("Clearance", 2, name="Clearance", GENERICCLEARANCE="6mil"),
        _rule("DiffPairsRouting", 1, "InDifferentialPairClass('HS')", name="DP_HS",
              MINLIMIT="4mil", MOSTFREQGAP="5mil", MAXLIMIT="6mil",
              TOPLAYER_MINWIDTH="4mil", TOPLAYER_PREFWIDTH="4.5mil",
              TOPLAYER_MAXWIDTH="5mil"),
        _rule("NetAntennae", 1, name="NetAntennae", enabled=False),
        _rule("RoomDefinition", 1, "InComponentClass('Ctrl')", name="RF",
              LAYER="TOP", VX0="0mil", VY0="0mil", VX1="10mil", VY1="0mil",
              VX2="10mil", VY2="10mil"),
    ]
    pcb_file = tmp_path / "Board.PcbDoc"
    pcb_file.write_bytes(b"fake")
    index = PcbIndex(data, NETLIST)
    monkeypatch.setattr(main_mod, "_project", {
        "name": "Demo",
        "root_dir": str(tmp_path),
        "prj_pcb_path": "x",
        "sheets": [],
        "pcb_doc_paths": [str(pcb_file)],
    })
    monkeypatch.setattr(main_mod._altium, "_netlist", NETLIST)
    monkeypatch.setattr(
        main_mod, "_variant_state",
        VariantState([VariantDefinition(name="Prod", dnp_refdes=["R1"])]),
    )
    monkeypatch.setattr(main_mod._pcb_session, "_load", lambda path, netlist: index)
    main_mod._pcb_session._index = None
    return index


def test_design_rules_summary(rules_pcb):
    result = json.loads(main_mod.get_design_rules())
    assert result["rule_count"] == 8
    assert result["total"] == 8
    assert result["rooms"] == 1
    assert result["disabled_rules"] == 1
    assert result["kinds"]["Width"] == 4
    assert all(rule["kind"] != "RoomDefinition" for rule in result["rules"])
    stale = next(rule for rule in result["rules"] if rule["name"] == "Width_Stale")
    assert stale["stale_references"] == ["innetclass('Gone')"]


def test_design_rules_by_kind_is_case_insensitive_and_ordered(rules_pcb):
    result = json.loads(main_mod.get_design_rules(kind="width"))
    assert result["kind"] == "Width"
    assert [rule["priority"] for rule in result["rules"]] == [1, 2, 3, 4]
    default = result["rules"][2]
    layers = default["values"]["layers_mil"]
    assert layers["Top Layer"]["min_width"] == 6.0
    assert layers["Bottom Layer"]["min_width"] == 5.0
    # MIDLAYER5 is not on this 4-layer board.
    assert set(layers) == {"Top Layer", "L2-GND", "L3-SIG", "Bottom Layer"}


def test_design_rules_by_name_and_units(rules_pcb):
    result = json.loads(main_mod.get_design_rules(name="clearance_clk", units="mm"))
    values = result["rules"][0]["values"]
    assert values["clearance_mm"] == pytest.approx(0.254)
    assert values["object_clearances_mm"]["Track-Via"] == pytest.approx(0.127)


def test_design_rules_rooms_by_kind(rules_pcb):
    result = json.loads(main_mod.get_design_rules(kind="RoomDefinition"))
    room = result["rules"][0]
    assert room["values"]["vertex_count"] == 3
    assert room["values"]["side"] == "top"


def test_design_rules_errors(rules_pcb):
    bad_kind = json.loads(main_mod.get_design_rules(kind="Nope"))
    assert bad_kind["error"] == "invalid_rule_kind"
    assert "Width" in bad_kind["available_kinds"]
    missing = json.loads(main_mod.get_design_rules(name="Width_"))
    assert missing["error"] == "rule_not_found"
    assert "Width_Clocks" in missing["suggestions"]


def test_design_rules_pagination(rules_pcb, monkeypatch):
    monkeypatch.setattr(main_mod, "MAX_RULE_RESULTS", 3)
    first = json.loads(main_mod.get_design_rules())
    assert len(first["rules"]) == 3
    assert first["has_more"] is True
    second = json.loads(main_mod.get_design_rules(offset=first["next_offset"]))
    assert second["offset"] == 3
    assert {rule["name"] for rule in first["rules"]}.isdisjoint(
        rule["name"] for rule in second["rules"]
    )


def _by_kind(result):
    return {
        (entry["kind"], entry.get("diff_pair")): entry for entry in result["rules"]
    }


def test_rules_for_net(rules_pcb):
    result = json.loads(main_mod.get_rules_for_net("clk_a"))
    assert result["target"] == "CLK_A"
    assert result["net_classes"] == ["Clocks"]
    assert result["diff_pairs"] == [
        {"name": "CLK", "polarity": "positive", "partner_net": "CLK_B"}
    ]
    rules = _by_kind(result)
    width = rules[("Width", None)]
    assert width["effective"]["name"] == "Width_Clocks"
    assert width["effective"]["values"]["min_width_mil"] == 4.0
    override = width["overridden_for_some_objects_by"][0]
    assert override["name"] == "Width_TopClk"
    assert override["conditions"] == ["OnLayer('Top Layer')"]
    clearance = rules[("Clearance", None)]
    assert [c["name"] for c in clearance["candidates"]] == ["Clearance_Clk", "Clearance"]
    assert clearance["candidates"][0]["orientations"] == [
        {"target_side": "scope1", "other_side": "All"}
    ]
    assert clearance["candidates"][0]["connective_checking"] == "different nets only"
    pair_rule = rules[("DiffPairsRouting", "CLK")]
    assert pair_rule["effective"]["name"] == "DP_HS"
    # A pair net's track width is checked by the diff-pair rule.
    assert "DP_HS" in width["note"]
    antennae = rules[("NetAntennae", None)]
    assert antennae["effective"] is None
    assert antennae["disabled_rules"] == ["NetAntennae"]


def test_rules_for_net_outside_classes(rules_pcb):
    result = json.loads(main_mod.get_rules_for_net("GND", kinds=["Width", "Clearance"]))
    rules = _by_kind(result)
    assert rules[("Width", None)]["effective"]["name"] == "Width"
    assert "overridden_for_some_objects_by" not in rules[("Width", None)]
    assert [c["name"] for c in rules[("Clearance", None)]["candidates"]] == [
        "Clearance_Clk", "Clearance"
    ]
    assert "net_classes" not in result
    assert ("DiffPairsRouting", None) not in rules


def test_rules_for_diff_pair(rules_pcb):
    result = json.loads(main_mod.get_rules_for_net("CLK"))
    assert result["type"] == "diff_pair"
    assert result["positive_net"] == "CLK_A"
    assert result["pair_classes"] == ["HS"]
    assert [entry["kind"] for entry in result["rules"]] == ["DiffPairsRouting"]
    layers = result["rules"][0]["effective"]["values"]["layers_mil"]
    assert layers["Top Layer"]["preferred_gap"] == 5.0
    assert layers["Top Layer"]["preferred_width"] == 4.5


def test_rules_for_net_kind_filter_and_errors(rules_pcb):
    only = json.loads(main_mod.get_rules_for_net("CLK_A", kinds=["clearance"]))
    assert [entry["kind"] for entry in only["rules"]] == ["Clearance"]
    every = json.loads(main_mod.get_rules_for_net("CLK_A", kinds=["all"]))
    assert {entry["kind"] for entry in every["rules"]} >= {"Width", "NetAntennae"}
    bad = json.loads(main_mod.get_rules_for_net("CLK_A", kinds=["Bogus"]))
    assert bad["error"] == "invalid_rule_kind"
    missing = json.loads(main_mod.get_rules_for_net("CLK"[:2]))
    assert missing["error"] == "net_not_found"
    assert "CLK_A" in missing["suggestions"]


def test_classes_listing(rules_pcb):
    result = json.loads(main_mod.get_classes())
    assert result["kinds"] == {"net": 2, "diff_pair": 2, "component": 2}
    all_nets = next(c for c in result["classes"] if c["name"] == "All Nets")
    assert all_nets == {"name": "All Nets", "kind": "net", "superclass": True}
    clocks = next(c for c in result["classes"] if c["name"] == "Clocks")
    assert clocks["member_count"] == 3
    auto = next(c for c in result["classes"] if c["name"] == "Top Side Components")
    assert auto["auto_generated"] is True
    nets_only = json.loads(main_mod.get_classes(kind="net"))
    assert {c["kind"] for c in nets_only["classes"]} == {"net"}


def test_class_members(rules_pcb):
    clocks = json.loads(main_mod.get_classes(name="clocks"))["classes"][0]
    assert clocks["members"] == ["CLK_A", "CLK_B", "MISSING_NET"]
    assert clocks["unresolved_members"] == ["MISSING_NET"]
    ctrl = json.loads(main_mod.get_classes(kind="component", name="Ctrl"))["classes"][0]
    by_refdes = {member["refdes"]: member for member in ctrl["members"]}
    assert by_refdes["R1"]["dnp"] is True
    assert by_refdes["U3"]["dnp"] is False
    assert ctrl["unresolved_members"] == ["X9"]
    hs = json.loads(main_mod.get_classes(name="HS"))["classes"][0]
    assert hs["members"][0] == {
        "pair": "CLK", "positive_net": "CLK_A", "negative_net": "CLK_B",
    }
    assert hs["unresolved_members"] == ["OLD_PAIR"]
    super_class = json.loads(main_mod.get_classes(name="All Nets"))["classes"][0]
    assert super_class["members"].startswith("implicit")


def test_class_errors(rules_pcb):
    assert json.loads(main_mod.get_classes(kind="bogus"))["error"] == "invalid_class_kind"
    missing = json.loads(main_mod.get_classes(name="Clock"))
    assert missing["error"] == "class_not_found"
    assert missing["suggestions"] == ["Clocks"]


def test_list_diff_pairs(rules_pcb):
    result = json.loads(main_mod.list_diff_pairs())
    pair = result["diff_pairs"][0]
    assert pair["name"] == "CLK"
    assert pair["classes"] == ["HS"]
    assert pair["routed_length_mil"] == {"positive": 200.0, "negative": 100.0}
    assert pair["length_skew_mil"] == 100.0
    assert pair["routing_rule"] == "DP_HS"
    in_mm = json.loads(main_mod.list_diff_pairs(units="mm"))
    assert in_mm["diff_pairs"][0]["length_skew_mm"] == pytest.approx(2.54)
    assert json.loads(main_mod.list_diff_pairs("^nope$"))["diff_pairs"] == []
    assert json.loads(main_mod.list_diff_pairs("clk_b"))["total"] == 1


def test_existing_tools_gain_class_and_pair_context(rules_pcb):
    net = json.loads(main_mod.get_net_pcb("CLK_A"))
    assert net["net_classes"] == ["Clocks"]
    assert net["diff_pairs"][0]["partner_net"] == "CLK_B"
    neighbors = json.loads(main_mod.get_net_neighbors("CLK_A"))
    partner = next(n for n in neighbors["neighbors"] if n["net"] == "CLK_B")
    assert partner["diff_pair_partner"] is True
    board = json.loads(main_mod.get_board_info())
    assert board["design_rules"] == {
        "rules": 8,
        "disabled_rules": 1,
        "rooms": 1,
        "net_classes": 1,
        "diff_pairs": 1,
    }


@pytest.mark.parametrize("tool_call", [
    lambda: main_mod.get_design_rules(units="inch"),
    lambda: main_mod.get_rules_for_net("CLK_A", units="inch"),
    lambda: main_mod.list_diff_pairs(units="inch"),
])
def test_rule_tools_reject_invalid_units(rules_pcb, tool_call):
    assert json.loads(tool_call())["error"] == "invalid_units"


def test_rule_tools_without_project(monkeypatch):
    monkeypatch.setattr(main_mod, "_project", None)
    for call in (
        main_mod.get_design_rules,
        lambda: main_mod.get_rules_for_net("X"),
        main_mod.get_classes,
        main_mod.list_diff_pairs,
    ):
        assert json.loads(call())["error"] == "no_project"


def test_stale_references_cover_scope2_of_every_binary_kind(rules_pcb):
    rules_pcb.pcb.rules.append(
        _rule("ShortCircuit", 1, "All", "InNet('NO_SUCH_NET')", name="Shorts")
    )
    listing = json.loads(main_mod.get_design_rules())
    shorts = next(rule for rule in listing["rules"] if rule["name"] == "Shorts")
    assert shorts["stale_references"] == ["innet('NO_SUCH_NET')"]
    detail = json.loads(main_mod.get_design_rules(name="Shorts"))["rules"][0]
    assert detail["stale_references"] == ["innet('NO_SUCH_NET')"]


@pytest.fixture
def context_pcb(pcb_data, monkeypatch, tmp_path: Path):
    """Board whose rules depend on what PcbIndex builds: superclasses, rooms,
    PCB designators that differ from the schematic refdes, pour ownership."""
    data = deepcopy(pcb_data)
    # Board-level annotation: the part placed as R1 shows 'R7' on the board.
    data.components[2].designator = "R7"
    data.components[0].designator = "U3"
    data.components[1].designator = "U4"
    data.diff_pairs = [DiffPair("CLK", "CLK_A", "CLK_B", None)]
    data.classes = [
        _class("All Differential Pairs", "diff_pair", superclass=True),
        _class("Passives", "component", ["R7"]),
        _class("Clock", "net", ["CLK_A"]),
    ]
    data.rules = [
        _rule("DiffPairsRouting", 1, "InDifferentialPairClass('All Differential Pairs')",
              name="DP_ALL", MINLIMIT="4mil", MOSTFREQGAP="5mil", MAXLIMIT="6mil"),
        _rule("Width", 1, "WithinRoom('RF')", name="W_ROOM", MINLIMIT="3mil"),
        _rule("Width", 2, "InComponent('R7')", name="W_R7", MINLIMIT="4mil"),
        _rule("Width", 3, "InComponentClass('Passives')", name="W_PASSIVES", MINLIMIT="4mil"),
        _rule("Width", 4, "All", name="W_ALL", MINLIMIT="5mil"),
        _rule("Clearance", 1, "InPolygon", "All", name="POUR", GENERICCLEARANCE="20mil"),
        _rule("Clearance", 2, name="DEFAULT", GENERICCLEARANCE="6mil"),
        _rule("RoomDefinition", 1, "All", name="RF", VX0="0mil", VY0="0mil"),
    ]
    pcb_file = tmp_path / "Board.PcbDoc"
    pcb_file.write_bytes(b"fake")
    index = PcbIndex(data, NETLIST)
    monkeypatch.setattr(main_mod, "_project", {
        "name": "Demo", "root_dir": str(tmp_path), "prj_pcb_path": "x",
        "sheets": [], "pcb_doc_paths": [str(pcb_file)],
    })
    monkeypatch.setattr(main_mod._altium, "_netlist", NETLIST)
    monkeypatch.setattr(
        main_mod, "_variant_state", VariantState([VariantDefinition(name="Default")])
    )
    monkeypatch.setattr(main_mod._pcb_session, "_load", lambda path, netlist: index)
    main_mod._pcb_session._index = None
    return index


def test_rule_context_through_tools(context_pcb):
    clk_b = json.loads(main_mod.get_rules_for_net("CLK_B"))
    rules = _by_kind(clk_b)
    # The negative leg resolves its pair; the superclass covers every pair.
    assert rules[("DiffPairsRouting", "CLK")]["effective"]["name"] == "DP_ALL"
    width = rules[("Width", None)]
    assert width["effective"]["name"] == "W_ALL"
    assert [r["name"] for r in width["overridden_for_some_objects_by"]] == ["W_ROOM"]
    gnd = _by_kind(json.loads(main_mod.get_rules_for_net("GND")))
    # GND reaches the part whose PCB designator is R7 (schematic R1).
    assert [r["name"] for r in gnd[("Width", None)]["overridden_for_some_objects_by"]] == [
        "W_ROOM", "W_R7", "W_PASSIVES",
    ]
    # GND owns the pour, so the pour-only clearance applies to its pour copper.
    pour = gnd[("Clearance", None)]["candidates"][0]
    assert pour["name"] == "POUR"
    assert "only the net's polygon pour copper" in pour["orientations"][0]["conditions"]
    clk_a = _by_kind(json.loads(main_mod.get_rules_for_net("CLK_A")))
    # CLK_A owns no pour: POUR only covers its routing against pour copper.
    clk_pour = clk_a[("Clearance", None)]["candidates"][0]
    assert clk_pour["orientations"] == [{"target_side": "scope2", "other_side": "InPolygon"}]


def test_component_class_members_resolve_by_pcb_designator(context_pcb):
    passives = json.loads(main_mod.get_classes(name="Passives"))["classes"][0]
    assert passives["members"] == [{"refdes": "R7", "sch_refdes": "R1", "dnp": False}]
    assert "unresolved_members" not in passives


def test_pair_sharing_a_net_name(context_pcb):
    context_pcb.pcb.nets.append("CLK")
    context_pcb._build_rule_context()
    pairs = json.loads(main_mod.list_diff_pairs())
    assert pairs["diff_pairs"][0]["routing_rule"] == "DP_ALL"
    net = json.loads(main_mod.get_rules_for_net("CLK"))
    assert net["type"] == "net"
    assert "also a differential pair" in net["note"]


def test_rules_for_net_trims_candidate_values_when_too_large(rules_pcb, monkeypatch):
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 500)
    result = json.loads(main_mod.get_rules_for_net("CLK_A"))
    clearance = next(entry for entry in result["rules"] if entry["kind"] == "Clearance")
    assert "values" in clearance["candidates"][0]
    assert "values" not in clearance["candidates"][1]
    assert "get_design_rules" in result["values_omitted"]
