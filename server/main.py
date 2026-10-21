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
from typing import Annotated, Any, Literal

vendor_dir = os.path.join(os.path.dirname(__file__), "vendor")
# When bundled with PyInstaller, vendor is embedded — no sys.path injection needed.
# Only inject when running from source and the vendor dir exists.
if os.path.exists(vendor_dir):
    sys.path.insert(0, vendor_dir)

import httpx
from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations
from pydantic import WithJsonSchema

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
        load_report, assess, summarize, other_sections, DrcLinker, count_problem,
    )
    from services.bom import (
        BASE_VARIANT, build_parts as build_bom_parts, compare_variants as compare_bom_variants,
        group_lines as group_bom_lines, natural_key, needs_mpn, plan_variant, variant_names,
        to_csv as bom_csv,
    )
    from services.bom_checks import run_bom_checks
    from services.decoupling import DEFAULT_MAX_DISTANCE_MIL, run_decoupling_check
    from services.via_checks import page_result as page_via_result, review_vias
    from services.pcb_health import run_health_check
    from services.dfm import (
        DRILL_ROWS_LISTED, SHRINK_STEPS, AssemblyContext, page_dfm, review_dfm,
    )
    from services.manufacturing_review import build_manufacturing_review
    from services.assembly_guide import (
        ROW_LIMITS as GUIDE_ROW_LIMITS, GuideVariant, build_guide,
        page as page_guide, summary as guide_summary,
    )
    from services.fab_profiles import load_profile
    from services.connectivity import (
        check_connectivity as review_connectivity, drc_comparison,
        page_result as page_connectivity_result,
    )
    from services.clone_status import clone_status
    from parsers.pcb_doc import CLASS_KINDS
    from services.design_notes import (
        DesignNotesCache, NOTES_MAX_RESULT_SIZE_CHARS, build_design_notes,
        draftsman_documents, note_rows, resolve_document, sheet_notes_for_context,
    )
    from services.images import (
        MAX_IMAGE_BYTES, MAX_PASSTHROUGH_SIDE_PX, bmp_to_png, image_format, image_size,
    )
    from services.design_hierarchy import build_design_hierarchy, page_design_hierarchy
    from services.project_options import build_project_options, page_project_options
    from services.project_integrity import build_project_integrity, page_project_integrity
    from services.design_diff import (
        DiffError, diff_folders, diff_revisions, page_design_diff,
    )
    from services.project_tour import build_project_tour, defined_variant_count
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
        load_report, assess, summarize, other_sections, DrcLinker, count_problem,
    )
    from server.services.bom import (
        BASE_VARIANT, build_parts as build_bom_parts, compare_variants as compare_bom_variants,
        group_lines as group_bom_lines, natural_key, needs_mpn, plan_variant, variant_names,
        to_csv as bom_csv,
    )
    from server.services.bom_checks import run_bom_checks
    from server.services.decoupling import DEFAULT_MAX_DISTANCE_MIL, run_decoupling_check
    from server.services.via_checks import page_result as page_via_result, review_vias
    from server.services.pcb_health import run_health_check
    from server.services.dfm import (
        DRILL_ROWS_LISTED, SHRINK_STEPS, AssemblyContext, page_dfm, review_dfm,
    )
    from server.services.manufacturing_review import build_manufacturing_review
    from server.services.assembly_guide import (
        ROW_LIMITS as GUIDE_ROW_LIMITS, GuideVariant, build_guide,
        page as page_guide, summary as guide_summary,
    )
    from server.services.fab_profiles import load_profile
    from server.services.connectivity import (
        check_connectivity as review_connectivity, drc_comparison,
        page_result as page_connectivity_result,
    )
    from server.services.clone_status import clone_status
    from server.parsers.pcb_doc import CLASS_KINDS
    from server.services.design_notes import (
        DesignNotesCache, NOTES_MAX_RESULT_SIZE_CHARS, build_design_notes,
        draftsman_documents, note_rows, resolve_document, sheet_notes_for_context,
    )
    from server.services.images import (
        MAX_IMAGE_BYTES, MAX_PASSTHROUGH_SIDE_PX, bmp_to_png, image_format, image_size,
    )
    from server.services.design_hierarchy import build_design_hierarchy, page_design_hierarchy
    from server.services.project_options import build_project_options, page_project_options
    from server.services.project_integrity import (
        build_project_integrity, page_project_integrity,
    )
    from server.services.design_diff import (
        DiffError, diff_folders, diff_revisions, page_design_diff,
    )
    from server.services.project_tour import build_project_tour, defined_variant_count
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

## Sheet Hierarchy

- get_design_hierarchy — "how is the design structured?", "why is this net called X?",
  "how does X get from sheet A to sheet B?", "why isn't this port connected?": the sheet
  tree (sheet symbols, device sheets, Repeat channels), each sheet's ports and its
  symbols' entries, and the Net Identifier Scope with what Automatic resolves to.
  Explain cross-sheet connections from scope.explanation and net names from
  scope.net_naming (name_precedence) plus each port's labels; never assume a scope.
  With a netlist, ports and entries name their net (nets: one per channel). Issues are
  read from the files, not Altium's compiler: quote issue_help and say Compile in
  Altium (Messages panel) is the final word. sheet= narrows to a subtree.

## Project Options

- get_project_options — "what are the project settings?", "is ERC configured right?",
  "why does compile not report X?", "where do outputs go?", "are any No ERCs hiding
  problems?", before an ERC review: the .PrjPcb settings (net scope and naming, diff-pair
  suffixes, class generation, multi-channel naming, ERC levels, Connection Matrix,
  Comparator, output path) and an audit. Quote each audit entry with its default and
  hides/risk; a flagged No ERC is a question for the designer, not a proven fault. Never
  guess what an unmapped key means. The saved file is read: Compile in Altium is final.

## Project Integrity

- check_project_integrity — "Update PCB wants to delete all my parts", "Constraint Manager
  won't save", "why do compiled designators differ from the sheet?", "is anything broken
  in the project files?", after importing or reannotating a design, before a release:
  legacy file extensions, documents missing on disk, a leftover .Annotation file, parts
  outside the sheet, compile masks, stale variant entries, hidden, non-ASCII or edge-space
  characters in names, bad bus ranges, ActiveBOM-reserved 'Manufacturer N' parameters and
  multi-channel formats that break bus names. Errors break compile, sync or save; warnings
  give silently wrong results. Quote each finding's fix and source; ERC and Compile in
  Altium are the final word.

## Project Tour

- project_tour — "walk me through this project", "I'm new to this board", "give me a
  tour", "onboard me", "write a project primer": a phased tour (hierarchy, schematic
  blocks and rails, PCB stackup, placement and key nets, outputs, variants and notes)
  ending in a Markdown project primer. Show the primer; save it only where the user
  agrees. A quick summary needs only the session-start steps.

## Design Diff

- diff_design — "what changed since the last release / commit?", "what did commit X
  change?", "write the release notes", "review this revision against the old one": an
  ECO-style changelog between two revisions of the project's Git clone (default: the
  last commit that changed a design file vs the files on disk; with no revisions it
  lists recent_commits, so offer them) or two folders (an archived release). Quote
  ict_hazards first (nets that kept their name but changed pins, designators now on
  another part): they break test fixtures and programs. Nets come from the PcbDoc: a
  schematic change not yet pushed to the board is not seen (check_sch_pcb_sync).

## Bill of Materials

- get_bom gives the BOM of the active variant (or another, or '[No Variations]'): lines
  with quantity, designators, Comment, footprint, manufacturer and MPN, from the compiled
  parts, without Not Fitted and no-BOM parts. compare_variants shows what each variant
  changes; format csv gives the lines as CSV.
- check_bom before a release or when asked whether the BOM is right. mpn_coverage_percent
  low means the project keeps MPNs elsewhere (e.g. ActiveBOM, not read): say so instead
  of listing every part as missing one.
- get_component's mpn falls back to Comment; for part numbers in a BOM use get_bom.
  A line's varies lists values its parts disagree on (varies_count when there are
  more): mention them, the line is not one part.

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
15. check_decoupling — "are my ICs decoupled?", "is C12 close enough to U3?", decoupling
    or bypass capacitors in a schematic or layout review: rails with no capacitor to
    ground, only Not Fitted ones, or behind a ferrite without their own; with the PcbDoc,
    each IC rail's nearest capacitor and a warning above 5 mm (name the IC in ic= for
    its 5 nearest). It is advice: rails come from net and pin names, and the datasheet
    decides how many capacitors a part needs.
16. check_vias — "what vias does this board use?", a via or drill review (a fab's
    capability limits: check_dfm), aspect ratio, annular ring, via-in-pad, tenting,
    blind/buried/microvias, back drilling or via stubs: via types with counts per type
    and net class, depth and aspect ratio from the stack, drill pairs and back drills,
    with findings. via_types is the via part of the drill table (component holes and
    slots are not in it); holes are as drawn (finished), which fabricators drill larger.
    Back drilling is recomputed the way Altium writes the drill files; stubs of nets
    with no back-drill rule are advice.
17. check_connectivity — "is the board fully routed?", "what is left to route?", "why
    does DRC say un-routed?", floating copper, net antennae, routing progress: per net,
    whether its saved copper (tracks, vias, pads, the pour copper as last poured,
    regions, fills, assigned internal planes) joins all its pads, with each missing
    connection's nearest pads and distance, a near-miss copper gap, isolated copper and
    antennae; weak (edge-only) joins when checks names weak or the Un-Routed Net rule
    checks incomplete connections (then nets_unrouted_in_altium, routed here only
    through weak joins, read un-routed in Altium). It reads the copper saved
    now; quote altium_drc's report issues before comparing with Altium's last DRC run. Nets
    connected through assumed copper (planes, split-plane or pour outlines) are in
    assumed_copper_nets; say those connections are assumed.
18. check_pcb_health — "run a health check", board clean-up, or before fab outputs:
    Altium's PCB Health Check Monitor offline (Altium's categories, ids and levels:
    shelved/modified polygons, duplicate designators, zero-area and self-intersecting
    shapes, micro-segments, 360deg components, reference points outside the part)
    plus parts outside the board; altium_report compares the last DRC report's health
    counts. Quote not_checked: mirrored footprints, panels and fonts need Altium.
