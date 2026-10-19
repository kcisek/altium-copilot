"""Differential-pair consistency checks (services/diff_pair_checks)."""

from collections import defaultdict
from copy import deepcopy
from pathlib import Path

import pytest

from server.parsers.pcb_doc import (
    DiffPair,
    PcbClass,
    PcbComponent,
    PcbData,
    PcbRule,
    XSignal,
)
from server.services.diff_pair_checks import (
    check_diff_pairs,
    complement,
    interface_hint,
    is_auto_net_name,
    is_supply_name,
    parse_value,
    polarity_tokens,
)
from server.services.pcb_index import PcbIndex
from tests.test_pcb_index import NETLIST, _board, _pad, _track, pcb_data  # noqa: F401

LAYERS = {"top layer": 1, "l2-gnd": 2, "l3-sig": 3, "bottom layer": 32, "top": 1, "bottom": 32}


# ---------- synthetic boards ----------


def _rule(kind, priority, scope1="All", name=None, enabled=True, **props):
    return PcbRule(
        name=name or kind,
        kind=kind,
        kind_id=0,
        enabled=enabled,
        priority=priority,
        scope1=scope1,
        scope2="All",
        net_scope="AnyNet",
        comment="",
        unique_id=None,
        from_schematic=False,
        properties=props,
    )


def _in_pair_rule(scope, tolerance="5mil", name="ML", priority=1, **props):
    return _rule(
        "MatchedLengths", priority, scope, name=name,
        TOLERANCE=tolerance, CHECKNETSINDIFFPAIR="TRUE",
        CHECKDIFFPAIRVSDIFFPAIR="FALSE", CHECKXSIGNALS="FALSE",
        CHECKOTHERS="FALSE", **props,
    )


def _build(parts, pairs=(), lengths=None, rules=(), classes=(), extra_nets=(),
           xsignals=()):
    """PcbData from parts [(designator, footprint, {pad: net}), ...].

    Nets are collected from the pads plus extra_nets; lengths {net: mil}
    adds one straight top-layer track per net. Each part is a PcbComponent
    whose PCB designator is its first element.
    """
    nets: list[str] = []

    def net_id(name):
        if name is None:
            return None
        if name not in nets:
            nets.append(name)
        return nets.index(name)

    for name in extra_nets:
        net_id(name)
    components = []
    primitives = []
    for index, part in enumerate(parts):
        designator, footprint, pads = part[:3]
        uid = part[3] if len(part) > 3 else None
        components.append(PcbComponent(
            index=index, refdes=designator, source_uid_tail=uid, channel_path=None,
            x=100.0 * index, y=0.0, rotation=0.0, side="top",
            footprint=footprint, designator=designator,
        ))
        for offset, (pad, net) in enumerate(pads.items()):
            primitives.append(_pad(
                net=net_id(net), component=index, x=100.0 * index + 10 * offset,
                y=0.0, name=pad,
            ))
    for pair in pairs:
        net_id(pair[1])
        net_id(pair[2])
    for row, (net, length) in enumerate((lengths or {}).items()):
        if length:
            primitives.append(_track(
                net=net_id(net), layer=1, x1=0.0, y1=500.0 + 20 * row,
                x2=float(length), y2=500.0 + 20 * row,
            ))
    return PcbData(
        board=_board(),
        nets=nets,
        components=components,
        primitives=primitives,
        pours=[],
        layer_name_to_v6=dict(LAYERS),
        rules=list(rules),
        classes=list(classes),
        diff_pairs=[DiffPair(name, positive, negative, None) for name, positive, negative in pairs],
        xsignals=list(xsignals),
    )


def _netlist(parts, info=None):
    """Netlist {components, pin_to_net, nets} for (refdes, {pad: (pin name, net)})."""
    components = {}
    pin_to_net: dict = defaultdict(dict)
    nets: dict = defaultdict(list)
    for refdes, pins in parts:
        entry = {"pins": {}, "value": "", "mpn": "", "description": ""}
        entry.update((info or {}).get(refdes, {}))
        for pad, (name, net) in pins.items():
            entry["pins"][pad] = {"name": name, "net": net}
            pin_to_net[refdes][pad] = net
            nets[net].append((refdes, pad))
        components[refdes] = entry
    return {"components": components, "pin_to_net": dict(pin_to_net), "nets": dict(nets)}


def _issues(result, check=None):
    return [
        (finding["severity"], finding["issue"], finding["pair"])
        for finding in result["findings"]
        if check is None or finding["check"] == check
    ]


# ---------- tokenizer, values, hints ----------


@pytest.mark.parametrize(
    "first, second, expected",
    [
        ("TXP", "TXN", ("+", "P/N")),
        ("RX0N", "RX0P", ("-", "P/N")),
        ("IO_L01P_0", "IO_L01N_0", ("+", "P/N")),
        ("D+", "D-", ("+", "+/-")),
        ("DP", "DM", ("+", "P/M")),
        ("DM", "DP", ("-", "P/M")),
        ("USB_DP", "USB_DN", ("+", "P/N")),
        ("CK_T", "CK_C", ("+", "T/C")),
        ("CANH", "CANL", ("+", "H/L")),
        ("CAN_L", "CAN_H", ("-", "H/L")),
        ("IN+", "IN-", ("+", "+/-")),
        ("POS", "NEG", ("+", "POS/NEG")),
        ("VPLUS", "VMINUS", ("+", "PLUS/MINUS")),
        ("PCIE_TX0_P", "pcie_tx0_n", ("+", "P/N")),
        ("T\\X\\P\\", "T\\X\\N\\", ("+", "P/N")),
        ("IN+", "IN\u2212", ("+", "+/-")),
    ],
)
def test_complement_matches_polarity_pairs(first, second, expected):
    assert complement(first, second) == expected


@pytest.mark.parametrize(
    "first, second",
    [
        ("RESET", "RESETN"),
        ("CS", "CSN"),
        ("LED_A", "LED_K"),
        ("SDA", "SCL"),
        ("TXP", "TXP"),
        ("TXP", "RXN"),
        ("", "TXN"),
        (None, "TXN"),
        ("1", "2"),
    ],
)
def test_complement_rejects_non_pairs(first, second):
    assert complement(first, second) is None


def test_polarity_tokens_letter_rules():
    assert polarity_tokens("TXP") == [("TX{}", "+", "P")]
    # T / C only after '_', M only after 'D' or '_', letters need a boundary after.
    assert polarity_tokens("CLKT") == []
    assert polarity_tokens("COMP") == [("COM{}", "+", "P")]
    assert polarity_tokens("SUM_M") == [("SUM_{}", "-", "M")]
    assert polarity_tokens("PNP") == [("PN{}", "+", "P")]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("100n", 1e-7),
        ("100nF", 1e-7),
        ("0.1uF", 1e-7),
        ("0.1µF", 1e-7),
        ("100nF/16V", 1e-7),
        ("100nF 16V X7R", 1e-7),
        ("4K7", 4700.0),
        ("4k7", 4700.0),
        ("49R9", 49.9),
        ("49.9", 49.9),
        ("0R", 0.0),
        ("1M", 1e6),
        ("10uH", 1e-5),
        ("0,1uF", 1e-7),
        ("100nF,16V", 1e-7),
        # A space between number and prefix, upper-case prefixes before a unit.
        ("100 nF", 1e-7),
        ("100 pF", 1e-10),
        ("0.1 uF", 1e-7),
        ("10 kΩ", 1e4),
        ("4.7 k", 4700.0),
        ("100 nF 16V", 1e-7),
        ("100NF", 1e-7),
        ("10UF", 1e-5),
        ("100 NF", 1e-7),
        ("22 R", 22.0),
    ],
)
def test_parse_value(raw, expected):
    assert parse_value(raw) == pytest.approx(expected, rel=1e-9, abs=0)


@pytest.mark.parametrize("raw", ["GRM155R71C104KA88D", "", None, "CAP", "0402B104K160NT"])
def test_parse_value_rejects_part_numbers(raw):
    assert parse_value(raw) is None


def test_name_classes():
    assert is_auto_net_name("NetC544_2")
    assert is_auto_net_name("NetBT1_P")
    assert not is_auto_net_name("NETWORK")
    assert not is_auto_net_name("PCIE_TX0_P")
    for name in ("GND", "3V3", "+5V", "VBAT+", "VCC_IO", "1V8_A", "V+"):
        assert is_supply_name(name), name
    for name in ("PCIE_TX0_P", "USB_DP", "CANH", "RX3N"):
        assert not is_supply_name(name), name


