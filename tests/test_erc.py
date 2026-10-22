"""The offline ERC (services/erc.py, run_erc): synthetic sheets (tests/sch_fixture.py)
compiled by the offline netlister, each check with the project's levels or Altium's
defaults, No Report and No ERC suppression, the Connection Matrix, the advisories, the
single-pin-net report, paging and the tool."""
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import main as main_mod
from altium import AltiumClient
from parsers.annotation import parse_annotation
from parsers.prj_pcb import VariantDefinition, VariantState
from services import erc
from services.erc import build_erc, page_erc
from services.project_options import ERC_CHECKS, MATRIX_DEFAULT, matrix_levels
from services.sch_netlister import ErcFacts, compile_netlist
from tests.sch_fixture import Sheet, project

IN, IO, OUT, PASSIVE, POWER = (f"|ELECTRICAL={n}" for n in (0, 1, 2, 4, 7))
# A 20000 x 200000 mil custom sheet, and one with a 100 mil snap grid as well.
BIG_SHEET = "|USECUSTOMSHEET=T|CUSTOMX=2000|CUSTOMY=20000"
GRID_SHEET = BIG_SHEET + "|SNAPGRIDON=T|SNAPGRIDSIZE=10"


def _compile(tmp_path, sheets: dict[str, Sheet], design: str = "",
             annotation: str | None = None):
    prj, read = project(tmp_path, sheets, design, annotation=annotation)
    entries = parse_annotation(Path(tmp_path) / "Board.Annotation") if annotation else []
    return compile_netlist(prj, entries, read_sheet=read)


def _model(compiled, levels: dict | None = None, matrix: dict | None = None) -> dict:
    return build_erc(compiled.erc, levels or {}, matrix or {})


def _rows(model: dict, violation: str | None = None, state: str = "reported") -> list[dict]:
    return [row for row in model["violations"] if row["_state"] == state
            and (violation is None or row["violation"] == violation)]


def _messages(model: dict, violation: str | None = None, state: str = "reported") -> list[str]:
    return [row["message"] for row in _rows(model, violation, state)]


def _sheet(*build) -> dict[str, Sheet]:
    sheet = Sheet()
    for step in build:
        step(sheet)
    return {"a.SchDoc": sheet}


# ---------- levels, defaults and No Report ----------

def test_levels_come_from_the_project_else_altiums_defaults(tmp_path):
    sheet = Sheet()
    sheet.part("R1", [("1", 0, 0, PASSIVE)])
    sheet.wire((0, 0), (50, 0))
    sheet.label("LONE", 50, 0)
    compiled = _compile(tmp_path, {"a.SchDoc": sheet})
    # Altium's documentation: Nets with only one pin is off by default.
    default = _model(compiled)
    assert not _rows(default, "Nets with only one pin")
    assert default["summary"]["suppressed"]["no_report"] == {"Nets with only one pin": 1}
    assert default["levels"]["by_check"]["Nets with only one pin"] == "No Report (default)"
    assert default["levels"]["source"].startswith("Altium defaults")
    raised = _model(compiled, {"Type59": "2"})
    (row,) = _rows(raised, "Nets with only one pin")
    assert row["level"] == "Error" and row["group"] == "Nets" and row["sheet"] == "a"
    assert row["message"] == "Net LONE has only one pin (Pin R1-1)"
    assert row["object"] == "Pin R1-1" and row["net"] == "LONE"
    assert (row["x_mil"], row["y_mil"]) == (0, 0) and row["fix"]
    assert raised["levels"]["by_check"]["Nets with only one pin"] == "Error"
    assert raised["summary"]["by_level"] == {"Fatal Error": 0, "Error": 1, "Warning": 0}


def test_every_registered_violation_is_an_altium_type_with_a_group_and_a_fix():
    names = [name for check in erc.REGISTRY for name in check.violations]
    assert len(names) == len(set(names)) == 32
    for name in names:
        assert name in ERC_CHECKS and name in erc.GROUPS and erc.FIXES[name]
    assert not set(names) & set(erc.NOT_CHECKED)
    with pytest.raises(ValueError, match="not Altium violation types"):
        erc.erc_check("Nets with a typo")


def test_a_type_set_to_no_report_is_counted_not_listed(tmp_path):
    sheet = Sheet()
    sheet.part("R1", [("1", 0, 0), ("2", 100, 0)])
    sheet.wire((0, 0), (0, 50))
    sheet.label("A", 0, 50)
    sheet.power("B", 0, 25)
    compiled = _compile(tmp_path, {"a.SchDoc": sheet})
    model = _model(compiled, {"Type14": "0"})
    assert not _rows(model, "Nets with multiple names")
    assert model["summary"]["suppressed"]["no_report"]["Nets with multiple names"] == 1
    listed = page_erc(model, min_level="all")["violations"]
    row = next(r for r in listed if r["violation"] == "Nets with multiple names")
    assert row["level"] == "No Report" and row["suppressed"] == "No Report"


# ---------- nets ----------

def test_single_pin_nets_and_the_report(tmp_path):
    sheet = Sheet()
    sheet.part("R1", [("1", 0, 0), ("2", 100, 0)])  # R1-2: nothing attached
    sheet.wire((0, 0), (0, 50))                       # R1-1: a dangling wire
    sheet.part("R2", [("1", 300, 0)])
    sheet.power("VX", 300, 0)                         # R2-1: a power port on the pin
    compiled = _compile(tmp_path, {"a.SchDoc": sheet})
    model = _model(compiled, {"Type59": "1"})
    assert sorted(_messages(model, "Nets with only one pin")) == [
        "Net NetR1_1 has only one pin (Pin R1-1)", "Net VX has only one pin (Pin R2-1)"]
    report = page_erc(_model(compiled), report="single_pin_nets")
    assert report["total"] == 2 and report["level"] == "No Report (default)"
    rows = {row["net"]: row for row in report["nets"]}
    assert rows["VX"]["pin"] == "R2-1" and rows["VX"]["attached"] == ["Power Object VX"]
    assert rows["NetR1_1"]["wired"] is True and rows["NetR1_1"]["sheet"] == "a"


def test_multiple_names_count_entries_and_ports_only_when_they_name_nets(tmp_path):
    def build(sheet):
        sheet.part("R1", [("1", 0, 0)])
        sheet.part("R2", [("1", 100, 0)])
        sheet.wire((0, 0), (100, 0))
        sheet.label("ALPHA", 50, 0)
        sheet.label("D[0..3]", 30, 0)  # a bus name names no net
        sheet.port("BETA", 100, 0)
    compiled = _compile(tmp_path / "a", _sheet(build))
    assert not _rows(_model(compiled), "Nets with multiple names")
    compiled = _compile(tmp_path / "b", _sheet(build), "AllowPortNetNames=1\n")
    (row,) = _rows(_model(compiled), "Nets with multiple names")
    assert row["message"] == "Nets Wire ALPHA has multiple names (Net Label ALPHA, Port BETA)"
    assert row["level"] == "Warning" and row["object"] == "Wire ALPHA"
    assert row["names"] == ["ALPHA", "BETA"]


