"""Driver/receiver logic-level candidates (services/logic_levels, check_logic_levels).
Synthetic boards with generic names only."""

import asyncio
import json
from pathlib import Path

import pytest

import server.main as main_mod
import server.services.logic_levels as ll
from server.parsers.prj_pcb import VariantDefinition, VariantState
from tests.test_pull_resistors import _netlist

GND = ("GND", "GND", "Power")
MCU = "ARM Cortex-M4 32-bit microcontroller"
SENSOR = "3-axis accelerometer"


def mcu(ref="U1", rail="3V3", **pins):
    """A processor on one supply; pins as name=net (Passive)."""
    spec = {"1": ("VDD", rail, "Power"), "2": GND}
    for index, (name, net) in enumerate(pins.items(), start=10):
        spec[str(index)] = (name, net) if isinstance(net, str) else (name, *net)
    return (ref, spec, {"description": MCU})


def chip(ref, supplies: dict, description=SENSOR, **pins):
    """A part with supplies {pin name: net} and signal pins name=net or (net, type)."""
    spec = {"0": GND}
    index = 1
    for name, net in supplies.items():
        spec[str(index)] = (name, net, "Power")
        index += 1
    for name, net in pins.items():
        spec[str(index)] = (name, net) if isinstance(net, str) else (name, *net)
        index += 1
    return (ref, spec, {"description": description})


def res(ref, net_a, net_b, value="10k"):
    return (ref, {"1": ("1", net_a), "2": ("2", net_b)}, {"value": value,
                                                         "description": "Resistor"})


def _run(board, **kwargs):
    kwargs.setdefault("limit", 1000)
    return ll.run_logic_level_check(_netlist(board), **kwargs)


def _findings(result, check=None, severity=None):
    return [row for row in result["findings"] if (check is None or row["check"] == check)
            and (severity is None or row["severity"] == severity)]


def _chips(board, rails=None):
    netlist = _netlist(board)
    tree = ll.build_tree(netlist)
    voltages, _ = ll.net_voltages(tree, ll.tree_data(tree), rails)
    return ll.build_chips(tree, voltages), voltages


def _rail(board, ref, pin, rails=None, direction=None):
    chips, voltages = _chips(board, rails)
    return ll.pin_rail(chips[ref], pin, voltages, direction)


# An MCU on 3V3 talking SPI to a sensor on 1V8.
SPI = [
    mcu(PA5="SPI_SCK", PA6="SPI_MISO", PA7="SPI_MOSI", PA4="SPI_CS"),
    chip("U2", {"VDD": "1V8"}, SCK="SPI_SCK", SDO="SPI_MISO", SDI="SPI_MOSI", CS="SPI_CS"),
]


# ---------- supply classes and I/O rails ----------

@pytest.mark.parametrize("name, cls", [
    ("VDD", "digital"), ("VCC", "digital"), ("VDDIO", "io"), ("VDD_IO", "io"),
    ("VCCIO2A", "io"), ("VCCO_14", "io"), ("OVDD", "io"), ("VL", "io"), ("DVDD", "dvdd"),
    ("AVDD", "analog"), ("VDDA", "analog"), ("VCCINT", "core"), ("VCCA_PLL", "core"),
    ("VBAT", "backup"), ("VPP", "skip"), ("VBUS", "skip"), ("VREF", "skip"),
    ("3.3 V", "digital"), ("DRVDD", "io"), ("VIN", "power_in"), ("PVIN", "power_in"),
    ("VIN1", "power_in"), ("VDDH", "power_in"), ("DVDDH", "io"),
])
def test_supply_class(name, cls):
    assert ll._supply_class(name) == cls


@pytest.mark.parametrize("name, bank", [
    ("VCCO_14", "14"), ("VCCO_0", "0"), ("VCCIO3G", "3G"), ("VCCIO_HPS", "HPS"),
    ("VDDIO2", "2"), ("VCCO", None), ("VDDIO", None), ("VDDIO_1V8", None), ("VDDIO18", None),
    ("VCCO_33", "33"),
])
def test_supply_bank(name, bank):
    assert ll._supply_bank(name) == bank


def test_rail_of_a_single_supply_part():
    rail = _rail(SPI, "U2", "4")
    assert (rail.net, rail.basis, rail.confidence, rail.volts) == ("1V8", "only_supply",
                                                                  "high", 1.8)


def test_rail_of_a_vddio_pin_wins_over_the_core_supply():
    board = [chip("U2", {"VDD": "3V3", "VDDIO": "1V8"}, SCK="SPI_SCK")]
    rail = _rail(board, "U2", "3")
    assert (rail.net, rail.basis, rail.confidence) == ("1V8", "io_supply", "high")


def test_rail_of_a_digital_supply_among_analog_ones():
    board = [chip("U2", {"AVDD": "5V", "DVDD": "3V3"}, SCLK="SPI_SCK")]
    rail = _rail(board, "U2", "3")
    assert (rail.net, rail.basis, rail.confidence) == ("3V3", "digital_supply", "medium")


def test_rail_of_several_digital_supplies_is_the_highest_at_low_confidence():
    board = [chip("U2", {"VCC": "5V", "VDD": "3V3"}, SCLK="SPI_SCK")]
    rail = _rail(board, "U2", "3")
    assert (rail.net, rail.basis, rail.confidence) == ("5V", "highest_supply", "low")


def test_rail_of_a_part_without_supply_pins():
    board = [chip("U2", {}, SCLK="SPI_SCK")]
    rail = _rail(board, "U2", "1")
    assert (rail.net, rail.basis) == (None, "none")


FPGA = ("U1", {
    "1": ("VCCO_14", "3V3", "Power"), "2": ("VCCO_15", "1V8", "Power"),
    "3": ("VCCINT", "1V0", "Power"), "4": ("VCCO_0", "2V5", "Power"), "5": GND,
    "10": ("IO_L1P_T0_14", "A_14", "IO"), "11": ("IO_L2N_T0_15", "A_15", "IO"),
    "12": ("DONE_0", "DONE", "IO"), "13": ("IO_25_14", "B_14", "IO"),
    "14": ("PROGRAM_B", "PROG", "Input"),
}, {"description": "Artix-7 FPGA"})


