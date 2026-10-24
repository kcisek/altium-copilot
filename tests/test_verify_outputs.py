"""verify_outputs (services/verify_outputs.py and the MCP tool): the files Altium generates
checked against a synthetic design. Every output here is synthetic, written in the layouts
Altium uses (stage A's builders in test_fab_outputs_parsers): pick and place, BOM (.csv and
.xlsx), IPC-D-356A, IPC-2581, Excellon with its .DRR and .LDP, in folders, zips and an
OutJob's container folders; each check matching and mismatching."""

import io
import json
import os
import sys
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

import server.main as main_mod
from server.parsers import fab_outputs as fo
from server.parsers.pcb_doc import (
    PAD_SHAPE_RECT, PAD_SHAPE_ROUND, BoardInfo, PcbComponent, PcbData, PcbLayer, Primitive,
)
from server.parsers.prj_pcb import VariantDefinition, VariantState
from server.services import release_gate as rg
from server.services import verify_outputs as vo
from server.services.pcb_index import PcbIndex
from server.services.via_checks import layer_token_v6
from tests.test_fab_outputs_parsers import ipc_record, pnp_csv, pnp_text
from tests.test_output_jobs import _outjob, _output

MM = 1 / 0.0254
GND, A, B, C, LONG = range(5)
NETS = ["GND", "A", "B", "C", "A_VERY_LONG_NET_NAME"]
ORIGIN = (100.0, 50.0)
LAYERS = {"top layer": 1, "l2-gnd": 2, "l3-sig": 3, "bottom layer": 32, "top": 1,
          "bottom": 32}


# ---------------------------------------------------------------- the synthetic design

def _layer(v6, name, order, kind="copper", **fields):
    return PcbLayer(v6_id=v6, name=name, kind=kind, stack_order=order, **fields)


def _board_info():
    return BoardInfo(
        origin_x=ORIGIN[0], origin_y=ORIGIN[1], sheet_x=0.0, sheet_y=0.0, sheet_width=5000.0,
        sheet_height=5000.0, display_unit="mil", stackup_source="v9",
        layers=[
            _layer(1, "Top Layer", 0, copper_thick_mil=1.4),
            _layer(None, "Dielectric 1", 1, kind="dielectric", diel_height_mil=5.0,
                   diel_const=4.2, material="FR-4"),
            _layer(2, "L2-GND", 2, copper_thick_mil=0.7),
            _layer(None, "Dielectric 2", 3, kind="dielectric", diel_height_mil=40.0,
                   diel_const=4.5, material="FR-4"),
            _layer(3, "L3-SIG", 4, copper_thick_mil=0.7),
            _layer(None, "Dielectric 3", 5, kind="dielectric", diel_height_mil=5.0,
                   diel_const=4.2, material="FR-4"),
            _layer(32, "Bottom Layer", 6, copper_thick_mil=1.4),
        ],
    )


def _part(index, designator, x, y, rotation=0.0, side="top", footprint=None, comment=None,
          kind=0):
    return PcbComponent(index=index, refdes=designator, source_uid_tail=None,
                        channel_path=None, x=x, y=y, rotation=rotation, side=side,
                        footprint=footprint, designator=designator, comment=comment,
                        kind=kind)


def _smd(component, x, y, name, net, layer=1):
    return Primitive(kind="pad", layer=layer, net=net, component=component, x1=x, y1=y,
                     x2=x, y2=y, width=20.0, height=20.0, shape=PAD_SHAPE_RECT, hole=0.0,
                     pad_name=name)


def _tht(component, x, y, name, net, hole=35.0, plated=True, size=60.0):
    return Primitive(kind="pad", layer=74, net=net, component=component, x1=x, y1=y, x2=x,
                     y2=y, width=size, height=size, shape=PAD_SHAPE_ROUND, hole=hole,
                     pad_name=name, plated=plated)


def _via(net, x, y, hole, layer=1, layer_to=32):
    return Primitive(kind="via", layer=layer, layer_to=layer_to, net=net, component=None,
                     x1=x, y1=y, x2=x, y2=y, width=hole + 12.0, hole=hole)


def design_index(r1_rotation=90.0, netlist=None) -> PcbIndex:
    """R1 (top, two SMD pads, footprint with a space), C1 (bottom), J1 (two plated
    through-hole pads), MH1 (Standard (No BOM), a non-plated hole), LOGO1 (Graphical, no
    pads), a through via and a buried via (L2-GND to L3-SIG). Board origin at (100, 50)."""
    components = [
        _part(0, "R1", 1100, 1050, r1_rotation, footprint="SYN FP1", comment="10k"),
        _part(1, "C1", 1500, 1200, 180.0, side="bottom", footprint="CAP0402",
              comment="100n"),
        _part(2, "J1", 2000, 1000, footprint="HDR1X2", comment="HDR"),
        _part(3, "MH1", 3000, 3000, footprint="MH3", kind=5),
        _part(4, "LOGO1", 500, 500, kind=2),
        _part(5, "U12345678", 2500, 2000, footprint="SOT23", comment="REG"),
    ]
    primitives = [
        _smd(0, 1080, 1050, "1", A), _smd(0, 1120, 1050, "2", B),
        _smd(1, 1490, 1200, "1", B, layer=32), _smd(1, 1510, 1200, "2", GND, layer=32),
        _tht(2, 1950, 1000, "1", A), _tht(2, 2050, 1000, "2", GND),
        _tht(3, 3000, 3000, "1", None, hole=120.0, plated=False, size=120.0),
        _smd(5, 2480, 2000, "1", LONG), _smd(5, 2520, 2000, "12345", GND),
        _via(A, 1300, 1100, 12.0),
        _via(B, 1400, 1300, 8.0, layer=2, layer_to=3),
    ]
    pcb = PcbData(board=_board_info(), nets=list(NETS), components=components,
                  primitives=primitives, pours=[], layer_name_to_v6=dict(LAYERS))
    return PcbIndex(pcb, netlist)


def _component(kind=0, description=None, footprint=None, **params):
    return {"kind": kind, "params": params, "description": description,
            "footprint": footprint, "pins": {}}


NETLIST = {
    "components": {
        "R1": _component(description="Resistor", footprint="SYN FP1", comment="10k",
                         value="10k", **{"manufacturer 1": "Yageo",
                                         "manufacturer part number 1": "RC0603-10K"}),
        "C1": _component(description="Capacitor", footprint="CAP0402", comment="100n",
                         value="100n", **{"manufacturer 1": "Murata",
                                          "manufacturer part number 1": "GRM-100N"}),
        "J1": _component(description="Header", footprint="HDR1X2", comment="HDR"),
        "U12345678": _component(description="Regulator", footprint="SOT23", comment="REG"),
        "MH1": _component(kind=5, footprint="MH3"),
    },
    "pin_to_net": {},
    "nets": {},
}


def _variants():
    return VariantState([VariantDefinition(name="Assy", dnp_refdes=["C1"]),
                         VariantDefinition(name="Full")])


@pytest.fixture
def project(tmp_path):
    """A project folder with a PcbDoc and sheet stub (their times are the design's)."""
    folder = tmp_path / "Board"
    folder.mkdir()
    (folder / "Board.PcbDoc").write_bytes(b"pcb")
    (folder / "Board.SchDoc").write_bytes(b"sch")
    (folder / "Board.PrjPcb").write_text(
        "[Design]\nOutputPath=\n[Document1]\nDocumentPath=Board.SchDoc\n"
        "[Document2]\nDocumentPath=Board.PcbDoc\n", encoding="utf-8")
    old = 1_600_000_000
    os.utime(folder / "Board.PcbDoc", (old, old))
    os.utime(folder / "Board.SchDoc", (old, old))
    return {"name": "Board", "root_dir": str(folder),
            "prj_pcb_path": str(folder / "Board.PrjPcb"),
            "sheets": [{"name": "Board", "path": str(folder / "Board.SchDoc")}],
            "pcb_doc_paths": [str(folder / "Board.PcbDoc")], "parameters": {}}


def _run(project, path, index=None, netlist=NETLIST, variants=None, **kwargs):
    index = index or design_index(netlist=netlist)
    return vo.run_verify_outputs(project, index, netlist, variants or _variants(),
                                 path=str(path), **kwargs)


def _codes(model, check=None):
    return {(f["check"], f["code"]): f for f in model["findings"]
            if check is None or f["check"] == check}


def _write(folder: Path, name: str, data: bytes | str) -> Path:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


def _mm(mil: float) -> str:
    return f"{mil * 0.0254:.4f}"


# Center-X/Y of each part relative to the origin, in mil.
CENTRES = {"R1": (1000.0, 1000.0), "C1": (1400.0, 1150.0), "J1": (1900.0, 950.0),
           "U12345678": (2400.0, 1950.0)}


def pnp_rows(skip=(), **changes):
    """Pick-and-place rows of the design; changes[designator] overrides cells by index."""
    base = {
        "R1": ["R1", "10k", "TopLayer", "SYN_FP1", *map(_mm, CENTRES["R1"]), "90", "Resistor"],
        "C1": ["C1", "100n", "BottomLayer", "CAP0402", *map(_mm, CENTRES["C1"]), "180",
               "Capacitor"],
        "J1": ["J1", "HDR", "TopLayer", "HDR1X2", *map(_mm, CENTRES["J1"]), "0", "Header"],
        "U12345678": ["U12345678", "REG", "TopLayer", "SOT23", *map(_mm, CENTRES["U12345678"]),
                      "0", "Regulator"],
    }
    rows = []
    for designator, row in base.items():
        if designator in skip:
            continue
        row = list(row)
        for column, value in changes.get(designator, {}).items():
            row[column] = value
        rows.append(row)
    rows += changes.get("extra", [])
    return rows


