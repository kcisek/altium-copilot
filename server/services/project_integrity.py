"""Problems in the project folder, the .PrjPcb and the strings on the sheets that silently
break compile, sync or saving, read offline (no netlist needed).

File checks: schematic or PCB documents with legacy extensions (.Sch, .S01, .S02 ...,
.Pcb), documents listed in the project but missing on disk, a leftover .Annotation file
that makes compiled designators differ from the editor's, parts placed outside the sheet,
compile masks hiding parts from the PCB, and variant entries pointing at designators or
UniqueIds that no longer exist (e.g. after a PCB reannotation). Name checks: hidden
characters (Unicode categories Cc, Cf, Co, Cn and spaces other than the ASCII one),
non-ASCII characters and leading or trailing spaces in net labels, power ports, ports,
sheet entries, designators and component parameters; bus ranges Altium cannot read;
local parameters named 'Manufacturer N' / 'Manufacturer Part Number N', which ActiveBOM
reserves; and a multi-channel designator format that puts channel text after the names
on channel sheets that carry buses.

Each finding groups one kind of problem with its count, examples (sheet, object, location
in mil) and how to fix it in Altium, with the Altium knowledge-base article that
documents the effect where there is one (KB below). Severity: error breaks compile, sync
or save; warning gives a silently wrong result; info is worth a look."""
import os
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

try:
    from altium import enrich_netlist_sheets
    from parsers.annotation import parse_annotation
    from parsers.prj_pcb import parse_prj_documents, parse_prj_options, parse_prj_pcb
    from parsers.sch_doc import ENTRY_REPEAT_RE
    from services.bom import natural_key, plan_variant, variant_names
    from services.design_hierarchy import build_design_hierarchy
    from services.project_options import ERC_CHECKS, ERC_DEFAULTS, LEVELS
except ImportError:
    from server.altium import enrich_netlist_sheets
    from server.parsers.annotation import parse_annotation
    from server.parsers.prj_pcb import parse_prj_documents, parse_prj_options, parse_prj_pcb
    from server.parsers.sch_doc import ENTRY_REPEAT_RE
    from server.services.bom import natural_key, plan_variant, variant_names
    from server.services.design_hierarchy import build_design_hierarchy
    from server.services.project_options import ERC_CHECKS, ERC_DEFAULTS, LEVELS

FILE_CHECKS = (
    "legacy_extension", "missing_document", "annotation_file", "outside_sheet_border",
    "compile_mask", "variant_entry",
)
NAME_CHECKS = (
    "hidden_characters", "non_ascii", "whitespace", "net_name_syntax",
    "reserved_bom_parameter", "channel_designator_format",
)
CHECKS = FILE_CHECKS + NAME_CHECKS
_CHECK_RANK = {check: rank for rank, check in enumerate(CHECKS)}
_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}

_KB = "https://www.altium.com/documentation/knowledge-base/altium-designer/"
KB = {
    "legacy_extension": _KB + "update-pcb-removes-components-due-to-legacy-schematic-file-extensions",
    "annotation_file": _KB + "multiple-different-designators-in-schematic",
    "hidden_characters": (_KB + "unable-to-save-file-prjpcbcstrschdoc-error-caused-by-"
                          "invalid-characters-in-net-names"),
    "reserved_bom_parameter": (_KB + "manufacturer-parameters-overwritten-by-part-choices-in-"
                               "exported-bom"),
    "channel_designator_format": _KB + "duplicate-net-names-error-on-multi-channel-buses",
}
KB["non_ascii"] = KB["hidden_characters"]

# Protel / Advanced Schematic and PCB extensions Altium opens but does not compile as
# its own documents (the KB's .S01, .S02).
_LEGACY_SCHEMATIC = re.compile(r"^\.(?:sch|s\d\d)$", re.IGNORECASE)
_LEGACY_PCB = re.compile(r"^\.pcb$", re.IGNORECASE)
# Documents whose loss changes what is compiled; any other missing one is a warning.
_COMPILED_TYPES = {".schdoc", ".harness"}
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

NET_KINDS = ("net_label", "power_port", "port", "sheet_entry")
_KIND_LABELS = {
    "net_label": "net labels", "power_port": "power ports", "port": "ports",
    "sheet_entry": "sheet entries", "designator": "designators",
    "parameter": "parameter names", "parameter_value": "parameter values",
}
_BUS_RANGE = re.compile(r"[^\[\]]+\[\s*\d+\s*(?:\.\.\s*\d+\s*)?\]")
_RESERVED = re.compile(r"^manufacturer(?: part number)? \d+$", re.IGNORECASE)
_RESERVED_MPN = re.compile(r"^manufacturer part number(?: \d+)?$", re.IGNORECASE)
_COMPONENT_TOKENS = ("$component", "$componentprefix", "$componentindex")
_BUS_BREAKING = set("[],")
DEFAULT_DESIGNATOR_FORMAT = "$Component_$RoomName"  # what every surveyed project writes

# ERC violation types (TypeN of [Electrical Rules Check]) that report a check's findings.
_ERC = {"outside_sheet_border": 2, "variant_entry": 111, "net_name_syntax": 25}

