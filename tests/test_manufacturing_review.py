"""PCB manufacturing review prompt, typical DFA guidance and board snapshot."""

import asyncio
import json
import re
import shutil
import string
from copy import deepcopy
from pathlib import Path

import pytest

import server.main as main_mod
from server.parsers.pcb_doc import PcbLayer, parse_pcb_doc
from server.services import dfm
from server.services.fab_profiles import ALTIMADE
from server.services.manufacturing_review import (
    ASSEMBLY_CHECKS,
    DFA_GUIDANCE,
    FAB_CHECKS,
    MANUFACTURING_REVIEW_NETLIST_FREE_NOTE,
    MANUFACTURING_REVIEW_TOOLS,
    PCB_MANUFACTURING_REVIEW_PROMPT,
    SNAPSHOT_MAX_FILES,
    build_manufacturing_review,
    manufacturing_snapshot,
    profile_lines,
    render_dfa_guidance,
)
from server.services.pcb_index import PcbIndex
from tests.test_pcb_index import NETLIST, pcb_data  # noqa: F401

EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")
TEMPLATE = Path(__file__).parent.parent / "examples" / "fab_profile.json"


@pytest.fixture
def project(tmp_path):
    return {
        "name": "Demo",
        "root_dir": str(tmp_path),
        "prj_pcb_path": str(tmp_path / "Demo.PrjPcb"),
        "sheets": [],
        "pcb_doc_paths": [str(tmp_path / "Board.PcbDoc")],
    }


@pytest.fixture
def index(pcb_data):
    return PcbIndex(pcb_data, NETLIST)


def _prompt(index, project, variant="Prod", **kwargs):
    return build_manufacturing_review(project, variant, index, **kwargs)


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _write(folder, name: str, data) -> None:
    (Path(folder) / name).write_text(json.dumps(data), encoding="utf-8")


# ---------- prompt structure ----------


def test_prompt_placeholders_are_exactly_the_build_arguments():
    fields = {
        field
        for _, field, _, _ in string.Formatter().parse(PCB_MANUFACTURING_REVIEW_PROMPT)
        if field is not None
    }
    assert fields == {
        "name", "pcb_doc", "active_variant", "root_dir", "snapshot", "fab_checks",
        "assembly_checks", "dfa_guidance",
    }


def test_phases_and_gates(index, project):
    prompt = _prompt(index, project)
    for phase in ("Phase 1", "Phase 2", "Phase 3", "Phase 4"):
        assert f"### {phase} — " in prompt
    assert "Do not proceed until the user answers." in prompt
    assert prompt.count("Done when:") == 3
    for gate in ("Phase 2", "Phase 3", "Phase 4"):
        assert f"Do not proceed to {gate} until then." in prompt
    # The fab question comes before any phase.
    ask = prompt.index("Which fabricator")
    assert ask < prompt.index("Do not proceed until the user answers.") < prompt.index(
        "### Phase 1")
    for header in (
        "Table 1 — Fabrication (profile: name and source)",
        "| Check | Status | Limit | Board worst | Evidence |",
        "Table 2 — Assembly",
        "| Check | Status | Limit | Board value | Evidence |",
        "Table 3 — Blockers & Questions for the Fab / Assembler",
        "| Item | Severity | Issue | Fix or question |",
        "Table 4 — Not Checked",
        "| Item | Why | How to confirm |",
    ):
        assert header in prompt


def test_walks_fabrication_then_assembly_then_verdict(index, project):
    prompt = _prompt(index, project)
    phase2, phase3, phase4 = (prompt.index(f"### Phase {n}") for n in (2, 3, 4))
    fab = prompt.index("check_dfm(profile=..., checks=" + '["trace_width"')
    steps = [fab] + [prompt.index(tool, phase2) for tool in (
        "check_vias:", "check_pcb_health:", "check_connectivity:", "get_drc_report:")]
    assert phase2 < fab and steps == sorted(steps) and steps[-1] < phase3
    assembly = prompt.index("check_dfm(profile=..., checks=" + '["part_counts"')
    assert phase3 < assembly < prompt.index("get_bom", phase3) < prompt.index(
        "check_bom", phase3) < phase4
    flat = _flat(prompt[phase4:])
    for verdict in ("GO (no fail, nothing blocking)", "GO WITH CONDITIONS", "NO-GO"):
        assert verdict in flat
    assert "Never a GO on checks that did not run." in flat
    assert "Table 4 lists every not_checked entry the tools returned" in flat


