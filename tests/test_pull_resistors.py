"""Pull-up / pull-down audit and I2C pull-up sizing (services/pull_resistors,
check_pull_resistors), and the pin electrical types it reads from the sheets."""

import json
from functools import partial
from unittest.mock import patch

import pytest

import altium
import server.main as main_mod
import server.services.pull_resistors as pr
from altium import AltiumClient
from server.altium import enrich_netlist
from server.parsers.prj_pcb import VariantDefinition, VariantState
from server.parsers.sch_doc import PIN_TYPES, components_from_stream, connectivity_from_stream
from server.services.bom import VariantPlan
from services.sch_netlister import compile_netlist
from tests.sch_fixture import Sheet, project, stream

# (refdes, {pin: (pin name, net) or (pin name, net, electrical type)}, info)
PARTS = [
    ("U1", {"1": ("VDD", "3V3", "Power"), "2": ("GND", "GND", "Power"),
            "3": ("NRST", "NRST", "Input"), "4": ("PA0", "REG_EN", "IO"),
            "5": ("PB7", "I2C1_SDA", "IO"), "6": ("PB6", "I2C1_SCL", "IO"),
            "7": ("PA1", "FET_DRIVE", "IO"), "8": ("PA2", "SENSOR_IRQ", "IO")}, {}),
    ("U2", {"1": ("VDD", "3V3", "Power"), "2": ("GND", "GND", "Power"),
            "3": ("SDA", "I2C1_SDA", "IO"), "4": ("SCL", "I2C1_SCL", "Input"),
            "5": ("ALERT", "SENSOR_IRQ", "OpenCollector")}, {}),
    ("U3", {"1": ("VIN", "5V", "Power"), "2": ("GND", "GND", "Power"),
            "3": ("EN", "REG_EN", "Input"), "4": ("VOUT", "3V3", "Power")}, {}),
    ("R1", {"1": ("1", "I2C1_SDA"), "2": ("2", "3V3")}, {"value": "4.7k"}),
    ("R2", {"1": ("1", "I2C1_SCL"), "2": ("2", "3V3")}, {"value": "4k7"}),
    ("R3", {"1": ("1", "NRST"), "2": ("2", "3V3")}, {"value": "10k"}),
    ("C1", {"1": ("1", "3V3"), "2": ("2", "GND")}, {"value": "100nF"}),
    ("Q1", {"1": ("G", "FET_DRIVE"), "2": ("S", "GND"), "3": ("D", "LOAD")},
     {"description": "N-channel MOSFET"}),
]


def _netlist(parts=PARTS):
    components, pin_to_net, nets, compiled = {}, {}, {}, {}
    for refdes, pins, info in parts:
        entry = {"pins": {}, "params": {}, "kind": 0, "description": None, "value": None,
                 "pin_types": {}}
        entry.update(info)
        if entry.get("value"):
            entry["params"] = {**entry["params"], "value": entry["value"]}
        for pin, spec in pins.items():
            name, net = spec[:2]
            entry["pins"][pin] = {"name": name, "net": net}
            entry["pin_types"][pin] = spec[2] if len(spec) > 2 else "Passive"
            pin_to_net.setdefault(refdes, {})[pin] = net
            nets.setdefault(net, []).append((refdes, pin))
        components[refdes] = entry
        compiled[refdes] = {"footprint": "", "comment": info.get("comment", "")}
    return {"nets": nets, "pin_to_net": pin_to_net, "components": components,
            "parts": compiled}


def _with(*extra, drop=()):
    return [part for part in PARTS if part[0] not in drop] + list(extra)


def _findings(result, check=None, net=None):
    return [f for f in result["findings"]
            if (check is None or f["check"] == check) and (net is None or f["net"] == net)]


def _signal(result, net):
    return next(row for row in result["signals"] if row["net"] == net)


# ---------- pin electrical types ----------

