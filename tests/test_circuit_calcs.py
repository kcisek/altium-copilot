"""Circuit calculators (services/circuit_calcs, get_circuit_calculations): dividers, LED and
optocoupler currents, ADC input filters and RC filters. Synthetic boards with generic
names only; part numbers are the makers' published examples."""

import asyncio
import json
import math
from pathlib import Path

import pytest

import server.main as main_mod
import server.services.circuit_calcs as cc
from server.parsers.prj_pcb import VariantDefinition, VariantState
from server.services.bom import VariantPlan
from tests.test_pull_resistors import _netlist

GND = ("GND", "GND", "Power")
MCU = "ARM Cortex-M4 32-bit microcontroller"


def mcu(ref="U1", rail="3V3", **pins):
    """A processor on one supply with GPIO-named pins; pins as name=net or (net, type)."""
    spec = {"1": ("VDD", rail, "Power"), "2": GND}
    filler = {f"PC{n}": f"FREE{ref}{n}" for n in range(6)}
    for index, (name, net) in enumerate({**pins, **filler}.items(), start=10):
        spec[str(index)] = (name, net) if isinstance(net, str) else (name, *net)
    return (ref, spec, {"description": MCU})


def res(ref, net_a, net_b, value="10k", **info):
    params = info.pop("params", None)
    entry = {"value": value, "description": info.pop("description", "Resistor"), **info}
    if params:
        entry["params"] = params
    return (ref, {"1": ("1", net_a), "2": ("2", net_b)}, entry)


def cap(ref, net_a, net_b="GND", value="100nF"):
    return (ref, {"1": ("1", net_a), "2": ("2", net_b)},
            {"value": value, "description": "Capacitor"})


def led(ref, anode, cathode, description="LED, red, 0603", **info):
    return (ref, {"1": ("A", anode), "2": ("K", cathode)}, {"description": description, **info})


def buck(ref="U2", vin="12V", sw="SW1", fb="FB1", en=None):
    pins = {"1": ("VIN", vin, "Power"), "2": ("SW", sw), "3": ("FB", fb, "Input"),
            "4": ("GND", "GND", "Power")}
    if en:
        pins["5"] = ("EN", en, "Input")
    return (ref, pins, {"description": "Synchronous step-down converter"})


def _run(board, plan=None, **kwargs):
    kwargs.setdefault("limit", 1000)
    return cc.run_circuit_calculations(_netlist(board), plan, **kwargs)


def _rows(result, kind=None):
    return [row for row in result["calculations"] if kind is None or row["kind"] == kind]


def _divider(result, mid):
    return next(row for row in _rows(result, "divider") if row["midpoint"] == mid)


def _findings(result, check=None, severity=None):
    return [row for row in result["findings"] if (check is None or row["check"] == check)
            and (severity is None or row["severity"] == severity)]


# ---------- reading values ----------

@pytest.mark.parametrize("text, bare, expected", [
    ("1%", False, 0.01), ("±0.5 %", False, 0.005), ("RES 10k +/-5% 0402", False, 0.05),
    ("Chip Resistor, 10 KOhm, +/- 1%, 0.1 W", False, 0.01), ("33%", False, None),
    ("1", True, 0.01), ("F", True, 0.01), ("J", True, 0.05), ("0.1%", True, 0.001),
    ("n/a", True, None),
])
def test_tolerance_value(text, bare, expected):
    assert cc.tolerance_value(text, bare=bare) == expected


@pytest.mark.parametrize("mpn, expected", [
    ("RC0402FR-0710KL", 0.01), ("RC0603JR-0710KL", 0.05), ("RT0603BRD0710KL", 0.001),
    ("RMCF0402FT10K0", 0.01), ("CRCW0805576RFKEA", 0.01), ("ERJ-3EKF1002V", 0.01),
    ("ERJ-3GEYJ821V", 0.05), ("CR0603-FX-1002ELF", 0.01), ("ERJ-2GE0R00X", None),
    ("GRM188R71C104KA01D", None),
])
def test_tolerance_from_part_numbers(mpn, expected):
    part = cc.Part("R1", "resistor", [("A", "B")], mpns=[mpn])
    found = cc.resistor_tolerance(part)
    assert (found[0] if found else None) == expected


@pytest.mark.parametrize("text, colours", [
    ("LED, red, 0603", ["red"]), ("Chip LED (Red)", ["red"]), ("LED_GREEN_K1A2", ["green"]),
    ("Bright Green", ["green"]), ("True green InGaN LED", ["true_green"]),
    ("Blue LED", ["blue"]), ("White LED 6500K", ["white"]), ("Infrared emitter", ["ir"]),
    ("IR LED 940nm", ["ir"]), ("Amber LED", ["amber"]), ("Yellow", ["yellow"]),
    ("Infra-red", ["ir"]), ("Bicolour red/green LED", ["green", "red"]),
    ("Monocolor waterclear", []), ("Hardware reset", []),
])
def test_colours_in_text(text, colours):
    assert sorted(cc.colours_in(text)) == sorted(colours)


@pytest.mark.parametrize("nm, colour", [
    (940, "ir"), (630, "red"), (605, "orange"), (590, "orange"), (585, "yellow"),
    (570, "green"), (525, "true_green"), (470, "blue"), (380, None),
])
def test_wavelength_colour(nm, colour):
    assert cc.wavelength_colour(nm) == colour


def test_read_led_parameters_before_text():
    params = {"color": ("Color", "Blue"), "forward voltage": ("Forward Voltage", "3.1 V"),
              "forward current": ("Forward Current", "15mA")}
    info = cc.read_led(params, "LED, red")
    assert (info.colour, info.colour_source) == ("blue", "parameter (Color)")
    assert (info.vf, info.rating) == (3.1, 0.015)
    info = cc.read_led({"dominant wavelength": ("Dominant Wavelength", "470 nm")}, "")
    assert info.colour == "blue"
    info = cc.read_led({}, "LED 0603, Vf=2.1V, 20mA, 625nm")
    assert (info.colour, info.vf, info.rating) == ("red", 2.1, 0.02)
    assert info.colour_source == "text (wavelength)"
    info = cc.read_led({"lens color": ("Lens Color", "White Diffused")}, "")
    assert info.colour is None


def test_parse_vf_and_vth():
    assert cc.parse_vf("LED1=2.9, red=1.9,True Green=3.1") == (
        {"LED1": 2.9, "red": 1.9, "true_green": 3.1}, None)
    assert cc.parse_vf(None) == (None, None)
    for bad in ("LED1", "LED1=9", "red=x"):
        assert cc.parse_vf(bad)[1]["error"] == "invalid_vf"
    assert cc.parse_vth("1.2") == (1.2, None)
    assert cc.parse_vth("U3=1.2,U7=0.8V") == ({"U3": 1.2, "U7": 0.8}, None)
    for bad in ("U3", "U3=-1", "abc"):
        assert cc.parse_vth(bad)[1]["error"] == "invalid_vth"


# ---------- dividers ----------

def _adc_board(top="28k", bottom="12k", rail="12V", extra=()):
    return [mcu(**{"PA0/ADC1_IN0": "VMON"}),
            res("R1", rail, "VMON", top), res("R2", "VMON", "GND", bottom),
            ("J1", {"1": ("1", rail), "2": ("2", "GND")}, {"description": "Power connector"}),
            *extra]


def test_two_resistor_divider_feeding_an_adc():
    result = _run(_adc_board())
    row = _divider(result, "VMON")
    assert row["inputs"]["R_top"]["value"] == "28k" and row["inputs"]["R_top"]["to"] == "12V"
    assert row["inputs"]["R_bottom"]["parts"] == ["R2"]
    assert row["inputs"]["V_top"]["volts"] == 12.0 and row["inputs"]["V_top"]["source"] == "name"
    assert row["result"]["ratio"] == 0.3
    assert row["result"]["v_mid"] == 3.6
    assert row["result"]["source_impedance"] == "8.4k"
    assert row["result"]["current_ua"] == 300.0
    assert row["feeds"][0]["use"] == "adc" and row["feeds"][0]["part"] == "U1"
    assert "ratio = R_bottom / (R_top + R_bottom)" in row["formula"]
    assert row["confidence"] == "high"
    assert result["summary"]["dividers"]["by_feed"] == {"adc": 1}


