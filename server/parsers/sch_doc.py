import logging
import re
import struct
import zlib
from dataclasses import dataclass, field

import olefile

_MPN_RE = re.compile(r"^Manufacturer Part Number( (\d+))?$", re.IGNORECASE)


@dataclass
class ComponentMeta:
    refdes: str = ""
    description: str | None = None
    mpn: str | None = None
    value: str | None = None
    unique_id: str = ""
    pins: dict[str, str] = field(default_factory=dict)  # pin_number -> pin_name
    # Every part's UniqueID: parts placed separately keep their own, and the PCB
    # carries only one of them (SOURCEUNIQUEID).
    unique_ids: list[str] = field(default_factory=list)
    # The current PCB footprint (RECORD=45 PCBLIB model with IsCurrent) and every
    # footprint model the part offers, in file order.
    footprint: str | None = None
    footprints: list[str] = field(default_factory=list)
    # ComponentKind: 0 Standard, 1 Mechanical, 2 Graphical, 3 Net Tie (In BOM),
    # 4 Net Tie, 5 Standard (No BOM), 6 Jumper.
    kind: int = 0
    # Every pin designator, unnamed pins included (pins drops those).
    pin_designators: list[str] = field(default_factory=list)


@dataclass
class DesignatorClash:
    """Two or more different components drawn with one designator on a sheet."""
    refdes: str
    # Each component's LibReference in file order; the last one is the one kept.
    lib_references: list[str] = field(default_factory=list)


def parse_sch_doc(
    path: str, clashes: list[DesignatorClash] | None = None
) -> dict[str, ComponentMeta]:
    """Parse an Altium .SchDoc OLE file and return component metadata keyed by refdes.

    Designators that name two or more different components are appended to clashes.
    """
    with olefile.OleFileIO(path) as f:
        return components_from_stream(
            f.openstream("FileHeader").read(), source=path, clashes=clashes
        )


def components_from_stream(
    data: bytes, source: str = "", clashes: list[DesignatorClash] | None = None
) -> dict[str, ComponentMeta]:
    """parse_sch_doc for the bytes of a FileHeader stream.

    The OwnerIndex field in child records (pins, designators, parameters) refers to
    the 0-based sequential record number of the parent RECORD=1 minus 1, i.e. the
    record immediately preceding the component record in the file stream.

    source names the sheet in logged warnings. Designators that name two or more
    different components are appended to clashes, see _merge_parts.
    """
    # Parsing state
    # Key: owner_key = record_num - 1 for the RECORD=1 entry
    components_by_owner: dict[int, ComponentMeta] = {}
    # Map owner_key of duplicate parts (same UniqueID) to the canonical owner_key
    canonical_owner: dict[int, int] = {}   # any owner_key -> canonical owner_key
    uid_seen: dict[str, int] = {}          # UniqueID -> canonical owner_key
    best_mpn_priority: dict[int, int] = {} # canonical owner_key -> lowest suffix seen (0 = best)
    parts: dict[int, _Part] = {}           # canonical owner_key -> which part it is
    model_lists: dict[int, int] = {}       # RECORD=44 owner_key -> its component's owner_key

    for record_num, props in enumerate(_read_records(data)):
        record_type = props.get("record")
        # owner_key for a RECORD=1 at record_num is (record_num - 1),
        # matching the OwnerIndex used by child records.
        if record_type == "1":
            _handle_component(
                props, record_num, components_by_owner, canonical_owner, uid_seen, parts
            )
        elif record_type == "34":
            _handle_designator(props, components_by_owner, canonical_owner)
        elif record_type == "2":
            _handle_pin(props, components_by_owner, canonical_owner)
        elif record_type == "41":
            _handle_parameter(
                props, components_by_owner, canonical_owner, best_mpn_priority
            )
        elif record_type == "44":
            # The component's implementation list; its models (RECORD=45) own to it.
            key = _owner_key(props, canonical_owner)
            if key is not None:
                model_lists[record_num - 1] = key
        elif record_type == "45":
            _handle_model(props, components_by_owner, model_lists)
        # RECORD=17 (power ports) intentionally skipped

    return _merge_parts(components_by_owner, parts, best_mpn_priority, source, clashes)