def test_pin_types_are_read_from_the_pin_records():
    sheet = Sheet()
    sheet.part("U1", [("1", 0, 0, "|ELECTRICAL=3"), ("2", 0, 10), ("3", 0, 20, "|ELECTRICAL=7"),
                      ("4", 0, 30, "|ELECTRICAL=1"), ("5", 0, 40, "|ELECTRICAL=6"),
                      ("6", 0, 50, "|ELECTRICAL=2"), ("7", 0, 60, "|ELECTRICAL=4"),
                      ("8", 0, 70, "|ELECTRICAL=5")])
    meta = components_from_stream(stream(sheet.records))["U1"]
    assert meta.pin_types == {"1": "OpenCollector", "2": "Input", "3": "Power", "4": "IO",
                              "5": "OpenEmitter", "6": "Output", "7": "Passive", "8": "HiZ"}
    assert set(PIN_TYPES.values()) == set(meta.pin_types.values())
    # The netlist compiler reads the same metadata.
    assert connectivity_from_stream(stream(sheet.records)).components["U1"].pin_types == \
        meta.pin_types


def test_each_part_of_a_multi_part_component_keeps_its_pins_types():
    sheet = Sheet()
    sheet.part("U5", [("1", 0, 0, "|ELECTRICAL=2"), ("8", 0, 10, "|ELECTRICAL=7")],
               uid="AAAA0001", part_id=1, part_count=3)
    sheet.part("U5", [("7", 100, 0, "|ELECTRICAL=3"), ("8", 100, 10, "|ELECTRICAL=7")],
               uid="AAAA0002", part_id=2, part_count=3, at=(100, 0))
    meta = components_from_stream(stream(sheet.records))["U5"]
    assert meta.pin_types == {"1": "Output", "8": "Power", "7": "OpenCollector"}


def test_a_pin_keeps_its_normal_display_modes_type():
    sheet = Sheet()
    sheet.part("U1", [("1", 0, 0, "|ELECTRICAL=3"),
                      ("1", 0, 0, "|ELECTRICAL=2|OWNERPARTDISPLAYMODE=1")])
    assert components_from_stream(stream(sheet.records))["U1"].pin_types == {"1": "OpenCollector"}


def test_the_enriched_netlist_carries_pin_types():
    sheet = Sheet()
    sheet.part("U1", [("1", 0, 0, "|ELECTRICAL=3"), ("2", 0, 10)])
    meta = components_from_stream(stream(sheet.records))["U1"]
    netlist = {"nets": {"N": [("U1", "1")]}, "pin_to_net": {"U1": {"1": "N"}}}
    enrich_netlist(netlist, {"U1": meta})
    entry = netlist["components"]["U1"]
    assert entry["pin_types"] == {"1": "OpenCollector", "2": "Input"}  # unconnected pins too
    assert entry["pins"] == {"1": {"name": "P1", "net": "N"}}  # pins keep their shape


def test_an_offline_compile_finds_a_pulled_open_collector_pin(tmp_path):
    sheet = Sheet()
    sheet.part("U1", [("1", 0, 0, "|ELECTRICAL=3"), ("2", 0, 100, "|ELECTRICAL=1")])
    sheet.part("U2", [("1", 300, 100), ("2", 300, 200, "|ELECTRICAL=7")])
    sheet.part("R1", [("1", 200, 0), ("2", 200, 200)], params={"Value": "10k"})
    sheet.wire((0, 0), (200, 0))
    sheet.wire((200, 200), (300, 200))
    sheet.power("3V3", 300, 200)
    sheet.wire((0, 100), (300, 100))
    prj, read = project(tmp_path, {"a.SchDoc": sheet})
    with patch.object(altium, "compile_netlist", partial(compile_netlist, read_sheet=read)):
        netlist = AltiumClient().compile_offline(prj)
    assert netlist["components"]["U1"]["pin_types"]["1"] == "OpenCollector"
    result = pr.run_pull_resistor_check(netlist)
    (row,) = [row for row in result["signals"] if "open_drain" in row["classes"]]
    assert row["classes"]["open_drain"] == ["U1.1 (P1) is OpenCollector"]
    assert row["pulls"] == [{"designator": "R1", "direction": "up", "to": "3V3",
                             "value": "10k", "on": row["net"]}]
    assert result["severity_counts"]["warning"] == 0