def test_duplicate_nets_unless_sheet_numbers_are_appended(tmp_path):
    sheets = {}
    for name, part in (("a.SchDoc", "R1"), ("b.SchDoc", "R3")):
        sheet = Sheet()
        sheet.part(part, [("1", 0, 0)])
        sheet.part(part[0] + str(int(part[1]) + 1), [("1", 100, 0)])
        sheet.wire((0, 0), (100, 0))
        sheet.label("EN", 50, 0)
        sheets[name] = sheet
    compiled = _compile(tmp_path / "flat", sheets, "HierarchyMode=1\n")
    (row,) = _rows(_model(compiled), "Duplicate Nets")
    assert row["message"] == "Duplicate Net Names Wire EN" and row["level"] == "Error"
    # The netlist tells the copies apart with a suffix; Altium's ERC names both EN.
    assert row["net_copies"] == [{"sheets": ["a"], "pins": 2, "netlist_net": "EN"},
                                 {"sheets": ["b"], "pins": 2, "netlist_net": "EN_1"}]
    appended = _compile(tmp_path / "appended", sheets,
                        "HierarchyMode=1\nAppendSheetNumberToLocalNets=1\n")
    assert not _rows(_model(appended), "Duplicate Nets")


def _ported_power() -> dict[str, Sheet]:
    """top places c.SchDoc with entry VIN; c wires port VIN to a VCC power port and R6."""
    top = Sheet()
    entry = top.symbol("U_C", "c.SchDoc", [("VIN", 1)])[0]
    top.part("R1", [("1", entry[0] + 100, entry[1])])
    top.wire(entry, (entry[0] + 100, entry[1]))
    child = Sheet()
    child.port("VIN", 0, -100)
    child.part("R6", [("1", 100, -100)])
    child.wire((10, -100), (100, -100))
    child.power("VCC", 100, -100)
    return {"top.SchDoc": top, "c.SchDoc": child}


def test_a_power_port_on_a_ports_wiring_is_reduced_to_local(tmp_path):
    compiled = _compile(tmp_path, _ported_power())
    assert compiled.scope == "Hierarchical"
    (row,) = _rows(_model(compiled), "Global Power-Object scope changes")
    assert row["message"] == ("Global Power-Object VCC at 1000mil,-1000mil has been reduced "
                              "to local level by presence of port at 0mil,-1000mil")
    assert row["sheet"] == "c" and row["port"] == "VIN" and row["level"] == "Warning"
    assert row["port_object"] == "Port"
    flat = _compile(tmp_path / "flat", _ported_power(), "HierarchyMode=1\n")
    assert not _rows(_model(flat), "Global Power-Object scope changes")


def test_an_off_sheet_connector_on_a_power_ports_wiring_makes_it_local(tmp_path):
    sheets = _ported_power()
    sheets["top.SchDoc"].part("R2", [("1", 900, 0)])
    sheets["top.SchDoc"].power("VDD", 900, 0)
    child = sheets["c.SchDoc"]
    child.part("R7", [("1", 300, -300)])
    child.wire((150, -300), (300, -300))
    child.power("VDD", 300, -300)
    child.off_sheet("VDD", 150, -300)
    compiled = _compile(tmp_path, sheets)
    # Altium: a port-based object (port, off sheet connector) on a power port's wiring
    # makes the power object local; the compile keeps the child's VDD off the top's.
    assert not any({"R2", "R7"} <= {ref for ref, _ in pins} for pins in compiled.nets.values())
    rows = {row["port_object"]: row for row in _rows(_model(compiled),
                                                     "Global Power-Object scope changes")}
    assert rows["Off Sheet Connector"]["message"] == (
        "Global Power-Object VDD at 3000mil,-3000mil has been reduced to local level by "
        "presence of port at 1500mil,-3000mil")
    assert rows["Off Sheet Connector"]["port"] == "VDD" and rows["Port"]["port"] == "VIN"


# ---------- components ----------

def test_duplicate_and_undesignated_parts(tmp_path):
    sheet = Sheet()
    sheet.part("R1", [("1", 0, 0)], at=(0, 0))
    sheet.part("R1", [("1", 100, 0)], at=(100, 0))
    sheet.part("R?", [("1", 300, 0)], at=(300, 0))
    sheet.part("C?", [("1", 400, 0)], at=(400, 0))
    sheet.mask(390, -10, 410, 10)  # a masked part is not annotated, and not reported
    model = _model(_compile(tmp_path, {"a.SchDoc": sheet}))
    assert _messages(model, "Duplicate Part Designators") == [
        "Duplicate Component Designators R1 at 0mil,0mil and 1000mil,0mil"]
    assert "note" not in _rows(model, "Duplicate Part Designators")[0]
    (row,) = _rows(model, "Un-Designated parts requiring annotation")
    assert row["message"] == "Un-Designated Part R?" and row["level"] == "Warning"
    assert row["group"] == "Components" and (row["x_mil"], row["y_mil"]) == (3000, 0)


def test_a_clash_made_by_the_annotation_file_says_so(tmp_path):
    sheet = Sheet()
    sheet.part("R1", [("1", 0, 0)], uid="RA", at=(0, 0))
    sheet.part("R2", [("1", 100, 0)], uid="RB", at=(100, 0))
    annotation = "[DesignatorManager]\n" + "".join(
        f"LogicalDesignator{n}={logical}\nPhysicalDesignator{n}={physical}\n"
        f"UniqueID{n}=\\{uid}\nDocumentName{n}=a.SchDoc\nChannelName{n}=\n"
        for n, (logical, physical, uid) in enumerate([
            ("R2", "R1", "RB"), ("R9", "R9", "GONE\\RX")], 1))
    compiled = _compile(tmp_path, {"a.SchDoc": sheet}, annotation=annotation)
    assert compiled.erc.stale_annotation == 1
    (row,) = _rows(_model(compiled), "Duplicate Part Designators")
    assert row["note"] == (
        "R1 comes from the project's .Annotation file for the part(s) drawn as R2; 1 "
        ".Annotation entries name no placed part, so the file may be stale: check Altium's "
        "compile before re-annotating.")


