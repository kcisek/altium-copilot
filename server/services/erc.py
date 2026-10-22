"""An offline approximation of Altium's ERC (Project > Validate / Compile, the Messages
panel), from the compiled netlist's ERC facts (sch_netlister.ErcFacts) and the project's
Error Reporting levels and Connection Matrix (project_options). Altium never saves its own
ERC result in the project files, so this is the closest offline answer to "why won't my
project compile?"; Compile in Altium is the final word.

Violations carry Altium's violation-type names (project_options.ERC_CHECKS), its groups
and message wording (Altium's Design Validation page), and the project's report level for
the type, or Altium's default where the project does not set one. Types set to No Report
are not reported but counted. Connection-matrix violations (two pin, port or sheet-entry
types on one net, an object left unconnected) take the matrix cell's level, as Altium
does, and are not reported when their type is set to No Report.

An active No ERC suppresses the violations at its point, of the types it covers (a
generic one: all; a specific one: the types it names, all when they cannot be read): a
violation about an object (an unconnected pin, a floating label, a stray wire, an
unmatched port, an off-grid point) when the marker sits on that object, a violation
about a whole net (its names, its drivers, its pin types, its one pin, its floating
input) when the marker sits anywhere on the net, as Altium's own examples use it.
Suppressed violations are counted. Compile masks remove what they cover before any check
runs (the compiler leaves masked parts, wires, labels, ports, directives, markers and
masked sheet symbols' entries out of the facts).

The net checks read the compiled nets; the document checks (ports and sheet entries
matched, duplicates, missing child sheets, sheet numbers and names, the snap grid and the
sheet size, floating objects and directives) read each sheet document once
(ErcFacts.sheets). Each check is registered with erc_check(...) and appends to the
context, so later checks are added the same way. Findings that are no Altium violation
(near-duplicate power aliases, names differing only in case, near-miss wire ends, the
ground-net inventory) are advisories, level info, kept apart."""
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Callable

try:
    from parsers.sch_doc import ENTRY_REPEAT_RE
    from services.decoupling import net_kind, net_tokens
    from services.project_options import (
        ERC_CHECKS, ERC_DEFAULTS, LEVELS, MATRIX_LABELS, erc_levels, matrix_levels,
    )
    from services.pull_resistors import rail_voltage
    from services.sch_netlister import (
        JUNCTION_TOLERANCE_MIL, SHEET_ONLY, ErcFacts, ErcNet, ErcNoErc, ErcObject, ErcSheet,
        alpha, natural_key,
    )
except ImportError:
    from server.parsers.sch_doc import ENTRY_REPEAT_RE
    from server.services.decoupling import net_kind, net_tokens
    from server.services.project_options import (
        ERC_CHECKS, ERC_DEFAULTS, LEVELS, MATRIX_LABELS, erc_levels, matrix_levels,
    )
    from server.services.pull_resistors import rail_voltage
    from server.services.sch_netlister import (
        JUNCTION_TOLERANCE_MIL, SHEET_ONLY, ErcFacts, ErcNet, ErcNoErc, ErcObject, ErcSheet,
        alpha, natural_key,
    )

APPROXIMATION_NOTE = (
    "An offline approximation of Altium's ERC, from the saved sheets, the project's Error "
    "Reporting levels and its Connection Matrix. Altium's own ERC result is never saved in "
    "the project files: Compile in Altium (Messages panel) is the final word, and the "
    "checks in not_checked were not run.")
NUMBERS = {name: number for number, name in enumerate(ERC_CHECKS, 1)}
# Levels Altium's documentation gives where project_options has no agreed default
# ("Nets with only one pin": "this message will not appear by default").
DOC_DEFAULTS = {59: 0}
# No Report, assumed from the project files Altium writes where neither its documentation
# nor project_options gives a default: Duplicate sheet numbers (Type7) is 0 in 10 of
# Altium's 11 AD25 example projects, and Port with no matching ports (Type154, absent
# from those older examples) is 0 in every surveyed project that writes it.
ASSUMED_DEFAULTS = {7: 0, 154: 0}
MIN_LEVELS = {"warning": 1, "error": 2, "fatal": 3, "all": 0}
UNCONNECTED = len(MATRIX_LABELS) - 1
# The types the Connection Matrix judges (a connection pair in a specific No ERC).
MATRIX_TYPES = frozenset({"Nets with possible connection problems",
                          "Nets containing multiple similar objects",
                          "Unconnected objects in net"})