# ---------- names ----------

@pytest.mark.parametrize("name, kind", [
    ("NRST", "reset"), ("RESET_N", "reset"), ("~RESET", "reset"), ("PERST#", "reset"),
    ("PWR_EN", "enable"), ("EN2", "enable"), ("nSHDN", "shutdown"), ("BOOT0", "boot"),
    ("STRAP1", "strap"), ("MODE", "mode"), ("PROG_B", "program"), ("ETH_TX_EN", None),
    ("GREEN", None), ("PA0", None), ("RESISTOR", None),
])
def test_control_names(name, kind):
    assert pr.control_kind(name) == kind


@pytest.mark.parametrize("name, line", [
    ("I2C1_SDA", ("SDA", "I2C1_*")), ("I2C1_SCL", ("SCL", "I2C1_*")), ("SDA0", ("SDA", "*0")),
    ("SCL", ("SCL", "*")), ("SMBCLK", ("SCL", "SMB*")), ("SMB_DATA", ("SDA", "SMB*")),
    ("SCLK", None), ("SPI_SCK", None),
])
def test_i2c_names(name, line):
    assert pr.i2c_line(name) == line


@pytest.mark.parametrize("net, volts", [
    ("3V3", 3.3), ("+3V3", 3.3), ("+3.3V", 3.3), ("1V8", 1.8), ("VDD_3V3", 3.3),
    ("P3V3", 3.3), ("5V", 5.0), ("VCC_1.8V", 1.8), ("VDD33", 3.3), ("VCC", None),
    ("VBUS", None), ("-5V", None),
])
def test_rail_voltage_from_the_name(net, volts):
    assert pr.rail_voltage(net) == volts


@pytest.mark.parametrize("texts, ohms", [
    (("10k",), 10e3), (("4K7",), 4.7e3), (("100R",), 100.0), ((None, "RES 10K 1% 0402"), 10e3),
    (("see note", "Chip Resistor, 49.9 Ohm, 1%"), 49.9), (("DNP",), None),
])
def test_resistance(texts, ohms):
    assert pr.resistance(*texts) == (pytest.approx(ohms) if ohms else None)


# ---------- classification and pulls ----------

def test_signals_are_classified_with_their_pulls():
    result = pr.run_pull_resistor_check(_netlist())
    classes = {row["net"]: set(row["classes"]) for row in result["signals"]}
    assert classes == {"NRST": {"control"}, "REG_EN": {"control"}, "I2C1_SDA": {"i2c"},
                       "I2C1_SCL": {"i2c"}, "FET_DRIVE": {"gate"},
                       "SENSOR_IRQ": {"open_drain"}}
    assert _signal(result, "NRST")["pulls"][0] == {
        "designator": "R3", "direction": "up", "to": "3V3", "value": "10k", "on": "NRST"}
    assert _signal(result, "REG_EN")["control"] == "enable"
    assert "driven by U1.7" in " ".join(_signal(result, "FET_DRIVE")["classes"]["gate"])
    assert result["overview"]["signals_classified"] == {
        "control": 2, "i2c": 2, "gate": 1, "open_drain": 1}
    assert result["overview"]["with_fitted_pull"] == {"control": 1, "i2c": 2}
    assert "heuristic" in result and "connector_no_pull" not in result["checks_run"]


def test_missing_pulls_and_their_severity():
    result = pr.run_pull_resistor_check(_netlist())
    by_net = {f["net"]: f for f in result["findings"]}
    assert by_net["SENSOR_IRQ"]["check"] == "open_drain_no_pull"
    assert by_net["SENSOR_IRQ"]["severity"] == "warning"  # an OpenCollector pin
    assert by_net["FET_DRIVE"]["check"] == "gate_no_pull"
    assert by_net["FET_DRIVE"]["severity"] == "warning"
    assert by_net["REG_EN"]["check"] == "control_no_pull"
    assert by_net["REG_EN"]["severity"] == "info"  # ICs often pull enables internally
    assert by_net["REG_EN"]["issue"].startswith("REG_EN is an enable net with no pull-up")
    assert result["severity_counts"] == {"warning": 2, "info": 1}
    assert [f["severity"] for f in result["findings"]] == ["warning", "warning", "info"]


