"""Reader for Altium Draftsman documents (.PCBDwf).

A .PCBDwf is neither an OLE compound file nor a zip. It is an lz4net LZ4Stream: a run
of chunks, each `varint flags, varint original length, [varint compressed length],
data`, where flag bit 0 marks an LZ4 block (Altium writes 1 MB chunks). The payload is
a .NET DataContract XML document in the namespace
Altium.Designer.PcbDrawing.DataSerialization.V1. Lengths in it are WPF device
independent units of 1/96 inch.

Only text a designer reads off the drawing is extracted: notes, free text, tables,
view titles, pictures, and the drill table and layer stack data the drawing caches from
the board at its last update.
"""
import base64
import binascii
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field

_XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"
_XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
_UNIT_MM = 25.4 / 96

VIEW_KINDS = {
    "BoardFabricationViewData": "fabrication view",
    "BoardAssemblyViewData": "assembly view",
    "BoardRealisticViewData": "realistic view",
    "BoardProjectionViewData": "projection view",
    "BillOfMaterialsView": "bill of materials",
    "DrillTableBoardView": "drill table",
    "DrillViewView": "drill drawing",
    "LayerStackLegendView": "layer stack legend",
    "TransmissionLineTableData": "impedance table",
}


@dataclass
class DraftsmanItem:
    page: int                       # 1-based page number
    kind: str                       # "note" | "text" | "table" | "picture" | "view"
    title: str | None = None
    text: str | None = None         # free text
    lines: list[str] = field(default_factory=list)       # note entries, in order
    rows: list[list[str]] = field(default_factory=list)  # table cells
    view: str | None = None         # views: e.g. "fabrication view"
    side: str | None = None         # views: Top / Bottom / ...
    layer: str | None = None        # fabrication views: the board layer shown
    from_template: bool = False     # placed by the sheet template (title block)
    picture_index: int | None = None  # pictures: index for get_design_image
    width_mm: float | None = None
    height_mm: float | None = None


@dataclass
class DraftsmanDoc:
    pages: list[str] = field(default_factory=list)  # sheet size per page, e.g. "A4"
    items: list[DraftsmanItem] = field(default_factory=list)
    drill_table: list[dict] = field(default_factory=list)
    layer_notes: list[dict] = field(default_factory=list)
    parameters: dict[str, str] = field(default_factory=dict)
    source_pcb: str | None = None   # board the drawing was last updated from
    last_update: str | None = None  # UTC, as Draftsman writes it (ends in "Z")
    pictures: list[bytes] = field(default_factory=list, repr=False)  # by picture_index


# Root elements holding the drawing's copy of the board (geometry, BOM, assembly data).
# They are over 99% of the XML of a large drawing and nothing here reads them, so their
# bytes are cut out before parsing. Each occurs at most once, directly under the root.
_SKIPPED_SUBTREES = (
    b"BillOfMaterialsInformation", b"BoardAssemblyInformation", b"BoardFabricationInformation",
    b"BoardLayersGeometryInformation", b"BoardProjectionInformation",
)
# A "=Name" special string inside drawing text (also mid-word, as in "v=Version");
# only names the drawing has a cached value for are replaced.
_SPECIAL_RE = re.compile(r"(?<!=)=([A-Za-z_]\w*)")


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, pos


def _lz4_block(src: bytes) -> bytearray:
    """Decompress one raw LZ4 block (no frame header)."""
    dst = bytearray()
    i, end = 0, len(src)
    while i < end:
        token = src[i]
        i += 1
        literal = token >> 4
        if literal == 15:
            while True:
                byte = src[i]
                i += 1
                literal += byte
                if byte != 255:
                    break
        dst += src[i : i + literal]
        i += literal
        if i >= end:  # the last sequence carries literals only
            break
        offset = src[i] | src[i + 1] << 8
        i += 2
        if offset == 0 or offset > len(dst):
            raise ValueError(f"LZ4 match offset {offset} outside the decoded data")
        match = token & 15
        if match == 15:
            while True:
                byte = src[i]
                i += 1
                match += byte
                if byte != 255:
                    break
        match += 4
        start = len(dst) - offset
        if match <= offset:
            dst += dst[start : start + match]
        else:  # an overlapping match repeats the last `offset` bytes
            pattern = dst[start:]
            repeats, rest = divmod(match, offset)
            dst += pattern * repeats + pattern[:rest]
    return dst


