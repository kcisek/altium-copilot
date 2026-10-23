"""Component derating check (services/derating, check_derating). Synthetic boards with
generic names; part numbers come from the manufacturers' published part-numbering guides
and catalogues."""

import asyncio
import json
from pathlib import Path

import pytest

import server.main as main_mod
import server.services.derating as dr
from server.parsers.prj_pcb import VariantDefinition, VariantState
from server.services.bom import VariantPlan
from tests.test_pull_resistors import _netlist

GND = ("2", "GND")


def _info(info: dict, mpn: str | None, params: dict | None) -> dict:
    params = dict(params or {})
    if mpn:
        params["manufacturer part number"] = mpn  # where the BOM reads part numbers
    return {**info, "params": params} if params else info


def cap(ref, net, other="GND", mpn=None, params=None, **info):
    return (ref, {"1": ("1", net), "2": ("2", other)}, _info(info, mpn, params))


def res(ref, net_a, net_b, mpn=None, params=None, **info):
    return (ref, {"1": ("1", net_a), "2": ("2", net_b)}, _info(info, mpn, params))


# A 12 V input (its voltage from rails=) into a buck whose unnamed output a 90k/10k divider
# sets, an LDO to 3V3 and a load.
BOARD = [
    ("J1", {"1": ("1", "VIN"), "2": GND}, {"description": "DC power jack"}),
    ("U1", {"1": ("VIN", "VIN", "Power"), "2": ("SW", "SW1"), "3": ("BST", "BST1"),
            "4": ("FB", "FB1", "Input"), "5": ("GND", "GND", "Power")},
     {"description": "Synchronous step-down converter"}),
    ("L1", {"1": ("1", "SW1"), "2": ("2", "BUCK_OUT")}, {"description": "Inductor 4.7uH"}),
    res("R1", "BUCK_OUT", "FB1", value="90k"),
    res("R2", "FB1", "GND", value="10k"),
    cap("C1", "VIN", value="10uF", description="CAP 10uF 25V X7R 1206"),
    cap("C2", "BUCK_OUT", value="22uF", description="CAP 22uF 16V X5R 0805"),
    cap("C3", "BST1", "SW1", value="100nF", description="CAP 100nF 16V X7R 0402"),
    ("U2", {"1": ("IN", "BUCK_OUT", "Power"), "2": ("OUT", "3V3", "Power"), "3": ("GND", "GND")},
     {"description": "LDO regulator"}),
    cap("C4", "3V3", value="100nF", description="CAP 100nF 16V X7R 0402"),
    ("U3", {"1": ("VDD", "3V3", "Power"), "2": ("GND", "GND", "Power")},
     {"description": "Microcontroller"}),
]


def _with(*extra, drop=(), board=BOARD):
    return [part for part in board if part[0] not in drop] + list(extra)


def _run(board=BOARD, plan=None, **kwargs):
    kwargs.setdefault("limit", 1000)
    return dr.run_derating_check(_netlist(board), plan, **kwargs)


def _findings(result, check=None, part=None):
    return [f for f in result["findings"] if (check is None or f["check"] == check)
            and (part is None or part in f["designators"])]


def _part(ref, *extra_parts, board=BOARD, **kwargs):
    """The parsed row of one part (part= lists it)."""
    result = _run(_with(*extra_parts, board=board), parts=[ref], **kwargs)
    return next(row for row in result["parts"] if row["designator"] == ref)


# ---------- text parsers ----------

@pytest.mark.parametrize("text, expected", [
    ("10uF 25V X5R 0603", (25.0, False)), ("100n/16V", (16.0, False)),
    ("100nF_50V", (50.0, False)), ("4u7 6V3", (6.3, False)), ("CAP 6.3 V", (6.3, False)),
    ("50VDC", (50.0, False)), ("1kV C0G", (1000.0, False)), ("X2 275VAC", (275.0, True)),
    ("100 Volts", (100.0, False)), ("Decoupling for the 3V3 rail", (None, False)),
    ("1.8V", (None, False)), ("+/-10%, 85 degC", (None, False)), ("X7R-16V", (16.0, False)),
    ("100nF 50V AC/DC", (50.0, False)),
])
def test_volts_in_text(text, expected):
    assert dr.volts_in_text(text) == expected


def test_parameter_voltages_take_any_number():
    assert dr._param_volts("6.3 V") == (6.3, False)
    assert dr._param_volts("25") == (25.0, False)
    assert dr._param_volts("3.3V") == (3.3, False)  # a parameter is a rating, any value
    assert dr._param_volts("n/a") == (None, False)


@pytest.mark.parametrize("text, bare, expected", [
    ("100 mW", False, 0.1), ("0.05W", False, 0.05), ("1/16W", False, 0.0625),
    ("Chip Resistor, 10 kOhm, 0.25 W, 1206", False, 0.25), ("0402WGF1000TCE", False, None),
    ("0.1", True, 0.1), ("100", True, None), ("0.1", False, None),
])
def test_watts_in_text(text, bare, expected):
    assert dr.watts_in_text(text, bare=bare) == expected


@pytest.mark.parametrize("text, expected", [
    ("X7R", "X7R"), ("x5r", "X5R"), ("COG", "C0G"), ("NP0 dielectric", "C0G"),
    ("NPO", "C0G"), ("Y5V", "Y5V"), ("C1608X7R1H104K", None), ("ceramic", None),
])
def test_dielectric_in_text(text, expected):
    assert dr.dielectric_in_text(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("Tantalum Capacitor", "tantalum"), ("Polymer tantalum capacitor", "tantalum_polymer"),
    ("Tantalum polymer, low ESR", "tantalum_polymer"), ("POSCAP", "tantalum_polymer"),
    ("Aluminum Electrolytic Capacitor", "aluminium_electrolytic"),
    ("Conductive polymer aluminium", "aluminium_polymer"), ("Film capacitor", "film"),
    ("Polypropylene", "film"), ("MLCC", "ceramic"), ("Ceramic capacitor", "ceramic"),
    ("Chip Capacitor, 100 nF", "ceramic"), ("Polar Capacitor", "polarized"),
    ("Non-polar electrolytic", "aluminium_electrolytic"), ("Non-polar capacitor", None),
    ("Supercapacitor 5.5V", "supercapacitor"), ("Capacitor", None),
])
def test_technology_in_text(text, expected):
    assert dr.technology_in_text(text) == expected


@pytest.mark.parametrize("text, metric, expected", [
    ("0402 (1005 Metric)", False, "0402"), ("CAP 10uF 0603 (1608 Metric)", False, "0603"),
    ("CAPC1608X90X35", False, "0603"), ("CAPC0603X33X15", False, "0201"),
    ("RESC1005X40X25", False, "0402"), ("RESC2013X70X40", False, "0805"),
    ("(1005 Metric)", False, "0402"), ("C_0805_2012Metric", False, "0805"),
    ("1608M", False, "0603"), ("C1608", False, "0603"), ("R0402", False, "0402"),
    ("0603", True, "0201"), ("1005", True, "0402"), ("EIA 3528", False, "1411"),
    ("2-Pin Surface Mount Device, Body 1 x 0.5 mm", False, "0402"),
    ("Body 3.2 x 1.6 mm", False, "1206"), ("1500pF", False, None), ("SOT-223", False, None),
    ("GRM188R71H104KA93D", False, None), (None, False, None),
])
def test_case_in_text(text, metric, expected):
    assert dr.case_in_text(text, metric=metric) == expected


# ---------- part numbers (manufacturers' published examples) ----------

