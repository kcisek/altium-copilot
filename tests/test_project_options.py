"""get_project_options: the .PrjPcb option sections (Net Identifier Scope, naming, diff
pairs, class generation, multi-channel, ERC levels, Connection Matrix, Comparator, output
path), the audit against Altium's defaults, and No ERC directives (SchDoc RECORD=22) with
the net they sit on. Synthetic sections and streams cover the mapping; Altium's public
AD25 examples are snapshot-checked when installed."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import main
from parsers.prj_pcb import VariantDefinition, VariantState, parse_prj_options, parse_prj_pcb
from parsers.sch_doc import hierarchy_from_stream, parse_sch_hierarchy
from services.project_options import (
    ERC_CHECKS,
    ERC_DEFAULTS,
    MATRIX_DEFAULT,
    MATRIX_LABELS,
    build_project_options,
    page_project_options,
)
from tests.test_design_hierarchy import _component, _port, _stream, _symbol, _wire

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")


def _matrix(changes: dict[tuple[int, int], str]) -> str:
    """[ERC Connection Matrix] rows: the default with symmetric cell changes."""
    rows = [list(row) for row in MATRIX_DEFAULT]
    for (i, j), letter in changes.items():
        rows[i][j] = rows[j][i] = letter
    return "".join(f"L{i}={''.join(row)}\n" for i, row in enumerate(rows, 1))


OUTPUT, POWER, PASSIVE = 2, 7, 4
INPUT, UNCONNECTED, OUT_ENTRY = 0, 16, 13

PRJ = (
    "[Design]\nHierarchyMode=2\nChannelRoomNamingStyle=2\nChannelRoomLevelSeperator=.\n"
    "ChannelDesignatorFormatString=$Component_$ChannelIndex\nOutputPath=Out\\Build\n"
    "LogFolderPath=\nOpenOutputs=1\nTimestampOutput=0\nArchiveProject=1\nSeparateFolders=0\n"
    "NetlistSinglePinNets=1\nAllowPortNetNames=0\nAllowSheetEntryNetNames=1\n"
    "[Document1]\nDocumentPath=a.SchDoc\nClassGenCCAutoEnabled=1\nClassGenCCAutoRoomEnabled=1\n"
    "ClassGenNCAutoScope=None\nGenerateClassCluster=0\n"
    "[Document2]\nDocumentPath=b.SchDoc\nClassGenCCAutoEnabled=0\nClassGenCCAutoRoomEnabled=0\n"
    "ClassGenNCAutoScope=All Nets\nGenerateClassCluster=1\n"
    "[Document3]\nDocumentPath=Board.PcbDoc\n"
    "[PrjClassGen]\nNetClassAutoBusEnabled=1\nNetClassAutoCompEnabled=0\n"
    "NetClassManualEnabled=1\nMysteryClassKey=1\n"
    "[Electrical Rules Check]\nType1=1\nType7=1\nType9=1\nType10=0\nType11=1\nType12=3\n"
    "Type59=0\nType64=0\nType200=2\nTypeX=1\nMultiChannelAlternate=2\nAlternateItemFail=1\n"
    "[ERC Connection Matrix]\n"
    + _matrix({(OUTPUT, OUTPUT): "W", (OUTPUT, POWER): "N", (INPUT, UNCONNECTED): "N",
               (PASSIVE, UNCONNECTED): "W", (OUT_ENTRY, UNCONNECTED): "N"})
    + "[Comparison Options]\n"
    "ComparisonOptions0=Kind=Net|MinPercent=75|MinMatch=3|ShowMatch=-1|Confirm=-1|UseName=0"
    "|InclAllRules=0\n"
    "[Difference Levels]\nType1=1\nType2=0\nType3=1\n"
    "[Annotate]\nPhysicalNamingFormat=$Component_$RoomName\n"
    "[DiffPairSuffix1]\nPositive=_DP\nNegative=_DN\n"
)


def _project(tmp_path, sheets: dict[str, bytes], prj_text: str = PRJ):
    """A project with the given option sections and synthetic sheets."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    prj = tmp_path / "Board.PrjPcb"
    prj.write_text(prj_text, encoding="utf-8")
    data = parse_prj_pcb(str(prj))
    streams = {name.lower(): stream for name, stream in sheets.items()}

    def read_sheet(path):
        name = Path(path).name.lower()
        if name not in streams:
            raise OSError("not a sheet of this test")
        return hierarchy_from_stream(streams[name])

    project = {"name": "Board", "prj_pcb_path": str(prj),
               "sheets": [{"name": Path(p).stem, "path": p} for p in data.sheet_paths]}
    return project, read_sheet


