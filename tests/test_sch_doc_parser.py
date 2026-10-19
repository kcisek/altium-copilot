import re
import struct
from pathlib import Path
import pytest
from server.parsers.sch_doc import components_from_stream, parse_sch_doc

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
