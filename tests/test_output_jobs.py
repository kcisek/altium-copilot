"""get_output_jobs: the project's OutJobs with their containers and outputs, coverage,
gap and pre-flight findings, and generated-file staleness. Synthetic OutJobs and projects
in tmp_path cover each field and finding; the board is a stub (_read_board is patched)
except where its reader is tested; Altium's public AD25 examples are snapshot-checked
when installed."""
import io
import json
import os
import struct
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import main
from parsers.pcb_dwf import DraftsmanDoc, DraftsmanItem
from parsers.prj_pcb import VariantDefinition, VariantState, parse_prj_pcb
from services import output_jobs
from services.output_jobs import (
    CHECKS,
    KB,
    STALE_TOLERANCE_S,
    _Board,
    _evaluate,
    _read_board,
    build_output_jobs,
    page_output_jobs,
)
from services.project_options import MATRIX_DEFAULT

EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")
CONTAINERS = (("Print Job", "Printer"), ("PDF", "Publish"), ("Folder", "GeneratedFiles"),
              ("Video", "Multimedia"))
GERBER = ("GerberUnit=Metric|NumberOfDecimals=5|OriginPosition={origin}|Plot.Set={plot}"
          "|Record=GerberView")
DRILL = "Units=Metric|NumberOfUnits=2|NumberOfDecimals=4|OriginPosition={origin}|Record=DrillView"
MECH_1 = "SerializeLayerHash.Version~2,ClassName~TLayerToBoolean,16777217~1,16908289~1"
COPPER_ONLY = "SerializeLayerHash.Version~2,ClassName~TLayerToBoolean,16777217~1"


def _output(type_, name=None, category="Fabrication", doc="", variant="", links=None,
            records=()):
    return {"type": type_, "name": name or type_, "category": category, "doc": doc,
            "variant": variant, "links": {3: 1} if links is None else links,
            "records": list(records)}


def _gerber(origin="Relative", plot=MECH_1, **kw):
    return _output("Gerber", "Gerber Files", records=[GERBER.format(origin=origin, plot=plot)],
                   **kw)


def _drill(origin="Relative", **kw):
    return _output("NC Drill", "NC Drill Files", records=[DRILL.format(origin=origin)], **kw)


def _outjob(outputs, target="Folder", variant="[No Variations]", publish=None, generated=""):
    lines = ["[OutputJobFile]", "Version=1.0", "Caption=", "ItemHRID=", "", "[OutputGroup1]",
             "Name=", f"TargetOutputMedium={target}", f"VariantName={variant}", "VariantScope=1"]
    for index, (name, kind) in enumerate(CONTAINERS, 1):
        lines += [f"OutputMedium{index}={name}", f"OutputMedium{index}_Type={kind}"]
    for n, out in enumerate(outputs, 1):
        lines += [f"OutputType{n}={out['type']}", f"OutputName{n}={out['name']}",
                  f"OutputCategory{n}={out['category']}", f"OutputDocumentPath{n}={out['doc']}",
                  f"OutputVariantName{n}={out['variant']}", f"OutputEnabled{n}=0"]
        lines += [f"OutputEnabled{n}_OutputMedium{i}={out['links'].get(i, 0)}"
                  for i in range(1, len(CONTAINERS) + 1)]
        for k, item in enumerate(out["records"], 1):
            lines += [f"Configuration{n}_Name{k}=OutputConfigurationParameter{k}",
                      f"Configuration{n}_Item{k}={item}"]
    if publish is None:
        publish = ("OutputFilePath2=C:\\elsewhere\\Board.pdf\nReleaseManaged2=1\n"
                   "OutputPathOutputer2=[Output Type]\nOutputFileName2=Board.pdf\n"
                   "ReleaseManaged3=1\nOutputPathMedia3=[Media Name]\n"
                   "OutputPathOutputer3=[Output Type]\nUseOutputNameForMulti3=1\n"
                   "ReleaseManaged4=1\n")
    lines += ["", "[PublishSettings]", publish, "[GeneratedFilesSettings]",
              generated or "TimestampFolder3=0\nAddToProject3=1"]
    return "\n".join(lines) + "\n"


PRJ_HEAD = "[Design]\nOutputPath=\nLogFolderPath=\n"
VARIANTS = ("[ProjectVariant1]\nDescription=Prod\nVariationCount=0\n"
            "[ProjectVariant2]\nDescription=Lite\nVariationCount=0\n")


def _project(tmp_path, outjobs: dict[str, str], documents=("a.SchDoc", "Board.PcbDoc"),
             missing=(), unlisted: dict[str, str] | None = None, design="", erc=""):
    folder = tmp_path / "Board"
    folder.mkdir(parents=True, exist_ok=True)
    listed = list(documents) + list(outjobs)
    for name in documents:
        if name not in missing:
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(b"design")
    for name, text in outjobs.items():
        if name not in missing:
            (folder / name).write_text(text, encoding="utf-8")
    for name, text in (unlisted or {}).items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text, encoding="utf-8")
    sections = "".join(f"[Document{i}]\nDocumentPath={name}\n" for i, name in enumerate(listed, 1))
    prj = folder / "Board.PrjPcb"
    head = PRJ_HEAD.replace("[Design]\n", "[Design]\n" + design)
    prj.write_text(head + sections + VARIANTS + erc, encoding="utf-8")
    data = parse_prj_pcb(str(prj))
    return {"name": "Board", "prj_pcb_path": str(prj),
            "sheets": [{"name": Path(p).stem, "path": p} for p in data.sheet_paths]}


def _board(**fields) -> _Board:
    board = _Board(path="Board.PcbDoc", stack="v9", overlays={"top": True, "bottom": True},
                   origin=(0.0, 0.0))
    board._cutouts = 0
    for key, value in fields.items():
        setattr(board, key, value)
    return board


@pytest.fixture
def board():
    """The board every test sees unless it sets fields: v9 stack with both overlays, its
    relative origin on the absolute one, no cutouts."""
    current = {"board": _board()}
    with patch.object(output_jobs, "_read_board", lambda path: current["board"]):
        yield current


def _no_drawing(path):
    raise OSError("no drawing in this test")


def _run(project, read_drawing=_no_drawing, outjob=None):
    return build_output_jobs(project, read_drawing, outjob)


def _findings(model, check):
    return [f for f in model["findings"] if f["check"] == check]


FULL = [_gerber(), _drill(), _output("ODB", "ODB++ Files"),
        _output("Pick Place", category="Assembly"),
        _output("Assembly", "Assembly Drawings", category="Assembly", links={2: 1}),
        _output("BOM_PartType", "Bill of Materials", category="Report", links={2: 2, 3: 4}),
        _output("Electrical Rules Check", category="Validation"),
        _output("Design Rules Check", category="Validation")]


# ---------- details and coverage ----------

