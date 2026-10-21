"""check_project_integrity: file checks (legacy extensions, missing documents, a leftover
.Annotation file, parts outside the sheet, compile masks, stale variant entries) and name
checks (hidden, non-ASCII and edge-space characters, bus ranges, ActiveBOM-reserved
parameters, the multi-channel designator format) on synthetic .PrjPcb, .SchDoc and
.Annotation files; Altium's public AD25 examples are snapshot-checked when installed."""
import json
import struct
from pathlib import Path
from unittest.mock import patch

import pytest

import main
from parsers.prj_pcb import (
    VariantDefinition, VariantState, parse_prj_documents, parse_prj_pcb,
)
from parsers.sch_doc import (
    SHEET_STYLES, components_from_stream, hierarchy_from_stream, integrity_from_stream,
    parse_sch_hierarchy, parse_sch_integrity,
)
from services.project_integrity import (
    CHECKS, KB, build_project_integrity, page_project_integrity,
)

EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")
A4 = "|RECORD=31|FONTIDCOUNT=1|SIZE1=10"  # no SheetStyle: A4 landscape, 11500 x 7600 mil
NBSP, ZWSP_UTF8, MICRO_UTF8 = "\xa0", "\xe2\x80\x8b", "\xc2\xb5"  # as latin-1 record text


def _record(body: str) -> bytes:
    data = body.encode("latin-1") + b"\0"
    return struct.pack("<I", len(data)) + data


class _Sheet:
    """A FileHeader stream built record by record; positions are tracked, so a child's
    OwnerIndex is its owner's position minus one."""

    def __init__(self, sheet_record: str = A4):
        self.records = ["|HEADER=Protel for Windows - Schematic Capture Binary File Version 5.0",
                        sheet_record]

    def add(self, *records: str) -> "_Sheet":
        self.records.extend(records)
        return self

    def part(self, refdes: str, x: int, y: int, params=(), pins=(), uid: str | None = None,
             extra: str = "", designator: str | None = None) -> "_Sheet":
        """A part at (x, y) in 10 mil; pins are (designator, x, y, orientation) with a
        20-unit length; params are (name, value) or raw RECORD=41 tails."""
        owner = len(self.records) - 1
        self.records.append(f"|RECORD=1|LIBREFERENCE=RES|CURRENTPARTID=1|PARTCOUNT=2"
                            f"|UNIQUEID={uid or refdes + 'UID'}|LOCATION.X={x}|LOCATION.Y={y}"
                            f"{extra}")
        for number, px, py, orientation in pins:
            self.records.append(f"|RECORD=2|OWNERINDEX={owner}|OWNERPARTID=1|DESIGNATOR={number}"
                                f"|PINLENGTH=20|PINCONGLOMERATE={orientation}"
                                f"|LOCATION.X={px}|LOCATION.Y={py}")
        text = designator if designator is not None else refdes
        self.records.append(f"|RECORD=34|OWNERINDEX={owner}|NAME=Designator|TEXT={text}"
                            f"|LOCATION.X={x}|LOCATION.Y={y + 10}")
        for param in params:
            tail = param if isinstance(param, str) else f"|NAME={param[0]}|TEXT={param[1]}"
            self.records.append(f"|RECORD=41|OWNERINDEX={owner}{tail}"
                                f"|LOCATION.X={x}|LOCATION.Y={y - 10}")
        return self

    def symbol(self, designator: str, file_name: str, entries=()) -> "_Sheet":
        owner = len(self.records) - 1
        self.records.append(f"|RECORD=15|LOCATION.X=100|LOCATION.Y=500|XSIZE=100|YSIZE=60"
                            f"|UNIQUEID=S{owner}|SYMBOLTYPE=Normal")
        for name in entries:
            self.records.append(f"|RECORD=16|OWNERINDEX={owner}|SIDE=0|DISTANCEFROMTOP=1"
                                f"|NAME={name}")
        self.records.append(f"|RECORD=32|OWNERINDEX={owner}|TEXT={designator}")
        self.records.append(f"|RECORD=33|OWNERINDEX={owner}|TEXT={file_name}")
        return self

    def label(self, text: str, x: int = 100, y: int = 100, record: str = "25") -> "_Sheet":
        key = "NAME" if record == "18" else "TEXT"
        return self.add(f"|RECORD={record}|LOCATION.X={x}|LOCATION.Y={y}|{key}={text}")

    def bytes(self) -> bytes:
        return b"".join(_record(record) for record in self.records)


