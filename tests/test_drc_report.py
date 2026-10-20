"""Altium Batch DRC reports: parsing, assessment, linking to the board, the tool."""

import json
import os
import shutil
from pathlib import Path

import pytest

import server.main as main_mod
from server.parsers.drc_report import (
    DrcReport, DrcTable, DrcViolation, parse_drc_html, parse_drc_report, parse_drc_txt,
    read_text,
)
from server.parsers.pcb_doc import (
    DiffPair, DrcOptions, PcbComponent, PcbData, PcbRule, PolygonPour,
)
from server.parsers.prj_pcb import VariantState
from server.services.drc_results import (
    DrcLinker, assess, find_reports, load_report, other_sections, pick_report, rule_kind,
    summarize,
)
from server.services.pcb_index import PcbIndex
from tests.test_pcb_index import _board, _pad

FIXTURES = Path(__file__).parent / "fixtures" / "drc"
HTML = FIXTURES / "Design Rule Check - Demo.html"
TXT = FIXTURES / "Design Rule Check - Demo.drc"


def _key(report):
    return [
        (t.type, t.title, t.count, [v.message for v in t.violations])
        for t in report.tables if t.type != "warning"
    ]


# ---------- parsing ----------

def test_html_report():
    report = parse_drc_report(HTML)
    assert (report.format, report.pcb_file) == ("html", "C:\\Work\\Demo\\Demo.PcbDoc")
    assert (report.date, report.time, report.elapsed) == ("3/14/2025", "2:07:41 PM", "00:00:03")
    assert report.totals == {"warnings": 0, "rule_violations": 25, "health_issues": 2}
    rules = [t for t in report.tables if t.type == "rule"]
    assert len(rules) == 26  # with those that found nothing (summary only)
    assert sum(t.count for t in rules) == sum(len(t.violations) for t in rules) == 25
    clearance = rules[0]
    assert clearance.title == "Clearance Constraint (Gap=0.2mm) (All),(All)"
    (violation,) = clearance.violations
    assert violation.message.startswith("Clearance Constraint: (0.15mm < 0.2mm) Between Arc")
    assert violation.box_mil == (1409.449, 1791.339, 1421.26, 1803.15)
    # Two rules with one description stay apart; the second holds the violation.
    twins = [t for t in rules if t.title.startswith("Clearance Constraint (Gap=0.3mm)")]
    assert [t.count for t in twins] == [0, 1]
    (health,) = [t for t in report.tables if t.type == "health"]
    assert (health.title, health.count) == ("Reference Point Outside the Component Area", 2)


def test_html_and_txt_hold_the_same_report():
    html, txt = parse_drc_report(HTML), parse_drc_report(TXT)
    assert txt.format == "txt"
    assert _key(html) == _key(txt)
    assert txt.totals == {"rule_violations": 25, "waived": 0, "health_issues": 2}
    # A text string's own line break is kept in both.
    assert any("\n" in v.message for t in txt.tables for v in t.violations)


TXT_EXTRAS = """\
Protel Design System Design Rule Check
PCB File : D:\\Boards\\Main.PcbDoc
Date     : 21/10/2025
Time     : 14:02:11

ERROR : 500 violations detected, DRC was stopped

WARNING: Unplated multi-layer pad(s) detected
   Pad MH1-1(5mm,5mm) on Multi-Layer
   Pad MH2-1(55mm,5mm) on Multi-Layer

Processing Rule : Hole Size Constraint (Min=0.2mm) (Max=3mm) (All)
   Violation between Hole Size Constraint: (3.2mm > 3mm) Pad J1-1(10mm,10mm) on Multi-Layer Actual Hole Size = 3.2mm
Rule Violations :1

Waived Violations Of Rule : Hole Size Constraint (Min=0.2mm) (Max=3mm) (All)
   Waived Violation between Hole Size Constraint: (3.5mm > 3mm) Pad J2-1(20mm,10mm) on Multi-Layer Actual Hole Size = 3.5mmWaived by K at 1/2/2025 10:00:00 AMmounting post
Waived Violations :1


Violations Detected : 1
Waived Violations : 1
Time Elapsed        : 00:00:01"""


