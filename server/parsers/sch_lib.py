"""Parser for Altium .SchLib schematic libraries (OLE compound files).

Pure parsing only: OLE streams -> dataclasses. A SchLib holds a FileHeader stream (the
library's component list: LibRef0..N, CompDescr, PartCount, aliases) and one storage
per component whose Data stream carries the component's records in the .SchDoc record
format (sch_doc.read_records), without OwnerIndex: RECORD=1 the component, 34 its
designator, 41 parameters, 44/45 its model list and models, 2 pins. Newer libraries
write pins as binary records (decode_binary_pin); older ones as text RECORD=2.

Lengths and positions are in mil (schematic units of 10 mil converted).
"""

import struct
from dataclasses import dataclass, field

import olefile

try:
    from parsers.pcb_doc import component_kind
    from parsers.sch_doc import PIN_TYPES, coord_mil, pin_end, pin_type, read_records
except ImportError:
    from server.parsers.pcb_doc import component_kind
    from server.parsers.sch_doc import PIN_TYPES, coord_mil, pin_end, pin_type, read_records

__all__ = ["PIN_TYPES", "LibPin", "ModelLink", "SchLibComponent", "SchLib",
           "decode_binary_pin", "parse_sch_lib", "sch_lib_from_ole", "storage_name"]

# PinConglomerate bits (KiCad's ASCH_PIN reads the same): 0-1 orientation (0 right,
# 1 up, 2 left, 3 down, the direction the pin points away from the body), 0x04 hidden,
# 0x08 name shown, 0x10 designator shown.
_PIN_HIDDEN = 0x04


@dataclass
class LibPin:
    """One pin record of a library component (mil; location is where the pin meets
    the body, end its electrical end)."""

    designator: str
    name: str
    electrical: str  # a PIN_TYPES name, or the value as written when unknown
    part: int  # OwnerPartId: 1 = part A, ...; 0 a pin every part shares
    display_mode: int  # OwnerPartDisplayMode: 0 normal, 1.. alternate graphics
    hidden: bool
    x: float
    y: float
    length: float
    orientation: int  # 0 right, 1 up, 2 left, 3 down
    end_x: float
    end_y: float
    description: str = ""
    swap_group: str = ""  # SwapIdGroup / pin swapping group, as written


@dataclass
class ModelLink:
    """A RECORD=45 model of a library component: a footprint (PCBLIB), a 3D model
    (PCB3DLIB), a simulation model (SIM) and so on."""

    model_type: str  # MODELTYPE upper-cased: PCBLIB, PCB3DLIB, SIM, SI, ...
    name: str  # MODELNAME
    current: bool = False  # IsCurrent: the footprint used when the part is placed
    description: str = ""
    # The library the link names (ModelDataFile0: a file name or path; empty when the
    # link searches every available library) and whether it takes the model from the
    # component's own library (UseComponentLibrary=T: an IntLib or Workspace part).
    library: str = ""
    from_component_library: bool = False
    workspace: bool = False  # MODELVAULTGUID set: a Workspace model


@dataclass
class SchLibComponent:
    """One library component (symbol)."""

    lib_reference: str
    storage: str  # the OLE storage it was read from
    description: str = ""
    design_item_id: str = ""
    # PartCount as Altium writes it is the number of parts + 1; this is the parts.
    part_count: int = 1
    display_mode_count: int = 1
    kind: int = 0  # ComponentKind, see sch_doc.ComponentMeta.kind
    designator: str = ""  # the default designator (U?, R?)
    comment: str = ""
    # Parameters as written: name -> text; the first of a name (any case) wins.
    parameters: dict[str, str] = field(default_factory=dict)
    models: list[ModelLink] = field(default_factory=list)
    pins: list[LibPin] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    # The component record's SourceLibraryName (the library it was last saved from),
    # and whether it is a Workspace item (VAULTGUID or ITEMGUID set).
    source_library: str = ""
    workspace: bool = False

    @property
    def footprints(self) -> list[ModelLink]:
        return [model for model in self.models if model.model_type == "PCBLIB"]

    @property
    def current_footprint(self) -> ModelLink | None:
        footprints = self.footprints
        return next((model for model in footprints if model.current),
                    footprints[0] if footprints else None)


