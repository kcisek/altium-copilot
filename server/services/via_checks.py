"""Via inventory and fabrication review over a PcbIndex.

Pure logic, no MCP. Lengths are mils under *_mil keys, so callers can convert units;
issue texts are written in the units asked for.

The inventory is what a fabricator asks about the vias: types by span (first and last
copper layer in stack order), hole as drawn (Altium's finished hole) and pad diameter;
counts per type and per net class; depth and aspect ratio from the stack's copper and
dielectric heights (copper to copper, no solder mask); the smallest annular ring over the
layers that keep a pad; tenting; vias in SMD pads; drill pairs; and back drilling.

A microvia follows IPC-T-50 / IPC-2226A: a blind or buried via over one dielectric, no
deeper than 0.25 mm from the capture land's foil to the target land, with an aspect ratio
of at most 1:1 or a hole of at most 0.15 mm (a laser via that is too deep is still a
microvia, and its aspect ratio is flagged). Without stack heights only the hole decides.

Back drilling follows Altium's Controlled Depth Drilling: the Layer Stack Manager's back
drills (Board6 LAYERPAIRn with BACKDRILL=TRUE) are drilled from their outer layer and
stop before the other layer; the Max Via Stub Length (Back Drilling) rule selects
candidate vias, and a candidate is drilled by a pair only when it has no connection on
the layers that pair drills. Altium computes this when it writes the drill files, so it
is recomputed here. Connections are same-net tracks, arcs and SMD pads touching the
via's pad on a layer (its hole where the pad is removed), same-net pours whose outline
holds the via, and same-net vias stacked on it. Internal planes, fills and regions are
not read: a candidate with no connection found is reported, not judged. A stub runs from
the face of the last connected copper layer to the end of the barrel. After back
drilling the drill tip stops somewhere in the dielectric before the stop layer, so the
residual stub is a range: stub_min_mil if it reaches the stop layer, stub_mil if it only
just clears the first drilled layer; stub_mil is compared with the rule.

Default limits: aspect ratio warns above 10:1 and notes above 8:1 for drilled vias
(depth over the hole as drawn; a fabricator drilling larger gets a lower ratio), notes a
mechanically drilled blind via above 1:1 (controlled-depth drilling); microvias warn
above 1:1 and note above 0.8:1. The annular ring is (pad - hole) / 2 on the finished
hole like Altium's Minimum Annular Ring rule, checked against that rule and the
requested minimum, else noted below 0.1 mm (0.05 mm for microvias). Stubs on
differential-pair nets (and the nets asked for) that no back-drill rule covers are noted
above 40 mil (1 mm), with their quarter-wave resonance.
"""

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field

try:
    from services.pcb_compliance import _Checker as RuleChecker
    from services.pcb_geometry import point_in_polygon, point_seg_distance
except ImportError:
    from server.services.pcb_compliance import _Checker as RuleChecker
    from server.services.pcb_geometry import point_in_polygon, point_seg_distance

CHECKS = (
    "aspect_ratio",
    "annular_ring",
    "via_style",
    "drill_pairs",
    "via_in_pad",
    "tenting",
    "back_drill",
)

EPS_MIL = 0.001
MM = 1 / 0.0254
MICROVIA_MAX_HOLE_MIL = 0.15 * MM
MICROVIA_MAX_DEPTH_MIL = 0.25 * MM
ASPECT_NOTE = 8.0
ASPECT_WARNING = 10.0
MICROVIA_ASPECT_NOTE = 0.8
MICROVIA_ASPECT_WARNING = 1.0
CONTROLLED_DEPTH_ASPECT = 1.0
DEFAULT_MIN_RING_MIL = 0.1 * MM
MICROVIA_MIN_RING_MIL = 0.05 * MM
DEFAULT_MAX_STUB_MIL = 40.0
# Solder mask does not reliably bridge larger holes; larger plated holes drawn as
# vias are mostly mounting holes.
TENT_MAX_HOLE_MIL = 0.5 * MM
LARGE_HOLE_MIL = 1.0 * MM
# A pad this large, holding this many vias or the footprint's own vias, is a
# thermal pad with a via array.
THERMAL_PAD_MIL = 2.0 * MM
THERMAL_VIAS = 4
SAMPLE = 5
DEFAULT_DK = 4.0
LIGHT_INCH_PER_NS = 11.8

_MODES = {0: "simple", 1: "top-middle-bottom", 2: "full stack"}
_SEVERITY_ORDER = {"warning": 0, "info": 1}
_KIND_ORDER = {"through": 0, "blind": 1, "buried": 2, "unknown": 3}


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


# ---------- stack ----------


class _Stack:
    """Copper layers (signal and plane) in stack order, with the depth (mil,
    from the top copper's top face) of each layer's top and bottom face."""

    def __init__(self, index):
        self.index = index
        self.order = index._copper_layers_in_stack_order()
        # Legacy stackups list layers by id, not in stack order.
        self.legacy = getattr(index.pcb.board, "stackup_source", "v9") != "v9"
        self.position = {layer: number for number, layer in enumerate(self.order)}
        self.last = len(self.order) - 1
        self.top: dict[int, float] | None = None
        self.bottom: dict[int, float] | None = None
        self.thickness_mil: float | None = None
        self.dielectrics: list[tuple[float, float, float | None]] = []
        rows = index.db.execute(
            "SELECT v6_id, kind, copper_thick_mil, diel_height_mil, diel_const FROM layers "
            "WHERE stack_order IS NOT NULL ORDER BY stack_order"
        ).fetchall()
        copper = [number for number, row in enumerate(rows) if row["kind"] == "copper"]
        if not copper:
            return
        top: dict[int, float] = {}
        bottom: dict[int, float] = {}
        dielectrics = []
        depth = 0.0
        for row in rows[copper[0] : copper[-1] + 1]:
            is_copper = row["kind"] == "copper"
            thickness = row["copper_thick_mil"] if is_copper else row["diel_height_mil"]
            if thickness is None:
                return
            if is_copper and row["v6_id"] is not None:
                top[row["v6_id"]] = depth
                bottom[row["v6_id"]] = depth + thickness
            elif not is_copper:
                dielectrics.append((depth, depth + thickness, row["diel_const"]))
            depth += thickness
        self.top, self.bottom, self.thickness_mil = top, bottom, depth
        self.dielectrics = dielectrics

    @property
    def has_heights(self) -> bool:
        return self.top is not None

    def name(self, position: int) -> str:
        return self.index.layer_name(self.order[position])

    def top_face(self, position: int) -> float:
        return self.top[self.order[position]]

    def bottom_face(self, position: int) -> float:
        return self.bottom[self.order[position]]

    def span_depth(self, low: int, high: int) -> float | None:
        """Copper and dielectric from the low layer's top face to the high
        layer's bottom face."""
        if not self.has_heights:
            return None
        return self.bottom_face(high) - self.top_face(low)

    def from_top(self, low: int, high: int) -> bool:
        """Is a one-hop via drilled from the top side (nearer the top)?"""
        return low == 0 or (high != self.last and low <= self.last - high)

    def capture_depth(self, low: int, high: int) -> float | None:
        """A microvia's depth: capture land foil plus dielectric."""
        if not self.has_heights:
            return None
        if self.from_top(low, high):
            return self.top_face(high) - self.top_face(low)
        return self.bottom_face(high) - self.bottom_face(low)

    def dk_between(self, start: float, end: float) -> float:
        """Thickness-weighted Dk of the dielectric between two depths."""
        low, high = sorted((start, end))
        weight = total = 0.0
        for top, bottom, dk in self.dielectrics:
            overlap = min(high, bottom) - max(low, top)
            if overlap > 0 and dk:
                weight += overlap
                total += overlap * dk
        return total / weight if weight else DEFAULT_DK

    def resolve(self, token: str) -> int | None:
        """A stored layer token (TOP, MID4, BOTTOM, PLANE2 or a layer name) as a
        stack position."""
        text = (token or "").strip().lower()
        if text == "top":
            layer = 1
        elif text == "bottom":
            layer = 32
        elif text.startswith("mid") and text[3:].isdigit():
            layer = int(text[3:]) + 1
        elif text.startswith("plane") and text[5:].isdigit():
            layer = int(text[5:]) + 38
        else:
            layer = self.index.rule_context.layer_ids.get(text)
        return self.position.get(layer)


