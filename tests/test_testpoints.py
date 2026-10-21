"""Test access (check_testpoints, services/testpoints): probe sites, coverage, the
Testpoint rules, the profile's geometry checks and the tool, on a synthetic board and on
Altium's examples (skipped when absent)."""

import dataclasses
import json
from pathlib import Path

import pytest

import server.main as main_mod
from server.parsers.pcb_doc import (
    BareHole,
    PcbData,
    PcbRule,
    Primitive,
    ShapeVertex,
    ViaStack,
    parse_pcb_doc,
)
from server.parsers.prj_pcb import VariantDefinition, VariantState
from server.services import testpoints as tp
from server.services.pcb_index import PcbIndex
from tests.test_dfm_assembly import _body, _part, _rect, _smd, _tht
from tests.test_pcb_index import _board

MM = 1 / 0.0254
NETS = ["GND", "+3V3", "SIG1", "SIG2", "SIG3", "SIG4"]
N = {name: number for number, name in enumerate(NETS)}
LAYERS = {"top layer": 1, "bottom layer": 32, "top": 1, "bottom": 32}
OUTLINE = [ShapeVertex(0, 0), ShapeVertex(4000, 0), ShapeVertex(4000, 3000),
           ShapeVertex(0, 3000)]
EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")
SPIRIT = EXAMPLES / "SpiritLevel-SL1" / "SL1 Xilinx Spartan-IIE PQ208 Rev1.02.PcbDoc"
SENTINEL = EXAMPLES / "Bluetooth Sentinel" / "Bluetooth_Sentinel.PcbDoc"


def _rule(kind, scope1="All", priority=1, kind_id=0, name=None, **props):
    return PcbRule(
        name=name or kind, kind=kind, kind_id=kind_id, enabled=True, priority=priority,
        scope1=scope1, scope2="All", net_scope="AnyNet", comment="", unique_id=None,
        from_schematic=False, properties=props,
    )


STYLE = dict(MINSIZE="40mil", MAXSIZE="100mil", MINHOLESIZE="0mil", MAXHOLESIZE="40mil",
             ALLOWSIDETOP="TRUE", ALLOWSIDEBOTTOM="TRUE", TESTPOINTUNDERCOMPONENT="TRUE")
RULES = [
    _rule("FabricationTestPointUsage", kind_id=44, VALID="0", ALLOWMULTIPLE="FALSE"),
    _rule("AssemblyTestPointUsage", kind_id=58),
    _rule("FabricationTestpoint", kind_id=43, **STYLE),
]


def _via(net, x, y, top=False, bottom=False, diameter=40.0, tented=(False, False)):
    return Primitive(
        kind="via", layer=1, layer_to=32, net=N[net], component=None, x1=x, y1=y, x2=x,
        y2=y, width=diameter, hole=12.0, via=ViaStack(*tented, top, bottom),
        test_fab_top=top, test_fab_bottom=bottom,
    )


