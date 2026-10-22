"""The project's netlist compiled offline from its saved .SchDoc files, the way Altium's
compiler joins them, so the schematic tools work without Altium Designer running.

Each sheet is wired on its own (parse_sch_connectivity): wires join where an end of one
lies on another, junctions and pin ends join the wires within 3 mil of them, and net
labels, power ports, ports, sheet entries and harness entries join the wiring at their
points. Buses and signal harnesses are a layer of their own, so a wire crossing or
ending on one does not join it (a junction neither), and a bus carries its members along
its own wiring only. A sheet placed by several symbols, or by a Repeat(...) symbol, is compiled once per
placement or channel. The sheets are then joined by the project's Net Identifier Scope:

- Flat: ports join by name everywhere; net labels stay on their sheet.
- Global: ports and net labels join by name everywhere (a port and a net label of one
  name are not joined by the name alone).
- Hierarchical: a sheet entry joins the port of its name on the sheet its symbol
  places, in that copy only. A power port wired to a port is local to its sheet.
- Strict Hierarchical: as Hierarchical, and every power port is local to its sheet.
- Automatic resolves to one of these as Altium does (design_hierarchy.effective_scope).

Everywhere else power ports join by name across the design, and on one sheet a net
label and a power port of one name are one net. Bus names with a range (D[0..7]) on
labels, ports and entries stand for their members (D0..D7), which join the labels of
those names. Harness entries of one name on joined harnesses are one net. The parts of
a multi-part component (one designator, different part ids) are one component, on any
sheet. Compile masks remove the parts and objects they cover. Hidden pins connect only
through a legacy HiddenNetName. No ERC markers connect nothing.

Nets are named by the project's netlist options (net labels, power ports, then ports
and sheet entries when allowed; Power Port Names Take Priority; Higher Level Names Take
Priority), ties going to the case-insensitive smallest name, else Net<designator>_<pin>
from the smallest designator and pin. Physical designators come from the .Annotation
file, else the sheet's, else (a sheet compiled several times) the project's
multi-channel Designator Format. Local net labels of such a sheet carry its channel
(room) name as a suffix.

Validated against Altium's own .NET files and PCB pad nets (README); what the files
cannot settle (Repeat channel names, unlabelled harness member names, off-sheet
connectors, sheet numbers on local nets) is reported in the warnings."""
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

try:
    from parsers.annotation import AnnotationEntry
    from parsers.prj_pcb import parse_prj_options, parse_prj_pcb
    from parsers.sch_doc import (
        ComponentMeta, LineIndex, SheetConnectivity, parse_sch_connectivity,
    )
    from services.design_hierarchy import child_sheet, effective_scope
except ImportError:
    from server.parsers.annotation import AnnotationEntry
    from server.parsers.prj_pcb import parse_prj_options, parse_prj_pcb
    from server.parsers.sch_doc import (
        ComponentMeta, LineIndex, SheetConnectivity, parse_sch_connectivity,
    )
    from server.services.design_hierarchy import child_sheet, effective_scope

# A junction, or a pin's end, joins the wires passing within this distance of it. Altium
# documents no tolerance; exact points left nets split on real projects that Altium (and
# the PCB made from its netlist) joins: pin ends 0.6-1.3 mil off the wire drawn to them.
# Net labels 3-5 mil off a wire are not joined to it.
JUNCTION_TOLERANCE_MIL = 3.0
MAX_DEPTH = 32
MAX_EXAMPLES = 10
DEFAULT_DESIGNATOR_FORMAT = "$Component_$RoomName"
# Project Options > Multi-Channel > Room Naming Style ([Design] ChannelRoomNamingStyle).
FLAT_STYLES = {0, 1}   # Flat Numeric / Flat Alpha With Names; 2..4 are name paths
ALPHA_STYLES = {1, 3}  # Flat Alpha With Names, Alpha Name Path
_RANGE_RE = re.compile(r"^(.*?)\[\s*(-?\d+)\s*\.\.\s*(-?\d+)\s*\](.*)$")
_REPEAT_ENTRY_RE = re.compile(r"^\s*repeat\s*\(\s*([^,()]+?)\s*\)\s*$", re.IGNORECASE)
_TOKEN_RE = re.compile(r"\$(ComponentPrefix|ComponentIndex|Component|RoomName|ChannelAlpha"
                       r"|ChannelPrefix|ChannelIndex)", re.IGNORECASE)
# Name kinds in Altium's default precedence; options reorder and filter them.
LABEL, POWER, PORT, ENTRY = "label", "power", "port", "entry"


def bus_members(name: str) -> list[str] | None:
    """The members of a ranged bus name (D[0..3] -> D0, D1, D2, D3, in order), else None."""
    match = _RANGE_RE.match(name.strip())
    if not match:
        return None
    first, last = int(match.group(2)), int(match.group(3))
    step = 1 if last >= first else -1
    return [f"{match.group(1)}{i}{match.group(4)}" for i in range(first, last + step, step)]