# ---------- vias ----------


@dataclass
class _Via:
    prim: object
    net: str | None
    low: int | None  # stack positions
    high: int | None
    kind: str  # through | blind | buried | unknown
    microvia: bool
    ring_mil: float
    type_key: tuple
    last: int = 0  # the stack's last position
    type_id: str = ""
    connected: set | None = None
    drilled: list = field(default_factory=list)  # back-drill labels
    drill_holes: dict = field(default_factory=dict)  # label -> back-drill diameter
    pad_sides: set = field(default_factory=set)  # v6 layers of SMD pads it sits in
    facts: object = None

    @property
    def stack(self):
        return self.prim.via

    @property
    def outer_sides(self) -> tuple:
        """The board faces the barrel reaches."""
        if self.low is None:
            return ("top", "bottom")
        return tuple(
            side
            for side, reached in (("top", self.low == 0), ("bottom", self.high == self.last))
            if reached
        )

    def row(self) -> dict:
        prim = self.prim
        return {
            "kind": "via",
            "component": prim.component,
            "x1": prim.x1,
            "y1": prim.y1,
            "x2": prim.x2,
            "y2": prim.y2,
            "layer": prim.layer,
            "layer_to": prim.layer_to,
            "pad_name": None,
            "hole": prim.hole,
            "width": prim.width,
        }

    def pad_radius(self, layer: int) -> float:
        """Radius of the via's copper on a layer: its pad, or its hole where
        the pad is removed."""
        prim = self.prim
        diameter = (
            self.stack.pad_diameter(layer, prim.width)
            if self.stack is not None
            else prim.width
        )
        return (diameter if diameter else prim.hole or 0.0) / 2.0

    def reach(self) -> float:
        """Largest copper radius on any layer."""
        sizes = [self.prim.width]
        if self.stack is not None and self.stack.layer_diameters:
            sizes.extend(self.stack.layer_diameters)
        return max(sizes) / 2.0

    def tented(self, side: str) -> bool | None:
        if self.stack is None:
            return None
        return self.stack.tent_top if side == "top" else self.stack.tent_bottom

    def test_point(self) -> bool:
        stack = self.stack
        return bool(stack and (stack.test_fab_top or stack.test_fab_bottom))


