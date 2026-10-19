import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path, PureWindowsPath

try:
    from netlist_parser import parse_protel_netlist
except ImportError:
    from server.netlist_parser import parse_protel_netlist

try:
    from parsers.sch_doc import ComponentMeta, parse_sch_doc
except ImportError:
    from server.parsers.sch_doc import ComponentMeta, parse_sch_doc

try:
    from parsers.prj_pcb import parse_prj_pcb
except ImportError:
    from server.parsers.prj_pcb import parse_prj_pcb

try:
    from parsers.annotation import AnnotationEntry, parse_annotation
except ImportError:
    from server.parsers.annotation import AnnotationEntry, parse_annotation

# When bundled with PyInstaller (--onefile), data files are extracted to sys._MEIPASS.
# In normal Python execution, scripts live next to this file.
if getattr(sys, "frozen", False):
    SCRIPTS_DIR = Path(sys._MEIPASS) / "scripts"
else:
    SCRIPTS_DIR = Path(__file__).parent / "scripts"


def enrich_netlist(netlist: dict, components: dict[str, "ComponentMeta"],
                   refdes_to_sheet: dict[str, str] | None = None) -> dict:
    """Merge SchDoc component metadata into the .NET-derived netlist dict.

    Adds netlist["components"] = {refdes: {mpn, description, value, sheet, pins: {pin: {name, net}}}}.

    Handles subsheet fan-out: when Altium instantiates the same schematic sheet
    multiple times, the netlist compiler appends a letter suffix to each refdes
    (C1 → C1A, C1B, C1C). The SchDoc contains the bare refdes (C1); this function
    detects those suffixed variants and creates enriched entries for each one.
    """
    sheet_map = refdes_to_sheet or {}
    sheets: dict[str, dict[str, ComponentMeta]] = {}
    for refdes, meta in components.items():
        sheets.setdefault(sheet_map.get(refdes, ""), {})[refdes] = meta
    return enrich_netlist_sheets(netlist, list(sheets.items()))


def enrich_netlist_sheets(
    netlist: dict,
    sheets: list[tuple[str, dict[str, "ComponentMeta"]]],
    annotation: list["AnnotationEntry"] | None = None,
) -> dict:
    """Merge each sheet's component metadata into the netlist, see enrich_netlist.

    sheets holds (sheet name, {sheet designator: metadata}) per parsed sheet. The netlist
    names physical designators. On a board-annotated or multi-channel project these differ
    from the sheet's (logical) ones, and the project's .Annotation file maps each placed
    part, by sheet and UniqueId, to its physical designators (one per channel). Those
    parts are filed under their physical designators, with logical_refdes set when it
    differs. Parts the file does not list keep matching by sheet designator, onto netlist
    names that no annotated part took.
    """
    pin_to_net = netlist["pin_to_net"]
    by_uid: dict[str, list[AnnotationEntry]] = {}
    for annotation_entry in annotation or []:
        by_uid.setdefault(annotation_entry.component_uid, []).append(annotation_entry)

    result: dict = {}
    unannotated: list[tuple[str, str, ComponentMeta]] = []
    for sheet, components in sheets:
        for refdes, meta in components.items():
            physical = _physical_designators(by_uid, sheet, meta.unique_id)
            if not physical:
                unannotated.append((sheet, refdes, meta))
                continue
            for name in physical:
                entry = _make_enriched(meta, pin_to_net.get(name, {}))
                entry["sheet"] = sheet
                if name != refdes:
                    entry["logical_refdes"] = refdes
                result[name] = entry

    claimed = set(result)
    for sheet, refdes, meta in unannotated:
        # Direct match (most components, and already-suffixed subsheet refs)
        if refdes in pin_to_net and refdes not in claimed:
            entry = _make_enriched(meta, pin_to_net[refdes])
            entry["sheet"] = sheet
            result[refdes] = entry
            continue

        # Subsheet fan-out: look for refdesA, refdesB, ... in pin_to_net
        suffixed = [
            r for r in pin_to_net
            if r not in claimed
            and re.fullmatch(rf"{re.escape(refdes)}(?:[_.\-]?[A-Za-z]+\d*|[_.\-]\d+)+", r)
        ]
        if suffixed:
            for suffixed_ref in suffixed:
                entry = _make_enriched(meta, pin_to_net[suffixed_ref])
                entry["sheet"] = sheet
                result[suffixed_ref] = entry
            continue

        if refdes in claimed:
            # The netlist's part of this name is an annotated part from another sheet;
            # this one's physical designator is not known.
            logging.warning("%s on sheet %s is not in the .Annotation file and its "
                            "designator names another part", refdes, sheet)
            continue

        # No net connections (chassis fuses, heatsinks, mounting holes, etc.)
        # Still include so Claude can answer BOM questions about them.
        entry = _make_enriched(meta, {})
        entry["sheet"] = sheet
        result[refdes] = entry

    netlist["components"] = result
    return netlist