@pytest.mark.parametrize(
    "names, pins, description, interface, tolerated",
    [
        (["X_P", "X_N"], ["PETP0", "PETN0"], "", "PCI Express", True),
        (["X_P", "X_N"], ["TXP", "TXN"], "USB 3.0 hub", "USB 3 SuperSpeed", True),
        (["X_P", "X_N"], ["SSRXP", "SSRXN"], "", "USB 3 SuperSpeed", True),
        (["USB3_TX_P", "USB3_TX_N"], ["TXP", "TXN"], "", "USB 3 SuperSpeed", True),
        (["X_P", "X_N"], ["MDI0+", "MDI0-"], "", "Ethernet MDI", True),
        (["X_P", "X_N"], ["D+", "D-"], "", "USB 2.0", False),
        (["USB_D_P", "USB_D_N"], ["DP", "DM"], "", "USB 2.0", False),
        # USB 2 pins of a PCIe-to-USB controller are not PCIe lanes.
        (["X_P", "X_N"], ["DP", "DM"], "PCIe to USB 3.0 host controller", "USB 2.0", False),
        (["X_P", "X_N"], ["CANH", "CANL"], "", "CAN", False),
        (["X_P", "X_N"], ["TMDS_D0+", "TMDS_D0-"], "", "HDMI / TMDS", False),
        (["X_P", "X_N"], ["IO_L01P_0", "IO_L01N_0"], "", "LVDS / MIPI / FPGA I/O", None),
        (["PCIE_REFCLK_P", "PCIE_REFCLK_N"], ["CLKP", "CLKN"], "", "clock", None),
        # "clock" in a description does not make the pair a clock.
        (["X_P", "X_N"], ["TXP", "TXN"], "PCIe switch with clock buffer", "PCI Express", True),
        (["X_P", "X_N"], ["XP", "XN"], "", None, None),
        # SGMII / SerDes lanes of an Ethernet PHY are not MDI.
        (["X_P", "X_N"], ["S_IN+", "S_IN-"], "88E1111 Gigabit Ethernet Transceiver",
         "SGMII / 1000BASE-X / SerDes", None),
        (["SGMII_TX_P", "SGMII_TX_N", "SGMII_TX"], ["S_OUTN", "S_OUTP"],
         "Integrated 10/100/1000 Mbps Ethernet Transceiver", "SGMII / 1000BASE-X / SerDes", None),
        (["QSGMII_RXN", "QSGMII_RXP"], ["P1", "P2"], "Ethernet switch",
         "SGMII / 1000BASE-X / SerDes", None),
        (["X_P", "X_N"], ["XP", "XN"], "1000BASE-X SFP transceiver",
         "SGMII / 1000BASE-X / SerDes", None),
        # LAN8720-style 10/100 PHY: TXP/TXN on an Ethernet part are MDI pins.
        (["X_P", "X_N"], ["TXP", "TXN"], "Ethernet PHY", "Ethernet MDI", True),
        # MDI pins named by port and pair letter on a gigabit switch module.
        (["ETX_P", "ETX_N"], ["P0A_N", "P0A_P"], "4 port 1G Ethernet switch module",
         "Ethernet MDI", True),
    ],
)
def test_interface_hint(names, pins, description, interface, tolerated):
    hint = interface_hint(names, pins, description)
    assert hint["interface"] == interface
    assert hint["inversion_tolerated"] is tolerated
    assert hint["hint"]


# ---------- naming ----------


def _naming_board():
    parts = [
        ("U1", "QFN", {
            "1": "X_P", "2": "X_N", "3": "Y_P", "4": "Y_N", "5": "D+_1", "6": "D-_1",
            "7": "NetBT1_P", "8": "NetBT1_N", "9": "BAT+", "10": "BAT-",
            "11": "3V3_SENSE+", "12": "3V3_SENSE-", "13": "Z_P", "14": "Z_N",
            "15": "A_P", "16": "A_N", "17": "B_N", "18": "CLK_A", "19": "CLK_B",
        }),
    ]
    pairs = [
        ("X", "X_P", "X_N"),
        ("Z", "Z_N", "Z_P"),          # legs swapped
        ("W", "W_P", "W_GONE"),       # W_GONE is added below, then removed
        ("A", "A_P", "A_N"),
        ("A2", "A_P", "B_N"),         # A_P in two pairs
        ("CLK", "CLK_A", "CLK_B"),    # no polarity suffix
    ]
    data = _build(parts, pairs)
    data.nets.remove("W_GONE")
    return data


def test_naming_undeclared_candidates_are_tiered_and_filtered():
    index = PcbIndex(_naming_board(), None)
    result = check_diff_pairs(index, None, checks=["naming"])
    summary = result["summary"]["naming"]
    assert summary["undeclared_warning"] == 1
    assert summary["undeclared_info"] == 1
    assert summary["suppressed_auto_named"] == 1
    assert summary["suppressed_supply_or_sense"] == 2
    assert summary["declared"] == 2  # X, A
    assert summary["declared_inverted"] == 1  # Z
    undeclared = {
        (finding["details"]["positive_net"], finding["details"]["negative_net"]):
        (finding["severity"], finding["details"]["style"])
        for finding in result["findings"]
        if finding["issue"] == "undeclared_pair"
    }
    assert undeclared == {
        ("Y_P", "Y_N"): ("warning", "P/N"),
        ("D+_1", "D-_1"): ("info", "+/-"),
    }


def test_naming_flags_pcb_pair_conventions():
    index = PcbIndex(_naming_board(), None)
    result = check_diff_pairs(index, None, checks=["naming"])
    issues = _issues(result, "naming")
    assert ("warning", "pair_polarity_inverted", "Z") in issues
    assert ("warning", "pair_leg_missing", "W") in issues
    assert ("info", "pair_names_unconventional", "CLK") in issues
    shared = [f for f in result["findings"] if f["issue"] == "net_in_multiple_pairs"]
    assert len(shared) == 1
    assert shared[0]["details"] == {"net": "A_P", "pairs": ["A", "A2"]}
    summary = result["summary"]["naming"]
    assert summary["inverted"] == 1
    assert summary["missing_legs"] == 1
    assert summary["unconventional"] == 2  # CLK, A2 (W is missing a leg)
    assert summary["nets_in_multiple_pairs"] == 1
    # The B_N / A_P candidate conflicts with pair A; A_P/A_N is declared.
    assert all(f["issue"] != "undeclared_pair" or "B_N" not in str(f) for f in result["findings"])


def test_naming_rail_pairs_are_suppressed():
    # VP / VN with 20 pads per leg: analog rails, not an undeclared pair.
    parts = [(f"C{index}", "0402", {"1": "VP", "2": "VN"}) for index in range(1, 21)]
    parts.append(("U1", "QFN", {"1": "MOD_P", "2": "MOD_N"}))
    result = check_diff_pairs(PcbIndex(_build(parts), None), None, checks=["naming"])
    summary = result["summary"]["naming"]
    assert summary["suppressed_rail"] == 1
    assert _issues(result) == [("warning", "undeclared_pair", None)]
    assert result["findings"][0]["details"]["positive_net"] == "MOD_P"


@pytest.mark.parametrize("positive, negative", [
    ("MOTOR_A_P", "MOTOR_A_N"), ("SPK_P", "SPK_N"), ("LED1_P", "LED1_N"),
    ("HEATER_P", "HEATER_N"), ("TEC_P", "TEC_N"),
])
def test_naming_load_pairs_are_info(positive, negative):
    parts = [("U1", "QFN", {"1": positive, "2": negative}),
             ("J1", "HDR", {"1": positive, "2": negative})]
    result = check_diff_pairs(PcbIndex(_build(parts), None), None, checks=["naming"])
    assert _issues(result) == [("info", "undeclared_pair", None)]
    assert result["findings"][0]["details"]["load_or_actuator"] is True


def test_naming_pattern_limits_pairs_and_candidates():
    index = PcbIndex(_naming_board(), None)
    result = check_diff_pairs(index, None, checks=["naming"], pattern="^Y_")
    assert result["pairs_checked"] == 0
    assert _issues(result) == [("warning", "undeclared_pair", None)]


# ---------- polarity ----------


