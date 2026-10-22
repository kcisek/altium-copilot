"""check_release_readiness gates, verdict and paging, and the release_review prompt.

Gates run against fakes of their sources (the context's cached sources set directly, or
the service function patched in release_gate), the synthetic board of test_pcb_index and
the netlist-free project of test_netlist_free_load; Altium's public Bluetooth Sentinel
example is a snapshot, skipped when it is not installed.
"""

import asyncio
import dataclasses
import itertools
import json
import random
import re
import string
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import server.main as main
from server.parsers.drc_report import DrcReport, DrcTable
from server.parsers.pcb_doc import BareHole, DrcOptions, PcbRule, ShapeVertex
from server.parsers.prj_pcb import VariantDefinition, VariantState
from server.services import release_gate as rg
from server.services.dfm import AssemblyContext
from server.services.pcb_index import PcbIndex
from tests.test_netlist_free_load import _load, env  # noqa: F401
from tests.test_pcb_index import NETLIST, _pad, pcb_data  # noqa: F401

EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")
GATE_IDS = [gate.id for gate in rg.GATES]
ROW_KEYS = {"id", "title", "status", "blocking", "evidence", "source_tool", "ms"}


class FakeNotes:
    """DesignNotesCache stand-in: sheet parameters and Draftsman parameters by path."""

    def __init__(self, sheets=None, drawings=None):
        self.sheets = sheets or {}
        self.drawings = drawings or {}

    def sheet(self, path):
        if path not in self.sheets:
            raise OSError("unreadable")
        return SimpleNamespace(sheet_parameters=self.sheets[path])

    def draftsman(self, path):
        return SimpleNamespace(parameters=self.drawings.get(path, {}))

    def hierarchy(self, path):
        raise OSError("not read in these tests")

    integrity = hierarchy


@pytest.fixture
def project(tmp_path):
    (tmp_path / "Board.PcbDoc").write_bytes(b"pcb")
    return {
        "name": "Board",
        "root_dir": str(tmp_path),
        "prj_pcb_path": str(tmp_path / "Board.PrjPcb"),
        "sheets": [{"name": "Power", "path": "power"}, {"name": "IO", "path": "io"}],
        "pcb_doc_paths": [str(tmp_path / "Board.PcbDoc")],
        "pcb_dwf_paths": [],
        "parameters": {},
    }


def _ctx(project, index=None, **overrides) -> rg.ReleaseContext:
    inputs = dict(
        project=project, netlist=None, variant_state=VariantState([]), notes=FakeNotes(),
        pcb_index=lambda: (index, None) if index is not None else (
            None, {"error": "pcb_parse_failed", "message": "bad file"}),
        saved_after_netlist=lambda p, compiled: ([], []),
        sheets_saved_after=lambda p, saved: [],
        dfm_variant=lambda: AssemblyContext(), fab_board_reader=lambda path: (None, "x"),
        clone_status=lambda folder: {"altium365": False, "reason": "not_a_git_repository"},
    )
    inputs.update(overrides)
    return rg.ReleaseContext(rg.ReleaseInputs(**inputs))


def _gate(gate_id: str) -> rg.Gate:
    return next(gate for gate in rg.GATES if gate.id == gate_id)


def _run(gate_id: str, ctx) -> dict:
    return rg.run_gate(_gate(gate_id), ctx)


def _rows(*statuses, blocking=True):
    return [{"id": f"g{n}", "title": "", "status": status, "blocking": blocking,
             "evidence": "", "source_tool": None, "ms": 0} for n, status in enumerate(statuses)]


# ---------- verdict, gate runner, paging ----------

@pytest.mark.parametrize("blocking, advisory, expected", [
    (["pass", "fail", "not_verified"], [], "fail"),
    (["pass", "not_verified"], [], "not_ready_unverified"),
    (["pass", "warn", "not_applicable"], ["not_verified", "warn"], "pass"),
    (["not_applicable"], [], "pass"),
])
def test_verdict(blocking, advisory, expected):
    rows = _rows(*blocking) + [dict(row, id=f"a{n}") for n, row in
                               enumerate(_rows(*advisory, blocking=False))]
    result = rg.verdict(rows)
    assert result["verdict"] == expected
    assert result["blocking_failed"] == [r["id"] for r in rows
                                         if r["blocking"] and r["status"] == "fail"]
    assert result["blocking_not_verified"] == [
        r["id"] for r in rows if r["blocking"] and r["status"] == "not_verified"]
    assert result["not_verified"] == [r["id"] for r in rows
                                      if not r["blocking"] and r["status"] == "not_verified"]
    assert result["verdict_summary"].startswith(
        {"fail": "FAIL", "not_ready_unverified": "NOT VERIFIED", "pass": "PASS"}[expected])


def test_not_verified_is_never_a_pass():
    for statuses in (["not_verified"], ["pass", "not_verified", "pass"]):
        assert rg.verdict(_rows(*statuses))["verdict"] != "pass"


RAW_STATUSES = (*rg.STATUSES, "maybe", "raise")


def _raw_gate(n: int, blocking: bool, raw: str) -> rg.Gate:
    def run(ctx):
        if raw == "raise":
            raise RuntimeError("broken")
        return raw, "evidence"
    return rg.Gate(f"g{n}", f"G{n}", blocking, None, run)


def test_verdict_property_over_every_combination(project):
    # Every (raw status, blocking) for three gates, through run_gate and verdict: pass
    # only without a blocking fail or not_verified, fail exactly when a blocking gate
    # failed, and no status outside STATUSES reaches the rows.
    ctx = _ctx(project)
    cells = list(itertools.product(RAW_STATUSES, (True, False)))
    for combo in itertools.product(cells, repeat=3):
        rows = [rg.run_gate(_raw_gate(n, blocking, raw), ctx)
                for n, (raw, blocking) in enumerate(combo)]
        result = rg.verdict(rows)
        blocking = [row["status"] for row in rows if row["blocking"]]
        assert all(row["status"] in rg.STATUSES for row in rows)
        assert all(row["status"] != "fail" for row in rows if not row["blocking"])
        assert (result["verdict"] == "fail") == ("fail" in blocking), combo
        if result["verdict"] == "pass":
            assert not {"fail", "not_verified"} & set(blocking), combo
            assert result["verdict_summary"].startswith("PASS")
        else:
            assert {"fail", "not_verified"} & set(blocking), combo
            assert not result["verdict_summary"].startswith("PASS")