def test_details_group_outputs_by_category(tmp_path, board):
    project = _project(tmp_path, {"Fab.OutJob": _outjob(FULL)})
    model = _run(project)
    assert model["outjobs"] == [
        {"file": "Fab.OutJob", "listed": True, "outputs": 8, "connected": 8}]
    (detail,) = model["details"]
    assert list(detail["outputs_by_category"]) == [
        "Fabrication", "Assembly", "Report", "Validation"]
    gerber = detail["outputs_by_category"]["Fabrication"][0]
    assert gerber == {"output": "Gerber Files", "type": "Gerber", "state": "connected",
                      "containers": ["Folder #1"], "document": "(default)",
                      "settings": {"units": "Metric", "decimals": "5", "origin": "Relative",
                                   "mechanical_layers_plotted": [1]},
                      "other_settings": 1}  # Plot.Set
    odb = detail["outputs_by_category"]["Fabrication"][2]
    assert odb["settings"]["origin"].startswith("Relative (ODB++ always")
    bom = detail["outputs_by_category"]["Report"][0]
    assert bom["containers"] == ["PDF #2", "Folder #4"] and bom["variant"] == "[No Variations]"
    assert detail["variant"] == "[No Variations]" and detail["variant_scope"] == "whole job"
    assert detail["selected_container"] == "Folder"
    pdf = next(c for c in detail["containers"] if c["container"] == "PDF")
    assert pdf["kind"] == "pdf"
    assert pdf["outputs"] == ["1. Assembly Drawings", "2. Bill of Materials"]
    assert pdf["path"]["managed"] == "release" and pdf["path"]["base_from"].endswith("(default)")
    assert pdf["path"]["naming"] == {"OutputFileName": "Board.pdf"}
    assert pdf["path"]["last_resolved_path"] == "C:\\elsewhere\\Board.pdf"
    folder = next(c for c in detail["containers"] if c["container"] == "Folder")
    assert folder["path"]["container_folder"] == "Folder"
    assert folder["path"]["resolved"].endswith(os.path.join("Project Outputs for Board", "Folder"))
    assert folder["path"]["add_to_project"] is True and folder["path"]["timestamp_folder"] is False
    assert folder["path"]["naming"] == {"UseOutputNameForMulti": True}
    printer = next(c for c in detail["containers"] if c["kind"] == "print job")
    assert printer == {"container": "Print Job", "kind": "print job", "outputs": []}
    assert all(row["status"] == "connected" for row in model["coverage"].values())
    assert model["coverage"]["fabrication_data"]["formats"] == ["Gerber RS-274X", "ODB++"]
    assert model["coverage"]["nc_drill"]["drill_data_in"] == ["ODB++"]
    assert model["findings"] == []
    assert {n["check"] for n in model["not_checked"]} == {
        "release_folders", "output_contents", "drc_settings", "long_path_support"}


def test_per_output_variant_and_unmapped_keys(tmp_path, board):
    text = _outjob([_output("BOM_PartType", category="Report", variant="Lite")], variant="Prod")
    text = text.replace("VariantScope=1", "VariantScope=0\nMystery=2").replace(
        "ItemHRID=", "ItemHRID=OJ-7")
    model = _run(_project(tmp_path, {"Fab.OutJob": text}))
    detail = model["details"][0]
    row = detail["outputs_by_category"]["Report"][0]
    assert row["variant"] == "Lite" and row["variant_from"] == "output"
    assert detail["variant"] == "Prod" and detail["variant_scope"] == "raw 0"
    assert "variant_scope_note" in detail
    assert detail["unmapped"] == {"ItemHRID": "OJ-7", "Mystery": "2"}


def test_settings_map_only_the_outputs_own_record(tmp_path, board):
    stray = _output("BOM_PartType", category="Report", records=[DRILL.format(origin="Absolute")])
    model = _run(_project(tmp_path, {"Fab.OutJob": _outjob([stray])}))
    row = model["details"][0]["outputs_by_category"]["Report"][0]
    assert "settings" not in row and row["other_settings"] == 4


def test_not_connected_and_present_not_connected(tmp_path, board):
    outputs = [_gerber(), _drill(), _output("Pick Place", category="Assembly", links={})]
    model = _run(_project(tmp_path, {"Fab.OutJob": _outjob(outputs)}))
    assert model["coverage"]["pick_and_place"] == {
        "status": "present, not connected",
        "not_connected": ["Fab.OutJob: Pick Place (Pick Place)"]}
    (gap,) = _findings(model, "no_pick_and_place")
    assert "connected to no container" in gap["issue"] and "green line" in gap["fix"]
    (idle,) = _findings(model, "not_connected")
    assert idle["severity"] == "info" and idle["examples"] == [
        {"outjob": "Fab.OutJob", "output": "Pick Place", "type": "Pick Place"}]
    row = model["details"][0]["outputs_by_category"]["Assembly"][0]
    assert row["state"] == "not connected" and "containers" not in row


# ---------- gaps ----------

def test_gaps_of_a_gerber_only_job(tmp_path, board):
    model = _run(_project(tmp_path, {"Fab.OutJob": _outjob([_gerber()])}))
    severities = {f["check"]: f["severity"] for f in model["findings"]}
    assert severities == {
        "no_nc_drill": "error", "no_pick_and_place": "warning", "no_assembly_drawing": "warning",
        "no_bom": "warning", "no_erc_validation": "warning", "no_drc_validation": "warning"}
    (drill,) = _findings(model, "no_nc_drill")
    assert drill["examples"] == [
        {"outjob": "Fab.OutJob", "output": "Gerber Files", "type": "Gerber"}]
    assert model["findings"][0]["check"] == "no_nc_drill"  # errors first
    assert model["summary"]["severity_counts"] == {"error": 1, "warning": 5, "info": 0}
    assert model["summary"]["by_check"]["no_nc_drill"] == "error"


def test_drill_data_in_gerber_x2_or_odb(tmp_path, board):
    x2 = _output("Gerber X2", records=["PlotPTHPairs=True|PlotNPTHPairs=False|Record=GerberX2View"])
    model = _run(_project(tmp_path, {"Fab.OutJob": _outjob([x2])}))
    assert not _findings(model, "no_nc_drill")
    assert model["coverage"]["nc_drill"]["drill_data_in"] == ["Gerber X2 drill files"]
    x2_bare = _output("Gerber X2", records=["PlotPTHPairs=False|Record=GerberX2View"])
    model = _run(_project(tmp_path / "b", {"Fab.OutJob": _outjob([x2_bare])}))
    assert _findings(model, "no_nc_drill")


def test_no_fabrication_data_and_schematic_only_project(tmp_path, board):
    model = _run(_project(tmp_path, {"Docs.OutJob": _outjob(
        [_output("Schematic Print", category="Documentation")])}))
    assert {f["check"] for f in model["findings"]} >= {"no_fabrication_data", "no_pick_and_place"}
    model = _run(_project(tmp_path / "sch", {"Docs.OutJob": _outjob(
        [_output("Schematic Print", category="Documentation")])}, documents=("a.SchDoc",)))
    checks = {f["check"] for f in model["findings"]}
    assert checks == {"no_bom", "no_erc_validation"}  # no PcbDoc: no board gaps


def test_pcb_prints_soften_the_assembly_drawing_gap(tmp_path, board):
    outputs = FULL[:4] + [_output("PCB Print", "PCB Prints", category="Documentation")] + FULL[5:]
    model = _run(_project(tmp_path, {"Fab.OutJob": _outjob(outputs)}))
    (gap,) = _findings(model, "no_assembly_drawing")
    assert gap["severity"] == "info" and gap["pcb_prints"] == ["Fab.OutJob: PCB Prints (PCB Print)"]


