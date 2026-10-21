"""diff_design: the changelog between two states of a project. The diff sections run on
synthetic sheets and boards; the Git side runs on tiny repositories built in tmp_path,
whose sheets and boards are JSON that the parsers are patched to read (one test commits
real fixture sheets). Nothing here contacts a server, and every Git test checks that
the clone is left exactly as it was."""
import json
import shutil
import subprocess
import typing
from pathlib import Path

import pytest

import server.main as main
import services.design_diff as dd
from parsers.pcb_doc import (
    BoardInfo, DiffPair, ImpedanceLayer, ImpedanceProfile, PcbClass, PcbComponent, PcbData,
    PcbLayer, PcbRule, Primitive,
)
from parsers.prj_pcb import PrjPcbData, VariantDefinition, Variation
from parsers.sch_doc import ComponentMeta
from services.design_diff import (
    DesignSource, DiffError, build_design_diff, diff_folders, diff_revisions,
    page_design_diff,
)

FIXTURES = Path(__file__).parent / "fixtures"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytest.fixture(autouse=True)
def fresh_cache():
    dd._CACHE.clear()
    yield
    dd._CACHE.clear()


# ---------- synthetic boards and sheets ----------

def layer(v6, name, order, kind="copper", thick=None, height=None, v7=None):
    return PcbLayer(v6_id=v6, name=name, kind=kind, stack_order=order,
                    copper_thick_mil=thick, diel_height_mil=height, v7_id=v7)


def stack(dielectric=4.0):
    return [layer(1, "Top Layer", 0, thick=1.4, v7=16842753),
            layer(None, "D1", 1, "dielectric", height=dielectric),
            layer(32, "Bottom Layer", 2, thick=1.4, v7=16842784)]


def profile(target=50.0, width=5.0):
    return ImpedanceProfile(
        id="{P1}", name="50R", display_name=None, target_ohm=target, tolerance_pct=10.0,
        differential=False,
        layers=[ImpedanceLayer(16842753, None, 16842784, None, width, None, None, None, None,
                               None, 50.1, 0.2, None, True)])


def rule(name, kind="Width", priority=1, uid=None, scope="All", **props):
    return PcbRule(name=name, kind=kind, kind_id=0, enabled=True, priority=priority,
                   scope1=scope, scope2="All", net_scope="DifferentNets", comment="",
                   unique_id=uid, from_schematic=False, properties=props)


def make_pcb(parts, tracks=(), vias=(), rules=(), classes=(), pairs=(), layers=None,
             profiles=(), unit="mil"):
    """parts: (designator, uid, x, y, rotation, side, footprint, {pad: net})."""
    nets: list[str] = []

    def net_id(name):
        if name is None:
            return None
        if name not in nets:
            nets.append(name)
        return nets.index(name)

    components, primitives = [], []
    for index, (designator, uid, x, y, rotation, side, footprint, pads) in enumerate(parts):
        components.append(PcbComponent(
            index=index, refdes=designator, source_uid_tail=uid, channel_path=None, x=x,
            y=y, rotation=rotation, side=side, footprint=footprint, designator=designator,
            source_unique_id=f"\\{uid}" if uid else None))
        for pad, net in pads.items():
            primitives.append(Primitive(kind="pad", layer=1, net=net_id(net), component=index,
                                        x1=x, y1=y, x2=x, y2=y, width=10.0, pad_name=pad))
    for net, x1, y1, x2, y2 in tracks:
        primitives.append(Primitive(kind="track", layer=1, net=net_id(net), component=None,
                                    x1=x1, y1=y1, x2=x2, y2=y2, width=5.0))
    for net, x, y in vias:
        primitives.append(Primitive(kind="via", layer=1, layer_to=32, net=net_id(net),
                                    component=None, x1=x, y1=y, x2=x, y2=y, width=20.0,
                                    hole=10.0))
    board = BoardInfo(origin_x=0.0, origin_y=0.0, sheet_x=0.0, sheet_y=0.0,
                      sheet_width=10000.0, sheet_height=10000.0, display_unit=unit,
                      layers=layers or stack(), stackup_source="v9",
                      impedance_profiles=list(profiles))
    return PcbData(board=board, nets=nets, components=components, primitives=primitives,
                   pours=[], layer_name_to_v6={}, rules=list(rules), classes=list(classes),
                   diff_pairs=list(pairs))


def comp(uid, comment=None, value=None, footprint="R0402", sheet="Main", **params):
    written = {"comment": comment, "value": value, **params}
    return {"unique_id": uid, "unique_ids": [uid], "sheet": sheet, "footprint": footprint,
            "kind": 0, "description": "", "lib_reference": "", "design_item_id": "",
            "params": {k: v for k, v in written.items() if v is not None}}


def source(components=None, pcb=None, variants=None, parameters=None, sheets=("Main",),
           digest="a"):
    """A DesignSource with its parsed sheets and board already in place."""
    prj = PrjPcbData(sheet_paths=[f"{s}.SchDoc" for s in sheets],
                     variants=variants or [VariantDefinition(name="Default")],
                     parameters=parameters or {})
    result = DesignSource(label={"source": "test", "digest": digest},
                          prj_path=Path("Board.PrjPcb"), prj=prj)
    result.sheets = {f"{s.lower()}.schdoc": f"{s}.SchDoc" for s in sheets}
    result.digests = {"<project>": digest, **{key: digest for key in result.sheets}}
    result._schematic = (components or {}, [])
    if pcb is not None:
        result.pcb = ("board.pcbdoc", "Board.PcbDoc")
        result.digests["board.pcbdoc"] = digest
    result._board = (pcb, None if pcb is not None else "the project lists no PcbDoc")
    return result


