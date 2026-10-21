"""Project tour: a newcomer's guided walk through the loaded project, ending in a primer.

Pure text building over the loaded project dict: no MCP, no file reads. The
project_tour tool returns build_project_tour(...) the way schematic_review returns its
prompt.

The tour follows the order Altium teaches a design: the project and its sheet
hierarchy, each schematic block and the power rails, the PCB (stackup, placement, key
nets), outputs and manufacturing state, then variants and design notes. It adapts to the
load: a netlist-free load swaps the schematic phase for what the files show without a
netlist, a PCB-only project skips the schematic phase and a project without a PcbDoc
skips the board phases. It ends in a Markdown
project primer with a fixed outline that the client shows and saves only with the
user's OK. The context lines hold counts and file names only.
"""

from pathlib import Path, PureWindowsPath

# Every tool the prompt can name; tests check each is registered on the server and
# that the prompt names no other registered tool.
PROJECT_TOUR_TOOLS = (
    "detect_altium_project",
    "set_project_dir",
    "project_tour",
    "get_design_hierarchy",
    "list_variants",
    "set_active_variant",
    "get_sheet_context",
    "get_design_image",
    "query_net",
    "get_board_info",
    "get_component_placement",
    "list_diff_pairs",
    "get_classes",
    "get_net_pcb",
    "check_lengths",
    "get_drc_report",
    "check_pcb_health",
    "get_bom",
    "get_output_jobs",
    "get_design_notes",
    "schematic_review",
    "constraints_review",
    "pcb_manufacturing_review",
)

# The primer's fixed outline, after its title line; tests check the prompt carries it.
PRIMER_SECTIONS = (
    "1. What the board does",
    "2. Sheet hierarchy and net scope",
    "3. Schematic blocks",
    "4. Power rails",
    "5. PCB stackup and impedance",
    "6. Placement and key nets",
    "7. Outputs and manufacturing state",
    "8. Variants",
    "9. Design notes and open questions",
    "10. Assumptions and not covered",
    "11. Where to go next",
)

# Above this many sheets the tour asks which blocks to cover first.
MAX_SHEETS_WITHOUT_ASKING = 12

PROJECT_TOUR_PROMPT = """\
Project: {name}
Folder: {root_dir}
Load: {load}
Schematic sheets: {sheet_count}
PCB: {pcb_doc}
Draftsman drawings: {drawings}
Variants: {variants}

You are giving a newcomer a guided tour of this project in the order Altium teaches a
design, ending in a project primer they keep. Before starting, ask the user in one
message: should the tour pause after each phase for questions, or run straight through;
and is there a part of the board to focus on? Do not proceed until the user answers.
(Another project: detect_altium_project → set_project_dir → project_tour again.)

## Ground Rules

- State only what the tools return, and cite it: sheet names, designators, net and
  layer names. Never fill a gap from memory.
- Mark every inference "Assumption:" with its evidence (e.g. "Assumption: U3 is the
  main MCU — description and pin count").
- A tool that answers an error: quote it, mark that item "not covered" and move on.
  A skipped phase keeps its primer section with the reason, never invented content.
- Work in the active variant ({active_variant}): Not Fitted parts are not populated.
  Reuse results already in this conversation (session start) instead of calling again.
- Read-only: the tour changes no design file. End each phase with a short summary (a
  table or about ten lines) and pause there if the user asked to; the detail goes in
  the primer.

---

### Phase 1 — Project and Hierarchy

1. get_design_hierarchy: the sheet tree (top sheet, sheet symbols, device sheets,
   Repeat channels) and the Net Identifier Scope. Explain in two sentences how nets
   cross sheets here (scope.explanation); quote issues with issue_help.
2. list_variants: each variant and its Not Fitted parts. If the user wants another
   variant, set_active_variant.

{phase_2}

{phase_3}

### Phase 4 — Outputs and Manufacturing State

{phase_4}
Then get_output_jobs: the OutJobs, what their connected outputs cover (coverage) and
the errors and warnings that would stop a release, with their fixes; no OutJob: say
the outputs are made by hand.
This is a snapshot, not a review.

### Phase 5 — Variants and Design Notes

1. {variants_step}
2. get_design_notes (every page): designer notes, open questions, DNP reasons, project
   parameters and the Draftsman fabrication and assembly notes. A note that contradicts
   a tool result is a mismatch to report, not a fact.

---

## Project Primer

Finish with the primer in Markdown, with exactly this outline (a section the tour could
not cover keeps its heading and says why):

# {name} — Project Primer
(variant, today's date if you know it, load mode, and that it was built from tool results)
{primer_sections}

Section 3 is a table | Sheet | Purpose | Key parts | Interfaces |; section 4 a table
| Rail | Source | Feeds | Evidence |, marked "derived, not a tool result". Section 11
points to the deeper modes where they apply: schematic_review, constraints_review,
pcb_manufacturing_review.

Show the primer in the chat, then ask whether to save it. Suggest this path, next to
the .PrjPcb: {primer_path}
If that folder is read-only, a shared archive or a managed library, ask the user for a
folder of theirs. This server has no save tool: write the file with your own file tool
only after the user agrees to a path, never overwrite a file without asking, and if
you cannot write files, tell the user to copy it.
"""

