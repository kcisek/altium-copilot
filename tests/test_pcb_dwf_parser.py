"""Draftsman (.PCBDwf) reader: LZ4Stream unpacking and XML extraction.

The fixture is synthetic (see tests/draftsman_fixture.py). Point
ALTIUM_COPILOT_ORACLE_PCBDWF at a real .PCBDwf of your own to also check a file
Altium wrote; never commit it.
"""
import os
import random
from pathlib import Path

import pytest

from parsers.pcb_dwf import decode_pcb_dwf, parse_pcb_dwf, read_pcb_dwf_picture
from tests.draftsman_fixture import FIXTURE, build, drawing_xml, lz4_stream


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
    assert note.lines == ["Board material FR-4, Tg 150 or higher", "Surface finish ENIG"]
    assert note.page == 1


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


def test_views_with_side_and_resolved_title(drawing):
    views = [(i.page, i.view, i.side, i.title) for i in drawing.items if i.kind == "view"]
    assert views == [
        (1, "fabrication view", "Top", "=ViewLayerName (Scale =ViewScale)"),
        (2, "assembly view", "Bottom", "View from Bottom side (Scale =ViewScale)"),
        (2, "layer stack legend", None, "Layer Stack Legend"),
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


def test_layer_stack_notes(drawing):
    assert drawing.layer_notes == [
        {"layer": "Dielectric 1", "comment": "Controlled impedance core", "note": "Use Isola 370HR"}
    ]


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
