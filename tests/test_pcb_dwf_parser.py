"""Draftsman (.PCBDwf) reader: LZ4Stream unpacking and XML extraction.

The fixture is synthetic (see tests/draftsman_fixture.py). Point
ALTIUM_COPILOT_ORACLE_PCBDWF at a real .PCBDwf of your own to also check a file
Altium wrote; never commit it.
"""
import os
import random
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from parsers.pcb_dwf import _without_board_copy, decode_pcb_dwf, parse_pcb_dwf, read_pcb_dwf_picture
from tests.draftsman_fixture import FIXTURE, TINY_PNG, build, drawing_xml, lz4_stream


@pytest.fixture(scope="module")
def drawing():
    return parse_pcb_dwf(str(FIXTURE))


# ---------- LZ4Stream ----------

def test_fixture_is_up_to_date():
    assert FIXTURE.read_bytes() == build(), "run python tests/draftsman_fixture.py"


def test_decode_fixture_multi_chunk_with_raw_chunk():
    assert decode_pcb_dwf(FIXTURE.read_bytes()) == drawing_xml().encode("utf-8")


@pytest.mark.parametrize("chunk_size", [7, 64, 4096])
def test_decode_round_trip_overlapping_matches(chunk_size):
    rng = random.Random(5)
    data = b"a" * 700 + bytes(rng.randrange(256) for _ in range(300)) + b"abcabcabc" * 90 + b"tail"
    assert decode_pcb_dwf(lz4_stream(data, chunk_size=chunk_size)) == data


def test_decode_empty():
    assert decode_pcb_dwf(b"") == b""


def test_decode_truncated_raises():
    packed = lz4_stream(b"hello world " * 50)
    with pytest.raises(ValueError):
        decode_pcb_dwf(packed[:-10])


def test_decode_bad_match_offset_raises():
    # flags=1, length=8, size=3: token 0x04 (no literals, match) then offset 5 > 0 decoded bytes
    with pytest.raises(ValueError, match="offset"):
        decode_pcb_dwf(bytes([1, 8, 3, 0x04, 5, 0]))


def test_decode_length_mismatch_raises():
    with pytest.raises(ValueError, match="expected"):
        decode_pcb_dwf(bytes([0, 9]) + b"short")


def test_not_a_draftsman_document(tmp_path):
    path = tmp_path / "other.PCBDwf"
    path.write_bytes(lz4_stream(b"<Something xmlns='urn:x'/>"))
    with pytest.raises(ValueError, match="Draftsman"):
        parse_pcb_dwf(str(path))


# ---------- document content ----------

def test_document_metadata(drawing):
    assert drawing.pages == ["A4", "A3", "custom"]
    assert drawing.source_pcb == "C:\\Projects\\Demo\\Demo.PcbDoc"
    assert drawing.last_update.startswith("2026-10-01T12:00:00")
    assert drawing.parameters == {"Revision": "B"}  # empty values are dropped


def test_note_lines_skip_empty_entries(drawing):
    note = next(i for i in drawing.items if i.kind == "note")
    assert note.title == "Notes:"
    assert note.lines[:2] == ["Board material FR-4, Tg 150 or higher", "Surface finish ENIG"]
    assert len(note.lines) == 3
    assert note.page == 1


def test_special_strings_take_the_values_the_drawing_cached(drawing):
    # =Total_Thickness from ParametersData/PCBParameters, =PrjRev from ProjectParameters
    # (also right after a letter, as "v=PrjRev"); a name without a value stays as typed.
    note = next(i for i in drawing.items if i.kind == "note")
    assert note.lines[2] == "Final thickness 1.6mm, part DEMO-v3, =Unknown_Param kept"


def test_free_text_and_template_text(drawing):
    texts = [(i.text, i.from_template) for i in drawing.items if i.kind == "text"]
    assert texts == [("Do not scale this drawing", False), ("Drawn by: J. Doe", True)]


def test_table_title_and_rows(drawing):
    table = next(i for i in drawing.items if i.kind == "table")
    assert table.title == "Impedance Table"
    assert table.rows == [["Layer", "Impedance"], ["L1", "50 ohm"]]


def test_picture_size_and_index(drawing):
    picture = next(i for i in drawing.items if i.kind == "picture")
    assert picture.picture_index == 0
    assert (picture.width_mm, picture.height_mm) == (50.0, 25.0)


