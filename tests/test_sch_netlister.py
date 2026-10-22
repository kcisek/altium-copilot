"""The offline schematic netlister (services/sch_netlister.py): synthetic sheets for each
compile rule (tests/sch_fixture.py writes the records), the load path through
AltiumClient.compile_offline, and Altium's public AD25 examples compared with their
PcbDocs' pad nets when the examples are installed."""
from collections import Counter
from functools import partial
from pathlib import Path
from unittest.mock import patch

import olefile
import pytest

import altium
from altium import AltiumClient
from parsers.annotation import parse_annotation
from parsers.pcb_doc import decode_components, decode_nets, decode_pads, read_text_records
from parsers.prj_pcb import parse_prj_pcb
from parsers.sch_doc import hierarchy_from_stream
from services.sch_netlister import bus_members, compile_netlist
from tests.sch_fixture import Sheet, project

EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")


def _compile(tmp_path, sheets: dict[str, Sheet], design: str = "", annotation: str | None = None,
             devices=()):
    prj, read = project(tmp_path, sheets, design, devices=devices, annotation=annotation)
    entries = parse_annotation(tmp_path / "Board.Annotation") if annotation else []
    return compile_netlist(prj, entries, read_sheet=read)


def _net(result, refdes: str, pin: str) -> str | None:
    return result.pin_to_net.get(refdes, {}).get(pin)


def _joined(result, *pins: str) -> bool:
    """Whether the pins (R1-1 style) are all on one net."""
    nets = {_net(result, *pin.split("-")) for pin in pins}
    return len(nets) == 1 and None not in nets


def _warnings(result) -> dict[str, int]:
    return {warning["kind"]: warning["count"] for warning in result.warnings}


# ---------- one sheet ----------

def test_wires_join_pins_and_crossing_wires_stay_apart(tmp_path):
    s = Sheet()
    s.part("R1", [("1", 0, 0), ("2", 0, -100)])
    s.part("R2", [("1", 200, 0)])
    s.wire((0, 0), (200, 0))
    # A vertical wire crossing the first one without a junction, under a No ERC marker.
    s.part("R3", [("1", 100, 100)])
    s.part("R4", [("1", 100, -50)])
    s.wire((100, 100), (100, -50))
    s.no_erc(100, 0)
    result = _compile(tmp_path, {"a.SchDoc": s})
    assert result.nets == {"NetR1_1": [("R1", "1"), ("R2", "1")],
                           "NetR3_1": [("R3", "1"), ("R4", "1")]}
    assert _net(result, "R1", "2") is None  # a single-pin net is left out, as in a .NET
    assert set(result.parts) == {"R1", "R2", "R3", "R4"}


def test_junctions_and_wire_ends_on_wires_join(tmp_path):
    s = Sheet()
    s.part("R1", [("1", 0, 0)])
    s.part("R2", [("1", 200, 0)])
    s.wire((0, 0), (200, 0))
    s.part("R3", [("1", 100, 100)])
    s.part("R4", [("1", 100, -50)])
    s.wire((100, 100), (100, -50))
    s.junction(100, 0)
    # A T: R5's wire ends on the middle of R1's wire.
    s.part("R5", [("1", 50, -80)])
    s.wire((50, -80), (50, 0))
    result = _compile(tmp_path, {"a.SchDoc": s})
    assert _joined(result, "R1-1", "R2-1", "R3-1", "R4-1", "R5-1")


def test_a_junction_joins_wires_within_three_mil(tmp_path):
    s = Sheet()
    s.part("R1", [("1", 0, 0)])
    s.wire((0, 0), (200, 0))
    s.part("R2", [("1", 100, 100)])
    s.wire((100, 100), (100, -50))
    s.junction(100, 0, "|LOCATION.Y_FRAC=20000")  # 2 mil above the horizontal wire
    s.part("R3", [("1", 300, 0)])
    s.wire((300, 0), (400, 0))
    s.part("R4", [("1", 350, 100)])
    s.wire((350, 100), (350, -50))
    s.junction(350, 1)  # 10 mil off: no join
    result = _compile(tmp_path, {"a.SchDoc": s})
    assert _joined(result, "R1-1", "R2-1")
    assert not _joined(result, "R3-1", "R4-1")


def test_a_pin_end_joins_a_wire_within_three_mil(tmp_path):
    # Real sheets have pin ends 0.6-1.3 mil off the wire drawn to them, which Altium (and
    # the PCB made from its netlist) connects.
    s = Sheet()
    s.part("R1", [("1", 0, 0, "|LOCATION.X_FRAC=-13000")])  # 1.3 mil left of the wire end
    s.part("R2", [("1", 200, 0)])
    s.wire((0, 0), (200, 0))
    s.part("R3", [("1", 0, -100, "|LOCATION.Y_FRAC=-50000")])  # 5 mil below the wire end
    s.part("R4", [("1", 200, -100)])
    s.wire((0, -100), (200, -100))
    result = _compile(tmp_path, {"a.SchDoc": s})
    assert _joined(result, "R1-1", "R2-1")
    assert not _joined(result, "R3-1", "R4-1")


def test_auto_names_take_the_smallest_designator_in_natural_order(tmp_path):
    s = Sheet()
    s.part("R10", [("1", 0, 0)])
    s.part("R2", [("3", 100, 0)])
    s.wire((0, 0), (100, 0))
    assert list(_compile(tmp_path, {"a.SchDoc": s}).nets) == ["NetR2_3"]


def test_net_labels_name_nets_and_ties_go_to_the_case_insensitive_smallest(tmp_path):
    s = Sheet()
    s.part("R1", [("1", 0, 0)])
    s.part("R2", [("1", 100, 0)])
    s.wire((0, 0), (100, 0))
    s.label("beta", 30, 0)
    s.label("Alpha", 60, 0)
    result = _compile(tmp_path, {"a.SchDoc": s})
    assert list(result.nets) == ["Alpha"]
    assert _warnings(result)["multiple_net_names"] == 1


