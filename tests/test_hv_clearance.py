"""Voltage-aware copper spacing (services/hv_clearance, check_hv_clearance): the IPC-2221B
and IPC-9592B lookups at every band edge, voltages (AC peak, phases, switch nodes,
groups), copper distances per layer type and object type on synthetic boards, the board
rule beside the standard, unknown voltages, paging and the tool. Generic names only."""

import json
import math
from pathlib import Path

import pytest

import server.main as main_mod
import server.services.hv_clearance as hv
from server.parsers.pcb_doc import (
    BareHole, CopperArea, PcbComponent, PcbData, PcbRule, PolygonPour, Primitive,
)
from server.parsers.prj_pcb import VariantDefinition, VariantState
from server.services import dfm_geometry as geometry
from server.services.pcb_index import PcbIndex
from tests.test_circuit_calcs import buck, res
from tests.test_pcb_health import _square
from tests.test_pcb_index import _board, _layer, _pad, _track
from tests.test_pull_resistors import _netlist

MM = 1 / 0.0254
NETS = ["GND", "HV_BUS", "SENSE", "+24V", "LINE_L", "LINE_N", "AUX", "SW1", "+48V"]
GND, HVB, SENSE, P24, LL, LN, AUX, SW, P48 = range(len(NETS))
LAYERS = {"top layer": 1, "l2-gnd": 2, "l3-sig": 3, "bottom layer": 32, "plane1": 39}
PARTS = [
    ("J1", {"1": ("1", "HV_BUS"), "2": ("2", "GND")}, {"description": "Terminal block"}),
    ("R1", {"1": ("1", "SENSE"), "2": ("2", "GND")}, {"description": "Resistor", "value": "10k"}),
    ("J2", {"1": ("1", "+24V"), "2": ("2", "GND")}, {"description": "Header 2 pin"}),
    ("J3", {"1": ("1", "LINE_L"), "2": ("2", "LINE_N")},
     {"description": "Mains inlet, IEC 60320"}),
    ("U1", {"1": ("VDD", "AUX", "Power"), "2": ("GND", "GND", "Power")},
     {"description": "Interface IC"}),
]


def _part(index, kind=0):
    return PcbComponent(index=index, refdes=f"U{index + 1}", source_uid_tail=None,
                        channel_path=None, x=0.0, y=0.0, rotation=0.0, side="top",
                        designator=f"U{index + 1}", kind=kind)


def _pcb(primitives=(), *, areas=(), pours=(), rules=(), bare_holes=(), components=(),
         plane=False):
    board = _board()
    if plane:
        board.layers.append(_layer(39, "Plane1", 3))
    pcb = PcbData(
        board=board, nets=list(NETS), components=list(components),
        primitives=list(primitives), pours=list(pours), layer_name_to_v6=dict(LAYERS),
        areas=list(areas), area_streams=("Regions6", "Fills6"), bare_holes=list(bare_holes),
        rules=list(rules),
    )
    return PcbIndex(pcb, None)


def _hv(text):
    levels, error = hv.parse_hv(text)
    assert error is None
    return levels


def _run(index, parts=PARTS, hv_text=None, **kwargs):
    kwargs.setdefault("limit", 1000)
    if hv_text:
        kwargs["hv"] = _hv(hv_text)
    return hv.run_hv_clearance_check(_netlist(parts), index=index, **kwargs)


def _findings(result, check=None):
    return [f for f in result["findings"] if check is None or f["check"] == check]


def _pair(result, first, second):
    return next(row for row in result["pairs"] if set(row["nets"]) == {first, second})


def _rule(name="Clearance", priority=1, scope1="All", scope2="All", **props):
    return PcbRule(name=name, kind="Clearance", kind_id=0, enabled=True, priority=priority,
                   scope1=scope1, scope2=scope2, net_scope="DifferentNets", comment="",
                   unique_id=None, from_schematic=False, properties=props)


def _via(net, x, y, width=30.0, hole=12.0):
    return Primitive(kind="via", layer=1, layer_to=32, net=net, component=None, x1=x, y1=y,
                     x2=x, y2=y, width=width, hole=hole)


def _hv_track(layer=1):
    return _track(HVB, layer, 0, 0, 1000, 0, width=20)  # copper from y -10 to 10


# ---------- the tables ----------

# Table 6-1 as cross-checked (Altair PollEx and EMA, every cell; see the module), typed
# again here so a change to the encoded table fails a test.
TABLE_6_1 = {
    "B1": (0.05, 0.05, 0.1, 0.1, 0.2, 0.2, 0.2, 0.2, 0.25, 0.0025),
    "B2": (0.1, 0.1, 0.6, 0.6, 0.6, 1.25, 1.25, 1.25, 2.5, 0.005),
    "B3": (0.1, 0.1, 0.6, 1.5, 3.2, 3.2, 6.4, 12.5, 12.5, 0.025),
    "B4": (0.05, 0.05, 0.13, 0.13, 0.4, 0.4, 0.4, 0.4, 0.8, 0.00305),
    "A5": (0.13, 0.13, 0.13, 0.13, 0.4, 0.4, 0.4, 0.4, 0.8, 0.00305),
    "A6": (0.13, 0.25, 0.4, 0.5, 0.8, 0.8, 0.8, 0.8, 1.5, 0.00305),
    "A7": (0.13, 0.13, 0.13, 0.13, 0.4, 0.4, 0.4, 0.8, 0.8, 0.00305),
}


@pytest.mark.parametrize("column", sorted(TABLE_6_1))
def test_every_band_at_and_past_its_edge(column):
    values = TABLE_6_1[column]
    assert hv.ipc2221_mm(column, 0.0) == (values[0], "0-15 V")
    for number, limit in enumerate(hv.IPC2221_LIMITS_V):
        # At the row's upper limit: that row.
        assert hv.ipc2221_mm(column, limit) == (values[number], hv.IPC2221_ROWS[number])
        # Just past it (15.5 V is between the 0-15 and 16-30 rows): the next row.
        above, label = hv.ipc2221_mm(column, limit + 0.5)
        if number + 1 < len(hv.IPC2221_LIMITS_V):
            assert (above, label) == (values[number + 1], hv.IPC2221_ROWS[number + 1])
        else:
            assert above == pytest.approx(values[8] + 0.5 * values[9])
            assert label.startswith(">500 V")


@pytest.mark.parametrize("column, volts, expected_mm", [
    ("B1", 600, 0.50),  # Altair's and Siemens EDA's worked example
    ("B2", 600, 3.00),  # viox.com's formula d500 + k x (V - 500)
    ("B1", 1000, 1.5), ("B2", 1000, 5.0), ("B4", 1000, 2.325),  # smpspowersupply.com
    ("B3", 600, 15.0), ("A5", 600, 1.105), ("A6", 600, 1.805), ("A7", 600, 1.105),
    ("B2", 500, 2.5), ("B3", 300, 12.5), ("B3", 301, 12.5), ("B2", 251, 1.25),
    ("B3", 151, 3.2), ("B3", 171, 6.4),
])
def test_increments_above_500_v_and_cross_checked_cells(column, volts, expected_mm):
    assert hv.ipc2221_mm(column, volts)[0] == pytest.approx(expected_mm)


def test_the_encoded_table_is_the_cross_checked_one():
    for column, values in TABLE_6_1.items():
        assert hv.IPC2221_MM[column] == values[:9]
        assert hv.IPC2221_PER_VOLT_MM[column] == values[9]
    assert set(hv.IPC2221_COLUMNS) == set(TABLE_6_1)