def test_txt_errors_warnings_and_waivers():
    report = parse_drc_txt(TXT_EXTRAS)
    assert report.errors == ["500 violations detected, DRC was stopped"]
    assert report.totals == {"rule_violations": 1, "waived": 1}
    warning, rule, waived = report.tables
    assert (warning.type, warning.title, warning.count) == (
        "warning", "Unplated multi-layer pad(s) detected", 2,
    )
    assert (rule.type, rule.count) == ("rule", 1)
    (row,) = waived.violations
    assert row.message.endswith("Actual Hole Size = 3.5mm")
    assert row.waiver == "Waived by K at 1/2/2025 10:00:00 AMmounting post"


def test_html_error_paragraph():
    text = HTML.read_text(encoding="utf-8").replace(
        "<h1>Design Rule Verification Report</h1>",
        '<h1>Design Rule Verification Report</h1><p class="error">ERROR : 25 violations '
        "detected, DRC was stopped</p>",
    )
    assert parse_drc_html(text).errors == ["25 violations detected, DRC was stopped"]


def test_reports_in_the_windows_code_page(tmp_path):
    path = tmp_path / "r.drc"
    path.write_bytes("Processing Rule : Width Constraint (Ø)\nRule Violations :0\n".encode("cp1252"))
    assert "Ø" in read_text(path)


# ---------- rule kinds, locating reports ----------

def test_rule_kind_by_longest_prefix():
    assert rule_kind("Clearance Constraint (Gap=0.2mm) (All),(All)") == ("Clearance", 0)
    assert rule_kind("Component Clearance Constraint ( Horizontal Gap = 0.2mm )") == (
        "ComponentClearance", 24,
    )
    assert rule_kind("Board Clearance Constraint (Gap=0.3mm) (All)") == (
        "BoardOutlineClearance", 63,
    )
    assert rule_kind("Un-Routed Net Constraint ( (All) )") == ("UnRoutedNet", 16)
    assert rule_kind("Something New (All)") == (None, None)


def _project(tmp_path, output_path=""):
    (tmp_path / "Demo.PrjPcb").write_text("[Design]\n", encoding="utf-8")
    pcb = tmp_path / "Demo.PcbDoc"
    pcb.write_bytes(b"pcb")
    return {
        "prj_pcb_path": str(tmp_path / "Demo.PrjPcb"),
        "pcb_doc_paths": [str(pcb)],
        "output_path": output_path,
        "log_folder_path": "",
    }, pcb


def _put_reports(folder: Path, saved: float):
    folder.mkdir(parents=True, exist_ok=True)
    for source in (HTML, TXT):
        target = folder / source.name
        shutil.copy(source, target)
        os.utime(target, (saved, saved))


def test_find_reports_in_the_output_folder(tmp_path):
    project, pcb = _project(tmp_path)
    _put_reports(tmp_path / "Project Outputs for Demo", 2_000_000)
    found, searched = find_reports(project, pcb)
    assert sorted(p.suffix for p in found) == [".drc", ".html"]
    assert pick_report(found).suffix == ".html"  # one run: the HTML has the zoom boxes
    assert searched == [str(tmp_path / "Project Outputs for Demo")]
    custom, _ = _project(tmp_path, output_path="Out")
    _put_reports(tmp_path / "Out", 3_000_000)
    found, searched = find_reports(custom, pcb)
    assert found[0].parent.name == "Out"  # newest first
    assert searched[0] == str(tmp_path / "Out")


# ---------- assessment ----------

def _rule(kind, kind_id, enabled=True):
    return PcbRule(
        name=kind, kind=kind, kind_id=kind_id, enabled=enabled, priority=1, scope1="All",
        scope2="All", net_scope="AnyNet", comment="", unique_id=None, from_schematic=False,
        properties={},
    )


def _pcb_with(rules=(), options=None):
    return PcbData(
        board=_board(), nets=[], components=[], primitives=[], pours=[],
        layer_name_to_v6={}, rules=list(rules), drc_options=options,
    )


def _report(*tables, errors=(), pcb_file="C:\\X\\Demo.PcbDoc"):
    tables = list(tables)
    return DrcReport(
        format="html", pcb_file=pcb_file, errors=list(errors), tables=tables,
        totals={"rule_violations": sum(t.count for t in tables if t.type == "rule")},
    )


