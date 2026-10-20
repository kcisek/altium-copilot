"""Offline PCB Health Check (services/pcb_health, check_pcb_health) and the PcbDoc
fields it reads: region shapes, fills, board regions, pour and outline arcs, settings."""

import json
import shutil
import struct
from pathlib import Path

import pytest

import server.main as main_mod
from server.parsers.pcb_doc import (
    PcbComponent, PcbData, PcbRule, PolygonPour, Primitive, ShapeVertex, XSignal,
    _health_check_storage, _hole_slot, decode_components, decode_fills, decode_polygons,
    decode_regions, parse_pcb_doc, shape_from_properties,
)
from server.parsers.prj_pcb import VariantDefinition, VariantState
from server.services.pcb_health import (
    CHECKS, area, contour, run_health_check, self_intersections, short_edges,
)
from server.services.pcb_index import PcbIndex
from tests.test_diff_pair_checks import LAYERS
from tests.test_pcb_index import _board, _pad, _track

UNITS = 10000  # internal units per mil
EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")
DRC_FIXTURE = Path(__file__).parent / "fixtures" / "drc" / "Design Rule Check - Demo.drc"


# ---------- record encoders ----------

def _region(layer, vertices, *, net=0xFFFF, polygon=0xFFFF, component=0xFFFF,
            keepout=False, kind=0, shape_based=True, name=""):
    """A Regions6 (shape_based False), ShapeBasedRegions6 or BoardRegions record.
    vertices: (x, y) or (x, y, (cx, cy, r, start, end)) in mil."""
    text = f"|KIND={kind}|NAME={name}".encode() + b"\x00"
    head = (bytes([layer, 0, 2 if keepout else 0])
            + struct.pack("<HHH", net, polygon, component) + b"\x00" * 5
            + struct.pack("<H", 0) + b"\x00\x00" + struct.pack("<I", len(text)) + text)
    body = struct.pack("<I", len(vertices))
    if shape_based:
        for vertex in [*vertices, vertices[0]]:
            x, y = vertex[0], vertex[1]
            arc = vertex[2] if len(vertex) > 2 else None
            cx, cy, radius, start, end = arc or (0, 0, 0, 0.0, 0.0)
            body += struct.pack("<B5i2d", 1 if arc else 0, round(x * UNITS), round(y * UNITS),
                                round(cx * UNITS), round(cy * UNITS), round(radius * UNITS),
                                start, end)
    else:
        body += b"".join(struct.pack("<dd", x * UNITS, y * UNITS) for x, y, *_ in vertices)
    payload = head + body
    return bytes([11]) + struct.pack("<I", len(payload)) + payload


def _fill(layer, x1, y1, x2, y2, component=0xFFFF, net=0xFFFF, rotation=0.0):
    payload = (bytes([layer, 0, 0]) + struct.pack("<HHH", net, 0xFFFF, component)
               + b"\x00" * 4 + struct.pack("<iiii", *(round(v * UNITS) for v in (x1, y1, x2, y2)))
               + struct.pack("<d", rotation))
    return bytes([6]) + struct.pack("<I", len(payload)) + payload


def _square(x0, y0, size):
    return [(x0, y0), (x0 + size, y0), (x0 + size, y0 + size), (x0, y0 + size)]


# ---------- parser ----------

def test_shapes_with_arcs_and_settings():
    properties = {
        "VX0": "0mil", "VY0": "0mil", "VX1": "100mil", "VY1": "0mil",
        "KIND1": "1", "CX1": "100mil", "CY1": "50mil", "R1": "50mil", "SA1": "270", "EA1": "90",
        "VX2": "100mil", "VY2": "100mil", "VX3": "0mil", "VY3": "0mil",
    }
    shape = shape_from_properties(properties)
    assert len(shape) == 3  # the closing copy of the first vertex is dropped
    assert shape[1].arc == (100.0, 50.0, 50.0, 270.0, 90.0)
    assert area(contour(shape)) > 100 * 50 / 2  # the arc bulges out
    storage = ",".join(str(b) for b in json.dumps(
        {"healthCheckRuleEnabledDict": json.dumps({"0": True, "11": False})}).encode())
    assert _health_check_storage(storage) == {0: True, 11: False}
    assert _health_check_storage("not,bytes") is None
    pours, _ = decode_polygons([{"VX0": "0mil", "VY0": "0mil", "VX1": "1mil", "VY1": "0mil",
                                 "LAYER": "TOP", "COPPERINVALIDATE": "TRUE",
                                 "POLYGONTYPE": "Polygon"}], {"top": 1})
    assert pours[0].copper_invalid and pours[0].polygon_type == "Polygon"
    (component,) = decode_components([{"LAYER": "Mid1", "X": "0mil", "Y": "0mil"}])
    assert (component.layer_name, component.side) == ("MID1", "top")


