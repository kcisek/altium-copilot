"""Firmware pin map and firmware-facing pinout review (services/firmware_pins,
get_firmware_pin_map, the firmware_pins table of package_for_xfn)."""

import asyncio
import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest

import server.main as main_mod
import server.services.firmware_pins as fp
from export import _create_schema, export_project
from server.altium import _file_entry
from server.parsers.prj_pcb import VariantDefinition, VariantState
from server.services.bom import VariantPlan
from server.services.pcb_index import PcbIndex
from server.services.testpoints import marked_testpoint_nets
from tests.test_decoupling import _pcb

# (refdes, {pin: (pin name, net or None, electrical type)}, info)
MCU = ("U1", {
    "1": ("VDD", "3V3", "Power"), "2": ("VSS", "GND", "Power"),
    "3": ("NRST", "NRST", "Input"), "4": ("BOOT0", "BOOT0", "Input"),
    "5": ("PA9/USART1_TX", "UART1_TX", "IO"), "6": ("PA10/USART1_RX", "UART1_RX", "IO"),
    "7": ("PA2/USART2_TX", "NetU1_7", "IO"), "8": ("PA3/USART2_RX", "CAN_RX", "IO"),
    "9": ("PA13", "SWDIO", "IO"), "10": ("PA14", "SWCLK", "IO"),
    "11": ("PB0", "NetU1_11", "IO"), "12": ("PB1", "USER_INPUT", "IO"),
    "13": ("PB3", None, "IO"), "14": ("PB4", "SPARE", "IO"), "15": ("NC", None, "Passive"),
    "16": ("PC14-OSC32_IN", "NetU1_16", "IO"),
    "17": ("PB6", "I2C1_SCL", "IO"), "18": ("PB7", "I2C1_SDA", "IO"),
    "19": ("PA5/SPI1_SCK", "FLASH_SCK", "IO"), "20": ("PA6/SPI1_MISO", "FLASH_MISO", "IO"),
    "21": ("PA7/SPI1_MOSI", "FLASH_MOSI", "IO"), "22": ("PA4", "FLASH_CS", "IO"),
}, {"description": "ARM Cortex-M4 microcontroller"})
PARTS = [
    MCU,
    ("R1", {"1": ("1", "NRST"), "2": ("2", "3V3")}, {"value": "10k"}),
    ("C1", {"1": ("1", "NRST"), "2": ("2", "GND")}, {"value": "100nF"}),
    ("SW1", {"1": ("1", "NRST"), "2": ("2", "GND")}, {"description": "Tactile switch"}),
    ("R2", {"1": ("1", "BOOT0"), "2": ("2", "GND")}, {"value": "10k"}),
    # A USB-UART bridge: its RXD takes the MCU's TX.
    ("U2", {"1": ("RXD", "UART1_TX"), "2": ("TXD", "UART1_RX"), "3": ("VCC", "3V3", "Power"),
            "4": ("GND", "GND", "Power")}, {"description": "USB to UART bridge"}),
    # A CAN transceiver behind a series resistor: its TXD takes the MCU's TX.
    ("R3", {"1": ("1", "NetU1_7"), "2": ("2", "CAN_TXD")}, {"value": "22R"}),
    ("U3", {"1": ("TXD", "CAN_TXD"), "2": ("RXD", "CAN_RX"), "3": ("VCC", "3V3", "Power"),
            "4": ("GND", "GND", "Power"), "5": ("CANH", "CAN_H"), "6": ("CANL", "CAN_L")},
     {"description": "CAN transceiver"}),
    ("J1", {"1": ("1", "3V3"), "2": ("2", "SWDIO"), "3": ("3", "GND"), "4": ("4", "SWCLK")},
     {"description": "Header 2x2, 1.27 mm"}),
    ("R4", {"1": ("1", "NetU1_11"), "2": ("2", "NetD1_A")}, {"value": "330R"}),
    ("D1", {"A": ("A", "NetD1_A"), "K": ("K", "GND")}, {"description": "LED green"}),
    ("SW2", {"1": ("1", "USER_INPUT"), "2": ("2", "GND")}, {"description": "Tactile switch"}),
    ("R5", {"1": ("1", "USER_INPUT"), "2": ("2", "3V3")}, {"value": "10k"}),
    ("Y1", {"1": ("1", "NetU1_16"), "2": ("2", "NetY1_2")}, {"description": "32.768kHz crystal"}),
    ("U4", {"1": ("A0", "GND"), "2": ("A1", "GND"), "3": ("A2", "GND"), "4": ("VSS", "GND", "Power"),
            "5": ("SDA", "I2C1_SDA"), "6": ("SCL", "I2C1_SCL"), "7": ("WP", "GND"),
            "8": ("VCC", "3V3", "Power")}, {"description": "EEPROM 256 Kbit, I2C"}),
    ("R6", {"1": ("1", "I2C1_SCL"), "2": ("2", "3V3")}, {"value": "4k7"}),
    ("R7", {"1": ("1", "I2C1_SDA"), "2": ("2", "3V3")}, {"value": "4k7"}),
    ("U5", {"1": ("/CS", "FLASH_CS"), "2": ("DO", "FLASH_MISO"), "3": ("/WP", "FLASH_WP"),
            "4": ("GND", "GND", "Power"), "5": ("DI", "FLASH_MOSI"), "6": ("CLK", "FLASH_SCK"),
            "7": ("/HOLD", None), "8": ("VCC", "3V3", "Power")},
     {"description": "Serial NOR flash 32 Mbit, SPI"}),
    ("R8", {"1": ("1", "FLASH_WP"), "2": ("2", "3V3")}, {"value": "10k"}),
]


def _netlist(parts=PARTS):
    """A compiled netlist; a pin whose net is None is drawn but not connected."""
    components, pin_to_net, nets, compiled = {}, {}, {}, {}
    for refdes, pins, info in parts:
        entry = {"pins": {}, "params": {}, "kind": 0, "description": None, "value": None,
                 "pin_types": {}, "pin_names": {}, "pin_designators": []}
        entry.update({key: value for key, value in info.items() if key != "comment"})
        if entry.get("value"):
            entry["params"] = {"value": entry["value"]}
        for pin, spec in pins.items():
            name, net = spec[:2]
            entry["pin_designators"].append(pin)
            entry["pin_types"][pin] = spec[2] if len(spec) > 2 else "Passive"
            if name:
                entry["pin_names"][pin] = name
            if net is None:
                continue
            entry["pins"][pin] = {"name": name, "net": net}
            pin_to_net.setdefault(refdes, {})[pin] = net
            nets.setdefault(net, []).append((refdes, pin))
        components[refdes] = entry
        compiled[refdes] = {"footprint": info.get("footprint", ""),
                            "comment": info.get("comment", "")}
    return {"nets": nets, "pin_to_net": pin_to_net, "components": components,
            "parts": compiled}


def _with(*extra, drop=(), base=PARTS):
    """base with the extra parts added; an extra part replaces the one of its name."""
    gone = set(drop) | {part[0] for part in extra}
    return [part for part in base if part[0] not in gone] + list(extra)


def _mcu(**pins):
    """The MCU with pins changed: pin=(name, net, type), or None to remove it."""
    changed = dict(MCU[1])
    for pin, spec in pins.items():
        if spec is None:
            changed.pop(pin, None)
        else:
            changed[pin] = spec
    return ("U1", changed, MCU[2])


def _stm32(**pins):
    """The MCU as an STM32-family part (its vendor names GPIOA, &gpioa, PA13 as SWDIO)."""
    return ("U1", _mcu(**pins)[1], {"description": "STM32F405 ARM Cortex-M4 microcontroller"})


def _run(parts=PARTS, **kwargs):
    kwargs.setdefault("limit", 1000)
    return fp.run_firmware_pin_map(_netlist(parts), **kwargs)


def _row(result, pin):
    return next(row for row in result["rows"] if row["pin"] == pin)


