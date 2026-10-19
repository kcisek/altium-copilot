"""Schematic-vs-PCB synchronisation check: an offline Project > Show Differences.

The schematic side is the compiled netlist: the Protel .NET part list (every compiled
part with its footprint and comment; compile-masked parts are not in it) and nets,
enriched from the sheets with each part's unique IDs, footprint models, kind and pin
designators. The PCB side is the saved PcbDoc. Matching follows Altium's comparator:
components link by unique ID, and by designator only when no unique ID links them (the
'Failed to Match Unique Identifiers' case); nets match by shared members first (Min
Match %, Min Matched Members) and by name second. A pin whose net changed is one
finding here, where Altium lists it under Extra Pins in Nets on both sides.
"""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

MECHANICAL, GRAPHICAL, JUMPER = 1, 2, 6
KIND_NAMES = {
    0: "standard", 1: "mechanical", 2: "graphical", 3: "net tie (in BOM)",
    4: "net tie", 5: "standard (no BOM)", 6: "jumper",
}

# Each check and the Altium Comparator difference (or ECO message) it corresponds to.
ALTIUM_NAMES = {
    "unknown_pin": "ECO error 'Unknown Pin' (Add Pins To Nets)",
    "pad_net": "Extra Pins in Nets",
    "missing_from_pcb": "Extra Components (schematic side)",
    "extra_on_pcb": "Extra Components (PCB side)",
    "designator": "Different Designators",
    "footprint": "Different Footprints",
    "comment": "Different Comments",
    "net_only_in_schematic": "Extra Nets (schematic side)",
    "net_only_on_pcb": "Extra Nets (PCB side)",
    "net_name": "Changed Net Names",
    "component_link": "Failed to Match Unique Identifiers (matched by designator)",
}
CHECK_GROUPS = {
    "components": (
        "missing_from_pcb", "extra_on_pcb", "designator", "footprint", "comment",
        "component_link",
    ),
    "pins": ("unknown_pin", "pad_net"),
    "nets": ("net_only_in_schematic", "net_only_on_pcb", "net_name"),
}
_CHECK_RANK = {check: rank for rank, check in enumerate(ALTIUM_NAMES)}
_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}
DEFAULT_LIMIT = 100

# Altium's installation defaults for net matching (Project Options > Comparator).
DEFAULT_NET_MATCHING = {
    "min_match_percent": 75,
    "min_matched_members": 3,
    "name_matching": True,
    "source": "Altium defaults",
}
_UNNAMED_NET = re.compile(r"^__unnamed_\d+$")

NOT_COMPARED = (
    "Parameters, net classes, component classes, rooms, rules and differential pairs "
    "are not compared (see get_classes, list_diff_pairs, get_design_rules)."
)
PIN_MAP_NOTE = (
    "Symbol pin maps (a pin mapped to a differently named pad) are not read: such a "
    "pin shows as an unknown pin. Pin and pad designators match exactly, else "
    "case-insensitively."
)
IDENTICAL_NETS_NOTE = (
    "Nets whose members are identical are matched even below Min Matched Members, and "
    "a different name is reported as a renamed net; Altium lists such small nets as an "
    "extra net on each side, and Update PCB has the same effect."
)


@dataclass
class _Part:
    """One compiled schematic part."""

    name: str
    footprint: str | None = None
    comment: str | None = None
    unique_ids: list[str] = field(default_factory=list)
    footprints: list[str] = field(default_factory=list)
    kind: int = 0
    sheet: str | None = None
    logical: str | None = None
    pin_designators: list[str] = field(default_factory=list)
    # Without the compiled part list: each unique ID's footprint from the sheets.
    footprint_by_uid: dict[str, str] = field(default_factory=dict)


