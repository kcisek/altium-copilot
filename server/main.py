# server/main.py
import json
import logging
import math
import os
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Literal

vendor_dir = os.path.join(os.path.dirname(__file__), "vendor")
# When bundled with PyInstaller, vendor is embedded — no sys.path injection needed.
# Only inject when running from source and the vendor dir exists.
if os.path.exists(vendor_dir):
    sys.path.insert(0, vendor_dir)

import httpx
from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations

try:
    from altium import AltiumClient, is_netlist_fresh, netlist_sources
    from parsers.prj_pcb import parse_prj_pcb, VariantState
    from services.registry import read_registry, upsert_registry_entry, mark_xfn_exported
    from services.page_netlist import build_sheet_context, MAX_RESULT_SIZE_CHARS
    from parsers.pcb_doc import parse_pcb_doc
    from parsers.sch_doc import read_sch_doc_image
    from services.pcb_index import PcbIndex
    from services.pcb_rules import (
        BINARY_KINDS, NET_RULE_KINDS, ROOM_KIND, resolve_kind, rule_header,
        rule_values, stale_references,
    )
    from services.pcb_length import (
        check_length_rules, net_length, pad_to_pad, pair_lengths, xsignal_length,
    )
    from services.pcb_compliance import check_compliance
    from services.diff_pair_checks import check_diff_pairs as run_diff_pair_checks
    from services.constraints_review import build_constraints_review
    from services.impedance import (
        PROPAGATION_UNIT_NOTE, impedance_profiles, profile_refs,
    )
    from services.sch_pcb_sync import net_matching_options, run_sync_check
    from services.drc_results import (
        load_report, assess, summarize, other_sections, DrcLinker,
    )
    from parsers.pcb_doc import CLASS_KINDS
    from services.design_notes import (
        DesignNotesCache, NOTES_MAX_RESULT_SIZE_CHARS, build_design_notes,
        draftsman_documents, note_rows, resolve_document, sheet_notes_for_context,
    )
    from services.images import (
        MAX_IMAGE_BYTES, MAX_PASSTHROUGH_SIDE_PX, bmp_to_png, image_format, image_size,
    )
    from export import export_project, HIGH_FANOUT_THRESHOLD
except ImportError:
    from server.altium import AltiumClient, is_netlist_fresh, netlist_sources
    from server.parsers.prj_pcb import parse_prj_pcb, VariantState
    from server.parsers.pcb_doc import parse_pcb_doc
    from server.parsers.sch_doc import read_sch_doc_image
    from server.services.registry import read_registry, upsert_registry_entry, mark_xfn_exported
    from server.services.page_netlist import build_sheet_context, MAX_RESULT_SIZE_CHARS
    from server.services.pcb_index import PcbIndex
    from server.services.pcb_rules import (
        BINARY_KINDS, NET_RULE_KINDS, ROOM_KIND, resolve_kind, rule_header,
        rule_values, stale_references,
    )
    from server.services.pcb_length import (
        check_length_rules, net_length, pad_to_pad, pair_lengths, xsignal_length,
    )
    from server.services.pcb_compliance import check_compliance
    from server.services.diff_pair_checks import check_diff_pairs as run_diff_pair_checks
    from server.services.constraints_review import build_constraints_review
    from server.services.impedance import (
        PROPAGATION_UNIT_NOTE, impedance_profiles, profile_refs,
    )
    from server.services.sch_pcb_sync import net_matching_options, run_sync_check
    from server.services.drc_results import (
        load_report, assess, summarize, other_sections, DrcLinker,
    )
    from server.parsers.pcb_doc import CLASS_KINDS
    from server.services.design_notes import (
        DesignNotesCache, NOTES_MAX_RESULT_SIZE_CHARS, build_design_notes,
        draftsman_documents, note_rows, resolve_document, sheet_notes_for_context,
    )
    from server.services.images import (
        MAX_IMAGE_BYTES, MAX_PASSTHROUGH_SIDE_PX, bmp_to_png, image_format, image_size,
    )
    from server.export import export_project, HIGH_FANOUT_THRESHOLD

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")


def _manifest_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "manifest.json"
    return Path(__file__).parent.parent / "manifest.json"


def _read_version() -> str:
    try:
        return json.loads(_manifest_path().read_text(encoding="utf-8"))["version"]
    except Exception:
        return "0.0.0"


STATE_PATH = Path(
    os.environ.get("USERPROFILE") or str(Path.home())
) / ".ee-in-a-box" / "altium-copilot-state.json"


def _is_newer(latest: str, current: str) -> bool:
    def _t(v: str) -> tuple:
        return tuple(int(x) for x in v.split("."))
    return _t(latest) > _t(current)


def _read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")



_GITHUB_RELEASES_URL = "https://api.github.com/repos/ee-in-a-box/altium-copilot/releases/latest"
_UPDATE_CHECK_INTERVAL_HOURS = 24


def _check_for_update(current_version: str) -> None:
    try:
        state = _read_state()
        last_checked = state.get("update_checked_at")
        if last_checked:
            delta = datetime.now(timezone.utc) - datetime.fromisoformat(last_checked)
            if delta.total_seconds() < _UPDATE_CHECK_INTERVAL_HOURS * 3600:
                return
        response = httpx.get(_GITHUB_RELEASES_URL, timeout=5)
        tag = response.json().get("tag_name", "").lstrip("v")
        if not tag:
            return
        state["update_checked_at"] = datetime.now(timezone.utc).isoformat()
        if _is_newer(tag, current_version):
            state["update_available"] = tag
        else:
            state.pop("update_available", None)
        _write_state(state)
    except Exception:
        return


QUERY_NET_MAX_RESULTS = 50
QUERY_NET_MAX_RESULT_SIZE_CHARS = 60_000

SERVER_INSTRUCTIONS = """\
You are an Altium Designer copilot. Use these tools to understand and discuss schematic projects.

## Session Start

At the start of every session, run these steps in order:

1. detect_altium_project — check if Altium is running and surface known projects
2. set_project_dir — load the project using the decision table below (required before any other tool)
3. list_variants — ask the user which variant to work in, then call set_active_variant
   (always ask — do not auto-select even if there is only one variant)
4. get_sheet_context — read the active tab, then get_design_notes (all pages); then
   summarize for the user: project name, sheet count, variant count, and what the
   board does per component descriptions, sheet names and notes.

### Decision table for set_project_dir

| Situation | Action |
|-----------|--------|
| Altium running + project in registry | Call set_project_dir(registry dir) silently |
| Altium running + not in registry, project_path non-empty | Confirm path with user, then call set_project_dir |
| Altium running + not in registry, project_path empty | Ask user for the folder path, then call set_project_dir |
| Altium not running + registry non-empty | Show projects sorted by last_used desc, let user pick, then call set_project_dir |
| Altium not running + registry empty | Ask user for the folder containing the .PrjPcb file |

## Switching Projects

Call set_project_dir with the new path, then repeat the session-start steps.

## General Rules

- Never answer from memory about component values, nets, or topology — always use the tools.
- Only state conclusions supported by tool results. If uncertain — call more tools,
  search a datasheet, or tell the user you are not sure. Do not shortcut to a guess.
- Always work in the context of the active variant. Components with dnp=true are not populated.
  If the user's question implies a different variant, ask before switching.
- Nets with many connections (>25 pins) are likely power or ground rails. query_net will return
  a sample and a warning — treat these as rails, not signals.
- schematic_review — call when the user explicitly asks to review or verify the schematic (e.g. "review this", "check my schematic", "do a design review", "is this correct?", "does this look right?"). Do not call for general questions about the circuit.
- brainstorm_circuits — call when the user wants to brainstorm, or is asking how to design, improve, or choose an approach for a circuit. Any question about topology, architecture, or how to add/change a sub-circuit should trigger this. Do not call for general questions or reviews.
- package_for_xfn — call when the user explicitly asks to export, package, or share the project with firmware, mechanical, test, or reliability engineers (e.g. "package this for the firmware team", "export for sharing", "create a pcb-copilot snapshot"). Do not call speculatively.
- When the user says they have changed or saved anything in the schematic, call `refresh_netlist`
  before answering questions about the updated design. Do not call it speculatively — only after
  the user confirms they have saved in Altium (Ctrl+S).

## Answering Circuit Questions

When the user asks about a component, net, or sub-circuit on a specific sheet:

1. **Start with get_sheet_context** — one call returns every component on the sheet with
   full pin-to-net data and one-hop cross-sheet neighbors (connected_to). This is almost
   always sufficient to answer the question.
2. **For cross-sheet tracing** — `connected_to` already gives you one hop for free
   (refdes, pin, and sheet name). When you need to follow a signal onto a different
   sheet, call get_sheet_context(sheet_name="SHEET") for that sheet — do NOT call
   get_component for each cross-sheet component one-by-one.
3. **For deeper tracing only** — call query_net or get_component when you need to go two
   hops deep on a specific net, or the net is high-fanout (>25 pins).

Do not call get_component or query_net individually for components already visible in a
sheet context you have already loaded.
Do not call get_sheet_context on the same sheet twice in the same conversation turn.

## Design Notes

Designers record intent the netlist cannot show: why a value was chosen, open questions,
DNP reasons, fabrication requirements. Use it.

- get_sheet_context includes the notes of that sheet; get_design_notes covers every sheet,
  the project parameters and the Draftsman (.PCBDwf) drawings. Pass document="<sheet or
  drawing name>" to read one document.
- "(near R4, U1)" lists the components drawn next to a note — a proximity hint, not a
  connection. Confirm with the circuit before relying on it.
- Notes can be stale or wrong. When a note contradicts the circuit (a value, a part
  number), point out the mismatch instead of trusting either side.
- Pictures ([image N] on sheets, [picture N] in drawings) are often datasheet excerpts,
  block diagrams or photos. Call get_design_image(document, index) when one may answer
  the question; do not guess what a picture shows from its file name.
- Draftsman drill tables and layer stack notes are cached from the board at the drawing's
  last update; prefer get_board_info / get_net_pcb for current PCB data, and mention it
  when source_pcb is flagged as not this project's PcbDoc.
- Review comments made in Altium 365 are stored in the cloud workspace and cannot be
  read by these tools. If the user asks about them, say so.

## PCB / Layout Questions

For questions about the physical board (where nets route, layers, stackup, crosstalk,
component locations, distances):

1. **Call get_board_info first** — it returns the stackup, origin, extents, units, and
   data freshness. State the origin and units when discussing any coordinates.
2. get_net_pcb — "where does net X route": layers, lengths, vias, pad endpoints.
3. get_net_neighbors — crosstalk questions: "what runs near X". Proximity results are
   geometric facts; judging whether coupling matters needs rise times, impedances, and
   termination knowledge — ask the user rather than guessing severity.
4. query_pcb_region — "what is at/near (x, y)".
5. get_component_placement — "where is U12, what's around it".
6. get_rules_for_net — "what width / clearance / via / diff-pair rule applies to X":
   the effective rule per kind with its values, resolved by priority the way Altium does.
7. get_design_rules — list or inspect the board's design rules by kind or name.
8. get_classes / list_diff_pairs — net, component and diff-pair classes; pairs with
   their length skew.
9. check_rule_compliance — "do the tracks and vias of X meet their rules": actual widths,
   via sizes, holes and routing layers against the rule applying to each object.
10. check_lengths — Length and MatchedLengths rules: members, measured lengths, spread
    against tolerance, in-pair skew. get_signal_length measures one net, pair or xSignal.
11. check_diff_pairs — undeclared pairs, polarity swaps at pins, AC-coupling symmetry,
    in-pair skew against within-pair rules.
12. check_sch_pcb_sync — "is the PCB in sync with the schematic?", "did I forget Update
    PCB?", before a release: an offline Show Differences (missing/extra parts, designator,
    footprint and comment changes, pads on the wrong net, renamed or one-sided nets) plus
    the ECO's 'Unknown Pin' failures. With netlist_stale, sheets were saved after the
    netlist was compiled: call refresh_netlist (those saves prove the user saved) and run
    it again. Otherwise name sheets saved after the PcbDoc as the likely cause. Altium's
    Show Differences is the final word.
13. get_drc_report — "what does my DRC say?", "any shorts?", "why does DRC fail?":
    Altium's last Batch DRC report, grouped by rule, component or net, linked to the board.
    Quote its assessment first (stopped at the violation limit, rule kinds not checked,
    older than the board); it is the last run's result, not the board as saved now.
14. constraints_review — call when the user asks to review or verify the PCB's high-speed
    constraints or the routing against them ("check my high-speed rules", "are my DDR
    length rules right?", "review the diff pairs before fab"): a whole-interface audit
    of rules and copper against each chip's layout guide. For one net use
    get_rules_for_net; for circuit correctness schematic_review.

get_board_info lists the stack manager's impedance profiles: target, solved width/gap and
calculated impedance per layer, reference layers, the rules using each, and issues. Quote
these as the design's impedance intent; the fabricator's result is still the final word.

Lengths are measured like Altium's Signal Length: the copper path from pad centre to pad
centre plus the via barrel over the layers it traverses plus the pin/package length stored
on the end pads (xSignals: legs through series parts, pin/package length at the end pins).
Nets without exactly 2 pads use routed copper plus barrels and no pin/package length, like
Altium's Routed Length. A pair's group length is the P/N average. Results marked marginal
are too close to a limit to call. The checks read the saved
rules and copper, not Altium's DRC report (get_drc_report reads that); checked_by_batch_drc false means the designer's
own batch DRC does not check that kind — say so before calling it a problem.

Rule scopes are evaluated per net or pair. A rule marked conditional (with
"conditions") applies only to some of the net's objects — e.g. only its vias, only on
one layer, only inside a room, only its pour copper. Quote the condition instead of
claiming it applies. Binary rules (Clearance, ...) list candidates with orientations:
which one wins depends on the other object, so name the other object when you quote a
clearance. Widths and diff-pair gaps are per layer: quote layers_mil for the layer in
question. A diff-pair net's track width is checked by its DiffPairsRouting rule, not
the Width rule. stale_references are scope names that match nothing on the board, so
that part of the rule silently applies to nothing — worth pointing out. Rule values are
what is stored in the PcbDoc and do not show that the board passes: Altium's own DRC
result is get_drc_report, whose assessment says whether it can be trusted.

Net names are shared between schematic and PCB — pivot freely between query_net
(connectivity) and get_net_pcb (physical routing). PCB tools return both `refdes` (the
designator on the PCB) and `sch_refdes` (the netlist name, used by the schematic tools).
They differ when the board is out of sync with the schematic; PCB-only parts (mounting
holes, logos) have no `sch_refdes`. On multi-channel and
board-annotated designs the schematic sheet's own (logical) refdes is a third name:
get_component_placement reports it as `logical_refdes` and, when nothing else matches,
accepts it, returning every physical instance with its channel path.

PCB data reflects the last SAVE of the .PcbDoc in Altium and refreshes automatically —
if the user made layout changes, ask them to save (Ctrl+S), then call the tool again.
get_board_info reports both PCB and netlist timestamps; if they disagree wildly, warn
the user the schematic and layout may be out of sync.

Before relying on polygon results, ask the user to repour polygons and save the PcbDoc.
The tools model saved nominal polygon outlines, not final region/fill copper; voids,
cutouts, removed islands, and clearances must be verified in Altium.

If the user wants to know where something is visually, give coordinates relative to the
origin and offer to interpret a screenshot or photo they paste for orientation — the
server never renders images.

## Behavioral Guidelines

- **Think Before Proposing:** State your assumptions explicitly. If multiple interpretations of the circuit exist, present them—don't pick silently.
- **Simplicity First:** When suggesting fixes, propose the minimum viable component change. Do not suggest speculative features or over-engineered architectures.
- **Surgical Changes:** Recommend touching only what must be fixed. Do not propose refactoring adjacent, working sub-circuits or nets unless they are directly causing the issue.
- **Goal-Driven Execution:** For complex tracing or multi-step analysis, state a brief plan (e.g., "1. Trace VCC -> verify. 2. Check IC21 inputs -> verify") and loop your tool usage until your success criteria are verified.

## Netlist Freshness

The netlist reflects the last-saved state of the schematic files. If the user has made recent
changes in Altium Designer, ask them to save first (Ctrl+S in Altium) before you answer —
otherwise your data may be stale.

## Error Recovery

- no_components_loaded (from set_project_dir, refresh_netlist or any component tool) → nets
  loaded but no schematic component metadata did. Tell the user, name any sheet_errors, and do
  not answer component questions from the nets alone or retry search_components in a loop
- sheet_errors in set_project_dir/refresh_netlist → those sheets failed to parse; their
  components are missing. Name the failing sheets and errors to the user
- designator_clashes in set_project_dir/refresh_netlist → on that sheet one refdes names
  two or more different components, and only the last one's metadata is loaded. Tell the
  user each sheet, refdes and lib_references (and designator_clash_count if more than are
  listed), and do not trust MPN, value or pin names of those refdes until they are renamed
  and the netlist is refreshed
- Component not found → call search_components with a regex to locate it
- Net not found → use query_net with a keyword pattern to discover the real name
  (e.g. query_net('MISO') or query_net('CAN')). Never guess net names or
  manually chase anonymous nets one by one.
- no_sheet_open → Altium is running but no active schematic sheet is detected (project may
  or may not be loaded); tell the user to open their project in Altium and open a schematic
  sheet, then try again
- active_document_outside_project → the user has a different project open in Altium;
  ask them to switch or call set_project_dir with the correct path
"""