def test_a_pull_down_or_a_push_pull_driver_clears_the_finding():
    pulled = _with(("R4", {"1": ("1", "FET_DRIVE"), "2": ("2", "GND")}, {"value": "100k"}))
    result = pr.run_pull_resistor_check(_netlist(pulled))
    assert not _findings(result, net="FET_DRIVE")
    driven = _with(("U4", {"1": ("RESET", "REG_EN", "Output"), "2": ("GND", "GND", "Power")}, {}))
    result = pr.run_pull_resistor_check(_netlist(driven))
    assert not _findings(result, net="REG_EN")
    assert _signal(result, "REG_EN")["push_pull_drivers"] == ["U4.1 (RESET)"]


def test_a_gate_driver_output_needs_no_pull():
    parts = _with(("U4", {"1": ("HO", "GATE_H", "Passive"), "2": ("VCC", "5V", "Power")},
                   {"description": "Half-bridge gate driver"}),
                  ("Q2", {"1": ("G", "GATE_H"), "2": ("S", "SW"), "3": ("D", "5V")}, {}))
    result = pr.run_pull_resistor_check(_netlist(parts))
    assert not _findings(result, net="GATE_H")


def test_a_pull_through_a_low_value_series_resistor_holds_both_nets():
    parts = _with(("R4", {"1": ("1", "REG_EN"), "2": ("2", "NODE_A")}, {"value": "0R"}),
                  ("R5", {"1": ("1", "NODE_A"), "2": ("2", "GND")}, {"value": "100k"}))
    result = pr.run_pull_resistor_check(_netlist(parts))
    assert not _findings(result, net="REG_EN")
    row = _signal(result, "REG_EN")
    assert row["joined_nets"] == ["NODE_A"] and row["pulls"][0]["designator"] == "R5"


def test_only_not_fitted_pulls():
    plan = VariantPlan(name="Lite", not_fitted={"R3"})
    result = pr.run_pull_resistor_check(_netlist(), plan)
    (finding,) = _findings(result, "not_fitted_pull")
    assert finding["net"] == "NRST" and finding["severity"] == "warning"
    assert "Not Fitted in variant Lite" in finding["issue"]
    assert finding["pulls"][0]["not_fitted"] is True


def test_only_not_fitted_pulls_on_a_signal_leaving_the_board_is_info():
    # An I2C pull-up fitted only on the board that is the bus master: as with no pull at
    # all, the pull may be on the other board.
    parts = _with(("J1", {"1": ("1", "I2C1_SDA"), "2": ("2", "I2C1_SCL")}, {}))
    result = pr.run_pull_resistor_check(_netlist(parts), VariantPlan(name="Lite",
                                                                     not_fitted={"R1"}))
    (finding,) = _findings(result, "not_fitted_pull")
    assert finding["net"] == "I2C1_SDA" and finding["severity"] == "info"
    assert finding["offboard"] == ["J1"] and "other board" in finding["issue"]


def test_a_net_fed_through_a_series_resistor_is_not_called_undriven():
    # An RC reset fed from a GPIO through 4.7 kohm: the GPIO drives it.
    parts = _with(("U4", {"1": ("RESETN", "RST_RC", "Input"), "2": ("GND", "GND", "Power")}, {}),
                  ("R4", {"1": ("1", "RST_RC"), "2": ("2", "MCU_RST")}, {"value": "4.7k"}),
                  ("U5", {"1": ("PA3", "MCU_RST", "IO")}, {}),
                  ("C2", {"1": ("1", "RST_RC"), "2": ("2", "GND")}, {"value": "100nF"}))
    result = pr.run_pull_resistor_check(_netlist(parts))
    (finding,) = _findings(result, "control_no_pull", "RST_RC")
    assert "undriven" not in finding and "nothing ever drives it" not in finding["issue"]
    alone = _with(("U4", {"1": ("RESETN", "RST_RC", "Input"), "2": ("GND", "GND", "Power")}, {}))
    (finding,) = _findings(pr.run_pull_resistor_check(_netlist(alone)), "control_no_pull",
                           "RST_RC")
    assert finding["undriven"] is True and "nothing ever drives it" in finding["issue"]


