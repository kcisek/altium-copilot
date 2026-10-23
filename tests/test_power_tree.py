"""Power tree extraction and power-path checks (services/power_tree, get_power_tree)."""

import asyncio
import json
from pathlib import Path

import pytest

import server.main as main_mod
import server.services.power_tree as pt
from server.parsers.prj_pcb import VariantDefinition, VariantState
from server.services.bom import VariantPlan
from tests.test_pull_resistors import _netlist

# (refdes, {pin: (pin name, net) or (pin name, net, electrical type)}, info)
BOARD = [
    # A DC jack through a fuse and a series Schottky to VIN, with a TVS and bulk capacitor.
    ("J1", {"1": ("1", "VIN_RAW"), "2": ("2", "GND")}, {"description": "DC power jack"}),
    ("F1", {"1": ("1", "VIN_RAW"), "2": ("2", "FUSED_NODE")}, {"description": "Fuse 2A"}),
    ("D1", {"1": ("A", "FUSED_NODE"), "2": ("K", "VIN")}, {"description": "Schottky diode"}),
    ("D2", {"1": ("K", "VIN"), "2": ("A", "GND")}, {"description": "TVS diode 24 V"}),
    ("C1", {"1": ("1", "VIN"), "2": ("2", "GND")}, {"value": "47uF"}),
    # A pi filter (C1, L1, C2) in front of the buck.
    ("L1", {"1": ("1", "VIN"), "2": ("2", "VIN_BUCK")}, {"description": "Ferrite bead"}),
    ("C2", {"1": ("1", "VIN_BUCK"), "2": ("2", "GND")}, {"value": "10uF"}),
    ("U1", {"1": ("VIN", "VIN_BUCK", "Power"), "2": ("SW", "SW1"), "3": ("BST", "BST1"),
            "4": ("FB", "FB1", "Input"), "5": ("EN", "EN1", "Input"),
            "6": ("PG", "PG1", "OpenCollector"), "7": ("GND", "GND", "Power")},
     {"description": "Synchronous step-down converter, 2 A"}),
    ("C3", {"1": ("1", "BST1"), "2": ("2", "SW1")}, {"value": "100nF"}),
    ("L2", {"1": ("1", "SW1"), "2": ("2", "3V3")}, {"description": "Inductor 4.7uH"}),
    ("C4", {"1": ("1", "3V3"), "2": ("2", "GND")}, {"value": "22uF"}),
    # Feedback divider: 100k / 22.1k sets 3.3 V with a 0.6 V reference.
    ("R1", {"1": ("1", "3V3"), "2": ("2", "FB1")}, {"value": "100k"}),
    ("R2", {"1": ("1", "FB1"), "2": ("2", "GND")}, {"value": "22.1k"}),
    # EN: an undervoltage divider from the buck's input.
    ("R3", {"1": ("1", "VIN_BUCK"), "2": ("2", "EN1")}, {"value": "100k"}),
    ("R4", {"1": ("1", "EN1"), "2": ("2", "GND")}, {"value": "20k"}),
    # PG pulled up to 3V3 and enabling the LDO: a sequence.
    ("R5", {"1": ("1", "PG1"), "2": ("2", "3V3")}, {"value": "10k"}),
    ("U2", {"1": ("IN", "3V3", "Power"), "2": ("OUT", "1V8", "Power"),
            "3": ("EN", "PG1", "Input"), "4": ("GND", "GND", "Power"),
            "5": ("PG", "PG2", "OpenCollector")},
     {"description": "LDO regulator, 1.8 V fixed"}),
    ("R6", {"1": ("1", "PG2"), "2": ("2", "1V8")}, {"value": "10k"}),
    ("C5", {"1": ("1", "1V8"), "2": ("2", "GND")}, {"value": "1uF"}),
    ("C6", {"1": ("1", "3V3"), "2": ("2", "GND")}, {"value": "100nF"}),
    # Loads.
    ("U3", {"1": ("VDD", "3V3", "Power"), "2": ("VDDA", "1V8", "Power"),
            "3": ("GND", "GND", "Power"), "4": ("PA0", "MCU_GPIO", "IO")},
     {"description": "Microcontroller"}),
    ("U4", {"1": ("VDD", "3V3", "Power"), "2": ("GND", "GND", "Power")},
     {"description": "Temperature sensor"}),
]


def _run(board=BOARD, plan=None, **kwargs):
    return pt.run_power_tree_check(_netlist(board), plan, **kwargs)


def _with(*extra, drop=()):
    return [part for part in BOARD if part[0] not in drop] + list(extra)


def _conv(result, ref):
    return next(row for row in result["converters"] if row["designator"] == ref)


def _rail(result, net):
    return next(row for row in result["rails"] if row["rail"] == net)


def _findings(result, check=None, part=None):
    return [f for f in result["findings"] if (check is None or f["check"] == check)
            and (part is None or part in f.get("designators", ()))]


# ---------- recognising converters ----------

@pytest.mark.parametrize("text, kind", [
    ("Synchronous step-down converter, 2 A", "buck"),
    ("PowerSoC buck with integrated inductor", "buck"),
    ("Boost converter 1 MHz", "boost"),
    ("4-switch buck-boost controller", "buck_boost"),
    ("SEPIC/boost controller", "sepic"),
    ("Isolated flyback controller", "flyback"),
    ("Inverting DC/DC converter, -5 V", "inverting"),
    ("Switched-capacitor voltage inverter", "charge_pump"),
    ("Low dropout regulator 500 mA", "ldo"),
    ("500 mA VLDO linear regulator, boost converter disabled", "ldo"),
    ("Positive voltage regulator 1 A", "ldo"),
    ("Power management IC with three bucks and two LDOs", "pmic"),
    ("Current-limited load switch", "load_switch"),
    ("eFuse with adjustable current limit", "efuse"),
    ("Hot-swap controller", "hot_swap"),
    ("Ideal diode controller", "ideal_diode"),
    ("Li-ion charger, 1 A", "charger"),
    ("Precision shunt voltage reference 2.5 V", "reference"),
    ("Window comparator with internal reference for overvoltage detection", "supervisor"),
    ("Voltage supervisor with manual reset", "supervisor"),
    ("DC/DC module 5 W", "switching"),
    ("Microcontroller with LDO", None),
    ("Operational amplifier, rail-to-rail", None),
    ("LED driver, boost", None),
    ("Temperature sensor", None),
    ("Power module, 5 V, 3 A", "switching"),
    ("PSU 24 V to 12 V", "switching"),
    ("Synchronous buck MOSFET driver", None),
    ("IGBT power module", None),
    ("Buck controller with integrated MOSFET drivers", "buck"),
    # A supervisor, reference or load switch word after the topology is a feature.
    ("Low dropout voltage regulator with reset and watchdog", "ldo"),
    ("Step-down converter with power-on reset", "buck"),
    ("LDO with precision reference", "ldo"),
    ("Buck converter with integrated load switch", "buck"),
    ("PMIC with watchdog", "pmic"),
    ("Reset IC with LDO", "supervisor"),
])
def test_classify_text(text, kind):
    assert pt.classify_text(text)[0] == kind


