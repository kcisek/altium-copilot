"""The assembly guide (services/assembly_guide and the get_assembly_guide tool): the
soldering sequence (first side, SMD before through-hole, short to tall), steps grouped by
BOM line or by footprint and Comment, what is left out (Not Fitted, fiducials, no-BOM,
mounting holes, pad-less parts, BOM and PCB out of sync), pin-1 and polarity hints on
both sides, Markdown and CSV, units, paging under the result size, and Altium's
MiniPC (skipped when absent)."""

import asyncio
import csv
import io
import json
from pathlib import Path

import pytest

import server.main as main_mod
from server.parsers.pcb_doc import parse_pcb_doc
from server.parsers.prj_pcb import VariantDefinition, VariantState
from server.services import assembly_guide as guide_mod
from server.services.bom import build_parts
from server.services.pcb_index import PcbIndex
from tests.test_dfm_assembly import EXAMPLES, _body, _index, _part, _rect, _smd, _tht
from tests.test_netlist_free_load import _load, env  # noqa: F401
from tests.test_pcb_index import pcb_data  # noqa: F401

LIMIT = 60000
MPN = "Manufacturer Part Number"


def _named_tht(component, x, y, name, size=60.0, plated=True):
    pad = _tht(component, x, y, size=size, plated=plated)
    pad.pad_name = name
    return pad


def _soic(component, cx, cy, layer=1):
    """SOIC-8 pads: 1-4 down the left (pin 1 at the top), 5-8 up the right."""
    left = [(cx - 50, cy + 60 - 40 * k) for k in range(4)]
    right = [(cx + 50, cy - 60 + 40 * k) for k in range(4)]
    return [_smd(component, x, y, 15.0, name=str(number + 1), layer=layer)
            for number, (x, y) in enumerate(left + right)]


def _board_parts():
    """Top SMD: R1 R2 (0402, 10k and '10k 1%'), R3 (0402 22k), U1 (SOIC, 60 mil body),
    D1 (K/A pads, no body), Q1 (three unnamed pads, no body), J2 (mixed: SMD pins and
    plated pegs, 120 mil body). Bottom SMD: C1 (0603), C2 (CAPAE with +/- pads, 80 mil
    body). Through-hole: J1 on top (header, 330 mil), K1 on the bottom (400 mil). Left
    out: FID1, MH1 (non-plated hole), LOGO1 (graphical), TP1 (No BOM), HS1 (no pads), and
    R4 (fitted unless a variant says so)."""
    components = [
        _part(0, "R1", 100, 100, footprint="R0402", comment="10k"),
        _part(1, "R2", 160, 100, footprint="R0402", comment="10k 1%"),
        _part(2, "R3", 220, 100, footprint="R0402", comment="22k"),
        _part(3, "U1", 500, 500, footprint="SOIC8", comment="LM358"),
        _part(4, "D1", 300, 300, footprint="SOD123", comment="BAT54"),
        _part(5, "Q1", 700, 300, footprint="SOT23", comment="BC847"),
        _part(6, "J2", 1000, 300, footprint="USB", comment="USB-C"),
        _part(7, "C1", 100, 600, side="bottom", footprint="C0603", comment="100n"),
        _part(8, "C2", 300, 600, side="bottom", footprint="CAPAE660X800", comment="47u"),
        _part(9, "J1", 800, 800, footprint="HDR1X3", comment="hdr"),
        _part(10, "K1", 1200, 800, side="bottom", footprint="RELAY", comment="G5V"),
        _part(11, "FID1", 50, 50, footprint="FID1MM"),
        _part(12, "MH1", 1400, 50),
        _part(13, "LOGO1", 1400, 400, kind=2),
        _part(14, "TP1", 1400, 600, kind=5),
        _part(15, "HS1", 500, 900, footprint="HEATSINK", comment="heatsink"),
        _part(16, "R4", 280, 100, footprint="R0402", comment="10k"),
    ]
    pads = []
    for component, x in ((0, 100), (1, 160), (2, 220), (16, 280)):
        pads += [_smd(component, x - 20, 100, 15.0, name="1"),
                 _smd(component, x + 20, 100, 15.0, name="2")]
    pads += _soic(3, 500, 500)
    pads += [_smd(4, 270, 300, 20.0, name="K"), _smd(4, 330, 300, 20.0, name="A")]
    pads += [_smd(5, 680, 280, 20.0), _smd(5, 720, 280, 20.0), _smd(5, 700, 320, 20.0)]
    pads += [_smd(6, 940 + 40 * k, 330, 15.0, name=str(k + 1)) for k in range(4)]
    pads += [_named_tht(6, 920, 280, ""), _named_tht(6, 1080, 280, "")]
    pads += [_smd(7, 80, 600, 20.0, layer=32, name="1"),
             _smd(7, 120, 600, 20.0, layer=32, name="2")]
    pads += [_smd(8, 250, 600, 40.0, layer=32, name="+"),
             _smd(8, 350, 600, 40.0, layer=32, name="-")]
    pads += [_named_tht(9, 750 + 50 * k, 800, str(k + 1)) for k in range(3)]
    pads += [_named_tht(10, x, y, str(number + 1)) for number, (x, y) in enumerate(
        [(1150, 850), (1150, 750), (1250, 750), (1250, 850)])]
    pads += [_smd(11, 50, 50, 40.0)]
    pads += [_tht(12, 1400, 50, size=120.0, hole=120.0, plated=False)]
    # Not fiducials: the logo pad is oblong, the test point netted.
    pads += [_smd(13, 1400, 400, 30.0, height=60.0), _smd(14, 1400, 600, 30.0, net=1)]
    bodies = [
        _body(3, _rect(440, 420, 560, 580), height=60.0),
        _body(6, _rect(900, 260, 1100, 360), height=120.0),
        _body(8, _rect(230, 560, 370, 640), height=80.0, projection="bottom"),
        _body(9, _rect(720, 780, 880, 820), height=330.0),
        _body(10, _rect(1100, 700, 1300, 900), height=400.0, projection="bottom"),
        _body(15, _rect(450, 850, 550, 950), height=500.0),
    ]
    return components, pads, bodies


