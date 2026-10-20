# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)

## [Unreleased]

### Added
- PCB design rules, classes and differential pairs, read from the `.PcbDoc` (`Rules6`,
  `Classes6`, `SignalClasses`, `DifferentialPairs6`, and xSignals from Board6). New tools:
  - `get_design_rules` — every rule's kind, priority, enabled flag and scope; per kind
    or name the values: widths, gaps, clearances with the object-to-object matrix,
    via sizes, lengths, per-layer values for the board's real signal layers, rooms.
    Flags `stale_references`: scope names that match no net, class, pair or room
  - `get_rules_for_net` — which rule of each kind applies to a net, pair or xSignal,
    resolved by priority like Altium. Scopes are evaluated with an Altium query-language
    subset that decides every scope seen on 61 real boards; rules that apply only to some
    of the net's objects (vias, a layer, a room, a component's pads, its pour copper) are
    listed with that condition instead of being claimed or dropped. Clearance-type
    rules list candidates with both orientations and their connective checking.
    Component and pad scopes use the PCB designator (decoded from `Texts6`), which
    differs from the schematic designator on multi-channel and board-annotated designs.
    For a diff-pair net, the note on Width points to the DiffPairsRouting rule that
    checks its track width
  - `get_classes` — net, component, diff-pair, pad, polygon, xSignal, layer, from-to and
    design-channel classes and their members, with unresolved members and DNP flags
  - `list_diff_pairs` — pairs with nets, classes, length per leg (pad-to-pad path plus
    via barrels; routed copper stays as `routed_length_mil`), skew and the routing rule
    for each pair
  - `get_net_pcb` adds the net's classes and pairs; `get_net_neighbors` marks the
    diff-pair partner; `get_board_info` adds rule, room, class and pair counts
- Rule-compliance, length and diff-pair checks on the saved copper. Rule scopes are
  decided per object (IsVia/IsTrack/IsPad, OnLayer and layer classes,
  WithinRoom/TouchesRoom, InComponent/HasPad/InPadClass, pad Name) and for xSignals
  (InxSignal, InxSignalClass, InAnyxSignal, IsxSignal). Lengths are measured like
  Altium's Signal Length: the copper path from pad centre to pad centre with true arc
  lengths, plus the via barrel over the layers it traverses, plus the pin/package length
  stored on the end pads; nets without exactly two pads use routed copper plus barrels,
  and xSignals are followed through series parts. New tools:
  - `check_rule_compliance` — track widths per layer (diff-pair nets against their
    DiffPairsRouting widths), via diameter and hole, via hole size and annular ring,
    routing layers and Length windows, with the rule resolved for each object: room,
    layer, via and pad scopes are decided per object. Optional approximate clearance
    spot-check. Says whether the board's own batch DRC checks each kind
  - `check_lengths` — Length and MatchedLengths rules evaluated like Altium: windows,
    matched-length groups (xSignals, pairs or nets) against the longest member or the
    target source, within-pair skew; stale scopes and conditional members reported
  - `get_signal_length` — one net, pair, xSignal or pin-to-pin path
  - `check_diff_pairs` — undeclared P/N pairs, polarity swaps at pins (with a hint when
    the interface tolerates inversion), series parts and AC coupling symmetry (value,
    footprint, DNP), in-pair skew against within-pair rules
  - What a check does not cover (pad holes and rings, uncoupled length, poured copper)
    is listed in its result
- `constraints_review`: a 4-phase audit of the board's high-speed interfaces, found from
  their parts and pins. Targets come from each chip's layout guide (typical values only
  as a labelled fallback); pairs, classes, impedance-driven rules, matched-length groups,
  clearances, reference layers and stale rules are checked against them, then
  the saved copper is spot-checked. Altium's last DRC report is quoted only with its
  assessment
- Impedance profiles from the Layer Stack Manager: `get_board_info` lists each profile's
  target and tolerance, its solved trace width/gap, calculated impedance and reference
  layers per signal layer, a derived propagation delay, the rules that use it, and issues
  (calculated impedance outside tolerance, profile-driven rule widths that drifted from the
  profile, unused profiles). Profile-driven rules name their profile in their values
