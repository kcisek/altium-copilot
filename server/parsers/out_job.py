"""Reader for Altium Output Job files (.OutJob).

An .OutJob is INI-like text (UTF-8 with or without BOM; older files Windows-1252) with
four sections: [OutputJobFile] (version and workspace identity), [OutputGroup1] (the
job: its variant, containers and outputs), [PublishSettings] and [GeneratedFilesSettings]
(the path and naming settings of each container, keyed by the container's number).

In the output group, OutputMediumN / OutputMediumN_Type is container N (Printer = Print
Job, Publish = PDF, GeneratedFiles = Folder Structure, Multimedia = Video) and output N
is OutputTypeN, OutputNameN, OutputCategoryN, OutputDocumentPathN (the data source),
OutputVariantNameN, OutputEnabledN_OutputMediumM (0: not connected to container M; k > 0:
connected, k-th in its generation order), ConfigurationN_NameK / ConfigurationN_ItemK
(the output's setup, '|'-separated Key=Value pairs with a Record=<kind>) and PageOptionsN.
OutputEnabledN only mirrors the link to the container selected in the editor
(TargetOutputMedium): it is no switch of its own.

Every key is kept as written; the services map only keys that Altium's example files
and a corpus of real projects verify (see the tests)."""
import re
from dataclasses import dataclass, field
from pathlib import Path

CONTAINER_KINDS = {
    "Printer": "print job", "Publish": "pdf", "GeneratedFiles": "folder", "Multimedia": "video",
}
_SECTION = re.compile(r"^\[(.+)\]\s*$")
_MEDIUM = re.compile(r"^OutputMedium(\d+)(?:_(\w+))?$")
_OUTPUT = re.compile(r"^(OutputType|OutputName|OutputCategory|OutputDocumentPath|"
                     r"OutputVariantName|OutputEnabled|OutputDefault|PageOptions)(\d+)"
                     r"(?:_OutputMedium(\d+))?$")
_CONFIG = re.compile(r"^Configuration(\d+)_(Name|Item)(\d+)$")
_NUMBERED = re.compile(r"^([A-Za-z]+?)(\d+)$")
_OUTPUT_FIELDS = {
    "OutputType": "type", "OutputName": "name", "OutputCategory": "category",
    "OutputDocumentPath": "document", "OutputVariantName": "variant",
}


@dataclass
class OutJobRecord:
    """One ConfigurationN_ItemK: its name (ConfigurationN_NameK), Record kind and pairs."""
    name: str
    record: str                      # the Record= value, '' when the item has none
    values: dict[str, str] = field(default_factory=dict)
    raw: str = ""                    # the item as written when it is no Key=Value list


@dataclass
class OutJobOutput:
    index: int
    type: str = ""
    name: str = ""
    category: str = ""
    document: str = ""               # OutputDocumentPathN as written ('' = default source)
    variant: str = ""                # OutputVariantNameN as written
    connected_to_selected: bool | None = None  # OutputEnabledN
    containers: dict[int, int] = field(default_factory=dict)  # container -> order (> 0)
    records: list[OutJobRecord] = field(default_factory=list)
    page_options: str = ""
    unmapped: dict[str, str] = field(default_factory=dict)


@dataclass
class OutJobContainer:
    index: int
    name: str = ""
    type: str = ""                   # OutputMediumN_Type as written
    publish: dict[str, str] = field(default_factory=dict)    # [PublishSettings] keys N
    generated: dict[str, str] = field(default_factory=dict)  # [GeneratedFilesSettings] keys N
    unmapped: dict[str, str] = field(default_factory=dict)   # other OutputMediumN_* keys

    @property
    def kind(self) -> str:
        return CONTAINER_KINDS.get(self.type, self.type or "unknown")


@dataclass
class OutJob:
    path: str
    header: dict[str, str] = field(default_factory=dict)  # [OutputJobFile]
    job: dict[str, str] = field(default_factory=dict)     # the group's own keys
    containers: list[OutJobContainer] = field(default_factory=list)
    outputs: list[OutJobOutput] = field(default_factory=list)
    # Unnumbered keys of the settings sections (e.g. GenerateTOC) and any other section,
    # including further OutputGroupN sections (only OutputGroup1 is ever seen), raw.
    other: dict[str, dict[str, str]] = field(default_factory=dict)
    encoding: str = "utf-8"

    @property
    def target(self) -> str:
        return self.job.get("TargetOutputMedium", "")

    @property
    def variant(self) -> str:
        return self.job.get("VariantName", "")

    def container(self, index: int) -> OutJobContainer | None:
        return next((c for c in self.containers if c.index == index), None)