NETLIST = {
    "nets": {}, "pin_to_net": {},
    "components": {
        "R1": {"kind": 0, "params": {"comment": "10k", MPN: "RC0402-10K"},
               "footprint": "R0402"},
        "R2": {"kind": 0, "params": {"comment": "10k 1%", MPN: "RC0402-10K"},
               "footprint": "R0402"},
        "R3": {"kind": 0, "params": {"comment": "22k", MPN: "RC0402-22K"},
               "footprint": "R0402"},
        "R4": {"kind": 0, "params": {"comment": "10k", MPN: "RC0402-10K"},
               "footprint": "R0402"},
        "U1": {"kind": 0, "params": {"comment": "LM358", MPN: "LM358DR"}, "footprint": "SOIC8"},
        "D1": {"kind": 0, "params": {"comment": "BAT54"}, "footprint": "SOD123"},
        "Q1": {"kind": 0, "params": {"comment": "BC847"}, "footprint": "SOT23"},
        "J2": {"kind": 0, "params": {"comment": "USB-C"}, "footprint": "USB"},
        "C1": {"kind": 0, "params": {"comment": "100n"}, "footprint": "C0603"},
        "C2": {"kind": 0, "params": {"comment": "47u"}, "footprint": "CAPAE660X800"},
        "J1": {"kind": 0, "params": {"comment": "hdr"}, "footprint": "HDR1X3"},
        "K1": {"kind": 0, "params": {"comment": "G5V"}, "footprint": "RELAY"},
        "MH1": {"kind": 0, "params": {}, "footprint": ""},
        "LOGO1": {"kind": 2, "params": {}, "footprint": ""},
        "TP1": {"kind": 5, "params": {}, "footprint": ""},
        "HS1": {"kind": 0, "params": {"comment": "heatsink"}, "footprint": "HEATSINK"},
        "C9": {"kind": 0, "params": {"comment": "1u"}, "footprint": "C0402"},
    },
}


def _board(netlist=None, extra=()):
    components, pads, bodies = _board_parts()
    for component, primitive in extra:
        components.append(component)
        pads += primitive
    index = _index(components, pads, bodies)
    return PcbIndex(index.pcb, netlist) if netlist is not None else index


def _variant(netlist=None, dnp=()):
    if netlist is None:
        return guide_mod.GuideVariant("Lite", frozenset(dnp).__contains__)
    names = list(netlist["components"])
    from server.services.bom import plan_variant
    plan = plan_variant(VariantDefinition(name="Lite", dnp_refdes=list(dnp)), netlist, names)
    parts, excluded = build_parts(netlist, plan)
    return guide_mod.GuideVariant(
        "Lite", plan.not_fitted.__contains__, {part.designator: part for part in parts},
        frozenset(name for names in excluded.values() for name in names))


def _steps(guide):
    return [(step.number, step.side, step.process, [row.designator for row in step.rows])
            for step in guide.steps]


# ---------- the sequence ----------


def test_smd_sides_first_then_through_hole_short_to_tall():
    guide = guide_mod.build_guide(_board(), _variant(dnp=["R4"]))
    assert _steps(guide) == [
        (1, "bottom", "smd", ["C1"]),  # chip 0603: at most 0.8 mm
        (2, "bottom", "smd", ["C2"]),  # 80 mil body
        (3, "top", "smd", ["R1"]),  # 0402 chips: R1 and R2 differ in Comment here
        (4, "top", "smd", ["R2"]),
        (5, "top", "smd", ["R3"]),
        (6, "top", "smd", ["U1"]),  # 60 mil
        (7, "top", "smd", ["J2"]),  # mixed, 120 mil
        (8, "top", "smd", ["D1"]),  # no height: smallest pad extent first
        (9, "top", "smd", ["Q1"]),
        (10, "top", "tht", ["J1"]),
        (11, "bottom", "tht", ["K1"]),
    ]
    first = guide.first
    assert (first.side, first.basis) == ("bottom", "height")
    assert first.tallest_mil == {"top": 120.0, "bottom": 80.0}
    text = guide_mod.first_side_text(first, to_mm=True)
    assert text.startswith("its tallest SMD part (2.03 mm) is shorter than the top side's "
                           "(3.05 mm)")
    heights = [(step.height_basis, step.size_code) for step in guide.steps]
    assert heights[0] == ("chip", "0603") and heights[1] == ("body", None)
    assert heights[7] == (None, None)
    # A chip without a body counts as its EIA width, not its length.
    assert guide.steps[0].height_mil == pytest.approx(0.8 * guide_mod.MM)
    assert guide.steps[2].height_mil == pytest.approx(0.5 * guide_mod.MM)
    assert guide.placements == {"top": {"smd": 6, "mixed": 1, "tht": 1},
                                "bottom": {"smd": 2, "mixed": 0, "tht": 1}}