def test_pull_up_and_down_and_parallel_pull_ups():
    parts = _with(("R4", {"1": ("1", "NRST"), "2": ("2", "GND")}, {"value": "100k"}),
                  ("R5", {"1": ("1", "I2C1_SDA"), "2": ("2", "3V3")}, {"value": "4.7k"}))
    result = pr.run_pull_resistor_check(_netlist(parts))
    (both,) = _findings(result, "pull_up_and_down")
    assert both["net"] == "NRST" and both["designators"] == ["R3", "R4"]
    (parallel,) = _findings(result, "parallel_pull_ups")
    assert parallel["net"] == "I2C1_SDA" and parallel["effective_ohm"] == pytest.approx(2350)
    assert both["severity"] == parallel["severity"] == "info"


def test_a_signal_leaving_the_board_is_info():
    parts = _with(("J1", {"1": ("1", "I2C1_SDA"), "2": ("2", "I2C1_SCL")}, {}),
                  drop=("R1", "R2"))
    result = pr.run_pull_resistor_check(_netlist(parts))
    sda = _findings(result, "open_drain_no_pull", "I2C1_SDA")[0]
    assert sda["severity"] == "info" and sda["offboard"] == ["J1"]
    assert "other board" in sda["issue"]


def test_resistor_arrays_pull_each_section():
    parts = _with(("RN1", {"1": ("1", "I2C1_SDA"), "8": ("8", "3V3"), "2": ("2", "I2C1_SCL"),
                           "7": ("7", "3V3")},
                   {"value": "2.2k", "pin_designators": [str(n) for n in range(1, 9)]}),
                  drop=("R1", "R2"))
    result = pr.run_pull_resistor_check(_netlist(parts))
    sda = _signal(result, "I2C1_SDA")["pulls"]
    assert sda == [{"designator": "RN1", "direction": "up", "to": "3V3", "value": "2.2k",
                    "on": "I2C1_SDA", "pins": "1-8"}]


def test_connector_inputs_only_when_asked():
    parts = _with(("J1", {"1": ("1", "BUTTON")}, {}),
                  ("U4", {"1": ("IN", "BUTTON", "Input"), "2": ("GND", "GND", "Power")}, {}))
    assert not _findings(pr.run_pull_resistor_check(_netlist(parts)), "connector_no_pull")
    result = pr.run_pull_resistor_check(_netlist(parts), checks=["connector_no_pull"])
    (finding,) = result["findings"]
    assert finding["net"] == "BUTTON" and finding["severity"] == "info"


def test_net_filter_and_errors():
    result = pr.run_pull_resistor_check(_netlist(), nets=["nrst", "3V3"])
    assert [row["net"] for row in result["signals"]] == ["NRST"]
    assert any("3V3 is a rail" in note for note in result["notes"])
    assert pr.run_pull_resistor_check(_netlist(), nets=["NOPE"])["error"] == "net_not_found"
    assert pr.run_pull_resistor_check(_netlist(), checks=["nope"])["error"] == "invalid_checks"
    assert pr.run_pull_resistor_check(_netlist(), i2c_mode="turbo")["error"] == \
        "invalid_i2c_mode"
    assert pr.run_pull_resistor_check(_netlist(), bus_capacitance_pf=-1)["error"] == \
        "invalid_bus_capacitance"
    huge = pr.run_pull_resistor_check(_netlist(), bus_capacitance_pf=2e5)
    assert huge["error"] == "invalid_bus_capacitance" and "below 100000" in huge["message"]


