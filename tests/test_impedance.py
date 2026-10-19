"""Layer Stack Manager impedance profiles: parsing, solutions, rule links, issues."""

import json
from copy import deepcopy

import pytest

import server.main as main_mod
from server.parsers.pcb_doc import PcbRule, decode_board, decode_impedance_profiles
from server.services.impedance import impedance_profiles, profile_refs, snapshot_line
from server.services.pcb_index import PcbIndex
from server.services.pcb_rules import rule_values
from tests.test_pcb_index import NETLIST, pcb_data  # noqa: F401
from tests.test_pcb_rules_tools import rules_pcb  # noqa: F401

TOP, L2, L3, BOTTOM = 0x01000001, 0x01000002, 0x01000003, 0x0100FFFF
NO_LAYER = 0x01030000
UNBOUNDED = "214748.3647mil"

PROFILE_PROPS = {
    "V9_IMPEDANCEPROFILE0_ID": "{D90}",
    "V9_IMPEDANCEPROFILE0_NAME": "USB",
    "V9_IMPEDANCEPROFILE0_DISPLAY_NAME": "D90 (USB)",
    "V9_IMPEDANCEPROFILE0_IMPEDANCE": "90.000000",
    "V9_IMPEDANCEPROFILE0_IMPEDANCE_TOLERANCE": "10.000000",
    "V9_IMPEDANCEPROFILE0_ISDIFFPAIR": "TRUE",
    "V9_IMPEDANCEPROFILE1_ID": "{S50}",
    "V9_IMPEDANCEPROFILE1_NAME": "",
    "V9_IMPEDANCEPROFILE1_DISPLAY_NAME": "S50",
    "V9_IMPEDANCEPROFILE1_IMPEDANCE": "50.000000",
    "V9_IMPEDANCEPROFILE1_ISDIFFPAIR": "FALSE",
}


def _trace(index, profile, layer, width, gap, calc, enabled="TRUE", top=NO_LAYER, bottom=L2):
    prefix = f"V9_TRACEIMPEDANCE{index}_"
    return {
        f"{prefix}PROFILE_ID": profile,
        f"{prefix}LAYER_V7ID": str(layer),
        f"{prefix}REF_TOP_V7ID": str(top),
        f"{prefix}REF_BOT_V7ID": str(bottom),
        f"{prefix}SUBSTACK_ID": "{RIGID}",
        f"{prefix}TRACE_WIDTH": width,
        f"{prefix}TRACE_MINWIDTH": width,
        f"{prefix}TRACE_MAXWIDTH": UNBOUNDED,
        f"{prefix}DIFF_PAIR_GAP": gap,
        f"{prefix}DIFF_PAIR_MINGAP": gap,
        f"{prefix}DIFF_PAIR_MAXGAP": UNBOUNDED,
        f"{prefix}CALC_IMPEDANCE": calc,
        f"{prefix}IMPEDANCE_ERR": "0.5",
        f"{prefix}PROPAGATION_SPEED": "0.150000",
        f"{prefix}ENABLED": enabled,
    }


TRACES = {
    **_trace(0, "{D90}", TOP, "4.5mil", "5mil", "90.4"),
    **_trace(1, "{D90}", L3, "4mil", "6mil", "101.0", top=L2, bottom=BOTTOM),
    **_trace(2, "{D90}", BOTTOM, "4.5mil", "5mil", "90.0", enabled="FALSE"),
    **_trace(3, "{S50}", TOP, "8mil", "5mil", "50.2"),
    **_trace(4, "{GONE}", TOP, "8mil", "5mil", "50.2"),
}