def decode_text(data: bytes) -> tuple[str, str]:
    """(text, encoding): UTF-8 (BOM optional), else Windows-1252 as older files are."""
    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace"), "cp1252"


def _sections(text: str) -> list[tuple[str, dict[str, str]]]:
    sections: list[tuple[str, dict[str, str]]] = []
    current: dict[str, str] | None = None
    for line in text.splitlines():
        match = _SECTION.match(line)
        if match:
            current = {}
            sections.append((match.group(1).strip(), current))
            continue
        if current is None:
            continue
        key, sep, value = line.partition("=")
        if sep and key.strip():
            current.setdefault(key.strip(), value.strip())
    return sections


def parse_record(name: str, item: str) -> OutJobRecord:
    """A ConfigurationN_ItemK: '|'-separated Key=Value pairs, or raw text (BOM filters)."""
    pairs = [part.partition("=") for part in item.split("|")]
    if not any(sep and key.strip() == "Record" for key, sep, _ in pairs):
        return OutJobRecord(name=name, record="", raw=item)
    values: dict[str, str] = {}
    for key, sep, value in pairs:
        if sep and key.strip():
            values.setdefault(key.strip(), value.strip())
    return OutJobRecord(name=name, record=values.get("Record", ""), values=values)


def parse_out_job_text(text: str, path: str = "") -> OutJob:
    job = OutJob(path=path)
    group_seen = False
    settings: list[tuple[str, dict[str, str]]] = []
    for header, values in _sections(text):
        if header == "OutputJobFile" and not job.header:
            job.header = values
        elif re.fullmatch(r"OutputGroup\d+", header) and not group_seen:
            group_seen = True
            _read_group(job, values)
        elif header in ("PublishSettings", "GeneratedFilesSettings"):
            settings.append((header, values))  # read once the containers are known
        else:
            job.other[header] = values
    for header, values in settings:
        for key, value in values.items():
            match = _NUMBERED.match(key)
            container = job.container(int(match.group(2))) if match else None
            if container is None:
                job.other.setdefault(header, {})[key] = value
                continue
            target = container.publish if header == "PublishSettings" else container.generated
            target[match.group(1)] = value
    return job


def _read_group(job: OutJob, values: dict[str, str]) -> None:
    containers: dict[int, OutJobContainer] = {}
    outputs: dict[int, OutJobOutput] = {}
    configs: dict[int, dict[int, dict[str, str]]] = {}
    for key, value in values.items():
        match = _MEDIUM.match(key)
        if match:
            container = containers.setdefault(int(match.group(1)),
                                              OutJobContainer(int(match.group(1))))
            if match.group(2) is None:
                container.name = value
            elif match.group(2) == "Type":
                container.type = value
            else:
                container.unmapped[match.group(2)] = value
            continue
        match = _OUTPUT.match(key)
        if match:
            field_name, number, medium = match.group(1), int(match.group(2)), match.group(3)
            output = outputs.setdefault(number, OutJobOutput(number))
            if medium is not None:
                if value.isdigit() and int(value) > 0:
                    output.containers[int(medium)] = int(value)
                elif not (value.isdigit() or value == ""):
                    output.unmapped[key] = value
            elif field_name in _OUTPUT_FIELDS:
                setattr(output, _OUTPUT_FIELDS[field_name], value)
            elif field_name == "OutputEnabled":
                output.connected_to_selected = {"1": True, "0": False}.get(value)
                if output.connected_to_selected is None:
                    output.unmapped[key] = value
            elif field_name == "PageOptions":
                output.page_options = value
            elif value not in ("", "0"):  # OutputDefaultN: 0 in every sample
                output.unmapped[key] = value
            continue
        match = _CONFIG.match(key)
        if match:
            number, part, item = int(match.group(1)), match.group(2), int(match.group(3))
            configs.setdefault(number, {}).setdefault(item, {})[part] = value
            continue
        job.job[key] = value
    for number, items in configs.items():
        output = outputs.setdefault(number, OutJobOutput(number))
        for item in sorted(items):
            parts = items[item]
            output.records.append(parse_record(parts.get("Name", ""), parts.get("Item", "")))
    job.containers = [containers[n] for n in sorted(containers)]
    job.outputs = [outputs[n] for n in sorted(outputs)]


def parse_out_job(path: str) -> OutJob:
    text, encoding = decode_text(Path(path).read_bytes())
    job = parse_out_job_text(text, str(path))
    job.encoding = encoding
    return job