def _findings(result, check=None, severity=None):
    return [f for f in result["findings"] if (check is None or f["check"] == check)
            and (severity is None or f["severity"] == severity)]


# ---------- names ----------

@pytest.mark.parametrize("name, scheme, port, bit", [
    ("PA0", "lettered", "A", 0), ("PB12", "lettered", "B", 12), ("PD2", "lettered", "D", 2),
    ("PA9/USART1_TX", "lettered", "A", 9), ("PD0-OSC_IN", "lettered", "D", 0),
    ("PTA1", "kinetis", "A", 1), ("RB3", "pic", "B", 3),
    ("P0.12", "numbered", "0", 12), ("P1_05", "numbered", "1", 5), ("P0_3", "numbered", "0", 3),
    ("PIO0_7", "numbered", "0", 7), ("P407", "renesas", "4", 7),
    ("GPIO1_IO05", "imx", "1", 5), ("GPIO12", "gpio_number", None, 12),
    ("IO12", "io_number", None, 12), ("PIO[2]", "indexed", None, 2),
    ("DIO_5", "dio_number", None, 5), ("IO_L1P_T0_14", "fpga_io", None, None),
])
def test_port_parsing_per_scheme(name, scheme, port, bit):
    parsed = fp.parse_port(name)
    assert (parsed.scheme, parsed.port, parsed.bit) == (scheme, port, bit)


@pytest.mark.parametrize("name", ["VDD", "NRST", "SDA", "TXD", "CLK", "A0", ""])
def test_non_gpio_names_give_no_port(name):
    assert fp.parse_port(name) is None


@pytest.mark.parametrize("name, expected", [
    ("UART1_TX", ("uart", "tx")), ("PA2_USART2_RX", ("uart", "rx")), ("TXD", ("uart", "tx")),
    ("DEBUG_TX", ("debug_uart", "tx")), ("SPI1_MOSI", ("spi", "mosi")),
    ("FLASH_CS", ("spi", "cs")), ("SPI_IN", ("spi", "miso")), ("QSPI_IO2", ("qspi", "io2")),
    ("I2C1_SDA", ("i2c", "sda")), ("SCL", ("i2c", "scl")), ("SWDIO", ("swd", "swdio")),
    ("JTAG_TCK", ("jtag", "tck")), ("UPDI", ("programming", "updi")),
    ("USB_DP", ("usb", "dp")), ("USB_D_N", ("usb", "dm")), ("CAN1_TX", ("can", "tx")),
    ("RMII_TXD0", ("ethernet", "tx")), ("MDIO", ("ethernet", "mdio")),
    ("SD_CMD", ("sdio", "cmd")), ("OSC_IN", ("crystal", "in")), ("NRST", ("reset", None)),
    ("BOOT0", ("boot_strap", "boot")), ("MOTOR_EN", ("enable", "enable")),
    ("ADC_IN3", ("analog", None)), ("TIM1_CH2", ("pwm_timer", None)),
    ("LED_RED", ("led", None)), ("BTN_USER", ("button", None)), ("SENSOR_INT", ("interrupt", None)),
    ("DDR_DQ3", ("memory_bus", None)), ("PCIE_TX0_P", ("high_speed", None)),
    ("TX0_P", ("high_speed", None)), ("AUDIO_OUT", None), ("NetR9_2", None),
])
def test_function_from_a_name(name, expected):
    assert fp.classify(name) == expected


def test_pin_names_and_negation():
    assert fp.classify("NC", pin=True) == ("nc", None)
    assert fp.alternatives("PA13 (JTMS-SWDIO)") == ["PA13", "JTMS", "SWDIO"]
    assert fp.auto_net("NetU1_23") and not fp.auto_net("UART1_TX")
    assert [fp.negated(name) for name in ("nRESET", "RESET_N", "RST#", "~CS", "R\\S\\T\\")] == \
        [True] * 5
    assert not any(fp.negated(name) for name in ("LED", "", "BTN", "nfc_irq"))
    assert [fp.identifier(name) for name in ("USB_D+", "RST#", "3V3_EN", "~CS")] == \
        ["USB_DP", "RST_N", "P_3V3_EN", "NCS"]


# ---------- candidates ----------

def test_candidates_rank_the_processor_and_skip_memory():
    module = ("M1", {str(n): (f"P0.{n:02d}", f"NetM1_{n}", "IO") for n in range(1, 7)},
              {"description": "Bluetooth LE module"})
    result = _run(_with(module))
    designators = [c["designator"] for c in result["candidates"]]
    assert designators == ["U1", "M1"] and result["mapped"] == ["U1"]
    top = result["candidates"][0]
    assert top["kind"] == "mcu" and top["port_scheme"] == "lettered"
    assert top["gpio_named_pins"] == 17 and top["score"] > result["candidates"][1]["score"]
    assert result["candidates"][1]["kind"] == "module"
    # A flash with IO0-IO3 pins is no processor.
    flash = ("U9", {f"{n}": (f"IO{n}", f"Q{n}") for n in range(4)}, {"description": "QSPI flash"})
    assert "U9" not in [c["designator"] for c in _run(_with(flash))["candidates"]]


def test_mcu_names_the_part_to_map():
    module = ("M1", {str(n): (f"P0.{n:02d}", f"NetM1_{n}", "IO") for n in range(1, 7)},
              {"description": "Bluetooth LE module"})
    result = _run(_with(module), mcu=["m1"])
    assert result["mapped"] == ["M1"]
    assert _row(result, "1")["port"] == "0" and _row(result, "1")["bit"] == 1
    forced = _run(mcu=["U4"])
    assert forced["mapped"] == ["U4"]
    assert any("not recognised as a processor" in note for note in forced["notes"])
    assert _run(mcu=["U99"])["error"] == "mcu_not_found"


def test_no_processor_says_so():
    result = _run([part for part in PARTS if part[0] in ("R1", "C1")])
    assert result["mapped"] == [] and result["candidates"] == []
    assert "No processor found" in result["notes"][-1]


def test_fpga_counts_and_says_so():
    fpga = ("U1", {"1": ("IO_L1P_T0_14", "LED0", "IO"), "2": ("IO_L1N_T0_14", "LED1", "IO"),
                   **{str(n): ("IO", f"NetU1_{n}", "IO") for n in range(3, 8)},
                   "8": ("TCK", "JTAG_TCK", "Input"),
                   "9": ("IO, LVDS1_2p", "NetU1_9", "IO")}, {"description": "Spartan FPGA"})
    result = _run([fpga, ("U2", {"1": ("DATA_0", "NetU1_9")}, {"description": "FIFO"})])
    assert result["candidates"][0]["kind"] == "fpga"
    assert any("FPGA" in note for note in result["notes"])
    assert _row(result, "8")["function"] == "jtag"
    # An FPGA pin's alternate names say what it can do, not what it does.
    assert _row(result, "9")["function"] == "unknown"


# ---------- pin rows ----------

def test_rows_ports_functions_and_basis():
    result = _run()
    tx = _row(result, "5")
    assert (tx["port"], tx["bit"], tx["function"], tx["role"]) == ("A", 9, "uart", "tx")
    assert tx["basis"] == ["net name UART1_TX"]
    assert tx["peers"] == [{"designator": "U2", "pin": "1", "name": "RXD", "kind": "ic"}]
    assert _row(result, "3")["function"] == "reset"  # a dedicated pin name
    assert _row(result, "3")["pulls"] == [{"designator": "R1", "direction": "up", "to": "3V3",
                                           "value": "10k"}]
    led = _row(result, "11")
    assert led["function"] == "led" and led["basis"] == ["reaches LED D1.A via R4"]
    assert _row(result, "12")["function"] == "button"  # the switch it reaches
    assert _row(result, "16")["function"] == "crystal"
    can = _row(result, "7")  # an auto-named net: the transceiver's TXD behind R3
    assert (can["function"], can["role"]) == ("uart", "tx")
    assert can["peers"][0]["through"] == ["R3"]
    assert _row(result, "15")["function"] == "nc"
    assert _row(result, "13") == {"pin": "13", "name": "PB3", "port": "B", "bit": 3,
                                  "type": "IO", "net": None, "function": "unconnected",
                                  "basis": ["not connected"], "unused": True}
    # Power and ground pins are summarised, not listed.
    assert result["power_pins"]["U1"] == {"power": {"3V3": ["1"]}, "ground": {"GND": ["2"]}}
    assert all(row["pin"] not in ("1", "2") for row in result["rows"])
    overview = result["overview"][0]
    assert overview["port_parsed"] == overview["gpio_named_pins"] == 17
    assert result["heuristic"].startswith("A heuristic")


