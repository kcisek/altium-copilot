"""The project's Output Jobs (.OutJob) read offline: each OutJob's containers (PDF, folder
structure, video, print job) with their path and naming settings, and its outputs by
category with the containers they are connected to and their order there, the data
source document and the variant; what the outputs cover (fabrication data, NC drill,
pick-and-place, assembly drawings, BOM, ERC and DRC validation); gaps and pre-flight
findings, each with a severity and how to fix it in Altium (with the Altium
knowledge-base article behind it where there is one); and the generated files at each
container's folder against the design documents they come from.

OutJobs are found through the .PrjPcb document list; .OutJob files in the project folder
(or one folder down, in a folder without a project of its own) that the project does not
list are reported as well. Keys are mapped where Altium's example and template OutJobs
and a corpus of real projects verify them (parsers/out_job.py); the rest is passed raw.
Nothing is written: generated files are only listed for their modification times.

Severity: error stops an output or a release, or produces wrong data; warning is a gap
or a likely wrong result; info is worth a look."""
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

import olefile

try:
    from parsers.out_job import CONTAINER_KINDS, OutJob, OutJobOutput, parse_out_job
    from parsers.pcb_doc import _region_heads, decode_board, read_text_records
    from parsers.prj_pcb import parse_prj_documents, parse_prj_options, parse_prj_pcb
    from services.project_options import (
        ERC_CHECKS, ERC_DEFAULTS, LEVELS, MATRIX_DEFAULT, MATRIX_DEFAULT_UNKNOWN, MATRIX_LABELS,
        NO_ERROR_TYPE,
    )
except ImportError:
    from server.parsers.out_job import CONTAINER_KINDS, OutJob, OutJobOutput, parse_out_job
    from server.parsers.pcb_doc import _region_heads, decode_board, read_text_records
    from server.parsers.prj_pcb import parse_prj_documents, parse_prj_options, parse_prj_pcb
    from server.services.project_options import (
        ERC_CHECKS, ERC_DEFAULTS, LEVELS, MATRIX_DEFAULT, MATRIX_DEFAULT_UNKNOWN, MATRIX_LABELS,
        NO_ERROR_TYPE,
    )

CHECKS = (
    "outjob_files", "no_fabrication_data", "no_nc_drill", "no_pick_and_place",
    "no_assembly_drawing", "no_bom", "no_erc_validation", "no_drc_validation",
    "no_odb_ipc2581", "bom_pnp_variant", "assembly_drawing_variant", "wrong_document",
    "erc_settings_differ", "output_path", "long_path", "fabrication_origin",
    "overlay_not_in_stackup", "board_cutouts", "draftsman_variant", "stale_outputs",
    "not_connected",
)
_CHECK_RANK = {check: rank for rank, check in enumerate(CHECKS)}
_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}
EXAMPLE_LIMIT = 25

_KB = "https://www.altium.com/documentation/knowledge-base/altium-designer/"
KB = {
    "erc_settings_differ": (_KB + "release-fails-when-erc-validation-settings-differ-from-"
                            "project-options"),
    "output_path": _KB + "empty-project-release-caused-by-output-path-configuration",
    "long_path": _KB + "file-buffer-is-not-initialized-error",
    "fabrication_origin": _KB + "resolve-component-position-offset-in-odb-files",
    "overlay_not_in_stackup": _KB + "odb-export-missing-overlay-layers",
    "board_cutouts": _KB + "board-cutout-not-in-gerber-output",
    "draftsman_variant": _KB + "how-to-control-the-draftsman-output-variant-for-project-release",
}

# OutputType values as Altium writes them (every one seen in its example and template
# OutJobs or the corpus).
FABRICATION_FORMATS = {"Gerber": "Gerber RS-274X", "Gerber X2": "Gerber X2", "ODB": "ODB++",
                       "IPC2581": "IPC-2581"}
INTELLIGENT_FORMATS = {"ODB", "IPC2581"}  # carry the drill data and the stackup themselves
ROLES = {
    "fabrication_data": set(FABRICATION_FORMATS),
    "nc_drill": {"NC Drill"},
    "pick_and_place": {"Pick Place"},
    "assembly_drawing": {"Assembly", "PCBDrawing"},
    "bom": {"BOM_PartType", "SimpleBOM"},
    "erc": {"Electrical Rules Check"},
    "drc": {"Design Rules Check"},
}
# Outputs made from the PcbDoc, from the schematic sheets, and Draftsman drawings; any other
# type (BOMs, comparisons) is made from both.
PCB_TYPES = {
    "Gerber", "Gerber X2", "ODB", "IPC2581", "NC Drill", "Pick Place", "Test Points",
    "Test Points For Assembly", "Assembly", "PCB Print", "PCB 3D Print", "Composite",
    "CompositeDrill", "Drill", "Final", "Mask", "Plane", "PDF3D", "ExportSTEP",
    "Board Stack Report", "Design Rules Check", "Footprint Comparison Report",
    "Save As/Export PCB",
}
SCH_TYPES = {
    "Schematic Print", "Electrical Rules Check", "EDIF", "MultiWire", "Pcad", "ProtelNetlist",
    "ReportHierarchy", "SinglePinNetReporter", "ComponentCrossReference",
}
DRAWING_TYPES = {"PCBDrawing"}
# Outputs whose content depends on the variant.
VARIANT_TYPES = {
    "BOM_PartType", "SimpleBOM", "Pick Place", "Assembly", "PCBDrawing", "Schematic Print",
    "PCB Print", "PCB 3D Print", "PDF3D", "ExportSTEP", "Composite", "Test Points",
    "Test Points For Assembly",
}
NO_VARIATIONS = "[No Variations]"
# Data sources written without brackets that name no file (Schematic Print, 9 in the corpus).
GENERIC_SOURCES = {"All SCH Documents"}
# The configuration record each output type is set up with.
RECORD_FOR_TYPE = {
    "Gerber": "GerberView", "Gerber X2": "GerberX2View", "ODB": "ODBView",
    "NC Drill": "DrillView", "IPC2581": "IPC2581View", "Pick Place": "PickPlaceView",
    "Electrical Rules Check": "ERCReportView",
}
# A document saved this much after the newest generated file makes the outputs stale;
# closer times are a checkout or copy writing both.
STALE_TOLERANCE_S = 120
CATEGORY_ORDER = ("Fabrication", "Assembly", "Documentation", "Report", "Validation",
                  "Export", "Netlist")
# Settings mapped per configuration record (Record=...), as sampled.
SETTING_KEYS = {
    "GerberView": {"GerberUnit": "units", "NumberOfDecimals": "decimals",
                   "OriginPosition": "origin", "CentrePlots": "centre_plots",
                   "PlotBoardProfile": "plot_board_profile"},
    "GerberX2View": {"GerberUnit": "units", "NumberOfDecimals": "decimals",
                     "PlotPTHPairs": "plated_drill_files",
                     "PlotNPTHPairs": "non_plated_drill_files",
                     "PlotBoardProfile": "plot_board_profile"},
    "ODBView": {"Units": "units"},
    "DrillView": {"Units": "units", "NumberOfUnits": "integer_digits",
                  "NumberOfDecimals": "decimals", "OriginPosition": "origin",
                  "ZeroesMode": "zeroes", "GenerateSeparatePlatedNonPlatedFiles":
                  "separate_plated_files", "GenerateDrilledSlotsG85": "slots_as_g85"},
    "IPC2581View": {"IPC2581Version": "version", "MeasurementSystem": "units",
                    "FloatingPointPrecision": "precision"},
    "PickPlaceView": {"Units": "units", "GenerateCSVFormat": "csv",
                      "GenerateTextFormat": "text", "IncludeVariations": "include_variations"},
    "ERCReportView": {"ERCReportSuppressedErrors": "report_suppressed",
                      "ERCValidationErrorLevel": "validation_error_level_raw",
                      "ERCReportMode": "report_mode_raw"},
}
# A container folder written as an expression ("=ProjectName", "=Revision + '_Out'",
# "=CurrentDate+'_'+CurrentTime"): quoted text and names joined by '+'.
_EXPRESSION_TOKEN = re.compile(r"\s*(?:'([^']*)'|([A-Za-z_]\w*)|(\+))\s*")
_MECHANICAL_IDS = range(0x01020001, 0x01020100)  # V7 ids of Mechanical 1..255
TOP_OVERLAY_ID, BOTTOM_OVERLAY_ID = 16973830, 16973831  # V9_STACK_LAYERn_LAYERID
MAX_PATH = 260
NEAR_MAX_PATH = 230
_FILE_NAME_ALLOWANCE = 30  # e.g. "Design Rule Check - " + the board name + extension
_WALK_LIMIT = 20_000
_DESIGN_SUFFIXES = {".prjpcb", ".schdoc", ".pcbdoc", ".pcbdwf", ".outjob", ".bomdoc",
                    ".harness", ".prjpcbstructure", ".annotation", ".schlib", ".pcblib"}

