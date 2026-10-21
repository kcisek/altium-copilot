# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)

## [Unreleased]

### Added
- `check_dfm`: a design-for-manufacture check of the saved PcbDoc against a
  fabricator's capability limits. The limits come from a profile: Altimade's published
  limits (Altium's ordering service, page updated 2026-10-13) built in and used when
  nothing else is chosen, said so in the result's `scope`; a `fab_profile.json` in the
  project folder or another `<name>.json` there named by `profile` (template:
  `examples/fab_profile.json`, a blank to fill in with your fab's numbers); or
  `profile="none"` with only the `limits` argument. The file, the argument and the
  echoed profile share one vocabulary: lengths end in `_mm` or `_mil`; via, PTH and
  NPTH drills, rings and hole-to-hole distances fall back to a generic limit, a hole
  wall to copper (`min_hole_to_copper`) to `min_spacing` and a hole to the board edge
  (`min_hole_to_edge`) to `min_copper_to_edge`; null removes one. Every value is checked
  by the server, from a file or the argument: numbers only (true/false and text are
  refused, not read as numbers), and a length outside what any fab offers (a spacing
  above 1 mm or below 0.5 mil, say) is refused, with the other unit suggested only when
  that reading is plausible. A bad file or limit answers `invalid_profile` or
  `invalid_limits` with what to fix and never switches to another profile; an unknown
  profile lists the built-ins and the files found.
  Checks: trace width; copper spacing between nets on each signal layer (pads per layer,
  vias per layer with removed pads read as the barrel, poured copper, regions and fills;
  net ties, no-net copper and shorts an enabled ShortCircuit rule allows are left out,
  pad gaps inside one footprint are their own class), counted in places where two nets'
  copper comes too close and grouped into routed and area copper; hole walls (non-plated
  holes, plated holes without a pad) to copper; hole sizes per drill row, bare holes and
  routed slots included; annular rings of vias and plated pads per layer; aspect ratios;
  hole to hole, slots as capsules, with overlapping and coincident holes reported
  whatever the profile; copper and holes to the board outline and cutouts, with
  edge-connector fingers inside the clearance as a warning (they need the fab's
  edge-connector option) and edge-plated holes and copper off the board as info; hole
  walls and holes near the edge judged against the `min_spacing` or
  `min_copper_to_edge` stand-in are warnings unless the drill cuts the copper or the
  edge; layer
  count; copper weight (`copper_weight`); board size; drill count; and board rules looser
  than the profile, which Altium's DRC would let through (`rules_vs_profile`). Each check
  gives its status, severity, limit and worst value: fail for a miss of severity error
  or warning, marginal, advisory when its only misses are info (Altimade's service-only
  limits for layer counts, board size and copper weight while Altimade is only the
  default, and the drill count), pass, or not_checked with the reason. Findings are
  classes (a width, a family of gaps, a drill row or via type) with counts, the worst
  value and its range, and one example per place; a miss within 2% of a limit (at most
  1 mil for a length) is a warning, and mm/mil rounding is named only where it explains
  the miss. `board_minimums` says what the board needs from any fabricator, whatever the
  profile, and `drills` is the drill table. Solder mask, silkscreen, acute angles, copper
  text, plane pullback and panels are listed as not checked. The review is
  computed once per board, profile and units, then paged to fit the result size.
  On Altium's examples MiniPC's drill table matches its Draftsman drill table (back
  drills aside, all 16 bare holes included), and the review takes about 2.2 s after the
  2.5 s index (a repeat call or another page 1 ms); its fabrication checks give 18
  findings (6 of them spacing). On a private set of 55 real boards it ran without an
  exception, at most 0.22 s per board after the index (median 0.05 s), with every page
  under the 60,000-character limit
- `check_dfm` checks assembly (DFA) too, for the active variant (its Not Fitted parts
  are left out): `part_counts` (placements and unique parts against `max_placements`
  and `max_unique_parts`, Altimade's published 600 and 200, which are advice while
  Altimade is only the default; unique parts are the BOM's lines with a netlist, else
  distinct footprint and Comment, said to be approximate), `assembly_sides` (SMD parts
  on both sides and through-hole parts on the bottom: extra process steps, info),
  `part_spacing` (same-side neighbours by the nearest of their 3D body outlines and pad
  copper, the pads' extent for a part without a body, each part's nearest fitted
  neighbour, against `min_part_spacing`; overlapping parts are a warning, or advice
  without the limit; a part the assembler does not place, such as a fan model or a
  mounting hole, over others is its own info class), `part_to_edge` (pad copper or body
  to the outline and cutouts against `min_part_to_edge`; pads crossing the edge, bodies
  past it and card-edge connectors are their own info classes, parts off the board a
  note), `fiducials` (global fiducials per assembled side and two near each fine-pitch
  part, against `min_global_fiducials` and `local_fiducial_pitch`, else the typical 3
  and 0.5 mm as info), `tombstoning` (small passives with one pad connected directly to
  a pour, region or fill and the other not are a warning; with no area copper at either
  pad, one pad on a much wider track is an imbalance to review) and `fine_pitch`
  (against `min_pitch`, the pitch measured between pins: a pin drawn as several pads is
  one; Altimade publishes no number, so parts at 0.5 mm or finer are advice). The new
  profile keys are in the template; an assembly limit that is not set is measured, not
  judged. `board_minimums` adds placements per side, unique parts, fiducials per side,
  the finest pitch and the smallest part gap and part-to-edge distance; courtyards,
  component heights, pick-and-place, paste and through-hole pins against the other
  side's parts are listed as not checked. On MiniPC the whole review takes
  about 2.5 s after the index. On a private set of 55 real boards it ran without an
  exception, at most 0.3 s per board after the index, every page under the limit; 7
  boards have a small passive with one pad directly connected, 16 have overlapping
  bodies and 3 have fiducials
- `check_dfm` checks the pick-and-place data (`pick_and_place`, no limit needed). It
  computes each part's Center-X/Y the way Altium's pick-and-place file does: the
  midpoint of the box of its pad centres over every pad (SMD and through-hole, any
  layer; pad size plays no part), counted from the board's relative origin. On a private
  set, 40 of Altium's own files (for 25 boards) agree on 2571 of 2571 rows of unchanged
  parts within 0.0001 mm (the copper extent's centre would miss 304 of them by up to
  1.4 mm), and their 12 rows for parts without pads sit at the absolute origin (2 of
  them on boards whose relative origin is set). Findings: fitted parts without pads,
  whose Center-X/Y Altium writes at the absolute origin (a warning for a part in the BOM
  or a fiducial, info for Mechanical, Graphical and No-BOM parts); asymmetric parts
  whose Center-X/Y is 0.5 mm (SMD or mixed) or 2 mm (through-hole) from their 3D body's
  centre (the pad copper's without a body), such as edge connectors, with the fix
  (Ref-X/Y with the reference point at the body centre, or the assembler's correction);
  a relative origin that is not set, outside the outline or more than 0.1 mm from its
  lower-left corner (info, with Gerber Setup's 'Reference to relative origin' for
  Gerbers to line up with the pick-and-place file and ODB++); and, not a miss, parts
  whose Ref-X/Y is more than
  0.5 mm from Center-X/Y (mostly through-hole parts with a pin-1 origin). Cases Altium's
  files did not settle (bare-hole pads, pads on both sides, rotations off the 90-degree
  grid) are counted per board where they move a centre. Rotations against the
  assembler's zero orientation stay listed as not checked. On the private set of 83
  boards it ran without an exception in about 0.05 s per board at most after the index:
  82 boards are advisory and none fails; 53 have an asymmetric SMD part, 39 an origin
  away from the lower-left corner (24 never set), 13 have No-BOM parts without pads
- `pcb_manufacturing_review`: a guided manufacturing-readiness review of the saved PCB.
  Claude asks which fabricator and assembler (their numbers or a `fab_profile.json`),
  confirms the profile, checks fabrication (`check_dfm`, vias, health, connectivity,
  Altium's DRC report with its assessment) then assembly (`check_dfm`'s DFA checks, the
  BOM), and ends in a go/no-go that lists what was not checked; it never presents
  Altimade's limits as another fab's
- The PcbDoc parser reads the parts' 3D bodies (outline, overall and standoff height,
  side and model type; never the model's name)
- The PcbDoc parser reads whether a pad's hole is plated, a square or slot hole's
  angle, whether the hole is offset from the pad, pads that are bare holes (no copper,
  such as mounting holes; MiniPC has 16 that Altium's drill table lists) and the number
  of embedded board arrays (panels)
- `check_pcb_health`: Altium's PCB Health Check Monitor run offline on the saved PcbDoc,
  with Altium's category names, ids and levels (High error, Medium warning, Low info) so
  a saved Batch DRC report's health section compares one to one (`altium_report`).
  Checks: Shelved/Modified Polygons (shelved, or COPPERINVALIDATE), Duplicate Component
  Designators, Zero Area Polygons and Regions, Self-Intersecting Regions, Micro-Segments
  (Board, Copper; Graphics/Mechanical on request, as Altium runs it only in debug mode):
  region edges under 0.03 mil, Slots Hole Size > Length, Broken and Unused xSignals,
  360deg Component, Reference Point Outside the Component Area, Components on
  Non-signal Layers, Invalid PCB Design Rules (empty or repeated names); plus parts
  outside the board shape, duplicate copper tracks and vias, and board-outline
  micro-segments. Checks turned off in the board's saved Health Check settings are
  skipped; mirrored footprints, panels, fonts and From-Tos are listed as not checked.
  Counts follow Altium's report: one row per object, per short edge for micro-segments
  and per crossing point for self-intersecting regions.
  On real boards it matched every saved report's health section (reference points
  outside the component on 11 of 11 boards). The PcbDoc parser now also reads region
  shapes (ShapeBasedRegions6, Regions6), fills, board regions, pour and outline arcs,
  COPPERINVALIDATE, the component's layer, pad hole shapes and slot lengths, and the
  board's Health Check settings
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
- `check_vias`: via inventory and fabrication review. Via types by span (first and last
  layer in stack order: through, blind, buried, microvia per IPC-2226A: one dielectric,
  at most 0.25 mm deep, and 1:1 or a hole up to 0.15 mm), hole as drawn and pad
  diameter, with counts per type and per net class, copper-to-copper depth and aspect
  ratio from the stack heights, the smallest annular ring over the layers that keep a
  pad, tenting, vias in SMD pads,
  stacked microvias and the stack's drill pairs. Back drilling is recomputed the way
  Altium writes the drill files: the Layer Stack Manager's back drills (drilled from the
  outer layer, stopping before the other one) apply to vias in a Max Via Stub Length
  (Back Drilling) rule's scope that have no connection on the layers they remove; each
  back drill lists its drilled layers, depth to the stop layer and drill sizes (hole
  plus the oversize). Findings: aspect ratio above 10:1 (info above 8:1, and for a
  mechanically drilled blind via above 1:1; microvias above 1:1, info above 0.8:1),
  annular ring below the Minimum Annular Ring rule or the requested minimum (info below
  0.1 mm, 0.05 mm on microvias), sizes outside the Routing Via Style rule (info for
  holes of 1 mm and up drawn as vias), blind or buried spans without a drill pair, vias
  in SMD pads (fill and cap advice, one note per thermal via array; warning for another
  net's pad or a ViasUnderSMD rule forbidding it), untented or one-side tented vias and
  tented holes above 0.5 mm, stubs above a back-drill rule's maximum (the residual after
  back drilling is taken as the worst case, the drill tip stopping just past the first
  drilled layer), back-drill rules without back drills, and stubs over 40 mil with their
  quarter-wave resonance on differential pairs no back-drill rule covers. On Altium's
  MiniPC example the recomputed back drill selects the same 16 vias whose pads Altium
  removed on the drilled layers
- `check_connectivity`: copper connectivity of the saved board, like Altium's Un-Routed
  Net and Net Antennae rules, offline. Per net, whether its copper joins all its pads:
  tracks, true arcs, vias, pads and any same-net copper that touches, a hatched pour's
  tracks and arcs, the regions a solid pour was last poured into (thermal spokes
  included), copper regions, fills, and internal planes assigned to the net (split
  planes by their outline, Power Plane Connect No Connect honoured; plane connections
  are labelled assumed). A Jumper component's pads on one net count as joined, as in
  Altium. The summary counts nets routed, partly routed and unrouted with percentages
  and connections complete (Altium's routing completion formula, N pads needing N - 1);
  single-pad nets and nets without pads are counted apart. Findings:
  - `unrouted`: each missing connection between the nearest pads of two copper
    islands with its distance, the islands' closest copper approach when within
    25 mil (`copper_gap_mil`, also listed as `near_misses` beyond the spanning tree),
    `same_component` for two pads of one part, and the net's shelved pours and pours
    with no saved copper
  - `isolated_copper`: copper of a net that reaches none of its pads; dead pour islands
    as info
  - `antenna`: an open-ended track or arc (once per primitive, also in isolated
    copper), or one ending on a via that connects nothing else, with the stub length
    back to the first branch and the open track's own length against the Net Antennae
    rule that applies, when one is enabled
  - `weak_connection`: copper that only touches where a net needs it, which Altium's
    'Check for incomplete connections' rejects (centrelines that do not meet, a track
    centreline off the pad, via or pour copper, a via centre off the pad), listed when
    `checks` names `weak` or the board's Un-Routed Net rule has that option on (see
    Changed; the summary always counts them); `nets_joined_only_by_weak_connections`
    would read un-routed with that option on
  Nets connected only through assumed copper are listed in `assumed_copper_nets`.
  Filters by net or pair, net class, layer and check; paginated, with long connection
  lists shortened to fit. `altium_drc` sets Altium's last Batch DRC report beside it:
  its Un-Routed Net rows (missing connections apart from dead copper and plane rows)
  and Net Antennae counts, the nets each side lists as not routed, the board's Net
  Antennae rules, and the issues that limit trust in the report. On Altium's 6 examples
  every net is routed (without regions and fills 2,572 connections would read
  missing). On the 18 of 83 real boards whose own DRC report sits beside them, none
  reports an un-routed net and neither does this check (so un-routed violations
  themselves are not yet compared on real data); the 2 Net Antennae violations on one
  board are the 2 antennae found here, inside the report's violation boxes, and vias
  that connect on one layer only are not reported, as those reports do not
- The PcbDoc parser reads `Regions6` (copper regions on signal layers: a solid pour's
  poured copper, free and footprint regions, with holes; keepouts, polygon cutouts and
  board cutouts left out) and `Fills6` (copper fills on signal layers) into
  `PcbData.areas`; regions and fills on internal plane layers are voids in the plane and
  are not read as copper. A stream with records that cannot be read (a record past the
  end, vertices that are not board coordinates) warns and does not count as read. Also
  read: the internal plane net assignments (Board6 `PLANEnNETNAME`) and each pour's
  `POLYGONTYPE` (split planes). A shelved pour's regions are dropped like its tracks.
  A pour's layer is now resolved from Altium's own tokens (`TOP`, `MIDn`, `BOTTOM`,
  `PLANEn`, so split planes land on their plane layer) before the board's layer names,
  which can no longer shadow them. Other tools do not use the regions yet
- The PcbDoc parser reads each via's tenting (with a manual solder mask expansion that
  covers the ring counted as tented), test-point flags, pad stack mode, per-layer pad
  diameters and removed pads, and the stack's drill pairs and back drills (Board6
  LAYERPAIRn)
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
- Netlist-free load: with Altium Designer closed and no up-to-date cached netlist (e.g. a
  fresh copy of an Altium 365 project, whose `.gitignore` leaves out `Project Outputs*/`),
  `set_project_dir` now loads the project from its files instead of answering
  `altium_not_running`. The response says `partial: true`, `load_mode: netlist_free`,
  why, and lists `netlist_required_tools`. Board, rule, length, differential-pair, via,
  DRC report, design-notes, picture and variant tools work, as does `constraints_review`
  (its prompt says how to find the interfaces without schematic parts). `query_net`,
  `get_component`, `search_components`, `get_sheet_context`, `get_bom`, `check_bom`,
  `check_decoupling`, `check_sch_pcb_sync`, `schematic_review` and `package_for_xfn`
  answer `netlist_required`: open the project in Altium once, then load it again. PCB
  parts are not linked to schematic parts (`sch_refdes` is empty, `get_board_info` says
  so instead of counting every part as unmatched), and DNP comes from the designators
  stored in the variants. A cached netlist older than the sheets is not used
  (`netlist_stale`). With Altium closed, a `.NET` that is newer than every source but
  not the one the registry recorded (Altium regenerated it on its own, the folder moved,
  a new registry) now loads fully instead of partially, and is recorded; with several
  `.NET` files under the folder only the one in the project's output folder is trusted.
  After a partial
  load, `refresh_netlist` loads the project again, fully once Altium has it open; it
  keeps the active variant (or says it is gone in `variant_note`), and when the full
  load fails (e.g. Altium is busy) the partial load is restored (`partial_load_kept`)
- Altium 365 clone status, offline. When the project folder is in a Git clone whose
  origin is `https://<workspace>.365.altium.com/git/<project GUID>.git`, every
  `set_project_dir` response carries an `altium365` block, and the new read-only tool
  `get_clone_status` reads it again: workspace and project GUID, the last fetch
  (`FETCH_HEAD` time, else the remote-tracking branch's reflog), commits ahead of and
  behind the remote-tracking branch as of that fetch, uncommitted changes by kind with a
  sample (design files first; files in new folders are listed one by one, so a new
  folder of sheets counts) and the last commit's date and message. Warnings: last
  fetch older than 30 days or unknown, behind, ahead, no remote branch to compare with,
  and uncommitted changes to the design files the tools read (`.PrjPcb`, `.SchDoc`,
  `.PcbDoc`, `.PCBDwf`, `.Annotation`); other local files such as `.PrjPcbStructure` are
  counted without a warning. Nothing is fetched and no credential is used: every `git`
  call runs with `--no-optional-locks` (the index is not rewritten),
  `protocol.allow=never` (no transport, so no lazy fetch either) and no prompts, and
  starts nothing the configuration names: `core.fsmonitor` off, filter drivers blanked
  for `git status` (a clean filter such as git-lfs could otherwise run and write to
  `.git`; passed as `GIT_CONFIG_KEY_n`, so a driver name with `=` is blanked too),
  submodules ignored, no signature checks in `git log`. A filtered file that only looks
  changed (its stat data moved, so its raw content is compared with the stored pointer)
  is counted as `unverified`, not as a change. A git that hangs past
  10 s is stopped with everything it started. A missing `git` or a git error is
  reported, never fails a load; whether the folder is an Altium 365 clone then comes
  from `.git/config`, so other repositories get no `altium365` block. Credentials in the
  remote URL are never echoed. On 55 local Altium 365 clones it
  recognises all 55 and warns about 50 fetched more than 30 days ago and 2 with an
  uncommitted `.PcbDoc`, in about 0.3 s per clone

### Changed
- `check_vias` is described as the via inventory and drill review, no longer a "fab
  review", in its description, the server instructions and the README; questions about
  a fabricator's capability ("can my fab make this?") go to `check_dfm`
- The session-start instructions read the design notes (`get_design_notes`) before
  summarising the board, and `schematic_review` treats notes as intent to verify. The
  server instructions fit within the 4096 characters that Claude Code shows; the
  design-notes caveats live in the tool descriptions
- CONTRIBUTING asks for a `SCHEMA_VERSION` bump only when an existing snapshot table or
  column changes or changes meaning; a new table needs none
- `get_design_rules` and `get_rules_for_net` name the values of Back Drilling rules
  (max_stub_length_mil, oversize_mil, tolerances, use_top_back_drill,
  use_bottom_back_drill), ViasUnderSMD (allowed), LayerPairs (enforce) and the stacked
  microvia flag of Hole To Hole Clearance (allow_stacked_microvias); they were under
  other / other_mil. Only Back Drilling is among get_rules_for_net's default kinds
- `check_rule_compliance` checks a via's Minimum Annular Ring on each signal layer of its
  span that keeps a pad, like `check_vias`: a top-middle-bottom or full-stack via whose
  pad is smaller on some layer than its nominal diameter is measured there, and the
  violation names that `layer` and its `pad_diameter_mil`. Before, the ring came from
  the nominal diameter, so the two tools could disagree. Simple-stack vias keep their
  ring; no ring changes on 83 real boards (all simple stack) or on Altium's examples
  (MiniPC's 34 full-stack vias have nominal-size pads where they keep one)
- `check_connectivity`'s `weak_connection` also tests a pad or via that touches a
  pour's copper (solid or hatched, or the outline standing in for unread copper), as
  Altium's 'Check for incomplete connections' does: when it is connected directly
  (Polygon Connect Style), its centre must be inside the pour's outline, arc edges
  included, within 0.5 mil. Relief connections are not tested, as in Altium's help,
  and neither is a pad or via whose style is not known. The style is that of the first
  enabled rule by priority whose first scope matches the pad or via and second the
  pour. Layer, net and IsPolygon terms are decided on the pour, a room or region term
  only when the pour lies clear of it, and its name and class are not read. Altium's
  help does not say whether the scopes may also match the other way
  round, so every rule that may come first counts, and the pad or via is tested only
  when they all say Direct. On Altium's 6 examples, 12 of 28,988 pad and via contacts
  with solid pour copper have their centre off the outline: 7 direct vias and 5
  relief-connected edge-connector fingers (not tested). One Mini PC via is a connection
  its net needs, so it is reported. On 83 real boards, 81 of 35,429 contacts are off
  the outline (78 direct). 24 SMD pads on 4 boards are needed connections, each with
  its centre 5 to 100 mil past the outline and at most half of the pad inside it. The
  test was left out when `check_connectivity` was added, because pour outlines then
  ignored their arcs
- `check_connectivity` reads the Un-Routed Net rule's 'Check for incomplete
  connections' option (saved as `CHECKBADCONNECTIONS=TRUE`). `rules.UnRoutedNet` gives
  each rule's `check_incomplete_connections`: true, false, or null when the rule does
  not save it, which means off. Altium's help gives no default, and boards save this
  key only when it is on, while other rule options are saved as FALSE too. Per net,
  the option is that of the Un-Routed Net rule that applies: the first enabled one by
  priority whose scope matches the net. When a scope is not decided for a net, every
  rule that may come first counts, and the option is null when they differ. Where it
  is or may be on, `weak_connection` findings are listed by default. Where it is on,
  they are warnings (`incomplete_connections_checked` on each finding), and the nets
  routed here only through weak connections read un-routed in Altium. Those nets are
  listed in `nets_unrouted_in_altium` and flagged `unrouted_in_altium` in `nets`. The
  summary gives `incomplete_connections_checked`,
  `nets_routed_here_unrouted_in_altium` and `incomplete_connections_note`.
  `altium_drc` counts those nets as not routed here, and the connections they join
  only through weak connections as missing
  (`nets_not_routed_here_only_by_weak_connections`,
  `missing_connections_here_only_by_weak_connections`). Net status and routing
  counts still follow touching copper. `pcb_manufacturing_review` treats a net in
  `nets_unrouted_in_altium` as a no-go, like an unrouted net. Altium's 6 examples do
  not save the option, so nothing changes there. On 83 real boards, 4 of 93 Un-Routed
  Net rules have it on, all enabled with scope All, one per board. 3 of those boards
  have no weak connection. The fourth has 17 (9 track to track, 7 track to pad, 1
  track to via; 7 miss by 0.5 to 1 mil, where Altium's undocumented tolerance
  decides). Altium would read 15 connections on 10 nets routed here as missing because
  of them. None of the 4 has a DRC report to compare

### Fixed
- Layer-specific keepout tracks and arcs on signal layers (Tracks6/Arcs6 flags2 = 2)
  were read as no-net copper: `get_board_info` counted them as tracks and arcs, and
  `check_rule_compliance` listed them among the no-net copper it skips. They are now
  skipped like keepout regions and fills; objects on the Keep-Out layer are unchanged
- `check_vias` sizes a back drill as the via hole plus the Max Via Stub Length (Back
  Drilling) rule's oversize, matching the drill sizes Altium wrote for the Mini PC
  example. It added the oversize twice, following Altium's help, which calls the
  oversize radial and gives hole + 2 x oversize. On Mini PC (0.2 mm holes, 0.3 mm
  oversize, saved by Altium Designer 22) the drill table cached in MiniPC_Drill.PCBDwf
  lists the 16 back drills at 0.5 mm, and the board's own hole list (the Board6
  `HASHKEY` entry marked `[Backdrill]`) holds the same 0.5 mm; `check_vias` gave
  0.8 mm. `drill_diameters_mil` now matches them
- Pour outlines follow their arc edges (Polygons6 `KINDn=1` with `CXn`, `CYn`, `Rn`,
  `SAn`, `EAn`). They were read as straight lines between the vertices, so a point
  near a rounded pour edge was outside the pour. Each arc now becomes chords at most
  0.25 mil inside it, as arc copper does, run counter-clockwise from start to end angle
  and drawn from the end at its vertex, as KiCad's Altium importer does (which also
  draws an arc of under 0.2 degrees straight). This applies to the pour outline and
  its zero-width outline rows, so containment and outline distance follow the arcs
  in `get_net_neighbors` (pours), `query_pcb_region`, `check_vias` (a pour around a
  via), `get_drc_report` (a violation's polygon net), `check_decoupling` (an
  opposite-side capacitor pad in a pour) and `check_connectivity` (pour outlines
  standing in for unread copper, split planes). A pour's `bbox_mil` in `get_net_pcb`
  and where `check_pcb_health` places a pour finding include the arcs too. Bluetooth
  Sentinel's round GND pours have 3 to 5 vertices and arcs of up to 315 degrees.
  Read straight, 13,468 of their 20,340 poured track and arc ends lay more than
  0.3 mil outside them; now none do. On 83 real boards, 116 pours have arc edges
- The board outline's points follow its arc edges (Board6 `KINDn=1`) too: they were
  its vertices only, so an arc bulging past them was cut off. `get_board_info` gave
  Altium's Bluetooth Sentinel example `board_extents_mil` of 3149.6 x 196.9 mil, the
  strip its vertices span; with its two 315-degree arcs the board is 3149.6 x 2165.4 mil.
  The arcs become chords at most 0.25 mil inside them, as pour outlines do
  (`check_dfm` and `check_pcb_health` already read the arcs themselves). `get_drc_report`
  measures a violation's zoom box against the outline with its arcs: adding the copper's
  extents, as it did, missed an arc reaching past the copper. On Altium's other examples
  the extents do not change; on 83 real boards, 51 have outline arcs and one board's
  extents change. An outline or pour arc whose angles are finite but too large to subtract
  (a corrupt record) is a straight edge, as other bad arc values are; in a pour it raised
  an error that dropped every pour ("Polygons6 parse failed")
- `check_pcb_health` read a Regions6 or BoardRegions record whose vertices are not
  finite coordinates, or whose hole list runs past the record, as a region. It now
  counts it as unread, as `check_connectivity` already did: both decode Regions6 and
  Fills6 with one record reader. No region changes on Altium's examples or 83 real boards
- `get_drc_report`: a report where several tables list a different number of rows
  than their counts ("3 tables list ...") was called incomplete instead of having counts
  that differ
- The length engine (`get_signal_length`, `check_lengths`, `list_diff_pairs`) grew a
  rectangular pad by a track's half width or a via's radius with square corners, so
  copper just off a pad's corner (up to 0.41 times that reach) counted as touching it.
  The corners are now round. On Altium's examples 2 of 1,276 net lengths change and on
  83 real boards 1 of 5,729, each by under 1 mil
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
- Through-hole pads with a top-middle-bottom or full pad stack keep their size and
  shape per layer in every PCB distance (clearance spot-check, `query_pcb_region`,
  `get_net_neighbors`, DRC violation nets, pad-to-pad length): two objects are measured
  on the layers they share, each pad with its copper there. They had the top-layer size
  on every layer. On 83 real boards (aggregate) 3,255 through-hole pads have other
  copper on the inner layers: 21 gaps to inner-layer tracks grew (by up to 12.4 mil),
  450 pad pairs with larger inner copper came within 20 mil (the closest 9.8 mil), and
  spot checks of 395 nets lost 6 THPad-Track clearance violations that only the
  top-layer size caused, with none new; the length of the 1,447 nets with such pads is
  unchanged. `query_pcb_region` lists only the layers a pad's copper reaches, and a
  clearance violation names the layer its gap is on. Altium's examples have no such
  pads and measure the same
- Stack layers get their layer ids from the stack's LAYERID, not from the layer name.
  Boards keep stale names in old layer slots (e.g. internal planes once named like a
  signal layer), and the name lookup put those signal layers on the plane ids: on
  Altium's MiniPC example 8 of the 16 copper layers, so their tracks fell outside the
  stack for lengths, barrels and rule scopes
- Shelved polygon pours are read (`SHELVED` in `Polygons6`). Altium hides a shelved pour
  from DRC and connectivity, so it is no longer treated as copper: `get_net_neighbors`,
  `query_pcb_region`, `check_decoupling` ("a pour around it"), `check_vias` (a same-net
  pour around a via connects it on that layer), DRC violation nets, rule scopes on a
  net's pour copper and the length tools' "pours may complete the connection" note leave
  it out, and its outline gets no proximity rows. Copper that
  Altium keeps in the file for a shelved pour is dropped
  (`skipped_records.shelved_pour_copper`). `get_net_pcb` still lists the pour with
  `shelved: true`, and `get_board_info` counts `shelved_pours`. On 2 of 83 real boards
  every pour is shelved (12 in all); for sampled nets there, `get_net_neighbors` listed
  292 pour entries, all from pours with no copper
- A hatched or outline-only pour's tracks and arcs are read as the pour's copper: the
  `Tracks6` / `Arcs6` polygon index (KiCad's `polygon` field; 0xFFFE marks board-outline
  primitives, not a pour). Altium saves them without a net, so they were netless routing:
  invisible to the distance tools and counted as unchecked no-net copper. They now take
  the pour's net. `get_net_neighbors` and `query_pcb_region` measure them (counted in
  `pour_copper_segments` with no parallel run, kind `pour`). The clearance spot-check
  matches them as the matrix's `Poly` object, and Width checks them, with `pour_copper`
  on violations: a Width rule on Altium's SL1 example is scoped `Not InPolygon`, though
  Altium's docs do not say. They are not routing, so routed length, the `get_net_pcb`
  layers, the length graph, DiffPairsRouting and RoutingLayers leave them out;
  `get_board_info` counts them as `pour_copper`. Rule scopes are decided per object: a
  pour's copper is InPolygon, but IsPolygon, IsNamedPolygon and InPolygonClass match only
  the polygon object, as in Altium. On Altium's Bluetooth Sentinel example (ten
  Horizontal GND pours: 6,473 tracks, 675 arcs) GND copper is within 10 mil of all 40 of
  the 40 longest signal nets (was 30), 3.94 mil at the closest: the pour's arcs around
  vias, at the board's 3.937 mil clearance. The 83 real boards have no hatched pours
- Copper distances to arcs are exact. Arcs are still split into chords (at most 0.25 mil
  sagitta) for the spatial index and for length, but each chord row now carries its
  arc's centre and radius (`Primitive.arc_x` / `arc_y` / `arc_radius`, same `prims`
  columns), and distances measure the arc between the chord's ends, grown by half the
  width, to points, tracks, pads, vias and other arcs, with bounding boxes that include
  the arc's bulge. Chords lie inside their arc, so copper on an arc's inner side read up
  to 0.25 mil too close and on its outer side up to 0.25 mil too far. This applies to
  `get_net_neighbors`, `query_pcb_region`, the clearance spot-check, DRC violation nets
  and `check_vias` connections. The clearance spot-check drops its
  `arc_chord_marginal_pairs` allowance: on Altium's Bluetooth Sentinel example the GND
  pour's arcs around vias read 3.937 mil, exactly the clearance (were 3.69), and the 10
  longest signal nets report no violations. On the MiniPC example the bends of the SFP
  differential pairs read 5.91 mil between legs on all four pairs (were 5.72 to 5.89);
  the other public examples are unchanged. On the 83 real boards no gap within 10 mil
  of a net's arc changes, and neither does the clearance spot-check of their arc nets

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