def test_divider_output_above_the_pins_rail():
    at_limit = _run(_adc_board())  # 3.6 V: the 3.3 V rail + 0.3 V exactly
    assert not _findings(at_limit, "divider_output_above_input_rating")
    above = _run(_adc_board(top="27.9k"))
    found = _findings(above, "divider_output_above_input_rating", "warning")
    assert len(found) == 1 and found[0]["limit_v"] == 3.6 and found[0]["v_mid"] > 3.6
    assert set(found[0]["designators"]) == {"R1", "R2", "U1"}
    assert found[0]["pin_rail"]["rail"] == "3V3"


def test_tolerance_range_moves_the_check_to_the_high_bound():
    tol = {"tolerance": "1%"}
    board = _adc_board(extra=())
    board = [part if part[0] not in ("R1", "R2") else
             (part[0], part[1], {**part[2], "params": {**part[2].get("params", {}), **tol}})
             for part in board]
    result = _run(board)
    row = _divider(result, "VMON")
    low, high = row["result"]["ratio_range"]
    assert low == pytest.approx(11.88 / (28.28 + 11.88), abs=1e-5)
    assert high == pytest.approx(12.12 / (27.72 + 12.12), abs=1e-5)
    assert row["inputs"]["R_top"]["tolerance"] == "1%"
    # The nominal 3.6 V sits on the limit; the 1% high bound is above it.
    assert _findings(result, "divider_output_above_input_rating")
    assert result["summary"]["dividers"]["with_tolerance_range"] == 1


def test_tolerance_from_text_and_mixed_values():
    board = [mcu(**{"PA0/ADC1_IN0": "VMON"}),
             res("R1", "5V", "VMON", "10k", description="RES 10k 5% 0402"),
             res("R2", "VMON", "GND", "10k", description="RES 10k 1% 0402")]
    row = _divider(_run(board), "VMON")
    assert row["inputs"]["R_top"]["tolerance"] == "5%"
    assert row["inputs"]["R_top"]["tolerance_source"] == "text (description)"
    assert "ratio_range" in row["result"]
    no_tol = [mcu(**{"PA0/ADC1_IN0": "VMON"}), res("R1", "5V", "VMON"),
              res("R2", "VMON", "GND")]
    assert "ratio_range" not in _divider(_run(no_tol), "VMON")["result"]


def test_series_chain_and_parallel_legs():
    board = [mcu(**{"PA0/ADC1_IN0": "VMON"}),
             res("R1", "24V", "CHAIN1", "100k"), res("R2", "CHAIN1", "VMON", "100k"),
             cap("C9", "CHAIN1"), res("R3", "VMON", "GND", "20k"), res("R4", "VMON", "GND", "20k")]
    result = _run(board)
    row = _divider(result, "VMON")
    assert row["inputs"]["R_top"]["parts"] == ["R2", "R1"]  # from the midpoint out
    assert row["inputs"]["R_top"]["value"] == "200k"
    assert row["inputs"]["R_top"]["arrangement"] == "series chain"
    assert row["inputs"]["R_bottom"]["parallel"] == 2 and row["inputs"]["R_bottom"]["value"] == "10k"
    assert row["result"]["ratio"] == pytest.approx(10 / 210, abs=1e-5)
    assert row["result"]["v_mid"] == pytest.approx(24 * 10 / 210, abs=1e-3)
    assert [r["midpoint"] for r in _rows(result, "divider")] == ["VMON"]  # not CHAIN1


def test_loaded_midpoint_lists_the_other_resistors():
    board = [*_adc_board(top="20k", bottom="10k"), res("R5", "VMON", "AUX", "100k"),
             res("R6", "AUX", "NODE2", "1k"),
             ("U9", {"1": ("IN", "AUX", "Input"), "2": GND}, {"description": "Buffer"})]
    row = _divider(_run(board), "VMON")
    assert row["also_on_midpoint"] == [{"parts": ["R5"], "to": "AUX", "value": "100k"}]
    assert row["confidence"] == "medium"
    assert any("other resistors" in note for note in row["notes"])


def test_pull_up_and_pull_down_on_a_driven_line_is_not_a_divider():
    board = [("U3", {"1": ("VDD", "3V3", "Power"), "2": GND, "3": ("OUT", "CTRL", "Output")},
              {"description": "Logic buffer"}),
             ("U4", {"1": ("VDD", "3V3", "Power"), "2": GND, "3": ("IN", "CTRL", "Input")},
              {"description": "Logic buffer"}),
             res("R1", "3V3", "CTRL", "10k"), res("R2", "CTRL", "GND", "100k")]
    result = _run(board)
    assert not _rows(result, "divider")
    assert result["summary"]["dividers"]["pull_pairs_skipped"] == 1


def test_a_bus_line_with_two_pulls_is_not_a_divider():
    board = [mcu(**{"PB7/TWI_SDA": "TWI_SDA", "PB6": "TWI_SCL"}),
             res("R1", "3V3", "TWI_SDA", "4.7k"), res("R2", "TWI_SDA", "GND", "100k")]
    result = _run(board)
    assert not _rows(result, "divider")
    assert result["summary"]["dividers"]["pull_pairs_skipped"] == 1


def test_a_series_resistor_into_a_pull_up_is_not_a_divider():
    board = [mcu(PA1="DRV"), res("R1", "DRV", "LINE", "270"), res("R2", "LINE", "5V", "4.7k"),
             ("U3", {"1": ("VDD", "5V", "Power"), "2": GND, "3": ("IN", "LINE", "Input")},
              {"description": "Logic buffer"})]
    assert not _rows(_run(board), "divider")


def test_divider_between_two_rails():
    board = [mcu(**{"PA0/ADC1_IN0": "VMID"}), res("R1", "5V", "VMID", "10k"),
             res("R2", "VMID", "1V8", "10k")]
    row = _divider(_run(board), "VMID")
    assert row["inputs"]["V_bottom"]["net"] == "1V8"
    assert row["result"]["v_mid"] == 3.4


def test_feedback_divider_takes_the_power_trees_vref():
    board = [buck(), ("L1", {"1": ("1", "SW1"), "2": ("2", "5V")}, {"description": "Inductor"}),
             res("R1", "5V", "FB1", "52.3k"), res("R2", "FB1", "GND", "10k"), cap("C1", "5V")]
    row = _divider(_run(board), "FB1")
    assert row["feeds"][0]["use"] == "fb" and row["feeds"][0]["part"] == "U2"
    assert row["fb"]["from"] == "get_power_tree's feedback row"
    assert row["fb"]["implied_vref"] == pytest.approx(5 * 10 / 62.3, abs=1e-3)
    assert row["fb"]["near_common_vref"] == 0.8
    given = _divider(_run(board, vref={"U2": 0.8}), "FB1")
    assert given["fb"]["vout_computed"] == pytest.approx(0.8 * 6.23, abs=1e-3)


def test_enable_divider_trip_voltage():
    board = [buck(en="EN1"), ("L1", {"1": ("1", "SW1"), "2": ("2", "5V")},
                                 {"description": "Inductor"}),
             res("R1", "5V", "FB1", "52.3k"), res("R2", "FB1", "GND", "10k"),
             res("R3", "12V", "EN1", "100k"), res("R4", "EN1", "GND", "10k")]
    row = _divider(_run(board), "EN1")
    assert row["feeds"][0]["use"] == "en"
    threshold = row["thresholds"][0]
    assert threshold["v_top_at_trip_examples"] == {"1.2 V": 13.2, "0.8 V": 8.8}
    assert "examples only" in threshold["note"]
    given = _divider(_run(board, vth={"U2": 1.25}), "EN1")["thresholds"][0]
    assert (given["v_th"], given["v_top_at_trip"]) == (1.25, 13.75)
    everyone = _divider(_run(board, vth=1.0), "EN1")["thresholds"][0]
    assert everyone["v_top_at_trip"] == 11.0
    # The EN pin is judged against the converter's input (12 V): no finding at 1.09 V.
    assert not _findings(_run(board), "divider_output_above_input_rating")