def _physical_designators(by_uid: dict[str, list["AnnotationEntry"]], sheet: str,
                          unique_id: str) -> list[str]:
    """Physical designators the .Annotation file gives a sheet's part, in file order."""
    names: list[str] = []
    if not unique_id:
        return names
    for annotation_entry in by_uid.get(unique_id, []):
        if annotation_entry.sheet and annotation_entry.sheet.lower() != sheet.lower():
            continue
        if annotation_entry.physical not in names:
            names.append(annotation_entry.physical)
    return names


def is_netlist_fresh(net_path: str | Path, sources: list[str | Path]) -> bool:
    """True when the .NET is at least as new as every source file on disk.

    Sources that cannot be stat'ed (listed in the .PrjPcb but missing) are skipped —
    Altium could not have compiled them either, and enrichment reports them as sheet
    errors. With no readable source at all the cache is not trusted.
    """
    net_mtime = Path(net_path).stat().st_mtime
    src_mtimes = []
    for src in sources:
        try:
            src_mtimes.append(Path(src).stat().st_mtime)
        except OSError:
            continue
    return bool(src_mtimes) and net_mtime >= max(src_mtimes)


def netlist_sources(project_path: str, sheet_paths: list[str],
                    annotation_paths: list[str] | None = None) -> list[Path]:
    """Files whose saves make the .NET stale: the .PrjPcb, the sheets and .Annotation
    files it lists, and any other .SchDoc in those folders.

    The extra sheets cover a sheet added in Altium and saved before the project itself
    was saved — it is compiled into the .NET but not yet listed in the .PrjPcb on disk.
    They only drive freshness, never enrichment.
    """
    sources = [Path(project_path), *(Path(p) for p in sheet_paths)]
    folders = dict.fromkeys(p.parent for p in sources)
    for folder in folders:
        try:
            sources.extend(folder.glob("*.[Ss][Cc][Hh][Dd][Oo][Cc]"))
        except OSError:
            continue
    sources.extend(Path(p) for p in annotation_paths or [])
    return sources


def _sheet_paths_for(project_path: str) -> list[str]:
    """Schematic sheets listed in the .PrjPcb, wherever they live in the project tree."""
    if not Path(project_path).exists():
        return []
    try:
        return parse_prj_pcb(project_path).sheet_paths
    except Exception as e:
        logging.warning("Could not read sheet list from %s: %s", project_path, e)
        return []


def _annotation_paths_for(project_path: str) -> list[str]:
    """Board-level annotation (.Annotation) files listed in the .PrjPcb."""
    if not Path(project_path).exists():
        return []
    try:
        return parse_prj_pcb(project_path).annotation_paths
    except Exception as e:
        logging.warning("Could not read annotation files from %s: %s", project_path, e)
        return []


def _read_annotation(paths: list[str]) -> tuple[list["AnnotationEntry"], list[dict]]:
    """Entries of every readable .Annotation file, and an error per unreadable one."""
    entries: list[AnnotationEntry] = []
    errors: list[dict] = []
    for path in paths:
        try:
            entries.extend(parse_annotation(path))
        except FileNotFoundError:
            errors.append({"path": str(path), "error": "file not found"})
        except Exception as e:
            errors.append({"path": str(path), "error": f"{type(e).__name__}: {e}"})
    for error in errors:
        logging.warning("Annotation file not applied, %s: %s", error["path"], error["error"])
    return entries, errors