def test_unused_and_duplicate_sub_parts(tmp_path):
    sheet = Sheet()
    # U1: three parts (PartCount 4), A placed twice, B once, C not at all.
    sheet.part("U1", [("1", 0, 0)], part_id=1, part_count=4, at=(0, 0))
    sheet.part("U1", [("1", 0, 100)], part_id=1, part_count=4, at=(0, 100))
    sheet.part("U1", [("2", 100, 0)], part_id=2, part_count=4, at=(100, 0))
    # U2: two parts, B compile-masked: placed, so not unused.
    sheet.part("U2", [("1", 300, 0)], part_id=1, part_count=3, at=(300, 0))
    sheet.part("U2", [("2", 400, 0)], part_id=2, part_count=3, at=(400, 0))
    sheet.mask(390, -10, 410, 10)
    model = _model(_compile(tmp_path, {"a.SchDoc": sheet}))
    assert _messages(model, "Components containing duplicate sub-parts") == [
        "Component U1A has duplicate sub-parts at 0mil,0mil and 0mil,1000mil"]
    (row,) = _rows(model, "Unused sub-part in component")
    assert row["message"] == "Component U1 has unused sub-part(s) (3)"
    assert row["unused_parts"] == ["C"] and row["level"] == "Warning"
    assert not _rows(model, "Duplicate Part Designators")


# ---------- the Connection Matrix ----------

def _net_of(*pins, label: str = "SIG") -> dict[str, Sheet]:
    """One net joining parts U1, U2 ... at pin 1, each with the given type fields."""
    sheet = Sheet()
    for index, extra in enumerate(pins, 1):
        sheet.part(f"U{index}", [("1", index * 100, 0, extra)])
    sheet.wire((100, 0), (len(pins) * 100, 0))
    sheet.label(label, 150, 0)
    return {"a.SchDoc": sheet}


def test_two_outputs_on_a_net_are_an_error_by_default(tmp_path):
    model = _model(_compile(tmp_path, _net_of(OUT, OUT, PASSIVE)))
    (row,) = _rows(model, "Nets containing multiple similar objects")
    assert row["message"] == "Net SIG contains multiple Output Pins (Pin U1-1, Pin U2-1)"
    assert row["level"] == "Error" and row["group"] == "Nets"
    assert row["pair"] == ["Output Pin", "Output Pin"]
    assert not _rows(model, "Nets with possible connection problems")


def test_passive_pins_never_conflict(tmp_path):
    model = _model(_compile(tmp_path, _net_of(PASSIVE, OUT, PASSIVE, PASSIVE)))
    assert not _rows(model, "Nets with possible connection problems")
    assert not _rows(model, "Nets containing multiple similar objects")
    matrix, own = matrix_levels({})
    assert not own and all(level == 0 for level in matrix[4])  # the Passive Pin row


def test_a_mixed_pair_takes_the_matrix_cells_level(tmp_path):
    compiled = _compile(tmp_path, _net_of(IO, OUT))
    (row,) = _rows(_model(compiled), "Nets with possible connection problems")
    assert row["message"] == "Net SIG contains IO Pin and Output Pin objects (Pin U1-1, Pin U2-1)"
    assert row["level"] == "Warning"
    # The project's own matrix: IO Pin x Output Pin raised to an error (both cells).
    rows = {f"L{i + 1}": line for i, line in enumerate(MATRIX_DEFAULT)}
    rows["L2"] = rows["L2"][:2] + "E" + rows["L2"][3:]
    rows["L3"] = rows["L3"][:1] + "E" + rows["L3"][2:]
    (row,) = _rows(_model(compiled, matrix=rows), "Nets with possible connection problems")
    assert row["level"] == "Error"
    # The type switched off in Error Reporting: counted, not reported.
    off = _model(compiled, {"Type12": "0"})
    assert off["summary"]["suppressed"]["no_report"] == {
        "Nets with possible connection problems": 1}


def test_ports_and_entries_are_judged_on_their_own_sheet(tmp_path):
    def sheets(child_output: bool) -> dict[str, Sheet]:
        top = Sheet()
        entry = top.symbol("U_C", "c.SchDoc", [("SIG", 1, "|IOTYPE=2")])[0]
        top.part("U1", [("1", entry[0] + 100, entry[1], OUT)])
        top.wire(entry, (entry[0] + 100, entry[1]))
        top.label("SIG", entry[0] + 50, entry[1])
        child = Sheet()
        child.port("SIG", 0, 0, io=2)
        child.part("U2", [("1", 100, 0, IN)])
        child.wire((10, 0), (100, 0), (100, -50))
        if child_output:
            child.part("U3", [("1", 100, -50, OUT)])
        return {"top.SchDoc": top, "c.SchDoc": child}
    # The parent's output drives the child's input port: correct, nothing to report.
    good = _model(_compile(tmp_path / "good", sheets(False)))
    assert not [row for row in good["violations"] if row["violation"] in erc.MATRIX_TYPES]
    bad = _model(_compile(tmp_path / "bad", sheets(True)))
    assert "Net SIG contains Output Pin and Input Port objects (Port SIG, Pin U3-1)" in \
        _messages(bad, "Nets with possible connection problems")
    assert _messages(bad, "Nets containing multiple similar objects") == [
        "Net SIG contains multiple Output Pins (Pin U1-1, Pin U3-1)"]


def test_unconnected_input_pins_and_no_erc(tmp_path):
    sheet = Sheet()
    sheet.part("U1", [("1", 0, 0, IN), ("2", 100, 0, IO), ("3", 200, 0, PASSIVE),
                      ("4", 300, 0, IN)])
    sheet.no_erc(300, 0)
    model = _model(_compile(tmp_path, {"a.SchDoc": sheet}))
    (row,) = _rows(model, "Unconnected objects in net")
    assert row["message"] == "Unconnected Pin U1-1 at 0mil,0mil" and row["level"] == "Warning"
    (row,) = _rows(model, "Nets containing floating input pins")
    assert row["message"] == "Net NetU1_1 contains floating input pins (Pin U1-1)"
    assert row["level"] == "Error" and row["group"] == "Nets"
    assert model["summary"]["suppressed"]["no_erc"] == {
        "Nets containing floating input pins": 1, "Unconnected objects in net": 1}
    listed = page_erc(model, min_level="all", checks=["Unconnected objects in net"])
    marked = [r for r in listed["violations"] if r.get("suppressed") == "No ERC"]
    assert marked[0]["suppressed_by_no_erc"] == {"sheet": "a", "x_mil": 3000, "y_mil": 0}