# ---------------------------------------------------------------- pick and place

def test_pick_place_matching_the_design_both_variants(project, tmp_path):
    folder = tmp_path / "out"
    _write(folder, "Pick Place for Board.txt", pnp_text(pnp_rows()))
    _write(folder, "assy/Pick Place for Board.csv",
           pnp_csv(pnp_rows(skip=("C1",))).replace(b"Assembly A", b"Assy"))
    model = _run(project, folder)
    assert model["verdict"] == "consistent", model["findings"]
    assert model["results"]["pick_place"] == {"files": 2, "mismatches": 0, "status": "match"}
    rows = {row["file"]: row for row in model["files"]}
    base = rows["Pick Place for Board.txt"]
    assert (base["variant"], base["variant_from"], base["rows"]) == ("[No Variations]", "file", 4)
    assert rows["assy/Pick Place for Board.csv"]["variant"] == "Assy"
    # MH1 (Standard (No BOM)) and LOGO1 (Graphical) may be left out.
    assert base["expected"] == 4


def test_pick_place_mismatches(project, tmp_path):
    rows = pnp_rows(skip=("J1",), R1={6: "0", 2: "BottomLayer"}, C1={4: "36.5600"},
                    U12345678={3: "SYN-FP9"}, extra=[["X9", "x", "TopLayer", "x", "1", "1",
                                                     "0", "x"]])
    _write(tmp_path / "out", "pnp.txt", pnp_text(rows, variant="Assy"))
    model = _run(project, tmp_path / "out")
    found = _codes(model, "pick_place")
    assert model["verdict"] == "mismatches"
    assert found[("pick_place", "rotation")]["examples"] == [
        {"designator": "R1", "file": 0.0, "design": 90.0}]
    assert found[("pick_place", "side")]["examples"][0]["file"] == "bottom"
    assert found[("pick_place", "dnp_present")]["examples"][0]["designator"] == "C1"
    position = found[("pick_place", "position")]["examples"][0]
    assert (position["designator"], position["point"]) == ("C1", "center")
    assert position["off_mil"] == pytest.approx(1 * MM, abs=0.01)
    assert found[("pick_place", "extra")]["examples"][0]["designator"] == "X9"
    missing = found[("pick_place", "missing")]["examples"]
    assert [m["designator"] for m in missing] == ["J1"]
    assert missing[0]["at_mil"] == [1900.0, 950.0]
    assert found[("pick_place", "footprint")]["examples"][0]["file"] == "SYN-FP9"
    assert all(f["severity"] == "warning" for f in found.values())


def test_pick_place_padless_parts_sit_at_the_absolute_origin(project, tmp_path):
    good = pnp_rows(extra=[["LOGO1", "", "TopLayer", "", "-2.5400", "-1.2700", "0", ""]])
    _write(tmp_path / "a", "pnp.txt", pnp_text(good))
    assert _run(project, tmp_path / "a")["verdict"] == "consistent"
    bad = pnp_rows(extra=[["LOGO1", "", "TopLayer", "", "0.0000", "0.0000", "0", ""]])
    _write(tmp_path / "b", "pnp.txt", pnp_text(bad))
    (example,) = _codes(_run(project, tmp_path / "b"))[("pick_place", "position")]["examples"]
    assert example["note"] == "no pads: Altium writes the absolute origin"
    assert example["design_at_mil"] == [-100.0, -50.0]


def test_pick_place_rotation_whole_degrees_and_tolerances(project, tmp_path):
    index = design_index(r1_rotation=45.3, netlist=NETLIST)
    _write(tmp_path / "a", "pnp.txt", pnp_text(pnp_rows(R1={6: "45"})))
    model = _run(project, tmp_path / "a", index=index)
    assert model["verdict"] == "consistent"
    assert model["files"][0]["rotation_whole_degrees"] == 1
    _write(tmp_path / "b", "pnp.txt", pnp_text(pnp_rows(R1={6: "45.2", 4: "25.4500"})))
    index = design_index(r1_rotation=45.3, netlist=NETLIST)
    found = _codes(_run(project, tmp_path / "b", index=index))
    assert ("pick_place", "rotation") in found and ("pick_place", "position") in found
    index = design_index(r1_rotation=45.3, netlist=NETLIST)
    loose = vo.Tolerances(position_mil=0.1 * MM, rotation_deg=0.2)
    assert _run(project, tmp_path / "b", index=index, tolerances=loose)["verdict"] == \
        "consistent"


def test_pick_place_ref_point_and_bottom_y_flip(project, tmp_path):
    header = ["Designator", "Layer", "Ref-X(mm)", "Ref-Y(mm)", "Rotation"]
    rows = [["R1", "TopLayer", _mm(1000), _mm(1000), "90"],
            ["C1", "BottomLayer", _mm(1400), "-" + _mm(1150), "180"],  # Y-flip option
            ["J1", "TopLayer", _mm(1900), _mm(950), "0"],
            ["U12345678", "TopLayer", _mm(2400), _mm(1950), "0"]]
    _write(tmp_path / "out", "pnp.txt", pnp_text(rows, header=header))
    model = _run(project, tmp_path / "out")
    assert model["verdict"] == "consistent", model["findings"]
    assert model["files"][0]["y_flipped_bottom_rows"] == 1


def test_pick_place_comment_and_description_are_info(project, tmp_path):
    rows = pnp_rows(R1={1: "10K 1%", 7: "Thick film"})
    _write(tmp_path / "out", "pnp.txt", pnp_text(rows))
    model = _run(project, tmp_path / "out")
    found = _codes(model)
    assert found[("pick_place", "comment")]["severity"] == "info"
    assert found[("pick_place", "description")]["examples"][0]["design"] == "Resistor"
    assert model["verdict"] == "consistent"


def test_pick_place_from_another_revision_is_called_out(project, tmp_path):
    rows = pnp_rows(**{d: {4: "1.0000"} for d in CENTRES})
    rows += [["MH1", "", "TopLayer", "MH3", "1.0000", "1.0000", "0", ""]]
    rows += [[f"R{n}", "x", "TopLayer", "x", "1", "1", "0", "x"] for n in range(2, 5)]
    _write(tmp_path / "out", "pnp.txt", pnp_text(rows))
    found = _codes(_run(project, tmp_path / "out"))
    assert found[("pick_place", "layout_differs")]["count"] == 5


def test_pick_place_unknown_variant_and_variant_argument(project, tmp_path):
    _write(tmp_path / "out", "pnp.txt", pnp_text(pnp_rows(), variant="Gone"))
    found = _codes(_run(project, tmp_path / "out"))
    assert "is not one of the project's variants" in \
        found[("pick_place", "variant_unknown")]["message"]
    # variant= checks the file against that variant instead: C1 is Not Fitted in Assy.
    model = _run(project, tmp_path / "out", variant="Assy")
    assert ("pick_place", "dnp_present") in _codes(model)
    assert model["files"][0]["variant_from"] == "argument"
    assert "checked against 'Assy' as asked" in model["files"][0]["note"]


def test_pick_place_of_an_unknown_variant_may_leave_out_not_fitted_parts(project, tmp_path):
    # C1 is Not Fitted in Assy: with the file's variant unknown it may rightly be absent.
    _write(tmp_path / "out", "pnp.txt", pnp_text(pnp_rows(skip=("C1",)), variant="Gone"))
    model = _run(project, tmp_path / "out")
    found = _codes(model)
    assert ("pick_place", "variant_unknown") in found
    assert ("pick_place", "missing") not in found
    (row,) = model["files"]
    assert (row["expected"], row["absent_not_fitted_in_a_variant"]) == (3, 1)
    # A part every variant fits is still required.
    _write(tmp_path / "b", "pnp.txt", pnp_text(pnp_rows(skip=("J1",)), variant="Gone"))
    missing = _codes(_run(project, tmp_path / "b"))[("pick_place", "missing")]
    assert [example["designator"] for example in missing["examples"]] == ["J1"]


def test_pick_place_twins_and_copies_are_checked_once(project, tmp_path):
    folder = tmp_path / "out"
    _write(folder, "a/pnp.txt", pnp_text(pnp_rows()))
    _write(folder, "b/pnp.txt", pnp_text(pnp_rows()))     # an identical copy
    _write(folder, "a/pnp.csv", pnp_csv(pnp_rows()))       # same rows, CSV
    model = _run(project, folder)
    assert model["results"]["pick_place"]["files"] == 1
    assert model["identical_files_skipped"] == 2


# ---------------------------------------------------------------- BOM

BOM_HEADER = ["Comment", "Description", "Designator", "Footprint", "Manufacturer 1",
              "Manufacturer Part Number 1", "Quantity"]


def bom_csv(lines, title=(), header=BOM_HEADER) -> bytes:
    rows = [list(row) for row in title] + [header] + [list(line) for line in lines]
    return ("\r\n".join(",".join(f'"{cell}"' for cell in row) for row in rows) + "\r\n").encode()


