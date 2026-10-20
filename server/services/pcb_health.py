"""Offline PCB Health Check: the checks of Altium Designer's PCB Health Check Monitor
computed from the saved PcbDoc, plus a few board-hygiene checks next to them.

Each Altium check keeps Altium's id, category name (the title its Batch DRC report and
the Health Check panel use) and level (Low, Medium, High: info, warning, error here),
so a saved report's health section can be compared category by category. Definitions
follow Altium Designer 26: a micro-segment is a region-shape edge shorter than 300
internal units (0.03 mil) on a user region (not pour copper); a 360deg component has
|rotation - 360| < 0.001; zero-area means no area at all. Checks Altium runs only in
debug mode are opt-in; checks that need data the PcbDoc does not hold (the source
PcbLib, embedded panels, the fonts on this PC) are listed as not checked.
"""

import math
import re
from collections import defaultdict

try:
    from parsers.pcb_doc import decode_fill_records, decode_region_records
    from services.pcb_geometry import (
        MAX_ARC_CHORDS, SNAP_MIL, contour, point_in_polygon, point_seg_distance,
    )
except ImportError:
    from server.parsers.pcb_doc import decode_fill_records, decode_region_records
    from server.services.pcb_geometry import (
        MAX_ARC_CHORDS, SNAP_MIL, contour, point_in_polygon, point_seg_distance,
    )

MICRO_SEGMENT_MIL = 0.03  # 300 internal units, Altium's GetSmallSegments(300)
ZERO_AREA_SQ_MIL = 1e-8  # one square internal unit
ROTATION_360_TOLERANCE = 0.001
GROUP_ABOVE = 25  # more items than this in one check become one grouped finding
LISTED = 50  # objects named in a grouped finding
FINDING_LIMIT = 100
SELF_INTERSECTION_MAX_POINTS = 2000

LEVEL_SEVERITY = {"high": "error", "medium": "warning", "low": "info"}
_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}

# key: (Altium id, category, panel group, level); id None for checks Altium lacks.
CHECKS = {
    "shelved_modified_polygons": (14, "Shelved/Modified Polygons", "Polygons", "high"),
    "duplicate_designators": (11, "Duplicate Component Designators", "Components", "medium"),
    "zero_area_polygons": (19, "Zero Area Polygons", "Polygons", "medium"),
    "zero_area_regions": (10, "Zero Area Regions", "Regions", "medium"),
    "self_intersecting_regions": (6, "Self-Intersecting Regions", "Regions", "medium"),
    "micro_segments_board": (15, "Micro-Segments (Board)", "Regions", "medium"),
    "micro_segments_copper": (16, "Micro-Segments (Copper)", "Regions", "medium"),
    "slot_holes": (4, "Slots Hole Size > Length", "Holes", "medium"),
    "broken_xsignals": (1, "Broken xSignals", "Signals", "medium"),
    "rotation_360": (5, "360deg Component", "Components", "low"),
    "reference_point_outside": (
        7, "Reference Point Outside the Component Area", "Components", "low"),
    "non_signal_layer_components": (8, "Components on Non-signal Layers", "Components", "low"),
    "unused_xsignals": (17, "Unused xSignals", "Signals", "low"),
    "invalid_rules": (0, "Invalid PCB Design Rules", "Rules", "low"),
    "micro_segments_graphics": (
        3, "Micro-Segments (Graphics/Mechanical)", "Regions", "low"),
    # Not Altium Health Check items.
    "components_outside_board": (None, "Components outside the board shape", "Extra", None),
    "duplicate_copper": (None, "Duplicate copper", "Extra", "low"),
    "outline_micro_segments": (None, "Board outline micro-segments", "Extra", "low"),
}
# Altium runs these only in debug mode, so its panel and reports never show them.
OPT_IN = frozenset({"micro_segments_graphics"})
# Altium checks the PcbDoc cannot answer offline: id -> (key, category, reason).
NOT_CHECKED = {
    2: ("mixed_layer_indexes", "Mixed Layer Indexes",
        "Altium runs it only in debug mode; it inspects layer-stack internals."),
    9: ("non_compatible_stackups", "Non-compatible Stackups in Panel",
        "embedded board arrays (panels) are not read."),
    12: ("vias_without_via_types", "Vias without Via Types Assigned",
         "check_vias reports via spans that are not drill pairs of the stack."),
    13: ("absent_fonts", "Absent TTF in the system, that was replaced with the default one",
         "it depends on the fonts installed on the PC that opens the board."),
    18: ("unused_from_tos", "Unused From-Tos",
         "From-Tos are not read (none were found in any board checked)."),
    20: ("mirrored_footprints", "Components with Mirrored Footprints",
         "it compares each footprint with its source PcbLib, which is not read; Altium "
         "turns it off by default."),
}
_ORDER = {key: rank for rank, key in enumerate(CHECKS)}
_SIGNAL_LAYER = re.compile(r"^(TOP|BOTTOM|MID\d+)$")
_ELECTRICAL = set(range(1, 33)) | set(range(39, 55)) | {74}
_MASK_LAYERS = {35, 36, 37, 38}
_KEEPOUT_LAYER = 56