def _make_enriched(meta: "ComponentMeta", comp_pins: dict) -> dict:
    enriched_pins = {
        pin_num: {"name": meta.pins.get(pin_num, ""), "net": net}
        for pin_num, net in comp_pins.items()
    }
    return {
        "mpn": meta.mpn,
        "description": meta.description,
        "value": meta.value,
        "unique_id": meta.unique_id,
        "pins": enriched_pins,
    }


def _run_ps(script: str, **params: str) -> str:
    """Run a PowerShell script and return stdout. Raises on non-zero exit.

    All scripts in server/scripts/ must implement Win32 calls via C# inline
    with Add-Type — never use PowerShell scriptblock delegates for Win32 work.
    Scriptblock delegates break on PS 7+ but C# Add-Type works on all versions.
    See get_altium_status.ps1 as the reference pattern.
    """
    args = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(SCRIPTS_DIR / script)]
    for key, value in params.items():
        args += [f"-{key}", value]

    result = subprocess.run(args, capture_output=True, text=True, timeout=10)  # noqa: S603
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"{script} failed")
    return result.stdout.strip()


# Altium Designer 17 and older (DXP.exe) lead the title bar with the product, then the
# focused document's full path, then the project:
#   "Altium Designer (14.3) - C:\Work\Board\Power.SchDoc - Board.PrjPcb. Licensed to ..."
# The document group is greedy, so " - " inside a folder name stays part of the path.
_LEGACY_TITLE = re.compile(
    r"^Altium Designer[^-]*?\s-\s(?:(?P<document>.*)\s-\s)?"
    r"(?:.*?[\\/])?(?P<project>[^\\/]+?\.PrjPcb)(?=$|[\s.])",
    re.IGNORECASE,
)


def _parse_legacy_title(title: str) -> tuple[str, str]:
    """(project_file, document_path) from a DXP.exe title bar; ("", "") if it has no project."""
    m = _LEGACY_TITLE.match(title)
    if not m:
        return "", ""
    # "*" is not valid in a Windows file name, so a trailing " *" is an unsaved-changes marker.
    return m["project"], (m["document"] or "").rstrip(" *").strip()


def _find_project_near(document: str, project_file: str) -> str:
    """Path of project_file in the document's folder or the nearest parent folder holding it."""
    doc = Path(document)
    if not doc.is_absolute():
        return ""
    for folder in doc.parents:
        candidate = folder / project_file
        if os.path.isfile(candidate):
            return str(candidate)
    return ""


def _apply_legacy_title(status: dict, title: str) -> None:
    """Fill project_file, project_path and active_tab for Altium Designer 17 and older.

    get_altium_status.ps1 leaves the title of a DXP.exe window unparsed because its layout
    differs from X2.exe. The project is looked up from the focused document's folder
    instead of the X2 search under Public Documents\\Altium.
    """
    project_file, document = _parse_legacy_title(title)
    if not project_file:
        return
    status["project_file"] = project_file
    if document and PureWindowsPath(document).suffix:
        status["active_tab"] = PureWindowsPath(document).name
        status["project_path"] = _find_project_near(document, project_file)


def _classify_tab(tab: str) -> str:
    lower = tab.lower()
    if lower.endswith(".schdoc"):
        return "schematic"
    if lower.endswith(".pcbdoc"):
        return "pcb"
    return "other"


