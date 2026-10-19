from server.altium import enrich_netlist, enrich_netlist_sheets
from server.parsers.annotation import AnnotationEntry
from server.parsers.sch_doc import ComponentMeta


def _meta(refdes: str) -> ComponentMeta:
    return ComponentMeta(refdes=refdes, mpn="MPN", description="desc")


def _netlist(*refdes_list: str) -> dict:
    """Build a minimal netlist where each refdes has pin 1 on net NET."""
    pin_to_net = {r: {"1": "NET"} for r in refdes_list}
    return {"nets": {"NET": [(r, "1") for r in refdes_list]}, "pin_to_net": pin_to_net}


# ---------- basic ----------

def test_direct_match():
    netlist = _netlist("C1", "U1")
    enrich_netlist(netlist, {"C1": _meta("C1"), "U1": _meta("U1")})
    assert "C1" in netlist["components"]
    assert "U1" in netlist["components"]
    assert netlist["components"]["C1"]["pins"]["1"]["net"] == "NET"


def test_sheet_field_added_from_map():
    netlist = _netlist("C1", "U1")
    enrich_netlist(netlist, {"C1": _meta("C1"), "U1": _meta("U1")},
                   {"C1": "PowerSupply", "U1": "MCU"})
    assert netlist["components"]["C1"]["sheet"] == "PowerSupply"
    assert netlist["components"]["U1"]["sheet"] == "MCU"


def test_sheet_field_empty_when_no_map():
    netlist = _netlist("C1")
    enrich_netlist(netlist, {"C1": _meta("C1")})
    assert netlist["components"]["C1"]["sheet"] == ""


def test_unconnected_component_included():
    netlist = {"nets": {}, "pin_to_net": {}}
    enrich_netlist(netlist, {"F1": _meta("F1")}, {"F1": "PowerSupply"})
    assert netlist["components"]["F1"]["sheet"] == "PowerSupply"
    assert netlist["components"]["F1"]["pins"] == {}


# ---------- subsheet fan-out suffix styles ----------

def test_suffix_single_letter():
    """C1 in SchDoc → C1A, C1B in netlist (direct letter suffix)."""
    netlist = _netlist("C1A", "C1B")
    enrich_netlist(netlist, {"C1": _meta("C1")})
    assert "C1A" in netlist["components"]
    assert "C1B" in netlist["components"]
    assert "C1" not in netlist["components"]


def test_suffix_underscore_letter():
    """C1 in SchDoc → C1_A, C1_B in netlist."""
    netlist = _netlist("C1_A", "C1_B")
    enrich_netlist(netlist, {"C1": _meta("C1")})
    assert "C1_A" in netlist["components"]
    assert "C1_B" in netlist["components"]


def test_suffix_underscore_letter_digit():
    """C1 in SchDoc → C1_A1, C1_B2 in netlist."""
    netlist = _netlist("C1_A1", "C1_B2")
    enrich_netlist(netlist, {"C1": _meta("C1")})
    assert "C1_A1" in netlist["components"]
    assert "C1_B2" in netlist["components"]


def test_suffix_dot_letter():
    """C1 in SchDoc → C1.A, C1.B in netlist."""
    netlist = _netlist("C1.A", "C1.B")
    enrich_netlist(netlist, {"C1": _meta("C1")})
    assert "C1.A" in netlist["components"]
    assert "C1.B" in netlist["components"]


def test_suffix_letter_multi_digits():
    """C1 in SchDoc → C1A12 (letter + multiple digits)."""
    netlist = _netlist("C1A12", "C1B3")
    enrich_netlist(netlist, {"C1": _meta("C1")})
    assert "C1A12" in netlist["components"]
    assert "C1B3" in netlist["components"]


# ---------- disambiguation: C1 must not absorb C10 ----------

def test_c10_not_matched_as_c1_suffix():
    """C10 in netlist is NOT a suffixed variant of C1."""
    netlist = _netlist("C10")
    enrich_netlist(netlist, {"C1": _meta("C1"), "C10": _meta("C10")})
    # C1 has no connections → falls through to unconnected path
    assert "C1" in netlist["components"]
    assert netlist["components"]["C1"]["pins"] == {}
    # C10 matches directly
    assert "C10" in netlist["components"]
    assert netlist["components"]["C10"]["pins"]["1"]["net"] == "NET"


def test_c10_suffix_not_confused_with_c1():
    """C10_A should only be picked up by C10, not by C1."""
    netlist = _netlist("C10_A")
    enrich_netlist(netlist, {"C1": _meta("C1"), "C10": _meta("C10")})
    assert "C10_A" in netlist["components"]
    # C1 should NOT have absorbed C10_A
    assert "C1" in netlist["components"]
    assert netlist["components"]["C1"]["pins"] == {}