def _pcb(rules=RULES, holes=((300, 300), (3700, 2700)), extra=()) -> PcbData:
    """U1 (top, a 1 mm body) and its six nets, TP1 (top, 40 mil) and TP2 (bottom, 30 mil),
    J1 (a connector 8 mm tall), R1, two tooling holes and five testpoint vias: +3V3 marked
    bottom and tented, SIG3 marked top 100 mil from the edge (30 mil), GND 50 mil from TP1,
    SIG2 under U1's body and SIG1 150 mil from J1's body."""
    components = [
        _part(0, "U1", 2000, 1500), _part(1, "TP1", 1000, 1000),
        _part(2, "TP2", 1000, 2000, side="bottom"), _part(3, "J1", 2600, 600),
        _part(4, "R1", 3000, 2500),
    ]
    u1 = [("SIG1", 1920, 1420), ("SIG2", 1920, 1480), ("SIG3", 1920, 1540),
          ("+3V3", 2080, 1420), ("GND", 2080, 1480), ("SIG4", 2080, 1540)]
    primitives = [_smd(0, x, y, 10.0, net=N[net], name=str(number))
                  for number, (net, x, y) in enumerate(u1, start=1)]
    primitives += [
        _smd(1, 1000, 1000, 40.0, net=N["SIG1"], name="1"),
        _smd(2, 1000, 2000, 30.0, net=N["SIG2"], layer=32, name="1"),
        _tht(3, 2550, 600, net=N["+3V3"]), _tht(3, 2600, 600, net=N["GND"]),
        _tht(3, 2650, 600, net=N["SIG4"]),
        _smd(4, 2980, 2500, net=N["SIG3"], name="1"), _smd(4, 3020, 2500, net=N["GND"],
                                                           name="2"),
        _via("+3V3", 3000, 1500, bottom=True, tented=(True, True)),
        _via("SIG3", 100, 1500, top=True, diameter=30.0),
        _via("GND", 1050, 1000, top=True),
        _via("SIG2", 2000, 1450, top=True),
        _via("SIG1", 2600, 850, top=True),
        *extra,
    ]
    bodies = [_body(0, _rect(1900, 1400, 2100, 1600), height=1.0 * MM),
              _body(3, _rect(2500, 500, 2700, 700), height=8.0 * MM)]
    board = _board()
    board.outline_shape = list(OUTLINE)
    return PcbData(
        board=board, nets=list(NETS), components=components, primitives=primitives,
        pours=[], layer_name_to_v6=dict(LAYERS), rules=list(rules), bodies=bodies,
        bare_holes=[BareHole(x=x, y=y, hole=125.0, layer=74, net=None, component=None,
                             pad_name="", plated=False) for x, y in holes],
    )


def _review(pcb=None, profile=None, **kwargs) -> dict:
    return tp.review_testpoints(PcbIndex(pcb or _pcb(), None), profile, **kwargs)


def _findings(result, check, severity=None):
    return [finding for finding in result["findings"] if finding["check"] == check
            and (severity is None or finding["severity"] == severity)]


# ---------- sites and coverage ----------


def test_sites_coverage_and_probe_side():
    result = _review()
    assert result["sites"] == {
        "probe_sites": 7, "probe_points": 7, "testpoint_marked": 5, "tp_part_pads": 2,
        "tp_parts": 2, "by_side": {"top": 5, "bottom": 2}, "by_kind": {"pad": 2, "via": 5},
        "nets_with_sites": 5,
    }
    coverage = result["coverage"]
    assert (coverage["nets"], coverage["with_access"], coverage["percent"]) == (6, 5, 83.3)
    assert (coverage["top"], coverage["bottom"], coverage["both_sides"]) == (4, 2, 1)
    assert (coverage["top_only"], coverage["bottom_only"]) == (3, 1)
    # ict probes one side: auto picks the side reaching more nets.
    assert (coverage["probe_side"], coverage["probe_side_chosen_by"]) == ("top", "auto")
    (side,) = _findings(result, "probe_side")
    assert side["severity"] == "info" and side["nets"] == ["+3V3"]
    assert side["issue"].startswith("1 net is reachable only from the bottom")
    assert result["profile"]["name"] == "ict" and result["profile"]["chosen_by"] == "default"
    assert "guidance" in result["profile"] and result["origin_mil"] == [0.0, 0.0]