BOM_ASSY = [
    ["10k", "Resistor", "R1", "SYN FP1", "Yageo", "RC0603-10K", "1"],
    ["HDR", "Header", "J1", "HDR1X2", "", "", "1"],
    ["REG", "Regulator", "U12345678", "SOT23", "", "", "1"],
]


def test_bom_matching_its_variant(project, tmp_path):
    _write(tmp_path / "out", "BOM.csv", bom_csv(BOM_ASSY, title=[["Variant:", "Assy"]]))
    model = _run(project, tmp_path / "out")
    assert model["verdict"] == "consistent", model["findings"]
    (row,) = model["files"]
    assert (row["variant"], row["variant_from"], row["designators"], row["expected"]) == (
        "Assy", "title", 3, 3)


def test_bom_mismatches(project, tmp_path):
    lines = [
        ["10k", "Resistor", "R1, J1", "SYN FP1", "Yageo Corp", "RC0603-10K-X", "3"],
        ["100n", "Capacitor", "C1", "CAP0402", "Murata", "GRM-100N", "1"],
        ["HDR", "Header", "J1, R99", "HDR1X2", "", "", "2"],
        ["Board", "Bare board", "", "", "", "", "1"],
        ["REG", "Regulator", "U12345678", "SOT23", "", "", "0"],
    ]
    _write(tmp_path / "out", "BOM.csv", bom_csv(lines, title=[["Variant:", "Assy"]]))
    found = _codes(_run(project, tmp_path / "out"))
    assert found[("bom", "quantity")]["examples"] == [
        {"row": 3, "quantity": 3.0, "designators": 2, "first": "R1"}]
    assert found[("bom", "dnp_listed")]["examples"][0]["designator"] == "C1"
    assert found[("bom", "extra")]["examples"][0]["designator"] == "R99"
    assert found[("bom", "duplicate")]["examples"][0]["designator"] == "J1"
    assert found[("bom", "no_designators")]["severity"] == "info"
    mpn = found[("bom", "field_mpn")]
    assert (mpn["severity"], mpn["examples"][0]["file"], mpn["examples"][0]["design"]) == (
        "warning", "RC0603-10K-X", "RC0603-10K")
    assert found[("bom", "field_manufacturer")]["severity"] == "info"
    # A zero quantity marks a Not Fitted line; U12345678 is fitted in Assy.
    assert found[("bom", "not_fitted_differs")]["examples"] == [
        {"designator": "U12345678", "design": "fitted"}]
    assert found[("bom", "missing")]["examples"] == [{"designator": "U12345678"}]


def test_bom_fitted_column_marks_not_fitted_lines(project, tmp_path):
    header = [*BOM_HEADER, "Fitted"]
    lines = [[*line, "Fitted"] for line in BOM_ASSY]
    lines.append(["100n", "Capacitor", "C1", "CAP0402", "Murata", "GRM-100N", "1", "Not Fitted"])
    _write(tmp_path / "out", "BOM.csv", bom_csv(lines, header=header,
                                                title=[["Variant:", "Assy"]]))
    model = _run(project, tmp_path / "out")
    assert model["verdict"] == "consistent", model["findings"]
    assert model["files"][0]["not_fitted_lines"] == 1


def test_bom_variant_from_outjob_file_name_and_best_fit(project, tmp_path):
    _write(tmp_path / "a", "BOM-Assy.csv", bom_csv(BOM_ASSY))
    (row,) = _run(project, tmp_path / "a")["files"]
    assert (row["variant"], row["variant_from"]) == ("Assy", "file_name")
    _write(tmp_path / "b", "BOM.csv", bom_csv(BOM_ASSY))
    (row,) = _run(project, tmp_path / "b")["files"]
    assert (row["variant"], row["variant_from"]) == ("Assy", "best_match")


def test_bom_column_shows_the_parameter_of_its_name(project, tmp_path):
    netlist = json.loads(json.dumps(NETLIST))
    netlist["components"]["R1"]["params"]["manufacturer part number"] = "OTHER-PN"
    _write(tmp_path / "out", "BOM.csv", bom_csv(BOM_ASSY, title=[["Variant:", "Assy"]]))
    model = _run(project, tmp_path / "out", netlist=netlist,
                 index=design_index(netlist=netlist))
    # get_bom's MPN is the lowest numbered ('Manufacturer Part Number', OTHER-PN); the
    # column 'Manufacturer Part Number 1' shows that parameter.
    assert model["verdict"] == "consistent", model["findings"]


def test_bom_column_values_resolve_special_strings_and_template_spelling(project, tmp_path):
    netlist = json.loads(json.dumps(NETLIST))
    params = netlist["components"]["R1"]["params"]
    params["value"] = "=comment"                      # shows 10k in a BOM
    params["manufacturer part number"] = "OTHER-PN"   # get_bom's MPN (lowest number)
    header = ["Comment", "Description", "Designator", "Footprint", "Manufacture 1",
              "Manufacture Part Number 1", "Quantity", "Value"]
    lines = [[*line, "10k" if line[2] == "R1" else ""] for line in BOM_ASSY]
    _write(tmp_path / "out", "BOM.csv", bom_csv(lines, header=header,
                                                title=[["Variant:", "Assy"]]))
    model = _run(project, tmp_path / "out", netlist=netlist,
                 index=design_index(netlist=netlist))
    # 'Manufacture Part Number 1' (the template's spelling) shows that parameter.
    assert model["verdict"] == "consistent", model["findings"]


def xlsx(rows, inline_row=None) -> bytes:
    """A minimal workbook: shared strings (one rich text), numbers, an inline string and a
    missing row, as Excel writes them."""
    strings, cells = [], []
    for r, row in enumerate(rows, start=1):
        if r == 2:
            continue  # a missing row
        refs = []
        for c, value in enumerate(row):
            ref = f"{chr(65 + c)}{r}"
            if value == "":
                continue
            if isinstance(value, (int, float)):
                refs.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                strings.append(value)
                refs.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
        cells.append(f'<row r="{r}">{"".join(refs)}</row>')
    if inline_row:
        cells.append(f'<row r="{len(rows) + 1}"><c r="A{len(rows) + 1}" t="inlineStr"><is><t>'
                     f'{inline_row}</t></is></c></row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    shared = "".join(f"<si><t>{s}</t></si>" if i else f"<si><r><t>{s[:2]}</t></r><r><t>"
                     f"{s[2:]}</t></r><rPh><t>x</t></rPh></si>" for i, s in enumerate(strings))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", f'<workbook {ns} {rel}><sheets><sheet name="BOM" '
                                            'sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels",
                         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                         'relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"'
                         ' Type="x"/></Relationships>')
        archive.writestr("xl/sharedStrings.xml", f"<sst {ns}>{shared}</sst>")
        archive.writestr("xl/worksheets/sheet1.xml",
                         f'<worksheet {ns}><sheetData>{"".join(cells)}</sheetData></worksheet>')
    return buffer.getvalue()


def test_xlsx_sheets_reader():
    data = xlsx([["Designator", "Quantity", "Value"], ["skipped"], ["R1", 1, 0.1],
                 ["C1", 2.0, 1e-3]], inline_row="note")
    ((name, rows),) = fo.xlsx_sheets(data)
    assert name == "BOM"
    assert rows == [["Designator", "Quantity", "Value"], [], ["R1", "1", "0.1"],
                    ["C1", "2", "0.001"], ["note"]]
    with pytest.raises(ValueError):
        fo.xlsx_sheets(b"not a zip")
    bom = fo.parse_bom_xlsx(data)
    assert [line.designators for line in bom.lines][:2] == [["R1"], ["C1"]]


def test_bom_workbooks_are_read_and_xls_is_reported(project, tmp_path):
    rows = [["Comment", "Designator", "Footprint", "Quantity"], ["unused"],
            ["10k", "R1", "SYN FP1", 1], ["HDR", "J1", "HDR1X2", 1],
            ["REG", "U12345678", "SOT23", 1]]
    _write(tmp_path / "out", "BOM.xlsx", xlsx(rows))
    _write(tmp_path / "out", "Old BOM.xls", b"\xd0\xcf\x11\xe0 BIFF")
    model = _run(project, tmp_path / "out", variant="Assy")
    assert model["results"]["bom"]["status"] == "match", model["findings"]
    xls = next(row for row in model["files"] if row["file"] == "Old BOM.xls")
    assert xls["status"] == "not_read" and ".xls (BIFF)" in xls["reason"]


def test_workbooks_without_a_bom_header_are_no_boms(project, tmp_path):
    _write(tmp_path / "out", "Notes.xlsx", xlsx([["Revision", "Date"], [], ["A", "2026"]]))
    _write(tmp_path / "out", "Notes2.xlsx",
           (tmp_path / "out" / "Notes.xlsx").read_bytes())
    _write(tmp_path / "out", "Old BOM.xls", b"\xd0\xcf\x11\xe0 BIFF")
    model = _run(project, tmp_path / "out")
    assert model["found"] == {}
    assert model["verdict"] == "no_outputs"
    assert model["results"]["bom"]["status"] == "no_files"
    notes = next(row for row in model["files"] if row["file"] == "Notes.xlsx")
    assert (notes["kind"], notes["reason"]) == ("workbook",
                                                "not a BOM: no sheet with a BOM header row")
    assert any(".xls (BIFF)" in row["reason"] for row in model["not_checked"])