def test_comparator_and_supervisor_inputs_take_vth():
    board = [("U5", {"1": ("V+", "5V", "Power"), "2": GND, "3": ("IN+", "SENSE1", "Input"),
                     "4": ("IN-", "REF1", "Input"), "5": ("OUT", "TRIP", "OpenCollector")},
              {"description": "Low-power comparator"}),
             res("R1", "24V", "SENSE1", "200k"), res("R2", "SENSE1", "GND", "10k")]
    row = _divider(_run(board, vth={"U5": 1.2}), "SENSE1")
    assert row["feeds"][0]["use"] == "comparator"
    assert row["thresholds"][0]["v_top_at_trip"] == pytest.approx(1.2 * 21, abs=1e-3)
    plain = _divider(_run(board), "SENSE1")["thresholds"][0]
    assert "pass vth=" in plain["note"] and "v_top_at_trip" not in plain


def test_shunt_reference_divider():
    board = [("U6", {"1": ("REF", "REFDIV"), "2": ("K", "OPTO_K"), "3": ("A", "GND")},
              {"description": "Adjustable precision shunt regulator TL431"}),
             res("R1", "5V", "REFDIV", "10k"), res("R2", "REFDIV", "GND", "10k")]
    row = _divider(_run(board), "REFDIV")
    assert row["feeds"][0]["use"] == "reference"
    assert row["reference"]["implied_vref"] == 2.5 and row["reference"]["near_common_vref"] == 2.5
    given = _divider(_run(board, vref={"U6": 2.495}), "REFDIV")["reference"]
    assert given["v_top_computed"] == 4.99
    # One vref for all is the converters' Vref, not the reference's.
    assert "vref_given" not in _divider(_run(board, vref=0.8), "REFDIV")["reference"]


def test_op_amp_feedback_divider_gives_the_gain():
    board = [("U7", {"1": ("V+", "5V", "Power"), "2": GND, "3": ("IN+", "SIG", "Input"),
                     "4": ("IN-", "FBN", "Input"), "5": ("OUT", "AMP_OUT", "Output")},
              {"description": "Rail-to-rail operational amplifier"}),
             res("R1", "AMP_OUT", "FBN", "9k"), res("R2", "FBN", "GND", "1k")]
    row = _divider(_run(board), "FBN")
    assert row["feeds"][0]["use"] == "opamp"
    assert row["opamp"]["noninverting_gain"] == 10.0


def test_transistor_base_divider_from_a_gpio():
    board = [mcu(PA1="BASE_DRV"), res("R1", "BASE_DRV", "QB", "1k"), res("R2", "QB", "GND", "10k"),
             ("Q1", {"1": ("B", "QB"), "2": ("E", "GND"), "3": ("C", "LOAD")},
              {"description": "NPN transistor"})]
    row = _divider(_run(board), "QB")
    assert row["feeds"][0]["use"] == "transistor" and row["feeds"][0]["detail"] == "base"
    assert row["inputs"]["V_top"]["volts"] == 3.3
    assert row["inputs"]["V_top"]["source"].startswith("I/O rail of U1")
    assert row["result"]["v_mid"] == 3.0
    assert any("driven signal" in note for note in row["notes"])
    assert any("clamps" in note for note in row["notes"])
    assert row["confidence"] == "medium"


def test_unknown_top_voltage_asks_for_rails():
    board = _adc_board(rail="VSYS")
    result = _run(board)
    row = _divider(result, "VMON")
    assert "v_mid" not in row["result"] and row["result"]["ratio"] == 0.3
    assert result["voltages_needed"] == [{"net": "VSYS", "calculations": 1}]
    given = _run(board, rails={"VSYS": 10.0})
    assert _divider(given, "VMON")["result"]["v_mid"] == 3.0
    assert "voltages_needed" not in given


def test_divider_feeding_an_input_through_a_series_resistor():
    board = [mcu(**{"PA0/ADC1_IN0": "ADC_IN"}), res("R1", "12V", "DIV", "20k"),
             res("R2", "DIV", "GND", "10k"), res("R3", "DIV", "ADC_IN", "1k"),
             cap("C1", "ADC_IN", value="100nF")]
    result = _run(board)
    row = _divider(result, "DIV")
    assert row["feeds"][0] == {"use": "adc", "part": "U1", "pin": "10", "name": "PA0",
                               "detail": row["feeds"][0]["detail"], "net": "ADC_IN",
                               "through": ["R3"]}
    assert "also_on_midpoint" not in row
    adc = next(r for r in _rows(result, "adc_input") if r["net"] == "ADC_IN")
    r_total = 1000 + 20e3 * 10e3 / 30e3
    assert adc["inputs"]["R"]["ohms"] == pytest.approx(r_total, abs=0.01)
    assert adc["divider"]["midpoint"] == "DIV" and adc["divider"]["through"] == "R3"
    assert adc["result"]["cutoff_hz"] == pytest.approx(1 / (2 * math.pi * r_total * 100e-9),
                                                       rel=1e-3)


def test_unreadable_values_and_not_fitted_parts():
    board = [*_adc_board(), res("R9", "12V", "VMON2", "TBD"), res("R10", "VMON2", "GND"),
             ("U9", {"1": ("IN", "VMON2", "Input"), "2": GND}, {"description": "Buffer"})]
    row = _divider(_run(board), "VMON2")
    assert row["confidence"] == "low" and "could not be read" in row["notes"][0]
    plan = VariantPlan(name="Lite", not_fitted={"R2"})
    result = _run(_adc_board(), plan=plan)
    assert not [r for r in _rows(result, "divider") if r["midpoint"] == "VMON"]
    assert result["variant"] == "Lite"


# ---------- LEDs ----------

def _led_board(r="330", rail="5V", description="LED, red, 0603", extra=(), **info):
    return [led("LED1", "LED1_A", "GND", description, **info), res("R21", rail, "LED1_A", r),
            ("J1", {"1": ("1", rail), "2": ("2", "GND")}, {"description": "Power connector"}),
            *extra]


def test_led_current_from_a_rail():
    result = _run(_led_board())
    row = _rows(result, "led")[0]
    assert row["colour"] == "red" and row["colour_source"] == "text"
    assert row["inputs"]["V_supply"]["volts"] == 5.0
    assert row["inputs"]["Vf"] == {"volts": 2.0, "source": "typical for red", "typical": True}
    assert row["inputs"]["R"]["parts"] == ["R21"]
    assert row["formula"] == "I = (V_supply - V_sink - Vf) / R"
    assert row["result"]["current_ma"] == pytest.approx(3 / 330 * 1000, rel=1e-3)
    assert row["result"]["resistor_power_mw"]["R21"] == pytest.approx(
        (3 / 330) ** 2 * 330 * 1e3, rel=1e-3)
    assert row["result"]["rating_ma"] == 20.0
    assert row["confidence"] == "medium"  # a typical Vf
    assert result["summary"]["leds"]["current_computed"] == 1


@pytest.mark.parametrize("description, vf", [
    ("LED red", 2.0), ("LED orange", 2.1), ("LED amber", 2.1), ("LED yellow", 2.1),
    ("LED green", 2.2), ("LED true green", 3.0), ("LED blue", 3.0), ("LED white", 3.0),
    ("IR LED", 1.3),
])
def test_led_vf_by_colour(description, vf):
    row = _rows(_run(_led_board(description=description)), "led")[0]
    assert row["inputs"]["Vf"]["volts"] == vf
    assert row["result"]["current_ma"] == pytest.approx((5 - vf) / 330 * 1000, rel=1e-3)


def test_led_vf_given_per_part_and_per_colour_and_stated():
    by_part = _rows(_run(_led_board(), vf={"LED1": 2.9}), "led")[0]
    assert by_part["inputs"]["Vf"] == {"volts": 2.9, "source": "vf argument (part)"}
    by_colour = _rows(_run(_led_board(), vf={"red": 1.8}), "led")[0]
    assert by_colour["inputs"]["Vf"]["volts"] == 1.8
    stated = _rows(_run(_led_board(params={"forward voltage": "1.95 V"})), "led")[0]
    assert stated["inputs"]["Vf"]["source"] == "stated: parameter (forward voltage)"


def test_led_of_unknown_colour_gives_the_current_against_vf():
    result = _run(_led_board(description="Chip LED"))
    row = _rows(result, "led")[0]
    assert "current_ma" not in row["result"]
    assert row["result"]["current_ma_at_vf"] == {"2 V": pytest.approx(9.091, rel=1e-3),
                                                 "3 V": pytest.approx(6.061, rel=1e-3)}
    assert "resistor_power_mw_at_vf_2" in row["result"]
    assert result["summary"]["leds"]["current_vs_vf"] == 1