@pytest.mark.parametrize("text, kind", [
    ("TPS62999Z", "buck"), ("TPS61999Z", "boost"), ("AMS1117-ZZ", "ldo"),
    ("TPS22918", "load_switch"), ("TL431", "reference"), ("TPS3839", "supervisor"),
    ("STM32F405", None),
])
def test_classify_part_number_families(text, kind):
    assert pt.classify_mpn(text)[0] == kind


@pytest.mark.parametrize("name, kind", [
    ("VIN", "in"), ("PVIN2", "in"), ("+VIN", "in"), ("IN", "in"), ("VOUT", "out"),
    ("VOUT+", "out"), ("OUT", "out"), ("VOUT0CFG", None), ("VIN_SNS", None),
    ("SW", "sw"), ("LX1", "sw"), ("PH", "sw"), ("FB", "fb"), ("ADJ", "fb"), ("VFB", "fb"),
    ("EN", "en"), ("EN2", "en"), ("S\\H\\D\\N\\", "en"), ("RUN", "en"), ("ON", "en"),
    ("PG", "pg"), ("PGOOD", "pg"), ("POK", "pg"), ("PWRGD", "pg"), ("BST", "bst"),
    ("BOOT", "bst"), ("SS/TR", "ss"), ("COMP", "comp"), ("ILIM", "ilim"),
    ("MODE/SYNC", "mode"), ("HG", "gate"), ("GND", "gnd"), ("-VOUT", "return"),
    ("VCC", "bias"), ("1", None),
    # Not power-good: a P-gate drive, a program pin; an Enpirion part's pre-bias select and
    # internal AVIN regulator output.
    ("PGATE", "gate"), ("PG_DRV", "gate"), ("PGM", None), ("PG_1V8", "pg"), ("EN_PB", "mode"),
    ("AVINO", "bias"), ("AVIN", "in"),
])
def test_pin_kinds(name, kind):
    assert pt.pin_kind(name) == kind


def test_a_part_with_regulator_pins_and_no_words_is_a_converter():
    plain = ("U9", {"1": ("VIN", "VIN", "Power"), "2": ("VOUT", "5V0", "Power"),
                    "3": ("EN", "VIN", "Input"), "4": ("GND", "GND", "Power")}, {})
    conv = _conv(_run(_with(plain)), "U9")
    assert conv["type"] == "linear" and "pin names" in conv["basis"][0]
    assert conv["outputs"][0]["rail"] == "5V0"
    # The same pins on a part whose words name another function are not a converter.
    mcu = ("U9", plain[1], {"description": "Microcontroller"})
    assert not any(row["designator"] == "U9" for row in _run(_with(mcu))["converters"])


# ---------- the tree ----------

def test_converter_edges_rails_and_loads():
    result = _run()
    buck, ldo = _conv(result, "U1"), _conv(result, "U2")
    assert buck["type"] == "buck" and buck["switching"] is True
    assert buck["inputs"] == [{"rail": "VIN_BUCK", "voltage": None}]
    assert buck["outputs"][0]["rail"] == "3V3" and "L2" in buck["outputs"][0]["via"]
    assert ldo["type"] == "ldo" and ldo["outputs"][0]["rail"] == "1V8"
    assert result["overview"]["converters_by_type"] == {"buck": 1, "ldo": 1}
    rail = _rail(result, "3V3")
    assert rail["supplied"] is True and rail["voltage"] == 3.3
    assert {"type": "converter", "part": "U1"} in rail["sources"]
    # Loads: parts with a supply pin, never resistors or capacitors; converters count.
    assert set(rail["loads"]) == {"U2", "U3", "U4"} and rail["load_count"] == 3
    assert _rail(result, "1V8")["loads"] == ["U3"]
    assert rail["capacitors"] == 2 and rail["capacitance_uF"] == 22.1


def test_edges_through_series_parts():
    result = pt.build_power_tree(_netlist(BOARD))
    edges = {(edge["part"], edge["type"]): edge for edge in result["edges"]}
    assert edges[("F1", "fuse")]["from"] == "VIN_RAW" and edges[("F1", "fuse")]["to"] == "FUSED_NODE"
    assert edges[("D1", "schottky")]["from"] == "FUSED_NODE"  # anode to cathode
    assert edges[("L1", "ferrite")]["from"] == "VIN" and edges[("L1", "ferrite")]["directed"]
    assert edges[("U1", "buck")]["converter"] is True
    extra = _with(
        ("R9", {"1": ("1", "3V3"), "2": ("2", "3V3_A")}, {"value": "0R"}),
        ("U8", {"1": ("VDD", "3V3_A", "Power"), "2": ("GND", "GND", "Power")}, {}),
        ("Q1", {"1": ("S", "3V3"), "2": ("G", "SW_EN"), "3": ("D", "3V3_SW")},
         {"description": "P-channel MOSFET"}),
        ("U7", {"1": ("VDD", "3V3_SW", "Power"), "2": ("GND", "GND", "Power")}, {}),
    )
    data = pt.build_power_tree(_netlist(extra))
    kinds = {edge["part"]: edge for edge in data["edges"]}
    assert kinds["R9"]["type"] == "0_ohm" and kinds["Q1"]["type"] == "p_fet"
    assert kinds["Q1"]["from"] == "3V3" and kinds["Q1"]["to"] == "3V3_SW"
    rails = {row["rail"]: row for row in data["rails"]}
    assert rails["3V3_A"]["supplied"] and rails["3V3_SW"]["supplied"]


def test_unnamed_power_path_nets_are_rails():
    """FUSED_NODE carries power from the fuse to the diode: a power-path net, not a signal."""
    rail = _rail(_run(), "FUSED_NODE")
    assert rail["supplied"] and "power path" in rail["origin"]


def test_sources_and_input_protection():
    result = _run()
    (row,) = result["inputs"]
    assert row["source"] == "J1" and row["exposure"] == "external" and row["power_only"]
    # The chain runs on through the ferrite to the buck's input, where it stops.
    assert row["path"] == ["VIN_RAW", "FUSED_NODE", "VIN", "VIN_BUCK"]
    assert row["fuse"] == ["F1 (fuse)"] and row["reverse_polarity"] == ["D1 (schottky)"]
    assert row["filter"] == ["L1 (ferrite)"] and row["feeds_converters"] == ["U1"]
    assert row["tvs"] == ["D2"] and row["bulk"] == {"count": 2, "total_uF": 57.0,
                                                    "largest_uF": 47.0}
    for check in ("no_input_fuse", "no_reverse_protection", "no_input_tvs"):
        assert not _findings(result, check)
    bare = _run(_with(drop=("F1", "D1", "D2")) + [
        ("R7", {"1": ("1", "VIN_RAW"), "2": ("2", "VIN")}, {"value": "0R"})])
    assert {f["check"] for f in _findings(bare, part="J1")} == {
        "no_input_fuse", "no_reverse_protection", "no_input_tvs"}
    assert all(f["severity"] == "info" for f in bare["findings"] if "input" in f["check"])


def test_a_p_fet_in_the_path_and_a_crowbar_count_as_reverse_protection():
    pfet = _run(_with(("Q1", {"1": ("D", "FUSED_NODE"), "2": ("G", "GND"), "3": ("S", "VIN")},
                      {"description": "P-channel MOSFET 30 V"}), drop=("D1",)))
    assert "Q1 (p fet)" in pfet["inputs"][0]["reverse_polarity"][0]
    crowbar = _run(_with(("R7", {"1": ("1", "FUSED_NODE"), "2": ("2", "VIN")}, {"value": "0R"}),
                         drop=("D1",)))
    assert "crowbar" in crowbar["inputs"][0]["reverse_polarity"][0]
    assert not _findings(crowbar, "no_reverse_protection")


