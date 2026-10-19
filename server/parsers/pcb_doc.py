"""Parser for Altium .PcbDoc OLE compound files.

Pure parsing only: OLE streams -> dataclasses. No MCP or index logic here.
All lengths and coordinates are converted to float mils at parse time.
"""

import math
import re
import struct
from dataclasses import dataclass, field

import olefile

ALTIUM_UNITS_PER_MIL = 10000
MM_PER_MIL = 0.0254
NO_NET = 0xFFFF
NO_COMPONENT = 0xFFFF

_DIM_RE = re.compile(r"^\s*(-?[\d.]+(?:[eE][+-]?\d+)?)\s*(mil|mm)?\s*$")


def parse_dim_mil(raw: str | None) -> float | None:
    """Parse an Altium dimension string into mils."""
    if not raw:
        return None
    match = _DIM_RE.match(raw)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    if match.group(2) == "mm":
        return value / MM_PER_MIL
    return value


def _parse_text_properties(body: bytes) -> dict[str, str]:
    text = body.rstrip(b"\x00").decode("latin-1", "replace")
    properties: dict[str, str] = {}
    for part in text.split("|"):
        if "=" in part:
            key, _, value = part.partition("=")
            properties[key.strip().upper()] = value
    return properties


def read_text_records(data: bytes) -> list[dict[str, str]]:
    """Decode u32-length-prefixed, pipe-delimited text-property records."""
    records: list[dict[str, str]] = []
    offset = 0
    while offset + 4 <= len(data):
        (record_length,) = struct.unpack_from("<I", data, offset)
        if record_length == 0 or offset + 4 + record_length > len(data):
            break
        records.append(
            _parse_text_properties(
                data[offset + 4 : offset + 4 + record_length]
            )
        )
        offset += 4 + record_length
    return records


def read_kind_prefixed_text_records(
    data: bytes,
) -> tuple[list[tuple[int, dict[str, str]]], int]:
    """Decode Rules6-style records: u16 kind id, u32 length, text properties.

    Returns (records, trailing_bytes). read_text_records cannot read these:
    it would take the kind id as part of the length and silently stop.
    """
    records: list[tuple[int, dict[str, str]]] = []
    offset = 0
    while offset + 6 <= len(data):
        kind_id, record_length = struct.unpack_from("<HI", data, offset)
        if record_length == 0 or offset + 6 + record_length > len(data):
            break
        records.append(
            (
                kind_id,
                _parse_text_properties(
                    data[offset + 6 : offset + 6 + record_length]
                ),
            )
        )
        offset += 6 + record_length
    return records, len(data) - offset


@dataclass
class Primitive:
    """One copper or geometry primitive normalized to mils."""

    kind: str
    layer: int
    net: int | None
    component: int | None
    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    height: float | None = None
    rotation: float = 0.0
    shape: int | None = None
    source_id: int | None = None
    layer_to: int | None = None
    hole: float | None = None
    pad_name: str | None = None
    pour_id: int | None = None
    # True copper length of an arc chord (r * sweep / steps); the chord itself
    # is shorter. None for other primitives: their length is the segment's.
    length: float | None = None
    # Pad Pin/Package Length (pad to die, mil): 0.0 when unset, None when the
    # pad record is too short to carry it (and for non-pad primitives).
    pin_package: float | None = None


def iter_binary_records(data: bytes):
    """Yield (record_type, payload) from u8-type/u32-length records."""
    offset = 0
    while offset + 5 <= len(data):
        record_type = data[offset]
        (record_length,) = struct.unpack_from("<I", data, offset + 1)
        if offset + 5 + record_length > len(data):
            break
        yield (
            record_type,
            data[offset + 5 : offset + 5 + record_length],
        )
        offset += 5 + record_length


def _units_to_mil(units: int) -> float:
    return units / ALTIUM_UNITS_PER_MIL


def _optional_index(value: int, sentinel: int) -> int | None:
    return None if value == sentinel else value


def _valid_layer(layer: int) -> bool:
    return 0 < layer <= 82


def decode_tracks(data: bytes) -> tuple[list[Primitive], int]:
    """Decode Tracks6 records."""
    primitives: list[Primitive] = []
    skipped = 0
    for record_type, payload in iter_binary_records(data):
        if (
            record_type != 4
            or len(payload) < 33
            or not _valid_layer(payload[0])
        ):
            skipped += 1
            continue
        net = struct.unpack_from("<H", payload, 3)[0]
        component = struct.unpack_from("<H", payload, 7)[0]
        x1, y1, x2, y2, width = struct.unpack_from(
            "<iiiii",
            payload,
            13,
        )
        if width <= 0:
            skipped += 1
            continue
        primitives.append(
            Primitive(
                kind="track",
                layer=payload[0],
                net=_optional_index(net, NO_NET),
                component=_optional_index(component, NO_COMPONENT),
                x1=_units_to_mil(x1),
                y1=_units_to_mil(y1),
                x2=_units_to_mil(x2),
                y2=_units_to_mil(y2),
                width=_units_to_mil(width),
            )
        )
    return primitives, skipped


