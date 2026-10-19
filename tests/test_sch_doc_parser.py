import logging
import re
import struct
from pathlib import Path
from unittest.mock import patch
import pytest
from server.parsers.sch_doc import DesignatorClash, components_from_stream, parse_sch_doc

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def sheet1():
    return parse_sch_doc(str(FIXTURES / "sheet1.SchDoc"))


def test_known_component_present(sheet1):
    assert "U1" in sheet1


def test_component_mpn_plain_form(sheet1):
    # U1 uses plain 'Manufacturer Part Number'
    assert sheet1["U1"].mpn == "STM32F407VGT6"


def test_component_mpn_suffix_fallback(sheet1):
    # R11 has 'Manufacturer Part Number 1' — no plain form
    assert "R11" in sheet1
    assert sheet1["R11"].mpn == "ERJ-3EKF1000V"


def test_component_description(sheet1):
    assert sheet1["U1"].description is not None
    assert "STM32" in sheet1["U1"].description


def test_named_pins_extracted(sheet1):
    # J1 is a debug connector with named pins
    assert sheet1["J1"].pins.get("1") == "VCC"
    assert sheet1["J1"].pins.get("3") == "GND"


def test_unnamed_pins_skipped(sheet1):
    # Passive components (R, C) have generic pin names — not stored
    # R11 pins should be empty or absent (name was '?')
    pins = sheet1["R11"].pins
    assert "?" not in pins.values()


def test_all_expected_components_present(sheet1):
    expected = {"U1", "U2", "R11", "C1", "J1", "X1", "RST1"}
    assert expected.issubset(set(sheet1.keys()))


def test_no_power_symbols(sheet1):
    # Power ports (RECORD=17) must not appear — they have no refdes
    for refdes in sheet1:
        assert refdes  # empty refdes would indicate a leaked power port
        assert refdes not in ("VCC", "GND", "3V3", "5V")


def test_rst1_mpn(sheet1):
    assert sheet1["RST1"].mpn == "B3U-1000P"


def test_sheet2_smoke():
    result = parse_sch_doc(str(FIXTURES / "sheet2.SchDoc"))
    assert len(result) > 0
    assert "U3" in result
    assert result["U3"].mpn == "LTC6811HG-1#3ZZTRPBF"


def test_multipart_dedup():
    # sheet1.SchDoc has STM32F407 split across multiple parts — must appear once
    result = parse_sch_doc(str(FIXTURES / "sheet1.SchDoc"))
    stm32_entries = [r for r in result if result[r].description and "STM32" in result[r].description]
    refdes_set = set(stm32_entries)
    # One physical chip = one refdes
    assert len(refdes_set) == 1


# ---------- synthetic sheets: upper-case and mixed-case keys ----------
# Altium writes the same keys as OwnerIndex/Text/Name in some sheets and as
# OWNERINDEX/TEXT/NAME in others (most of its AD25 examples). Both must parse alike.

def _record(body: str) -> bytes:
    data = body.encode("latin-1") + b"\0"  # every record ends with a NUL
    return struct.pack("<I", len(data)) + data


def _stream(*records: str) -> bytes:
    """FileHeader bytes: a header record (position 0), then the given records."""
    out = _record("|HEADER=Protel for Windows - Schematic Capture Binary File Version 5.0")
    for rec in records:
        out += _record(rec)
    return out


def _upper_keys(record: str) -> str:
    return re.sub(r"\|([^|=]+)=", lambda m: f"|{m.group(1).upper()}=", record)