# Altium's Design Validation page groups (Violations Associated with ...).
GROUPS = {
    **dict.fromkeys((
        "Bus indices out of range", "Bus range syntax errors", "Illegal bus range values",
        "Mismatched bus label ordering", "Mismatched bus widths",
        "Mismatched Bus/Wire object on Wire/Bus", "Mixed generic and numeric bus labeling",
        "Illegal bus definitions", "Mismatched Bus-Section index ordering",
        "Mismatched electrical types on bus", "Mismatched Generics on bus (First Index)",
        "Mismatched Generics on bus (Second Index)"), "Buses"),
    **dict.fromkeys((
        "Component has been deleted", "Component Implementations with invalid pin mappings",
        "Component revision has inapplicable state", "Component revision is Out of Date",
        "Components containing duplicate sub-parts", "Components with duplicate pins",
        "Duplicate Part Designators", "Extra pin found in component display mode",
        "Generic Component", "Mismatched hidden pin connections", "Mismatched pin visibility",
        "Missing Component Models", "Missing pin found in component display mode",
        "Sheet Symbol with duplicate entries", "Sheet Symbols with duplicated indexes",
        "Un-Designated parts requiring annotation", "Unused sub-part in component"),
        "Components"),
    **dict.fromkeys((
        "Ambiguous Device Sheet Path Resolution", "Circular Document Dependency",
        "Missing child sheet for sheet symbol", "Multiple Top-Level Documents",
        "Port not linked to parent sheet symbol", "Sheet Entry not linked to child sheet",
        "Sheet Names Clash", "Duplicate sheet numbers"), "Documents"),
    **dict.fromkeys((
        "Conflicting Harness Definition", "Harness Connector Type Syntax Error",
        "Invalid Connection to a Harness Connector", "Missing Harness Type on Harness",
        "Multiple Harness Types on Harness", "Unknown Harness Type",
        "Unconnected Harness Entry"), "Harnesses"),
    **dict.fromkeys((
        "Adding hidden net to sheet", "Adding Items from hidden net to net",
        "Bus Object on a Harness", "Differential Pair Net Connection Polarity Inversed",
        "Differential Pair Net Unconnected To Differential Pair Pin",
        "Differential Pair Unproperly Connected to Device", "Duplicate Nets",
        "External and Schematic Net Names are Unsynchronized", "Floating net labels",
        "Floating power objects", "Global Power-Object scope changes",
        "Harness Object on a Bus", "Harness Object on a Wire",
        "Missing Negative Net in Differential Pair",
        "Missing Positive Net in Differential Pair", "Net Parameters with no name",
        "Net Parameters with no value", "Nets containing floating input pins",
        "Nets containing multiple similar objects", "Nets with multiple names",
        "Nets with no driving source", "Nets with only one pin",
        "Nets with possible connection problems", "Port with no matching ports",
        "Same Net used in Multiple Differential Pairs", "Sheets containing duplicate ports",
        "Unconnected objects in net", "Unconnected wires"), "Nets"),
    **dict.fromkeys((
        "Duplicated project files", "Fail to add alternate item", "Floating Directive Object",
        "Incorrect link in project variant", "Object not completely within sheet boundaries",
        "Off-grid object"), "Others"),
    **dict.fromkeys((
        "Same parameter containing different types",
        "Same parameter containing different values"), "Parameters"),
}
# What Altium's documentation recommends for each violation reported here.
FIXES = {
    "Nets with only one pin": (
        "Wire the net to the other pins it should reach, or delete the dangling wire or "
        "label; on a pin left unused on purpose place a No ERC instead."),
    "Nets with multiple names": (
        "Make the names on the net agree (net labels, power ports, sheet entries, off-sheet "
        "connectors); if several names are intended, set this check to No Report in Project "
        "Options > Error Reporting."),
    "Duplicate Nets": (
        "Use unique net labels per sheet, join the sheets with ports or sheet entries of one "
        "name, or check a broken port or sheet-entry connection between them."),
    "Global Power-Object scope changes": (
        "A power port wired to a port is local to its sheet under Hierarchical scope: wire "
        "the power port to the pins it should feed, or remove the port."),
    "Duplicate Part Designators": (
        "Give each part a unique designator: Tools > Annotation > Reset Duplicate Schematic "
        "Designators, then Annotate Schematics."),
    "Un-Designated parts requiring annotation": (
        "Annotate the schematic (Tools > Annotation > Annotate Schematics Quietly) or give the "
        "part a designator; Altium leaves it out of the netlist until then."),
    "Unused sub-part in component": (
        "Place the unused part (for a spare gate or op-amp, tie its inputs to a defined "
        "level) or confirm it is unused on purpose; its pins are in no net."),
    "Components containing duplicate sub-parts": (
        "Change one copy's part number (Edit > Increment Part Number) so each part of the "
        "component is placed once."),
    "Nets with possible connection problems": (
        "Check whether these objects really drive each other: correct a wrong pin or port "
        "type in the library or the sheet, or the connection; an intended case takes a "
        "specific No ERC or a Connection Matrix change."),
    "Nets containing multiple similar objects": (
        "Check whether the objects really share the net (two outputs fighting): correct the "
        "wiring or a wrong pin type; an intended case takes a specific No ERC."),
    "Unconnected objects in net": (
        "Wire the object if it is used; tie an unused input to a defined level and place a "
        "No ERC on it, or delete a redundant port or sheet entry."),
    "Nets containing floating input pins": (
        "Tie the unused input to a supply or ground as its datasheet says, or connect it; "
        "place a No ERC on it when it may float."),
    "Nets with no driving source": (
        "Make sure something drives the input. Passive pins (connectors, resistors) drive "
        "nothing in Altium's sense: a pull resistor or a connector feeding the input reports "
        "this; set the source pin's type (IO, Output) or place a No ERC on the net when that "
        "is intended."),
    "Net Parameters with no value": (
        "Give every parameter of the Parameter Set a value, or remove the empty one."),
    "Net Parameters with no name": (
        "Give every parameter of the Parameter Set a name, or remove the unnamed one."),
    "Missing Positive Net in Differential Pair": (
        "Name the positive net <pair>_P on the wire beside the negative one and give it a "
        "Differential Pair directive too."),
    "Missing Negative Net in Differential Pair": (
        "Name the negative net <pair>_N on the wire beside the positive one and give it a "
        "Differential Pair directive too."),
    "Floating net labels": (
        "Move the net label so its hot spot (bottom-left corner) touches the wire or bus "
        "it names, or delete it if it is redundant; a label beside a wire names nothing."),
    "Floating power objects": (
        "Wire the power port to the circuit (its hot spot must touch the wire or pin end), "
        "or delete it if it is redundant."),
    "Unconnected wires": (
        "Connect the wire to the pins, labels or ports it was meant to reach (an end must "
        "meet a pin end or another wire exactly), or delete the stray wire."),
    "Port not linked to parent sheet symbol": (
        "Add the matching sheet entry to the parent's sheet symbol (Sheet Symbol Actions > "
        "Synchronize Sheet Entries and Ports), rename the port to an existing entry, or "
        "delete the port if it is unused."),
    "Sheet Entry not linked to child sheet": (
        "Add the matching port to the child sheet (Synchronize Sheet Entries and Ports "
        "creates it), rename the entry to an existing port, or delete the entry."),
    "Missing child sheet for sheet symbol": (
        "Point the sheet symbol's File Name at a sheet of the project, add the missing "
        "sheet to the project, or restore it; nothing below the symbol is compiled."),
    "Port with no matching ports": (
        "Place a port of the same name on the sheet it should connect to, rename the port "
        "to the one it should meet, or delete it if it is unused."),
    "Sheets containing duplicate ports": (
        "Find which of the two ports is wrong and rename or delete it; one port per name "
        "on a sheet."),
    "Sheet Symbol with duplicate entries": (
        "Rename the duplicate sheet entry (or delete it) so each entry of the symbol has "
        "its own name."),
    "Duplicate sheet numbers": (
        "Renumber the sheets (Tools > Annotation > Number Schematic Sheets) so each sheet "
        "has its own SheetNumber."),
    "Sheet Names Clash": (
        "Save one of the documents under another name so no two project sheets share a "
        "file name."),
    "Off-grid object": (
        "Put the object back on the snap grid (select it and use Edit > Align > Align To "
        "Grid, or fix the library pin), or set the sheet's snap grid to the one the design "
        "uses; off-grid pin ends and wires are where connections silently fail."),
    "Object not completely within sheet boundaries": (
        "Move the object onto the sheet or enlarge the sheet (Properties panel, Page "
        "Options); objects off the sheet are compiled but not printed."),
    "Floating Directive Object": (
        "Move the directive so its hot spot touches the wire, bus or pin it is for, or "
        "delete it; a floating directive applies to nothing."),
    "Components with duplicate pins": (
        "Give each pin of the component its own designator in the library and update the "
        "part from the library."),
}
# Checks of Altium's list this offline ERC does not run (yet).
NOT_CHECKED = (
    "Differential Pair Net Connection Polarity Inversed",
    "Differential Pair Unproperly Connected to Device",
    "Differential Pair Net Unconnected To Differential Pair Pin",
    "Same Net used in Multiple Differential Pairs", "Mismatched hidden pin connections",
    "Missing Component Models", "Same parameter containing different values",
    "Same parameter containing different types", "Adding hidden net to sheet",
    "Adding Items from hidden net to net", "Multiple Top-Level Documents",
    "Circular Document Dependency", "Ambiguous Device Sheet Path Resolution",
    "bus and harness checks", "component model, revision and library checks",
)
ADVISORIES = ("near_duplicate_power_aliases", "label_casing", "near_miss_wire_ends",
              "ground_nets")
ADVISORY_LIMIT = 40
# Listed after the other violations of their level: many, and rarely why a design fails;
# a page gives them one row per sheet with a count and this many examples.
DRAWING = frozenset({"Off-grid object", "Object not completely within sheet boundaries"})
DRAWING_EXAMPLES = 5

_PIN_ROWS = {"Input": 0, "IO": 1, "Output": 2, "OpenCollector": 3, "Passive": 4, "HiZ": 5,
             "OpenEmitter": 6, "Power": 7}
_IO_ROWS = {"Input": 0, "Output": 1, "Bidirectional": 2, "Unspecified": 3}
DRIVERS = frozenset({"IO", "Output", "OpenCollector", "HiZ", "OpenEmitter", "Power"})
_KIND_WORDS = {"pin": "Pin", "hidden_pin": "Pin", "port": "Port", "entry": "Sheet Entry",
               "label": "Net Label", "power": "Power Object",
               "off_sheet": "Off Sheet Connector", "harness_entry": "Harness Entry"}
_RAIL_FILLERS = frozenset({"V", "VCC", "VDD", "VS", "P", "PWR", "POWER", "SUPPLY", "RAIL",
                           "VPWR", "VSUPPLY"})
_PAIR_SUFFIX = re.compile(r"^(.*)_([PN])$", re.IGNORECASE)
# A name token that is a voltage and nothing else (3V3, 3.3V, 5V0, P3V3, VCC3V3, VDD33).
_VOLTAGE_TOKEN = re.compile(r"^(?:VCC|VDD|VS|V|P)?(?:\d{1,2}V\d{0,3}|\d{1,2}[.,P]\d{1,3}V?)$"
                            r"|^(?:VCC|VDD)\d\d$")


# ---------- the registry ----------

@dataclass(frozen=True)
class ErcCheck:
    violations: tuple[str, ...]   # the Altium violation types it reports
    run: Callable[["_Context"], None]


REGISTRY: list[ErcCheck] = []


def erc_check(*violations: str):
    """Register a check reporting these Altium violation types (ERC_CHECKS names)."""
    unknown = [name for name in violations if name not in NUMBERS]
    if unknown:
        raise ValueError(f"not Altium violation types: {unknown}")

    def register(function):
        REGISTRY.append(ErcCheck(violations, function))
        return function
    return register