def decode_vias(data: bytes) -> tuple[list[Primitive], int]:
    """Decode Vias6 records."""
    primitives: list[Primitive] = []
    skipped = 0
    for record_type, payload in iter_binary_records(data):
        if record_type != 3 or len(payload) < 31:
            skipped += 1
            continue
        layer_from, layer_to = payload[29], payload[30]
        if not (_valid_layer(layer_from) and _valid_layer(layer_to)):
            skipped += 1
            continue
        net = struct.unpack_from("<H", payload, 3)[0]
        component = struct.unpack_from("<H", payload, 7)[0]
        x, y, diameter, hole = struct.unpack_from("<iiii", payload, 13)
        if diameter <= 0 or hole < 0 or hole > diameter:
            skipped += 1
            continue
        x_mil = _units_to_mil(x)
        y_mil = _units_to_mil(y)
        primitives.append(
            Primitive(
                kind="via",
                layer=layer_from,
                layer_to=layer_to,
                net=_optional_index(net, NO_NET),
                component=_optional_index(component, NO_COMPONENT),
                x1=x_mil,
                y1=y_mil,
                x2=x_mil,
                y2=y_mil,
                width=_units_to_mil(diameter),
                hole=_units_to_mil(hole),
            )
        )
    return primitives, skipped


ARC_CHORD_STEP_DEG = 30.0
ARC_MAX_SAGITTA_MIL = 0.25


def decode_arcs(data: bytes) -> tuple[list[Primitive], int]:
    """Decode Arcs6 records into chord segments for proximity math."""
    primitives: list[Primitive] = []
    skipped = 0
    for arc_id, (record_type, payload) in enumerate(
        iter_binary_records(data)
    ):
        if (
            record_type != 1
            or len(payload) < 45
            or not _valid_layer(payload[0])
        ):
            skipped += 1
            continue
        try:
            net = struct.unpack_from("<H", payload, 3)[0]
            component = struct.unpack_from("<H", payload, 7)[0]
            center_x, center_y = struct.unpack_from("<ii", payload, 13)
            (radius,) = struct.unpack_from("<i", payload, 21)
            start_angle, end_angle = struct.unpack_from("<dd", payload, 25)
            (width,) = struct.unpack_from("<i", payload, 41)
        except struct.error:
            skipped += 1
            continue
        if (
            radius <= 0
            or width < 0
            or not math.isfinite(start_angle)
            or not math.isfinite(end_angle)
        ):
            skipped += 1
            continue
        if width == 0 and net == NO_NET:
            # Component/mechanical outline arcs can legitimately be zero-width
            # and netless; they are not copper primitives for these tools.
            continue
        if width == 0:
            skipped += 1
            continue
        center_x_mil = _units_to_mil(center_x)
        center_y_mil = _units_to_mil(center_y)
        radius_mil = _units_to_mil(radius)
        sweep = (end_angle - start_angle) % 360.0 or 360.0
        adaptive_step = math.degrees(
            2.0
            * math.acos(
                max(
                    -1.0,
                    1.0 - min(ARC_MAX_SAGITTA_MIL / radius_mil, 2.0),
                )
            )
        )
        chord_step = min(
            ARC_CHORD_STEP_DEG,
            max(adaptive_step, 0.5),
        )
        steps = max(2, math.ceil(sweep / chord_step))
        points = [
            (
                center_x_mil
                + radius_mil
                * math.cos(math.radians(start_angle + sweep * index / steps)),
                center_y_mil
                + radius_mil
                * math.sin(math.radians(start_angle + sweep * index / steps)),
            )
            for index in range(steps + 1)
        ]
        chord_length = radius_mil * math.radians(sweep) / steps
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            primitives.append(
                Primitive(
                    kind="arc",
                    layer=payload[0],
                    net=_optional_index(net, NO_NET),
                    component=_optional_index(component, NO_COMPONENT),
                    x1=x1,
                    y1=y1,
                    x2=x2,
                    y2=y2,
                    width=_units_to_mil(width),
                    source_id=arc_id,
                    length=chord_length,
                )
            )
    return primitives, skipped


_PAD_SUBRECORD_COUNT = 5
_PAD_MAIN_INDEX = 3
_PAD_MAIN_MIN_LENGTH = 60
# Pin/Package Length: int32 in internal units (1e-4 mil) at this offset of
# the main subrecord. Seen set only in 194- and 202-byte records (whole-mm
# values on user boards; on the MiniPC example it is what closes the tuned
# groups at the FPGA); 171-, 185- and 186-byte records hold 0 there throughout
# the corpus.
_PAD_PIN_PACKAGE_OFFSET = 158


