from server.parsers.annotation import AnnotationEntry, parse_annotation
from server.parsers.prj_pcb import parse_prj_pcb

# Shaped like Altium's Bluetooth_Sentinel.Annotation (CRLF, one numbered group per part).
ANNOTATION = (
    "[DesignatorManager]\r\n"
    "LogicalDesignator0=U1\r\n"
    "LogicalPartID0=1\r\n"
    "DocumentName0=3-Axis_Accelerometer.SchDoc\r\n"
    "ChannelName0=Accelerometer\r\n"
    "UniqueID0=\\VBFEKVDF\\RGVBRMHC\r\n"
    "PhysicalDesignator0=U3\r\n"
    "PhysicalDesignatorLocked0=0\r\n"
    "LogicalDesignator1=R1\r\n"
    "LogicalPartID1=1\r\n"
    "DocumentName1=Red_LED.SchDoc\r\n"
    "ChannelName1=Red_LED1\r\n"
    "UniqueID1=\\HJYSAVCF\\ASIVGCJH\\HRWSBXKU\r\n"
    "PhysicalDesignator1=R3\r\n"
    "PhysicalDesignatorLocked1=0\r\n"
    "SheetNumberOrder=Display Order\r\n"
    "SheetNumberMethod=Increasing\r\n"
)


def _write(tmp_path, text: str, encoding: str = "utf-8"):
    path = tmp_path / "Board.Annotation"
    path.write_bytes(text.encode(encoding))
    return path


def test_entries_map_unique_id_paths_to_physical_designators(tmp_path):
    entries = parse_annotation(_write(tmp_path, ANNOTATION))
    assert entries == [
        AnnotationEntry(
            logical="U1", physical="U3", unique_id="\\VBFEKVDF\\RGVBRMHC",
            document="3-Axis_Accelerometer.SchDoc", channel="Accelerometer",
        ),
        AnnotationEntry(
            logical="R1", physical="R3", unique_id="\\HJYSAVCF\\ASIVGCJH\\HRWSBXKU",
            document="Red_LED.SchDoc", channel="Red_LED1",
        ),
    ]


def test_entry_names_the_component_uid_and_sheet(tmp_path):
    entry = parse_annotation(_write(tmp_path, ANNOTATION))[1]
    assert entry.component_uid == "HRWSBXKU"
    assert entry.sheet == "Red_LED"
    assert AnnotationEntry(logical="R1", physical="R2", unique_id="ABCDEFGH").sheet == ""
    assert AnnotationEntry(logical="R1", physical="R2", unique_id="ABCDEFGH").component_uid == "ABCDEFGH"


def test_entries_are_ordered_by_index_not_by_line(tmp_path):
    text = (
        "[DesignatorManager]\n"
        "UniqueID10=\\B\nPhysicalDesignator10=R10\n"
        "UniqueID2=\\A\nPhysicalDesignator2=R2\n"
    )
    assert [e.physical for e in parse_annotation(_write(tmp_path, text))] == ["R2", "R10"]


def test_unannotated_or_incomplete_entries_are_skipped(tmp_path):
    text = (
        "[DesignatorManager]\n"
        "LogicalDesignator0=R?\nUniqueID0=\\AAAA\nPhysicalDesignator0=R?\n"
        "LogicalDesignator1=R1\nUniqueID1=\\BBBB\nPhysicalDesignator1=\n"
        "LogicalDesignator2=R2\nPhysicalDesignator2=R5\n"
        "LogicalDesignator3=R3\nUniqueID3=\\CCCC\nPhysicalDesignator3=R7\n"
    )
    assert [e.physical for e in parse_annotation(_write(tmp_path, text))] == ["R7"]


def test_keys_are_case_insensitive_and_other_sections_ignored(tmp_path):
    text = (
        "[Other]\nUniqueID0=\\ZZZZ\nPhysicalDesignator0=X9\n"
        "[designatormanager]\nuniqueid0=\\AAAA\nPHYSICALDESIGNATOR0=R4\nlogicaldesignator0=R1\n"
    )
    entries = parse_annotation(_write(tmp_path, text))
    assert [(e.logical, e.physical, e.unique_id) for e in entries] == [("R1", "R4", "\\AAAA")]


def test_non_utf8_file_is_read_as_latin1(tmp_path):
    text = "[DesignatorManager]\nUniqueID0=\\AAAA\nPhysicalDesignator0=R1\nChannelName0=Kanal_\xb5\n"
    assert parse_annotation(_write(tmp_path, text, "latin-1"))[0].channel == "Kanal_\xb5"


def test_prj_pcb_lists_annotation_documents(tmp_path):
    prj = tmp_path / "Board.PrjPcb"
    prj.write_text(
        "[Design]\nVersion=1.0\n"
        "[Document1]\nDocumentPath=Board.Annotation\n"
        "[Document2]\nDocumentPath=sheets\\Power.SchDoc\n",
        encoding="utf-8",
    )
    data = parse_prj_pcb(str(prj))
    assert data.annotation_paths == [str((tmp_path / "Board.Annotation").resolve())]
    assert data.sheet_paths == [str((tmp_path / "sheets" / "Power.SchDoc").resolve())]