def _polarity_case(u1_pins, info=None, sch_name="U1", uid=None, extra_parts=()):
    """Pair X (X_P / X_N) through U1; u1_pins {pad: (pin name, net)}."""
    pads = {pad: net for pad, (_, net) in u1_pins.items()}
    parts = [("U1", "QFN", pads, uid), *[(d, f, p) for d, f, p, _ in extra_parts]]
    data = _build(parts, [("X", "X_P", "X_N")])
    netlist_parts = [(sch_name, u1_pins), *[(d, pins) for d, _, _, pins in extra_parts]]
    netlist = _netlist(netlist_parts, info)
    if uid:
        netlist["components"][sch_name]["unique_id"] = uid
    return PcbIndex(data, netlist), netlist


def test_polarity_swapped_pins_warn_without_a_tolerant_interface():
    index, netlist = _polarity_case({"A1": ("TXN", "X_P"), "A2": ("TXP", "X_N")})
    result = check_diff_pairs(index, netlist, checks=["polarity"])
    summary = result["summary"]["polarity"]
    assert summary["judged"] == 1
    assert summary["swapped"] == 1
    finding = result["findings"][0]
    assert finding["severity"] == "warning"
    assert finding["issue"] == "swapped_at_pins"
    assert finding["details"]["positive_net"] == {"net": "X_P", "pin": "A1", "pin_name": "TXN"}
    assert finding["details"]["negative_net"] == {"net": "X_N", "pin": "A2", "pin_name": "TXP"}
    assert finding["details"]["inversion_tolerated"] is None


def test_polarity_correct_pins_are_ok():
    index, netlist = _polarity_case({"A1": ("RX0P", "X_P"), "A2": ("RX0N", "X_N")})
    result = check_diff_pairs(index, netlist, checks=["polarity"])
    assert result["summary"]["polarity"]["ok"] == 1
    assert result["findings"] == []


def test_polarity_swap_on_usb3_part_is_info():
    index, netlist = _polarity_case(
        {"A1": ("TXN", "X_P"), "A2": ("TXP", "X_N")},
        info={"U1": {"description": "USB 3.0 hub controller"}},
    )
    result = check_diff_pairs(index, netlist, checks=["polarity"])
    finding = result["findings"][0]
    assert finding["severity"] == "info"
    assert finding["details"]["interface"] == "USB 3 SuperSpeed"
    assert finding["details"]["inversion_tolerated"] is True
    assert result["summary"]["polarity"]["swapped_inversion_tolerated"] == 1


def test_polarity_swap_on_usb2_pins_is_warning():
    index, netlist = _polarity_case({"1": ("D-", "X_P"), "2": ("D+", "X_N")})
    finding = check_diff_pairs(index, netlist, checks=["polarity"])["findings"][0]
    assert finding["severity"] == "warning"
    assert finding["details"]["interface"] == "USB 2.0"
    assert finding["details"]["inversion_tolerated"] is False


def test_polarity_numeric_pins_are_unjudged():
    index, netlist = _polarity_case(
        {"A1": ("RX0P", "X_P"), "A2": ("RX0N", "X_N")},
        extra_parts=[("J1", "HDR", {"1": "X_P", "2": "X_N"},
                      {"1": ("1", "X_P"), "2": ("2", "X_N")})],
    )
    summary = check_diff_pairs(index, netlist, checks=["polarity"])["summary"]["polarity"]
    assert summary["incidences"] == 2
    assert summary["judged"] == 1
    assert summary["unjudged"] == 1


def test_polarity_uses_sch_refdes_when_pcb_designator_differs():
    index, netlist = _polarity_case(
        {"A1": ("TXN", "X_P"), "A2": ("TXP", "X_N")}, sch_name="U7", uid="UID1",
    )
    assert index.find_components("U1")[0]["sch_refdes"] == "U7"
    result = check_diff_pairs(index, netlist, checks=["polarity"])
    finding = result["findings"][0]
    assert finding["details"]["component"] == "U1"
    assert finding["details"]["sch_refdes"] == "U7"


def test_polarity_dnp_part_is_info():
    index, netlist = _polarity_case({"A1": ("TXN", "X_P"), "A2": ("TXP", "X_N")})
    result = check_diff_pairs(index, netlist, is_dnp=lambda refdes: refdes == "U1",
                              checks=["polarity"])
    finding = result["findings"][0]
    assert finding["severity"] == "info"
    assert finding["details"]["dnp"] is True


def test_polarity_without_pin_names_is_not_checked():
    index, _ = _polarity_case({"A1": ("TXN", "X_P"), "A2": ("TXP", "X_N")})
    result = check_diff_pairs(index, None, checks=["polarity"])
    assert "polarity" not in result["summary"]
    assert [entry["check"] for entry in result["not_checked"]] == ["polarity"]


def _usb_polarity(extra_parts, info=None, pairs=(("USB", "USB_DP", "USB_DM"),)):
    """USB pair from U1 (USB_DP / USB_DM pins) to J1, plus extra parts
    [(designator, {pad: (pin name, net)})]."""
    netlist_parts = [
        ("U1", {"1": ("USB_DP", "USB_DP"), "2": ("USB_DM", "USB_DM")}),
        ("J1", {"3": ("D+", "USB_DP"), "2": ("D-", "USB_DM"), "1": ("VBUS", "VBUS")}),
        *extra_parts,
    ]
    parts = [
        (designator, "FP", {pad: net for pad, (_, net) in pins.items()})
        for designator, pins in netlist_parts
    ]
    netlist = _netlist(netlist_parts, info)
    return PcbIndex(_build(parts, pairs), netlist), netlist


def test_polarity_shunt_esd_part_is_not_judged():
    # A 2-channel USB ESD array wired D+ to pin 'D-': a shunt to ground, so
    # its pin order does not change link polarity.
    esd = ("U5", {"1": ("D+", "USB_DM"), "2": ("D-", "USB_DP"), "3": ("GND", "GND")})
    index, netlist = _usb_polarity(
        [esd], {"U5": {"description": "2-channel ESD protection for USB 2.0"}}
    )
    result = check_diff_pairs(index, netlist, checks=["polarity"])
    assert result["findings"] == []
    summary = result["summary"]["polarity"]
    assert summary["unjudged_shunt"] == 1
    assert summary["swapped"] == 0
    # A D-prefixed diode array is a shunt without a description too.
    diode = ("D7", {"1": ("IO+", "USB_DM"), "2": ("IO-", "USB_DP"), "3": ("GND", "GND")})
    index, netlist = _usb_polarity([diode])
    assert check_diff_pairs(index, netlist, checks=["polarity"])["findings"] == []


def _mux(common, side):
    """USB 2.0 mux U3: common pins D+ / D- and switched pins 1D+ / 1D-."""
    return ("U3", {
        "1": ("D+", common[0]), "2": ("D-", common[1]),
        "3": ("1D+", side[0]), "4": ("1D-", side[1]), "5": ("GND", "GND"),
    })


def test_polarity_pass_through_mux_consistent_swap_is_harmless():
    mux = _mux(("USB_DM", "USB_DP"), ("USB1_DM", "USB1_DP"))  # swapped on both sides
    pairs = (("USB", "USB_DP", "USB_DM"), ("USB1", "USB1_DP", "USB1_DM"))
    index, netlist = _usb_polarity(
        [mux], {"U3": {"description": "USB 2.0 analog switch multiplexer"}}, pairs
    )
    result = check_diff_pairs(index, netlist, checks=["polarity"])
    assert result["findings"] == []
    assert result["summary"]["polarity"]["pass_through_consistent"] == 2  # seen from both pairs


@pytest.mark.parametrize("declared", [True, False])
def test_polarity_pass_through_mux_crossing_warns_once(declared):
    mux = _mux(("USB_DM", "USB_DP"), ("USB1_DP", "USB1_DM"))  # swapped on one side only
    pairs = (("USB", "USB_DP", "USB_DM"),)
    if declared:
        pairs += (("USB1", "USB1_DP", "USB1_DM"),)
    index, netlist = _usb_polarity(
        [mux], {"U3": {"description": "USB 2.0 analog switch multiplexer"}}, pairs
    )
    result = check_diff_pairs(index, netlist, checks=["polarity"])
    assert _issues(result) == [("warning", "crossed_through_part", "USB")]
    details = result["findings"][0]["details"]
    assert details["other_side"]["positive_pin"]["net"] == "USB1_DP"
    assert details["interface"] == "USB 2.0"