def _two_sided(top_heights, bottom_heights):
    components, pads, bodies = [], [], []
    for side, heights in (("top", top_heights), ("bottom", bottom_heights)):
        layer = 1 if side == "top" else 32
        for height in heights:
            number = len(components)
            x = 100.0 + 200.0 * number
            components.append(_part(number, f"U{number + 1}", x, 100, side=side,
                                    footprint="QFN", comment=f"ic{number}"))
            pads += [_smd(number, x - 20, 100, layer=layer, name="1"),
                     _smd(number, x + 20, 100, layer=layer, name="2"),
                     _smd(number, x, 140, layer=layer, name="3")]
            if height is not None:
                bodies.append(_body(number, _rect(x - 40, 60, x + 40, 160), height=height,
                                    projection=side))
    return guide_mod.build_guide(_index(components, pads, bodies), guide_mod.GuideVariant("V"))


@pytest.mark.parametrize("top, bottom, side, basis", [
    ([40.0], [100.0], "top", "height"),  # the top's tallest is shorter
    ([100.0], [40.0, 30.0], "bottom", "height"),
    ([40.0, 40.0], [41.0], "bottom", "count"),  # within 0.1 mm: fewer parts first
    ([41.0], [40.0, 40.0], "top", "count"),
    ([None, None], [None], "bottom", "count"),  # no heights: the count decides
    ([40.0], [40.0], "bottom", "convention"),
    # Exactly 0.1 mm apart is still alike: the count, then the convention, decides.
    ([2 * guide_mod.SIDE_HEIGHT_TIE_MIL], [guide_mod.SIDE_HEIGHT_TIE_MIL], "bottom",
     "convention"),
    ([40.0], [], "top", "only"),
])
def test_first_side(top, bottom, side, basis):
    guide = _two_sided(top, bottom)
    assert (guide.first.side, guide.first.basis) == (side, basis)
    assert guide.steps[0].side == side
    assert guide_mod.first_side_text(guide.first, to_mm=False)


def test_first_side_counts_a_chip_without_a_body_as_its_width():
    """An 0603 chip without a body (0.8 mm wide, 1.6 mm long) against a 50 mil (1.27 mm)
    body on the bottom: the top is the flatter side."""
    components = [_part(0, "C1", 100, 100, footprint="C0603", comment="100n"),
                  _part(1, "U1", 400, 100, side="bottom", footprint="QFN", comment="ic")]
    pads = [_smd(0, 80, 100, 20.0, name="1"), _smd(0, 120, 100, 20.0, name="2"),
            *(_smd(1, x, 100, 15.0, layer=32, name=str(k + 1))
              for k, x in enumerate((370, 400, 430)))]
    bodies = [_body(1, _rect(360, 60, 440, 140), height=50.0, projection="bottom")]
    guide = guide_mod.build_guide(_index(components, pads, bodies), guide_mod.GuideVariant("V"))
    assert (guide.first.side, guide.first.basis) == ("top", "height")
    assert guide.first.tallest_mil["top"] == pytest.approx(0.8 * guide_mod.MM)


def test_side_filter_keeps_one_side():
    top = guide_mod.build_guide(_board(), _variant(), side="top")
    assert {row.side for row in top.rows} == {"top"} and len(top.rows) == 9
    assert [step.number for step in top.steps] == list(range(1, 9))
    assert "bottom" not in top.placements
    # The other side's parts are still on the board: only C9 is a BOM part missing.
    netlist = guide_mod.build_guide(_board(NETLIST), _variant(NETLIST), side="top")
    assert netlist.left_out["bom_not_on_board"] == ["C9"]
    bottom = guide_mod.build_guide(_board(), _variant(), side="bottom")
    assert _steps(bottom) == [(1, "bottom", "smd", ["C1"]), (2, "bottom", "smd", ["C2"]),
                              (3, "bottom", "tht", ["K1"])]
    assert "first_side" not in guide_mod.summary(bottom, to_mm=True)


# ---------- grouping ----------


def test_netlist_free_groups_by_footprint_and_comment():
    components = [_part(number, f"R{number + 1}", 100 + 60 * number, 100, footprint="R0402",
                        comment=comment, side=side)
                  for number, (comment, side) in enumerate(
                      [("10k", "top"), ("10K", "top"), ("10k", "bottom"), ("22k", "top")])]
    pads = []
    for number, component in enumerate(components):
        layer = 1 if component.side == "top" else 32
        pads += [_smd(number, component.x - 20, 100, layer=layer),
                 _smd(number, component.x + 20, 100, layer=layer)]
    guide = guide_mod.build_guide(_index(components, pads), guide_mod.GuideVariant("V"))
    assert guide.approximate and guide.grouping == "footprint_and_comment"
    # The bottom first (as tall, fewer parts); R3 is R1's line on the other side.
    assert _steps(guide) == [(1, "bottom", "smd", ["R3"]),
                             (2, "top", "smd", ["R1", "R2"]),  # case folded
                             (3, "top", "smd", ["R4"])]
    assert guide.first.basis == "count"


