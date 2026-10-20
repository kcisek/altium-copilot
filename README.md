# Altium Copilot

[![CI](https://github.com/ee-in-a-box/altium-copilot/actions/workflows/ci.yml/badge.svg)](https://github.com/ee-in-a-box/altium-copilot/actions/workflows/ci.yml)
[![Downloads](https://img.shields.io/github/downloads/ee-in-a-box/altium-copilot/total?color=brightgreen)](https://github.com/ee-in-a-box/altium-copilot/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)]()

**Software engineers have Claude Code. Hardware engineers have been pasting netlists into chat.**

> Read the full writeup → [maisamp.github.io](https://maisamp.github.io/posts/altium-copilot/)

Altium Copilot gives Claude read access to your Altium project: schematics, board, design rules, BOM, DRC report and design notes. Ask it about component values, trace a net across sheets, check a net's routing against its rules, pull a per-variant BOM, review your schematic, brainstorm a circuit — all from your Claude chat interface, grounded in your actual design files.

For now, the copilot is read-only. It only extracts data. It cannot modify your schematic, move components, or overwrite your `.PrjPcb` file, **more features coming soon.** The only files it creates are the Protel netlist it asks Altium to export and, when you ask for one, a `package_for_xfn` snapshot.

Part of the [ee-in-a-box](https://github.com/ee-in-a-box) suite.

![Demo](assets/Intro.gif)



---

## What it does

| What you ask | What happens | Tool |
|---|---|---|
| *"What's on the power sheet?"* | Every component on the sheet with its pins and nets, one-hop cross-sheet neighbours, and the sheet's notes | `get_sheet_context` |
| *"What's connected to the 3.3V net?"* | Traces the net across all sheets | `query_net` |
| *"What's different in the Prototype variant?"* | Compares DNP lists between variants; the BOM adds varied values and alternate parts | `list_variants`, `get_bom` |
| *"Review this power supply circuit"* | Structured multi-phase schematic review | `schematic_review` |
| *"What does U12 do?"* | Pulls component, MPN, value, DNP status, all pins, connected nets | `get_component` |
| *"Find every 10k resistor"* | Regex search over refdes, MPN or description, grouped by MPN | `search_components` |
| *"Where does RMII_REF_CLK route?"* | Layers, routed length per layer, vias, endpoints | `get_net_pcb` |
| *"What runs within 10 mil of CLK on L3?"* | Crosstalk proximity with parallel run lengths | `get_net_neighbors` |
| *"Describe my stackup"* | Layer-by-layer: copper weight, dielectric, material | `get_board_info` |
| *"Give me the BOM for the Lite variant"* | Per-variant BOM from the compiled parts with MPN and manufacturer, variant differences, CSV; `check_bom` for missing or inconsistent part numbers | `get_bom`, `check_bom` |
| *"Are my ICs decoupled? Is C12 close enough to U3?"* | Decoupling coverage per rail and IC: rails with no capacitor to ground, only Not Fitted ones, or fed through a ferrite or resistor without their own; with the PcbDoc, each IC rail's nearest capacitors and their distance, and a warning above 5 mm. Advice, not DRC: rails come from net and pin names | `check_decoupling` |
| *"What vias does this board use? Any fab issues?"* | Via types by span, hole and pad with counts per type and net class, depth and aspect ratio from the stackup, annular ring, vias in pads, tenting, drill pairs and back drilling, and the via stubs left on differential pairs | `check_vias` |
| *"Is the board fully routed? What is left to route?"* | Per net, whether the saved copper (tracks, vias, pads, poured copper, regions, fills, internal planes) joins all its pads: routing progress with percentages, each missing connection between the nearest pads with its distance and near-miss copper gap, floating copper, and net antennae against the board's Net Antennae rule, beside Altium's last DRC report | `check_connectivity` |
| *"What does my DRC report say? Any shorts?"* | Altium's last Batch DRC report grouped by rule, component or net, with a warning when it stopped at the violation limit, skipped checks or is older than the board | `get_drc_report` |
| *"Is the PCB in sync with the schematic?"* | Offline Show Differences: missing or extra parts, footprint and comment changes, pads on the wrong net, and pins Update PCB would fail on | `check_sch_pcb_sync` |
| *"Is my USB pair routed for 90 ohm?"* | Impedance profiles: target, solved width/gap and calculated impedance per layer, and which rules use them; the pair's routed widths checked against its rule | `get_board_info`, `check_rule_compliance` |
| *"Where is U12? What's next to it?"* | Placement, side, rotation, nearest parts | `get_component_placement` |
| *"What's at X 1200, Y 850?"* | Nets, components and saved pour outlines near a board coordinate | `query_pcb_region` |
| *"What width and clearance apply to USB_D_P?"* | The effective design rule per kind, resolved by priority, with values per layer | `get_rules_for_net` |
| *"Which rules point at classes that no longer exist?"* | Every rule's kind, priority, scope and values, with scope names that match no net, class, pair or room | `get_design_rules` |
| *"Which nets are in the HighSpeed class?"* | Net, component, diff-pair, pad, polygon and xSignal classes with their members | `get_classes` |
| *"List my differential pairs and their skew"* | Pairs, classes, length per leg (pad-to-pad path plus via barrels), and the routing rule for each | `list_diff_pairs` |
| *"Do my tracks and vias meet their rules?"* | Widths per layer, via sizes, holes, annular rings, routing layers and length windows checked against the rule that applies to each object; optional approximate clearance spot-check | `check_rule_compliance` |
| *"Are my DDR length groups within tolerance?"* | Length and matched-length rules evaluated like Altium, xSignals through series parts included | `check_lengths` |
| *"How long is the path from U1-5 to J1-3?"* | One net, pair, xSignal or pin-to-pin path measured on the saved copper, via barrels included | `get_signal_length` |
| *"Any polarity swaps or missing AC caps on my pairs?"* | Undeclared pairs, swapped P/N at pins, asymmetric series parts, in-pair skew against the rule | `check_diff_pairs` |
| *"What did we note about the power input?"* | Schematic notes, text frames and labels, with the parts they sit next to | `get_design_notes` |
| *"What are the fabrication notes?"* | Draftsman notes, tables, view titles, drill table and layer stack notes | `get_design_notes` |
| *"Show me the picture on the PSU sheet"* | Pasted datasheet excerpts and diagrams, returned as images | `get_design_image` |
| *"Is my copy of this Altium 365 project up to date?"* | Where the local Git clone stands, read offline: when it was last updated from Altium 365, commits ahead or behind as of that update, design files changed locally, the last commit. Never fetches | `get_clone_status` |
| *"Package this project for the firmware team"* | A SQLite snapshot your teammates open without Altium (see [Sharing with your team](#-new-sharing-with-your-team)) | `package_for_xfn` |

---

## Specialist modes

Claude activates these automatically based on what you say — no commands needed.

| Mode | Triggers when you say... | What happens |
|---|---|---|
| `schematic_review` | *"review this"*, *"is this correct?"*, *"check my power supply"* | 3-phase structured audit: Claude reads datasheets and confirms its understanding before flagging anything. Every finding cites a datasheet value or a netlist fact. Ask Claude to write the report to a file if you want one. |
| `constraints_review` | *"check my high-speed constraints"*, *"are my DDR length rules right?"*, *"review the diff pairs before fab"* | 4-phase PCB constraints audit: Claude identifies each interface from its parts and pins, takes targets from the chip's own layout guide (typical values only as a labelled fallback), checks that pairs, classes, impedance profiles and rules encode them, then spot-checks widths, lengths, skew and neighbours on the saved copper. Checks the saved rules and copper; quotes Altium's last DRC report only with its assessment. |
| `brainstorm_circuits` | *"I want to add a new circuit, lets brainstorm"*, *"How would i improve this circuit"*, *"What power supply topology should I use here?"*  | 5-phase design session: reads your loaded schematic first (it also works without a loaded project), then asks one question at a time — problem, constraints, proposes 2–3 topologies with tradeoffs, checks your existing design before recommending anything new. |

---

## 🆕 PCB awareness

Altium Copilot now reads your `.PcbDoc` directly (the first one listed in the project).
Ask where nets route, what's near what (crosstalk), your full stackup and impedance
profiles, and where components sit.
It also reads the board's design rules, classes, differential pairs and xSignals, and works out
which rule applies to a net the way Altium does — including rules that only apply to
some of a net's objects (its vias, one layer, a room) and scopes that point at classes
or pairs that no longer exist. It also checks the saved copper against those rules —
widths, vias, holes, annular rings, routing layers, length groups and diff-pair
consistency — and on the DRC reports we compared, its width, via, hole and
routing-layer counts matched Altium's. `check_sch_pcb_sync` compares the board with
the compiled schematic the way Show Differences does, without running an ECO.
It reads the saved file and never runs Altium's DRC; `get_drc_report` reads the last
DRC report Altium saved and says when it should not be trusted.
Data refreshes automatically every time you save the board in Altium. Read-only,
like everything else.

Not modelled: solid pours' copper (a pour is its saved outline, plus its tracks and arcs
when hatched, so repour and save before asking about pours; shelved pours have no
copper), fills, regions and planes, pad slot holes and per-layer pad sizes
(pad holes and rings are not checked), and diff-pair uncoupled length. The checks list
what they left out in their results.

---

## Design notes

Claude also reads what you wrote into the project besides the circuit: notes (with
author and collapsed state), text frames, free text, pictures, directives and title
blocks on every schematic sheet, project parameters, and the notes, tables, view titles,
drill table and layer stack notes in your Draftsman (`.PCBDwf`) drawings. Pictures can be
shown to Claude as images. `package_for_xfn` snapshots carry the same notes (pictures are
described, not embedded). Review comments made in Altium 365 are stored in your cloud
workspace, not in the project files, so they are not available.

---

## What it reads

Everything comes from the files Altium saves. Nothing is written back to your design.

| File | What is read |
|---|---|
| `.PrjPcb` | Sheet list (subfolders, `..\` paths and device sheets included), variants, project parameters, output folder, Comparator settings |
| `.SchDoc` | Components, parameters, pins, footprints, compile masks, notes, text frames, labels, pictures, directives, title blocks |
| Protel netlist (`.NET`) | Connectivity and the compiled part list. Altium writes it: when it is missing or older than the sheets, the copilot sends Altium the keystrokes for Design » Netlist For Project » Protel. With Altium closed the project loads without it (see [Requirements](#requirements)) |
| `.Annotation` | Board designators of board-annotated and multi-channel designs |
| `.PcbDoc` | Tracks, arcs, vias, pads, saved pour outlines (shelved or not), the copper a pour was last poured into (solid pours' regions, hatched pours' tracks and arcs), copper regions and fills, internal plane nets, components and designators, stackup, impedance profiles, rules, classes, differential pairs, xSignals, DRC settings |
| `.PCBDwf` (Draftsman) | Notes, free text, tables, view titles, pictures, drill table, layer stack notes |
| `Design Rule Check - <PcbDoc>.html` or `.drc` | Altium's last Batch DRC report, from the project's output folder |
| Git clone of an Altium 365 project (`.git`) | When the origin is `https://<workspace>.365.altium.com/git/<project GUID>.git`: the project GUID, the last fetch time (`FETCH_HEAD`), the local commit against the remote-tracking branch as of that fetch, uncommitted changes and the last commit. Read with local `git` commands that cannot open a connection (`protocol.allow=never`) and do not rewrite the index (`--no-optional-locks`); nothing is fetched and no credential is used |

Not read: schematic wires (connectivity comes from the netlist), ERC results, OutJob
outputs and the DRC XML report, ActiveBOM and supply-chain data (lifecycle, stock,
price), and Altium 365 review comments. The copilot never runs Altium's ERC or DRC.

---

## Requirements

- Windows
- [Altium Designer](https://www.altium.com/altium-designer) — running with the project open and a schematic sheet active whenever the netlist has to be generated: the first time a project is loaded and after schematic changes
  - A project whose netlist is still up to date loads with Altium closed; the board, rule, DRC report, BOM and notes tools only read the saved files
  - Without Altium and without an up-to-date netlist (for example a fresh copy of an Altium 365 project, which never contains `Project Outputs`), the project loads partially: the board, rule, length, differential-pair, via, connectivity, DRC report, notes, picture and variant tools work. Nets, components, sheet context, BOM, decoupling, schematic-PCB sync, schematic review and the team snapshot need the netlist: open the project in Altium once, then load it again
  - Altium Designer 17 and older (`DXP.exe`) are detected too
- [Git](https://git-scm.com/download/win) (optional) — only to report where an Altium 365 project's local clone stands; without it everything else works
- [Claude Desktop](https://claude.com/download) or [Claude Code](https://claude.com/product/claude-code) — works with Pro and Enterprise subscriptions

---

## Install

1. Install [Claude Desktop](https://claude.com/download) or [Claude Code](https://claude.com/product/claude-code)
2. Open **PowerShell** (search `powershell` in the Start menu) and run:
   ```powershell
   irm https://raw.githubusercontent.com/ee-in-a-box/altium-copilot/main/install.ps1 | iex
   ```
3. Open Altium Designer with your project loaded
4. Start asking
   
---

## Usage

Once installed, open Claude and ask anything about your schematic — the copilot detects your running Altium instance and orients itself to your project automatically. With Altium closed, it offers the projects you loaded before. It asks which variant to work in, and every answer follows that variant's fitted and Not Fitted parts.

After you save schematic changes in Altium, tell Claude: it calls `refresh_netlist` to pick them up. Board changes are picked up on the next question after you save the `.PcbDoc`.

**Example prompts:**
- *"What's connected to the 3.3V net?"*
- *"What's U4 doing on this page?"*
- *"What's different between my schematic variants?"*
- *"How should i improve this circuit?"*
- *"Is this power supply circuit correct?"* — triggers a structured design review automatically
- *"Is the PCB in sync with the schematic?"*
- *"Give me the BOM for this variant as CSV"*

---

## Privacy

Your data stays yours. The MCP server runs locally and sends no telemetry: its only network request is a check for a new release on GitHub, at most once a day, and it carries no project data. For Altium 365 projects it runs local `git` commands in the project's clone, which are blocked from contacting any server. It keeps the list of projects you loaded in `%USERPROFILE%\.ee-in-a-box\`, and its warnings go to your MCP client's log. All schematic data is sent only to Claude via your own Enterprise or Pro subscription.

---

## 🚀 NEW: Sharing with your team

Want to share the same capabilities with firmware, mechanical, and test engineers without them needing an Altium license?

Ask Claude to package your project:

> *"Package this Altium project to share with the cross functional team"* / *"Export this Altium project for sharing"*

Claude calls `package_for_xfn`, which writes a portable SQLite snapshot (`<project>-pcb-copilot.db`) next to your `.PrjPcb`. Share that file with your team via Confluence, Slack, or a shared drive.

The snapshot holds the schematic side: sheets, variants, components, nets, pins and the design notes (pictures as metadata only, and file names instead of local folders). Board, rule, BOM and DRC data are not in it.

Your teammates open it with **[pcb-copilot](https://github.com/ee-in-a-box/pcb-copilot)** — a companion MCP server that gives Claude the same schematic Q&A tools against the snapshot, no Altium license required. Pairs well with the free [Altium 365 Viewer](https://www.altium.com/altium-365/viewer) for visual reference alongside the AI Q&A.

---

## Give Claude system-level context

The schematic tools tell Claude what's in your design. A `CLAUDE.md` in your project folder tells it *why*.

Drop a `CLAUDE.md` file in the same folder as your `.PrjPcb` file — it's loaded automatically when the project opens. Put in what the schematic can't tell Claude:

- What the board does and where it lives in the system
- What it interfaces with upstream and downstream
- Terms that look similar but mean different things (board vs. assembly, vendor naming vs. internal naming)
- Key requirements — voltage rails, power budget, timing constraints, isolation barriers
- Pointers to your requirements doc, Confluence page, or block diagram

**Don't have time to write one?** Download the [template](examples/CLAUDE.md), upload it to Claude, and say: *"Use this template and what you can read from my schematic to draft a CLAUDE.md — then ask me one question at a time for anything you can't figure out from the netlist."*

---

## What i'm experimenting with next

- **Simulator** — walks you through simulating and validating your design before you spin a board
- **Librarian** — periodic audits of your component libraries, flags stale or discontinued entries
- **Supply Chain Expert** — component availability, lead times, alternatives, and sourcing risk

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

For bug reports and support, open an issue at [github.com/ee-in-a-box/altium-copilot/issues](https://github.com/ee-in-a-box/altium-copilot/issues).

---

## License

MIT © Maisam Pyarali