@dataclass
class _Part:
    lib_reference: str  # LibReference, the library symbol it was placed from
    part_id: str        # CurrentPartId: 1 = part A, 2 = part B, ...
    part_count: str     # PartCount: number of parts + 1


def _part_number(part_id: str) -> int:
    return int(part_id) if part_id.isdigit() else 0


class _Component:
    """A component being assembled from its parts, see _merge_parts."""

    def __init__(self, meta: ComponentMeta, part: _Part, mpn_priority: int):
        self.meta = meta
        self.first = part
        self.part_ids = {part.part_id}
        self.top_part = _part_number(part.part_id)  # the part whose UniqueID meta carries
        self.mpn_priority = mpn_priority

    def can_take(self, part: _Part) -> bool:
        return part.part_id not in self.part_ids and part.part_count == self.first.part_count

    def add(self, meta: ComponentMeta, part: _Part, mpn_priority: int) -> None:
        self.part_ids.add(part.part_id)
        for pin, name in meta.pins.items():
            self.meta.pins.setdefault(pin, name)
        _merge_part_lists(self.meta, meta)
        self.meta.description = self.meta.description or meta.description
        self.meta.value = self.meta.value or meta.value
        if meta.mpn and (not self.meta.mpn or mpn_priority < self.mpn_priority):
            self.meta.mpn, self.mpn_priority = meta.mpn, mpn_priority
        if _part_number(part.part_id) > self.top_part:
            self.meta.unique_id, self.top_part = meta.unique_id, _part_number(part.part_id)


def _merge_part_lists(target: ComponentMeta, other: ComponentMeta) -> None:
    """Unite the identity and model lists of two parts of one component."""
    for name in ("unique_ids", "footprints", "pin_designators"):
        held = getattr(target, name)
        held.extend(item for item in getattr(other, name) if item not in held)
    target.footprint = target.footprint or other.footprint
    target.kind = target.kind or other.kind


def _merge_parts(
    components_by_owner: dict[int, ComponentMeta],
    parts: dict[int, _Part],
    best_mpn_priority: dict[int, int],
    source: str,
    clashes: list[DesignatorClash] | None = None,
) -> dict[str, ComponentMeta]:
    """One ComponentMeta per designator, the parts of a multi-part component merged.

    Each part of a multi-part component is its own record. In older sheets the parts
    share a UniqueID and are merged while reading; in newer ones (most of Altium's AD25
    examples) each part has its own, and only the designator ties them together.
    Parts with the same designator, different part ids and the same part count merge:
    pins are united, the first non-empty description and value and the best MPN are
    kept, and unique_id is the highest part's, the one Altium gives the component on
    the PCB (SOURCEUNIQUEID). A difference in LibReference is logged but merged, since
    parts of one component can be placed from different libraries. A part that cannot
    belong to the component holding its designator (a part id seen before, or another
    part count) is another component with the same designator: it replaces the earlier
    one, as before, and the clash is logged and appended to clashes, one entry per
    designator. Unannotated parts (U?) share a designator and are neither merged nor
    logged.
    """
    components: dict[str, _Component] = {}
    clashed: dict[str, DesignatorClash] = {}
    for key, meta in components_by_owner.items():
        refdes, part = meta.refdes, parts[key]
        if not refdes:
            continue
        held = components.get(refdes)
        if held and "?" not in refdes:
            if held.can_take(part):
                if part.lib_reference != held.first.lib_reference:
                    logging.warning(
                        "%s: the parts of %s come from different library references "
                        "(%s and %s); merged as one component", source or "SchDoc", refdes,
                        held.first.lib_reference, part.lib_reference,
                    )
                held.add(meta, part, best_mpn_priority.get(key, 999))
                continue
            logging.warning(
                "%s: %s is the designator of two different components (%s and %s); "
                "using the later one", source or "SchDoc", refdes,
                held.first.lib_reference, part.lib_reference,
            )
            if refdes not in clashed:
                clashed[refdes] = DesignatorClash(refdes, [held.first.lib_reference])
                if clashes is not None:
                    clashes.append(clashed[refdes])
            clashed[refdes].lib_references.append(part.lib_reference)
        components[refdes] = _Component(meta, part, best_mpn_priority.get(key, 999))
    return {refdes: held.meta for refdes, held in components.items()}