def _table(title, count=0, rows=()):
    return DrcTable(type="rule", title=title, count=count,
                    violations=[DrcViolation(message=m) for m in rows])


def _assess(tmp_path, report, pcb, report_saved=2_000_000, pcb_saved=2_000_000):
    project, pcb_path = _project(tmp_path)
    folder = tmp_path / "Project Outputs for Demo"
    folder.mkdir(exist_ok=True)
    path = folder / "Design Rule Check - Demo.html"
    path.write_text("x", encoding="utf-8")
    os.utime(path, (report_saved, report_saved))
    os.utime(pcb_path, (pcb_saved, pcb_saved))
    return {issue["issue"]: issue for issue in assess(report, path, pcb_path, project, pcb)}


ALL_CHECKED = (
    _table("Short-Circuit Constraint (Allowed=No) (All),(All)"),
    _table("Un-Routed Net Constraint ( (All) )"),
)


def test_a_clean_report_has_nothing_to_say(tmp_path):
    pcb = _pcb_with(
        [_rule("ShortCircuit", 15), _rule("UnRoutedNet", 16)],
        DrcOptions(max_violations=500, batch_kinds={15, 16}, make_report=True,
                   include_health=True),
    )
    report = _report(*ALL_CHECKED)
    report.totals["health_issues"] = 0  # a report with its (empty) health section
    assert _assess(tmp_path, report, pcb) == {}


def test_the_violation_limit(tmp_path):
    pcb = _pcb_with([_rule("ShortCircuit", 15), _rule("UnRoutedNet", 16), _rule("Width", 2)],
                    DrcOptions(max_violations=3, batch_kinds={2, 15, 16}))
    report = _report(_table("Width Constraint (Min=0.2mm) (All)", 3, ["a", "b", "c"]),
                     *ALL_CHECKED)
    issues = _assess(tmp_path, report, pcb)
    limit = issues["violation_limit_reached"]
    assert (limit["severity"], limit["violations"], limit["limit"]) == ("error", 3, 3)
    assert "short circuits included" in limit["detail"]
    # The report's own notice counts even when the board's limit changed since.
    report.errors = ["500 violations detected, DRC was stopped"]
    pcb.drc_options.max_violations = 500
    issues = _assess(tmp_path, report, pcb)
    assert {"report_error", "violation_limit_reached"} <= set(issues)


def test_rule_kinds_not_checked(tmp_path):
    pcb = _pcb_with([_rule("ShortCircuit", 15), _rule("UnRoutedNet", 16)],
                    DrcOptions(max_violations=500, batch_kinds={15}))
    issues = _assess(tmp_path, _report(ALL_CHECKED[0]), pcb)
    detail = issues["rule_kind_not_checked"]["detail"]
    assert "not ticked for batch DRC" in detail and "broken planes" in detail
    pcb = _pcb_with([_rule("ShortCircuit", 15)], DrcOptions(batch_kinds={15, 16}))
    issues = _assess(tmp_path, _report(ALL_CHECKED[0]), pcb)
    assert "no enabled Un-Routed Net rule" in issues["rule_kind_not_checked"]["detail"]


def test_report_age_eco_logs_and_another_board(tmp_path):
    pcb = _pcb_with([_rule("ShortCircuit", 15), _rule("UnRoutedNet", 16)],
                    DrcOptions(batch_kinds={15, 16}))
    report = _report(*ALL_CHECKED)
    assert _assess(tmp_path, report, pcb, pcb_saved=2_000_000 + 600)[
        "saved_after_report"]["severity"] == "info"
    assert _assess(tmp_path, report, pcb, pcb_saved=2_000_000 + 7200)[
        "older_than_board"]["severity"] == "warning"
    logs = tmp_path / "Project Logs for Demo"
    logs.mkdir()
    log = logs / "Demo PCB ECO 1-2-2025 10-00-00.LOG"
    log.write_text("Added Component: Designator=R9(R0603)", encoding="utf-8")
    os.utime(log, (2_100_000, 2_100_000))
    assert "eco_after_report" in _assess(tmp_path, report, pcb)
    other = _report(*ALL_CHECKED, pcb_file="D:\\Old\\Rev1.PcbDoc")
    detail = _assess(tmp_path, other, pcb)["other_board"]["detail"]
    assert "made for D:\\Old\\Rev1.PcbDoc" in detail


