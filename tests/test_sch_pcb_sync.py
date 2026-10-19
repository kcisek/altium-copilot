"""Schematic-vs-PCB sync check: linking, component, pin and net differences."""

import json
import os
from collections import defaultdict
from pathlib import Path

import pytest

import server.main as main_mod
from server.parsers.pcb_doc import PcbComponent, PcbData
from server.parsers.prj_pcb import VariantState
from server.services.pcb_index import PcbIndex
from server.services.sch_pcb_sync import (
    DEFAULT_NET_MATCHING,
    net_matching_options,
    run_sync_check,
)
from tests.test_pcb_index import _board, _pad

NETS = ["GND", "VIN", "OUT", "FB"]
GND, VIN, OUT, FB = range(4)


def _component(index, designator, uid=None, footprint=None, comment=None, kind=0, path=None):
    """A PCB part; path is its full SOURCEUNIQUEID (channel copies differ only there)."""
    return PcbComponent(
        index=index,
        refdes=designator if uid else None,
        source_uid_tail=uid,
        source_unique_id=path or (f"\\TOP\\{uid}" if uid else None),
        channel_path=None,
        x=index * 100.0,
        y=0.0,
        rotation=0.0,
        side="top",
        footprint=footprint,
        designator=designator,
        comment=comment,
        kind=kind,
    )


def _pcb(extra_components=(), extra_pads=()):
    """R1/R2 divider into U1's FB pin, C1 on VIN; a logo and a mounting hole."""
    components = [
        _component(0, "R1", "UR1", "R0603", "10k"),
        _component(1, "R2", "UR2", "R0603", "1k"),
        _component(2, "U1", "UU1", "SOIC8", "OPA"),
        _component(3, "C1", "UC1", "C0402", "100n"),
        _component(4, "LOGO1", kind=2),
        _component(5, "MH1", kind=1),
        *extra_components,
    ]
    pads = [
        (VIN, 0, "1"), (FB, 0, "2"),
        (FB, 1, "1"), (GND, 1, "2"),
        (OUT, 2, "1"), (FB, 2, "2"), (VIN, 2, "3"), (GND, 2, "4"),
        (VIN, 3, "1"), (GND, 3, "2"),
        (GND, 5, "1"),
        *extra_pads,
    ]
    return PcbData(
        board=_board(),
        nets=list(NETS),
        components=components,
        primitives=[
            _pad(net, component, component * 100.0, i * 10.0, name=name)
            for i, (net, component, name) in enumerate(pads)
        ],
        pours=[],
        layer_name_to_v6={"top layer": 1, "top": 1},
    )


def _part(uid, footprint, pins):
    return {
        "unique_id": uid, "unique_ids": [uid], "footprint": footprint,
        "footprints": [footprint], "kind": 0, "pin_designators": pins,
        "sheet": "Main", "pins": {},
    }


def _netlist():
    # OUT has one pin: the Protel netlist leaves single-pin nets out.
    nets = {
        "VIN": [("R1", "1"), ("U1", "3"), ("C1", "1")],
        "FB": [("R1", "2"), ("R2", "1"), ("U1", "2")],
        "GND": [("R2", "2"), ("U1", "4"), ("C1", "2")],
    }
    pin_to_net: dict = {}
    for net, pins in nets.items():
        for ref, pin in pins:
            pin_to_net.setdefault(ref, {})[pin] = net
    return {
        "nets": nets,
        "pin_to_net": pin_to_net,
        "parts": {
            "R1": {"footprint": "R0603", "comment": "10k"},
            "R2": {"footprint": "R0603", "comment": "1k"},
            "U1": {"footprint": "SOIC8", "comment": "OPA"},
            "C1": {"footprint": "C0402", "comment": "100n"},
        },
        "components": {
            "R1": _part("UR1", "R0603", ["1", "2"]),
            "R2": _part("UR2", "R0603", ["1", "2"]),
            "U1": _part("UU1", "SOIC8", ["1", "2", "3", "4"]),
            "C1": _part("UC1", "C0402", ["1", "2"]),
        },
    }


