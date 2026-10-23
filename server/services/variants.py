"""Full variant data: what a variant changes on each part (Not Fitted, varied parameters,
alternate parts), the difference between two variants (compare_variants), and whether
each variant has its own assembly outputs and Draftsman notes.

Altium's four variation types (Variant Management): Fitted, Fitted with varied
parameters, Not Fitted and Alternate Part. The .PrjPcb stores them per variant as
VariationN lines (Kind 0 fitted, 1 Not Fitted, 2 alternate part with AltLibLink_* fields,
keyed by the component's UniqueId) and ParamDesignatorN / ParamVariationN pairs (keyed
by designator); alternate parts themselves are copied into <project>.PrjPcbVariants
(parsers/prj_pcb.py). Variations bind to the loaded parts the way the BOM does
(bom.plan_variant: UniqueId path, its last segment, then the stored designator).

Validated on a private corpus of 53 projects with variants: Not Fitted lines, Kind 0
lines with parameter variations, and Kind 2 alternates that re-select the base part's
own library reference with the same footprint. An alternate with another library
reference or footprint (IsSameFootprint false), a varied Value parameter and variants on
a multi-channel design have no sample: they follow Altium's documentation and the key
shapes seen, unvalidated.
"""
import re
from dataclasses import dataclass, field

try:
    from services.bom import (
        BASE_VARIANT, alternate_lib_reference, alternate_of, natural_key, plan_variant,
        variant_names,
    )
except ImportError:
    from server.services.bom import (
        BASE_VARIANT, alternate_lib_reference, alternate_of, natural_key, plan_variant,
        variant_names,
    )

_MPN_NAME = re.compile(r"^manufacturer part number(?: (\d+))?$")
# Parameters that read as the part's value; the rest are listed as other parameters.
VALUE_PARAMETERS = ("value", "comment")
# Component fields a variant varies by parameter name (ParameterName=Description or
# Footprint, as get_bom applies them): their base values are the part's fields.
FIELD_PARAMETERS = ("description", "footprint")
GROUPS = ("fitted", "values", "parameters", "alternate_parts", "variant_parameters")
FITTED, NOT_FITTED = "fitted", "not fitted"
ROLES = ("bom", "pick_and_place", "assembly_drawing")
LABELS_SHOWN = 5  # outputs listed per role and variant in the documentation check
# Separators allowed between the word "variant" and a variant's name in a note.
_NEXT_TO = r"[\s:=\"'()\[\]‘’“”-]{1,4}"


# ---------- one part in one variant ----------

def _unset(text: str | None) -> bool:
    return text is None or text.strip() in ("", "*")


def _mpn(params: dict[str, str]) -> str | None:
    """The part's MPN as the sheet parser picks it: the lowest-index 'Manufacturer Part
    Number[ N]' (no index first), else Comment."""
    best: tuple[int, str] | None = None
    for name, text in params.items():
        match = _MPN_NAME.match(name)
        if match and text and not text.startswith('"'):
            rank = int(match[1]) if match[1] else 0
            if best is None or rank < best[0]:
                best = (rank, text)
    if best:
        return best[1]
    comment = params.get("comment")
    return comment if comment and not comment.startswith('"') else None


def _library(alternate: dict | None):
    return getattr((alternate or {}).get("variation"), "library_part", None)


def base_params(meta: dict, alternate: dict | None) -> dict[str, str]:
    """A part's parameters before the variant's parameter variations: an alternate
    part's copy in the .PrjPcbVariants file when there is one, else the sheet part's."""
    library = _library(alternate)
    if library is not None:
        return {key: text for key, (_, text) in library.params.items()}
    return dict(meta.get("params") or {})


def base_values(meta: dict, alternate: dict | None) -> dict[str, str]:
    """base_params with the FIELD_PARAMETERS read the way get_bom reads them: the part's
    description (an alternate's: its copy's) and footprint (an alternate's: the
    variant's when it gives one)."""
    library = _library(alternate)
    values = base_params(meta, alternate)
    if library is not None:
        description = library.description or values.get("description")
    else:
        description = meta.get("description") or values.get("description")
    footprint = (alternate or {}).get("footprint") or meta.get("footprint")
    for key, value in (("description", description), ("footprint", footprint)):
        if value:
            values[key] = value
        else:
            values.pop(key, None)
    return values


