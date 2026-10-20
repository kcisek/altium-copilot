"""Altium's own Batch DRC results: find the last report, judge whether it can be trusted,
and link its violations to the board.

Altium writes 'Design Rule Check - <PcbDoc>.html' (and '.drc') to the project's output
folder ([Design] OutputPath, by default 'Project Outputs for <project>'). A report is a
snapshot: it can be older than the board, it stops at the board's violation limit
('Stop when n violations found', 500 by default), and it checks only the rule kinds
ticked for batch DRC. The assessment says so before any violation is quoted.
"""

import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

try:
    from parsers.drc_report import HEALTH, RULE, WAIVED, WARNING, parse_drc_report
except ImportError:
    from server.parsers.drc_report import HEALTH, RULE, WAIVED, WARNING, parse_drc_report

# A report names a rule by its description, which starts with the kind's display name;
# each maps to the Rules6 kind name and id. 'verified' kinds were checked against real
# reports; the others come from the description templates in Altium's PCB module and
# only label the rule. The longest prefix wins ('Component Clearance' over 'Clearance').
RULE_KINDS = [
    ("Clearance Constraint", "Clearance", 0, True),
    ("Width Constraint", "Width", 2, True),
    ("Length Constraint", "Length", 3, True),
    ("Matched Lengths", "MatchedLengths", 4, True),
    ("Power Plane Connect Rule", "PlaneConnect", 6, True),
    ("Routing Topology Rule", "RoutingTopology", 7, True),
    ("Routing Layers", "RoutingLayers", 9, True),
    ("Routing Via", "RoutingVias", 11, True),
    ("Short-Circuit Constraint", "ShortCircuit", 15, True),
    ("Un-Routed Net Constraint", "UnRoutedNet", 16, True),
    ("Minimum Annular Ring", "MinimumAnnularRing", 19, True),
    ("Component Clearance Constraint", "ComponentClearance", 24, True),
    ("Hole Size Constraint", "HoleSize", 42, True),
    ("Pads and Vias to follow the Drill pairs settings", "LayerPairs", 48, True),
    ("Height Constraint", "Height", 50, True),
    ("Differential Pairs", "DiffPairsRouting", 51, True),
    ("Hole To Hole Clearance", "HoleToHoleClearance", 52, True),
    ("Minimum Solder Mask Sliver", "MinimumSolderMaskSliver", 53, True),
    ("Silk To Solder Mask", "SilkToSolderMaskClearance", 54, True),
    ("Silk to Silk", "SilkToSilkClearance", 55, True),
    ("Net Antennae", "NetAntennae", 56, True),
    ("Silk primitive without silk layer", "SilkToBoardRegionClearance", 59, True),
    ("Modified Polygon", "UnpouredPolygon", 62, True),
    ("Board Clearance Constraint", "BoardOutlineClearance", 63, True),
    ("Creepage Distance Constraint", "Creepage", 65, True),
    ("Acute Angle Constraint", "AcuteAngle", None, False),
    ("Vias Under SMD Constraint", "ViasUnderSMD", None, False),
    ("Room Definition", "RoomDefinition", None, False),
    ("Max Via Stub Length", "BackDrilling", None, False),
    ("Power Plane Clearance", "PlaneClearance", None, False),
]
SHORT_CIRCUIT, UNROUTED_NET = 15, 16
_VERIFIED = {name for _, name, _, verified in RULE_KINDS if verified}
# Kinds whose violations are about connectivity: nets of the copper they touch count.
_NET_KINDS = {
    "Clearance", "ShortCircuit", "UnRoutedNet", "Width", "DiffPairsRouting", "RoutingVias",
    "NetAntennae", "Creepage", "PlaneConnect", "Length", "RoutingTopology", "RoutingLayers",
}
LIMIT_KB = (
    "https://www.altium.com/documentation/knowledge-base/altium-designer/"
    "batch-drc-reports-no-shorts-when-the-violation-limit-is-reached"
)
GROUP_LIMIT = 50
VIOLATION_LIMIT = 100
SECTION_LIMIT = 20
_SAME_RUN_SECONDS = 60.0
_HOUR = 3600.0
_MULTI_LAYER = 74
# Altium's own wording ('More than %d violations detected, DRC was stopped'), not any
# 'stopped' in a net name.
_STOP_NOTICE = re.compile(
    r"DRC was stopped|violations detected, DRC|maximum violation", re.IGNORECASE
)

