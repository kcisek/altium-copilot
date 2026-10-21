"""The project's sheet hierarchy offline, like Altium's Report Project Hierarchy: the sheet
tree from the top sheet(s) down through sheet symbols (device sheets and Repeat(...)
channels marked), each sheet's ports and the sheet entries of the symbols placed on it,
the project's Net Identifier Scope with what Automatic resolves to, and consistency
issues. Read from the .PrjPcb and the .SchDoc files alone; with a compiled netlist each
port and entry also names the net it joins."""
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

try:
    from parsers.prj_pcb import parse_prj_pcb
    from parsers.sch_doc import SheetHierarchy, SheetSymbol, WireGroup
except ImportError:
    from server.parsers.prj_pcb import parse_prj_pcb
    from server.parsers.sch_doc import SheetHierarchy, SheetSymbol, WireGroup

# [Design] HierarchyMode: Altium's TFlattenMode (eFlatten_Smart, eFlatten_Flat,
# eFlatten_Hierarchical, eFlatten_Global). Strict Hierarchical came later and is not in
# that published list; 4 is taken to be it.
SCOPES = {
    "0": "Automatic", "1": "Flat", "2": "Hierarchical", "3": "Global",
    "4": "Strict Hierarchical",
}
SCOPE_LABELS = {
    "Automatic": "Automatic (Based on project contents)",
    "Flat": "Flat (Only ports global)",
    "Hierarchical": "Hierarchical (Sheet entry <-> port connections, power ports global)",
    "Strict Hierarchical": (
        "Strict Hierarchical (Sheet entry <-> port connections, power ports local)"
    ),
    "Global": "Global (Netlabels and ports global)",
}
SCOPE_EXPLANATIONS = {
    "Hierarchical": (
        "Nets cross sheets only vertically: a port on a child sheet joins the sheet entry "
        "of the same name on the sheet symbol that places that sheet, and so the net wired "
        "to that entry on the parent sheet. A port with no matching entry connects nothing "
        "above it, even when another sheet has a port of that name; ports never join ports "
        "on other sheets, and net labels stay on their own sheet. Power ports (and hidden "
        "power pins) are global: every power port with one name is one net across the "
        "design. A sheet placed by several symbols or by a Repeat(...) symbol is compiled "
        "once per channel, so each of its local nets exists once per channel."
    ),
    "Strict Hierarchical": (
        "Nets cross sheets only vertically: a port on a child sheet joins the sheet entry "
        "of the same name on the sheet symbol that places that sheet, and so the net wired "
        "to that entry on the parent sheet. Ports never join ports on other sheets, net "
        "labels stay on their own sheet, and unlike Hierarchical, power ports are local "
        "too: a power port joins only the power ports of its name on the same sheet, so a "
        "supply reaches a child sheet only through a port and sheet entry. A sheet placed "
        "by several symbols or by a Repeat(...) symbol is compiled once per channel, so "
        "each of its local nets exists once per channel."
    ),
    "Flat": (
        "Nets cross sheets horizontally through ports: every port with one name joins one "
        "net on all sheets, whatever symbols place them. Net labels stay on their own "
        "sheet, so a signal that leaves a sheet needs a port; power ports are global. "
        "Sheet entries do not make these connections, so in a design with sheet symbols "
        "the entries do not decide which ports meet."
    ),
    "Global": (
        "Nets cross sheets horizontally through both ports and net labels: every port with "
        "one name joins one net on all sheets, and so does every net label with one name "
        "(a port and a net label are not joined by sharing a name; on one net the net "
        "label names it). Power ports are global. Sheet entries do not make these "
        "connections."
    ),
}
HIERARCHICAL = ("Hierarchical", "Strict Hierarchical")
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
MAX_TREE_LINES = 400
MAX_DEPTH = 32


@dataclass
class _Sheet:
    name: str
    path: str
    device: bool = False
    data: SheetHierarchy | None = None
    error: str = ""
    children: list[tuple[SheetSymbol, int | None]] = field(default_factory=list)
    parents: list[tuple[int, SheetSymbol]] = field(default_factory=list)


@dataclass
class _Node:
    sheet: int | None          # None: the symbol's file is not a project sheet
    depth: int
    symbol: SheetSymbol | None = None  # the symbol placing it; None for a top sheet
    copies: int = 1            # compiled copies: channel counts multiplied down the tree
    cycle: bool = False        # the sheet is already above: not walked again


