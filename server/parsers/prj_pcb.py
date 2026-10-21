import os
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Variation:
    """One component variation of a variant ([ProjectVariantN] VariationN).

    kind: 1 Not Fitted, 2 an alternate part (AltLibLink_* fields), 0 fitted with
    parameter variations. Altium keys variations by the component's UniqueId path; the
    designator is what it was when the variation was made.
    """

    designator: str
    unique_id: str  # as written, e.g. \\SHEETSYMBOL\\COMPONENT
    kind: int
    alternate_part: str = ""
    alt: dict[str, str] = field(default_factory=dict)  # AltLibLink_* without the prefix

    @property
    def uid_tail(self) -> str:
        return self.unique_id.rsplit("\\", 1)[-1]


@dataclass
class VariantDefinition:
    name: str
    dnp_refdes: list[str] = field(default_factory=list)
    dnp_uid: list[str] = field(default_factory=list)  # UniqueIds for stale R? designators
    variations: list[Variation] = field(default_factory=list)
    # Parameter variations by designator: lower-case parameter name -> (name, value).
    param_variations: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)


@dataclass
class PrjPcbData:
    sheet_paths: list[str]  # project sheets and device sheets
    variants: list[VariantDefinition]
    pcb_doc_paths: list[str] = field(default_factory=list)
    pcb_dwf_paths: list[str] = field(default_factory=list)  # Draftsman documents
    parameters: dict[str, str] = field(default_factory=dict)  # project parameters
    # Board-level annotation files (logical -> physical designators)
    annotation_paths: list[str] = field(default_factory=list)
    # [Design] OutputPath and LogFolderPath, as written ('' = Altium's defaults,
    # 'Project Outputs for <project>' and 'Project Logs for <project>').
    output_path: str = ""
    log_folder_path: str = ""
    # [Design] CurrentVariant: the variant last active in Altium.
    current_variant: str = ""
    # The device sheets among sheet_paths ([DeviceSheetN] sections).
    device_sheet_paths: list[str] = field(default_factory=list)
    # [Design] HierarchyMode, the Net Identifier Scope as written ('' when absent):
    # 0 Automatic, 1 Flat, 2 Hierarchical, 3 Global (Altium's TFlattenMode order).
    hierarchy_mode: str = ""
    # [Design] net naming options as written (key -> '0'/'1'), see NET_NAMING_KEYS.
    net_naming: dict[str, str] = field(default_factory=dict)


@dataclass
class PrjOptions:
    """The option sections of a .PrjPcb as written (key -> value), read for
    get_project_options. Keys keep their case; values are stripped."""
    design: dict[str, str] = field(default_factory=dict)              # [Design]
    erc_levels: dict[str, str] = field(default_factory=dict)          # [Electrical Rules Check]
    connection_matrix: dict[str, str] = field(default_factory=dict)   # [ERC Connection Matrix]
    class_generation: dict[str, str] = field(default_factory=dict)    # [PrjClassGen]
    # (DocumentPath as written, its ClassGen* / GenerateClassCluster keys) per schematic
    # [DocumentN] and [DeviceSheetN].
    sheet_class_generation: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    comparison: list[dict[str, str]] = field(default_factory=list)    # ComparisonOptionsN fields
    difference_levels: dict[str, str] = field(default_factory=dict)   # [Difference Levels]
    annotate: dict[str, str] = field(default_factory=dict)            # [Annotate]
    diff_pair_suffixes: list[dict[str, str]] = field(default_factory=list)  # [DiffPairSuffixN]


def parse_prj_options(path: str) -> PrjOptions:
    """The project option sections of a .PrjPcb (Project Options dialog), as written."""
    sections = _split_sections(Path(path).read_text(encoding="utf-8-sig"))
    options = PrjOptions(
        design=_section(sections, "Design"),
        erc_levels=_section(sections, "Electrical Rules Check"),
        connection_matrix=_section(sections, "ERC Connection Matrix"),
        class_generation=_section(sections, "PrjClassGen"),
        difference_levels=_section(sections, "Difference Levels"),
        annotate=_section(sections, "Annotate"),
    )
    for header, lines in sections:
        if re.match(r"^(?:Document|DeviceSheet)\d+$", header, re.IGNORECASE):
            raw = _get_field(lines, "DocumentPath") or ""
            if raw.lower().endswith(".schdoc"):
                fields = _fields(lines)
                options.sheet_class_generation.append((raw, {
                    key: value for key, value in fields.items()
                    if key.lower().startswith("classgen") or key.lower() == "generateclasscluster"
                }))
        elif re.match(r"^DiffPairSuffix\d+$", header, re.IGNORECASE):
            options.diff_pair_suffixes.append(_fields(lines))
        elif header.lower() == "comparison options":
            for key, value in _fields(lines).items():
                if key.lower().startswith("comparisonoptions"):
                    options.comparison.append(_parse_inline_props(value))
    return options