def _mil(x: float, y: float) -> str:
    return f"{round(x, 2):g}mil,{round(y, 2):g}mil"


class _Context:
    """What the checks read and the violations they append, with the project's levels."""

    def __init__(self, facts: ErcFacts, levels: dict[int, int], matrix: list[list[int]]):
        self.facts = facts
        self.levels = levels
        self.matrix = matrix
        self.found: list[dict] = []
        self.notes: list[str] = []
        # (sheet, object number) -> every compiled copy's (net, object)
        self.copies: dict[tuple[str, int], list[tuple[ErcNet, ErcObject]]] = defaultdict(list)
        for net in facts.nets:
            for item in net.objects:
                if item.obj >= 0:
                    self.copies[(item.sheet, item.obj)].append((net, item))

    def type_level(self, violation: str) -> tuple[int, str]:
        """The project's level for the type, else Altium's default (and which): a type
        with no known default is assumed No Report when Altium's own project files say so
        (ASSUMED_DEFAULTS), else Warning."""
        number = NUMBERS[violation]
        if number in self.levels:
            return self.levels[number], "project"
        default = ERC_DEFAULTS.get(number, DOC_DEFAULTS.get(number))
        if default is None:
            return ASSUMED_DEFAULTS.get(number, 1), "assumed"
        return default, "default"

    def cell(self, a: int, b: int) -> int:
        return max(self.matrix[a][b], self.matrix[b][a])

    def add(self, violation: str, *, sheet: str, obj: str, message: str,
            location: tuple[float, float] | None = None, net: ErcNet | None = None,
            matrix_level: int | None = None, marks: list[ErcNoErc] | None = None,
            **extra) -> None:
        """Append a violation. marks: the active No ERC markers sitting on what it is
        about (see _marks_on); the first that covers its type suppresses it."""
        level, _ = self.type_level(violation)
        state = "reported"
        if matrix_level is not None:
            state = "reported" if level else "no_report"
            level = matrix_level if level else 0
        elif not level:
            state = "no_report"
        row: dict = {"violation": violation, "level": LEVELS[level],
                     "group": GROUPS.get(violation, "Others"), "sheet": sheet, "object": obj}
        if net is not None:
            row["net"] = net.name
            if net.netlist_name and net.netlist_name != net.name:
                row["netlist_net"] = net.netlist_name  # what query_net knows it by
        if location is not None:
            row["x_mil"], row["y_mil"] = round(location[0], 2), round(location[1], 2)
        row["message"] = message
        row["fix"] = FIXES.get(violation, "")
        row.update(extra)
        mark = _suppressing(marks or (), violation)
        if mark is not None and state == "reported":
            state = "no_erc"
            row["suppressed_by_no_erc"] = {"sheet": mark.sheet, "x_mil": round(mark.x_mil, 2),
                                           "y_mil": round(mark.y_mil, 2)}
        sheets = {o.sheet for o in net.objects} if net is not None else set()
        sheets |= set(extra.get("sheets") or ())
        row["_sheets"] = sorted(sheets | {sheet}, key=natural_key)
        row["_state"] = state
        row["_rank"] = (-level, violation in DRAWING, NUMBERS[violation], natural_key(sheet),
                        natural_key(obj))
        self.found.append(row)


def _specific_types(error_kinds: str, connection_pairs: str) -> set[str] | None:
    """The violation types a specific No ERC lists, None when they cannot be read (then
    it is taken to suppress everything at its point). The stored format is not
    documented: names of Altium's violation types are recognised, and any connection
    pair covers the Connection Matrix types."""
    tokens = [t.strip().lower() for t in re.split(r"[,;|]", error_kinds) if t.strip()]
    names = {name.lower(): name for name in NUMBERS}
    found = {names[t] for t in tokens if t in names}
    if tokens and len(found) < len(tokens):
        return None
    if connection_pairs.strip():
        found |= MATRIX_TYPES
    return found or None


def _suppressing(marks, violation: str):
    for mark in marks:
        if mark.suppress_all:
            return mark
        types = _specific_types(mark.error_kinds, mark.connection_pairs)
        if types is None or violation in types:
            return mark
    return None


# ---------- net helpers ----------

def _marks_on(net: ErcNet, objects: list[ErcObject]) -> list[ErcNoErc]:
    """The net's active No ERC markers sitting on one of these objects: what suppresses a
    violation about those objects (an unconnected pin, port or entry). A violation about
    the net as a whole (its names, its drivers, its pin types, its one pin, a floating
    input) takes every marker on the net, net.no_ercs: designers' markers in Altium's
    examples and the surveyed boards sit on a net's wiring or stub ends, not on the
    conflicting pins (unconfirmed in Altium)."""
    keys = {item.key for item in objects}
    return [mark for mark in net.no_ercs if mark.on is not None and mark.on in keys]


def _marks_at(document: ErcSheet, x: float, y: float, obj: int = -1) -> list[ErcNoErc]:
    """The sheet's active No ERC markers on an object (by its number) or at a point."""
    return [mark for mark in (*document.no_ercs, *document.floating_no_ercs)
            if (obj >= 0 and mark.obj == obj)
            or (abs(mark.x_mil - x) <= JUNCTION_TOLERANCE_MIL
                and abs(mark.y_mil - y) <= JUNCTION_TOLERANCE_MIL)]

def _distinct(objects: list[ErcObject]) -> list[ErcObject]:
    seen, result = set(), []
    for item in objects:
        key = (item.kind if item.kind not in ("pin", "hidden_pin") else "pin", item.key)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _pins(net: ErcNet) -> list[ErcObject]:
    return [o for o in _distinct(net.objects) if o.kind in ("pin", "hidden_pin")]


def _alone(net: ErcNet) -> ErcObject | None:
    """The one object of a net nothing else joins (no wire, no other object)."""
    objects = _distinct(net.objects)
    return objects[0] if len(objects) == 1 and not net.wired else None


def _single_pin(net: ErcNet) -> ErcObject | None:
    """The pin of a net with one pin and something else on it (Altium: a pin attached to a
    wire or net label, but no other pins on the net)."""
    if len(net.pins) != 1:
        return None
    pins = _pins(net)
    if not pins or (not net.wired and len(_distinct(net.objects)) == 1
                    and pins[0].kind == "pin"):
        return None
    return pins[0]


def _row(item: ErcObject) -> int | None:
    """The object's Connection Matrix row (MATRIX_LABELS order), None when it has none."""
    if item.kind in ("pin", "hidden_pin"):
        return _PIN_ROWS.get(item.io_type)
    if item.bus or item.io_type not in _IO_ROWS:
        return None
    if item.kind == "port":
        return 8 + _IO_ROWS[item.io_type]
    if item.kind == "entry":
        return 12 + _IO_ROWS[item.io_type]
    return None


def _ref(item: ErcObject) -> str:
    return f"{_KIND_WORDS.get(item.kind, item.kind)} {item.text}"


def _refs(items: list[ErcObject], limit: int = 8) -> str:
    text = ", ".join(_ref(item) for item in items[:limit])
    return text + (f", ... {len(items) - limit} more" if len(items) > limit else "")


def _plural(label: str) -> str:
    return label[:-1] + "ies" if label.endswith("y") else label + "s"


def _letter(part_id: str) -> str:
    """A part id as Altium letters it (1 -> A)."""
    return alpha(int(part_id)) if part_id.strip().isdigit() else part_id


def _parts_in(part_count: str) -> int:
    try:
        return int(part_count) - 1
    except ValueError:
        return 1


# ---------- checks: nets ----------

@erc_check("Nets with only one pin")
def _single_pin_nets(ctx: _Context) -> None:
    for net in ctx.facts.nets:
        pin = _single_pin(net)
        if pin is not None:
            ctx.add("Nets with only one pin", sheet=pin.sheet, obj=f"Pin {pin.text}", net=net,
                    location=(pin.x_mil, pin.y_mil), marks=net.no_ercs,
                    message=f"Net {net.name} has only one pin (Pin {pin.text})")