def test_findings_on_ict_guidance():
    result = _review()
    assert result["severity_counts"] == {"warning": 4, "info": 6}
    assert [(f["check"], f["severity"]) for f in result["findings"]] == [
        ("rails", "warning"), ("pad_size", "warning"), ("spacing", "warning"),
        ("edge_clearance", "warning"), ("required_nets", "info"), ("probe_side", "info"),
        ("under_component", "info"), ("tall_parts", "info"), ("solder_mask", "info"),
        ("style_rule", "info"),
    ]
    (rail,) = _findings(result, "rails")
    assert (rail["net"], rail["sites"]) == ("+3V3", 0)  # its site is on the bottom
    # The probe side is the top: TP2's 30 mil pad on the bottom is never probed.
    sizes = {(f["size_mil"], f["examples"][0]["kind"]) for f in _findings(result, "pad_size")}
    assert sizes == {(30.0, "via")}
    (spacing,) = _findings(result, "spacing")
    assert spacing["closest_mil"] == 50.0
    assert {spacing["examples"][0]["first"].get("pad"),
            spacing["examples"][0]["second"].get("net")} == {"TP1-1", "GND"}
    (edge,) = _findings(result, "edge_clearance")
    assert edge["examples"][0]["edge_distance_mil"] == 100.0
    assert edge["examples"][0]["net"] == "SIG3"
    (required,) = _findings(result, "required_nets")
    # Fabrication and assembly default rules ask for the same nets: one finding.
    assert required["rules"] == ["FabricationTestPointUsage", "AssemblyTestPointUsage"]
    assert required["nets"] == ["SIG4"] and "like Altium's default rule)" in required["issue"]
    (under,) = _findings(result, "under_component")
    assert under["part"] == "U1" and "allows testpoints under components" in under["issue"]
    (tall,) = _findings(result, "tall_parts")
    assert (tall["part"], tall["closest_mil"]) == ("J1", 150.0)
    (mask,) = _findings(result, "solder_mask")
    assert mask["examples"][0]["net"] == "+3V3" and mask["examples"][0]["side"] == "bottom"
    (style,) = _findings(result, "style_rule")
    assert (style["problem"], style["limit_mil"], style["count"]) == ("size_below_rule", 40.0, 1)
    assert result["tooling_holes"]["count"] == 2 and not _findings(result, "tooling_holes")
    assert result["usage"] == [
        {"usage": "fabrication", "rules": ["FabricationTestPointUsage"], "required": 6,
         "required_without_access": 1},
        {"usage": "assembly", "rules": ["AssemblyTestPointUsage"], "required": 6,
         "required_without_access": 1},
    ]
    kinds = [rule["kind"] for rule in result["testpoint_rules"]]
    assert kinds == ["FabricationTestPointUsage", "AssemblyTestPointUsage",
                     "FabricationTestpoint"]
    assert result["testpoint_rules"][2]["values"]["min_size_mil"] == 40.0


def test_fixture_geometry_is_judged_on_the_probe_side_only():
    # A bottom-side fixture never lands on the top: the top sites near J1, under U1, 50 mil
    # from TP1 and 100 mil from the edge are no findings; TP2's small pad on the bottom is.
    bottom, _ = tp.build_profile(None, None, "bottom")
    result = _review(profile=bottom)
    for check in ("spacing", "edge_clearance", "under_component", "tall_parts"):
        assert not _findings(result, check), check
    (size,) = _findings(result, "pad_size")
    assert size["examples"][0]["pad"] == "TP2-1"
    # The marks themselves are judged on every face they are on.
    assert _findings(result, "solder_mask") and _findings(result, "style_rule")
    both, _ = tp.build_profile(None, None, "both")
    result = _review(profile=both)
    for check in ("spacing", "edge_clearance", "under_component", "tall_parts"):
        assert _findings(result, check), check
    assert len(_findings(result, "pad_size")) == 2


def test_auto_probe_side_breaks_a_tie_on_probe_points():
    # One net reached from each side: the side with more probe points, not the bottom.
    pcb = _pcb()
    pcb.primitives = [prim for prim in pcb.primitives if prim.kind != "via"
                      and prim.component not in (1, 2)]
    pcb.primitives += [_via("SIG4", 1500, 2500, top=True), _via("SIG4", 1700, 2500, top=True),
                       _via("SIG4", 1900, 2500, bottom=True)]
    assert _review(pcb)["coverage"]["probe_side"] == "top"
    pcb.primitives[-1] = _via("SIG4", 1900, 2500, top=True, bottom=True)
    pcb.primitives += [_via("SIG4", 2100, 2500, bottom=True),
                       _via("SIG4", 2300, 2500, bottom=True)]
    assert _review(pcb)["coverage"]["probe_side"] == "bottom"


