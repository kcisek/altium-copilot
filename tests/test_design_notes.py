"""get_design_notes / get_design_image / design notes in get_sheet_context."""
import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp.server.fastmcp import Image

import main
import parsers.pcb_dwf as pcb_dwf
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


def test_drawing_from_a_same_name_board_elsewhere_says_so(project, cache):
    # A copied project or an earlier revision: same board file name, other folder.
    project["pcb_doc_paths"] = [str(Path(project["root_dir"]) / "Demo.PcbDoc")]
    text = build_design_notes(cache, project, "Fab")
    assert "(a board of the same name in another folder" in text
    assert "not this project's PcbDoc" not in text


def test_same_board_through_another_path_is_not_flagged(tmp_path):
    board = tmp_path / "Board.PcbDoc"
    board.write_bytes(b"x")
    alias = tmp_path / "Alias.PcbDoc"
    os.link(board, alias)  # the same file reached by another path (cf. junction, mapped drive)
    assert design_notes._source_warning(str(alias), [str(board)]) is None


def test_last_update_keeps_utc_marker(project, cache):
    assert "last_update:2026-10-01T12:00:00Z" in build_design_notes(cache, project, "Fab")


def test_drill_row_without_size_does_not_hide_the_drawing(project, cache):
    doc = cache.draftsman(project["pcb_dwf_paths"][0])
    doc.drill_table[0]["size_mm"] = None
    text = build_design_notes(cache, project, "Fab")
    assert "    ? mm plated Via Top Layer - Bottom Layer x3" in text
    assert "1. Board material FR-4" in text


def test_fabrication_view_names_its_layer(project, cache):
    text = build_design_notes(cache, project, "Fab")
    assert "[view] fabrication view, Top side: Top Layer (Scale =ViewScale)" in text


# ---------- special strings ----------

@pytest.mark.parametrize("text,expected", [
    ("=Title", "Main"),
    ("=title", "Main"),                                   # names are case-insensitive
    ("=PrjName + '-v' + Rev", "BMS-v3"),                  # expression of names and literals
    ("=PrjName+\"+\"+Rev", "BMS+3"),                      # '+' inside a literal
    ("=SchRev", "3"),                                     # sheet value pointing at a project one
    ("=Missing", None),
    ("plain text", None),
])
def test_resolve_special_string(text, expected):
    sheet = {"Title": "Main", "SchRev": "=Rev"}
    project = {"PrjName": "BMS", "Rev": "3", "Title": "ignored: the sheet wins"}
    assert design_notes.resolve_special_string(text, sheet, project) == expected


def test_resolve_special_string_stops_on_a_cycle():
    assert design_notes.resolve_special_string("=A", {"A": "=B", "B": "=A"}) is None


def _sheet_with_specials() -> SheetAnnotations:
    return SheetAnnotations(
        items=[
            SheetAnnotation(kind="label", text="=PrjMPN + '-v' + PrjVersion"),
            SheetAnnotation(kind="text_frame", text="=Unset"),
            SheetAnnotation(kind="label", text="=Unknown + 'x'"),
            SheetAnnotation(kind="label", text="=ProjectName + ' ' + DocumentName"),
            SheetAnnotation(kind="label", text="='Drawn by ' + DrawnBy"),
            SheetAnnotation(kind="label", text="='Printed ' + CurrentDate"),
        ],
        title_block={"Revision": "=SCH_Revision", "Title": "Main"},
        sheet_parameters={
            "Unset": "*", "Revision": "=SCH_Revision", "Title": "Main",
            # Stored placeholders and computed values Altium does not draw as stored:
            "ProjectName": "*", "DrawnBy": "*", "CurrentDate": "Not shown in title block",
        },
    )


