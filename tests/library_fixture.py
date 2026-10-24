"""Synthetic Altium library files for the library readers' tests: a minimal OLE compound
file writer (olefile only reads them), and SchLib, PcbLib and IntLib streams written the
way Altium writes them. Every name and value here is made up.

Lengths and coordinates given in mil (PcbLib) or in schematic units of 10 mil (SchLib
pins), as the files store them after conversion."""
import struct
import zlib

from tests.sch_fixture import record

# ---------- OLE compound file (CFB version 3) ----------

_SECTOR, _MINI, _CUTOFF = 512, 64, 4096
_FREE, _END, _FAT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD
_NONE = 0xFFFFFFFF


class _Entry:
    def __init__(self, name: str, kind: int, data: bytes = b""):
        self.name, self.kind, self.data = name, kind, data
        self.children: dict[str, _Entry] = {}
        self.id = 0
        self.start, self.size = _END, 0
        self.left = self.right = self.child = _NONE


def _chain(fat: list[int], first: int, count: int) -> None:
    for sector in range(first, first + count):
        fat.append(sector + 1 if sector + 1 < first + count else _END)


def _sectors(length: int, size: int) -> int:
    return -(-length // size)


def ole_bytes(streams: dict[str, bytes]) -> bytes:
    """A CFB v3 file holding streams ({"Storage/Stream": data}); streams under 4096
    bytes go to the mini stream, as the format requires."""
    root = _Entry("Root Entry", 5)
    for path, data in streams.items():
        node = root
        *storages, name = path.split("/")
        for storage in storages:
            node = node.children.setdefault(storage, _Entry(storage, 1))
        node.children[name] = _Entry(name, 2, data)
    entries: list[_Entry] = []

    def number(node: _Entry) -> None:
        node.id = len(entries)
        entries.append(node)
        for child in node.children.values():
            number(child)

    def tree(nodes: list[_Entry]) -> int:  # a balanced tree in CFB sibling order
        if not nodes:
            return _NONE
        middle = len(nodes) // 2
        nodes[middle].left = tree(nodes[:middle])
        nodes[middle].right = tree(nodes[middle + 1:])
        return nodes[middle].id

    number(root)
    for entry in entries:
        if entry.children:
            entry.child = tree(sorted(entry.children.values(),
                                      key=lambda node: (len(node.name), node.name.upper())))
    mini = bytearray()
    mini_fat: list[int] = []
    big: list[_Entry] = []
    for entry in entries:
        if entry.kind != 2:
            continue
        entry.size = len(entry.data)
        if not entry.data:
            continue
        if entry.size < _CUTOFF:
            entry.start = len(mini) // _MINI
            count = _sectors(entry.size, _MINI)
            _chain(mini_fat, entry.start, count)
            mini += entry.data + b"\0" * (count * _MINI - entry.size)
        else:
            big.append(entry)
    fat: list[int] = []
    body = bytearray()
    for entry in big:
        entry.start = len(fat)
        count = _sectors(entry.size, _SECTOR)
        _chain(fat, entry.start, count)
        body += entry.data + b"\0" * (count * _SECTOR - entry.size)
    root.size = len(mini)
    if mini:
        root.start = len(fat)
        count = _sectors(len(mini), _SECTOR)
        _chain(fat, root.start, count)
        body += mini + b"\0" * (count * _SECTOR - len(mini))
    mini_fat_start, mini_fat_count = _END, 0
    if mini_fat:
        mini_fat_start, mini_fat_count = len(fat), _sectors(4 * len(mini_fat), _SECTOR)
        _chain(fat, mini_fat_start, mini_fat_count)
        table = mini_fat + [_FREE] * (mini_fat_count * 128 - len(mini_fat))
        body += struct.pack(f"<{len(table)}I", *table)
    directory_start = len(fat)
    directory_count = _sectors(len(entries), 4)
    _chain(fat, directory_start, directory_count)
    for entry in entries + [None] * (directory_count * 4 - len(entries)):
        if entry is None:
            body += b"\0" * 64 + struct.pack("<HBBIII", 0, 0, 0, _NONE, _NONE, _NONE) + b"\0" * 48
            continue
        name = entry.name.encode("utf-16-le") + b"\0\0"
        body += name.ljust(64, b"\0") + struct.pack(
            "<HBBIII", len(name), entry.kind, 1, entry.left, entry.right, entry.child)
        body += b"\0" * 16 + struct.pack("<IQQIQ", 0, 0, 0, entry.start, entry.size)
    fat_count = 1
    while fat_count * 128 < len(fat) + fat_count:
        fat_count += 1
    fat_start = len(fat)
    fat += [_FAT] * fat_count
    fat += [_FREE] * (fat_count * 128 - len(fat))
    body += struct.pack(f"<{len(fat)}I", *fat)
    difat = list(range(fat_start, fat_start + fat_count)) + [_FREE] * (109 - fat_count)
    header = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 16
              + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6) + b"\0" * 6
              + struct.pack("<IIIIIIIII", 0, fat_count, directory_start, 0, _CUTOFF,
                            mini_fat_start, mini_fat_count, _END, 0)
              + struct.pack("<109I", *difat))
    return header + bytes(body)


