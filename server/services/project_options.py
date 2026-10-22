"""The project settings that change how Altium reads a design, offline from the .PrjPcb
(Project Options): the Net Identifier Scope and net naming, diff-pair suffixes, class
generation, multi-channel naming, the ERC report levels (Error Reporting) and the
Connection Matrix, the Comparator's matching criteria and the output paths. The audit
lists ERC checks switched off or downgraded from Altium's default, risky connection-matrix
cells relaxed from theirs, and every No ERC directive on the sheets (RECORD=22) with the
net it sits on, flagged when an active one is on a power net or a net with several pins,
where it may hide real problems.

Keys are mapped only where Altium's own names verify them; anything else is reported raw
under unmapped. [Electrical Rules Check] TypeN is the violation type of TErrorKind ordinal
N-1 (Altium's WSM API order, the order of the violation names in its Workspace Manager,
whose 120th and 121st are the two Altium writes by name: MultiChannelAlternate and
AlternateItemFail; Altium Develop's adds the 166th and 167th); its value is a
TErrorLevel (0 No Report .. 3 Fatal Error). The
Connection Matrix rows L1..L17 hold one letter per column in the dialog's order (N No
Report, W Warning, E Error). Defaults are the level Altium writes for a new project where
every surveyed source agrees (its AD25 example projects, the projects shipped with its
extensions and a corpus of real projects); where they disagree the default is unknown and
the check is not audited."""
import os
from collections import Counter
from pathlib import Path

try:
    from parsers.prj_pcb import parse_prj_options
    from services.decoupling import net_kind
    from services.design_hierarchy import _NetIndex, build_design_hierarchy
except ImportError:
    from server.parsers.prj_pcb import parse_prj_options
    from server.services.decoupling import net_kind
    from server.services.design_hierarchy import _NetIndex, build_design_hierarchy

HIGH_FANOUT_PINS = 25
FINDING_LIMIT = 80
LEVELS = ("No Report", "Warning", "Error", "Fatal Error")  # TErrorLevel