FIXES = {
    "outjob_missing": (
        "Restore the OutJob at that path, or remove the entry (Projects panel > right-click > "
        "Remove from Project) and save the project."),
    "outjob_unlisted": (
        "If the project should use it, add it (Project > Add Existing to Project) and save "
        "the project; otherwise delete or move it so nobody generates from a stale job."),
    "outjob_unreadable": "Open the OutJob in Altium and save it again.",
    "gap": (
        "Open the OutJob, add the output with 'Add New {kind} Output' in its category "
        "(right-click in the Outputs list), set its data source, then connect it to a "
        "container by clicking its Enabled circle under that container (a number shows its "
        "order) and save."),
    "connect": (
        "Select the container on the right of the OutJob and click the output's Enabled "
        "circle so a green line connects it (a number shows its order), then save."),
    "no_nc_drill": (
        "Add an NC Drill output (Fabrication Outputs > NC Drill Files) on the same PcbDoc "
        "and origin as the Gerbers and connect it to the fabrication container, or add an "
        "ODB++ or IPC-2581 output, which carry the drill data."),
    "no_odb_ipc2581": (
        "Add an ODB++ or IPC-2581 output (Fabrication Outputs) next to the Gerbers, or send "
        "the fabricator the stackup and drill spans another way."),
    "bom_pnp_variant": (
        "Set the BOM and the pick-and-place outputs to the same variant (the Variant Choice "
        "of the OutJob, or each output's Variant column when variants differ per output) "
        "and generate both again."),
    "assembly_drawing_variant": (
        "Add an assembly drawing for that variant (an Assembly Drawings output, or a "
        "Draftsman drawing with that variant in its Properties) and connect it."),
    "missing_document": (
        "Select the output's Data Source column and pick the project's document (or "
        "restore the file), then save the OutJob."),
    "not_in_project": (
        "Point the output's Data Source at the project's own document, or add the file to "
        "the project if it belongs to it, then save the OutJob."),
    "drawing_source": (
        "Open the Draftsman drawing, check Properties > Source points at this project's "
        "PcbDoc and run Tools > Update Board (Import Changes from the PcbDoc), then save."),
    "ambiguous_source": (
        "Set each PCB output's Data Source to the PcbDoc it is for instead of the default."),
    "erc_settings_differ": (
        "In the OutJob, right-click the Electrical Rules Check output > Configure; on its "
        "Error Reporting tab use Set To Project Defaults (the KB's fix), or set the levels "
        "you release with in both places, then save the OutJob and run the release again."),
    "output_path": (
        "Project > Project Options > Options: set the Output Path and ECO Log Path to a "
        "folder on the same drive as the project (e.g. the default relative "
        "'Project Outputs for <project>'), and give manually managed containers a base path "
        "inside the project folder (the container's Change link > Manually Managed)."),
    "long_path": (
        "Shorten the path: a shorter base folder (Project Options > Output Path, or the "
        "container's base path), no container or output-type sub-folders, or shorter "
        "output names; or enable Windows long paths (LongPathsEnabled) as Altium's "
        "long-path documentation describes."),
    "fabrication_origin": (
        "Open the Gerber output's setup (Advanced tab, Position on Film) and choose "
        "Reference to relative origin, as ODB++ and the NC Drill output use; generate "
        "both again."),
    "drill_origin": (
        "Give the Gerber and NC Drill outputs the same origin (Gerber Setup > Advanced > "
        "Position on Film, NC Drill Setup > Coordinate Positions) and generate both again."),
    "overlay_not_in_stackup": (
        "Layer Stack Manager: add the overlay layer back (right-click > Insert Layer Above/"
        "Below > Overlay), save the board, then check the layer is enabled in the ODB++ and "
        "Gerber setups (the KB's fix)."),
    "board_cutouts": (
        "Design > Board Shape > Create Primitives From Board Shape on a mechanical layer "
        "with a small width and Include Cutouts, then enable that mechanical layer in the "
        "Gerber setup (the KB's fix); or confirm with the fabricator that the board "
        "profile file carries the cutouts."),
    "draftsman_view": (
        "In the Draftsman drawing select each such view and set its Variation to "
        "'Document: <variant>' so it follows the drawing; set the drawing's variant with "
        "nothing selected (Properties > Source > Variants) (the KB's fix)."),
    "draftsman_document": (
        "If the drawing should show the OutJob's variant, set it in the drawing (nothing "
        "selected, Properties > Source > Variants): the drawing's own variant is what a "
        "release prints."),
    "stale_outputs": (
        "Generate the OutJob again (right-click the container > Generate Content, or the "
        "Project Releaser) after the last design change, and archive or delete old outputs "
        "so nobody sends them."),
    "not_connected": (
        "Connect the output to the container that should generate it (its Enabled circle "
        "under that container), or delete it if it is not needed."),
}


def build_output_jobs(project: dict, read_drawing, outjob: str | None = None) -> dict:
    """The Output Job model of the loaded project, or {"error": ...}.

    read_drawing(path) returns a Draftsman document's DraftsmanDoc (parse_pcb_dwf,
    cached). outjob (a file name or stem) narrows the OutJob details; the coverage and
    findings stay project-wide."""
    prj_path = project["prj_pcb_path"]
    try:
        documents = parse_prj_documents(prj_path)
        prj = parse_prj_pcb(prj_path)
        options = parse_prj_options(prj_path)
    except (OSError, UnicodeDecodeError) as error:
        return {"error": "project_file_unreadable",
                "message": f"{Path(prj_path).name}: {type(error).__name__}: {error}"}
    context = _Context(project, prj, options, documents, read_drawing)
    shown = context.jobs
    if outjob:
        wanted = outjob.strip().lower()
        shown = [job for job in context.jobs
                 if job.file.lower() == wanted or Path(job.file).name.lower() == wanted
                 or Path(job.file).stem.lower() == wanted]
        if not shown:
            return {"error": "outjob_not_found",
                    "message": f"No OutJob '{outjob}' in the project.",
                    "outjobs": [job.file for job in context.jobs]}
    findings: list[dict] = []
    for runner in _RUNNERS:
        findings.extend(runner(context))
    findings.sort(key=lambda f: (_SEVERITY_RANK[f["severity"]], _CHECK_RANK[f["check"]],
                                 -f["count"]))
    result: dict = {
        "project": project.get("name"),
        "prj_pcb": Path(prj_path).name,
        "outjobs": [job.summary() for job in context.jobs],
        "coverage": _coverage(context),
        "summary": _summary(findings),
        "findings": findings,
        "not_checked": context.not_checked,
        "details": [_detail(context, job) for job in shown],
        "notes": [
            "Read from the saved .OutJob, .PrjPcb and design files, not from Altium: "
            "generate the OutJob (or run the Project Releaser) to see what Altium makes.",
            "An output is generated only by the containers it is connected to (state "
            "connected, with its order there); OutputEnabled in the file only mirrors the "
            "container selected in the editor.",
            "Release-managed containers are resolved to the project's Output Path, where "
            "Generate Content writes; the Project Releaser writes to its own release folder.",
            "Generated-file times are file modification times: on a fresh clone or copy "
            "they are the checkout time, not when Altium generated them; a document saved "
            f"less than {STALE_TOLERANCE_S} s after the newest output is not counted.",
            "Container folders and PDF file names written as expressions are resolved from "
            "quoted text, ProjectName and project parameters; one using CurrentDate, "
            "CurrentTime or another name only Altium resolves: such a PDF is not checked, "
            "and such a folder is checked through its base folder (scope).",
            "Coverage and findings count the OutJobs the project lists; an unlisted OutJob "
            "is shown in details only.",
        ],
    }
    return result


def page_output_jobs(model: dict, offset: int, limit: int, example_limit: int) -> dict:
    """One page of OutJob details; coverage, findings (each with at most example_limit
    examples) and notes go with the first page."""
    offset = max(0, int(offset or 0))
    details = model["details"]
    if offset:
        keep = ("project", "prj_pcb", "outjobs")
        result = {key: model[key] for key in keep if key in model}
    else:
        result = {key: value for key, value in model.items() if key != "details"}
        page = []
        for finding in model["findings"]:
            finding = dict(finding)
            if len(finding["examples"]) > example_limit:
                finding["examples"] = finding["examples"][:example_limit]
                finding["examples_shown"] = example_limit
            page.append(finding)
        result["findings"] = page
    result["details_total"] = len(details)
    result["offset"] = offset
    result["has_more"] = offset + limit < len(details)
    if result["has_more"]:
        result["next_offset"] = offset + limit
    result["details"] = details[offset:offset + limit]
    return result


def draftsman_outputs(project: dict) -> list[dict]:
    """The Draftsman (PCBDrawing) outputs of the OutJobs the project lists, for the tools
    that check the drawings themselves: [{outjob, output, drawing (its path; None when
    the default source is ambiguous or the file is not found), variant, connected}].
    [] when the .PrjPcb cannot be read."""
    prj_path = project["prj_pcb_path"]
    try:
        documents = parse_prj_documents(prj_path)
        prj = parse_prj_pcb(prj_path)
        options = parse_prj_options(prj_path)
    except (OSError, UnicodeDecodeError):
        return []
    context = _Context(project, prj, options, documents, lambda path: None)
    return [
        {"outjob": out.job.file, "output": out.output.name or out.output.type,
         "drawing": _drawing_path(context, out), "variant": out.variant,
         "connected": out.connected}
        for out in context.outputs if out.output.type in DRAWING_TYPES
    ]


