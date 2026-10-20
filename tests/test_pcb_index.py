import math
from copy import deepcopy

import pytest

from server.parsers.pcb_doc import (
    PAD_SHAPE_RECT,
    PAD_SHAPE_ROUND,
    PAD_SHAPE_ROUNDRECT,
    BoardInfo,
    PcbComponent,
    PcbData,
    PcbLayer,
    PolygonPour,
    Primitive,
)
from server.services.pcb_index import PcbIndex


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
    segment, radius, polygon = index._primitive_geometry(pad)
    assert polygon is None
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