def _rename(netlist, old, new):
    for key in ("parts", "components", "pin_to_net"):
        if old in netlist[key]:
            netlist[key][new] = netlist[key].pop(old)
    for net, pins in netlist["nets"].items():
        netlist["nets"][net] = [(new if ref == old else ref, pin) for ref, pin in pins]


def _add_pin(netlist, ref, pin, net):
    netlist["nets"].setdefault(net, []).append((ref, pin))
    netlist["pin_to_net"].setdefault(ref, {})[pin] = net


def _remove_pin(netlist, ref, pin):
    net = netlist["pin_to_net"][ref].pop(pin)
    netlist["nets"][net].remove((ref, pin))


def _rename_net(netlist, old, new):
    netlist["nets"][new] = netlist["nets"].pop(old)
    for pins in netlist["pin_to_net"].values():
        for pin, net in pins.items():
            if net == old:
                pins[pin] = new


def _check(pcb=None, netlist=None, **kwargs):
    netlist = netlist or _netlist()
    kwargs.setdefault("limit", 1000)
    return run_sync_check(PcbIndex(pcb or _pcb(), netlist), netlist, **kwargs)


def _problems(result):
    return [finding for finding in result["findings"] if finding["severity"] != "info"]


def test_in_sync_board():
    result = _check()
    assert result["in_sync"] is True
    assert result["components"] == {
        "schematic": 4, "pcb": 6, "linked_by_unique_id": 4, "linked_by_designator": 0,
    }
    assert result["nets"]["matched_by_members"] == 3
    # The logo and the mounting hole are PCB-only by design.
    assert [(f["check"], f["severity"], f["pcb"]) for f in result["findings"]] == [
        ("extra_on_pcb", "info", "LOGO1"), ("extra_on_pcb", "info", "MH1"),
    ]
    assert any("1 PCB nets with a single pad" in note for note in result["notes"])  # OUT


def test_designator_swap_is_two_designator_differences():
    netlist = _netlist()
    _rename(netlist, "R1", "TMP")
    _rename(netlist, "R2", "R1")
    _rename(netlist, "TMP", "R2")
    result = _check(netlist=netlist)
    # Linked by unique ID: no footprint, comment or pad differences follow.
    assert [(f["check"], f["schematic"], f["pcb"]) for f in _problems(result)] == [
        ("designator", "R1", "R2"), ("designator", "R2", "R1"),
    ]


def test_footprint_and_comment_differences():
    netlist = _netlist()
    netlist["parts"]["R1"]["footprint"] = "R0402"
    netlist["components"]["R1"]["footprints"] = ["R0402", "R0603"]
    netlist["parts"]["R2"]["comment"] = "1K"  # case only: Altium ignores it by default
    netlist["parts"]["C1"]["comment"] = "220n"
    footprint, comment = _problems(_check(netlist=netlist))
    assert (footprint["check"], footprint["schematic"], footprint["pcb"]) == (
        "footprint", "R0402", "R0603",
    )
    assert footprint["pcb_is_a_schematic_alternative"] is True
    assert (comment["check"], comment["component"], comment["schematic"], comment["pcb"]) == (
        "comment", "C1", "220n", "100n",
    )


def test_moved_pin_and_renamed_net():
    netlist = _netlist()
    _remove_pin(netlist, "U1", "2")
    _add_pin(netlist, "U1", "2", "VIN")
    _rename_net(netlist, "GND", "0V")
    result = _check(netlist=netlist)
    pad, renamed = _problems(result)
    assert (pad["check"], pad["severity"], pad["pin"]) == ("pad_net", "error", "U1-2")
    assert (pad["schematic_net"], pad["pcb_net"]) == ("VIN", "FB")
    assert pad["issue"] == (
        "Pad U1-2 is on FB on the PCB; the schematic connects it to VIN."
    )
    # Update PCB renames the net: classes, rules and pairs go by name.
    assert (renamed["check"], renamed["schematic"], renamed["pcb"], renamed["severity"]) == (
        "net_name", "0V", "GND", "warning",
    )
    assert result["nets"]["matched_by_members"] == 2  # VIN (3 of 4 shared) and 0V
    assert result["nets"]["matched_by_name"] == 1  # FB: 2 shared, below 3 members