def test_a_net_label_and_a_power_port_of_one_name_are_one_net(tmp_path):
    s = Sheet()
    s.part("R1", [("1", 0, 0)])
    s.wire((0, 0), (100, 0))
    s.label("VDD", 50, 0)
    s.part("R2", [("1", 0, -100)])
    s.wire((0, -100), (100, -100))
    s.power("VDD", 100, -100)
    assert _compile(tmp_path, {"a.SchDoc": s}).nets == {"VDD": [("R1", "1"), ("R2", "1")]}


@pytest.mark.parametrize("design, name", [
    ("", "SIGA"),
    ("PowerPortNamesTakePriority=1\n", "PWR"),
])
def test_power_port_names_take_priority_when_set(tmp_path, design, name):
    s = Sheet()
    s.part("R1", [("1", 0, 0)])
    s.part("R2", [("1", 100, 0)])
    s.wire((0, 0), (100, 0))
    s.label("SIGA", 30, 0)
    s.power("PWR", 100, 0)
    assert list(_compile(tmp_path, {"a.SchDoc": s}, design).nets) == [name]


def test_hidden_pins_connect_only_through_a_hidden_net_name(tmp_path):
    s = Sheet()
    s.part("U1", [("1", 0, 0), ("2", 500, 500, "|PINCONGLOMERATE=4|HIDDENNETNAME=VCC"),
                  ("3", 0, 0, "|PINCONGLOMERATE=4")])
    s.part("R1", [("1", 100, 0)])
    s.wire((0, 0), (100, 0))
    s.part("R2", [("1", 0, -100)])
    s.wire((0, -100), (100, -100))
    s.power("VCC", 100, -100)
    result = _compile(tmp_path, {"a.SchDoc": s})
    assert result.nets["VCC"] == [("R2", "1"), ("U1", "2")]
    assert _net(result, "U1", "3") is None  # hidden and drawn on the wire: still nothing
    assert _joined(result, "U1-1", "R1-1")
    assert _warnings(result)["hidden_pins"] == 1


def test_compile_masks_remove_parts_and_labels(tmp_path):
    s = Sheet()
    s.mask(0, 0, 100, 100)
    s.part("R1", [("1", 50, 80)], at=(50, 50))           # inside the mask
    s.part("R2", [("1", -50, 50)], at=(-50, 50))
    s.part("R5", [("1", 150, 50)], at=(150, 50))
    s.wire((-50, 50), (150, 50))                          # passes through the mask
    s.label("MASKED", 50, 50)                             # inside it: removed
    s.wire((50, 80), (150, 80))
    s.part("R6", [("1", 150, 80)], at=(150, 80))
    result = _compile(tmp_path, {"a.SchDoc": s})
    assert "R1" not in result.parts and "R2" in result.parts
    assert result.nets == {"NetR2_1": [("R2", "1"), ("R5", "1")]}
    assert _warnings(result)["compile_masked"] == 1


def test_compile_masks_remove_what_lies_wholly_inside(tmp_path):
    s = Sheet()
    s.mask(0, 0, 100, 100)  # parts sit at their pin: outside it unless placed inside
    # A wire from inside the mask to R1 outside stays (only wholly covered wires go).
    s.part("R1", [("1", 200, 50)], at=(200, 50))
    s.part("R2", [("1", 200, 20)], at=(200, 20))
    s.wire((50, 50), (200, 50))
    s.wire((50, 50), (50, 20), (200, 20))
    # A port, a junction and an off-sheet connector inside the mask are removed.
    s.part("R3", [("1", 200, 80)], at=(200, 80))
    s.wire((50, 80), (200, 80))
    s.port("P", 40, 80)
    s.part("R4", [("1", 300, 80)], at=(300, 80))
    s.wire((300, 80), (400, 80))
    s.port("P", 400, 80)
    s.part("R5", [("1", 200, -50)], at=(200, -50))
    s.wire((70, 120), (70, -50), (200, -50))
    s.wire((30, 90), (150, 90))
    s.part("R6", [("1", 30, 150)], at=(30, 150))
    s.wire((30, 150), (30, 90))
    s.junction(70, 90)
    s.off_sheet("X", 90, 10)
    s.wire((90, 10), (90, -100), (200, -100))
    s.part("R7", [("1", 200, -100)], at=(200, -100))
    other = Sheet()
    other.part("R8", [("1", 0, 0)], at=(0, 0))
    other.wire((0, 0), (50, 0))
    other.off_sheet("X", 50, 0)
    # The parts of U1: part A outside the mask, part B inside it. Only part A compiles,
    # as parse_sch_doc's components have it: B's pin 5 connects nothing.
    s.part("U1", [("1", 300, 300)], part_id=1, part_count=3, at=(300, 300))
    s.part("R9", [("1", 400, 300)], at=(400, 300))
    s.wire((300, 300), (400, 300))
    s.part("U1", [("5", 20, 60)], part_id=2, part_count=3, at=(20, 60))
    s.part("R10", [("1", 20, -200)], at=(20, -200))
    s.wire((20, 60), (20, -200))
    result = _compile(tmp_path, {"a.SchDoc": s, "b.SchDoc": other}, "HierarchyMode=1\n")
    assert _joined(result, "R1-1", "R2-1")
    assert not _joined(result, "R3-1", "R4-1")
    assert not _joined(result, "R5-1", "R6-1")
    assert not _joined(result, "R7-1", "R8-1")
    assert result.pin_to_net["U1"] == {"1": "NetR9_1"} and "U1" in result.parts
    assert _net(result, "R10", "1") is None
    assert _warnings(result)["compile_masked"] == 1


def test_unannotated_parts_are_left_out(tmp_path):
    s = Sheet()
    s.part("R?", [("1", 0, 0)])
    s.part("R1", [("1", 100, 0)])
    s.part("R2", [("1", 200, 0)])
    s.wire((0, 0), (200, 0))
    result = _compile(tmp_path, {"a.SchDoc": s})
    assert set(result.parts) == {"R1", "R2"} and _joined(result, "R1-1", "R2-1")
    assert _warnings(result)["unannotated_parts"] == 1


