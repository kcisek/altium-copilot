"""Copper geometry of check_dfm (services/dfm_geometry): copper spacing, copper to board
edge and hole to hole on synthetic boards, and on Altium's examples (skipped when absent)."""

import gc
import math
import weakref
from pathlib import Path

import pytest

from server.parsers.pcb_doc import (
    PAD_INNER_LAYERS,
    PAD_SHAPE_OCTAGONAL,
    PAD_SHAPE_RECT,
    BareHole,
    CopperArea,
    PadCopper,
    PcbComponent,
    PcbData,
    PolygonPour,
    Primitive,
    ShapeVertex,
    ViaStack,
    parse_pcb_doc,
)
from server.services import dfm_geometry as geometry
from server.services.pcb_index import PcbIndex
from tests.test_pcb_health import _region, _square
from tests.test_pcb_index import _board, _pad, _track

GND, A, B, C = range(4)
NETS = ["GND", "A", "B", "C"]
LAYERS = {"top layer": 1, "l2-gnd": 2, "l3-sig": 3, "bottom layer": 32, "top": 1, "bottom": 32}
EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")
# A 1000 x 1000 mil board whose top right corner is rounded (radius 100).
OUTLINE = [
    ShapeVertex(0, 0), ShapeVertex(1000, 0), ShapeVertex(1000, 900, (900, 900, 100, 0, 90)),
    ShapeVertex(900, 1000), ShapeVertex(0, 1000),
]


def _part(index, kind=0):
    return PcbComponent(
        index=index, refdes=f"U{index}", source_uid_tail=None, channel_path=None, x=0.0,
        y=0.0, rotation=0.0, side="top", designator=f"U{index}", kind=kind,
    )


def _via(net, x, y, width=20.0, hole=8.0, layer=1, layer_to=32, stack=None):
    return Primitive(kind="via", layer=layer, layer_to=layer_to, net=net, component=None,
                     x1=x, y1=y, x2=x, y2=y, width=width, hole=hole, via=stack)


def _th_pad(net, x, y, width=20.0, hole=10.0, component=None, plated=True, **fields):
    return Primitive(kind="pad", layer=74, net=net, component=component, x1=x, y1=y, x2=x,
                     y2=y, width=width, height=fields.pop("height", width),
                     shape=fields.pop("shape", 1), hole=hole, pad_name="1", plated=plated,
                     **fields)


def _area(layer, outline, net=None, holes=(), pour_id=None, kind="region", component=None):
    return CopperArea(kind=kind, layer=layer, net=net, component=component, outline=outline,
                      holes=[list(hole) for hole in holes], pour_id=pour_id)


def _index(primitives=(), *, areas=(), pours=(), components=(), outline=None, regions=b"",
           bare_holes=()):
    board = _board()
    if outline is not None:
        board.outline_shape = list(outline)
    pcb = PcbData(
        board=board, nets=list(NETS), components=list(components),
        primitives=list(primitives), pours=list(pours), layer_name_to_v6=dict(LAYERS),
        raw={"ShapeBasedRegions6/Data": regions} if regions else {}, areas=list(areas),
        bare_holes=list(bare_holes),
    )
    return PcbIndex(pcb, None)


def _kinds(record):
    return sorted((record.first.kind, record.second.kind))


# ---------- spacing ----------


def test_gap_between_two_tracks_and_the_reach():
    index = _index([
        _track(A, 1, 0, 0, 100, 0),
        _track(B, 1, 0, 9, 100, 9),  # 3 mil from the first, edge to edge
        _track(A, 1, 0, -8, 100, -8),  # the same net
        _track(C, 3, 0, 9, 100, 9),  # another layer
    ])
    result = geometry.spacing(index, 5.0)
    (record,) = result.records
    assert record.gap_mil == pytest.approx(3.0)
    assert (record.layer, record.layers, _kinds(record)) == (1, (1,), ["track", "track"])
    assert {record.first.owner, record.second.owner} == {("prim", 1), ("prim", 2)}
    assert {record.first.net, record.second.net} == {A, B}
    assert record.at[1] == pytest.approx(4.5) and 0 <= record.at[0] <= 100
    assert not record.marks and not result.stopped
    # A pair is reported below reach - CLEARANCE_TOLERANCE_MIL (0.01 mil) only.
    assert geometry.spacing(index, 3.005).records == []
    assert len(geometry.spacing(index, 3.02).records) == 1


def test_arc_chords_and_hatched_pour_tracks_are_one_object_each():
    chord = dict(kind="arc", layer=1, net=B, component=None, width=6.0, source_id=7)
    index = _index(
        [
            _track(A, 1, 0, 0, 200, 0),
            Primitive(x1=0, y1=9, x2=50, y2=9, **chord),
            Primitive(x1=50, y1=9, x2=100, y2=9, **chord),
            Primitive(kind="track", layer=1, net=GND, component=None, x1=100, y1=-9, x2=150,
                      y2=-9, width=6.0, poured_by=0),
            Primitive(kind="track", layer=1, net=GND, component=None, x1=150, y1=-9, x2=200,
                      y2=-9, width=6.0, poured_by=0),
            # The pour's outline row is no copper.
            Primitive(kind="track", layer=1, net=GND, component=None, x1=0, y1=5, x2=200,
                      y2=5, width=0.0, pour_id=0),
        ],
        pours=[PolygonPour(0, GND, 1, "Hatched", _square(0, -20, 200))],
    )
    records = geometry.spacing(index, 5.0).records
    # Owners of a record are in order: ('arc', ..) < ('pour', ..) < ('prim', ..).
    assert [(record.first.owner, record.second.owner) for record in records] == [
        (("arc", 7), ("prim", 1)), (("pour", 0), ("prim", 1)),
    ]
    pour = records[1]
    assert (pour.first.kind, pour.first.pour, pour.gap_mil) == ("pour", 0, pytest.approx(3.0))
    assert pour.first.is_area and not pour.second.is_area


def test_no_net_pairs_are_counted_not_reported():
    result = geometry.spacing(_index([
        _track(None, 1, 0, 0, 100, 0), _track(None, 1, 0, 8, 100, 8),
    ]), 5.0)
    assert result.records == [] and result.no_net_pairs == 1


