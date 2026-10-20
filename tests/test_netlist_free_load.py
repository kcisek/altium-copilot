"""Netlist-free load: set_project_dir without Altium and without an up-to-date cached
netlist loads the project from its files. PCB, rule, notes, picture and variant tools
work; the netlist tools answer netlist_required."""
import asyncio
import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp.server.fastmcp import Image

import server.main as main
from altium import AltiumClient
from parsers.prj_pcb import VariantDefinition, VariantState
from server.services.pcb_index import PcbIndex
from tests.draftsman_fixture import FIXTURE as DRAWING
from tests.test_pcb_index import NETLIST, pcb_data  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures"

# R1 is Not Fitted in the Prod variant; its UniqueId tail matches the PCB part.
PRJ_TEXT = (
    "[Design]\nVersion=1.0\n"
    "[Document1]\nDocumentPath=source\\sheet1.SchDoc\n"
    "[Document2]\nDocumentPath=source\\sheet2.SchDoc\n"
    "[Document3]\nDocumentPath=Board.PcbDoc\n"
    "[Document4]\nDocumentPath=Fab.PCBDwf\n"
    "[ProjectVariant1]\nDescription=Prod\nVariationCount=1\n"
    "Variation1=Designator=R1|UniqueId=\\ABCDEFGH\\RRRR|Kind=1|AlternatePart=\n"
)

# Tools that need no project at all, or load one.
LOADERS = {"detect_altium_project", "set_project_dir", "refresh_netlist", "brainstorm_circuits"}

NETLIST_TOOL_CALLS = {
    "query_net": lambda: main.query_net("GND"),
    "get_component": lambda: main.get_component("R1"),
    "search_components": lambda: main.search_components("R"),
    "get_sheet_context": lambda: main.get_sheet_context("sheet1"),
    "get_bom": lambda: main.get_bom(),
    "check_bom": lambda: main.check_bom(),
    "check_decoupling": lambda: main.check_decoupling(),
    "check_sch_pcb_sync": lambda: main.check_sch_pcb_sync(),
    "schematic_review": lambda: main.schematic_review(),
    "package_for_xfn": lambda: main.package_for_xfn(),
}

WORKING_TOOL_CALLS = {
    "get_board_info": lambda: main.get_board_info(),
    "get_net_pcb": lambda: main.get_net_pcb("CLK_A"),
    "get_net_neighbors": lambda: main.get_net_neighbors("CLK_A"),
    "query_pcb_region": lambda: main.query_pcb_region(100.0, 100.0),
    "get_component_placement": lambda: main.get_component_placement("R1"),
    "get_design_rules": lambda: main.get_design_rules(),
    "get_rules_for_net": lambda: main.get_rules_for_net("CLK_A"),
    "get_classes": lambda: main.get_classes(),
    "list_diff_pairs": lambda: main.list_diff_pairs(),
    "check_rule_compliance": lambda: main.check_rule_compliance(),
    "check_lengths": lambda: main.check_lengths(),
    "get_signal_length": lambda: main.get_signal_length("CLK_A"),
    "check_diff_pairs": lambda: main.check_diff_pairs(),
    "check_vias": lambda: main.check_vias(),
    "check_connectivity": lambda: main.check_connectivity(),
    "get_drc_report": lambda: main.get_drc_report(),
    "constraints_review": lambda: main.constraints_review(),
    "get_design_notes": lambda: main.get_design_notes(),
    "get_design_image": lambda: main.get_design_image("Fab.PCBDwf"),
    "list_variants": lambda: main.list_variants(),
    "set_active_variant": lambda: main.set_active_variant("Prod"),
    "get_clone_status": lambda: main.get_clone_status(),
}


def _write_project(root: Path, prj_text: str = PRJ_TEXT) -> Path:
    src = root / "source"
    src.mkdir(parents=True)
    for name in ("sheet1.SchDoc", "sheet2.SchDoc"):
        shutil.copy(FIXTURES / name, src / name)
    shutil.copy(DRAWING, root / "Fab.PCBDwf")
    (root / "Board.PcbDoc").write_bytes(b"fake")
    prj = root / "Board.PrjPcb"
    prj.write_text(prj_text, encoding="utf-8")
    return prj


