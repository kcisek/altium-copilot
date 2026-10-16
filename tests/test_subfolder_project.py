"""Issue #6 — projects whose .SchDoc files live in a subfolder (e.g. source\\) next to a
root-level .PrjPcb. Sheets must come from the .PrjPcb document list, not a top-level glob."""
import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from altium import AltiumClient
from parsers.prj_pcb import parse_prj_pcb

FIXTURES = Path(__file__).parent / "fixtures"
SHEET1_COUNT = 67
TOTAL_COUNT = 127  # sheet1 (67) + sheet2 (60), no shared refdes

NET_TEXT = (
    "[\nU1\nLQFP100\nSTM32F407VGT6\n]\n"
    "(\nVDDA\nU1-22\n)\n"
    "(\nVCC\nJ1-1\nU3-1\n)\n"
    "(\nGND\nJ1-3\n)\n"
)

# Variant with a stale "R?" designator whose UniqueId is R11 on sheet1.
STALE_R11_DNP_VARIANT = (
    "[ProjectVariant1]\nDescription=Prod\nVariationCount=1\n"
    "Variation1=Designator=R?|UniqueId=\\ABCDEFGH\\YNKVFXPS|Kind=1|AlternatePart=\n"
)


def _prj_text(*document_paths: str, extra: str = "") -> str:
    docs = "".join(
        f"[Document{i}]\nDocumentPath={p}\n" for i, p in enumerate(document_paths, start=1)
    )
    return "[Design]\nVersion=1.0\n" + docs + extra


def _set_mtime(path: Path, mtime: float) -> None:
    os.utime(path, (mtime, mtime))


def _make_net_fresh(root: Path, net: Path) -> None:
    newest = max(p.stat().st_mtime for p in root.rglob("*") if p.is_file() and p != net)
    _set_mtime(net, newest + 100)


def _make_stale(path: Path, net: Path) -> None:
    _set_mtime(path, net.stat().st_mtime + 100)


@pytest.fixture
def subfolder_project(tmp_path):
    """Board/Board.PrjPcb + Board/source/sheet{1,2}.SchDoc + Board/Project Outputs for Board/Board.NET"""
    root = tmp_path / "Board"
    src = root / "source"
    src.mkdir(parents=True)
    shutil.copy(FIXTURES / "sheet1.SchDoc", src / "sheet1.SchDoc")
    shutil.copy(FIXTURES / "sheet2.SchDoc", src / "sheet2.SchDoc")
    prj = root / "Board.PrjPcb"
    prj.write_text(_prj_text("source\\sheet1.SchDoc", "source\\sheet2.SchDoc"), encoding="utf-8")
    out = root / "Project Outputs for Board"
    out.mkdir()
    net = out / "Board.NET"
    net.write_text(NET_TEXT, encoding="utf-8")
    _make_net_fresh(root, net)
    return root, prj, net


def _fake_ps_writing(net: Path):
    def fake_run_ps(script, **kwargs):
        net.write_text(NET_TEXT, encoding="utf-8")
        return '{"success":true}'
    return fake_run_ps


# ---------- AltiumClient ----------

def test_cache_hit_enriches_subfolder_sheets(subfolder_project):
    _, prj, _ = subfolder_project
    client = AltiumClient()
    with patch("altium._run_ps") as mock_ps:
        result = client.generate_netlist(str(prj))
        mock_ps.assert_not_called()

    assert result is False
    comps = client._netlist["components"]
    assert len(comps) == TOTAL_COUNT
    assert comps["U1"]["sheet"] == "sheet1"
    assert comps["U1"]["pins"]["22"] == {"name": "VDDA", "net": "VDDA"}
    assert comps["U3"]["sheet"] == "sheet2"
    assert comps["U3"]["pins"]["1"] == {"name": "V+", "net": "VCC"}
    assert client._netlist["sheet_errors"] == []