mcp = FastMCP("altium-copilot", instructions=SERVER_INSTRUCTIONS)
# FastMCP 1.27 reports its framework version unless the low-level server
# version is explicitly set.
mcp._mcp_server.version = _read_version()

# ---------- module-level state ----------
_altium: AltiumClient = AltiumClient()
_project: dict | None = None          # {name, root_dir, prj_pcb_path, sheets: [{name, path}], ...}
_variant_state: VariantState | None = None
_netlist_last_updated: str | None = None
_design_notes = DesignNotesCache()


def _require_project():
    if _project is None or _altium._netlist is None or _variant_state is None:
        raise ValueError("No project loaded. Call set_project_dir first.")
    return _project, _altium._netlist, _variant_state


def _no_components_error(netlist: dict) -> dict | None:
    """Structured error when the netlist has no schematic component metadata, else None."""
    if netlist.get("components"):
        return None
    error = {
        "error": "no_components_loaded",
        "message": (
            f"The netlist has {len(netlist.get('nets') or {})} nets but no component metadata "
            "was loaded from the schematic sheets. Component, sheet and BOM answers are not "
            "available. Check sheet_errors, fix or re-save the failing sheets, then call "
            "refresh_netlist or set_project_dir again."
        ),
    }
    if netlist.get("sheet_errors"):
        error["sheet_errors"] = netlist["sheet_errors"]
    return error


_MAX_DESIGNATOR_CLASHES = 20  # designator_clashes listed in a load response


def _load_diagnostics(netlist: dict) -> dict:
    """Component count, per-sheet parse errors and coverage warnings for load responses."""
    components = netlist.get("components") or {}
    diagnostics: dict = {"component_count": len(components)}
    if netlist.get("sheet_errors"):
        diagnostics["sheet_errors"] = netlist["sheet_errors"]
    if netlist.get("annotation_errors"):
        diagnostics["annotation_errors"] = netlist["annotation_errors"]
    clashes = netlist.get("designator_clashes") or []
    if clashes:
        diagnostics["designator_clash_count"] = len(clashes)
        diagnostics["designator_clashes"] = clashes[:_MAX_DESIGNATOR_CLASHES]
        diagnostics["designator_clashes_hint"] = (
            "Each refdes names two or more different components on that sheet, one per "
            "entry in lib_references. Only the last one's metadata (MPN, description, "
            "value, pin names) is loaded; the others have none. Rename or re-annotate the "
            "duplicates in Altium, save, then call refresh_netlist."
            + (f" Only the first {_MAX_DESIGNATOR_CLASHES} are listed."
               if len(clashes) > _MAX_DESIGNATOR_CLASHES else "")
        )
    renamed = sum(1 for comp in components.values() if comp.get("logical_refdes"))
    if renamed:
        diagnostics["renamed_by_annotation"] = renamed
        diagnostics["renamed_by_annotation_hint"] = (
            "The project's .Annotation file gives these parts a board designator other than "
            "the one drawn on their sheet. Tools name board designators; logical_refdes is "
            "the sheet's."
        )
    if not components and netlist.get("pin_to_net"):
        error = _no_components_error(netlist)
        diagnostics["warning"] = error["error"]
        diagnostics["message"] = error["message"]
    elif components:
        missing = sorted(set(netlist.get("pin_to_net") or {}) - set(components))
        if missing:
            diagnostics["refdes_without_metadata"] = len(missing)
            diagnostics["refdes_without_metadata_sample"] = missing[:10]
            diagnostics["refdes_without_metadata_hint"] = (
                "These refdes are in the netlist but on no sheet listed in the saved .PrjPcb "
                "(or on a sheet in sheet_errors). If sheets were added in Altium, save the "
                "project, then call refresh_netlist."
            )
    return diagnostics


def _project_documents(prj_data) -> dict:
    """Board, Draftsman and parameter entries of the project state, from a parsed .PrjPcb."""
    return {
        "pcb_doc_paths": prj_data.pcb_doc_paths,
        "pcb_dwf_paths": prj_data.pcb_dwf_paths,
        "parameters": prj_data.parameters,
        "output_path": prj_data.output_path,
        "log_folder_path": prj_data.log_folder_path,
    }


def _design_info(prj_data) -> dict:
    """Project parameters and Draftsman documents for load responses (omitted when empty)."""
    info: dict = {}
    if prj_data.parameters:
        info["project_parameters"] = prj_data.parameters
    if prj_data.pcb_dwf_paths:
        info["draftsman_documents"] = [Path(p).name for p in prj_data.pcb_dwf_paths]
    return info


def _resolve_dnp_uids(variant_state: VariantState, netlist: dict | None) -> None:
    uid_map = {
        comp.get("unique_id"): refdes
        for refdes, comp in (netlist or {}).get("components", {}).items()
        if comp.get("unique_id")
    }
    variant_state.resolve_dnp_uid(uid_map)


class PcbSession:
    """Lazy-loading, mtime-cached PCB index for the loaded project."""

    def __init__(self):
        self._index = None
        self._path: str | None = None
        self._file_signature: tuple[int, int] | None = None
        self._netlist_ref: dict | None = None

    def invalidate(self) -> None:
        """Drop parsed PCB state when its schematic mapping input changes."""
        self._index = None
        self._path = None
        self._file_signature = None
        self._netlist_ref = None

    def _load(self, path: str, netlist: dict):
        return PcbIndex(parse_pcb_doc(path), netlist)

    def get(self, project: dict, netlist: dict):
        """Return (index, None) or (None, structured_error)."""
        paths = project.get("pcb_doc_paths") or []
        if not paths:
            return None, {
                "error": "no_pcb_document",
                "message": (
                    f"Project '{project.get('name')}' lists no .PcbDoc. "
                    "PCB tools need a board document in the project."
                ),
            }
        path = paths[0]
        pcb_path = Path(path)
        if not pcb_path.exists():
            return None, {
                "error": "pcb_file_missing",
                "message": f"PCB document not found on disk: {path}",
            }
        stat = pcb_path.stat()
        file_signature = (stat.st_mtime_ns, stat.st_size)
        if (
            self._index is not None
            and self._path == path
            and self._file_signature == file_signature
            and self._netlist_ref is netlist
        ):
            return self._index, None
        try:
            index = self._load(path, netlist)
        except Exception as error:
            return None, {
                "error": "pcb_parse_failed",
                "message": str(error),
            }
        self._index = index
        self._path = path
        self._file_signature = file_signature
        self._netlist_ref = netlist
        return index, None


_pcb_session = PcbSession()


# ---------- detect_altium_project ----------

def _detect_altium_project_impl(info: dict, registry: dict) -> dict:
    if not info.get("running"):
        return {"running": False, "registry": registry}
    if not info.get("project_file"):
        return {
            "running": True,
            "warning": "no_sheet_open",
            "message": (
                "Altium is running but no active schematic sheet was detected. "
                "Open your project in Altium and make sure a schematic sheet is open, then try again."
            ),
            "registry": registry,
        }

    project_path = info.get("project_path", "")
    if not project_path:
        entry = next(
            (e for e in registry["projects"]
             if e["name"].lower() == info["project_file"].lower()),
            None,
        )
        if entry:
            project_path = str(Path(entry["dir"]) / info["project_file"])

    return {
        "running": True,
        "project_file": info["project_file"],
        "project_path": project_path,
        "registry": registry,
    }


@mcp.tool(title="Detect Altium Project", annotations=ToolAnnotations(readOnlyHint=True))
def detect_altium_project() -> str:
    """Detect whether Altium Designer is running, which project is open, and known projects."""
    info = _altium.get_status()
    registry = read_registry()
    result = _detect_altium_project_impl(info, registry)
    current = _read_version()
    result["server_version"] = current
    state = _read_state()
    update_available = state.get("update_available")
    if update_available and _is_newer(update_available, current):
        result["update_available"] = update_available
        result["update_command"] = "irm https://raw.githubusercontent.com/ee-in-a-box/altium-copilot/main/install.ps1 | iex"
    return json.dumps(result, indent=2)


# ---------- set_project_dir ----------