@pytest.mark.parametrize("mpn, expected", [
    # Murata GRM / GCM: size, thickness, temperature characteristic, voltage, capacitance.
    ("GRM188R71H104KA93D", {"case": "0603", "dielectric": "X7R", "rated_v": 50.0,
                            "farads": 1e-7}),
    ("GRM155R61A105KE15D", {"case": "0402", "dielectric": "X5R", "rated_v": 10.0,
                            "farads": 1e-6}),
    ("GRM1555C1H101JA01D", {"case": "0402", "dielectric": "C0G", "rated_v": 50.0,
                            "farads": 1e-10}),
    ("GCM188R71H104KA57D", {"case": "0603", "dielectric": "X7R", "rated_v": 50.0}),
    ("GRM31CR60J107ME39L", {"case": "1206", "dielectric": "X5R", "rated_v": 6.3,
                            "farads": 1e-4}),
    # TDK C series (metric size) and CGA automotive (size digit).
    ("C1608X7R1H104K080AA", {"case": "0603", "dielectric": "X7R", "rated_v": 50.0,
                             "farads": 1e-7}),
    ("C2012X5R1A106K125AB", {"case": "0805", "dielectric": "X5R", "rated_v": 10.0}),
    ("CGA3E2X7R1H104K080AA", {"case": "0603", "dielectric": "X7R", "rated_v": 50.0}),
    ("CGA4J2X7R1E105K125AA", {"case": "0805", "dielectric": "X7R", "rated_v": 25.0,
                              "farads": 1e-6}),
    # Samsung CL: size, characteristic, capacitance, tolerance, voltage letter.
    ("CL10A106KP8NNNC", {"case": "0603", "dielectric": "X5R", "rated_v": 10.0,
                         "farads": 1e-5}),
    ("CL05B104KO5NNNC", {"case": "0402", "dielectric": "X7R", "rated_v": 16.0}),
    ("CL21B225KAFNNNE", {"case": "0805", "dielectric": "X7R", "rated_v": 25.0}),
    # KEMET C...C: size, capacitance, tolerance, voltage digit, dielectric.
    ("C0603C104K5RACTU", {"case": "0603", "dielectric": "X7R", "rated_v": 50.0,
                          "farads": 1e-7}),
    ("C0805C106K8PACTU", {"case": "0805", "dielectric": "X5R", "rated_v": 10.0}),
    # Yageo CC: size, tolerance, packing, dielectric, voltage digit, capacitance last.
    ("CC0603KRX7R9BB104", {"case": "0603", "dielectric": "X7R", "rated_v": 50.0,
                           "farads": 1e-7}),
    ("CC0402KRX5R6BB105", {"case": "0402", "dielectric": "X5R", "rated_v": 10.0}),
    # AVX/Kyocera: size, voltage, dielectric, capacitance.
    ("08055C103KAT2A", {"case": "0805", "dielectric": "X7R", "rated_v": 50.0,
                        "farads": 1e-8}),
    ("0402ZD105KAT2A", {"case": "0402", "dielectric": "X5R", "rated_v": 10.0}),
    # Taiyo Yuden: voltage letter, MK, size, characteristic, capacitance.
    ("TMK107B7224KA-T", {"case": "0603", "dielectric": "X7R", "rated_v": 25.0,
                         "farads": 2.2e-7}),
    ("JMK107BJ106MA-T", {"case": "0603", "dielectric": "X5R", "rated_v": 6.3}),
    # Walsin: size, dielectric, capacitance, tolerance, voltage (two digits and zeros).
    ("0402B104K160CT", {"case": "0402", "dielectric": "X7R", "rated_v": 16.0}),
    ("0603N101J500CT", {"case": "0603", "dielectric": "C0G", "rated_v": 50.0,
                        "farads": 1e-10}),
    # TDK's size digit 1 is an 0201 (0603 metric).
    ("CGA1A2X7R1E102K030BA", {"case": "0201", "dielectric": "X7R", "rated_v": 25.0,
                              "farads": 1e-9}),
    # Samsung's catalogue: X6S, X7S, X7T and X8G characteristics.
    ("CL10X226MQ91IN", {"case": "0603", "dielectric": "X6S", "rated_v": 6.3}),
    ("CL05Y105KO66PN", {"case": "0402", "dielectric": "X7S", "rated_v": 16.0}),
    ("CL10Z106MP96PN", {"case": "0603", "dielectric": "X7T", "rated_v": 10.0}),
    ("CL05G102JC51PN", {"case": "0402", "dielectric": "X8G", "rated_v": 100.0}),
])
def test_ceramic_part_numbers(mpn, expected):
    decoded = dr.decode_capacitor_mpn(mpn)
    assert decoded["technology"] == "ceramic"
    for key, value in expected.items():
        assert decoded[key] == pytest.approx(value) if key == "farads" else \
            decoded[key] == value, (mpn, key)


@pytest.mark.parametrize("letter, volts", [("T", 75.0), ("X", 1250.0), ("V", 1500.0),
                                           ("K", 3000.0)])
def test_samsung_voltage_letters(letter, volts):
    # The rated voltage code table of Samsung's MLCC catalogue (the letter after the
    # tolerance).
    assert dr.decode_capacitor_mpn(f"CL31B103K{letter}HNNNE")["rated_v"] == volts


@pytest.mark.parametrize("mpn, technology, volts, farads", [
    ("T491D107M010AT", "tantalum", 10.0, 1e-4),
    ("T491A106K016AT", "tantalum", 16.0, 1e-5),
    ("T520B107M006ATE040", "tantalum_polymer", 6.3, 1e-4),
    ("TAJA106K016RNJ", "tantalum", 16.0, 1e-5),
    ("TPSD107K010R0100", "tantalum", 10.0, 1e-4),
    ("TCJB107M006R0070", "tantalum_polymer", 6.3, 1e-4),
    ("6TPF330M9L", "tantalum_polymer", 6.3, 3.3e-4),
    ("2R5TPE330M9", "tantalum_polymer", 2.5, 3.3e-4),
    ("16TQC22MYF", "tantalum_polymer", 16.0, 2.2e-5),
])
def test_tantalum_part_numbers(mpn, technology, volts, farads):
    decoded = dr.decode_capacitor_mpn(mpn)
    assert decoded["technology"] == technology
    assert decoded["rated_v"] == volts
    assert decoded["farads"] == pytest.approx(farads)


@pytest.mark.parametrize("mpn", ["STM32F103C8T6", "LM317", "0402WGF1000TCE", "", None,
                                 "1N4148W", "GRM", "RC0402FR-0710KL"])
def test_unknown_capacitor_part_numbers(mpn):
    assert dr.decode_capacitor_mpn(mpn) is None


@pytest.mark.parametrize("mpn, case, high_power", [
    ("RC0402FR-0710KL", "0402", False), ("RC0603JR-0710KL", "0603", False),
    ("CRCW080549K9FKEA", "0805", False), ("CRCW12061K00FKEA", "1206", False),
    ("ERJ-3EKF1002V", "0603", False), ("ERJ-2RKF1002X", "0402", False),
    ("ERJ-6ENF1001V", "0805", False), ("ERJ-1GEJ150C", "0201", False),
    ("ERJ-12ZYJ103U", "2010", False), ("ERJ-P06J103V", "0805", True),
    ("RMCF0603FT10K0", "0603", False), ("RMCP2010FT10R0", "2010", True),
    ("CRCW1206100RFKEAHP", "1206", True),
])
def test_resistor_part_numbers(mpn, case, high_power):
    decoded = dr.decode_resistor_mpn(mpn)
    assert decoded["case"] == case
    assert bool(decoded.get("high_power")) == high_power