@pytest.mark.parametrize("pin, net, bank", [
    ("10", "3V3", "14"), ("11", "1V8", "15"), ("12", "2V5", "0"), ("13", "3V3", "14"),
])
def test_rail_of_an_fpga_pin_is_its_bank(pin, net, bank):
    rail = _rail([FPGA], "U1", pin)
    assert (rail.net, rail.basis, rail.confidence) == (net, "bank", "high")
    assert f"bank {bank}" in rail.detail


def test_fpga_pin_whose_bank_is_not_in_its_name_takes_the_highest_bank_low():
    rail = _rail([FPGA], "U1", "14")
    assert (rail.net, rail.basis, rail.confidence) == ("3V3", "highest_supply", "low")


def test_intel_bank_names():
    board = [("U1", {
        "1": ("VCCIO3G", "1V8", "Power"), "2": ("VCCIO2A", "1V2", "Power"),
        "3": ("VCCIO_HPS", "3V3", "Power"), "4": GND,
        "10": ("IO, LVDS3G_6p", "N1", "IO"), "11": ("IO, CLK_2A_1n, LVDS2A_3n", "N2", "IO"),
        "12": ("IO, HPS_DDR, LVDS2A_9p", "N3", "IO"), "13": ("HPS_CLK1", "N4", "Input"),
    }, {"description": "Arria 10 FPGA"})]
    assert _rail(board, "U1", "10").net == "1V8"
    assert _rail(board, "U1", "11").net == "1V2"
    assert _rail(board, "U1", "12").net == "1V2"  # the vendor bank pattern wins over HPS
    assert _rail(board, "U1", "13").net == "3V3"


TRANSLATOR = ("U3", {
    "1": ("VCCA", "1V8", "Power"), "2": ("VCCB", "3V3", "Power"), "3": GND,
    "4": ("A1", "LOW_SIDE", "IO"), "5": ("B1", "HIGH_SIDE", "IO"), "6": ("DIR", "DIR_NET"),
}, {"description": "Dual-supply bus transceiver, voltage level translator"})


def test_translator_sides():
    assert _rail([TRANSLATOR], "U3", "4").net == "1V8"
    assert _rail([TRANSLATOR], "U3", "5").net == "3V3"
    direction = _rail([TRANSLATOR], "U3", "6")
    assert direction.net == "1V8" and "A (low) side" in direction.detail


def test_isolator_sides_by_pin_half():
    board = [("U4", {
        "1": ("VDD1", "3V3", "Power"), "2": ("GND1", "GND", "Power"), "3": ("INA", "IN_SIDE"),
        "4": ("NC", "NC4"), "5": ("NC", "NC5"), "6": ("NC", "NC6"), "7": ("NC", "NC7"),
        "8": ("NC", "NC8"), "9": ("NC", "NC9"), "10": ("NC", "NC10"), "11": ("NC", "NC11"),
        "12": ("NC", "NC12"), "13": ("NC", "NC13"), "14": ("OUTA", "OUT_SIDE"),
        "15": ("GND2", "GND_ISO", "Power"), "16": ("VDD2", "5V", "Power"),
    }, {"description": "Digital isolator, 2 channels"})]
    inside, outside = _rail(board, "U4", "3"), _rail(board, "U4", "14")
    assert (inside.net, outside.net) == ("3V3", "5V")
    assert inside.confidence == "medium" and "half" in inside.detail


def test_clock_outputs_take_their_output_supply():
    board = [("U5", {
        "1": ("VDD", "3V3", "Power"), "2": ("VDDO0", "1V8", "Power"),
        "3": ("VDDO1", "2V5", "Power"), "4": GND,
        "5": ("CLK0", "CK0", "Output"), "6": ("CLK1", "CK1", "Output"),
        "7": ("SCL", "AUX_SCL", "Input"),
    }, {"description": "Programmable clock generator"})]
    chips, voltages = _chips(board)
    assert chips["U5"].cls == "clock"
    assert ll.pin_rail(chips["U5"], "5", voltages, "out").net == "1V8"
    assert ll.pin_rail(chips["U5"], "6", voltages, "out").net == "2V5"
    assert ll.pin_rail(chips["U5"], "7", voltages, "in").net == "3V3"


# ---------- directions ----------

@pytest.mark.parametrize("description, name, electrical, expected", [
    (SENSOR, "SDO", "Passive", ("out", True)), (SENSOR, "SDI", "Passive", ("in", True)),
    (SENSOR, "SCK", "Passive", ("in", True)), (SENSOR, "CS", "Passive", ("in", True)),
    (MCU, "MISO", "Passive", ("in", True)), (MCU, "MOSI", "Passive", ("out", True)),
    (MCU, "UART5_TX", "Passive", ("out", True)), (MCU, "UART5_RX", "Passive", ("in", True)),
    ("CAN transceiver", "TXD", "Passive", ("in", True)),
    ("CAN transceiver", "RXD", "Passive", ("out", True)),
    ("10/100 Ethernet PHY", "TXD0", "Passive", ("in", True)),
    (SENSOR, "TDO", "Passive", ("out", True)), (SENSOR, "TCK", "Passive", ("in", True)),
    (SENSOR, "SDA", "Passive", ("od", True)), (SENSOR, "INT1", "Passive", ("od_out", False)),
    (SENSOR, "RESET", "Passive", ("in", True)), (MCU, "PA5", "Passive", ("unknown", False)),
    (SENSOR, "OUT1", "Passive", ("out", True)), (SENSOR, "SDO", "Input", ("in", True)),
    (SENSOR, "X", "Output", ("out", True)), (SENSOR, "X", "IO", ("bidir", True)),
    (SENSOR, "X", "OpenCollector", ("od", True)), (SENSOR, "X", "HiZ", ("out", True)),
    ("Hex inverter, 74HC04", "1Y", "Passive", ("out", True)),
    ("Hex inverter, 74HC04", "1A", "Passive", ("in", True)),
])
def test_pin_direction(description, name, electrical, expected):
    board = [chip("U2", {"VDD": "3V3"}, description=description, **{name: ("SIG", electrical)})]
    chips, _ = _chips(board)
    direction, certain, _ = ll.pin_direction(chips["U2"], "2", electrical, "SIG")
    assert (direction, certain) == expected