def build_design_hierarchy(project: dict, read_sheet, netlist: dict | None = None,
                           sheet: str | None = None) -> dict:
    """The full hierarchy model of the loaded project, or {"error": ...}.

    read_sheet(path) returns a sheet's SheetHierarchy (parse_sch_hierarchy, cached).
    sheet narrows the tree, sheet details and issues to that sheet's subtree."""
    try:
        prj = parse_prj_pcb(project["prj_pcb_path"])
        mode, naming = prj.hierarchy_mode, prj.net_naming
        devices = {os.path.normcase(p) for p in prj.device_sheet_paths}
    except Exception:  # the project loaded; read what the sheets give
        mode, naming, devices = "", {}, set()
    sheets = [_Sheet(s["name"], s["path"], os.path.normcase(s["path"]) in devices)
              for s in project.get("sheets") or []]
    for item in sheets:
        try:
            item.data = read_sheet(item.path)
        except Exception as error:
            item.error = f"{type(error).__name__}: {error}"
    _link(sheets)
    nodes, cycles = _tree(sheets)
    scope = _scope(mode, naming, sheets, nodes)
    nets = _NetIndex(netlist) if netlist else None
    issues = _issues(sheets, nodes, cycles, scope["effective"])
    focus = None
    if sheet is not None:
        wanted = sheet.strip().lower()
        focus = next((i for i, s in enumerate(sheets)
                      if wanted in (s.name.lower(), Path(s.path).name.lower())), None)
        if focus is None:
            return {
                "error": "sheet_not_found",
                "message": f"Sheet '{sheet}' not found. Available: {[s.name for s in sheets]}",
            }
    tops = [sheets[n.sheet].name for n in nodes if n.depth == 0]
    copies = Counter()
    for node in nodes:
        if node.sheet is not None and not node.cycle:
            copies[node.sheet] += node.copies
    summary = _summary(sheets, nodes, issues, tops)
    order, nodes = _focus(nodes, focus, sheets)
    shown = set(order)
    if focus is not None:
        issues = [i for i in issues if i["_sheet"] in shown]
    links = _links(sheets, scope["effective"])
    details = [_sheet_details(i, sheets, copies[i], nets, links) for i in order]
    for issue in issues:
        issue.pop("_sheet")
    result: dict = {
        "project": project.get("name"),
        "scope": scope,
        "summary": summary,
        "tree": _tree_lines(nodes, sheets),
        "issues": issues,
        "sheets": details,
    }
    if focus is not None:
        result["focus"] = sheets[focus].name
    if nets is None:
        result["nets_note"] = (
            "No compiled netlist is loaded, so ports and entries do not name the net they "
            "join; labels lists the net labels and power ports on each one's wiring. Load "
            "the project with a netlist (Altium Designer running once) for net names."
        )
    return result


def page_design_hierarchy(model: dict, offset: int, limit: int, issue_limit: int) -> dict:
    """One page of sheet details; the tree and issues go with the first page only."""
    offset = max(0, int(offset or 0))
    details = model["sheets"]
    result = {key: value for key, value in model.items() if key not in ("sheets", "issues")}
    if offset:
        result.pop("tree", None)
    else:
        issues = model["issues"]
        result["issues"] = issues[:issue_limit]
        if len(issues) > issue_limit:
            result["issues_limited_to"] = issue_limit
        kinds = dict.fromkeys(issue["kind"] for issue in result["issues"])
        if kinds:
            result["issue_help"] = {kind: ISSUE_HELP[kind] for kind in kinds}
    result["sheets_total"] = len(details)
    result["offset"] = offset
    result["has_more"] = offset + limit < len(details)
    if result["has_more"]:
        result["next_offset"] = offset + limit
    result["sheets"] = details[offset:offset + limit]
    return result


# ---------- the tree ----------

def _child_index(sheets: list[_Sheet], parent: _Sheet, file_name: str) -> int | None:
    """The project sheet a symbol's file name points at: a relative path from the
    parent's folder when it holds one, else the sheet of that file name (the one in the
    parent's folder first)."""
    if not file_name:
        return None
    normalized = file_name.replace("\\", "/")
    by_path = {os.path.normcase(s.path): i for i, s in enumerate(sheets)}
    if "/" in normalized:
        target = os.path.normcase(str((Path(parent.path).parent / normalized).resolve()))
        if target in by_path:
            return by_path[target]
    name = Path(normalized).name.lower()
    matches = [i for i, s in enumerate(sheets) if Path(s.path).name.lower() == name]
    folder = os.path.normcase(str(Path(parent.path).parent))
    same = [i for i in matches if os.path.normcase(str(Path(sheets[i].path).parent)) == folder]
    return (same or matches or [None])[0]


def _link(sheets: list[_Sheet]) -> None:
    for index, item in enumerate(sheets):
        for symbol in item.data.symbols if item.data else ():
            child = _child_index(sheets, item, symbol.file_name)
            item.children.append((symbol, child))
            if child is not None:
                sheets[child].parents.append((index, symbol))


def _channel_count(symbol: SheetSymbol | None) -> int:
    channel = symbol.channel if symbol else None
    return max(channel[2] - channel[1] + 1, 0) if channel else 1