def test_load_netlist_from_file_uses_prjpcb_sheet_paths(subfolder_project):
    _, prj, net = subfolder_project
    client = AltiumClient()
    client.load_netlist_from_file(str(net), parse_prj_pcb(str(prj)).sheet_paths)
    assert len(client._netlist["components"]) == TOTAL_COUNT


@pytest.mark.parametrize("stale", ["source/sheet1.SchDoc", "Board.PrjPcb"])
def test_regenerates_when_subfolder_source_is_newer(subfolder_project, stale):
    root, prj, net = subfolder_project
    _make_stale(root / stale, net)
    client = AltiumClient()
    with patch("altium._run_ps", side_effect=_fake_ps_writing(net)) as mock_ps:
        result = client.generate_netlist(str(prj))
        mock_ps.assert_called_once_with("generate_protel_netlist.ps1", Delay="50")

    assert result is True
    assert len(client._netlist["components"]) == TOTAL_COUNT


def test_regenerates_when_sheet_outside_project_dir_is_newer(subfolder_project):
    root, prj, net = subfolder_project
    common = root.parent / "common"
    common.mkdir()
    shutil.copy(FIXTURES / "sheet2.SchDoc", common / "shared.SchDoc")
    prj.write_text(_prj_text("source\\sheet1.SchDoc", "..\\common\\shared.SchDoc"), encoding="utf-8")
    _set_mtime(prj, net.stat().st_mtime - 100)
    _make_stale(common / "shared.SchDoc", net)

    client = AltiumClient()
    with patch("altium._run_ps", side_effect=_fake_ps_writing(net)) as mock_ps:
        assert client.generate_netlist(str(prj)) is True
        mock_ps.assert_called_once()
    assert client._netlist["components"]["U3"]["sheet"] == "shared"


def test_unlisted_schdoc_drives_freshness_but_not_enrichment(subfolder_project):
    """A sheet added in Altium and saved before the project is compiled into the .NET but
    not yet listed in the .PrjPcb: it must trigger regeneration, yet only listed sheets
    are enriched (so stray copies never pollute components)."""
    root, prj, net = subfolder_project
    shutil.copy(FIXTURES / "sheet1.SchDoc", root / "source" / "new_sheet.SchDoc")
    _make_stale(root / "source" / "new_sheet.SchDoc", net)

    client = AltiumClient()
    with patch("altium._run_ps", side_effect=_fake_ps_writing(net)) as mock_ps:
        assert client.generate_netlist(str(prj)) is True
        mock_ps.assert_called_once()
    sheets = {c["sheet"] for c in client._netlist["components"].values()}
    assert sheets == {"sheet1", "sheet2"}


def test_flat_layout_new_sheet_saved_before_project_triggers_regeneration(tmp_path):
    """Regression guard for the classic flat layout (sheets next to the .PrjPcb)."""
    shutil.copy(FIXTURES / "sheet1.SchDoc", tmp_path / "sheet1.SchDoc")
    prj = tmp_path / "Flat.PrjPcb"
    prj.write_text(_prj_text("sheet1.SchDoc"), encoding="utf-8")
    out = tmp_path / "Project Outputs for Flat"
    out.mkdir()
    net = out / "Flat.NET"
    net.write_text(NET_TEXT, encoding="utf-8")
    _make_net_fresh(tmp_path, net)
    (tmp_path / "Added.SchDoc").write_bytes(b"")
    _make_stale(tmp_path / "Added.SchDoc", net)

    client = AltiumClient()
    with patch("altium._run_ps", side_effect=_fake_ps_writing(net)) as mock_ps:
        assert client.generate_netlist(str(prj)) is True
        mock_ps.assert_called_once()
    assert client._netlist["components"]["U1"]["sheet"] == "sheet1"