@pytest.mark.parametrize("name, reason", [
    ("ADC_IN0", "analog"), ("XTAL_IN", "crystal"), ("USB_DP", "differential"),
    ("VREF", "power"),
])
def test_pin_exclusions(name, reason):
    board = [chip("U2", {"VDD": "3V3"}, **{name: "SIG"})]
    chips, _ = _chips(board)
    assert ll.pin_direction(chips["U2"], "2", "Passive", "SIG")[0] == reason


def test_processor_gpio_with_alternate_analog_function_is_still_a_gpio():
    board = [mcu(**{"PA0/ADC_IN0": "SIG"})]
    chips, _ = _chips(board)
    assert ll.pin_direction(chips["U1"], "10", "Passive", "SIG")[0] == "unknown"


def test_processor_gpio_direction_from_the_net_name():
    board = [mcu(PA9="DEBUG_UART_TX")]
    chips, _ = _chips(board)
    direction, certain, basis = ll.pin_direction(chips["U1"], "10", "Passive",
                                                 "DEBUG_UART_TX")
    assert (direction, certain) == ("out", False) and "net name" in basis


# ---------- candidates ----------

def test_spi_to_a_lower_rail_sensor():
    result = _run(SPI)
    over = _findings(result, "receiver_overvoltage")
    assert len(over) == 1 and over[0]["severity"] == "warning"
    assert over[0]["nets"] == ["SPI_CS", "SPI_MOSI", "SPI_SCK"] and over[0]["count"] == 3
    assert over[0]["driver"]["rail"] == "3V3" and over[0]["receiver"]["rail"] == "1V8"
    low = _findings(result, "driver_low_for_receiver")
    assert len(low) == 1 and low[0]["severity"] == "info" and low[0]["nets"] == ["SPI_MISO"]
    assert "VOH < VIH" in low[0]["issue"] and "datasheet" in low[0]["issue"]
    summary = result["summary"]
    assert summary["signals"]["checked"] == 4 and summary["ics"]["by_basis"] == {
        "only_supply": 2}
    assert summary["candidates"]["receiver_overvoltage"] == {"warning": 1}


def _pair(driver_v, receiver_v, receiver_type="Input", **kwargs):
    """A typed Output on rail VA driving a pin of receiver_type on rail VB."""
    board = [chip("U1", {"VDD": "VA"}, OUT=("SIG", "Output")),
             chip("U2", {"VDD": "VB"}, description=kwargs.pop("description", SENSOR),
                  **{kwargs.pop("pin", "IN"): ("SIG", receiver_type)}),
             *kwargs.pop("extra", ())]
    return _run(board, rails={"VA": driver_v, "VB": receiver_v, **kwargs.pop("rails", {})},
                **kwargs)


@pytest.mark.parametrize("driver_v, receiver_v, expected", [
    (1.8, 5.0, "warning"), (1.65, 3.3, "warning"), (1.8, 3.3, "info"), (2.3, 3.3, "info"),
    (2.4, 3.3, None), (3.3, 3.3, None), (3.3, 5.0, "info"), (2.5, 5.0, "warning"),
])
def test_driver_low_thresholds(driver_v, receiver_v, expected):
    rows = _findings(_pair(driver_v, receiver_v), "driver_low_for_receiver")
    assert [row["severity"] for row in rows] == ([expected] if expected else [])


@pytest.mark.parametrize("driver_v, receiver_v, expected", [
    (3.3, 3.0, None), (3.4, 3.0, "warning"), (5.0, 3.3, "warning"), (3.3, 1.8, "warning"),
])
def test_overvoltage_thresholds(driver_v, receiver_v, expected):
    rows = _findings(_pair(driver_v, receiver_v), "receiver_overvoltage")
    assert [row["severity"] for row in rows] == ([expected] if expected else [])


def test_five_volt_tolerant_pin_name_softens():
    rows = _findings(_pair(5.0, 3.3, pin="PA9_FT"), "receiver_overvoltage")
    assert rows[0]["severity"] == "info" and "tolerant" in rows[0]["softened"]
    # Above 5.5 V even a tolerant pin is a warning.
    rows = _findings(_pair(6.0, 3.3, pin="PA9_FT"), "receiver_overvoltage")
    assert rows[0]["severity"] == "warning"


@pytest.mark.parametrize("description", [
    "Single buffer, 5V tolerant inputs", "SN74LVC1G17 single Schmitt-trigger buffer",
])
def test_five_volt_tolerant_part_softens(description):
    rows = _findings(_pair(5.0, 3.3, description=description, pin="A"),
                     "receiver_overvoltage")
    assert rows[0]["severity"] == "info" and "tolerant" in rows[0]["softened"]


def test_series_resistor_softens_overvoltage():
    board = [chip("U1", {"VDD": "5V"}, OUT=("SIG_A", "Output")),
             res("R1", "SIG_A", "SIG_B", "10k"),
             chip("U2", {"VDD": "3V3"}, IN=("SIG_B", "Input"))]
    result = _run(board)
    rows = _findings(result, "receiver_overvoltage")
    assert rows[0]["severity"] == "info" and rows[0]["series"]["designator"] == "R1"
    assert "injection-current" in rows[0]["softened"]
    assert result["summary"]["signals"]["series_resistor_links"] == 1


def test_resistor_divider_lowers_the_level():
    board = [chip("U1", {"VDD": "5V"}, OUT=("SIG_A", "Output")),
             res("R1", "SIG_A", "SIG_B", "10k"), res("R2", "SIG_B", "GND", "20k"),
             chip("U2", {"VDD": "3V3"}, IN=("SIG_B", "Input"))]
    assert not _findings(_run(board), "receiver_overvoltage")
    # A divider that drops the level too far is too low.
    board[2] = res("R2", "SIG_B", "GND", "2k")
    rows = _findings(_run(board), "driver_low_for_receiver")
    assert rows and rows[0]["severity"] == "warning"
    assert "divided" in rows[0]["driver"]["level_basis"]