def test_draftsman_drawing_counts_when_it_has_an_assembly_view(tmp_path, board):
    outputs = FULL[:4] + [_output("PCBDrawing", "Fab Drawing", category="Documentation",
                                  doc="Fab.PCBDwf")] + FULL[5:]
    project = _project(tmp_path, {"Fab.OutJob": _outjob(outputs)},
                       documents=("a.SchDoc", "Board.PcbDoc", "Fab.PCBDwf"))
    fab_only = DraftsmanDoc(items=[DraftsmanItem(page=1, kind="view", view="fabrication view")])
    model = _run(project, lambda path: fab_only)
    assert _findings(model, "no_assembly_drawing")
    assembly = DraftsmanDoc(items=[DraftsmanItem(page=1, kind="view", view="assembly view",
                                                 variant="Document")])
    model = _run(project, lambda path: assembly)
    assert not _findings(model, "no_assembly_drawing")
    row = model["details"][0]["outputs_by_category"]["Documentation"][0]
    assert row["drawing"] == "Fab.PCBDwf" and row["assembly_views"] == 1
    assert row["drawing_variant"] == "[No Variations]"


def test_odb_or_ipc_expected_on_rigid_flex_or_blind_vias(tmp_path, board):
    project = _project(tmp_path, {"Fab.OutJob": _outjob([_gerber(), _drill()])})
    board["board"] = _board(flex=True, blind_buried_pairs=2)
    (finding,) = _findings(_run(project), "no_odb_ipc2581")
    assert finding["severity"] == "warning" and "rigid-flex" in finding["issue"]
    assert "2 blind or buried" in finding["issue"]
    board["board"] = _board(blind_buried_pairs=1)
    assert _findings(_run(project), "no_odb_ipc2581")[0]["severity"] == "info"
    board["board"] = _board()
    assert not _findings(_run(project), "no_odb_ipc2581")
    project = _project(tmp_path / "ipc", {"Fab.OutJob": _outjob([_gerber(), _drill(),
                                                                 _output("IPC2581")])})
    board["board"] = _board(flex=True)
    assert not _findings(_run(project), "no_odb_ipc2581")


# ---------- OutJob files ----------

def test_missing_unlisted_and_nested_outjobs(tmp_path, board):
    nested = {"Nested/Other.PrjPcb": "[Design]\n", "Nested/Theirs.OutJob": _outjob([]),
              "Jobs/Old.OutJob": _outjob([_gerber()]), "Loose.OutJob": "[OutputJobFile]\n"}
    project = _project(tmp_path, {"Fab.OutJob": _outjob(FULL), "Gone.OutJob": ""},
                       missing=("Gone.OutJob",), unlisted=nested)
    model = _run(project)
    files = {(j["file"], j["listed"]) for j in model["outjobs"]}
    assert files == {("Fab.OutJob", True), ("Gone.OutJob", True),
                     ("Loose.OutJob", False), (os.path.join("Jobs", "Old.OutJob"), False)}
    missing, unlisted = _findings(model, "outjob_files")
    assert missing["severity"] == "error" and missing["examples"] == [{"outjob": "Gone.OutJob"}]
    assert unlisted["severity"] == "info" and unlisted["count"] == 2
    gone = next(d for d in model["details"] if d["file"] == "Gone.OutJob")
    assert gone == {"file": "Gone.OutJob", "listed": True, "error": "missing on disk"}


def test_history_folder_and_cp1252_outjob(tmp_path, board):
    project = _project(tmp_path, {"Fab.OutJob": _outjob(FULL)},
                       unlisted={"History/Fab~1.OutJob": _outjob(FULL)})
    folder = Path(project["prj_pcb_path"]).parent
    text = _outjob(FULL).replace("Caption=", "Caption=Prüfung")
    (folder / "Fab.OutJob").write_bytes(text.encode("cp1252"))
    model = _run(project)
    assert [j["file"] for j in model["outjobs"]] == ["Fab.OutJob"]  # History is skipped
    assert model["details"][0]["encoding"] == "cp1252"


def test_unlisted_outjobs_are_shown_but_not_counted(tmp_path, board):
    project = _project(tmp_path, {}, unlisted={"Stray.OutJob": _outjob(
        [_gerber(doc="Gone.PcbDoc"), _output("Pick Place", links={})])})
    model = _run(project)
    assert all(row["status"] == "missing" for row in model["coverage"].values())
    assert {(f["check"], f["severity"]) for f in model["findings"]} == {
        ("outjob_files", "info"), ("outjob_files", "warning")}
    (detail,) = model["details"]
    assert detail["file"] == "Stray.OutJob" and not detail["listed"]
    assert detail["outputs_by_category"]["Fabrication"][0]["document"] == "Gone.PcbDoc"


def test_a_project_without_outjobs_gets_one_finding(tmp_path, board):
    model = _run(_project(tmp_path, {}))
    assert [(f["check"], f["severity"]) for f in model["findings"]] == [("outjob_files", "warning")]
    assert all(row["status"] == "missing" for row in model["coverage"].values())


def test_outjob_filter(tmp_path, board):
    project = _project(tmp_path, {"Fab.OutJob": _outjob(FULL), "Docs.OutJob": _outjob([])})
    model = _run(project, outjob="docs")
    assert [d["file"] for d in model["details"]] == ["Docs.OutJob"]
    assert len(model["outjobs"]) == 2
    error = _run(project, outjob="Nope")
    assert error["error"] == "outjob_not_found"
    assert set(error["outjobs"]) == {"Fab.OutJob", "Docs.OutJob"}


def test_unreadable_project_file(tmp_path):
    project = {"name": "X", "prj_pcb_path": str(tmp_path / "missing.PrjPcb"), "sheets": []}
    assert _run(project)["error"] == "project_file_unreadable"


# ---------- variants ----------

def test_bom_and_pick_and_place_on_different_variants(tmp_path, board):
    outputs = [_output("BOM_PartType", category="Report", variant="Prod"),
               _output("Pick Place", category="Assembly", variant="Lite"),
               _output("Assembly", category="Assembly", variant="Prod")]
    model = _run(_project(tmp_path, {"Asm.OutJob": _outjob(outputs)}))
    (finding,) = _findings(model, "bom_pnp_variant")
    assert finding["count"] == 2
    assert finding["examples"][0]["variant"] == "Prod"
    assert finding["examples"][0]["pick-and-place_variants"] == ["Lite"]
    (drawing,) = _findings(model, "assembly_drawing_variant")
    assert drawing["examples"] == [{"variant": "Lite",
                                    "used_by": ["Asm.OutJob: Pick Place (Pick Place)"],
                                    "assembly_drawings_show": ["Prod"]}]


def test_job_variant_applies_to_outputs_without_their_own(tmp_path, board):
    outputs = [_output("BOM_PartType", category="Report"),
               _output("Pick Place", category="Assembly"), _output("Assembly", category="Assembly")]
    model = _run(_project(tmp_path, {"Asm.OutJob": _outjob(outputs, variant="Prod")}))
    assert not _findings(model, "bom_pnp_variant")
    assert not _findings(model, "assembly_drawing_variant")