def decode_pads(data: bytes) -> tuple[list[Primitive], int]:
    """Decode Pads6 multi-subrecord records."""
    primitives: list[Primitive] = []
    skipped = 0
    offset = 0
    while offset + 5 <= len(data):
        record_type = data[offset]
        (name_length,) = struct.unpack_from("<I", data, offset + 1)
        if offset + 5 + name_length > len(data):
            break
        name_blob = data[offset + 5 : offset + 5 + name_length]
        pad_name = (
            name_blob[1 : 1 + name_blob[0]].decode("latin-1", "replace")
            if name_blob
            else ""
        )
        position = offset + 5 + name_length
        subrecords: list[bytes] = []
        truncated = False
        for _ in range(_PAD_SUBRECORD_COUNT):
            if position + 4 > len(data):
                truncated = True
                break
            (subrecord_length,) = struct.unpack_from("<I", data, position)
            if position + 4 + subrecord_length > len(data):
                truncated = True
                break
            subrecords.append(
                data[position + 4 : position + 4 + subrecord_length]
            )
            position += 4 + subrecord_length
        if truncated:
            break
        offset = position
        main = subrecords[_PAD_MAIN_INDEX]
        if (
            record_type != 2
            or len(main) < _PAD_MAIN_MIN_LENGTH
            or not _valid_layer(main[0])
        ):
            skipped += 1
            continue
        net = struct.unpack_from("<H", main, 3)[0]
        component = struct.unpack_from("<H", main, 7)[0]
        x, y = struct.unpack_from("<ii", main, 13)
        (
            size_x,
            size_y,
            _mid_size_x,
            _mid_size_y,
            _bottom_size_x,
            _bottom_size_y,
            hole,
        ) = struct.unpack_from("<iiiiiii", main, 21)
        shape = main[49]
        (rotation,) = struct.unpack_from("<d", main, 52)
        if size_x <= 0 or size_y <= 0 or hole < 0:
            skipped += 1
            continue
        pin_package = None
        if len(main) >= _PAD_PIN_PACKAGE_OFFSET + 4:
            (raw_pin_package,) = struct.unpack_from(
                "<i", main, _PAD_PIN_PACKAGE_OFFSET
            )
            if raw_pin_package >= 0:
                pin_package = _units_to_mil(raw_pin_package)
        x_mil = _units_to_mil(x)
        y_mil = _units_to_mil(y)
        primitives.append(
            Primitive(
                kind="pad",
                layer=main[0],
                net=_optional_index(net, NO_NET),
                component=_optional_index(component, NO_COMPONENT),
                x1=x_mil,
                y1=y_mil,
                x2=x_mil,
                y2=y_mil,
                width=_units_to_mil(size_x),
                height=_units_to_mil(size_y),
                rotation=rotation if math.isfinite(rotation) else 0.0,
                shape=shape,
                hole=_units_to_mil(hole),
                pad_name=pad_name,
                pin_package=pin_package,
            )
        )
    return primitives, skipped


@dataclass
class PcbLayer:
    """One physical stackup or legacy board layer."""

    v6_id: int | None
    name: str
    kind: str
    stack_order: int | None
    copper_thick_mil: float | None = None
    diel_height_mil: float | None = None
    diel_const: float | None = None
    material: str | None = None
    v7_id: int | None = None


@dataclass
class ImpedanceLayer:
    """One signal layer of an impedance profile (Layer Stack Manager).

    Lengths in mil; None for unset or unbounded (Altium writes int32 max).
    """

    layer_v7: int | None
    ref_top_v7: int | None
    ref_bottom_v7: int | None
    substack_id: str | None
    width_mil: float | None
    min_width_mil: float | None
    max_width_mil: float | None
    gap_mil: float | None
    min_gap_mil: float | None
    max_gap_mil: float | None
    calculated_ohm: float | None
    error_pct: float | None
    propagation_speed: float | None  # apparently m/ns (= mm/ps)
    enabled: bool


@dataclass
class ImpedanceProfile:
    """A Layer Stack Manager impedance profile and its per-layer solutions."""

    id: str
    name: str
    display_name: str | None
    target_ohm: float | None
    tolerance_pct: float | None
    differential: bool
    layers: list[ImpedanceLayer] = field(default_factory=list)


@dataclass
class BoardInfo:
    """Board coordinate system and layer metadata."""

    origin_x: float | None
    origin_y: float | None
    sheet_x: float | None
    sheet_y: float | None
    sheet_width: float | None
    sheet_height: float | None
    display_unit: str
    layers: list[PcbLayer]
    stackup_source: str
    outline_vertices: list[tuple[float, float]] = field(
        default_factory=list
    )
    impedance_profiles: list[ImpedanceProfile] = field(default_factory=list)
    # Layer-stack regions (rigid/flex); each {"id", "name", "is_flex"}.
    substacks: list[dict] = field(default_factory=list)


@dataclass
class PcbComponent:
    """A Components6 placement record."""

    index: int
    refdes: str | None
    source_uid_tail: str | None
    channel_path: str | None
    x: float | None
    y: float | None
    rotation: float | None
    side: str
    footprint: str | None = None
    # The designator shown on the board (Texts6). Differs from refdes
    # (SOURCEDESIGNATOR) on multi-channel and board-annotated designs; class
    # members and rule scopes use it.
    designator: str | None = None


@dataclass
class PolygonPour:
    """A polygon-pour outline and its copper identity."""

    pour_id: int
    net: int | None
    layer: int | None
    hatch_style: str | None
    vertices: list[tuple[float, float]]


# Altium writes int32 max (214748.3647 mil) for an unbounded max width/gap.
_UNBOUNDED_MIL = 214748.0


def _optional_int(raw: str | None) -> int | None:
    try:
        return int(raw) if raw not in (None, "") else None
    except ValueError:
        return None