def _no_erc(x: int, y: int, extra: str = "|ISACTIVE=T|SUPPRESSALL=T|SYMBOL=Thin Cross") -> str:
    return f"|RECORD=22|LOCATION.X={x}|LOCATION.Y={y}|COLOR=255|OWNERPARTID=-1{extra}"


# R1 pin 1 ends at (300, 490) and is unconnected: No ERC A sits on it. R1 pin 2 (end
# 400, 490) is wired to R2 pin 1 (end 500, 490): No ERC B sits on that wire. R3 pin 1
# (end 300, 390) is wired to a VCC power port: No ERC C. Ports PA and PB sit on wires that
# cross at (200, 200) without a junction: No ERC D sits on the crossing. E is inactive on
# nothing; F is a legacy record (no flags) on nothing; G is specific on the R1-R2 wire.
SHEET = _stream(
    *_component(1, "R1", [("1", 320, 490, 2), ("2", 380, 490, 0)]),   # positions 2..5
    *_component(5, "R2", [("1", 520, 490, 2)]),                        # 6..8
    _wire((400, 490), (500, 490)),                                     # 9
    _no_erc(300, 490),                                                 # 10: A
    _no_erc(450, 490),                                                 # 11: B
    *_component(11, "R3", [("1", 320, 390, 2)]),                       # 12..14
    _wire((200, 390), (300, 390)),
    "|RECORD=17|LOCATION.X=200|LOCATION.Y=390|TEXT=VCC|STYLE=1",
    _no_erc(250, 390),                                                 # C
    _port("PA", 50, 200),
    _wire((100, 200), (300, 200)),
    _port("PB", 200, 300),
    _wire((200, 300), (200, 100)),
    _no_erc(200, 200),                                                 # D
    _no_erc(600, 600, "|ISACTIVE=F|SUPPRESSALL=T|SYMBOL=Thick Cross"),  # E
    _no_erc(700, 700, ""),                                             # F
    _no_erc(460, 490, "|ISACTIVE=T|SUPPRESSALL=F|SYMBOL=Thin Cross"
                      "|ERRORKINDSETTOSUPPRESS=9|CONNECTIONPAIRSTOSUPPRESS=2:2"),  # G
)


# ---------- the parsers ----------

def test_prj_options_sections_as_written(tmp_path):
    project, _ = _project(tmp_path, {})
    options = parse_prj_options(project["prj_pcb_path"])
    assert options.design["ChannelRoomNamingStyle"] == "2"
    assert options.erc_levels["Type10"] == "0" and options.erc_levels["AlternateItemFail"] == "1"
    assert len(options.connection_matrix) == 17
    assert options.class_generation["NetClassAutoBusEnabled"] == "1"
    assert options.sheet_class_generation == [
        ("a.SchDoc", {"ClassGenCCAutoEnabled": "1", "ClassGenCCAutoRoomEnabled": "1",
                      "ClassGenNCAutoScope": "None", "GenerateClassCluster": "0"}),
        ("b.SchDoc", {"ClassGenCCAutoEnabled": "0", "ClassGenCCAutoRoomEnabled": "0",
                      "ClassGenNCAutoScope": "All Nets", "GenerateClassCluster": "1"}),
    ]
    assert options.comparison == [{"Kind": "Net", "MinPercent": "75", "MinMatch": "3",
                                   "ShowMatch": "-1", "Confirm": "-1", "UseName": "0",
                                   "InclAllRules": "0"}]
    assert options.difference_levels == {"Type1": "1", "Type2": "0", "Type3": "1"}
    assert options.diff_pair_suffixes == [{"Positive": "_DP", "Negative": "_DN"}]