def test_numeric_only_suffix_not_matched():
    """C11 in netlist is not a suffix of C1 — purely numeric."""
    netlist = _netlist("C11")
    enrich_netlist(netlist, {"C1": _meta("C1"), "C11": _meta("C11")})
    assert "C1" in netlist["components"]
    assert netlist["components"]["C1"]["pins"] == {}
    assert "C11" in netlist["components"]
    assert netlist["components"]["C11"]["pins"]["1"]["net"] == "NET"


# ---------- sheet propagated to all suffixed variants ----------

def test_sheet_propagated_to_suffixed_variants():
    netlist = _netlist("C1_A", "C1_B")
    enrich_netlist(netlist, {"C1": _meta("C1")}, {"C1": "SubPage"})
    assert netlist["components"]["C1_A"]["sheet"] == "SubPage"
    assert netlist["components"]["C1_B"]["sheet"] == "SubPage"


# ---------- large refdes numbers ----------

def test_high_refdes_number_suffix():
    """R100_A should be matched as R100 suffix, not confused with anything."""
    netlist = _netlist("R100_A", "R100_B")
    enrich_netlist(netlist, {"R100": _meta("R100")})
    assert "R100_A" in netlist["components"]
    assert "R100_B" in netlist["components"]


# ---------- >26 instances: multi-letter suffix ----------

def test_suffix_multi_letter_aa():
    """C1_AA, C1_AB for >26 subsheet instances."""
    netlist = _netlist("C1_AA", "C1_AB")
    enrich_netlist(netlist, {"C1": _meta("C1")})
    assert "C1_AA" in netlist["components"]
    assert "C1_AB" in netlist["components"]


# ---------- lowercase suffix ----------

def test_suffix_lowercase_letter():
    """C1_a — lowercase suffix treated the same as uppercase."""
    netlist = _netlist("C1_a", "C1_b")
    enrich_netlist(netlist, {"C1": _meta("C1")})
    assert "C1_a" in netlist["components"]
    assert "C1_b" in netlist["components"]


# ---------- hyphen separator ----------

def test_suffix_hyphen_letter():
    """C1-A — hyphen as separator."""
    netlist = _netlist("C1-A", "C1-B")
    enrich_netlist(netlist, {"C1": _meta("C1")})
    assert "C1-A" in netlist["components"]
    assert "C1-B" in netlist["components"]


# ---------- numeric-only suffix (mandatory separator) ----------

def test_suffix_numeric_with_separator():
    """C1_11 — separator + digits only."""
    netlist = _netlist("C1_11", "C1_2")
    enrich_netlist(netlist, {"C1": _meta("C1")})
    assert "C1_11" in netlist["components"]
    assert "C1_2" in netlist["components"]


def test_suffix_numeric_no_separator_rejected():
    """C111 — digits only with no separator must NOT match C1."""
    netlist = _netlist("C111")
    enrich_netlist(netlist, {"C1": _meta("C1"), "C111": _meta("C111")})
    assert netlist["components"]["C1"]["pins"] == {}
    assert netlist["components"]["C111"]["pins"]["1"]["net"] == "NET"


# ---------- chained segments ----------

def test_suffix_chained_segments():
    """C1_1_E1 — two suffix segments chained."""
    netlist = _netlist("C1_1_E1")
    enrich_netlist(netlist, {"C1": _meta("C1")})
    assert "C1_1_E1" in netlist["components"]


# ---------- C10 not stolen by C1 with new pattern ----------

def test_c10_suffix_numeric_not_stolen_by_c1():
    """C10_11 should match C10, not C1."""
    netlist = _netlist("C10_11")
    enrich_netlist(netlist, {"C1": _meta("C1"), "C10": _meta("C10")})
    assert "C10_11" in netlist["components"]
    assert netlist["components"]["C1"]["pins"] == {}


# ---------- board-level annotation (.Annotation file) ----------

def _part(refdes: str, uid: str, mpn: str) -> ComponentMeta:
    return ComponentMeta(refdes=refdes, mpn=mpn, unique_id=uid, pins={"1": "IN"})


def _entry(logical: str, physical: str, path: str, sheet: str = "", channel: str = "") -> AnnotationEntry:
    document = f"{sheet}.SchDoc" if sheet else ""
    return AnnotationEntry(logical=logical, physical=physical, unique_id=path,
                           document=document, channel=channel)