@pytest.mark.parametrize("volts, expected_mm", [
    (0, 0.13), (14.9, 0.13), (15, 0.25), (29.9, 0.25), (30, 0.4), (50, 0.6),
    (99.9, 1.099), (100, 1.1), (400, 2.6), (1000, 5.6),
])
def test_ipc9592(volts, expected_mm):
    assert hv.ipc9592_mm(volts)[0] == pytest.approx(expected_mm)


def test_columns_by_layer_type_coating_altitude_and_assembly():
    bare = hv.Options()
    assert bare.columns("inner", False) == ("B1",) and bare.columns("outer", True) == ("B2",)
    assert hv.Options(altitude_m=3050).columns("outer", False) == ("B2",)
    assert hv.Options(altitude_m=3051).columns("outer", False) == ("B3",)
    assert hv.Options(coating=True, altitude_m=5000).columns("outer", False) == ("B4",)
    assembly = hv.Options("ipc2221_assembly")
    assert assembly.columns("outer", True) == ("A6",)
    assert assembly.columns("outer", False) == ("B2",)
    assert assembly.columns("inner", True) == ("B1",)
    coated = hv.Options("ipc2221_assembly", coating=True)
    assert coated.columns("outer", True) == ("A7",) and coated.columns("outer", False) == ("A5",)
    high = hv.Options("ipc2221_assembly", altitude_m=4000)
    assert high.columns("outer", True) == ("A6", "B3")
    # Above 3050 m an uncoated lead takes the larger of A6 and B3.
    assert high.requirement(20, "outer", True)["column"] == "A6"  # 0.25 > 0.1
    assert high.requirement(200, "outer", True)["column"] == "B3"  # 6.4 > 0.8
    nine = hv.Options("ipc9592").requirement(400, "inner", False)
    assert nine["required_mm"] == pytest.approx(2.6) and nine["column"] is None
    assert bare.widest_mm(400) == 2.5 and hv.Options(coating=True).widest_mm(400) == 0.8


# ---------- arguments and voltages ----------

@pytest.mark.parametrize("text, expected", [
    ("400", (400.0, 0.0, 0.0)), ("-48", (-48.0, 0.0, 0.0)), ("400VDC", (400.0, 0.0, 0.0)),
    ("230AC", (0.0, 230 * math.sqrt(2), 0.0)), ("230VAC", (0.0, 230 * math.sqrt(2), 0.0)),
    ("230 Vrms", (0.0, 230 * math.sqrt(2), 0.0)), ("325pk", (0.0, 325.0, 0.0)),
    ("230AC@120", (0.0, 230 * math.sqrt(2), 120.0)), ("1.2kV", (1200.0, 0.0, 0.0)),
])
def test_parse_level(text, expected):
    assert hv.parse_level(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["abc", "-230AC", "400@90", "30kV", "", "230ACX"])
def test_parse_level_errors(text):
    assert hv.parse_level(text) is None


def test_parse_hv_groups_working_voltage_and_min_distance():
    assert hv.parse_hv("L=230AC, N=0,HV_BUS=400") == (
        {"L": pytest.approx((0, 325.269, 0), abs=1e-3), "N": (0.0, 0.0, 0.0),
         "HV_BUS": (400.0, 0.0, 0.0)}, None)
    assert set(hv.parse_hv("L,L_F=230AC")[0]) == {"L", "L_F"}
    assert hv.parse_hv(None) == (None, None) and hv.parse_hv(" ") == (None, None)
    for bad in ("L", "L=x", "L=230AC,N", "=400", "A:B"):
        assert hv.parse_hv(bad)[1]["error"] == "invalid_hv", bad
    assert hv.parse_groups("PRI:L,N,HV_*;SEC:GND") == (
        [("PRI", ["L", "N", "HV_*"]), ("SEC", ["GND"])], None)
    assert hv.parse_groups("A,B;C") == ([("G1", ["A", "B"]), ("G2", ["C"])], None)
    for bad in ("PRI:", "X:A;X:B", ":A"):
        assert hv.parse_groups(bad)[1]["error"] == "invalid_groups", bad
    assert hv.parse_working_voltage("250AC")[0] == pytest.approx(353.553, abs=1e-3)
    assert hv.parse_working_voltage("400") == (400.0, None)
    assert hv.parse_working_voltage(None) == (None, None)
    for bad in ("0", "x"):
        assert hv.parse_working_voltage(bad)[1]["error"] == "invalid_working_voltage"
    parsed, error = hv.parse_min_distance("2.5, HV_BUS=4, A&B=3", MM)
    assert error is None and parsed["all"] == pytest.approx(2.5 * MM)
    assert parsed["nets"] == {"HV_BUS": pytest.approx(4 * MM)}
    assert parsed["pairs"] == {("A", "B"): pytest.approx(3 * MM)}
    assert hv.parse_min_distance("100", 1.0)[0]["all"] == 100.0  # mil
    for bad in ("-1", "x", "&N=3", "=3", "A=0"):
        assert hv.parse_min_distance(bad, MM)[1]["error"] == "invalid_min_distance", bad


def test_pair_peak_dc_ac_phases_and_switch_nodes():
    dc400, zero = hv.Level(400.0), hv.Level(0.0)
    line = hv.Level(0.0, 230 * math.sqrt(2))
    assert hv.pair_peak(dc400, zero) == 400.0
    assert hv.pair_peak(line, zero) == pytest.approx(325.27, abs=0.01)
    assert hv.pair_peak(line, hv.Level(0.0, 230 * math.sqrt(2))) == pytest.approx(0.0)
    # Two phases 120 degrees apart: the line-to-line voltage, 400 V RMS.
    phase_b = hv.Level(0.0, 230 * math.sqrt(2), 120.0)
    assert hv.pair_peak(line, phase_b) == pytest.approx(230 * math.sqrt(6), abs=0.01)
    assert hv.pair_peak(line, dc400) == pytest.approx(725.27, abs=0.01)
    switch = hv.Level(24.0, 24.0, switching=True)  # 0 to 48 V
    assert hv.pair_peak(switch, zero) == 48.0 and hv.pair_peak(switch, hv.Level(48.0)) == 48.0
    assert hv.pair_peak(switch, hv.Level(24.0, 24.0, switching=True)) == 48.0
    assert switch.text() == "0 to 48 V (switch node)" and switch.extreme == 48.0
    assert line.text() == "230 V AC (325.27 V peak)"


@pytest.mark.parametrize("name, words", [
    ("HV_SENSE", "HV"), ("MAINS_L", "MAINS"), ("N", "N"), ("LINE_L", "LINE"),
    ("AC_IN", "AC"), ("PE", "PE"), ("230VAC_IN", "230 V"), ("DC_LINK", "DCLINK"),
    ("DCLINK", "DCLINK"), ("HV-BUS", "HV, HVBUS"), ("BUS_2KV", "kilovolts"),
    ("PHASE_A", "PHASE"), ("L_F", "L"), ("N_F", "N"), ("AC_L", "AC"), ("VAC_IN", "VAC"),
    ("+400V_BUS", "400 V"), ("400VDC", "400 V"), ("L1", "L1"),
])
def test_hv_looking_names(name, words):
    assert hv.hv_name_reason(name).endswith(words), name


@pytest.mark.parametrize("name", ["TX_N", "CLK_N", "USB_DP", "12V", "48V", "3V3", "SDA",
                                  "L_SENSE_OUT", "SPARE_AC_X",
                                  # an active-low or enable lead, a status or audio word
                                  "N_CS", "L_EN", "PE_RESET", "AC_OK", "AC_DET", "LINE_OUT",
                                  # volts that are not a word of their own
                                  "U100VCC", "BUS_2KVA"])