def changes(model, section, change=None):
    return [e for e in model["changes"]
            if e["section"] == section and (change is None or e["change"] == change)]


def texts(model, section):
    return [e["text"] for e in changes(model, section)]


# ---------- the board ----------

U1_A = {"1": "SIG_A", "2": "SIG_B", "3": "GND", "7": "DATA", "8": "M1", "9": "M2",
        "10": "S", "11": "S", "12": "P1", "13": "P2", "15": "GONE"}
U1_B = {"1": "SIG_A", "2": "SIG_B", "3": "GND", "7": "DATA_IN", "8": "M1", "9": "M1",
        "10": "S", "11": "S2", "12": "P1", "13": "P2", "14": "NEW", "15": None}


def board_a():
    return make_pcb(
        parts=[
            ("U1", "U1ID", 1000.0, 1000.0, 0.0, "top", "QFN", U1_A),
            ("R1", "R1ID", 2000.0, 1000.0, 0.0, "top", "R0402", {"1": "SIG_A", "2": "VCC"}),
            ("R2", "R2ID", 2000.0, 2000.0, 0.0, "top", "R0402", {"1": "SIG_B", "2": "VCC"}),
            ("C1", "C1ID", 3000.0, 1000.0, 0.0, "top", "C0402", {"1": "VCC", "2": "GND"}),
            ("C2", "C2ID", 3000.0, 2000.0, 0.0, "top", "C0402", {"1": "VCC", "2": "GND"}),
            ("J1", "J1ID", 500.0, 500.0, 0.0, "top", "HDR",
             {"4": "DATA", "5": "M1", "6": "M2", "7": "S", "8": "S", "9": "GONE"}),
            ("R3", "R3ID", 100.0, 100.0, 0.0, "top", "R0402", {"1": "P1"}),
            ("C3", "C3ID", 200.0, 100.0, 0.0, "top", "C0402", {"1": "P1"}),
            ("R4", "R4ID", 300.0, 100.0, 0.0, "top", "R0402", {"1": "P2"}),
            ("R6", "R6ID", 400.0, 100.0, 0.0, "top", "R0402", {}),
        ],
        tracks=[("SIG_A", 0.0, 0.0, 100.0, 0.0)],
        rules=[rule("Width", uid="W1", MAXLIMIT="10mil"),
               rule("Clearance", kind="Clearance", uid="CL1", GAP="6mil"),
               rule("WidthA", uid="W2", priority=2), rule("WidthB", uid="W3", priority=3)],
        classes=[PcbClass("PWR", "net", ["VCC", "GND"], False, False, None),
                 PcbClass("OLD", "net", ["SIG_A"], False, False, None),
                 PcbClass("All Nets", "net", [], True, False, None)],
        pairs=[DiffPair("USB", "USB_P", "USB_N", None), DiffPair("ETH", "ETH_P", "ETH_N", None)],
        profiles=[profile()],
    )


def board_b():
    return make_pcb(
        parts=[
            ("U1", "U1ID", 1000.0, 1000.0, 0.0, "top", "QFN", U1_B),
            ("R1", "R1ID", 2100.0, 1000.0, 0.0, "top", "R0402", {"1": "SIG_A", "2": "VCC"}),
            ("R2", "R2ID", 2000.0, 2000.0, 90.0, "top", "R0402", {"1": "SIG_B", "2": "VCC"}),
            ("C1", "C1ID", 3000.0, 1000.0, 0.0, "bottom", "C0402", {"1": "VCC", "2": "GND"}),
            ("J1", "J1ID", 500.0, 500.0, 0.0, "top", "HDR2",
             {"4": "DATA_IN", "5": "M1", "6": "M1", "7": "S", "8": "S2", "9": None}),
            ("R3", "R3ID", 100.0, 100.0, 0.0, "top", "R0402", {"1": "P1"}),
            ("C3", "C3ID", 200.0, 100.0, 0.0, "top", "C0402", {"1": "P2"}),
            ("R4", "R4ID", 300.0, 100.0, 0.0, "top", "R0402", {"1": "P2"}),
            ("R5", "R5ID", 600.0, 100.0, 0.0, "top", "R0402", {"1": "NEW"}),
            ("R6", "R6NEW", 400.0, 100.0, 0.0, "top", "R0402", {}),
        ],
        tracks=[("SIG_A", 0.0, 0.0, 300.0, 0.0)],
        vias=[("SIG_B", 10.0, 10.0), ("SIG_B", 20.0, 20.0)],
        rules=[rule("Width", uid="W1", MAXLIMIT="12mil"),
               rule("Width_USB", uid="W9", scope="InNet('USB_P')"),
               rule("WidthA", uid="W2", priority=3), rule("WidthB", uid="W3", priority=2)],
        classes=[PcbClass("PWR", "net", ["VCC", "GND", "VBAT"], False, False, None),
                 PcbClass("NEWC", "net", ["S"], False, False, None),
                 PcbClass("All Nets", "net", [], True, False, None)],
        pairs=[DiffPair("USB", "USB_P", "USB_N2", None), DiffPair("CAN", "CAN_P", "CAN_N", None)],
        layers=stack(dielectric=5.0),
        profiles=[profile(target=45.0, width=6.0)],
    )


@pytest.fixture(scope="module")
def board_model():
    return build_design_diff(source(pcb=board_a(), digest="a"), source(pcb=board_b(), digest="b"))