def write_ole(path, streams: dict[str, bytes]):
    path.write_bytes(ole_bytes(streams))
    return path


# ---------- SchLib ----------

def binary_record(body: bytes) -> bytes:
    """A schematic record of type 1 (binary): 24-bit length, then the type byte."""
    return struct.pack("<I", len(body) | 1 << 24) + body


def _pascal(text: str) -> bytes:
    data = text.encode("cp1252")
    return bytes([len(data)]) + data


def binary_pin(designator: str, name: str, electrical: int = 4, part: int = 1, mode: int = 0,
               x: int = 0, y: int = 0, length: int = 30, conglomerate: int = 0x18,
               description: str = "") -> bytes:
    """A SchLib binary pin record (see sch_lib.decode_binary_pin); x, y and length in
    10 mil units."""
    body = struct.pack("<ibhB", 2, 0, part, mode) + b"\0" * 4 + _pascal(description)
    body += bytes([1, electrical, conglomerate]) + struct.pack("<hhhi", length, x, y, 0)
    body += _pascal(name) + _pascal(designator) + _pascal("") + _pascal("") + _pascal("")
    return binary_record(body)


def text_pin(designator: str, name: str, electrical: int = 4, part: int = 1, mode: int = 0,
             x: int = 0, y: int = 0, length: int = 30, conglomerate: int = 0x18) -> bytes:
    """A text RECORD=2 pin, as older libraries write them."""
    return record(f"|RECORD=2|OWNERPARTID={part}|OWNERPARTDISPLAYMODE={mode}"
                  f"|ELECTRICAL={electrical}|PINCONGLOMERATE={conglomerate}|PINLENGTH={length}"
                  f"|LOCATION.X={x}|LOCATION.Y={y}|NAME={name}|DESIGNATOR={designator}")


def sch_component(lib_reference: str, pins: list[bytes], description: str = "",
                  parts: int = 1, designator: str = "U?", params: dict | None = None,
                  footprints=(), design_item_id: str | None = None, extra: str = "") -> bytes:
    """A component's Data stream: RECORD=1, its designator, parameters, model list and
    footprints (name, current, library, from component library), then the pins.
    footprints entries are names or (name, current, library, use component library)."""
    data = record(f"|RECORD=1|LIBREFERENCE={lib_reference}|COMPONENTDESCRIPTION={description}"
                  f"|PARTCOUNT={parts + 1}|DISPLAYMODECOUNT=1|CURRENTPARTID=1"
                  f"|DESIGNITEMID={design_item_id or lib_reference}{extra}")
    data += record(f"|RECORD=34|NAME=Designator|TEXT={designator}")
    for name, text in (params or {}).items():
        data += record(f"|RECORD=41|NAME={name}|TEXT={text}")
    if footprints:
        data += record("|RECORD=44")
        for footprint in footprints:
            name, current, library, own = (footprint, True, "", False) if isinstance(
                footprint, str) else footprint
            fields = f"|MODELDATAFILE0={library}" if library else ""
            fields += "|USECOMPONENTLIBRARY=T" if own else ""
            data += record(f"|RECORD=45|MODELNAME={name}|MODELTYPE=PCBLIB"
                           f"|ISCURRENT={'T' if current else 'F'}|DATAFILECOUNT=1{fields}")
    return data + b"".join(pins)


def storage_of(name: str) -> str:
    """The storage name Altium gives a library item: '/' as '_', cut to 31 characters."""
    return name.replace("/", "_")[:31]


def sch_lib_streams(*components: tuple[str, bytes],
                    aliases: dict[str, list[str]] | None = None) -> dict[str, bytes]:
    """A SchLib's streams: FileHeader listing the components ((lib reference, Data
    stream) pairs) and one storage per component."""
    head = ("|HEADER=Protel for Windows - Schematic Library Editor Binary File Version 5.0"
            f"|COMPCOUNT={len(components)}")
    for index, (name, _) in enumerate(components):
        head += f"|LIBREF{index}={name}|COMPDESCR{index}=|PARTCOUNT{index}=2"
        for j, alias in enumerate((aliases or {}).get(name, [])):
            head += f"|ALIASCOUNT{index}={j + 1}|COMP{index}ALIAS{j}={alias}"
    streams = {"FileHeader": record(head), "Storage": record("|HEADER=Icon storage")}
    for name, data in components:
        streams[f"{storage_of(name)}/Data"] = data
    return streams