def _tree(sheets: list[_Sheet]) -> tuple[list[_Node], list[tuple[int, SheetSymbol]]]:
    """Pre-order nodes from the top sheets: those no other sheet's symbol places. Sheets
    reachable from no top sheet (only placed inside a cycle) become tops too."""
    placed = {child for i, s in enumerate(sheets) for _, child in s.children
              if child is not None and child != i}
    nodes: list[_Node] = []
    cycles: list[tuple[int, SheetSymbol]] = []
    reached: set[int] = set()

    def walk(index: int, depth: int, symbol, copies: int, path: tuple[int, ...]):
        nodes.append(_Node(index, depth, symbol, copies))
        reached.add(index)
        if depth >= MAX_DEPTH:
            return
        for child_symbol, child in sheets[index].children:
            count = copies * _channel_count(child_symbol)
            if child is None:
                nodes.append(_Node(None, depth + 1, child_symbol, count))
            elif child in path:
                cycles.append((index, child_symbol))
                nodes.append(_Node(child, depth + 1, child_symbol, count, cycle=True))
            else:
                walk(child, depth + 1, child_symbol, count, path + (child,))

    for index in range(len(sheets)):
        if index not in placed:
            walk(index, 0, None, 1, (index,))
    for index in range(len(sheets)):
        if index not in reached:
            walk(index, 0, None, 1, (index,))
    return nodes, cycles


def _focus(nodes: list[_Node], focus: int | None, sheets: list[_Sheet]):
    """Sheet order (first appearance in the tree) and the nodes shown: every subtree
    rooted at the focused sheet, re-based to depth 0."""
    if focus is not None:
        kept: list[_Node] = []
        start = None
        for node in nodes:
            if start is not None and node.depth <= start:
                start = None
            if start is None and node.sheet == focus and not node.cycle:
                start = node.depth
            if start is not None:
                kept.append(_Node(node.sheet, node.depth - start, node.symbol, node.copies,
                                  node.cycle))
        nodes = kept
    order = list(dict.fromkeys(n.sheet for n in nodes if n.sheet is not None and not n.cycle))
    if focus is None:
        order += [i for i in range(len(sheets)) if i not in order]
    return order, nodes


def _tree_lines(nodes: list[_Node], sheets: list[_Sheet]) -> list[str]:
    lines = []
    for node in nodes[:MAX_TREE_LINES]:
        item = sheets[node.sheet] if node.sheet is not None else None
        name = item.name if item else (node.symbol.file_name or "(no file)")
        text = name if node.symbol is None else f"{node.symbol.designator or '?'} -> {name}"
        flags = []
        if item is None:
            flags.append("missing")
        elif node.depth == 0:
            flags.append("top" if not item.parents else "subtree")
        if item and item.device:
            flags.append("device sheet")
        if node.symbol is not None and node.symbol.channel:
            flags.append(f"{_channel_count(node.symbol)} channels")
        if node.copies > 1 and node.depth:
            flags.append(f"{node.copies} compiled copies")
        if node.cycle:
            flags.append("cycle: already above")
        if item and item.error:
            flags.append("not readable")
        lines.append("  " * node.depth + text + (f" [{', '.join(flags)}]" if flags else ""))
    if len(nodes) > MAX_TREE_LINES:
        lines.append(f"... {len(nodes) - MAX_TREE_LINES} more lines; pass sheet= for a subtree")
    return lines


# ---------- the scope ----------

def _scope(mode: str, naming: dict[str, str], sheets: list[_Sheet], nodes: list[_Node]) -> dict:
    setting = SCOPES.get(mode.strip(), "Automatic" if not mode.strip() else f"Unknown ({mode})")
    tops = {n.sheet for n in nodes if n.depth == 0}
    top_entries = any(symbol.entries for i in tops for symbol, _ in sheets[i].children)
    ports = any(s.data and s.data.ports for s in sheets)
    scope: dict = {"setting": SCOPE_LABELS.get(setting, setting), "hierarchy_mode": mode or None}
    if setting == "Automatic":
        if top_entries:
            effective = "Hierarchical"
            reason = "the top sheet has sheet entries, so Automatic uses Hierarchical"
        elif ports:
            effective = "Flat"
            reason = "no sheet entries on the top sheet but ports present, so Automatic uses Flat"
        else:
            effective = "Global"
            reason = "no sheet entries on the top sheet and no ports, so Automatic uses Global"
        scope["effective"] = effective
        scope["automatic_rule"] = (
            "Altium's rule: Hierarchical when the top sheet has sheet entries, else Flat "
            "when there are ports, else Global; Automatic never picks Strict Hierarchical."
        )
        scope["resolved_because"] = reason
    elif setting in SCOPE_EXPLANATIONS:
        scope["effective"] = setting
    else:
        scope["effective"] = "Hierarchical" if top_entries else "Flat" if ports else "Global"
        scope["note"] = (f"HierarchyMode={mode} is not a value this tool knows; read as "
                         f"Automatic would resolve it ({scope['effective']}).")
    if mode.strip() == "4":
        scope["note"] = ("HierarchyMode=4 is read as Strict Hierarchical: Altium's published "
                         "list ends at Global (3) and Strict Hierarchical was added after it.")
    scope["explanation"] = SCOPE_EXPLANATIONS[scope["effective"]]
    scope["net_naming"] = _naming(naming)
    return scope