def natural_key(text: str) -> list:
    """Natural order: R2 before R10, compared case-insensitively."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


@dataclass
class CompiledNetlist:
    """The netlist in the server's .NET shape, and what enrichment and diagnostics need."""
    nets: dict[str, list[tuple[str, str]]]          # multi-pin nets: name -> (refdes, pin)
    pin_to_net: dict[str, dict[str, str]]
    parts: dict[str, dict[str, str]]                # every compiled part: footprint, comment
    scope: str                                      # the effective Net Identifier Scope
    warnings: list[dict] = field(default_factory=list)
    # (sheet name, parse_sch_doc's components) per readable sheet, in project order.
    sheets: list[tuple[str, dict[str, ComponentMeta]]] = field(default_factory=list)
    sheet_errors: list[dict] = field(default_factory=list)
    designator_clashes: list[dict] = field(default_factory=list)
    # The physical designators the compiler gave sheet copies the .Annotation file does
    # not cover, in its format, so enrichment files them like annotated parts.
    channel_entries: list[AnnotationEntry] = field(default_factory=list)
    # Each compiled part's UniqueId path (symbols, then the part; upper case, no leading
    # backslash) -> its physical designator: what a PCB's SOURCEUNIQUEID names.
    part_paths: dict[str, str] = field(default_factory=dict)


class _UnionFind:
    """Union-find over hashable keys."""

    def __init__(self):
        self.parent: dict = {}

    def find(self, key):
        parent = self.parent
        if key not in parent:
            parent[key] = key
            return key
        root = key
        while parent[root] != root:
            root = parent[root]
        while parent[key] != root:
            parent[key], key = root, parent[key]
        return root

    def union(self, a, b) -> bool:
        a, b = self.find(a), self.find(b)
        if a == b:
            return False
        self.parent[b] = a
        return True


# ---------- one sheet's wiring ----------

@dataclass
class _Wiring:
    """One sheet wired on its own: each object's group, and where to find objects."""
    objects: list[tuple[str, object]] = field(default_factory=list)  # (kind, payload)
    group: list[int] = field(default_factory=list)   # union-find root per object
    names: dict[tuple[str, str], int] = field(default_factory=dict)  # (space, name) -> object
    pins: dict[int, int] = field(default_factory=dict)       # pin index -> object
    entries: dict[tuple[int, int], int] = field(default_factory=dict)  # (symbol, entry)
    ports: dict[str, list[int]] = field(default_factory=dict)  # lower name -> objects
    connectors: dict[int, int] = field(default_factory=dict)  # harness connector -> object
    live: set[int] = field(default_factory=set)      # parts compiled (not masked, annotated)
    masked: int = 0                                   # parts removed by compile masks


def _live_parts(sheet: SheetConnectivity) -> tuple[set[int], int]:
    """Parts that compile: annotated (no '?') and not removed by a compile mask. A part
    inside a mask is removed even when another part of its component is outside one (its
    pins connect nothing), as parse_sch_doc's components have it."""
    live: set[int] = set()
    masked = 0
    for index, part in enumerate(sheet.parts):
        x, y = part.origin
        if any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in sheet.masks):
            masked += 1
        elif part.designator.strip() and "?" not in part.designator:
            live.add(index)
    return live, masked