def test_names_that_do_not_look_high_voltage(name):
    assert hv.hv_name_reason(name) is None, name


# ---------- distances per layer type and object type ----------

def test_track_spacing_per_layer_type():
    index = _pcb([
        _hv_track(1), _track(GND, 1, 0, 60, 1000, 60, width=20),  # 40 mil gap, outer
        _hv_track(3), _track(GND, 3, 0, 35, 1000, 35, width=20),  # 15 mil gap, inner
    ])
    result = _run(index, hv_text="HV_BUS=400")
    (finding,) = _findings(result, "spacing_below_standard")
    assert finding["nets"] == ["GND", "HV_BUS"] and finding["voltage_v"] == 400.0
    assert (finding["layer"], finding["layer_type"], finding["column"]) == (
        "Top Layer", "outer", "B2")
    assert finding["band"] == "301-500 V" and finding["confidence"] == "high"
    assert finding["gap_mil"] == pytest.approx(40.0)
    assert finding["required_mil"] == pytest.approx(2.5 * MM, abs=1e-3)
    assert finding["at"]["y_mil"] == pytest.approx(30.0) and finding["objects"] == ["track"] * 2
    assert finding["voltage"] == "GND 0 V (ground) to HV_BUS 400 V (hv)"
    assert "1.02 mm apart on Top Layer" in finding["issue"]
    row = _pair(result, "GND", "HV_BUS")
    assert [(item["layer"], item["status"]) for item in row["layers"]] == [
        ("Top Layer", "below"), ("L3-SIG", "ok")]
    inner = row["layers"][1]
    assert inner["gap_mil"] == pytest.approx(15.0)
    assert inner["required_mil"] == pytest.approx(0.25 * MM, abs=1e-3)  # B1
    assert row["closest"]["layer"] == "Top Layer" and row["worst_ratio"] == pytest.approx(
        40 / (2.5 * MM), abs=1e-3)
    summary = result["summary"]
    assert summary["pairs"]["measured"] == 1 and summary["pairs"]["below_standard"] == 1
    assert summary["by_layer_type"]["outer"] == {"pair_layers": 1, "below": 1,
                                                 "min_gap_mil": 40.0}
    assert summary["by_layer_type"]["inner"]["below"] == 0
    assert summary["net_voltages"]["known_by_source"] == {"hv": 1, "ground": 1, "name": 1}
    assert summary["hv_nets"][0] == {"net": "HV_BUS", "voltage": "400 V", "source": "hv",
                                     "confidence": "high"}
    assert any("coating=true" in note for note in result["notes"])


def _arc_chord(net):
    # The arc of radius 100 about (500, 140) from 225 to 315 degrees: its lowest point is
    # at y = 40 (copper from 35 with width 10), its chord at y = 69.3.
    offset = 100 / math.sqrt(2)
    return Primitive(kind="arc", layer=1, net=net, component=None, x1=500 - offset,
                     y1=140 - offset, x2=500 + offset, y2=140 - offset, width=10.0,
                     source_id=5, arc_x=500.0, arc_y=140.0, arc_radius=100.0)


OBJECTS = {
    "track": (lambda: {"primitives": [_track(GND, 1, 0, 60, 1000, 60, width=20)]}, 40.0,
              "track"),
    "arc": (lambda: {"primitives": [_arc_chord(GND)]}, 25.0, "arc"),
    "pad": (lambda: {"primitives": [_pad(GND, 0, 500, 60, width=20, height=20, shape=2)],
                     "components": [_part(0)]}, 40.0, "pad"),
    "via": (lambda: {"primitives": [_via(GND, 500, 55)]}, 30.0, "via"),
    "region": (lambda: {"areas": [CopperArea("region", 1, GND, None, _square(400, 50, 200))]},
               40.0, "region"),
    "fill": (lambda: {"areas": [CopperArea("fill", 1, GND, None, _square(400, 50, 200))]},
             40.0, "fill"),
    "poured region": (lambda: {
        "areas": [CopperArea("region", 1, GND, None, _square(400, 50, 200), pour_id=0)],
        "pours": [PolygonPour(0, GND, 1, "Solid", _square(390, 45, 220))]}, 40.0, "pour"),
    "hatched pour": (lambda: {
        "primitives": [Primitive(kind="track", layer=1, net=GND, component=None, x1=400,
                                 y1=60, x2=600, y2=60, width=20.0, poured_by=0)],
        "pours": [PolygonPour(0, GND, 1, "Hatched", _square(390, 45, 220))]}, 40.0, "pour"),
}


@pytest.mark.parametrize("name", sorted(OBJECTS))
def test_distance_per_object_type(name):
    build, gap, kind = OBJECTS[name]
    fields = build()
    fields["primitives"] = [_hv_track(1), *fields.get("primitives", [])]
    result = _run(_pcb(**fields), hv_text="HV_BUS=400")
    row = _pair(result, "GND", "HV_BUS")
    top = next(item for item in row["layers"] if item["layer"] == "Top Layer")
    assert top["gap_mil"] == pytest.approx(gap, abs=1e-3)
    assert sorted(row["closest"]["objects"]) == sorted([kind, "track"])
    if name == "pad":
        assert row["closest"]["parts"] == ["U1"]


def test_an_arcs_gap_is_located_on_the_true_arc_not_its_chord():
    index = _pcb([_hv_track(1), _arc_chord(GND)])
    (record,) = geometry.net_gaps(index, {HVB: 100.0}).records
    start, end = record.ends  # the arc's lowest copper (y 35), the track's edge (y 10)
    assert start == pytest.approx((500.0, 35.0)) and end == pytest.approx((500.0, 10.0))
    (finding,) = _findings(_run(index, hv_text="HV_BUS=400"), "spacing_below_standard")
    assert finding["gap_mil"] == pytest.approx(25.0)
    assert finding["at"] == {"x_mil": pytest.approx(500.0), "y_mil": pytest.approx(22.5)}


def test_closest_points_of_arc_pieces():
    arc = (0.0, 0.0, 10.0, 10.0, 0.0, 0.0, 10.0)  # a quarter circle, (10, 0) to (0, 10)
    root = 10 / math.sqrt(2)
    # A segment beyond the arc's middle: the foot of the perpendicular from the centre.
    distance, on_arc, on_segment = geometry._closest_pieces(arc, (20.0, 0.0, 0.0, 20.0))
    assert distance == pytest.approx(20 / math.sqrt(2) - 10)
    assert on_arc == pytest.approx((root, root)) and on_segment == pytest.approx((10, 10))
    # A segment crossing the arc: they touch where it crosses.
    distance, on_arc, _ = geometry._closest_pieces(arc, (0.0, 0.0, 20.0, 20.0))
    assert distance == 0.0 and on_arc == pytest.approx((root, root))
    # Two arcs facing each other along the line through their centres.
    other = (30.0, 0.0, 10.0, 20.0, 0.0, 30.0, -10.0)
    distance, on_first, on_second = geometry._closest_pieces(arc, other)
    assert distance == pytest.approx(10.0)
    assert on_first == pytest.approx((10.0, 0.0)) and on_second == pytest.approx((20.0, 0.0))
    # The order of the pieces only swaps the points.
    distance, on_segment, on_arc = geometry._closest_pieces((20.0, 0.0, 0.0, 20.0), arc)
    assert on_arc == pytest.approx((root, root))


def test_a_via_counts_on_every_layer_it_spans():
    index = _pcb([_hv_track(1), _hv_track(3), _via(GND, 500, 55)])
    row = _pair(_run(index, hv_text="HV_BUS=400"), "GND", "HV_BUS")
    assert {item["layer"]: item["gap_mil"] for item in row["layers"]} == {
        "Top Layer": pytest.approx(30.0), "L3-SIG": pytest.approx(30.0)}