def _same_text(a: str | None, b: str | None) -> bool:
    return (a or "").strip() == (b or "").strip()


def part_changes(meta: dict, variant_name: str, params: dict[str, tuple[str, str]],
                 alternate: dict | None) -> dict | None:
    """What a variant changes on one loaded part: the fields it changes (value,
    description, mpn, footprint, lib_reference, each {variant, base}), its varied
    parameters whose value differs from the base design's ({parameter, value, base})
    and its alternate part (library reference, library, footprint, with the base
    part's). None when the variant changes nothing (a variation equal to the base value
    changes nothing)."""
    if not params and not alternate:
        return None
    library = _library(alternate)
    base = base_values(meta, None)
    effective = base_values(meta, alternate)
    for key, (_, value) in params.items():
        effective[key] = value
    fields: dict[str, dict] = {}

    def change(name: str, now, before) -> None:
        if (now or None) != (before or None):
            fields[name] = {"variant": now or None, "base": before or None}

    if "value" in params or library is not None:
        change("value", effective.get("value"), meta.get("value"))
    if "description" in params or library is not None:
        change("description", effective.get("description"), meta.get("description"))
    touched = library is not None or any(
        key == "comment" or _MPN_NAME.match(key) for key in params)
    if touched:
        change("mpn", _mpn(effective), meta.get("mpn"))
    if "footprint" in params or (alternate and alternate.get("footprint")):
        change("footprint", effective.get("footprint"), meta.get("footprint"))
    if alternate:
        change("lib_reference", alternate_lib_reference(alternate.get("variation")),
               meta.get("lib_reference"))
    varied = [{"parameter": name, "value": value, "base": base.get(key)}
              for key, (name, value) in sorted(params.items())
              if not _same_text(value, base.get(key))]
    if not fields and not varied and not alternate:
        return None
    out: dict = {"variant": variant_name}
    if fields:
        out["fields"] = fields
    if varied:
        out["varied_parameters"] = varied
    if alternate:
        identity = alternate["variation"].alternate_identity() \
            if alternate.get("variation") is not None else {"lib_reference": alternate.get("part")}
        identity["base_lib_reference"] = meta.get("lib_reference") or None
        identity["base_footprint"] = meta.get("footprint") or None
        out["alternate_part"] = {k: v for k, v in identity.items() if v is not None}
    return out


def active_changes(variant, refdes: str, meta: dict, fitted: bool = True) -> dict | None:
    """part_changes for a part in a VariantDefinition (bound or by stored designator).
    None for a part the variant does not fit: variations stored on it do not apply
    (get_bom and compare_variants report them as stale_variant_entries)."""
    if variant is None or not fitted or not hasattr(variant, "variation_of"):
        return None
    params, alternate = variant.variation_of(refdes)
    if alternate is not None and "part" not in alternate:
        alternate = alternate_of(alternate["variation"])
    return part_changes(meta, variant.name, params, alternate)


def shown_values(variant_state, refdes: str, meta: dict) -> tuple[dict, dict | None]:
    """The MPN, description and value the component tools show for a part in the active
    variant (the base design's where it changes none, or does not fit the part), and
    its active_changes."""
    fitted = variant_state is None or not variant_state.is_dnp(refdes)
    changes = active_changes(getattr(variant_state, "active", None), refdes, meta, fitted)
    fields = (changes or {}).get("fields") or {}
    return ({name: fields[name]["variant"] if name in fields else meta.get(name)
             for name in ("mpn", "description", "value")}, changes)


def sheet_line(changes: dict) -> str:
    """One compact text line for get_sheet_context."""
    parts = []
    for name, change in (changes.get("fields") or {}).items():
        parts.append(f"{name} {change['variant'] or '(none)'} (base {change['base'] or '(none)'})")
    shown = {name.lower() for name in changes.get("fields") or {}}
    for row in changes.get("varied_parameters") or []:
        if row["parameter"].lower() in shown:
            continue
        parts.append(f"{row['parameter']}={row['value']} (base {row['base'] or '(none)'})")
    alternate = changes.get("alternate_part")
    if alternate:
        text = f"alternate part {alternate.get('lib_reference') or '?'}"
        if alternate.get("library"):
            text += f" from {alternate['library']}"
        parts.append(text)
    return f"  variant {changes['variant']}: " + "; ".join(parts)


