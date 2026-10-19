"""Altium Batch DRC reports: 'Design Rule Check - <PcbDoc>.html' and '.drc'.

Altium renders both from one XML document through the report_html_drc.xsl and
report_drc.xsl templates in its Templates folder, so they hold the same content: a
header (PCB file, date, time, elapsed time), report-level errors ('ERROR : ...'), and
tables typed rule, waived, health or warning. A rule table is titled by the rule's
description (kind, values and scopes, never the rule's name) and lists one row per
violation; the HTML lists rules without violations only in its summary. The HTML also
gives each row a zoom box: the violation area in absolute board coordinates (mil).
The coordinates inside a message are relative to the board origin, in the report unit.
Dates follow the generating machine's locale and are kept as written.

A report Altium could not finish (an empty HTML when the path is over 256 characters, a
file cut short) must not read as a clean board, so each parse lists its problems.
"""

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

RULE, WAIVED, HEALTH, WARNING = "rule", "waived", "health", "warning"
_SUMMARY_TYPES = {
    "rule violations": RULE,
    "waived violations": WAIVED,
    "pcb health issues": HEALTH,
    "warnings": WARNING,
}
_HEADER_TOTALS = {
    "warnings:": "warnings",
    "rule violations:": "rule_violations",
    "pcb health issues:": "health_issues",
    "waived violations:": "waived",
}
_BOX = re.compile(
    r"Location1\.X=(?P<x1>-?[\d.]+)mil\|Location2\.X=(?P<x2>-?[\d.]+)mil\|"
    r"Location1\.Y=(?P<y1>-?[\d.]+)mil\|Location2\.Y=(?P<y2>-?[\d.]+)mil"
)


@dataclass
class DrcViolation:
    message: str
    # The violation area, absolute board coordinates in mil (HTML reports only).
    box_mil: tuple[float, float, float, float] | None = None
    # 'Waived by <author> at <time>' and the waiver comment, for waived rows.
    waiver: str | None = None


@dataclass
class DrcTable:
    type: str  # rule | waived | health | warning
    title: str  # a rule's description, or a health / warning category
    count: int
    violations: list[DrcViolation] = field(default_factory=list)


@dataclass
class DrcReport:
    format: str  # html | txt
    pcb_file: str | None = None
    date: str | None = None
    time: str | None = None
    elapsed: str | None = None
    errors: list[str] = field(default_factory=list)
    tables: list[DrcTable] = field(default_factory=list)
    # As the report states them: rule_violations, waived, health_issues, warnings.
    totals: dict[str, int] = field(default_factory=dict)
    # Why the report may be incomplete (empty, cut short, rows not matching counts).
    problems: list[str] = field(default_factory=list)


def read_text(path: str | Path) -> str:
    """UTF-8 (what Altium writes today), else the Windows code page."""
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def parse_drc_report(path: str | Path) -> DrcReport:
    """Parse an .html or .drc Batch DRC report by its extension."""
    text = read_text(path)
    if Path(path).suffix.lower() in (".html", ".htm"):
        return parse_drc_html(text)
    return parse_drc_txt(text)


def _number(text: str) -> int | None:
    text = text.strip()
    return int(text) if text.isdigit() else None


def _check_counts(report: DrcReport) -> None:
    """Rows against each table's count, and the rule counts against the stated total."""
    mismatched = [
        table.title for table in report.tables
        if table.type != WARNING and len(table.violations) != table.count
    ]
    if mismatched:
        tables = "1 table lists" if len(mismatched) == 1 else f"{len(mismatched)} tables list"
        report.problems.append(
            f"{tables} a different number of rows than its count (e.g. {mismatched[0]})"
        )
    total = report.totals.get("rule_violations")
    counted = sum(table.count for table in report.tables if table.type == RULE)
    if total is not None and total != counted:
        report.problems.append(
            f"the rule counts add up to {counted}, the report states {total}"
        )


# ---------- HTML ----------