_COUNT_PROBLEMS = re.compile(r"(?:1 table lists|\d+ tables list|the rule counts) ")
_PAD = re.compile(r"\bPad (\S+?)\(")
_COMPONENT = re.compile(r"\bComponent (\S+)")
_NET = re.compile(r"\b(?:Between|And) Net (\S+)")  # Matched Lengths rows
_PAIR = re.compile(r"\b(?:Differential Pair|diff pair) (\S+?)[.,]?(?=\s|$)")
_REGION_LAYER = re.compile(r"hole\(s\)\)\s+(.+?)(?:\s+(?:And|Location)\b|$)")
_COORD = re.compile(r"\((-?[\d.]+)(mm|mil)\s*,\s*(-?[\d.]+)(mm|mil)\)")


def rule_kind(title: str) -> tuple[str | None, int | None]:
    """The Rules6 kind (name, id) of a report rule description."""
    best = None
    for prefix, name, kind_id, _ in RULE_KINDS:
        if title.lower().startswith(prefix.lower()) and (
            best is None or len(prefix) > len(best[0])
        ):
            best = (prefix, name, kind_id)
    return (best[1], best[2]) if best else (None, None)


# ---------- finding the report ----------

def find_reports(project: dict, pcb_path: Path) -> tuple[list[Path], list[str]]:
    """This board's DRC reports, newest first, and the folders searched. Without one in
    the output folder, the project folder is searched (a stale OutputPath is common)."""
    prj = Path(project["prj_pcb_path"])
    folders: list[Path] = []
    output = (project.get("output_path") or "").strip()
    if output:
        path = Path(output)
        folders.append(path if path.is_absolute() else prj.parent / path)
    folders.append(prj.parent / f"Project Outputs for {prj.stem}")
    names = [f"Design Rule Check - {pcb_path.stem}{ext}" for ext in (".html", ".drc")]
    found: list[Path] = []
    for folder in dict.fromkeys(folders):
        found.extend(folder / name for name in names if (folder / name).is_file())
    if not found:
        try:
            found = [
                path for name in names for path in prj.parent.rglob(name) if path.is_file()
            ]
        except OSError:
            found = []
    found.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return found, [str(folder) for folder in dict.fromkeys(folders)]


def pick_report(found: list[Path]) -> Path | None:
    """The newest report; of an HTML and .drc pair from one run, the HTML (it has the
    zoom boxes)."""
    if not found:
        return None
    newest = found[0]
    for path in found:
        if path.suffix.lower() == ".html" and abs(
            path.stat().st_mtime - newest.stat().st_mtime
        ) <= _SAME_RUN_SECONDS:
            return path
    return newest


def load_report(project: dict, pcb_path: Path):
    """(report, path, other reports, folders searched, notes); report None when absent.

    An HTML that is empty or incomplete (Altium writes an empty one when the path is
    over 256 characters) gives way to the .drc of the same run.
    """
    found, searched = find_reports(project, pcb_path)
    path = pick_report(found)
    if path is None:
        return None, None, [], searched, []
    notes: list[dict] = []
    report = parse_drc_report(path)
    if report.problems and path.suffix.lower() == ".html":
        twin = next((
            other for other in found
            if other.suffix.lower() == ".drc"
            and abs(other.stat().st_mtime - path.stat().st_mtime) <= _SAME_RUN_SECONDS
        ), None)
        if twin is not None:
            fallback = parse_drc_report(twin)
            if not fallback.problems:
                notes.append(_issue(
                    "html_report_unusable", "info",
                    f"The HTML report is unusable ({report.problems[0]}); the .drc from "
                    "the same run is read instead.",
                ))
                report, path = fallback, twin
    if str(path.parent) not in searched:
        notes.append(_issue(
            "report_outside_output_folder", "warning",
            f"No report was in the project's output folder; this one was found in "
            f"{path.parent.name}, which may belong to another or older project.",
        ))
    return report, path, [p for p in found if p != path], searched, notes


# ---------- assessment ----------