# ---------- shapes ----------

def area(points: list[tuple[float, float]]) -> float:
    """The shoelace area (lobes of a self-crossing shape cancel)."""
    if len(points) < 3:
        return 0.0
    total = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def short_edges(vertices, limit: float = MICRO_SEGMENT_MIL) -> list[tuple]:
    """Edges shorter than limit: (length, (x1, y1), (x2, y2)). An arc edge measures its
    arc length; a repeated vertex is an edge of length 0."""
    edges = []
    count = len(vertices)
    if count < 2:
        return edges
    for index, vertex in enumerate(vertices):
        following = vertices[(index + 1) % count]
        if vertex.arc:
            _, _, radius, start, end = vertex.arc
            length = radius * math.radians((end - start) % 360.0 or 360.0)
        else:
            length = math.dist((vertex.x, vertex.y), (following.x, following.y))
            if length < 1e-9 and following.arc:
                continue  # the arc starts here: no edge in Altium's shape
        if length < limit:
            edges.append((length, (vertex.x, vertex.y), (following.x, following.y)))
    return edges


def _side(a, b, c, tolerance: float) -> int:
    """Which side of line a-b point c lies on, 0 within tolerance (a distance)."""
    length = math.dist(a, b)
    if length == 0:
        return 0
    value = ((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) / length
    return 0 if abs(value) < tolerance else (1 if value > 0 else -1)


def _on_segment(a, b, p, tolerance: float) -> bool:
    """p lies on segment a-b, away from its ends."""
    length = math.dist(a, b)
    if length < 2 * tolerance or _side(a, b, p, tolerance):
        return False
    along = ((p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])) / length
    return tolerance < along < length - tolerance


def _angle(origin, point) -> float:
    return math.atan2(point[1] - origin[1], point[0] - origin[0])


def _same_direction(first: float, second: float) -> bool:
    difference = abs(first - second) % (2 * math.pi)
    return min(difference, 2 * math.pi - difference) < 1e-6