@pytest.fixture
def env(tmp_path, pcb_data):
    """Isolated main state, Altium not running, an empty registry, and a PcbDoc that
    parses to the synthetic pcb_data. loads records the netlist each PCB parse gets."""
    root = tmp_path / "Board"
    _write_project(root)
    loads: list = []

    def load(path, netlist):
        loads.append(netlist)
        return PcbIndex(pcb_data, netlist)

    session = main.PcbSession()
    session._load = load
    with patch.object(main, "_project", None), \
         patch.object(main, "_variant_state", None), \
         patch.object(main, "_netlist_last_updated", None), \
         patch.object(main, "_pcb_session", session), \
         patch.object(main, "_design_notes", main.DesignNotesCache()), \
         patch.object(main, "_altium", AltiumClient()), \
         patch.object(main, "read_registry", return_value={"projects": []}), \
         patch.object(main, "upsert_registry_entry") as upsert, \
         patch.object(main._altium, "get_status", return_value={"running": False}):
        yield root, upsert, loads


def _load(root: Path) -> dict:
    return json.loads(main.set_project_dir(str(root)))


def test_load_without_altium_is_partial(env):
    root, upsert, _ = env
    result = _load(root)

    assert result["loaded"] is True
    assert result["partial"] is True
    assert result["load_mode"] == "netlist_free"
    assert result["project"] == "Board"
    assert result["sheets"] == ["sheet1", "sheet2"]
    assert result["variants"] == ["Prod"]
    assert result["pcb_doc"] == "Board.PcbDoc"
    assert result["draftsman_documents"] == ["Fab.PCBDwf"]
    assert result["netlist_updated_utc"] is None
    assert result["netlist_required_tools"] == list(main.NETLIST_TOOLS)
    assert "Altium Designer is not running" in result["message"]
    assert "get_sheet_context" in result["session_start"]
    assert "netlist_stale" not in result
    assert main._altium._netlist is None
    # Registered without a netlist mtime, so a later cache shortcut is never faked.
    upsert.assert_called_once_with("Board.PrjPcb", str(root))


def test_load_reads_project_context(env):
    root, _, _ = env
    (root / "CLAUDE.md").write_text("# Board notes", encoding="utf-8")
    assert _load(root)["project_context"] == "# Board notes"


def test_every_tool_is_classified():
    registered = {tool.name for tool in asyncio.run(main.mcp.list_tools())}
    assert set(main.NETLIST_TOOLS) == set(NETLIST_TOOL_CALLS)
    assert registered == LOADERS | set(NETLIST_TOOL_CALLS) | set(WORKING_TOOL_CALLS)


@pytest.mark.parametrize("tool", sorted(NETLIST_TOOL_CALLS))
def test_netlist_tools_answer_netlist_required(env, tool):
    root, _, _ = env
    _load(root)
    result = json.loads(NETLIST_TOOL_CALLS[tool]())
    assert result["error"] == "netlist_required"
    assert "Project 'Board' was loaded without a netlist" in result["message"]
    assert "Open the project in Altium Designer once" in result["message"]


@pytest.mark.parametrize("tool", sorted(WORKING_TOOL_CALLS))
def test_other_tools_work_without_netlist(env, tool):
    root, _, loads = env
    _load(root)
    result = WORKING_TOOL_CALLS[tool]()
    if isinstance(result, Image):
        return
    try:
        error = json.loads(result).get("error")
    except ValueError:  # constraints_review returns a prompt, not JSON
        error = None
    # There is no DRC report in the project folder, and it is no Altium 365 clone;
    # everything else answers.
    assert error in (None, "no_drc_report", "not_an_altium365_clone")
    assert all(netlist is None for netlist in loads)


def test_board_info_says_parts_are_not_linked(env):
    root, _, _ = env
    _load(root)
    result = json.loads(main.get_board_info())
    assert "netlist-free load" in result["netlist"]
    assert "refdes_mapping_unmatched" not in result
    assert result["netlist_updated_utc"] is None


def test_dnp_comes_from_the_variants_stored_designators(env):
    root, _, _ = env
    _load(root)
    main.set_active_variant("Prod")
    assert json.loads(main.list_variants())["variants"][0]["dnp_components"] == ["R1"]
    placement = json.loads(main.get_component_placement("R1"))["instances"][0]
    assert placement["sch_refdes"] is None
    assert placement["dnp"] is True


def test_design_notes_and_pictures(env):
    root, _, _ = env
    _load(root)
    notes = main.get_design_notes()
    assert "sheet:sheet1 notes:" in notes
    assert "draftsman:Fab.PCBDwf" in notes
    assert isinstance(main.get_design_image("Fab.PCBDwf"), Image)


