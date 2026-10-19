"""MCP tools for rule compliance, lengths, diff-pair checks and the constraints review."""

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