def validate_active_document(
    status: dict,
    project_root: str,
    prj_pcb_path: str,
    sheet_paths: list[str],
) -> str | dict:
    """Pure function — validate the active Altium tab against the loaded project.

    Returns the active tab filename (str) if valid, or a warning dict if not.
    """
    if not status.get("running"):
        return {
            "warning": "altium_not_running",
            "message": "Altium Designer is not running. Open Altium and try again.",
        }

    if status.get("warning") == "no_sheet_open":
        return {
            "warning": "no_sheet_open",
            "message": (
                "Altium is running but no active schematic sheet was detected. "
                "Open a schematic sheet in Altium, then try again."
            ),
        }

    # Check project matches
    if status.get("project_file"):
        loaded = os.path.basename(prj_pcb_path).lower()
        active = status["project_file"].lower()
        if active != loaded:
            return {
                "warning": "active_document_outside_project",
                "active_filename": status.get("active_tab", ""),
                "scoped_project": os.path.basename(project_root),
                "message": (
                    f"Altium has \"{status['project_file']}\" open, but the loaded project is "
                    f"\"{os.path.basename(prj_pcb_path)}\". Switch projects or call set_project_dir."
                ),
            }

    # Check tab belongs to this project's sheets
    active_tab = status.get("active_tab", "")
    sheet_names = [os.path.basename(p).lower() for p in sheet_paths]
    if active_tab.lower() not in sheet_names:
        return {
            "warning": "active_document_outside_project",
            "active_filename": active_tab,
            "scoped_project": os.path.basename(project_root),
            "message": (
                f"\"{active_tab}\" does not belong to project \"{os.path.basename(project_root)}\". "
                "Switch to a sheet in this project."
            ),
        }

    return active_tab


