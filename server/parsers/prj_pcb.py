import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    from parsers.sch_doc import _read_records
except ImportError:
    from server.parsers.sch_doc import _read_records

# Kind of a [ProjectVariantN] VariationN line, as the Variant Management dialog names it.
KIND_FITTED, KIND_NOT_FITTED, KIND_ALTERNATE = 0, 1, 2
KIND_NAMES = {KIND_FITTED: "fitted", KIND_NOT_FITTED: "not fitted",
              KIND_ALTERNATE: "alternate part"}
VARIANT_LIBRARY_SUFFIX = ".PrjPcbVariants"


@dataclass
class LibraryPart:
    """An alternate part's copy in the project's <project>.PrjPcbVariants file: an OLE
    schematic library holding one storage per alternate part (named by its library
    reference) whose Data stream carries the part's records without OwnerIndex, plus a
    default Component_1. Altium's docs: "The Alternate Parts are stored in the
    <ProjectName>.PrjPcbVariants file"; the copy is not refreshed by library updates."""

    lib_reference: str
    storage: str = ""             # the OLE storage name (the library reference as saved)
    design_item_id: str = ""
    description: str = ""         # ComponentDescription
    library: str = ""             # SourceLibraryName
    library_path: str = ""        # LibraryPath
    footprint: str | None = None  # the current PCBLIB model
    footprints: list[str] = field(default_factory=list)
    # Parameters: lower-case name -> (name as written, text); empty texts left out.
    params: dict[str, tuple[str, str]] = field(default_factory=dict)


@dataclass
class Variation:
    """One component variation of a variant ([ProjectVariantN] VariationN).

    kind: 1 Not Fitted, 2 an alternate part (AltLibLink_* fields), 0 fitted with
    parameter variations. Altium keys variations by the component's UniqueId path; the
    designator is what it was when the variation was made. library_part is the
    alternate's copy in the .PrjPcbVariants file, when it was found there;
    library_error says why that file could not be read.
    """

    designator: str
    unique_id: str  # as written, e.g. \\SHEETSYMBOL\\COMPONENT
    kind: int
    alternate_part: str = ""
    alt: dict[str, str] = field(default_factory=dict)  # AltLibLink_* without the prefix
    library_part: LibraryPart | None = field(default=None, compare=False, repr=False)
    library_error: str = field(default="", compare=False, repr=False)

    @property
    def uid_tail(self) -> str:
        return self.unique_id.rsplit("\\", 1)[-1]

    @property
    def kind_name(self) -> str:
        return KIND_NAMES.get(self.kind, f"kind {self.kind}")

    @property
    def alternate_reference(self) -> str:
        """The library reference an alternate part names: AlternatePart, else
        AltLibLink_DesignItemID, else AltLibLink_SymbolReference ('' for none). On every
        corpus alternate AlternatePart and DesignItemID are the part's library reference
        and SymbolReference is neither."""
        return (self.alternate_part or self.alt.get("DesignItemID")
                or self.alt.get("SymbolReference") or "")

    def alternate_identity(self) -> dict:
        """What the variation says about its alternate part (Kind=2): library reference,
        design item, library and footprint (None with IsSameFootprint: the base part's is
        kept), every other AltLibLink_* field as written under link, and what its copy in
        the .PrjPcbVariants file adds (description, Comment, Value). Sampled only on
        alternates that re-select the base part with the same footprint (6 corpus lines):
        another part, a database table or IsSameFootprint false are read as the key
        names suggest, unvalidated."""
        alt = {key: value for key, value in self.alt.items() if value}
        same = alt.get("IsSameFootprint", "").strip().lower() in ("true", "t", "1")
        identity: dict = {
            "lib_reference": self.alternate_reference or None,
            "design_item_id": alt.get("DesignItemID") or None,
            "library": alt.get("SourceLibraryName") or alt.get("LibraryIdentifier") or None,
            "footprint": None if same else (alt.get("Footprint") or None),
            "same_footprint": same,
        }
        if alt.get("UseDBTableName", "").strip().lower() in ("true", "t", "1") and \
                alt.get("DatabaseTableName"):
            identity["database_table"] = alt["DatabaseTableName"]
        shown = {"DesignItemID", "SourceLibraryName", "Footprint", "IsSameFootprint",
                 "UseDBTableName", "DatabaseTableName"}
        if alt.get("SymbolReference") == identity["lib_reference"]:
            shown.add("SymbolReference")
        link = {key: value for key, value in alt.items() if key not in shown}
        if link:
            identity["link"] = link
        part = self.library_part
        identity["library_copy"] = part is not None
        if self.library_error:
            identity["library_error"] = self.library_error
        if part is not None:
            identity["description"] = part.description or None
            for name in ("comment", "value"):
                if name in part.params:
                    identity[name] = part.params[name][1]
            if identity["footprint"] is None and not same and part.footprint:
                identity["footprint"] = part.footprint
        return {key: value for key, value in identity.items() if value is not None}