def test_draftsman_views_with_their_own_variant(tmp_path, board):
    outputs = [_output("PCBDrawing", "Assembly Drawing", category="Documentation")]
    project = _project(tmp_path, {"Asm.OutJob": _outjob(outputs, variant="Prod")},
                       documents=("a.SchDoc", "Board.PcbDoc", "Asm.PCBDwf"))
    doc = DraftsmanDoc(document_variant="Prod", items=[
        DraftsmanItem(page=1, kind="view", view="assembly view", title="Top", variant="Lite"),
        DraftsmanItem(page=1, kind="view", view="assembly view", variant="Document"),
        DraftsmanItem(page=2, kind="view", view="bill of materials", variant="Prod")])
    model = _run(project, lambda path: doc)
    (views,) = _findings(model, "draftsman_variant")
    assert views["severity"] == "warning" and views["source"] == KB["draftsman_variant"]
    assert views["examples"] == [{"outjob": "Asm.OutJob", "output": "Assembly Drawing",
                                  "type": "PCBDrawing", "drawing": "Asm.PCBDwf", "page": 1,
                                  "view": "assembly view", "title": "Top", "view_variant": "Lite",
                                  "drawing_variant": "Prod", "outjob_variant": "Prod"}]
    doc.document_variant = "Lite"
    doc.items = [doc.items[1]]  # the Document view follows the drawing
    (document,) = _findings(_run(project, lambda path: doc), "draftsman_variant")
    assert document["severity"] == "info" and document["examples"][0]["drawing_variant"] == "Lite"


def test_all_variants_drawing_and_unconnected_drawing_source(tmp_path, board):
    outputs = [_output("PCBDrawing", "Assembly Drawing", category="Documentation")]
    project = _project(tmp_path, {"Asm.OutJob": _outjob(outputs)},
                       documents=("a.SchDoc", "Board.PcbDoc", "Asm.PCBDwf"))
    # An older drawing's [All Variants] shows every part, as [No Variations] does.
    doc = DraftsmanDoc(document_variant="[All Variants]")
    assert not _findings(_run(project, lambda path: doc), "draftsman_variant")
    doc = DraftsmanDoc(source_pcb="C:\\Other\\Different.PcbDoc")
    assert _findings(_run(project, lambda path: doc), "wrong_document")
    # An output no container generates is not checked against the drawing's board.
    idle = _project(tmp_path / "idle", {"Asm.OutJob": _outjob(
        [_output("PCBDrawing", "Assembly Drawing", category="Documentation", links={})])},
        documents=("a.SchDoc", "Board.PcbDoc", "Asm.PCBDwf"))
    assert not _findings(_run(idle, lambda path: doc), "wrong_document")


def test_unreadable_drawing_is_not_checked(tmp_path, board):
    outputs = [_output("PCBDrawing", "Assembly Drawing", category="Documentation")]
    project = _project(tmp_path, {"Asm.OutJob": _outjob(outputs)},
                       documents=("a.SchDoc", "Board.PcbDoc", "Asm.PCBDwf"))
    model = _run(project)
    (note,) = [n for n in model["not_checked"] if n["check"] == "draftsman_variant"]
    assert "Asm.OutJob: Assembly Drawing (PCBDrawing)" in note["reason"]


# ---------- data sources ----------

def test_wrong_documents(tmp_path, board):
    outputs = [_gerber(doc="Old.PcbDoc"), _drill(doc="Spare.PcbDoc"),
               _output("Schematic Print", category="Documentation", doc="All SCH Documents"),
               _output("Pick Place", category="Assembly", doc="board.pcbdoc"),
               _output("PCBDrawing", "Drawing", category="Documentation", doc="Asm.PCBDwf")]
    project = _project(tmp_path, {"Fab.OutJob": _outjob(outputs)},
                       documents=("a.SchDoc", "Board.PcbDoc", "Asm.PCBDwf"))
    (Path(project["prj_pcb_path"]).parent / "Spare.PcbDoc").write_bytes(b"x")
    doc = DraftsmanDoc(source_pcb="C:\\Other\\Board.PcbDoc")
    model = _run(project, lambda path: doc)
    missing, outside, drawn = _findings(model, "wrong_document")
    assert missing["severity"] == "error" and missing["examples"][0]["document"] == "Old.PcbDoc"
    assert outside["severity"] == "warning" and outside["examples"][0]["document"] == "Spare.PcbDoc"
    assert drawn["severity"] == "info" and drawn["examples"][0]["same_name_elsewhere"] is True
    doc.source_pcb = "C:\\Other\\Different.PcbDoc"
    drawn = _findings(_run(project, lambda path: doc), "wrong_document")[-1]
    assert drawn["severity"] == "warning" and drawn["examples"][0]["same_name_elsewhere"] is False


def test_default_source_with_two_boards_is_ambiguous(tmp_path, board):
    project = _project(tmp_path, {"Fab.OutJob": _outjob(FULL)},
                       documents=("a.SchDoc", "Board.PcbDoc", "Panel.PcbDoc"))
    ambiguous = _findings(_run(project), "wrong_document")[0]
    assert ambiguous["severity"] == "info" and ambiguous["count"] == 6
    assert any(n["check"] == "board_checks" for n in _run(project)["not_checked"])


# ---------- ERC validation settings ----------

def _erc_record(levels: dict[int, int], matrix_changes=()) -> str:
    rows = [list(row) for row in MATRIX_DEFAULT]
    for row, column, letter in matrix_changes:
        rows[row][column] = letter
    parts = [f"ERCType{n}={level}" for n, level in levels.items()]
    parts += [f"ERCConnectionMatrixL{i}={''.join(row)}" for i, row in enumerate(rows, 1)]
    return "|".join(parts + ["ERCReportMode=0", "ERCValidationErrorLevel=1",
                             "ERCReportSuppressedErrors=False", "Record=ERCReportView"])


def test_erc_validation_levels_differ_from_project_options(tmp_path, board):
    erc = _output("Electrical Rules Check", category="Validation",
                  records=[_erc_record({10: 0, 11: 3, 12: 2, 1: 1, 64: 2, 999: 1},
                                       matrix_changes=[(2, 2, "N")])])
    project = _project(tmp_path, {"Val.OutJob": _outjob(FULL[:6] + [erc, FULL[7]])},
                       erc="[Electrical Rules Check]\nType10=1\nType11=2\nType12=2\n")
    model = _run(project)
    (finding,) = _findings(model, "erc_settings_differ")
    assert finding["severity"] == "warning" and finding["source"] == KB["erc_settings_differ"]
    example = finding["examples"][0]
    assert example["levels_differ"] == 2  # Type1 equals Altium's default, 64 and 999 are no checks
    assert example["levels"] == [
        {"check": "Nets with no driving source", "outjob": "No Report", "project": "Warning",
         "outjob_is": "looser"},
        {"check": "Nets containing floating input pins", "outjob": "Fatal Error",
         "project": "Error", "outjob_is": "stricter"}]
    # A Warning where the project has No Report fails no release: the label says stricter,
    # not that the release fails.
    assert "fails on it" not in json.dumps(finding)
    assert example["connection_matrix_cells_differ"] == 1
    assert example["connection_matrix"] == [{"row": "Output Pin", "column": "Output Pin",
                                             "outjob": "No Report", "project": "Error"}]
    row = model["details"][0]["outputs_by_category"]["Validation"][0]
    assert row["settings"] == {"validation_error_level_raw": "1", "report_mode_raw": "0",
                               "report_suppressed": False}


def test_erc_matrix_asymmetric_cells_and_unknown_defaults(tmp_path, board):
    # A cell below the diagonal is compared when the matrix is not symmetric.
    erc = _output("Electrical Rules Check", category="Validation",
                  records=[_erc_record({}, matrix_changes=[(2, 0, "E")])])
    (finding,) = _findings(_run(_project(tmp_path, {"Val.OutJob": _outjob([erc])})),
                           "erc_settings_differ")
    assert finding["examples"][0]["connection_matrix"] == [
        {"row": "Output Pin", "column": "Input Pin", "outjob": "Error", "project": "No Report"}]
    # Where the project writes no matrix, the cells whose default is not known are skipped.
    letter = "E" if MATRIX_DEFAULT[13][16] != "E" else "N"
    erc = _output("Electrical Rules Check", category="Validation",
                  records=[_erc_record({}, matrix_changes=[(13, 16, letter), (16, 13, letter)])])
    model = _run(_project(tmp_path / "u", {"Val.OutJob": _outjob([erc])}))
    assert not _findings(model, "erc_settings_differ")