def test_bom_without_a_netlist_uses_the_pcb_parts(project, tmp_path):
    _write(tmp_path / "out", "BOM.csv", bom_csv(BOM_ASSY, title=[["Variant:", "Assy"]]))
    model = _run(project, tmp_path / "out", netlist=None)
    assert model["verdict"] == "consistent", model["findings"]
    assert model["files"][0]["basis"] == "pcb"
    assert any("No netlist with schematic parts" in note for note in model["notes"])


def test_bom_designators_with_a_space():
    assert fo.split_designators("U1 A, U2 A") == (["U1 A", "U2 A"], 0)
    assert fo.split_designators("R1 R2, R3") == (["R1", "R2", "R3"], 0)


# ---------------------------------------------------------------- IPC-D-356A

def _at(xy):
    return int(round(xy[0] * 10)), int(round(xy[1] * 10))  # mil -> 0.0001 inch


def ipc356(records) -> bytes:
    lines = ["C  IPC-D-356A", "P  JOB   BOARD", "P  UNITS CUST", "P  VER   IPC-D-356A",
             *records, "999"]
    return ("\r\n".join(lines) + "\r\n").encode()


def design_records(**nets):
    """IPC-D-356A records of every pad of the design; nets[(designator, pin)] overrides."""
    pads = [
        ("327", "A", "R1", "1", (980, 1000)), ("327", "B", "R1", "2", (1020, 1000)),
        ("327", "B", "C1", "1", (1390, 1150)), ("327", "GND", "C1", "2", (1410, 1150)),
        ("317", "A", "J1", "1", (1850, 950)), ("317", "GND", "J1", "2", (1950, 950)),
        ("317", "N/C", "MH1", "1", (2900, 2950)),
        ("327", "A_VERY_LONG_NE", "U12345", "1", (2380, 1950)),
        ("327", "GND", "U12345", "1234", (2420, 1950)),
    ]
    records = []
    for code, net, refdes, pin, xy in pads:
        net = nets.get(f"{refdes}-{pin}", net)
        if net is None:
            continue
        x, y = _at(xy)
        hole = {"J1": 350, "MH1": 1200}.get(refdes) if code == "317" else None
        records.append(ipc_record(code, net, refdes, pin, hole=hole,
                                  plated="P" if code == "317" else "U", x=x, y=y))
    records.append(ipc_record("317", "A", "VIA", "", hole=120, plated="P", x=12000,
                              y=10500))
    return records


def test_ipc356_netlist_matches_the_pad_nets(project, tmp_path):
    _write(tmp_path / "out", "Board.ipc", ipc356(design_records()))
    model = _run(project, tmp_path / "out")
    assert model["verdict"] == "consistent", model["findings"]
    (row,) = model["files"]
    # Long designators, pins and net names are cut to the format's 6, 4 and 14 characters.
    assert (row["pins"], row["vias"], row["mismatches"]) == (9, 1, 0)


def test_ipc356_netlist_mismatches(project, tmp_path):
    records = design_records(**{"R1-2": "A", "C1-1": "B2", "J1-1": "N/C", "MH1-1": "C",
                                "C1-2": None})
    records.append(ipc_record("327", "B", "R7", "1", plated="U", x=100, y=100))
    _write(tmp_path / "out", "Board.ipc", ipc356(records))
    found = _codes(_run(project, tmp_path / "out"), "netlist")
    assert found[("netlist", "pins_missing")]["examples"] == [
        {"designator": "C1", "pin": "2", "design_net": "GND", "at_mil": [1410.0, 1150.0]}]
    assert found[("netlist", "pins_extra")]["examples"][0]["designator"] == "R7"
    assert found[("netlist", "pin_unconnected")]["examples"][0]["designator"] == "J1"
    assert found[("netlist", "pin_connected")]["examples"][0]["file_net"] == "C"
    merged = found[("netlist", "nets_merged")]["examples"][0]
    assert (merged["file_net"], set(merged["design_nets"])) == ("A", {"A", "B"})
    split = found[("netlist", "net_split")]["examples"][0]
    assert (split["design_net"], set(split["file_nets"])) == ("B", {"A", "B2"})


def test_split_and_merged_nets_list_their_pins(project, tmp_path):
    # U12345678 pin 12345 leaves GND for GND_B; R1-2 (design net B) joins net A.
    records = design_records(**{"U12345-1234": "GND_B", "R1-2": "A"})
    _write(tmp_path / "out", "Board.ipc", ipc356(records))
    found = _codes(_run(project, tmp_path / "out"), "netlist")
    split = next(example for example in found[("netlist", "net_split")]["examples"]
                 if example["design_net"] == "GND")
    assert split == {"design_net": "GND", "file_nets": {"GND": 2, "GND_B": 1}, "pins": [
        {"file_net": "GND_B", "designator": "U12345678", "pin": "12345",
         "at_mil": [2420.0, 1950.0]}]}
    merged = found[("netlist", "nets_merged")]["examples"][0]
    assert merged["file_net"] == "A" and merged["pins"] == [
        {"design_net": "B", "designator": "R1", "pin": "2", "at_mil": [1020.0, 1000.0]}]


def test_ipc356_renamed_net_is_info_and_another_board_is_called_out(project, tmp_path):
    records = design_records(**{"C1-2": "GROUND", "J1-2": "GROUND", "U12345-1234": "GROUND"})
    _write(tmp_path / "a", "Board.ipc", ipc356(records))
    model = _run(project, tmp_path / "a")
    found = _codes(model)
    # The first pins of the renamed net, with their place on the board.
    assert found[("netlist", "net_name")]["examples"] == [
        {"file_net": "GROUND", "design_net": "GND", "pins": [
            {"designator": "C1", "pin": "2", "at_mil": [1410.0, 1150.0]},
            {"designator": "J1", "pin": "2", "at_mil": [1950.0, 950.0]},
            {"designator": "U12345678", "pin": "12345", "at_mil": [2420.0, 1950.0]}]}]
    assert model["verdict"] == "consistent"
    others = [ipc_record("327", "X", f"Q{n}", "1", plated="U", x=n, y=n) for n in range(12)]
    _write(tmp_path / "b", "Other.ipc", ipc356(others))
    assert ("netlist", "netlist_differs") in _codes(_run(project, tmp_path / "b"))


# ---------------------------------------------------------------- IPC-2581

def ipc2581(copper="0.0356", rotation="90", populate_c1="false", masks=None) -> bytes:
    """masks: the layerFunction of a solder mask layer outside each outer copper layer."""
    def mm(mil):
        return f"{mil * 0.0254:.4f}"
    text = f"""<?xml version="1.0" encoding="UTF-8"?>
<IPC-2581 revision="C" xmlns="http://webstds.ipc.org/2581">
  <Bom name="B"><BomItem OEMDesignNumberRef="X" quantity="1">
    <RefDes name="R1" populate="true"/><RefDes name="C1" populate="{populate_c1}"/>
  </BomItem></Bom>
  <Ecad name="B"><CadHeader units="MILLIMETER">
    <Spec name="FR4"><General type="MATERIAL"><Property text="FR-4"/></General>
      <Dielectric type="DIELECTRIC_CONSTANT"><Property value="4.2"/></Dielectric></Spec>
  </CadHeader><CadData>
    <Layer name="Top Layer" layerFunction="SIGNAL" side="TOP"/>
    <Layer name="D1" layerFunction="DIELPREG" side="INTERNAL"/>
    <Layer name="L2-GND" layerFunction="PLANE" side="INTERNAL"/>
    <Layer name="D2" layerFunction="DIELCORE" side="INTERNAL"/>
    <Layer name="L3-SIG" layerFunction="SIGNAL" side="INTERNAL"/>
    <Layer name="D3" layerFunction="DIELPREG" side="INTERNAL"/>
    <Layer name="Bottom Layer" layerFunction="SIGNAL" side="BOTTOM"/>
    <Stackup name="S" overallThickness="1.4">
      <StackupLayer layerOrGroupRef="Top Layer" thickness="{copper}" sequence="1"/>
      <StackupLayer layerOrGroupRef="D1" thickness="{mm(5)}" sequence="2"><SpecRef id="FR4"/></StackupLayer>
      <StackupLayer layerOrGroupRef="L2-GND" thickness="{mm(0.7)}" sequence="3"/>
      <StackupLayer layerOrGroupRef="D2" thickness="{mm(40)}" sequence="4"/>
      <StackupLayer layerOrGroupRef="L3-SIG" thickness="{mm(0.7)}" sequence="5"/>
      <StackupLayer layerOrGroupRef="D3" thickness="{mm(5)}" sequence="6"/>
      <StackupLayer layerOrGroupRef="Bottom Layer" thickness="{mm(1.4)}" sequence="7"/>
    </Stackup>
    <Step name="B">
      <Component refDes="R1" layerRef="Top Layer"><Xform rotation="{rotation}"/><Location x="25.4" y="25.4"/></Component>
      <Component refDes="C1" layerRef="Bottom Layer"><Xform rotation="180" mirror="true"/><Location x="0" y="0"/></Component>
      <LogicalNet name="A"><PinRef componentRef="R1" pin="1"/><PinRef componentRef="J1" pin="1"/></LogicalNet>
      <LogicalNet name="B"><PinRef componentRef="R1" pin="2"/><PinRef componentRef="C1" pin="1"/></LogicalNet>
      <LogicalNet name="GND"><PinRef componentRef="C1" pin="2"/><PinRef componentRef="J1" pin="2"/><PinRef componentRef="U12345678" pin="12345"/></LogicalNet>
      <LogicalNet name="A_VERY_LONG_NET_NAME"><PinRef componentRef="U12345678" pin="1"/></LogicalNet>
    </Step>
  </CadData></Ecad>
</IPC-2581>
"""
    if masks:
        for side, sequence in (("Top", 0), ("Bottom", 8)):
            layer = f'<Layer name="{side} Solder" layerFunction="{masks}" side="{side.upper()}"/>'
            before = "<Layer " if side == "Top" else "    <Stackup "
            text = text.replace(before, layer + before.lstrip(), 1)
            text = text.replace("</Stackup>", f'<StackupLayer layerOrGroupRef="{side} Solder" '
                                f'thickness="{mm(0.4)}" sequence="{sequence}"/></Stackup>', 1)
    return text.encode()