def _optional_float(raw: str | None) -> float | None:
    try:
        return float(raw) if raw not in (None, "") else None
    except ValueError:
        return None


def _bounded_mil(raw: str | None) -> float | None:
    value = parse_dim_mil(raw)
    return None if value is None or value >= _UNBOUNDED_MIL else value


def decode_impedance_profiles(properties: dict[str, str]) -> list[ImpedanceProfile]:
    """Decode V9_IMPEDANCEPROFILEn_* and their V9_TRACEIMPEDANCEn_* layers."""
    profiles: dict[str, ImpedanceProfile] = {}
    index = 0
    while f"V9_IMPEDANCEPROFILE{index}_ID" in properties:
        prefix = f"V9_IMPEDANCEPROFILE{index}_"
        profile_id = properties[f"{prefix}ID"]
        profiles[profile_id.upper()] = ImpedanceProfile(
            id=profile_id,
            name=properties.get(f"{prefix}NAME") or profile_id,
            display_name=properties.get(f"{prefix}DISPLAY_NAME") or None,
            target_ohm=_optional_float(properties.get(f"{prefix}IMPEDANCE")),
            tolerance_pct=_optional_float(
                properties.get(f"{prefix}IMPEDANCE_TOLERANCE")
            ),
            differential=properties.get(f"{prefix}ISDIFFPAIR", "").upper() == "TRUE",
        )
        index += 1
    index = 0
    while f"V9_TRACEIMPEDANCE{index}_PROFILE_ID" in properties:
        prefix = f"V9_TRACEIMPEDANCE{index}_"
        index += 1
        profile = profiles.get(properties[f"{prefix}PROFILE_ID"].upper())
        if profile is None:
            continue
        profile.layers.append(
            ImpedanceLayer(
                layer_v7=_optional_int(properties.get(f"{prefix}LAYER_V7ID")),
                ref_top_v7=_optional_int(properties.get(f"{prefix}REF_TOP_V7ID")),
                ref_bottom_v7=_optional_int(properties.get(f"{prefix}REF_BOT_V7ID")),
                substack_id=properties.get(f"{prefix}SUBSTACK_ID") or None,
                width_mil=_bounded_mil(properties.get(f"{prefix}TRACE_WIDTH")),
                min_width_mil=_bounded_mil(properties.get(f"{prefix}TRACE_MINWIDTH")),
                max_width_mil=_bounded_mil(properties.get(f"{prefix}TRACE_MAXWIDTH")),
                gap_mil=_bounded_mil(properties.get(f"{prefix}DIFF_PAIR_GAP")),
                min_gap_mil=_bounded_mil(properties.get(f"{prefix}DIFF_PAIR_MINGAP")),
                max_gap_mil=_bounded_mil(properties.get(f"{prefix}DIFF_PAIR_MAXGAP")),
                calculated_ohm=_optional_float(properties.get(f"{prefix}CALC_IMPEDANCE")),
                error_pct=_optional_float(properties.get(f"{prefix}IMPEDANCE_ERR")),
                propagation_speed=_optional_float(
                    properties.get(f"{prefix}PROPAGATION_SPEED")
                ),
                enabled=properties.get(f"{prefix}ENABLED", "TRUE").upper() != "FALSE",
            )
        )
    return list(profiles.values())