def test_pin_without_a_pad_is_an_eco_failure():
    netlist = _netlist()
    _add_pin(netlist, "U1", "5", "VIN")
    (finding,) = _problems(_check(netlist=netlist))
    assert (finding["check"], finding["severity"], finding["pin"]) == (
        "unknown_pin", "error", "U1-5",
    )
    assert "Unknown Pin: Pin U1-5" in finding["issue"]


def test_pins_behind_a_footprint_change_are_not_checked():
    netlist = _netlist()
    _add_pin(netlist, "U1", "5", "VIN")
    netlist["parts"]["U1"]["footprint"] = "SOIC14"
    result = _check(netlist=netlist)
    assert [f["check"] for f in _problems(result)] == ["footprint"]
    assert any("footprint changes" in item for item in result["not_checked"])


def test_parts_missing_from_the_pcb():
    netlist = _netlist()
    netlist["parts"]["R3"] = {"footprint": "R0603", "comment": "0R"}
    netlist["components"]["R3"] = {"sheet": "Power", "unique_ids": ["UR3"]}
    _add_pin(netlist, "R3", "1", "FB")
    netlist["parts"]["JS1"] = {"footprint": "", "comment": "Jumper socket"}
    result = _check(
        netlist=netlist, is_dnp=lambda name: name == "R3", sheets_saved_after_pcb=["Power"],
    )
    (r3,) = _problems(result)
    assert (r3["check"], r3["schematic"], r3["dnp"], r3["sheet_saved_after_pcb"]) == (
        "missing_from_pcb", "R3", True, True,
    )
    assert "still places every variant's footprints" in r3["issue"]
    (js1,) = [f for f in result["findings"] if f.get("schematic") == "JS1"]
    assert js1["severity"] == "info" and "no footprint" in js1["issue"]
    # FB still matches: R3-1 is counted as a member not on the PCB.
    assert not any(f["check"].startswith("net_only") for f in result["findings"])


def test_parts_only_on_the_pcb():
    pcb = _pcb(extra_components=[
        _component(6, "R9", "GONE", "R0603"),   # its schematic part was deleted
        _component(7, "R1", "UR1", "R0402"),    # a variant's alternate part beside R1
        _component(8, "R5", "UR2", "R0603"),    # a copy of R2
        _component(9, "TP1", footprint="TP"),   # placed on the PCB only
    ])
    result = _check(pcb=pcb)
    extra = {
        (f["pcb"], f.get("footprint")): f
        for f in result["findings"] if f["check"] == "extra_on_pcb"
    }
    assert extra[("R9", "R0603")]["severity"] == "warning"
    assert "part is gone" in extra[("R9", "R0603")]["issue"]
    # The R1 placement with the schematic's footprint is the linked one.
    assert extra[("R1", "R0402")]["severity"] == "info"
    assert "alternate part" in extra[("R1", "R0402")]["issue"]
    assert extra[("R5", "R0603")]["severity"] == "warning"
    assert "copied footprint" in extra[("R5", "R0603")]["issue"]
    assert extra[("TP1", "TP")]["severity"] == "warning"
    assert extra[("LOGO1", None)]["kind"] == "graphical"
    assert extra[("MH1", None)]["severity"] == "info"
    assert result["components"]["linked_by_unique_id"] == 4


def test_pads_the_schematic_does_not_connect():
    pcb = _pcb(extra_pads=[(GND, 2, "5")])  # U1 pad 5: the symbol has no pin 5
    netlist = _netlist()
    _remove_pin(netlist, "C1", "2")  # left unconnected in the schematic
    result = _check(pcb=pcb, netlist=netlist)
    pads = {f["pin"]: f for f in _problems(result)}
    assert set(pads) == {"U1-5", "C1-2"}
    assert pads["U1-5"]["severity"] == "warning" and "no such pin" in pads["U1-5"]["issue"]
    assert pads["C1-2"]["severity"] == "error"
    assert (pads["C1-2"]["schematic_net"], pads["C1-2"]["pcb_net"]) == (None, "GND")