FIXES = {
    "legacy_extension": (
        "Open each file in Altium and use File > Save As with the type Advanced Schematic "
        "binary (*.SchDoc) (a PCB: PCB Binary Files (*.PcbDoc)); remove the old documents "
        "from the project (Projects panel > right-click > Remove from Project), add the new "
        "ones (Project > Add Existing to Project), save the project and compile before "
        "running Update PCB."),
    "missing_schematic": (
        "Restore the file at that path, or remove the entry (it shows as missing in the "
        "Projects panel: right-click > Remove from Project) and add the file from where it "
        "is now (Project > Add Existing to Project); save the project and compile. Do not "
        "run Update PCB first: the parts of a sheet that is not compiled are offered for "
        "removal from the board."),
    "missing_device_sheet": (
        "Restore the device sheet, or point the project at the folder that holds it "
        "(Preferences > Data Management > Device Sheets) and compile; a device sheet that is "
        "not found leaves its sheet symbol without a child sheet."),
    "missing_other": (
        "Restore the file at that path, or remove the entry (Projects panel > right-click > "
        "Remove from Project) and save the project."),
    "annotation_renames": (
        "If Board Level Annotation was not intended: close the project, delete the "
        ".Annotation file from the project folder, reopen the project, remove the "
        ".Annotation entry the Projects panel now shows as missing, and save the project "
        "(there is no command that removes it). If it was intended, review it with Tools > "
        "Annotation > Board Level Annotate."),
    "annotation_idle": (
        "Nothing changes while it renames no part. If Board Level Annotation is not used, "
        "delete it as the KB describes (close the project, delete the file, reopen, remove "
        "the missing entry, save) so a later run cannot rename parts unnoticed."),
    "annotation_stale": (
        "Run Tools > Annotation > Board Level Annotate and accept the update so the file "
        "matches the sheets, then save the project."),
    "annotation_not_listed": (
        "If the design uses Board Level Annotation, add the file to the project (Project > "
        "Add Existing to Project); otherwise delete it so it cannot be added back by "
        "accident."),
    "outside_sheet_border": (
        "Move the part onto the sheet (or enlarge the sheet in the Properties panel, Page "
        "Options), or delete it if it is a leftover: it is compiled, so it reaches the PCB "
        "and the BOM, while prints and PDFs do not show it. ERC reports these as 'Object not "
        "completely within sheet boundaries'."),
    "compile_mask": (
        "If leaving these parts out was not intended, collapse the compile mask (click the "
        "triangle at its top-left corner, which disables it) or delete it, then compile and "
        "run Update PCB. A collapsed mask leaves everything under it compiled."),
    "masked_copies": (
        "Nothing to do if the masked copy is a kept alternative; otherwise delete it, so the "
        "designator names one part on the sheet."),
    "variant_entry": (
        "Open Project > Variants (Variant Management), re-apply the variation to the part it "
        "is meant for and remove the stale entry, then save the project. After a PCB "
        "reannotation (Back Annotate), check every variant this way; ERC reports these as "
        "'Incorrect link in project variant'."),
    "hidden_characters": (
        "Select the object (SCH List panel or Find Similar Objects), delete its text and "
        "retype it in Altium instead of pasting it, then save; Constraint Manager can save "
        "again once no net name carries such a character."),
    "non_ascii": (
        "Retype the name with ASCII characters only (e.g. u or 'micro' for the micro sign), "
        "then save and compile."),
    "whitespace": (
        "Retype the text without the leading or trailing space in the Properties panel "
        "(Find Similar Objects selects every copy), then save and compile."),
    "net_name_syntax": (
        "Write a bus range as Name[first..last] (two dots, numbers only), or remove the "
        "brackets from a label that names a single net."),
    "reserved_bom_parameter": (
        "Rename these parameters to names ActiveBOM does not reserve (the KB suggests MPN 1 "
        "and MFG 1) with Tools > Parameter Manager, or keep the data in the part's Part "
        "Choices, and update BOM templates and scripts that read the old names."),
    "copilot_mpn": (
        "Compare with the BOM Altium exports (Reports > Bill of Materials). Renaming the "
        "parameters as above makes the copilot read Comment as get_component's MPN; "
        "get_bom also reads 'Mfr Part Number N' with 'Mfr N'."),
    "channel_designator_format": (
        "Project > Project Options > Multi-Channel: set Designator Format to "
        "$RoomName_$Component (the KB's fix), compile and check the buses in the Messages "
        "panel (Duplicate Net Names, Bus Slice)."),
    "format_characters": (
        "Project > Project Options > Multi-Channel: remove the bracket, comma or space from "
        "the Designator Format and the level separator, then compile."),
}