def test_polarity_sgmii_swap_on_ethernet_phy_warns():
    index, netlist = _polarity_case(
        {"A1": ("S_OUTN", "X_P"), "A2": ("S_OUTP", "X_N")},
        info={"U1": {"description": "Integrated 10/100/1000 Mbps Ethernet Transceiver"}},
    )
    finding = check_diff_pairs(index, netlist, checks=["polarity"])["findings"][0]
    assert finding["severity"] == "warning"
    assert finding["details"]["interface"] == "SGMII / 1000BASE-X / SerDes"
    assert finding["details"]["inversion_tolerated"] is None


def test_polarity_counts_pcb_only_parts_as_unjudged():
    data = _build([("U1", "QFN", {"A1": "X_P", "A2": "X_N"})], [("X", "X_P", "X_N")])
    netlist = _netlist([("R9", {"1": ("1", "X_P")})])
    index = PcbIndex(data, netlist)
    summary = check_diff_pairs(index, netlist, checks=["polarity"])["summary"]["polarity"]
    assert summary["incidences"] == 1
    assert summary["unjudged_no_netlist_part"] == 1
    assert summary["judged"] == 0


# ---------- series parts ----------


def _series_case(positive_parts, negative_parts, far_pairs=(("X_C", "X_C_P", "X_C_N"),),
                 lengths=None, extra_parts=()):
    """Pair X with series parts: [(designator, footprint, value, far net)] per leg."""
    parts = []
    info = {}
    netlist_parts = []
    for leg_net, specs in (("X_P", positive_parts), ("X_N", negative_parts)):
        for designator, footprint, value, far in specs:
            parts.append((designator, footprint, {"1": leg_net, "2": far}))
            info[designator] = {"value": value}
            netlist_parts.append((designator, {"1": ("1", leg_net), "2": ("2", far)}))
    parts.append(("U1", "QFN", {"A1": "X_P", "A2": "X_N", "A3": "GND"}))
    netlist_parts.append(
        ("U1", {"A1": ("TXP", "X_P"), "A2": ("TXN", "X_N"), "A3": ("GND", "GND")})
    )
    for designator, footprint, pads in extra_parts:
        parts.append((designator, footprint, pads))
        netlist_parts.append((designator, {pad: (pad, net) for pad, net in pads.items()}))
    data = _build(parts, [("X", "X_P", "X_N"), *far_pairs], lengths=lengths)
    netlist = _netlist(netlist_parts, info)
    return PcbIndex(data, netlist), netlist


def test_series_symmetric_caps_with_equal_values_are_clean():
    index, netlist = _series_case(
        [("C1", "0402", "100nF", "X_C_P")], [("C2", "0402", "0.1uF", "X_C_N")]
    )
    result = check_diff_pairs(index, netlist, is_dnp=lambda refdes: False, checks=["series"])
    summary = result["summary"]["series"]
    # X sees the caps toward X_C and X_C sees them toward X.
    assert summary["pairs_with_series"] == 2
    assert summary["capacitor_coupled"] == 2
    assert summary["far_pair_ok"] == 2
    assert summary["asymmetric_pairs"] == 0
    assert result["findings"] == []


def test_series_cap_on_one_leg_only():
    index, netlist = _series_case([("C1", "0402", "100nF", "X_C_P")], [])
    result = check_diff_pairs(index, netlist, checks=["series"])
    assert _issues(result) == [("warning", "ac_coupling_asymmetric", "X")]
    finding = result["findings"][0]
    assert [part["refdes"] for part in finding["details"]["positive"]] == ["C1"]
    assert finding["details"]["negative"] == []
    # Seen again from the coupled pair X_C, reported once.
    assert finding["details"]["also_pairs"] == ["X_C"]


def test_series_value_mismatch():
    index, netlist = _series_case(
        [("C1", "0402", "100n", "X_C_P")], [("C2", "0402", "220n", "X_C_N")]
    )
    result = check_diff_pairs(index, netlist, checks=["series"])
    assert _issues(result) == [("warning", "series_value_mismatch", "X")]


def test_series_values_fall_back_to_text():
    index, netlist = _series_case(
        [("C1", "0402", "GRM155R71C104KA88D", "X_C_P")],
        [("C2", "0402", "GRM155R71C104KA88D", "X_C_N")],
    )
    assert check_diff_pairs(index, netlist, checks=["series"])["findings"] == []
    index, netlist = _series_case(
        [("C1", "0402", "GRM155R71C104KA88D", "X_C_P")],
        [("C2", "0402", "GRM155R71C224KA88D", "X_C_N")],
    )
    result = check_diff_pairs(index, netlist, checks=["series"])
    assert _issues(result) == [("warning", "series_value_mismatch", "X")]


def test_series_footprint_mismatch():
    index, netlist = _series_case(
        [("C1", "0402", "100nF", "X_C_P")], [("C2", "0603", "100nF", "X_C_N")]
    )
    result = check_diff_pairs(index, netlist, checks=["series"])
    assert _issues(result) == [("warning", "series_footprint_mismatch", "X")]


def test_series_dnp_on_one_leg():
    index, netlist = _series_case(
        [("C1", "0402", "100nF", "X_C_P")], [("C2", "0402", "100nF", "X_C_N")]
    )
    result = check_diff_pairs(index, netlist, is_dnp=lambda refdes: refdes == "C2",
                              checks=["series"])
    assert _issues(result) == [("warning", "series_dnp_mismatch", "X")]


def test_series_common_node_termination_is_not_series():
    index, netlist = _series_case(
        [("R1", "0402", "49.9", "CT")], [("R2", "0402", "49.9", "CT")], far_pairs=(),
    )
    result = check_diff_pairs(index, netlist, checks=["series"])
    summary = result["summary"]["series"]
    assert summary["common_node_pairs"] == 1
    assert summary["pairs_with_series"] == 0
    assert result["findings"] == []


def test_series_far_pair_crossed():
    index, netlist = _series_case(
        [("C1", "0402", "100nF", "X_C_N")], [("C2", "0402", "100nF", "X_C_P")]
    )
    result = check_diff_pairs(index, netlist, checks=["series"])
    assert _issues(result) == [("warning", "coupled_side_inverted", "X")]
    assert result["findings"][0]["details"]["far_pair"] == "X_C"
    assert result["summary"]["series"]["far_pair_crossed"] == 2  # seen from X and X_C


def test_series_coupled_side_not_a_pair():
    index, netlist = _series_case(
        [("C1", "0402", "100nF", "Q_P")], [("C2", "0402", "100nF", "Q_N")],
        far_pairs=(), lengths={"Q_P": 900, "Q_N": 880},
    )
    result = check_diff_pairs(index, netlist, checks=["series"])
    assert _issues(result) == [("info", "coupled_side_not_a_pair", "X")]
    finding = result["findings"][0]
    assert finding["details"]["named_like_pair"] is True
    assert finding["details"]["named_polarity"] == "ok"
    assert finding["far_routed_mil"] == [900.0, 880.0]


def test_series_short_auto_named_stub_is_suppressed():
    index, netlist = _series_case(
        [("C1", "0402", "100nF", "NetC1_2")], [("C2", "0402", "100nF", "NetC2_2")],
        far_pairs=(), lengths={"NetC1_2": 60, "NetC2_2": 45},
    )
    result = check_diff_pairs(index, netlist, checks=["series"])
    assert result["findings"] == []
    assert result["summary"]["series"]["coupled_side_not_pair_suppressed"] == 1


def test_series_ignores_supply_far_nets_and_rails():
    rail_pads = {str(pin): "BUS" for pin in range(1, 15)}
    index, netlist = _series_case(
        [("C1", "0402", "100nF", "VCC")], [("R2", "0402", "10k", "BUS")],
        far_pairs=(), extra_parts=[("U9", "BGA", rail_pads)],
    )
    result = check_diff_pairs(index, netlist, checks=["series"])
    assert result["summary"]["series"]["pairs_with_series"] == 0
    assert result["findings"] == []


def _series_board(parts, pairs, info=None, dnp=None, lengths=None):
    """check_diff_pairs(series) on parts [(designator, {pad: net})] with
    numeric pin names; info {refdes: {"value": ...}}."""
    data = _build([(designator, "0402", pads) for designator, pads in parts], pairs,
                  lengths=lengths)
    netlist = _netlist(
        [(designator, {pad: (pad, net) for pad, net in pads.items()}) for designator, pads in parts],
        info,
    )
    return check_diff_pairs(PcbIndex(data, netlist), netlist, is_dnp=dnp, checks=["series"])