def test_channel_copies_and_designator_fallback():
    pcb = _pcb(extra_components=[
        _component(6, "R5A", "CH", "R0603", path="\\SA\\CH"),
        _component(7, "R5B", "CH", "R0603", path="\\SB\\CH"),
        _component(8, "J1", footprint="HDR2"),  # never linked to the schematic
    ])
    netlist = _netlist()
    for name in ("R5A", "R5B"):  # channel copies share one unique ID
        netlist["parts"][name] = {"footprint": "R0603", "comment": ""}
        netlist["components"][name] = {"unique_ids": ["CH"]}
    netlist["parts"]["J1"] = {"footprint": "HDR2", "comment": ""}
    netlist["components"]["J1"] = {"unique_ids": ["UJ1"], "pin_designators": ["1", "2"]}
    result = _check(pcb=pcb, netlist=netlist)
    assert result["components"]["linked_by_unique_id"] == 6
    assert result["components"]["linked_by_designator"] == 1
    assert _problems(result) == []
    (link,) = [f for f in result["findings"] if f["check"] == "component_link"]
    assert link["component"] == "J1" and "no schematic unique ID" in link["issue"]


def test_jumper_nets_are_not_compared():
    pcb = _pcb(
        extra_components=[_component(6, "W1", "UW1", "JMP", kind=6)],
        extra_pads=[(VIN, 6, "1"), (OUT, 6, "2")],
    )
    netlist = _netlist()
    netlist["parts"]["W1"] = {"footprint": "JMP", "comment": ""}
    netlist["components"]["W1"] = {"unique_ids": ["UW1"]}
    _add_pin(netlist, "W1", "1", "FB")  # differs from the PCB's VIN: not synchronized
    _add_pin(netlist, "U1", "1", "OUT")  # OUT now has two pins, so the netlist lists it
    _add_pin(netlist, "W1", "2", "OUT")
    assert _problems(_check(pcb=pcb, netlist=netlist)) == []


def test_net_matching_options(tmp_path):
    prj = tmp_path / "Demo.PrjPcb"
    prj.write_text(
        "[Comparison Options]\n"
        "ComparisonOptions0=Kind=Net|MinPercent=60|MinMatch=2|ShowMatch=-1|Confirm=-1"
        "|UseName=0|InclAllRules=0\n"
        "ComparisonOptions1=Kind=Net Class|MinPercent=75|MinMatch=3|ShowMatch=-1"
        "|Confirm=-1|UseName=-1|InclAllRules=0\n",
        encoding="utf-8",
    )
    assert net_matching_options(str(prj)) == {
        "min_match_percent": 60, "min_matched_members": 2, "name_matching": False,
        "source": "project options",
    }
    assert net_matching_options(str(tmp_path / "missing.PrjPcb")) == DEFAULT_NET_MATCHING
    assert net_matching_options(None) == DEFAULT_NET_MATCHING


def test_without_name_matching_a_small_renamed_net_is_extra_on_both_sides():
    netlist = _netlist()
    _remove_pin(netlist, "U1", "2")  # FB keeps 2 members, below Min Matched Members
    _add_pin(netlist, "U1", "2", "VIN")
    options = {**DEFAULT_NET_MATCHING, "name_matching": False}
    result = _check(netlist=netlist, net_matching=options)
    assert result["summary"]["net_only_in_schematic"] == 1
    assert result["summary"]["net_only_on_pcb"] == 1


def test_checks_filter_paging_and_errors():
    netlist = _netlist()
    _add_pin(netlist, "U1", "5", "VIN")
    netlist["parts"]["C1"]["comment"] = "220n"
    _rename_net(netlist, "GND", "0V")
    nets_only = _check(netlist=netlist, checks=["nets"])
    assert [f["check"] for f in nets_only["findings"]] == ["net_name"]
    assert nets_only["checks_run"] == ["nets"]
    paged = _check(netlist=netlist, limit=1)
    assert (paged["total"], paged["has_more"], paged["next_offset"]) == (5, True, 1)
    assert paged["findings"][0]["check"] == "unknown_pin"  # errors first
    assert paged["severity_counts"] == {"error": 1, "warning": 2, "info": 2}
    assert paged["altium_names"]["unknown_pin"].startswith("ECO error 'Unknown Pin'")
    assert _check(checks=["wires"])["error"] == "invalid_checks"
    assert run_sync_check(PcbIndex(_pcb(), None), None)["error"] == "no_netlist"