def test_nets_renamed_merged_split_added_removed_and_pins_moved(board_model):
    nets = {e["change"]: e for e in changes(board_model, "nets")}
    assert nets["renamed"]["previous_name"] == "DATA" and nets["renamed"]["net"] == "DATA_IN"
    assert nets["merged"]["net"] == "M1" and nets["merged"]["merged"] == ["M1", "M2"]
    assert nets["split"]["net"] == "S" and nets["split"]["into"] == ["S", "S2"]
    assert nets["added"]["net"] == "NEW"
    assert nets["removed"]["net"] == "GONE"
    assert nets["pin_moved"]["pin"] == "C3-1"
    assert (nets["pin_moved"]["from"], nets["pin_moved"]["to"]) == ("P1", "P2")
    assert "Move Pin C3-1 from P1 to P2" in texts(board_model, "nets")
    # Nets in a merge or split are not also reported as added or removed.
    assert not any(e["net"] in ("M2", "S2") for e in changes(board_model, "nets", "added")
                   + changes(board_model, "nets", "removed"))
    assert board_model["summary"]["nets"] == {
        "split": 1, "merged": 1, "renamed": 1, "removed": 1, "added": 1, "pin_moved": 1}


def test_ict_hazards_come_first(board_model):
    assert board_model["changes"][0]["section"] == "ict_hazards"
    hazards = {e.get("net") or e.get("designator"): e for e in changes(board_model, "ict_hazards")}
    # Kept their name, changed their pins.
    assert {"M1", "S", "P1", "P2", "VCC", "GND"} <= set(hazards)
    assert hazards["P1"]["pins_removed"] == ["C3-1"]
    assert hazards["M1"]["pins_added_count"] == 2
    # VCC and GND lost C2's pads only: a removed part.
    assert hazards["GND"]["only_parts_added_or_removed"] is True
    assert "only_parts_added_or_removed" not in hazards["P1"]
    # R6 is now another part: matched by designator only, UniqueIds differ.
    assert hazards["R6"]["change"] == "designator_reused"
    assert hazards["R6"]["where"] == ["pcb"]
    assert hazards["R6"]["same_comment_and_footprint"] is True
    assert "likely deleted and placed again" in hazards["R6"]["text"]
    assert "DATA" not in hazards and "DATA_IN" not in hazards


def test_placement_moves_rotations_flips_and_footprints(board_model, ):
    place = {(e["change"], e["designator"]): e for e in changes(board_model, "placement")}
    moved = place[("moved", "R1")]
    assert (moved["dx_mil"], moved["dy_mil"]) == (100.0, 0.0)
    assert place[("rotated", "R2")]["rotation"] == {"from": 0.0, "to": 90.0}
    assert place[("flipped", "C1")]["side"] == {"from": "top", "to": "bottom"}
    assert place[("footprint", "J1")]["footprint"] == {"from": "HDR", "to": "HDR2"}
    assert ("removed", "C2") in place and ("added", "R5") in place
    assert not any(d == "U1" for _, d in place)


def test_rules_added_removed_changed_and_priority_order(board_model):
    rules = {(e["change"], e["rule"]): e for e in changes(board_model, "rules")}
    assert ("added", "Width_USB") in rules and ("removed", "Clearance") in rules
    width = rules[("changed", "Width")]["changes"]
    assert {"field": "values.max_width", "from_mil": 10.0, "to_mil": 12.0} in width
    # WidthA and WidthB swapped order: a priority change. Width_USB's arrival shifts
    # nothing that is reported.
    assert rules[("changed", "WidthA")]["changes"] == [
        {"field": "priority", "from": 2, "to": 3}]
    assert not any(c["field"] == "priority" for c in width)


def test_classes_pairs_stackup_and_routing(board_model):
    classes = {(e["change"], e["name"]): e for e in changes(board_model, "classes")}
    assert classes[("members", "PWR")]["members_added"] == ["VBAT"]
    assert ("removed", "OLD") in classes and ("added", "NEWC") in classes
    assert not any(name == "All Nets" for _, name in classes)
    pairs = {(e["change"], e["pair"]): e for e in changes(board_model, "diff_pairs")}
    assert pairs[("nets", "USB")]["negative_net"] == {"from": "USB_N", "to": "USB_N2"}
    assert ("removed", "ETH") in pairs and ("added", "CAN") in pairs
    stackup = {e["change"]: e for e in changes(board_model, "stackup")}
    assert stackup["layer_changed"]["changes"] == [
        {"field": "diel_height", "from_mil": 4.0, "to_mil": 5.0}]
    assert (stackup["thickness"]["thickness_from_mil"],
            stackup["thickness"]["thickness_to_mil"]) == (6.8, 7.8)
    profile_changes = stackup["profile_changed"]["changes"]
    assert {"field": "target_ohm", "from": 50.0, "to": 45.0} in profile_changes
    assert {"field": "Top Layer.width", "from_mil": 5.0, "to_mil": 6.0} in profile_changes
    routing = {e["net"]: e for e in changes(board_model, "routing")}
    assert routing["SIG_A"]["length_delta_mil"] == 200.0
    assert routing["SIG_B"]["vias"] == {"from": 0, "to": 2}
    assert board_model["routing_totals"] == {"length_delta_mil": 200.0, "via_delta": 2}


def test_identical_boards_report_nothing():
    model = build_design_diff(source(pcb=board_a(), digest="a"),
                              source(pcb=board_a(), digest="b"))
    assert model["no_changes"] is True and model["changes"] == []


def test_identical_files_are_not_parsed():
    a, b = source(pcb=board_a()), source(pcb=board_b())
    a._board = b._board = None  # parsing would fail: the digests say identical
    model = build_design_diff(a, b)
    assert model["unchanged_or_not_compared"]["board"] == "the PcbDoc is identical"