def test_usb_vbus_and_batteries():
    usb = [("J1", {"1": ("VBUS", "VBUS"), "2": ("D-", "USB_DM"), "3": ("D+", "USB_DP"),
                   "5": ("GND", "GND")}, {"description": "USB micro-B receptacle"}),
           ("U4", {"1": ("VDD", "VBUS", "Power"), "2": ("GND", "GND", "Power")}, {}),
           ("BT1", {"1": ("+", "VBAT"), "2": ("-", "GND")}, {"description": "Coin cell holder"}),
           ("U5", {"1": ("VBAT", "VBAT", "Power"), "2": ("GND", "GND", "Power")}, {})]
    result = _run(usb)
    rows = {row["source"]: row for row in result["inputs"]}
    assert rows["J1"]["usb"] and rows["BT1"]["kind"] == "battery"
    # USB: no fuse or reverse-polarity advice; batteries get none.
    assert {f["check"] for f in result["findings"]} <= {"no_input_tvs"}
    assert _rail(result, "VBAT")["supplied"]


def test_a_connector_fed_from_the_board_is_an_outlet():
    extra = _with(("J2", {"1": ("1", "3V3"), "2": ("2", "GND")}, {"description": "Header 1x2"}))
    data = pt.build_power_tree(_netlist(extra))
    roles = {source["part"]: source.get("role") for source in data["sources"]
             if source["type"] == "connector"}
    assert roles == {"J1": "input", "J2": "outlet"}
    assert [row["source"] for row in data["inputs"]] == ["J1"]


# ---------- per-converter facts ----------

def test_enable_states():
    result = _run()
    (en,) = _conv(result, "U1")["enable"]
    assert en["state"] == "divider" and en["from"] == "VIN_BUCK"
    assert en["ratio"] == pytest.approx(20 / 120, abs=1e-3)
    (en2,) = _conv(result, "U2")["enable"]
    assert en2["state"] == "sequenced" and en2["by"] == ["U1.6 (PG)"]
    tied = _run(_with(("U2", {"1": ("IN", "3V3", "Power"), "2": ("OUT", "1V8", "Power"),
                              "3": ("EN", "3V3", "Input"), "4": ("GND", "GND")},
                       {"description": "LDO regulator"}), drop=("U2",)))
    (en3,) = _conv(tied, "U2")["enable"]
    assert en3["state"] == "tied" and en3["to_own_input"] is True
    assert not _findings(tied, "enable_tied_note")
    noted = _run(_with(("U2", {"1": ("IN", "3V3", "Power"), "2": ("OUT", "1V8", "Power"),
                               "3": ("EN", "3V3", "Input"), "4": ("GND", "GND")},
                        {"description": "LDO regulator"}), drop=("U2",)),
                 checks=["enable_tied_note"])
    assert _findings(noted, "enable_tied_note", "U2")


def test_enable_driven_by_a_gpio_pulled_and_floating():
    gpio = _run(_with(("U2", {"1": ("IN", "3V3", "Power"), "2": ("OUT", "1V8", "Power"),
                              "3": ("EN", "MCU_GPIO", "Input"), "4": ("GND", "GND")},
                       {"description": "LDO regulator"}), drop=("U2",)))
    (en,) = _conv(gpio, "U2")["enable"]
    assert en["state"] == "gpio" and en["by"] == ["U3.4 (PA0)"]
    pulled = _run(_with(("U2", {"1": ("IN", "3V3", "Power"), "2": ("OUT", "1V8", "Power"),
                                "3": ("EN", "LDO_EN", "Input"), "4": ("GND", "GND")},
                         {"description": "LDO regulator"}),
                        ("R8", {"1": ("1", "LDO_EN"), "2": ("2", "GND")}, {"value": "100k"}),
                        drop=("U2",)))
    assert _conv(pulled, "U2")["enable"][0]["state"] == "pulled_down"
    floating = _run(_with(("U2", {"1": ("IN", "3V3", "Power"), "2": ("OUT", "1V8", "Power"),
                                  "3": ("EN", "LDO_EN", "Input"), "4": ("GND", "GND")},
                           {"description": "LDO regulator"}), drop=("U2",)))
    assert _conv(floating, "U2")["enable"][0]["state"] == "floating"
    (finding,) = _findings(floating, "enable_floating")
    assert finding["severity"] == "warning" and finding["designators"] == ["U2"]


def test_an_unconnected_enable_pin_is_floating():
    ldo = ("U2", {"1": ("IN", "3V3", "Power"), "2": ("OUT", "1V8", "Power"),
                  "4": ("GND", "GND")},
           {"description": "LDO regulator",
            "pin_names": {"1": "IN", "2": "OUT", "3": "EN", "4": "GND"}})
    result = _run(_with(ldo, drop=("U2",)))
    assert _conv(result, "U2")["enable"][0]["state"] == "unconnected"
    assert "is not connected" in _findings(result, "enable_floating")[0]["issue"]


def test_power_good_used_and_unused():
    result = _run()
    (pg1,) = _conv(result, "U1")["power_good"]
    assert pg1["state"] == "used" and pg1["pulled_up_to"] == ["3V3"]
    assert pg1["used_by"] == [{"role": "enable", "pin": "U2.3 (EN)"}]
    (pg2,) = _conv(result, "U2")["power_good"]
    assert pg2["state"] == "unused" and pg2["pulled_up_to"] == ["1V8"]
    (finding,) = _findings(result, "pgood_unused")
    assert finding["designators"] == ["U2"] and finding["severity"] == "info"


def test_sequencing_chain():
    result = _run()
    (chain,) = result["sequencing"]
    assert chain["parts"] == ["U1", "U2"]
    assert chain["links"] == [{"from": "U1", "to": "U2", "how": "PG -> EN"}]
    (finding,) = _findings(result, "sequencing")
    assert "U1 -> U2" in finding["issue"]


def test_feedback_divider_implied_and_given_vref():
    result = _run()
    (fb,) = _conv(result, "U1")["feedback"]
    assert fb["state"] == "divider" and fb["top"] == ["R1"] and fb["bottom"] == ["R2"]
    assert fb["rtop"] == "100k" and fb["rbot"] == "22.1k"
    assert fb["vout_over_vref"] == pytest.approx(1 + 100 / 22.1, abs=1e-3)
    assert fb["implied_vref"] == pytest.approx(0.597, abs=1e-3)
    assert fb["near_common_vref"] == 0.6
    (finding,) = _findings(result, "fb_divider_mismatch")
    assert finding["severity"] == "info" and "close to the common 0.6 V" in finding["issue"]
    match = _run(vref={"u1": 0.6})
    (fb,) = _conv(match, "U1")["feedback"]
    assert fb["vout_computed"] == pytest.approx(3.315, abs=1e-3)
    assert not _findings(match, "fb_divider_mismatch")
    mismatch = _run(vref=0.8)
    (finding,) = _findings(mismatch, "fb_divider_mismatch")
    assert finding["severity"] == "warning" and "4.42 V" in finding["issue"]