def test_low_series_resistor_joins_one_signal():
    board = [chip("U1", {"VDD": "5V"}, OUT=("SIG_A", "Output")),
             res("R1", "SIG_A", "SIG_B", "33"),
             chip("U2", {"VDD": "3V3"}, IN=("SIG_B", "Input"))]
    rows = _findings(_run(board), "receiver_overvoltage")
    assert rows[0]["severity"] == "warning" and rows[0]["nets"] == ["SIG_A", "SIG_B"]


I2C = [mcu(PB6="AUX_SCL", PB7="AUX_SDA"),
       chip("U2", {"VDD": "1V8"}, SCL="AUX_SCL", SDA="AUX_SDA")]


def test_open_drain_pull_up_above_a_receiver():
    board = [*I2C, res("R1", "AUX_SCL", "3V3", "4k7"), res("R2", "AUX_SDA", "3V3", "4k7")]
    result = _run(board)
    rows = _findings(result, "open_drain_pullup_mismatch")
    assert len(rows) == 1 and rows[0]["severity"] == "warning"
    assert rows[0]["receiver"]["designator"] == "U2"
    assert rows[0]["pull_up"]["rail"] == "3V3" and rows[0]["pull_up"]["volts"] == 3.3
    assert result["summary"]["signals"]["open_drain"] == 2


def test_open_drain_pull_up_below_a_receivers_vih():
    board = [*I2C, res("R1", "AUX_SCL", "1V8", "4k7"), res("R2", "AUX_SDA", "1V8", "4k7")]
    rows = _findings(_run(board), "open_drain_pullup_mismatch")
    assert len(rows) == 1 and rows[0]["severity"] == "info"
    assert rows[0]["receiver"]["designator"] == "U1" and rows[0]["kind"] == "below_vih"


def test_open_collector_output_with_a_pull_up():
    board = [chip("U1", {"VDD": "3V3"}, ALERT=("ALERT_N", "OpenCollector")),
             res("R1", "ALERT_N", "5V"),
             chip("U2", {"VDD": "3V3"}, INT=("ALERT_N", "Input"))]
    rows = _findings(_run(board), "open_drain_pullup_mismatch")
    assert rows[0]["severity"] == "warning" and rows[0]["receiver"]["designator"] == "U2"


def test_open_drain_line_without_pull_up_is_not_judged():
    result = _run(I2C)
    assert not _findings(result, "open_drain_pullup_mismatch")
    assert result["summary"]["signals"]["open_drain_without_pull_up"] == 2


def test_pull_up_on_a_push_pull_line():
    board = [chip("U1", {"VDD": "3V3"}, OUT=("SIG", "Output")), res("R1", "SIG", "5V"),
             chip("U2", {"VDD": "3V3"}, IN=("SIG", "Input"))]
    rows = _findings(_run(board), "receiver_overvoltage")
    assert {row["receiver"]["designator"] for row in rows} == {"U1", "U2"}
    assert all(row["pull_up"]["designators"] == ["R1"] and row["severity"] == "warning"
               for row in rows)
    assert all("whenever no driver holds the line" in row["issue"] for row in rows)
    # The output pin is back-driven by the pull-up too: judged, and named as an output.
    output = next(row for row in rows if row["receiver"]["designator"] == "U1")
    assert output["receiver"]["pin_role"] == "push-pull output"
    assert "back-driven" in output["issue"]
    # A pull-up within the output's own rail is fine.
    board[1] = res("R1", "SIG", "3V3")
    assert not _findings(_run(board), "receiver_overvoltage")


def test_bidirectional_pins_are_judged_both_ways_at_info():
    board = [chip("U1", {"VDD": "3V3"}, D0=("DATA0", "IO")),
             chip("U2", {"VDD": "1V8"}, D0=("DATA0", "IO"))]
    result = _run(board)
    severities = {(row["check"], row["severity"]) for row in result["findings"]}
    assert severities == {("receiver_overvoltage", "info"), ("driver_low_for_receiver", "info")}
    assert "both ways" in _findings(result, "receiver_overvoltage")[0]["issue"]


def test_bidirectional_pin_facing_an_output_receives():
    board = [chip("U1", {"VDD": "3V3"}, OUT=("SIG", "Output")),
             chip("U2", {"VDD": "1V8"}, D0=("SIG", "IO"))]
    result = _run(board)
    assert [(row["check"], row["severity"]) for row in result["findings"]] == [
        ("receiver_overvoltage", "warning")]


def test_low_confidence_rail_caps_at_info():
    board = [chip("U1", {"VDD": "3V3"}, OUT=("SIG", "Output")),
             chip("U2", {"VCC": "1V8", "VDD": "1V2"}, IN=("SIG", "Input"))]
    rows = _findings(_run(board), "receiver_overvoltage")
    assert rows[0]["severity"] == "info" and "low-confidence" in rows[0]["issue"]


def test_regulator_enable_has_fixed_thresholds():
    board = [chip("U1", {"VDD": "1V8"}, GPIO_EN=("REG_EN", "Output")),
             ("U2", {"1": ("VIN", "12V", "Power"), "2": ("EN", "REG_EN", "Input"),
                     "3": ("SW", "SW_NODE"), "4": ("FB", "FB_NET"), "5": GND},
              {"description": "Synchronous step-down converter"})]
    result = _run(board)
    assert not _findings(result, "driver_low_for_receiver")


def test_gate_driver_input_is_info_only():
    board = [chip("U1", {"VDD": "1V8"}, OUT=("PWM_H", "Output")),
             chip("U2", {"VDD": "12V"}, description="Half-bridge gate driver",
                  HIN=("PWM_H", "Input"))]
    rows = _findings(_run(board), "driver_low_for_receiver")
    assert rows[0]["severity"] == "info" and "fixed" in rows[0]["issue"]


def test_translator_wired_correctly_and_backwards():
    board = [chip("U1", {"VDD": "1V8"}, OUT=("LOW_SIDE", "Output")), TRANSLATOR,
             chip("U2", {"VDD": "3V3"}, IN=("HIGH_SIDE", "Input"))]
    result = _run(board)
    assert not [row for row in result["findings"] if row["check"] != "unknown_rail"]
    assert result["translators"][0]["designator"] == "U3"
    assert result["translators"][0]["sides"]["A"]["rail"] == "1V8"
    assert result["summary"]["translators_handled"] == 1
    backwards = [chip("U1", {"VDD": "3V3"}, OUT=("LOW_SIDE", "Output")), TRANSLATOR]
    rows = _findings(_run(backwards), "receiver_overvoltage")
    assert rows[0]["severity"] == "warning" and rows[0]["receiver"]["designator"] == "U3"