def test_function_from_alternates_and_ambiguity():
    single = _run(_with(_mcu(**{"14": ("PB4/TIM3_CH1", "NetU1_14", "IO")}),
                        ("TP1", {"1": ("1", "NetU1_14")}, {})))
    assert _row(single, "14")["function"] == "pwm_timer"
    assert _row(single, "14")["basis"][0].startswith("pin alternate function")
    several = _run(_with(_mcu(**{"14": ("PB4/TIM3_CH1/SPI1_MISO", "NetU1_14", "IO")}),
                         ("TP1", {"1": ("1", "NetU1_14")}, {})))
    row = _row(several, "14")
    assert row["function"] == "gpio" and row["alternates"] == ["pwm_timer", "spi"]


def test_the_pins_own_role_wins_over_a_net_named_from_the_other_end():
    result = _run(_with(_mcu(**{"5": ("PA9/USART1_TX", "UART1_RX", "IO")}), drop=("U2",)))
    row = _row(result, "5")
    assert (row["function"], row["role"]) == ("uart", "tx")
    (mismatch,) = _findings(result, "uart_crossing")
    assert mismatch["severity"] == "info" and "named from the other end" in mismatch["issue"]


def test_tied_gpio_and_pin_names_from_another_sheet():
    tied = _with(_mcu(**{"14": ("PB4", "GND", "IO")}))
    row = _row(_run(tied), "14")
    assert (row["function"], row["role"]) == ("tied", "ground") and "peers" not in row
    netlist = _netlist()
    # A part placed on two sheets: the latest entry lacks the other part's names.
    netlist["components"]["U1"]["pins"]["5"]["name"] = ""
    assert _row(fp.run_firmware_pin_map(netlist, limit=1000), "5")["name"] == "PA9/USART1_TX"


# ---------- uart_crossing ----------

def test_uart_bridge_tx_meeting_tx_is_a_warning():
    swapped = _with(("U2", {"1": ("TXD", "UART1_TX"), "2": ("RXD", "UART1_RX"),
                            "3": ("VCC", "3V3", "Power"), "4": ("GND", "GND", "Power")},
                     {"description": "USB to UART bridge"}), drop=("U2",))
    result = _run(swapped, checks=["uart_crossing"])
    warnings = _findings(result, "uart_crossing", "warning")
    assert [(f["pin"], f["peer"], f["peer_class"]) for f in warnings] == [
        ("5", "U2.1", "bridge"), ("6", "U2.2", "bridge")]
    assert "TX meets TX" in warnings[0]["issue"] and "RX meets RX" in warnings[1]["issue"]


def test_uart_transceiver_txd_takes_the_mcus_tx():
    assert _findings(_run(checks=["uart_crossing"])) == []
    wrong = _with(("U3", {"1": ("RXD", "CAN_TXD"), "2": ("TXD", "CAN_RX"),
                          "3": ("VCC", "3V3", "Power"), "4": ("GND", "GND", "Power")},
                   {"description": "CAN transceiver"}), drop=("U3",))
    finding, can_rx = _findings(_run(wrong, checks=["uart_crossing"]), "uart_crossing")
    assert finding["severity"] == "warning" and finding["peer_class"] == "transceiver"
    assert finding["pin"] == "7" and "R3" in finding["designators"]
    # The line named CAN_RX is a CAN line: its RX meets the transceiver's TXD input too.
    assert (can_rx["pin"], can_rx["severity"]) == ("8", "warning")
    assert "RX meets RX" in can_rx["issue"]
    # A CAN transceiver's TXD gives a pin with no alternate names the CAN controller's TX.
    plain = _run(_with(_mcu(**{"7": ("PA2", "NetU1_7", "IO")})))
    assert (_row(plain, "7")["function"], _row(plain, "7")["role"]) == ("can", "tx")


def test_uart_connector_pin_named_tx_is_ambiguous():
    header = ("J2", {"1": ("TX", "UART1_TX"), "2": ("RX", "UART1_RX"), "3": ("GND", "GND")},
              {"description": "Pin header 1x3"})
    result = _run(_with(header, drop=("U2",)), checks=["uart_crossing"])
    findings = _findings(result, "uart_crossing")
    assert [(f["severity"], f["peer_class"]) for f in findings] == [("info", "connector")] * 2
    assert "naming convention" in findings[0]["issue"]


def test_uart_typed_pins_decide():
    typed = _with(_mcu(**{"5": ("PA9", "UART1_TX", "Output")}),
                  ("U2", {"1": ("DOUT", "UART1_TX", "Output"), "2": ("DIN", "UART1_RX", "Input")},
                   {"description": "Level translator"}), drop=("U2",))
    findings = _findings(_run(typed, checks=["uart_crossing"]), "uart_crossing")
    # An Output meets an Output (TX meets TX); an RX meets the translator's Input.
    assert [(f["pin"], f["severity"], f["certain"]) for f in findings] == [
        ("5", "warning", True), ("6", "warning", True)]
    assert "typed Output" in findings[0]["issue"] and "typed Input" in findings[1]["issue"]


# ---------- debug_access ----------

def test_debug_access_complete_through_a_header():
    result = _run(checks=["debug_access"])
    (swd,) = result["debug_access"]["U1"][:1]
    assert swd["interface"] == "swd" and swd["complete"] is True
    assert [pin["access"] for pin in swd["pins"][:2]] == [["J1.2"], ["J1.4"]]
    assert _findings(result) == []


def test_no_debug_path_is_a_warning_and_testpoint_pads_count():
    parts = _with(drop=("J1",))
    result = _run(parts, checks=["debug_access"])
    (finding,) = _findings(result)
    assert finding["severity"] == "warning" and "no debug or programming path" in \
        finding["issue"]
    # A testpoint pad on the PcbDoc gives the access back.
    loaded = []

    def loader():
        loaded.append(True)
        return {"SWDIO", "SWCLK"}, None

    fixed = _run(parts, checks=["debug_access"], testpoint_loader=loader)
    assert loaded and _findings(fixed) == []
    assert fixed["debug_access"]["U1"][0]["pins"][0]["access"] == ["testpoint pad on SWDIO"]
    # Not loaded when the netlist already shows a header.
    loaded.clear()
    _run(checks=["debug_access"], testpoint_loader=loader)
    assert not loaded


def test_partial_debug_set_and_usb_only():
    partial = _with(("J1", {"1": ("1", "3V3"), "2": ("2", "SWDIO"), "3": ("3", "GND")},
                     {"description": "Header 1x3"}), drop=("J1",))
    (finding,) = _findings(_run(partial, checks=["debug_access"]))
    assert finding["severity"] == "info" and "1 of 2 required pins" in finding["issue"]
    usb = _with(_mcu(**{"14": ("PA12", "USB_DP", "IO")}),
                ("J3", {"1": ("D+", "USB_DP")}, {"description": "USB receptacle"}),
                drop=("J1",))
    (finding,) = _findings(_run(usb, checks=["debug_access"]))
    assert finding["severity"] == "info" and "USB bootloader" in finding["issue"]