def decode_board(records: list[dict[str, str]]) -> BoardInfo:
    """Decode Board6 text properties into coordinate and stackup metadata."""
    properties = records[0] if records else {}
    name_to_v6: dict[str, int] = {}
    for key, value in properties.items():
        match = re.fullmatch(r"LAYER(\d+)NAME", key)
        if match:
            name_to_v6[value.lower()] = int(match.group(1))

    layers: list[PcbLayer] = []
    order = 0
    stackup_source = "legacy"
    while True:
        prefix = f"V9_STACK_LAYER{order}_"
        if f"{prefix}NAME" not in properties:
            break
        stackup_source = "v9"
        name = properties[f"{prefix}NAME"]
        copper_thickness = parse_dim_mil(
            properties.get(f"{prefix}COPTHICK")
        )
        dielectric_height = parse_dim_mil(
            properties.get(f"{prefix}DIELHEIGHT")
        )
        if copper_thickness is not None:
            kind = "copper"
        elif (
            dielectric_height is not None
            or f"{prefix}DIELTYPE" in properties
        ):
            kind = "dielectric"
        else:
            kind = "non-stack"
        dielectric_constant = properties.get(f"{prefix}DIELCONST")
        layers.append(
            PcbLayer(
                v6_id=name_to_v6.get(name.lower()),
                name=name,
                kind=kind,
                stack_order=order,
                copper_thick_mil=copper_thickness,
                diel_height_mil=dielectric_height,
                diel_const=(
                    float(dielectric_constant)
                    if dielectric_constant
                    else None
                ),
                material=properties.get(f"{prefix}DIELMATERIAL"),
                v7_id=_optional_int(properties.get(f"{prefix}LAYERID")),
            )
        )
        order += 1

    stack_names = {layer.name.lower() for layer in layers}
    for layer_name, v6_id in sorted(
        name_to_v6.items(),
        key=lambda item: item[1],
    ):
        if layer_name not in stack_names:
            layers.append(
                PcbLayer(
                    v6_id=v6_id,
                    name=properties[f"LAYER{v6_id}NAME"],
                    kind="non-stack",
                    stack_order=None,
                )
            )

    if stackup_source == "legacy":
        layers = [
            PcbLayer(
                v6_id=v6_id,
                name=properties[f"LAYER{v6_id}NAME"],
                kind="copper" if v6_id <= 32 else "non-stack",
                stack_order=None,
            )
            for _layer_name, v6_id in sorted(
                name_to_v6.items(),
                key=lambda item: item[1],
            )
        ]

    outline_vertices: list[tuple[float, float]] = []
    vertex_index = 0
    while (
        f"VX{vertex_index}" in properties
        and f"VY{vertex_index}" in properties
    ):
        x = parse_dim_mil(properties[f"VX{vertex_index}"])
        y = parse_dim_mil(properties[f"VY{vertex_index}"])
        if x is not None and y is not None:
            outline_vertices.append((x, y))
        vertex_index += 1

    substacks: list[dict] = []
    substack_index = 0
    while f"V9_SUBSTACK{substack_index}_ID" in properties:
        prefix = f"V9_SUBSTACK{substack_index}_"
        substacks.append(
            {
                "id": properties[f"{prefix}ID"],
                "name": properties.get(f"{prefix}NAME")
                or properties[f"{prefix}ID"],
                "is_flex": properties.get(f"{prefix}ISFLEX", "").upper()
                == "TRUE",
            }
        )
        substack_index += 1

    return BoardInfo(
        origin_x=parse_dim_mil(properties.get("ORIGINX")),
        origin_y=parse_dim_mil(properties.get("ORIGINY")),
        sheet_x=parse_dim_mil(properties.get("SHEETX")),
        sheet_y=parse_dim_mil(properties.get("SHEETY")),
        sheet_width=parse_dim_mil(properties.get("SHEETWIDTH")),
        sheet_height=parse_dim_mil(properties.get("SHEETHEIGHT")),
        display_unit=(
            "mil" if properties.get("DISPLAYUNIT", "1") == "1" else "mm"
        ),
        layers=layers,
        stackup_source=stackup_source,
        outline_vertices=outline_vertices,
        substacks=substacks,
        impedance_profiles=decode_impedance_profiles(properties),
    )


def decode_nets(records: list[dict[str, str]]) -> list[str]:
    """Decode Nets6 record order into the PCB net-name table."""
    return [
        record.get("NAME", f"__unnamed_{index}")
        for index, record in enumerate(records)
    ]


def decode_components(
    records: list[dict[str, str]],
) -> list[PcbComponent]:
    """Decode Components6 placement and schematic-source metadata."""
    components: list[PcbComponent] = []
    for index, record in enumerate(records):
        source_uid = record.get("SOURCEUNIQUEID", "")
        rotation_raw = record.get("ROTATION")
        try:
            rotation = float(rotation_raw) if rotation_raw else None
        except ValueError:
            rotation = None
        components.append(
            PcbComponent(
                index=index,
                refdes=record.get("SOURCEDESIGNATOR") or None,
                source_uid_tail=(
                    source_uid.rsplit("\\", 1)[-1] if source_uid else None
                ),
                channel_path=(
                    record.get("SOURCEHIERARCHICALPATH") or None
                ),
                x=parse_dim_mil(record.get("X")),
                y=parse_dim_mil(record.get("Y")),
                rotation=rotation,
                side=(
                    "bottom"
                    if record.get("LAYER", "").upper() == "BOTTOM"
                    else "top"
                ),
                footprint=record.get("PATTERN") or None,
            )
        )
    return components


def decode_polygons(
    records: list[dict[str, str]],
    layer_name_to_v6: dict[str, int],
) -> tuple[list[PolygonPour], list[Primitive]]:
    """Decode pour outlines for containment and proximity queries."""
    pours: list[PolygonPour] = []
    edges: list[Primitive] = []
    for pour_id, record in enumerate(records):
        vertices: list[tuple[float, float]] = []
        vertex_index = 0
        while (
            f"VX{vertex_index}" in record
            and f"VY{vertex_index}" in record
        ):
            x = parse_dim_mil(record[f"VX{vertex_index}"])
            y = parse_dim_mil(record[f"VY{vertex_index}"])
            if x is not None and y is not None:
                vertices.append((x, y))
            vertex_index += 1
        net_raw = record.get("NET")
        layer = layer_name_to_v6.get(record.get("LAYER", "").lower())
        pour = PolygonPour(
            pour_id=pour_id,
            net=int(net_raw) if net_raw and net_raw.isdigit() else None,
            layer=layer,
            hatch_style=record.get("HATCHSTYLE"),
            vertices=vertices,
        )
        pours.append(pour)
        if layer is None or len(vertices) < 3:
            continue
        closed = vertices + [vertices[0]]
        for (x1, y1), (x2, y2) in zip(closed, closed[1:]):
            edges.append(
                Primitive(
                    kind="track",
                    layer=layer,
                    net=pour.net,
                    component=None,
                    x1=x1,
                    y1=y1,
                    x2=x2,
                    y2=y2,
                    width=0.0,
                    pour_id=pour_id,
                )
            )
    return pours, edges