def _parse_props(text: str) -> dict[str, str]:
    """`|KEY=VALUE|...` as a dict with lower-case keys.

    Altium writes the same key as OwnerIndex in some sheets and OWNERINDEX in others
    (most of its AD25 examples), so keys are matched case-insensitively.
    """
    props: dict[str, str] = {}
    for part in text.split("|"):
        if "=" in part:
            key, _, val = part.partition("=")
            key = key.strip().lower()
            val = val.strip()
            if key.startswith("%utf8%"):
                # The record was decoded as latin-1, so %UTF8% values are
                # latin-1 bytes that are actually UTF-8. Re-encode→decode to
                # get the correct Unicode (e.g. "ÂµF" → "µF").
                try:
                    val = val.encode("latin-1").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
                props[key[6:]] = val
            elif key not in props:
                props[key] = val
    return props


def _read_records(data: bytes) -> list[dict[str, str]]:
    """Split a schematic stream into property dicts with lower-case keys.

    Each record is a 4-byte header (24-bit length, then a type byte, 0 = text) and a
    `|KEY=VALUE|...` body. Binary records become empty dicts so that list positions,
    which OwnerIndex refers to, stay aligned. Position 0 is the stream header, and a
    child's OwnerIndex is its owner's position minus one.
    """
    records: list[dict[str, str]] = []
    offset = 0
    while offset + 4 <= len(data):
        header = struct.unpack_from("<I", data, offset)[0]
        length = header & 0xFFFFFF
        body = data[offset + 4 : offset + 4 + length]
        offset += 4 + length
        if header >> 24:
            records.append({})
            continue
        text = body.decode("latin-1").rstrip("\x00")
        records.append(_parse_props(text))
    return records


def _owner_key(props: dict[str, str], canonical_owner: dict[int, int]) -> int | None:
    """The canonical owner_key a child record belongs to, or None if it has no owner."""
    try:
        owner = int(props.get("ownerindex", ""))
    except ValueError:
        return None
    return canonical_owner.get(owner, owner)


def _handle_component(
    props: dict[str, str],
    record_num: int,
    components_by_owner: dict[int, ComponentMeta],
    canonical_owner: dict[int, int],
    uid_seen: dict[str, int],
    parts: dict[int, _Part],
) -> None:
    # The owner_key used by child records equals record_num - 1
    owner_key = record_num - 1
    uid = props.get("uniqueid", "")
    desc = props.get("componentdescription") or None

    if uid and uid in uid_seen:
        # Subsequent part of a multi-part component — map to canonical owner_key
        canonical_owner[owner_key] = uid_seen[uid]
    else:
        comp = ComponentMeta(
            description=desc,
            unique_id=uid,
            unique_ids=[uid] if uid else [],
            kind=_component_kind(props),
        )
        components_by_owner[owner_key] = comp
        canonical_owner[owner_key] = owner_key
        parts[owner_key] = _Part(
            lib_reference=props.get("libreference", ""),
            part_id=props.get("currentpartid", ""),
            part_count=props.get("partcount", ""),
        )
        if uid:
            uid_seen[uid] = owner_key