def self_intersections(points, cap: int = SELF_INTERSECTION_MAX_POINTS,
                       tolerance: float = SNAP_MIL) -> list | None:
    """Points where the outline crosses itself: proper crossings of non-adjacent edges,
    a vertex the outline passes through another edge at, and a vertex visited twice
    with the two passes crossing. Touching without crossing, and crossings within
    tolerance of an edge's end (grid rounding), are not crossings. None when the shape
    has more than cap points."""
    count = len(points)
    if count > cap:
        return None
    if count < 4:
        return []
    found = []
    edges = [(points[i], points[(i + 1) % count]) for i in range(count)]
    boxes = [(min(a[0], b[0]) - tolerance, min(a[1], b[1]) - tolerance,
              max(a[0], b[0]) + tolerance, max(a[1], b[1]) + tolerance) for a, b in edges]
    # Only edges whose boxes overlap can meet: sort by left edge and sweep.
    order = sorted(range(count), key=lambda index: boxes[index][0])
    candidates = []
    active: list[int] = []
    for i in order:
        left = boxes[i][0]
        active = [j for j in active if boxes[j][2] >= left]
        for j in active:
            if boxes[j][1] <= boxes[i][3] and boxes[i][1] <= boxes[j][3]:
                candidates.append((min(i, j), max(i, j)))
        active.append(i)
    for i, j in candidates:
        if j - i < 2 or (i == 0 and j == count - 1):
            continue  # neighbours share a vertex
        (a, b), (c, d) = edges[i], edges[j]
        if _side(a, b, c, tolerance) * _side(a, b, d, tolerance) < 0 \
                and _side(c, d, a, tolerance) * _side(c, d, b, tolerance) < 0:
            denominator = (a[0] - b[0]) * (c[1] - d[1]) - (a[1] - b[1]) * (c[0] - d[0])
            t = ((a[0] - c[0]) * (c[1] - d[1]) - (a[1] - c[1]) * (c[0] - d[0])) / denominator
            found.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
    # A vertex the outline passes through another edge at: test each vertex against
    # the edges whose box holds it. Points on the edge's line (a run along the edge, or
    # points within tolerance of it) are walked past to where the outline leaves it,
    # and the crossing is counted once, at the vertex where the outline arrives.
    def leaving_side(c, d, k, step):
        for offset in range(1, count):
            side = _side(c, d, points[(k + step * offset) % count], tolerance)
            if side:
                return side
        return 0

    for i, j in candidates:
        for edge, other in ((i, j), (j, i)):
            for k in (other, (other + 1) % count):
                if edge in (k, (k - 1) % count):
                    continue
                here = points[k]
                c, d = edges[edge]
                if not _on_segment(c, d, here, tolerance) or _side(c, d, points[k - 1], tolerance) == 0:
                    continue
                if _side(c, d, points[k - 1], tolerance) * leaving_side(c, d, k, 1) < 0:
                    found.append(here)
    # A point the outline visits twice: a crossing when the two passes interleave
    # around it, not when they only touch.
    seen: dict[tuple[int, int], list[int]] = defaultdict(list)
    for k, here in enumerate(points):
        seen[(round(here[0] / tolerance), round(here[1] / tolerance))].append(k)
    for group in seen.values():
        for first_index in range(len(group)):
            for second_index in range(first_index + 1, len(group)):
                k, m = group[first_index], group[second_index]
                if m - k < 2 or (k == 0 and m == count - 1):
                    continue
                here = points[k]
                first = sorted((_angle(here, points[k - 1]), _angle(here, points[(k + 1) % count])))
                second = [_angle(here, points[m - 1]), _angle(here, points[(m + 1) % count])]
                if any(_same_direction(a, b) for a in first for b in second):
                    continue  # the passes share a ray (spike, slit, keyhole): a touch
                inside = [first[0] < angle < first[1] for angle in second]
                if inside[0] != inside[1]:
                    found.append(here)
    return list(dict.fromkeys(found))


# ---------- the board ----------

class _Board:
    def __init__(self, pcb):
        self.pcb = pcb
        board = pcb.board
        self.origin = (board.origin_x or 0.0, board.origin_y or 0.0)
        raw = pcb.raw or {}
        # ShapeBasedRegions6 keeps arcs; Regions6 (read only without it) flattens them.
        self.shape_based = "ShapeBasedRegions6/Data" in raw
        self.region_stream = self.shape_based or "Regions6/Data" in raw
        data = raw.get("ShapeBasedRegions6/Data") or raw.get("Regions6/Data") or b""
        self.region_bytes = len(data)
        self.regions, bad_regions = decode_region_records(data, self.shape_based)
        self.board_regions, bad_board = decode_region_records(raw.get("BoardRegions/Data", b""), False)
        self.fills, bad_fills = decode_fill_records(raw.get("Fills6/Data", b""))
        self.unread = {"region": bad_regions, "board region": bad_board, "fill": bad_fills}
        self.designators = {
            component.index: component.designator or component.refdes or f"#{component.index}"
            for component in pcb.components
        }
        self.layer_names = {layer.v6_id: layer.name for layer in board.layers}

    def at(self, x: float, y: float) -> list[float]:
        return [round(x - self.origin[0], 3), round(y - self.origin[1], 3)]

    def layer(self, v6_id: int | None) -> str:
        name = self.layer_names.get(v6_id)
        return name or (f"layer {v6_id}" if v6_id is not None else "no layer")

    def net(self, index: int | None) -> str | None:
        return self.pcb.nets[index] if index is not None and index < len(self.pcb.nets) else None

    def region_label(self, region) -> str:
        owner = self.designators.get(region.component)
        what = "Board cutout" if region.board_cutout else "Region"
        parts = [what, f"on {self.layer(region.layer)}"]
        if region.name:
            parts.insert(1, f"'{region.name}'")
        if owner:
            parts.append(f"in {owner}")
        return " ".join(parts)

    def pour_label(self, pour) -> str:
        net = self.net(pour.net)
        return f"Polygon on {self.layer(pour.layer)}" + (f" ({net})" if net else " (no net)")