def test_led_driven_by_a_gpio_and_sunk_by_a_gpio():
    source = [mcu(PA5="LED_DRV"), res("R1", "LED_DRV", "LED1_A", "1k"),
              led("LED1", "LED1_A", "GND", "LED green")]
    row = _rows(_run(source), "led")[0]
    assert row["inputs"]["V_supply"]["volts"] == 3.3
    assert row["inputs"]["V_supply"]["via"].startswith("gpio U1.")
    assert row["result"]["current_ma"] == pytest.approx(1.1, abs=1e-3)
    assert any("GPIO" in note for note in row["notes"])
    sink = [mcu(PA5="LED_K"), res("R1", "LED1_K", "LED_K", "1k"),
            led("LED1", "3V3", "LED1_K", "LED green")]
    row = _rows(_run(sink), "led")[0]
    assert row["inputs"]["V_supply"]["net"] == "3V3"
    assert row["inputs"]["V_sink"]["via"].startswith("gpio U1.") and \
        row["inputs"]["V_sink"]["volts"] == 0.0
    assert row["result"]["current_ma"] == pytest.approx(1.1, abs=1e-3)


def test_led_switched_by_a_transistor():
    board = [led("LED1", "LED1_A", "LED1_K", "LED blue"), res("R1", "12V", "LED1_A", "1k"),
             ("Q1", {"1": ("G", "Q_GATE"), "2": ("S", "GND"), "3": ("D", "LED1_K")},
              {"description": "N-channel MOSFET"})]
    row = _rows(_run(board), "led")[0]
    assert row["inputs"]["V_sink"]["via"].startswith("transistor Q1.3 (D) low-side switch")
    assert row["result"]["current_ma"] == 9.0


def test_led_supply_unknown_gives_the_formula():
    result = _run(_led_board(rail="VLED"))
    row = _rows(result, "led")[0]
    assert row["result"]["current_formula"] == "I = (V(VLED) - 2 V) / 330 ohm"
    assert result["voltages_needed"] == [{"net": "VLED", "calculations": 1}]
    assert row["confidence"] == "low"


@pytest.mark.parametrize("r, severity", [
    ("150", None),  # 20.0 mA: at the typical rating
    ("149", "info"),  # 20.1 mA: above it, under 30 mA
    ("100", "info"),  # 30.0 mA: not above 30 mA
    ("99", "warning"),  # 30.3 mA with a known Vf
])
def test_led_overcurrent_thresholds(r, severity):
    found = _findings(_run(_led_board(r=r)), "led_overcurrent")
    assert [row["severity"] for row in found] == ([severity] if severity else [])


def test_led_overcurrent_against_a_stated_rating_and_an_unknown_colour():
    assert not _findings(_run(_led_board(r="99", params={"forward current": "50mA"})),
                         "led_overcurrent")
    # A rating a parameter states, exceeded with a known Vf: a warning below 30 mA too.
    tight = _findings(_run(_led_board(r="330", params={"forward current": "5mA"})),
                      "led_overcurrent")
    assert [row["severity"] for row in tight] == ["warning"] and tight[0]["rating_ma"] == 5.0
    # A current read from the text is often the test current: info below 30 mA.
    texted = _findings(_run(_led_board(r="330", description="LED red, 5mA")),
                       "led_overcurrent")
    assert [row["severity"] for row in texted] == ["info"]
    # Unknown colour: the stated rating alone does not make it a warning.
    unknown_vf = _findings(_run(_led_board(r="330", description="Chip LED",
                                           params={"forward current": "5mA"})),
                           "led_overcurrent")
    assert [row["severity"] for row in unknown_vf] == ["info"]
    unknown = _findings(_run(_led_board(r="47", description="Chip LED")), "led_overcurrent")
    assert [row["severity"] for row in unknown] == ["info"]
    assert "colour is not known" in unknown[0]["issue"]
    assert not _findings(_run(_led_board(r="150", description="Chip LED")), "led_overcurrent")


def test_resistor_overpower_at_the_led_current():
    small = {"description": "RES 100R 0402"}
    board = [led("LED1", "LED1_A", "GND"), res("R1", "5V", "LED1_A", "100", **small)]
    found = _findings(_run(board), "resistor_overpower")  # 30 mA: 90 mW in 62.5 mW
    assert [row["severity"] for row in found] == ["warning"]
    assert found[0]["power_mw"] == 90.0 and found[0]["rating_mw"] == 62.5
    derated = [led("LED1", "LED1_A", "GND"), res("R1", "5V", "LED1_A", "220", **small)]
    found = _findings(_run(derated), "resistor_overpower")  # 13.6 mA: 41 mW, above 31 mW
    assert [row["severity"] for row in found] == ["info"]
    cool = [led("LED1", "LED1_A", "GND"), res("R1", "5V", "LED1_A", "1k", **small)]
    assert not _findings(_run(cool), "resistor_overpower")


def test_led_without_a_series_resistor():
    board = [led("LED1", "5V", "GND")]
    row = _rows(_run(board), "led")[0]
    assert "no series resistor" in row["notes"][0] and row["confidence"] == "low"


def test_led_polarity_from_the_circuit_when_pins_are_numbered():
    board = [("D1", {"1": ("1", "D1_N"), "2": ("2", "GND")}, {"description": "LED red"}),
             res("R1", "5V", "D1_N", "330")]
    row = _rows(_run(board), "led")[0]
    assert (row["anode"], row["cathode"]) == ("D1_N", "GND")
    assert row["result"]["current_ma"] == pytest.approx(9.091, rel=1e-3)
    assert any("polarity read from the circuit" in note for note in row["notes"])
    sink = [mcu(PA5="LED_K"), ("D1", {"1": ("1", "3V3"), "2": ("2", "D1_N")},
                               {"description": "LED red"}), res("R1", "D1_N", "LED_K", "1k")]
    row = _rows(_run(sink), "led")[0]
    assert (row["anode"], row["cathode"]) == ("3V3", "D1_N")


def test_multi_colour_led_by_its_common_pin():
    board = [("LED1", {"1": ("A", "3V3"), "2": ("R", "NR"), "3": ("G", "NG"), "4": ("B", "NB")},
              {"description": "RGB LED common anode"}),
             res("R1", "NR", "GND", "100"), res("R2", "NG", "GND", "100"),
             res("R3", "NB", "GND", "100")]
    rows = _rows(_run(board), "led")
    assert {(row["channel"], row["colour"]) for row in rows} == {
        ("R", "red"), ("G", "green"), ("B", "blue")}
    by = {row["colour"]: row["result"]["current_ma"] for row in rows}
    assert by == {"red": 13.0, "green": 11.0, "blue": pytest.approx(3.0, abs=1e-6)}


def test_led_with_its_own_driver_is_counted_not_calculated():
    board = [("LED1", {"1": ("VDD", "5V", "Power"), "2": ("DOUT", "D2"), "3": ("VSS", "GND"),
                       "4": ("DIN", "D1")}, {"description": "Addressable RGB LED"})]
    result = _run(board)
    assert not _rows(result, "led")
    assert result["summary"]["leds"]["with_own_driver_skipped"] == 1


# ---------- optocouplers ----------

def _opto_board(pins=None, extra=()):
    pins = pins or {"1": ("A", "OPTO_A"), "2": ("K", "GND"), "3": ("E", "GND"),
                    "4": ("C", "OPTO_OUT")}
    return [("U5", pins, {"description": "Phototransistor optocoupler"}),
            mcu(PA5="OPTO_DRV"), res("R1", "OPTO_DRV", "OPTO_A", "330"),
            res("R2", "3V3", "OPTO_OUT", "10k"), *extra]


def test_optocoupler_input_current_and_output():
    result = _run(_opto_board())
    row = _rows(result, "opto")[0]
    assert row["inputs"]["Vf"]["volts"] == 1.2
    assert row["formula"] == "I_F = (V_supply - V_sink - Vf) / R"
    assert row["result"]["if_ma"] == pytest.approx(2.1 / 330 * 1000, rel=1e-3)
    assert row["output"]["load"] == "R2" and row["output"]["rail"] == "3V3"
    assert row["output"]["i_out_sat_ma"] == 0.33
    assert row["result"]["ctr_needed_percent"] == pytest.approx(0.33 / 6.364 * 100, abs=0.1)
    found = _findings(result, "opto_no_input_shunt", "info")
    assert len(found) == 1 and found[0]["designators"] == ["U5"]
    assert result["summary"]["optos"] == {"parts": 1, "channels": 1, "without_input_shunt": 1,
                                          "unread": 0}


