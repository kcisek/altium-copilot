"""Assembly guide for prototype builds (get_assembly_guide) over a PcbIndex: a variant's
placed parts in soldering order, grouped into steps, each part with its pick-and-place
coordinates, rotation and a pin-1 or polarity hint, as a Markdown checklist (Done and
Skipped columns) or CSV.

Pure logic, no MCP. Lengths are mils inside; coordinates are counted from the board's
relative origin; the pages render in mm or mil. The part facts are dfm_assembly's
(cached on the index): mount class, side, pads, 3D-body heights, chip size codes,
fiducial flags and Altium's pick-and-place centres.

Placed: a part of a BOM kind (Standard, Mechanical, Net Tie (In BOM), Jumper) with
soldered pads (smd, tht or mixed), not a fiducial, fitted in the variant and, with a
netlist, in the compiled BOM. Everything else is listed as left out, by reason.

Sequence (SEQUENCE_RULES): the SMD of both sides first, one side after the other, then
the through-hole parts (top, then bottom). The first SMD side is the one whose tallest
SMD part is shorter (by more than SIDE_HEIGHT_TIE_MIL), else the one with fewer SMD
parts, else the bottom: the side with the taller, heavier parts is soldered last, so in
reflow its parts never hang under the board in a second pass, and by hand the board
rests on the flatter side. Mixed parts (SMD pads and plated pins) go with the SMD of
their side. On each side and process, steps go from short to tall: the 3D-body height
(the tallest member's), else for a chip passive at most its EIA body width (an MLCC is
never thicker than it is wide), else unknown: those go last, smallest pad extent first.

Steps: one per side and process and per BOM line (get_bom's grouping, MPN first) with a
netlist, else per footprint and Comment (approximate). Pin 1: for polarized parts
(diodes and LEDs, polarized capacitors, batteries and buzzers, connectors, pads named
+/- or A/K, and any part with three or more pad names: ICs, transistors, crystals) the
pad named for the cathode, +, pin 1 or A1, as a direction from the part's centre (the
pad centres' box) seen from the side being assembled: the bottom is seen with the
board flipped left to right. A diode or polarized capacitor with only numbered pads gets
its pin 1, which does not say where the cathode or + is: counted, with PIN_NUMBER_NOTE.
"""

import csv
import io
import math
import re
from dataclasses import dataclass, field
from typing import Callable

try:
    from services import dfm_assembly as assembly
    from services.bom import BOM_KINDS, BomPart, _cell, group_key, natural_key
except ImportError:
    from server.services import dfm_assembly as assembly
    from server.services.bom import BOM_KINDS, BomPart, _cell, group_key, natural_key

MM = 1 / 0.0254
SIDES = ("top", "bottom")
# The first SMD side: tallest SMD parts within this are alike (then the count decides).
SIDE_HEIGHT_TIE_MIL = 0.1 * MM
# Rows per page, largest first: the page shrinks until it fits the result size.
ROW_LIMITS = (300, 120, 50, 20, 5)
LEFT_OUT_SHOWN = 60  # designators listed per left-out reason
CELL_CHARS = 60  # Markdown cells longer than this are cut
# A pin-1 pad within this share of the part's half extent of its centre is centred.
_DIRECTION_SHARE = 1.0 / 3.0

_PREFIX = re.compile(r"^[A-Za-z]+")
_DIODES = {"D", "LED", "LD", "CR", "ZD", "VD", "DS", "DZ", "TVS"}
# Two-pin parts whose orientation matters: batteries, buzzers, speakers, microphones,
# connectors and ICs.
_POLAR_TWO_PIN = {"BT", "BAT", "BZ", "LS", "SP", "MK", "MIC", "J", "P", "CN", "CON", "U",
                  "IC"}