def test_verdict_property_on_the_real_gate_table(project):
    # The fixed gate table with random statuses: the same rules hold end to end.
    rng = random.Random(38)
    for _ in range(300):
        picks = {gate.id: rng.choice(RAW_STATUSES) for gate in rg.GATES}
        gates = tuple(rg.Gate(gate.id, gate.title, gate.blocking, None,
                              _raw_gate(0, gate.blocking, picks[gate.id]).run)
                      for gate in rg.GATES)
        model = rg.build_release_readiness(_ctx(project).inputs, gates)
        statuses = {row["id"]: row["status"] for row in model["gates"]}
        bad = {gate.id for gate in rg.GATES
               if gate.blocking and statuses[gate.id] in ("fail", "not_verified")}
        assert (model["verdict"] == "pass") == (not bad)
        assert set(model["blocking_failed"]) | set(model["blocking_not_verified"]) == bad


def test_pass_summary_names_blocking_warnings():
    result = rg.verdict(_rows("pass", "warn", "not_applicable"))
    assert result["verdict"] == "pass"
    assert "g1" in result["verdict_summary"] and "warn" in result["verdict_summary"]
    assert "every blocking gate passed" not in result["verdict_summary"]


def test_run_gate_guards(project):
    ctx = _ctx(project)

    def boom(_):
        raise RuntimeError("broken source")
    row = rg.run_gate(rg.Gate("x", "X", True, None, boom), ctx)
    assert row["status"] == "not_verified" and "RuntimeError: broken source" in row["evidence"]
    assert set(row) == ROW_KEYS and row["ms"] >= 0
    # Only blocking gates fail; a non-blocking problem is a warn.
    row = rg.run_gate(rg.Gate("x", "X", False, None, lambda c: ("fail", "bad")), ctx)
    assert row["status"] == "warn"
    row = rg.run_gate(rg.Gate("x", "X", True, None, lambda c: ("maybe", "?")), ctx)
    assert row["status"] == "not_verified"
    row = rg.run_gate(rg.Gate("x", "X", True, None, lambda c: ("pass", "word " * 200)), ctx)
    assert len(row["evidence"]) <= rg.EVIDENCE_CHARS and row["evidence"].endswith("…")


def test_gate_table_is_fixed():
    assert len(set(GATE_IDS)) == len(GATE_IDS)
    blocking = {gate.id for gate in rg.GATES if gate.blocking}
    assert blocking == {"drc", "erc", "netlist_fresh", "sch_pcb_sync", "unrouted_nets",
                        "polygons", "board_outline", "outjob_coverage", "fab_drawing",
                        "project_integrity", "clone_status"}
    registered = {tool.name for tool in asyncio.run(main.mcp.list_tools())}
    for gate in rg.GATES:
        assert gate.source_tool is None or gate.source_tool in registered, gate.id


def test_paging_keeps_the_verdict(project):
    gates = tuple(rg.Gate(f"g{n}", f"G{n}", n == 0, None,
                          lambda c, n=n: ("fail" if n == 0 else "pass", "e"))
                  for n in range(7))
    model = rg.build_release_readiness(_ctx(project).inputs, gates)
    assert model["verdict"] == "fail" and model["counts"]["fail"] == 1
    seen, offset = [], 0
    while True:
        page = rg.page_release_readiness(model, offset, 3)
        assert page["verdict"] == "fail" and page["gates_total"] == 7 and page["notes"]
        seen += [row["id"] for row in page["gates"]]
        if not page["has_more"]:
            assert "next_offset" not in page
            break
        offset = page["next_offset"]
    assert seen == [f"g{n}" for n in range(7)]


# ---------- DRC and ERC ----------

def _report(violations=0, **tables):
    rows = [DrcTable(type="rule", title=title, count=count) for title, count in tables.items()]
    return DrcReport(format="html", totals={"rule_violations": violations}, tables=rows)


def _drc(project, report, *issues):
    path = Path(project["root_dir"]) / "Design Rule Check - Board.html"
    path.write_text("x", encoding="utf-8")
    ctx = _ctx(project)
    ctx.drc = {"report": report, "path": path,
               "issues": [{"issue": issue, "severity": "warning", "detail": f"{issue} detail"}
                          for issue in issues]}
    return _run("drc", ctx)


def test_drc_gate(project):
    assert _drc(project, _report())["status"] == "pass"
    row = _drc(project, _report(3, **{"Clearance Constraint (Gap=10mil) (All),(All)": 3}))
    assert row["status"] == "fail" and "3 rule violations" in row["evidence"]
    assert "Clearance Constraint: 3" in row["evidence"]
    assert _drc(project, _report(), "violation_limit_reached")["status"] == "fail"
    stale = _drc(project, _report(), "older_than_board")
    assert stale["status"] == "fail" and "not newer than the design" in stale["evidence"]
    for issue in ("other_board", "report_incomplete"):
        assert _drc(project, _report(), issue)["status"] == "not_verified"
    # A report for another board is not this board's, violations or not.
    assert _drc(project, _report(2), "other_board")["status"] == "not_verified"
    assert _drc(project, _report(), "rule_kind_not_checked")["status"] == "warn"
    assert _drc(project, _report(), "saved_after_report")["status"] == "pass"


def _drc_unchecked(project, label):
    path = Path(project["root_dir"]) / "Design Rule Check - Board.html"
    path.write_text("x", encoding="utf-8")
    ctx = _ctx(project)
    ctx.drc = {"report": _report(), "path": path, "issues": [{
        "issue": "rule_kind_not_checked", "severity": "warning", "rule_kind": label,
        "detail": f"{label}: the board has no enabled {label} rule."}]}
    return _run("drc", ctx)


