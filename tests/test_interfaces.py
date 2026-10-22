"""Connector and interface protection audit (services/interfaces, check_interfaces)."""

import json
from pathlib import Path

import pytest

import server.main as main_mod
import server.services.interfaces as itf
from server.parsers.prj_pcb import VariantDefinition, VariantState
from server.services.bom import VariantPlan
from server.services.pcb_index import PcbIndex
from tests.test_decoupling import _pcb
from tests.test_pull_resistors import _netlist

# (refdes, {pin: (pin name, net) or (pin name, net, electrical type)}, info)
MCU = ("U1", {"1": ("VDD", "3V3", "Power"), "2": ("GND", "GND", "Power"),
              "3": ("PA0", "BTN_IN", "IO"), "4": ("PA1", "UART_TX", "IO"),
              "5": ("PA11", "USB_DM", "IO"), "6": ("PA12", "USB_DP", "IO"),
              "7": ("PA2", "HDR_SIG", "IO"), "8": ("PA13", "SWDIO", "IO"),
              "9": ("PA14", "SWCLK", "IO"), "10": ("PA3", "B2B_SIG", "IO")},
       {"description": "Microcontroller"})
PARTS = [
    MCU,
    ("C1", {"1": ("1", "3V3"), "2": ("2", "GND")}, {"value": "100nF"}),
    # A USB receptacle: a TVS array at the connector, 22 ohm series resistors to the MCU.
    ("J1", {"1": ("VBUS", "VBUS"), "2": ("D-", "USB_DM_CONN"), "3": ("D+", "USB_DP_CONN"),
            "5": ("GND", "GND"), "S1": ("SHIELD", "GND")},
     {"description": "USB 2.0 Micro-B receptacle", "pin_designators": ["1", "2", "3", "4", "5",
                                                                        "S1"]}),
    ("D1", {"1": ("IO1", "USB_DP_CONN"), "2": ("GND", "GND"), "3": ("IO2", "USB_DM_CONN"),
            "5": ("VBUS", "VBUS")}, {"description": "ESD protection array", "comment": "USBLC6-2SC6"}),
    ("R1", {"1": ("1", "USB_DM_CONN"), "2": ("2", "USB_DM")}, {"value": "22"}),
    ("R2", {"1": ("1", "USB_DP_CONN"), "2": ("2", "USB_DP")}, {"value": "22"}),
    # A terminal block: pin 1 straight to the MCU with a plain diode, pin 2 through a
    # series resistor with the TVS on the MCU side.
    ("J2", {"1": ("1", "BTN_IN"), "2": ("2", "TB_TX"), "3": ("3", "GND")},
     {"description": "Terminal block, 3 positions, screw"}),
    ("D2", {"1": ("K", "BTN_IN"), "2": ("A", "GND")}, {"description": "Switching diode",
                                                       "comment": "1N4148"}),
    ("R3", {"1": ("1", "TB_TX"), "2": ("2", "UART_TX")}, {"value": "100R"}),
    ("D3", {"1": ("K", "UART_TX"), "2": ("A", "GND")}, {"description": "TVS diode",
                                                        "comment": "SMAJ5.0A"}),
    # A debug header (internal by its nets), a plain pin header (unknown), and a
    # board-to-board connector (internal by its text).
    ("J3", {"1": ("1", "3V3"), "2": ("2", "SWDIO"), "3": ("3", "GND"), "4": ("4", "SWCLK")},
     {"description": "Header 2x2, 1.27 mm"}),
    ("J4", {"1": ("1", "HDR_SIG"), "2": ("2", "GND")}, {"description": "Pin header 1x2"}),
    ("J5", {"1": ("1", "B2B_SIG"), "2": ("2", "GND")},
     {"description": "Board-to-board connector, 2 positions"}),
]


def _run(parts=PARTS, plan=None, **kwargs):
    return itf.run_interface_check(_netlist(parts), plan, **kwargs)


def _with(*extra, drop=()):
    return [part for part in PARTS if part[0] not in drop] + list(extra)


def _connector(result, ref):
    return next(row for row in result["connectors"] if row["designator"] == ref)


def _pin(result, ref, pin):
    return next(row for row in result["pin_tables"][ref] if row["pin"] == pin)


def _findings(result, check=None, connector=None):
    return [f for f in result["findings"] if (check is None or f["check"] == check)
            and (connector is None or f["connector"] == connector)]


# ---------- recognising parts ----------

@pytest.mark.parametrize("prefix, text, expected", [
    ("J", "USB receptacle", True),
    ("P", "", True),
    ("X", "Connector, 4 positions", True),
    ("X", "Crystal 16 MHz", False),
    ("X", "16MHz", False),
    ("J", "Coin cell holder", False),
    ("J", "LAN transformer modular jack RJ45", True),  # integrated magnetics: a connector
    ("T", "LAN transformer", False),
    ("JP", "Jumper", False),
    ("USB", "", True),
    ("U", "USB receptacle", False),
])
def test_connector_recognition(prefix, text, expected):
    assert itf.is_interface_connector(prefix, text) is expected