def _project(tmp_path: Path, prj_text: str, sheets: dict[str, _Sheet], files=()):
    """A project folder: the .PrjPcb, placeholder files for the sheets (read from their
    streams) and for files, and the project dict main builds."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    prj = tmp_path / "Board.PrjPcb"
    prj.write_text(prj_text, encoding="utf-8")
    streams = {name.lower(): sheet.bytes() for name, sheet in sheets.items()}
    for name in [*sheets, *files]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(b"placeholder")
    data = parse_prj_pcb(str(prj))
    project = {"name": "Board", "prj_pcb_path": str(prj),
               "sheets": [{"name": Path(p).stem, "path": p} for p in data.sheet_paths]}

    def stream(path):
        name = Path(path).name.lower()
        if name not in streams:
            raise OSError("not a sheet of this test")
        return streams[name]

    return (project, lambda path: hierarchy_from_stream(stream(path)),
            lambda path: integrity_from_stream(stream(path)))


def _findings(model: dict) -> dict[tuple[str, str], dict]:
    return {(f["check"], f["kind"]): f for f in model["findings"]}


def _documents(*paths: str, start: int = 1) -> str:
    return "".join(f"[Document{i}]\nDocumentPath={path}\n"
                   for i, path in enumerate(paths, start))


# ---------- the parsers ----------

def test_sheet_sizes_follow_kicads_table():
    assert len(SHEET_STYLES) == 18 and SHEET_STYLES[0] == ("A4", 1150, 760)
    assert SHEET_STYLES[7] == ("C", 2000, 1500) and SHEET_STYLES[17] == ("OrCAD E", 4280, 3280)
    default = integrity_from_stream(_Sheet().bytes())
    assert (default.sheet_style, default.width_mil, default.height_mil) == ("A4", 11500, 7600)
    style_c = integrity_from_stream(_Sheet("|RECORD=31|SHEETSTYLE=7").bytes())
    assert (style_c.sheet_style, style_c.width_mil, style_c.height_mil) == ("C", 20000, 15000)
    portrait = integrity_from_stream(_Sheet("|RECORD=31|WORKSPACEORIENTATION=1").bytes())
    assert (portrait.width_mil, portrait.height_mil, portrait.portrait) == (7600, 11500, True)
    custom = integrity_from_stream(_Sheet(
        "|RECORD=31|SHEETSTYLE=6|USECUSTOMSHEET=T|CUSTOMX=2000|CUSTOMY=1000"
        "|CUSTOMX_FRAC=50000").bytes())
    assert (custom.sheet_style, custom.width_mil, custom.height_mil) == ("Custom", 20005, 10000)
    # CustomX/CustomY without UseCustomSheet (every AD25 example writes them) do not count.
    unused = integrity_from_stream(_Sheet("|RECORD=31|CUSTOMX=1500|CUSTOMY=950").bytes())
    assert unused.sheet_style == "A4"
    unknown = integrity_from_stream(_Sheet("|RECORD=31|SHEETSTYLE=40").bytes())
    assert unknown.sheet_style == "A4"


def test_names_are_read_as_written():
    sheet = (_Sheet()
             .part("R1", 100, 100, params=[("Value", "10k "),
                                           "|NAME=Note|TEXT=a\x96b",
                                           f"|NAME=Mu|TEXT=u|%UTF8%TEXT={MICRO_UTF8}F"],
                   designator="R1 ")
             .label(" CLK")
             .label(f"VBUS?_T|%UTF8%TEXT=VBUS{ZWSP_UTF8}_T")
             .label("P1 ", record="18")
             .label(f"VCC{NBSP}", record="17")
             .symbol("U_A", "a.SchDoc", entries=["SDA "]))
    data = integrity_from_stream(sheet.bytes())
    names = {(n.kind, n.text) for n in data.names}
    assert {("net_label", " CLK"), ("net_label", "VBUS\u200b_T"), ("port", "P1 "),
            ("power_port", "VCC\xa0"), ("designator", "R1 "), ("sheet_entry", "SDA ")} <= names
    values = {n.text: n.value for n in data.names if n.kind == "parameter"}
    # 0x96 without a %UTF8% copy is the cp1252 en dash, not a C1 control code.
    assert values == {"Value": "10k ", "Note": "a\u2013b", "Mu": "\xb5F"}
    entry = next(n for n in data.names if n.kind == "sheet_entry")
    assert (entry.owner, entry.x_mil, entry.y_mil) == ("U_A", 1000, 5000)
    # The stripped readers are unchanged.
    assert components_from_stream(sheet.bytes())["R1"].params["value"] == "10k"


def test_parts_points_and_compile_masks():
    sheet = (_Sheet()
             .part("R1", 100, 100, pins=[("1", 120, 100, 0)])
             .part("R2", 300, 300)
             .add("|RECORD=211|LOCATION.X=250|LOCATION.Y=250|CORNER.X=350|CORNER.Y=350")
             .part("R3", 600, 600)
             .add("|RECORD=211|LOCATION.X=550|LOCATION.Y=550|CORNER.X=650|CORNER.Y=650"
                  "|COLLAPSED=T"))
    owner = len(sheet.records) - 1
    sheet.part("R5", 100, 300, pins=[("1", 120, 300, 0), ("2", 5000, 300, 4)])  # 2 hidden
    # A pin of the part's alternate display mode is not drawn either.
    sheet.add(f"|RECORD=2|OWNERINDEX={owner}|OWNERPARTID=1|OWNERPARTDISPLAYMODE=1"
              "|DESIGNATOR=3|PINLENGTH=20|LOCATION.X=6000|LOCATION.Y=300")
    data = integrity_from_stream(sheet.bytes())
    parts = {p.designator: p for p in data.parts}
    assert parts["R1"].points == [(1000, 1000), (1400, 1000)]
    assert parts["R5"].points == [(1000, 3000), (1400, 3000)]
    assert (parts["R2"].masked, parts["R3"].masked) == (True, False)
    assert data.components["R2"].compile_masked and not data.components["R3"].compile_masked


def test_project_documents_keep_every_type(tmp_path):
    prj = tmp_path / "Board.PrjPcb"
    prj.write_text(_documents("a.SchDoc", "old.S01", "lib\\x.SchLib")
                   + "[DeviceSheet1]\nDocumentPath=Dev\\d.SchDoc\n", encoding="utf-8")
    documents = parse_prj_documents(str(prj))
    assert [d.section for d in documents] == ["Document1", "Document2", "Document3",
                                              "DeviceSheet1"]
    assert documents[2].raw == "lib\\x.SchLib"
    assert documents[2].path == str((tmp_path / "lib" / "x.SchLib").resolve())


# ---------- file checks ----------

def test_legacy_extensions_and_missing_documents(tmp_path):
    prj = (_documents("a.SchDoc", "old.S01", "older.sch", "Board.PCB", "gone.SchDoc",
                      "Board.PcbDoc", "Wires.Harness", "out\\Fab.OutJob")
           + "[DeviceSheet1]\nDocumentPath=Dev\\Missing.SchDoc\n")
    (tmp_path / "older.sch").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "older.sch").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest")
    project, read_h, read_i = _project(tmp_path, prj, {"a.SchDoc": _Sheet()},
                                       files=["old.S01", "Board.PCB"])
    model = build_project_integrity(project, read_h, read_i)
    found = _findings(model)
    legacy = found[("legacy_extension", "legacy_schematic_extension")]
    assert legacy["severity"] == "error" and legacy["source"] == KB["legacy_extension"]
    assert [(e["document"], e["altium_binary"]) for e in legacy["examples"]] == [
        ("old.S01", False), ("older.sch", True)]
    pcb = found[("legacy_extension", "legacy_pcb_extension")]
    # The KB names the schematic extensions as the cause, not the PCB's.
    assert pcb["count"] == 1 and pcb["severity"] == "warning"
    missing = found[("missing_document", "missing_schematic")]
    assert {e["document"] for e in missing["examples"]} == {"gone.SchDoc", "Wires.Harness"}
    assert missing["severity"] == "error"
    device = found[("missing_document", "missing_device_sheet")]
    assert device["examples"][0]["section"] == "DeviceSheet1"
    # Altium also looks in the Device Sheets folders set in Preferences.
    assert device["severity"] == "warning"
    other = found[("missing_document", "missing_document")]
    assert other["severity"] == "warning"
    assert {e["document"] for e in other["examples"]} == {"Board.PcbDoc", "out\\Fab.OutJob"}
    assert "sheets_not_read" not in model  # missing sheets are findings, not read errors
    assert model["findings"][0]["severity"] == "error"


def test_missing_legacy_schematic_is_a_missing_sheet(tmp_path):
    project, read_h, read_i = _project(tmp_path, _documents("a.SchDoc", "lost.S02"),
                                       {"a.SchDoc": _Sheet()})
    found = _findings(build_project_integrity(project, read_h, read_i))
    assert found[("legacy_extension", "legacy_schematic_extension")]["examples"] == [
        {"document": "lost.S02", "section": "Document2", "on_disk": False}]
    missing = found[("missing_document", "missing_schematic")]
    assert missing["examples"] == [{"document": "lost.S02", "section": "Document2",
                                    "type": ".s02"}]


ANNOTATION = (
    "[DesignatorManager]\n"
    "LogicalDesignator1=R1\nLogicalPartID1=1\nDocumentName1=a.SchDoc\nChannelName1=\n"
    "UniqueID1=\\R1UID\nPhysicalDesignator1=R101\n"
    "LogicalDesignator2=R2\nLogicalPartID2=1\nDocumentName2=a.SchDoc\nChannelName2=\n"
    "UniqueID2=\\R2UID\nPhysicalDesignator2=R2\n"
    "LogicalDesignator3=R9\nLogicalPartID3=1\nDocumentName3=a.SchDoc\nChannelName3=\n"
    "UniqueID3=\\GONEUID\nPhysicalDesignator3=R9\n"
)


def test_leftover_annotation_file_on_a_flat_design(tmp_path):
    sheet = _Sheet().part("R1", 100, 100).part("R2", 200, 100)
    project, read_h, read_i = _project(
        tmp_path, _documents("a.SchDoc", "Board.Annotation"), {"a.SchDoc": sheet})
    (tmp_path / "Board.Annotation").write_text(ANNOTATION, encoding="utf-8")
    found = _findings(build_project_integrity(project, read_h, read_i))
    renames = found[("annotation_file", "annotation_renames")]
    assert renames["severity"] == "warning" and renames["source"] == KB["annotation_file"]
    assert renames["examples"] == [{"sheet": "a", "designator": "R1", "board_designator": "R101"}]
    stale = found[("annotation_file", "annotation_stale")]
    assert [e["designator"] for e in stale["examples"]] == ["R9"]


def test_idle_and_unlisted_annotation_files(tmp_path):
    idle = ANNOTATION.replace("PhysicalDesignator1=R101", "PhysicalDesignator1=R1")
    project, read_h, read_i = _project(
        tmp_path / "idle", _documents("a.SchDoc", "Board.Annotation"),
        {"a.SchDoc": _Sheet().part("R1", 100, 100)})
    (tmp_path / "idle" / "Board.Annotation").write_text(idle, encoding="utf-8")
    found = _findings(build_project_integrity(project, read_h, read_i, checks=["annotation_file"]))
    assert found[("annotation_file", "annotation_idle")]["severity"] == "info"
    assert ("annotation_file", "annotation_renames") not in found

    project, read_h, read_i = _project(tmp_path / "unlisted", _documents("a.SchDoc"),
                                       {"a.SchDoc": _Sheet()})
    (tmp_path / "unlisted" / "board.annotation").write_text(ANNOTATION, encoding="utf-8")
    found = _findings(build_project_integrity(project, read_h, read_i, checks=["annotation_file"]))
    assert list(found) == [("annotation_file", "annotation_not_listed")]


@pytest.mark.parametrize("on_disk", [False, True])  # missing, or there but unreadable
def test_no_stale_annotation_entries_while_a_sheet_is_not_read(tmp_path, on_disk):
    sheet = _Sheet().part("R1", 100, 100).part("R2", 200, 100)
    project, read_h, read_i = _project(
        tmp_path, _documents("a.SchDoc", "b.SchDoc", "Board.Annotation"), {"a.SchDoc": sheet},
        files=["b.SchDoc"] if on_disk else [])
    (tmp_path / "Board.Annotation").write_text(ANNOTATION, encoding="utf-8")
    found = _findings(build_project_integrity(project, read_h, read_i,
                                              checks=["annotation_file"]))
    # GONEUID may be on the sheet that was not read.
    assert ("annotation_file", "annotation_stale") not in found
    assert ("annotation_file", "annotation_renames") in found


def _channels(fmt: str | None, child: _Sheet, separator: str = "_") -> str:
    design = "[Design]\n"
    if fmt is not None:
        design += f"ChannelDesignatorFormatString={fmt}\nChannelRoomLevelSeperator={separator}\n"
    return design + _documents("top.SchDoc", "ch.SchDoc", "Board.Annotation")


def _multi_channel(tmp_path, fmt, child=None, separator="_"):
    child = child or (_Sheet().part("R1", 100, 100)
                      .add("|RECORD=26|LOCATIONCOUNT=2|X1=10|Y1=10|X2=100|Y2=10")
                      .label("D[0..7]"))
    top = _Sheet().symbol("Repeat(CH,1,2)", "ch.SchDoc")
    project, read_h, read_i = _project(tmp_path, _channels(fmt, child, separator),
                                       {"top.SchDoc": top, "ch.SchDoc": child})
    # One entry per channel copy of R1, as Altium writes them for a Repeat symbol.
    (tmp_path / "Board.Annotation").write_text(
        "[DesignatorManager]\n"
        "LogicalDesignator1=R1\nDocumentName1=ch.SchDoc\nChannelName1=CH1\n"
        "UniqueID1=\\S1\\R1UID\nPhysicalDesignator1=R1_1\n"
        "LogicalDesignator2=R1\nDocumentName2=ch.SchDoc\nChannelName2=CH2\n"
        "UniqueID2=\\S1\\R1UID\nPhysicalDesignator2=R1_2\n", encoding="utf-8")
    return build_project_integrity(project, read_h, read_i)


def test_annotation_of_a_multi_channel_design_is_needed(tmp_path):
    model = _multi_channel(tmp_path, "$RoomName_$Component")
    found = _findings(model)
    assert ("annotation_file", "annotation_renames") not in found
    assert any("multi-channel" in note for note in model["notes"])


def test_parts_outside_the_sheet(tmp_path):
    sheet = (_Sheet()
             .part("R1", 100, 100)                                   # inside A4
             .part("R2", 2000, 100, pins=[("1", 2010, 100, 0)])      # x 20000 mil: outside
             .part("R3", 1140, 100, pins=[("1", 1145, 100, 0)])      # pin end 11650: partly
             .part("G1", 3000, 100, extra="|COMPONENTKIND=2")        # graphical: not compiled
             .part("R4", 3000, 3000)                                 # masked: not compiled
             .add("|RECORD=211|LOCATION.X=2900|LOCATION.Y=2900|CORNER.X=3100|CORNER.Y=3100")
             .part("U1", 1140, 200, pins=[("1", 1145, 200, 0)])      # part A: partly
             .part("U1", 2000, 200))                                 # part B: outside
    big = _Sheet("|RECORD=31|USECUSTOMSHEET=T|CUSTOMX=2500|CUSTOMY=1000").part("R5", 2000, 100)
    project, read_h, read_i = _project(
        tmp_path, "[Electrical Rules Check]\nType2=0\n" + _documents("a.SchDoc", "b.SchDoc"),
        {"a.SchDoc": sheet, "b.SchDoc": big})
    found = _findings(build_project_integrity(project, read_h, read_i,
                                              checks=["outside_sheet_border"]))
    outside = found[("outside_sheet_border", "outside")]
    assert outside["severity"] == "warning"
    assert outside["examples"] == [
        {"sheet": "a", "designator": "R2", "x_mil": 20000.0, "y_mil": 1000.0,
         "sheet_size": "A4 11500 x 7600 mil"},
        # Part A only reaches past the edge; part B is fully outside, which wins.
        {"sheet": "a", "designator": "U1", "x_mil": 20000.0, "y_mil": 2000.0,
         "sheet_size": "A4 11500 x 7600 mil"}]
    assert outside["erc"]["check"] == "Object not completely within sheet boundaries"
    assert outside["erc"]["level"] == "No Report" and "does not report" in outside["erc"]["note"]
    partly = found[("outside_sheet_border", "partly_outside")]
    assert [e["designator"] for e in partly["examples"]] == ["R3"]
    assert partly["severity"] == "info"


def test_custom_portrait_sheet_is_read_both_ways(tmp_path):
    # KiCad's importer turns a custom size on a portrait sheet; Altium does not document it.
    sheet = (_Sheet("|RECORD=31|USECUSTOMSHEET=T|CUSTOMX=1000|CUSTOMY=2000"
                    "|WORKSPACEORIENTATION=1")
             .part("R1", 1500, 500)     # inside 20000 x 10000 only
             .part("R2", 500, 1500)     # inside 10000 x 20000 only
             .part("R3", 2500, 500))    # inside neither
    project, read_h, read_i = _project(tmp_path, _documents("a.SchDoc"), {"a.SchDoc": sheet})
    found = _findings(build_project_integrity(project, read_h, read_i,
                                              checks=["outside_sheet_border"]))
    assert list(found) == [("outside_sheet_border", "outside")]
    assert [e["designator"] for e in found[("outside_sheet_border", "outside")]["examples"]] == [
        "R3"]


def test_compile_masks(tmp_path):
    sheet = (_Sheet()
             .add("|RECORD=211|LOCATION.X=250|LOCATION.Y=250|CORNER.X=350|CORNER.Y=350")
             .part("R2", 300, 300)
             .part("R?", 320, 320)  # unannotated: no designator to report
             .part("R7", 310, 310, uid="MASKED7")
             .part("R7", 600, 600, uid="LIVE7"))
    project, read_h, read_i = _project(tmp_path, _documents("a.SchDoc"), {"a.SchDoc": sheet})
    found = _findings(build_project_integrity(project, read_h, read_i, checks=["compile_mask"]))
    masked = found[("compile_mask", "masked_parts")]
    assert masked["severity"] == "info"
    assert masked["examples"] == [{"sheet": "a", "designator": "R2", "x_mil": 3000.0,
                                   "y_mil": 3000.0}]
    copies = found[("compile_mask", "masked_copies")]
    assert copies["examples"] == [{"sheet": "a", "designator": "R7", "x_mil": 3100.0,
                                   "y_mil": 3100.0}]


VARIANTS = (
    "[ProjectVariant1]\nDescription=Lite\nVariationCount=5\n"
    "Variation1=Designator=R1|UniqueId=\\R1UID|Kind=1|AlternatePart=\n"
    "Variation2=Designator=R9|UniqueId=\\GONEUID|Kind=1|AlternatePart=\n"
    "Variation3=Designator=R5|UniqueId=\\R2UID|Kind=1|AlternatePart=\n"
    "Variation4=Designator=R3|UniqueId=\\NOPE|Kind=1|AlternatePart=\n"
    "Variation5=Designator=R?|UniqueId=\\ZZZ|Kind=1|AlternatePart=\n"
)


def test_stale_variant_entries(tmp_path):
    sheet = _Sheet().part("R1", 100, 100).part("R2", 200, 100).part("R3", 300, 100)
    project, read_h, read_i = _project(tmp_path, _documents("a.SchDoc") + VARIANTS,
                                       {"a.SchDoc": sheet})
    model = build_project_integrity(project, read_h, read_i, checks=["variant_entry"])
    found = _findings(model)
    assert model["mode"] == "files"
    gone = found[("variant_entry", "no_such_part")]
    # The unresolved Not Fitted entry of an unannotated part (R?) is one of them.
    assert {e["designator"] for e in gone["examples"]} == {"R9", "R?"}
    assert gone["erc"] == {"check": "Incorrect link in project variant",
                           "level": "Warning (default)"}  # no [Electrical Rules Check]
    changed = found[("variant_entry", "designator_changed")]
    assert changed["examples"][0]["designator"] == "R5" and "R2" in changed["examples"][0]["detail"]
    assert found[("variant_entry", "unique_id_not_found")]["examples"][0]["designator"] == "R3"
    assert all(f["severity"] == "warning" for f in model["findings"])


def test_variant_entries_bind_to_the_compiled_designators(tmp_path):
    sheet = _Sheet().part("R1", 100, 100)
    project, read_h, read_i = _project(
        tmp_path, _documents("a.SchDoc") + "[ProjectVariant1]\nDescription=Lite\n"
        "Variation1=Designator=R1|UniqueId=\\R1UID|Kind=1|AlternatePart=\n",
        {"a.SchDoc": sheet})
    netlist = {"pin_to_net": {}, "parts": {"R101": {}},
               "components": {"R101": {"unique_id": "R1UID", "unique_ids": ["R1UID"],
                                       "sheet": "a", "logical_refdes": "R1"}}}
    model = build_project_integrity(project, read_h, read_i, netlist=netlist,
                                    checks=["variant_entry"])
    assert model["mode"] == "netlist"
    changed = _findings(model)[("variant_entry", "designator_changed")]
    assert "R101" in changed["examples"][0]["detail"]


def test_older_variant_format_is_checked(tmp_path):
    # [Variation]/[CompVar] variants carry only Not Fitted designators.
    prj = (_documents("a.SchDoc", "b.SchDoc")
           + "[Variation1]\nVariantName=Lite\n"
           "[CompVar1]\nVariantKind=3\nRefDesignator1=R1\n"
           "[CompVar2]\nVariantKind=3\nRefDesignator1=R99\n")
    project, read_h, read_i = _project(tmp_path, prj, {"a.SchDoc": _Sheet().part("R1", 100, 100)})
    model = build_project_integrity(project, read_h, read_i, checks=["variant_entry"])
    assert "not_applicable" not in model
    gone = _findings(model)[("variant_entry", "no_such_part")]
    assert [(e["variant"], e["designator"]) for e in gone["examples"]] == [("Lite", "R99")]
    # R99 may be on b.SchDoc, which is not on disk.
    assert any("missing on disk" in note for note in model["notes"])


def test_multi_channel_variant_entries_without_channel_names(tmp_path):
    child = _Sheet().part("R1", 100, 100)
    top = _Sheet().symbol("Repeat(CH,1,2)", "ch.SchDoc")
    prj = (_documents("top.SchDoc", "ch.SchDoc") + "[ProjectVariant1]\nDescription=Lite\n"
           "Variation1=Designator=R1_2|UniqueId=\\S1\\R1UID|Kind=1|AlternatePart=\n")
    project, read_h, read_i = _project(tmp_path, prj, {"top.SchDoc": top, "ch.SchDoc": child})
    model = build_project_integrity(project, read_h, read_i, checks=["variant_entry"])
    # Without a netlist or .Annotation file R1_2 is unknown: not reported as changed.
    assert model["findings"] == []
    assert any("channel designators" in note for note in model["notes"])


def test_no_variants_is_not_applicable(tmp_path):
    project, read_h, read_i = _project(tmp_path, _documents("a.SchDoc"), {"a.SchDoc": _Sheet()})
    model = build_project_integrity(project, read_h, read_i, checks=["variant_entry"])
    assert model["findings"] == [] and "variant_entry" in model["not_applicable"]
    assert model["summary"]["clean"] == ["variant_entry"]


# ---------- name checks ----------

NAMES = (_Sheet()
         .part("R1", 100, 100, params=[("Value", "100n "), ("Packaging", f"Tube{NBSP}"),
                                       ("Note", "a\x96b"), "|NAME=Warto?c|%UTF8%NAME=Warto"
                                       "\xc5\x9b\xc4\x87|TEXT=1"])
         .part("R7", 200, 100, designator="R7\x01")
         .part("U1", 300, 100, designator="U1 ")
         .label(f"VBUS?_T|%UTF8%TEXT=VBUS{ZWSP_UTF8}_T")
         .label("CLK ")
         .label(f"?C_RX|%UTF8%TEXT={MICRO_UTF8}C_RX")
         .label("D[0..7]").label("D[0..7").label("A[0:3]").label("LED[1]")
         .label(f"VCC{NBSP}", record="17")
         .symbol("U_A", "b.SchDoc", entries=["Repeat(D[0..3])", "SDA[1..2"]))


def _names_model(tmp_path, **kwargs):
    project, read_h, read_i = _project(tmp_path, _documents("a.SchDoc"), {"a.SchDoc": NAMES})
    return build_project_integrity(project, read_h, read_i, **kwargs)


def test_hidden_characters(tmp_path):
    found = _findings(_names_model(tmp_path, checks=["hidden_characters"]))
    label = found[("hidden_characters", "net_label")]
    assert label["severity"] == "error" and label["source"] == KB["hidden_characters"]
    assert label["examples"][0]["shown"] == "VBUS<U+200B>_T"
    assert label["characters"] == {"U+200B ZERO WIDTH SPACE": 1}
    assert found[("hidden_characters", "power_port")]["characters"] == {
        "U+00A0 NO-BREAK SPACE": 1}
    assert found[("hidden_characters", "designator")]["examples"][0]["shown"] == "R7<U+0001>"
    value = found[("hidden_characters", "parameter_value")]
    assert value["severity"] == "warning"
    # Only the NBSP: the cp1252 en dash (0x96) is not a control code.
    assert [(e["designator"], e["parameter"]) for e in value["examples"]] == [("R1", "Packaging")]


def test_non_ascii(tmp_path):
    found = _findings(_names_model(tmp_path, checks=["non_ascii"]))
    assert found[("non_ascii", "net_label")]["characters"] == {"U+00B5 MICRO SIGN": 1}
    assert found[("non_ascii", "net_label")]["severity"] == "warning"
    name = found[("non_ascii", "parameter")]
    assert name["severity"] == "info" and name["examples"][0]["parameter"] == "Wartość"
    assert ("non_ascii", "parameter_value") not in found  # values may carry micro and ohm


def test_whitespace(tmp_path):
    found = _findings(_names_model(tmp_path, checks=["whitespace"]))
    label = found[("whitespace", "net_label")]
    assert (label["severity"], label["examples"][0]["name"], label["examples"][0]["where"]) == (
        "warning", '"CLK "', "trailing")
    assert found[("whitespace", "designator")]["examples"][0]["designator"] == '"U1 "'
    value = found[("whitespace", "parameter_value")]
    assert value["severity"] == "info" and value["examples"][0]["value"] == '"100n "'


def test_bus_ranges(tmp_path):
    found = _findings(_names_model(tmp_path, checks=["net_name_syntax"]))
    rows = found[("net_name_syntax", "bus_range")]["examples"]
    # D[0..7], LED[1] and the Repeat(D[0..3]) entry are fine.
    assert sorted(row["name"] for row in rows) == ["A[0:3]", "D[0..7", "SDA[1..2"]
    assert found[("net_name_syntax", "bus_range")]["erc"]["check"] == "Bus range syntax errors"


def test_reserved_bom_parameters(tmp_path):
    sheet = (_Sheet()
             .part("R1", 100, 100, params=[("Manufacturer Part Number 1", "ABC-1"),
                                           ("Manufacturer 1", "Acme")])
             .part("R2", 200, 100, params=[("Manufacturer Part Number", "XYZ"),
                                           ("Manufacturer Part Number 2", "XYZ-2"),
                                           ("Manufacturer 3", "*")])
             .part("R3", 300, 100, params=[("MPN 1", "ok"), ("Manufacturer Part Number 1x", "y")]))
    project, read_h, read_i = _project(tmp_path, _documents("a.SchDoc", "Board.BomDoc"),
                                       {"a.SchDoc": sheet}, files=["Board.BomDoc"])
    found = _findings(build_project_integrity(project, read_h, read_i,
                                              checks=["reserved_bom_parameter"]))
    reserved = found[("reserved_bom_parameter", "reserved_name")]
    assert reserved["severity"] == "info" and reserved["bomdoc_in_project"] is True
    assert [(e["designator"], e["parameter"]) for e in reserved["examples"]] == [
        ("R1", "Manufacturer Part Number 1"), ("R1", "Manufacturer 1"),
        ("R2", "Manufacturer Part Number 2")]
    copilot = found[("reserved_bom_parameter", "copilot_mpn_from_reserved_name")]
    assert [(e["designator"], e["value"]) for e in copilot["examples"]] == [("R1", "ABC-1")]
    # The copilot's MPN really comes from it.
    assert components_from_stream(sheet.bytes())["R1"].mpn == "ABC-1"


def test_copilot_mpn_follows_the_sheet_parser(tmp_path):
    mpn1, mpn2 = "Manufacturer Part Number 1", "Manufacturer Part Number 2"
    sheet = (_Sheet()
             .part("R4", 100, 100, params=[(mpn1, '"Q"'), (mpn2, "REAL")])  # quoted: skipped
             .part("R5", 200, 100, params=[(mpn1, "*"), (mpn2, "OTHER")])   # '*' is read
             .part("R6", 300, 100, params=[(mpn2, "TWO"), (mpn1, "ONE")])   # lowest N wins
             .part("R?", 400, 100, params=[(mpn1, "UNANNOTATED")]))
    project, read_h, read_i = _project(tmp_path, _documents("a.SchDoc"), {"a.SchDoc": sheet})
    found = _findings(build_project_integrity(project, read_h, read_i,
                                              checks=["reserved_bom_parameter"]))
    rows = found[("reserved_bom_parameter", "copilot_mpn_from_reserved_name")]["examples"]
    assert [(e["designator"], e["parameter"], e["value"]) for e in rows] == [
        ("R4", mpn2, "REAL"), ("R5", mpn1, "*"), ("R6", mpn1, "ONE")]
    components = components_from_stream(sheet.bytes())
    assert [components[e["designator"]].mpn for e in rows] == ["REAL", "*", "ONE"]
    reserved = found[("reserved_bom_parameter", "reserved_name")]["examples"]
    assert "R?" not in {e["designator"] for e in reserved}


def test_channel_format_after_bus_names(tmp_path):
    found = _findings(_multi_channel(tmp_path / "suffix", "$Component_$ChannelIndex"))
    bus = found[("channel_designator_format", "channel_text_after_bus_names")]
    assert bus["severity"] == "warning" and bus["source"] == KB["channel_designator_format"]
    assert bus["examples"] == [{"sheet": "ch", "compiled_copies": 2, "bus_wires": 1,
                                "bus_names": ["D[0..7]"], "bus_names_total": 1}]
    # The KB's fix, and a channel sheet without buses, are clean.
    assert not _multi_channel(tmp_path / "prefix", "$RoomName_$Component")["findings"]
    plain = _multi_channel(tmp_path / "plain", "$Component_$ChannelIndex",
                           child=_Sheet().part("R1", 100, 100))
    assert not [f for f in plain["findings"] if f["check"] == "channel_designator_format"]
    # Not written: Altium's default $Component_$RoomName is assumed.
    default = _findings(_multi_channel(tmp_path / "default", None))
    assert default[("channel_designator_format", "channel_text_after_bus_names")][
        "designator_format_written"] is False


def test_channel_format_characters(tmp_path):
    found = _findings(_multi_channel(tmp_path, "$RoomName[$Component]", separator=","))
    odd = found[("channel_designator_format", "format_characters")]
    assert odd["examples"][0]["characters"] == ["U+002C COMMA", "U+005B LEFT SQUARE BRACKET",
                                                "U+005D RIGHT SQUARE BRACKET"]


def test_channel_format_needs_a_multi_channel_design(tmp_path):
    model = _names_model(tmp_path, checks=["channel_designator_format"])
    assert model["findings"] == []
    assert "multi-channel" in model["not_applicable"]["channel_designator_format"]


# ---------- the model and the tool ----------

def test_checks_filter_and_errors(tmp_path):
    model = _names_model(tmp_path, checks=["whitespace", " Whitespace "])
    assert model["checks_run"] == ["whitespace"]
    assert {f["check"] for f in model["findings"]} == {"whitespace"}
    assert _names_model(tmp_path, checks=["nope"])["error"] == "invalid_checks"
    project = {"name": "x", "prj_pcb_path": str(tmp_path / "missing.PrjPcb"), "sheets": []}
    assert build_project_integrity(project, parse_sch_hierarchy, parse_sch_integrity)[
        "error"] == "project_file_unreadable"


def test_every_check_runs_and_findings_sort_worst_first(tmp_path):
    model = _names_model(tmp_path)
    assert model["checks_run"] == list(CHECKS)
    ranks = [{"error": 0, "warning": 1, "info": 2}[f["severity"]] for f in model["findings"]]
    assert ranks == sorted(ranks)
    summary = model["summary"]
    assert summary["by_check"]["hidden_characters"]["worst"] == "error"
    assert sum(summary["severity_counts"].values()) == len(model["findings"])
    assert all(f["fix"] and f["examples"] for f in model["findings"])


def test_unreadable_sheet_is_reported(tmp_path):
    project, read_h, read_i = _project(tmp_path, _documents("a.SchDoc", "b.SchDoc"),
                                       {"a.SchDoc": _Sheet()}, files=["b.SchDoc"])
    model = build_project_integrity(project, read_h, read_i)
    assert model["sheets_not_read"] == [{"sheet": "b", "error": "OSError: not a sheet of this test"}]


def test_paging_cuts_examples(tmp_path):
    model = _names_model(tmp_path)
    first = page_project_integrity(model, 0, 2, 1)
    assert first["has_more"] and first["next_offset"] == 2 and "notes" in first
    assert all(len(f["examples"]) <= 1 for f in first["findings"])
    last = page_project_integrity(model, len(model["findings"]) - 1, 2, 5)
    assert not last["has_more"] and "notes" not in last and "summary" in last


def test_tool_needs_a_project():
    with patch("main._project", None):
        assert json.loads(main.check_project_integrity())["error"] == "no_project"


def test_tool_renders_pages_and_load_diagnostics(tmp_path):
    project, read_h, read_i = _project(tmp_path, _documents("a.SchDoc"), {"a.SchDoc": NAMES})
    netlist = {"pin_to_net": {"R1": {}}, "components": {"R1": {"sheet": "a"}},
               "sheet_errors": [{"sheet": "z", "error": "boom"}]}
    with patch("main._project", project), \
         patch("main._altium") as altium, \
         patch("main._variant_state", VariantState([VariantDefinition(name="Default")])), \
         patch.object(main._design_notes, "hierarchy", read_h), \
         patch.object(main._design_notes, "integrity", read_i):
        altium._netlist = None
        result = json.loads(main.check_project_integrity())
        assert result["prj_pcb"] == "Board.PrjPcb" and result["mode"] == "files"
        assert "load_diagnostics" not in result
        assert json.loads(main.check_project_integrity(checks=["x"]))["error"] == "invalid_checks"
        assert json.loads(main.check_project_integrity(offset=3))["offset"] == 3
        altium._netlist = netlist
        result = json.loads(main.check_project_integrity(checks=["whitespace"]))
        assert result["load_diagnostics"]["sheet_errors"] == [{"sheet": "z", "error": "boom"}]


# ---------- Altium's public examples ----------

def _example(relative: str) -> dict:
    path = EXAMPLES / relative
    if not path.exists():
        pytest.skip("Altium AD25 examples not installed")
    data = parse_prj_pcb(str(path))
    project = {"name": path.stem, "prj_pcb_path": str(path),
               "sheets": [{"name": Path(p).stem, "path": p} for p in data.sheet_paths]}
    return build_project_integrity(project, parse_sch_hierarchy, parse_sch_integrity)


def test_example_mini_pc_has_one_hidden_character():
    model = _example(r"Mini PC\Mini PC - Motherboard\MiniPC.PrjPcb")
    assert [(f["check"], f["kind"]) for f in model["findings"]] == [
        ("hidden_characters", "parameter_value")]
    example = model["findings"][0]["examples"][0]
    assert (example["sheet"], example["designator"], example["parameter"]) == (
        "Arria10_Power", "U13", "Packaging")
    assert example["characters"] == ["U+00A0 NO-BREAK SPACE"]


def test_example_bluetooth_sentinel_is_multi_channel_and_clean():
    model = _example(r"Bluetooth Sentinel\Bluetooth_Sentinel.PrjPcb")
    assert model["findings"] == []
    assert "channel_designator_format" not in model.get("not_applicable", {})
    assert any("multi-channel" in note for note in model["notes"])


@pytest.mark.parametrize("relative", [
    r"Simulation Quick Start\Quick start  Help for beginners.PrjPcb",  # a portrait A4 sheet
    r"SpiritLevel-SL1\SL1 Xilinx Spartan-IIE PQ208 Rev1.02.PrjPcb",    # styles A, B and C
    r"Harness Demo MB\Harness_Demo_PCB_A\Harness_Demo_PCB_A.PrjPcb",
])
def test_examples_have_every_part_on_its_sheet(relative):
    model = _example(relative)
    assert model["findings"] == []
