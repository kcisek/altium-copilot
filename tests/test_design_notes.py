"""get_design_notes / get_design_image / design notes in get_sheet_context."""
import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp.server.fastmcp import Image

import main
import services.design_notes as design_notes
from parsers.prj_pcb import PrjPcbData, VariantDefinition, VariantState, parse_prj_pcb
from parsers.sch_doc import SheetAnnotation, SheetAnnotations
from services.design_notes import (
    DesignNotesCache,
    build_design_notes,
    resolve_document,
    sheet_notes_for_context,
)
from tests.draftsman_fixture import FIXTURE as DRAWING
from tests.test_images import PICTURE, _bmp, _pixels

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def project(tmp_path):
    for name in ("sheet1.SchDoc", "sheet2.SchDoc"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    shutil.copy(DRAWING, tmp_path / "Fab.PCBDwf")
    return {
        "name": "BMS",
        "root_dir": str(tmp_path),
        "prj_pcb_path": str(tmp_path / "BMS.PrjPcb"),
        "sheets": [{"name": n, "path": str(tmp_path / f"{n}.SchDoc")} for n in ("sheet1", "sheet2")],
        "pcb_doc_paths": [str(tmp_path / "BMS.PcbDoc")],
        "pcb_dwf_paths": [str(tmp_path / "Fab.PCBDwf")],
        "parameters": {"PrjName": "BMS_11", "PrjPCBRev": "1"},
    }


@pytest.fixture
def cache():
    return DesignNotesCache()


# ---------- get_design_notes ----------

def test_whole_project(project, cache):
    text = build_design_notes(cache, project)
    header = text.splitlines()[0]
    assert header == "design_notes total:4 offset:0 has_more:false"
    assert "project:BMS parameters: PrjName=BMS_11; PrjPCBRev=1" in text
    assert "sheet:sheet1 notes:" in text and "sheet:sheet2 notes:" in text
    assert "[text_frame] 1k resistor instead of 10k in order to not cross logic threshhold (near R4)" in text
    assert "title_block: DocumentNumber=11; SheetNumber=10; SheetTotal=19" in text
    assert "directives: 50 Ohm x4" in text
    assert "draftsman:Fab.PCBDwf pages:3" in text
    assert "      1. Board material FR-4, Tg 150 or higher" in text
    assert "0.300 mm plated Via Top Layer - Bottom Layer x3" in text
    assert "[picture 0] 50x25 mm -> get_design_image(document='Fab.PCBDwf', index=0)" in text


def test_drawing_from_another_board_is_flagged(project, cache):
    text = build_design_notes(cache, project, "Fab")
    assert "source_pcb:C:\\Projects\\Demo\\Demo.PcbDoc (not this project's PcbDoc" in text
    project["pcb_doc_paths"] = ["c:/projects/demo/Demo.PcbDoc"]
    assert "not this project's PcbDoc" not in build_design_notes(cache, project, "Fab")


def test_pagination(project, cache, monkeypatch):
    monkeypatch.setattr(design_notes, "NOTES_PAGE_CHAR_BUDGET", 10)
    first = build_design_notes(cache, project)
    assert first.splitlines()[:2] == [
        "design_notes total:4 offset:0 has_more:true",
        "next:get_design_notes(offset=1) [3 remaining]",
    ]
    last = build_design_notes(cache, project, offset=3)
    assert last.startswith("design_notes total:4 offset:3 has_more:false")
    assert "draftsman:Fab.PCBDwf" in last
    beyond = build_design_notes(cache, project, offset=9)
    assert "warning:offset 9 >= total 4" in beyond


def test_single_document_by_name(project, cache):
    text = build_design_notes(cache, project, "SHEET2")
    assert "sheet:sheet2 notes:" in text and "sheet:sheet1" not in text
    assert "total:1" in text
    assert "draftsman:Fab.PCBDwf" in build_design_notes(cache, project, "fab.pcbdwf")


def test_unknown_document(project, cache):
    text = build_design_notes(cache, project, "nope")
    assert text.startswith("error: document_not_found")
    assert "'sheet1', 'sheet2', 'Fab.PCBDwf'" in text


def test_unreadable_document_does_not_hide_the_others(project, cache):
    Path(project["sheets"][1]["path"]).write_bytes(b"not an OLE file")
    text = build_design_notes(cache, project)
    assert "sheet:sheet2 error: notes could not be read" in text
    assert "sheet:sheet1 notes:" in text


def test_project_without_drawings_says_so(project, cache):
    project["pcb_dwf_paths"] = []
    project["parameters"] = {}
    text = build_design_notes(cache, project)
    assert "total:2" in text
    assert "note: the project lists no Draftsman (.PCBDwf) documents" in text


def test_resolve_document_prefers_sheets(project):
    project["pcb_dwf_paths"].append(str(Path(project["root_dir"]) / "sheet1.PCBDwf"))
    assert resolve_document(project, "sheet1")[0] == "sheet"
    assert resolve_document(project, "sheet1.PCBDwf")[0] == "draftsman"


def test_cache_reuses_parse_until_file_changes(project, cache):
    path = project["sheets"][0]["path"]
    with patch.object(design_notes, "parse_sch_doc_annotations",
                      wraps=design_notes.parse_sch_doc_annotations) as parse:
        cache.sheet(path)
        cache.sheet(path)
        assert parse.call_count == 1
        stat = os.stat(path)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        cache.sheet(path)
        assert parse.call_count == 2


# ---------- notes in get_sheet_context ----------

NETLIST = {
    "nets": {"N1": [("R4", "1"), ("U1", "5")]},
    "pin_to_net": {"R4": {"1": "N1"}, "U1": {"5": "N1"}},
    "components": {
        "R4": {"mpn": "ERJ-PA3F1001V", "description": "RES 1k", "value": "1k", "sheet": "sheet1",
               "pins": {"1": {"name": "1", "net": "N1"}}},
        "U1": {"mpn": "STM32F407VGT6", "description": "MCU", "value": None, "sheet": "sheet1",
               "pins": {"5": {"name": "PA0", "net": "N1"}}},
    },
}


def test_sheet_context_has_notes_between_header_and_components(project, cache):
    vs = VariantState([VariantDefinition(name="Default")])
    text = main._get_sheet_context_impl(project, NETLIST, vs, "sheet1", {}, notes_cache=cache)
    header, notes, components = text.split("\n\n", 2)
    assert header.startswith("sheet:sheet1 total:2")
    assert notes.startswith("design_notes:\nsheet:sheet1 notes:")
    assert "(near R4)" in notes
    assert components.startswith("R4|")


def test_sheet_context_notes_only_on_first_page(project, cache):
    vs = VariantState([VariantDefinition(name="Default")])
    text = main._get_sheet_context_impl(project, NETLIST, vs, "sheet1", {}, offset=1, notes_cache=cache)
    assert "design_notes:" not in text


def test_sheet_context_without_cache_is_unchanged(project):
    vs = VariantState([VariantDefinition(name="Default")])
    text = main._get_sheet_context_impl(project, NETLIST, vs, "sheet1", {})
    assert "design_notes:" not in text


def test_sheet_notes_truncated_with_pointer(project, cache, monkeypatch):
    monkeypatch.setattr(design_notes, "SHEET_CONTEXT_NOTES_CHARS", 300)
    text = sheet_notes_for_context(cache, project, "sheet1")
    assert text.endswith("... truncated — call get_design_notes(document='sheet1') for all notes")
    assert len(text) < 450


def test_sheet_without_notes_adds_nothing(project, cache):
    with patch.object(cache, "sheet", return_value=SheetAnnotations()):
        assert sheet_notes_for_context(cache, project, "sheet1") is None


# ---------- get_design_image ----------

def _sheet_with_image(file_name: str, embedded: bool) -> SheetAnnotations:
    return SheetAnnotations(items=[SheetAnnotation(
        kind="image", file_name=file_name, embedded=embedded, image_index=0,
        width_mil=1000, height_mil=500,
    )])


def test_draftsman_picture(project, cache):
    image = main._get_design_image_impl(project, cache, "Fab.PCBDwf", 0)
    assert isinstance(image, Image)
    assert image.to_image_content().mimeType == "image/png"
    assert image.data.startswith(b"\x89PNG")


def test_embedded_schematic_picture_becomes_png(project, cache):
    with patch.object(cache, "sheet", return_value=_sheet_with_image("ClipboardPicture1", True)), \
         patch("main.read_sch_doc_image", return_value=_bmp(3, 2, PICTURE)) as read:
        image = main._get_design_image_impl(project, cache, "sheet1", 0)
    read.assert_called_once_with(project["sheets"][0]["path"], "ClipboardPicture1")
    assert image.to_image_content().mimeType == "image/png"
    assert _pixels(image.data) == PICTURE


def test_linked_schematic_picture_relative_to_sheet(project, cache):
    pictures = Path(project["root_dir"]) / "pics"
    pictures.mkdir()
    (pictures / "block.jpg").write_bytes(b"\xff\xd8\xff\xe0 jpeg data")
    with patch.object(cache, "sheet", return_value=_sheet_with_image("pics\\block.jpg", False)):
        image = main._get_design_image_impl(project, cache, "sheet1", 0)
    assert image.to_image_content().mimeType == "image/jpeg"


def test_moved_absolute_link_found_next_to_sheet(project, cache):
    (Path(project["root_dir"]) / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n data")
    with patch.object(cache, "sheet", return_value=_sheet_with_image("Z:\\old\\logo.png", False)):
        image = main._get_design_image_impl(project, cache, "sheet1", 0)
    assert image.to_image_content().mimeType == "image/png"


def test_linked_picture_missing(project, cache):
    with patch.object(cache, "sheet", return_value=_sheet_with_image("C:\\gone\\x.png", False)):
        result = main._get_design_image_impl(project, cache, "sheet1", 0)
    assert result["error"] == "linked_image_missing"


def test_picture_index_out_of_range(project, cache):
    assert main._get_design_image_impl(project, cache, "sheet1", 0)["error"] == "image_not_found"
    assert main._get_design_image_impl(project, cache, "Fab", 3)["error"] == "image_not_found"


def test_picture_of_unknown_document(project, cache):
    result = main._get_design_image_impl(project, cache, "Nope", 0)
    assert result["error"] == "document_not_found"


def test_unsupported_picture_data(project, cache):
    with patch.object(cache, "sheet", return_value=_sheet_with_image("x", True)), \
         patch("main.read_sch_doc_image", return_value=b"%PDF-1.7"):
        result = main._get_design_image_impl(project, cache, "sheet1", 0)
    assert result["error"] == "unsupported_image_format"


# ---------- tool wrappers and load responses ----------

def test_tools_need_a_project():
    with patch("main._project", None):
        assert json.loads(main.get_design_notes())["error"] == "no_project"
        assert json.loads(main.get_design_image("sheet1"))["error"] == "no_project"


def test_get_design_image_tool_returns_json_errors(project):
    with patch("main._project", project), \
         patch("main._altium") as altium, \
         patch("main._variant_state", VariantState([VariantDefinition(name="Default")])):
        altium._netlist = NETLIST
        assert json.loads(main.get_design_image("Nope"))["error"] == "document_not_found"
        assert isinstance(main.get_design_image("Fab"), Image)
        assert main.get_design_notes(document="sheet2").startswith("design_notes total:1")


def test_set_project_dir_reports_parameters_and_drawings(tmp_path):
    (tmp_path / "Test.PrjPcb").write_text("[Design]\n", encoding="utf-8")
    with patch("main._project", None), patch("main._variant_state", None), \
         patch("main._altium") as altium, \
         patch("main.parse_prj_pcb") as parse, \
         patch("main.upsert_registry_entry"):
        parse.return_value = PrjPcbData(
            sheet_paths=[str(tmp_path / "Sheet1.SchDoc")],
            variants=[VariantDefinition(name="Default")],
            pcb_dwf_paths=[str(tmp_path / "Fab.PCBDwf")],
            parameters={"PrjName": "Demo"},
        )
        altium.get_status.return_value = {"running": True, "project_file": "Test.PrjPcb"}
        altium._netlist = {"nets": {}, "pin_to_net": {}, "components": {}}
        result = json.loads(main.set_project_dir(str(tmp_path)))
        assert main._project["pcb_dwf_paths"] == [str(tmp_path / "Fab.PCBDwf")]
        assert main._project["parameters"] == {"PrjName": "Demo"}
    assert result["project_parameters"] == {"PrjName": "Demo"}
    assert result["draftsman_documents"] == ["Fab.PCBDwf"]


def test_set_project_dir_omits_empty_design_info(tmp_path):
    (tmp_path / "Test.PrjPcb").write_text("[Design]\n", encoding="utf-8")
    with patch("main._project", None), patch("main._variant_state", None), \
         patch("main._altium") as altium, \
         patch("main.parse_prj_pcb") as parse, \
         patch("main.upsert_registry_entry"):
        parse.return_value = PrjPcbData(sheet_paths=[str(tmp_path / "S.SchDoc")],
                                        variants=[VariantDefinition(name="Default")])
        altium.get_status.return_value = {"running": True, "project_file": "Test.PrjPcb"}
        altium._netlist = {"nets": {}, "pin_to_net": {}, "components": {}}
        result = json.loads(main.set_project_dir(str(tmp_path)))
    assert "project_parameters" not in result and "draftsman_documents" not in result


def test_refresh_netlist_picks_up_new_drawings(tmp_path):
    project = {"name": "Test", "root_dir": str(tmp_path), "prj_pcb_path": str(tmp_path / "Test.PrjPcb"),
               "sheets": [], "pcb_doc_paths": [], "pcb_dwf_paths": [], "parameters": {}}
    with patch("main._project", project), \
         patch("main._variant_state", VariantState([VariantDefinition(name="Default")])), \
         patch("main._altium") as altium, \
         patch("main.parse_prj_pcb") as parse:
        parse.return_value = PrjPcbData(
            sheet_paths=[], variants=[VariantDefinition(name="Default")],
            pcb_dwf_paths=[str(tmp_path / "New.PCBDwf")], parameters={"Rev": "C"},
        )
        altium._netlist = {"nets": {}, "pin_to_net": {}, "components": {}}
        altium.generate_netlist.return_value = False
        main.refresh_netlist()
    assert project["pcb_dwf_paths"] == [str(tmp_path / "New.PCBDwf")]
    assert project["parameters"] == {"Rev": "C"}


def test_fixture_project_lists_its_drawings():
    data = parse_prj_pcb(str(FIXTURES / "test.prjpcb"))
    assert [Path(p).name for p in data.pcb_dwf_paths] == [
        "pcb-fab-drawing.PCBDwf", "smt-assembly-drawing.PCBDwf",
    ]
    assert data.parameters["PrjName"] == "BMS_11"
    assert "PrjCode" not in data.parameters  # empty value