def test_rules_changed_and_settings_off(tmp_path):
    pcb = _pcb_with(
        [_rule("ShortCircuit", 15), _rule("UnRoutedNet", 16), _rule("Width", 2),
         _rule("Width", 2, enabled=False)],
        DrcOptions(batch_kinds={2, 15, 16}, include_health=False, make_report=False),
    )
    issues = _assess(tmp_path, _report(*ALL_CHECKED), pcb)
    assert "Width: 0 in the report, 1 on the board" in issues["rules_changed"]["detail"]
    assert {"no_health_section", "report_file_off"} <= set(issues)


# ---------- linking to the board ----------

def _index():
    """U1 pad 5 on CLK, R-1 (a designator holding '-') pad 2 on GND; origin (1000, 1000)."""
    board = _board()
    board.origin_x, board.origin_y = 1000.0, 1000.0
    data = PcbData(
        board=board,
        nets=["GND", "CLK"],
        components=[
            PcbComponent(index=0, refdes="U1", source_uid_tail=None, channel_path=None,
                         x=1500.0, y=1500.0, rotation=0.0, side="top", designator="U1"),
            PcbComponent(index=1, refdes="R-1", source_uid_tail=None, channel_path=None,
                         x=2000.0, y=1500.0, rotation=0.0, side="top", designator="R-1"),
        ],
        primitives=[
            _pad(net=1, component=0, x=1500.0, y=1500.0, name="5"),
            _pad(net=0, component=1, x=2000.0, y=1500.0, name="2"),
        ],
        pours=[],
        layer_name_to_v6={"top layer": 1},
    )
    return PcbIndex(data, None)


def test_link_parts_nets_and_location():
    linker = DrcLinker(_index())
    assert linker.split("R-1-2") == ("R-1", "2")
    linked = linker.link(
        "Clearance Constraint: (0.1mm < 0.2mm) Between Pad U1-5(12.7mm,12.7mm) on Top Layer "
        "And Pad R-1-2(25.4mm,12.7mm) on Top Layer",
        (1495.0, 1495.0, 1505.0, 1505.0),
        "Clearance",
    )
    assert linked == {
        "components": ["U1", "R-1"],
        "nets": ["CLK", "GND"],
        "location_mil": {"x_mil": 1500.0, "y_mil": 1500.0},
    }
    # Without a zoom box (a .drc report) the message's coordinates count from the origin.
    linked = linker.link("Width Constraint: Track (12.7mm,12.7mm)(15mm,12.7mm) on Top Layer",
                         None, "Width")
    assert linked["location_mil"] == {"x_mil": 1500.0, "y_mil": 1500.0}
    assert linker.link("Matched Net Lengths: Between Net A And Net B Length:5mm", None,
                       "MatchedLengths")["nets"] == ["A", "B"]


def _demo_report():
    return parse_drc_report(HTML)


def test_summary_by_rule_component_and_net():
    linker = DrcLinker(None)
    by_rule = summarize(_demo_report(), linker)
    assert (by_rule["rules_checked"], by_rule["total"]) == (26, by_rule["rules_with_violations"])
    first = by_rule["groups"][0]
    assert (first["rule"], first["rule_kind"], first["count"]) == (
        "Clearance Constraint (Gap=0.2mm) (All),(All)", "Clearance", 1,
    )
    # Without the board neither its frame nor its copper is known: no location.
    assert "location_mil" not in first["examples"][0]
    by_component = summarize(_demo_report(), linker, group_by="component")
    assert by_component["groups"][0]["count"] >= by_component["groups"][-1]["count"]
    by_net = summarize(_demo_report(), linker, group_by="net")
    assert by_net["total"] >= 1
    page = summarize(_demo_report(), linker, limit=2)
    assert (len(page["groups"]), page["has_more"], page["next_offset"]) == (2, True, 2)


def test_one_rules_violations():
    linker = DrcLinker(None)
    result = summarize(_demo_report(), linker, rule="clearance constraint (gap=0.3mm)")
    assert [r["count"] for r in result["rules"]] == [0, 1]
    assert result["total"] == 1
    by_kind = summarize(_demo_report(), linker, rule="Clearance")
    assert by_kind["total"] >= 2
    missing = summarize(_demo_report(), linker, rule="no such rule")
    assert missing["error"] == "rule_not_in_report"