def _utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _issue(issue: str, severity: str, detail: str, **fields) -> dict:
    return {"issue": issue, "severity": severity, **fields, "detail": detail}


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _eco_logs_after(project: dict, pcb_path: Path, saved: float) -> list[Path]:
    """This board's Update PCB logs ('<PcbDoc> PCB ECO <date>.LOG') after the report."""
    prj = Path(project["prj_pcb_path"])
    folder = (project.get("log_folder_path") or "").strip()
    folders = [prj.parent / folder] if folder else []
    folders.append(prj.parent / f"Project Logs for {prj.stem}")
    name = re.compile(rf"^{re.escape(pcb_path.stem)} PCB ECO .*\.log$", re.IGNORECASE)
    logs = []
    for path in dict.fromkeys(folders):
        try:
            logs.extend(
                log for log in path.iterdir()
                if name.match(log.name) and log.stat().st_mtime > saved + _SAME_RUN_SECONDS
            )
        except OSError:
            continue
    return logs


def _stop_notices(report) -> list[str]:
    """The report's own word that the DRC stopped early (an error or a warning)."""
    texts = list(report.errors) + [
        text for table in report.tables if table.type == WARNING
        for text in (table.title, *(row.message for row in table.violations))
    ]
    return [text for text in texts if _STOP_NOTICE.search(text)]


def _same_file(first: str, second: Path) -> bool | None:
    """Whether two paths are one file; None when the first is not on this machine."""
    try:
        return os.path.samefile(first, second)
    except OSError:
        return None


def count_problem(problem: str) -> bool:
    """A count that does not add up, as against a report that is empty or cut short."""
    return bool(_COUNT_PROBLEMS.match(problem))