def test_netlist_groups_by_bom_line_with_mpn():
    """R1 and R2 share an MPN although their Comments differ: one step, as get_bom
    groups them; the PCB parts take their netlist names through the index."""
    guide = guide_mod.build_guide(_board(NETLIST), _variant(NETLIST, dnp=["R4"]))
    assert not guide.approximate and guide.grouping == "bom_lines"
    steps = _steps(guide)
    assert steps[2] == (3, "top", "smd", ["R1", "R2"])
    assert steps[3] == (4, "top", "smd", ["R3"])
    step = guide.steps[2]
    assert (step.mpn, step.comment, step.footprint) == ("RC0402-10K", "10k", "R0402")
    page = guide_mod.markdown(guide, 0, 300, True, "Guide")
    assert "| MPN |" in page and "RC0402-10K" in page
    assert "No netlist" not in page


def test_bom_lookup_falls_back_to_the_pcb_designator():
    """An index built without the netlist (no sch_refdes) still finds the BOM parts."""
    guide = guide_mod.build_guide(_board(), _variant(NETLIST))
    assert _steps(guide)[2] == (3, "top", "smd", ["R1", "R2", "R4"])


# ---------- left out ----------


def test_left_out_without_a_netlist():
    guide = guide_mod.build_guide(_board(), _variant(dnp=["R4", "U1", "FID1"]))
    assert guide.left_out == {
        "not_fitted": ["R4", "U1"], "fiducials": ["FID1"], "no_bom": ["LOGO1", "TP1"],
        "mounting_holes": ["MH1"], "no_pads": ["HS1"],
    }
    placed = {row.designator for row in guide.rows}
    assert not placed & {"R4", "U1", "FID1", "LOGO1", "TP1", "MH1", "HS1"}
    summary = guide_mod.summary(guide, to_mm=True)
    assert summary["left_out"]["not_fitted"] == {
        "count": 2, "why": "Not Fitted in the variant", "designators": ["R4", "U1"]}
    page = guide_mod.markdown(guide, 0, 300, True, "Guide")
    assert "## Left out" in page and "- Not Fitted in the variant (2): R4, U1" in page


def test_left_out_with_a_netlist():
    """R9 is on the board but not in the BOM; C9 is in the BOM but not on the board;
    LOGO1 and TP1 are left out by their compiled kind."""
    extra = [(_part(17, "R9", 900, 100, footprint="R0402", comment="1k"),
              [_smd(17, 880, 100, name="1"), _smd(17, 920, 100, name="2")])]
    guide = guide_mod.build_guide(_board(NETLIST, extra), _variant(NETLIST, dnp=["R4"]))
    assert guide.left_out["not_in_bom"] == ["R9"]
    assert guide.left_out["bom_not_on_board"] == ["C9"]
    assert guide.left_out["not_fitted"] == ["R4"]
    assert guide.left_out["no_bom"] == ["LOGO1", "TP1"]
    assert "R9" not in {row.designator for row in guide.rows}


@pytest.mark.parametrize("netlist", [None, NETLIST])
def test_not_fitted_parts_are_never_listed_to_fit_by_hand(netlist):
    """A Not Fitted heatsink (no pads) or mounting hole is listed as Not Fitted, not
    under the reasons that ask for it to be fitted by hand."""
    guide = guide_mod.build_guide(_board(netlist), _variant(netlist, dnp=["HS1", "MH1"]))
    assert guide.left_out["not_fitted"] == ["HS1", "MH1"]
    assert "no_pads" not in guide.left_out and "mounting_holes" not in guide.left_out
    fitted = guide_mod.build_guide(_board(netlist), _variant(netlist))
    assert fitted.left_out["no_pads"] == ["HS1"]


def test_many_left_out_are_capped():
    extra = [(_part(17 + number, f"FID{number + 2}", 10 * number, 10, footprint="FID"),
              [_smd(17 + number, 10 * number, 10, 40.0)]) for number in range(70)]
    summary = guide_mod.summary(guide_mod.build_guide(_board(None, extra), _variant()), True)
    entry = summary["left_out"]["fiducials"]
    assert entry["count"] == 71 and len(entry["designators"]) == 60 and entry["more"] == 11


# ---------- pin 1 and polarity ----------


