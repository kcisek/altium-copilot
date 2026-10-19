"""MCP tools for rule compliance, lengths, diff-pair checks and the constraints review."""

import asyncio
import json

import pytest

import server.main as main_mod
from tests.test_pcb_index import NETLIST, pcb_data  # noqa: F401
from tests.test_pcb_rules_tools import _rule, context_pcb, rules_pcb  # noqa: F401


def test_check_rule_compliance_board_and_net(rules_pcb):
    board = json.loads(main_mod.check_rule_compliance())
    assert board["nets"] == "all"
    kinds = {entry["kind"] for entry in board["kinds"]}
    assert "Width" in kinds
    assert any(item["kind"] == "RoutingCorners" for item in board["not_checked"])
    one = json.loads(main_mod.check_rule_compliance(net="CLK", kinds=["DiffPairsRouting"]))
    assert one["expanded_pairs"]
    assert {entry["kind"] for entry in one["kinds"]} == {"DiffPairsRouting"}


def test_check_rule_compliance_errors_and_units(rules_pcb):
    assert json.loads(main_mod.check_rule_compliance(net="NOPE"))["error"] == "net_not_found"
    assert json.loads(main_mod.check_rule_compliance(kinds=["Bogus"]))["error"] == "invalid_kind"
    text = main_mod.check_rule_compliance(units="mm")
    assert "_mil" not in text


def test_check_lengths_tool(rules_pcb):
    empty = json.loads(main_mod.check_lengths())
    assert empty["rule_count"] == 0
    rules_pcb.pcb.rules.append(
        _rule("Length", 1, "InNet('CLK_A')", name="LEN_CLK",
              MINLIMIT="100mil", MAXLIMIT="150mil")
    )
    result = json.loads(main_mod.check_lengths(rule="LEN_CLK"))
    rule = result["rules"][0]
    assert rule["name"] == "LEN_CLK"
    assert rule["mode"] == "range"
    assert json.loads(main_mod.check_lengths(rule="Nope"))["error"] == "rule_not_found"


def test_get_signal_length_tool(rules_pcb):
    net = json.loads(main_mod.get_signal_length("CLK_A"))
    assert net["net"] == "CLK_A" and "length_mil" in net
    pair = json.loads(main_mod.get_signal_length("clk"))
    assert pair["pair"] == "CLK"
    assert "skew_mil" in pair
    in_mm = json.loads(main_mod.get_signal_length("CLK_A", units="mm"))
    assert "length_mm" in in_mm
    missing = json.loads(main_mod.get_signal_length("NOPE"))
    assert missing["error"] == "net_not_found"
    assert "xSignal" in missing["message"]


def test_check_diff_pairs_tool(rules_pcb):
    result = json.loads(main_mod.check_diff_pairs())
    assert result["pairs_total"] == 1
    assert set(result["checks_run"]) == {"naming", "polarity", "series", "skew"}
    assert any(item["check"] == "directives" for item in result["not_checked"])
    bad = json.loads(main_mod.check_diff_pairs(checks=["bogus"]))
    assert bad["error"] == "invalid_checks"


def test_list_diff_pairs_reports_path_and_routed_lengths(rules_pcb):
    pair = json.loads(main_mod.list_diff_pairs())["diff_pairs"][0]
    assert set(pair["length_mil"]) == {"positive", "negative"}
    assert pair["routed_length_mil"] == {"positive": 200.0, "negative": 100.0}
    assert "length_skew_mil" in pair


def test_constraints_review_tool(rules_pcb):
    text = main_mod.constraints_review()
    assert "Project: Demo" in text
    assert "Board.PcbDoc" in text
    assert "Phase 4" in text
    for tool in ("check_rule_compliance", "check_lengths", "check_diff_pairs"):
        assert tool in text


def test_constraints_review_guards(rules_pcb, monkeypatch):
    project = dict(main_mod._project)
    project["pcb_doc_paths"] = []
    monkeypatch.setattr(main_mod, "_project", project)
    main_mod._pcb_session._index = None
    result = json.loads(main_mod.constraints_review())
    assert result["error"] == "no_pcb_document"
    assert "schematic_review" in result["hint"]
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.constraints_review())["error"] == "no_project"


@pytest.mark.parametrize("tool_call", [
    lambda: main_mod.check_rule_compliance(units="inch"),
    lambda: main_mod.check_lengths(units="inch"),
    lambda: main_mod.get_signal_length("CLK_A", units="inch"),
    lambda: main_mod.check_diff_pairs(units="inch"),
])
def test_new_tools_reject_invalid_units(rules_pcb, tool_call):
    assert json.loads(tool_call())["error"] == "invalid_units"


