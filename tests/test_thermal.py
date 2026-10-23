"""Thermal estimate and thermal-via audit (services/thermal, check_thermal): hot parts,
dissipation and Tj arithmetic, exposed pads and their vias, neighbours. Generic parts only."""

import json
import math
from pathlib import Path

import pytest

import server.main as main_mod
import server.services.thermal as th
from server.parsers.pcb_doc import CopperArea, PcbComponent, PcbData, PolygonPour, Primitive
from server.parsers.prj_pcb import VariantDefinition, VariantState
from server.services.bom import VariantPlan
from server.services.decoupling import component_pads, pcb_components
from server.services.pcb_index import PcbIndex
from tests.test_circuit_calcs import buck, cap, led, res
from tests.test_diff_pair_checks import LAYERS
from tests.test_pcb_index import _board
from tests.test_pull_resistors import _netlist

MM = 1 / 0.0254

# (refdes, {pin: (name, net[, type])}, info): an input FET, an LDO, a buck, a buck
# controller with its two FETs, a shunt, a power resistor, a bright LED, a PHY with its
# crystal, a loose crystal, an electrolytic, a plain IC and an LED driver.
BOARD = [
    ("J1", {"1": ("VIN", "+12V_IN"), "2": ("GND", "GND")}, {"description": "DC power jack"}),
    ("Q3", {"1": ("G", "Q3_G"), "2": ("S", "+12V_IN"), "3": ("D", "+12V")},
     {"description": "P-channel MOSFET"}),
    res("R9", "Q3_G", "GND", "100k"),
    ("U1", {"1": ("IN", "+5V", "Power"), "2": ("OUT", "+3V3", "Power"),
            "3": ("GND", "GND", "Power")}, {"description": "LDO regulator, 3.3 V"}),
    buck("U2", vin="+12V", sw="SW1", fb="FB1"),
    ("L1", {"1": ("1", "SW1"), "2": ("2", "+5V")}, {"description": "Inductor 4.7uH"}),
    res("R5", "+5V", "FB1", "100k"), res("R6", "FB1", "GND", "22k"),
    ("U3", {"1": ("VIN", "+12V", "Power"), "2": ("HG", "HG1"), "3": ("LG", "LG1"),
            "4": ("SW", "SW2"), "5": ("FB", "FB2", "Input"), "6": ("GND", "GND", "Power")},
     {"description": "Synchronous buck controller"}),
    ("Q1", {"1": ("G", "HG1"), "2": ("D", "+12V"), "3": ("S", "SW2")},
     {"description": "N-channel MOSFET"}),
    ("Q2", {"1": ("G", "LG1"), "2": ("D", "SW2"), "3": ("S", "GND")},
     {"description": "N-channel MOSFET"}),
    ("L2", {"1": ("1", "SW2"), "2": ("2", "+1V8")}, {"description": "Inductor 1uH"}),
    res("R7", "+1V8", "FB2", "20k"), res("R8", "FB2", "GND", "10k"),
    res("R1", "+5V", "+5V_LOAD", "0.01", description="Current sense resistor"),
    res("R2", "+5V", "GND", "22", description="Resistor 2512"),
    led("LED1", "LED1_A", "GND", "LED, red, 1206"), res("R3", "+5V", "LED1_A", "10"),
    ("U4", {"1": ("VDD", "+3V3", "Power"), "2": ("GND", "GND", "Power"),
            "3": ("XI", "XTAL_I"), "4": ("XO", "XTAL_O"), "5": ("TXP", "TX_P"),
            "6": ("TXN", "TX_N")}, {"description": "Gigabit Ethernet PHY transceiver"}),
    ("Y1", {"1": ("1", "XTAL_I"), "2": ("2", "XTAL_O")}, {"description": "Crystal 25 MHz"}),
    ("Y2", {"1": ("1", "OSC_A"), "2": ("2", "OSC_B")}, {"description": "Crystal 32.768 kHz"}),
    ("C3", {"1": ("1", "+12V"), "2": ("2", "GND")},
     {"value": "220uF", "description": "Aluminium electrolytic capacitor 25V"}),
    cap("C4", "+3V3"),
    ("U5", {"1": ("VDD", "+3V3", "Power"), "2": ("GND", "GND", "Power"),
            "3": ("IO1", "SIG1")}, {"description": "Interface IC"}),
    ("U6", {"1": ("VIN", "+3V3", "Power"), "2": ("GND", "GND", "Power"),
            "3": ("LED1", "LEDK1")}, {"description": "LED driver, constant current"}),
]


def _run(board=BOARD, plan=None, **kwargs):
    kwargs.setdefault("limit", 1000)
    return th.run_thermal_check(_netlist(board), plan, **kwargs)


def _row(result, ref):
    return next(row for row in result["parts"] if row["designator"] == ref)


def _findings(result, check=None, ref=None):
    return [f for f in result["findings"] if (check is None or f["check"] == check)
            and (ref is None or ref in f["designators"])]


def _inputs(**maps):
    values = {}
    for name in ("load", "power", "iq", "theta", "tj_max", "eta"):
        if name in maps:
            values[name], error = th.parse_values(maps.pop(name), name)
            assert error is None
    return th.Inputs(**{key: value for key, value in values.items() if value is not None},
                     **maps)


# ---------- arguments ----------

def test_parse_values():
    assert th.parse_values("U5=0.3, 3V3=500mA", "load") == ({"U5": 0.3, "3V3": 0.5}, None)
    assert th.parse_values("U1=1.2,Q3=400mW", "power") == ({"U1": 1.2, "Q3": 0.4}, None)
    assert th.parse_values("U5=5mA", "iq")[0] == {"U5": pytest.approx(0.005)}
    assert th.parse_values("U5=45 C/W", "theta") == ({"U5": 45.0}, None)
    assert th.parse_values("150", "tj_max") == (150.0, None)
    assert th.parse_values("U5=150C", "tj_max") == ({"U5": 150.0}, None)
    assert th.parse_values("0.85", "eta") == (0.85, None)
    assert th.parse_values("U7=88%", "eta") == ({"U7": 0.88}, None)
    assert th.parse_values("U7=88", "eta") == ({"U7": 0.88}, None)  # a bare percentage
    assert th.parse_values("U5=0.3,", "power") == ({"U5": 0.3}, None)  # a trailing comma
    # Unit scaling leaves no float noise: 700 x 0.001 is 0.7000000000000001.
    assert th.parse_values("U6=700mA", "load") == ({"U6": 0.7}, None)
    assert th.parse_values("U6=700mW", "power") == ({"U6": 0.7}, None)
    assert th.parse_values("U6=100uA", "iq") == ({"U6": 0.0001}, None)
    assert th.parse_values("", "load") == (None, None)
    assert th.parse_values(None, "theta") == (None, None)