def test_vias_are_measured_per_layer():
    """A via with no pad on a layer has only its barrel there: a pour void around the
    barrel is a 4 mil gap, not copper of two nets overlapping."""
    removed = ViaStack(False, False, False, False, removed_pads=frozenset({2}))
    diameters = tuple(0.0 if layer == 3 else 20.0 for layer in range(1, 33))
    full_stack = ViaStack(False, False, False, False, mode=2, layer_diameters=diameters)
    index = _index(
        [_via(A, 0, 0, stack=removed), _via(A, 300, 0, stack=full_stack)],
        areas=[
            _area(2, _square(-100, -100, 200), GND, holes=[_square(-8, -8, 16)]),
            _area(3, _square(200, -100, 200), GND, holes=[_square(292, -8, 16)]),
        ],
    )
    records = geometry.spacing(index, 5.0).records
    assert [(record.layer, round(record.gap_mil, 6)) for record in records] == [(2, 4.0), (3, 4.0)]
    assert all(_kinds(record) == ["region", "via"] for record in records)
    assert not any("touching" in record.marks for record in records)


def test_pad_holes_without_copper():
    index = _index([
        # Non-plated, copper no larger than the hole: no copper at all.
        _th_pad(None, 0, 0, width=30.0, hole=30.0, plated=False),
        _track(A, 1, -50, 17, 50, 17, width=2.0),
        # Plated, inner copper smaller than the hole: the barrel is the copper there.
        _th_pad(B, 200, 0, width=40.0, hole=24.0,
                layer_copper={PAD_INNER_LAYERS: PadCopper(20.0, 20.0, 1)}),
        _track(A, 2, 150, 14, 250, 14, width=2.0),
    ])
    (record,) = geometry.spacing(index, 5.0).records
    assert (record.layer, record.gap_mil) == (2, pytest.approx(1.0))
    assert _kinds(record) == ["pad", "track"]


def test_net_tie_pairs_are_skipped_and_counted():
    """NT1 (ComponentKind 4) joins nets A and B with a no-net bar: the bar against its
    pads and against routing of A or B is no short; other nets still count."""
    index = _index(
        [
            _pad(A, 0, 0, 0, name="1"),
            _pad(B, 0, 40, 0, name="2"),
            _track(None, 1, 0, 0, 40, 0, component=0),  # the tie's bar
            _track(A, 1, -50, 0, 0, 0),  # routing of A, touching the bar
            _track(C, 1, 10, 7, 30, 7, width=2.0),  # 3 mil from the bar
        ],
        components=[_part(0, kind=4)],
    )
    result = geometry.spacing(index, 5.0)
    (record,) = result.records
    assert {record.first.net, record.second.net} == {None, C}
    assert record.gap_mil == pytest.approx(3.0)
    assert result.net_tie_pairs == {0: 3}


def test_marks_footprint_touching_and_stale_pours():
    index = _index(
        [
            _pad(A, 1, 0, 200, name="1"),
            _pad(B, 1, 14, 200, name="2"),  # 4 mil from its neighbour in the footprint
            _pad(A, 2, 100, 200),
            _track(None, 1, 100, 200, 120, 200, width=4.0, component=2),  # on its own pad
            _track(A, 1, 0, 300, 50, 300),
            _track(B, 1, 25, 300, 75, 300),  # over the other
            _pad(A, None, 230, 30),  # inside the copper of a modified pour
            _pad(B, None, 450, 50),  # in a void of a valid pour, 4 mil from its edge
        ],
        components=[_part(0), _part(1), _part(2)],
        pours=[
            PolygonPour(0, GND, 1, "Solid", _square(200, 0, 60), copper_invalid=True),
            PolygonPour(1, GND, 1, "Solid", _square(400, 0, 100)),
        ],
        areas=[
            _area(1, _square(200, 0, 60), GND, pour_id=0),
            _area(1, _square(400, 0, 100), GND, holes=[_square(441, 41, 18)], pour_id=1),
        ],
    )
    result = geometry.spacing(index, 5.0)
    by_marks = sorted(
        (sorted(record.marks), round(record.gap_mil, 6)) for record in result.records
    )
    assert by_marks == [
        ([], 4.0),
        (["footprint_copper", "touching"], 0.0),
        (["same_footprint"], 4.0),
        (["stale_pour", "touching"], 0.0),
        (["touching"], 0.0),
    ]
    stale = next(record for record in result.records if "stale_pour" in record.marks)
    assert stale.stale_pours == (0,) and stale.first.owner == ("area", 0)
    assert stale.at == (230.0, 30.0)
    # Touching, stale and footprint copper do not set the board's minimum gap.
    assert result.minimum().gap_mil == pytest.approx(4.0)


def test_area_copper():
    index = _index(
        [
            _track(C, 1, 300, 150, 320, 150, width=4.0),  # inside the region of B
            Primitive(kind="pad", layer=1, net=B, component=None, x1=600, y1=50, x2=600,
                      y2=50, width=100.0, height=100.0, shape=PAD_SHAPE_RECT, pad_name="1"),
        ],
        areas=[
            _area(1, _square(0, 0, 50), A),
            _area(1, _square(52, 0, 50), B),  # 2 mil from the first
            _area(1, _square(200, 0, 200), B),
            _area(1, _square(250, 50, 10), A, kind="fill"),  # inside the region of B
            _area(1, _square(595, 45, 10), A, kind="fill"),  # inside the pad of B
        ],
    )
    records = geometry.spacing(index, 5.0).records
    found = sorted((_kinds(record), round(record.gap_mil, 6)) for record in records)
    assert found == [
        (["fill", "pad"], 0.0),
        (["fill", "region"], 0.0),
        (["region", "region"], 2.0),
        (["region", "track"], 0.0),
    ]
    assert all(record.first.row is None or record.first.area is None for record in records)