- `check_sch_pcb_sync`: an offline Project > Show Differences. It compares the compiled
  schematic netlist with the saved PcbDoc and reports parts missing from or extra on the
  PCB, designator, footprint and comment differences, pads whose net differs from their
  pin's, renamed nets, nets on one side only, and schematic pins whose placed footprint
  has no pad (Update PCB stops with 'Unknown Pin'). Components link by unique ID first
  (channel copies by their full sheet-symbol path from the .Annotation file, else by
  their non-auto-named nets) and by designator second; nets match by shared members
  first and by name second, with the project's Comparator settings. Mechanical and
  graphical parts, variant alternates, copied footprints, removed channels, net ties,
  jumpers and single-pin nets are handled as Altium does, and every finding names its
  Altium difference type. Sheets saved after the PcbDoc are listed as the likely cause;
  sheets saved after the netlist was compiled are reported as `netlist_stale`, so a stale
  netlist is not mistaken for an out-of-date board. To feed it, the Protel netlist's part
  list (footprint, comment) is now read, the sheets give each part's unique IDs,
  footprint models, kind and pin designators, and the PcbDoc gives the full source unique
  ID, the component kind (Jumper included, stored as ComponentKindVersion3) and the
  comment text (from WideStrings6, so µ and Ω survive)
- `get_drc_report`: reads Altium's last Batch DRC report ('Design Rule Check - <PcbDoc>'
  .html or .drc in the project's output folder) and explains it. Violations are grouped by
  rule, component or net and linked to the board (components and nets through 'Pad
  <designator>-<pad>', the copper each violation touches on the layers it names, pours
  for shorts, differential pairs, and the board location), with PCB health issues, waived
  violations and the report's own warnings. An assessment comes first: the report is
  incomplete (an empty HTML gives way to the .drc of the same run), the DRC stopped at its
  violation limit (later rules, shorts included, read 0), Un-Routed Net or Short-Circuit
  was not checked, the report is older than the board or an Update PCB ran since, it was
  made for another board or found outside the output folder, or the rules changed since.
  The PcbDoc's Design Rule Checker settings (violation limit, batch and online rule kinds,
  report and health options) and the PrjPcb's output and log folders are now read. ERC,
  OutJob outputs and the DRC XML report are not read
- Bill of materials per variant:
  - `get_bom` lists lines (quantity, designators, Comment, description, value,
    footprint, manufacturer, manufacturer part number) built from the compiled parts, so
    compile-masked parts stay out, without Graphical, Net Tie and Standard (No BOM)
    parts and without the variant's Not Fitted parts, with its parameter variations
    applied (they follow their variation's part) and an alternate part's footprint used.
    Variants bind to parts by UniqueId as Altium does, so a stale designator in the
    project file does not pick the wrong part. The MPN comes from part parameters (the
    spellings libraries use for 'Manufacturer Part Number 1', paired with its
    manufacturer), never from Comment; 'N/A' and the like are shown as placeholders, not
    part numbers. Special strings such as '=Value' are resolved against the part's
    parameters, then the project's. Lines group by MPN, else Comment and footprint, and
    list the values a line's parts disagree on; variants can be compared and the lines
    returned as CSV (formula-like cells kept as text)
  - `check_bom` runs offline hygiene checks: duplicate designators, unresolved special
    strings, fitted parts marked DNP in their text, parts without a footprint, one MPN
    with different values/descriptions/footprints, MPNs without a manufacturer, parts
    without an MPN (where the project keeps MPNs in parameters), placeholder
    descriptions, stale variant entries, consolidation candidates among passives, and
    sheet parts outside the compiled design. ActiveBOM and supply data (lifecycle,
    stock, price) are not read
  - The sheets now keep every parameter and the library reference, and the project
    file's variants are read in full (alternate parts, parameter variations,
    CurrentVariant)
- `check_decoupling`: offline decoupling coverage. From the compiled netlist and pin names
  it finds the rails (nets named like power, or reaching an IC supply, reference or
  capacitor pin; never a net carrying EN, PG or SENSE), the power pins of ICs fitted in
  the active variant, and the fitted capacitors to ground (two-pin, arrays and
  feed-throughs); a 0 ohm link joins two rails. Per rail it reports no capacitor to
  ground, only Not Fitted ones, a rail joined through a ferrite or resistor to
  capacitors with none of its own (warnings), and only capacitors to other nets
  (bootstrap, another rail, a 0 ohm link), no capacitor under 1 µF, unreadable values
  and, when asked, no bulk capacitor (info). With the PcbDoc it lists each IC rail's
  nearest capacitor (and nearest under 1 µF) with its distance, straight line, pad edge
  to pad edge, warns above 5 mm, and reports power pads far from every capacitor and an
  opposite-side capacitor with no via or pour found at its pad; naming an IC lists its
  5 nearest capacitors. Rules and thresholds were calibrated on real boards (85% of the
  IC rails measured have a capacitor within 5 mm); regions and planes are not read, so
  it is advice, not DRC. Filters by IC and rail; any part can be checked as an IC