@pytest.mark.parametrize("prefix, text, expected", [
    ("TVS", "", True),
    ("ESD", "", True),
    ("D", "TVS diode, 5 V", True),
    ("D", "SMAJ5.0A", True),
    ("U", "TPD4E05U06 4-channel array", True),
    ("U", "IP4220CZ6", True),
    ("D", "PESD5V0S1BA", True),
    ("RV", "Varistor 14 V", True),
    ("D", "Switching diode 1N4148", False),
    ("D", "Schottky diode BAT54", False),
    ("U", "Microcontroller", False),
    ("R", "ESD-safe resistor", False),
    ("C", "ESD capacitor", False),
    ("L", "EMI suppression ferrite bead", False),
    # A part's own ESD rating, or another function named first, is not a device.
    ("U", "Microcontroller, 32-bit, ESD sensitive device", False),
    ("U", "RS-485/RS-422 transceiver, half duplex, +/-15 kV ESD protection", False),
    ("U", "CAN FD transceiver with ESD-protected bus pins", False),
    ("U", "Ethernet PHY 10/100, ESD", False),
    ("U", "eFuse with overvoltage and surge protection", False),
    ("Q", "N-channel MOSFET 60 V, ESD protected gate", False),
    ("Q", "ESD", False),
    ("U", "SP3485EN-L/TR RS-485 transceiver", False),
    ("U", "SP3232EEY-L/TR", False),  # MaxLinear and Littelfuse share SP3xxx
    ("U", "SP3012-04UTG 0.5 pF diode array", True),
    ("U", "ESD protection diode array for Ethernet PHY interfaces", True),
    ("U", "TVS array for CAN transceivers", True),
    ("U", "Low capacitance ESD protection", True),
    ("U", "4-channel 15 kV ESD protection array", True),
    ("U", "USB 2.0 integrated ESD protection", True),
    ("D", "ESD", True),
])
def test_esd_recognition(prefix, text, expected):
    assert itf.is_esd(prefix, text) is expected


def test_esd_hints_need_an_ic_whose_pins_drive_nothing():
    # A bare 'ESD' or an SP3xxx with protection words counts on an IC only when none of
    # its pins is an Input or Output; a diode needs no such check.
    assert itf.is_esd("U", "SP3010 protection", ["Input", "Output", "Power"]) is False
    assert itf.is_esd("U", "SP3010 protection", ["IO", "Passive"]) is True
    assert itf.is_esd("U", "ESD", ["IO", "IO", "Passive"]) is True
    assert itf.is_esd("U", "ESD", ["Input", "Output"]) is False
    assert itf.is_esd("D", "ESD", ["Input"]) is True


@pytest.mark.parametrize("text, expected", [
    ("RJ45 modular jack with integrated magnetics", True),
    ("RJ45 modular jack without magnetics, shielded", False),
    ("RJ45 jack, no magnetics", False),
    ("RJ45 w/o transformer", False),
    ("MagJack 10/100", True),
    ("LAN transformer", True),
])
def test_magnetics(text, expected):
    assert itf.has_magnetics(text) is expected


# ---------- pins ----------

def test_pin_classes():
    result = _run()
    table = {row["pin"]: row for row in result["pin_tables"]["J1"]}
    assert table["1"]["class"] == "power"
    assert table["2"]["class"] == table["3"]["class"] == "signal"
    assert "differential" in table["3"]["tags"] and "high_speed" in table["3"]["tags"]
    assert table["4"] == {"pin": "4", "net": None, "class": "unconnected"}
    assert table["5"]["class"] == "ground"
    assert table["S1"]["class"] == "shield" and table["S1"]["class_basis"] == "pin name"
    assert _connector(result, "J1")["pins"] == {"power": 1, "signal": 2, "unconnected": 1,
                                                "ground": 1, "shield": 1}


def test_shell_legs_past_the_position_count_are_shield():
    coax = ("J7", {"1": ("1", "RF_IN"), "2": ("2", "GND"), "3": ("3", "GND"),
                   "4": ("4", "GND"), "5": ("5", "GND")},
            {"description": "SMA connector, 1 position"})
    amp = ("U2", {"1": ("IN", "RF_IN", "Input"), "2": ("GND", "GND", "Power")}, {})
    result = _run(_with(coax, amp))
    assert [row["class"] for row in result["pin_tables"]["J7"]] == [
        "signal", "shield", "shield", "shield", "shield"]
    assert _pin(result, "J7", "2")["class_basis"] == "past the 1 positions in its description"
    # RF coax: no TVS is often the right call, so only info.
    (finding,) = _findings(result, "no_esd", "J7")
    assert finding["severity"] == "info" and "RF lines" in finding["issue"]
    # A '10-pin' dual-row header with 20 pins: the count was read wrong, not 10 shell legs.
    header = ("J8", {str(n): (str(n), "GND" if n == 20 else f"IO{n}") for n in range(1, 21)},
              {"description": "Header, 10-Pin, dual row"})
    result = _run(_with(header))
    assert _pin(result, "J8", "20")["class"] == "ground"


def test_power_pins_by_name_by_connector_text_and_by_feed():
    jack = ("J6", {"1": ("1", "JACK_IN"), "2": ("2", "GND"), "3": ("3", "GND")},
            {"description": "DC power jack, 2.1 mm barrel"})
    fuse = ("F1", {"1": ("1", "JACK_IN"), "2": ("2", "JACK_FUSED")}, {"description": "Fuse 2 A"})
    diode = ("D4", {"1": ("A", "JACK_FUSED"), "2": ("K", "12V")}, {"description": "Schottky rectifier"})
    reg = ("U3", {"1": ("VIN", "12V", "Power"), "2": ("GND", "GND", "Power"),
                  "3": ("VOUT", "3V3", "Power")}, {})
    tvs = ("D5", {"1": ("K", "JACK_IN"), "2": ("A", "GND")}, {"comment": "SMBJ15A"})
    result = _run(_with(jack, fuse, diode, reg, tvs))
    row = _pin(result, "J6", "1")
    assert row["class"] == "power" and row["class_basis"] == "a power connector"
    assert row["feeds"] == {"rail": "12V", "through": ["F1", "D4"]}
    assert row["esd"] == ["D5"]
    assert _connector(result, "J6")["exposure"] == "external"
    # J1's VBUS has D1's VBUS pin too.
    assert result["overview"]["by_exposure"]["external"]["power_pins_with_esd"] == 2
    # The same jack without a power word in its text: its pin feeds a rail through F1, D4.
    plain = ("J6", jack[1], {"description": "Jack, 3 pins"})
    row = _pin(_run(_with(plain, fuse, diode, reg)), "J6", "1")
    assert row["class"] == "power" and row["class_basis"] == "feeds a rail"
    # A clamp pair (a diode to the rail and one to ground) is not a supply feed.
    clamp_hi = ("D6", {"1": ("A", "SENSE_IN"), "2": ("K", "3V3")}, {"description": "diode"})
    clamp_lo = ("D7", {"1": ("A", "GND"), "2": ("K", "SENSE_IN")}, {"description": "diode"})
    sense = ("J9", {"1": ("1", "SENSE_IN"), "2": ("2", "GND")}, {"description": "Connector"})
    resistor = ("R9", {"1": ("1", "SENSE_IN"), "2": ("2", "BTN_IN")}, {"value": "1k"})
    row = _pin(_run(_with(sense, clamp_hi, clamp_lo, resistor)), "J9", "1")
    # D2 sits on BTN_IN, behind R9.
    assert row["class"] == "signal" and row["clamp_diodes"] == ["D6", "D7", "D2"]