def test_ipc2581_netlist_components_and_stackup_match(project, tmp_path):
    _write(tmp_path / "out", "Board.cvg", ipc2581())
    model = _run(project, tmp_path / "out", variant="Assy")
    assert model["verdict"] == "consistent", model["findings"]
    assert model["results"]["stackup"]["status"] == "match"
    assert model["results"]["netlist"]["status"] == "match"
    # No variant given and no OutJob output: the populate flags pick the variant (C1
    # unpopulated: Assy), not the design with no variations.
    model = _run(project, tmp_path / "out")
    assert model["verdict"] == "consistent", model["findings"]
    (row,) = model["files"]
    assert (row["populate_variant"], row["populate_variant_from"]) == ("Assy", "best_match")


def test_ipc2581_mismatches(project, tmp_path):
    _write(tmp_path / "out", "Board.cvg", ipc2581(copper="0.07", rotation="0",
                                                   populate_c1="true"))
    found = _codes(_run(project, tmp_path / "out", variant="Assy"))
    thickness = found[("stackup", "copper_thickness")]["examples"][0]
    assert (thickness["layer"], thickness["design_mil"]) == ("Top Layer", 1.4)
    assert found[("netlist", "component_rotation")]["examples"][0]["designator"] == "R1"
    assert found[("netlist", "component_populate")]["examples"] == [
        {"designator": "C1", "file_populate": True, "design_fitted": False}]


def _with_solder_masks(index: PcbIndex) -> PcbIndex:
    """The design's stack with a solder mask outside each outer copper layer, as the Layer
    Stack Manager keeps them (dielectric-like layers with a thickness)."""
    board = index.pcb.board
    board.layers.insert(0, _layer(None, "Top Solder", -1, kind="dielectric",
                                  diel_height_mil=0.4, diel_const=3.5))
    board.layers.append(_layer(None, "Bottom Solder", 7, kind="dielectric",
                               diel_height_mil=0.4, diel_const=3.5))
    return index


def test_ipc2581_stackup_leaves_solder_mask_out(project, tmp_path):
    # Solder masks sit outside the outer copper layers on both sides, however the file
    # labels them: only the dielectrics between the copper layers are compared.
    for function in ("SOLDERMASK", "DIELCORE"):
        _write(tmp_path / function, "Board.cvg", ipc2581(masks=function))
        index = _with_solder_masks(design_index(netlist=NETLIST))
        model = _run(project, tmp_path / function, index=index, variant="Assy")
        assert model["results"]["stackup"]["status"] == "match", (function, model["findings"])
    # A dielectric left out: the count differs; the others are paired by name only (none
    # here), not by position.
    text = ipc2581().replace(b'<StackupLayer layerOrGroupRef="D3" thickness="0.1270" '
                             b'sequence="6"/>', b"")
    _write(tmp_path / "short", "Board.cvg", text)
    found = _codes(_run(project, tmp_path / "short", variant="Assy"), "stackup")
    assert set(found) == {("stackup", "dielectric_count")}


# ---------------------------------------------------------------- NC drill

def _xy(mil):
    return f"X{round(mil[0] * 100)}Y{round(mil[1] * 100)}"


def excellon(sections, offset=(0.0, 0.0)) -> bytes:
    """INCH,TZ 2:5 Excellon: sections [(type, [(tool, inch size, [(x, y) mil relative])])]."""
    head = ["M48", ";FILE_FORMAT=2:5", "INCH,TZ"]
    body = []
    for kind, tools in sections:
        head.append(f";TYPE={kind}")
        for number, size, points in tools:
            head.append(f"T{number}F00S00C{size}")
            body.append(f"T{number:02d}")
            body += [_xy((x + offset[0], y + offset[1])) for x, y in points]
    return ("\r\n".join([*head, "%", *body, "M30"]) + "\r\n").encode()


THROUGH = [("PLATED", [(1, "0.0120", [(1200, 1050)]),
                       (2, "0.0350", [(1850, 950), (1950, 950)])]),
           ("NON_PLATED", [(3, "0.1200", [(2900, 2950)])])]
BURIED = [("PLATED", [(1, "0.0080", [(1300, 1250)])])]
LDP = ("Layer Pairs Export File for PCB: C:\\Work\\Board.PcbDoc\r\n"
       "LayersSetName=Top-Bottom|DrillFile=Board.TXT|DrillLayers=gtl,g1,g2,gbl\r\n"
       "LayersSetName=L2-L3|DrillFile=Board-L2-L3.TXT|DrillLayers=g1,g2\r\n")
DRR = """NCDrill File Report For: Board.PcbDoc     24/10/2026  10:11:12

Layer Pair : Top Layer to Bottom Layer
ASCII RoundHoles File : Board.TXT

Tool    Hole Size            Hole Tolerance        Hole Type  Hole Count  Plated  Tool Travel
T1      12mil (0.3048mm)                           Round      1           PTH     0.10inch (2.54mm)
T2      35mil (0.889mm)                            Round      {j1}           PTH     0.10inch (2.54mm)
T3      120mil (3.048mm)                           Round      1           NPTH    0.00inch (0.00mm)
Totals                                                        {total}
"""


def drill_folder(folder: Path, through=THROUGH, buried=BURIED, j1=2, report=True, ldp=True):
    _write(folder, "Board.TXT", excellon(through))
    if buried is not None:
        _write(folder, "Board-L2-L3.TXT", excellon(buried))
    if report:
        _write(folder, "Board.DRR", DRR.format(j1=j1, total=2 + j1))
    if ldp:
        _write(folder, "Board.LDP", LDP)


def test_drill_files_match_the_holes_per_layer_pair(project, tmp_path):
    drill_folder(tmp_path / "out")
    model = _run(project, tmp_path / "out")
    assert model["verdict"] == "consistent", model["findings"]
    drill = model["results"]["drill"]
    assert (drill["status"], drill["drill_sets"], drill["tools"], drill["holes"]) == (
        "match", 2, 4, 5)
    assert {p["pair"]: p["origin"] for p in drill["positions"]} == {
        "Top Layer - Bottom Layer": "relative", "L2-GND - L3-SIG": "relative"}
    assert model["results"]["stackup"]["status"] == "match"


def test_drill_mismatches(project, tmp_path):
    through = [("PLATED", [(1, "0.0130", [(1200, 1050)]),           # 13 mil: no such hole
                           (2, "0.0350", [(1850, 950), (1950, 990), (2500, 500)])]),
               ("NON_PLATED", [(3, "0.1200", [(2900, 2950)])])]
    drill_folder(tmp_path / "out", through=through, j1=3)
    found = _codes(_run(project, tmp_path / "out"), "drill")
    assert found[("drill", "tool_not_in_design")]["examples"][0]["tool"] == "T1"
    size = found[("drill", "size_missing")]["examples"][0]
    assert (size["size_mil"], size["plated"], size["design_holes"]) == (12.0, True, 1)
    count = found[("drill", "hole_count")]["examples"][0]
    assert (count["file_holes"], count["design_holes"]) == (3, 2)
    assert found[("drill", "hole_missing_at")]["examples"][0]["at_mil"] == [1950.0, 950.0]
    extra = [e["at_mil"] for e in found[("drill", "hole_extra_at")]["examples"]]
    assert [1950.0, 990.0] in extra and [2500.0, 500.0] in extra


def test_drill_report_against_its_file_and_alone(project, tmp_path):
    drill_folder(tmp_path / "a", j1=5)  # the report says 5 holes, the file drills 2
    found = _codes(_run(project, tmp_path / "a"), "drill")
    assert found[("drill", "report_differs")]["examples"] == [
        {"tool": "T2", "report_vs_file": {"count": [5, 2]}}]
    _write(tmp_path / "b", "Board.DRR", DRR.format(j1=2, total=4))
    model = _run(project, tmp_path / "b")
    assert model["results"]["drill"]["status"] == "match", model["findings"]
    _write(tmp_path / "c", "Board.DRR", DRR.format(j1=7, total=9))
    found = _codes(_run(project, tmp_path / "c"), "drill")
    assert found[("drill", "report_count")]["examples"][0]["report"] == 7