def _region_category(region) -> str:
    if region.board_cutout or region.keepout or region.layer in _MASK_LAYERS \
            or region.layer == _KEEPOUT_LAYER:
        return "micro_segments_board"
    if region.layer in _ELECTRICAL:
        return "micro_segments_copper"
    return "micro_segments_graphics"


def _bounds(box, x0, y0, x1, y1):
    if box is None:
        return [x0, y0, x1, y1]
    box[0], box[1] = min(box[0], x0), min(box[1], y0)
    box[2], box[3] = max(box[2], x1), max(box[3], y1)
    return box


def _rect_extent(width, height, rotation) -> tuple[float, float]:
    theta = math.radians(rotation or 0.0)
    half_w, half_h = width / 2.0, height / 2.0
    return (abs(half_w * math.cos(theta)) + abs(half_h * math.sin(theta)),
            abs(half_w * math.sin(theta)) + abs(half_h * math.cos(theta)))


def _component_boxes(board: _Board) -> dict[int, list[float]]:
    """Each component's area: the bounding box of its pads, tracks, arcs, fills and
    regions, texts left out (this reproduced Altium's 'Reference Point Outside the
    Component Area' rows on every saved report checked)."""
    boxes: dict[int, list[float]] = {}
    for prim in board.pcb.primitives:
        if prim.component is None or prim.pour_id is not None:
            continue
        if prim.kind == "pad":
            dx, dy = _rect_extent(prim.width, prim.height or prim.width, prim.rotation)
            box = (prim.x1 - dx, prim.y1 - dy, prim.x1 + dx, prim.y1 + dy)
        elif prim.kind in ("track", "arc"):
            half = max(prim.width or 0.0, 0.0) / 2.0
            box = (min(prim.x1, prim.x2) - half, min(prim.y1, prim.y2) - half,
                   max(prim.x1, prim.x2) + half, max(prim.y1, prim.y2) + half)
        elif prim.kind == "via":
            half = (prim.width or 0.0) / 2.0
            box = (prim.x1 - half, prim.y1 - half, prim.x1 + half, prim.y1 + half)
        else:
            continue
        boxes[prim.component] = _bounds(boxes.get(prim.component), *box)
    for fill in board.fills:
        if fill.component is None:
            continue
        dx, dy = _rect_extent(abs(fill.x2 - fill.x1), abs(fill.y2 - fill.y1), fill.rotation)
        cx, cy = (fill.x1 + fill.x2) / 2.0, (fill.y1 + fill.y2) / 2.0
        boxes[fill.component] = _bounds(boxes.get(fill.component), cx - dx, cy - dy, cx + dx, cy + dy)
    for region in board.regions:
        if region.component is None or region.polygon is not None:
            continue
        points = contour(region.vertices)
        if points:
            xs, ys = [p[0] for p in points], [p[1] for p in points]
            boxes[region.component] = _bounds(
                boxes.get(region.component), min(xs), min(ys), max(xs), max(ys))
    return boxes


def _board_shape(board: _Board) -> tuple[list[list[tuple[float, float]]], str]:
    """The board's shape as polygons, and where it came from."""
    # Fine chords on curved edges, so a part on or just inside one is on the board.
    shapes = [contour(region.vertices, max_sagitta=SNAP_MIL) for region in board.board_regions]
    shapes = [shape for shape in shapes if len(shape) >= 3]
    if shapes:
        return shapes, "board regions"
    outline = (contour(board.pcb.board.outline_shape, max_sagitta=SNAP_MIL)
               or list(board.pcb.board.outline_vertices))
    return ([outline], "board outline") if len(outline) >= 3 else ([], "none")


# ---------- checks ----------