def test_vendor_default_swd_pins_and_a_gpio_that_debugs_another_part():
    stm32 = ("U1", {"1": ("VDD", "3V3", "Power"), "2": ("PA13", "NetU1_2", "IO"),
                    "3": ("PA14", "NetU1_3", "IO"), "4": ("PB0", "MOD_SWDIO", "IO"),
                    "5": ("PB1", "PB1_NET", "IO"), "6": ("PB2", "PB2_NET", "IO")},
             {"description": "STM32F103C8T6"})
    header = ("J1", {"1": ("1", "NetU1_2"), "2": ("2", "NetU1_3")}, {"description": "Header"})
    module = ("U2", {"1": ("SWDIO", "MOD_SWDIO")}, {"description": "Radio module"})
    result = _run([stm32, header, module], checks=["debug_access"])
    swd = result["debug_access"]["U1"][0]
    assert swd["complete"] and [pin["pin"] for pin in swd["pins"]] == ["2", "3"]
    assert swd["pins"][0]["basis"].startswith("vendor default: PA13")


def test_an_io_expander_is_not_checked_for_debug_access():
    expander = [("U1", {str(n): (f"P{'AB'[n % 2]}{n}", f"NET{n}", "IO") for n in range(1, 9)},
                 {"description": "16-bit I/O expander, I2C"})]
    result = _run(expander, checks=["debug_access"])
    assert result["candidates"][0]["kind"] == "io_expander"
    assert result["findings"] == [] and result["debug_access"] == {}
    assert any("I/O expander" in note for note in result["notes"])


def test_debug_access_names_nothing():
    plain = [("U1", {str(n): (f"PA{n}", f"NET{n}", "IO") for n in range(1, 6)},
              {"description": "Microcontroller"})]
    (finding,) = _findings(_run(plain, checks=["debug_access"]))
    assert finding["severity"] == "info" and "no SWD, JTAG" in finding["issue"]


# ---------- boot_straps ----------

def _strap(result, pin):
    return next(entry for entry in result["boot_straps"]["U1"] if entry["pin"] == pin)


def test_boot_strap_pulled_down_and_reset_pulled_up():
    result = _run(checks=["boot_straps"])
    boot = _strap(result, "4")
    assert (boot["kind"], boot["level"]) == ("boot", "low")
    assert boot["level_basis"] == "pull: down 10k to GND (R2)"
    reset = _strap(result, "3")
    assert (reset["kind"], reset["level"]) == ("reset", "high")
    assert reset["set_by"] == ["SW1.1"]
    assert _findings(result) == []


def test_boot_strap_divider_with_its_voltage():
    divider = _with(("R9", {"1": ("1", "BOOT0"), "2": ("2", "3V3")}, {"value": "1k"}))
    boot = _strap(_run(divider, checks=["boot_straps"]), "4")
    assert (boot["level"], boot["ratio"], boot["volts"]) == ("high", 0.909, 3.0)
    middle = _with(("R9", {"1": ("1", "BOOT0"), "2": ("2", "3V3")}, {"value": "10k"}))
    result = _run(middle, checks=["boot_straps"])
    assert _strap(result, "4")["level"] == "mid_rail"
    (warning,) = _findings(result, "boot_straps", "warning")
    assert "50%" in warning["issue"]


def test_boot_strap_ties():
    zero = _with(("R2", {"1": ("1", "BOOT0"), "2": ("2", "3V3")}, {"value": "0R"}), drop=("R2",))
    boot = _strap(_run(zero, checks=["boot_straps"]), "4")
    assert boot["level"] == "high" and boot["level_basis"] == "tied through: up 0R to 3V3 (R2)"
    direct = _with(_mcu(**{"4": ("BOOT0", "GND", "Input")}), drop=("R2",))
    boot = _strap(_run(direct, checks=["boot_straps"]), "4")
    assert (boot["level"], boot["level_basis"]) == ("low", "tied straight to GND")


def test_boot_strap_with_nothing_is_the_internal_default():
    result = _run(_with(drop=("R2",)), checks=["boot_straps"])
    assert _strap(result, "4")["level"] == "internal_default"
    (finding,) = _findings(result, "boot_straps")
    assert finding["severity"] == "info" and "datasheet" in finding["issue"]
    unconnected = _run(_with(_mcu(**{"4": ("BOOT0", None, "Input")}), drop=("R2",)),
                       checks=["boot_straps"])
    assert _strap(unconnected, "4")["level"] == "not_connected"
    not_fitted = _run(checks=["boot_straps"], plan=VariantPlan("Lite", not_fitted={"R2"}))
    entry = _strap(not_fitted, "4")
    assert entry["level"] == "internal_default" and entry["not_fitted"][0]["designator"] == "R2"


def test_esp32_strapping_pins_and_serial_bootloader():
    esp = ("U1", {"1": ("EN", "CHIP_EN", "Input"), "2": ("IO0", "BOOT_SW", "IO"),
                  "3": ("IO2", "NetU1_3", "IO"), "4": ("IO12", "NetU1_4", "IO"),
                  "5": ("TXD0", "U0_TX", "IO"), "6": ("RXD0", "U0_RX", "IO"),
                  **{str(n): (f"IO{n}", f"NET{n}", "IO") for n in range(13, 19)}},
           {"description": "ESP32 Wi-Fi module"})
    parts = [esp,
             ("R1", {"1": ("1", "CHIP_EN"), "2": ("2", "3V3")}, {"value": "10k"}),
             ("R2", {"1": ("1", "NetU1_4"), "2": ("2", "GND")}, {"value": "10k"}),
             ("SW1", {"1": ("1", "BOOT_SW"), "2": ("2", "GND")}, {"description": "Switch"}),
             ("J1", {"1": ("1", "U0_TX"), "2": ("2", "U0_RX"), "3": ("3", "BOOT_SW"),
                     "4": ("4", "CHIP_EN")}, {"description": "Programming header 1x4"})]
    result = _run(parts, checks=["boot_straps", "debug_access"])
    straps = {entry["pin"]: entry for entry in result["boot_straps"]["U1"]}
    assert (straps["1"]["kind"], straps["1"]["level"]) == ("reset", "high")
    assert (straps["2"]["kind"], straps["2"]["level"]) == ("strap", "set_externally")
    assert straps["3"]["level"] == "internal_default"
    assert (straps["4"]["level"], straps["4"]["basis"]) == (
        "low", "a strapping pin of this family (IO12)")
    (loader,) = result["debug_access"]["U1"]
    assert loader["interface"] == "uart_bootloader" and loader["complete"]
    assert [pin["role"] for pin in loader["pins"]] == ["boot", "uart_tx", "uart_rx", "reset"]


# ---------- external_memory ----------

def test_external_memory_buses_and_control_pins():
    result = _run(checks=["external_memory"])
    by_ref = {entry["designator"]: entry for entry in result["external_memory"]}
    eeprom, flash = by_ref["U4"], by_ref["U5"]
    assert eeprom["bus"] == "i2c" and eeprom["control_pins"]["A0 (pin 1)"] == "tied low (GND)"
    assert flash["bus"] == "spi" and flash["chip_select"] == ["U1.22"]
    assert {link["role"] for link in flash["links"]} == {"cs", "data", "clock"}
    assert flash["control_pins"] == {"/WP (pin 3)": "up 10k to 3V3 (R8)",
                                     "/HOLD (pin 7)": "not connected"}
    (hold,) = _findings(result, "external_memory", "warning")
    assert "/HOLD (pin 7)" in hold["issue"] and hold["memory"] == "U5"
    infos = _findings(result, "external_memory", "info")
    assert all("reaches no connector" in f["issue"] for f in infos) and len(infos) == 2