def test_without_the_compiled_part_list():
    """An older netlist: footprints from the sheets, comments not compared."""
    netlist = _netlist()
    del netlist["parts"]
    netlist["components"]["R1"]["footprint_by_uid"] = {"UR1": "R0402"}
    result = _check(netlist=netlist)
    (finding,) = _problems(result)
    assert (finding["check"], finding["schematic"], finding["pcb"]) == (
        "footprint", "R0402", "R0603",
    )
    assert any("comments are not compared" in item for item in result["not_checked"])


def _add_part(netlist, name, footprint, uid, pins, **meta):
    netlist["parts"][name] = {"footprint": footprint, "comment": ""}
    netlist["components"][name] = {"unique_ids": [uid], "pin_designators": list(pins), **meta}
    for pin, net in pins.items():
        _add_pin(netlist, name, pin, net)


def _channel_board(first=(VIN, GND), second=(FB, GND)):
    """Two channel copies of one schematic part (unique ID CH) on the given nets."""
    return _pcb(
        extra_components=[
            _component(6, "R5A", "CH", "R0603", path="\\SA\\CH"),
            _component(7, "R5B", "CH", "R0603", path="\\SB\\CH"),
        ],
        extra_pads=[(first[0], 6, "1"), (first[1], 6, "2"), (second[0], 7, "1"),
                    (second[1], 7, "2")],
    )


def test_channel_copies_link_by_their_full_path():
    """Board-level annotation renumbered the copies; the .Annotation paths tell them apart."""
    netlist = _netlist()
    _add_part(netlist, "R5B", "R0603", "CH", {"1": "VIN", "2": "GND"},
              unique_paths=["\\SA\\CH"])
    _add_part(netlist, "R5A", "R0603", "CH", {"1": "FB", "2": "GND"},
              unique_paths=["\\SB\\CH"])
    result = _check(pcb=_channel_board(), netlist=netlist)
    assert [(f["check"], f["schematic"], f["pcb"]) for f in _problems(result)] == [
        ("designator", "R5A", "R5B"), ("designator", "R5B", "R5A"),
    ]


def test_renamed_channel_copies_pair_by_their_named_nets():
    """A new designator format renamed both copies; no .Annotation paths."""
    netlist = _netlist()
    _add_part(netlist, "R5_2", "R0603", "CH", {"1": "VIN", "2": "GND"})
    _add_part(netlist, "R5_1", "R0603", "CH", {"1": "FB", "2": "GND"})
    problems = _problems(_check(pcb=_channel_board(), netlist=netlist))
    assert {f["check"] for f in problems} == {"designator"}
    assert sorted((f["schematic"], f["pcb"]) for f in problems) == [
        ("R5_1", "R5B"), ("R5_2", "R5A"),
    ]


def test_interchangeable_channel_copies_pair_in_order():
    """Decoupling caps on the same rails: either pairing is right, none is extra."""
    netlist = _netlist()
    _add_part(netlist, "R5_1", "R0603", "CH", {"1": "VIN", "2": "GND"})
    _add_part(netlist, "R5_2", "R0603", "CH", {"1": "VIN", "2": "GND"})
    problems = _problems(_check(pcb=_channel_board(second=(VIN, GND)), netlist=netlist))
    assert [(f["check"], f["schematic"], f["pcb"]) for f in problems] == [
        ("designator", "R5_1", "R5A"), ("designator", "R5_2", "R5B"),
    ]