def test_unconnected_erc_output_differences_are_info(tmp_path, board):
    erc = _output("Electrical Rules Check", category="Validation", links={},
                  records=[_erc_record({10: 0})])
    model = _run(_project(tmp_path, {"Val.OutJob": _outjob([erc])}))
    (finding,) = _findings(model, "erc_settings_differ")
    assert finding["severity"] == "info"
    assert finding["examples"][0]["levels"][0]["project"] == "Warning (default)"


# ---------- paths ----------

def test_paths_on_another_drive(tmp_path, board):
    publish = ("ReleaseManaged3=0\nOutputBasePath3=Q:\\Builds\nOutputPathOutputer3=[Output Type]\n"
               "ReleaseManaged2=0\nOutputBasePath2=\\\\server\\share\\out\nOutputFileName2=B.pdf\n")
    project = _project(tmp_path, {"Fab.OutJob": _outjob(FULL, publish=publish)},
                       design="LogFolderPath=Z:\\Logs\n")
    (finding,) = _findings(_run(project), "output_path")
    settings = {row["setting"]: row for row in finding["examples"]}
    assert set(settings) == {"Project Options > ECO Log Path", "container 'Folder' base path",
                             "container 'PDF' base path"}
    assert settings["container 'PDF' base path"]["reachable"] is None  # a share is not probed
    if not Path("Q:\\").exists() or not Path("Z:\\").exists():
        assert finding["severity"] == "error"
    assert finding["source"] == KB["output_path"]
    same_drive = str(Path(project["prj_pcb_path"]).parent / "Out")
    project = _project(tmp_path / "same", {"Fab.OutJob": _outjob(FULL)},
                       design=f"OutputPath={same_drive}\n")
    assert not _findings(_run(project), "output_path")


def test_long_output_paths(tmp_path, board):
    deep = "d" * 120
    publish = (f"ReleaseManaged3=0\nOutputBasePath3={deep}\\{deep}\n"
               "OutputPathOutputer3=[Output Type]\n")
    (finding,) = _findings(_run(_project(tmp_path, {"Fab.OutJob": _outjob(FULL, publish=publish)})),
                           "long_path")
    assert finding["severity"] == "error" and finding["source"] == KB["long_path"]
    row = finding["examples"][0]
    assert row["container"] == "Folder" and row["estimated_file_path_length"] >= 260
    publish = "ReleaseManaged3=0\nOutputBasePath3=" + "e" * 60 + "\n"
    project = _project(tmp_path / "near" / ("n" * (190 - len(str(tmp_path)))),
                       {"Fab.OutJob": _outjob(FULL, publish=publish)})
    near = _findings(_run(project), "long_path")
    assert near and near[0]["severity"] in ("warning", "error")
    assert not _findings(_run(_project(tmp_path / "short", {"Fab.OutJob": _outjob(FULL)})),
                         "long_path")


def test_long_path_estimate_per_container_kind(tmp_path, board):
    deep = "d" * 100 + "\\" + "d" * 100
    name = "N" * 40
    outputs = [_gerber(), _output("NC Drill", name, records=[DRILL.format(origin="Relative")]),
               _output("BOM_PartType", category="Report", links={2: 1})]
    publish = (f"ReleaseManaged3=0\nOutputBasePath3={deep}\nOutputPathOutputer3=[Output Name]\n"
               f"ReleaseManaged2=0\nOutputBasePath2={deep}\nOutputPathOutputer2=[Output Type]\n"
               f"OutputFileName2=B.pdf\nReleaseManaged4=0\nOutputBasePath4={deep}\n")
    project = _project(tmp_path, {"Fab.OutJob": _outjob(outputs, publish=publish)},
                       design="LogFolderPath=" + "l" * 240 + "\n")
    (finding,) = _findings(_run(project), "long_path")
    rows = {row.get("container", row.get("setting")): row for row in finding["examples"]}
    assert set(rows) == {"Folder", "PDF", "ECO Log Path"}  # the Video container has no output
    stem = len("Board")
    folder = rows["Folder"]["folder_length"]
    # The longest output name is the sub-folder of an [Output Name] container.
    assert rows["Folder"]["estimated_file_path_length"] == folder + len(name) + 1 + 1 + 30 + stem
    # A PDF goes straight into its folder under its own name: no output sub-folder.
    pdf = rows["PDF"]["folder_length"]
    assert rows["PDF"]["estimated_file_path_length"] == pdf + 1 + len("B.pdf")
    eco = rows["ECO Log Path"]
    assert eco["estimated_file_path_length"] == eco["folder_length"] + 1 + 30 + stem
    custom = publish.replace("[Output Name]", "[Output Custom]\nOutputPathOutputerValue3=" + "C" * 20)
    project = _project(tmp_path / "c", {"Fab.OutJob": _outjob(outputs, publish=custom)})
    model = _run(project)
    (row,) = [r for r in _findings(model, "long_path")[0]["examples"] if r.get("container") == "Folder"]
    assert row["estimated_file_path_length"] == row["folder_length"] + 21 + 1 + 30 + stem
    folder = next(c for c in model["details"][0]["containers"] if c["container"] == "Folder")
    assert folder["path"]["output_folder_value"] == "C" * 20


def test_project_on_a_mapped_drive_is_on_its_own_drive(tmp_path, board):
    # Path.resolve() turns a mapped drive into its UNC share: the project's drive is the
    # one it is opened from, so an Output Path on that drive letter is no other drive.
    output = tmp_path / "Board" / "Out"
    project = _project(tmp_path, {"Fab.OutJob": _outjob(FULL)}, design=f"OutputPath={output}\n")
    real = Path.resolve

    def unc(self, strict=False):
        resolved = real(self, strict)
        return Path("\\\\server\\share") / resolved.relative_to(resolved.anchor)
    with patch.object(Path, "resolve", unc):
        assert not _findings(_run(project), "output_path")


# ---------- the board against the outputs ----------

def test_gerber_on_the_absolute_origin_next_to_odb(tmp_path, board):
    project = _project(tmp_path, {"Fab.OutJob": _outjob([_gerber("Absolute"), _drill("Absolute"),
                                                         _output("ODB")])})
    board["board"] = _board(origin=(1000.0, 500.0))
    (finding,) = _findings(_run(project), "fabrication_origin")
    assert finding["severity"] == "warning" and finding["source"] == KB["fabrication_origin"]
    assert {e["type"] for e in finding["examples"]} == {"Gerber", "ODB"}
    board["board"] = _board()  # relative origin on the absolute origin: no offset
    assert not _findings(_run(project), "fabrication_origin")