def _naming(naming: dict[str, str]) -> dict:
    def flag(key: str):
        return {"1": True, "0": False}.get(naming.get(key, ""))

    options = {
        "allow_ports_to_name_nets": flag("AllowPortNetNames"),
        "allow_sheet_entries_to_name_nets": flag("AllowSheetEntryNetNames"),
        "higher_level_names_take_priority": flag("NameNetsHierarchically"),
        "power_port_names_take_priority": flag("PowerPortNamesTakePriority"),
        "append_sheet_numbers_to_local_nets": flag("AppendSheetNumberToLocalNets"),
    }
    order = ["net label", "power port"]
    if options["power_port_names_take_priority"]:
        order = ["power port", "net label"]
    if options["allow_ports_to_name_nets"]:
        order.append("port")
    if options["allow_sheet_entries_to_name_nets"]:
        order.append("sheet entry")
    order.append("generated from a pin (e.g. NetR1_1)")
    result = {key: value for key, value in options.items() if value is not None}
    result["name_precedence"] = order
    result["explanation"] = (
        "A net takes the name of the highest object in name_precedence on it (Project "
        "Options > Options > Netlist Options). Ports and sheet entries name a net only when "
        "allowed. With higher_level_names_take_priority the net labels on higher sheets name "
        "the nets on lower sheets; Altium's documentation gives no rule for when it is off, "
        "so read the name from the compiled netlist then. append_sheet_numbers adds the "
        "sheet number to nets that stay on one sheet (not under Global)."
    )
    return result


# ---------- nets ----------

class _NetIndex:
    """Compiled nets by (sheet, sheet designator, pin): one per channel copy, each with
    the UniqueId paths (from the .Annotation file) that name the sheet symbols placing
    that copy."""

    def __init__(self, netlist: dict):
        self._by_logical: dict[tuple[str, str, str], list[tuple[str, tuple[str, ...]]]] = {}
        self._by_sheet: dict[str, list[tuple[str, dict]]] = {}
        for refdes, comp in (netlist.get("components") or {}).items():
            sheet = (comp.get("sheet") or "").lower()
            self._by_sheet.setdefault(sheet, []).append((refdes, comp))
            logical = comp.get("logical_refdes") or refdes
            for pin, data in (comp.get("pins") or {}).items():
                net = data.get("net") if isinstance(data, dict) else data
                if net:
                    self._by_logical.setdefault((sheet, logical, str(pin)), []).append(
                        (net, tuple(comp.get("unique_paths") or ())))

    def copies(self, sheet: str, refdes: str, pin: str) -> list[tuple[str, tuple[str, ...]]]:
        """(net, UniqueId paths) of every compiled copy of a sheet part's pin."""
        key = (sheet.lower(), refdes, pin)
        if key in self._by_logical:
            return self._by_logical[key]
        # A channel copy named by suffix (C1 -> C1A, C1_2) without an annotation file.
        pattern = re.compile(rf"{re.escape(refdes)}(?:[_.\-]?[A-Za-z]+\d*|[_.\-]\d+)+")
        found: list[tuple[str, tuple[str, ...]]] = []
        for name, comp in self._by_sheet.get(sheet.lower(), ()):
            if pattern.fullmatch(name):
                data = (comp.get("pins") or {}).get(pin)
                net = data.get("net") if isinstance(data, dict) else data
                if net:
                    found.append((net, tuple(comp.get("unique_paths") or ())))
        self._by_logical[key] = found
        return found


# A cross-sheet join: (sheet, group) reached, and how. "down" goes from a sheet entry to
# its child's port and "up" back, both through one sheet symbol, named (parent sheet,
# position among its children); "" is a join by name (ports, net labels, power ports).
_Step = tuple[int, int]
_Link = tuple[tuple[int, int], str, _Step | None]


