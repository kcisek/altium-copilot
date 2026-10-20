"""Two-dimensional geometry helpers for PCB proximity queries."""

import math


def point_seg_distance(
    px: float,
    py: float,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
) -> float:
    """Return the minimum distance from a point to a line segment."""
    dx = x2 - x1
    dy = y2 - y1
    length_squared = dx * dx + dy * dy
    if length_squared == 0:
        return math.hypot(px - x1, py - y1)
    projection = ((px - x1) * dx + (py - y1) * dy) / length_squared
    projection = max(0.0, min(1.0, projection))
    return math.hypot(
        px - (x1 + projection * dx),
        py - (y1 + projection * dy),
    )


def _segments_intersect(
    ax: float,
    ay: float,
    bx: float,
    by: float,
    cx: float,
    cy: float,
    dx: float,
    dy: float,
) -> bool:
    def orientation(ox, oy, px, py, qx, qy):
        value = (px - ox) * (qy - oy) - (py - oy) * (qx - ox)
        return 0 if value == 0 else (1 if value > 0 else -1)

    first = orientation(ax, ay, bx, by, cx, cy)
    second = orientation(ax, ay, bx, by, dx, dy)
    third = orientation(cx, cy, dx, dy, ax, ay)
    fourth = orientation(cx, cy, dx, dy, bx, by)
    return first != second and third != fourth


def seg_seg_distance(
    ax: float,
    ay: float,
    bx: float,
    by: float,
    cx: float,
    cy: float,
    dx: float,
    dy: float,
) -> float:
    """Return the minimum centerline distance between two segments."""
    if _segments_intersect(ax, ay, bx, by, cx, cy, dx, dy):
        return 0.0
    return min(
        point_seg_distance(ax, ay, cx, cy, dx, dy),
        point_seg_distance(bx, by, cx, cy, dx, dy),
        point_seg_distance(cx, cy, ax, ay, bx, by),
        point_seg_distance(dx, dy, ax, ay, bx, by),
    )


def edge_to_edge(
    centerline_distance: float,
    width_a: float,
    width_b: float,
) -> float:
    """Convert centerline separation to copper edge-to-edge clearance."""
    return max(0.0, centerline_distance - (width_a + width_b) / 2.0)


def parallel_overlap(
    ax: float,
    ay: float,
    bx: float,
    by: float,
    cx: float,
    cy: float,
    dx: float,
    dy: float,
) -> float:
    """Approximate coupled run length using projected segment overlap."""
    unit_x = bx - ax
    unit_y = by - ay
    first_length = math.hypot(unit_x, unit_y)
    if first_length == 0:
        return 0.0
    unit_x /= first_length
    unit_y /= first_length
    projection_1 = (cx - ax) * unit_x + (cy - ay) * unit_y
    projection_2 = (dx - ax) * unit_x + (dy - ay) * unit_y
    low = min(projection_1, projection_2)
    high = max(projection_1, projection_2)
    overlap = max(0.0, min(high, first_length) - max(low, 0.0))
    second_length = math.hypot(dx - cx, dy - cy)
    if second_length == 0 or overlap == 0:
        return 0.0
    cosine = abs(
        (dx - cx) * unit_x + (dy - cy) * unit_y
    ) / second_length
    return overlap * cosine


def point_in_polygon(
    px: float,
    py: float,
    vertices: list[tuple[float, float]],
) -> bool:
    """Return whether a point is inside a polygon using ray casting."""
    inside = False
    for index, (x1, y1) in enumerate(vertices):
        x2, y2 = vertices[(index + 1) % len(vertices)]
        if (y1 > py) != (y2 > py):
            intersection_x = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
            if px < intersection_x:
                inside = not inside
    return inside


def oriented_rect_vertices(
    cx: float,
    cy: float,
    width: float,
    height: float,
    rotation_deg: float,
) -> list[tuple[float, float]]:
    """Return the four vertices of a centered, rotated rectangle."""
    angle = math.radians(rotation_deg)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    vertices = []
    for local_x, local_y in (
        (-width / 2.0, -height / 2.0),
        (width / 2.0, -height / 2.0),
        (width / 2.0, height / 2.0),
        (-width / 2.0, height / 2.0),
    ):
        vertices.append(
            (
                cx + local_x * cosine - local_y * sine,
                cy + local_x * sine + local_y * cosine,
            )
        )
    return vertices


def point_polygon_distance(
    px: float,
    py: float,
    vertices: list[tuple[float, float]],
) -> float:
    """Return minimum distance from a point to a filled polygon."""
    if not vertices:
        return math.inf
    if point_in_polygon(px, py, vertices):
        return 0.0
    closed = vertices + [vertices[0]]
    return min(
        point_seg_distance(px, py, x1, y1, x2, y2)
        for (x1, y1), (x2, y2) in zip(closed, closed[1:])
    )


def seg_polygon_distance(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    vertices: list[tuple[float, float]],
) -> float:
    """Return minimum distance from a segment to a filled polygon."""
    if not vertices:
        return math.inf
    if (
        point_in_polygon(x1, y1, vertices)
        or point_in_polygon(x2, y2, vertices)
    ):
        return 0.0
    closed = vertices + [vertices[0]]
    return min(
        seg_seg_distance(x1, y1, x2, y2, ax, ay, bx, by)
        for (ax, ay), (bx, by) in zip(closed, closed[1:])
    )


def polygon_polygon_distance(
    first: list[tuple[float, float]],
    second: list[tuple[float, float]],
) -> float:
    """Return minimum distance between two filled polygons."""
    if not first or not second:
        return math.inf
    if (
        point_in_polygon(*first[0], second)
        or point_in_polygon(*second[0], first)
    ):
        return 0.0
    first_closed = first + [first[0]]
    second_closed = second + [second[0]]
    return min(
        seg_seg_distance(ax, ay, bx, by, cx, cy, dx, dy)
        for (ax, ay), (bx, by) in zip(
            first_closed,
            first_closed[1:],
        )
        for (cx, cy), (dx, dy) in zip(
            second_closed,
            second_closed[1:],
        )
    )