# ---------- a variant as bound to the parts ----------

@dataclass
class VariantView:
    name: str
    not_fitted: set[str] = field(default_factory=set)
    params: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)
    alternates: dict[str, dict] = field(default_factory=dict)
    parameters: dict[str, str] = field(default_factory=dict)  # variant parameters
    stale: list[dict] = field(default_factory=list)
    base: bool = False


def view_of(variant, netlist: dict | None) -> VariantView:
    """A variant's Not Fitted parts, parameter variations and alternate parts by the
    designator they apply to: bound like the BOM with a netlist, else by the
    designators stored in the .PrjPcb ('?' designators by UniqueId). None: the base."""
    if variant is None:
        return VariantView(name=BASE_VARIANT, base=True)
    names = variant_names(netlist or {})
    parameters = dict(getattr(variant, "parameters", None) or {})
    if names:
        plan = plan_variant(variant, netlist, names)
        return VariantView(name=variant.name, not_fitted=set(plan.not_fitted),
                           params=plan.params, alternates=plan.alternates,
                           parameters=parameters, stale=plan.stale)
    view = VariantView(name=variant.name, parameters=parameters,
                       params={d: dict(p) for d, p in variant.param_variations.items()})
    for line in variant.not_fitted():
        view.not_fitted.add(_stored(line))
    view.not_fitted.update(variant.dnp_refdes)
    for line in variant.variations:
        if line.kind == 2:
            view.alternates[_stored(line)] = alternate_of(line)
    return view


def _stored(line) -> str:
    if line.designator and "?" not in line.designator:
        return line.designator
    return f"{line.designator or '?'} (UniqueId {line.uid_tail})"


def export_rows(variants: list, netlist: dict) -> list[dict]:
    """Rows of package_for_xfn's variant_variations table: per variant, one per varied
    parameter and one per alternate part of each part it fits, bound like the BOM."""
    components = netlist.get("components") or {}
    rows = []
    for variant in variants:
        view = view_of(variant, netlist)
        varied = (set(view.params) | set(view.alternates)) - view.not_fitted
        for designator in sorted(varied, key=natural_key):
            meta = components.get(designator) or {}
            base = base_values(meta, None)
            for key, (name, value) in sorted((view.params.get(designator) or {}).items()):
                rows.append({"variant": view.name, "refdes": designator, "kind": "parameter",
                             "parameter": name, "value": value, "base_value": base.get(key)})
            if designator in view.alternates:
                identity = _identity(view.alternates[designator], meta)
                rows.append({
                    "variant": view.name, "refdes": designator, "kind": "alternate_part",
                    "value": identity.get("lib_reference"),
                    "base_value": meta.get("lib_reference") or None,
                    "data": {key: value for key, value in identity.items()
                             if key not in ("part", "lib_reference")},
                })
    return rows


# ---------- comparing two variants ----------

def _identity(alternate: dict | None, meta: dict) -> dict:
    if alternate is None:
        return {"part": "base", "lib_reference": meta.get("lib_reference") or None,
                "footprint": meta.get("footprint") or None}
    variation = alternate.get("variation")
    identity = variation.alternate_identity() if variation is not None else {
        "lib_reference": alternate.get("part")}
    identity = {"part": "alternate", **identity}
    if not identity.get("footprint"):
        identity["footprint"] = meta.get("footprint") or None
    if meta and identity.get("lib_reference") == meta.get("lib_reference") \
            and identity.get("footprint") == (meta.get("footprint") or None):
        # Re-selecting the base part as its own alternate (the only kind sampled): the
        # same part, with the variant's parameter values.
        identity["same_as_base_part"] = True
    return {key: value for key, value in identity.items() if value is not None}


_IDENTITY_KEYS = ("part", "lib_reference", "design_item_id", "library", "footprint")


def _values_in(view: VariantView, designator: str, meta: dict) -> dict[str, str]:
    """A part's values in a variant: base_values (an alternate's copy) with the
    variant's parameter variations on top. Without a netlist (no meta) only what the
    variant itself gives is known."""
    alternate = view.alternates.get(designator)
    values = base_values(meta, alternate) if meta or _library(alternate) else {}
    for key, (_, value) in (view.params.get(designator) or {}).items():
        values[key] = value
    return values