def _check_all(board: _Board, wanted: set[str]) -> tuple[dict[str, list[dict]], list[str]]:
    """Items per check: {"label", "at": (x, y) | None, **fields}."""
    pcb = board.pcb
    items: dict[str, list[dict]] = defaultdict(list)
    notes: list[str] = []

    def add(check, label, at=None, weight=1, **fields):
        """weight: how many rows Altium writes for the object (one per short segment
        or crossing point for the region checks)."""
        if check in wanted:
            items[check].append({"label": label, "at": at, "weight": weight, **fields})

    for pour in pcb.pours:
        centre = _centre(pour.vertices)
        if pour.shelved:
            add("shelved_modified_polygons", f"{board.pour_label(pour)} is shelved", centre,
                state="shelved")
        elif pour.copper_invalid:
            add("shelved_modified_polygons",
                f"{board.pour_label(pour)} was modified and not repoured", centre,
                state="modified")
        if area(contour(pour.shape) or pour.vertices) < ZERO_AREA_SQ_MIL:
            add("zero_area_polygons", board.pour_label(pour), centre)

    seen_text: set[str] = set()
    seen_uid: set[str] = set()
    for component in pcb.components:
        label = board.designators[component.index]
        at = (component.x, component.y) if component.x is not None else None
        uid = (component.source_unique_id or "").rsplit("@", 1)[0]
        text = component.designator or ""
        if text and not (uid and uid in seen_uid):
            if text in seen_text:
                add("duplicate_designators", label, at, footprint=component.footprint)
            seen_text.add(text)
        if uid:
            seen_uid.add(uid)
        if component.rotation is not None and abs(component.rotation - 360.0) < ROTATION_360_TOLERANCE:
            add("rotation_360", label, at)
        if component.layer_name and not _SIGNAL_LAYER.match(component.layer_name):
            add("non_signal_layer_components", f"{label} on {component.layer_name}", at)

    if "reference_point_outside" in wanted:
        boxes = _component_boxes(board)
        for component in pcb.components:
            box = boxes.get(component.index)
            if box is None or component.x is None or component.y is None:
                continue
            gap = max(box[0] - component.x, component.x - box[2],
                      box[1] - component.y, component.y - box[3])
            if gap > 1e-6:
                add("reference_point_outside", board.designators[component.index],
                    (component.x, component.y), distance_mil=round(gap, 2))

    for region in board.regions:
        if region.polygon is not None:
            continue  # pour copper: Altium checks only user regions
        points = contour(region.vertices)
        label = board.region_label(region)
        crossings = self_intersections(points)
        if crossings is None:
            notes.append(f"{label}: too many vertices ({len(points)}) to test for "
                         "self-intersections.")
        elif crossings:
            add("self_intersecting_regions", label, crossings[0], weight=len(crossings),
                crossings=len(crossings))
        # A self-crossing shape's lobes cancel in the shoelace area; it is not empty.
        if area(points) < ZERO_AREA_SQ_MIL and not crossings:
            add("zero_area_regions", label, _centre(points))
        # Regions6 (read only without ShapeBasedRegions6) stores arcs as short chords:
        # its edges are no measure of Altium's micro-segments.
        short = short_edges(region.vertices) if board.shape_based else []
        if short:
            add(_region_category(region), label, short[0][1], weight=len(short),
                segments=len(short), shortest_um=round(min(edge[0] for edge in short) * 25.4, 4))
    if not board.region_stream:
        notes.append("The PcbDoc has no region stream: region checks found nothing to check.")
    elif not board.region_bytes:
        notes.append("The board has no regions.")
    elif not board.shape_based:
        notes.append("Regions were read from Regions6, where arcs are stored as short "
                     "chords: micro-segments were not checked.")
    unread = [f"{count} {kind} records" for kind, count in board.unread.items() if count]
    if unread:
        notes.append(f"Could not read {', '.join(unread)}: the checks that use them may be "
                     "incomplete.")

    outline_short = short_edges(pcb.board.outline_shape)
    for length, start, _ in outline_short:
        add("outline_micro_segments", "Board outline edge", start,
            length_um=round(length * 25.4, 4))

    pads = defaultdict(set)
    for prim in pcb.primitives:
        if prim.kind != "pad":
            continue
        if prim.component is not None:
            pads[board.designators.get(prim.component)].add(prim.pad_name)
        if prim.hole_shape in (1, 2) and prim.slot_length is not None and prim.hole \
                and prim.slot_length < prim.hole:
            owner = board.designators.get(prim.component)
            add("slot_holes", f"{owner}-{prim.pad_name}" if owner else f"Pad {prim.pad_name}",
                (prim.x1, prim.y1), hole_mil=round(prim.hole, 3),
                slot_length_mil=round(prim.slot_length, 3))

    for xsignal in pcb.xsignals:
        missing = [pin for pin in xsignal.pins
                   if not (len(pin.rsplit("-", 1)) == 2
                           and pin.rsplit("-", 1)[1] in pads.get(pin.rsplit("-", 1)[0], ()))]
        if missing:
            add("broken_xsignals", xsignal.name, None, missing_pins=missing[:10])
    if "unused_xsignals" in wanted and pcb.xsignals:
        used = _used_xsignals(pcb)
        for xsignal in pcb.xsignals:
            if used is not True and xsignal.name.lower() not in used:
                add("unused_xsignals", xsignal.name)

    names: dict[str, int] = defaultdict(int)
    for rule in pcb.rules:
        names[(rule.name or "").strip().lower()] += 1
    for rule in pcb.rules:
        key = (rule.name or "").strip().lower()
        if getattr(rule, "unnamed", False) or not key:
            add("invalid_rules", f"A {rule.kind} rule has no name", kind=rule.kind)
            continue
        elif names[key] > 1:
            add("invalid_rules", f"Rule name '{rule.name}' is used by {names[key]} rules",
                kind=rule.kind)
            names[key] = 0  # report each name once

    if "components_outside_board" in wanted:
        _outside_board(board, add, notes)
    if "duplicate_copper" in wanted:
        _duplicate_copper(board, add)
    return items, notes


