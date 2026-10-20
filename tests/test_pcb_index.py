import math
import struct
from copy import deepcopy
from dataclasses import replace

import pytest

from server.parsers.pcb_doc import (
    PAD_INNER_LAYERS,
    PAD_SHAPE_RECT,
    PAD_SHAPE_ROUND,
    PAD_SHAPE_ROUNDRECT,
    BoardInfo,
    PadCopper,
    PcbComponent,
    PcbData,
    PcbLayer,
    PolygonPour,
    Primitive,
)
from server.services.pcb_index import PcbIndex
from server.parsers.pcb_doc import decode_arcs, decode_polygons


def _layer(v6, name, order, kind="copper", **kwargs):
    return PcbLayer(
        v6_id=v6,
        name=name,
        kind=kind,
        stack_order=order,
        **kwargs,
    )


def _board():
    return BoardInfo(
        origin_x=0.0,
        origin_y=0.0,
        sheet_x=0.0,
        sheet_y=0.0,
        sheet_width=1000.0,
        sheet_height=1000.0,
        display_unit="mil",
        stackup_source="v9",
        layers=[
            _layer(1, "Top Layer", 0),
            _layer(
                None,
                "Dielectric1",
                1,
                kind="dielectric",
                diel_height_mil=5.0,
            ),
            _layer(2, "L2-GND", 2),
            _layer(3, "L3-SIG", 4),
            _layer(32, "Bottom Layer", 6),
        ],
    )


def _track(net, layer, x1, y1, x2, y2, width=6.0, component=None):
    return Primitive(
        kind="track",
        layer=layer,
        net=net,
        component=component,
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        width=width,
    )


def _pad(
    net,
    component,
    x,
    y,
    name="1",
    layer=1,
    width=10.0,
    height=None,
    shape=1,
    rotation=0.0,
):
    return Primitive(
        kind="pad",
        layer=layer,
        net=net,
        component=component,
        x1=x,
        y1=y,
        x2=x,
        y2=y,
        width=width,
        height=height if height is not None else width,
        shape=shape,
        rotation=rotation,
        pad_name=name,
    )


@pytest.fixture
def pcb_data():
    return PcbData(
        board=_board(),
        nets=["GND", "CLK_A", "CLK_B"],
        components=[
            PcbComponent(
                index=0,
                refdes="U3",
                source_uid_tail="NWGG",
                channel_path="top\\VS\\Top",
                x=100.0,
                y=100.0,
                rotation=0.0,
                side="top",
            ),
            PcbComponent(
                index=1,
                refdes="U3",
                source_uid_tail="NWGG",
                channel_path="top\\VS\\Bottom",
                x=500.0,
                y=100.0,
                rotation=0.0,
                side="top",
            ),
            PcbComponent(
                index=2,
                refdes="R1",
                source_uid_tail="RRRR",
                channel_path=None,
                x=300.0,
                y=300.0,
                rotation=90.0,
                side="bottom",
            ),
        ],
        primitives=[
            _pad(net=1, component=0, x=100.0, y=100.0),
            _pad(net=0, component=0, x=100.0, y=110.0, name="2"),
            _pad(net=2, component=1, x=500.0, y=100.0),
            _pad(net=0, component=1, x=500.0, y=110.0, name="2"),
            _pad(net=0, component=2, x=300.0, y=300.0),
            _track(net=1, layer=1, x1=100, y1=100, x2=200, y2=100),
            _track(net=1, layer=3, x1=200, y1=100, x2=300, y2=100),
            _track(net=2, layer=1, x1=100, y1=103, x2=200, y2=103),
            Primitive(
                kind="via",
                layer=1,
                layer_to=32,
                net=1,
                component=None,
                x1=200,
                y1=100,
                x2=200,
                y2=100,
                width=20.0,
                hole=8.0,
            ),
        ],
        pours=[
            PolygonPour(
                pour_id=0,
                net=0,
                layer=2,
                hatch_style="Solid",
                vertices=[
                    (0, 0),
                    (1000, 0),
                    (1000, 1000),
                    (0, 1000),
                ],
            )
        ],
        layer_name_to_v6={
            "top layer": 1,
            "l2-gnd": 2,
            "l3-sig": 3,
            "bottom layer": 32,
            "top": 1,
            "bottom": 32,
        },
    )


NETLIST = {
    "components": {
        "U3A": {
            "unique_id": "NWGG",
            "pins": {
                "1": {"name": "CLK", "net": "CLK_A"},
                "2": {"name": "GND", "net": "GND"},
            },
        },
        "U3B": {
            "unique_id": "NWGG",
            "pins": {
                "1": {"name": "CLK", "net": "CLK_B"},
                "2": {"name": "GND", "net": "GND"},
            },
        },
        "R1": {
            "unique_id": "RRRR",
            "pins": {"1": {"name": "~", "net": "GND"}},
        },
    },
}