def _fields(lines: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.strip():
            result.setdefault(key.strip(), value.strip())
    return result


def _section(sections: list[tuple[str, list[str]]], name: str) -> dict[str, str]:
    for header, lines in sections:
        if header.lower() == name.lower():
            return _fields(lines)
    return {}


# Project Options > Options > Netlist Options, as the [Design] keys that store them.
NET_NAMING_KEYS = (
    "AllowPortNetNames", "AllowSheetEntryNetNames", "AppendSheetNumberToLocalNets",
    "NameNetsHierarchically", "PowerPortNamesTakePriority",
)


def parse_prj_pcb(path: str) -> PrjPcbData:
    text = Path(path).read_text(encoding="utf-8-sig")
    project_dir = Path(path).parent
    sections = _split_sections(text)
    sheet_paths = _extract_sheets(sections, project_dir)
    variants = _extract_variants(sections)
    pcb_doc_paths = _extract_documents(sections, project_dir, ".pcbdoc")
    net_naming = {key: _design_field(sections, key) for key in NET_NAMING_KEYS}
    return PrjPcbData(
        sheet_paths=sheet_paths,
        variants=variants,
        pcb_doc_paths=pcb_doc_paths,
        pcb_dwf_paths=_extract_documents(sections, project_dir, ".pcbdwf"),
        parameters=_extract_parameters(sections),
        annotation_paths=_extract_documents(sections, project_dir, ".annotation"),
        output_path=_design_field(sections, "OutputPath"),
        log_folder_path=_design_field(sections, "LogFolderPath"),
        current_variant=_design_field(sections, "CurrentVariant"),
        device_sheet_paths=_extract_documents(sections, project_dir, ".schdoc", "DeviceSheet"),
        hierarchy_mode=_design_field(sections, "HierarchyMode"),
        net_naming={key: value for key, value in net_naming.items() if value},
    )


def _design_field(sections: list[tuple[str, list[str]]], key: str) -> str:
    for header, lines in sections:
        if header.lower() == "design":
            return _get_field(lines, key) or ""
    return ""


def _split_sections(text: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    current_header: str | None = None
    current_lines: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^\[(.+)\]\s*$", line)
        if m:
            if current_header is not None:
                sections.append((current_header, current_lines))
            current_header = m.group(1)
            current_lines = []
        elif current_header is not None:
            current_lines.append(line)
    if current_header is not None:
        sections.append((current_header, current_lines))
    return sections


def _get_field(lines: list[str], key: str) -> str | None:
    for line in lines:
        m = re.match(rf"^\s*{re.escape(key)}\s*=\s*(.*)\s*$", line, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def _parse_inline_props(s: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in s.split("|"):
        if "=" in part:
            key, _, val = part.partition("=")
            result[key.strip()] = val.strip()
    return result


def _extract_documents(
    sections: list[tuple[str, list[str]]],
    project_dir: Path,
    suffix: str,
    section: str = "Document",
) -> list[str]:
    paths: list[str] = []
    for header, lines in sections:
        if not re.match(rf"^(?:{section})\d+$", header, re.IGNORECASE):
            continue
        raw = _get_field(lines, "DocumentPath")
        if not raw:
            continue
        normalized = raw.replace("\\", "/")
        if not normalized.lower().endswith(suffix):
            continue
        paths.append(str((project_dir / normalized).resolve()))
    return paths


def _extract_parameters(sections: list[tuple[str, list[str]]]) -> dict[str, str]:
    """Project parameters ([ParameterN] Name/Value), e.g. a revision or project title."""
    params: dict[str, str] = {}
    for header, lines in sections:
        if not re.match(r"^Parameter\d+$", header, re.IGNORECASE):
            continue
        name, value = _get_field(lines, "Name"), _get_field(lines, "Value")
        if name and value:
            params[name] = value
    return params


def _extract_sheets(sections: list[tuple[str, list[str]]], project_dir: Path) -> list[str]:
    """The project's own sheets ([DocumentN]) and its device sheets ([DeviceSheetN], e.g.
    Managed\\Sheets\\<GUID>\\Name.SchDoc for a managed one), in file order, each listed once."""
    paths: dict[str, str] = {}
    for path in _extract_documents(sections, project_dir, ".schdoc", "Document|DeviceSheet"):
        paths.setdefault(os.path.normcase(path), path)
    return list(paths.values())


def _extract_variants(sections: list[tuple[str, list[str]]]) -> list[VariantDefinition]:
    headers = [h for h, _ in sections]
    if any(re.match(r"^ProjectVariant\d+$", h, re.IGNORECASE) for h in headers):
        return _extract_format_a(sections)
    if any(re.match(r"^Variation\d+$", h, re.IGNORECASE) for h in headers):
        return _extract_format_b(sections)
    return [VariantDefinition(name="Default")]


def _normalize_uid(uid: str) -> str:
    """Strip \\SheetUID\\ prefix from Altium variant UniqueIds, returning the component UID."""
    return uid.rsplit("\\", 1)[-1] if "\\" in uid else uid


def _extract_format_a(sections: list[tuple[str, list[str]]]) -> list[VariantDefinition]:
    variants: list[VariantDefinition] = []
    for header, lines in sections:
        if not re.match(r"^ProjectVariant\d+$", header, re.IGNORECASE):
            continue
        name = _get_field(lines, "Description")
        if not name:
            continue
        dnp: list[str] = []
        dnp_uid: list[str] = []
        variations: list[Variation] = []
        designators: dict[str, str] = {}
        values: dict[str, str] = {}
        for line in lines:
            pair = re.match(r"^\s*(ParamDesignator|ParamVariation)(\d+)=(.*)$", line,
                            re.IGNORECASE)
            if pair:
                target = designators if pair.group(1).lower() == "paramdesignator" else values
                target[pair.group(2)] = pair.group(3).strip()
                continue
            m = re.match(r"^\s*Variation\d+=(.+)$", line, re.IGNORECASE)
            if not m:
                continue
            props = _parse_inline_props(m.group(1))
            designator = props.get("Designator", "")
            kind = props.get("Kind", "")
            variations.append(Variation(
                designator=designator,
                unique_id=props.get("UniqueId", ""),
                kind=int(kind) if kind.isdigit() else 0,
                alternate_part=props.get("AlternatePart", ""),
                alt={key[len("AltLibLink_"):]: value for key, value in props.items()
                     if key.startswith("AltLibLink_")},
            ))
            # Not Fitted, whatever AlternatePart holds (Altium leaves some behind).
            if kind != "1":
                continue
            uid = _normalize_uid(props.get("UniqueId", ""))
            if designator and "?" not in designator:
                dnp.append(designator)
            elif uid:
                # Stale designator — defer resolution to uid map built from schematics
                dnp_uid.append(uid)
        param_variations: dict[str, dict[str, tuple[str, str]]] = {}
        for index, designator in designators.items():
            fields = _parse_inline_props(values.get(index, ""))
            parameter = fields.get("ParameterName", "")
            if designator and parameter:
                param_variations.setdefault(designator, {})[parameter.lower()] = (
                    parameter, fields.get("VariantValue", ""),
                )
        variants.append(VariantDefinition(
            name=name, dnp_refdes=dnp, dnp_uid=dnp_uid, variations=variations,
            param_variations=param_variations,
        ))
    return variants or [VariantDefinition(name="Default")]


class VariantState:
    def __init__(self, variants: list[VariantDefinition]):
        self._variants = variants
        self._active = variants[0] if variants else VariantDefinition(name="Default")

    def set_variant(self, name: str) -> None:
        match = next((v for v in self._variants if v.name.lower() == name.lower()), None)
        if match is None:
            available = [v.name for v in self._variants]
            raise ValueError(f"Variant '{name}' not found. Available: {available}")
        self._active = match

    def is_dnp(self, refdes: str) -> bool:
        return refdes in self._active.dnp_refdes

    def resolve_dnp_uid(self, uid_to_refdes: dict[str, str]) -> None:
        """Resolve stale DNP UniqueIds to real refdes using the schematic uid map.
        Call after schematics are parsed. Safe to call multiple times."""
        for variant in self._variants:
            if not variant.dnp_uid:
                continue
            resolved = set(variant.dnp_refdes)
            unresolved: list[str] = []
            for uid in variant.dnp_uid:
                if uid in uid_to_refdes:
                    resolved.add(uid_to_refdes[uid])
                else:
                    unresolved.append(uid)
            variant.dnp_refdes = list(resolved)
            # Keep UIDs the schematics did not cover (e.g. a sheet failed to parse) so a
            # later refresh_netlist can still resolve them.
            variant.dnp_uid = unresolved

    @property
    def active(self) -> VariantDefinition:
        return self._active

    @property
    def variants(self) -> list[VariantDefinition]:
        return list(self._variants)

    def get(self, name: str) -> VariantDefinition | None:
        return next((v for v in self._variants if v.name.lower() == name.lower()), None)

def _extract_format_b(sections: list[tuple[str, list[str]]]) -> list[VariantDefinition]:
    variants: list[VariantDefinition] = []
    for header, lines in sections:
        if re.match(r"^Variation\d+$", header, re.IGNORECASE):
            name = _get_field(lines, "VariantName")
            if name:
                variants.append(VariantDefinition(name=name))

    active: VariantDefinition | None = None
    for header, lines in sections:
        if re.match(r"^Variation\d+$", header, re.IGNORECASE):
            name = _get_field(lines, "VariantName")
            active = next((v for v in variants if v.name == name), None)
        elif re.match(r"^CompVar\d+$", header, re.IGNORECASE) and active:
            kind = _get_field(lines, "VariantKind")
            if kind == "3":
                refdes = _get_field(lines, "RefDesignator1")
                if refdes:
                    active.dnp_refdes.append(refdes)

    return variants or [VariantDefinition(name="Default")]