def test_supply_names_with_a_signal_token_are_signals():
    # check_decoupling's name rules: 3V3_EN and GND_SENSE are signals, no rail or ground.
    pcb = _pcb()
    pcb.nets += ["3V3_EN", "GND_SENSE"]
    en_net, sense_net = len(NETS), len(NETS) + 1
    pcb.primitives += [_smd(4, 2900, 2400, net=en_net), _smd(4, 2900, 2450, net=en_net),
                       _smd(4, 2950, 2400, net=sense_net), _smd(4, 2950, 2450, net=sense_net)]
    result = _review(pcb)
    assert [f["net"] for f in _findings(result, "rails")] == ["+3V3"]
    assert not _findings(result, "ground")


def test_rail_with_too_few_or_too_many_sites():
    # One site on the probe side is short of the 2 asked for (info; none is a warning).
    (rail,) = _findings(_review(_pcb(extra=[_via("+3V3", 300, 2700, top=True)])), "rails")
    assert (rail["sites"], rail["severity"]) == (1, "info") and "at least 2" in rail["issue"]
    # +3V3 probed at 11 places on the probe side: guidance is at most 10 per rail.
    extra = [_via("+3V3", 300 + 150 * n, 2700, top=True) for n in range(11)]
    (rail,) = _findings(_review(_pcb(extra=extra)), "rails")
    assert (rail["net"], rail["sites"], rail["severity"]) == ("+3V3", 11, "info")
    assert "at most 10" in rail["issue"]


def test_a_testpoint_footprint_makes_a_tp_part():
    pcb = _pcb(extra=[_smd(5, 500, 500, 40.0, net=N["SIG4"], name="1")])
    pcb.components.append(_part(5, "X1", 500, 500, footprint="Keystone Test Point 5015"))
    result = _review(pcb)
    assert result["sites"]["tp_parts"] == 3 and result["coverage"]["with_access"] == 6


def test_tp_parts_are_no_tall_parts():
    # A test loop is often 5 to 7 mm tall: a target, not a keep-out for its neighbours.
    pcb = _pcb()
    pcb.bodies.append(_body(1, _rect(980, 980, 1020, 1020), height=7.0 * MM))
    result = _review(pcb)
    assert [f["part"] for f in _findings(result, "tall_parts")] == ["J1"]
    # Its own body over its pad does not block it either.
    assert [f["part"] for f in _findings(result, "under_component")] == ["U1"]


def test_flags_on_a_face_without_copper_and_off_board_sites_are_not_sites():
    extra = [
        dataclasses.replace(_smd(None, 500, 2500, 40.0, net=N["SIG4"]), test_fab_bottom=True),
        _via("SIG4", 5000, 1500, top=True),  # off the board
        # A TP part's pad whose copper is no larger than its hole: nothing to probe.
        _tht(1, 1500, 2600, size=30.0, hole=35.0, net=N["SIG4"]),
        # A blind via from the top to layer 2 marked bottom: no copper on the bottom.
        dataclasses.replace(_via("SIG4", 600, 2500, bottom=True), layer_to=2),
    ]
    result = _review(_pcb(extra=extra))
    assert result["sites"]["marks_without_copper"] == 2
    assert result["sites"]["off_board"] == 1
    assert result["coverage"]["with_access"] == 5


def test_no_board_outline():
    pcb = _pcb(extra=[_via("SIG4", 5000, 1500, top=True)])
    pcb.board.outline_shape = []
    result = _review(pcb)
    assert any("No board outline" in note for note in result["notes"])
    assert not _findings(result, "edge_clearance")
    # Without an outline no site is off the board: the far via counts.
    assert "off_board" not in result["sites"] and result["coverage"]["with_access"] == 6


def test_under_component_follows_the_testpoint_rule_and_the_variant():
    rules = RULES[:2] + [_rule("FabricationTestpoint", **dict(
        STYLE, TESTPOINTUNDERCOMPONENT="FALSE"))]
    (under,) = _findings(_review(_pcb(rules)), "under_component")
    assert under["severity"] == "warning" and "allows" not in under["issue"]
    # U1 Not Fitted: its body no longer covers the via.
    result = _review(is_dnp=lambda name: name == "U1")
    assert not _findings(result, "under_component")
    assert any("Not Fitted" in note for note in result["notes"])