def test_shelved_and_unpoured_pours_and_outline_rows_hold_no_copper():
    index = _pcb(
        [_hv_track(1),
         # The outline row of the unpoured pour: the drawn boundary, not copper.
         Primitive(kind="track", layer=1, net=GND, component=None, x1=0, y1=14, x2=500,
                   y2=14, width=0.0, pour_id=1)],
        pours=[PolygonPour(0, GND, 1, "Solid", _square(0, 12, 300), shelved=True),
               PolygonPour(1, GND, 1, "Solid", _square(0, 14, 300))],
    )
    result = _run(index, hv_text="HV_BUS=400")
    assert result["pairs"] == [] and not _findings(result, "spacing_below_standard")
    assert any(note.startswith("1 shelved and 1 unpoured pour(s)") for note in result["notes"])


def test_copper_inside_another_nets_region_touches():
    index = _pcb([_track(HVB, 1, 100, 0, 900, 0, width=20)],
                 areas=[CopperArea("region", 1, GND, None, [(0, -100), (1000, -100),
                                                            (1000, 100), (0, 100)])])
    (finding,) = _findings(_run(index, hv_text="HV_BUS=400"), "spacing_below_standard")
    assert finding["gap_mil"] == 0.0 and finding["marks"] == ["touching"]
    assert "the copper touches" in finding["issue"]


def test_no_net_copper_and_net_ties_are_counted_not_judged():
    tie = _part(0, kind=3)  # a net tie joining HV_BUS and GND
    index = _pcb([
        _hv_track(1), _track(None, 1, 0, 30, 1000, 30, width=10),
        _pad(HVB, 0, 1500, 0, width=20, height=20, shape=2),
        _pad(GND, 0, 1520, 0, width=20, height=20, shape=2),
    ], components=[tie])
    result = _run(index, hv_text="HV_BUS=400")
    assert result["summary"]["geometry"]["no_net_copper_nearby"] == 1
    assert result["summary"]["geometry"]["net_tie_pairs_skipped"] == 1
    assert result["pairs"] == []


# ---------- voltages ----------

def test_ac_is_judged_at_its_peak_and_phases_count():
    index = _pcb([_track(LL, 1, 0, 0, 1000, 0, width=20),
                  _track(LN, 1, 0, 60, 1000, 60, width=20)])
    result = _run(index, hv_text="LINE_L=230AC,LINE_N=0")
    (finding,) = _findings(result, "spacing_below_standard")
    assert finding["voltage_v"] == pytest.approx(325.27, abs=0.01)
    assert finding["band"] == "301-500 V"
    assert finding["voltage"] == ("LINE_L 230 V AC (325.27 V peak) (hv) to LINE_N 0 V (hv)")
    assert result["summary"]["net_voltages"]["ac_nets"] == 1
    phases = _run(index, hv_text="LINE_L=230AC,LINE_N=230AC@120")
    (finding,) = _findings(phases, "spacing_below_standard")
    assert finding["voltage_v"] == pytest.approx(563.38, abs=0.01)
    assert finding["required_mil"] == pytest.approx((2.5 + 63.38 * 0.005) * MM, abs=0.01)
    assert finding["band"].startswith(">500 V")
    same = _run(index, hv_text="LINE_L,LINE_N=230AC")  # one phase: no voltage between
    assert same["pairs"] == []


def test_groups_take_the_working_voltage_across_them():
    index = _pcb([_hv_track(1), _track(AUX, 1, 0, 60, 1000, 60, width=20),
                  _track(GND, 1, 0, -60, 1000, -60, width=20)])
    result = _run(index, hv_text="HV_BUS=400", groups=hv.parse_groups("PRI:HV_BUS,LINE_*")[0],
                  working_voltage=hv.parse_working_voltage("250AC")[0])
    findings = {tuple(f["nets"]): f for f in _findings(result, "spacing_below_standard")}
    across = findings[("HV_BUS", "AUX")] if ("HV_BUS", "AUX") in findings else \
        findings[("AUX", "HV_BUS")]
    assert across["voltage_v"] == pytest.approx(353.55, abs=0.01)
    assert across["voltage"] == ("across groups PRI / board: the working voltage, "
                                 "353.55 V peak")
    # GND is in the board's group too: across the barrier, at the working voltage.
    assert set(f["nets"][0] for f in findings.values()) | set(
        f["nets"][1] for f in findings.values()) == {"HV_BUS", "AUX", "GND"}
    groups = result["summary"]["groups"]
    assert groups["declared"] == ["PRI"] and groups["members"]["PRI"] == 3
    assert not any(note.startswith("groups names no PCB net") for note in result["notes"])
    # Without a working voltage the pairs across groups are listed, not judged.
    loose = _run(index, hv_text="HV_BUS=400", groups=hv.parse_groups("PRI:HV_BUS")[0])
    assert not _findings(loose, "spacing_below_standard")
    (unknown,) = _findings(loose, "unknown_voltage_pairs")
    assert {row["reason"] for row in unknown["pairs"]} == {
        "across groups without working_voltage"}
    assert {tuple(sorted(row["nets"])) for row in unknown["pairs"]} == {
        ("AUX", "HV_BUS"), ("GND", "HV_BUS")}
    assert any(note.startswith("Pairs across the declared groups are not judged")
               for note in loose["notes"])
    assert loose["summary"]["groups"]["pairs_across"] == len(NETS) - 1
    assert loose["summary"]["groups"]["pairs_across_judged"] is False
    assert result["summary"]["groups"]["pairs_across_judged"] is True


def test_groups_without_a_working_voltage_say_so_when_nothing_is_high():
    index = _pcb([_track(P24, 1, 0, 0, 1000, 0, width=20),
                  _track(AUX, 1, 0, 30, 1000, 30, width=20)])
    result = _run(index, groups=hv.parse_groups("PRI:+24V,GND")[0], threshold=30.0)
    assert result["pairs"] == [] and result["summary"]["pairs"]["above_threshold"] == 0
    assert any(note.startswith("Pairs across the declared groups are not judged without "
                               "working_voltage=") for note in result["notes"])
    assert result["summary"]["groups"]["pairs_across"] == 2 * (len(NETS) - 2)


def test_switch_node_swings_from_zero_to_its_input():
    parts = [*PARTS, buck("U2", vin="+48V", sw="SW1", fb="FB1"),
             ("L1", {"1": ("1", "SW1"), "2": ("2", "+5V")}, {"description": "Inductor 10uH"}),
             res("R5", "+5V", "FB1", "100k"), res("R6", "FB1", "GND", "22k")]
    index = _pcb([_track(SW, 1, 0, 0, 1000, 0, width=20),
                  _track(GND, 1, 0, 30, 1000, 30, width=20)])  # 10 mil gap
    result = _run(index, parts=parts)
    assert result["summary"]["net_voltages"]["switch_nodes"] == 1
    (finding,) = _findings(result, "spacing_below_standard")
    assert finding["voltage_v"] == 48.0 and finding["band"] == "31-50 V"
    assert finding["required_mil"] == pytest.approx(0.6 * MM, abs=1e-3)
    assert "0 to 48 V (switch node)" in finding["voltage"]
    assert finding["confidence"] == "low"


