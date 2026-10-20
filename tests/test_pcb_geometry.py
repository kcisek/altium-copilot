import math
import random

import pytest

from server.services.pcb_geometry import (
    arc_arc_distance,
    arc_bounds,
    edge_to_edge,
    oriented_rect_vertices,
    parallel_overlap,
    point_arc_distance,
    point_in_polygon,
    point_polygon_distance,
    point_seg_distance,
    polygon_arc_distance,
    seg_arc_distance,
    seg_seg_distance,
)


def _arc(cx, cy, radius, start_deg, end_deg):
    """Arc piece from start_deg to end_deg (the shorter way round)."""
    start, end = math.radians(start_deg), math.radians(end_deg)
    return (
        cx, cy, radius,
        cx + radius * math.cos(start), cy + radius * math.sin(start),
        cx + radius * math.cos(end), cy + radius * math.sin(end),
    )


# 60 to 120 degrees about the origin: apex (0, 30), ends at y = 25.98.
TOP = _arc(0.0, 0.0, 30.0, 60.0, 120.0)
END_X, END_Y = TOP[3], TOP[4]


def test_point_seg_distance():
    assert point_seg_distance(0, 5, 0, 0, 10, 0) == 5.0
    assert point_seg_distance(-3, 4, 0, 0, 10, 0) == 5.0
    assert point_seg_distance(5, 0, 0, 0, 10, 0) == 0.0


def test_seg_seg_distance_parallel():
    assert seg_seg_distance(0, 0, 10, 0, 0, 3, 10, 3) == 3.0


def test_seg_seg_distance_crossing_is_zero():
    assert seg_seg_distance(0, 0, 10, 10, 0, 10, 10, 0) == 0.0


def test_seg_seg_distance_collinear_gap():
    assert seg_seg_distance(0, 0, 10, 0, 15, 0, 25, 0) == 5.0


def test_seg_seg_distance_degenerate_points():
    assert seg_seg_distance(1, 1, 1, 1, 4, 5, 4, 5) == 5.0


def test_edge_to_edge_subtracts_half_widths():
    assert edge_to_edge(3.0, 2.0, 1.0) == 1.5
    assert edge_to_edge(1.0, 2.0, 1.0) == 0.0


def test_parallel_overlap_full():
    assert parallel_overlap(0, 0, 10, 0, 2, 1, 8, 1) == pytest.approx(6.0)


def test_parallel_overlap_none_for_perpendicular():
    assert parallel_overlap(0, 0, 10, 0, 5, -5, 5, 5) == pytest.approx(
        0.0,
        abs=0.5,
    )


def test_parallel_overlap_partial():
    assert parallel_overlap(0, 0, 10, 0, 5, 1, 15, 1) == pytest.approx(5.0)


def test_point_in_polygon():
    square = [(0, 0), (10, 0), (10, 10), (0, 10)]
    assert point_in_polygon(5, 5, square) is True
    assert point_in_polygon(15, 5, square) is False
    assert point_in_polygon(-1, -1, square) is False


def test_point_arc_distance_inside_beyond_and_off_the_span():
    assert point_arc_distance(0, 0, TOP) == pytest.approx(30.0)  # the centre
    assert point_arc_distance(0, 10, TOP) == pytest.approx(20.0)  # in the bend
    assert point_arc_distance(0, 40, TOP) == pytest.approx(10.0)  # beyond it
    # Off the span the closest point is an end.
    assert point_arc_distance(40, 0, TOP) == pytest.approx(math.hypot(40 - END_X, END_Y))


def test_seg_arc_distance_measures_to_the_arc_not_its_chord():
    # Above the apex: the chord between the ends is 4 mil lower.
    assert seg_arc_distance(-5, 40, 5, 40, TOP) == pytest.approx(10.0)
    # In the bend: the segment's ends are closest, radially.
    assert seg_arc_distance(-10, 20, 10, 20, TOP) == pytest.approx(30 - math.sqrt(500))
    # Crossing the arc.
    assert seg_arc_distance(0, 20, 0, 40, TOP) == 0.0
    assert seg_arc_distance(-40, 28, 40, 28, TOP) == 0.0
    # The perpendicular from the centre misses the span: an end is closest.
    assert seg_arc_distance(40, -10, 40, 10, TOP) == pytest.approx(
        math.hypot(40 - END_X, END_Y - 10)
    )
    # A zero-length segment is a point.
    assert seg_arc_distance(0, 10, 0, 10, TOP) == pytest.approx(20.0)