# Altium's violation types in TErrorKind order: [Electrical Rules Check] TypeN is
# ERC_CHECKS[N - 1]. The first 75 are the WSM API's TErrorKind list; the first 165 are the
# violation names in AD25's Workspace Manager in that order, and Altium Develop's
# Workspace Manager appends the last two (written as Type166 and Type167).
ERC_CHECKS = (
    "Off-grid object",
    "Object not completely within sheet boundaries",
    "Missing child sheet for sheet symbol",
    "Missing sub-Project sheet for component",
    "Port not linked to parent sheet symbol",
    "Sheet Entry not linked to child sheet",
    "Duplicate sheet numbers",
    "Unconnected wires",
    "Unconnected objects in net",
    "Nets with no driving source",  # Type10
    "Nets containing floating input pins",
    "Nets with possible connection problems",
    "Nets containing multiple similar objects",
    "Nets with multiple names",
    "Adding Items from hidden net to net",
    "Adding hidden net to sheet",
    "Global Power-Object scope changes",
    "Net Parameters with no name",
    "Net Parameters with no value",
    "Mismatched Bus-Section index ordering",  # Type20
    "Mismatched Generics on bus (First Index)",
    "Mismatched Generics on bus (Second Index)",
    "Mismatched electrical types on bus",
    "Bus indices out of range",
    "Bus range syntax errors",
    "Illegal bus definitions",
    "Illegal bus range values",
    "Mismatched bus widths",
    "Mismatched bus label ordering",
    "Mixed generic and numeric bus labeling",  # Type30
    "Un-Designated parts requiring annotation",
    "Duplicate Part Designators",
    "Duplicate Sheet Symbol Names",
    "Duplicate Nets",
    "Components with duplicate pins",
    "Sheet Symbol with duplicate entries",
    "Sheets containing duplicate ports",
    "Components containing duplicate sub-parts",
    "Mismatched hidden pin connections",
    "Mismatched pin visibility",  # Type40
    "Same parameter containing different values",
    "Same parameter containing different types",
    "Missing Component Models",
    "Models Found in Different Model Locations",
    "Missing Component Models in Model Files",
    "Duplicate Component Models",
    "Missing Component Model Parameters",
    "Errors in Component Model Parameters",
    "Component Implementations with duplicate pins usage",
    "Component Implementations with invalid pin mappings",  # Type50
    "Component Implementations with missing pins in sequence",
    "Components with duplicate Implementations",
    "Unused sub-part in component",
    "Extra pin found in component display mode",
    "Missing pin found in component display mode",
    "Mismatched Bus/Wire object on Wire/Bus",
    "Floating net labels",
    "Floating power objects",
    "Nets with only one pin",
    "Signals with no load",  # Type60
    "Signals with no driver",
    "Signals with multiple drivers",
    "Auto-Assigned Ports To Device Pins",
    "NoError",
    "Multiple Top-Level Documents",
    "Multiple Configuration Targets",
    "Conflicting Constraints",
    "Missing Configuration Target",
    "Unique Identifiers Errors",
    "Missing Positive Net in Differential Pair",  # Type70
    "Missing Negative Net in Differential Pair",
    "Same Net used in Multiple Differential Pairs",
    "Differential Pair Unproperly Connected to Device",
    "Differential Pair Net Connection Polarity Inversed",
    "Differential Pair Net Unconnected To Differential Pair Pin",
    "Constraint Port Without Pin in Configuration",
    "Constraint Board Not Found in Configuration",
    "Constraint Connector Creation Failed in Configuration",
    "Constraint Configuration Has Duplicate Board Instance",
    "Missing child HDL entity for sheet symbol",  # Type80
    "Forbidden OpenBus Link",
    "Mismatching Address/Data Widths of OpenBus Ports",
    "Conflicting Harness Definition",
    "Missing Harness Type on Harness",
    "Unknown Harness Type",
    "Bus Object on a Harness",
    "Harness Object on a Wire",
    "Harness Object on a Bus",
    "Harness Connector Type Syntax Error",
    "Multiple Harness Types on Harness",  # Type90
    "Cascaded Interconnects in OpenBus document",
    "Arbiter loop in OpenBus document",
    "Missing Exported Function in Source File",
    "No Exported Functions in Code Symbol",
    "Duplicate Code Entry Names in Code Symbol",
    "Reserved Names Used in Code Symbol",
    "Identifier Case Mismatch Between Code Symbol and Source File",
    "HDL Identifier Renamed",
    "Ambiguous Device Sheet Path Resolution",
    "Sheet Names Clash",  # Type100
    "Missing Component Model editor",
    "Sheets containing duplicate FSM states",
    "No input transitions to FSM state",
    "No output transitions from FSM state",
    "Multiple transitions between FSM states",
    "FSM transition with empty condition",
    "No default FSM state specified",
    "Unreachable FSM states",
    "Unused FSM port",
    "Component revision has inapplicable state",  # Type110
    "Incorrect link in project variant",
    "Component revision is Out of Date",
    "Component revision is obsolete",
    "Different Net Names",
    "Entry Is Empty",
    "No Mated Part",
    "No Net",
    "Unresolved Conflict",
    "Circular Document Dependency",
    "Unsupported multi-channel alternate item",  # Type120, written MultiChannelAlternate
    "Fail to add alternate item",  # Type121, written AlternateItemFail
    "External and Schematic Net Names are Unsynchronized",
    "Component has been deleted",
    "Generic Component",
    "Floating Directive Object",
    "Sheet Symbols with duplicated indexes",
    "Duplicated project files",
    "Unwired connection",
    "Unterminated wire",
    "Electrical mismatch - wire terminated at a wrong pin",  # Type130
    "Electrical mismatch - shorted nets",
    "Empty Cable object",
    "Empty Shield object",
    "Empty Twist object",
    "Unconnected Splice object",
    "Duplicate Designator (WD)",
    "Unwired shield connection",
    "Unspecified connector cavities - no part choice for active pins",
    "Cable with only 1 element",
    "Shield with only 1 element",  # Type140
    "Twist with only 1 element",
    "Splice with only 1 wire",
    "Wire-net with multiple wire colors",
    "Unnecessary Splice",
    "Unnecessary Tap",
    "Empty Tap object",
    "No tapped Wire in Tap object",
    "Orphaned wire - not routed via any bundle",
    "Orphaned connection point - no wires routed nor objects assigned",
    "Empty bundle object",  # Type150
    "Duplicate Designator (LD)",
    "Unconnected Harness Entry",
    "Invalid Connection to a Harness Connector",
    "Port with no matching ports",
    "Functional Block has no implementation",
    "Functional Block key components have no implementation",
    "Functional Block key components are not properly implemented",
    "Interface is not properly implemented",
    "I2C Interface has no implementation",
    "I2C Interface implementation has wrong connectivity",  # Type160
    "I2C Interface implementation contains no pull-up resistors",
    "Reuse Block revision has inapplicable state",
    "Reuse Block revision is Out of Date",
    "Direct connection to Reuse Block's object",
    "Mismatched parameters in connected wire segments",  # Type165
    "Unused pins in dynamic symbol",
    "Generic Component Not Replaced With Library Component",  # Type167
)
NAMED_TYPES = {"MultiChannelAlternate": 120, "AlternateItemFail": 121}
NO_ERROR_TYPE = 64  # eError_NoError: not a check, never shown in the dialog

