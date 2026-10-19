"""Bill of materials per variant, from the compiled part list and the sheets' parameters.

The parts are the compiled ones (netlist['parts'], from the Protel .NET; compile-masked
parts are not in it), with their physical designators. Altium leaves out Graphical,
Net Tie (No BOM) and Standard (No BOM) parts, and drops a variant's Not Fitted parts;
its parameter variations replace the part's values. Altium matches a variation to its
component by UniqueId, so the designator stored with it is only a fallback.

The manufacturer part number comes from the parts' parameters ('Manufacturer Part
Number 1' and the spellings libraries use for it), never from Comment: Comment is its own
column, with special strings such as '=Value' resolved.
"""

import csv
import io
import re
from dataclasses import dataclass, field

# ComponentKind values that stay in the BOM: Standard, Mechanical, Net Tie (In BOM),
# Jumper. Out: Graphical (2), Net Tie (4), Standard (No BOM) (5).
BOM_KINDS = {0, 1, 3, 6}
KIND_NAMES = {
    0: "standard", 1: "mechanical", 2: "graphical", 3: "net tie (in BOM)",
    4: "net tie", 5: "standard (no BOM)", 6: "jumper",
}
BASE_VARIANT = "[No Variations]"
LINE_LIMIT = 100
_HOPS = 4

_MANUFACTURER = r"(?:manufacturer|manufact|manuf|mfr|mfg)"
_MPN_NAME = re.compile(
    rf"^{_MANUFACTURER}\.? ?(?:part ?(?:number|num|no\.?|#)?|pn|p/n|#) ?(\d*)$"
)
_MFR_NAME = re.compile(rf"^{_MANUFACTURER}\.?(?: ?name)? ?(\d*)$")
_PART_NUMBER = re.compile(r"^part ?(?:number|no\.?|#)(?: ?\d+)?$")
_NATURAL = re.compile(r"(\d+)")


def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.replace("_", " ")).strip().lower()


def _unset(text: str | None) -> bool:
    return text is None or text.strip() in ("", "*")


def resolve(text: str | None, params: dict[str, str]) -> tuple[str | None, bool]:
    """Resolve an '=Name' special string against the part's parameters (a few hops).
    Returns (text, resolved); unresolvable text comes back as written."""
    hops = 0
    while text and text.startswith("=") and hops < _HOPS:
        name = text[1:].strip().strip('"').lower()
        if name not in params:
            return text, False
        text, hops = params[name], hops + 1
    if text and text.startswith("="):
        return text, False
    return text, True


def part_numbers(params: dict[str, str]) -> tuple[str | None, str | None, str | None]:
    """(mpn, manufacturer, source): the lowest-index 'Manufacturer Part Number N' (in the
    spellings libraries use) with the manufacturer of the same index (or an unnumbered
    one); else a generic 'Part Number' (source 'part number')."""
    mpns: dict[int, str] = {}
    makers: dict[int, str] = {}
    generic = None
    for name, text in params.items():
        if _unset(text):
            continue
        normal = _norm_name(name)
        match = _MPN_NAME.match(normal)
        if match:
            mpns.setdefault(int(match[1] or 0), text.strip())
            continue
        match = _MFR_NAME.match(normal)
        if match:
            makers.setdefault(int(match[1] or 0), text.strip())
            continue
        if _PART_NUMBER.match(normal) and generic is None:
            generic = text.strip()
    if mpns:
        index = min(mpns)
        maker = makers.get(index) or makers.get(0) or (makers.get(1) if index == 0 else None)
        return mpns[index], maker, "parameter"
    if generic:
        return generic, makers.get(0) or makers.get(1), "part number"
    return None, (makers.get(0) or makers.get(1)), None


def natural_key(designator: str):
    return [int(part) if part.isdigit() else part.lower()
            for part in _NATURAL.split(designator)]


@dataclass
class BomPart:
    designator: str
    kind: int = 0
    comment: str | None = None
    value: str | None = None
    description: str | None = None
    footprint: str | None = None
    mpn: str | None = None
    manufacturer: str | None = None
    mpn_source: str | None = None
    fitted: bool = True
    logical: str | None = None
    sheet: str | None = None
    lib_reference: str = ""
    overrides: dict[str, str] = field(default_factory=dict)  # field -> varied value
    unresolved: list[str] = field(default_factory=list)  # special strings left as written