# ---------- exposure ----------

def test_exposure_from_text_nets_and_overrides():
    result = _run()
    assert _connector(result, "J1")["exposure"] == "external"
    assert _connector(result, "J1")["exposure_basis"] == ["text: USB"]
    assert _connector(result, "J2")["exposure_basis"] == ["text: terminal block"]
    assert _connector(result, "J3")["exposure"] == "internal"
    assert "debug or programming nets" in _connector(result, "J3")["exposure_basis"][0]
    assert _connector(result, "J4")["exposure"] == "unknown"
    assert "pin header" in _connector(result, "J4")["exposure_basis"][0]
    assert _connector(result, "J5")["exposure"] == "internal"
    # External connectors come first.
    assert [row["exposure"] for row in result["connectors"]] == [
        "external", "external", "unknown", "internal", "internal"]
    swapped = _run(external=["j4"], internal=["J2"])
    assert _connector(swapped, "J4")["exposure"] == "external"
    assert _connector(swapped, "J4")["exposure_basis"] == ["given by the caller"]
    assert _connector(swapped, "J2")["exposure"] == "internal"
    assert not _findings(swapped, connector="J2")


def test_a_transceiver_behind_a_plain_connector_makes_it_external():
    can = ("U4", {"1": ("CANH", "CAN_H", "IO"), "2": ("CANL", "CAN_L", "IO"),
                  "3": ("GND", "GND", "Power")}, {"description": "CAN transceiver"})
    plug = ("J6", {"1": ("1", "CAN_H"), "2": ("2", "CAN_L"), "3": ("3", "GND")},
            {"description": "Connector, 3 positions"})
    result = _run(_with(can, plug))
    row = _connector(result, "J6")
    assert row["exposure"] == "external" and "U4" in row["exposure_basis"][0]
    (finding,) = _findings(result, "no_esd", "J6")
    assert finding["severity"] == "warning" and finding["pins"] == ["1", "2"]
    # CAN lines are a differential field bus: no series-element advice.
    assert not _findings(result, "no_series_element", "J6")


def test_a_processor_listing_its_buses_is_not_a_transceiver():
    mcu = ("U4", {"1": ("PA1", "IO_A", "IO"), "2": ("PA2", "IO_B", "IO"),
                  "3": ("GND", "GND", "Power")},
           {"description": "ARM Cortex-M4 MCU, 512 kB flash, CAN, USB, Ethernet"})
    plug = ("J6", {"1": ("1", "IO_A"), "2": ("2", "IO_B"), "3": ("3", "GND")},
            {"description": "Connector, 3 positions"})
    result = _run(_with(mcu, plug))
    assert _connector(result, "J6")["exposure"] == "unknown"
    assert "field_bus" not in _pin(result, "J6", "1").get("tags", [])
    (finding,) = _findings(result, "no_esd", "J6")
    assert finding["severity"] == "info"


@pytest.mark.parametrize("description, severity", [
    ("RS-485 transceiver, half duplex", "warning"),
    ("RS-485 transceiver, half duplex, +/-15 kV ESD protection", "info"),
    ("CAN FD transceiver with ESD-protected bus pins", "info"),
    ("SP3485EN RS-485 transceiver", "warning"),
])
def test_a_transceiver_is_never_the_esd_device(description, severity):
    # A transceiver advertising ESD stays the IC the lines reach: no_esd still fires,
    # as info quoting its claim when its text claims protected pins.
    xcvr = ("U4", {"1": ("A", "BUS_A", "IO"), "2": ("B", "BUS_B", "IO"),
                   "3": ("RO", "UART_RX", "Output"), "4": ("GND", "GND", "Power")},
            {"description": description})
    block = ("J6", {"1": ("1", "BUS_A"), "2": ("2", "BUS_B"), "3": ("3", "GND")},
             {"description": "Terminal block, 3 positions"})
    result = _run(_with(xcvr, block))
    assert result["overview"]["esd_devices"] == 2  # D1 and D3: U4 is not one
    (finding,) = _findings(result, "no_esd", "J6")
    assert finding["severity"] == severity and finding["pins"] == ["1", "2"]
    assert (finding.get("protection_claimed_by") == ["U4"]) is (severity == "info")


def test_an_esd_sensitive_mcu_still_needs_esd():
    mcu = ("U4", {"1": ("PA1", "RXD", "IO"), "2": ("GND", "GND", "Power")},
           {"description": "Microcontroller, 32-bit, ESD sensitive device"})
    block = ("J6", {"1": ("1", "RXD"), "2": ("2", "GND")},
             {"description": "Terminal block, 2 positions"})
    (finding,) = _findings(_run(_with(mcu, block)), "no_esd", "J6")
    assert finding["severity"] == "warning" and "protection_claimed_by" not in finding