def _centre(points) -> tuple[float, float] | None:
    if not points:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)


_SCOPE_REF = re.compile(r"\b(InxSignal|IsxSignal|InxSignalClass|IsxSignalClass)\s*\(\s*'([^']*)'",
                        re.IGNORECASE)


def _used_xsignals(pcb):
    """Lower-case names of the xSignals a rule scope uses, or True for all of them
    (InAnyxSignal, or the class 'All xSignals')."""
    used: set[str] = set()
    classes = {cls.name.lower(): cls for cls in pcb.classes if cls.kind == "xsignal"}
    for rule in pcb.rules:
        for scope in (rule.scope1, rule.scope2):
            text = scope or ""
            if re.search(r"\bInAnyxSignal\b", text, re.IGNORECASE):
                return True
            for function, name in _SCOPE_REF.findall(text):
                if "class" in function.lower():
                    if name.lower() == "all xsignals":
                        return True
                    cls = classes.get(name.lower())
                    used.update(member.lower() for member in (cls.members if cls else []))
                else:
                    used.add(name.lower())
    return used


def _on_boundary(x: float, y: float, shape, tolerance: float = SNAP_MIL) -> bool:
    return any(point_seg_distance(x, y, *a, *b) < tolerance
               for a, b in zip(shape, shape[1:] + shape[:1]))


def _on_arc_edge(x: float, y: float, vertices) -> bool:
    """(x, y) lies on one of the shape's arc edges, or between it and the chords that
    stand in for it (a large arc at MAX_ARC_CHORDS chords cuts more than SNAP_MIL)."""
    for vertex in vertices:
        if not vertex.arc:
            continue
        cx, cy, radius, start, end = vertex.arc
        sweep = (end - start) % 360.0 or 360.0
        sagitta = radius * (1 - math.cos(math.radians(sweep / MAX_ARC_CHORDS) / 2))
        distance = math.dist((x, y), (cx, cy))
        if not radius - max(sagitta, SNAP_MIL) - SNAP_MIL <= distance <= radius + SNAP_MIL:
            continue
        if (math.degrees(math.atan2(y - cy, x - cx)) - start) % 360.0 <= sweep:
            return True
    return False