@dataclass
class VariantPlan:
    """A variant resolved against the compiled parts."""

    name: str
    not_fitted: set[str] = field(default_factory=set)
    params: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)
    stale: list[dict] = field(default_factory=list)  # variation entries that do not fit


def plan_variant(variant, netlist: dict, names: list[str]) -> VariantPlan:
    """Bind a variant's entries to compiled parts: by full UniqueId path, then its last
    segment, then the designator stored with it (Altium follows the UniqueId)."""
    if variant is None or variant.name == BASE_VARIANT:
        return VariantPlan(name=BASE_VARIANT)
    components = netlist.get("components") or {}
    known = set(names)
    by_path: dict[str, list[str]] = {}
    by_tail: dict[str, list[str]] = {}
    for name in names:
        meta = components.get(name) or {}
        for path in meta.get("unique_paths") or []:
            by_path.setdefault(path, []).append(name)
        tails = list(meta.get("unique_ids") or [])
        if meta.get("unique_id") and meta["unique_id"] not in tails:
            tails.append(meta["unique_id"])
        for tail in tails:
            by_tail.setdefault(tail, []).append(name)
    plan = VariantPlan(name=variant.name)
    for variation in getattr(variant, "variations", []) or []:
        targets = by_path.get(variation.unique_id) or []
        if not targets and variation.uid_tail:
            candidates = by_tail.get(variation.uid_tail) or []
            # Channel copies share the tail: the stored designator picks one if it can.
            targets = (
                [variation.designator] if variation.designator in candidates else candidates
            )
        if not targets and variation.designator in known:
            targets = [variation.designator]
            if variation.unique_id:
                plan.stale.append({
                    "designator": variation.designator, "issue": "unique_id_not_found",
                    "detail": "matched by designator; its UniqueId is on no compiled part",
                })
        elif targets and variation.designator and variation.designator not in targets \
                and "?" not in variation.designator:
            plan.stale.append({
                "designator": variation.designator, "issue": "designator_changed",
                "detail": f"its UniqueId is on {', '.join(sorted(targets, key=natural_key))} "
                f"now; the stored designator is stale",
            })
        if not targets:
            plan.stale.append({
                "designator": variation.designator or variation.unique_id,
                "issue": "no_such_part",
                "detail": "the variation names no compiled part (deleted or renamed)",
            })
            continue
        if variation.kind == 1:
            plan.not_fitted.update(targets)
    for designator, params in (getattr(variant, "param_variations", {}) or {}).items():
        if designator not in known:
            plan.stale.append({
                "designator": designator, "issue": "no_such_part",
                "detail": "a parameter variation names no compiled part",
            })
            continue
        plan.params[designator] = params
    for designator in sorted(set(plan.params) & plan.not_fitted, key=natural_key):
        plan.stale.append({
            "designator": designator, "issue": "varied_but_not_fitted",
            "detail": "parameter variations on a part the variant does not fit",
        })
    return plan


def build_parts(netlist: dict, plan: VariantPlan | None = None) -> tuple[list[BomPart], dict]:
    """The variant's BOM parts, and what was left out (by kind, not fitted)."""
    plan = plan or VariantPlan(name=BASE_VARIANT)
    compiled = netlist.get("parts") or {}
    components = netlist.get("components") or {}
    names = list(compiled) if compiled else list(components)
    parts: list[BomPart] = []
    excluded: dict[str, list[str]] = {}
    for name in names:
        meta = components.get(name) or {}
        kind = int(meta.get("kind") or 0)
        if kind not in BOM_KINDS:
            excluded.setdefault(KIND_NAMES.get(kind, str(kind)), []).append(name)
            continue
        params = dict(meta.get("params") or {})
        overrides: dict[str, str] = {}
        varied = plan.params.get(name) or {}
        for key, (original, value) in varied.items():
            params[key] = value
            overrides[original] = value
        unresolved: list[str] = []

        def text(raw):
            value, ok = resolve(raw, params)
            if not ok:
                unresolved.append(raw)
            return None if _unset(value) else value.strip()

        block = compiled.get(name) or {}
        comment = text(params.get("comment")) if "comment" in params else (
            block.get("comment") or None
        )
        mpn, manufacturer, source = part_numbers(params)
        # A variant can vary the footprint and description, which are not parameters.
        if "description" in varied:
            description = text(params["description"])
        else:
            description = meta.get("description") or text(params.get("description"))
        footprint = (
            text(params["footprint"]) if "footprint" in varied
            else block.get("footprint") or meta.get("footprint")
        )
        part = BomPart(
            designator=name,
            kind=kind,
            comment=comment,
            value=text(params.get("value")),
            description=description,
            footprint=footprint,
            mpn=mpn,
            manufacturer=manufacturer,
            mpn_source=source,
            fitted=name not in plan.not_fitted,
            logical=meta.get("logical_refdes"),
            sheet=meta.get("sheet"),
            lib_reference=meta.get("lib_reference") or "",
            overrides=overrides,
            unresolved=unresolved,
        )
        parts.append(part)
    parts.sort(key=lambda part: natural_key(part.designator))
    return parts, excluded