def _links(sheets: list[_Sheet], effective: str) -> dict[tuple[int, int], set[_Link]]:
    """Cross-sheet joins between the wiring groups of ports and entries, under the scope:
    entry <-> child port (hierarchical), same-name ports (flat, global), same-name net
    labels (global), same-name power ports (all but strict)."""
    links: dict[tuple[int, int], set[_Link]] = {}

    def join(a: tuple[int, int], b: tuple[int, int], step: _Step | None = None) -> None:
        if a != b and a[1] >= 0 and b[1] >= 0:
            links.setdefault(a, set()).add((b, "down" if step else "", step))
            links.setdefault(b, set()).add((a, "up" if step else "", step))

    by_name: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for index, item in enumerate(sheets):
        if not item.data:
            continue
        if effective in HIERARCHICAL:
            for position, (symbol, child) in enumerate(item.children):
                child_data = sheets[child].data if child is not None else None
                if not child_data:
                    continue
                for entry in symbol.entries:
                    for port in child_data.ports:
                        if port.name.lower() == entry.port_name.lower():
                            join((index, entry.group), (child, port.group), (index, position))
        else:
            for port in item.data.ports:
                by_name.setdefault(("port", port.name.lower()), []).append((index, port.group))
        for group_index, group in enumerate(item.data.groups):
            names = [("power", p.lower()) for p in group.power_ports]
            if effective == "Strict Hierarchical":
                names = []
            if effective == "Global":
                names += [("label", label.lower()) for label in group.labels]
            for name in names:
                by_name.setdefault(name, []).append((index, group_index))
    for members in by_name.values():
        for other in members[1:]:
            join(members[0], other)
    return links


def _group_nets(sheets: list[_Sheet], nets: _NetIndex, sheet: int, group: int,
                down: tuple[_Step, ...] = ()) -> tuple[set[str], bool]:
    """The compiled nets of the pins on one wiring group, and whether they are certain.

    down holds the sheet symbols passed going down the hierarchy to reach the group:
    only the copies of its sheet that those symbols place count. A copy is told apart by
    its UniqueId paths, which hold the UniqueIds of the symbols placing it; without them
    every copy counts, which is certain only when no other symbol places those sheets."""
    symbols = [sheets[parent].children[position] for parent, position in down]
    uids = [symbol.unique_id for symbol, _ in symbols]
    whole = all(child is not None and len(sheets[child].parents) == 1 for _, child in symbols)
    found: set[str] = set()
    sure = True
    for refdes, pin in sheets[sheet].data.groups[group].pins:
        for net, paths in nets.copies(sheets[sheet].name, refdes, pin):
            if whole:
                found.add(net)
            elif not paths or not all(uids):
                found.add(net)
                sure = False
            elif any(all(uid in path.split("\\") for uid in uids) for path in paths):
                found.add(net)
    return found, sure


AMBIGUOUS_COPIES_NOTE = (
    "every copy's net: several symbols place the sheet below, and without an .Annotation "
    "file its copies cannot be told apart; it joins its own symbol's copy")


def _resolve_nets(sheets, nets: _NetIndex | None, links, sheet: int, group: int) -> dict:
    """{net | nets, via} for one port or entry: the compiled nets of the pins on its own
    wiring, else of the nearest wiring it joins on other sheets. Going down through a
    sheet symbol counts only the copies of the child that symbol places, and coming back
    up goes through that symbol only (not its siblings placing the same sheet)."""
    if nets is None or group < 0:
        return {}
    found, sure = _group_nets(sheets, nets, sheet, group)
    via = None
    if not found:
        # A group reached through different symbols stands for different copies, so a
        # visit is a group and the symbols passed going down to it.
        start: tuple[tuple[int, int], tuple[_Step, ...]] = ((sheet, group), ())
        seen = {start}
        frontier = [start]
        while frontier and not found:
            reached = []
            for node, down in frontier:
                for other, move, step in links.get(node, ()):
                    if move == "down":
                        if step in down:
                            continue  # a recursive hierarchy: already passed
                        path = down + (step,)
                    elif move == "up":
                        if down and down[-1] != step:
                            continue  # back up only through the symbol that came down
                        path = down[:-1]
                    else:
                        path = ()  # a join by name is one net in every copy
                    if (other, path) not in seen:
                        seen.add((other, path))
                        reached.append((other, path))
            certain: set[str] = set()
            loose: set[str] = set()
            for other, path in reached:
                more, more_sure = _group_nets(sheets, nets, *other, path)
                (certain if more_sure else loose).update(more)
            # Copies that cannot be told apart are certain only when they share one net.
            found, sure = (certain, True) if certain else (loose, len(loose) <= 1)
            frontier = reached
        via = "other sheets" if found else None
    if not found and sheets[sheet].data.groups[group].pins:
        return {"net": None, "net_note": (
            "its pins are in no compiled net: a single-pin net, which the netlist leaves "
            "out, or a netlist older than the sheet")}
    if not found:
        return {"net": None}
    result: dict = {"net": next(iter(found))} if len(found) == 1 else {"nets": sorted(found)}
    if via:
        result["net_via"] = via
    if not sure:
        result["net_note"] = AMBIGUOUS_COPIES_NOTE
    return result


# ---------- details ----------