def test_octagonal_pads_are_octagons():
    """Shape 3 is the pad's rectangle with each corner cut by a quarter of its short side
    (as KiCad's Altium importer reads it), rotated with the pad, per-layer copper too."""
    def octagon(net, x, y, rotation=0.0):
        return Primitive(kind="pad", layer=1, net=net, component=None, x1=x, y1=y, x2=x,
                         y2=y, width=60.0, height=60.0, shape=PAD_SHAPE_OCTAGONAL,
                         rotation=rotation, pad_name="1")

    index = _index([
        octagon(A, 0, 0), _via(B, 40, 40, width=10.0, hole=5.0),
        # Turned 45 degrees a corner cut faces +x: 45 / sqrt(2) from the centre.
        octagon(A, 1000, 0, rotation=45.0), _via(B, 1060, 0, width=10.0, hole=5.0),
        # Round on the outer layers, an octagon on the inner ones.
        _th_pad(A, 2000, 0, width=30.0, hole=10.0,
                layer_copper={PAD_INNER_LAYERS: PadCopper(60.0, 60.0, PAD_SHAPE_OCTAGONAL)}),
        _via(B, 2040, 40, width=10.0, hole=5.0),
    ])
    found = sorted((record.first.row, round(record.gap_mil, 4), record.layers)
                   for record in geometry.spacing(index, 25.0).records)
    # The cut corner of the first is the line x + y = 45: (80 - 45) / sqrt(2) - 5 away.
    cut = round(35.0 / math.sqrt(2.0) - 5.0, 4)
    assert found == [
        (1, cut, (1,)),
        (3, round(60.0 - 45.0 / math.sqrt(2.0) - 5.0, 4), (1,)),
        (5, cut, (2, 3)),
    ]


def test_areas_inside_a_valid_pour_and_objects_behind_the_flag(monkeypatch):
    """A valid pour clears other nets' copper when poured: a region of another net lying
    in its copper is still a short (always tested); an object lying in it is tested only
    with VALID_POUR_ITEMS (it costs more than the rest), and the result says which."""
    def board():
        return _index(
            [_pad(B, None, 500, 500, width=40.0), _via(C, 600, 600)],
            pours=[PolygonPour(0, GND, 1, "Solid", _square(0, 0, 1000))],
            areas=[_area(1, _square(0, 0, 1000), GND, pour_id=0),
                   _area(1, _square(200, 200, 50), B)],
        )

    result = geometry.spacing(board(), 5.0)
    assert [(_kinds(record), record.gap_mil, sorted(record.marks))
            for record in result.records] == [(["pour", "region"], 0.0, ["touching"])]
    assert result.valid_pour_items_checked is False
    monkeypatch.setattr(geometry, "VALID_POUR_ITEMS", True)
    result = geometry.spacing(board(), 5.0)
    assert sorted((_kinds(record), record.gap_mil) for record in result.records) == [
        (["pad", "pour"], 0.0), (["pour", "region"], 0.0), (["pour", "via"], 0.0),
    ]
    assert result.valid_pour_items_checked is True


def test_large_areas_answer_points_from_the_chunk_grid():
    """A large area (more ring points than _BANDS_MAX_POINTS) answers a few points by
    counting the ring edges of its chunks in the grid row, toward the nearer side of its
    box: the answers of its band index, without building one."""
    count = 2400
    ring = [(500 + 400 * math.cos(2 * math.pi * k / count),
             500 + 400 * math.sin(2 * math.pi * k / count)) for k in range(count)]
    holes = [_square(450, 450, 100), [(200, 300), (300, 250), (250, 400)]]
    index = _index(pours=[PolygonPour(0, GND, 1, "Solid", ring)],
                   areas=[_area(1, ring, GND, holes=holes, pour_id=0)])
    model = geometry.copper_model(index)
    (area,) = model.areas
    assert area.points > geometry._BANDS_MAX_POINTS
    points = [(x, y) for x in range(60, 960, 37) for y in range(60, 960, 41)]
    answers = [model.area_contains(area, x, y) for x, y in points]
    assert area._bands is None
    assert answers == [area.contains(x, y) for x, y in points]
    assert 0 < sum(answers) < len(points)


def test_area_spacing_beyond_the_default_grid_grow():
    """At 0.5 mm (19.685 mil) area pairs meet only through the grown area grid (A and B
    are in neighbouring cells), and a track only through its grown grid query."""
    reach = 0.5 / 0.0254
    assert geometry._grid_grow(reach) > geometry.AREA_GROW_MIL
    index = _index(
        [_track(C, 3, 71, 0, 71, 60, width=4.0)],  # copper up to x = 73
        areas=[
            _area(1, [(0, 0), (70, 0), (70, 60), (0, 60)], A),
            _area(1, [(85, 0), (145, 0), (145, 60), (85, 60)], B),  # 15 mil from A
            _area(3, [(90, 0), (150, 0), (150, 60), (90, 60)], B),  # 17 mil from the track
        ],
    )
    found = sorted((_kinds(record), round(record.gap_mil, 6), record.layer)
                   for record in geometry.spacing(index, reach).records)
    assert found == [(["region", "region"], 15.0, 1), (["region", "track"], 17.0, 3)]


def test_area_containment_sweep_is_bounded():
    """Areas inside areas are found by a sweep over their boxes, each comparison a work
    unit: boxes that all overlap (none inside another) stop at the cap instead of an
    all-pairs loop, and a long row of small areas costs a few units each."""
    overlapping = [_area(1, _square(n, -n, 500), A) for n in range(400)]
    capped = geometry.spacing(_index(areas=overlapping), 5.0, cap=10_000)
    assert capped.stopped
    row = [_area(1, _square(30.0 * n, 0, 10), A if n % 2 else B) for n in range(1000)]
    row.append(_area(1, _square(30000, 0, 200), A))
    row.append(_area(1, _square(30050, 50, 20), B))  # inside the last one
    result = geometry.spacing(_index(areas=row), 5.0)
    assert not result.stopped and result.distance_calls < 20_000
    assert [(_kinds(record), record.gap_mil) for record in result.records] == [
        (["region", "region"], 0.0),
    ]


def _cache_board():
    """Pairs at several gaps: a via whose pad is smaller on the inner layers next to a
    through-hole pad (3 mil outside, 8 mil inside), tracks 6 and 12 mil apart, no-net
    pairs 4 and 9 mil apart, and a net tie's bar 0 and 6 mil from its nets."""
    diameters = tuple(20.0 if layer in (1, 32) else 10.0 for layer in range(1, 33))
    stack = ViaStack(False, False, False, False, mode=2, layer_diameters=diameters)
    return _index(
        [
            _via(A, 0, 0, stack=stack), _th_pad(B, 23, 0, width=20.0, hole=10.0),
            _track(A, 1, 0, 200, 100, 200), _track(B, 1, 0, 212, 100, 212),
            _track(A, 3, 0, 300, 100, 300), _track(C, 3, 0, 318, 100, 318),
            _track(None, 1, 0, 400, 100, 400), _track(None, 1, 0, 410, 100, 410),
            _track(None, 2, 0, 500, 100, 500), _track(None, 2, 0, 515, 100, 515),
            _pad(A, 0, 1000, 0, name="1"), _pad(B, 0, 1040, 0, name="2"),
            _track(None, 1, 1000, 0, 1040, 0, component=0),  # the tie's bar
            _track(A, 1, 1000, 12, 1040, 12),  # 6 mil from the bar
        ],
        components=[_part(0, kind=4)],
    )