def test_unknown_resistor_part_numbers():
    assert dr.decode_resistor_mpn("LM317") is None
    assert dr.decode_resistor_mpn("10k") is None


# ---------- reading a part's fields ----------

def test_parameters_come_first_then_text_then_part_number_then_footprint():
    row = _part("C9", cap("C9", "3V3", value="1uF", footprint="CAPC1608X90",
                          description="Capacitor", params={"voltage": "16 V"},
                          mpn="GRM188R61C105KA93D"))
    assert "conflicts" not in row
    assert row["rated_v"] == 16.0 and row["rated_v_source"] == "parameter (voltage)"
    assert row["dielectric"] == "X5R" and row["dielectric_source"] == "mpn (Murata GRM)"
    assert row["case"] == "0603" and row["case_source"] == "mpn (Murata GRM)"
    assert row["value"] == "1uF" and row["value_source"] == "text (value)"
    row = _part("C9", cap("C9", "3V3", value="1uF", footprint="C_0603_1608Metric"))
    assert row["case"] == "0603" and row["case_source"] == "footprint"
    assert row["technology"] == "ceramic" and row["technology_source"].startswith("inferred")
    assert "rated_v" not in row


def test_common_parameter_names_are_read():
    params = {"rated voltage": "25V", "temperature coefficient": "X7S",
              "case/package": "0402", "capacitance": "100nF"}
    row = _part("C9", cap("C9", "3V3", params=params))
    assert (row["rated_v"], row["dielectric"], row["case"], row["value"]) == (
        25.0, "X7S", "0402", "100nF")
    assert row["case_source"] == "parameter (case/package)"
    row = _part("C9", cap("C9", "3V3", params={"case code (metric)": "1005"}))
    assert row["case"] == "0402"
    row = _part("R9", res("R9", "3V3", "SIG", value="10k",
                          params={"power": "0.25W", "size": "1206"}))
    assert row["power_w"] == 0.25 and row["case"] == "1206"


def test_a_value_field_holding_the_whole_spec_is_read():
    row = _part("C9", cap("C9", "3V3", value="10uF 25V X5R 0603"))
    assert (row["rated_v"], row["dielectric"], row["case"], row["technology"]) == (
        25.0, "X5R", "0603", "ceramic")


def test_disagreeing_sources_are_kept_and_the_majority_wins():
    # One parameter says 3 V; another parameter, the text and the part number say 6.3 V.
    params = {"voltage rating": "3 V", "rated voltage": "6.3 V"}
    row = _part("C9", cap("C9", "3V3", params=params, description="CAP 100nF 6.3V X5R",
                          mpn="GRM033R60J104KE19D"))
    assert row["rated_v"] == 6.3
    assert set(row["conflicts"]["rated_v"].values()) == {3.0, 6.3}
    # A tie: a parameter before a decoded part number, a part number before free text.
    row = _part("C9", cap("C9", "3V3", params={"voltage": "16V"},
                          mpn="GRM155R61A105KE15D"))
    assert row["rated_v"] == 16.0 and row["conflicts"]["rated_v"]["mpn (Murata GRM)"] == 10.0
    row = _part("C9", cap("C9", "3V3", value="1uF", description="CAP 1uF 50V X5R 0402",
                          mpn="GRM155R61A105KE15D"))
    assert row["rated_v"] == 10.0 and row["rated_v_source"] == "mpn (Murata GRM)"
    assert row["conflicts"]["rated_v"]["text (description)"] == 50.0


def test_several_ratings_in_one_text():
    # The rating beside the capacitance and dielectric, not a rail named before it.
    assert dr.text_ratings("Decoupling for 5.5V supply, CAP 100nF 50V X7R 0603") == (
        [50.0], None)
    assert dr.text_ratings("X2 275VAC 630VDC") == ([630.0], 275.0)
    # Two ratings beside the same specification: both kept, a conflict.
    assert dr.text_ratings("CAP 100nF 50V X7R 0603 16V") == ([50.0, 16.0], None)
    row = _part("C9", cap("C9", "24V", value="100nF",
                          description="Decoupling for 5.5V supply, CAP 100nF 50V X7R 0603"))
    assert row["rated_v"] == 50.0 and "conflicts" not in row


def test_a_part_number_narrows_a_generic_technology():
    row = _part("C9", cap("C9", "3V3", description="Tantalum Capacitor 100uF 6.3V",
                          mpn="T520B107M006ATE040"))
    assert row["technology"] == "tantalum_polymer" and "conflicts" not in row
    row = _part("C9", cap("C9", "3V3", description="Polar Capacitor 10uF 16V",
                          footprint="CAPMP3528X210"))
    assert row["technology"] == "tantalum"
    row = _part("C9", cap("C9", "3V3", params={"type": "Tantalum"},
                          description="Polar Capacitor 10uF 16V"))
    assert row["technology"] == "tantalum" and "conflicts" not in row


def test_ac_ratings_are_not_dc_ratings():
    board = _with(cap("C9", "3V3", value="100nF", description="X2 class capacitor 275VAC"))
    result = _run(board, parts=["C9"])
    row = result["parts"][0]
    assert row["rated_ac_v"] == 275.0 and "rated_v" not in row
    assert _findings(result, "unknown_rating", "C9")


# ---------- net voltages ----------

@pytest.mark.parametrize("net, volts", [
    ("3V3", 3.3), ("+5V", 5.0), ("VDD_1V8", 1.8), ("P3V3", 3.3), ("12V_IN", 12.0),
    ("VCC_3.3V", 3.3), ("VDD33", 3.3), ("3P3", 3.3), ("CORE_0V95OUT", 0.95),
    ("-12V", -12.0), ("N5V", -5.0), ("-VEE_5V", -5.0),
    ("32.768K_IN", None), ("A10VCCPLL", None), ("VBAT", None), ("ADC12", None),
    # The P form with a trailing V: volts and fraction, not the digits before the V.
    ("P1P8V", 1.8), ("1P8V", 1.8), ("VDD_1P35V", 1.35), ("0P9V", 0.9), ("VCC_3P3V", 3.3),
    # VDD/VCC and two digits: only the usual rails read as volts and tenths.
    ("VDD18", 1.8), ("VCC36", None), ("VCC48", None),
    # VDD12 and VCC15 may be 12 V or 15 V as well as 1.2 V or 1.5 V: asked, not guessed.
    ("VDD12", None), ("VCC15", None),
    # A negative rail named by a word, above 60 V, AC nets and control nets.
    ("VNEG_5V", -5.0), ("VEE_12V", -12.0), ("5V_NEG", -5.0), ("72V", 72.0),
    ("HV_400V", 400.0), ("VBUS_100V", 100.0), ("24VAC", None), ("230V_AC", None),
    ("BST_5V", None), ("SS_3V3", None), ("COMP_1V8", None), ("GATE_12V", None),
    ("VFB_1V8", None), ("12V_DIV", None),
])
def test_signed_rail_voltage(net, volts):
    assert dr.signed_rail_voltage(net) == volts


def test_name_voltage_by_convention_is_medium_confidence():
    assert dr.name_voltage("VDD33") == (3.3, True)
    assert dr.name_voltage("3V3") == (3.3, False)
    board = _with(cap("C9", "VDD33", value="1uF", description="CAP 1uF 6.3V X5R 0603"),
                  ("U9", {"1": ("VDD", "VDD33", "Power"), "2": ("GND", "GND", "Power")}, {}))
    volts = _run(board, nets=["VDD33", "3V3"])["net_voltages"]
    assert volts["VDD33"]["confidence"] == "medium" and "convention" in volts["VDD33"]["basis"]
    assert volts["3V3"]["confidence"] == "high"