def _wiring(group: WireGroup | None) -> dict:
    if group is None:
        return {}
    info: dict = {}
    labels = list(dict.fromkeys([*group.labels, *group.power_ports]))
    if labels:
        info["labels"] = labels
    if not group.wired:
        info["unwired"] = True
    return info


def _sheet_details(index: int, sheets: list[_Sheet], copies: int, nets, links) -> dict:
    item = sheets[index]
    detail: dict = {"sheet": item.name, "file": Path(item.path).name}
    if item.device:
        detail["device_sheet"] = True
    if item.error:
        detail["error"] = item.error
        return detail
    if item.parents:
        detail["placed_by"] = [
            {"sheet": sheets[parent].name, "symbol": symbol.designator}
            | ({"channels": _channel_count(symbol)} if symbol.channel else {})
            for parent, symbol in item.parents
        ]
    else:
        detail["top"] = True
    if copies > 1:
        detail["compiled_copies"] = copies
    data = item.data
    ports = []
    for port in data.ports:
        entry: dict = {"name": port.name, "io": port.io_type}
        if port.harness_type:
            entry["harness"] = port.harness_type
        entry |= _resolve_nets(sheets, nets, links, index, port.group)
        entry |= _wiring(data.groups[port.group] if port.group >= 0 else None)
        ports.append(entry)
    if ports:
        detail["ports"] = ports
    symbols = []
    for symbol, child in item.children:
        child_ports = {}
        if child is not None and sheets[child].data:
            for port in sheets[child].data.ports:
                child_ports.setdefault(port.name.lower(), port)
        info: dict = {"designator": symbol.designator, "file": symbol.file_name,
                      "child": sheets[child].name if child is not None else None}
        if symbol.symbol_type and symbol.symbol_type != "Normal":
            info["symbol_type"] = symbol.symbol_type
        if child is not None and sheets[child].device:
            info["device_sheet"] = True
        if symbol.channel:
            name, first, last = symbol.channel
            info["channels"] = {"name": name, "first": first, "last": last,
                                "count": _channel_count(symbol)}
        entries = []
        for sheet_entry in symbol.entries:
            row: dict = {"name": sheet_entry.name, "io": sheet_entry.io_type}
            if sheet_entry.harness_type:
                row["harness"] = sheet_entry.harness_type
            if child is not None and sheets[child].data:
                # The child port's I/O type when it differs, or that there is none.
                port = child_ports.get(sheet_entry.port_name.lower())
                if port is None or port.io_type != sheet_entry.io_type:
                    row["port_io"] = port.io_type if port else "missing"
            row |= _resolve_nets(sheets, nets, links, index, sheet_entry.group)
            row |= _wiring(data.groups[sheet_entry.group] if sheet_entry.group >= 0 else None)
            entries.append(row)
        if entries:
            info["entries"] = entries
        symbols.append(info)
    if symbols:
        detail["sheet_symbols"] = symbols
    return detail


def _summary(sheets: list[_Sheet], nodes: list[_Node], issues: list[dict], roots) -> dict:
    symbols = [symbol for s in sheets for symbol, _ in s.children]
    return {
        "sheets": len(sheets),
        "top_sheets": roots,
        "depth": max((n.depth for n in nodes), default=0),
        "sheet_symbols": len(symbols),
        "device_sheets": sum(1 for s in sheets if s.device),
        "repeated_symbols": sum(1 for symbol in symbols if symbol.channel),
        "ports": sum(len(s.data.ports) for s in sheets if s.data),
        "sheet_entries": sum(len(symbol.entries) for symbol in symbols),
        "issues": dict(_count(issues, "severity")),
        "issue_kinds": dict(_count(issues, "kind")),
    }


def _count(issues: list[dict], key: str) -> Counter:
    counts: Counter = Counter()
    for issue in issues:
        counts[issue[key]] += _findings(issue)
    return counts


# ---------- issues ----------