def test_pin1_and_polarity_hints():
    guide = guide_mod.build_guide(_board(), _variant())
    hints = {row.designator: row.pin1 for row in guide.rows}
    assert hints["U1"] == "pin 1 top-left"
    assert hints["D1"] == "cathode (K) left"
    assert hints["J1"] == "pin 1 left"
    assert hints["J2"] == "pin 1 top-left"  # the pegs widen the extent below the pins
    assert hints["Q1"] == guide_mod.NO_PIN1
    # Bottom: seen with the board flipped left to right.
    assert hints["C2"] == "positive (+) right"
    assert hints["K1"] == "pin 1 top-right"
    assert hints["R1"] is None and hints["C1"] is None  # not polarized
    summary = guide_mod.summary(guide, to_mm=True)
    assert summary["polarized_parts"] == {"count": 7, "with_pin1_hint": 6}
    assert guide_mod.PIN_NUMBER_NOTE not in guide_mod.markdown(guide, 0, 300, True, "G")


def test_numbered_diodes_and_capacitors_are_flagged():
    """A diode or polarized capacitor with numbered pads gets 'pin 1 ...', which does
    not say where the cathode or + is: counted, and the note says so."""
    extra = [(_part(17, "D5", 900, 100, footprint="SOD123", comment="1N4148"),
              [_smd(17, 880, 100, name="1"), _smd(17, 920, 100, name="2")]),
             (_part(18, "C5", 900, 300, footprint="CAPT3528", comment="10u"),
              [_smd(18, 880, 300, name="1"), _smd(18, 920, 300, name="2")])]
    guide = guide_mod.build_guide(_board(None, extra), _variant())
    hints = {row.designator: (row.polarity, row.pin1) for row in guide.rows}
    assert hints["D5"] == ("diode", "pin 1 left") and hints["C5"] == ("polar", "pin 1 left")
    assert hints["U1"][0] == "pin1" and hints["D1"] == ("diode", "cathode (K) left")
    polarized = guide_mod.summary(guide, to_mm=True)["polarized_parts"]
    assert polarized == {"count": 9, "with_pin1_hint": 8,
                         "diode_or_capacitor_by_pin_number": 2,
                         "note": guide_mod.PIN_NUMBER_NOTE}
    assert f"- {guide_mod.PIN_NUMBER_NOTE}" in guide_mod.markdown(guide, 0, 300, True, "G")


def _hint(designator, names, footprint=None, comment=None, side="top"):
    layer = 1 if side == "top" else 32
    count = len(names)
    component = _part(0, designator, 0, 0, footprint=footprint, comment=comment, side=side)
    pads = [_smd(0, -50 + 100 * k / max(count - 1, 1), 0, 10.0, name=name, layer=layer)
            for k, name in enumerate(names)]
    guide = guide_mod.build_guide(_index([component], pads), guide_mod.GuideVariant("V"))
    return guide.rows[0].pin1


@pytest.mark.parametrize("designator, names, footprint, comment, hint", [
    ("D1", ["1", "2"], "SOD123", None, "pin 1 left"),
    ("LED1", ["A", "K"], "LED0603", None, "cathode (K) right"),
    ("D2", ["A", "C"], "SOD323", None, "cathode (C) right"),
    ("D3", ["x", "y"], "SOD323", None, guide_mod.NO_PIN1),
    ("C1", ["1", "2"], "CAPT3528", None, "pin 1 left"),  # tantalum by footprint
    ("C2", ["1", "2"], "C1206", "Cap Pol1", "pin 1 left"),  # Altium's polarized cap
    ("C3", ["1", "2"], "C1206", "10u", None),
    ("BT1", ["1", "2"], "CR2032", None, "pin 1 left"),
    ("J1", ["2", "1"], "HDR1X2", None, "pin 1 right"),
    ("X1", ["1", "2"], "XTAL", None, None),
    ("U1", ["A1", "A2", "A3"], "BGA", None, "pin A1 left"),
    ("U2", ["4", "1", "5"], "SOT", None, "pin 1 at the centre"),
    ("SW1", ["", "", ""], "SW", None, guide_mod.NO_PIN1),
    ("Z1", ["-", "+"], "BUZZ", None, "positive (+) right"),
])
def test_polarity_classes(designator, names, footprint, comment, hint):
    assert _hint(designator, names, footprint, comment) == hint


def test_a_depopulated_a1_is_placed_at_the_grids_corner():
    """A 3x3 ball grid without A1 (row A at the top, column 1 at the left)."""
    component = _part(0, "U1", 0, 0, footprint="BGA8")
    pads = [_smd(0, -40 + 40 * (column - 1), 40 - 40 * row, 10.0, name=f"{letter}{column}")
            for row, letter in enumerate("ABC") for column in (1, 2, 3)
            if (letter, column) != ("A", 1)]
    guide = guide_mod.build_guide(_index([component], pads), guide_mod.GuideVariant("V"))
    assert guide.rows[0].pin1 == "pin A1 top-left (no A1 pad: the grid's corner)"
    assert guide_mod._grid_a1([("a2", 0, 0), ("b1", 1, 1), ("x", 0, 0)]) is None


def test_polarity_on_the_bottom_is_mirrored():
    assert _hint("D1", ["K", "A"], "SOD123", None, side="bottom") == "cathode (K) right"


@pytest.mark.parametrize("dx, dy, bottom, text", [
    (-10, 10, False, "top-left"), (10, -10, False, "bottom-right"),
    (-10, 0, False, "left"), (0, 10, False, "top"), (1, 1, False, "centre"),
    (-10, 10, True, "top-right"), (10, 0, True, "left"),
])
def test_direction(dx, dy, bottom, text):
    assert guide_mod.direction(dx, dy, 10.0, 10.0, bottom) == text