def decode_designators(data: bytes) -> dict[int, str]:
    """Map component index -> designator text from Texts6.

    Each record is u8 type, u32 length, a fixed sub-record (component index
    u16 at offset 7, IsDesignator flag at offset 41), then u32 length and a
    Pascal string holding the text.
    """
    designators: dict[int, str] = {}
    offset = 0
    while offset + 5 <= len(data):
        (first_length,) = struct.unpack_from("<I", data, offset + 1)
        second_at = offset + 5 + first_length
        if second_at + 4 > len(data):
            break
        (second_length,) = struct.unpack_from("<I", data, second_at)
        if second_at + 4 + second_length > len(data):
            break
        first = data[offset + 5 : second_at]
        text = data[second_at + 4 : second_at + 4 + second_length]
        offset = second_at + 4 + second_length
        if first_length < 42 or not first[41]:
            continue
        (component,) = struct.unpack_from("<H", first, 7)
        if component == NO_COMPONENT or not text:
            continue
        designator = text[1 : 1 + text[0]].decode("latin-1", "replace")
        if designator:
            designators.setdefault(component, designator)
    return designators


@dataclass
class PcbRule:
    """One Rules6 design rule (rooms are RoomDefinition rules).

    properties keeps the kind-specific keys verbatim; services/pcb_rules
    turns them into values. Lower priority numbers win within a kind.
    """

    name: str
    kind: str
    kind_id: int
    enabled: bool
    priority: int
    scope1: str
    scope2: str
    net_scope: str | None
    comment: str
    unique_id: str | None
    from_schematic: bool
    properties: dict[str, str] = field(default_factory=dict)


@dataclass
class PcbClass:
    """A Classes6 / SignalClasses object class and its members.

    Superclasses ("All Nets", ...) have implicit membership and no members.
    Component and pad members use PCB designators ("U1", "U1-5").
    """

    name: str
    kind: str
    members: list[str]
    superclass: bool
    auto_generated: bool
    unique_id: str | None


@dataclass
class DiffPair:
    """A DifferentialPairs6 pair; nets are Nets6 names."""

    name: str
    positive_net: str
    negative_net: str
    unique_id: str | None


# Rules6 keys shared by every rule kind; the rest are kind-specific values.
# The primitive-base keys (SELECTION ... UNIONINDEX) carry no rule meaning,
# except LAYER on RoomDefinition, where it is the room's side.
_RULE_GENERIC_KEYS = frozenset({
    "SELECTION", "LAYER", "LOCKED", "POLYGONOUTLINE", "USERROUTED",
    "KEEPOUT", "UNIONINDEX", "RULEKIND", "NETSCOPE", "LAYERKIND",
    "SCOPE1EXPRESSION", "SCOPE2EXPRESSION", "NAME", "ENABLED", "PRIORITY",
    "COMMENT", "UNIQUEID", "DEFINEDBYLOGICALDOCUMENT",
})

# TClassMemberKind ordinals (KIND=); xSignal classes live in SignalClasses.
CLASS_KINDS = {
    0: "net",
    1: "component",
    2: "from_to",
    3: "pad",
    4: "layer",
    5: "design_channel",
    6: "diff_pair",
    7: "polygon",
    8: "split_plane",
    9: "object",
    10: "xsignal",
    11: "xnet",
    12: "xnet_class",
}

_MEMBER_KEY_RE = re.compile(r"M(\d+)")


def decode_rules(
    records: list[tuple[int, dict[str, str]]],
) -> list[PcbRule]:
    """Decode Rules6 records into rules, keeping kind values raw."""
    rules: list[PcbRule] = []
    for kind_id, record in records:
        kind = record.get("RULEKIND") or f"Kind{kind_id}"
        try:
            priority = int(record.get("PRIORITY", ""))
        except ValueError:
            priority = 1_000_000
        properties = {
            key: value
            for key, value in record.items()
            if key not in _RULE_GENERIC_KEYS
        }
        if kind == "RoomDefinition" and "LAYER" in record:
            properties["LAYER"] = record["LAYER"]
        rules.append(
            PcbRule(
                name=record.get("NAME") or f"{kind} #{len(rules) + 1}",
                kind=kind,
                kind_id=kind_id,
                enabled=record.get("ENABLED", "TRUE").upper() != "FALSE",
                priority=priority,
                scope1=record.get("SCOPE1EXPRESSION") or "All",
                scope2=record.get("SCOPE2EXPRESSION") or "All",
                net_scope=record.get("NETSCOPE") or None,
                comment=record.get("COMMENT", ""),
                unique_id=record.get("UNIQUEID") or None,
                from_schematic=(
                    record.get("DEFINEDBYLOGICALDOCUMENT", "").upper()
                    == "TRUE"
                ),
                properties=properties,
            )
        )
    return rules