def test_an_input_whose_net_reaches_no_other_pin_floats(tmp_path):
    sheet = Sheet()
    sheet.part("U1", [("1", 0, 0, IN)])
    sheet.wire((0, 0), (100, 0))            # a stub reaching no other pin
    sheet.part("U2", [("1", 0, -100, IN)])
    sheet.wire((0, -100), (100, -100))
    sheet.no_erc(100, -100)                 # on U2's stub: the net's violations
    sheet.part("U3", [("1", 0, -200, IN)])
    sheet.part("U4", [("1", 100, -200, OUT)])
    sheet.wire((0, -200), (100, -200))      # driven: no floating input
    model = _model(_compile(tmp_path, {"a.SchDoc": sheet}))
    assert _messages(model, "Nets containing floating input pins") == [
        "Net NetU1_1 contains floating input pins (Pin U1-1)"]
    assert _messages(model, "Nets with no driving source") == [
        "Net NetU1_1 has no driving source (Pin U1-1)"]
    assert not _rows(model, "Unconnected objects in net")  # wired
    assert model["summary"]["suppressed"]["no_erc"] == {
        "Nets containing floating input pins": 1, "Nets with no driving source": 1}


def test_an_unwired_sheet_entry_is_unconnected_though_its_net_goes_on(tmp_path):
    top = Sheet()
    entries = top.symbol("U_C", "c.SchDoc", [("SIG", 1), ("QUIET", 2), ("USED", 3)])
    top.no_erc(*entries[1])                 # QUIET left unwired on purpose
    top.part("R1", [("1", entries[2][0] + 100, entries[2][1])])
    top.wire(entries[2], (entries[2][0] + 100, entries[2][1]))
    child = Sheet()
    for row, name in enumerate(("SIG", "QUIET", "USED")):
        y = -100 * (row + 1)
        child.port(name, 0, y)
        child.part(f"U{row + 5}", [("1", 100, y, PASSIVE)])
        child.wire((10, y), (100, y))
    compiled = _compile(tmp_path, {"top.SchDoc": top, "c.SchDoc": child})
    assert compiled.scope == "Hierarchical"
    model = _model(compiled)
    # The entry's net goes on to the child's port and pin, but nothing touches the entry
    # on its own sheet: Altium's "not wired up to the rest of the circuit".
    (row,) = _rows(model, "Unconnected objects in net")
    assert row["message"] == "Unconnected Sheet Entry SIG at 2000mil,4900mil"
    assert row["sheet"] == "top" and row["level"] == "Warning"
    assert model["summary"]["suppressed"]["no_erc"] == {"Unconnected objects in net": 1}


def test_a_specific_no_erc_suppresses_only_what_it_names(tmp_path):
    sheet = Sheet()
    sheet.part("U1", [("1", 0, 0, IN)])
    sheet._add("|RECORD=22|LOCATION.X=0|LOCATION.Y=0|ISACTIVE=T|SUPPRESSALL=F"
               "|ERRORKINDSETTOSUPPRESS=Nets containing floating input pins")
    model = _model(_compile(tmp_path, {"a.SchDoc": sheet}))
    assert _messages(model, "Unconnected objects in net")
    assert not _rows(model, "Nets containing floating input pins")
    assert erc._specific_types("garbled", "") is None  # unreadable: everything
    assert "Unconnected objects in net" in erc._specific_types("", "x")


def test_nets_with_no_driving_source(tmp_path):
    model = _model(_compile(tmp_path / "a", _net_of(IN, PASSIVE)))
    (row,) = _rows(model, "Nets with no driving source")
    # Altium lists every pin of the net, the passive ones that fail to drive it included.
    assert row["message"] == "Net SIG has no driving source (Pin U1-1, Pin U2-1)"
    assert row["level"] == "Warning" and row["object"] == "Net SIG"
    assert row["inputs"] == ["U1-1"]
    for driver in (OUT, POWER, IO, "|ELECTRICAL=3", "|ELECTRICAL=5", "|ELECTRICAL=6"):
        driven = _model(_compile(tmp_path / driver[-1], _net_of(IN, PASSIVE, driver)))
        assert not _rows(driven, "Nets with no driving source")


# ---------- parameter sets and differential pairs ----------

def test_net_parameters_and_differential_pairs(tmp_path):
    sheet = Sheet()
    for row, name in enumerate(("TX_P", "TX_N", "RX_P")):
        y = -row * 100
        sheet.part(f"R{row + 1}", [("1", 0, y), ("2", 100, y)])
        sheet.part(f"R{row + 11}", [("1", 300, y)])
        sheet.wire((100, y), (300, y))
        sheet.label(name, 150, y)
        sheet.parameter_set(200, y, [("DifferentialPair", "True")])
    sheet.parameter_set(250, 0, [("Rule", "")])
    sheet.parameter_set(250, -100, [("", "x")])
    sheet.parameter_set(250, -500, [("Rule", "")])  # on no wire: not a net parameter
    compiled = _compile(tmp_path, {"a.SchDoc": sheet})
    assert compiled.nets["TX_P"] == [("R1", "2"), ("R11", "1")]  # directives join nothing
    model = _model(compiled)
    assert _messages(model, "Missing Negative Net in Differential Pair") == [
        "Missing Negative Net for differential pair RX, positive net RX_P"]
    assert not _rows(model, "Missing Positive Net in Differential Pair")
    assert _messages(model, "Net Parameters with no value") == [
        "Invalid net-parameter value at 2500mil,0mil"]
    assert _messages(model, "Net Parameters with no name") == [
        "Invalid net-parameter name at 2500mil,-1000mil"]


def _pair_half(sheet: Sheet, name: str, y: int, part: str) -> None:
    sheet.part(part, [("1", 0, y)])
    sheet.part(part + "0", [("1", 300, y)])
    sheet.wire((0, y), (300, y))
    sheet.label(name, 100, y)
    sheet.parameter_set(200, y, [("DifferentialPair", "True")])


def test_a_differential_pair_split_over_sheets_is_whole(tmp_path):
    a, b = Sheet(), Sheet()
    _pair_half(a, "D_P", 0, "R1")
    _pair_half(b, "D_N", 0, "R2")
    model = _model(_compile(tmp_path, {"a.SchDoc": a, "b.SchDoc": b}))
    assert not [row for row in model["violations"] if "Differential" in row["violation"]]