def test_direction_of_a_row_of_pads():
    assert guide_mod.direction(-50, 0, 50.0, 0.0, False) == "left"
    assert guide_mod.direction(0, 40, 0.0, 40.0, False) == "top"


# ---------- formats, units, paging ----------


def test_markdown_checklist():
    guide = guide_mod.build_guide(_board(), _variant(dnp=["R4"]))
    page = guide_mod.markdown(guide, 0, 300, True, "Assembly guide: Demo")
    lines = page.splitlines()
    assert lines[0] == "# Assembly guide: Demo"
    assert any(line.startswith("- Bottom side SMD first: its tallest SMD part (2.03 mm)")
               for line in lines)
    assert "- No netlist: steps group parts by footprint and Comment" in page
    assert lines.count("## Bottom side, SMD") == 1 and "## Top side, through-hole" in lines
    header = next(line for line in lines if line.startswith("| Step |"))
    assert header == ("| Step | Done | Skipped | Qty | Part | Footprint | Height (mm) | "
                      "Designator | Center X, Y | Ref X, Y | Rot | Pin 1 / polarity |")
    u1 = next(line for line in lines if "| U1 |" in line)
    assert u1 == ("| 6 | [ ] | [ ] | 1 | LM358 | SOIC8 | 1.52 | U1 | 12.700, 12.700 | "
                  "12.700, 12.700 | 0 | pin 1 top-left |")
    chip = next(line for line in lines if "| C1 |" in line)
    assert "| <=0.80 (0603) |" in chip
    unknown = next(line for line in lines if "| D1 |" in line)
    assert "| ? |" in unknown
    mil = guide_mod.markdown(guide, 0, 300, False, "G")
    assert "| Height (mil) |" in mil and "| 500.0, 500.0 |" in mil


def test_markdown_escapes_and_cuts_cells():
    extra = [(_part(17, "R9", 900, 100, footprint="R|0402", comment="x" * 80),
              [_smd(17, 880, 100), _smd(17, 920, 100)])]
    page = guide_mod.markdown(guide_mod.build_guide(_board(None, extra), _variant()),
                              0, 300, True, "G")
    row = next(line for line in page.splitlines() if "| R9 |" in line)
    assert "R\\|0402" in row and "x" * 57 + "..." in row and "x" * 58 not in row


def test_csv_rows_every_column():
    index = _board(None, [(_part(17, "R9", 900, 100, footprint="R0402", comment="=SUM(A1)"),
                           [_smd(17, 880, 100), _smd(17, 920, 100)])])
    index.pcb.board.origin_x = 1000.0  # coordinates become negative
    guide = guide_mod.build_guide(index, _variant())
    text = guide_mod.to_csv(guide, 0, 300, True)
    rows = list(csv.DictReader(io.StringIO(text)))
    assert list(rows[0]) == guide_mod.csv_columns(True)
    assert len(rows) == len(guide.rows)
    r9 = next(row for row in rows if row["Designator"] == "R9")
    assert r9["Comment"] == "'=SUM(A1)"  # no formula
    assert r9["Center-X(mm)"] == "-2.5400" and r9["Ref-X(mm)"] == "-2.5400"
    u1 = next(row for row in rows if row["Designator"] == "U1")
    # C1, C2, R1 with R4, R2, R3, R9, then U1
    assert (u1["Step"], u1["Side"], u1["Process"], u1["Quantity"]) == ("7", "top", "SMD", "1")
    assert (u1["Height(mm)"], u1["Height Basis"], u1["Rotation"]) == ("1.52", "3D body", "0")
    assert u1["Pin 1 / Polarity"] == "pin 1 top-left" and u1["Done"] == ""
    c1 = next(row for row in rows if row["Designator"] == "C1")
    assert c1["Height Basis"] == "chip 0603 width (at most)"
    j2 = next(row for row in rows if row["Designator"] == "J2")
    assert (j2["Process"], j2["Mount"]) == ("SMD", "mixed")


def test_rotation_and_reference_point():
    component = _part(0, "U1", 100, 100, rotation=450.0, footprint="SOIC8")
    pads = _soic(0, 130, 100)  # the pads' centre is 30 mil from the reference point
    guide = guide_mod.build_guide(_index([component], pads), guide_mod.GuideVariant("V"))
    row = guide.rows[0]
    assert row.rotation == 90.0 and row.centre == (130.0, 100.0)
    assert row.reference == (100.0, 100.0)


