"""Project tour prompt: phases, load-mode variants, the primer outline and tool names."""

import asyncio
import json
import re
import string
from pathlib import Path
from unittest.mock import patch

import pytest

import server.main as main_mod
from server.parsers.prj_pcb import VariantDefinition, VariantState, parse_prj_pcb
from server.services.constraints_review import CONSTRAINTS_REVIEW_PROMPT
from server.services.manufacturing_review import PCB_MANUFACTURING_REVIEW_PROMPT
from server.services.project_tour import (
    MAX_SHEETS_WITHOUT_ASKING,
    PHASE_2_NETLIST_FREE,
    PHASE_2_SCHEMATIC,
    PHASE_3_NO_PCB,
    PHASE_3_PCB,
    PRIMER_SECTIONS,
    PROJECT_TOUR_PROMPT,
    PROJECT_TOUR_TOOLS,
    build_project_tour,
    defined_variant_count,
    primer_path,
)

EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")
MAX_PROMPT_CHARS = 7000

MODES = {
    "full": {"netlist_free": False, "pcb": True},
    "netlist_free": {"netlist_free": True, "pcb": True},
    "no_pcb": {"netlist_free": False, "pcb": False},
    "netlist_free_no_pcb": {"netlist_free": True, "pcb": False},
    # A PCB-only project: it loads netlist-free, and Altium's full load refuses it.
    "no_sheets": {"netlist_free": True, "pcb": True, "sheets": False},
}


@pytest.fixture
def project(tmp_path):
    return {
        "name": "Demo",
        "root_dir": str(tmp_path),
        "prj_pcb_path": str(tmp_path / "Demo.PrjPcb"),
        "sheets": [{"name": "Top", "path": "Top.SchDoc"}, {"name": "Power", "path": "Power.SchDoc"}],
        "pcb_doc_paths": [str(tmp_path / "Board.PcbDoc")],
        "pcb_dwf_paths": [str(tmp_path / "Fab.PCBDwf")],
    }


def _tour(project, mode="full", variant="Prod", count=2):
    settings = MODES[mode]
    if not settings["pcb"]:
        project = dict(project, pcb_doc_paths=[])
    if not settings.get("sheets", True):
        project = dict(project, sheets=[])
    return build_project_tour(project, variant, count, netlist_free=settings["netlist_free"])


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _named(text: str, names) -> set[str]:
    return {name for name in names if re.search(rf"\b{name}\b", text)}


# ---------- structure ----------


def test_prompt_placeholders_are_exactly_the_build_arguments():
    fields = {field for _, field, _, _ in string.Formatter().parse(PROJECT_TOUR_PROMPT)
              if field is not None}
    assert fields == {
        "name", "root_dir", "load", "sheet_count", "pcb_doc", "drawings", "variants",
        "active_variant", "phase_2", "phase_3", "phase_4", "variants_step",
        "primer_sections", "primer_path",
    }


def test_context_lines(project):
    prompt = _tour(project)
    assert prompt.startswith("Project: Demo\n")
    for line in (f"Folder: {project['root_dir']}", "Load: full (compiled netlist)",
                 "Schematic sheets: 2", "PCB: Board.PcbDoc", "Draftsman drawings: 1",
                 "Variants: 2 (active: Prod)"):
        assert line + "\n" in prompt, line
    assert ("Variants: none defined in the .PrjPcb (the base design, shown as 'Default')"
            in _tour(project, variant="Default", count=0))


def test_defined_variant_count():
    stand_in = VariantDefinition(name="Default")
    assert defined_variant_count([stand_in]) == 0
    assert defined_variant_count([VariantDefinition(name="Default", dnp_refdes=["R1"])]) == 1
    assert defined_variant_count([VariantDefinition(name="Prod")]) == 1
    assert defined_variant_count([stand_in, VariantDefinition(name="Lite")]) == 2
    assert defined_variant_count([]) == 0
    # A real 'Default' variant that only varies a parameter or a stale UniqueId counts.
    assert defined_variant_count([VariantDefinition(
        name="Default", param_variations={"R1": {"value": ("Value", "10k")}})]) == 1
    assert defined_variant_count([VariantDefinition(name="Default", dnp_uid=["\\A\\B"])]) == 1


