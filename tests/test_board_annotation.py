"""Board-annotated projects: the .NET names physical designators, which the project's
.Annotation file maps from each sheet part (sheet + UniqueId). Built on the real fixture
sheets, with sheet1's U1 (STM32, UniqueId PYMEJSBM) annotated to U3 on the board and
sheet2's U3 (LTC6811, UniqueId NWGGFMAY) to U1."""
import json
import os
import shutil
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from altium import AltiumClient, netlist_sources
from parsers.prj_pcb import parse_prj_pcb

FIXTURES = Path(__file__).parent / "fixtures"
TOTAL_COUNT = 127  # sheet1 (67) + sheet2 (60)

ANNOTATION = (
    "[DesignatorManager]\r\n"
    "LogicalDesignator0=U1\r\nLogicalPartID0=1\r\nDocumentName0=sheet1.SchDoc\r\n"
    "ChannelName0=MCU\r\nUniqueID0=\\ABCDEFGH\\PYMEJSBM\r\nPhysicalDesignator0=U3\r\n"
    "PhysicalDesignatorLocked0=0\r\n"
    "LogicalDesignator1=U3\r\nLogicalPartID1=1\r\nDocumentName1=sheet2.SchDoc\r\n"
    "ChannelName1=BMS\r\nUniqueID1=\\IJKLMNOP\\NWGGFMAY\r\nPhysicalDesignator1=U1\r\n"
    "PhysicalDesignatorLocked1=0\r\n"
)

# Physical names: the STM32's VDDA pin is on U3, the LTC6811's V+ pin on U1.
NET_TEXT = (
    "[\nU3\nLQFP100\nSTM32F407VGT6\n]\n"
    "(\nVDDA\nU3-22\n)\n"
    "(\nVCC\nJ1-1\nU1-1\n)\n"
    "(\nGND\nJ1-3\n)\n"
)


def _prj_text(*document_paths: str) -> str:
    docs = "".join(
        f"[Document{i}]\nDocumentPath={p}\n" for i, p in enumerate(document_paths, start=1)
    )
    return "[Design]\nVersion=1.0\n" + docs


def _set_mtime(path: Path, mtime: float) -> None:
    os.utime(path, (mtime, mtime))


def _make_net_fresh(root: Path, net: Path) -> None:
    newest = max(p.stat().st_mtime for p in root.rglob("*") if p.is_file() and p != net)
    _set_mtime(net, newest + 100)


@pytest.fixture
def annotated_project(tmp_path):
    root = tmp_path / "Board"
    root.mkdir()
    shutil.copy(FIXTURES / "sheet1.SchDoc", root / "sheet1.SchDoc")
    shutil.copy(FIXTURES / "sheet2.SchDoc", root / "sheet2.SchDoc")
    (root / "Board.Annotation").write_text(ANNOTATION, encoding="utf-8", newline="")
    prj = root / "Board.PrjPcb"
    prj.write_text(_prj_text("sheet1.SchDoc", "sheet2.SchDoc", "Board.Annotation"),
                   encoding="utf-8")
    out = root / "Project Outputs for Board"
    out.mkdir()
    net = out / "Board.NET"
    net.write_text(NET_TEXT, encoding="utf-8")
    _make_net_fresh(root, net)
    return root, prj, net


# ---------- AltiumClient ----------

def test_parts_are_filed_under_their_physical_designators(annotated_project):
    _, prj, _ = annotated_project
    client = AltiumClient()
    with patch("altium._run_ps") as mock_ps:
        assert client.generate_netlist(str(prj)) is False
        mock_ps.assert_not_called()

    comps = client._netlist["components"]
    assert len(comps) == TOTAL_COUNT
    stm32, ltc = comps["U3"], comps["U1"]
    assert (stm32["mpn"], stm32["sheet"], stm32["logical_refdes"]) == (
        "STM32F407VGT6", "sheet1", "U1")
    assert stm32["pins"]["22"] == {"name": "VDDA", "net": "VDDA"}
    assert (ltc["mpn"], ltc["sheet"], ltc["logical_refdes"]) == (
        "LTC6811HG-1#3ZZTRPBF", "sheet2", "U3")
    assert ltc["pins"]["1"] == {"name": "V+", "net": "VCC"}
    # Parts the file does not list keep their sheet designator.
    assert comps["J1"]["pins"]["1"] == {"name": "VCC", "net": "VCC"}
    assert "logical_refdes" not in comps["J1"]
    assert "annotation_errors" not in client._netlist


def test_load_netlist_from_file_applies_the_given_annotation(annotated_project):
    _, prj, net = annotated_project
    data = parse_prj_pcb(str(prj))
    client = AltiumClient()
    client.load_netlist_from_file(str(net), data.sheet_paths, data.annotation_paths)
    assert client._netlist["components"]["U3"]["mpn"] == "STM32F407VGT6"

    client.load_netlist_from_file(str(net), data.sheet_paths)
    assert client._netlist["components"]["U3"]["mpn"] == "LTC6811HG-1#3ZZTRPBF"