def test_decode_profiles_and_layers():
    profiles = decode_impedance_profiles({**PROFILE_PROPS, **TRACES})
    d90, s50 = profiles
    assert (d90.name, d90.target_ohm, d90.tolerance_pct, d90.differential) == (
        "D90 (USB)", 90.0, 10.0, True,
    )
    assert s50.name == "S50"  # empty NAME: the label Altium shows
    assert s50.tolerance_pct is None and not s50.differential
    assert len(d90.layers) == 3  # disabled entries kept, flagged
    top = d90.layers[0]
    assert top.layer_v7 == TOP and top.width_mil == 4.5 and top.gap_mil == 5.0
    assert top.max_width_mil is None and top.max_gap_mil is None  # unbounded
    assert d90.layers[2].enabled is False
    assert decode_impedance_profiles({}) == []


@pytest.fixture
def profile_index(pcb_data):
    data = deepcopy(pcb_data)
    board = decode_board([{
        "V9_STACK_LAYER0_NAME": "Top Layer", "V9_STACK_LAYER0_LAYERID": str(TOP),
        "V9_STACK_LAYER0_COPTHICK": "1.4mil",
        "V9_STACK_LAYER1_NAME": "Dielectric 1", "V9_STACK_LAYER1_DIELHEIGHT": "5mil",
        "V9_STACK_LAYER2_NAME": "L2-GND", "V9_STACK_LAYER2_LAYERID": str(L2),
        "V9_STACK_LAYER2_COPTHICK": "1.2mil",
        "V9_STACK_LAYER3_NAME": "L3-SIG", "V9_STACK_LAYER3_LAYERID": str(L3),
        "V9_STACK_LAYER3_COPTHICK": "1.2mil",
        "V9_STACK_LAYER4_NAME": "Bottom Layer", "V9_STACK_LAYER4_LAYERID": str(BOTTOM),
        "V9_STACK_LAYER4_COPTHICK": "1.4mil",
        "LAYER1NAME": "Top Layer", "LAYER2NAME": "L2-GND", "LAYER3NAME": "L3-SIG",
        "LAYER32NAME": "Bottom Layer",
        **PROFILE_PROPS, **TRACES,
    }])
    data.board.impedance_profiles = board.impedance_profiles
    for layer in data.board.layers:
        layer.v7_id = {1: TOP, 2: L2, 3: L3, 32: BOTTOM}.get(layer.v6_id)
    data.rules = [
        PcbRule(
            name="DP_USB", kind="DiffPairsRouting", kind_id=51, enabled=True,
            priority=1, scope1="All", scope2="All", net_scope="AnyNet", comment="",
            unique_id=None, from_schematic=False,
            properties={
                "IMPEDANCEPROFILEDRIVEN": "TRUE", "IMPEDANCEPROFILEID": "{d90}",
                "IMPEDANCEPROFILEVALUE": "90.000000",
                "MINLIMIT": "5mil", "MOSTFREQGAP": "5mil", "MAXLIMIT": "5mil",
                "TOPLAYER_MINWIDTH": "4.5mil", "TOPLAYER_PREFWIDTH": "4.5mil",
                "TOPLAYER_MAXWIDTH": "4.5mil",
                "MIDLAYER2_MINWIDTH": "4.4mil", "MIDLAYER2_PREFWIDTH": "4.4mil",
                "MIDLAYER2_MAXWIDTH": "4.4mil", "MIDLAYER2_PREFGAP": "6mil",
            },
        )
    ]
    return PcbIndex(data, NETLIST)


def test_impedance_profiles_resolve_layers_rules_and_issues(profile_index):
    d90, s50 = impedance_profiles(profile_index)
    assert d90["type"] == "differential"
    assert d90["used_by_rules"] == [
        {"rule": "DP_USB", "kind": "DiffPairsRouting", "enabled": True}
    ]
    top, l3 = d90["layers"]  # the disabled bottom entry is left out
    assert top["layer"] == "Top Layer"
    assert top["reference_layers"] == ["L2-GND"]  # 'no layer' above is dropped
    assert (top["width_mil"], top["gap_mil"], top["calculated_ohm"]) == (4.5, 5.0, 90.4)
    assert top["within_tolerance"] is True
    # Stored 0.150 is ns/inch: 150 ps per inch.
    assert top["delay_ps_per_inch"] == pytest.approx(150.0)
    assert "max_width_mil" not in top
    assert l3["reference_layers"] == ["L2-GND", "Bottom Layer"]
    assert l3["within_tolerance"] is False
    issues = d90["issues"]
    assert any("outside 90 ohm +/-10%" in issue for issue in issues)
    # The rule's L3 width drifted from the profile's 4 mil solution.
    assert any("DP_USB preferred width on L3-SIG is 4.4 mil" in issue for issue in issues)
    assert s50["issues"] == ["no design rule uses this profile"]
    assert "gap_mil" not in s50["layers"][0]