def test_constraints_review_explains_the_missing_netlist(env):
    root, _, _ = env
    _load(root)
    review = main.constraints_review()
    assert "Netlist-free load" in review
    assert "get_component_placement" in review


def test_stale_cached_netlist_is_not_used(env):
    root, _, _ = env
    net = root / "Project Outputs for Board" / "Board.NET"
    net.parent.mkdir()
    net.write_text("(\nGND\nR1-1\n)\n", encoding="utf-8")
    registry = {"projects": [{"dir": str(root), "netlist_mtime": net.stat().st_mtime}]}
    # sheet1 saved after the netlist was generated
    os.utime(root / "source" / "sheet1.SchDoc", (net.stat().st_mtime + 100,) * 2)
    with patch.object(main, "read_registry", return_value=registry):
        result = _load(root)
    assert result["partial"] is True
    assert result["netlist_stale"] is True
    assert main._altium._netlist is None


def test_project_without_pcbdoc(env):
    root, _, _ = env
    prj = root / "Board.PrjPcb"
    prj.write_text(PRJ_TEXT.replace("[Document3]\nDocumentPath=Board.PcbDoc\n", ""),
                   encoding="utf-8")
    result = _load(root)
    assert result["partial"] is True
    assert result["pcb_doc"] is None
    assert "no .PcbDoc" in result["message"]
    assert json.loads(main.get_board_info())["error"] == "no_pcb_document"
    assert json.loads(main.list_variants())["active_variant"] == "Prod"


def test_without_a_project_tools_still_say_no_project(env):
    for call in (main.query_net, main.get_board_info, main.get_design_notes):
        args = ("GND",) if call is main.query_net else ()
        assert json.loads(call(*args))["error"] == "no_project"
    assert json.loads(main.schematic_review())["error"] == "no_project"
    assert json.loads(main.constraints_review())["error"] == "no_project"


def test_refresh_netlist_without_altium_stays_partial(env):
    root, _, _ = env
    _load(root)
    result = json.loads(main.refresh_netlist())
    assert result["partial"] is True
    assert main._project["name"] == "Board"


def test_refresh_netlist_loads_fully_once_altium_has_the_project(env):
    root, _, _ = env
    _load(root)

    def generate(prj_pcb_path, sheet_paths=None, annotation_paths=None):
        main._altium._netlist = {
            "nets": {"GND": [("R1", "1")]},
            "pin_to_net": {"R1": {"1": "GND"}},
            "components": {"R1": {"pins": {"1": {"name": "1", "net": "GND"}}}},
        }
        return True

    status = {"running": True, "project_file": "Board.PrjPcb", "tab_type": "schematic"}
    with patch.object(main._altium, "get_status", return_value=status), \
         patch.object(main._altium, "generate_netlist", side_effect=generate):
        result = json.loads(main.refresh_netlist())
    assert result["loaded"] is True
    assert "partial" not in result
    assert json.loads(main.query_net("GND"))["pin_count"] == 1


def test_altium_running_on_another_project_keeps_the_partial_load(env):
    root, _, _ = env
    _load(root)
    status = {"running": True, "project_file": "Other.PrjPcb", "tab_type": "schematic"}
    with patch.object(main._altium, "get_status", return_value=status):
        result = json.loads(main.refresh_netlist())
    assert result["error"] == "project_mismatch"
    assert main._project["name"] == "Board"
    assert json.loads(main.get_board_info()).get("error") is None


def test_full_load_netlist_reaches_the_pcb_index(tmp_path, pcb_data):
    # A regular load still hands the netlist to the PCB index (sch_refdes resolved).
    root = tmp_path / "Board"
    _write_project(root)
    session = main.PcbSession()
    session._load = lambda path, netlist: PcbIndex(pcb_data, netlist)
    with patch.object(main, "_project", {"name": "Board", "root_dir": str(root),
                                         "prj_pcb_path": str(root / "Board.PrjPcb"),
                                         "sheets": [],
                                         "pcb_doc_paths": [str(root / "Board.PcbDoc")]}), \
         patch.object(main, "_variant_state", VariantState([VariantDefinition(name="Default")])), \
         patch.object(main, "_pcb_session", session), \
         patch.object(main, "_altium", AltiumClient()):
        main._altium._netlist = NETLIST
        result = json.loads(main.get_board_info())
    assert result["refdes_mapping_unmatched"] == 0
    assert "netlist" not in result