# The comment is each record's position; a child's OwnerIndex is its owner's position - 1.
_SHEET = [
    "|RECORD=31|FontIdCount=1",                                                                 # 1
    "|RECORD=1|LibReference=USB_C|ComponentDescription=USB-C receptacle|UniqueID=AAAAAAAA",     # 2
    "|RECORD=2|OwnerIndex=1|Designator=A4|Name=VBUS",                                           # 3
    "|RECORD=2|OwnerIndex=1|Designator=A1|Name=?",                                              # 4
    "|RECORD=34|OwnerIndex=1|Text=P1|Name=Designator",                                          # 5
    "|RECORD=41|OwnerIndex=1|Name=Manufacturer Part Number|Text=USB4105-GF-A",                  # 6
    "|RECORD=1|LibReference=RES|ComponentDescription=Resistor|UniqueID=BBBBBBBB",               # 7
    "|RECORD=41|OwnerIndex=6|Name=Comment|Text=RC0603FR-0710KL",                                # 8
    "|RECORD=41|OwnerIndex=6|Name=Value|Text=10k",                                              # 9
    "|RECORD=34|OwnerIndex=6|Name=Designator|Text=R1",                                          # 10
    "|RECORD=1|LibReference=CAP|ComponentDescription=Capacitor|UniqueID=CCCCCCCC",              # 11
    "|RECORD=34|OwnerIndex=10|Name=Designator|Text=C1",                                         # 12
    # Text holds the latin-1 spelling, %UTF8%Text the UTF-8 bytes of "4.7µF"
    "|RECORD=41|OwnerIndex=10|Name=Value|Text=4.7\xb5F|%UTF8%Text=4.7\xc2\xb5F",                # 13
    "|RECORD=1|LibReference=OPA|ComponentDescription=Dual op-amp|UniqueID=DDDDDDDD|CurrentPartId=1",  # 14
    "|RECORD=2|OwnerIndex=13|Designator=1|Name=OUTA",                                           # 15
    "|RECORD=34|OwnerIndex=13|Text=U1|Name=Designator",                                         # 16
    "|RECORD=1|LibReference=OPA|ComponentDescription=Dual op-amp|UniqueID=DDDDDDDD|CurrentPartId=2",  # 17
    "|RECORD=2|OwnerIndex=16|Designator=7|Name=OUTB",                                           # 18
    "|RECORD=34|OwnerIndex=16|Text=U1|Name=Designator",                                         # 19
    "|RECORD=17|Text=GND|Style=4",                                                              # 20 power port
]


@pytest.fixture(params=["mixed", "upper"])
def synthetic(request):
    records = _SHEET if request.param == "mixed" else [_upper_keys(r) for r in _SHEET]
    return components_from_stream(_stream(*records))


def test_upper_keys_helper_matches_altium_spelling():
    assert _upper_keys(_SHEET[4]) == "|RECORD=34|OWNERINDEX=1|TEXT=P1|NAME=Designator"


def test_synthetic_components_found(synthetic):
    # Power port skipped; the two-part op-amp appears once
    assert set(synthetic) == {"P1", "R1", "C1", "U1"}


def test_synthetic_component_fields(synthetic):
    p1 = synthetic["P1"]
    assert p1.refdes == "P1"
    assert p1.description == "USB-C receptacle"
    assert p1.unique_id == "AAAAAAAA"
    assert p1.mpn == "USB4105-GF-A"


def test_synthetic_last_field_has_no_trailing_nul(synthetic):
    # Text is the last field of R1's designator record, just before the record's NUL
    assert synthetic["R1"].refdes == "R1"


def test_synthetic_pins(synthetic):
    assert synthetic["P1"].pins == {"A4": "VBUS"}  # '?' pin name skipped
    # Pins of both parts of a multi-part component (same UniqueID) are merged
    assert synthetic["U1"].pins == {"1": "OUTA", "7": "OUTB"}
    assert synthetic["U1"].description == "Dual op-amp"


def test_synthetic_parameters(synthetic):
    r1 = synthetic["R1"]
    assert r1.value == "10k"
    assert r1.mpn == "RC0603FR-0710KL"  # Comment used when there is no Manufacturer Part Number
    assert synthetic["C1"].value == "4.7µF"  # the %UTF8% value wins


def test_parameter_names_are_case_insensitive():
    result = components_from_stream(_stream(
        "|RECORD=1|UNIQUEID=AAAAAAAA",
        "|RECORD=34|OWNERINDEX=0|TEXT=R7|NAME=DESIGNATOR",
        "|RECORD=41|OWNERINDEX=0|NAME=COMMENT|TEXT=10k 1%",
        "|RECORD=41|OWNERINDEX=0|NAME=MANUFACTURER PART NUMBER 1|TEXT=ERJ-3EKF1002V",
        "|RECORD=41|OWNERINDEX=0|NAME=VALUE|TEXT=10k",
    ))
    assert result["R7"].mpn == "ERJ-3EKF1002V"
    assert result["R7"].value == "10k"