def test_single_pin_nets_when_the_project_keeps_them(tmp_path):
    s = Sheet()
    s.part("R1", [("1", 0, 0), ("2", 0, -100)])
    s.label("LONE", 0, -100)
    result = _compile(tmp_path, {"a.SchDoc": s}, "NetlistSinglePinNets=1\n")
    assert result.nets == {"NetR1_1": [("R1", "1")], "LONE": [("R1", "2")]}


def test_parts_carry_the_footprint_and_the_comment_as_shown(tmp_path):
    s = Sheet()
    s.part("R1", [("1", 0, 0)], comment="=Value", params={"Value": "10k"}, footprint="R0603")
    s.part("C1", [("1", 100, 0)], comment="GRM188")
    s.wire((0, 0), (100, 0))
    result = _compile(tmp_path, {"a.SchDoc": s})
    assert result.parts == {"C1": {"footprint": "", "comment": "GRM188"},
                            "R1": {"footprint": "R0603", "comment": "10k"}}


# ---------- buses ----------

def test_bus_members_of_ranged_names():
    assert bus_members("D[0..3]") == ["D0", "D1", "D2", "D3"]
    assert bus_members("A[3..1]") == ["A3", "A2", "A1"]
    assert bus_members("CLK") is None


def _bus_sheet(first: str, second: str) -> Sheet:
    """A ranged port D[0..1] on a bus labelled D[0..1]; wires labelled D0 and D1 to the
    two parts' pins; a wire whose end touches the bus without joining it."""
    s = Sheet()
    s.port("D[0..1]", 0, 0)
    s.bus((10, 0), (100, 0))
    s.label("D[0..1]", 50, 0)
    s.part(first, [("1", 200, -50)])
    s.wire((120, -50), (200, -50))
    s.label("D0", 150, -50)
    s.part(second, [("1", 200, -100)])
    s.wire((120, -100), (200, -100))
    s.label("D1", 150, -100)
    # Two wires ending on the bus: buses and wires do not join, so neither do they.
    for x, part in ((70, first.replace("R", "X")), (90, first.replace("R", "Y"))):
        s.part(part, [("1", x, -60)])
        s.wire((x, 0), (x, -60))
    return s


def test_ranged_ports_carry_bus_members_across_sheets(tmp_path):
    result = _compile(tmp_path, {"a.SchDoc": _bus_sheet("R1", "R2"),
                                 "b.SchDoc": _bus_sheet("R3", "R4")}, "HierarchyMode=1\n")
    assert result.nets["D0"] == [("R1", "1"), ("R3", "1")]
    assert result.nets["D1"] == [("R2", "1"), ("R4", "1")]
    assert _net(result, "X1", "1") is None and _net(result, "Y1", "1") is None


def test_ranged_sheet_entries_carry_bus_members_down(tmp_path):
    top = Sheet()
    entry = top.symbol("U_B", "b.SchDoc", [("D[0..1]", 1)])[0]
    top.bus(entry, (entry[0] + 100, entry[1]))
    top.label("D[0..1]", entry[0] + 50, entry[1])
    top.part("R1", [("1", 400, 300)])
    top.wire((300, 300), (400, 300))
    top.label("D1", 350, 300)
    child = Sheet()
    child.port("D[0..1]", 0, 0)
    child.bus((10, 0), (100, 0))
    child.label("D[0..1]", 50, 0)
    child.part("R2", [("1", 200, -100)])
    child.wire((120, -100), (200, -100))
    child.label("D1", 150, -100)
    result = _compile(tmp_path, {"top.SchDoc": top, "b.SchDoc": child})
    assert result.scope == "Hierarchical"
    assert result.nets["D1"] == [("R1", "1"), ("R2", "1")]


def _bus_child(part: str) -> Sheet:
    """A port D[0..1] on a bus labelled D[0..1], member D0 on part's pin 1."""
    child = Sheet()
    child.port("D[0..1]", 0, 0)
    child.bus((10, 0), (100, 0))
    child.label("D[0..1]", 50, 0)
    child.part(part, [("1", 200, -100)])
    child.wire((120, -100), (200, -100))
    child.label("D0", 150, -100)
    return child


def test_ranged_entries_on_separate_buses_stay_apart(tmp_path):
    # Two symbols with an entry D[0..1] each; only U_A's entry is bussed to the top's
    # D[0..1] (member D0 on R1). U_B's entry is wired to nothing, like a scalar entry
    # left open: its child's D0 must not join U_A's by the entry name alone.
    top = Sheet()
    a = top.symbol("U_A", "a.SchDoc", [("D[0..1]", 1)])[0]
    top.symbol("U_B", "b.SchDoc", [("D[0..1]", 1)], at=(100, 100))
    top.bus(a, (a[0] + 100, a[1]))
    top.label("D[0..1]", a[0] + 50, a[1])
    top.part("R1", [("1", 400, 300)])
    top.wire((300, 300), (400, 300))
    top.label("D0", 350, 300)
    result = _compile(tmp_path, {"top.SchDoc": top, "a.SchDoc": _bus_child("R2"),
                                 "b.SchDoc": _bus_child("R3")})
    assert result.scope == "Hierarchical"
    assert _joined(result, "R1-1", "R2-1")
    assert not _joined(result, "R1-1", "R3-1")


def test_a_junction_joins_crossing_buses(tmp_path):
    # The entry's bus carries no label of its own; a crossing bus labelled D[0..1] joins
    # it under a junction, so the entry's members reach the top's D1 wire (R1).
    def top_sheet(junction: bool) -> Sheet:
        top = Sheet()
        entry = top.symbol("U_B", "b.SchDoc", [("D[0..1]", 1)])[0]
        top.bus(entry, (entry[0] + 200, entry[1]))
        top.bus((entry[0] + 100, entry[1] + 50), (entry[0] + 100, entry[1] - 50))
        top.label("D[0..1]", entry[0] + 100, entry[1] + 30)
        if junction:
            top.junction(entry[0] + 100, entry[1])
        top.part("R1", [("1", 400, 300)])
        top.wire((300, 300), (400, 300))
        top.label("D1", 350, 300)
        return top

    child = Sheet()
    child.port("D[0..1]", 0, 0)
    child.bus((10, 0), (100, 0))
    child.part("R2", [("1", 200, -100)])
    child.wire((120, -100), (200, -100))
    child.label("D1", 150, -100)
    joined = _compile(tmp_path / "j", {"top.SchDoc": top_sheet(True), "b.SchDoc": child})
    assert _joined(joined, "R1-1", "R2-1")
    crossing = _compile(tmp_path / "x", {"top.SchDoc": top_sheet(False), "b.SchDoc": child})
    assert not _joined(crossing, "R1-1", "R2-1")