PHASE_2_SCHEMATIC = f"""\
### Phase 2 — Schematic Blocks

Visit the sheets in tree order (a Repeat channel or a sheet placed several times once;
more than {MAX_SHEETS_WITHOUT_ASKING} sheets: ask which blocks to cover first). For each,
get_sheet_context(sheet_name=...) and give its purpose, key ICs (designator,
description), interfaces (ports and connectors, and the sheets they reach per
connected_to) and notes worth knowing. A block diagram picture: get_design_image.

Power rails: no tool builds a power tree. Take the rail names from the power ports and
the regulators on the sheets read, confirm each with query_net (rails are high-fanout:
it samples them), and present the tree as your reading:
| Rail | Source (designator, sheet) | Feeds (sheets) | Evidence |"""

PHASE_2_NETLIST_FREE = """\
### Phase 2 — Schematic Blocks (limited: netlist-free load)

This load has no netlist, so get_sheet_context, query_net and get_bom answer
netlist_required: no parts, pins or rails come from the schematic. Say so, then per
sheet in tree order give what the files show: its ports and sheet symbols
(get_design_hierarchy(sheet=...)) and its notes and title block
(get_design_notes(document=...)). No tool builds a power tree, and none can be traced
without a netlist{rails_hint}.
Opening the project in Altium Designer once, then set_project_dir again, enables the
full tour."""

_RAILS_HINT_PCB = (
    ":\nname the rail nets the board shows (get_classes net classes, get_net_pcb on a named\n"
    "rail) as PCB data only"
)

# A PCB-only project (no .SchDoc) loads netlist-free, and the full Altium load refuses it
# (no_sheets), so it gets neither the sheet walk nor the "open it in Altium" hint.
PHASE_2_NO_SHEETS = """\
### Phase 2 — Schematic Blocks (skipped: no schematic sheets)

The project lists no .SchDoc sheet, so there are no schematic blocks, ports or power
ports to read, and opening it in Altium Designer gives no netlist either. Say so in the
primer. No tool builds a power tree{rails_hint}."""

PHASE_3_PCB = """\
### Phase 3 — PCB

1. get_board_info: size and outline, the stackup (copper layers, thickness,
   dielectrics), impedance profiles (target, width/gap, reference layers), origin and
   units, and {saved}.
2. Placement: get_component_placement (side, position, rotation, neighbours) for the
   key ICs and connectors{key_parts}; describe the floor plan by board region.
3. Key nets: list_diff_pairs and get_classes; for the few that matter (high-speed
   pairs, clocks, main rails) get_net_pcb (layers, length, vias). When the board has
   Length or MatchedLengths rules, quote their status from check_lengths."""

_KEY_PARTS_FULL = " of Phase 2"
_KEY_PARTS_NETLIST_FREE = " (the PCB holds no part\n   descriptions in this load: ask the user)"

# get_board_info's last_saved_utc and netlist_updated_utc (null without a netlist).
_SAVED_FULL = (
    "when the PcbDoc was saved and the netlist compiled (state both; the\n"
    "   times alone do not show whether the board matches the schematic)"
)
_SAVED_NETLIST_FREE = "when the PcbDoc was last saved (no netlist time in this load)"

PHASE_3_NO_PCB = """\
### Phase 3 — PCB (skipped: no PcbDoc)

The project lists no PcbDoc, so the board tools answer no_pcb_document. Skip stackup,
placement and key nets, and say so in the primer."""