def test_non_numeric_owner_index_is_ignored():
    result = components_from_stream(_stream(
        "|RECORD=1|UNIQUEID=AAAAAAAA",
        "|RECORD=34|OWNERINDEX=0|TEXT=R7|NAME=Designator",
        "|RECORD=41|OWNERINDEX=x|NAME=Value|TEXT=10k",
    ))
    assert result["R7"].value is None


# ---------- parts of a multi-part component ----------
# Newer sheets (most of Altium's AD25 examples) give each part of a multi-part component
# its own UniqueID; only the designator ties the parts together.

def _part(refdes: str, uid: str, part_id: str, lib: str = "LM339", part_count: str = "5",
          pins: dict[str, str] | None = None, params: dict[str, str] | None = None,
          description: str = "") -> list[str]:
    """Records of one placed part; _parts_stream fills in the children's OwnerIndex."""
    records = [f"|RECORD=1|LibReference={lib}|ComponentDescription={description}"
               f"|UniqueID={uid}|CurrentPartId={part_id}|PartCount={part_count}"]
    records += [f"|RECORD=2|OwnerIndex={{owner}}|Designator={number}|Name={name}"
                for number, name in (pins or {}).items()]
    records.append(f"|RECORD=34|OwnerIndex={{owner}}|Name=Designator|Text={refdes}")
    records += [f"|RECORD=41|OwnerIndex={{owner}}|Name={name}|Text={text}"
                for name, text in (params or {}).items()]
    return records


def _parts_stream(*parts: list[str], upper: bool = False) -> bytes:
    records: list[str] = []
    for part in parts:
        owner = len(records)  # the part's RECORD=1 lands at position owner + 1
        records += [record.format(owner=owner) for record in part]
    return _stream(*(_upper_keys(r) if upper else r for r in records))


@pytest.mark.parametrize("upper", [False, True], ids=["mixed", "upper"])
def test_parts_with_own_unique_ids_merge(upper):
    result = components_from_stream(_parts_stream(
        _part("U5", "AAAA0001", "1", pins={"1": "OUT1", "3": "VCC"}),
        _part("U5", "AAAA0002", "2", pins={"2": "OUT2", "3": "VCC"},
              params={"Value": "LM339"}, description="Quad comparator"),
        _part("U5", "AAAA0003", "3", pins={"14": "OUT3"},
              params={"Manufacturer Part Number": "LM339DR"}),
        _part("R1", "BBBB0001", "1", lib="RES", part_count="2"),
        upper=upper,
    ))
    assert set(result) == {"U5", "R1"}
    u5 = result["U5"]
    assert u5.pins == {"1": "OUT1", "3": "VCC", "2": "OUT2", "14": "OUT3"}
    assert u5.unique_id == "AAAA0003"  # the highest part's, as on the PCB
    assert u5.description == "Quad comparator"  # first non-empty, from part 2
    assert u5.value == "LM339"
    assert u5.mpn == "LM339DR"


def test_unique_id_is_the_highest_parts_wherever_it_is_placed():
    # The PcbDoc's SOURCEUNIQUEID names the highest part, not the first or last in the file
    result = components_from_stream(_parts_stream(
        _part("U5", "AAAA0004", "4"),
        _part("U5", "AAAA0001", "1"),
        _part("U5", "AAAA0002", "2"),
    ))
    assert result["U5"].unique_id == "AAAA0004"


def test_merged_mpn_prefers_manufacturer_part_number():
    result = components_from_stream(_parts_stream(
        _part("U5", "AAAA0001", "1", params={"Comment": "LM339"}),
        _part("U5", "AAAA0002", "2", params={"Manufacturer Part Number 1": "LM339DR"}),
        _part("U5", "AAAA0003", "3", params={"Manufacturer Part Number 2": "LM339ADR"}),
    ))
    assert result["U5"].mpn == "LM339DR"


def test_parts_from_different_libraries_merge_and_are_logged(caplog):
    with caplog.at_level(logging.WARNING):
        result = components_from_stream(_parts_stream(
            _part("U5", "AAAA0001", "1", lib="LM339", pins={"1": "OUT1"}),
            _part("U5", "AAAA0002", "2", lib="CMP-0001", pins={"2": "OUT2"}),
        ), source="Comparators.SchDoc")
    assert result["U5"].pins == {"1": "OUT1", "2": "OUT2"}
    assert "Comparators.SchDoc" in caplog.text
    assert "LM339" in caplog.text and "CMP-0001" in caplog.text