def test_power_supply_connector_and_sfp_ports_are_external():
    supply = ("J6", {"1": ("1", "JACK_IN"), "2": ("2", "GND")},
              {"description": "Low Voltage Power Supply Connector"})
    reg = ("U3", {"1": ("VIN", "JACK_IN", "Power"), "2": ("GND", "GND", "Power")}, {})
    row = _connector(_run(_with(supply, reg)), "J6")
    assert row["exposure"] == "external" and row["exposure_basis"] == ["text: power jack"]
    serdes = ("U4", {"1": ("TXP", "SFP0_TX_P", "Output"), "2": ("TXN", "SFP0_TX_N", "Output"),
                     "3": ("RXP", "SFP0_RX_P", "Input"), "4": ("RXN", "SFP0_RX_N", "Input"),
                     "5": ("GND", "GND", "Power")}, {"description": "FPGA"})
    cage = ("J7", {"1": ("1", "SFP0_TX_P"), "2": ("2", "SFP0_TX_N"), "3": ("3", "SFP0_RX_P"),
                   "4": ("4", "SFP0_RX_N"), "5": ("5", "GND")},
            {"description": "Female transceiver interface, 20 positions"})
    result = _run(_with(serdes, cage))
    row = _connector(result, "J7")
    assert row["exposure"] == "external" and row["module_port"] is True
    assert "SFP/QSFP module port" in row["exposure_basis"][0]
    (finding,) = _findings(result, "no_esd", "J7")
    assert finding["severity"] == "info" and "pluggable module port" in finding["issue"]


def test_configuration_jumpers_are_internal():
    reg = ("U4", {"1": ("VFB", "FB_NODE", "Input"), "2": ("SEL0", "SEL_NODE", "Input"),
                  "3": ("GND", "GND", "Power"), "4": ("VIN", "3V3", "Power")},
           {"description": "Buck regulator"})
    jumper = ("J6", {"1": ("1", "GND"), "2": ("2", "JMP_A")}, {"description": "Header 1x2"})
    divider = ("R30", {"1": ("1", "JMP_A"), "2": ("2", "FB_NODE")}, {"value": "1.78M"})
    select = ("J7", {"1": ("1", "SEL_NODE"), "2": ("2", "GND")}, {"description": "Header 1x2"})
    pull = ("R31", {"1": ("1", "SEL_NODE"), "2": ("2", "3V3")}, {"value": "1k"})
    result = _run(_with(reg, jumper, divider, select, pull))
    for ref, target in (("J6", "U4.VFB"), ("J7", "U4.SEL0")):
        row = _connector(result, ref)
        assert row["exposure"] == "internal"
        assert row["exposure_basis"][0].startswith("a configuration jumper") \
            and target in row["exposure_basis"][0]
        assert not _findings(result, connector=ref)
    # A header whose line reaches a general input is not a jumper.
    assert _connector(_run(), "J4")["exposure"] == "unknown"


def test_magnetics_led_and_centre_tap_pins_are_not_counted_as_lines():
    phy = ("U5", {"1": ("TXP", "MDI0_P", "IO"), "2": ("TXN", "MDI0_N", "IO"),
                  "3": ("LED1/PHYAD0", "LINK_LED", "IO"), "4": ("GND", "GND", "Power")},
           {"description": "Ethernet PHY"})
    cap = ("C20", {"1": ("1", "CT_NODE"), "2": ("2", "GND")}, {"value": "10nF"})
    jack = ("J6", {"1": ("TD+", "MDI0_P"), "2": ("TD-", "MDI0_N"), "3": ("TCT", "CT_NODE"),
                   "4": ("4", "LINK_LED")}, {"description": "RJ45 jack, no magnetics"})
    result = _run(_with(phy, cap, jack))
    assert "centre_tap" in _pin(result, "J6", "3")["tags"]
    assert "led" in _pin(result, "J6", "4")["tags"]
    row = _connector(result, "J6")
    assert row["signal_pins_reaching_a_part"] == 2 and row["pins"]["signal"] == 4
    (finding,) = _findings(result, "no_esd", "J6")
    assert finding["severity"] == "warning" and finding["pins"] == ["1", "2"]
    assert "2 of 2 signal pins that reach a part" in finding["issue"]


def test_integrated_magnetics_make_no_esd_advice():
    phy = ("U5", {"1": ("TXP", "MDI0_P", "IO"), "2": ("TXN", "MDI0_N", "IO"),
                  "3": ("GND", "GND", "Power")}, {"description": "Ethernet PHY"})
    jack = ("J6", {"1": ("TD+", "MDI0_P"), "2": ("TD-", "MDI0_N")},
            {"description": "RJ45 modular jack with integrated magnetics"})
    result = _run(_with(phy, jack))
    assert _connector(result, "J6")["integrated_magnetics"] is True
    (finding,) = _findings(result, "no_esd", "J6")
    assert finding["severity"] == "info" and "magnetics" in finding["issue"]


# ---------- ESD and the series walk ----------

def test_esd_array_at_the_connector_and_the_series_walk():
    result = _run()
    row = _pin(result, "J1", "3")
    assert row["esd"] == [{"designator": "D1", "side": "connector"}]
    assert row["series"] == [{"designator": "R2", "type": "resistor", "value": "22"}]
    assert row["reaches"] == {"designator": "U1", "pin": "6", "kind": "ic", "name": "PA12",
                              "type": "IO", "series_elements": 1}
    assert not _findings(result, connector="J1")
    assert _connector(result, "J1")["esd_protected"] == 2