def variant_outputs(project: dict, read_drawing) -> dict:
    """The BOM, pick-and-place and assembly-drawing outputs of the OutJobs the project
    lists, with the variant each one uses, for compare_variants' documentation check:
    {"outputs": [{role, outjob, output, type, variant, variant_from, connected}],
    "outjobs": n}, or {"error": ...}. An assembly drawing's variant is a Draftsman
    drawing's own when it has one (what a release prints), else the OutJob's."""
    prj_path = project["prj_pcb_path"]
    try:
        documents = parse_prj_documents(prj_path)
        prj = parse_prj_pcb(prj_path)
        options = parse_prj_options(prj_path)
    except (OSError, UnicodeDecodeError) as error:
        return {"error": "project_file_unreadable",
                "message": f"{Path(prj_path).name}: {type(error).__name__}: {error}"}
    context = _Context(project, prj, options, documents, read_drawing)
    rows = []
    for out in context.outputs:
        role = next((name for name in ("bom", "pick_and_place", "assembly_drawing")
                     if out.output.type in ROLES[name]), None)
        if role is None:
            continue
        variant, source = out.variant, out.variant_from
        if role == "assembly_drawing":
            if _is_assembly_drawing(context, out) is False:
                continue
            drawn = _drawing_variant(context, out)
            if drawn != variant:
                variant, source = drawn, "drawing"
        rows.append({"role": role, "outjob": out.job.file,
                     "output": out.output.name or out.output.type, "type": out.output.type,
                     "variant": variant, "variant_from": source,
                     "connected": out.connected})
    return {"outputs": rows, "outjobs": len(context.listed_jobs())}


def output_locations(project: dict) -> dict:
    """Where the OutJobs the project lists generate their files, for verify_outputs:
    {"outputs": [{outjob, output, type, variant, document, sources, settings, folder,
    exact}], "output_path": the resolved Output Path folder, "bomdocs": the project's
    .BomDoc paths, "outjobs": n}, or {"error": ...}. One row per output and
    folder-structure container it is connected to; sources are the design documents it is
    generated from (as the staleness check of get_output_jobs reads them); folder is the
    container folder (its output sub-folder is not added: Altium names it after the
    output type's display name, so the caller looks below it). exact False: the
    container folder is an expression only Altium resolves (CurrentDate, ...), and
    folder is its base, where each generation gets a folder of its own."""
    prj_path = project["prj_pcb_path"]
    try:
        documents = parse_prj_documents(prj_path)
        prj = parse_prj_pcb(prj_path)
        options = parse_prj_options(prj_path)
    except (OSError, UnicodeDecodeError) as error:
        return {"error": "project_file_unreadable",
                "message": f"{Path(prj_path).name}: {type(error).__name__}: {error}"}
    context = _Context(project, prj, options, documents, lambda path: None)
    rows = []
    for out in context.outputs:
        for index in sorted(out.output.containers):
            container = out.job.job.container(index) if out.job.job else None
            if container is None or container.type != "GeneratedFiles":
                continue
            path = _container_path(context, container)
            if path is None:
                continue
            settings, _ = _settings(out.output)
            rows.append({"outjob": out.job.file, "output": out.output.name or out.output.type,
                         "type": out.output.type, "variant": out.variant,
                         "document": out.output.document, "sources": context.sources(out),
                         "settings": settings, "folder": path["resolved"],
                         "exact": not path.get("container_folder_unresolved")})
    return {"outputs": rows, "output_path": str(context.resolve(context.output_path)),
            "bomdocs": list(context.bomdocs), "outjobs": len(context.listed_jobs())}


# ---------- shared state ----------

@dataclass
class _Job:
    file: str                  # relative to the project folder
    path: str
    listed: bool
    job: OutJob | None = None
    error: str | None = None

    def summary(self) -> dict:
        row: dict = {"file": self.file, "listed": self.listed}
        if self.job is None:
            row["error"] = self.error
            return row
        row["outputs"] = len(self.job.outputs)
        row["connected"] = sum(1 for o in self.job.outputs if o.containers)
        return row


@dataclass
class _Out:
    """An output with what the checks need."""
    job: _Job
    output: OutJobOutput
    variant: str
    variant_from: str          # "output" | "job"

    @property
    def connected(self) -> bool:
        return bool(self.output.containers)

    def label(self) -> dict:
        return {"outjob": self.job.file, "output": self.output.name or self.output.type,
                "type": self.output.type}


@dataclass
class _Board:
    path: str
    error: str | None = None
    stack: str = ""
    overlays: dict[str, bool] = field(default_factory=dict)
    origin: tuple[float, float] | None = None
    blind_buried_pairs: int = 0
    flex: bool = False
    parts: Counter = field(default_factory=Counter)
    _cutouts: int | None = None

    def cutouts(self) -> int | None:
        if self.error:
            return None
        if self._cutouts is None:
            try:
                with olefile.OleFileIO(self.path) as ole:
                    available = {"/".join(s) for s in ole.listdir()}
                    stream = next((s for s in ("ShapeBasedRegions6/Data", "Regions6/Data")
                                   if s in available), None)
                    data = ole.openstream(stream).read() if stream else b""
            except Exception:  # an unreadable board leaves the check undone
                return None
            count = 0
            for _, _, head in _region_heads(data):
                if head is not None and (head.properties.get("ISBOARDCUTOUT", "").upper() == "TRUE"
                                         or head.properties.get("KIND") == "3"):
                    count += 1
            self._cutouts = count
        return self._cutouts


def _read_board(path: str) -> _Board:
    board = _Board(path)
    try:
        with olefile.OleFileIO(path) as ole:
            available = {"/".join(s) for s in ole.listdir()}
            records = read_text_records(ole.openstream("Board6/Data").read())
            components = (read_text_records(ole.openstream("Components6/Data").read())
                          if "Components6/Data" in available else [])
    except Exception as error:  # not an OLE file, or no Board6 stream
        board.error = f"{type(error).__name__}: {error}"
        return board
    info = decode_board(records)
    board.stack = info.stackup_source
    ids = {layer.v7_id for layer in info.layers}
    if info.stackup_source == "v9":
        board.overlays = {"top": TOP_OVERLAY_ID in ids, "bottom": BOTTOM_OVERLAY_ID in ids}
    if info.origin_x is not None and info.origin_y is not None:
        board.origin = (info.origin_x, info.origin_y)
    board.blind_buried_pairs = sum(
        1 for pair in info.drill_pairs
        if not pair.back_drill and {pair.low.upper(), pair.high.upper()} != {"TOP", "BOTTOM"})
    board.flex = any(stack.get("is_flex") for stack in info.substacks)
    for record in components:
        side = record.get("LAYER", "").strip().upper()
        if side in ("TOP", "BOTTOM"):
            board.parts[side.lower()] += 1
    return board


class _Context:
    def __init__(self, project: dict, prj, options, documents, read_drawing):
        self.project = project
        self.prj_path = Path(project["prj_pcb_path"])
        self.folder = self.prj_path.parent
        self.prj = prj
        self.options = options
        self.read_drawing = read_drawing
        self.not_checked: list[dict] = []
        by_suffix: dict[str, list] = {}
        for doc in documents:
            suffix = PureWindowsPath(doc.raw).suffix.lower()
            by_suffix.setdefault(suffix, []).append(doc)
        self.documents = documents
        self.sheets = [d.path for d in by_suffix.get(".schdoc", [])]
        self.pcbs = [d.path for d in by_suffix.get(".pcbdoc", [])]
        self.drawings = [d.path for d in by_suffix.get(".pcbdwf", [])]
        self.bomdocs = [d.path for d in by_suffix.get(".bomdoc", [])]
        self.output_path = prj.output_path or f"Project Outputs for {self.prj_path.stem}"
        self.log_path = prj.log_folder_path or f"Project Logs for {self.prj_path.stem}"
        self.variants = {v.name for v in prj.variants}
        self.parameters = {name.lower(): value for name, value in prj.parameters.items()}
        self.jobs = self._find_jobs(by_suffix.get(".outjob", []))
        self.all_outputs: list[_Out] = []
        for job in self.jobs:
            if job.job is None:
                continue
            for output in job.job.outputs:
                variant, source = _variant(job.job, output)
                self.all_outputs.append(_Out(job, output, variant, source))
        # Coverage and the checks count the OutJobs the project lists; an unlisted one is
        # shown in the details only.
        self.outputs = [o for o in self.all_outputs if o.job.listed]
        self._board: _Board | None = None
        self._drawings: dict[str, object] = {}
        # Generated files per (OutJob path, container) and (OutJob path, output index).
        self.generated: dict[tuple[str, int], dict] = {}
        self.generated_outputs: dict[tuple[str, int], dict] = {}

    def _find_jobs(self, listed_docs) -> list[_Job]:
        jobs: list[_Job] = []
        seen: set[str] = set()
        for doc in listed_docs:
            key = os.path.normcase(os.path.abspath(doc.path))
            if key in seen:
                continue
            seen.add(key)
            jobs.append(self._load(doc.raw, doc.path, True))
        for path in _unlisted_candidates(self.folder):
            key = os.path.normcase(os.path.abspath(path))
            if key in seen:
                continue
            seen.add(key)
            jobs.append(self._load(os.path.relpath(path, self.folder), str(path), False))
        return jobs

    def listed_jobs(self) -> list[_Job]:
        return [job for job in self.jobs if job.listed and job.job is not None]

    @staticmethod
    def _load(file: str, path: str, listed: bool) -> _Job:
        job = _Job(file=file, path=path, listed=listed)
        if not Path(path).is_file():
            job.error = "missing on disk"
            return job
        try:
            job.job = parse_out_job(path)
        except (OSError, ValueError) as error:
            job.error = f"{type(error).__name__}: {error}"
        return job

    def board(self) -> _Board | None:
        """The project's (first) PcbDoc, read lightly; None without one."""
        if not self.pcbs:
            return None
        if self._board is None:
            self._board = _read_board(self.pcbs[0])
        return self._board

    def drawing(self, path: str):
        """The DraftsmanDoc at path, or None when it cannot be read."""
        if path not in self._drawings:
            try:
                self._drawings[path] = self.read_drawing(path)
            except Exception:  # reported through not_checked
                self._drawings[path] = None
        return self._drawings[path]

    def resolve(self, raw: str) -> Path:
        """A path as written: absolute, or relative to the project folder."""
        text = raw.replace("/", "\\")
        if PureWindowsPath(text).is_absolute() or text.startswith("\\\\"):
            return Path(text)
        return self.folder / text

    def document(self, raw: str) -> tuple[str | None, bool]:
        """(path, listed) of an output's data source file: the project's document of that
        path or file name, else the file next to the project (None when not found)."""
        wanted = raw.replace("/", "\\").lower()
        for doc in self.documents:
            if doc.raw.lower() == wanted or PureWindowsPath(doc.raw).name.lower() == wanted:
                return doc.path, True
        path = self.resolve(raw)
        return (str(path), False) if path.is_file() else (None, False)

    def sources(self, out: _Out) -> list[str]:
        """The design documents an output is generated from."""
        output = out.output
        if output.document and not _generic(output.document):
            path, _ = self.document(output.document)
            return [path] if path else []
        if output.type in DRAWING_TYPES:
            return self.drawings
        if output.document == "[ActiveBOM Document]":
            return self.bomdocs + self.sheets
        if output.type in PCB_TYPES:
            return self.pcbs
        if output.type in SCH_TYPES:
            return self.sheets
        return self.sheets + self.pcbs