def _spacing_summary(result):
    records = [(record.first.owner, record.second.owner, round(record.gap_mil, 6),
                record.layer, record.layers, sorted(record.marks)) for record in result.records]
    return records, result.no_net_pairs, result.net_tie_pairs, result.stopped


def test_smaller_reaches_are_cut_from_the_widest_result(monkeypatch):
    reaches = (16.0, 10.0, 7.0, 5.0, 3.5)
    fresh = {reach: _spacing_summary(geometry.spacing(_cache_board(), reach))
             for reach in reaches}
    assert fresh[5.0][0][0][4] == (1, 32) and fresh[10.0][0][0][4] == (1, 2, 3, 32)
    assert (fresh[5.0][1], fresh[10.0][1]) == (1, 2)
    # The tie: its bar on both pads (0), the A track on the B pad (4) and on the bar (6).
    assert (fresh[5.0][2], fresh[10.0][2]) == ({0: 3}, {0: 4})
    index = _cache_board()
    widest = geometry.spacing(index, 0.5 / 0.0254)

    def no_search(self):
        raise AssertionError("searched again")

    monkeypatch.setattr(geometry._Spacing, "prims_vs_prims", no_search)
    for reach in reaches:
        cut = geometry.spacing(index, reach)
        assert _spacing_summary(cut) == fresh[reach]
        assert cut.distance_calls == widest.distance_calls
    store = index._dfm_geometry.results["spacing"]
    assert len(store) <= geometry._RESULTS_KEPT
    assert (round(0.5 / 0.0254, 6), geometry.MAX_DISTANCE_CALLS) in store


def test_area_grids_of_old_grows_are_dropped():
    index = _index(areas=[_area(1, _square(0, 0, 50), A), _area(1, _square(60, 0, 50), B)])
    for reach in (12.0, 14.0, 16.0, 18.0, 20.0):
        assert len(geometry.spacing(index, reach).records) == 1
    model = index._dfm_geometry
    assert len([key for key in model._grids if key[0] == 1]) <= 2
    assert (1, geometry._grid_grow(20.0)) in model._grids


def test_one_record_per_owner_pair_with_its_layers():
    index = _index([_th_pad(A, 0, 500), _th_pad(B, 24, 500)])
    (record,) = geometry.spacing(index, 5.0).records
    assert record.layers == (1, 2, 3, 32) and record.gap_mil == pytest.approx(4.0)


def test_work_cap_stops_the_scan():
    pairs = []
    for number in range(3):
        x = number * 500.0
        pairs += [_track(A, 1, x, 0, x + 20, 0), _track(B, 1, x, 8, x + 20, 8)]
    index = _index(pairs)
    capped = geometry.spacing(index, 5.0, cap=0)
    assert capped.stopped and len(capped.records) < 3
    assert len(geometry.spacing(index, 5.0).records) == 3


def test_results_and_model_are_cached_on_the_index():
    index = _index([_track(A, 1, 0, 0, 100, 0), _track(B, 1, 0, 9, 100, 9)])
    first = geometry.spacing(index, 5.0)
    assert geometry.spacing(index, 5.0) is first
    model = index._dfm_geometry
    geometry.copper_to_edge(index, 10.0)
    geometry.hole_to_hole(index, 10.0)
    assert index._dfm_geometry is model
    store = model.results["spacing"]
    cap = geometry.MAX_DISTANCE_CALLS
    # A complete result of a larger reach replaces those of smaller ones and serves them.
    assert geometry.spacing(index, 6.0) is not first
    assert list(store) == [(6.0, cap)]
    again = geometry.spacing(index, 5.0)
    assert again is not first and again.records == first.records
    assert geometry.spacing(index, 5.0) is again
    for reach in (7.0, 8.0, 9.0, 10.0):
        geometry.spacing(index, reach)
    assert list(store) == [(10.0, cap)]
    for reach in (9.0, 8.0, 7.0, 6.0, 5.0):
        geometry.spacing(index, reach)
    assert len(store) == geometry._RESULTS_KEPT and (10.0, cap) in store


def test_holes_build_no_copper_model(monkeypatch):
    def no_build(self):
        raise AssertionError("the copper model was built")

    monkeypatch.setattr(geometry._Model, "_build_items", no_build)
    monkeypatch.setattr(geometry._Model, "_build_areas", no_build)
    index = _index([_via(A, 0, 0), _th_pad(B, 100, 0), _track(A, 1, 0, 0, 100, 0)],
                   areas=[_area(1, _square(0, 0, 50), A)])
    assert [hole.kind for hole in geometry.holes(index)] == ["via", "pad"]


def test_a_replaced_index_is_freed_without_a_collection():
    """The model holds its index weakly: dropping the index frees both at once, with no
    reference cycle left for the garbage collector (MiniPC keeps about 290 MB)."""
    index = _index(
        [_track(A, 1, 100, 10, 500, 10), _track(B, 1, 100, 19, 500, 19), _via(B, 960, 960),
         _th_pad(None, 300, 300, width=40.0, hole=40.0, plated=False), _via(A, 300, 330)],
        areas=[_area(1, _square(6, 300, 50), A)], outline=OUTLINE,
        bare_holes=[BareHole(x=500, y=990, hole=30.0, layer=74, net=None, component=None,
                             pad_name="M", plated=False)],
    )
    for search in (geometry.spacing, geometry.copper_to_edge, geometry.hole_to_hole,
                   geometry.hole_to_copper, geometry.hole_to_edge):
        assert search(index, 12.0).records
        search(index, 10.0)  # cut from the wider result
    geometry.board_minimums(index)
    assert geometry.spacing(index, 10.0).records[0].at
    index_ref, model_ref = weakref.ref(index), weakref.ref(index._dfm_geometry)
    gc.disable()
    try:
        del index
        assert index_ref() is None and model_ref() is None
    finally:
        gc.enable()