_CAPACITORS = {"C", "CP", "CE", "EC", "TC"}
_POLAR_CAP_PREFIXES = {"CP", "CE", "EC", "TC"}
# Tantalum, electrolytic and other polarized capacitors by footprint, Comment,
# description or library reference: IPC-7351 CAPAE/CAPT/CAPMP/CAPPR/CAPPA, Altium's
# 'Cap Pol'.
_POLAR_CAP = re.compile(
    r"tant|electro|elko|polar|cap ?pol|\bpol\b|\bcp\b|capae|capt|capmp|cappr|cappa|"
    r"\bcae|alu(?:minium|minum)?[ _-]?el|\be-?cap\b|\belec\b",
    re.IGNORECASE,
)
_CATHODE = ("k", "c", "cathode", "kathode")
_ANODE = ("a", "anode")
_PLUS = ("+", "pos", "positive")
_MINUS = ("-", "neg", "negative")
_PIN1 = ("1", "a1", "p1", "pin1", "01", "001")
_PROCESS_NAMES = {"smd": "SMD", "tht": "through-hole"}
NO_PIN1 = "no pin 1 marked"
# A diode or polarized capacitor with only numbered pads gets a pin-1 hint, which does
# not say which end is the cathode or +.
PIN_NUMBER_NOTE = (
    "Diodes and polarized capacitors whose pads are only numbered get a pin-1 hint: "
    "whether pin 1 is the cathode or + depends on the footprint library, so check the "
    "part's marking against the silkscreen."
)

SEQUENCE_RULES = (
    "SMD before through-hole: the SMD of both sides first, one side after the other, "
    "then the through-hole parts (top, then bottom), whose leads would stick out of the "
    "other side and keep the board from lying flat for the stencil and the iron.",
    "On each side, short parts first and tall last (3D body height; a chip part without "
    "a body counts as at most its width; parts with no height go last, smallest pad "
    "extent first), so tall parts never block the iron or the nozzle.",
    "Mixed parts (SMD pads and plated pins) go with the SMD of their side: solder their "
    "plated pins with the through-hole parts.",
)
LEFT_OUT_REASONS = {
    "not_fitted": "Not Fitted in the variant",
    "fiducials": "fiducials (copper only, nothing to place)",
    "no_bom": "not in the BOM by kind: Graphical, Net Tie and Standard (No BOM) parts",
    "mounting_holes": "mounting holes: only non-plated holes, or Mechanical parts of holes",
    "no_pads": "in the BOM but without pads (heatsinks, screws, labels): fit by hand",
    "not_in_bom": "on the PCB but not in the compiled BOM: check_sch_pcb_sync",
    "bom_not_on_board": "fitted BOM parts with no PCB footprint: hardware such as screws or "
                        "jumper links to fit by hand, or the PCB out of sync "
                        "(check_sch_pcb_sync)",
}


@dataclass
class GuideVariant:
    """The variant the guide follows. is_dnp: applied to a part's netlist name, else its
    PCB designator (None: every part fitted); bom: the variant's BOM parts by netlist name
    (fitted or not), None without a netlist (steps grouped by footprint and Comment);
    no_bom: netlist names the BOM leaves out by kind; unapplied: Not Fitted entries that
    name no designator (a UniqueId only) and could not be bound without the schematic
    parts, so is_dnp misses their parts."""

    name: str
    is_dnp: Callable[[str], bool] | None = None
    bom: dict[str, BomPart] | None = None
    no_bom: frozenset = frozenset()
    unapplied: int = 0


@dataclass
class Row:
    """One placed part (mils, from the board origin)."""

    step: int
    designator: str
    side: str
    mount: str
    centre: tuple[float, float]  # Center-X/Y
    reference: tuple[float, float]  # Ref-X/Y
    rotation: float
    polarized: bool
    pin1: str | None  # the hint; NO_PIN1 when polarized and none found; None: not polarized
    polarity: str | None = None  # polarity_class: diode | polar | pin1 | None


@dataclass
class Step:
    number: int
    side: str
    process: str  # smd | tht
    comment: str | None
    footprint: str | None
    manufacturer: str | None
    mpn: str | None
    height_mil: float | None
    height_basis: str | None  # body | chip | None (unknown)
    size_code: str | None
    extent_mil2: float
    rows: list[Row] = field(default_factory=list)