def _handle_designator(
    props: dict[str, str],
    components_by_owner: dict[int, ComponentMeta],
    canonical_owner: dict[int, int],
) -> None:
    if props.get("name", "").lower() != "designator":
        return
    key = _owner_key(props, canonical_owner)
    text = props.get("text", "")
    if key is None or not text:
        return
    if key in components_by_owner:
        components_by_owner[key].refdes = text


def _handle_pin(
    props: dict[str, str],
    components_by_owner: dict[int, ComponentMeta],
    canonical_owner: dict[int, int],
) -> None:
    key = _owner_key(props, canonical_owner)
    name = props.get("name", "")
    designator = props.get("designator", "")
    if key is None or not designator or key not in components_by_owner:
        return
    component = components_by_owner[key]
    if designator not in component.pin_designators:
        component.pin_designators.append(designator)
    if not name or name == "?":
        return
    component.pins[designator] = name


def _component_kind(props: dict[str, str]) -> int:
    """ComponentKind, where ComponentKindVersion2 (e.g. 5, Standard (No BOM)) wins."""
    for key in ("componentkindversion2", "componentkind"):
        value = props.get(key, "")
        if value.isdigit():
            return int(value)
    return 0


def _handle_model(
    props: dict[str, str],
    components_by_owner: dict[int, ComponentMeta],
    model_lists: dict[int, int],
) -> None:
    """A RECORD=45 model: PCBLIB models are the part's footprints."""
    if props.get("modeltype", "").upper() != "PCBLIB":
        return
    try:
        key = model_lists.get(int(props.get("ownerindex", "")))
    except ValueError:
        return
    name = props.get("modelname", "")
    if key is None or key not in components_by_owner or not name:
        return
    component = components_by_owner[key]
    if name not in component.footprints:
        component.footprints.append(name)
    if props.get("iscurrent", "").upper() == "T" and not component.footprint:
        component.footprint = name


def _handle_parameter(
    props: dict[str, str],
    components_by_owner: dict[int, ComponentMeta],
    canonical_owner: dict[int, int],
    best_mpn_priority: dict[int, int],
) -> None:
    key = _owner_key(props, canonical_owner)
    # Parameter names are case-insensitive in Altium ("Value" and "VALUE" both occur).
    name = props.get("name", "").lower()
    text = props.get("text", "")
    if key is None or not text or text.startswith('"'):
        return
    if key not in components_by_owner:
        return
    comp = components_by_owner[key]

    if name == "value":
        comp.value = text
        return

    m = _MPN_RE.match(name)
    if m:
        # Priority: no suffix (0) beats suffix 1 beats suffix 2 etc.
        priority = int(m.group(2)) if m.group(2) else 0
        if priority < best_mpn_priority.get(key, 999):
            comp.mpn = text
            best_mpn_priority[key] = priority
        return

    # Fall back to Comment as MPN when no Manufacturer Part Number exists.
    # Comment is used as the primary part number in many Altium libraries.
    if name == "comment" and best_mpn_priority.get(key, 999) == 999:
        comp.mpn = text


# ---------- design annotations: notes, text frames, labels, images, directives ----------

# Schematic coordinates are in units of 10 mil; a *_Frac field adds 1/100000 of a unit.
_UNIT_MIL = 10.0
NEAR_COMPONENT_MIL = 800.0  # text this close to a component's pins is reported as near it
CAPTION_MIL = 250.0         # text this close to a picture is reported as its caption
_MAX_NEAR = 4