def wire_sheet(sheet: SheetConnectivity) -> _Wiring:
    """Join one sheet's objects by geometry and by name, see the module docstring."""
    result = _Wiring()
    result.live, result.masked = _live_parts(sheet)

    def inside(point) -> bool:
        return any(x0 <= point[0] <= x1 and y0 <= point[1] <= y1
                   for x0, y0, x1, y1 in sheet.masks)

    def kept(point) -> bool:
        return not (sheet.masks and inside(point))

    wires, buses = LineIndex(), LineIndex()
    for vertices in sheet.wires:
        if not (sheet.masks and vertices and all(inside(v) for v in vertices)):
            wires.add(vertices)
    for vertices in [*sheet.buses, *sheet.harnesses]:
        buses.add(vertices)
    offset = len(wires.lines)
    parent = list(range(offset + len(buses.lines)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        a, b = find(a), find(b)
        if a != b:
            parent[b] = a

    for node, vertices in enumerate(wires.lines):
        for x, y in (vertices[0], vertices[-1]):
            for other in wires.at(x, y):
                union(node, other)
    for node, vertices in enumerate(buses.lines):
        for x, y in (vertices[0], vertices[-1]):
            for other in buses.at(x, y):
                union(offset + node, offset + other)
    first_object = len(parent)
    at: dict[tuple[int, int], int] = {}

    def add(kind: str, payload, points, wire: bool = True, bus: bool = False,
            name: tuple[str, str] | None = None) -> int:
        node = len(parent)
        parent.append(node)
        result.objects.append((kind, payload))
        for x, y in points:
            if wire:
                for other in wires.at(x, y):
                    union(node, other)
            if bus:
                for other in buses.at(x, y):
                    union(node, offset + other)
            key = (round(x * 100), round(y * 100))
            if key in at:
                union(node, at[key])
            else:
                at[key] = node
        if name is not None:
            # On one sheet, objects of one name in one name space are one net.
            if name in result.names:
                union(node, first_object + result.names[name])
            else:
                result.names[name] = node - first_object
        return node - first_object

    for index, pin in enumerate(sheet.pins):
        if pin.part in result.live and not pin.hidden:
            result.pins[index] = add("pin", index, [pin.point])
            for other in wires.near(pin.point[0], pin.point[1], JUNCTION_TOLERANCE_MIL):
                union(first_object + result.pins[index], other)
    for point in sheet.junctions:
        if kept(point):
            # A junction joins the wires at it, and the buses at it, never one to the other.
            node = first_object + add("junction", None, [point])
            for other in wires.near(point[0], point[1], JUNCTION_TOLERANCE_MIL):
                union(node, other)
            on_buses = sorted(buses.at(*point))
            for other in on_buses[1:]:
                union(offset + on_buses[0], offset + other)
    # A net label and a power port of one name are one net on their sheet.
    for kind, items in ((LABEL, sheet.labels), (POWER, sheet.power_ports)):
        for text, point in items:
            if kept(point):
                add(kind, text, [point], bus=kind == LABEL, name=("net", text.strip().lower()))
    for text, point in sheet.off_sheet:
        if kept(point):
            add("off_sheet", text, [point], name=("off", text.strip().lower()))
    for index, (port, ends) in enumerate(sheet.ports):
        if ends and kept(ends[0]):
            key = port.name.strip().lower()
            result.ports.setdefault(key, []).append(
                add(PORT, index, ends, bus=True, name=("port", key)))
    for symbol_index, (_, points) in enumerate(sheet.symbols):
        for entry_index, point in enumerate(points):
            result.entries[(symbol_index, entry_index)] = add(
                ENTRY, (symbol_index, entry_index), [point], bus=True)
    for index, connector in enumerate(sheet.connectors):
        result.connectors[index] = add("connector", index, [connector.point], wire=False,
                                       bus=True)
        for name, point in connector.entries:
            add("harness_entry", (index, name), [point])
    result.group = [find(first_object + i) for i in range(len(result.objects))]
    return result


# ---------- the sheet tree ----------

@dataclass
class _Instance:
    """One compiled copy of a sheet."""
    sheet: int
    path: tuple[str, ...]          # UniqueIds of the symbols placing it, top down
    depth: int
    parent: int | None = None      # index of the parent copy
    symbol: int = -1               # the placing symbol's index on the parent sheet
    channel: int | None = None     # the Repeat(...) channel number
    level: str = ""                # this level's channel name
    index: int = 1                 # 1-based position among the copies of its sheet
    room: str = ""                 # channel (room) name, "" for a sheet compiled once


class _Compiler:
    def __init__(self, prj_pcb_path: str, sheet_paths: list[str] | None,
                 annotation: list[AnnotationEntry], read_sheet):
        prj = parse_prj_pcb(prj_pcb_path)
        try:
            design = parse_prj_options(prj_pcb_path).design
        except (OSError, UnicodeDecodeError):
            design = {}
        self.paths = list(sheet_paths if sheet_paths is not None else prj.sheet_paths)
        self.mode = prj.hierarchy_mode
        self.naming = prj.net_naming
        self.design = design
        self.annotation = annotation
        self.warnings: dict[str, dict] = {}
        self.sheets: list[SheetConnectivity | None] = []
        self.sheet_errors: list[dict] = []
        for path in self.paths:
            if not Path(path).is_file():
                error = "file not found"
            else:
                try:
                    self.sheets.append(read_sheet(path))
                    continue
                except Exception as e:
                    error = f"{type(e).__name__}: {e}"
            self.sheets.append(None)
            self.sheet_errors.append({"sheet": Path(path).stem, "path": str(path),
                                      "error": error})
        self.wirings = [wire_sheet(sheet) if sheet else None for sheet in self.sheets]
        self.uf = _UnionFind()
        self.names: dict = defaultdict(list)   # node -> [(kind, name, depth)]
        # Harness entries: (entry node, connector node, lower-case entry name).
        self.harness: list[tuple[tuple, tuple, str]] = []
        self.channel_entries: list[AnnotationEntry] = []
        self.part_paths: dict[str, str] = {}
        # After run(): the components by physical designator, and each net's name by its
        # union-find root (what a validation needs to tell duplicate designators apart).
        self.components: dict[str, list[dict]] = {}
        self.net_roots: dict = {}
        self.annotation_by_path: dict[str, list[AnnotationEntry]] = defaultdict(list)
        for entry in annotation:
            self.annotation_by_path[entry.unique_id.strip("\\").upper()].append(entry)

    # ----- warnings -----

    def warn(self, kind: str, message: str, example: str | None = None) -> None:
        held = self.warnings.setdefault(kind, {"kind": kind, "message": message, "count": 0})
        held["count"] += 1
        if example is not None:
            examples = held.setdefault("examples", [])
            if len(examples) < MAX_EXAMPLES and example not in examples:
                examples.append(example)

    # ----- tree and scope -----

    def instances(self) -> list[_Instance]:
        """Every compiled sheet copy, pre-order from the top sheets (those no symbol
        places; sheets reachable only inside a cycle become tops too)."""
        children: dict[int, list[tuple[int, int | None]]] = {}
        placed: set[int] = set()
        for index, sheet in enumerate(self.sheets):
            children[index] = []
            for position, (symbol, _) in enumerate(sheet.symbols if sheet else ()):
                child = child_sheet(self.paths, self.paths[index], symbol.file_name)
                children[index].append((position, child))
                if child is not None and child != index:
                    placed.add(child)
        result: list[_Instance] = []
        reached: set[int] = set()

        def walk(instance: _Instance, above: tuple[int, ...]) -> None:
            number = len(result)
            result.append(instance)
            reached.add(instance.sheet)
            if instance.depth >= MAX_DEPTH:
                return
            sheet = self.sheets[instance.sheet]
            levels = Counter(sheet.symbols[p][0].designator.strip().lower()
                             for p, c in children[instance.sheet] if c is not None)
            seen: dict[str, int] = defaultdict(int)
            for position, child in children[instance.sheet]:
                symbol = sheet.symbols[position][0]
                where = f"{Path(self.paths[instance.sheet]).stem}/{symbol.designator}"
                if child is None:
                    self.warn("missing_child_sheet",
                              "Sheet symbols whose file is not a sheet of the project: nothing "
                              "below them is compiled.", where)
                    continue
                if self.sheets[child] is None:
                    continue
                if child in above:
                    self.warn("sheet_cycle", "Sheet symbols placing a sheet already above "
                              "them; Altium cannot compile a recursive hierarchy.", where)
                    continue
                channel = symbol.channel
                if channel and channel[2] < channel[1]:
                    self.warn("bad_channel_range", "Repeat(name, first, last) with last below "
                              "first places no channel.", where)
                    continue
                designator = symbol.designator.strip()
                key = designator.lower()
                seen[key] += 1
                for channel_number in (range(channel[1], channel[2] + 1) if channel else [None]):
                    if channel_number is not None:
                        level = f"{channel[0]}{self._channel_text(channel_number)}"
                    elif levels[key] > 1:
                        # Symbols sharing a designator are numbered in placement order.
                        level = f"{designator}{self._channel_text(seen[key])}"
                    else:
                        level = designator
                    walk(_Instance(child, instance.path + (symbol.unique_id,),
                                   instance.depth + 1, number, position, channel_number,
                                   level), above + (child,))

        for index, sheet in enumerate(self.sheets):
            if sheet is not None and index not in placed:
                walk(_Instance(index, (), 0), (index,))
        for index, sheet in enumerate(self.sheets):
            if sheet is not None and index not in reached:
                walk(_Instance(index, (), 0), (index,))
        return result

    def _channel_text(self, number: int) -> str:
        style = _int(self.design.get("ChannelRoomNamingStyle", "0"))
        return _alpha(number) if style in ALPHA_STYLES else str(number)

    def scope(self, instances: list[_Instance]) -> str:
        tops = {i.sheet for i in instances if i.depth == 0}
        top_entries = any(symbol.entries for index in tops
                          for symbol, _ in self.sheets[index].symbols)
        ports = any(sheet and sheet.ports for sheet in self.sheets)
        return effective_scope(self.mode, top_entries, ports)

    def rooms(self, instances: list[_Instance]) -> None:
        """Each copy's position among its sheet's copies and, for a sheet compiled more
        than once, its room name: the .Annotation file's channel name for that copy, else
        built by the project's Room Naming Style (flat: this level's name; name path:
        every level's name joined by the level separator)."""
        copies: dict[int, int] = defaultdict(int)
        for instance in instances:
            copies[instance.sheet] += 1
            instance.index = copies[instance.sheet]
        channels: dict[str, str] = {}
        for entry in self.annotation:
            path = entry.unique_id.strip("\\").upper().rsplit("\\", 1)[0]
            if entry.channel and "\\" in entry.unique_id.strip("\\"):
                channels.setdefault(path, entry.channel)
        style = _int(self.design.get("ChannelRoomNamingStyle", "0"))
        separator = self.design.get("ChannelRoomLevelSeperator", "_")
        paths: dict[int, str] = {}
        for number, instance in enumerate(instances):
            if copies[instance.sheet] < 2:
                continue
            known = channels.get("\\".join(instance.path).upper())
            if known and instance.channel is None:
                instance.room = known
                continue
            levels = []
            walk: _Instance | None = instance
            while walk is not None and walk.parent is not None:
                levels.append(walk.level)
                walk = instances[walk.parent]
            levels.reverse()
            paths[number] = separator.join(levels)
            instance.room = levels[-1] if style in FLAT_STYLES else paths[number]
        # A flat name repeats when a sheet is copied below a copied sheet (both copies of
        # the leaf are "L"): those copies take their name path, so designators stay apart.
        rooms = Counter((instances[n].sheet, instances[n].room.lower()) for n in paths)
        for number, path in paths.items():
            if rooms[(instances[number].sheet, instances[number].room.lower())] > 1:
                instances[number].room = path

    # ----- the compile -----

    def run(self) -> CompiledNetlist:
        instances = self.instances()
        self.rooms(instances)
        scope = self.scope(instances)
        hierarchical = scope in ("Hierarchical", "Strict Hierarchical")
        uf, names = self.uf, self.names
        hidden_unconnected = 0
        off_sheet = sum(len(sheet.off_sheet) for sheet in self.sheets if sheet)
        if off_sheet:
            self.warnings["off_sheet_connectors"] = {
                "kind": "off_sheet_connectors", "count": off_sheet,
                "message": ("Off-sheet connectors join the sheets placed by one symbol (the "
                            "top sheets of a flat design) by name; no sample project "
                            "confirmed this: check those nets in Altium.")}
        for number, instance in enumerate(instances):
            sheet, wiring = self.sheets[instance.sheet], self.wirings[instance.sheet]

            def node(obj: int, number=number, wiring=wiring):
                return ("g", number, wiring.group[obj])

            def member(obj: int, name: str, number=number, wiring=wiring):
                return _member(number, wiring.group[obj], name)

            ported = {wiring.group[o] for objs in wiring.ports.values() for o in objs}
            suffix = f"_{instance.room}" if instance.room else ""
            for obj, (kind, payload) in enumerate(wiring.objects):
                if kind == LABEL:
                    names[node(obj)].append(
                        (LABEL, payload.strip() + ("" if scope == "Global" else suffix),
                         instance.depth))
                    if scope == "Global":
                        uf.union(node(obj), ("label", payload.strip().lower()))
                elif kind == POWER:
                    names[node(obj)].append((POWER, payload.strip(), instance.depth))
                    key = payload.strip().lower()
                    local = scope == "Strict Hierarchical" or (
                        scope == "Hierarchical" and wiring.group[obj] in ported)
                    uf.union(node(obj), ("power", number, key) if local else ("power", key))
                elif kind == PORT:
                    port = sheet.ports[payload][0]
                    names[node(obj)].append(
                        (PORT, port.name + (suffix if hierarchical else ""), instance.depth))
                    if not hierarchical:
                        members = bus_members(port.name)
                        if members is None:
                            uf.union(node(obj), ("port", port.name.strip().lower()))
                        for name in members or ():
                            uf.union(member(obj, name), ("port", name.strip().lower()))
                elif kind == ENTRY:
                    symbol, entry = payload
                    name = sheet.symbols[symbol][0].entries[entry].port_name
                    names[node(obj)].append((ENTRY, name + suffix, instance.depth))
                elif kind == "off_sheet":
                    uf.union(node(obj), ("off", instance.path, payload.strip().lower()))
                elif kind == "harness_entry":
                    connector, name = payload
                    self.harness.append((node(obj), node(wiring.connectors[connector]),
                                         name.strip().lower()))
                else:
                    uf.find(node(obj))
            # The members of a bus (its wiring's ranged labels and ports) join the
            # same-sheet labels (and power ports) of their names.
            for obj, (kind, payload) in enumerate(wiring.objects):
                if kind not in (LABEL, PORT):
                    continue
                text = sheet.ports[payload][0].name if kind == PORT else payload
                for name in bus_members(text) or ():
                    held = wiring.names.get(("net", name.strip().lower()))
                    if held is not None:
                        uf.union(member(obj, name), node(held))
            # Hidden pins: only a legacy HiddenNetName connects one.
            for index, pin in enumerate(sheet.pins):
                if pin.hidden and pin.part in wiring.live:
                    if not pin.hidden_net.strip():
                        hidden_unconnected += 1
                        continue
                    key = pin.hidden_net.strip().lower()
                    names[("h", number, index)].append((POWER, pin.hidden_net.strip(),
                                                        instance.depth))
                    uf.union(("h", number, index), ("power", number, key)
                             if scope == "Strict Hierarchical" else ("power", key))
        if hierarchical:
            self._join_entries(instances)
        components = self._components(instances)
        self._join_harnesses()
        if hidden_unconnected:
            self.warnings["hidden_pins"] = {
                "kind": "hidden_pins", "count": hidden_unconnected,
                "message": ("Hidden pins without a HiddenNetName connect nothing: Altium no "
                            "longer joins a hidden pin to a net by its name. Show and wire "
                            "them (or check the part) if they must connect.")}
        if self.design.get("AppendSheetNumberToLocalNets", "").strip() in ("1", "T"):
            self.warn("append_sheet_numbers", "Append Sheet Numbers to Local Nets is on, but "
                      "the offline compile does not append them: local net names lack "
                      "Altium's sheet-number suffix.")
        return self._result(components, scope)

    def _join_entries(self, instances: list[_Instance]) -> None:
        """Hierarchical scopes: each sheet entry joins the port of its name on the copy
        of the child sheet its symbol places; ranged names join member by member, and a
        Repeat(X) entry gives channel N the parent's bus member XN."""
        uf = self.uf
        warned: set = set()
        for number, instance in enumerate(instances):
            if instance.parent is None:
                continue
            parent = instances[instance.parent]
            parent_sheet = self.sheets[parent.sheet]
            parent_wiring, child_wiring = self.wirings[parent.sheet], self.wirings[instance.sheet]
            symbol = parent_sheet.symbols[instance.symbol][0]
            for entry_index, entry in enumerate(symbol.entries):
                obj = parent_wiring.entries.get((instance.symbol, entry_index))
                if obj is None:
                    continue
                name = entry.port_name.strip()
                ports = child_wiring.ports.get(name.lower(), [])
                where = (parent.sheet, instance.symbol, entry_index)
                label = (f"{Path(self.paths[parent.sheet]).stem}/{symbol.designator}/"
                         f"{entry.name}")
                if not ports:
                    if where not in warned:
                        warned.add(where)
                        self.warn("entry_without_port", "Sheet entries with no port of their "
                                  "name on the child sheet connect nothing below them.", label)
                    continue
                parent_group = parent_wiring.group[obj]
                if entry.repeated and instance.channel is not None:
                    if entry.harness_type:
                        if where not in warned:
                            warned.add(where)
                            self.warn("repeat_harness_entry", "Repeat(...) entries carrying "
                                      "a harness: Altium does not pass harnesses into "
                                      "channels, so they connect nothing.", label)
                        continue
                    if where not in warned:
                        warned.add(where)
                        self.warn("repeat_entries", "Repeat(...) sheet entries give channel N "
                                  "the member <name>N of the parent's bus; no sample project "
                                  "confirmed this: check those nets in Altium.", label)
                    channel_member = _member(instance.parent, parent_group,
                                             f"{name}{instance.channel}")
                    for port in ports:
                        uf.union(channel_member, ("g", number, child_wiring.group[port]))
                    continue
                parent_node = ("g", instance.parent, parent_group)
                for port in ports:
                    uf.union(parent_node, ("g", number, child_wiring.group[port]))
                    # A bus joins member by member, along the wiring of entry and port.
                    for member in bus_members(name) or ():
                        uf.union(_member(instance.parent, parent_group, member),
                                 _member(number, child_wiring.group[port], member))

    def _join_harnesses(self) -> None:
        """Harness entries of one name on joined harnesses are one net. Joining them can
        join further harnesses (a harness carried inside another), so repeat until
        nothing changes."""
        uf = self.uf
        changed = True
        while changed and self.harness:
            changed = False
            held: dict[tuple, tuple] = {}
            for entry, connector, name in self.harness:
                key = (uf.find(connector), name)
                if key in held:
                    changed |= uf.union(held[key], entry)
                else:
                    held[key] = entry

    # ----- components and designators -----

    def _physical(self, instance: _Instance, part) -> tuple[str, bool]:
        """A placed part's physical designator, and whether the .Annotation file gave it."""
        full = "\\".join([*instance.path, part.unique_id]).upper()
        entries = self.annotation_by_path.get(full, [])
        if len(entries) > 1 and instance.room:
            entries = [e for e in entries if e.channel.lower() == instance.room.lower()]
        if entries:
            return entries[0].physical, True
        designator = part.designator.strip()
        if not instance.room:
            return designator, False
        return self._channel_designator(designator, instance), False

    def _channel_designator(self, designator: str, instance: _Instance) -> str:
        """The multi-channel Designator Format ($Component_$RoomName unless the project
        sets another) for a part on a sheet compiled several times."""
        text = self.design.get("ChannelDesignatorFormatString") or DEFAULT_DESIGNATOR_FORMAT
        match = re.match(r"^(.*?)(\d*)$", designator)
        prefix, number = match.group(1), match.group(2)
        index = instance.channel if instance.channel is not None else instance.index
        values = {
            "componentprefix": prefix, "componentindex": number, "component": designator,
            "roomname": instance.room, "channelalpha": _alpha(index),
            "channelprefix": re.sub(r"\d+$", "", instance.level), "channelindex": str(index),
        }
        return _TOKEN_RE.sub(lambda m: values[m.group(1).lower()], text)

    def _components(self, instances: list[_Instance]) -> dict[str, list[dict]]:
        """Compiled components by physical designator. Placed parts with one physical
        designator and different part ids are the parts of one component, wherever they
        sit; a part id seen again is another component with the same designator."""
        by_name: dict[str, list[dict]] = defaultdict(list)
        for number, instance in enumerate(instances):
            sheet, wiring = self.sheets[instance.sheet], self.wirings[instance.sheet]
            pins: dict[int, list[int]] = defaultdict(list)
            for index, pin in enumerate(sheet.pins):
                pins[pin.part].append(index)
            for part_index in sorted(wiring.live):
                part = sheet.parts[part_index]
                physical, annotated = self._physical(instance, part)
                self.part_paths["\\".join([*instance.path, part.unique_id]).upper()] = physical
                held = next((c for c in by_name[physical]
                             if part.part_id not in c["part_ids"]
                             and part.part_count == c["part_count"]), None)
                if held is None:
                    held = {"part_ids": set(), "part_count": part.part_count, "pins": {},
                            "sheet": instance.sheet, "designator": part.designator.strip(),
                            "channel": not annotated and bool(instance.room), "paths": []}
                    by_name[physical].append(held)
                held["part_ids"].add(part.part_id)
                held["paths"].append("\\".join([*instance.path, part.unique_id]).upper())
                if not annotated and (instance.room or physical != part.designator.strip()):
                    self.channel_entries.append(AnnotationEntry(
                        logical=part.designator.strip(), physical=physical,
                        unique_id="\\" + "\\".join([*instance.path, part.unique_id]),
                        document=Path(self.paths[instance.sheet]).name, channel=instance.room))
                for index in pins[part_index]:
                    pin = sheet.pins[index]
                    if index in wiring.pins:
                        node = ("g", number, wiring.group[wiring.pins[index]])
                    elif pin.hidden and pin.hidden_net.strip():
                        node = ("h", number, index)
                    else:
                        continue
                    held["pins"].setdefault(pin.designator, []).append(node)
        for physical, components in by_name.items():
            if len(components) > 1:
                self.warn("duplicate_designators", "Different components compile to one "
                          "designator (Altium: Duplicate Part Designators): the netlist "
                          "cannot tell their pins apart. Annotate them apart in Altium.",
                          physical)
            for component in components:
                for nodes in component["pins"].values():
                    for other in nodes[1:]:
                        self.uf.union(nodes[0], other)
        channel_parts = sum(1 for held in by_name.values() for c in held if c["channel"])
        if channel_parts:
            self.warnings["channel_designators"] = {
                "kind": "channel_designators", "count": channel_parts,
                "message": ("Parts on sheets compiled more than once that the .Annotation "
                            "file does not name took designators from the project's "
                            "multi-channel Designator Format and room names; no sample "
                            "project confirmed this naming: compile in Altium to confirm.")}
        return by_name

    # ----- nets, names and the result -----

    def _result(self, components: dict[str, list[dict]], scope: str) -> CompiledNetlist:
        uf = self.uf
        self.components = components
        members: dict = defaultdict(set)
        for physical, held in components.items():
            for component in held:
                for pin, nodes in component["pins"].items():
                    members[uf.find(nodes[0])].add((physical, pin))
        candidates: dict = defaultdict(list)
        for key, found in self.names.items():
            candidates[uf.find(key)].extend(found)
        single = self.design.get("NetlistSinglePinNets", "").strip() in ("1", "T")
        order = sorted(
            ((sorted(pins, key=lambda m: (natural_key(m[0]), natural_key(m[1]))), root)
             for root, pins in members.items() if len(pins) >= (1 if single else 2)),
            key=lambda item: (natural_key(item[0][0][0]), natural_key(item[0][0][1])))
        nets: dict[str, list[tuple[str, str]]] = {}
        taken: set[str] = set()
        harness_roots = {uf.find(entry) for entry, _, _ in self.harness}
        for pins, root in order:
            name, generated = self._name(candidates.get(root, []), pins)
            if name.lower() in taken:
                base, count = name, 1
                while f"{base}_{count}".lower() in taken:
                    count += 1
                name = f"{base}_{count}"
                self.warn("duplicate_net_names", "Different nets with one name (Altium: "
                          "Duplicate Nets); the later ones are suffixed _1, _2, ... here.", base)
            nets[name] = pins
            self.net_roots[root] = name
            taken.add(name.lower())
            if generated and root in harness_roots:
                self.warn("harness_member_names", "Nets joined through harness entries that "
                          "no net label names: Altium may name them after the harness "
                          "(Harness.Entry); here they take a Net<designator>_<pin> name.",
                          name)
        pin_to_net: dict[str, dict[str, str]] = {}
        for name, pins in nets.items():
            for refdes, pin in pins:
                pin_to_net.setdefault(refdes, {})[pin] = name
        parts: dict[str, dict[str, str]] = {}
        for physical, held in sorted(components.items(), key=lambda item: natural_key(item[0])):
            component = held[-1]
            meta = self.sheets[component["sheet"]].components.get(component["designator"])
            parts[physical] = {"footprint": (meta.footprint or "") if meta else "",
                               "comment": _comment(meta) if meta else ""}
        masked = sum(w.masked for w in self.wirings if w)
        if masked:
            self.warnings["compile_masked"] = {
                "kind": "compile_masked", "count": masked,
                "message": "Parts inside compile masks are left out of the netlist, as Altium "
                           "does."}
        unannotated = sum(1 for sheet in self.sheets if sheet for part in sheet.parts
                          if "?" in part.designator)
        if unannotated:
            self.warnings["unannotated_parts"] = {
                "kind": "unannotated_parts", "count": unannotated,
                "message": "Parts whose designator is not annotated (U?) are left out of the "
                           "netlist: annotate the schematic."}
        clashes = [{"sheet": Path(self.paths[i]).stem, "refdes": clash.refdes,
                    "lib_references": clash.lib_references}
                   for i, sheet in enumerate(self.sheets) if sheet for clash in sheet.clashes]
        return CompiledNetlist(
            nets=nets, pin_to_net=pin_to_net, parts=parts, scope=scope,
            warnings=list(self.warnings.values()),
            sheets=[(Path(self.paths[i]).stem, sheet.components)
                    for i, sheet in enumerate(self.sheets) if sheet],
            sheet_errors=self.sheet_errors, designator_clashes=clashes,
            channel_entries=self.channel_entries, part_paths=self.part_paths)

    def _name(self, found: list[tuple[str, str, int]],
              pins: list[tuple[str, str]]) -> tuple[str, bool]:
        """The net's name by the project's netlist options (see the module docstring), and
        whether it was generated from a pin."""
        naming = self.naming
        order = [LABEL, POWER]
        if naming.get("PowerPortNamesTakePriority") == "1":
            order = [POWER, LABEL]
        if naming.get("AllowPortNetNames") == "1":
            order.append(PORT)
        if naming.get("AllowSheetEntryNetNames") == "1":
            order.append(ENTRY)
        distinct = {name.lower() for kind, name, _ in found
                    if kind in (LABEL, POWER) and not bus_members(name)}
        if len(distinct) > 1:
            self.warn("multiple_net_names", "Nets carrying several names (net labels or power "
                      "ports); the name chosen follows the project's netlist options.",
                      ", ".join(sorted(distinct)[:4]))
        for kind in order:
            named = [(depth, name) for k, name, depth in found
                     if k == kind and name and not bus_members(name)]
            if not named:
                continue
            if naming.get("NameNetsHierarchically") == "1":
                top = min(depth for depth, _ in named)
                named = [item for item in named if item[0] == top]
            return min({name for _, name in named}, key=lambda n: (n.lower(), n)), False
        refdes, pin = pins[0]
        return f"Net{refdes}_{pin}", True


def _member(copy: int, group: int, name: str) -> tuple:
    """The union-find key of bus member `name` on one wiring group of a compiled copy."""
    return ("pm", copy, group, name.strip().lower())


def _int(text: str) -> int:
    try:
        return int(str(text).strip())
    except ValueError:
        return 0


def _alpha(number: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA."""
    text = ""
    while number > 0:
        number, rest = divmod(number - 1, 26)
        text = chr(ord("A") + rest) + text
    return text


def _comment(meta: ComponentMeta) -> str:
    """The Comment as the netlist writes it: a "=Parameter" Comment shows that value."""
    text = meta.params.get("comment", "")
    if text.startswith("="):
        return meta.params.get(text[1:].strip().lower(), text)
    return text


def compile_netlist(prj_pcb_path: str, annotation: list[AnnotationEntry] | None = None,
                    sheet_paths: list[str] | None = None,
                    read_sheet=parse_sch_connectivity) -> CompiledNetlist:
    """Compile the project's netlist from its sheets (default: those the .PrjPcb lists).

    annotation holds the project's .Annotation entries (physical designators by UniqueId
    path). read_sheet(path) returns a sheet's SheetConnectivity. A sheet that cannot be
    read is reported in sheet_errors and left out."""
    return _Compiler(prj_pcb_path, sheet_paths, list(annotation or []), read_sheet).run()


# ---------- the source files ----------

def file_signature(paths) -> tuple:
    """(path, mtime_ns, size) of each file, None for one that cannot be stat'ed: a compile
    of the same signature can be reused."""
    signature = []
    for path in paths:
        try:
            stat = os.stat(path)
            signature.append((os.path.normcase(str(path)), stat.st_mtime_ns, stat.st_size))
        except OSError:
            signature.append((os.path.normcase(str(path)), None, None))
    return tuple(signature)