def decode_pcb_dwf(data: bytes) -> bytes:
    """Unpack the LZ4Stream chunks of a .PCBDwf into its XML bytes."""
    return bytes(_decode(data))


def _decode(data: bytes) -> bytearray:
    out = bytearray()
    pos = 0
    try:
        while pos < len(data):
            flags, pos = _varint(data, pos)
            length, pos = _varint(data, pos)
            if flags & 1:
                size, pos = _varint(data, pos)
                block = _lz4_block(data[pos : pos + size])
                pos += size
            else:
                block = data[pos : pos + length]
                pos += length
            if len(block) != length:
                raise ValueError(f"chunk decoded to {len(block)} bytes, expected {length}")
            out += block
    except IndexError as e:
        raise ValueError("truncated Draftsman chunk") from e
    return out


def _without_board_copy(xml: bytearray) -> bytes:
    """The XML with the first element of each _SKIPPED_SUBTREES tag cut out.

    An element is left in place when it is self-closing (Draftsman writes empty ones as
    <Tag i:nil="true"/>), has no closing tag, or holds another element of the same name.
    """
    cuts = []
    for tag in _SKIPPED_SUBTREES:
        opening = b"<" + tag
        start = xml.find(opening)
        if start < 0 or xml[start + len(opening) : start + len(opening) + 1] not in (b" ", b">"):
            continue
        start_end = xml.find(b">", start)
        if start_end < 0 or xml[start_end - 1 : start_end] == b"/":
            continue
        close = b"</" + tag + b">"
        end = xml.find(close, start_end)
        if end < 0 or xml.find(opening, start_end, end) >= 0:
            continue
        cuts.append((start, end + len(close)))
    kept, pos = [], 0
    for start, end in sorted(cuts):
        if start >= pos:
            kept.append(xml[pos:start])
            pos = end
    kept.append(xml[pos:])
    return b"".join(kept)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el: ET.Element | None, name: str) -> ET.Element | None:
    if el is None:
        return None
    return next((c for c in el if _local(c.tag) == name), None)


def _children(el: ET.Element | None, name: str) -> list[ET.Element]:
    return [] if el is None else [c for c in el if _local(c.tag) == name]


def _text(el: ET.Element | None, name: str) -> str:
    found = _child(el, name)
    return (found.text or "").strip() if found is not None else ""


def _mm(value: str) -> float | None:
    try:
        return round(float(value) * _UNIT_MM, 3)
    except ValueError:
        return None


def _item_title(item: ET.Element) -> str | None:
    """A view's or table's title: its own Title, or the linked "Title" text element."""
    title = _text(item, "Title")
    if title:
        return title
    for element in _children(_child(item, "ElementItems"), "ElementItemData"):
        if _text(element, "Name") == "Title" and _text(element, "Title"):
            return _text(element, "Title")
    return None


def _picture_bytes(item: ET.Element) -> bytes:
    """The picture's image file, or b"" when it cannot be decoded (only that picture fails)."""
    try:
        return base64.b64decode(_text(item, "Bitmap") or _text(item, "Bitmap2"))
    except (binascii.Error, ValueError):
        return b""