def test_copied_footprint_removed_channel_and_mechanical_part():
    pcb = _pcb(extra_components=[
        _component(6, "C1", "UC1", "C0402"),                     # C1 pasted again
        _component(7, "R5A", "CH", "R0603", path="\\SA\\CH"),
        _component(8, "R5B", "CH", "R0603", path="\\SB\\CH"),    # its channel is gone
        _component(9, "HS1", "UHS1", "TO220_HS", kind=1),        # mechanical, part deleted
    ])
    netlist = _netlist()
    _add_part(netlist, "R5A", "R0603", "CH", {}, unique_paths=["\\SA\\CH"])
    result = _check(pcb=pcb, netlist=netlist)
    extra = {f["pcb"]: f for f in result["findings"] if f["check"] == "extra_on_pcb"}
    assert extra["C1"]["severity"] == "warning" and "copied footprint" in extra["C1"]["issue"]
    assert extra["R5B"]["severity"] == "warning"
    assert "another channel's copy of R5A" in extra["R5B"]["issue"]
    assert extra["HS1"]["severity"] == "info" and "mechanical" in extra["HS1"]["issue"]


def test_deleted_part_leaves_a_single_pin_net():
    """TP1 was deleted: OUT keeps one pin, which the Protel netlist leaves out."""
    pcb = _pcb(extra_components=[_component(6, "TP1", "UTP", "TP")], extra_pads=[(OUT, 6, "1")])
    result = _check(pcb=pcb)
    assert [(f["check"], f["pcb"]) for f in _problems(result)] == [("extra_on_pcb", "TP1")]


def test_variant_alternate_on_a_single_pin_net():
    pcb = _pcb(
        extra_components=[_component(6, "U1", "UU1", "SOIC8W")],
        extra_pads=[(OUT, 6, "1"), (FB, 6, "2"), (VIN, 6, "3"), (GND, 6, "4")],
    )
    result = _check(pcb=pcb)
    assert result["in_sync"] is True
    (alternate,) = [f for f in result["findings"] if f["pcb"] == "U1"]
    assert alternate["footprint"] == "SOIC8W" and "alternate part" in alternate["issue"]


def test_case_only_net_names():
    netlist = _netlist()
    _rename_net(netlist, "GND", "gnd")  # matched by members
    _remove_pin(netlist, "U1", "2")
    _add_pin(netlist, "U1", "2", "VIN")
    _rename_net(netlist, "FB", "fb")  # 2 members left: matched by name, ignoring case
    result = _check(netlist=netlist)
    assert result["nets"]["matched_by_name"] == 1
    assert sorted(
        (f["schematic"], f["pcb"], f["severity"])
        for f in result["findings"] if f["check"] == "net_name"
    ) == [("fb", "FB", "info"), ("gnd", "GND", "info")]


def test_pad_on_a_net_matched_to_another_schematic_net():
    pcb = _pcb(extra_pads=[(GND, 2, "5")])
    netlist = _netlist()
    _rename_net(netlist, "GND", "AGND")
    netlist["components"]["U1"]["pin_designators"].append("5")
    _add_pin(netlist, "U1", "5", "GND")  # a new schematic GND, on the PCB's GND copper
    result = _check(pcb=pcb, netlist=netlist)
    (pad,) = [f for f in result["findings"] if f["check"] == "pad_net"]
    assert pad["issue"] == (
        "Pad U1-5 is on GND (the schematic's AGND) on the PCB; the schematic connects it "
        "to GND (a net the PCB lacks)."
    )
    assert (pad["pcb_net_matches"], pad["schematic_net_on_pcb"]) == ("AGND", None)


def test_new_net_behind_a_footprint_change():
    netlist = _netlist()
    netlist["parts"]["U1"]["footprint"] = "SOIC14"
    _add_pin(netlist, "U1", "5", "N_NEW")
    _add_pin(netlist, "U1", "6", "N_NEW")
    result = _check(netlist=netlist)
    (new,) = [f for f in result["findings"] if f["check"] == "net_only_in_schematic"]
    assert (new["net"], new["members"], new["members_behind_a_footprint_change"]) == (
        "N_NEW", 2, 2,
    )