def test_polygon_arc_distance():
    above = [(-5, 35), (5, 35), (5, 45), (-5, 45)]
    assert polygon_arc_distance(above, TOP) == pytest.approx(5.0)
    # Straddling the apex, the arc's ends outside: the edges cross it.
    across = [(-2, 28), (2, 28), (2, 32), (-2, 32)]
    assert polygon_arc_distance(across, TOP) == 0.0
    # Holding the whole arc.
    around = [(-40, 0), (40, 0), (40, 40), (-40, 40)]
    assert polygon_arc_distance(around, TOP) == 0.0
    # A square turned 45 degrees in the bend: its top corner is closest.
    diamond = oriented_rect_vertices(0, 0, 20, 20, 45)
    assert polygon_arc_distance(diamond, TOP) == pytest.approx(30 - 10 * math.sqrt(2))


def test_arc_arc_distance():
    # Concentric with overlapping spans: the radial gap.
    assert arc_arc_distance(TOP, _arc(0, 0, 40, 70, 110)) == pytest.approx(10.0)
    assert arc_arc_distance(_arc(0, 0, 40, 70, 110), TOP) == pytest.approx(10.0)
    # Concentric, spans apart: end to end.
    assert arc_arc_distance(TOP, _arc(0, 0, 40, 150, 170)) == pytest.approx(
        math.sqrt(30**2 + 40**2 - 2 * 30 * 40 * math.cos(math.radians(30)))
    )
    # Facing apexes on the line through the centres.
    assert arc_arc_distance(TOP, _arc(0, 100, 30, 240, 300)) == pytest.approx(40.0)
    # Crossing.
    assert arc_arc_distance(TOP, _arc(0, 60, 30.5, 250, 290)) == 0.0


def test_arc_bounds_include_the_bulge():
    assert arc_bounds(TOP) == pytest.approx((-15.0, END_Y, 15.0, 30.0))
    assert arc_bounds(_arc(0, 0, 30, -15, 15))[2] == pytest.approx(30.0)


def _sampled_arc(arc, steps=1500):
    cx, cy, radius, x1, y1, x2, y2 = arc
    start = math.atan2(y1 - cy, x1 - cx)
    sweep = (math.atan2(y2 - cy, x2 - cx) - start + math.pi) % (2 * math.pi) - math.pi
    return [
        (cx + radius * math.cos(start + sweep * i / steps),
         cy + radius * math.sin(start + sweep * i / steps))
        for i in range(steps + 1)
    ]


def test_arc_distances_match_dense_sampling():
    # Every candidate is a true distance, so the exact one is never above the
    # sampled one, and sampling comes within its point spacing of it.
    rng = random.Random(7)

    def random_arc():
        start = rng.uniform(0, 360)
        return _arc(rng.uniform(-20, 20), rng.uniform(-20, 20), rng.uniform(5, 50),
                    start, start + rng.uniform(0.5, 30))

    def close(exact, sampled):
        return exact <= sampled + 1e-9 and sampled - exact < 0.02

    for _ in range(150):
        arc, other = random_arc(), random_arc()
        points = _sampled_arc(arc)
        x1, y1, x2, y2 = (rng.uniform(-60, 60) for _ in range(4))
        assert close(seg_arc_distance(x1, y1, x2, y2, arc),
                     min(point_seg_distance(x, y, x1, y1, x2, y2) for x, y in points))
        assert close(arc_arc_distance(arc, other),
                     min(point_arc_distance(x, y, other) for x, y in points))
        polygon = oriented_rect_vertices(rng.uniform(-40, 40), rng.uniform(-40, 40),
                                         rng.uniform(1, 30), rng.uniform(1, 30),
                                         rng.uniform(0, 180))
        assert close(polygon_arc_distance(polygon, arc),
                     min(point_polygon_distance(x, y, polygon) for x, y in points))