# Sheet parameters Altium fills in by itself, not information a designer entered.
_AUTO_SHEET_PARAMS = {
    "currenttime", "currentdate", "time", "date", "documentfullpathandname",
    "documentname", "modifieddate", "application_buildnumber", "spicemodelcache",
    "configurationparameters", "configuratorname", "isuserconfigurable", "pagesetup",
}
# A label holding only a special string (e.g. "=Title") repeats a title-block parameter.
_SPECIAL_STRING_RE = re.compile(r"^=[\w.]+$")
_TEXT_KINDS = {"209": "note", "28": "text_frame", "4": "label"}
# Text frames and notes escape their text: ~1 is a line break, ~2 a '|', ~~ a '~'.
_TEXT_ESCAPES = {"1": "\n", "2": "|", "~": "~"}
_TEXT_ESCAPE_RE = re.compile(r"~([12~])")
# Altium draws schematic font size N at about 0.646 N points (checked against its own
# 1:1 PDF export); an average character is about half an em wide.
_FONT_SIZE_MIL = 0.646 * 1000 / 72
_CHAR_WIDTH_EM = 0.5


@dataclass
class SheetAnnotation:
    kind: str                       # "note" | "text_frame" | "label" | "image"
    text: str = ""
    x_mil: float = 0.0              # lower-left corner; Altium's Y axis points up
    y_mil: float = 0.0
    width_mil: float = 0.0
    height_mil: float = 0.0
    author: str | None = None       # notes
    collapsed: bool = False         # notes folded to an icon on the sheet
    file_name: str | None = None    # images: embedded picture name or linked file path
    embedded: bool = False          # images: picture data is stored in the .SchDoc
    image_index: int | None = None  # images: index for get_design_image, in file order
    near: list[str] = field(default_factory=list)      # refdes of components close by
    captions: list[str] = field(default_factory=list)  # images: text placed next to them


@dataclass
class SheetDirective:
    name: str
    x_mil: float
    y_mil: float
    parameters: dict[str, str] = field(default_factory=dict)


@dataclass
class SheetAnnotations:
    items: list[SheetAnnotation] = field(default_factory=list)
    directives: list[SheetDirective] = field(default_factory=list)
    title_block: dict[str, str] = field(default_factory=dict)
    template: str | None = None
    # Every sheet-level parameter as stored, for resolving special strings ("=Revision").
    sheet_parameters: dict[str, str] = field(default_factory=dict)


def _coord(props: dict[str, str], key: str) -> float:
    try:
        whole = int(props.get(key, "0"))
        frac = int(props.get(key + "_frac", "0"))
    except ValueError:
        return 0.0
    return (whole + frac / 100_000) * _UNIT_MIL


def _rect(props: dict[str, str]) -> list[float]:
    """[x0, y0, x1, y1] in mil from Location and Corner."""
    x0, y0 = _coord(props, "location.x"), _coord(props, "location.y")
    x1, y1 = _coord(props, "corner.x"), _coord(props, "corner.y")
    return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]