def test_missing_and_corrupt_sheets_do_not_discard_other_sheets(subfolder_project):
    root, prj, net = subfolder_project
    (root / "source" / "broken.SchDoc").write_bytes(b"")
    prj.write_text(
        _prj_text("source\\sheet1.SchDoc", "source\\broken.SchDoc", "source\\missing.SchDoc",
                  "source\\sheet2.SchDoc"),
        encoding="utf-8",
    )
    _make_net_fresh(root, net)

    client = AltiumClient()
    with patch("altium._run_ps") as mock_ps:
        # A listed-but-missing sheet must not break the freshness check (which would
        # otherwise move the .NET aside and drive Altium on every call).
        assert client.generate_netlist(str(prj)) is False
        mock_ps.assert_not_called()

    assert len(client._netlist["components"]) == TOTAL_COUNT
    errors = {e["sheet"]: e for e in client._netlist["sheet_errors"]}
    assert set(errors) == {"broken", "missing"}
    assert "OLE2" in errors["broken"]["error"]
    assert errors["missing"]["error"] == "file not found"
    assert errors["missing"]["path"].endswith("missing.SchDoc")


def test_enrich_with_no_sheets_sets_empty_components():
    netlist = {"nets": {}, "pin_to_net": {"U1": {"1": "N1"}}}
    AltiumClient()._enrich_from_schdocs([], netlist)
    assert netlist["components"] == {}
    assert netlist["sheet_errors"] == []


def test_failed_regeneration_restores_previous_netlist(subfolder_project):
    """Altium closed or unresponsive must not cost the user their only .NET."""
    root, prj, net = subfolder_project
    _make_stale(root / "source" / "sheet1.SchDoc", net)
    client = AltiumClient()
    with patch("altium._run_ps", side_effect=RuntimeError("Altium Designer is not running")):
        with pytest.raises(RuntimeError, match="not running"):
            client.generate_netlist(str(prj))

    assert net.read_text(encoding="utf-8") == NET_TEXT
    assert not net.with_name(net.name + ".bak").exists()


def test_successful_regeneration_removes_backup(subfolder_project):
    root, prj, net = subfolder_project
    _make_stale(root / "source" / "sheet1.SchDoc", net)
    client = AltiumClient()
    with patch("altium._run_ps", side_effect=_fake_ps_writing(net)):
        assert client.generate_netlist(str(prj)) is True
    assert net.exists()
    assert not net.with_name(net.name + ".bak").exists()


def _locked_bak(method: str):
    """Path.<method> that fails for the .bak only, like a sync client holding the file."""
    original = getattr(Path, method)

    def guarded(self, *args, **kwargs):
        if self.name.endswith(".bak"):
            raise PermissionError(32, "sharing violation", str(self))
        return original(self, *args, **kwargs)
    return patch.object(Path, method, guarded)


def test_locked_backup_does_not_fail_successful_regeneration(subfolder_project):
    root, prj, net = subfolder_project
    _make_stale(root / "source" / "sheet1.SchDoc", net)
    client = AltiumClient()
    with patch("altium._run_ps", side_effect=_fake_ps_writing(net)), _locked_bak("unlink"):
        assert client.generate_netlist(str(prj)) is True
    assert len(client._netlist["components"]) == TOTAL_COUNT


def test_locked_backup_does_not_mask_regeneration_error(subfolder_project):
    root, prj, net = subfolder_project
    _make_stale(root / "source" / "sheet1.SchDoc", net)
    client = AltiumClient()
    with patch("altium._run_ps", side_effect=RuntimeError("Altium Designer is not running")), \
         _locked_bak("replace"):
        # The first replace (.NET -> .bak) targets Board.NET, so only the restore is blocked.
        with pytest.raises(RuntimeError, match="not running"):
            client.generate_netlist(str(prj))


# ---------- main.py tools ----------

@pytest.fixture
def main_env(subfolder_project):
    """Isolated main module state with a real AltiumClient and a registry entry whose
    netlist_mtime matches the .NET, so set_project_dir can take the cache shortcut."""
    import main
    root, prj, net = subfolder_project

    def registry():
        return {"projects": [{"dir": str(root), "netlist_mtime": net.stat().st_mtime}]}

    with patch.object(main, "_project", None), \
         patch.object(main, "_variant_state", None), \
         patch.object(main, "_netlist_last_updated", None), \
         patch.object(main, "_pcb_session", main.PcbSession()), \
         patch.object(main, "_altium", AltiumClient()), \
         patch.object(main, "read_registry", side_effect=registry), \
         patch.object(main, "upsert_registry_entry") as upsert:
        yield main, root, prj, net, upsert