def test_external_memory_reachable_from_a_header():
    header = ("J5", {"1": ("1", "FLASH_SCK"), "2": ("2", "FLASH_MOSI"), "3": ("3", "FLASH_MISO"),
                     "4": ("4", "FLASH_CS")}, {"description": "Programming header 1x4"})
    result = _run(_with(header), checks=["external_memory"])
    flash = next(e for e in result["external_memory"] if e["designator"] == "U5")
    assert flash["external_access"][:2] == ["J5.4", "J5.3"]
    text = next(f["issue"] for f in _findings(result) if f["memory"] == "U5" and
                f["severity"] == "info")
    assert "read and written on a prototype from outside" in text


# ---------- unused, conflicts, variants ----------

def test_unused_pins():
    (finding,) = _findings(_run(checks=["unused_pins"]))
    assert finding["pins"] == ["13", "14"]  # not connected, alone on SPARE; NC left out
    assert finding["severity"] == "info"


def test_conflicting_assignment():
    shared = _with(_mcu(**{"14": ("PB4", "UART1_TX", "IO")}))
    (finding,) = _findings(_run(shared, checks=["conflicting_assignment"]))
    assert finding["severity"] == "info" and finding["pins"] == ["5", "14"]
    driven = _with(_mcu(**{"5": ("PA9", "UART1_TX", "Output"), "14": ("PB4", "UART1_TX", "Output")}))
    (finding,) = _findings(_run(driven, checks=["conflicting_assignment"]))
    assert finding["severity"] == "warning"
    mux = _with(_mcu(**{"14": ("PB4/TIM3_CH1/USART3_RX", "SPI2_SCK", "IO")}),
                ("TP1", {"1": ("1", "SPI2_SCK")}, {}))
    (finding,) = _findings(_run(mux, checks=["conflicting_assignment"]))
    assert "pin-mux" in finding["issue"] and finding["pin"] == "14"


def test_variant_changes():
    plans = [VariantPlan("Full"), VariantPlan("Lite", not_fitted={"R3", "U3"})]
    result = _run(checks=["variant_changes"], variant_plans=plans)
    by_pin = {f["pin"]: f for f in _findings(result)}
    assert set(by_pin) == {"7", "8"}
    assert by_pin["7"]["parts"] == {
        "R3": {"fitted_in": ["Full"], "not_fitted_in": ["Lite"]},
        "U3": {"fitted_in": ["Full"], "not_fitted_in": ["Lite"]}}
    values = [VariantPlan("A"), VariantPlan("B", params={"R2": {"value": ("Value", "1k")}})]
    (finding,) = _findings(_run(checks=["variant_changes"], variant_plans=values))
    assert finding["pin"] == "4" and finding["values"] == {"R2": {"A": None, "B": "1k"}}
    single = _run(checks=["variant_changes"], variant_plans=[VariantPlan("Only")])
    assert single["not_checked"] == {"variant_changes": "the project defines fewer than two "
                                                        "variants"}
    assert single["checks_run"] == []


# ---------- formats ----------

def test_csv_lists_every_pin():
    text = _run(format="csv")["csv"]
    lines = text.splitlines()
    assert lines[0] == ",".join(fp.CSV_COLUMNS)
    assert len(lines) == 1 + len(MCU[1])
    assert "U1,5,PA9/USART1_TX,A,9,IO,UART1_TX,uart,tx,,U2.1 (RXD),net name UART1_TX" in lines
    assert "U1,1,VDD,,,Power,3V3,power,,,,pin name" in lines


def test_c_header():
    pio = ("U2", {"1": ("PIO[2]", "STATUS", "IO"), **{str(n): (f"PIO[{n}]", f"N{n}", "IO")
                                                       for n in range(3, 7)}},
           {"description": "Bluetooth radio"})
    header = _run(_with(pio, _stm32()), format="c_header", mcu=["U1", "U2"])["c_header"]
    assert "#define U1_UART1_TX_PORT GPIOA\n#define U1_UART1_TX_PIN 9  /* pin 5 PA9/USART1_TX, " \
           "uart tx, no pull, net UART1_TX */" in header
    assert "#define U1_NRST" not in header and "/* U1_NRST: port scheme not recognised" in header
    assert "#define U2_STATUS_PIN 2  /* GPIO number, no port;" in header
    assert "#define U1_PB0_PORT GPIOB" in header  # an auto-named net uses the pin name
    assert "heuristic starting point" in header
    # GPIOA is an STM32 name: another vendor's lettered port is written as its letter.
    generic = _run(format="c_header")["c_header"]
    assert "#define UART1_TX_PORT 'A'\n#define UART1_TX_PIN 9" in generic


def test_devicetree():
    nreset = _with(_stm32(**{"14": ("PB4", "nWIFI_RESET", "IO")}),
                   ("U6", {"1": ("RESET", "nWIFI_RESET")}, {"description": "Wi-Fi module"}))
    dts = _run(nreset, format="devicetree")["devicetree"]
    assert "zephyr,user {" in dts and "STARTING POINT" in dts
    assert "\t\tnwifi-reset-gpios = <&gpiob 4 GPIO_ACTIVE_LOW>;" in dts
    assert "\t\tpb0-gpios = <&gpiob 0 GPIO_ACTIVE_HIGH>;" in dts  # the LED
    # A property name has at most 31 characters: a long net name gives way to the pin's.
    long_name = _with(_stm32(**{"14": ("PB4", "MAIN_BOARD_STATUS_INDICATOR_LED", "IO")}))
    assert "\t\tpb4-gpios = <&gpiob 4 GPIO_ACTIVE_HIGH>;" in \
        _run(long_name, format="devicetree")["devicetree"]
    assert "/* uart: pin 5 PA9/USART1_TX, net UART1_TX, uart tx (configure in pinctrl) */" in dts
    numbered = [("U1", {str(n): (f"P0.{n:02d}", f"LED{n}", "IO") for n in range(4)},
                 {"description": "nRF52 SoC"})]
    assert "<&gpio0 1 GPIO_ACTIVE_HIGH>" in _run(numbered, format="devicetree")["devicetree"]
    pio = [("U1", {str(n): (f"PIO[{n}]", f"LED{n}", "IO") for n in range(4)},
            {"description": "Bluetooth radio"})]
    assert "no controller label known for this part, not emitted" in \
        _run(pio, format="devicetree")["devicetree"]
    # Lettered ports of a vendor without a known label (SAM's &porta) are not guessed.
    assert "<&gpio" not in _run(format="devicetree")["devicetree"]


def test_invalid_format_and_checks():
    assert _run(format="xml")["error"] == "invalid_format"
    assert _run(format="dts")["devicetree"].startswith("/*")
    assert _run(checks=["nope"])["error"] == "invalid_checks"


# ---------- paging ----------

def test_rows_and_findings_page_together():
    built = fp.build_firmware_map(_netlist())
    full = built.page(0, 1000)
    first = built.page(0, 5)
    assert first["rows_total"] == full["rows_total"] == 20
    assert len(first["rows"]) == 5 and first["next_offset"] == 5 and first["has_more"]
    assert first["findings"] == full["findings"][:5]
    last = built.page(15, 5)
    assert len(last["rows"]) == 5 and "next_offset" not in last
    assert first["rows"][0]["pin"] == "3"
    csv_page = fp.build_firmware_map(_netlist(), format="csv").page(20, 5)
    assert csv_page["csv"].count("\n") == 3  # the header and the last two pins


# ---------- export ----------

def test_export_rows_and_the_firmware_pins_table(tmp_path):
    netlist = _netlist()
    rows = fp.export_rows(netlist)
    assert len(rows) == len(MCU[1]) and {row["mcu"] for row in rows} == {"U1"}
    tx = next(row for row in rows if row["pin"] == "5")
    assert tx == {"mcu": "U1", "pin": "5", "name": "PA9/USART1_TX", "port": "A", "bit": 9,
                  "net": "UART1_TX", "function": "uart", "role": "tx", "pull": None,
                  "peers": [{"designator": "U2", "pin": "1", "name": "RXD", "kind": "ic"}]}
    project = {"name": "Board", "root_dir": str(tmp_path), "sheets": []}
    path = export_project(project, netlist, VariantState([]), "0.0.1", firmware_pins=rows)
    conn = sqlite3.connect(path)
    stored = conn.execute(
        "SELECT mcu_refdes, pin_number, pin_name, port, bit, net_name, function, role, pull,"
        " peers FROM firmware_pins WHERE pin_number = '3'").fetchone()
    count = conn.execute("SELECT COUNT(*) FROM firmware_pins").fetchone()[0]
    conn.close()
    assert count == len(rows)
    assert stored[:9] == ("U1", "3", "NRST", None, None, "NRST", "reset", None,
                          "up 10k to 3V3 (R1)")
    assert json.loads(stored[9])[0]["designator"] == "SW1"