def test_part_without_sheet_metadata():
    netlist = _netlist()
    del netlist["components"]["C1"]  # e.g. its sheet failed to parse
    pcb = _pcb(extra_pads=[(GND, 3, "3")])  # C1 pad 3 tied to GND by hand
    result = _check(pcb=pcb, netlist=netlist)
    (link,) = [f for f in result["findings"] if f["check"] == "component_link"]
    assert "no sheet gave the schematic part's unique ID" in link["issue"]
    (pad,) = [f for f in result["findings"] if f["check"] == "pad_net"]
    assert pad["severity"] == "warning" and "pin list was not read" in pad["issue"]


def test_in_sync_covers_every_check():
    netlist = _netlist()
    _add_pin(netlist, "U1", "5", "VIN")
    result = _check(netlist=netlist, checks=["nets"])
    assert result["findings"] == [] and result["in_sync"] is False
    assert result["other_checks_with_differences"] == ["pins"]


def test_net_matching_options_fall_back_on_bad_values(tmp_path):
    prj = tmp_path / "Demo.PrjPcb"
    prj.write_text(
        "ComparisonOptions0=Kind=Net Class|MinPercent=50|MinMatch=1\n"
        "ComparisonOptions1=Kind=Net|MinPercent=x|MinMatch=3|UseName=-1\n",
        encoding="utf-8",
    )
    assert net_matching_options(str(prj)) == DEFAULT_NET_MATCHING


EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")


def _example(project_dir: str, pcb_name: str, project_name: str):
    """An Altium example board with a netlist built from its pads, enriched from its
    sheets (no .NET ships with the examples, so the compiled part list is absent)."""
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
    return run_sync_check(PcbIndex(pcb, netlist), netlist, limit=1000)


def test_example_wifi_alternate_footprints():
    result = _example("Mini PC/Mini PC - WiFi", "WiFi.PcbDoc", "WiFi_miniPCIe.PrjPcb")
    assert result["components"]["linked_by_unique_id"] == 28
    footprints = [f for f in result["findings"] if f["check"] == "footprint"]
    assert len(footprints) == len(result["findings"]) == 4
    # Each PCB footprint is another density variant offered by the schematic part.
    assert all(f["pcb_is_a_schematic_alternative"] for f in footprints)


def test_example_bluetooth_board_annotation_in_sync():
    result = _example("Bluetooth Sentinel", "Bluetooth_Sentinel.PcbDoc", "Bluetooth_Sentinel.PrjPcb")
    # 56 parts carry board-level (physical) designators: none is a difference.
    assert result["in_sync"] is True
    assert result["components"]["linked_by_unique_id"] == 72
    assert [(f["check"], f["pcb"], f["kind"]) for f in result["findings"]] == [
        ("extra_on_pcb", "Altium Logo BOT1", "graphical"),
    ]


def test_example_sl1_mechanical_parts_and_net_tie():
    result = _example(
        "SpiritLevel-SL1", "SL1 Xilinx Spartan-IIE PQ208 Rev1.02.PcbDoc",
        "SL1 Xilinx Spartan-IIE PQ208 Rev1.02.PrjPcb",
    )
    # The jumper sockets are mechanical parts the PCB never placed; NT1 is a net tie
    # whose pads stay on two nets.
    assert result["in_sync"] is True
    assert sorted(
        f["schematic"] for f in result["findings"] if f["check"] == "missing_from_pcb"
    ) == ["JS1", "JS2", "JS3"]
    assert [f["pcb"] for f in result["findings"] if f["check"] == "extra_on_pcb"] == ["Logo5"]


