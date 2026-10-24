"""Parser for Altium .PcbLib footprint libraries (OLE compound files).

Pure parsing only: OLE streams -> dataclasses, lengths and coordinates in mil relative
to the footprint's reference point, like the rest of the PCB code.

Layout (checked on Altium's AD25 example libraries): FileHeader names the format
("PCB 6.0 Binary Library File"); Library/Data holds the library's text properties
(the layer table, with each mechanical layer's kind) followed by a u32 footprint count
and the footprint names (u32 length + u8-length string); Library/ComponentParamsTOC
lists Name, Pad Count, Height and Description per footprint. Each footprint has a
storage (sch_lib.storage_name: '/' as '_', cut to OLE's 31 characters) holding
Parameters (PATTERN, HEIGHT, DESCRIPTION, ITEMGUID, REVISIONGUID) and Data: the
footprint's name, then its primitives one after another, each in the record layout of
the PcbDoc stream for its type (u8 type 1 arc, 2 pad, 3 via, 4 track, 5 text, 6 fill,
11 region in the Regions6 layout, 12 3D body). split_primitives groups them so
pcb_doc's stream decoders read them unchanged. Every primitive sits on a v6 layer
(1..82); a mechanical layer above 16 is v6 72 (Mechanical 16) and named by the V7 layer
id of a track, arc or fill (pcb_doc.v7_mechanical) or the V7_LAYER of a region or body.
"""

import math
import re
import struct
from dataclasses import dataclass, field

import olefile

try:
    from parsers.pcb_doc import (
        V7_MECHANICAL, BareHole, ComponentBody, PcbFill, PcbRegion, Primitive, decode_arcs,
        decode_bare_holes, decode_component_bodies, decode_fill_records, decode_pads,
        decode_region_records, decode_tracks, decode_vias, parse_dim_mil, read_text_records,
    )
    from parsers.sch_lib import storage_name
except ImportError:
    from server.parsers.pcb_doc import (
        V7_MECHANICAL, BareHole, ComponentBody, PcbFill, PcbRegion, Primitive, decode_arcs,
        decode_bare_holes, decode_component_bodies, decode_fill_records, decode_pads,
        decode_region_records, decode_tracks, decode_vias, parse_dim_mil, read_text_records,
    )
    from server.parsers.sch_lib import storage_name

RECORD_ARC, RECORD_PAD, RECORD_VIA, RECORD_TRACK = 1, 2, 3, 4
RECORD_TEXT, RECORD_FILL, RECORD_REGION, RECORD_BODY = 5, 6, 11, 12
# A pad is its name and five subrecords, a text its properties and its string; every
# other primitive one u32-length record.
_SUBRECORDS = {RECORD_PAD: 6, RECORD_TEXT: 2}
_RECORD_NAMES = {
    RECORD_ARC: "arcs", RECORD_PAD: "pads", RECORD_VIA: "vias", RECORD_TRACK: "tracks",
    RECORD_TEXT: "texts", RECORD_FILL: "fills", RECORD_REGION: "regions",
    RECORD_BODY: "bodies",
}
# v6 ids of Mechanical 1..16; mechanical layers above 16 have a V7 layer id only.
_MECHANICAL_V6 = range(57, 73)
_MECH_KIND_KEY = re.compile(r"(.*?)_?MECHKIND")
_TEXT_HEAD = 42  # the properties subrecord holds at least the comment/designator flags


@dataclass
class FootprintText:
    """A footprint's text (mil): .Designator and .Comment are the placed part's."""

    layer: int
    x: float
    y: float
    height: float
    rotation: float
    text: str
    is_designator: bool = False
    is_comment: bool = False


@dataclass
class Outline:
    """The lines of one mechanical layer kind (courtyard, assembly, ...) on one side:
    tracks, arc chords, fills and regions together, as a bounding box (mil)."""

    kind: str  # courtyard, assembly, 3d_body, component_center, or the kind as written
    side: str | None  # top, bottom, or None when the kind names no side
    mechanical: int  # the mechanical layer's number (1..1024)
    primitives: int
    box: tuple[float, float, float, float]  # min x, min y, max x, max y