@dataclass
class VariantDefinition:
    name: str
    dnp_refdes: list[str] = field(default_factory=list)
    dnp_uid: list[str] = field(default_factory=list)  # UniqueIds for stale R? designators
    variations: list[Variation] = field(default_factory=list)
    # Parameter variations by designator: lower-case parameter name -> (name, value).
    param_variations: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)
    unique_id: str = ""            # the variant's own UniqueId
    allow_fabrication: bool = False  # AllowFabrication: varies fabrication outputs too
    # Variant parameters ([ParameterN_M] sections after the variant): name -> value.
    parameters: dict[str, str] = field(default_factory=dict)
    # Set by binding the variant to the loaded parts (bom.plan_variant): parameter
    # variations and alternate parts by the designator they apply to now. None until
    # bound; the stored designators are used meanwhile.
    bound_params: dict[str, dict[str, tuple[str, str]]] | None = field(
        default=None, compare=False, repr=False)
    bound_alternates: dict[str, dict] | None = field(default=None, compare=False, repr=False)

    def not_fitted(self) -> list[Variation]:
        """The Not Fitted lines (or, in a variant known by designators only, one per
        Not Fitted designator)."""
        if self.variations:
            return [v for v in self.variations if v.kind == KIND_NOT_FITTED]
        return [Variation(designator=d, unique_id="", kind=KIND_NOT_FITTED)
                for d in self.dnp_refdes]

    def alternate_parts(self) -> list[dict]:
        """Each alternate part: {designator, unique_id, **alternate_identity()}."""
        return [{"designator": v.designator, "unique_id": v.unique_id,
                 **v.alternate_identity()}
                for v in self.variations if v.kind == KIND_ALTERNATE]

    def varied_parameters(self) -> list[dict]:
        """Each parameter variation: {designator, unique_id (of the variation line with
        that designator, '' when none or several), parameter, value}. Altium stores
        parameter variations by designator only."""
        lines: dict[str, set[str]] = {}
        for variation in self.variations:
            lines.setdefault(variation.designator, set()).add(variation.unique_id)
        out = []
        for designator, params in self.param_variations.items():
            uids = lines.get(designator) or set()
            uid = next(iter(uids)) if len(uids) == 1 else ""
            for name, value in params.values():
                out.append({"designator": designator, "unique_id": uid,
                            "parameter": name, "value": value})
        return out

    def variation_of(self, designator: str) -> tuple[dict[str, tuple[str, str]], dict | None]:
        """(parameter variations, alternate part) this variant gives a loaded part: as
        bound to the parts by UniqueId once bind() ran, else by the stored designator."""
        if self.bound_params is not None:
            return (self.bound_params.get(designator) or {},
                    (self.bound_alternates or {}).get(designator))
        alternate = next((v for v in self.variations
                          if v.kind == KIND_ALTERNATE and v.designator == designator), None)
        return (self.param_variations.get(designator) or {},
                {"variation": alternate} if alternate is not None else None)

    def bind(self, params: dict[str, dict[str, tuple[str, str]]],
             alternates: dict[str, dict]) -> None:
        self.bound_params = params
        self.bound_alternates = alternates


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
    # The <project>.PrjPcbVariants file the alternate parts were looked up in ('' when
    # no variant has an alternate part or the file is absent), and why it could not be
    # read ('' when it was).
    variant_library_path: str = ""
    variant_library_error: str = ""


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


@dataclass
class ProjectDocument:
    """A [DocumentN] or [DeviceSheetN] entry of a .PrjPcb, whatever its file type."""
    section: str  # e.g. Document3, DeviceSheet1
    raw: str      # DocumentPath as written
    path: str     # resolved against the project folder


