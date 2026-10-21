"""get_design_hierarchy: sheet symbols, sheet entries and ports read from .SchDoc
records (15/16/32/33 and 18) with their wiring, the Net Identifier Scope from the
.PrjPcb, the sheet tree, consistency issues and, with a netlist, the net each port and
entry joins. Synthetic streams cover the records; Altium's public AD25 examples are
checked against the .PrjPcbStructure Altium wrote for them when installed."""
import json
import re
import struct
from pathlib import Path
from unittest.mock import patch

import pytest

import server.main as main
from parsers.prj_pcb import VariantDefinition, VariantState, parse_prj_pcb
from parsers.sch_doc import hierarchy_from_stream, parse_sch_hierarchy
from services.design_hierarchy import (
    AMBIGUOUS_COPIES_NOTE,
    ISSUE_HELP,
    build_design_hierarchy,
    page_design_hierarchy,
)

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")


def _record(body: str) -> bytes:
    data = body.encode("latin-1") + b"\0"
    return struct.pack("<I", len(data)) + data


def _stream(*records: str) -> bytes:
    """FileHeader bytes: the header, the sheet record (position 1), then the records;
    the first of them is at position 2, so its children have OwnerIndex=1."""
    out = _record("|HEADER=Protel for Windows - Schematic Capture Binary File Version 5.0")
    out += _record("|RECORD=31|FONTIDCOUNT=1|SIZE1=10")
    for rec in records:
        out += _record(rec)
    return out


def _symbol(owner: int, designator: str, file_name: str, entries=(), x=100, y=500,
            xsize=100, ysize=60) -> list[str]:
    """A sheet symbol at record position owner + 1 with its name, file name and entries
    (name, side, distance from top in 100 mil, extra fields)."""
    records = [f"|RECORD=15|LOCATION.X={x}|LOCATION.Y={y}|XSIZE={xsize}|YSIZE={ysize}"
               f"|UNIQUEID=S{owner}|SYMBOLTYPE=Normal"]
    for name, side, distance, extra in entries:
        records.append(f"|RECORD=16|OWNERINDEX={owner}|SIDE={side}|DISTANCEFROMTOP={distance}"
                       f"|NAME={name}{extra}")
    records.append(f"|RECORD=32|OWNERINDEX={owner}|TEXT={designator}")
    records.append(f"|RECORD=33|OWNERINDEX={owner}|TEXT={file_name}")
    return records


def _port(name: str, x: int, y: int, io: str = "", style: int = 3, width: int = 50,
          extra: str = "") -> str:
    io_field = f"|IOTYPE={io}" if io else ""
    return (f"|RECORD=18|STYLE={style}|WIDTH={width}|LOCATION.X={x}|LOCATION.Y={y}"
            f"|NAME={name}{io_field}{extra}")


def _wire(*points: tuple[int, int]) -> str:
    fields = "".join(f"|X{i}={x}|Y{i}={y}" for i, (x, y) in enumerate(points, 1))
    return f"|RECORD=27|LOCATIONCOUNT={len(points)}{fields}"


def _component(owner: int, refdes: str, pins) -> list[str]:
    """A resistor at position owner + 1; pins are (designator, x, y, orientation) with a
    20-unit length, so the electrical end is 20 units from Location."""
    records = [f"|RECORD=1|LIBREFERENCE=RES|CURRENTPARTID=1|PARTCOUNT=2|UNIQUEID=C{owner}"]
    for designator, x, y, orientation in pins:
        records.append(f"|RECORD=2|OWNERINDEX={owner}|OWNERPARTID=1|DESIGNATOR={designator}"
                       f"|PINLENGTH=20|PINCONGLOMERATE={orientation}"
                       f"|LOCATION.X={x}|LOCATION.Y={y}")
    records.append(f"|RECORD=34|OWNERINDEX={owner}|NAME=Designator|TEXT={refdes}")
    return records


# Top: symbol U_Child (positions 2..) with entries SDA (right, 100 mil down, I/O 3) and
# EN (left, 200 mil down, I/O 1). SDA's entry point is (2000, 4900) mil: a wire runs to
# R1 pin 1, whose end is at (3000, 4900). EN at (1000, 4800) is wired to nothing.
TOP = _stream(
    *_symbol(1, "U_Child", "child.SchDoc", [
        ("SDA", 1, 1, "|IOTYPE=3"), ("EN", 0, 2, "|IOTYPE=1"),
    ]),
    _wire((200, 490), (300, 490)),
    *_component(7, "R1", [("1", 320, 490, 2)]),
    "|RECORD=25|LOCATION.X=250|LOCATION.Y=490|TEXT=T_SDA_NET",
)
# Child: port SDA (I/O 3) wired to U5 pin 7; port EN (I/O 2) wired to nothing; port
# NC_PORT that no entry matches; power port VCC.
CHILD = _stream(
    _port("SDA", 100, 300, "3"),
    _wire((150, 300), (250, 300)),
    *_component(3, "U5", [("7", 270, 300, 2)]),
    _port("EN", 100, 200, "2"),
    _port("NC_PORT", 100, 100),
    "|RECORD=17|LOCATION.X=400|LOCATION.Y=400|TEXT=VCC|STYLE=1",
)


# ---------- the parser ----------

def test_sheet_symbol_with_its_entries():
    sheet = hierarchy_from_stream(TOP)
    [symbol] = sheet.symbols
    assert (symbol.designator, symbol.file_name, symbol.symbol_type) == (
        "U_Child", "child.SchDoc", "Normal")
    assert (symbol.x_mil, symbol.y_mil, symbol.width_mil, symbol.height_mil) == (
        1000.0, 5000.0, 1000.0, 600.0)
    assert [(e.name, e.io_type, e.side) for e in symbol.entries] == [
        ("SDA", "Bidirectional", "right"), ("EN", "Output", "left")]
    assert symbol.channel is None