def test_drill_absolute_origin_and_files_off_the_board(project, tmp_path):
    drill_folder(tmp_path / "a", through=[(kind, tools) for kind, tools in THROUGH],
                 buried=None, report=False, ldp=False)
    absolute = tmp_path / "b"
    _write(absolute, "Board.TXT", excellon(THROUGH, offset=ORIGIN))
    model = _run(project, absolute)
    (positions,) = model["results"]["drill"]["positions"]
    assert positions["origin"] == "absolute"
    # Without a .DRR or .LDP the file is taken as the through holes: the buried via is
    # in no file, so nothing is missing there.
    assert model["verdict"] == "consistent", model["findings"]
    _write(tmp_path / "c", "Board.TXT", excellon(THROUGH, offset=(5000, 5000)))
    found = _codes(_run(project, tmp_path / "c"))
    assert found[("drill", "positions_do_not_fit")]["on_board_share"] == 0.0
    assert ("drill", "hole_missing_at") not in found


def test_drill_layer_pairs_naming_unknown_layers(project, tmp_path):
    drill_folder(tmp_path / "out", report=False, ldp=False)
    _write(tmp_path / "out", "Board.LDP", LDP.replace("g1,g2\r\n", "g7,g8\r\n"))
    model = _run(project, tmp_path / "out")
    found = _codes(model)
    assert found[("stackup", "layer_pair_layers")]["examples"] == [
        {"pair": "L2-L3", "unknown_layers": ["g7", "g8"]}]
    # Its drill file is not compared (not taken as the through holes either): the drill
    # check says so.
    assert "'L2-L3'" in found[("drill", "layer_pair_unknown")]["message"]
    assert model["results"]["drill"]["drill_sets"] == 1


def test_drill_sets_follow_their_reports(project, tmp_path):
    # Two complete outputs side by side, their tools numbered differently: each report is
    # compared with the drill file it names only.
    renumbered = [("PLATED", [(4, "0.0120", [(1200, 1050)]),
                              (5, "0.0350", [(1850, 950), (1950, 950)])]),
                  ("NON_PLATED", [(6, "0.1200", [(2900, 2950)])])]
    report = DRR.format(j1=2, total=4)
    _write(tmp_path / "a", "A.TXT", excellon(THROUGH))
    _write(tmp_path / "a", "A.DRR", report.replace("Board.TXT", "A.TXT"))
    _write(tmp_path / "a", "B.TXT", excellon(renumbered))
    _write(tmp_path / "a", "B.DRR", report.replace("Board.TXT", "B.TXT").replace(
        "T1  ", "T4  ").replace("T2  ", "T5  ").replace("T3  ", "T6  "))
    model = _run(project, tmp_path / "a")
    assert model["verdict"] == "consistent", model["findings"]
    assert model["results"]["drill"]["drill_sets"] == 2
    # Identical outputs in two folders of one generation: compared once.
    drill_folder(tmp_path / "b" / "Release" / "NC Drill")
    drill_folder(tmp_path / "b" / "Release" / "Copy")
    model = _run(project, tmp_path / "b")
    assert model["verdict"] == "consistent", model["findings"]
    assert model["results"]["drill"]["drill_sets"] == 2
    # A report whose drill file is not there, beside a folder with a good set: compared
    # with the board (5 holes reported where the board has 2), not with the other file.
    drill_folder(tmp_path / "c" / "Release" / "A")
    _write(tmp_path / "c" / "Release" / "B", "Other.DRR",
           DRR.format(j1=5, total=7).replace("Board.TXT", "Other.TXT"))
    found = _codes(_run(project, tmp_path / "c"), "drill")
    assert set(found) == {("drill", "report_count")}
    assert found[("drill", "report_count")]["file"] == "Release/B/Other.DRR"
    # A file no report names, identical to one a report names: not compared again.
    drill_folder(tmp_path / "d" / "x" / "NC Drill", ldp=False)
    _write(tmp_path / "d" / "x" / "Loose", "Board.TXT", excellon(THROUGH))
    model = _run(project, tmp_path / "d")
    assert model["results"]["drill"]["drill_sets"] == 2, model["results"]["drill"]


def test_drill_files_of_another_board(project, tmp_path):
    # A report naming a board the project does not list: a warning, nothing compared.
    folder = tmp_path / "a"
    _write(folder, "Board.TXT", excellon(THROUGH, offset=(5000, 5000)))
    _write(folder, "Board.DRR", DRR.format(j1=2, total=4).replace("Board.PcbDoc",
                                                                     "Old Board.PcbDoc"))
    model = _run(project, folder)
    found = _codes(model)
    assert set(found) == {("drill", "other_board")}
    assert found[("drill", "other_board")]["severity"] == "warning"
    assert "'Old Board.PcbDoc'" in found[("drill", "other_board")]["message"]
    assert found[("drill", "other_board")]["examples"] == [{"file": "Board.DRR"}]
    assert model["results"]["drill"]["status"] == "mismatch"
    # A layer-pairs file for the project's other PcbDoc: info, its files not compared.
    two = {**project, "pcb_doc_paths": [*project["pcb_doc_paths"],
                                        str(Path(project["root_dir"]) / "Panel.PcbDoc")]}
    _write(tmp_path / "b", "Panel.TXT", excellon(THROUGH, offset=(5000, 5000)))
    _write(tmp_path / "b", "Panel.LDP", LDP.replace("Board.PcbDoc", "Panel.PcbDoc").replace(
        "DrillFile=Board.TXT", "DrillFile=Panel.TXT"))
    model = _run(two, tmp_path / "b")
    found = _codes(model)
    assert set(found) == {("drill", "other_board")}
    assert found[("drill", "other_board")]["severity"] == "info"
    assert model["verdict"] == "consistent"
    assert vo.board_named("Layer Pairs Export File for PCB: C:\\A\\B.PcbDoc") == "B.PcbDoc"
    assert vo.board_named("Board.PcbDoc     24/10/2026  10:11:12") == "Board.PcbDoc"


def test_layer_pairs_in_layer_id_order(project, tmp_path):
    # L3-SIG (MID2) above L2-GND (MID1): the .LDP still lists g1 before g2 (layer-ID order).
    index = design_index(netlist=NETLIST)
    layers = {layer.name: layer for layer in index.pcb.board.layers}
    layers["L2-GND"].stack_order, layers["L3-SIG"].stack_order = 4, 2
    index = PcbIndex(index.pcb, NETLIST)
    drill_folder(tmp_path / "out", report=False)
    model = _run(project, tmp_path / "out", index=index)
    assert model["verdict"] == "consistent", model["findings"]
    assert model["results"]["drill"]["drill_sets"] == 2
    design = vo.Design({"name": "B"}, index, None, _variants())
    # The span is the outermost of the layers named, not the first and the last word.
    assert design.layer_span(["gtl", "g1", "g2"]) == (0, 2)
    assert design.layer_span(["g1", "g2"]) == (1, 2)
    assert design.layer_span(["gtl", "g7"]) is None


def test_coincident_holes_are_drilled_once(project, tmp_path):
    index = design_index(netlist=NETLIST)
    index.pcb.primitives.append(replace(index.pcb.primitives[4]))  # J1-1 stacked twice
    index = PcbIndex(index.pcb, NETLIST)
    drill_folder(tmp_path / "out")
    model = _run(project, tmp_path / "out", index=index)
    assert model["results"]["drill"]["coincident_holes_merged"] == 1
    assert model["results"]["drill"]["status"] == "match", model["findings"]


def test_layer_tokens_and_gerber_extensions():
    index = design_index()
    assert [layer_token_v6(index, t) for t in ("TOP", "MID2", "BOTTOM", "PLANE1", "nope")] == [
        1, 3, 32, 39, None]
    assert [vo.gerber_layer_v6(t) for t in ("gtl", "GBL", "g1", "gp2", "gto", None)] == [
        1, 32, 2, 40, None, None]
    design = vo.Design({"name": "B"}, index, None, _variants())
    assert [design.layer_position(t) for t in ("gtl", "g1", "MID2", "Bottom Layer", "x")] == [
        0, 1, 2, 3, None]
    drill = fo.parse_excellon(excellon(THROUGH))
    assert drill.tools[1].diameter_step_mil == pytest.approx(0.1)


# ---------------------------------------------------------------- zips, staleness, files

def test_release_zip_read_in_memory_with_a_nested_zip(project, tmp_path):
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as archive:
        archive.writestr("NC Drill/Board.TXT", excellon(THROUGH))
        archive.writestr("NC Drill/Board.LDP", LDP)
        archive.writestr("NC Drill/Board-L2-L3.TXT", excellon(BURIED))
    outer = tmp_path / "Release.zip"
    with zipfile.ZipFile(outer, "w") as archive:
        archive.writestr("Pick Place/pnp.txt", pnp_text(pnp_rows()))
        archive.writestr("Fab.zip", inner.getvalue())
        archive.writestr("Gerber/Board.GTL", "%FSLAX24Y24*%")
    model = _run(project, outer)
    assert model["verdict"] == "consistent", model["findings"]
    assert model["found"] == {"Gerber": 1, "NC drill": 3, "pick-and-place": 1}
    labels = {row["file"] for row in model["files"]}
    assert "Release.zip/Fab.zip/NC Drill/Board.TXT" in labels
    assert any(row["check"] == "gerber" for row in model["not_checked"])
    assert not list(tmp_path.glob("**/pnp.txt"))  # nothing extracted


