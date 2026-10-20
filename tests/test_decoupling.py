"""Decoupling coverage (services/decoupling, check_decoupling): rails, gaps, distances."""

import json
from pathlib import Path

import pytest

import server.main as main_mod
from server.parsers.pcb_doc import PcbComponent, PcbData, PolygonPour, Primitive
from server.parsers.prj_pcb import VariantDefinition, VariantState
from server.services.bom import VariantPlan
from server.services.decoupling import (
    DEFAULT_MAX_DISTANCE_MIL, capacitance, pin_role, run_decoupling_check,
)
from server.services.pcb_index import PcbIndex
from tests.test_diff_pair_checks import LAYERS
from tests.test_pcb_index import _board, _pad

# (refdes, {pin: (pin name, net)}, info)
PARTS = [
    ("U1", {"1": ("VDD", "+3V3"), "2": ("VSS", "GND"), "3": ("VDDA", "VDDA"),
            "4": ("PA0", "3V3_EN")}, {}),
    ("U2", {"1": ("IN", "+5V"), "2": ("GND", "GND"), "3": ("VOUT", "+1V8")}, {}),
    ("U3", {"1": ("VDD", "+1V8"), "2": ("VREF", "VREF"), "3": ("GND", "GND"),
            "4": ("VCCSENSE", "+1V8")}, {}),
    ("C1", {"1": ("1", "+3V3"), "2": ("2", "GND")}, {"value": "100nF"}),
    ("C2", {"1": ("1", "+3V3"), "2": ("2", "GND")}, {"params": {"capacitance": "10uF"}}),
    ("C3", {"1": ("1", "+5V"), "2": ("2", "GND")}, {"value": "1uF"}),
    ("C4", {"1": ("1", "VREF"), "2": ("2", "+3V3")}, {"value": "100n"}),
    ("C5", {"1": ("1", "3V3_EN"), "2": ("2", "GND")}, {"value": "1nF"}),
    ("FB1", {"1": ("1", "+3V3"), "2": ("2", "VDDA")}, {"description": "Ferrite bead"}),
]


def _netlist(parts=PARTS):
    components, pin_to_net, nets, compiled = {}, {}, {}, {}
    for refdes, pins, info in parts:
        entry = {"pins": {}, "params": {}, "kind": 0, "description": None, "value": None}
        entry.update(info)
        if entry.get("value"):
            entry["params"] = {**entry["params"], "value": entry["value"]}
        for pin, (name, net) in pins.items():
            entry["pins"][pin] = {"name": name, "net": net}
            pin_to_net.setdefault(refdes, {})[pin] = net
            nets.setdefault(net, []).append((refdes, pin))
        components[refdes] = entry
        compiled[refdes] = {"footprint": "", "comment": ""}
    return {"nets": nets, "pin_to_net": pin_to_net, "components": components,
            "parts": compiled}


def _checks(result, check=None):
    return [f for f in result["findings"] if check is None or f["check"] == check]


def test_pin_roles_and_values():
    assert [pin_role(name) for name in ("VDD", "VDDA_1", "I/O, VREF 1", "VCCSENSE", "VCAP_1",
                                        "VOUT", "IN", "GND", "", "PA0")] == [
        "supply", "supply", "other", "other", "cap", "out", "in", "ground", "none", "other"]
    assert capacitance("100nF/16V") == pytest.approx(1e-7)
    assert capacitance(None, "4u7") == pytest.approx(4.7e-6)
    assert capacitance("CAP CER 0.1 µF 50V X7R") == pytest.approx(1e-7)
    assert capacitance("10k") is None


def test_schematic_gaps():
    plan = VariantPlan(name="Lite", not_fitted={"C3"})
    result = run_decoupling_check(_netlist(), plan, pcb_unavailable="no PcbDoc")
    by_check = {f["check"]: f for f in result["findings"]}
    # VDDA hangs off +3V3 through FB1 with no capacitor of its own.
    assert by_check["filtered_rail"]["rail"] == "VDDA" and "FB1" in by_check["filtered_rail"]["issue"]
    # +5V is decoupled only by C3, Not Fitted in this variant.
    assert by_check["not_fitted_only"]["designators"] == ["C3", "U2"]
    # +1V8 feeds U3's VDD with nothing; VCCSENSE does not count as a supply pin.
    assert by_check["no_capacitor"]["rail"] == "+1V8"
    assert by_check["no_capacitor"]["pins"] == {"U3": ["1"]}
    # VREF has only C4, which goes to +3V3.
    assert "C4 to +3V3" in by_check["capacitor_elsewhere"]["issue"]
    assert result["severity_counts"]["warning"] == 3
    assert [f["severity"] for f in result["findings"][:3]] == ["warning"] * 3
    # 3V3_EN is a signal (EN): no rail, though named like power and capacitive.
    assert "3V3_EN" not in {row["rail"] for row in result["rails"]}
    rail = next(row for row in result["rails"] if row["rail"] == "+3V3")
    assert (rail["capacitors"], rail["under_1uF"], rail["from_1uF"], rail["evidence"]) == (
        2, 1, 1, ["name", "ic_pin"])
    assert set(result["not_checked"]) == {"far_capacitor", "pads_far", "opposite_side_no_via"}