def test_a_plain_diode_is_not_esd():
    result = _run()
    (finding,) = _findings(result, "no_esd", "J2")
    assert finding["severity"] == "warning" and finding["pins"] == ["1"]
    assert finding["clamps"] == ["D2"] and "not recognised as an ESD/TVS" in finding["issue"]
    assert _pin(result, "J2", "1")["clamp_diodes"] == ["D2"]
    assert "esd" not in _pin(result, "J2", "1")


def test_esd_behind_a_series_element():
    result = _run()
    row = _pin(result, "J2", "2")
    assert row["esd"] == [{"designator": "D3", "side": "behind_series", "net": "UART_TX"}]
    (finding,) = _findings(result, "esd_behind_series", "J2")
    assert finding["severity"] == "info" and finding["pins"] == ["2"]
    assert "R3" in finding["designators"] and "D3" in finding["designators"]


def test_no_series_element_on_a_low_speed_external_line():
    result = _run()
    (finding,) = _findings(result, "no_series_element", "J2")
    assert finding["pins"] == ["1"] and "0 ohm" in finding["issue"]
    # USB lines are high speed and have their 22 ohm anyway; pin 2 goes through R3.
    assert not _findings(result, "no_series_element", "J1")


def test_not_fitted_esd_leaves_the_pins_unprotected():
    result = _run(plan=VariantPlan(name="Lite", not_fitted={"D1"}))
    (finding,) = _findings(result, "no_esd", "J1")
    assert finding["severity"] == "warning" and finding["not_fitted_esd"] == ["D1"]
    assert "D1 would protect it but is Not Fitted in variant Lite" in finding["issue"]
    assert _pin(result, "J1", "3")["esd_not_fitted"] == ["D1"]


def test_hop_limit_and_ferrite_choke_and_capacitor_series_parts():
    chain = [("J6", {"1": ("1", "N0"), "2": ("2", "GND")}, {"description": "Terminal block"})]
    for n in range(4):
        chain.append((f"R{20 + n}", {"1": ("1", f"N{n}"), "2": ("2", f"N{n + 1}")},
                      {"value": "0R"}))
    chain.append(("U6", {"1": ("IN", "N4", "Input"), "2": ("GND", "GND", "Power")}, {}))
    result = _run(_with(*chain))
    assert _pin(result, "J6", "1")["reaches"] == "nothing within 3 series elements"
    shorter = _run(_with(*[part for part in chain if part[0] != "R23"],
                         ("U6", {"1": ("IN", "N3", "Input"), "2": ("GND", "GND", "Power")}, {})))
    row = _pin(shorter, "J6", "1")
    assert row["reaches"]["series_elements"] == 3
    assert [s["type"] for s in row["series"]] == ["0_ohm", "0_ohm", "0_ohm"]
    # A ferrite, then a 100 nF AC-coupling capacitor.
    parts = _with(
        ("J6", {"1": ("1", "A0"), "2": ("2", "GND")}, {"description": "SMA connector"}),
        ("FB1", {"1": ("1", "A0"), "2": ("2", "A1")}, {"description": "Ferrite bead 600R"}),
        ("C9", {"1": ("1", "A1"), "2": ("2", "A2")}, {"value": "100nF"}),
        ("U6", {"1": ("IN", "A2", "Input"), "2": ("GND", "GND", "Power")}, {}),
    )
    row = _pin(_run(parts), "J6", "1")
    assert [(s["designator"], s["type"]) for s in row["series"]] == [
        ("FB1", "ferrite"), ("C9", "capacitor")]
    assert row["reaches"]["designator"] == "U6"


def test_a_common_mode_choke_keeps_each_line_on_its_own_winding():
    usb = ("J6", {"1": ("VBUS", "VBUS"), "2": ("D-", "DM_C"), "3": ("D+", "DP_C"),
                  "4": ("GND", "GND")}, {"description": "USB Type-C receptacle"})
    choke = ("L5", {"1": ("1", "DP_C"), "2": ("2", "DM_C"), "3": ("3", "USB2_DM"),
                    "4": ("4", "USB2_DP")}, {"description": "Common mode choke 90R"})
    phy = ("U7", {"1": ("DP", "USB2_DP", "IO"), "2": ("DM", "USB2_DM", "IO"),
                  "3": ("GND", "GND", "Power")}, {"description": "USB hub"})
    tvs = ("D8", {"1": ("IO1", "USB2_DP"), "2": ("GND", "GND"), "3": ("IO2", "USB2_DM")},
           {"comment": "TPD2E2U06"})
    result = _run(_with(usb, choke, phy, tvs))
    plus, minus = _pin(result, "J6", "3"), _pin(result, "J6", "2")
    assert (plus["reaches"]["designator"], plus["reaches"]["pin"]) == ("U7", "1")
    assert (minus["reaches"]["designator"], minus["reaches"]["pin"]) == ("U7", "2")
    assert plus["series"] == [{"designator": "L5", "type": "common_mode_choke"}]
    assert plus["esd"] == [{"designator": "D8", "side": "behind_series", "net": "USB2_DP"}]
    assert _findings(result, "esd_behind_series", "J6")


# ---------- shields ----------

def _shielded(*extra, shield_net="SHIELD_J6", pins=None):
    jack = ("J6", pins or {"1": ("1", "BTN_IN"), "2": ("2", "GND"),
                           "S1": ("SHELL", shield_net), "S2": ("SHELL", shield_net)},
            {"description": "USB receptacle"})
    return _run(_with(jack, *extra))