# ---------- exclusions and unknown rails ----------

@pytest.mark.parametrize("net, reason", [
    ("ADC_IN3", "analog"), ("LVDS_CLK_P", "differential"), ("CAN_H_X", None),
    ("CLK_A_P", "differential"), ("RX2P", "differential"),
])
def test_excluded_nets(net, reason):
    board = [chip("U1", {"VDD": "3V3"}, OUT=(net, "Output")),
             chip("U2", {"VDD": "1V8"}, IN=(net, "Input"))]
    if net.endswith("P"):
        board += [chip("U3", {"VDD": "3V3"}, OUT=(net[:-1] + "N", "Output")),
                  chip("U4", {"VDD": "1V8"}, IN=(net[:-1] + "N", "Input"))]
    result = _run(board)
    excluded = result["summary"]["signals"]["excluded"]
    if reason:
        assert excluded.get(reason) and not _findings(result, "receiver_overvoltage")
    else:
        assert _findings(result, "receiver_overvoltage")


def test_active_low_name_is_no_differential_pair():
    board = [chip("U1", {"VDD": "3V3"}, OUT=("RESET_N", "Output")),
             chip("U2", {"VDD": "1V8"}, IN=("RESET_N", "Input"))]
    assert _findings(_run(board), "receiver_overvoltage")


def test_crystal_and_one_ic_nets_are_left_out():
    board = [chip("U1", {"VDD": "3V3"}, OSC=("XO_NET", "Output"), LONE=("LONE", "Output")),
             chip("U2", {"VDD": "1V8"}, CLKIN=("XO_NET", "Input")),
             ("Y1", {"1": ("1", "XO_NET"), "2": ("2", "GND")}, {"description": "Crystal 25MHz"})]
    excluded = _run(board)["summary"]["signals"]["excluded"]
    assert excluded["crystal"] == 1 and excluded["one_ic"] >= 1


def test_unknown_rail_row_and_rails_argument():
    board = [chip("U1", {"VDD": "3V3"}, OUT=("SIG", "Output")),
             chip("U2", {"VDD": "VIO_X"}, IN=("SIG", "Input"))]
    result = _run(board)
    row = _findings(result, "unknown_rail")[0]
    assert row["rails"][0] == {"rail": "VIO_X", "signals": 1, "example_nets": ["SIG"]}
    assert "rails='NAME=volts'" in row["issue"] and result["summary"]["unknown_rails"][0][
        "rail"] == "VIO_X"
    given = _run(board, rails={"VIO_X": 1.8})
    assert not _findings(given, "unknown_rail") and _findings(given, "receiver_overvoltage")


def test_rails_naming_no_net_is_noted():
    result = _run(SPI, rails={"NOPE": 1.0})
    assert any("NOPE" in note for note in result["notes"])


# ---------- filters, rows and paging ----------

def test_part_filter_lists_each_pin_rail():
    result = _run(SPI, parts=["U2"])
    row = result["parts"][0]
    assert row["designator"] == "U2" and row["supplies"][0]["net"] == "1V8"
    assert row["pins_on_signals"] == 4 and result["part_pins_total"] == 4
    assert {pin["direction"] for pin in result["part_pins"]} == {"in", "out"}
    assert {pin["designator"] for pin in result["part_pins"]} == {"U2"}
    assert all(f["receiver"]["designator"] == "U2" or f["driver"]["designator"] == "U2"
               for f in result["findings"] if f["check"] != "unknown_rail")


def test_net_filter_lists_the_signal():
    result = _run(SPI, nets=["SPI_SCK"])
    signal = result["signals"][0]
    assert signal["nets"] == ["SPI_SCK"] and len(signal["ends"]) == 2
    assert [f["nets"] for f in result["findings"]] == [["SPI_CS", "SPI_MOSI", "SPI_SCK"]]


def test_check_filter_and_errors():
    result = _run(SPI, checks=["driver_low_for_receiver"])
    assert {row["check"] for row in result["findings"]} == {"driver_low_for_receiver"}
    assert result["checks_run"] == ["driver_low_for_receiver"]
    assert _run(SPI, checks=["nope"])["error"] == "invalid_checks"
    assert _run(SPI, parts=["Q99"])["error"] == "part_not_found"
    assert _run(SPI, nets=["NOPE"])["error"] == "net_not_found"


def _many(count=30):
    board = [mcu(**{f"PA{n}": (f"SIG{n}", "Output") for n in range(count)})]
    board += [chip(f"U{n + 10}", {"VDD": "1V8"}, IN=(f"SIG{n}", "Input"))
              for n in range(count)]
    return board


def test_paging():
    first = _run(_many(6), limit=4)
    assert first["total"] == 6 and first["has_more"] and first["next_offset"] == 4
    rest = _run(_many(6), offset=4, limit=4)
    assert not rest["has_more"] and "next_offset" not in rest
    seen = [row["receiver"]["designator"] for row in first["findings"] + rest["findings"]]
    assert len(set(seen)) == 6


def test_result_shape():
    result = _run(SPI)
    assert list(result)[:3] == ["variant", "summary", "severity_counts"]
    assert "datasheet" in result["heuristic"] and "Altium" in result["guidance"]
    json.dumps(result)


# ---------- rails, directions and lines fixed on review ----------

def test_numbered_port_supply_is_not_the_parts_io_rail():
    board = [chip("U1", {"VDD": "3V3", "VDDA": "3V3", "VDDIO2": "1V8"}, description=MCU,
                  PA5=("SPI_SCK", "IO"), PA6=("SPI_MISO", "IO")),
             chip("U2", {"VDD": "3V3"}, SCK=("SPI_SCK", "Input"), SDO=("SPI_MISO", "Output"))]
    rail = _rail(board, "U1", "4")
    assert (rail.net, rail.confidence) == ("3V3", "low") and "VDDIO2" in rail.detail
    assert not _findings(_run(board), severity="warning")
    # VDDIO2 tied to VDD: one rail, read with high confidence.
    same = [chip("U1", {"VDD": "3V3", "VDDIO2": "3V3"}, description=MCU, PA5="SPI_SCK")]
    assert _rail(same, "U1", "3").confidence == "high"


