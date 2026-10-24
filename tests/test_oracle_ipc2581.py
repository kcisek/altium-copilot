"""Cross-validate PcbDoc parsing against an IPC-2581 export.

This local-only test is skipped unless both oracle paths are supplied through
environment variables. Never commit oracle files or values derived from them.
"""

import os
from collections import Counter

import pytest

from server.parsers.fab_outputs import parse_ipc2581
from server.parsers.pcb_doc import parse_pcb_doc

PCBDOC = os.environ.get("ALTIUM_COPILOT_ORACLE_PCBDOC")
CVG = os.environ.get("ALTIUM_COPILOT_ORACLE_CVG")

pytestmark = pytest.mark.skipif(
    not (PCBDOC and CVG and os.path.exists(PCBDOC) and os.path.exists(CVG)),
    reason="oracle env vars not set",
)


@pytest.fixture(scope="module")
def parsed():
    return parse_pcb_doc(PCBDOC)


@pytest.fixture(scope="module")
def ipc():
    export = parse_ipc2581(CVG)
    return {
        "nets": export.net_names,
        "refdes": {component.refdes for component in export.components},
        # thickness in mil (the parser converts from the file's units)
        "stackup": [(layer.name, layer.thickness_mil or 0.0) for layer in export.stackup],
        "span_count": export.span_count,
        "via_hole_count": sum(
            hole.kind == "LayerHole" and hole.plating == "VIA" for hole in export.holes
        ),
    }


def test_net_names_match(parsed, ipc):
    pcb_nets = set(parsed.nets)
    ipc_nets = {
        net
        for net in ipc["nets"]
        if net and net.lower() != "no net"
    }
    missing = ipc_nets - pcb_nets
    assert not missing, (
        f"nets in IPC-2581 but not parsed: {sorted(missing)[:10]}"
    )


def test_component_refdes_match(parsed, ipc):
    pcb_refdes = Counter(
        component.refdes
        for component in parsed.components
        if component.refdes
    )
    normalized_ipc = Counter()
    for refdes in ipc["refdes"]:
        if refdes in pcb_refdes:
            normalized_ipc[refdes] += 1
        elif (
            refdes[-1:].isalpha()
            and refdes[:-1] in pcb_refdes
        ):
            normalized_ipc[refdes[:-1]] += 1
        else:
            normalized_ipc[refdes] += 1

    assert len(ipc["refdes"]) == len(parsed.components)
    assert not (pcb_refdes - normalized_ipc)
    designatorless = sum(
        component.refdes is None for component in parsed.components
    )
    assert sum((normalized_ipc - pcb_refdes).values()) == designatorless


def test_copper_stack_layer_names_match(parsed, ipc):
    parsed_copper = [
        layer.name
        for layer in parsed.board.layers
        if layer.kind == "copper"
    ]
    ipc_names = [name for name, _thickness in ipc["stackup"]]
    for name in parsed_copper:
        assert name in ipc_names


def test_copper_thicknesses_match(parsed, ipc):
    ipc_thickness = dict(ipc["stackup"])
    for layer in parsed.board.layers:
        if layer.kind == "copper" and layer.copper_thick_mil:
            assert ipc_thickness[layer.name] == pytest.approx(
                layer.copper_thick_mil,
                rel=0.01,
            )


def test_via_count_matches(parsed, ipc):
    parsed_vias = sum(
        primitive.kind == "via" for primitive in parsed.primitives
    )
    # Every LayerHole has a Span, including component and NPTH holes. IPC's
    # platingStatus="VIA" is the stable subset corresponding to Vias6.
    assert ipc["span_count"] >= ipc["via_hole_count"]
    assert parsed_vias == ipc["via_hole_count"]


def test_no_parse_warnings(parsed):
    assert parsed.warnings == []
    assert parsed.skipped_records == {}