# ---------- the tool ----------

@pytest.fixture
def drc_project(monkeypatch, tmp_path):
    project, pcb = _project(tmp_path)
    _put_reports(tmp_path / "Project Outputs for Demo", 2_000_000)
    os.utime(pcb, (2_000_000, 2_000_000))
    index = _index()
    index.pcb.drc_options = DrcOptions(max_violations=500, batch_kinds={15, 16},
                                       make_report=True, include_health=True)
    index.pcb.rules = [_rule("ShortCircuit", 15), _rule("UnRoutedNet", 16)]
    monkeypatch.setattr(main_mod, "_project", {"name": "Demo", "root_dir": str(tmp_path),
                                               "sheets": [], **project})
    monkeypatch.setattr(main_mod._altium, "_netlist", {"nets": {}, "pin_to_net": {}})
    monkeypatch.setattr(main_mod, "_variant_state", VariantState([]))
    monkeypatch.setattr(main_mod._pcb_session, "_load", lambda path, netlist: index)
    main_mod._pcb_session._index = None
    return tmp_path


def test_get_drc_report_tool(drc_project):
    result = json.loads(main_mod.get_drc_report(units="mm"))
    assert result["report"]["file"] == str(Path("Project Outputs for Demo")
                                           / "Design Rule Check - Demo.html")
    assert (result["report"]["format"], result["report"]["made_for"]) == ("html", "Demo.PcbDoc")
    assert result["drc_settings"]["stop_after_violations"] == 500
    assert result["totals"]["rule_violations"] == 25
    assert result["other_reports"] == ["Design Rule Check - Demo.drc"]
    assert "location_mm" in result["groups"][0]["examples"][0]
    assert result["health_issues"][0]["count"] == 2
    assert "not a check of the board as saved now" in result["not_read"]
    assert json.loads(main_mod.get_drc_report(group_by="layer"))["error"] == "invalid_group_by"


def test_get_drc_report_without_a_report(drc_project):
    shutil.rmtree(drc_project / "Project Outputs for Demo")
    result = json.loads(main_mod.get_drc_report())
    assert result["error"] == "no_drc_report"
    assert result["searched"] == [str(drc_project / "Project Outputs for Demo")]


def test_tool_description_mentions_paging():
    assert "next_offset" in main_mod.get_drc_report.__doc__


# ---------- review fixes ----------

def test_incomplete_reports_are_flagged():
    text = HTML.read_text(encoding="utf-8")
    cut = parse_drc_html(text[: text.index("Back to top") + 400])
    assert any("ends early" in problem for problem in cut.problems)
    assert parse_drc_html("").problems == ["the file is empty"]
    drc = TXT.read_text(encoding="utf-8")
    short = parse_drc_txt(drc[: drc.index("Rule Violations :1") - 5])
    assert any("no count line" in problem for problem in short.problems)
    assert any("no 'Violations Detected' footer" in problem for problem in short.problems)
    assert parse_drc_report(HTML).problems == parse_drc_report(TXT).problems == []


def test_an_empty_html_gives_way_to_the_drc(tmp_path):
    project, pcb = _project(tmp_path)
    folder = tmp_path / "Project Outputs for Demo"
    _put_reports(folder, 2_000_000)
    (folder / HTML.name).write_text("", encoding="utf-8")
    os.utime(folder / HTML.name, (2_000_000, 2_000_000))
    report, path, _, _, notes = load_report(project, pcb)
    assert (path.suffix, report.totals["rule_violations"]) == (".drc", 25)
    assert notes[0]["issue"] == "html_report_unusable"
    (folder / TXT.name).unlink()
    report, path, _, _, _ = load_report(project, pcb)
    issues = {i["issue"] for i in assess(report, path, pcb, project)}
    assert "report_incomplete" in issues


def _cell_report(cells: str, section: str = "warning") -> DrcReport:
    return parse_drc_html(
        '<table><tr><th class="column1">Warnings</th></tr></table>'
        f'<a name="IDX"><table><tr><th class="{section}">T</th></tr>{cells}</table></a></html>'
    )