def test_multiple_pcb_docs_are_named(project):
    project = dict(project, pcb_doc_paths=["a/Main.PcbDoc", "b/Panel.PcbDoc"])
    assert ("PCB: Main.PcbDoc (the project lists 2 PcbDocs; the board tools read this one)"
            in _tour(project))
    assert "PCB: none (the project lists no PcbDoc)" in _tour(project, "no_pcb")


def test_asks_before_starting(project):
    prompt = _tour(project)
    ask = prompt.index("Before starting, ask the user")
    assert ask < prompt.index("Do not proceed until the user answers.") < prompt.index(
        "### Phase 1")


@pytest.mark.parametrize("mode", sorted(MODES))
def test_phases_in_altiums_teaching_order(project, mode):
    prompt = _tour(project, mode)
    heads = [prompt.index(f"### Phase {n} — ") for n in range(1, 6)]
    assert heads == sorted(heads)
    assert heads[-1] < prompt.index("## Project Primer")


def test_full_tour_calls_tools_in_order(project):
    prompt = _tour(project)
    order = ["get_design_hierarchy", "list_variants", "get_sheet_context", "query_net",
             "get_board_info", "get_component_placement", "list_diff_pairs", "get_net_pcb",
             "check_lengths", "get_drc_report", "check_pcb_health", "get_bom",
             "get_design_notes (every page)"]
    positions = [prompt.index(name, prompt.index("### Phase 1")) for name in order]
    assert positions == sorted(positions)
    phases = [prompt.index(f"### Phase {n}") for n in range(1, 6)] + [len(prompt)]
    expected_phase = {"get_design_hierarchy": 1, "list_variants": 1, "get_sheet_context": 2,
                      "query_net": 2, "get_board_info": 3, "check_lengths": 3,
                      "get_drc_report": 4, "check_pcb_health": 4, "get_bom": 4,
                      "get_design_notes (every page)": 5}
    for name, phase in expected_phase.items():
        at = prompt.index(name, phases[0])
        assert phases[phase - 1] < at < phases[phase], name


def test_power_tree_is_a_derived_reading(project):
    flat = _flat(_tour(project))
    assert "no tool builds a power tree" in flat.lower()
    assert "| Rail | Source (designator, sheet) | Feeds (sheets) | Evidence |" in flat
    assert ('section 4 a table | Rail | Source | Feeds | Evidence |, marked "derived, not a '
            'tool result"') in flat
    assert "Section 3 is a table | Sheet | Purpose | Key parts | Interfaces |" in flat
    assert f"more than {MAX_SHEETS_WITHOUT_ASKING} sheets: ask which blocks to cover first" in flat


def test_ground_rules(project):
    flat = _flat(_tour(project))
    assert "State only what the tools return, and cite it: sheet names, designators" in flat
    assert "Never fill a gap from memory." in flat
    assert 'Mark every inference "Assumption:" with its evidence' in flat
    assert 'mark that item "not covered"' in flat
    assert "Work in the active variant (Prod)" in flat
    assert "Read-only: the tour changes no design file." in flat
    assert "pause there if the user asked to" in flat
    assert "Reuse results already in this conversation" in flat
    assert "Then get_output_jobs: the OutJobs, what their connected outputs cover" in flat
    assert "quote its assessment first" in flat


# ---------- load-mode variants ----------


def test_full_tour_has_no_netlist_free_or_skip_text(project):
    prompt = _tour(project)
    assert PHASE_2_SCHEMATIC in prompt
    assert "netlist_required" not in prompt and "(skipped" not in prompt
    assert "netlist-free" not in prompt.lower()
    assert "ask the user)" not in prompt  # key parts come from Phase 2
    assert "get_bom(compare_variants=True)" in prompt