- Design notes: Claude now reads what designers wrote into the project besides the
  circuit. New tool `get_design_notes` (paginated, or one document by name) returns:
  - project parameters from the `.PrjPcb`
  - per schematic sheet: notes (author, collapsed state), text frames, free text labels,
    pictures (embedded or linked, size, position, nearby caption text), directives /
    parameter sets with their parameters, title-block parameters and the sheet template.
    Text drawn inside component symbols or the template is left out. Each note lists the
    components drawn next to it (`near R4, U1`) as a proximity hint, measured at
    Altium's real font size with rotated labels turned about their anchor
  - per Draftsman document (`.PCBDwf`, now read from the `.PrjPcb` document list):
    notes, free text, tables, view titles and sides (fabrication views name the layer
    they show), pictures, and the drill table and layer stack notes cached from the
    board (with several stacks, each note names its stack). A drawing last updated from a `.PcbDoc` that is not
    this project's is flagged as possibly out of date, or as a copied project or earlier
    revision when a board of the same name sits in another folder. Large drawings are
    parsed without the board copy they embed (~0.7 s for a 6.8 MB drawing)
  - text as the sheet shows it: Altium's escapes are decoded (`~1` a line break, `~2` a
    `|`, `~~` a `~`), and special strings are resolved: label and text-frame formulas
    (`=PrjMPN + '-v' + Version`) and title-block values that point at project
    parameters (`Revision = =SCH_Revision`) against the sheet's parameters, then the
    project's, then `ProjectName`/`DocumentName` (a `*` placeholder counts as not set;
    values Altium computes when printing, such as dates, stay as typed). In Draftsman
    notes, text, tables and view titles, `=Name` takes the value the drawing cached
    (e.g. `Final thickness of PCB: =Total_Thickness`) and `=ViewScale` the view's scale
    (e.g. `2:1`)
  - not included: review comments made in Altium 365, which live in the cloud workspace,
    not in the project files
- `get_sheet_context` shows the sheet's notes above its components (first page)
- New tool `get_design_image` returns a schematic or Draftsman picture as an image
  Claude can look at (schematic bitmaps are re-encoded as PNG, no new dependency).
  Pictures over 8000 px per side are refused (sizes are read from PNG, GIF, JPEG and
  WebP headers), and a linked TIFF or metafile is reported as an unsupported format
