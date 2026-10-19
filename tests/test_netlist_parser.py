import pytest
from server.netlist_parser import parse_protel_netlist

SAMPLE_NET = """\
[
BT1
HOLDER_BATT_CR2032_HARWIN_S8421-45R
S8421-45R



]
[
C1A
C0402_0.55MM
CGA2B3X7R1H104M050BB



]
(
Y15B
D21C-3
R216C-2
U9B-16
)
(
GND
C1A-2
U1-7
R216C-1
)
(
VBAT
D21C-1
C1A-1
)
"""


@pytest.fixture
def net_file(tmp_path):
    f = tmp_path / "test.NET"
    f.write_text(SAMPLE_NET, encoding="utf-8")
    return str(f)


def test_nets_extracted(net_file):
    result = parse_protel_netlist(net_file)
    assert "Y15B" in result["nets"]
    assert "GND" in result["nets"]
    assert "VBAT" in result["nets"]


def test_net_pins_correct(net_file):
    result = parse_protel_netlist(net_file)
    assert ("D21C", "3") in result["nets"]["Y15B"]
    assert ("R216C", "2") in result["nets"]["Y15B"]
    assert ("U9B", "16") in result["nets"]["Y15B"]


def test_component_blocks_skipped(net_file):
    # [BT1 ...] and [C1A ...] are component definitions — not nets
    result = parse_protel_netlist(net_file)
    assert "BT1" not in result["nets"]
    assert "C1A" not in result["nets"]
    assert "HOLDER_BATT_CR2032_HARWIN_S8421-45R" not in result["nets"]


def test_pin_to_net_inverted_index(net_file):
    result = parse_protel_netlist(net_file)
    assert result["pin_to_net"]["D21C"]["3"] == "Y15B"
    assert result["pin_to_net"]["D21C"]["1"] == "VBAT"
    assert result["pin_to_net"]["C1A"]["2"] == "GND"
    assert result["pin_to_net"]["C1A"]["1"] == "VBAT"
    assert result["pin_to_net"]["R216C"]["2"] == "Y15B"
    assert result["pin_to_net"]["R216C"]["1"] == "GND"


def test_refdes_suffix_treated_as_distinct(net_file):
    # C1A is a separate component from C1B — not grouped
    result = parse_protel_netlist(net_file)
    assert "C1A" in result["pin_to_net"]
    # C1B doesn't appear in this sample — that's correct
    assert "C1B" not in result["pin_to_net"]


def test_split_on_last_hyphen(tmp_path):
    # RefDes like "U9B" pin "16" — last '-' splits correctly
    content = "(\nNET1\nU9B-16\n)\n"
    f = tmp_path / "test.NET"
    f.write_text(content, encoding="utf-8")
    result = parse_protel_netlist(str(f))
    assert ("U9B", "16") in result["nets"]["NET1"]
    assert result["pin_to_net"]["U9B"]["16"] == "NET1"


def test_empty_lines_ignored(tmp_path):
    content = "(\n\nNET1\n\nR1-1\n\nR1-2\n\n)\n"
    f = tmp_path / "test.NET"
    f.write_text(content, encoding="utf-8")
    result = parse_protel_netlist(str(f))
    assert ("R1", "1") in result["nets"]["NET1"]
    assert ("R1", "2") in result["nets"]["NET1"]


def test_part_blocks_give_footprint_and_comment(net_file):
    parts = parse_protel_netlist(net_file)["parts"]
    # BT1 is on no net but is still a compiled part.
    assert parts == {
        "BT1": {"footprint": "HOLDER_BATT_CR2032_HARWIN_S8421-45R", "comment": "S8421-45R"},
        "C1A": {"footprint": "C0402_0.55MM", "comment": "CGA2B3X7R1H104M050BB"},
    }


def test_part_block_lines_are_positional(tmp_path):
    f = tmp_path / "p.NET"
    f.write_text("[\nJ1\n\nNo footprint\n\n\n\n]\n[\nTP1\nTP_PAD\n\n\n\n\n]\n", encoding="utf-8")
    parts = parse_protel_netlist(str(f))["parts"]
    assert parts["J1"] == {"footprint": "", "comment": "No footprint"}
    assert parts["TP1"] == {"footprint": "TP_PAD", "comment": ""}


def test_ansi_netlist_keeps_symbols(tmp_path):
    f = tmp_path / "ansi.NET"
    f.write_bytes("[\nC1\nC0603\n4.7µF\n\n\n\n]\n(\nNET1\nC1-1\n)\n".encode("cp1252"))
    result = parse_protel_netlist(str(f))
    assert result["parts"]["C1"]["comment"] == "4.7µF"
    assert result["nets"] == {"NET1": [("C1", "1")]}