def test_a_three_terminal_adjustable_regulator_uses_its_own_formula():
    lm317 = [("U5", {"1": ("1", "ADJ5"), "2": ("OUT", "3V3"), "3": ("IN", "5V0")},
              {"description": "Adjustable positive voltage regulator"}),
             ("R1", {"1": ("1", "3V3"), "2": ("2", "ADJ5")}, {"value": "240"}),
             ("R2", {"1": ("1", "ADJ5"), "2": ("2", "GND")}, {"value": "390"}),
             ("U4", {"1": ("VDD", "3V3", "Power"), "2": ("GND", "GND", "Power")}, {}),
             ("J1", {"1": ("1", "5V0"), "2": ("2", "GND")}, {"description": "Power jack"})]
    (fb,) = _conv(_run(lm317), "U5")["feedback"]
    assert fb["pin"] == "1" and fb["formula"].startswith("Vout = Vref x (1 + Rbot/Rtop)")
    assert fb["implied_vref"] == pytest.approx(3.3 / (1 + 390 / 240), abs=1e-3)
    assert fb["near_common_vref"] == 1.25


def test_input_and_output_capacitors_and_the_smps_input_filter():
    result = _run()
    buck = _conv(result, "U1")
    assert buck["input_capacitors"] == {"count": 1, "total_uF": 10.0, "values": {"10uF": 1}}
    assert buck["outputs"][0]["capacitors"] == 2
    assert buck["input_filter"] == {"filtered": True, "part": "L1", "type": "ferrite",
                                    "from": "VIN", "form": "pi"}
    unfiltered = _run(_with(("R7", {"1": ("1", "VIN"), "2": ("2", "VIN_BUCK")},
                             {"value": "0R"}), drop=("L1",)))
    assert _conv(unfiltered, "U1")["input_filter"] == {"filtered": False}
    (finding,) = _findings(unfiltered, "smps_input_unfiltered")
    assert finding["designators"] == ["U1"] and finding["severity"] == "info"
    bare = _run(_with(drop=("C5",)))
    (finding,) = _findings(bare, "no_output_capacitor")
    assert finding["designators"] == ["U2"] and finding["rail"] == "1V8"


# ---------- topologies ----------

def test_boost_through_its_diode():
    boost = _with(
        ("U6", {"1": ("VIN", "3V3", "Power"), "2": ("SW", "SW6"), "3": ("FB", "FB6"),
                "4": ("GND", "GND")}, {"description": "Boost converter"}),
        ("L6", {"1": ("1", "3V3"), "2": ("2", "SW6")}, {"description": "Inductor 10uH"}),
        ("D6", {"1": ("A", "SW6"), "2": ("K", "12V")}, {"description": "Schottky diode"}),
        ("C7", {"1": ("1", "12V"), "2": ("2", "GND")}, {"value": "10uF"}),
        ("R10", {"1": ("1", "12V"), "2": ("2", "FB6")}, {"value": "180k"}),
        ("R11", {"1": ("1", "FB6"), "2": ("2", "GND")}, {"value": "20k"}),
        ("U7", {"1": ("VCC", "12V", "Power"), "2": ("GND", "GND")}, {}),
    )
    conv = _conv(_run(boost), "U6")
    assert conv["type"] == "boost" and conv["outputs"][0]["rail"] == "12V"
    assert "D6" in conv["outputs"][0]["via"]
    assert conv["feedback"][0]["implied_vref"] == pytest.approx(1.2, abs=1e-3)


def test_controller_output_through_external_mosfets():
    ctrl = [
        ("J1", {"1": ("1", "12V"), "2": ("2", "GND")}, {"description": "Power jack"}),
        ("U6", {"1": ("VIN", "12V", "Power"), "2": ("HG", "HG6"), "3": ("LG", "LG6"),
                "4": ("FB", "FB6"), "5": ("GND", "GND")},
         {"description": "Synchronous buck controller"}),
        ("Q1", {"1": ("G", "HG6"), "2": ("D", "12V"), "3": ("S", "SWN6")},
         {"description": "N-channel MOSFET"}),
        ("Q2", {"1": ("G", "LG6"), "2": ("D", "SWN6"), "3": ("S", "GND")},
         {"description": "N-channel MOSFET"}),
        ("L6", {"1": ("1", "SWN6"), "2": ("2", "5V0")}, {"description": "Inductor 6.8uH"}),
        ("C7", {"1": ("1", "5V0"), "2": ("2", "GND")}, {"value": "47uF"}),
        ("R10", {"1": ("1", "5V0"), "2": ("2", "FB6")}, {"value": "52.3k"}),
        ("R11", {"1": ("1", "FB6"), "2": ("2", "GND")}, {"value": "10k"}),
        ("U7", {"1": ("VCC", "5V0", "Power"), "2": ("GND", "GND")}, {}),
    ]
    result = _run(ctrl)
    conv = _conv(result, "U6")
    assert conv["type"] == "buck" and conv["controller"] is True
    assert conv["outputs"][0]["rail"] == "5V0" and "L6" in conv["outputs"][0]["via"]
    assert _rail(result, "5V0")["supplied"]
    # The switch node is not a rail.
    assert not any(row["rail"] == "SWN6" for row in result["rails"])


def test_pmic_outputs_and_a_module_named_for_its_voltages():
    pmic = [("U6", {"1": ("VIN", "5V0", "Power"), "2": ("SW1", "SW_A"), "3": ("FB1", "1V2"),
                    "4": ("LDO1", "2V8", "Power"), "5": ("GND", "GND")},
             {"description": "PMIC, one buck and one LDO"}),
            ("L1", {"1": ("1", "SW_A"), "2": ("2", "1V2")}, {"description": "Inductor"}),
            ("J1", {"1": ("1", "5V0"), "2": ("2", "GND")}, {"description": "Power jack"})]
    conv = _conv(_run(pmic), "U6")
    assert conv["type"] == "pmic"
    assert {(out["rail"], out["type"]) for out in conv["outputs"]} == {("1V2", "buck"),
                                                                      ("2V8", "ldo")}
    module = [("PS1", {"1": ("+VIN", "24V"), "2": ("-VIN", "GND"), "3": ("+15V", "P15V"),
                       "4": ("-15V", "N15V"), "5": ("COM", "GND")},
               {"description": "Isolated DC/DC converter module, 2 W"}),
              ("J1", {"1": ("1", "24V"), "2": ("2", "GND")}, {"description": "Terminal block"})]
    conv = _conv(_run(module), "PS1")
    assert conv["module"] is True and conv["isolated"] is True
    assert {out["rail"] for out in conv["outputs"]} == {"P15V", "N15V"}