def assess(report, report_path: Path, pcb_path: Path, project: dict, pcb=None) -> list[dict]:
    """What limits trust in the report: incomplete, stopped early, kinds not checked,
    older than the board, another board, rules changed since."""
    issues: list[dict] = []
    broken = [p for p in report.problems if not count_problem(p)]
    if broken:
        issues.append(_issue(
            "report_incomplete", "error",
            "The report is incomplete (" + "; ".join(report.problems) + "): it cannot "
            "show the board is clean. Altium writes an empty report when the path is over "
            "256 characters or its report templates are missing; run the DRC again.",
        ))
    elif report.problems:
        issues.append(_issue(
            "report_counts_differ", "warning",
            "The rows read do not match the report's own counts ("
            + "; ".join(report.problems) + "): some violations may be missing or extra "
            "here; check the report file itself.",
        ))
    options = getattr(pcb, "drc_options", None) if pcb is not None else None
    kinds = Counter(rule_kind(table.title)[1] for table in report.tables if table.type == RULE)
    total = report.totals.get(
        "rule_violations",
        sum(table.count for table in report.tables if table.type == RULE),
    )

    for error in report.errors:
        issues.append(_issue("report_error", "error", f"The report says: {error}."))
    limit = options.max_violations if options else None
    notices = _stop_notices(report)
    if notices:
        now = f"; the board's limit is now {limit}" if limit else ""
        issues.append(_issue(
            "violation_limit_reached", "error",
            f"The report says the DRC stopped early ({notices[0]}){now}: rules processed "
            "after the stop were not checked and can read 0, short circuits included. "
            "Raise 'Stop when n violations found' and run the DRC again.",
            violations=total, limit=limit, reference=LIMIT_KB,
        ))
    elif limit and limit <= total <= limit + 10:
        issues.append(_issue(
            "violation_limit_reached", "error",
            f"The DRC found {total} violations and the board stops at {limit} ('Stop when "
            "n violations found'), so it most likely stopped early: rules after the stop "
            "were not checked and can read 0, short circuits included. The report does not "
            "record the limit it ran with. Raise the limit and run the DRC again.",
            violations=total, limit=limit, reference=LIMIT_KB,
        ))
    elif limit and total > limit + 10:
        issues.append(_issue(
            "violation_limit_lowered", "info",
            f"The report holds {total} violations, more than the board's limit of {limit}: "
            "the limit was lowered after the run, which then was not cut short by it.",
            violations=total, limit=limit,
        ))

    batch = options.batch_kinds if options else None
    enabled = Counter()
    if pcb is not None:
        enabled = Counter(rule.kind_id for rule in pcb.rules if rule.enabled)
    for kind_id, label in ((UNROUTED_NET, "Un-Routed Net"), (SHORT_CIRCUIT, "Short-Circuit")):
        if kinds.get(kind_id):
            continue
        if batch is not None and kind_id not in batch:
            reason = (
                f"{label} is not ticked for batch DRC on this board, so the report never "
                "checked it"
            )
        elif pcb is not None and not enabled.get(kind_id):
            reason = f"the board has no enabled {label} rule"
        elif broken:
            reason = "the report is incomplete, so it may be in the missing part"
        else:
            reason = "it is not in the report (the run stopped, or settings changed since)"
        extra = (
            "; unconnected nets, broken planes and dead copper were not checked"
            if kind_id == UNROUTED_NET else "; shorts were not checked"
        )
        issues.append(_issue(
            "rule_kind_not_checked", "warning", f"{label}: {reason}{extra}.", rule_kind=label,
        ))

    report_saved = report_path.stat().st_mtime
    pcb_saved = pcb_path.stat().st_mtime if pcb_path.is_file() else None
    if pcb_saved is None:
        issues.append(_issue(
            "pcb_file_missing", "warning",
            f"{pcb_path.name} is not on disk, so the report cannot be dated against it.",
        ))
    elif pcb_saved > report_saved + _HOUR:
        issues.append(_issue(
            "older_than_board", "warning",
            "The PcbDoc was saved more than an hour after this report, so the report may "
            "be stale (a checkout also moves the time). Run the DRC again before relying "
            "on it.",
            report_saved_utc=_utc(report_saved), pcb_saved_utc=_utc(pcb_saved),
        ))
    elif pcb_saved > report_saved + _SAME_RUN_SECONDS:
        issues.append(_issue(
            "saved_after_report", "info",
            "The PcbDoc was saved shortly after this report: usually the save that follows "
            "the DRC run (it stores its results in the board). If the board was edited "
            "since, run the DRC again.",
            report_saved_utc=_utc(report_saved), pcb_saved_utc=_utc(pcb_saved),
        ))
    logs = _eco_logs_after(project, pcb_path, report_saved)
    if logs:
        issues.append(_issue(
            "eco_after_report", "warning",
            f"{_plural(len(logs), 'Update PCB (ECO) run')} logged after this report: the "
            "board changed since it was checked.",
        ))
    if report.pcb_file:
        same = _same_file(report.pcb_file, pcb_path)
        name = re.split(r"[\\/]", report.pcb_file)[-1]
        if same is False or (same is None and name.lower() != pcb_path.name.lower()):
            issues.append(_issue(
                "other_board", "warning",
                f"The report was made for {report.pcb_file}, not {pcb_path}.",
            ))
    if pcb is not None and batch is not None and not broken:
        board = Counter(
            rule.kind for rule in pcb.rules
            if rule.enabled and rule.kind_id in batch and rule.kind in _VERIFIED
        )
        reported = Counter(
            name for table in report.tables if table.type == RULE
            for name in [rule_kind(table.title)[0]] if name in _VERIFIED
        )
        changed = sorted(
            name for name in set(board) | set(reported) if board[name] != reported[name]
        )
        if changed:
            issues.append(_issue(
                "rules_changed", "info",
                "The board's batch-checked rules differ from those in the report ("
                + "; ".join(
                    f"{name}: {reported[name]} in the report, {board[name]} on the board"
                    for name in changed[:8]
                )
                + "): rules changed since, or the run stopped.",
            ))
    has_health = any(table.type == HEALTH for table in report.tables) or (
        "health_issues" in report.totals
    )
    if not has_health and not broken:
        issues.append(_issue(
            "no_health_section", "info",
            "The report has no PCB health section ('Report PCB Health Issues' was off, or "
            "the Altium version that ran it does not offer it).",
        ))
    if options is not None and options.make_report is False:
        issues.append(_issue(
            "report_file_off", "info",
            "'Create Report File' is off on this board: running the DRC now writes no "
            "report, so this one is from before it was turned off.",
        ))
    return issues


# ---------- linking violations to the board ----------

def _inside(point: tuple[float, float], vertices) -> bool:
    """Point in polygon (ray casting)."""
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(vertices, vertices[1:] + vertices[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _edge_distance(point: tuple[float, float], vertices) -> float:
    """Distance from a point to a polygon's outline."""
    x, y = point
    best = float("inf")
    for (x1, y1), (x2, y2) in zip(vertices, vertices[1:] + vertices[:1]):
        dx, dy = x2 - x1, y2 - y1
        length = dx * dx + dy * dy
        t = 0.0 if not length else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / length))
        best = min(best, ((x - x1 - t * dx) ** 2 + (y - y1 - t * dy) ** 2) ** 0.5)
    return best