def _parse_item(item: ET.Element, page: int, layers: dict[str, str],
                values: dict[str, str]) -> DraftsmanItem | None:
    kind = item.attrib.get(_XSI_TYPE, "")
    parsed = DraftsmanItem(page=page, kind="", from_template=_text(item, "OriginatedFromTemplate") == "true")

    def resolve(text: str) -> str:
        """Put the cached value in place of each "=Name" the drawing knows."""
        return _SPECIAL_RE.sub(lambda m: values.get(m.group(1).lower()) or m.group(0), text)

    if kind == "Note":
        parsed.kind = "note"
        parsed.title = resolve(_text(item, "Title")) or None
        elements = _children(_child(item, "Elements"), "NoteElement")
        parsed.lines = [resolve(t) for t in (_text(e, "Text") for e in elements) if t]
        if not parsed.lines and not parsed.title:
            return None
    elif kind == "Text":
        parsed.kind = "text"
        parsed.text = resolve(_text(item, "Text"))
        if not parsed.text:
            return None
    elif kind == "Table":
        parsed.kind = "table"
        parsed.title = resolve(_item_title(item) or "") or None
        for row in _children(_child(item, "Rows"), "ArrayOfTableCell"):
            cells = [resolve(_text(cell, "Text")) for cell in _children(row, "TableCell")]
            if any(cells):
                parsed.rows.append(cells)
    elif kind == "Picture":
        parsed.kind = "picture"
        rect = _child(item, "Rect")
        parsed.width_mm = _mm(_text(rect, "Width"))
        parsed.height_mm = _mm(_text(rect, "Height"))
    elif kind in VIEW_KINDS:
        parsed.kind = "view"
        parsed.view = VIEW_KINDS[kind]
        parsed.side = _text(item, "ViewSide") or None
        if kind == "BoardFabricationViewData":
            layer_id = _text(item, "LayerV7Id")
            parsed.layer = layers.get(layer_id) or _default_layer_name(layer_id)
        title = _item_title(item) or ""
        if parsed.side:
            title = title.replace("=ViewSide", parsed.side)
        if parsed.layer:
            title = title.replace("=ViewLayerName", parsed.layer)
        scale = _child(item, "Scale")
        if scale is not None and _text(item, "UseCustomScale") != "true" and \
                _text(scale, "Numenator") and _text(scale, "Denominator"):
            title = title.replace("=ViewScale", f"{_text(scale, 'Numenator')}:{_text(scale, 'Denominator')}")
        parsed.title = resolve(title) or None
    else:
        return None
    return parsed


def _drill_table(root: ET.Element) -> list[dict]:
    info = _child(root, "DrillTableInformation")
    if info is None or info.attrib.get(_XSI_NIL) == "true":
        return []
    holes = _children(_child(info, "HolesV2"), "LayerStackHole") or _children(
        _child(info, "Holes"), "LayerStackHole"
    )
    counts: Counter = Counter()
    for hole in holes:
        slot = _mm(_text(hole, "Lenght"))  # sic: Altium's spelling of "Length"
        counts[(
            _mm(_text(hole, "HoleSize")),
            _text(hole, "Plated") == "true",
            _text(hole, "HoleType"),
            f"{_text(hole, 'TopLayerName')} - {_text(hole, 'BottomLayerName')}",
            slot or None,
            _text(hole, "IsBackdrill") == "true",
        )] += 1
    rows = []
    for (size, plated, hole_type, span, slot, backdrill), count in counts.items():
        row = {"size_mm": size, "plated": plated, "type": hole_type, "span": span, "count": count}
        if slot:
            row["slot_length_mm"] = slot
        if backdrill:
            row["backdrill"] = True
        rows.append(row)
    return sorted(rows, key=lambda r: (r["size_mm"] or 0, r["type"], not r["plated"]))


def _layer_stacks(root: ET.Element) -> list[ET.Element]:
    """The board's layer stacks. Draftsman also serialises a second copy of each stack
    under LayerStackInformation/LayerStack, which is ignored here."""
    data = _child(_child(root, "LayerStackInformation"), "BoardLayerStackData")
    return _children(data, "BoardLayerStack")


