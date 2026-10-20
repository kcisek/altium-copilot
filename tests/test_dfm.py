"""check_dfm's review (services/dfm): each check on synthetic boards, marginal results and
the exclusions, results, board minimums and the drill table, paging and the cache, and
Altium's examples (skipped when absent)."""

import gc
import json
import time
import weakref
from pathlib import Path

import pytest

import server.main as main_mod
from server.parsers.pcb_doc import (
    PAD_INNER_LAYERS,
    PAD_SHAPE_RECT,
    BareHole,
    BoardInfo,
    PadCopper,
    PcbComponent,
    PcbData,
    PcbLayer,
    PcbRule,
    PolygonPour,
    Primitive,
    ShapeVertex,
    parse_pcb_doc,
)
from server.parsers.pcb_dwf import parse_pcb_dwf
from server.services import dfm
from server.services.dfm import (
    SHRINK_STEPS,
    VALID_CHECKS,
    _pair,
    page_dfm,
    review_dfm,
)
from server.services.fab_profiles import MM, load_profile
from server.services.pcb_index import PcbIndex
from tests import test_dfm_assembly as facts
from tests.test_dfm_geometry import OUTLINE, _area, _th_pad, _via
from tests.test_pcb_health import _region, _square
from tests.test_pcb_index import _pad, _track

GND, A, B, C, D = range(5)
NETS = ["GND", "A", "B", "C", "D"]
LAYERS = {"top layer": 1, "l2": 2, "l3": 3, "bottom layer": 32, "top": 1, "bottom": 32}
EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")
ORIGIN = (100.0, 50.0)
# Copper-to-copper depth of the test stack: 4 x 1.4 mil copper + 4 + 40 + 4 mil.
DEPTH = 53.6


def _board(*, heights=True, copper=1.4, inner_layers=2, legacy=False, outline=OUTLINE):
    def layer(v6, name, order, kind="copper", thick=None, height=None):
        return PcbLayer(v6_id=v6, name=name, kind=kind, stack_order=order,
                        copper_thick_mil=thick, diel_height_mil=height)

    thick = copper if heights else None
    layers = [layer(None, "Top Solder", 0, "dielectric", height=0.4),
              layer(1, "Top Layer", 1, thick=thick),
              layer(None, "D1", 2, "dielectric", height=4.0)]
    if inner_layers >= 1:
        layers += [layer(2, "L2", 3, thick=thick), layer(None, "D2", 4, "dielectric", height=40.0)]
    if inner_layers >= 2:
        layers += [layer(3, "L3", 5, thick=thick), layer(None, "D3", 6, "dielectric", height=4.0)]
    layers += [layer(32, "Bottom Layer", 7, thick=thick),
               layer(None, "Bottom Solder", 8, "dielectric", height=0.4)]
    return BoardInfo(
        origin_x=ORIGIN[0], origin_y=ORIGIN[1], sheet_x=0.0, sheet_y=0.0, sheet_width=5000.0,
        sheet_height=5000.0, display_unit="mil", stackup_source="legacy" if legacy else "v9",
        layers=layers, outline_shape=list(outline or []),
    )


def _part(index, kind=0, footprint="R0402"):
    return PcbComponent(
        index=index, refdes=f"U{index}", source_uid_tail=None, channel_path=None, x=0.0,
        y=0.0, rotation=0.0, side="top", designator=f"U{index}", kind=kind,
        footprint=footprint,
    )


def _rule(kind, priority=1, scope1="All", scope2="All", name=None, **props):
    return PcbRule(
        name=name or kind, kind=kind, kind_id=0, enabled=True, priority=priority,
        scope1=scope1, scope2=scope2, net_scope="DifferentNets", comment="", unique_id=None,
        from_schematic=False, properties=props,
    )


def _index(primitives=(), *, areas=(), pours=(), components=None, regions=b"", bare_holes=(),
           rules=(), embedded=0, **board):
    pcb = PcbData(
        board=_board(**board), nets=list(NETS),
        components=list(components if components is not None else [_part(i) for i in range(4)]),
        primitives=list(primitives), pours=list(pours), layer_name_to_v6=dict(LAYERS),
        raw={"ShapeBasedRegions6/Data": regions} if regions else {}, areas=list(areas),
        bare_holes=list(bare_holes), rules=list(rules), embedded_boards=embedded,
    )
    return PcbIndex(pcb, None)


def _profile(limits=None, name="none"):
    profile, error = load_profile(name, None, limits)
    assert error is None, error
    return profile


def _default():
    return _profile(name=None)


def _found(result, check, severity=None):
    return [finding for finding in result["findings"] if finding["check"] == check
            and (severity is None or finding["severity"] == severity)]


def _rendered(result, to_mm=False) -> str:
    return json.dumps(main_mod._convert_units(result, to_mm), indent=2)


# ---------- the vocabulary ----------


def test_invalid_checks():
    result = review_dfm(_index(), _default(), ["spacing", "Bogus"])
    assert result["error"] == "invalid_checks" and "bogus" in result["message"]
    assert result["valid_checks"] == list(VALID_CHECKS)
    assert len(VALID_CHECKS) == 19 and "copper_weight" in VALID_CHECKS
    assert VALID_CHECKS[12:] == dfm._ASSEMBLY == (
        "part_counts", "assembly_sides", "part_spacing", "part_to_edge", "fiducials",
        "tombstoning", "fine_pitch")
    # Names are case- and space-insensitive and keep the checks' own order.
    full = review_dfm(_index(), _default(), [" Board_Size ", "trace_width"])
    assert full["checks_run"] == ["trace_width", "board_size"]
    assert list(full["results"]) == ["trace_width", "board_size"]


def test_status_follows_the_severity_of_the_misses():
    """fail: an error or warning miss; marginal: every such miss marginal; advisory: only
    info misses (Altimade's service limits while it is the default, drill sizes)."""
    three = _index(inner_layers=1, copper=2.8)  # 3 copper layers of 2 oz
    default = review_dfm(three, _default(), ["layer_count", "copper_weight"])
    for check in ("layer_count", "copper_weight"):
        entry = default["results"][check]
        assert (entry["status"], entry["severity"], entry["objects"]) == (
            "advisory", "info", 1 if check == "layer_count" else 3)
    chosen = review_dfm(three, _profile(name="altimade"), ["layer_count", "copper_weight"])
    assert {check: (entry["status"], entry["severity"])
            for check, entry in chosen["results"].items()} == {
        "layer_count": ("fail", "error"), "copper_weight": ("fail", "warning")}
    # Info findings that are no misses (an absent rule) leave a check at pass.
    rules = review_dfm(_index(), _default(), ["rules_vs_profile"])["results"]["rules_vs_profile"]
    assert (rules["status"], rules["severity"], rules["objects"]) == ("pass", "info", 0)


def test_values_and_limits_print_with_enough_decimals():
    assert _pair(3.937, 4.0, True) == ("0.1 mm", "0.1016 mm")
    assert _pair(4.9999, 5.0, False) == ("4.9999 mil", "5 mil")
    assert _pair(4.99999, 5.0, False) == ("4.99999 mil", "5 mil")
    # Equal to the last decimal tried: printed as equal rather than with noise.
    assert _pair(5.0 - 1e-9, 5.0, False) == ("5 mil", "5 mil")


# ---------- trace width ----------


def test_trace_width_classes_marginal_and_board_minimum():
    chord = dict(kind="arc", layer=2, net=A, component=None, width=2.0, source_id=5)
    index = _index(
        [
            _track(A, 1, 200, 200, 300, 200, width=2.0),  # a clear miss of 0.075 mm
            _track(B, 1, 200, 300, 300, 300, width=2.9),  # within 2%: marginal
            Primitive(x1=200, y1=400, x2=250, y2=400, **chord),  # one arc, three chords
            Primitive(x1=250, y1=400, x2=300, y2=400, **chord),
            Primitive(x1=300, y1=400, x2=350, y2=400, **chord),
            Primitive(kind="track", layer=1, net=GND, component=None, x1=500, y1=500, x2=600,
                      y2=500, width=2.0, poured_by=0),
            Primitive(kind="track", layer=1, net=GND, component=None, x1=500, y1=520, x2=600,
                      y2=520, width=2.0, poured_by=0),
            _track(A, 32, 200, 700, 300, 700, width=6.0),
        ],
        pours=[PolygonPour(0, GND, 1, "Hatched", _square(480, 480, 60))],
    )
    result = review_dfm(index, _default(), ["trace_width"])
    found = sorted((f["layer"], f["width_mil"], f["count"], f["severity"], bool(f.get("marginal")))
                   for f in _found(result, "trace_width"))
    assert found == [
        ("L2", 2.0, 1, "error", False),
        ("Top Layer", 2.0, 1, "error", False),
        ("Top Layer", 2.0, 1, "error", False),
        ("Top Layer", 2.9, 1, "warning", True),
    ]
    pour = next(f for f in _found(result, "trace_width") if "hatched pour" in f["issue"])
    assert pour["issue"].startswith("Polygon on Top Layer (GND) is a hatched pour")
    marginal = next(f for f in _found(result, "trace_width") if f.get("marginal"))
    assert "'2.955 mil (0.075mm)' as published" in marginal["issue"]
    # Examples are origin-relative.
    assert marginal["examples"][0] == {"at_mil": [150.0, 250.0], "net": "B"}
    entry = result["results"]["trace_width"]
    assert (entry["status"], entry["objects"], entry["groups"]) == ("fail", 4, 4)
    assert entry["worst_mil"] == 2.0 and entry["limit_mil"] == pytest.approx(2.9528, abs=1e-4)
    assert result["board_minimums"]["min_trace"] == {
        "value_mil": 2.0, "layer": "Top Layer", "count": 3,
    }


def test_marginal_band_is_capped_for_lengths_and_rounding_named_only_when_it_applies():
    # A 2% band of a 5 mm (196.85 mil) edge limit would be 3.9 mil: capped at 1 mil.
    index = _index([_via(A, 300, 203.9, width=20.0), _via(B, 600, 206.3, width=20.0)])
    result = review_dfm(index, _profile({"min_copper_to_edge_mm": 5}), ["copper_to_edge"])
    found = sorted((f["distance_mil"], f["severity"], bool(f.get("marginal")))
                   for f in _found(result, "copper_to_edge"))
    assert found == [(193.9, "error", False), (196.3, "warning", True)]
    marginal = next(f for f in _found(result, "copper_to_edge") if f.get("marginal"))
    # A limit from the argument has no published text: no rounding is claimed.
    assert marginal["issue"].endswith(
        "Marginal: within 1 mil of the limit; confirm with the fabricator.")
    # Small limits keep the percent band, and quote the published text.
    trace = review_dfm(_index([_track(A, 1, 0, 0, 100, 0, width=2.9)]), _default(),
                       ["trace_width"])
    (finding,) = _found(trace, "trace_width")
    assert finding["marginal"] and "Marginal: within 2% of the limit ('2.955 mil " \
        "(0.075mm)' as published); confirm" in finding["issue"]
    assert "rounding" not in finding["issue"]  # 0.075 mm rounds to 3 mil, which passes
    # 0.1 mm is 4 mil (0.1016 mm) rounded to mm: the text says so.
    ring = review_dfm(_index([_via(A, 0, 0, width=15.748, hole=7.874)]), _default(),
                      ["annular_ring"], to_mm=True)
    (finding,) = _found(ring, "annular_ring")
    assert ("('4 mil (0.1016mm)' as published; rounded to 0.1 mm the limit is missed, so "
            "mm/mil rounding may explain it)") in finding["issue"]
    # Board size: 7 mm over Altimade's 377.952 mm is a miss, 0.018 mm over is marginal.
    for width_mm, verdict in ((385.0, (False, "error")), (377.97, (True, "warning"))):
        outline = [ShapeVertex(0, 0), ShapeVertex(width_mm * MM, 0),
                   ShapeVertex(width_mm * MM, 4000), ShapeVertex(0, 4000)]
        sized = review_dfm(_index(outline=outline), _profile(name="altimade"), ["board_size"])
        (finding,) = _found(sized, "board_size")
        assert (bool(finding.get("marginal")), finding["severity"]) == verdict
        assert ("Marginal: within 1 mil of the limit" in finding["issue"]) == verdict[0]
    heavy = review_dfm(_index(copper=7.14), _profile({"copper_oz": 5}), ["copper_weight"])
    (finding,) = _found(heavy, "copper_weight")  # 5.1 oz against 5 oz
    assert finding["marginal"] and "Marginal: within 2% of the limit; confirm" in finding[
        "issue"]


# ---------- spacing ----------


def _spacing_board():
    return _index(
        [
            _track(A, 1, 0, 0, 100, 0), _track(B, 1, 0, 9, 100, 9),  # 3 mil: below the rule
            _track(A, 2, 0, 100, 100, 100), _track(B, 2, 0, 110.95, 100, 110.95),  # 4.95
            _track(A, 1, 0, 200, 50, 200), _track(C, 1, 25, 200, 75, 200),  # touching
            _pad(A, 1, 0, 400, name="1"), _pad(B, 1, 14, 400, name="2"),  # one footprint
            _pad(A, 2, 100, 400), _track(None, 1, 100, 400, 120, 400, width=4.0, component=2),
            _pad(A, None, 530, 30),  # in a pour modified since it was poured
            _pad(A, 3, 700, 0, name="1"), _pad(B, 3, 740, 0, name="2"),  # net tie U3
            _track(None, 1, 700, 0, 740, 0, component=3),
            _track(None, 1, 800, 500, 900, 500), _track(None, 1, 800, 508, 900, 508),  # no nets
            _track(C, 32, 0, 600, 50, 600), _track(D, 32, 25, 600, 75, 600),  # an allowed short
        ],
        components=[_part(0), _part(1), _part(2), _part(3, kind=4)],
        pours=[PolygonPour(0, GND, 1, "Solid", _square(500, 0, 60), copper_invalid=True)],
        areas=[_area(1, _square(500, 0, 60), GND, pour_id=0)],
        rules=[
            _rule("Clearance", GENERICCLEARANCE="4mil"),
            _rule("ShortCircuit", 1, "InNet('C')", "InNet('D')", name="Allow CD", ALLOWED="TRUE"),
            _rule("ShortCircuit", 2, ALLOWED="FALSE"),
        ],
    )