@pytest.mark.parametrize("text, expected", [
    ("VIN=24,VBUS=5", {"VIN": 24.0, "VBUS": 5.0}),
    ("VIN,VIN_F=24", {"VIN": 24.0, "VIN_F": 24.0}),
    ("VNEG = -5V; VBAT:3.7", {"VNEG": -5.0, "VBAT": 3.7}),
    (None, None), ("", None),
])
def test_rail_map(text, expected):
    assert dr.parse_rail_map(text) == (expected, None)


@pytest.mark.parametrize("text", ["VIN", "VIN=abc", "VIN=5000", "VIN=5,VBUS", "=5"])
def test_rail_map_errors(text):
    assert dr.parse_rail_map(text)[1]["error"] == "invalid_rails"


def test_voltage_sources_and_precedence():
    result = _run(rails={"VIN": 12}, vref=0.6, parts=["C1", "C2", "C4"],
                  nets=["VIN", "BUCK_OUT", "3V3", "GND", "BST1"])
    volts = result["net_voltages"]
    assert volts["VIN"] == {"volts": 12.0, "source": "user", "confidence": "high",
                            "basis": "given in rails", "ripple": "input of U1 (buck)"}
    assert volts["BUCK_OUT"]["source"] == "power_tree" and volts["BUCK_OUT"]["volts"] == 6.0
    assert "U1's feedback divider with Vref 0.6 V" in volts["BUCK_OUT"]["basis"]
    assert volts["3V3"]["source"] == "name" and volts["GND"]["source"] == "ground"
    assert volts["BST1"] is None
    # FB1 is the midpoint of the 90k/10k divider from BUCK_OUT, at the input of U1's FB pin.
    assert result["summary"]["net_voltages"]["known_by_source"] == {
        "user": 1, "name": 1, "power_tree": 2, "divider": 1, "ground": 1}
    # The user's value wins over the name and the power tree.
    result = _run(rails={"3V3": 3.0, "BUCK_OUT": 5.0}, vref=0.6, nets=["3V3", "BUCK_OUT"])
    assert result["net_voltages"]["3V3"]["volts"] == 3.0
    assert result["net_voltages"]["BUCK_OUT"]["source"] == "user"
    # Without rails or vref the input and the buck's output are not known.
    result = _run()
    assert [row["net"] for row in result["rails_without_voltage"]] == ["BUCK_OUT", "VIN"]
    unknown = _run(rails={"NOPE": 5})
    assert any("NOPE" in note for note in unknown["notes"])


def test_voltages_cross_same_voltage_links_and_skip_status_nets():
    board = _with(
        ("FB1", {"1": ("1", "3V3"), "2": ("2", "3V3_FILT")}, {"description": "Ferrite bead"}),
        cap("C9", "3V3_FILT", value="1uF", description="CAP 1uF 2.5V X5R 0603"),
        res("R9", "3V3", "LINKED", value="0R"),
        ("U9", {"1": ("VDD", "LINKED", "Power"), "2": ("GND", "GND", "Power")}, {}),
        cap("C10", "LINKED", value="1uF", description="CAP 1uF 2.5V X5R 0603"),
        cap("C11", "EN_5V", value="1uF", description="CAP 1uF 2.5V X5R 0603"),
    )
    result = _run(board, nets=["3V3_FILT", "LINKED", "EN_5V"])
    volts = result["net_voltages"]
    assert volts["3V3_FILT"]["volts"] == 3.3  # its own name
    assert volts["LINKED"]["source"] == "power_tree" and "R9" in volts["LINKED"]["basis"]
    assert volts["EN_5V"] is None  # an enable net's name is not its voltage
    assert {f["designators"][0] for f in _findings(result, "over_rated")} == {"C9", "C10"}


def test_negative_rails_and_caps_between_rails():
    board = _with(cap("C9", "-12V", value="1uF", description="CAP 1uF 16V X7R 0805"),
                  cap("C10", "+12V", "-12V", value="1uF", description="CAP 1uF 25V X7R 0805"))
    result = _run(board, parts=["C9", "C10"])
    rows = {row["designator"]: row for row in result["parts"]}
    assert rows["C9"]["across_v"] == 12.0 and rows["C10"]["across_v"] == 24.0
    assert _findings(result, "ceramic_voltage", "C10")[0]["ratio"] == 0.96


def test_ripple_nets_and_switch_nodes():
    board = _with(cap("C9", "SW1", value="1nF", description="CAP 1nF 16V X7R 0603"))
    result = _run(board, rails={"VIN": 12}, parts=["C3", "C9", "C4"],
                  nets=["VIN", "BUCK_OUT", "SW1", "3V3"])
    volts = result["net_voltages"]
    assert volts["SW1"]["confidence"] == "low" and volts["SW1"]["volts"] == 12.0
    assert volts["VIN"]["ripple"] == "input of U1 (buck)"
    assert volts["SW1"]["ripple"] == "switch node of U1"
    assert "ripple" not in volts["3V3"]  # an LDO's output
    rows = {row["designator"]: row for row in result["parts"]}
    assert rows["C3"]["across_v"] is None and "bootstrap" in rows["C3"]["voltage_basis"]
    assert rows["C9"]["across_v"] == 12.0
    # 12 V on a 16 V part is 75%: above the 50% a ripple net allows.
    found = _findings(result, "ceramic_voltage", "C9")[0]
    assert found["voltage_confidence"] == "low" and found["limit"] == 0.5


# A second buck fed from the first one's output: BUCK_OUT is U1's output and U5's input.
BUCK_ON_BUCK = _with(
    ("U5", {"1": ("VIN", "BUCK_OUT", "Power"), "2": ("SW", "SW5"), "3": ("BST", "CB_1V8"),
            "4": ("FB", "1V8", "Input"), "5": ("SS", "SS_1V8"), "6": ("GND", "GND", "Power")},
     {"description": "Synchronous step-down converter"}),
    ("L5", {"1": ("1", "SW5"), "2": ("2", "1V8")}, {"description": "Inductor 3.3uH"}),
    cap("C9", "CB_1V8", "SW5", value="100nF", description="CAP 100nF 16V X7R 0402"),
    cap("C10", "SS_1V8", value="10nF", description="CAP 10nF 6.3V X7R 0402"),
    cap("C11", "BUCK_OUT", value="10uF", description="CAP 10uF 10V X5R 0805"),
)


def test_ripple_lists_every_role_output_first():
    result = _run(BUCK_ON_BUCK, rails={"VIN": 12, "BUCK_OUT": 6}, nets=["BUCK_OUT"])
    assert result["net_voltages"]["BUCK_OUT"]["ripple"] == (
        "output of U1 (buck); input of U5 (buck)")
    found = _findings(result, "ceramic_voltage", "C11")[0]
    assert "(output of U1 (buck); input of U5 (buck))" in found["issue"]


