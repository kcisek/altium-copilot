"""Parser for Altium .IntLib integrated libraries (OLE compound files).

An IntLib is an OLE file holding its source libraries compiled in: one stream per
embedded library under a storage named for its kind (SchLib/0.schlib, PCBLib/0.PcbLib;
simulation models under CKT and MDL, 3D models under PCB3DLib), plus LibCrossRef.Txt,
Parameters .bin and Version.Txt. Each embedded stream is one byte (2 on every file
read) followed by a zlib stream; the zlib stream inflates to the library's own OLE
file, read here with sch_lib and pcb_lib. Checked on Altium's installed IntLibs.
"""

import io
import zlib
from dataclasses import dataclass, field

import olefile

try:
    from parsers.pcb_lib import Footprint, PcbLib, pcb_lib_from_ole
    from parsers.sch_lib import SchLib, SchLibComponent, sch_lib_from_ole
except ImportError:
    from server.parsers.pcb_lib import Footprint, PcbLib, pcb_lib_from_ole
    from server.parsers.sch_lib import SchLib, SchLibComponent, sch_lib_from_ole

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_KINDS = {"schlib": "SchLib", "pcblib": "PcbLib"}


@dataclass
class IntLib:
    path: str
    # The embedded libraries read, each with its path set to "<IntLib path>|<stream>".
    sch_libs: list[SchLib] = field(default_factory=list)
    pcb_libs: list[PcbLib] = field(default_factory=list)
    # Every other storage of embedded files (CKT, MDL, PCB3DLib, ...) -> stream count.
    other_models: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def find_component(self, name: str) -> tuple[SchLib, SchLibComponent] | None:
        for library in self.sch_libs:
            component = library.find(name)
            if component:
                return library, component
        return None

    def find_footprint(self, name: str) -> tuple[PcbLib, Footprint] | None:
        for library in self.pcb_libs:
            footprint = library.find(name)
            if footprint:
                return library, footprint
        return None

    @property
    def index(self) -> dict[str, dict[str, list[str]]]:
        """Which embedded library holds what: {"components": {stream: names},
        "footprints": {stream: names}}."""
        return {
            "components": {lib.path.rsplit("|", 1)[-1]: lib.header_names for lib in self.sch_libs},
            "footprints": {lib.path.rsplit("|", 1)[-1]: [fp.name for fp in lib.footprints]
                           for lib in self.pcb_libs},
        }


def decompress_embedded(data: bytes) -> bytes:
    """An embedded library stream's OLE file: the zlib stream after the one-byte prefix
    (data that is already an OLE file is returned as it is). zlib.error when the
    stream is neither."""
    if data.startswith(OLE_MAGIC):
        return data
    return zlib.decompress(data[1:])


def int_lib_from_ole(ole: olefile.OleFileIO, path: str = "",
                     components: set[str] | None = None,
                     footprints: set[str] | None = None) -> IntLib:
    """An IntLib from an open OLE file. components and footprints (case-folded names),
    when given, limit what each embedded library reads (see sch_lib_from_ole and
    pcb_lib_from_ole)."""
    library = IntLib(path=path)
    for entry in ole.listdir():
        if len(entry) != 2:
            continue
        storage, stream = entry
        kind = _KINDS.get(storage.casefold())
        if kind is None:
            library.other_models[storage] = library.other_models.get(storage, 0) + 1
            continue
        name = "/".join(entry)
        try:
            embedded = olefile.OleFileIO(io.BytesIO(
                decompress_embedded(ole.openstream(name).read())))
        except Exception as error:  # one unreadable library leaves the others
            library.warnings.append(f"{name}: {type(error).__name__}: {error}")
            continue
        with embedded:
            if kind == "SchLib":
                library.sch_libs.append(sch_lib_from_ole(embedded, f"{path}|{name}", components))
            else:
                library.pcb_libs.append(pcb_lib_from_ole(embedded, f"{path}|{name}", footprints))
    return library


def parse_int_lib(path: str, components: set[str] | None = None,
                  footprints: set[str] | None = None) -> IntLib:
    """Read an .IntLib file (see int_lib_from_ole)."""
    with olefile.OleFileIO(path) as ole:
        return int_lib_from_ole(ole, path, components, footprints)