@pytest.mark.parametrize("driver", [
    # Transistor-switched 1.5k pull-up (USB soft connect).
    [("Q1", {"3": "NetQ1_3", "2": "3V3", "1": "USB_PU_EN"})],
    # GPIO-driven pull-up from the MCU.
    [("U2", {"7": "USB_PU", "8": "GND", "9": "SDA"})],
])
def test_series_usb_soft_connect_pull_up_is_a_shunt(driver):
    far = driver[0][1].get("3") or driver[0][1]["7"]
    parts = [
        ("U1", {"1": "USB_DP", "2": "USB_DM", "3": "GND"}),
        ("J1", {"3": "USB_DP", "2": "USB_DM", "1": "VBUS", "4": "GND"}),
        ("R5", {"1": "USB_DP", "2": far}),
        *driver,
    ]
    result = _series_board(parts, [("USB", "USB_DP", "USB_DM")], {"R5": {"value": "1k5"}})
    assert _issues(result) == [("info", "shunt_part_one_leg", "USB")]
    assert result["findings"][0]["details"]["part"]["refdes"] == "R5"
    summary = result["summary"]["series"]
    assert summary["shunt_parts"] == 1
    assert summary["pairs_with_series"] == 0
    assert summary["asymmetric_pairs"] == 0


def test_series_isolated_rs485_bias_is_a_shunt():
    # A pulled up to VISO (not a supply name the regex knows), B to GND_ISO.
    parts = [
        ("U1", {"12": "RS485_A", "13": "RS485_B", "16": "VISO", "9": "GND_ISO"}),
        ("J1", {"1": "RS485_A", "2": "RS485_B", "3": "GND_ISO"}),
        ("R1", {"1": "RS485_A", "2": "VISO"}),
        ("R2", {"1": "RS485_B", "2": "GND_ISO"}),
        ("C1", {"1": "VISO", "2": "GND_ISO"}),
        ("C2", {"1": "VISO", "2": "GND_ISO"}),
    ]
    result = _series_board(parts, [("RS485", "RS485_A", "RS485_B")], {"R1": {"value": "680"}})
    assert [f["severity"] for f in result["findings"]] == ["info"]
    assert _issues(result) == [("info", "shunt_part_one_leg", "RS485")]


def test_series_extra_pull_up_does_not_make_a_count_mismatch():
    # 22R series resistors on both legs, a 1k5 pull-up on D+ only.
    parts = [
        ("U1", {"1": "USB_DP", "2": "USB_DM", "3": "GND"}),
        ("J1", {"3": "USB_DP_C", "2": "USB_DM_C", "1": "VBUS", "4": "GND"}),
        ("R1", {"1": "USB_DP_C", "2": "USB_DP"}),
        ("R2", {"1": "USB_DM_C", "2": "USB_DM"}),
        ("R5", {"1": "USB_DP_C", "2": "NetQ1_3"}),
        ("Q1", {"3": "NetQ1_3", "2": "3V3", "1": "USB_PU_EN"}),
    ]
    info = {"R1": {"value": "22R"}, "R2": {"value": "22R"}, "R5": {"value": "1k5"}}
    result = _series_board(
        parts, [("USB", "USB_DP", "USB_DM"), ("USB_C", "USB_DP_C", "USB_DM_C")], info
    )
    assert _issues(result) == [("info", "shunt_part_one_leg", "USB_C")]
    assert result["summary"]["series"]["far_pair_ok"] == 2


def test_series_one_leg_cap_still_warns_when_partner_reaches_the_connector():
    # TX_N runs straight to J1; TX_P goes through C1: a real asymmetry.
    parts = [
        ("U1", {"1": "TX_P", "2": "TX_N", "3": "GND"}),
        ("C1", {"1": "TX_P", "2": "NetC1_2"}),
        ("J1", {"1": "NetC1_2", "2": "TX_N", "3": "GND"}),
    ]
    result = _series_board(parts, [("TX", "TX_P", "TX_N")], {"C1": {"value": "100nF"}})
    assert _issues(result) == [("warning", "ac_coupling_asymmetric", "TX")]


@pytest.mark.parametrize("value, dnp, severity", [
    ("10k", False, "info"),   # a bias resistor, not a series part
    ("0R", True, "info"),     # DNP in the active variant
    ("0R", False, "warning"),
])
def test_series_one_leg_part_downgrades(value, dnp, severity):
    parts = [
        ("U1", {"1": "TX_P", "2": "TX_N", "3": "GND"}),
        ("R1", {"1": "TX_P", "2": "NetR1_2"}),
        ("J1", {"1": "NetR1_2", "2": "TX_N", "3": "GND"}),
    ]
    result = _series_board(parts, [("TX", "TX_P", "TX_N")], {"R1": {"value": value}},
                           dnp=lambda refdes: dnp and refdes == "R1")
    assert _issues(result) == [(severity, "series_part_one_leg_only", "TX")]
    details = result["findings"][0]["details"]
    assert ("likely_not_series" in details) is (severity == "info")


def test_series_far_side_is_checked_despite_an_extra_dnp_part():
    # C3 is a DNP alternate of C1; the crossed coupled side is still reported.
    parts = [
        ("U1", {"1": "X_P", "2": "X_N", "3": "GND"}),
        ("C1", {"1": "X_P", "2": "X_C_N"}),
        ("C3", {"1": "X_P", "2": "X_C_N"}),
        ("C2", {"1": "X_N", "2": "X_C_P"}),
        ("J1", {"1": "X_C_P", "2": "X_C_N", "3": "GND"}),
    ]
    info = {name: {"value": "100nF"} for name in ("C1", "C2", "C3")}
    result = _series_board(parts, [("X", "X_P", "X_N"), ("X_C", "X_C_P", "X_C_N")], info,
                           dnp=lambda refdes: refdes == "C3")
    issues = _issues(result)
    assert ("warning", "coupled_side_inverted", "X") in issues
    assert ("info", "series_count_mismatch", "X") in issues
    assert not [issue for issue in issues if issue[0] == "warning" and issue[1] != "coupled_side_inverted"]


@pytest.mark.parametrize("values, expected", [
    (("100 nF", "100 pF"), [("warning", "series_value_mismatch", "X")]),
    (("100 nF", "100nF"), []),
    (("100nF", "100NF"), []),
    (("0.1 uF", "100n"), []),
    (("CAP 0402", "CAP0402"), []),
])
def test_series_values_with_spaces_and_upper_case(values, expected):
    index, netlist = _series_case(
        [("C1", "0402", values[0], "X_C_P")], [("C2", "0402", values[1], "X_C_N")]
    )
    assert _issues(check_diff_pairs(index, netlist, checks=["series"])) == expected


def _crossed_board(far_positive, far_negative, pairs, extra=()):
    parts = [
        ("U1", {"1": pairs[0][1], "2": pairs[0][2], "3": "GND"}),
        ("C1", {"1": pairs[0][1], "2": far_positive}),
        ("C2", {"1": pairs[0][2], "2": far_negative}),
        ("J1", {"1": far_positive, "2": far_negative, "3": "GND"}),
        *extra,
    ]
    info = {"C1": {"value": "100nF"}, "C2": {"value": "100nF"}}
    return _series_board(parts, pairs, info, lengths={far_positive: 800, far_negative: 800})


@pytest.mark.parametrize("pair, far", [
    (("TX", "TX_P", "TX_N"), ("TX_C_N", "TX_C_P")),
    (("A", "A+", "A-"), ("B-", "B+")),
])
def test_series_crossed_undeclared_coupled_side_warns(pair, far):
    result = _crossed_board(*far, [pair])
    assert _issues(result) == [("warning", "coupled_side_crossed_by_name", pair[0])]
    finding = result["findings"][0]
    assert "crossed" in finding["message"]
    assert result["summary"]["series"]["coupled_side_crossed_by_name"] == 1


def _double_swap(declare_far):
    parts = [
        ("U1", {"1": "TX0_P", "2": "TX0_N", "3": "TX1_P", "4": "TX1_N", "5": "GND"}),
        ("C1", {"1": "TX0_P", "2": "TX0_C_P"}), ("C2", {"1": "TX0_N", "2": "TX1_C_N"}),
        ("C3", {"1": "TX1_P", "2": "TX1_C_P"}), ("C4", {"1": "TX1_N", "2": "TX0_C_N"}),
        ("J1", {"1": "TX0_C_P", "2": "TX0_C_N", "3": "TX1_C_P", "4": "TX1_C_N", "5": "GND"}),
    ]
    pairs = [("TX0", "TX0_P", "TX0_N"), ("TX1", "TX1_P", "TX1_N")]
    if declare_far:
        pairs += [("TX0_C", "TX0_C_P", "TX0_C_N"), ("TX1_C", "TX1_C_P", "TX1_C_N")]
    info = {name: {"value": "100nF"} for name in ("C1", "C2", "C3", "C4")}
    return _series_board(parts, pairs, info)