def test_control_nets_named_after_a_rail_take_no_voltage_from_the_name():
    # CB_1V8 is on U5's BST pin (no word says so), SS_1V8 on its SS pin: neither is 1.8 V.
    result = _run(BUCK_ON_BUCK, rails={"VIN": 12, "BUCK_OUT": 6}, parts=["C9", "C10"],
                  nets=["CB_1V8", "SS_1V8", "1V8"])
    volts = result["net_voltages"]
    assert volts["CB_1V8"] is None and volts["SS_1V8"] is None
    assert volts["1V8"]["volts"] == 1.8  # the FB pin tied to the output keeps its name
    rows = {row["designator"]: row for row in result["parts"]}
    assert rows["C9"]["across_v"] is None and "bootstrap" in rows["C9"]["voltage_basis"]
    assert not _findings(result, part="C9") and not _findings(result, part="C10")
    # Nor are they rails to ask the user about.
    asked = {row["net"] for row in _run(BUCK_ON_BUCK)["rails_without_voltage"]}
    assert asked == {"VIN", "BUCK_OUT"}


# ---------- checks around their thresholds ----------

def _verdict(board, check, part, **kwargs):
    return [f["severity"] for f in _findings(_run(board, **kwargs), check, part)]


@pytest.mark.parametrize("volts, expected", [(7.5, []), (7.6, ["warning"])])
def test_ceramic_voltage_threshold(volts, expected):
    board = _with(cap("C9", "VX", value="1uF", description="CAP 1uF 10V X7R 0805"))
    assert _verdict(board, "ceramic_voltage", "C9", rails={"VX": volts}) == expected
    c0g = _with(cap("C9", "VX", value="1nF", description="CAP 1nF 10V C0G 0805"))
    assert _verdict(c0g, "ceramic_voltage", "C9", rails={"VX": volts}) == expected


@pytest.mark.parametrize("volts, expected", [(5.0, []), (5.1, ["warning"])])
def test_ceramic_voltage_on_a_ripple_net(volts, expected):
    board = _with(cap("C9", "BUCK_OUT", value="1uF", description="CAP 1uF 10V X7R 0805"))
    found = _findings(_run(board, rails={"BUCK_OUT": volts}), "ceramic_voltage", "C9")
    assert [f["severity"] for f in found] == expected
    if found:
        assert found[0]["limit"] == 0.5 and found[0]["ripple"] == "output of U1 (buck)"
        assert "a 16 V part would meet it" in found[0]["issue"]
    # The same voltage off a ripple net is fine.
    quiet = _with(cap("C9", "VX", value="1uF", description="CAP 1uF 10V X7R 0805"))
    assert _verdict(quiet, "ceramic_voltage", "C9", rails={"VX": volts}) == []


@pytest.mark.parametrize("volts, expected", [(8.0, []), (8.1, ["warning"])])
def test_tantalum_and_electrolytic_thresholds(volts, expected):
    for description, check in (("Tantalum capacitor 10uF 10V", "tantalum_voltage"),
                               ("Polymer tantalum 10uF 10V", "tantalum_voltage"),
                               ("Aluminum electrolytic 100uF 10V", "electrolytic_voltage"),
                               ("Polar capacitor 100uF 10V", "electrolytic_voltage")):
        board = _with(cap("C9", "VX", description=description))
        assert _verdict(board, check, "C9", rails={"VX": volts}) == expected, description


def test_ceramic_issue_text_follows_the_policy():
    board = _with(cap("C9", "BUCK_OUT", value="1uF", description="CAP 1uF 10V X7R 0805"))
    found = _findings(_run(board, rails={"BUCK_OUT": 5.5}, policy={
        "ceramic_ripple_max_ratio": 0.4}), "ceramic_voltage", "C9")[0]
    assert "above 40% (60% derating on a ripple net (output of U1 (buck)))" in found["issue"]
    # A ripple limit looser than the standard one has no effect, and a note says so.
    result = _run(board, rails={"BUCK_OUT": 7}, policy={"ceramic_max_ratio": 0.6,
                                                      "ceramic_ripple_max_ratio": 0.9})
    found = _findings(result, "ceramic_voltage", "C9")[0]
    assert found["limit"] == 0.6 and "above 60% (40% derating)" in found["issue"]
    assert any("looser than ceramic_max_ratio" in note for note in result["notes"])


def test_tantalum_mentions_mno2_practice_and_policy_overrides():
    board = _with(cap("C9", "VX", description="Tantalum capacitor 10uF 10V"))
    found = _findings(_run(board, rails={"VX": 9}), "tantalum_voltage", "C9")[0]
    assert "50% derating is common practice" in found["issue"] and found["limit"] == 0.8
    assert _verdict(board, "tantalum_voltage", "C9", rails={"VX": 6},
                    policy={"tantalum_max_ratio": 0.5}) == ["warning"]
    polymer = _with(cap("C9", "VX", description="Polymer tantalum 10uF 10V"))
    assert _verdict(polymer, "tantalum_voltage", "C9", rails={"VX": 8.5},
                    policy={"tantalum_polymer_max_ratio": 0.9}) == []


@pytest.mark.parametrize("volts, expected", [(16.0, []), (16.5, ["warning"])])
def test_over_rated_applies_to_any_technology(volts, expected):
    for description in ("CAP 16V X7R 1206", "Film capacitor 16V", "Capacitor 16V"):
        board = _with(cap("C9", "VX", value="1uF", description=description))
        found = _verdict(board, "over_rated", "C9", rails={"VX": volts})
        assert found == expected, description


@pytest.mark.parametrize("volts, expected", [(1.8, []), (1.9, ["info"]), (3.3, ["info"])])
def test_dc_bias_threshold(volts, expected):
    board = _with(cap("C9", "VX", value="1uF", description="CAP 1uF 6.3V X5R 0402"))
    found = _findings(_run(board, rails={"VX": volts}), "dc_bias", "C9")
    assert [f["severity"] for f in found] == expected
    if volts == 3.3:
        assert found[0]["loss_band"] == "about 40-70%"
        assert "not a number" in found[0]["issue"] and "datasheet" in found[0]["issue"]


def test_dc_bias_skips_large_cases_small_values_and_c0g():
    for description, value in (("CAP 6.3V X5R 0805", "10uF"), ("CAP 6.3V X5R 0402", "100nF"),
                               ("CAP 6.3V C0G 0402", "1uF")):
        board = _with(cap("C9", "VX", value=value, description=description))
        assert _verdict(board, "dc_bias", "C9", rails={"VX": 3.3}) == [], description
    # A ceramic of unknown dielectric that large for its size is taken for Class II.
    board = _with(cap("C9", "VX", value="10uF", description="Capacitor 10V 0603"))
    found = _findings(_run(board, rails={"VX": 5}), "dc_bias", "C9")[0]
    assert "taken for Class II" in found["issue"]
    # Y5V is Class III: a wider loss.
    board = _with(cap("C9", "VX", value="1uF", description="CAP 1uF 10V Y5V 0402"))
    assert _findings(_run(board, rails={"VX": 3.3}), "dc_bias", "C9")[0][
        "loss_band"] == "about 60-90%"


def test_dc_bias_policy_table():
    board = _with(cap("C9", "VX", value="1uF", description="CAP 1uF 6.3V X5R 0805"))
    policy = {"dc_bias_max_case": "0805", "dc_bias_min_uf": {"0805": 1.0}}
    assert _verdict(board, "dc_bias", "C9", rails={"VX": 3.3}, policy=policy) == ["info"]


@pytest.mark.parametrize("volts, expected", [(50.0, []), (51.0, ["warning"])])
def test_resistor_voltage_threshold(volts, expected):
    board = _with(res("R9", "VX", "GND", value="1M", footprint="R0402"))
    found = _findings(_run(board, rails={"VX": volts}), "resistor_voltage", "R9")
    assert [f["severity"] for f in found] == expected
    if found:
        assert "typical 50 V" in found[0]["issue"]
        assert found[0]["max_voltage_source"] == "package_typical (0402)"