def _gate_through_resistor():
    return [
        ("U1", {"1": ("PA1", "MCU_FET", "IO"), "2": ("VDD", "3V3", "Power")}, {}),
        ("R1", {"1": ("1", "MCU_FET"), "2": ("2", "GATE_N")}, {"value": "1k"}),
        ("Q1", {"1": ("G", "GATE_N"), "2": ("S", "GND"), "3": ("D", "LOAD")}, {}),
    ]


def test_a_gate_is_found_from_its_driving_net():
    result = pr.run_pull_resistor_check(_netlist(_gate_through_resistor()), nets=["MCU_FET"])
    assert [row["net"] for row in result["signals"]] == ["GATE_N"]
    assert _signal(result, "GATE_N")["driven_through"] == ["MCU_FET"]
    assert [f["check"] for f in result["findings"]] == ["gate_no_pull"]
    assert not any("no reset, enable" in note for note in result["notes"])


def _switched_pull_ups():
    return [
        ("U1", {"1": ("SDA", "NetU1_1", "IO"), "2": ("SCL", "NetU1_2", "IO"),
                "3": ("PIO2", "GPIO_PULL", "IO")}, {}),
        ("U2", {"1": ("SDA", "NetU1_1", "IO"), "2": ("SCL", "NetU1_2", "Input")}, {}),
        ("R1", {"1": ("1", "NetU1_1"), "2": ("2", "GPIO_PULL")}, {"value": "4.7k"}),
        ("R2", {"1": ("1", "NetU1_2"), "2": ("2", "GPIO_PULL")}, {"value": "4.7k"}),
    ]


def test_a_net_filter_keeps_a_switched_pull_up():
    # The switched supply is recognised by the two lines it pulls up, also when only
    # one of them is asked for.
    result = pr.run_pull_resistor_check(_netlist(_switched_pull_ups()), nets=["NetU1_1"])
    assert [row["net"] for row in result["signals"]] == ["NetU1_1"]
    assert _signal(result, "NetU1_1")["pulls"][0]["to_signal"] is True
    assert not _findings(result, "open_drain_no_pull")


# ---------- I2C sizing ----------

def _bus(result):
    (bus,) = result["i2c"]["buses"]
    return bus


def test_i2c_buses_pair_by_name_and_size_the_pull_ups():
    result = pr.run_pull_resistor_check(_netlist(), bus_capacitance_pf=100, i2c_mode="fast")
    bus = _bus(result)
    assert bus["bus"] == "I2C1_SDA / I2C1_SCL" and bus["paired_by"] == "name"
    sda = bus["lines"]["SDA"]
    assert sda["rmin_ohm"] == pytest.approx(966.7, abs=0.1)  # (3.3 - 0.4) V / 3 mA
    assert sda["rmax_ohm"] == pytest.approx(3540.7, abs=0.1)  # 300 ns / (0.8473 x 100 pF)
    assert sda["effective_ohm"] == 4700 and sda["vdd_v"] == 3.3
    assert sda["verdict"] == bus["verdict"] == "too_high"
    (finding,) = _findings(result, "i2c_pull_value", "I2C1_SDA")
    assert finding["severity"] == "warning"  # the mode was given


def test_a_pull_up_too_weak_only_for_the_assumed_fast_mode_is_info():
    result = pr.run_pull_resistor_check(_netlist(), bus_capacitance_pf=100)
    assert _bus(result)["verdict"] == "too_high"
    finding = _findings(result, "i2c_pull_value", "I2C1_SDA")[0]
    assert finding["severity"] == "info" and "i2c_mode='standard'" in finding["issue"]
    standard = pr.run_pull_resistor_check(_netlist(), bus_capacitance_pf=100,
                                          i2c_mode="standard")
    assert _bus(standard)["verdict"] == "ok"
    assert _bus(standard)["lines"]["SDA"]["rmax_ohm"] == pytest.approx(11802.2, abs=0.1)