class DrcLinker:
    """Ties violation messages to board parts, nets and locations."""

    def __init__(self, index):
        self.index = index
        self.designators: dict[str, str] = {}
        self.pairs: dict[str, tuple[str, str]] = {}
        self.layers: list[tuple[str, int]] = []
        self.extents: tuple[float, float, float, float] | None = None
        self.origin: tuple[float, float] | None = None
        if index is None:
            return
        pcb = index.pcb
        for component in pcb.components:
            name = component.designator or component.refdes
            if name:
                self.designators.setdefault(name.lower(), name)
        for pair in pcb.diff_pairs:
            self.pairs[pair.name.lower()] = (pair.positive_net, pair.negative_net)
        self.layers = sorted(
            {(layer.name, layer.v6_id) for layer in pcb.board.layers
             if layer.name and layer.v6_id is not None},
            key=lambda item: -len(item[0]),
        )
        self.origin = (pcb.board.origin_x or 0.0, pcb.board.origin_y or 0.0)
        # The outline's vertices (its arcs can bulge past them) and the copper together.
        xs = [v[0] for v in pcb.board.outline_vertices or []]
        ys = [v[1] for v in pcb.board.outline_vertices or []]
        row = index.db.execute(
            "SELECT MIN(minx) a, MIN(miny) b, MAX(maxx) c, MAX(maxy) d FROM prims"
        ).fetchone()
        if row["a"] is not None:
            xs += [row["a"], row["c"]]
            ys += [row["b"], row["d"]]
        if xs and max(xs) - min(xs) > 1 and max(ys) - min(ys) > 1:
            self.extents = (min(xs), min(ys), max(xs), max(ys))

    def split(self, token: str) -> tuple[str | None, str]:
        """'U1-5' -> ('U1', '5'), by the longest board designator before a '-'
        (designators and pads can hold '-' themselves)."""
        best = None
        for at in [i for i, char in enumerate(token) if char == "-"]:
            name = self.designators.get(token[:at].lower())
            if name and (best is None or at > best[1]):
                best = (name, at)
        if best:
            return best[0], token[best[1] + 1:]
        name = self.designators.get(token.lower())
        return (name, "") if name else (None, "")

    def _named_layers(self, message: str) -> set[int]:
        lower = message.lower()
        named = set()
        for name, v6 in self.layers:  # longest first: 'L10' before 'L1'
            pattern = re.compile(r"(?:on |from |to |\) )" + re.escape(name.lower()) + r"(?![\w-])")
            match = pattern.search(lower)
            if match:
                named.add(v6)
                lower = lower[: match.start()] + " " * len(match[0]) + lower[match.end():]
        if "multi-layer" in lower:
            named.add(_MULTI_LAYER)
        return named

    def _trusted(self, box, kind: str | None) -> bool:
        """A zoom box marks the violation, except for shorts (Altium gives those a
        board-sized box around a point relative to the origin) and boxes larger than the
        board."""
        if box is None or kind == "ShortCircuit":
            return False
        if self.extents is None:
            return True
        width, height = self.extents[2] - self.extents[0], self.extents[3] - self.extents[1]
        return box[2] - box[0] <= width * 1.1 and box[3] - box[1] <= height * 1.1

    def link(self, message: str, box, kind: str | None) -> dict:
        designators: list[str] = []
        nets: list[str] = []
        for token in _PAD.findall(message):
            designator, pad = self.split(token)
            if designator:
                designators.append(designator)
                if self.index is not None and pad:
                    net = self.index.pad_net(f"{designator}-{pad}")
                    if net:
                        nets.append(net)
        for token in _COMPONENT.findall(message):
            designator, _ = self.split(token)
            if designator:
                designators.append(designator)
        nets.extend(_NET.findall(message))
        pairs = []
        for token in _PAIR.findall(message):
            pair = self.pairs.get(token.lower())
            pairs.append(token)
            if pair:
                nets.extend(net for net in pair if net)
        location = None
        if self.index is None:
            pass  # no board: neither its frame nor its copper
        elif self._trusted(box, kind):
            location = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
            if kind in _NET_KINDS:
                nets.extend(self._box_nets(box, message))
                if "polygon" in message.lower():
                    reach = ((box[2] - box[0]) ** 2 + (box[3] - box[1]) ** 2) ** 0.5 / 2
                    nets.extend(self._pour_nets(location, message, reach))
        elif self.origin is not None:
            # The first object's coordinates, relative to the board origin.
            match = _COORD.search(message)
            if match:
                scale = {"mm": 1000 / 25.4, "mil": 1.0}
                location = (
                    float(match[1]) * scale[match[2]] + self.origin[0],
                    float(match[3]) * scale[match[4]] + self.origin[1],
                )
                if kind in _NET_KINDS and "polygon" in message.lower():
                    nets.extend(self._pour_nets(location, message))
        linked: dict = {}
        if designators:
            linked["components"] = list(dict.fromkeys(designators))
        if nets:
            linked["nets"] = list(dict.fromkeys(nets))
        if pairs:
            linked["diff_pairs"] = list(dict.fromkeys(pairs))
        if location:
            linked["location_mil"] = {
                "x_mil": round(location[0], 2), "y_mil": round(location[1], 2),
            }
        return linked

    def _box_nets(self, box, message: str, margin: float = 0.5) -> list[str]:
        """Nets of the copper the violation touches: on the layers the message names,
        within the zoom box (not just its bounding box), pour outlines left out."""
        if self.index is None:
            return []
        rows = self.index.db.execute(
            "SELECT p.*, n.name AS net_name FROM prims p JOIN nets n ON n.id = p.net_id "
            "WHERE p.maxx >= ? AND p.minx <= ? AND p.maxy >= ? AND p.miny <= ? "
            "AND p.pour_id IS NULL",
            (box[0] - margin, box[2] + margin, box[1] - margin, box[3] + margin),
        ).fetchall()
        named = self._named_layers(message)
        centre = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        reach = ((box[2] - box[0]) ** 2 + (box[3] - box[1]) ** 2) ** 0.5 / 2 + margin
        nets = []
        for row in rows:
            row_layers = self.index._primitive_layers(row)
            on_named = [layer for layer in row_layers if layer in named]
            if named and not on_named and not (
                _MULTI_LAYER in named and row["layer"] == _MULTI_LAYER
            ):
                continue
            # A multi-layer pad's copper can differ per layer: measured on
            # the named layers, else on every layer it occupies.
            if self.index._point_primitive_distance(
                centre[0], centre[1], row, on_named or row_layers
            ) > reach:
                continue
            nets.append(row["net_name"])
        nets = list(dict.fromkeys(nets))
        return nets if len(nets) <= 3 else []

    def _pour_nets(self, point, message: str, reach: float = 0.0) -> list[str]:
        """For a violation against a polygon: the net of the pour on the layer the
        message names that holds the point, or whose edge is within reach of it."""
        if self.index is None:
            return []
        match = _REGION_LAYER.search(message)
        if not match:
            return []
        layer = self.index.resolve_layer(match[1].strip())
        nets = []
        for pour in self.index.pcb.pours:
            if (
                pour.layer != layer
                or pour.net is None
                or pour.shelved
                or len(pour.vertices) < 3
            ):
                continue
            vertices = list(pour.vertices)
            if _inside(point, vertices) or (reach and _edge_distance(point, vertices) <= reach):
                nets.append(self.index.pcb.nets[pour.net])
        return nets