def decode_classes(records: list[dict[str, str]]) -> list[PcbClass]:
    """Decode Classes6 / SignalClasses records."""
    classes: list[PcbClass] = []
    for record in records:
        kind_raw = record.get("KIND", "")
        kind = (
            CLASS_KINDS.get(int(kind_raw), f"unknown_{kind_raw}")
            if kind_raw.isdigit()
            else f"unknown_{kind_raw}"
        )
        indexed = sorted(
            (int(match.group(1)), value)
            for key, value in record.items()
            if (match := _MEMBER_KEY_RE.fullmatch(key))
        )
        # Duplicate members occur in real files; keep the first occurrence.
        members = list(dict.fromkeys(value for _, value in indexed if value))
        classes.append(
            PcbClass(
                name=record.get("NAME", ""),
                kind=kind,
                members=members,
                superclass=record.get("SUPERCLASS", "").upper() == "TRUE",
                auto_generated=(
                    record.get("AUTOGENERATEDCLASS", "").upper() == "TRUE"
                ),
                unique_id=record.get("UNIQUEID") or None,
            )
        )
    return classes


@dataclass
class XSignal:
    """A Board6 xSignal (PINPAIR*): a pad chain such as U1-W1,R4-1,R4-2,J1-3.

    Consecutive pads on one net are a routed leg; consecutive pads on one
    component are a hop through a series part.
    """

    name: str
    pins: list[str]


def decode_xsignals(properties: dict[str, str]) -> list[XSignal]:
    """Decode the xSignal definitions held in the Board6 record."""
    try:
        count = int(properties.get("PINPAIRCOUNT", "0") or 0)
    except ValueError:
        count = 0
    xsignals: list[XSignal] = []
    for index in range(count):
        name = properties.get(f"PINPAIRNAME{index}")
        pins = [
            pin.strip()
            for pin in properties.get(f"PINPAIRPINS{index}", "").split(",")
            if pin.strip()
        ]
        if name and pins:
            xsignals.append(XSignal(name=name, pins=pins))
    return xsignals


def decode_drc_rule_kinds(records: list[dict[str, str]]) -> set[int] | None:
    """Rule kind ids the board's batch DRC checks (RULESETTOCHECK)."""
    if not records or "RULESETTOCHECK" not in records[0]:
        return None
    return {
        int(part)
        for part in records[0]["RULESETTOCHECK"].split(",")
        if part.strip().isdigit()
    }


def decode_diff_pairs(records: list[dict[str, str]]) -> list[DiffPair]:
    """Decode DifferentialPairs6 records."""
    return [
        DiffPair(
            name=record.get("NAME", ""),
            positive_net=record.get("POSITIVENETNAME", ""),
            negative_net=record.get("NEGATIVENETNAME", ""),
            unique_id=record.get("UNIQUEID") or None,
        )
        for record in records
        if record.get("NAME")
    ]


@dataclass
class PcbData:
    """Parsed PCB content ready for indexing."""

    board: BoardInfo
    nets: list[str]
    components: list[PcbComponent]
    primitives: list[Primitive]
    pours: list[PolygonPour]
    layer_name_to_v6: dict[str, int]
    warnings: list[str] = field(default_factory=list)
    skipped_records: dict[str, int] = field(default_factory=dict)
    rules: list[PcbRule] = field(default_factory=list)
    classes: list[PcbClass] = field(default_factory=list)
    diff_pairs: list[DiffPair] = field(default_factory=list)
    xsignals: list[XSignal] = field(default_factory=list)
    # Rules6 kind ids checked by the board's batch DRC; None when unknown.
    drc_rule_kinds: set[int] | None = None


def _build_layer_aliases(board: BoardInfo) -> dict[str, int]:
    aliases = {
        layer.name.lower(): layer.v6_id
        for layer in board.layers
        if layer.v6_id is not None
    }
    aliases.setdefault("top", 1)
    aliases.setdefault("bottom", 32)
    # Altium's V6 signal-layer numbering is fixed regardless of stack size:
    # 1 = Top, 2..31 = Mid1..Mid30, 32 = Bottom. Polygons6 (and other text
    # streams) reference inner copper layers by this positional alias rather
    # than by LAYERnNAME, so it must be resolvable even though no board we've
    # seen actually populates all 30 mid slots.
    for mid_index in range(1, 31):
        aliases.setdefault(f"mid{mid_index}", mid_index + 1)
    return aliases