def test_fast_plus_and_too_low():
    parts = _with(("R1", {"1": ("1", "I2C1_SDA"), "2": ("2", "3V3")}, {"value": "680"}),
                  drop=("R1",))
    result = pr.run_pull_resistor_check(_netlist(parts), bus_capacitance_pf=50)
    sda = _bus(result)["lines"]["SDA"]
    assert sda["verdict"] == "too_low" and _findings(result, "i2c_pull_value")[0]["severity"] \
        == "warning"
    plus = pr.run_pull_resistor_check(_netlist(parts), bus_capacitance_pf=50,
                                      i2c_mode="fast_plus")
    line = _bus(plus)["lines"]["SDA"]
    assert line["rmin_ohm"] == pytest.approx(145.0)  # (3.3 - 0.4) V / 20 mA
    assert line["verdict"] == "ok"


def test_an_unknown_rail_voltage_is_not_sized():
    parts = _with(("R1", {"1": ("1", "I2C1_SDA"), "2": ("2", "VCC")}, {"value": "4.7k"}),
                  ("U4", {"1": ("VCC", "VCC", "Power")}, {}), drop=("R1",))
    result = pr.run_pull_resistor_check(_netlist(parts))
    sda = _bus(result)["lines"]["SDA"]
    assert sda["verdict"] == "unknown" and "rmin_ohm" not in sda
    assert "no voltage in the pull-up's rail name (VCC)" in sda["reason"]
    assert "rmax_ohm" in sda  # Rmax needs no voltage
    assert _findings(result, "i2c_pull_value", "I2C1_SDA")[0]["severity"] == "info"


def test_a_pull_up_over_rmax_is_too_high_whatever_its_rail():
    parts = _with(("R1", {"1": ("1", "I2C1_SDA"), "2": ("2", "VCC")}, {"value": "47k"}),
                  ("U4", {"1": ("VCC", "VCC", "Power")}, {}), drop=("R1",))
    result = pr.run_pull_resistor_check(_netlist(parts), bus_capacitance_pf=100,
                                        i2c_mode="fast")
    sda = _bus(result)["lines"]["SDA"]
    assert sda["rmax_ohm"] == pytest.approx(3540.7, abs=0.1) and sda["verdict"] == "too_high"
    assert "rmin_ohm" not in sda and "no voltage in the pull-up's rail name (VCC)" in \
        sda["rmin_note"]
    (finding,) = _findings(result, "i2c_pull_value", "I2C1_SDA")
    assert finding["verdict"] == "too_high" and finding["severity"] == "warning"


def test_capacitance_estimate_and_routed_length(monkeypatch):
    calls = []

    def loader():
        calls.append(1)
        return "index", None

    monkeypatch.setattr(pr, "net_length", lambda index, net: {"routed_mil": 1000 / 0.0254})
    result = pr.run_pull_resistor_check(_netlist(), index_loader=loader)
    sda = _bus(result)["lines"]["SDA"]
    # U1 and U2 pins (2 x 10 pF) and 1000 mm of routing x 0.12 pF/mm.
    assert sda["cb_pf"] == pytest.approx(140.0)
    assert "2 pins x 10 pF" in sda["cb_basis"] and "1000 mm routed" in sda["cb_basis"]
    assert calls == [1]
    given = pr.run_pull_resistor_check(_netlist(), index_loader=loader, bus_capacitance_pf=80)
    assert _bus(given)["lines"]["SDA"]["cb_basis"] == "given (bus_capacitance_pf)"
    assert calls == [1]  # no PCB needed
    missing = pr.run_pull_resistor_check(_netlist(), index_loader=lambda: (None, "no PcbDoc"))
    assert any("leaves out routing: no PcbDoc" in note for note in missing["notes"])


def test_buses_pair_by_shared_parts_and_switched_pull_ups():
    result = pr.run_pull_resistor_check(_netlist(_switched_pull_ups()))
    bus = _bus(result)
    assert bus["paired_by"] == "shared_parts"
    assert bus["lines"]["SDA"]["pull_ups"][0]["to_signal"] is True
    assert not _findings(result, "open_drain_no_pull")