def test_regions_fills_and_slots_decode():
    data = (_region(1, [(0, 0), (10, 0), (10, 10, (5, 10, 5, 0, 180))], net=2, component=3)
            + _region(33, _square(0, 0, 5), polygon=7, keepout=True, kind=3))
    regions, skipped = decode_regions(data, True)
    assert skipped == 0 and len(regions) == 2
    first, second = regions
    assert (first.layer, first.net, first.component, first.polygon) == (1, 2, 3, None)
    assert len(first.vertices) == 3 and first.vertices[2].arc == (5.0, 10.0, 5.0, 0.0, 180.0)
    assert (second.polygon, second.keepout, second.board_cutout) == (7, True, True)
    flat, _ = decode_regions(_region(57, _square(0, 0, 5), shape_based=False), False)
    assert [(v.x, v.y) for v in flat[0].vertices] == _square(0, 0, 5)
    fills, _ = decode_fills(_fill(1, 0, 0, 20, 10, component=4, rotation=45.0))
    assert (fills[0].component, fills[0].x2, fills[0].rotation) == (4, 20.0, 45.0)
    size_shape = bytearray(300)
    size_shape[262] = 2
    size_shape[263:267] = struct.pack("<i", 30 * UNITS)
    assert _hole_slot(bytes(size_shape)) == (2, 30.0)
    assert _hole_slot(b"") == (None, None)


def test_shape_geometry():
    bow_tie = [ShapeVertex(0, 0), ShapeVertex(10, 10), ShapeVertex(10, 0), ShapeVertex(0, 10)]
    assert self_intersections(contour(bow_tie)) == [(5.0, 5.0)]
    assert self_intersections(contour([ShapeVertex(*p) for p in _square(0, 0, 10)])) == []
    tiny = [ShapeVertex(0, 0), ShapeVertex(0.01, 0), ShapeVertex(10, 0), ShapeVertex(10, 10)]
    assert [round(edge[0], 4) for edge in short_edges(tiny)] == [0.01]
    # A zero-length edge where an arc starts is no edge of the shape.
    arc_start = [ShapeVertex(0, 0), ShapeVertex(0, 0, (5, 0, 5, 180, 0)), ShapeVertex(10, 0)]
    assert short_edges(arc_start) == []


# ---------- checks ----------

def _component(index, designator, x, y, uid, rotation=0.0, layer="TOP"):
    return PcbComponent(
        index=index, refdes=designator, source_uid_tail=uid, channel_path=None, x=x, y=y,
        rotation=rotation, side="bottom" if layer == "BOTTOM" else "top", designator=designator,
        source_unique_id=f"\\{uid}", layer_name=layer,
    )