def test_base_schema_is_unchanged_and_the_table_is_new(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "a.db"))
    _create_schema(conn)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "firmware_pins" not in tables
    path = export_project({"name": "B", "root_dir": str(tmp_path), "sheets": []},
                          {"nets": {}, "components": {}}, VariantState([]), "0.0.1")
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM firmware_pins").fetchone() == (0,)
    conn.close()


def test_package_for_xfn_writes_the_pin_map(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "mark_xfn_exported", lambda name: None)
    project = {"name": "Board", "root_dir": str(tmp_path), "prj_pcb_path": str(tmp_path / "B.PrjPcb"),
               "sheets": []}
    out = main_mod._package_for_xfn_impl(project, _netlist(), VariantState([]), "0.0.1")
    assert f"Firmware pin map: {len(MCU[1])} pins of U1 in the firmware_pins table" in out
    conn = sqlite3.connect(str(tmp_path / "Board-pcb-copilot.db"))
    assert conn.execute("SELECT COUNT(*) FROM firmware_pins").fetchone() == (len(MCU[1]),)
    conn.close()

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(main_mod, "firmware_export_rows", broken)
    out = main_mod._package_for_xfn_impl(project, _netlist(), VariantState([]), "0.0.1")
    assert "Exported to:" in out
    assert "Warning: the firmware pin map could not be built (RuntimeError: boom)" in out


# ---------- review fixes ----------

@pytest.mark.parametrize("name, expected", [
    ("RTS1", ("uart", "rts")), ("U1RTS", ("uart", "rts")), ("CTS2", ("uart", "cts")),
    ("RTS1_DP", None),  # a UART's RTS or USB's D+: the name cannot tell
    ("LED_RX", ("led", None)), ("TX_EN", None), ("ETH_TX_EN", ("ethernet", "tx")),
    ("CRS_DV", ("ethernet", "rx")), ("XTAL1", ("crystal", "in")), ("XTAL2", ("crystal", "out")),
    ("OSC1", ("crystal", "in")), ("DDR4_DIMM_SDA", ("i2c", "sda")), ("HDMI_SCL", ("i2c", "scl")),
    ("MSEL0", ("boot_strap", "mode")), ("CONFIG[5]", ("boot_strap", "strap")),
    ("LEDC_CH0", ("pwm_timer", None)),
])
def test_function_names_fixed_on_review(name, expected):
    assert fp.classify(name, pin=True) == expected


def test_active_low_suffixes():
    assert all(fp.negated(name) for name in ("RESET_B", "CS_L", "SPI_CS_B", "BTN_N"))
    assert not any(fp.negated(name) for name in ("LED_B", "RGB_B", "MOTOR_L", "LED_G"))


def _two_mcus(scl, sda):
    def mcu(ref):
        return (ref, {"1": ("PB6/USART1_TX/I2C1_SCL", scl, "IO"),
                      "2": ("PB7/USART1_RX/I2C1_SDA", sda, "IO"), "3": ("PA0", None, "IO"),
                      "4": ("PA1", None, "IO"), "5": ("VDD", "3V3", "Power")},
                {"description": "Microcontroller"})
    return [mcu("U1"), mcu("U2"), ("R1", {"1": ("1", scl), "2": ("2", "3V3")}, {"value": "4k7"}),
            ("R2", {"1": ("1", sda), "2": ("2", "3V3")}, {"value": "4k7"})]


def test_a_peer_pin_listing_several_functions_names_none():
    result = _run(_two_mcus("NetU1_1", "NetU1_2"))
    assert _findings(result, "uart_crossing") == []
    row = _row(result, "1")
    assert (row["function"], row["role"]) == ("i2c", "scl")
    assert row["basis"] == ["pin alternate function USART1_TX, I2C1_SCL, and a pull-up on the "
                            "line"]


def test_an_ethernet_phy_is_read_as_ethernet():
    mac = ("U1", {"1": ("PB12/ETH_RMII_TXD0", "NetU1_1", "IO"), "2": ("PC4", "NetU1_2", "IO"),
                  "3": ("PA7", "NetU1_3", "IO"), "4": ("PA1/ETH_RMII_REF_CLK", "NetU1_4", "IO"),
                  "5": ("PB0", None, "IO")}, {"description": "Microcontroller"})
    phy = ("U2", {"1": ("TXD0", "NetU1_1"), "2": ("RXD0/MODE0", "NetU1_2"),
                  "3": ("CRS_DV/MODE2", "NetU1_3"), "4": ("nINT/REFCLKO", "NetU1_4")},
           {"description": "Ethernet PHY, RMII"})
    result = _run([mac, phy])
    assert [(_row(result, pin)["function"], _row(result, pin)["role"]) for pin in "1234"] == [
        ("ethernet", "tx"), ("ethernet", "rx"), ("ethernet", "rx"), ("ethernet", "ref_clk")]
    assert _findings(result, "uart_crossing") == []
    # A peer that is no PHY: the pin's own single alternate wins over its TXD0 read as a UART.
    plain = _run([mac, (phy[0], phy[1], {"description": "Transceiver"})])
    row = _row(plain, "1")
    assert (row["function"], row["role"]) == ("ethernet", "tx")
    assert row["basis"][0].startswith("pin alternate function ETH_RMII_TXD0; reaches U2.1")


def test_jtag_prefers_dedicated_pins_and_names_a_chain():
    fpga = ("U1", {"1": ("IO_L1P_0", "SOFT_TCK", "IO"), "2": ("IO_L1N_0", "SOFT_TDI", "IO"),
                   "3": ("IO_L2P_0", "SOFT_TDO", "IO"), "4": ("IO_L2N_0", "SOFT_TMS", "IO"),
                   "10": ("TCK", "HARD_TCK", "Input"), "11": ("TMS", "HARD_TMS", "Input"),
                   "12": ("TDI", "HARD_TDI", "Input"), "13": ("TDO", "CHAIN", "Output"),
                   **{str(n): (f"IO_L{n}P_0", f"X{n}", "IO") for n in range(20, 25)}},
            {"description": "Spartan FPGA"})
    header = ("J1", {"1": ("1", "HARD_TCK"), "2": ("2", "HARD_TMS"), "3": ("3", "HARD_TDI"),
                     "5": ("5", "SOFT_TCK"), "6": ("6", "SOFT_TDI"), "7": ("7", "SOFT_TDO"),
                     "8": ("8", "SOFT_TMS")}, {"description": "Header 2x5"})
    prom = ("U2", {"1": ("TDI", "CHAIN"), "2": ("TDO", "PROM_TDO")}, {"description": "PROM"})
    result = _run([fpga, header, prom], checks=["debug_access"])
    (jtag,) = result["debug_access"]["U1"]
    assert [pin["pin"] for pin in jtag["pins"]] == ["10", "11", "12", "13"]
    assert jtag["pins"][3]["jtag_chain"] == ["U2.1 (TDI)"] and not jtag["complete"]
    (finding,) = _findings(result)
    assert finding["severity"] == "info" and "in a JTAG chain through U2.1 (TDI)" in \
        finding["issue"]


