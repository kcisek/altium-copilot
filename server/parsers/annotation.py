"""Altium board-level annotation files (<Project>.Annotation).

A board-annotated or multi-channel project gives each placed part a physical designator
that differs from the logical one drawn on its sheet. The compiled netlist and the PCB use
the physical designator. The .Annotation file holds the mapping as numbered keys in a
[DesignatorManager] section:

    LogicalDesignator2=R1
    LogicalPartID2=1
    DocumentName2=Red_LED.SchDoc
    ChannelName2=Red_LED1
    UniqueID2=\\HJYSAVCF\\ASIVGCJH\\HRWSBXKU
    PhysicalDesignator2=R3

UniqueID is the path of sheet-symbol UniqueIds from the top sheet down to the component's
own UniqueId, so a sheet placed several times (one channel each) shares the last segment.
Each part of a multi-part component has its own entry and UniqueId.
"""
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

_KEY_RE = re.compile(r"^\s*([A-Za-z]+)(\d+)\s*=(.*)$")


@dataclass
class AnnotationEntry:
    logical: str          # designator drawn on the sheet
    physical: str         # designator in the compiled netlist and on the board
    unique_id: str        # full UniqueId path, e.g. \HJYSAVCF\ASIVGCJH\HRWSBXKU
    document: str = ""    # sheet file name, e.g. Red_LED.SchDoc (may be empty)
    channel: str = ""

    @property
    def component_uid(self) -> str:
        """The component's own UniqueId, the last segment of the path."""
        return self.unique_id.rsplit("\\", 1)[-1]

    @property
    def sheet(self) -> str:
        """Sheet name (file stem) the component is drawn on, "" when not recorded."""
        return PureWindowsPath(self.document).stem if self.document else ""


def parse_annotation(path: str | Path) -> list[AnnotationEntry]:
    """Entries of a .Annotation file that name both a UniqueId and a physical designator."""
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    fields: dict[int, dict[str, str]] = {}
    in_manager = False
    for line in text.splitlines():
        header = re.match(r"^\s*\[(.+)\]\s*$", line)
        if header:
            in_manager = header.group(1).strip().lower() == "designatormanager"
            continue
        if not in_manager:
            continue
        m = _KEY_RE.match(line)
        if m:
            fields.setdefault(int(m.group(2)), {})[m.group(1).lower()] = m.group(3).strip()

    entries: list[AnnotationEntry] = []
    for _, values in sorted(fields.items()):
        physical = values.get("physicaldesignator", "")
        unique_id = values.get("uniqueid", "")
        if not physical or "?" in physical or not unique_id.strip("\\"):
            continue
        entries.append(AnnotationEntry(
            logical=values.get("logicaldesignator", ""),
            physical=physical,
            unique_id=unique_id,
            document=values.get("documentname", ""),
            channel=values.get("channelname", ""),
        ))
    return entries