def _pcb(**changes):
    """A 1000 x 1000 mil board with one problem per check."""
    components = [
        _component(0, "U1", 100, 100, "AAA", rotation=360.0),
        _component(1, "U1", 300, 300, "BBB"),  # duplicate designator
        _component(2, "R1", 500, 500, "CCC", layer="MECHANICAL1"),
        _component(3, "C1", 800, 100, "DDD"),  # origin away from its pads
        _component(4, "J9", 5000, 5000, "EEE"),  # parked outside the board
    ]
    primitives = [
        _pad(net=0, component=0, x=100, y=100, name="1"),
        _pad(net=0, component=1, x=300, y=300, name="1"),
        _pad(net=0, component=2, x=500, y=500, name="1"),
        _pad(net=0, component=3, x=700, y=100, name="1"),
        _pad(net=1, component=4, x=5000, y=5000, name="1"),
        Primitive(kind="pad", layer=74, net=1, component=0, x1=150, y1=100, x2=150, y2=100,
                  width=60, height=60, shape=1, hole=40.0, pad_name="2", hole_shape=2,
                  slot_length=30.0),
        _track(0, 1, 0, 200, 100, 200),
        _track(0, 1, 100, 200, 0, 200),  # the same track again, reversed
    ]
    pours = [
        PolygonPour(0, 0, 1, None, _square(0, 0, 100), shelved=True),
        PolygonPour(1, 0, 1, None, _square(200, 0, 100), copper_invalid=True),
        PolygonPour(2, None, 1, None, [(0, 0), (10, 0)]),  # zero area
    ]
    regions = (
        _region(1, [(0, 0), (10, 10), (10, 0), (0, 20)])  # self-intersecting
        + _region(1, [(0, 0), (5, 0), (10, 0)])  # zero area
        + _region(1, [(0, 0), (0.01, 0), (40, 0), (40, 40)])  # copper micro-segment
        + _region(56, [(0, 0), (0.01, 0), (40, 0), (40, 40)])  # keep-out: board category
        + _region(57, [(0, 0), (0.01, 0), (40, 0), (40, 40)])  # mechanical: debug only
        + _region(1, [(0, 0), (0.01, 0), (40, 0), (40, 40)], polygon=1)  # pour copper
    )
    board = _board()
    board.outline_shape = [ShapeVertex(*p) for p in _square(0, 0, 1000)]
    data = PcbData(
        board=board, nets=["GND", "VCC"], components=components, primitives=primitives,
        pours=pours, layer_name_to_v6=dict(LAYERS),
        rules=[
            PcbRule("Width", "Width", 7, True, 1, "All", "", None, "", None, False),
            PcbRule("Width", "Width", 7, True, 2, "InNet('GND')", "", None, "", None, False),
            PcbRule("XS", "Length", 9, True, 1, "InxSignal('USED')", "", None, "", None, False),
        ],
        xsignals=[XSignal("BROKEN", ["U1-1", "U7-3"]), XSignal("USED", ["U1-1", "C1-1"]),
                  XSignal("SPARE", ["U1-1", "C1-1"])],
        raw={
            "ShapeBasedRegions6/Data": regions,
            "BoardRegions/Data": _region(1, _square(0, 0, 1000), shape_based=False),
            "Fills6/Data": _fill(33, 650, 50, 750, 150, component=3),
        },
    )
    for key, value in changes.items():
        setattr(data, key, value)
    return data


def _by_check(result):
    found = {}
    for finding in result["findings"]:
        found.setdefault(finding["check"], []).append(finding)
    return found


def test_every_check_on_a_synthetic_board():
    result = run_health_check(_pcb())
    found = _by_check(result)
    assert sorted(found) == sorted([
        "shelved_modified_polygons", "duplicate_designators", "zero_area_polygons",
        "zero_area_regions", "self_intersecting_regions", "micro_segments_board",
        "micro_segments_copper", "slot_holes", "broken_xsignals", "rotation_360",
        "reference_point_outside", "non_signal_layer_components", "unused_xsignals",
        "invalid_rules", "components_outside_board", "duplicate_copper",
    ])
    shelved = found["shelved_modified_polygons"]
    assert [f["state"] for f in shelved] == ["shelved", "modified"]
    assert {f["severity"] for f in shelved} == {"error"}
    assert shelved[0]["altium_id"] == 14 and shelved[0]["category"] == "Shelved/Modified Polygons"
    assert found["duplicate_designators"][0]["object"] == "U1"
    assert found["rotation_360"][0]["severity"] == "info"
    assert found["reference_point_outside"][0]["object"] == "C1"
    assert found["reference_point_outside"][0]["distance_mil"] == pytest.approx(50.0)
    assert found["non_signal_layer_components"][0]["object"] == "R1 on MECHANICAL1"
    assert found["broken_xsignals"][0]["missing_pins"] == ["U7-3"]
    assert [f["object"] for f in found["unused_xsignals"]] == ["BROKEN", "SPARE"]
    assert found["slot_holes"][0]["slot_length_mil"] == 30.0
    assert found["components_outside_board"][0]["object"] == "J9"
    assert found["components_outside_board"][0]["severity"] == "warning"  # netted pads
    assert found["micro_segments_copper"][0]["segments"] == 1
    assert [f["severity"] for f in result["findings"][:2]] == ["error", "error"]
    assert "micro_segments_graphics" in result["not_checked"]
    assert "mirrored_footprints" in result["not_checked"]
    graphics = run_health_check(_pcb(), checks=["micro_segments_graphics"])
    assert graphics["summary"] == {"micro_segments_graphics": 1}
    assert run_health_check(_pcb(), checks=["nope"])["error"] == "invalid_checks"