def test_a_junction_does_not_join_a_wire_to_a_bus(tmp_path):
    # Wires from R1 and R2 end on one bus, each under a junction: wires and buses are
    # different layers, so the pins stay apart (Altium joins them only by net labels).
    s = Sheet()
    s.bus((0, 0), (300, 0))
    s.label("D[0..1]", 150, 0)
    for x, part in ((100, "R1"), (200, "R2")):
        s.part(part, [("1", x, -100)])
        s.wire((x, 0), (x, -100))
        s.junction(x, 0)
    result = _compile(tmp_path, {"a.SchDoc": s})
    assert not _joined(result, "R1-1", "R2-1")


# ---------- the net identifier scope ----------

def _flat_pair() -> dict[str, Sheet]:
    """Two top sheets: port P and label L on R1's wire on a; port P on R2's wire and
    label L on R3/R4's wire on b."""
    a, b = Sheet(), Sheet()
    a.port("P", 0, 0)
    a.part("R1", [("1", 50, 0)])
    a.wire((10, 0), (50, 0))
    a.label("L", 30, 0)
    b.port("P", 0, 0)
    b.part("R2", [("1", 50, 0)])
    b.wire((10, 0), (50, 0))
    b.part("R3", [("1", 0, -100)])
    b.part("R4", [("1", 50, -100)])
    b.wire((0, -100), (50, -100))
    b.label("L", 20, -100)
    return {"a.SchDoc": a, "b.SchDoc": b}


def test_flat_joins_ports_and_keeps_net_labels_on_their_sheet(tmp_path):
    result = _compile(tmp_path, _flat_pair())  # Automatic: ports, no top entries: Flat
    assert result.scope == "Flat"
    assert _joined(result, "R1-1", "R2-1") and not _joined(result, "R1-1", "R3-1")
    # Both nets are named L by their sheet's label: Altium's Duplicate Nets.
    assert {name for name in result.nets} == {"L", "L_1"}
    assert _warnings(result)["duplicate_net_names"] == 1


def test_global_joins_net_labels_across_sheets(tmp_path):
    result = _compile(tmp_path, _flat_pair(), "HierarchyMode=3\n")
    assert result.scope == "Global"
    assert result.nets == {"L": [("R1", "1"), ("R2", "1"), ("R3", "1"), ("R4", "1")]}


def test_a_port_and_a_net_label_of_one_name_do_not_join_by_name(tmp_path):
    a, b = Sheet(), Sheet()
    a.port("X", 0, 0)
    a.part("R1", [("1", 50, 0)])
    a.part("R2", [("1", 50, -50)])
    a.wire((10, 0), (50, 0), (50, -50))
    b.part("R3", [("1", 0, 0)])
    b.part("R4", [("1", 50, 0)])
    b.wire((0, 0), (50, 0))
    b.label("X", 20, 0)
    result = _compile(tmp_path, {"a.SchDoc": a, "b.SchDoc": b}, "HierarchyMode=3\n")
    assert not _joined(result, "R1-1", "R3-1")


def _hierarchy(top_power: bool = False) -> dict[str, Sheet]:
    """top: symbol U_C (entries SIG, VIN and NOPE) placing c.SchDoc; R1 on SIG with label
    TOPNAME, R5 on a VCC power port when top_power. c: port SIG on R2 with label AAA, port
    VIN wired to a VCC power port and R6, port LONE on R3/R4 (no entry)."""
    top = Sheet()
    sig, vin, _ = top.symbol("U_C", "c.SchDoc", [("SIG", 1), ("VIN", 2), ("NOPE", 3)])
    top.part("R1", [("1", sig[0] + 100, sig[1])])
    top.wire(sig, (sig[0] + 100, sig[1]))
    top.label("TOPNAME", sig[0] + 50, sig[1])
    top.part("R7", [("1", vin[0] + 100, vin[1])])
    top.wire(vin, (vin[0] + 100, vin[1]))
    if top_power:
        top.part("R5", [("1", 0, -300)])
        top.wire((0, -300), (50, -300))
        top.power("VCC", 50, -300)
    child = Sheet()
    child.port("SIG", 0, 0)
    child.part("R2", [("1", 100, 0)])
    child.wire((10, 0), (100, 0))
    child.label("AAA", 50, 0)
    child.port("VIN", 0, -100)
    child.part("R6", [("1", 100, -100)])
    child.wire((10, -100), (100, -100))
    child.power("VCC", 100, -100)
    child.port("LONE", 0, -200)
    child.part("R3", [("1", 100, -200)])
    child.part("R4", [("1", 100, -250)])
    child.wire((10, -200), (100, -200), (100, -250))
    return {"top.SchDoc": top, "c.SchDoc": child}


def test_hierarchical_entries_join_their_ports(tmp_path):
    result = _compile(tmp_path, _hierarchy())
    assert result.scope == "Hierarchical"
    assert _joined(result, "R1-1", "R2-1") and _joined(result, "R7-1", "R6-1")
    assert not _joined(result, "R3-1", "R1-1")
    assert _warnings(result)["entry_without_port"] == 1
    # Without Higher Level Names Take Priority the smallest label wins.
    assert _net(result, "R1", "1") == "AAA"


def test_higher_level_names_take_priority(tmp_path):
    result = _compile(tmp_path, _hierarchy(), "NameNetsHierarchically=1\n")
    assert _net(result, "R1", "1") == "TOPNAME"


@pytest.mark.parametrize("design, name", [
    ("", "NetR3_1"),
    ("AllowPortNetNames=1\n", "LONE"),
])
def test_ports_name_nets_only_when_allowed(tmp_path, design, name):
    assert _net(_compile(tmp_path, _hierarchy(), design), "R3", "1") == name