def test_gerber_and_drill_origins_pair_within_an_outjob(tmp_path, board):
    board["board"] = _board(origin=(10.0, 10.0))
    project = _project(tmp_path, {"Fab.OutJob": _outjob([_gerber("Relative"), _drill("Absolute")]),
                                  "Asm.OutJob": _outjob([_gerber("Absolute")])})
    (finding,) = _findings(_run(project), "fabrication_origin")
    assert finding["severity"] == "error"
    assert finding["examples"] == [
        {"outjob": "Fab.OutJob", "output": "Gerber Files", "type": "Gerber", "origin": "Relative"},
        {"outjob": "Fab.OutJob", "output": "NC Drill Files", "type": "NC Drill",
         "origin": "Absolute"}]
    project = _project(tmp_path / "ok", {
        "Fab.OutJob": _outjob([_gerber("Relative"), _drill("Relative")]),
        "Asm.OutJob": _outjob([_gerber("Absolute")])})
    assert not _findings(_run(project), "fabrication_origin")
    board["board"] = _board(origin=None)
    project = _project(tmp_path / "x", {
        "Fab.OutJob": _outjob([_gerber("Relative"), _drill("Absolute")])})
    assert "could not be read" in _findings(_run(project), "fabrication_origin")[0]["issue"]
    # No OutJob has both: the Gerber and NC Drill outputs of the project are paired.
    project = _project(tmp_path / "split", {"Gerber.OutJob": _outjob([_gerber("Relative")]),
                                            "Drill.OutJob": _outjob([_drill("Absolute")])})
    (finding,) = _findings(_run(project), "fabrication_origin")
    assert {(e["outjob"], e["origin"]) for e in finding["examples"]} == {
        ("Gerber.OutJob", "Relative"), ("Drill.OutJob", "Absolute")}


def test_overlay_missing_from_the_stack(tmp_path, board):
    project = _project(tmp_path, {"Fab.OutJob": _outjob([_gerber(), _drill()])})
    board["board"] = _board(overlays={"top": False, "bottom": True})
    (finding,) = _findings(_run(project), "overlay_not_in_stackup")
    assert finding["severity"] == "warning" and finding["examples"] == [
        {"layer": "Top Overlay", "parts_on_side": 0}]
    assert finding["outputs"] == ["Fab.OutJob: Gerber Files (Gerber)"]  # fabrication data
    board["board"] = _board(overlays={"top": True, "bottom": False})
    assert _findings(_run(project), "overlay_not_in_stackup")[0]["severity"] == "info"
    board["board"].parts["bottom"] = 3
    assert _findings(_run(project), "overlay_not_in_stackup")[0]["severity"] == "warning"
    board["board"] = _board(stack="legacy", overlays={})
    model = _run(project)
    assert not _findings(model, "overlay_not_in_stackup")
    assert any(n["check"] == "overlay_not_in_stackup" for n in model["not_checked"])


def test_board_cutouts_without_a_mechanical_layer(tmp_path, board):
    project = _project(tmp_path, {"Fab.OutJob": _outjob([_gerber(plot=COPPER_ONLY), _drill()])})
    board["board"] = _board(_cutouts=2)
    (finding,) = _findings(_run(project), "board_cutouts")
    assert finding["severity"] == "warning" and finding["source"] == KB["board_cutouts"]
    assert finding["examples"][0]["mechanical_layers_plotted"] == []
    project = _project(tmp_path / "m", {"Fab.OutJob": _outjob([_gerber(plot=MECH_1), _drill()])})
    assert _findings(_run(project), "board_cutouts")[0]["severity"] == "info"
    board["board"] = _board(_cutouts=0)
    assert not _findings(_run(project), "board_cutouts")


def test_board_cutouts_with_the_board_profile_plotted(tmp_path, board):
    board["board"] = _board(_cutouts=1)
    profile = _output("Gerber", "Gerber Files", records=[
        f"GerberUnit=Metric|OriginPosition=Relative|Plot.Set={COPPER_ONLY}|PlotBoardProfile=True"
        "|Record=GerberView"])
    (finding,) = _findings(_run(_project(tmp_path, {"Fab.OutJob": _outjob([profile, _drill()])})),
                           "board_cutouts")
    assert finding["severity"] == "info" and finding["examples"][0]["plot_board_profile"] is True
    # PlotMechanicalNPlot is an ODB++ key: in a Gerber setup it plots no mechanical layer.
    odb_key = _output("Gerber", "Gerber Files", records=[
        f"GerberUnit=Metric|OriginPosition=Relative|Plot.Set={COPPER_ONLY}|PlotMechanical1Plot=True"
        "|Record=GerberView"])
    (finding,) = _findings(_run(_project(tmp_path / "o", {"Fab.OutJob": _outjob([odb_key, _drill()])})),
                           "board_cutouts")
    assert finding["severity"] == "warning"
    assert finding["examples"][0]["mechanical_layers_plotted"] == []


def test_unreadable_board_is_not_checked(tmp_path, board):
    board["board"] = _board(error="OSError: not an OLE file")
    model = _run(_project(tmp_path, {"Fab.OutJob": _outjob([_gerber(), _drill()])}))
    reasons = {n["check"]: n["reason"] for n in model["not_checked"]}
    assert "not an OLE file" in reasons["overlay_not_in_stackup"]
    assert "board_cutouts" in reasons


# ---------- generated files ----------

def _age(path: Path, seconds_ago: float):
    stamp = time.time() - seconds_ago
    os.utime(path, (stamp, stamp))


def test_stale_outputs(tmp_path, board):
    project = _project(tmp_path, {"Fab.OutJob": _outjob(FULL)})
    folder = Path(project["prj_pcb_path"]).parent
    out = folder / "Project Outputs for Board" / "Folder"
    (out / "Gerber").mkdir(parents=True)
    (out / "Gerber" / "Board.GTL").write_text("G04*")
    (out / "Gerber" / "Board.PcbDoc").write_text("a copy is no output")
    (out / "Pick Place").mkdir()
    (out / "Pick Place" / "Pick Place for Board.txt").write_text("x")
    for path in (out / "Gerber" / "Board.GTL", out / "Pick Place" / "Pick Place for Board.txt"):
        _age(path, 3600)
    _age(folder / "a.SchDoc", 7200)
    model = _run(project)
    (finding,) = _findings(model, "stale_outputs")
    row = finding["examples"][0]
    assert row["container"] == "Folder" and row["documents_newer"][0]["document"] == "Board.PcbDoc"
    assert set(row["stale_outputs"]) == {"Gerber Files", "Pick Place"}
    detail = model["details"][0]
    generated = next(c for c in detail["containers"] if c["container"] == "Folder")["generated"]
    assert generated["exists"] is True and generated["files"] == 2 and generated["stale"] is True
    gerber = detail["outputs_by_category"]["Fabrication"][0]["generated"]
    assert gerber["files"] == 1 and gerber["stale"] is True
    pdf = next(c for c in detail["containers"] if c["container"] == "PDF")["generated"]
    assert pdf["exists"] is False and pdf["location"].endswith("Board.pdf")
    # A document saved within the tolerance (a checkout writing both) is no change.
    _age(folder / "Board.PcbDoc", 3600 - STALE_TOLERANCE_S / 2)
    assert not _findings(_run(project), "stale_outputs")