def test_load_switch_reference_and_supervisor():
    parts = _with(
        ("U6", {"1": ("VIN", "3V3", "Power"), "2": ("VOUT", "3V3_SW", "Power"),
                "3": ("ON", "MCU_GPIO", "Input"), "4": ("GND", "GND")},
         {"description": "Load switch, 2 A"}),
        ("U7", {"1": ("VIN", "3V3", "Power"), "2": ("VOUT", "VREF_2V5"), "3": ("GND", "GND")},
         {"description": "Precision voltage reference 2.5 V"}),
        ("U8", {"1": ("VDD", "3V3", "Power"), "2": ("RESET", "NRST", "OpenCollector"),
                "3": ("GND", "GND")}, {"description": "Voltage supervisor"}),
    )
    result = _run(parts)
    switch = _conv(result, "U6")
    assert switch["category"] == "switch" and switch["outputs"][0]["rail"] == "3V3_SW"
    assert switch["enable"][0]["state"] == "gpio"
    assert not any(row["designator"] in ("U7", "U8") for row in result["converters"])
    assert result["references"][0]["designator"] == "U7"
    assert result["supervisors"][0]["designator"] == "U8"
    assert result["supervisors"][0]["monitors"] == ["3V3"]


# ---------- rails without a source ----------

def test_rail_without_source():
    orphan = _with(("U8", {"1": ("VCC", "5V_AUX", "Power"), "2": ("GND", "GND")}, {}),
                   ("U9", {"1": ("VCC", "5V_AUX", "Power"), "2": ("GND", "GND")}, {}))
    result = _run(orphan)
    (finding,) = _findings(result, "rail_without_source")
    assert finding["severity"] == "warning" and finding["rail"] == "5V_AUX"
    assert finding["loads"] == ["U8", "U9"]
    # One part only: its own regulator pin, or a pin left off its rail: info.
    single = _run(_with(("U8", {"1": ("VCC", "5V_AUX", "Power"), "2": ("GND", "GND")}, {})))
    assert _findings(single, "rail_without_source")[0]["severity"] == "info"
    # A connector pin on the rail: it may be fed from outside.
    fed = _run(orphan + [("J2", {"1": ("1", "5V_AUX"), "2": ("2", "AUX_SIG")},
                          {"description": "Header 1x2"})])
    assert not _findings(fed, "rail_without_source")


def test_a_rail_fed_only_by_a_not_fitted_regulator():
    plan = VariantPlan(name="Lite", not_fitted={"U2"})
    result = _run(plan=plan)
    (finding,) = _findings(result, "rail_without_source")
    assert finding["severity"] == "warning" and "Not Fitted in variant Lite" in finding["issue"]
    assert not any(row["designator"] == "U2" for row in result["converters"])


def test_internal_regulator_pins_are_sources():
    soc = [("U5", {"1": ("VDD", "3V3", "Power"), "2": ("VCAP", "VCAP_1", "Power"),
                   "3": ("GND", "GND", "Power")}, {"description": "Microcontroller"}),
           ("C9", {"1": ("1", "VCAP_1"), "2": ("2", "GND")}, {"value": "2.2uF"}),
           ("J1", {"1": ("1", "3V3"), "2": ("2", "GND")}, {"description": "Header 1x2"})]
    result = _run(soc)
    assert _rail(result, "VCAP_1")["supplied"]
    assert not _findings(result, "rail_without_source")


# ---------- review fixes ----------

SENSORS = [("U8", {"1": ("VDD", "3V3_SENSOR", "Power"), "2": ("GND", "GND", "Power")}, {}),
           ("U9", {"1": ("VDD", "3V3_SENSOR", "Power"), "2": ("GND", "GND", "Power")}, {})]


def test_a_high_value_resistor_does_not_supply_a_rail():
    weak = _run(_with(("R9", {"1": ("1", "3V3"), "2": ("2", "3V3_SENSOR")}, {"value": "100k"}),
                      *SENSORS))
    rail = _rail(weak, "3V3_SENSOR")
    assert rail["supplied"] is False and "fed_by" not in rail
    assert rail["only_through_resistor"] == [{"part": "R9", "value": "100k", "from": "3V3"}]
    (finding,) = _findings(weak, "rail_without_source")
    assert finding["severity"] == "warning" and "only through R9 (100k)" in finding["issue"]
    # Up to 100 ohm it is an RC filter and carries the supply.
    rc = _run(_with(("R9", {"1": ("1", "3V3"), "2": ("2", "3V3_SENSOR")}, {"value": "10"}),
                    *SENSORS))
    assert _rail(rc, "3V3_SENSOR")["supplied"] and not _findings(rc, "rail_without_source")
    # A divider setting reference inputs (a DDR VREF) does supply them.
    vref = _run(_with(("R9", {"1": ("1", "3V3"), "2": ("2", "VREF_HALF")}, {"value": "1k"}),
                      ("R10", {"1": ("1", "VREF_HALF"), "2": ("2", "GND")}, {"value": "1k"}),
                      ("U8", {"1": ("VREF", "VREF_HALF", "Power"), "2": ("GND", "GND")}, {}),
                      ("U9", {"1": ("VREFCA", "VREF_HALF", "Power"), "2": ("GND", "GND")}, {})))
    assert _rail(vref, "VREF_HALF")["supplied"] and not _findings(vref, "rail_without_source")
    assert any(edge["type"] == "reference_feed" for edge in
               pt.build_power_tree(_netlist(_with(
                   ("R9", {"1": ("1", "3V3"), "2": ("2", "VREF_HALF")}, {"value": "1k"}),
                   ("U8", {"1": ("VREF", "VREF_HALF", "Power"), "2": ("GND", "GND")}, {}))))
               ["edges"])


def test_a_p_fet_with_its_source_toward_the_input_is_a_switch():
    jack = [("J1", {"1": ("1", "VIN_RAW"), "2": ("2", "GND")}, {"description": "DC power jack"}),
            ("C1", {"1": ("1", "VIN"), "2": ("2", "GND")}, {"value": "47uF"}),
            ("U1", {"1": ("VDD", "VIN", "Power"), "2": ("GND", "GND", "Power")}, {})]
    switch = _run(jack + [("Q1", {"1": ("S", "VIN_RAW"), "2": ("G", "GATE1"), "3": ("D", "VIN")},
                           {"description": "P-channel MOSFET"})])
    (row,) = switch["inputs"]
    assert "reverse_polarity" not in row and "a switch" in row["other_series"][0]
    assert _findings(switch, "no_reverse_protection")
    # An N-FET with its source toward the input (an ideal-diode or reverse-polarity FET).
    nfet = _run(jack + [("Q1", {"1": ("S", "VIN_RAW"), "2": ("G", "GATE1"), "3": ("D", "VIN")},
                         {"description": "N-channel MOSFET"})])
    assert "body diode forward" in nfet["inputs"][0]["reverse_polarity"][0]
    assert not _findings(nfet, "no_reverse_protection")


def _ldo(en_name, en_net, extra=()):
    return _with(("U2", {"1": ("IN", "3V3", "Power"), "2": ("OUT", "1V8", "Power"),
                         "3": (en_name, en_net, "Input"), "4": ("GND", "GND", "Power")},
                  {"description": "LDO regulator"}), *extra, drop=("U2",))