# ---------- copper to edge ----------


def test_edge_distance_to_the_outline_and_its_arc():
    index = _index(
        [
            _track(A, 1, 100, 10, 500, 10),  # 7 mil from the bottom edge
            _via(B, 960, 960),  # near the rounded corner
            _track(A, 1, 100, 500, 500, 500),  # far inside
        ],
        outline=OUTLINE,
    )
    result = geometry.copper_to_edge(index, 10.0)
    corner = 100 - math.hypot(60, 60) - 10
    assert [(record.copper.kind, round(record.distance_mil, 6)) for record in result.records] == [
        ("via", round(corner, 6)), ("track", 7.0),
    ]
    via, track = result.records
    assert via.layers == (1, 2, 3, 32) and via.edge == "outline" and not via.marks
    assert track.at[1] == pytest.approx(7.0)
    assert result.minimum() is via
    assert result.has_outline and not result.off_board and not result.stopped


def test_cutouts_off_board_copper_and_crossing():
    regions = (_region(56, _square(400, 400, 100), kind=3)
               + _region(56, _square(700, 400, 60), kind=3, component=1))
    index = _index(
        [
            _track(A, 1, 420, 395, 480, 395, width=4.0),  # 3 mil below the first cutout
            _track(A, 1, 710, 466, 750, 466, width=4.0),  # 4 mil above the part's cutout
            _track(B, 1, 430, 450, 470, 450, width=4.0),  # inside the first cutout
            _track(B, 1, 2000, 2000, 2100, 2000),  # off the board
            _track(C, 1, -50, 500, 50, 500, width=4.0),  # across the left edge
        ],
        outline=OUTLINE, regions=regions, components=[_part(0), _part(1)],
    )
    result = geometry.copper_to_edge(index, 10.0)
    assert (result.cutouts, result.footprint_cutouts) == (2, 1)
    found = [(record.copper.row, round(record.distance_mil, 6), record.edge, record.cutout,
              record.cutout_component, sorted(record.marks)) for record in result.records]
    assert found == [
        (5, 0.0, "outline", None, None, ["crosses"]),
        (1, 3.0, "cutout", 0, None, []),
        (2, 4.0, "cutout", 1, 1, []),
    ]
    off = sorted((record.copper.row, record.inside_cutout, record.objects)
                 for record in result.off_board)
    assert off == [(3, True, 1), (4, False, 1)]


def test_copper_over_a_cutout():
    index = _index(
        areas=[_area(1, _square(350, 350, 200), A)],
        outline=OUTLINE, regions=_region(56, _square(400, 400, 100), kind=3),
    )
    (record,) = geometry.copper_to_edge(index, 10.0).records
    assert (record.copper.kind, record.distance_mil, record.edge) == ("region", 0.0, "cutout")


def test_edge_fingers_and_edge_plated_holes():
    finger = dict(kind="pad", layer=1, width=20.0, height=60.0, shape=PAD_SHAPE_RECT)
    primitives = [
        Primitive(net=A, component=1, x1=x, y1=35, x2=x, y2=35, pad_name=str(n), **finger)
        for n, x in enumerate(range(100, 400, 50))  # 6 fingers, 5 mil from the edge
    ]
    primitives += [
        Primitive(net=B, component=2, x1=x, y1=35, x2=x, y2=35, pad_name=str(n), **finger)
        for n, x in enumerate(range(600, 800, 50))  # 4 pads: not an edge connector
    ]
    primitives.append(_th_pad(GND, 0, 500, width=60.0, hole=40.0, component=3))  # half hole
    bare = [
        BareHole(x=500, y=1000, hole=40.0, layer=74, net=None, component=None,
                 pad_name="M", plated=True),
        BareHole(x=300, y=1000, hole=40.0, layer=74, net=None, component=None,
                 pad_name="N", plated=False),
    ]
    index = _index(primitives, outline=OUTLINE, bare_holes=bare,
                   components=[_part(0), _part(1), _part(2), _part(3)])
    result = geometry.copper_to_edge(index, 10.0)
    (group,) = result.fingers
    assert (group.component, len(group.pads), group.distance_mil) == (1, 6, pytest.approx(5.0))
    assert group.layers == (1,) and group.edge == "outline" and group.at[1] == 35
    assert sorted(record.copper.component for record in result.records) == [2, 2, 2, 2]
    holes = sorted((hole.hole.kind, hole.copper.component if hole.copper else None)
                   for hole in result.edge_holes)
    assert holes == [("bare_hole", None), ("pad", 3)]
    assert not result.off_board
    assert result.minimum().distance_mil == pytest.approx(5.0) and group.layer == 1


def test_ic_and_connector_pads_at_the_edge_are_no_fingers():
    """Fingers are a card edge: long, narrow pads on an outer layer that make up the part.
    A QFN's row at the edge, a connector with other pads and pads on an inner layer stay
    edge records (errors), whatever their number and orientation."""
    side = dict(kind="pad", layer=1, width=9.84, height=31.5, shape=PAD_SHAPE_RECT)
    finger = dict(kind="pad", layer=1, width=20.0, height=60.0, shape=PAD_SHAPE_RECT)
    inner = dict(kind="pad", layer=2, width=60.0, height=20.0, shape=PAD_SHAPE_RECT)
    primitives = [  # U1: a QFN-32's bottom row 4 mil from the edge, 24 more pads inside
        Primitive(net=A, component=1, x1=300 + 20 * n, y1=4 + 31.5 / 2, x2=300 + 20 * n,
                  y2=4 + 31.5 / 2, pad_name=str(n), **side)
        for n in range(8)
    ]
    primitives += [
        Primitive(net=A, component=1, x1=300 + 20 * n, y1=200, x2=300 + 20 * n, y2=200,
                  pad_name=str(8 + n), **side)
        for n in range(24)
    ]
    primitives += [  # U2: 6 long pads 5 mil from the edge and 4 more inside
        Primitive(net=B, component=2, x1=x, y1=35, x2=x, y2=35, pad_name=str(x), **finger)
        for x in range(600, 900, 50)
    ]
    primitives += [
        Primitive(net=B, component=2, x1=x, y1=400, x2=x, y2=400, pad_name=str(x), **finger)
        for x in range(600, 800, 50)
    ]
    primitives += [  # U3: 6 long pads on an inner layer, 5 mil from the left edge
        Primitive(net=C, component=3, x1=35, y1=y, x2=35, y2=y, pad_name=str(y), **inner)
        for y in range(400, 700, 50)
    ]
    primitives += [  # U4: a part of only 8 short pads 4 mil from the top edge (too short)
        Primitive(net=C, component=4, x1=300 + 20 * n, y1=1000 - 4 - 31.5 / 2, x2=300 + 20 * n,
                  y2=1000 - 4 - 31.5 / 2, pad_name=str(n), **side)
        for n in range(8)
    ]
    index = _index(primitives, outline=OUTLINE,
                   components=[_part(0), _part(1), _part(2), _part(3), _part(4)])
    result = geometry.copper_to_edge(index, 10.0)
    assert result.fingers == []
    assert sorted((record.copper.component, round(record.distance_mil, 6))
                  for record in result.records) == (
        [(1, 4.0)] * 8 + [(2, 5.0)] * 6 + [(3, 5.0)] * 6 + [(4, 4.0)] * 8
    )