def test_a_net_without_pads_matches_by_name_with_name_matching_off():
    """A net on tracks only has no members to match: with the Comparator's name matching
    off it was reported removed and added again."""
    pads = {"1": "A", "2": "B"}
    a = source(pcb=make_pcb([("R1", "R1ID", 0.0, 0.0, 0.0, "top", "R0402", pads)],
                            tracks=[("ROUTED", 0.0, 0.0, 10.0, 0.0)]))
    b = source(pcb=make_pcb([("R1", "R1ID", 50.0, 0.0, 0.0, "top", "R0402", pads)],
                            tracks=[("ROUTED", 0.0, 0.0, 10.0, 0.0)]), digest="b")
    model = build_design_diff(a, b, net_options={"name_matching": False})
    assert [(e["section"], e["change"]) for e in model["changes"]] == [("placement", "moved")]


def test_units_follow_the_board_or_the_request():
    a, b = source(pcb=board_a()), source(pcb=board_b(), digest="b")
    b._board[0].board.display_unit = "mm"
    model = build_design_diff(a, b)
    assert model["units"] == "mm"
    moved = changes(model, "placement", "moved")[0]
    assert moved["text"] == "Move R1 by (+2.54 mm, +0 mm) to (53.34 mm, 25.4 mm)"
    assert build_design_diff(a, b, units="mil")["units"] == "mil"


def test_without_a_board_on_one_side_the_board_is_not_compared():
    model = build_design_diff(source(pcb=board_a()), source(pcb=None, digest="b"))
    assert "the project lists no PcbDoc" in model["unchanged_or_not_compared"]["board"]
    assert not changes(model, "nets")


def test_board_edge_cases():
    """Angles wrap, logos match by position, net names match case-insensitively, a split
    that also renames is one entry, one moved pin is no split, pour copper is no routing,
    superclasses and priority shifts from a new rule are quiet, a reordered stack is not."""
    x8 = {str(n): "X" for n in range(1, 9)}
    a = make_pcb(
        parts=[("R1", "R1ID", 1000.0, 1000.0, 0.0, "top", "R0402", {"1": "clk"}),
               ("R2", "R2ID", 2000.0, 1000.0, 0.0, "top", "R0402", {"1": "clk"}),
               ("U1", "U1ID", 3000.0, 1000.0, 0.0, "top", "QFN", x8),
               ("U2", "U2ID", 4000.0, 1000.0, 0.0, "top", "SOT", {"1": "W", "2": "W", "3": "W"}),
               ("", None, 5000.0, 5000.0, 0.0, "top", "LOGO", {})],
        rules=[rule("Width", uid="W1", priority=1)],
        classes=[PcbClass("All Nets", "net", ["X"], True, False, None)])
    b = make_pcb(
        parts=[("R1", "R1ID", 1000.0, 1000.0, 360.0, "top", "R0402", {"1": "CLK"}),
               ("R3", "R3ID", 2500.0, 1000.0, 0.0, "top", "R0402", {"1": "CLK", "2": "NEW1"}),
               ("U1", "U1ID", 3000.0, 1000.0, 0.0, "top", "QFN",
                {**{str(n): "Y" for n in range(1, 7)}, "7": "Z", "8": "Z"}),
               ("U2", "U2ID", 4000.0, 1000.0, 0.0, "top", "SOT",
                {"1": "W", "2": "W", "3": "NEW1"}),
               ("", None, 5000.0, 5000.0, 0.0, "top", "LOGO", {})],
        rules=[rule("WidthNew", uid="W5", priority=1), rule("Width", uid="W1", priority=2)],
        classes=[PcbClass("All Nets", "net", ["Y", "Z", "W"], True, False, None)],
        layers=[layer(32, "Bottom Layer", 0, thick=1.4), layer(None, "D1", 1, "dielectric",
                                                               height=4.0),
                layer(1, "Top Layer", 2, thick=1.4)])
    b.primitives.append(Primitive(kind="track", layer=1, net=b.nets.index("W"), component=None,
                                  x1=0.0, y1=0.0, x2=500.0, y2=0.0, width=5.0, pour_id=0))
    model = build_design_diff(source(pcb=a), source(pcb=b, digest="b"))
    placement = {(e["change"], e["designator"]) for e in changes(model, "placement")}
    assert placement == {("removed", "R2"), ("added", "R3")}
    nets = {(e["change"], e["net"]) for e in changes(model, "nets") if "net" in e}
    assert nets == {("renamed", "CLK"), ("split", "X"), ("added", "NEW1")}
    assert changes(model, "nets", "split")[0]["into"] == ["Y", "Z"]
    assert [e["pin"] for e in changes(model, "nets", "pin_moved")] == ["U2-3"]
    assert [(e["change"], e["rule"]) for e in changes(model, "rules")] == [
        ("added", "WidthNew")]
    assert not changes(model, "classes") and not changes(model, "routing")
    assert [e["change"] for e in changes(model, "stackup")] == ["reordered"]


# ---------- the schematic ----------

def sheets_a():
    return {
        "R1": comp("A1", "10k", "10k"),
        "R2": comp("A2", "1k", "1k"),
        "C1": comp("C1ID", "100nF", footprint="C0402"),
        "C2": comp("C2ID", "1uF", footprint="C0402", tolerance="10%"),
        "U1": comp("U1ID", "MCU", footprint="QFN"),
        "R5": comp("X5", "0R"),
    }


def sheets_b():
    return {
        "R1": comp("A1", "4k7", "4k7"),
        "R20": comp("A2", "1k", "1k"),
        "C1": comp("C1ID", "100nF", footprint="C0603"),
        "C2": comp("C2ID", "1uF", footprint="C0402", tolerance="5%", voltage="16V"),
        "U9": comp("U9ID", "LDO", footprint="SOT23"),
        "R5": comp("Y5", "0R"),
    }


PROD_A = VariantDefinition(name="Prod", variations=[
    Variation(designator="R1", unique_id="\\A1", kind=1)])