def test_fpga_core_vcc_is_not_an_unbanked_pins_rail():
    board = [("U1", {
        "1": ("VCC", "1V2", "Power"), "2": ("VCCIO_0", "3V3", "Power"),
        "3": ("VCCIO_1", "3V3", "Power"), "4": ("VCCIO_2", "1V8", "Power"), "5": GND,
        "10": ("IOT_37A", "SIG_X", "IO")}, {"description": "iCE40 FPGA"}),
        chip("U2", {"VDD": "3V3"}, IN=("SIG_X", "Input"))]
    rail = _rail(board, "U1", "10")
    assert (rail.net, rail.basis, rail.confidence) == ("3V3", "highest_supply", "low")
    assert not _findings(_run(board), severity="warning")


@pytest.mark.parametrize("supplies, expected", [
    ({"VIN0": "12V", "VIN1": "12V", "VDD33": "VDD33_NET"}, "VDD33_NET"),
    ({"PVIN": "12V", "AVIN": "5V"}, "5V"),
    ({"VIN": "12V"}, "12V"),
])
def test_regulator_power_input_is_not_its_logic_rail(supplies, expected):
    board = [chip("U1", supplies, description="Step-down regulator module with PMBus",
                  SDA=("PMB_SDA", "IO"), PG=("PWR_OK", "Output"))]
    rail = _rail(board, "U1", str(len(supplies) + 1))
    assert rail.net == expected
    if len(supplies) > 1:
        assert "power input" in rail.detail


def test_bias_outputs_are_not_a_parts_rail():
    cap = ("C1", {"1": ("1", "BIAS_NET"), "2": ("2", "GND")},
           {"value": "100n", "description": "Capacitor"})
    regulator = ("U1", {"1": ("PVIN", "12V", "Power"), "2": ("AVIN", "AVIN_NET", "Power"),
                        "3": ("VDDB", "BIAS_NET2", "Power"), "4": ("EN", "REG_EN", "Input"),
                        "5": GND}, {"description": "Synchronous step-down converter"})
    assert _rail([regulator], "U1", "4").net == "AVIN_NET"
    # A supply pin on a net only the part and capacitors reach: its own internal output.
    board = [chip("U2", {"VDD": "3V3", "VDD_INT": "BIAS_NET"}, SCK="SPI_SCK"), cap]
    rail = _rail(board, "U2", "3")
    assert (rail.net, rail.basis, rail.confidence) == ("3V3", "only_supply", "high")


def test_radio_soc_regulator_input_is_not_its_io_rail():
    board = [chip("U1", {"VDD": "3V0", "VDDH": "5V"}, description="Bluetooth Low Energy SoC",
                  P0_05=("GPIO_LINE", "IO")),
             chip("U2", {"VDD": "3V3"}, OUT=("GPIO_LINE", "Output"))]
    assert _rail(board, "U1", "3").net == "3V0"
    assert not _findings(_run(board), "driver_low_for_receiver")


OSCILLATOR = chip("U1", {"VDD": "3V3"}, description="Oscillator 25 MHz",
                  OUT=("CLK25", "Output"))


@pytest.mark.parametrize("name", ["CLKIN", "XTAL_IN", "XI"])
def test_crystal_pin_driven_by_an_active_clock_is_judged_at_info(name):
    board = [OSCILLATOR, chip("U2", {"VDDIO": "1V8", "VDD": "1V8"}, description=MCU,
                              **{name: ("CLK25", "Input")})]
    result = _run(board)
    rows = _findings(result, "receiver_overvoltage")
    assert len(rows) == 1 and rows[0]["severity"] == "info"
    assert "crystal-oscillator pin" in rows[0]["receiver"]["basis"]
    assert "crystal" not in result["summary"]["signals"]["excluded"]


def test_oscillator_output_named_output_drives_a_crystal_pin():
    board = [chip("U1", {"VCC": "2V5"}, description="Oscillator (SPXO) 25 MHz",
                  OUTPUT="CLK25"),
             chip("U2", {"DVDDH": "1V8", "AVDDH": "3V3"}, description="Gigabit Ethernet PHY",
                  XI="CLK25")]
    result = _run(board, nets=["CLK25"])
    ends = {end["pin"]: end for end in result["signals"][0]["ends"]}
    assert ends["U1.2 (OUTPUT)"]["direction"] == "out"
    # The crystal pin is judged against the part's highest supply (its oscillator's).
    assert ends["U2.3 (XI)"]["rail"] == "3V3" and ends["U2.3 (XI)"]["confidence"] == "low"
    assert not [row for row in result["findings"] if row["check"] != "unknown_rail"]


def test_crystal_line_without_an_active_driver_stays_excluded():
    board = [chip("U1", {"VDD": "3V3"}, description=MCU, XTAL_OUT="XO_LINE"),
             chip("U2", {"VDD": "1V8"}, XTAL_IN="XO_LINE")]
    assert _run(board)["summary"]["signals"]["excluded"] == {"crystal": 1}


def test_oscillator_with_a_crystal_prefix_drives():
    board = [("Y1", {"1": ("EN", "3V3"), "2": GND, "3": ("OUT", "CLK_A", "Output"),
                     "4": ("VDD", "3V3", "Power")}, {"description": "Oscillator XO 25 MHz"}),
             chip("U2", {"VDD": "1V8"}, MCLK=("CLK_A", "Input"))]
    rows = _findings(_run(board), "receiver_overvoltage")
    assert rows and rows[0]["driver"]["designator"] == "Y1"


@pytest.mark.parametrize("name, judged", [
    ("GTX_CLK", True), ("PERST", True), ("SFP_LOS", True), ("TX0_P", False),
    ("PCIE_TXP0", False), ("MIPI_CLK", False), ("USB_DP", False), ("GXB_REFCLK_CHTp", False),
])
def test_single_ended_high_speed_names_are_judged(name, judged):
    board = [chip("U1", {"VDD": "3V3"}, OUT=("LINE", "Output")),
             chip("U2", {"VDD": "1V8"}, description="Gigabit Ethernet PHY",
                  **{name: ("LINE", "Input")})]
    result = _run(board)
    assert bool(_findings(result, "receiver_overvoltage")) is judged
    assert ("differential" in result["summary"]["signals"]["excluded"]) is not judged