- `set_project_dir` reports `project_parameters` and `draftsman_documents`
- `package_for_xfn` snapshots include the design notes in a new `notes` table: project
  parameters; per sheet the notes, text frames, labels, pictures, directives, title-block
  parameters and template; per Draftsman document its source board, parameters, notes,
  text, tables, pictures, view titles, drill table and layer stack notes. One row per item
  with `document_kind`, `document`, `sheet_id`, `page`, `kind`, `title`, `text`, `author`,
  `near_refdes` (JSON) and kind-specific `data` (JSON). Pictures are stored as metadata
  only (name, size, position, index), never their image data. Paths are reduced to file
  names (template, linked pictures, the drawing's source board), so a shared snapshot
  carries no local folders or user names. A document whose notes cannot be read gets one
  `unreadable` row with the reason and a warning, instead of failing the export. The
  snapshot keeps `SCHEMA_VERSION` 1: the `notes` table is new and no existing table
  changed, so every released pcb-copilot (which accepts only schema 1 and reads only the
  tables it knows) still opens it
- `package_for_xfn` warns when the snapshot is missing components from failed sheets
- `set_project_dir` / `refresh_netlist` responses include `component_count`, and
  `refdes_without_metadata` (with a sample of up to 10) when netlist refdes are missing
  from every parsed sheet
- A designator that names two or more different components on one sheet is reported
  instead of only logged. Only the last component's metadata is loaded, so the others
  silently had none. `set_project_dir` and `refresh_netlist` add `designator_clashes`
  (sheet, refdes and each component's `lib_references`, the first 20),
  `designator_clash_count` and a hint, and a `refresh_netlist` cache hit names them; the
  server instructions tell Claude to pass them on. A compile-masked copy is not a clash
  (see Fixed). `parse_sch_doc` and `components_from_stream` take an optional `clashes`
  list to fill
- `get_board_info` adds `refdes_mapping_unmatched_sample` (up to 10 board designators
  with no netlist part) and `refdes_differs_from_logical` (how many parts the board
  renames)
- Altium Designer 17 and older, which run as `DXP.exe` (e.g. 14.3), are now detected
  alongside `X2.exe` (AD 18+). Their title bar (`Altium Designer (14.3) - <sheet path> -
  <project>.PrjPcb. Licensed to …`) gives the project, active sheet and project path. The
  project path is found in the sheet's folder or a parent folder
  ([ee-in-a-box/altium-copilot#5](https://github.com/ee-in-a-box/altium-copilot/issues/5)).
  The title layout has not been checked on a live DXP install yet

### Changed
- The session-start instructions read the design notes (`get_design_notes`) before
  summarising the board, and `schematic_review` treats notes as intent to verify. The
  server instructions fit within the 4096 characters that Claude Code shows; the
  design-notes caveats live in the tool descriptions
- CONTRIBUTING asks for a `SCHEMA_VERSION` bump only when an existing snapshot table or
  column changes or changes meaning; a new table needs none

### Fixed
- `requirements.txt` pins `mcp<2`: mcp 2.x removed `mcp.server.fastmcp`, so a fresh
  install failed to start the server. `httpx`, which the update check imports, is now a
  declared dependency instead of arriving through mcp
- Projects that keep their `.SchDoc` files in a subfolder (e.g. `source\`) or outside
  the project folder (`..\common\`) now load components. Sheets come from the `.PrjPcb`
  document list instead of a top-level `*.SchDoc` glob, for both enrichment and netlist
  freshness ([ee-in-a-box/altium-copilot#6](https://github.com/ee-in-a-box/altium-copilot/issues/6))
- Device sheets are read as project sheets. The `.PrjPcb` lists them under
  `[DeviceSheetN]` (a managed one as `Managed\Sheets\<GUID>\Name.SchDoc`), not
  `[DocumentN]`, so their parts got no schematic metadata, saving one did not make the
  netlist stale, and `get_sheet_context` and design notes did not know the sheet. On
  Altium's Bluetooth_Sentinel example (10 device sheets) 72 of 72 schematic parts now
  load (was 15). A listed device sheet missing on disk is reported in `sheet_errors`,
  like a missing project sheet; a sheet listed both ways is read once. Projects without
  device sheets are unchanged
- Schematic enrichment reads sheets that write record keys in upper case (`OWNERINDEX`,
  `TEXT`, `NAME`, `UNIQUEID`, ...), as most of Altium's AD25 examples do. `parse_sch_doc`
  matched only the mixed-case spelling, so Bluetooth Sentinel, Mini PC (Motherboard,
  WiFi) and SpiritLevel-SL1 loaded no components and `set_project_dir` reported
  `no_components_loaded`; PC4-SODIMM loaded 16 of 199. Keys and component parameter
  names (`VALUE`, `COMMENT`) are now matched case-insensitively, a record's trailing NUL
  no longer ends up in its last value, and a non-numeric `OwnerIndex` is skipped instead
  of failing the sheet. On the 84 example sheets 1537 of 1581 parts load (was 98); the
  other 44 are parts of multi-part components, merged by the next fix
- Parts of a multi-part component that each have their own UniqueId, as on most of
  Altium's AD25 examples, are merged by designator. `parse_sch_doc` merged parts only
  when they shared a UniqueId and kept one entry per designator, so the last part placed
  replaced the others: 44 parts of 37 components on the examples, 399 pin names among
  them. Parts with the same designator, different part ids and the same part count now
  merge (pins united, first non-empty description and value, best MPN), so all 1581
  example parts load. The component keeps the UniqueId of its highest part, the one the
  PcbDoc's `SOURCEUNIQUEID` names (29 of 29 on the examples; the last part placed
  matched 28). Parts placed from different library references are merged and logged.
  Two different components with one designator on a sheet are reported as a designator
  clash and, as before, the later one is kept
- Compile-masked parts are recognised (inside a sheet's compile mask, RECORD=211). A
  masked copy that shares a live part's designator, on the same sheet or another, no
  longer replaces the live part's parameters, description and kind, and is not reported
  as a designator clash; a live part replaces a masked one, and a variant entry on the
  masked copy no longer applies to it. A collapsed (disabled) mask masks nothing. On
  real projects the masked copy's data had replaced 25 live parts' (wrong MPN, Comment
  and description in their BOM lines), and every designator clash found involved a
  masked copy
- Schematic metadata (MPN, description, value, pin names, sheet) of board-annotated and
  multi-channel designs follows the project's `.Annotation` file. The netlist names
  physical designators and the sheets logical ones, so the netlist's U1 took the metadata
  of the sheet's U1, another part, and renamed parts got none. Each sheet part is now
  matched by sheet and UniqueId to its physical designators, one per channel; the same
  sheet designator on two sheets no longer drops one of the parts. On Altium's
  Bluetooth_Sentinel example with every sheet read, 72 of 72 parts carry their own
  metadata (before: 14, with 42 carrying another part's). Parts the file does not list
  keep their sheet designator unless an annotated part has that name. Projects without
  an `.Annotation` file are unchanged
  - `get_component`, `search_components`, `get_sheet_context`, `package_for_xfn` and
    stale-designator DNP resolution name board designators. `get_component` adds
    `logical_refdes` when the sheet's designator differs and accepts a sheet designator
    that no board part has; `get_sheet_context` shows it as `U3 (U1 on sheet)`
  - `set_project_dir` and `refresh_netlist` report `renamed_by_annotation`, and
    `annotation_errors` for a listed `.Annotation` file that cannot be read
  - Saving the `.Annotation` file makes the cached netlist stale
- A sheet part with no nets no longer takes, by channel-suffix matching, the netlist name
  of another part (TP1 claiming TP1V8): exact designators are matched first. Unconnected
  channel copies are now found through the compiled part list, so they get their
  sheet's metadata
- A sheet that fails to parse no longer discards every other sheet's components; the
  failure is returned as `sheet_errors` (sheet, path, error)
- Nets loaded with zero components now report `no_components_loaded` from
  `set_project_dir`, `refresh_netlist`, `search_components`, `get_component`,
  `get_sheet_context` and `package_for_xfn` instead of "Pattern matched all 0 components"
- Saving a sheet in a subfolder now makes `refresh_netlist` regenerate, and
  `set_project_dir` no longer serves a cached netlist older than the sheets or `.PrjPcb`
  (offline load returns `altium_not_running` with `netlist_stale: true`)
- A failed regeneration (e.g. Altium closed) restores the previous `.NET` instead of
  leaving the project without a netlist
- Protel netlist members whose pin designator holds a '-' (a battery's '-' pin, written
  `BT1--`) are split at the rightmost '-' that leaves a known part, so the pin no longer
  becomes a phantom part `BT1-` with an empty pin. The .NET is read as UTF-8, falling
  back to the Windows code page instead of replacing characters such as µ
- Netlist generation only activates a window owned by Altium. Before, the first visible
  window with "Altium Designer" in its title got the keystrokes, which could be a
  browser tab
- A variant's Not Fitted entry (Kind=1) that still carries an AlternatePart (Altium leaves
  some behind, e.g. 'NP') now makes the part DNP; it was read as fitted
- DNP flags (`get_component`, `list_variants`, sheet context, PCB tools, sync check,
  export) bind a variant's Not Fitted entries by UniqueId, then the stored designator,
  as Altium and `get_bom` do: a stale designator in the project file named the wrong part
- `refresh_netlist` re-reads the project's variants, so Not Fitted parts, parameter
  variations and variants saved since the project was loaded apply; the active variant
  stays by name, and the response says so when it was renamed or deleted
- Stale-designator DNP UniqueIds that could not be resolved at load are kept and
  resolved by the next `refresh_netlist`
- `get_net_neighbors` and `query_pcb_region` no longer measure polygon-pour outlines as
  copper. A pour's outline (the boundary drawn in `Polygons6`, kept as zero-width edges)
  was matched like a track: on Altium's PC4-SODIMM example DQ09 read GND at 1.13 mil
  with an 81.6 mil parallel run, where the nearest GND copper is 4.92 mil away, and 22 of
  the 25 neighbours listed for GND were at 0.0 mil (now 3.94 mil at the closest, against
  a 3.937 mil clearance rule). Copper distances come from tracks, arcs, vias and pads
  only; a hatched pour's tracks are real copper and still count. `get_net_neighbors`
  adds `pours`: other nets' pours on the net's layers, by outline, with `inside_pour`
  when the net's copper lies within the outline and `min_outline_distance_mil` when the
  outline comes within the search distance. The net's own outlines are not measured.
  `query_pcb_region` lists outlines only under `pours`. Solid pour copper is still not
  parsed. The clearance spot-check, DRC violation nets and `check_decoupling` already
  left outlines out
- PCB tools name components by the designator printed on the board (decoded from
  `Texts6`), not the PcbDoc's `SOURCEDESIGNATOR`, which is the schematic sheet's logical
  name and differs on multi-channel and board-annotated designs (57 of 73 parts on
  Altium's Bluetooth_Sentinel example, 8 of 199 on PC4-SODIMM). `get_component_placement`,
  `query_pcb_region` and the `get_net_pcb` pad list report it as `refdes`.
  `get_component_placement` adds `logical_refdes` and still accepts the logical name when
  no designator or netlist refdes matches, returning every channel instance. PCB-only
  parts (mounting holes, logos) now have a `refdes` instead of `null`
- `sch_refdes` (the netlist name) matches the PCB designator first, then the schematic
  UniqueId, then `SOURCEDESIGNATOR`, and a fallback never takes a netlist part that
  another board designator names. Before, a board-annotated board's U3 (sheet U1) was
  mapped to the netlist's U1, a different part: 64 wrong mappings on the AD25 examples.
  Netlist refdes without schematic metadata now match too
- Rounded-rectangle pads keep their corners in every PCB distance (clearance
  spot-check, `query_pcb_region`, `get_net_neighbors`, `check_decoupling`, DRC
  violation nets, pad-to-pad length). Altium stores them as round pads with the real
  shape and corner radius in the pad's size-and-shape subrecord; they were modelled as
  capsules, which cut their corners away. On Altium's MiniPC example U9's 3V3 pad is
  33.2 mil from C69, pad edge to pad edge (was 37.0), and on its SODIMM board a 1%
  corner pad that read 7.6 mil from a track is 3.97 mil from it (the clearance rule is
  3.937); on real boards pad gaps shrank by up to 14.6 mil. Corner radii were checked
  against the ROUNDEDRECT apertures of Altium's Gerber output. Pads with a
  top-middle-bottom or full stack on the bottom or an inner layer take that layer's
  size and shape, and an empty slot falls back to a filled one: three bottom-layer
  pads with an empty top slot on the Bluetooth Sentinel example were dropped

## [0.3.0] - 2026-07-19

### Added
- PCB understanding: parse the project's `.PcbDoc` directly (no export step) into a
  spatial index. New tools:
  - `get_board_info` — stackup (thickness, dielectric, material), origin, extents, counts
  - `get_net_pcb` — where a net routes: layers, lengths, vias, pad endpoints; exact net
    names always resolve to full detail, even when they also match other nets by pattern
  - `get_net_neighbors` — crosstalk proximity: nearby nets, edge-to-edge distance,
    parallel run length, broadside overlaps on adjacent copper layers
  - `query_pcb_region` — what's near a board coordinate, including polygon pour
    containment
  - `get_component_placement` — placement, pins, nearest components; resolves
    multi-channel refdes (`U3` vs `U3A`/`U3B`) via UniqueId + pad-net signature
  - All four net/region/placement tools annotate results with the active variant's
    DNP status; `get_net_neighbors`/`query_pcb_region` reject non-copper layer names
    (e.g. silkscreen) with the valid copper layer list
- PCB data auto-refreshes on `.PcbDoc` save (mtime-based, no manual refresh)
- `get_board_info` and polygon-returning tools note that pour outlines reflect the
  last-saved nominal geometry, not live copper — repour and save in Altium before
  relying on polygon results for voids, cutouts, or clearances

### Fixed
- `install.ps1` now verifies Claude Code MCP registration persisted and prints recovery
  steps when a running session clobbers it
- Inner copper layers (`Mid1`–`Mid30`) referenced by their Altium positional alias in
  polygon pour data now resolve correctly — previously these pours were dropped from
  crosstalk/proximity results entirely
- `page_netlist.py` high-fanout threshold now matches its documented "more than 25
  pins" contract (was flagging exactly-25-pin nets)

---

## [0.2.4] - 2026-06-09

### Fixed
- Variant DNP detection now correctly resolves components whose designator entry in the
  `.PrjPcb` variant table is stale (e.g. `R?` — set before annotation ran). The parser
  captures the stable `UniqueId` for any stale entry and defers resolution until after the
  schematics are parsed; the `UniqueId → refdes` map built from `.SchDoc` files is then
  applied to produce the correct DNP list. Previously, such components were silently omitted
  from the DNP set in all queries and exports.

---

## [0.2.3] - 2026-05-08

### Added
- `set_project_dir` now loads from disk cache when the netlist is unchanged — no Altium required.
  The `.NET` file mtime is stored in the project registry after every successful load or refresh;
  on subsequent calls the mtime is compared and, if it matches, the project loads instantly
  without touching Altium. Falls back to the full Altium flow when the cache is absent or stale.
- `schematic_review` prompt now suggests parallel agent dispatch for large multi-sheet projects,
  including the exact MCP init sequence each agent needs to self-initialize independently.

### Fixed
- `get_sheet_context` `has_more` flag now emits lowercase `true`/`false` instead of Python's
  `True`/`False` so it is consistent with JSON convention and easier to parse in agent prompts.
- Registry entries with a stale camelCase `lastUsed` key (written by older versions) are now
  silently cleaned up on the next load — no migration script required.
- `refresh_netlist` now updates the registry `netlist_mtime` after regeneration so the cache
  remains valid across sessions after a schematic save.

---

## [0.2.2] - 2026-05-07

### Changed
- `query_net` now accepts regex patterns for discovery (e.g. `UART` finds all UART nets) and
  falls back to exact case-insensitive match when the pattern is invalid regex or finds nothing —
  so PCB net names like `+5V`, `+3V3`, and `VIN[0]` resolve correctly without escaping
- `search_nets` removed — `query_net` covers all its use cases with the same or smaller token cost
- `get_sheet_context` output switched from JSON to compact text format (reduces token usage ~60%)
  with automatic pagination: if `has_more: True`, call again with `offset=<next_offset>`
- Added `maxResultSizeChars` MCP annotations to `query_net` (60K) and `get_sheet_context` (200K)

### Fixed
- `get_sheet_context` with a negative or out-of-bounds `offset` is now handled gracefully
  instead of silently returning wrong data or looping

---

## [0.2.1] - 2026-05-03

### Fixed
- Netlist generation now retries on focus acquisition timeout and mid-sequence focus loss (another window stealing focus) instead of surfacing an error immediately — up to 3 attempts before giving up
- PowerShell focus poll reduced from 10 s to 5 s so transient failures retry faster; worst-case total wait drops from 30 s to 15 s

---

## [0.2.0] - 2026-05-03

### Added
- `package_for_xfn` tool — exports the loaded project to a portable SQLite `.db` snapshot that cross-functional team members (firmware, mechanical, test, reliability) can query with [pcb-copilot](https://github.com/ee-in-a-box/pcb-copilot) without needing Altium
- SQLite export schema: `project`, `sheets`, `variants`, `components`, `nets`, `pins` tables with indexed lookups; `nets.pin_count` for fanout queries
- Registry now stamps `last_exported_xfn` timestamp on every successful `package_for_xfn` call
- `CONTRIBUTING.md`: DB schema versioning guidelines — bump `SCHEMA_VERSION` in `export.py` and `SUPPORTED_SCHEMA_VERSION` in pcb-copilot on any schema change

### Removed
- `--update` self-update command — the one-line `install.ps1` installer handles updates; in-chat update notifications are retained

### Fixed
- Registry writes are now atomic (write to `.tmp` then `os.replace`) — no corruption on crash or power loss
- Connection leak in SQLite export when an exception occurred mid-write; now always closes the connection in `finally`
- Swallowed `OSError` in export rollback path — exceptions now propagate correctly
- Duplicate `HIGH_FANOUT_THRESHOLD` definition removed (was defined in both `export.py` and `main.py`)

### Internal
- 330 lines of new test coverage: `test_export.py`, `test_package_for_xfn.py`, registry tests for `mark_xfn_exported`
- `.gitignore`: added `*-pcb-copilot.db` (binary export snapshots) and `.worktrees/`

---

## [0.1.10] - 2026-04-23

### Internal
- Release workflow now runs lint and tests before building — a failing test blocks the release

---

## [0.1.9] - 2026-04-23

### Fixed
- Netlist foreground acquisition now uses `AttachThreadInput` so `SetForegroundWindow` reliably succeeds even when Altium is behind another window or minimized; focus poll timeout extended to 10s (was 1.5s) — no keystrokes are sent if focus is never confirmed
- Settle time after focus increased to 2s; ESC sent before menu sequence to clear any residual state; inter-keystroke delay increased to 50ms

---

## [0.1.7] - 2026-04-23

### Fixed
- Subsheet fan-out suffix matching extended: numeric-only suffixes with a separator now matched, chained segments now matched and bare numeric suffixes without a separator correctly rejected (`C111`)

---

## [0.1.6] - 2026-04-22

### Fixed
- MCP server initialization timeout on cold starts: update check now runs on a background thread so `mcp.run()` starts immediately instead of blocking on a GitHub HTTP call
- Subsheet fan-out pin enrichment: refdes suffix matching now handles all separator styles (`_`, `.`, `-`, none), any case, multi-letter suffixes, and trailing digits

---

## [0.1.5] - 2026-04-22

### Fixed
- Netlist keystroke reliability on slow machines: increased inter-keystroke delay from 1ms to 10ms, added 1s settle wait after Altium gains focus before sending the first key (prevents dropped Alt+D on slower PCs)
- Added ESC after netlist generation (with 1s delay) to dismiss any lingering Altium dialog
- Clear error when a non-schematic tab is active (homepage, Extensions and Updates, etc.) — instead of timing out with a confusing "check Altium is responsive" message, Claude now tells you to click on a schematic sheet tab

### Removed
- `save_schematic_review` tool — unnecessary overhead; ask Claude to write the review directly if you want it saved

### Internal
- Scoped pytest to `tests/` directory to prevent vendor directory crash on collection

---

## [0.1.4] - 2026-04-20

### Changed
- `brainstorm_circuits` trigger broadened — now fires on any question about topology, architecture, or how to design/improve a sub-circuit, not just when the user says "brainstorm"
- `brainstorm_circuits` docstring updated to match

---

## [0.1.3] - 2026-04-20

### Changed
- `get_sheet_context` is now the primary tool for all circuit questions, not just design reviews — server instructions guide Claude to start every question with a single bulk sheet load before falling back to `query_net` or `get_component`
- Cross-sheet tracing now uses `get_sheet_context(sheet_name=X)` instead of calling `get_component` one-by-one for each cross-sheet neighbor — significantly faster on dense, multi-sheet designs
- `get_sheet_context` docstring updated to make the cross-sheet and general Q&A use cases explicit

---

## [0.1.2] - 2026-04-20

### Changed
- Added `title` and `readOnlyHint`/`destructiveHint` annotations to all 13 tools — Claude Desktop now surfaces correct read/write intent for each tool call
- Schematic review prompt refactored to a template string — same review behavior, cleaner internals
- `manifest.json`: richer description, author GitHub URL, repository metadata, otter icon

### Added
- `assets/icon.png` — otter icon displayed in Claude Desktop Extensions UI when installed via `.mcpb`

---

## [0.1.1] - 2026-04-20

### Changed
- Removed `.mcpb` packaging — installer downloads the `.exe` directly, no bundle needed
- Simplified release workflow: dropped Node.js dependency and checksums generation

## [0.1.0] - 2026-04-19

### Added
- Initial release
- `detect_altium_project` — detects your running Altium instance and surfaces known projects
- `set_project_dir` — loads a project and builds the netlist context Claude works from
- `list_variants` / `set_active_variant` — switch between project variants; DNP components are excluded automatically
- `get_sheet_context` — reads the active schematic sheet and summarizes components on it
- `get_component` — pulls a component's description, parameters, and all connected nets by refdes
- `query_net` — traces a net across all sheets and returns every pin connected to it
- `search_components` — finds components by refdes, MPN, or description using regex
- `search_nets` — finds nets by keyword without needing exact names
- `refresh_netlist` — reloads the netlist after saving changes in Altium
- `schematic_review` — structured 3-phase design review: reads datasheets, audits against them, saves findings to a markdown report
- `brainstorm_circuits` — 5-phase design session: reads your loaded schematic, asks constraints, proposes topologies with tradeoffs
- Auto-update: server checks for new releases on startup and notifies you in chat
- One-line installer via PowerShell (`irm | iex`) — no Python required
- PyInstaller build — ships as a standalone `.exe`, no Python required