PROD_B = VariantDefinition(name="Prod", variations=[
    Variation(designator="R20", unique_id="\\A2", kind=1),
    Variation(designator="C2", unique_id="\\C2ID", kind=2, alternate_part="CAP-2",
              alt={"DesignItemId": "CAP-2"})],
    param_variations={"C2": {"voltage": ("Voltage", "25V")}})
LITE = VariantDefinition(name="Lite", variations=[
    Variation(designator="U9", unique_id="\\U9ID", kind=1)])


@pytest.fixture(scope="module")
def sheet_model():
    a = source(sheets_a(), variants=[PROD_A], parameters={"Revision": "A"})
    b = source(sheets_b(), variants=[PROD_B, LITE], parameters={"Revision": "B"},
               sheets=("Main", "Power"), digest="b")
    return build_design_diff(a, b)


def test_components_added_removed_redesignated_revalued_footprint_parameters(sheet_model):
    found = {(e["change"], e["designator"]): e for e in changes(sheet_model, "components")}
    assert found[("revalued", "R1")]["comment"] == {"from": "10k", "to": "4k7"}
    assert found[("revalued", "R1")]["value"] == {"from": "10k", "to": "4k7"}
    assert found[("redesignated", "R20")]["previous_designator"] == "R2"
    assert found[("footprint", "C1")]["footprint"] == {"from": "C0402", "to": "C0603"}
    assert found[("parameters", "C2")]["parameters"] == [
        {"name": "tolerance", "from": "10%", "to": "5%"},
        {"name": "voltage", "from": None, "to": "16V"}]
    assert ("removed", "U1") in found and ("added", "U9") in found
    assert "Change Designator R2 -> R20" in texts(sheet_model, "components")


def test_reused_schematic_designator_is_a_hazard(sheet_model):
    hazards = changes(sheet_model, "ict_hazards", "designator_reused")
    assert [h["designator"] for h in hazards] == ["R5"]
    assert hazards[0]["where"] == ["schematic"]


def test_variants_fitting_alternates_and_varied_parameters(sheet_model):
    variants = texts(sheet_model, "variants")
    assert "Add Variant Lite (1 Not Fitted, 0 alternate parts)" in variants
    assert "Variant Prod: R1 fitted (was Not Fitted)" in variants
    assert "Variant Prod: R20 Not Fitted (was fitted)" in variants
    assert "Variant Prod: C2 alternate part CAP-2 (was fitted)" in variants
    assert "Variant Prod: C2 varied parameters: add voltage = 25V" in variants


def test_bom_lines_per_variant(sheet_model):
    bom = changes(sheet_model, "bom")
    base = [e for e in bom if e["variant"] == "[No Variations]"]
    lines = {(e["change"], e["line"]): e for e in base}
    assert ("line_added", "4k7, R0402") in lines and ("line_removed", "10k, R0402") in lines
    assert lines[("line_added", "LDO, SOT23")]["designators_added"] == ["U9"]
    assert lines[("quantity", "1k, R0402")]["designators_added"] == ["R20"]
    # Prod fits R1 now and leaves out R20: its changes differ from the base BOM's.
    prod = {(e["change"], e["line"]) for e in bom if e["variant"] == "Prod"}
    assert ("line_removed", "1k, R0402") in prod
    assert ("line_added", "4k7, R0402") in prod


def test_project_sheets_and_parameters(sheet_model):
    assert "Add sheet Power.SchDoc to the project" in texts(sheet_model, "project")
    parameters = changes(sheet_model, "project", "parameters")[0]["parameters"]
    assert parameters == [{"name": "Revision", "from": "A", "to": "B"}]


def test_schematic_edge_cases():
    """A project without variants has no 'Default' variant to remove, unannotated parts
    never match by designator, Comment special strings resolve before comparing, and a
    variant whose BOM changes equal the base BOM's is only named."""
    a = source({"R?": comp("Q1", "1k"),
                "C1": comp("C1ID", "=Rating", footprint="C0402", rating="16V")})
    b = source({"R?": comp("Q2", "1k"),
                "C1": comp("C1ID", "=Rating", footprint="C0402", rating="25V")},
               variants=[VariantDefinition(name="Lite")], digest="b")
    model = build_design_diff(a, b)
    assert texts(model, "variants") == ["Add Variant Lite (0 Not Fitted, 0 alternate parts)"]
    found = {(e["change"], e["designator"]): e for e in changes(model, "components")}
    assert ("removed", "R?") in found and ("added", "R?") in found
    assert found[("revalued", "C1")]["comment"] == {"from": "16V", "to": "25V"}

    plain = VariantDefinition(name="Plain", variations=[
        Variation(designator="R9", unique_id="\\R9ID", kind=1)])
    a = source({"R9": comp("R9ID", "1k")}, variants=[plain])
    b = source({"R9": comp("R9ID", "1k"), "R7": comp("R7ID", "10k")}, variants=[plain],
               digest="b")
    model = build_design_diff(a, b)
    assert model["bom_variants_same_as_base"] == ["Plain"]
    assert {e["variant"] for e in changes(model, "bom")} == {"[No Variations]"}


def test_identical_schematic_is_skipped():
    a, b = source(sheets_a()), source(sheets_a())
    a._schematic = b._schematic = None
    model = build_design_diff(a, b)
    assert "identical" in model["unchanged_or_not_compared"]["schematic"]


def test_compile_masked_parts_are_left_out():
    masked = {**sheets_a(), "R9": {**comp("R9ID", "1k"), "compile_masked": True}}
    model = build_design_diff(source(sheets_a()), source(masked, digest="b"))
    assert not changes(model, "components")


# ---------- paging ----------