# Default level per TypeN where Altium's AD25 examples, its extension projects and the
# surveyed corpus agree (types left out: the sources disagree or only the corpus has them).
ERC_DEFAULTS = {
    1: 1, 2: 1, 3: 2, 4: 1, 5: 2, 6: 2, 8: 1, 9: 1, 10: 1, 11: 2,
    12: 2, 13: 2, 14: 1, 15: 1, 16: 1, 17: 1, 18: 1, 19: 1, 24: 1, 25: 2,
    27: 2, 28: 1, 29: 1, 30: 1, 31: 1, 32: 2, 34: 2, 35: 1, 36: 2, 37: 1,
    38: 2, 39: 2, 40: 2, 41: 0, 42: 2, 43: 1, 50: 2, 53: 1, 54: 1, 55: 1,
    56: 2, 57: 1, 58: 1, 64: 0, 65: 2, 66: 3, 67: 2, 68: 2, 70: 2, 71: 2,
    72: 2, 73: 2, 74: 1, 75: 2, 76: 1, 77: 1, 78: 1, 79: 1, 80: 2, 81: 3,
    82: 3, 83: 3, 84: 3, 85: 3, 86: 2, 87: 2, 88: 2, 89: 1, 90: 1, 91: 3,
    92: 3, 93: 2, 94: 2, 95: 2, 96: 2, 97: 2, 98: 0, 99: 1, 100: 2, 102: 2,
    103: 2, 104: 1, 105: 2, 106: 2, 107: 2, 108: 2, 109: 1, 111: 1, 112: 1, 113: 1,
    114: 2, 115: 2, 116: 2, 117: 3, 118: 3, 119: 3, 121: 3, 122: 2, 123: 1, 124: 1,
    125: 1, 126: 1, 127: 1,
}
# Checks whose loss hides connectivity mistakes, with what goes unreported.
KEY_CHECKS = {
    10: "inputs that nothing drives (a missing pull-up, a driver on the wrong net)",
    9: "pins and objects left unconnected on a net",
    11: "input pins floating on a net with no other connection",
    12: "pin-type conflicts the Connection Matrix marks (two outputs on one net)",
    13: "several objects of one kind on a net that should have one",
    8: "wires ending in nothing",
    14: "nets that carry two names",
    32: "two parts with one designator",
    34: "two nets with one name",
    57: "net labels attached to nothing",
    58: "power ports attached to nothing",
    5: "ports without a matching sheet entry",
    6: "sheet entries without a matching port",
    59: "nets with only one pin",
}

# The Connection Matrix in the dialog's row and column order.
MATRIX_LABELS = (
    "Input Pin", "IO Pin", "Output Pin", "Open Collector Pin", "Passive Pin", "HiZ Pin",
    "Open Emitter Pin", "Power Pin", "Input Port", "Output Port", "Bidirectional Port",
    "Unspecified Port", "Input Sheet Entry", "Output Sheet Entry",
    "Bidirectional Sheet Entry", "Unspecified Sheet Entry", "Unconnected",
)
MATRIX_LETTERS = {"N": 0, "W": 1, "E": 2}
MATRIX_DEFAULT = (
    "NNNNNNNNNNNWNNNWW", "NNWNNNNWWWNWNWNWN", "NWEENEEEENEWNEEWN", "NNENNNWEENNWNENWN",
    "NNNNNNNNNNNNNNNNN", "NNENNNNEENNWNENWN", "NNEWNNWEENNWNENWN", "NWEENEENEEENNEENN",
    "NWEENEEEENEWNEEWW", "NWNNNNNENNEWNNEWN", "NNENNNNEEENWNENWN", "WWWWNWWNWWWNWWWNN",
    "NNNNNNNNNNNWNNNWW", "NWEENEEEENEWNEEWW", "NNENNNNEEENWNENWW", "WWWWNWWNWWWNWWWNW",
    "WNNNNNNNWNNNWWWWN",
)
# Cells whose default differs between Altium's sources (Output, Bidirectional and
# Unspecified Sheet Entry against Unconnected): not audited.
MATRIX_DEFAULT_UNKNOWN = {(13, 16), (14, 16), (15, 16)}
INPUT_PIN, UNCONNECTED = 0, 16