def build_project_integrity(project: dict, read_hierarchy, read_integrity,
                            netlist: dict | None = None,
                            checks: list[str] | None = None) -> dict:
    """The integrity model of the loaded project, or {"error": ...}.

    read_hierarchy(path) returns a sheet's SheetHierarchy (parse_sch_hierarchy) and
    read_integrity(path) its SheetIntegrity (parse_sch_integrity), both cached. With a
    netlist, variant entries bind to the compiled designators; without one to the
    sheets' designators through the .Annotation file."""
    requested = [check.strip().lower() for check in checks or [] if check.strip()]
    unknown = [check for check in requested if check not in CHECKS]
    if unknown:
        return {"error": "invalid_checks",
                "message": f"Unknown checks {unknown}; use {list(CHECKS)}."}
    wanted = list(dict.fromkeys(requested or CHECKS))
    prj_path = project["prj_pcb_path"]
    try:
        documents = parse_prj_documents(prj_path)
        prj = parse_prj_pcb(prj_path)
        options = parse_prj_options(prj_path)
    except (OSError, UnicodeDecodeError) as error:
        return {"error": "project_file_unreadable",
                "message": f"{Path(prj_path).name}: {type(error).__name__}: {error}"}
    context = _Context(project, prj, options, documents, read_hierarchy, read_integrity,
                       netlist)
    findings: list[dict] = []
    for check in wanted:
        findings.extend(_RUNNERS[check](context))
    findings.sort(key=lambda f: (_SEVERITY_RANK[f["severity"]], _CHECK_RANK[f["check"]],
                                 -f["count"], f["kind"]))
    result: dict = {
        "project": project.get("name"),
        "prj_pcb": Path(prj_path).name,
        "mode": "netlist" if context.netlist_parts else "files",
        "checks_run": wanted,
        "summary": _summary(findings, wanted),
    }
    if context.not_applicable:
        result["not_applicable"] = {check: why for check, why in context.not_applicable.items()
                                    if check in wanted}
    if context.sheet_errors:
        result["sheets_not_read"] = context.sheet_errors
    result["findings"] = findings
    result["notes"] = [
        "Read from the saved .PrjPcb, sheets and .Annotation file, not Altium's compiler: "
        "Compile and ERC in Altium (Messages panel) are the final word.",
        "Names are checked as written on the sheets (the UTF-8 copy Altium stores for "
        "non-ASCII text, else read as Windows-1252); text a special string or the library "
        "shows instead is not checked.",
    ] + context.notes
    return result


def page_project_integrity(model: dict, offset: int, limit: int, example_limit: int) -> dict:
    """One page of findings, each with at most example_limit examples; the summary goes
    with every page, the notes with the first."""
    offset = max(0, int(offset or 0))
    findings = model["findings"]
    if offset:
        keep = ("project", "prj_pcb", "mode", "checks_run", "summary")
        result = {key: model[key] for key in keep if key in model}
    else:
        result = {key: value for key, value in model.items() if key != "findings"}
    page = []
    for finding in findings[offset:offset + limit]:
        finding = dict(finding)
        if len(finding["examples"]) > example_limit:
            finding["examples"] = finding["examples"][:example_limit]
            finding["examples_shown"] = example_limit
        page.append(finding)
    result["findings_total"] = len(findings)
    result["offset"] = offset
    result["has_more"] = offset + limit < len(findings)
    if result["has_more"]:
        result["next_offset"] = offset + limit
    result["findings"] = page
    return result


# ---------- shared state ----------

class _Context:
    """What the checks share: the parsed project, the sheets (read once, lazily) and
    which sheets are compiled more than once."""

    def __init__(self, project, prj, options, documents, read_hierarchy, read_integrity,
                 netlist):
        self.project, self.prj, self.options, self.documents = project, prj, options, documents
        self.read_hierarchy, self.read_integrity = read_hierarchy, read_integrity
        self.netlist = netlist
        self.netlist_parts = bool(netlist and netlist.get("components"))
        self.not_applicable: dict[str, str] = {}
        self.notes: list[str] = []
        self._sheets: list[tuple[str, str, object]] | None = None
        self.sheet_errors: list[dict] = []
        self.missing_sheets: list[str] = []
        self._copies: dict[str, int] | None = None
        self._annotation: list | None = None

    @property
    def sheets(self) -> list[tuple[str, str, object]]:
        """(name, path, SheetIntegrity) per readable sheet of the project."""
        if self._sheets is None:
            self._sheets = []
            for sheet in self.project.get("sheets") or []:
                if not Path(sheet["path"]).exists():
                    self.missing_sheets.append(sheet["name"])  # missing_document reports it
                    continue
                try:
                    self._sheets.append((sheet["name"], sheet["path"],
                                         self.read_integrity(sheet["path"])))
                except Exception as error:  # one bad sheet never hides the others
                    self.sheet_errors.append({"sheet": sheet["name"],
                                              "error": f"{type(error).__name__}: {error}"})
        return self._sheets

    @property
    def copies(self) -> dict[str, int]:
        """Compiled copies per sheet name (channels multiplied down the tree)."""
        if self._copies is None:
            self._copies = {}
            try:
                model = build_design_hierarchy(self.project, self.read_hierarchy)
            except Exception:  # the other checks still run
                model = {}
            for detail in model.get("sheets") or []:
                self._copies[detail["sheet"]] = detail.get("compiled_copies", 1)
        return self._copies

    @property
    def multi_channel(self) -> bool:
        return any(count > 1 for count in self.copies.values())

    @property
    def annotation(self) -> list:
        """The entries of every readable .Annotation file the project lists."""
        if self._annotation is None:
            self._annotation = []
            for path in self.prj.annotation_paths:
                try:
                    self._annotation.extend(parse_annotation(path))
                except Exception:  # missing_document reports a missing one
                    continue
        return self._annotation


def _finding(check: str, severity: str, kind: str, issue: str, examples: list[dict],
             fix: str, **fields) -> dict:
    finding = {"check": check, "severity": severity, "kind": kind, "count": len(examples),
               "issue": issue}
    erc = fields.pop("erc", None)
    finding.update(fields)
    if erc:
        finding["erc"] = erc
    finding["fix"] = fix
    if check in KB:
        finding["source"] = KB[check]
    finding["examples"] = examples
    return finding


def _erc(context: _Context, check: str) -> dict:
    """The ERC violation type that reports a check, with the level the project gives it."""
    number = _ERC[check]
    value = context.options.erc_levels.get(f"Type{number}", "")
    level = int(value) if value.isdigit() and int(value) < len(LEVELS) else None
    default = ERC_DEFAULTS.get(number)
    result = {"check": ERC_CHECKS[number - 1]}
    if level is not None:
        result["level"] = LEVELS[level]
    elif default is not None:
        result["level"] = f"{LEVELS[default]} (default)"
    if level == 0:
        result["note"] = "set to No Report: ERC does not report these"
    return result