def test_stale_files_are_info(project, tmp_path):
    path = _write(tmp_path / "out", "pnp.txt", pnp_text(pnp_rows()))
    os.utime(path, (1_500_000_000, 1_500_000_000))
    model = _run(project, tmp_path / "out")
    stale = _codes(model)[("staleness", "stale")]
    assert stale["severity"] == "info"
    # The PcbDoc, and the .PrjPcb where the variants a pick-and-place depends on live.
    assert {row["document"] for row in stale["examples"][0]["saved_since"]} == {
        "Board.PcbDoc", "Board.PrjPcb"}
    assert model["results"]["staleness"]["status"] == "stale"
    assert model["verdict"] == "consistent"
    # Older than the .PrjPcb only (a variant changed since): stale too.
    os.utime(path, (1_650_000_000, 1_650_000_000))
    model = _run(project, tmp_path / "out")
    (row,) = _codes(model)[("staleness", "stale")]["examples"][0]["saved_since"]
    assert row["document"] == "Board.PrjPcb"
    # Without variants the .PrjPcb is not a source.
    model = _run(project, tmp_path / "out", variants=VariantState([]))
    assert model["results"]["staleness"]["status"] == "current"


def test_errors_and_empty_folders(project, tmp_path):
    assert _run(project, tmp_path / "nowhere")["error"] == "path_not_found"
    assert _run(project, tmp_path, checks=["bogus"])["error"] == "invalid_checks"
    assert _run(project, tmp_path, variant="Nope")["error"] == "variant_not_found"
    (tmp_path / "empty").mkdir()
    model = _run(project, tmp_path / "empty")
    assert model["verdict"] == "no_outputs"
    assert ("files", "no_outputs_found") in _codes(model)


def test_checks_narrow_and_no_board(project, tmp_path):
    drill_folder(tmp_path / "out")
    _write(tmp_path / "out", "BOM.csv", bom_csv(BOM_ASSY, title=[["Variant:", "Assy"]]))
    model = _run(project, tmp_path / "out", checks=["bom"])
    assert model["checks_run"] == ["bom"] and list(model["results"]) == ["bom"]
    model = vo.run_verify_outputs(project, None, NETLIST, _variants(),
                                  path=str(tmp_path / "out"))
    assert model["results"]["bom"]["status"] == "match"
    assert model["results"]["drill"]["status"] == "not_checked"


# ---------------------------------------------------------------- project mode (OutJob)

def _outjob_project(project, outputs, publish):
    folder = Path(project["root_dir"])
    (folder / "Board.OutJob").write_text(_outjob(outputs, publish=publish), encoding="utf-8")
    (folder / "Board.PrjPcb").write_text(
        "[Design]\nOutputPath=\n[Document1]\nDocumentPath=Board.SchDoc\n"
        "[Document2]\nDocumentPath=Board.PcbDoc\n[Document3]\nDocumentPath=Board.OutJob\n",
        encoding="utf-8")
    return folder


def test_project_mode_finds_the_container_folders(project):
    publish = "ReleaseManaged3=1\nOutputPathMedia3=[Media Name]\nOutputPathOutputer3=[Output Type]\n"
    folder = _outjob_project(project, [
        _output("Pick Place", "Pick Place Files", category="Assembly", variant="Assy"),
        _output("NC Drill", "NC Drill Files"),
        _output("BOM_PartType", "Bill of Materials", category="Report"),
    ], publish)
    out = folder / "Project Outputs for Board" / "Folder"
    _write(out, "Pick Place/pnp.txt", pnp_text(pnp_rows(skip=("C1",)), variant="Assy"))
    model = vo.run_verify_outputs(project, design_index(netlist=NETLIST), NETLIST, _variants())
    assert model["source"]["mode"] == "project"
    assert model["source"]["searched"][0] == "Project Outputs for Board/Folder"
    (row,) = [r for r in model["files"] if r["kind"] == "pick_place"]
    assert row["outjob_output"] == ["Board.OutJob: Pick Place Files"]
    assert row["file"] == "Project Outputs for Board/Folder/Pick Place/pnp.txt"
    missing = _codes(model)[("files", "output_not_found")]
    assert {e["output"] for e in missing["examples"]} == {"NC Drill Files", "Bill of Materials"}
    assert missing["severity"] == "info"


def test_project_mode_checks_the_newest_generation(project):
    publish = ("ReleaseManaged3=1\nOutputPathMedia3==CurrentDate+'_'+CurrentTime\n"
               "OutputPathOutputer3=[Output Type]\n")
    folder = _outjob_project(project, [
        _output("Pick Place", "Pick Place Files", category="Assembly")], publish)
    base = folder / "Project Outputs for Board"
    old = _write(base, "2026-01-01 10-00/Pick Place/pnp.txt",
                 pnp_text(pnp_rows(R1={6: "0"})))
    os.utime(old, (1_700_000_000, 1_700_000_000))
    _write(base, "2026-02-01 10-00/Pick Place/pnp.txt", pnp_text(pnp_rows()))
    model = vo.run_verify_outputs(project, design_index(netlist=NETLIST), NETLIST, _variants())
    assert model["older_generations_skipped"] == {"pick-and-place": 1}
    assert model["verdict"] == "consistent", model["findings"]
    (row,) = model["files"]
    assert row["file"].startswith("Project Outputs for Board/2026-02-01")


def test_project_mode_orders_generations_by_their_folder_names(project):
    publish = ("ReleaseManaged3=1\nOutputPathMedia3==CurrentDate+'_'+CurrentTime\n"
               "OutputPathOutputer3=[Output Type]\n")
    folder = _outjob_project(project, [
        _output("Pick Place", "Pick Place Files", category="Assembly")], publish)
    base = folder / "Project Outputs for Board"
    # A copy or checkout reset the times: the older generation's file is the newer one.
    good = _write(base, "2026-10-24 10-00-00/Pick Place/pnp.txt", pnp_text(pnp_rows()))
    os.utime(good, (1_700_000_000, 1_700_000_000))
    _write(base, "2026-10-01 10-00-00/Pick Place/pnp.txt", pnp_text(pnp_rows(R1={6: "0"})))
    model = vo.run_verify_outputs(project, design_index(netlist=NETLIST), NETLIST, _variants())
    assert model["verdict"] == "consistent", model["findings"]
    assert model["generations_checked"] == [{
        "kind": "pick-and-place", "folder": "Project Outputs for Board/2026-10-24 10-00-00",
        "newest_by": "folder name"}]


def test_generation_folder_names():
    keys = vo._generation_keys(["24.10.2026 10-00", "01.11.2026 09-00", "Pick Place"])
    assert keys["01.11.2026 09-00"] > keys["24.10.2026 10-00"]  # day first (24 > 12)
    assert "Pick Place" not in keys
    keys = vo._generation_keys(["10-24-2026", "11-01-2026"])  # month first
    assert keys["11-01-2026"] > keys["10-24-2026"]
    assert vo._generation_keys(["01-02-2026", "02-01-2026"]) == {}  # cannot tell
    keys = vo._generation_keys(["20261024_101500", "20261101_090000"])
    assert keys["20261101_090000"] > keys["20261024_101500"]


def test_project_mode_test_points_for_assembly_make_an_ipc_netlist(project):
    folder = _outjob_project(project, [
        _output("Test Points For Assembly", "Test Point Report", category="Assembly")],
        "ReleaseManaged3=1\nOutputPathMedia3=[Media Name]\nOutputPathOutputer3=[Output Type]\n")
    _write(folder / "Project Outputs for Board" / "Folder", "Test Points/Board.ipc",
           ipc356(design_records()))
    model = vo.run_verify_outputs(project, design_index(netlist=NETLIST), NETLIST, _variants())
    (row,) = model["files"]
    assert row["outjob_output"] == ["Board.OutJob: Test Point Report"]
    assert ("files", "output_not_found") not in _codes(model)


def test_project_mode_bom_on_another_variant_than_its_outjob(project):
    publish = "ReleaseManaged3=1\nOutputPathMedia3=[Media Name]\nOutputPathOutputer3=[Output Type]\n"
    folder = _outjob_project(project, [
        _output("BOM_PartType", "Bill of Materials", category="Report", variant="Full")],
        publish)
    # The OutJob says Full (C1 fitted); the file's parts are exactly Assy's.
    _write(folder / "Project Outputs for Board" / "Folder", "BOM/BOM.csv", bom_csv(BOM_ASSY))
    model = vo.run_verify_outputs(project, design_index(netlist=NETLIST), NETLIST, _variants())
    assert model["verdict"] == "consistent", model["findings"]
    differs = _codes(model)[("bom", "variant_differs_from_outjob")]
    assert differs["severity"] == "info" and "'Assy'" in differs["message"]
    (row,) = model["files"]
    assert (row["variant"], row["variant_from"]) == ("Assy", "best_match")