def test_style_rule_sides_and_holes():
    # A Testpoint rule allowing the top only, holes up to 10 mil: the +3V3 via marked bottom
    # is on a side the rule forbids, and every marked via's 12 mil hole is too large.
    rules = RULES[:2] + [_rule("FabricationTestpoint", **dict(
        STYLE, ALLOWSIDEBOTTOM="FALSE", MAXHOLESIZE="10mil"))]
    found = {f["problem"]: f for f in _findings(_review(_pcb(rules)), "style_rule")}
    assert set(found) == {"side_not_allowed", "hole_above_rule", "size_below_rule"}
    side = found["side_not_allowed"]
    assert side["severity"] == "warning" and side["count"] == 1
    assert (side["examples"][0]["net"], side["examples"][0]["side"]) == ("+3V3", "bottom")
    hole = found["hole_above_rule"]
    assert (hole["severity"], hole["count"], hole["limit_mil"]) == ("info", 5, 10.0)
    assert hole["examples"][0]["hole_mil"] == 12.0


def test_usage_rules_scoped_batch_checked_and_prohibited():
    rules = [
        _rule("FabricationTestPointUsage", "InNet('SIG4')", 1, 44, "Critical", VALID="0"),
        _rule("FabricationTestPointUsage", "InNet('SIG3')", 2, 44, "NoTp", VALID="1"),
        _rule("FabricationTestPointUsage", "All", 3, 44, "Rest", VALID="2"),
    ]
    result = _review(_pcb(rules))
    found = {f["rules"][0]: f for f in _findings(result, "required_nets")}
    assert found["Critical"]["severity"] == "warning" and found["Critical"]["nets"] == ["SIG4"]
    assert found["NoTp"]["severity"] == "warning" and "prohibits" in found["NoTp"]["issue"]
    assert result["usage"] == [{"usage": "fabrication", "rules": ["Critical", "NoTp", "Rest"],
                                "dont_care": 4, "prohibited": 1,
                                "prohibited_with_testpoint": 1, "required": 1,
                                "required_without_access": 1}]
    # The default rule over All is a warning when the board's batch DRC checks its kind.
    pcb = _pcb(RULES[:1])
    pcb.drc_rule_kinds = {44}
    (required,) = _findings(_review(pcb), "required_nets")
    assert required["severity"] == "warning"
    assert _review(pcb)["testpoint_rules"][0]["checked_by_batch_drc"] is True
    pcb.drc_rule_kinds = {43}
    (required,) = _findings(_review(pcb), "required_nets")
    assert required["severity"] == "info"
    assert "not checked by the board's batch DRC" in required["issue"]


def test_usage_rules_undecided_and_assembly_prohibition():
    # A scope the net alone cannot settle (IsPad) decides no net; an assembly usage rule
    # prohibiting SIG1 is not broken by its Fabrication Testpoint mark (assembly marks are
    # not read).
    rules = [
        _rule("FabricationTestPointUsage", "IsPad", 1, 44, "Pads", VALID="0"),
        _rule("AssemblyTestPointUsage", "InNet('SIG1')", 1, 58, "NoAsm", VALID="1"),
        _rule("AssemblyTestPointUsage", "All", 2, 58, "AsmRest", VALID="2"),
    ]
    result = _review(_pcb(rules))
    assert result["usage"] == [
        {"usage": "fabrication", "rules": ["Pads"], "undecided": 6},
        {"usage": "assembly", "rules": ["NoAsm", "AsmRest"], "dont_care": 5, "prohibited": 1},
    ]
    assert not _findings(result, "required_nets")