def _point(x: float, y: float) -> dict:
    return {"x_mil": round(x, 2), "y_mil": round(y, 2)}


def _summary(findings: list[dict], wanted: list[str]) -> dict:
    severities = Counter(f["severity"] for f in findings)
    by_check: dict[str, dict] = {}
    for finding in findings:
        row = by_check.setdefault(finding["check"], {"findings": 0, "objects": 0,
                                                     "worst": finding["severity"]})
        row["findings"] += 1
        row["objects"] += finding["count"]
        if _SEVERITY_RANK[finding["severity"]] < _SEVERITY_RANK[row["worst"]]:
            row["worst"] = finding["severity"]
    return {
        "severity_counts": {level: severities.get(level, 0)
                            for level in ("error", "warning", "info")},
        "by_check": {check: by_check[check] for check in CHECKS if check in by_check},
        "clean": [check for check in wanted if check not in by_check],
    }


# ---------- file checks ----------

def _document_type(raw: str) -> str:
    return Path(raw.replace("\\", "/")).suffix.lower()


def _is_ole(path: str) -> bool | None:
    try:
        with open(path, "rb") as handle:
            return handle.read(8) == _OLE_MAGIC
    except OSError:
        return None


def _legacy_extension(context: _Context) -> list[dict]:
    rows: dict[str, list[dict]] = {"schematic": [], "pcb": []}
    for document in context.documents:
        suffix = _document_type(document.raw)
        kind = ("schematic" if _LEGACY_SCHEMATIC.match(suffix)
                else "pcb" if _LEGACY_PCB.match(suffix) else None)
        if kind is None:
            continue
        on_disk = Path(document.path).exists()
        row = {"document": document.raw, "section": document.section, "on_disk": on_disk}
        if on_disk:
            row["altium_binary"] = bool(_is_ole(document.path))
        rows[kind].append(row)
    findings = []
    if rows["schematic"]:
        findings.append(_finding(
            "legacy_extension", "error", "legacy_schematic_extension",
            "Schematic documents with a legacy extension: Altium opens them, but compile-"
            "based tasks may not treat them as schematics, so Update PCB can offer to remove "
            "every part they place (Footprint Manager and the netlist miss them too). These "
            "tools read no parts from them either.",
            rows["schematic"], FIXES["legacy_extension"]))
    if rows["pcb"]:
        # The KB blames the schematic extensions for Update PCB removing parts; it only
        # renames the PCB along with them, so a legacy PCB alone is no proven break.
        findings.append(_finding(
            "legacy_extension", "warning", "legacy_pcb_extension",
            "PCB documents with a legacy extension: the KB's fix renames the PCB to "
            ".PcbDoc along with the schematics (it names the schematic extensions, not the "
            "PCB's, as what makes Update PCB remove parts).",
            rows["pcb"], FIXES["legacy_extension"]))
    return findings


def _missing_document(context: _Context) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for document in context.documents:
        if Path(document.path).exists():
            continue
        suffix = _document_type(document.raw)
        if document.section.lower().startswith("devicesheet"):
            group = "device_sheet"
        elif suffix in _COMPILED_TYPES or _LEGACY_SCHEMATIC.match(suffix):
            group = "schematic"
        else:
            group = "other"
        groups[group].append({"document": document.raw, "section": document.section,
                              "type": suffix or "(none)"})
    findings = []
    if groups["schematic"]:
        findings.append(_finding(
            "missing_document", "error", "missing_schematic",
            "Sheets or harness files the project lists are not on disk: compile leaves them "
            "out, so their parts and nets are missing from the design, the BOM and the PCB "
            "(Update PCB offers to remove the parts).",
            groups["schematic"], FIXES["missing_schematic"]))
    if groups["device_sheet"]:
        # Altium also finds device sheets in the Device Sheets folders set in Preferences
        # (which this check cannot read), so the recorded path alone proves no break.
        findings.append(_finding(
            "missing_document", "warning", "missing_device_sheet",
            "Device sheets the project lists are not at the recorded path; unless a Device "
            "Sheets folder in Preferences holds them, the sheet symbols placing them have no "
            "child sheet and their parts are not compiled.",
            groups["device_sheet"], FIXES["missing_device_sheet"]))
    if groups["other"]:
        findings.append(_finding(
            "missing_document", "warning", "missing_document",
            "Documents the project lists are not on disk (outputs, libraries, the PCB, "
            "the .Annotation file or others): what uses them fails or falls back silently.",
            groups["other"], FIXES["missing_other"]))
    return findings