def test_no_erc_records_and_their_flags():
    sheet = hierarchy_from_stream(SHEET)
    a, b, c, d, e, f, g = sheet.no_ercs
    assert (a.x_mil, a.y_mil, a.active, a.suppress_all, a.symbol) == (
        3000.0, 4900.0, True, True, "Thin Cross")
    assert (e.active, e.symbol) == (False, "Thick Cross")
    # A record saved before the flags existed: active and generic (KiCad's defaults).
    assert (f.active, f.suppress_all, f.flags_written) == (True, True, False)
    assert (g.suppress_all, g.error_kinds, g.connection_pairs) == (False, "9", "2:2")


def test_no_erc_takes_the_wiring_it_sits_on():
    sheet = hierarchy_from_stream(SHEET)
    a, b, c, d, e, f, g = sheet.no_ercs
    assert a.wiring.pins == [("R1", "1")] and a.wiring.wires == 0
    assert sorted(b.wiring.pins) == [("R1", "2"), ("R2", "1")] and b.wiring.wires == 1
    assert c.wiring.power_ports == ["VCC"] and c.wiring.pins == [("R3", "1")]
    assert d.wiring.wires == 1 and len(d.wiring.ports) == 1
    assert not (e.wiring.pins or e.wiring.wires)


def test_no_erc_on_crossing_wires_joins_nothing():
    sheet = hierarchy_from_stream(SHEET)
    pa, pb = sheet.ports
    assert pa.group != pb.group
    assert sheet.groups[pa.group].ports == ["PA"] and sheet.groups[pb.group].ports == ["PB"]


def test_records_inside_a_component_are_not_sheet_directives():
    stream = _stream(*_component(1, "R1", [("1", 320, 490, 2)]),
                     "|RECORD=22|OWNERINDEX=1|LOCATION.X=300|LOCATION.Y=490")
    assert hierarchy_from_stream(stream).no_ercs == []


# ---------- the settings ----------

def test_settings_groups(tmp_path):
    project, read = _project(tmp_path, {"a.SchDoc": _stream(), "b.SchDoc": _stream()})
    model = build_project_options(project, read)
    assert model["net_identifier_scope"]["effective"] == "Hierarchical"
    assert "explanation" not in model["net_identifier_scope"]
    assert model["net_naming"]["allow_single_pin_nets"] is True
    assert model["net_naming"]["allow_ports_to_name_nets"] is False
    assert model["diff_pairs"]["default_suffixes"] == {"positive": "_P", "negative": "_N"}
    assert model["unmapped"]["diff_pair_suffix_sections"] == [
        {"Positive": "_DP", "Negative": "_DN"}]
    classes = model["class_generation"]
    assert classes["generate_net_classes_for_buses"] is True
    assert classes["generate_net_classes_for_components"] is False
    assert classes["user_defined_net_classes"] is True
    assert "MysteryClassKey" not in json.dumps(classes)
    assert classes["sheets"]["total"] == 2
    assert classes["sheets"]["most_sheets"] == {
        "component_class": True, "net_classes": "None", "room": True,
        "structure_class": False, "count": 1}
    assert classes["sheets"]["other_sheets"] == [{
        "sheet": "b", "component_class": False, "room": False, "net_classes": "All Nets",
        "structure_class": True}]
    assert model["multi_channel"] == {
        "room_naming_style": "Numeric Name Path", "level_separator": ".",
        "designator_format": "$Component_$ChannelIndex", "designator_format_predefined": True}
    assert model["outputs"]["output_path"] == "Out\\Build"
    assert "output_path_default" not in model["outputs"]
    assert model["outputs"]["eco_log_path"] == "Project Logs for Board"
    assert model["outputs"]["archive_project_document"] is True
    [net] = model["comparator"]["object_matching"]
    assert net == {"object_type": "Net", "min_match_percent": 75, "min_matched_members": 3,
                   "show_manual_matching_dialog": True, "use_name_matching": False}
    assert model["unmapped"]["comparison_options"] == {
        "Net": {"Confirm": "-1", "InclAllRules": "0"}}
    assert model["comparator"]["difference_types"] == {"total": 3, "not_1": 1}
    assert model["unmapped"]["difference_levels_not_1"] == {"Type2": "0"}
    assert model["unmapped"]["Annotate.PhysicalNamingFormat"] == "$Component_$RoomName"