def test_resistor_voltage_stated_and_one_end_known():
    board = _with(res("R9", "VX", "GND", value="1M", footprint="R0402",
                      params={"voltage rating": "75 V"}))
    assert _verdict(board, "resistor_voltage", "R9", rails={"VX": 60}) == []
    board = _with(res("R9", "VX", "MID", value="1M", footprint="R0201"))
    found = _findings(_run(board, rails={"VX": 30}), "resistor_voltage", "R9")
    assert found[0]["severity"] == "info" and found[0]["bound_v"] == 30.0


@pytest.mark.parametrize("volts, expected", [(5.5, []), (7.9, ["info"]), (8.0, ["warning"])])
def test_resistor_power_thresholds(volts, expected):
    # 1 kohm in 0402: typical 62.5 mW, derated 31.25 mW; 7.9 V gives 62.4 mW, 8 V 64 mW.
    board = _with(res("R9", "VX", "GND", value="1k", footprint="R0402"))
    found = _findings(_run(board, rails={"VX": volts}), "resistor_power", "R9")
    assert [f["severity"] for f in found] == expected
    if found:
        assert found[0]["power_rating_source"] == "package_typical (0402)"


def test_resistor_power_stated_rating_and_derating_policy():
    board = _with(res("R9", "VX", "GND", value="1k", footprint="R0402",
                      description="RES 1k 0.25W"))
    assert _verdict(board, "resistor_power", "R9", rails={"VX": 8}) == []
    plain = _with(res("R9", "VX", "GND", value="1k", footprint="R0402"))
    assert _verdict(plain, "resistor_power", "R9", rails={"VX": 5.5},
                    policy={"resistor_power_derating": 0.4}) == ["info"]
    assert _verdict(plain, "resistor_power", "R9", rails={"VX": 5.5},
                    policy={"resistor_power_w": {"0402": 0.5}}) == []


def test_zero_ohm_link_between_two_voltages_is_a_short():
    board = _with(res("R9", "5V", "3V3", value="0R", footprint="R0402"))
    found = _findings(_run(board), "resistor_power", "R9")[0]
    assert found["severity"] == "warning" and "short" in found["issue"]


def test_needs_current_for_leds_shunts_and_series_supply_resistors():
    board = _with(
        res("R9", "5V", "LED_A", value="330", footprint="R0402"),
        ("LED1", {"1": ("A", "LED_A"), "2": ("K", "GND")}, {"description": "LED red"}),
        res("R10", "5V", "LED_B", value="10k", footprint="R0402"),
        ("LED2", {"1": ("A", "LED_B"), "2": ("K", "GND")}, {"description": "LED green"}),
        res("R11", "BUCK_OUT", "VLOAD", value="0.01", footprint="R2512",
            description="Current sense shunt"),
        res("R12", "3V3", "VDDA", value="10", footprint="R0402"),
        ("U9", {"1": ("VDDA", "VDDA", "Power"), "2": ("GND", "GND", "Power")}, {}),
        ("U10", {"1": ("VDD", "VLOAD", "Power"), "2": ("GND", "GND", "Power")}, {}),
    )
    result = _run(board)
    led = _findings(result, "needs_current", "R9")[0]
    assert led["kind"] == "led_resistor" and set(led["designators"]) == {"R9", "LED1"}
    assert led["rail"] == "5V" and led["bound_w"] == pytest.approx(25 / 330, rel=1e-3)
    assert "forward voltage" in led["issue"]
    assert not _findings(result, "needs_current", "R10")  # bounded under the derated limit
    assert _findings(result, "needs_current", "R11")[0]["kind"] == "shunt"
    supply = _findings(result, "needs_current", "R12")[0]
    assert supply["kind"] == "series_supply" and supply["rail"] == "3V3"
    assert result["summary"]["parts_checked"]["leds"] == 2


def test_led_driven_from_a_signal_assumes_the_policy_level():
    board = _with(res("R9", "GPIO_LED", "LED_A", value="100", footprint="R0402"),
                  ("LED1", {"1": ("A", "LED_A"), "2": ("K", "GND")}, {}))
    found = _findings(_run(board), "needs_current", "R9")[0]
    assert "assumed at most 5 V" in found["issue"]
    assert not _findings(_run(board, policy={"signal_max_v": 1.0}), "needs_current", "R9")


def test_led_from_a_rail_of_unknown_voltage_names_the_rail():
    # The anode on VBAT (voltage not known), the resistor from the cathode to a GPIO.
    board = _with(("LED1", {"1": ("A", "VBAT"), "2": ("K", "LED_K")}, {"description": "LED"}),
                  res("R9", "LED_K", "GPIO_LED", value="100", footprint="R0402"),
                  ("U9", {"1": ("VBAT", "VBAT", "Power"), "2": ("GND", "GND", "Power")}, {}))
    found = _findings(_run(board), "needs_current", "R9")[0]
    assert "driven from VBAT through LED1, whose voltage is not known" in found["issue"]
    assert found["rail"] == "VBAT"


def test_one_end_known_resistor_needs_current_when_the_bound_is_high():
    board = _with(res("R9", "VX", "GATE", value="10", footprint="R0402"),
                  res("R10", "VX", "PULL", value="100k", footprint="R0402"))
    result = _run(board, rails={"VX": 12})
    found = _findings(result, "needs_current", "R9")[0]
    assert found["kind"] == "one_end_known" and found["bound_w"] == pytest.approx(14.4)
    assert not _findings(result, "needs_current", "R10")


def test_divider_legs_get_their_power_from_the_midpoint():
    # 39 ohm from 2V5 and 39 ohm to ground: 1.25 V across each, 40 mW in a 62.5 mW 0402
    # (above the derated half); the midpoint feeds only a capacitor and a connector pin.
    extra = (res("R9", "2V5", "MID", value="39", footprint="R0402"),
             res("R10", "MID", "GND", value="39", footprint="R0402"),
             cap("C9", "MID", value="100nF", description="CAP 100nF 16V X7R 0402"),
             ("J9", {"1": ("1", "MID"), "2": GND}, {"description": "Header"}))
    result = _run(_with(*extra), nets=["MID"])
    assert result["net_voltages"]["MID"]["source"] == "divider"
    assert result["net_voltages"]["MID"]["volts"] == 1.25
    for ref in ("R9", "R10"):
        found = _findings(result, "resistor_power", ref)
        assert [f["severity"] for f in found] == ["info"], ref
        assert found[0]["power_w"] == pytest.approx(1.25 ** 2 / 39, rel=1e-3)
    assert not _findings(result, "needs_current", "R9")
    # A supply pin on the midpoint draws current: not a divider.
    loaded = _with(*extra, ("U9", {"1": ("VDD", "MID", "Power"), "2": GND}, {}))
    assert _run(loaded, nets=["MID"])["net_voltages"]["MID"] is None


def test_unknown_rating_is_one_row_with_the_list():
    board = _with(cap("C9", "3V3", value="100nF"), cap("C10", "3V3", value="1uF"),
                  cap("C11", "NOWHERE", value="1uF"))
    found = _findings(_run(board), "unknown_rating")
    assert len(found) == 1 and found[0]["designators"] == ["C9", "C10"]
    assert found[0]["count"] == 2 and found[0]["severity"] == "info"