def _annotation_file(context: _Context) -> list[dict]:
    prj_path = Path(context.project["prj_pcb_path"])
    listed = {os.path.normcase(p) for p in context.prj.annotation_paths}
    findings = []
    default_name = f"{prj_path.stem}.annotation".lower()
    try:
        on_disk = [p for p in prj_path.parent.iterdir() if p.name.lower() == default_name]
    except OSError:
        on_disk = []
    unlisted = [p for p in on_disk if os.path.normcase(str(p.resolve())) not in listed]
    if unlisted:
        findings.append(_finding(
            "annotation_file", "info", "annotation_not_listed",
            "A .Annotation file named after the project is in the project folder but not "
            "listed in the .PrjPcb: these tools do not apply it.",
            [{"document": p.name} for p in unlisted], FIXES["annotation_not_listed"]))
    entries = context.annotation
    if not entries:
        return findings
    if context.multi_channel:
        context.notes.append(
            "The design has sheets compiled more than once (multi-channel): its .Annotation "
            "file holds the channel designators and is needed; it is not reported as a "
            "leftover.")
    else:
        renamed = {}
        for entry in entries:
            if entry.logical and entry.physical != entry.logical:
                renamed.setdefault((entry.sheet, entry.logical, entry.physical), entry)
        if renamed:
            examples = [{"sheet": sheet, "designator": logical, "board_designator": physical}
                        for sheet, logical, physical in sorted(
                            renamed, key=lambda key: (key[0], natural_key(key[1])))]
            findings.append(_finding(
                "annotation_file", "warning", "annotation_renames",
                "The project's .Annotation file (Board Level Annotation) gives these parts a "
                "compiled designator other than the one on their sheet, and no sheet is "
                "compiled more than once: the Compiled tab, the PCB and the BOM show the "
                "board designator while the editor shows the sheet's. Values set by Board "
                "Level Annotation override the editor's and are sent to the PCB.",
                examples, FIXES["annotation_renames"]))
        else:
            findings.append(_finding(
                "annotation_file", "info", "annotation_idle",
                "The project lists a .Annotation file but no sheet is compiled more than once "
                "and the file renames no part: a leftover of Board Level Annotation.",
                [{"document": Path(p).name} for p in context.prj.annotation_paths],
                FIXES["annotation_idle"]))
    # A sheet not read or missing on disk would make its parts' entries look stale.
    if context.sheets and not context.sheet_errors and not context.missing_sheets:
        known = {uid for _, _, data in context.sheets for part in data.parts
                 for uid in [part.unique_id] if uid}
        stale = {}
        for entry in entries:
            if entry.component_uid not in known:
                stale.setdefault((entry.sheet, entry.logical, entry.physical), entry)
        if stale:
            examples = [{"sheet": sheet, "designator": logical, "board_designator": physical}
                        for sheet, logical, physical in sorted(
                            stale, key=lambda key: (key[0], natural_key(key[1])))]
            findings.append(_finding(
                "annotation_file", "info", "annotation_stale",
                "Entries of the .Annotation file whose UniqueId is on no part of the sheets "
                "(parts deleted or re-placed since Board Level Annotation ran).",
                examples, FIXES["annotation_stale"]))
    return findings


def _outside_sheet_border(context: _Context) -> list[dict]:
    outside: dict[tuple[str, str], dict] = {}
    for sheet, _, data in context.sheets:
        width, height = data.width_mil, data.height_mil
        # Whether Altium turns a custom size on a portrait sheet is not documented (KiCad's
        # importer does, parse_sch_integrity does not): a point inside either way is inside.
        sizes = [(width, height)]
        if data.sheet_style == "Custom" and data.portrait:
            sizes.append((height, width))
        for part in data.parts:
            meta = data.components.get(part.designator.strip())
            if part.masked or (meta is not None and meta.kind == 2):
                continue  # not compiled (compile mask) or graphical only
            inside = [any(0 <= x <= w and 0 <= y <= h for w, h in sizes)
                      for x, y in part.points]
            if all(inside):
                continue
            key = (sheet, part.designator.strip() or part.unique_id)
            status = "outside" if not any(inside) else "partly_outside"
            held = outside.get(key)
            if held is None or (held["status"] == "partly_outside" and status == "outside"):
                outside[key] = {
                    "sheet": sheet, "designator": key[1], "status": status,
                    **_point(part.x_mil, part.y_mil),
                    "sheet_size": f"{data.sheet_style} {width:g} x {height:g} mil"
                                  + (" portrait" if data.portrait else ""),
                }
    rows = sorted(outside.values(), key=lambda r: (r["sheet"], natural_key(r["designator"])))
    findings = []
    full = [row for row in rows if row["status"] == "outside"]
    partly = [row for row in rows if row["status"] == "partly_outside"]
    erc = _erc(context, "outside_sheet_border")
    if full:
        findings.append(_finding(
            "outside_sheet_border", "warning", "outside",
            "Parts placed outside the sheet (origin and every drawn pin beyond the sheet "
            "size): they are compiled, so they reach the PCB and the BOM, but prints and PDFs "
            "of the sheet do not show them.",
            [{k: v for k, v in row.items() if k != "status"} for row in full],
            FIXES["outside_sheet_border"], erc=erc))
    if partly:
        findings.append(_finding(
            "outside_sheet_border", "info", "partly_outside",
            "Parts that reach past the sheet edge (origin or a pin end outside the sheet "
            "size): prints cut them off.",
            [{k: v for k, v in row.items() if k != "status"} for row in partly],
            FIXES["outside_sheet_border"], erc=erc))
    return findings