def test_a_switch_node_given_in_rails_or_hv_keeps_its_swing():
    parts = [*PARTS, buck("U2", vin="+48V", sw="SW1", fb="FB1"),
             ("L1", {"1": ("1", "SW1"), "2": ("2", "+5V")}, {"description": "Inductor 10uH"}),
             res("R5", "+5V", "FB1", "100k"), res("R6", "FB1", "GND", "22k")]
    index = _pcb([_track(SW, 1, 0, 0, 1000, 0, width=20),
                  _track(P48, 1, 0, 30, 1000, 30, width=20)])  # 10 mil from +48V
    for kwargs in ({}, {"rails": {"SW1": 48.0}}, {"hv_text": "SW1=48"}):
        result = _run(index, parts=parts, **kwargs)
        # 0 to 48 V against 48 V DC: 48 V when the switch is low.
        (row,) = result["pairs"]
        assert row["voltage_v"] == 48.0 and "0 to 48 V (switch node)" in row["voltage"]
        assert result["summary"]["net_voltages"]["switch_nodes"] == 1
        assert any("switch node(s) given" in note for note in result["notes"]) == bool(kwargs)
    # An AC level of its own is kept: in phase with nothing else, it is plain AC.
    own = _run(index, parts=parts, hv_text="SW1=60pk")
    assert own["summary"]["net_voltages"]["switch_nodes"] == 0
    assert hv.Level(-48.0).swinging().text() == "0 to -48 V (switch node)"


def test_a_switch_node_of_unknown_voltage_is_asked_about():
    parts = [*PARTS, buck("U2", vin="AUX", sw="SW1", fb="FB1")]
    index = _pcb([_track(SW, 1, 0, 0, 1000, 0, width=20)])
    (unknown,) = _findings(_run(index, parts=parts), "unknown_voltage_pairs")
    why = {row["net"]: row["why"] for row in unknown["hv_looking_nets"]}
    assert "the switch node of U2 (buck)" in why["SW1"]


def test_threshold():
    index = _pcb([_track(P24, 1, 0, 0, 1000, 0, width=20),
                  _track(GND, 1, 0, 22, 1000, 22, width=20)])  # 2 mil gap
    (finding,) = _findings(_run(index), "spacing_below_standard")
    assert finding["voltage"] == "GND 0 V (ground) to +24V 24 V (name)"
    assert finding["band"] == "16-30 V" and finding["required_mil"] == pytest.approx(
        0.1 * MM, abs=1e-3)
    quiet = _run(index, threshold=30.0)
    assert quiet["pairs"] == [] and not _findings(quiet, "spacing_below_standard")


def test_min_distance_overrides_the_standard():
    index = _pcb([_hv_track(1), _track(GND, 1, 0, 60, 1000, 60, width=20)])
    every = hv.parse_min_distance("3", MM)[0]
    (finding,) = _findings(_run(index, hv_text="HV_BUS=400", min_distance=every),
                           "spacing_below_standard")
    assert finding["required_mil"] == pytest.approx(3 * MM, abs=1e-3)
    assert finding["standard"] == "min_distance (user)" and "column" not in finding
    assert finding["standard_required_mil"] == pytest.approx(2.5 * MM, abs=1e-3)
    net = hv.parse_min_distance("HV_BUS=4", MM)[0]
    (finding,) = _findings(_run(index, hv_text="HV_BUS=400", min_distance=net),
                           "spacing_below_standard")
    assert finding["required_mil"] == pytest.approx(4 * MM, abs=1e-3)
    pair = hv.parse_min_distance("HV_BUS=4,GND&HV_BUS=0.9,X&Y=1", MM)[0]
    result = _run(index, hv_text="HV_BUS=400", min_distance=pair)
    assert not _findings(result, "spacing_below_standard")  # 1.02 mm > 0.9 mm
    assert _pair(result, "GND", "HV_BUS")["layers"][0]["required_mil"] == pytest.approx(
        0.9 * MM, abs=1e-3)
    assert any("X&Y" in note for note in result["notes"])


def test_a_net_figure_reaches_pairs_searched_from_the_other_side():
    # +24V is searched (GND is not); GND's copper is 2 mm away, past the standard's reach.
    gap = 2.0 * MM
    index = _pcb([_track(P24, 1, 0, 0, 1000, 0, width=20),
                  _track(GND, 1, 0, 20 + gap, 1000, 20 + gap, width=20)])
    assert _run(index)["pairs"] == []
    for text in ("GND=4", "+24V=4", "+24V&GND=4"):
        result = _run(index, min_distance=hv.parse_min_distance(text, MM)[0])
        (finding,) = _findings(result, "spacing_below_standard")
        assert finding["gap_mil"] == pytest.approx(gap, abs=1e-3), text
        assert finding["required_mil"] == pytest.approx(4 * MM, abs=1e-3), text


def test_unknown_pairs_take_the_users_figure():
    index = _pcb([_hv_track(1), _track(SENSE, 1, 0, 60, 1000, 60, width=20)])
    (unknown,) = _findings(_run(index, hv_text="HV_BUS=400"), "unknown_voltage_pairs")
    (row,) = unknown["pairs"]
    assert row["if_at_reference_required_mil"] == pytest.approx(2.5 * MM, abs=1e-3)
    assert "required_by" not in row
    for text in ("HV_BUS=4", "HV_BUS&SENSE=4", "4"):
        result = _run(index, hv_text="HV_BUS=400",
                      min_distance=hv.parse_min_distance(text, MM)[0])
        (row,) = _findings(result, "unknown_voltage_pairs")[0]["pairs"]
        assert row["if_at_reference_required_mil"] == pytest.approx(4 * MM, abs=1e-3), text
        assert row["required_by"] == "min_distance (user)"
    # And the search reaches as far as the figure: SENSE 7 mm away, 6 mm asked.
    far = 20 + 7 * MM
    index = _pcb([_hv_track(1), _track(SENSE, 1, 0, far, 1000, far, width=20)])
    assert _run(index, hv_text="HV_BUS=400")["summary"]["pairs"]["unknown_voltage_nearby"] == 0
    asked = _run(index, hv_text="HV_BUS=400", min_distance=hv.parse_min_distance(
        "HV_BUS=6", MM)[0])
    assert asked["summary"]["pairs"]["unknown_voltage_nearby"] == 1


@pytest.mark.parametrize("options, column, required_mm", [
    (hv.Options(coating=True), "B4", 0.8),
    (hv.Options(altitude_m=4000), "B3", 12.5),
    (hv.Options("ipc2221_assembly"), "A6", 1.5),
    (hv.Options("ipc2221_assembly", coating=True), "A7", 0.8),
    (hv.Options("ipc9592"), None, 2.6),
])
def test_coating_altitude_assembly_and_ipc9592(options, column, required_mm):
    index = _pcb([_hv_track(1), _pad(GND, 0, 500, 60, width=20, height=20, shape=2)],
                 components=[_part(0)])
    result = _run(index, hv_text="HV_BUS=400", options=options)
    row = _pair(result, "GND", "HV_BUS")
    assert row["closest"]["column"] == column
    assert row["closest"]["required_mil"] == pytest.approx(required_mm * MM, abs=1e-3)
    assert result["assumptions"]["coating"] == options.coating


def test_assembly_columns_differ_for_pads_and_tracks():
    index = _pcb([_hv_track(1), _pad(GND, 0, 500, 60, width=20, height=20, shape=2),
                  _track(GND, 1, 0, 50, 300, 50, width=20)], components=[_part(0)])
    coated = _run(index, hv_text="HV_BUS=400",
                  options=hv.Options("ipc2221_assembly", coating=True))
    row = _pair(coated, "GND", "HV_BUS")["layers"][0]
    # The track (30 mil) against A5, the pad (40 mil) against A7: both 0.8 mm at 400 V.
    assert row["gap_mil"] == pytest.approx(30.0)
    uncoated = _run(index, hv_text="HV_BUS=400", options=hv.Options("ipc2221_assembly"))
    row = _pair(uncoated, "GND", "HV_BUS")["layers"][0]
    # The track needs B2's 2.5 mm, the pad A6's 1.5 mm: the worst is the track.
    assert row["required_mil"] == pytest.approx(2.5 * MM, abs=1e-3)
    assert "worst_gap_mil" not in row