def test_annotated_parts_take_their_physical_designators():
    """Bluetooth Sentinel: the board's U3 is sheet U1, the board's U1 is sheet U4."""
    netlist = {"nets": {}, "pin_to_net": {"U1": {"1": "RF"}, "U3": {"1": "SDA"}}}
    sheets = [
        ("Accelerometer", {"U1": _part("U1", "ACCEL", "LIS331DLTR")}),
        ("Bluetooth", {"U4": _part("U4", "RADIO", "CSR1010")}),
    ]
    annotation = [
        _entry("U1", "U3", "\\TOP\\ACCEL", "Accelerometer"),
        _entry("U4", "U1", "\\TOP\\RADIO", "Bluetooth"),
    ]
    comps = enrich_netlist_sheets(netlist, sheets, annotation)["components"]
    assert set(comps) == {"U1", "U3"}
    assert (comps["U1"]["mpn"], comps["U1"]["logical_refdes"], comps["U1"]["sheet"]) == (
        "CSR1010", "U4", "Bluetooth")
    assert comps["U1"]["pins"] == {"1": {"name": "IN", "net": "RF"}}
    assert (comps["U3"]["mpn"], comps["U3"]["logical_refdes"], comps["U3"]["sheet"]) == (
        "LIS331DLTR", "U1", "Accelerometer")
    assert comps["U3"]["unique_id"] == "ACCEL"


def test_same_sheet_designator_on_two_sheets_keeps_both_parts():
    """Sheets annotated on their own both use R9; only the EEPROM's is renamed."""
    netlist = _netlist("R1", "R9")
    sheets = [
        ("MCU", {"R9": _part("R9", "PULLUP", "10k")}),
        ("EEPROM", {"R9": _part("R9", "WP", "4k7")}),
    ]
    annotation = [_entry("R9", "R1", "\\E\\WP", "EEPROM")]
    comps = enrich_netlist_sheets(netlist, sheets, annotation)["components"]
    assert (comps["R9"]["mpn"], comps["R9"]["sheet"]) == ("10k", "MCU")
    assert "logical_refdes" not in comps["R9"]
    assert (comps["R1"]["mpn"], comps["R1"]["sheet"], comps["R1"]["logical_refdes"]) == (
        "4k7", "EEPROM", "R9")


def test_multi_channel_part_fills_every_channel():
    """One sheet placed three times: one entry per channel, same component UniqueId."""
    netlist = _netlist("LED1", "LED2", "LED3")
    sheets = [("Red_LED", {"LED1": _part("LED1", "ENIIUKEP", "LNJ247")})]
    annotation = [
        _entry("LED1", f"LED{n}", f"\\LEDS\\CH{n}\\ENIIUKEP", "Red_LED", f"Red_LED{n}")
        for n in (1, 2, 3)
    ]
    comps = enrich_netlist_sheets(netlist, sheets, annotation)["components"]
    assert sorted(comps) == ["LED1", "LED2", "LED3"]
    assert all(comps[r]["mpn"] == "LNJ247" for r in comps)
    assert "logical_refdes" not in comps["LED1"]
    assert comps["LED3"]["logical_refdes"] == "LED1"


def test_multi_part_component_is_one_physical_part():
    """Each part of a multi-part component has its own UniqueId and entry."""
    netlist = _netlist("U4")
    sheets = [("MCU", {"U2": _part("U2", "PARTB", "STM32")})]
    annotation = [
        _entry("U2", "U4", "\\P\\M\\PARTA", "MCU"),
        _entry("U2", "U4", "\\P\\M\\PARTB", "MCU"),
    ]
    comps = enrich_netlist_sheets(netlist, sheets, annotation)["components"]
    assert list(comps) == ["U4"]
    assert comps["U4"]["logical_refdes"] == "U2"


def test_entry_for_another_sheet_with_the_same_uid_is_not_applied():
    """A copied sheet keeps its components' UniqueIds; the document name tells them apart."""
    netlist = _netlist("C1", "C5")
    sheets = [
        ("Supply_A", {"C1": _part("C1", "SAMEUID", "A")}),
        ("Supply_B", {"C1": _part("C1", "SAMEUID", "B")}),
    ]
    annotation = [_entry("C1", "C5", "\\B\\SAMEUID", "Supply_B")]
    comps = enrich_netlist_sheets(netlist, sheets, annotation)["components"]
    assert (comps["C1"]["mpn"], comps["C1"]["sheet"]) == ("A", "Supply_A")
    assert (comps["C5"]["mpn"], comps["C5"]["sheet"]) == ("B", "Supply_B")