def test_series_cross_pair_miswire_with_declared_far_pairs_warns():
    result = _double_swap(declare_far=True)
    issues = _issues(result)
    assert {issue[0] for issue in issues} == {"warning"}
    assert sorted(issue[2] for issue in issues if issue[1] == "coupled_side_cross_pair") == [
        "TX0", "TX0_C", "TX1", "TX1_C"
    ]
    tx0 = next(f for f in result["findings"] if f["pair"] == "TX0")
    assert "the negative leg of pair TX1_C" in tx0["message"]
    assert "not a PCB differential pair" not in tx0["message"]
    assert result["summary"]["series"]["coupled_side_not_pair"] == 0


def test_series_cross_pair_miswire_with_undeclared_far_nets_warns():
    result = _double_swap(declare_far=False)
    assert _issues(result) == [
        ("warning", "coupled_side_cross_pair", "TX0"),
        ("warning", "coupled_side_cross_pair", "TX1"),
    ]
    tx0 = result["findings"][0]
    assert "TX0_C_N" in tx0["message"]  # the name TX0_C_P is paired with


def test_series_part_across_the_pair_is_counted_not_flagged():
    index, netlist = _series_case(
        [], [], far_pairs=(), extra_parts=[("R5", "0402", {"1": "X_P", "2": "X_N"})],
    )
    result = check_diff_pairs(index, netlist, checks=["series"])
    assert result["summary"]["series"]["across_pair_parts"] == 1
    assert result["findings"] == []


# ---------- skew ----------


def _skew_case(rules, positive=100.0, negative=112.0, classes=(), xsignals=(),
               pairs=(("X", "X_P", "X_N"),), extra_nets=(), extra_parts=()):
    parts = [
        ("U1", "QFN", {"A1": "X_P", "A2": "X_N"}),
        ("J1", "HDR", {"1": "X_P", "2": "X_N"}),
        *extra_parts,
    ]
    data = _build(
        parts, pairs, lengths={"X_P": positive, "X_N": negative}, rules=rules,
        classes=classes, extra_nets=extra_nets, xsignals=xsignals,
    )
    return PcbIndex(data, None)


def _class(name, kind, members):
    return PcbClass(name=name, kind=kind, members=list(members), superclass=False,
                    auto_generated=False, unique_id=None)


def test_skew_pair_scoped_rule_over_tolerance_warns():
    index = _skew_case([_in_pair_rule("InDifferentialPair('X')")])
    result = check_diff_pairs(index, None, checks=["skew"])
    assert _issues(result) == [("warning", "in_pair_skew_exceeds_tolerance", "X")]
    finding = result["findings"][0]
    assert finding["positive_mil"] == 100.0
    assert finding["negative_mil"] == 112.0
    assert finding["skew_mil"] == 12.0
    assert finding["tolerance_mil"] == 5.0
    assert finding["excess_mil"] == 7.0
    assert finding["details"]["rule_scope"] == "pair"
    summary = result["summary"]["skew"]
    assert summary["with_in_pair_rule"] == 1
    assert summary["pair_scoped"] == 1
    assert summary["over_tolerance"] == 1
    assert summary["in_pair_rules"][0]["scope_kind"] == "pair"
    assert summary["in_pair_rules"][0]["max_skew_mil"] == 12.0


def test_skew_within_tolerance_has_no_finding():
    index = _skew_case([_in_pair_rule("InDifferentialPair('X')")], negative=104.0)
    result = check_diff_pairs(index, None, checks=["skew"])
    assert result["findings"] == []
    assert result["summary"]["skew"]["within_tolerance"] == 1


def test_skew_net_scoped_rule_warns_with_note():
    # Same verdict as check_lengths (a violation), with a note on the scope.
    index = _skew_case(
        [_in_pair_rule("InNetClass('HS')")], classes=[_class("HS", "net", ["X_P", "X_N"])],
    )
    result = check_diff_pairs(index, None, checks=["skew"])
    assert _issues(result) == [("warning", "in_pair_skew_exceeds_tolerance", "X")]
    details = result["findings"][0]["details"]
    assert details["rule_scope"] == "net"
    assert "InDifferentialPair" in details["note"]
    summary = result["summary"]["skew"]
    assert summary["net_scoped_only"] == 1
    assert summary["net_scoped_violations"] == 1
    assert summary["over_tolerance"] == 1


@pytest.mark.parametrize(
    "scope",
    [
        "InDifferentialPair('X') or InNetClass('HS')",
        "InDifferentialPairClass('PAIRS') or InNetClass('HS')",
        "IsDifferentialPair or InNetClass('HS')",
        "InNetClass('HS') or (ObjectKind = 'Differential Pair')",
    ],
)
def test_skew_pair_term_or_net_term_is_pair_scoped(scope):
    index = _skew_case(
        [_in_pair_rule(scope)],
        classes=[_class("HS", "net", ["X_P", "X_N"]), _class("PAIRS", "diff_pair", ["X"])],
    )
    result = check_diff_pairs(index, None, checks=["skew"])
    assert _issues(result) == [("warning", "in_pair_skew_exceeds_tolerance", "X")]
    details = result["findings"][0]["details"]
    assert details["rule_scope"] == "pair"
    assert "note" not in details
    assert result["summary"]["skew"]["pair_scoped"] == 1


def test_skew_pair_term_that_misses_the_pair_stays_net_scoped():
    # Y is not selected by its pair term; only the net class selects it.
    index = _skew_case(
        [_in_pair_rule("InDifferentialPair('X') or InNetClass('HS')")],
        pairs=(("X", "X_P", "X_N"), ("Y", "Y_P", "Y_N")),
        classes=[_class("HS", "net", ["X_P", "X_N", "Y_P", "Y_N"])],
        extra_parts=[("U2", "QFN", {"1": "Y_P", "2": "Y_N"}),
                     ("J2", "HDR", {"1": "Y_P", "2": "Y_N"})],
    )
    result = check_diff_pairs(index, None, checks=["skew"], pattern="^Y")
    rule = result["summary"]["skew"]["in_pair_rules"][0]
    assert rule["scope_kind"] == "net"


def test_skew_all_scoped_rule_counts_as_pair_scoped():
    index = _skew_case([_in_pair_rule("All")])
    result = check_diff_pairs(index, None, checks=["skew"])
    assert _issues(result) == [("warning", "in_pair_skew_exceeds_tolerance", "X")]
    assert result["findings"][0]["details"]["rule_scope"] == "all"


def test_skew_stale_rule_warns():
    index = _skew_case([
        _in_pair_rule("InDifferentialPairClass('GONE')", name="ML_GONE"),
        _in_pair_rule("InNet('GND') and IsDifferentialPair", name="ML_NONE", priority=2),
    ], extra_nets=["GND"])
    result = check_diff_pairs(index, None, checks=["skew"])
    issues = _issues(result)
    assert ("warning", "in_pair_rule_stale", None) in issues
    assert ("warning", "in_pair_rule_matches_no_pair", None) in issues
    stale = next(f for f in result["findings"] if f["issue"] == "in_pair_rule_stale")
    assert stale["details"]["rule"] == "ML_GONE"
    assert stale["details"]["stale_references"] == ["indifferentialpairclass('GONE')"]
    assert result["summary"]["skew"]["stale_rules"] == 2
    assert result["summary"]["skew"]["with_in_pair_rule"] == 0


def test_skew_unrouted_leg_has_no_verdict():
    index = _skew_case([_in_pair_rule("InDifferentialPair('X')")], negative=0)
    result = check_diff_pairs(index, None, checks=["skew"])
    assert _issues(result) == [("warning", "leg_unrouted", "X")]
    finding = result["findings"][0]
    assert finding["details"]["unrouted_nets"] == ["X_N"]
    assert finding["negative_mil"] == 0.0
    summary = result["summary"]["skew"]
    assert summary["unrouted"] == 1
    assert summary["no_verdict_unrouted"] == 1
    assert summary["over_tolerance"] == 0