def _layer_names(stacks: list[ET.Element]) -> dict[str, str]:
    """IdInBoard -> layer name, for the layer a fabrication view shows (LayerV7Id)."""
    return {
        _text(layer, "IdInBoard"): _text(layer, "Name")
        for stack in stacks
        for layer in _children(_child(stack, "Stack"), "StackLayerBoardLayerData")
        if _text(layer, "IdInBoard") and _text(layer, "Name")
    }


def _layer_notes(stacks: list[ET.Element]) -> list[dict]:
    notes = []
    for stack in stacks:
        for layer in _children(_child(stack, "Stack"), "StackLayerBoardLayerData"):
            entry = {key.lower(): _text(layer, key) for key in ("Comment", "Note") if _text(layer, key)}
            if entry:
                if len(stacks) > 1:
                    entry = {"stack": _text(stack, "Name"), **entry}
                notes.append({"layer": _text(layer, "Name"), **entry})
    return notes


def _cached_values(root: ET.Element) -> dict[str, str]:
    """Lower-cased parameter name -> value the drawing shows for "=Name" special strings.

    Document parameters come first, then the board and project values the drawing cached
    at its last update (ParametersData)."""
    values: dict[str, str] = {}
    data = _child(root, "ParametersData")
    groups = [_child(root, "Parameters")] + [
        _child(data, name) for name in ("PCBParametersV2", "PCBParameters", "ProjectParameters")
    ]
    for group in groups:
        for param in _children(group, "DrawingDocumentParameterData"):
            name, value = _text(param, "Name"), _text(param, "Value")
            if name and value:
                values.setdefault(name.lower(), value)
    return values


def _default_layer_name(layer_id: str) -> str | None:
    """Altium's default name for a V7 layer id, for drawings without a cached stack."""
    try:
        n = int(layer_id)
    except ValueError:
        return None
    if n == 1:
        return "Top Layer"
    if n == 32:
        return "Bottom Layer"
    if 2 <= n <= 31:
        return f"Mid-Layer {n - 1}"
    if 39 <= n <= 54:
        return f"Internal Plane {n - 38}"
    return None


def _parse_root(xml: bytearray) -> ET.Element:
    try:
        root = ET.fromstring(_without_board_copy(xml))
    except ET.ParseError:  # the cut went wrong on an unusual file: parse all of it
        root = ET.fromstring(bytes(xml))
    if _local(root.tag) != "Document" or "PcbDrawing" not in root.tag:
        raise ValueError("not an Altium Draftsman document")
    return root


def parse_pcb_dwf(path: str) -> DraftsmanDoc:
    """Read the notes, text, tables, view titles, pictures and cached board data of a .PCBDwf."""
    with open(path, "rb") as f:
        root = _parse_root(_decode(f.read()))
    stacks = _layer_stacks(root)
    layers, values = _layer_names(stacks), _cached_values(root)
    doc = DraftsmanDoc(
        source_pcb=_text(root, "SourceDocumentName") or None,
        last_update=_text(root, "LastUpdate") or None,
        drill_table=_drill_table(root),
        layer_notes=_layer_notes(stacks),
    )
    for param in _children(_child(root, "Parameters"), "DrawingDocumentParameterData"):
        name, value = _text(param, "Name"), _text(param, "Value")
        if name and value:
            doc.parameters[name] = value
    for page_no, page in enumerate(_children(_child(root, "Pages"), "Page"), start=1):
        doc.pages.append(_text(page, "StandardSizeName") or "custom")
        for element in _children(_child(page, "Items"), "Item"):
            item = _parse_item(element, page_no, layers, values)
            if item is None:
                continue
            if item.kind == "picture":
                item.picture_index = len(doc.pictures)
                doc.pictures.append(_picture_bytes(element))
            doc.items.append(item)
    return doc


def read_pcb_dwf_picture(path: str, index: int) -> bytes:
    """Return the image file bytes (usually PNG) of the index-th picture in a .PCBDwf."""
    pictures = parse_pcb_dwf(path).pictures
    if not 0 <= index < len(pictures):
        raise KeyError(index)
    return pictures[index]