def test_ground_rules(index, project):
    flat = _flat(_prompt(index, project))
    assert "Never present an Altimade result as another fab's." in flat
    assert "never call the board manufacturable in general" in flat
    assert 'profile="none"' in flat and '{"min_spacing_mm": 0.1, "max_aspect_ratio": 8}' in flat
    assert "Never invent a limit or change its unit." in flat
    assert "Altimade's published limits, not your fab's" in flat
    for status in ("fail (a miss of severity error or warning)", "marginal", "advisory",
                   "not_checked carries its reason"):
        assert status in flat
    assert "A check that did not run is not a pass." in flat
    assert "board_minimums is what the board needs from any fab" in flat
    assert "add origin_mil before query_pcb_region" in flat
    assert "get_drc_report is Altium's last batch DRC, not a check of the board as saved" in flat
    assert "Work in the variant confirmed in Phase 1 (set_active_variant when it is not Prod)"         in flat and "Not Fitted parts are not placed" in flat
    # Typical guidance is compared in the report, never passed as a limit or a fail.
    assert "never pass a typical value as a check_dfm limit, and never count a miss of one "         "as a fail" in flat
    assert "never passed as a check_dfm limit" in flat
    assert "(Ctrl+S)" in flat


def test_every_check_is_named_for_its_phase(index, project):
    prompt = _prompt(index, project)
    phase2, phase3 = prompt.index("### Phase 2"), prompt.index("### Phase 3")
    fab = '["' + '", "'.join(FAB_CHECKS) + '"]'
    assembly = '["' + '", "'.join(ASSEMBLY_CHECKS) + '"]'
    assert phase2 < prompt.index(fab) < phase3 < prompt.index(assembly)
    # The assembly limits the prompt asks for are profile keys.
    from server.services.fab_profiles import LIMIT_KEYS
    for key in ("min_part_spacing", "min_part_to_edge", "min_global_fiducials",
                "local_fiducial_pitch", "min_pitch", "max_placements", "max_unique_parts"):
        assert key in LIMIT_KEYS and key in prompt


def test_fab_checks_are_check_dfm_checks():
    assert not set(FAB_CHECKS) & set(ASSEMBLY_CHECKS)
    assert set(FAB_CHECKS) <= set(dfm.VALID_CHECKS)
    assert [name for name in dfm.VALID_CHECKS if name in FAB_CHECKS] == list(FAB_CHECKS)


def test_assembly_checks_are_check_dfm_checks_once_integrated():
    if not set(ASSEMBLY_CHECKS) & set(dfm.VALID_CHECKS):
        pytest.skip("check_dfm's assembly checks are not integrated yet")
    assert set(FAB_CHECKS) | set(ASSEMBLY_CHECKS) == set(dfm.VALID_CHECKS)


def test_prompt_offers_no_save_tool():
    # The template and the note, not a rendered prompt: tmp_path names hold the test name.
    for text in (PCB_MANUFACTURING_REVIEW_PROMPT, MANUFACTURING_REVIEW_NETLIST_FREE_NOTE,
                 render_dfa_guidance()):
        assert "save_" not in text
        assert "markdown file" not in text.lower()


def test_context_lines(index, project):
    prompt = _prompt(index, project)
    assert "Project: Demo" in prompt
    assert "PCB: Board.PcbDoc" in prompt
    assert "Active variant: Prod" in prompt
    assert f'set_project_dir("{project["root_dir"]}")' in prompt
    # Sub-agents set the variant the user confirmed, not the one active when it was built.
    assert "set_active_variant with the variant confirmed in Phase 1" in _flat(prompt)
    assert 'set_active_variant("Prod")' not in prompt
    assert MANUFACTURING_REVIEW_NETLIST_FREE_NOTE not in prompt


def test_multiple_pcb_docs_are_named(index, project):
    project = dict(project, pcb_doc_paths=["a/Main.PcbDoc", "b/Panel.PcbDoc"])
    prompt = _prompt(index, project)
    assert "PCB: Main.PcbDoc (the project lists 2 PcbDocs; only this one is reviewed)" in prompt
    assert "PCB: (none)" in _prompt(index, dict(project, pcb_doc_paths=[]))


def test_netlist_free_note(index, project):
    prompt = _prompt(index, project, netlist_free=True)
    assert prompt.endswith(MANUFACTURING_REVIEW_NETLIST_FREE_NOTE)
    flat = _flat(MANUFACTURING_REVIEW_NETLIST_FREE_NOTE)
    assert "get_bom, check_bom and check_sch_pcb_sync answer netlist_required" in flat
    assert "unique-part count is approximate" in flat
    assert "the BOM and the schematic sync were not checked" in flat


def test_format_safe_with_braces(index, project, tmp_path):
    _write(tmp_path, "fab_profile.json", {"{bad}": 1})
    project = dict(project, name="Demo {0} {name}", root_dir=str(tmp_path / "{x}"))
    (tmp_path / "{x}").mkdir()
    _write(tmp_path / "{x}", "fab_profile.json", {"{bad}": 1})
    prompt = build_manufacturing_review(project, "V{1}", index)
    assert "Project: Demo {0} {name}" in prompt
    assert "Active variant: V{1}" in prompt
    assert f'set_project_dir("{tmp_path / "{x}"}")' in prompt
    assert "unknown key '{bad}'" in prompt