def _compile_mask(context: _Context) -> list[dict]:
    masked, copies = [], []
    for sheet, _, data in context.sheets:
        by_uid = {part.unique_id: part for part in data.parts if part.unique_id}
        for designator, meta in data.components.items():
            part = by_uid.get(meta.unique_id)
            where = _point(part.x_mil, part.y_mil) if part else {}
            if meta.compile_masked and "?" not in designator:
                masked.append({"sheet": sheet, "designator": designator, **where})
            elif meta.masked_unique_ids:
                copy = next((by_uid[uid] for uid in meta.masked_unique_ids if uid in by_uid),
                            None)
                copies.append({"sheet": sheet, "designator": designator,
                               **(_point(copy.x_mil, copy.y_mil) if copy else where)})
    def key(row: dict) -> tuple:
        return row["sheet"], natural_key(row["designator"])

    findings = []
    if masked:
        findings.append(_finding(
            "compile_mask", "info", "masked_parts",
            "Parts inside an enabled compile mask: Altium leaves them out of the compiled "
            "design, so they are not on the PCB (Update PCB removes them if they were) and "
            "not in the BOM, although the sheet shows them. Usually deliberate: confirm it "
            "is intended.",
            sorted(masked, key=key), FIXES["compile_mask"]))
    if copies:
        findings.append(_finding(
            "compile_mask", "info", "masked_copies",
            "A compile-masked copy shares its designator with a live part on the same sheet "
            "(the live one is compiled; the location is the masked copy's).",
            sorted(copies, key=key), FIXES["masked_copies"]))
    return findings


_VARIANT_SEVERITY = {
    "no_such_part": "warning", "unique_id_not_found": "warning",
    "designator_changed": "warning", "ambiguous_unique_id": "warning",
    "ambiguous_designator": "warning", "on_masked_part": "info",
    "unique_id_not_compiled": "info", "varied_but_not_fitted": "info",
}
_VARIANT_ISSUES = {
    "no_such_part": ("Variant entries that name no part: neither their UniqueId nor their "
                     "designator is on a sheet (part deleted, or re-placed and renamed); "
                     "they apply to nothing."),
    "unique_id_not_found": ("Variant entries whose UniqueId is on no part while a part "
                            "carries their designator: Altium binds variations by UniqueId, "
                            "so it does not apply them to that part (the copilot's BOM "
                            "matches them by designator)."),
    "designator_changed": ("Variant entries whose UniqueId is on a part that now has another "
                           "designator (e.g. after a PCB reannotation): Altium applies them "
                           "to that part, while the designator stored with them is stale "
                           "and may name another part."),
    "ambiguous_unique_id": ("Variant entries whose UniqueId is on several channel copies "
                            "while their stored designator names none of them."),
    "ambiguous_designator": ("Parameter variations on a designator that several component "
                             "variations carry for different parts: not applied."),
    "on_masked_part": ("Variant entries whose UniqueId is on a compile-masked copy, which is "
                       "not compiled: not applied."),
    "unique_id_not_compiled": ("Variant entries whose UniqueId is on a sheet part outside the "
                               "compiled design: matched by their designator."),
    "varied_but_not_fitted": "Parameter variations on parts the variant does not fit.",
}


def _variant_entry(context: _Context) -> list[dict]:
    # dnp_refdes: a variant in the older [Variation]/[CompVar] format has only these.
    variants = [v for v in context.prj.variants
                if v.variations or v.param_variations or v.dnp_refdes]
    if not variants:
        context.not_applicable["variant_entry"] = "the project has no variant entries"
        return []
    if context.netlist_parts:
        base = context.netlist
    else:
        sheets = [(name, data.components) for name, _, data in context.sheets]
        base = enrich_netlist_sheets({"pin_to_net": {}}, sheets, context.annotation)
    names = variant_names(base)
    unsure = (not context.netlist_parts and context.multi_channel and not context.annotation)
    if unsure:
        context.notes.append(
            "variant_entry: without a netlist or .Annotation file the channel designators "
            "of a multi-channel design are unknown, so entries whose stored designator differs "
            "from the sheet's (designator_changed, ambiguous_unique_id) are not reported. "
            "Load the project with a netlist for them.")
    groups: dict[str, list[dict]] = defaultdict(list)
    for variant in variants:
        plan = plan_variant(variant, base, names)
        for entry in plan.stale:
            issue = entry["issue"]
            if unsure and issue in ("designator_changed", "ambiguous_unique_id"):
                continue
            groups[issue].append({"variant": variant.name,
                                  "designator": entry.get("designator") or "",
                                  "detail": entry["detail"]})
    if not context.netlist_parts and (context.sheet_errors or context.missing_sheets):
        context.notes.append(
            "variant_entry: some sheets were not read (sheets_not_read) or are missing on "
            "disk (missing_document), so entries naming their parts read as no_such_part.")
    erc = _erc(context, "variant_entry")
    return [
        _finding("variant_entry", _VARIANT_SEVERITY.get(issue, "warning"), issue,
                 _VARIANT_ISSUES.get(issue, issue), rows, FIXES["variant_entry"], erc=erc)
        for issue, rows in groups.items()
    ]


# ---------- name checks ----------

def _names(context: _Context):
    """(sheet, SheetName) for every distinct name on the readable sheets; the parts of a
    multi-part component carry copies of the same designator and parameters."""
    seen = set()
    for sheet, _, data in context.sheets:
        for item in data.names:
            key = (sheet, item.kind, item.owner, item.text, item.value)
            if key not in seen:
                seen.add(key)
                yield sheet, item


def _hidden(char: str) -> bool:
    category = unicodedata.category(char)
    return category[0] == "C" or (category[0] == "Z" and char != " ")


def _describe(char: str) -> str:
    return f"U+{ord(char):04X} {unicodedata.name(char, 'UNNAMED')}"


def _shown(text: str) -> str:
    """The text with hidden characters spelled out, e.g. 'VBUS<U+200B>_T'."""
    return "".join(f"<U+{ord(c):04X}>" if _hidden(c) else c for c in text)