def net_matching_options(prj_pcb_path: str | None) -> dict:
    """The project's Net row of [Comparison Options], else Altium's defaults.

    A row reads 'Kind=Net|MinPercent=75|MinMatch=3|...|UseName=-1'. The keys are not
    documented; they line up with Altium's Comparator tab (UseName -1 = match names
    after members, 0 = never).
    """
    options = dict(DEFAULT_NET_MATCHING)
    if not prj_pcb_path:
        return options
    try:
        text = Path(prj_pcb_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return options
    for line in text.splitlines():
        if not line.startswith("ComparisonOptions") or "Kind=Net|" not in line:
            continue
        fields = dict(
            item.split("=", 1) for item in line.split("=", 1)[1].split("|") if "=" in item
        )
        if fields.get("Kind") != "Net":
            continue
        try:
            options["min_match_percent"] = int(fields.get("MinPercent", 75))
            options["min_matched_members"] = int(fields.get("MinMatch", 3))
        except ValueError:
            return dict(DEFAULT_NET_MATCHING)
        options["name_matching"] = fields.get("UseName", "-1") != "0"
        options["source"] = "project options"
        break
    return options


def _schematic_parts(netlist: dict) -> tuple[dict[str, _Part], bool]:
    """Compiled parts by netlist designator, and whether the .NET part list was read.

    Without it (an older netlist), the parts are the enriched components plus every
    designator in the nets; footprints then come from the sheets and comments are
    not known.
    """
    compiled = netlist.get("parts") or {}
    components = netlist.get("components") or {}
    if compiled:
        names = list(compiled)
    else:
        names = list(dict.fromkeys([*components, *(netlist.get("pin_to_net") or {})]))
    parts: dict[str, _Part] = {}
    for name in names:
        meta = components.get(name) or {}
        unique_ids = list(meta.get("unique_ids") or [])
        if meta.get("unique_id") and meta["unique_id"] not in unique_ids:
            unique_ids.append(meta["unique_id"])
        footprints = list(meta.get("footprints") or [])
        if compiled:
            footprint = compiled[name].get("footprint") or None
            comment = compiled[name].get("comment", "")
        else:
            footprint = meta.get("footprint") or (
                footprints[0] if len(footprints) == 1 else None
            )
            comment = None
        parts[name] = _Part(
            name=name,
            footprint=footprint,
            comment=comment,
            unique_ids=unique_ids,
            footprints=footprints,
            kind=int(meta.get("kind") or 0),
            sheet=meta.get("sheet"),
            logical=meta.get("logical_refdes"),
            pin_designators=list(meta.get("pin_designators") or []),
            footprint_by_uid={} if compiled else dict(meta.get("footprint_by_uid") or {}),
        )
    return parts, bool(compiled)


class _Board:
    """The PCB side: components, their pads by name, and net members."""

    def __init__(self, index):
        self.index = index
        self.components = {component.index: component for component in index.pcb.components}
        self.pads: dict[int, dict[str, set]] = defaultdict(lambda: defaultdict(set))
        # Every component pad on each net, linked or not: (component, pad name).
        self.net_members: dict[str, set[tuple[int, str]]] = defaultdict(set)
        rows = index.db.execute(
            "SELECT component, pad_name, net_id FROM prims "
            "WHERE kind = 'pad' AND component IS NOT NULL"
        ).fetchall()
        for row in rows:
            pad = row["pad_name"]
            if not pad:
                continue
            net = index.pcb.nets[row["net_id"]] if row["net_id"] is not None else None
            self.pads[row["component"]][pad].add(net)
            if net is not None:
                self.net_members[net].add((row["component"], pad))

    def designator(self, component) -> str:
        return component.designator or component.refdes or f"#{component.index}"

    def signature(self, component) -> dict[str, str]:
        """Pad name -> net for the component's connected pads (one net each)."""
        return {
            pad: next(iter(nets))
            for pad, nets in self.pads.get(component.index, {}).items()
            if len(nets) == 1 and None not in nets
        }

    def pad_for(self, component, pin: str) -> str | None:
        """The pad a schematic pin lands on: same designator, else the one pad whose
        designator differs only in case."""
        pads = self.pads.get(component.index, {})
        if pin in pads:
            return pin
        folded = [pad for pad in pads if pad.lower() == pin.lower()]
        return folded[0] if len(folded) == 1 else None


def _same(a: str | None, b: str | None) -> bool:
    """Altium compares designators, footprints and comments case-insensitively."""
    return (a or "").strip().lower() == (b or "").strip().lower()


def _link(parts: dict[str, _Part], board: _Board, pin_to_net: dict) -> dict[int, tuple]:
    """PCB component index -> (part name, how linked).

    By unique ID first. Channel copies (and the parts of a component placed on
    several sheets) share IDs, so a group with several parts or footprints pairs by
    designator, then by pad-net signature, then the last one left. Components still
    unlinked pair by designator.
    """
    links: dict[int, tuple] = {}
    taken: set[str] = set()
    by_uid: dict[str, list[str]] = defaultdict(list)
    for part in parts.values():
        for uid in part.unique_ids:
            by_uid[uid].append(part.name)
    groups: dict[str, list] = defaultdict(list)
    for component in board.components.values():
        tail = component.source_uid_tail
        if tail and tail in by_uid:
            groups[tail].append(component)

    def link(component, name: str, how: str) -> None:
        links[component.index] = (name, how)
        taken.add(name)

    for uid, components in groups.items():
        names = [name for name in dict.fromkeys(by_uid[uid]) if name not in taken]
        if not names:
            continue
        if len(names) == 1 and len(components) == 1:
            link(components[0], names[0], "unique_id")
            continue
        # Prefer the footprint the part asks for when two placements share a designator
        # (a variant's alternate part sits beside the main one).
        footprint_of = {name: parts[name].footprint for name in names}
        ordered = sorted(
            components,
            key=lambda c: not any(
                _same(board.designator(c), name) and _same(c.footprint, footprint_of[name])
                for name in names
            ),
        )
        for component in ordered:
            match = next(
                (n for n in names if n not in taken and _same(n, board.designator(component))),
                None,
            )
            if match:
                link(component, match, "unique_id")
        left = [c for c in components if c.index not in links]
        free = [n for n in names if n not in taken]
        for component in left:
            signature = board.signature(component)
            matches = [
                n for n in free
                if n not in taken and signature
                and dict(pin_to_net.get(n) or {}) == signature
            ]
            if len(matches) == 1:
                link(component, matches[0], "unique_id")
        left = [c for c in components if c.index not in links]
        free = [n for n in names if n not in taken]
        if len(left) == 1 and len(free) == 1:
            link(left[0], free[0], "unique_id")

    by_name = {name.lower(): name for name in parts}
    for component in board.components.values():
        if component.index in links:
            continue
        name = by_name.get(board.designator(component).lower())
        if name and name not in taken:
            link(component, name, "designator")
    return links


class _Report:
    def __init__(self):
        self.findings: list[dict] = []

    def add(self, check: str, severity: str, issue: str, **fields) -> None:
        self.findings.append({"check": check, "severity": severity, **fields, "issue": issue})


def _component_findings(report, parts, board, links, is_dnp, newer_sheets) -> None:
    linked_parts = {name: index for index, (name, _) in links.items()}

    def sheet_fields(part: _Part) -> dict:
        fields: dict = {}
        if part.sheet:
            fields["sheet"] = part.sheet
            if part.sheet.lower() in newer_sheets:
                fields["sheet_saved_after_pcb"] = True
        return fields

    for part in parts.values():
        if part.name in linked_parts:
            continue
        fields = {"schematic": part.name, "footprint": part.footprint, **sheet_fields(part)}
        if is_dnp and is_dnp(part.name):
            fields["dnp"] = True
        if part.kind in (MECHANICAL, GRAPHICAL) or not part.footprint:
            reason = (
                f"a {KIND_NAMES[part.kind]} part" if part.kind in (MECHANICAL, GRAPHICAL)
                else "it has no footprint"
            )
            report.add(
                "missing_from_pcb", "info",
                f"{part.name} is not on the PCB; {reason}, so Update PCB may not place it.",
                **fields,
            )
            continue
        note = (
            " It is not fitted in the active variant, but Altium still places every "
            "variant's footprints." if fields.get("dnp") else ""
        )
        report.add(
            "missing_from_pcb", "warning",
            f"{part.name} is not on the PCB: Update PCB would add it.{note}",
            **fields,
        )

    linked_uids = {
        board.components[index].source_uid_tail: (name, index)
        for index, (name, _) in links.items()
        if board.components[index].source_uid_tail
    }
    all_uids = {uid for part in parts.values() for uid in part.unique_ids}
    for component in board.components.values():
        if component.index in links:
            continue
        designator = board.designator(component)
        fields = {"pcb": designator, "footprint": component.footprint}
        if component.kind != 0:
            fields["kind"] = KIND_NAMES.get(component.kind, str(component.kind))
        tail = component.source_uid_tail
        if component.kind == GRAPHICAL:
            report.add(
                "extra_on_pcb", "info",
                f"{designator} is a graphical part placed on the PCB only; Altium never "
                "synchronizes graphical parts.",
                **fields,
            )
        elif tail and tail in linked_uids:
            twin_name, twin_index = linked_uids[tail]
            twin = board.designator(board.components[twin_index])
            if _same(twin, designator):
                report.add(
                    "extra_on_pcb", "info",
                    f"{designator} is placed twice with one schematic part ({twin_name}): "
                    "normally a variant's alternate part with another footprint.",
                    **fields,
                )
            else:
                report.add(
                    "extra_on_pcb", "warning",
                    f"{designator} carries the unique ID of {twin_name} (on the PCB as "
                    f"{twin}): a copied footprint; Update PCB would remove it.",
                    **fields,
                )
        elif tail:
            gone = "" if tail in all_uids else " Its unique ID is in no compiled part."
            report.add(
                "extra_on_pcb", "warning",
                f"{designator} came from the schematic but its part is gone (deleted, "
                f"compile-masked or re-placed with a new ID): Update PCB would remove "
                f"it.{gone}",
                **fields,
            )
        elif component.kind == MECHANICAL:
            report.add(
                "extra_on_pcb", "info",
                f"{designator} is a mechanical part placed on the PCB only.",
                **fields,
            )
        else:
            report.add(
                "extra_on_pcb", "warning",
                f"{designator} was placed on the PCB only. Unless Remove Components is "
                "off in the project's ECO options, Update PCB would remove it; make it "
                "a Mechanical part to keep it.",
                **fields,
            )

    for index, (name, how) in links.items():
        component = board.components[index]
        part = parts[name]
        designator = board.designator(component)
        if how == "designator":
            detail = (
                "the PCB part has no schematic unique ID"
                if not component.source_uid_tail
                else "their unique IDs differ"
            )
            report.add(
                "component_link", "info",
                f"{designator} matched by designator: {detail}. Altium asks to match "
                "by designator (Failed to Match Unique Identifiers), or use Project > "
                "Component Links.",
                component=designator,
            )
        if not _same(designator, name) and not (
            part.logical and _same(designator, part.logical)
        ):
            report.add(
                "designator", "warning",
                f"The schematic calls this part {name}, the PCB {designator}: the "
                "designators differ (e.g. re-annotated and not yet sent to the PCB).",
                schematic=name, pcb=designator, **sheet_fields(part),
            )
        expected = part.footprint_by_uid.get(component.source_uid_tail or "", part.footprint)
        if expected and not _same(expected, component.footprint):
            fields = {
                "component": designator,
                "schematic": expected,
                "pcb": component.footprint,
                **sheet_fields(part),
            }
            alternative = any(_same(component.footprint, f) for f in part.footprints)
            if alternative:
                fields["pcb_is_a_schematic_alternative"] = True
            report.add(
                "footprint", "warning",
                f"{designator} uses footprint {component.footprint} on the PCB; the "
                f"schematic asks for {expected}"
                + (" (the PCB one is another of the part's footprint models)." if alternative
                   else "."),
                **fields,
            )
        if part.comment is not None and component.comment is not None and not _same(
            part.comment, component.comment
        ):
            report.add(
                "comment", "warning",
                f"{designator}: comment '{component.comment}' on the PCB, "
                f"'{part.comment}' in the schematic.",
                component=designator, schematic=part.comment, pcb=component.comment,
                **sheet_fields(part),
            )


def _net_findings(report, parts, board, links, netlist, options) -> dict:
    """Member sets, net matching, and the pin, pad and net findings. Returns counts."""
    component_of = {name: board.components[index] for index, (name, _) in links.items()}
    skip = {
        index for index, component in board.components.items()
        if component.kind in (JUMPER, GRAPHICAL)
    } | {
        component_of[name].index for name, part in parts.items()
        if name in component_of and part.kind == JUMPER
    }
    counts = Counter()

    # Schematic members as (PCB component, pad), and the pins with no pad.
    sch_members: dict[str, set] = defaultdict(set)
    off_board: Counter = Counter()
    net_of_pad: dict[tuple[int, str], str] = {}
    for net, pins in (netlist.get("nets") or {}).items():
        sch_members.setdefault(net, set())
        for name, pin in pins:
            component = component_of.get(name)
            if component is None:
                off_board[net] += 1
                continue
            if component.index in skip:
                continue
            pad = board.pad_for(component, pin)
            if pad is None:
                part = parts.get(name)
                if part and part.footprint and not _same(part.footprint, component.footprint):
                    # Update PCB swaps the footprint first; its pads are not on the board.
                    counts["pins_behind_a_footprint_change"] += 1
                    continue
                designator = board.designator(component)
                report.add(
                    "unknown_pin", "error",
                    f"Pin {pin} of {designator} is on net {net}, but footprint "
                    f"{component.footprint} has no pad {pin}: Update PCB stops with "
                    f"'Unknown Pin: Pin {designator}-{pin}'.",
                    pin=f"{designator}-{pin}", net=net, footprint=component.footprint,
                )
                continue
            key = (component.index, pad)
            sch_members[net].add(key)
            net_of_pad.setdefault(key, net)

    linked_indexes = {component.index for component in component_of.values()} - skip
    pcb_members: dict[str, set] = defaultdict(set)
    for net, members in board.net_members.items():
        for key in members:
            if key[0] in linked_indexes:
                pcb_members[net].add(key)

    # Match nets: shared members first (the most shared first), then names.
    owner: dict[tuple[int, str], set[str]] = defaultdict(set)
    for net, members in pcb_members.items():
        for key in members:
            owner[key].add(net)
    candidates = []
    for net, members in sch_members.items():
        shared = Counter(pcb for key in members for pcb in owner.get(key, ()))
        for pcb, count in shared.items():
            size = max(len(members), len(pcb_members[pcb]))
            identical = members == pcb_members[pcb]
            if identical or (
                count >= options["min_matched_members"]
                and 100.0 * count / size >= options["min_match_percent"]
            ):
                candidates.append((-count, pcb != net, net, pcb, identical))
    candidates.sort()
    to_pcb: dict[str, str] = {}
    matched_pcb: set[str] = set()
    for _, _, net, pcb, _ in candidates:
        if net in to_pcb or pcb in matched_pcb:
            continue
        to_pcb[net] = pcb
        matched_pcb.add(pcb)
    counts["matched_by_members"] = len(to_pcb)
    if options["name_matching"]:
        unmatched_pcb = {
            net for net in board.net_members if net not in matched_pcb
        } | {net for net in board.index.pcb.nets if net not in matched_pcb}
        folded: dict[str, list[str]] = defaultdict(list)
        for net in unmatched_pcb:
            folded[net.lower()].append(net)
        for net in sch_members:
            if net in to_pcb:
                continue
            pcb = net if net in unmatched_pcb else None
            if pcb is None and len(folded.get(net.lower(), [])) == 1:
                pcb = folded[net.lower()][0]
            if pcb and pcb not in matched_pcb:
                to_pcb[net] = pcb
                matched_pcb.add(pcb)
                counts["matched_by_name"] += 1

    for net, pcb in to_pcb.items():
        if pcb != net:
            report.add(
                "net_name", "info",
                f"Net {net} is named {pcb} on the PCB.",
                schematic=net, pcb=pcb, members=len(sch_members[net]),
            )
    for net, members in sch_members.items():
        if net in to_pcb or not (members or off_board[net]):
            continue  # matched, or only on jumpers (Altium does not sync their nets)
        fields = {"net": net, "members": len(members) + off_board[net]}
        if off_board[net]:
            fields["members_not_on_pcb"] = off_board[net]
        report.add(
            "net_only_in_schematic", "warning",
            f"Net {net} has no counterpart on the PCB: Update PCB would add it.",
            **fields,
        )
    for net, members in board.net_members.items():
        if net in matched_pcb or _UNNAMED_NET.match(net):
            continue
        if len(members) <= 1:
            # The Protel netlist leaves out single-pin nets the PCB keeps.
            counts["single_pin_pcb_nets"] += 1
            continue
        if all(key[0] in skip for key in members):
            continue  # only on jumper or graphical pads
        on_linked = sum(1 for key in members if key[0] in linked_indexes)
        if not on_linked:
            report.add(
                "net_only_on_pcb", "info",
                f"Net {net} is only on parts placed on the PCB alone.",
                net=net, members=len(members),
            )
            continue
        report.add(
            "net_only_on_pcb", "warning",
            f"Net {net} is not in the schematic: Update PCB would remove it.",
            net=net, members=len(members),
        )

    # Pads: one finding per pad whose net differs from its pin's.
    for index in sorted(linked_indexes):
        component = board.components[index]
        designator = board.designator(component)
        part = parts[links[index][0]]
        for pad, nets in sorted(board.pads.get(index, {}).items()):
            key = (index, pad)
            net = net_of_pad.get(key)
            for pcb_net in sorted(nets, key=lambda value: value or ""):
                pin = f"{designator}-{pad}"
                if net is not None:
                    expected = to_pcb.get(net)
                    if pcb_net is None:
                        report.add(
                            "pad_net", "error",
                            f"Pad {pin} has no net on the PCB; the schematic connects "
                            f"it to {net}.",
                            pin=pin, schematic_net=net, pcb_net=None,
                        )
                    elif pcb_net != expected:
                        report.add(
                            "pad_net", "error",
                            f"Pad {pin} is on {pcb_net} on the PCB; the schematic "
                            f"connects it to {net}"
                            + (f" ({expected} on the PCB)." if expected and expected != net
                               else "."),
                            pin=pin, schematic_net=net, pcb_net=pcb_net,
                        )
                    continue
                if pcb_net is None:
                    continue
                if board.net_members.get(pcb_net) == {key}:
                    continue  # a single-pin net, absent from the Protel netlist
                if part.pin_designators and not any(
                    _same(pad, pin_name) for pin_name in part.pin_designators
                ):
                    report.add(
                        "pad_net", "warning",
                        f"Pad {pin} is on {pcb_net} on the PCB but the schematic symbol "
                        "has no such pin (e.g. a shield pad tied by hand): Update PCB "
                        "would take it off the net.",
                        pin=pin, schematic_net=None, pcb_net=pcb_net,
                    )
                else:
                    report.add(
                        "pad_net", "error",
                        f"Pad {pin} is on {pcb_net} on the PCB but its schematic pin is "
                        "not connected.",
                        pin=pin, schematic_net=None, pcb_net=pcb_net,
                    )
    counts["schematic_nets"] = len(sch_members)
    counts["pcb_nets"] = len(board.net_members)
    return counts


def run_sync_check(
    index,
    netlist: dict | None,
    is_dnp=None,
    checks: list[str] | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    sheets_saved_after_pcb: list[str] | None = None,
    net_matching: dict | None = None,
) -> dict:
    """Compare the compiled schematic with the PCB; findings are paginated."""
    requested = [check.strip().lower() for check in checks or [] if check.strip()]
    unknown = [check for check in requested if check not in CHECK_GROUPS]
    if unknown:
        return {
            "error": "invalid_checks",
            "message": f"Unknown checks {unknown}; use {sorted(CHECK_GROUPS)}.",
        }
    run = requested or list(CHECK_GROUPS)
    parts, compiled = _schematic_parts(netlist or {})
    if not parts:
        return {
            "error": "no_netlist",
            "message": "The schematic netlist has no parts: load the project with "
            "set_project_dir (or refresh_netlist) first.",
        }
    options = net_matching or dict(DEFAULT_NET_MATCHING)
    board = _Board(index)
    links = _link(parts, board, (netlist or {}).get("pin_to_net") or {})
    newer = {sheet.lower() for sheet in sheets_saved_after_pcb or []}
    report = _Report()
    _component_findings(report, parts, board, links, is_dnp, newer)
    counts = _net_findings(report, parts, board, links, netlist or {}, options)

    wanted = {check for group in run for check in CHECK_GROUPS[group]}
    findings = [finding for finding in report.findings if finding["check"] in wanted]
    findings.sort(key=lambda finding: (
        _SEVERITY_RANK[finding["severity"]],
        _CHECK_RANK[finding["check"]],
        finding["issue"],
    ))
    summary = Counter(finding["check"] for finding in findings)
    severities = Counter(finding["severity"] for finding in findings)
    offset = max(0, int(offset or 0))
    limit = max(1, int(limit or DEFAULT_LIMIT))
    how = Counter(how for _, how in links.values())
    not_checked = [NOT_COMPARED, PIN_MAP_NOTE]
    if not compiled:
        not_checked.append(
            "The netlist has no compiled part list (.NET part blocks): footprints come "
            "from the sheets and comments are not compared."
        )
    skipped_pads = index.pcb.skipped_records.get("Pads6", 0)
    if skipped_pads:
        not_checked.append(
            f"{skipped_pads} pads without copper (e.g. NPTH holes) are not read: a pin "
            "connected to one shows as an unknown pin."
        )
    if counts["pins_behind_a_footprint_change"]:
        not_checked.append(
            f"{counts['pins_behind_a_footprint_change']} pins of parts whose footprint "
            "changes were not checked against pads: the new footprint is not on the board."
        )
    notes = [IDENTICAL_NETS_NOTE]
    if counts["single_pin_pcb_nets"]:
        notes.append(
            f"{counts['single_pin_pcb_nets']} PCB nets with a single pad are not "
            "reported: the Protel netlist leaves out single-pin nets."
        )
    result: dict = {
        "in_sync": not (severities["error"] or severities["warning"]),
        "checks_run": run,
        "components": {
            "schematic": len(parts),
            "pcb": len(board.components),
            "linked_by_unique_id": how["unique_id"],
            "linked_by_designator": how["designator"],
        },
        "nets": {
            "schematic": counts["schematic_nets"],
            "pcb": counts["pcb_nets"],
            "matched_by_members": counts["matched_by_members"],
            "matched_by_name": counts["matched_by_name"],
        },
        "net_matching": options,
        "summary": dict(summary),
        "severity_counts": {
            level: severities.get(level, 0) for level in ("error", "warning", "info")
        },
        "altium_names": {check: ALTIUM_NAMES[check] for check in summary},
        "findings": findings[offset : offset + limit],
        "total": len(findings),
        "offset": offset,
        "has_more": offset + limit < len(findings),
    }
    if result["has_more"]:
        result["next_offset"] = offset + limit
    result["not_checked"] = not_checked
    result["notes"] = notes
    return result