# Project Options > Multi-Channel (WSM TChannelRoomNamingStyle order).
ROOM_NAMING_STYLES = (
    "Flat Numeric With Names", "Flat Alpha With Names", "Numeric Name Path",
    "Alpha Name Path", "Mixed Name Path",
)
DESIGNATOR_FORMATS = (
    "$Component_$RoomName", "$RoomName_$Component", "$Component$ChannelAlpha",
    "$Component_$ChannelPrefix$ChannelAlpha", "$Component_$ChannelIndex",
    "$Component_$ChannelPrefix$ChannelIndex", "$ComponentPrefix_$ChannelIndex_$ComponentIndex",
    "$ComponentPrefix_$RoomName_$ComponentIndex",
)
# Project Options > Class Generation, project-wide ([PrjClassGen]).
CLASS_GENERATION = {
    "NetClassAutoBusEnabled": "generate_net_classes_for_buses",
    "NetClassSeparateForBusSections": "generate_separate_net_classes_for_bus_sections",
    "NetClassAutoCompEnabled": "generate_net_classes_for_components",
    "NetClassAutoNamedHarnessEnabled": "generate_net_classes_for_named_signal_harnesses",
    "CompClassManualEnabled": "user_defined_component_classes",
    "CompClassManualRoomEnabled": "user_defined_component_class_rooms",
    "NetClassManualEnabled": "user_defined_net_classes",
}
# Per sheet ([DocumentN]): Component Classes, Generate Rooms, Net Classes scope, Structure.
SHEET_CLASS_GENERATION = {
    "ClassGenCCAutoEnabled": "component_class",
    "ClassGenCCAutoRoomEnabled": "room",
    "ClassGenNCAutoScope": "net_classes",
    "GenerateClassCluster": "structure_class",
}
# Project Options > Options, output part.
OUTPUT_FLAGS = {
    "OpenOutputs": "open_outputs_after_compile",
    "TimestampOutput": "timestamp_folder",
    "ArchiveProject": "archive_project_document",
    "SeparateFolders": "separate_folder_per_output_type",
}
COMPARISON_FIELDS = {
    "MinPercent": "min_match_percent", "MinMatch": "min_matched_members",
    "UseName": "use_name_matching", "ShowMatch": "show_manual_matching_dialog",
}


def build_project_options(project: dict, read_sheet, netlist: dict | None = None) -> dict:
    """The options model of the loaded project, or {"error": ...}.

    read_sheet(path) returns a sheet's SheetHierarchy (parse_sch_hierarchy, cached)."""
    prj_path = project["prj_pcb_path"]
    try:
        options = parse_prj_options(prj_path)
    except (OSError, UnicodeDecodeError) as error:
        return {"error": "project_file_unreadable",
                "message": f"{Path(prj_path).name}: {type(error).__name__}: {error}"}
    unmapped: dict = {}
    hierarchy = build_design_hierarchy(project, read_sheet)
    scope = {key: value for key, value in (hierarchy.get("scope") or {}).items()
             if key not in ("explanation", "automatic_rule")}
    naming = dict(scope.pop("net_naming", {}))
    naming.pop("explanation", None)
    erc, erc_audit = _erc(options.erc_levels, unmapped)
    matrix, matrix_audit = _matrix(options.connection_matrix, unmapped)
    directives, sheet_errors = _no_ercs(project, read_sheet, netlist, scope.get("effective", ""))
    result: dict = {
        "project": project.get("name"),
        "prj_pcb": Path(prj_path).name,
        "net_identifier_scope": scope,
        "net_naming": naming | _netlist_flags(options.design),
        "diff_pairs": _diff_pairs(options, unmapped),
        "class_generation": _class_generation(project, options),
        "multi_channel": _multi_channel(options, unmapped),
        "erc": erc,
        "connection_matrix": matrix,
        "comparator": _comparator(options, unmapped),
        "outputs": _outputs(project, options.design),
        "audit": {
            "erc_checks_off_or_downgraded": erc_audit,
            "connection_matrix_relaxed": matrix_audit,
            "no_erc": _no_erc_summary(directives),
        },
        "no_erc_directives": directives,
    }
    if sheet_errors:
        result["sheets_not_read"] = sheet_errors
    if unmapped:
        result["unmapped"] = unmapped
    result["notes"] = [
        "Read from the saved .PrjPcb and sheets, not Altium's compiler: Compile in Altium "
        "(Messages panel) is the final word, and Project Options > Set To Installation "
        "Defaults shows Altium's defaults.",
        "default is the level Altium writes for a new project where its example projects, "
        "the projects shipped with it and a corpus of real projects agree; default null: "
        "the sources disagree, so the check is not audited.",
    ]
    if netlist is None:
        result["nets_note"] = (
            "No compiled netlist is loaded: a No ERC's net is named from the power ports, "
            "net labels and ports on its wiring, and pins_on_sheet counts that sheet only. "
            "Load the project with a netlist (Altium Designer running once) for net names "
            "and pin counts.")
    return result