def test_optocoupler_with_an_input_shunt():
    with_r = _run(_opto_board(extra=[res("R3", "OPTO_A", "GND", "10k")]))
    row = _rows(with_r, "opto")[0]
    assert row["input_shunt"] == {"resistors": ["R3"]}
    assert not _findings(with_r, "opto_no_input_shunt")
    with_c = _run(_opto_board(extra=[cap("C3", "OPTO_A", "GND", "1nF")]))
    assert _rows(with_c, "opto")[0]["input_shunt"] == {"capacitors": ["C3"]}
    assert not _findings(with_c, "opto_no_input_shunt")


def test_optocoupler_with_unnamed_pins_takes_the_standard_pinout():
    pins = {"1": ("1", "OPTO_A"), "2": ("2", "GND"), "3": ("3", "GND"), "4": ("4", "OPTO_OUT")}
    row = _rows(_run(_opto_board(pins=pins)), "opto")[0]
    assert row["confidence"] == "low" and any("standard pinout" in n for n in row["notes"])
    assert row["result"]["if_ma"] == pytest.approx(6.364, rel=1e-3)
    odd = {"1": ("X1", "OPTO_A"), "2": ("X2", "GND"), "3": ("X3", "GND"),
           "4": ("X4", "OPTO_OUT"), "5": ("X5", "N5")}
    result = _run(_opto_board(pins=odd))
    assert result["summary"]["optos"]["unread"] == 1


def test_optocoupler_by_part_number_family():
    board = [("U5", {"1": ("A", "OPTO_A"), "2": ("K", "GND"), "3": ("E", "GND"),
                     "4": ("C", "OPTO_OUT")}, {"description": "PC817"}),
             res("R1", "5V", "OPTO_A", "1k")]
    row = _rows(_run(board), "opto")[0]
    assert row["result"]["if_ma"] == 3.8


# ---------- ADC inputs and RC filters ----------

def test_adc_input_with_an_rc_filter():
    board = [mcu(**{"PA0/ADC1_IN0": "AIN_F"}), res("R1", "SENSOR_OUT", "AIN_F", "1k"),
             cap("C1", "AIN_F", value="100nF"),
             ("U8", {"1": ("VDD", "3V3", "Power"), "2": GND, "3": ("VOUT", "SENSOR_OUT",
                                                                   "Output")},
              {"description": "Temperature sensor"})]
    result = _run(board)
    row = _rows(result, "adc_input")[0]
    assert row["filter"] == "rc" and row["part"] == "U1"
    assert row["inputs"]["R"]["parts"] == ["R1"] and row["inputs"]["C"]["value"] == "100nF"
    assert row["formula"] == "fc = 1 / (2 pi R C)"
    assert row["result"]["cutoff_hz"] == pytest.approx(1 / (2 * math.pi * 1e3 * 100e-9), rel=1e-3)
    assert row["result"]["cutoff"] == "1.59 kHz"
    assert row["source_side"]["parts"] == ["U8"]
    assert not _findings(result, "adc_no_filter")
    assert result["summary"]["adc_inputs"]["by_filter"] == {"rc": 1}
    assert any("sample capacitor" in note for note in result["notes"])


def test_adc_input_without_a_filter():
    board = [mcu(**{"PA1": "VSENSE"}),
             ("J2", {"1": ("1", "VSENSE"), "2": ("2", "GND")}, {"description": "Header"})]
    result = _run(board)
    row = _rows(result, "adc_input")[0]
    assert row["filter"] == "none" and row["basis"].startswith("processor analog pin")
    found = _findings(result, "adc_no_filter", "info")
    assert len(found) == 1 and found[0]["net"] == "VSENSE"


def test_adc_part_input_with_a_capacitor_only():
    board = [("U9", {"1": ("VDD", "3V3", "Power"), "2": GND, "3": ("AIN0", "CH0"),
                     "4": ("SCLK", "SCK"), "5": ("REFIN", "VREF_ADC")},
              {"description": "16-bit delta-sigma ADC"}),
             cap("C1", "CH0", value="10nF"),
             ("J2", {"1": ("1", "CH0"), "2": ("2", "GND")}, {"description": "Header"})]
    result = _run(board)
    rows = _rows(result, "adc_input")
    assert [(row["pin"], row["filter"]) for row in rows] == [("3", "c_only")]
    assert "source" in rows[0]["result"]["note"]
    assert not _findings(result, "adc_no_filter")


def test_digital_pin_and_dac_are_not_adc_inputs():
    board = [mcu(**{"PA4/DAC_OUT1": "DAC_OUT", "PA5": "LED_CTRL"}),
             ("J2", {"1": ("1", "DAC_OUT"), "2": ("2", "LED_CTRL")}, {"description": "Header"})]
    assert not _rows(_run(board), "adc_input")


def test_divider_at_an_adc_input_gives_the_filter_its_source_impedance():
    board = _adc_board(top="20k", bottom="10k", extra=[cap("C1", "VMON", value="1uF")])
    result = _run(board)
    row = next(r for r in _rows(result, "adc_input") if r["net"] == "VMON")
    assert row["filter"] == "rc" and row["divider"]["midpoint"] == "VMON"
    assert row["inputs"]["R"]["ohms"] == pytest.approx(20e3 / 3, abs=0.01)
    assert row["result"]["cutoff_hz"] == pytest.approx(1 / (2 * math.pi * 20e3 / 3 * 1e-6),
                                                       rel=1e-3)


def test_rc_filter_between_two_ics():
    board = [mcu(PB0=("PWM_RAW", "Output")), res("R1", "PWM_RAW", "PWM_F", "10k"),
             cap("C1", "PWM_F", value="10nF"),
             ("U3", {"1": ("VDD", "3V3", "Power"), "2": GND, "3": ("IN", "PWM_F", "Input")},
              {"description": "Logic buffer"})]
    result = _run(board)
    row = _rows(result, "rc_filter")[0]
    assert row["net"] == "PWM_F" and row["from"] == "PWM_RAW"
    assert row["result"]["cutoff_hz"] == pytest.approx(1591.55, rel=1e-3)
    assert row["drivers"][0].startswith("U1.") and row["receivers"] == ["U3.3 (IN)"]
    assert result["summary"]["rc_filters"]["total"] == 1


def test_rc_filter_with_a_pull_takes_the_thevenin_resistance():
    board = [mcu(PB0=("BTN_RAW", "Output")), res("R1", "BTN_RAW", "BTN_F", "10k"),
             res("R2", "BTN_F", "3V3", "10k"), cap("C1", "BTN_F", value="100nF"),
             ("U3", {"1": ("VDD", "3V3", "Power"), "2": GND, "3": ("IN", "BTN_F", "Input")},
              {"description": "Logic buffer"})]
    row = _rows(_run(board), "rc_filter")[0]
    assert row["inputs"]["R"]["value"] == "5k" and "Thevenin" in row["inputs"]["R"]["source"]


def test_no_rc_filter_without_a_driver_or_with_a_diode():
    board = [res("R1", "FLOAT", "NODE", "10k"), cap("C1", "NODE", value="10nF"),
             ("U3", {"1": ("VDD", "3V3", "Power"), "2": GND, "3": ("IN", "NODE", "Input")},
              {"description": "Logic buffer"})]
    assert not _rows(_run(board), "rc_filter")
    clamp = [mcu(PB0=("RAW", "Output")), res("R1", "RAW", "NODE", "10k"),
             cap("C1", "NODE", value="10nF"), ("D1", {"1": ("A", "NODE"), "2": ("K", "3V3")},
                                               {"description": "Schottky diode"}),
             ("U3", {"1": ("VDD", "3V3", "Power"), "2": GND, "3": ("IN", "NODE", "Input")},
              {"description": "Logic buffer"})]
    assert not _rows(_run(clamp), "rc_filter")


# ---------- filters, paging, errors ----------

def _big_board():
    board = []
    for n in range(30):
        board += [led(f"LED{n}", f"A{n}", "GND"), res(f"R{n}", "5V", f"A{n}", "330")]
    return board