def test_small_and_bulk_capacitor_advice():
    parts = [
        ("U1", {"1": ("VDD", "+3V3"), "2": ("GND", "GND")}, {}),
        ("C1", {"1": ("1", "+3V3"), "2": ("2", "GND")}, {"value": "100nF"}),
        ("U2", {"1": ("VCC", "+5V"), "2": ("GND", "GND")}, {}),
        ("C2", {"1": ("1", "+5V"), "2": ("2", "GND")}, {"value": "22uF"}),
        ("C3", {"1": ("1", "+5V"), "2": ("2", "GND")}, {"value": "see note"}),
    ]
    result = run_decoupling_check(_netlist(parts))
    assert [f["check"] for f in result["findings"]] == ["unknown_value"]  # C3 unread
    assert "no_bulk_capacitor" not in result["checks_run"]  # only when asked
    asked = run_decoupling_check(_netlist(parts), checks=["no_bulk_capacitor",
                                                          "no_small_capacitor"])
    assert {(f["check"], f["rail"]) for f in asked["findings"]} == {
        ("no_bulk_capacitor", "+3V3")}
    parts[4] = ("C3", {"1": ("1", "+5V"), "2": ("2", "GND")}, {"value": "47uF"})
    asked = run_decoupling_check(_netlist(parts), checks=["no_small_capacitor"])
    assert [f["rail"] for f in asked["findings"]] == ["+5V"]
    assert run_decoupling_check(_netlist(parts), checks=["nope"])["error"] == "invalid_checks"


@pytest.mark.parametrize("name, role", [
    ("+VS", "supply"), ("-VS", "supply"), ("+VIN", "supply"), ("VIN-", "other"),
    ("IN+", "other"), ("-IN", "other"), ("3.3 V", "supply"), ("VDD(3V3)", "supply"),
    ("VDD_1.8V", "supply"), ("VCC3.3", "supply"), ("12", "none"), ("V", "other"),
    ("V-", "supply"), ("VREF+", "ref"), ("BST", "other"),
])
def test_pin_name_spellings(name, role):
    assert pin_role(name) == role


def test_capacitance_spellings():
    assert capacitance("0.1UF") == pytest.approx(1e-7)
    assert capacitance("100NF") == pytest.approx(1e-7)
    assert capacitance("4U7") == pytest.approx(4.7e-6)
    assert capacitance("1N4148", "10UH") is None


def _rails(parts, **kwargs):
    result = run_decoupling_check(_netlist(parts), **kwargs)
    return result, {(f["check"], f.get("rail")) for f in result["findings"]}


def test_feeders_need_capacitors_on_a_rail():
    mcu = ("U1", {"1": ("VDD", "+3V3"), "2": ("NRST", "NRST"), "3": ("GND", "GND")}, {})
    pull_up = ("R1", {"1": ("1", "+3V3"), "2": ("2", "NRST")}, {"value": "10k"})
    reset_cap = ("C2", {"1": ("1", "NRST"), "2": ("2", "GND")}, {"value": "100nF"})
    # A reset RC on the far side of a pull-up is no supply: +3V3 simply has no capacitor.
    result, found = _rails([mcu, pull_up, reset_cap])
    assert found == {("no_capacitor", "+3V3")}
    assert "filtered_to" not in result["rails"][0]
    # Ferrite with no capacitors on either side: no claim about capacitors upstream.
    parts = [("U1", {"1": ("VDD", "+3V3"), "2": ("VDDA", "VDDA"), "3": ("GND", "GND")}, {}),
             ("FB1", {"1": ("1", "+3V3"), "2": ("2", "VDDA")}, {"description": "Ferrite"})]
    _, found = _rails(parts)
    assert found == {("no_capacitor", "+3V3"), ("no_capacitor", "VDDA")}
    # A Not Fitted ferrite joins nothing.
    parts.append(("C1", {"1": ("1", "+3V3"), "2": ("2", "GND")}, {"value": "100nF"}))
    _, found = _rails(parts, plan=VariantPlan(name="V", not_fitted={"FB1"}))
    assert found == {("no_capacitor", "VDDA")}
    _, found = _rails(parts)
    assert found == {("filtered_rail", "VDDA")}


