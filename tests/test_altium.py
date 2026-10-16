import json
import pytest
from unittest.mock import patch
from server.altium import AltiumClient


def test_get_status_returns_not_running_when_script_says_so():
    client = AltiumClient()
    with patch("server.altium._run_ps", return_value='{"running": false}'):
        result = client.get_status()
    assert result == {"running": False}


def test_get_status_returns_project_and_tab_when_altium_is_running():
    payload = {"running": True, "project_file": "MyProject.PrjPcb", "project_path": "C:/Projects/MyProject.PrjPcb", "active_tab": "PowerSupply.SchDoc"}
    client = AltiumClient()
    with patch("server.altium._run_ps", return_value=json.dumps(payload)):
        result = client.get_status()
    assert result["running"] is True
    assert result["project_file"] == "MyProject.PrjPcb"
    assert result["active_tab"] == "PowerSupply.SchDoc"


def test_get_status_raises_when_script_fails():
    client = AltiumClient()
    with patch("server.altium._run_ps", side_effect=RuntimeError("get_altium_status.ps1 failed")):
        with pytest.raises(RuntimeError):
            client.get_status()


# --- tab_type classification ---

def test_schematic_tab_classified_correctly():
    payload = {"running": True, "project_file": "MyProject.PrjPcb", "project_path": "", "active_tab": "PowerSupply.SchDoc"}
    client = AltiumClient()
    with patch("server.altium._run_ps", return_value=json.dumps(payload)):
        result = client.get_status()
    assert result["tab_type"] == "schematic"


def test_pcb_tab_classified_correctly():
    payload = {"running": True, "project_file": "MyProject.PrjPcb", "project_path": "", "active_tab": "MyProject.PcbDoc"}
    client = AltiumClient()
    with patch("server.altium._run_ps", return_value=json.dumps(payload)):
        result = client.get_status()
    assert result["tab_type"] == "pcb"


def test_other_tab_classified_correctly():
    payload = {"running": True, "project_file": "MyProject.PrjPcb", "project_path": "", "active_tab": "Home Page"}
    client = AltiumClient()
    with patch("server.altium._run_ps", return_value=json.dumps(payload)):
        result = client.get_status()
    assert result["tab_type"] == "other"


# --- empty project_file warning ---

def test_warning_added_when_project_file_is_empty():
    payload = {"running": True, "project_file": "", "project_path": "", "active_tab": "PowerSupply.SchDoc"}
    client = AltiumClient()
    with patch("server.altium._run_ps", return_value=json.dumps(payload)):
        result = client.get_status()
    assert result["warning"] == "no_sheet_open"


def test_no_warning_when_project_file_is_present():
    payload = {"running": True, "project_file": "MyProject.PrjPcb", "project_path": "", "active_tab": "PowerSupply.SchDoc"}
    client = AltiumClient()
    with patch("server.altium._run_ps", return_value=json.dumps(payload)):
        result = client.get_status()
    assert "warning" not in result


# --- Altium Designer 17 and older (DXP.exe, issue #5) ---

def _dxp_status(title: str, active_tab: str = "") -> dict:
    """get_status() for a DXP.exe window: the script leaves title parsing to Python."""
    payload = {"running": True, "process": "DXP", "window_title": title,
               "project_file": "", "project_path": "", "active_tab": active_tab}
    with patch("server.altium._run_ps", return_value=json.dumps(payload)):
        return AltiumClient().get_status()


def test_dxp_title_gives_project_tab_and_path(tmp_path):
    board = tmp_path / "Board"
    board.mkdir()
    (board / "Board.PrjPcb").touch()
    sheet = board / "Power.SchDoc"
    result = _dxp_status(f"Altium Designer (14.3) - {sheet} - Board.PrjPcb. Licensed to ACME. Not signed in.")
    assert result["running"] is True
    assert result["process"] == "DXP"
    assert result["project_file"] == "Board.PrjPcb"
    assert result["active_tab"] == "Power.SchDoc"
    assert result["tab_type"] == "schematic"
    assert result["project_path"] == str(board / "Board.PrjPcb")
    assert "warning" not in result
    assert "window_title" not in result


def test_dxp_project_found_in_parent_of_sheet_subfolder(tmp_path):
    (tmp_path / "Board.PrjPcb").touch()
    sheet = tmp_path / "source" / "Power.SchDoc"
    result = _dxp_status(f"Altium Designer (17.1) - {sheet} - Board.PrjPcb. Licensed to ACME.")
    assert result["project_path"] == str(tmp_path / "Board.PrjPcb")


def test_dxp_folder_with_dash_stays_in_document_path(tmp_path):
    board = tmp_path / "Rev B - final"
    board.mkdir()
    (board / "Board.PrjPcb").touch()
    sheet = board / "Power.SchDoc"
    result = _dxp_status(f"Altium Designer (14.3) - {sheet} - Board.PrjPcb. Licensed to ACME.")
    assert result["project_file"] == "Board.PrjPcb"
    assert result["active_tab"] == "Power.SchDoc"
    assert result["project_path"] == str(board / "Board.PrjPcb")