def test_views_with_side_layer_and_resolved_title(drawing):
    views = [(i.page, i.view, i.side, i.layer, i.title) for i in drawing.items if i.kind == "view"]
    assert views == [
        # LayerV7Id 1 is IdInBoard 1 of the cached stack
        (1, "fabrication view", "Top", "Top Layer", "Top Layer (Scale =ViewScale)"),
        (2, "assembly view", "Bottom", None, "View from Bottom side (Scale 2:1)"),
        (2, "layer stack legend", None, None, "Layer Stack Legend"),
    ]


def test_unknown_items_are_ignored(drawing):
    assert {i.kind for i in drawing.items} == {"note", "text", "table", "picture", "view"}


def test_drill_table_groups_holes_in_mm(drawing):
    assert drawing.drill_table == [
        {"size_mm": 0.3, "plated": True, "type": "Via", "span": "Top Layer - Bottom Layer", "count": 3},
        {"size_mm": 1.0, "plated": True, "type": "Pad", "span": "Top Layer - Bottom Layer", "count": 1,
         "slot_length_mm": 2.5},
        {"size_mm": 3.2, "plated": False, "type": "Pad", "span": "Top Layer - Bottom Layer", "count": 2},
    ]


def test_layer_stack_notes_once_despite_the_second_stack_copy(drawing):
    # The fixture, like real drawings, also serialises the stack under LayerStack/Tuple...
    assert drawing.layer_notes == [
        {"layer": "Dielectric 1", "comment": "Controlled impedance core", "note": "Use Isola 370HR"}
    ]


def _with_layer_stacks(*stacks: str) -> str:
    body = "".join(
        f"<BoardLayerStack><Name>{name}</Name><Stack><StackLayerBoardLayerData><Comment>c</Comment>"
        f"<Name>Core</Name><Note/></StackLayerBoardLayerData></Stack></BoardLayerStack>"
        for name in stacks
    )
    xml = drawing_xml()
    start = xml.index("<LayerStackInformation>")
    end = xml.index("</LayerStackInformation>") + len("</LayerStackInformation>")
    return (xml[:start] + f"<LayerStackInformation><BoardLayerStackData>{body}</BoardLayerStackData>"
            "</LayerStackInformation>" + xml[end:])


def test_layer_notes_name_their_stack_when_there_are_several(tmp_path):
    path = tmp_path / "flex.PCBDwf"
    path.write_bytes(lz4_stream(_with_layer_stacks("Rigid", "Flex").encode()))
    assert parse_pcb_dwf(str(path)).layer_notes == [
        {"layer": "Core", "stack": "Rigid", "comment": "c"},
        {"layer": "Core", "stack": "Flex", "comment": "c"},
    ]


def test_board_copy_is_cut_before_parsing():
    xml = bytearray(drawing_xml().encode())
    assert b"board copy" in xml
    kept = _without_board_copy(xml)
    assert b"board copy" not in kept and b"<BoardAssemblyInformation" not in kept
    assert b'<BillOfMaterialsInformation i:nil="true"/>' in kept  # self-closing: left alone
    assert len(xml) - len(kept) == len(
        b"<BoardAssemblyInformation><Components><q:Part>board copy</q:Part></Components></BoardAssemblyInformation>"
    )


def test_pictures_are_kept_with_the_parse(drawing):
    assert len(drawing.pictures) == 1
    assert drawing.pictures[0].startswith(b"\x89PNG\r\n\x1a\n")
    assert "pictures" not in repr(drawing)


def test_read_picture_returns_png():
    assert read_pcb_dwf_picture(str(FIXTURE), 0).startswith(b"\x89PNG\r\n\x1a\n")


def test_read_picture_bad_index():
    with pytest.raises(KeyError):
        read_pcb_dwf_picture(str(FIXTURE), 1)


@pytest.mark.skipif(
    not os.environ.get("ALTIUM_COPILOT_ORACLE_PCBDWF"),
    reason="set ALTIUM_COPILOT_ORACLE_PCBDWF to a real .PCBDwf to run",
)
def test_real_draftsman_file_parses():
    path = Path(os.environ["ALTIUM_COPILOT_ORACLE_PCBDWF"])
    drawing = parse_pcb_dwf(str(path))
    assert drawing.pages
    for item in drawing.items:
        if item.kind == "picture":
            assert read_pcb_dwf_picture(str(path), item.picture_index)