def test_set_project_dir_cache_shortcut_loads_subfolder_components(main_env):
    main, root, _, _, _ = main_env
    with patch.object(main._altium, "get_status") as get_status:
        result = json.loads(main.set_project_dir(str(root)))
        get_status.assert_not_called()

    assert result["loaded"] is True
    assert result["sheets"] == ["sheet1", "sheet2"]
    assert result["component_count"] == TOTAL_COUNT
    assert "warning" not in result
    assert "sheet_errors" not in result

    search = json.loads(main.search_components("STM32", "mpn"))
    assert search["match_count"] == 1
    assert json.loads(main.get_component("C1"))["sheet"] == "sheet1"
    context = main.get_sheet_context("sheet2")
    assert context.startswith("sheet:sheet2 total:60 ")
    assert "U3|" in context


def test_set_project_dir_skips_cache_when_subfolder_sheet_is_newer(main_env):
    main, root, _, net, _ = main_env
    _make_stale(root / "source" / "sheet2.SchDoc", net)
    with patch.object(main._altium, "get_status", return_value={"running": False}), \
         patch.object(main._altium, "load_netlist_from_file") as load:
        result = json.loads(main.set_project_dir(str(root)))
        load.assert_not_called()

    assert result["error"] == "altium_not_running"
    assert result["netlist_stale"] is True
    assert main._project is None


def test_set_project_dir_reports_sheet_errors(main_env):
    main, root, prj, net, _ = main_env
    (root / "source" / "broken.SchDoc").write_bytes(b"")
    prj.write_text(
        _prj_text("source\\sheet1.SchDoc", "source\\sheet2.SchDoc", "source\\broken.SchDoc"),
        encoding="utf-8",
    )
    _make_net_fresh(root, net)

    result = json.loads(main.set_project_dir(str(root)))

    assert result["loaded"] is True
    assert result["component_count"] == TOTAL_COUNT
    assert [e["sheet"] for e in result["sheet_errors"]] == ["broken"]
    assert main.get_sheet_context("broken").startswith("warning: sheet_load_failed")


def test_zero_components_is_reported_everywhere(main_env):
    """The issue #6 symptom: nets load, components don't — say so instead of 'matched all 0'."""
    main, root, prj, net, _ = main_env
    prj.write_text(_prj_text("source\\gone.SchDoc"), encoding="utf-8")
    _make_net_fresh(root, net)

    result = json.loads(main.set_project_dir(str(root)))
    assert result["loaded"] is True
    assert result["component_count"] == 0
    assert result["warning"] == "no_components_loaded"
    assert result["sheet_errors"][0]["sheet"] == "gone"

    search = main.search_components("STM32", "mpn")
    assert "matched all 0" not in search
    assert json.loads(search)["error"] == "no_components_loaded"
    assert json.loads(main.get_component("U1"))["error"] == "no_components_loaded"
    assert json.loads(main.get_sheet_context("gone"))["error"] == "no_components_loaded"
    assert json.loads(main.package_for_xfn())["error"] == "no_components_loaded"


def test_stale_designator_dnp_resolved_for_subfolder_project(main_env):
    main, root, prj, net, _ = main_env
    prj.write_text(
        _prj_text("source\\sheet1.SchDoc", "source\\sheet2.SchDoc", extra=STALE_R11_DNP_VARIANT),
        encoding="utf-8",
    )
    _make_net_fresh(root, net)

    json.loads(main.set_project_dir(str(root)))
    assert main._variant_state.is_dnp("R11")