@pytest.mark.parametrize("second", [
    {"part_id": "1"},                                     # part A placed twice
    {"part_id": "2", "lib": "OPA2", "part_count": "3"},  # another component's part B
], ids=["same-part-id", "other-part-count"])
def test_different_components_with_one_designator_are_not_merged(caplog, second):
    clashes: list[DesignatorClash] = []
    with caplog.at_level(logging.WARNING):
        result = components_from_stream(_parts_stream(
            _part("U5", "AAAA0001", "1", pins={"1": "OUT1"}, params={"Value": "LM339"}),
            _part("U5", "BBBB0001", pins={"1": "IN", "2": "OUT"},
                  params={"Value": "OPA2"}, **second),
        ), clashes=clashes)
    u5 = result["U5"]  # the later component, as before
    assert (u5.unique_id, u5.pins, u5.value) == ("BBBB0001", {"1": "IN", "2": "OUT"}, "OPA2")
    assert "two different components" in caplog.text
    assert clashes == [DesignatorClash("U5", ["LM339", second.get("lib", "LM339")])]


def test_designator_of_three_components_is_one_clash():
    clashes: list[DesignatorClash] = []
    result = components_from_stream(_parts_stream(
        _part("R1", "AAAA0001", "1", lib="RES", part_count="2"),
        _part("U5", "BBBB0001", "1", lib="LDO", part_count="2"),
        _part("U5", "CCCC0001", "1", lib="LM339"),
        _part("U5", "DDDD0001", "1", lib="OPA2", part_count="3"),
        _part("C1", "EEEE0001", "1", lib="CAP", part_count="2"),
        _part("C1", "FFFF0001", "1", lib="CAP", part_count="2"),
    ), clashes=clashes)
    assert set(result) == {"R1", "U5", "C1"}
    assert clashes == [DesignatorClash("U5", ["LDO", "LM339", "OPA2"]),
                       DesignatorClash("C1", ["CAP", "CAP"])]


def test_parts_after_a_designator_clash_merge_into_the_later_component():
    clashes: list[DesignatorClash] = []
    result = components_from_stream(_parts_stream(
        _part("U5", "AAAA0001", "1", lib="LDO", part_count="2", pins={"1": "VIN"}),
        _part("U5", "BBBB0001", "1", pins={"1": "OUT1"}),
        _part("U5", "BBBB0002", "2", pins={"7": "OUT2"}),
    ), clashes=clashes)
    assert result["U5"].pins == {"1": "OUT1", "7": "OUT2"}
    assert result["U5"].unique_id == "BBBB0002"
    assert clashes == [DesignatorClash("U5", ["LDO", "LM339"])]


def test_merged_parts_are_no_clash():
    clashes: list[DesignatorClash] = []
    components_from_stream(_parts_stream(
        _part("U5", "AAAA0001", "1", lib="LM339"),
        _part("U5", "AAAA0002", "2", lib="CMP-0001"),
    ), clashes=clashes)
    assert clashes == []


def test_unannotated_parts_are_not_merged_or_logged(caplog):
    clashes: list[DesignatorClash] = []
    with caplog.at_level(logging.WARNING):
        result = components_from_stream(_parts_stream(
            _part("U?", "AAAA0001", "1", pins={"1": "OUT1"}),
            _part("U?", "AAAA0002", "2", pins={"7": "OUT2"}),
            _part("U?", "BBBB0001", "1", lib="RES", part_count="2", pins={"1": "A"}),
        ), clashes=clashes)
    assert result["U?"].pins == {"1": "A"}
    assert not caplog.records
    assert clashes == []


def test_parse_sch_doc_reports_clashes():
    stream = _parts_stream(
        _part("U5", "AAAA0001", "1", lib="LDO", part_count="2"),
        _part("U5", "BBBB0001", "1"),
    )
    clashes: list[DesignatorClash] = []
    with patch("server.parsers.sch_doc.olefile.OleFileIO") as ole:
        ole.return_value.__enter__.return_value.openstream.return_value.read.return_value = stream
        result = parse_sch_doc("Power.SchDoc", clashes)
    assert result["U5"].unique_id == "BBBB0001"
    assert clashes == [DesignatorClash("U5", ["LDO", "LM339"])]


