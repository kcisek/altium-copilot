"""Local-only check of rule/class/pair decoding on a real board.

Set ALTIUM_COPILOT_ORACLE_PCBDOC to a .PcbDoc path (e.g. one of Altium's
shipped examples) to run. Nothing from the board is committed.
"""

import os
import struct

import olefile
import pytest

from server.parsers.pcb_doc import parse_pcb_doc
from server.services.pcb_index import PcbIndex
from server.services.pcb_rules import parse_scope, rule_values

PCBDOC = os.environ.get("ALTIUM_COPILOT_ORACLE_PCBDOC")

pytestmark = pytest.mark.skipif(
    not PCBDOC, reason="set ALTIUM_COPILOT_ORACLE_PCBDOC to run"
)


def test_every_declared_rule_decodes_and_every_scope_parses():
    with olefile.OleFileIO(PCBDOC) as ole:
        (declared,) = struct.unpack("<I", ole.openstream("Rules6/Header").read()[:4])
    parsed = parse_pcb_doc(PCBDOC)
    assert len(parsed.rules) == declared
    assert "Rules6" not in parsed.skipped_records
    index = PcbIndex(parsed, None)
    layers = index.signal_layers()
    for rule in parsed.rules:
        parse_scope(rule.scope1)
        parse_scope(rule.scope2)
        rule_values(rule, layers)


def test_pairs_and_class_members_resolve_to_nets():
    parsed = parse_pcb_doc(PCBDOC)
    nets = {name.lower() for name in parsed.nets}
    for pair in parsed.diff_pairs:
        assert pair.positive_net.lower() in nets
        assert pair.negative_net.lower() in nets
    index = PcbIndex(parsed, None)
    for net in parsed.nets[:200]:
        assert index.rule_target(net) is not None