def test_a_zero_ohm_link_joins_rails():
    parts = [("U1", {"1": ("VDD", "+3V3_MCU"), "2": ("GND", "GND")}, {}),
             ("R1", {"1": ("1", "+3V3"), "2": ("2", "+3V3_MCU")}, {"value": "0R"}),
             ("C1", {"1": ("1", "+3V3"), "2": ("2", "GND")}, {"value": "100nF"})]
    result, found = _rails(parts)
    assert found == {("capacitor_elsewhere", "+3V3_MCU")}
    assert "R1 (0 ohm) to +3V3, with C1" in result["findings"][0]["issue"]
    assert result["rails"][0]["linked_to"] == ["R1 to +3V3"]


def test_not_fitted_ics_are_not_checked():
    parts = [("U5", {"1": ("VDD", "+3V3_OPT"), "2": ("GND", "GND")}, {}),
             ("C5", {"1": ("1", "+3V3_OPT"), "2": ("2", "GND")}, {"value": "100nF"})]
    plan = VariantPlan(name="Lite", not_fitted={"U5", "C5"})
    result, found = _rails(parts, plan=plan)
    assert found == set() and "1 ICs Not Fitted in variant Lite" in result["notes"][0]
    result, _ = _rails(parts, plan=plan, ics=["U5"])
    assert "U5 is Not Fitted in variant Lite: not checked." in result["notes"]


def test_rail_recognition_edge_cases():
    # A TL431 reference on a divider node, with no capacitor, is no rail.
    shunt = [("U4", {"1": ("REF", "NetR3_2"), "2": ("A", "GND"), "3": ("K", "VSHUNT")}, {}),
             ("R3", {"1": ("1", "+5V"), "2": ("2", "NetR3_2")}, {"value": "10k"})]
    assert _rails(shunt)[1] == set()
    # A VSS pin on a negative rail named like power is a supply pin.
    switch = [("U8", {"1": ("VDD", "+15V"), "2": ("VSS", "-15V"), "3": ("GND", "GND")}, {}),
              ("C1", {"1": ("1", "+15V"), "2": ("2", "GND")}, {"value": "100nF"})]
    assert _rails(switch)[1] == {("no_capacitor", "-15V")}
    # A charge pump's output on a VSS pin is a negative rail, not ground.
    pump = [("U7", {"1": ("VOUT", "NetU7_1"), "2": ("GND", "GND")}, {}),
            ("U8", {"1": ("VSS", "NetU7_1"), "2": ("VDD", "+5V")}, {}),
            ("C8", {"1": ("1", "+5V"), "2": ("2", "NetU7_1")}, {"value": "100nF"})]
    assert ("no_capacitor", "+5V") in _rails(pump)[1]
    # A supply pin on a net that carries a signal token is no rail.
    assert _rails([("U1", {"1": ("VDD", "3V3_EN"), "2": ("GND", "GND")}, {})])[1] == set()


def test_capacitor_arrays_and_feed_throughs():
    parts = [("U1", {"1": ("VDD", "+3V3"), "2": ("VDDA", "+1V8"), "3": ("GND", "GND")}, {}),
             ("CA1", {"1": ("1", "+3V3"), "2": ("2", "+1V8"), "3": ("3", "GND"),
                      "4": ("4", "GND")}, {"description": "Capacitor array 100nF"}),
             ("U2", {"1": ("VDD", "VDD_RF"), "2": ("GND", "GND")}, {}),
             ("C10", {"1": ("1", "+5V"), "2": ("2", "GND"), "3": ("3", "VDD_RF")},
              {"description": "Feedthrough capacitor 1uF"})]
    result, found = _rails(parts)
    assert not any(check == "no_capacitor" for check, _ in found)