def test_a_channels_pair_net_pairs_only_within_its_channel(tmp_path):
    top, chan = Sheet(), Sheet()
    top.symbol("Repeat(CH,1,2)", "chan.SchDoc")
    _pair_half(top, "D_N", -500, "R1")   # the top sheet's own D_N
    _pair_half(chan, "D_P", 0, "R5")     # D_P_CH1, D_P_CH2: local to each channel
    compiled = _compile(tmp_path, {"top.SchDoc": top, "chan.SchDoc": chan},
                        "HierarchyMode=2\n")
    assert compiled.scope == "Hierarchical"
    model = _model(compiled)
    assert sorted(_messages(model, "Missing Negative Net in Differential Pair")) == [
        "Missing Negative Net for differential pair D, positive net D_P_CH1",
        "Missing Negative Net for differential pair D, positive net D_P_CH2"]
    assert _messages(model, "Missing Positive Net in Differential Pair") == [
        "Missing Positive Net for differential pair D, negative net D_N"]


# ---------- advisories ----------

def test_advisories_stay_apart_from_violations(tmp_path):
    sheet = Sheet()
    for row, (name, power) in enumerate((("3V3", True), ("+3.3V", True), ("VCC_3V3", False),
                                         ("3V3_A", True), ("GND", True), ("AGND", True))):
        y = -row * 100
        sheet.part(f"R{row + 1}", [("1", 0, y)])
        sheet.part(f"R{row + 11}", [("1", 100, y)])
        sheet.wire((0, y), (100, y))
        (sheet.power if power else sheet.label)(name, 50, y)
    sheet.label("Reset", 150, -700)
    sheet.part("R30", [("1", 150, -700)])
    sheet.label("RESET", 250, -800)
    sheet.part("R31", [("1", 250, -800)])
    model = _model(_compile(tmp_path, {"a.SchDoc": sheet}))
    advisories = {a["advisory"]: a for a in model["advisories"]}
    assert all(a["level"] == "info" for a in advisories.values())
    aliases = advisories["near_duplicate_power_aliases"]
    assert aliases["voltage"] == 3.3
    assert {n["net"] for n in aliases["nets"]} == {"+3.3V", "3V3", "VCC_3V3"}
    assert advisories["label_casing"]["names"] == ["RESET", "Reset"]
    assert [n["net"] for n in advisories["ground_nets"]["nets"]] == ["AGND", "GND"]
    assert not {row["violation"] for row in model["violations"]} & set(erc.ADVISORIES)


def test_a_qualifier_glued_to_the_voltage_names_another_rail():
    for plain in ("3V3", "+3.3V", "VCC_3V3", "P3V3", "VCC3V3", "VDD33", "5V0", "V3P3"):
        assert erc._rail_voltage(plain) is not None, plain
    for qualified in ("2V5OUT", "2V5out", "3V3A", "+5VA", "3V3_A", "VDD_IO_1V8"):
        assert erc._rail_voltage(qualified) is None, qualified


# ---------- floating objects, wires and directives ----------

def test_floating_labels_power_ports_and_unconnected_wires(tmp_path):
    sheet = Sheet(header=BIG_SHEET)
    sheet.part("R1", [("1", 100, 100), ("2", 200, 100)])
    sheet.wire((100, 100), (100, 150))
    sheet.label("ON_WIRE", 100, 130)       # on the wire: attached
    sheet.label("BESIDE", 103, 130)        # 30 mil beside it: floating
    sheet.power("VCC", 200, 100)           # on the pin end: attached
    sheet.power("VLOOSE", 400, 400)        # on nothing
    sheet.bus((500, 100), (600, 100))
    sheet.label("D[0..3]", 550, 100)       # on a bus: attached
    sheet.wire((700, 700), (800, 700))     # joined to nothing
    sheet.wire((700, 600), (800, 600))
    sheet.junction(750, 600)               # a junction alone connects nothing
    sheet.wire((700, 500), (800, 500))
    sheet.label("STUB", 800, 500)          # a labelled stub is connected to its label
    model = _model(_compile(tmp_path, {"a.SchDoc": sheet}))
    (row,) = _rows(model, "Floating net labels")
    assert row["message"] == "Floating Net Label BESIDE" and row["level"] == "Warning"
    assert row["object"] == "Net Label BESIDE" and (row["x_mil"], row["y_mil"]) == (1030, 1300)
    assert _messages(model, "Floating power objects") == ["Floating Power Object VLOOSE"]
    assert sorted(_messages(model, "Unconnected wires")) == [
        "Unconnected line 7000mil,6000mil To 8000mil,6000mil",
        "Unconnected line 7000mil,7000mil To 8000mil,7000mil"]
    assert all(row["group"] == "Nets" for row in _rows(model, "Unconnected wires"))


def test_a_no_erc_suppresses_at_its_point(tmp_path):
    sheet = Sheet(header=BIG_SHEET)
    # A net-wide violation: U1-1 is fed only by a passive pin; the No ERC on R1-1 covers it.
    sheet.part("U1", [("1", 100, 100, IN), ("2", 100, 300, IN)])
    sheet.part("R1", [("1", 300, 100, PASSIVE)])
    sheet.wire((100, 100), (300, 100))
    sheet.no_erc(300, 100)
    # Object violations: the No ERC on the floating label covers it, not U1-2 beside it.
    sheet.label("LOOSE", 150, 300)
    sheet.no_erc(150, 300)
    sheet.power("VX", 400, 400)
    sheet.wire((700, 700), (800, 700))
    sheet.no_erc(750, 700)                  # on the stray wire
    model = _model(_compile(tmp_path, {"a.SchDoc": sheet}))
    suppressed = model["summary"]["suppressed"]["no_erc"]
    assert suppressed == {"Floating net labels": 1, "Nets with no driving source": 1,
                          "Unconnected wires": 1}
    assert _messages(model, "Unconnected objects in net") == [
        "Unconnected Pin U1-2 at 1000mil,3000mil"]
    assert _messages(model, "Floating power objects") == ["Floating Power Object VX"]
    (row,) = _rows(model, "Floating net labels", state="no_erc")
    assert row["suppressed_by_no_erc"] == {"sheet": "a", "x_mil": 1500, "y_mil": 3000}


def test_floating_directives(tmp_path):
    sheet = Sheet(header=BIG_SHEET)
    sheet.part("R1", [("1", 100, 100), ("2", 200, 100)])
    sheet.wire((100, 100), (200, 100))
    sheet.parameter_set(150, 100, [("Rule", "x")])                 # on the wire
    sheet.parameter_set(150, 300, [("Rule", "x")])                 # on nothing
    sheet.parameter_set(150, 400, [("DifferentialPair", "True")])  # on nothing
    sheet.symbol("U_S", "b.SchDoc", at=(500, 500), size=(100, 60))
    sheet.parameter_set(550, 480, [("Rule", "x")])                 # on a sheet symbol
    sheet.no_erc(900, 900)                                          # on nothing
    model = _model(_compile(tmp_path, {"a.SchDoc": sheet}))
    assert sorted(_messages(model, "Floating Directive Object")) == [
        "Floating Differential Pair directive at 1500mil,4000mil",
        "Floating NoERC directive at 9000mil,9000mil",
        "Floating Parameter Set directive at 1500mil,3000mil"]
    assert {row["level"] for row in _rows(model, "Floating Directive Object")} == {"Warning"}