def test_new_tools_without_project(monkeypatch):
    monkeypatch.setattr(main_mod, "_project", None)
    for call in (
        main_mod.check_rule_compliance,
        main_mod.check_lengths,
        lambda: main_mod.get_signal_length("X"),
        main_mod.check_diff_pairs,
    ):
        assert json.loads(call())["error"] == "no_project"


def test_check_lengths_trims_members_to_fit(rules_pcb, monkeypatch):
    rules_pcb.pcb.rules.append(
        _rule("MatchedLengths", 1, "InNetClass('Clocks')", name="ML_CLK",
              TOLERANCE="1mil", CHECKNETSINDIFFPAIR="FALSE",
              CHECKDIFFPAIRVSDIFFPAIR="TRUE", CHECKXSIGNALS="TRUE", CHECKOTHERS="TRUE")
    )
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 400)
    result = json.loads(main_mod.check_lengths())
    assert result["members_per_rule_limited_to"] == 1
    assert "check_lengths(rule=" in result["hint"]


def _fake_compliance(seen: dict, total: int = 120):
    def fake(index, nets, kinds, length_of=None, clearance=False, limit=50, offset=0):
        seen.update(limit=limit, offset=offset, kinds=kinds)
        groups = [
            {"net": f"N{i}", "layer": "Top Layer", "count": 1, "margin_mil": 1.0,
             "example": {"x_mil": 1.0, "y_mil": 2.0}}
            for i in range(total)
        ]
        page = groups[offset:offset + limit]
        entry = {
            "kind": "Width", "violation_count": total, "violation_groups": total,
            "violations": page, "has_more": offset + limit < total,
        }
        if entry["has_more"]:
            entry["next_offset"] = offset + limit
        return {"nets": "all", "violation_count": total, "kinds": [entry],
                "not_checked": [], "approximations": []}
    return fake


def test_check_rule_compliance_pages_with_offset(rules_pcb, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(main_mod, "check_compliance", _fake_compliance(seen))
    result = json.loads(main_mod.check_rule_compliance(kinds=["Width"], offset=60))
    assert seen == {"limit": 50, "offset": 60, "kinds": ["Width"]}
    width = result["kinds"][0]
    assert [group["net"] for group in width["violations"]][:2] == ["N60", "N61"]
    assert len(width["violations"]) == 50
    assert width["next_offset"] == 110
    assert "violations_per_kind_limited_to" not in result
    json.loads(main_mod.check_rule_compliance(offset=-5))
    assert seen["offset"] == 0


def test_check_rule_compliance_trims_groups_to_fit(rules_pcb, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(main_mod, "check_compliance", _fake_compliance(seen))
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 2500)
    text = main_mod.check_rule_compliance(kinds=["Width"], offset=10)
    assert len(text) <= 2500
    result = json.loads(text)
    width = result["kinds"][0]
    shown = len(width["violations"])
    assert result["violations_per_kind_limited_to"] == shown < 50
    assert width["violations"][0]["net"] == "N10"
    # Paging stays consistent: the next call starts after the last shown group.
    assert width["has_more"] is True and width["next_offset"] == 10 + shown
    assert width["violation_count"] == 120
    assert "offset=" in result["hint"]


def test_check_rule_compliance_exposes_offset_parameter():
    tools = {tool.name: tool for tool in asyncio.run(main_mod.mcp.list_tools())}
    schema = tools["check_rule_compliance"].inputSchema["properties"]
    assert "offset" in schema
    assert "next_offset" in tools["check_rule_compliance"].description


def test_check_rule_compliance_real_offset(rules_pcb):
    first = json.loads(main_mod.check_rule_compliance())
    later = json.loads(main_mod.check_rule_compliance(offset=1000))
    for entry in later["kinds"]:
        assert entry.get("violations", []) == []
    assert later["violation_count"] == first["violation_count"]


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_length_wording_includes_pin_package_length(rules_pcb):
    # The length engine adds the end pads' pin/package length (Altium Signal
    # Length); nothing may say it is excluded or bound its effect to a few mil.
    instructions = _flat(main_mod.SERVER_INSTRUCTIONS)
    assert "plus the pin/package length stored on the end pads" in instructions
    assert "like Altium's Routed Length" in instructions
    assert "group length is the P/N average" in instructions
    tools = {tool.name: tool for tool in asyncio.run(main_mod.mcp.list_tools())}
    pair_note = json.loads(main_mod.list_diff_pairs())["note"]
    assert "pin/package length" in pair_note
    texts = [instructions, _flat(pair_note)]
    for name in ("get_signal_length", "check_lengths"):
        description = _flat(tools[name].description)
        assert "pin/package length" in description.lower()
        texts.append(description)
    for text in texts:
        assert "few mil" not in text
        assert "pin/package length is not" not in text.lower()
        assert "length is not included" not in text.lower()