@dataclass
class Guide:
    variant: str
    grouping: str  # bom_lines | footprint_and_comment
    side: str | None
    origin: tuple[float, float]
    first: "FirstSide | None"
    steps: list[Step]
    rows: list[Row]
    left_out: dict[str, list[str]]
    placements: dict[str, dict[str, int]]

    @property
    def approximate(self) -> bool:
        return self.grouping != "bom_lines"


# ---------- helpers ----------


def _prefix(name: str) -> str:
    match = _PREFIX.match(name or "")
    return match[0].upper() if match else ""


def _fold(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _pad_names(pcb, part) -> list[tuple[str, float, float]]:
    """(folded name, x, y) of each of the part's pads ('' when unnamed)."""
    out = []
    for row in part.pads:
        pad = pcb.primitives[row - 1]
        out.append(((pad.pad_name or "").strip().lower(), pad.x1, pad.y1))
    return out


def _polar_cap(part, bom_part: BomPart | None, prefix: str) -> bool:
    if prefix in _POLAR_CAP_PREFIXES:
        return True
    if prefix not in _CAPACITORS:
        return False
    texts = [part.footprint, part.comment]
    if bom_part is not None:
        texts += [bom_part.footprint, bom_part.comment, bom_part.value,
                  bom_part.description, bom_part.lib_reference]
    return any(_POLAR_CAP.search(text) for text in texts if text)


def polarity_class(part, names: list[tuple[str, float, float]],
                   bom_part: BomPart | None = None) -> str | None:
    """diode | polar (a + pin) | pin1 (orientation by pin 1) | None (not polarized).
    names: _pad_names of the part."""
    prefix = _prefix(part.designator or "")
    distinct = {name for name, _, _ in names if name}
    if prefix in _DIODES or ("k" in distinct and "a" in distinct):
        return "diode"
    if _polar_cap(part, bom_part, prefix) or distinct & set(_PLUS + _MINUS):
        return "polar"
    if prefix in _POLAR_TWO_PIN and len(names) >= 2:
        return "pin1"
    if len(distinct) >= 3 or (not distinct and len(names) >= 3):
        return "pin1"
    return None


def direction(dx: float, dy: float, half_w: float, half_h: float, bottom: bool) -> str:
    """Where (dx, dy) lies from the centre, against the half extents: 'top-left',
    'right', 'centre'; the bottom is seen with the board flipped left to right."""
    if bottom:
        dx = -dx
    nx = dx / half_w if half_w > 1e-6 else 0.0
    ny = dy / half_h if half_h > 1e-6 else 0.0
    vertical = "top" if ny > _DIRECTION_SHARE else "bottom" if ny < -_DIRECTION_SHARE else ""
    horizontal = "right" if nx > _DIRECTION_SHARE else "left" if nx < -_DIRECTION_SHARE else ""
    if vertical and horizontal:
        return f"{vertical}-{horizontal}"
    return vertical or horizontal or "centre"


def pin1_hint(kind: str | None, names, centre, bottom: bool) -> str | None:
    """The hint for a part of polarity class kind: the marked pad and its direction,
    NO_PIN1 when no pad is named for it, None for a part that is not polarized."""
    if kind is None:
        return None
    if kind == "diode":
        wanted = ((_CATHODE, "cathode"), (_PIN1, "pin"), (_ANODE, "anode"))
    elif kind == "polar":
        wanted = ((_PLUS, "positive"), (_PIN1, "pin"), (_MINUS, "negative"))
    else:
        wanted = ((_PIN1, "pin"),)
    xs = [x for _, x, _ in names]
    ys = [y for _, _, y in names]

    def place(x: float, y: float) -> str:
        where = direction(x - centre[0], y - centre[1], (max(xs) - min(xs)) / 2.0,
                          (max(ys) - min(ys)) / 2.0, bottom)
        return "at the centre" if where == "centre" else where

    for options, label in wanted:
        for option in options:
            pads = [(x, y) for name, x, y in names if name == option]
            if not pads:
                continue
            x = sum(point[0] for point in pads) / len(pads)
            y = sum(point[1] for point in pads) / len(pads)
            shown = option.upper()
            if label == "pin":
                return f"pin {shown} {place(x, y)}"
            return f"{label} ({shown}) {place(x, y)}"
    corner = _grid_a1(names) if kind == "pin1" else None
    if corner is not None:
        return f"pin A1 {place(*corner)} (no A1 pad: the grid's corner)"
    return NO_PIN1


_GRID_NAME = re.compile(r"^([a-z]{1,2})(\d{1,3})$")


def _grid_a1(names) -> tuple[float, float] | None:
    """Where a grid array's A1 lies when it has no A1 pad (a depopulated ball): A_k +
    r_1 - r_k for a row r with a column-1 pad and a column k it shares with row A."""
    cells = {}
    for name, x, y in names:
        match = _GRID_NAME.match(name)
        if match:
            cells[(match[1], int(match[2]))] = (x, y)
    if len(cells) < 4 or ("a", 1) in cells:
        return None
    columns = sorted(column for row, column in cells if row == "a")
    rows = sorted((row for row, column in cells if column == 1 and row != "a"),
                  key=lambda row: (len(row), row))
    for row in rows:
        for column in columns:
            if (row, column) in cells:
                ax, ay = cells[("a", column)]
                rx, ry = cells[(row, 1)]
                kx, ky = cells[(row, column)]
                return ax + rx - kx, ay + ry - ky
    return None


def _extent_area(part) -> float:
    rect = part.pad_rect
    if not rect:
        return 0.0
    return math.dist(rect[0], rect[1]) * math.dist(rect[1], rect[2])


def _effective_height(part) -> tuple[float | None, str | None]:
    if part.height_mil is not None:
        return part.height_mil, "body"
    size = assembly.BODY_SIZES_MM.get(part.size_code or "")
    if size is not None:
        return size[1] * MM, "chip"
    return None, None


def _step_height(members) -> tuple[float | None, str | None, str | None]:
    bodies = [part.height_mil for part in members if part.height_mil is not None]
    if bodies:
        return max(bodies), "body", None
    codes = {part.size_code for part in members}
    if len(codes) == 1 and next(iter(codes)) in assembly.BODY_SIZES_MM:
        code = next(iter(codes))
        return assembly.BODY_SIZES_MM[code][1] * MM, "chip", code
    return None, None, None


@dataclass
class FirstSide:
    """The SMD side soldered first, and what decided it: only (one side has SMD),
    height (its tallest SMD part is shorter), count (fewer SMD parts) or convention."""

    side: str
    basis: str
    tallest_mil: dict[str, float | None]
    counts: dict[str, int]


def _first_side(smd: dict[str, list]) -> FirstSide | None:
    sides = [side for side in SIDES if smd.get(side)]
    if not sides:
        return None
    tallest: dict[str, float | None] = {}
    for side in SIDES:
        known = [height for height in (_effective_height(part)[0] for part in smd[side])
                 if height is not None]
        tallest[side] = max(known) if known else None
    counts = {side: len(smd[side]) for side in SIDES}
    if len(sides) == 1:
        return FirstSide(sides[0], "only", tallest, counts)
    top, bottom = tallest["top"], tallest["bottom"]
    if top is not None and bottom is not None and abs(top - bottom) > SIDE_HEIGHT_TIE_MIL:
        return FirstSide("top" if top < bottom else "bottom", "height", tallest, counts)
    if counts["top"] != counts["bottom"]:
        return FirstSide("top" if counts["top"] < counts["bottom"] else "bottom", "count",
                         tallest, counts)
    return FirstSide("bottom", "convention", tallest, counts)


def first_side_text(first: FirstSide | None, to_mm: bool) -> str | None:
    """Why the first SMD side goes first, heights in the output unit."""
    if first is None:
        return None
    side = first.side
    other = "bottom" if side == "top" else "top"
    if first.basis == "only":
        return f"only the {side} side has SMD parts"
    if first.basis == "height":
        return (f"its tallest SMD part ({_length(first.tallest_mil[side], to_mm)}) is "
                f"shorter than the {other} side's ({_length(first.tallest_mil[other], to_mm)}),"
                " so the side with the taller, heavier parts is soldered last: in reflow its "
                "parts never hang under the board in a second pass, and by hand the board "
                "rests on the flatter side")
    if first.basis == "count":
        return ("the tallest SMD parts do not decide it (alike within "
                f"{_length(SIDE_HEIGHT_TIE_MIL, to_mm)}, or no heights) and it has fewer "
                f"SMD parts ({first.counts[side]} against {first.counts[other]}): the "
                "denser side is soldered last, as in a double reflow")
    return ("both sides alike in SMD part height and count: the bottom first, as usual in "
            "a double reflow")


# ---------- the guide ----------


def build_guide(index, variant: GuideVariant, side: str | None = None) -> Guide:
    """The guide for one variant (side: top, bottom or None for both)."""
    pcb = index.pcb
    parts = assembly.parts(index)
    centroids = assembly.centroids(index)
    origin = assembly.origin(index).origin
    bom = variant.bom
    left_out: dict[str, list[str]] = {reason: [] for reason in LEFT_OUT_REASONS}
    placed: list[tuple] = []
    on_board: set[str] = set()
    for part in parts:
        designator = index.designator(part.component) or f"#{part.component}"
        name = part.name  # the netlist name, else the PCB designator
        if name:
            on_board.add(name)
        if side is not None and part.side != side:
            continue
        bom_part = bom.get(name) if bom is not None and name else None
        # The compiled part's kind decides when the netlist has the part, else the PCB's.
        if bom_part is not None or name in variant.no_bom:
            no_bom = name in variant.no_bom
        else:
            no_bom = part.kind not in BOM_KINDS
        # Not Fitted before the reasons that ask for a part to be fitted by hand or
        # checked (a Not Fitted heatsink is not to be fitted).
        if part.fiducial or assembly.named_fiducial(part):
            reason = "fiducials"
        elif no_bom:
            reason = "no_bom"
        elif not part.fitted(variant.is_dnp) or (bom_part is not None and not bom_part.fitted):
            reason = "not_fitted"
        elif part.mount == "mechanical":
            reason = "mounting_holes"
        elif part.mount == "no_pads":
            reason = "no_pads"
        elif bom is not None and bom_part is None:
            reason = "not_in_bom"
        else:
            placed.append((part, bom_part, designator))
            continue
        left_out[reason].append(designator)
    if bom is not None:
        left_out["bom_not_on_board"] = [name for name, bom_part in bom.items()
                                        if bom_part.fitted and name not in on_board]
    smd = {key: [part for part, _, _ in placed
                 if part.side == key and part.mount in ("smd", "mixed")] for key in SIDES}
    first = _first_side(smd)
    order_sides = ([first.side, "bottom" if first.side == "top" else "top"] if first
                   else list(SIDES))
    phases = [("smd", key) for key in order_sides] + [("tht", key) for key in SIDES]
    groups: dict[tuple, list] = {}
    for part, bom_part, designator in placed:
        process = "tht" if part.mount == "tht" else "smd"
        if bom_part is not None:
            key = group_key(bom_part, "mpn")
        else:
            key = ("approximate", _fold(part.footprint), _fold(part.comment))
        groups.setdefault((process, part.side, key), []).append((part, bom_part, designator))
    steps: list[Step] = []
    rows: list[Row] = []
    number = 0
    for process, phase_side in phases:
        phase = []
        for (group_process, group_side, _), members in groups.items():
            if group_process != process or group_side != phase_side:
                continue
            members.sort(key=lambda member: natural_key(member[2]))
            height, basis, code = _step_height([member[0] for member in members])
            first_part, first_bom, _ = members[0]
            source = first_bom
            phase.append((Step(
                number=0, side=phase_side, process=process,
                comment=((source.comment or source.value) if source is not None
                         else first_part.comment) or None,
                footprint=(source.footprint if source is not None and source.footprint
                           else first_part.footprint),
                manufacturer=source.manufacturer if source is not None else None,
                mpn=source.mpn if source is not None else None,
                height_mil=height, height_basis=basis, size_code=code,
                extent_mil2=max(_extent_area(member[0]) for member in members),
            ), members))
        phase.sort(key=lambda item: (
            item[0].height_mil is None, item[0].height_mil or 0.0, item[0].extent_mil2,
            natural_key(item[1][0][2])))
        for step, members in phase:
            number += 1
            step.number = number
            for part, bom_part, designator in members:
                facts = centroids[part.component]
                centre = facts.centre if facts.centre is not None else facts.reference
                names = _pad_names(pcb, part)
                kind = polarity_class(part, names, bom_part)
                row = Row(
                    step=number, designator=designator, side=part.side, mount=part.mount,
                    centre=(centre[0] - origin[0], centre[1] - origin[1]),
                    reference=(facts.reference[0] - origin[0], facts.reference[1] - origin[1]),
                    rotation=round(part.rotation % 360.0, 2) % 360.0,
                    polarized=kind is not None,
                    pin1=pin1_hint(kind, names, centre, part.side == "bottom"),
                    polarity=kind,
                )
                step.rows.append(row)
                rows.append(row)
            steps.append(step)
    placements = {
        key: {mount: sum(1 for part, _, _ in placed if part.side == key and part.mount == mount)
              for mount in ("smd", "mixed", "tht")}
        for key in SIDES if side in (None, key)
    }
    for names in left_out.values():
        names.sort(key=natural_key)
    return Guide(
        variant=variant.name, grouping="bom_lines" if bom is not None else "footprint_and_comment",
        side=side, origin=origin, first=first, steps=steps, rows=rows,
        left_out={key: names for key, names in left_out.items() if names},
        placements=placements,
    )


# ---------- rendering ----------


def _number(mil: float, to_mm: bool, mm_digits: int, mil_digits: int) -> str:
    digits = mm_digits if to_mm else mil_digits
    value = round(mil * 0.0254 if to_mm else mil, digits) + 0.0  # no "-0.000"
    return f"{value:.{digits}f}"


def _height(step: Step, to_mm: bool) -> str:
    if step.height_mil is None:
        return "?"
    text = _number(step.height_mil, to_mm, 2, 1)
    if step.height_basis == "chip":
        return f"<={text} ({step.size_code})"
    return text


def _rotation(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _length(mil: float, to_mm: bool) -> str:
    text = _number(mil, to_mm, 2, 1)
    return f"{text} {'mm' if to_mm else 'mil'}"


def sequence(guide: Guide) -> list[dict]:
    """The phases in order: side, process, steps and parts."""
    out: list[dict] = []
    for step in guide.steps:
        if out and out[-1]["side"] == step.side and out[-1]["process"] == step.process:
            out[-1]["steps"][1] = step.number
            out[-1]["parts"] += len(step.rows)
        else:
            out.append({"side": step.side, "process": step.process,
                        "steps": [step.number, step.number], "parts": len(step.rows)})
    return out


def _md(text) -> str:
    value = re.sub(r"\s+", " ", "" if text is None else str(text)).strip()
    if len(value) > CELL_CHARS:
        value = value[: CELL_CHARS - 3].rstrip() + "..."
    return value.replace("|", "\\|")


def _phase_title(step: Step) -> str:
    return f"{step.side.capitalize()} side, {_PROCESS_NAMES[step.process]}"


def pin_numbered(guide: Guide) -> int:
    """Diodes and polarized capacitors whose hint names a numbered pin, not the cathode
    or + (PIN_NUMBER_NOTE)."""
    return sum(1 for row in guide.rows if row.polarity in ("diode", "polar")
               and (row.pin1 or "").startswith("pin "))


def left_out_summary(guide: Guide) -> dict:
    out = {}
    for reason, names in guide.left_out.items():
        entry: dict = {"count": len(names), "why": LEFT_OUT_REASONS[reason],
                       "designators": names[:LEFT_OUT_SHOWN]}
        if len(names) > LEFT_OUT_SHOWN:
            entry["more"] = len(names) - LEFT_OUT_SHOWN
        out[reason] = entry
    return out


def markdown(guide: Guide, offset: int, limit: int, to_mm: bool, title: str) -> str:
    """Rows offset..offset+limit as a Markdown checklist; the first page opens with the
    sequence and conventions, the last ends with what was left out."""
    unit = "mm" if to_mm else "mil"
    steps = {step.number: step for step in guide.steps}
    page = guide.rows[offset: offset + limit]
    lines: list[str] = []
    if offset == 0:
        lines.append(f"# {title}")
        lines.append("")
        if guide.first is not None and guide.side is None:
            lines.append(f"- {guide.first.side.capitalize()} side SMD first: "
                         f"{first_side_text(guide.first, to_mm)}.")
        lines += [f"- {rule}" for rule in SEQUENCE_RULES]
        lines.append(
            f"- Coordinates in {unit} from the board origin: Center X, Y as Altium's "
            "pick-and-place file writes it (the middle of the pad centres), Ref X, Y the "
            "footprint's reference point; seen from the top. Pin-1 directions are seen "
            "from the side being assembled (bottom: the board flipped left to right).")
        if pin_numbered(guide):
            lines.append(f"- {PIN_NUMBER_NOTE}")
        if guide.approximate:
            lines.append("- No netlist: steps group parts by footprint and Comment "
                         "(approximate BOM lines; no MPN).")
        lines.append("")
    header = ("| Step | Done | Skipped | Qty | Part | Footprint | "
              + ("" if guide.approximate else "MPN | ")
              + f"Height ({unit}) | Designator | Center X, Y | Ref X, Y | Rot | Pin 1 / polarity |")
    columns = header.count("|") - 1
    rule = "|" + "|".join(["---"] * columns) + "|"
    current = None
    shown_step = None
    for row in page:
        step = steps[row.step]
        phase = (step.side, step.process)
        if phase != current:
            if current is not None:
                lines.append("")
            current = phase
            lines.append(f"## {_phase_title(step)}")
            lines.append("")
            lines.append(header)
            lines.append(rule)
            shown_step = None
        centre = ", ".join(_number(value, to_mm, 3, 1) for value in row.centre)
        reference = ", ".join(_number(value, to_mm, 3, 1) for value in row.reference)
        if shown_step != step.number:
            continued = row is not step.rows[0]
            cells = [f"{step.number}{' (cont.)' if continued else ''}", "[ ]", "[ ]",
                     str(len(step.rows)), _md(step.comment), _md(step.footprint)]
            if not guide.approximate:
                cells.append(_md(step.mpn))
            cells.append(_height(step, to_mm))
            shown_step = step.number
        else:
            cells = ["", "[ ]", "[ ]", "", "", ""] + ([] if guide.approximate else [""]) + [""]
        cells += [_md(row.designator), centre, reference, _rotation(row.rotation),
                  _md(row.pin1 or "")]
        lines.append("| " + " | ".join(cells) + " |")
    if offset + limit >= len(guide.rows) and guide.left_out:
        lines += ["", "## Left out", ""]
        for reason, names in guide.left_out.items():
            shown = ", ".join(_md(name) for name in names[:LEFT_OUT_SHOWN])
            if len(names) > LEFT_OUT_SHOWN:
                shown += f", ... ({len(names) - LEFT_OUT_SHOWN} more)"
            why = LEFT_OUT_REASONS[reason]
            lines.append(f"- {why[0].upper()}{why[1:]} ({len(names)}): {shown}")
    return "\n".join(lines) + "\n"


def csv_columns(to_mm: bool) -> list[str]:
    unit = "mm" if to_mm else "mil"
    return ["Step", "Side", "Process", "Mount", "Quantity", "Comment", "Footprint",
            "Manufacturer", "Manufacturer Part Number", f"Height({unit})", "Height Basis",
            "Designator", f"Center-X({unit})", f"Center-Y({unit})", f"Ref-X({unit})",
            f"Ref-Y({unit})", "Rotation", "Pin 1 / Polarity", "Done", "Skipped"]


def to_csv(guide: Guide, offset: int, limit: int, to_mm: bool) -> str:
    """Rows offset..offset+limit as CSV, one part per row, every column filled (a cell
    that would start a spreadsheet formula gets a leading apostrophe)."""
    steps = {step.number: step for step in guide.steps}
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(csv_columns(to_mm))
    for row in guide.rows[offset: offset + limit]:
        step = steps[row.step]
        height = "" if step.height_mil is None else _number(step.height_mil, to_mm, 2, 1)
        basis = {"body": "3D body", "chip": f"chip {step.size_code} width (at most)",
                 None: "unknown"}[step.height_basis]
        # Numbers as written (a negative coordinate stays a number); text guarded.
        writer.writerow([
            step.number, row.side, _PROCESS_NAMES[step.process], row.mount, len(step.rows),
            _cell(step.comment), _cell(step.footprint), _cell(step.manufacturer),
            _cell(step.mpn), height, basis, _cell(row.designator),
            *(_number(value, to_mm, 4, 2) for value in row.centre),
            *(_number(value, to_mm, 4, 2) for value in row.reference),
            _rotation(row.rotation), row.pin1 or "", "", "",
        ])
    return out.getvalue()


def page(guide: Guide, offset: int, limit: int, fmt: str, to_mm: bool, title: str) -> dict:
    """One page of rows: paging fields and the page as Markdown or CSV text."""
    total = len(guide.rows)
    result: dict = {"rows_total": total, "offset": offset,
                    "has_more": offset + limit < total}
    if result["has_more"]:
        result["next_offset"] = offset + limit
    if fmt == "csv":
        result["csv"] = to_csv(guide, offset, limit, to_mm)
    else:
        result["markdown"] = markdown(guide, offset, limit, to_mm, title)
    return result


def summary(guide: Guide, to_mm: bool) -> dict:
    """The page-independent header: sequence, counts, heights, pin-1 hints, left out."""
    heights = {"body": 0, "chip": 0, "unknown": 0}
    for step in guide.steps:
        heights[step.height_basis or "unknown"] += 1
    polarized = [row for row in guide.rows if row.polarized]
    result: dict = {
        "variant": guide.variant,
        "grouping": guide.grouping,
        "side": guide.side or "both",
        "placements": guide.placements,
        "steps_total": len(guide.steps),
        "parts_total": len(guide.rows),
    }
    if guide.approximate:
        result["grouping_approximate"] = True
    if guide.first is not None and guide.side is None:
        result["first_side"] = guide.first.side
        result["first_side_reason"] = first_side_text(guide.first, to_mm)
    result["sequence"] = sequence(guide)
    result["sequence_rules"] = list(SEQUENCE_RULES)
    result["step_heights"] = heights
    result["polarized_parts"] = {
        "count": len(polarized),
        "with_pin1_hint": sum(1 for row in polarized if row.pin1 != NO_PIN1),
    }
    numbered = pin_numbered(guide)
    if numbered:
        result["polarized_parts"]["diode_or_capacitor_by_pin_number"] = numbered
        result["polarized_parts"]["note"] = PIN_NUMBER_NOTE
    if guide.left_out:
        result["left_out"] = left_out_summary(guide)
    return result