def test_pdf_container_age_and_project_folder_base(tmp_path, board):
    publish = ("ReleaseManaged2=0\nOutputBasePath2=Docs\nOutputFileName2=Board\n"
               "ReleaseManaged3=0\nOutputBasePath3=\n")
    project = _project(tmp_path, {"Fab.OutJob": _outjob(FULL, publish=publish)})
    folder = Path(project["prj_pcb_path"]).parent
    (folder / "Docs").mkdir()
    (folder / "Docs" / "Board.pdf").write_text("%PDF")
    _age(folder / "Docs" / "Board.pdf", 10)
    for name in ("a.SchDoc", "Board.PcbDoc"):
        _age(folder / name, 3600)
    detail = _run(project)["details"][0]
    pdf = next(c for c in detail["containers"] if c["container"] == "PDF")["generated"]
    assert pdf["exists"] is True and pdf["stale"] is False
    assert pdf["location"].endswith(os.path.join("Docs", "Board.pdf"))
    own = next(c for c in detail["containers"] if c["container"] == "Folder")["generated"]
    assert own == {"location": str(folder), "checked": False, "reason": "the project folder itself"}


def test_generated_files_skip_dot_folders_and_stop_at_the_walk_limit(tmp_path, board):
    project = _project(tmp_path, {"Fab.OutJob": _outjob(FULL)})
    out = Path(project["prj_pcb_path"]).parent / "Project Outputs for Board" / "Folder"
    (out / ".svn").mkdir(parents=True)
    (out / ".svn" / "entries").write_text("x")
    (out / "a.txt").write_text("x")
    (out / "b.txt").write_text("x")
    for name in ("a.txt", "b.txt"):
        _age(out / name, 3600)

    def folder_row(model):
        detail = model["details"][0]
        return next(c for c in detail["containers"] if c["container"] == "Folder")["generated"]
    model = _run(project)
    row = folder_row(model)
    assert row["files"] == 2 and "files_truncated" not in row and row["stale"] is True
    assert _findings(model, "stale_outputs")
    # Past the walk limit the newest file may be one not walked: no age is claimed.
    with patch.object(output_jobs, "_WALK_LIMIT", 1):
        model = _run(project)
    row = folder_row(model)
    assert row["files"] == 1 and row["files_truncated"] is True and "stale" not in row
    assert not _findings(model, "stale_outputs")


def test_output_name_sub_folders(tmp_path, board):
    publish = "ReleaseManaged3=1\nOutputPathMedia3=[Media Name]\nOutputPathOutputer3=[Output Name]\n"
    project = _project(tmp_path, {"Fab.OutJob": _outjob(FULL, publish=publish)})
    sub = Path(project["prj_pcb_path"]).parent / "Project Outputs for Board" / "Folder" / "Gerber Files"
    sub.mkdir(parents=True)
    (sub / "Board.GTL").write_text("G04*")
    gerber = _run(project)["details"][0]["outputs_by_category"]["Fabrication"][0]
    assert gerber["generated"]["files"] == 1
    assert gerber["generated"]["location"].endswith("Gerber Files")


# ---------- container folder expressions ----------

def test_evaluate_container_folder_expressions():
    context = SimpleNamespace(prj_path=Path("X.PrjPcb"), parameters={"rev": "B"})
    assert _evaluate("=ProjectName", context) == "X"
    assert _evaluate("=Rev + '_' +ProjectName", context) == "B_X"
    assert _evaluate("='Out'", context) == "Out"
    for expression in ("=CurrentDate+'_'+CurrentTime", "=Unknown", "=ProjectName +",
                       "='a' 'b'", "=", "=Rev-1", "=+'a'"):
        assert _evaluate(expression, context) is None, expression


def test_container_folder_expressions_resolve(tmp_path, board):
    # '=ProjectName' and project parameters name the folder Altium writes to; read as text
    # they named a folder '=ProjectName' that never exists.
    publish = ("ReleaseManaged2=1\nOutputPathMedia2==Revision + '_Docs'\nOutputFileName2=Board.pdf\n"
               "ReleaseManaged3=1\nOutputPathMedia3==ProjectName\nOutputPathOutputer3=[Output Type]\n")
    project = _project(tmp_path, {"Fab.OutJob": _outjob(FULL, publish=publish)},
                       erc="[Parameter1]\nName=Revision\nValue=B\n")
    out = Path(project["prj_pcb_path"]).parent / "Project Outputs for Board"
    (out / "Board" / "Gerber").mkdir(parents=True)
    (out / "Board" / "Gerber" / "Board.GTL").write_text("G04*")
    (out / "B_Docs").mkdir()
    (out / "B_Docs" / "Board.pdf").write_text("%PDF")
    detail = _run(project)["details"][0]
    containers = {c["container"]: c for c in detail["containers"]}
    folder = containers["Folder"]
    assert folder["path"]["container_folder"] == "Board"
    assert folder["path"]["container_folder_expression"] == "=ProjectName"
    assert "container_folder_unresolved" not in folder["path"]
    assert folder["generated"]["exists"] is True and folder["generated"]["files"] == 1
    assert detail["outputs_by_category"]["Fabrication"][0]["generated"]["files"] == 1
    pdf = containers["PDF"]
    assert pdf["path"]["container_folder"] == "B_Docs" and pdf["generated"]["exists"] is True
    # A special file name (an expression) names the PDF instead of OutputFileName.
    special = publish + "OutputFileNameSpecial2==Revision + '_Schematics'\n"
    project = _project(tmp_path / "s", {"Fab.OutJob": _outjob(FULL, publish=special)},
                       erc="[Parameter1]\nName=Revision\nValue=B\n")
    docs = Path(project["prj_pcb_path"]).parent / "Project Outputs for Board" / "B_Docs"
    docs.mkdir(parents=True)
    (docs / "B_Schematics.pdf").write_text("%PDF")
    pdf = next(c for c in _run(project)["details"][0]["containers"] if c["container"] == "PDF")
    assert pdf["path"]["naming"]["pdf_file_name"] == "B_Schematics"
    assert pdf["generated"]["exists"] is True
    assert pdf["generated"]["location"].endswith("B_Schematics.pdf")
    dated = publish + "OutputFileNameSpecial2==CurrentDate\n"
    project = _project(tmp_path / "d", {"Fab.OutJob": _outjob(FULL, publish=dated)},
                       erc="[Parameter1]\nName=Revision\nValue=B\n")
    pdf = next(c for c in _run(project)["details"][0]["containers"] if c["container"] == "PDF")
    assert pdf["path"]["naming"]["pdf_file_name"] is None
    assert pdf["generated"]["checked"] is False and "file name =CurrentDate" in pdf["generated"]["reason"]


def test_date_time_container_folders_check_the_base(tmp_path, board):
    publish = ("ReleaseManaged2=1\nOutputPathMedia2==CurrentDate\nOutputFileName2=Board.pdf\n"
               "ReleaseManaged3=1\nOutputPathMedia3==CurrentDate+'_'+CurrentTime\n"
               "OutputPathOutputer3=[Output Type]\n")
    project = _project(tmp_path, {"Fab.OutJob": _outjob(FULL, publish=publish)})
    base = Path(project["prj_pcb_path"]).parent / "Project Outputs for Board"
    old = base / "2024-01-02_10-00" / "Gerber"
    old.mkdir(parents=True)
    (old / "Board.GTL").write_text("G04*")
    _age(old / "Board.GTL", 3600)
    (base / "Gerber").mkdir()  # another container's, not this one's output folder
    (base / "Gerber" / "Other.GTL").write_text("G04*")
    _age(base / "Gerber" / "Other.GTL", 3600)
    model = _run(project)
    (stale,) = _findings(model, "stale_outputs")
    row = stale["examples"][0]
    assert row["container"] == "Folder" and row["location"] == str(base)
    assert "resolved by Altium when it generates" in row["scope"]
    assert "stale_outputs" not in row  # no per-output folder under an unresolved one
    containers = {c["container"]: c for c in model["details"][0]["containers"]}
    assert containers["Folder"]["path"]["container_folder"] is None
    assert containers["Folder"]["path"]["container_folder_unresolved"] == (
        "=CurrentDate+'_'+CurrentTime")
    assert containers["PDF"]["generated"]["checked"] is False