@pytest.mark.parametrize("description, bus_pins", [
    ("CAN bus transceiver", ("CANH", "CANL")), ("RS-485 transceiver", ("A", "B")),
    ("Dual line driver and receiver", ("T1OUT", "R1IN")),
])
def test_transceiver_bus_pins_are_left_out(description, bus_pins):
    board = [chip(f"U{n}", {"VCC": rail}, description=description,
                  **{pin: f"BUS_{index}" for index, pin in enumerate(bus_pins)})
             for n, rail in ((1, "3V3"), (2, "5V"))]
    result = _run(board)
    assert not [row for row in result["findings"] if row["check"] != "unknown_rail"]
    assert result["summary"]["signals"]["excluded"]["differential"] == 2


def test_analog_output_is_left_out():
    board = [mcu(**{"PA0/ADC_IN0": "NetU2_3"}),
             chip("U2", {"VCC": "5V"}, description="Hall effect current sensor",
                  VOUT=("NetU2_3", "Output"))]
    result = _run(board)
    assert not _findings(result, "receiver_overvoltage")
    assert result["summary"]["signals"]["excluded"] == {"analog": 1}


def test_typed_open_collector_i2c_pins_receive():
    board = [chip("U1", {"VDD": "3V3"}, description=MCU, SCL=("AUX_SCL", "OpenCollector"),
                  SDA=("AUX_SDA", "OpenCollector")),
             chip("U2", {"VDD": "1V8"}, SCL=("AUX_SCL", "OpenCollector"),
                  SDA=("AUX_SDA", "OpenCollector")),
             res("R1", "AUX_SCL", "3V3", "4k7"), res("R2", "AUX_SDA", "3V3", "4k7")]
    rows = _findings(_run(board), "open_drain_pullup_mismatch")
    assert len(rows) == 1 and rows[0]["severity"] == "warning" and rows[0]["count"] == 2
    assert rows[0]["receiver"]["designator"] == "U2"


def test_open_drain_output_pulled_above_its_rail_is_info():
    board = [mcu(PA1=("SENSOR_INT_N", "Input")),
             chip("U2", {"VDD": "1V8"}, INT=("SENSOR_INT_N", "OpenCollector")),
             res("R1", "SENSOR_INT_N", "3V3")]
    rows = _findings(_run(board), "open_drain_pullup_mismatch")
    assert len(rows) == 1 and rows[0]["severity"] == "info"
    assert rows[0]["receiver"]["pin_role"] == "open-drain output"
    assert "open-drain output" in rows[0]["softened"]
    # An optocoupler's or a comparator's output transistor is rated apart from its supply.
    for description, pin in (("Optocoupler, phototransistor output", "C"),
                             ("Dual comparator", "OUT")):
        board[1] = chip("U2", {"VCC": "1V8"}, description=description,
                        **{pin: ("SENSOR_INT_N", "OpenCollector")})
        assert not _findings(_run(board), "open_drain_pullup_mismatch")


def test_every_unknown_rail_of_a_pair_is_listed_at_once():
    board = [chip("U1", {"VDD": "RAIL_X"}, SDA=("TWI_DAT", "IO")),
             chip("U2", {"VDD": "RAIL_Y"}, SDA=("TWI_DAT", "IO")),
             chip("U3", {"VDD": "RAIL_Z"}, OUT=("OTHER", "Output")),
             res("R1", "TWI_DAT", "RAIL_Z", "4k7")]
    rails = [row["rail"] for row in _run(board)["summary"]["unknown_rails"]]
    assert rails == ["RAIL_X", "RAIL_Y", "RAIL_Z"]


def test_rails_joined_through_a_link_are_asked_once():
    board = [chip("U1", {"VDD": "VIO_A"}, OUT=("SIG", "Output")),
             ("R9", {"1": ("1", "VIO_A"), "2": ("2", "NetR9_2")},
              {"value": "0R", "description": "Resistor"}),
             chip("U2", {"VDD": "NetR9_2"}, IN=("SIG", "Input"))]
    rails = _run(board)["summary"]["unknown_rails"]
    assert rails == [{"rail": "VIO_A", "signals": 1, "example_nets": ["SIG"],
                      "joined_rails": ["NetR9_2"]}]
    assert not _findings(_run(board, rails={"VIO_A": 3.3}), "unknown_rail")


def test_supply_pin_on_a_signal_named_net_is_flagged_not_asked():
    board = [mcu(PA1="PA1", PA2=("SIG", "Output")),
             chip("U2", {"VCC": "PA1"}, IN=("SIG", "Input"))]
    result = _run(board)
    row = _findings(result, "unknown_rail")[0]
    assert row["rails"] == [] and result["summary"]["unknown_rails"] == []
    assert row["supply_pins_on_signal_nets"] == [{"net": "PA1", "parts": ["U2"],
                                                  "signals": 1}]
    assert "pin mapping" in row["issue"]


def test_count_is_signals_not_nets():
    board = [chip("U1", {"VDD": "3V3"}, OUT=("SIG_A", "Output")),
             chip("U2", {"VDD": "1V8"}, IN=("SIG_B", "Input")),
             res("R1", "SIG_A", "SIG_B", "33")]
    row = _findings(_run(board), "receiver_overvoltage")[0]
    assert row["nets"] == ["SIG_A", "SIG_B"] and "count" not in row


def test_series_crossing_is_counted_apart_and_shown_on_its_nets():
    board = [chip("U1", {"VDD": "5V"}, OUT=("LINE_A", "Output")),
             res("R1", "LINE_A", "LINE_B", "10k"),
             chip("U2", {"VDD": "3V3"}, IN=("LINE_B", "Input"))]
    result = _run(board, nets=["LINE_B"])
    signals = result["summary"]["signals"]
    assert (signals["checked"], signals["judged"]) == (0, 0)
    assert (signals["series_resistor_links"], signals["series_resistor_links_judged"]) == (1, 1)
    row = result["signals"][0]
    assert row["series_crossings"] == [{"through": "R1", "value": "10k", "to_nets": ["LINE_A"],
                                        "judged": True}]
    assert "judged across its series resistor" in row["excluded"]