def test_kinds_parts_and_nets_narrow_the_result():
    board = [*_adc_board(), *_led_board()[:2]]
    only_leds = _run(board, kinds=["leds"])
    assert {row["kind"] for row in only_leds["calculations"]} == {"led"}
    assert set(only_leds["summary"]) == {"leds", "findings"}
    by_part = _run(board, parts=["R2"])
    assert [row["kind"] for row in by_part["calculations"]] == ["divider", "adc_input"]
    by_net = _run(board, nets=["LED1_A"])
    assert [row["kind"] for row in by_net["calculations"]] == ["led"]
    missing = _run(board, parts=["R2", "Q77"])
    assert any("Not found: Q77" in note for note in missing["notes"])


def test_errors():
    board = _adc_board()
    assert _run(board, kinds=["bogus"])["error"] == "invalid_kinds"
    assert _run(board, parts=["Q77"])["error"] == "part_not_found"
    assert _run(board, nets=["NOPE"])["error"] == "net_not_found"
    assert "rails names no net" in " ".join(_run(board, rails={"NOPE": 3.0})["notes"])


def test_paging_calculations_and_findings_together():
    first = _run(_big_board(), limit=10)
    assert first["calculations_total"] == 30 and len(first["calculations"]) == 10
    assert first["has_more"] and first["next_offset"] == 10
    rest = _run(_big_board(), offset=20, limit=10)
    assert not rest["has_more"] and len(rest["calculations"]) == 10
    seen = {row["led"] for page in (0, 10, 20)
            for row in _run(_big_board(), offset=page, limit=10)["calculations"]}
    assert len(seen) == 30


# ---------- tool ----------

@pytest.fixture
def board(monkeypatch, tmp_path: Path):
    netlist = _netlist([*_adc_board(), *_led_board()[:2]])
    monkeypatch.setattr(main_mod, "_project", {
        "name": "Demo", "root_dir": str(tmp_path), "prj_pcb_path": str(tmp_path / "D.PrjPcb"),
        "sheets": [], "pcb_doc_paths": [],
    })
    monkeypatch.setattr(main_mod._altium, "_netlist", netlist)
    monkeypatch.setattr(main_mod, "_variant_state",
                        VariantState([VariantDefinition(name="Default")]))
    return netlist


def test_get_circuit_calculations_tool(board):
    result = json.loads(main_mod.get_circuit_calculations())
    assert result["variant"] == "Default" and "netlist_updated_utc" in result
    assert result["summary"]["dividers"]["total"] == 1 and "page_limited_to" not in result
    narrowed = json.loads(main_mod.get_circuit_calculations(
        part="LED1", vf="LED1=2.5", rails="5V=5", kinds=["leds"]))
    assert narrowed["calculations"][0]["inputs"]["Vf"]["volts"] == 2.5
    trip = json.loads(main_mod.get_circuit_calculations(net="VMON", vth="1.2", vref="0.6"))
    assert trip["calculations"][0]["midpoint"] == "VMON"


def test_get_circuit_calculations_tool_errors(board, monkeypatch):
    assert json.loads(main_mod.get_circuit_calculations(vref="nope"))["error"] == "invalid_vref"
    assert json.loads(main_mod.get_circuit_calculations(rails="VIO"))["error"] == "invalid_rails"
    assert json.loads(main_mod.get_circuit_calculations(vf="red"))["error"] == "invalid_vf"
    assert json.loads(main_mod.get_circuit_calculations(vth="x"))["error"] == "invalid_vth"
    assert json.loads(main_mod.get_circuit_calculations(kinds=["x"]))["error"] == "invalid_kinds"
    assert json.loads(main_mod.get_circuit_calculations(part="Q99"))["error"] == "part_not_found"
    assert json.loads(main_mod.get_circuit_calculations(net="NOPE"))["error"] == "net_not_found"
    monkeypatch.setattr(main_mod._altium, "_netlist", {**board, "components": {}})
    assert json.loads(main_mod.get_circuit_calculations())["error"] == "no_components_loaded"
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.get_circuit_calculations())["error"] == "no_project"


def test_get_circuit_calculations_shrinks_to_fit(board, monkeypatch):
    monkeypatch.setattr(main_mod._altium, "_netlist", _netlist(_big_board()))
    full = main_mod.get_circuit_calculations()
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", len(full) // 3)
    service = main_mod.prepare_circuit_calculations.__globals__
    builds = []
    build = service["build_tree"]
    monkeypatch.setitem(service, "build_tree", lambda *a, **k: builds.append(1) or build(*a, **k))
    result = json.loads(main_mod.get_circuit_calculations())
    assert builds == [1]  # read once, rendered per shrink step
    assert result["page_limited_to"] < 100 and result["has_more"]
    assert len(json.dumps(result, indent=2)) <= len(full) // 3
    rest = json.loads(main_mod.get_circuit_calculations(offset=result["next_offset"]))
    assert rest["calculations"][0]["led"] != result["calculations"][0]["led"]


def test_tool_registration_manifest_readme_and_instructions():
    tools = {tool.name: tool for tool in asyncio.run(main_mod.mcp.list_tools())}
    tool = tools["get_circuit_calculations"]
    assert tool.annotations.readOnlyHint is True and tool.title == "Get Circuit Calculations"
    assert tool.meta["anthropic/maxResultSizeChars"] == main_mod.QUERY_NET_MAX_RESULT_SIZE_CHARS
    assert set(tool.inputSchema["properties"]) == {"part", "net", "kinds", "rails", "vref",
                                                   "vf", "vth", "offset"}
    assert "datasheet" in tool.description and "next_offset" in tool.description
    assert "get_circuit_calculations" in main_mod.NETLIST_TOOLS
    assert main_mod.SERVER_INSTRUCTIONS.index("get_circuit_calculations") > 4096
    assert "get_circuit_calculations" in main_mod.SCHEMATIC_REVIEW_PROMPT
    root = Path(__file__).parents[1]
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    assert "get_circuit_calculations" in {entry["name"] for entry in manifest["tools"]}
    assert "| `get_circuit_calculations` |" in (root / "README.md").read_text("utf-8")
    assert "`get_circuit_calculations`" in (root / "CHANGELOG.md").read_text("utf-8")


# ---------- more topologies ----------

def test_led_on_an_led_driver_pin_is_set_by_the_driver():
    board = [led("LED1", "5V", "LED1_K", "LED white"),
             ("U3", {"1": ("VIN", "5V", "Power"), "2": GND, "3": ("OUT1", "LED1_K", "Output")},
              {"description": "Constant-current LED driver"})]
    row = _rows(_run(board), "led")[0]
    assert "current_ma" not in row["result"]
    assert any("LED driver" in note for note in row["notes"])


def test_led_with_its_anode_on_ground_is_flagged_in_its_notes():
    board = [led("LED1", "GND", "LED1_K"), res("R21", "LED1_K", "5V", "330")]
    row = _rows(_run(board), "led")[0]
    assert row["result"]["current_ma"] < 0
    assert any("anode is on ground" in note for note in row["notes"])


def test_diode_across_an_optocoupler_input_is_not_in_its_path():
    board = _opto_board(extra=[("D2", {"1": ("A", "GND"), "2": ("K", "OPTO_A")},
                                {"description": "Switching diode"})])
    row = _rows(_run(board), "opto")[0]
    assert row["diode_across"] == ["D2"]
    assert row["result"]["if_ma"] == pytest.approx(6.364, rel=1e-3)


def test_optocoupler_whose_cathode_is_named_c_takes_the_standard_pinout():
    pins = {"1": ("A", "OPTO_A"), "2": ("C", "GND"), "3": ("E", "GND"), "4": ("COL", "OPTO_OUT")}
    row = _rows(_run(_opto_board(pins=pins)), "opto")[0]
    assert row["confidence"] == "low"
    assert row["output"]["load"] == "R2"
    disagree = {"1": ("E", "OPTO_A"), "2": ("C", "GND"), "3": ("A", "GND"),
                "4": ("COL", "OPTO_OUT")}
    assert _run(_opto_board(pins=disagree))["summary"]["optos"]["unread"] == 1


# ---------- resistor stacks, chains and selectable legs ----------

def _window_board():
    return [("U9", {"1": ("VDD", "3V3", "Power"), "2": GND, "3": ("INA+", "TAPA", "Input"),
                    "4": ("INB-", "TAPB", "Input"), "5": ("OUTA", "WIN", "OpenCollector")},
             {"description": "Window comparator supervisor"}),
            res("R1", "12V", "TAPA", "100k"), res("R2", "TAPA", "TAPB", "2k"),
            res("R3", "TAPB", "GND", "10k")]