@mcp.tool(title="Load Project", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def set_project_dir(project_dir: str) -> str:
    """Load an Altium project from disk. Must be called before any other tool.
    If the netlist cache is valid, loads from disk without requiring Altium to be open.
    Otherwise requires Altium Designer to be running with the project open."""
    global _project, _variant_state, _netlist_last_updated

    # Find .PrjPcb upfront — needed by both the cache shortcut and the Altium flow.
    prj_files = list(Path(project_dir).glob("*.PrjPcb")) + list(Path(project_dir).glob("*.prjpcb"))
    if not prj_files:
        return json.dumps({
            "error": "no_prjpcb",
            "message": f"No .PrjPcb file found in {project_dir}. Check the path."
        })
    prj_pcb_path = str(prj_files[0])
    project_name = Path(prj_pcb_path).stem

    # Cache shortcut — if registry has a validated netlist mtime and the .NET file matches,
    # load from disk without requiring Altium to be running or on a schematic sheet.
    # The .NET must also be at least as new as every sheet listed in the .PrjPcb (wherever
    # it lives in the tree) and the .PrjPcb itself — otherwise fall through and regenerate.
    _stale_cache = False
    _registry = read_registry()
    for _entry in _registry.get("projects", []):
        if Path(_entry.get("dir", "")).resolve() == Path(project_dir).resolve():
            _stored_mtime = _entry.get("netlist_mtime")
            if _stored_mtime is not None:
                _net_matches = (
                    list(Path(project_dir).rglob(f"{project_name}.NET")) or
                    list(Path(project_dir).rglob(f"{project_name}.net"))
                )
                if _net_matches:
                    try:
                        if abs(_net_matches[0].stat().st_mtime - _stored_mtime) < 0.01:
                            prj_data = parse_prj_pcb(prj_pcb_path)
                            _fresh = bool(prj_data.sheet_paths) and is_netlist_fresh(
                                _net_matches[0],
                                netlist_sources(
                                    prj_pcb_path, prj_data.sheet_paths, prj_data.annotation_paths
                                ),
                            )
                            _stale_cache = bool(prj_data.sheet_paths) and not _fresh
                            if _fresh:
                                sheets = [{"name": Path(p).stem, "path": p} for p in prj_data.sheet_paths]
                                _project = None
                                _variant_state = None
                                _altium._netlist = None
                                _netlist_last_updated = None
                                _pcb_session.__init__()
                                _altium.load_netlist_from_file(
                                    str(_net_matches[0]), prj_data.sheet_paths,
                                    prj_data.annotation_paths,
                                )
                                _project = {
                                    "name": project_name,
                                    "root_dir": project_dir,
                                    "prj_pcb_path": prj_pcb_path,
                                    "sheets": sheets,
                                    **_project_documents(prj_data),
                                }
                                _variant_state = VariantState(prj_data.variants)
                                _resolve_dnp_uids(_variant_state, _altium._netlist)
                                _netlist_last_updated = datetime.now(timezone.utc).isoformat()
                                upsert_registry_entry(Path(prj_pcb_path).name, project_dir)
                                response = {
                                    "loaded": True,
                                    "project": project_name,
                                    "sheets": [s["name"] for s in sheets],
                                    "sheet_count": len(sheets),
                                    "variants": [v.name for v in prj_data.variants],
                                    "variant_count": len(prj_data.variants),
                                    "netlist_updated_utc": _netlist_last_updated,
                                    **_load_diagnostics(_altium._netlist or {}),
                                    **_design_info(prj_data),
                                }
                                _claude_md = (
                                    list(Path(project_dir).glob("CLAUDE.md")) +
                                    list(Path(project_dir).glob("claude.md"))
                                )
                                if _claude_md:
                                    response["project_context"] = _claude_md[0].read_text(encoding="utf-8")
                                return json.dumps(response, indent=2)
                    except Exception as e:
                        logging.warning("Registry cache shortcut failed: %s", e)
            break

    # Full Altium flow — required when cache is absent or stale.
    status = _altium.get_status()
    if not status.get("running"):
        response = {
            "error": "altium_not_running",
            "message": "Altium Designer is not running. Open Altium with your project before loading.",
        }
        if _stale_cache:
            response["netlist_stale"] = True
            response["message"] += (
                " The project or its schematic sheets were saved after the cached netlist "
                "was generated, so it must be regenerated in Altium."
            )
        return json.dumps(response)
    if status.get("warning") == "no_sheet_open":
        return json.dumps({
            "error": "no_sheet_open",
            "message": (
                "Altium is running but no active schematic sheet was detected. "
                "Open a schematic sheet in Altium, then try again."
            ),
        })
    if status.get("tab_type") == "other":
        active_tab = status.get("active_tab") or "unknown"
        return json.dumps({
            "error": "no_schematic_active",
            "message": (
                f"Altium is showing '{active_tab}', not a schematic sheet. "
                "Please click on a schematic sheet tab in Altium and try again."
            ),
        })

    altium_project_file = status.get("project_file", "")
    if altium_project_file.lower() != Path(prj_pcb_path).name.lower():
        return json.dumps({
            "error": "project_mismatch",
            "message": (
                f"Altium has '{altium_project_file}' open, but you asked to load "
                f"'{Path(prj_pcb_path).name}'. Open that project in Altium first, then try again."
            ),
            "altium_open": altium_project_file,
            "requested": Path(prj_pcb_path).name,
        })
    # Clear state before attempting generation — if it fails the server is
    # in a clean "no project loaded" state rather than silently serving stale data.
    _project = None
    _variant_state = None
    _altium._netlist = None
    _netlist_last_updated = None
    _pcb_session.__init__()

    prj_data = parse_prj_pcb(prj_pcb_path)

    if not prj_data.sheet_paths:
        return json.dumps({
            "error": "no_sheets",
            "message": "No .SchDoc sheets found in project. Check the .PrjPcb file."
        })

    sheets = [{"name": Path(p).stem, "path": p} for p in prj_data.sheet_paths]

    try:
        _altium.generate_netlist(
            prj_pcb_path,
            sheet_paths=prj_data.sheet_paths,
            annotation_paths=prj_data.annotation_paths,
        )
    except (RuntimeError, OSError) as e:
        return json.dumps({
            "error": "netlist_generation_failed",
            "message": str(e),
        })

    _project = {
        "name": Path(prj_pcb_path).stem,
        "root_dir": project_dir,
        "prj_pcb_path": prj_pcb_path,
        "sheets": sheets,
        **_project_documents(prj_data),
    }
    _variant_state = VariantState(prj_data.variants)
    _resolve_dnp_uids(_variant_state, _altium._netlist)
    _netlist_last_updated = datetime.now(timezone.utc).isoformat()

    _net_matches = (
        list(Path(project_dir).rglob(f"{project_name}.NET")) or
        list(Path(project_dir).rglob(f"{project_name}.net"))
    )
    _net_mtime = _net_matches[0].stat().st_mtime if _net_matches else None
    upsert_registry_entry(Path(prj_pcb_path).name, project_dir, netlist_mtime=_net_mtime)

    response = {
        "loaded": True,
        "project": _project["name"],
        "sheets": [s["name"] for s in sheets],
        "sheet_count": len(sheets),
        "variants": [v.name for v in prj_data.variants],
        "variant_count": len(prj_data.variants),
        "netlist_updated_utc": _netlist_last_updated,
        **_load_diagnostics(_altium._netlist or {}),
        **_design_info(prj_data),
    }

    claude_md_files = (
        list(Path(project_dir).glob("CLAUDE.md")) +
        list(Path(project_dir).glob("claude.md"))
    )
    if claude_md_files:
        response["project_context"] = claude_md_files[0].read_text(encoding="utf-8")

    return json.dumps(response, indent=2)


# ---------- refresh_netlist ----------

@mcp.tool(title="Refresh Netlist", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def refresh_netlist() -> str:
    """Refresh the netlist after the user has saved schematic changes in Altium.
    Call this when the user says they changed, added, or saved anything in the schematic.
    Do not call speculatively — only when the user confirms they have saved."""
    global _netlist_last_updated
    try:
        project, _, variant_state = _require_project()
    except ValueError as e:
        return json.dumps({"error": "no_project", "message": str(e)})
    # Re-read the sheet list so sheets added, moved or removed since load are picked up
    # and the sheet names stay in step with the components' sheet attribution.
    prj_data = None
    sheet_paths = [s["path"] for s in project.get("sheets", [])]
    annotation_paths = None
    try:
        prj_data = parse_prj_pcb(project["prj_pcb_path"])
        sheet_paths = prj_data.sheet_paths
        annotation_paths = prj_data.annotation_paths
    except Exception as e:
        logging.warning("Could not re-read .PrjPcb, using the sheet list from load: %s", e)
    try:
        regenerated = _altium.generate_netlist(
            project["prj_pcb_path"], sheet_paths=sheet_paths, annotation_paths=annotation_paths
        )
    except Exception as e:
        return json.dumps({"error": "generate_failed", "message": str(e)})
    if prj_data is not None:
        project["sheets"] = [{"name": Path(p).stem, "path": p} for p in prj_data.sheet_paths]
        project.update(_project_documents(prj_data))
    _resolve_dnp_uids(variant_state, _altium._netlist)
    diagnostics = _load_diagnostics(_altium._netlist or {})
    if not regenerated:
        message = "Netlist is already up to date. If you expected changes, save in Altium and try refresh again."
        if diagnostics.get("warning"):
            message += f"\nWarning ({diagnostics['warning']}): {diagnostics['message']}"
        for err in diagnostics.get("sheet_errors", []):
            message += f"\nSheet '{err['sheet']}' failed to load: {err['error']}"
        for err in diagnostics.get("annotation_errors", []):
            message += f"\nAnnotation file '{Path(err['path']).name}' not applied: {err['error']}"
        clashes = diagnostics.get("designator_clashes", [])
        for clash in clashes:
            message += (
                f"\nSheet '{clash['sheet']}': {clash['refdes']} names "
                f"{len(clash['lib_references'])} different components "
                f"({', '.join(clash['lib_references'])}); only the last one is loaded"
            )
        if diagnostics.get("designator_clash_count", 0) > len(clashes):
            message += (f"\n... and {diagnostics['designator_clash_count'] - len(clashes)} "
                        "more designator clashes")
        return message
    _pcb_session.invalidate()
    _netlist_last_updated = datetime.now(timezone.utc).isoformat()
    _rn_name = Path(project["prj_pcb_path"]).stem
    _rn_dir = project["root_dir"]
    _rn_net = (
        list(Path(_rn_dir).rglob(f"{_rn_name}.NET")) or
        list(Path(_rn_dir).rglob(f"{_rn_name}.net"))
    )
    upsert_registry_entry(
        Path(project["prj_pcb_path"]).name,
        _rn_dir,
        netlist_mtime=_rn_net[0].stat().st_mtime if _rn_net else None,
    )
    return json.dumps({
        "refreshed": True,
        "netlist_updated_utc": _netlist_last_updated,
        **diagnostics,
    })


# ---------- query_net ----------

def _query_net_impl(netlist: dict, pattern: str) -> str:
    nets = netlist["nets"]

    # Try regex first for discovery (e.g. 'UART' finds MCU_UART_TX and USB_UART_RX).
    # If regex is invalid or finds nothing, fall back to exact case-insensitive match
    # so PCB net names with metacharacters (+5V, VIN[0]) resolve without escaping.
    try:
        regex = re.compile(pattern, re.IGNORECASE)
        matches = [name for name in nets if regex.search(name)]
    except re.error:
        matches = []
    if not matches:
        exact_key = next((k for k in nets if k.lower() == pattern.lower()), None)
        if exact_key is not None:
            matches = [exact_key]

    if not matches:
        return json.dumps({"error": "net_not_found", "pattern": pattern,
                           "message": f"No nets matching '{pattern}'. Use a broader pattern or check the name."})

    if len(matches) > QUERY_NET_MAX_RESULTS:
        return json.dumps({
            "error": "too_many_matches",
            "message": f"Pattern matched {len(matches)} nets (limit: {QUERY_NET_MAX_RESULTS}). Be more specific.",
        })

    results = []
    for net_key in matches:
        all_connections = nets[net_key]
        if len(all_connections) > HIGH_FANOUT_THRESHOLD:
            results.append({
                "net": net_key,
                "pin_count": len(all_connections),
                "warning": "high_fanout",
                "message": (
                    f"Net '{net_key}' has {len(all_connections)} connections — likely a power rail or ground. "
                    "Showing a sample of 10 connections only."
                ),
                "pins_sample": [{"refdes": r, "pin": p} for r, p in all_connections[:10]],
            })
        else:
            results.append({
                "net": net_key,
                "pin_count": len(all_connections),
                "pins": [{"refdes": r, "pin": p} for r, p in all_connections],
            })

    if len(results) == 1:
        return json.dumps(results[0], indent=2)
    return json.dumps({"match_count": len(results), "nets": results}, indent=2)


@mcp.tool(title="Query Net", annotations=ToolAnnotations(readOnlyHint=True),
          meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS})
def query_net(pattern: str) -> str:
    """Find nets by name and return every component pin connected to them.
    Accepts a case-insensitive regex pattern for discovery (e.g. 'UART' finds all
    UART nets, 'SPI_CLK|SPI_CS' finds both, 'GND' finds GND/AGND/DGND).
    If the pattern is not valid regex (e.g. +5V, VIN[0] contain metacharacters),
    it automatically falls back to an exact case-insensitive match — so common
    power rail names like +5V and +3V3 work without escaping.
    High-fanout nets (power rails, ground) are flagged and sampled at 10 pins.
    Use get_sheet_context for full pin-to-net data on a sheet; use this tool to
    discover net names or trace a specific net across sheets."""
    try:
        _, netlist, _ = _require_project()
    except ValueError as e:
        return json.dumps({"error": "no_project", "message": str(e)})
    return _query_net_impl(netlist, pattern)


# ---------- get_component ----------

def _get_component_impl(netlist: dict, variant_state: VariantState, refdes: str) -> str:
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    components = netlist.get("components", {})
    matched = next((k for k in components if k.lower() == refdes.lower()), None)
    if matched is None:
        # On a board-annotated project, a sheet designator that names no board part.
        placed = [k for k, comp in components.items()
                  if (comp.get("logical_refdes") or "").lower() == refdes.lower()]
        if len(placed) > 1:
            return json.dumps({
                "error": "component_not_found",
                "message": (
                    f"'{refdes}' is a sheet designator placed {len(placed)} times; on the "
                    f"board it is {', '.join(placed)}. Call get_component with one of those."
                ),
                "board_designators": [
                    {"refdes": k, "sheet": components[k].get("sheet")} for k in placed
                ],
            })
        matched = placed[0] if placed else None
    if matched is None:
        return json.dumps({
            "error": "component_not_found",
            "message": f"Component '{refdes}' not found. Use search_components to find available components.",
        })

    comp = components[matched]
    detail = {"refdes": matched}
    if comp.get("logical_refdes"):
        detail["logical_refdes"] = comp["logical_refdes"]
    return json.dumps({
        **detail,
        "mpn": comp.get("mpn"),
        "description": comp.get("description"),
        "value": comp.get("value"),
        "dnp": variant_state.is_dnp(matched),
        "sheet": comp.get("sheet"),
        "pins": comp.get("pins", {}),
    }, indent=2)


@mcp.tool(title="Get Component", annotations=ToolAnnotations(readOnlyHint=True))
def get_component(refdes: str) -> str:
    """Get full detail for one component: MPN, value, DNP status, and every pin with its net.
    Use this to drill into a specific component and trace its connections.
    refdes is the board designator; logical_refdes, when present, is the different one
    drawn on the sheet (board-level annotation), also accepted as refdes when no board
    part has that name."""
    try:
        _, netlist, variant_state = _require_project()
    except ValueError as e:
        return json.dumps({"error": "no_project", "message": str(e)})
    return _get_component_impl(netlist, variant_state, refdes)


# ---------- search_components ----------

def _search_components_impl(netlist: dict, pattern: str, search_by: str) -> str:
    if search_by not in ("refdes", "mpn", "description"):
        return json.dumps({
            "error": "invalid_search_by",
            "message": "search_by must be 'refdes', 'mpn', or 'description'",
        })

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return json.dumps({"error": "invalid_pattern", "message": f"Invalid regex: {e}"})

    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)

    components = netlist.get("components", {})
    matches: dict = {}
    for refdes, comp in components.items():
        if search_by == "refdes":
            target = refdes
        elif search_by == "mpn":
            target = comp.get("mpn") or ""
        else:
            target = comp.get("description") or ""

        if regex.search(target):
            matches[refdes] = comp

    if len(matches) == len(components):
        return json.dumps({
            "error": "too_many_matches",
            "message": f"Pattern matched all {len(components)} components. Be more specific.",
        })

    # Group by MPN
    groups: dict[str, dict] = {}
    for refdes, comp in matches.items():
        mpn_key = comp.get("mpn") or f"__no_mpn_{refdes}__"
        if mpn_key not in groups:
            groups[mpn_key] = {
                "mpn": comp.get("mpn"),
                "description": comp.get("description"),
                "count": 0,
                "refdes": [],
                "_values": set(),
            }
        groups[mpn_key]["count"] += 1
        groups[mpn_key]["refdes"].append(refdes)
        if comp.get("value"):
            groups[mpn_key]["_values"].add(comp["value"])

    results = []
    for group in groups.values():
        values = group.pop("_values")
        group["value"] = next(iter(values)) if len(values) == 1 else None
        results.append(group)

    return json.dumps({
        "results": results,
        "match_count": sum(g["count"] for g in results),
    }, indent=2)