def test_skew_unrouted_pair_without_rule_is_info():
    index = _skew_case([], positive=0, negative=0)
    result = check_diff_pairs(index, None, checks=["skew"])
    assert _issues(result) == [("info", "pair_unrouted", "X")]


def _measured(positive, negative):
    """A pair_lengths callable returning these per-leg dicts."""
    def pair_lengths(name):
        return {"positive": dict(positive), "negative": dict(negative), "skew_mil": None}
    return pair_lengths


def test_skew_single_pad_legs_have_nothing_to_route():
    single = {"length_mil": 0.0, "status": "ok", "pad_count": 1, "copper_mil": 0.0}
    index = _skew_case([_in_pair_rule("InDifferentialPair('X')")])
    result = check_diff_pairs(index, None, checks=["skew"],
                              pair_lengths=_measured(single, single))
    assert result["findings"] == []
    summary = result["summary"]["skew"]
    assert summary["single_pad_pairs"] == 1
    assert summary["unrouted"] == 0


def test_skew_one_single_pad_leg_is_info_not_unrouted():
    routed = {"length_mil": 210.0, "status": "ok", "pad_count": 2, "copper_mil": 210.0}
    single = {"length_mil": 0.0, "status": "ok", "pad_count": 1, "copper_mil": 0.0}
    index = _skew_case([_in_pair_rule("InDifferentialPair('X')")])
    result = check_diff_pairs(index, None, checks=["skew"],
                              pair_lengths=_measured(routed, single))
    assert _issues(result) == [("info", "leg_single_pad", "X")]
    assert result["findings"][0]["details"]["single_pad_net"] == "X_N"
    summary = result["summary"]["skew"]
    assert summary["single_pad_legs"] == 1
    assert summary["no_verdict_unrouted"] == 1


def test_skew_single_pad_legs_on_routed_length_fallback():
    data = _build([("U1", "QFN", {"1": "X_P", "2": "X_N"})], [("X", "X_P", "X_N")])
    result = check_diff_pairs(PcbIndex(data, None), None, checks=["skew"])
    assert result["findings"] == []
    assert result["summary"]["skew"]["single_pad_pairs"] == 1


def test_skew_incomplete_legs_with_copper_are_not_called_unrouted():
    note = "The net has polygon pours, which are not part of the copper graph."
    legs = [
        {"length_mil": 1932.0, "status": "incomplete", "pad_count": 3,
         "copper_mil": 1932.0, "pad_islands": 3, "note": note},
        {"length_mil": 1992.0, "status": "incomplete", "pad_count": 3,
         "copper_mil": 1992.0, "pad_islands": 3},
    ]
    index = _skew_case([])
    result = check_diff_pairs(index, None, checks=["skew"], pair_lengths=_measured(*legs))
    assert _issues(result) == [("info", "pair_unrouted", "X")]
    finding = result["findings"][0]
    assert "is completely routed" in finding["message"]
    assert not finding["message"].endswith("is routed.")
    assert "1932" in finding["message"] and "1992" in finding["message"]
    details = finding["details"]
    assert details["note"] == note
    assert [leg["pad_islands"] for leg in details["legs"]] == [3, 3]
    assert [leg["pad_count"] for leg in details["legs"]] == [3, 3]


def test_skew_xsignal_scoped_rule_is_conditional():
    index = _skew_case(
        [_in_pair_rule("InxSignal('XS1')")],
        xsignals=[XSignal(name="XS1", pins=["U1-A1", "J1-1"])],
    )
    result = check_diff_pairs(index, None, checks=["skew"])
    assert _issues(result) == [("info", "in_pair_rule_conditional", "X")]
    assert result["findings"][0]["details"]["conditions"] == ["InxSignal('XS1')"]
    summary = result["summary"]["skew"]
    assert summary["conditional"] == 1
    assert summary["with_in_pair_rule"] == 0


def test_skew_ignores_group_rules_and_lists_disabled_in_pair_rules():
    group = _rule("MatchedLengths", 1, "InDifferentialPair('X')", name="GROUP",
                  TOLERANCE="1mil", CHECKNETSINDIFFPAIR="FALSE")
    disabled = _in_pair_rule("InDifferentialPair('X')", name="OFF", enabled=False)
    index = _skew_case([group, disabled])
    result = check_diff_pairs(index, None, checks=["skew"])
    assert result["findings"] == []
    summary = result["summary"]["skew"]
    assert summary["in_pair_rules"] == []
    assert summary["disabled_in_pair_rules"] == ["OFF"]
    assert summary["largest_skew_without_rule"] == {"pair": "X", "skew_mil": 12.0}


def test_skew_delay_rule_is_not_evaluated():
    index = _skew_case([_in_pair_rule("InDifferentialPair('X')", USEDELAYUNITS="TRUE")])
    result = check_diff_pairs(index, None, checks=["skew"])
    assert result["findings"] == []
    assert any(entry.get("rule") == "ML" for entry in result["not_checked"])
    assert result["summary"]["skew"]["in_pair_rules"][0]["not_evaluated"] == "delay units"


def test_skew_uses_pair_lengths_callable():
    calls = []

    def pair_lengths(name):
        calls.append(name)
        return {
            "positive": {"length_mil": 210.0, "status": "ok"},
            "negative": {"length_mil": 200.0, "status": "ok"},
            "skew_mil": 10.0,
        }

    index = _skew_case([_in_pair_rule("InDifferentialPair('X')")], negative=100.0)
    result = check_diff_pairs(index, None, checks=["skew"], pair_lengths=pair_lengths)
    assert calls == ["X"]
    finding = result["findings"][0]
    assert finding["skew_mil"] == 10.0
    assert finding["positive_mil"] == 210.0
    assert result["summary"]["skew"]["length_source"] == "pair_lengths"
    assert any("length engine" in note for note in result["approximations"])


def test_skew_pair_lengths_incomplete_leg_is_unrouted():
    def pair_lengths(name):
        return {
            "positive": {"length_mil": 210.0, "status": "ok"},
            "negative": {"length_mil": 150.0, "status": "no_path"},
            "skew_mil": 60.0,
        }

    index = _skew_case([_in_pair_rule("InDifferentialPair('X')")])
    result = check_diff_pairs(index, None, checks=["skew"], pair_lengths=pair_lengths)
    assert _issues(result) == [("warning", "leg_unrouted", "X")]
    assert result["findings"][0]["details"]["statuses"] == ["ok", "no_path"]


# ---------- entry point ----------


def test_invalid_arguments_return_errors():
    index = _skew_case([])
    assert check_diff_pairs(index, None, checks=["bogus"])["error"] == "invalid_checks"
    assert check_diff_pairs(index, None, pattern="([")["error"] == "invalid_pattern"


def test_default_run_lists_deferred_directives_check():
    index = _skew_case([])
    result = check_diff_pairs(index, None)
    assert result["checks_run"] == ["naming", "polarity", "series", "skew"]
    not_checked = {entry["check"] for entry in result["not_checked"]}
    assert {"directives", "polarity"} <= not_checked
    directives = next(e for e in result["not_checked"] if e["check"] == "directives")
    assert "schematic" in directives["reason"].lower()
    assert result["approximations"]
    assert result["pairs_total"] == result["pairs_checked"] == 1


def test_requesting_only_directives_runs_nothing():
    index = _skew_case([])
    result = check_diff_pairs(index, None, checks=["directives"])
    assert result["checks_run"] == []
    assert result["summary"] == {}
    assert [entry["check"] for entry in result["not_checked"]] == ["directives"]


def test_findings_are_sorted_and_paginated():
    index = _skew_case(
        [_in_pair_rule("InDifferentialPair('X')")],
        pairs=(("X", "X_P", "X_N"), ("CLK", "CLK_A", "CLK_B")),
        extra_parts=[("U2", "QFN", {"1": "CLK_A", "2": "CLK_B"}),
                     ("J2", "HDR", {"1": "CLK_A", "2": "CLK_B"})],
    )
    full = check_diff_pairs(index, None, checks=["naming", "skew"])
    severities = [finding["severity"] for finding in full["findings"]]
    assert severities == sorted(severities, key=["warning", "info"].index)
    assert full["total"] == len(full["findings"]) >= 3
    first = check_diff_pairs(index, None, checks=["naming", "skew"], limit=1)
    assert first["findings"] == full["findings"][:1]
    assert first["has_more"] is True
    assert first["next_offset"] == 1
    last = check_diff_pairs(index, None, checks=["naming", "skew"], offset=full["total"] - 1, limit=1)
    assert last["has_more"] is False
    assert "next_offset" not in last
    assert full["severity_counts"]["warning"] == 1