@pytest.fixture
def sync_project(monkeypatch, tmp_path):
    pcb_file = tmp_path / "Board.PcbDoc"
    main_sheet = tmp_path / "Main.SchDoc"
    power_sheet = tmp_path / "Power.SchDoc"
    # Main was saved before the board, Power after it.
    for path, saved in ((main_sheet, 1_000_000), (pcb_file, 2_000_000), (power_sheet, 3_000_000)):
        path.write_bytes(b"x")
        os.utime(path, (saved, saved))
    netlist = _netlist()
    netlist["net_file_mtime"] = 2_500_000  # compiled after Main and the board, before Power
    index = PcbIndex(_pcb(), netlist)
    project = {
        "name": "Demo",
        "root_dir": str(tmp_path),
        "prj_pcb_path": str(tmp_path / "Demo.PrjPcb"),
        "sheets": [
            {"name": "Main", "path": str(main_sheet)},
            {"name": "Power", "path": str(power_sheet)},
            {"name": "Gone", "path": str(tmp_path / "Gone.SchDoc")},  # missing on disk
        ],
        "pcb_doc_paths": [str(pcb_file)],
    }
    monkeypatch.setattr(main_mod, "_project", project)
    monkeypatch.setattr(main_mod._altium, "_netlist", netlist)
    monkeypatch.setattr(main_mod, "_variant_state", VariantState([]))
    monkeypatch.setattr(main_mod._pcb_session, "_load", lambda path, netlist: index)
    main_mod._pcb_session._index = None
    return {"project": project, "netlist": netlist, "tmp_path": tmp_path}


def test_check_sch_pcb_sync_tool(sync_project):
    result = json.loads(main_mod.check_sch_pcb_sync())
    assert result["pcb_doc"] == "Board.PcbDoc"
    assert result["sheets_saved_after_pcb"] == ["Power"]
    assert result["in_sync"] is True
    assert result["net_matching"]["source"] == "Altium defaults"
    assert "note" not in result and "sheet_errors" not in result
    # Power was saved after the netlist was compiled: its changes are not compared.
    assert result["netlist_stale"] is True
    assert result["sheets_saved_after_netlist"] == ["Power"]
    assert "refresh_netlist" in result["hint"]
    assert result["netlist_compiled_utc"].startswith("1970-01-29")
    error = json.loads(main_mod.check_sch_pcb_sync(checks=["wires"]))
    assert error["error"] == "invalid_checks"


def test_check_sch_pcb_sync_context(sync_project, monkeypatch):
    from server.parsers.prj_pcb import VariantDefinition

    netlist = sync_project["netlist"]
    netlist["net_file_mtime"] = 4_000_000  # fresh
    netlist["parts"]["R3"] = {"footprint": "R0603", "comment": "0R"}
    netlist["sheet_errors"] = [{"sheet": "Power", "path": "", "error": "boom"}]
    sync_project["project"]["pcb_doc_paths"].append(str(sync_project["tmp_path"] / "Panel.PcbDoc"))
    monkeypatch.setattr(
        main_mod, "_variant_state",
        VariantState([VariantDefinition(name="Prod", dnp_refdes=["R3"])]),
    )
    result = json.loads(main_mod.check_sch_pcb_sync())
    assert "netlist_stale" not in result
    assert result["sheet_errors"] == netlist["sheet_errors"]
    assert result["note"] == "Project lists 2 PcbDocs; compared with 'Board.PcbDoc'."
    (missing,) = [f for f in result["findings"] if f["check"] == "missing_from_pcb"]
    assert (missing["schematic"], missing["dnp"]) == ("R3", True)


def test_check_sch_pcb_sync_errors(sync_project, monkeypatch):
    sync_project["netlist"]["components"] = {}
    assert json.loads(main_mod.check_sch_pcb_sync())["error"] == "no_components_loaded"
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.check_sch_pcb_sync())["error"] == "no_project"


def test_check_sch_pcb_sync_shrinks_to_fit(sync_project, monkeypatch):
    for pin in range(5, 45):  # 40 pins the footprint has no pad for
        _add_pin(sync_project["netlist"], "U1", str(pin), "VIN")
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 8000)
    result = json.loads(main_mod.check_sch_pcb_sync())
    assert result["findings_limited_to"] == 10
    assert (len(result["findings"]), result["next_offset"], result["total"]) == (10, 10, 42)
    page = json.loads(main_mod.check_sch_pcb_sync(offset=result["next_offset"]))
    assert page["offset"] == 10 and page["findings"][0] != result["findings"][0]


def test_tool_description_mentions_paging():
    assert "next_offset" in main_mod.check_sch_pcb_sync.__doc__