def _parameter_names(view: VariantView, designator: str, names: dict[str, str]) -> None:
    """Add the parameters a variant gives a part (lower-case name -> name as written):
    its varied ones, and every parameter of an alternate part's library copy, whose
    parameters replace the base part's."""
    for key, (name, _) in (view.params.get(designator) or {}).items():
        names.setdefault(key, name)
    library = _library(view.alternates.get(designator))
    if library is not None:
        for key, (name, _) in library.params.items():
            if key not in FIELD_PARAMETERS:
                names.setdefault(key, name)


def compare(a: VariantView, b: VariantView, netlist: dict | None) -> list[dict]:
    """Every difference between two variants, as rows {group, designator, ...} in GROUPS
    order, designators in natural order: fitted (fitted in one only; the fitted side's
    variations listed), values (Value and Comment), parameters (any other varied
    parameter, Description and Footprint against the part's fields), alternate_parts
    (the part each variant places) and variant_parameters. A part not fitted in both is
    left out; a part fitted in one only is listed under fitted alone. Values a variant
    does not vary are the base part's (an alternate's: its library copy's, whose
    parameters are all compared; None without a netlist)."""
    components = (netlist or {}).get("components") or {}
    designators = set(a.not_fitted) | set(b.not_fitted) | set(a.params) | set(b.params) \
        | set(a.alternates) | set(b.alternates)
    rows: dict[str, list[dict]] = {group: [] for group in GROUPS}
    for designator in sorted(designators, key=natural_key):
        meta = components.get(designator) or {}
        fitted_a = designator not in a.not_fitted
        fitted_b = designator not in b.not_fitted
        if not fitted_a and not fitted_b:
            continue
        if fitted_a != fitted_b:
            row: dict = {"designator": designator, "a": FITTED if fitted_a else NOT_FITTED,
                         "b": FITTED if fitted_b else NOT_FITTED}
            side = a if fitted_a else b
            varied = [f"{name}={value}" for name, value in
                      (side.params.get(designator) or {}).values()]
            if varied:
                row["fitted_with_varied_parameters"] = varied
            if designator in side.alternates:
                row["fitted_as_alternate"] = _identity(side.alternates[designator], meta)
            rows["fitted"].append(row)
            continue
        identity_a = _identity(a.alternates.get(designator), meta)
        identity_b = _identity(b.alternates.get(designator), meta)
        if any(identity_a.get(k) != identity_b.get(k) for k in _IDENTITY_KEYS):
            rows["alternate_parts"].append({"designator": designator, "a": identity_a,
                                            "b": identity_b})
        names: dict[str, str] = {}
        for view in (a, b):
            _parameter_names(view, designator, names)
        values_a = _values_in(a, designator, meta)
        values_b = _values_in(b, designator, meta)
        base = base_values(meta, None) if meta else {}
        for key in sorted(names):
            value_a, value_b = values_a.get(key), values_b.get(key)
            if _same_text(value_a, value_b):
                continue
            row = {"designator": designator, "parameter": names[key], "a": value_a,
                   "b": value_b}
            if meta:
                row["base"] = base.get(key)
            rows["values" if key in VALUE_PARAMETERS else "parameters"].append(row)
    for name in sorted(set(a.parameters) | set(b.parameters), key=str.lower):
        if a.parameters.get(name) != b.parameters.get(name):
            rows["variant_parameters"].append({"parameter": name, "a": a.parameters.get(name),
                                               "b": b.parameters.get(name)})
    return [{"group": group, **row} for group in GROUPS for row in rows[group]]


def unvalidated(rows: list[dict]) -> bool:
    """Whether a comparison shows a kind of variation with no validated sample: a varied
    Value, or an alternate part other than the base part re-selected."""
    def other_part(identity: dict | None) -> bool:
        return bool(identity) and identity.get("part") == "alternate"             and not identity.get("same_as_base_part")

    return any(
        (row["group"] == "values" and row["parameter"].lower() == "value")
        or (row["group"] == "alternate_parts" and (other_part(row["a"]) or other_part(row["b"])))
        or other_part(row.get("fitted_as_alternate"))
        for row in rows)