def test_rule_values_name_the_profile(profile_index):
    rule = profile_index.pcb.rules[0]
    values = rule_values(
        rule, profile_index.signal_layers(), [], profile_refs(profile_index.pcb.board)
    )
    assert values["impedance_profile"] == {
        "name": "D90 (USB)", "type": "differential", "target_ohm": 90.0,
        "tolerance_pct": 10.0,
    }
    assert "impedanceprofileid" not in values.get("other", {})
    missing = rule_values(rule, profile_index.signal_layers(), [], {})
    assert missing["impedance_profile"] == {"id": "{d90}", "missing": True}


def test_snapshot_line(profile_index):
    line = snapshot_line(profile_index)
    assert line.startswith("- Impedance profiles (2): ")
    assert "D90 (USB) (differential 90 ohm +/-10%, used by 1 rule, has issues)" in line
    assert "S50 (single-ended 50 ohm, unused)" in line


def test_get_board_info_lists_profiles(rules_pcb):
    board = json.loads(main_mod.get_board_info())
    assert "impedance_profiles" not in board
    rules_pcb.pcb.board.impedance_profiles = decode_impedance_profiles(
        {**PROFILE_PROPS, **TRACES}
    )
    board = json.loads(main_mod.get_board_info(units="mm"))
    names = [profile["name"] for profile in board["impedance_profiles"]]
    assert names == ["D90 (USB)", "S50"]
    assert "width_mm" in board["impedance_profiles"][0]["layers"][0]
    assert "ns/inch" in board["impedance_note"]


def test_profile_id_does_not_hide_other_rule_values():
    for kind in ("Width", "DiffPairsRouting"):
        rule = PcbRule(
            name="R", kind=kind, kind_id=2, enabled=True, priority=1, scope1="All",
            scope2="All", net_scope="AnyNet", comment="", unique_id=None,
            from_schematic=False,
            properties={"IMPEDANCEPROFILEDRIVEN": "TRUE", "IMPEDANCEPROFILEID": "{X}",
                        "CHECKCONNECTEDCOPPER": "TRUE"},
        )
        values = rule_values(rule, {1: "Top"}, [], {})
        assert values["other"] == {"checkconnectedcopper": True}
        assert values["impedance_profile"] == {"id": "{X}", "missing": True}


def test_delay_matches_stripline_physics():
    # A homogeneous Dk 4.1 stripline: eps_eff = 4.1, delay = sqrt(4.1)/11.803 ns/in.
    props = {
        "V9_IMPEDANCEPROFILE0_ID": "{P}", "V9_IMPEDANCEPROFILE0_DISPLAY_NAME": "S50",
        "V9_IMPEDANCEPROFILE0_IMPEDANCE": "50", "V9_IMPEDANCEPROFILE0_ISDIFFPAIR": "FALSE",
        **_trace(0, "{P}", L3, "5mil", "5mil", "50", top=L2, bottom=BOTTOM),
        "V9_TRACEIMPEDANCE0_PROPAGATION_SPEED": "0.171550",
    }
    (profile,) = decode_impedance_profiles(props)
    delay = profile.layers[0].propagation_speed * 1000
    assert delay == pytest.approx((4.1 ** 0.5) / 11.803 * 1000, rel=0.01)