@mcp.tool(title="Search Components", annotations=ToolAnnotations(readOnlyHint=True))
def search_components(pattern: str, search_by: str = "description") -> str:
    """Search components by regex pattern. search_by: 'refdes', 'mpn', or 'description'.
    Returns components grouped by MPN with counts. Use get_component for full pin detail."""
    try:
        _, netlist, _ = _require_project()
    except ValueError as e:
        return json.dumps({"error": "no_project", "message": str(e)})
    return _search_components_impl(netlist, pattern, search_by)


# ---------- get_sheet_context ----------

def _get_sheet_context_impl(project: dict, netlist: dict, variant_state: VariantState,
                             sheet_name: str | None, altium_status: dict,
                             offset: int = 0, notes_cache: DesignNotesCache | None = None) -> str:
    if sheet_name is None:
        if not altium_status.get("running"):
            return json.dumps({
                "warning": "altium_not_running",
                "message": "Altium Designer is not running. Open Altium and try again.",
            })
        active_tab = altium_status.get("active_tab", "")
        # Strip Altium's unsaved-changes marker (" *") from the tab title
        active_tab = active_tab.rstrip(" *").strip()
        tab_to_sheet = {Path(s["path"]).name.lower(): s["name"] for s in project["sheets"]}
        # Altium tab titles may omit the file extension — also index by stem
        tab_to_sheet.update({Path(s["path"]).stem.lower(): s["name"] for s in project["sheets"]})
        matched = tab_to_sheet.get(active_tab.lower())
        if not matched:
            return json.dumps({
                "warning": "active_document_outside_project",
                "active_filename": active_tab,
                "message": (
                    f'"{active_tab}" does not belong to project "{project["name"]}". '
                    "Switch to a sheet in this project."
                ),
            })
        sheet_name = matched
    else:
        matched = next((s["name"] for s in project["sheets"]
                        if s["name"].lower() == sheet_name.lower()), None)
        if matched is None:
            available = [s["name"] for s in project["sheets"]]
            return json.dumps({
                "error": "sheet_not_found",
                "message": f"Sheet '{sheet_name}' not found. Available: {available}",
            })
        sheet_name = matched

    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    context = build_sheet_context(netlist, sheet_name, variant_state, offset)
    sheet_error = next((e for e in netlist.get("sheet_errors") or []
                        if e.get("sheet", "").lower() == sheet_name.lower()), None)
    if sheet_error:
        context = (
            f"warning: sheet_load_failed — '{sheet_name}' could not be parsed "
            f"({sheet_error['error']}); its components are missing below.\n" + context
        )
    if notes_cache is not None and offset == 0:
        # Designer notes go between the header and the components, on the first page only.
        notes = sheet_notes_for_context(notes_cache, project, sheet_name)
        if notes:
            header, _, components = context.partition("\n\n")
            context = f"{header}\n\n{notes}\n\n{components}"
    return context


@mcp.tool(title="Get Sheet Context", annotations=ToolAnnotations(readOnlyHint=True),
          meta={"anthropic/maxResultSizeChars": MAX_RESULT_SIZE_CHARS})
def get_sheet_context(sheet_name: str | None = None, offset: int = 0) -> str:
    """Get all components on a schematic sheet with their pin-to-net connections and
    one-hop cross-sheet neighbors. Pass sheet_name to load any sheet by name — not just
    the active Altium tab. Use this as the FIRST tool for any question about a sheet, and
    again with sheet_name when following cross-sheet signals. Only call query_net or
    get_component afterward for high-fanout nets (>25 pins) or two-hop tracing.

    The first page also carries a design_notes block: the designer's notes, text frames,
    labels, pictures, directives and title block on that sheet. "(near R4, U1)" names the
    components drawn next to a note — a proximity hint, not a connection. Notes can be
    stale: when one contradicts the circuit, point out the mismatch.

    Results are paginated by character budget. If the response contains has_more:true,
    call this tool again with the offset from the next: line and the same sheet_name,
    and repeat until has_more:false. Accumulate all pages before answering the user."""
    try:
        project, netlist, variant_state = _require_project()
    except ValueError as e:
        return json.dumps({"error": "no_project", "message": str(e)})

    altium_status = _altium.get_status() if sheet_name is None else {}
    return _get_sheet_context_impl(project, netlist, variant_state, sheet_name, altium_status,
                                   offset, notes_cache=_design_notes)


# ---------- get_design_notes ----------

@mcp.tool(title="Get Design Notes", annotations=ToolAnnotations(readOnlyHint=True),
          meta={"anthropic/maxResultSizeChars": NOTES_MAX_RESULT_SIZE_CHARS})
def get_design_notes(document: str | None = None, offset: int = 0) -> str:
    """Read what designers wrote into the project besides the circuit itself: project
    parameters; on every schematic sheet the notes (author, collapsed state), text frames,
    free text labels, pictures, directives and title-block parameters; and in every
    Draftsman drawing (.PCBDwf) the fabrication/assembly notes, free text, tables, view
    titles, pictures, and the drill table and layer stack notes cached from the board.

    Pass document (a sheet name or Draftsman file name) to read one document. Schematic
    notes, text frames and labels list the components drawn next to them ("near ...") —
    a proximity hint, not a connection. Notes record intent, open questions and
    manufacturing requirements, but can be stale: report a note that contradicts the
    circuit instead of trusting either. Pictures ([image N], [picture N]) are often
    datasheet excerpts or block diagrams: call get_design_image to look at one rather
    than guessing from its file name. Draftsman drill tables and layer notes are cached
    from the board at the drawing's last update, and source_pcb is flagged when that was
    not this project's PcbDoc. Review comments made in Altium 365 live in the cloud
    workspace and cannot be read here.

    Results are paginated by character budget. If the response contains has_more:true,
    call again with the offset from the next: line until has_more:false."""
    try:
        project, _, _ = _require_project()
    except ValueError as e:
        return json.dumps({"error": "no_project", "message": str(e)})
    return build_design_notes(_design_notes, project, document, offset)


# ---------- get_design_image ----------

def _linked_image_path(sheet_path: str, file_name: str | None) -> Path | None:
    """The picture file a schematic links to, if it is on disk.

    A relative link is resolved against the sheet's folder. An absolute link that no
    longer exists (e.g. the project was moved) falls back to its file name next to the sheet.
    """
    if not file_name:
        return None
    windows_path = PureWindowsPath(file_name)
    sheet_dir = Path(sheet_path).parent
    if windows_path.is_absolute():
        candidates = [Path(file_name), sheet_dir / windows_path.name]
    else:
        candidates = [sheet_dir / Path(*windows_path.parts)]
    return next((c for c in candidates if c.is_file()), None)


def _get_design_image_impl(project: dict, notes_cache: DesignNotesCache,
                           document: str, index: int) -> Image | dict:
    resolved = resolve_document(project, document)
    if resolved is None:
        available = [s["name"] for s in project.get("sheets") or []] + [
            d["name"] for d in draftsman_documents(project)
        ]
        return {"error": "document_not_found",
                "message": f"'{document}' is not a sheet or Draftsman document. Available: {available}"}
    kind, doc = resolved
    not_found = {"error": "image_not_found",
                 "message": f"'{doc['name']}' has no picture {index}. "
                            "get_design_notes lists the pictures of each document with their index."}
    try:
        if kind == "sheet":
            image = next((i for i in notes_cache.sheet(doc["path"]).items
                          if i.kind == "image" and i.image_index == index), None)
            if image is None:
                return not_found
            if image.embedded:
                data = read_sch_doc_image(doc["path"], image.file_name or "")
            else:
                linked = _linked_image_path(doc["path"], image.file_name)
                if linked is None:
                    return {"error": "linked_image_missing",
                            "message": f"Picture {index} on '{doc['name']}' links to "
                                       f"'{image.file_name}', which is not a readable image on disk."}
                data = linked.read_bytes()
        else:
            pictures = notes_cache.draftsman(doc["path"]).pictures
            if not 0 <= index < len(pictures):
                return not_found
            data = pictures[index]
        fmt = image_format(data)
        if fmt == "bmp":
            data, fmt = bmp_to_png(data), "png"
    except KeyError:
        return not_found
    except Exception as e:
        return {"error": "image_read_failed", "message": f"{type(e).__name__}: {e}"}
    if fmt is None:
        return {"error": "unsupported_image_format",
                "message": f"Picture {index} of '{doc['name']}' is not PNG, JPEG, GIF, WebP or BMP "
                           "(e.g. a linked TIFF or metafile), so it cannot be shown."}
    if len(data) > MAX_IMAGE_BYTES:
        return {"error": "image_too_large",
                "message": f"Picture {index} of '{doc['name']}' is {len(data)} bytes; "
                           f"the limit is {MAX_IMAGE_BYTES}."}
    size = image_size(data)
    if size and max(size) > MAX_PASSTHROUGH_SIDE_PX:
        return {"error": "image_too_large",
                "message": f"Picture {index} of '{doc['name']}' is {size[0]}x{size[1]} px; "
                           f"images over {MAX_PASSTHROUGH_SIDE_PX} px per side are rejected by the API."}
    return Image(data=data, format=fmt)


@mcp.tool(title="Get Design Image", annotations=ToolAnnotations(readOnlyHint=True),
          structured_output=False)
def get_design_image(document: str, index: int = 0) -> Image | str:
    """Look at a picture placed on a schematic sheet or in a Draftsman drawing — e.g. a
    pasted datasheet excerpt, block diagram, photo or logo. document is the sheet name or
    Draftsman file name, index the picture number that get_design_notes and
    get_sheet_context list as [image N] / [picture N]. Schematic pictures are stored as
    bitmaps and returned as PNG."""
    try:
        project, _, _ = _require_project()
    except ValueError as e:
        return json.dumps({"error": "no_project", "message": str(e)})
    result = _get_design_image_impl(project, _design_notes, document, index)
    return json.dumps(result) if isinstance(result, dict) else result


# ---------- list_variants ----------

def _list_variants_impl(variant_state: VariantState) -> str:
    variants = []
    for v in variant_state._variants:
        variants.append({
            "name": v.name,
            "dnp_count": len(v.dnp_refdes),
            "dnp_components": v.dnp_refdes,
            "is_active": v.name == variant_state.active.name,
        })
    return json.dumps({
        "active_variant": variant_state.active.name,
        "variants": variants,
    }, indent=2)


@mcp.tool(title="List Variants", annotations=ToolAnnotations(readOnlyHint=True))
def list_variants() -> str:
    """List all project variants and their DNP component lists.
    Call this at session start so the user can choose which variant to work in."""
    try:
        _, _, variant_state = _require_project()
    except ValueError as e:
        return json.dumps({"error": "no_project", "message": str(e)})
    return _list_variants_impl(variant_state)


# ---------- set_active_variant ----------

def _set_active_variant_impl(variant_state: VariantState, variant_name: str) -> str:
    try:
        variant_state.set_variant(variant_name)
        active = variant_state.active
        return json.dumps({
            "active_variant": active.name,
            "dnp_components": active.dnp_refdes,
            "message": f"Switched to variant '{active.name}'. {len(active.dnp_refdes)} components are DNP.",
        }, indent=2)
    except ValueError as e:
        return json.dumps({"error": "variant_not_found", "message": str(e)})


@mcp.tool(title="Set Active Variant", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def set_active_variant(variant_name: str) -> str:
    """Switch the active variant. DNP annotations on get_component and get_sheet_context
    will reflect the new variant immediately."""
    try:
        _, _, variant_state = _require_project()
    except ValueError as e:
        return json.dumps({"error": "no_project", "message": str(e)})
    return _set_active_variant_impl(variant_state, variant_name)


def _convert_units(obj, to_mm: bool, inside_mil: bool = False):
    """Convert unit-explicit mil values and containers to millimeters."""
    if not to_mm:
        return obj
    if isinstance(obj, dict):
        converted = {}
        for key, value in obj.items():
            is_mil_key = key.endswith("_mil")
            new_key = key[:-4] + "_mm" if is_mil_key else key
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
            ):
                converted[new_key] = (
                    round(value * 0.0254, 4)
                    if is_mil_key or inside_mil
                    else value
                )
            else:
                converted[new_key] = _convert_units(
                    value,
                    to_mm,
                    inside_mil or is_mil_key,
                )
        return converted
    if isinstance(obj, list):
        return [
            _convert_units(value, to_mm, inside_mil)
            for value in obj
        ]
    if (
        isinstance(obj, (int, float))
        and not isinstance(obj, bool)
        and inside_mil
    ):
        return round(obj * 0.0254, 4)
    return obj


PcbUnits = Literal["mil", "mm"]


def _pcb_units(index, units: PcbUnits | None) -> tuple[bool, str]:
    resolved = (units or index.pcb.board.display_unit).lower()
    if resolved not in {"mil", "mm"}:
        raise ValueError("units must be 'mil' or 'mm'")
    return resolved == "mm", resolved


def _pcb_units_or_error(index, units: PcbUnits | None):
    try:
        to_mm, resolved = _pcb_units(index, units)
    except (AttributeError, ValueError):
        return None, None, {
            "error": "invalid_units",
            "message": "units must be 'mil' or 'mm'.",
        }
    return to_mm, resolved, None


def _input_to_mil(value: float, units: str) -> float:
    return value / 0.0254 if units == "mm" else value


def _finite_nonnegative(value: float) -> bool:
    return math.isfinite(value) and value >= 0.0


PCB_POLYGON_GEOMETRY_NOTE = (
    "Repour polygons in Altium and save the PcbDoc before relying on polygon "
    "results. These tools model saved nominal polygon outlines, not final "
    "region/fill copper; verify voids, cutouts, removed islands, and "
    "clearances in Altium."
)


def _annotate_dnp(records: list[dict]) -> None:
    if _variant_state is None:
        return
    for record in records:
        refdes = record.get("sch_refdes") or record.get("refdes")
        record["dnp"] = bool(refdes and _variant_state.is_dnp(refdes))