class AltiumClient:
    def __init__(self):
        self._netlist: dict | None = None

    def _enrich_from_schdocs(self, sheet_paths: list[str], netlist: dict,
                             annotation_paths: list[str] | None = None) -> None:
        """Parse each .PrjPcb-listed sheet and merge its components into the netlist.

        A sheet that fails to parse is recorded in netlist["sheet_errors"] and skipped,
        so one bad sheet never discards the components of the others. The project's
        .Annotation files map sheet designators to the netlist's physical ones; one that
        cannot be read is recorded in netlist["annotation_errors"].
        """
        sheets: list[tuple[str, dict]] = []
        sheet_errors: list[dict] = []
        for sch_path in sheet_paths:
            sheet_name = Path(sch_path).stem
            if not Path(sch_path).is_file():
                error = "file not found"
            else:
                try:
                    sheets.append((sheet_name, parse_sch_doc(str(sch_path))))
                    continue
                except Exception as e:
                    error = f"{type(e).__name__}: {e}"
            logging.warning("SchDoc enrichment failed for %s: %s", sch_path, error)
            sheet_errors.append({"sheet": sheet_name, "path": str(sch_path), "error": error})
        annotation, annotation_errors = _read_annotation(annotation_paths or [])
        enrich_netlist_sheets(netlist, sheets, annotation)
        netlist["sheet_errors"] = sheet_errors
        if annotation_errors:
            netlist["annotation_errors"] = annotation_errors

    def _load_enriched(self, net_path: str | Path, sheet_paths: list[str],
                       annotation_paths: list[str] | None = None) -> dict:
        """Parse a .NET file and enrich it from the given sheets. Always returns a new dict."""
        netlist = parse_protel_netlist(str(net_path))
        try:
            self._enrich_from_schdocs(sheet_paths, netlist, annotation_paths)
        except Exception as e:
            logging.warning("SchDoc enrichment failed: %s", e)
            netlist["components"] = {}
            netlist["sheet_errors"] = [{"sheet": "*", "path": "", "error": f"{type(e).__name__}: {e}"}]
        return netlist

    def get_status(self) -> dict:
        result = json.loads(_run_ps("get_altium_status.ps1"))
        if not result.get("running"):
            return result
        title = result.pop("window_title", "")
        if (result.get("process") or "").upper() == "DXP":
            _apply_legacy_title(result, title)
        result["tab_type"] = _classify_tab(result.get("active_tab", ""))
        if not result.get("project_file"):
            # Can't tell from the title bar whether no project is loaded or a project
            # is loaded but no sheet is active (Altium title just shows "Altium Designer"
            # in both cases). Treat as no_sheet_open — the fix is the same either way.
            result["warning"] = "no_sheet_open"
        return result

    def load_netlist_from_file(self, net_path: str, sheet_paths: list[str],
                               annotation_paths: list[str] | None = None) -> None:
        """Load netlist from an existing .NET file without triggering Altium automation."""
        self._netlist = self._load_enriched(net_path, sheet_paths, annotation_paths)

    def generate_netlist(self, project_path: str, sheet_paths: list[str] | None = None,
                         annotation_paths: list[str] | None = None) -> bool:
        """Load the project netlist, regenerating it through Altium when it is stale.

        sheet_paths and annotation_paths default to the sheets and .Annotation files listed
        in the .PrjPcb. Returns True when Altium regenerated the .NET, False when the
        existing file was fresh.
        """
        project_dir = Path(project_path).parent
        project_name = Path(project_path).stem
        if sheet_paths is None:
            sheet_paths = _sheet_paths_for(project_path)
        if annotation_paths is None:
            annotation_paths = _annotation_paths_for(project_path)

        def _find_net_file() -> Path | None:
            matches = list(project_dir.rglob(f"{project_name}.NET"))
            if not matches:
                matches = list(project_dir.rglob(f"{project_name}.net"))
            return matches[0] if matches else None

        netlist_path = _find_net_file()

        # Cache check — skip GUI automation if .NET is newer than all source files.
        if netlist_path is not None and netlist_path.exists():
            try:
                fresh = is_netlist_fresh(
                    netlist_path, netlist_sources(project_path, sheet_paths, annotation_paths)
                )
            except OSError as e:
                logging.warning("Cache check failed, regenerating: %s", e)
                fresh = False
            if fresh:
                logging.info("Netlist cache is fresh — skipping generation")
                try:
                    self._netlist = self._load_enriched(netlist_path, sheet_paths, annotation_paths)
                    return False  # cache hit — nothing regenerated
                except Exception as e:
                    logging.warning("Cached netlist unreadable, regenerating: %s", e)

        # Move the stale netlist aside so we can detect when Altium writes a fresh one.
        # It is restored if regeneration fails, so a closed or unresponsive Altium never
        # costs the user their only netlist.
        backup: Path | None = None
        if netlist_path is not None and netlist_path.exists():
            backup = netlist_path.with_name(netlist_path.name + ".bak")
            netlist_path.replace(backup)

        # Backup housekeeping is best-effort: a sync client or AV holding the .bak must
        # neither turn a successful regeneration into an error nor mask the real failure.
        try:
            regenerated = self._regenerate(sheet_paths, _find_net_file, annotation_paths)
        except BaseException:
            if backup is not None and backup.exists() and _find_net_file() is None:
                try:
                    backup.replace(netlist_path)
                except OSError as e:
                    logging.warning("Could not restore %s: %s", backup, e)
            raise
        if backup is not None:
            try:
                backup.unlink(missing_ok=True)
            except OSError as e:
                logging.warning("Could not remove %s: %s", backup, e)
        return regenerated

    def _regenerate(self, sheet_paths: list[str], _find_net_file,
                    annotation_paths: list[str] | None = None) -> bool:
        max_attempts = 3
        per_attempt_timeout = 15  # seconds per attempt

        for attempt in range(1, max_attempts + 1):
            logging.info("Generating netlist (attempt %d/%d)", attempt, max_attempts)
            try:
                _run_ps("generate_protel_netlist.ps1", Delay="50")
            except subprocess.TimeoutExpired:
                logging.warning("PS timed out on attempt %d, retrying...", attempt)
                continue
            except RuntimeError as e:
                if "focus" in str(e).lower() or "foreground" in str(e).lower():
                    logging.warning("Focus acquisition failed on attempt %d, retrying...", attempt)
                    continue
                raise

            start_time = time.time()
            while time.time() - start_time < per_attempt_timeout:
                found = _find_net_file()
                if found is not None and found.exists():
                    self._netlist = self._load_enriched(found, sheet_paths, annotation_paths)
                    return True  # PowerShell ran, netlist regenerated
                time.sleep(1)

            if attempt < max_attempts:
                logging.warning("Netlist not generated within %ds, retrying...", per_attempt_timeout)

        raise RuntimeError(
            f"Netlist not generated after {max_attempts} attempts — check Altium is responsive "
            f"and the project output directory exists."
        )