def test_a_lower_rating_from_a_tied_source_is_still_a_warning():
    board = _with(cap("C9", "VX", params={"voltage": "16V"}, mpn="GRM155R61A105KE15D"))
    found = _findings(_run(board, rails={"VX": 9}), "ceramic_voltage", "C9")
    assert [f["severity"] for f in found] == ["warning"]
    assert found[0]["rated_v"] == 10.0 and "rating sources disagree" in found[0]["issue"]
    # An outvoted lower rating (the text and the part number agree on 10 V) is not judged.
    board = _with(cap("C9", "VX", params={"voltage": "6.3V"}, mpn="GRM155R61A105KE15D",
                      description="CAP 1uF 10V X5R 0402"))
    assert not _findings(_run(board, rails={"VX": 6}), "ceramic_voltage", "C9")


@pytest.mark.parametrize("volts, expected", [(12.0, []), (12.5, ["info"]), (13.0, ["warning"])])
def test_a_capacitor_of_unknown_technology_is_judged_against_every_limit(volts, expected):
    # 16 V rated, no technology read: above 75% (a ceramic's limit) info, above 80% (the
    # most lenient) a warning.
    board = _with(cap("C9", "VX", description="Capacitor 10uF 16V", footprint="CAP_RADIAL"))
    found = _findings(_run(board, rails={"VX": volts}), "capacitor_voltage", "C9")
    assert [f["severity"] for f in found] == expected
    if found:
        assert "technology was not read" in found[0]["issue"]


def _array(ref, pins, **info):
    return (ref, {pin: (pin, net) for pin, net in pins.items()},
            {"pin_designators": list(pins), **info})


def test_an_isolated_array_with_one_element_on_ground_pairs_its_pins():
    # 4 x 22 ohm isolated: 1-8 from 3V3 to a signal, 2-7 a pull-down; 62.5 mW per element.
    pins = {"1": "3V3", "2": "NET_B", "3": "SIG_C", "4": "SIG_E", "5": "SIG_F", "6": "SIG_D",
            "7": "GND", "8": "NET_A"}
    board = _with(_array("RN1", pins, value="22", params={"power": "62.5 mW"}))
    result = _run(board, parts=["RN1"])
    assert result["parts"][0]["nets"] == [["3V3", "NET_A"], ["NET_B", "GND"],
                                          ["SIG_C", "SIG_D"], ["SIG_E", "SIG_F"]]
    assert not _findings(result, "resistor_power", "RN1")
    assert _findings(result, "needs_current", "RN1")[0]["kind"] == "one_end_known"
    # A bussed array's common pin (an end pin) on ground: each net to ground.
    bussed = {"1": "GND", **{str(n): f"S{n}" for n in range(2, 11)}}
    result = _run(_with(_array("RN2", bussed, value="10k")), parts=["RN2"])
    assert result["parts"][0]["nets"][0] == ["S2", "GND"]
    assert len(result["parts"][0]["nets"]) == 9


def test_an_array_takes_the_lowest_stated_power_per_element():
    # A package rating in a parameter, a per-element rating in the description.
    pins = {"1": "VX", "2": "B", "3": "C", "4": "D", "5": "E", "6": "F", "7": "G", "8": "GND"}
    board = _with(_array("RN1", pins, value="100", params={"power": "120 mW"},
                         description="Resistor array, 4 x 100 Ohm, 62.5 mW per element"))
    found = _findings(_run(board, rails={"VX": 2.0}), "resistor_power", "RN1")[0]
    # 2 V across 100 ohm is 40 mW: above half of 62.5 mW (half of 120 mW would pass).
    assert found["power_rating_w"] == 0.0625 and found["severity"] == "info"
    assert "per element" in found["power_rating_source"]


def test_identical_findings_group_and_not_fitted_parts_are_skipped():
    board = _with(*(cap(f"C{n}", "VX", value="1uF", description="CAP 1uF 10V X7R 0805")
                    for n in range(10, 14)))
    found = _findings(_run(board, rails={"VX": 9}), "ceramic_voltage")
    assert len(found) == 1 and found[0]["count"] == 4
    plan = VariantPlan(name="Lite", not_fitted={"C10", "C11"})
    found = _findings(_run(board, plan=plan, rails={"VX": 9}), "ceramic_voltage")
    assert found[0]["designators"] == ["C12", "C13"]


def test_checks_filter_and_errors():
    result = _run(checks=["dc_bias"])
    assert result["checks_run"] == ["dc_bias"]
    assert {f["check"] for f in result["findings"]} <= {"dc_bias"}
    assert _run(checks=["nope"])["error"] == "invalid_checks"
    assert _run(parts=["Q99"])["error"] == "part_not_found"
    assert _run(nets=["NOPE"])["error"] == "net_not_found"
    partly = _run(parts=["C4", "Q99"])
    assert any("Q99" in note for note in partly["notes"])
    assert any("U3 is not a fitted" in note for note in _run(parts=["U3"])["notes"])


# ---------- policy ----------

def test_default_policy_and_overrides():
    info, error = dr.load_policy(None)
    assert error is None and info["chosen_by"] == "default"
    assert info["values"] == dr.DEFAULT_POLICY
    info, _ = dr.load_policy(None, {"ceramic_max_ratio": 0.6,
                                    "resistor_power_w": {"0402": 0.1}})
    assert info["chosen_by"] == "argument" and info["values"]["ceramic_max_ratio"] == 0.6
    assert info["values"]["resistor_power_w"]["0402"] == 0.1
    assert info["values"]["resistor_power_w"]["0603"] == 0.1  # merged, not replaced
    assert dr.DEFAULT_POLICY["resistor_power_w"]["0402"] == 0.0625  # untouched


@pytest.mark.parametrize("override, word", [
    ({"nope": 1}, "unknown policy key"), ({"ceramic_max_ratio": 1.5}, "ratio"),
    ({"ceramic_max_ratio": True}, "ratio"), ({"dc_bias_max_case": "9999"}, "case code"),
    ({"resistor_power_w": {"0402": -1}}, "positive"), ({"resistor_power_w": {"big": 1}},
                                                       "not an imperial case code"),
    ({"resistor_power_w": 3}, "an object"), ({"signal_max_v": "5"}, "volts"), ([1], "object"),
])
def test_policy_argument_errors(override, word):
    info, error = dr.load_policy(None, override)
    assert info is None and error["error"] == "invalid_policy" and word in error["message"]
    assert "hint" in error


def test_policy_error_messages_read_as_sentences():
    _, error = dr.load_policy(None, {"resistor_power_w": {"0402M": 1}})
    assert error["message"] == ("policy argument: resistor_power_w has '0402M', which is not "
                                "an imperial case code (use 0201, 0402, 0603, ...).")
    _, error = dr.load_policy(None, {"resistor_power_w": {"0402": -1}})
    assert error["message"] == ("policy argument: resistor_power_w must map 0402 to a "
                                "positive number.")
    _, error = dr.load_policy(None, {"ceramic_max_ratio": 2})
    assert error["message"].startswith("policy argument: ceramic_max_ratio must be a ratio")


def test_policy_file(tmp_path):
    (tmp_path / dr.POLICY_FILE).write_text(json.dumps({
        "name": "House rules", "comment": "ours", "_ignored": 1,
        "policy": {"ceramic_max_ratio": 0.5, "_notes": {}},
    }), encoding="utf-8")
    info, error = dr.load_policy(tmp_path)
    assert error is None and info["chosen_by"] == "project_file"
    assert info["file"] == dr.POLICY_FILE and info["set_by_file"] == ["ceramic_max_ratio"]
    assert info["name"] == "House rules" and info["values"]["ceramic_max_ratio"] == 0.5
    info, _ = dr.load_policy(tmp_path, {"ceramic_max_ratio": 0.7})
    assert info["chosen_by"] == "project_file+argument"
    assert info["values"]["ceramic_max_ratio"] == 0.7
    board = _with(cap("C9", "VX", value="1uF", description="CAP 1uF 10V X7R 0805"))
    result = _run(board, rails={"VX": 6}, root_dir=tmp_path)
    assert result["policy"]["chosen_by"] == "project_file"
    assert _findings(result, "ceramic_voltage", "C9")[0]["limit"] == 0.5