def test_html_cells_keep_their_whole_text():
    callback = '<a href="dxpprocess://x" class="callback"><acronym>{}</acronym></a>'
    row = ('<tr><td class="column1">' + callback.format("Pad J1-1(5mm,5mm) on Multi-Layer")
           + " on Net " + callback.format("GND") + "<br></td></tr>")
    (table,) = _cell_report(row).tables
    assert table.violations[0].message == "Pad J1-1(5mm,5mm) on Multi-Layer on Net GND"
    assert table.violations[0].waiver is None
    waived = ('<tr><td class="column1">' + callback.format("Hole Size Constraint: x")
              + "<br>Waived by K at 1/2/2025<br>mounting post</td></tr>")
    (table,) = _cell_report(waived, "waived").tables
    assert (table.violations[0].message, table.violations[0].waiver) == (
        "Hole Size Constraint: x", "Waived by K at 1/2/2025\nmounting post",
    )
    (table,) = _cell_report('<tr><td class="column1">Plain text row</td></tr>').tables
    assert table.violations[0].message == "Plain text row"


def test_txt_rows_start_only_at_violation_between():
    report = parse_drc_txt(
        "Processing Rule : Silk to Silk (Clearance=0.1mm) (All),(All)\n"
        '   Violation between Silk To Silk Clearance Constraint: Text "A\n'
        '   B" (1mm,1mm) on Top Overlay\n'
        "Rule Violations :1\n\n\nViolations Detected : 1\n"
    )
    (row,) = report.tables[0].violations
    assert row.message.endswith('Text "A\nB" (1mm,1mm) on Top Overlay')
    assert report.problems == []


def test_the_stop_notice_and_a_lowered_limit(tmp_path):
    pcb = _pcb_with([_rule("ShortCircuit", 15), _rule("UnRoutedNet", 16)],
                    DrcOptions(max_violations=2000, batch_kinds={15, 16}))
    report = _report(*ALL_CHECKED)
    report.tables.append(DrcTable(type="warning", title="More than 500 violations detected, "
                                  "DRC was stopped", count=0))
    limit = _assess(tmp_path, report, pcb)["violation_limit_reached"]
    assert "the board's limit is now 2000" in limit["detail"]
    pcb.drc_options.max_violations = 30
    lowered = _report(_table("Width Constraint (All)", 600, ["x"] * 600), *ALL_CHECKED)
    assert _assess(tmp_path, lowered, pcb)["violation_limit_lowered"]["severity"] == "info"


def test_only_this_boards_eco_logs_count(tmp_path):
    pcb = _pcb_with([_rule("ShortCircuit", 15), _rule("UnRoutedNet", 16)],
                    DrcOptions(batch_kinds={15, 16}))
    logs = tmp_path / "Project Logs for Demo"
    logs.mkdir()
    for name in ("Daughter PCB ECO 1-2-2025 10-00-00.LOG", "Demo SCH ECO 1-2-2025.LOG"):
        (logs / name).write_text("x", encoding="utf-8")
        os.utime(logs / name, (2_100_000, 2_100_000))
    assert "eco_after_report" not in _assess(tmp_path, _report(*ALL_CHECKED), pcb)
    (logs / "Demo PCB ECO 1-2-2025 10-00-00.LOG").write_text("x", encoding="utf-8")
    os.utime(logs / "Demo PCB ECO 1-2-2025 10-00-00.LOG", (2_100_000, 2_100_000))
    eco = _assess(tmp_path, _report(*ALL_CHECKED), pcb)["eco_after_report"]
    assert eco["detail"].startswith("1 Update PCB (ECO) run logged")


def test_a_report_made_for_a_same_named_board_elsewhere(tmp_path):
    elsewhere = tmp_path / "RevA"
    elsewhere.mkdir()
    (elsewhere / "Demo.PcbDoc").write_bytes(b"old")
    pcb = _pcb_with([_rule("ShortCircuit", 15), _rule("UnRoutedNet", 16)],
                    DrcOptions(batch_kinds={15, 16}))
    report = _report(*ALL_CHECKED, pcb_file=str(elsewhere / "Demo.PcbDoc"))
    assert "RevA" in _assess(tmp_path, report, pcb)["other_board"]["detail"]