def test_shield_direct_rc_and_floating():
    direct = _shielded(shield_net="GND")
    assert _connector(direct, "J6")["shield"] == "direct"
    assert not _findings(direct, "shield_rc") and not _findings(direct, "shield_floating")
    rc = _shielded(("R8", {"1": ("1", "SHIELD_J6"), "2": ("2", "GND")}, {"value": "1M"}),
                   ("C8", {"1": ("1", "SHIELD_J6"), "2": ("2", "GND")}, {"value": "4.7nF"}))
    (finding,) = _findings(rc, "shield_rc", "J6")
    assert finding["severity"] == "info" and finding["shield"]["through"] == ["C8", "R8"]
    assert "rather than through RC or snubber networks" in finding["issue"]
    floating = _shielded(pins={"1": ("1", "BTN_IN"), "2": ("2", "GND")})
    floating_netlist = _netlist(_with(("J6", {"1": ("1", "BTN_IN"), "2": ("2", "GND")},
                                       {"description": "USB receptacle",
                                        "pin_designators": ["1", "2", "S1", "S2"]})))
    floating = itf.run_interface_check(floating_netlist)
    (finding,) = _findings(floating, "shield_floating", "J6")
    assert finding["severity"] == "warning" and finding["shield"]["unconnected_pins"] == ["S1", "S2"]
    # Mounting pins left unconnected are not a shield.
    mounts = itf.run_interface_check(_netlist(_with(
        ("J6", {"1": ("1", "BTN_IN"), "2": ("2", "GND")},
         {"description": "USB receptacle", "pin_designators": ["1", "2", "MP1", "MP2"]}))))
    assert _connector(mounts, "J6")["shield"] == "floating"
    assert not _findings(mounts, "shield_floating")


def test_a_chassis_net_with_more_on_it_is_direct():
    hole = ("MH1", {"1": ("1", "CHASSIS")}, {"description": "Mounting hole"})
    other = ("J9", {"1": ("SHIELD", "CHASSIS")}, {"description": "D-Sub 9 shell"})
    result = _shielded(hole, other, shield_net="CHASSIS")
    assert _connector(result, "J6")["shield"] == "direct"


# ---------- ground pins ----------

def test_sparse_ground():
    pins = {str(n): (str(n), f"IO{n}") for n in range(1, 11)}
    pins["11"] = ("11", "GND")
    mcu = ("U8", {str(n): (f"P{n}", f"IO{n}", "IO") for n in range(1, 11)}
           | {"11": ("GND", "GND", "Power")}, {})
    header = ("J6", pins, {"description": "Pin header 1x11"})
    (finding,) = _findings(_run(_with(header, mcu)), "sparse_ground", "J6")
    assert (finding["ground_pins"], finding["signal_pins"]) == (1, 10)
    assert finding["longest_signal_run"] == 10 and finding["severity"] == "info"
    interleaved = dict(pins)
    for n in (3, 6, 9):
        interleaved[str(n)] = (str(n), "GND")
    assert not _findings(_run(_with(("J6", interleaved, {"description": "Pin header"}), mcu)),
                         "sparse_ground")
    # Differential pairs carry their own return.
    pairs = {str(n): (str(n), f"LANE{n // 2}_{'P' if n % 2 else 'N'}") for n in range(1, 11)}
    assert not _findings(_run(_with(("J6", pairs, {"description": "Pin header"}))),
                         "sparse_ground")
    # An internal connector gets no finding unless it is named.
    fpc = ("J6", pins, {"description": "FPC connector, 11 positions, ZIF"})
    assert not _findings(_run(_with(fpc, mcu)), connector="J6")
    assert _findings(_run(_with(fpc, mcu), connectors=["J6"]), "sparse_ground", "J6")


def test_ground_pins_that_reach_ground_through_a_part():
    usb = ("J6", {"1": ("VBUS", "VBUS"), "2": ("D-", "USB_DM"), "3": ("D+", "USB_DP"),
                  "4": ("GND", "NetC30_1"), "5": ("SHIELD", "GND")},
           {"description": "USB 2.0 Type-A receptacle"})
    cap = ("C30", {"1": ("1", "NetC30_1"), "2": ("2", "GND")}, {"value": "100nF"})
    result = _run(_with(usb, cap))
    row = _pin(result, "J6", "4")
    assert row["class"] == "ground"
    assert row["class_basis"] == "pin name; reaches a board ground only through C30"
    (finding,) = _findings(result, "ground_indirect", "J6")
    assert finding["severity"] == "warning" and finding["through"] == ["C30"]
    assert "no DC path" in finding["issue"]
    # Through a ferrite there is a DC path: info.
    bead = ("FB9", {"1": ("1", "NetC30_1"), "2": ("2", "GND")}, {"description": "Ferrite bead"})
    (finding,) = _findings(_run(_with(usb, bead)), "ground_indirect", "J6")
    assert finding["severity"] == "info" and finding["through"] == ["FB9"]
    # A local ground with another part's ground pin on it is left alone.
    sensor = ("U9", {"1": ("GND", "NetC30_1", "Power")}, {})
    assert not _findings(_run(_with(usb, cap, sensor)), "ground_indirect")


# ---------- connectors named, filters, not fitted ----------

def test_internal_connectors_are_audited_when_named():
    assert not _findings(_run(), connector="J5")
    named = _run(connectors=["J5"])
    assert list(named["pin_tables"]) == ["J5"]
    (finding,) = _findings(named, "no_esd", "J5")
    assert finding["severity"] == "info" and "checked because it was named" in finding["issue"]
    other = _run(connectors=["U1"])
    assert any("U1 is not recognised as a connector" in note for note in other["notes"])
    assert _run(connectors=["J99"])["error"] == "connector_not_found"
    assert _run(checks=["nope"])["error"] == "invalid_checks"
    only = _run(checks=["no_series_element"])
    assert {f["check"] for f in only["findings"]} == {"no_series_element"}


def test_not_fitted_connectors_are_skipped():
    result = _run(plan=VariantPlan(name="Lite", not_fitted={"J2"}))
    assert "J2" not in {row["designator"] for row in result["connectors"]}
    assert any("1 connector is Not Fitted in variant Lite" in note for note in result["notes"])