def test_duplicate_pins_and_near_miss_wire_ends(tmp_path):
    sheet = Sheet(header=BIG_SHEET)
    sheet.part("J1", [("1", 100, 100), ("1", 100, 200), ("2", 100, 300)])
    sheet.part("R1", [("1", 300, 100)])
    # A wire drawn to R1-1 whose end stops 5 mil short of it.
    sheet._add("|RECORD=27|LOCATIONCOUNT=2|X1=200|Y1=100|X2=299|X2_FRAC=50000|Y2=100")
    sheet.label("NM", 250, 100)
    model = _model(_compile(tmp_path, {"a.SchDoc": sheet}))
    (row,) = _rows(model, "Components with duplicate pins")
    assert row["message"] == "Duplicate pins in component Pin J1-1 and Pin J1-1"
    assert row["at"] == ["1000mil,1000mil", "1000mil,2000mil"] and row["level"] == "Warning"
    (advisory,) = [a for a in model["advisories"] if a["advisory"] == "near_miss_wire_ends"]
    assert advisory["level"] == "info" and advisory["sheet"] == "a"
    assert advisory["ends"] == [{"pin": "R1-1", "x_mil": 2995, "y_mil": 1000,
                                 "distance_mil": 5.0}]


# ---------- ports, sheet entries and documents ----------

def _hierarchy(entries, ports, missing: bool = False) -> dict[str, Sheet]:
    top = Sheet(header=BIG_SHEET)
    top.symbol("U_C", "c.SchDoc", [(name, i + 1) for i, name in enumerate(entries)],
               at=(100, 5000))
    if missing:
        top.symbol("U_X", "gone.SchDoc", at=(500, 5000))
    child = Sheet(header=BIG_SHEET)
    for i, name in enumerate(ports):
        child.port(name, 100, 5000 - i * 100)
    return {"top.SchDoc": top, "c.SchDoc": child}


def test_ports_and_sheet_entries_with_no_match(tmp_path):
    compiled = _compile(tmp_path, _hierarchy(["A", "B"], ["A", "C"], missing=True))
    assert compiled.scope == "Hierarchical"
    model = _model(compiled)
    assert _messages(model, "Sheet Entry not linked to child sheet") == [
        "Sheet-Entry B not matched to Port at 2000mil,49800mil"]
    (row,) = _rows(model, "Port not linked to parent sheet symbol")
    assert row["message"] == "Port C not matched to Sheet-Entry at 1000mil,49000mil"
    assert row["sheet"] == "c" and row["parent"] == "top" and row["symbol"] == "U_C"
    assert row["level"] == "Error" and row["group"] == "Documents"
    assert _messages(model, "Missing child sheet for sheet symbol") == [
        "Missing child-sheet in gone.SchDoc in Symbol U_X"]
    # Flat scope: ports join by name, so a port needs no entry above it.
    flat = _model(_compile(tmp_path / "flat", _hierarchy(["A", "B"], ["A", "C"]),
                           "HierarchyMode=1\n"))
    assert not _rows(flat, "Port not linked to parent sheet symbol")
    assert _messages(flat, "Sheet Entry not linked to child sheet")


def test_unmatched_and_duplicate_ports_and_entries(tmp_path):
    a, b = Sheet(header=BIG_SHEET), Sheet(header=BIG_SHEET)
    a.port("X", 100, 5000)
    a.port("Y", 100, 4900)
    a.port("Z", 100, 4800)
    a.port("Z", 300, 4800)
    b.port("X", 100, 5000)
    b.symbol("U_B", "c.SchDoc", [("E", 1), ("E", 2)], at=(500, 5000))
    compiled = _compile(tmp_path, {"a.SchDoc": a, "b.SchDoc": b}, "HierarchyMode=3\n")
    # No Report unless the project raises it: Altium's own project files write it so.
    default = _model(compiled)
    assert default["levels"]["by_check"]["Port with no matching ports"] == \
        "No Report (assumed)"
    assert default["summary"]["suppressed"]["no_report"]["Port with no matching ports"] == 1
    model = _model(compiled, {"Type154": "1"})
    assert _messages(model, "Port with no matching ports") == [
        "Port Y Port with no matching ports detected at 1000mil,49000mil"]
    assert _messages(model, "Sheets containing duplicate ports") == [
        "Sheet contains duplicate ports Port Z at 1000mil,48000mil and 3000mil,48000mil"]
    assert _messages(model, "Sheet Symbol with duplicate entries") == [
        "Sheet Symbol with duplicate entries Sheet Entry E at 6000mil,49900mil and "
        "6000mil,49800mil"]
    assert model["levels"]["by_check"]["Port with no matching ports"] == "Warning"


def test_duplicate_sheet_numbers_and_sheet_names_clash(tmp_path):
    sheets = {}
    for name, number in (("a.SchDoc", "1"), ("b.SchDoc", "1"), ("c.SchDoc", "*")):
        sheet = Sheet(header=BIG_SHEET)
        sheet._add(f"|RECORD=41|NAME=SheetNumber|TEXT={number}")
        sheets[name] = sheet
    compiled = _compile(tmp_path, sheets)
    assert _model(compiled)["summary"]["suppressed"]["no_report"] == {
        "Duplicate sheet numbers": 1}
    model = _model(compiled, {"Type7": "1"})
    (row,) = _rows(model, "Duplicate sheet numbers")
    assert row["message"] == "Duplicate sheet number 1 on sheets a, b"
    assert row["sheets"] == ["a", "b"]
    facts = ErcFacts(project="Board", sheet_paths=[
        r"C:\p\one\x.SchDoc", r"C:\p\two\X.SchDoc", r"C:\p\y.SchDoc"])
    (row,) = _rows(build_erc(facts, {}, {}), "Sheet Names Clash")
    assert row["message"] == "Project Board contains several documents named x.SchDoc"
    assert row["level"] == "Error" and row["group"] == "Documents"


# ---------- the drawing: grid and sheet ----------