def _fold(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def group_key(part: BomPart, group_by: str) -> tuple:
    """MPN (with manufacturer) when the part has one, else Comment and footprint. This
    reproduced the grouping of 37 of 41 BOMs exported from real projects; 'comment'
    groups by Comment and footprint only, close to Altium's default."""
    if group_by == "mpn" and part.mpn:
        return ("mpn", _fold(part.mpn), _fold(part.manufacturer))
    return ("comment", _fold(part.comment or part.value), _fold(part.footprint))


_LINE_FIELDS = ("comment", "description", "value", "footprint", "manufacturer", "mpn")


def group_lines(parts: list[BomPart], group_by: str = "mpn") -> list[dict]:
    """BOM lines: quantity, designators (natural order) and the members' fields; a
    field the members disagree on is listed in 'varies'."""
    groups: dict[tuple, list[BomPart]] = {}
    for part in parts:
        groups.setdefault(group_key(part, group_by), []).append(part)
    lines = []
    for members in groups.values():
        first = members[0]
        line: dict = {
            "quantity": len(members),
            "designators": [part.designator for part in members],
        }
        varies = []
        for name in _LINE_FIELDS:
            values = [getattr(part, name) for part in members]
            present = [value for value in values if value]
            if present:
                line[name] = present[0]
            if len({_fold(value) for value in values}) > 1:
                varies.append(name)
        if first.mpn_source and first.mpn_source != "parameter":
            line["mpn_source"] = first.mpn_source
        if varies:
            line["varies"] = varies
        logical = [part.logical for part in members if part.logical]
        if logical:
            line["logical_designators"] = logical
        overridden = sorted({key for part in members for key in part.overrides})
        if overridden:
            line["varied_parameters"] = overridden
        lines.append(line)
    lines.sort(key=lambda line: natural_key(line["designators"][0]))
    return lines


def compare_variants(netlist: dict, variants: list, names: list[str]) -> list[dict]:
    """What each variant changes against the BOM with no variations: parts not fitted
    and parameter values changed."""
    base, _ = build_parts(netlist)
    by_name = {part.designator: part for part in base}
    out = []
    for variant in variants:
        plan = plan_variant(variant, netlist, names)
        parts, _ = build_parts(netlist, plan)
        changed = []
        for part in parts:
            if not part.fitted:
                continue
            before = by_name.get(part.designator)
            fields = {
                name: {"from": getattr(before, name), "to": getattr(part, name)}
                for name in _LINE_FIELDS
                if before is not None and _fold(getattr(before, name)) != _fold(getattr(part, name))
            }
            if fields:
                changed.append({"designator": part.designator, **fields})
        out.append({
            "variant": variant.name,
            "not_fitted": sorted(plan.not_fitted, key=natural_key),
            "changed": changed,
        })
    return out


CSV_COLUMNS = [
    ("Comment", "comment"), ("Description", "description"), ("Designator", "designators"),
    ("Footprint", "footprint"), ("Quantity", "quantity"), ("Manufacturer", "manufacturer"),
    ("Manufacturer Part Number", "mpn"), ("Value", "value"),
]


def to_csv(lines: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow([title for title, _ in CSV_COLUMNS])
    for line in lines:
        writer.writerow([
            ", ".join(line[key]) if key == "designators" else line.get(key, "")
            for _, key in CSV_COLUMNS
        ])
    return out.getvalue()