@dataclass
class Footprint:
    name: str
    storage: str
    description: str = ""
    # Parameters HEIGHT: the footprint's Height property, the fallback 3D height when
    # it has no 3D body. None when not set.
    height_mil: float | None = None
    item_guid: str = ""
    revision_guid: str = ""
    pads: list[Primitive] = field(default_factory=list)  # pads with copper
    holes: list[BareHole] = field(default_factory=list)  # pads without copper
    tracks: list[Primitive] = field(default_factory=list)
    arcs: list[Primitive] = field(default_factory=list)  # chord rows, see decode_arcs
    vias: list[Primitive] = field(default_factory=list)
    fills: list[PcbFill] = field(default_factory=list)
    regions: list[PcbRegion] = field(default_factory=list)
    bodies: list[ComponentBody] = field(default_factory=list)
    texts: list[FootprintText] = field(default_factory=list)
    outlines: list[Outline] = field(default_factory=list)
    # Primitive records per type as read (texts, bodies and so on by name), records
    # the decoders skipped per type, and bytes left after the last whole record.
    records: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    unread_bytes: int = 0

    @property
    def pad_designators(self) -> list[str]:
        """Every pad's designator (holes included), in file order."""
        return [pad.pad_name or "" for pad in self.pads] + [hole.pad_name for hole in self.holes]

    @property
    def body_height_mil(self) -> float | None:
        """The tallest 3D body (OverallHeight), or None without bodies."""
        heights = [body.overall_height_mil for body in self.bodies
                   if body.overall_height_mil is not None]
        return max(heights) if heights else None

    @property
    def models(self) -> list[str]:
        """The 3D model names the bodies use, in file order, each once."""
        names: list[str] = []
        for body in self.bodies:
            if body.model_name and body.model_name not in names:
                names.append(body.model_name)
        return names

    def outline(self, kind: str, side: str | None = None) -> list[Outline]:
        return [item for item in self.outlines
                if item.kind == kind and (side is None or item.side in (side, None))]