@pytest.mark.parametrize("design, name", [
    ("", "NetR1_1"),
    ("AllowSheetEntryNetNames=1\n", "EN"),
])
def test_sheet_entries_name_nets_only_when_allowed(tmp_path, design, name):
    top = Sheet()
    entry = top.symbol("U_C", "c.SchDoc", [("EN", 1)])[0]
    top.part("R1", [("1", entry[0] + 100, entry[1])])
    top.wire(entry, (entry[0] + 100, entry[1]))
    child = Sheet()
    child.port("EN", 0, 0)
    child.part("R2", [("1", 100, 0)])
    child.wire((10, 0), (100, 0))
    result = _compile(tmp_path, {"top.SchDoc": top, "c.SchDoc": child}, design)
    assert result.nets == {name: [("R1", "1"), ("R2", "1")]}


def test_hierarchical_power_port_wired_to_a_port_is_local(tmp_path):
    result = _compile(tmp_path, _hierarchy(top_power=True))
    assert _joined(result, "R7-1", "R6-1")
    assert _net(result, "R5", "1") is None  # the top VCC stays apart (and single)


def test_power_ports_are_global_and_local_under_strict_hierarchical(tmp_path):
    a, b = Sheet(), Sheet()
    for sheet, part in ((a, "R1"), (b, "R2")):
        sheet.part(part, [("1", 0, 0)])
        sheet.wire((0, 0), (50, 0))
        sheet.power("GND", 50, 0)
    sheets = {"a.SchDoc": a, "b.SchDoc": b}
    assert _compile(tmp_path / "flat", sheets, "HierarchyMode=1\n").nets == {
        "GND": [("R1", "1"), ("R2", "1")]}
    strict = _compile(tmp_path / "strict", sheets, "HierarchyMode=4\n")
    assert strict.scope == "Strict Hierarchical" and strict.nets == {}


def test_hidden_net_names_are_local_under_strict_hierarchical(tmp_path):
    a, b = Sheet(), Sheet()
    a.part("U1", [("1", 500, 500, "|PINCONGLOMERATE=4|HIDDENNETNAME=GND")])
    a.part("R1", [("1", 0, 0)])
    a.wire((0, 0), (50, 0))
    a.power("GND", 50, 0)
    b.part("U2", [("1", 500, 500, "|PINCONGLOMERATE=4|HIDDENNETNAME=GND")])
    sheets = {"a.SchDoc": a, "b.SchDoc": b}
    strict = _compile(tmp_path / "strict", sheets, "HierarchyMode=4\n")
    assert _joined(strict, "U1-1", "R1-1") and not _joined(strict, "U1-1", "U2-1")
    assert _joined(_compile(tmp_path / "flat", sheets, "HierarchyMode=1\n"),
                   "U1-1", "R1-1", "U2-1")


def test_off_sheet_connectors_join_the_top_sheets(tmp_path):
    a, b = Sheet(), Sheet()
    for sheet, part in ((a, "R1"), (b, "R2")):
        sheet.part(part, [("1", 0, 0)])
        sheet.wire((0, 0), (50, 0))
        sheet.off_sheet("X", 50, 0)
    result = _compile(tmp_path, {"a.SchDoc": a, "b.SchDoc": b}, "HierarchyMode=1\n")
    assert _joined(result, "R1-1", "R2-1")
    assert _warnings(result)["off_sheet_connectors"] == 2
    # Child sheets placed by different symbols are not joined by their connectors.
    top = Sheet()
    top.symbol("U_A", "a.SchDoc", [])
    top.symbol("U_B", "b.SchDoc", [], at=(400, 500))
    nested = _compile(tmp_path / "nested", {"top.SchDoc": top, "a.SchDoc": a, "b.SchDoc": b},
                      "HierarchyMode=1\n")
    assert not _joined(nested, "R1-1", "R2-1")


def test_the_hierarchy_reader_no_longer_reads_off_sheet_connectors_as_power_ports():
    s = Sheet()
    s.off_sheet("X", 50, 0)
    s.power("GND", 0, 0)
    assert hierarchy_from_stream(s.streams()[0]).power_ports == ["GND"]


# ---------- components ----------

def test_the_parts_of_a_component_join_across_sheets(tmp_path):
    a, b = Sheet(), Sheet()
    a.part("U1", [("1", 0, 0)], part_id=1, part_count=3)
    a.part("R1", [("1", 50, 0)])
    a.wire((0, 0), (50, 0))
    b.part("U1", [("5", 0, 0)], part_id=2, part_count=3)
    b.part("R2", [("1", 50, 0)])
    b.wire((0, 0), (50, 0))
    # The same part (id 1) twice is a duplicate designator, not a component.
    a.part("R9", [("1", 0, -100)])
    b.part("R9", [("1", 0, -100)])
    result = _compile(tmp_path, {"a.SchDoc": a, "b.SchDoc": b})
    assert result.pin_to_net["U1"] == {"1": "NetR1_1", "5": "NetR2_1"}
    assert list(result.parts).count("U1") == 1
    assert _warnings(result)["duplicate_designators"] == 1


# ---------- channels ----------

def _channels(designator: str = "Repeat(CH,1,2)") -> dict[str, Sheet]:
    """top: R1 on entry EN; a bus SIG[1..2] on entry Repeat(SIG) with labels SIG1 (R2)
    and SIG2 (R3). chan: port EN on R10, port SIG on R11, label LOC on R12/R13."""
    top = Sheet()
    en, sig = top.symbol(designator, "chan.SchDoc", [("EN", 1), ("Repeat(SIG)", 2)])
    top.part("R1", [("1", en[0] + 100, en[1])])
    top.wire(en, (en[0] + 100, en[1]))
    top.bus(sig, (sig[0] + 100, sig[1]))
    top.label("SIG[1..2]", sig[0] + 50, sig[1])
    for number, part in ((1, "R2"), (2, "R3")):
        y = -100 * number
        top.part(part, [("1", 500, y)])
        top.wire((400, y), (500, y))
        top.label(f"SIG{number}", 450, y)
    top.part("R4", [("1", 600, 0)])
    top.wire((en[0] + 100, en[1]), (600, en[1]), (600, 0))
    chan = Sheet()
    chan.port("EN", 0, 0)
    chan.part("R10", [("1", 100, 0)])
    chan.wire((10, 0), (100, 0))
    chan.port("SIG", 0, -100)
    chan.part("R11", [("1", 100, -100)])
    chan.wire((10, -100), (100, -100))
    chan.part("R12", [("1", 0, -200)])
    chan.part("R13", [("1", 100, -200)])
    chan.wire((0, -200), (100, -200))
    chan.label("LOC", 50, -200)
    return {"top.SchDoc": top, "chan.SchDoc": chan}