def test_spacing_classes_and_exclusions():
    result = review_dfm(_spacing_board(), _profile({"min_spacing_mil": 5}), ["spacing"])
    assert sorted((f["severity"], f.get("kinds", ""), bool(f.get("marginal")))
                  for f in _found(result, "spacing")) == [
        ("error", "track-track", False),  # 3 mil
        ("error", "track-track", False),  # touching
        ("info", "", False),  # unnetted footprint copper on its pad
        ("warning", "", False),  # the modified pour
        ("warning", "pad-pad", False),  # pads of one footprint
        ("warning", "track-track", True),  # 4.95 mil
    ]
    below = next(f for f in _found(result, "spacing") if f.get("drc_violation"))
    assert (below["gap_mil"], below["count"], below["severity"]) == (3.0, 1, "error")
    assert "Below every Clearance rule of the board" in below["issue"]
    assert "board_rule" not in below
    marginal = next(f for f in _found(result, "spacing") if f.get("marginal"))
    assert marginal["severity"] == "warning" and marginal["gap_mil"] == pytest.approx(4.95)
    assert marginal["board_rule"] == {"name": "Clearance", "value_mil": 4.0,
                                      "checked_by_batch_drc": None}
    assert "go down to 4 mil: Altium's DRC may pass them (board_rule)" in marginal["issue"]
    assert marginal["layers"] == ["L2"] and "on L2" in marginal["issue"]
    touching = next(f for f in _found(result, "spacing") if "touches (gap 0)" in f["issue"])
    assert (touching["severity"], touching["kinds"], touching["gap_mil"]) == (
        "error", "track-track", 0.0)
    assert touching["issue"] == ("1 place where track-track copper of two nets touches "
                                 "(gap 0) on Top Layer: a short unless intended.")
    pads = next(f for f in _found(result, "spacing") if f.get("footprint"))
    assert (pads["footprint"], pads["severity"], pads["kinds"]) == ("R0402", "warning", "pad-pad")
    assert "only the land pattern" in pads["issue"]
    copper = next(f for f in _found(result, "spacing", "info"))
    assert copper["issue"].startswith("Unnetted copper of U2 touches its own pad on A")
    stale = next(f for f in _found(result, "spacing") if "modified since" in f["issue"])
    assert stale["severity"] == "warning" and stale["issue"].startswith(
        "Polygon on Top Layer (GND) was modified")
    assert "(1 place closer than the limit to other copper)" in stale["issue"]
    # The net tie, no-net copper and the allowed short are left out, and said so.
    assert any(note.startswith("Net ties (U3)") for note in result["notes"])
    assert any("ShortCircuit rule allows" in note for note in result["notes"])
    assert "1 pair(s) of no-net copper" in result["not_checked"]["no_net_gaps"]
    entry = result["results"]["spacing"]
    assert (entry["status"], entry["objects"], entry["groups"]) == ("fail", 4, 6)
    assert entry["worst_mil"] == 3.0 and entry["board_rule"]["name"] == "Clearance"
    # Touching copper does not set the minimum gap; it is counted (the allowed short,
    # the stale pour and the footprint copper included).
    assert result["board_minimums"]["min_gap"] == {
        "value_mil": 3.0, "layer": "Top Layer", "reach_mil": pytest.approx(5.0),
        "touching_pairs": 4,
    }


def test_spacing_marginal_is_one_class_and_examples_are_worst_first():
    tracks = []
    for number, gap in enumerate((4.92, 4.97, 4.95)):
        y = number * 100.0
        tracks += [_track(A, 1, 0, y, 100, y), _track(B, 1, 0, y + 6 + gap, 100, y + 6 + gap)]
    result = review_dfm(_index(tracks), _profile({"min_spacing_mil": 5}), ["spacing"])
    (finding,) = _found(result, "spacing")
    assert finding["marginal"] and finding["count"] == 3
    assert finding["gap_mil"] == pytest.approx(4.92)
    assert finding["gap_range_mil"] == pytest.approx([4.92, 4.97])
    assert "3 places where tracks, vias or pads of two nets are 4.92 mil to 4.97 mil apart " \
        "(track-track 3) on Top Layer" in finding["issue"]
    # Halfway across each gap, the smallest gap first; origin-relative.
    assert [example["at_mil"][1] for example in finding["examples"]] == pytest.approx(
        [5.46 - ORIGIN[1], 205.475 - ORIGIN[1], 105.485 - ORIGIN[1]])
    assert result["results"]["spacing"]["status"] == "marginal"


def test_spacing_classes_are_families_with_their_layers():
    """Routed copper (tracks, vias, pads) and area copper are one class each, whatever
    the gap or object kinds: one fix covers a class. The layers named are the real
    ones (outer, inner or both)."""
    index = _index([
        _track(A, 1, 0, 0, 100, 0), _track(B, 1, 0, 10, 100, 10),  # 4 mil on the top
        _track(A, 32, 0, 100, 100, 100), _track(B, 32, 0, 109, 100, 109),  # 3 mil, bottom
        _via(C, 300, 0, width=10.0), _track(D, 1, 250, 12, 350, 12),  # track-via, 4 mil
        _track(A, 2, 0, 300, 100, 300),  # 4 mil from a region of B on L2
        _track(A, 3, 0, 500, 100, 500), _track(B, 3, 0, 509.5, 100, 509.5),  # 3.5, L3
    ], areas=[_area(2, _square(0, 307, 50), B)])
    result = review_dfm(index, _profile({"min_spacing_mil": 5}), ["spacing"])
    area, routed = sorted(_found(result, "spacing"), key=lambda f: f["kinds"])
    assert (routed["count"], routed["gap_mil"], routed["gap_range_mil"], routed["kinds"]) == (
        4, 3.0, [3.0, 4.0], "track-track, track-via")
    assert routed["layers"] == ["Top Layer", "L3", "Bottom Layer"]
    assert ("4 places where tracks, vias or pads of two nets are 3 mil to 4 mil apart "
            "(track-track 3, track-via 1) on 3 layers (outer and inner), below") in routed[
        "issue"]
    assert (area["count"], area["kinds"]) == (1, "region-track")
    assert "where pour, region or fill copper is 4 mil from copper of another net " \
        "(region-track 1) on L2" in area["issue"]
    outer = review_dfm(_index(index.pcb.primitives[:4]), _profile({"min_spacing_mil": 5}),
                       ["spacing"])
    (finding,) = _found(outer, "spacing")
    assert "on 2 outer layers" in finding["issue"]


def test_spacing_counts_places_and_lists_one_example_per_place():
    # Two tracks of A meet under a via of B: two object pairs at one place. The other
    # pair has the via first: examples name the objects in the kinds' order.
    index = _index([
        _track(A, 1, 0, 0, 50, 0), _via(B, 50, 12, width=10.0), _track(A, 1, 50, 0, 100, 0),
        _via(C, 300, 12, width=10.0), _track(D, 1, 250, 0, 350, 0),
    ])
    result = review_dfm(index, _profile({"min_spacing_mil": 5}), ["spacing"])
    (finding,) = _found(result, "spacing")
    assert (finding["count"], finding["object_pairs"], finding["kinds"]) == (2, 3, "track-via")
    assert finding["issue"].startswith(
        "2 places where tracks, vias or pads of two nets are 4 mil apart (track-via 2) on "
        "Top Layer, below the profile's minimum spacing 5 mil (3 object pairs).")
    assert sorted(example["objects"] for example in finding["examples"]) == [
        "Track (A) / Via (B)", "Track (D) / Via (C)"]
    assert result["results"]["spacing"]["objects"] == 2


def test_spacing_rule_context_and_the_limit_itself():
    # The Clearance rule's hole and text entries are not copper to copper: the floor is
    # its 5 mil, so a 4 mil gap is below every rule (an Altium DRC violation too).
    matrix = ("ClearanceObj_Hole-ClearanceObj_Track:20000;"
              "ClearanceObj_Text-ClearanceObj_Track:10000")
    index = _index([_track(A, 1, 0, 0, 100, 0), _track(B, 1, 0, 10, 100, 10)],
                   rules=[_rule("Clearance", GENERICCLEARANCE="5mil", OBJECTCLEARANCES=matrix)])
    result = review_dfm(index, _profile({"min_spacing_mil": 5}), ["spacing"])
    (finding,) = _found(result, "spacing")
    assert finding["drc_violation"] and "board_rule" not in finding
    assert "board_rule" not in result["results"]["spacing"]
    # Below dfm_geometry's 5 mil default reach: a gap at the limit passes, one just below
    # is marginal.
    limit = 0.09 * MM
    index = _index([_track(A, 1, 0, 0, 100, 0), _track(B, 1, 0, 6 + limit, 100, 6 + limit),
                    _track(C, 1, 0, 100, 100, 100), _track(D, 1, 0, 109.5, 100, 109.5)])
    result = review_dfm(index, _profile({"min_spacing_mm": 0.09}), ["spacing"])
    (finding,) = _found(result, "spacing")
    assert finding["marginal"] and finding["gap_mil"] == pytest.approx(3.5)
    assert finding["count"] == 1 and result["results"]["spacing"]["status"] == "marginal"


def test_copper_near_hole_walls_is_spacing():
    index = _index(
        [
            # A non-plated hole without copper: its wall is on every layer.
            _th_pad(None, 200, 200, width=40.0, hole=40.0, plated=False, component=1),
            _track(A, 1, 100, 225, 300, 225),  # 2 mil from the wall
            _track(B, 2, 195, 100, 195, 300),  # through the hole: the drill cuts it
            _pad(C, 1, 200, 170, name="2"),  # U1's own pad, 5 mil from its hole
            _track(A, 1, 500, 575, 700, 575),  # the plated hole's own net: no gap
            _track(B, 1, 500, 625, 700, 625),  # 2 mil from a plated hole without a pad
        ],
        bare_holes=[BareHole(x=600, y=600, hole=40.0, layer=74, net=A, component=None,
                             pad_name="M", plated=True)],
    )
    result = review_dfm(index, _profile({"min_spacing_mil": 6}), ["spacing"])
    found = sorted((f["severity"], f["kinds"], f["plated_hole"], f["gap_mil"])
                   for f in _found(result, "spacing"))
    # min_spacing stands in for the missing min_hole_to_copper: misses are warnings,
    # copper the drill cuts is an error.
    assert found == [
        ("error", "hole-track", False, 0.0),
        ("warning", "hole-pad", False, 5.0),
        ("warning", "hole-track", False, 2.0),
        ("warning", "hole-track", True, 2.0),
    ]
    near = next(f for f in _found(result, "spacing")
                if f["gap_mil"] == 2.0 and not f["plated_hole"])
    assert near["issue"] == (
        "1 place where track copper is 2 mil from a non-plated hole (wall to copper) on Top "
        "Layer, below the profile's minimum spacing 6 mil (it sets no min_hole_to_copper).")
    assert near["min_hole_to_copper_mil"] == 6.0
    cut = next(f for f in _found(result, "spacing") if f["gap_mil"] == 0.0)
    assert cut["issue"].endswith("on L2: the drill cuts the copper.")
    plated = next(f for f in _found(result, "spacing") if f["plated_hole"])
    assert "plated hole without a pad" in plated["issue"] and plated["nets"] == ["B", "A"]
    own = next(f for f in _found(result, "spacing") if f["kinds"] == "hole-pad")
    assert own["footprint"] == "R0402" and "U1's own" in own["issue"]
    entry = result["results"]["spacing"]
    assert entry["hole_to_copper"] == {"limit_mil": 6.0, "worst_mil": 2.0}
    assert (entry["status"], entry["objects"]) == ("fail", 4)
    assert result["board_minimums"]["min_hole_to_copper"] == {
        "value_mil": 2.0, "layer": "Top Layer", "reach_mil": 6.0, "touching_pairs": 1,
    }
    # Its own limit, and alone: copper pairs are then not compared, and said so.
    alone = review_dfm(index, _profile({"min_hole_to_copper_mil": 3}), ["spacing"])
    assert sorted(f["gap_mil"] for f in _found(alone, "spacing")) == [0.0, 2.0, 2.0]
    assert "minimum hole-to-copper clearance 3 mil." in next(
        f for f in _found(alone, "spacing") if f["gap_mil"] == 2.0)["issue"]
    assert alone["results"]["spacing"]["limit_mil"] is None
    assert "only hole walls to copper" in alone["not_checked"]["copper_gaps"]


# ---------- holes ----------


def _hole_board():
    return _index(
        [
            _via(A, 0, 0, width=20.0, hole=6.0),  # below the via drill
            _via(A, 100, 0, width=20.0, hole=7.9),  # marginal
            _th_pad(A, 200, 0, width=40.0, hole=15.0),  # below the PTH drill
            _th_pad(None, 300, 0, width=30.0, hole=30.0, plated=False),  # below the NPTH drill
            _th_pad(A, 400, 0, width=60.0, height=90.0, hole=30.0, hole_shape=2,
                    slot_length=60.0, slot_rotation=90.0),  # a routed slot under 40 mil
        ],
        bare_holes=[
            BareHole(x=500, y=0, hole=30.0, layer=74, net=None, component=None, pad_name="M",
                     plated=False),
            BareHole(x=700, y=0, hole=120.0, layer=74, net=None, component=None, pad_name="N",
                     plated=True),  # above the maximum drill
        ],
    )


HOLE_LIMITS = {"min_via_drill_mil": 8, "min_pth_drill_mil": 20, "min_npth_drill_mil": 40,
               "max_drill_mil": 100, "min_route_size_mil": 40}