def test_edge_results_cut_from_a_wider_reach_match_a_fresh_search():
    """A smaller reach is cut from a wider cached result, and its fingers are judged at
    that reach: U1's 6 fingers are within 1 mm, only 2 are within 10 mil."""
    finger = dict(kind="pad", layer=1, width=20.0, height=60.0, shape=PAD_SHAPE_RECT)

    def board():
        primitives = [
            Primitive(net=A, component=1, x1=x, y1=965 - (25 if n >= 2 else 0), x2=x,
                      y2=965 - (25 if n >= 2 else 0), pad_name=str(n), **finger)
            for n, x in enumerate(range(100, 400, 50))
        ]
        primitives += [
            _track(A, 1, 100, 10, 500, 10), _via(B, 960, 960),
            _track(C, 1, -50, 500, 50, 500, width=4.0), _track(B, 1, 2000, 2000, 2100, 2000),
            _th_pad(GND, 0, 600, width=60.0, hole=40.0, component=3),
        ]
        return _index(primitives, outline=OUTLINE, areas=[_area(1, _square(6, 300, 50), A)],
                      components=[_part(0), _part(1), _part(2), _part(3)])

    def summary(result):
        return (
            [(record.copper.owner, round(record.distance_mil, 6), record.layers,
              sorted(record.marks)) for record in result.records],
            [(group.component, len(group.pads), round(group.distance_mil, 6))
             for group in result.fingers],
            [(record.copper.owner, record.inside_cutout) for record in result.off_board],
            [(found.hole.row, found.edge) for found in result.edge_holes],
        )

    fresh = summary(geometry.copper_to_edge(board(), 10.0))
    index = board()
    wide = geometry.copper_to_edge(index, geometry.DEFAULT_EDGE_REACH_MIL)
    assert [(group.component, len(group.pads)) for group in wide.fingers] == [(1, 6)]
    cut = geometry.copper_to_edge(index, 10.0)
    assert summary(cut) == fresh and cut.fingers == []
    assert sum(record.copper.component == 1 for record in cut.records) == 2
    assert cut.distance_calls == wide.distance_calls


def test_area_over_the_whole_board_crosses_the_edge():
    """Copper drawn larger than the board covers its edge (it crosses it), however far
    its own edges are; a ring of copper around the board lies off it."""
    ring = _area(2, _square(-200, -200, 1400), GND, holes=[_square(-100, -100, 1200)])
    index = _index(areas=[_area(1, _square(-50, -50, 1100), GND, kind="fill"), ring],
                   outline=OUTLINE)
    result = geometry.copper_to_edge(index, 10.0)
    assert [(record.copper.kind, record.distance_mil, record.edge, sorted(record.marks))
            for record in result.records] == [("fill", 0.0, "outline", ["crosses"])]
    assert [(record.copper.owner, record.inside_cutout) for record in result.off_board] == [
        (("area", 1), False),
    ]
    assert result.minimum().distance_mil == 0.0
    # Copper reaching the edge is found at any reach, 0 included (a profile asking for
    # 0 mil to the edge): nothing is then closer than the reach, and nothing that reaches
    # the edge, the first vertex of a region outside the board, is off-board copper.
    index = _index(
        [_track(C, 1, -50, 500, 50, 500, width=4.0)],
        areas=[_area(1, _square(-50, -50, 1100), GND, kind="fill"),
               _area(3, [(-20, 100), (500, 100), (500, 500), (-20, 500)], A), ring],
        outline=OUTLINE,
    )
    for reach in (0.0, 10.0):
        result = geometry.copper_to_edge(index, reach)
        assert len(result.records) == (3 if reach else 0)
        assert [record.copper.owner for record in result.off_board] == [("area", 2)]


def test_degenerate_cutout_is_no_edge():
    """A board cutout whose vertices all coincide has no edge: it is counted, not used."""
    index = _index([_track(A, 1, 100, 100, 200, 100)], outline=OUTLINE,
                   regions=_region(56, [(500, 500), (500, 500), (500, 500)], kind=3))
    result = geometry.copper_to_edge(index, 10.0)
    assert (result.cutouts, result.degenerate_cutouts, result.records) == (0, 1, [])
    assert geometry.hole_to_edge(index, 10.0).records == []


def test_area_edge_records_and_stale_pours():
    index = _index(
        areas=[
            _area(1, _square(6, 300, 50), A),  # 6 mil from the left edge
            _area(1, _square(980, 300, 50), GND, pour_id=0),  # across the right edge
            _area(1, _square(2000, 0, 50), B),  # off the board
        ],
        pours=[PolygonPour(0, GND, 1, "Solid", _square(980, 300, 50), copper_invalid=True)],
        outline=OUTLINE,
    )
    result = geometry.copper_to_edge(index, 10.0)
    found = [(record.copper.kind, round(record.distance_mil, 6), sorted(record.marks))
             for record in result.records]
    assert found == [("pour", 0.0, ["crosses", "stale_pour"]), ("region", 6.0, [])]
    (off,) = result.off_board
    assert off.copper.owner == ("area", 2)
    assert result.minimum() is result.records[1]  # stale pour copper left out