def test_drc_gate_with_shorts_not_checked_is_not_verified(project):
    # A clean report that never checked Short-Circuit cannot show the board has no
    # shorts, and no other gate checks them: not verified, never a pass or a warn.
    row = _drc_unchecked(project, "Short-Circuit")
    assert row["status"] == "not_verified" and "Short-Circuit" in row["evidence"]
    # Un-Routed Net is checked from the copper by unrouted_nets: a caution only.
    assert _drc_unchecked(project, "Un-Routed Net")["status"] == "warn"
    # get_drc_report's own assessment names the kind as the gate expects.
    path = Path(project["root_dir"]) / "Design Rule Check - Board.html"
    issues = rg.assess(_report(), path, Path(project["pcb_doc_paths"][0]), project,
                       SimpleNamespace(rules=[], drc_options=None))
    labels = {issue.get("rule_kind") for issue in issues
              if issue["issue"] == "rule_kind_not_checked"}
    assert rg.SHORT_CIRCUIT_LABEL in labels


def test_drc_gate_without_a_report_or_board(project):
    ctx = _ctx(project)
    ctx.drc = None
    row = _run("drc", ctx)
    assert row["status"] == "not_verified" and "Design Rule Check - Board" in row["evidence"]
    assert _run("drc", _ctx(dict(project, pcb_doc_paths=[])))["status"] == "not_applicable"


def test_erc_gate_is_never_verified(project):
    ctx = _ctx(project)
    ctx.outjobs = {"coverage": {"erc": {"status": "connected", "outputs": ["a: ERC"]}},
                   "summary": {"by_check": {"erc_settings_differ": "warning"}}}
    row = _run("erc", ctx)
    assert row["status"] == "not_verified" and row["blocking"]
    assert "Project Releaser runs ERC" in row["evidence"] and "differ" in row["evidence"]
    ctx = _ctx(project)
    ctx.outjobs = {"coverage": {"erc": {"status": "missing"}}, "summary": {"by_check": {}}}
    assert "does not run ERC" in _run("erc", ctx)["evidence"]
    assert _run("erc", _ctx(dict(project, sheets=[])))["status"] == "not_applicable"


# ---------- netlist and sync ----------

def test_netlist_fresh_gate(project):
    assert _run("netlist_fresh", _ctx(project))["status"] == "not_verified"
    netlist = {"net_file_mtime": 1_700_000_000.0, "components": {}}
    fresh = _ctx(project, netlist=netlist)
    assert _run("netlist_fresh", fresh)["status"] == "pass"
    stale = _ctx(project, netlist=netlist,
                 saved_after_netlist=lambda p, c: (["Power"], ["Board.PrjPcb"]))
    row = _run("netlist_fresh", stale)
    assert row["status"] == "fail" and "Power" in row["evidence"] and "refresh_netlist" in \
        row["evidence"]
    assert _run("netlist_fresh", _ctx(project, netlist={}))["status"] == "not_verified"
    # A .PrjPcb or .Annotation saved after the netlist is as stale as a sheet.
    others = _ctx(project, netlist=netlist,
                  saved_after_netlist=lambda p, c: ([], ["Board.Annotation"]))
    row = _run("netlist_fresh", others)
    assert row["status"] == "fail" and "Board.Annotation" in row["evidence"]
    assert _run("netlist_fresh", _ctx(dict(project, sheets=[])))["status"] == "not_applicable"