# ---------- PcbLib ----------

def _units(mil: float) -> int:
    return round(mil * 10_000)


def _primitive(record_type: int, payload: bytes) -> bytes:
    return bytes([record_type]) + struct.pack("<I", len(payload)) + payload


def text_props(props: dict[str, str]) -> bytes:
    body = ("|" + "|".join(f"{key}={value}" for key, value in props.items())).encode("latin-1")
    return struct.pack("<I", len(body) + 1) + body + b"\0"


def pad(name: str, x: float, y: float, sx: float, sy: float, shape: int = 2, layer: int = 1,
        hole: float = 0.0, rotation: float = 0.0, plated: bool | None = None) -> bytes:
    """A pad record: its name, then five subrecords, the fourth the main one (layer at 0,
    position at 13, top/mid/bottom sizes from 21, hole at 45, shapes at 49, rotation
    at 52, plated at 60)."""
    name_blob = _pascal(name)
    main = bytearray(185)
    main[0] = layer
    struct.pack_into("<HH", main, 3, 0xFFFF, 0xFFFF)
    struct.pack_into("<H", main, 7, 0xFFFF)
    struct.pack_into("<ii", main, 13, _units(x), _units(y))
    struct.pack_into("<iiiiiii", main, 21, *(_units(v) for v in (sx, sy, sx, sy, sx, sy, hole)))
    main[49] = main[50] = main[51] = shape
    struct.pack_into("<d", main, 52, rotation)
    if plated is not None:
        main[60] = int(plated)
    data = b"\x02" + struct.pack("<I", len(name_blob)) + name_blob
    for subrecord in (b"\0", b"\0" * 5, b"\0", bytes(main), b""):
        data += struct.pack("<I", len(subrecord)) + subrecord
    return data


_V7_MECHANICAL = 0x01020000


def _v7(payload: bytearray, at: int, mechanical: int | None) -> None:
    """Write a mechanical layer's V7 layer id at `at`, as newer files do."""
    if mechanical is not None:
        struct.pack_into("<I", payload, at, _V7_MECHANICAL | mechanical)


def track(layer: int, x1: float, y1: float, x2: float, y2: float, width: float = 5.0,
          mechanical: int | None = None) -> bytes:
    """A track record; mechanical: the V7 layer id's Mechanical number (at 41)."""
    payload = bytearray(49)
    payload[0] = layer
    struct.pack_into("<HHH", payload, 3, 0xFFFF, 0xFFFF, 0xFFFF)
    struct.pack_into("<iiiii", payload, 13, *(_units(v) for v in (x1, y1, x2, y2, width)))
    _v7(payload, 41, mechanical)
    return _primitive(4, bytes(payload))


def arc(layer: int, cx: float, cy: float, radius: float, start: float = 0.0,
        end: float = 360.0, width: float = 5.0, mechanical: int | None = None) -> bytes:
    """An arc record; mechanical: the V7 layer id's Mechanical number (at 52)."""
    payload = bytearray(60)
    payload[0] = layer
    struct.pack_into("<HHH", payload, 3, 0xFFFF, 0xFFFF, 0xFFFF)
    struct.pack_into("<iii", payload, 13, _units(cx), _units(cy), _units(radius))
    struct.pack_into("<dd", payload, 25, start, end)
    struct.pack_into("<i", payload, 41, _units(width))
    _v7(payload, 52, mechanical)
    return _primitive(1, bytes(payload))


def body(vertices: list[tuple[float, float]], layer: int = 58, **props: str) -> bytes:
    """A 3D body record: the region head (layer at 0, component at 7, hole count at 14,
    text length at 18), its text, then u32 count + (double x, double y) vertices."""
    text = ("|" + "|".join(f"{key}={value}" for key, value in props.items())).encode() + b"\0"
    head = bytearray(22)
    head[0] = layer
    struct.pack_into("<HHH", head, 3, 0xFFFF, 0xFFFF, 0xFFFF)
    struct.pack_into("<I", head, 18, len(text))
    ring = struct.pack("<I", len(vertices)) + b"".join(
        struct.pack("<dd", x * 10_000, y * 10_000) for x, y in vertices)
    return _primitive(12, bytes(head) + text + ring)