def test_edge_without_the_rtree_matches():
    regions = _region(56, _square(400, 400, 100), kind=3)
    primitives = [
        _track(A, 1, 420, 395, 480, 395, width=4.0),
        _track(A, 1, 100, 10, 500, 10),
        _via(B, 960, 960),
    ]
    with_rtree = _index(primitives, outline=OUTLINE, regions=regions)
    without = _index(primitives, outline=OUTLINE, regions=regions)
    without._has_rtree = False

    def summary(index):
        return [(record.copper.owner, record.distance_mil)
                for record in geometry.copper_to_edge(index, 10.0).records]

    assert summary(without) == summary(with_rtree) and len(summary(without)) == 3


def test_no_outline_measures_nothing():
    result = geometry.copper_to_edge(_index([_track(A, 1, 0, 0, 10, 0)]), 10.0)
    assert not result.has_outline and result.records == [] and result.minimum() is None


# ---------- hole to hole ----------


def test_hole_to_hole():
    index = _index(
        [
            _via(A, 0, 0, hole=10.0), _via(B, 18, 0, hole=10.0),  # 8 mil apart
            _via(A, 100, 0, hole=6.0, layer=1, layer_to=2),  # blind
            _via(B, 108, 0, hole=6.0, layer=3, layer_to=32),  # buried: spans apart
            _via(A, 150, 0, hole=6.0, layer=1, layer_to=2),  # stacked microvias: they
            _via(A, 150, 0, hole=6.0, layer=2, layer_to=3),  # meet at layer 2 only
            # A vertical slot (slot angle relative to the pad): 2 mil from the via above.
            _th_pad(A, 300, 0, width=30.0, height=70.0, hole=10.0, hole_shape=2,
                    slot_length=50.0, slot_rotation=90.0),
            _via(B, 300, 32, hole=10.0),
            _th_pad(A, 528, 0, hole=10.0),  # 8 mil from the bare hole
            _via(A, 700, 0, hole=10.0), _via(A, 700, 0, hole=10.0),  # drilled twice
            _th_pad(A, 900, 0, hole=10.0, hole_offset=True), _via(B, 918, 0, hole=10.0),
        ],
        bare_holes=[BareHole(x=500, y=0, hole=30.0, layer=74, net=None, component=None,
                             pad_name="M", plated=False)],
    )
    assert len(geometry.holes(index)) == 14  # 10 vias, 3 pads, 1 bare hole
    result = geometry.hole_to_hole(index, 10.0)
    found = [(round(record.gap_mil, 6), record.pair_class, sorted(record.marks),
              sorted((record.first.kind, record.second.kind))) for record in result.records]
    assert sorted(found) == [
        (0.0, "via", ["coincident", "touching"], ["via", "via"]),
        (2.0, "pad", [], ["pad", "via"]),
        (8.0, "pad", [], ["bare_hole", "pad"]),
        (8.0, "pad", ["hole_offset"], ["pad", "via"]),
        (8.0, "via", [], ["via", "via"]),
    ]
    slot = next(record for record in result.records if record.gap_mil == pytest.approx(2.0))
    assert slot.at == pytest.approx((300.0, 26.0))
    assert result.minimum().gap_mil == pytest.approx(2.0)


def _bare(x, y, hole=40.0, plated=False, net=None):
    return BareHole(x=x, y=y, hole=hole, layer=74, net=net, component=None, pad_name="M",
                    plated=plated)


def test_copper_near_hole_walls():
    """Walls no copper of the hole covers: copper of any net (no net too) to a
    non-plated wall, copper of other nets to a plated bare hole's barrel."""
    index = _index(
        [
            # A non-plated pad, copper no larger than its hole: a wall on every layer.
            _th_pad(None, 0, 0, width=100.0, hole=120.0, plated=False),
            _track(A, 1, -200, 0, 200, 0, width=8.0),  # straight through it
            _track(None, 3, 450, 25, 550, 25, width=4.0),  # 3 mil from the bare hole
            _track(GND, 1, 950, 25, 1050, 25, width=4.0),  # the plated hole's own net
            _track(B, 2, 950, 25, 1050, 25, width=4.0),
            _track(None, 1, 1450, 25, 1550, 25, width=4.0),  # no net, like that hole
            _track(C, 1, 1450, -25, 1550, -25, width=4.0),
            # Non-plated with a ring on the outer layers only: a wall on the inner ones.
            _th_pad(None, 2000, 0, width=80.0, hole=40.0, plated=False,
                    layer_copper={PAD_INNER_LAYERS: PadCopper(30.0, 30.0, 1)}),
            _track(A, 2, 1950, 25, 2050, 25, width=4.0),
            _track(A, 1, 1950, 25, 2050, 25, width=4.0),  # over the ring: spacing's
        ],
        bare_holes=[_bare(500, 0), _bare(1000, 0, plated=True, net=GND),
                    _bare(1500, 0, plated=True)],
        pours=[PolygonPour(0, GND, 2, "Solid", _square(400, -100, 200))],
        areas=[
            _area(2, _square(400, -100, 200), GND, holes=[_square(476, -24, 48)], pour_id=0),
            _area(3, _square(-100, -100, 200), B),  # over the first hole, no void
        ],
    )
    result = geometry.hole_to_copper(index, 5.0)
    assert result.walls == 5 and not result.stopped
    found = [(record.hole.kind, record.hole.x, record.copper.kind, record.copper.net,
              round(record.gap_mil, 6), record.layers, sorted(record.marks))
             for record in result.records]
    assert found == [
        ("pad", 0.0, "region", B, 0.0, (3,), ["touching"]),
        ("pad", 0.0, "track", A, 0.0, (1,), ["touching"]),
        ("bare_hole", 500.0, "track", None, 3.0, (3,), []),
        ("bare_hole", 1000.0, "track", B, 3.0, (2,), []),
        ("bare_hole", 1500.0, "track", C, 3.0, (1,), []),
        ("pad", 2000.0, "track", A, 3.0, (2,), []),
        ("bare_hole", 500.0, "pour", GND, 4.0, (2,), []),
    ]
    assert result.records[2].at == pytest.approx((500.0, 21.5))
    assert result.minimum().gap_mil == pytest.approx(3.0)
    cut = geometry.hole_to_copper(index, 3.5)
    assert [record.gap_mil for record in cut.records] == [0.0, 0.0, 3.0, 3.0, 3.0, 3.0]
    # A part's own pad 2 mil from its NPTH peg is marked, and still sets the minimum.
    peg = geometry.hole_to_copper(_index(
        [_th_pad(None, 0, 0, width=40.0, hole=40.0, plated=False, component=1),
         _pad(A, 1, 0, 32, width=20.0)], components=[_part(0), _part(1)]), 5.0)
    (record,) = peg.records
    assert record.marks == {"same_part"} and peg.minimum() is record
    assert record.gap_mil == pytest.approx(2.0)