def _identifiers(net: ErcNet, naming: dict[str, str]) -> list[ErcObject]:
    """The objects naming the net: net labels, power ports and off-sheet connectors, sheet
    entries and ports when the project lets them name nets; bus names left out."""
    kinds = {"label", "power", "off_sheet"}
    if naming.get("AllowSheetEntryNetNames") == "1":
        kinds.add("entry")
    if naming.get("AllowPortNetNames") == "1":
        kinds.add("port")
    return [o for o in net.objects if o.kind in kinds and not o.bus and o.text]


@erc_check("Nets with multiple names")
def _multiple_names(ctx: _Context) -> None:
    for net in ctx.facts.nets:
        named: dict[str, ErcObject] = {}
        for item in _identifiers(net, ctx.facts.naming):
            named.setdefault(item.text.lower(), item)
        if len(named) < 2:
            continue
        items = sorted(named.values(), key=lambda o: (natural_key(o.text), o.kind))
        first = items[0]
        ctx.add("Nets with multiple names", sheet=first.sheet, obj=f"Wire {net.name}", net=net,
                location=(first.x_mil, first.y_mil),
                marks=net.no_ercs,
                message=f"Nets Wire {net.name} has multiple names ({_refs(items, 12)})",
                names=[item.text for item in items])


@erc_check("Duplicate Nets")
def _duplicate_nets(ctx: _Context) -> None:
    if ctx.facts.design.get("AppendSheetNumberToLocalNets", "").strip() in ("1", "T"):
        return  # Altium appends the sheet number to local names: no duplicates
    by_name: dict[str, list[ErcNet]] = defaultdict(list)
    for net in ctx.facts.nets:
        if net.name and not net.generated and net.pins:
            by_name[net.name.lower()].append(net)
    for nets in by_name.values():
        if len(nets) < 2:
            continue
        copies, marks = [], []
        for net in nets:
            marks += net.no_ercs
            sheets = sorted({o.sheet for o in net.objects}, key=natural_key)
            copies.append({"sheets": sheets, "pins": len(net.pins),
                           "netlist_net": net.netlist_name})
        first = nets[0]
        anchor = next((o for o in first.objects if o.text.lower() == first.name.lower()),
                      first.objects[0])
        ctx.add("Duplicate Nets", sheet=anchor.sheet, obj=f"Wire {first.name}",
                location=(anchor.x_mil, anchor.y_mil), marks=marks,
                message=f"Duplicate Net Names Wire {first.name}", net_copies=copies)


@erc_check("Global Power-Object scope changes")
def _scope_changes(ctx: _Context) -> None:
    """A power port wired to a port or an off-sheet connector under Hierarchical scope
    (the compile makes it local to its sheet)."""
    for change in ctx.facts.scope_changes:
        marks = [mark for net, item in ctx.copies.get((change.sheet, change.obj), ())
                 for mark in _marks_on(net, [item])]
        ctx.add("Global Power-Object scope changes", sheet=change.sheet,
                obj=f"Power Object {change.name}", location=(change.x_mil, change.y_mil),
                marks=marks,
                message=(f"Global Power-Object {change.name} at "
                         f"{_mil(change.x_mil, change.y_mil)} has been reduced to local level "
                         f"by presence of port at {_mil(change.port_x_mil, change.port_y_mil)}"),
                port=change.port, port_object=change.port_kind)


# ---------- checks: the Connection Matrix and driving sources ----------

def _typed(net: ErcNet) -> list[ErcObject]:
    """The net's objects with a matrix row, each once per sheet copy."""
    seen, result = set(), []
    for item in net.objects:
        if _row(item) is None:
            continue
        key = (item.copy, "pin" if item.kind == "hidden_pin" else item.kind, item.key)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def matrix_pairs(net: ErcNet, scope: str, cell) -> dict[tuple[int, int], list[ErcObject]]:
    """The object-type pairs on a net the Connection Matrix judges (cell(a, b) > 0), with
    the objects of each. Pins meet pins across the whole net. Ports and sheet entries
    point into or out of their own sheet, so each meets the objects on its sheet copy, a
    sheet entry also the ports of the sheet its symbol places, and under Flat and Global
    scope (ports join by name) ports meet ports on every sheet."""
    typed = _typed(net)
    found: dict[tuple[int, int], dict[int, ErcObject]] = defaultdict(dict)
    rows: dict[int, dict[tuple, ErcObject]] = defaultdict(dict)
    for item in typed:
        if item.kind in ("pin", "hidden_pin"):
            rows[_row(item)].setdefault(item.key, item)
    order = sorted(rows)
    for index, a in enumerate(order):
        for b in order[index:]:
            if (a != b or len(rows[a]) > 1) and cell(a, b):
                for item in [*rows[a].values(), *(rows[b].values() if a != b else ())]:
                    found[(a, b)][id(item)] = item
    by_copy: dict[int, list[ErcObject]] = defaultdict(list)
    for item in typed:
        by_copy[item.copy].append(item)
    ports = [item for item in typed if item.kind == "port"]
    for item in typed:
        if item.kind not in ("port", "entry"):
            continue
        others = [other for other in by_copy[item.copy] if other is not item]
        if item.kind == "entry":
            others += [port for port in ports if port.copy in item.peers]
        elif scope in ("Flat", "Global"):
            others += [port for port in ports if port.copy != item.copy]
        for other in others:
            a, b = sorted((_row(item), _row(other)))
            if cell(a, b):
                held = found[(a, b)]
                held.setdefault(id(item), item)
                held.setdefault(id(other), other)
    return {pair: list(held.values()) for pair, held in found.items()}


@erc_check("Nets containing multiple similar objects", "Nets with possible connection problems")
def _matrix_pairs(ctx: _Context) -> None:
    """Every pair of object types the matrix marks, reported once per net and pair."""
    for net in ctx.facts.nets:
        for (a, b), items in sorted(matrix_pairs(net, ctx.facts.scope, ctx.cell).items()):
            first = items[0]
            if a == b:
                violation = "Nets containing multiple similar objects"
                message = (f"Net {net.name} contains multiple {_plural(MATRIX_LABELS[a])} "
                           f"({_refs(items)})")
            else:
                violation = "Nets with possible connection problems"
                message = (f"Net {net.name} contains {MATRIX_LABELS[a]} and "
                           f"{MATRIX_LABELS[b]} objects ({_refs(items)})")
            ctx.add(violation, sheet=first.sheet, obj=_ref(first), net=net,
                    location=(first.x_mil, first.y_mil), message=message,
                    marks=net.no_ercs,
                    matrix_level=ctx.cell(a, b), pair=[MATRIX_LABELS[a], MATRIX_LABELS[b]])


@erc_check("Unconnected objects in net", "Nets containing floating input pins")
def _unconnected(ctx: _Context) -> None:
    """Objects not wired up to the rest of the circuit, by the matrix's Unconnected column:
    a pin alone on its net, and a port or sheet entry nothing touches on its own sheet
    (even when its net goes on through the hierarchy). An Input pin that is the only pin
    of its net floats: alone, or on wiring, labels or ports that reach no other pin."""
    for net in ctx.facts.nets:
        alone = _alone(net)
        loose = [alone] if alone is not None and alone.kind == "pin" else []
        loose += [item for item in _distinct(net.objects)
                  if item.kind in ("port", "entry") and not item.attached]
        for item in loose:
            row = _row(item)
            level = ctx.cell(row, UNCONNECTED) if row is not None else 0
            if level:
                ctx.add("Unconnected objects in net", sheet=item.sheet, obj=_ref(item),
                        net=net, location=(item.x_mil, item.y_mil), matrix_level=level,
                        marks=_marks_on(net, [item]),
                        message=f"Unconnected {_ref(item)} at {_mil(item.x_mil, item.y_mil)}")
        pins = _pins(net)
        if len(net.pins) == 1 and len(pins) == 1 and pins[0].kind == "pin" \
                and pins[0].io_type == "Input":
            item = pins[0]
            ctx.add("Nets containing floating input pins", sheet=item.sheet,
                    obj=f"Pin {item.text}", net=net, location=(item.x_mil, item.y_mil),
                    marks=net.no_ercs,
                    message=f"Net {net.name} contains floating input pins (Pin {item.text})")