class _Review:
    def __init__(self, index, selection: set[str] | None, limits: dict, checks: set,
                 units: str):
        self.index = index
        self.stack = _Stack(index)
        self.selection = selection
        self.limits = limits
        self.checks = checks
        self.units = units
        self.checker = RuleChecker(index, 1)
        self.findings: list[dict] = []
        self.notes: list[str] = []
        self.undecided: Counter = Counter()
        self._enabled_rules: dict[str, list] = {}
        self.stacked: list[_Via] = []
        self.pad_hits: dict[int, dict] = {}
        self.vias = self._collect()
        self.types = self._types()

    # --- formatting ---

    def _len(self, mil: float, digits: int = 2) -> str:
        if self.units == "mm":
            return f"{mil * 0.0254:.{max(digits, 3)}f} mm"
        return f"{mil:.{digits}f} mil"

    # --- collection ---

    def _collect(self) -> list[_Via]:
        nets = self.index.pcb.nets
        stack = self.stack
        vias = []
        for prim in self.index.pcb.primitives:
            if prim.kind != "via":
                continue
            net = nets[prim.net] if prim.net is not None and prim.net < len(nets) else None
            if self.selection is not None and (net is None or net.lower() not in self.selection):
                continue
            a, b = stack.position.get(prim.layer), stack.position.get(prim.layer_to)
            hole = prim.hole or 0.0
            if a is None or b is None:
                low = high = None
                kind = "unknown"
                key_span = tuple(sorted((prim.layer, prim.layer_to)))
            else:
                low, high = sorted((a, b))
                if low == 0 and high == stack.last:
                    kind = "through"
                elif low == 0 or high == stack.last:
                    kind = "blind"
                else:
                    kind = "buried"
                key_span = (stack.order[low], stack.order[high])
            via = _Via(
                prim=prim,
                net=net,
                low=low,
                high=high,
                kind=kind,
                microvia=self._is_microvia(kind, low, high, hole),
                ring_mil=0.0,
                type_key=(_KIND_ORDER[kind], key_span, round(hole, 3), round(prim.width, 3)),
                last=stack.last,
            )
            via.ring_mil = self._ring(via)
            vias.append(via)
        return vias

    def _is_microvia(self, kind: str, low, high, hole: float) -> bool:
        if kind not in ("blind", "buried") or high - low != 1 or hole <= 0:
            return False
        depth = self.stack.capture_depth(low, high)
        if depth is None:
            return hole <= MICROVIA_MAX_HOLE_MIL + EPS_MIL
        return depth <= MICROVIA_MAX_DEPTH_MIL + EPS_MIL and (
            hole <= MICROVIA_MAX_HOLE_MIL + EPS_MIL or depth / hole <= 1.0 + 1e-9
        )

    def _ring(self, via: _Via) -> float:
        """Smallest (pad - hole) / 2 over the signal layers in the span that
        keep a pad; the via's diameter when no layer is known."""
        prim = via.prim
        hole = prim.hole or 0.0
        layers = (
            [layer for layer in self.stack.order[via.low : via.high + 1] if 1 <= layer <= 32]
            if via.low is not None
            else []
        )
        sizes = []
        for layer in layers:
            diameter = (
                via.stack.pad_diameter(layer, prim.width)
                if via.stack is not None
                else prim.width
            )
            if diameter:
                sizes.append(diameter)
        return (min(sizes or [prim.width]) - hole) / 2.0

    def _span_label(self, via: _Via) -> str:
        if via.low is None:
            return (
                f"{self.index.layer_name(via.prim.layer)} -> "
                f"{self.index.layer_name(via.prim.layer_to)}"
            )
        return f"{self.stack.name(via.low)} -> {self.stack.name(via.high)}"

    def _depth(self, via: _Via) -> float | None:
        if via.low is None:
            return None
        if via.microvia:
            return self.stack.capture_depth(via.low, via.high)
        return self.stack.span_depth(via.low, via.high)

    def _types(self) -> dict[tuple, dict]:
        groups: dict[tuple, list[_Via]] = defaultdict(list)
        for via in self.vias:
            groups[via.type_key].append(via)
        types = {}
        for number, key in enumerate(sorted(groups), start=1):
            members = groups[key]
            first = members[0]
            type_id = f"V{number}"
            for via in members:
                via.type_id = type_id
            hole = first.prim.hole or 0.0
            depth = self._depth(first)
            types[key] = {
                "id": type_id,
                "vias": members,
                "span": self._span_label(first),
                "kind": first.kind,
                "microvia": first.microvia,
                "hole": hole,
                "diameter": first.prim.width,
                "depth": depth,
                "aspect": depth / hole if depth is not None and hole > 0 else None,
            }
        return types

    def _type_of(self, type_id: str) -> dict:
        return next(info for info in self.types.values() if info["id"] == type_id)

    # --- rules ---

    def _enabled(self, kind: str) -> list:
        if kind not in self._enabled_rules:
            self._enabled_rules[kind] = self.checker.enabled(kind)
        return self._enabled_rules[kind]

    def _rule(self, kind: str, via: _Via):
        if not self._enabled(kind):
            return None
        if via.facts is None:
            via.facts = self.index.object_facts(via.row())
        rule, undecided = self.checker.resolver(kind, via.net).resolve(via.facts)
        if undecided:
            self.undecided[kind] += 1
            return None
        return rule

    def _finding(self, check: str, severity: str, issue: str, **fields) -> None:
        if check not in self.checks:
            return
        record = {"check": check, "severity": severity}
        record.update({key: value for key, value in fields.items() if value is not None})
        record["issue"] = issue
        self.findings.append(record)

    # --- spatial ---

    def _near(self, x: float, y: float, reach: float, where: str, params: tuple):
        # CROSS JOIN keeps SQLite on the rtree: with a net filter it would
        # otherwise scan every primitive of the net for each via.
        if self.index._has_rtree:
            sql = (
                "SELECT p.* FROM prims_rtree r CROSS JOIN prims p ON p.id = r.id "
                "WHERE r.minx <= ? AND r.maxx >= ? AND r.miny <= ? AND r.maxy >= ? "
            )
        else:
            sql = (
                "SELECT p.* FROM prims p "
                "WHERE p.minx <= ? AND p.maxx >= ? AND p.miny <= ? AND p.maxy >= ? "
            )
        return self.index.db.execute(
            sql + where, (x + reach, x - reach, y + reach, y - reach, *params)
        ).fetchall()

    def _in_span(self, via: _Via, layer: int) -> int | None:
        position = self.stack.position.get(layer)
        if position is None or via.low is None or not via.low <= position <= via.high:
            return None
        return position

    def _designator(self, component) -> str | None:
        if component is None or not 0 <= component < len(self.index.pcb.components):
            return None
        return self.index.designator(component)

    # --- inventory ---

    def _tent_state(self, via: _Via) -> str:
        """tented, top_only, bottom_only, untented, in_pad, buried or unknown,
        judged on the board faces the via reaches outside SMD pads."""
        sides = via.outer_sides
        if not sides:
            return "buried"
        if via.pad_sides:
            return "in_pad"
        states = [via.tented(side) for side in sides]
        if None in states:
            return "unknown"
        if all(states):
            return "tented"
        if not any(states):
            return "untented"
        return "top_only" if via.tented("top") else "bottom_only"

    def inventory(self) -> dict:
        entries = []
        for info in self.types.values():
            members = info["vias"]
            nets = {via.net for via in members if via.net}
            tented = Counter(self._tent_state(via) for via in members)
            modes = Counter(
                _MODES.get(via.stack.mode, "simple") for via in members if via.stack
            )
            entry = {
                "type": info["id"],
                "span": info["span"],
                "kind": info["kind"],
                "hole_mil": _round(info["hole"]),
                "diameter_mil": _round(info["diameter"]),
                "count": len(members),
                "nets": len(nets),
            }
            if info["microvia"]:
                entry["microvia"] = True
            if info["hole"] >= LARGE_HOLE_MIL - EPS_MIL:
                entry["large_hole"] = True
            no_net = sum(1 for via in members if via.net is None)
            if no_net:
                entry["no_net"] = no_net
            entry["depth_mil"] = _round(info["depth"], 2)
            entry["aspect_ratio"] = _round(info["aspect"], 2)
            entry["min_annular_ring_mil"] = _round(min(via.ring_mil for via in members))
            entry["tented"] = dict(tented)
            if modes and set(modes) != {"simple"}:
                entry["pad_stack"] = dict(modes)
            test_points = sum(1 for via in members if via.test_point())
            if test_points:
                entry["test_points"] = test_points
            removed = sum(1 for via in members if via.stack and via.stack.removed_pads)
            if removed:
                entry["with_removed_pads"] = removed
            in_pad = sum(1 for via in members if via.pad_sides)
            if in_pad:
                entry["in_pad"] = in_pad
            drilled = sum(1 for via in members if via.drilled)
            if drilled:
                entry["back_drilled"] = drilled
            if info["kind"] in ("blind", "buried"):
                pair = self._pair_of(members[0])
                if pair is not None:
                    entry["drill_pair"] = pair
            entries.append(entry)
        entries.sort(key=lambda entry: (-entry["count"], entry["type"]))

        classes: dict[str, Counter] = defaultdict(Counter)
        for via in self.vias:
            if via.net is None:
                continue
            for name in self.index.net_classes_of(via.net):
                classes[name][via.type_id] += 1
        class_entries = sorted(
            (
                {
                    "class": name,
                    "vias": sum(counts.values()),
                    "types": dict(counts.most_common()),
                }
                for name, counts in classes.items()
            ),
            key=lambda entry: (-entry["vias"], entry["class"].lower()),
        )
        return {"via_types": entries, "net_classes": class_entries}

    def summary(self) -> dict:
        kinds = Counter(via.kind for via in self.vias)
        tented = Counter(self._tent_state(via) for via in self.vias)
        summary = {
            "vias": len(self.vias),
            "types": len(self.types),
            "through": kinds["through"],
            "blind": kinds["blind"],
            "buried": kinds["buried"],
            "microvias": sum(1 for via in self.vias if via.microvia),
            "no_net": sum(1 for via in self.vias if via.net is None),
            "tented": tented["tented"],
            "one_side_tented": tented["top_only"] + tented["bottom_only"],
            "untented": tented["untented"],
            "in_pad": tented["in_pad"],
            "back_drilled": sum(1 for via in self.vias if via.drilled),
        }
        if self.stacked:
            summary["stacked_microvias"] = len(self.stacked)
        if kinds["unknown"]:
            summary["unknown_span"] = kinds["unknown"]
        return summary

    def board(self) -> dict:
        board = {
            "copper_layers": len(self.stack.order),
            "stack_thickness_mil": _round(self.stack.thickness_mil, 2),
        }
        if not self.stack.has_heights:
            board["stack_heights"] = "missing: depths, aspect ratios and stubs not computed"
        if self.stack.legacy:
            board["stack_order"] = "legacy stackup: layer order assumed from layer ids"
        return board

    # --- drill pairs ---

    def drill_pairs(self) -> list[dict]:
        """Resolved Board6 drill pairs: {pair, low, high} (stack positions)."""
        if not hasattr(self, "_pairs"):
            self._pairs = []
            for pair in getattr(self.index.pcb.board, "drill_pairs", []):
                a, b = self.stack.resolve(pair.low), self.stack.resolve(pair.high)
                self._pairs.append(
                    {
                        "pair": pair,
                        "low": None if a is None or b is None else min(a, b),
                        "high": None if a is None or b is None else max(a, b),
                    }
                )
        return self._pairs

    def _pair_of(self, via: _Via) -> bool | None:
        pairs = [entry for entry in self.drill_pairs() if not entry["pair"].back_drill]
        if not pairs or via.low is None:
            return None
        return any(
            entry["low"] == via.low and entry["high"] == via.high for entry in pairs
        )

    def drill_pair_report(self) -> list[dict]:
        counts = Counter((via.low, via.high) for via in self.vias if via.low is not None)
        report = []
        for entry in self.drill_pairs():
            pair = entry["pair"]
            if pair.back_drill:
                continue
            if entry["low"] is None:
                report.append(
                    {"from_layer": pair.low, "to_layer": pair.high, "resolved": False}
                )
                continue
            report.append(
                {
                    "from_layer": self.stack.name(entry["low"]),
                    "to_layer": self.stack.name(entry["high"]),
                    "vias": counts[(entry["low"], entry["high"])],
                }
            )
        return report

    # --- checks ---

    def check_aspect(self) -> None:
        if not self.stack.has_heights:
            return
        explicit = self.limits["max_aspect_ratio"]
        warn = explicit or ASPECT_WARNING
        explicit_micro = self.limits["max_microvia_aspect_ratio"]
        micro_warn = explicit_micro or MICROVIA_ASPECT_WARNING
        for info in self.types.values():
            aspect = info["aspect"]
            if aspect is None:
                continue
            shown = round(aspect, 2)
            # Default thresholds compare the ratio to one decimal, as fabricators
            # quote it; a requested limit is compared as shown.
            judged = round(aspect, 1)
            fields = {
                "type": info["id"],
                "span": info["span"],
                "count": len(info["vias"]),
                "hole_mil": _round(info["hole"]),
                "depth_mil": _round(info["depth"], 2),
                "aspect_ratio": shown,
            }
            if info["microvia"]:
                if (shown if explicit_micro else judged) > micro_warn:
                    self._finding(
                        "aspect_ratio", "warning",
                        f"Microvia aspect ratio {shown:.2f}:1 is above {micro_warn:g}:1 "
                        "(capture foil and dielectric over the hole): laser vias this "
                        "deep plate poorly. Use a larger hole or a thinner dielectric.",
                        **fields,
                    )
                elif explicit_micro is None and judged > MICROVIA_ASPECT_NOTE:
                    self._finding(
                        "aspect_ratio", "info",
                        f"Microvia aspect ratio {shown:.2f}:1 is above the 0.8:1 many "
                        "fabricators prefer for copper-filled microvias.",
                        **fields,
                    )
                continue
            if (shown if explicit else judged) > warn:
                self._finding(
                    "aspect_ratio", "warning",
                    f"Aspect ratio {shown:.2f}:1 is above {warn:g}:1 (copper-to-copper "
                    "depth over the hole as drawn): beyond what most fabricators plate. "
                    "Use a larger hole or confirm the fabricator's capability.",
                    **fields,
                )
            elif explicit is None and judged > ASPECT_NOTE:
                self._finding(
                    "aspect_ratio", "info",
                    f"Aspect ratio {shown:.2f}:1 is above the 8:1 many fabricators quote "
                    "as standard (on the hole as drawn; a fabricator drilling larger gets "
                    "a lower ratio). Confirm with the fabricator.",
                    **fields,
                )
            elif info["kind"] == "blind" and judged > CONTROLLED_DEPTH_ASPECT:
                self._finding(
                    "aspect_ratio", "info",
                    f"Mechanically drilled blind via at {shown:.2f}:1: controlled-depth "
                    "drilling is limited to about 1:1, so this span needs sequential "
                    "lamination (drill and plate the sub-laminate first).",
                    **fields,
                )

    def check_ring(self) -> None:
        explicit = self.limits["min_annular_ring_mil"]
        groups: dict[tuple, dict] = {}
        for via in self.vias:
            rule = self._rule("MinimumAnnularRing", via)
            rule_min = (
                self.checker.values(rule).get("min_ring_mil") if rule is not None else None
            )
            candidates = []
            if rule_min is not None:
                candidates.append((rule_min, "rule"))
            if explicit is not None:
                candidates.append((explicit, "parameter"))
            if candidates:
                minimum, source = max(candidates, key=lambda item: item[0])
            else:
                minimum = MICROVIA_MIN_RING_MIL if via.microvia else DEFAULT_MIN_RING_MIL
                source = "default"
            if via.ring_mil >= minimum - EPS_MIL:
                continue
            rule_name = rule.name if rule is not None and source == "rule" else None
            key = (via.type_id, rule_name, source, round(via.ring_mil, 3))
            group = groups.get(key)
            if group is None:
                groups[key] = group = {
                    "type": via.type_id,
                    "rule": rule_name,
                    "source": source,
                    "microvia": via.microvia,
                    "ring": via.ring_mil,
                    "minimum": minimum,
                    "count": 0,
                    "nets": set(),
                    "at": (via.prim.x1, via.prim.y1),
                }
            group["count"] += 1
            if via.net:
                group["nets"].add(via.net)
        for group in groups.values():
            against = {
                "rule": f"the {group['rule']} rule's minimum",
                "parameter": "the requested minimum",
                "default": (
                    "the 0.05 mm commonly allowed on microvia capture pads"
                    if group["microvia"] else "a common 0.1 mm fabrication minimum"
                ),
            }[group["source"]]
            self._finding(
                "annular_ring", "info" if group["source"] == "default" else "warning",
                f"Annular ring {self._len(group['ring'])} (pad minus finished hole) is "
                f"below {against} of {self._len(group['minimum'])}; drilling oversize "
                "for plating leaves less.",
                type=group["type"],
                rule=group["rule"],
                count=group["count"],
                annular_ring_mil=_round(group["ring"]),
                min_annular_ring_mil=_round(group["minimum"]),
                nets=sorted(group["nets"])[:SAMPLE] or None,
                x_mil=round(group["at"][0], 3),
                y_mil=round(group["at"][1], 3),
            )

    def check_style(self) -> None:
        groups: dict[tuple, dict] = {}
        for via in self.vias:
            rule = self._rule("RoutingVias", via)
            if rule is None:
                continue
            values = self.checker.values(rule)
            problems = []
            style = (values.get("via_style") or "").lower()
            if via.kind in ("blind", "buried"):
                if "through" in style:
                    problems.append("style")
                elif "adjacent" in style and via.high - via.low != 1:
                    problems.append("style")
            for name, actual, low, high in (
                ("diameter", via.prim.width, values.get("min_diameter_mil"),
                 values.get("max_diameter_mil")),
                ("hole", via.prim.hole or 0.0, values.get("min_hole_mil"),
                 values.get("max_hole_mil")),
            ):
                if low is not None and actual < low - EPS_MIL:
                    problems.append(f"{name}_below_min")
                if high is not None and actual > high + EPS_MIL:
                    problems.append(f"{name}_above_max")
            if not problems:
                continue
            key = (via.type_id, rule.name, tuple(problems))
            group = groups.setdefault(
                key, {"values": values, "count": 0, "via": via, "nets": set()}
            )
            group["count"] += 1
            if via.net:
                group["nets"].add(via.net)
        for (type_id, rule_name, problems), group in groups.items():
            values = group["values"]
            via = group["via"]
            size = [problem for problem in problems if problem != "style"]
            fields = {
                "type": type_id,
                "span": self._span_label(via),
                "rule": rule_name,
                "count": group["count"],
                "nets": sorted(group["nets"])[:SAMPLE] or None,
            }
            if size:
                large = (via.prim.hole or 0.0) >= LARGE_HOLE_MIL - EPS_MIL
                issue = (
                    f"Outside the {rule_name} Routing Via Style sizes: "
                    + ", ".join(problem.replace("_", " ") for problem in size) + "."
                )
                if large:
                    issue += (
                        f" A {self._len(via.prim.hole or 0.0)} plated hole drawn as a via "
                        "is usually a mounting hole: a pad or a rule for it is cleaner."
                    )
                self._finding(
                    "via_style", "info" if large else "warning", issue,
                    problems=size,
                    diameter_mil=_round(via.prim.width),
                    hole_mil=_round(via.prim.hole),
                    min_diameter_mil=_round(values.get("min_diameter_mil")),
                    max_diameter_mil=_round(values.get("max_diameter_mil")),
                    min_hole_mil=_round(values.get("min_hole_mil")),
                    max_hole_mil=_round(values.get("max_hole_mil")),
                    **fields,
                )
            if "style" in problems:
                self._finding(
                    "via_style", "info",
                    f"A {via.kind} via where the {rule_name} rule's via style is "
                    f"'{values.get('via_style')}'. The style steers the router, Altium's "
                    "DRC does not check it: confirm the via is intended and its span is "
                    "a drill pair.",
                    via_style=values.get("via_style"),
                    **fields,
                )

    def check_drill_pairs(self) -> None:
        pairs = [entry for entry in self.drill_pairs() if not entry["pair"].back_drill]
        for entry in pairs:
            if entry["low"] is None:
                pair = entry["pair"]
                self._finding(
                    "drill_pairs", "info",
                    f"Drill pair {pair.low} - {pair.high} names a layer that is not in "
                    "the stack.",
                )
        if not pairs:
            if any(via.kind in ("blind", "buried") for via in self.vias):
                self.notes.append(
                    "The PcbDoc stores no drill pairs, so blind and buried spans were "
                    "not matched to drill pairs."
                )
            return
        for info in self.types.values():
            first = info["vias"][0]
            if first.kind not in ("blind", "buried") or self._pair_of(first):
                continue
            self._finding(
                "drill_pairs", "warning",
                f"No drill pair {info['span']} in the layer stack: the drill files have "
                "no layer pair for these holes. Add the pair (Layer Stack Manager) or "
                "change the via span.",
                type=info["id"],
                span=info["span"],
                count=len(info["vias"]),
            )

    def find_in_pads(self) -> None:
        """Mark vias whose hole overlaps an SMD pad on a layer of its span."""
        for via in self.vias:
            prim = via.prim
            hole = prim.hole or 0.0
            if hole <= 0 or via.low is None:
                continue
            for pad in self._near(
                prim.x1, prim.y1, hole / 2.0,
                "AND p.kind = 'pad' AND p.layer != 74 AND COALESCE(p.hole, 0) = 0", (),
            ):
                if self._in_span(via, pad["layer"]) is None:
                    continue
                distance = self.index._point_primitive_distance(prim.x1, prim.y1, pad)
                if distance >= hole / 2.0 - EPS_MIL:
                    continue
                via.pad_sides.add(pad["layer"])
                hit = self.pad_hits.setdefault(pad["id"], {"pad": pad, "vias": []})
                hit["vias"].append(via)

    def check_in_pad(self) -> None:
        groups: dict[tuple, dict] = {}
        for hit in self.pad_hits.values():
            pad = hit["pad"]
            vias = hit["vias"]
            pad_net = pad["net_id"]
            designator = self._designator(pad["component"])
            thermal = (
                len(vias) >= THERMAL_VIAS
                or max(pad["width"], pad["height"] or pad["width"]) >= THERMAL_PAD_MIL - EPS_MIL
                or any(
                    via.prim.component is not None
                    and via.prim.component == pad["component"]
                    for via in vias
                )
            )
            nets = {via.prim.net for via in vias}
            if any(net is not None and pad_net is not None and net != pad_net
                   for net in nets):
                status = "short"
            elif any((net is None) != (pad_net is None) for net in nets):
                status = "net_mismatch"
            else:
                status = "same_net"
            rule = None
            if self._enabled("ViasUnderSMD"):
                pad_net_name = self.index.pcb.nets[pad_net] if pad_net is not None else None
                found, undecided = self.checker.resolver(
                    "ViasUnderSMD", pad_net_name
                ).resolve(self.index.object_facts(pad))
                if undecided:
                    self.undecided["ViasUnderSMD"] += 1
                elif found is not None:
                    rule = (found.name, self.checker.values(found).get("allowed"))
            category = "thermal" if thermal else "via_in_pad"
            key = (designator, category, status, rule)
            group = groups.setdefault(
                key,
                {"pads": [], "pad_count": 0, "vias": 0, "types": Counter(),
                 "layer": pad["layer"], "other_side": 0, "other_side_tented": 0},
            )
            group["pad_count"] += 1
            group["vias"] += len(vias)
            for via in vias:
                group["types"][via.type_id] += 1
                other = "bottom" if pad["layer"] == 1 else "top"
                if other in via.outer_sides and not via.pad_sides & {1 if other == "top" else 32}:
                    group["other_side"] += 1
                    group["other_side_tented"] += bool(via.tented(other))
            name = pad["pad_name"]
            if name and name not in group["pads"]:
                group["pads"].append(name)
        for (designator, category, status, rule), group in groups.items():
            fields = {
                "designator": designator,
                "layer": self.index.layer_name(group["layer"]),
                "pads": group["pad_count"],
                "count": group["vias"],
                "pad_names": group["pads"][:SAMPLE] or None,
                "types": [type_id for type_id, _ in group["types"].most_common(SAMPLE)],
                "thermal_pad": category == "thermal" or None,
            }
            if group["other_side"]:
                fields["tented_on_other_side"] = (
                    f"{group['other_side_tented']} of {group['other_side']}"
                )
            if rule is not None:
                fields["rule"] = rule[0]
            if status == "short":
                self._finding(
                    "via_in_pad", "warning",
                    "Via hole inside an SMD pad of another net: a short.", **fields,
                )
                continue
            if rule is not None and rule[1] is False:
                self._finding(
                    "via_in_pad", "warning",
                    f"Via in SMD pad where the {rule[0]} rule does not allow vias under "
                    "SMD pads.",
                    **fields,
                )
                continue
            prefix = (
                "The pad and the via in it are not on the same net (one has no net): "
                "check the connection. "
                if status == "net_mismatch" else ""
            )
            if category == "thermal":
                issue = (
                    prefix + "Vias in a thermal pad: open holes wick solder from the "
                    "joint. Specify on the fabrication drawing what the package guidance "
                    "asks for (small open vias, vias tented or plugged from the other "
                    "side, or filled and capped, IPC-4761 Type VII) unless already done."
                )
            else:
                issue = (
                    prefix + "Via in SMD pad: an open or tented hole wicks solder from "
                    "the joint. Specify filled and capped vias (IPC-4761 Type VII) on "
                    "the fabrication drawing unless already done."
                )
            self._finding("via_in_pad", "info", issue, **fields)

    def check_tenting(self) -> None:
        eligible = [
            via for via in self.vias
            if via.outer_sides and not via.pad_sides and not via.test_point()
            and via.stack is not None
        ]
        small = [via for via in eligible if (via.prim.hole or 0.0) <= TENT_MAX_HOLE_MIL + EPS_MIL]
        states = Counter(self._tent_state(via) for via in small)
        if small and states["untented"] == len(small):
            self._finding(
                "tenting", "info",
                f"None of the {len(small)} vias checked (holes up to "
                f"{self._len(TENT_MAX_HOLE_MIL)}, outside SMD pads, not test points) is "
                "tented: the rings are exposed to solder. Fine if that is the house "
                "style; otherwise tent or plug them.",
                count=len(small),
            )
        else:
            groups = Counter()
            for via in small:
                state = self._tent_state(via)
                if state in ("untented", "top_only", "bottom_only"):
                    groups[(via.type_id, state)] += 1
            for (type_id, state), count in groups.items():
                if state == "untented":
                    issue = (
                        "Untented and not a test point: the ring is exposed to solder. "
                        "Fine if intended (probing or heat), otherwise tent or plug them."
                    )
                    tented = "none"
                else:
                    tented = "top" if state == "top_only" else "bottom"
                    issue = (
                        f"Tented on the {tented} only: IPC-4761 advises against "
                        "one-sided tenting, which can trap process chemistry in the "
                        "hole. Tent both sides or plug."
                    )
                self._finding("tenting", "info", issue, type=type_id, count=count,
                              tented=tented)
        large = Counter(
            via.type_id for via in eligible
            if (via.prim.hole or 0.0) > TENT_MAX_HOLE_MIL + EPS_MIL
            and self._tent_state(via) in ("tented", "top_only", "bottom_only")
        )
        for type_id, count in large.items():
            hole = self._type_of(type_id)["hole"]
            self._finding(
                "tenting", "info",
                f"Tented {self._len(hole)} holes: solder mask does not reliably bridge "
                f"holes above {self._len(TENT_MAX_HOLE_MIL)}. Plug them or leave them "
                "open.",
                type=type_id, count=count, hole_mil=_round(hole),
            )

    def check_stacked(self) -> None:
        """Microvias stacked on a blind or buried via (same centre, a shared
        layer). One sharing its centre with a through via is a drill clash, which
        Altium's hole-to-hole rule reports."""
        microvias = [via for via in self.vias if via.microvia]
        if not microvias:
            return
        by_point = defaultdict(list)
        for via in self.vias:
            if via.low is not None:
                by_point[(round(via.prim.x1, 1), round(via.prim.y1, 1))].append(via)
        for via in microvias:
            for other in by_point[(round(via.prim.x1, 1), round(via.prim.y1, 1))]:
                if other is not via and other.kind in ("blind", "buried") and (
                    other.low in (via.low, via.high) or other.high in (via.low, via.high)
                ):
                    self.stacked.append(via)
                    break
        if not self.stacked:
            return
        allowed = [
            self.checker.values(rule).get("allow_stacked_microvias")
            for rule in self._enabled("HoleToHoleClearance")
        ]
        forbidden = allowed and all(value is False for value in allowed)
        self._finding(
            "aspect_ratio", "warning" if forbidden else "info",
            "Microvias stacked on another via: they must be copper filled and are a "
            "known reliability risk; confirm the fabricator builds them"
            + (
                ", and the Hole To Hole Clearance rule does not allow stacked microvias, "
                "so Altium's DRC flags them." if forbidden else "."
            ),
            count=len(self.stacked),
            types=sorted({via.type_id for via in self.stacked}),
        )

    # --- back drilling and stubs ---

    def _connected(self, via: _Via) -> set[int]:
        """Stack positions where the via meets same-net copper."""
        if via.connected is not None:
            return via.connected
        prim = via.prim
        connected: set[int] = set()
        via.connected = connected
        if prim.net is None or via.low is None:
            return connected
        x, y = prim.x1, prim.y1
        rows = self._near(
            x, y, via.reach(), "AND p.net_id = ? AND p.pour_id IS NULL", (prim.net,),
        )
        for row in rows:
            kind = row["kind"]
            if kind in ("track", "arc"):
                position = self._in_span(via, row["layer"])
                if position is None:
                    continue
                reach = via.pad_radius(row["layer"]) + row["width"] / 2.0
                if point_seg_distance(
                    x, y, row["x1"], row["y1"], row["x2"], row["y2"]
                ) <= reach + EPS_MIL:
                    connected.add(position)
            elif kind == "pad":
                if row["layer"] == 74:
                    continue
                position = self._in_span(via, row["layer"])
                if position is None:
                    continue
                if self.index._point_primitive_distance(
                    x, y, row
                ) <= via.pad_radius(row["layer"]) + EPS_MIL:
                    connected.add(position)
            elif kind == "via":
                if (
                    row["x1"] == x and row["y1"] == y and row["layer"] == prim.layer
                    and row["layer_to"] == prim.layer_to
                ):
                    continue
                if math.hypot(row["x1"] - x, row["y1"] - y) > (
                    row["width"] + prim.width
                ) / 2.0 + EPS_MIL:
                    continue
                for layer in (row["layer"], row["layer_to"]):
                    position = self._in_span(via, layer)
                    if position is not None:
                        connected.add(position)
        for pour in self._pours_by_net().get(prim.net, ()):
            position = self._in_span(via, pour.layer)
            if position is None or position in connected:
                continue
            if point_in_polygon(x, y, pour.vertices):
                connected.add(position)
        return connected

    def _pours_by_net(self) -> dict:
        if not hasattr(self, "_pours"):
            self._pours = defaultdict(list)
            for pour in self.index.pcb.pours:
                if pour.net is not None and pour.layer is not None and len(pour.vertices) >= 3:
                    self._pours[pour.net].append(pour)
        return self._pours

    def back_drill_pairs(self) -> list[dict]:
        """Back drills as {pair, side, stop} (stop: stack position)."""
        if hasattr(self, "_back_drills"):
            return self._back_drills
        result = []
        for entry in self.drill_pairs():
            pair = entry["pair"]
            if not pair.back_drill:
                continue
            item = {"pair": pair, "side": None, "stop": None}
            if entry["low"] is not None:
                low, high = entry["low"], entry["high"]
                if low == 0 and high != self.stack.last:
                    item.update(side="top", stop=high)
                elif high == self.stack.last and low != 0:
                    item.update(side="bottom", stop=low)
            result.append(item)
        self._back_drills = result
        return result

    def _pair_label(self, item: dict) -> str:
        outer = "Top" if item["side"] == "top" else "Bottom"
        return f"{outer} -> before {self.stack.name(item['stop'])}"

    def _barrel(self, side: str, connection: int, end: int) -> float | None:
        """Barrel length from the connected layer's face to the end layer's face."""
        if not self.stack.has_heights:
            return None
        if side == "bottom":
            return self.stack.bottom_face(end) - self.stack.bottom_face(connection)
        return self.stack.top_face(connection) - self.stack.top_face(end)

    def _side_stub(self, via: _Via, side: str, rule_values: dict | None) -> dict | None:
        """The stub at one end of the via after any back drill, None without one."""
        connected = self._connected(via)
        if not connected:
            return None
        if side == "bottom":
            connection, end = max(connected), via.high
            if connection >= end:
                return None
        else:
            connection, end = min(connected), via.low
            if connection <= end:
                return None
        record = {
            "side": side,
            "connection": connection,
            "connected_layer": self.stack.name(connection),
            "barrel_end": self.stack.name(end),
            "stub_layers": abs(end - connection),
            "stub_mil": self._barrel(side, connection, end),
        }
        if rule_values is None:
            return record
        use = rule_values.get(f"use_{side}_back_drill", True)
        outer = via.high == self.stack.last if side == "bottom" else via.low == 0
        candidates = [
            item for item in self.back_drill_pairs()
            if item["side"] == side and item["stop"] is not None
        ]
        usable = [
            item for item in candidates
            if (connection <= item["stop"] < end if side == "bottom"
                else end < item["stop"] <= connection)
        ]
        reason = None
        if not use:
            reason = f"the rule does not use {side} back drills"
        elif not outer:
            reason = f"the via does not reach the {side} layer"
        elif not candidates:
            reason = f"the stack has no back drill from the {side}"
        elif not usable:
            reason = (
                f"connected on {self.stack.name(connection)}, inside the layers every "
                f"{side} back drill removes"
            )
        if reason:
            record["not_back_drilled"] = reason
            return record
        best = (
            min(usable, key=lambda item: item["stop"]) if side == "bottom"
            else max(usable, key=lambda item: item["stop"])
        )
        stop = best["stop"]
        label = self._pair_label(best)
        via.drilled.append(label)
        oversize = rule_values.get("oversize_mil") or 0.0
        via.drill_holes[label] = (via.prim.hole or 0.0) + 2 * oversize
        # The tip stops in the dielectric between the stop layer and the first
        # drilled layer.
        first_drilled = stop + 1 if side == "bottom" else stop - 1
        record.update(
            back_drilled=label,
            barrel_end=self.stack.name(stop),
            stub_layers=abs(stop - connection),
            stub_min_mil=self._barrel(side, connection, stop),
        )
        if self.stack.has_heights:
            near_face = (
                self.stack.top_face(first_drilled) if side == "bottom"
                else self.stack.bottom_face(first_drilled)
            )
            record["stub_mil"] = abs(
                near_face - (
                    self.stack.bottom_face(connection) if side == "bottom"
                    else self.stack.top_face(connection)
                )
            )
        else:
            record["stub_mil"] = None
        return record

    def _resonance_ghz(self, stub: dict) -> float | None:
        """Quarter-wave resonance of a stub: c / (4 L sqrt(Dk)), Dk of the
        dielectric along it."""
        length = stub.get("stub_mil")
        if not length or not self.stack.has_heights:
            return None
        if stub["side"] == "bottom":
            start = self.stack.bottom_face(stub["connection"])
            end = start + length
        else:
            start = self.stack.top_face(stub["connection"])
            end = start - length
        dk = self.stack.dk_between(start, end)
        return LIGHT_INCH_PER_NS / (4 * (length / 1000.0) * math.sqrt(dk))

    def check_back_drill(self) -> dict:
        rules = self._enabled("BackDrilling")
        pairs = self.back_drill_pairs()
        high_speed_limit = self.limits["max_stub_mil"]
        if high_speed_limit is None:
            high_speed_limit = DEFAULT_MAX_STUB_MIL
        rule_stats: dict[str, Counter] = defaultdict(Counter)
        groups: dict[tuple, dict] = {}
        checked = 0
        for via in self.vias:
            if via.kind == "unknown" or via.net is None:
                continue
            rule = self._rule("BackDrilling", via) if rules else None
            pair_of = self.index.diff_pairs_of(via.net)
            high_speed = bool(pair_of) or self.selection is not None
            if rule is None and not high_speed:
                continue
            checked += 1
            values = self.checker.values(rule) if rule is not None else None
            if rule is not None:
                rule_stats[rule.name]["candidates"] += 1
            if not self._connected(via):
                if rule is not None:
                    rule_stats[rule.name]["no_connection_found"] += 1
                continue
            stubs = [
                stub for stub in (
                    self._side_stub(via, "top", values),
                    self._side_stub(via, "bottom", values),
                )
                if stub is not None
            ]
            if rule is not None and via.drilled:
                rule_stats[rule.name]["back_drilled"] += 1
            for stub in stubs:
                length = stub["stub_mil"]
                if length is None:
                    continue
                if rule is not None:
                    limit = values.get("max_stub_length_mil")
                    if limit is None or length <= limit + EPS_MIL:
                        continue
                    severity, source = "warning", rule.name
                else:
                    limit = high_speed_limit
                    if length <= limit + EPS_MIL:
                        continue
                    severity, source = "info", None
                owner = pair_of[0]["name"] if pair_of and source is None else via.net
                key = (owner, source, stub["side"], stub["connected_layer"],
                       stub.get("not_back_drilled"), stub.get("back_drilled"))
                group = groups.get(key)
                if group is None:
                    groups[key] = group = {
                        "severity": severity,
                        "pair": pair_of[0]["name"] if pair_of else None,
                        "nets": set(),
                        "rule": source,
                        "limit": limit,
                        "stub": stub,
                        "via": via,
                        "count": 0,
                        "types": set(),
                    }
                group["count"] += 1
                group["nets"].add(via.net)
                group["types"].add(via.type_id)
                if length > group["stub"]["stub_mil"]:
                    group["stub"], group["via"] = stub, via

        if rules and not [item for item in pairs if item["side"]]:
            self._finding(
                "back_drill", "warning",
                "Back-drilling rules exist but the layer stack has no usable back drill "
                "(Layer Stack Manager, Back Drills tab), so nothing is back drilled.",
                rule=", ".join(rule.name for rule in rules[:SAMPLE])
                + (f" and {len(rules) - SAMPLE} more" if len(rules) > SAMPLE else ""),
            )
        if pairs and not rules:
            self._finding(
                "back_drill", "info",
                "The layer stack defines back drills but no enabled Max Via Stub Length "
                "(Back Drilling) rule selects vias for them, so nothing is back drilled.",
            )
        for item in pairs:
            if item["side"] is None:
                pair = item["pair"]
                self._finding(
                    "back_drill", "info",
                    f"Back drill {pair.low} - {pair.high} does not start at an outer "
                    "layer (or names a layer not in the stack); it was not applied.",
                )
        unconnected = sum(stats["no_connection_found"] for stats in rule_stats.values())
        if unconnected:
            self.notes.append(
                f"{unconnected} back-drill candidate via(s) have no connection found "
                "(planes, fills and regions are not read), so whether Altium drills them "
                "was not decided."
            )
        for group in groups.values():
            stub = group["stub"]
            length = stub["stub_mil"]
            ghz = self._resonance_ghz(stub)
            nets = sorted(group["nets"])
            if group["rule"]:
                issue = (
                    f"Stub {self._len(length, 1)} past {stub['connected_layer']} "
                    f"({stub['side']} end) exceeds the {group['rule']} rule's max stub "
                    f"{self._len(group['limit'], 1)}"
                )
                if stub.get("not_back_drilled"):
                    issue += f": not back drilled, {stub['not_back_drilled']}."
                else:
                    issue += (
                        f" even when back drilled ({stub['back_drilled']}): the drill tip "
                        "may stop anywhere in the dielectric before the stop layer."
                    )
            else:
                owner = f"pair {group['pair']}" if group["pair"] else f"net {nets[0]}"
                issue = (
                    f"Via stubs of {owner} reach {self._len(length, 1)} past "
                    f"{stub['connected_layer']} ({stub['side']} end), quarter-wave "
                    f"resonance about {ghz:.0f} GHz, and no back-drill rule covers them. "
                    "That matters when the signal has energy near it (multi-gigabit "
                    "links): route to the far layer or back drill; slower signals can "
                    "ignore it."
                )
            self._finding(
                "back_drill", group["severity"], issue,
                pair=group["pair"],
                nets=nets[:SAMPLE],
                rule=group["rule"],
                count=group["count"],
                types=sorted(group["types"])[:SAMPLE],
                side=stub["side"],
                connected_layer=stub["connected_layer"],
                barrel_end=stub["barrel_end"],
                stub_mil=_round(length, 2),
                stub_min_mil=_round(stub.get("stub_min_mil"), 2),
                max_stub_mil=_round(group["limit"]),
                resonance_ghz=_round(ghz, 1),
                x_mil=round(group["via"].prim.x1, 3),
                y_mil=round(group["via"].prim.y1, 3),
            )

        report: dict = {"vias_checked_for_stubs": checked}
        drilled = Counter(label for via in self.vias for label in via.drilled)
        holes: dict[str, set] = defaultdict(set)
        for via in self.vias:
            for label, hole in via.drill_holes.items():
                holes[label].add(round(hole, 3))
        pair_report = []
        for item in pairs:
            pair = item["pair"]
            if item["side"] is None:
                pair_report.append({"layers": f"{pair.low} - {pair.high}", "applied": False})
                continue
            label = self._pair_label(item)
            stop = item["stop"]
            if item["side"] == "bottom":
                drilled_layers = range(stop + 1, self.stack.last + 1)
                depth = (
                    self.stack.thickness_mil - self.stack.bottom_face(stop)
                    if self.stack.has_heights else None
                )
            else:
                drilled_layers = range(0, stop)
                depth = self.stack.top_face(stop) if self.stack.has_heights else None
            names = [self.stack.name(position) for position in drilled_layers]
            sizes = sorted(holes[label])
            pair_report.append(
                {
                    "back_drill": label,
                    "from_layer": "Top" if item["side"] == "top" else "Bottom",
                    "stops_before": self.stack.name(stop),
                    "drilled_layers": len(names),
                    "first_and_last_drilled": [names[0], names[-1]] if names else [],
                    "depth_to_stop_layer_mil": _round(depth, 2),
                    "vias": drilled[label],
                    "drill_diameters_mil": sizes[:SAMPLE * 2] or None,
                }
            )
            if len(sizes) > SAMPLE * 2:
                pair_report[-1]["drill_diameters_total"] = len(sizes)
        if pair_report:
            report["pairs"] = pair_report
        if rules:
            report["rules"] = []
            for rule in rules:
                values = self.checker.values(rule)
                entry = {"rule": rule.name, "scope": rule.scope1}
                for key in ("max_stub_length_mil", "oversize_mil", "tolerance_plus_mil",
                            "tolerance_minus_mil", "use_top_back_drill",
                            "use_bottom_back_drill"):
                    entry[key] = values.get(key)
                entry["candidate_vias"] = rule_stats[rule.name]["candidates"]
                entry["back_drilled_vias"] = rule_stats[rule.name]["back_drilled"]
                if rule_stats[rule.name]["no_connection_found"]:
                    entry["no_connection_found"] = rule_stats[rule.name]["no_connection_found"]
                report["rules"].append(entry)
        return report