def test_netlist_free_text(project):
    prompt = _tour(project, "netlist_free")
    flat = _flat(prompt)
    assert "Load: netlist-free (no netlist: Altium Designer was not running)" in prompt
    assert PHASE_2_NETLIST_FREE.splitlines()[0] == (
        "### Phase 2 — Schematic Blocks (limited: netlist-free load)")
    assert PHASE_2_NETLIST_FREE.splitlines()[0] in prompt
    assert PHASE_2_SCHEMATIC not in prompt
    assert ("get_sheet_context, query_net and get_bom answer netlist_required: no parts, "
            "pins or rails come from the schematic") in flat
    assert "get_design_hierarchy(sheet=...)" in flat and "get_design_notes(document=...)" in flat
    assert "name the rail nets the board shows (get_classes net classes, get_net_pcb" in flat
    assert "as PCB data only" in flat
    assert "the PCB holds no part descriptions in this load: ask the user" in flat
    assert "get_bom needs the netlist: the BOM is not covered in this load." in flat
    assert "get_bom(compare_variants=True)" not in flat
    assert "list_variants' Not Fitted designators, stored in the .PrjPcb" in flat
    # The netlist tools appear only as the ones that answer netlist_required.
    phase_2 = prompt[prompt.index("### Phase 2"):prompt.index("### Phase 3")]
    assert "get_sheet_context(" not in phase_2 and "query_net (" not in phase_2


def test_no_pcb_text(project):
    prompt = _tour(project, "no_pcb")
    assert PHASE_3_NO_PCB in prompt and PHASE_3_PCB.splitlines()[0] + "\n" not in prompt
    for tool in ("get_board_info", "get_component_placement", "list_diff_pairs",
                 "get_net_pcb", "check_lengths", "get_drc_report", "check_pcb_health"):
        assert not re.search(rf"\b{tool}\b", prompt), tool
    flat = _flat(prompt)
    assert "Without a PcbDoc there is no DRC report or board health to read: say so." in flat
    assert "1. get_bom (active variant)" in flat
    assert "pcb_manufacturing_review" in flat  # where it applies, in section 11


@pytest.mark.parametrize("mode", sorted(MODES))
def test_board_times_are_stated_not_judged(project, mode):
    # get_board_info has last_saved_utc, and netlist_updated_utc only with a netlist;
    # two save times never "agree", so the tour must not ask for a verdict on them.
    flat = _flat(_tour(project, mode))
    assert "disagree" not in flat
    if "get_board_info" not in flat:
        return
    if MODES[mode]["netlist_free"]:
        assert "when the PcbDoc was last saved (no netlist time in this load)" in flat
        assert "netlist compiled" not in flat
    else:
        assert ("when the PcbDoc was saved and the netlist compiled (state both; the times "
                "alone do not show whether the board matches the schematic)") in flat


def test_pcb_only_project_skips_the_schematic_phase(project):
    prompt = _tour(project, "no_sheets")
    flat = _flat(prompt)
    phase_2 = prompt[prompt.index("### Phase 2"):prompt.index("### Phase 3")]
    assert phase_2.startswith("### Phase 2 — Schematic Blocks (skipped: no schematic sheets)")
    assert "Schematic sheets: 0\n" in prompt
    # No sheet walk, and no promise that Altium's full load (which answers no_sheets)
    # would enable the full tour.
    assert "per sheet" not in phase_2 and "Visit the sheets" not in phase_2
    assert "enables the full tour" not in flat
    assert not re.search(r"\bget_sheet_context\b", prompt)
    assert "as PCB data only" in phase_2
    assert "the PCB holds no part descriptions in this load: ask the user" in flat
    assert "### Phase 3 — PCB\n" in prompt
    # Even with a netlist in hand, a sheetless project has no Phase 2 parts to place.
    full = _flat(build_project_tour(dict(project, sheets=[]), "Prod", 2))
    assert "(skipped: no schematic sheets)" in full and "of Phase 2" not in full


def test_netlist_free_without_pcb(project):
    prompt = _tour(project, "netlist_free_no_pcb")
    assert "(limited: netlist-free load)" in prompt and PHASE_3_NO_PCB in prompt
    assert "none can be traced without a netlist. Opening" in _flat(prompt)
    assert "PCB data only" not in prompt
    for tool in ("get_classes", "get_net_pcb", "get_board_info", "check_pcb_health"):
        assert not re.search(rf"\b{tool}\b", prompt), tool
    assert "get_bom needs the netlist" in prompt


# ---------- primer ----------