@pytest.mark.parametrize("text, arg", [
    ("U5", "load"), ("U5=x", "load"), ("U5=3V", "load"), ("45", "theta"),
    ("U5=2000", "theta"), ("U5=-1", "power"), ("U7=0", "eta"), ("U7=2", "eta"),
    ("U7=150", "eta"), ("U5=20", "tj_max"), ("U5=1V", "iq"),
])
def test_parse_values_errors(text, arg):
    value, error = th.parse_values(text, arg)
    assert value is None and error["error"] == f"invalid_{arg}"
    assert arg in error["message"]


# ---------- hot parts ----------

def test_hot_parts_and_their_basis():
    result = _run()
    bases = {row["designator"]: set(row["basis"]) for row in result["parts"]}
    assert bases["U1"] == {"linear_regulator"}
    assert bases["U2"] == {"switching_converter"} and bases["U3"] == {"switching_converter"}
    assert bases["Q1"] == bases["Q2"] == {"switch_fet"}
    assert bases["Q3"] == {"power_path_transistor"}
    assert bases["R1"] == {"shunt_resistor"}
    assert bases["R2"] == {"power_resistor"}  # 5 V across 22 ohm: 1.14 W
    assert bases["R3"] == {"power_resistor"}  # 0.3 A of LED current through 10 ohm
    assert bases["LED1"] == {"power_led"}  # (5 - 2.0) / 10 = 0.3 A at 2.0 V: 0.6 W
    assert bases["U4"] == {"phy"} and bases["U6"] == {"led_driver"}
    # Parts that do not dissipate by themselves are not listed.
    assert not {"U5", "C3", "C4", "Y1", "Y2", "R5", "L1"} & set(bases)
    assert _row(result, "Q1")["power_stage"] == ["Q2", "Q3", "U3"]
    assert result["summary"]["hot_parts"] == 12
    assert result["summary"]["by_basis"]["switch_fet"] == 2
    # Without a PcbDoc the PCB checks are skipped and the rest still runs.
    assert set(result["not_checked"]) == th.PCB_CHECKS
    assert "thermal_pads" not in result["summary"]
    assert "pcb" not in _row(result, "U1")


def test_not_fitted_parts_are_left_out_and_named_parts_are_added():
    result = _run(plan=VariantPlan(name="Lite", not_fitted={"R2", "Q1"}), parts=["U5", "Q2"])
    assert [row["designator"] for row in result["parts"]] == ["Q2", "U5"]
    assert _row(result, "U5")["basis"] == {"named": "named in part"}
    assert set(_row(result, "Q2")["basis"]) == {"switch_fet"}
    assert _run(parts=["U99"])["error"] == "part_not_found"
    assert _run(checks=["nope"])["error"] == "invalid_checks"


# ---------- dissipation and Tj ----------

def test_ldo_dissipation_and_tj():
    inputs = _inputs(load="U1=0.5", iq="U1=2mA", theta="U1=50", ta=40.0, ta_max=85.0)
    row = _row(_run(inputs=inputs), "U1")
    power = row["power"]
    # (5 - 3.3) x 0.5 + 5 x 0.002 = 0.86 W
    assert power["status"] == "computed" and power["watts"] == pytest.approx(0.86)
    assert power["formula"] == "P = (Vin - Vout) x I_out + Vin x Iq"
    assert power["inputs"]["Vin"]["volts"] == 5.0 and power["inputs"]["Vout"]["volts"] == 3.3
    assert power["inputs"]["I_out"]["source"] == "load argument (U1)"
    tj = row["tj"]
    assert tj["rise_c"] == pytest.approx(43.0) and tj["tj_c"] == pytest.approx(83.0)
    assert tj["tj_worst_c"] == pytest.approx(128.0) and tj["status"] == "over_max"
    result = _run(inputs=inputs)
    (over,) = _findings(result, "tj_over_max")
    assert over["severity"] == "warning" and "128 C" in over["issue"]
    # The load by its output rail; a higher Tj max makes it near the limit.
    near = _run(inputs=_inputs(load="+3V3=0.5", theta="U1=50", tj_max="U1=150",
                               ta_max=85.0))
    row = _row(near, "U1")
    assert row["power"]["inputs"]["I_out"]["source"] == "load argument (rail +3V3)"
    assert row["tj"]["tj_max_c"] == 150.0 and row["tj"]["status"] == "near_max"
    (finding,) = _findings(near, "tj_near_max")
    assert finding["severity"] == "info"
    # One Tj max for all, at the default 25 C ambient: fine.
    ok = _row(_run(inputs=_inputs(load="U1=0.5", theta="U1=50", tj_max="125")), "U1")
    assert ok["tj"]["tj_c"] == pytest.approx(67.5) and ok["tj"]["status"] == "ok"
    assert "tj_worst_c" not in ok["tj"]


def test_switcher_needs_eta_and_load():
    row = _row(_run(), "U2")
    assert row["power"]["status"] == "needs_inputs"
    needs = " ".join(row["power"]["needs"])
    assert "load='U2=amps'" in needs and "eta='U2=0.85'" in needs
    # 5 V x 2 A x (1/0.8 - 1) = 2.5 W; eta one for all.
    row = _row(_run(inputs=_inputs(load="U2=2", eta="0.8", theta="U2=30")), "U2")
    assert row["power"]["watts"] == pytest.approx(2.5)
    assert row["power"]["formula"] == "P = Vout x I_out x (1/eta - 1)"
    assert row["tj"]["tj_c"] == pytest.approx(100.0)
    # eta by designator, as a percentage.
    row = _row(_run(inputs=_inputs(load="+5V=1", eta="U2=90%")), "U2")
    assert row["power"]["watts"] == pytest.approx(5 * 1 * (1 / 0.9 - 1), rel=1e-3)
    assert row["tj"]["status"] == "needs_theta"


def test_other_estimates():
    inputs = _inputs(load="R1=3,Q3=2", power="Q1=0.4,U4=1.1")
    result = _run(inputs=inputs)
    shunt = _row(result, "R1")["power"]
    assert shunt["watts"] == pytest.approx(0.09) and shunt["formula"] == "P = I^2 x R"
    assert _row(result, "R2")["power"]["watts"] == pytest.approx(25 / 22, rel=1e-3)
    assert _row(result, "LED1")["power"]["watts"] == pytest.approx(0.6)
    assert _row(result, "R3")["power"]["watts"] == pytest.approx(0.9)
    assert _row(result, "Q1")["power"] == {"status": "given", "watts": 0.4,
                                           "source": "power argument"}
    assert _row(result, "U4")["power"]["status"] == "given"
    # A FET in the power path needs its own figure: a load alone does not give it.
    assert _row(result, "Q3")["power"]["status"] == "needs_inputs"
    assert result["summary"]["dissipation"] == {"computed": 4, "given": 2, "needs_inputs": 6}
    needed = result["inputs_needed"]
    assert "U2" in needed["load"]["designators"] and "Q2" in needed["power"]["designators"]
    assert set(needed["theta"]["designators"]) >= {"R1", "R2", "LED1", "Q1", "U4"}
    # Hottest first.
    assert [row["designator"] for row in result["parts"][:3]] == ["R2", "U4", "R3"]