def _selection(index, names: list[str] | None) -> tuple[set[str] | None, list[str]]:
    """Lower-case nets for net, differential-pair and net-class names."""
    if not names:
        return None, []
    ctx = index.rule_context
    nets: set[str] = set()
    unknown = []
    for name in names:
        key = name.lower()
        if key in ctx.net_names:
            nets.add(key)
        elif key in ctx.pairs:
            nets.update(net.lower() for net in ctx.pairs[key][1:])
        elif key in ctx.net_classes:
            nets.update(ctx.net_classes[key])
        else:
            unknown.append(name)
    return nets, unknown


def review_vias(
    index,
    checks: list[str] | None = None,
    nets: list[str] | None = None,
    max_aspect_ratio: float | None = None,
    max_microvia_aspect_ratio: float | None = None,
    min_annular_ring_mil: float | None = None,
    max_stub_mil: float | None = None,
    units: str = "mil",
) -> dict:
    """The whole review, unpaged (page_result pages it), or an error."""
    selected = list(dict.fromkeys(checks)) if checks else list(CHECKS)
    invalid = [check for check in selected if check not in CHECKS]
    if invalid:
        return {
            "error": "invalid_checks",
            "message": f"Unknown checks: {', '.join(invalid)}.",
            "valid_checks": list(CHECKS),
        }
    selection, unknown = _selection(index, nets)
    if unknown:
        return {
            "error": "net_not_found",
            "message": (
                "No PCB net, differential pair or net class named "
                + ", ".join(f"'{name}'" for name in unknown) + "."
            ),
        }
    review = _Review(
        index,
        selection,
        {
            "max_aspect_ratio": max_aspect_ratio,
            "max_microvia_aspect_ratio": max_microvia_aspect_ratio,
            "min_annular_ring_mil": min_annular_ring_mil,
            "max_stub_mil": max_stub_mil,
        },
        set(selected),
        units,
    )
    # Pads, stacking and back drilling also feed the inventory, so they always run.
    review.find_in_pads()
    review.check_stacked()
    back_drill = review.check_back_drill()
    if "aspect_ratio" in selected:
        review.check_aspect()
    if "annular_ring" in selected:
        review.check_ring()
    if "via_style" in selected:
        review.check_style()
    if "drill_pairs" in selected:
        review.check_drill_pairs()
    if "via_in_pad" in selected:
        review.check_in_pad()
    if "tenting" in selected:
        review.check_tenting()

    findings = sorted(
        review.findings,
        key=lambda finding: (
            _SEVERITY_ORDER[finding["severity"]],
            CHECKS.index(finding["check"]),
            -finding.get("count", 1),
            str(finding.get("type", "")),
            str(finding.get("nets", "")),
        ),
    )
    result: dict = {
        "checks_run": selected,
        "board": review.board(),
        "summary": review.summary(),
    }
    if selection is not None:
        result["nets_selected"] = len(selection)
    result.update(review.inventory())
    result["drill_pairs"] = review.drill_pair_report()
    result["back_drill"] = back_drill
    result["severity_counts"] = dict(Counter(finding["severity"] for finding in findings))
    result["findings_by_check"] = dict(Counter(finding["check"] for finding in findings))
    result["findings"] = findings
    if review.undecided:
        result["undecided_rules"] = dict(review.undecided)
        review.notes.append(
            "Some rule scopes could not be decided for some vias (undecided_rules); "
            "those vias were not checked against that rule kind."
        )
    result["notes"] = review.notes + [
        "Holes are as drawn (Altium's finished hole); fabricators drill larger for "
        "plating. Plugging and filling (IPC-4761 beyond tenting) are not decoded from "
        "the PcbDoc; tenting is, and vias in SMD pads are counted apart.",
    ]
    return result


