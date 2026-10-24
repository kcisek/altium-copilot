"""Readers for Altium's fabrication and assembly outputs (server/parsers/fab_outputs.py).

Every file here is synthetic, written in the layouts Altium uses (fixed-width and CSV
pick and place with their preamble, BOM tables with a template title block, 80-column
IPC-D-356A records, IPC-2581 XML, Excellon with ;TYPE sections, .DRR and .LDP reports).
"""

import io

import pytest

from server.parsers import fab_outputs as fo

MIL_PER_MM = 1 / 0.0254
BOM = "\ufeff"


def approx(value):
    return pytest.approx(value, abs=1e-6)


# ---------------------------------------------------------------- helpers

def fixed_width(rows: list[list[str]]) -> list[str]:
    """Rows padded like Altium's text output: each column as wide as its widest cell plus
    one space, values left-aligned, a value (not a header name) with a space in it quoted."""
    cells = [rows[0]] + [[f'"{c}"' if " " in c and not c.startswith('"') else c for c in row]
                         for row in rows[1:]]
    widths = [max(len(row[i]) for row in cells) for i in range(len(cells[0]))]
    return [" ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() + " " for row in cells]


PREAMBLE = [
    "Altium Designer Pick and Place Locations",
    "C:\\Projects\\Demo\\Project Outputs for Demo\\Pick Place for Demo.txt",
    "",
    "=" * 60,
    "File Design Information:",
    "",
    "Date:       24/10/26",
    "Time:       10:45",
    "Revision:   Not in VersionControl",
    "Variant:    {variant}",
    "Units used: {units}",
    "",
]
PNP_HEADER = ["Designator", "Comment", "Layer", "Footprint", "Center-X(mm)", "Center-Y(mm)",
              "Rotation", "Description"]


def pnp_text(rows, variant="No variations", units="mm", header=PNP_HEADER, bom=True):
    lines = [line.format(variant=variant, units=units) for line in PREAMBLE]
    table = fixed_width([header, *rows])
    text = "\r\n".join(lines) + "\r\n" + (BOM if bom else "") + "\r\n".join(table) + "\r\n"
    return ((BOM if bom else "") + text).encode("utf-8")


def pnp_csv(rows, header=PNP_HEADER, quote_header=False, units="mm"):
    lines = [line.format(variant="Assembly A", units=units) for line in PREAMBLE]
    head = ",".join(f'"{c}"' if quote_header else c for c in header)
    body = [",".join(f'"{c}"' for c in row) for row in rows]
    return ("\r\n".join([*lines, head, *body]) + "\r\n").encode("cp1252")


# ---------------------------------------------------------------- shared helpers

def test_parse_number_units_and_decimal_comma():
    assert fo.parse_number("12.5") == (12.5, None)
    assert fo.parse_number("-3") == (-3.0, None)
    assert fo.parse_number("1234.5mil") == (1234.5, "mil")
    assert fo.parse_number(" 0.3 MM ") == (0.3, "mm")
    assert fo.parse_number("2mils") == (2.0, "mil")
    assert fo.parse_number("1,5") is None
    assert fo.parse_number("1,5", decimal_comma=True) == (1.5, None)
    assert fo.parse_number("abc") is None
    assert fo.parse_number(None) is None


def test_unit_scale():
    assert fo.unit_scale("mm") == approx(MIL_PER_MM)
    assert fo.unit_scale("MIL") == 1.0
    assert fo.unit_scale("inch") == 1000.0
    assert fo.unit_scale("parsec") is None
    assert fo.unit_scale(None) is None


def test_decode_lines_mixed_encodings_and_byte_order_marks():
    data = (b"\xef\xbb\xbfplain\r\n" + "caf\u00e9".encode("utf-8") + b"\r\n"
            + "\u00b5F".encode("cp1252") + b"\r\n\xef\xbb\xbfheader\n")
    lines, encoding = fo.decode_lines(data)
    assert lines == ["plain", "caf\u00e9", "\u00b5F", "header"]
    assert encoding == "mixed"
    assert fo.decode_lines(b"a\nb\n") == (["a", "b"], "ascii")
    utf16 = "\ufeffx,y\r\n1,2\r\n".encode("utf-16-le")
    assert fo.decode_lines(b"\xff\xfe" + utf16[2:]) == (["x,y", "1,2"], "utf-16")


def test_side_of_layer_words():
    for word in ("TopLayer", "Top", "T", "top layer"):
        assert fo.side_of(word) == "top"
    for word in ("BottomLayer", "Bottom", "B", "Bot"):
        assert fo.side_of(word) == "bottom"
    assert fo.side_of("MultiLayer") is None
    assert fo.side_of(None) is None


# ---------------------------------------------------------------- pick and place

PNP_ROWS = [
    ["R1", "10k", "TopLayer", "RES0603", "10.0000", "20.5000", "90", "Resistor"],
    ["C1", "100n 50V", "BottomLayer", "CAP0402", "-1.2500", "3.0000", "270",
     "Capacitor, ceramic"],
    ["J1", "HDR", "TopLayer", "HDR1X2", "0.0000", "0.0000", "0", "Header 0.1\" pitch"],
]


def test_pick_place_text_output_fixed_width_utf8():
    result = fo.parse_pick_place(pnp_text(PNP_ROWS), "Pick Place for Demo.txt")
    assert result.warnings == []
    assert result.delimiter == "fixed"
    assert result.header_line == 13
    assert result.generator == "Altium Designer Pick and Place Locations"
    assert result.source.endswith("Pick Place for Demo.txt")  # the file's own path
    assert result.variant is None
    assert result.preamble["Units used"] == "mm"
    assert (result.units, result.units_from, result.point) == ("mm", "header", "center")
    assert result.encoding == "utf-8"
    assert [r.designator for r in result.rows] == ["R1", "C1", "J1"]
    r1, c1, j1 = result.rows
    assert (r1.x_mil, r1.y_mil) == (approx(10 * MIL_PER_MM), approx(20.5 * MIL_PER_MM))
    assert (r1.side, r1.rotation, r1.layer, r1.footprint) == ("top", 90.0, "TopLayer", "RES0603")
    assert c1.comment == "100n 50V"
    assert c1.side == "bottom"
    assert c1.x_mil == approx(-1.25 * MIL_PER_MM)
    assert j1.description == 'Header 0.1" pitch'  # an unescaped inch mark stays
    assert r1.points == {"center": (r1.x_mil, r1.y_mil)}
    assert r1.line == 14


def test_pick_place_csv_output_quoted_fields_and_inch_marks():
    rows = PNP_ROWS + [["U1", "MCU", "TopLayer", "QFN32", "5.5", "6.5", "45", 'Pitch 0.5" (12.7mm) "x"']]
    for quote_header in (True, False):
        result = fo.parse_pick_place(pnp_csv(rows, quote_header=quote_header), "x.csv")
        assert result.delimiter == ","
        assert result.encoding == "ascii"
        assert result.variant == "Assembly A"
        assert len(result.rows) == 4 and result.unparsed_lines == []
        assert result.rows[1].description == "Capacitor, ceramic"
        assert result.rows[2].description == 'Header 0.1" pitch'
        assert result.rows[3].description == 'Pitch 0.5" (12.7mm) "x"'
        assert result.rows[3].rotation == 45.0


def test_pick_place_text_and_csv_twins_agree():
    text = fo.parse_pick_place(pnp_text(PNP_ROWS), "a.txt")
    sheet = fo.parse_pick_place(pnp_csv(PNP_ROWS), "a.csv")

    def key(rows):
        return [(r.designator, round(r.x_mil, 6), round(r.y_mil, 6), r.rotation, r.side,
                 r.footprint, r.comment, r.description) for r in rows]

    assert key(text.rows) == key(sheet.rows)


def test_pick_place_value_with_a_line_break():
    rows = [["R1", "10k", "TopLayer", "RES0603", "1.0", "2.0", "0", "OK"],
            ["R2", "1k", "TopLayer", "RES0603", "3.0", "4.0", "180",
             "first line\r\nsecond line"],
            ["R3", "1k", "TopLayer", "RES0603", "5.0", "6.0", "0", 'odd 1" quote']]
    result = fo.parse_pick_place(pnp_csv(rows), "x.csv")
    assert [r.designator for r in result.rows] == ["R1", "R2", "R3"]
    assert result.rows[1].description == "first line\nsecond line"
    assert result.unparsed_lines == []
    # Text output: the continuation line is no row of its own and joins its row; a row
    # with an odd quote whose next line is a row of its own stays alone.
    header, first, third, fourth = fixed_width([
        PNP_HEADER, ["R1", "10k", "TopLayer", "RES0603", "1.0", "2.0", "0", '"open'],
        ["R3", "1k", "TopLayer", "RES0603", "5.0", "6.0", "0", 'a 1" b'],
        ["R4", "1k", "TopLayer", "RES0603", "7.0", "8.0", "0", "x"]])
    text = "\r\n".join([header, first, 'continued"', third, fourth])
    result = fo.parse_pick_place(text.encode(), "x.txt")
    assert [r.designator for r in result.rows] == ["R1", "R3", "R4"]
    assert " ".join(result.rows[0].description.split()) == "open continued"
    assert result.rows[2].y_mil == approx(8 * MIL_PER_MM)
    assert result.unparsed_lines == []


def test_pick_place_overflowing_value_falls_back_to_whitespace_split():
    table = fixed_width([PNP_HEADER, ["R1", "10k", "TopLayer", "RES0603", "1.0", "2.0", "0",
                                      "a"]])
    long_row = 'R2 "a very long comment" TopLayer RES0603 3.0 4.0 90 b'
    result = fo.parse_pick_place("\n".join([*table, long_row]).encode(), "x.txt")
    assert [r.designator for r in result.rows] == ["R1", "R2"]
    assert result.rows[1].comment == "a very long comment"
    assert result.rows[1].x_mil == approx(3 * MIL_PER_MM)


def test_pick_place_unreadable_row_is_listed():
    table = fixed_width([PNP_HEADER, ["R1", "10k", "TopLayer", "RES0603", "1.0", "2.0", "0", "a"]])
    result = fo.parse_pick_place("\n".join([*table, "garbage", ""]).encode(), "x.txt")
    assert len(result.rows) == 1
    assert result.unparsed_lines == [3]
    assert "1 rows not split" in result.warnings[0]


def test_pick_place_mixed_line_encodings():
    lines = [line.format(variant="V\u00e6rsion", units="mm") for line in PREAMBLE]
    head = ",".join(PNP_HEADER)
    rows = ['"R1","10\u00b5","TopLayer","X","1","2","0","\u03a9 part"']
    data = "\r\n".join(lines).encode("cp1252") + b"\r\n" + head.encode() + b"\r\n" + \
        "\r\n".join(rows).encode("utf-8")
    result = fo.parse_pick_place(data, "x.csv")
    assert result.encoding == "mixed"
    assert result.variant == "V\u00e6rsion"
    assert result.rows[0].comment == "10\u00b5"
    assert result.rows[0].description == "\u03a9 part"


def test_pick_place_mil_units_from_the_header():
    header = [c.replace("(mm)", "(mil)") for c in PNP_HEADER]
    result = fo.parse_pick_place(pnp_csv([["R1", "x", "T", "f", "100", "-50", "0", "d"]],
                                         header=header, units="mil"), "x.csv")
    assert (result.units, result.units_from) == ("mil", "header")
    assert (result.rows[0].x_mil, result.rows[0].y_mil) == (100.0, -50.0)
    assert result.rows[0].side == "top"


def test_pick_place_older_header_with_units_in_the_values():
    header = ["Designator", "Footprint", "Mid X", "Mid Y", "Ref X", "Ref Y", "Pad X", "Pad Y",
              "TB", "Rotation", "Comment"]
    rows = [["U1", "SO8", "1000mil", "2000mil", "1100mil", "2000mil", "900mil", "1950mil",
             "B", "180.00", "IC"],
            ["R1", "0603", "25.4mm", "0mm", "25.4mm", "0mm", "25mm", "0mm", "T", "0", "R"]]
    text = "\n".join(["Pick and place for test", "", *fixed_width([header, *rows])])
    result = fo.parse_pick_place(text.encode(), "old.txt")
    assert result.warnings == []
    assert (result.units, result.units_from, result.point) == ("mil", "values", "center")
    u1, r1 = result.rows
    assert (u1.x_mil, u1.y_mil, u1.side, u1.rotation) == (1000.0, 2000.0, "bottom", 180.0)
    assert u1.points["ref"] == (1100.0, 2000.0)
    assert u1.points["pad"] == (900.0, 1950.0)
    assert r1.x_mil == approx(1000.0)  # a value's own unit wins
    assert r1.side == "top"


def test_pick_place_semicolons_decimal_commas_and_ref_point():
    text = ("Designator;Layer;Ref-X(mm);Ref-Y(mm);Rotation;Value\n"
            "R1;Top;1,5;-2,25;90,5;10k\n")
    result = fo.parse_pick_place(text.encode(), "x.csv")
    assert (result.delimiter, result.point, result.units) == (";", "ref", "mm")
    row = result.rows[0]
    assert (row.x_mil, row.y_mil) == (approx(1.5 * MIL_PER_MM), approx(-2.25 * MIL_PER_MM))
    assert row.rotation == 90.5
    assert row.value == "10k"
    assert row.side == "top"


def test_pick_place_extra_columns_are_kept():
    header = PNP_HEADER + ["Height(mm)", "Supplier"]
    result = fo.parse_pick_place(
        pnp_csv([["R1", "x", "T", "f", "1", "2", "0", "d", "0.5", "Acme"]], header=header),
        "x.csv")
    assert result.rows[0].extra == {"Supplier": "Acme"}
    assert result.roles["height"] == 8


def test_pick_place_units_unstated_are_assumed_mil():
    text = "Designator,Center-X,Center-Y,Rotation\nR1,10,20,0\n"
    result = fo.parse_pick_place(text.encode(), "x.csv")
    assert (result.units, result.units_from) == ("mil", "assumed")
    assert result.rows[0].x_mil == 10.0
    assert "units not stated" in result.warnings[0]


def test_pick_place_without_a_header():
    result = fo.parse_pick_place(b"just,some,text\n1,2,3\n", "x.csv")
    assert result.rows == [] and result.header_line is None
    assert result.warnings == ["no header row with a designator and an X column"]


def test_pick_place_trailing_delimiters_and_bad_numbers():
    text = ("Designator,Center-X(mm),Center-Y(mm),Rotation,\n"
            "R1,1,2,0,\n"
            "R2,abc,2,0,\n"
            "R3,1,2,ninety,\n")
    result = fo.parse_pick_place(text.encode(), "x.csv")
    assert [r.designator for r in result.rows] == ["R1"]
    assert result.unparsed_lines == [3, 4]


# ---------------------------------------------------------------- BOM

def test_split_designators():
    assert fo.split_designators("R1, R2, R3") == (["R1", "R2", "R3"], 0)
    assert fo.split_designators("R1,R2;R3 R4") == (["R1", "R2", "R3", "R4"], 0)
    assert fo.split_designators("R1-R4, R7") == (["R1", "R2", "R3", "R4", "R7"], 1)
    assert fo.split_designators("C10 - C12") == (["C10", "C11", "C12"], 1)
    assert fo.split_designators("J1-1") == (["J1-1"], 0)       # a pin-like name stays
    assert fo.split_designators("R4-R1") == (["R4-R1"], 0)     # descending: as written
    assert fo.split_designators("") == ([], 0)


def test_bom_role_names():
    assert fo.bom_role("Designator") == ("designator", 0)
    assert fo.bom_role("Qty") == ("quantity", 0)
    assert fo.bom_role("Manufacturer Part Number 2") == ("mpn", 2)
    assert fo.bom_role("Manufacture Part Number") == ("mpn", 0)
    assert fo.bom_role("Mfr. P/N") == ("mpn", 0)
    assert fo.bom_role("Manufacturer 1") == ("manufacturer", 1)
    assert fo.bom_role("Manufacture") == ("manufacturer", 0)
    assert fo.bom_role("Part Number") == ("part_number", 0)
    assert fo.bom_role("Supplier Part Number 1") == (None, 0)
    assert fo.bom_role("Name") == ("item_id", 0)
    assert fo.bom_role("Line #") == ("line", 0)
    assert fo.bom_role("Something else") == (None, 0)


def test_bom_csv_altium_default_columns():
    text = ('"Comment","Description","Designator","Footprint","LibRef","Quantity"\r\n'
            '"10k","Resistor","R1, R2, R3","RES0603","RES","3"\r\n'
            '"100n","Capacitor","C1","CAP0402","CAP","1"\r\n')
    bom = fo.parse_bom_csv(text.encode("utf-8-sig"))
    assert bom.warnings == []
    assert (bom.header_row, bom.delimiter, bom.encoding) == (1, ",", "utf-8")
    assert [line.designators for line in bom.lines] == [["R1", "R2", "R3"], ["C1"]]
    assert [line.quantity for line in bom.lines] == [3.0, 1.0]
    assert bom.lines[0].comment == "10k" and bom.lines[0].libref == "RES"
    assert bom.lines[0].footprint == "RES0603"
    assert bom.lines[0].row == 2
    assert bom.designators == ["R1", "R2", "R3", "C1"]


def test_bom_rows_below_a_template_title_block():
    rows = [
        [],
        ["Bill of Materials for", "Demo.PrjPcb"],
        ["Source Data From:", "Demo.PrjPcb"],
        ["Variant:", "Assembly A"],
        ["Report Date:", "24/10/2026"],
        [],
        [],
        ["", "Primary", "", "", "", "Secondary"],
        ["Altium Vault P/N", "Designator", "Description", "Qty", "Supplier",
         "Manufacture", "Manufacture Part Number", "Supplier", "Manufacturer 2",
         "Manufacturer Part Number 2"],
        ["CMP-1", "R1, R2", "Resistor", "2", "Dist A", "Maker A", "MPN-A", "Dist B",
         "Maker B", "MPN-B"],
        ["CMP-2", "U1", "Regulator", "1", "", "Maker C", "MPN-C", "", "", ""],
        [],
        ["", "", "", "", "", "Approved", "", "Notes"],
    ]
    bom = fo.parse_bom_rows(rows)
    assert bom.header_row == 9
    assert bom.title == {"Source Data From": "Demo.PrjPcb", "Variant": "Assembly A",
                         "Report Date": "24/10/2026"}
    first, second = bom.lines
    assert (first.designators, first.quantity) == (["R1", "R2"], 2.0)
    assert (first.mpn, first.manufacturer, first.item_id) == ("MPN-A", "Maker A", "CMP-1")
    assert second.mpn == "MPN-C"
    assert first.fields["Supplier"] == "Dist A"
    assert first.fields["Supplier (2)"] == "Dist B"
    assert bom.skipped_rows == 1  # the footer


def test_bom_lowest_numbered_mpn_wins_and_ranges_expand():
    rows = [["Designator", "Quantity", "Manufacturer Part Number 2",
             "Manufacturer Part Number 1", "Value"],
            ["R1-R3, R9", "4", "ALT", "MAIN", "1k"],
            ["", "", "", "", "a note"],
            ["R10", "x", "", "", ""]]
    bom = fo.parse_bom_rows(rows)
    assert bom.lines[0].designators == ["R1", "R2", "R3", "R9"]
    assert bom.lines[0].mpn == "MAIN"
    assert bom.lines[0].value == "1k"
    assert bom.ranges_expanded == 1
    assert bom.skipped_rows == 1
    assert bom.lines[1].quantity is None
    assert bom.warnings == ["row 4: quantity is not a number"]


def test_bom_semicolon_csv_and_header_without_quantity():
    text = "Ref;Description;MPN\nR1 R2;Resistor;ABC-1\n"
    bom = fo.parse_bom_csv(text.encode())
    assert bom.delimiter == ";"
    assert bom.lines[0].designators == ["R1", "R2"]
    assert bom.lines[0].quantity is None
    assert bom.lines[0].mpn == "ABC-1"


def test_bom_without_a_header():
    bom = fo.parse_bom_csv(b"a,b,c\n1,2,3\n")
    assert bom.lines == [] and bom.header_row is None
    assert bom.warnings == ["no header row with a designator column"]
    assert fo.parse_bom_rows([["x"]]).warnings == ["no header row with a designator column"]


# ---------------------------------------------------------------- IPC-D-356A

def ipc_record(code, net, refdes, pin="", *, hole=None, plated=None, access=0, x=0, y=0,
               w=None, h=None, rot=None, mask=3, mid=False):
    """One 80-column record in the IPC-D-356A layout (values in file units)."""
    line = code + net.ljust(14) + "   " + refdes.ljust(6) + "-" + pin.ljust(4)
    line += "M" if mid else " "
    line += f"D{hole:04d}" if hole is not None else "     "
    line += plated or " "
    line += f"A{access:02d}"
    line += f"X{'-' if x < 0 else ' '}{abs(x):06d}Y{'-' if y < 0 else ' '}{abs(y):06d}"
    line += f"X{w:04d}" if w is not None else "     "
    line += f"Y{h:04d}" if h is not None else "     "
    line += f"R{rot:03d}" if rot is not None else "    "
    line += f" S{mask}"
    assert len(line) == 74
    return line.ljust(80)


def ipc356_file(units="CUST 0", extra=()):
    lines = [
        "C  IPC-D-356A test file",
        "P  JOB   DEMO",
        f"P  UNITS {units}" if units is not None else "C  no units",
        "P  VER   IPC-D-356A",
        "P  IMAGE PRIMARY",
        "P  NNAME1     A_VERY_LONG_NET_NAME_1",
        ipc_record("317", "GND", "J1", "1", hole=400, plated="P", access=0, x=10000,
                   y=5000, w=600, mask=3),
        ipc_record("317", "NNAME1", "J1", "2", hole=400, plated="P", access=0, x=11000,
                   y=5000, w=600, mask=3),
        ipc_record("317", "GND", "VIA", "", hole=120, plated="P", access=0, x=-2000,
                   y=-3000, w=240, mask=0),
        ipc_record("327", "NET1", "R1", "1", plated="U", access=1, x=20000, y=1000, w=300,
                   h=200, rot=90, mask=1),
        ipc_record("327", "NET1", "U1", "12", plated="U", access=2, x=20500, y=1000,
                   w=300, h=200, rot=0, mask=2),
        ipc_record("327", "N/C", "U1", "13", plated="U", access=2, x=20600, y=1000, w=300,
                   h=200, rot=0, mask=2),
        ipc_record("367", "", "", "", hole=1250, plated="U", access=0, x=500, y=500,
                   mask=0),
        "379GND NET1 NNAME1",
        "079 NET2",
        "389BOARD_EDGE    X000000Y000000",
        *extra,
        "999",
    ]
    return ("\r\n".join(lines) + "\r\n").encode()


def test_ipc356_records_inch_units():
    ipc = fo.parse_ipc356(ipc356_file())
    assert ipc.warnings == []
    assert (ipc.units, ipc.units_raw) == ("inch", "CUST 0")
    assert ipc.parameters["JOB"] == "DEMO" and ipc.parameters["VER"] == "IPC-D-356A"
    assert ipc.aliases == {"NNAME1": "A_VERY_LONG_NET_NAME_1"}
    assert ipc.ended
    assert ipc.record_counts["317"] == 3 and ipc.record_counts["327"] == 3
    j1, j2, via, r1, u1, nc, tool = ipc.records
    assert (j1.code, j1.net, j1.refdes, j1.pin) == ("317", "GND", "J1", "1")
    assert (j1.x_mil, j1.y_mil, j1.hole_mil, j1.width_mil) == (1000.0, 500.0, 40.0, 60.0)
    assert (j1.plated, j1.access, j1.side(), j1.soldermask) == (True, 0, "both", 3)
    assert (j2.net, j2.raw_net) == ("A_VERY_LONG_NET_NAME_1", "NNAME1")
    assert via.is_via and (via.x_mil, via.y_mil, via.hole_mil) == (-200.0, -300.0, 12.0)
    assert (r1.side(), r1.rotation, r1.width_mil, r1.height_mil) == ("top", 90.0, 30.0, 20.0)
    assert r1.plated is False and r1.hole_mil is None
    assert u1.side() == "bottom"            # by its solder mask code
    assert u1.side(copper_layers=2) == "bottom"
    assert u1.side(copper_layers=4) == "inner"
    assert nc.unconnected and not j1.unconnected
    assert tool.code == "367" and tool.hole_mil == 125.0
    assert j1.line == 7
    assert ipc.nets() == {"GND": [("J1", "1")], "A_VERY_LONG_NET_NAME_1": [("J1", "2")],
                          "NET1": [("R1", "1"), ("U1", "12")]}
    assert ipc.adjacency == {"GND": ["NET1", "A_VERY_LONG_NET_NAME_1", "NET2"]}
    assert ipc.comments == ["IPC-D-356A test file"]


@pytest.mark.parametrize("units, scale", [("CUST", 0.1), ("CUST 1", 0.001 * MIL_PER_MM),
                                          ("SI", 0.001 * MIL_PER_MM), ("ENG", 0.1)])
def test_ipc356_units(units, scale):
    ipc = fo.parse_ipc356(ipc356_file(units))
    assert ipc.records[0].x_mil == approx(10000 * scale)
    assert ipc.records[0].hole_mil == approx(400 * scale)


def test_ipc356_unknown_and_missing_units():
    ipc = fo.parse_ipc356(ipc356_file("FURLONG"))
    assert ipc.units == "inch"
    assert "not known" in ipc.warnings[0]
    ipc = fo.parse_ipc356(ipc356_file(None))
    assert ipc.units == "inch"
    assert ipc.warnings == ["no P UNITS record: inch (CUST 0) assumed"]


def test_ipc356_unreadable_record_and_continuations():
    ipc = fo.parse_ipc356(ipc356_file(extra=["317GND           J1    -3    garbage",
                                             "027 more data"]))
    assert len(ipc.records) == 7
    assert ipc.warnings[0].endswith("317 record not read")
    assert ipc.warnings[1] == "1 continuation records (017/027) not read"


# ---------------------------------------------------------------- IPC-2581

IPC2581 = """<?xml version="1.0" encoding="UTF-8"?>
<IPC-2581 revision="C" xmlns="http://webstds.ipc.org/2581">
  <Content roleRef="Owner"><DictionaryStandard units="{units}"/></Content>
  <Bom name="Demo">
    <BomItem OEMDesignNumberRef="CMP-1" quantity="2">
      <RefDes name="R1" populate="true" layerRef="TOP"/>
      <RefDes name="R2" populate="false" layerRef="BOTTOM"/>
    </BomItem>
  </Bom>
  <Ecad name="Demo">
    <CadHeader units="{units}">
      <Spec name="SPEC_CORE">
        <General type="MATERIAL"><Property text="FR-4"/></General>
        <Dielectric type="DIELECTRIC_CONSTANT"><Property value="4.3"/></Dielectric>
        <Dielectric type="LOSS_TANGENT"><Property value="0.02"/></Dielectric>
      </Spec>
      <Spec name="SPEC_CU"><General type="MATERIAL"><Property text="Copper"/></General></Spec>
    </CadHeader>
    <CadData>
      <Layer name="TOP" layerFunction="SIGNAL" side="TOP" polarity="POSITIVE"/>
      <Layer name="CORE" layerFunction="DIELCORE" side="INTERNAL"/>
      <Layer name="BOTTOM" layerFunction="SIGNAL" side="BOTTOM" polarity="POSITIVE"/>
      <Layer name="DRILL_1_2" layerFunction="DRILL" side="ALL"/>
      <Stackup name="S" overallThickness="{total}">
        <StackupGroup name="G" thickness="{total}">
          <StackupLayer layerOrGroupRef="TOP" thickness="{cu}" sequence="1"><SpecRef id="SPEC_CU"/></StackupLayer>
          <StackupLayer layerOrGroupRef="CORE" thickness="{core}" sequence="2"><SpecRef id="SPEC_CORE"/></StackupLayer>
          <StackupLayer layerOrGroupRef="BOTTOM" thickness="{cu}" sequence="3"><SpecRef id="SPEC_CU"/></StackupLayer>
        </StackupGroup>
      </Stackup>
      <Step name="Demo">
        <Component refDes="R1" packageRef="RES0603" layerRef="TOP" part="CMP-1" mountType="SMT">
          <Xform rotation="90"/>
          <Location x="{x1}" y="{y1}"/>
        </Component>
        <Component refDes="R2" packageRef="RES0603" layerRef="BOTTOM" part="CMP-1">
          <Xform rotation="180" mirror="true"/>
          <Location x="{x2}" y="0"/>
        </Component>
        <LogicalNet name="GND"><PinRef componentRef="R1" pin="1"/><PinRef componentRef="R2" pin="1"/></LogicalNet>
        <LogicalNet name="NET1"><PinRef componentRef="R1" pin="2"/></LogicalNet>
        <PhyNetGroup name="P">
          <PhyNet name="GND"><PhyNetPoint x="0" y="0" layerRef="TOP"/><PhyNetPoint x="1" y="0" layerRef="BOTTOM"/></PhyNet>
        </PhyNetGroup>
        <LayerFeature layerRef="DRILL_1_2">
          <Set net="GND">
            <Hole name="H1" diameter="{via}" platingStatus="VIA" plusTol="0" minusTol="0" x="{x1}" y="{y1}"/>
          </Set>
          <Set net="No Net">
            <Hole name="H2" diameter="{via}" platingStatus="NONPLATED" x="0" y="0"/>
          </Set>
        </LayerFeature>
        <LayerHole name="LH1" diameter="{via}" platingStatus="VIA"><Span fromLayer="TOP" toLayer="BOTTOM"/></LayerHole>
      </Step>
    </CadData>
  </Ecad>
</IPC-2581>
"""


def ipc2581(units="MILLIMETER", scale=1.0):
    return IPC2581.format(units=units, total=1.6 * scale, cu=0.035 * scale, core=1.53 * scale,
                          x1=2.54 * scale, y1=1.27 * scale, x2=5.08 * scale,
                          via=0.3 * scale).encode()


def test_ipc2581_model():
    export = fo.parse_ipc2581(ipc2581())
    assert export.warnings == []
    assert (export.revision, export.units) == ("C", "MILLIMETER")
    assert export.nets == {"GND": [("R1", "1"), ("R2", "1")], "NET1": [("R1", "2")]}
    assert export.phy_nets == {"GND": 2}
    assert export.net_names == {"GND", "No Net"}
    r1, r2 = export.components
    assert (r1.refdes, r1.side, r1.rotation, r1.mirror, r1.package, r1.part) == (
        "R1", "top", 90.0, None, "RES0603", "CMP-1")
    assert (r1.x_mil, r1.y_mil, r1.mount_type) == (approx(100.0), approx(50.0), "SMT")
    assert (r2.side, r2.rotation, r2.mirror, r2.x_mil) == ("bottom", 180.0, True, approx(200.0))
    assert [layer.function for layer in export.layers] == ["SIGNAL", "DIELCORE", "SIGNAL",
                                                           "DRILL"]
    top, core, bottom = export.stackup
    assert (top.name, top.sequence, top.material, top.function) == ("TOP", 1, "Copper", "SIGNAL")
    assert top.thickness_mil == approx(0.035 * MIL_PER_MM)
    assert (core.material, core.dielectric_constant, core.loss_tangent) == ("FR-4", 4.3, 0.02)
    assert core.thickness_mil == approx(1.53 * MIL_PER_MM)
    assert export.overall_thickness_mil == approx(1.6 * MIL_PER_MM)
    h1, h2, layer_hole = export.holes
    assert (h1.kind, h1.plating, h1.net, h1.layer) == ("Hole", "VIA", "GND", "DRILL_1_2")
    assert (h1.diameter_mil, h1.x_mil) == (approx(0.3 * MIL_PER_MM), approx(100.0))
    assert h2.plating == "NONPLATED" and h2.net == "No Net"
    assert (layer_hole.kind, layer_hole.span, layer_hole.x_mil) == (
        "LayerHole", ("TOP", "BOTTOM"), None)
    assert export.span_count == 1
    assert export.populate == {"R1": True, "R2": False}


def test_ipc2581_inch_units_and_sources(tmp_path):
    data = ipc2581("INCH", scale=1 / 25.4)
    path = tmp_path / "demo.cvg"
    path.write_bytes(data)
    for source in (data, str(path), path, io.BytesIO(data)):
        export = fo.parse_ipc2581(source)
        assert export.units == "INCH"
        assert export.components[0].x_mil == approx(100.0)
        assert export.stackup[1].thickness_mil == approx(1.53 * MIL_PER_MM)


def test_ipc2581_broken_xml_keeps_what_was_read():
    data = ipc2581()
    export = fo.parse_ipc2581(data[: data.index(b"<LogicalNet name=\"NET1\"")] + b"<oops")
    assert export.warnings and export.warnings[0].startswith("XML error")
    assert export.nets == {"GND": [("R1", "1"), ("R2", "1")]}
    assert len(export.components) == 2


def test_ipc2581_unknown_units():
    export = fo.parse_ipc2581(ipc2581("FATHOM"))
    assert "not known" in export.warnings[0]
    assert export.components[0].x_mil == approx(2.54 * MIL_PER_MM)


# ---------------------------------------------------------------- Excellon

EXCELLON_INCH = """M48
;Layer_Color=9474304
;FILE_FORMAT=2:5
INCH,TZ
;TYPE=PLATED
T1F00S00C0.01200
T2F00S00C0.04000
;TYPE=NON_PLATED
T3F00S00C0.12500
%
T01
X100000Y50000
Y75000
X-25000Y125000
T02
X200000Y200000
G00X100000Y100000
M15
G01X150000Y100000
M16
M17
T03
X10000Y10000G85X20000Y10000
X300000Y300000
M30
"""


def test_excellon_inch_trailing_zeros_kept():
    drill = fo.parse_excellon(EXCELLON_INCH.encode(), "Demo-RoundHoles.TXT")
    assert drill.warnings == []
    assert (drill.units, drill.zeros, drill.integer_digits, drill.decimals,
            drill.format_from) == ("inch", "TZ", 2, 5, "FILE_FORMAT")
    assert drill.file_type == "PLATED"
    assert drill.ended
    t1, t2, t3 = (drill.tools[n] for n in (1, 2, 3))
    assert (t1.diameter_mil, t1.plated, t1.hits) == (approx(12.0), True, 3)
    assert (t2.diameter_mil, t2.plated, t2.hits, t2.slots) == (approx(40.0), True, 1, 1)
    assert (t3.diameter_mil, t3.plated, t3.hits, t3.slots) == (approx(125.0), False, 1, 1)
    points = [(h.tool, h.kind, round(h.x_mil, 6), round(h.y_mil, 6)) for h in drill.hits]
    assert points[:4] == [(1, "hole", 1000.0, 500.0), (1, "hole", 1000.0, 750.0),
                          (1, "hole", -250.0, 1250.0), (2, "hole", 2000.0, 2000.0)]
    route = drill.hits[4]
    assert (route.kind, route.x_mil, route.y_mil, route.x2_mil, route.y2_mil) == (
        "route", 1000.0, 1000.0, 1500.0, 1000.0)
    slot = drill.hits[5]
    assert (slot.kind, slot.x_mil, slot.x2_mil, slot.y2_mil) == ("slot", 100.0, 200.0, 100.0)
    assert drill.hits[6].x_mil == approx(3000.0)
    assert drill.holes_by_tool() == {1: 3, 2: 2, 3: 2}
    assert drill.comments[:2] == ["Layer_Color=9474304", "FILE_FORMAT=2:5"]


def test_excellon_metric_and_leading_zeros_kept():
    metric = "M48\n;FILE_FORMAT=4:4\nMETRIC,TZ\nT1F00S00C0.3000\n%\nT01\nX254000Y-127000\nM30\n"
    drill = fo.parse_excellon(metric.encode(), "x.txt")
    assert drill.units == "mm"
    assert drill.tools[1].diameter_mil == approx(0.3 * MIL_PER_MM)
    assert (drill.hits[0].x_mil, drill.hits[0].y_mil) == (approx(1000.0), approx(-500.0))
    lz = "M48\n;FILE_FORMAT=2:4\nINCH,LZ\nT1C0.0100\n%\nT1\nX0125Y-005\nM30\n"
    drill = fo.parse_excellon(lz.encode(), "x.txt")
    assert drill.zeros == "LZ"
    assert (drill.hits[0].x_mil, drill.hits[0].y_mil) == (approx(1250.0), approx(-500.0))
    # all zeros kept: either reading gives the same value
    keep = "M48\n;FILE_FORMAT=2:5\nINCH\nT1C0.0100\n%\nT1\nX0100000Y0050000\nM30\n"
    drill = fo.parse_excellon(keep.encode(), "x.txt")
    assert (drill.zeros, drill.hits[0].x_mil, drill.hits[0].y_mil) == (None, 1000.0, 500.0)


def test_excellon_header_format_decimal_points_and_name_plating():
    text = "M48\nMETRIC,TZ,000.000\nT1C1.000\n%\nT1\nX1.5Y-2.25\nX001500Y002000\nM30\n"
    drill = fo.parse_excellon(text.encode(), "Demo-NonPlated.TXT")
    assert (drill.integer_digits, drill.decimals, drill.format_from) == (3, 3, "header")
    assert drill.tools[1].plated is False
    assert (drill.hits[0].x_mil, drill.hits[0].y_mil) == (approx(1.5 * MIL_PER_MM),
                                                          approx(-2.25 * MIL_PER_MM))
    assert drill.hits[1].x_mil == approx(1.5 * MIL_PER_MM)
    assert fo.parse_excellon(b"M48\nINCH\nT1C0.01\n%\n", "Demo-Plated.txt").tools[1].plated


def test_excellon_defaults_and_undefined_tools():
    text = "M48\nT1C0.0100\n%\nT1\nX10000Y20000\nT2\nX1Y1\nG91\nR4X100\nM30\n"
    drill = fo.parse_excellon(text.encode(), "x.drl")
    assert drill.units == "inch"
    assert (drill.integer_digits, drill.decimals, drill.format_from) == (2, 4, "default")
    assert drill.hits[0].x_mil == approx(1000.0)
    assert drill.tools[2].diameter_mil is None
    warnings = " | ".join(drill.warnings)
    assert "T2 used, not defined" in warnings
    assert "no number format stated" in warnings
    assert "G91" in warnings and "repeat code" in warnings
    assert "no INCH/METRIC line" in warnings
    assert fo.parse_excellon(b"M48\nM72\nT1C0.01\n%\nM71\n", "x").units == "mm"


# ---------------------------------------------------------------- .DRR and .LDP

DRR = """-------------------------------------------------------------------------------------------
NCDrill File Report For: Demo.PcbDoc     24/10/2026  10:11:12
-------------------------------------------------------------------------------------------

Layer Pair : Top Layer to Bottom Layer
ASCII RoundHoles File : Demo-RoundHoles.TXT
ASCII SlotHoles File : Demo-SlotHoles.TXT

Tool    Hole Size            Hole Tolerance        Hole Type  Hole Count  Plated  Tool Travel
-------------------------------------------------------------------------------------------
T1      12mil (0.3048mm)                           Round      10          PTH     1.23inch (31.24mm)
T2      0.8mm (31.496mil)    +/-1mil (0.0254mm)    Round      4           PTH     12.70mm (0.50inch)
T3      125mil (3.175mm)                           Slot       2           NPTH    0.10inch (2.54mm)
-------------------------------------------------------------------------------------------
Totals                                                        16

Layer Pair : L1 (Top) to L2 (Mid)
ASCII File : Demo-L1-L2.TXT
EIA File   : Demo-L1-L2.DRL

Tool    Hole Size            Hole Tolerance        Hole Type  Hole Count  Plated  Tool Travel
T1      4mil (0.1mm)                               Round      7           PTH     1.00inch (25.40mm)
Totals                                                        8

Total Processing Time (hh:mm:ss) : 00:00:01

-----------------------
Duplicate holes at same position : 2
1000,2000
-3000,4000
"""


def test_drill_report():
    report = fo.parse_drill_report(DRR.encode())
    assert (report.board, report.stamp) == ("Demo.PcbDoc", "24/10/2026  10:11:12")
    first, second = report.pairs
    assert (first.from_layer, first.to_layer) == ("Top Layer", "Bottom Layer")
    assert first.files == {"ASCII RoundHoles": "Demo-RoundHoles.TXT",
                           "ASCII SlotHoles": "Demo-SlotHoles.TXT"}
    t1, t2, t3 = first.tools
    assert (t1.tool, t1.hole_type, t1.count, t1.plated) == ("T1", "Round", 10, True)
    assert t1.size_mil == approx(0.3048 * MIL_PER_MM)   # the finer (mm) reading
    assert t1.size_step_mil == approx(0.0001 * MIL_PER_MM)
    assert t1.travel_mil == approx(1230.0)
    assert t2.size_mil == approx(31.496) and t2.tolerance == "+/-1mil (0.0254mm)"
    assert t2.travel_mil == approx(12.7 * MIL_PER_MM)
    assert (t3.hole_type, t3.plated, t3.size_mil) == ("Slot", False, approx(125.0))
    assert first.total == 16
    assert (second.from_layer, second.to_layer) == ("L1 (Top)", "L2 (Mid)")
    assert second.files == {"ASCII": "Demo-L1-L2.TXT", "EIA": "Demo-L1-L2.DRL"}
    assert second.tools[0].size_mil == approx(4.0)       # whole mils beat 0.1 mm
    assert second.tools[0].size_step_mil == 1.0
    assert report.processing_time == "00:00:01"
    assert report.hole_lists == [{"label": "Duplicate holes at same position", "count": 2,
                                  "points": [(1000.0, 2000.0), (-3000.0, 4000.0)]}]
    assert report.warnings == ["L1 (Top) to L2 (Mid): tool counts do not add up to the total"]


def test_drill_report_odd_lines_and_no_pair_line():
    report = fo.parse_drill_report(b"ASCII File : x.TXT\nT1 10mil (0.254mm) Round 3 PTH\n"
                                   b"something else\nTotals 3\n")
    assert len(report.pairs) == 1 and report.pairs[0].layer_pair is None
    assert report.pairs[0].tools[0].count == 3
    assert report.warnings == ["line 3: not read"]


def test_layer_pairs():
    text = ("Layer Pairs Export File for PCB: C:\\Projects\\Demo\\Demo.PcbDoc\r\n"
            "LayersSetName=Top-Bottom|DrillFile=Demo.TXT|DrillLayers=gtl,g1,g2,gbl\r\n"
            "LayersSetName=L1-L2|DrillFile=Demo-L1-L2.TXT|DrillLayers=gtl,g1\r\n"
            "junk\r\n")
    pairs = fo.parse_layer_pairs(text.encode())
    assert pairs.board.endswith("Demo.PcbDoc")
    assert [(p.name, p.drill_file, p.layers) for p in pairs.pairs] == [
        ("Top-Bottom", "Demo.TXT", ["gtl", "g1", "g2", "gbl"]),
        ("L1-L2", "Demo-L1-L2.TXT", ["gtl", "g1"])]
    assert pairs.pairs[0].values["DrillFile"] == "Demo.TXT"
    assert pairs.warnings == ["line 4: not read"]


# ---------------------------------------------------------------- detection

@pytest.mark.parametrize("name, head, kind", [
    ("Pick Place for Demo.txt", pnp_text(PNP_ROWS)[:2000], "pick_place"),
    ("x.csv", b"Designator,Ref X,Ref Y\n", "pick_place"),
    ("Demo.csv", b'"Comment","Designator","Quantity"\n', "bom"),
    ("Demo.xlsx", b"PK\x03\x04", "bom_workbook"),
    ("Demo.IPC", ipc356_file()[:500], "ipc356"),
    ("Demo.cvg", ipc2581()[:500], "ipc2581"),
    ("Demo.TXT", EXCELLON_INCH.encode()[:200], "excellon"),
    ("Demo.DRR", DRR.encode()[:300], "drill_report"),
    ("Demo.LDP", b"Layer Pairs Export File for PCB: x\n", "layer_pairs"),
    ("Demo.GTL", b"G04 x*\n%FSLAX25Y25*%\n%MOIN*%\n", "gerber"),
    ("Demo.GM13", b"", "gerber"),
    ("notes.txt", b"hello", None),
])
def test_detect_output_kind(name, head, kind):
    assert fo.detect_output_kind(name, head) == kind