def test_each_tap_of_a_resistor_stack_is_a_divider():
    result = _run(_window_board(), vth=0.4)
    upper, lower = _divider(result, "TAPA"), _divider(result, "TAPB")
    assert upper["inputs"]["R_bottom"]["parts"] == ["R2", "R3"]
    assert upper["result"]["ratio"] == pytest.approx(12 / 112, abs=1e-5)
    assert lower["inputs"]["R_top"]["parts"] == ["R2", "R1"]
    assert lower["result"]["ratio"] == pytest.approx(10 / 112, abs=1e-5)
    assert upper["feeds"][0]["use"] == "supervisor" and lower["feeds"][0]["pin"] == "4"
    assert upper["taps"][0]["net"] == "TAPB" and upper["taps"][0]["leg"] == "bottom"
    assert upper["thresholds"][0]["v_top_at_trip"] == pytest.approx(0.4 * 112 / 12, abs=1e-3)
    assert lower["thresholds"][0]["v_top_at_trip"] == pytest.approx(0.4 * 112 / 10, abs=1e-3)


def test_enable_divider_with_an_open_drain_hysteresis_tap():
    board = [buck(en="EN1"), ("L1", {"1": ("1", "SW1"), "2": ("2", "5V")},
                                 {"description": "Inductor"}),
             res("R5", "5V", "FB1", "52.3k"), res("R6", "FB1", "GND", "10k"),
             res("R1", "12V", "EN1", "100k"), res("R2", "EN1", "HYS", "10k"),
             res("R3", "HYS", "GND", "10k"),
             ("U3", {"1": ("VDD", "3V3", "Power"), "2": GND,
                     "3": ("OUT", "HYS", "OpenCollector")}, {"description": "Logic gate"})]
    row = _divider(_run(board, vth={"U2": 1.2}), "EN1")
    assert row["result"]["ratio"] == pytest.approx(20 / 120, abs=1e-5)
    tap = row["taps"][0]
    assert tap["net"] == "HYS"
    assert tap["ratio_when_sinking"] == pytest.approx(10 / 110, abs=1e-5)
    assert row["thresholds"][0]["v_top_at_trip"] == 7.2
    assert row["thresholds"][0]["v_top_at_trip_when_sinking"] == 13.2
    assert any("switches the stack" in note for note in row["notes"])


@pytest.mark.parametrize("probe", [
    ("TP1", {"1": ("1", "XN")}, {"description": "Test point"}),
    ("J7", {"1": ("1", "XN"), "2": ("2", "GND")}, {"description": "Header 2 pin"}),
])
def test_feedback_chain_through_a_test_point_or_header_pin(probe):
    board = [buck(), ("L1", {"1": ("1", "SW1"), "2": ("2", "5V")}, {"description": "Inductor"}),
             cap("C1", "5V"), res("R1", "5V", "XN", "40k"), probe,
             res("R2", "XN", "FB1", "12.3k"), res("R3", "FB1", "GND", "10k")]
    result = _run(board)
    row = _divider(result, "FB1")
    assert row["inputs"]["R_top"]["parts"] == ["R2", "R1"]
    assert row["result"]["ratio"] == pytest.approx(10 / 62.3, abs=1e-5)
    assert row["fb"]["implied_vref"] == pytest.approx(5 * 10 / 62.3, abs=1e-3)
    assert any("passes XN" in note for note in row["notes"])
    assert row["confidence"] == "medium"
    assert [r["midpoint"] for r in _rows(result, "divider")] == ["FB1"]  # XN is inside


def test_feedback_vref_from_a_top_voltage_given_in_rails():
    board = [buck(), ("L1", {"1": ("1", "SW1"), "2": ("2", "VOUT_A")},
                             {"description": "Inductor"}), cap("C1", "VOUT_A"),
             res("R1", "VOUT_A", "FB1", "52.3k"), res("R2", "FB1", "GND", "10k")]
    assert "implied_vref" not in _divider(_run(board), "FB1")["fb"]
    fb = _divider(_run(board, rails={"VOUT_A": 5.0}), "FB1")["fb"]
    assert fb["implied_vref"] == pytest.approx(5 * 10 / 62.3, abs=1e-3)
    assert fb["near_common_vref"] == 0.8 and fb["top_source"] == "VOUT_A: user"


def test_three_terminal_regulator_vref_sits_across_the_top_resistor():
    board = [("U3", {"1": ("ADJ", "ADJ1"), "2": ("VOUT", "VOUT_B"), "3": ("VIN", "5V")},
              {"description": "Adjustable LDO regulator"}),
             res("R1", "VOUT_B", "ADJ1", "120"), res("R2", "ADJ1", "GND", "200"),
             cap("C1", "VOUT_B", value="10uF")]
    fb = _divider(_run(board, rails={"VOUT_B": 3.3}), "ADJ1")["fb"]
    assert fb["vout_over_vref"] == pytest.approx(320 / 120, abs=1e-4)
    assert fb["implied_vref"] == pytest.approx(3.3 * 120 / 320, abs=1e-4)  # not 3.3 x ratio


def test_jumper_selected_feedback_legs():
    board = [buck(), ("L1", {"1": ("1", "SW1"), "2": ("2", "5V")}, {"description": "Inductor"}),
             cap("C1", "5V"), res("R1", "5V", "FB1", "160k"), res("R2", "FB1", "GND", "191k"),
             res("R3", "FB1", "SEL2", "137k"), res("R4", "FB1", "SEL4", "240k"),
             ("J5", {"1": ("1", "GND"), "2": ("2", "SEL2"), "3": ("3", "GND"),
                     "4": ("4", "SEL4")}, {"description": "Header 2x2"})]
    row = _divider(_run(board), "FB1")
    assert [feed["use"] for feed in row["feeds"]] == ["fb"]  # no connector feeds
    legs = {tuple(leg["parts"]): leg for leg in row["selectable_legs"]}
    alone = 191e3 * 137e3 / (191e3 + 137e3)
    assert legs[("R3",)]["ratio"] == pytest.approx(alone / (160e3 + alone), abs=1e-5)
    assert legs[("R3",)]["vout_over_vref"] == pytest.approx(1 + 160e3 / alone, abs=1e-3)
    assert legs[("R3",)]["through"].startswith("J5.2") and legs[("R3",)]["to"] == "GND"
    assert "also_on_midpoint" not in row
    # A connector that also carries other signals is a consumer, not a selector.
    board[-1] = ("J5", {"1": ("1", "GND"), "2": ("2", "SEL2"), "3": ("3", "SIG"),
                        "4": ("4", "SEL4")}, {"description": "Header 2x2"})
    row = _divider(_run(board), "FB1")
    assert "selectable_legs" not in row
    assert any(feed["use"] == "connector" for feed in row["feeds"])


def test_series_resistor_into_a_pull_down_is_not_a_divider():
    codec = ("U4", {"1": ("VDD", "3V3", "Power"), "2": GND, "3": ("CLKIN", "CLK", "Input")},
             {"description": "Audio codec"})
    board = [mcu(PA8=("CLK_RAW", "Output")), res("R1", "CLK_RAW", "CLK", "33"),
             res("R2", "CLK", "GND", "10k"), codec]
    result = _run(board)
    assert not _rows(result, "divider")
    assert result["summary"]["dividers"]["series_pull_down_skipped"] == 1
    gate = [mcu(PA8="GDRV"), res("R1", "GDRV", "QG", "100"), res("R2", "QG", "GND", "100k"),
            ("Q1", {"1": ("G", "QG"), "2": ("S", "GND"), "3": ("D", "QD")},
             {"description": "N-channel MOSFET"})]
    row = _divider(_run(gate), "QG")
    assert row["pattern"].startswith("a series resistor and a pull-down")


def test_divider_below_ground_on_an_adc_pin():
    board = [mcu(**{"PA0/ADC1_IN0": "LVL"}), res("R1", "VNEG", "LVL", "10k"),
             res("R2", "LVL", "3V3", "10k")]
    result = _run(board, rails={"VNEG": -12.0})
    assert _divider(result, "LVL")["result"]["v_mid"] == pytest.approx(-4.35, abs=1e-3)
    found = _findings(result, "divider_output_below_ground", "warning")
    assert len(found) == 1 and found[0]["limit_v"] == -0.3
    assert not _findings(result, "divider_output_above_input_rating")