@pytest.mark.parametrize("content, word", [
    ("{not json", "not valid JSON at line 1"), ("[1]", "one JSON object"),
    ('{"limits": {}}', "unknown key 'limits'"),
    ('{"policy": {"ceramic_max_ratio": 2}}', "ceramic_max_ratio must be"),
    ('{"policy": {"a": 1, "a": 2}}', "duplicate key"),
])
def test_policy_file_errors(tmp_path, content, word):
    (tmp_path / dr.POLICY_FILE).write_text(content, encoding="utf-8")
    info, error = dr.load_policy(tmp_path)
    assert info is None and error["error"] == "invalid_policy"
    assert word in error["message"] and error["file"] == dr.POLICY_FILE


def test_example_policy_file_is_the_default():
    root = Path(__file__).parents[1] / "examples"
    data = json.loads((root / "derating_policy.json").read_text("utf-8"))
    info, error = dr.load_policy(root)
    assert error is None and info["values"] == dr.DEFAULT_POLICY
    assert set(data["policy"]) - {"_notes"} == set(dr.DEFAULT_POLICY)


# ---------- output and paging ----------

def test_summary_comes_first_and_counts_sources():
    result = _run(rails={"VIN": 12}, vref=0.6)
    assert list(result)[:3] == ["variant", "summary", "severity_counts"]
    caps = result["summary"]["capacitors"]
    assert result["summary"]["parts_checked"] == {"capacitors": 4, "resistors": 2, "leds": 0}
    assert caps["rating_source"] == {"text": 4} and caps["case_source"] == {"text": 4}
    assert caps["applied_voltage_known"] == 3 and caps["bootstrap"] == 1
    assert "heuristic" in result and "Altium" in result["guidance"]
    json.dumps(result)


def test_paging():
    board = _with(*(cap(f"C{n}", f"V{n}", value="1uF", description="CAP 1uF 10V X7R 0805")
                    for n in range(10, 16)))
    rails = {f"V{n}": 9.0 + n / 100 for n in range(10, 16)}
    first = _run(board, rails=rails, limit=4)
    assert first["total"] == 6 and first["has_more"] and first["next_offset"] == 4
    rest = _run(board, rails=rails, offset=4, limit=4)
    assert not rest["has_more"] and "next_offset" not in rest
    seen = [f["designators"][0] for f in first["findings"] + rest["findings"]]
    assert sorted(seen) == [f"C{n}" for n in range(10, 16)]


def test_render_pages_parts_and_findings_together():
    prepared = dr.prepare_derating(_netlist(BOARD), nets=["3V3"])
    page = dr.render_derating(prepared, offset=0, limit=1)
    assert page["parts_total"] == 1 and len(page["parts"]) == 1
    assert page["net_voltages"]["3V3"]["source"] == "name"


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


def test_check_derating_tool(board, tmp_path):
    result = json.loads(main_mod.check_derating(rails="VIN=12", vref="U1=0.6"))
    assert result["variant"] == "Default" and "netlist_updated_utc" in result
    assert result["summary"]["capacitors"]["applied_voltage_known"] == 3
    assert "page_limited_to" not in result and result["policy"]["chosen_by"] == "default"
    detail = json.loads(main_mod.check_derating(part="C2", rails="VIN=12", vref="0.6"))
    assert detail["parts"][0]["across_v"] == 6.0
    (tmp_path / dr.POLICY_FILE).write_text('{"policy": {"ceramic_max_ratio": 0.3}}',
                                           encoding="utf-8")
    result = json.loads(main_mod.check_derating(rails="VIN=12", vref="0.6", policy={
        "ceramic_ripple_max_ratio": 0.3}))
    assert result["policy"]["chosen_by"] == "project_file+argument"
    # 12 V on 25 V and 6 V on 16 V, both ripple nets, are above 30%; 3.3 V on 16 V is not.
    assert {f["designators"][0] for f in _findings(result, "ceramic_voltage")} == {"C1", "C2"}


def test_check_derating_tool_errors(board, monkeypatch):
    assert json.loads(main_mod.check_derating(vref="nope"))["error"] == "invalid_vref"
    assert json.loads(main_mod.check_derating(rails="VIN"))["error"] == "invalid_rails"
    assert json.loads(main_mod.check_derating(checks=["x"]))["error"] == "invalid_checks"
    assert json.loads(main_mod.check_derating(part="Q99"))["error"] == "part_not_found"
    assert json.loads(main_mod.check_derating(net="NOPE"))["error"] == "net_not_found"
    assert json.loads(main_mod.check_derating(policy={"x": 1}))["error"] == "invalid_policy"
    assert json.loads(main_mod.check_derating(policy="0.5"))["error"] == "invalid_policy"
    monkeypatch.setattr(main_mod._altium, "_netlist", {**board, "components": {}})
    assert json.loads(main_mod.check_derating())["error"] == "no_components_loaded"
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.check_derating())["error"] == "no_project"


def test_check_derating_shrinks_to_fit(board, monkeypatch):
    many = _with(*(cap(f"C{n}", f"V{n}", value="1uF", description="CAP 1uF 10V X7R 0805")
                   for n in range(10, 70)))
    rails = ",".join(f"V{n}={9 + n / 1000}" for n in range(10, 70))
    monkeypatch.setattr(main_mod._altium, "_netlist", _netlist(many))
    full = main_mod.check_derating(rails=rails)
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", len(full) // 3)
    service = main_mod.prepare_derating.__globals__
    builds = []
    build = service["build_tree"]
    monkeypatch.setitem(service, "build_tree", lambda *a, **k: builds.append(1) or build(*a, **k))
    result = json.loads(main_mod.check_derating(rails=rails))
    assert builds == [1]  # read once, rendered per shrink step
    assert result["page_limited_to"] < 100 and result["has_more"]
    assert len(json.dumps(result, indent=2)) <= len(full) // 3
    rest = json.loads(main_mod.check_derating(rails=rails, offset=result["next_offset"]))
    assert rest["findings"][0]["designators"] != result["findings"][0]["designators"]


def test_tool_registration_manifest_readme_and_instructions():
    tools = {tool.name: tool for tool in asyncio.run(main_mod.mcp.list_tools())}
    tool = tools["check_derating"]
    assert tool.annotations.readOnlyHint is True and tool.title == "Check Derating"
    assert tool.meta["anthropic/maxResultSizeChars"] == main_mod.QUERY_NET_MAX_RESULT_SIZE_CHARS
    assert set(tool.inputSchema["properties"]) == {"part", "net", "checks", "rails", "vref",
                                                   "policy", "offset"}
    assert "heuristic" in tool.description and "next_offset" in tool.description
    assert "check_derating" in main_mod.NETLIST_TOOLS
    assert main_mod.SERVER_INSTRUCTIONS.index("check_derating") > 4096
    assert "check_derating" in main_mod.SCHEMATIC_REVIEW_PROMPT
    root = Path(__file__).parents[1]
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    assert "check_derating" in {entry["name"] for entry in manifest["tools"]}
    assert "| `check_derating` |" in (root / "README.md").read_text("utf-8")
    assert "`check_derating`" in (root / "CHANGELOG.md").read_text("utf-8")