@erc_check("Nets with no driving source")
def _no_driving_source(ctx: _Context) -> None:
    """A net with an input pin and no pin of a driving type (IO, Output, Open Collector,
    HiZ, Open Emitter, Power), listing the net's pins as Altium does; an input alone (in
    no net) is only a floating input."""
    for net in ctx.facts.nets:
        pins = _pins(net)
        inputs = [pin for pin in pins if pin.io_type == "Input"]
        if not inputs or _alone(net) is not None:
            continue
        if any(pin.io_type in DRIVERS or not pin.io_type for pin in pins):
            continue  # driven, or a pin of unknown type
        first = inputs[0]
        listed = sorted(pins, key=lambda pin: natural_key(pin.text))
        ctx.add("Nets with no driving source", sheet=first.sheet, obj=f"Net {net.name}",
                net=net, location=(first.x_mil, first.y_mil), marks=net.no_ercs,
                message=f"Net {net.name} has no driving source ({_refs(listed)})",
                inputs=[pin.text for pin in inputs])


# ---------- checks: components ----------

@erc_check("Un-Designated parts requiring annotation")
def _undesignated(ctx: _Context) -> None:
    seen: set[tuple[str, int]] = set()
    for part in ctx.facts.parts:
        if "?" in part.designator and not part.masked and (part.sheet, part.index) not in seen:
            seen.add((part.sheet, part.index))
            ctx.add("Un-Designated parts requiring annotation", sheet=part.sheet,
                    obj=part.designator, location=(part.x_mil, part.y_mil),
                    message=f"Un-Designated Part {part.designator}")


@erc_check("Duplicate Part Designators", "Components containing duplicate sub-parts",
           "Unused sub-part in component")
def _designators(ctx: _Context) -> None:
    placed: dict[str, list] = defaultdict(list)
    for part in ctx.facts.parts:
        if part.physical:
            placed[part.physical].append(part)
    # Masked copies count as placed, so masking a part never reads as an unused one.
    masked: dict[tuple[int, str], set[str]] = defaultdict(set)
    for part in ctx.facts.parts:
        if part.masked:
            masked[(part.copy, part.designator)].add(part.part_id)
    for physical in sorted(placed, key=natural_key):
        parts = placed[physical]
        kinds = {part.part_count for part in parts}
        multi = len(kinds) == 1 and _parts_in(parts[0].part_count) >= 2
        first = parts[0]
        if len(kinds) > 1 or (not multi and len(parts) > 1):
            here = (first.sheet, first.x_mil, first.y_mil)
            other = next((p for p in parts[1:] if (p.sheet, p.x_mil, p.y_mil) != here),
                         parts[1])
            extra = {}
            renamed = sorted({p.designator for p in parts if p.annotated
                              and p.designator != physical}, key=natural_key)
            if renamed:
                # The clash is the board-level .Annotation file's doing: say so before
                # anyone re-annotates a design Altium may compile cleanly.
                stale = ctx.facts.stale_annotation
                extra["note"] = (
                    f"{physical} comes from the project's .Annotation file for the part(s) "
                    f"drawn as {', '.join(renamed)}"
                    + (f"; {stale} .Annotation entries name no placed part, so the file "
                       "may be stale" if stale else "")
                    + ": check Altium's compile before re-annotating.")
            ctx.add("Duplicate Part Designators", sheet=first.sheet, obj=physical,
                    location=(first.x_mil, first.y_mil),
                    message=(f"Duplicate Component Designators {physical} at "
                             f"{_mil(first.x_mil, first.y_mil)} and "
                             f"{_mil(other.x_mil, other.y_mil)}"),
                    placed=len(parts),
                    sheets=sorted({p.sheet for p in parts}, key=natural_key), **extra)
            continue
        if not multi:
            continue
        ids = Counter(part.part_id for part in parts)
        for part_id in sorted(ids, key=natural_key):
            if ids[part_id] > 1:
                copies = [part for part in parts if part.part_id == part_id]
                a, b = copies[0], copies[1]
                ctx.add("Components containing duplicate sub-parts", sheet=a.sheet,
                        obj=f"{physical}{_letter(part_id)}", location=(a.x_mil, a.y_mil),
                        message=(f"Component {physical}{_letter(part_id)} has duplicate "
                                 f"sub-parts at {_mil(a.x_mil, a.y_mil)} and "
                                 f"{_mil(b.x_mil, b.y_mil)}"))
        used = set(ids)
        for part in parts:
            used |= masked.get((part.copy, part.designator), set())
        unused = [str(n) for n in range(1, _parts_in(first.part_count) + 1)
                  if str(n) not in used]
        if unused:
            ctx.add("Unused sub-part in component", sheet=first.sheet, obj=physical,
                    location=(first.x_mil, first.y_mil),
                    message=f"Component {physical} has unused sub-part(s) ({', '.join(unused)})",
                    unused_parts=[_letter(n) for n in unused])


# ---------- checks: parameter sets ----------

@erc_check("Net Parameters with no value", "Net Parameters with no name")
def _net_parameters(ctx: _Context) -> None:
    seen: set[tuple[str, float, float]] = set()
    for item in ctx.facts.parameter_sets:
        where = (item.sheet, item.x_mil, item.y_mil)
        if not item.attached or where in seen:
            continue
        seen.add(where)
        location = (item.x_mil, item.y_mil)
        if any(name.strip() and not value.strip() for name, value in item.parameters):
            ctx.add("Net Parameters with no value", sheet=item.sheet, obj="Parameter Set",
                    location=location, message=f"Invalid net-parameter value at {_mil(*location)}",
                    parameters=[name for name, value in item.parameters
                                if name.strip() and not value.strip()])
        if any(not name.strip() for name, _ in item.parameters):
            ctx.add("Net Parameters with no name", sheet=item.sheet, obj="Parameter Set",
                    location=location, message=f"Invalid net-parameter name at {_mil(*location)}")


def _is_pair_directive(parameters: list[tuple[str, str]]) -> bool:
    return any(name.strip().lower() == "differentialpair"
               and value.strip().lower() in ("true", "t", "1") for name, value in parameters)


@erc_check("Missing Positive Net in Differential Pair", "Missing Negative Net in Differential Pair")
def _diff_pairs(ctx: _Context) -> None:
    """Differential Pair directives on nets named <pair>_P / <pair>_N (Altium's default
    suffixes): a polarity without its partner anywhere in the design. A net carrying a
    channel's room suffix pairs only within that channel (the suffix set aside)."""
    pairs: dict[tuple[str, str], dict[str, object]] = defaultdict(dict)
    for item in ctx.facts.parameter_sets:
        if not item.net or not _is_pair_directive(item.parameters):
            continue
        name, channel = item.net, ""
        if item.room and name.lower().endswith(f"_{item.room.lower()}"):
            name, channel = name[:-len(item.room) - 1], item.room.lower()
        match = _PAIR_SUFFIX.match(name)
        if match:
            pairs[(channel, match.group(1).lower())].setdefault(
                match.group(2).upper(), (match.group(1), item))
    for (_, _), held in sorted(pairs.items(), key=lambda kv: (natural_key(kv[0][0]),
                                                               natural_key(kv[0][1]))):
        if len(held) == 2:
            continue
        polarity, (base, item) = next(iter(held.items()))
        missing = "Negative" if polarity == "P" else "Positive"
        known = "positive" if polarity == "P" else "negative"
        ctx.add(f"Missing {missing} Net in Differential Pair", sheet=item.sheet,
                obj=f"Differential Pair {base}", location=(item.x_mil, item.y_mil),
                message=(f"Missing {missing} Net for differential pair {base}, {known} net "
                         f"{item.net}"))


# ---------- checks: floating objects and wires ----------

@erc_check("Floating net labels", "Floating power objects")
def _floating(ctx: _Context) -> None:
    """Net labels and power ports touching no wire, bus, pin or other object at their
    hot spot (a name joins them to nothing on its own)."""
    for document in ctx.facts.sheets:
        for item in document.objects:
            if item.kind not in ("label", "power") or item.attached:
                continue
            label = item.kind == "label"
            ctx.add("Floating net labels" if label else "Floating power objects",
                    sheet=document.name, obj=_ref(item), location=(item.x_mil, item.y_mil),
                    marks=_marks_at(document, item.x_mil, item.y_mil, item.obj),
                    message=(f"Floating Net Label {item.text}" if label
                             else f"Floating Power Object {item.text}"))