def test_comparator_values_that_are_not_numbers_stay_raw(tmp_path):
    text = "[Comparison Options]\nComparisonOptions0=Kind=Net|MinPercent=abc|UseName=-1\n"
    project, read = _project(tmp_path, {}, text)
    model = build_project_options(project, read)
    assert model["comparator"]["object_matching"] == [
        {"object_type": "Net", "use_name_matching": True}]
    assert model["unmapped"]["comparison_options"] == {"Net": {"MinPercent": "abc"}}


def test_default_output_paths(tmp_path):
    project, read = _project(tmp_path, {}, "[Design]\nOutputPath=\n")
    outputs = build_project_options(project, read)["outputs"]
    assert outputs["output_path"] == "Project Outputs for Board"
    assert outputs["output_path_default"] is True


def test_erc_levels_against_defaults(tmp_path):
    project, read = _project(tmp_path, {})
    model = build_project_options(project, read)
    erc, audit = model["erc"], model["audit"]["erc_checks_off_or_downgraded"]
    lowered = {row["check"]: row for row in audit}
    # Switched off / downgraded from the default, key checks first.
    assert [row["check"] for row in audit[:2]] == [
        "Nets with no driving source", "Nets containing floating input pins"]
    assert lowered["Nets with no driving source"]["level"] == "No Report"
    assert lowered["Nets with no driving source"]["default"] == "Warning"
    assert "nothing drives" in lowered["Nets with no driving source"]["hides"]
    assert lowered["Nets containing floating input pins"]["level"] == "Warning"
    # A type Altium writes by name.
    assert lowered["Fail to add alternate item"] == {
        "check": "Fail to add alternate item", "level": "Warning", "default": "Fatal Error"}
    # Defaults the sources disagree on are not audited.
    assert "Nets with only one pin" not in lowered and "Duplicate sheet numbers" not in lowered
    assert {"check": "Nets with only one pin", "level": "No Report", "default": None} in (
        erc["key_checks"])
    assert erc["raised_above_default"] == [{
        "check": "Nets with possible connection problems", "level": "Fatal Error",
        "default": "Error"}]
    assert erc["lowered_from_default"] == len(audit) == 3
    # NoError is not a check; unknown and out-of-range keys stay raw.
    assert erc["checks"] == 9
    assert model["unmapped"]["electrical_rules_check"] == {"Type200": "2", "TypeX": "1"}
    # Every check is accounted for: at its default, lowered, raised, or listed by level
    # when its default is unknown (key checks show theirs in key_checks).
    assert erc["at_default"] == 2
    assert erc["default_unknown_by_level"] == {
        "Warning": ["Duplicate sheet numbers"],
        "Error": ["Unsupported multi-channel alternate item"]}


def test_erc_keys_outside_the_slots_stay_raw(tmp_path):
    text = "[Electrical Rules Check]\nType0=2\nType2=4\nType166=2\nType167=1\nType168=1\n"
    project, read = _project(tmp_path, {}, text)
    model = build_project_options(project, read)
    # Type0 is no slot (TypeN is ordinal N-1) and 4 is no report level.
    assert model["unmapped"]["electrical_rules_check"] == {
        "Type0": "2", "Type2": "4", "Type168": "1"}
    assert model["erc"]["default_unknown_by_level"] == {
        "Warning": ["Generic Component Not Replaced With Library Component"],
        "Error": ["Unused pins in dynamic symbol"]}


def test_many_raised_checks_are_cut_with_a_total(tmp_path):
    raised = [n for n, level in ERC_DEFAULTS.items() if level < 3 and n != 64]
    text = "[Electrical Rules Check]\n" + "".join(f"Type{n}=3\n" for n in raised)
    project, read = _project(tmp_path, {}, text)
    erc = build_project_options(project, read)["erc"]
    assert len(raised) > 80 and len(erc["raised_above_default"]) == 80
    assert erc["raised_above_default_total"] == len(raised)