ISSUE_HELP = {
    "sheet_not_readable": (
        "The sheet could not be read (error); its symbols and ports are missing from the tree."
    ),
    "symbol_without_file": "The sheet symbol names no file, so it places no sheet.",
    "missing_child_sheet": (
        "The symbol's file is not a sheet of the project, so nothing below it is compiled. "
        "on_disk: the file is next to the parent sheet; add it to the project."
    ),
    "sheet_cycle": (
        "The symbol places a sheet that is already above it in the tree; Altium cannot "
        "compile a recursive hierarchy."
    ),
    "bad_channel_range": "Repeat(name, first, last) with last below first places no channel.",
    "entry_without_port": (
        "The child sheet has no port of the entry's name, so the entry connects nothing "
        "below it. A net label of that name on the child (child_has_net_label) never "
        "connects to an entry, and under Strict Hierarchical neither does a power port "
        "(child_has_power_port): the child needs a port."
    ),
    "duplicate_sheet_symbol": (
        "Several sheet symbols on one sheet share a designator (count), which names their "
        "channels (rooms, designator suffixes): check the compiled names in Altium."
    ),
    "duplicate_sheet_entry": "Several entries of one symbol share a name (count).",
    "io_mismatch": (
        "The entry's I/O type differs from its port's (entry_io, port_io), though both "
        "describe one signal's direction: Input against Output is reversed, and against "
        "Bidirectional one of them is less specific; check which one is out of date."
    ),
    "harness_mismatch": "The entry and its port carry different harness types.",
    "repeat_entry_without_channels": (
        "A Repeat(...) entry on a symbol whose designator is not Repeat(...): there are no "
        "channels to split it across."
    ),
    "port_without_entry": (
        "A port on the child sheet with no entry on the symbol that places it: under a "
        "hierarchical scope it connects nothing above it."
    ),
    "top_sheet_port": (
        "A port on a top sheet under a hierarchical scope: no symbol is above it, so it "
        "connects nothing outside its sheet (expected for a board's connector port in a "
        "multi-board design)."
    ),
    "multiple_top_sheets": (
        "Under a hierarchical scope more than one sheet is placed by no symbol: each is a "
        "top of its own, joined to the others only by power ports (not under Strict)."
    ),
    "unwired_port": "Nothing is wired to the port on its sheet.",
    "unwired_entry": "Nothing is wired to the sheet entry on the parent sheet.",
    "entry_matches_power_port": (
        "No port of the entry's name on the child sheet, but a power port of that name. "
        "Power ports are global under this scope, so the child reaches the net through the "
        "power port; the entry itself connects nothing below it."
    ),
    "port_alone": (
        "Under Flat or Global no other sheet has a port of this name, and no power port "
        "(or, under Global, net label) on its wiring carries the net elsewhere, so it "
        "connects nothing on other sheets."
    ),
    "sheet_placed_several_times": (
        "Several symbols place this sheet (symbols): it is compiled once per symbol, like "
        "channels, and its parts get one designator per copy."
    ),
}


def _issue(kind: str, severity: str, sheet_index: int, sheets, **fields) -> dict:
    return {"kind": kind, "severity": severity, "sheet": sheets[sheet_index].name,
            **fields, "_sheet": sheet_index}


def _issues(sheets: list[_Sheet], nodes: list[_Node], cycles, effective: str) -> list[dict]:
    issues: list[dict] = []
    tops = sorted({n.sheet for n in nodes if n.depth == 0 and not sheets[n.sheet].parents})
    for index, item in enumerate(sheets):
        if item.error:
            issues.append(_issue("sheet_not_readable", "error", index, sheets, error=item.error))
            continue
        designators = Counter(symbol.designator.lower() for symbol, _ in item.children
                              if symbol.designator)
        for symbol, _ in item.children:
            count = designators.pop(symbol.designator.lower(), 0)
            if count > 1:
                issues.append(_issue("duplicate_sheet_symbol", "warning", index, sheets,
                                     symbol=symbol.designator, count=count))
        for symbol, child in item.children:
            issues += _symbol_issues(index, symbol, child, sheets, effective)
        for port in item.data.ports:
            if port.group >= 0 and not item.data.groups[port.group].wired:
                issues.append(_issue("unwired_port", "warning", index, sheets, port=port.name))
    for index, symbol in cycles:
        issues.append(_issue("sheet_cycle", "error", index, sheets, symbol=symbol.designator,
                             file=symbol.file_name))
    issues += _top_issues(sheets, tops, effective)
    for index, item in enumerate(sheets):
        if len(item.parents) > 1:
            issues.append(_issue("sheet_placed_several_times", "info", index, sheets,
                                 symbols=[f"{sheets[p].name}/{s.designator}"
                                          for p, s in item.parents]))
    issues.sort(key=lambda i: (SEVERITY_ORDER[i["severity"]], i["kind"], i["sheet"].lower()))
    return _grouped(issues)


def _grouped(issues: list[dict]) -> list[dict]:
    """One issue per kind and place: findings that differ only in their entry or port
    become one issue listing them (entries, ports)."""
    groups: dict[tuple, dict] = {}
    for issue in issues:
        name = "entry" if "entry" in issue else "port" if "port" in issue else None
        rest = tuple(sorted((k, repr(v)) for k, v in issue.items() if k != name))
        key = (name, rest) if name else (id(issue),)
        held = groups.get(key)
        plural = "entries" if name == "entry" else "ports"
        if held is None:
            groups[key] = issue
        elif name in held:
            held[plural] = [held.pop(name), issue[name]]
        else:
            held[plural].append(issue[name])
    return list(groups.values())


def _findings(issue: dict) -> int:
    """How many findings an issue stands for (entries or ports grouped into it)."""
    return len(issue.get("entries") or issue.get("ports") or [None])