# ---------- summaries ----------

def _violation(table, violation, linker) -> dict:
    kind_name, _ = rule_kind(table.title)
    row = {"message": violation.message,
           **linker.link(violation.message, violation.box_mil, kind_name)}
    if violation.waiver:
        row["waiver"] = violation.waiver
    return row


def _top(counter: Counter, limit: int = 10) -> dict:
    return dict(counter.most_common(limit))


def _page(rows: list, offset: int, limit: int) -> dict:
    page = {"total": len(rows), "offset": offset, "has_more": offset + limit < len(rows)}
    if page["has_more"]:
        page["next_offset"] = offset + limit
    return page


def summarize(
    report, linker, group_by: str = "rule", rule: str | None = None,
    offset: int = 0, limit: int | None = None,
) -> dict:
    """Groups of rule violations (by rule, component or net), or the violations of the
    rules (or health, waived and warning tables) a rule filter matches."""
    rule_tables = [table for table in report.tables if table.type == RULE]
    result: dict = {
        "rules_checked": len(rule_tables),
        "rules_with_violations": sum(
            1 for table in rule_tables if table.count or table.violations
        ),
    }
    offset = max(0, int(offset or 0))
    if rule:
        needle = rule.strip().lower()
        # The closest reading of what was asked: the whole description, its start, the
        # rule kind, then any part of the description.
        matches = []
        for test in (
            lambda title: title.strip().lower() == needle,
            lambda title: title.lower().startswith(needle),
            lambda title: (rule_kind(title)[0] or "").lower() == needle,
            lambda title: needle in title.lower(),
        ):
            matches = [table for table in report.tables if test(table.title)]
            if matches:
                break
        if not matches:
            return {
                "error": "rule_not_in_report",
                "message": f"No rule or section in the report matches '{rule}'.",
                "rules": [table.title for table in rule_tables if table.count][:50],
            }
        rows = [
            {"rule": table.title,
             **({"section": table.type} if table.type != RULE else {}),
             **_violation(table, violation, linker)}
            for table in matches for violation in table.violations
        ]
        limit = limit or VIOLATION_LIMIT
        result["rules"] = [
            {"rule": table.title, "rule_kind": rule_kind(table.title)[0], "count": table.count,
             **({"section": table.type} if table.type != RULE else {})}
            for table in matches
        ]
        result["violations"] = rows[offset : offset + limit]
        result.update(_page(rows, offset, limit))
        return result

    groups: dict = {}
    for position, table in enumerate(rule_tables):
        if not (table.count or table.violations):
            continue
        kind_name, _ = rule_kind(table.title)
        if group_by == "rule":
            # Two rules can share a description; each keeps its own group.
            groups[position] = {
                "rule": table.title, "count": 0, "kinds": Counter(),
                "components": Counter(), "nets": Counter(), "examples": [],
                "reported": table.count,
            }
        for violation in table.violations:
            linked = _violation(table, violation, linker)
            if group_by == "rule":
                keys = [position]
            elif group_by == "component":
                keys = linked.get("components") or ["(no component)"]
            else:
                keys = linked.get("nets") or ["(no net)"]
            for key in keys:
                group = groups.setdefault(key, {
                    group_by: key, "count": 0, "kinds": Counter(),
                    "components": Counter(), "nets": Counter(), "examples": [],
                })
                group["count"] += 1
                group["kinds"][kind_name or table.title] += 1
                group["components"].update(linked.get("components", []))
                group["nets"].update(linked.get("nets", []))
                if len(group["examples"]) < 3:
                    group["examples"].append(linked | {"message": violation.message})
    ordered = list(groups.values())
    if group_by != "rule":
        ordered.sort(key=lambda group: (group[group_by].startswith("("), -group["count"]))
    limit = limit or GROUP_LIMIT
    page = []
    for group in ordered[offset : offset + limit]:
        entry: dict = {group_by: group[group_by], "count": group["count"]}
        if group_by == "rule":
            entry["rule_kind"] = rule_kind(group["rule"])[0]
            if group["reported"] != group["count"]:
                entry["reported_count"] = group["reported"]
        else:
            entry["by_rule_kind"] = _top(group["kinds"])
        if group_by != "component" and group["components"]:
            entry["components"] = _top(group["components"])
        if group_by != "net" and group["nets"]:
            entry["nets"] = _top(group["nets"])
        entry["examples"] = group["examples"]
        page.append(entry)
    result["groups"] = page
    result.update(_page(ordered, offset, limit))
    if group_by != "rule":
        result["grouping_note"] = (
            f"A violation counts in the group of each {group_by} it names, so group counts "
            "can add up to more than totals.rule_violations."
        )
    return result


def other_sections(report, linker, limit: int = SECTION_LIMIT) -> dict:
    """Health issues, waived violations and the report's own warnings (all rows of a
    table: pass its title as rule)."""
    out: dict = {}
    for kind, key in ((HEALTH, "health_issues"), (WAIVED, "waived"), (WARNING, "warnings")):
        tables = [table for table in report.tables if table.type == kind]
        if not tables:
            continue
        out[key] = []
        for table in tables:
            entry = {
                "title": table.title,
                "count": table.count,
                "examples": [_violation(table, violation, linker)
                             for violation in table.violations[:limit]],
            }
            if len(table.violations) > limit:
                entry["omitted"] = len(table.violations) - limit
            out[key].append(entry)
    return out