def test_dxp_project_not_on_disk_leaves_path_empty_without_warning():
    result = _dxp_status(r"Altium Designer (14.3) - Z:\nowhere\Power.SchDoc - Board.PrjPcb. Licensed to ACME.")
    assert result["project_file"] == "Board.PrjPcb"
    assert result["project_path"] == ""
    assert "warning" not in result


def test_dxp_unsaved_marker_stripped_from_active_tab():
    result = _dxp_status(r"Altium Designer (14.3) - C:\Work\Power.SchDoc * - Board.PrjPcb. Licensed to ACME.")
    assert result["active_tab"] == "Power.SchDoc"
    assert result["tab_type"] == "schematic"


def test_dxp_title_without_document_keeps_tab_from_script():
    result = _dxp_status("Altium Designer Summer 09 - PCB_Project1.PrjPCB. Licensed to ACME.",
                         active_tab="Sheet1.SchDoc")
    assert result["project_file"] == "PCB_Project1.PrjPCB"
    assert result["active_tab"] == "Sheet1.SchDoc"
    assert result["tab_type"] == "schematic"


def test_dxp_non_document_segment_is_not_a_tab():
    result = _dxp_status("Altium Designer (14.3) - Home Page - Board.PrjPcb. Licensed to ACME.")
    assert result["project_file"] == "Board.PrjPcb"
    assert result["active_tab"] == ""
    assert result["tab_type"] == "other"


def test_dxp_free_document_reports_no_sheet_open():
    result = _dxp_status(r"Altium Designer (14.3) - C:\Work\Sheet1.SchDoc - Free Documents. Licensed to ACME.")
    assert result["project_file"] == ""
    assert result["warning"] == "no_sheet_open"


def test_x2_title_is_not_reparsed():
    payload = {"running": True, "process": "X2", "project_file": "MyProject.PrjPcb", "project_path": "",
               "active_tab": "PowerSupply.SchDoc",
               "window_title": r"Altium Designer (14.3) - C:\Other\Sheet.SchDoc - Other.PrjPcb"}
    with patch("server.altium._run_ps", return_value=json.dumps(payload)):
        result = AltiumClient().get_status()
    assert result["project_file"] == "MyProject.PrjPcb"
    assert result["active_tab"] == "PowerSupply.SchDoc"
    assert "window_title" not in result


def test_null_process_is_treated_as_x2():
    payload = {"running": True, "process": None, "project_file": "MyProject.PrjPcb", "project_path": "",
               "active_tab": "PowerSupply.SchDoc", "window_title": "MyProject.PrjPcb - Altium Designer"}
    with patch("server.altium._run_ps", return_value=json.dumps(payload)):
        result = AltiumClient().get_status()
    assert result["project_file"] == "MyProject.PrjPcb"
    assert "warning" not in result


@pytest.mark.parametrize("script", ["get_altium_status.ps1", "generate_protel_netlist.ps1"])
def test_scripts_look_for_both_altium_process_names(script):
    from server.altium import SCRIPTS_DIR
    assert 'Get-Process -Name "X2", "DXP"' in (SCRIPTS_DIR / script).read_text(encoding="utf-8")


# --- validate_active_document ---

from server.altium import validate_active_document

SHEET_PATHS = [
    "C:/Projects/MyProject/PowerSupply.SchDoc",
    "C:/Projects/MyProject/MCU.SchDoc",
]
PRJ_PCB = "C:/Projects/MyProject/MyProject.PrjPcb"
PROJECT_ROOT = "C:/Projects/MyProject"


def test_validate_returns_tab_when_everything_matches():
    status = {"running": True, "project_file": "MyProject.PrjPcb", "active_tab": "PowerSupply.SchDoc", "tab_type": "schematic"}
    result = validate_active_document(status, PROJECT_ROOT, PRJ_PCB, SHEET_PATHS)
    assert result == "PowerSupply.SchDoc"


def test_validate_warns_when_altium_not_running():
    status = {"running": False}
    result = validate_active_document(status, PROJECT_ROOT, PRJ_PCB, SHEET_PATHS)
    assert result["warning"] == "altium_not_running"


def test_validate_warns_when_project_mismatch():
    status = {"running": True, "project_file": "OtherProject.PrjPcb", "active_tab": "PowerSupply.SchDoc", "tab_type": "schematic"}
    result = validate_active_document(status, PROJECT_ROOT, PRJ_PCB, SHEET_PATHS)
    assert result["warning"] == "active_document_outside_project"


def test_validate_warns_when_sheet_not_in_project():
    status = {"running": True, "project_file": "MyProject.PrjPcb", "active_tab": "ForeignSheet.SchDoc", "tab_type": "schematic"}
    result = validate_active_document(status, PROJECT_ROOT, PRJ_PCB, SHEET_PATHS)
    assert result["warning"] == "active_document_outside_project"


def test_validate_warns_when_no_sheet_open():
    status = {"running": True, "project_file": "", "active_tab": "PowerSupply.SchDoc", "tab_type": "schematic", "warning": "no_sheet_open"}
    result = validate_active_document(status, PROJECT_ROOT, PRJ_PCB, SHEET_PATHS)
    assert result["warning"] == "no_sheet_open"