def test_repeat_channels_compile_once_per_channel(tmp_path):
    result = _compile(tmp_path, _channels())
    assert {"R10_CH1", "R10_CH2", "R13_CH2"} <= set(result.parts) and "R10" not in result.parts
    # A plain entry reaches every channel; Repeat(SIG) gives channel N the bus member SIGN.
    assert _joined(result, "R1-1", "R4-1", "R10_CH1-1", "R10_CH2-1")
    assert _joined(result, "R2-1", "R11_CH1-1") and _joined(result, "R3-1", "R11_CH2-1")
    # Local net labels carry the channel's room name.
    assert result.nets["LOC_CH1"] == [("R12_CH1", "1"), ("R13_CH1", "1")]
    assert result.nets["LOC_CH2"] == [("R12_CH2", "1"), ("R13_CH2", "1")]
    assert _warnings(result)["channel_designators"] == 8
    assert _warnings(result)["repeat_entries"] == 1  # unconfirmed by any sample: said so
    paths = {entry.physical: entry.logical for entry in result.channel_entries}
    assert paths["R10_CH2"] == "R10" and len(paths) == 8


def test_the_projects_channel_designator_format(tmp_path):
    design = "ChannelDesignatorFormatString=$Component$ChannelAlpha\n"
    result = _compile(tmp_path, _channels(), design)
    assert {"R10A", "R10B"} <= set(result.parts)
    # Room Naming Style Flat Alpha: channels A, B instead of 1, 2.
    alpha = _compile(tmp_path / "alpha", _channels(), "ChannelRoomNamingStyle=1\n")
    assert {"R10_CHA", "R10_CHB"} <= set(alpha.parts)


def test_repeat_channels_take_the_annotation_files_names_per_channel(tmp_path):
    # One UniqueId path for every channel of a Repeat symbol: the .Annotation file's
    # ChannelName tells the channels' designators apart.
    annotation = "[DesignatorManager]\n" + "".join(
        f"LogicalDesignator{n}={logical}\nPhysicalDesignator{n}=R{channel}{logical[1:]}\n"
        f"UniqueID{n}=\\S0000001\\{uid}\nDocumentName{n}=chan.SchDoc\nChannelName{n}=CH{channel}\n"
        for n, (channel, logical, uid) in enumerate(
            [(c, f"R1{i}", f"P000000{i + 1}") for c in (1, 2) for i in range(4)], 1))
    result = _compile(tmp_path, _channels(), annotation=annotation)
    assert {"R110", "R113", "R210", "R213"} <= set(result.parts)
    assert result.nets["LOC_CH1"] == [("R112", "1"), ("R113", "1")]
    assert result.nets["LOC_CH2"] == [("R212", "1"), ("R213", "1")]
    assert _joined(result, "R2-1", "R111-1") and _joined(result, "R3-1", "R211-1")
    assert "duplicate_designators" not in _warnings(result)
    assert "channel_designators" not in _warnings(result)


def test_a_repeat_range_running_backwards_places_nothing(tmp_path):
    result = _compile(tmp_path, _channels("Repeat(CH,3,1)"))
    assert _warnings(result)["bad_channel_range"] == 1
    assert not any("_CH" in name for name in result.parts)  # no channel compiled


def test_a_sheet_placed_twice_takes_the_annotation_files_names(tmp_path):
    top = Sheet()
    top.symbol("LED", "led.SchDoc", [], uid="SYMA")
    top.symbol("LED", "led.SchDoc", [], at=(400, 500), uid="SYMB")
    led = Sheet()
    led.part("R1", [("1", 0, 0)], uid="RUID")
    led.part("D1", [("1", 100, 0)], uid="DUID")
    led.wire((0, 0), (100, 0))
    led.label("DRV", 50, 0)
    annotation = "[DesignatorManager]\n" + "".join(
        f"LogicalDesignator{n}={logical}\nPhysicalDesignator{n}={physical}\n"
        f"UniqueID{n}=\\{symbol}\\{uid}\nDocumentName{n}=led.SchDoc\nChannelName{n}={room}\n"
        for n, (logical, physical, symbol, uid, room) in enumerate([
            ("R1", "R7", "SYMA", "RUID", "Red1"), ("D1", "D7", "SYMA", "DUID", "Red1"),
            ("R1", "R8", "SYMB", "RUID", "Red2"), ("D1", "D8", "SYMB", "DUID", "Red2")], 1))
    result = _compile(tmp_path, {"top.SchDoc": top, "led.SchDoc": led}, "HierarchyMode=2\n",
                      annotation=annotation)
    assert result.nets == {"DRV_Red1": [("D7", "1"), ("R7", "1")],
                           "DRV_Red2": [("D8", "1"), ("R8", "1")]}
    assert not result.channel_entries and "channel_designators" not in _warnings(result)
    assert result.part_paths == {"SYMA\\RUID": "R7", "SYMA\\DUID": "D7",
                                 "SYMB\\RUID": "R8", "SYMB\\DUID": "D8"}


def test_symbols_sharing_a_designator_are_numbered(tmp_path):
    top = Sheet()
    top.symbol("LED", "led.SchDoc", [])
    top.symbol("LED", "led.SchDoc", [], at=(400, 500))
    led = Sheet()
    led.part("R1", [("1", 0, 0)])
    led.part("D1", [("1", 100, 0)])
    led.wire((0, 0), (100, 0))
    result = _compile(tmp_path, {"top.SchDoc": top, "led.SchDoc": led})
    assert set(result.parts) == {"R1_LED1", "D1_LED1", "R1_LED2", "D1_LED2"}
    assert set(result.nets) == {"NetD1_LED1_1", "NetD1_LED2_1"}