def test_bootstrap_capacitors_versus_signal_pins():
    buck = [("U3", {"1": ("VDDB", "NetU3_1"), "2": ("SW", "SW_NODE"), "3": ("VIN", "+12V"),
                    "4": ("GND", "GND")}, {}),
            ("C1", {"1": ("1", "+12V"), "2": ("2", "GND")}, {"value": "10uF"}),
            ("C4", {"1": ("1", "NetU3_1"), "2": ("2", "SW_NODE")}, {"value": "100nF"})]
    assert ("capacitor_elsewhere", "NetU3_1") in _rails(buck)[1]
    adc = [("U3", {"1": ("VDD", "+5V"), "2": ("AIN", "AIN"), "3": ("GND", "GND")}, {}),
           ("C4", {"1": ("1", "+5V"), "2": ("2", "AIN")}, {"value": "100nF"})]
    assert _rails(adc)[1] == {("no_capacitor", "+5V")}


def test_filters_by_ic_and_rail():
    result = run_decoupling_check(_netlist(), ics=["u3"])
    assert {f["rail"] for f in result["findings"]} == {"+1V8", "VREF"}
    result = run_decoupling_check(_netlist(), rails=["+1v8"])
    assert [f["check"] for f in result["findings"]] == ["no_capacitor"]
    assert run_decoupling_check(_netlist(), ics=["U9"])["error"] == "ic_not_found"
    assert run_decoupling_check(_netlist(), rails=["NOPE"])["error"] == "rail_not_found"
    # A part named explicitly is checked like an IC, e.g. a connector's power pin.
    parts = [*PARTS, ("J1", {"1": ("1", "+12V"), "2": ("2", "GND")}, {})]
    result = run_decoupling_check(_netlist(parts), ics=["J1"])
    assert [(f["check"], f["rail"]) for f in result["findings"]] == [("no_capacitor", "+12V")]
    # An IC whose pins are only numbered is checked on named rails when named.
    parts = [*PARTS, ("U9", {"1": ("1", "+12V"), "2": ("2", "GND")}, {})]
    result = run_decoupling_check(_netlist(parts), ics=["U9"])
    assert [(f["check"], f["rail"]) for f in result["findings"]] == [("no_capacitor", "+12V")]
    # Nothing to check says so.
    parts = [*PARTS, ("U9", {"1": ("PA1", "SIG"), "2": ("2", "GND")}, {})]
    result = run_decoupling_check(_netlist(parts), ics=["U9"])
    assert "U9: no pin on a power rail was recognised." in result["notes"]
    result = run_decoupling_check(_netlist(), rails=["+3V3", "+5V"], ics=["U3"])
    assert "+3V3: no fitted IC power pin on it." in result["notes"]


# ---------- PCB ----------

def _via(net, x, y):
    return Primitive(kind="via", layer=1, layer_to=32, net=net, component=None,
                     x1=x, y1=y, x2=x, y2=y, width=20.0, hole=8.0)


def _pcb(placements, vias=(), pours=()):
    """PcbData from placements [(designator, side, x, y, {pad: (net, dx, dy)})]."""
    nets: list[str] = []

    def net_id(name):
        if name not in nets:
            nets.append(name)
        return nets.index(name)

    components, primitives = [], []
    for index, (designator, side, x, y, pads) in enumerate(placements):
        components.append(PcbComponent(
            index=index, refdes=designator, source_uid_tail=None, channel_path=None,
            x=x, y=y, rotation=0.0, side=side, footprint="", designator=designator,
        ))
        for pad, (net, dx, dy) in pads.items():
            primitives.append(_pad(net=net_id(net), component=index, x=x + dx, y=y + dy,
                                   name=pad, layer=32 if side == "bottom" else 1,
                                   width=20.0))
    primitives += [_via(net_id(net), x, y) for net, x, y in vias]
    return PcbData(
        board=_board(), nets=nets, components=components, primitives=primitives,
        pours=[PolygonPour(i, net_id(net), layer, None, vertices)
               for i, (net, layer, vertices) in enumerate(pours)],
        layer_name_to_v6=dict(LAYERS), rules=[], classes=[], diff_pairs=[], xsignals=[],
    )


BOARD_PARTS = [
    ("U1", {"1": ("VDD", "+3V3"), "2": ("VDD", "+3V3"), "3": ("GND", "GND")}, {}),
    ("C1", {"1": ("1", "+3V3"), "2": ("2", "GND")}, {"value": "100nF"}),
    ("C2", {"1": ("1", "+3V3"), "2": ("2", "GND")}, {"value": "10uF"}),
    ("C9", {"1": ("1", "+3V3"), "2": ("2", "GND")}, {"value": "100nF"}),
]