# ---------- paging and the tool ----------

def test_paging(tmp_path, board):
    project = _project(tmp_path, {f"J{i}.OutJob": _outjob([_gerber()]) for i in range(3)})
    model = _run(project)
    first = page_output_jobs(model, 0, 2, 1)
    assert first["details_total"] == 3 and first["has_more"] and first["next_offset"] == 2
    assert [d["file"] for d in first["details"]] == ["J0.OutJob", "J1.OutJob"]
    assert "coverage" in first and "findings" in first
    (drill,) = [f for f in first["findings"] if f["check"] == "no_nc_drill"]
    assert drill["count"] == 3 and len(drill["examples"]) == 1 and drill["examples_shown"] == 1
    second = page_output_jobs(model, 2, 2, 1)
    assert set(second) == {"project", "prj_pcb", "outjobs", "details_total", "offset",
                           "has_more", "details"}
    assert not second["has_more"] and second["details"][0]["file"] == "J2.OutJob"


def test_checks_are_ranked_and_named():
    assert len(set(CHECKS)) == len(CHECKS)
    assert set(KB) <= set(CHECKS)
    assert all(url.startswith("https://www.altium.com/documentation/knowledge-base/")
               for url in KB.values())


def test_tool_needs_a_project():
    with patch("main._project", None):
        assert json.loads(main.get_output_jobs())["error"] == "no_project"


def test_tool_renders(tmp_path, board):
    project = _project(tmp_path, {"Fab.OutJob": _outjob(FULL)})
    with patch("main._project", project), \
         patch("main._altium") as altium, \
         patch("main._variant_state", VariantState([VariantDefinition(name="Default")])):
        altium._netlist = None
        result = json.loads(main.get_output_jobs())
        assert result["prj_pcb"] == "Board.PrjPcb" and result["details_total"] == 1
        assert json.loads(main.get_output_jobs(outjob="x"))["error"] == "outjob_not_found"


# ---------- the board reader ----------

class _FakeOle:
    def __init__(self, streams: dict[str, bytes]):
        self.streams = streams

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def listdir(self):
        return [name.split("/") for name in self.streams]

    def openstream(self, name):
        return io.BytesIO(self.streams[name])


def _text_record(properties: dict[str, str]) -> bytes:
    body = "|" + "|".join(f"{k}={v}" for k, v in properties.items())
    data = body.encode("latin-1") + b"\x00"
    return struct.pack("<I", len(data)) + data


def test_read_board_facts():
    board6 = _text_record({
        "ORIGINX": "1000mil", "ORIGINY": "500mil",
        "V9_STACK_LAYER0_NAME": "Top Overlay", "V9_STACK_LAYER0_LAYERID": "16973830",
        "V9_STACK_LAYER1_NAME": "Top Layer", "V9_STACK_LAYER1_LAYERID": "16777217",
        "V9_STACK_LAYER1_COPTHICK": "1.4mil",
        "V9_STACK_LAYER2_NAME": "Bottom Layer", "V9_STACK_LAYER2_LAYERID": "16842751",
        "V9_STACK_LAYER2_COPTHICK": "1.4mil",
        "LAYERPAIR0LOW": "TOP", "LAYERPAIR0HIGH": "BOTTOM",
        "LAYERPAIR1LOW": "TOP", "LAYERPAIR1HIGH": "MID1",
        "LAYERPAIR2LOW": "TOP", "LAYERPAIR2HIGH": "MID2", "LAYERPAIR2BACKDRILL": "TRUE",
        "V9_SUBSTACK0_ID": "1", "V9_SUBSTACK0_ISFLEX": "TRUE"})
    components = _text_record({"LAYER": "TOP"}) + _text_record({"LAYER": "BOTTOM"}) * 2
    streams = {"Board6/Data": board6, "Components6/Data": components, "Regions6/Data": b""}
    with patch.object(output_jobs.olefile, "OleFileIO", lambda path: _FakeOle(streams)):
        board = _read_board("Board.PcbDoc")
        assert board.error is None and board.stack == "v9"
        assert board.overlays == {"top": True, "bottom": False}
        assert board.origin == (1000.0, 500.0)
        assert board.blind_buried_pairs == 1 and board.flex is True
        assert board.parts == {"top": 1, "bottom": 2}
        heads = [(0, b"", type("Head", (), {"properties": {"ISBOARDCUTOUT": "TRUE"}})()),
                 (1, b"", type("Head", (), {"properties": {"KIND": "3"}})()),
                 (2, b"", type("Head", (), {"properties": {"KIND": "0"}})()), (3, b"", None)]
        with patch.object(output_jobs, "_region_heads", lambda data: iter(heads)):
            assert board.cutouts() == 2


def test_read_board_of_a_file_that_is_no_pcbdoc(tmp_path):
    path = tmp_path / "Board.PcbDoc"
    path.write_bytes(b"fake")
    board = _read_board(str(path))
    assert board.error and board.cutouts() is None


# ---------- Altium's public examples ----------

def _example(relative: str):
    path = EXAMPLES / relative
    if not path.exists():
        pytest.skip("Altium AD25 examples not installed")
    data = parse_prj_pcb(str(path))
    return {"name": path.stem, "prj_pcb_path": str(path),
            "sheets": [{"name": Path(p).stem, "path": p} for p in data.sheet_paths]}


def test_bluetooth_sentinel_example():
    project = _example(r"Bluetooth Sentinel\Bluetooth_Sentinel.PrjPcb")
    model = build_output_jobs(project, _no_drawing)
    assert [j["file"] for j in model["outjobs"]] == [
        "Documentation.OutJob", "Assembly.OutJob", "Fabrication.OutJob"]
    found = {(f["check"], f["severity"]) for f in model["findings"]}
    assert found == {("fabrication_origin", "warning"), ("no_assembly_drawing", "info"),
                     ("no_erc_validation", "warning"), ("no_drc_validation", "warning")}
    (origin,) = _findings(model, "fabrication_origin")
    assert {(e["outjob"], e["type"]) for e in origin["examples"]} == {
        ("Assembly.OutJob", "Gerber"), ("Fabrication.OutJob", "ODB")}
    assert model["coverage"]["fabrication_data"]["formats"] == ["Gerber RS-274X", "ODB++"]


def test_mini_pc_example():
    from parsers.pcb_dwf import parse_pcb_dwf
    project = _example(r"Mini PC\Mini PC - Motherboard\MiniPC.PrjPcb")
    model = build_output_jobs(project, parse_pcb_dwf)
    assert {(j["file"], j["listed"]) for j in model["outjobs"]} == {
        ("Assembly.OutJob", True), ("Fabrication.OutJob", True), ("Job1.OutJob", False)}
    assert all(row["status"] == "connected" for row in model["coverage"].values())
    erc = _findings(model, "erc_settings_differ")
    assert len(erc) == 2 and all(f["examples"][0]["levels_differ"] == 8 for f in erc)
    assert {f["check"] for f in model["findings"]} == {
        "erc_settings_differ", "outjob_files", "wrong_document", "not_connected"}