19. check_dfm — "is this board manufacturable?", "DFM check", "can <fab> make this?",
    before ordering: copper, holes and outline against a fab profile (altimade built
    in, fab_profile.json in the project folder, or limits). Say which profile was used
    (scope); never present Altimade's limits as another fabricator's: ask for that
    fab's numbers and pass profile="none" with limits. Answer from each result's status
    and severity together (status advisory: its only misses are info, such as
    Altimade's ordering limits while it is only the default; that is not a fabrication
    miss, so report it as a note), quote not_checked, and say "meets <profile>'s limits
    on the checks run", never just "manufacturable". Assembly (DFA: "can it be
    assembled?", fiducials, tombstoning, part spacing, too many unique parts) is the
    same tool for the active variant: part_counts, assembly_sides, part_spacing,
    part_to_edge, fiducials, tombstoning, fine_pitch, pick_and_place (centroids, board
    origin). A limit the profile does not set
    (part spacing, part to edge, pitch) is measured, not judged: ask the assembler.
20. pcb_manufacturing_review — "is this board ready to order?", "review it for
    manufacturing", "go/no-go before fab": a guided fabrication then assembly review
    ending in a go / no-go that lists what was not checked; it asks which fabricator
    first. One check against a fab profile is check_dfm.
21. get_assembly_guide — "how do I assemble / hand-solder this prototype?", "assembly
    guide", "build sheet", "soldering order": the variant's parts in soldering order
    (the SMD of both sides first, the side with shorter parts first, then through-hole;
    short to tall on each side) in BOM-line steps with Center-X/Y, rotation and pin-1
    or polarity hints, as a Markdown checklist (Done / Skipped) or CSV. Quote
    first_side_reason and left_out (Not Fitted parts, fiducials, parts nobody places);
    without a netlist the steps are approximate (grouping_approximate): say so.

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
cutouts, removed islands, and clearances must be verified in Altium. check_connectivity
is the exception: it reads the poured copper as saved (a pour changed since its last
repour is checked as it was poured, and an unpoured one connects nothing).

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

For an Altium 365 project, set_project_dir's altium365 block says where the local copy
stands (read offline, nothing fetched). Pass its warnings on before answering: an old last
fetch, a copy behind Altium 365, local changes not committed. get_clone_status re-reads it.

## Error Recovery

- netlist_required → the project was loaded without a netlist (partial load). Tell the user
  this answer needs the project opened in Altium Designer once, then set_project_dir again.
  Do not retry, and do not answer schematic questions (parts, values, pins, nets) from PCB
  data without saying it is PCB data only
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


# Tools that need the compiled netlist; after a netlist-free load they answer
# netlist_required. Every other tool works from the project files alone.
NETLIST_TOOLS = (
    "query_net", "get_component", "search_components", "get_sheet_context", "get_bom",
    "check_bom", "check_decoupling", "check_sch_pcb_sync", "schematic_review",
    "package_for_xfn",
)


class ProjectNotReady(ValueError):
    """Raised by _require_project; payload is the structured tool error."""

    def __init__(self, payload: dict):
        super().__init__(payload["message"])
        self.payload = payload


def _netlist_required_error() -> dict:
    name = (_project or {}).get("name")
    return {
        "error": "netlist_required",
        "message": (
            f"Project '{name}' was loaded without a netlist: Altium Designer was not "
            "running and no up-to-date cached netlist was found. This tool needs the "
            "schematic connectivity of the netlist, which only Altium Designer can "
            "generate. Open the project in Altium Designer once, then call set_project_dir "
            "again; later loads reuse the cached netlist without Altium. Board, design-rule, "
            "length, differential-pair, via, DRC report, design-notes, picture, sheet-hierarchy "
            "and variant tools work now."
        ),
    }


def _require_project(netlist: bool = True):
    """The loaded (project, netlist, variant_state). A netlist-free load has netlist
    None: with netlist=False it is returned as is, otherwise netlist_required is raised."""
    if _project is None or _variant_state is None:
        raise ProjectNotReady({
            "error": "no_project",
            "message": "No project loaded. Call set_project_dir first.",
        })
    if netlist and _altium._netlist is None:
        raise ProjectNotReady(_netlist_required_error())
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


def _altium365_info(project_dir: str) -> dict:
    """The altium365 block of a load response: where the project's Altium 365 clone
    stands, read offline. Empty when the folder is not in one; never fails a load."""
    try:
        status = clone_status(project_dir)
    except Exception as error:
        logging.warning("Altium 365 clone status failed: %s", error)
        return {}
    if status.get("altium365") is False:
        return {}
    return {"altium365": status}


def _resolve_dnp_uids(variant_state: VariantState, netlist: dict | None) -> None:
    """Bind each variant's Not Fitted entries to the loaded parts the way get_bom does
    (UniqueId path, then its last segment, then the stored designator), so is_dnp and
    every DNP flag agree with the BOM. Without loaded parts, the stored designators and
    the UniqueIds of '?' designators stay as read."""
    netlist = netlist or {}
    uid_map = {
        comp.get("unique_id"): refdes
        for refdes, comp in netlist.get("components", {}).items()
        if comp.get("unique_id")
    }
    variant_state.resolve_dnp_uid(uid_map)
    names = variant_names(netlist)
    if not names:
        return
    for variant in variant_state.variants:
        if variant.variations:
            plan = plan_variant(variant, netlist, names)
            variant.dnp_refdes = sorted(plan.not_fitted, key=natural_key)


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


# ---------- get_clone_status ----------

@mcp.tool(title="Altium 365 Clone Status", annotations=ToolAnnotations(readOnlyHint=True))
def get_clone_status(project_dir: str | None = None) -> str:
    """Where a project's local copy stands against Altium 365, read offline from its Git
    clone: workspace and project GUID, when it was last updated from Altium 365 (last
    fetch), commits ahead of or behind Altium 365 as of that fetch, files changed locally
    and not committed, and the last commit. Nothing is fetched, so changes made in Altium
    365 since then are not seen; warnings flag an old fetch, a copy behind the workspace
    and local changes. set_project_dir returns the same block as altium365: call this
    after the user updates or saves the project in Altium. project_dir defaults to the
    loaded project's folder."""
    if project_dir is None:
        if _project is None:
            return json.dumps({
                "error": "no_project",
                "message": "No project loaded. Call set_project_dir first, or pass project_dir.",
            })
        project_dir = _project["root_dir"]
    if not Path(project_dir).is_dir():
        return json.dumps({"error": "folder_not_found",
                           "message": f"Folder not found: {project_dir}"})
    try:
        status = clone_status(project_dir)
    except Exception as error:
        logging.warning("Altium 365 clone status failed: %s", error)
        return json.dumps({"error": "clone_status_failed",
                           "message": f"{type(error).__name__}: {error}"})
    if status.get("altium365") is False:
        return json.dumps({
            "error": "not_an_altium365_clone",
            "reason": status["reason"],
            "message": (
                "The folder is not in a Git clone whose origin is an Altium 365 project "
                "(https://<workspace>.365.altium.com/git/<project GUID>.git), so there is "
                "no Altium 365 status to read."
            ),
        })
    return json.dumps(status, indent=2)


# ---------- set_project_dir ----------

def _project_context(project_dir: str) -> str | None:
    """The project folder's CLAUDE.md, if any."""
    claude_md = list(Path(project_dir).glob("CLAUDE.md")) + list(Path(project_dir).glob("claude.md"))
    return claude_md[0].read_text(encoding="utf-8") if claude_md else None


def _load_cached_netlist(project_dir: str, prj_pcb_path: str, prj_data, net_path: Path,
                         netlist_mtime: float | None = None) -> str:
    """Full load from an up-to-date .NET on disk, without Altium."""
    global _project, _variant_state, _netlist_last_updated
    project_name = Path(prj_pcb_path).stem
    sheets = [{"name": Path(p).stem, "path": p} for p in prj_data.sheet_paths]
    _project = None
    _variant_state = None
    _altium._netlist = None
    _netlist_last_updated = None
    _pcb_session.__init__()
    _altium.load_netlist_from_file(str(net_path), prj_data.sheet_paths, prj_data.annotation_paths)
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
    if netlist_mtime is None:
        upsert_registry_entry(Path(prj_pcb_path).name, project_dir)
    else:
        upsert_registry_entry(Path(prj_pcb_path).name, project_dir, netlist_mtime=netlist_mtime)
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
        **_altium365_info(project_dir),
    }
    context = _project_context(project_dir)
    if context is not None:
        response["project_context"] = context
    return json.dumps(response, indent=2)


def _load_unregistered_netlist(project_dir: str, prj_pcb_path: str) -> str | None:
    """Full load from a .NET that is up to date but not the one the registry recorded
    (Altium regenerated it on its own, the folder moved, a new registry), or None.
    With several candidates (e.g. an archived copy of the project in a subfolder) only
    the one in the project's output folder is trusted."""
    name = Path(prj_pcb_path).stem
    matches = list(Path(project_dir).rglob(f"{name}.NET")) or list(Path(project_dir).rglob(f"{name}.net"))
    if not matches:
        return None
    try:
        prj_data = parse_prj_pcb(prj_pcb_path)
        if len(matches) == 1:
            net_path = matches[0]
        else:
            output = PureWindowsPath(prj_data.output_path or f"Project Outputs for {name}")
            output_dir = (Path(*output.parts) if output.is_absolute()
                          else Path(prj_pcb_path).parent / Path(*output.parts)).resolve()
            net_path = next((m for m in matches if m.parent.resolve() == output_dir), None)
            if net_path is None:
                return None
        sources = netlist_sources(prj_pcb_path, prj_data.sheet_paths, prj_data.annotation_paths)
        if not prj_data.sheet_paths or not is_netlist_fresh(net_path, sources):
            return None
        return _load_cached_netlist(project_dir, prj_pcb_path, prj_data, net_path,
                                    netlist_mtime=net_path.stat().st_mtime)
    except Exception as e:
        logging.warning("Unregistered netlist not loaded: %s", e)
        return None