def page_project_options(model: dict, offset: int, limit: int) -> dict:
    """One page of No ERC directives; the settings and audit go with the first page."""
    offset = max(0, int(offset or 0))
    directives = model["no_erc_directives"]
    if offset:
        keep = ("project", "prj_pcb", "nets_note")
        result = {key: model[key] for key in keep if key in model}
    else:
        result = {key: value for key, value in model.items() if key != "no_erc_directives"}
    result["no_erc_total"] = len(directives)
    result["offset"] = offset
    result["has_more"] = offset + limit < len(directives)
    if result["has_more"]:
        result["next_offset"] = offset + limit
    result["no_erc_directives"] = directives[offset:offset + limit]
    return result


# ---------- naming, diff pairs, classes, channels, comparator, outputs ----------

def _bool(value: str | None):
    return {"1": True, "0": False, "-1": True, "T": True, "F": False}.get((value or "").strip())


def _netlist_flags(design: dict[str, str]) -> dict:
    single = _bool(design.get("NetlistSinglePinNets"))
    return {} if single is None else {"allow_single_pin_nets": single}


def _diff_pairs(options, unmapped: dict) -> dict:
    result: dict = {
        "default_suffixes": {"positive": "_P", "negative": "_N"},
        "note": ("Project Options > Options > Diff Pairs: _P/_N is Altium's default and "
                 "always applies; custom suffix pairs add to it."),
    }
    if options.diff_pair_suffixes:
        # [DiffPairSuffixN] is the section Altium writes custom pairs to; no surveyed
        # project has one, so its keys are passed on as written.
        unmapped["diff_pair_suffix_sections"] = options.diff_pair_suffixes
        result["custom_suffixes"] = "present (see unmapped.diff_pair_suffix_sections)"
    return result


def _class_generation(project: dict, options) -> dict:
    result: dict = {}
    for key, name in CLASS_GENERATION.items():
        value = _bool(options.class_generation.get(key))
        if value is not None:
            result[name] = value
    sheets = {os.path.normcase(s["path"]): s["name"] for s in project.get("sheets") or []}
    root = Path(project["prj_pcb_path"]).parent
    rows = []
    for raw, fields in options.sheet_class_generation:
        path = os.path.normcase(str((root / raw.replace("\\", "/")).resolve()))
        row = {"sheet": sheets.get(path, Path(raw.replace("\\", "/")).stem)}
        for key, name in SHEET_CLASS_GENERATION.items():
            if key in fields:
                value = fields[key]
                row[name] = value if key == "ClassGenNCAutoScope" else _bool(value)
        rows.append(row)
    if rows:
        combos = Counter(tuple(sorted((k, v) for k, v in r.items() if k != "sheet"))
                         for r in rows)
        common, count = combos.most_common(1)[0]
        result["sheets"] = {"total": len(rows), "most_sheets": dict(common) | {"count": count}}
        others = [r for r in rows
                  if tuple(sorted((k, v) for k, v in r.items() if k != "sheet")) != common]
        if others:
            result["sheets"]["other_sheets"] = others[:FINDING_LIMIT]
    result["note"] = ("Per sheet: component_class and room (a component class and room per "
                      "sheet), net_classes (None, local nets or all nets of the sheet), "
                      "structure_class. Classes reach the PCB on Update PCB.")
    return result


def _multi_channel(options, unmapped: dict) -> dict:
    design = options.design
    result: dict = {}
    style = design.get("ChannelRoomNamingStyle", "")
    if style.isdigit() and int(style) < len(ROOM_NAMING_STYLES):
        result["room_naming_style"] = ROOM_NAMING_STYLES[int(style)]
    elif style:
        unmapped["ChannelRoomNamingStyle"] = style
    if "ChannelRoomLevelSeperator" in design:
        result["level_separator"] = design["ChannelRoomLevelSeperator"]
    fmt = design.get("ChannelDesignatorFormatString")
    if fmt is not None:
        result["designator_format"] = fmt
        result["designator_format_predefined"] = fmt in DESIGNATOR_FORMATS
    if "PhysicalNamingFormat" in options.annotate:
        unmapped["Annotate.PhysicalNamingFormat"] = options.annotate["PhysicalNamingFormat"]
    return result