def _example(sheet: str, item, text: str, field: str) -> dict:
    row: dict = {"sheet": sheet, "object": field}
    if field == "parameter_value":
        row |= {"designator": item.owner, "parameter": item.text.strip(), "value": text}
    elif field == "parameter":
        row |= {"designator": item.owner, "parameter": text}
    elif field == "designator":
        row["designator"] = text
    else:
        row["name"] = text
        if field == "sheet_entry" and item.owner:
            row["sheet_symbol"] = item.owner
    if _shown(text) != text:
        row["shown"] = _shown(text)
    return row | _point(item.x_mil, item.y_mil)


def _texts(item):
    """(text, field) pairs a SheetName carries: a parameter's name and its value."""
    if item.kind == "parameter":
        yield item.text, "parameter"
        if item.value:
            yield item.value, "parameter_value"
    else:
        yield item.text, item.kind


def _character_findings(context: _Context, check: str) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    characters: dict[str, Counter] = defaultdict(Counter)
    for sheet, item in _names(context):
        for text, field in _texts(item):
            if check == "hidden_characters":
                found = [c for c in text if _hidden(c)]
            elif field == "parameter_value":
                continue  # micro, ohm and degree signs are at home in values
            else:
                found = [c for c in text if ord(c) > 127 and not _hidden(c)]
            if found:
                example = _example(sheet, item, text, field)
                example["characters"] = sorted({_describe(c) for c in found})
                groups[field].append(example)
                characters[field].update(_describe(c) for c in found)
    findings = []
    for field, rows in groups.items():
        net = field in NET_KINDS or field == "designator"
        if check == "hidden_characters":
            severity = "error" if net else "warning"
            issue = (f"Hidden characters in {_KIND_LABELS[field]} (control, format or "
                     "non-ASCII space characters, often pasted from a datasheet or a "
                     "spreadsheet): Altium does not show them, " +
                     ("and they make saving Constraint Manager changes fail ('Unable to "
                      "save file <project>.PrjPcb.CstrSchDoc'); a name with one is also a "
                      "different name from the one typed without it." if net else
                      "and a value with one differs from the same value typed, so BOM "
                      "lines and parameter matches split."))
        else:
            severity = "warning" if net else "info"
            issue = (f"Non-ASCII characters in {_KIND_LABELS[field]}: " +
                     ("the KB names unsupported characters in net names and designators as "
                      "a cause of Constraint Manager save failures, and PCB outputs (Gerber "
                      "netlists, IPC-D-356, pick and place) carry ASCII." if net else
                      "BOM templates, scripts and other tools may not match them."))
        findings.append(_finding(
            check, severity, field, issue, rows, FIXES[check],
            characters=dict(characters[field].most_common(10))))
    return findings


def _hidden_characters(context: _Context) -> list[dict]:
    return _character_findings(context, "hidden_characters")


def _non_ascii(context: _Context) -> list[dict]:
    return _character_findings(context, "non_ascii")


def _whitespace(context: _Context) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for sheet, item in _names(context):
        for text, field in _texts(item):
            if text.strip(" ") != text:
                groups[field].append(_example(sheet, item, f'"{text}"', field) | {
                    "where": ("both ends" if text.startswith(" ") and text.endswith(" ")
                              else "leading" if text.startswith(" ") else "trailing")})
    findings = []
    for field, rows in groups.items():
        if field in NET_KINDS:
            severity, effect = "warning", (
                "it is part of the name, so anything that compares net names exactly "
                "(another sheet's label or port, net classes, PCB rules, scripts) sees "
                "another net than the one typed without it")
        elif field in ("designator", "parameter"):
            severity, effect = "warning", (
                "it is part of the name, so a lookup by the name typed without it (BOM "
                "columns, special strings, variants, scripts) misses it")
        else:
            severity, effect = "info", "BOM lines split on values that differ only by it"
        findings.append(_finding(
            "whitespace", severity, field,
            f"Leading or trailing spaces in {_KIND_LABELS[field]}: {effect}.",
            rows, FIXES["whitespace"]))
    return findings


def _net_name_syntax(context: _Context) -> list[dict]:
    rows = []
    for sheet, item in _names(context):
        if item.kind not in NET_KINDS:
            continue
        text = item.text.strip()
        repeat = ENTRY_REPEAT_RE.match(text)  # a sheet entry's Repeat(Name)
        name = repeat.group(1) if repeat else text
        if ("[" in name or "]" in name) and not _BUS_RANGE.fullmatch(name):
            rows.append(_example(sheet, item, text, item.kind))
    if not rows:
        return []
    return [_finding(
        "net_name_syntax", "warning", "bus_range",
        "Net names with brackets that are not a bus range Name[first..last]: Altium reads "
        "brackets as a bus range, so the name is either rejected (Bus range syntax errors) "
        "or not the single net it looks like.",
        rows, FIXES["net_name_syntax"], erc=_erc(context, "net_name_syntax"))]