def _symbol_issues(index: int, symbol: SheetSymbol, child: int | None, sheets,
                   effective: str) -> list[dict]:
    item = sheets[index]
    where = {"symbol": symbol.designator}
    if not symbol.file_name:
        return [_issue("symbol_without_file", "error", index, sheets, **where)]
    if child is None:
        on_disk = (Path(item.path).parent / symbol.file_name.replace("\\", "/")).exists()
        return [_issue("missing_child_sheet", "error", index, sheets, file=symbol.file_name,
                       on_disk=on_disk, **where)]
    issues: list[dict] = []
    channel = symbol.channel
    if channel and channel[2] < channel[1]:
        issues.append(_issue("bad_channel_range", "error", index, sheets, **where))
    names = Counter(entry.name.lower() for entry in symbol.entries)
    for entry in symbol.entries:
        count = names.pop(entry.name.lower(), 0)
        if count > 1:
            issues.append(_issue("duplicate_sheet_entry", "warning", index, sheets,
                                 entry=entry.name, count=count, **where))
        if entry.repeated and not channel:
            issues.append(_issue("repeat_entry_without_channels", "warning", index, sheets,
                                 entry=entry.name, **where))
        if entry.group >= 0 and not item.data.groups[entry.group].wired:
            issues.append(_issue("unwired_entry", "warning", index, sheets, entry=entry.name,
                                 **where))
    child_data = sheets[child].data
    if child_data is None:
        return issues
    where["child"] = sheets[child].name
    ports = {}
    for port in child_data.ports:
        ports.setdefault(port.name.lower(), port)
    for entry in symbol.entries:
        port = ports.get(entry.port_name.lower())
        if port is None:
            issues.append(_entry_without_port(index, entry, child_data, sheets, effective,
                                              where))
            continue
        if "Unspecified" not in (entry.io_type, port.io_type) and entry.io_type != port.io_type:
            issues.append(_issue("io_mismatch", "warning", index, sheets, entry=entry.name,
                                 entry_io=entry.io_type, port_io=port.io_type, **where))
        if entry.harness_type != port.harness_type:
            issues.append(_issue("harness_mismatch", "warning", index, sheets, entry=entry.name,
                                 entry_harness=entry.harness_type or None,
                                 port_harness=port.harness_type or None, **where))
    entries = {entry.port_name.lower() for entry in symbol.entries}
    hierarchical = effective in HIERARCHICAL
    if not hierarchical and not entries:
        return issues  # a flat design's symbol without entries: its ports are global
    for name, port in ports.items():
        if name not in entries:
            issues.append(_issue("port_without_entry", "warning" if hierarchical else "info",
                                 index, sheets, port=port.name, **where))
    return issues


def _entry_without_port(index: int, entry, child_data: SheetHierarchy, sheets,
                        effective: str, where: dict) -> dict:
    """An entry whose child sheet has no port of its name. A power port of that name on
    the child reaches the parent's net anyway where power ports are global."""
    name = entry.port_name.lower()
    power = any(p.lower() == name for p in child_data.power_ports)
    if power and effective != "Strict Hierarchical":
        return _issue("entry_matches_power_port", "info", index, sheets, entry=entry.name,
                      **where)
    fields: dict = {"entry": entry.name, **where}
    if power:
        fields["child_has_power_port"] = True
    if any(n.lower() == name for n in child_data.net_labels):
        fields["child_has_net_label"] = True
    return _issue("entry_without_port", "error" if effective in HIERARCHICAL else "warning",
                  index, sheets, **fields)


def _top_issues(sheets: list[_Sheet], tops: list[int], effective: str) -> list[dict]:
    issues: list[dict] = []
    if effective in HIERARCHICAL:
        if len(tops) > 1:
            issues += [_issue("multiple_top_sheets", "warning", index, sheets)
                       for index in tops]
        for index in tops:
            names = [port.name for port in sheets[index].data.ports] if sheets[index].data else []
            for name in dict.fromkeys(names):
                issues.append(_issue("top_sheet_port", "warning", index, sheets, port=name))
        return issues
    if len(sheets) < 2:
        return issues  # one sheet: its ports have nothing to reach
    counts: Counter = Counter()
    for item in sheets:
        counts.update({port.name.lower() for port in item.data.ports} if item.data else ())
    for index, item in enumerate(sheets):
        seen: set[str] = set()
        for port in item.data.ports if item.data else ():
            key = port.name.lower()
            group = item.data.groups[port.group] if port.group >= 0 else None
            # Under Global a net label (and anywhere a power port) on its wiring carries
            # the net to other sheets anyway.
            reaches = group is not None and (group.power_ports or (
                effective == "Global" and group.labels))
            if counts[key] == 1 and key not in seen and not reaches:
                seen.add(key)
                issues.append(_issue("port_alone", "info", index, sheets, port=port.name))
    return issues