def _unlisted_candidates(folder: Path) -> list[Path]:
    """.OutJob files in the project folder and one folder down, skipping folders that hold
    a project of their own."""
    found: list[Path] = []
    try:
        entries = sorted(folder.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return found
    for entry in entries:
        if entry.is_file() and entry.suffix.lower() == ".outjob":
            found.append(entry)
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith((".", "__")) or \
                entry.name.lower() == "history":
            continue
        try:
            children = sorted(entry.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        if any(child.suffix.lower() == ".prjpcb" for child in children):
            continue
        found.extend(child for child in children
                     if child.is_file() and child.suffix.lower() == ".outjob")
    return found


def _variant(job: OutJob, output: OutJobOutput) -> tuple[str, str]:
    """The variant an output uses: its own (written when the job picks a variant per
    output) or the job's."""
    if output.variant:
        return output.variant, "output"
    return job.variant or NO_VARIATIONS, "job"


def _generic(source: str) -> bool:
    """A data source naming no file: [Project Physical Documents], [ActiveBOM Document],
    [Project], All SCH Documents ..."""
    return source.startswith("[") or source in GENERIC_SOURCES


def _same_variant(a: str, b: str) -> bool:
    def norm(name: str) -> str:
        return NO_VARIATIONS if name in ("", NO_VARIATIONS, "[All Variants]") else name
    return norm(a) == norm(b)


def _finding(check: str, severity: str, issue: str, examples: list[dict], fix: str,
             **fields) -> dict:
    finding = {"check": check, "severity": severity, "count": len(examples), "issue": issue}
    finding.update(fields)
    finding["fix"] = fix
    if check in KB:
        finding["source"] = KB[check]
    finding["examples"] = examples
    return finding


def _summary(findings: list[dict]) -> dict:
    severities = Counter(f["severity"] for f in findings)
    by_check: dict[str, str] = {}
    for finding in findings:
        worst = by_check.get(finding["check"])
        if worst is None or _SEVERITY_RANK[finding["severity"]] < _SEVERITY_RANK[worst]:
            by_check[finding["check"]] = finding["severity"]
    return {
        "severity_counts": {level: severities.get(level, 0)
                            for level in ("error", "warning", "info")},
        "by_check": {check: by_check[check] for check in CHECKS if check in by_check},
    }


def utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bool(value: str):
    return {"True": True, "False": False, "1": True, "0": False}.get(value, value)


# ---------- the details of one OutJob ----------

def _records(output: OutJobOutput, kind: str):
    return [record for record in output.records if record.record == kind]


def _settings(output: OutJobOutput) -> tuple[dict, int]:
    """(mapped settings, number of other setup keys) of an output."""
    settings: dict = {}
    other = 0
    for record in output.records:
        # Only the record of the output's own type: a leftover of another kind (a DrillView
        # on a BOM output) is counted, not mapped.
        mapping = (SETTING_KEYS.get(record.record, {})
                   if RECORD_FOR_TYPE.get(output.type) == record.record else {})
        for key, value in record.values.items():
            if key in mapping:
                settings[mapping[key]] = {"True": True, "False": False}.get(value, value)
            elif key not in ("Record", "DocumentPath") and not (
                    record.record == "ERCReportView"
                    and key.startswith(("ERCType", "ERCConnectionMatrix"))):
                other += 1
        if record.record == "GerberView" and mapping:
            settings["mechanical_layers_plotted"] = sorted(_mechanical_layers(record.values))
        if not record.record:
            other += 1
    if output.type == "ODB":
        settings["origin"] = "Relative (ODB++ always uses the relative origin)"
    return settings, other


def _layer_set(value: str) -> set[int]:
    """V7 layer ids of a SerializeLayerHash value (id~1 entries)."""
    ids = set()
    for part in value.split(","):
        layer, sep, flag = part.partition("~")
        if sep and layer.strip().isdigit() and flag.strip() == "1":
            ids.add(int(layer))
    return ids


def _mechanical_layers(values: dict[str, str]) -> set[int]:
    """Mechanical layers a GerberView plots (Plot.Set) or adds to every plot
    (AddToAllPlots.Set). PlotMechanicalNPlot keys belong to ODBView: no sampled GerberView
    has them."""
    ids = _layer_set(values.get("Plot.Set", "")) | _layer_set(values.get("AddToAllPlots.Set", ""))
    return {i - 0x01020000 for i in ids if i in _MECHANICAL_IDS}


def _evaluate(expression: str, context: _Context) -> str | None:
    """The folder name an '=' expression gives: quoted text, ProjectName (the .PrjPcb's
    name) and project parameters joined by '+'. None when it uses anything else, such as
    CurrentDate or CurrentTime, which Altium resolves each time it generates."""
    text, pos, parts, want_value = expression[1:], 0, [], True
    while pos < len(text):
        match = _EXPRESSION_TOKEN.match(text, pos)
        if match is None or match.end() == pos:
            return None
        pos = match.end()
        literal, name, plus = match.groups()
        if plus:
            if want_value:
                return None
            want_value = True
            continue
        if not want_value:
            return None
        want_value = False
        if literal is not None:
            parts.append(literal)
        elif name.lower() == "projectname":
            parts.append(context.prj_path.stem)
        elif name.lower() in context.parameters:
            parts.append(context.parameters[name.lower()])
        else:
            return None
    return "".join(parts) if parts and not want_value else None


def _pdf_name(context: _Context, container) -> tuple[str, str | None]:
    """(file name, expression only Altium resolves) of a PDF container: its special file
    name (OutputFileNameSpecial, an expression) where one is written, as every corpus PDF
    found next to such a setting is named; else OutputFileName."""
    special = container.publish.get("OutputFileNameSpecial", "")
    if not special:
        return container.publish.get("OutputFileName", ""), None
    name = _evaluate(special, context) if special.startswith("=") else special
    return (name, None) if name else ("", special)


def _container_path(context: _Context, container) -> dict | None:
    """Where a container writes: its base (release managed: the project's Output Path),
    container folder and output sub-folders, resolved against the project folder."""
    if container.type not in ("Publish", "GeneratedFiles", "Multimedia"):
        return None
    pub = container.publish
    managed = {"1": "release", "0": "manual"}.get(pub.get("ReleaseManaged", ""))
    base = context.output_path if managed != "manual" else pub.get("OutputBasePath", "")
    folder = context.resolve(base) if base else context.folder
    media = pub.get("OutputPathMedia", "")
    container_folder, unresolved = None, None
    if media == "[Media Name]":
        container_folder = container.name
    elif media.startswith("="):
        container_folder = _evaluate(media, context)
        unresolved = None if container_folder else media
    elif media.startswith("["):
        unresolved = media
    elif media:
        container_folder = media
    if container_folder:
        folder = folder / container_folder
    result: dict = {
        "managed": managed or pub.get("ReleaseManaged", "") or None,
        "base": base,
        "container_folder": container_folder or None,
        "output_folder": pub.get("OutputPathOutputer") or None,
        "resolved": str(folder),
    }
    if managed == "release":
        result["base_from"] = "Project Options > Output Path" + (
            "" if context.prj.output_path else " (default)")
    if pub.get("OutputPathOutputer") == "[Output Custom]":
        result["output_folder_value"] = pub.get("OutputPathOutputerValue", "")
    if media.startswith("="):
        result["container_folder_expression"] = media
    if unresolved:
        # 'resolved' then stops at the base folder.
        result["container_folder_unresolved"] = unresolved
    naming = {key: pub[key] for key in ("OutputFileName", "OutputFileNameMulti",
                                        "OutputFileNameSpecial") if pub.get(key)}
    if pub.get("UseOutputNameForMulti"):
        naming["UseOutputNameForMulti"] = _bool(pub["UseOutputNameForMulti"])
    if container.type == "Publish" and pub.get("OutputFileNameSpecial"):
        name, _ = _pdf_name(context, container)
        naming["pdf_file_name"] = name or None  # None: only Altium resolves it
    if naming:
        result["naming"] = naming
    generated = container.generated
    for key, label in (("TimestampFolder", "timestamp_folder"), ("AddToProject", "add_to_project")):
        if key in generated:
            result[label] = _bool(generated[key])
    if pub.get("OutputFilePath"):
        result["last_resolved_path"] = pub["OutputFilePath"]
    return result


def _detail(context: _Context, job: _Job) -> dict:
    detail: dict = {"file": job.file, "listed": job.listed}
    if job.job is None:
        detail["error"] = job.error
        return detail
    oj = job.job
    scope = oj.job.get("VariantScope", "")
    detail["variant"] = oj.variant or NO_VARIATIONS
    detail["variant_scope"] = {"1": "whole job"}.get(scope, f"raw {scope}" if scope else None)
    if scope != "1":
        detail["variant_scope_note"] = (
            "Only VariantScope 1 (a single variant for the whole OutJob) is verified; "
            "outputs show their own variant where one is written, else the job's.")
    detail["selected_container"] = oj.target
    if oj.job.get("CurrentConfigurationName"):
        detail["configuration"] = oj.job["CurrentConfigurationName"]
    names = {o.output.index: o for o in context.all_outputs if o.job is job}
    containers = []
    for container in oj.containers:
        row: dict = {"container": container.name, "kind": container.kind}
        linked = sorted(((o.containers[container.index], o) for o in oj.outputs
                         if container.index in o.containers), key=lambda t: (t[0], t[1].index))
        row["outputs"] = [f"{order}. {o.name or o.type}" for order, o in linked]
        path = _container_path(context, container) if linked else None
        if path:
            row["path"] = path
            generated = context.generated.get((job.path, container.index))
            if generated:
                row["generated"] = generated
        containers.append(row)
    detail["containers"] = containers
    by_category: dict[str, list] = {}
    for output in oj.outputs:
        out = names[output.index]
        row = {"output": output.name or output.type, "type": output.type,
               "state": "connected" if output.containers else "not connected"}
        if output.containers:
            row["containers"] = [
                f"{(oj.container(index).name if oj.container(index) else index)} #{order}"
                for index, order in sorted(output.containers.items())]
        row["document"] = output.document or "(default)"
        if output.type in VARIANT_TYPES or output.variant:
            row["variant"] = out.variant
            if out.variant_from == "output":
                row["variant_from"] = "output"
        settings, other = _settings(output)
        if settings:
            row["settings"] = settings
        if other:
            row["other_settings"] = other
        if output.unmapped:
            row["unmapped"] = output.unmapped
        if output.type in DRAWING_TYPES:
            path = _drawing_path(context, out)
            drawing = context.drawing(path) if path else None
            if drawing is not None:
                row["drawing"] = PureWindowsPath(path).name
                row["drawing_variant"] = drawing.document_variant or NO_VARIATIONS
                row["assembly_views"] = sum(1 for item in drawing.items
                                            if item.kind == "view" and item.view == "assembly view")
        generated = context.generated_outputs.get((job.path, output.index))
        if generated:
            row["generated"] = generated
        by_category.setdefault(output.category or "(none)", []).append(row)
    order = {name: rank for rank, name in enumerate(CATEGORY_ORDER)}
    detail["outputs_by_category"] = {
        name: by_category[name]
        for name in sorted(by_category, key=lambda n: (order.get(n, len(order)), n))}
    unmapped = {key: value for key, value in oj.header.items()
                if value and key not in ("Version", "Caption", "Description")}
    if oj.header.get("Version", "1.0") != "1.0":
        unmapped["Version"] = oj.header["Version"]
    for key in ("Name", "Description"):
        if oj.job.get(key):
            detail[key.lower()] = oj.job[key]
    job_keys = {"Name", "Description", "TargetOutputMedium", "VariantName", "VariantScope",
                "CurrentConfigurationName", "TargetPrinter", "PrinterOptions"}
    unmapped.update({key: value for key, value in oj.job.items()
                     if key not in job_keys and not key.startswith("PcbPrintPreferences")})
    for section, values in oj.other.items():
        if values:
            unmapped[f"[{section}]"] = values
    if unmapped:
        detail["unmapped"] = unmapped
    if oj.encoding != "utf-8":
        detail["encoding"] = oj.encoding
    return detail


# ---------- coverage and gaps ----------

def _role(context: _Context, role: str) -> tuple[list[_Out], list[_Out]]:
    """(connected, not connected) outputs of a role."""
    outs = [o for o in context.outputs if o.output.type in ROLES[role]]
    if role == "assembly_drawing":
        outs = [o for o in outs if _is_assembly_drawing(context, o) is not False]
    return [o for o in outs if o.connected], [o for o in outs if not o.connected]


def _drawing_path(context: _Context, out: _Out) -> str | None:
    if out.output.document and not _generic(out.output.document):
        return context.document(out.output.document)[0]
    return context.drawings[0] if len(context.drawings) == 1 else None


def _is_assembly_drawing(context: _Context, out: _Out) -> bool | None:
    """Assembly Drawings outputs are; a Draftsman output is when its drawing has an
    assembly view (None: the drawing cannot be read)."""
    if out.output.type != "PCBDrawing":
        return True
    path = _drawing_path(context, out)
    doc = context.drawing(path) if path else None
    if doc is None:
        return None
    return any(item.kind == "view" and item.view == "assembly view" for item in doc.items)


def _labels(outs: list[_Out]) -> list[str]:
    return [f"{o.job.file}: {o.output.name or o.output.type} ({o.output.type})" for o in outs]


def _coverage(context: _Context) -> dict:
    coverage: dict = {}
    for role in ROLES:
        connected, idle = _role(context, role)
        row: dict = {"status": "connected" if connected else
                     "present, not connected" if idle else "missing"}
        if connected:
            row["outputs"] = _labels(connected)
        if idle:
            row["not_connected"] = _labels(idle)
        coverage[role] = row
    formats = sorted({FABRICATION_FORMATS[o.output.type] for o in context.outputs
                      if o.connected and o.output.type in FABRICATION_FORMATS})
    coverage["fabrication_data"]["formats"] = formats
    coverage["nc_drill"]["drill_data_in"] = _drill_sources(context)
    return coverage


def _drill_sources(context: _Context) -> list[str]:
    """Connected outputs that carry drill data besides NC Drill: ODB++, IPC-2581, and
    Gerber X2 with plated or non-plated drill files."""
    found = []
    for o in context.outputs:
        if not o.connected:
            continue
        if o.output.type in INTELLIGENT_FORMATS:
            found.append(FABRICATION_FORMATS[o.output.type])
        elif o.output.type == "Gerber X2":
            values = {k: v for r in _records(o.output, "GerberX2View") for k, v in r.values.items()}
            if values.get("PlotPTHPairs") == "True" or values.get("PlotNPTHPairs") == "True":
                found.append("Gerber X2 drill files")
    return sorted(set(found))


def _gap(context: _Context, role: str, check: str, severity: str, what: str, kind: str,
         why: str) -> list[dict]:
    if not any(job.job and job.listed for job in context.jobs):
        return []  # one outjob_files finding says there is no OutJob
    connected, idle = _role(context, role)
    if connected:
        return []
    if idle:
        return [_finding(check, severity, f"{what} is in an OutJob but connected to no "
                         f"container, so it is never generated. {why}",
                         [o.label() for o in idle], FIXES["connect"])]
    return [_finding(check, severity, f"No OutJob of the project has {what}. {why}", [],
                     FIXES["gap"].format(kind=kind))]


def _outjob_files(context: _Context) -> list[dict]:
    findings = []
    missing = [j for j in context.jobs if j.listed and j.error == "missing on disk"]
    if missing:
        findings.append(_finding(
            "outjob_files", "error", "The project lists OutJobs that are not on disk.",
            [{"outjob": j.file} for j in missing], FIXES["outjob_missing"]))
    broken = [j for j in context.jobs if j.error and j.error != "missing on disk"]
    if broken:
        findings.append(_finding(
            "outjob_files", "error", "OutJobs that could not be read.",
            [{"outjob": j.file, "error": j.error} for j in broken], FIXES["outjob_unreadable"]))
    unlisted = [j for j in context.jobs if not j.listed]
    if unlisted:
        findings.append(_finding(
            "outjob_files", "info", "OutJob files in the project folder that the project does "
            "not list: they are not in the Projects panel or the Project Releaser.",
            [{"outjob": j.file} for j in unlisted], FIXES["outjob_unlisted"]))
    if not any(j.listed and j.job for j in context.jobs):
        findings.append(_finding(
            "outjob_files", "warning", "The project lists no readable OutJob: outputs are "
            "made by hand or by another project, and nothing here can be checked for gaps.",
            [], "Add an OutJob (File > New > Output Job File, or copy one of Altium's "
                "templates from Documents\\Altium\\<version>\\OutputJobs), save it in the "
                "project and connect its outputs to containers."))
    return findings


def _has_board(context: _Context) -> bool:
    return bool(context.pcbs)


def _no_fabrication_data(context):
    if not _has_board(context):
        return []
    return _gap(context, "fabrication_data", "no_fabrication_data", "warning",
                "a fabrication data output (Gerber, Gerber X2, ODB++ or IPC-2581)",
                "Fabrication", "Without it the board cannot be ordered from this project's "
                "outputs.")


def _no_nc_drill(context):
    if not _has_board(context):
        return []
    gerbers = [o for o in context.outputs
               if o.connected and o.output.type in ("Gerber", "Gerber X2")]
    connected, idle = _role(context, "nc_drill")
    if not gerbers or connected or _drill_sources(context):
        return []
    issue = ("Gerber artwork is generated without drill data: no NC Drill output, and no "
             "ODB++, IPC-2581 or Gerber X2 drill files.")
    if idle:
        issue += " An NC Drill output exists but is connected to no container."
    return [_finding("no_nc_drill", "error", issue,
                     [o.label() for o in gerbers + idle], FIXES["no_nc_drill"])]


def _no_pick_and_place(context):
    if not _has_board(context):
        return []
    return _gap(context, "pick_and_place", "no_pick_and_place", "warning",
                "a pick-and-place output", "Assembly",
                "The assembler then has to build the placement file from the board.")


def _no_assembly_drawing(context):
    if not _has_board(context):
        return []
    prints = [o for o in context.outputs if o.connected and o.output.type == "PCB Print"]
    findings = _gap(context, "assembly_drawing", "no_assembly_drawing",
                    "info" if prints else "warning",
                    "an assembly drawing (Assembly Drawings, or a Draftsman drawing with an "
                    "assembly view)", "Assembly", "Assemblers and inspectors need it.")
    for finding in findings:
        if prints:
            finding["issue"] += (" PCB Print outputs are connected: if one is set up as an "
                                 "assembly drawing, this is covered.")
            finding["pcb_prints"] = _labels(prints)
    return findings


def _no_bom(context):
    return _gap(context, "bom", "no_bom", "warning", "a bill of materials output", "Report",
                "The assembly data is incomplete without it.")


def _no_erc(context):
    if not context.sheets:
        return []
    return _gap(context, "erc", "no_erc_validation", "warning",
                "an Electrical Rules Check validation output", "Validation",
                "Generating or releasing does not then fail on ERC errors.")


def _no_drc(context):
    if not _has_board(context):
        return []
    return _gap(context, "drc", "no_drc_validation", "warning",
                "a Design Rules Check validation output", "Validation",
                "Generating or releasing does not then fail on DRC violations.")


def _no_odb_ipc(context):
    if not _has_board(context):
        return []
    fab = [o for o in context.outputs if o.connected and o.output.type in FABRICATION_FORMATS]
    if not fab or any(o.output.type in INTELLIGENT_FORMATS for o in fab):
        return []
    board = context.board()
    if board is None or board.error:
        return []
    reasons, severity = [], "info"
    if board.flex:
        reasons.append("the stackup has flex regions (rigid-flex)")
        severity = "warning"
    if board.blind_buried_pairs:
        reasons.append(f"{board.blind_buried_pairs} blind or buried drill pair(s) are defined")
    if not reasons:
        return []
    return [_finding(
        "no_odb_ipc2581", severity,
        "The fabrication data is Gerber only while " + " and ".join(reasons) + ": ODB++ "
        "and IPC-2581 carry the stackup and drill spans, Gerber files do not.",
        [o.label() for o in fab], FIXES["no_odb_ipc2581"])]


# ---------- variants ----------

def _bom_pnp_variant(context):
    boms = [o for o in context.outputs if o.connected and o.output.type in ROLES["bom"]]
    pnps = [o for o in context.outputs if o.connected and o.output.type in ROLES["pick_and_place"]]
    if not boms or not pnps:
        return []
    examples = []
    for group, others, what in ((boms, pnps, "pick-and-place"), (pnps, boms, "BOM")):
        for out in group:
            if not any(_same_variant(out.variant, other.variant) for other in others):
                examples.append(out.label() | {
                    "variant": out.variant,
                    f"{what.lower()}_variants": sorted({o.variant for o in others})})
    if not examples:
        return []
    return [_finding(
        "bom_pnp_variant", "warning", "BOM and pick-and-place outputs use different variants: "
        "the assembler then places parts the BOM leaves out, or misses parts it lists.",
        examples, FIXES["bom_pnp_variant"])]


def _drawing_variant(context: _Context, out: _Out) -> str:
    """The variant an assembly drawing shows: a Draftsman drawing's own, else the OutJob's."""
    if out.output.type == "PCBDrawing":
        path = _drawing_path(context, out)
        doc = context.drawing(path) if path else None
        if doc is not None and doc.document_variant:
            return doc.document_variant
    return out.variant


def _assembly_drawing_variant(context):
    drawings, _ = _role(context, "assembly_drawing")
    if not drawings:
        return []
    shown = {_drawing_variant(context, o) for o in drawings}
    used = [o for o in context.outputs if o.connected
            and o.output.type in ROLES["bom"] | ROLES["pick_and_place"]]
    missing: dict[str, list] = {}
    for out in used:
        if not any(_same_variant(out.variant, v) for v in shown):
            missing.setdefault(out.variant, []).append(out)
    if not missing:
        return []
    examples = [{"variant": variant, "used_by": _labels(outs),
                 "assembly_drawings_show": sorted(shown)} for variant, outs in missing.items()]
    return [_finding(
        "assembly_drawing_variant", "warning", "A variant has BOM or pick-and-place outputs "
        "but no assembly drawing showing it.", examples, FIXES["assembly_drawing_variant"])]


def _draftsman_variant(context):
    findings = []
    views, documents = [], []
    unread = []
    for out in context.outputs:
        if not out.connected or out.output.type != "PCBDrawing":
            continue
        path = _drawing_path(context, out)
        doc = context.drawing(path) if path else None
        if doc is None:
            unread.append(out)
            continue
        drawing_variant = doc.document_variant or NO_VARIATIONS
        for item in doc.items:
            if item.kind != "view" or not item.variant or item.variant.startswith("Document"):
                continue
            if not _same_variant(item.variant, drawing_variant):
                views.append(out.label() | {
                    "drawing": PureWindowsPath(path).name, "page": item.page, "view": item.view,
                    "title": item.title, "view_variant": item.variant,
                    "drawing_variant": drawing_variant, "outjob_variant": out.variant})
        if not _same_variant(drawing_variant, out.variant):
            documents.append(out.label() | {"drawing": Path(path).name,
                                            "drawing_variant": drawing_variant,
                                            "outjob_variant": out.variant})
    if views:
        findings.append(_finding(
            "draftsman_variant", "warning", "Draftsman views with a variant of their own that "
            "overrides the drawing's: they keep it when generated or released, whatever the "
            "drawing or the OutJob says.",
            views, FIXES["draftsman_view"]))
    if documents:
        findings.append(_finding(
            "draftsman_variant", "info", "Draftsman drawings whose own variant differs from "
            "the variant the OutJob gives them; a release prints the drawing's variant.",
            documents, FIXES["draftsman_document"]))
    if unread:
        context.not_checked.append({
            "check": "draftsman_variant",
            "reason": "these Draftsman outputs' drawings could not be read (missing, ambiguous "
                      "default source or unreadable): " + "; ".join(_labels(unread))})
    return findings


# ---------- documents ----------

def _wrong_document(context):
    missing, outside, drawn, ambiguous = [], [], [], []
    pcb_names = {PureWindowsPath(p).name.lower() for p in context.pcbs}
    for out in context.outputs:
        doc = out.output.document
        if doc and not _generic(doc):
            path, listed = context.document(doc)
            if path is None:
                missing.append(out.label() | {"document": doc})
            elif not listed:
                outside.append(out.label() | {"document": doc})
        elif not doc and out.output.type in PCB_TYPES and len(context.pcbs) > 1:
            ambiguous.append(out.label())
        if out.connected and out.output.type == "PCBDrawing":
            path = _drawing_path(context, out)
            drawing = context.drawing(path) if path else None
            source = getattr(drawing, "source_pcb", None)
            if source and context.pcbs and not any(_same_file(source, p) for p in context.pcbs):
                same_name = PureWindowsPath(source).name.lower() in pcb_names
                drawn.append(out.label() | {"drawing": Path(path).name, "source_pcb": source,
                                            "same_name_elsewhere": same_name})
    findings = []
    if missing:
        findings.append(_finding(
            "wrong_document", "error", "Outputs whose data source is a file that is not on "
            "disk: they fail or are left out when generated.", missing,
            FIXES["missing_document"]))
    if outside:
        findings.append(_finding(
            "wrong_document", "warning", "Outputs whose data source is a file the project "
            "does not list (another board or a leftover copy).", outside,
            FIXES["not_in_project"]))
    if drawn:
        severe = any(not d["same_name_elsewhere"] for d in drawn)
        findings.append(_finding(
            "wrong_document", "warning" if severe else "info",
            "Draftsman outputs whose drawing was last updated from another PcbDoc than the "
            "project's (same_name_elsewhere: a board of that name in another folder, e.g. a "
            "copied project).", drawn, FIXES["drawing_source"]))
    if ambiguous:
        findings.append(_finding(
            "wrong_document", "info", "PCB outputs with the default data source in a project "
            f"with {len(context.pcbs)} PcbDocs.", ambiguous, FIXES["ambiguous_source"]))
    return findings


def _same_file(a: str, b: str) -> bool:
    if a.replace("/", "\\").lower().rstrip("\\") == b.replace("/", "\\").lower().rstrip("\\"):
        return True
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


# ---------- validation settings ----------

def _erc_settings_differ(context):
    findings = []
    project_levels = {}
    for key, value in context.options.erc_levels.items():
        match = re.fullmatch(r"Type(\d+)", key)
        if match and value.isdigit() and int(value) < len(LEVELS):
            project_levels[int(match.group(1))] = int(value)
    for out in context.outputs:
        for record in _records(out.output, "ERCReportView"):
            levels = []
            for key, value in record.values.items():
                match = re.fullmatch(r"ERCType(\d+)", key)
                if not match or not value.isdigit():
                    continue
                number, level = int(match.group(1)), int(value)
                if number == NO_ERROR_TYPE or number > len(ERC_CHECKS) or level >= len(LEVELS):
                    continue
                project = project_levels.get(number)
                default = project is None
                if default:
                    project = ERC_DEFAULTS.get(number)
                if project is None or project == level:
                    continue
                levels.append({"check": ERC_CHECKS[number - 1], "outjob": LEVELS[level],
                               "project": LEVELS[project] + (" (default)" if default else ""),
                               "outjob_is": "stricter" if level > project else "looser"})
            cells = _matrix_differences(record.values, context.options.connection_matrix)
            if not levels and not cells:
                continue
            example = out.label() | {"state": "connected" if out.connected else "not connected",
                                     "levels_differ": len(levels), "levels": levels[:EXAMPLE_LIMIT]}
            if cells:
                example["connection_matrix_cells_differ"] = len(cells)
                example["connection_matrix"] = cells[:10]
            findings.append(_finding(
                "erc_settings_differ", "warning" if out.connected else "info",
                "The ERC validation output keeps its own report levels, which differ from "
                "Project Options: generating and releasing report on these levels, not on the "
                "project's ('outjob_is' stricter: a higher level, so a release can fail on a "
                "violation the project's ERC passes; looser: a lower one, so the release "
                "misses what the project reports).", [example], FIXES["erc_settings_differ"]))
    return findings


def _matrix_differences(values: dict[str, str], project_rows: dict[str, str]) -> list[dict]:
    """Connection Matrix cells of an ERC output that differ from the project's (Altium's
    default matrix where the project writes none, leaving out the cells whose default is
    not known). One triangle when both matrices are symmetric, every cell otherwise."""
    letters = {"N": "No Report", "W": "Warning", "E": "Error"}
    size = len(MATRIX_LABELS)
    own = [values.get(f"ERCConnectionMatrixL{row + 1}", "") for row in range(size)]
    theirs = [project_rows.get(f"L{row + 1}", "") for row in range(size)]
    defaults = not any(theirs)
    if defaults:
        theirs = list(MATRIX_DEFAULT)
    whole = [m for m in (own, theirs) if all(len(line) == size for line in m)]
    symmetric = len(whole) == 2 and all(m[i][j] == m[j][i] for m in whole
                                        for i in range(size) for j in range(size))
    cells = []
    for row in range(size):
        for column, (a, b) in enumerate(zip(own[row], theirs[row] or MATRIX_DEFAULT[row])):
            if a == b or column >= size or (symmetric and column < row):
                continue
            if defaults and (min(row, column), max(row, column)) in MATRIX_DEFAULT_UNKNOWN:
                continue
            cells.append({"row": MATRIX_LABELS[row], "column": MATRIX_LABELS[column],
                          "outjob": letters.get(a, a), "project": letters.get(b, b)})
    return cells


# ---------- paths ----------

def _drive(path: str) -> str:
    return PureWindowsPath(path).drive.lower()


def _output_path(context):
    # abspath, not resolve(): resolve() turns a mapped network drive (or a subst drive) into
    # its UNC share or target, which then looks like another drive than the paths on it.
    project_drive = _drive(os.path.abspath(context.folder))
    rows = []
    paths = [("Project Options > Output Path", context.output_path, None),
             ("Project Options > ECO Log Path", context.log_path, None)]
    for job in context.listed_jobs():
        if job.job is None:
            continue
        for container in job.job.containers:
            if container.type in ("Publish", "GeneratedFiles", "Multimedia") and \
                    container.publish.get("ReleaseManaged") == "0":
                paths.append((f"container '{container.name}' base path",
                              container.publish.get("OutputBasePath", ""), job.file))
    severity = "warning"
    for setting, raw, outjob in paths:
        if not raw:
            continue
        absolute = context.resolve(raw)
        drive = _drive(str(absolute))
        if not PureWindowsPath(raw.replace("/", "\\")).is_absolute() and not raw.startswith("\\\\"):
            continue
        if drive == project_drive:
            continue
        # A drive letter is checked; a network share is not probed (it can hang).
        reachable = (None if drive.startswith("\\\\")
                     else Path(PureWindowsPath(str(absolute)).anchor).exists())
        row = {"setting": setting, "path": raw, "project_drive": project_drive.upper(),
               "reachable": reachable}
        if outjob:
            row["outjob"] = outjob
        rows.append(row)
        if reachable is False:
            severity = "error"
    if not rows:
        return []
    return [_finding(
        "output_path", severity, "Output or ECO log paths on another drive (or a network "
        "share) than the project: the Project Releaser can then make an empty release, and an "
        "unreachable path stops generation.", rows, FIXES["output_path"])]


def _long_path(context):
    stem = max([len(Path(p).stem) for p in context.pcbs] + [len(context.prj_path.stem)])
    rows = []
    for job in context.listed_jobs():
        if job.job is None:
            continue
        for container in job.job.containers:
            path = _container_path(context, container)
            if path is None:
                continue
            folder = os.path.abspath(path["resolved"])
            # A container folder Altium resolves when generating counts with the
            # expression's own length.
            unresolved = path.get("container_folder_unresolved", "")
            sub = len(unresolved) + 1 if unresolved else 0
            linked = [o for o in job.job.outputs if container.index in o.containers]
            if not linked:
                continue  # a container with no output writes nothing
            outputer = container.publish.get("OutputPathOutputer", "")
            name, unresolved_name = (_pdf_name(context, container)
                                     if container.type == "Publish" else ("", None))
            name = name or unresolved_name or ""
            if name:
                # A PDF goes straight into the container folder (no output sub-folder in
                # any sampled last-resolved path), under its own file name.
                estimate = len(folder) + sub + 1 + len(name) + (0 if Path(name).suffix else 4)
            else:
                if outputer == "[Output Type]":
                    sub += max((len(o.type) + 1 for o in linked), default=0)
                elif outputer == "[Output Name]":
                    sub += max((len(o.name) + 1 for o in linked), default=0)
                elif outputer == "[Output Custom]":
                    custom = container.publish.get("OutputPathOutputerValue", "")
                    sub += len(custom) + 1 if custom else 0
                estimate = len(folder) + sub + 1 + _FILE_NAME_ALLOWANCE + stem
            if estimate >= NEAR_MAX_PATH:
                rows.append({"outjob": job.file, "container": container.name, "folder": folder,
                             "folder_length": len(folder), "estimated_file_path_length": estimate})
    log = os.path.abspath(str(context.resolve(context.log_path)))
    if len(log) + 1 + _FILE_NAME_ALLOWANCE + stem >= NEAR_MAX_PATH:
        rows.append({"setting": "ECO Log Path", "folder": log, "folder_length": len(log),
                     "estimated_file_path_length": len(log) + 1 + _FILE_NAME_ALLOWANCE + stem})
    if not rows:
        return []
    severity = ("error" if any(r["estimated_file_path_length"] >= MAX_PATH for r in rows)
                else "warning")
    return [_finding(
        "long_path", severity, f"Output folders whose file paths reach or come near Windows' "
        f"{MAX_PATH}-character limit (estimated with the output sub-folder and a "
        f"{_FILE_NAME_ALLOWANCE}-character file name plus the board name; a PDF with its own "
        "file name): Gerber and other outputs then fail ('File buffer is not initialized').",
        rows, FIXES["long_path"])]


# ---------- the board against the fabrication outputs ----------

def _fabrication_origin(context):
    gerbers = [o for o in context.outputs if o.connected and o.output.type == "Gerber"]
    drills = [o for o in context.outputs if o.connected and o.output.type == "NC Drill"]
    odbs = [o for o in context.outputs if o.connected and o.output.type == "ODB"]

    def origin(out: _Out, kind: str) -> str | None:
        values = {k: v for r in _records(out.output, kind) for k, v in r.values.items()}
        return values.get("OriginPosition")

    board = context.board()
    at_absolute = board is not None and board.origin is not None and board.origin == (0.0, 0.0)
    if at_absolute:
        return []  # the relative origin is the absolute origin: no offset either way
    origin_note = ("the board's relative origin is not at the absolute origin"
                   if board is not None and board.origin is not None else
                   "the board's origin could not be read")
    findings = []
    absolute = [o for o in gerbers if origin(o, "GerberView") == "Absolute"]
    if odbs and absolute:
        findings.append(_finding(
            "fabrication_origin", "warning", "Gerber outputs referenced to the absolute "
            f"origin next to ODB++, which always uses the relative origin ({origin_note}): "
            "the two data sets are offset.", [o.label() for o in absolute + odbs],
            FIXES["fabrication_origin"]))
    # Gerber and NC Drill outputs are paired within an OutJob; across the project only
    # when no OutJob has both.
    paired = [(g, d) for g in gerbers for d in drills if g.job is d.job]
    if not paired:
        paired = [(g, d) for g in gerbers for d in drills]
    mismatched = [(g, d) for g, d in paired
                  if None not in (origin(g, "GerberView"), origin(d, "DrillView"))
                  and origin(g, "GerberView") != origin(d, "DrillView")]
    if mismatched:
        examples = []
        for out, kind in ([(g, "GerberView") for g, _ in mismatched]
                          + [(d, "DrillView") for _, d in mismatched]):
            example = out.label() | {"origin": origin(out, kind)}
            if example not in examples:
                examples.append(example)
        findings.append(_finding(
            "fabrication_origin", "error", "Gerber and NC Drill outputs use different origins "
            f"({origin_note}): the holes are offset from the copper.", examples,
            FIXES["drill_origin"]))
    return findings


def _board_note(context: _Context, check: str) -> _Board | None:
    board = context.board()
    if board is not None and board.error:
        context.not_checked.append({
            "check": check, "reason": f"the PcbDoc could not be read ({board.error})"})
        return None
    return board


def _overlay_not_in_stackup(context):
    fab = [o for o in context.outputs if o.connected and o.output.type in FABRICATION_FORMATS]
    if not fab:
        return []
    board = _board_note(context, "overlay_not_in_stackup")
    if board is None:
        return []
    if board.stack != "v9":
        context.not_checked.append({"check": "overlay_not_in_stackup",
                                    "reason": "legacy layer stack (no V9 stackup to look in)"})
        return []
    rows = []
    for side in ("top", "bottom"):
        if not board.overlays.get(side, True):
            rows.append({"layer": f"{side.title()} Overlay", "parts_on_side": board.parts[side]})
    if not rows:
        return []
    severe = any(r["layer"] == "Top Overlay" or r["parts_on_side"] for r in rows)
    return [_finding(
        "overlay_not_in_stackup", "warning" if severe else "info",
        "Overlay (silkscreen) layers missing from the layer stack: ODB++ export leaves them "
        "out and no Gerber can be plotted for them.", rows, FIXES["overlay_not_in_stackup"],
        outputs=_labels(fab))]


def _board_cutouts(context):
    gerbers = [o for o in context.outputs if o.connected and o.output.type == "Gerber"]
    if not gerbers:
        return []
    board = _board_note(context, "board_cutouts")
    if board is None:
        return []
    cutouts = board.cutouts()
    if cutouts is None:
        context.not_checked.append({"check": "board_cutouts",
                                    "reason": "the PcbDoc's region streams could not be read"})
        return []
    if not cutouts:
        return []
    rows = []
    for out in gerbers:
        values = {k: v for r in _records(out.output, "GerberView") for k, v in r.values.items()}
        rows.append(out.label() | {
            "mechanical_layers_plotted": sorted(_mechanical_layers(values)),
            "plot_board_profile": _bool(values.get("PlotBoardProfile", "False"))})
    bare = [r for r in rows
            if not r["mechanical_layers_plotted"] and r["plot_board_profile"] is not True]
    issue = (f"The board has {cutouts} board cutout(s); Gerber output does not carry board "
             "cutouts unless they are drawn on a mechanical layer that is plotted.")
    if bare:
        return [_finding("board_cutouts", "warning", issue + " These Gerber outputs plot no "
                         "mechanical layer and no board profile.", bare, FIXES["board_cutouts"])]
    return [_finding("board_cutouts", "info", issue + " These Gerber outputs plot mechanical "
                     "layers or the board profile: check the cutouts are drawn on a plotted "
                     "mechanical layer or that the fabricator reads them from the profile (not "
                     "verified).", rows, FIXES["board_cutouts"])]


# ---------- generated files ----------

def _walk(folder: Path) -> tuple[int, float | None, bool]:
    """(files, newest mtime, truncated) of the generated files under folder."""
    count, newest, truncated = 0, None, False
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if Path(name).suffix.lower() in _DESIGN_SUFFIXES:
                continue
            try:
                mtime = os.stat(os.path.join(root, name)).st_mtime
            except OSError:
                continue
            count += 1
            newest = mtime if newest is None or mtime > newest else newest
            if count >= _WALK_LIMIT:
                return count, newest, True
    return count, newest, truncated


def _mtimes(paths: list[str]) -> dict[str, float]:
    times = {}
    for path in paths:
        try:
            times[path] = os.stat(path).st_mtime
        except OSError:
            continue
    return times


def _age(newest: float, sources: dict[str, float]) -> dict:
    newer = sorted((t, p) for p, t in sources.items() if t > newest + STALE_TOLERANCE_S)
    row: dict = {"newest_output_utc": utc_iso(newest)}
    if sources:
        row["design_newest_utc"] = utc_iso(max(sources.values()))
    row["stale"] = bool(newer)
    if newer:
        row["documents_newer"] = [{"document": Path(p).name, "modified_utc": utc_iso(t)}
                                  for t, p in reversed(newer[-5:])]
    return row


def _generated(context: _Context) -> None:
    """Fill context.generated (per container) and context.generated_outputs (per output)."""
    context.generated = {}
    context.generated_outputs = {}
    project_folder = os.path.normcase(os.path.abspath(context.folder))
    for job in context.jobs:
        if job.job is None:
            continue
        outs = {o.output.index: o for o in context.all_outputs if o.job is job}
        for container in job.job.containers:
            path = _container_path(context, container)
            linked = [outs[o.index] for o in job.job.outputs if container.index in o.containers]
            if path is None or not linked:
                continue
            folder = Path(path["resolved"])
            sources = _mtimes(sorted({s for o in linked for s in context.sources(o)}))
            key = (job.path, container.index)
            if os.path.normcase(os.path.abspath(folder)) == project_folder:
                context.generated[key] = {"location": str(folder), "checked": False,
                                          "reason": "the project folder itself"}
                continue
            unresolved = path.get("container_folder_unresolved")
            if container.type == "Publish":
                name, unresolved_name = _pdf_name(context, container)
                if unresolved or unresolved_name:
                    what = (f"container folder {unresolved}" if unresolved
                            else f"file name {unresolved_name}")
                    context.generated[key] = {
                        "location": str(folder), "checked": False,
                        "reason": f"the {what} is resolved by Altium when it generates"}
                    continue
                if not name:
                    continue
                candidates = [folder / name]
                if not Path(name).suffix:
                    candidates.append(folder / (name + ".pdf"))
                file = next((c for c in candidates if c.is_file()), None)
                row: dict = {"location": str(candidates[-1] if file is None else file),
                             "exists": file is not None}
                if file is not None:
                    row |= _age(os.stat(file).st_mtime, sources)
                context.generated[key] = row
                continue
            if container.type != "GeneratedFiles":
                continue
            row = {"location": str(folder), "exists": folder.is_dir()}
            if unresolved:
                # Every generation's folder is under the base: if even the newest file there
                # predates the design, this container's outputs do too.
                row["scope"] = (f"the base folder: the container folder {unresolved} is "
                                "resolved by Altium when it generates")
            if folder.is_dir():
                count, newest, truncated = _walk(folder)
                row["files"] = count
                if truncated:
                    # The newest file may be among those not walked: no age is claimed.
                    row["files_truncated"] = True
                elif newest is not None:
                    row |= _age(newest, sources)
                outputer = container.publish.get("OutputPathOutputer", "")
                for out in linked if not unresolved else ():
                    sub = {"[Output Type]": out.output.type, "[Output Name]": out.output.name,
                           "[Output Custom]": container.publish.get("OutputPathOutputerValue", "")
                           }.get(outputer)
                    if not sub or not (folder / sub).is_dir():
                        continue
                    count, newest, truncated = _walk(folder / sub)
                    if newest is None or truncated:
                        continue
                    context.generated_outputs[(job.path, out.output.index)] = (
                        {"location": str(folder / sub), "files": count}
                        | _age(newest, _mtimes(context.sources(out))))
            context.generated[key] = row


def _stale_outputs(context):
    rows = []
    for job in context.listed_jobs():
        if job.job is None:
            continue
        for container in job.job.containers:
            generated = context.generated.get((job.path, container.index))
            if generated and generated.get("stale"):
                row = {"outjob": job.file, "container": container.name,
                       "location": generated["location"],
                       "newest_output_utc": generated["newest_output_utc"],
                       "documents_newer": generated["documents_newer"]}
                if generated.get("scope"):
                    row["scope"] = generated["scope"]
                outputs =[o.output.name or o.output.type for o in context.all_outputs
                           if o.job is job and context.generated_outputs.get(
                               (job.path, o.output.index), {}).get("stale")]
                if outputs:
                    row["stale_outputs"] = outputs
                rows.append(row)
    if not rows:
        return []
    return [_finding(
        "stale_outputs", "warning", "Generated outputs older than the design documents they "
        "come from: the newest generated file predates the last save of these documents.",
        rows, FIXES["stale_outputs"])]


def _not_connected(context):
    idle = [o for o in context.outputs if not o.connected]
    if not idle:
        return []
    return [_finding(
        "not_connected", "info", "Outputs connected to no container: no container generates "
        "them.", [o.label() for o in idle], FIXES["not_connected"])]


def _prepare(context):
    _generated(context)
    context.not_checked.extend([
        {"check": "release_folders",
         "reason": "the Project Releaser's release folders and Altium 365 release data are "
                   "not read; release-managed containers are resolved to the Output Path"},
        {"check": "output_contents",
         "reason": "the layers each output plots are not compared with the layers the board "
                   "uses (only mechanical layers, for cutouts), and generated files are not "
                   "opened"},
        {"check": "drc_settings",
         "reason": "Design Rules Check outputs store no settings in any sampled OutJob: they "
                   "run the board's rules (get_drc_report reads the last DRC report)"},
        {"check": "long_path_support",
         "reason": "whether Windows long paths are enabled (LongPathsEnabled) is not read"},
    ])
    if context.pcbs and len(context.pcbs) > 1:
        context.not_checked.append({
            "check": "board_checks", "reason": "the board checks read the first PcbDoc only"})
    return []


_RUNNERS = (
    _prepare, _outjob_files, _no_fabrication_data, _no_nc_drill, _no_pick_and_place,
    _no_assembly_drawing, _no_bom, _no_erc, _no_drc, _no_odb_ipc, _bom_pnp_variant,
    _assembly_drawing_variant, _wrong_document, _erc_settings_differ, _output_path,
    _long_path, _fabrication_origin, _overlay_not_in_stackup, _board_cutouts,
    _draftsman_variant, _stale_outputs, _not_connected,
)

__all__ = ["CHECKS", "CONTAINER_KINDS", "build_output_jobs", "page_output_jobs"]