def test_hole_size_per_drill_row():
    result = review_dfm(_hole_board(), _profile(HOLE_LIMITS), ["hole_size"])
    found = sorted((f["kind"], f["hole_mil"], f["severity"], bool(f.get("marginal")),
                    next(key for key in f if key.endswith("_mil") and key.startswith(("min_", "max_"))))
                   for f in _found(result, "hole_size"))
    assert found == [
        ("bare_hole", 30.0, "error", False, "min_npth_drill_mil"),
        ("bare_hole", 120.0, "error", False, "max_drill_mil"),
        ("pad", 15.0, "error", False, "min_pth_drill_mil"),
        ("pad", 30.0, "error", False, "min_npth_drill_mil"),
        ("pad", 30.0, "info", False, "min_route_size_mil"),
        ("via", 6.0, "error", False, "min_via_drill_mil"),
        ("via", 7.9, "warning", True, "min_via_drill_mil"),
    ]
    slot = next(f for f in _found(result, "hole_size", "info"))
    assert slot["slot_length_mil"] == 60.0 and "minimum route size" in slot["issue"]
    entry = result["results"]["hole_size"]
    assert entry["status"] == "fail" and entry["objects"] == 6
    assert entry["limit_mil"] == {"via": 8.0, "pth": 20.0, "npth": 40.0, "max": 100.0}
    assert entry["worst_mil"] == {"via": 6.0, "pth": 15.0, "npth": 30.0, "max": 120.0}
    minimums = result["board_minimums"]
    assert (minimums["min_via_hole_mil"], minimums["min_pth_hole_mil"],
            minimums["min_npth_hole_mil"]) == (6.0, 15.0, 30.0)
    # Unknown plating is judged as plated (a PTH drill), not as NPTH.
    unknown = review_dfm(_index([_th_pad(A, 0, 0, width=40.0, hole=15.0, plated=None)]),
                         _profile(HOLE_LIMITS), ["hole_size"])
    (finding,) = _found(unknown, "hole_size")
    assert finding.get("plated") is None and finding["min_pth_drill_mil"] == 20.0
    assert "plated (PTH) hole 15 mil" in finding["issue"]


def test_drill_table():
    result = review_dfm(_hole_board(), _default(), ["drill_count"])
    assert result["drills"] == [
        {"kind": "via", "hole_mil": 6.0, "plated": True, "span": "Top Layer - Bottom Layer",
         "count": 1},
        {"kind": "via", "hole_mil": 7.9, "plated": True, "span": "Top Layer - Bottom Layer",
         "count": 1},
        {"kind": "pad", "hole_mil": 15.0, "plated": True, "span": "Top Layer - Bottom Layer",
         "count": 1},
        {"kind": "pad", "hole_mil": 30.0, "plated": False, "span": "Top Layer - Bottom Layer",
         "count": 1},
        {"kind": "pad", "hole_mil": 30.0, "slot_length_mil": 60.0, "plated": True,
         "span": "Top Layer - Bottom Layer", "count": 1},
        {"kind": "bare_hole", "hole_mil": 30.0, "plated": False,
         "span": "Top Layer - Bottom Layer", "count": 1},
        {"kind": "bare_hole", "hole_mil": 120.0, "plated": True,
         "span": "Top Layer - Bottom Layer", "count": 1},
    ]
    # No unit container hides a count: _convert_units leaves them alone.
    converted = main_mod._convert_units(result, True)
    assert converted["drills"][0] == {"kind": "via", "hole_mm": 0.1524, "plated": True,
                                      "span": "Top Layer - Bottom Layer", "count": 1}


def test_drill_count():
    index = _hole_board()
    result = review_dfm(index, _profile({"max_drill_sizes": 3}), ["drill_count"])
    (finding,) = _found(result, "drill_count")
    # 6, 7.9, 15, 30, a 30 x 60 slot and 120 mil: six sizes.
    assert (finding["severity"], finding["drill_sizes"], finding["max_drill_sizes"]) == (
        "info", 6, 3)
    # More sizes than the profile's is advice (cost, tool changes): advisory, not fail.
    assert result["results"]["drill_count"] == {
        "status": "advisory", "limit": 3, "value": 6, "objects": 6, "groups": 1,
        "severity": "info",
    }
    assert review_dfm(index, _default(), ["drill_count"])["results"]["drill_count"] == {
        "status": "not_checked", "reason": "the profile sets no max_drill_sizes.",
    }


# ---------- annular ring ----------


def test_annular_ring():
    index = _index(
        [
            _via(A, 0, 0, width=15.874, hole=7.874),  # 4 mil: meets 0.1016 mm
            _via(A, 100, 0, width=15.748, hole=7.874),  # 0.1 mm against 4 mil: marginal
            _via(A, 200, 0, width=12.0, hole=8.0),  # 2 mil
            # Inner pads no larger than the hole are removed pads: skipped, not rings of 0.
            _th_pad(A, 300, 0, width=40.0, hole=24.0,
                    layer_copper={PAD_INNER_LAYERS: PadCopper(20.0, 20.0, 1)}),
            _th_pad(A, 400, 0, width=40.0, hole=24.0,
                    layer_copper={PAD_INNER_LAYERS: PadCopper(30.0, 30.0, 1)}),  # 3 mil inside
            _th_pad(A, 500, 0, width=30.0, height=56.0, hole=10.0, hole_shape=2,
                    slot_length=50.0, slot_rotation=90.0),  # 3 mil at the slot's ends
            _th_pad(A, 600, 0, width=30.0, height=70.0, hole=10.0, hole_shape=2,
                    slot_length=50.0, slot_rotation=90.0),  # 10 mil all round
            _th_pad(A, 700, 0, width=20.0, hole=18.0, hole_offset=True),  # offset: skipped
            _th_pad(None, 800, 0, width=30.0, hole=30.0, plated=False),  # NPTH: no ring
            _th_pad(A, 100, 100, width=28.0, hole=24.0, plated=None),  # unknown: plated, 2
            # A slot along the pad at 0 deg: 2.5 mil at its ends (10 mil across).
            _th_pad(A, 200, 100, width=55.0, height=30.0, hole=10.0, hole_shape=2,
                    slot_length=50.0, slot_rotation=0.0),
            # At 45 deg the slot is read along the pad's long side: 3.5 mil at its ends.
            _th_pad(A, 300, 100, width=30.0, height=57.0, hole=10.0, hole_shape=2,
                    slot_length=50.0, slot_rotation=45.0),
        ],
        bare_holes=[BareHole(x=900, y=0, hole=40.0, layer=74, net=None, component=None,
                             pad_name="M", plated=True)],
    )
    result = review_dfm(index, _default(), ["annular_ring"])
    found = sorted((f["kind"], f["ring_mil"], f["severity"], bool(f.get("marginal")), f.get("layer"))
                   for f in _found(result, "annular_ring"))
    assert found == [
        ("bare_hole", 0.0, "warning", False, None),
        ("pad", 2.0, "error", False, "Top Layer"),
        ("pad", 2.5, "error", False, "Top Layer"),
        ("pad", 3.0, "error", False, "L2"),
        ("pad", 3.0, "error", False, "Top Layer"),
        ("pad", 3.5, "error", False, "Top Layer"),
        ("via", 2.0, "error", False, None),
        ("via", 3.937, "warning", True, None),
    ]
    marginal = next(f for f in _found(result, "annular_ring") if f.get("marginal"))
    assert "'4 mil (0.1016mm)' as published" in marginal["issue"]
    assert marginal["span"] == "Top Layer - Bottom Layer" and marginal["diameter_mil"] == 15.748
    rings = {f["ring_mil"]: f for f in _found(result, "annular_ring")
             if f.get("layer") == "Top Layer"}
    assert rings[3.0]["pad_mil"] == [30.0, 56.0] and "slot 10 mil x 50 mil" in rings[3.0][
        "issue"]
    assert rings[2.5]["pad_mil"] == [55.0, 30.0] and rings[3.5]["pad_mil"] == [30.0, 57.0]
    assert rings[2.0]["pad_mil"] == [28.0, 28.0] and "plated pad hole" in rings[2.0]["issue"]
    bare = next(f for f in _found(result, "annular_ring") if f["kind"] == "bare_hole")
    assert "without an annular ring" in bare["issue"]
    assert any(note.startswith("2 inner pad layer(s)") for note in result["notes"])
    assert result["not_checked"]["pad_hole_offsets"].startswith("1 plated pad(s)")
    minimums = result["board_minimums"]
    assert (minimums["min_via_ring_mil"], minimums["min_pth_ring_mil"],
            minimums["plated_holes_without_ring"]) == (2.0, 2.0, 1)
    entry = result["results"]["annular_ring"]
    assert entry["status"] == "fail" and entry["objects"] == 8
    assert entry["limit_mil"] == {"via": 4.0, "pth": 4.0}
    # The plated hole without a ring is the worst PTH ring, as its finding says (the
    # board minimum counts rings above 0, and the holes without one apart).
    assert entry["worst_mil"] == {"via": 2.0, "pth": 0.0}


# ---------- aspect ratio ----------


def test_aspect_ratio():
    index = _index(
        [
            _via(A, 0, 0, hole=5.0),  # 10.72:1
            _via(A, 100, 0, hole=5.3),  # 10.11:1, judged 10.1: marginal
            _via(A, 200, 0, hole=5.34),  # 10.04:1, judged 10.0: passes
            _via(A, 300, 0, hole=4.0, width=10.0, layer=1, layer_to=2),  # a microvia
            _th_pad(A, 400, 0, width=12.0, hole=4.0),  # a plated pad hole: 13.4:1
            _th_pad(None, 500, 0, width=4.0, hole=4.0, plated=False),  # not plated
        ],
    )
    limits = {"max_aspect_ratio": 10, "drill_allowance_mm": 0.1}
    result = review_dfm(index, _profile(limits), ["aspect_ratio"])
    found = sorted((f["kind"], f["aspect_ratio"], bool(f.get("marginal")))
                   for f in _found(result, "aspect_ratio"))
    assert found == [("pad", 13.4, False), ("via", 10.11, True), ("via", 10.72, False)]
    assert all(f["severity"] == "warning" for f in _found(result, "aspect_ratio"))
    via = next(f for f in _found(result, "aspect_ratio") if f["aspect_ratio"] == 10.72)
    assert via["depth_mil"] == pytest.approx(DEPTH) and via["max_aspect_ratio"] == 10
    assert via["aspect_ratio_drilled"] == pytest.approx(DEPTH / (5.0 + 0.1 * MM), abs=0.01)
    assert "copper-to-copper depth" in via["issue"] and "hole as drawn" in via["issue"]
    assert result["not_checked"]["microvia_aspect_ratio"].startswith("1 microvia(s)")
    # The board's worst ratios; max_aspect_ratio names the profile's limit only.
    assert result["board_minimums"]["worst_aspect_ratio"] == 13.4
    assert result["board_minimums"]["worst_microvia_aspect_ratio"] == pytest.approx(1.35)
    assert "max_aspect_ratio" not in result["board_minimums"]
    micro = review_dfm(index, _profile({"max_microvia_aspect_ratio": 1}), ["aspect_ratio"])
    (finding,) = _found(micro, "aspect_ratio")
    assert finding["microvia"] and finding["max_microvia_aspect_ratio"] == 1
    assert "capture depth" in finding["issue"]
    flat = _index([_via(A, 0, 0, hole=5.0)], heights=False)
    assert review_dfm(flat, _profile(limits), ["aspect_ratio"])["results"]["aspect_ratio"][
        "status"] == "not_checked"


# ---------- hole to hole ----------


def test_hole_to_hole():
    holes = [
        _via(A, 0, 0, hole=10.0), _via(B, 18, 0, hole=10.0),  # 8 mil: below the via limit
        _via(A, 100, 0, hole=10.0), _via(B, 119.9, 0, hole=10.0),  # 9.9: marginal
        _th_pad(A, 300, 0, hole=10.0), _th_pad(B, 325, 0, hole=10.0),  # pads 15 mil apart
    ]
    overlaps = [
        _via(A, 200, 0, hole=10.0), _via(A, 200, 0, hole=10.0),  # drilled twice
        _via(A, 400, 0, hole=10.0), _via(B, 406, 0, hole=10.0),  # overlapping
        _th_pad(A, 500, 0, width=60.0, hole=40.0), _via(C, 500, 0, hole=10.0),  # concentric
    ]
    rules = [_rule("HoleToHoleClearance", GAP="10mil")]
    index = _index(holes + overlaps, rules=rules)
    limits = {"min_via_hole_to_hole_mil": 10, "min_pad_hole_to_hole_mil": 20}
    result = review_dfm(index, _profile(limits), ["hole_to_hole"])
    found = sorted((f["kinds"], f["gap_mil"], f["severity"], bool(f.get("marginal")))
                   for f in _found(result, "hole_to_hole"))
    assert found == [
        ("pad-pad", 15.0, "error", False),
        ("pad-via", 0.0, "error", False),  # concentric, different sizes: an overlap
        ("via-via", 0.0, "error", False),  # overlapping, not coincident
        ("via-via", 0.0, "warning", False),  # coincident: drilled twice
        ("via-via", 8.0, "error", False),
        ("via-via", 9.9, "warning", True),
    ]
    zero = {(f["kinds"], f["severity"]): f["issue"] for f in _found(result, "hole_to_hole")
            if f["gap_mil"] == 0.0}
    assert "drilled twice" in zero[("via-via", "warning")]
    assert "overlap: overlapping drills" in zero[("via-via", "error")]
    assert "overlap: overlapping drills" in zero[("pad-via", "error")]
    pads = next(f for f in _found(result, "hole_to_hole") if f["kinds"] == "pad-pad")
    assert pads["min_pad_hole_to_hole_mil"] == 20.0
    entry = result["results"]["hole_to_hole"]
    assert entry["limit_mil"] == {"via": 10.0, "pad": 20.0} and entry["status"] == "fail"
    assert "note" not in entry
    # Altimade sets no hole-to-hole limit: the gaps are not compared, but drill overlaps
    # need no limit and are reported all the same.
    default = review_dfm(index, _default(), ["hole_to_hole"])
    assert sorted((f["kinds"], f["severity"]) for f in _found(default, "hole_to_hole")) == [
        ("pad-via", "error"), ("via-via", "error"), ("via-via", "warning")]
    entry = default["results"]["hole_to_hole"]
    assert (entry["status"], entry["objects"], entry["limit_mil"]) == ("fail", 3, {})
    assert entry["note"].startswith("The profile sets no min_hole_to_hole (")
    assert "overlapping and coincident holes were checked all the same" in entry["note"]
    assert "HoleToHoleClearance rule asks for 10 mil" in entry["note"]
    assert "min_pad_hole_to_hole_mil" not in _found(default, "hole_to_hole")[0]
    assert default["board_minimums"]["min_hole_to_hole"] == {
        "value_mil": 8.0, "reach_mil": pytest.approx(0.5 * MM, abs=1e-3), "overlapping_pairs": 3,
    }
    # Without overlaps nothing is checked, and the reason says what was looked for.
    clean = review_dfm(_index(holes, rules=rules), _default(), ["hole_to_hole"])
    entry = clean["results"]["hole_to_hole"]
    assert entry["status"] == "not_checked"
    assert entry["reason"].startswith("the profile sets no min_hole_to_hole")
    assert "no holes overlap. The board's HoleToHoleClearance rule asks for 10 mil." in entry[
        "reason"]