def text(layer: int, x: float, y: float, string: str, designator: bool = False,
         comment: bool = False, height: float = 60.0) -> bytes:
    head = bytearray(230)
    head[0] = layer
    struct.pack_into("<H", head, 7, 0xFFFF)
    struct.pack_into("<iii", head, 13, _units(x), _units(y), _units(height))
    struct.pack_into("<d", head, 27, 0.0)
    head[40], head[41] = int(comment), int(designator)
    raw = _pascal(string)
    return (b"\x05" + struct.pack("<I", len(head)) + bytes(head)
            + struct.pack("<I", len(raw)) + raw)


def footprint_streams(name: str, primitives: list[bytes], height: str = "0mil",
                      description: str = "") -> dict[str, bytes]:
    """A footprint storage: Data (its name, then the primitives) and Parameters."""
    name_blob = _pascal(name)
    storage = storage_of(name)
    return {
        f"{storage}/Data": struct.pack("<I", len(name_blob)) + name_blob + b"".join(primitives),
        f"{storage}/Header": struct.pack("<I", len(primitives)),
        f"{storage}/Parameters": text_props({"PATTERN": name, "HEIGHT": height,
                                             "DESCRIPTION": description, "ITEMGUID": "",
                                             "REVISIONGUID": ""}),
    }


def pcb_lib_streams(*footprints: dict[str, bytes], names: list[str],
                    layer_props: dict[str, str] | None = None) -> dict[str, bytes]:
    """A PcbLib's streams: FileHeader, Library/Data (layer table props, then the
    footprint names) and the footprints' storages (footprint_streams)."""
    listing = struct.pack("<I", len(names))
    for name in names:
        blob = _pascal(name)
        listing += struct.pack("<I", len(blob)) + blob
    streams = {
        "FileHeader": struct.pack("<I", 27) + _pascal("PCB 6.0 Binary Library File"),
        "Library/Header": struct.pack("<I", 1),
        "Library/Data": text_props({"FILENAME": "made-up.PcbLib",
                                    "KIND": "Protel_Advanced_PCB_Library",
                                    **(layer_props or {})}) + listing,
    }
    for footprint in footprints:
        streams.update(footprint)
    return streams


# ---------- IntLib ----------

def embed(streams: dict[str, bytes]) -> bytes:
    """An IntLib's embedded library: one byte (2), then the library's OLE file deflated."""
    return b"\x02" + zlib.compress(ole_bytes(streams))


def int_lib_streams(sch_lib: dict[str, bytes] | None = None,
                    pcb_lib: dict[str, bytes] | None = None) -> dict[str, bytes]:
    streams = {"Version.Txt": b"\x00\x02\x00\x00\x00", "LibCrossRef.Txt": b"\x00" * 5,
               "CKT/0.ckt": b"\x02" + zlib.compress(b"* model")}
    if sch_lib is not None:
        streams["SchLib/0.schlib"] = embed(sch_lib)
    if pcb_lib is not None:
        streams["PCBLib/0.PcbLib"] = embed(pcb_lib)
    return streams


def region(vertices: list[tuple[float, float]], layer: int = 1, kind: int = 0,
           v7_layer: str | None = None) -> bytes:
    """A footprint region record (type 11, the Regions6 layout): the region head (layer
    at 0, component at 7, hole count at 14, text length at 18), its text (KIND, and
    V7_LAYER when given), then u32 count + (double x, double y) vertices."""
    text = (f"|KIND={kind}" + (f"|V7_LAYER={v7_layer}" if v7_layer else "")).encode() + b"\0"
    head = bytearray(22)
    head[0] = layer
    struct.pack_into("<HHH", head, 3, 0xFFFF, 0xFFFF, 0xFFFF)
    struct.pack_into("<I", head, 18, len(text))
    ring = struct.pack("<I", len(vertices)) + b"".join(
        struct.pack("<dd", x * 10_000, y * 10_000) for x, y in vertices)
    return _primitive(11, bytes(head) + text + ring)


def fill(layer: int, x1: float, y1: float, x2: float, y2: float, rotation: float = 0.0,
         mechanical: int | None = None) -> bytes:
    """A fill record (type 6): layer at 0, net, ? and component u16 at 3, 5 and 7, the
    corners (int32 x, y) at 13 and 21, rotation (double) at 29, V7 layer id at 42."""
    payload = bytearray(50)
    payload[0] = layer
    struct.pack_into("<HHH", payload, 3, 0xFFFF, 0xFFFF, 0xFFFF)
    struct.pack_into("<iiii", payload, 13, *(_units(v) for v in (x1, y1, x2, y2)))
    struct.pack_into("<d", payload, 29, rotation)
    _v7(payload, 42, mechanical)
    return _primitive(6, bytes(payload))