def test_paging_and_sections(board_model):
    first = page_design_diff(board_model, 0, 5)
    assert first["summary"] == board_model["summary"] and len(first["changes"]) == 5
    assert first["has_more"] is True and first["next_offset"] == 5
    second = page_design_diff(board_model, 5, 5)
    assert "summary" not in second and second["changes"][0] == board_model["changes"][5]
    rules = page_design_diff(board_model, 0, 100, section="rules")
    assert rules["section"] == "rules" and rules["has_more"] is False
    assert {e["change"] for e in rules["changes"]} == {"added", "removed", "changed"}
    assert "section" not in rules["changes"][0]


def test_tool_sections_match_the_service():
    assert typing.get_args(main.DiffSection) == dd.SECTIONS


# ---------- Git ----------

@pytest.fixture
def isolated_git(tmp_path, monkeypatch):
    """No user or system git config, fixed identities."""
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.com")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


PRJ = ("[Design]\n[Document1]\nDocumentPath=Main.SchDoc\n"
       "[Document2]\nDocumentPath=Board.PcbDoc\n")


def write_json_sheet(path: Path, parts: dict) -> None:
    path.write_text(json.dumps(parts), encoding="utf-8")


def json_sheet(path):
    """parse_sch_doc for the JSON sheets: {designator: [uid, comment, footprint]}."""
    parts = json.loads(Path(path).read_text(encoding="utf-8"))
    return {d: ComponentMeta(refdes=d, unique_id=uid, unique_ids=[uid], footprint=fp,
                             params={"comment": text}) for d, (uid, text, fp) in parts.items()}


def json_board(path):
    """parse_pcb_doc for the JSON boards: make_pcb's parts."""
    return make_pcb([tuple(p) for p in json.loads(Path(path).read_text(encoding="utf-8"))])


@pytest.fixture
def json_parsers(monkeypatch):
    monkeypatch.setattr(dd, "parse_sch_doc", json_sheet)
    monkeypatch.setattr(dd, "parse_pcb_doc", json_board)


SHEET_1 = {"R1": ["A1", "10k", "R0402"], "C1": ["C1ID", "100nF", "C0402"]}
SHEET_2 = {"R1": ["A1", "4k7", "R0402"], "C1": ["C1ID", "100nF", "C0402"],
           "U1": ["U1ID", "MCU", "QFN"]}
BOARD_1 = [["R1", "A1", 0.0, 0.0, 0.0, "top", "R0402", {"1": "N1", "2": "GND"}]]
BOARD_2 = [["R1", "A1", 50.0, 0.0, 0.0, "top", "R0402", {"1": "N1", "2": "GND"}]]