def test_sheet_notes_show_special_strings_as_displayed():
    project = {"prj_pcb_path": "C:/Boards/BMS.PrjPcb",
               "parameters": {"PrjMPN": "E002", "PrjVersion": "6.0", "SCH_Revision": "B", "DrawnBy": "KPC"}}
    notes = design_notes.resolved_sheet(_sheet_with_specials(), project, "C:/Boards/Power.SchDoc")
    text = design_notes.render_sheet_notes("S", notes)
    assert "[label] E002-v6.0" in text
    assert "=Unset" not in text  # set nowhere: nothing is drawn on the sheet
    assert "[label] =Unknown + 'x'" in text  # unknown name: shown as typed
    assert "[label] BMS.PrjPcb Power.SchDoc" in text  # built in, not the stored "*"
    assert "[label] Drawn by KPC" in text  # the sheet's "*" falls through to the project
    assert "[label] ='Printed ' + CurrentDate" in text  # computed when printed: left as typed
    assert "title_block: Revision=B; Title=Main" in text
    assert text.startswith("sheet:S notes:5 ")


def test_project_parameters_reach_sheet_notes_and_snapshot_rows(project, cache):
    notes = SheetAnnotations(items=[SheetAnnotation(kind="label", text="=PrjName + '-x'")],
                             title_block={"Rev": "=PrjPCBRev"})
    with patch.object(cache, "sheet", return_value=notes):
        text = build_design_notes(cache, project, "sheet1")
        context = sheet_notes_for_context(cache, project, "sheet1")
        rows, _ = design_notes.note_rows(cache, project)
    for shown in (text, context):
        assert "[label] BMS_11-x" in shown and "title_block: Rev=1" in shown
    sheet_rows = {r["kind"]: r for r in rows if r["document"] == "sheet1"}
    assert sheet_rows["label"]["text"] == "BMS_11-x"
    assert sheet_rows["title_block"]["text"] == "1"


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
    error = json.loads(build_design_notes(cache, project, "nope"))
    assert error["error"] == "document_not_found"
    assert "'sheet1', 'sheet2', 'Fab.PCBDwf'" in error["message"]


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


def test_draftsman_pictures_come_from_the_cached_parse(project, cache):
    # The file is decoded once for the notes; pictures, and bad indexes, reuse that parse.
    with patch.object(pcb_dwf, "_decode", wraps=pcb_dwf._decode) as decode:
        build_design_notes(cache, project, "Fab")
        assert isinstance(main._get_design_image_impl(project, cache, "Fab", 0), Image)
        assert main._get_design_image_impl(project, cache, "Fab", 7)["error"] == "image_not_found"
    assert decode.call_count == 1


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


def test_linked_picture_in_another_format_is_not_called_missing(project, cache):
    (Path(project["root_dir"]) / "scan.tif").write_bytes(b"II*\x00 tiff data")
    with patch.object(cache, "sheet", return_value=_sheet_with_image("scan.tif", False)):
        result = main._get_design_image_impl(project, cache, "sheet1", 0)
    assert result["error"] == "unsupported_image_format"


def test_oversized_picture_is_refused_before_it_reaches_the_conversation(project, cache):
    header = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (9000).to_bytes(4, "big") + (10).to_bytes(4, "big")
    (Path(project["root_dir"]) / "wide.png").write_bytes(header + b"\x08\x02\x00\x00\x00")
    with patch.object(cache, "sheet", return_value=_sheet_with_image("wide.png", False)):
        result = main._get_design_image_impl(project, cache, "sheet1", 0)
    assert result["error"] == "image_too_large"
    assert "9000x10 px" in result["message"]


# ---------- server instructions ----------

def test_key_instructions_fit_in_claude_codes_4096_character_cut():
    # Claude Code truncates MCP server instructions at 4096 characters.
    visible = main.SERVER_INSTRUCTIONS[:4096]
    assert "then get_design_notes (all pages)" in visible
    assert ("Do not call get_component or query_net individually for components already visible in a\n"
            "sheet context you have already loaded.") in visible


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
        assert json.loads(main.get_design_notes(document="Nope"))["error"] == "document_not_found"
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