def _mpn_number(name: str) -> int:
    """N of 'Manufacturer Part Number N', 0 for the unnumbered name."""
    tail = name.rsplit(" ", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def _reserved_bom_parameter(context: _Context) -> list[dict]:
    reserved, copilot = [], []
    bomdoc = any(_document_type(d.raw) == ".bomdoc" for d in context.documents)
    for sheet, _, data in context.sheets:
        by_part: dict[str, dict[str, str]] = {}
        where: dict[str, tuple[float, float]] = {}
        for item in data.names:
            if item.kind != "parameter" or not item.owner or "?" in item.owner:
                continue
            value = (item.value or "").strip()
            if value in ("", "*"):
                continue
            by_part.setdefault(item.owner, {}).setdefault(item.text.strip(), value)
            where.setdefault(item.owner, (item.x_mil, item.y_mil))
        for designator in sorted(by_part, key=natural_key):
            params = by_part[designator]
            names = [name for name in params if _RESERVED.match(name)]
            for name in names:
                reserved.append({"sheet": sheet, "designator": designator, "parameter": name,
                                 "value": params[name], **_point(*where[designator])})
        # The copilot's MPN as parse_sch_doc reads it (ComponentMeta.mpn): its params hold
        # the parameters _MPN_RE considered (no empty or quoted text), and the unnumbered
        # name beats the lowest N.
        written = {(item.owner, item.text.strip().lower()): item.text.strip()
                   for item in data.names if item.kind == "parameter"}
        for designator in sorted(data.components, key=natural_key):
            meta = data.components[designator]
            if "?" in designator or not meta.mpn:
                continue
            numbers = [_mpn_number(name) for name in meta.params if _RESERVED_MPN.match(name)]
            if numbers and min(numbers) > 0:
                name = f"manufacturer part number {min(numbers)}"
                copilot.append({"sheet": sheet, "designator": designator,
                                "parameter": written.get((designator, name), name),
                                "value": meta.mpn})
    findings = []
    if reserved:
        counts = Counter(row["parameter"].lower() for row in reserved)
        findings.append(_finding(
            # Info: the overwrite is Altium's intended behaviour, and it fires on most
            # projects that keep MPNs in parameters (39 of 63 in the validation set).
            "reserved_bom_parameter", "info", "reserved_name",
            "Local parameters with names ActiveBOM reserves ('Manufacturer N', "
            "'Manufacturer Part Number N'): when a part has Part Choices, the BOM Altium "
            "exports fills these columns from them and the local values are overwritten "
            "(intended behaviour, per the KB).",
            reserved, FIXES["reserved_bom_parameter"],
            parameters=dict(counts.most_common(10)), bomdoc_in_project=bomdoc))
    if copilot:
        findings.append(_finding(
            "reserved_bom_parameter", "info", "copilot_mpn_from_reserved_name",
            "The copilot's MPN for these parts comes from a reserved parameter (sch_doc's "
            "_MPN_RE reads 'Manufacturer Part Number [N]' for get_component and search; "
            "get_bom and check_bom read 'Manufacturer Part Number N' with 'Manufacturer N'), "
            "so where the parts have Part Choices the copilot's MPN can differ from the BOM "
            "Altium exports.",
            copilot, FIXES["copilot_mpn"]))
    return findings


def _channel_designator_format(context: _Context) -> list[dict]:
    channel_sheets = {name: count for name, count in context.copies.items() if count > 1}
    if not channel_sheets:
        context.not_applicable["channel_designator_format"] = (
            "no sheet is compiled more than once (no multi-channel design)")
        return []
    design = context.options.design
    written = design.get("ChannelDesignatorFormatString")
    fmt = written or DEFAULT_DESIGNATOR_FORMAT
    separator = design.get("ChannelRoomLevelSeperator", "_")
    tokens = re.findall(r"\$\w+", fmt)
    suffix = bool(tokens) and tokens[-1].lower() not in _COMPONENT_TOKENS
    base = {"designator_format": fmt}
    if not written:
        base["designator_format_written"] = False
    findings = []
    if suffix:
        rows = []
        for sheet, _, data in context.sheets:
            if sheet not in channel_sheets:
                continue
            buses = sorted({item.text.strip() for item in data.names
                            if item.kind in NET_KINDS and "[" in item.text})
            if data.bus_wires or buses:
                rows.append({"sheet": sheet, "compiled_copies": channel_sheets[sheet],
                             "bus_wires": data.bus_wires, "bus_names": buses[:5],
                             "bus_names_total": len(buses)})
        if rows:
            findings.append(_finding(
                "channel_designator_format", "warning", "channel_text_after_bus_names",
                f"The multi-channel Designator Format {fmt} puts the channel text after the "
                "name, and these sheets compiled once per channel carry buses: the KB "
                "reports that multi-channel naming then adds text to the bus names, breaking "
                "the bus syntax, so compile reports Duplicate Net Names (Bus Slice) and the "
                "bus nets do not connect as drawn.",
                rows, FIXES["channel_designator_format"], **base))
    literal = re.sub(r"\$\w+", "", fmt)
    odd = sorted({c for c in literal + separator
                  if c in _BUS_BREAKING or c.isspace() or ord(c) > 127})
    if odd:
        findings.append(_finding(
            "channel_designator_format", "warning", "format_characters",
            "The multi-channel Designator Format or level separator writes characters that "
            "bus and net names cannot carry into every channel's designators and names.",
            [{"designator_format": fmt, "level_separator": separator,
              "characters": [_describe(c) for c in odd]}],
            FIXES["format_characters"]))
    return findings


_RUNNERS = {
    "legacy_extension": _legacy_extension,
    "missing_document": _missing_document,
    "annotation_file": _annotation_file,
    "outside_sheet_border": _outside_sheet_border,
    "compile_mask": _compile_mask,
    "variant_entry": _variant_entry,
    "hidden_characters": _hidden_characters,
    "non_ascii": _non_ascii,
    "whitespace": _whitespace,
    "net_name_syntax": _net_name_syntax,
    "reserved_bom_parameter": _reserved_bom_parameter,
    "channel_designator_format": _channel_designator_format,
}