def test_entry_wiring_reaches_pins_and_labels():
    sheet = hierarchy_from_stream(TOP)
    sda, en = sheet.symbols[0].entries
    group = sheet.groups[sda.group]
    assert group.pins == [("R1", "1")]
    assert group.labels == ["T_SDA_NET"]
    assert group.wires == 1 and group.wired
    assert not sheet.groups[en.group].wired


def test_ports_with_io_types_and_wiring():
    sheet = hierarchy_from_stream(CHILD)
    assert [(p.name, p.io_type) for p in sheet.ports] == [
        ("SDA", "Bidirectional"), ("EN", "Input"), ("NC_PORT", "Unspecified")]
    assert sheet.groups[sheet.ports[0].group].pins == [("U5", "7")]
    assert not sheet.groups[sheet.ports[1].group].wired
    assert sheet.power_ports == ["VCC"]


def test_top_and_bottom_entries_sit_on_those_edges():
    # The symbol spans x 1000..2000, y 5000 down to 4400 mil. TOP is 200 mil from the
    # left on the top edge, on R1 pin 1; BOTTOM is 300 mil from the left on the bottom
    # edge, on R2 pin 1.
    stream = _stream(*_symbol(1, "U_S", "s.SchDoc", [("TOP", 2, 2, ""), ("BOTTOM", 3, 3, "")]),
                     *_component(6, "R1", [("1", 120, 520, 3)]),
                     *_component(9, "R2", [("1", 130, 420, 1)]))
    sheet = hierarchy_from_stream(stream)
    top, bottom = sheet.symbols[0].entries
    assert (top.side, bottom.side) == ("top", "bottom")
    assert sheet.groups[top.group].pins == [("R1", "1")]
    assert sheet.groups[bottom.group].pins == [("R2", "1")]


def test_vertical_port_connects_above_its_location():
    stream = _stream(_port("UP", 100, 100, style=7, width=40), _wire((100, 140), (100, 200)),
                     *_component(3, "R2", [("2", 100, 220, 3)]))
    sheet = hierarchy_from_stream(stream)
    assert sheet.groups[sheet.ports[0].group].pins == [("R2", "2")]


def test_net_labels_and_ports_of_one_name_join_on_a_sheet():
    # The port's wire carries label A; R3's wire elsewhere carries label A too.
    stream = _stream(
        _port("P", 100, 100), _wire((150, 100), (200, 100)),
        "|RECORD=25|LOCATION.X=180|LOCATION.Y=100|TEXT=A",
        _wire((500, 500), (600, 500)), "|RECORD=25|LOCATION.X=550|LOCATION.Y=500|TEXT=a",
        *_component(6, "R3", [("1", 620, 500, 2)]),
    )
    sheet = hierarchy_from_stream(stream)
    assert sheet.groups[sheet.ports[0].group].pins == [("R3", "1")]


def test_crossing_wires_without_a_junction_stay_apart():
    stream = _stream(
        _port("P", 100, 100), _wire((150, 100), (300, 100)),
        _wire((200, 0), (200, 200)), *_component(4, "R4", [("1", 200, 220, 3)]),
    )
    sheet = hierarchy_from_stream(stream)
    assert sheet.groups[sheet.ports[0].group].pins == []
    with_junction = _stream(
        _port("P", 100, 100), _wire((150, 100), (300, 100)),
        _wire((200, 0), (200, 200)), *_component(4, "R4", [("1", 200, 220, 3)]),
        "|RECORD=29|LOCATION.X=200|LOCATION.Y=100",
    )
    sheet = hierarchy_from_stream(with_junction)
    assert sheet.groups[sheet.ports[0].group].pins == [("R4", "1")]


def test_hidden_pins_and_other_parts_pins_are_left_out():
    stream = _stream(
        _port("P", 100, 100), _wire((150, 100), (200, 100)),
        "|RECORD=1|LIBREFERENCE=IC|CURRENTPARTID=1|PARTCOUNT=3|UNIQUEID=U",
        "|RECORD=2|OWNERINDEX=3|OWNERPARTID=2|DESIGNATOR=9|PINLENGTH=20|PINCONGLOMERATE=2"
        "|LOCATION.X=220|LOCATION.Y=100",
        "|RECORD=2|OWNERINDEX=3|OWNERPARTID=1|DESIGNATOR=8|PINLENGTH=20|PINCONGLOMERATE=6"
        "|LOCATION.X=220|LOCATION.Y=100",
        "|RECORD=34|OWNERINDEX=3|NAME=Designator|TEXT=U1",
    )
    sheet = hierarchy_from_stream(stream)
    assert sheet.groups[sheet.ports[0].group].pins == []


def test_signal_harness_in_the_additional_stream_wires_a_port():
    stream = _stream(_port("BUS", 100, 100, extra="|HARNESSTYPE=I2C"))
    additional = _stream("|RECORD=218|LOCATIONCOUNT=2|X1=150|Y1=100|X2=300|Y2=100")
    assert not hierarchy_from_stream(stream).groups[0].wired
    sheet = hierarchy_from_stream(stream, additional)
    assert sheet.ports[0].harness_type == "I2C"
    assert sheet.groups[sheet.ports[0].group].wired


def test_repeat_designator_and_entry():
    stream = _stream(*_symbol(1, "Repeat(CH, 1, 4)", "chan.SchDoc", [
        ("Repeat(ADC)", 0, 1, ""), ("CLK", 0, 2, "|DISTANCEFROMTOP_FRAC1=50000"),
    ]))
    [symbol] = hierarchy_from_stream(stream).symbols
    assert symbol.channel == ("CH", 1, 4)
    assert [(e.port_name, e.repeated) for e in symbol.entries] == [("ADC", True), ("CLK", False)]


# ---------- the .PrjPcb ----------