# ---------- the board's rule ----------

def test_the_boards_clearance_rule_is_reported_beside_the_standard():
    index = _pcb([_hv_track(1), _track(GND, 1, 0, 60, 1000, 60, width=20)],
                 rules=[_rule(GENERICCLEARANCE="8mil")])
    result = _run(index, hv_text="HV_BUS=400")
    row = _pair(result, "GND", "HV_BUS")
    assert row["board_rule"] == {"rule": "Clearance", "clearance_mil": 8.0}
    (below,) = _findings(result, "spacing_below_rule")
    assert below["severity"] == "info" and below["rule_clearance_mil"] == 8.0
    assert below["required_mil"] == pytest.approx(2.5 * MM, abs=1e-3)
    (warning,) = _findings(result, "spacing_below_standard")
    assert warning["board_rule"] == {"rule": "Clearance", "allows_mil": 8.0}
    # A rule for the HV net that asks for enough: no info.
    strict = _pcb([_hv_track(1), _track(GND, 1, 0, 60, 1000, 60, width=20)],
                  rules=[_rule(GENERICCLEARANCE="8mil", priority=2),
                         _rule("HV", 1, "InNet('HV_BUS')", "All", GENERICCLEARANCE="120mil")])
    result = _run(strict, hv_text="HV_BUS=400")
    assert _pair(result, "GND", "HV_BUS")["board_rule"]["rule"] == "HV"
    assert not _findings(result, "spacing_below_rule")


def test_one_info_per_board_rule_lists_the_pairs_it_lets_too_close():
    index = _pcb([_hv_track(1), _track(GND, 1, 0, 60, 1000, 60, width=20),
                  _track(SENSE, 1, 0, -60, 1000, -60, width=20),
                  _track(P24, 3, 0, 0, 1000, 0, width=20),
                  _track(AUX, 3, 0, 22, 1000, 22, width=20)],
                 rules=[_rule(GENERICCLEARANCE="8mil")])
    result = _run(index, hv_text="HV_BUS=400,SENSE=0,AUX=0")
    (below,) = _findings(result, "spacing_below_rule")
    assert below["pairs_total"] == 2 and below["rule"] == "Clearance"
    assert [item["nets"] for item in below["pairs"]] == [["GND", "HV_BUS"],
                                                         ["HV_BUS", "SENSE"]]
    assert below["required_mil"] == pytest.approx(2.5 * MM, abs=1e-3)
    assert "2 pair(s)" in below["issue"] and "GND and HV_BUS at 400 V peak" in below["issue"]
    # +24V / AUX (24 V, 0.1 mm needed) is above the rule's 8 mil: not listed.
    assert len(result["pairs"]) == 3
    # A user figure counts as what the pair needs.
    figure = _run(index, hv_text="HV_BUS=400,SENSE=0,AUX=0",
                  min_distance=hv.parse_min_distance("+24V=1", MM)[0])
    (below,) = _findings(figure, "spacing_below_rule")
    assert below["pairs_total"] == 3
    assert {"nets": ["+24V", "AUX"], "voltage_v": 24.0, "required_mil": pytest.approx(
        MM, abs=1e-3), "standard": "min_distance (user)"} in below["pairs"]


def test_the_rules_object_matrix_counts_copper_to_copper_cells_only():
    # Copper to hole (0) is no copper spacing; track to via (5 mil) is.
    matrix = ("ClearanceObj_Track-ClearanceObj_Hole:0;"
              "ClearanceObj_Track-ClearanceObj_Via:50000")
    index = _pcb([_hv_track(1), _track(GND, 1, 0, 60, 1000, 60, width=20)],
                 rules=[_rule(GENERICCLEARANCE="10mil", OBJECTCLEARANCES=matrix)])
    result = _run(index, hv_text="HV_BUS=400")
    assert _pair(result, "GND", "HV_BUS")["board_rule"] == {
        "rule": "Clearance", "clearance_mil": 10.0, "smallest_mil": 5.0}
    assert _findings(result, "spacing_below_rule")[0]["rule_clearance_mil"] == 5.0


def test_a_rule_that_depends_on_the_objects_lists_its_candidates():
    index = _pcb([_hv_track(1), _track(GND, 1, 0, 60, 1000, 60, width=20)],
                 rules=[_rule("Vias", 1, "IsVia", "All", GENERICCLEARANCE="4mil"),
                        _rule(priority=2, GENERICCLEARANCE="20mil")])
    result = _run(index, hv_text="HV_BUS=400")
    rule = _pair(result, "GND", "HV_BUS")["board_rule"]
    assert rule["rule"] is None
    assert [(item["rule"], item["clearance_mil"]) for item in rule["candidates"]] == [
        ("Vias", 4.0), ("Clearance", 20.0)]
    assert rule["candidates"][1]["otherwise"] is True
    (below,) = _findings(result, "spacing_below_rule")
    assert below["rule_clearance_mil"] == 4.0 and "depending on the objects" in below["issue"]
    assert below["depends_on_objects"] is True
    (warning,) = _findings(result, "spacing_below_standard")
    assert warning["board_rule"] == {"rule": "Vias, Clearance", "allows_mil": 4.0,
                                     "depends_on_objects": True}


# ---------- unknown voltages ----------

def test_unknown_voltages_are_listed_not_judged():
    index = _pcb([
        _hv_track(1), _track(GND, 1, 0, 60, 1000, 60, width=20),
        _track(P24, 32, 0, 0, 1000, 0, width=20),
        _track(SENSE, 32, 0, 22, 1000, 22, width=20),  # 2 mil from +24V, outer
        _track(LL, 3, 0, 0, 100, 0, width=20), _track(LN, 3, 0, 30, 100, 30, width=20),
    ])
    result = _run(index)
    # HV_BUS has no voltage: its pair with GND is not judged.
    assert not [f for f in _findings(result, "spacing_below_standard") if "HV_BUS" in f["nets"]]
    (unknown,) = _findings(result, "unknown_voltage_pairs")
    why = {row["net"]: row["why"] for row in unknown["hv_looking_nets"]}
    assert why["HV_BUS"][0].startswith("its name holds HV")
    assert "a pin of mains connector J3" in why["LINE_L"] and "LINE_N" in why
    assert {"HV_BUS", "LINE_L", "LINE_N"} <= set(result["summary"]["unknown_hv_nets"])
    assert len(why["LINE_L"]) == 2  # its name and the mains connector
    (row,) = unknown["pairs"]
    assert row["nets"] == ["SENSE", "+24V"] and row["unknown"] == ["SENSE"]
    assert row["known"] == ["+24V 24 V (name)"] and row["if_at_reference_v"] == 24.0
    assert row["if_at_reference_required_mil"] == pytest.approx(0.1 * MM, abs=1e-3)
    assert row["below_if_at_reference"] is True and row["gap_mil"] == pytest.approx(2.0)
    assert any(note.startswith("No hv= or rails= given") for note in result["notes"])
    # Given, they are judged.
    given = _run(index, hv_text="HV_BUS=400,SENSE=5")
    assert not [f for f in _findings(given, "unknown_voltage_pairs") if f.get("pairs")]
    assert {tuple(f["nets"]) for f in _findings(given, "spacing_below_standard")} == {
        ("GND", "HV_BUS"), ("SENSE", "+24V")}