@erc_check("Unconnected wires")
def _unconnected_wires(ctx: _Context) -> None:
    """Wires whose wiring reaches no pin, label, power port, port, sheet entry or other
    object (junctions aside); one violation per wire."""
    for document in ctx.facts.sheets:
        # A No ERC on one wire of a stray piece of wiring covers the whole piece.
        marked: dict[int, list[ErcNoErc]] = defaultdict(list)
        for mark in document.no_ercs:
            if mark.line is not None:
                marked[document.line_group[mark.line]].append(mark)
        for line in document.unconnected:
            vertices = document.wires[line]
            (x0, y0), (x1, y1) = vertices[0], vertices[-1]
            ctx.add("Unconnected wires", sheet=document.name, obj="Wire", location=(x0, y0),
                    marks=marked.get(document.line_group[line]),
                    message=f"Unconnected line {_mil(x0, y0)} To {_mil(x1, y1)}")


# ---------- checks: ports, sheet entries and documents ----------

def _port_name(name: str) -> str:
    """The port a sheet entry connects to (Repeat(X) -> X), lower case."""
    match = ENTRY_REPEAT_RE.match(name)
    return (match.group(1) if match else name).strip().lower()


def _ports(document: ErcSheet) -> list[ErcObject]:
    return [item for item in document.objects if item.kind == "port"]


@erc_check("Port not linked to parent sheet symbol", "Sheet Entry not linked to child sheet",
           "Missing child sheet for sheet symbol")
def _hierarchy_links(ctx: _Context) -> None:
    """Ports of a child sheet with no entry of their name on a symbol placing it (under a
    hierarchical scope, where only entries connect them upward), sheet entries with no
    port of their name on the child sheet, and symbols whose file is no project sheet."""
    documents = {document.name.lower(): document for document in ctx.facts.sheets}
    hierarchical = ctx.facts.scope in ("Hierarchical", "Strict Hierarchical")
    for document in ctx.facts.sheets:
        for symbol in document.symbols:
            where = (symbol.x_mil, symbol.y_mil)
            if symbol.child is None:
                ctx.add("Missing child sheet for sheet symbol", sheet=document.name,
                        obj=f"Sheet Symbol {symbol.designator}", location=where,
                        message=(f"Missing child-sheet in {symbol.file_name or '(no file)'} "
                                 f"in Symbol {symbol.designator}"),
                        file_name=symbol.file_name)
                continue
            child = documents.get(symbol.child.lower())
            if child is None:
                continue  # the child sheet could not be read
            ports = {port.text.strip().lower() for port in _ports(child)}
            for entry in symbol.entries:
                if _port_name(entry.text) not in ports:
                    ctx.add("Sheet Entry not linked to child sheet", sheet=document.name,
                            obj=f"Sheet Entry {entry.text}",
                            location=(entry.x_mil, entry.y_mil),
                            marks=_marks_at(document, entry.x_mil, entry.y_mil, entry.obj),
                            message=(f"Sheet-Entry {entry.text} not matched to Port at "
                                     f"{_mil(entry.x_mil, entry.y_mil)}"),
                            symbol=symbol.designator, child=child.name)
        if not hierarchical:
            continue
        for parent_name, index in document.placed_by:
            parent = documents.get(parent_name.lower())
            if parent is None:
                continue
            symbol = next((s for s in parent.symbols if s.index == index), None)
            if symbol is None:
                continue
            entries = {_port_name(entry.text) for entry in symbol.entries}
            for port in _ports(document):
                if port.text.strip().lower() not in entries:
                    ctx.add("Port not linked to parent sheet symbol", sheet=document.name,
                            obj=f"Port {port.text}", location=(port.x_mil, port.y_mil),
                            marks=_marks_at(document, port.x_mil, port.y_mil, port.obj),
                            message=(f"Port {port.text} not matched to Sheet-Entry at "
                                     f"{_mil(port.x_mil, port.y_mil)}"),
                            parent=parent.name, symbol=symbol.designator)


@erc_check("Port with no matching ports")
def _unmatched_ports(ctx: _Context) -> None:
    """Flat and Global scope, where ports join by name: a port whose name no other port
    in the project, nor an entry of a symbol placing its sheet, carries."""
    if ctx.facts.scope not in ("Flat", "Global"):
        return
    documents = {document.name.lower(): document for document in ctx.facts.sheets}
    where: Counter = Counter()
    for document in ctx.facts.sheets:
        for port in _ports(document):
            where[port.text.strip().lower()] += 1
    for document in ctx.facts.sheets:
        above = set()
        for parent_name, index in document.placed_by:
            parent = documents.get(parent_name.lower())
            for symbol in parent.symbols if parent else ():
                if symbol.index == index:
                    above |= {_port_name(entry.text) for entry in symbol.entries}
        for port in _ports(document):
            name = port.text.strip().lower()
            if where[name] > 1 or name in above:
                continue  # another port of its name (two on one sheet: duplicate ports)
            ctx.add("Port with no matching ports", sheet=document.name,
                    obj=f"Port {port.text}", location=(port.x_mil, port.y_mil),
                    marks=_marks_at(document, port.x_mil, port.y_mil, port.obj),
                    message=(f"Port {port.text} Port with no matching ports detected at "
                             f"{_mil(port.x_mil, port.y_mil)}"))


def _first_two(items: list[ErcObject]) -> tuple[ErcObject, ErcObject]:
    ordered = sorted(items, key=lambda o: (o.y_mil * -1, o.x_mil))
    return ordered[0], ordered[1]


@erc_check("Sheets containing duplicate ports", "Sheet Symbol with duplicate entries")
def _duplicate_ports(ctx: _Context) -> None:
    for document in ctx.facts.sheets:
        named: dict[str, list[ErcObject]] = defaultdict(list)
        for port in _ports(document):
            named[port.text.strip().lower()].append(port)
        for items in named.values():
            if len(items) > 1:
                a, b = _first_two(items)
                ctx.add("Sheets containing duplicate ports", sheet=document.name,
                        obj=f"Port {a.text}", location=(a.x_mil, a.y_mil),
                        marks=[m for o in (a, b) for m in _marks_at(document, o.x_mil,
                                                                    o.y_mil, o.obj)],
                        message=(f"Sheet contains duplicate ports Port {a.text} at "
                                 f"{_mil(a.x_mil, a.y_mil)} and {_mil(b.x_mil, b.y_mil)}"),
                        count=len(items))
        for symbol in document.symbols:
            entries: dict[str, list[ErcObject]] = defaultdict(list)
            for entry in symbol.entries:
                entries[entry.text.strip().lower()].append(entry)
            for items in entries.values():
                if len(items) > 1:
                    a, b = _first_two(items)
                    ctx.add("Sheet Symbol with duplicate entries", sheet=document.name,
                            obj=f"Sheet Symbol {symbol.designator}",
                            location=(a.x_mil, a.y_mil),
                            message=(f"Sheet Symbol with duplicate entries Sheet Entry "
                                     f"{a.text} at {_mil(a.x_mil, a.y_mil)} and "
                                     f"{_mil(b.x_mil, b.y_mil)}"),
                            symbol=symbol.designator)


@erc_check("Duplicate sheet numbers", "Sheet Names Clash")
def _documents(ctx: _Context) -> None:
    """Sheets numbered alike (the SheetNumber sheet parameter; blank and * left out) and
    project sheets sharing a file name in different folders."""
    numbered: dict[str, list[ErcSheet]] = defaultdict(list)
    for document in ctx.facts.sheets:
        number = document.sheet_number.strip()
        if number and number != "*" and not number.startswith("="):
            numbered[number.lower()].append(document)
    for documents in numbered.values():
        if len(documents) > 1:
            names = sorted((d.name for d in documents), key=natural_key)
            ctx.add("Duplicate sheet numbers", sheet=names[0], obj="Sheet",
                    message=(f"Duplicate sheet number {documents[0].sheet_number.strip()} on "
                             f"sheets {', '.join(names)}"),
                    sheets=names)
    named: dict[str, list[str]] = defaultdict(list)
    for path in ctx.facts.sheet_paths:
        named[PureWindowsPath(path).name.lower()].append(path)
    for paths in named.values():
        if len({os.path.normcase(os.path.normpath(p)) for p in paths}) > 1:
            name = PureWindowsPath(paths[0]).name
            stem = PureWindowsPath(paths[0]).stem
            ctx.add("Sheet Names Clash", sheet=stem, obj=name,
                    message=f"Project {ctx.facts.project} contains several documents named {name}",
                    paths=len(paths))