@pytest.mark.parametrize("mode", sorted(MODES))
def test_primer_outline_is_fixed(project, mode):
    prompt = _tour(project, mode)
    primer = prompt[prompt.index("## Project Primer"):]
    title = primer.index("\n# Demo — Project Primer\n")
    positions = [primer.index(f"\n## {section}\n") for section in PRIMER_SECTIONS]
    assert title < positions[0] and positions == sorted(positions)
    assert len(PRIMER_SECTIONS) == 11
    assert [section.split(".")[0] for section in PRIMER_SECTIONS] == [
        str(n) for n in range(1, 12)]
    assert "keeps its heading and says why" in _flat(primer)
    # No tool returns today's date: the primer must not make one up.
    assert "(variant, today's date if you know it, load mode," in primer


def test_primer_is_saved_only_with_the_users_ok(project, tmp_path):
    flat = _flat(_tour(project))
    assert "Show the primer in the chat, then ask whether to save it." in flat
    assert f"Suggest this path, next to the .PrjPcb: {tmp_path / 'Demo_primer.md'} " in flat
    assert "only after the user agrees to a path" in flat
    assert "never overwrite a file without asking" in flat
    assert "ask the user for a folder of theirs" in flat
    assert "This server has no save tool" in flat


def test_primer_path():
    assert primer_path({"name": "Demo", "root_dir": ""}) == "Demo_primer.md"
    assert primer_path({}) == "Project_primer.md"
    assert primer_path({"name": "X", "root_dir": "proj"}) == str(Path("proj") / "X_primer.md")


def test_format_safe_with_braces(project, tmp_path):
    project = dict(project, name="Demo {0} {name}", root_dir=str(tmp_path / "{x}"))
    prompt = _tour(project, "netlist_free", variant="V{1}")
    assert "Project: Demo {0} {name}" in prompt
    assert "# Demo {0} {name} — Project Primer" in prompt
    assert "Variants: 2 (active: V{1})" in prompt
    assert f"Folder: {tmp_path / '{x}'}" in prompt


# ---------- size ----------


@pytest.mark.parametrize("mode", sorted(MODES))
def test_lines_are_wrapped(mode):
    project = {"name": "Demo", "root_dir": "C:\\p", "sheets": [{"name": "Top"}],
               "pcb_doc_paths": ["B.PcbDoc"]}
    lines = _tour(project, mode).splitlines()
    assert max(len(line) for line in lines) <= 90, max(lines, key=len)


@pytest.mark.parametrize("mode", sorted(MODES))
def test_prompt_size_is_bounded(project, mode):
    size = len(_tour(project, mode))
    assert size < MAX_PROMPT_CHARS
    # Well under the review prompts' own templates.
    assert size < 0.8 * min(len(CONSTRAINTS_REVIEW_PROMPT), len(PCB_MANUFACTURING_REVIEW_PROMPT))


# ---------- tool references ----------


def _registered() -> set[str]:
    return {tool.name for tool in asyncio.run(main_mod.mcp.list_tools())}


def test_prompt_tools_are_registered():
    assert set(PROJECT_TOUR_TOOLS) <= _registered()
    assert len(set(PROJECT_TOUR_TOOLS)) == len(PROJECT_TOUR_TOOLS)


def test_every_tool_named_in_any_mode_is_tracked(project):
    registered = _registered()
    named: set[str] = set()
    for mode in MODES:
        prompt = _tour(project, mode)
        found = _named(prompt, registered)
        assert found <= set(PROJECT_TOUR_TOOLS), (mode, found - set(PROJECT_TOUR_TOOLS))
        # Anything written as a call is a registered tool.
        called = set(re.findall(r"\b([a-z]+(?:_[a-z]+)+)\(", prompt))
        assert called <= registered, (mode, called - registered)
        named |= found
    # And every tracked tool is used by at least one mode.
    assert named == set(PROJECT_TOUR_TOOLS)


# ---------- the tool ----------


def test_tool_without_a_project_says_no_project():
    with patch.object(main_mod, "_project", None), patch.object(main_mod, "_variant_state", None):
        assert json.loads(main_mod.project_tour()) == main_mod.REVIEW_NO_PROJECT_ERROR