def commit_all(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD").strip()


@pytest.fixture
def clone(tmp_path, isolated_git, json_parsers):
    """A clone with the project in 'Board [Rev A]': two design commits and a README
    commit on top. Returns (repo root, project folder, [design commit hashes])."""
    root = tmp_path / "repo"
    project = root / "Board [Rev A]"
    project.mkdir(parents=True)
    git(root, "init", "-q", "-b", "master")
    (project / "Board.PrjPcb").write_text(PRJ, encoding="utf-8")
    write_json_sheet(project / "Main.SchDoc", SHEET_1)
    (project / "Board.PcbDoc").write_text(json.dumps(BOARD_1), encoding="utf-8")
    first = commit_all(root, "First release")
    write_json_sheet(project / "Main.SchDoc", SHEET_2)
    (project / "Board.PcbDoc").write_text(json.dumps(BOARD_2), encoding="utf-8")
    second = commit_all(root, "Add MCU")
    (root / "README.txt").write_text("notes", encoding="utf-8")
    commit_all(root, "Docs only")
    return root, project, [first, second]


def snapshot(root: Path) -> dict:
    """Every file in the work tree and .git with its mtime and size."""
    return {str(p.relative_to(root)): (p.stat().st_mtime_ns, p.stat().st_size)
            for p in root.rglob("*") if p.is_file()}


@needs_git
def test_no_revisions_lists_commits_and_compares_the_last_design_commit(clone):
    root, project, (first, second) = clone
    before = snapshot(root)
    model = diff_revisions(str(project / "Board.PrjPcb"))
    assert [c["hash"] for c in model["recent_commits"]] == [second, first]
    assert model["recent_commits"][0]["subject"] == "Add MCU"
    assert model["recent_commits"][0]["author"] == "Test"
    assert model["a"]["hash"] == second and model["b"]["source"] == "working"
    assert model["no_changes"] is True and "rev_a" in model["no_changes_hint"]
    assert snapshot(root) == before


@needs_git
def test_a_commit_against_its_parent(clone):
    root, project, (first, second) = clone
    before = snapshot(root)
    model = diff_revisions(str(project / "Board.PrjPcb"), rev_b=second[:10])
    assert model["a"]["hash"] == first and model["b"]["hash"] == second
    assert "recent_commits" not in model
    assert "Change Comment R1: 10k -> 4k7" in texts(model, "components")
    assert [e["designator"] for e in changes(model, "components", "added")] == ["U1"]
    assert changes(model, "placement", "moved")[0]["dx_mil"] == 50.0
    assert snapshot(root) == before


@needs_git
def test_working_tree_changes_and_tags(clone):
    root, project, (first, second) = clone
    git(root, "tag", "rel-1", first)
    write_json_sheet(project / "Main.SchDoc", {**SHEET_2, "R1": ["A1", "1k", "R0402"]})
    before = snapshot(root)
    model = diff_revisions(str(project / "Board.PrjPcb"), rev_a="rel-1")
    assert model["a"]["hash"] == first and model["b"]["source"] == "working"
    assert "Change Comment R1: 10k -> 1k" in texts(model, "components")
    # Uncommitted edits against the last design commit.
    model = diff_revisions(str(project / "Board.PrjPcb"), rev_a="HEAD")
    assert "Change Comment R1: 4k7 -> 1k" in texts(model, "components")
    assert "unchanged_or_not_compared" in model  # the PcbDoc is unchanged
    reverse = diff_revisions(str(project / "Board.PrjPcb"), rev_a="working", rev_b="rel-1")
    assert "Change Comment R1: 1k -> 10k" in texts(reverse, "components")
    assert snapshot(root) == before


@needs_git
def test_temporary_files_are_deleted(clone, monkeypatch):
    root, project, (first, second) = clone
    made = []
    real = dd.tempfile.mkdtemp

    def mkdtemp(**kwargs):
        made.append(Path(real(**kwargs)))
        return str(made[-1])

    monkeypatch.setattr(dd.tempfile, "mkdtemp", mkdtemp)
    diff_revisions(str(project / "Board.PrjPcb"), rev_a=first, rev_b=second)
    assert made and not made[0].exists()

    def fail(*args, **kwargs):
        raise RuntimeError("the diff failed")

    monkeypatch.setattr(dd, "build_design_diff", fail)
    dd._CACHE.clear()
    with pytest.raises(RuntimeError):
        diff_revisions(str(project / "Board.PrjPcb"), rev_a=first, rev_b=second)
    assert len(made) == 2 and not made[1].exists()


@needs_git
def test_a_temporary_folder_inside_the_clone_is_refused_before_it_is_made(clone,
                                                                         monkeypatch):
    root, project, (first, second) = clone
    inside = root / "tmp"
    inside.mkdir()
    made = []
    real = dd.tempfile.mkdtemp

    def mkdtemp(**kwargs):
        made.append(real(**kwargs))
        return made[-1]

    monkeypatch.setattr(dd.tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(dd.tempfile, "tempdir", str(inside))
    before = snapshot(root)
    with pytest.raises(DiffError) as error:
        diff_revisions(str(project / "Board.PrjPcb"), rev_a=first, rev_b=second)
    assert error.value.payload["error"] == "temp_inside_clone"
    assert made == [] and list(inside.iterdir()) == []
    assert snapshot(root) == before


def test_a_project_path_leaving_the_repository_is_not_written(tmp_path):
    """A tree entry is any byte string; one that resolves outside the temporary folder is
    refused before anything is written."""
    class Repo:
        prefix = ""

        def tree(self, sha):
            return {"board.prjpcb": ("../escaped/Board.PrjPcb", "1" * 40)}

        def blobs(self, oids):
            return {oid: b"[Design]\n" for oid in oids}

        def commit(self, sha):
            return {"commit": sha[:7], "hash": sha}

    temp = tmp_path / "temp" / "rev"
    temp.mkdir(parents=True)
    with pytest.raises(DiffError) as error:
        dd.git_source(Repo(), "a" * 40, "Board.PrjPcb", temp)
    assert error.value.payload["error"] == "unsafe_path"
    assert not (tmp_path / "temp" / "escaped").exists()


@needs_git
def test_bad_revisions(clone):
    root, project, (first, _) = clone
    prj = str(project / "Board.PrjPcb")
    with pytest.raises(DiffError) as error:
        diff_revisions(prj, rev_a="no-such-tag")
    assert error.value.payload["error"] == "revision_not_found"
    for bad in ("--output=x", "--all", "-n1", "HEAD..x y", ""):
        with pytest.raises(DiffError) as error:
            diff_revisions(prj, rev_a=bad or " ")
        assert error.value.payload["error"] == "invalid_revision"
    with pytest.raises(DiffError) as error:
        diff_revisions(prj, rev_b=first)
    assert error.value.payload["error"] == "no_parent_commit"


@needs_git
def test_project_missing_from_a_revision(clone):
    root, project, _ = clone
    other = root / "Other"
    other.mkdir()
    (other / "Other.PrjPcb").write_text(PRJ, encoding="utf-8")
    write_json_sheet(other / "Main.SchDoc", SHEET_1)
    head = commit_all(root, "Other project")
    with pytest.raises(DiffError) as error:
        diff_revisions(str(other / "Other.PrjPcb"), rev_a=f"{head}~1", rev_b=head)
    assert error.value.payload["error"] == "project_not_in_revision"


@needs_git
def test_not_a_git_repository(tmp_path, isolated_git):
    folder = tmp_path / "plain"
    folder.mkdir()
    (folder / "Board.PrjPcb").write_text(PRJ, encoding="utf-8")
    with pytest.raises(DiffError) as error:
        diff_revisions(str(folder / "Board.PrjPcb"))
    assert error.value.payload["error"] in ("not_a_git_repository", "git_failed")


@needs_git
def test_lfs_pointers_are_not_read(clone):
    root, project, _ = clone
    (project / "Main.SchDoc").write_text(
        "version https://git-lfs.github.com/spec/v1\noid sha256:00\nsize 1\n",
        encoding="utf-8")
    commit_all(root, "LFS")
    model = diff_revisions(str(project / "Board.PrjPcb"), rev_b="HEAD")
    assert "Git LFS" in model["documents_not_read"]["b"]["main.schdoc"]


@needs_git
def test_a_project_file_missing_from_the_object_store_is_a_clear_error(clone, monkeypatch):
    root, project, commits = clone
    monkeypatch.setattr(dd._Repo, "blobs", lambda self, oids: {})  # a partial clone
    with pytest.raises(DiffError) as error:
        diff_revisions(str(project / "Board.PrjPcb"), rev_a=commits[0], rev_b=commits[1])
    assert error.value.payload["error"] == "missing_object"


def test_only_read_commands_are_run(tmp_path):
    with pytest.raises(dd.GitError):
        dd._run_git("git", tmp_path, "checkout", "-f")


@needs_git
def test_real_sheets_committed_in_git(tmp_path, isolated_git):
    """The fixture sheets, committed one after the other under one name, parsed for real."""
    root = tmp_path / "real"
    root.mkdir()
    git(root, "init", "-q", "-b", "master")
    (root / "Board.PrjPcb").write_text("[Design]\n[Document1]\nDocumentPath=Board.SchDoc\n",
                                       encoding="utf-8")
    shutil.copy(FIXTURES / "sheet1.SchDoc", root / "Board.SchDoc")
    commit_all(root, "one")
    shutil.copy(FIXTURES / "sheet2.SchDoc", root / "Board.SchDoc")
    commit_all(root, "two")
    before = snapshot(root)
    model = diff_revisions(str(root / "Board.PrjPcb"), rev_b="HEAD")
    summary = model["summary"]["components"]
    assert summary.get("added") and summary.get("removed")
    assert "the project lists no PcbDoc" in model["unchanged_or_not_compared"]["board"]
    assert snapshot(root) == before


@needs_git
def test_documents_outside_the_project_folder(tmp_path, isolated_git, json_parsers):
    """A sheet the project lists from another folder of the repository: its commits are
    design commits, and each revision reads it."""
    root = tmp_path / "repo"
    project, shared = root / "Board", root / "Shared"
    project.mkdir(parents=True)
    shared.mkdir()
    git(root, "init", "-q", "-b", "master")
    (project / "Board.PrjPcb").write_text(
        "[Design]\n[Document1]\nDocumentPath=..\\Shared\\Main.SchDoc\n", encoding="utf-8")
    write_json_sheet(shared / "Main.SchDoc", SHEET_1)
    first = commit_all(root, "one")
    write_json_sheet(shared / "Main.SchDoc", SHEET_2)
    second = commit_all(root, "shared sheet only")
    before = snapshot(root)
    model = diff_revisions(str(project / "Board.PrjPcb"))
    assert [c["hash"] for c in model["recent_commits"]] == [second, first]
    model = diff_revisions(str(project / "Board.PrjPcb"), rev_b="HEAD")
    assert "Change Comment R1: 10k -> 4k7" in texts(model, "components")
    assert snapshot(root) == before


# ---------- folders and the tool ----------

def make_folder(path: Path, sheet: dict, board: list) -> Path:
    path.mkdir(parents=True)
    (path / "Board.PrjPcb").write_text(PRJ, encoding="utf-8")
    write_json_sheet(path / "Main.SchDoc", sheet)
    (path / "Board.PcbDoc").write_text(json.dumps(board), encoding="utf-8")
    return path


def test_two_folders(tmp_path, json_parsers):
    a = make_folder(tmp_path / "rel1", SHEET_1, BOARD_1)
    b = make_folder(tmp_path / "rel2", SHEET_2, BOARD_2)
    model = diff_folders(str(a), str(b))
    assert model["a"]["source"] == "folder"
    assert "Change Comment R1: 10k -> 4k7" in texts(model, "components")
    assert changes(model, "placement", "moved")
    with pytest.raises(DiffError) as error:
        diff_folders(str(tmp_path / "nowhere"), str(b))
    assert error.value.payload["error"] == "folder_not_found"
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(DiffError) as error:
        diff_folders(str(empty), str(b))
    assert error.value.payload["error"] == "no_prjpcb"


def test_a_missing_sheet_is_reported(tmp_path, json_parsers):
    a = make_folder(tmp_path / "rel1", SHEET_1, BOARD_1)
    b = make_folder(tmp_path / "rel2", SHEET_2, BOARD_1)
    (b / "Main.SchDoc").unlink()
    model = diff_folders(str(a), str(b))
    assert model["documents_not_read"]["b"] == {"main.schdoc": "file not found"}
    assert model["sheet_errors"]["b"] == [{"sheet": "Main", "error": "file not found"}]


@pytest.fixture
def loaded(tmp_path, monkeypatch, json_parsers):
    folder = make_folder(tmp_path / "work", SHEET_2, BOARD_2)
    monkeypatch.setattr(main, "_project", {"name": "Board", "root_dir": str(folder),
                                           "prj_pcb_path": str(folder / "Board.PrjPcb")})
    return folder


def test_tool_folder_mode_paging_and_units(tmp_path, loaded):
    old = make_folder(tmp_path / "old", SHEET_1, BOARD_1)
    result = json.loads(main.diff_design(project_dir_a=str(old), units="mm"))
    assert result["units"] == "mm" and result["b"]["folder"] == str(loaded)
    moved = next(e for e in result["changes"] if e["change"] == "moved")
    assert moved["dx_mm"] == 1.27 and "x_mil" not in moved
    page = json.loads(main.diff_design(project_dir_a=str(old), section="components",
                                       offset=1))
    assert page["offset"] == 1 and "summary" not in page
    assert all("section" not in e for e in page["changes"])


@needs_git
def test_tool_git_mode(clone, monkeypatch):
    root, project, (first, second) = clone
    monkeypatch.setattr(main, "_project", {"name": "Board", "root_dir": str(project),
                                           "prj_pcb_path": str(project / "Board.PrjPcb")})
    result = json.loads(main.diff_design(rev_a=first))
    assert result["a"]["hash"] == first and result["total_changes"] > 0
    assert json.loads(main.diff_design(rev_a="nope"))["error"] == "revision_not_found"


def test_tool_errors(monkeypatch):
    monkeypatch.setattr(main, "_project", None)
    assert json.loads(main.diff_design())["error"] == "no_project"
    assert json.loads(main.diff_design(project_dir_b="x"))["error"] == "folders_required"