def _load_netlist_free(project_dir: str, prj_pcb_path: str, netlist_stale: bool) -> dict:
    """Load the project from its files alone: Altium Designer is not running and no
    up-to-date cached netlist exists. Board, rule, notes, picture and variant tools work;
    the NETLIST_TOOLS answer netlist_required."""
    global _project, _variant_state, _netlist_last_updated
    _project = None
    _variant_state = None
    _altium._netlist = None
    _netlist_last_updated = None
    _pcb_session.__init__()

    prj_data = parse_prj_pcb(prj_pcb_path)
    if not prj_data.sheet_paths and not prj_data.pcb_doc_paths:
        return {
            "error": "no_documents",
            "message": (
                "The .PrjPcb lists no .SchDoc sheet and no .PcbDoc, so there is nothing to "
                "load without Altium. Check the .PrjPcb file."
            ),
        }
    sheets = [{"name": Path(p).stem, "path": p} for p in prj_data.sheet_paths]
    _project = {
        "name": Path(prj_pcb_path).stem,
        "root_dir": project_dir,
        "prj_pcb_path": prj_pcb_path,
        "sheets": sheets,
        **_project_documents(prj_data),
    }
    _variant_state = VariantState(prj_data.variants)
    _resolve_dnp_uids(_variant_state, None)
    upsert_registry_entry(Path(prj_pcb_path).name, project_dir)

    message = (
        "Partial load: Altium Designer is not running and no up-to-date cached netlist was "
        "found, so the project was loaded from its files without a netlist. The tools in "
        "netlist_required_tools need it and answer netlist_required; board, design-rule, "
        "length, differential-pair, via, DRC report, design-notes, picture, sheet-hierarchy "
        "and variant tools work. PCB parts are not linked to schematic parts (sch_refdes is empty) and "
        "DNP comes from the designators stored in the variants. To load fully, open the "
        "project in Altium Designer once and call set_project_dir again; later loads "
        "reuse the cached netlist without Altium."
    )
    if not prj_data.pcb_doc_paths:
        message += " The project lists no .PcbDoc, so the board tools answer no_pcb_document."
    if netlist_stale:
        message += (
            " A cached netlist exists, but the project or its schematic sheets were saved "
            "after it was generated, so it is not used."
        )
    response = {
        "loaded": True,
        "partial": True,
        "load_mode": "netlist_free",
        "project": _project["name"],
        "sheets": [s["name"] for s in sheets],
        "sheet_count": len(sheets),
        "variants": [v.name for v in prj_data.variants],
        "variant_count": len(prj_data.variants),
        "pcb_doc": Path(prj_data.pcb_doc_paths[0]).name if prj_data.pcb_doc_paths else None,
        "netlist_updated_utc": None,
        "netlist_required_tools": list(NETLIST_TOOLS),
        "message": message,
        "session_start": (
            "Tell the user the load is partial and why. Skip get_sheet_context: summarize "
            "the board from get_design_notes and get_board_info instead."
        ),
        **_design_info(prj_data),
        **_altium365_info(project_dir),
    }
    if netlist_stale:
        response["netlist_stale"] = True
    context = _project_context(project_dir)
    if context is not None:
        response["project_context"] = context
    return response


@mcp.tool(title="Load Project", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def set_project_dir(project_dir: str) -> str:
    """Load an Altium project from disk. Must be called before any other tool.
    If the netlist cache is valid, loads from disk without requiring Altium to be open.
    Otherwise generates the netlist in Altium Designer, which must have the project open.
    When Altium is not running, loads the project without a netlist (partial: true):
    board, rule, notes, picture and variant tools work, the tools listed in
    netlist_required_tools answer netlist_required."""
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
                                return _load_cached_netlist(
                                    project_dir, prj_pcb_path, prj_data, _net_matches[0]
                                )
                    except Exception as e:
                        logging.warning("Registry cache shortcut failed: %s", e)
            break

    # Full Altium flow — required when cache is absent or stale. Without Altium the
    # project still loads from its files, without a netlist.
    status = _altium.get_status()
    if not status.get("running"):
        loaded = None if _stale_cache else _load_unregistered_netlist(project_dir, prj_pcb_path)
        if loaded is not None:
            return loaded
        return json.dumps(_load_netlist_free(project_dir, prj_pcb_path, _stale_cache), indent=2)
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
        **_altium365_info(project_dir),
    }

    context = _project_context(project_dir)
    if context is not None:
        response["project_context"] = context

    return json.dumps(response, indent=2)


# ---------- refresh_netlist ----------