def test_off_grid_and_off_sheet_objects(tmp_path):
    sheet = Sheet(header=GRID_SHEET)                     # 100 mil snap grid
    sheet.part("R1", [("1", 105, 1000), ("2", 200, 1000)])
    sheet.wire((200, 1000), (300, 1000), (300, 1005))
    sheet.part("R2", [("1", 400, -100)])                  # below the sheet
    sheet.part("R3", [("1", 515, 1000)])
    sheet.no_erc(515, 1000)                               # its off-grid pin under a No ERC
    sheet.part("R4", [("1", 615, 1000)], at=(615, 1000))
    sheet.mask(600, 990, 630, 1010)                       # compile-masked: nothing
    model = _model(_compile(tmp_path, {"a.SchDoc": sheet}))
    assert sorted(_messages(model, "Off-grid object")) == [
        "Off grid Pin R1-1 at 1050mil,10000mil", "Off grid Wire at 3000mil,10050mil"]
    assert model["summary"]["suppressed"]["no_erc"]["Off-grid object"] == 1
    assert _messages(model, "Object not completely within sheet boundaries") == [
        "Off sheet Pin R2-1 at 4000mil,-1000mil"]
    # Many and rarely the cause: listed after the other violations of their level.
    order = [row["violation"] for row in _rows(model) if row["level"] == "Warning"]
    tail = {"Off-grid object", "Object not completely within sheet boundaries"}
    assert order[-3:] and set(order[-3:]) <= tail
    assert not {name for name in order[:-3]} & tail
    assert not model["notes"]
    # Without a snap grid in the sheet header, Off-grid object is not checked: a note says so.
    plain = Sheet()
    plain.part("R1", [("1", 105, 100)])
    model = _model(_compile(tmp_path / "plain", {"b.SchDoc": plain}))
    assert not _rows(model, "Off-grid object")
    assert model["notes"] == ["Off-grid object was not checked on 1 sheet(s) whose header "
                              "gives no snap grid, or has snap off: b."]
    # Snap off: Altium leaves SnapGridOn out of the header rather than writing F.
    off = Sheet(header=BIG_SHEET + "|SNAPGRIDSIZE=10")
    off.part("R1", [("1", 105, 100)])
    model = _model(_compile(tmp_path / "off", {"c.SchDoc": off}))
    assert not _rows(model, "Off-grid object") and model["notes"][0].endswith(": c.")


def test_a_page_lists_drawing_violations_one_row_per_sheet(tmp_path):
    sheet = Sheet(header=GRID_SHEET)
    for index in range(7):
        sheet.part(f"R{index + 1}", [("1", 105 + index * 100, 1000, PASSIVE)])
    sheet.part("U1", [("1", 0, 0, OUT)])
    sheet.part("U2", [("1", 100, 0, OUT)])
    sheet.wire((0, 0), (100, 0))
    model = _model(_compile(tmp_path, {"a.SchDoc": sheet}))
    page = page_erc(model)
    assert page["total"] == 2 and page["summary"]["reported"] == 8
    error, drawing = page["violations"]
    assert error["level"] == "Error"
    assert drawing["violation"] == "Off-grid object" and drawing["count"] == 7
    assert len(drawing["examples"]) == erc.DRAWING_EXAMPLES and drawing["sheet"] == "a"
    assert drawing["message"] == ("7 x Off-grid object on sheet a (e.g. Off grid Pin R1-1 "
                                  "at 1050mil,10000mil)")
    assert "drawing_note" in page
    each = page_erc(model, checks=["Off-grid object"])
    assert each["total"] == 7 and "drawing_note" not in each


def test_compile_masks_hide_every_object_they_cover(tmp_path):
    sheet = Sheet(header=GRID_SHEET)
    sheet.label("LOOSE", 105, 105)
    sheet.power("PLOOSE", 115, 105)
    sheet.wire((120, 120), (180, 120))
    sheet.parameter_set(150, 150, [("Rule", "x")])
    sheet.part("J1", [("1", 105, 110), ("1", 105, 130)], at=(105, 110))
    sheet.port("P", 155, 180)
    sheet.no_erc(185, 185)
    sheet.symbol("U_X", "gone.SchDoc", [("E1", 1)], at=(160, 175), size=(20, 10))
    sheet.mask(90, 90, 200, 200)
    model = _model(_compile(tmp_path, {"a.SchDoc": sheet}))
    assert model["violations"] == []
    assert not [a for a in model["advisories"] if a["advisory"] == "near_miss_wire_ends"]


# ---------- pages, filters and the tool ----------

def _many_outputs(tmp_path, count: int = 30):
    sheet = Sheet(header=BIG_SHEET)  # every object on the sheet
    for index in range(count):
        y = 15000 - index * 100
        sheet.part(f"U{index * 2 + 1}", [("1", 0, y, OUT)])
        sheet.part(f"U{index * 2 + 2}", [("1", 100, y, OUT)])
        sheet.wire((0, y), (100, y))
    sheet.part("R?", [("1", 500, 0)])
    return _compile(tmp_path, {"a.SchDoc": sheet})


def test_pages_and_filters(tmp_path):
    model = _model(_many_outputs(tmp_path))
    first = page_erc(model, limit=10)
    assert first["total"] == 31 and first["next_offset"] == 10 and len(first["violations"]) == 10
    assert first["summary"]["reported"] == 31 and "advisories" in first
    assert first["violations"][0]["level"] == "Error"  # errors first
    rest = page_erc(model, offset=30, limit=10)
    assert "next_offset" not in rest and "summary" not in rest and len(rest["violations"]) == 1
    assert page_erc(model, min_level="error")["total"] == 30
    assert page_erc(model, min_level="fatal")["total"] == 0
    assert page_erc(model, checks=["components"])["total"] == 1
    assert page_erc(model, checks=["Nets"])["total"] == 30
    assert page_erc(model, sheet="A")["total"] == 31
    assert page_erc(model, sheet="nope")["total"] == 0
    assert page_erc(model, checks=["nope"])["error"] == "unknown_checks"
    assert page_erc(model, min_level="loud")["error"] == "invalid_min_level"
    assert page_erc(model, report="other")["error"] == "invalid_report"


@pytest.fixture
def tool(tmp_path):
    """main's state with a loaded netlist, and the compile run_erc reads patched in."""
    compiled = _many_outputs(tmp_path, 120)
    prj = Path(tmp_path) / "Board.PrjPcb"
    prj.write_text(prj.read_text(encoding="utf-8")
                   + "[Electrical Rules Check]\nType59=1\n", encoding="utf-8")
    state = {"name": "Board", "root_dir": str(tmp_path), "prj_pcb_path": str(prj),
             "sheets": [{"name": "a", "path": str(Path(tmp_path) / "a.SchDoc")}]}
    client = AltiumClient()
    client._netlist = {"source": "offline", "nets": {}, "components": {"U1": {}}}
    with patch.object(main_mod, "_project", state), \
         patch.object(main_mod, "_variant_state", VariantState([VariantDefinition(name="A")])), \
         patch.object(main_mod, "_altium", client), \
         patch.object(client, "compiled_netlist", return_value=compiled) as compile_call:
        yield compile_call