def test_build_and_counts(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    counts = index.counts()
    assert counts["tracks"] == 3
    assert counts["arcs"] == 0
    assert counts["vias"] == 1
    assert counts["pads"] == 5
    assert counts["pours"] == 1
    assert counts["nets"] == 3
    assert counts["components"] == 3


def test_rtree_shadow_matches_primitive_rows_when_available(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    assert isinstance(index._has_rtree, bool)
    if index._has_rtree:
        primitive_count = index.db.execute(
            "SELECT COUNT(*) FROM prims"
        ).fetchone()[0]
        shadow_count = index.db.execute(
            "SELECT COUNT(*) FROM prims_rtree"
        ).fetchone()[0]
        assert shadow_count == primitive_count


def test_refdes_resolution_direct_and_signature(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    rows = index.find_components("U3")
    assert {row["sch_refdes"] for row in rows} == {"U3A", "U3B"}
    by_schematic = {row["sch_refdes"]: row for row in rows}
    assert by_schematic["U3A"]["channel_path"] == "top\\VS\\Top"
    assert by_schematic["U3B"]["channel_path"] == "top\\VS\\Bottom"
    assert index.find_components("R1")[0]["sch_refdes"] == "R1"
    assert (
        index.find_components("U3B")[0]["channel_path"]
        == "top\\VS\\Bottom"
    )


def test_refdes_resolution_unmatched_count(pcb_data):
    index = PcbIndex(pcb_data, netlist=None)
    assert index.unmatched_components() == 3
    resolved_index = PcbIndex(pcb_data, NETLIST)
    assert resolved_index.unmatched_components() == 0


def test_refdes_resolution_rejects_partial_signature_match(pcb_data):
    data = deepcopy(pcb_data)
    data.components.append(
        PcbComponent(
            index=3,
            refdes="U9",
            source_uid_tail="PARTIAL",
            channel_path="top\\Partial",
            x=700.0,
            y=100.0,
            rotation=0.0,
            side="top",
        )
    )
    data.primitives.extend([
        _pad(net=1, component=3, x=700.0, y=100.0, name="1"),
        _pad(net=0, component=3, x=710.0, y=100.0, name="2"),
    ])
    netlist = deepcopy(NETLIST)
    netlist["components"].update({
        "U9A": {
            "unique_id": "PARTIAL",
            "pins": {
                "1": {"net": "CLK_A"},
                "2": {"net": "CLK_B"},
            },
        },
        "U9B": {
            "unique_id": "PARTIAL",
            "pins": {
                "1": {"net": "CLK_B"},
                "2": {"net": "CLK_B"},
            },
        },
    })
    index = PcbIndex(data, netlist)
    assert index.find_components("U9")[0]["sch_refdes"] is None


def test_layer_resolution(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    index.pcb.layer_name_to_v6["mid30"] = 31
    assert index.resolve_layer("L3-SIG") == 3
    assert index.resolve_layer("l3-sig") == 3
    assert index.resolve_layer("nope") is None
    assert index.resolve_layer("MID30") is None


def test_net_summary(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    summary = index.net_summary("CLK_A")
    assert summary["net"] == "CLK_A"
    layers = {layer["layer"]: layer for layer in summary["layers"]}
    assert layers["Top Layer"]["segment_count"] == 1
    assert layers["Top Layer"]["length_mil"] == pytest.approx(100.0)
    assert layers["Top Layer"]["bbox_mil"] == {
        "minx_mil": pytest.approx(97.0),
        "miny_mil": pytest.approx(97.0),
        "maxx_mil": pytest.approx(203.0),
        "maxy_mil": pytest.approx(103.0),
    }
    assert layers["L3-SIG"]["length_mil"] == pytest.approx(100.0)
    assert summary["via_count"] == 1
    assert summary["vias"][0]["span"] == "Top Layer -> Bottom Layer"
    assert summary["pads"][0]["refdes"] == "U3"
    assert summary["pads"][0]["sch_refdes"] == "U3A"
    assert summary["bbox_mil"] == {
        "minx_mil": pytest.approx(95.0),
        "miny_mil": pytest.approx(90.0),
        "maxx_mil": pytest.approx(303.0),
        "maxy_mil": pytest.approx(110.0),
    }


def test_net_summary_unknown_net(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    assert index.net_summary("NOPE") is None


def test_net_summary_caps_detail_collections_and_reports_totals(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    index.MAX_NET_DETAIL_RESULTS = 0
    summary = index.net_summary("CLK_A")
    assert summary["via_count"] == 1
    assert summary["vias"] == []
    assert summary["vias_has_more"] is True
    assert summary["pad_count"] == 1
    assert summary["pads"] == []
    assert summary["pads_has_more"] is True


def test_net_neighbors_same_layer(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    result = index.net_neighbors("CLK_A", distance=10.0)
    names = [neighbor["net"] for neighbor in result["neighbors"]]
    assert "CLK_B" in names
    clock_b = next(
        neighbor
        for neighbor in result["neighbors"]
        if neighbor["net"] == "CLK_B"
    )
    assert clock_b["layer"] == "Top Layer"
    assert clock_b["min_edge_distance_mil"] == 0.0
    assert clock_b["parallel_run_mil"] == pytest.approx(100.0)


def test_net_neighbors_excludes_far_nets(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    result = index.net_neighbors("CLK_A", distance=0.5, layer=3)
    assert result["neighbors"] == []


def test_net_neighbors_broadside(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    result = index.net_neighbors("CLK_A", distance=5.0)
    assert "GND" in [entry["net"] for entry in result["broadside"]]


def test_broadside_uses_per_layer_target_extents(pcb_data):
    data = deepcopy(pcb_data)
    data.nets.extend(["TARGET_GAP", "MID_GAP"])
    data.primitives.extend([
        _track(net=3, layer=1, x1=0, y1=500, x2=100, y2=500),
        _track(net=3, layer=3, x1=900, y1=500, x2=1000, y2=500),
        _track(net=4, layer=2, x1=450, y1=500, x2=550, y2=500),
    ])
    index = PcbIndex(data, NETLIST)
    result = index.net_neighbors("TARGET_GAP", distance=5.0)
    assert "MID_GAP" not in [
        entry["net"] for entry in result["broadside"]
    ]


def test_inner_layer_queries_include_through_via_candidates(pcb_data):
    data = deepcopy(pcb_data)
    data.nets.append("VIA_NET")
    data.primitives.append(
        Primitive(
            kind="via",
            layer=1,
            layer_to=32,
            net=3,
            component=None,
            x1=250.0,
            y1=105.0,
            x2=250.0,
            y2=105.0,
            width=10.0,
            hole=5.0,
        )
    )
    index = PcbIndex(data, NETLIST)

    neighbors = index.net_neighbors("CLK_A", distance=1.0, layer=3)
    assert any(
        neighbor["net"] == "VIA_NET"
        for neighbor in neighbors["neighbors"]
    )

    reverse = index.net_neighbors("VIA_NET", distance=1.0, layer=3)
    assert any(
        neighbor["net"] == "CLK_A"
        for neighbor in reverse["neighbors"]
    )

    region = index.region_query(250.0, 105.0, radius=0.0, layer=3)
    assert any(net["net"] == "VIA_NET" for net in region["nets"])
    via_net = next(net for net in region["nets"] if net["net"] == "VIA_NET")
    assert via_net["layers"] == ["L3-SIG"]


def test_inner_layer_region_includes_multilayer_pad(pcb_data):
    data = deepcopy(pcb_data)
    data.nets.append("PTH_NET")
    data.board.layers.append(
        _layer(74, "Multi-Layer", None, kind="non-stack")
    )
    data.primitives.append(
        _pad(
            net=3,
            component=None,
            x=250.0,
            y=105.0,
            layer=74,
            width=10.0,
        )
    )
    index = PcbIndex(data, NETLIST)
    region = index.region_query(250.0, 105.0, radius=0.0, layer=3)
    assert any(net["net"] == "PTH_NET" for net in region["nets"])


def test_net_neighbors_caps_broadside(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    index.MAX_NEIGHBORS = 0
    result = index.net_neighbors("CLK_A", distance=5.0)
    assert result["broadside"] == []
    assert result["broadside_count"] > 0
    assert result["broadside_has_more"] is True


def test_net_neighbors_unknown_net(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    assert index.net_neighbors("NOPE", distance=10.0) is None


def test_region_query(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    result = index.region_query(150.0, 100.0, radius=60.0)
    net_names = [net["net"] for net in result["nets"]]
    assert "CLK_A" in net_names
    assert "CLK_B" in net_names
    clock_a = next(net for net in result["nets"] if net["net"] == "CLK_A")
    assert clock_a["min_distance_mil"] == 0.0
    assert "U3" in [
        component["refdes"] for component in result["components"]
    ]
    ground_pour = next(
        pour for pour in result["pours"] if pour["net"] == "GND"
    )
    assert ground_pour["inside_pour"] is True


def test_region_query_layer_filter(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    result = index.region_query(250.0, 100.0, radius=10.0, layer=3)
    assert [net["net"] for net in result["nets"]] == ["CLK_A"]


def test_region_query_excludes_pour_when_only_bbox_is_near(pcb_data):
    data = deepcopy(pcb_data)
    data.pours = [
        PolygonPour(
            pour_id=0,
            net=0,
            layer=2,
            hatch_style="Solid",
            vertices=[(0, 0), (100, 0), (0, 100)],
        )
    ]
    index = PcbIndex(data, NETLIST)
    result = index.region_query(90.0, 90.0, radius=5.0, layer=2)
    assert result["pours"] == []


def _add_pour(data, net, layer, vertices, **props):
    """Add a pour and its zero-width outline rows the way parse_streams does."""
    record = {"NET": str(net), "LAYER": layer, "HATCHSTYLE": "Solid", **props}
    for vertex, (x, y) in enumerate(vertices):
        record[f"VX{vertex}"] = f"{x}mil"
        record[f"VY{vertex}"] = f"{y}mil"
    (pour,), edges = decode_polygons([record], data.layer_name_to_v6)
    pour.pour_id = len(data.pours)
    for edge in edges:
        edge.pour_id = pour.pour_id
    data.pours.append(pour)
    data.primitives.extend(edges)


@pytest.fixture
def outline_data(pcb_data):
    """PLANE (net 4) pours on Top Layer; its outline's left edge (x = 650)
    crosses SIG's track, the rest of SIG lies inside. NEAR stops 2 mil short of
    the outline, outside it."""
    data = deepcopy(pcb_data)
    data.nets.extend(["SIG", "PLANE", "NEAR"])
    data.primitives.extend([
        _track(net=3, layer=1, x1=600, y1=600, x2=700, y2=600),
        _track(net=5, layer=1, x1=600, y1=800, x2=645, y2=800),
    ])
    _add_pour(
        data, 4, "TOP", [(650, 500), (900, 500), (900, 900), (650, 900)]
    )
    return data


def test_pour_outline_is_not_a_copper_neighbor(outline_data):
    index = PcbIndex(outline_data, NETLIST)
    result = index.net_neighbors("SIG", distance=10.0)
    assert "PLANE" not in [n["net"] for n in result["neighbors"]]
    assert result["pours"] == [
        {
            "net": "PLANE",
            "layer": "Top Layer",
            "inside_pour": True,
            "min_outline_distance_mil": 0.0,
        }
    ]
    assert result["pour_count"] == 1
    assert result["pours_has_more"] is False
    # The pour's own outline is not PLANE copper either.
    plane = index.net_neighbors("PLANE", distance=10.0)
    assert plane["neighbors"] == []
    assert plane["pours"] == []


def test_pour_outline_reports_inside_and_distance_separately(outline_data):
    index = PcbIndex(outline_data, NETLIST)
    near = index.net_neighbors("NEAR", distance=10.0)
    assert near["neighbors"] == []
    assert near["pours"] == [
        {
            "net": "PLANE",
            "layer": "Top Layer",
            "inside_pour": False,
            "min_outline_distance_mil": 2.0,
        }
    ]
    assert index.net_neighbors("NEAR", distance=1.0)["pours"] == []
    # Deep inside the pour, the outline is beyond the search distance.
    outline_data.nets.append("DEEP")
    outline_data.primitives.append(
        _track(net=6, layer=1, x1=750, y1=700, x2=800, y2=700)
    )
    deep = PcbIndex(outline_data, NETLIST).net_neighbors("DEEP", 10.0)
    assert deep["pours"] == [
        {
            "net": "PLANE",
            "layer": "Top Layer",
            "inside_pour": True,
            "min_outline_distance_mil": None,
        }
    ]


def test_pour_outline_follows_its_arcs(pcb_data):
    # PLANE's right edge is a Polygons6 arc bulging out to x = 1100: SIG's track
    # starts in the bulge and crosses the arc there.
    data = deepcopy(pcb_data)
    data.nets.extend(["SIG", "PLANE"])
    data.primitives.append(_track(net=3, layer=1, x1=1050, y1=700, x2=1200, y2=700))
    _add_pour(
        data, 4, "TOP", [(650, 500), (900, 500), (900, 900), (650, 900)],
        KIND1="1", CX1="900mil", CY1="700mil", R1="200mil", SA1="270", EA1="90",
    )
    index = PcbIndex(data, NETLIST)
    assert index.net_neighbors("SIG", distance=10.0)["pours"] == [
        {
            "net": "PLANE",
            "layer": "Top Layer",
            "inside_pour": True,
            "min_outline_distance_mil": 0.0,
        }
    ]
    inside = index.region_query(1050.0, 700.0, radius=0.0, layer=1)
    assert [(p["net"], p["inside_pour"]) for p in inside["pours"]] == [("PLANE", True)]
    (beyond,) = index.region_query(1105.0, 700.0, radius=10.0, layer=1)["pours"]
    assert beyond["inside_pour"] is False
    assert beyond["min_distance_mil"] == pytest.approx(5.0, abs=0.25)


def test_pour_outline_respects_the_layer_filter(outline_data):
    index = PcbIndex(outline_data, NETLIST)
    assert index.net_neighbors("SIG", distance=10.0, layer=3)["pours"] == []


def _hatch(net, x1, y1, x2, y2, width=8.0, pour=1):
    """A hatched pour's track: Tracks6 copper with the pour's index (PLANE's
    pour is index 1 in outline_data)."""
    return replace(_track(net, 1, x1, y1, x2, y2, width=width), poured_by=pour)


def test_poured_copper_stays_a_neighbor(outline_data):
    # A hatched pour's tracks come from Tracks6 with their width and the
    # pour's index: real copper, measured edge to edge (608 - 603 = 5 mil),
    # but no routing segment and no parallel run beside SIG's track.
    outline_data.primitives.append(_hatch(4, 660, 612, 900, 612))
    index = PcbIndex(outline_data, NETLIST)
    result = index.net_neighbors("SIG", distance=10.0)
    plane = next(n for n in result["neighbors"] if n["net"] == "PLANE")
    assert plane["min_edge_distance_mil"] == pytest.approx(5.0)
    assert plane["close_segments"] == 0
    assert plane["parallel_run_mil"] == 0.0
    assert plane["pour_copper_segments"] == 1
    assert result["pours"][0]["min_outline_distance_mil"] == 0.0
    # As a target too: PLANE's hatch is near SIG's track.
    reverse = index.net_neighbors("PLANE", distance=10.0)
    sig = next(n for n in reverse["neighbors"] if n["net"] == "SIG")
    assert (sig["close_segments"], sig["pour_copper_segments"]) == (0, 1)
    # A routed PLANE track in the same place is routing: segment and run.
    outline_data.primitives[-1] = _track(4, 1, 660, 612, 900, 612, width=8.0)
    routed = PcbIndex(outline_data, NETLIST).net_neighbors("SIG", distance=10.0)
    plane = next(n for n in routed["neighbors"] if n["net"] == "PLANE")
    assert plane["close_segments"] == 1 and plane["parallel_run_mil"] == 40.0
    assert "pour_copper_segments" not in plane


def test_poured_copper_is_not_routing(outline_data):
    outline_data.primitives.extend([
        _hatch(4, 660, 612, 900, 612),
        _track(4, 1, 950, 950, 1000, 950),  # PLANE's only routing, 50 mil
    ])
    index = PcbIndex(outline_data, NETLIST)
    assert index.routed_length("PLANE") == 50.0
    (layer,) = index.net_summary("PLANE")["layers"]
    assert (layer["segment_count"], layer["length_mil"]) == (1, 50.0)
    counts = index.counts()
    # pcb_data's 3 tracks, SIG, NEAR and PLANE's routing; outlines excluded.
    assert (counts["tracks"], counts["pour_copper"]) == (6, 1)
    # Region queries see it as copper of kind pour.
    region = index.region_query(700.0, 612.0, radius=5.0, layer=1)
    plane = next(n for n in region["nets"] if n["net"] == "PLANE")
    assert plane["kinds"] == ["pour"] and plane["min_distance_mil"] == 0.0


def test_shelved_pour_has_no_copper_but_is_listed(pcb_data):
    # CLK_B pours Top around CLK_A's track (100..200, y 100).
    square = [(50, 50), (250, 50), (250, 150), (50, 150)]
    shelved = deepcopy(pcb_data)
    _add_pour(shelved, 2, "TOP", square, SHELVED="TRUE")
    poured = deepcopy(pcb_data)
    _add_pour(poured, 2, "TOP", square, SHELVED="FALSE")
    for data, expected in ((poured, True), (shelved, False)):
        index = PcbIndex(data, NETLIST)
        neighbors = index.net_neighbors("CLK_A", distance=10.0)
        assert ("CLK_B" in [p["net"] for p in neighbors["pours"]]) is expected
        region = index.region_query(150.0, 100.0, radius=10.0, layer=1)
        assert ("CLK_B" in [p["net"] for p in region["pours"]]) is expected
        assert index.rule_target("CLK_B").has_pours is expected
        outline_rows = index.db.execute(
            "SELECT COUNT(*) c FROM prims WHERE pour_id = 1"
        ).fetchone()["c"]
        assert (outline_rows == 4) is expected
    index = PcbIndex(shelved, NETLIST)
    # The net's pour inventory still lists it, labelled.
    summary = index.net_summary("CLK_B")
    assert summary["pour_count"] == 1
    assert summary["pours"][0]["shelved"] is True
    assert "shelved" not in index.net_summary("GND")["pours"][0]
    counts = index.counts()
    assert (counts["pours"], counts["shelved_pours"]) == (2, 1)


def test_region_query_lists_pour_outline_only_under_pours(outline_data):
    index = PcbIndex(outline_data, NETLIST)
    # The outline (x = 650) is 5 mil away: within the radius, but not copper.
    result = index.region_query(655.0, 600.0, radius=5.0, layer=1)
    assert [net["net"] for net in result["nets"]] == ["SIG"]
    assert result["pours"] == [
        {
            "net": "PLANE",
            "layer": "Top Layer",
            "inside_pour": True,
            "min_distance_mil": 0.0,
        }
    ]


def test_component_without_coordinates_does_not_appear_at_origin(pcb_data):
    data = deepcopy(pcb_data)
    data.components.append(
        PcbComponent(
            index=3,
            refdes=None,
            source_uid_tail=None,
            channel_path=None,
            x=None,
            y=None,
            rotation=None,
            side="top",
        )
    )
    index = PcbIndex(data, NETLIST)
    result = index.region_query(0.0, 0.0, radius=1.0)
    assert result["components"] == []


def test_rectangular_pad_uses_rotated_shape_not_max_diameter(pcb_data):
    data = deepcopy(pcb_data)
    data.nets.extend(["RECT_PAD", "FAR_TRACK"])
    data.components.append(
        PcbComponent(
            index=3,
            refdes="J1",
            source_uid_tail=None,
            channel_path=None,
            x=0.0,
            y=0.0,
            rotation=0.0,
            side="top",
        )
    )
    data.primitives.extend([
        _pad(
            net=3,
            component=3,
            x=0.0,
            y=0.0,
            width=100.0,
            height=10.0,
            shape=2,
        ),
        _track(
            net=4,
            layer=1,
            x1=-10.0,
            y1=40.0,
            x2=10.0,
            y2=40.0,
            width=2.0,
        ),
    ])
    index = PcbIndex(data, NETLIST)
    component = index.find_components("J1")[0]
    assert component["minx"] == pytest.approx(-50.0)
    assert component["maxx"] == pytest.approx(50.0)
    assert component["miny"] == pytest.approx(-5.0)
    assert component["maxy"] == pytest.approx(5.0)
    neighbors = index.net_neighbors("RECT_PAD", distance=10.0)
    assert "FAR_TRACK" not in [
        neighbor["net"] for neighbor in neighbors["neighbors"]
    ]
    region = index.region_query(0.0, 40.0, radius=10.0)
    assert "RECT_PAD" not in [net["net"] for net in region["nets"]]


def _rounded_pad(corner, net=0, component=None, rotation=0.0):
    """A 40 x 60 mil rounded-rectangle pad at the origin."""
    pad = _pad(
        net=net,
        component=component,
        x=0.0,
        y=0.0,
        width=40.0,
        height=60.0,
        shape=PAD_SHAPE_ROUNDRECT,
        rotation=rotation,
    )
    pad.corner_radius = corner
    return pad


def test_rounded_rectangle_pad_distances(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    pad = _rounded_pad(corner=10.0)
    sharp = deepcopy(pad)
    sharp.shape = PAD_SHAPE_RECT
    oblong = deepcopy(pad)
    oblong.shape = PAD_SHAPE_ROUND
    # Off the corner: the corner arc is centred at (10, 20), radius 10.
    assert index._point_primitive_distance(30.0, 40.0, pad) == pytest.approx(
        math.hypot(20.0, 20.0) - 10.0
    )
    assert index._point_primitive_distance(30.0, 40.0, sharp) == pytest.approx(
        math.hypot(10.0, 10.0)
    )
    assert index._point_primitive_distance(
        30.0, 40.0, oblong
    ) == pytest.approx(math.hypot(30.0, 30.0) - 20.0)
    # Inside the sharp corner, outside the rounded one.
    assert index._point_primitive_distance(19.5, 29.5, pad) == pytest.approx(
        math.hypot(9.5, 9.5) - 10.0
    )
    # Straight edges and the inside are the rectangle's.
    assert index._point_primitive_distance(25.0, 0.0, pad) == pytest.approx(5.0)
    assert index._point_primitive_distance(0.0, -35.0, pad) == pytest.approx(
        5.0
    )
    assert index._point_primitive_distance(15.0, 25.0, pad) == 0.0
    # Bounds are the full pad.
    assert index._primitive_bounds(pad) == pytest.approx(
        (-20.0, -30.0, 20.0, 30.0)
    )
    # A track across the corner diagonal (x + y = 60, 2 mil wide).
    track = _track(net=1, layer=1, x1=15.0, y1=45.0, x2=35.0, y2=25.0, width=2.0)
    assert index._primitive_distance(pad, track) == pytest.approx(
        30.0 / math.sqrt(2.0) - 10.0 - 1.0
    )
    assert index._primitive_distance(track, pad) == pytest.approx(
        30.0 / math.sqrt(2.0) - 10.0 - 1.0
    )
    # Corner to corner with another rounded pad and with a via.
    other = deepcopy(pad)
    other.x1 = other.x2 = 60.0
    other.y1 = other.y2 = 90.0
    assert index._primitive_distance(pad, other) == pytest.approx(
        math.hypot(40.0, 50.0) - 20.0
    )
    via = Primitive(
        kind="via", layer=1, layer_to=32, net=2, component=None,
        x1=30.0, y1=40.0, x2=30.0, y2=40.0, width=4.0, hole=2.0,
    )
    assert index._primitive_distance(via, pad) == pytest.approx(
        math.hypot(20.0, 20.0) - 10.0 - 2.0
    )


def test_rounded_rectangle_pad_rotated(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    # Turned 90 degrees the pad spans x +-30, y +-20; corner arc at (20, 10).
    pad = _rounded_pad(corner=10.0, rotation=90.0)
    assert index._point_primitive_distance(40.0, 30.0, pad) == pytest.approx(
        math.hypot(20.0, 20.0) - 10.0
    )
    assert index._primitive_bounds(pad) == pytest.approx(
        (-30.0, -20.0, 30.0, 20.0)
    )


@pytest.mark.parametrize(
    "corner, same_as",
    [(0.0, PAD_SHAPE_RECT), (None, PAD_SHAPE_RECT), (20.0, PAD_SHAPE_ROUND),
     (35.0, PAD_SHAPE_ROUND)],
)
def test_rounded_rectangle_pad_limits(pcb_data, corner, same_as):
    index = PcbIndex(pcb_data, NETLIST)
    pad = _rounded_pad(corner=corner)
    reference = deepcopy(pad)
    reference.shape = same_as
    for point in [(30.0, 40.0), (19.5, 29.5), (25.0, 3.0), (5.0, 45.0)]:
        assert index._point_primitive_distance(
            *point, pad
        ) == pytest.approx(index._point_primitive_distance(*point, reference))


def test_rounded_rectangle_pad_at_an_angle_and_overlapping(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    pad = _rounded_pad(corner=10.0, rotation=30.0)
    cosine, sine = math.cos(math.radians(30.0)), math.sin(math.radians(30.0))
    # (30, 40) in the pad's own frame, turned with it.
    x, y = 30.0 * cosine - 40.0 * sine, 30.0 * sine + 40.0 * cosine
    assert index._point_primitive_distance(x, y, pad) == pytest.approx(
        math.hypot(20.0, 20.0) - 10.0
    )
    # Core 20 x 40 turned 30 degrees, grown by 10.
    half_x = 10.0 * cosine + 20.0 * sine + 10.0
    half_y = 10.0 * sine + 20.0 * cosine + 10.0
    assert index._primitive_bounds(pad) == pytest.approx(
        (-half_x, -half_y, half_x, half_y)
    )
    inside = _rounded_pad(corner=1.0)
    inside.width = inside.height = 4.0
    assert index._primitive_distance(pad, inside) == 0.0
    assert index._primitive_distance(inside, pad) == 0.0
    overlapping = _rounded_pad(corner=10.0, rotation=30.0)
    overlapping.x1 = overlapping.x2 = 30.0 * cosine
    overlapping.y1 = overlapping.y2 = 30.0 * sine
    assert index._primitive_distance(pad, overlapping) == 0.0


def test_full_corner_radius_is_an_oblong(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    pad = _rounded_pad(corner=20.0 - 1e-12)
    segment, radius, polygon, arc = index._primitive_geometry(pad)
    assert polygon is None and arc is None
    assert radius == pytest.approx(20.0)
    assert segment == pytest.approx((0.0, -10.0, 0.0, 10.0))


def test_rounded_rectangle_row_without_corner_column(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    row = {
        "kind": "pad", "x1": 0.0, "y1": 0.0, "x2": 0.0, "y2": 0.0,
        "width": 40.0, "height": 60.0, "rotation": 0.0,
        "shape": PAD_SHAPE_ROUNDRECT,
    }
    assert index._point_primitive_distance(19.5, 29.5, row) == 0.0


def test_rounded_rectangle_pad_in_index_queries(pcb_data):
    data = deepcopy(pcb_data)
    data.nets.extend(["ROUNDED_PAD", "CORNER_TRACK"])
    data.components.append(
        PcbComponent(
            index=3,
            refdes="J1",
            source_uid_tail=None,
            channel_path=None,
            x=0.0,
            y=0.0,
            rotation=0.0,
            side="top",
        )
    )
    data.primitives.extend([
        _rounded_pad(corner=10.0, net=3, component=3),
        # 10.2 mil from the rounded corner, 6.1 mil from a sharp one.
        _track(net=4, layer=1, x1=15.0, y1=45.0, x2=35.0, y2=25.0, width=2.0),
    ])
    index = PcbIndex(data, NETLIST)
    rows = index.db.execute(
        "SELECT shape, corner_radius FROM prims WHERE kind = 'pad' "
        "AND net_id = 3"
    ).fetchall()
    assert [tuple(row) for row in rows] == [(PAD_SHAPE_ROUNDRECT, 10.0)]
    component = index.find_components("J1")[0]
    assert (component["minx"], component["maxy"]) == pytest.approx(
        (-20.0, 30.0)
    )
    neighbors = index.net_neighbors("ROUNDED_PAD", distance=8.0)
    assert "CORNER_TRACK" not in [
        neighbor["net"] for neighbor in neighbors["neighbors"]
    ]
    neighbors = index.net_neighbors("ROUNDED_PAD", distance=11.0)
    assert "CORNER_TRACK" in [
        neighbor["net"] for neighbor in neighbors["neighbors"]
    ]
    assert "ROUNDED_PAD" not in [
        net["net"] for net in index.region_query(19.5, 29.5, radius=1.0)["nets"]
    ]
    assert "ROUNDED_PAD" in [
        net["net"] for net in index.region_query(19.5, 25.0, radius=1.0)["nets"]
    ]


def _stack_pad(net, x=0.0, y=0.0, component=None):
    """A Multi-Layer pad: 60 mil round on top and bottom, 40 mil round on
    the inner layers but L3-SIG, where it is an 80 x 20 mil rectangle."""
    pad = _pad(net=net, component=component, x=x, y=y, layer=74, width=60.0)
    pad.layer_copper = {
        PAD_INNER_LAYERS: PadCopper(40.0, 40.0, PAD_SHAPE_ROUND),
        3: PadCopper(80.0, 20.0, PAD_SHAPE_RECT),
    }
    return pad


def test_multi_layer_pad_geometry_per_layer(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    pad = _stack_pad(net=0)
    # Bounds cover every layer: the L3-SIG rectangle is the wider.
    assert index._primitive_bounds(pad) == pytest.approx((-40.0, -30.0, 40.0, 30.0))
    # Without layers a pad has its main (top-layer) copper.
    assert index._point_primitive_distance(35.0, 0.0, pad) == pytest.approx(5.0)
    assert index._point_primitive_distance(35.0, 0.0, pad, [2]) == pytest.approx(15.0)
    assert index._point_primitive_distance(35.0, 0.0, pad, [3]) == 0.0
    assert index._point_primitive_distance(35.0, 0.0, pad, [1, 2]) == pytest.approx(5.0)
    assert sorted(
        (distance, tuple(group))
        for distance, group in index._point_layer_distances(
            35.0, 0.0, pad, [1, 2, 3, 32]
        )
    ) == [(0.0, (3,)), (pytest.approx(5.0), (1, 32)), (pytest.approx(15.0), (2,))]
    # A track on L2-GND 35 mil off the centre (2 mil wide).
    track = _track(net=1, layer=2, x1=35.0, y1=-50.0, x2=35.0, y2=50.0, width=2.0)
    assert index._primitive_distance(pad, track, [2]) == pytest.approx(14.0)
    assert index._primitive_distance(track, pad, [2]) == pytest.approx(14.0)
    assert index._primitive_distance(pad, track) == pytest.approx(4.0)
    # Pad to pad, 100 mil apart: closest on top and bottom, unless only
    # inner layers are shared.
    other = _stack_pad(net=1, x=100.0)
    assert index._closest_layers(pad, other, [1, 2, 32]) == (
        pytest.approx(40.0), [1, 32]
    )
    assert index._closest_layers(pad, other, [2, 3]) == (pytest.approx(20.0), [3])
    # A pad without per-layer copper measures once for every layer given.
    simple = _pad(net=1, component=None, x=100.0, y=0.0, layer=74, width=60.0)
    distance, layers = index._closest_layers(simple, track, {1, 2})
    assert (distance, sorted(layers)) == (pytest.approx(34.0), [1, 2])
    assert index._closest_layers(simple, track) == (pytest.approx(34.0), None)


def test_multi_layer_pad_in_index_queries(pcb_data):
    data = deepcopy(pcb_data)
    data.nets.extend(["PTH", "L2_NET", "L3_NET"])
    data.board.layers.append(_layer(74, "Multi-Layer", None, kind="non-stack"))
    data.primitives.extend([
        _stack_pad(net=3, x=700.0, y=700.0),
        # 14 mil from the L2-GND copper (4 mil from the top-layer size).
        _track(net=4, layer=2, x1=735.0, y1=650.0, x2=735.0, y2=750.0, width=2.0),
        # 4 mil from the L3-SIG rectangle, beyond the top-layer size's reach.
        _track(net=5, layer=3, x1=745.0, y1=650.0, x2=745.0, y2=750.0, width=2.0),
    ])
    index = PcbIndex(data, NETLIST)
    row = index.db.execute("SELECT * FROM prims WHERE net_id = 3").fetchone()
    assert (row["minx"], row["maxx"]) == pytest.approx((660.0, 740.0))
    assert index._primitive_distance(
        row, index.db.execute("SELECT * FROM prims WHERE net_id = 4").fetchone(), [2]
    ) == pytest.approx(14.0)

    def neighbors(distance):
        return {
            (neighbor["net"], neighbor["layer"]): neighbor["min_edge_distance_mil"]
            for neighbor in index.net_neighbors("PTH", distance=distance)["neighbors"]
        }

    assert neighbors(10.0) == {("L3_NET", "L3-SIG"): 4.0}
    assert neighbors(15.0) == {
        ("L2_NET", "L2-GND"): 14.0, ("L3_NET", "L3-SIG"): 4.0,
    }
    # 4 mil above the top and bottom copper, 14 / 24 mil from the inner one.
    (net,) = index.region_query(700.0, 734.0, radius=5.0)["nets"]
    assert (net["net"], net["min_distance_mil"]) == ("PTH", 4.0)
    assert net["layers"] == ["Bottom Layer", "Top Layer"]
    assert index.region_query(700.0, 734.0, radius=5.0, layer=2)["nets"] == []
    inner = index.region_query(700.0, 734.0, radius=15.0, layer=2)["nets"]
    assert [(net["net"], net["min_distance_mil"], net["layers"]) for net in inner] == [
        ("PTH", 14.0, ["L2-GND"])
    ]


def test_region_query_caps_pours(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    index.MAX_REGION_RESULTS = 0
    result = index.region_query(150.0, 100.0, radius=60.0)
    assert result["nets"] == []
    assert result["components"] == []
    assert result["pours"] == []
    assert result["net_count"] > 0
    assert result["component_count"] > 0
    assert result["pour_count"] > 0
    assert result["nets_has_more"] is True
    assert result["components_has_more"] is True
    assert result["pours_has_more"] is True


def test_component_detail(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    details = index.component_detail("U3B")
    assert len(details) == 1
    instance = details[0]
    assert (instance["refdes"], instance["sch_refdes"]) == ("U3", "U3B")
    assert instance["channel_path"] == "top\\VS\\Bottom"
    assert instance["x_mil"] == 500.0
    assert {pin["pin"] for pin in instance["pins"]} == {"1", "2"}
    pin_1 = next(pin for pin in instance["pins"] if pin["pin"] == "1")
    assert pin_1["net"] == "CLK_B"
    assert "R1" in [
        component["refdes"]
        for component in instance["nearest_components"]
    ]


def test_component_detail_ambiguous_returns_all(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    assert len(index.component_detail("U3")) == 2


def test_component_detail_not_found(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    assert index.component_detail("U99") == []


# ---------- PCB designator vs SOURCEDESIGNATOR ----------


def _component(index, refdes, designator, uid, x, y, channel_path=None):
    return PcbComponent(
        index=index,
        refdes=refdes,
        source_uid_tail=uid,
        channel_path=channel_path,
        x=x,
        y=y,
        rotation=0.0,
        side="top",
        designator=designator,
    )


@pytest.fixture
def annotated_pcb(pcb_data):
    """Board-level annotation: SOURCEDESIGNATOR is the logical name.

    Shaped like Altium's Bluetooth_Sentinel example, where the board's U3
    is the schematic sheet's U1 and the board's U1 is the sheet's U4.
    """
    data = deepcopy(pcb_data)
    data.components = [
        _component(0, "U1", "U3", "ACCEL", 100.0, 100.0, "Top\\Accel"),
        _component(1, "U4", "U1", "RADIO", 500.0, 100.0, "Top\\Radio"),
        _component(2, "U3", "U2", "EEPROM", 300.0, 300.0, "Top\\Eeprom"),
        # PCB-only part: a designator but no schematic source.
        _component(3, None, "MH1", None, 900.0, 900.0),
    ]
    data.primitives = [
        _pad(net=1, component=0, x=100.0, y=100.0),
        _pad(net=0, component=0, x=100.0, y=110.0, name="2"),
        _pad(net=2, component=1, x=500.0, y=100.0),
        _pad(net=0, component=1, x=500.0, y=110.0, name="2"),
        _pad(net=0, component=2, x=300.0, y=300.0),
        _pad(net=None, component=3, x=900.0, y=900.0),
    ]
    return data


# The netlist names physical designators. Its components come from the
# sheets' logical names, so U1 carries the accelerometer's UniqueId and the
# sheet's U4 (no physical U4) has no pins: what enrich_netlist produces when
# it cannot read the project's .Annotation file.
ANNOTATED_NETLIST = {
    "pin_to_net": {
        "U1": {"1": "CLK_B", "2": "GND"},
        "U2": {"1": "GND"},
        "U3": {"1": "CLK_A", "2": "GND"},
    },
    "components": {
        "U1": {"unique_id": "ACCEL", "pins": {
            "1": {"net": "CLK_B"}, "2": {"net": "GND"},
        }},
        "U3": {"unique_id": "EEPROM", "pins": {
            "1": {"net": "CLK_A"}, "2": {"net": "GND"},
        }},
        "U4": {"unique_id": "RADIO", "pins": {}},
    },
}


def test_refdes_is_the_pcb_designator(annotated_pcb):
    index = PcbIndex(annotated_pcb, ANNOTATED_NETLIST)
    rows = index.find_components("U1")
    assert [
        (row["refdes"], row["logical_refdes"], row["sch_refdes"])
        for row in rows
    ] == [("U1", "U4", "U1")]
    assert index.find_components("U3")[0]["channel_path"] == "Top\\Accel"
    assert index.find_components("mh1")[0]["refdes"] == "MH1"


def test_netlist_matches_pcb_designator_before_source_designator(
    annotated_pcb,
):
    index = PcbIndex(annotated_pcb, ANNOTATED_NETLIST)
    mapping = {
        row["logical_refdes"]: row["sch_refdes"]
        for row in index.db.execute(
            "SELECT logical_refdes, sch_refdes FROM components "
            "WHERE logical_refdes IS NOT NULL"
        )
    }
    # Matching SOURCEDESIGNATOR would give U1, U4 and U3: other parts.
    assert mapping == {"U1": "U3", "U4": "U1", "U3": "U2"}
    assert index.unmatched_components() == 1
    assert index.unmatched_refdes(10) == ["MH1"]
    assert index.renamed_components() == 3


def test_logical_refdes_is_a_fallback_lookup(annotated_pcb):
    index = PcbIndex(annotated_pcb, ANNOTATED_NETLIST)
    # U4 names no PCB designator or netlist part: the sheet's U4 is found.
    assert [row["refdes"] for row in index.find_components("U4")] == ["U1"]
    assert index.find_components("U9") == []


def test_tool_outputs_name_pcb_designators(annotated_pcb):
    index = PcbIndex(annotated_pcb, ANNOTATED_NETLIST)
    pads = index.net_summary("CLK_A")["pads"]
    assert [(pad["refdes"], pad["sch_refdes"]) for pad in pads] == [
        ("U3", "U3")
    ]
    region = index.region_query(100.0, 100.0, radius=5.0)
    assert [item["refdes"] for item in region["components"]] == ["U3"]
    detail = index.component_detail("U3")
    assert len(detail) == 1
    assert (
        detail[0]["refdes"],
        detail[0]["sch_refdes"],
        detail[0]["logical_refdes"],
    ) == ("U3", "U3", "U1")
    assert "U1" in [
        item["refdes"] for item in detail[0]["nearest_components"]
    ]
    assert index.find_by_designator("u2") == [
        {"refdes": "U2", "logical_refdes": "U3", "sch_refdes": "U2"}
    ]


def test_fallbacks_skip_netlist_parts_claimed_by_a_designator(
    annotated_pcb,
):
    # The board's U3 was renamed U7 on the PCB only, so U7 is not in the
    # netlist. Its SOURCEDESIGNATOR U1 and UniqueId point at the netlist's
    # U1, which the board's own U1 already is.
    annotated_pcb.components[0].designator = "U7"
    netlist = deepcopy(ANNOTATED_NETLIST)
    index = PcbIndex(annotated_pcb, netlist)
    assert index.find_components("U7")[0]["sch_refdes"] is None
    assert index.find_components("U1")[0]["logical_refdes"] == "U4"


def test_fallback_to_unique_id_skips_claimed_candidates(annotated_pcb):
    # The board's U2 was renamed U8 on the PCB only. Its UniqueId names the
    # netlist's U3 (claimed by the board's U3) and U2: the pad nets pick U2.
    annotated_pcb.components[2].designator = "U8"
    netlist = deepcopy(ANNOTATED_NETLIST)
    netlist["components"]["U2"] = {
        "unique_id": "EEPROM",
        "pins": {"1": {"net": "GND"}},
    }
    index = PcbIndex(annotated_pcb, netlist)
    assert index.find_components("U8")[0]["sch_refdes"] == "U2"


def test_fallback_to_source_designator_unless_unique_id_differs(
    annotated_pcb,
):
    # Logical U5, renamed U8 on the PCB only; its UniqueId's one netlist
    # part (U3) is the board's U3, so SOURCEDESIGNATOR U5 is tried.
    annotated_pcb.components[2].refdes = "U5"
    annotated_pcb.components[2].designator = "U8"
    netlist = deepcopy(ANNOTATED_NETLIST)
    netlist["pin_to_net"]["U5"] = netlist["pin_to_net"].pop("U2")
    index = PcbIndex(annotated_pcb, netlist)
    assert index.find_components("U8")[0]["sch_refdes"] == "U5"

    netlist["components"]["U5"] = {"unique_id": "OTHER", "pins": {}}
    index = PcbIndex(annotated_pcb, netlist)
    assert index.find_components("U8")[0]["sch_refdes"] is None


def test_multichannel_pcb_designators(pcb_data):
    data = deepcopy(pcb_data)
    data.components[0].designator = "U3A"
    data.components[1].designator = "U3B"
    index = PcbIndex(data, NETLIST)
    assert [
        (row["refdes"], row["sch_refdes"])
        for row in index.find_components("U3B")
    ] == [("U3B", "U3B")]
    # The sheet's U3 returns both channel instances.
    assert sorted(
        row["refdes"] for row in index.find_components("U3")
    ) == ["U3A", "U3B"]
    assert index.renamed_components() == 2


def decoded_arc(net, cx, cy, radius, start, end, width, layer=1):
    """The chord rows decode_arcs makes of one Arcs6 record."""
    payload = bytearray(45)
    payload[0] = layer
    struct.pack_into("<HHH", payload, 3, net, 0xFFFF, 0xFFFF)
    struct.pack_into(
        "<iii", payload, 13, round(cx * 1e4), round(cy * 1e4), round(radius * 1e4)
    )
    struct.pack_into("<dd", payload, 25, start, end)
    struct.pack_into("<i", payload, 41, round(width * 1e4))
    chords, _ = decode_arcs(b"\x01" + struct.pack("<I", len(payload)) + bytes(payload))
    return chords


def _closest(index, first, second):
    return min(index._primitive_distance(a, b) for a in first for b in second)


def _as_chords(rows):
    return [replace(row, arc_radius=None) for row in rows]


# A GND bend: radius 30 mil, 8 mil wide, over the top half of a circle about
# (500, 500). Its copper spans radii 26 to 34; the chords lie up to ~0.2 mil
# inside it.
def _bend():
    return decoded_arc(0, 500.0, 500.0, 30.0, 0.0, 180.0, 8.0)


def test_via_inside_a_bend_measures_exactly(pcb_data):
    bend = _bend()
    via = Primitive(kind="via", layer=1, layer_to=32, net=1, component=None,
                    x1=500.0, y1=500.0, x2=500.0, y2=500.0, width=20.0, hole=8.0)
    data = replace(pcb_data, primitives=bend + [via])
    index = PcbIndex(data, NETLIST)
    assert _closest(index, bend, [via]) == pytest.approx(16.0, abs=1e-9)
    assert _closest(index, _as_chords(bend), [via]) < 15.8
    (gnd,) = index.net_neighbors("CLK_A", 20.0)["neighbors"]
    assert (gnd["net"], gnd["min_edge_distance_mil"]) == ("GND", 16.0)
    near = {
        entry["net"]: entry["min_distance_mil"]
        for entry in index.region_query(500.0, 500.0, 30.0)["nets"]
    }
    assert near == {"CLK_A": 0.0, "GND": 26.0}


def test_tracks_and_pads_measure_to_the_arc(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    bend = _bend()
    # Above the apex (the chord through it is lower): 40 - 30 - 4 - 3.
    above = _track(1, 1, 490.0, 540.0, 510.0, 540.0)
    assert _closest(index, bend, [above]) == pytest.approx(3.0)
    assert _closest(index, _as_chords(bend), [above]) > 3.1
    # In the bend: its ends are closest, radially.
    inside = _track(1, 1, 490.0, 520.0, 510.0, 520.0)
    assert _closest(index, bend, [inside]) == pytest.approx(30 - math.sqrt(500) - 7)
    # A rectangular pad above the apex and a turned square one in the bend.
    pad = _pad(1, None, 500.0, 545.0, shape=PAD_SHAPE_RECT)
    assert _closest(index, bend, [pad]) == pytest.approx(6.0)
    turned = _pad(1, None, 500.0, 500.0, width=20.0, shape=PAD_SHAPE_RECT, rotation=45.0)
    assert _closest(index, bend, [turned]) == pytest.approx(26 - 10 * math.sqrt(2))
    assert _closest(index, _as_chords(bend), [turned]) < 26 - 10 * math.sqrt(2) - 0.1


def test_arc_to_arc_measures_exactly(pcb_data):
    index = PcbIndex(pcb_data, NETLIST)
    bend = _bend()
    # Concentric, 15 mil further out and 6 mil wide: 15 - 4 - 3. Its chords
    # are split differently, so chord-to-chord reads anything near that.
    outer = decoded_arc(1, 500.0, 500.0, 45.0, 0.0, 180.0, 6.0)
    assert _closest(index, bend, outer) == pytest.approx(8.0)
    assert _closest(index, _as_chords(bend), _as_chords(outer)) != pytest.approx(8.0)
    # Facing it apex to apex: 100 - 2 * 30 - 4 - 4.
    facing = decoded_arc(1, 500.0, 600.0, 30.0, 180.0, 360.0, 8.0)
    assert _closest(index, bend, facing) == pytest.approx(32.0)


def test_arc_rows_are_bounded_by_the_arc(pcb_data):
    data = replace(pcb_data, primitives=_bend())
    index = PcbIndex(data, NETLIST)
    top = index.db.execute("SELECT MAX(maxy) FROM prims WHERE kind = 'arc'").fetchone()[0]
    assert top == pytest.approx(534.0)
    centre = index.db.execute(
        "SELECT DISTINCT arc_x, arc_y, arc_radius FROM prims WHERE kind = 'arc'"
    ).fetchall()
    assert [tuple(row) for row in centre] == [(500.0, 500.0, 30.0)]