def test_pattern_filters_pairs_and_skips_unrelated_rule_findings():
    index = _skew_case(
        [_in_pair_rule("InDifferentialPairClass('GONE')")],
        pairs=(("X", "X_P", "X_N"), ("Y", "Y_P", "Y_N")),
        extra_nets=["Y_P", "Y_N"],
    )
    result = check_diff_pairs(index, None, checks=["skew"], pattern="^Y")
    assert result["pairs_checked"] == 1
    assert all(f["issue"] != "in_pair_rule_stale" for f in result["findings"])
    assert check_diff_pairs(index, None, checks=["skew"], pattern="GONE")["summary"]["skew"]["stale_rules"] == 1


def test_board_without_pairs_has_note(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    result = check_diff_pairs(index, NETLIST)
    assert result["pairs_total"] == 0
    assert "note" in result
    assert result["findings"] == []


def test_shared_fixture_pair_across_channel_copies(pcb_data):
    data = deepcopy(pcb_data)
    data.diff_pairs = [DiffPair("CLK", "CLK_A", "CLK_B", None)]
    index = PcbIndex(data, NETLIST)
    result = check_diff_pairs(index, NETLIST, checks=["naming", "polarity"])
    assert _issues(result) == [("info", "pair_names_unconventional", "CLK")]
    # The two U3 channel copies each touch one leg only.
    assert result["summary"]["polarity"]["incidences"] == 0


# ---------- Altium example boards (skipped when absent) ----------

EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples\Mini PC")


def _example(project_dir: str, pcb_name: str, project_name: str):
    from server.altium import AltiumClient
    from server.parsers.pcb_doc import parse_pcb_doc
    from server.parsers.prj_pcb import parse_prj_pcb

    pcb_path = EXAMPLES / project_dir / pcb_name
    if not pcb_path.is_file():
        pytest.skip("Altium example boards not installed")
    pcb = parse_pcb_doc(str(pcb_path))
    pin_to_net: dict = defaultdict(dict)
    nets: dict = defaultdict(list)
    for primitive in pcb.primitives:
        if primitive.kind != "pad" or primitive.component is None or primitive.net is None:
            continue
        component = pcb.components[primitive.component]
        designator = component.designator or component.refdes
        if designator and primitive.pad_name and primitive.pad_name not in pin_to_net[designator]:
            pin_to_net[designator][primitive.pad_name] = pcb.nets[primitive.net]
            nets[pcb.nets[primitive.net]].append((designator, primitive.pad_name))
    netlist = {"nets": dict(nets), "pin_to_net": dict(pin_to_net)}
    project = parse_prj_pcb(str(EXAMPLES / project_dir / project_name))
    AltiumClient()._enrich_from_schdocs(project.sheet_paths, netlist, project.annotation_paths)
    return PcbIndex(pcb, netlist), netlist


@pytest.fixture(scope="module")
def minipc():
    return _example("Mini PC - Motherboard", "MiniPC.PcbDoc", "MiniPC.PrjPcb")


def test_example_minipc(minipc):
    from server.services.pcb_length import check_length_rules, pair_lengths

    index, netlist = minipc
    # The runtime path: leg lengths from the length engine, as check_lengths.
    result = check_diff_pairs(
        index, netlist, limit=1000, pair_lengths=lambda name: pair_lengths(index, name)
    )
    summary = result["summary"]
    assert result["pairs_total"] == 96
    assert summary["naming"]["undeclared_warning"] == 0
    assert summary["naming"]["undeclared_info"] == 0
    assert summary["naming"]["suppressed_supply_or_sense"] == 2  # *_SENSE+/-
    assert summary["polarity"]["judged"] == 76
    assert summary["polarity"]["swapped"] == 0
    assert summary["series"]["capacitor_coupled"] == 44
    assert summary["series"]["asymmetric_pairs"] == 0
    assert summary["series"]["shunt_parts"] == 0
    assert summary["series"]["common_node_pairs"] == 8
    assert summary["series"]["coupled_side_not_pair"] == 0
    assert summary["skew"]["unrouted"] == 0
    rules = {rule["rule"]: rule for rule in summary["skew"]["in_pair_rules"]}
    assert rules["MatchedLengths_PCIE_RX_TX_TX_C_CLK"]["scope_kind"] == "net"
    assert rules["MatchedLengths_SFP"]["scope_kind"] == "pair"
    assert rules["MatchedLengths_DDR4_DIFF"]["stale_references"]
    # The only warnings: the stale rule, and in-pair skew violations, which
    # are exactly the pairs check_lengths reports over the same rule.
    warnings = [f for f in result["findings"] if f["severity"] == "warning"]
    assert {f["issue"] for f in warnings} <= {
        "in_pair_rule_stale", "in_pair_skew_exceeds_tolerance"
    }
    assert [f["details"]["rule"] for f in warnings if f["issue"] == "in_pair_rule_stale"] == [
        "MatchedLengths_DDR4_DIFF"
    ]
    over = {
        (f["details"]["rule"], f["pair"]) for f in result["findings"]
        if f["issue"] == "in_pair_skew_exceeds_tolerance"
    }
    exceeded = {}
    for name in ("MatchedLengths_PCIE_RX_TX_TX_C_CLK", "MatchedLengths_SFP"):
        checked = check_length_rules(index, rule=name, limit=1000)["rules"][0]
        exceeded.update({
            (name, row["object"]): bool(row.get("marginal"))
            for row in checked["pairs"] if row["status"] == "skew_exceeded"
        })
    assert over == set(exceeded)
    # Same verdict: a warning, or info when check_lengths marks it marginal.
    for finding in result["findings"]:
        if finding["issue"] == "in_pair_skew_exceeds_tolerance":
            marginal = exceeded[(finding["details"]["rule"], finding["pair"])]
            assert finding["severity"] == ("info" if marginal else "warning")
            assert bool(finding["details"].get("marginal")) is marginal


def test_example_sodimm():
    index, netlist = _example(
        "Mini PC - SODIMM", "PC4-SODIMM_V200_RC_A1_20141015.PcbDoc",
        "PC4-SODIMM_V200_RC_A1_20141015.PrjPcb",
    )
    result = check_diff_pairs(index, netlist)
    series = result["summary"]["series"]
    assert series["resistor_series"] == 16
    assert series["far_pair_ok"] == 16
    assert result["severity_counts"] == {"warning": 0, "info": 0}


def test_result_is_json_and_converts_to_mm():
    import json

    from server.main import _convert_units

    index = _skew_case([_in_pair_rule("InDifferentialPair('X')")])
    result = check_diff_pairs(index, None)
    json.dumps(result)
    converted = _convert_units(result, True)
    finding = next(
        f for f in converted["findings"] if f["issue"] == "in_pair_skew_exceeds_tolerance"
    )
    assert finding["skew_mm"] == pytest.approx(12.0 * 0.0254)
    assert finding["tolerance_mm"] == pytest.approx(5.0 * 0.0254)
    assert converted["summary"]["skew"]["in_pair_rules"][0]["tolerance_mm"] == pytest.approx(0.127)


def test_skew_marginal_excess_is_inconclusive_info():
    # As check_lengths: within MARGINAL_MIL of the tolerance a violation is
    # marked marginal (inconclusive); beyond it, a warning.
    from server.services.pcb_length import MARGINAL_MIL

    index = _skew_case([_in_pair_rule("InDifferentialPair('X')")], negative=105.6)
    result = check_diff_pairs(index, None, checks=["skew"])
    assert _issues(result) == [("info", "in_pair_skew_exceeds_tolerance", "X")]
    finding = result["findings"][0]
    assert finding["excess_mil"] == pytest.approx(0.6)
    assert finding["details"]["marginal"] is True
    summary = result["summary"]["skew"]
    assert summary["over_tolerance"] == 1
    assert summary["marginal"] == 1
    negative = 105.0 + MARGINAL_MIL + 1.0
    index = _skew_case([_in_pair_rule("InDifferentialPair('X')")], negative=negative)
    finding = check_diff_pairs(index, None, checks=["skew"])["findings"][0]
    assert finding["severity"] == "warning"
    assert finding["excess_mil"] == pytest.approx(MARGINAL_MIL + 1.0)
    assert "marginal" not in finding["details"]