def parse_prj_documents(path: str) -> list[ProjectDocument]:
    """Every document the .PrjPcb lists, in file order, with any extension (parse_prj_pcb
    keeps only the types the tools read)."""
    project_dir = Path(path).parent
    documents = []
    for header, lines in _split_sections(Path(path).read_text(encoding="utf-8-sig")):
        if re.match(r"^(?:Document|DeviceSheet)\d+$", header, re.IGNORECASE):
            raw = _get_field(lines, "DocumentPath") or ""
            if raw:
                resolved = str((project_dir / raw.replace("\\", "/")).resolve())
                documents.append(ProjectDocument(header, raw, resolved))
    return documents


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
    library_path, library_error = _attach_library_parts(variants, Path(path))
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
        variant_library_path=library_path,
        variant_library_error=library_error,
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
    current: VariantDefinition | None = None
    for header, lines in sections:
        if re.match(r"^Parameter\d+_\d+$", header, re.IGNORECASE):
            # A variant's parameters follow its section (corpus: [Parameter2_1] after
            # ProjectVariant1); the indexes are not the variant's, so position decides.
            name, value = _get_field(lines, "Name"), _get_field(lines, "Value")
            if current is not None and name:
                current.parameters[name] = value or ""
            continue
        if not re.match(r"^ProjectVariant\d+$", header, re.IGNORECASE):
            current = None
            continue
        current = None
        name = _get_field(lines, "Description")
        if not name:
            continue
        dnp: list[str] = []
        dnp_uid: list[str] = []
        variations: list[Variation] = []
        designators: dict[str, str] = {}
        values: dict[str, str] = {}
        for line in lines:
            # 'Key = Value' as well as 'Key=Value', like _get_field (some saved projects
            # write every key line with spaces around '=').
            pair = re.match(r"^\s*(ParamDesignator|ParamVariation)(\d+)\s*=(.*)$", line,
                            re.IGNORECASE)
            if pair:
                target = designators if pair.group(1).lower() == "paramdesignator" else values
                target[pair.group(2)] = pair.group(3).strip()
                continue
            m = re.match(r"^\s*Variation\d+\s*=(.+)$", line, re.IGNORECASE)
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
        current = VariantDefinition(
            name=name, dnp_refdes=dnp, dnp_uid=dnp_uid, variations=variations,
            param_variations=param_variations,
            unique_id=_get_field(lines, "UniqueId") or "",
            allow_fabrication=(_get_field(lines, "AllowFabrication") or "0") not in ("", "0"),
        )
        variants.append(current)
    return variants or [VariantDefinition(name="Default")]


def variant_library_path(prj_path: Path) -> Path | None:
    """The project's <project>.PrjPcbVariants next to it (any case), or None."""
    wanted = (prj_path.stem + VARIANT_LIBRARY_SUFFIX).lower()
    candidate = prj_path.with_name(prj_path.stem + VARIANT_LIBRARY_SUFFIX)
    if candidate.is_file():
        return candidate
    try:
        return next((p for p in prj_path.parent.iterdir()
                     if p.name.lower() == wanted and p.is_file()), None)
    except OSError:
        return None


def parse_variant_library(path: str) -> list[LibraryPart]:
    """The alternate parts saved in a .PrjPcbVariants file (see LibraryPart); the default
    Component_N storage a new library starts with is left out."""
    import olefile

    parts: list[LibraryPart] = []
    with olefile.OleFileIO(path) as ole:
        for stream in ole.listdir():
            if len(stream) != 2 or stream[1] != "Data" or \
                    re.fullmatch(r"Component_\d+", stream[0]):
                continue
            records = _read_records(ole.openstream("/".join(stream)).read())
            if not records or records[0].get("record") != "1":
                continue
            head = records[0]
            part = LibraryPart(
                lib_reference=head.get("libreference", "") or stream[0],
                storage=stream[0],
                design_item_id=head.get("designitemid", ""),
                description=head.get("componentdescription", ""),
                library=head.get("sourcelibraryname", ""),
                library_path=head.get("librarypath", ""),
            )
            current = None
            for record in records[1:]:
                kind = record.get("record")
                if kind == "41" and record.get("name") and record.get("text") \
                        and not record["text"].startswith('"'):
                    part.params.setdefault(record["name"].lower(),
                                           (record["name"], record["text"]))
                elif kind == "45" and record.get("modeltype", "").upper() == "PCBLIB" \
                        and record.get("modelname"):
                    name = record["modelname"]
                    if name not in part.footprints:
                        part.footprints.append(name)
                    if record.get("iscurrent", "").upper() == "T" and current is None:
                        current = name
            part.footprint = current or (part.footprints[0] if part.footprints else None)
            parts.append(part)
    return parts


def _attach_library_parts(variants: list[VariantDefinition], prj_path: Path) -> tuple[str, str]:
    """Give each alternate-part variation its copy from the .PrjPcbVariants file, matched
    by the names it gives in Variation.alternate_reference order against the storage
    name, library reference and design item id. Returns (the file read, the error
    reading it, also set on each alternate); ('', '') when no variant has an alternate
    part."""
    alternates = [v for variant in variants for v in variant.variations
                  if v.kind == KIND_ALTERNATE]
    if not alternates:
        return "", ""
    path = variant_library_path(prj_path)
    if path is None:
        return "", ""
    try:
        parts = parse_variant_library(str(path))
    except Exception as error:  # an unreadable library leaves the alternates bare
        logging.warning("Variant library %s not read: %s", path.name, error)
        message = f"{type(error).__name__}: {error}"
        for variation in alternates:
            variation.library_error = message
        return str(path), message
    by_name: dict[str, LibraryPart] = {}
    for part in parts:
        for key in (part.storage, part.lib_reference, part.design_item_id):
            if key:
                by_name.setdefault(key.lower(), part)
    for variation in alternates:
        for key in (variation.alternate_part, variation.alt.get("DesignItemID", ""),
                    variation.alt.get("SymbolReference", "")):
            if key and key.lower() in by_name:
                variation.library_part = by_name[key.lower()]
                break
    return str(path), ""


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