def test_prjpcb_scope_naming_options_and_device_sheets(tmp_path):
    prj = tmp_path / "p.PrjPcb"
    prj.write_text(
        "[Design]\nHierarchyMode=2\nAllowPortNetNames=1\nAllowSheetEntryNetNames=0\n"
        "NameNetsHierarchically=1\n"
        "[Document1]\nDocumentPath=top.SchDoc\n"
        "[DeviceSheet1]\nDocumentPath=Managed\\Sheets\\X\\Dev.SchDoc\n", encoding="utf-8")
    data = parse_prj_pcb(str(prj))
    assert data.hierarchy_mode == "2"
    assert data.net_naming == {"AllowPortNetNames": "1", "AllowSheetEntryNetNames": "0",
                               "NameNetsHierarchically": "1"}
    assert [Path(p).name for p in data.device_sheet_paths] == ["Dev.SchDoc"]
    assert [Path(p).name for p in data.sheet_paths] == ["top.SchDoc", "Dev.SchDoc"]


def test_fixture_project_scope_is_read():
    data = parse_prj_pcb(str(FIXTURES / "test.prjpcb"))
    assert data.hierarchy_mode == "4"
    assert data.net_naming["AllowPortNetNames"] == "0"


# ---------- the model ----------

def _project(tmp_path, sheets: dict[str, bytes], prj_extra: str = "", devices=()):
    """A project of synthetic sheets: (project dict, read_sheet)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    prj = tmp_path / "Board.PrjPcb"
    docs = "".join(f"[Document{i}]\nDocumentPath={name}\n" for i, name in
                   enumerate([n for n in sheets if n not in devices], 1))
    docs += "".join(f"[DeviceSheet{i}]\nDocumentPath={name}\n" for i, name in
                    enumerate(devices, 1))
    prj.write_text(f"[Design]\n{prj_extra}{docs}", encoding="utf-8")
    data = parse_prj_pcb(str(prj))
    streams = {Path(p).name.lower(): sheets[Path(p).name] for p in data.sheet_paths}

    def read_sheet(path):
        return hierarchy_from_stream(streams[Path(path).name.lower()])

    project = {"name": "Board", "prj_pcb_path": str(prj),
               "sheets": [{"name": Path(p).stem, "path": p} for p in data.sheet_paths]}
    return project, read_sheet


def _kinds(model) -> list[tuple[str, str]]:
    return [(i["kind"], i["severity"]) for i in model["issues"]]


def test_tree_scope_and_entries(tmp_path):
    project, read = _project(tmp_path, {"top.SchDoc": TOP, "child.SchDoc": CHILD})
    model = build_design_hierarchy(project, read)
    assert model["tree"] == ["top [top]", "  U_Child -> child"]
    scope = model["scope"]
    assert scope["setting"].startswith("Automatic") and scope["hierarchy_mode"] is None
    assert scope["effective"] == "Hierarchical"
    assert "top sheet has sheet entries" in scope["resolved_because"]
    assert "sheet entry" in scope["explanation"]
    top, child = model["sheets"]
    assert top["top"] is True
    [symbol] = top["sheet_symbols"]
    assert symbol["child"] == "child"
    assert symbol["entries"][0] == {"name": "SDA", "io": "Bidirectional",
                                    "labels": ["T_SDA_NET"]}
    assert symbol["entries"][1]["port_io"] == "Input"  # differs from the entry's Output
    assert child["placed_by"] == [{"sheet": "top", "symbol": "U_Child"}]
    assert [p["name"] for p in child["ports"]] == ["SDA", "EN", "NC_PORT"]
    assert "nets_note" in model


def test_issues_of_a_hierarchical_design(tmp_path):
    project, read = _project(tmp_path, {"top.SchDoc": TOP, "child.SchDoc": CHILD})
    model = build_design_hierarchy(project, read)
    issues = {i["kind"]: i for i in model["issues"]}
    assert issues["io_mismatch"] == {
        "kind": "io_mismatch", "severity": "warning", "sheet": "top", "entry": "EN",
        "entry_io": "Output", "port_io": "Input", "symbol": "U_Child", "child": "child"}
    assert issues["port_without_entry"]["port"] == "NC_PORT"
    assert issues["port_without_entry"]["severity"] == "warning"
    assert issues["unwired_entry"]["entry"] == "EN"
    # Findings of one kind on one sheet are listed together, and counted one by one.
    assert issues["unwired_port"]["ports"] == ["EN", "NC_PORT"]
    assert model["summary"]["issue_kinds"]["unwired_port"] == 2
    page = page_design_hierarchy(model, 0, 10, 50)
    assert set(page["issue_help"]) == {i["kind"] for i in page["issues"]}
    assert set(ISSUE_HELP) >= set(issues)


def test_entry_without_port_and_power_ports_by_scope(tmp_path):
    top = _stream(*_symbol(1, "U_Child", "child.SchDoc", [("VCC", 0, 1, ""), ("RST", 0, 2, "")]))
    child = _stream("|RECORD=17|LOCATION.X=400|LOCATION.Y=400|TEXT=VCC",
                    "|RECORD=25|LOCATION.X=400|LOCATION.Y=300|TEXT=RST")
    project, read = _project(tmp_path, {"top.SchDoc": top, "child.SchDoc": child})
    kinds = _kinds(build_design_hierarchy(project, read))
    assert ("entry_matches_power_port", "info") in kinds
    assert ("entry_without_port", "error") in kinds
    strict, read = _project(tmp_path / "s", {"top.SchDoc": top, "child.SchDoc": child},
                            "HierarchyMode=4\n")
    model = build_design_hierarchy(strict, read)
    assert model["scope"]["effective"] == "Strict Hierarchical"
    assert "note" in model["scope"]
    missing = [i for i in model["issues"] if i["kind"] == "entry_without_port"]
    assert [(i["entry"], i.get("child_has_power_port"), i.get("child_has_net_label"))
            for i in missing] == [("VCC", True, None), ("RST", None, True)]


@pytest.mark.parametrize("mode, setting, effective", [
    ("1", "Flat (Only ports global)", "Flat"),
    ("2", "Hierarchical (Sheet entry <-> port connections, power ports global)",
     "Hierarchical"),
    ("3", "Global (Netlabels and ports global)", "Global"),
    ("4", "Strict Hierarchical (Sheet entry <-> port connections, power ports local)",
     "Strict Hierarchical"),
])
def test_scope_values(tmp_path, mode, setting, effective):
    project, read = _project(tmp_path, {"a.SchDoc": _stream()}, f"HierarchyMode={mode}\n")
    scope = build_design_hierarchy(project, read)["scope"]
    assert (scope["setting"], scope["effective"]) == (setting, effective)
    assert "automatic_rule" not in scope


def test_automatic_resolves_flat_with_ports_and_global_without(tmp_path):
    sheets = {"a.SchDoc": _stream(_port("X", 0, 0)), "b.SchDoc": _stream(_port("Y", 0, 0))}
    project, read = _project(tmp_path, sheets, "HierarchyMode=0\n")
    model = build_design_hierarchy(project, read)
    assert model["scope"]["effective"] == "Flat"
    assert model["summary"]["top_sheets"] == ["a", "b"]
    assert {(i["kind"], i["port"]) for i in model["issues"] if i["kind"] == "port_alone"} == {
        ("port_alone", "X"), ("port_alone", "Y")}
    project, read = _project(tmp_path / "g", {"a.SchDoc": _stream()})
    assert build_design_hierarchy(project, read)["scope"]["effective"] == "Global"


def test_unknown_scope_value_falls_back_to_the_automatic_rule(tmp_path):
    project, read = _project(tmp_path, {"a.SchDoc": _stream()}, "HierarchyMode=9\n")
    scope = build_design_hierarchy(project, read)["scope"]
    assert scope["setting"] == "Unknown (9)" and scope["effective"] == "Global"


def test_naming_options_order(tmp_path):
    project, read = _project(tmp_path, {"a.SchDoc": _stream()},
                             "AllowPortNetNames=1\nAllowSheetEntryNetNames=1\n"
                             "PowerPortNamesTakePriority=1\n")
    naming = build_design_hierarchy(project, read)["scope"]["net_naming"]
    assert naming["name_precedence"][:4] == ["power port", "net label", "port", "sheet entry"]
    assert naming["allow_ports_to_name_nets"] is True


def test_repeated_channels_device_sheets_and_copies(tmp_path):
    top = _stream(*_symbol(1, "Repeat(CH,1,4)", "chan.SchDoc", [("Repeat(IN)", 0, 1, "")]),
                  *_symbol(5, "U_Dev", "Dev.SchDoc", y=900))
    chan = _stream(_port("IN", 0, 0), *_symbol(2, "U_Dev2", "Dev.SchDoc"))
    project, read = _project(tmp_path, {"top.SchDoc": top, "chan.SchDoc": chan,
                                        "Dev.SchDoc": _stream()}, devices=("Dev.SchDoc",))
    model = build_design_hierarchy(project, read)
    assert model["tree"] == [
        "top [top]",
        "  Repeat(CH,1,4) -> chan [4 channels, 4 compiled copies]",
        "    U_Dev2 -> Dev [device sheet, 4 compiled copies]",
        "  U_Dev -> Dev [device sheet]",
    ]
    symbol = model["sheets"][0]["sheet_symbols"][0]
    assert symbol["channels"] == {"name": "CH", "first": 1, "last": 4, "count": 4}
    assert "port_io" not in symbol["entries"][0]  # the port matches
    dev = next(s for s in model["sheets"] if s["sheet"] == "Dev")
    assert dev["device_sheet"] is True and dev["compiled_copies"] == 5
    assert model["summary"]["repeated_symbols"] == 1
    assert model["summary"]["device_sheets"] == 1
    assert ("sheet_placed_several_times", "info") in _kinds(model)


def test_missing_child_cycle_and_duplicates(tmp_path):
    (tmp_path / "loose.SchDoc").write_bytes(b"")
    top = _stream(*_symbol(1, "U_A", "a.SchDoc"), *_symbol(4, "U_A", "loose.SchDoc", y=900),
                  *_symbol(7, "U_B", "gone.SchDoc", y=1300), *_symbol(10, "U_C", "", y=1700))
    a = _stream(*_symbol(1, "U_Back", "a.SchDoc", [("X", 0, 1, ""), ("x", 0, 2, "")]))
    project, read = _project(tmp_path, {"top.SchDoc": top, "a.SchDoc": a})
    model = build_design_hierarchy(project, read)
    issues = model["issues"]
    missing = [(i["file"], i["on_disk"]) for i in issues if i["kind"] == "missing_child_sheet"]
    assert missing == [("loose.SchDoc", True), ("gone.SchDoc", False)]
    assert ("symbol_without_file", "error") in _kinds(model)
    assert ("sheet_cycle", "error") in _kinds(model)
    assert ("duplicate_sheet_symbol", "warning") in _kinds(model)
    assert ("duplicate_sheet_entry", "warning") in _kinds(model)
    assert "    U_Back -> a [cycle: already above]" in model["tree"]
    assert "  U_B -> gone.SchDoc [missing]" in model["tree"]


def test_top_sheet_ports_and_multiple_tops_under_a_hierarchical_scope(tmp_path):
    sheets = {"a.SchDoc": _stream(_port("X", 0, 0)), "b.SchDoc": _stream()}
    project, read = _project(tmp_path, sheets, "HierarchyMode=2\n")
    kinds = _kinds(build_design_hierarchy(project, read))
    assert kinds.count(("multiple_top_sheets", "warning")) == 2
    assert ("top_sheet_port", "warning") in kinds


def test_focus_on_a_subtree_and_unknown_sheet(tmp_path):
    project, read = _project(tmp_path, {"top.SchDoc": TOP, "child.SchDoc": CHILD})
    model = build_design_hierarchy(project, read, sheet="child.schdoc")
    assert model["focus"] == "child"
    assert model["tree"] == ["U_Child -> child [subtree]"]
    assert [s["sheet"] for s in model["sheets"]] == ["child"]
    assert all(i["sheet"] == "child" for i in model["issues"])
    assert model["summary"]["sheets"] == 2  # the summary is the whole project's
    error = build_design_hierarchy(project, read, sheet="nope")
    assert error["error"] == "sheet_not_found"


def test_unreadable_sheet_is_an_issue(tmp_path):
    project, _ = _project(tmp_path, {"top.SchDoc": TOP, "child.SchDoc": CHILD})

    def read(path):
        if Path(path).name == "child.SchDoc":
            raise OSError("bad file")
        return hierarchy_from_stream(TOP)

    model = build_design_hierarchy(project, read)
    assert ("sheet_not_readable", "error") in _kinds(model)
    assert model["sheets"][1]["error"] == "OSError: bad file"


# ---------- nets from the compiled netlist ----------

NETLIST = {
    "components": {
        "R1": {"sheet": "top", "pins": {"1": {"net": "T_SDA_NET"}}},
        "U5": {"sheet": "child", "pins": {"7": {"net": "T_SDA_NET"}}},
    },
}


def test_ports_and_entries_name_their_net(tmp_path):
    project, read = _project(tmp_path, {"top.SchDoc": TOP, "child.SchDoc": CHILD})
    model = build_design_hierarchy(project, read, NETLIST)
    top, child = model["sheets"]
    sda, en = top["sheet_symbols"][0]["entries"]
    assert sda["net"] == "T_SDA_NET" and "net_via" not in sda
    assert en["net"] is None
    assert child["ports"][0]["net"] == "T_SDA_NET"
    assert "nets_note" not in model


def test_pins_in_no_compiled_net_are_explained(tmp_path):
    project, read = _project(tmp_path, {"top.SchDoc": TOP, "child.SchDoc": CHILD})
    netlist = {"components": {"U5": {"sheet": "child", "pins": {}}}}
    port = build_design_hierarchy(project, read, netlist)["sheets"][1]["ports"][0]
    assert port["net"] is None and "single-pin net" in port["net_note"]


def test_a_port_without_pins_takes_the_net_across_the_hierarchy(tmp_path):
    child = _stream(_port("SDA", 100, 300, "3"))
    project, read = _project(tmp_path, {"top.SchDoc": TOP, "child.SchDoc": child})
    model = build_design_hierarchy(project, read, NETLIST)
    port = model["sheets"][1]["ports"][0]
    assert (port["net"], port["net_via"]) == ("T_SDA_NET", "other sheets")


# Two symbols place one leaf sheet; their entries IN have no pins on the parent, the
# leaf's port IN is wired to R9 pin 1 (one compiled copy per symbol).
TWO_PLACEMENTS = _stream(*_symbol(1, "U_A", "leaf.SchDoc", [("IN", 0, 1, "")]),
                         *_symbol(5, "U_B", "leaf.SchDoc", [("IN", 0, 1, "")], y=900))
LEAF = _stream(_port("IN", 100, 300), _wire((150, 300), (250, 300)),
               *_component(3, "R9", [("1", 270, 300, 2)]))


def _copies_netlist(paths: bool) -> dict:
    return {"components": {
        f"R9_{name}": {"sheet": "leaf", "logical_refdes": "R9", "pins": {"1": {"net": net}}}
        | ({"unique_paths": [f"\\{uid}\\C4"]} if paths else {})
        for name, uid, net in (("A", "S1", "NA"), ("B", "S5", "NB"))
    }}


def test_an_entry_takes_only_its_own_copy_of_the_child_net(tmp_path):
    project, read = _project(tmp_path, {"top.SchDoc": TWO_PLACEMENTS, "leaf.SchDoc": LEAF})
    model = build_design_hierarchy(project, read, _copies_netlist(paths=True))
    top, leaf = model["sheets"]
    entries = [s["entries"][0] for s in top["sheet_symbols"]]
    assert [(e["net"], e["net_via"]) for e in entries] == [
        ("NA", "other sheets"), ("NB", "other sheets")]
    assert not any("net_note" in e for e in entries)
    assert leaf["ports"][0]["nets"] == ["NA", "NB"]  # the port: one net per copy


def test_copies_that_cannot_be_told_apart_are_flagged(tmp_path):
    project, read = _project(tmp_path, {"top.SchDoc": TWO_PLACEMENTS, "leaf.SchDoc": LEAF})
    model = build_design_hierarchy(project, read, _copies_netlist(paths=False))
    entry = model["sheets"][0]["sheet_symbols"][0]["entries"][0]
    assert entry["nets"] == ["NA", "NB"] and entry["net_note"] == AMBIGUOUS_COPIES_NOTE
    assert "net_note" not in model["sheets"][1]["ports"][0]


def test_an_entry_does_not_take_a_sibling_symbols_net(tmp_path):
    # U_B's entry sits on R2 pin 1; U_A's entry and the leaf's port have no pins.
    top = _stream(*_symbol(1, "U_A", "leaf.SchDoc", [("IN", 0, 1, "")]),
                  *_symbol(5, "U_B", "leaf.SchDoc", [("IN", 0, 1, "")], y=900),
                  *_component(9, "R2", [("1", 80, 890, 0)]))
    project, read = _project(tmp_path, {"top.SchDoc": top,
                                        "leaf.SchDoc": _stream(_port("IN", 100, 300))})
    netlist = {"components": {"R2": {"sheet": "top", "pins": {"1": {"net": "NB"}}}}}
    model = build_design_hierarchy(project, read, netlist)
    entry_a, entry_b = (s["entries"][0] for s in model["sheets"][0]["sheet_symbols"])
    assert entry_a["net"] is None and entry_b["net"] == "NB"
    assert model["sheets"][1]["ports"][0]["net"] == "NB"


def test_entries_wired_together_reach_both_copies(tmp_path):
    # The two entries IN are wired to each other on the parent: one net for both copies.
    top = _stream(*_symbol(1, "U_A", "leaf.SchDoc", [("IN", 0, 1, "")]),
                  *_symbol(5, "U_B", "leaf.SchDoc", [("IN", 0, 1, "")], y=900),
                  _wire((100, 490), (100, 890)))
    project, read = _project(tmp_path, {"top.SchDoc": top, "leaf.SchDoc": LEAF})
    model = build_design_hierarchy(project, read, _copies_netlist(paths=True))
    for symbol in model["sheets"][0]["sheet_symbols"]:
        assert symbol["entries"][0]["nets"] == ["NA", "NB"]
        assert "net_note" not in symbol["entries"][0]


def test_a_power_port_reaches_every_copy_of_another_sheet(tmp_path):
    # U_A and U_A2 place leaf1, whose port IN carries power port PWR and no pins. U_B
    # and U_C place leaf2, whose port Q carries PWR and R9 pin 1 (net PWRNET per copy).
    pwr = "|RECORD=17|LOCATION.X=150|LOCATION.Y=300|TEXT=PWR"
    top = _stream(*_symbol(1, "U_A", "leaf1.SchDoc", [("IN", 0, 1, "")]),
                  *_symbol(5, "U_A2", "leaf1.SchDoc", [("IN", 0, 1, "")], y=900),
                  *_symbol(9, "U_B", "leaf2.SchDoc", y=1300),
                  *_symbol(12, "U_C", "leaf2.SchDoc", y=1700))
    leaf1 = _stream(_port("IN", 100, 300), pwr)
    leaf2 = _stream(_port("Q", 100, 300), pwr, _wire((150, 300), (250, 300)),
                    *_component(4, "R9", [("1", 270, 300, 2)]))
    project, read = _project(tmp_path, {"top.SchDoc": top, "leaf1.SchDoc": leaf1,
                                        "leaf2.SchDoc": leaf2})
    netlist = {"components": {
        f"R9_{uid}": {"sheet": "leaf2", "logical_refdes": "R9",
                      "unique_paths": [f"\\{uid}\\C5"], "pins": {"1": {"net": "PWRNET"}}}
        for uid in ("S9", "S12")
    }}
    entry = build_design_hierarchy(project, read, netlist)["sheets"][0]["sheet_symbols"][0][
        "entries"][0]
    assert (entry["net"], entry["net_via"]) == ("PWRNET", "other sheets")


def test_a_net_found_two_sheets_up(tmp_path):
    # top: U_Mid's entry A on R1 pin 1. mid: port A wired to U_Leaf's entry A. leaf: port A.
    top = _stream(*_symbol(1, "U_Mid", "mid.SchDoc", [("A", 1, 1, "")]),
                  _wire((200, 490), (300, 490)), *_component(6, "R1", [("1", 320, 490, 2)]))
    mid = _stream(_port("A", 100, 300), _wire((150, 300), (150, 480), (100, 480)),
                  *_symbol(3, "U_Leaf", "leaf.SchDoc", [("A", 0, 2, "")]))
    project, read = _project(tmp_path, {"top.SchDoc": top, "mid.SchDoc": mid,
                                        "leaf.SchDoc": _stream(_port("A", 100, 300))})
    netlist = {"components": {"R1": {"sheet": "top", "pins": {"1": {"net": "SIG"}}}}}
    leaf = build_design_hierarchy(project, read, netlist)["sheets"][2]
    assert (leaf["sheet"], leaf["ports"][0]["net"]) == ("leaf", "SIG")


def _two_ports(kind: str) -> dict[str, bytes]:
    """Sheet a: port P with a power port or net label X at its end and no pins. Sheet b:
    port Q with X at its end, wired to R1 pin 1."""
    record = "17" if kind == "power" else "25"
    marker = f"|RECORD={record}|LOCATION.X=150|LOCATION.Y=100|TEXT=X"
    return {"a.SchDoc": _stream(_port("P", 100, 100), marker),
            "b.SchDoc": _stream(_port("Q", 100, 100), marker, _wire((150, 100), (250, 100)),
                                *_component(4, "R1", [("1", 270, 100, 2)]))}


@pytest.mark.parametrize("kind, mode, net", [
    ("power", "2", "NX"), ("power", "4", None), ("power", "1", "NX"),
    ("label", "3", "NX"), ("label", "1", None), ("label", "2", None),
])
def test_power_ports_and_net_labels_cross_sheets_by_scope(tmp_path, kind, mode, net):
    project, read = _project(tmp_path, _two_ports(kind), f"HierarchyMode={mode}\n")
    netlist = {"components": {"R1": {"sheet": "b", "pins": {"1": {"net": "NX"}}}}}
    port = build_design_hierarchy(project, read, netlist)["sheets"][0]["ports"][0]
    assert port["net"] == net


def test_channel_copies_named_by_suffix(tmp_path):
    top = _stream(*_symbol(1, "Repeat(CH,1,2)", "child.SchDoc", [("Repeat(SDA)", 1, 1, "")]))
    project, read = _project(tmp_path, {"top.SchDoc": top, "child.SchDoc": CHILD})
    netlist = {"components": {
        "U5A": {"sheet": "child", "pins": {"7": {"net": "SDA_A"}}},
        "U5B": {"sheet": "child", "pins": {"7": {"net": "SDA_B"}}},
    }}
    model = build_design_hierarchy(project, read, netlist)
    assert model["sheets"][1]["ports"][0]["nets"] == ["SDA_A", "SDA_B"]


def test_port_alone_unless_a_power_port_carries_it(tmp_path):
    vcc = "|RECORD=17|LOCATION.X=150|LOCATION.Y=100|TEXT=VCC"
    sheets = {"a.SchDoc": _stream(_port("X", 100, 100), vcc), "b.SchDoc": _stream(_port("Y", 0, 0))}
    project, read = _project(tmp_path, sheets, "HierarchyMode=1\n")
    alone = [i for i in build_design_hierarchy(project, read)["issues"] if i["kind"] == "port_alone"]
    assert [i["port"] for i in alone] == ["Y"]


def test_symbol_checks_channels_harness_and_repeat_entries(tmp_path):
    top = _stream(*_symbol(1, "Repeat(CH,4,1)", "child.SchDoc"),
                  *_symbol(4, "U_H", "child.SchDoc", [("BUS", 0, 1, "|HARNESSTYPE=I2C")], y=900),
                  *_symbol(8, "U_R", "child.SchDoc", [("Repeat(BUS)", 0, 1, "")], y=1300))
    project, read = _project(tmp_path, {"top.SchDoc": top,
                                        "child.SchDoc": _stream(_port("BUS", 0, 0))})
    model = build_design_hierarchy(project, read)
    issues = {i["kind"]: i for i in model["issues"]}
    assert issues["bad_channel_range"]["symbol"] == "Repeat(CH,4,1)"
    assert (issues["harness_mismatch"]["entry_harness"],
            issues["harness_mismatch"]["port_harness"]) == ("I2C", None)
    assert issues["repeat_entry_without_channels"]["symbol"] == "U_R"
    assert "  Repeat(CH,4,1) -> child [0 channels]" in model["tree"]


def test_ports_of_a_flat_designs_structural_symbol_are_not_flagged(tmp_path):
    sheets = {"top.SchDoc": _stream(*_symbol(1, "U_C", "child.SchDoc")),
              "child.SchDoc": _stream(_port("X", 0, 0))}
    for mode, flagged in (("1", False), ("3", False), ("2", True)):
        project, read = _project(tmp_path / mode, sheets, f"HierarchyMode={mode}\n")
        kinds = [k for k, _ in _kinds(build_design_hierarchy(project, read))]
        assert ("port_without_entry" in kinds) is flagged


def test_a_symbol_path_with_a_folder_picks_that_sheet(tmp_path):
    prj = tmp_path / "Board.PrjPcb"
    prj.write_text("[Design]\n[Document1]\nDocumentPath=top.SchDoc\n[Document2]\n"
                   "DocumentPath=x.SchDoc\n[Document3]\nDocumentPath=sub\\x.SchDoc\n",
                   encoding="utf-8")
    streams = {"top": _stream(*_symbol(1, "U_X", "sub\\x.SchDoc")),
               "root": _stream(_port("ROOT", 0, 0)), "sub": _stream(_port("SUB", 0, 0))}

    def read(path):
        p = Path(path)
        return hierarchy_from_stream(
            streams["top" if p.stem == "top" else "sub" if p.parent.name == "sub" else "root"])

    paths = parse_prj_pcb(str(prj)).sheet_paths
    project = {"name": "Board", "prj_pcb_path": str(prj),
               "sheets": [{"name": Path(p).stem, "path": p} for p in paths]}
    placed = next(s for s in build_design_hierarchy(project, read)["sheets"] if "placed_by" in s)
    assert [p["name"] for p in placed["ports"]] == ["SUB"]


def test_a_diagonal_wire_connects(tmp_path):
    stream = _stream(_port("P", 100, 100), _wire((150, 100), (250, 200)),
                     *_component(3, "R5", [("1", 270, 200, 2)]))
    sheet = hierarchy_from_stream(stream)
    assert sheet.groups[sheet.ports[0].group].pins == [("R5", "1")]


@pytest.mark.parametrize("mode, pins", [("0", [("U1", "8")]), ("1", [])])
def test_pins_of_another_display_mode_are_left_out(mode, pins):
    stream = _stream(
        _port("P", 100, 100), _wire((150, 100), (200, 100)),
        "|RECORD=1|LIBREFERENCE=IC|CURRENTPARTID=1|DISPLAYMODE=0|UNIQUEID=U",
        f"|RECORD=2|OWNERINDEX=3|OWNERPARTID=1|OWNERPARTDISPLAYMODE={mode}|DESIGNATOR=8"
        "|PINLENGTH=20|PINCONGLOMERATE=2|LOCATION.X=220|LOCATION.Y=100",
        "|RECORD=34|OWNERINDEX=3|NAME=Designator|TEXT=U1",
    )
    sheet = hierarchy_from_stream(stream)
    assert sheet.groups[sheet.ports[0].group].pins == pins


def test_help_and_naming_text_claim_only_what_altium_documents(tmp_path):
    assert "Bidirectional" in ISSUE_HELP["io_mismatch"]
    assert "wrong way round" not in ISSUE_HELP["io_mismatch"]
    project, read = _project(tmp_path, {"a.SchDoc": _stream()})
    explanation = build_design_hierarchy(project, read)["scope"]["net_naming"]["explanation"]
    assert "lower sheet's wins" not in explanation
    assert "no rule" in explanation


def test_channel_copies_give_one_net_each(tmp_path):
    top = _stream(*_symbol(1, "Repeat(CH,1,2)", "child.SchDoc", [("Repeat(SDA)", 1, 1, "")]))
    project, read = _project(tmp_path, {"top.SchDoc": top, "child.SchDoc": CHILD})
    netlist = {"components": {
        "U5": {"sheet": "child", "pins": {"7": {"net": "T_SDA_C1"}}},
        "U5_CH2": {"sheet": "child", "logical_refdes": "U5", "pins": {"7": {"net": "T_SDA_C2"}}},
    }}
    model = build_design_hierarchy(project, read, netlist)
    assert model["sheets"][1]["ports"][0]["nets"] == ["T_SDA_C1", "T_SDA_C2"]


# ---------- the tool ----------

@pytest.fixture
def loaded():
    project = {
        "name": "Board", "root_dir": str(FIXTURES), "prj_pcb_path": str(FIXTURES / "none.PrjPcb"),
        "sheets": [{"name": n, "path": str(FIXTURES / f"{n}.SchDoc")} for n in ("sheet1", "sheet2")],
    }
    with patch.object(main, "_project", project), \
         patch.object(main, "_variant_state", VariantState([VariantDefinition(name="Default")])), \
         patch.object(main, "_design_notes", main.DesignNotesCache()), \
         patch.object(main._altium, "_netlist", None):
        yield project


def test_tool_reads_the_fixture_sheets_without_a_netlist(loaded):
    result = json.loads(main.get_design_hierarchy())
    assert result["project"] == "Board"
    # No .PrjPcb to read: Automatic, ports and no entries -> Flat.
    assert result["scope"]["effective"] == "Flat"
    assert result["tree"] == ["sheet1 [top]", "sheet2 [top]"]
    assert result["summary"]["ports"] > 0
    assert result["sheets_total"] == 2 and result["has_more"] is False
    port = next(p for p in result["sheets"][0]["ports"] if p["name"] == "EDISC_CLOSED")
    assert port["io"] == "Input" and "net" not in port


def test_tool_pages_and_errors(loaded):
    page = json.loads(main.get_design_hierarchy(offset=1))
    assert page["offset"] == 1 and "tree" not in page and "issues" not in page
    assert [s["sheet"] for s in page["sheets"]] == ["sheet2"]
    assert json.loads(main.get_design_hierarchy(sheet="nope"))["error"] == "sheet_not_found"
    with patch.object(main, "_project", None):
        assert json.loads(main.get_design_hierarchy())["error"] == "no_project"


def test_instructions_mention_the_tool_after_the_cut():
    instructions = main.SERVER_INSTRUCTIONS
    assert instructions.index("get_design_hierarchy") > 4096


# ---------- Altium's public examples ----------

def _example_projects():
    if not EXAMPLES.exists():
        return []
    return sorted(p for p in EXAMPLES.rglob("*.PrjPcb")
                  if p.with_suffix(".PrjPcbStructure").exists())


def _model(prj_path: Path) -> dict:
    data = parse_prj_pcb(str(prj_path))
    project = {"name": prj_path.stem, "prj_pcb_path": str(prj_path),
               "sheets": [{"name": Path(p).stem, "path": p} for p in data.sheet_paths]}
    return build_design_hierarchy(project, parse_sch_hierarchy)


@pytest.mark.parametrize("prj_path", _example_projects(), ids=lambda p: p.stem)
def test_example_symbols_match_altiums_project_structure(prj_path):
    """Every sheet symbol (sheet, designator, file) Altium listed in .PrjPcbStructure."""
    expected = []
    text = prj_path.with_suffix(".PrjPcbStructure").read_text(encoding="utf-8-sig",
                                                             errors="replace")
    for line in text.splitlines():
        if line.startswith("Record=SheetSymbol"):
            fields = dict(part.split("=", 1) for part in line.split("|") if "=" in part)
            expected.append((Path(fields["SourceDocument"]).stem.lower(),
                             fields["Designator"], fields["FileName"].lower()))
    data = parse_prj_pcb(str(prj_path))
    found = []
    for path in data.sheet_paths:
        if Path(path).exists():
            for symbol in parse_sch_hierarchy(path).symbols:
                found.append((Path(path).stem.lower(), symbol.designator,
                              symbol.file_name.lower()))
    assert sorted(found) == sorted(expected)


def _example(relative: str) -> Path:
    path = EXAMPLES / relative
    if not path.exists():
        pytest.skip("Altium's AD25 examples are not installed")
    return path


def test_example_bluetooth_sentinel():
    model = _model(_example("Bluetooth Sentinel/Bluetooth_Sentinel.PrjPcb"))
    assert model["scope"]["setting"].startswith("Automatic")
    assert model["scope"]["effective"] == "Hierarchical"
    assert model["tree"][:3] == [
        "Bluetooth_Sentinel [top]",
        "  Power -> CR2032_Battery_Connector [device sheet]",
        "  LEDs -> Visual_LEDx6",
    ]
    summary = model["summary"]
    assert (summary["sheets"], summary["sheet_symbols"], summary["device_sheets"],
            summary["ports"], summary["sheet_entries"]) == (15, 20, 10, 48, 60)
    assert summary["issue_kinds"] == {"duplicate_sheet_symbol": 1, "unwired_entry": 1,
                                      "sheet_placed_several_times": 2}


def test_example_mini_pc_is_hierarchical_by_setting():
    model = _model(_example("Mini PC/Mini PC - Motherboard/MiniPC.PrjPcb"))
    scope = model["scope"]
    assert (scope["hierarchy_mode"], scope["effective"]) == ("2", "Hierarchical")
    assert scope["net_naming"]["higher_level_names_take_priority"] is True
    assert model["summary"]["top_sheets"] == ["MiniPC_Func"]
    assert model["summary"]["sheet_symbols"] == 45
    # Its decoupling sheets take power through global power ports, not their entries.
    assert model["summary"]["issue_kinds"] == {"entry_matches_power_port": 122}


def test_example_spirit_level_io_mismatch():
    model = _model(_example("SpiritLevel-SL1/SL1 Xilinx Spartan-IIE PQ208 Rev1.02.PrjPcb"))
    assert [i["kind"] for i in model["issues"]] == ["io_mismatch"]
    assert all(re.match(r"^\s*U_SL_", line) for line in model["tree"][1:])