_PHASE_4_DRC = (
    "1. get_drc_report: Altium's last Batch DRC; quote its assessment first (age, stopped\n"
    "   at the violation limit, rules not checked). no_drc_report: say none was found.\n"
    "2. check_pcb_health: counts per finding kind, and anything that stops output\n"
    "   generation (shelved or modified pours)."
)
_PHASE_4_NO_PCB = (
    "Without a PcbDoc there is no DRC report or board health to read: say so."
)
_PHASE_4_BOM = (
    "{n}. get_bom (active variant): line and part counts, MPN coverage, lines worth "
    "knowing."
)
_PHASE_4_NO_BOM = "get_bom needs the netlist: the BOM is not covered in this load."

_VARIANTS_FULL = (
    "Variants: what each one changes, from get_bom(compare_variants=True). No variant\n"
    "   defined: say the design has one build."
)
_VARIANTS_NETLIST_FREE = (
    "Variants: list_variants' Not Fitted designators, stored in the .PrjPcb (they can be\n"
    "   stale without a netlist). No variant defined: say the design has one build."
)


def _pcb_doc_line(paths: list) -> str:
    if not paths:
        return "none (the project lists no PcbDoc)"
    line = PureWindowsPath(paths[0]).name
    if len(paths) > 1:
        line += f" (the project lists {len(paths)} PcbDocs; the board tools read this one)"
    return line


def _phase_4(netlist_free: bool, has_pcb: bool) -> str:
    lines = [_PHASE_4_DRC if has_pcb else _PHASE_4_NO_PCB]
    if netlist_free:
        lines.append(_PHASE_4_NO_BOM)
    else:
        lines.append(_PHASE_4_BOM.format(n=3 if has_pcb else 1))
    return "\n".join(lines)


def defined_variant_count(variants: list) -> int:
    """How many variants the .PrjPcb defines. The parser stands in a bare 'Default'
    variant (nothing Not Fitted, nothing varied) when it defines none: that counts 0."""
    if len(variants) == 1:
        only = variants[0]
        if only.name == "Default" and not (only.dnp_refdes or only.dnp_uid
                                           or only.variations or only.param_variations):
            return 0
    return len(variants)


def primer_path(project: dict) -> str:
    """Where the tour suggests saving the primer: next to the .PrjPcb."""
    file_name = f"{project.get('name') or 'Project'}_primer.md"
    root = project.get("root_dir")
    return str(Path(root) / file_name) if root else file_name


def build_project_tour(project: dict, active_variant: str, variant_count: int, *,
                       netlist_free: bool = False) -> str:
    """The project-tour prompt for a loaded project. netlist_free swaps the schematic
    phase for the netlist-free one; a project without sheets skips the schematic phase,
    one without pcb_doc_paths the PCB phase and the board items of Phase 4."""
    pcb_paths = project.get("pcb_doc_paths") or []
    has_pcb = bool(pcb_paths)
    has_sheets = bool(project.get("sheets"))
    rails_hint = _RAILS_HINT_PCB if has_pcb else ""
    if not has_sheets:
        phase_2 = PHASE_2_NO_SHEETS.format(rails_hint=rails_hint)
    elif netlist_free:
        phase_2 = PHASE_2_NETLIST_FREE.format(rails_hint=rails_hint)
    else:
        phase_2 = PHASE_2_SCHEMATIC
    if has_pcb:
        phase_3 = PHASE_3_PCB.format(
            key_parts=(_KEY_PARTS_FULL if has_sheets and not netlist_free
                       else _KEY_PARTS_NETLIST_FREE),
            saved=_SAVED_NETLIST_FREE if netlist_free else _SAVED_FULL)
    else:
        phase_3 = PHASE_3_NO_PCB
    variants = (f"{variant_count} (active: {active_variant})" if variant_count
                else f"none defined in the .PrjPcb (the base design, shown as "
                     f"'{active_variant}')")
    return PROJECT_TOUR_PROMPT.format(
        name=project.get("name", "?"),
        root_dir=project.get("root_dir", ""),
        load=("netlist-free (no netlist: Altium Designer was not running)" if netlist_free
              else "full (compiled netlist)"),
        sheet_count=len(project.get("sheets") or []),
        pcb_doc=_pcb_doc_line(pcb_paths),
        drawings=len(project.get("pcb_dwf_paths") or []),
        variants=variants,
        active_variant=active_variant,
        phase_2=phase_2,
        phase_3=phase_3,
        phase_4=_phase_4(netlist_free, has_pcb),
        variants_step=_VARIANTS_NETLIST_FREE if netlist_free else _VARIANTS_FULL,
        primer_sections="\n".join(f"## {section}" for section in PRIMER_SECTIONS),
        primer_path=primer_path(project),
    )