def test_rails_win_over_names_and_hv_over_rails():
    index = _pcb([_track(P24, 1, 0, 0, 1000, 0, width=20),
                  _track(GND, 1, 0, 22, 1000, 22, width=20)])
    rails = _run(index, rails={"+24V": 60.0})
    assert "+24V 60 V (user)" in _findings(rails, "spacing_below_standard")[0]["voltage"]
    both = _run(index, rails={"+24V": 60.0}, hv_text="+24V=100")
    assert _findings(both, "spacing_below_standard")[0]["voltage_v"] == 100.0
    noted = _run(index, rails={"NOPE": 5.0}, hv_text="NOPE2=5")
    assert any("rails names no net" in note for note in noted["notes"])
    assert any("hv names no net" in note for note in noted["notes"])


# ---------- creepage notes ----------

def test_slots_and_holes_in_the_path_are_listed():
    slot = BareHole(x=500, y=30, hole=10, layer=74, net=None, component=None, pad_name="",
                    hole_shape=2, slot_length=60, plated=False)
    hole = BareHole(x=500, y=85, hole=10, layer=74, net=None, component=None, pad_name="")
    index = _pcb([_hv_track(1), _pad(GND, 0, 500, 60, width=20, height=20, shape=2)],
                 components=[_part(0)], bare_holes=[slot, hole])
    result = _run(index, hv_text="HV_BUS=400")
    (finding,) = _findings(result, "spacing_below_standard")
    kinds = {(item["kind"], item["in_path"]) for item in finding["openings"]}
    assert kinds == {("slot", True), ("hole", False)}
    assert "lies in the path" in finding["issue"]
    (note,) = _findings(result, "creepage_not_computed")
    assert note["openings_total"] == 1 and note["openings"][0]["nets"] == ["GND", "HV_BUS"]


def test_openings_near_and_gap_ends():
    index = _pcb([_hv_track(1), _track(GND, 1, 0, 60, 1000, 60, width=20)],
                 bare_holes=[BareHole(x=300, y=30, hole=10, layer=74, net=None, component=None,
                                      pad_name="")])
    record = geometry.net_gaps(index, {HVB: 50.0}).records[0]
    start, end = record.ends  # GND's copper first (the lower net id), then HV_BUS's
    assert (start[1], end[1]) == (pytest.approx(50.0), pytest.approx(10.0))
    assert start[0] == end[0]
    found = geometry.openings_near(index, (300, 10), (300, 50), 20.0)
    assert [(item["kind"], item["in_path"]) for item in found] == [("hole", True)]
    assert geometry.openings_near(index, (100, 10), (100, 50), 20.0) == []


# ---------- the engine ----------

def test_net_gaps_reach_sides_and_layers():
    index = _pcb([_hv_track(1), _track(GND, 1, 0, 60, 1000, 60, width=20),
                  _track(P24, 3, 0, 0, 1000, 0, width=20),
                  _track(GND, 3, 0, 40, 1000, 40, width=20)])
    assert geometry.net_gaps(index, {HVB: 30.0}).records == []
    (record,) = geometry.net_gaps(index, {HVB: 50.0}).records
    assert (record.first.net, record.second.net, record.layer) == (GND, HVB, 1)
    both = geometry.net_gaps(index, {HVB: 50.0, GND: 50.0, P24: 50.0})
    assert sorted((r.first.net, r.second.net, r.layer) for r in both.records) == [
        (GND, HVB, 1), (GND, P24, 3)]  # each pair once, though both sides searched
    only = geometry.net_gaps(index, {HVB: 50.0, P24: 50.0}, layers={3})
    assert [(r.first.net, r.second.net) for r in only.records] == [(GND, P24)]
    skip = geometry.net_gaps(index, {HVB: 50.0, P24: 50.0},
                             pair_reach=lambda a, b: None if HVB in (a, b) else 50.0)
    assert [(r.first.net, r.second.net) for r in skip.records] == [(GND, P24)]
    assert not both.stopped and both.distance_calls > 0


def test_net_gaps_keeps_the_smallest_gap_per_pair_layer_and_pad_side():
    index = _pcb([_hv_track(1), _track(GND, 1, 0, 60, 500, 60, width=20),
                  _track(GND, 1, 500, 40, 1000, 40, width=20),
                  _pad(GND, 0, 200, 45, width=10, height=10, shape=2)],
                 components=[_part(0)])
    records = geometry.net_gaps(index, {HVB: 100.0}).records
    by_pad = {("pad" in (r.first.kind, r.second.kind)): r.gap_mil for r in records}
    assert by_pad == {False: pytest.approx(20.0), True: pytest.approx(30.0)}


def test_area_against_area_and_area_sources():
    index = _pcb(areas=[CopperArea("region", 1, HVB, None, _square(0, 0, 100)),
                        CopperArea("fill", 1, GND, None, _square(130, 0, 100)),
                        CopperArea("region", 1, SENSE, None, _square(0, 125, 50))],
                 primitives=[_track(AUX, 1, 0, -40, 100, -40, width=20)])
    records = geometry.net_gaps(index, {HVB: 60.0}).records
    gaps = {tuple(sorted((r.first.kind, r.second.kind))) + (r.second.net, ): r.gap_mil
            for r in records}
    assert gaps == {("fill", "region", HVB): pytest.approx(30.0),
                    ("region", "region", SENSE): pytest.approx(25.0),
                    ("region", "track", AUX): pytest.approx(30.0)}


# ---------- paging and filters ----------

def test_paging_filters_and_checks():
    index = _pcb([_hv_track(1), _track(GND, 1, 0, 60, 1000, 60, width=20),
                  _track(SENSE, 1, 0, -60, 1000, -60, width=20),
                  _hv_track(3), _track(GND, 3, 0, 25, 1000, 25, width=20)])
    prepared = hv.prepare_hv(_netlist(PARTS), index=index, hv=_hv("HV_BUS=400,SENSE=0"))
    first = hv.render_hv(prepared, offset=0, limit=1)
    assert len(first["findings"]) == 1 and first["has_more"] and first["next_offset"] == 1
    assert first["findings_total"] == 5 and first["pairs_total"] == 2
    assert first["findings"][0]["severity"] == "warning"
    last = hv.render_hv(prepared, offset=4, limit=1)
    assert not last["has_more"] and "next_offset" not in last
    assert first["screening"] == hv.SCREENING and "Altair" in first["sources"]
    netted = _run(index, hv_text="HV_BUS=400,SENSE=0", nets=["sense"])
    assert [row["nets"] for row in netted["pairs"]] == [["HV_BUS", "SENSE"]]
    layered = _run(index, hv_text="HV_BUS=400,SENSE=0", layer=3)
    assert [row["layer"] for row in _pair(layered, "GND", "HV_BUS")["layers"]] == ["L3-SIG"]
    narrowed = _run(index, hv_text="HV_BUS=400,SENSE=0", checks=["spacing_below_standard"])
    assert {f["check"] for f in narrowed["findings"]} == {"spacing_below_standard"}
    assert narrowed["checks_run"] == ["spacing_below_standard"]
    assert _run(index, checks=["nope"])["error"] == "invalid_checks"
    assert _run(index, nets=["NOPE"])["error"] == "net_not_found"


