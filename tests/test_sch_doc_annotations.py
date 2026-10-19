"""Designer annotations in .SchDoc files: notes, text frames, labels, pictures,
directives, title block. The fixture sheets have text frames, labels, directives and a
title block; notes and pictures are built as synthetic FileHeader / Storage streams."""
import struct
import zlib
from pathlib import Path

import pytest

from parsers.sch_doc import (
    annotations_from_stream,
    image_from_storage,
    parse_sch_doc_annotations,
    read_sch_doc_image,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def sheet1():
    return parse_sch_doc_annotations(str(FIXTURES / "sheet1.SchDoc"))


def _record(body: str | bytes, binary: bool = False) -> bytes:
    data = (body.encode("latin-1") if isinstance(body, str) else body) + b"\0"
    return struct.pack("<I", len(data) | (0x01000000 if binary else 0)) + data


def _stream(*records: str | bytes | tuple) -> bytes:
    """FileHeader bytes: a header record, then the given records (tuple = binary record)."""
    out = _record("|HEADER=Protel for Windows - Schematic Capture Binary File Version 5.0")
    for rec in records:
        out += _record(rec[0], binary=True) if isinstance(rec, tuple) else _record(rec)
    return out


# ---------- fixture sheets ----------

def test_fixture_text_frames(sheet1):
    frames = [i.text for i in sheet1.items if i.kind == "text_frame"]
    assert len(frames) == 15
    assert "Note: RMII clock provided by the  PHY chip" in frames


def test_fixture_sheet2_text_frames():
    notes = parse_sch_doc_annotations(str(FIXTURES / "sheet2.SchDoc"))
    assert len([i for i in notes.items if i.kind == "text_frame"]) == 6


def test_fixture_labels_skip_symbol_text(sheet1):
    labels = [i.text for i in sheet1.items if i.kind == "label"]
    assert {"Power", "JTAG", "Communication", "MCU"} <= set(labels)
    # "PA13 is JTCK-SWDIO" is drawn inside the MCU symbol (OwnerIndex set) — not a note
    assert not any(text.startswith("PA13 is") for text in labels)


def test_fixture_note_near_the_component_it_describes(sheet1):
    note = next(i for i in sheet1.items if i.text.startswith("1k resistor instead of 10k"))
    assert note.near == ["R4"]  # R4 is the 1k resistor


def test_fixture_note_lists_several_nearby_components(sheet1):
    note = next(i for i in sheet1.items if i.text.startswith("Supply configuration"))
    assert note.near[:4] == ["C3", "C4", "C5", "C6"]


def test_fixture_title_block_skips_placeholders_and_auto_values(sheet1):
    assert sheet1.title_block == {"DocumentNumber": "11", "SheetNumber": "10", "SheetTotal": "19"}


def test_fixture_directives(sheet1):
    assert [d.name for d in sheet1.directives] == ["50 Ohm"] * 4
    assert sheet1.directives[0].parameters == {}


def test_fixture_has_no_template_or_pictures(sheet1):
    assert sheet1.template is None
    assert not [i for i in sheet1.items if i.kind == "image"]


def test_fixture_reading_order_is_top_down(sheet1):
    tops = [i.y_mil + i.height_mil for i in sheet1.items]
    assert tops == sorted(tops, reverse=True)


# ---------- synthetic streams ----------

SHEET = "|RECORD=31|FontIdCount=1|Size1=10|FontName1=Arial"
# Positions: 0 header, 1 sheet, 2 component R1 with two pins, a designator and a symbol label.
R1 = (
    "|RECORD=1|Location.X=100|Location.Y=100|CurrentPartId=1|UniqueID=AAAAAAAA",
    "|RECORD=2|OwnerIndex=1|OwnerPartId=1|Location.X=100|Location.Y=100|Name=1|Designator=1",
    "|RECORD=2|OwnerIndex=1|OwnerPartId=1|Location.X=140|Location.Y=100|Name=2|Designator=2",
    "|RECORD=2|OwnerIndex=1|OwnerPartId=2|Location.X=900|Location.Y=900|Name=3|Designator=3",
    "|RECORD=34|OwnerIndex=1|Name=Designator|Text=R1",
    "|RECORD=4|OwnerIndex=1|IsNotAccesible=T|Location.X=110|Location.Y=110|Text=inside symbol",
)


def _annotations(*records):
    return annotations_from_stream(_stream(SHEET, *R1, *records))


def test_note_author_collapsed_and_line_breaks():
    notes = _annotations(
        "|RECORD=209|OwnerPartId=-1|Location.X=150|Location.Y=100|Corner.X=200|Corner.Y=120"
        "|Text=Check alternatives:~1PART-A123|Author=jdoe|Collapsed=T"
    )
    note = next(i for i in notes.items if i.kind == "note")
    assert note.text == "Check alternatives:\nPART-A123"
    assert note.author == "jdoe"
    assert note.collapsed is True
    assert (note.x_mil, note.y_mil, note.width_mil, note.height_mil) == (1500, 1000, 500, 200)
    assert note.near == ["R1"]


def test_note_utf8_text_wins_over_ansi():
    raw = b"|RECORD=209|Location.X=1|Location.Y=1|Text=Gr?e|%UTF8%Text=" + "Grüße".encode("utf-8")
    notes = annotations_from_stream(_stream(raw))
    assert notes.items[0].text == "Grüße"


def test_symbol_text_and_hidden_frames_are_skipped():
    notes = _annotations(
        "|RECORD=28|Location.X=500|Location.Y=500|Corner.X=600|Corner.Y=550|Text=hidden|IsHidden=T",
        "|RECORD=4|Location.X=10|Location.Y=10|Text==Title",
        "|RECORD=4|Location.X=10|Location.Y=20|Text=",
    )
    assert notes.items == []


def test_pins_of_parts_not_drawn_do_not_count_as_near():
    # Pin 3 belongs to part 2, which is not the placed part, at (900,900) x 10 mil.
    notes = _annotations("|RECORD=28|Location.X=900|Location.Y=900|Corner.X=950|Corner.Y=950|Text=far away")
    assert notes.items[0].near == []


def test_label_extent_reaches_nearby_component():
    # Anchor 1000 mil left of R1, but a 30-character label at 10 pt is ~2300 mil wide.
    notes = _annotations("|RECORD=4|Location.X=0|Location.Y=100|Justification=0|Text=" + "x" * 30)
    assert notes.items[0].near == ["R1"]


def test_legacy_uppercase_keys():
    notes = annotations_from_stream(_stream(
        "|RECORD=4|OWNERPARTID=-1|LOCATION.X=10|LOCATION.Y=20|TEXT=Legacy label",
        "|RECORD=41|NAME=Title|TEXT=Old board",
    ))
    assert notes.items[0].text == "Legacy label"
    assert (notes.items[0].x_mil, notes.items[0].y_mil) == (100, 200)
    assert notes.title_block == {"Title": "Old board"}


def test_binary_records_keep_owner_indexes_aligned():
    # A binary record at position 1 shifts the component to position 2 (OwnerIndex=1).
    notes = annotations_from_stream(_stream(
        ("\x00\x01\x02",),
        "|RECORD=1|Location.X=100|Location.Y=100|CurrentPartId=1",
        "|RECORD=2|OwnerIndex=1|Location.X=100|Location.Y=100",
        "|RECORD=34|OwnerIndex=1|Name=Designator|Text=U9",
        "|RECORD=28|Location.X=105|Location.Y=100|Corner.X=120|Corner.Y=110|Text=next to U9",
    ))
    assert notes.items[0].near == ["U9"]


def test_images_captions_and_template_content():
    notes = _annotations(
        "|RECORD=39|IsNotAccesible=T|FileName=C:\\Templates\\A3.SchDot",
        # Owned by the template record at position 8 (OwnerIndex=7): the logo, skipped.
        "|RECORD=30|OwnerIndex=7|Location.X=0|Location.Y=0|Corner.X=10|Corner.Y=10|EmbedImage=T|FileName=logo.jpg",
        "|RECORD=30|Location.X=300|Location.Y=300|Corner.X=500|Corner.Y=400|Corner.Y_Frac=50000"
        "|KeepAspect=T|EmbedImage=T|FileName=ClipboardPicture01/01/2026 10:00:00",
        "|RECORD=4|Location.X=300|Location.Y=280|Text=Figure 3: reference layout",
        "|RECORD=30|Location.X=800|Location.Y=800|Corner.X=900|Corner.Y=900|FileName=pics\\block.png",
    )
    assert notes.template == "C:\\Templates\\A3.SchDot"
    images = sorted((i for i in notes.items if i.kind == "image"), key=lambda i: i.image_index)
    assert [i.file_name for i in images] == ["ClipboardPicture01/01/2026 10:00:00", "pics\\block.png"]
    assert [i.image_index for i in images] == [0, 1]
    assert images[0].embedded is True and images[1].embedded is False
    assert (images[0].width_mil, images[0].height_mil) == (2000, 1005)
    assert images[0].captions == ["Figure 3: reference layout"]


def test_directive_parameters_and_sheet_parameters():
    notes = _annotations(
        "|RECORD=43|Location.X=50|Location.Y=60|Name=DiffPair",  # position 8
        "|RECORD=41|OwnerIndex=7|Name=DifferentialPair|Text=True",
        "|RECORD=41|Name=Title|Text=Main board",
        "|RECORD=41|Name=Revision|Text=*",
        "|RECORD=41|Name=CurrentDate|Text=01/01/2026",
        "|RECORD=41|Name=VersionControl_RevNumber|Text=42",
    )
    assert len(notes.directives) == 1
    assert notes.directives[0].name == "DiffPair"
    assert notes.directives[0].parameters == {"DifferentialPair": "True"}
    assert (notes.directives[0].x_mil, notes.directives[0].y_mil) == (500, 600)
    assert notes.title_block == {"Title": "Main board"}


# ---------- embedded picture storage ----------

def _storage(*entries: tuple[str, bytes]) -> bytes:
    out = _record("|HEADER=Icon storage|Weight=%d" % len(entries))
    for name, data in entries:
        packed = zlib.compress(data)
        body = b"\xd0" + bytes([len(name)]) + name.encode("latin-1") + struct.pack("<I", len(packed)) + packed
        out += struct.pack("<I", len(body)) + body
    return out


def test_image_from_storage_finds_by_name():
    storage = _storage(("first", b"BM-one"), ("ClipboardPicture01/01/2026", b"BM-two"))
    assert image_from_storage(storage, "ClipboardPicture01/01/2026") == b"BM-two"
    assert image_from_storage(storage, "first") == b"BM-one"


def test_image_from_storage_unknown_name():
    with pytest.raises(KeyError):
        image_from_storage(_storage(("first", b"x")), "missing")


def test_read_sch_doc_image_fixture_has_empty_storage():
    with pytest.raises(KeyError):
        read_sch_doc_image(str(FIXTURES / "sheet1.SchDoc"), "anything")