def test_holes_near_the_board_edge():
    index = _index(
        [
            _th_pad(None, 500, 26, width=40.0, hole=40.0, plated=False),  # 6 mil
            _th_pad(None, 970, 300, width=80.0, hole=40.0, plated=False),  # a ring: 10 mil
            _via(A, 500, 995),  # a via's copper is copper_to_edge's
        ],
        bare_holes=[
            _bare(5, 500),  # across the left edge
            _bare(300, 1000, plated=True),  # cut by the edge: an edge-plated hole
            _bare(700, 975, plated=True),  # 5 mil
            _bare(2000, 2000),  # off the board
            _bare(500, -30),  # off the board, 10 mil outside the edge
            _bare(500, 500),  # far inside
        ],
        outline=OUTLINE,
    )
    result = geometry.hole_to_edge(index, 12.0)
    found = [(record.hole.kind, record.hole.plated, round(record.distance_mil, 6),
              record.edge, sorted(record.marks)) for record in result.records]
    assert found == [
        ("bare_hole", False, 0.0, "outline", ["crosses"]),
        ("bare_hole", True, 5.0, "outline", []),
        ("pad", False, 6.0, "outline", []),
        ("pad", False, 10.0, "outline", []),
    ]
    assert result.records[2].at == pytest.approx((500.0, 6.0))
    assert [record.distance_mil for record in geometry.hole_to_edge(index, 5.5).records] == [
        0.0, pytest.approx(5.0)]
    edge = geometry.copper_to_edge(index, 12.0)
    assert [found.hole.bare for found in edge.edge_holes] == [1]
    assert not geometry.hole_to_edge(_index([], bare_holes=[_bare(0, 0)]), 12.0).has_outline


def test_board_minimums():
    index = _index(
        [_track(A, 1, 0, 0, 100, 0), _track(B, 1, 0, 9, 100, 9), _track(C, 1, 0, 400, 100, 400),
         _track(B, 1, 0, 401, 100, 401), _via(A, 300, 300), _via(B, 324, 300)],
        outline=OUTLINE,
    )
    minimums = geometry.board_minimums(index)
    assert minimums["min_gap"]["value_mil"] == pytest.approx(3.0)
    assert minimums["min_gap"]["touching_pairs"] == 1
    assert minimums["min_gap"]["reach_mil"] == pytest.approx(geometry.DEFAULT_GAP_REACH_MIL)
    assert minimums["min_copper_to_edge"]["value_mil"] == pytest.approx(0.0)  # crosses
    assert minimums["min_hole_to_hole"]["value_mil"] == pytest.approx(16.0)
    # A result cached for a larger reach serves a smaller one.
    wider = geometry.spacing(index, 12.0)
    assert geometry.board_minimums(index, gap_reach_mil=6.0)["min_gap"]["reach_mil"] == 12.0
    assert wider.minimum().gap_mil == pytest.approx(3.0)


# ---------- Altium's examples ----------


def _example(name):
    path = next(EXAMPLES.rglob(name), None) if EXAMPLES.is_dir() else None
    if path is None:
        pytest.skip("Altium example boards not installed")
    return PcbIndex(parse_pcb_doc(str(path)), None)


@pytest.fixture(scope="module")
def minipc():
    return _example("MiniPC.PcbDoc")


def test_minipc_spacing(minipc):
    """At 0.127 mm every pair is a real gap: vias are measured per layer (no via-void
    'shorts'), and MiniPC's BGA pads are the only footprint pairs."""
    result = geometry.spacing(minipc, 5.0)
    assert len(result.records) == 16541 and not result.stopped
    assert not any("touching" in record.marks for record in result.records)
    # 180 full-stack vias without inner pads sit in GND pour voids: barrel to void edge
    # is 0.1 mm (3.937 mil), where their nominal pads would read as overlapping.
    padless = []
    for record in result.records:
        for side in (record.first, record.second):
            via = minipc.pcb.primitives[side.row - 1] if side.kind == "via" else None
            if via is not None and via.via.pad_diameter(record.layer, via.width) is None:
                padless.append(round(record.gap_mil, 3))
    assert padless == [3.937] * 180
    assert sum("same_footprint" in record.marks for record in result.records) == 226
    assert result.minimum().gap_mil == pytest.approx(3.5432, abs=1e-4)
    # The work stays far below the cap (about 0.3M distances).
    assert result.distance_calls < 500_000
    edge = geometry.copper_to_edge(minipc, 10.0)
    assert edge.records == [] and edge.off_board == [] and edge.edge_holes == []
    assert len(geometry.holes(minipc)) == 4270


def test_sl1_net_tie_is_not_a_short():
    index = _example("SL1 Xilinx Spartan-IIE PQ208 Rev1.02.PcbDoc")
    result = geometry.spacing(index, 5.0)
    assert result.records == []
    ((component, pairs),) = result.net_tie_pairs.items()
    assert pairs == 4 and index.pcb.components[component].kind == 4


@pytest.mark.parametrize("name, pads, others", [
    ("PC4-SODIMM_V200_RC_A1_20141015.PcbDoc", 260, 3),
    ("WiFi.PcbDoc", 52, 4),
])
def test_edge_connector_fingers(name, pads, others):
    result = geometry.copper_to_edge(_example(name), 10.0)
    (group,) = result.fingers
    assert len(group.pads) == pads
    assert len(result.records) == others
    assert all(record.copper.kind == "pour" for record in result.records)


def test_bluetooth_hatched_pours_are_one_object_each():
    index = _example("Bluetooth_Sentinel.PcbDoc")
    assert len(geometry.spacing(index, 5.0).records) == 805
    edge = geometry.copper_to_edge(index, 10.0)
    kinds = sorted(record.copper.kind for record in edge.records)
    assert kinds == ["pour"] * 10 + ["via"] * 2