@dataclass
class PcbLib:
    path: str
    footprints: list[Footprint] = field(default_factory=list)
    # The footprint names Library/Data lists, in file order.
    names: list[str] = field(default_factory=list)
    # Mechanical layer number -> its kind as written (CourtyardTop, AssemblyBottom,
    # 3DBodyTop, ...), from the layer table; empty for libraries without layer kinds.
    mech_kinds: dict[int, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def find(self, name: str) -> Footprint | None:
        """The footprint named name: an exact match first, then one that differs only
        in case, as Altium's library search."""
        if not name:
            return None
        exact = next((fp for fp in self.footprints if fp.name == name), None)
        if exact:
            return exact
        folded = name.casefold()
        return next((fp for fp in self.footprints if fp.name.casefold() == folded), None)


def _block_string(data: bytes, at: int) -> tuple[str, int]:
    """A u32-length block holding a u8-length string (names in Library/Data and at the
    start of a footprint's Data) and the offset after the block."""
    (length,) = struct.unpack_from("<I", data, at)
    block = data[at + 4 : at + 4 + length]
    if at + 4 + length > len(data) or not block:
        raise ValueError("name block past the stream")
    return block[1 : 1 + block[0]].decode("latin-1"), at + 4 + length


def split_primitives(data: bytes, at: int = 0) -> tuple[dict[int, bytes], dict[int, int], int]:
    """Group the primitive records from offset `at` of a footprint's Data stream by
    type: ({type: the records' bytes, as the PcbDoc stream of that type holds them},
    {type: records}, bytes left unread). An unknown type or a record running past the
    stream ends the walk."""
    streams: dict[int, bytearray] = {}
    counts: dict[int, int] = {}
    while at + 5 <= len(data):
        record_type = data[at]
        if record_type not in _RECORD_NAMES:
            break
        end = at + 1
        for _ in range(_SUBRECORDS.get(record_type, 1)):
            if end + 4 > len(data):
                end = len(data) + 1  # the subrecord's length is cut off
                break
            (length,) = struct.unpack_from("<I", data, end)
            end += 4 + length
        if end > len(data):
            break
        streams.setdefault(record_type, bytearray()).extend(data[at:end])
        counts[record_type] = counts.get(record_type, 0) + 1
        at = end
    return {kind: bytes(raw) for kind, raw in streams.items()}, counts, len(data) - at


def _texts(data: bytes) -> list[FootprintText]:
    """Text records (u8 type, u32 length + properties, u32 length + u8-length string):
    layer at 0, position (int32 x, y) at 13, height at 21, rotation (double) at 27,
    IsComment at 40, IsDesignator at 41, as in Texts6."""
    texts: list[FootprintText] = []
    at = 0
    while at + 5 <= len(data):
        (head_length,) = struct.unpack_from("<I", data, at + 1)
        head = data[at + 5 : at + 5 + head_length]
        string_at = at + 5 + head_length
        (string_length,) = struct.unpack_from("<I", data, string_at)
        raw = data[string_at + 4 : string_at + 4 + string_length]
        at = string_at + 4 + string_length
        if len(head) < _TEXT_HEAD:
            continue
        x, y, height = struct.unpack_from("<iii", head, 13)
        (rotation,) = struct.unpack_from("<d", head, 27)
        body = raw[1 : 1 + raw[0]] if raw else b""
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            text = body.decode("latin-1")
        texts.append(FootprintText(
            layer=head[0], x=x / 10000.0, y=y / 10000.0, height=height / 10000.0,
            rotation=rotation if math.isfinite(rotation) else 0.0, text=text,
            is_comment=bool(head[40]), is_designator=bool(head[41]),
        ))
    return texts


def mech_kinds(properties: dict[str, str]) -> dict[int, str]:
    """Mechanical layer number -> MECHKIND from a layer table (Library/Data or Board6):
    LAYER<v6>MECHKIND for Mechanical 1-16, and any layer entry whose LAYERID is a
    mechanical V7 id (0x0102nnnn) with a MECHKIND beside it."""
    kinds: dict[int, str] = {}
    for key, value in properties.items():
        value = value.strip()
        match = _MECH_KIND_KEY.fullmatch(key)
        if not match or not value or value.lower() in ("notset", "none"):
            continue
        prefix = match.group(1)
        legacy = re.fullmatch(r"LAYER(\d+)", prefix)
        if legacy and int(legacy.group(1)) in _MECHANICAL_V6:
            kinds.setdefault(int(legacy.group(1)) - 56, value)
            continue
        layer_id = properties.get(f"{prefix}LAYERID") or properties.get(f"{prefix}_LAYERID")
        try:
            number = int(layer_id or "")
        except ValueError:
            continue
        if number & 0xFFFF0000 == V7_MECHANICAL:
            kinds.setdefault(number & 0xFFFF, value)
    return dict(sorted(kinds.items()))


def layer_kind(mech_kind: str) -> tuple[str, str | None]:
    """(kind, side) of a MECHKIND: CourtyardTop -> (courtyard, top), 3DBodyBottom ->
    (3d_body, bottom), AssemblyTop -> (assembly, top); other kinds lower-cased."""
    low = mech_kind.strip().lower()
    side = None
    for suffix in ("top", "bottom"):
        if low.endswith(suffix) and len(low) > len(suffix):
            side, low = suffix, low[: -len(suffix)]
            break
    names = {"courtyard": "courtyard", "assembly": "assembly", "3dbody": "3d_body",
             "componentcenter": "component_center", "componentoutline": "component_outline",
             "designator": "designator", "value": "value"}
    return names.get(low, low), side


def mechanical_number(primitive) -> int | None:
    """The mechanical layer number of a track, arc, fill or region (its V7 layer id, else
    its v6 layer), None off the mechanical layers."""
    if primitive.mechanical is not None:
        return primitive.mechanical
    return primitive.layer - 56 if primitive.layer in _MECHANICAL_V6 else None


def _outlines(footprint: Footprint, kinds: dict[int, str]) -> list[Outline]:
    """Footprint.outlines: the boxes of the primitives on mechanical layers with a kind."""
    boxes: dict[int, list[float]] = {}
    counts: dict[int, int] = {}

    def add(primitive, points: list[tuple[float, float]], grow: float = 0.0) -> None:
        number = mechanical_number(primitive)
        if number not in kinds or not points:
            return
        xs = [x for x, _ in points]
        ys = [y for _, y in points]
        box = boxes.setdefault(number, [math.inf, math.inf, -math.inf, -math.inf])
        box[0] = min(box[0], min(xs) - grow)
        box[1] = min(box[1], min(ys) - grow)
        box[2] = max(box[2], max(xs) + grow)
        box[3] = max(box[3], max(ys) + grow)
        counts[number] = counts.get(number, 0) + 1

    for line in footprint.tracks + footprint.arcs:
        add(line, [(line.x1, line.y1), (line.x2, line.y2)], line.width / 2.0)
    for fill in footprint.fills:
        add(fill, [(fill.x1, fill.y1), (fill.x2, fill.y2)])
    for region in footprint.regions:
        add(region, [(vertex.x, vertex.y) for vertex in region.vertices])
    outlines = []
    for number, box in sorted(boxes.items()):
        kind, side = layer_kind(kinds[number])
        outlines.append(Outline(kind, side, number, counts[number], tuple(box)))
    return outlines


def _decode(record_type: int, data: bytes, footprint: Footprint) -> None:
    """Decode one footprint's records of one type into footprint (its skips counted)."""
    skipped = 0
    if record_type == RECORD_PAD:
        footprint.pads, skipped = decode_pads(data)
        footprint.holes = decode_bare_holes(data)
        skipped -= len(footprint.holes)
    elif record_type == RECORD_TRACK:
        footprint.tracks, skipped = decode_tracks(data)
    elif record_type == RECORD_ARC:
        footprint.arcs, skipped = decode_arcs(data)
    elif record_type == RECORD_VIA:
        footprint.vias, skipped = decode_vias(data)
    elif record_type == RECORD_FILL:
        footprint.fills, skipped = decode_fill_records(data)
    elif record_type == RECORD_REGION:
        footprint.regions, skipped = decode_region_records(data, shape_based=False)
    elif record_type == RECORD_BODY:
        footprint.bodies, skipped = decode_component_bodies(data)
    elif record_type == RECORD_TEXT:
        footprint.texts = _texts(data)
    if skipped:
        footprint.skipped[_RECORD_NAMES[record_type]] = skipped


def footprint_from_streams(storage: str, data: bytes, parameters: bytes = b"",
                           kinds: dict[int, str] | None = None) -> Footprint:
    """A footprint from its storage's Data and Parameters streams."""
    name, at = _block_string(data, 0)
    footprint = Footprint(name=name or storage, storage=storage)
    properties = read_text_records(parameters)[0] if parameters else {}
    footprint.description = properties.get("DESCRIPTION", "")
    footprint.height_mil = parse_dim_mil(properties.get("HEIGHT"))
    footprint.item_guid = properties.get("ITEMGUID", "")
    footprint.revision_guid = properties.get("REVISIONGUID", "")
    streams, counts, footprint.unread_bytes = split_primitives(data, at)
    footprint.records = {_RECORD_NAMES[kind]: count for kind, count in sorted(counts.items())}
    for record_type, raw in sorted(streams.items()):
        try:
            _decode(record_type, raw, footprint)
        except Exception:  # an undecodable type is counted, the rest kept
            footprint.skipped[_RECORD_NAMES[record_type]] = counts[record_type]
    footprint.outlines = _outlines(footprint, kinds or {})
    return footprint


def _library_names(data: bytes) -> tuple[dict[str, str], list[str]]:
    """(the Library/Data text properties, the footprint names after them)."""
    (length,) = struct.unpack_from("<I", data, 0)
    properties = read_text_records(data[: 4 + length])
    names: list[str] = []
    at = 4 + length
    if at + 4 <= len(data):
        (count,) = struct.unpack_from("<I", data, at)
        at += 4
        for _ in range(count):
            try:
                name, at = _block_string(data, at)
            except (ValueError, struct.error):
                break
            names.append(name)
    return (properties[0] if properties else {}), names


def pcb_lib_from_ole(ole: olefile.OleFileIO, path: str = "",
                     names: set[str] | None = None) -> PcbLib:
    """A PcbLib from an open OLE file. names (case-folded footprint names), when given,
    limits the footprints read to those (storages are matched by sch_lib.storage_name;
    when that misses, the other storages are read to find them)."""
    library = PcbLib(path=path)
    if ole.exists("Library/Data"):
        properties, library.names = _library_names(ole.openstream("Library/Data").read())
        library.mech_kinds = mech_kinds(properties)
    storages = sorted({entry[0] for entry in ole.listdir()
                       if len(entry) == 2 and entry[1] == "Data"
                       and entry[0] not in ("Library", "FileVersionInfo")})

    def read(storage: str) -> None:
        try:
            data = ole.openstream(f"{storage}/Data").read()
            parameters = (ole.openstream(f"{storage}/Parameters").read()
                          if ole.exists(f"{storage}/Parameters") else b"")
            library.footprints.append(
                footprint_from_streams(storage, data, parameters, library.mech_kinds))
        except Exception as error:  # one unreadable footprint leaves the others
            library.warnings.append(f"{storage}: {type(error).__name__}: {error}")

    if names is None:
        for storage in storages:
            read(storage)
        return library
    wanted = {storage_name(name) for name in names}
    first = [storage for storage in storages if storage.casefold() in wanted]
    for storage in first:
        read(storage)
    if names - {fp.name.casefold() for fp in library.footprints}:
        for storage in storages:
            if storage not in first:
                read(storage)
        library.footprints = [fp for fp in library.footprints if fp.name.casefold() in names]
    return library


def parse_pcb_lib(path: str, names: set[str] | None = None) -> PcbLib:
    """Read a .PcbLib file (see pcb_lib_from_ole for names)."""
    with olefile.OleFileIO(path) as ole:
        return pcb_lib_from_ole(ole, path, names)
