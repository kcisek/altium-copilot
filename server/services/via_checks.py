"""Via inventory and fabrication review over a PcbIndex.

Pure logic, no MCP. Lengths are mils under *_mil keys, so callers can convert units.

The inventory is what a fabricator asks for: via types by span (first and last copper
layer in stack order), hole and pad diameter; counts per type and per net class; the
drilled depth and aspect ratio from the stack's copper and dielectric heights; the
smallest annular ring over the layers that keep a pad; tenting; vias in SMD pads; the
drill pairs; and back drilling.

Back drilling follows Altium's Controlled Depth Drilling: the Layer Stack Manager's back
drills (Board6 LAYERPAIRn with BACKDRILL=TRUE) are drilled from their outer layer and
stop before the other layer; the Max Via Stub Length (Back Drilling) rule selects
candidate vias, and a candidate is drilled by a pair only when it has no connection on
the layers that pair drills. Altium computes this when it writes the drill files, so it
is recomputed here. Connections are same-net tracks, arcs and SMD pads touching the
via's pad on a layer (its hole where the pad is removed), same-net pours whose outline
holds the via, and same-net vias stacked on it. Internal planes are not read, so a via
whose only connection is a plane shows no stub. A stub runs from the face of the last
connected copper layer to the end of the barrel (the stop layer's far face when back
drilled).

Limits: aspect ratio warns above 10:1 and notes above 8:1 (through and mechanically
drilled blind/buried vias; drilled depth over hole); microvias (one dielectric, hole up
to 0.15 mm, IPC-2226) warn above 1:1 (dielectric over hole). The annular ring is
(pad - hole) / 2 like Altium's Minimum Annular Ring rule, checked against that rule,
else against 0.1 mm as a note. Stubs of differential-pair nets (and the nets asked
for) not covered by a back-drill rule are noted above 10 mil, Altium's signal-integrity
guidance for residual stubs.
"""

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
ASPECT_NOTE = 8.0
ASPECT_WARNING = 10.0
MICROVIA_ASPECT_WARNING = 1.0
DEFAULT_MIN_RING_MIL = 0.1 * MM
DEFAULT_MAX_STUB_MIL = 10.0
PAD_SAMPLE = 5

_MODES = {0: "simple", 1: "top-middle-bottom", 2: "full stack"}
_SEVERITY_ORDER = {"warning": 0, "info": 1}


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
        rows = index.db.execute(
            "SELECT v6_id, kind, copper_thick_mil, diel_height_mil FROM layers "
            "WHERE stack_order IS NOT NULL ORDER BY stack_order"
        ).fetchall()
        copper = [number for number, row in enumerate(rows) if row["kind"] == "copper"]
        if not copper:
            return
        top: dict[int, float] = {}
        bottom: dict[int, float] = {}
        depth = 0.0
        for row in rows[copper[0] : copper[-1] + 1]:
            is_copper = row["kind"] == "copper"
            thickness = row["copper_thick_mil"] if is_copper else row["diel_height_mil"]
            if thickness is None:
                return
            if is_copper and row["v6_id"] is not None:
                top[row["v6_id"]] = depth
                bottom[row["v6_id"]] = depth + thickness
            depth += thickness
        self.top, self.bottom, self.thickness_mil = top, bottom, depth

    @property
    def has_heights(self) -> bool:
        return self.top is not None

    def name(self, position: int) -> str:
        return self.index.layer_name(self.order[position])

    def span_depth(self, low: int, high: int) -> float | None:
        """Copper and dielectric from the low layer's top face to the high
        layer's bottom face."""
        if not self.has_heights:
            return None
        return self.bottom[self.order[high]] - self.top[self.order[low]]

    def dielectric(self, low: int, high: int) -> float | None:
        """Dielectric between two layers (a microvia's laser depth)."""
        if not self.has_heights:
            return None
        return self.top[self.order[high]] - self.bottom[self.order[low]]

    def resolve(self, token: str) -> int | None:
        """A stored layer token (TOP, MID4, BOTTOM, PLANE2 or a layer name) as a
        stack position."""
        text = (token or "").strip().lower()
        layer = None
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
    type_id: str = ""
    connected: set | None = None
    back_drilled: str | None = None  # pair label
    back_drill_hole: float | None = None
    in_pad: list = field(default_factory=list)
    facts: object = None

    @property
    def stack(self):
        return self.prim.via

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