def test_fpga_configuration_memory():
    fpga = ("U1", {"1": ("IO_L1P_0", "LED0", "IO"), **{str(n): ("IO", f"X{n}", "IO")
                                                       for n in range(2, 7)},
                   "10": ("DCLK", "DCLK", "IO"), "11": ("nCSO", "NetU1_11", "Output"),
                   **{str(12 + n): (f"AS_DATA{n}", f"NetU1_{12 + n}", "IO") for n in range(4)}},
            {"description": "Cyclone FPGA"})
    config = ("U2", {"1": ("DCLK", "DCLK"), "2": ("nCS", "NetU1_11"),
                     **{str(3 + n): (f"DATA{n}", f"NetU1_{12 + n}") for n in range(4)}},
              {"description": "Serial configuration device"})
    prom = ("U2", {"1": ("D0", "X2"), "2": ("CLK", "X3"), "3": ("CE", "X4")},
            {"description": "Platform Flash configuration PROM"})
    (entry,) = _run([fpga, config], checks=["external_memory"])["external_memory"]
    assert (entry["bus"], entry["use"], entry["chip_select"]) == (
        "qspi", "FPGA configuration", ["U1.11"])
    (entry,) = _run([fpga, prom], checks=["external_memory"])["external_memory"]
    assert entry["bus"] == "serial_configuration"


def test_stm32_boot_pins():
    boot_high = _with(_stm32(**{"13": ("PB2", None, "IO")}),
                      ("R2", {"1": ("1", "BOOT0"), "2": ("2", "3V3")}, {"value": "10k"}),
                      drop=("R2",))
    result = _run(boot_high, checks=["boot_straps"])
    (warning,) = _findings(result, "boot_straps", "warning")
    assert warning["pin"] == "4" and "system-memory bootloader" in warning["issue"]
    (boot1,) = _findings(result, "boot_straps", "info")
    assert boot1["pin"] == "13" and "BOOT1" in boot1["issue"]
    assert _strap(result, "13")["basis"] == "BOOT1 on this family (PB2)"
    # BOOT0 pulled low: BOOT1 does not matter and gets no finding; a switch makes BOOT0 high
    # a choice.
    low = _run(_with(_stm32(**{"13": ("PB2", None, "IO")})), checks=["boot_straps"])
    assert _findings(low) == []
    switch = ("SW3", {"1": ("1", "BOOT0"), "2": ("2", "GND")}, {"description": "Switch"})
    assert _findings(_run(_with(switch, base=boot_high), checks=["boot_straps"]),
                      severity="warning") == []


@pytest.mark.parametrize("family, held_low, warned", [
    ("ESP32-WROOM-32E module", "IO0", True), ("ESP32-C3-MINI module", "IO0", False),
    ("ESP32-C3-MINI module", "IO9", True),
])
def test_esp32_boot_pin_held_in_download_mode(family, held_low, warned):
    esp = ("U1", {str(n): (f"IO{n}", f"NET{n}", "IO") for n in range(0, 10)},
           {"description": family})
    pull = ("R1", {"1": ("1", f"NET{held_low[2:]}"), "2": ("2", "GND")}, {"value": "10k"})
    result = _run([esp, pull], checks=["boot_straps"])
    warnings = _findings(result, "boot_straps", "warning")
    assert bool(warnings) is warned
    if warned:
        assert "serial download mode" in warnings[0]["issue"]


def test_a_strap_driven_by_another_processors_gpio():
    host = ("U9", {"1": ("PA1", "MOD_BOOT", "IO"), **{str(n): (f"PB{n}", None, "IO")
                                                       for n in range(2, 6)}},
            {"description": "Microcontroller"})
    module = ("M1", {"1": ("BOOT0", "MOD_BOOT", "Input"),
                     **{str(n): (f"GPIO{n}", None, "IO") for n in range(2, 7)}},
              {"description": "Wi-Fi module"})
    result = _run([host, module], mcu=["M1"], checks=["boot_straps"])
    entry = result["boot_straps"]["M1"][0]
    assert (entry["level"], entry["gpio_driven_by"]) == ("driven", ["U9.1 (PA1)"])
    (finding,) = _findings(result)
    assert finding["severity"] == "info" and "floats while that processor is in reset" in \
        finding["issue"]


def test_a_targets_debug_port_driven_by_a_host_gpio():
    host = ("U9", {"1": ("PA0", "MOD_SWDIO", "IO"), "2": ("PA1", "MOD_SWCLK", "IO"),
                   **{str(n): (f"PB{n}", None, "IO") for n in range(3, 7)}},
            {"description": "Microcontroller"})
    module = ("M1", {"1": ("NC", "MOD_SWDIO"), "2": ("NC", "MOD_SWCLK"),
                     **{str(n): (f"GPIO{n}", None, "IO") for n in range(3, 8)}},
              {"description": "Radio module"})
    result = _run([host, module], mcu=["M1"], checks=["debug_access"])
    (swd,) = result["debug_access"]["M1"]
    assert swd["in_system"] and swd["pins"][0]["via_processor"] == ["U9.1 (PA0)"]
    (finding,) = _findings(result)
    assert finding["severity"] == "info" and "programmed in-system" in finding["issue"]
    # From the host's side the lines are GPIOs driving the module, not its own debug port.
    host_map = _run([host, module], mcu=["U9"], format="devicetree")
    assert "mod-swdio-gpios" in host_map["devicetree"]
    assert host_map["debug_access"]["U9"] == []


def test_debug_access_with_a_boot_switch_and_with_usb_only():
    esp = ("U1", {"1": ("EN", "CHIP_EN", "Input"), "2": ("IO0", "BOOT_SW", "IO"),
                  "5": ("TXD0", "U0_TX", "IO"), "6": ("RXD0", "U0_RX", "IO"),
                  **{str(n): (f"IO{n}", f"NET{n}", "IO") for n in range(13, 19)}},
           {"description": "ESP32 Wi-Fi module"})
    parts = [esp, ("SW1", {"1": ("1", "BOOT_SW"), "2": ("2", "GND")}, {"description": "Switch"}),
             ("R1", {"1": ("1", "BOOT_SW"), "2": ("2", "3V3")}, {"value": "10k"}),
             ("J1", {"1": ("1", "U0_TX"), "2": ("2", "U0_RX")}, {"description": "Header 1x2"})]
    (loader,) = _run(parts, checks=["debug_access"])["debug_access"]["U1"]
    assert loader["complete"] and loader["pins"][0]["access"] == ["set by SW1.1"]
    # SWD unconnected and only USB on a connector: still no programming path.
    usb = _with(_mcu(**{"9": ("PA13", None, "IO"), "10": ("PA14", None, "IO"),
                        "14": ("PA12", "USB_DP", "IO")}),
                ("J3", {"1": ("D+", "USB_DP")}, {"description": "USB receptacle"}), drop=("J1",))
    (finding,) = _findings(_run(usb, checks=["debug_access"]))  # no debug pin names
    assert finding["severity"] == "info" and "no SWD, JTAG" in finding["issue"]
    named = _with(_mcu(**{"9": ("SWDIO", None, "IO"), "10": ("SWCLK", None, "IO"),
                          "14": ("PA12", "USB_DP", "IO")}),
                  ("J3", {"1": ("D+", "USB_DP")}, {"description": "USB receptacle"}), drop=("J1",))
    (finding,) = _findings(_run(named, checks=["debug_access"]))
    assert finding["severity"] == "warning" and "are not connected; only USB" in finding["issue"]


def test_unused_pins_leave_the_debug_port_alone():
    result = _run(_with(_stm32(**{"9": ("PA13", None, "IO"), "10": ("PA14", None, "IO")}),
                        drop=("J1",)), checks=["unused_pins"])
    (finding,) = _findings(result)
    assert finding["pins"] == ["14"] and finding["debug_pins"] == ["9", "10", "13"]
    assert "leave them in their debug function" in finding["issue"]