def _comparator(options, unmapped: dict) -> dict:
    rows = []
    for fields in options.comparison:
        row: dict = {"object_type": fields.get("Kind", "")}
        for key, value in fields.items():
            if key in ("MinPercent", "MinMatch") and value.lstrip("-").isdigit():
                row[COMPARISON_FIELDS[key]] = int(value)
            elif key in ("UseName", "ShowMatch") and _bool(value) is not None:
                row[COMPARISON_FIELDS[key]] = _bool(value)
            elif key != "Kind":
                unmapped.setdefault("comparison_options", {}).setdefault(
                    row["object_type"], {})[key] = value
        rows.append(row)
    result: dict = {"object_matching": rows} if rows else {}
    levels = options.difference_levels
    if levels:
        # Difference Levels TypeN: Altium's comparison types, in an order no source
        # verifies; reported raw.
        off = {key: value for key, value in levels.items() if value != "1"}
        result["difference_types"] = {"total": len(levels), "not_1": len(off)}
        if off:
            unmapped["difference_levels_not_1"] = off
    return result


def _outputs(project: dict, design: dict[str, str]) -> dict:
    name = Path(project["prj_pcb_path"]).stem
    output, log = design.get("OutputPath", ""), design.get("LogFolderPath", "")
    result: dict = {
        "output_path": output or f"Project Outputs for {name}",
        "eco_log_path": log or f"Project Logs for {name}",
    }
    if not output:
        result["output_path_default"] = True
    if not log:
        result["eco_log_path_default"] = True
    for key, label in OUTPUT_FLAGS.items():
        value = _bool(design.get(key))
        if value is not None:
            result[label] = value
    return result


# ---------- ERC report levels and the connection matrix ----------

def _type_number(key: str) -> int | None:
    if key in NAMED_TYPES:
        return NAMED_TYPES[key]
    if key[:4].lower() == "type" and key[4:].isdigit() and int(key[4:]) >= 1:
        return int(key[4:])
    return None  # Type0 is no slot: TypeN is ordinal N-1


def _level(value: str) -> int | None:
    return int(value) if value.isdigit() and int(value) < len(LEVELS) else None


def _erc(levels: dict[str, str], unmapped: dict) -> tuple[dict, list[dict]]:
    counts: Counter = Counter()
    changed, audit, key_checks, raw = [], [], [], {}
    unknown: dict[str, list[str]] = {}  # level -> checks whose default is unknown
    at_default = 0
    found: dict[int, int] = {}
    for key, value in levels.items():
        number, level = _type_number(key), _level(value)
        if number is None or number > len(ERC_CHECKS) or level is None:
            raw[key] = value
            continue
        found[number] = level
    order = list(KEY_CHECKS)

    def rank(number: int) -> tuple[int, int]:
        return (0, order.index(number)) if number in KEY_CHECKS else (1, number)

    for number in sorted(found, key=rank):
        if number == NO_ERROR_TYPE:
            continue
        level, default = found[number], ERC_DEFAULTS.get(number)
        counts[LEVELS[level]] += 1
        row = {"check": ERC_CHECKS[number - 1], "level": LEVELS[level],
               "default": LEVELS[default] if default is not None else None}
        if number in KEY_CHECKS:
            key_checks.append(row)
        if default is None:
            if number not in KEY_CHECKS:
                unknown.setdefault(LEVELS[level], []).append(row["check"])
        elif level > default:
            changed.append(row)
        elif level < default:
            audit.append(row | ({"hides": KEY_CHECKS[number]} if number in KEY_CHECKS else {}))
        else:
            at_default += 1
    if raw:
        unmapped["electrical_rules_check"] = raw
    result: dict = {
        "checks": len(found) - (NO_ERROR_TYPE in found),
        "level_counts": dict(counts),
        "key_checks": key_checks,
        "at_default": at_default,
        "lowered_from_default": len(audit),
        "raised_above_default": changed[:FINDING_LIMIT],
    }
    if unknown:
        # Every check is then accounted for: at its default (counted), lowered (audit),
        # raised, or here by level.
        result["default_unknown_by_level"] = {level: unknown[level]
                                              for level in LEVELS if level in unknown}
    if len(changed) > FINDING_LIMIT:
        result["raised_above_default_total"] = len(changed)
    if not levels:
        result["note"] = "No [Electrical Rules Check] section: Altium uses its defaults."
    return result, audit[:FINDING_LIMIT]