def test_tool_with_a_netlist_gives_the_full_tour(project):
    variants = VariantState([VariantDefinition(name="Prod"), VariantDefinition(name="Lite")])
    with patch.object(main_mod, "_project", project), \
         patch.object(main_mod, "_variant_state", variants), \
         patch.object(main_mod._altium, "_netlist", {"nets": {}, "components": {}}):
        prompt = main_mod.project_tour()
    assert prompt == build_project_tour(project, "Prod", 2)
    assert "Load: full (compiled netlist)" in prompt
    # A .PrjPcb without variants loads the parser's stand-in 'Default' variant.
    for state in (VariantState([]), VariantState([VariantDefinition(name="Default")])):
        with patch.object(main_mod, "_project", project), \
             patch.object(main_mod, "_variant_state", state), \
             patch.object(main_mod._altium, "_netlist", {"nets": {}, "components": {}}):
            assert "Variants: none defined in the .PrjPcb" in main_mod.project_tour()
    # The tour works in the variant the user picked, not the first one listed.
    variants.set_variant("Lite")
    with patch.object(main_mod, "_project", project), \
         patch.object(main_mod, "_variant_state", variants), \
         patch.object(main_mod._altium, "_netlist", {"nets": {}, "components": {}}):
        prompt = main_mod.project_tour()
    assert "Variants: 2 (active: Lite)\n" in prompt
    assert "Work in the active variant (Lite)" in prompt


def test_tool_metadata_manifest_readme_and_instructions():
    tools = {tool.name: tool for tool in asyncio.run(main_mod.mcp.list_tools())}
    tool = tools["project_tour"]
    assert tool.annotations.readOnlyHint is True
    description = _flat(tool.description)
    for phrase in ("'walk me through this design'", "'write a project primer'",
                   "saves only where the user agrees", "netlist-free load",
                   "without a PcbDoc skips the board", "a PCB-only project the schematic"):
        assert phrase in description, phrase
    instructions = main_mod.SERVER_INSTRUCTIONS
    assert instructions.index("project_tour") > 4096
    assert "## Project Tour" in instructions
    manifest = json.loads((Path(__file__).parents[1] / "manifest.json").read_text("utf-8"))
    entries = {entry["name"]: entry["description"] for entry in manifest["tools"]}
    assert "project primer" in entries["project_tour"]
    readme = (Path(__file__).parents[1] / "README.md").read_text("utf-8")
    modes = readme[readme.index("## Specialist modes"):]
    modes = modes[:modes.index("\n---")]
    assert "| `project_tour` |" in modes
    assert "without a PcbDoc or without schematic sheets" in modes
    changelog = (Path(__file__).parents[1] / "CHANGELOG.md").read_text("utf-8")
    unreleased = changelog[changelog.index("## [Unreleased]"):]
    unreleased = unreleased[:unreleased.index("\n## [", 1)]
    assert "- `project_tour`:" in unreleased
    assert "a PCB-only project (no schematic sheets) skips" in _flat(unreleased)


# ---------- Altium's public examples ----------


def _example_projects():
    if not EXAMPLES.is_dir():
        return []
    return sorted(EXAMPLES.rglob("*.PrjPcb"))


@pytest.mark.parametrize("prj_path", _example_projects(), ids=lambda p: p.stem)
def test_renders_for_public_examples(prj_path):
    data = parse_prj_pcb(str(prj_path))
    project = {"name": prj_path.stem, "root_dir": str(prj_path.parent),
               "prj_pcb_path": str(prj_path),
               "sheets": [{"name": Path(p).stem, "path": p} for p in data.sheet_paths],
               "pcb_doc_paths": data.pcb_doc_paths, "pcb_dwf_paths": data.pcb_dwf_paths}
    state = VariantState(data.variants)
    for netlist_free in (False, True):
        prompt = build_project_tour(project, state.active.name,
                                    defined_variant_count(data.variants),
                                    netlist_free=netlist_free)
        assert len(prompt) < MAX_PROMPT_CHARS
        assert f"Schematic sheets: {len(data.sheet_paths)}\n" in prompt
        if data.pcb_doc_paths:
            assert f"PCB: {Path(data.pcb_doc_paths[0]).name}" in prompt