def test_missing_annotation_file_is_reported_and_sheet_designators_used(annotated_project):
    root, prj, _ = annotated_project
    (root / "Board.Annotation").unlink()
    client = AltiumClient()
    with patch("altium._run_ps"):
        client.generate_netlist(str(prj))

    assert client._netlist["annotation_errors"] == [
        {"path": str((root / "Board.Annotation").resolve()), "error": "file not found"}
    ]
    assert client._netlist["components"]["U1"]["mpn"] == "STM32F407VGT6"


def test_annotation_saved_after_the_net_makes_it_stale(annotated_project):
    root, prj, net = annotated_project
    data = parse_prj_pcb(str(prj))
    assert Path(data.annotation_paths[0]) in netlist_sources(
        str(prj), data.sheet_paths, data.annotation_paths)

    _set_mtime(root / "Board.Annotation", net.stat().st_mtime + 100)

    def fake_run_ps(script, **kwargs):
        net.write_text(NET_TEXT, encoding="utf-8")
        return '{"success":true}'

    client = AltiumClient()
    with patch("altium._run_ps", side_effect=fake_run_ps) as mock_ps, \
         patch("altium.time.sleep"):
        assert client.generate_netlist(str(prj)) is True
        mock_ps.assert_called_once()
    assert client._netlist["components"]["U3"]["logical_refdes"] == "U1"


# ---------- tools ----------

@pytest.fixture
def main_env(annotated_project):
    import main
    root, prj, net = annotated_project

    def registry():
        return {"projects": [{"dir": str(root), "netlist_mtime": net.stat().st_mtime}]}

    with patch.object(main, "_project", None), \
         patch.object(main, "_variant_state", None), \
         patch.object(main, "_netlist_last_updated", None), \
         patch.object(main, "_pcb_session", main.PcbSession()), \
         patch.object(main, "_altium", AltiumClient()), \
         patch.object(main, "read_registry", side_effect=registry), \
         patch.object(main, "upsert_registry_entry"):
        yield main, root


def test_set_project_dir_reports_renamed_parts(main_env):
    main, root = main_env
    with patch.object(main._altium, "get_status") as get_status:
        result = json.loads(main.set_project_dir(str(root)))
        get_status.assert_not_called()

    assert result["loaded"] is True
    assert result["component_count"] == TOTAL_COUNT
    assert result["renamed_by_annotation"] == 2
    assert "board designator" in result["renamed_by_annotation_hint"]


def test_tools_name_board_designators_with_the_sheet_designator(main_env):
    main, root = main_env
    main.set_project_dir(str(root))

    stm32 = json.loads(main.get_component("U3"))
    assert (stm32["refdes"], stm32["logical_refdes"], stm32["mpn"]) == (
        "U3", "U1", "STM32F407VGT6")
    assert json.loads(main.get_component("U1"))["mpn"] == "LTC6811HG-1#3ZZTRPBF"
    assert "logical_refdes" not in json.loads(main.get_component("J1"))

    search = json.loads(main.search_components("STM32", "mpn"))
    assert search["results"][0]["refdes"] == ["U3"]

    assert "\nU3 (U1 on sheet)|STM32F407VGT6|" in main.get_sheet_context("sheet1")
    assert "\nU1 (U3 on sheet)|LTC6811HG-1#3ZZTRPBF|" in main.get_sheet_context("sheet2")


def test_package_for_xfn_exports_board_designators(main_env):
    main, root = main_env
    main.set_project_dir(str(root))
    with patch.object(main, "mark_xfn_exported"):
        result = main.package_for_xfn()

    db_path = result.split("Exported to: ", 1)[1].splitlines()[0]
    conn = sqlite3.connect(db_path)
    try:
        rows = dict(conn.execute(
            "SELECT c.refdes, c.mpn || '@' || s.name FROM components c "
            "JOIN sheets s ON s.id = c.sheet_id WHERE c.refdes IN ('U1', 'U3')"
        ).fetchall())
    finally:
        conn.close()
    assert rows == {"U3": "STM32F407VGT6@sheet1", "U1": "LTC6811HG-1#3ZZTRPBF@sheet2"}


def test_stale_designator_dnp_resolves_to_the_board_designator(main_env):
    """A variant's "U?" entry names the sheet part by UniqueId; the netlist calls it U3."""
    main, root = main_env
    prj = root / "Board.PrjPcb"
    prj.write_text(
        prj.read_text(encoding="utf-8")
        + "[ProjectVariant1]\nDescription=Lite\nVariationCount=1\n"
        "Variation1=Designator=U?|UniqueId=\\ABCDEFGH\\PYMEJSBM|Kind=1|AlternatePart=\n",
        encoding="utf-8",
    )
    _make_net_fresh(root, root / "Project Outputs for Board" / "Board.NET")
    main.set_project_dir(str(root))
    assert main._variant_state.is_dnp("U3")
    assert not main._variant_state.is_dnp("U1")