def test_fixture_needs_the_board_copy_cut_to_parse():
    # Guards the wiring: the fixture's board copy is not valid XML on its own.
    with pytest.raises(ET.ParseError):
        ET.fromstring(drawing_xml())


@pytest.mark.parametrize("xml", [
    # Draftsman's empty form, then a same-named element elsewhere: nothing is cut
    b'<D><BoardAssemblyInformation i:nil="true"/><P>keep</P><BoardAssemblyInformation>x</BoardAssemblyInformation></D>',
    # a same-named element nested inside: the outer one is left alone
    b"<D><BoardAssemblyInformation><BoardAssemblyInformation>x</BoardAssemblyInformation></BoardAssemblyInformation><P>keep</P></D>",
    # no closing tag
    b"<D><BoardAssemblyInformation><P>keep</P></D>",
    # a longer tag that only starts with the name
    b"<D><BoardAssemblyInformationX>keep</BoardAssemblyInformationX></D>",
])
def test_board_copy_cut_leaves_unusual_elements_alone(xml):
    assert _without_board_copy(bytearray(xml)) == xml


def test_bad_picture_does_not_hide_the_drawing(tmp_path):
    path = tmp_path / "bad.PCBDwf"
    path.write_bytes(lz4_stream(drawing_xml().replace(TINY_PNG, TINY_PNG[:-1]).encode()))
    drawing = parse_pcb_dwf(str(path))
    assert drawing.pictures == [b""]
    assert any(i.kind == "note" for i in drawing.items)


def test_fabrication_view_without_cached_stack_uses_default_layer_names(tmp_path):
    xml = drawing_xml().replace("<LayerV7Id>1</LayerV7Id>", "<LayerV7Id>32</LayerV7Id>")
    start = xml.index("<LayerStackInformation>")
    end = xml.index("</LayerStackInformation>") + len("</LayerStackInformation>")
    xml = xml[:start] + '<LayerStackInformation i:nil="true"/>' + xml[end:]
    path = tmp_path / "led.PCBDwf"
    path.write_bytes(lz4_stream(xml.encode()))
    view = next(i for i in parse_pcb_dwf(str(path)).items if i.view == "fabrication view")
    assert view.layer == "Bottom Layer"
    assert view.title.startswith("Bottom Layer (Scale")


def test_variants_of_the_drawing_and_its_views(tmp_path, drawing):
    # The fixture stores none: the views follow the drawing, which names no variant.
    assert drawing.document_variant is None
    assert all(item.variant is None for item in drawing.items if item.kind == "view")
    xml = drawing_xml().replace(
        "<DocumentId>",
        "<DocumentOptions><AssemblyVariant>[All Variants]</AssemblyVariant>"
        "<AssemblyVariantV2>Prod</AssemblyVariantV2></DocumentOptions><DocumentId>")
    xml = xml.replace(
        "<ViewSide>Bottom</ViewSide></Item>",
        "<ViewComponentDisplaySettingsData><Variant>Lite</Variant></ViewComponentDisplaySettingsData>"
        "<ViewSide>Bottom</ViewSide></Item>")
    xml = xml.replace(
        "<UseCustomScale>true</UseCustomScale><ViewSide>Top</ViewSide></Item>",
        '<UseCustomScale>true</UseCustomScale><Variant i:nil="true"/><ViewSide>Top</ViewSide></Item>')
    path = tmp_path / "variants.PCBDwf"
    path.write_bytes(lz4_stream(xml.encode()))
    parsed = parse_pcb_dwf(str(path))
    assert parsed.document_variant == "Prod"
    views = {item.view: item.variant for item in parsed.items if item.kind == "view"}
    assert views["assembly view"] == "Lite" and views["fabrication view"] is None
    # Without the V2 element the older AssemblyVariant is read.
    old = xml.replace("<AssemblyVariantV2>Prod</AssemblyVariantV2>", "")
    path.write_bytes(lz4_stream(old.encode()))
    assert parse_pcb_dwf(str(path)).document_variant == "[All Variants]"