def test_connection_matrix_cells(tmp_path):
    project, read = _project(tmp_path, {})
    model = build_project_options(project, read)
    matrix = model["connection_matrix"]
    assert matrix["order"][2] == "Output Pin" and matrix["symmetric"] is True
    assert "Output Pin x Output Pin: Warning (default Error)" in matrix["changed_from_default"]
    assert "Passive Pin x Unconnected: Warning (default No Report)" in (
        matrix["changed_from_default"])
    assert "Output Sheet Entry x Unconnected: No Report (default unknown)" in (
        matrix["changed_from_default"])
    relaxed = {cell["pair"]: cell for cell in model["audit"]["connection_matrix_relaxed"]}
    assert set(relaxed) == {"Input Pin x Unconnected", "Output Pin x Output Pin",
                            "Output Pin x Power Pin"}
    assert relaxed["Output Pin x Power Pin"]["level"] == "No Report"
    assert "driver conflict" in relaxed["Output Pin x Output Pin"]["risk"]
    assert "unconnected input" in relaxed["Input Pin x Unconnected"]["risk"]


def test_connection_matrix_odd_shapes(tmp_path):
    def build(text: str) -> dict:
        project, read = _project(tmp_path / str(len(list(tmp_path.iterdir()))), {},
                                 "[ERC Connection Matrix]\n" + text)
        return build_project_options(project, read)

    # Only the lower cell changed: the matrix is read in full, not as a triangle.
    rows = [list(row) for row in MATRIX_DEFAULT]
    rows[UNCONNECTED][INPUT] = "N"
    model = build("".join(f"L{i}={''.join(row)}\n" for i, row in enumerate(rows, 1)))
    assert model["connection_matrix"]["symmetric"] is False
    assert [cell["pair"] for cell in model["audit"]["connection_matrix_relaxed"]] == [
        "Unconnected x Input Pin"]
    # An unconnected input port relaxed is listed, not audited; a letter that is not N/W/E
    # stays raw.
    model = build(_matrix({(8, UNCONNECTED): "N", (OUTPUT, PASSIVE): "F"}))
    assert "Input Port x Unconnected: No Report (default Warning)" in (
        model["connection_matrix"]["changed_from_default"])
    assert "Output Pin x Passive Pin: F (unmapped) (default No Report)" in (
        model["connection_matrix"]["changed_from_default"])
    assert model["audit"]["connection_matrix_relaxed"] == []
    assert model["unmapped"]["connection_matrix_letters"] == ["F"]
    # A row of the wrong length: the section is passed on raw.
    model = build(_matrix({}).replace("L5=NNNNNNNNNNNNNNNNN", "L5=NNN"))
    assert "not 17 x 17" in model["connection_matrix"]["note"]
    assert model["unmapped"]["erc_connection_matrix"]["L5"] == "NNN"


def test_missing_sections_mean_altium_defaults(tmp_path):
    project, read = _project(tmp_path, {}, "[Design]\n")
    model = build_project_options(project, read)
    assert "Altium uses its defaults" in model["erc"]["note"]
    assert "Altium uses its defaults" in model["connection_matrix"]["note"]
    assert model["audit"]["erc_checks_off_or_downgraded"] == []
    assert model["audit"]["connection_matrix_relaxed"] == []


def test_tables_line_up():
    assert len(MATRIX_LABELS) == 17 and all(len(row) == 17 for row in MATRIX_DEFAULT)
    assert ERC_CHECKS[9] == "Nets with no driving source"  # Type10
    assert ERC_CHECKS[119] == "Unsupported multi-channel alternate item"
    assert ERC_CHECKS[120] == "Fail to add alternate item"
    assert len(ERC_CHECKS) == 167 and ERC_CHECKS[165] == "Unused pins in dynamic symbol"
    assert set(ERC_DEFAULTS) <= set(range(1, len(ERC_CHECKS) + 1))
    assert len(set(ERC_CHECKS)) == len(ERC_CHECKS)


def test_the_repo_fixture_project_reads_with_defaults(tmp_path):
    project = {"name": "test", "prj_pcb_path": str(FIXTURES / "test.prjpcb"), "sheets": []}
    model = build_project_options(project, parse_sch_hierarchy)
    assert model["erc"]["checks"] == 160
    assert {"check": "Nets with no driving source", "level": "Warning",
            "default": "Warning"} in model["erc"]["key_checks"]
    assert model["connection_matrix"]["changed_from_default"] == []
    assert model["multi_channel"]["designator_format"] == "$Component$ChannelAlpha"