def _label_rect(props: dict[str, str], text: str, font_sizes: dict[str, int]) -> list[float]:
    """Approximate extent of a free text label; Altium stores only its anchor point.

    The box is sized from the font size and placed by Justification (0..8 = bottom/
    center/top x left/center/right) in the text's own frame, then turned about the
    anchor by Orientation x 90 degrees counter-clockwise.
    """
    x, y = _coord(props, "location.x"), _coord(props, "location.y")
    em = font_sizes.get(props.get("fontid", "1"), 10) * _FONT_SIZE_MIL
    lines = text.split("\n")
    width, height = max(len(line) for line in lines) * em * _CHAR_WIDTH_EM, em * len(lines)
    try:
        justification = int(props.get("justification", "0"))
        turns = int(props.get("orientation", "0")) % 4
    except ValueError:
        justification, turns = 0, 0
    x0, y0 = -width * (justification % 3) / 2, -height * (justification // 3) / 2
    corners = [(x0, y0), (x0 + width, y0), (x0, y0 + height), (x0 + width, y0 + height)]
    for _ in range(turns):
        corners = [(-cy, cx) for cx, cy in corners]
    xs = [x + cx for cx, _ in corners]
    ys = [y + cy for _, cy in corners]
    return [min(xs), min(ys), max(xs), max(ys)]


def _gap(a: list[float], b: list[float]) -> float:
    """Edge-to-edge distance between two rectangles (0 when they overlap)."""
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    return (dx * dx + dy * dy) ** 0.5


def _owner_position(props: dict[str, str]) -> int | None:
    """Position of the owning record, or None for objects placed on the sheet itself."""
    try:
        owner = int(props.get("ownerindex", ""))
    except ValueError:
        return None
    return owner + 1 if owner > 0 else None


def parse_sch_doc_annotations(path: str) -> SheetAnnotations:
    """Read the information a designer added to a .SchDoc besides the circuit itself.

    Returns sheet-level notes (RECORD=209), text frames (28), free text labels (4),
    pictures (30), directives / parameter sets (43 with their RECORD=41 children), the
    title-block (sheet) parameters (RECORD=41 without an owner) and the sheet template
    (39). Text and pictures that belong to a component symbol or to the template are
    skipped. Each text lists the components whose pins are close by, and each picture
    the text placed next to it.
    """
    with olefile.OleFileIO(path) as f:
        return annotations_from_stream(f.openstream("FileHeader").read())


def annotations_from_stream(data: bytes) -> SheetAnnotations:
    """parse_sch_doc_annotations for the bytes of a FileHeader stream."""
    records = _read_records(data)
    result = SheetAnnotations()
    rects: dict[int, list[float]] = {}           # id(annotation) -> rectangle
    part_boxes: dict[int, list[float]] = {}      # component part position -> pin bounding box
    part_modes: dict[int, tuple[str, str]] = {}  # position -> (CurrentPartId, DisplayMode)
    designators: dict[int, str] = {}
    directives: dict[int, SheetDirective] = {}
    font_sizes: dict[str, int] = {}
    image_count = 0

    for pos, props in enumerate(records):
        record = props.get("record")
        owner = _owner_position(props)
        if record == "31":  # sheet settings, including the font table
            for key, val in props.items():
                if key.startswith("size") and key[4:].isdigit() and val.isdigit():
                    font_sizes[key[4:]] = int(val)
            continue
        if record == "1":
            x, y = _coord(props, "location.x"), _coord(props, "location.y")
            part_boxes[pos] = [x, y, x, y]
            part_modes[pos] = (props.get("currentpartid", "1"), props.get("displaymode", "0"))
            continue
        if owner is not None:
            if record == "2" and owner in part_boxes:
                # A placed part carries the pins of every part; only its own are drawn.
                part, mode = part_modes[owner]
                if (props.get("ownerpartid", part) in (part, "-1", "0")
                        and props.get("ownerpartdisplaymode", "0") == mode):
                    x, y = _coord(props, "location.x"), _coord(props, "location.y")
                    box = part_boxes[owner]
                    box[:] = [min(box[0], x), min(box[1], y), max(box[2], x), max(box[3], y)]
            elif record == "34" and props.get("name") == "Designator" and owner in part_boxes:
                designators[owner] = props.get("text", "")
            elif record == "41" and owner in directives and props.get("name"):
                directives[owner].parameters[props["name"]] = props.get("text", "")
            continue  # symbol graphics, component parameters, template content

        if record in _TEXT_KINDS:
            text = props.get("text", "")
            if record != "4":  # labels are single-line and stored without escapes
                text = _TEXT_ESCAPE_RE.sub(lambda m: _TEXT_ESCAPES[m.group(1)], text)
            text = text.strip()
            if (not text or props.get("ishidden") == "T"
                    or (record == "4" and _SPECIAL_STRING_RE.match(text))):
                continue
            rect = _label_rect(props, text, font_sizes) if record == "4" else _rect(props)
            item = SheetAnnotation(kind=_TEXT_KINDS[record], text=text)
            if record == "209":
                item.author = props.get("author") or None
                item.collapsed = props.get("collapsed") == "T"
        elif record == "30":
            rect = _rect(props)
            item = SheetAnnotation(
                kind="image",
                file_name=props.get("filename") or None,
                embedded=props.get("embedimage") == "T",
                image_index=image_count,
            )
            image_count += 1
        elif record == "43":
            directives[pos] = SheetDirective(
                name=props.get("name", ""),
                x_mil=_coord(props, "location.x"),
                y_mil=_coord(props, "location.y"),
            )
            result.directives.append(directives[pos])
            continue
        elif record == "41":
            name, value = props.get("name", ""), props.get("text", "")
            if name:
                result.sheet_parameters[name] = value
            if (name and value not in ("", "*") and name.lower() not in _AUTO_SHEET_PARAMS
                    and not name.lower().startswith("versioncontrol_")
                    and not value.startswith("Record=")):  # printer setup, not a title
                result.title_block[name] = value
            continue
        elif record == "39":
            result.template = props.get("filename") or None
            continue
        else:
            continue
        item.x_mil, item.y_mil = rect[0], rect[1]
        item.width_mil, item.height_mil = rect[2] - rect[0], rect[3] - rect[1]
        rects[id(item)] = rect
        result.items.append(item)

    components = [(designators[pos], box) for pos, box in part_boxes.items() if designators.get(pos)]
    # A special string ("=A + '-' + B") is drawn as its value, whose extent is unknown
    # here, so it is not related to nearby parts or pictures.
    texts = [(i.text, rects[id(i)]) for i in result.items
             if i.kind != "image" and not i.text.startswith("=")]
    for item in result.items:
        if item.kind != "image" and not item.text.startswith("="):
            item.near = _closest(rects[id(item)], components, NEAR_COMPONENT_MIL, _MAX_NEAR)
        elif item.kind == "image":
            item.captions = _closest(rects[id(item)], texts, CAPTION_MIL, 3)
    # Reading order: top of the sheet first, then left to right.
    result.items.sort(key=lambda i: (-round(i.y_mil + i.height_mil), i.x_mil))
    return result


def _closest(rect: list[float], candidates: list[tuple[str, list[float]]],
             limit_mil: float, count: int) -> list[str]:
    names: list[str] = []
    for distance, name in sorted((_gap(rect, box), name) for name, box in candidates):
        if distance > limit_mil or len(names) == count:
            break
        if name not in names:
            names.append(name)
    return names


def read_sch_doc_image(path: str, file_name: str) -> bytes:
    """Return the picture Altium embedded in a .SchDoc under `file_name`.

    Embedded pictures live in the "Storage" stream: a `|HEADER=Icon storage|` record,
    then one record per picture holding 0xD0, a length-prefixed name, a uint32 size and
    zlib-compressed data. Altium stores the decoded bitmap (a BMP) whatever the original
    file type was. Raises KeyError when no picture has that name.
    """
    with olefile.OleFileIO(path) as f:
        if not f.exists("Storage"):
            raise KeyError(file_name)
        return image_from_storage(f.openstream("Storage").read(), file_name)


def image_from_storage(data: bytes, file_name: str) -> bytes:
    """read_sch_doc_image for the bytes of a Storage stream."""
    offset = 0
    first = True
    while offset + 4 <= len(data):
        length = struct.unpack_from("<I", data, offset)[0] & 0xFFFFFF
        body = data[offset + 4 : offset + 4 + length]
        offset += 4 + length
        if first:  # |HEADER=Icon storage|Weight=N
            first = False
            continue
        if len(body) < 6 or body[0] != 0xD0:
            continue
        name_len = body[1]
        raw_name = body[2 : 2 + name_len]
        size = struct.unpack_from("<I", body, 2 + name_len)[0]
        names = {raw_name.decode("latin-1")}
        try:
            names.add(raw_name.decode("utf-8"))
        except UnicodeDecodeError:
            pass
        if file_name in names:
            return zlib.decompress(body[6 + name_len : 6 + name_len + size])
    raise KeyError(file_name)