# ---------- copper to edge ----------


def test_copper_to_edge():
    finger = dict(kind="pad", layer=1, width=20.0, height=60.0, shape=PAD_SHAPE_RECT)
    primitives = [
        _track(A, 1, 100, 10, 300, 10),  # 7 mil from the bottom edge
        _track(A, 1, 400, 12.9, 600, 12.9),  # 9.9 mil: marginal
        _track(C, 1, -50, 500, 50, 500, width=4.0),  # across the left edge
        _track(B, 1, 2000, 2000, 2100, 2000),  # off the board, free
        _pad(B, 2, 3000, 3000), _pad(B, 2, 3050, 3000, name="2"),  # a part parked off it
        _th_pad(GND, 1000, 300, width=60.0, hole=40.0, component=3),  # a half hole
        _track(A, 1, 420, 395, 480, 395, width=4.0),  # 3 mil from U1's own cutout
    ]
    primitives += [
        Primitive(net=A, component=1, x1=x, y1=965, x2=x, y2=965, pad_name=str(n), **finger)
        for n, x in enumerate(range(100, 400, 50))  # six fingers 5 mil from the top edge
    ]
    index = _index(primitives, areas=[_area(1, _square(6, 600, 50), A)],
                   regions=_region(56, _square(400, 400, 100), kind=3, component=1))
    result = review_dfm(index, _profile({"min_copper_to_edge_mil": 10}), ["copper_to_edge"])
    found = sorted((f["severity"], f["kind"], f.get("distance_mil"), bool(f.get("marginal")))
                   for f in _found(result, "copper_to_edge"))
    assert found == [
        ("error", "track", 0.0, False),
        ("error", "track", 3.0, False),
        ("error", "track", 7.0, False),
        ("info", "off_board", None, False),
        ("info", "off_board", None, False),
        ("info", "plated_hole", None, False),
        ("warning", "pad", 5.0, False),
        ("warning", "region", 6.0, False),
        ("warning", "track", 9.9, True),
    ]
    crossing = next(f for f in _found(result, "copper_to_edge") if f.get("distance_mil") == 0.0)
    assert "reach or cross the board outline" in crossing["issue"]
    cutout = next(f for f in _found(result, "copper_to_edge") if f.get("distance_mil") == 3.0)
    assert "closer to the part's own cutout than" in cutout["issue"]
    fingers = next(f for f in _found(result, "copper_to_edge") if f["kind"] == "pad")
    assert fingers["designator"] == "U1" and fingers["count"] == 6
    assert "Edge-connector fingers of U1: 6 pads" in fingers["issue"]
    parked = next(f for f in _found(result, "copper_to_edge") if f.get("designator") == "U2")
    assert parked["count"] == 2 and "components_outside_board" in parked["issue"]
    entry = result["results"]["copper_to_edge"]
    assert entry["status"] == "fail" and entry["objects"] == 5
    assert entry["worst_mil"] == 0.0
    near = result["board_minimums"]["min_copper_to_edge"]
    assert near["value_mil"] == 0.0 and near["reach_mil"] == pytest.approx(MM)
    outline_free = _index([_track(A, 1, 0, 0, 10, 0)], outline=None)
    result = review_dfm(outline_free, _default(), ["copper_to_edge"])
    assert result["results"]["copper_to_edge"] == {
        "status": "not_checked", "reason": "the board has no outline.",
    }
    assert "min_copper_to_edge" in result["board_minimums"]["not_measured"]


def test_edge_fingers_are_judged_at_the_limit_and_holes_near_the_edge():
    finger = dict(kind="pad", layer=1, width=20.0, height=60.0, shape=PAD_SHAPE_RECT)
    # U1: 2 SMD pads 5 mil from the top edge and 4 more 30 mil from it. Within the
    # profile's 10 mil only 2: errors, not edge-connector fingers (at 1 mm all 6 are).
    primitives = [
        Primitive(net=A, component=1, x1=x, y1=965 if n < 2 else 940, x2=x,
                  y2=965 if n < 2 else 940, pad_name=str(n), **finger)
        for n, x in enumerate(range(100, 400, 50))
    ]
    # U2: through-hole pads in a row 5 mil from the bottom edge: no fingers either.
    primitives += [_th_pad(B, x, 35, width=20.0, height=60.0, hole=10.0, component=2)
                   for x in range(100, 400, 50)]
    # A non-plated pad across the left edge: its ring and its hole cross it (no
    # edge-plated hole); a non-plated bare hole 8 mil from the right edge.
    primitives.append(_th_pad(None, 5, 500, width=40.0, hole=20.0, plated=False))
    bare = [BareHole(x=972, y=500, hole=40.0, layer=74, net=None, component=None,
                     pad_name="M", plated=False)]
    index = _index(primitives, bare_holes=bare)
    result = review_dfm(index, _profile({"min_copper_to_edge_mil": 10}), ["copper_to_edge"])
    found = sorted((f["kind"], f["severity"], f["count"], f["distance_mil"])
                   for f in _found(result, "copper_to_edge"))
    assert found == [
        ("hole", "error", 1, 0.0),
        ("hole", "warning", 1, 8.0),
        ("pad", "error", 1, 0.0),
        ("pad", "error", 8, 5.0),
    ]
    near = next(f for f in _found(result, "copper_to_edge") if f.get("distance_mil") == 8.0)
    assert near["issue"] == (
        "1 non-plated hole closer to the board outline than the profile's copper-to-edge "
        "clearance 10 mil (it sets no min_hole_to_edge) (wall to edge): 8 mil from it.")
    assert near["plated_hole"] is False and near["min_hole_to_edge_mil"] == 10.0
    crossing = next(f for f in _found(result, "copper_to_edge")
                    if f["kind"] == "hole" and f["distance_mil"] == 0.0)
    assert "reach or cross the board outline: the drill runs off the board" in crossing[
        "issue"]
    entry = result["results"]["copper_to_edge"]
    assert entry["hole_to_edge"] == {"limit_mil": 10.0, "worst_mil": 0.0}
    assert (entry["status"], entry["objects"]) == ("fail", 11)
    assert result["board_minimums"]["min_hole_to_edge"] == {
        "value_mil": 0.0, "reach_mil": pytest.approx(MM)}
    # Its own limit: the bare hole at 8 mil passes 5 mil.
    own = review_dfm(index, _profile({"min_copper_to_edge_mil": 10, "min_hole_to_edge_mil": 5}),
                     ["copper_to_edge"])
    holes = [f for f in _found(own, "copper_to_edge") if f["kind"] == "hole"]
    assert [f["distance_mil"] for f in holes] == [0.0]


def test_copper_off_the_board_is_info_and_the_worst_is_the_nearest_searched():
    index = _index(
        [_track(A, 1, 200, -9, 300, -9),  # 6 mil below the outline
         _track(B, 1, 500, 23, 600, 23)],  # 20 mil inside: beyond the 10 mil limit
        areas=[_area(1, _square(700, -60, 54), A)],  # a region 6 mil below it
        regions=_region(56, [(500, 500), (500, 500), (500, 500)], kind=3),
    )
    result = review_dfm(index, _profile({"min_copper_to_edge_mil": 10}), ["copper_to_edge"])
    (finding,) = _found(result, "copper_to_edge")
    assert (finding["kind"], finding["severity"], finding["count"]) == ("off_board", "info", 2)
    entry = result["results"]["copper_to_edge"]
    # Nothing within the limit, but the nearest copper of the 1 mm search is said.
    assert (entry["status"], entry["worst_mil"]) == ("pass", 20.0)
    assert result["board_minimums"]["min_copper_to_edge"]["value_mil"] == 20.0
    # A cutout whose vertices are all on one spot is no edge: said, not a crash.
    assert any(note.startswith("1 board cutout(s) have no edge of non-zero length")
               for note in result["notes"])


# ---------- layer count, copper weight and board size ----------


def test_layer_count_and_copper_weight():
    three = _index(inner_layers=1)  # 3 copper layers
    default = review_dfm(three, _default(), ["layer_count"])
    (finding,) = _found(default, "layer_count")
    assert finding["severity"] == "info" and "Altimade is only the default" in finding["issue"]
    assert default["results"]["layer_count"]["value"] == 3
    chosen = review_dfm(three, _profile(name="altimade"), ["layer_count"])
    assert [f["severity"] for f in _found(chosen, "layer_count")] == ["error"]
    assert review_dfm(_index(), _profile({"layer_counts": [2, 6]}), ["layer_count"])[
        "results"]["layer_count"]["status"] == "fail"
    # Copper weight is its own check: heavy copper leaves layer_count alone.
    heavy = review_dfm(_index(copper=2.8), _profile(name="altimade"),
                       ["layer_count", "copper_weight"])
    assert heavy["results"]["layer_count"] == {
        "status": "pass", "limit": [2, 4, 6, 8], "value": 4, "objects": 0, "groups": 0}
    (copper,) = _found(heavy, "copper_weight")
    assert (copper["severity"], copper["copper_oz"], copper["max_copper_oz"],
            copper["count"]) == ("warning", 2.0, 1.0, 4)
    # results: the profile's limit and the board's heaviest copper (worst), in oz.
    assert heavy["results"]["copper_weight"] == {
        "status": "fail", "limit": 1.0, "worst": 2.0, "objects": 4, "groups": 1,
        "severity": "warning"}
    # 1.42 mil is 1 oz as quoted (0.1 oz steps): not a marginal miss.
    assert not _found(review_dfm(_index(copper=1.42), _default(), ["copper_weight"]),
                      "copper_weight")
    legacy = review_dfm(_index(legacy=True), _default(), ["layer_count", "copper_weight"])
    assert {check: entry["status"] for check, entry in legacy["results"].items()} == {
        "layer_count": "not_checked", "copper_weight": "not_checked"}
    flat = review_dfm(_index(heights=False), _default(), ["copper_weight"])
    assert flat["results"]["copper_weight"]["reason"] == (
        "the layer stack gives no copper thickness.")


def test_board_size_includes_arc_bulges_and_is_orientation_free():
    # The right edge is a half circle bulging 500 mil past the vertices.
    outline = [ShapeVertex(0, 0), ShapeVertex(1000, 0, (1000, 500, 500, 270, 90)),
               ShapeVertex(1000, 1000), ShapeVertex(0, 1000)]
    index = _index(outline=outline)
    result = review_dfm(index, _profile({"max_board_size_mil": [2000, 1200]}), ["board_size"])
    assert result["board"]["size_mil"] == [1500.0, 1000.0]
    assert result["results"]["board_size"]["status"] == "pass"  # 1000 x 1500 in 1200 x 2000
    small = review_dfm(index, _profile({"max_board_size_mil": [1400, 1200]}), ["board_size"])
    (finding,) = _found(small, "board_size")
    assert finding["severity"] == "error" and "above the maximum" in finding["issue"]
    assert finding["size_mil"] == [1500.0, 1000.0]
    assert review_dfm(_index(outline=None), _profile({"min_board_size_mil": 10}),
                      ["board_size"])["results"]["board_size"]["status"] == "not_checked"


# ---------- rules against the profile ----------


def test_rules_vs_profile():
    rules = [
        _rule("Clearance", GENERICCLEARANCE="4mil"),
        _rule("Width", MINLIMIT="2mil", MAXLIMIT="20mil", PREFEREDWIDTH="5mil"),
        _rule("HoleSize", MINLIMIT="1mil", MAXLIMIT="100mil", ABSOLUTEVALUES="TRUE"),
        _rule("RoutingVias", MINHOLEWIDTH="6mil"),
        _rule("BoardOutlineClearance", GENERICCLEARANCE="8mil"),
        _rule("HoleToHoleClearance", GAP="9.9mil"),
    ]
    index = _index([_via(A, 500, 500, width=20.0, hole=7.0)], rules=rules)
    limits = {"min_spacing_mil": 5, "min_trace_mil": 3, "min_via_drill_mil": 8,
              "min_annular_ring_mil": 4, "min_copper_to_edge_mil": 10,
              "min_hole_to_hole_mil": 10}
    result = review_dfm(index, _profile(limits))
    found = {(f["kind"], f["severity"], bool(f.get("marginal"))): f
             for f in _found(result, "rules_vs_profile")}
    assert sorted(found) == [
        ("BoardOutlineClearance", "warning", False),
        ("Clearance", "warning", False),
        ("HoleSize", "info", False),
        ("HoleToHoleClearance", "warning", True),
        ("MinimumAnnularRing", "info", False),
        ("Width", "warning", False),
    ]
    clearance = found[("Clearance", "warning", False)]
    assert clearance["issue"] == (
        "Rule 'Clearance' (Clearance) sets the minimum clearance to 4 mil, looser than the "
        "profile's 5 mil: the copper meets the profile today, but Altium's DRC would pass a "
        "later edit that does not. Raise the rule to 5 mil.")
    assert clearance["rule_value_mil"] == 4.0 and clearance["min_spacing_mil"] == 5.0
    assert clearance["sets"] == "minimum clearance"
    assert clearance["rules"] == [{"rule": "Clearance", "value_mil": 4.0, "scope": "All"}]
    assert "Altium's default 1 mil" in found[("HoleSize", "info", False)]["issue"]
    assert "No enabled MinimumAnnularRing rule" in found[("MinimumAnnularRing", "info", False)][
        "issue"]
    # RoutingVias is looser and the via hole fails too: the rule rides on the hole finding.
    (hole,) = _found(result, "hole_size")
    assert hole["board_rule"]["name"] == "RoutingVias"
    rows = {row["kind"]: row for row in result["results"]["rules_vs_profile"]["rules"]}
    assert rows["RoutingVias"]["copper"] == "fails" and rows["Clearance"]["copper"] == "passes"
    assert rows["RoutingVias"]["covered_by"] == "hole_size"
    assert rows["RoutingVias"]["sets"] == "minimum hole" and "covered_by" not in rows["Clearance"]
    assert result["results"]["hole_size"]["board_rule"]["value_mil"] == 6.0
    assert result["results"]["rules_vs_profile"]["status"] == "fail"