# ---------- No ERC directives ----------

def _directives(model) -> dict[tuple[float, float], dict]:
    return {(d["x_mil"], d["y_mil"]): d for d in model["no_erc_directives"]}


def test_no_erc_directives_offline(tmp_path):
    project, read = _project(tmp_path, {"a.SchDoc": SHEET})
    model = build_project_options(project, read)
    found = _directives(model)
    a, b, c = found[(3000.0, 4900.0)], found[(4500.0, 4900.0)], found[(2500.0, 3900.0)]
    assert (a["net_kind"], a["pins_on_sheet"], a["on_pins"]) == ("single_pin", 1, ["R1.1"])
    assert "flagged" not in a
    assert (b["net_kind"], b["pins_on_sheet"]) == ("signal", 2)
    assert "2 pins" in b["flagged"] and "every violation" in b["flagged"]
    assert "2 pins on this sheet" in b["flagged"] and "reported at its point" in b["flagged"]
    assert (c["net"], c["net_kind"]) == ("VCC", "power") and "power net" in c["flagged"]
    # Under Hierarchical scope a port joins only the entry of a symbol placing its sheet:
    # no sheet places this one, so D's port leads nowhere.
    d = found[(2000.0, 2000.0)]
    assert d["on_ports"] in (["PA"], ["PB"]) and d["net_kind"] == "wire_only"
    assert "flagged" not in d
    e = found[(6000.0, 6000.0)]
    assert (e["active"], e["net_kind"], e["symbol"]) == (False, "nothing", "Thick Cross")
    assert "flagged" not in e
    assert found[(7000.0, 7000.0)]["legacy_record"] is True
    g = found[(4600.0, 4900.0)]
    assert g["mode"] == "specific"
    assert g["suppresses"] == {"violation_types": "9", "connection_pairs": "2:2"}
    assert "selected violation types" in g["flagged"]
    summary = model["audit"]["no_erc"]
    assert (summary["total"], summary["active"], summary["inactive"]) == (7, 6, 1)
    assert (summary["generic"], summary["specific"], summary["flagged"]) == (6, 1, 3)
    assert summary["by_net_kind"]["signal"] == 2
    assert "not the whole net" in summary["note"]
    # Flagged first.
    assert all(d.get("flagged") for d in model["no_erc_directives"][:3])
    assert "nets_note" in model


def _parent(entries: tuple[str, ...] = ("PA", "PB")) -> bytes:
    """b.SchDoc placing a.SchDoc through a symbol with the given entries."""
    return _stream(*_symbol(1, "U_A", "a.SchDoc",
                            [(name, 0, 1 + i, "") for i, name in enumerate(entries)]))


def test_port_leaves_the_sheet_through_a_parent_entry(tmp_path):
    project, read = _project(tmp_path, {"a.SchDoc": SHEET, "b.SchDoc": _parent()})
    d = _directives(build_project_options(project, read))[(2000.0, 2000.0)]
    assert d["net_kind"] == "signal" and "leaves the sheet" in d["flagged"]
    # An entry for another port does not take this one off the sheet.
    project, read = _project(tmp_path / "other", {"a.SchDoc": SHEET,
                                                  "b.SchDoc": _parent(("PX",))})
    d = _directives(build_project_options(project, read))[(2000.0, 2000.0)]
    assert d["net_kind"] == "wire_only" and "flagged" not in d


def test_ports_join_by_name_under_flat_and_labels_under_global(tmp_path):
    shared = _stream(_port("PA", 50, 200), _port("PB", 50, 400))
    flat = PRJ.replace("HierarchyMode=2", "HierarchyMode=1")
    project, read = _project(tmp_path, {"a.SchDoc": SHEET, "b.SchDoc": shared}, flat)
    d = _directives(build_project_options(project, read))[(2000.0, 2000.0)]
    assert "leaves the sheet" in d["flagged"]
    project, read = _project(tmp_path / "alone", {"a.SchDoc": SHEET,
                                                  "b.SchDoc": _stream()}, flat)
    d = _directives(build_project_options(project, read))[(2000.0, 2000.0)]
    assert "flagged" not in d
    # A net label: on its own sheet under Flat, joined by name under Global.
    labelled = _stream(*_component(1, "R1", [("1", 320, 490, 2)]),
                       "|RECORD=25|LOCATION.X=300|LOCATION.Y=490|TEXT=NETX",
                       _wire((300, 490), (300, 600)), _no_erc(300, 600))
    other = _stream("|RECORD=25|LOCATION.X=100|LOCATION.Y=100|TEXT=NETX")
    for mode, flagged in (("1", False), ("3", True)):
        text = PRJ.replace("HierarchyMode=2", f"HierarchyMode={mode}")
        project, read = _project(tmp_path / mode, {"a.SchDoc": labelled, "b.SchDoc": other},
                                 text)
        [row] = build_project_options(project, read)["no_erc_directives"]
        assert bool(row.get("flagged")) is flagged, mode