def group_rows(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in rows:
        row = dict(row)
        out.setdefault(row.pop("group"), []).append(row)
    return out


# ---------- documentation per variant ----------

def _mentions(text: str, name: str) -> bool:
    """Whether a note names the variant: its name next to the word "variant" ("variant
    Lite", "Lite variant", "Variant: Lite"), so a name that is also an ordinary word
    ("Finish") is not counted where it is used as one."""
    if not text or not name:
        return False
    named = rf"(?<![\w-]){re.escape(name)}(?![\w-])"
    word = r"\bvariants?(?: name)?\b"
    return re.search(rf"{word}{_NEXT_TO}{named}|{named}{_NEXT_TO}{word}", text,
                     re.IGNORECASE) is not None


def _item_texts(item) -> list[str]:
    texts = [item.title or "", item.text or "", *item.lines]
    texts.extend(" ".join(row) for row in item.rows)
    return [text for text in texts if text]


def draftsman_variants(drawings: list[tuple[str, object]], names: list[str]) -> dict:
    """Per drawing: its own variant, the variants its views and BOM tables are set to,
    whether a text prints the variant name field, and the variant names its notes,
    texts and tables mention (title-block texts placed by the sheet template are left
    out; a document parameter counts when its name says variant and its value is the
    name). drawings: (file name, DraftsmanDoc or None when unreadable)."""
    out = {}
    for drawing, doc in drawings:
        if doc is None:
            out[drawing] = {"readable": False}
            continue
        items = [item for item in doc.items if item.kind in ("note", "text", "table")]
        notes = [text for item in items if not item.from_template
                 for text in _item_texts(item)]
        parameters = [value.strip().lower() for key, value in doc.parameters.items()
                      if value and "variant" in key.lower()]
        views = sorted({item.variant for item in doc.items
                        if item.kind == "view" and item.variant
                        and not item.variant.startswith("Document")})
        tables = sorted({row.get("variant") for row in doc.bom_data if row.get("variant")})
        out[drawing] = {
            "readable": True,
            "drawing_variant": doc.document_variant or BASE_VARIANT,
            "view_variants": views,
            "bom_table_variants": tables,
            "prints_variant_name": any(re.search(r"variantname", text, re.IGNORECASE)
                                       for item in items for text in _item_texts(item)),
            "mentions": [name for name in names
                         if name.strip().lower() in parameters
                         or any(_mentions(text, name) for text in notes)],
        }
    return out


def documentation(variants: list[str], outputs: list[dict], drawings: dict) -> list[dict]:
    """Per variant: the connected OutJob outputs set to it by role (BOM, pick and place,
    assembly drawing), the roles with none, and the Draftsman drawings set to it or
    whose notes name it (info only)."""
    rows = []
    for name in variants:
        row: dict = {"variant": name}
        missing = []
        for role in ROLES:
            labels = [f"{o['outjob']}: {o['output']}" for o in outputs
                      if o["role"] == role and o["connected"]
                      and same_variant(o["variant"], name)]
            if labels:
                row[role] = labels[:LABELS_SHOWN]
                if len(labels) > LABELS_SHOWN:
                    row[f"{role}_count"] = len(labels)
            else:
                missing.append(role)
        row["outputs_missing"] = missing
        on_drawing = [drawing for drawing, info in drawings.items() if info.get("readable")
                      and (same_variant(info["drawing_variant"], name)
                           or name in info["view_variants"]
                           or name in info["bom_table_variants"])]
        mentioned = [drawing for drawing, info in drawings.items()
                     if name in (info.get("mentions") or [])]
        if drawings:
            row["draftsman_set_to_variant"] = on_drawing
            row["draftsman_notes_mention"] = mentioned
        rows.append(row)
    return rows


def same_variant(a: str | None, b: str | None) -> bool:
    """Whether two variant names are the same variant, case-insensitively; empty and
    [All Variants] (an OutJob setting) read as the base design."""
    def norm(name):
        return BASE_VARIANT.lower() if (name or "").strip().lower() in (
            "", BASE_VARIANT.lower(), "[all variants]") else (name or "").strip().lower()
    return norm(a) == norm(b)
