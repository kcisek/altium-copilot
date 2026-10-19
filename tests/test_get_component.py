import json
import pytest
from parsers.prj_pcb import VariantDefinition, VariantState
from main import _get_component_impl


@pytest.fixture
def default_vs():
    return VariantState([VariantDefinition(name="Default")])


@pytest.fixture
def dnp_vs():
    return VariantState([VariantDefinition(name="Prod", dnp_refdes=["R45"])])


def test_get_component_found(sample_netlist, default_vs):
    result = json.loads(_get_component_impl(sample_netlist, default_vs, "R45"))
    assert result["refdes"] == "R45"
    assert result["mpn"] == "RC0402FR-0710KL"
    assert result["value"] == "10K"
    assert result["dnp"] is False
    assert "1" in result["pins"]
    assert result["pins"]["1"]["net"] == "MCU_UART_TX"


def test_get_component_not_found(sample_netlist, default_vs):
    result = json.loads(_get_component_impl(sample_netlist, default_vs, "Z99"))
    assert result["error"] == "component_not_found"


def test_get_component_case_insensitive(sample_netlist, default_vs):
    result = json.loads(_get_component_impl(sample_netlist, default_vs, "r45"))
    assert result["refdes"] == "R45"


def test_get_component_dnp_annotated(sample_netlist, dnp_vs):
    result = json.loads(_get_component_impl(sample_netlist, dnp_vs, "R45"))
    assert result["dnp"] is True


def test_get_component_not_dnp(sample_netlist, dnp_vs):
    result = json.loads(_get_component_impl(sample_netlist, dnp_vs, "U1"))
    assert result["dnp"] is False


def test_get_component_includes_sheet(sample_netlist, default_vs):
    result = json.loads(_get_component_impl(sample_netlist, default_vs, "R45"))
    assert "sheet" in result
    assert result["sheet"] == "Comms"


def test_get_component_sheet_u1(sample_netlist, default_vs):
    result = json.loads(_get_component_impl(sample_netlist, default_vs, "U1"))
    assert result["sheet"] == "MCU"


def test_get_component_empty_components_returns_no_components_loaded(default_vs):
    netlist = {"nets": {"N": [("U1", "1")]}, "pin_to_net": {"U1": {"1": "N"}}, "components": {},
               "sheet_errors": [{"sheet": "Power", "path": "p", "error": "file not found"}]}
    result = json.loads(_get_component_impl(netlist, default_vs, "U1"))
    assert result["error"] == "no_components_loaded"
    assert result["sheet_errors"][0]["sheet"] == "Power"


# ---------- board-level annotation ----------

ANNOTATED = {
    "nets": {},
    "pin_to_net": {},
    "components": {
        "U1": {"mpn": "CSR1010", "sheet": "Bluetooth", "logical_refdes": "U4", "pins": {}},
        "U3": {"mpn": "LIS331DLTR", "sheet": "Accelerometer", "logical_refdes": "U1", "pins": {}},
        "C18": {"mpn": "1uF", "sheet": "Accelerometer", "logical_refdes": "C29", "pins": {}},
        "R3": {"mpn": "200R", "sheet": "Red_LED", "logical_refdes": "R1", "pins": {}},
        "R4": {"mpn": "200R", "sheet": "Red_LED", "logical_refdes": "R1", "pins": {}},
        "J1": {"mpn": "CR2032", "sheet": "Power", "pins": {}},
    },
}


def test_board_designator_comes_with_the_sheet_designator(default_vs):
    result = json.loads(_get_component_impl(ANNOTATED, default_vs, "U1"))
    assert (result["refdes"], result["logical_refdes"], result["mpn"]) == ("U1", "U4", "CSR1010")
    assert list(result)[:2] == ["refdes", "logical_refdes"]
    assert "logical_refdes" not in json.loads(_get_component_impl(ANNOTATED, default_vs, "J1"))


def test_sheet_designator_no_board_part_has_finds_its_part(default_vs):
    result = json.loads(_get_component_impl(ANNOTATED, default_vs, "c29"))
    assert (result["refdes"], result["logical_refdes"], result["mpn"]) == ("C18", "C29", "1uF")


def test_sheet_designator_placed_several_times_lists_its_parts(default_vs):
    result = json.loads(_get_component_impl(ANNOTATED, default_vs, "R1"))
    assert result["error"] == "component_not_found"
    assert result["board_designators"] == [
        {"refdes": "R3", "sheet": "Red_LED"},
        {"refdes": "R4", "sheet": "Red_LED"},
    ]
    assert "R3, R4" in result["message"]


def test_unknown_designator_is_still_not_found(default_vs):
    result = json.loads(_get_component_impl(ANNOTATED, default_vs, "U9"))
    assert result["error"] == "component_not_found"
    assert "board_designators" not in result