def test_global_scope_joins_the_copies_and_names_without_a_room(tmp_path):
    top = Sheet()
    top.symbol("LED", "led.SchDoc", [])
    top.symbol("LED", "led.SchDoc", [], at=(400, 500))
    led = Sheet()
    led.part("R1", [("1", 0, 0)])
    led.part("D1", [("1", 100, 0)])
    led.wire((0, 0), (100, 0))
    led.label("DRV", 50, 0)
    led.part("R2", [("1", 0, -100)])
    led.part("D2", [("1", 100, -100)])
    led.wire((0, -100), (100, -100))
    led.port("PX", 100, -100)
    result = _compile(tmp_path, {"top.SchDoc": top, "led.SchDoc": led},
                      "HierarchyMode=3\nAllowPortNetNames=1\n")
    assert result.scope == "Global"
    assert result.nets["DRV"] == [("D1_LED1", "1"), ("D1_LED2", "1"),
                                  ("R1_LED1", "1"), ("R1_LED2", "1")]
    assert len(result.nets["PX"]) == 4 and set(result.nets) == {"DRV", "PX"}


def test_nested_copies_get_rooms_of_their_own(tmp_path):
    # mid.SchDoc placed by MA and MB places leaf.SchDoc once: the leaf's two copies
    # share their level's name (L), so a flat room name would give both the same
    # designators; each takes its name path instead.
    top = Sheet()
    top.symbol("MA", "mid.SchDoc", [])
    top.symbol("MB", "mid.SchDoc", [], at=(400, 500))
    mid = Sheet()
    mid.symbol("L", "leaf.SchDoc", [])
    leaf = Sheet()
    leaf.part("R1", [("1", 0, 0)])
    leaf.part("R2", [("1", 100, 0)])
    leaf.wire((0, 0), (100, 0))
    result = _compile(tmp_path, {"top.SchDoc": top, "mid.SchDoc": mid, "leaf.SchDoc": leaf})
    assert set(result.parts) == {"R1_MA_L", "R2_MA_L", "R1_MB_L", "R2_MB_L"}
    assert "duplicate_designators" not in _warnings(result)
    assert set(result.nets) == {"NetR1_MA_L_1", "NetR1_MB_L_1"}
    # Copied at one level only, the leaf keeps the flat room names (LA, LB).
    top = Sheet()
    top.symbol("M", "mid.SchDoc", [])
    mid = Sheet()
    mid.symbol("LA", "leaf.SchDoc", [])
    mid.symbol("LB", "leaf.SchDoc", [], at=(400, 500))
    flat = _compile(tmp_path / "flat", {"top.SchDoc": top, "mid.SchDoc": mid, "leaf.SchDoc": leaf})
    assert set(flat.parts) == {"R1_LA", "R2_LA", "R1_LB", "R2_LB"}


# ---------- harnesses ----------

def _harness_sheet(sda: str, scl: str) -> Sheet:
    """A harness connector with entries SDA and SCL wired to two parts, its harness run
    to port I2C."""
    s = Sheet()
    point, (sda_at, scl_at) = s.harness_connector((0, 100), (50, 100), [("SDA", 2), ("SCL", 4)],
                                                  primary=50)
    s.harness_line(point, (200, point[1]), (200, 0))
    s.port("I2C", 200, 0, harness="I2C")
    s.part(sda, [("1", -50, sda_at[1])])
    s.wire(sda_at, (-50, sda_at[1]))
    s.part(scl, [("1", -50, scl_at[1])])
    s.wire(scl_at, (-50, scl_at[1]))
    return s


def test_harness_entries_of_one_name_are_one_net(tmp_path):
    result = _compile(tmp_path, {"a.SchDoc": _harness_sheet("R1", "R2"),
                                 "b.SchDoc": _harness_sheet("R3", "R4")}, "HierarchyMode=1\n")
    assert _joined(result, "R1-1", "R3-1") and _joined(result, "R2-1", "R4-1")
    assert not _joined(result, "R1-1", "R2-1")
    assert _warnings(result)["harness_member_names"] == 2


# ---------- the project ----------

def test_missing_and_unreadable_sheets(tmp_path):
    top = Sheet()
    top.symbol("U_X", "missing.SchDoc", [])
    top.part("R1", [("1", 0, 0)])
    top.part("R2", [("1", 50, 0)])
    top.wire((0, 0), (50, 0))
    prj, read = project(tmp_path, {"top.SchDoc": top, "bad.SchDoc": Sheet()})

    def read_sheet(path):
        if Path(path).name == "bad.SchDoc":
            raise ValueError("not an OLE file")
        return read(path)

    result = compile_netlist(prj, read_sheet=read_sheet)
    assert result.nets == {"NetR1_1": [("R1", "1"), ("R2", "1")]}
    assert [(e["sheet"], e["error"]) for e in result.sheet_errors] == [
        ("bad", "ValueError: not an OLE file")]
    assert _warnings(result)["missing_child_sheet"] == 1


def test_a_recursive_hierarchy_compiles_each_sheet_once(tmp_path):
    # a places b and b places a: no sheet is a top, Altium cannot compile the cycle.
    a, b = Sheet(), Sheet()
    a.symbol("U_B", "b.SchDoc", [])
    a.part("R1", [("1", 0, 0)])
    a.part("R2", [("1", 50, 0)])
    a.wire((0, 0), (50, 0))
    b.symbol("U_A", "a.SchDoc", [])
    b.part("R3", [("1", 0, 0)])
    result = _compile(tmp_path, {"a.SchDoc": a, "b.SchDoc": b})
    assert set(result.parts) == {"R1", "R2", "R3"}
    assert result.nets == {"NetR1_1": [("R1", "1"), ("R2", "1")]}
    assert _warnings(result)["sheet_cycle"] == 1