def test_sch_pcb_sync_gate(project, pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    assert _run("sch_pcb_sync", _ctx(project, index))["status"] == "not_verified"
    assert _run("sch_pcb_sync", _ctx(project, index, netlist={"components": {}}))[
        "status"] == "not_verified"
    result = {"in_sync": True, "components": {"schematic": 3}, "nets": {"schematic": 3},
              "severity_counts": {"error": 0, "warning": 0}, "summary": {}}
    with patch.object(rg, "run_sync_check", return_value=result) as sync:
        assert _run("sch_pcb_sync", _ctx(project, index, netlist=NETLIST))["status"] == "pass"
        assert sync.call_args.kwargs["limit"] == 1
    result.update(in_sync=False, severity_counts={"error": 2, "warning": 1},
                  summary={"missing_on_pcb": 2, "pin_net": 1})
    with patch.object(rg, "run_sync_check", return_value=result):
        row = _run("sch_pcb_sync", _ctx(project, index, netlist=NETLIST,
                                         sheets_saved_after=lambda p, s: ["IO"]))
    assert row["status"] == "fail" and "missing_on_pcb 2" in row["evidence"]
    assert "IO" in row["evidence"]
    unreadable = _run("sch_pcb_sync", _ctx(project, netlist=NETLIST))
    assert unreadable["status"] == "not_verified" and "bad file" in unreadable["evidence"]


# ---------- the board ----------

def test_unrouted_gate_on_the_synthetic_board(project, pcb_data):
    # GND's SMD pads have no copper between them (the pour is on an inner layer).
    row = _run("unrouted_nets", _ctx(project, PcbIndex(pcb_data, NETLIST)))
    assert row["status"] == "fail" and "GND" in row["evidence"]
    full = {"summary": {"nets_with_2_or_more_pads": 4, "connections": {"total": 6,
                                                                        "missing": 0}},
            "_not_routed_nets": [], "nets_unrouted_in_altium": []}
    with patch.object(rg, "review_connectivity", return_value=full):
        assert _run("unrouted_nets", _ctx(project, PcbIndex(pcb_data, NETLIST)))[
            "status"] == "pass"
        full["nets_unrouted_in_altium"] = ["VCC"]
        row = _run("unrouted_nets", _ctx(project, PcbIndex(pcb_data, NETLIST)))
    assert row["status"] == "fail" and "weak joins" in row["evidence"]


def test_polygon_gate(project, pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    assert _run("polygons", _ctx(project, index))["status"] == "pass"
    shelved = {"summary": {"shelved_modified_polygons": 2}}
    with patch.object(rg, "run_health_check", return_value=shelved) as health:
        row = _run("polygons", _ctx(project, index))
    assert row["status"] == "fail" and "2 polygons of 1" in row["evidence"]
    assert health.call_args.kwargs["checks"] == ["shelved_modified_polygons"]
    pcb_data.pours = []
    assert _run("polygons", _ctx(project, PcbIndex(pcb_data, NETLIST)))[
        "status"] == "not_applicable"


def test_board_gates_without_a_readable_pcb(project):
    for gate_id in ("unrouted_nets", "polygons", "board_outline", "fiducials",
                    "mounting_holes", "silkscreen"):
        row = _run(gate_id, _ctx(project))
        assert row["status"] == "not_verified" and "bad file" in row["evidence"], gate_id
        none = _run(gate_id, _ctx(dict(project, pcb_doc_paths=[])))
        assert none["status"] == "not_applicable", gate_id


def test_board_outline_gate(project, pcb_data):
    row = _run("board_outline", _ctx(project, PcbIndex(pcb_data, NETLIST)))
    assert row["status"] == "fail" and "Board Shape" in row["evidence"]
    pcb_data.board.outline_shape = [ShapeVertex(0, 0), ShapeVertex(1000, 0),
                                    ShapeVertex(1000, 500), ShapeVertex(0, 500)]
    row = _run("board_outline", _ctx(project, PcbIndex(pcb_data, NETLIST)))
    assert row["status"] == "pass" and "25.4 x 12.7 mm" in row["evidence"]


def test_fiducial_gate(project, pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    counts = {"top": {"smd": 12, "tht": 0, "mixed": 0}, "bottom": {"smd": 0, "tht": 1,
                                                                    "mixed": 0}}
    placed = SimpleNamespace(counts=counts)
    entry = {"status": "advisory", "limit": None, "value": {"top": 1, "bottom": 0},
             "missing_global": {"top": 2, "bottom": 0}}
    with patch.object(rg, "placements", return_value=placed), \
         patch.object(rg, "review_dfm", return_value={"results": {"fiducials": entry}}) as dfm:
        row = _run("fiducials", _ctx(project, index))
        assert row["status"] == "warn" and not row["blocking"]
        assert "top 1 for 12 SMD placements" in row["evidence"] and "bottom" not in \
            row["evidence"]
        assert dfm.call_args.args[2] == ["fiducials"]
        entry["missing_global"] = {"top": 0, "bottom": 0}
        assert _run("fiducials", _ctx(project, index))["status"] == "pass"
        counts["top"]["smd"] = 0
        assert _run("fiducials", _ctx(project, index))["status"] == "not_applicable"


def test_fiducial_gate_counts_sides_as_check_dfm_does(project, pcb_data):
    # check_dfm counts mixed parts (SMD and THT pads) as SMD placements on a side: a
    # side of mixed parts only still needs fiducials, and the counts quoted are dfm's.
    index = PcbIndex(pcb_data, NETLIST)
    counts = {"top": {"smd": 0, "tht": 0, "mixed": 4}, "bottom": {"smd": 2, "tht": 0,
                                                                   "mixed": 1}}
    entry = {"status": "advisory", "limit": None, "value": {"top": 0, "bottom": 3},
             "missing_global": {"top": 3, "bottom": 0}}
    with patch.object(rg, "placements", return_value=SimpleNamespace(counts=counts)), \
         patch.object(rg, "review_dfm", return_value={"results": {"fiducials": entry}}):
        row = _run("fiducials", _ctx(project, index))
    assert row["status"] == "warn"
    assert "top 0 for 4 SMD placements" in row["evidence"]
    assert "bottom 3 for 3 SMD placements" in row["evidence"]


def test_fiducial_gate_on_the_synthetic_board_matches_check_dfm(project, pcb_data):
    # The real check: the side counts quoted are those of check_dfm's own finding text.
    index = PcbIndex(pcb_data, NETLIST)
    row = _run("fiducials", _ctx(project, index))
    profile, _ = rg.load_profile(None, project["root_dir"])
    full = rg.review_dfm(index, profile, ["fiducials"], False, AssemblyContext())
    texts = " ".join(finding.get("issue", "") for finding in full["findings"])
    missing = full["results"]["fiducials"]["missing_global"]
    quoted = re.findall(r"(top|bottom) (\d+) for (\d+) SMD", row["evidence"])
    for side, have, smd in quoted:
        assert int(have) == full["results"]["fiducials"]["value"][side]
        if missing[side]:
            assert f"the {side} side, which has {smd} SMD" in texts, (side, texts)
    assert {side for side, _, _ in quoted} >= {side for side, count in missing.items()
                                               if count}
    assert (row["status"] == "warn") == any(missing.values())


def test_mounting_hole_gate(project, pcb_data):
    row = _run("mounting_holes", _ctx(project, PcbIndex(pcb_data, NETLIST)))
    assert row["status"] == "warn" and not row["blocking"]
    pcb_data.bare_holes = [BareHole(x=50, y=50, hole=125.0, layer=74, net=None,
                                    component=None, pad_name="")]
    row = _run("mounting_holes", _ctx(project, PcbIndex(pcb_data, NETLIST)))
    assert row["status"] == "pass" and "1 free bare hole" in row["evidence"]


def test_mounting_hole_gate_counts_free_pads_with_a_large_hole(project, pcb_data):
    pcb_data.primitives.append(dataclasses.replace(
        _pad(net=None, component=None, x=50.0, y=50.0, width=150.0), hole=125.0, layer=74))
    row = _run("mounting_holes", _ctx(project, PcbIndex(pcb_data, NETLIST)))
    assert row["status"] == "pass" and "1 free pad with a hole of 1 mm" in row["evidence"]


@pytest.mark.parametrize("gate_id, target", [
    ("unrouted_nets", "review_connectivity"),
    ("polygons", "run_health_check"),
    ("fiducials", "review_dfm"),
    ("testpoints", "review_testpoints"),
])
def test_board_gates_quote_their_source_errors(project, pcb_data, gate_id, target):
    # A source tool's error answer is not_verified with its message, never a pass.
    error = {"error": "invalid_checks", "message": "source says no"}
    counts = {"top": {"smd": 3, "tht": 0, "mixed": 0}, "bottom": {"smd": 0, "tht": 0,
                                                                  "mixed": 0}}
    with patch.object(rg, target, return_value=error), \
         patch.object(rg, "placements", return_value=SimpleNamespace(counts=counts)):
        row = _run(gate_id, _ctx(project, PcbIndex(pcb_data, NETLIST)))
    assert row["status"] == "not_verified" and "source says no" in row["evidence"]


def test_testpoints_gate(project, pcb_data):
    # check_testpoints on its ICT profile, advisory: no probe site is a warn, never a pass.
    row = _run("testpoints", _ctx(project, PcbIndex(pcb_data, NETLIST)))
    assert row["status"] == "warn" and not row["blocking"]
    assert row["source_tool"] == "check_testpoints"
    assert "No probe sites" in row["evidence"] and "Assembly Testpoint" in row["evidence"]
    pcb_data.primitives[8].test_fab_top = True  # the CLK_A via
    row = _run("testpoints", _ctx(project, PcbIndex(pcb_data, NETLIST)))
    # GND (3 connections, the only net of 2+ pads) has no site, the via is 20 mil and
    # there is no tooling hole.
    assert row["status"] == "warn"
    assert row["evidence"] == (
        "1 probe site reaches 0 of 1 net (0.0%; top 0, bottom 0); 3 warnings on ICT "
        "guidance (ground 1, pad_size 1, tooling_holes 1).")
    clean = {"sites": {"probe_sites": 4}, "findings": [
        {"check": "spacing", "severity": "info"}],
        "coverage": {"nets": 3, "with_access": 3, "percent": 100.0, "top": 3, "bottom": 1}}
    with patch.object(rg, "review_testpoints", return_value=clean):
        row = _run("testpoints", _ctx(project, PcbIndex(pcb_data, NETLIST)))
    assert row["status"] == "pass"
    assert row["evidence"] == ("4 probe sites reach 3 of 3 nets (100.0%; top 3, bottom 1); "
                               "no warning on ICT guidance.")
    # No net of 2+ pads: no share to quote (coverage percent is None).
    clean["coverage"] = {"nets": 0, "with_access": 0, "percent": None, "top": 0, "bottom": 0}
    with patch.object(rg, "review_testpoints", return_value=clean):
        row = _run("testpoints", _ctx(project, PcbIndex(pcb_data, NETLIST)))
    assert row["evidence"] == ("4 probe sites reach 0 of 0 nets (no net of 2+ pads; top 0, "
                               "bottom 0); no warning on ICT guidance.")


def test_fiducial_gate_not_checked_by_check_dfm(project, pcb_data):
    counts = {"top": {"smd": 3, "tht": 0, "mixed": 0}, "bottom": {"smd": 0, "tht": 0,
                                                                  "mixed": 0}}
    entry = {"status": "not_checked", "reason": "no placements read"}
    with patch.object(rg, "review_dfm", return_value={"results": {"fiducials": entry}}), \
         patch.object(rg, "placements", return_value=SimpleNamespace(counts=counts)):
        row = _run("fiducials", _ctx(project, PcbIndex(pcb_data, NETLIST)))
    assert row["status"] == "not_verified" and "no placements read" in row["evidence"]


def test_sync_and_fab_gates_quote_their_source_errors(project, pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    error = {"error": "no_components_loaded", "message": "source says no"}
    with patch.object(rg, "run_sync_check", return_value=error):
        row = _run("sch_pcb_sync", _ctx(project, index, netlist=NETLIST))
    assert row["status"] == "not_verified" and "source says no" in row["evidence"]
    drawn = dict(project, pcb_dwf_paths=["Fab.PCBDwf"])
    with patch.object(rg, "build_fab_drawing", return_value=error):
        row = _run("fab_drawing", _ctx(drawn))
    assert row["status"] == "not_verified" and "source says no" in row["evidence"]


def test_silkscreen_gate(project, pcb_data):
    rule = PcbRule(name="Silk", kind="SilkToSolderMaskClearance", kind_id=54, enabled=True,
                   priority=1, scope1="All", scope2="All", net_scope=None, comment="",
                   unique_id=None, from_schematic=False)
    pcb_data.rules = [rule]
    pcb_data.drc_options = DrcOptions(batch_kinds={54})
    index = PcbIndex(pcb_data, NETLIST)
    ctx = _ctx(project, index)
    ctx.drc = None
    assert _run("silkscreen", ctx)["status"] == "not_verified"
    report = _report(2, **{"Silk To Solder Mask (Clearance=4mil) (All),(All)": 2})
    ctx = _ctx(project, index)
    ctx.drc = {"report": report, "path": None, "issues": []}
    row = _run("silkscreen", ctx)
    assert row["status"] == "warn" and "Silk To Solder Mask" in row["evidence"]
    ctx = _ctx(project, index)
    ctx.drc = {"report": _report(), "path": None, "issues": []}
    assert _run("silkscreen", ctx)["status"] == "pass"
    ctx = _ctx(project, index)
    ctx.drc = {"report": _report(), "path": None,
               "issues": [{"issue": "older_than_board", "detail": ""}]}
    assert _run("silkscreen", ctx)["status"] == "not_verified"
    # A run stopped at its violation limit reads 0 for the rules after the stop.
    ctx = _ctx(project, index)
    ctx.drc = {"report": _report(), "path": None,
               "issues": [{"issue": "violation_limit_reached", "detail": ""}]}
    assert _run("silkscreen", ctx)["status"] == "not_verified"
    ctx = _ctx(project, index)
    ctx.drc = {"report": report, "path": None,
               "issues": [{"issue": "violation_limit_reached", "detail": ""}]}
    assert _run("silkscreen", ctx)["status"] == "warn"
    pcb_data.drc_options = DrcOptions(batch_kinds={0})
    ctx = _ctx(project, PcbIndex(pcb_data, NETLIST))
    ctx.drc = {"report": _report(), "path": None, "issues": []}
    assert _run("silkscreen", ctx)["status"] == "not_verified"


# ---------- OutJobs ----------

def _outjobs(findings=(), outputs=(), coverage=None, containers=()):
    rows = [{"output": name, "type": kind, "state": "connected", "variant": variant}
            for name, kind, variant in outputs]
    return {
        "outjobs": [{"file": "Release.OutJob", "listed": True}],
        "coverage": coverage or {role: {"status": "connected"} for role in rg.ROLE_LABELS},
        "summary": {"by_check": {}},
        "findings": [{"check": check, "severity": severity, "count": 1, "examples": []}
                     for check, severity in findings],
        "details": [{"file": "Release.OutJob", "containers": list(containers),
                     "outputs_by_category": {"Assembly": rows}}],
    }


def _variants(*dnp):
    return VariantState([VariantDefinition(name="Prod", dnp_refdes=list(dnp)),
                         VariantDefinition(name="Lite")])


def test_outjob_coverage_gate(project):
    ctx = _ctx(project, variant_state=_variants())
    ctx.outjobs = _outjobs(outputs=[("BOM", "BOM_PartType", "Prod"),
                                    ("PnP", "Pick Place", "Lite")])
    row = _run("outjob_coverage", ctx)
    assert row["status"] == "pass" and "fabrication data" in row["evidence"]
    ctx = _ctx(project, variant_state=_variants())
    ctx.outjobs = _outjobs([("no_pick_and_place", "warning"), ("not_connected", "warning")],
                           outputs=[("BOM", "BOM_PartType", "Prod")])
    row = _run("outjob_coverage", ctx)
    assert row["status"] == "fail" and "no pick-and-place output" in row["evidence"]
    ctx = _ctx(project, variant_state=_variants())
    ctx.outjobs = _outjobs([("not_connected", "warning"), ("outjob_files", "info")],
                           outputs=[("BOM", "BOM_PartType", "Prod")])
    row = _run("outjob_coverage", ctx)
    assert row["status"] == "warn" and "no BOM or pick-and-place output for Lite" in \
        row["evidence"]
    ctx = _ctx(project)
    ctx.outjobs = dict(_outjobs(), outjobs=[{"file": "Old.OutJob", "listed": False}])
    assert _run("outjob_coverage", ctx)["status"] == "fail"
    ctx = _ctx(project)
    ctx.outjobs = {"error": "project_file_unreadable", "message": "locked"}
    assert _run("outjob_coverage", ctx)["status"] == "not_verified"
    # get_output_jobs' errors fail the release whatever the check.
    ctx = _ctx(project)
    ctx.outjobs = _outjobs([("wrong_document", "error")])
    row = _run("outjob_coverage", ctx)
    assert row["status"] == "fail" and "outputs on a missing or foreign" in row["evidence"]


def test_outjob_gates_without_a_readable_outjob(project):
    ctx = _ctx(project, variant_state=_variants("R1"))
    ctx.outjobs = dict(_outjobs(outputs=[("PnP", "Pick Place", "Prod")]),
                       outjobs=[{"file": "Release.OutJob", "listed": True, "error": "bad"}])
    row = _run("dnp_parts", ctx)
    assert row["status"] == "not_verified" and "No readable OutJob" in row["evidence"]
    assert _run("outputs_current", ctx)["status"] == "not_applicable"


def test_dnp_gate(project):
    assert _run("dnp_parts", _ctx(project, variant_state=_variants()))[
        "status"] == "not_applicable"
    ctx = _ctx(project, variant_state=_variants("R1"))
    ctx.outjobs = _outjobs(outputs=[("BOM", "BOM_PartType", "Prod"),
                                    ("PnP", "Pick Place", "Prod")])
    row = _run("dnp_parts", ctx)
    assert row["status"] == "pass" and "Prod (1)" in row["evidence"]
    ctx = _ctx(project, variant_state=_variants("R1"))
    ctx.outjobs = _outjobs(outputs=[("PnP", "Pick Place", "[No Variations]")])
    row = _run("dnp_parts", ctx)
    assert row["status"] == "warn" and "[No Variations]" in row["evidence"]
    ctx = _ctx(project, variant_state=_variants("R1"))
    ctx.outjobs = _outjobs()
    assert _run("dnp_parts", ctx)["status"] == "not_verified"


def test_dnp_gate_names_only_the_variants_the_outputs_use(project):
    # Outputs on Lite (nothing Not Fitted) do not leave out Prod's Not Fitted parts.
    ctx = _ctx(project, variant_state=_variants("R1"))
    ctx.outjobs = _outjobs(outputs=[("BOM", "BOM_PartType", "Lite"),
                                    ("PnP", "Pick Place", "Lite")])
    row = _run("dnp_parts", ctx)
    assert row["status"] == "pass" and "Lite" in row["evidence"]
    assert "Prod" not in row["evidence"]


def test_all_variants_on_an_assembly_output_places_every_part(project):
    # get_output_jobs reads [All Variants] as [No Variations] (every part shown): the
    # gates do the same, so it neither hides Not Fitted parts nor covers each variant.
    ctx = _ctx(project, variant_state=_variants("R1"))
    ctx.outjobs = _outjobs(outputs=[("PnP", "Pick Place", "[All Variants]")])
    row = _run("dnp_parts", ctx)
    assert row["status"] == "warn" and "on [All Variants]" in row["evidence"]
    row = _run("outjob_coverage", ctx)
    assert row["status"] == "warn" and "no BOM or pick-and-place output for Prod" in \
        row["evidence"]


def test_outputs_current_gate(project):
    ctx = _ctx(project)
    ctx.outjobs = _outjobs(containers=[{"generated": {"newest_output_utc": "2026-01-02"}}])
    assert _run("outputs_current", ctx)["status"] == "pass"
    ctx = _ctx(project)
    model = _outjobs([("stale_outputs", "warning")])
    model["findings"][0]["examples"] = [{"newest_output_utc": "2026-01-01",
                                         "documents_newer": [{"document": "Board.PcbDoc"}]}]
    ctx.outjobs = model
    row = _run("outputs_current", ctx)
    assert row["status"] == "warn" and "Board.PcbDoc" in row["evidence"]
    ctx = _ctx(project)
    ctx.outjobs = _outjobs()
    assert _run("outputs_current", ctx)["status"] == "not_verified"


def test_not_verifiable_gates(project):
    ctx = _ctx(project)
    ctx.outjobs = _outjobs(outputs=[("Footprints", "Footprint Comparison Report", "")])
    for gate_id in ("footprints_vs_libraries", "lifecycle_compliance", "teardrops",
                    "via_stitching", "team_template"):
        row = _run(gate_id, ctx)
        assert row["status"] == "not_verified" and not row["blocking"], gate_id
    assert "Footprint Comparison Report output is connected" in \
        _run("footprints_vs_libraries", ctx)["evidence"]


# ---------- drawings, title blocks, files, clone ----------

def test_fab_drawing_gate(project):
    # No Draftsman drawing: the fab notes (a mechanical layer, a PDF) are a source the
    # gate cannot read, so not verified, never passed as not applicable.
    row = _run("fab_drawing", _ctx(project))
    assert row["status"] == "not_verified" and row["blocking"]
    assert "no Draftsman drawing" in row["evidence"] and "PDF" in row["evidence"]
    # Without a board there is no stackup to agree with.
    no_board = dict(project, pcb_doc_paths=[])
    assert _run("fab_drawing", _ctx(no_board))["status"] == "not_applicable"
    project = dict(project, pcb_dwf_paths=["Fab.PCBDwf"])
    model = {"drawings_total": 1, "boards": [{"pcb_doc": "Board.PcbDoc"}], "findings": [
        {"check": "thickness", "severity": "error", "drawing": "Fab.PCBDwf",
         "issue": "1.6 mm stated, the stack is 1.2 mm."}]}
    with patch.object(rg, "build_fab_drawing", return_value=model):
        row = _run("fab_drawing", _ctx(project))
        assert row["status"] == "fail" and "1.6 mm stated" in row["evidence"]
        model["findings"][0]["severity"] = "warning"
        assert _run("fab_drawing", _ctx(project))["status"] == "warn"
        model["findings"] = []
        assert _run("fab_drawing", _ctx(project))["status"] == "pass"
        model["boards"] = [{"pcb_doc": "Board.PcbDoc", "error": "gone"}]
        assert _run("fab_drawing", _ctx(project))["status"] == "not_verified"


def test_title_block_gate(project):
    notes = FakeNotes({"power": {"Revision": "B", "Date": "2026-10-01"},
                       "io": {"Revision": "*", "Date": "2026-10-01"}})
    project = dict(project, parameters={"Revision": "B"})
    row = _run("title_block", _ctx(project, notes=notes))
    assert row["status"] == "pass" and "Revision 'B'" in row["evidence"]
    notes.sheets["io"]["Revision"] = "A"
    row = _run("title_block", _ctx(project, notes=notes))
    assert row["status"] == "warn" and "Revision differs" in row["evidence"]
    assert "'A' on IO" in row["evidence"]
    notes.sheets["io"] = {"Revision": "=Rev", "Rev": "B"}
    row = _run("title_block", _ctx(project, notes=notes))
    assert row["status"] == "warn" and "Date missing on IO" in row["evidence"]
    project = dict(project, pcb_dwf_paths=["Fab.PCBDwf"])
    notes.sheets["io"]["Date"] = "2026-10-01"
    notes.drawings["Fab.PCBDwf"] = {"Revision": "C"}
    row = _run("title_block", _ctx(project, notes=notes))
    assert row["status"] == "warn" and "'C' on Fab.PCBDwf" in row["evidence"]
    assert _run("title_block", _ctx(project))["status"] == "not_verified"
    assert _run("title_block", _ctx(dict(project, sheets=[])))["status"] == "not_applicable"
    # One sheet unread: the others agreeing is not enough.
    del notes.sheets["io"]
    row = _run("title_block", _ctx(dict(project, pcb_dwf_paths=[]), notes=notes))
    assert row["status"] == "warn" and "1 sheet could not be read" in row["evidence"]


def test_project_integrity_gate(project):
    summary = {"severity_counts": {"error": 1, "warning": 0, "info": 0},
               "by_check": {"legacy_extension": {"worst": "error"}}}
    model = {"summary": summary, "checks_run": ["legacy_extension"]}
    with patch.object(rg, "build_project_integrity", return_value=model):
        row = _run("project_integrity", _ctx(project))
        assert row["status"] == "fail" and "legacy_extension" in row["evidence"]
        summary.update(severity_counts={"error": 0, "warning": 2, "info": 0},
                       by_check={"whitespace": {"worst": "warning"}})
        assert _run("project_integrity", _ctx(project))["status"] == "warn"
        summary.update(severity_counts={"error": 0, "warning": 0, "info": 3}, by_check={})
        assert _run("project_integrity", _ctx(project))["status"] == "pass"
        model["sheets_not_read"] = ["IO"]
        row = _run("project_integrity", _ctx(project))
        assert row["status"] == "warn" and "1 sheet not read" in row["evidence"]
        del model["sheets_not_read"]
        row = _run("project_integrity", _ctx(project, netlist={"sheet_errors": [{}, {}]}))
        assert row["status"] == "warn" and "2 sheets not read" in row["evidence"]


@pytest.mark.parametrize("status, expected", [
    ({"altium365": False, "reason": "origin_is_not_altium365"}, "not_applicable"),
    ({"altium365": True, "error": "git_not_found", "message": "no git"}, "not_verified"),
    ({"altium365": True, "uncommitted_design_files": 2, "days_since_fetch": 1}, "fail"),
    ({"altium365": True, "ahead": 1, "days_since_fetch": 1}, "fail"),
    ({"altium365": True, "behind": 3, "days_since_fetch": 1}, "fail"),
    ({"altium365": True, "days_since_fetch": 40,
      "warnings": [{"warning": "fetch_stale", "message": "40 days ago."}]}, "warn"),
    ({"altium365": True, "days_since_fetch": 0, "warnings": []}, "pass"),
])
def test_clone_status_gate(project, status, expected):
    row = _run("clone_status", _ctx(project, clone_status=lambda folder: status))
    assert row["status"] == expected


# ---------- the tool ----------

def test_tool_on_the_netlist_free_project(env):
    root, _, _ = env
    _load(root)
    result = json.loads(main.check_release_readiness())
    assert [row["id"] for row in result["gates"]] == GATE_IDS
    assert all(set(row) == ROW_KEYS for row in result["gates"])
    statuses = {row["id"]: row["status"] for row in result["gates"]}
    # Missing sources are not verified, never passed.
    for gate_id in ("drc", "erc", "netlist_fresh", "sch_pcb_sync", "silkscreen"):
        assert statuses[gate_id] == "not_verified", gate_id
    assert statuses["unrouted_nets"] == "fail" and statuses["board_outline"] == "fail"
    assert statuses["clone_status"] == "not_applicable"
    assert statuses["fab_drawing"] in ("pass", "warn", "fail")
    assert result["verdict"] == "fail" and result["load"] == "netlist_free"
    assert result["project"] == "Board" and result["active_variant"] == "Prod"
    assert result["gates_total"] == len(GATE_IDS) and result["has_more"] is False
    assert sum(result["counts"].values()) == len(GATE_IDS)


def test_tool_pages_when_the_result_is_too_large(env):
    root, _, _ = env
    _load(root)
    with patch.object(main, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 6000):
        page = json.loads(main.check_release_readiness())
        assert page["has_more"] is True and len(page["gates"]) < len(GATE_IDS)
        assert len(main.check_release_readiness()) <= 6000
        rest = json.loads(main.check_release_readiness(offset=page["next_offset"]))
    assert rest["verdict"] == page["verdict"]
    assert rest["gates"][0]["id"] == GATE_IDS[page["next_offset"]]


def test_tool_needs_a_project(env):
    assert json.loads(main.check_release_readiness())["error"] == "no_project"
    assert json.loads(main.release_review())["error"] == "no_project"


def test_instructions_name_the_tools_after_the_cut():
    assert main.SERVER_INSTRUCTIONS.index("check_release_readiness") > 4096
    assert main.SERVER_INSTRUCTIONS.index("release_review") > 4096


# ---------- the release_review prompt ----------

def test_prompt_placeholders_are_exactly_the_build_arguments():
    fields = {field for _, field, _, _ in string.Formatter().parse(rg.RELEASE_REVIEW_PROMPT)
              if field is not None}
    assert fields == {"name", "pcb_doc", "active_variant", "load"}


def test_prompt_tools_are_named_and_registered():
    registered = {tool.name for tool in asyncio.run(main.mcp.list_tools())}
    assert set(rg.RELEASE_REVIEW_TOOLS) <= registered
    for tool in rg.RELEASE_REVIEW_TOOLS:
        assert re.search(rf"\b{tool}\b", rg.RELEASE_REVIEW_PROMPT), tool
    named = {tool for tool in registered if re.search(rf"\b{tool}\b", rg.RELEASE_REVIEW_PROMPT)}
    assert named <= set(rg.RELEASE_REVIEW_TOOLS), named - set(rg.RELEASE_REVIEW_TOOLS)
    sources = {gate.source_tool for gate in rg.GATES if gate.source_tool}
    assert sources <= set(rg.RELEASE_REVIEW_TOOLS)


def test_prompt_rules():
    prompt = " ".join(rg.RELEASE_REVIEW_PROMPT.split())
    assert "not_verified is never a pass" in prompt
    assert "one failed blocking gate fails the release" in prompt
    # GO follows the verdict: a blocking warn passes, a blocking not_verified never does.
    assert "GO only when the verdict is pass" in prompt
    assert "not_verified gate in the GO" not in prompt
    assert "Never list a not_verified gate as passed" in prompt
    for gate in rg.GATES:
        if gate.source_tool and gate.id not in ("footprints_vs_libraries",):
            assert gate.id in prompt, gate.id


def test_release_review_tool(env):
    root, _, _ = env
    _load(root)
    review = main.release_review()
    assert review.startswith("Project: Board\nPCB: Board.PcbDoc\nActive variant: Prod\n")
    assert "Load: netlist-free" in review and "Netlist-free load" in review
    full = rg.build_release_review({"name": "X", "pcb_doc_paths": []}, "Default")
    assert "PCB: (none)" in full and "Netlist-free load" not in full


# ---------- Altium's public example ----------

@pytest.fixture(scope="module")
def sentinel():
    folder = EXAMPLES / "Bluetooth Sentinel"
    if not (folder / "Bluetooth_Sentinel.PrjPcb").is_file():
        pytest.skip("Altium's Bluetooth Sentinel example is not installed")
    with patch.object(main, "_project", None), \
         patch.object(main, "_variant_state", None), \
         patch.object(main, "_netlist_last_updated", None), \
         patch.object(main, "_pcb_session", main.PcbSession()), \
         patch.object(main, "_design_notes", main.DesignNotesCache()), \
         patch.object(main, "_altium", main.AltiumClient()), \
         patch.object(main, "read_registry", return_value={"projects": []}), \
         patch.object(main, "upsert_registry_entry"), \
         patch.object(main._altium, "get_status", return_value={"running": False}):
        assert json.loads(main.set_project_dir(str(folder)))["loaded"]
        return json.loads(main.check_release_readiness())


def test_bluetooth_sentinel_snapshot(sentinel):
    statuses = {row["id"]: row["status"] for row in sentinel["gates"]}
    assert statuses["unrouted_nets"] == "pass" and statuses["polygons"] == "pass"
    assert statuses["board_outline"] == "pass" and statuses["fiducials"] == "warn"
    assert statuses["drc"] == "not_verified" and statuses["erc"] == "not_verified"
    assert statuses["clone_status"] == "not_applicable"
    assert statuses["dnp_parts"] == "not_applicable"
    assert statuses["fab_drawing"] == "not_verified"  # no Draftsman drawing
    # Loaded without Altium, the sheets are compiled offline, so the netlist gates run:
    # the example's PcbDoc predates its sheets and carries duplicate designators (a stale
    # .Annotation file), so it is not in sync.
    assert statuses["netlist_fresh"] == "pass" and statuses["sch_pcb_sync"] == "fail"
    assert sentinel["verdict"] == "fail"
    assert {"drc", "erc"} <= set(sentinel["blocking_not_verified"])
    assert sentinel["blocking_failed"] == ["sch_pcb_sync"]