# ---------- footprints, kind, unique IDs and pin designators (sync check) ----------

def test_current_footprint_from_the_fixture(sheet1):
    assert sheet1["R11"].footprint == "R0603_0.55MM"
    assert sheet1["R11"].unique_ids == [sheet1["R11"].unique_id]


def test_footprint_models_kind_and_pin_designators():
    # A child's OwnerIndex is its owner's position - 1; RECORD=45 models own to RECORD=44.
    data = _stream(
        "|RECORD=1|LibReference=NT|UniqueID=AAAAAAAA|ComponentKind=4",                  # 1
        "|RECORD=34|OwnerIndex=0|Name=Designator|Text=NT1",                            # 2
        "|RECORD=2|OwnerIndex=0|Designator=1|Name=?",                                  # 3
        "|RECORD=2|OwnerIndex=0|Designator=2|Name=B",                                  # 4
        "|RECORD=44|OwnerIndex=0",                                                     # 5
        "|RECORD=45|OwnerIndex=4|ModelName=NT_SMALL|ModelType=PCBLIB",                 # 6
        "|RECORD=45|OwnerIndex=4|ModelName=NT_LARGE|ModelType=PCBLIB|IsCurrent=T",     # 7
        "|RECORD=45|OwnerIndex=4|ModelName=NT_SIM|ModelType=SIM|IsCurrent=T",          # 8
        "|RECORD=1|LibReference=TP|UniqueID=BBBBBBBB|ComponentKind=0|ComponentKindVersion2=5",  # 9
        "|RECORD=34|OwnerIndex=8|Name=Designator|Text=TP1",                            # 10
    )
    parts = components_from_stream(data)
    nt1 = parts["NT1"]
    assert (nt1.footprint, nt1.footprints, nt1.kind) == ("NT_LARGE", ["NT_SMALL", "NT_LARGE"], 4)
    assert nt1.pin_designators == ["1", "2"]  # pins keeps named ones only
    assert nt1.pins == {"2": "B"}
    assert (parts["TP1"].kind, parts["TP1"].footprint) == (5, None)


def test_parts_with_their_own_unique_ids_keep_every_id():
    data = _stream(
        "|RECORD=1|LibReference=OPA|UniqueID=PARTAAAA|CurrentPartId=1|PartCount=3",    # 1
        "|RECORD=34|OwnerIndex=0|Name=Designator|Text=U1",                             # 2
        "|RECORD=1|LibReference=OPA|UniqueID=PARTBBBB|CurrentPartId=2|PartCount=3",    # 3
        "|RECORD=34|OwnerIndex=2|Name=Designator|Text=U1",                             # 4
        "|RECORD=44|OwnerIndex=2",                                                     # 5
        "|RECORD=45|OwnerIndex=4|ModelName=SOIC8|ModelType=PCBLIB|IsCurrent=T",        # 6
    )
    u1 = components_from_stream(data)["U1"]
    assert u1.unique_ids == ["PARTAAAA", "PARTBBBB"]
    assert u1.unique_id == "PARTBBBB"  # the highest part's, as on the PCB
    assert u1.footprint == "SOIC8"


def test_jumper_kind_is_component_kind_version3():
    """Altium writes a Jumper as ComponentKindVersion3=6 (with or without zeros before)."""
    data = _stream(
        "|RECORD=1|LibReference=W|UniqueID=AAAAAAAA|ComponentKind=0"
        "|ComponentKindVersion2=0|ComponentKindVersion3=6",                            # 1
        "|RECORD=34|OwnerIndex=0|Name=Designator|Text=W1",                             # 2
        "|RECORD=1|LibReference=W|UniqueID=BBBBBBBB|ComponentKindVersion3=6",          # 3
        "|RECORD=34|OwnerIndex=2|Name=Designator|Text=W2",                             # 4
    )
    parts = components_from_stream(data)
    assert (parts["W1"].kind, parts["W2"].kind) == (6, 6)