def test_diodes_processors_and_multi_output_regulators():
    board = [
        ("J1", {"1": ("VIN", "+12V_IN"), "2": ("GND", "GND")}, {"description": "DC power jack"}),
        ("D1", {"1": ("A", "+12V_IN"), "2": ("K", "+12V")}, {"description": "Schottky diode"}),
        ("U7", {"1": ("VIN", "+12V", "Power"), "2": ("SW", "SW7"), "3": ("FB", "FB7", "Input"),
                "4": ("GND", "GND", "Power")}, {"description": "Step-down converter"}),
        ("D2", {"1": ("A", "GND"), "2": ("K", "SW7")}, {"description": "Schottky diode"}),
        ("L7", {"1": ("1", "SW7"), "2": ("2", "+5V")}, {"description": "Inductor 10uH"}),
        res("R71", "+5V", "FB7", "100k"), res("R72", "FB7", "GND", "22k"),
        ("U9", {"1": ("VIN", "+5V", "Power"), "2": ("OUT1", "+3V3A", "Power"),
                "3": ("OUT2", "+1V2A", "Power"), "4": ("GND", "GND", "Power")},
         {"description": "Dual LDO regulator"}),
        ("U10", {"1": ("VDD", "+3V3A", "Power"), "2": ("GND", "GND", "Power"),
                 **{str(n): (f"PA{n}", f"GPIO{n}") for n in range(3, 9)}},
         {"description": "ARM Cortex-M4 32-bit microcontroller"}),
    ]
    result = _run(board, inputs=_inputs(load="D1=2,+3V3A=0.2"))
    assert set(_row(result, "D1")["basis"]) == {"power_path_diode"}
    assert set(_row(result, "D2")["basis"]) == {"converter_diode"}
    assert _row(result, "D2")["power_stage"] == ["D1", "U7"]  # D1 feeds U7
    assert set(_row(result, "U10")["basis"]) == {"processor"}
    diode = _row(result, "D1")["power"]
    assert diode["watts"] == pytest.approx(0.9) and diode["inputs"]["Vf"]["typical"]
    dual = _row(result, "U9")["power"]
    # (5 - 3.3) x 0.2 for the output with a load; the other is waiting for one.
    assert dual["status"] == "computed" and dual["watts"] == pytest.approx(0.34)
    assert dual["partial"] == "1 of 2 outputs counted"
    assert [out["output"] for out in dual["outputs"]] == ["+1V2A", "+3V3A"]
    assert "load='+1V2A=amps'" in " ".join(dual["needs"])


def _diode_board(description="Schottky diode", **info):
    return [
        ("J1", {"1": ("VIN", "+12V_IN"), "2": ("GND", "GND")}, {"description": "DC power jack"}),
        ("D1", {"1": ("A", "+12V_IN"), "2": ("K", "+12V")}, {"description": description, **info}),
        ("U7", {"1": ("VIN", "+12V", "Power"), "2": ("OUT", "+5V", "Power"),
                "3": ("GND", "GND", "Power")}, {"description": "LDO regulator, 5 V"}),
    ]


def test_a_diode_uses_its_stated_vf():
    inputs = _inputs(load="D1=3")
    # The one voltage below 1.6 V in a rectifier's text is its Vf.
    text = _row(_run(_diode_board("Schottky barrier rectifier, 5 A, 40 V, 0.55 V"),
                     inputs=inputs), "D1")["power"]
    assert text["watts"] == pytest.approx(1.65)
    assert text["inputs"]["Vf"] == {"volts": 0.55, "source": "stated: text (a voltage this "
                                    "low on a rectifier: read as its Vf)"}
    assert "stated Vf" in text["note"]
    # A forward-voltage parameter, then 'Vf 0.6V' in the text.
    param = _row(_run(_diode_board(params={"Forward Voltage": "0.5V"}), inputs=inputs),
                 "D1")["power"]
    assert param["inputs"]["Vf"]["volts"] == 0.5 and "parameter" in param["inputs"]["Vf"][
        "source"]
    keyword = _row(_run(_diode_board("Rectifier, Vf 0.9V, 1000 V"), inputs=inputs),
                   "D1")["power"]
    assert keyword["inputs"]["Vf"] == {"volts": 0.9, "source": "stated: text (Vf)"}
    # Nothing stated: the typical Schottky Vf, labelled.
    typical = _row(_run(_diode_board(), inputs=inputs), "D1")["power"]
    assert typical["watts"] == pytest.approx(1.35) and typical["inputs"]["Vf"]["typical"]


def test_a_shunt_gets_no_v2r_from_nominal_voltages():
    """1 V between two nominal rails across a 0.5 ohm sense resistor would read 2 W: a
    shunt's power comes only from its current (I^2 x R)."""
    board = BOARD + [res("R16", "+5V", "ISNS", "0.5", description="Current sense resistor")]
    row = _row(_run(board, rails={"ISNS": 4.0}), "R16")
    assert set(row["basis"]) == {"shunt_resistor"}
    assert row["power"]["status"] == "needs_inputs"
    assert _row(_run(board, rails={"ISNS": 4.0}, inputs=_inputs(load="R16=2")),
                "R16")["power"]["watts"] == pytest.approx(2.0)


def test_iq_with_an_unknown_vin_is_asked_for():
    board = [buck("U12", vin="VIN_X", sw="SW12", fb="FB12"),
             ("L12", {"1": ("1", "SW12"), "2": ("2", "+5V")}, {"description": "Inductor"}),
             res("R12", "+5V", "FB12", "100k"), res("R13", "FB12", "GND", "22k")]
    result = _run(board, inputs=_inputs(load="U12=1", eta="0.9", iq="U12=1mA"))
    power = _row(result, "U12")["power"]
    assert power["status"] == "computed" and "iq" not in power
    assert any("Vin x Iq" in need for need in power["needs"])
    assert result["inputs_needed"]["rails"]["designators"] == ["U12"]


def test_unknown_names_in_the_inputs_are_noted():
    result = _run(inputs=_inputs(load="U77=1,+5V=2", theta="Q99=40"))
    note = next(note for note in result["notes"] if note.startswith("Names no part"))
    assert "load U77" in note and "theta Q99" in note and "+5V" not in note


# ---------- the PcbDoc ----------

def _transform(side, x, y, rotation, dx, dy):
    if side == "bottom":
        dx = -dx
    angle = math.radians(rotation)
    return (x + dx * math.cos(angle) - dy * math.sin(angle),
            y + dx * math.sin(angle) + dy * math.cos(angle))