def test_a_forced_non_processor_gets_no_debug_warning_or_strap_conflicts():
    phy = ("U7", {"1": ("TCK", None, "Input"), "2": ("TMS", None, "Input"),
                  "3": ("TDI", None, "Input"), "4": ("TDO", None, "Output"),
                  "5": ("LED_LINK", "LED_LINK", "Output"), "6": ("CONFIG[1]", "LED_LINK", "Input"),
                  "7": ("TXD0", "TIE", "Input"), "8": ("TXD1", "TIE", "Input")},
           {"description": "Gigabit Ethernet transceiver"})
    tie = ("R9", {"1": ("1", "TIE"), "2": ("2", "GND")}, {"value": "0R"})
    result = _run([phy, tie], mcu=["U7"], checks=["debug_access", "conflicting_assignment",
                                                    "boot_straps"])
    assert [(f["check"], f["severity"]) for f in result["findings"]] == [("debug_access", "info")]
    assert _row(result, "7")["function"] == "ethernet"
    (strap,) = result["boot_straps"]["U7"]
    assert (strap["pin"], strap["level"], strap["shared_with"]) == ("6", "shared", ["5"])


def test_c_header_identifiers_are_unique_across_pages():
    spare = _stm32(**{pin: (f"PB{int(pin) - 10}", "SPARE", "IO") for pin in ("11", "12", "13")})
    built = fp.build_firmware_map(_netlist(_with(spare)), format="c_header")
    total = built.page(0, 1000)["rows_total"]
    defined = [line.split()[1] for offset in range(0, total, 3)
               for line in built.page(offset, 3)["c_header"].splitlines()
               if line.startswith("#define")]
    assert len(defined) == len(set(defined)) and "SPARE_3_PIN" in defined


def test_duplicate_designators_and_a_processor_without_pins_are_noted():
    netlist = _netlist()
    netlist["compile_warnings"] = [{"kind": "duplicate_designators", "count": 1,
                                    "examples": ["U4"], "message": "..."}]
    netlist["components"]["U8"] = {"pins": {}, "description": "Microcontroller",
                                   "pin_types": {}, "pin_names": {}}
    notes = fp.run_firmware_pin_map(netlist, limit=1000)["notes"]
    assert any(note.startswith("U8 reads as a processor but has no connected pin") for note in notes)
    assert any(note.startswith("U4 is among the compile's duplicate designators") for note in notes)


# ---------- the tool ----------

@pytest.fixture
def board(monkeypatch, tmp_path: Path):
    netlist = _netlist()
    pcb_file = tmp_path / "Board.PcbDoc"
    pcb_file.write_bytes(b"fake")
    monkeypatch.setattr(main_mod, "_project", {
        "name": "Demo", "root_dir": str(tmp_path), "prj_pcb_path": str(tmp_path / "D.PrjPcb"),
        "sheets": [], "pcb_doc_paths": [str(pcb_file)],
    })
    monkeypatch.setattr(main_mod._altium, "_netlist", netlist)
    monkeypatch.setattr(main_mod, "_variant_state", VariantState([
        VariantDefinition(name="Full"), VariantDefinition(name="Lite", dnp_refdes=["R4"])]))
    monkeypatch.setattr(main_mod._pcb_session, "_load", lambda path, netlist: object())
    monkeypatch.setattr(main_mod, "marked_testpoint_nets", lambda index: {"SWDIO", "SWCLK"})
    main_mod._pcb_session._index = None
    return netlist


def test_get_firmware_pin_map_tool(board):
    result = json.loads(main_mod.get_firmware_pin_map())
    assert result["mapped"] == ["U1"] and result["variant"] == "Full"
    assert "netlist_updated_utc" in result and "page_limited_to" not in result
    (change,) = [f for f in result["findings"] if f["check"] == "variant_changes"]
    assert change["pin"] == "11" and "R4" in change["parts"]
    header = json.loads(main_mod.get_firmware_pin_map(mcu="U1", format="c_header"))
    assert "#define UART1_TX_PIN 9" in header["c_header"]
    assert json.loads(main_mod.get_firmware_pin_map(mcu="U99"))["error"] == "mcu_not_found"
    assert json.loads(main_mod.get_firmware_pin_map(checks=["x"]))["error"] == "invalid_checks"
    assert json.loads(main_mod.get_firmware_pin_map(format="xml"))["error"] == "invalid_format"


def test_tool_reads_testpoint_pads_when_no_header(board, monkeypatch):
    netlist = _netlist(_with(drop=("J1",)))
    monkeypatch.setattr(main_mod._altium, "_netlist", netlist)
    result = json.loads(main_mod.get_firmware_pin_map(checks=["debug_access"]))
    assert result["debug_access"]["U1"][0]["complete"] and result["findings"] == []
    monkeypatch.setitem(main_mod._project, "pcb_doc_paths", [])
    result = json.loads(main_mod.get_firmware_pin_map(checks=["debug_access"]))
    assert result["findings"][0]["severity"] == "warning"
    assert "Testpoint pads not read: the project lists no PcbDoc." in result["notes"]


def test_tool_shrinks_its_page_to_fit(board, monkeypatch):
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 12500)
    result = json.loads(main_mod.get_firmware_pin_map())
    assert result["page_limited_to"] < 100 and result["next_offset"] == result["page_limited_to"]
    assert len(json.dumps(result, indent=2)) <= 12500
    rest = json.loads(main_mod.get_firmware_pin_map(offset=result["next_offset"]))
    assert rest["rows"][0]["pin"] != result["rows"][0]["pin"]


def test_tool_registration_manifest_readme_and_instructions():
    tools = {tool.name: tool for tool in asyncio.run(main_mod.mcp.list_tools())}
    tool = tools["get_firmware_pin_map"]
    assert tool.annotations.readOnlyHint is True and tool.title == "Get Firmware Pin Map"
    assert tool.meta["anthropic/maxResultSizeChars"] == main_mod.QUERY_NET_MAX_RESULT_SIZE_CHARS
    assert set(tool.inputSchema["properties"]) == {"mcu", "format", "checks", "offset"}
    assert "heuristic" in tool.description and "next_offset" in tool.description
    assert "get_firmware_pin_map" in main_mod.NETLIST_TOOLS
    assert main_mod.SERVER_INSTRUCTIONS.index("get_firmware_pin_map") > 4096
    root = Path(__file__).parents[1]
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    assert "get_firmware_pin_map" in {entry["name"] for entry in manifest["tools"]}
    assert "| `get_firmware_pin_map` |" in (root / "README.md").read_text("utf-8")


def test_tool_errors(monkeypatch):
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.get_firmware_pin_map())["error"] == "no_project"
    monkeypatch.setattr(main_mod, "_project", {"name": "Demo", "sheets": []})
    monkeypatch.setattr(main_mod, "_variant_state", VariantState([]))
    monkeypatch.setattr(main_mod._altium, "_netlist", {"nets": {"A": []}, "components": {}})
    assert json.loads(main_mod.get_firmware_pin_map())["error"] == "no_components_loaded"


# ---------- helpers made public, pin names ----------

def test_marked_testpoint_nets():
    pcb = _pcb([("U1", "top", 0.0, 0.0, {"1": ("SWDIO", 0.0, 0.0), "2": ("SWCLK", 50.0, 0.0)}),
                ("TP1", "top", 500.0, 0.0, {"1": ("NRST", 0.0, 0.0)})])
    pcb.primitives[0] = dataclasses.replace(pcb.primitives[0], test_fab_top=True)
    index = PcbIndex(pcb, None)
    assert marked_testpoint_nets(index) == {"SWDIO", "NRST"}


def test_pin_names_are_united_across_sheets():
    result: dict = {}
    _file_entry(result, "U1", {"pin_names": {"1": "PA0"}, "pin_types": {}, "compile_masked": False})
    _file_entry(result, "U1", {"pin_names": {"2": "PA1"}, "pin_types": {}, "compile_masked": False})
    assert result["U1"]["pin_names"] == {"1": "PA0", "2": "PA1"}