def test_bom_from_an_activebom_document_leaves_part_numbers_unverified(project, tmp_path):
    publish = "ReleaseManaged3=1\nOutputPathMedia3=[Media Name]\nOutputPathOutputer3=[Output Type]\n"
    folder = _outjob_project(project, [
        _output("BOM_PartType", "Bill of Materials", category="Report", variant="Assy",
                doc="Board.BomDoc")], publish)
    lines = [["10k", "Resistor", "R1", "SYN FP1", "Yageo", "RC0603-10K-CHOSEN", "1"],
             *BOM_ASSY[1:]]
    _write(folder / "Project Outputs for Board" / "Folder", "BOM/BOM.csv", bom_csv(lines))
    model = vo.run_verify_outputs(project, design_index(netlist=NETLIST), NETLIST, _variants())
    mpn = _codes(model)[("bom", "field_mpn")]
    assert mpn["severity"] == "info" and "ActiveBOM" in mpn["message"]
    assert model["verdict"] == "consistent"
    # The same file given by path: the project's BOM outputs all use the BomDoc.
    _write(tmp_path / "release", "BOM.csv", bom_csv(lines))
    model = _run(project, tmp_path / "release")
    assert _codes(model)[("bom", "field_mpn")]["severity"] == "info"
    assert model["files"][0]["part_choices_not_read"] is True


def test_project_mode_flags_a_file_on_another_variant_than_its_outjob(project):
    publish = "ReleaseManaged3=1\nOutputPathMedia3=[Media Name]\nOutputPathOutputer3=[Output Type]\n"
    folder = _outjob_project(project, [
        _output("Pick Place", "Pick Place Files", category="Assembly", variant="Assy")], publish)
    _write(folder / "Project Outputs for Board" / "Folder", "Pick Place/pnp.txt",
           pnp_text(pnp_rows()))
    model = vo.run_verify_outputs(project, design_index(netlist=NETLIST), NETLIST, _variants())
    assert ("pick_place", "variant_differs_from_outjob") in _codes(model)


# ---------------------------------------------------------------- the MCP tool

def _service():
    """The services.verify_outputs module main.py imported (by either package path)."""
    return sys.modules[main_mod.run_verify_outputs.__module__]


@pytest.fixture
def loaded(monkeypatch, project):
    session = main_mod.PcbSession()
    session._load = lambda path, netlist: design_index(netlist=netlist)
    monkeypatch.setattr(main_mod, "_project", project)
    monkeypatch.setattr(main_mod._altium, "_netlist", NETLIST)
    monkeypatch.setattr(main_mod, "_variant_state", _variants())
    monkeypatch.setattr(main_mod, "_pcb_session", session)
    return project


def _call(**arguments) -> dict:
    return json.loads(main_mod.verify_outputs(**arguments))


def test_tool_units_paging_and_header(loaded, tmp_path):
    rows = pnp_rows(skip=("J1",), R1={6: "0", 4: "30.0000"}, extra=[
        ["X1", "x", "TopLayer", "x", "1", "1", "0", "x"]])
    _write(tmp_path / "out", "pnp.txt", pnp_text(rows))
    result = _call(path=str(tmp_path / "out"))
    assert (result["project"], result["pcb_doc"], result["units"]) == ("Board", "Board.PcbDoc",
                                                                       "mil")
    assert result["active_variant"] == "Assy"
    assert result["tolerances"]["position_mil"] == pytest.approx(0.3937, abs=1e-4)
    total = result["findings_total"]
    assert total >= 4 and result["has_more"] is False
    mm = _call(path=str(tmp_path / "out"), units="mm")
    assert mm["tolerances"]["position_mm"] == pytest.approx(0.01)
    position = next(f for f in mm["findings"] if f["code"] == "position")
    assert position["examples"][0]["file_at_mm"] == [30.0, 25.4]
    second = _call(path=str(tmp_path / "out"), offset=2)
    assert second["offset"] == 2 and len(second["findings"]) == total - 2


def test_tool_arguments_and_errors(loaded, tmp_path, monkeypatch):
    assert _call(position_tolerance=-1)["error"] == "invalid_tolerance"
    assert _call(rotation_tolerance=500)["error"] == "invalid_tolerance"
    assert _call(units="inch")["error"] == "invalid_units"
    assert _call(variant="Nope")["error"] == "variant_not_found"
    assert _call(path=str(tmp_path / "missing"))["error"] == "path_not_found"
    _write(tmp_path / "out", "pnp.txt", pnp_text(pnp_rows(R1={4: "25.4100"})))
    # 0.01 mm off: outside 0.005 mm, inside 0.02 mm (in the board's mil: 0.79 mil)
    assert _call(path=str(tmp_path / "out"), position_tolerance=0.2)["verdict"] == "mismatches"
    assert _call(path=str(tmp_path / "out"), position_tolerance=0.8)["verdict"] == "consistent"
    monkeypatch.setattr(main_mod, "_project", None)
    assert _call()["error"] == "no_project"


def test_tool_shrinks_to_fit(loaded, tmp_path, monkeypatch):
    rows = pnp_rows(extra=[[f"X{n}", "x", "TopLayer", "x", "1", "1", "0", "x" * 50]
                           for n in range(200)])
    _write(tmp_path / "out", "pnp.txt", pnp_text(rows))
    full = main_mod.verify_outputs(path=str(tmp_path / "out"))
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", len(full) - 1)
    result = _call(path=str(tmp_path / "out"))
    assert result["findings_limited_to"] < 60 or result["examples_per_finding"] < 5
    assert len(json.dumps(result, indent=2)) < len(full)
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 1500)
    brief = _call(path=str(tmp_path / "out"))
    assert "findings_omitted" in brief and brief["verdict"] == "mismatches"


def test_tool_caches_the_model_for_paging(loaded, tmp_path, monkeypatch):
    _write(tmp_path / "out", "pnp.txt", pnp_text(pnp_rows(R1={6: "0"})))
    calls = []
    service = _service()
    original = service._run
    monkeypatch.setattr(service, "_run", lambda *a, **k: calls.append(1) or original(*a, **k))
    _call(path=str(tmp_path / "out"))
    _call(path=str(tmp_path / "out"), offset=1)
    assert len(calls) == 1
    _write(tmp_path / "out", "pnp2.txt", pnp_text(pnp_rows(R1={6: "180"})))
    _call(path=str(tmp_path / "out"))
    assert len(calls) == 2


def test_tool_is_registered_with_metadata():
    tool = main_mod.mcp._tool_manager.get_tool("verify_outputs")
    assert tool.annotations.readOnlyHint is True
    assert "verify_outputs" in main_mod.SERVER_INSTRUCTIONS
    assert main_mod.SERVER_INSTRUCTIONS.index("verify_outputs") > 4096
    manifest = json.loads((Path(__file__).parents[1] / "manifest.json").read_text("utf-8"))
    assert any(entry["name"] == "verify_outputs" for entry in manifest["tools"])


# ---------------------------------------------------------------- release readiness

def _release_ctx(project, model):
    inputs = rg.ReleaseInputs(
        project=project, netlist=None, variant_state=_variants(), notes=None,
        pcb_index=lambda: (None, {"message": "x"}), saved_after_netlist=lambda p, c: ([], []),
        sheets_saved_after=lambda p, s: [], dfm_variant=lambda: None,
        fab_board_reader=lambda path: (None, "x"), verify_outputs=lambda: model)
    return rg.ReleaseContext(inputs)


def _gate(gate_id):
    return next(gate for gate in rg.GATES if gate.id == gate_id)


def test_release_gates_cite_verify_outputs(project, tmp_path):
    outjobs = {"outjobs": [{"file": "Board.OutJob", "listed": True}], "findings": [],
               "details": [{"file": "Board.OutJob", "containers": [
                   {"generated": {"newest_output_utc": "2026-10-01T00:00:00Z"}}],
                   "outputs_by_category": {"Assembly": [
                       {"state": "connected", "type": "Pick Place", "output": "P",
                        "variant": "Assy"}]}}]}
    clean = {"verdict": "consistent", "severity_counts": {"warning": 0, "info": 0},
             "results": {"pick_place": {"status": "match"}}, "findings": []}
    ctx = _release_ctx(project, clean)
    ctx.outjobs = outjobs
    row = rg.run_gate(_gate("outputs_current"), ctx)
    assert row["status"] == "pass"
    assert "the pick-and-place files match the design (verify_outputs)" in row["evidence"]
    # Files found but none compared (Gerber only): nothing is claimed about their content.
    gerber_only = {**clean, "results": {"drill": {"status": "no_files"}}}
    ctx = _release_ctx(project, gerber_only)
    ctx.outjobs = outjobs
    row = rg.run_gate(_gate("outputs_current"), ctx)
    assert row["status"] == "pass" and "verify_outputs" not in row["evidence"]
    stale = {"verdict": "mismatches", "severity_counts": {"warning": 2, "info": 0},
             "results": {"pick_place": {"status": "mismatch"}},
             "findings": [{"check": "pick_place", "code": "dnp_present", "count": 1}]}
    ctx = _release_ctx(project, stale)
    ctx.outjobs = outjobs
    row = rg.run_gate(_gate("outputs_current"), ctx)
    assert row["status"] == "warn" and "in pick-and-place (verify_outputs)" in row["evidence"]
    row = rg.run_gate(_gate("dnp_parts"), ctx)
    assert row["status"] == "warn" and "Not Fitted parts as placed (1 listing" in row["evidence"]
    # Both gates stay advisory: the release verdict cannot change through them.
    assert not _gate("outputs_current").blocking and not _gate("dnp_parts").blocking
    # A failing verify_outputs never stops a gate.
    ctx = _release_ctx(project, None)
    ctx.inputs.verify_outputs = lambda: 1 / 0
    ctx.outjobs = outjobs
    assert rg.run_gate(_gate("outputs_current"), ctx)["status"] == "pass"
    assert "verify_outputs" in rg.RELEASE_REVIEW_TOOLS