def test_an_enable_held_off_with_nothing_driving_it():
    down = _run(_ldo("EN", "LDO_EN", [("R8", {"1": ("1", "LDO_EN"), "2": ("2", "GND")},
                                       {"value": "100k"})]))
    (en,) = _conv(down, "U2")["enable"]
    assert en["state"] == "pulled_down" and en["held_off"] and en["on_when"] == "high"
    (finding,) = _findings(down, "enable_held_off")
    assert finding["severity"] == "warning" and "1V8 never comes up" in finding["issue"]
    assert finding["designators"] == ["R8", "U2"]
    grounded = _run(_ldo("EN", "GND"))
    assert _findings(grounded, "enable_held_off")[0]["severity"] == "warning"
    # An active-low enable pulled up holds the part off; pulled down it runs.
    low_up = _run(_ldo("E\\N\\", "LDO_EN", [("R8", {"1": ("1", "LDO_EN"), "2": ("2", "3V3")},
                                              {"value": "100k"})]))
    assert _findings(low_up, "enable_held_off")
    low_down = _run(_ldo("E\\N\\", "LDO_EN", [("R8", {"1": ("1", "LDO_EN"), "2": ("2", "GND")},
                                                {"value": "100k"})]))
    assert not _findings(low_down, "enable_held_off")
    # SHDN with no negation mark: the polarity is not known, so no finding.
    shdn = _run(_ldo("SHDN", "LDO_EN", [("R8", {"1": ("1", "LDO_EN"), "2": ("2", "GND")},
                                          {"value": "100k"})]))
    assert not _findings(shdn, "enable_held_off")


def test_a_modules_on_pin_polarity_is_not_guessed():
    module = [("J1", {"1": ("1", "24V"), "2": ("2", "GND")}, {"description": "Terminal block"}),
              ("PS1", {"1": ("VIN", "24V"), "2": ("ON", "ON1_NET"), "3": ("VOUT", "5V"),
                       "4": ("GND", "GND")}, {"description": "DC/DC converter module"}),
              ("R1", {"1": ("1", "ON1_NET"), "2": ("2", "GND")}, {"value": "0R5"})]
    result = _run(module)
    assert _conv(result, "PS1")["enable"][0]["state"] == "pulled_down"
    assert not _findings(result, "enable_held_off")  # negative-logic modules run when low


def test_a_held_off_channel_of_a_multi_output_part_is_info():
    pmic = [("J1", {"1": ("1", "5V0"), "2": ("2", "GND")}, {"description": "Power jack"}),
            ("U6", {"1": ("VIN", "5V0", "Power"), "2": ("LDO1", "2V8", "Power"),
                    "3": ("LDO2", "1V2", "Power"), "4": ("EN1", "5V0"), "5": ("EN2", "GND"),
                    "6": ("GND", "GND")}, {"description": "PMIC, two LDOs"})]
    (finding,) = _findings(_run(pmic), "enable_held_off")
    assert finding["severity"] == "info" and finding["pin"] == "5"


def test_a_regulator_module_with_a_connector_designator():
    module = [("J1", {"1": ("1", "VIN"), "2": ("2", "GND")}, {"description": "DC power jack"}),
              ("J2", {"1": ("IN", "VIN", "Power"), "2": ("GND", "GND", "Power"),
                      "3": ("OUT", "3V3", "Power")}, {"description": "LDO regulator module 3.3 V"}),
              ("C2", {"1": ("1", "3V3"), "2": ("2", "GND")}, {"value": "10uF"}),
              ("U2", {"1": ("VDD", "3V3", "Power"), "2": ("GND", "GND", "Power")}, {})]
    result = _run(module)
    conv = _conv(result, "J2")
    assert conv["module"] and conv["outputs"][0]["rail"] == "3V3"
    assert [row["source"] for row in result["inputs"]] == ["J1"]
    assert not any("J2" in f.get("designators", ()) for f in result["findings"]
                   if f["check"] in ("no_input_fuse", "no_reverse_protection", "no_input_tvs"))
    # A plain header with IN and OUT pins stays a connector.
    header = [module[0], ("J2", module[1][1], {"description": "Header 1x3"})] + module[2:]
    assert not any(row["designator"] == "J2" for row in _run(header)["converters"])


def test_a_bare_pg_pin_beside_pok_or_on_a_bias_capacitor_is_not_power_good():
    part = {"1": ("VIN", "VIN_BUCK", "Power"), "2": ("SW", "SW1"), "3": ("PG", "PGB"),
            "4": ("BTMP", "BTMP1"), "5": ("FB", "FB1"), "6": ("GND", "GND", "Power")}
    cap = ("C9", {"1": ("1", "PGB"), "2": ("2", "BTMP1")}, {"value": "100nF"})
    biased = _run(_with(("U1", part, {"description": "Step-down converter"}), cap,
                        drop=("U1",)))
    assert "power_good" not in _conv(biased, "U1")
    assert not _findings(biased, "pgood_unused", "U1")
    both = _run(_with(("U1", {**part, "7": ("POK", "PG1", "OpenCollector")},
                       {"description": "Step-down converter"}), drop=("U1",)))
    assert [row["name"] for row in _conv(both, "U1")["power_good"]] == ["POK"]


def test_rails_page_with_the_converters_and_unsupplied_rails_come_first():
    result = _run(_with(*SENSORS), limit=2)
    assert result["rails"][0]["rail"] == "3V3_SENSOR" and len(result["rails"]) == 2
    seen, offset = [], 0
    while True:
        page = _run(_with(*SENSORS), limit=2, offset=offset)
        seen += [row["rail"] for row in page["rails"]]
        if not page.get("next_offset"):
            break
        offset = page["next_offset"]
    assert len(seen) == len(set(seen)) == page["rails_total"]


def test_one_input_per_net():
    parallel = _with(("J2", {"1": ("1", "VIN_RAW"), "2": ("2", "GND")},
                      {"description": "Header 1x2"}), drop=("F1", "D1", "D2"))
    data = pt.build_power_tree(_netlist(parallel))
    roles = {s["part"]: s.get("role") for s in data["sources"] if s["type"] == "connector"}
    assert roles == {"J1": "input", "J2": "outlet"}  # the power jack is likelier
    twins = [("J1", {"1": ("1", "VIN"), "2": ("2", "GND")}, {"description": "Header 1x2"}),
             ("J2", {"1": ("1", "VIN"), "2": ("2", "GND")}, {"description": "Header 1x2"}),
             ("U4", {"1": ("VDD", "VIN", "Power"), "2": ("GND", "GND", "Power")}, {})]
    result = _run(twins)
    (row,) = result["inputs"]
    assert row["source"] == "J1" and row["also_input_on_net"] == ["J2"]
    assert len(_findings(result, "no_input_fuse")) == 1


def test_a_feedback_leg_switched_by_a_jumper():
    leg = _run(_with(("R30", {"1": ("1", "FB1"), "2": ("2", "FB_JMP")}, {"value": "100k"}),
                     ("JP1", {"1": ("1", "FB_JMP"), "2": ("2", "GND")}, {"description": "Jumper"})))
    (fb,) = _conv(leg, "U1")["feedback"]
    assert fb["selectable_leg"]["through"] == "JP1" and fb["selectable_leg"]["to"] == "ground"
    rbot = 1 / (1 / 22.1e3 + 1 / 100e3)
    assert fb["selectable_leg"]["vout_over_vref"] == pytest.approx(1 + 100e3 / rbot, abs=1e-3)
    (finding,) = _findings(leg, "fb_divider_mismatch")
    assert "with JP1 closed, R30 joins the bottom leg" in finding["issue"]


