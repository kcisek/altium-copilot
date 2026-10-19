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
    ],
)
def test_parse_value(raw, expected):
    assert parse_value(raw) == pytest.approx(expected)


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


def test_series_part_across_the_pair_is_counted_not_flagged():
    index, netlist = _series_case(
        [], [], far_pairs=(), extra_parts=[("R5", "0402", {"1": "X_P", "2": "X_N"})],
    )
    result = check_diff_pairs(index, netlist, checks=["series"])
    assert result["summary"]["series"]["across_pair_parts"] == 1
    assert result["findings"] == []


# ---------- skew ----------


def _skew_case(rules, positive=100.0, negative=107.0, classes=(), xsignals=(),
               pairs=(("X", "X_P", "X_N"),), extra_nets=()):
    parts = [
        ("U1", "QFN", {"A1": "X_P", "A2": "X_N"}),
        ("J1", "HDR", {"1": "X_P", "2": "X_N"}),
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
    assert finding["negative_mil"] == 107.0
    assert finding["skew_mil"] == 7.0
    assert finding["tolerance_mil"] == 5.0
    assert finding["excess_mil"] == 2.0
    assert finding["details"]["rule_scope"] == "pair"
    summary = result["summary"]["skew"]
    assert summary["with_in_pair_rule"] == 1
    assert summary["pair_scoped"] == 1
    assert summary["over_tolerance"] == 1
    assert summary["in_pair_rules"][0]["scope_kind"] == "pair"
    assert summary["in_pair_rules"][0]["max_skew_mil"] == 7.0


def test_skew_within_tolerance_has_no_finding():
    index = _skew_case([_in_pair_rule("InDifferentialPair('X')")], negative=104.0)
    result = check_diff_pairs(index, None, checks=["skew"])
    assert result["findings"] == []
    assert result["summary"]["skew"]["within_tolerance"] == 1


def test_skew_net_scoped_rule_is_info_with_note():
    index = _skew_case(
        [_in_pair_rule("InNetClass('HS')")], classes=[_class("HS", "net", ["X_P", "X_N"])],
    )
    result = check_diff_pairs(index, None, checks=["skew"])
    assert _issues(result) == [("info", "in_pair_skew_exceeds_tolerance", "X")]
    details = result["findings"][0]["details"]
    assert details["rule_scope"] == "net"
    assert "InDifferentialPair" in details["note"]
    assert result["summary"]["skew"]["net_scoped_only"] == 1


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
    assert summary["largest_skew_without_rule"] == {"pair": "X", "skew_mil": 7.0}


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
        extra_nets=["CLK_A", "CLK_B"],
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
    index, netlist = minipc
    result = check_diff_pairs(index, netlist, limit=1000)
    summary = result["summary"]
    assert result["pairs_total"] == 96
    assert summary["naming"]["undeclared_warning"] == 0
    assert summary["naming"]["undeclared_info"] == 0
    assert summary["naming"]["suppressed_supply_or_sense"] == 2  # *_SENSE+/-
    assert summary["polarity"]["judged"] == 76
    assert summary["polarity"]["swapped"] == 0
    assert summary["series"]["capacitor_coupled"] == 44
    assert summary["series"]["asymmetric_pairs"] == 0
    assert summary["series"]["common_node_pairs"] == 8
    assert summary["series"]["coupled_side_not_pair"] == 0
    rules = {rule["rule"]: rule for rule in summary["skew"]["in_pair_rules"]}
    assert rules["MatchedLengths_PCIE_RX_TX_TX_C_CLK"]["scope_kind"] == "net"
    assert rules["MatchedLengths_SFP"]["scope_kind"] == "pair"
    assert rules["MatchedLengths_DDR4_DIFF"]["stale_references"]
    warnings = [f for f in result["findings"] if f["severity"] == "warning"]
    assert [(f["issue"], f["details"]["rule"]) for f in warnings] == [
        ("in_pair_rule_stale", "MatchedLengths_DDR4_DIFF")
    ]
    over = {
        f["pair"] for f in result["findings"]
        if f["issue"] == "in_pair_skew_exceeds_tolerance"
    }
    assert over == {"PCIE_TX5", "PCIE_TX0", "PCIE_RX4", "PCIE_RX3", "PCIE_REFCLK_QR0"}


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
    assert finding["skew_mm"] == pytest.approx(7.0 * 0.0254)
    assert finding["tolerance_mm"] == pytest.approx(5.0 * 0.0254)
    assert converted["summary"]["skew"]["in_pair_rules"][0]["tolerance_mm"] == pytest.approx(0.127)


def test_skew_borderline_excess_is_info():
    index = _skew_case([_in_pair_rule("InDifferentialPair('X')")], negative=105.6)
    result = check_diff_pairs(index, None, checks=["skew"])
    assert _issues(result) == [("info", "in_pair_skew_exceeds_tolerance", "X")]
    finding = result["findings"][0]
    assert finding["excess_mil"] == pytest.approx(0.6)
    assert finding["details"]["borderline"] is True
    summary = result["summary"]["skew"]
    assert summary["over_tolerance"] == 1
    assert summary["borderline"] == 1