class _HtmlReport(HTMLParser):
    """Collects the header, the summary tables and the violation sections."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.report = DrcReport(format="html")
        self.summary: list[tuple[str, str, int, str | None]] = []  # type, title, count, anchor
        self.sections: dict[str, DrcTable] = {}  # anchor -> table
        self.closed = False  # </html> reached
        self._capture: str | None = None  # what the current text belongs to
        self._text: list[str] = []
        self._label = ""
        self._anchor: str | None = None  # the last <a name=...> outside a cell
        self._summary_type: str | None = None
        self._summary_row: dict | None = None
        self._section: DrcTable | None = None
        self._cell: dict | None = None
        self._callback_depth = 0  # <a> tags open inside a callback link

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        cls = attrs.get("class") or ""
        if self._cell is not None:
            self._cell_start(tag, attrs, cls)
            return
        if tag == "a" and attrs.get("name"):
            self._anchor = attrs["name"]
        if tag == "td" and cls in (
            "front_matter_column1", "front_matter_column3",
            "DRC_summary_header_col1", "DRC_summary_header_col3",
        ):
            self._start(cls)
        elif tag == "p" and cls == "error":
            self._start("error")
        elif tag == "th" and cls == "column1":
            self._start("summary_title")
        elif tag == "th" and cls in (RULE, WAIVED, HEALTH, WARNING):
            self._section = DrcTable(type=cls, title="", count=0)
            if self._anchor:
                self.sections[self._anchor] = self._section
            self._start("section_title")
        elif tag == "td" and cls == "column1":
            if self._section is not None:
                self._cell = {"text": [], "after": [], "box": None, "broken": False}
            elif self._summary_type is not None:
                self._summary_row = {"title": "", "anchor": None, "count": None}
                self._start("summary_desc")
        elif tag == "td" and cls == "column2" and self._summary_row is not None:
            self._start("summary_count")
        elif tag == "a" and self._summary_row is not None and self._capture == "summary_desc":
            href = attrs.get("href") or ""
            if href.startswith("#"):
                self._summary_row["anchor"] = href[1:]

    def _cell_start(self, tag, attrs, cls):
        cell = self._cell
        if tag == "a":
            if self._callback_depth:
                self._callback_depth += 1  # e.g. a coordinate anchor inside the link
            elif cls == "callback":
                self._callback_depth = 1
                match = _BOX.search(attrs.get("href") or "")
                if match and cell["box"] is None:
                    x1, x2 = float(match["x1"]), float(match["x2"])
                    y1, y2 = float(match["y1"]), float(match["y2"])
                    cell["box"] = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        elif tag == "br" and not self._callback_depth:
            if cell["broken"]:
                cell["after"].append("\n")  # 'Waived by ...<br>comment'
            elif "".join(cell["text"]).strip():
                cell["broken"] = True  # the message ends here

    def handle_endtag(self, tag):
        if tag == "html":
            self.closed = True
        if self._cell is not None:
            if tag == "a" and self._callback_depth:
                self._callback_depth -= 1
            elif tag == "td":
                self._end_cell()
            return
        if tag in ("td", "p", "th") and self._capture in (
            "front_matter_column1", "front_matter_column3", "DRC_summary_header_col1",
            "DRC_summary_header_col3", "error", "summary_title", "section_title",
            "summary_count",
        ):
            self._finish(self._capture, "".join(self._text).strip())
            self._capture = None
        elif tag == "td" and self._capture == "summary_desc":
            self._summary_row["title"] = "".join(self._text).strip()
            self._capture = None
        elif tag == "tr" and self._summary_row is not None:
            row = self._summary_row
            if (row["anchor"] or row["title"].lower() != "total") and row["count"] is not None:
                self.summary.append(
                    (self._summary_type, row["title"], row["count"], row["anchor"])
                )
            self._summary_row = None
        elif tag == "table":
            if self._section is not None:
                self._section = None
            elif self._summary_type is not None:
                self._summary_type = None

    def _end_cell(self):
        cell = self._cell
        self._cell, self._callback_depth = None, 0
        # A text string's own line break reaches the text as CRLF.
        message = "".join(cell["text"]).replace("\r\n", "\n").strip()
        after = "".join(cell["after"]).replace("\r\n", "\n").strip()
        if self._section.type != WAIVED and after:
            message, after = f"{message}\n{after}".strip(), ""
        if message or after:
            self._section.violations.append(DrcViolation(
                message=message, box_mil=cell["box"], waiver=after or None,
            ))

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["after" if self._cell["broken"] else "text"].append(data)
        elif self._capture is not None:
            self._text.append(data)

    def _start(self, what: str) -> None:
        self._capture = what
        self._text = []

    def _finish(self, what: str, text: str) -> None:
        report = self.report
        if what in ("front_matter_column1", "DRC_summary_header_col1"):
            self._label = text.lower()
        elif what == "front_matter_column3":
            if self._label == "date:":
                report.date = text
            elif self._label == "time:":
                report.time = text
            elif self._label == "elapsed time:":
                report.elapsed = text
            elif self._label == "filename:":
                report.pcb_file = text
        elif what == "DRC_summary_header_col3":
            key = _HEADER_TOTALS.get(self._label)
            if key and _number(text) is not None:
                report.totals[key] = _number(text)
        elif what == "error":
            report.errors.append(re.sub(r"^ERROR\s*:\s*", "", text))
        elif what == "summary_title":
            self._summary_type = _SUMMARY_TYPES.get(text.lower())
        elif what == "section_title" and self._section is not None:
            self._section.title = text
        elif what == "summary_count" and self._summary_row is not None:
            self._summary_row["count"] = _number(text)


def parse_drc_html(text: str) -> DrcReport:
    parser = _HtmlReport()
    parser.feed(text)
    parser.close()
    report = parser.report
    used: set[str] = set()
    for table_type, title, count, anchor in parser.summary:
        section = parser.sections.get(anchor or "")
        if section is not None:
            used.add(anchor)
            report.tables.append(DrcTable(
                type=table_type or section.type, title=title or section.title,
                count=count, violations=section.violations,
            ))
        else:
            # Rules without violations have no section (their link points nowhere).
            report.tables.append(DrcTable(type=table_type, title=title, count=count))
    for anchor, section in parser.sections.items():
        if anchor not in used:  # a section the summary did not list (e.g. warnings)
            section.count = len(section.violations)
            report.tables.append(section)
    if not text.strip():
        report.problems.append("the file is empty")
    else:
        if not parser.closed:
            report.problems.append("the file ends early (no </html>)")
        if "rule_violations" not in report.totals:
            report.problems.append("the header has no violation total")
        _check_counts(report)
    return report


# ---------- TXT (.drc) ----------

_TXT_STARTS = (
    ("Processing Rule : ", RULE),
    ("Waived Violations Of Rule : ", WAIVED),
    ("PCB Health Issue Category : ", HEALTH),
    ("WARNING: ", WARNING),
)
# Each row starts with this (rule and waived tables); other lines continue the row.
_ROW_STARTS = {RULE: "   Violation between ", WAIVED: "   Waived Violation between "}
_TXT_COUNTS = ("Rule Violations :", "Waived Violations :", "Issues :")
_TXT_FOOTER = {
    "Violations Detected": "rule_violations",
    "Waived Violations": "waived",
    "PCB Health Issues": "health_issues",
}
_WAIVED_BY = re.compile(r"Waived by ")


def _continue_row(table: DrcTable, line: str) -> None:
    """A line that continues the last row: a second cell (indented) or a text string's
    own line break; for waived rows, the waiver and its comment."""
    last = table.violations[-1]
    stripped = line.strip()
    if table.type == WAIVED and last.waiver is None:
        found = _WAIVED_BY.search(stripped)
        if found:
            last.message = f"{last.message}\n{stripped[: found.start()]}".rstrip()
            last.waiver = stripped[found.start():]
            return
    if table.type == WAIVED and last.waiver is not None:
        last.waiver = f"{last.waiver}\n{stripped}"
        return
    joiner = " " if line.startswith("    ") else "\n"
    last.message = f"{last.message}{joiner}{stripped}"


def parse_drc_txt(text: str) -> DrcReport:
    report = DrcReport(format="txt")
    table: DrcTable | None = None
    footer_seen = False
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        stripped = line.strip()
        start = next((pair for pair in _TXT_STARTS if line.startswith(pair[0])), None)
        if start:
            if table is not None and table.type != WARNING:
                report.problems.append(f"'{table.title}' has no count line")
                table.count = len(table.violations)
            table = DrcTable(type=start[1], title=line[len(start[0]):].strip(), count=0)
            report.tables.append(table)
            continue
        if (
            table is not None
            and stripped.startswith(_TXT_COUNTS)
            and re.match(r"^[A-Za-z ]+:\s*\d+$", stripped)
        ):
            # 'Rule Violations :n' (no space), unlike the footer's 'Waived Violations : n'.
            table.count = int(stripped.rsplit(":", 1)[1])
            table = None
            continue
        footer = re.match(
            r"^(Violations Detected|Waived Violations|PCB Health Issues)\s*:\s*(\d+)$", stripped
        )
        if footer and (table is None or table.type == WARNING):
            table = None
            footer_seen = footer_seen or footer[1] == "Violations Detected"
            report.totals[_TXT_FOOTER[footer[1]]] = int(footer[2])
            continue
        header = re.match(r"^(PCB File|Date|Time|Time Elapsed)\s*:\s*(.*)$", stripped)
        if header and table is None:
            key, value = header[1], header[2].strip()
            if key == "PCB File":
                report.pcb_file = value
            elif key == "Date":
                report.date = value
            elif key == "Time":
                report.time = value
            else:
                report.elapsed = value
            continue
        if stripped.startswith("ERROR :") and table is None:
            report.errors.append(stripped[len("ERROR :"):].strip())
            continue
        if table is None:
            continue
        if not stripped:
            if table.type == WARNING:
                table.count = len(table.violations)
                table = None
            continue
        row_start = _ROW_STARTS.get(table.type)
        if row_start is not None:
            if line.startswith(row_start):
                table.violations.append(DrcViolation(message=line[len(row_start):].strip()))
                if table.type == WAIVED:
                    last = table.violations[-1]
                    found = _WAIVED_BY.search(last.message)
                    if found:
                        last.message, last.waiver = (
                            last.message[: found.start()].rstrip(), last.message[found.start():]
                        )
            elif table.violations:
                _continue_row(table, line)
        elif line.startswith("   ") and not line.startswith("    "):
            table.violations.append(DrcViolation(message=stripped))
        elif table.violations:
            _continue_row(table, line)
    if table is not None:
        if table.type != WARNING:
            report.problems.append(f"'{table.title}' has no count line")
        table.count = len(table.violations)
    if not text.strip():
        report.problems.append("the file is empty")
    elif not footer_seen:
        report.problems.append("the file ends early (no 'Violations Detected' footer)")
    _check_counts(report)
    return report