def test_termination_is_not_a_different_level():
    board = [chip("U1", {"VDDQ": "1V2"}, description="DDR4 SDRAM", A0=("ADDR0", "Input")),
             chip("U2", {"VDDQ": "1V2"}, description="DDR4 SDRAM", A0=("ADDR0", "Input")),
             res("R1", "ADDR0", "VTT_0V6", "39")]
    signals = _run(board, rails={"VTT_0V6": 0.6})["summary"]["signals"]
    assert (signals["judged"], signals["between_different_levels"]) == (1, 0)


def test_translators_with_unknown_sides_are_not_counted_as_handled():
    board = [("U4", {"1": ("P1", "SIDE1"), "2": ("P2", "SIDE2"), "3": ("P3", "GND")},
              {"description": "Digital isolator"}),
             chip("U1", {"VDD": "3V3"}, OUT=("SIDE1", "Output")),
             chip("U2", {"VDD": "5V"}, IN=("SIDE2", "Input"))]
    result = _run(board)
    summary = result["summary"]
    assert (summary["translators_handled"], summary["translators_sides_unknown"]) == (0, 1)
    assert "not judged" in result["translators"][0]["issue"]


def test_part_pins_page_with_the_findings():
    pins = {f"D{n}": (f"BUS{n}", "IO") for n in range(80)}
    board = [chip("U1", {"VDD": "3V3"}, **pins), chip("U2", {"VDD": "3V3"}, **pins)]
    first = _run(board, parts=["U1"], limit=50)
    assert first["part_pins_total"] == 80 and len(first["part_pins"]) == 50
    assert first["has_more"] and first["next_offset"] == 50
    rest = _run(board, parts=["U1"], offset=50, limit=50)
    assert len(rest["part_pins"]) == 30 and not rest["has_more"]
    seen = {row["pin"] for row in first["part_pins"] + rest["part_pins"]}
    assert len(seen) == 80


# ---------- tool ----------

@pytest.fixture
def board(monkeypatch, tmp_path: Path):
    netlist = _netlist(SPI)
    monkeypatch.setattr(main_mod, "_project", {
        "name": "Demo", "root_dir": str(tmp_path), "prj_pcb_path": str(tmp_path / "D.PrjPcb"),
        "sheets": [], "pcb_doc_paths": [],
    })
    monkeypatch.setattr(main_mod._altium, "_netlist", netlist)
    monkeypatch.setattr(main_mod, "_variant_state",
                        VariantState([VariantDefinition(name="Default")]))
    return netlist


def test_check_logic_levels_tool(board):
    result = json.loads(main_mod.check_logic_levels())
    assert result["variant"] == "Default" and "netlist_updated_utc" in result
    assert result["summary"]["signals"]["checked"] == 4 and "page_limited_to" not in result
    detail = json.loads(main_mod.check_logic_levels(part="U2", rails="1V8=1.8"))
    assert detail["parts"][0]["designator"] == "U2"
    nets = json.loads(main_mod.check_logic_levels(net="SPI_MISO", vref="0.6"))
    assert nets["signals"][0]["nets"] == ["SPI_MISO"]


def test_check_logic_levels_tool_errors(board, monkeypatch):
    assert json.loads(main_mod.check_logic_levels(vref="nope"))["error"] == "invalid_vref"
    assert json.loads(main_mod.check_logic_levels(rails="VIO"))["error"] == "invalid_rails"
    assert json.loads(main_mod.check_logic_levels(checks=["x"]))["error"] == "invalid_checks"
    assert json.loads(main_mod.check_logic_levels(part="Q99"))["error"] == "part_not_found"
    assert json.loads(main_mod.check_logic_levels(net="NOPE"))["error"] == "net_not_found"
    monkeypatch.setattr(main_mod._altium, "_netlist", {**board, "components": {}})
    assert json.loads(main_mod.check_logic_levels())["error"] == "no_components_loaded"
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.check_logic_levels())["error"] == "no_project"


def test_check_logic_levels_shrinks_to_fit(board, monkeypatch):
    monkeypatch.setattr(main_mod._altium, "_netlist", _netlist(_many()))
    full = main_mod.check_logic_levels()
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", len(full) // 3)
    service = main_mod.prepare_logic_levels.__globals__
    builds = []
    build = service["build_tree"]
    monkeypatch.setitem(service, "build_tree", lambda *a, **k: builds.append(1) or build(*a, **k))
    result = json.loads(main_mod.check_logic_levels())
    assert builds == [1]  # read once, rendered per shrink step
    assert result["page_limited_to"] < 100 and result["has_more"]
    assert len(json.dumps(result, indent=2)) <= len(full) // 3
    rest = json.loads(main_mod.check_logic_levels(offset=result["next_offset"]))
    assert rest["findings"][0]["receiver"] != result["findings"][0]["receiver"]


def test_tool_registration_manifest_readme_and_instructions():
    tools = {tool.name: tool for tool in asyncio.run(main_mod.mcp.list_tools())}
    tool = tools["check_logic_levels"]
    assert tool.annotations.readOnlyHint is True and tool.title == "Check Logic Levels"
    assert tool.meta["anthropic/maxResultSizeChars"] == main_mod.QUERY_NET_MAX_RESULT_SIZE_CHARS
    assert set(tool.inputSchema["properties"]) == {"part", "net", "checks", "rails", "vref",
                                                   "offset"}
    assert "datasheet" in tool.description and "next_offset" in tool.description
    assert "check_logic_levels" in main_mod.NETLIST_TOOLS
    assert main_mod.SERVER_INSTRUCTIONS.index("check_logic_levels") > 4096
    assert "check_logic_levels" in main_mod.SCHEMATIC_REVIEW_PROMPT
    root = Path(__file__).parents[1]
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    assert "check_logic_levels" in {entry["name"] for entry in manifest["tools"]}
    assert "| `check_logic_levels` |" in (root / "README.md").read_text("utf-8")
    assert "`check_logic_levels`" in (root / "CHANGELOG.md").read_text("utf-8")