def test_refresh_netlist_picks_up_sheet_added_in_subfolder(main_env):
    main, root, prj, net, upsert = main_env
    prj.write_text(_prj_text("source\\sheet1.SchDoc"), encoding="utf-8")
    _make_net_fresh(root, net)
    assert json.loads(main.set_project_dir(str(root)))["component_count"] == SHEET1_COUNT

    # User adds sheet2 to the project in Altium and saves: the .PrjPcb is now newer.
    prj.write_text(_prj_text("source\\sheet1.SchDoc", "source\\sheet2.SchDoc"), encoding="utf-8")
    _make_stale(prj, net)
    with patch("altium._run_ps", side_effect=_fake_ps_writing(net)):
        result = json.loads(main.refresh_netlist())

    assert result["refreshed"] is True
    assert result["component_count"] == TOTAL_COUNT
    assert [s["name"] for s in main._project["sheets"]] == ["sheet1", "sheet2"]
    assert main.get_sheet_context("sheet2").startswith("sheet:sheet2 total:60 ")


def test_refresh_netlist_cache_hit_mentions_sheet_errors(main_env):
    main, root, prj, net, _ = main_env
    prj.write_text(_prj_text("source\\sheet1.SchDoc", "source\\missing.SchDoc"), encoding="utf-8")
    _make_net_fresh(root, net)
    main.set_project_dir(str(root))

    with patch("altium._run_ps") as mock_ps:
        result = main.refresh_netlist()
        mock_ps.assert_not_called()
    assert "up to date" in result
    assert "Sheet 'missing' failed to load: file not found" in result


def test_set_project_dir_full_altium_flow_loads_subfolder_components(main_env):
    main, root, _, net, upsert = main_env
    _make_stale(root / "source" / "sheet1.SchDoc", net)  # cache shortcut must fall through
    status = {"running": True, "project_file": "Board.PrjPcb", "active_tab": "sheet1.SchDoc",
              "tab_type": "schematic"}
    with patch.object(main._altium, "get_status", return_value=status), \
         patch("altium._run_ps", side_effect=_fake_ps_writing(net)) as mock_ps:
        result = json.loads(main.set_project_dir(str(root)))
        mock_ps.assert_called_once()

    assert result["loaded"] is True
    assert result["component_count"] == TOTAL_COUNT
    assert "warning" not in result
    upsert.assert_called_once_with("Board.PrjPcb", str(root), netlist_mtime=net.stat().st_mtime)


def test_refresh_netlist_recovers_dnp_once_failed_sheet_is_fixed(main_env):
    """A stale-designator DNP on a sheet that failed to parse at load is resolved by the
    next refresh, instead of being dropped for the rest of the session."""
    main, root, prj, net, _ = main_env
    sheet1 = root / "source" / "sheet1.SchDoc"
    sheet1.write_bytes(b"corrupt")
    prj.write_text(
        _prj_text("source\\sheet1.SchDoc", "source\\sheet2.SchDoc", extra=STALE_R11_DNP_VARIANT),
        encoding="utf-8",
    )
    _make_net_fresh(root, net)
    result = json.loads(main.set_project_dir(str(root)))
    assert [e["sheet"] for e in result["sheet_errors"]] == ["sheet1"]
    assert not main._variant_state.is_dnp("R11")

    shutil.copy(FIXTURES / "sheet1.SchDoc", sheet1)
    _make_stale(sheet1, net)
    with patch("altium._run_ps", side_effect=_fake_ps_writing(net)):
        result = json.loads(main.refresh_netlist())

    assert result["component_count"] == TOTAL_COUNT
    assert "sheet_errors" not in result
    assert main._variant_state.is_dnp("R11")


def test_package_for_xfn_warns_about_failed_sheets(main_env):
    main, root, _, net, _ = main_env
    (root / "source" / "sheet2.SchDoc").write_bytes(b"corrupt")
    _make_net_fresh(root, net)
    main.set_project_dir(str(root))

    with patch.object(main, "mark_xfn_exported"):
        result = main.package_for_xfn()

    assert "Exported to:" in result
    assert "Warning: sheet 'sheet2' failed to load" in result