def _many(count, comment="10k"):
    components, pads = [], []
    for number in range(count):
        x, y = 100.0 + 60.0 * (number % 40), 100.0 + 60.0 * (number // 40)
        components.append(_part(number, f"R{number + 1}", x, y, footprint="R0402",
                                comment=comment))
        pads += [_smd(number, x - 15, y, name="1"), _smd(number, x + 15, y, name="2")]
    return _index(components, pads)


def test_pages_continue_a_step():
    guide = guide_mod.build_guide(_many(400), guide_mod.GuideVariant("V"))
    assert len(guide.steps) == 1 and len(guide.rows) == 400
    first = guide_mod.page(guide, 0, 300, "markdown", True, "G")
    assert first["has_more"] and first["next_offset"] == 300 and first["rows_total"] == 400
    second = guide_mod.page(guide, 300, 300, "markdown", True, "G")
    assert not second["has_more"] and "next_offset" not in second
    assert not second["markdown"].startswith("# G")
    assert "| 1 (cont.) | [ ] | [ ] | 400 |" in second["markdown"]
    assert sum(1 for line in first["markdown"].splitlines() if "| [ ] | [ ] |" in line) == 300
    csv_page = guide_mod.page(guide, 300, 300, "csv", True, "G")["csv"]
    assert csv_page.count("\n") == 101  # the header row and 100 parts


# ---------- the tool ----------


def _call(**arguments) -> dict:
    return json.loads(main_mod.get_assembly_guide(**arguments))


@pytest.fixture
def board(monkeypatch, tmp_path: Path):
    """A loaded project whose PcbDoc parses to the synthetic board, no netlist; holder
    swaps the board."""
    pcb_file = tmp_path / "Demo.PcbDoc"
    pcb_file.write_bytes(b"fake")
    holder = {"index": _board()}
    session = main_mod.PcbSession()
    session._load = lambda path, netlist: holder["index"]
    monkeypatch.setattr(main_mod, "_project", {
        "name": "Demo", "root_dir": str(tmp_path), "prj_pcb_path": str(tmp_path / "Demo.PrjPcb"),
        "sheets": [], "pcb_doc_paths": [str(pcb_file)],
    })
    monkeypatch.setattr(main_mod._altium, "_netlist", None)
    monkeypatch.setattr(main_mod, "_variant_state", VariantState([
        VariantDefinition(name="Lite", dnp_refdes=["R4"]), VariantDefinition(name="Full")]))
    monkeypatch.setattr(main_mod, "_pcb_session", session)
    return holder


def _swap(holder, index) -> None:
    holder["index"] = index
    main_mod._pcb_session.invalidate()


def test_tool_netlist_free(board):
    result = _call(units="mm")
    assert result["variant"] == "Lite" and result["grouping_approximate"] is True
    assert result["pcb_doc"] == "Demo.PcbDoc" and result["units"] == "mm"
    assert result["origin_mm"] == [0.0, 0.0]
    assert result["first_side"] == "bottom" and result["steps_total"] == 11
    assert result["parts_total"] == 11 and result["rows_total"] == 11
    assert result["sequence"][0] == {"side": "bottom", "process": "smd", "steps": [1, 2],
                                     "parts": 2}
    assert result["left_out"]["not_fitted"]["designators"] == ["R4"]
    assert "netlist" in result["notes"][0]
    assert result["markdown"].startswith("# Assembly guide: Demo, variant 'Lite'\n")
    assert not result["has_more"] and "rows_limited_to" not in result
    full = _call(variant="Full")
    assert "not_fitted" not in full["left_out"] and full["parts_total"] == 12
    base = _call(variant="[No Variations]")
    assert base["variant"] == "[No Variations]" and base["parts_total"] == 12


def test_tool_netlist_free_flags_not_fitted_entries_it_cannot_bind(board, monkeypatch):
    """A Not Fitted entry stored with a '?' designator keeps only its UniqueId: without
    the schematic parts it binds to no part, and the result says so."""
    monkeypatch.setattr(main_mod, "_variant_state", VariantState([
        VariantDefinition(name="Lite", dnp_refdes=["R4"], dnp_uid=["ABCDEFGH", "IJKLMNOP"])]))
    result = _call()
    assert result["left_out"]["not_fitted"]["designators"] == ["R4"]
    assert len(result["notes"]) == 2
    assert result["notes"][1].startswith(
        "Not Fitted entries of variant 'Lite' with no designator (only a UniqueId): 2.")
    monkeypatch.setattr(main_mod, "_variant_state", VariantState([
        VariantDefinition(name="Lite", dnp_refdes=["R4"])]))
    assert len(_call()["notes"]) == 1


def test_tool_with_a_netlist(board, monkeypatch):
    monkeypatch.setattr(main_mod._altium, "_netlist", NETLIST)
    _swap(board, _board(NETLIST))
    result = _call(format="csv", units="mil")
    assert result["grouping"] == "bom_lines" and "grouping_approximate" not in result
    assert "notes" not in result and result["origin_mil"] == [0.0, 0.0]
    rows = list(csv.DictReader(io.StringIO(result["csv"])))
    r1 = next(row for row in rows if row["Designator"] == "R1")
    assert r1["Manufacturer Part Number"] == "RC0402-10K" and r1["Quantity"] == "2"
    assert result["left_out"]["bom_not_on_board"]["designators"] == ["C9"]
    assert result["left_out"]["not_fitted"]["designators"] == ["R4"]


def test_tool_side_and_errors(board, monkeypatch):
    top = _call(side="top")
    assert top["side"] == "top" and top["parts_total"] == 8 and "first_side" not in top
    assert "Demo, variant 'Lite', top side" in top["markdown"]
    assert _call(side="both")["parts_total"] == 11
    assert _call(side="left")["error"] == "invalid_side"
    assert _call(format="pdf")["error"] == "invalid_format"
    assert _call(units="inch")["error"] == "invalid_units"
    missing = _call(variant="Nope")
    assert missing["error"] == "variant_not_found" and "Lite, Full" in missing["message"]
    monkeypatch.setattr(main_mod._altium, "_netlist", NETLIST)
    assert _call(variant="Nope")["error"] == "variant_not_found"
    monkeypatch.setattr(main_mod, "_project", None)
    assert _call()["error"] == "no_project"


def test_tool_pages_fit_the_result_size(board, monkeypatch):
    """Long Comments in CSV: 300 rows do not fit, 120 do; every page fits; at a tiny
    limit the rows are left out with a hint."""
    _swap(board, _many(400, comment="c" * 200))
    first = main_mod.get_assembly_guide(variant="Full", format="csv")
    assert len(first) <= LIMIT
    result = json.loads(first)
    assert result["rows_limited_to"] == 120 and result["next_offset"] == 120
    offset, seen = 0, 0
    while True:
        text = main_mod.get_assembly_guide(variant="Full", format="csv", offset=offset)
        assert len(text) <= LIMIT
        page = json.loads(text)
        seen += page["csv"].count("\n") - 1
        if not page["has_more"]:
            break
        offset = page["next_offset"]
    assert seen == 400
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 3000)
    tiny = json.loads(main_mod.get_assembly_guide(format="csv"))
    assert "csv" not in tiny and "side set to top or bottom" in tiny["text_omitted"]