def _placements(c1=(400.0, 0.0, "top"), c2=(3000.0, 0.0)):
    x, y, side = c1
    return [
        ("U1", "top", 0.0, 0.0, {"1": ("+3V3", 0, 0), "2": ("+3V3", 0, 600),
                                 "3": ("GND", 0, 40)}),
        ("C1", side, x, y, {"1": ("+3V3", 0, 0), "2": ("GND", 40, 0)}),
        ("C2", "top", *c2, {"1": ("+3V3", 0, 0), "2": ("GND", 40, 0)}),
        ("C9", "top", 2000.0, 0.0, {"1": ("+3V3", 0, 0), "2": ("GND", 40, 0)}),
    ]


def _run(placements, plan=None, vias=(), pours=(), **kwargs):
    netlist = _netlist(BOARD_PARTS)
    index = PcbIndex(_pcb(placements, vias, pours), netlist)
    return run_decoupling_check(netlist, plan, index=index, **kwargs)


def test_far_capacitor():
    result = _run(_placements())
    (far,) = _checks(result, "far_capacitor")
    assert far["nearest"]["designator"] == "C1"
    assert far["nearest"]["distance_mil"] == pytest.approx(380.0)
    assert result["pcb"]["pairs_measured"] == 1 and result["pcb"]["within_mm"]["10"] == 1
    (pair,) = result["pairs"]
    assert (pair["ic"], pair["rail"], pair["pins"]) == ("U1", "+3V3", ["1", "2"])
    close = _run(_placements(c1=(60.0, 0.0, "top")))
    assert not _checks(close, "far_capacitor")
    # U1's second VDD pad is 600 mil from every capacitor.
    (pads,) = _checks(close, "pads_far")
    assert (pads["pads_far"], pads["pads"]) == (1, 2)
    wide = _run(_placements(), max_distance_mil=400.0)
    assert not _checks(wide, "far_capacitor")


def test_a_bulk_capacitor_at_the_pin_counts():
    # A regulator's 10 uF input capacitor beside the pin: no warning, though the nearest
    # 100 nF is 380 mil away (it is listed as nearest_under_1uF).
    result = _run(_placements(c2=(40.0, 0.0)))
    assert not _checks(result, "far_capacitor")
    (pair,) = result["pairs"]
    assert pair["nearest"]["designator"] == "C2"
    assert pair["nearest_under_1uF"]["designator"] == "C1"


def test_naming_an_ic_lists_its_nearest_capacitors():
    result = _run(_placements(), ics=["U1"])
    (pair,) = result["pairs"]
    assert [cap["designator"] for cap in pair["capacitors"]] == ["C1", "C9", "C2"]
    assert _run(_placements(), ics=["C1"])["error"] == "not_an_ic"


def test_not_fitted_nearer_capacitor_is_named():
    result = _run(_placements(c1=(60.0, 0.0, "top")), VariantPlan(name="V", not_fitted={"C1"}))
    (far,) = _checks(result, "far_capacitor")
    assert far["nearest"]["designator"] == "C9"
    assert far["not_fitted_nearer"]["designator"] == "C1"
    assert "C1 would be nearer but is Not Fitted" in far["issue"]


def test_opposite_side_capacitor_needs_a_via_or_pour():
    under = (0.0, 60.0, "bottom")
    (finding,) = _checks(_run(_placements(c1=under)), "opposite_side_no_via")
    assert finding["nearest"]["designator"] == "C1" and finding["nearest"]["side"] == "bottom"
    assert not _checks(_run(_placements(c1=under), vias=[("+3V3", 30.0, 60.0)]),
                       "opposite_side_no_via")
    square = [(-100.0, -100.0), (100.0, -100.0), (100.0, 200.0), (-100.0, 200.0)]
    assert not _checks(_run(_placements(c1=under), pours=[("+3V3", 32, square)]),
                       "opposite_side_no_via")


def test_every_board_copy_of_a_part_is_measured():
    # Two PCB components carry C1 (an out-of-sync board): the near one counts.
    placements = [*_placements(), ("C1", "top", 60.0, 0.0,
                                   {"1": ("+3V3", 0, 0), "2": ("GND", 40, 0)})]
    result = _run(placements)
    assert not _checks(result, "far_capacitor")
    assert result["pairs"][0]["nearest"]["distance_mil"] == pytest.approx(40.0)


def test_parts_missing_on_the_pcb_are_listed():
    placements = [p for p in _placements() if p[0] != "C9"]
    result = _run(placements)
    assert result["pcb"]["not_on_pcb"] == ["C9"]