def test_common_vrefs_include_0v75_and_0v4():
    assert {0.4, 0.75} <= set(pt.COMMON_VREFS)
    board = _with(("R1", {"1": ("1", "3V3"), "2": ("2", "FB1")}, {"value": "100k"}),
                  ("R2", {"1": ("1", "FB1"), "2": ("2", "GND")}, {"value": "29.4k"}),
                  drop=("R1", "R2"))
    assert _conv(_run(board), "U1")["feedback"][0]["near_common_vref"] == 0.75


def test_part_filter_keeps_only_that_converters_findings():
    twins = [("J1", {"1": ("1", "VIN"), "2": ("2", "GND")}, {"description": "Terminal block"}),
             ("U1", {"1": ("VIN", "VIN", "Power"), "2": ("VOUT", "3V3", "Power"),
                     "3": ("GND", "GND", "Power")}, {"description": "Buck converter module"}),
             ("U2", {"1": ("VIN", "VIN", "Power"), "2": ("VOUT", "5V", "Power"),
                     "3": ("GND", "GND", "Power")}, {"description": "Buck converter module"})]
    result = _run(twins, parts=["U1"])
    assert not any("U2" in f.get("designators", ()) for f in result["findings"])
    assert _findings(result, "smps_input_unfiltered", "U1")
    assert _findings(_run(twins, rails=["VIN"]), "smps_input_unfiltered", "U2")


def test_batteries_group_their_pins_and_skip_a_thermistor_pin():
    cell = [("BT1", {"1": ("+", "VBAT"), "2": ("+", "VBAT"), "3": ("-", "GND")},
             {"description": "Coin cell holder"}),
            ("U1", {"1": ("VDD", "VBAT", "Power"), "2": ("GND", "GND", "Power")}, {})]
    data = pt.build_power_tree(_netlist(cell))
    assert [(s["part"], s["pins"]) for s in data["sources"]] == [("BT1", ["1", "2"])]
    assert pt.mermaid(data).count("BT1 pin") == 1
    pack = [("J1", {"1": ("BAT+", "VBAT"), "2": ("NTC", "BAT_NTC"), "3": ("BAT-", "GND")},
             {"description": "Battery connector"}),
            ("R1", {"1": ("1", "BAT_NTC"), "2": ("2", "GND")}, {"value": "10k"}),
            ("U1", {"1": ("VDD", "VBAT", "Power"), "2": ("GND", "GND", "Power")}, {})]
    nets = {s["net"] for s in pt.build_power_tree(_netlist(pack))["sources"]}
    assert nets == {"VBAT"}


def test_internal_regulator_pin_names():
    regin = [("U1", {"1": ("VDD_REG_IN", "REG_IN", "Power"), "2": ("GND", "GND", "Power")},
              {"description": "Bluetooth SoC"}),
             ("U2", {"1": ("VDD", "REG_IN", "Power"), "2": ("GND", "GND", "Power")}, {})]
    assert not _rail(_run(regin), "REG_IN")["supplied"]  # a regulator's input, not output
    core = [("U1", {"1": ("VDD_CORE", "CORE", "Power"), "2": ("VDD_CORE", "CORE", "Power"),
                    "3": ("GND", "GND", "Power")}, {"description": "Bluetooth SoC"}),
            ("C1", {"1": ("1", "CORE"), "2": ("2", "GND")}, {"value": "1uF"})]
    data = pt.build_power_tree(_netlist(core))
    assert [(s["type"], s["pins"]) for s in data["sources"]] == [("internal", ["1", "2"])]
    # An AVINO pin feeding the part's own AVIN: its own bias regulator.
    avino = _with(("U1", {"1": ("PVIN", "VIN_BUCK", "Power"), "2": ("PVIN", "VIN_BUCK"),
                          "3": ("AVIN", "AV1"), "4": ("AVINO", "AV1"), "5": ("SW", "SW1"),
                          "6": ("FB", "FB1"), "7": ("GND", "GND")},
                   {"description": "Step-down converter"}),
                  ("C9", {"1": ("1", "AV1"), "2": ("2", "GND")}, {"value": "1uF"}), drop=("U1",))
    assert _rail(_run(avino), "AV1")["supplied"]


def test_vref_for_a_converter_without_a_divider_is_noted():
    notes = _run(vref={"U2": 0.6, "U77": 0.6})["notes"]
    assert any("U2, which has no feedback divider read" in note for note in notes)
    assert any("U77, not a converter here" in note for note in notes)


def test_a_pass_controller_keeps_its_output_as_a_rail():
    base = [("J1", {"1": ("1", "VIN"), "2": ("2", "GND")}, {"description": "DC power jack"}),
            ("U2", {"1": ("VDD", "VOUT_OR", "Power"), "2": ("GND", "GND", "Power")}, {}),
            ("U3", {"1": ("VDD", "VOUT_OR", "Power"), "2": ("GND", "GND", "Power")}, {})]
    ideal = _run(base + [
        ("U1", {"1": ("IN", "VIN", "Power"), "2": ("GATE", "G1"), "3": ("OUT", "VOUT_OR"),
                "4": ("GND", "GND")}, {"description": "Ideal diode controller"}),
        ("Q1", {"1": ("S", "VIN"), "2": ("G", "G1"), "3": ("D", "VOUT_OR")},
         {"description": "N-channel MOSFET"})])
    assert _rail(ideal, "VOUT_OR")["supplied"] and _rail(ideal, "VOUT_OR")["load_count"] == 2
    # A hot-swap controller with no OUT pin: the output is past the MOSFET it drives.
    hot = _run(base + [
        ("U1", {"1": ("VCC", "VIN", "Power"), "2": ("SENSE", "VSNS"), "3": ("GATE", "G1"),
                "4": ("GND", "GND")}, {"description": "Hot swap controller"}),
        ("R1", {"1": ("1", "VIN"), "2": ("2", "VSNS")}, {"value": "0.01"}),
        ("Q1", {"1": ("D", "VSNS"), "2": ("G", "G1"), "3": ("S", "VOUT_OR")},
         {"description": "N-channel MOSFET"})])
    assert [out["rail"] for out in _conv(hot, "U1")["outputs"]] == ["VOUT_OR"]
    assert _rail(hot, "VOUT_OR")["supplied"]


def test_a_ferrite_on_a_branch_the_input_feeds_is_not_its_filter():
    branch = _run(_with(("R7", {"1": ("1", "VIN"), "2": ("2", "VIN_BUCK")}, {"value": "0R"}),
                        ("FB2", {"1": ("1", "VIN_BUCK"), "2": ("2", "VIN_ANA")},
                         {"description": "Ferrite bead"}),
                        ("U9", {"1": ("V+", "VIN_ANA", "Power"), "2": ("GND", "GND", "Power")},
                         {"description": "Operational amplifier"}), drop=("L1",)))
    assert _conv(branch, "U1")["input_filter"] == {"filtered": False}


def test_a_converter_input_with_no_source():
    lone = [("U1", {"1": ("VIN", "VIN_X", "Power"), "2": ("VOUT", "3V3", "Power"),
                    "3": ("GND", "GND", "Power")}, {"description": "LDO regulator"}),
            ("U2", {"1": ("VDD", "3V3", "Power"), "2": ("GND", "GND", "Power")}, {})]
    (finding,) = _findings(_run(lone), "rail_without_source")
    assert finding["rail"] == "VIN_X" and "is the input of U1" in finding["issue"]


# ---------- output formats and entry point ----------