def test_entry_without_document_name_matches_by_unique_id():
    netlist = _netlist("R7")
    sheets = [("Power", {"R1": _part("R1", "RUID", "1k")})]
    comps = enrich_netlist_sheets(netlist, sheets, [_entry("R1", "R7", "\\RUID")])["components"]
    assert list(comps) == ["R7"]
    assert comps["R7"]["logical_refdes"] == "R1"


def test_sheet_name_match_is_case_insensitive():
    netlist = _netlist("R7")
    sheets = [("power", {"R1": _part("R1", "RUID", "1k")})]
    annotation = [_entry("R1", "R7", "\\S\\RUID", "POWER")]
    assert list(enrich_netlist_sheets(netlist, sheets, annotation)["components"]) == ["R7"]


def test_annotated_part_without_nets_is_kept_under_its_physical_designator():
    netlist = _netlist("U1")
    sheets = [("Mech", {"MH1": _part("MH1", "HOLE", "M3")})]
    comps = enrich_netlist_sheets(netlist, sheets, [_entry("MH1", "MH4", "\\M\\HOLE", "Mech")])[
        "components"]
    assert comps["MH4"]["pins"] == {}
    assert "MH1" not in comps


def test_part_missing_from_annotation_falls_back_to_its_sheet_designator():
    """Parts added after the board was annotated keep their sheet designator."""
    netlist = _netlist("U3", "FL1")
    sheets = [("RF", {
        "U1": _part("U1", "ACCEL", "LIS331"),
        "FL1": _part("FL1", "NEWPART", "LFB21"),
    })]
    comps = enrich_netlist_sheets(netlist, sheets, [_entry("U1", "U3", "\\R\\ACCEL", "RF")])[
        "components"]
    assert comps["FL1"]["mpn"] == "LFB21"
    assert comps["FL1"]["pins"]["1"]["net"] == "NET"
    assert "logical_refdes" not in comps["FL1"]


def test_unannotated_part_never_takes_a_name_an_annotated_part_has():
    """Sheet U1 is not in the file, but the netlist's U1 is the annotated radio."""
    netlist = _netlist("U1", "U3")
    sheets = [
        ("Accel", {"U1": _part("U1", "NOTLISTED", "LIS331")}),
        ("Radio", {"U4": _part("U4", "RADIO", "CSR1010")}),
    ]
    comps = enrich_netlist_sheets(netlist, sheets, [_entry("U4", "U1", "\\R\\RADIO", "Radio")])[
        "components"]
    assert comps["U1"]["mpn"] == "CSR1010"
    assert "U3" not in comps


def test_unannotated_suffix_fan_out_skips_names_annotated_parts_have():
    netlist = _netlist("C1_A", "C1_B")
    sheets = [
        ("Chan", {"C1": _part("C1", "CHANCAP", "100n")}),
        ("Other", {"C9": _part("C9", "OTHERCAP", "1u")}),
    ]
    comps = enrich_netlist_sheets(netlist, sheets, [_entry("C9", "C1_B", "\\O\\OTHERCAP", "Other")])[
        "components"]
    assert comps["C1_A"]["mpn"] == "100n"
    assert comps["C1_B"]["mpn"] == "1u"


def test_without_annotation_sheets_enrich_like_enrich_netlist():
    netlist = _netlist("C1_A", "C1_B", "U1")
    sheets = [("Chan", {"C1": _meta("C1")}), ("MCU", {"U1": _meta("U1"), "F1": _meta("F1")})]
    comps = enrich_netlist_sheets(netlist, sheets)["components"]
    assert sorted(comps) == ["C1_A", "C1_B", "F1", "U1"]
    assert comps["C1_B"]["sheet"] == "Chan"
    assert not any("logical_refdes" in c for c in comps.values())


def test_parts_of_one_component_on_two_sheets_unite_their_ids():
    """A component split over sheets: the PCB carries one part's unique ID."""
    netlist = _netlist("U1")
    sheets = [
        ("Power", {"U1": ComponentMeta(
            refdes="U1", unique_id="PWR", unique_ids=["PWR"], footprint="BGA",
            footprints=["BGA"], pin_designators=["A1"])}),
        ("IO", {"U1": ComponentMeta(
            refdes="U1", unique_id="IO", unique_ids=["IO"], footprint="BGA",
            footprints=["BGA"], pin_designators=["B2"])}),
    ]
    comps = enrich_netlist_sheets(netlist, sheets)["components"]
    assert comps["U1"]["unique_ids"] == ["PWR", "IO"]
    assert comps["U1"]["pin_designators"] == ["A1", "B2"]
    assert comps["U1"]["footprint_by_uid"] == {"PWR": "BGA", "IO": "BGA"}
    assert comps["U1"]["sheet"] == "IO"  # the latest sheet, as before