def test_power_by_name_only_without_a_signal_token(tmp_path):
    # Unused pins whose net label names a rail-derived signal (VCC_EN) or a rail (+3V3).
    sheet = _stream(*_component(1, "R1", [("1", 320, 490, 2)]),
                    "|RECORD=25|LOCATION.X=300|LOCATION.Y=490|TEXT=VCC_EN",
                    _no_erc(300, 490),
                    *_component(6, "R2", [("1", 320, 390, 2)]),
                    "|RECORD=25|LOCATION.X=300|LOCATION.Y=390|TEXT=+3V3",
                    _no_erc(300, 390))
    project, read = _project(tmp_path, {"a.SchDoc": sheet})
    found = _directives(build_project_options(project, read))
    enable, rail = found[(3000.0, 4900.0)], found[(3000.0, 3900.0)]
    assert (enable["net"], enable["net_kind"]) == ("VCC_EN", "single_pin")
    assert "flagged" not in enable
    assert (rail["net"], rail["net_kind"]) == ("+3V3", "power") and "power net" in rail["flagged"]


def test_an_inactive_no_erc_is_never_flagged(tmp_path):
    sheet = _stream(*_component(1, "R1", [("2", 380, 490, 0)]),
                    *_component(4, "R2", [("1", 520, 490, 2)]),
                    _wire((400, 490), (500, 490)),
                    _no_erc(450, 490, "|ISACTIVE=F|SUPPRESSALL=T|SYMBOL=Thin Cross"))
    project, read = _project(tmp_path, {"a.SchDoc": sheet})
    [row] = build_project_options(project, read)["no_erc_directives"]
    assert (row["active"], row["net_kind"], row["pins_on_sheet"]) == (False, "signal", 2)
    assert "flagged" not in row


def test_no_erc_nets_from_the_netlist(tmp_path):
    project, read = _project(tmp_path, {"a.SchDoc": SHEET, "b.SchDoc": _parent()})
    pins = {f"P{i}": {"net": "SIG"} for i in range(3)}
    netlist = {"components": {
        "R1": {"sheet": "a", "pins": {"1": {"net": "NC_R1"}, "2": {"net": "LOAD"}}},
        "R2": {"sheet": "a", "pins": {"1": {"net": "LOAD"}}},
        "R3": {"sheet": "a", "pins": {"1": {"net": "VCC"}}},
        "U9": {"sheet": "b", "pins": pins | {f"V{i}": {"net": "VCC"} for i in range(30)}},
    }}
    model = build_project_options(project, read, netlist)
    found = _directives(model)
    a, b = found[(3000.0, 4900.0)], found[(4500.0, 4900.0)]
    assert (a["net"], a["pins"], a["net_kind"]) == ("NC_R1", 1, "single_pin")
    assert (b["net"], b["pins"], b["net_kind"]) == ("LOAD", 2, "signal")
    c = found[(2500.0, 3900.0)]
    assert (c["net"], c["pins"], c["net_kind"]) == ("VCC", 31, "power")
    # Wiring with no pin reaching the netlist: named from its port, not flagged as
    # leaving the sheet once the netlist decides.
    d = found[(2000.0, 2000.0)]
    assert d["net_kind"] == "wire_only" and "flagged" not in d
    assert "nets_note" not in model