def qfn(net_ep="GND", ep=(100.0, 100.0), ep_name="17", nets=None):
    """A 16-pin QFN: 10 x 30 mil pins on a 20 mil pitch around a centre pad."""
    pads = []
    number = 1
    for side_x, side_y, vertical in ((-80, 0, False), (0, -80, True), (80, 0, False),
                                     (0, 80, True)):
        for step in range(4):
            offset = (step - 1.5) * 20
            dx, dy = (side_x, offset) if not vertical else (offset, side_y)
            w, h = (30.0, 10.0) if not vertical else (10.0, 30.0)
            pads.append((str(number), (nets or {}).get(str(number), "SIG1"), dx, dy, w, h, 2))
            number += 1
    if ep:
        pads.append((ep_name, net_ep, 0.0, 0.0, ep[0], ep[1], 2))
    return pads


def sot223(tab_net="+3V3"):
    return [("1", "GND", -90.0, -120.0, 40.0, 70.0, 2), ("2", "+3V3", 0.0, -120.0, 40.0, 70.0, 2),
            ("3", "+5V", 90.0, -120.0, 40.0, 70.0, 2), ("4", tab_net, 0.0, 120.0, 130.0, 80.0, 2)]


def two_pin(net_a, net_b):
    return [("1", net_a, -40.0, 0.0, 30.0, 30.0, 2), ("2", net_b, 40.0, 0.0, 30.0, 30.0, 2)]


def sot23(nets):
    return [("1", nets[0], -37.0, -45.0, 25.0, 35.0, 2), ("2", nets[1], 37.0, -45.0, 25.0, 35.0, 2),
            ("3", nets[2], 0.0, 45.0, 25.0, 35.0, 2)]


class Pcb:
    """A synthetic board: placements in the footprint's frame, placed as Altium stores
    them (rotated, mirrored on the bottom)."""

    def __init__(self):
        self.nets: list[str] = []
        self.components: list[PcbComponent] = []
        self.primitives: list[Primitive] = []
        self.pours: list[PolygonPour] = []
        self.areas: list[CopperArea] = []
        self.where: dict[str, tuple] = {}

    def net(self, name):
        if name is None:
            return None
        if name not in self.nets:
            self.nets.append(name)
        return self.nets.index(name)

    def place(self, designator, x, y, pads, rotation=0.0, side="top"):
        index = len(self.components)
        self.components.append(PcbComponent(
            index=index, refdes=designator, source_uid_tail=None, channel_path=None, x=x,
            y=y, rotation=rotation, side=side, footprint="", designator=designator))
        self.where[designator] = (side, x, y, rotation)
        for name, net, dx, dy, w, h, shape in pads:
            px, py = _transform(side, x, y, rotation, dx, dy)
            self.primitives.append(Primitive(
                kind="pad", layer=32 if side == "bottom" else 1, net=self.net(net),
                component=index, x1=px, y1=py, x2=px, y2=py, width=w, height=h, shape=shape,
                rotation=rotation, pad_name=name))
        return self

    def via(self, net, designator=None, dx=0.0, dy=0.0, x=None, y=None, hole=12.0,
            layer_to=32):
        if designator is not None:
            x, y = _transform(*self.where[designator], dx, dy)
        self.primitives.append(Primitive(kind="via", layer=1, layer_to=layer_to,
                                         net=self.net(net), component=None, x1=x, y1=y, x2=x,
                                         y2=y, width=24.0, hole=hole))
        return self

    def pour(self, net, layer, box, poured=True, shelved=False):
        """A solid pour with its poured copper saved as a region (as Altium does), or a
        shelved one, or one never poured: neither holds copper."""
        x1, y1, x2, y2 = box
        outline = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
        self.pours.append(PolygonPour(len(self.pours), self.net(net), layer, "Solid",
                                      outline, shelved=shelved))
        if poured and not shelved:
            self.areas.append(CopperArea("region", layer, self.net(net), None, outline,
                                         pour_id=len(self.pours) - 1))
        return self

    def fill(self, designator, net, dx1, dy1, dx2, dy2):
        """A copper fill the placed footprint owns (a drain paddle), in its frame."""
        side, x, y, rotation = self.where[designator]
        corners = [_transform(side, x, y, rotation, dx, dy)
                   for dx, dy in ((dx1, dy1), (dx2, dy1), (dx2, dy2), (dx1, dy2))]
        component = next(c.index for c in self.components if c.designator == designator)
        self.areas.append(CopperArea("fill", 32 if side == "bottom" else 1, self.net(net),
                                     component, corners))
        return self

    def region(self, net, layer, box):
        x1, y1, x2, y2 = box
        self.areas.append(CopperArea("region", layer, self.net(net), None,
                                     [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]))
        return self

    def index(self, netlist):
        data = PcbData(
            board=_board(), nets=self.nets, components=self.components,
            primitives=self.primitives, pours=self.pours, layer_name_to_v6=dict(LAYERS),
            rules=[], classes=[], diff_pairs=[], xsignals=[], areas=self.areas,
            area_streams=("Regions6", "Fills6"))
        return PcbIndex(data, netlist)


def _pads_of(index, designator):
    by_name = pcb_components(index)
    rows = by_name[designator]
    return rows[0]["id"], component_pads(index, by_name, [designator])[rows[0]["id"]]