def test_append_sheet_numbers_is_reported_not_guessed(tmp_path):
    s = Sheet()
    s.part("R1", [("1", 0, 0)])
    s.part("R2", [("1", 50, 0)])
    s.wire((0, 0), (50, 0))
    s.label("LOCAL", 20, 0)
    result = _compile(tmp_path, {"a.SchDoc": s}, "AppendSheetNumberToLocalNets=1\n")
    assert list(result.nets) == ["LOCAL"] and "append_sheet_numbers" in _warnings(result)


def test_device_sheets_compile_like_child_sheets(tmp_path):
    sheets = _hierarchy()
    result = _compile(tmp_path, sheets, devices=("c.SchDoc",))
    assert _joined(result, "R1-1", "R2-1")


# ---------- the load path ----------

def test_compile_offline_files_channel_copies_like_annotated_parts(tmp_path):
    prj, read = project(tmp_path, _channels())
    client = AltiumClient()
    with patch.object(altium, "compile_netlist", partial(compile_netlist, read_sheet=read)), \
         patch.object(altium, "parse_sch_doc", side_effect=AssertionError("read again")):
        netlist = client.compile_offline(prj)
    assert netlist["source"] == "offline" and netlist["net_file"] is None
    assert netlist["net_scope"] == "Hierarchical"
    copy = netlist["components"]["R10_CH2"]
    assert copy["logical_refdes"] == "R10" and copy["sheet"] == "chan"
    assert copy["pins"]["1"]["net"] == netlist["pin_to_net"]["R10_CH2"]["1"]
    assert copy["unique_paths"] and "R10" not in netlist["components"]
    assert {w["kind"] for w in netlist["compile_warnings"]} >= {"channel_designators"}


def test_compile_offline_reuses_an_unchanged_compile(tmp_path):
    prj, read = project(tmp_path, _channels())
    client = AltiumClient()
    calls = []

    def counted(*args, **kwargs):
        calls.append(1)
        return compile_netlist(*args, read_sheet=read, **kwargs)

    with patch.object(altium, "compile_netlist", counted):
        first = client.compile_offline(prj)
        second = client.compile_offline(prj)
        assert len(calls) == 1 and first is not second
        assert first["nets"] == second["nets"] and first["nets"] is not second["nets"]
        Path(prj).write_text(Path(prj).read_text(encoding="utf-8") + "\n", encoding="utf-8")
        client.compile_offline(prj)
    assert len(calls) == 2


def test_compile_offline_without_a_readable_sheet_fails(tmp_path):
    prj, _ = project(tmp_path, {"a.SchDoc": Sheet()})
    with pytest.raises(RuntimeError, match="no sheet of the project could be read"):
        AltiumClient().compile_offline(prj)


# ---------- Altium's public examples against their PcbDocs ----------

def _pcb_pin_nets(pcb_path: str, part_paths: dict[str, str]) -> dict[tuple[str, str], str]:
    """(physical designator, pad) -> PCB net, for parts the PCB names once."""
    with olefile.OleFileIO(pcb_path) as ole:
        def data(name):
            return ole.openstream(name).read() if ole.exists(name) else b""
        nets = decode_nets(read_text_records(data("Nets6/Data")))
        components = decode_components(read_text_records(data("Components6/Data")))
        pads, _ = decode_pads(data("Pads6/Data"))
    counts = Counter(c.refdes for c in components)
    names = {}
    for component in components:
        name = part_paths.get((component.source_unique_id or "").strip("\\").upper())
        if name and counts[component.refdes] == 1:
            names[component.index] = name
    found: dict[tuple[str, str], set] = {}
    for pad in pads:
        if pad.component in names and pad.pad_name and pad.net is not None:
            found.setdefault((names[pad.component], pad.pad_name), set()).add(nets[pad.net])
    return {key: next(iter(value)) for key, value in found.items() if len(value) == 1}


EXAMPLE_BOARDS = [
    # (folder, project, connected pins compared, nets compared). Bluetooth Sentinel's PCB
    # names six designators twice (a stale .Annotation file): those parts are left out.
    ("Bluetooth Sentinel", "Bluetooth_Sentinel", 136, 45),
    ("Mini PC", "MiniPC", 3975, 837),
    ("Mini PC", "PC4-SODIMM_V200_RC_A1_20141015", 1253, 229),
    ("Mini PC", "WiFi_miniPCIe", 111, 27),
    ("SpiritLevel-SL1", "SL1 Xilinx Spartan-IIE PQ208 Rev1.02", 517, 126),
]


def test_every_public_example_compiles():
    projects = sorted(EXAMPLES.glob("**/*.PrjPcb"))
    if not projects:
        pytest.skip("Altium's AD25 examples are not installed")
    for prj_path in projects:
        result = compile_netlist(str(prj_path))
        assert not result.sheet_errors and result.parts, prj_path.name


@pytest.mark.parametrize("folder, name, pins, nets", EXAMPLE_BOARDS)
def test_public_example_nets_match_the_pcb(folder, name, pins, nets):
    prj_path = next(iter((EXAMPLES / folder).glob(f"**/{name}.PrjPcb")), None)
    if prj_path is None:
        pytest.skip(f"Altium's {name} example is not installed")
    prj = parse_prj_pcb(str(prj_path))
    annotation = [entry for path in prj.annotation_paths for entry in parse_annotation(path)]
    result = compile_netlist(str(prj_path), annotation)
    pcb = _pcb_pin_nets(prj.pcb_doc_paths[0], result.part_paths)
    pcb_sizes = Counter(pcb.values())
    by_compiled: dict[str, set] = {}
    by_pcb: dict[str, set] = {}
    missing = compared = 0
    for key, pcb_net in pcb.items():
        mine = result.pin_to_net.get(key[0], {}).get(key[1])
        if mine is None:
            missing += pcb_sizes[pcb_net] > 1  # the PCB joins what the schematic leaves single
            continue
        compared += 1
        by_compiled.setdefault(mine, set()).add(pcb_net)
        by_pcb.setdefault(pcb_net, set()).add(mine)
    assert missing == 0
    assert all(len(found) == 1 for found in [*by_compiled.values(), *by_pcb.values()])
    assert (compared, len(by_compiled)) == (pins, nets)
    renamed = [mine for mine, found in by_compiled.items() if mine.lower() != next(iter(found)).lower()]
    assert renamed == []
