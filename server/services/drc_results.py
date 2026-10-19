"""Altium's own Batch DRC results: find the last report, judge whether it can be trusted,
and link its violations to the board.

Altium writes 'Design Rule Check - <PcbDoc>.html' (and '.drc') to the project's output
folder ([Design] OutputPath, by default 'Project Outputs for <project>'). A report is a
snapshot: it can be older than the board, it stops at the board's violation limit
('Stop when n violations found', 500 by default), and it checks only the rule kinds
ticked for batch DRC. The assessment says so before any violation is quoted.
"""

import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

try:
    from parsers.drc_report import HEALTH, RULE, WAIVED, WARNING, parse_drc_report
except ImportError:
    from server.parsers.drc_report import HEALTH, RULE, WAIVED, WARNING, parse_drc_report

# A report names a rule by its description, which starts with the kind's display name;
# each maps to the Rules6 kind (name, id). Checked against real reports for every kind
# listed; the longest prefix wins ('Component Clearance' over 'Clearance').
RULE_KINDS = [
    ("Clearance Constraint", "Clearance", 0),
    ("Width Constraint", "Width", 2),
    ("Length Constraint", "Length", 3),
    ("Matched Lengths", "MatchedLengths", 4),
    ("Power Plane Connect Rule", "PlaneConnect", 6),
    ("Routing Topology Rule", "RoutingTopology", 7),
    ("Routing Layers", "RoutingLayers", 9),
    ("Routing Via", "RoutingVias", 11),
    ("Short-Circuit Constraint", "ShortCircuit", 15),
    ("Un-Routed Net Constraint", "UnRoutedNet", 16),
    ("Component Clearance Constraint", "ComponentClearance", 24),
    ("Hole Size Constraint", "HoleSize", 42),
    ("Pads and Vias to follow the Drill pairs settings", "LayerPairs", 48),
    ("Height Constraint", "Height", 50),
    ("Differential Pairs", "DiffPairsRouting", 51),
    ("Hole To Hole Clearance", "HoleToHoleClearance", 52),
    ("Minimum Solder Mask Sliver", "MinimumSolderMaskSliver", 53),
    ("Silk To Solder Mask", "SilkToSolderMaskClearance", 54),
    ("Silk to Silk", "SilkToSilkClearance", 55),
    ("Net Antennae", "NetAntennae", 56),
    ("Silk primitive without silk layer", "SilkToBoardRegionClearance", 59),
    ("Modified Polygon", "UnpouredPolygon", 62),
    ("Board Clearance Constraint", "BoardOutlineClearance", 63),
    ("Creepage Distance Constraint", "Creepage", 65),
]
SHORT_CIRCUIT, UNROUTED_NET = 15, 16
# Kinds whose violations are about connectivity: nets in the zoom box are worth naming.
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
_STALE_SECONDS = 60.0
_HOUR = 3600.0

_PAD = re.compile(r"\bPad (\S+?)\(")
_COMPONENT = re.compile(r"\bComponent (\S+)")
_NET = re.compile(r"\b(?:Between|And) Net (\S+)")  # Matched Lengths rows
_COORD = re.compile(r"\((-?[\d.]+)(mm|mil)\s*,\s*(-?[\d.]+)(mm|mil)\)")


def rule_kind(title: str) -> tuple[str | None, int | None]:
    """The Rules6 kind (name, id) of a report rule description."""
    best = None
    for prefix, name, kind_id in RULE_KINDS:
        if title.lower().startswith(prefix.lower()) and (
            best is None or len(prefix) > len(best[0])
        ):
            best = (prefix, name, kind_id)
    return (best[1], best[2]) if best else (None, None)


def find_reports(project: dict, pcb_path: Path) -> tuple[list[Path], list[str]]:
    """This board's DRC reports, newest first, and the folders searched."""
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
        ) <= _STALE_SECONDS:
            return path
    return newest


def _utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _issue(issue: str, severity: str, detail: str, **fields) -> dict:
    return {"issue": issue, "severity": severity, **fields, "detail": detail}


def _eco_logs_after(project: dict, pcb_path: Path, saved: float) -> list[Path]:
    """ECO logs (Update PCB runs) written after the report."""
    prj = Path(project["prj_pcb_path"])
    folder = (project.get("log_folder_path") or "").strip()
    folders = [prj.parent / folder] if folder else []
    folders.append(prj.parent / f"Project Logs for {prj.stem}")
    logs = []
    for path in dict.fromkeys(folders):
        try:
            logs.extend(
                log for log in path.glob("*ECO*.[Ll][Oo][Gg]")
                if log.stat().st_mtime > saved + _STALE_SECONDS
            )
        except OSError:
            continue
    return logs