def test_run_erc_tool(tool):
    result = json.loads(main_mod.run_erc())
    assert result["approximation"].startswith("An offline approximation of Altium's ERC")
    assert result["net_identifier_scope"] == "Global" and result["project"] == "Board"
    assert result["levels"]["by_check"]["Nets with only one pin"] == "Warning"
    assert result["total"] == 121 and result["next_offset"] == len(result["violations"])
    assert result["summary"]["by_level"]["Error"] == 120
    tool.assert_called_once()
    rest = json.loads(main_mod.run_erc(offset=result["next_offset"]))
    assert rest["offset"] == result["next_offset"] and "summary" not in rest
    single = json.loads(main_mod.run_erc(report="single_pin_nets"))
    assert single["total"] == 0 and single["nets"] == []
    assert json.loads(main_mod.run_erc(min_level="loud"))["error"] == "invalid_min_level"


def test_run_erc_shrinks_a_page_to_fit(tool, monkeypatch):
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 20_000)
    result = main_mod.run_erc()
    assert len(result) <= 20_000
    page = json.loads(result)
    assert page["page_limited_to"] < 100 and page["next_offset"] == page["page_limited_to"]


def test_run_erc_tool_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.run_erc())["error"] == "no_project"
    monkeypatch.setattr(main_mod, "_project", {"name": "Demo", "sheets": [],
                                               "prj_pcb_path": str(tmp_path / "x.PrjPcb")})
    monkeypatch.setattr(main_mod, "_variant_state", VariantState([]))
    client = AltiumClient()
    monkeypatch.setattr(main_mod, "_altium", client)
    client._netlist = {"nets": {"A": []}, "components": {}}
    assert json.loads(main_mod.run_erc())["error"] == "no_components_loaded"
    client._netlist = {"nets": {}, "components": {"R1": {}}}
    failed = json.loads(main_mod.run_erc())
    assert failed["error"] == "compile_failed"


def test_the_erc_model_is_reused_for_an_unchanged_compile(tool):
    project = main_mod._project
    compiled, model = main_mod._offline_erc(project)
    assert main_mod._offline_erc(project)[1] is model
    summary = main_mod._offline_erc_summary(project)
    assert summary["by_level"]["Error"] == 120 and summary["source"].startswith("run_erc")
    tool.side_effect = RuntimeError("no sheet of the project could be read")
    assert main_mod._offline_erc_summary(project) is None


def test_project_options_cite_the_offline_erc(tool):
    prj = Path(main_mod._project["prj_pcb_path"])
    prj.write_text(prj.read_text(encoding="utf-8").replace("Type59=1", "Type13=0"),
                   encoding="utf-8")
    with patch.object(main_mod._design_notes, "hierarchy",
                      side_effect=OSError("not read here")):
        result = json.loads(main_mod.get_project_options())
    offline = result["offline_erc"]
    assert offline["hidden_by_no_report"] == {"Nets containing multiple similar objects": 120}
    row = next(r for r in result["audit"]["erc_checks_off_or_downgraded"]
               if r["check"] == "Nets containing multiple similar objects")
    assert row["level"] == "No Report" and row["offline_erc_hidden"] == 120


def test_schematic_review_runs_the_erc_first():
    prompt = main_mod.SCHEMATIC_REVIEW_PROMPT
    assert prompt.index("run_erc") < prompt.index("choose a scope") < prompt.index("Phase 1")
    assert 'run_erc(sheet="<sheet>"' in prompt and "approximation" in prompt


def test_compiled_netlist_leaves_the_loaded_netlist(tmp_path):
    import altium
    from functools import partial
    prj, read = project(tmp_path, _net_of(OUT, OUT))
    client = AltiumClient()
    with patch.object(altium, "compile_netlist", partial(compile_netlist, read_sheet=read)):
        compiled = client.compiled_netlist(prj)
        assert client._netlist is None and compiled.erc.nets
        assert client.compile_offline(prj)["nets"] == {"SIG": [("U1", "1"), ("U2", "1")]}
    assert client._compiled[1] is compiled  # one compile, reused


def test_tool_registration_manifest_readme_and_instructions():
    tools = {tool.name: tool for tool in asyncio.run(main_mod.mcp.list_tools())}
    tool = tools["run_erc"]
    assert tool.annotations.readOnlyHint is True and tool.title == "Run ERC"
    assert tool.meta["anthropic/maxResultSizeChars"] == main_mod.QUERY_NET_MAX_RESULT_SIZE_CHARS
    assert set(tool.inputSchema["properties"]) == {"checks", "sheet", "min_level", "report",
                                                   "offset"}
    assert "approximation" in tool.description and "next_offset" in tool.description
    assert "run_erc" in main_mod.NETLIST_TOOLS
    assert main_mod.SERVER_INSTRUCTIONS.index("run_erc") > 4096
    root = Path(__file__).parents[1]
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    assert "run_erc" in {entry["name"] for entry in manifest["tools"]}
    assert "| `run_erc` |" in (root / "README.md").read_text("utf-8")


# ---------- Altium's public examples ----------

EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")


def _example(name: str):
    found = sorted(EXAMPLES.rglob(name)) if EXAMPLES.is_dir() else []
    if not found:
        pytest.skip(f"{name} is not installed")
    from parsers.prj_pcb import parse_prj_options
    compiled = compile_netlist(str(found[0]))
    options = parse_prj_options(str(found[0]))
    return compiled, build_erc(compiled.erc, options.erc_levels, options.connection_matrix)


def test_the_mini_pc_example_reports_nothing_and_its_pairs_are_whole():
    compiled, model = _example("MiniPC.PrjPcb")
    assert compiled.scope == "Hierarchical" and model["summary"]["reported"] == 0
    pairs = [s for s in compiled.erc.parameter_sets
             if any(name == "DifferentialPair" for name, _ in s.parameters)]
    assert pairs and all(s.net and s.net[-2:] in ("_P", "_N") for s in pairs)


def test_the_bluetooth_examples_no_erc_markers_suppress():
    _, model = _example("Bluetooth_Sentinel.PrjPcb")
    assert model["summary"]["suppressed"]["no_erc"]
    assert not _rows(model, "Unconnected objects in net")
    # The designer's No ERC sits on a sheet entry left unwired on its sheet.
    (row,) = _rows(model, "Unconnected objects in net", state="no_erc")
    assert row["object"].startswith("Sheet Entry ")