@dataclass
class SchLib:
    path: str
    components: list[SchLibComponent] = field(default_factory=list)
    # FileHeader's LibRef list in file order (what the Libraries panel shows), and
    # each LibRef's aliases (CompNAliasM).
    header_names: list[str] = field(default_factory=list)
    aliases: dict[str, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def find(self, name: str) -> SchLibComponent | None:
        """The component whose library reference (or alias) is name: an exact match
        first, then one that differs only in case, as Altium's library search."""
        if not name:
            return None
        exact = next((c for c in self.components
                      if c.lib_reference == name or name in c.aliases), None)
        if exact:
            return exact
        folded = name.casefold()
        return next((c for c in self.components
                     if c.lib_reference.casefold() == folded
                     or any(alias.casefold() == folded for alias in c.aliases)), None)

    @property
    def names(self) -> list[str]:
        return [component.lib_reference for component in self.components]


def storage_name(name: str) -> str:
    """The OLE storage name a library gives a component or footprint called name
    (case-folded): '/' becomes '_' and the name is cut to OLE's 31 characters. Checked
    on every library read; the reader falls back to reading every storage."""
    return name.casefold().replace("/", "_")[:31]


def _pascal(body: bytes, at: int) -> tuple[str, int]:
    """A u8-length string at `at` (Windows-1252) and the offset after it."""
    length = body[at]
    end = at + 1 + length
    if end > len(body):
        raise ValueError("string past the record")
    return body[at + 1 : end].decode("cp1252", errors="replace"), end


def decode_binary_pin(body: bytes) -> dict[str, str] | None:
    """A binary pin record of a SchLib Data stream as the properties a text RECORD=2 pin
    has (lower-case keys), or None for another binary record.

    Layout (KiCad's SchLib reader reads the same; checked on Altium's AD25 example
    libraries): int32 record 2, a byte, int16 OwnerPartId, u8 OwnerPartDisplayMode,
    four symbol bytes (inner edge, outer edge, inside, outside), the description, a
    byte (formal type), u8 Electrical, u8 PinConglomerate, int16 PinLength, int16
    Location.X and .Y (10 mil units), int32 colour, then the name, the designator, the
    swap group and the part-and-sequence text, each a u8-length string.
    """
    try:
        (record,) = struct.unpack_from("<i", body, 0)
        if record != 2:
            return None
        (owner_part,) = struct.unpack_from("<h", body, 5)
        display_mode = body[7]
        description, at = _pascal(body, 12)
        electrical = body[at + 1]
        conglomerate = body[at + 2]
        length, x, y = struct.unpack_from("<hhh", body, at + 3)
        name, at = _pascal(body, at + 13)
        designator, at = _pascal(body, at)
        swap_group = _pascal(body, at)[0] if at < len(body) else ""
    except (struct.error, IndexError, ValueError):
        return None
    return {
        "record": "2",
        "ownerpartid": str(owner_part),
        "ownerpartdisplaymode": str(display_mode),
        "description": description,
        "electrical": str(electrical),
        "pinconglomerate": str(conglomerate),
        "pinlength": str(length),
        "location.x": str(x),
        "location.y": str(y),
        "name": name,
        "designator": designator,
        "swapidgroup": swap_group,
    }


def _int(text: str | None, default: int = 0) -> int:
    try:
        return int((text or "").strip())
    except ValueError:
        return default


def _pin(props: dict[str, str]) -> LibPin:
    conglomerate = _int(props.get("pinconglomerate"))
    end_x, end_y = pin_end(props, conglomerate)
    return LibPin(
        designator=props.get("designator", ""),
        name=props.get("name", ""),
        electrical=pin_type(props),
        part=_int(props.get("ownerpartid"), 0),
        display_mode=_int(props.get("ownerpartdisplaymode"), 0),
        hidden=bool(conglomerate & _PIN_HIDDEN),
        x=coord_mil(props, "location.x"),
        y=coord_mil(props, "location.y"),
        length=coord_mil(props, "pinlength"),
        orientation=conglomerate & 0x03,
        end_x=end_x,
        end_y=end_y,
        description=props.get("description", ""),
        swap_group=props.get("swapidgroup", ""),
    )


def _model(props: dict[str, str]) -> ModelLink:
    return ModelLink(
        model_type=props.get("modeltype", "").strip().upper(),
        name=props.get("modelname", ""),
        current=props.get("iscurrent", "").strip().upper() == "T",
        description=props.get("description", ""),
        library=props.get("modeldatafile0", ""),
        from_component_library=props.get("usecomponentlibrary", "").strip().upper() == "T",
        workspace=bool(props.get("modelvaultguid")),
    )


def component_from_records(records: list[dict[str, str]], storage: str = "") -> SchLibComponent | None:
    """The component a SchLib storage's Data stream holds (read_records with
    decode_binary_pin), or None when its first record is not a RECORD=1."""
    head = next((props for props in records if props), None)
    if not head or head.get("record") != "1":
        return None
    component = SchLibComponent(
        lib_reference=head.get("libreference", "") or storage,
        storage=storage,
        description=head.get("componentdescription", ""),
        design_item_id=head.get("designitemid", ""),
        part_count=max(_int(head.get("partcount"), 2) - 1, 1),
        display_mode_count=max(_int(head.get("displaymodecount"), 1), 1),
        kind=component_kind(head.get("componentkind", ""),
                            head.get("componentkindversion2", ""),
                            head.get("componentkindversion3", "")),
        source_library=head.get("sourcelibraryname", ""),
        workspace=bool(head.get("vaultguid") or head.get("itemguid")),
    )
    folded: set[str] = set()
    for props in records:
        kind = props.get("record")
        if kind == "2":
            component.pins.append(_pin(props))
        elif kind == "34" and props.get("name", "").lower() == "designator":
            component.designator = component.designator or props.get("text", "")
        elif kind == "41":
            name, text = props.get("name", ""), props.get("text", "")
            if name and name.casefold() not in folded:
                folded.add(name.casefold())
                component.parameters[name] = text
                if name.casefold() == "comment":
                    component.comment = text
        elif kind == "45" and props.get("modeltype"):
            component.models.append(_model(props))
    return component


def _header(ole: olefile.OleFileIO) -> tuple[list[str], dict[str, list[str]]]:
    """(LibRef names in FileHeader order, LibRef -> alias names)."""
    if not ole.exists("FileHeader"):
        return [], {}
    records = read_records(ole.openstream("FileHeader").read())
    head = records[0] if records else {}
    names: list[str] = []
    aliases: dict[str, list[str]] = {}
    for index in range(_int(head.get("compcount"))):
        name = head.get(f"libref{index}", "")
        if not name:
            continue
        names.append(name)
        count = _int(head.get(f"aliascount{index}"))
        found = [head.get(f"comp{index}alias{j}", "") for j in range(count)]
        if any(found):
            aliases[name] = [alias for alias in found if alias]
    return names, aliases


def sch_lib_from_ole(ole: olefile.OleFileIO, path: str = "",
                     names: set[str] | None = None) -> SchLib:
    """A SchLib from an open OLE file. names (case-folded library references), when
    given, limits the components read to those (storages are matched by storage_name;
    when that misses, the other storages are read to find them)."""
    library = SchLib(path=path)
    library.header_names, library.aliases = _header(ole)
    aliases = library.aliases
    storages = sorted({entry[0] for entry in ole.listdir()
                       if len(entry) == 2 and entry[1] == "Data"})

    def read(storage: str) -> None:
        try:
            records = read_records(ole.openstream(f"{storage}/Data").read(),
                                   binary=decode_binary_pin)
        except Exception as error:  # one unreadable storage leaves the others
            library.warnings.append(f"{storage}: {type(error).__name__}: {error}")
            return
        component = component_from_records(records, storage)
        if component is not None:
            component.aliases = aliases.get(component.lib_reference, [])
            library.components.append(component)

    if names is None:
        for storage in storages:
            read(storage)
        return library
    wanted = {storage_name(name) for name in names}
    for name, alias_list in aliases.items():
        if any(alias.casefold() in names for alias in alias_list):
            wanted.add(storage_name(name))
    first = [storage for storage in storages if storage.casefold() in wanted]
    for storage in first:
        read(storage)
    found = {key.casefold() for component in library.components
             for key in (component.lib_reference, *component.aliases)}
    if names - found:  # a storage name Altium changed: read the others to find it
        for storage in storages:
            if storage not in first:
                read(storage)
        library.components = [
            component for component in library.components
            if {key.casefold() for key in (component.lib_reference, *component.aliases)}
            & names]
    return library


def parse_sch_lib(path: str, names: set[str] | None = None) -> SchLib:
    """Read a .SchLib file (see sch_lib_from_ole for names)."""
    with olefile.OleFileIO(path) as ole:
        return sch_lib_from_ole(ole, path, names)