def assess(report, report_path: Path, pcb_path: Path, project: dict, pcb=None) -> list[dict]:
    """What limits trust in the report: stopped early, kinds not checked, older than the
    board, another board, rules changed since."""
    issues: list[dict] = []
    options = getattr(pcb, "drc_options", None) if pcb is not None else None
    kinds = Counter(rule_kind(table.title)[1] for table in report.tables if table.type == RULE)
    total = report.totals.get(
        "rule_violations",
        sum(table.count for table in report.tables if table.type == RULE),
    )

    for error in report.errors:
        issues.append(_issue(
            "report_error", "error",
            f"The report says: {error}. A stopped DRC did not check every rule.",
        ))
    limit = options.max_violations if options else None
    stopped = any(
        re.search(r"stopped|maximum violation", error, re.IGNORECASE) for error in report.errors
    )
    if stopped or (limit and total >= limit):
        issues.append(_issue(
            "violation_limit_reached", "error",
            f"The DRC found {total} violations and the board stops at {limit or 'its limit'} "
            "('Stop when n violations found'): it stopped early, so rules after the stop "
            "were not checked and can read 0, short circuits included. Raise the limit in "
            "Tools > Design Rule Check and run it again.",
            violations=total, limit=limit, reference=LIMIT_KB,
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
    pcb_saved = pcb_path.stat().st_mtime
    if pcb_saved > report_saved + _HOUR:
        issues.append(_issue(
            "older_than_board", "warning",
            "The PcbDoc was saved more than an hour after this report, so the report may "
            "be stale (a checkout also moves the time). Run the DRC again before relying "
            "on it.",
            report_saved_utc=_utc(report_saved), pcb_saved_utc=_utc(pcb_saved),
        ))
    elif pcb_saved > report_saved + _STALE_SECONDS:
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
            f"{len(logs)} Update PCB (ECO) runs are logged after this report: the board "
            "changed since it was checked.",
        ))
    if report.pcb_file:
        name = re.split(r"[\\/]", report.pcb_file)[-1]
        if name.lower() != pcb_path.name.lower():
            issues.append(_issue(
                "other_board", "warning",
                f"The report was made for {name}, not {pcb_path.name}.",
            ))
    if pcb is not None and batch is not None:
        board = Counter(
            rule.kind_id for rule in pcb.rules if rule.enabled and rule.kind_id in batch
        )
        changed = sorted(
            kind_id for kind_id in set(board) | set(kinds)
            if kind_id is not None and board.get(kind_id, 0) != kinds.get(kind_id, 0)
        )
        if changed:
            names = {kind_id: name for _, name, kind_id in RULE_KINDS}
            issues.append(_issue(
                "rules_changed", "info",
                "The board's batch-checked rules differ from those in the report "
                + ", ".join(
                    f"{names.get(k, k)}: {kinds.get(k, 0)} in the report, "
                    f"{board.get(k, 0)} on the board" for k in changed[:8]
                )
                + ": rules changed since, or the run stopped.",
            ))
    if options is not None and options.include_health is False:
        issues.append(_issue(
            "health_not_reported", "info",
            "'Report PCB Health Issues' is off, so the report lists no health issues.",
        ))
    if options is not None and options.make_report is False:
        issues.append(_issue(
            "report_file_off", "info",
            "'Create Report File' is off on this board: running the DRC now writes no "
            "report, so this one is from before it was turned off.",
        ))
    return issues


class DrcLinker:
    """Ties violation messages to board parts and nets."""

    def __init__(self, index):
        self.index = index
        self.designators: dict[str, str] = {}
        if index is not None:
            for component in index.pcb.components:
                name = component.designator or component.refdes
                if name:
                    self.designators.setdefault(name.lower(), name)
        board = index.pcb.board if index is not None else None
        self.origin = (
            (board.origin_x or 0.0, board.origin_y or 0.0) if board is not None else (0.0, 0.0)
        )

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
        location = None
        if box is not None:
            location = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
            if kind in _NET_KINDS:
                nets.extend(self._box_nets(box))
        else:
            match = _COORD.search(message)
            if match:
                scale = {"mm": 1000 / 25.4, "mil": 1.0}
                location = (
                    float(match[1]) * scale[match[2]] + self.origin[0],
                    float(match[3]) * scale[match[4]] + self.origin[1],
                )
        linked: dict = {}
        if designators:
            linked["components"] = list(dict.fromkeys(designators))
        if nets:
            linked["nets"] = list(dict.fromkeys(nets))
        if location:
            linked["location_mil"] = {
                "x_mil": round(location[0], 2), "y_mil": round(location[1], 2),
            }
        return linked

    def _box_nets(self, box, margin: float = 0.5) -> list[str]:
        """Nets of the copper the zoom box touches, when they are few enough to name."""
        if self.index is None:
            return []
        rows = self.index.db.execute(
            "SELECT DISTINCT n.name FROM prims p JOIN nets n ON n.id = p.net_id "
            "WHERE p.maxx >= ? AND p.minx <= ? AND p.maxy >= ? AND p.miny <= ?",
            (box[0] - margin, box[2] + margin, box[1] - margin, box[3] + margin),
        ).fetchall()
        names = [row["name"] for row in rows]
        return names if len(names) <= 3 else []