def test_prompt_size_is_bounded(index, project):
    assert len(_prompt(index, project, netlist_free=True)) < 16000


# ---------- snapshot ----------


def test_snapshot_bare_board(index, project):
    lines = manufacturing_snapshot(index, project["root_dir"]).splitlines()
    assert all(line.startswith("- ") for line in lines)
    assert lines[:3] == [
        "- Copper layers: 4",
        "- Board outline: none in the PcbDoc, so check_dfm cannot measure the board size "
        "or any edge clearance",
        "- Components on the PCB: 3 (2 top, 1 bottom; every kind, Not Fitted parts "
        "included: check_dfm part_counts counts the fitted placements)",
    ]
    assert lines[3].startswith("- Fab profile: no fab_profile.json in the project folder")
    assert len(lines) == 4  # no panel, no other files


def test_snapshot_planes_outline_panel_and_no_parts(pcb_data, project):
    data = deepcopy(pcb_data)
    data.board.layers.append(PcbLayer(39, "GND-PLANE", "copper", 5))
    data.board.outline_vertices = [(0.0, 0.0), (1000.0, 0.0), (1000.0, 800.0)]
    data.embedded_boards = 4
    data.components = []
    snapshot = manufacturing_snapshot(PcbIndex(data, NETLIST), project["root_dir"])
    assert "- Copper layers: 5 (1 internal plane among them)" in snapshot
    assert "- Board outline: defined (get_board_info gives its size)" in snapshot
    assert "- Components on the PCB: none" in snapshot
    assert "- Embedded boards: 4 (this PcbDoc is a panel;" in snapshot


def test_profile_line_default(tmp_path):
    for root in (None, "", str(tmp_path)):
        lines = profile_lines(root)
        assert lines == [
            "- Fab profile: no fab_profile.json in the project folder, so check_dfm "
            "defaults to Altimade's published limits (Altium's ordering service), not the "
            "user's fabricator"
        ]


def test_profile_line_project_file(tmp_path):
    _write(tmp_path, "fab_profile.json", {"name": "Secret Fab Name", "source": "secret url",
                                          "limits": {"min_trace_mm": 0.1,
                                                     "min_spacing_mm": 0.1}})
    lines = profile_lines(tmp_path)
    # Its own two limits (min_hole_to_copper only follows min_spacing).
    assert lines == ["- Fab profile: the project's fab_profile.json (2 limits) is check_dfm's "
                     "default; confirm with the user that it is the fabricator building "
                     "this board"]
    # The file's own name and source are user text: never in the prompt.
    assert "secret" not in "\n".join(lines).lower()
    _write(tmp_path, "fab_profile.json", {"based_on": "altimade",
                                          "limits": {"min_pitch_mm": 0.4}})
    assert ("(1 limit of its own on top of Altimade's published limits) is check_dfm's "
            "default") in profile_lines(tmp_path)[0]
    _write(tmp_path, "fab_profile.json", {"based_on": "altimade"})
    assert "(0 limits of its own on top of Altimade's" in profile_lines(tmp_path)[0]


def test_profile_line_blank_template(tmp_path):
    shutil.copy(TEMPLATE, tmp_path / "fab_profile.json")
    line = profile_lines(tmp_path)[0]
    assert "sets no limit yet (every value is null)" in line
    assert "checks nothing against it until the fab's numbers are filled in" in line


def test_profile_line_invalid_file(tmp_path):
    (tmp_path / "fab_profile.json").write_text('{"limits": {"min_trace_mm" 0.1}}',
                                               encoding="utf-8")
    line = profile_lines(tmp_path)[0]
    assert line.startswith("- Fab profile: the project's fab_profile.json cannot be used, "
                           "so check_dfm answers invalid_profile until it is fixed: "
                           "fab_profile.json: not valid JSON at line 1, column 28")
    assert "\n" not in line


def test_other_json_files_are_listed_and_capped(tmp_path):
    _write(tmp_path, "fab_profile.json", {"limits": {"min_trace_mm": 0.1}})
    _write(tmp_path, "acme.json", {})
    lines = profile_lines(tmp_path)
    assert lines[1] == ("- Other .json files in the project folder (1; a fab profile only if "
                        "the user says so, passed by name as profile): acme.json")
    for number in range(SNAPSHOT_MAX_FILES + 3):
        _write(tmp_path, f"z{number}.json", {})
    line = profile_lines(tmp_path)[1]
    assert line.startswith("- Other .json files in the project folder (9;")
    assert line.endswith(": acme.json, z0.json, z1.json, z2.json, z3.json, +4 more")
    assert "fab_profile.json" not in line