class _Review:
    def __init__(self, index, selection: set[str] | None, limits: dict, checks: set):
        self.index = index
        self.stack = _Stack(index)
        self.selection = selection
        self.limits = limits
        self.checks = checks
        self.checker = RuleChecker(index, 1)
        self.findings: list[dict] = []
        self.notes: list[str] = []
        self.undecided: Counter = Counter()
        self._enabled_rules: dict[str, list] = {}
        self.vias = self._collect()
        self.types = self._types()

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
            microvia = (
                kind in ("blind", "buried")
                and high - low == 1
                and 0 < hole <= MICROVIA_MAX_HOLE_MIL + EPS_MIL
            )
            via = _Via(
                prim=prim,
                net=net,
                low=low,
                high=high,
                kind=kind,
                microvia=microvia,
                ring_mil=0.0,
                type_key=(
                    {"through": 0, "blind": 1, "buried": 2, "unknown": 3}[kind],
                    key_span,
                    round(hole, 3),
                    round(prim.width, 3),
                ),
            )
            via.ring_mil = self._ring(via)
            vias.append(via)
        return vias

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
            return self.stack.dielectric(via.low, via.high)
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
        if self.index._has_rtree:
            sql = (
                "SELECT p.* FROM prims_rtree r JOIN prims p ON p.id = r.id "
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

    # --- inventory ---

    def inventory(self) -> dict:
        drilled = Counter(via.type_id for via in self.vias if via.back_drilled)
        in_pad = Counter(via.type_id for via in self.vias if via.in_pad)
        entries = []
        for info in self.types.values():
            members = info["vias"]
            nets = {via.net for via in members if via.net}
            tented = Counter()
            modes = Counter()
            test_points = removed = 0
            for via in members:
                stack = via.stack
                if stack is None:
                    tented["unknown"] += 1
                    continue
                if stack.tent_top and stack.tent_bottom:
                    tented["both"] += 1
                elif stack.tent_top:
                    tented["top_only"] += 1
                elif stack.tent_bottom:
                    tented["bottom_only"] += 1
                else:
                    tented["none"] += 1
                modes[_MODES.get(stack.mode, f"mode {stack.mode}")] += 1
                test_points += stack.test_fab_top or stack.test_fab_bottom
                removed += bool(stack.removed_pads)
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
            no_net = sum(1 for via in members if via.net is None)
            if no_net:
                entry["no_net"] = no_net
            entry["depth_mil"] = _round(info["depth"], 2)
            entry["aspect_ratio"] = _round(info["aspect"], 2)
            entry["min_annular_ring_mil"] = _round(min(via.ring_mil for via in members))
            entry["tented"] = dict(tented)
            if modes and set(modes) != {"simple"}:
                entry["pad_stack"] = dict(modes)
            if test_points:
                entry["test_points"] = test_points
            if removed:
                entry["with_removed_pads"] = removed
            if in_pad[info["id"]]:
                entry["in_pad"] = in_pad[info["id"]]
            if drilled[info["id"]]:
                entry["back_drilled"] = drilled[info["id"]]
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
                {"class": name, "vias": sum(counts.values()), "types": dict(counts)}
                for name, counts in classes.items()
            ),
            key=lambda entry: (-entry["vias"], entry["class"].lower()),
        )
        return {"via_types": entries, "net_classes": class_entries}

    def summary(self) -> dict:
        kinds = Counter(via.kind for via in self.vias)
        tented = Counter()
        for via in self.vias:
            stack = via.stack
            if stack is None:
                continue
            sides = stack.tent_top + stack.tent_bottom
            tented[("untented", "one_side_tented", "tented")[sides]] += 1
        summary = {
            "vias": len(self.vias),
            "types": len(self.types),
            "through": kinds["through"],
            "blind": kinds["blind"],
            "buried": kinds["buried"],
            "microvias": sum(1 for via in self.vias if via.microvia),
            "no_net": sum(1 for via in self.vias if via.net is None),
            "tented": tented["tented"],
            "one_side_tented": tented["one_side_tented"],
            "untented": tented["untented"],
            "in_pad": sum(1 for via in self.vias if via.in_pad),
            "back_drilled": sum(1 for via in self.vias if via.back_drilled),
        }
        if kinds["unknown"]:
            summary["unknown_span"] = kinds["unknown"]
        return summary

    def board(self) -> dict:
        board = {
            "copper_layers": len(self.stack.order),
            "thickness_mil": _round(self.stack.thickness_mil, 2),
        }
        if not self.stack.has_heights:
            board["stack_heights"] = "missing: depths, aspect ratios and stubs not computed"
        if self.stack.legacy:
            board["stack_order"] = "legacy stackup: layer order assumed from layer ids"
        return board

    # --- drill pairs ---

    def drill_pairs(self) -> list[dict]:
        """Resolved Board6 drill pairs: (low, high, back_drill, pair)."""
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
        explicit = self.limits["max_aspect_ratio"] is not None
        warn = self.limits["max_aspect_ratio"] or ASPECT_WARNING
        micro_warn = self.limits["max_microvia_aspect_ratio"] or MICROVIA_ASPECT_WARNING
        for info in self.types.values():
            aspect = info["aspect"]
            if aspect is None:
                continue
            fields = {
                "type": info["id"],
                "span": info["span"],
                "count": len(info["vias"]),
                "hole_mil": _round(info["hole"]),
                "depth_mil": _round(info["depth"], 2),
                "aspect_ratio": round(aspect, 2),
            }
            if info["microvia"]:
                if aspect > micro_warn + 1e-9:
                    self._finding(
                        "aspect_ratio", "warning",
                        f"Microvia aspect ratio {aspect:.2f}:1 is above {micro_warn:g}:1 "
                        "(dielectric depth over hole); laser vias this deep plate poorly. "
                        "Use a larger hole or a thinner dielectric, or check the "
                        "fabricator's microvia limit.",
                        **fields,
                    )
                continue
            if aspect > warn + 1e-9:
                self._finding(
                    "aspect_ratio", "warning",
                    f"Aspect ratio {aspect:.2f}:1 is above {warn:g}:1 (drilled depth over "
                    "hole): beyond most fabricators' plating limit. Use a larger hole or "
                    "confirm the fabricator's capability.",
                    **fields,
                )
            elif not explicit and aspect > ASPECT_NOTE + 1e-9:
                self._finding(
                    "aspect_ratio", "info",
                    f"Aspect ratio {aspect:.2f}:1 is above the 8:1 many fabricators quote "
                    "as standard (10:1 and more is advanced); confirm with the fabricator.",
                    **fields,
                )

    def check_ring(self) -> None:
        explicit = self.limits["min_annular_ring_mil"]
        groups: dict[tuple, dict] = {}
        for via in self.vias:
            rule = self._rule("MinimumAnnularRing", via)
            minimum = (
                self.checker.values(rule).get("min_ring_mil") if rule is not None else None
            )
            if minimum is None:
                minimum = explicit if explicit is not None else DEFAULT_MIN_RING_MIL
                source = "parameter" if explicit is not None else "default"
            else:
                source = "rule"
            if via.ring_mil >= minimum - EPS_MIL:
                continue
            key = (via.type_id, rule.name if rule else None, round(via.ring_mil, 3))
            group = groups.get(key)
            if group is None:
                groups[key] = group = {
                    "type": via.type_id,
                    "rule": rule.name if rule else None,
                    "source": source,
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
            severity = "info" if group["source"] == "default" else "warning"
            against = {
                "rule": f"the {group['rule']} rule's",
                "parameter": "the requested",
                "default": "the common 0.1 mm",
            }[group["source"]]
            self._finding(
                "annular_ring", severity,
                f"Annular ring {group['ring']:.2f} mil is below {against} minimum "
                f"{group['minimum']:.2f} mil.",
                type=group["type"],
                rule=group["rule"],
                count=group["count"],
                annular_ring_mil=_round(group["ring"]),
                min_annular_ring_mil=_round(group["minimum"]),
                nets=sorted(group["nets"])[:PAD_SAMPLE] or None,
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
                key,
                {"rule": rule, "values": values, "problems": problems, "count": 0,
                 "via": via, "nets": set()},
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
                "nets": sorted(group["nets"])[:PAD_SAMPLE] or None,
            }
            if size:
                self._finding(
                    "via_style", "warning",
                    f"Outside the {rule_name} Routing Via Style sizes: "
                    + ", ".join(problem.replace("_", " ") for problem in size) + ".",
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

    def check_in_pad(self) -> None:
        groups: dict[tuple, dict] = {}
        for via in self.vias:
            prim = via.prim
            hole = prim.hole or 0.0
            if hole <= 0 or via.low is None:
                continue
            rows = self._near(
                prim.x1, prim.y1, hole / 2.0,
                "AND p.kind = 'pad' AND p.layer != 74 AND COALESCE(p.hole, 0) = 0",
                (),
            )
            for pad in rows:
                if self._in_span(via, pad["layer"]) is None:
                    continue
                if self.index._point_primitive_distance(prim.x1, prim.y1, pad) >= hole / 2.0 - EPS_MIL:
                    continue
                same_net = pad["net_id"] == prim.net and prim.net is not None
                via.in_pad.append(pad)
                component = pad["component"]
                designator = (
                    self.index.designator(component)
                    if component is not None and 0 <= component < len(self.index.pcb.components)
                    else None
                )
                facts = self.index.object_facts(pad)
                pad_net = (
                    self.index.pcb.nets[pad["net_id"]] if pad["net_id"] is not None else None
                )
                allowed = None
                if self._enabled("ViasUnderSMD"):
                    rule, undecided = self.checker.resolver(
                        "ViasUnderSMD", pad_net
                    ).resolve(facts)
                    if undecided:
                        self.undecided["ViasUnderSMD"] += 1
                    elif rule is not None:
                        allowed = (rule.name, self.checker.values(rule).get("allowed"))
                key = (designator, via.type_id, same_net, allowed)
                group = groups.setdefault(
                    key,
                    {"pads": [], "count": 0, "layer": pad["layer"],
                     "footprint_vias": 0, "untented": 0},
                )
                group["count"] += 1
                if prim.component is not None and prim.component == component:
                    group["footprint_vias"] += 1
                stack = via.stack
                if stack is not None:
                    tented = stack.tent_top if pad["layer"] == 1 else stack.tent_bottom
                    group["untented"] += not tented
                name = pad["pad_name"]
                if name and name not in group["pads"]:
                    group["pads"].append(name)
                break
        for (designator, type_id, same_net, allowed), group in groups.items():
            fields = {
                "designator": designator,
                "type": type_id,
                "layer": self.index.layer_name(group["layer"]),
                "count": group["count"],
                "pads": group["pads"][:PAD_SAMPLE] or None,
                "footprint_vias": group["footprint_vias"] or None,
                "untented": group["untented"] or None,
            }
            if allowed is not None:
                fields["rule"] = allowed[0]
            if not same_net:
                self._finding(
                    "via_in_pad", "warning",
                    "Via hole inside an SMD pad of another net: a short.",
                    **fields,
                )
            elif allowed is not None and allowed[1] is False:
                self._finding(
                    "via_in_pad", "warning",
                    f"Via in SMD pad where the {allowed[0]} rule does not allow vias "
                    "under SMD pads.",
                    **fields,
                )
            else:
                self._finding(
                    "via_in_pad", "info",
                    "Via in SMD pad: open or only tented, it wicks solder from the "
                    "joint. Specify filled and capped (IPC-4761 Type VII) or plugged "
                    "vias on the fabrication drawing.",
                    **fields,
                )

    def check_tenting(self) -> None:
        groups: dict[tuple, int] = Counter()
        for via in self.vias:
            stack = via.stack
            if stack is None or (stack.tent_top and stack.tent_bottom):
                continue
            if stack.test_fab_top or stack.test_fab_bottom:
                continue
            state = "one_side" if stack.tent_top or stack.tent_bottom else "none"
            side = "top" if stack.tent_top else "bottom" if stack.tent_bottom else None
            groups[(via.type_id, state, side)] += 1
        for (type_id, state, side), count in groups.items():
            if state == "none":
                issue = (
                    "Untented on both sides and not a test point: the ring is exposed "
                    "to solder. Fine if intended (thermal or probing vias), otherwise "
                    "tent or plug them."
                )
            else:
                issue = (
                    f"Tented on the {side} only: IPC-4761 advises against one-sided "
                    "tenting, which can trap process chemistry in the hole. Tent both "
                    "sides or plug."
                )
            self._finding(
                "tenting", "info", issue, type=type_id, count=count,
                tented=side if state == "one_side" else "none",
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
            x, y, prim.width / 2.0,
            "AND p.net_id = ? AND (p.pour_id IS NULL)", (prim.net,),
        )
        for row in rows:
            kind = row["kind"]
            if kind in ("track", "arc"):
                position = self._in_span(via, row["layer"])
                if position is None:
                    continue
                reach = via.pad_radius(row["layer"]) + row["width"] / 2.0
                if point_seg_distance(x, y, row["x1"], row["y1"], row["x2"], row["y2"]) <= reach + EPS_MIL:
                    connected.add(position)
            elif kind == "pad":
                if row["layer"] == 74:
                    continue
                position = self._in_span(via, row["layer"])
                if position is None:
                    continue
                if self.index._point_primitive_distance(x, y, row) <= via.pad_radius(row["layer"]) + EPS_MIL:
                    connected.add(position)
            elif kind == "via":
                if row["x1"] == x and row["y1"] == y and row["layer"] == prim.layer and row["layer_to"] == prim.layer_to:
                    continue
                if ((row["x1"] - x) ** 2 + (row["y1"] - y) ** 2) ** 0.5 > (row["width"] + prim.width) / 2.0 + EPS_MIL:
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
        """Back drills as {pair, side, start, stop} (stack positions)."""
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
        return result

    def _pair_label(self, item: dict) -> str:
        outer = "Top" if item["side"] == "top" else "Bottom"
        return f"{outer} -> before {self.stack.name(item['stop'])}"

    def _stub(self, via: _Via, side: str, connection: int, end: int) -> float | None:
        if not self.stack.has_heights:
            return None
        order = self.stack.order
        if side == "bottom":
            return self.stack.bottom[order[end]] - self.stack.bottom[order[connection]]
        return self.stack.top[order[connection]] - self.stack.top[order[end]]

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
            "connected_layer": self.stack.name(connection),
            "barrel_end": self.stack.name(end),
            "stub_layers": abs(end - connection),
        }
        reason = None
        if rule_values is not None:
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
            if not use:
                reason = f"the rule does not use {side} back drills"
            elif not outer:
                reason = f"the via does not reach the {side} layer"
            elif not candidates:
                reason = f"the stack has no back drill from the {side}"
            elif not usable:
                reason = (
                    f"connected on {self.stack.name(connection)}, inside the range every "
                    f"{side} back drill removes"
                )
            else:
                best = (
                    min(usable, key=lambda item: item["stop"]) if side == "bottom"
                    else max(usable, key=lambda item: item["stop"])
                )
                end = best["stop"]
                via.back_drilled = self._pair_label(best)
                record["back_drilled"] = via.back_drilled
                record["barrel_end"] = self.stack.name(end)
                record["stub_layers"] = abs(end - connection)
            if reason:
                record["not_back_drilled"] = reason
        record["stub_mil"] = _round(self._stub(via, side, connection, end), 2)
        return record

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
            rule = None
            if rules:
                rule = self._rule("BackDrilling", via)
            high_speed = bool(self.index.diff_pairs_of(via.net)) or (
                self.selection is not None
            )
            if rule is None and not high_speed:
                continue
            checked += 1
            values = self.checker.values(rule) if rule is not None else None
            if rule is not None:
                rule_stats[rule.name]["candidates"] += 1
            stubs = [
                stub for stub in (
                    self._side_stub(via, "top", values),
                    self._side_stub(via, "bottom", values),
                )
                if stub is not None
            ]
            if rule is not None and via.back_drilled:
                rule_stats[rule.name]["back_drilled"] += 1
                via.back_drill_hole = (via.prim.hole or 0.0) + 2 * (
                    values.get("oversize_mil") or 0.0
                )
            if not stubs:
                continue
            worst = max(stubs, key=lambda stub: (stub["stub_mil"] or 0, stub["stub_layers"]))
            if rule is not None:
                limit = values.get("max_stub_length_mil")
                stub = worst["stub_mil"]
                if limit is None or stub is None or stub <= limit + EPS_MIL:
                    continue
                severity, source = "warning", rule.name
            else:
                stub = worst["stub_mil"]
                if stub is None or stub <= high_speed_limit + EPS_MIL:
                    continue
                severity, source, limit = "info", None, high_speed_limit
            key = (via.net, source, worst["side"], worst["connected_layer"],
                   worst.get("not_back_drilled"), worst.get("back_drilled"))
            group = groups.get(key)
            if group is None:
                pairs_of = self.index.diff_pairs_of(via.net)
                groups[key] = group = {
                    "severity": severity,
                    "net": via.net,
                    "pair": pairs_of[0]["name"] if pairs_of else None,
                    "rule": source,
                    "limit": limit,
                    "stub": worst,
                    "count": 0,
                    "types": set(),
                    "at": (via.prim.x1, via.prim.y1),
                }
            group["count"] += 1
            group["types"].add(via.type_id)
            if (worst["stub_mil"] or 0) > (group["stub"]["stub_mil"] or 0):
                group["stub"] = worst
                group["at"] = (via.prim.x1, via.prim.y1)

        if rules and not [item for item in pairs if item["side"]]:
            self._finding(
                "back_drill", "warning",
                "Back-drilling rules exist but the layer stack has no back drill "
                "(Layer Stack Manager, Back Drills tab), so nothing is back drilled.",
                rule=", ".join(rule.name for rule in rules),
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
        for group in groups.values():
            stub = group["stub"]
            if group["rule"]:
                issue = (
                    f"Stub {stub['stub_mil']:.1f} mil below {stub['connected_layer']} "
                    f"({stub['side']} end) exceeds the {group['rule']} rule's max stub "
                    f"{group['limit']:.1f} mil"
                )
                if stub.get("not_back_drilled"):
                    issue += f": not back drilled, {stub['not_back_drilled']}."
                else:
                    issue += f" after back drilling ({stub['back_drilled']})."
            else:
                issue = (
                    f"{'Differential-pair' if group['pair'] else 'Selected'} net with a "
                    f"{stub['stub_mil']:.1f} mil via stub past {stub['connected_layer']} "
                    f"({stub['side']} end) and no back-drilling rule. Stubs resonate at "
                    "multi-gigabit rates; route to the far layer, back drill, or ignore "
                    "for slower signals."
                )
            self._finding(
                "back_drill", group["severity"], issue,
                net=group["net"],
                pair=group["pair"],
                rule=group["rule"],
                count=group["count"],
                types=sorted(group["types"]),
                side=stub["side"],
                connected_layer=stub["connected_layer"],
                barrel_end=stub["barrel_end"],
                stub_mil=stub["stub_mil"],
                max_stub_mil=_round(group["limit"]),
                x_mil=round(group["at"][0], 3),
                y_mil=round(group["at"][1], 3),
            )

        report: dict = {"vias_checked_for_stubs": checked}
        pair_report = []
        drilled = Counter(via.back_drilled for via in self.vias if via.back_drilled)
        holes: dict[str, set] = defaultdict(set)
        for via in self.vias:
            if via.back_drilled and via.back_drill_hole is not None:
                holes[via.back_drilled].add(round(via.back_drill_hole, 3))
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
                    self.stack.thickness_mil - self.stack.bottom[self.stack.order[stop]]
                    if self.stack.has_heights else None
                )
            else:
                drilled_layers = range(0, stop)
                depth = self.stack.top[self.stack.order[stop]] if self.stack.has_heights else None
            pair_report.append(
                {
                    "back_drill": label,
                    "from_layer": "Top" if item["side"] == "top" else "Bottom",
                    "stops_before": self.stack.name(stop),
                    "drilled_layers": [self.stack.name(position) for position in drilled_layers],
                    "depth_mil": _round(depth, 2),
                    "vias": drilled[label],
                    "drill_diameters_mil": sorted(holes[label]) or None,
                }
            )
        if pair_report:
            report["pairs"] = pair_report
        if rules:
            report["rules"] = []
            for rule in rules:
                values = self.checker.values(rule)
                report["rules"].append(
                    {
                        "rule": rule.name,
                        "scope": rule.scope1,
                        "max_stub_length_mil": values.get("max_stub_length_mil"),
                        "oversize_mil": values.get("oversize_mil"),
                        "use_top_back_drill": values.get("use_top_back_drill"),
                        "use_bottom_back_drill": values.get("use_bottom_back_drill"),
                        "candidate_vias": rule_stats[rule.name]["candidates"],
                        "back_drilled_vias": rule_stats[rule.name]["back_drilled"],
                    }
                )
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
    )
    # Via-in-pad and back drilling also feed the inventory, so they always run.
    review.check_in_pad()
    back_drill = review.check_back_drill()
    if "aspect_ratio" in selected:
        review.check_aspect()
    if "annular_ring" in selected:
        review.check_ring()
    if "via_style" in selected:
        review.check_style()
    if "drill_pairs" in selected:
        review.check_drill_pairs()
    if "tenting" in selected:
        review.check_tenting()

    findings = sorted(
        review.findings,
        key=lambda finding: (
            _SEVERITY_ORDER[finding["severity"]],
            CHECKS.index(finding["check"]),
            -finding.get("count", 1),
            str(finding.get("type", "")),
            str(finding.get("net", "")),
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
        "Plugging and filling (IPC-4761 types beyond tenting) are not decoded from the "
        "PcbDoc; tenting is.",
        "Back drilling is recomputed the way Altium does when it writes drill files; "
        "internal planes are not read, so a via connected only to a plane shows no stub.",
    ]
    return result


def page_result(
    full: dict,
    offset: int = 0,
    limit: int = 100,
    type_limit: int = 60,
    class_limit: int = 30,
) -> dict:
    """A page of review_vias' result: findings from offset, the most used via
    types and net classes."""
    result = dict(full)
    offset = max(0, offset)
    findings = full["findings"]
    for key, cap in (("via_types", type_limit), ("net_classes", class_limit)):
        if len(full[key]) > cap:
            result[key] = full[key][:cap]
            result[f"{key}_total"] = len(full[key])
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
    offset: int = 0,
    limit: int = 100,
    type_limit: int = 60,
    class_limit: int = 30,
) -> dict:
    """Via inventory and fab-review findings, paged (see the module docstring)."""
    full = review_vias(
        index, checks, nets, max_aspect_ratio, max_microvia_aspect_ratio,
        min_annular_ring_mil, max_stub_mil,
    )
    if "error" in full:
        return full
    return page_result(full, offset, limit, type_limit, class_limit)