def _violation(table, violation, linker) -> dict:
    kind_name, _ = rule_kind(table.title)
    row = {"message": violation.message, **linker.link(violation.message, violation.box_mil, kind_name)}
    if violation.waiver:
        row["waiver"] = violation.waiver
    return row


def _top(counter: Counter, limit: int = 10) -> dict:
    return dict(counter.most_common(limit))


def summarize(
    report, linker, group_by: str = "rule", rule: str | None = None,
    offset: int = 0, limit: int | None = None,
) -> dict:
    """Groups of rule violations (by rule, component or net), or one rule's violations."""
    rule_tables = [table for table in report.tables if table.type == RULE]
    result: dict = {
        "rules_checked": len(rule_tables),
        "rules_with_violations": sum(1 for table in rule_tables if table.count),
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
            matches = [table for table in rule_tables if test(table.title)]
            if matches:
                break
        if not matches:
            return {
                "error": "rule_not_in_report",
                "message": f"No rule in the report matches '{rule}'.",
                "rules": [table.title for table in rule_tables if table.count][:50],
            }
        rows = [
            {"rule": table.title, **_violation(table, violation, linker)}
            for table in matches for violation in table.violations
        ]
        limit = limit or VIOLATION_LIMIT
        result["rules"] = [
            {"rule": table.title, "rule_kind": rule_kind(table.title)[0], "count": table.count}
            for table in matches
        ]
        page = rows[offset : offset + limit]
        result.update({"violations": page, "total": len(rows), "offset": offset,
                       "has_more": offset + limit < len(rows)})
        if result["has_more"]:
            result["next_offset"] = offset + limit
        return result

    groups: dict[str, dict] = {}
    for table in rule_tables:
        if not table.count:
            continue
        kind_name, _ = rule_kind(table.title)
        for violation in table.violations:
            linked = _violation(table, violation, linker)
            if group_by == "rule":
                keys = [table.title]
            elif group_by == "component":
                keys = linked.get("components") or ["(no component)"]
            else:
                keys = linked.get("nets") or ["(no net)"]
            for key in keys:
                group = groups.setdefault(key, {
                    "count": 0, "kinds": Counter(), "components": Counter(),
                    "nets": Counter(), "examples": [],
                })
                group["count"] += 1
                group["kinds"][kind_name or table.title] += 1
                group["components"].update(linked.get("components", []))
                group["nets"].update(linked.get("nets", []))
                if len(group["examples"]) < 3:
                    group["examples"].append(linked | {"message": violation.message})
        if group_by == "rule" and table.title in groups:
            groups[table.title]["reported"] = table.count
    ordered = sorted(groups.items(), key=lambda item: -item[1]["count"]) if group_by != "rule" \
        else list(groups.items())
    limit = limit or GROUP_LIMIT
    page = []
    for key, group in ordered[offset : offset + limit]:
        entry: dict = {group_by: key, "count": group["count"]}
        if group_by == "rule":
            entry["rule_kind"] = rule_kind(key)[0]
            if group.get("reported", group["count"]) != group["count"]:
                entry["reported_count"] = group["reported"]
        else:
            entry["by_rule_kind"] = _top(group["kinds"])
        if group_by != "component" and group["components"]:
            entry["components"] = _top(group["components"])
        if group_by != "net" and group["nets"]:
            entry["nets"] = _top(group["nets"])
        entry["examples"] = group["examples"]
        page.append(entry)
    result.update({"groups": page, "total": len(ordered), "offset": offset,
                   "has_more": offset + limit < len(ordered)})
    if result["has_more"]:
        result["next_offset"] = offset + limit
    return result


def other_sections(report, linker, limit: int = 20) -> dict:
    """Health issues, waived violations and the report's own warnings."""
    out: dict = {}
    for kind, key in ((HEALTH, "health_issues"), (WAIVED, "waived"), (WARNING, "warnings")):
        tables = [table for table in report.tables if table.type == kind]
        if not tables:
            continue
        out[key] = [
            {
                "title": table.title,
                "count": table.count,
                "examples": [_violation(table, violation, linker)
                             for violation in table.violations[:limit]],
            }
            for table in tables
        ]
    return out


def load_report(project: dict, pcb_path: Path):
    """(report, path, other reports found, folders searched); report None when absent."""
    found, searched = find_reports(project, pcb_path)
    path = pick_report(found)
    if path is None:
        return None, None, [], searched
    return parse_drc_report(path), path, [p for p in found if p != path], searched