# ---------- checks: drawing ----------

# On grid when within this of a grid line (coordinates are stored to 0.0001 mil; a metric
# grid's size is rounded in the file).
GRID_TOLERANCE_MIL = 0.1


def _off_grid(value: float, grid: float) -> bool:
    return abs(value - round(value / grid) * grid) > GRID_TOLERANCE_MIL


def _grid_points(document: ErcSheet):
    """(object, x, y, object number or -1, identity) for every point the grid and sheet
    checks read; one identity per object (a wire's vertices share one)."""
    for label, x, y, obj in document.points:
        yield label, x, y, obj, ("o", obj) if obj >= 0 else ("p", label, round(x), round(y))
    for line, vertices in enumerate(document.wires):
        for x, y in vertices:
            yield "Wire", x, y, -1, ("w", line)


@erc_check("Off-grid object", "Object not completely within sheet boundaries")
def _drawing(ctx: _Context) -> None:
    """Pin ends, wire vertices, net labels, power ports, ports, junctions, off-sheet
    connectors and sheet symbol corners off the sheet's snap grid (when its header gives
    one and snap is on), or beyond the sheet's size; one violation per object."""
    unchecked = []
    for document in ctx.facts.sheets:
        grid = document.snap_grid_mil
        if not grid or grid <= 0 or not document.snap_grid_on:
            if document.points or document.wires:
                unchecked.append(document.name)
            grid = None
        sizes = [(document.width_mil, document.height_mil)]
        if document.sheet_style == "Custom" and document.portrait:
            sizes.append((document.height_mil, document.width_mil))
        off_grid: dict[tuple, tuple[str, float, float, int]] = {}
        off_sheet: dict[tuple, tuple[str, float, float, int]] = {}
        for label, x, y, obj, identity in _grid_points(document):
            if grid and obj != SHEET_ONLY and (_off_grid(x, grid) or _off_grid(y, grid)):
                off_grid.setdefault(identity, (label, x, y, obj))
            if sizes[0][0] > 0 and not any(0 <= x <= w and 0 <= y <= h for w, h in sizes):
                off_sheet.setdefault(identity, (label, x, y, obj))
        for violation, found, word in (("Off-grid object", off_grid, "Off grid"),
                                       ("Object not completely within sheet boundaries",
                                        off_sheet, "Off sheet")):
            for label, x, y, obj in found.values():
                ctx.add(violation, sheet=document.name, obj=label, location=(x, y),
                        marks=_marks_at(document, x, y, obj),
                        message=f"{word} {label} at {_mil(x, y)}")
    if unchecked:
        shown = ", ".join(unchecked[:10]) + (f" and {len(unchecked) - 10} more"
                                             if len(unchecked) > 10 else "")
        ctx.notes.append(f"Off-grid object was not checked on {len(unchecked)} sheet(s) whose "
                         f"header gives no snap grid, or has snap off: {shown}.")


@erc_check("Floating Directive Object")
def _floating_directives(ctx: _Context) -> None:
    """Parameter Set and Differential Pair directives on no wire, bus, signal harness or
    sheet symbol (what Altium attaches them to), and No ERC markers on no object, wire or
    bus."""
    seen: set[tuple[str, float, float]] = set()
    for item in ctx.facts.parameter_sets:
        where = (item.sheet, item.x_mil, item.y_mil)
        if item.attached or where in seen:
            continue
        seen.add(where)
        kind = ("Differential Pair directive" if _is_pair_directive(item.parameters)
                else "Parameter Set directive")
        ctx.add("Floating Directive Object", sheet=item.sheet, obj=kind,
                location=(item.x_mil, item.y_mil),
                message=f"Floating {kind} at {_mil(item.x_mil, item.y_mil)}")
    for document in ctx.facts.sheets:
        for mark in document.floating_no_ercs:
            ctx.add("Floating Directive Object", sheet=document.name, obj="NoERC directive",
                    location=(mark.x_mil, mark.y_mil),
                    message=f"Floating NoERC directive at {_mil(mark.x_mil, mark.y_mil)}")


@erc_check("Components with duplicate pins")
def _duplicate_pins(ctx: _Context) -> None:
    """Pins of one placed part sharing a designator (in its current display mode)."""
    for document in ctx.facts.sheets:
        seen: set[tuple[str, str]] = set()
        for part, pin, first, second in document.duplicate_pins:
            key = (part.lower(), pin.lower())
            if key in seen:
                continue
            seen.add(key)
            ctx.add("Components with duplicate pins", sheet=document.name, obj=part,
                    location=first,
                    message=f"Duplicate pins in component Pin {part}-{pin} and Pin {part}-{pin}",
                    pin=pin, at=[_mil(*first), _mil(*second)])


# ---------- advisories ----------

def _rail_voltage(name: str) -> float | None:
    """The voltage of a plain supply name (3V3, +3.3V, VCC_3V3, P3V3), None for a ground,
    a signal, a name without a voltage, or one with a qualifier (3V3_A, VDD_IO_1V8,
    2V5OUT, 3V3A)."""
    kind = net_kind(name)
    if not kind["power"] or kind["ground"] or kind["signal"] or kind["status"]:
        return None
    volts = rail_voltage(name)
    if volts is None:
        return None
    for token in net_tokens(name):
        token = token.strip("+-")
        if not token or token in _RAIL_FILLERS:
            continue
        if _VOLTAGE_TOKEN.match(token):
            continue
        return None  # a qualifier, also one glued to the voltage (2V5OUT, 3V3A)
    return volts


def _advisories(facts: ErcFacts) -> list[dict]:
    nets = [net for net in facts.nets if net.pins and net.name]
    result: list[dict] = []
    rails: dict[float, dict[str, ErcNet]] = defaultdict(dict)
    for net in nets:
        if net.generated:
            continue
        volts = _rail_voltage(net.name)
        if volts is not None:
            rails[round(volts, 3)].setdefault(net.name.lower(), net)
    for volts in sorted(rails):
        named = rails[volts]
        if len(named) < 2:
            continue
        listed = sorted(named.values(), key=lambda n: natural_key(n.name))
        result.append({
            "advisory": "near_duplicate_power_aliases", "level": "info",
            "voltage": volts, "nets": [{"net": n.name, "pins": len(n.pins)} for n in listed],
            "message": (f"{', '.join(n.name for n in listed)} read as one {volts:g} V supply "
                        "under different names, yet they are separate nets. If they are one "
                        "rail, use one name so they join; if they are different rails, a "
                        "qualifier in the name (e.g. 3V3_A) says so."),
        })
    spellings: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for net in facts.nets:
        for item in net.objects:
            if item.kind in ("label", "power", "port", "entry", "off_sheet") and item.text:
                spellings[item.text.lower()][item.text].add(item.sheet)
    for key in sorted(spellings, key=natural_key):
        held = spellings[key]
        if len(held) < 2:
            continue
        names = sorted(held, key=lambda s: (natural_key(s), s))
        result.append({
            "advisory": "label_casing", "level": "info", "names": names,
            "sheets": sorted({s for sheets in held.values() for s in sheets}, key=natural_key),
            "message": (f"{' / '.join(names)} differ only in case: Altium matches names "
                        "regardless of case, but scripts, PCB rules and classes may not; "
                        "use one spelling."),
        })
    for document in facts.sheets:
        if document.near_misses:
            result.append({
                "advisory": "near_miss_wire_ends", "level": "info", "sheet": document.name,
                "ends": [{"pin": pin, "x_mil": round(x, 2), "y_mil": round(y, 2),
                          "distance_mil": distance}
                         for pin, x, y, distance in document.near_misses],
                "message": ("Wire ends that touch nothing yet stop a few mil short of a pin's "
                            "end: drawn to the pin, but not connected to it (Altium joins a "
                            "wire to a pin only at its end). Move the wire end onto the pin."),
            })
    grounds = sorted((net for net in nets if net_kind(net.name)["ground"]),
                     key=lambda n: (-len(n.pins), natural_key(n.name)))
    if grounds:
        result.append({
            "advisory": "ground_nets", "level": "info",
            "nets": [{"net": n.name, "pins": len(n.pins),
                      "sheets": len({o.sheet for o in n.objects})} for n in grounds],
            "message": (f"{len(grounds)} ground net(s). Where there are several (GND, AGND, "
                        "PGND, chassis), check that they join only where intended (a net tie, "
                        "0 ohm part or ferrite) and nowhere else."),
        })
    return result