def test_rules_vs_profile_sides_and_widths_of_pairs():
    # HoleSize looser on both sides: only the maximum side's holes fail, so the minimum
    # side is a warning here and the maximum rides on the 120 mil hole's finding.
    rules = [_rule("HoleSize", MINLIMIT="4mil", MAXLIMIT="200mil", ABSOLUTEVALUES="TRUE"),
             _rule("DiffPairsRouting", TOPLAYER_MINWIDTH="2mil")]
    index = _index([_via(A, 500, 500, width=30.0, hole=10.0), _track(A, 1, 0, 0, 100, 0)],
                   bare_holes=[BareHole(x=700, y=700, hole=120.0, layer=74, net=None,
                                        component=None, pad_name="N", plated=True)],
                   rules=rules)
    limits = {"min_drill_mil": 8, "max_drill_mil": 100, "min_trace_mil": 4}
    result = review_dfm(index, _profile(limits), ["trace_width", "hole_size",
                                                  "rules_vs_profile"])
    rows = {row["sets"]: row for row in result["results"]["rules_vs_profile"]["rules"]}
    assert (rows["minimum hole"]["copper"], rows["minimum hole"]["value_mil"]) == ("passes", 4.0)
    assert (rows["maximum hole"]["copper"], rows["maximum hole"]["covered_by"]) == (
        "fails", "hole_size")
    assert rows["minimum width"]["kind"] == "DiffPairsRouting"
    warnings = {f["sets"]: f for f in _found(result, "rules_vs_profile", "warning")}
    assert sorted(warnings) == ["minimum hole", "minimum width"]
    assert warnings["minimum hole"]["board_rule"]["value_mil"] == 4.0
    assert "sets the minimum width to 2 mil" in warnings["minimum width"]["issue"]
    (large,) = _found(result, "hole_size")
    assert large["max_drill_mil"] == 100.0 and large["board_rule"]["value_mil"] == 200.0
    entry = result["results"]["rules_vs_profile"]
    assert (entry["status"], entry["severity"], entry["objects"]) == ("fail", "warning", 3)


def test_copper_rules_do_not_ride_on_hole_wall_findings():
    # Only hole walls miss: the Clearance and BoardOutlineClearance rules (copper to
    # copper, copper to edge) are looser while the copper itself passes.
    index = _index(
        [_th_pad(None, 200, 200, width=40.0, hole=40.0, plated=False),
         _track(A, 1, 100, 225, 300, 225)],  # 2 mil from the hole's wall
        bare_holes=[BareHole(x=25, y=500, hole=40.0, layer=74, net=None, component=None,
                             pad_name="M", plated=False)],  # 5 mil from the left edge
        rules=[_rule("Clearance", GENERICCLEARANCE="4mil"),
               _rule("BoardOutlineClearance", GENERICCLEARANCE="8mil")],
    )
    limits = {"min_spacing_mil": 5, "min_copper_to_edge_mil": 10}
    result = review_dfm(index, _profile(limits), ["spacing", "copper_to_edge",
                                                  "rules_vs_profile"])
    assert {f["kinds"] for f in _found(result, "spacing")} == {"hole-track"}
    assert {f["kind"] for f in _found(result, "copper_to_edge")} == {"hole"}
    rows = {row["kind"]: row for row in result["results"]["rules_vs_profile"]["rules"]}
    assert rows["Clearance"]["copper"] == rows["BoardOutlineClearance"]["copper"] == "passes"
    assert not any("covered_by" in row for row in rows.values())
    assert {f["kind"] for f in _found(result, "rules_vs_profile", "warning")} == {
        "Clearance", "BoardOutlineClearance"}
    assert all("board_rule" not in f for f in _found(result, "copper_to_edge"))


def test_rules_riding_on_a_measured_finding_keep_the_check_at_warning():
    # The only looser rule rides on the failing spacing: rules_vs_profile still fails,
    # with severity warning (not the unrelated info of an absent rule).
    result = review_dfm(_spacing_board(), _profile({"min_spacing_mil": 5,
                                                     "min_annular_ring_mil": 4}),
                        ["spacing", "rules_vs_profile"])
    entry = result["results"]["rules_vs_profile"]
    assert [row["covered_by"] for row in entry["rules"]] == ["spacing"]
    assert (entry["status"], entry["severity"], entry["objects"], entry["groups"]) == (
        "fail", "warning", 1, 1)
    assert [f["severity"] for f in _found(result, "rules_vs_profile")] == ["info"]


def test_a_looser_via_rule_is_reported_when_only_pad_holes_fail():
    index = _index([_via(A, 0, 0, hole=10.0), _th_pad(A, 100, 0, width=40.0, hole=15.0)],
                   rules=[_rule("RoutingVias", MINHOLEWIDTH="6mil")])
    result = review_dfm(index, _profile({"min_via_drill_mil": 8, "min_pth_drill_mil": 20}))
    (hole,) = _found(result, "hole_size")
    assert hole["kind"] == "pad" and "board_rule" not in hole
    (rule,) = _found(result, "rules_vs_profile", "warning")
    assert rule["kind"] == "RoutingVias" and "the copper meets the profile" in rule["issue"]
    assert result["results"]["rules_vs_profile"]["rules"][0]["copper"] == "passes"
    # Narrowed to the rules, the measured check did not run: said so.
    narrowed = review_dfm(index, _profile({"min_via_drill_mil": 8}), ["rules_vs_profile"])
    (rule,) = _found(narrowed, "rules_vs_profile", "warning")
    assert "not checked against the profile" in rule["issue"]
    assert narrowed["results"]["rules_vs_profile"]["rules"][0]["copper"] == "not_checked"


def test_clearance_rule_rides_on_failing_spacing():
    result = review_dfm(_spacing_board(), _profile({"min_spacing_mil": 5}))
    assert not [f for f in _found(result, "rules_vs_profile") if f["kind"] == "Clearance"]
    (row,) = [row for row in result["results"]["rules_vs_profile"]["rules"]
              if row["kind"] == "Clearance"]
    assert row["copper"] == "fails"


# ---------- the whole result ----------


def test_result_shape_board_facts_and_units():
    index = _spacing_board()
    result = review_dfm(index, _default())
    assert list(result) == [
        "checks_run", "scope", "profile", "origin_mil", "board", "board_minimums", "results",
        "severity_counts", "drills", "findings", "not_checked", "notes",
    ]
    assert result["scope"].startswith("Checked against Altimade's published limits (default")
    assert "scope" not in result["profile"] and result["profile"]["chosen_by"] == "default"
    assert result["origin_mil"] == [100.0, 50.0]
    assert result["board"] == {
        "copper_layers": 4, "stack_thickness_mil": DEPTH, "total_thickness_mil": DEPTH + 0.8,
        "size_mil": [1000.0, 1000.0], "embedded_boards": 0,
    }
    assert set(result["results"]) == set(VALID_CHECKS)
    for entry in result["results"].values():
        assert entry["status"] in ("pass", "fail", "marginal", "advisory", "not_checked")
    assert result["severity_counts"] == {
        level: sum(1 for f in result["findings"] if f["severity"] == level)
        for level in ("error", "warning", "info")
    }
    ranks = [("error", "warning", "info").index(f["severity"]) for f in result["findings"]]
    assert ranks == sorted(ranks)
    for key in ("solder_mask", "silkscreen", "acute_angles", "copper_text", "plane_pullback",
                "same_net_gaps", "panels", "courtyards", "component_heights",
                "pick_and_place", "paste_stencil", "board_thickness", "no_net_gaps",
                "inside_valid_pours", "other_side_pins"):
        assert key in result["not_checked"]
    assert "assembly" not in result["not_checked"]
    assert any("Altimade's published limits were used" in note for note in result["notes"])
    # Issue texts follow the units: mm with enough decimals to tell value from limit.
    mm = review_dfm(index, _default(), ["spacing"], to_mm=True)
    marginal = next(f for f in _found(mm, "spacing") if f.get("marginal"))
    assert "0.1257 mm apart (track-track 1) on L2, below the profile's minimum spacing " \
        "0.127 mm" in marginal["issue"]
    converted = main_mod._convert_units(mm, True)
    assert converted["origin_mm"] == [2.54, 1.27]
    assert converted["profile"]["limits"][1] == {
        "limit": "min_spacing_mm", "value_mm": 0.127, "published": "5 mil (0.127mm)",
        "origin": "builtin",
    }


def test_panels_back_drills_and_stale_pours_are_said():
    index = _index(embedded=2, pours=[
        PolygonPour(0, GND, 1, "Solid", _square(0, 0, 10), copper_invalid=True),
        PolygonPour(1, GND, 2, "Solid", _square(0, 0, 10), shelved=True),
    ])
    result = review_dfm(index, _default(), ["board_size"])
    assert result["board"]["embedded_boards"] == 2
    assert result["not_checked"]["panels"].startswith("This PcbDoc holds 2 embedded board")
    assert any("1 pour(s) modified since" in note and "1 shelved pour(s)" in note
               for note in result["notes"])


def test_narrowed_checks_measure_no_more_than_asked():
    index = _spacing_board()
    result = review_dfm(index, _default(), ["layer_count"])
    assert list(result["results"]) == ["layer_count"]
    assert result["board_minimums"]["not_measured"] == [
        "min_gap", "min_hole_to_copper", "min_hole_to_hole", "min_copper_to_edge",
        "min_hole_to_edge", "min_part_gap", "min_part_to_edge"]
    assert not any(index._dfm_geometry.results[name] for name in (
        "spacing", "hole_to_copper", "copper_to_edge", "hole_to_edge", "hole_to_hole"))
    # Hole, ring and trace minimums are cheap: always there.
    assert "min_trace" in result["board_minimums"] and "worst_aspect_ratio" in result[
        "board_minimums"]


# ---------- paging and the cache ----------


def test_paging_cuts_findings_examples_and_drill_rows():
    full = review_dfm(_spacing_board(), _profile({"min_spacing_mil": 5}))
    snapshot = json.dumps(full, sort_keys=True)
    total = len(full["findings"])
    page = page_dfm(full, 0, 2, examples=1)
    assert len(page["findings"]) == 2 and page["total"] == total
    assert page["has_more"] and page["next_offset"] == 2 and page["offset"] == 0
    assert all(len(f.get("examples", [])) <= 1 for f in page["findings"])
    last = page_dfm(full, 2, 100, examples=0)
    assert not last["has_more"] and "next_offset" not in last
    assert len(last["findings"]) == total - 2
    assert all("examples" not in f for f in last["findings"])
    assert list(last)[-2:] == ["not_checked", "notes"]
    holes = review_dfm(_hole_board(), _default(), ["drill_count"])
    cut = page_dfm(holes, 0, 100, 5, drill_rows=2)
    assert cut["drills"] == holes["drills"][:2] and cut["drills_total"] == 7
    assert json.dumps(full, sort_keys=True) == snapshot  # the cached result is untouched


def test_paging_boundaries_order_nets_and_spans():
    # Six tracks of six nets at one width, and a 1 mil track: worst (by margin) first.
    tracks = [_track(net, 1, 0, 100 * net, 50, 100 * net, width=2.9) for net in range(5)]
    tracks += [Primitive(kind="track", layer=1, net=5, component=None, x1=0, y1=600, x2=50,
                         y2=600, width=2.9),
               _track(A, 1, 300, 0, 350, 0, width=1.0),
               _via(A, 500, 500, layer=1, layer_to=99)]  # to a layer not in the stack
    index = _index(tracks)
    index.pcb.nets.append("E")
    full = review_dfm(index, _profile({"min_trace_mil": 3}), ["trace_width", "drill_count"])
    assert [f["width_mil"] for f in _found(full, "trace_width")] == [1.0, 2.9]
    total = len(full["findings"])
    exact = page_dfm(full, 0, total)
    assert not exact["has_more"] and "next_offset" not in exact
    short = page_dfm(full, 0, total - 1)
    assert short["has_more"] and short["next_offset"] == total - 1
    # Sample nets: at most NETS_LISTED kept, and a page keeps max(PAGE_NETS, examples).
    wide = next(f for f in full["findings"] if f.get("width_mil") == 2.9)
    assert len(wide["nets"]) == dfm.NETS_LISTED == 5
    for examples, listed in ((0, 3), (2, 3), (4, 4), (5, 5)):
        page = page_dfm(full, 0, 100, examples)
        assert len(next(f for f in page["findings"] if f.get("width_mil") == 2.9)[
            "nets"]) == listed
    # A via to a layer the stack does not have is still labelled.
    assert {row["span"] for row in full["drills"]} == {"Top Layer - layer 99"}


def test_a_replaced_index_is_freed_without_a_garbage_collection():
    index = _spacing_board()
    review_dfm(index, _profile({"min_spacing_mil": 5, "min_copper_to_edge_mil": 10,
                                "min_hole_to_hole_mil": 10}))
    ref = weakref.ref(index)
    gc.disable()
    try:
        del index
        assert ref() is None
    finally:
        gc.enable()


def test_full_result_is_cached_on_the_index():
    index = _spacing_board()
    profile = _default()
    first = review_dfm(index, profile)
    assert review_dfm(index, _default()) is first  # an equal profile hits the cache
    assert review_dfm(index, profile, to_mm=True) is not first
    assert review_dfm(index, profile, ["spacing"]) is not first
    assert review_dfm(index, _profile({"min_spacing_mil": 6}, name="altimade")) is not first
    review_dfm(index, profile, ["board_size"])
    assert len(index._dfm_cache) == dfm.CACHE_SIZE


