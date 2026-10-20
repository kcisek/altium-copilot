# Altium Copilot

[![CI](https://github.com/ee-in-a-box/altium-copilot/actions/workflows/ci.yml/badge.svg)](https://github.com/ee-in-a-box/altium-copilot/actions/workflows/ci.yml)
[![Downloads](https://img.shields.io/github/downloads/ee-in-a-box/altium-copilot/total?color=brightgreen)](https://github.com/ee-in-a-box/altium-copilot/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)]()

**Software engineers have Claude Code. Hardware engineers have been pasting netlists into chat.**

> Read the full writeup → [maisamp.github.io](https://maisamp.github.io/posts/altium-copilot/)

Altium Copilot gives Claude real-time read access to your open Altium project. Ask it about component values, trace a net across sheets, review your schematic, brainstorm a circuit — all from your Claude chat interface, grounded in your actual design files.

For now, the copilot is read-only. It only extracts data. It cannot modify your schematic, move components, or overwrite your `.PrjPcb` file, **more features coming soon.**

Part of the [ee-in-a-box](https://github.com/ee-in-a-box) suite.

![Demo](assets/Intro.gif)



---

## What it does

| What you ask | What happens |
|---|---|
| *"What's connected to the 3.3V net?"* | Traces the net across all sheets |
| *"What's different in the Prototype variant?"* | Compares DNP lists between variants |
| *"Review this power supply circuit"* | Structured multi-phase schematic review |
| *"What does U12 do?"* | Pulls component, all pins, connected nets |
| *"Where does RMII_REF_CLK route?"* | Layers, routed length per layer, vias, endpoints |
| *"What runs within 10 mil of CLK on L3?"* | Crosstalk proximity with parallel run lengths |
| *"Describe my stackup"* | Layer-by-layer: copper weight, dielectric, material |
| *"Give me the BOM for the Lite variant"* | Per-variant BOM from the compiled parts with MPN and manufacturer, variant differences, CSV; `check_bom` for missing or inconsistent part numbers |
| *"What does my DRC report say? Any shorts?"* | Altium's last Batch DRC report grouped by rule, component or net, with a warning when it stopped at the violation limit, skipped checks or is older than the board |
| *"Is the PCB in sync with the schematic?"* | Offline Show Differences: missing or extra parts, footprint and comment changes, pads on the wrong net, and pins Update PCB would fail on |
| *"Is my USB pair routed for 90 ohm?"* | Impedance profiles: target, solved width/gap and calculated impedance per layer, and which rules use them |
| *"Where is U12? What's next to it?"* | Placement, side, rotation, nearest parts |
| *"What width and clearance apply to USB_D_P?"* | The effective design rule per kind, resolved by priority, with values per layer |
| *"Which nets are in the HighSpeed class?"* | Net, component and diff-pair classes with their members |
| *"List my differential pairs and their skew"* | Pairs, classes, length per leg (pad-to-pad path plus via barrels), and the routing rule for each |
| *"Do my tracks and vias meet their rules?"* | Widths per layer, via sizes, holes and routing layers checked against the rule that applies to each object |
| *"Are my DDR length groups within tolerance?"* | Length and matched-length rules evaluated like Altium, xSignals through series parts included |
| *"Any polarity swaps or missing AC caps on my pairs?"* | Undeclared pairs, swapped P/N at pins, asymmetric series parts, in-pair skew against the rule |
| *"What did we note about the power input?"* | Schematic notes, text frames and labels, with the parts they sit next to |
| *"What are the fabrication notes?"* | Draftsman notes, tables, drill table and title-block data |
| *"Show me the picture on the PSU sheet"* | Pasted datasheet excerpts and diagrams, returned as images |

---

## Specialist modes

Claude activates these automatically based on what you say — no commands needed.

| Mode | Triggers when you say... | What happens |
|---|---|---|
| `schematic_review` | *"review this"*, *"is this correct?"*, *"check my power supply"* | 3-phase structured audit: Claude reads datasheets and confirms its understanding before flagging anything. Every finding cites a datasheet value or a netlist fact. Ask Claude to write the report to a file if you want one. |
| `constraints_review` | *"check my high-speed constraints"*, *"are my DDR length rules right?"*, *"review the diff pairs before fab"* | 4-phase PCB constraints audit: Claude identifies each interface from its parts and pins, takes targets from the chip's own layout guide (typical values only as a labelled fallback), checks that pairs, classes and rules encode them, then spot-checks widths, lengths, skew and neighbours on the saved copper. Checks the saved rules and copper; quotes Altium's last DRC report only with its assessment. |
| `brainstorm_circuits` | *"I want to add a new circuit, lets brainstorm"*, *"How would i improve this circuit"*, *"What power supply topology should I use here?"*  | 5-phase design session: reads your loaded schematic first, then asks one question at a time — problem, constraints, proposes 2–3 topologies with tradeoffs, checks your existing design before recommending anything new. |

---

## 🆕 PCB awareness

Altium Copilot now reads your `.PcbDoc` directly. Ask where nets
route, what's near what (crosstalk), your full stackup, and where components sit.
It also reads the board's design rules, classes and differential pairs, and works out
which rule applies to a net the way Altium does — including rules that only apply to
some of a net's objects (its vias, one layer, a room) and scopes that point at classes
or pairs that no longer exist. It also checks the saved copper against those rules —
widths, vias, routing layers, length groups and diff-pair consistency — and on the DRC
reports we compared, its counts matched Altium's. It reads the saved file and never runs
Altium's DRC; `get_drc_report` reads the last DRC report Altium saved.
Data refreshes automatically every time you save the board in Altium. Read-only,
like everything else.

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

## Requirements

- Windows
- [Altium Designer](https://www.altium.com/altium-designer) (must be running with a project open)
- [Claude Desktop](https://claude.com/download) — works with Pro and Enterprise subscriptions

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

Once installed, open Claude and ask anything about your schematic — the copilot detects your running Altium instance and orients itself to your project automatically.

**Example prompts:**
- *"What's connected to the 3.3V net?"*
- *"What's U4 doing on this page?"*
- *"What's different between my schematic variants?"*
- *"How should i improve this circuit?"*
- *"Is this power supply circuit correct?"* — triggers a structured design review automatically

---

## Privacy

Your data stays yours. The MCP server runs locally, doesn't log anything, and doesn't phone home. All schematic data is sent only to Claude via your own Enterprise or Pro subscription.

---

## 🚀 NEW: Sharing with your team

Want to share the same capabilities with firmware, mechanical, and test engineers without them needing an Altium license?

Ask Claude to package your project:

> *"Package this Altium project to share with the cross functional team"* / *"Export this Altium project for sharing"*

Claude calls `package_for_xfn`, which writes a portable SQLite snapshot (`.db`) next to your `.PrjPcb`. Share that file with your team via Confluence, Slack, or a shared drive.

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

- **PCB layout understanding** — same live-access model, extended to the board layer
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