def test_pin_tables_by_exposure():
    result = _run(pin_tables=("external",))
    assert set(result["pin_tables"]) == {"J1", "J2"}
    assert "pin_tables" not in _run(pin_tables=False)


# ---------- PCB ----------

def _placements(tvs_x):
    return [
        ("J1", "top", 0.0, 0.0, {"2": ("USB_DM_CONN", 0, 0), "3": ("USB_DP_CONN", 0, 50),
                                 "5": ("GND", 0, 100)}),
        ("D1", "top", tvs_x, 0.0, {"1": ("USB_DP_CONN", 0, 50), "3": ("USB_DM_CONN", 0, 0),
                                   "2": ("GND", 0, 100)}),
        ("U1", "top", 3300.0, 0.0, {"5": ("USB_DM", 0, 0), "6": ("USB_DP", 0, 50)}),
    ]


def test_tvs_distance_to_connector_and_ic():
    netlist = _netlist(PARTS)
    far = itf.run_interface_check(netlist, index=PcbIndex(_pcb(_placements(3000.0)), netlist))
    (finding,) = _findings(far, "tvs_far_from_connector", "J1")
    assert finding["severity"] == "warning" and finding["device"] == "D1"
    assert finding["to_connector_mil"] == pytest.approx(2980.0)
    assert finding["to_ic_mil"] == pytest.approx(280.0)
    assert "nearer the IC than the connector" in finding["issue"]
    assert far["pcb"]["esd_pins_measured"] == 2
    assert _pin(far, "J1", "3")["pcb"]["esd"] == [
        {"designator": "D1", "to_connector_mil": 2980.0, "to_ic_mil": 280.0}]
    near = itf.run_interface_check(netlist, index=PcbIndex(_pcb(_placements(100.0)), netlist))
    assert not _findings(near, "tvs_far_from_connector")
    # Beyond max_distance but still nearer the connector: info.
    wide = itf.run_interface_check(netlist, index=PcbIndex(_pcb(_placements(1500.0)), netlist),
                                   max_distance_mil=1000.0)
    (finding,) = _findings(wide, "tvs_far_from_connector")
    assert finding["severity"] == "info" and "beyond 25.4 mm" in finding["issue"]
    without = itf.run_interface_check(netlist, pcb_unavailable="the project lists no PcbDoc")
    assert without["not_checked"] == {"tvs_far_from_connector": "the project lists no PcbDoc"}


def test_tvs_distance_text_units_and_sides():
    netlist = _netlist(PARTS)
    # The limit keeps its digits and the text follows units.
    tight = itf.run_interface_check(netlist, index=PcbIndex(_pcb(_placements(100.0)), netlist),
                                    max_distance_mil=0.05 * itf.MIL_PER_MM)
    (finding,) = _findings(tight, "tvs_far_from_connector")
    assert "beyond 0.05 mm" in finding["issue"] and "2.03 mm from the connector" in \
        finding["issue"]
    mil = itf.run_interface_check(netlist, index=PcbIndex(_pcb(_placements(100.0)), netlist),
                                  max_distance_mil=2.0, units="mil")
    (finding,) = _findings(mil, "tvs_far_from_connector")
    assert "is 80 mil from the connector pad" in finding["issue"]
    assert "beyond 2 mil" in finding["issue"]
    # A TVS on the bottom under the connector's top pads: plan view, marked opposite side.
    placements = _placements(3000.0)
    placements[1] = ("D1", "bottom", *placements[1][2:])
    flipped = itf.run_interface_check(netlist, index=PcbIndex(_pcb(placements), netlist))
    (finding,) = _findings(flipped, "tvs_far_from_connector")
    assert finding["opposite_side"] is True and "other side of the board" in finding["issue"]
    assert _pin(flipped, "J1", "3")["pcb"]["esd"][0]["opposite_side"] is True
    # Within 2 mm of the connector pad a TVS is at the connector, even with the IC nearer.
    near = _placements(60.0)
    near[2] = ("U1", "top", 90.0, 0.0, {"5": ("USB_DM", 0, 0), "6": ("USB_DP", 0, 50)})
    result = itf.run_interface_check(netlist, index=PcbIndex(_pcb(near), netlist))
    measured = _pin(result, "J1", "3")["pcb"]["esd"][0]
    assert measured["to_ic_mil"] < measured["to_connector_mil"] < 2 * itf.MIL_PER_MM
    assert not _findings(result, "tvs_far_from_connector")


def test_internal_connectors_get_no_pcb_findings_unless_named():
    netlist = _netlist(PARTS)
    index = PcbIndex(_pcb(_placements(3000.0)), netlist)
    inside = itf.run_interface_check(netlist, index=index, internal=["J1"])
    assert not _findings(inside, "tvs_far_from_connector")
    assert _pin(inside, "J1", "3")["pcb"]["esd"]  # still measured in the pin table
    named = itf.run_interface_check(netlist, index=index, internal=["J1"], connectors=["J1"])
    assert _findings(named, "tvs_far_from_connector", "J1")


# ---------- paging ----------

def _many_blocks(count: int):
    parts = list(PARTS)
    for n in range(count):
        parts.append((f"J{100 + n}", {"1": ("1", f"IN{n}"), "2": ("2", "GND")},
                      {"description": "Terminal block"}))
        parts.append((f"U{100 + n}", {"1": ("IN", f"IN{n}", "Input"),
                                      "2": ("GND", "GND", "Power")}, {}))
    return _netlist(parts)