def _matrix(rows: dict[str, str], unmapped: dict) -> tuple[dict, list[dict]]:
    size = len(MATRIX_LABELS)
    lines = [rows.get(f"L{i}", "") for i in range(1, size + 1)]
    if not any(lines):
        return {"note": "No [ERC Connection Matrix] section: Altium uses its defaults."}, []
    if any(len(line) != size for line in lines):
        unmapped["erc_connection_matrix"] = rows
        return {"note": "The matrix is not 17 x 17 as written; see unmapped."}, []
    odd = sorted({c for line in lines for c in line if c not in MATRIX_LETTERS})
    if odd:
        unmapped["connection_matrix_letters"] = odd
    changed, audit = [], []
    symmetric = all(lines[i][j] == lines[j][i] for i in range(size) for j in range(size))
    for i in range(size):
        for j in range(i if symmetric else 0, size):
            letter, default = lines[i][j], MATRIX_DEFAULT[i][j]
            if letter == default:
                continue
            level = MATRIX_LETTERS.get(letter)
            unknown = (min(i, j), max(i, j)) in MATRIX_DEFAULT_UNKNOWN
            name = LEVELS[level] if level is not None else f"{letter} (unmapped)"
            default_name = "unknown" if unknown else LEVELS[MATRIX_LETTERS[default]]
            pair = f"{MATRIX_LABELS[i]} x {MATRIX_LABELS[j]}"
            changed.append(f"{pair}: {name} (default {default_name})")
            if unknown or level is None or level >= MATRIX_LETTERS[default]:
                continue
            cell = {"pair": pair, "level": name, "default": default_name}
            if MATRIX_LETTERS[default] >= 2:
                audit.append(cell | {"risk": "driver conflict (e.g. two outputs on one net) "
                                             "not reported"})
            elif {i, j} == {INPUT_PIN, UNCONNECTED}:
                audit.append(cell | {"risk": "unconnected input pin not reported"})
    result = {
        "order": list(MATRIX_LABELS),
        "rows": {MATRIX_LABELS[i]: lines[i] for i in range(size)},
        "legend": "one letter per column in order: N No Report, W Warning, E Error",
        "symmetric": symmetric,
        "changed_from_default": changed,
    }
    return result, audit


# ---------- No ERC directives ----------

def _pin_counts(netlist: dict) -> Counter:
    counts: Counter = Counter()
    for comp in (netlist.get("components") or {}).values():
        for data in (comp.get("pins") or {}).values():
            net = data.get("net") if isinstance(data, dict) else data
            if net:
                counts[net] += 1
    return counts


def _no_ercs(project: dict, read_sheet, netlist: dict | None,
             scope: str) -> tuple[list[dict], list]:
    nets = _NetIndex(netlist) if netlist else None
    pins = _pin_counts(netlist) if netlist else Counter()
    directives: list[tuple[tuple, dict]] = []
    errors = []
    read: list[tuple[int, dict, object]] = []
    names: Counter = Counter()  # ("label" | "port", name) -> sheets having it
    entry_ports: dict[str, set[str]] = {}  # child file name -> ports its symbols' entries join
    for order, sheet in enumerate(project.get("sheets") or []):
        try:
            data = read_sheet(sheet["path"])
        except Exception as error:
            errors.append({"sheet": sheet["name"], "error": f"{type(error).__name__}: {error}"})
            continue
        read.append((order, sheet, data))
        names.update({("label", n.lower()) for n in data.net_labels}
                     | {("port", p.name.lower()) for p in data.ports})
        for symbol in data.symbols:
            child = Path(symbol.file_name.replace("\\", "/")).name.lower()
            entry_ports.setdefault(child, set()).update(
                entry.port_name.lower() for entry in symbol.entries)
    for order, sheet, data in read:
        parent_entries = entry_ports.get(Path(sheet["path"].replace("\\", "/")).name.lower(),
                                         set())
        for item in data.no_ercs:
            reaches = _reaches(item.wiring, scope, names, parent_entries)
            row = _directive(sheet["name"], item, nets, pins, reaches)
            rank = 0 if row.get("flagged") else 1 if row["active"] else 2
            directives.append(((rank, order, -item.y_mil, item.x_mil), row))
    directives.sort(key=lambda pair: pair[0])
    return [row for _, row in directives], errors


def _reaches(wiring, scope: str, names: Counter, parent_entries: set[str]) -> bool:
    """Whether a sheet's wiring joins other sheets under the scope: through a sheet entry;
    under the hierarchical scopes through a port only when a symbol placing the sheet has
    its entry (parent_entries: those entries' port names), as ports never join ports
    there; under Flat and Global through a port another sheet also has, under Global a
    net label another sheet also has; a power port except under Strict Hierarchical."""
    if wiring.entries or (wiring.power_ports and scope != "Strict Hierarchical"):
        return True
    if scope in ("Hierarchical", "Strict Hierarchical"):
        return any(p.lower() in parent_entries for p in wiring.ports)
    if any(names[("port", p.lower())] > 1 for p in wiring.ports):
        return True
    return scope == "Global" and any(names[("label", n.lower())] > 1 for n in wiring.labels)


