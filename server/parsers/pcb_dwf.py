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
    last_update: str | None = None


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
    return bytes(out)


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
    return base64.b64decode(_text(item, "Bitmap") or _text(item, "Bitmap2"))


def _parse_item(item: ET.Element, page: int) -> DraftsmanItem | None:
    kind = item.attrib.get(_XSI_TYPE, "")
    parsed = DraftsmanItem(page=page, kind="", from_template=_text(item, "OriginatedFromTemplate") == "true")
    if kind == "Note":
        parsed.kind = "note"
        parsed.title = _text(item, "Title") or None
        elements = _children(_child(item, "Elements"), "NoteElement")
        parsed.lines = [t for t in (_text(e, "Text") for e in elements) if t]
        if not parsed.lines and not parsed.title:
            return None
    elif kind == "Text":
        parsed.kind = "text"
        parsed.text = _text(item, "Text")
        if not parsed.text:
            return None
    elif kind == "Table":
        parsed.kind = "table"
        parsed.title = _item_title(item)
        for row in _children(_child(item, "Rows"), "ArrayOfTableCell"):
            cells = [_text(cell, "Text") for cell in _children(row, "TableCell")]
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
        parsed.title = _item_title(item)
        if parsed.title and parsed.side:
            parsed.title = parsed.title.replace("=ViewSide", parsed.side)
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


def _layer_notes(root: ET.Element) -> list[dict]:
    info = _child(root, "LayerStackInformation")
    if info is None:
        return []
    notes = []
    for layer in info.iter():
        if _local(layer.tag) != "StackLayerBoardLayerData":
            continue
        entry = {key.lower(): _text(layer, key) for key in ("Comment", "Note") if _text(layer, key)}
        if entry:
            notes.append({"layer": _text(layer, "Name"), **entry})
    return notes


def _parse_root(xml: bytes) -> ET.Element:
    root = ET.fromstring(xml)
    if _local(root.tag) != "Document" or "PcbDrawing" not in root.tag:
        raise ValueError("not an Altium Draftsman document")
    return root


def parse_pcb_dwf(path: str) -> DraftsmanDoc:
    """Read the notes, text, tables, view titles and cached board data of a .PCBDwf."""
    with open(path, "rb") as f:
        root = _parse_root(decode_pcb_dwf(f.read()))
    doc = DraftsmanDoc(
        source_pcb=_text(root, "SourceDocumentName") or None,
        last_update=_text(root, "LastUpdate") or None,
        drill_table=_drill_table(root),
        layer_notes=_layer_notes(root),
    )
    for param in _children(_child(root, "Parameters"), "DrawingDocumentParameterData"):
        name, value = _text(param, "Name"), _text(param, "Value")
        if name and value:
            doc.parameters[name] = value
    pictures = 0
    for page_no, page in enumerate(_children(_child(root, "Pages"), "Page"), start=1):
        doc.pages.append(_text(page, "StandardSizeName") or "custom")
        for element in _children(_child(page, "Items"), "Item"):
            item = _parse_item(element, page_no)
            if item is None:
                continue
            if item.kind == "picture":
                item.picture_index = pictures
                pictures += 1
            doc.items.append(item)
    return doc


def read_pcb_dwf_picture(path: str, index: int) -> bytes:
    """Return the image file bytes (usually PNG) of the index-th picture in a .PCBDwf."""
    with open(path, "rb") as f:
        root = _parse_root(decode_pcb_dwf(f.read()))
    pictures = [
        item for page in _children(_child(root, "Pages"), "Page")
        for item in _children(_child(page, "Items"), "Item")
        if item.attrib.get(_XSI_TYPE) == "Picture"
    ]
    if not 0 <= index < len(pictures):
        raise KeyError(index)
    return _picture_bytes(pictures[index])