def test_findings_are_paged():
    netlist = _many_blocks(120)
    first = itf.run_interface_check(netlist, checks=["no_esd"])
    # 120 terminal blocks and J2 (warnings), and the J4 pin header (info).
    assert first["total"] == 122 and len(first["findings"]) == 100
    assert first["has_more"] and first["next_offset"] == 100
    assert all(f["severity"] == "warning" for f in first["findings"])
    second = itf.run_interface_check(netlist, checks=["no_esd"], offset=100)
    assert len(second["findings"]) == 22 and "next_offset" not in second
    assert second["findings"][-1]["severity"] == "info"


# ---------- tool ----------

@pytest.fixture
def board(monkeypatch, tmp_path: Path):
    netlist = _netlist(PARTS)
    index = PcbIndex(_pcb(_placements(3000.0)), netlist)
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


def test_check_interfaces_tool(board):
    result = json.loads(main_mod.check_interfaces())
    assert result["pcb_doc"] == "Board.PcbDoc" and result["units"] == "mm"
    assert result["variant"] == "Default" and "pin_tables" in result
    (far,) = _findings(result, "tvs_far_from_connector")
    assert far["to_connector_mm"] == pytest.approx(75.692)
    assert result["pcb"]["max_distance_mm"] == pytest.approx(10.0, abs=1e-3)
    assert result["findings"][0]["severity"] == "warning"
    mil = json.loads(main_mod.check_interfaces(units="mil", max_distance=4000))
    assert mil["pcb"]["max_distance_mil"] == pytest.approx(4000.0)
    named = json.loads(main_mod.check_interfaces(connector="J5, J1", checks=["no_esd"]))
    assert set(named["pin_tables"]) == {"J1", "J5"}
    assert [f["connector"] for f in named["findings"]] == ["J5"]
    ext = json.loads(main_mod.check_interfaces(external="J4"))
    assert _connector(ext, "J4")["exposure"] == "external"
    assert json.loads(main_mod.check_interfaces(units="inch"))["error"] == "invalid_units"
    assert json.loads(main_mod.check_interfaces(max_distance=0))["error"] == \
        "invalid_max_distance"
    assert json.loads(main_mod.check_interfaces(max_distance=float("nan")))["error"] == \
        "invalid_max_distance"
    assert json.loads(main_mod.check_interfaces(connector="J99"))["error"] == \
        "connector_not_found"
    assert json.loads(main_mod.check_interfaces(checks=["x"]))["error"] == "invalid_checks"


def test_check_interfaces_without_a_board(board, monkeypatch):
    monkeypatch.setitem(main_mod._project, "pcb_doc_paths", [])
    result = json.loads(main_mod.check_interfaces())
    assert "pcb" not in result and "units" not in result
    assert result["not_checked"]["tvs_far_from_connector"] == "the project lists no PcbDoc"


def test_check_interfaces_pages_and_shrinks(board, monkeypatch):
    monkeypatch.setattr(main_mod._altium, "_netlist", _many_blocks(150))
    monkeypatch.setitem(main_mod._project, "pcb_doc_paths", [])
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 10**6)
    result = json.loads(main_mod.check_interfaces(checks=["no_esd"]))
    assert result["total"] == 152 and result["next_offset"] == 100
    assert len(result["connectors"]) == 60 and len(result["pin_tables"]) == 60
    assert "findings_limited_to" not in result and "pin_tables_omitted" not in result
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 30000)
    trimmed = json.loads(main_mod.check_interfaces(checks=["no_esd"]))
    assert trimmed["findings_limited_to"] in (40, 15)
    assert trimmed["next_offset"] == trimmed["findings_limited_to"]
    assert len(trimmed["connectors"]) <= 20 and "pin_tables" not in trimmed
    assert "name connectors in connector=" in trimmed["pin_tables_omitted"]
    assert "external connectors' are given" not in trimmed["pin_tables_omitted"]
    named = json.loads(main_mod.check_interfaces(connector="J100", checks=["no_esd"]))
    assert list(named["pin_tables"]) == ["J100"] and "pin_tables_omitted" not in named


def test_named_connectors_keep_their_tables(board, monkeypatch):
    # A wide internal connector named in connector=: its table shrinks to its signal pins
    # rather than vanishing.
    pins = {str(n): (str(n), "GND" if n % 4 == 0 else f"BUS{n}") for n in range(1, 161)}
    ic = ("U9", {str(n): (f"P{n}", f"BUS{n}", "IO") for n in range(1, 161) if n % 4}
          | {"200": ("GND", "GND", "Power")}, {"description": "Processor"})
    socket = ("J9", pins, {"description": "SO-DIMM socket, 160 positions"})
    monkeypatch.setattr(main_mod._altium, "_netlist", _netlist(_with(ic, socket)))
    monkeypatch.setitem(main_mod._project, "pcb_doc_paths", [])
    full = main_mod.check_interfaces(connector="J9")
    assert len(json.loads(full)["pin_tables"]["J9"]) == 160
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", len(full) // 2)
    result = json.loads(main_mod.check_interfaces(connector="J9"))
    table = result["pin_tables"]["J9"]
    assert table and all(row["class"] == "signal" for row in table)
    assert result["pin_tables_truncated"]["J9"].startswith(f"{len(table)} of 160 pins")
    assert "pin_tables_truncated" in result["pin_tables_omitted"]
    assert len(json.dumps(result, indent=2)) <= len(full) // 2


def test_check_interfaces_needs_a_project(monkeypatch):
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.check_interfaces())["error"] == "no_project"
    assert "next_offset" in main_mod.check_interfaces.__doc__
    assert "heuristic" in main_mod.check_interfaces.__doc__


def test_check_interfaces_without_components(board, monkeypatch):
    monkeypatch.setattr(main_mod._altium, "_netlist", {**board, "components": {}})
    assert json.loads(main_mod.check_interfaces())["error"] == "no_components_loaded"


def test_instructions_name_the_tool_after_the_cut():
    assert main_mod.SERVER_INSTRUCTIONS.index("check_interfaces") > 4096