@mcp.tool(
    title="Get Board Info",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_board_info(units: PcbUnits | None = None) -> str:
    """Return board geometry, stackup, counts, and data freshness.

    units controls returned coordinates and lengths; it defaults to the
    board's display unit.
    """
    try:
        project, netlist, _ = _require_project()
    except ValueError as error:
        return json.dumps({"error": "no_project", "message": str(error)})
    index, error = _pcb_session.get(project, netlist)
    if error:
        return json.dumps(error)
    board = index.pcb.board
    to_mm, unit_name, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    sides = {
        row["side"]: row["c"]
        for row in index.db.execute(
            "SELECT side, COUNT(*) c FROM components GROUP BY side"
        )
    }
    copper_bbox = index.db.execute(
        "SELECT MIN(minx) a, MIN(miny) b, "
        "MAX(maxx) c, MAX(maxy) d FROM prims"
    ).fetchone()
    if board.outline_vertices:
        outline_x = [
            vertex[0] for vertex in board.outline_vertices
        ]
        outline_y = [
            vertex[1] for vertex in board.outline_vertices
        ]
        board_bbox = {
            "minx_mil": min(outline_x),
            "miny_mil": min(outline_y),
            "maxx_mil": max(outline_x),
            "maxy_mil": max(outline_y),
        }
        extents_source = "board_outline"
    else:
        board_bbox = {
            "minx_mil": copper_bbox["a"],
            "miny_mil": copper_bbox["b"],
            "maxx_mil": copper_bbox["c"],
            "maxy_mil": copper_bbox["d"],
        }
        extents_source = "copper"
    stackup = [
        {
            "name": layer.name,
            "kind": layer.kind,
            "stack_order": layer.stack_order,
            "copper_thick_mil": layer.copper_thick_mil,
            "diel_height_mil": layer.diel_height_mil,
            "diel_const": layer.diel_const,
            "material": layer.material,
        }
        for layer in board.layers
        if layer.stack_order is not None
    ]
    pcb_path = Path(project["pcb_doc_paths"][0])
    result = {
        "pcb_doc": pcb_path.name,
        "last_saved_utc": datetime.fromtimestamp(
            pcb_path.stat().st_mtime,
            tz=timezone.utc,
        ).isoformat(),
        "units": unit_name,
        "origin_mil": {
            "x_mil": board.origin_x,
            "y_mil": board.origin_y,
        },
        "copper_extents_mil": {
            "minx_mil": copper_bbox["a"],
            "miny_mil": copper_bbox["b"],
            "maxx_mil": copper_bbox["c"],
            "maxy_mil": copper_bbox["d"],
        },
        "board_extents_mil": board_bbox,
        "extents_source": extents_source,
        "stackup_source": board.stackup_source,
        "stackup": stackup,
        **(
            {
                "impedance_profiles": impedance_profiles(index),
                "impedance_note": PROPAGATION_UNIT_NOTE,
            }
            if index.pcb.board.impedance_profiles
            else {}
        ),
        "netlist_updated_utc": _netlist_last_updated,
        "counts": index.counts(),
        "design_rules": _design_rule_counts(index),
        "components_per_side": sides,
        "refdes_mapping_unmatched": index.unmatched_components(),
        "warnings": index.pcb.warnings,
        "skipped_records": index.pcb.skipped_records,
    }
    if result["refdes_mapping_unmatched"]:
        result["refdes_mapping_unmatched_sample"] = index.unmatched_refdes(10)
    renamed = index.renamed_components()
    if renamed:
        result["refdes_differs_from_logical"] = renamed
    if result["counts"]["pours"]:
        result["analysis_notes"] = [PCB_POLYGON_GEOMETRY_NOTE]
    if len(project["pcb_doc_paths"]) > 1:
        result["note"] = (
            f"Project lists {len(project['pcb_doc_paths'])} PcbDocs; "
            f"using '{pcb_path.name}'. Others: "
            + ", ".join(
                Path(path).name
                for path in project["pcb_doc_paths"][1:]
            )
        )
    return json.dumps(_convert_units(result, to_mm), indent=2)


def _pcb_index_or_error():
    try:
        project, netlist, _ = _require_project()
    except ValueError as error:
        return None, {"error": "no_project", "message": str(error)}
    return _pcb_session.get(project, netlist)


def _resolve_layer_or_error(index, layer: str | None):
    if layer is None:
        return None, None
    resolved = index.resolve_layer(layer)
    if resolved is None:
        return None, {
            "error": "layer_not_found",
            "available_layers": [
                board_layer.name
                for board_layer in index.pcb.board.layers
                if board_layer.kind == "copper"
            ],
            "message": f"Layer '{layer}' not found.",
        }
    return resolved, None


def _safe_regex(pattern: str) -> bool:
    try:
        re.compile(pattern, re.IGNORECASE)
        return True
    except re.error:
        return False


@mcp.tool(
    title="Get Net PCB Routing",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_net_pcb(net: str, units: PcbUnits | None = None) -> str:
    """Return physical PCB routing details for exact or regex-matched nets."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, _, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    exact = next(
        (
            name
            for name in index.pcb.nets
            if name.lower() == net.lower()
        ),
        None,
    )
    matches = (
        [exact]
        if exact
        else (
            [
                name
                for name in index.pcb.nets
                if re.search(net, name, re.IGNORECASE)
            ]
            if _safe_regex(net)
            else []
        )
    )
    if not matches:
        return json.dumps(
            {
                "error": "net_not_found",
                "pattern": net,
                "message": f"No PCB nets matching '{net}'.",
            }
        )
    if len(matches) > QUERY_NET_MAX_RESULTS:
        return json.dumps(
            {
                "error": "too_many_matches",
                "message": (
                    f"Pattern matched {len(matches)} nets "
                    f"(limit {QUERY_NET_MAX_RESULTS}). Be more specific."
                ),
            }
        )
    if len(matches) > 1:
        compact = []
        for match in matches:
            summary = index.net_summary(match)
            compact.append(
                {
                    "net": summary["net"],
                    "layers": [
                        layer["layer"] for layer in summary["layers"]
                    ],
                    "total_length_mil": round(
                        sum(
                            layer["length_mil"]
                            for layer in summary["layers"]
                        ),
                        1,
                    ),
                    "via_count": summary["via_count"],
                    "pad_count": summary["pad_count"],
                }
            )
        return json.dumps(
            _convert_units(
                {
                    "match_count": len(compact),
                    "nets": compact,
                    "hint": (
                        "Call get_net_pcb with an exact name "
                        "for full detail."
                    ),
                },
                to_mm,
            ),
            indent=2,
        )
    summary = index.net_summary(matches[0])
    _annotate_dnp(summary["pads"])
    classes = index.net_classes_of(summary["net"])
    if classes:
        summary["net_classes"] = classes
    pairs = index.diff_pairs_of(summary["net"])
    if pairs:
        summary["diff_pairs"] = pairs
    if summary["pour_count"]:
        summary["geometry_note"] = PCB_POLYGON_GEOMETRY_NOTE
    return json.dumps(_convert_units(summary, to_mm), indent=2)


@mcp.tool(
    title="Get Net Neighbors",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_net_neighbors(
    net: str,
    distance: float | None = None,
    layer: str | None = None,
    units: PcbUnits | None = None,
) -> str:
    """Return same-layer proximity and adjacent-layer overlap for a PCB net.

    An explicit distance is expressed in units (default: board display unit).
    Omitting distance uses a physical default of 10 mil.
    """
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, input_units, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    if distance is not None and not _finite_nonnegative(distance):
        return json.dumps(
            {
                "error": "invalid_distance",
                "message": "distance must be finite and nonnegative.",
            }
        )
    distance_mil = (
        10.0
        if distance is None
        else _input_to_mil(distance, input_units)
    )
    layer_id, layer_error = _resolve_layer_or_error(index, layer)
    if layer_error:
        return json.dumps(layer_error)
    result = index.net_neighbors(
        net,
        distance=distance_mil,
        layer=layer_id,
    )
    if result is None:
        return json.dumps(
            {
                "error": "net_not_found",
                "message": (
                    f"Net '{net}' not found on the PCB. "
                    "Use get_net_pcb with a pattern to discover names."
                ),
            }
        )
    partners = {
        pair["partner_net"].lower() for pair in index.diff_pairs_of(net)
    }
    if partners:
        for entry in result.get("neighbors", []) + result.get("broadside", []):
            if str(entry.get("net", "")).lower() in partners:
                entry["diff_pair_partner"] = True
    result["geometry_note"] = PCB_POLYGON_GEOMETRY_NOTE
    return json.dumps(_convert_units(result, to_mm), indent=2)


@mcp.tool(
    title="Query PCB Region",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def query_pcb_region(
    x: float,
    y: float,
    radius: float | None = None,
    layer: str | None = None,
    units: PcbUnits | None = None,
) -> str:
    """Return nets, components, and pours near a board coordinate.

    x, y, and an explicit radius are expressed in units (default: board
    display unit). Omitting radius uses a physical default of 50 mil.
    """
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, input_units, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    if not (math.isfinite(x) and math.isfinite(y)):
        return json.dumps(
            {
                "error": "invalid_coordinate",
                "message": "x and y must be finite.",
            }
        )
    if radius is not None and not _finite_nonnegative(radius):
        return json.dumps(
            {
                "error": "invalid_radius",
                "message": "radius must be finite and nonnegative.",
            }
        )
    x_mil = _input_to_mil(x, input_units)
    y_mil = _input_to_mil(y, input_units)
    radius_mil = (
        50.0
        if radius is None
        else _input_to_mil(radius, input_units)
    )
    layer_id, layer_error = _resolve_layer_or_error(index, layer)
    if layer_error:
        return json.dumps(layer_error)
    result = index.region_query(
        x_mil,
        y_mil,
        radius_mil,
        layer=layer_id,
    )
    _annotate_dnp(result["components"])
    result["geometry_note"] = PCB_POLYGON_GEOMETRY_NOTE
    return json.dumps(_convert_units(result, to_mm), indent=2)


@mcp.tool(
    title="Get Component Placement",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_component_placement(
    refdes: str,
    units: PcbUnits | None = None,
) -> str:
    """Return physical placement and nearby-component details.

    refdes is the designator on the PCB or the netlist refdes; a schematic
    sheet's logical refdes is accepted when neither matches.
    """
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, _, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    instances = index.component_detail(refdes)
    if not instances:
        try:
            regex = re.compile(refdes, re.IGNORECASE)
            candidates = sorted(
                {
                    row["refdes"]
                    for row in index.db.execute(
                        "SELECT DISTINCT refdes FROM components "
                        "WHERE refdes IS NOT NULL"
                    )
                    if regex.search(row["refdes"])
                }
            )[:10]
        except re.error:
            candidates = []
        return json.dumps(
            {
                "error": "component_not_found",
                "message": f"'{refdes}' not on the PCB.",
                "suggestions": candidates,
            }
        )
    _annotate_dnp(instances)
    for instance in instances:
        _annotate_dnp(instance["nearest_components"])
    return json.dumps(
        _convert_units({"instances": instances}, to_mm),
        indent=2,
    )



# ---------- design rules, classes, differential pairs ----------

MAX_RULE_RESULTS = 60
MAX_CLASS_RESULTS = 200
MAX_CLASS_MEMBERS = 200
MAX_DIFF_PAIRS = 100


def _design_rule_counts(index) -> dict:
    rules = index.pcb.rules
    return {
        "rules": sum(1 for rule in rules if rule.kind != ROOM_KIND),
        "disabled_rules": sum(
            1 for rule in rules if rule.kind != ROOM_KIND and not rule.enabled
        ),
        "rooms": sum(1 for rule in rules if rule.kind == ROOM_KIND),
        "net_classes": sum(
            1
            for pcb_class in index.pcb.classes
            if pcb_class.kind == "net" and not pcb_class.superclass
        ),
        "diff_pairs": len(index.pcb.diff_pairs),
    }


def _rule_stale(index, rule) -> list[str]:
    stale = stale_references(rule.scope1, index.rule_context)
    if rule.kind in BINARY_KINDS:
        stale += stale_references(rule.scope2, index.rule_context)
    return list(dict.fromkeys(stale))


def _rule_detail(index, rule) -> dict:
    detail = rule_header(rule)
    values = rule_values(
        rule,
        index.signal_layers(),
        index.pcb.board.substacks,
        profile_refs(index.pcb.board),
    )
    if values:
        detail["values"] = values
    stale = _rule_stale(index, rule)
    if stale:
        detail["stale_references"] = stale
    return detail


def _page(items: list, offset: int, limit: int) -> tuple[list, dict]:
    offset = max(0, offset)
    page = items[offset : offset + limit]
    meta = {"total": len(items), "offset": offset, "has_more": offset + limit < len(items)}
    if meta["has_more"]:
        meta["next_offset"] = offset + limit
    return page, meta


def _get_design_rules_impl(index, kind, name, offset, to_mm) -> dict:
    rules = sorted(index.pcb.rules, key=lambda rule: (rule.kind, rule.priority))
    if name:
        matches = [rule for rule in rules if rule.name.lower() == name.lower()]
        if not matches:
            suggestions = [
                rule.name for rule in rules if name.lower() in rule.name.lower()
            ][:10]
            return {
                "error": "rule_not_found",
                "message": f"No design rule named '{name}'.",
                "suggestions": suggestions,
            }
        return {"rules": [_rule_detail(index, rule) for rule in matches]}
    if kind:
        kinds = {rule.kind.lower(): rule.kind for rule in rules}
        if kind.lower() not in kinds:
            return {
                "error": "invalid_rule_kind",
                "message": f"No rules of kind '{kind}' on this board.",
                "available_kinds": sorted(set(kinds.values())),
            }
        selected = [rule for rule in rules if rule.kind == kinds[kind.lower()]]
        page, meta = _page(selected, offset, MAX_RULE_RESULTS)
        return {
            "kind": kinds[kind.lower()],
            **meta,
            "rules": [_rule_detail(index, rule) for rule in page],
        }
    listed = [rule for rule in rules if rule.kind != ROOM_KIND]
    page, meta = _page(listed, offset, MAX_RULE_RESULTS)
    headers = []
    for rule in page:
        header = rule_header(rule)
        stale = _rule_stale(index, rule)
        if stale:
            header["stale_references"] = stale
        headers.append(header)
    kinds_count: dict[str, int] = {}
    for rule in listed:
        kinds_count[rule.kind] = kinds_count.get(rule.kind, 0) + 1
    counts = _design_rule_counts(index)
    return {
        "rule_count": counts.pop("rules"),
        **counts,
        "kinds": kinds_count,
        **meta,
        "rules": headers,
        "hint": (
            "Pass kind (e.g. 'Width') or name for values. Rooms are "
            "RoomDefinition rules: kind='RoomDefinition'."
        ),
    }


@mcp.tool(
    title="Get Design Rules",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_design_rules(
    kind: str | None = None,
    name: str | None = None,
    offset: int = 0,
    units: PcbUnits | None = None,
) -> str:
    """List the PCB design rules saved in the PcbDoc, or show one kind or rule with values.

    With no arguments: rule counts per kind and every rule's name, priority (1 = highest),
    enabled flag and scope query. kind (e.g. 'Clearance', 'Width', 'DiffPairsRouting',
    'RoutingVias', 'RoomDefinition') or name returns full values: widths, gaps, clearances
    including the object-to-object matrix, per-layer values for the board's signal layers.
    stale_references lists scope names that match no net, class, pair or room. Lengths use
    units (default: board display unit). Paginated: pass offset from next_offset."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, _, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    result = _get_design_rules_impl(index, kind, name, offset, to_mm)
    if "error" in result:
        return json.dumps(result)
    return json.dumps(_convert_units(result, to_mm), indent=2)


def _net_or_pair_not_found(index, name: str) -> dict:
    suggestions = [
        net for net in index.pcb.nets if name.lower() in net.lower()
    ][:10] + [
        pair.name
        for pair in index.pcb.diff_pairs
        if name.lower() in pair.name.lower()
    ][:5]
    return {
        "error": "net_not_found",
        "message": f"No PCB net or differential pair named '{name}'.",
        "suggestions": suggestions,
    }


# Rule kinds that apply to a whole xSignal (its routed legs through series
# parts); every other kind applies to its member nets.
XSIGNAL_RULE_KINDS = ("Length", "MatchedLengths")


def _xsignal_classes_of(index, name: str) -> list[str]:
    key = name.lower()
    return [
        pcb_class.name
        for pcb_class in index.pcb.classes
        if pcb_class.kind == "xsignal"
        and not pcb_class.superclass
        and key in {member.lower() for member in pcb_class.members}
    ]


def _get_rules_for_net_impl(index, net, kinds) -> dict:
    target = index.rule_target(net)
    if target is None:
        missing = _net_or_pair_not_found(index, net)
        missing["message"] = (
            f"No PCB net, differential pair or xSignal named '{net}'."
        )
        missing["suggestions"] += [
            xsignal.name
            for xsignal in index.pcb.xsignals
            if net.lower() in xsignal.name.lower()
        ][:5]
        return missing
    ctx = index.rule_context
    also_pair = target.kind == "net" and net.lower() in ctx.pairs
    also_xsignal = target.kind != "xsignal" and net.lower() in ctx.xsignals
    by_kind: dict[str, list] = {}
    for rule in index.pcb.rules:
        if rule.kind != ROOM_KIND:
            by_kind.setdefault(rule.kind, []).append(rule)
    not_for_xsignal: list[str] = []
    if kinds:
        lookup = {kind.lower(): kind for kind in by_kind}
        wanted = (
            list(by_kind)
            if any(kind.lower() == "all" for kind in kinds)
            else [lookup[kind.lower()] for kind in kinds if kind.lower() in lookup]
        )
        unknown = [
            kind for kind in kinds
            if kind.lower() != "all" and kind.lower() not in lookup
        ]
        if unknown and not wanted:
            return {
                "error": "invalid_rule_kind",
                "message": f"No rules of kind {unknown} on this board.",
                "available_kinds": sorted(by_kind),
            }
        if target.kind == "xsignal":
            not_for_xsignal = [
                kind for kind in wanted if kind not in XSIGNAL_RULE_KINDS
            ]
            wanted = [kind for kind in wanted if kind in XSIGNAL_RULE_KINDS]
    else:
        wanted = [kind for kind in NET_RULE_KINDS if kind in by_kind]
        if target.kind == "diff_pair":
            wanted = [
                kind for kind in ("DiffPairsRouting", "MatchedLengths")
                if kind in by_kind
            ]
        elif target.kind == "xsignal":
            wanted = [kind for kind in XSIGNAL_RULE_KINDS if kind in by_kind]

    def describe(rule):
        return _rule_detail(index, rule)

    results = []
    for kind in wanted:
        if kind == "DiffPairsRouting" and target.kind == "net":
            for pair in index.diff_pairs_of(target.name):
                pair_target = index.rule_target(pair["name"], kind="diff_pair")
                resolved = resolve_kind(
                    by_kind[kind], pair_target, index.rule_context, describe
                )
                resolved["diff_pair"] = pair["name"]
                results.append(resolved)
            continue
        results.append(
            resolve_kind(by_kind[kind], target, index.rule_context, describe)
        )
    pair_rules = [
        entry["effective"]["name"]
        for entry in results
        if entry["kind"] == "DiffPairsRouting" and entry.get("effective")
    ]
    if pair_rules:
        for entry in results:
            if entry["kind"] == "Width":
                entry["note"] = (
                    "This net is in a differential pair: Altium checks the "
                    "width of its pair tracks with DiffPairsRouting "
                    f"{', '.join(pair_rules)} (per-layer widths there), not "
                    "with this Width rule."
                )
    result: dict = {"target": target.name, "type": target.kind}
    if target.kind == "net":
        classes = index.net_classes_of(target.name)
        if classes:
            result["net_classes"] = classes
        pairs = index.diff_pairs_of(target.name)
        if pairs:
            result["diff_pairs"] = pairs
    elif target.kind == "diff_pair":
        _, positive, negative = ctx.pairs[target.name.lower()]
        result["positive_net"] = positive
        result["negative_net"] = negative
        result["pair_classes"] = index.pair_classes_of(target.name)
        result["hint"] = (
            "Track widths come from the DiffPairsRouting rule above. Clearance, "
            "via and length rules apply to the member nets: call "
            "get_rules_for_net on each net."
        )
    elif target.kind == "xsignal":
        key = target.name.lower()
        result["nets"] = sorted(
            ctx.net_names.get(member, member) for member in ctx.xsignals.get(key, ())
        )
        xsignal = index.xsignals.get(key)
        if xsignal is not None:
            result["pins"] = list(xsignal.pins)
        classes = _xsignal_classes_of(index, target.name)
        if classes:
            result["xsignal_classes"] = classes
        if not_for_xsignal:
            result["kinds_not_resolved"] = not_for_xsignal
        result["hint"] = (
            "An xSignal is checked by Length and MatchedLengths rules (listed in "
            "rules; check_lengths(target=...) measures it). Width, clearance, "
            "via and layer rules apply to its member nets: call "
            "get_rules_for_net on each net."
        )
    result["rules"] = results
    notes = []
    if also_pair:
        notes.append(
            f"'{net}' is also a differential pair name; its pair rules are "
            "listed under DiffPairsRouting when the net belongs to it, or call "
            "list_diff_pairs."
        )
    if also_xsignal:
        notes.append(
            f"'{net}' is also an xSignal name; call check_lengths(target=...) "
            "for the xSignal's length rules."
        )
    if notes:
        result["note"] = " ".join(notes)
    return result


@mcp.tool(
    title="Get Rules for Net",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_rules_for_net(
    net: str,
    kinds: list[str] | None = None,
    units: PcbUnits | None = None,
) -> str:
    """Resolve which PCB design rules apply to a net (or differential pair) and their values.

    Per rule kind, returns the effective rule (first enabled rule by priority whose scope
    matches), plus higher-priority rules that apply only to some of the net's objects
    (conditions: only its vias, only on a layer, only in a room...). Binary kinds such as
    Clearance list candidates in priority order, since the winner depends on the other
    object; each lists orientations (which side of the rule the net is on, and what the
    other object must be). Diff-pair routing rules are resolved for the pairs the net
    belongs to; a pair net's track width comes from them, not from Width. An xSignal
    name gets its Length and MatchedLengths rules and its member nets.
    kinds narrows the kinds (default: routing-related kinds; ['all'] for every kind).
    Lengths use units (default: board display unit)."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, _, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    result = _get_rules_for_net_impl(index, net, kinds)
    if "error" in result:
        return json.dumps(result)
    rendered = json.dumps(_convert_units(result, to_mm), indent=2)
    if len(rendered) > QUERY_NET_MAX_RESULT_SIZE_CHARS:
        # Keep every rule's identity and the winners' values; drop values of
        # lower-priority binary candidates, which get_design_rules can show.
        for entry in result["rules"]:
            for candidate in entry.get("candidates", [])[1:]:
                candidate.pop("values", None)
        result["values_omitted"] = (
            "Response too large: values are shown only for the first candidate "
            "of each binary kind. Call get_design_rules(name=...) for the rest."
        )
        rendered = json.dumps(_convert_units(result, to_mm), indent=2)
    return rendered


def _class_members(index, pcb_class, offset: int) -> dict:
    if pcb_class.superclass:
        return {"members": f"implicit: every {pcb_class.kind} on the board"}
    members, meta = _page(pcb_class.members, offset, MAX_CLASS_MEMBERS)
    ctx = index.rule_context
    unresolved: list[str] = []
    if pcb_class.kind == "net":
        rendered = members
        unresolved = [
            member for member in pcb_class.members
            if member.lower() not in ctx.net_names
        ]
    elif pcb_class.kind == "diff_pair":
        rendered = []
        for member in members:
            pair = ctx.pairs.get(member.lower())
            if pair:
                rendered.append(
                    {"pair": pair[0], "positive_net": pair[1], "negative_net": pair[2]}
                )
            else:
                rendered.append({"pair": member})
        unresolved = [
            member for member in pcb_class.members
            if member.lower() not in ctx.pairs
        ]
    elif pcb_class.kind == "component":
        rendered = []
        for member in members:
            found = index.find_by_designator(member)
            record = {"refdes": member}
            if found and found[0].get("sch_refdes"):
                record["sch_refdes"] = found[0]["sch_refdes"]
            rendered.append(record)
        _annotate_dnp(rendered)
        unresolved = [
            member for member in pcb_class.members
            if not index.find_by_designator(member)
        ]
    else:
        rendered = members
    result = {"member_count": len(pcb_class.members), **meta, "members": rendered}
    if unresolved:
        result["unresolved_members"] = unresolved[:MAX_CLASS_MEMBERS]
    return result


@mcp.tool(
    title="Get Classes",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_classes(
    kind: str | None = None,
    name: str | None = None,
    offset: int = 0,
) -> str:
    """List the PCB's object classes, or show one class's members.

    kind filters: net, component, diff_pair, pad, polygon, xsignal, layer, from_to,
    design_channel. Without name: each class with its member count; superclasses
    ('All Nets'...) have implicit membership; auto_generated marks Altium's side/board
    component classes. With name: members (component members are PCB designators, with DNP for
    the active variant; unresolved_members match no net, pair or designator). Paginated:
    pass offset from next_offset."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    valid_kinds = sorted(set(CLASS_KINDS.values()))
    if kind and kind.lower() not in valid_kinds:
        return json.dumps({
            "error": "invalid_class_kind",
            "message": f"Unknown class kind '{kind}'.",
            "available_kinds": valid_kinds,
        })
    classes = [
        pcb_class for pcb_class in index.pcb.classes
        if not kind or pcb_class.kind == kind.lower()
    ]
    if name:
        matches = [c for c in classes if c.name.lower() == name.lower()]
        if not matches:
            return json.dumps({
                "error": "class_not_found",
                "message": f"No class named '{name}'.",
                "suggestions": [
                    c.name for c in classes if name.lower() in c.name.lower()
                ][:10],
            })
        return json.dumps(
            {
                "classes": [
                    {
                        "name": c.name,
                        "kind": c.kind,
                        **({"superclass": True} if c.superclass else {}),
                        **({"auto_generated": True} if c.auto_generated else {}),
                        **_class_members(index, c, offset),
                    }
                    for c in matches
                ]
            },
            indent=2,
        )
    listing = []
    for c in classes:
        entry = {"name": c.name, "kind": c.kind}
        if c.superclass:
            entry["superclass"] = True
        else:
            entry["member_count"] = len(c.members)
        if c.auto_generated:
            entry["auto_generated"] = True
        listing.append(entry)
    page, meta = _page(listing, offset, MAX_CLASS_RESULTS)
    kinds_count: dict[str, int] = {}
    for c in classes:
        kinds_count[c.kind] = kinds_count.get(c.kind, 0) + 1
    return json.dumps({"kinds": kinds_count, **meta, "classes": page}, indent=2)


def _list_diff_pairs_impl(index, pattern, offset) -> dict:
    pairs = index.pcb.diff_pairs
    if pattern:
        if not _safe_regex(pattern):
            return {"error": "invalid_pattern", "message": f"Invalid regex: {pattern}"}
        pairs = [
            pair for pair in pairs
            if re.search(pattern, pair.name, re.IGNORECASE)
            or re.search(pattern, pair.positive_net, re.IGNORECASE)
            or re.search(pattern, pair.negative_net, re.IGNORECASE)
        ]
    routing_rules = [
        rule for rule in index.pcb.rules if rule.kind == "DiffPairsRouting"
    ]
    page, meta = _page(pairs, offset, MAX_DIFF_PAIRS)
    rendered = []
    for pair in page:
        positive_length = index.routed_length(pair.positive_net)
        negative_length = index.routed_length(pair.negative_net)
        entry: dict = {
            "name": pair.name,
            "positive_net": pair.positive_net,
            "negative_net": pair.negative_net,
        }
        classes = index.pair_classes_of(pair.name)
        if classes:
            entry["classes"] = classes
        lengths = pair_lengths(index, pair.name)
        if "error" not in lengths:
            entry["length_mil"] = {
                "positive": lengths["positive"].get("length_mil"),
                "negative": lengths["negative"].get("length_mil"),
            }
            if lengths.get("skew_mil") is not None:
                entry["length_skew_mil"] = lengths["skew_mil"]
            if lengths.get("status") != "ok":
                entry["length_status"] = lengths.get("status")
        if positive_length is not None and negative_length is not None:
            entry["routed_length_mil"] = {
                "positive": positive_length,
                "negative": negative_length,
            }
        if routing_rules:
            resolved = resolve_kind(
                routing_rules,
                index.rule_target(pair.name, kind="diff_pair"),
                index.rule_context,
                rule_header,
            )
            effective = resolved.get("effective")
            entry["routing_rule"] = effective["name"] if effective else None
            if resolved.get("overridden_for_some_objects_by"):
                entry["routing_rule_conditional_overrides"] = [
                    rule["name"] for rule in resolved["overridden_for_some_objects_by"]
                ]
        rendered.append(entry)
    return {
        **meta,
        "diff_pairs": rendered,
        "note": (
            "length_mil is the copper path pad centre to pad centre plus the via "
            "barrel over the layers it traverses plus the end pads' pin/package "
            "length, like Altium's Signal Length (multi-pad legs: routed copper "
            "plus barrels, no pin/package length). routed_length_mil "
            "is all track and arc copper of each leg, including stubs. Use "
            "check_diff_pairs for skew against the in-pair rule and "
            "get_rules_for_net for the routing rule's gap and widths."
        ),
    }


@mcp.tool(
    title="List Differential Pairs",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def list_diff_pairs(
    pattern: str | None = None,
    offset: int = 0,
    units: PcbUnits | None = None,
) -> str:
    """List the PCB's differential pairs with nets, classes, routed length skew and routing rule.

    pattern is a case-insensitive regex on the pair or net names. Lengths use units
    (default: board display unit). Paginated: pass offset from next_offset."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, _, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    result = _list_diff_pairs_impl(index, pattern, offset)
    if "error" in result:
        return json.dumps(result)
    return json.dumps(_convert_units(result, to_mm), indent=2)


# ---------- rule compliance, lengths, diff-pair checks ----------

def _split_names(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


COMPLIANCE_GROUP_LIMIT = 50


def _trim_violation_groups(result: dict, offset: int, limit: int) -> None:
    """Cut every kind's violation groups to limit, keeping paging consistent."""
    for entry in result.get("kinds", []):
        groups = entry.get("violations")
        if not isinstance(groups, list) or len(groups) <= limit:
            continue
        entry["violations"] = groups[:limit]
        entry["has_more"] = True
        entry["next_offset"] = offset + limit


@mcp.tool(
    title="Check Rule Compliance",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_rule_compliance(
    net: str | None = None,
    kinds: list[str] | None = None,
    clearance_spot_check: bool = False,
    offset: int = 0,
    units: PcbUnits | None = None,
) -> str:
    """Check the saved copper against the design rules: track widths per layer (pair
    nets against their DiffPairsRouting widths), via diameter/hole, via hole size and
    annular ring, routing layers and Length windows, resolving the rule for EACH object
    (room, layer and via scopes decided per object).

    net is a net or diff-pair name (comma-separated for several); omit for the whole
    board. kinds narrows to Width, DiffPairsRouting, RoutingVias, HoleSize,
    MinimumAnnularRing, RoutingLayers, Length, Clearance. Diff-pair nets' track widths
    are checked under DiffPairsRouting, not Width (widths only; uncoupled length is not
    checked). clearance_spot_check adds an approximate clearance check for up to 10
    nets. Violations are grouped with one example location, worst first, up to 50
    groups per kind; if a kind has has_more true, call again with kinds=[that kind]
    and offset=next_offset (offset skips that many groups of every kind).
    checked_by_batch_drc says whether the board's own batch DRC checks that kind.
    Lengths use units (default: board display unit). This reads the saved file, not
    Altium's DRC report."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, _, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    offset = max(0, offset)
    result = check_compliance(
        index,
        _split_names(net),
        kinds,
        length_of=lambda name: net_length(index, name),
        clearance=clearance_spot_check,
        limit=COMPLIANCE_GROUP_LIMIT,
        offset=offset,
    )
    if "error" in result:
        return json.dumps(result)
    # A board-wide call with many failing kinds: list fewer groups per kind
    # until the answer fits; counts and summaries stay complete.
    rendered = json.dumps(_convert_units(result, to_mm), indent=2)
    for limit in (20, 10, 3, 1):
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
        _trim_violation_groups(result, offset, limit)
        result["violations_per_kind_limited_to"] = limit
        result["hint"] = (
            "Response trimmed to fit: call check_rule_compliance(kinds=[<kind>], "
            "offset=<next_offset>) to page one kind's violation groups."
        )
        rendered = json.dumps(_convert_units(result, to_mm), indent=2)
    return rendered


@mcp.tool(
    title="Check Lengths",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_lengths(
    rule: str | None = None,
    target: str | None = None,
    offset: int = 0,
    units: PcbUnits | None = None,
) -> str:
    """Evaluate Length and MatchedLengths rules on the saved copper, as Altium does:
    Length windows per object; matched-length groups (xSignals, pairs or nets) against
    the longest member or the target source; within-pair skew. Lengths are the copper
    path pad to pad plus the via barrel over the layers traversed plus the end pads'
    pin/package length (xSignals through series parts, pin/package length at the end
    pins); nets without exactly 2 pads use routed copper plus barrels. A pair's group
    length is the P/N average.

    rule picks one rule; target (net, pair, xSignal or xSignal class) shows the rules
    covering it; with neither, every rule is listed but only members whose status is
    not ok are shown (summaries count all members). Stale scopes,
    conditional members and approximations are reported. Paginated per rule: pass
    offset from members_page.next_offset. Lengths use units (default: board display
    unit)."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, _, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    # Big boards with many failing groups: shrink the members listed per rule
    # until the answer fits; every rule's summary and pagination stay intact.
    for limit in (50, 10, 3, 1):
        result = check_length_rules(
            index, rule=rule, target=target, offset=offset, limit=limit
        )
        if "error" in result:
            return json.dumps(result)
        if limit < 50:
            result["members_per_rule_limited_to"] = limit
            result["hint"] = (
                "Response trimmed to fit: call check_lengths(rule=...) for one rule's "
                "members, paging with offset."
            )
        rendered = json.dumps(_convert_units(result, to_mm), indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


@mcp.tool(
    title="Get Signal Length",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_signal_length(
    name: str,
    to_pin: str | None = None,
    units: PcbUnits | None = None,
) -> str:
    """Measure one signal on the saved copper: a net (2 pads: pad-to-pad path; more:
    routed copper plus via barrels), a differential pair (both legs and skew) or an
    xSignal (legs through series parts). With to_pin, name is a pin like 'U1-5' and the
    path between the two pins is measured. Via barrels are included. Pin/package length
    stored on the end pads is added to pad-to-pad paths and at an xSignal's end pins,
    like Altium's Signal Length; routed copper of multi-pad nets has none, like Routed
    Length. Lengths use units (default: board display unit)."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, _, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    ctx = index.rule_context
    key = name.lower()
    if to_pin:
        result = pad_to_pad(index, None, name, to_pin)
    elif key in ctx.net_names:
        result = net_length(index, ctx.net_names[key])
    elif key in ctx.pairs:
        result = pair_lengths(index, ctx.pairs[key][0])
    elif key in ctx.xsignals:
        result = xsignal_length(index, name)
    else:
        result = _net_or_pair_not_found(index, name)
        result["message"] = f"No PCB net, differential pair or xSignal named '{name}'."
    if "error" in result:
        return json.dumps(result)
    return json.dumps(_convert_units(result, to_mm), indent=2)


@mcp.tool(
    title="Check Differential Pairs",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_diff_pairs(
    pattern: str | None = None,
    checks: list[str] | None = None,
    offset: int = 0,
    units: PcbUnits | None = None,
) -> str:
    """Check differential-pair consistency: undeclared P/N net pairs (naming), polarity
    swaps at component pins (with a hint when the interface tolerates inversion), series
    parts and AC coupling symmetric on both legs (value, footprint, DNP in the active
    variant), and in-pair skew against within-pair MatchedLengths rules.

    pattern is a regex on pair or net names; checks narrows to naming, polarity, series,
    skew. Findings are paginated (offset from next_offset), warnings first. Lengths use
    units (default: board display unit)."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, _, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    _, netlist, variant_state = _require_project()
    result = run_diff_pair_checks(
        index,
        netlist,
        is_dnp=variant_state.is_dnp,
        checks=checks,
        pattern=pattern,
        pair_lengths=lambda pair: pair_lengths(index, pair),
        offset=offset,
    )
    if "error" in result:
        return json.dumps(result)
    return json.dumps(_convert_units(result, to_mm), indent=2)


def _sheets_saved_after(project: dict, saved: float) -> list[str]:
    """The project's sheets saved after the given time (a missing sheet is skipped)."""
    newer = []
    for sheet in project.get("sheets", []):
        try:
            if Path(sheet["path"]).stat().st_mtime > saved:
                newer.append(sheet["name"])
        except OSError:
            continue
    return newer


def _saved_after_netlist(project: dict, compiled: float) -> tuple[list[str], list[str]]:
    """Files saved after the netlist was compiled, as refresh_netlist judges them (the
    .PrjPcb, its sheets and .Annotation files, other sheets in those folders): sheet
    names, and the other file names."""
    prj_path = project.get("prj_pcb_path")
    try:
        prj = parse_prj_pcb(prj_path)
        sources = netlist_sources(prj_path, prj.sheet_paths, prj.annotation_paths)
    except Exception:
        sources = [Path(sheet["path"]) for sheet in project.get("sheets", [])]
    sheets: list[str] = []
    others: list[str] = []
    seen: set[str] = set()
    for source in sources:
        source = Path(source)
        key = os.path.normcase(str(source.resolve()))
        if key in seen:
            continue
        seen.add(key)
        try:
            if source.stat().st_mtime <= compiled:
                continue
        except OSError:
            continue
        if source.suffix.lower() == ".schdoc":
            sheets.append(source.stem)
        else:
            others.append(source.name)
    return sheets, others


def _utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


@mcp.tool(
    title="Check Schematic-PCB Sync",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_sch_pcb_sync(checks: list[str] | None = None, offset: int = 0) -> str:
    """Compare the schematic with the saved PCB offline, like Altium's Project > Show
    Differences: parts missing from or extra on the PCB, designator, footprint and comment
    differences, pads whose net differs from their pin's, renamed nets, nets on one side
    only, and schematic pins whose placed footprint has no pad (Update PCB stops with
    'Unknown Pin'). Parts link by unique ID, then designator; nets by shared members, then
    name (the project's Comparator settings). The schematic side is the compiled netlist
    from the last load or refresh_netlist; netlist_stale lists sheets saved after it was
    compiled, whose changes are not compared until refresh_netlist. in_sync covers every
    check, also those left out by checks.

    checks narrows to components, pins, nets. Findings are paginated (offset from
    next_offset), errors first; each names the Altium difference type in altium_names."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    project, netlist, variant_state = _require_project()
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    pcb_path = Path(project["pcb_doc_paths"][0])
    pcb_saved = pcb_path.stat().st_mtime
    newer = _sheets_saved_after(project, pcb_saved)
    compiled = netlist.get("net_file_mtime")
    stale, stale_others = _saved_after_netlist(project, compiled) if compiled else ([], [])
    options = net_matching_options(project.get("prj_pcb_path"))
    for limit in (100, 30, 10):
        result = run_sync_check(
            index,
            netlist,
            is_dnp=variant_state.is_dnp,
            checks=checks,
            offset=offset,
            limit=limit,
            sheets_saved_after_pcb=newer,
            net_matching=options,
        )
        if "error" in result:
            return json.dumps(result)
        context: dict = {
            "pcb_doc": pcb_path.name,
            "pcb_saved_utc": _utc(pcb_saved),
            "netlist_updated_utc": _netlist_last_updated,
        }
        if compiled:
            context["netlist_compiled_utc"] = _utc(compiled)
        if stale or stale_others:
            context["netlist_stale"] = True
            if stale:
                context["sheets_saved_after_netlist"] = stale
            if stale_others:
                context["other_files_saved_after_netlist"] = stale_others
            context["hint"] = (
                "These files were saved after the netlist was compiled: their changes "
                "are not compared. Call refresh_netlist, then check again."
            )
        context["sheets_saved_after_pcb"] = newer
        if netlist.get("sheet_errors"):
            context["sheet_errors"] = netlist["sheet_errors"][:20]
        result = {**context, **result}
        if len(project["pcb_doc_paths"]) > 1:
            result["note"] = (
                f"Project lists {len(project['pcb_doc_paths'])} PcbDocs; compared with "
                f"'{pcb_path.name}'."
            )
        if limit < 100:
            result["findings_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


DRC_NOT_READ = (
    "This is Altium's last Batch DRC run, not a check of the board as saved now "
    "(check_rule_compliance and check_lengths measure the saved copper). ERC (the "
    "Messages panel), OutJob validation outputs and the DRC XML report are not read."
)


@mcp.tool(
    title="Get DRC Report",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_drc_report(
    group_by: Literal["rule", "component", "net", "part"] = "rule",
    rule: str | None = None,
    offset: int = 0,
    units: PcbUnits | None = None,
) -> str:
    """Read Altium's last Batch DRC report for the project's board ('Design Rule Check -
    <PcbDoc>.html' or '.drc' in the project's output folder): violations grouped by rule,
    component or net (group_by), each linked to its components, nets and board location;
    PCB health issues, waived violations and the report's warnings. The assessment comes
    first and says whether to trust the report: incomplete, the DRC stopped at its
    violation limit (later rules, shorts included, then read 0), Un-Routed Net or
    Short-Circuit not checked, older than the board or an Update PCB ran since, made for
    another board, or rules changed since.

    rule (a rule description or part of it, a rule kind such as Clearance, or a health,
    waived or warning title) lists all its violations. Groups and violations are
    paginated (offset from next_offset). location_* is absolute (board origin in
    origin_*, the frame query_pcb_region takes); a message's own coordinates are relative
    to the origin. Locations use units (default: board display unit)."""
    try:
        project, netlist, _ = _require_project()
    except ValueError as error:
        return json.dumps({"error": "no_project", "message": str(error)})
    if group_by == "part":
        group_by = "component"
    if group_by not in ("rule", "component", "net"):
        return json.dumps({
            "error": "invalid_group_by",
            "message": "group_by is one of rule, component, net.",
        })
    paths = project.get("pcb_doc_paths") or []
    if not paths:
        return json.dumps({
            "error": "no_pcb_document",
            "message": "The project lists no .PcbDoc, so there is no DRC report to read.",
        })
    pcb_path = Path(paths[0])
    report, report_path, others, searched, notes = load_report(project, pcb_path)
    if report is None:
        return json.dumps({
            "error": "no_drc_report",
            "message": (
                f"No 'Design Rule Check - {pcb_path.stem}.html' or '.drc' was found. In "
                "Altium run Tools > Design Rule Check with 'Create Report File' on; the "
                "report goes to the project's output folder."
            ),
            "searched": searched,
        })
    index = None
    if pcb_path.is_file():
        index, _ = _pcb_session.get(project, netlist)
    to_mm = units == "mm"
    if index is not None:
        to_mm, _, units_error = _pcb_units_or_error(index, units)
        if units_error:
            return json.dumps(units_error)
    pcb = index.pcb if index is not None else None
    assessment = notes + assess(report, report_path, pcb_path, project, pcb)
    linker = DrcLinker(index)
    options = getattr(pcb, "drc_options", None)
    folder = Path(project["prj_pcb_path"]).parent
    header: dict = {
        "report": {
            "file": str(report_path.relative_to(folder))
            if report_path.is_relative_to(folder) else str(report_path),
            "format": report.format,
            "made_for": re.split(r"[\\/]", report.pcb_file)[-1] if report.pcb_file else None,
            "generated": " ".join(part for part in (report.date, report.time) if part),
            "saved_utc": _utc(report_path.stat().st_mtime),
            "elapsed": report.elapsed,
        },
        "pcb_doc": pcb_path.name,
        "pcb_saved_utc": _utc(pcb_path.stat().st_mtime) if pcb_path.is_file() else None,
        "totals": report.totals,
    }
    if pcb is not None:
        header["origin_mil"] = {
            "x_mil": pcb.board.origin_x or 0.0, "y_mil": pcb.board.origin_y or 0.0,
        }
    if options is not None:
        header["drc_settings"] = {
            "stop_after_violations": options.max_violations,
            "create_report_file": options.make_report,
            "report_health_issues": options.include_health,
        }
    if index is None:
        header["note"] = (
            "The PcbDoc could not be read: violations are not linked to components, board "
            "copper or locations; nets and pairs shown come only from the message text."
        )
        if group_by != "rule":
            header["grouping_note"] = (
                f"Grouping by {group_by} needs the PcbDoc; grouped by rule instead."
            )
            group_by = "rule"
    header["assessment"] = assessment
    for limit in (None, 15, 5):
        body = summarize(report, linker, group_by=group_by, rule=rule, offset=offset,
                         limit=limit)
        if "error" in body:
            return json.dumps(body)
        result = {**header, **body, **other_sections(report, linker, limit=limit or 20)}
        if others:
            result["other_reports"] = [path.name for path in others]
        result["not_read"] = DRC_NOT_READ
        if limit:
            result["trimmed_to"] = limit
        rendered = json.dumps(_convert_units(result, to_mm), indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered



SCHEMATIC_REVIEW_PROMPT = """\
Project: {name}
Sheets: {sheets}
Active variant: {active_variant}

You are starting a structured schematic review. Ask the user to choose
a scope before doing anything:
  - A specific sheet (e.g. "{first_sheet}")
  - The full project ({n} sheets, one at a time)

For large projects, consider reviewing each sheet as an independent task dispatched
in parallel — one agent per sheet or functional block. Each agent can self-initialize:
set_project_dir("{root_dir}") → set_active_variant("{active_variant}") →
get_sheet_context(sheet_name="<assigned sheet>"), then run Phases 1–2 and return
their tables. Merge all results into a single Phase 3 report with an added "Sheet" column.

Do not proceed until the user answers.

---

### Phase 1 — Understand Before You Judge

Call get_sheet_context. Each pin already includes connected_to —
the full cross-sheet neighbor list. Use this to trace every signal
source-to-destination. Call query_net only if you need to go two hops deep on a specific net.

Its design_notes block holds the designer's notes for the sheet. Treat them as stated
intent to verify: a note that contradicts the circuit (a value, a part number, a DNP
reason) is a finding. Look at pictures with get_design_image when they may hold the
reference design or datasheet excerpt the circuit was built from.

For each IC that has external components influencing its operating
behavior, look up its manufacturer datasheet and confirm the parameters
those components set. The datasheet application circuit will tell you
which ones matter.

If you cannot find a datasheet for an IC, say so before proceeding —
do not assume pin functions or parameter values.

Then state explicitly:
  - What you believe this circuit does
  - What each key IC's operating parameters are, sourced from its datasheet
  - Anything you cannot determine from the netlist or datasheets —
    name it, ask about it

Done when: the user has confirmed or corrected your understanding.
Do not proceed to Phase 2 until then.

---

### Phase 2 — Audit (Evidenced Issues Only)

Assume the designer made mistakes. Your job is to find them.

Ground truth rule: the manufacturer datasheet overrides everything —
Altium pin names, net labels, user descriptions. Every finding must
trace to a datasheet value or a measurable netlist fact.
Report only what you can evidence. Do not speculate.

At minimum, check:
1. Pin assignments — does each IC pin connect to what the datasheet
   application circuit requires?
2. Passive values — do component values produce correct operating
   parameters per datasheet formulas?
3. Signal continuity — use connected_to on each critical pin to trace
   source-to-destination; verify correct termination and that every
   expected endpoint appears
4. Cross-sheet nets — use query_net with broad patterns to enumerate
   all cross-sheet references; flag dangling or unexpected appearances
5. Failure modes — for each sub-circuit: what happens if inputs are out
   of range, or a passive is off by 10%?

For circuit types that require it, apply first principles beyond this
list. The topology determines what matters.

Done when: you have worked through the checklist and any additional
checks the circuit demands. Do not proceed to Phase 3 until then.

---

### Phase 3 — Document

Present findings as three Markdown tables:

  Table 1 — Critical Issues
  | Component | Issue | Datasheet Requires | Schematic Shows |

  Table 2 — Warnings & Nitpicks
  | Component | Observation |

  Table 3 — Verified Critical Nets
  | Net | Source → Destination | Result |

Ask: "Any sub-circuits to dive deeper into, or alternative
architectures to brainstorm?"
"""


@mcp.tool(title="Constraints Review", annotations=ToolAnnotations(readOnlyHint=True))
def constraints_review() -> str:
    """Start a structured review of the PCB's high-speed constraints: finds the board's
    interfaces from parts and pins (USB, PCIe, Ethernet, DDR, HDMI/DP, MIPI, LVDS, SATA,
    CAN, RS-485, SerDes), checks pairs, classes, impedance-driven rules, matched-length
    groups, clearances, reference layers and stale rules against each chip's layout
    guide, then spot-checks the saved copper. Call when the user asks to review or verify
    the layout's constraints, design rules or high-speed routing ('check my high-speed
    rules', 'are my DDR length rules right?', 'review the diff pairs before fab'). Not for
    one net's rule (get_rules_for_net) or a schematic review (schematic_review)."""
    if _project is None or _altium._netlist is None or _variant_state is None:
        return json.dumps({
            "error": "no_project",
            "message": (
                "No project loaded. Run the session-start flow first: "
                "detect_altium_project → set_project_dir → list_variants → set_active_variant."
            ),
        })
    index, error = _pcb_index_or_error()
    if error:
        error["hint"] = (
            "constraints_review needs the project's PcbDoc; for the circuit itself "
            "use schematic_review."
        )
        return json.dumps(error)
    return build_constraints_review(_project, _variant_state.active.name, index)


@mcp.tool(title="Schematic Review", annotations=ToolAnnotations(readOnlyHint=True))
def schematic_review() -> str:
    """Start a structured schematic review. Call when the user explicitly asks to review
    or verify the schematic (e.g. 'review this', 'check my schematic', 'do a design review',
    'is this correct?', 'does this look right?'). Do not call for general questions."""
    if _project is None or _altium._netlist is None or _variant_state is None:
        return json.dumps({
            "error": "no_project",
            "message": (
                "No project loaded. Run the session-start flow first: "
                "detect_altium_project → set_project_dir → list_variants → set_active_variant."
            ),
        })

    sheets = [s["name"] for s in _project["sheets"]]
    active_variant = _variant_state.active.name
    first_sheet = sheets[0] if sheets else "Power_Supply"
    n = len(sheets)
    name = _project["name"]

    return SCHEMATIC_REVIEW_PROMPT.format(
        name=name,
        sheets=", ".join(sheets),
        active_variant=active_variant,
        first_sheet=first_sheet,
        n=n,
        root_dir=_project["root_dir"],
    )



BRAINSTORM_CIRCUITS_PROMPT = """\
You are starting a structured circuit brainstorming session.
Follow these phases in order. Ask one question at a time — never more than one per message.

## Ground Rules

- Do not state component values, voltage ratings, current limits, pin assignments, or any other
  specs from memory. If you need to reference a specific part, use web search to find its datasheet first.
- Do not recommend a specific component unless you have verified its key specs from a datasheet
  or authoritative source during this session.
- If you are uncertain about a topology's behavior or limitations, say so and search before advising.

---

## Phase 0 — Context (skip if no project is loaded)

If a project is loaded, use the available tools before asking the user anything:
- Call get_sheet_context on the active or most relevant sheet.
- If the user mentioned a specific sub-circuit or net, call query_net and get_component to understand it.
- Summarize what is already on the relevant sheet(s) in one short paragraph: what the circuit does,
  key components, and any obvious constraints visible in the design.

If no project is loaded, skip to Phase 1.

---

## Phase 1 — Problem statement

Ask the user one open-ended question: "What does this circuit need to do?"

Do not assume a topology, domain, or electrical spec yet. If the answer is vague or could be
interpreted multiple ways, name what is confusing and ask again before moving on.

---

## Phase 2 — Constraints (one question at a time)

Based on the circuit type that emerged from Phase 1, identify which specs would most change your
topology recommendation. Ask about them one at a time — do not send a list of questions.

Stop asking once you have enough to propose meaningful options. State any assumptions you are
making before moving to Phase 3.

---

## Phase 3 — Propose 2–3 topologies

Lead with the simplest viable option. For each topology:
- Name it clearly.
- List key tradeoffs: complexity, cost, parts count, performance.
- Give one reason to choose it and one reason to skip it.

State your assumptions explicitly. Do not recommend an approach before completing Phase 2.

---

## Phase 4 — Fit check (skip if no project is loaded)

Before the user commits to an approach, check the existing design:
- Use search_components to find components that might already serve a similar role.
- Use query_net to check whether relevant power rails or signal nets already exist.

Do not guess from memory — verify with tools. If the design already has something relevant, say so.

---

## Phase 5 — Design summary

Once the user selects an approach:
- Summarize: topology chosen, key specs agreed, suggested components with brief reasoning.
- List any open questions that remain before layout can begin.

Do not suggest implementation steps or specific part numbers until the user approves this summary.
"""


@mcp.tool(title="Brainstorm Circuits", annotations=ToolAnnotations(readOnlyHint=True))
def brainstorm_circuits() -> str:
    """Start a structured circuit brainstorming session. Call when the user is asking
    how to design, improve, or choose an approach for a circuit — not just when they
    say "brainstorm". Works with or without a loaded project."""
    return BRAINSTORM_CIRCUITS_PROMPT


# ---------- package_for_xfn ----------

def _package_for_xfn_impl(
    project: dict,
    netlist: dict,
    variant_state,
    version: str,
    notes_cache: DesignNotesCache | None = None,
) -> str:
    # Never ship a hollow snapshot: nets present but no component metadata.
    no_components = netlist.get("pin_to_net") and _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    notes, notes_errors = note_rows(notes_cache, project) if notes_cache is not None else ([], [])
    try:
        db_path = export_project(project, netlist, variant_state, version, notes)
    except Exception as e:
        return json.dumps({"error": "export_failed", "message": str(e)})
    prj_pcb_name = Path(project["prj_pcb_path"]).name
    incomplete_warning = "".join(
        f"\nWarning: sheet '{err['sheet']}' failed to load ({err['error']}); "
        "its components are missing from this snapshot."
        for err in netlist.get("sheet_errors") or []
    )
    incomplete_warning += "".join(
        f"\nWarning: the notes of {err['document_kind']} '{err['document']}' could not be read "
        f"({err['error']}); they are missing from this snapshot."
        for err in notes_errors
    )
    registry_warning = ""
    try:
        mark_xfn_exported(prj_pcb_name)
    except Exception as error:
        logging.warning(
            "Snapshot exported but registry update failed: %s",
            error,
        )
        registry_warning = (
            "\nWarning: snapshot export succeeded, but the local registry "
            f"could not be updated: {error}"
        )
    return (
        f"Exported to: {db_path}\n"
        "Share this file with your cross-functional team via Slack or a shared drive.\n"
        "Do not commit it to Git — it is a binary snapshot and will cause repository bloat."
        f"{incomplete_warning}"
        f"{registry_warning}"
    )


@mcp.tool(
    title="Package for Cross-Functional Team",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
)
def package_for_xfn() -> str:
    """Export the current schematic project to a portable .db snapshot file for the
    cross-functional team. Only call this tool when the user explicitly asks to package
    or export the project for sharing with firmware, mechanical, test, or reliability
    engineers. Do not call speculatively."""
    try:
        project, netlist, variant_state = _require_project()
    except ValueError:
        return json.dumps({
            "error": "no_project",
            "message": "No project loaded. Ask the user to open a project in Altium first.",
        })
    return _package_for_xfn_impl(project, netlist, variant_state, _read_version(), _design_notes)


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(f"altium-copilot v{_read_version()}")  # noqa: T201
        sys.exit(0)
    threading.Thread(target=_check_for_update, args=(_read_version(),), daemon=True).start()
    try:
        mcp.run()
    finally:
        # After the MCP stdio transport closes, Python's exit sequence tries to
        # flush sys.stdout, which is now a closed pipe → ValueError.  Redirect to
        # devnull so the process exits cleanly and Claude Desktop doesn't see a
        # crashed server on next load.
        try:
            sys.stdout = open(os.devnull, "w")
            sys.stderr = open(os.devnull, "w")
        except Exception:
            pass