# An arc piece is (cx, cy, r, x1, y1, x2, y2): the shorter way round the
# circle of radius r about (cx, cy) from its end (x1, y1) to (x2, y2). The
# end points lie on the circle; a piece spans less than 180 degrees.


def in_arc_span(px: float, py: float, arc: tuple) -> bool:
    """Is the direction of the point from the arc's centre within its span?
    The centre itself is (every point of the arc is r from it)."""
    cx, cy, _, x1, y1, x2, y2 = arc
    ax, ay = x1 - cx, y1 - cy
    bx, by = x2 - cx, y2 - cy
    vx, vy = px - cx, py - cy
    turn = ax * by - ay * bx
    after_start = ax * vy - ay * vx
    before_end = vx * by - vy * bx
    if turn > 0:
        return after_start >= 0 and before_end >= 0
    if turn < 0:
        return after_start <= 0 and before_end <= 0
    # A zero-length piece is just its end point (measured from the ends).
    return vx == 0 and vy == 0


def point_arc_distance(px: float, py: float, arc: tuple) -> float:
    """Return the minimum distance from a point to an arc piece."""
    cx, cy, radius, x1, y1, x2, y2 = arc
    if in_arc_span(px, py, arc):
        return abs(math.hypot(px - cx, py - cy) - radius)
    return min(math.hypot(px - x1, py - y1), math.hypot(px - x2, py - y2))


def seg_arc_distance(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    arc: tuple,
) -> float:
    """Return the minimum distance between a segment and an arc piece.

    The closest points are an end of one of them, or the foot of the
    perpendicular from the arc's centre on the segment and the arc point on
    that radius; every candidate is a true distance, so the smallest is exact.
    """
    cx, cy, radius, ax, ay, bx, by = arc
    candidates = [
        point_arc_distance(x1, y1, arc),
        point_arc_distance(x2, y2, arc),
        point_seg_distance(ax, ay, x1, y1, x2, y2),
        point_seg_distance(bx, by, x1, y1, x2, y2),
    ]
    dx, dy = x2 - x1, y2 - y1
    length_squared = dx * dx + dy * dy
    if length_squared > 0:
        fx, fy = x1 - cx, y1 - cy
        along = -(fx * dx + fy * dy) / length_squared
        if 0 < along < 1:
            candidates.append(
                point_arc_distance(x1 + along * dx, y1 + along * dy, arc)
            )
        # Crossings of the segment and the circle: |f + t d| = r.
        discriminant = along * along - (fx * fx + fy * fy - radius * radius) / (
            length_squared
        )
        if discriminant >= 0:
            root = math.sqrt(discriminant)
            for t in (along - root, along + root):
                if 0 <= t <= 1 and in_arc_span(x1 + t * dx, y1 + t * dy, arc):
                    return 0.0
    return min(candidates)


def polygon_arc_distance(
    vertices: list[tuple[float, float]],
    arc: tuple,
) -> float:
    """Return minimum distance from a filled polygon to an arc piece."""
    if not vertices:
        return math.inf
    _, _, _, ax, ay, bx, by = arc
    if point_in_polygon(ax, ay, vertices) or point_in_polygon(bx, by, vertices):
        return 0.0
    closed = vertices + [vertices[0]]
    return min(
        seg_arc_distance(x1, y1, x2, y2, arc)
        for (x1, y1), (x2, y2) in zip(closed, closed[1:])
    )


def arc_arc_distance(first: tuple, second: tuple) -> float:
    """Return the minimum distance between two arc pieces.

    The closest points are an end of one of them, or lie on the line through
    both centres (a common normal); concentric pieces are closest at an end.
    """
    c1x, c1y, r1, a1x, a1y, b1x, b1y = first
    c2x, c2y, r2, a2x, a2y, b2x, b2y = second
    candidates = [
        point_arc_distance(a1x, a1y, second),
        point_arc_distance(b1x, b1y, second),
        point_arc_distance(a2x, a2y, first),
        point_arc_distance(b2x, b2y, first),
    ]
    dx, dy = c2x - c1x, c2y - c1y
    apart = math.hypot(dx, dy)
    if apart > 0:
        ux, uy = dx / apart, dy / apart
        for sign in (1.0, -1.0):
            px, py = c1x + sign * r1 * ux, c1y + sign * r1 * uy
            if in_arc_span(px, py, first):
                candidates.append(point_arc_distance(px, py, second))
        if abs(r1 - r2) <= apart <= r1 + r2:
            along = (r1 * r1 - r2 * r2 + apart * apart) / (2.0 * apart)
            height = math.sqrt(max(0.0, r1 * r1 - along * along))
            for sign in (1.0, -1.0):
                px = c1x + along * ux - sign * height * uy
                py = c1y + along * uy + sign * height * ux
                if in_arc_span(px, py, first) and in_arc_span(px, py, second):
                    return 0.0
    return min(candidates)


def arc_bounds(arc: tuple) -> tuple[float, float, float, float]:
    """(min x, min y, max x, max y) of an arc piece: its ends and the
    circle's extremes within its span."""
    cx, cy, radius, x1, y1, x2, y2 = arc
    xs, ys = [x1, x2], [y1, y2]
    for ux, uy in ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0)):
        px, py = cx + radius * ux, cy + radius * uy
        if in_arc_span(px, py, arc):
            xs.append(px)
            ys.append(py)
    return min(xs), min(ys), max(xs), max(ys)