def test_tool_without_a_netlist_load(env):
    root, _, loads = env
    _load(root)
    result = _call()
    assert "error" not in result and result["grouping_approximate"] is True
    assert all(netlist is None for netlist in loads)


def test_tool_metadata_wording_manifest_readme_and_instructions():
    tools = {tool.name: tool for tool in asyncio.run(main_mod.mcp.list_tools())}
    tool = tools["get_assembly_guide"]
    assert tool.annotations.readOnlyHint is True and tool.title == "Get Assembly Guide"
    assert tool.meta["anthropic/maxResultSizeChars"] == LIMIT
    description = " ".join(tool.description.split())
    for phrase in ('"assembly guide"', '"how do I assemble / hand-solder this board?"',
                   "Done and Skipped columns", "No board map is drawn", "'no pin 1 marked'",
                   "first_side_reason and sequence_rules say why", "polarized_parts.note",
                   "known only by UniqueId"):
        assert phrase in description, phrase
    properties = tool.inputSchema["properties"]
    assert set(properties) == {"variant", "side", "format", "units", "offset"}
    instructions = main_mod.SERVER_INSTRUCTIONS
    assert instructions.index("get_assembly_guide") > 4096
    manifest = json.loads((Path(__file__).parents[1] / "manifest.json").read_text("utf-8"))
    entries = {entry["name"]: entry["description"] for entry in manifest["tools"]}
    assert "pin-1" in entries["get_assembly_guide"]
    readme = (Path(__file__).parents[1] / "README.md").read_text("utf-8")
    assert "| `get_assembly_guide` |" in readme


# ---------- Altium's examples ----------


@pytest.fixture(scope="module")
def minipc_index():
    path = next(EXAMPLES.rglob("MiniPC.PcbDoc"), None) if EXAMPLES.is_dir() else None
    if path is None:
        pytest.skip("Altium example boards not installed")
    return PcbIndex(parse_pcb_doc(str(path)), None)


def test_minipc_snapshot(board, minipc_index, monkeypatch):
    _swap(board, minipc_index)
    monkeypatch.setattr(main_mod, "_variant_state",
                        VariantState([VariantDefinition(name="Default")]))
    result = _call(units="mm")
    snapshot = {key: result[key] for key in (
        "steps_total", "parts_total", "first_side", "placements", "step_heights",
        "polarized_parts")}
    assert snapshot == MINIPC
    assert {reason: entry["count"] for reason, entry in result["left_out"].items()} == \
        MINIPC_LEFT_OUT
    offset = 0
    while True:
        text = main_mod.get_assembly_guide(units="mm", offset=offset)
        assert len(text) <= LIMIT
        page = json.loads(text)
        if not page["has_more"]:
            break
        offset = page["next_offset"]


# The bottom carries 677 SMD parts up to 3.05 mm, the top 346 up to 10.5 mm: the
# bottom goes first. Every step has a 3D body; U26's A1 ball is depopulated.
MINIPC = {
    "steps_total": 167, "parts_total": 1041, "first_side": "bottom",
    "placements": {"top": {"smd": 346, "mixed": 1, "tht": 16},
                   "bottom": {"smd": 677, "mixed": 0, "tht": 1}},
    "step_heights": {"body": 167, "chip": 0, "unknown": 0},
    "polarized_parts": {"count": 101, "with_pin1_hint": 99,
                        "diode_or_capacitor_by_pin_number": 13,
                        "note": guide_mod.PIN_NUMBER_NOTE},
}
MINIPC_LEFT_OUT = {"mounting_holes": 4, "no_pads": 1}