def test_mermaid():
    result = _run(format="mermaid")
    text = result["mermaid"]
    assert text.startswith("flowchart LR") and "J1 pin 1 (external input)" in text
    assert '["U1 buck"]' in text and '|"F1 fuse"|' in text and "load" in text
    assert "findings" not in result and result["overview"]["converters"] == 2
    small = _run(format="mermaid", mermaid_lines=6)
    assert len(small["mermaid"].splitlines()) == 7 and "more lines left out" in small["mermaid"]


def test_filters_by_rail_and_part():
    by_part = _run(parts=["U2"])
    assert [row["designator"] for row in by_part["converters"]] == ["U2"]
    assert {row["rail"] for row in by_part["rails"]} >= {"3V3", "1V8"}
    assert all("U2" in f.get("designators", ()) or f.get("rail") in ("3V3", "1V8", "PG1")
               for f in by_part["findings"])
    by_rail = _run(rails=["1v8"])
    assert [row["designator"] for row in by_rail["converters"]] == ["U2"]
    assert _run(rails=["NOPE"])["error"] == "rail_not_found"
    assert _run(parts=["NOPE"])["error"] == "part_not_found"
    assert "Not found: NOPE." in _run(rails=["1V8", "NOPE"])["notes"]


def test_checks_paging_and_errors():
    assert _run(checks=["nope"])["error"] == "invalid_checks"
    assert _run(format="svg")["error"] == "invalid_format"
    only = _run(checks=["pgood_unused"])
    assert only["checks_run"] == ["pgood_unused"] and {f["check"] for f in only["findings"]} == {
        "pgood_unused"}
    first = _run(limit=1)
    assert first["has_more"] and first["next_offset"] == 1 and len(first["converters"]) == 1
    second = _run(limit=1, offset=1)
    assert second["converters"][0]["designator"] == "U2"
    assert first["findings"][0] != second["findings"][0]
    assert "heuristic" in first and "draft" in first["heuristic"]


def test_build_power_tree_returns_plain_data():
    data = pt.build_power_tree(_netlist(BOARD), vref=0.6)
    json.dumps(data)
    assert set(data) >= {"converters", "rails", "edges", "sources", "inputs", "references",
                         "supervisors", "sequencing", "variant"}
    assert pt.power_findings(data)


# ---------- tool ----------

@pytest.fixture
def board(monkeypatch, tmp_path: Path):
    netlist = _netlist(BOARD)
    monkeypatch.setattr(main_mod, "_project", {
        "name": "Demo", "root_dir": str(tmp_path), "prj_pcb_path": str(tmp_path / "D.PrjPcb"),
        "sheets": [], "pcb_doc_paths": [],
    })
    monkeypatch.setattr(main_mod._altium, "_netlist", netlist)
    monkeypatch.setattr(main_mod, "_variant_state",
                        VariantState([VariantDefinition(name="Default")]))
    return netlist


def test_get_power_tree_tool(board):
    result = json.loads(main_mod.get_power_tree())
    assert result["variant"] == "Default" and result["converters_total"] == 2
    assert "netlist_updated_utc" in result and "page_limited_to" not in result
    vref = json.loads(main_mod.get_power_tree(vref="U1=0.8"))
    assert _findings(vref, "fb_divider_mismatch")[0]["severity"] == "warning"
    mermaid = json.loads(main_mod.get_power_tree(format="mermaid", rail="1V8"))
    assert mermaid["mermaid"].startswith("flowchart LR") and "U2 ldo" in mermaid["mermaid"]


@pytest.mark.parametrize("text, expected", [
    ("0.6", 0.6), ("0.8V", 0.8), ("U5=0.8, U7=0.6", {"U5": 0.8, "U7": 0.6}),
    ("U5:1.25v", {"U5": 1.25}), (None, None), ("", None),
])
def test_vref_parsing(text, expected):
    assert main_mod._parse_vref(text) == (expected, None)


@pytest.mark.parametrize("text", ["abc", "U5=", "U5=0", "U5=12", "U5=0.6,junk", "-1"])
def test_vref_parsing_errors(text):
    assert main_mod._parse_vref(text)[1]["error"] == "invalid_vref"


def test_get_power_tree_shrinks_to_fit(board, monkeypatch):
    many = []
    for index in range(40):
        many += [(f"U{index + 10}", {"1": ("VIN", "VIN", "Power"),
                                     "2": ("VOUT", f"{index + 1}V0", "Power"),
                                     "3": ("EN", f"EN_{index}", "Input"),
                                     "4": ("GND", "GND", "Power")},
                  {"description": "LDO regulator"}),
                 (f"U{index + 100}", {"1": ("VDD", f"{index + 1}V0", "Power"),
                                      "2": ("GND", "GND", "Power")}, {})]
    many.append(("J1", {"1": ("1", "VIN"), "2": ("2", "GND")}, {"description": "Power jack"}))
    monkeypatch.setattr(main_mod._altium, "_netlist", _netlist(many))
    full = main_mod.get_power_tree()
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", len(full) // 3)
    service = main_mod.prepare_power_tree.__globals__
    builds = []
    build = service["build_tree"]
    monkeypatch.setitem(service, "build_tree", lambda *a, **k: builds.append(1) or build(*a, **k))
    result = json.loads(main_mod.get_power_tree())
    assert builds == [1]  # built once, rendered per shrink step
    assert result["page_limited_to"] < 100 and result["has_more"]
    assert len(json.dumps(result, indent=2)) <= len(full) // 3
    rest = json.loads(main_mod.get_power_tree(offset=result["next_offset"]))
    assert rest["converters"][0]["designator"] != result["converters"][0]["designator"]


def test_get_power_tree_errors(board, monkeypatch):
    assert json.loads(main_mod.get_power_tree(vref="nope"))["error"] == "invalid_vref"
    assert json.loads(main_mod.get_power_tree(checks=["x"]))["error"] == "invalid_checks"
    assert json.loads(main_mod.get_power_tree(part="Q99"))["error"] == "part_not_found"
    monkeypatch.setattr(main_mod._altium, "_netlist", {**board, "components": {}})
    assert json.loads(main_mod.get_power_tree())["error"] == "no_components_loaded"
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.get_power_tree())["error"] == "no_project"


def test_tool_registration_manifest_readme_and_instructions():
    tools = {tool.name: tool for tool in asyncio.run(main_mod.mcp.list_tools())}
    tool = tools["get_power_tree"]
    assert tool.annotations.readOnlyHint is True and tool.title == "Get Power Tree"
    assert tool.meta["anthropic/maxResultSizeChars"] == main_mod.QUERY_NET_MAX_RESULT_SIZE_CHARS
    assert set(tool.inputSchema["properties"]) == {"rail", "part", "checks", "vref", "format",
                                                   "offset"}
    assert "heuristic" in tool.description and "next_offset" in tool.description
    assert "get_power_tree" in main_mod.NETLIST_TOOLS
    assert main_mod.SERVER_INSTRUCTIONS.index("get_power_tree") > 4096
    assert "get_power_tree" in main_mod.SCHEMATIC_REVIEW_PROMPT
    root = Path(__file__).parents[1]
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    assert "get_power_tree" in {entry["name"] for entry in manifest["tools"]}
    assert "| `get_power_tree` |" in (root / "README.md").read_text("utf-8")