# ---------- DFA guidance ----------


def test_dfa_guidance_complete_and_sourced():
    keys = {"topic", "guidance", "source"}
    for row in DFA_GUIDANCE:
        assert set(row) == keys
        assert all(str(row[key]).strip() for key in keys), row["topic"]
        assert len(row["source"].strip()) >= 5, row["topic"]
    assert len({row["topic"] for row in DFA_GUIDANCE}) == len(DFA_GUIDANCE)
    topics = " / ".join(row["topic"] for row in DFA_GUIDANCE)
    for topic in ("Global fiducials", "Local fiducials", "Part to board edge", "Part to part",
                  "Tombstoning", "Altimade"):
        assert topic in topics
    # Altimade's counts come from the profile module, as published.
    altimade = next(row for row in DFA_GUIDANCE if row["topic"] == "Altimade assembly")
    limits = ALTIMADE["limits"]
    assert f"At most {limits['max_unique_parts']['published']} unique components" in (
        altimade["guidance"])
    assert f"and {limits['max_placements']['published']} placements" in altimade["guidance"]
    assert "200" in altimade["guidance"] and "600" in altimade["guidance"]
    assert ALTIMADE["source_updated"] in altimade["source"]


def test_render_dfa_guidance_is_a_valid_table():
    lines = render_dfa_guidance().splitlines()
    assert lines[0] == "| Topic | Guidance | Source |"
    assert lines[1] == "|---|---|---|"
    assert len(lines) == 2 + len(DFA_GUIDANCE)
    for line, row in zip(lines[2:], DFA_GUIDANCE):
        assert line.startswith(f"| {row['topic']} |") and line.count("|") == 4


def test_guidance_is_in_the_prompt_as_fallback(index, project):
    prompt = _prompt(index, project)
    assert ("## Typical DFA Guidance (fallback only — the assembler's own numbers decide)"
            in prompt)
    assert render_dfa_guidance() in prompt
    assert prompt.index("### Phase 4") < prompt.index("## Typical DFA Guidance")


# ---------- tool references ----------


REQUIRED_TOOLS = (
    "check_dfm", "check_vias", "check_pcb_health", "get_drc_report", "get_bom", "check_bom",
)


def test_tools_tuple_matches_prompt(index, project):
    prompt = _prompt(index, project, netlist_free=True)
    assert set(REQUIRED_TOOLS) <= set(MANUFACTURING_REVIEW_TOOLS)
    assert len(set(MANUFACTURING_REVIEW_TOOLS)) == len(MANUFACTURING_REVIEW_TOOLS)
    for tool in MANUFACTURING_REVIEW_TOOLS:
        assert re.search(rf"\b{tool}\b", PCB_MANUFACTURING_REVIEW_PROMPT), tool
    # Anything written as a call in the prompt is a tracked tool.
    called = set(re.findall(r"\b([a-z]+(?:_[a-z]+)+)\(", prompt))
    assert called <= set(MANUFACTURING_REVIEW_TOOLS), called - set(MANUFACTURING_REVIEW_TOOLS)


def test_registered_tools_named_in_prompt_are_tracked(index, project):
    registered = {tool.name for tool in asyncio.run(main_mod.mcp.list_tools())}
    prompt = _prompt(index, project, netlist_free=True)
    named = {tool for tool in registered if re.search(rf"\b{tool}\b", prompt)}
    assert named <= set(MANUFACTURING_REVIEW_TOOLS), named - set(MANUFACTURING_REVIEW_TOOLS)


def test_prompt_tools_are_registered():
    registered = {tool.name for tool in asyncio.run(main_mod.mcp.list_tools())}
    assert set(MANUFACTURING_REVIEW_TOOLS) <= registered


# ---------- Altium's examples ----------


def test_snapshot_on_an_altium_example(tmp_path):
    path = next(EXAMPLES.rglob("Bluetooth_Sentinel.PcbDoc"), None) if EXAMPLES.is_dir() else None
    if path is None:
        pytest.skip("Altium example boards not installed")
    index = PcbIndex(parse_pcb_doc(str(path)), None)
    project = {"name": "Bluetooth_Sentinel", "root_dir": str(tmp_path),
               "pcb_doc_paths": [str(path)]}
    prompt = build_manufacturing_review(project, "Default", index, netlist_free=True)
    snapshot = manufacturing_snapshot(index, str(tmp_path))
    assert snapshot in prompt
    assert "- Board outline: defined" in snapshot
    assert re.search(r"- Copper layers: \d+", snapshot)
    assert re.search(r"- Components on the PCB: \d+ \(\d+ top", snapshot)