def test_critical_nets():
    result = _review(nets=["SIG4", "+3V3"])
    found = {f["net"]: f for f in _findings(result, "required_nets", "warning")}
    assert "no probe site" in found["SIG4"]["issue"]
    assert "only from the bottom" in found["+3V3"]["issue"]
    assert result["nets_asked"] == 2
    assert _review(nets=["NOPE"])["error"] == "net_not_found"


def test_no_probe_sites():
    pcb = _pcb()
    pcb.primitives = [prim for prim in pcb.primitives if prim.kind != "via"
                      and prim.component not in (1, 2)]
    result = _review(pcb)
    (coverage,) = _findings(result, "coverage")
    assert coverage["severity"] == "warning" and coverage["nets"] == 4
    assert not _findings(result, "rails") and not _findings(result, "ground")
    assert any("not judged per net" in note for note in result["notes"])


def test_ground_wants_about_one_site_per_twenty_connections():
    extra = [_smd(4, 3000 + 3 * n, 2700, 2.0, net=N["GND"]) for n in range(40)]
    (ground,) = _findings(_review(_pcb(extra=extra)), "ground")
    assert (ground["connections"], ground["sites"], ground["wanted"]) == (43, 1, 3)
    assert ground["severity"] == "info"


def test_tooling_holes():
    (missing,) = _findings(_review(_pcb(holes=((300, 300),))), "tooling_holes")
    assert missing["severity"] == "warning" and missing["count"] == 1
    (close,) = _findings(_review(_pcb(holes=((300, 300), (600, 300)))), "tooling_holes")
    assert close["severity"] == "info" and close["spread_mil"] == 300.0


# ---------- profiles and limits ----------


def test_flying_probe_profile():
    profile, error = tp.build_profile("flying-probe")
    assert error is None and profile["name"] == "flying_probe"
    result = _review(profile=profile)
    assert result["coverage"]["probe_side"] == "both"
    checks = {f["check"] for f in result["findings"]}
    assert "probe_side" not in checks and "pad_size" not in checks
    assert "spacing" not in checks and "tooling_holes" not in checks
    assert _findings(result, "edge_clearance", "warning")  # 100 mil < 3 mm


def test_limits_and_probe_side():
    profile, error = tp.build_profile(None, {"spacing_min_mm": 1.0, "rail_min_sites": None,
                                             "pad_min_mil": 25}, "top")
    assert error is None
    assert profile["spacing_min_mil"] == pytest.approx(MM)
    assert profile["changed_by_limits"] == ["pad_min_mil", "rail_min_sites",
                                            "spacing_min_mil"]
    result = _review(profile=profile)
    assert (result["coverage"]["probe_side"], result["coverage"]["probe_side_chosen_by"]) \
        == ("top", "argument")
    assert _findings(result, "spacing")[0]["severity"] == "info"
    assert not _findings(result, "rails")
    assert {f["severity"] for f in _findings(result, "pad_size")} == {"info"}
    for bad in ({"spacing_min": 1}, {"pad_min_mm": -1}, {"rail_min_sites": 1.5},
                {"rail_max_sites": True}, {"pad_min_mm": 2.0}):
        assert tp.build_profile(None, bad)[1]["error"] == "invalid_limits", bad
    assert tp.build_profile("bed")[1]["error"] == "invalid_profile"
    assert tp.build_profile(None, None, "left")[1]["error"] == "invalid_probe_side"
    assert _review(checks=["nope"])["error"] == "invalid_checks"


def test_checks_narrow_the_findings():
    result = _review(checks=["spacing", "edge_clearance"])
    assert {f["check"] for f in result["findings"]} == {"spacing", "edge_clearance"}
    assert result["checks_run"] == ["spacing", "edge_clearance"]


# ---------- tool ----------