def test_worst_page_fits_on_a_synthetic_board():
    """Many classes with long net names: page (40, 3) stays under the tool's limit."""
    nets = [f"VERY_LONG_NET_NAME_FOR_SIZE_TESTING_{number:04d}" for number in range(400)]
    primitives = []
    for number in range(200):
        x = (number % 20) * 100.0
        y = (number // 20) * 100.0
        width = 1.0 + number * 0.01
        primitives += [
            Primitive(kind="track", layer=1, net=2 * number, component=None, x1=x, y1=y,
                      x2=x + 50, y2=y, width=width),
            Primitive(kind="track", layer=1, net=2 * number + 1, component=None, x1=x,
                      y1=y + width + number * 0.004, x2=x + 50, y2=y + width + number * 0.004,
                      width=width),
        ]
    pcb = PcbData(board=_board(), nets=nets, components=[], primitives=primitives, pours=[],
                  layer_name_to_v6=dict(LAYERS))
    full = review_dfm(PcbIndex(pcb, None), _profile({"min_spacing_mil": 5, "min_trace_mil": 5}))
    assert len(full["findings"]) > 100
    page = page_dfm(full, 0, *SHRINK_STEPS[1])
    assert len(_rendered(page, True)) < 60000 and len(_rendered(page)) < 60000


# ---------- assembly (DFA) ----------

ASSEMBLY = list(dfm._ASSEMBLY)
# Assembly limits every check misses on MiniPC (part spacing at its 5 mm bound).
ASSEMBLY_STRICT = {
    "max_placements": 1, "max_unique_parts": 1, "min_part_spacing_mm": 5.0,
    "min_part_to_edge_mm": 10.0, "min_global_fiducials": 10, "local_fiducial_pitch_mm": 2.54,
    "min_pitch_mm": 2.54,
}


def _variant(*dnp, unique=None, netlist_free=False, name="Lite"):
    return dfm.AssemblyContext(variant=name, is_dnp=lambda refdes: refdes in dnp,
                               dnp_key=tuple(sorted(dnp)), unique_parts=unique,
                               netlist_free=netlist_free)


def _parts_board(resistors=3, bottom_smd=False):
    """resistors 0402 resistors on top (netted pads), J1 a through-hole header on the
    bottom, MH1 a no-BOM mounting hole and LOGO1 a graphical part; bottom_smd adds C1, an
    SMD capacitor on the bottom."""
    components, primitives = [], []
    for number in range(resistors):
        x = 100.0 + 60.0 * (number % 12)
        y = 100.0 + 60.0 * (number // 12)
        components.append(facts._part(number, f"R{number + 1}", x, y, footprint="R0402",
                                      comment="10k"))
        primitives += [facts._smd(number, x - 10, y, 10.0, net=A),
                       facts._smd(number, x + 10, y, 10.0, net=B)]
    header = len(components)
    components += [facts._part(header, "J1", 500, 800, side="bottom", footprint="HDR",
                               comment="hdr"),
                   facts._part(header + 1, "MH1", 800, 800, kind=5),
                   facts._part(header + 2, "LOGO1", 900, 900, kind=2)]
    primitives += [facts._tht(header, 500, 800, net=A), facts._tht(header, 600, 800, net=B),
                   facts._tht(header + 1, 800, 800, plated=False),
                   facts._smd(header + 2, 900, 900, net=A)]
    if bottom_smd:
        components.append(facts._part(header + 3, "C1", 300, 600, side="bottom",
                                      footprint="C0402", comment="1u"))
        primitives += [facts._smd(header + 3, 290, 600, 10.0, net=A, layer=32),
                       facts._smd(header + 3, 310, 600, 10.0, net=B, layer=32)]
    return facts._index(components, primitives, outline=facts.OUTLINE)


def test_part_counts_against_the_profile_and_the_variant():
    index = _parts_board()
    limits = {"max_placements": 3, "max_unique_parts": 1}
    full = review_dfm(index, _profile(limits), ["part_counts"])
    entry = full["results"]["part_counts"]
    # Placements: R1-R3 and J1; the no-BOM mounting hole and the logo are not placed.
    assert entry == {"status": "fail", "limit": {"placements": 3, "unique_parts": 1},
                     "value": {"placements": 4, "unique_parts": 2},
                     "unique_parts_approximate": True, "objects": 2, "groups": 2,
                     "severity": "error"}
    unique, placements = _found(full, "part_counts")  # the larger miss first
    assert (placements["placements"], placements["max_placements"]) == (4, 3)
    assert placements["issue"].startswith("4 placements (3 top, 1 bottom), above the "
                                          "profile's maximum of 3.")
    assert unique["issue"].startswith("About 2 unique parts (distinct footprint and Comment")
    # The active variant: R1 Not Fitted; with a netlist the BOM's line count is exact.
    lite = review_dfm(index, _profile(limits), ["part_counts"], variant=_variant("R1", unique=1))
    assert lite["results"]["part_counts"]["value"] == {"placements": 3, "unique_parts": 1}
    assert lite["results"]["part_counts"]["status"] == "pass"
    assert "unique_parts_approximate" not in lite["results"]["part_counts"]
    assert lite["board_minimums"]["placements"] == {"top_smd": 2, "top_tht": 0,
                                                    "bottom_smd": 0, "bottom_tht": 1}
    assert lite["board_minimums"]["unique_parts"] == {"value": 1, "approximate": False}
    assert any("active variant 'Lite'" in note for note in lite["notes"])
    # No limit: not checked, the counts still in board_minimums.
    none = review_dfm(index, _profile(), ["part_counts"])
    assert none["results"]["part_counts"] == {
        "status": "not_checked",
        "reason": "the profile sets no max_placements or max_unique_parts."}
    assert none["board_minimums"]["placements"]["top_smd"] == 3


def test_altimade_part_counts_are_advice_while_it_is_only_the_default():
    index = _parts_board(resistors=600)  # 601 placements
    default = review_dfm(index, _default(), ["part_counts"])
    entry = default["results"]["part_counts"]
    assert (entry["status"], entry["severity"], entry["limit"]) == (
        "advisory", "info", {"placements": 600, "unique_parts": 200})
    (finding,) = _found(default, "part_counts")
    assert finding["issue"].endswith("not what an assembler can place (advisory).")
    assert any("placements and unique parts) are info" in note for note in default["notes"])
    chosen = review_dfm(index, _profile(name="altimade"), ["part_counts"])
    assert chosen["results"]["part_counts"]["status"] == "fail"
    assert _found(chosen, "part_counts")[0]["severity"] == "error"


def test_assembly_sides_are_advice():
    single = review_dfm(_parts_board(), _default(), ["assembly_sides"])
    entry = single["results"]["assembly_sides"]
    assert (entry["status"], entry["sides"]) == ("advisory", ["top", "bottom"])
    (tht,) = _found(single, "assembly_sides")
    assert (tht["severity"], tht["count"], tht["side"]) == ("info", 1, "bottom")
    assert tht["issue"].startswith("1 through-hole part on the bottom side")
    assert tht["examples"] == [{"at_mil": [550.0, 800.0], "object": "J1", "side": "bottom"}]
    both = review_dfm(_parts_board(bottom_smd=True), _default(), ["assembly_sides"])
    smd = next(f for f in _found(both, "assembly_sides") if "smd_parts" in f)
    assert smd["smd_parts"] == {"top": 3, "bottom": 1} and smd["count"] == 1
    assert smd["issue"].startswith("1 SMD part on the bottom side and 3 on the top: two "
                                   "reflow passes")
    # The count is the bottom side's SMD parts (a unit of parts, as the THT class), and a
    # mixed part on the bottom is a through-hole part there too.
    components = [facts._part(0, "R1"), facts._part(1, "Q1", side="bottom"),
                  facts._part(2, "C1", side="bottom"), facts._part(3, "C2", side="bottom")]
    primitives = [facts._smd(0, 0, 0, net=A), facts._smd(0, 40, 0, net=B),
                  facts._smd(1, 200, 0, layer=32, net=A), facts._tht(1, 300, 0, net=B)]
    primitives += [facts._smd(part, x, 200, layer=32, net=net) for part, x, net in (
        (2, 0, A), (2, 40, B), (3, 200, A), (3, 240, B))]
    bottom = review_dfm(facts._index(components, primitives), _default(), ["assembly_sides"])
    smd, tht = sorted(_found(bottom, "assembly_sides"), key=lambda f: "smd_parts" not in f)
    assert (smd["count"], smd["smd_parts"]) == (3, {"top": 1, "bottom": 3})
    assert (tht["count"], [e["object"] for e in tht["examples"]]) == (1, ["Q1"])
    assert bottom["results"]["assembly_sides"]["objects"] == 4
    top = facts._index([facts._part(0, "R1")], [facts._smd(0, 0, 0, net=A),
                                                 facts._smd(0, 40, 0, net=B)])
    alone = review_dfm(top, _default(), ["assembly_sides"])
    assert alone["results"]["assembly_sides"]["status"] == "pass"
    assert not alone["findings"]


def test_part_spacing_classes_overlaps_and_the_variant():
    index = facts._spacing_board()
    full = review_dfm(index, _profile({"min_part_spacing_mil": 21.5}), ["part_spacing"])
    entry = full["results"]["part_spacing"]
    assert (entry["status"], entry["limit_mil"], entry["worst_mil"]) == ("fail", 21.5, 0.0)
    assert entry["overlapping_pairs"] == 1 and entry["severity"] == "warning"
    found = {(f["basis"], bool(f.get("marginal")), f["count"], f["gap_mil"])
             for f in _found(full, "part_spacing")}
    # E's body reaches past its pads towards F: 10 mil body to pads (mixed), as G to H.
    assert found == {("body", False, 1, 0.0), ("body", False, 1, 0.0), ("pads", False, 1, 20.0),
                     ("mixed", False, 2, 10.0), ("body", True, 1, 21.2132)}
    overlap = next(f for f in _found(full, "part_spacing") if "overlap" in f["issue"])
    assert overlap["examples"] == [{"at_mil": [4100.0, 0.0], "objects": "I / J",
                                    "side": "top", "gap_mil": 0.0}]
    assert overlap["issue"].startswith("1 pair of placed parts whose 3D bodies overlap")
    marginal = next(f for f in _found(full, "part_spacing") if f.get("marginal"))
    assert "confirm with the assembler" in marginal["issue"]
    pads = next(f for f in _found(full, "part_spacing") if f["basis"] == "pads")
    assert "20 mil between their pads (pad copper, or a rectangle around the pads of a part " \
        "without a 3D body)" in pads["issue"]
    mixed = next(f for f in _found(full, "part_spacing") if f["basis"] == "mixed")
    assert mixed["gap_range_mil"] == [10.0, 20.0]
    assert "10 mil to 20 mil from one part's 3D body to the other's pads" in mixed["issue"]
    assert full["board_minimums"]["min_part_gap"] == {
        "value_mil": 0.0, "basis": "body", "reach_mil": pytest.approx(196.8504, abs=1e-4),
        "overlapping_pairs": 1}
    # No limit: overlapping bodies alone are advice.
    bare = review_dfm(index, _profile(), ["part_spacing"])
    (finding,) = _found(bare, "part_spacing")
    assert bare["results"]["part_spacing"]["status"] == "advisory"
    assert finding["severity"] == "info" and finding["issue"].endswith("so this is advisory.")
    # J Not Fitted: no overlap left, so nothing to report without a limit.
    lite = review_dfm(index, _profile(), ["part_spacing"], variant=_variant("J"))
    assert lite["results"]["part_spacing"] == {
        "status": "not_checked",
        "reason": "the profile sets no min_part_spacing, and no fitted parts' outlines "
                  "overlap."}
    assert lite["board_minimums"]["min_part_gap"]["value_mil"] == 0.0
    assert "overlapping_pairs" not in lite["board_minimums"]["min_part_gap"]
    # With the limit, a pair with a Not Fitted part is no miss: C out, C / D goes.
    narrow = review_dfm(index, _profile({"min_part_spacing_mil": 21.5}), ["part_spacing"],
                        variant=_variant("C"))
    assert not [f for f in _found(narrow, "part_spacing") if f["basis"] == "pads"]
    assert narrow["results"]["part_spacing"]["objects"] == 5


def test_part_spacing_searches_on_past_not_fitted_neighbours():
    """R2 and R4 Not Fitted sit 2 mil from R1 and R3, which are 10 mil apart: the fitted
    pair sets the gap with or without the limit."""
    index = facts._dnp_board()
    lite = review_dfm(index, _profile(), ["part_spacing"], variant=_variant("R2", "R4"))
    assert lite["results"]["part_spacing"]["status"] == "not_checked"
    assert lite["board_minimums"]["min_part_gap"] == {
        "value_mil": 10.0, "basis": "pads", "reach_mil": pytest.approx(196.8504, abs=1e-4)}
    limited = review_dfm(index, _profile({"min_part_spacing_mil": 5}), ["part_spacing"],
                         variant=_variant("R2", "R4"))
    entry = limited["results"]["part_spacing"]
    assert (entry["status"], entry["worst_mil"]) == ("pass", 10.0)
    every = review_dfm(index, _profile(), ["part_spacing"])
    assert every["board_minimums"]["min_part_gap"]["value_mil"] == 2.0


def _unplaced_board():
    """M1, a Mechanical 3D model without pads (a fan), over R1 and R2; U1's body over
    C3's pads (C3 has no body)."""
    components = [facts._part(0, "R1"), facts._part(1, "R2"),
                  facts._part(2, "M1", kind=1), facts._part(3, "U1"), facts._part(4, "C3")]
    primitives = [facts._smd(0, 100, 100, net=A), facts._smd(0, 140, 100, net=B),
                  facts._smd(1, 300, 100, net=A), facts._smd(1, 340, 100, net=B)]
    primitives += [facts._smd(3, 960 + 20 * n, 500, 10.0, net=A) for n in range(5)]
    primitives += [facts._smd(4, 1020, 525, 10.0, net=A), facts._smd(4, 1040, 525, 10.0, net=B)]
    bodies = [facts._body(0, facts._rect(110, 90, 130, 110)),
              facts._body(1, facts._rect(310, 90, 330, 110)),
              facts._body(2, facts._rect(0, 0, 500, 200)),
              facts._body(3, facts._rect(930, 470, 1070, 540))]
    return facts._index(components, primitives, bodies)


def test_overlaps_with_parts_the_assembler_does_not_place_are_their_own_class():
    index = _unplaced_board()
    full = review_dfm(index, _profile({"min_part_spacing_mil": 4}), ["part_spacing"])
    entry = full["results"]["part_spacing"]
    # The fan model over R1 and R2 is info, not a miss: only U1 over C3's pads fails.
    assert (entry["status"], entry["overlapping_pairs"], entry["objects"]) == ("fail", 1, 1)
    model, covered = sorted(_found(full, "part_spacing"), key=lambda f: f["severity"])
    assert (model["severity"], model["count"], model["object"], model["not_placed"]) == (
        "info", 2, "M1", "a 3D model without pads")
    assert model["issue"].startswith("M1, which the assembler does not place (a 3D model "
                                     "without pads), overlaps the outlines of 2 other parts")
    assert [e["objects"] for e in model["examples"]] == ["M1 / R1", "M1 / R2"]
    assert (covered["severity"], covered["basis"], covered["count"]) == ("warning", "mixed", 1)
    assert covered["issue"].startswith("1 pair of placed parts where one part's 3D body lies "
                                       "over the other's pads")
    assert full["board_minimums"]["min_part_gap"]["overlapping_pairs"] == 1
    # Without the pads overlap and without a limit: the model alone is a run entry (pass).
    alone = review_dfm(index, _profile(), ["part_spacing"], variant=_variant("C3"))
    assert alone["results"]["part_spacing"] == {
        "status": "pass", "limit_mil": None, "worst_mil": 140.0, "objects": 0, "groups": 1,
        "severity": "info"}


def test_part_to_edge_classes_and_their_own_classes():
    index = facts._edge_board()
    full = review_dfm(index, _profile({"min_part_to_edge_mil": 50}), ["part_to_edge"])
    entry = full["results"]["part_to_edge"]
    assert (entry["status"], entry["limit_mil"], entry["worst_mil"]) == ("fail", 50.0, 2.0)
    found = {(f["severity"], f.get("basis") or f["kind"], f["count"], f.get("distance_mil"),
              f["examples"][0]["object"]) for f in _found(full, "part_to_edge")}
    assert found == {
        ("warning", "body", 1, 2.0, "P0"),  # its body nearer than its pads (7 mil)
        ("warning", "pads", 1, 5.0, "P6"),  # a cutout; P5's own cutout is no edge
        ("warning", "pads", 1, 15.0, "P1"),  # its body overhangs: the pads are measured
        ("info", "crossing", 1, None, "P2"),
        ("info", "overhang", 1, None, "P1"),
    }
    cutout = next(f for f in _found(full, "part_to_edge") if f.get("distance_mil") == 5.0)
    assert cutout["issue"].startswith("1 placed part whose pad copper is closer to a board "
                                      "cutout than the profile's minimum part to edge 50 mil")
    assert any("1 part(s) lie outside the board outline" in note for note in full["notes"])
    assert full["board_minimums"]["min_part_to_edge"]["value_mil"] == 2.0
    # No limit: its own classes ran, so a run entry (info) that judges no distance, with
    # the guidance and the nearest part.
    bare = review_dfm(index, _profile(), ["part_to_edge"])
    entry = bare["results"]["part_to_edge"]
    assert (entry["status"], entry["severity"], entry["limit_mil"], entry["worst_mil"],
            entry["objects"]) == ("pass", "info", None, 2.0, 0)
    assert entry["note"].startswith("Not judged: the profile sets no min_part_to_edge")
    assert "125 mil for top-side SMD" in entry["note"] and "body 2 mil from it" in entry["note"]
    assert {f["kind"] for f in _found(bare, "part_to_edge")} == {"crossing", "overhang"}
    # Nothing of its own (P1 and P2 Not Fitted): not checked, with the reason.
    quiet = review_dfm(index, _profile(), ["part_to_edge"], variant=_variant("P1", "P2"))
    assert quiet["results"]["part_to_edge"]["status"] == "not_checked"
    assert "body 2 mil from it" in quiet["results"]["part_to_edge"]["reason"]
    assert not _found(quiet, "part_to_edge")
    # Not Fitted parts are not placed.
    lite = review_dfm(index, _profile({"min_part_to_edge_mil": 50}), ["part_to_edge"],
                      variant=_variant("P0", "P6"))
    assert lite["results"]["part_to_edge"]["worst_mil"] == 15.0
    flat = review_dfm(facts._index([facts._part(0, "R1")], [facts._smd(0, 0, 0, net=A)]),
                      _profile({"min_part_to_edge_mil": 50}), ["part_to_edge"])
    assert flat["results"]["part_to_edge"] == {"status": "not_checked",
                                               "reason": "the board has no outline."}
    assert "min_part_to_edge" in flat["board_minimums"]["not_measured"]


def test_edge_connectors_are_their_own_class():
    finger = dict(width=20.0, height=60.0)
    primitives = [facts._smd(0, x, 35, net=A, **finger) for x in range(100, 400, 50)]
    index = facts._index([facts._part(0, "X1")], primitives, outline=facts.OUTLINE)
    full = review_dfm(index, _profile({"min_part_to_edge_mil": 50}), ["part_to_edge"])
    (finding,) = _found(full, "part_to_edge")
    assert (finding["kind"], finding["severity"], finding["count"]) == ("fingers", "info", 1)
    assert full["results"]["part_to_edge"]["status"] == "pass"


def _fiducial_board():
    """Three free-pad fiducials on top, U1 (0.4 mm pitch) 3000 mil from them, U2 (0.65 mm)
    near them and an SMD capacitor on the bottom."""
    one = 1.0 * MM
    components = [facts._part(0, "U1"), facts._part(1, "U2"),
                  facts._part(2, "C1", side="bottom")]
    primitives = [facts._smd(None, x, y, one, shape=facts.PAD_SHAPE_ROUND)
                  for x, y in ((100, 100), (900, 100), (100, 900))]
    primitives += [facts._smd(0, 3500 + 0.4 * MM * n, 500, 8.0, net=A) for n in range(4)]
    primitives += [facts._smd(1, 400 + 0.65 * MM * n, 500, 8.0, net=A) for n in range(4)]
    primitives += [facts._smd(2, 600, 600, 10.0, net=A, layer=32),
                   facts._smd(2, 640, 600, 10.0, net=B, layer=32)]
    return facts._index(components, primitives)


def test_fiducials_global_and_local():
    index = _fiducial_board()
    default = review_dfm(index, _default(), ["fiducials"])
    entry = default["results"]["fiducials"]
    assert (entry["status"], entry["value"], entry["limit"]) == (
        "advisory", {"top": 3, "bottom": 0}, None)
    bottom, local = _found(default, "fiducials")
    assert (bottom["side"], bottom["count"], bottom["typical_global_fiducials"]) == (
        "bottom", 3, 3)
    assert bottom["issue"].startswith("No global fiducials on the bottom side, which has 1 "
                                      "SMD placement; assemblers typically want 3")
    # Two counts, not one objects: missing global fiducials and parts short of local ones.
    assert (entry["missing_global"], entry["local_parts"]) == ({"top": 0, "bottom": 3}, 1)
    assert "objects" not in entry
    assert (local["severity"], local["count"], local["pitch_mil"]) == (
        "info", 1, pytest.approx(0.4 * MM, abs=1e-3))
    assert local["examples"][0]["object"] == "U1" and local["examples"][0][
        "fiducials_nearby"] == 0
    assert "within 1969 mil" in local["issue"] and "rule of thumb" in local["issue"]
    assert "(finest 15.748 mil) has fewer than 2 fiducials within 1969 mil on its side" in \
        local["issue"]
    mm = review_dfm(index, _default(), ["fiducials"], to_mm=True)
    assert "within 50 mm" in _found(mm, "fiducials")[1]["issue"]
    # With the assembler's limits: warnings; 0.4 mm is not at or below 0.3 mm.
    limits = {"min_global_fiducials": 2, "local_fiducial_pitch_mm": 0.3}
    strict = review_dfm(index, _profile(limits), ["fiducials"])
    (finding,) = _found(strict, "fiducials")
    assert (finding["severity"], finding["count"], finding["min_global_fiducials"]) == (
        "warning", 2, 2)
    assert strict["results"]["fiducials"]["status"] == "fail"
    assert strict["results"]["fiducials"]["local_fiducial_pitch_mil"] == pytest.approx(
        0.3 * MM, abs=1e-3)
    local = review_dfm(index, _profile({"local_fiducial_pitch_mm": 0.65}), ["fiducials"])
    local_finding = next(f for f in _found(local, "fiducials") if "pitch_mil" in f)
    assert local_finding["severity"] == "warning" and local_finding["count"] == 1  # U2 has 3


def test_two_local_fiducials_are_enough():
    """U1 (0.4 mm pitch) with exactly LOCAL_FIDUCIALS free-pad fiducials near it is not
    listed; with one it is."""
    one = 1.0 * MM

    def board(fiducials):
        primitives = [facts._smd(None, 100 + 200 * n, 100, one, shape=facts.PAD_SHAPE_ROUND)
                      for n in range(fiducials)]
        primitives += [facts._smd(0, 500 + 0.4 * MM * n, 500, 8.0, net=A) for n in range(4)]
        return facts._index([facts._part(0, "U1")], primitives)

    assert dfm.LOCAL_FIDUCIALS == 2
    two = review_dfm(board(2), _default(), ["fiducials"])
    assert two["results"]["fiducials"]["local_parts"] == 0
    assert not [f for f in _found(two, "fiducials") if "pitch_mil" in f]
    one_only = review_dfm(board(1), _default(), ["fiducials"])
    assert one_only["results"]["fiducials"]["local_parts"] == 1


def test_tombstoning_classes_and_the_variant():
    index = facts._tomb_board()
    full = review_dfm(index, _profile(), ["tombstoning"])
    entry = full["results"]["tombstoning"]
    assert (entry["status"], entry["small_passives"], entry["severity"]) == (
        "fail", 6, "warning")  # R8 (0805) is not small
    direct, track = _found(full, "tombstoning")
    assert (direct["size_code"], direct["cause"], direct["severity"]) == (
        "0402", "direct", "warning")
    assert direct["examples"][0] == {"at_mil": [130.0, 100.0], "object": "R1", "side": "top",
                                     "connections": ["direct", "relief"]}
    assert "thermal reliefs" in direct["issue"]
    assert (track["size_code"], track["cause"], track["severity"]) == ("0603", "track", "info")
    assert track["examples"][0]["tracks_mil"] == [20.0, 8.0]
    assert "not a certainty" in track["issue"]
    # R1 Not Fitted: only the track imbalance is left, which is advice.
    lite = review_dfm(index, _profile(), ["tombstoning"], variant=_variant("R1"))
    assert lite["results"]["tombstoning"]["status"] == "advisory"
    assert lite["results"]["tombstoning"]["small_passives"] == 5


def _pitch_board():
    """U1 0.4 mm, U2 0.495 mm (within 2% of 0.5 mm), U3 0.65 mm pitch."""
    components = [facts._part(n, f"U{n + 1}") for n in range(3)]
    primitives = [facts._smd(part, 500 * part + pitch * MM * n, 0, 8.0, net=A)
                  for part, pitch in enumerate((0.4, 0.495, 0.65)) for n in range(4)]
    return facts._index(components, primitives)


def test_fine_pitch():
    index = _pitch_board()
    full = review_dfm(index, _profile({"min_pitch_mm": 0.5}), ["fine_pitch"])
    entry = full["results"]["fine_pitch"]
    assert (entry["status"], entry["severity"]) == ("fail", "error")
    assert entry["worst_mil"] == pytest.approx(0.4 * MM, abs=1e-3)
    miss, marginal = _found(full, "fine_pitch")
    assert (miss["count"], miss["examples"][0]["object"], miss.get("marginal")) == (1, "U1", None)
    assert marginal["marginal"] and marginal["severity"] == "warning"
    assert miss["issue"].startswith("1 fitted part with a pad pitch of 15.748 mil, below "
                                    "the profile's minimum pitch 19.685 mil.")
    # Altimade publishes no pitch: parts at 0.5 mm or finer are advice.
    default = review_dfm(index, _default(), ["fine_pitch"])
    (advice,) = _found(default, "fine_pitch")
    assert default["results"]["fine_pitch"]["status"] == "advisory"
    assert advice["count"] == 2 and "Altimade checks component pitch" in advice["issue"]
    coarse = review_dfm(index, _profile(), ["fine_pitch"], variant=_variant("U1", "U2"))
    assert coarse["results"]["fine_pitch"] == {
        "status": "not_checked",
        "reason": "the profile sets no min_pitch; the finest pitch is 25.591 mil."}
    assert coarse["board_minimums"]["min_pitch_mil"] == pytest.approx(0.65 * MM, abs=1e-3)


def test_assembly_minimums_notes_and_the_cache_follow_the_variant():
    index = _parts_board(bottom_smd=True)
    profile = _default()
    narrowed = review_dfm(index, profile, ["layer_count"])
    minimums = narrowed["board_minimums"]
    assert minimums["placements"] == {"top_smd": 3, "top_tht": 0, "bottom_smd": 1,
                                      "bottom_tht": 1}
    assert minimums["fiducials"] == {"top": 0, "bottom": 0}
    assert minimums["min_pitch_mil"] is None
    assert minimums["not_measured"][-2:] == ["min_part_gap", "min_part_to_edge"]
    assert not any("Assembly checks" in note for note in narrowed["notes"])
    lite = review_dfm(index, profile, ASSEMBLY, variant=_variant("R1"))
    note = next(note for note in lite["notes"] if "Assembly checks" in note)
    assert "Fiducials are counted whatever the variant: their copper is on the bare board." \
        in note
    assert lite is review_dfm(index, profile, ASSEMBLY, variant=_variant("R1"))
    assert lite is not review_dfm(index, profile, ASSEMBLY, variant=_variant("R2"))
    assert lite is not review_dfm(index, profile, ASSEMBLY, variant=_variant("R1", unique=2))
    free = review_dfm(index, profile, ["part_counts"], variant=_variant(netlist_free=True))
    note = next(note for note in free["notes"] if "Assembly checks" in note)
    assert "No netlist is loaded" in note
    whole = review_dfm(index, profile)
    assert list(whole["results"]) == list(VALID_CHECKS)


# ---------- Altium's examples ----------


def _example(name):
    path = next(EXAMPLES.rglob(name), None) if EXAMPLES.is_dir() else None
    if path is None:
        pytest.skip("Altium example boards not installed")
    return PcbIndex(parse_pcb_doc(str(path)), None), path


# Default profile (Altimade, chosen_by default), every part fitted: every check's status
# per board.
SNAPSHOT = {
    "Bluetooth_Sentinel.PcbDoc": {
        "trace_width": "pass", "spacing": "fail", "hole_size": "pass",
        "annular_ring": "marginal", "aspect_ratio": "pass", "hole_to_hole": "not_checked",
        "copper_to_edge": "fail", "layer_count": "pass", "copper_weight": "pass",
        "board_size": "pass", "drill_count": "not_checked", "rules_vs_profile": "fail",
        "part_counts": "pass", "assembly_sides": "advisory", "part_spacing": "not_checked",
        "part_to_edge": "pass", "fiducials": "advisory",
        "tombstoning": "advisory", "fine_pitch": "advisory",
    },
    "MiniPC.PcbDoc": {
        "trace_width": "pass", "spacing": "fail", "hole_size": "pass", "annular_ring": "fail",
        "aspect_ratio": "pass", "hole_to_hole": "not_checked", "copper_to_edge": "pass",
        "layer_count": "advisory", "copper_weight": "advisory", "board_size": "pass",
        "drill_count": "not_checked", "rules_vs_profile": "fail",
        "part_counts": "advisory", "assembly_sides": "advisory", "part_spacing": "advisory",
        "part_to_edge": "pass", "fiducials": "advisory",
        "tombstoning": "fail", "fine_pitch": "advisory",
    },
    "PC4-SODIMM_V200_RC_A1_20141015.PcbDoc": {
        "trace_width": "pass", "spacing": "fail", "hole_size": "pass",
        "annular_ring": "marginal", "aspect_ratio": "pass", "hole_to_hole": "not_checked",
        "copper_to_edge": "fail", "layer_count": "pass", "copper_weight": "advisory",
        "board_size": "pass", "drill_count": "not_checked", "rules_vs_profile": "fail",
        "part_counts": "pass", "assembly_sides": "advisory", "part_spacing": "not_checked",
        "part_to_edge": "not_checked", "fiducials": "advisory",
        "tombstoning": "pass", "fine_pitch": "advisory",
    },
    "WiFi.PcbDoc": {
        "trace_width": "pass", "spacing": "fail", "hole_size": "pass", "annular_ring": "pass",
        "aspect_ratio": "pass", "hole_to_hole": "not_checked", "copper_to_edge": "fail",
        "layer_count": "pass", "copper_weight": "advisory", "board_size": "pass",
        "drill_count": "not_checked", "rules_vs_profile": "fail",
        "part_counts": "pass", "assembly_sides": "advisory", "part_spacing": "not_checked",
        "part_to_edge": "not_checked", "fiducials": "advisory",
        "tombstoning": "pass", "fine_pitch": "advisory",
    },
    "WiFi_panel.PcbDoc": {
        "trace_width": "pass", "spacing": "pass", "hole_size": "pass", "annular_ring": "fail",
        "aspect_ratio": "pass", "hole_to_hole": "not_checked", "copper_to_edge": "pass",
        "layer_count": "pass", "copper_weight": "advisory", "board_size": "pass",
        "drill_count": "not_checked", "rules_vs_profile": "pass",
        "part_counts": "pass", "assembly_sides": "pass", "part_spacing": "not_checked",
        "part_to_edge": "not_checked", "fiducials": "pass",
        "tombstoning": "pass", "fine_pitch": "not_checked",
    },
    "SL1 Xilinx Spartan-IIE PQ208 Rev1.02.PcbDoc": {
        "trace_width": "pass", "spacing": "pass", "hole_size": "pass",
        "annular_ring": "marginal", "aspect_ratio": "pass", "hole_to_hole": "not_checked",
        "copper_to_edge": "pass", "layer_count": "pass", "copper_weight": "pass",
        "board_size": "pass", "drill_count": "not_checked", "rules_vs_profile": "pass",
        "part_counts": "pass", "assembly_sides": "advisory", "part_spacing": "advisory",
        "part_to_edge": "not_checked", "fiducials": "advisory",
        "tombstoning": "pass", "fine_pitch": "advisory",
    },
}


@pytest.mark.parametrize("name", sorted(SNAPSHOT))
def test_examples_status_snapshot(name):
    index, _ = _example(name)
    full = review_dfm(index, _default())
    assert {check: entry["status"] for check, entry in full["results"].items()} == SNAPSHOT[name]
    # A check fails only with an error or warning miss; advisory is info only.
    for entry in full["results"].values():
        if entry["status"] in ("fail", "marginal"):
            assert entry["severity"] in ("error", "warning")
        elif entry["status"] == "advisory":
            assert entry["severity"] == "info"
    # Spacing classes are families, not gap buckets: a handful per board.
    assert len(_found(full, "spacing")) <= 8
    # Whatever the board, a page that fits is found within the shrink steps.
    for to_mm in (True, False):
        mm = review_dfm(index, _default(), to_mm=to_mm)
        sizes = [len(_rendered(page_dfm(mm, 0, limit, examples), to_mm))
                 for limit, examples in SHRINK_STEPS]
        assert sizes[1] < 60000


def test_sodimm_assembly():
    """Free-pad fiducials (3 per side), its card edge (a no-BOM part: not placed) and
    pads relief-connected only: no tombstoning."""
    index, _ = _example("PC4-SODIMM_V200_RC_A1_20141015.PcbDoc")
    full = review_dfm(index, _profile(ASSEMBLY_STRICT), ASSEMBLY)
    assert full["results"]["fiducials"]["value"] == {"top": 3, "bottom": 3}
    assert full["results"]["tombstoning"] == {"status": "pass", "small_passives": 182,
                                              "objects": 0, "groups": 0}
    assert full["board_minimums"]["placements"] == {"top_smd": 98, "top_tht": 0,
                                                    "bottom_smd": 100, "bottom_tht": 0}
    assert not [f for f in _found(full, "part_to_edge") if f.get("kind") == "fingers"]
    examples = [e["object"] for f in _found(full, "part_to_edge") for e in f["examples"]]
    assert "X1" not in examples


@pytest.fixture(scope="module")
def minipc():
    return _example("MiniPC.PcbDoc")


def test_minipc_review_runtime_and_numbers(minipc):
    index, _ = minipc
    start = time.perf_counter()
    full = review_dfm(index, _default())
    elapsed = time.perf_counter() - start
    # About 2 s on a 2026 laptop after the index; the budget is 3 s, with room for a slow
    # test machine.
    assert elapsed < 6.0
    spacing = full["results"]["spacing"]
    # Places: 16,541 copper object pairs meet at 13,937 places; 64 more are pour copper
    # 0.1 mm from non-plated holes (Altium's pour clearance there).
    assert spacing["objects"] == 14001 and spacing["board_rule"]["value_mil"] == pytest.approx(
        3.5433, abs=1e-4)
    found = _found(full, "spacing")
    assert sum(f.get("object_pairs", f["count"]) for f in found) == 16541 + 64
    assert len(found) == 6
    walls = next(f for f in found if f["kinds"] == "hole-pour")
    assert (walls["count"], walls["gap_mil"]) == (64, pytest.approx(3.937, abs=1e-3))
    assert spacing["hole_to_copper"]["worst_mil"] == pytest.approx(3.937, abs=1e-3)
    ring = full["results"]["annular_ring"]
    assert ring["objects"] == 1785 + 6  # 0.1 mm via rings (marginal) and 6 plated bare holes
    assert ring["worst_mil"]["pth"] == 0.0
    marginal = next(f for f in _found(full, "annular_ring") if f.get("marginal"))
    assert marginal["count"] == 1785
    assert full["board_minimums"]["worst_aspect_ratio"] == 10.04  # 10.04:1 passes 10 at 0.1
    assert full["board"]["copper_layers"] == 16
    pad_gaps = next(f for f in found if f.get("footprint"))
    assert pad_gaps["count"] == 226
    # Nothing within Altimade's 10 mil of the edge: the nearest copper of the 1 mm search.
    assert full["results"]["copper_to_edge"]["worst_mil"] == pytest.approx(11.811, abs=1e-3)
    # Assembly, every part fitted: Altimade's 600 placements is advice while it is only
    # the default; the power passives' direct pads (the map's validation) are a warning.
    minimums = full["board_minimums"]
    # MH1-MH4 (Mechanical parts, a plated hole each) are mounting holes, not placements.
    assert minimums["placements"] == {"top_smd": 346, "top_tht": 17, "bottom_smd": 677,
                                      "bottom_tht": 1}
    assert minimums["unique_parts"] == {"value": 130, "approximate": True}
    assert minimums["fiducials"] == {"top": 0, "bottom": 0}
    assert minimums["min_pitch_mil"] == pytest.approx(0.4 * MM, abs=1e-3)
    # Pad copper reaches past the bodies: two capacitors' pads 0.08 mm apart.
    assert minimums["min_part_gap"]["value_mil"] == pytest.approx(3.15, abs=1e-3)
    assert minimums["min_part_gap"]["basis"] == "pads"
    assert minimums["min_part_gap"]["overlapping_pairs"] == 13
    assert minimums["min_part_to_edge"] == {"value_mil": pytest.approx(29.134, abs=1e-3),
                                            "basis": "pads", "reach_mil": pytest.approx(
                                                393.7008, abs=1e-3)}
    counts = full["results"]["part_counts"]
    assert counts["value"] == {"placements": 1041, "unique_parts": 130}
    assert counts["severity"] == "info"
    tombs = _found(full, "tombstoning")
    # Track imbalance only where neither pad touches area copper.
    assert sorted((f["size_code"], f["cause"], f["count"], f["severity"]) for f in tombs) == [
        ("0402", "direct", 1, "warning"), ("0402", "track", 3, "info"),
        ("0603", "direct", 6, "warning")]
    assert full["results"]["tombstoning"]["small_passives"] == 825
    # 13 pairs of placed parts' bodies overlap (advisory); M1, a fan model without pads,
    # lies over 12 more parts: its own class, not a miss.
    overlap, model = _found(full, "part_spacing")
    assert (overlap["count"], overlap["basis"], model["count"], model["object"]) == (
        13, "body", 12, "M1")
    assert full["results"]["part_spacing"]["objects"] == 13
    sides = next(f for f in _found(full, "assembly_sides") if "smd_parts" in f)
    assert sides["count"] == 677 and sides["smd_parts"] == {"top": 347, "bottom": 677}
    assert full["results"]["fiducials"]["missing_global"] == {"top": 3, "bottom": 3}
    assert full["results"]["fiducials"]["local_parts"] == 20
    (fine,) = _found(full, "fine_pitch")
    assert fine["count"] == 20 and fine["severity"] == "info"
    assert "publishes no number" in fine["issue"]


def test_minipc_drill_table_matches_draftsman(minipc):
    index, path = minipc
    drawing = path.parent / "MiniPC_Drill.PCBDwf"
    if not drawing.is_file():
        pytest.skip("MiniPC_Drill.PCBDwf not installed")
    expected = {}
    for row in parse_pcb_dwf(str(drawing)).drill_table:
        if row.get("backdrill"):
            continue  # back drills are not in the DFM drill table
        key = (round(row["size_mm"], 3), row["plated"], row["type"],
               round(row.get("slot_length_mm") or 0.0, 3))
        expected[key] = expected.get(key, 0) + row["count"]
    found = {}
    for row in review_dfm(index, _default(), ["drill_count"])["drills"]:
        key = (round(row["hole_mil"] / MM, 3), row["plated"],
               "Via" if row["kind"] == "via" else "Pad",
               round((row.get("slot_length_mil") or 0.0) / MM, 3))
        found[key] = found.get(key, 0) + row["count"]
    assert found == expected
    assert sum(row["count"] for row in review_dfm(index, _default(), ["drill_count"])["drills"]
               if row["kind"] == "bare_hole") == 16


def test_minipc_worst_case_page_fits(minipc):
    index, _ = minipc
    strict = {
        "min_trace_mm": 1.0, "min_spacing_mm": 0.3, "min_drill_mm": 5.0, "max_drill_mm": 0.15,
        "min_annular_ring_mm": 0.5, "min_copper_to_edge_mm": 5.0, "max_aspect_ratio": 1,
        "min_hole_to_hole_mm": 0.5, "min_pad_hole_to_hole_mm": 1.0, "layer_counts": [2],
        "min_board_size_mm": [500, 500], "max_drill_sizes": 1, "copper_oz": 0.5,
        "min_route_size_mm": 5.0, **ASSEMBLY_STRICT,
    }
    start = time.perf_counter()
    full = review_dfm(index, _profile(strict), to_mm=True)
    assert time.perf_counter() - start < 9.0  # about 3.5 s, part spacing at its 5 mm bound
    # Every check fails but the two that are advice whatever the limits.
    assert {check: entry["status"] for check, entry in full["results"].items()
            if entry["status"] != "fail"} == {"drill_count": "advisory",
                                              "assembly_sides": "advisory"}
    assert len(_rendered(page_dfm(full, 0, *SHRINK_STEPS[1]), True)) < 60000


def test_removed_hole_limits_are_listed_as_not_checked():
    # null stops min_hole_to_copper / min_hole_to_edge falling back: say what was skipped.
    index = _index([_track(A, 1, 100, 100, 300, 100)])
    limits = {"min_spacing_mil": 5, "min_hole_to_copper_mil": None,
              "min_copper_to_edge_mil": 10, "min_hole_to_edge_mil": None}
    result = review_dfm(index, _profile(limits), ["spacing", "copper_to_edge"])
    assert "min_hole_to_copper" in result["not_checked"]["hole_walls"]
    assert "min_hole_to_edge" in result["not_checked"]["holes_to_edge"]
    kept = review_dfm(index, _profile({"min_spacing_mil": 5, "min_copper_to_edge_mil": 10}),
                      ["spacing", "copper_to_edge"])
    assert "hole_walls" not in kept["not_checked"] and "holes_to_edge" not in kept["not_checked"]