def parse_streams(streams: dict[str, bytes]) -> PcbData:
    """Assemble parsed PCB data from raw OLE stream bytes."""
    warnings: list[str] = []
    skipped_records: dict[str, int] = {}

    def text(stream: str) -> list[dict[str, str]]:
        if stream not in streams:
            warnings.append(f"{stream.split('/')[0]} stream missing")
            return []
        try:
            return read_text_records(streams[stream])
        except Exception as error:
            warnings.append(
                f"{stream.split('/')[0]} parse failed: {error}"
            )
            return []

    board_records = text("Board6/Data")
    try:
        board = decode_board(board_records)
    except Exception as error:
        warnings.append(f"Board6 parse failed: {error}")
        board = decode_board([])
    try:
        xsignals = decode_xsignals(board_records[0] if board_records else {})
    except Exception as error:
        warnings.append(f"xSignals parse failed: {error}")
        xsignals = []
    try:
        nets = decode_nets(text("Nets6/Data"))
    except Exception as error:
        warnings.append(f"Nets6 parse failed: {error}")
        nets = []
    try:
        components = decode_components(text("Components6/Data"))
    except Exception as error:
        warnings.append(f"Components6 parse failed: {error}")
        components = []
    layer_aliases = _build_layer_aliases(board)

    primitives: list[Primitive] = []
    binary_decoders = (
        ("Tracks6/Data", decode_tracks),
        ("Vias6/Data", decode_vias),
        ("Arcs6/Data", decode_arcs),
        ("Pads6/Data", decode_pads),
    )
    for stream, decoder in binary_decoders:
        stream_name = stream.split("/")[0]
        if stream not in streams:
            warnings.append(f"{stream_name} stream missing")
            continue
        try:
            decoded, skipped = decoder(streams[stream])
        except Exception as error:
            warnings.append(f"{stream_name} parse failed: {error}")
            continue
        primitives.extend(decoded)
        if skipped:
            skipped_records[stream_name] = skipped

    pours: list[PolygonPour] = []
    if "Polygons6/Data" in streams:
        try:
            pours, edges = decode_polygons(
                text("Polygons6/Data"),
                layer_aliases,
            )
            primitives.extend(edges)
        except Exception as error:
            warnings.append(f"Polygons6 parse failed: {error}")
    else:
        warnings.append("Polygons6 stream missing")

    valid_primitives = []
    invalid_net_count = 0
    for primitive in primitives:
        if primitive.net is not None and primitive.net >= len(nets):
            invalid_net_count += 1
            continue
        valid_primitives.append(primitive)
    if invalid_net_count:
        skipped_records["net_range"] = invalid_net_count

    if "Texts6/Data" in streams:
        try:
            designators = decode_designators(streams["Texts6/Data"])
            for component in components:
                component.designator = designators.get(component.index)
        except Exception as error:
            warnings.append(f"Texts6 parse failed: {error}")

    # Rules, classes and pairs are optional: older or simple boards may lack
    # a stream, and an empty DifferentialPairs6 is normal. Only failures warn.
    rules: list[PcbRule] = []
    if "Rules6/Data" in streams:
        try:
            rule_records, _ = read_kind_prefixed_text_records(
                streams["Rules6/Data"]
            )
            rules = decode_rules(rule_records)
            header = streams.get("Rules6/Header", b"")
            if len(header) >= 4:
                (declared,) = struct.unpack_from("<I", header, 0)
                if declared > len(rules):
                    skipped_records["Rules6"] = declared - len(rules)
        except Exception as error:
            warnings.append(f"Rules6 parse failed: {error}")
    classes: list[PcbClass] = []
    for stream in ("Classes6/Data", "SignalClasses/Data"):
        if stream not in streams:
            continue
        try:
            classes.extend(decode_classes(read_text_records(streams[stream])))
        except Exception as error:
            warnings.append(f"{stream.split('/')[0]} parse failed: {error}")
    drc_rule_kinds: set[int] | None = None
    if "Design Rule Checker Options6/Data" in streams:
        try:
            drc_rule_kinds = decode_drc_rule_kinds(
                read_text_records(streams["Design Rule Checker Options6/Data"])
            )
        except Exception as error:
            warnings.append(f"Design Rule Checker Options6 parse failed: {error}")
    diff_pairs: list[DiffPair] = []
    if "DifferentialPairs6/Data" in streams:
        try:
            diff_pairs = decode_diff_pairs(
                read_text_records(streams["DifferentialPairs6/Data"])
            )
        except Exception as error:
            warnings.append(f"DifferentialPairs6 parse failed: {error}")

    return PcbData(
        board=board,
        nets=nets,
        components=components,
        primitives=valid_primitives,
        pours=pours,
        layer_name_to_v6=layer_aliases,
        warnings=warnings,
        skipped_records=skipped_records,
        rules=rules,
        classes=classes,
        diff_pairs=diff_pairs,
        xsignals=xsignals,
        drc_rule_kinds=drc_rule_kinds,
    )


_WANTED_STREAMS = (
    "Board6/Data",
    "Nets6/Data",
    "Components6/Data",
    "Tracks6/Data",
    "Vias6/Data",
    "Arcs6/Data",
    "Pads6/Data",
    "Polygons6/Data",
    "Texts6/Data",
    "Rules6/Data",
    "Rules6/Header",
    "Classes6/Data",
    "SignalClasses/Data",
    "DifferentialPairs6/Data",
    "Design Rule Checker Options6/Data",
)


def parse_pcb_doc(path: str) -> PcbData:
    """Read a .PcbDoc OLE file and parse the streams used by PCB tools."""
    streams: dict[str, bytes] = {}
    read_warnings: list[str] = []
    with olefile.OleFileIO(path) as ole:
        available = {"/".join(stream) for stream in ole.listdir()}
        for stream in _WANTED_STREAMS:
            if stream in available:
                try:
                    streams[stream] = ole.openstream(stream).read()
                except Exception as error:
                    read_warnings.append(
                        f"{stream.split('/')[0]} read failed: {error}"
                    )
    parsed = parse_streams(streams)
    parsed.warnings = read_warnings + parsed.warnings
    return parsed