def test_a_report_found_outside_the_output_folder(tmp_path):
    project, pcb = _project(tmp_path)
    _put_reports(tmp_path / "Archive" / "Project Outputs for Old", 2_000_000)
    _, path, _, _, notes = load_report(project, pcb)
    assert path is not None and notes[-1]["issue"] == "report_outside_output_folder"


def test_rules_changed_ignores_kinds_the_report_cannot_name(tmp_path):
    pcb = _pcb_with(
        [_rule("ShortCircuit", 15), _rule("UnRoutedNet", 16), _rule("AcuteAngle", 21)],
        DrcOptions(batch_kinds={15, 16, 21}),
    )
    report = _report(*ALL_CHECKED, _table("Acute Angle Constraint (Minimum=90) (All)"))
    assert "rules_changed" not in _assess(tmp_path, report, pcb)
    assert rule_kind("Acute Angle Constraint (Minimum=90) (All)") == ("AcuteAngle", None)


SHORT = (
    "Short-Circuit Constraint: Between Pad U1-5(12.7mm,12.7mm) on Multi-Layer And "
    "Polygon Region (2 hole(s)) Bottom Layer Location : [X = 50mm][Y = 1mm]"
)


def test_shorts_ignore_the_board_sized_box():
    index = _index()
    index.pcb.layer_name_to_v6["bottom layer"] = 32
    index.pcb.board.outline_vertices = [(1000, 1000), (3000, 1000), (3000, 3000), (1000, 3000)]
    pour = PolygonPour(  # a GND pour on Bottom Layer around U1-5 (on CLK)
        pour_id=0, net=0, layer=32, hatch_style="Solid",
        vertices=[(1400, 1400), (1600, 1400), (1600, 1600), (1400, 1600)],
    )
    index.pcb.pours = [pour]
    box = (-125000.0, -125000.0, 125000.0, 125000.0)  # Altium's board-sized box for shorts
    linked = DrcLinker(index).link(SHORT, box, "ShortCircuit")
    assert linked["location_mil"] == {"x_mil": 1500.0, "y_mil": 1500.0}
    assert linked["nets"] == ["CLK", "GND"]  # the pad's net and the pour's
    pour.shelved = True  # no copper: it cannot be the polygon in the short
    assert DrcLinker(index).link(SHORT, box, "ShortCircuit")["nets"] == ["CLK"]
    pour.shelved = False
    pour.vertices = [(2400, 2400), (2600, 2400), (2600, 2600), (2400, 2600)]
    assert DrcLinker(index).link(SHORT, box, "ShortCircuit")["nets"] == ["CLK"]


def test_clearance_to_a_polygon_takes_the_pours_net():
    index = _index()
    index.pcb.pours = [PolygonPour(
        pour_id=0, net=0, layer=1, hatch_style="Solid",
        vertices=[(1600, 1400), (1800, 1400), (1800, 1600), (1600, 1600)],
    )]
    linked = DrcLinker(index).link(
        "Clearance Constraint: (0.1mm < 0.2mm) Between Polygon Region (0 hole(s)) Top Layer "
        "And Pad U1-5(12.7mm,12.7mm) on Top Layer", (1500.0, 1495.0, 1602.0, 1505.0),
        "Clearance",
    )
    assert linked["nets"] == ["CLK", "GND"]


def test_layer_names_need_a_boundary():
    index = _index()
    linker = DrcLinker(index)
    linker.layers = sorted([("L1", 1), ("L10", 10)], key=lambda item: -len(item[0]))
    assert linker._named_layers("Track (1mm,1mm)(2mm,1mm) on L10 Actual Width = 1mm") == {10}
    assert linker._named_layers("Track (1mm,1mm)(2mm,1mm) on L1 Actual Width = 1mm") == {1}


def test_stop_notice_is_altiums_wording(tmp_path):
    pcb = _pcb_with([_rule("ShortCircuit", 15), _rule("UnRoutedNet", 16)],
                    DrcOptions(max_violations=500, batch_kinds={15, 16}))
    report = _report(*ALL_CHECKED)
    report.tables.append(DrcTable(
        type="warning", title="Unplated multi-layer pad(s) detected", count=1,
        violations=[DrcViolation("Pad MH1-1(5mm,5mm) on Multi-Layer on Net MOTOR_STOPPED")],
    ))
    assert "violation_limit_reached" not in _assess(tmp_path, report, pcb)