def _outside_board(board: _Board, add, notes: list[str]) -> None:
    shapes, source = _board_shape(board)
    curved = board.pcb.board.outline_shape if source == "board outline" else []
    if not shapes:
        notes.append("The board has no outline: components outside the board were not checked.")
        return
    netted = defaultdict(bool)
    for prim in board.pcb.primitives:
        if prim.kind == "pad" and prim.component is not None and prim.net is not None:
            netted[prim.component] = True
    for component in board.pcb.components:
        if component.x is None or component.y is None:
            continue
        x, y = component.x, component.y
        if any(point_in_polygon(x, y, shape) or _on_boundary(x, y, shape) for shape in shapes):
            continue
        if _on_arc_edge(x, y, curved):
            continue  # on the edge counts as on the board (edge-launch parts)
        add("components_outside_board", board.designators[component.index],
            (component.x, component.y), severity="warning" if netted[component.index] else "info",
            with_nets=netted[component.index], shape=source)


def _duplicate_copper(board: _Board, add) -> None:
    seen: dict[tuple, int] = {}
    for prim in board.pcb.primitives:
        if prim.net is None or prim.poured_by is not None or prim.pour_id is not None:
            continue
        if prim.kind == "track" and prim.layer in _ELECTRICAL:
            ends = tuple(sorted(((round(prim.x1, 4), round(prim.y1, 4)),
                                 (round(prim.x2, 4), round(prim.y2, 4)))))
            key = ("track", prim.layer, prim.net, round(prim.width, 4), ends)
        elif prim.kind == "via":
            key = ("via", prim.net, prim.layer, prim.layer_to,
                   round(prim.x1, 4), round(prim.y1, 4))
        else:
            continue
        seen[key] = seen.get(key, 0) + 1
        if seen[key] == 2:
            what = "Track" if key[0] == "track" else "Via"
            where = f" on {board.layer(prim.layer)}" if key[0] == "track" else ""
            add("duplicate_copper", f"{what}{where} ({board.net(prim.net)})", (prim.x1, prim.y1),
                object=key[0])


# ---------- result ----------

def _fmt(value_mil: float, to_mm: bool) -> str:
    """A length in the output unit with fixed decimals (0.01 mil, 0.0001 mm)."""
    text = f"{value_mil * 0.0254:.4f}" if to_mm else f"{value_mil:.2f}"
    text = text.rstrip("0").rstrip(".") if "." in text else text
    return f"{'0' if text in ('-0', '') else text} {'mm' if to_mm else 'mil'}"


def _detail(member: dict, to_mm: bool) -> str:
    if "distance_mil" in member:
        return f", {_fmt(member['distance_mil'], to_mm)} outside its pads and outline"
    if "shortest_um" in member:
        return (f", {member['segments']} edge(s) shorter than {_fmt(MICRO_SEGMENT_MIL, to_mm)} "
                f"({MICRO_SEGMENT_MIL * 25.4:.3f} um), the shortest {member['shortest_um']} um")
    if "length_um" in member:
        return f", {member['length_um']} um long"
    if "slot_length_mil" in member:
        return (f", slot length {_fmt(member['slot_length_mil'], to_mm)} under the hole size "
                f"{_fmt(member['hole_mil'], to_mm)}")
    if "missing_pins" in member:
        return f", pins not on the board: {', '.join(member['missing_pins'])}"
    if "crossings" in member:
        return f", {member['crossings']} crossing point(s)"
    return ""


def _findings(board: _Board, check: str, items: list[dict], to_mm: bool) -> list[dict]:
    altium_id, category, group, level = CHECKS[check]
    out = []
    by_severity: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        severity = item.pop("severity", None) or LEVEL_SEVERITY.get(level, "info")
        by_severity[severity].append(item)
    for severity, members in by_severity.items():
        base = {"check": check, "severity": severity, "category": category}
        if altium_id is not None:
            base["altium_id"] = altium_id
        if len(members) > GROUP_ABOVE:
            objects = []
            for member in members[:LISTED]:
                entry = {"object": member["label"]}
                if member.get("at") is not None:
                    entry["at_mil"] = board.at(*member["at"])
                objects.append(entry)
            out.append({
                **base, "count": len(members),
                "rows": sum(member["weight"] for member in members),
                "objects": objects,
                "issue": f"{category}: {len(members)} objects"
                + (f" (the first {LISTED} listed)." if len(members) > LISTED else "."),
            })
            continue
        for member in members:
            at = member.pop("at", None)
            label = member.pop("label")
            weight = member.pop("weight")
            finding = {**base, "object": label}
            if at is not None:
                finding["at_mil"] = board.at(*at)
            finding.update(member)
            finding["rows"] = weight
            where = (f" ({_fmt(at[0] - board.origin[0], to_mm)}, "
                     f"{_fmt(at[1] - board.origin[1], to_mm)})") if at is not None else ""
            finding["issue"] = f"{category}: {label}{where}{_detail(member, to_mm)}."
            out.append(finding)
    return out