@pytest.fixture
def board(monkeypatch, tmp_path: Path):
    index = PcbIndex(_pcb(), None)
    pcb_file = tmp_path / "Board.PcbDoc"
    pcb_file.write_bytes(b"fake")
    monkeypatch.setattr(main_mod, "_project", {
        "name": "Demo", "root_dir": str(tmp_path), "prj_pcb_path": str(tmp_path / "D.PrjPcb"),
        "sheets": [], "pcb_doc_paths": [str(pcb_file)],
    })
    monkeypatch.setattr(main_mod._altium, "_netlist", {"components": {}, "nets": {}})
    monkeypatch.setattr(main_mod, "_variant_state",
                        VariantState([VariantDefinition(name="Default")]))
    monkeypatch.setattr(main_mod._pcb_session, "_load", lambda path, netlist: index)
    main_mod._pcb_session._index = None
    return index


def test_check_testpoints_tool(board):
    result = json.loads(main_mod.check_testpoints(units="mm"))
    assert result["pcb_doc"] == "Board.PcbDoc" and result["units"] == "mm"
    assert result["profile"]["values"]["pad_min_mm"] == pytest.approx(0.9, abs=1e-4)
    (edge,) = [f for f in result["findings"] if f["check"] == "edge_clearance"]
    assert edge["examples"][0]["at_mm"] == [2.54, 38.1]
    assert " mm" in edge["issue"]
    assert result["total"] == 10 and result["has_more"] is False
    paged = json.loads(main_mod.check_testpoints(offset=9, units="mil"))
    assert len(paged["findings"]) == 1 and paged["offset"] == 9
    limited = json.loads(main_mod.check_testpoints(
        profile="flying_probe", limits={"edge_clearance_mm": 2.0}, nets="SIG4"))
    assert limited["profile"]["name"] == "flying_probe"
    assert [f["net"] for f in limited["findings"] if f.get("net")] == ["SIG4"]
    assert json.loads(main_mod.check_testpoints(limits={"x": 1}))["error"] == "invalid_limits"
    assert json.loads(main_mod.check_testpoints(nets="NOPE"))["error"] == "net_not_found"
    assert json.loads(main_mod.check_testpoints(units="inch"))["error"] == "invalid_units"


def test_check_testpoints_needs_a_project(monkeypatch):
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.check_testpoints())["error"] == "no_project"
    assert "next_offset" in main_mod.check_testpoints.__doc__


def test_instructions_name_the_tool_after_the_cut():
    assert main_mod.SERVER_INSTRUCTIONS.index("22. check_testpoints") > 4096


# ---------- Altium's examples (skipped when absent) ----------


@pytest.mark.skipif(not SPIRIT.exists(), reason="Altium example not installed")
def test_spirit_level_testpoint_vias():
    # The only testpoint marks in the examples: two 0.9 mm GND vias, Fabrication Testpoint
    # bottom, tented; three TP parts (TP1 through-hole, TP2 and TP3).
    pcb = parse_pcb_doc(str(SPIRIT))
    marked = [prim for prim in pcb.primitives if prim.test_fab_top or prim.test_fab_bottom]
    assert [(prim.kind, pcb.nets[prim.net], prim.test_fab_top, prim.test_fab_bottom)
            for prim in marked] == [("via", "GND", False, True)] * 2
    assert {round(prim.width * 0.0254, 3) for prim in marked} == {0.9}
    result = tp.review_testpoints(PcbIndex(pcb, None))
    assert result["sites"]["probe_sites"] == 5 and result["sites"]["testpoint_marked"] == 2
    assert (result["coverage"]["nets"], result["coverage"]["with_access"]) == (126, 4)
    (mask,) = _findings(result, "solder_mask")
    assert mask["count"] == 2
    assert result["tooling_holes"]["count"] == 4


@pytest.mark.skipif(not SENTINEL.exists(), reason="Altium example not installed")
def test_bluetooth_sentinel_tp_parts():
    result = tp.review_testpoints(PcbIndex(parse_pcb_doc(str(SENTINEL)), None))
    assert result["sites"]["tp_parts"] == 11 and result["sites"]["testpoint_marked"] == 0
    assert (result["coverage"]["nets"], result["coverage"]["with_access"]) == (55, 10)
    assert result["coverage"]["probe_side"] == "top"
    assert result["tooling_holes"]["count"] == 0
    assert _findings(result, "tooling_holes", "warning")