# ---------- the model and its pages ----------

def single_pin_nets(facts: ErcFacts) -> list[dict]:
    """Every single-pin net with its pin, sheet and the objects on it, whatever the level
    of "Nets with only one pin"."""
    rows = []
    for net in facts.nets:
        pin = _single_pin(net)
        if pin is None:
            continue
        row = {"net": net.name, "pin": pin.text, "sheet": pin.sheet,
               "x_mil": round(pin.x_mil, 2), "y_mil": round(pin.y_mil, 2),
               "attached": [_ref(o) for o in _distinct(net.objects)
                            if o.kind not in ("pin", "hidden_pin")][:6],
               "wired": net.wired}
        if net.no_ercs:
            row["no_erc"] = True
        rows.append(row)
    rows.sort(key=lambda r: (natural_key(r["sheet"]), natural_key(r["net"])))
    return rows


def build_erc(facts: ErcFacts, erc_section: dict[str, str],
              matrix_rows: dict[str, str]) -> dict:
    """Every violation (suppressed ones marked), the advisories and the summary."""
    levels = erc_levels(erc_section)
    matrix, own_matrix = matrix_levels(matrix_rows)
    ctx = _Context(facts, levels, matrix)
    for check in REGISTRY:
        check.run(ctx)
    ctx.found.sort(key=lambda row: row["_rank"])
    reported = [row for row in ctx.found if row["_state"] == "reported"]
    by_type: dict[str, dict] = {}
    for row in reported:
        held = by_type.setdefault(row["violation"], {"violation": row["violation"],
                                                     "group": row["group"], "count": 0})
        held["count"] += 1
    checks_run = [name for check in REGISTRY for name in check.violations]
    levels_used = {}
    for name in checks_run:
        level, source = ctx.type_level(name)
        levels_used[name] = LEVELS[level] + ("" if source == "project" else f" ({source})")
    summary = {
        "reported": len(reported),
        "by_level": {level: sum(1 for row in reported if row["level"] == level)
                     for level in reversed(LEVELS[1:])},
        "by_violation": sorted(by_type.values(), key=lambda r: (-r["count"], r["violation"])),
        "suppressed": {
            "no_report": dict(sorted(Counter(row["violation"] for row in ctx.found
                                             if row["_state"] == "no_report").items())),
            "no_erc": dict(sorted(Counter(row["violation"] for row in ctx.found
                                          if row["_state"] == "no_erc").items())),
        },
    }
    return {
        "approximation": APPROXIMATION_NOTE,
        "summary": summary,
        "levels": {"source": "project" if levels else "Altium defaults (the project sets none)",
                   "connection_matrix": "project" if own_matrix else "Altium default",
                   "by_check": levels_used},
        "checks_run": checks_run,
        "not_checked": list(NOT_CHECKED),
        "notes": ctx.notes,
        "violations": ctx.found,
        "advisories": _advisories(facts),
        "single_pin_nets": single_pin_nets(facts),
    }


def check_names() -> dict[str, list[str]]:
    """What checks= accepts: violation types, groups and advisories."""
    names = [name for check in REGISTRY for name in check.violations]
    return {"violations": names,
            "groups": sorted({GROUPS.get(name, "Others") for name in names}),
            "advisories": list(ADVISORIES)}


def page_erc(model: dict, *, checks: list[str] | None = None, sheet: str | None = None,
             min_level: str = "warning", report: str = "violations", offset: int = 0,
             limit: int = 100, advisory_limit: int = ADVISORY_LIMIT) -> dict:
    """One page of the model, filtered, or {"error": ...} for a bad argument."""
    if min_level not in MIN_LEVELS:
        return {"error": "invalid_min_level",
                "message": f"min_level must be one of {list(MIN_LEVELS)}."}
    if report not in ("violations", "single_pin_nets"):
        return {"error": "invalid_report",
                "message": "report must be 'violations' or 'single_pin_nets'."}
    accepted = check_names()
    wanted = {name.strip().lower() for name in checks or [] if name.strip()}
    known = {name.lower() for names in accepted.values() for name in names}
    unknown = sorted(wanted - known)
    if unknown:
        return {"error": "unknown_checks", "message": f"Unknown checks: {unknown}.",
                "accepted": accepted}
    offset = max(0, int(offset or 0))
    wanted_sheet = (sheet or "").strip().lower()
    result: dict = {"approximation": model["approximation"]}
    if report == "single_pin_nets":
        rows = [row for row in model["single_pin_nets"]
                if not wanted_sheet or row["sheet"].lower() == wanted_sheet]
        result["level"] = model["levels"]["by_check"].get("Nets with only one pin")
        result["note"] = ("Every net with one pin and something else on it (a wire, label, "
                          "port), whatever the level of 'Nets with only one pin'.")
        return _page(result, rows, "nets", offset, limit)
    floor = MIN_LEVELS[min_level]
    rows = []
    # Off-grid and off-sheet violations, one row per sheet, level and state unless their
    # own name is asked for: hundreds on a sheet drawn off its snap grid.
    drawing: dict[tuple, dict] = {}
    for row in model["violations"]:
        if row["_state"] != "reported" and min_level != "all":
            continue
        if LEVELS.index(row["level"]) < floor:
            continue
        if wanted and not {row["violation"].lower(), row["group"].lower()} & wanted:
            continue
        if wanted_sheet and wanted_sheet not in {s.lower() for s in row["_sheets"]}:
            continue
        shown = {key: value for key, value in row.items() if not key.startswith("_")}
        if row["_state"] != "reported":
            shown["suppressed"] = "No Report" if row["_state"] == "no_report" else "No ERC"
        if row["violation"] in DRAWING and row["violation"].lower() not in wanted:
            key = (row["violation"], row["sheet"], row["level"], shown.get("suppressed"))
            held = drawing.get(key)
            if held is None:
                held = drawing[key] = {
                    "violation": row["violation"], "level": row["level"],
                    "group": row["group"], "sheet": row["sheet"], "count": 0,
                    "examples": [], "fix": row["fix"]}
                if "suppressed" in shown:
                    held["suppressed"] = shown["suppressed"]
                rows.append(held)
            held["count"] += 1
            if len(held["examples"]) < DRAWING_EXAMPLES:
                held["examples"].append(row["message"])
            continue
        rows.append(shown)
    for held in drawing.values():
        held["message"] = (f"{held['count']} x {held['violation']} on sheet {held['sheet']} "
                           f"(e.g. {held['examples'][0]})")
    if drawing:
        result["drawing_note"] = (
            "Off-grid object and Object not completely within sheet boundaries are listed "
            "one row per sheet with a count and examples; checks=['Off-grid object'] (or "
            "the other name) lists each.")
    if offset == 0:
        for key in ("summary", "levels", "checks_run", "not_checked"):
            result[key] = model[key]
        if model.get("notes"):
            result["notes"] = model["notes"]
        advisories = [a for a in model["advisories"]
                      if not wanted or a["advisory"] in wanted]
        result["advisories"] = [_cap(a, advisory_limit) for a in advisories[:advisory_limit]]
        if len(advisories) > advisory_limit:
            result["advisories_total"] = len(advisories)
    return _page(result, rows, "violations", offset, limit)


def _cap(advisory: dict, limit: int) -> dict:
    for key in ("nets", "ends"):
        if len(advisory.get(key) or []) > limit:
            return {**advisory, key: advisory[key][:limit], f"{key}_total": len(advisory[key])}
    return advisory


def _page(result: dict, rows: list, key: str, offset: int, limit: int) -> dict:
    result["total"] = len(rows)
    result["offset"] = offset
    result[key] = rows[offset:offset + limit]
    if offset + limit < len(rows):
        result["next_offset"] = offset + limit
    return result