def test_exposed_pad_detection():
    netlist = _netlist(BOARD)
    pcb = (Pcb().place("U5", 0, 0, qfn())
           .place("U1", 1000, 0, sot223())
           .place("U4", 2000, 0, qfn(ep=None) + [
               ("EP", "GND", dx, dy, 45.0, 45.0, 2) for dx in (-25, 25) for dy in (-25, 25)])
           .place("U6", 3000, 0, qfn(ep=None) + [("17", "GND", -30, 0, 50.0, 100.0, 2),
                                                 ("18", "+3V3", 30, 0, 50.0, 100.0, 2)])
           .place("U2", 4000, 0, [(str(n), "SIG1", (n % 3) * 40.0, (n // 3) * 40.0, 25.0,
                                   25.0, 1) for n in range(9)])
           .place("U3", 5000, 0, qfn(ep=(20.0, 20.0)))
           .place("J1", 6000, 0, qfn(ep=(150.0, 150.0))))
    index = pcb.index(netlist)

    def found(designator, excluded=False):
        component, pads = _pads_of(index, designator)
        return th.find_thermal_pads(index, component, pads, excluded)

    (pad,) = found("U5")
    assert pad.position == "centre" and pad.names == ["17"] and pad.name_hint
    assert pad.area_mm2 == pytest.approx(100 * 100 / MM ** 2)
    assert pad.ratio == pytest.approx(100 * 100 / 300)
    (tab,) = found("U1")
    assert tab.position == "tab" and tab.names == ["4"] and tab.ratio > 3
    (split,) = found("U4")  # four pieces of one name make one pad
    assert len(split.pieces) == 4 and split.area_mm2 == pytest.approx(4 * 45 * 45 / MM ** 2)
    assert split.name_hint
    dual = found("U6")
    assert sorted(pad.names[0] for pad in dual) == ["17", "18"]
    assert found("U2") == []  # equal pads (a BGA)
    assert found("U3") == []  # under 0.5 mm^2
    assert found("J1", excluded=True) == []


def test_vias_in_rotated_and_bottom_pads():
    """A 60 x 140 mil pad on a part turned 30 degrees on the bottom: two vias inside it, one
    inside its bounding box but outside the copper, one of another net, one beside it."""
    netlist = _netlist(BOARD)
    pads = qfn(ep=(60.0, 140.0), nets={"1": "+3V3"})
    pcb = Pcb().place("U5", 500, 500, pads, rotation=30.0, side="bottom")
    pcb.via("GND", "U5", 0, 0).via("GND", "U5", 0, 50).via("GND", "U5", 0, -50)
    # Inside the pad's axis-aligned box, outside the turned copper.
    corner = _transform("bottom", 500, 500, 30.0, 28, 68)
    outside = _transform("bottom", 500, 500, 30.0, 40, 75)
    assert abs(outside[0] - corner[0]) < 20
    pcb.via("GND", x=outside[0], y=outside[1])
    pcb.via("SIG1", "U5", 0, 20)  # another net, inside
    pcb.via("SIG1", "U5", 0, -20, layer_to=2)  # blind, top to L2: never meets the pad
    pcb.via("GND", "U5", 0, 100)  # beside: 30 mil past the pad's end
    pcb.pour("GND", 2, (0, 0, 1000, 1000))
    index = pcb.index(netlist)
    component, rows = _pads_of(index, "U5")
    (pad,) = th.find_thermal_pads(index, component, rows)
    th._audit_pad(index, pad, rows)
    assert len(pad.vias) == 4 and len(pad.wrong) == 1
    assert pad.beside == 2  # the one past the end and the one off the corner
    assert pad.reach == ["L2-GND"] and pad.layer == 32
    result = th.run_thermal_check(netlist, index=index, parts=["U5"], limit=1000)
    (wrong,) = _findings(result, "thermal_vias_wrong_net")
    assert wrong["severity"] == "warning" and wrong["wrong_nets"] == ["SIG1"]
    row = _row(result, "U5")["pcb"]["thermal_pads"][0]
    assert row["vias"] == 4 and row["wrong_net_vias"] == 1 and row["reaches"] == ["L2-GND"]
    assert row["drills"] == [{"drill_mil": 12.0, "count": 4}]
    assert row["layer"] == "Bottom Layer"


def _audit_board(**changes):
    netlist = _netlist(BOARD)
    pcb = Pcb()
    # U1, a regulator, with no via in its tab: a warning.
    pcb.place("U1", 0, 0, sot223(), rotation=90.0)
    # U5, a plain IC: its bare pad is info.
    pcb.place("U5", 2000, 0, qfn())
    # U4, a PHY: 2 vias in a 6.45 mm^2 pad (5 by the guideline), reaching nothing.
    pcb.place("U4", 4000, 0, qfn())
    pcb.via("GND", "U4", -20, 0).via("GND", "U4", 20, 0)
    # U6, an LED driver: 6 vias reaching an inner region.
    pcb.place("U6", 6000, 0, qfn())
    for dx in (-30, 0, 30):
        for dy in (-20, 20):
            pcb.via("GND", "U6", dx, dy)
    pcb.region("GND", 3, (5800, -200, 6200, 200))
    for designator, (x, y, rotation, side) in changes.items():
        pcb.place(designator, x, y, qfn(), rotation=rotation, side=side)
    return netlist, pcb


def test_thermal_pad_findings():
    netlist, pcb = _audit_board()
    result = th.run_thermal_check(netlist, index=pcb.index(netlist), limit=1000)
    by = {(f["check"], f["designators"][0]): f for f in result["findings"]}
    assert by[("thermal_pad_no_vias", "U1")]["severity"] == "warning"
    assert by[("thermal_pad_no_vias", "U1")]["position"] == "tab"
    assert by[("thermal_pad_no_vias", "U5")]["severity"] == "info"
    few = by[("thermal_pad_few_vias", "U4")]
    assert (few["vias"], few["guideline_vias"]) == (2, 5)
    assert by[("thermal_pad_unconnected", "U4")]["severity"] == "info"
    assert ("thermal_pad_few_vias", "U6") not in by and ("thermal_pad_unconnected", "U6") not in by
    assert _row(result, "U6")["pcb"]["thermal_pads"][0]["reaches"] == ["L3-SIG"]
    assert set(_row(result, "U5")["basis"]) == {"exposed_pad"}
    pads = result["summary"]["thermal_pads"]
    assert pads == {"pads": 4, "parts": 4, "exposed_pads": 3, "tabs": 1,
                    "via_counts": {"0": 2, "1-3": 1, "4-8": 1, "9+": 0}, "vias_total": 8,
                    "wrong_net_vias": 0, "reaching_plane_or_pour": 1}
    assert result["severity_counts"]["warning"] == 1
    # checks narrows the findings.
    narrowed = th.run_thermal_check(netlist, index=pcb.index(netlist),
                                    checks=["thermal_pad_few_vias"])
    assert [f["check"] for f in narrowed["findings"]] == ["thermal_pad_few_vias"]


def test_a_tab_in_its_own_pour_or_with_vias_beside_it_is_info():
    netlist, pcb = _audit_board()
    pcb.pour("+3V3", 1, (-300, -300, 300, 300))
    result = th.run_thermal_check(netlist, index=pcb.index(netlist), parts=["U1"])
    (finding,) = _findings(result, "thermal_pad_no_vias")
    assert finding["severity"] == "info" and "pour of its net" in finding["issue"]


def test_a_tab_without_a_net():
    netlist = _netlist(BOARD)
    index = Pcb().place("U1", 0, 0, sot223(tab_net=None)).place(
        "U5", 1000, 0, sot223(tab_net=None)).index(netlist)
    result = th.run_thermal_check(netlist, index=index, limit=1000)
    # The regulator's tab: no vias and no net. A part not known to dissipate: an
    # anchoring tab, left out.
    assert {f["check"] for f in _findings(result, ref="U1")} == {
        "thermal_pad_no_vias", "thermal_pad_unconnected"}
    assert "has no net on the PcbDoc" in _findings(result, "thermal_pad_unconnected")[0]["issue"]
    assert "U5" not in {row["designator"] for row in result["parts"]}


def powerpak(source="+12V_IN", gate="Q3_G", drain="+12V"):
    """An 8-pin PowerPAK-style FET: sources 1-3 and the gate 4 down one side, drains 5-8
    down the other (30 x 25 mil each); its drain paddle is a footprint fill (Pcb.fill)."""
    pads = [(str(n), source if n < 4 else gate, -100.0, (n - 2.5) * 50, 30.0, 25.0, 2)
            for n in range(1, 5)]
    return pads + [(str(n), drain, 100.0, (6.5 - n) * 50, 30.0, 25.0, 2) for n in range(5, 9)]


def test_a_footprint_fill_is_a_thermal_pad():
    """Q3's drain paddle is a fill (140 x 220 mil) the footprint owns, under pads 5-8, on a
    part turned 90 degrees on the bottom: three vias in it, one outside; a fill of a net
    the part does not have is not its pad."""
    netlist = _netlist(BOARD)
    pcb = Pcb().place("Q3", 1000, 1000, powerpak(), rotation=90.0, side="bottom")
    pcb.fill("Q3", "+12V", -20, -110, 120, 110).fill("Q3", "SIG9", -140, -110, -60, 110)
    for dy in (-50, 0, 50):
        pcb.via("+12V", "Q3", 40, dy)
    pcb.via("+12V", "Q3", 40, 140)  # 30 mil past the paddle's end: beside it
    pcb.region("+12V", 2, (0, 0, 2000, 2000))
    result = th.run_thermal_check(netlist, index=pcb.index(netlist), parts=["Q3"])
    (pad,) = _row(result, "Q3")["pcb"]["thermal_pads"]
    assert pad["drawn_as"] == "footprint fill" and pad["pads"] == ["5", "6", "7", "8"]
    assert pad["net"] == "+12V" and pad["layer"] == "Bottom Layer"
    assert pad["position"] == "tab"
    assert pad["area_mm2"] == pytest.approx(140 * 220 / MM ** 2, abs=0.05)
    assert pad["vias"] == 3 and pad["same_net_vias_beside"] == 1
    assert pad["reaches"] == ["L2-GND"]
    assert set(_row(result, "Q3")["basis"]) == {"power_path_transistor", "exposed_pad"}
    (few,) = _findings(result, "thermal_pad_few_vias")
    assert "a footprint fill under pads 5, 6, 7 and 1 more" in few["issue"]


def lga(ground="GND"):
    """A module's land pattern: 12 signal pads (20 x 20 mil) and 6 large pads (80 x 80 mil),
    three on ground, two on its input and one on its output."""
    pads = [(f"S{n}", "SIG1", -200.0 + n * 35, -200.0, 20.0, 20.0, 2) for n in range(12)]
    nets = [ground, ground, ground, "+12V", "+12V", "+5V"]
    return pads + [(f"P{n}", net, -150.0 + (n % 3) * 150, (n // 3) * 150 - 50, 80.0, 80.0, 2)
                   for n, net in enumerate(nets)]


def test_a_pad_array_is_audited_on_its_ground_pads():
    netlist = _netlist(BOARD)
    pcb = Pcb().place("U2", 0, 0, lga())
    pcb.via("GND", "U2", -160, -50).via("GND", "U2", -140, -50).via("GND", "U2", 0, -50)
    # Eight equal pads and a small marker pad: no pad array.
    pcb.place("U5", 2000, 0, [(str(n), "SIG1", (n % 4) * 100.0, (n // 4) * 150.0, 60.0, 35.0,
                               2) for n in range(1, 9)] + [("0", None, 150.0, 75.0, 15.0, 15.0, 1)])
    index = pcb.index(netlist)
    result = th.run_thermal_check(netlist, index=index, limit=1000)
    (pad,) = _row(result, "U2")["pcb"]["thermal_pads"]
    assert pad["pad_array"] == {"large_pads": 6, "audited": 3, "audited_net": "ground",
                                "not_audited": 3}
    assert pad["pads"] == ["P0", "P1", "P2"] and pad["net"] == "GND" and pad["vias"] == 3
    assert pad["area_mm2"] == pytest.approx(3 * 80 * 80 / MM ** 2, abs=0.05)
    (few,) = _findings(result, "thermal_pad_few_vias")
    assert few["issue"].startswith("3 via(s) in U2's pad array (P0, P1, P2,")
    assert result["summary"]["thermal_pads"]["pad_arrays"] == 1
    assert "U5" not in {row["designator"] for row in result["parts"]}


@pytest.mark.parametrize("poured, shelved, why", [(False, True, "shelved"),
                                                  (False, False, "not poured")])
def test_vias_landing_in_a_pour_without_copper(poured, shelved, why):
    """U4's vias land in a ground pour on L3 that holds no copper in the file: not
    'unconnected', listed apart, with a board note."""
    netlist, pcb = _audit_board()
    pcb.pour("GND", 3, (3700, -300, 4300, 300), poured=poured, shelved=shelved)
    result = th.run_thermal_check(netlist, index=pcb.index(netlist), limit=1000)
    assert not _findings(result, "thermal_pad_unconnected", "U4")
    pad = _row(result, "U4")["pcb"]["thermal_pads"][0]
    assert pad["reaches_pours_without_copper"] == [f"L3-SIG ({why})"]
    assert pad["reaches_layers"] == 0
    assert result["summary"]["thermal_pads"]["landing_in_pours_without_copper"] == 1
    assert any(note.startswith("Pours holding no copper in the file: 1 ") and why in note
               for note in result["notes"])
    # Poured, the same pour is reached copper.
    poured_result = th.run_thermal_check(netlist, index=_audit_board()[1].pour(
        "GND", 3, (3700, -300, 4300, 300)).index(netlist), limit=1000)
    assert _row(poured_result, "U4")["pcb"]["thermal_pads"][0]["reaches"] == ["L3-SIG"]


def test_a_t_prefixed_transistor_keeps_its_tab():
    """T and TR name transistors too (DIN style): a DPAK tab is audited, a transformer's
    pads are not."""
    board = BOARD + [
        ("T1", {"1": ("G", "T1_G"), "2": ("S", "GND"), "3": ("D", "LOAD1")},
         {"description": "N-channel MOSFET, DPAK"}),
        ("T2", {"1": ("1", "AC1"), "2": ("2", "AC2"), "3": ("3", "AC3"), "4": ("4", "AC4")},
         {"description": "Transformer"})]
    netlist = _netlist(board)
    dpak = [("1", "T1_G", -90.0, -150.0, 40.0, 60.0, 2), ("3", "GND", 90.0, -150.0, 40.0, 60.0, 2),
            ("2", "LOAD1", 0.0, 80.0, 240.0, 220.0, 2)]
    pcb = Pcb().place("T1", 0, 0, dpak).place("T2", 2000, 0, dpak)
    result = th.run_thermal_check(netlist, index=pcb.index(netlist), limit=1000)
    (pad,) = _row(result, "T1")["pcb"]["thermal_pads"]
    assert pad["position"] == "tab" and pad["pads"] == ["2"]
    assert "T2" not in {row["designator"] for row in result["parts"]}


def test_a_part_drawing_from_a_converters_input_bus_is_not_in_its_stage():
    """Q3 feeds +12V, the buck U2's input: one stage. Q5 draws from the same +12V (a
    switch for U7): beside U2 it is a hot neighbour, not part of U2's stage."""
    board = BOARD + [
        ("Q5", {"1": ("G", "Q5_G"), "2": ("S", "+12V"), "3": ("D", "+12V_SW")},
         {"description": "P-channel MOSFET"}),
        res("R15", "Q5_G", "GND", "100k"),
        ("U7", {"1": ("IN", "+12V_SW", "Power"), "2": ("OUT", "+5V_AUX", "Power"),
                "3": ("GND", "GND", "Power")}, {"description": "LDO regulator, 5 V"})]
    netlist = _netlist(board)
    pcb = Pcb().place("U2", 0, 0, two_pin("+12V", "SW1"))
    pcb.place("Q3", 0, 150, sot23(["Q3_G", "+12V_IN", "+12V"]))
    pcb.place("Q5", 0, -150, sot23(["Q5_G", "+12V", "+12V_SW"]))
    pcb.place("U7", 3000, 0, two_pin("+12V_SW", "+5V_AUX"))
    result = th.run_thermal_check(netlist, index=pcb.index(netlist), limit=1000)
    assert "U2" in _row(result, "Q3")["power_stage"]
    assert "U2" not in _row(result, "Q5")["power_stage"]
    assert "U7" in _row(result, "Q5")["power_stage"]
    pairs = [f["designators"] for f in _findings(result, "hot_neighbours")]
    assert ["Q5", "U2"] in pairs and ["Q3", "U2"] not in pairs


def test_a_sensitive_part_sharing_only_a_supply_is_judged():
    """Y3, an oscillator powered from +3V3 (U1's output) but clocking nothing of U1's, sits
    beside U1: reported. Y1, the PHY's crystal, sits beside the PHY by design. A part named
    in part= is no heat source: C3 beside U2 is a sensitive part only."""
    board = BOARD + [("Y3", {"1": ("VDD", "+3V3", "Power"), "2": ("GND", "GND", "Power"),
                             "3": ("OUT", "CLK9"), "4": ("EN", "+3V3")},
                      {"description": "Oscillator 25 MHz"})]
    netlist = _netlist(board)
    pcb = Pcb().place("U1", 0, 0, two_pin("+5V", "+3V3"))
    pcb.place("Y3", 150, 0, two_pin("+3V3", "CLK9"))
    pcb.place("U4", 0, 3000, two_pin("+3V3", "GND")).place("Y1", 150, 3000,
                                                            two_pin("XTAL_I", "XTAL_O"))
    pcb.place("U2", 0, 6000, two_pin("+12V", "SW1")).place("C3", 150, 6000,
                                                            two_pin("+12V", "GND"))
    index = pcb.index(netlist)
    result = th.run_thermal_check(netlist, index=index, limit=1000)
    sensitive = {next(ref for ref in f["designators"] if ref not in f["hot_parts"])
                 for f in _findings(result, "sensitive_near_hot")}
    assert sensitive == {"C3", "Y3"}
    named = th.run_thermal_check(netlist, index=index, parts=["C3", "U2"], limit=1000)
    assert not _findings(named, "hot_neighbours")
    assert [f["designators"] for f in _findings(named, "sensitive_near_hot")] == [["C3", "U2"]]


def test_an_oscillator_on_a_processors_supply_is_judged():
    """Y4 takes +3V3_EXT (a connector's rail, outside the power tree) on its supply pin,
    as the processor U10 beside it does: that is no wiring to U10."""
    board = [
        ("J5", {"1": ("3V3", "+3V3_EXT"), "2": ("GND", "GND")}, {"description": "Header"}),
        ("U10", {"1": ("VDD", "+3V3_EXT", "Power"), "2": ("GND", "GND", "Power"),
                 **{str(n): (f"PA{n}", f"GPIO{n}") for n in range(3, 9)}},
         {"description": "ARM Cortex-M4 32-bit microcontroller"}),
        ("Y4", {"1": ("VDD", "+3V3_EXT", "Power"), "2": ("GND", "GND", "Power"),
                "3": ("OUT", "CLK_A")}, {"description": "Oscillator 25 MHz"})]
    netlist = _netlist(board)
    pcb = Pcb().place("U10", 0, 0, two_pin("+3V3_EXT", "GND"))
    pcb.place("Y4", 150, 0, two_pin("+3V3_EXT", "CLK_A"))
    result = th.run_thermal_check(netlist, index=pcb.index(netlist), limit=1000)
    (finding,) = _findings(result, "sensitive_near_hot")
    assert finding["designators"] == ["U10", "Y4"] and finding["hot_parts"] == ["U10"]


def _neighbours(gap_mil, side="top", max_mil=None):
    """U1 (an LDO) and U2 (a buck) gap_mil apart edge to edge; Q1 and Q2 (U3's FETs) next
    to U3; Y1 (the PHY's crystal) by the PHY; Y2 and C3 beside U2."""
    netlist = _netlist(BOARD)
    pcb = Pcb()
    pcb.place("U1", 0, 0, two_pin("+5V", "+3V3"))  # pads span x -55 .. 55
    pcb.place("U2", 110 + gap_mil, 0, two_pin("+12V", "SW1"), side=side)
    pcb.place("U3", 0, 3000, two_pin("+12V", "SW2"))
    pcb.place("Q1", 120, 3000, sot23(["HG1", "SW2", "+12V"]))
    pcb.place("Q2", 240, 3000, sot23(["LG1", "GND", "SW2"]))
    pcb.place("U4", 0, 6000, two_pin("+3V3", "GND"))
    pcb.place("Y1", 120, 6000, two_pin("XTAL_I", "XTAL_O"))
    pcb.place("Y2", 110 + gap_mil, 150, two_pin("OSC_A", "OSC_B"))
    pcb.place("C3", 110 + gap_mil + 160, 0, two_pin("+12V", "GND"))
    kwargs = {"max_distance_mil": max_mil} if max_mil else {}
    return th.run_thermal_check(netlist, index=pcb.index(netlist), limit=1000, **kwargs)


def test_hot_neighbours_at_and_around_the_distance():
    result = _neighbours(150.0, max_mil=150.0)
    (pair,) = _findings(result, "hot_neighbours", "U1")
    assert pair["designators"] == ["U1", "U2"]
    assert pair["distance_mil"] == pytest.approx(150.0) and pair["same_side"] is True
    assert "same side" in pair["issue"]
    assert _row(result, "U1")["pcb"]["hot_neighbours"] == [
        {"designator": "U2", "distance_mil": 150.0, "same_side": True}]
    assert not _findings(_neighbours(150.5, max_mil=150.0), "hot_neighbours", "U1")
    opposite = _neighbours(100.0, side="bottom")
    (pair,) = _findings(opposite, "hot_neighbours", "U1")
    assert pair["same_side"] is False and "opposite sides" in pair["issue"]
    # A converter's own FETs sit beside it by design: no pair.
    assert not _findings(result, "hot_neighbours", "Q1")
    assert result["summary"]["neighbour_pairs"] == 1


def test_sensitive_parts_near_hot_parts():
    result = _neighbours(100.0)
    found = {next(ref for ref in f["designators"] if ref not in f["hot_parts"]): f
             for f in _findings(result, "sensitive_near_hot")}
    # The PHY's own crystal sits beside it by design; the loose one and the
    # electrolytic do not.
    assert set(found) == {"C3", "Y2"}
    assert found["C3"]["sensitivity"] == "electrolytic_capacitor"
    assert found["Y2"]["sensitivity"] == "crystal_oscillator"
    assert "U2" in found["C3"]["hot_parts"]


def test_parts_named_but_not_placed_are_noted():
    netlist = _netlist(BOARD)
    index = Pcb().place("U5", 0, 0, qfn()).index(netlist)
    result = th.run_thermal_check(netlist, index=index, parts=["U1"])
    assert any(note.startswith("Not on the PcbDoc") and "U1" in note
               for note in result["notes"])


def test_pad_area_shapes():
    assert th.pad_area_mil2({"width": 10, "height": 20, "shape": 2}) == 200
    assert th.pad_area_mil2({"width": 10, "height": 10, "shape": 1}) == pytest.approx(
        math.pi * 25)
    assert th.pad_area_mil2({"width": 10, "height": 30, "shape": 1}) == pytest.approx(
        math.pi * 25 + 200)
    assert th.pad_area_mil2({"width": 20, "height": 20, "shape": 9, "corner_radius": 5}) == \
        pytest.approx(400 - (4 - math.pi) * 25)
    assert th.pad_area_mil2({"width": 20, "height": 20, "shape": 3}) == pytest.approx(350)


# ---------- paging ----------

def test_paging_and_rendering():
    netlist = _netlist(BOARD)
    prepared = th.prepare_thermal(netlist)
    first = th.render_thermal(prepared, offset=0, limit=5)
    assert len(first["parts"]) == 5 and first["next_offset"] == 5
    assert first["summary"]["hot_parts"] == 12
    last = th.render_thermal(prepared, offset=10, limit=5)
    assert len(last["parts"]) == 2 and not last["has_more"]
    assert "estimate" in first and "guidance" in first


# ---------- tool ----------

@pytest.fixture
def board(monkeypatch, tmp_path: Path):
    netlist, pcb = _audit_board()
    pcb.place("U2", 0, 250, two_pin("+12V", "SW1"))  # 125 mil from U1
    index = pcb.index(netlist)
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


def test_check_thermal_tool(board):
    result = json.loads(main_mod.check_thermal(load="U1=0.5", theta="U1=40", ta_max=70))
    assert result["pcb_doc"] == "Board.PcbDoc" and result["units"] == "mm"
    assert result["assumptions"]["max_distance_mm"] == pytest.approx(5.0, abs=1e-3)
    row = next(row for row in result["parts"] if row["designator"] == "U1")
    assert row["power"]["watts"] == pytest.approx(0.85)
    assert row["tj"]["tj_worst_c"] == pytest.approx(104.0)
    pad = row["pcb"]["thermal_pads"][0]
    assert pad["size_mm"] == [pytest.approx(80 * 0.0254), pytest.approx(130 * 0.0254)]
    (pair,) = [f for f in result["findings"] if f["check"] == "hot_neighbours"]
    assert pair["designators"] == ["U1", "U2"] and "distance_mm" in pair
    mil = json.loads(main_mod.check_thermal(units="mil", max_distance=100))
    assert mil["assumptions"]["max_distance_mil"] == pytest.approx(100.0)
    assert not [f for f in mil["findings"] if f["check"] == "hot_neighbours"]
    errors = {
        "invalid_units": {"units": "inch"}, "invalid_max_distance": {"max_distance": -1},
        "invalid_ta": {"ta": 400}, "invalid_ta_max": {"ta": 50, "ta_max": 40},
        "invalid_load": {"load": "U1"}, "invalid_theta": {"theta": "45"},
        "invalid_eta": {"eta": "U2=2"}, "invalid_tj_max": {"tj_max": "U1=hot"},
        "invalid_power": {"power": "U1=-2"}, "invalid_iq": {"iq": "U1=1V"},
        "invalid_rails": {"rails": "VIN=x"}, "invalid_vref": {"vref": "U2=20"},
        "invalid_checks": {"checks": ["hot"]}, "part_not_found": {"part": "U99"},
    }
    for code, kwargs in errors.items():
        assert json.loads(main_mod.check_thermal(**kwargs))["error"] == code, code


def test_check_thermal_without_a_board(board, monkeypatch):
    monkeypatch.setitem(main_mod._project, "pcb_doc_paths", [])
    result = json.loads(main_mod.check_thermal())
    assert result["not_checked"]["thermal_pad_no_vias"] == "the project lists no PcbDoc"
    assert "pcb_doc" not in result and "thermal_pads" not in result["summary"]
    assert result["summary"]["hot_parts"] == 12


def test_check_thermal_pages_and_shrinks(board, monkeypatch):
    first = json.loads(main_mod.check_thermal(offset=0))
    assert first["parts_total"] == 13 and "page_limited_to" not in first
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 9000)
    small = main_mod.check_thermal()
    result = json.loads(small)
    assert len(small) <= 9000 and result["page_limited_to"] < 100
    assert result["next_offset"] == result["page_limited_to"]
    following = json.loads(main_mod.check_thermal(offset=result["next_offset"]))
    assert following["offset"] == result["next_offset"]


def test_check_thermal_echoes_inputs_without_float_noise(board):
    result = json.loads(main_mod.check_thermal(load="U1=700mA", iq="U1=100uA"))
    row = next(row for row in result["parts"] if row["designator"] == "U1")
    assert row["power"]["inputs"]["I_out"]["amps"] == 0.7
    assert row["power"]["iq"]["amps"] == 0.0001


def test_check_thermal_needs_a_project(monkeypatch):
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.check_thermal())["error"] == "no_project"
    assert "next_offset" in main_mod.check_thermal.__doc__


def test_instructions_name_the_tool_after_the_cut():
    assert main_mod.SERVER_INSTRUCTIONS.index("check_thermal") > 4096
    assert "check_thermal" in main_mod.NETLIST_TOOLS