def test_high_fanout_signal_net(tmp_path):
    project, read = _project(tmp_path, {"a.SchDoc": SHEET})
    pins = {f"P{i}": {"net": "BUS_CLK"} for i in range(40)}
    netlist = {"components": {"R1": {"sheet": "a", "pins": {"2": {"net": "BUS_CLK"}}},
                              "U9": {"sheet": "b", "pins": pins}}}
    b = _directives(build_project_options(project, read, netlist))[(4500.0, 4900.0)]
    assert (b["net"], b["pins"], b["net_kind"]) == ("BUS_CLK", 41, "high_fanout")
    assert "high-fanout" in b["flagged"]


def test_unreadable_sheet_and_paging(tmp_path):
    project, read = _project(tmp_path, {"a.SchDoc": SHEET})
    project["sheets"].append({"name": "gone", "path": str(tmp_path / "gone.SchDoc")})
    model = build_project_options(project, read)
    assert {s["sheet"] for s in model["sheets_not_read"]} == {"b", "gone"}
    first = page_project_options(model, 0, 3)
    assert (first["no_erc_total"], first["has_more"], first["next_offset"]) == (7, True, 3)
    assert "audit" in first and len(first["no_erc_directives"]) == 3
    second = page_project_options(model, 6, 3)
    assert "audit" not in second and second["has_more"] is False
    assert len(second["no_erc_directives"]) == 1


def test_unreadable_project_file(tmp_path):
    project = {"name": "x", "prj_pcb_path": str(tmp_path / "missing.PrjPcb"), "sheets": []}
    assert build_project_options(project, parse_sch_hierarchy)["error"] == (
        "project_file_unreadable")


# ---------- the tool ----------

def test_tool_needs_a_project():
    with patch("main._project", None):
        assert json.loads(main.get_project_options())["error"] == "no_project"


def test_tool_renders_pages(tmp_path):
    project, read = _project(tmp_path, {"a.SchDoc": SHEET})
    with patch("main._project", project), \
         patch("main._altium") as altium, \
         patch("main._variant_state", VariantState([VariantDefinition(name="Default")])), \
         patch.object(main._design_notes, "hierarchy", read):
        altium._netlist = None
        result = json.loads(main.get_project_options())
        assert result["prj_pcb"] == "Board.PrjPcb" and result["no_erc_total"] == 7
        assert json.loads(main.get_project_options(offset=5))["offset"] == 5


# ---------- Altium's public examples ----------

def _example(relative: str):
    path = EXAMPLES / relative
    if not path.exists():
        pytest.skip("Altium AD25 examples not installed")
    data = parse_prj_pcb(str(path))
    project = {"name": path.stem, "prj_pcb_path": str(path),
               "sheets": [{"name": Path(p).stem, "path": p} for p in data.sheet_paths
                          if Path(p).exists()]}
    return build_project_options(project, parse_sch_hierarchy)


def test_example_bluetooth_sentinel_no_ercs():
    model = _example(r"Bluetooth Sentinel\Bluetooth_Sentinel.PrjPcb")
    summary = model["audit"]["no_erc"]
    assert (summary["total"], summary["active"], summary["generic"]) == (12, 12, 12)
    assert summary["by_net_kind"] == {"single_pin": 11, "signal": 1}
    assert model["audit"]["erc_checks_off_or_downgraded"] == []
    assert model["connection_matrix"]["changed_from_default"] == []


def test_example_mini_pc_relaxed_erc():
    model = _example(r"Mini PC\Mini PC - Motherboard\MiniPC.PrjPcb")
    lowered = {row["check"] for row in model["audit"]["erc_checks_off_or_downgraded"]}
    assert {"Nets with no driving source", "Nets containing floating input pins"} <= lowered
    relaxed = {cell["pair"] for cell in model["audit"]["connection_matrix_relaxed"]}
    assert {"Output Pin x Power Pin", "Input Pin x Unconnected"} <= relaxed
    assert model["net_identifier_scope"]["effective"] == "Hierarchical"


def test_example_spirit_level_legacy_no_ercs():
    model = _example(r"SpiritLevel-SL1\SL1 Xilinx Spartan-IIE PQ208 Rev1.02.PrjPcb")
    directives = model["no_erc_directives"]
    assert len(directives) == 78
    assert all(d["legacy_record"] and d["active"] and d["mode"] == "generic"
               for d in directives)