def page_result(
    full: dict,
    offset: int = 0,
    limit: int = 100,
    type_limit: int = 60,
    class_limit: int = 30,
) -> dict:
    """A page of review_vias' result: findings from offset; the most used via
    types, net classes (each with its most used types), drill pairs and back
    drills."""
    result = dict(full)
    offset = max(0, offset)
    findings = full["findings"]
    for key, cap in (
        ("via_types", type_limit),
        ("net_classes", class_limit),
        ("drill_pairs", type_limit),
    ):
        if len(full[key]) > cap:
            result[key] = full[key][:cap]
            result[f"{key}_total"] = len(full[key])
    result["net_classes"] = [
        {
            **entry,
            "types": dict(list(entry["types"].items())[:type_limit]),
            "types_total": len(entry["types"]),
        }
        if len(entry["types"]) > type_limit else entry
        for entry in result["net_classes"]
    ]
    back_drill = dict(full["back_drill"])
    for key in ("pairs", "rules"):
        if len(back_drill.get(key, [])) > class_limit:
            back_drill[f"{key}_total"] = len(back_drill[key])
            back_drill[key] = back_drill[key][:class_limit]
    result["back_drill"] = back_drill
    notes = result.pop("notes")
    result["findings"] = findings[offset : offset + limit]
    result["total"] = len(findings)
    result["offset"] = offset
    result["has_more"] = offset + limit < len(findings)
    if result["has_more"]:
        result["next_offset"] = offset + limit
    result["notes"] = notes
    return result


def run_via_check(
    index,
    checks: list[str] | None = None,
    nets: list[str] | None = None,
    max_aspect_ratio: float | None = None,
    max_microvia_aspect_ratio: float | None = None,
    min_annular_ring_mil: float | None = None,
    max_stub_mil: float | None = None,
    units: str = "mil",
    offset: int = 0,
    limit: int = 100,
    type_limit: int = 60,
    class_limit: int = 30,
) -> dict:
    """Via inventory and fab-review findings, paged (see the module docstring)."""
    full = review_vias(
        index, checks, nets, max_aspect_ratio, max_microvia_aspect_ratio,
        min_annular_ring_mil, max_stub_mil, units,
    )
    if "error" in full:
        return full
    return page_result(full, offset, limit, type_limit, class_limit)