def _directive(sheet: str, item, nets: _NetIndex | None, pin_counts: Counter,
               reaches: bool) -> dict:
    wiring = item.wiring
    row: dict = {"sheet": sheet, "x_mil": round(item.x_mil, 2), "y_mil": round(item.y_mil, 2),
                 "active": item.active,
                 "mode": "generic" if item.suppress_all else "specific"}
    if not item.suppress_all:
        row["suppresses"] = {"violation_types": item.error_kinds or None,
                             "connection_pairs": item.connection_pairs or None}
    if item.symbol:
        row["symbol"] = item.symbol
    names = list(dict.fromkeys([*wiring.power_ports, *wiring.labels, *wiring.ports]))
    found: set[str] = set()
    if nets is not None:
        for refdes, pin in wiring.pins:
            found.update(net for net, _ in nets.copies(sheet, refdes, pin))
        if not found:
            found = {name for name in names if name in pin_counts}
    on = [f"{refdes}.{pin}" for refdes, pin in wiring.pins]
    if found:
        row["net" if len(found) == 1 else "nets"] = (
            next(iter(found)) if len(found) == 1 else sorted(found))
        count = max(pin_counts.get(net, 0) for net in found)
        row["pins"] = count
    elif names:
        row["net"] = names[0]
        count = len(on)
        row["pins_on_sheet"] = count
    else:
        row["net"] = None
        count = len(on)
        if on:
            row["pins_on_sheet"] = count
    if on:
        row["on_pins"] = on[:6] + ([f"... {len(on) - 6} more"] if len(on) > 6 else [])
    if wiring.ports:
        row["on_ports"] = wiring.ports[:6]
    if wiring.entries:
        row["on_sheet_entries"] = [f"{symbol}/{entry}" for symbol, entry in wiring.entries[:6]]
    # A rail by name: a supply or ground name with no signal token (VCC_EN, VBUS_SENSE
    # and 3V3_PGOOD are signals), as the decoupling check reads net names.
    kinds = [net_kind(name) for name in (found or names)]
    if not (wiring.pins or wiring.wires or wiring.labels or wiring.power_ports or reaches):
        kind = "nothing"
    elif wiring.power_ports or any((k["power"] or k["ground"]) and not k["signal"]
                                   for k in kinds):
        kind = "power"
    elif count > HIGH_FANOUT_PINS:
        kind = "high_fanout"
    elif count >= 2 or (reaches and nets is None):
        kind = "signal"
    elif count == 1:
        kind = "single_pin"
    else:
        kind = "wire_only"
    row["net_kind"] = kind
    if not item.flags_written:
        row["legacy_record"] = True
    reason = _flag_reason(item, kind, count, nets is None)
    if reason:
        row["flagged"] = reason
    return row


def _flag_reason(item, kind: str, count: int, offline: bool) -> str | None:
    if not item.active:
        return None
    what = ("every violation" if item.suppress_all
            else "the selected violation types and connection pairs")
    if kind in ("power", "high_fanout"):
        return (f"active on a {'power' if kind == 'power' else 'high-fanout'} net: it "
                f"suppresses {what} reported at its point, which can hide a short, a "
                f"missing supply connection or a driver conflict")
    if kind == "signal" and count >= 2:
        where = " on this sheet" if offline else ""
        return (f"active on a net with {count} pins{where}: it suppresses {what} reported "
                f"at its point, on a net that connects parts (e.g. no driving source, "
                f"output conflicts)")
    if kind == "signal" and offline:
        return (f"active on a net that leaves the sheet (port, sheet entry or global net "
                f"label): it suppresses {what} reported at its point; load a netlist for "
                f"the net's pin count")
    return None


def _no_erc_summary(directives: list[dict]) -> dict:
    summary: dict = {
        "total": len(directives),
        "active": sum(1 for d in directives if d["active"]),
        "inactive": sum(1 for d in directives if not d["active"]),
        "generic": sum(1 for d in directives if d["mode"] == "generic"),
        "specific": sum(1 for d in directives if d["mode"] == "specific"),
        "flagged": sum(1 for d in directives if d.get("flagged")),
        "by_net_kind": dict(Counter(d["net_kind"] for d in directives)),
    }
    summary["note"] = (
        "A No ERC suppresses the violations Altium reports at its point (generic: all of "
        "them; specific: the selected ones), not the whole net. On an unused pin (net_kind "
        "single_pin) that is the intended use. flagged: an active one on a power net, a "
        "high-fanout net or a net with several pins, where a net-level violation reported "
        "there (no driving source, a pin-type conflict) goes unreported too; prefer a "
        "specific No ERC or fixing the cause.")
    return summary