def test_board_settings_turn_checks_off():
    board = _board()
    board.health_checks = {11: False, 5: True}
    result = run_health_check(_pcb(board=board))
    assert "duplicate_designators" not in result["summary"]
    assert result["not_checked"]["duplicate_designators"].startswith("turned off")
    assert "turned off: ['duplicate_designators']" in result["board_settings"]


def test_many_objects_become_one_finding():
    components = [_component(i, f"R{i}", 10 + i, 10, f"U{i}", rotation=360.0) for i in range(30)]
    result = run_health_check(_pcb(components=components, primitives=[]),
                              checks=["rotation_360"])
    (finding,) = result["findings"]
    assert finding["count"] == 30 and len(finding["objects"]) == 30
    assert result["summary"] == {"rotation_360": 30}


def test_comparison_with_the_saved_report():
    result = run_health_check(_pcb(), report_health={
        "Reference Point Outside the Component Area": 1, "Unknown Category": 2})
    rows = {row["category"]: row for row in result["altium_report"]["categories"]}
    assert rows["Reference Point Outside the Component Area"] == {
        "category": "Reference Point Outside the Component Area", "report": 1, "offline": 1}
    assert rows["Unknown Category"]["offline"] is None


# ---------- tool ----------

@pytest.fixture
def board(monkeypatch, tmp_path: Path):
    pcb_file = tmp_path / "Demo.PcbDoc"
    pcb_file.write_bytes(b"fake")
    index = PcbIndex(_pcb(), None)
    monkeypatch.setattr(main_mod, "_project", {
        "name": "Demo", "root_dir": str(tmp_path), "prj_pcb_path": str(tmp_path / "Demo.PrjPcb"),
        "sheets": [], "pcb_doc_paths": [str(pcb_file)],
    })
    monkeypatch.setattr(main_mod._altium, "_netlist", {"nets": {}, "pin_to_net": {}})
    monkeypatch.setattr(main_mod, "_variant_state",
                        VariantState([VariantDefinition(name="Default")]))
    monkeypatch.setattr(main_mod._pcb_session, "_load", lambda path, netlist: index)
    main_mod._pcb_session._index = None
    return tmp_path


def test_check_pcb_health_tool(board):
    result = json.loads(main_mod.check_pcb_health(units="mm"))
    assert result["pcb_doc"] == "Demo.PcbDoc" and result["units"] == "mm"
    reference = next(f for f in result["findings"] if f["check"] == "reference_point_outside")
    assert reference["at_mm"] == [20.32, 2.54] and reference["distance_mm"] == pytest.approx(1.27)
    assert "1.27 mm outside" in reference["issue"]
    assert "altium_report" not in result
    # Altium's saved report next to it: its health counts are compared.
    shutil.copy(DRC_FIXTURE, board / DRC_FIXTURE.name)
    result = json.loads(main_mod.check_pcb_health())
    (row,) = result["altium_report"]["categories"]
    assert row == {"category": "Reference Point Outside the Component Area",
                   "report": 2, "offline": 1}
    assert result["altium_report_file"] == DRC_FIXTURE.name
    assert json.loads(main_mod.check_pcb_health(checks=["x"]))["error"] == "invalid_checks"


def test_check_pcb_health_trims_to_fit(board, monkeypatch):
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 4000)
    result = json.loads(main_mod.check_pcb_health())
    assert result["findings_limited_to"] in (40, 15, 5) and "next_offset" in result
    assert "next_offset" in main_mod.check_pcb_health.__doc__


def test_check_list_matches_altium_ids():
    ids = [entry[0] for entry in CHECKS.values() if entry[0] is not None]
    assert len(ids) == len(set(ids)) == 15


# ---------- Altium's examples ----------

@pytest.mark.parametrize("folder, name, expected", [
    ("Bluetooth Sentinel", "Bluetooth_Sentinel.PcbDoc", {}),
    ("Mini PC", "MiniPC.PcbDoc", {"unused_xsignals": 969, "duplicate_copper": 1}),
])
def test_examples(folder, name, expected):
    paths = list((EXAMPLES / folder).rglob(name))
    if not paths:
        pytest.skip("Altium example boards not installed")
    assert run_health_check(parse_pcb_doc(str(paths[0])))["summary"] == expected