def test_adc_without_a_filter_names_the_divider():
    found = _findings(_run(_adc_board()), "adc_no_filter")
    assert "only the divider R1/R2, source impedance 8.4k" in found[0]["issue"]


# ---------- LEDs and optocouplers: more ----------

def test_resistor_across_an_led_takes_its_share():
    board = [led("LED1", "LA", "GND", "LED red"), res("R1", "5V", "LA", "1k"),
             res("R2", "LA", "GND", "1k")]
    row = _rows(_run(board), "led")[0]
    assert row["formula"] == "I = (V_supply - V_sink - Vf) / R - Vf / R_across"
    assert row["inputs"]["R_across"]["parts"] == ["R2"]
    assert row["result"]["current_ma"] == pytest.approx(1.0, abs=1e-6)  # 3 mA - 2 mA
    assert row["result"]["series_current_ma"] == 3.0
    assert row["result"]["resistor_power_mw"] == {"R1": 9.0, "R2": 4.0}


def test_resistor_across_an_opto_input_can_hold_it_off():
    opto = ("U5", {"1": ("A", "OA"), "2": ("K", "GND"), "3": ("E", "GND"), "4": ("C", "OUT")},
            {"description": "Phototransistor optocoupler"})
    board = [opto, res("R1", "5V", "OA", "4.7k"), res("R2", "OA", "GND", "1k"),
             res("R3", "3V3", "OUT", "10k")]
    result = _run(board)
    row = _rows(result, "opto")[0]
    assert row["result"]["if_ma"] == 0.0 and "ctr_needed_percent" not in row["result"]
    assert row["result"]["v_across_diode"] == pytest.approx(5 / 5.7, abs=1e-3)
    found = _findings(result, "diode_shunted_off", "warning")
    assert len(found) == 1 and set(found[0]["designators"]) == {"U5", "R1", "R2"}
    assert not _findings(_run(board, kinds=["leds"]), "diode_shunted_off")


def test_opto_ctr_from_unrounded_currents():
    opto = ("U5", {"1": ("A", "OA"), "2": ("K", "GND"), "3": ("E", "GND"), "4": ("C", "OUT")},
            {"description": "PC817"})
    row = _rows(_run([opto, res("R1", "24V", "OA", "2.2k"), res("R3", "3V3", "OUT", "10k")]),
                "opto")[0]
    exact = (3.3 / 10e3) / (22.8 / 2.2e3) * 100
    assert row["result"]["ctr_needed_percent"] == pytest.approx(exact, abs=0.005)


def test_parallel_resistors_in_an_led_path_are_combined():
    board = [led("LED1", "LA", "GND", "LED red"), res("R1", "5V", "LA", "220"),
             res("R2", "5V", "LA", "220")]
    row = _rows(_run(board), "led")[0]
    assert row["inputs"]["R"]["value"] == "110"
    assert row["inputs"]["R"]["arrangement"] == "parallel resistors"
    assert row["result"]["current_ma"] == pytest.approx(3 / 110 * 1000, rel=1e-3)
    each = (3 / 110) ** 2 * 110 / 2 * 1000  # half the current in each
    assert row["result"]["resistor_power_mw"]["R1"] == pytest.approx(each, rel=1e-3)


def test_led_side_not_followed_says_why():
    board = [led("LED1", "LA", "MID", "LED red"), led("LED2", "MID", "GND", "LED red"),
             res("R1", "12V", "LA", "1k")]
    second = next(row for row in _rows(_run(board), "led") if row["led"] == "LED2")
    assert "another diode (LED1)" in second["notes"][0]
    assert "no series resistor" not in " ".join(second["notes"])
    assert "value" not in second["inputs"]["R"]


def test_resistor_overpower_with_an_unknown_colour():
    board = [led("LED1", "LA", "GND", "Chip LED"),
             res("R1", "12V", "LA", "220", description="RES 220R 0603")]
    found = _findings(_run(board), "resistor_overpower")
    assert [row["severity"] for row in found] == ["warning"]
    assert "even at 3 V" in found[0]["issue"]
    marginal = [led("LED1", "LA", "GND", "Chip LED"),
                res("R1", "5V", "LA", "68", description="RES 68R 0603")]
    found = _findings(_run(marginal), "resistor_overpower")  # 132 mW at 2 V, 59 mW at 3 V
    assert [row["severity"] for row in found] == ["info"]


def test_kinds_keep_each_finding_with_its_row_kind():
    board = [led("LED1", "LA", "GND", "LED red"),
             res("R1", "5V", "LA", "68", description="RES 68R 0402")]
    assert _findings(_run(board, kinds=["leds"]), "resistor_overpower")
    assert not _run(board, kinds=["optos"])["findings"]


def test_led_pins_on_another_part_such_as_a_backlight():
    board = [("LCD1", {"1": ("GND", "GND", "Power"), "2": ("VDD", "5V", "Power"),
                       "3": ("LED+", "5V"), "4": ("LED-", "BL_K")},
              {"description": "Character LCD display with backlight"}),
             res("R1", "BL_K", "GND", "68"), res("R2", "BL_K", "GND", "68")]
    result = _run(board, vf={"LCD1": 3.2})
    row = _rows(result, "led")[0]
    assert row["led"] == "LCD1" and row["channel"] == "LED+/LED-"
    assert row["result"]["current_ma"] == pytest.approx((5 - 3.2) / 34 * 1000, rel=1e-3)
    assert "rating_ma" not in row["result"] and not _findings(result, "led_overcurrent")
    assert result["summary"]["leds"]["in_other_parts"] == 1


def test_same_part_number_shares_a_colour_and_vf_beside_a_current():
    text = "SMD chip LED, waterclear, 20 mA, 2.1 V"
    board = [led("LED1", "A1", "GND", text, mpn="LEDPN-0001", params={"color": "Red"}),
             led("LED2", "A2", "GND", text, mpn="LEDPN-0001"),
             res("R1", "5V", "A1", "330"), res("R2", "5V", "A2", "330")]
    rows = {row["led"]: row for row in _rows(_run(board), "led")}
    assert rows["LED2"]["colour"] == "red"
    assert rows["LED2"]["colour_source"].startswith("same part number as LED1")
    assert rows["LED2"]["inputs"]["Vf"]["volts"] == 2.1
    assert "beside a current" in rows["LED2"]["inputs"]["Vf"]["source"]
    assert rows["LED2"]["confidence"] == "medium"


def test_a_header_across_a_feedback_chain_leaves_one_row():
    # 5V -> R0 -> VCC_X (a rail by its name) -> R1 -> N1 -> R2 -> N2 -> R4 -> FB1 -> R3 -> GND,
    # a jumper header on VCC_X, N1 and N2: the FB row covers the chain, and neither the
    # header's nodes nor the chain's end get a row of their own.
    board = [buck(), ("L1", {"1": ("1", "SW1"), "2": ("2", "5V")}, {"description": "Inductor"}),
             cap("C1", "5V"), res("R0", "5V", "VCC_X", "1k"),
             ("JP9", {"1": ("1", "VCC_X"), "2": ("2", "N1"), "3": ("3", "N2")},
              {"description": "Jumper header"}),
             res("R1", "VCC_X", "N1", "20k"), res("R2", "N1", "N2", "20k"),
             res("R4", "N2", "FB1", "20k"), res("R3", "FB1", "GND", "10k")]
    result = _run(board)
    assert [r["midpoint"] for r in _rows(result, "divider")] == ["FB1"]
    row = _divider(result, "FB1")
    assert row["inputs"]["R_top"]["parts"] == ["R4", "R2", "R1"]
    assert [tap["net"] for tap in row["taps"]] == ["N2", "N1"]


def test_led_from_a_pin_whose_rail_is_not_read_gives_the_formula_with_voh():
    board = [("U3", {"1": ("OUT", "DRV", "Output"), "2": GND}, {"description": "Logic buffer"}),
             res("R1", "DRV", "LA", "1k"), led("LED1", "LA", "GND", "LED red")]
    row = _rows(_run(board), "led")[0]
    assert row["result"]["current_formula"] == "I = (VOH - 2 V) / 1k ohm"
    assert "I/O rail was not read" in row["notes"][0]