def test_an_incomplete_report_names_no_other_cause(tmp_path):
    pcb = _pcb_with([_rule("ShortCircuit", 15), _rule("UnRoutedNet", 16), _rule("Width", 2)],
                    DrcOptions(batch_kinds={2, 15, 16}))
    report = parse_drc_html("")
    issues = _assess(tmp_path, report, pcb)
    assert issues["report_incomplete"]["severity"] == "error"
    assert "rules_changed" not in issues and "no_health_section" not in issues
    assert "may be in the missing part" in issues["rule_kind_not_checked"]["detail"]
    counts = _report(*ALL_CHECKED)
    counts.problems = ["1 table lists a different number of rows than its count (e.g. X)"]
    issues = _assess(tmp_path, counts, pcb)
    assert "report_incomplete" not in issues
    assert issues["report_counts_differ"]["severity"] == "warning"
    counts.problems = ["3 tables list a different number of rows than its count (e.g. X)"]
    issues = _assess(tmp_path, counts, pcb)
    assert "report_incomplete" not in issues and "report_counts_differ" in issues


def test_box_nets_follow_the_named_layer():
    index = _index()
    linker = DrcLinker(index)
    on_top = linker.link("Width Constraint: Track (12.7mm,12.7mm)(13mm,12.7mm) on Top Layer",
                         (1495.0, 1495.0, 1505.0, 1505.0), "Width")
    on_bottom = linker.link("Width Constraint: Track (12.7mm,12.7mm)(13mm,12.7mm) on "
                            "Bottom Layer", (1495.0, 1495.0, 1505.0, 1505.0), "Width")
    assert on_top["nets"] == ["CLK"] and "nets" not in on_bottom


def test_diff_pair_rows_link_their_nets():
    index = _index()
    index.pcb.diff_pairs = [DiffPair("USB", "USB_P", "USB_N", None)]
    linked = DrcLinker(index).link(
        "Matched Net Lengths: Differential Pair USB Actual Length Difference against diff "
        "pair USB is: 0.5mm, Length Tolerance : 0.1mm.", None, "MatchedLengths",
    )
    assert (linked["diff_pairs"], linked["nets"]) == (["USB"], ["USB_P", "USB_N"])


def test_rules_sharing_a_description_stay_apart():
    report = _report(_table("Width Constraint (All)", 2, ["a", "b"]),
                     _table("Width Constraint (All)", 1, ["c"]))
    groups = summarize(report, DrcLinker(None))["groups"]
    assert [(g["rule"], g["count"]) for g in groups] == [
        ("Width Constraint (All)", 2), ("Width Constraint (All)", 1),
    ]
    assert all("reported_count" not in g for g in groups)


def test_sections_say_what_they_omit_and_can_be_listed():
    report = _report(*ALL_CHECKED)
    report.tables.append(DrcTable(type="waived", title="Hole Size Constraint (All)", count=25,
                                  violations=[DrcViolation(f"w{i}") for i in range(25)]))
    (waived,) = other_sections(report, DrcLinker(None))["waived"]
    assert (len(waived["examples"]), waived["omitted"]) == (20, 5)
    listed = summarize(report, DrcLinker(None), rule="Hole Size")
    assert (listed["total"], listed["violations"][0]["section"]) == (25, "waived")


def test_tool_without_the_pcbdoc(drc_project):
    (drc_project / "Demo.PcbDoc").unlink()
    result = json.loads(main_mod.get_drc_report(group_by="part"))
    issues = {issue["issue"] for issue in result["assessment"]}
    assert "pcb_file_missing" in issues and result["pcb_saved_utc"] is None
    assert "grouped by rule instead" in result["grouping_note"]
    assert result["groups"][0]["rule"].startswith("Clearance Constraint")


def test_tool_trims_to_fit_and_reports_the_origin(drc_project, monkeypatch):
    result = json.loads(main_mod.get_drc_report(group_by="part"))
    assert result["origin_mil"] == {"x_mil": 1000.0, "y_mil": 1000.0}
    assert "grouping_note" in result
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 9000)
    trimmed = json.loads(main_mod.get_drc_report())
    assert trimmed["trimmed_to"] in (15, 5) and len(trimmed["groups"]) <= 15
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.get_drc_report())["error"] == "no_project"
