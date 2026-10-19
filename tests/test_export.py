import base64
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from export import SCHEMA_VERSION, _create_schema, export_project
from parsers.sch_doc import SheetAnnotation, SheetAnnotations, SheetDirective
from services.design_notes import DesignNotesCache, note_rows
from tests.draftsman_fixture import FIXTURE as DRAWING
from tests.draftsman_fixture import TINY_PNG

FIXTURES = Path(__file__).parent / "fixtures"


def test_schema_version_is_2():
    assert SCHEMA_VERSION == 2


def test_create_schema_tables(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    _create_schema(conn)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()
    assert tables == {"project", "sheets", "variants", "components", "nets", "pins", "notes"}


def test_create_schema_indices(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    _create_schema(conn)
    indices = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    conn.close()
    assert "idx_pins_net_name" in indices
    assert "idx_pins_component_id" in indices
    assert "idx_components_sheet_id" in indices
    assert "idx_notes_sheet_id" in indices


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

class _FakeVariant:
    def __init__(self, name, dnp_refdes=None):
        self.name = name
        self.dnp_refdes = dnp_refdes or []


class _FakeVariantState:
    def __init__(self, variants):
        self._variants = variants


def _minimal_project(tmp_path):
    return {
        "name": "TestBoard",
        "root_dir": str(tmp_path),
        "sheets": [
            {"name": "MCU", "path": "MCU.SchDoc"},
            {"name": "Comms", "path": "Comms.SchDoc"},
        ],
    }


def _minimal_netlist():
    return {
        "nets": {
            "MCU_TX": [("U1", "PA9"), ("R1", "1")],
            "GND": [("U1", "GND"), ("R1", "2")],
        },
        "pin_to_net": {
            "U1": {"PA9": "MCU_TX", "GND": "GND"},
            "R1": {"1": "MCU_TX", "2": "GND"},
        },
        "components": {
            "U1": {
                "mpn": "STM32G474",
                "description": "MCU",
                "value": None,
                "sheet": "MCU",
                "pins": {
                    "PA9": {"name": "PA9", "net": "MCU_TX"},
                    "GND": {"name": "GND", "net": "GND"},
                },
            },
            "R1": {
                "mpn": "RC0402",
                "description": "RES 10K",
                "value": "10K",
                "sheet": "Comms",
                "pins": {
                    "1": {"name": "~", "net": "MCU_TX"},
                    "2": {"name": "~", "net": "GND"},
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# project / sheets / variants / nets rows
# ---------------------------------------------------------------------------

def test_export_creates_db_file(tmp_path):
    vs = _FakeVariantState([_FakeVariant("Default")])
    db_path = export_project(_minimal_project(tmp_path), _minimal_netlist(), vs, "0.1.10")
    assert db_path == str(tmp_path / "TestBoard-pcb-copilot.db")
    assert Path(db_path).exists()


def test_export_project_row(tmp_path):
    vs = _FakeVariantState([_FakeVariant("Default"), _FakeVariant("Lite", ["R1"])])
    export_project(_minimal_project(tmp_path), _minimal_netlist(), vs, "0.1.10")
    conn = sqlite3.connect(str(tmp_path / "TestBoard-pcb-copilot.db"))
    row = dict(zip(
        ["id", "name", "root_dir", "exported_at", "exported_by", "schema_version",
         "sheet_count", "component_count", "net_count"],
        conn.execute("SELECT * FROM project LIMIT 1").fetchone(),
    ))
    conn.close()
    assert row["name"] == "TestBoard"
    assert row["schema_version"] == SCHEMA_VERSION
    assert row["exported_by"] == "altium-copilot v0.1.10"
    assert row["sheet_count"] == 2
    assert row["component_count"] == 2
    assert row["net_count"] == 2


def test_export_sheets_rows(tmp_path):
    vs = _FakeVariantState([_FakeVariant("Default")])
    export_project(_minimal_project(tmp_path), _minimal_netlist(), vs, "0.1.0")
    conn = sqlite3.connect(str(tmp_path / "TestBoard-pcb-copilot.db"))
    names = [r[0] for r in conn.execute("SELECT name FROM sheets ORDER BY id").fetchall()]
    conn.close()
    assert names == ["MCU", "Comms"]


def test_export_variants_rows(tmp_path):
    vs = _FakeVariantState([_FakeVariant("Default"), _FakeVariant("Lite", ["R1"])])
    export_project(_minimal_project(tmp_path), _minimal_netlist(), vs, "0.1.0")
    conn = sqlite3.connect(str(tmp_path / "TestBoard-pcb-copilot.db"))
    rows = conn.execute("SELECT name, dnp_refdes FROM variants ORDER BY id").fetchall()
    conn.close()
    assert rows[0][0] == "Default"
    assert json.loads(rows[0][1]) == []
    assert rows[1][0] == "Lite"
    assert json.loads(rows[1][1]) == ["R1"]


def test_export_nets_rows(tmp_path):
    vs = _FakeVariantState([_FakeVariant("Default")])
    export_project(_minimal_project(tmp_path), _minimal_netlist(), vs, "0.1.0")
    conn = sqlite3.connect(str(tmp_path / "TestBoard-pcb-copilot.db"))
    nets = {r[0]: r[1] for r in conn.execute("SELECT name, pin_count FROM nets").fetchall()}
    conn.close()
    assert nets["MCU_TX"] == 2
    assert nets["GND"] == 2


# ---------------------------------------------------------------------------
# components and pins rows
# ---------------------------------------------------------------------------

def test_export_components_rows(tmp_path):
    vs = _FakeVariantState([_FakeVariant("Default")])
    export_project(_minimal_project(tmp_path), _minimal_netlist(), vs, "0.1.0")
    conn = sqlite3.connect(str(tmp_path / "TestBoard-pcb-copilot.db"))
    comps = {
        r[0]: {"mpn": r[1], "description": r[2], "value": r[3], "sheet_id": r[4]}
        for r in conn.execute(
            "SELECT refdes, mpn, description, value, sheet_id FROM components"
        ).fetchall()
    }
    sheets = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM sheets").fetchall()}
    conn.close()
    assert "U1" in comps
    assert comps["U1"]["mpn"] == "STM32G474"
    assert sheets[comps["U1"]["sheet_id"]] == "MCU"
    assert "R1" in comps
    assert comps["R1"]["value"] == "10K"
    assert sheets[comps["R1"]["sheet_id"]] == "Comms"


def test_export_pins_rows(tmp_path):
    vs = _FakeVariantState([_FakeVariant("Default")])
    export_project(_minimal_project(tmp_path), _minimal_netlist(), vs, "0.1.0")
    conn = sqlite3.connect(str(tmp_path / "TestBoard-pcb-copilot.db"))
    comp_id = conn.execute("SELECT id FROM components WHERE refdes='U1'").fetchone()[0]
    pins = {
        r[0]: {"pin_name": r[1], "net_name": r[2]}
        for r in conn.execute(
            "SELECT pin_number, pin_name, net_name FROM pins WHERE component_id=?", (comp_id,)
        ).fetchall()
    }
    conn.close()
    assert "PA9" in pins
    assert pins["PA9"]["pin_name"] == "PA9"
    assert pins["PA9"]["net_name"] == "MCU_TX"
    assert "GND" in pins
    assert pins["GND"]["net_name"] == "GND"


def test_export_unconnected_pin_has_null_net(tmp_path):
    netlist = _minimal_netlist()
    netlist["components"]["R1"]["pins"]["NC"] = {"name": "NC", "net": None}
    vs = _FakeVariantState([_FakeVariant("Default")])
    export_project(_minimal_project(tmp_path), netlist, vs, "0.1.0")
    conn = sqlite3.connect(str(tmp_path / "TestBoard-pcb-copilot.db"))
    comp_id = conn.execute("SELECT id FROM components WHERE refdes='R1'").fetchone()[0]
    net_name = conn.execute(
        "SELECT net_name FROM pins WHERE component_id=? AND pin_number='NC'", (comp_id,)
    ).fetchone()[0]
    conn.close()
    assert net_name is None


def test_export_overwrites_existing_db(tmp_path):
    vs = _FakeVariantState([_FakeVariant("Default")])
    db_path = export_project(_minimal_project(tmp_path), _minimal_netlist(), vs, "0.1.0")
    db_path2 = export_project(_minimal_project(tmp_path), _minimal_netlist(), vs, "0.1.1")
    assert db_path == db_path2
    conn = sqlite3.connect(db_path2)
    exported_by = conn.execute("SELECT exported_by FROM project").fetchone()[0]
    conn.close()
    assert exported_by == "altium-copilot v0.1.1"


# ---------------------------------------------------------------------------
# duplicate refdes detection
# ---------------------------------------------------------------------------

def test_duplicate_refdes_raises_value_error(tmp_path):
    """A UNIQUE violation on components.refdes must surface as ValueError and leave no DB file."""

    class _DuplicateDict(dict):
        """Yields the same refdes twice to force an IntegrityError on the second INSERT."""
        def items(self):
            comp = {"mpn": None, "description": None, "value": None, "sheet": "MCU", "pins": {}}
            yield "R1", comp
            yield "R1", comp

    netlist = {"nets": {}, "pin_to_net": {}, "components": _DuplicateDict()}
    project = {
        "name": "TestBoard",
        "root_dir": str(tmp_path),
        "sheets": [{"name": "MCU", "path": ""}],
    }
    vs = _FakeVariantState([_FakeVariant("Default")])

    with pytest.raises(ValueError, match="R1"):
        export_project(project, netlist, vs, "0.1.0")

    assert not (tmp_path / "TestBoard-pcb-copilot.db").exists()


# ---------------------------------------------------------------------------
# notes rows
# ---------------------------------------------------------------------------

@pytest.fixture
def notes_project(tmp_path):
    """sheet1/sheet2 fixtures, the synthetic Draftsman drawing and two project parameters."""
    for name in ("sheet1.SchDoc", "sheet2.SchDoc"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    shutil.copy(DRAWING, tmp_path / "Fab.PCBDwf")
    return {
        "name": "TestBoard",
        "root_dir": str(tmp_path),
        "sheets": [{"name": n, "path": str(tmp_path / f"{n}.SchDoc")} for n in ("sheet1", "sheet2")],
        "pcb_doc_paths": [str(tmp_path / "TestBoard.PcbDoc")],
        "pcb_dwf_paths": [str(tmp_path / "Fab.PCBDwf")],
        "parameters": {"PrjName": "BMS_11", "PrjPCBRev": "1"},
    }


def _export_notes(project, cache=None) -> tuple[str, list[dict]]:
    rows, errors = note_rows(cache or DesignNotesCache(), project)
    vs = _FakeVariantState([_FakeVariant("Default")])
    return export_project(project, {"nets": {}, "components": {}}, vs, "0.1.0", rows), errors


def _notes(db_path: str, where: str = "1") -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(f"SELECT * FROM notes WHERE {where} ORDER BY id")]
    conn.close()
    for row in rows:
        row["near_refdes"] = json.loads(row["near_refdes"])
        row["data"] = json.loads(row["data"])
    return rows


def test_export_without_notes_leaves_notes_table_empty(tmp_path):
    vs = _FakeVariantState([_FakeVariant("Default")])
    db_path = export_project(_minimal_project(tmp_path), _minimal_netlist(), vs, "0.1.0")
    assert _notes(db_path) == []


def test_export_notes_project_parameters(notes_project):
    db_path, errors = _export_notes(notes_project)
    assert errors == []
    params = _notes(db_path, "document_kind='project'")
    assert [(r["document"], r["kind"], r["title"], r["text"]) for r in params] == [
        ("TestBoard", "parameter", "PrjName", "BMS_11"),
        ("TestBoard", "parameter", "PrjPCBRev", "1"),
    ]
    assert all(r["sheet_id"] is None for r in params)


def test_export_notes_sheet_text_frames(notes_project):
    db_path, _ = _export_notes(notes_project)
    conn = sqlite3.connect(db_path)
    sheet_ids = dict(conn.execute("SELECT name, id FROM sheets").fetchall())
    conn.close()
    frames = _notes(db_path, "document='sheet1' AND kind='text_frame'")
    assert len(frames) == 15
    assert {r["sheet_id"] for r in frames} == {sheet_ids["sheet1"]}
    assert {r["document_kind"] for r in frames} == {"sheet"}
    resistor = next(r for r in frames if r["text"].startswith("1k resistor instead of 10k"))
    assert resistor["near_refdes"] == ["R4"]
    assert resistor["page"] is None
    assert set(resistor["data"]) == {"x_mil", "y_mil", "width_mil", "height_mil"}
    rmii = next(r for r in frames if "RMII clock" in r["text"])
    assert set(rmii["near_refdes"]) == {"R13", "R14", "U1"}
    sheet2 = _notes(db_path, "document='sheet2'")
    assert sheet2 and {r["sheet_id"] for r in sheet2} == {sheet_ids["sheet2"]}


def test_export_notes_sheet_labels_directives_title_block(notes_project):
    db_path, _ = _export_notes(notes_project)
    labels = {r["text"] for r in _notes(db_path, "document='sheet1' AND kind='label'")}
    assert {"JTAG", "Power", "Communication"} <= labels
    assert "Debugging LEDs" not in labels  # a text frame, not a label
    directives = _notes(db_path, "document='sheet1' AND kind='directive'")
    assert [r["title"] for r in directives] == ["50 Ohm"] * 4
    assert directives[0]["data"] == {"parameters": {}, "x_mil": 17800.0, "y_mil": 9100.0}
    title_block = _notes(db_path, "document='sheet1' AND kind='title_block'")
    assert {r["title"]: r["text"] for r in title_block} == {
        "DocumentNumber": "11", "SheetNumber": "10", "SheetTotal": "19",
    }


def test_export_notes_draftsman(notes_project):
    db_path, _ = _export_notes(notes_project)
    rows = _notes(db_path, "document='Fab.PCBDwf'")
    assert {r["document_kind"] for r in rows} == {"draftsman"}
    assert all(r["sheet_id"] is None for r in rows)
    by_kind: dict[str, list[dict]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)

    drawing = by_kind["drawing"][0]
    assert drawing["text"] == "C:\\Projects\\Demo\\Demo.PcbDoc"
    assert drawing["data"]["matches_project_pcb"] is False
    assert drawing["data"]["pages"] == ["A4", "A3", "custom"]
    assert drawing["data"]["last_update"].startswith("2026-10-01T12:00:00")
    assert [(r["title"], r["text"]) for r in by_kind["parameter"]] == [("Revision", "B")]

    note = by_kind["note"][0]
    assert (note["page"], note["title"]) == (1, "Notes:")
    assert note["text"] == (
        "1. Board material FR-4, Tg 150 or higher\n2. Surface finish ENIG\n"
        "3. Final thickness 1.6mm, part DEMO-v3, =Unknown_Param kept"
    )
    assert note["data"]["lines"][:2] == ["Board material FR-4, Tg 150 or higher", "Surface finish ENIG"]

    texts = {r["text"]: r["data"]["from_template"] for r in by_kind["text"]}
    assert texts == {"Do not scale this drawing": False, "Drawn by: J. Doe": True}

    table = by_kind["table"][0]
    assert table["title"] == "Impedance Table"
    assert table["text"] == "Layer | Impedance\nL1 | 50 ohm"
    assert table["data"]["rows"] == [["Layer", "Impedance"], ["L1", "50 ohm"]]

    assert [(r["page"], r["text"], r["data"]["side"], r["data"]["layer"]) for r in by_kind["view"]] == [
        (1, "fabrication view, Top side", "Top", "Top Layer"),
        (2, "assembly view, Bottom side", "Bottom", None),
        (2, "layer stack legend", None, None),
    ]
    assert by_kind["view"][1]["title"] == "View from Bottom side (Scale 2:1)"

    assert [r["text"] for r in by_kind["drill"]] == [
        "0.300 mm plated Via Top Layer - Bottom Layer x3",
        "1.000 mm plated Pad Top Layer - Bottom Layer x1 slot 2.500 mm",
        "3.200 mm non-plated Pad Top Layer - Bottom Layer x2",
    ]
    assert by_kind["drill"][0]["data"]["count"] == 3
    layer = by_kind["layer_note"][0]
    assert layer["title"] == "Dielectric 1"
    assert layer["text"] == "comment=Controlled impedance core; note=Use Isola 370HR"


def test_export_notes_draftsman_picture_is_metadata_only(notes_project):
    db_path, _ = _export_notes(notes_project)
    pictures = _notes(db_path, "kind='picture'")
    assert len(pictures) == 1
    assert pictures[0]["page"] == 1
    assert pictures[0]["data"] == {
        "from_template": False, "picture_index": 0, "width_mm": 50.0, "height_mm": 25.0,
    }
    raw = Path(db_path).read_bytes()
    assert TINY_PNG.encode() not in raw
    assert base64.b64decode(TINY_PNG) not in raw


def test_export_notes_drawing_of_this_projects_board(notes_project):
    notes_project["pcb_doc_paths"] = ["c:/projects/demo/Demo.PcbDoc"]
    db_path, _ = _export_notes(notes_project)
    assert _notes(db_path, "kind='drawing'")[0]["data"]["matches_project_pcb"] is True


class _StubNotesCache:
    def __init__(self, sheet: SheetAnnotations):
        self._sheet = sheet

    def sheet(self, path):
        return self._sheet

    def draftsman(self, path):
        raise AssertionError("the project lists no Draftsman documents")


def test_export_notes_sheet_note_and_image(tmp_path):
    sheet = SheetAnnotations(
        items=[
            SheetAnnotation(kind="note", text="Check\nthe footprint", author="KC", collapsed=True,
                            x_mil=100, y_mil=200, width_mil=300, height_mil=50, near=["U1"]),
            SheetAnnotation(kind="image", file_name="ClipboardPicture2026", embedded=True,
                            image_index=0, x_mil=1000, y_mil=2000, width_mil=400, height_mil=300,
                            near=["J1"], captions=["Connector pinout"]),
        ],
        directives=[SheetDirective(name="DiffPair", x_mil=5, y_mil=6,
                                   parameters={"DifferentialPair": "USB"})],
        template="C:\\Templates\\A3.SchDot",
    )
    project = {"name": "TestBoard", "root_dir": str(tmp_path),
               "sheets": [{"name": "MCU", "path": "MCU.SchDoc"}]}
    db_path, errors = _export_notes(project, _StubNotesCache(sheet))
    assert errors == []
    note, image, directive, template = _notes(db_path)
    assert (note["kind"], note["text"], note["author"], note["near_refdes"]) == (
        "note", "Check\nthe footprint", "KC", ["U1"],
    )
    assert note["data"] == {"x_mil": 100, "y_mil": 200, "width_mil": 300, "height_mil": 50,
                            "collapsed": True}
    assert (image["kind"], image["title"], image["text"], image["near_refdes"]) == (
        "image", "ClipboardPicture2026", "Connector pinout", ["J1"],
    )
    assert image["data"] == {
        "x_mil": 1000, "y_mil": 2000, "width_mil": 400, "height_mil": 300,
        "file_name": "ClipboardPicture2026", "embedded": True, "image_index": 0,
        "captions": ["Connector pinout"],
    }
    assert (directive["title"], directive["text"]) == ("DiffPair", "DifferentialPair=USB")
    assert (template["kind"], template["text"]) == ("template", "A3.SchDot")
    assert template["data"] == {"path": "C:\\Templates\\A3.SchDot"}


def test_unreadable_document_is_reported_and_skipped(notes_project, tmp_path):
    (tmp_path / "sheet2.SchDoc").write_bytes(b"not an OLE file")
    Path(notes_project["pcb_dwf_paths"][0]).unlink()
    db_path, errors = _export_notes(notes_project)
    assert [(e["document_kind"], e["document"]) for e in errors] == [
        ("sheet", "sheet2"), ("draftsman", "Fab.PCBDwf"),
    ]
    assert "FileNotFoundError" in errors[1]["error"]
    assert {r["document"] for r in _notes(db_path)} == {"TestBoard", "sheet1"}