def run_health_check(
    pcb,
    checks: list[str] | None = None,
    to_mm: bool = False,
    offset: int = 0,
    limit: int = FINDING_LIMIT,
    report_health: dict[str, int] | None = None,
) -> dict:
    """The health findings for a parsed board (PcbData). report_health holds the
    category counts of the last saved Batch DRC report's health section, if any.

    summary counts what Altium's report would count per check: one row per object,
    except micro-segments (one per short edge) and self-intersections (one per
    crossing point); severity_counts counts findings."""
    requested = [check.strip().lower() for check in checks or [] if check.strip()]
    known = set(CHECKS) | {entry[0] for entry in NOT_CHECKED.values()}
    invalid = [check for check in requested if check not in known]
    if invalid:
        return {"error": "invalid_checks",
                "message": f"Unknown checks {invalid}.", "valid_checks": sorted(known)}
    board = _Board(pcb)
    settings = pcb.board.health_checks or {}
    disabled = {key for key, entry in CHECKS.items()
                if entry[0] is not None and settings.get(entry[0]) is False}
    wanted = set(requested) if requested else set(CHECKS) - OPT_IN - disabled
    items, notes = _check_all(board, wanted & set(CHECKS))
    counts = {
        check: sum(item["weight"] for item in items.get(check, []))
        for check in sorted(wanted & set(CHECKS), key=_ORDER.get)
    }
    findings: list[dict] = []
    for check in sorted(wanted & set(CHECKS), key=_ORDER.get):
        findings += _findings(board, check, items.get(check, []), to_mm)
    findings.sort(key=lambda f: (_SEVERITY_RANK[f["severity"]], _ORDER[f["check"]]))
    not_checked = {
        key: reason for key, _, reason in NOT_CHECKED.values()
        if not requested or key in requested
    }
    for key in sorted(disabled - set(requested), key=_ORDER.get):
        not_checked[key] = "turned off in this board's Health Check settings."
    if not requested:
        for key in OPT_IN:
            not_checked[key] = "Altium runs it only in debug mode; name it in checks to run it."
    offset = max(0, int(offset or 0))
    result: dict = {
        "checks_run": sorted(wanted & set(CHECKS), key=_ORDER.get),
        "summary": {check: count for check, count in counts.items() if count},
        "severity_counts": {level: sum(1 for f in findings if f["severity"] == level)
                            for level in ("error", "warning", "info")},
        "findings": findings[offset:offset + limit],
        "total": len(findings),
        "offset": offset,
        "has_more": offset + limit < len(findings),
    }
    if result["has_more"]:
        result["next_offset"] = offset + limit
    if not_checked:
        result["not_checked"] = not_checked
    if pcb.board.health_checks is not None:
        result["board_settings"] = (
            "The board stores Health Check settings"
            + (f"; turned off: {sorted(disabled)}" if disabled else "; every check is on")
            + "."
        )
    if report_health is not None:
        result["altium_report"] = _compare(report_health, counts)
    notes.append(
        "Coordinates are relative to the board origin. Altium also runs these checks when "
        "the board loads (all but Components with Mirrored Footprints), and a shelved or "
        "modified polygon stops output generation with a warning."
    )
    result["notes"] = notes
    return result


def _compare(report_health: dict[str, int], counts: dict[str, int]) -> dict:
    """The saved report's health counts next to this check's, per Altium category."""
    by_category = {CHECKS[key][1].lower(): key for key in CHECKS if CHECKS[key][0] is not None}
    rows = []
    for title, count in sorted(report_health.items()):
        key = by_category.get(title.strip().lower())
        rows.append({"category": title, "report": count,
                     "offline": counts.get(key) if key else None})
    return {
        "categories": rows,
        "note": "The report lists only categories with issues; a category it leaves out "
                "had none or was not run.",
    }