def test_example_bluetooth_sentinel():
    """Altium's public example: every IC rail measured, all within 5 mm but one pad."""
    from server.altium import AltiumClient
    from server.parsers.pcb_doc import parse_pcb_doc
    from server.parsers.prj_pcb import parse_prj_pcb

    folder = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples\Bluetooth Sentinel")
    if not (folder / "Bluetooth_Sentinel.PcbDoc").is_file():
        pytest.skip("Altium example boards not installed")
    pcb = parse_pcb_doc(str(folder / "Bluetooth_Sentinel.PcbDoc"))
    pin_to_net: dict = {}
    for primitive in pcb.primitives:
        if primitive.kind == "pad" and primitive.component is not None \
                and primitive.net is not None and primitive.pad_name:
            component = pcb.components[primitive.component]
            pins = pin_to_net.setdefault(component.designator or component.refdes, {})
            pins.setdefault(primitive.pad_name, pcb.nets[primitive.net])
    netlist = {"nets": {}, "pin_to_net": pin_to_net}
    project = parse_prj_pcb(str(folder / "Bluetooth_Sentinel.PrjPcb"))
    AltiumClient()._enrich_from_schdocs(project.sheet_paths, netlist, project.annotation_paths)
    result = run_decoupling_check(netlist, index=PcbIndex(pcb, netlist))
    assert result["overview"]["ic_rail_pairs"] == 8
    assert result["pcb"]["pairs_measured"] == 8 and result["pcb"]["within_mm"]["5"] == 8
    assert result["severity_counts"]["warning"] == 0
    assert {f["check"] for f in result["findings"]} == {"pads_far", "no_small_capacitor"}


# ---------- tool ----------

@pytest.fixture
def board(monkeypatch, tmp_path: Path):
    netlist = _netlist(BOARD_PARTS)
    index = PcbIndex(_pcb(_placements()), netlist)
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


def test_check_decoupling_tool(board):
    result = json.loads(main_mod.check_decoupling(units="mm"))
    assert result["pcb_doc"] == "Board.PcbDoc" and result["units"] == "mm"
    (far,) = _checks(result, "far_capacitor")
    assert far["nearest"]["distance_mm"] == pytest.approx(9.652)
    assert result["pcb"]["max_distance_mm"] == pytest.approx(5.0, abs=1e-3)
    wide = json.loads(main_mod.check_decoupling(max_distance=400, units="mil"))
    assert not _checks(wide, "far_capacitor")
    # Without units everything is in mm, max_distance included (the board shows mil).
    default = json.loads(main_mod.check_decoupling(max_distance=10))
    assert default["units"] == "mm" and not _checks(default, "far_capacitor")
    assert default["pairs"][0]["nearest"]["distance_mm"] == pytest.approx(9.652)
    assert json.loads(main_mod.check_decoupling(units="inch"))["error"] == "invalid_units"
    assert json.loads(main_mod.check_decoupling(max_distance=-1))["error"] == \
        "invalid_max_distance"
    assert json.loads(main_mod.check_decoupling(ic="U7"))["error"] == "ic_not_found"
    assert DEFAULT_MAX_DISTANCE_MIL == pytest.approx(196.85, abs=0.01)


def test_check_decoupling_without_a_board(board, monkeypatch):
    monkeypatch.setitem(main_mod._project, "pcb_doc_paths", [])
    result = json.loads(main_mod.check_decoupling())
    assert "pcb" not in result and "far_capacitor" in result["not_checked"]
    assert result["not_checked"]["far_capacitor"] == "the project lists no PcbDoc"


def test_check_decoupling_pages_and_trims(board, monkeypatch):
    for n in range(10, 160):
        board["parts"][f"U{n}"] = {"footprint": "", "comment": ""}
        board["pin_to_net"][f"U{n}"] = {"1": f"+V{n}", "2": "GND"}
        board["components"][f"U{n}"] = {
            "pins": {"1": {"name": "VDD", "net": f"+V{n}"}, "2": {"name": "GND", "net": "GND"}},
            "params": {}, "kind": 0,
        }
    result = json.loads(main_mod.check_decoupling(checks=["no_capacitor"]))
    assert result["total"] == 150 and result["next_offset"] == 100
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 12000)
    trimmed = json.loads(main_mod.check_decoupling(checks=["no_capacitor"]))
    assert trimmed["findings_limited_to"] in (40, 15) and len(trimmed["rails"]) <= 20


def test_check_decoupling_needs_a_project(monkeypatch):
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.check_decoupling())["error"] == "no_project"
    assert "next_offset" in main_mod.check_decoupling.__doc__