def test_net_narrows_the_search_too():
    index = _pcb([_hv_track(1), _track(GND, 1, 0, 60, 1000, 60, width=20),
                  _track(SENSE, 1, 0, -60, 1000, -60, width=20),
                  _track(AUX, 1, 0, 200, 1000, 200, width=20),
                  _track(P48, 3, 0, 0, 1000, 0, width=20),
                  _track(LL, 3, 0, 40, 1000, 40, width=20)])
    every = _run(index, hv_text="HV_BUS=400,SENSE=0,AUX=0,LINE_L=0,+48V=48")
    assert len(every["pairs"]) == 4
    for nets, expected in ((["SENSE"], [["HV_BUS", "SENSE"]]), (["AUX"], [["HV_BUS", "AUX"]]),
                           (["LINE_L"], [["LINE_L", "+48V"]])):
        narrowed = _run(index, hv_text="HV_BUS=400,SENSE=0,AUX=0,LINE_L=0,+48V=48", nets=nets)
        assert [row["nets"] for row in narrowed["pairs"]] == expected
        geometry_calls = narrowed["summary"]["geometry"]["distance_calls"]
        assert 0 < geometry_calls < every["summary"]["geometry"]["distance_calls"]
    assert narrowed["summary"]["pairs"]["searched_from_nets"] == 3  # each may pair with it
    # A searched net named: only its own copper is searched.
    own = _run(index, hv_text="HV_BUS=400,SENSE=0,AUX=0,LINE_L=0,+48V=48", nets=["HV_BUS"])
    assert len(own["pairs"]) == 3 and own["summary"]["pairs"]["searched_from_nets"] == 1


def test_the_prepared_result_is_kept_for_paging():
    index = _pcb([_hv_track(1), _track(GND, 1, 0, 60, 1000, 60, width=20)])
    netlist = _netlist(PARTS)
    first = hv.prepare_hv_cached(netlist, index=index, hv=_hv("HV_BUS=400"), units="mm")
    assert hv.prepare_hv_cached(netlist, index=index, hv=_hv("HV_BUS=400"),
                                units="mm") is first
    other = hv.prepare_hv_cached(netlist, index=index, hv=_hv("HV_BUS=300"), units="mm")
    assert other is not first and other["pairs"][0]["voltage_v"] == 300.0
    # Another netlist (refreshed) is never served the old result.
    assert hv.prepare_hv_cached(_netlist(PARTS), index=index, hv=_hv("HV_BUS=400"),
                                units="mm") is not first
    for number in range(hv.CACHE_SIZE + 2):
        hv.prepare_hv_cached(netlist, index=index, threshold=float(number))
    assert len(index._hv_cache) == hv.CACHE_SIZE
    assert hv.prepare_hv(_netlist(PARTS), index=index,
                         options=hv.Options("iec"))["error"] == "invalid_standard"


# ---------- the tool ----------

@pytest.fixture
def board(monkeypatch, tmp_path: Path):
    netlist = _netlist(PARTS)
    index = _pcb([_hv_track(1), _track(GND, 1, 0, 60, 1000, 60, width=20),
                  _track(SENSE, 1, 0, -60, 1000, -60, width=20)], plane=True)
    pcb_file = tmp_path / "Board.PcbDoc"
    pcb_file.write_bytes(b"fake")
    monkeypatch.setattr(main_mod, "_project", {
        "name": "Demo", "root_dir": str(tmp_path), "prj_pcb_path": str(tmp_path / "D.PrjPcb"),
        "sheets": [], "pcb_doc_paths": [str(pcb_file)],
    })
    monkeypatch.setattr(main_mod._altium, "_netlist", netlist)
    monkeypatch.setattr(main_mod, "_variant_state",
                        VariantState([VariantDefinition(name="Default")]))
    monkeypatch.setattr(main_mod._pcb_session, "_load", lambda path, netlist: index)
    main_mod._pcb_session._index = None
    return netlist


def test_check_hv_clearance_tool(board):
    result = json.loads(main_mod.check_hv_clearance(hv="HV_BUS=400"))
    assert result["pcb_doc"] == "Board.PcbDoc" and result["units"] == "mm"
    (finding,) = [f for f in result["findings"] if f["check"] == "spacing_below_standard"]
    assert finding["gap_mm"] == pytest.approx(1.016) and finding["required_mm"] == 2.5
    assert finding["at"] == {"x_mm": 0.0, "y_mm": pytest.approx(0.762)}
    mil = json.loads(main_mod.check_hv_clearance(hv="HV_BUS=400", units="mil"))
    (finding,) = [f for f in mil["findings"] if f["check"] == "spacing_below_standard"]
    assert finding["gap_mil"] == 40.0 and "40.0 mil apart" in finding["issue"]
    coated = json.loads(main_mod.check_hv_clearance(hv="HV_BUS=400", coating=True,
                                                    min_distance="HV_BUS&SENSE=0.1"))
    assert coated["assumptions"]["columns"] == {"inner": "B1", "outer": "B4"}
    assert not [f for f in coated["findings"] if f["check"] == "spacing_below_standard"]
    high = json.loads(main_mod.check_hv_clearance(hv="HV_BUS=400", altitude=4000,
                                                  standard="ipc2221_assembly"))
    assert high["assumptions"]["columns"]["outer"] == "B3/A6"
    nine = json.loads(main_mod.check_hv_clearance(hv="HV_BUS=400", standard="ipc9592",
                                                  layer="Top Layer", net="GND"))
    assert nine["assumptions"]["standard"] == "IPC-9592B" and nine["pairs_total"] == 1
    errors = {
        "invalid_units": {"units": "inch"}, "invalid_threshold": {"threshold": -1},
        "invalid_altitude": {"altitude": 40000}, "invalid_standard": {"standard": "iec"},
        "invalid_hv": {"hv": "HV_BUS"}, "invalid_groups": {"groups": "PRI:"},
        "invalid_working_voltage": {"working_voltage": "x"},
        "invalid_min_distance": {"min_distance": "-2"}, "invalid_rails": {"rails": "A=x"},
        "invalid_vref": {"vref": "U2=20"}, "invalid_checks": {"checks": ["nope"]},
        "net_not_found": {"net": "NOPE"}, "layer_not_found": {"layer": "Nope"},
        "layer_not_signal": {"layer": "Plane1"},
    }
    for code, kwargs in errors.items():
        assert json.loads(main_mod.check_hv_clearance(**kwargs))["error"] == code, code


def test_check_hv_clearance_needs_a_board(board, monkeypatch):
    monkeypatch.setitem(main_mod._project, "pcb_doc_paths", [])
    result = json.loads(main_mod.check_hv_clearance(hv="HV_BUS=400"))
    assert result["error"] == "no_pcb_document" and "PcbDoc" in result["message"]


def test_check_hv_clearance_pages_and_shrinks(board, monkeypatch):
    first = json.loads(main_mod.check_hv_clearance(hv="HV_BUS=400,SENSE=0"))
    assert "page_limited_to" not in first and first["findings_total"] == 4
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 7000)
    small = main_mod.check_hv_clearance(hv="HV_BUS=400,SENSE=0")
    result = json.loads(small)
    assert len(small) <= 7000 and result["page_limited_to"] < 100
    assert result["next_offset"] == result["page_limited_to"]
    following = json.loads(main_mod.check_hv_clearance(hv="HV_BUS=400,SENSE=0",
                                                       offset=result["next_offset"]))
    assert following["offset"] == result["next_offset"]


def test_check_hv_clearance_needs_a_project(monkeypatch):
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.check_hv_clearance())["error"] == "no_project"
    assert "next_offset" in main_mod.check_hv_clearance.__doc__


def test_instructions_name_the_tool_after_the_cut():
    assert main_mod.SERVER_INSTRUCTIONS.index("check_hv_clearance") > 4096
    assert "check_hv_clearance" in main_mod.NETLIST_TOOLS