@mcp.tool(title="Refresh Netlist", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def refresh_netlist() -> str:
    """Refresh the netlist after the user has saved schematic changes in Altium.
    Call this when the user says they changed, added, or saved anything in the schematic.
    Do not call speculatively — only when the user confirms they have saved.
    After a netlist-free load (partial: true) it loads the project again, fully
    when Altium Designer now has it open."""
    global _netlist_last_updated, _variant_state
    try:
        project, netlist, variant_state = _require_project(netlist=False)
    except ProjectNotReady as e:
        return json.dumps(e.payload)
    if netlist is None:
        return _reload_partial(project, variant_state)
    # Re-read the sheet list so sheets added, moved or removed since load are picked up
    # and the sheet names stay in step with the components' sheet attribution.
    prj_data = None
    variant_note = None
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
        # Variants saved since load (Not Fitted parts, parameter variations, new
        # variants) come with the project file; the active one stays by name.
        active = variant_state.active.name
        variant_state = VariantState(prj_data.variants)
        if variant_state.get(active):
            variant_state.set_variant(active)
        else:
            variant_note = (
                f"The active variant '{active}' is no longer in the project (renamed or "
                f"deleted); '{variant_state.active.name}' is active now. Ask the user which "
                "variant to work in, then call set_active_variant."
            )
        _variant_state = variant_state
    _resolve_dnp_uids(variant_state, _altium._netlist)
    diagnostics = _load_diagnostics(_altium._netlist or {})
    if not regenerated:
        message = "Netlist is already up to date. If you expected changes, save in Altium and try refresh again."
        if variant_note:
            message += f"\n{variant_note}"
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
    result = {
        "refreshed": True,
        "netlist_updated_utc": _netlist_last_updated,
        **diagnostics,
    }
    if variant_note:
        result["variant_note"] = variant_note
    return json.dumps(result)


def _reload_partial(project: dict, variant_state: VariantState) -> str:
    """refresh_netlist after a netlist-free load: load the project again, fully once
    Altium has it open. The active variant stays; if the full load fails after it
    cleared the state, the partial load is restored."""
    active = variant_state.active.name
    result = json.loads(set_project_dir(project["root_dir"]))
    if "error" in result and _project is None:
        if _load_netlist_free(project["root_dir"], project["prj_pcb_path"], False).get("loaded"):
            result["partial_load_kept"] = True
            result["message"] = (
                result.get("message", "") + " The partial load is kept: board, rule, notes "
                "and variant tools still work."
            ).strip()
    if _variant_state is not None:
        if _variant_state.get(active):
            _variant_state.set_variant(active)
        else:
            result["variant_note"] = (
                f"The active variant '{active}' is no longer in the project (renamed or "
                f"deleted); '{_variant_state.active.name}' is active now. Ask the user which "
                "variant to work in, then call set_active_variant."
            )
        if "error" not in result:
            result["active_variant"] = _variant_state.active.name
    return json.dumps(result, indent=2)


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
    except ProjectNotReady as e:
        return json.dumps(e.payload)
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
    except ProjectNotReady as e:
        return json.dumps(e.payload)
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
    except ProjectNotReady as e:
        return json.dumps(e.payload)
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
    except ProjectNotReady as e:
        return json.dumps(e.payload)

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
        project, _, _ = _require_project(netlist=False)
    except ProjectNotReady as e:
        return json.dumps(e.payload)
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
        project, _, _ = _require_project(netlist=False)
    except ProjectNotReady as e:
        return json.dumps(e.payload)
    result = _get_design_image_impl(project, _design_notes, document, index)
    return json.dumps(result) if isinstance(result, dict) else result


# ---------- get_design_hierarchy ----------

@mcp.tool(
    title="Get Design Hierarchy",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_design_hierarchy(sheet: str | None = None, offset: int = 0) -> str:
    """The project's sheet hierarchy offline, like Altium's Report Project Hierarchy: the
    sheet tree from the top sheet(s) (sheets no sheet symbol places) down through the
    sheet symbols, with device sheets, Repeat(...) channels and their counts marked; per
    sheet its ports (name, I/O type, harness) and its sheet symbols with their entries
    (name, I/O type, harness; port_io when the child's port differs or is missing); and
    the project's Net
    Identifier Scope (the .PrjPcb setting, what Automatic resolves to for this project,
    how nets cross sheets under it, and the net naming options). Use it to explain why a
    net has its name and how it crosses sheets. With a compiled netlist each port and
    entry names the net (or one net per channel) it joins; labels lists the net labels
    and power ports on its wiring. Issues: symbols pointing at missing files, entries
    without a port in the child and ports without an entry, I/O and harness mismatches,
    ports on a top sheet under a hierarchical scope, duplicate symbols or entries,
    sheets placed several times, cycles, unwired ports and entries (issue_help explains
    each kind; findings of one kind on one symbol or sheet are listed together in
    entries or ports, and summary counts each one).

    sheet (a sheet name or file name) narrows the tree, sheets and issues to its
    subtree. Sheet details are paginated (offset from next_offset); the tree and issues
    come with the first page."""
    try:
        project, netlist, _ = _require_project(netlist=False)
    except ProjectNotReady as e:
        return json.dumps(e.payload)
    model = build_design_hierarchy(project, _design_notes.hierarchy, netlist, sheet)
    if "error" in model:
        return json.dumps(model)
    if netlist is not None:
        model["netlist_updated_utc"] = _netlist_last_updated
    # Sheet details shrink first; the issues (errors first) only when that is not enough.
    for limit, issue_limit in ((40, 150), (15, 150), (5, 150), (5, 60), (1, 60), (1, 20)):
        rendered = json.dumps(page_design_hierarchy(model, offset, limit, issue_limit),
                              indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# ---------- get_project_options ----------

@mcp.tool(
    title="Get Project Options",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_project_options(offset: int = 0) -> str:
    """The project settings that change how Altium reads the design, offline from the
    .PrjPcb (Project Options): the Net Identifier Scope (and what Automatic resolves to)
    with the net naming options, diff-pair suffixes, class generation (project-wide and
    per sheet), multi-channel room naming and designator format, the ERC report level of
    each violation type (Error Reporting), the Connection Matrix, the Comparator's
    matching criteria and the output paths. The audit lists ERC checks switched off or
    downgraded from Altium's default (hides says what goes unreported), Connection
    Matrix cells relaxed for risky pin pairs (driver conflicts such as output to output,
    unconnected input pins), and every No ERC directive on the sheets with its sheet,
    location (mil), active flag, generic or specific mode, the net it sits on and its
    kind (power, high_fanout, signal, single_pin, wire_only, nothing), flagged when an
    active one sits on a power net or a net with several pins, where the violations it
    suppresses at its point may hide real problems. With a
    compiled netlist each No ERC names its net and the net's pin count; without one,
    the names on its wiring and that sheet's pins. Keys that no Altium source verifies
    are passed on raw under unmapped.

    No ERC directives are paginated (offset from next_offset), flagged first; the
    settings and audit come with the first page."""
    try:
        project, netlist, _ = _require_project(netlist=False)
    except ProjectNotReady as e:
        return json.dumps(e.payload)
    model = build_project_options(project, _design_notes.hierarchy, netlist)
    if "error" in model:
        return json.dumps(model)
    if netlist is not None:
        model["netlist_updated_utc"] = _netlist_last_updated
    for limit in (200, 80, 30, 10, 1):
        rendered = json.dumps(page_project_options(model, offset, limit), indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# ---------- check_project_integrity ----------

# Load diagnostics that point at the same problems: sheets that did not parse, unreadable
# .Annotation files, designator clashes and netlist parts on no listed sheet.
_INTEGRITY_DIAGNOSTICS = (
    "sheet_errors", "annotation_errors", "designator_clash_count", "designator_clashes",
    "designator_clashes_hint", "refdes_without_metadata", "refdes_without_metadata_sample",
    "refdes_without_metadata_hint", "warning", "message",
)


@mcp.tool(
    title="Check Project Integrity",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_project_integrity(checks: list[str] | None = None, offset: int = 0) -> str:
    """Problems in the project folder, the .PrjPcb and the strings on the sheets that
    silently break compile, sync or saving, offline (no netlist needed). File checks:
    legacy_extension (schematics saved as .Sch/.S01/.S02: Update PCB can then remove every
    part; a .Pcb is renamed with them), missing_document (listed in the project, not on disk),
    annotation_file (a leftover .Annotation file from Board Level Annotation that makes
    the compiled designators differ from the editor's, entries for parts that are gone),
    outside_sheet_border (parts placed beyond the sheet size: compiled, not printed),
    compile_mask (parts a compile mask leaves out of the PCB and BOM) and variant_entry
    (variant entries whose designator or UniqueId no longer exists, e.g. after a PCB
    reannotation). Name checks on net labels, power ports, ports, sheet entries,
    designators and component parameters: hidden_characters (control, format and
    non-ASCII space characters: these make Constraint Manager saves fail), non_ascii,
    whitespace (leading or trailing spaces), net_name_syntax (brackets that are no
    Name[first..last] bus range), reserved_bom_parameter (local 'Manufacturer N' /
    'Manufacturer Part Number N' parameters, which the exported BOM overwrites from Part
    Choices, and the parts whose copilot MPN comes from one) and
    channel_designator_format (a multi-channel Designator Format that adds channel text
    after bus names on channel sheets).

    Each finding has a severity (error: breaks compile, sync or save; warning: a silently
    wrong result; info), its count, examples (sheet, designator, net, port or parameter,
    location in mil), how to fix it in Altium and the Altium knowledge-base article
    behind it (source). checks narrows to some of these. Findings are paginated (offset
    from next_offset), errors first; with a netlist, variant entries bind to the
    compiled designators and the load diagnostics come with the first page."""
    try:
        project, netlist, _ = _require_project(netlist=False)
    except ProjectNotReady as e:
        return json.dumps(e.payload)
    model = build_project_integrity(project, _design_notes.hierarchy, _design_notes.integrity,
                                    netlist, checks)
    if "error" in model:
        return json.dumps(model)
    if netlist is not None:
        model["netlist_updated_utc"] = _netlist_last_updated
        diagnostics = _load_diagnostics(netlist)
        kept = {key: diagnostics[key] for key in _INTEGRITY_DIAGNOSTICS if key in diagnostics}
        if kept:
            model["load_diagnostics"] = kept
    for limit, examples in ((60, 10), (30, 5), (15, 3), (5, 2), (1, 1)):
        rendered = json.dumps(page_project_integrity(model, offset, limit, examples), indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


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
        _, _, variant_state = _require_project(netlist=False)
    except ProjectNotReady as e:
        return json.dumps(e.payload)
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
        _, _, variant_state = _require_project(netlist=False)
    except ProjectNotReady as e:
        return json.dumps(e.payload)
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


NETLIST_FREE_PCB_NOTE = (
    "Not loaded (netlist-free load): PCB designators are not linked to schematic parts, "
    "so sch_refdes is empty, and DNP flags come from the designators stored in the "
    "project's variants."
)

PCB_POLYGON_GEOMETRY_NOTE = (
    "Repour polygons in Altium and save the PcbDoc before relying on polygon "
    "results. These tools model saved nominal polygon outlines and the tracks "
    "and arcs of hatched pours, not solid pours' region/fill copper; shelved "
    "pours have no copper. Verify voids, cutouts, removed islands, and "
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

    counts: tracks and arcs are routing; pour_copper is the tracks and arcs of
    hatched pours; shelved_pours are pours without copper.

    units controls returned coordinates and lengths; it defaults to the
    board's display unit.
    """
    try:
        project, netlist, _ = _require_project(netlist=False)
    except ProjectNotReady as error:
        return json.dumps(error.payload)
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
        "warnings": index.pcb.warnings,
        "skipped_records": index.pcb.skipped_records,
    }
    if netlist is None:
        result["netlist"] = NETLIST_FREE_PCB_NOTE
    else:
        result["refdes_mapping_unmatched"] = index.unmatched_components()
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
        project, netlist, _ = _require_project(netlist=False)
    except ProjectNotReady as error:
        return None, error.payload
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
    """Return physical PCB routing details for exact or regex-matched nets.

    layers are routing: a hatched pour's tracks and arcs are not counted.
    pours lists the net's polygon pours with their names (several may share
    one), shelved ones (no copper) with shelved true.
    """
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

    neighbors are copper-to-copper edge distances (tracks, arcs, vias, pads,
    and a hatched pour's tracks and arcs: pour_copper_segments counts those;
    they add no close_segments or parallel run).
    pours lists other nets' polygon pours on the net's layers, by their drawn
    outline only: inside_pour when the net's copper lies within the outline,
    min_outline_distance_mil when the outline comes within distance. An outline
    is not copper, and poured copper keeps the pour's clearance from the net.
    Shelved pours have no copper and are left out.

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

    nets holds copper only (tracks, arcs, vias, pads; a hatched pour's tracks
    and arcs are kind "pour"). pours are polygon pours by their drawn outline:
    inside_pour, or the distance to the outline, which is not copper. Shelved
    pours have no copper and are left out.

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
    annular ring (smallest over a full-stack via's pads, naming the layer), routing
    layers and Length windows, resolving the rule for EACH object
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
    _, netlist, variant_state = _require_project(netlist=False)
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
    try:
        project, netlist, variant_state = _require_project()
    except ProjectNotReady as e:
        return json.dumps(e.payload)
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
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


# ---------- diff_design ----------

DiffSection = Literal[
    "ict_hazards", "project", "components", "variants", "bom", "nets", "placement",
    "rules", "classes", "diff_pairs", "stackup", "routing",
]


@mcp.tool(
    title="Diff Design",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def diff_design(
    rev_a: str | None = None,
    rev_b: str | None = None,
    project_dir_a: str | None = None,
    project_dir_b: str | None = None,
    section: DiffSection | None = None,
    units: PcbUnits | None = None,
    offset: int = 0,
) -> str:
    """What changed between two states of the project, as an ECO-style changelog for a
    review or release notes. Sources: two revisions of the Git clone holding the loaded
    project (rev_a older, rev_b newer: a commit hash, branch, tag, HEAD~n, or 'working'
    for the files on disk; rev_b defaults to working, rev_a to the last commit that
    changed a design file, or to rev_b's parent when rev_b is a commit), or two project
    folders (project_dir_a, project_dir_b; b defaults to the loaded project's folder),
    e.g. an archived release. With no revisions the recent commits that changed design
    files are listed (recent_commits) so the user can pick. Revisions are read offline
    with read-only git commands into a temporary folder that is deleted; the clone is
    never written.

    Sections: project (sheets, project parameters), components (added, removed,
    re-designated, revalued: Comment/Value, footprint, parameters), variants (Not Fitted
    and alternate parts per variant, varied parameters), bom (line changes per variant;
    variants whose changes equal the base BOM's are named in
    bom_variants_same_as_base), nets (from the PcbDoc: added, removed, renamed, merged,
    split, pins moved between nets), placement (parts added, removed, moved, rotated,
    flipped, footprint or designator changed on the board), rules, classes, diff_pairs,
    stackup (layers, thickness, impedance profiles) and routing (per-net routed length
    and via count). ict_hazards come first: nets that kept their name while their pins
    changed, and designators now on a different part. Components match by UniqueId,
    nets by shared pins then name, as Altium's comparator does. Purely graphical edits
    are not reported. Nets come from the PcbDoc: schematic wiring not pushed to the
    board is not seen (check_sch_pcb_sync). Lengths in units (default: the board's).
    Paginated: section= narrows to one section, offset from next_offset; summary and
    notes come with the first page."""
    try:
        if project_dir_a or project_dir_b:
            loaded = _project or {}
            folder_b = project_dir_b or loaded.get("root_dir")
            if not project_dir_a or not folder_b:
                return json.dumps({
                    "error": "folders_required",
                    "message": "Pass project_dir_a (and project_dir_b, or load the project "
                               "to compare with) to compare two folders.",
                })
            prj_name = Path(loaded["prj_pcb_path"]).name if loaded.get("prj_pcb_path") else None
            model = diff_folders(project_dir_a, folder_b, prj_name, prj_name, units)
        else:
            if _project is None:
                return json.dumps({
                    "error": "no_project",
                    "message": "No project loaded. Call set_project_dir first, or pass "
                               "project_dir_a and project_dir_b.",
                })
            model = diff_revisions(_project["prj_pcb_path"], rev_a, rev_b, units)
    except DiffError as error:
        return json.dumps(error.payload)
    except Exception as error:
        logging.warning("Design diff failed: %s", error)
        return json.dumps({"error": "diff_failed",
                           "message": f"{type(error).__name__}: {error}"})
    to_mm = model.get("units") == "mm"
    for limit in (200, 80, 30, 10, 1):
        page = _convert_units(page_design_diff(model, offset, limit, section), to_mm)
        rendered = json.dumps(page, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


@mcp.tool(
    title="Check Decoupling",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_decoupling(
    ic: str | None = None,
    rail: str | None = None,
    checks: list[str] | None = None,
    max_distance: float | None = None,
    units: PcbUnits | None = None,
    offset: int = 0,
) -> str:
    """Check decoupling offline: which capacitors decouple each IC's power pins, and the
    gaps. Call it when the user asks about decoupling or bypass capacitors, whether an
    IC's power pins are decoupled, or for a design or layout review. Per rail: no
    capacitor to ground, only capacitors Not Fitted in the active variant, a rail joined
    through a ferrite or resistor to capacitors with none of its own (warnings); only
    capacitors to other nets (bootstrap, another rail, a 0 ohm link), no small (<1 uF)
    capacitor, unreadable values (info); no_bulk_capacitor only when named in checks
    (bulk is often upstream; each rail's counts are in rails). With a PcbDoc, pairs gives
    each IC rail's nearest capacitor (and nearest under 1 uF) with its distance, and a
    warning when the nearest fitted capacitor is farther than max_distance (straight
    line, pad edge to pad edge); power pads far from every capacitor, and an
    opposite-side capacitor with no via or pour found at its pad, are info. Naming ICs in
    ic lists their 5 nearest capacitors ('is C12 close enough to U3?').

    It is advice, not DRC: rails are inferred from net and pin names, ICs are U and IC
    parts fitted in the variant (ic also takes others, e.g. a connector), the datasheet
    decides how many capacitors a part needs, and regions and planes are not read. ic and
    rail take comma-separated names; checks narrows to the names in summary. Findings
    are paginated (offset from next_offset), warnings first. max_distance and every
    distance are in units, default mm (max_distance default 5 mm)."""
    try:
        project, netlist, variant_state = _require_project()
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    index, pcb_unavailable = None, None
    if units not in (None, "mm", "mil"):
        return json.dumps({"error": "invalid_units", "message": "units must be 'mil' or 'mm'."})
    resolved = units or "mm"  # decoupling guidance is in mm, whatever the board shows
    to_mm = resolved == "mm"
    if project.get("pcb_doc_paths"):
        index, error = _pcb_session.get(project, netlist)
        if error:
            pcb_unavailable = f"{error['error']}: {error.get('message', '')}".strip(": ")
    else:
        pcb_unavailable = "the project lists no PcbDoc"
    max_mil = DEFAULT_MAX_DISTANCE_MIL
    if max_distance is not None:
        if not _finite_nonnegative(max_distance) or max_distance == 0:
            return json.dumps({"error": "invalid_max_distance",
                               "message": "max_distance must be a positive number."})
        max_mil = _input_to_mil(max_distance, resolved)
    plan, _, _ = _bom_plan(netlist, variant_state, None)
    for limit, rail_limit in ((100, 60), (40, 20), (15, 5)):
        result = run_decoupling_check(
            netlist, plan, project.get("parameters") or {}, index=index,
            pcb_unavailable=pcb_unavailable, checks=checks, ics=_split_names(ic),
            rails=_split_names(rail), max_distance_mil=max_mil, offset=offset,
            limit=limit, rail_limit=rail_limit,
        )
        if "error" in result:
            return json.dumps(result)
        context: dict = {"netlist_updated_utc": _netlist_last_updated}
        if index is not None:
            context.update({"pcb_doc": Path(project["pcb_doc_paths"][0]).name,
                            "units": resolved})
            if len(project["pcb_doc_paths"]) > 1:
                context["note"] = (
                    f"Project lists {len(project['pcb_doc_paths'])} PcbDocs; measured on "
                    f"'{Path(project['pcb_doc_paths'][0]).name}'."
                )
        result = _convert_units({**context, **result}, to_mm)
        if limit < 100:
            result["findings_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


@mcp.tool(
    title="Check Vias",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_vias(
    net: str | None = None,
    checks: list[str] | None = None,
    max_aspect_ratio: float | None = None,
    max_microvia_aspect_ratio: float | None = None,
    min_annular_ring: float | None = None,
    max_stub: float | None = None,
    units: PcbUnits | None = None,
    offset: int = 0,
) -> str:
    """Via inventory and drill review of the PcbDoc: "what vias does this board use?",
    blind/buried/microvias, aspect ratio, annular ring, via-in-pad, tenting, back
    drilling, via stubs. A fabricator's capability limits (is the board manufacturable,
    can my fab make it): check_dfm.

    via_types: each type (span by stack layers, hole as drawn, pad) with count, nets,
    copper-to-copper depth, aspect ratio, smallest annular ring, tenting, in-pad and
    back-drilled counts; net_classes counts types per class; drill_pairs and back_drill
    give the stack's drill pairs, back drills and back-drill rules. Findings, warnings
    first: aspect_ratio (above max_aspect_ratio, default 10:1, info above 8:1 and for a
    blind via above 1:1; microvias above max_microvia_aspect_ratio, default 1:1, info
    above 0.8:1; stacked microvias), annular_ring (below the Minimum Annular Ring rule
    or min_annular_ring, whichever is larger; info under 0.1 mm, microvias 0.05 mm),
    via_style (outside the Routing Via Style sizes), drill_pairs (blind/buried span
    without a drill pair), via_in_pad (fill and cap advice; warning for another net's
    pad or a ViasUnderSMD rule forbidding it), tenting, back_drill (stub above the rule's
    max even when back drilled; info for differential pairs, or the nets in net, with
    stubs above max_stub, default 40 mil). Back drilling is recomputed like Altium: a
    via in the rule's scope is drilled when it has no connection on the layers removed.

    net narrows to comma-separated nets, pairs or net classes; checks narrows the
    findings, paginated (offset from next_offset). min_annular_ring, max_stub and
    lengths use units (default: board display unit). Plugging and filling are not read."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, resolved, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    limits = {}
    for name, value in (
        ("max_aspect_ratio", max_aspect_ratio),
        ("max_microvia_aspect_ratio", max_microvia_aspect_ratio),
        ("min_annular_ring", min_annular_ring),
        ("max_stub", max_stub),
    ):
        if value is None:
            continue
        if not _finite_nonnegative(value) or (value == 0 and name != "min_annular_ring"):
            kind = "non-negative" if name == "min_annular_ring" else "positive"
            return json.dumps({
                "error": f"invalid_{name}",
                "message": f"{name} must be a {kind} number.",
            })
        limits[name] = value
    full = review_vias(
        index,
        checks=checks,
        nets=_split_names(net),
        max_aspect_ratio=limits.get("max_aspect_ratio"),
        max_microvia_aspect_ratio=limits.get("max_microvia_aspect_ratio"),
        min_annular_ring_mil=(
            _input_to_mil(min_annular_ring, resolved) if "min_annular_ring" in limits else None
        ),
        max_stub_mil=_input_to_mil(max_stub, resolved) if "max_stub" in limits else None,
        units=resolved,
    )
    if "error" in full:
        return json.dumps(full)
    project, _, _ = _require_project(netlist=False)
    paths = project.get("pcb_doc_paths") or []
    context: dict = {"units": resolved}
    if paths:
        context["pcb_doc"] = Path(paths[0]).name
        if len(paths) > 1:
            context["note"] = (
                f"Project lists {len(paths)} PcbDocs; reviewed '{Path(paths[0]).name}'."
            )
    for limit, type_limit, class_limit in ((100, 60, 30), (40, 20, 10), (15, 10, 5), (5, 5, 3)):
        result = _convert_units(
            {**context, **page_via_result(full, offset, limit, type_limit, class_limit)},
            to_mm,
        )
        if limit < 100:
            result["findings_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


@mcp.tool(
    title="Check Connectivity",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_connectivity(
    net: str | None = None,
    net_class: str | None = None,
    layer: str | None = None,
    checks: list[str] | None = None,
    compare_drc: bool = True,
    offset: int = 0,
    units: PcbUnits | None = None,
) -> str:
    """Copper connectivity of the saved PcbDoc, like Altium's Un-Routed Net and Net
    Antennae rules: "is the board fully routed?", "what is left to route?", "why does
    DRC say un-routed?", dead copper, antennae, routing progress.

    Per net, whether its copper joins all its pads: tracks, arcs, vias, pads and touching
    copper, the pour copper saved in the file (solid pours' regions, hatched pours'
    tracks), copper regions, fills, and internal planes assigned to the net. summary
    counts nets routed / partly routed / unrouted with percentages and connections
    complete (Altium's routing completion). Findings, warnings first: unrouted (each
    missing connection between the nearest pads of two copper islands, distance_mil,
    and copper_gap_mil when the islands' copper comes within 25 mil: a near miss; the
    closest near misses in near_misses), isolated_copper (copper of a net reaching none of its
    pads: floating tracks and vias, dead pour islands as info), antenna (open-ended
    tracks and tracks ending on a via that connects nothing else, with stub_mil and
    open_track_mil; over_tolerance against an enabled Net Antennae rule, null without
    one), weak_connection (copper that only touches where a net needs it, or a pad or via
    connected directly to a pour with its centre off the pour's outline, which Altium's
    'Check for incomplete connections' rejects). That option of the board's Un-Routed
    Net rule is read (rules.UnRoutedNet, summary.incomplete_connections_checked): where
    it is on, weak connections are warnings and the nets routed here only through them
    read un-routed in Altium (nets_unrouted_in_altium). With
    compare_drc, altium_drc puts Altium's last Batch DRC report beside it
    (get_drc_report reads that report).

    net (nets or differential pairs) and net_class take comma-separated names; layer
    narrows the findings (not the summary) to one copper layer; checks narrows to
    unrouted, isolated, antennae, weak (weak is listed by default only when the option
    is or may be on; the summary always counts it). Findings are paginated (offset from
    next_offset). Lengths use units (default: board display unit)."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, resolved, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    nets = _split_names(net)
    classes = _split_names(net_class)
    full = review_connectivity(index, nets=nets, net_classes=classes, layer=layer, checks=checks)
    if "error" in full:
        return json.dumps(full)
    project, _, _ = _require_project(netlist=False)
    paths = project.get("pcb_doc_paths") or []
    context: dict = {"units": resolved}
    if paths:
        pcb_path = Path(paths[0])
        context["pcb_doc"] = pcb_path.name
        context["pcb_saved_utc"] = _utc(pcb_path.stat().st_mtime) if pcb_path.is_file() else None
        if len(paths) > 1:
            context["note"] = f"Project lists {len(paths)} PcbDocs; checked '{pcb_path.name}'."
        if compare_drc:
            context["altium_drc"] = _connectivity_drc(
                project, pcb_path, index, full, bool(nets or classes)
            )
    for limit, net_limit, connection_limit in (
        (100, 200, 20), (40, 100, 10), (15, 50, 5), (5, 20, 3), (2, 10, 1),
    ):
        result = _convert_units(
            {**context, **page_connectivity_result(
                full, offset, limit, net_limit, connection_limit
            )},
            to_mm,
        )
        if limit < 100:
            result["findings_limited_to"] = limit
            result["connections_per_finding_limited_to"] = connection_limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


def _connectivity_drc(project: dict, pcb_path: Path, index, full: dict, filtered: bool) -> dict:
    """Altium's last Batch DRC report beside check_connectivity: its Un-Routed Net and
    Net Antennae counts and the issues that limit trust in it."""
    report, report_path, _, _, notes = load_report(project, pcb_path)
    if report is None:
        return {
            "report": None,
            "note": "No Batch DRC report was found for this board (get_drc_report says "
            "where it looks).",
        }
    issues = notes + assess(report, report_path, pcb_path, project, index.pcb)
    block: dict = {
        "report": report_path.name,
        "report_saved_utc": _utc(report_path.stat().st_mtime),
        **drc_comparison(report, full),
    }
    relevant = [
        {"issue": issue["issue"], "severity": issue["severity"], "detail": issue["detail"]}
        for issue in issues if issue["severity"] in ("error", "warning")
    ]
    if relevant:
        block["report_issues"] = relevant
    if filtered:
        block["note"] = (
            "The report's counts are the whole board's; the net lists cover the nets "
            "checked."
        )
    return block


@mcp.tool(
    title="Check PCB Health",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_pcb_health(
    checks: list[str] | None = None,
    units: PcbUnits | None = None,
    offset: int = 0,
) -> str:
    """Run Altium's PCB Health Check Monitor offline on the saved PcbDoc, plus a few
    board-hygiene checks. Call it when the user asks for a PCB health check, board
    clean-up or hygiene, or before generating fabrication outputs (a fabricator's
    capability limits: check_dfm). Each finding names
    Altium's category and id, with Altium's level as severity (High error, Medium
    warning, Low info): shelved_modified_polygons (stops output generation in Altium),
    duplicate_designators, zero_area_polygons, zero_area_regions,
    self_intersecting_regions, micro_segments_board and micro_segments_copper (region
    edges under 0.03 mil), slot_holes, broken_xsignals, rotation_360,
    reference_point_outside, non_signal_layer_components, unused_xsignals,
    invalid_rules; micro_segments_graphics only when named in checks (Altium runs it in
    debug mode). Not Altium items: components_outside_board (warning when the part has
    netted pads, often parked parts), duplicate_copper, outline_micro_segments.
    altium_report puts the last saved Batch DRC report's health counts next to these.
    not_checked lists what cannot be done offline (mirrored footprints, panels, fonts,
    From-Tos) and checks turned off in the board's own Health Check settings.

    checks narrows to some of these; findings are paginated (offset from next_offset),
    errors first; many objects in one check become one finding listing them.
    Coordinates are relative to the board origin, in units (default: board display
    unit)."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, resolved, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    project, _, _ = _require_project(netlist=False)
    paths = project.get("pcb_doc_paths") or []
    context: dict = {"units": resolved}
    report_health = None
    if paths:
        pcb_path = Path(paths[0])
        context["pcb_doc"] = pcb_path.name
        if len(paths) > 1:
            context["note"] = (
                f"Project lists {len(paths)} PcbDocs; checked '{pcb_path.name}'."
            )
        try:
            report, report_path, _, _, report_notes = load_report(project, pcb_path)
        except Exception:  # a broken report must not stop the health check
            report, report_path, report_notes = None, None, []
        if report is not None:
            context["altium_report_file"] = report_path.name
            report_saved = report_path.stat().st_mtime
            context["altium_report_saved_utc"] = _utc(report_saved)
            cautions = [note["detail"] for note in report_notes if note.get("detail")]
            tables = [table for table in report.tables if table.type == "health"]
            broken = [problem for problem in report.problems if not count_problem(problem)]
            listed = sum(table.count for table in tables)
            if broken:
                cautions.append(
                    "The report is incomplete (empty or cut short), so it is not compared: "
                    "run the Batch DRC again. " + "; ".join(broken[:3])
                )
            elif tables or "health_issues" in report.totals:
                report_health = {table.title: table.count for table in tables}
                if report.totals.get("health_issues", listed) > listed:
                    cautions.append(
                        f"The report counts {report.totals['health_issues']} health issues "
                        f"but its tables list {listed}: the comparison may be incomplete."
                    )
                if report_saved < pcb_path.stat().st_mtime:
                    cautions.append(
                        "The report is older than the saved board: its counts may be out "
                        "of date."
                    )
            else:
                option = index.pcb.drc_options.include_health if index.pcb.drc_options else None
                cautions.append(
                    "The report has no PCB health section, so there is nothing to compare: "
                    + ("'Report PCB Health Issues' is off in the Batch DRC report options."
                       if option is False else
                       "'Report PCB Health Issues' was off, or the Altium version that ran "
                       "it does not offer it.")
                )
            if cautions:
                context["altium_report_notes"] = cautions
    for limit in (100, 40, 15, 5):
        result = run_health_check(
            index.pcb, checks=checks, to_mm=to_mm, offset=offset, limit=limit,
            report_health=report_health,
        )
        if "error" in result:
            return json.dumps(result)
        result = _convert_units({**context, **result}, to_mm)
        if limit < 100:
            result["findings_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# check_dfm's limits as a client sees them: an object of limit keys, each a number, a
# list of numbers or null. The argument itself is typed Any, so that fab_profiles
# checks every value: a wrong type answers invalid_limits with the valid keys instead of
# a pydantic error, and true/false or "0.1" are refused instead of read as numbers.
DFM_LIMITS_SCHEMA = {
    "anyOf": [
        {"type": "object", "additionalProperties": {"anyOf": [
            {"type": "number"}, {"type": "array", "items": {"type": "number"}},
            {"type": "null"},
        ]}},
        {"type": "null"},
    ],
}


@mcp.tool(
    title="Check Manufacturability (DFM)",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_dfm(
    checks: list[str] | None = None,
    profile: str | None = None,
    limits: Annotated[Any, WithJsonSchema(DFM_LIMITS_SCHEMA)] = None,
    units: PcbUnits | None = None,
    offset: int = 0,
) -> str:
    """Design-for-manufacture (DFM) and assembly (DFA) check of the saved PcbDoc against
    a fabricator's or assembler's capability limits: "is this board manufacturable?",
    "DFM check", "can my fab / Altimade / JLCPCB / PCBWay make this?", "check against my
    fab's capabilities", before ordering boards; "can it be assembled?", "DFA check",
    fiducials, tombstoning, part spacing, parts near the edge, "too many unique parts
    for Altimade?", "is the pick-and-place file right?". The answer is "meets
    <profile>'s limits on the checks run", never "manufacturable" in general: name
    the profile (scope says which), and never present
    Altimade's result as another fabricator's. When the user names another fab or
    assembler, ask for its numbers (or a fab_profile.json) and pass profile="none" with
    limits. Not: the board's own rules against its copper (check_rule_compliance);
    Altium's DRC result (get_drc_report); via inventory, back drills and stubs
    (check_vias); Altium's health check (check_pcb_health); the parts list (get_bom,
    check_bom); a full manufacturing review (pcb_manufacturing_review).

    profile: "altimade" (built in: Altium's Altimade ordering service, its published
    limits), "none" (only limits apply) or a .json file in the project folder ("acme"
    for acme.json). Default: the project's fab_profile.json (chosen_by project_file),
    else altimade (chosen_by default). limits sets single values on top, with the keys
    of the file and of the echoed profile: lengths end in _mm or _mil
    ({"min_spacing_mm": 0.1}), ratios and counts take no suffix ({"max_aspect_ratio": 8,
    "layer_counts": [2, 4]}); a value is a number or a list of numbers, never text, and
    null removes a limit. min_hole_to_copper falls back to min_spacing and
    min_hole_to_edge to min_copper_to_edge. The errors profile_not_found,
    invalid_profile and invalid_limits say what to fix: tell the user, and never
    substitute another profile or switch a limit's unit without the fab's table.

    checks: trace_width, spacing (hole walls to copper too), hole_size, annular_ring,
    aspect_ratio (copper-to-copper depth over the hole as drawn), hole_to_hole
    (overlapping holes whatever the profile), copper_to_edge (holes to the edge too),
    layer_count, copper_weight, board_size, drill_count, rules_vs_profile (a board rule
    looser than the profile, so Altium's DRC would let it through). Assembly, for the
    active variant (its Not Fitted parts left out): part_counts (placements and unique
    parts against max_placements / max_unique_parts, Altimade's ordering limits; unique
    parts are the BOM's lines with a netlist, else approximate), assembly_sides (SMD on
    both sides, THT on the bottom: extra steps, info), part_spacing (same-side parts by
    3D body outline and pad copper; overlapping parts), part_to_edge, fiducials
    (global per assembled side, local near fine-pitch parts), tombstoning (small
    passives whose pads meet their copper unequally), fine_pitch and pick_and_place
    (Center-X/Y as Altium's pick-and-place file writes it, the midpoint of the outermost
    pad centres: fitted parts without pads, written at the absolute origin, are a
    warning when in the BOM or fiducials; parts whose Center-X/Y is off their 3D body,
    a board origin away from the outline's lower-left corner and Ref-X/Y far from
    Center-X/Y are info; no limit needed). An assembly limit
    the profile does not set (min_part_spacing, min_part_to_edge, min_global_fiducials,
    local_fiducial_pitch, min_pitch) is measured, not judged: ask the assembler.
    results gives each check's status with its severity, limit and worst value: answer
    from status and severity together. Status is fail (a miss of severity error or
    warning), marginal (every such miss within tolerance_percent of its limit, default
    2% and at most 1 mil, e.g. 0.1 mm against 4 mil: a warning), advisory (only info
    misses, such as Altimade's ordering limits while Altimade is only the default: not a
    fabrication miss, report it as a note), pass, or not_checked with a reason.
    board_minimums is what the board needs from any fab or assembler, whatever the
    profile; drills is the drill table. Findings are classes (a width, a family of
    gaps, a drill row or via type) with counts (spacing counts places: spots where two
    nets' copper comes too close) and the worst examples, errors first. Quote
    not_checked (solder mask, silkscreen, plane pullback, panels, courtyards, paste,
    pick-and-place rotations).

    checks narrows to some of these; findings are paginated (offset from next_offset).
    Example locations (at_mil) are relative to the board origin: add origin_mil for
    query_pcb_region. Lengths use units (default: board display unit); limits carry
    their own unit suffix."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, resolved, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    project, netlist, variant_state = _require_project(netlist=False)
    fab_profile, error = load_profile(profile, project.get("root_dir"), limits)
    if error:
        return json.dumps(error)
    # Cached on the index per (checks, units, profile, variant): paging and repeat calls
    # reuse it.
    full = review_dfm(index, fab_profile, checks, to_mm,
                      _dfm_variant(project, netlist, variant_state))
    if "error" in full:
        return json.dumps(full)
    paths = project.get("pcb_doc_paths") or []
    context: dict = {"units": resolved}
    if paths:
        context["pcb_doc"] = Path(paths[0]).name
        if len(paths) > 1:
            context["note"] = (
                f"Project lists {len(paths)} PcbDocs; checked '{Path(paths[0]).name}'."
            )
    # Every answer needs the scope, and example locations need the origin: both lead.
    context["origin_mil"] = full["origin_mil"]
    context["scope"] = full["scope"]
    return _dfm_page(context, full, offset, to_mm)


def _dfm_variant(project: dict, netlist: dict | None,
                 variant_state: VariantState) -> AssemblyContext:
    """check_dfm's assembly context: the active variant's Not Fitted parts and, with a
    netlist, the BOM line count of its fitted parts (grouped as get_bom groups them)."""
    active = variant_state.active
    unique = None
    if netlist is not None and netlist.get("components"):
        plan, _, _ = _bom_plan(netlist, variant_state, None)
        parts, _ = build_bom_parts(netlist, plan, project.get("parameters") or {})
        unique = len(group_bom_lines([part for part in parts if part.fitted]))
    return AssemblyContext(
        variant=active.name, is_dnp=variant_state.is_dnp,
        dnp_key=tuple(sorted(set(active.dnp_refdes))), unique_parts=unique,
        netlist_free=netlist is None,
    )


DFM_FINDINGS_OMITTED = (
    "The result is too large even at 5 findings per page, so findings are left out: call "
    "again with checks narrowed to one check."
)


def _dfm_page(context: dict, full: dict, offset: int, to_mm: bool) -> str:
    """The largest page of check_dfm's result that fits the result size: fewer findings
    and examples per step, then the drill table cut to its first rows, and at worst the
    verdict without findings. Never longer than the limit."""
    steps = [(limit, examples, None) for limit, examples in SHRINK_STEPS]
    steps.append((*SHRINK_STEPS[-1], DRILL_ROWS_LISTED))
    for limit, examples, drill_rows in steps:
        result = _convert_units(
            {**context, **page_dfm(full, offset, limit, examples, drill_rows)}, to_mm,
        )
        if (limit, examples) != SHRINK_STEPS[0]:
            result["findings_limited_to"] = limit
            result["examples_per_finding"] = examples
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            return rendered
    counts = {"total": len(full["findings"]), "drills_total": len(full["drills"]),
              "findings_omitted": DFM_FINDINGS_OMITTED}
    brief = {
        **context,
        **{key: full[key] for key in (
            "checks_run", "profile", "board", "board_minimums", "results", "severity_counts",
        )},
        **counts,
    }
    rendered = json.dumps(_convert_units(brief, to_mm), indent=2)
    if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
        return rendered
    statuses = {check: entry["status"] for check, entry in full["results"].items()}
    return json.dumps({
        "units": context["units"], "scope": full["scope"], "results": statuses,
        "severity_counts": full["severity_counts"], **counts,
    }, indent=2)


BOM_LINE_LIMIT = 100
BOM_BASE_NAMES = ("[no variations]", "no variations", "none", "base")


def _bom_plan(netlist: dict, variant_state: VariantState, variant: str | None):
    """The variant plan for a BOM call: the active variant, a named one, or none (a
    project's own variant named e.g. 'Base' wins over the base-design aliases)."""
    names = variant_names(netlist)
    if variant is None:
        chosen = variant_state.active
    else:
        chosen = variant_state.get(variant.strip())
        if chosen is None and variant.strip().lower() in BOM_BASE_NAMES:
            return plan_variant(None, netlist, names), names, None
        if chosen is None:
            return None, names, _variant_not_found(variant, variant_state)
    return plan_variant(chosen, netlist, names), names, None


def _variant_not_found(variant: str, variant_state: VariantState) -> dict:
    return {
        "error": "variant_not_found",
        "message": f"No variant '{variant}'. Variants: "
        + ", ".join(v.name for v in variant_state.variants)
        + f", or '{BASE_VARIANT}'.",
    }


def _bom_header(netlist: dict, plan, parts: list, excluded: dict) -> dict:
    fitted = [part for part in parts if part.fitted]
    counted = [part for part in fitted if needs_mpn(part)]
    header: dict = {
        "variant": plan.name,
        "netlist_updated_utc": _netlist_last_updated,
        "compiled_part_list": bool(netlist.get("parts")),
        "parts": {
            "in_bom": len(fitted),
            "not_fitted": len(parts) - len(fitted),
            "excluded_by_kind": {kind: len(names) for kind, names in excluded.items()},
        },
        "mpn_coverage_percent": (
            round(100 * sum(1 for part in counted if part.mpn) / len(counted))
            if counted else None
        ),
    }
    if plan.stale:
        header["stale_variant_entries"] = len(plan.stale)
    if any(part.alternate_part for part in fitted):
        header["alternate_parts_note"] = (
            "Alternate parts take their footprint from the variant; their library data "
            "(Comment, description, MPN) is not read, only the variant's parameter "
            "variations."
        )
    return header


@mcp.tool(
    title="Get BOM",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_bom(
    variant: str | None = None,
    group_by: Literal["mpn", "comment"] = "mpn",
    include_not_fitted: bool = False,
    compare_variants: bool = False,
    format: Literal["json", "csv"] = "json",
    offset: int = 0,
) -> str:
    """Bill of materials for a variant (default: the active one; '[No Variations]' for
    the base design): lines with quantity, designators, Comment, description, value,
    footprint, manufacturer and manufacturer part number. Built from the compiled parts
    (compile-masked parts are not in it); Graphical, Net Tie and Standard (No BOM) parts
    and the variant's Not Fitted parts are left out, its parameter variations applied.
    The MPN comes from part parameters, never from Comment ('N/A' and the like are
    shown as mpn_placeholder). group_by mpn groups by MPN, else Comment and footprint;
    comment always by Comment and footprint. A field the line's parts disagree on is in
    varies with its values; logical_designators maps a part to its sheet designator.

    include_not_fitted lists the Not Fitted parts as lines; compare_variants adds what
    each variant changes (Not Fitted parts, varied values, alternate parts); format csv
    returns the page as CSV text. Lines are paginated (offset from next_offset)."""
    try:
        project, netlist, variant_state = _require_project()
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    plan, names, error = _bom_plan(netlist, variant_state, variant)
    if error:
        return json.dumps(error)
    project_params = project.get("parameters") or {}
    parts, excluded = build_bom_parts(netlist, plan, project_params)
    fitted = [part for part in parts if part.fitted]
    not_fitted = [part for part in parts if not part.fitted]
    lines = group_bom_lines(fitted, group_by)
    header = _bom_header(netlist, plan, parts, excluded)
    offset = max(0, int(offset or 0))
    for limit in (BOM_LINE_LIMIT, 40, 15):
        page = lines[offset : offset + limit]
        result = {**header, "lines_total": len(lines), "offset": offset,
                  "has_more": offset + limit < len(lines)}
        if result["has_more"]:
            result["next_offset"] = offset + limit
        if format == "csv":
            result["csv"] = bom_csv(page)
        else:
            result["lines"] = page
        if not_fitted:
            if include_not_fitted:
                result["not_fitted"] = group_bom_lines(not_fitted, group_by)
            else:
                result["not_fitted_designators"] = [part.designator for part in not_fitted]
        if compare_variants:
            real = [
                v for v in variant_state.variants
                if v.variations or v.param_variations or v.dnp_refdes
            ]
            result["variants"] = compare_bom_variants(netlist, real, names, project_params)
        if limit < BOM_LINE_LIMIT:
            result["lines_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


@mcp.tool(
    title="Check BOM",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_bom(
    variant: str | None = None,
    checks: list[str] | None = None,
    offset: int = 0,
) -> str:
    """Offline BOM hygiene checks for a variant (default: the active one): duplicate
    designators, special strings ('=Value') that name no parameter, fitted parts whose
    Value or Comment says DNP, parts without a footprint, one MPN with different values,
    descriptions or footprints, an MPN without a manufacturer, parts without an MPN
    (only where the project keeps MPNs in parameters), placeholder descriptions, variant
    entries that no longer fit their parts (designator changed, part gone), passives that
    could share one part number, and sheet parts left out of the compiled design.
    Supply data (lifecycle, stock, price) is not checked.

    checks narrows to some of these (names as in summary). Findings are paginated
    (offset from next_offset), errors first."""
    try:
        project, netlist, variant_state = _require_project()
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    plan, _, error = _bom_plan(netlist, variant_state, variant)
    if error:
        return json.dumps(error)
    parts, excluded = build_bom_parts(netlist, plan, project.get("parameters") or {})
    header = _bom_header(netlist, plan, parts, excluded)
    for limit in (100, 40, 15):
        result = run_bom_checks(parts, netlist, plan, checks=checks, offset=offset,
                                limit=limit)
        if "error" in result:
            return json.dumps(result)
        result = {**header, **result}
        if limit < 100:
            result["findings_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


GUIDE_APPROXIMATE_NOTE = (
    "No netlist with schematic parts is loaded: steps group parts by footprint and the "
    "PCB's Comment (approximate BOM lines, no MPN), and Not Fitted parts come from the "
    "designators stored in the project's variants."
)
GUIDE_UNAPPLIED_NOTE = (
    "Not Fitted entries of variant '{variant}' with no designator (only a UniqueId): "
    "{count}. Without the schematic parts they cannot be bound, so their parts may be "
    "listed as placed: check the variant in Altium. Open the project in Altium Designer "
    "once, then call set_project_dir again for a guide that leaves them out."
)
GUIDE_TEXT_OMITTED = (
    "The page is too large even at 5 rows, so its rows are left out: call again with side "
    "set to top or bottom."
)


def _guide_variant(project: dict, netlist: dict | None, variant_state: VariantState,
                   variant: str | None):
    """(GuideVariant, error) for get_assembly_guide: with schematic parts the variant's
    BOM as get_bom builds it (Not Fitted parts bound by UniqueId, parameter variations
    applied), else the designators stored in the project's variant."""
    if netlist is not None and netlist.get("components"):
        plan, _, error = _bom_plan(netlist, variant_state, variant)
        if error:
            return None, error
        parts, excluded = build_bom_parts(netlist, plan, project.get("parameters") or {})
        no_bom = frozenset(name for names in excluded.values() for name in names)
        return GuideVariant(plan.name, plan.not_fitted.__contains__,
                            {part.designator: part for part in parts}, no_bom), None
    chosen = variant_state.active if variant is None else variant_state.get(variant.strip())
    if chosen is None:
        if variant.strip().lower() not in BOM_BASE_NAMES:
            return None, _variant_not_found(variant, variant_state)
        return GuideVariant(BASE_VARIANT), None
    return GuideVariant(chosen.name, frozenset(chosen.dnp_refdes).__contains__,
                        unapplied=len(chosen.dnp_uid or ())), None


@mcp.tool(
    title="Get Assembly Guide",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_assembly_guide(
    variant: str | None = None,
    side: Literal["top", "bottom", "both"] | None = None,
    format: Literal["markdown", "csv"] = "markdown",
    units: PcbUnits | None = None,
    offset: int = 0,
) -> str:
    """Assembly sequence for a prototype build of a variant (default: the active one;
    '[No Variations]' for the base design) from the saved PcbDoc: "how do I assemble /
    hand-solder this board?", "assembly guide", "build sheet", "placement order",
    "soldering checklist". Parts are grouped into steps per side and process by BOM line
    (get_bom's grouping, MPN first; without a netlist by footprint and Comment, marked
    approximate) and ordered: the SMD of both sides first, the side whose tallest SMD
    part is shorter (else the one with fewer SMD parts, else the bottom) first, so the
    taller side is soldered last; then the through-hole parts (top, then bottom); on
    each side short to tall (3D body height, else at most a chip part's width, parts
    with no height last). first_side_reason and sequence_rules say why: quote them. Each
    step has its quantity, Comment, footprint, MPN and height, and per part the
    designator, Center-X/Y (as Altium's pick-and-place file writes it) and Ref-X/Y from
    the board origin, rotation and, for polarized parts (diodes, LEDs, polarized
    capacitors, ICs, transistors, connectors), where pin 1, the cathode or + lies from
    the part's centre ('pin 1 top-left', seen from the side being assembled: the bottom
    flipped left to right) or 'no pin 1 marked'. Left out and listed: the variant's Not
    Fitted parts, fiducials, no-BOM kinds, mounting holes, parts without pads and, with
    a netlist, PCB parts not in the BOM and BOM parts not on the board. Quote notes
    (without schematic parts: approximate steps, and Not Fitted entries known only by
    UniqueId that could not be applied) and polarized_parts.note (diodes and polarized
    capacitors with numbered pads: pin 1 is not said to be the cathode or +). No board
    map is drawn (get_component_placement and query_pcb_region locate a part).

    format markdown is a printable checklist (Done and Skipped columns; the first page
    opens with the sequence, the last lists what was left out); csv has one part per row
    with every column. side narrows to top or bottom. Rows are paginated (offset from
    next_offset). Coordinates and heights use units (default: board display unit). Not:
    the pick-and-place data check (check_dfm pick_and_place); the parts list (get_bom)."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, resolved, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    if side not in (None, "top", "bottom", "both"):
        return json.dumps({"error": "invalid_side", "message": "side is top, bottom or both."})
    if format not in ("markdown", "csv"):
        return json.dumps({"error": "invalid_format",
                           "message": "format is markdown or csv."})
    project, netlist, variant_state = _require_project(netlist=False)
    chosen, error = _guide_variant(project, netlist, variant_state, variant)
    if error:
        return json.dumps(error)
    side = None if side == "both" else side
    guide = build_guide(index, chosen, side)
    paths = project.get("pcb_doc_paths") or []
    header: dict = {"units": resolved}
    if paths:
        header["pcb_doc"] = Path(paths[0]).name
    header["origin_mil"] = [round(value, 3) for value in guide.origin]
    header.update(guide_summary(guide, to_mm))
    notes = [GUIDE_APPROXIMATE_NOTE] if guide.approximate else []
    if chosen.unapplied:
        notes.append(GUIDE_UNAPPLIED_NOTE.format(count=chosen.unapplied, variant=chosen.name))
    if notes:
        header["notes"] = notes
    header = _convert_units(header, to_mm)
    title = f"Assembly guide: {project.get('name')}, variant '{guide.variant}'"
    if side:
        title += f", {side} side"
    offset = max(0, int(offset or 0))
    for limit in GUIDE_ROW_LIMITS:
        result = {**header, **page_guide(guide, offset, limit, format, to_mm, title)}
        if limit < GUIDE_ROW_LIMITS[0]:
            result["rows_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            return rendered
    brief = {key: value for key, value in result.items() if key not in ("markdown", "csv")}
    brief.pop("left_out", None)
    brief["text_omitted"] = GUIDE_TEXT_OMITTED
    return json.dumps(brief, indent=2)


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
        project, netlist, _ = _require_project(netlist=False)
    except ProjectNotReady as error:
        return json.dumps(error.payload)
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
    try:
        project, netlist, variant_state = _require_project(netlist=False)
    except ProjectNotReady:
        return json.dumps(REVIEW_NO_PROJECT_ERROR)
    index, error = _pcb_index_or_error()
    if error:
        error["hint"] = (
            "constraints_review needs the project's PcbDoc; for the circuit itself "
            "use schematic_review."
        )
        return json.dumps(error)
    review = build_constraints_review(project, variant_state.active.name, index)
    if netlist is None:
        review += CONSTRAINTS_REVIEW_NETLIST_FREE_NOTE
    return review


@mcp.tool(title="PCB Manufacturing Review", annotations=ToolAnnotations(readOnlyHint=True))
def pcb_manufacturing_review() -> str:
    """Start a structured manufacturing-readiness review of the saved PCB before ordering:
    fabrication against the user's fabricator (check_dfm with its profile, check_vias,
    check_pcb_health, check_connectivity, get_drc_report), then assembly (check_dfm's
    assembly checks, get_bom and check_bom), ending in a go / no-go that lists what was
    not checked. It asks which fabricator first and never presents Altimade's limits as
    another fab's. Call when the user asks for a manufacturing, DFM/DFA or production
    readiness review ('is this board ready to order?', 'review it for manufacturing',
    'go/no-go before fab', 'can it be built and assembled?'). Not for one check against a
    fab profile (check_dfm), the high-speed constraints (constraints_review) or the
    circuit (schematic_review)."""
    try:
        project, netlist, variant_state = _require_project(netlist=False)
    except ProjectNotReady:
        return json.dumps(REVIEW_NO_PROJECT_ERROR)
    index, error = _pcb_index_or_error()
    if error:
        error["hint"] = (
            "pcb_manufacturing_review needs the project's PcbDoc; for the parts list alone "
            "use get_bom and check_bom."
        )
        return json.dumps(error)
    return build_manufacturing_review(
        project, variant_state.active.name, index, netlist_free=netlist is None,
    )


REVIEW_NO_PROJECT_ERROR = {
    "error": "no_project",
    "message": (
        "No project loaded. Run the session-start flow first: "
        "detect_altium_project → set_project_dir → list_variants → set_active_variant."
    ),
}

CONSTRAINTS_REVIEW_NETLIST_FREE_NOTE = """
---

Netlist-free load: this project was loaded without a netlist (Altium Designer was not
running), so search_components, get_component and query_net answer netlist_required.
Find the interfaces from the board instead: list_diff_pairs, get_classes and net names,
then get_component_placement for the parts on those nets (its pins list each pad's net).
The PCB holds no part descriptions or pin names: ask the user which parts are the
interface ICs, and say in the report that pins were not checked against the schematic.
"""


@mcp.tool(title="Schematic Review", annotations=ToolAnnotations(readOnlyHint=True))
def schematic_review() -> str:
    """Start a structured schematic review. Call when the user explicitly asks to review
    or verify the schematic (e.g. 'review this', 'check my schematic', 'do a design review',
    'is this correct?', 'does this look right?'). Do not call for general questions."""
    try:
        _require_project()
    except ProjectNotReady as e:
        if e.payload["error"] == "netlist_required":
            return json.dumps(e.payload)
        return json.dumps(REVIEW_NO_PROJECT_ERROR)

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


@mcp.tool(title="Project Tour", annotations=ToolAnnotations(readOnlyHint=True))
def project_tour() -> str:
    """Start a guided tour of the loaded project for a newcomer, in the order Altium
    teaches a design: project and sheet hierarchy (get_design_hierarchy, list_variants),
    each schematic block and the power rails, the PCB (stackup and impedance, placement,
    key nets), outputs and manufacturing state (DRC report, health, BOM), then variants
    and design notes. It ends in a Markdown project primer with a fixed outline, which
    Claude shows and saves only where the user agrees. Adapts to the load: a
    netlist-free load tours what the files show, a project without a PcbDoc skips the
    board, a PCB-only project the schematic. Call when the user is new to the project or
    asks for a walk-through ('walk me through this design', 'give me a tour', 'onboard
    me', 'write a project primer'). Not
    for a quick summary (the session start covers it) or a review (schematic_review,
    constraints_review, pcb_manufacturing_review)."""
    try:
        project, netlist, variant_state = _require_project(netlist=False)
    except ProjectNotReady:
        return json.dumps(REVIEW_NO_PROJECT_ERROR)
    return build_project_tour(
        project, variant_state.active.name, defined_variant_count(variant_state.variants),
        netlist_free=netlist is None,
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
    except ProjectNotReady as e:
        if e.payload["error"] == "netlist_required":
            return json.dumps(e.payload)
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