# ---------- paging ----------

def _many_enables(count: int):
    parts = list(PARTS)
    for n in range(count):
        parts.append((f"U{10 + n}", {"1": ("EN", f"EN_{n}", "Input"),
                                     "2": ("GND", "GND", "Power")}, {}))
    return _netlist(parts)


def test_findings_are_paged():
    netlist = _many_enables(150)
    first = pr.run_pull_resistor_check(netlist, checks=["control_no_pull"])
    assert first["total"] == 151 and len(first["findings"]) == 100
    assert first["has_more"] and first["next_offset"] == 100
    second = pr.run_pull_resistor_check(netlist, checks=["control_no_pull"], offset=100)
    assert len(second["findings"]) == 51 and not second["has_more"]
    assert "next_offset" not in second


# ---------- tool ----------

@pytest.fixture
def board(monkeypatch, tmp_path):
    netlist = _netlist()
    monkeypatch.setattr(main_mod, "_project", {
        "name": "Demo", "root_dir": str(tmp_path), "prj_pcb_path": str(tmp_path / "D.PrjPcb"),
        "sheets": [], "pcb_doc_paths": [],
    })
    monkeypatch.setattr(main_mod._altium, "_netlist", netlist)
    monkeypatch.setattr(main_mod, "_variant_state",
                        VariantState([VariantDefinition(name="Default")]))
    return netlist


def test_check_pull_resistors_tool(board):
    result = json.loads(main_mod.check_pull_resistors())
    assert result["variant"] == "Default" and result["severity_counts"]["warning"] == 2
    assert result["i2c"]["mode"] == "fast"
    assert any("no PcbDoc" in note for note in result["notes"])
    one = json.loads(main_mod.check_pull_resistors(net="I2C1_SDA, I2C1_SCL",
                                                   bus_capacitance_pf=100, i2c_mode="standard"))
    assert [bus["verdict"] for bus in one["i2c"]["buses"]] == ["ok"]
    assert {row["net"] for row in one["signals"]} == {"I2C1_SDA", "I2C1_SCL"}
    checks = json.loads(main_mod.check_pull_resistors(checks=["gate_no_pull"]))
    assert [f["net"] for f in checks["findings"]] == ["FET_DRIVE"]
    assert json.loads(main_mod.check_pull_resistors(net="NOPE"))["error"] == "net_not_found"
    assert json.loads(main_mod.check_pull_resistors(bus_capacitance_pf=0))["error"] == \
        "invalid_bus_capacitance"
    assert json.loads(main_mod.check_pull_resistors(bus_capacitance_pf=float("nan")))[
        "error"] == "invalid_bus_capacitance"


def test_check_pull_resistors_pages_and_shrinks(board, monkeypatch):
    many = _many_enables(150)
    monkeypatch.setattr(main_mod._altium, "_netlist", many)
    result = json.loads(main_mod.check_pull_resistors(checks=["control_no_pull"]))
    assert result["total"] == 151 and result["next_offset"] == 100
    assert "findings_limited_to" not in result
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 20000)
    trimmed = json.loads(main_mod.check_pull_resistors(checks=["control_no_pull"]))
    assert trimmed["findings_limited_to"] in (40, 15)
    assert trimmed["next_offset"] == trimmed["findings_limited_to"]
    assert len(trimmed["signals"]) <= 20


def test_check_pull_resistors_needs_a_project(monkeypatch):
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.check_pull_resistors())["error"] == "no_project"
    assert "next_offset" in main_mod.check_pull_resistors.__doc__
    assert "heuristic" in main_mod.check_pull_resistors.__doc__


def test_check_pull_resistors_without_components(board, monkeypatch):
    monkeypatch.setattr(main_mod._altium, "_netlist", {**board, "components": {}})
    assert json.loads(main_mod.check_pull_resistors())["error"] == "no_components_loaded"


def test_instructions_name_the_tool_after_the_cut():
    assert main_mod.SERVER_INSTRUCTIONS.index("check_pull_resistors") > 4096
