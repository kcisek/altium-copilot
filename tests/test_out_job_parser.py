""".OutJob parser: the INI-like sections, containers (OutputMediumN), outputs and their
container links and order, configuration records, and the per-container path settings.
Synthetic text covers every parsed field; Altium's public AD25 examples are
snapshot-checked when installed."""
from pathlib import Path

import pytest

from parsers.out_job import (
    CONTAINER_KINDS,
    decode_text,
    parse_out_job,
    parse_out_job_text,
    parse_record,
)

EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")

OUTJOB = (
    "[OutputJobFile]\nVersion=1.0\nCaption=Fab job\nDescription=\nVaultGUID=\nItemHRID=OJ-001\n"
    "\n[OutputGroup1]\nName=Job Group\nDescription=\nTargetOutputMedium=Fab data\n"
    "VariantName=Prod\nVariantScope=1\nCurrentConfigurationName=Fabrication\n"
    "TargetPrinter=Office\nPrinterOptions=Record=PrinterOptions|Copies=1\n"
    "OutputMedium1=Print Job\nOutputMedium1_Type=Printer\nOutputMedium1_Printer=\n"
    "OutputMedium2=Docs\nOutputMedium2_Type=Publish\n"
    "OutputMedium3=Fab data\nOutputMedium3_Type=GeneratedFiles\n"
    "OutputMedium4=Video\nOutputMedium4_Type=Multimedia\n"
    "OutputType1=Gerber\nOutputName1=Gerber Files\nOutputCategory1=Fabrication\n"
    "OutputDocumentPath1=Board.PcbDoc\nOutputVariantName1=\nOutputEnabled1=1\n"
    "OutputEnabled1_OutputMedium1=0\nOutputEnabled1_OutputMedium2=0\n"
    "OutputEnabled1_OutputMedium3=2\nOutputEnabled1_OutputMedium4=0\nOutputDefault1=0\n"
    "Configuration1_Name1=OutputConfigurationParameter1\n"
    "Configuration1_Item1=GerberUnit=Metric|NumberOfDecimals=5|OriginPosition=Absolute|"
    "Plot.Set=SerializeLayerHash.Version~2,ClassName~TLayerToBoolean,16908289~1|Record=GerberView\n"
    "OutputType2=BOM_PartType\nOutputName2=Bill of Materials\nOutputCategory2=Report\n"
    "OutputDocumentPath2=[ActiveBOM Document]\nOutputVariantName2=Prod\nOutputEnabled2=0\n"
    "OutputEnabled2_OutputMedium2=1\nOutputEnabled2_OutputMedium3=0\nOutputDefault2=7\n"
    "PageOptions2=Record=PageOptions|PrintScale=1.00\n"
    "Configuration2_Name1=Filter\nConfiguration2_Item1=545046300E5446696C746572\n"
    "Configuration2_Name2=General\nConfiguration2_Item2=OpenExported=True|AddToProject=False\n"
    "OutputType3=NC Drill\nOutputName3=Drill\nOutputCategory3=Fabrication\n"
    "OutputEnabled3=0\nOutputEnabled3_OutputMedium3=1\nOutputEnabled3_OutputMedium2=x\n"
    "Configuration3_Item1=nentStateSettings|CountGroups=1\n"
    "Mystery=1\n"
    "\n[PublishSettings]\nOutputFilePath2=D:\\old\\Docs.pdf\nReleaseManaged2=0\n"
    "OutputBasePath2=Outputs\nOutputPathMedia2=[Media Name]\nOutputPathOutputer2=[Output Type]\n"
    "OutputFileName2=Docs.pdf\nPublishMethod2=0\nGenerateTOC=1\n"
    "OutputFilePath3=\\\nReleaseManaged3=1\nOutputPathMedia3=Fab\nOutputPathMediaValue3=Fab\n"
    "OutputPathOutputer3=[Output Custom]\nOutputPathOutputerValue3=Data\n"
    "OutputFilePath9=stray\n"
    "\n[GeneratedFilesSettings]\nRelativeOutputPath3=\\\nTimestampFolder3=1\nAddToProject3=0\n"
    "\n[OutputGroup2]\nName=Second\n"
    "\n[Extra]\nKey=Value\n"
)


@pytest.fixture
def job():
    return parse_out_job_text(OUTJOB, "Fab.OutJob")


def test_header_and_job_keys(job):
    assert job.header["Caption"] == "Fab job" and job.header["ItemHRID"] == "OJ-001"
    assert job.target == "Fab data" and job.variant == "Prod"
    assert job.job["VariantScope"] == "1" and job.job["CurrentConfigurationName"] == "Fabrication"
    assert job.job["Mystery"] == "1"  # unknown job keys are kept raw


def test_containers(job):
    assert [(c.index, c.name, c.type, c.kind) for c in job.containers] == [
        (1, "Print Job", "Printer", "print job"), (2, "Docs", "Publish", "pdf"),
        (3, "Fab data", "GeneratedFiles", "folder"), (4, "Video", "Multimedia", "video")]
    assert job.container(1).unmapped == {"Printer": ""}
    assert job.container(5) is None
    assert set(CONTAINER_KINDS) == {"Printer", "Publish", "GeneratedFiles", "Multimedia"}


def test_container_settings_are_keyed_by_container(job):
    docs, fab = job.container(2), job.container(3)
    assert docs.publish == {
        "OutputFilePath": "D:\\old\\Docs.pdf", "ReleaseManaged": "0", "OutputBasePath": "Outputs",
        "OutputPathMedia": "[Media Name]", "OutputPathOutputer": "[Output Type]",
        "OutputFileName": "Docs.pdf", "PublishMethod": "0"}
    assert fab.publish["OutputPathOutputerValue"] == "Data" and fab.publish["ReleaseManaged"] == "1"
    assert fab.generated == {"RelativeOutputPath": "\\", "TimestampFolder": "1",
                             "AddToProject": "0"}
    # Unnumbered keys and keys of containers that do not exist stay raw.
    assert job.other["PublishSettings"] == {"GenerateTOC": "1", "OutputFilePath9": "stray"}


def test_outputs(job):
    gerber, bom, drill = job.outputs
    assert (gerber.index, gerber.type, gerber.name, gerber.category, gerber.document) == (
        1, "Gerber", "Gerber Files", "Fabrication", "Board.PcbDoc")
    assert gerber.connected_to_selected is True and gerber.containers == {3: 2}
    assert (bom.variant, bom.document, bom.connected_to_selected) == (
        "Prod", "[ActiveBOM Document]", False)
    assert bom.containers == {2: 1}  # connected to Docs although not to the selected one
    assert bom.page_options.startswith("Record=PageOptions")
    assert bom.unmapped == {"OutputDefault2": "7"}  # 0 in every sample
    assert drill.containers == {3: 1}
    assert drill.unmapped == {"OutputEnabled3_OutputMedium2": "x"}


def test_configuration_records(job):
    gerber, bom, drill = job.outputs
    (record,) = gerber.records
    assert record.name == "OutputConfigurationParameter1" and record.record == "GerberView"
    assert record.values["OriginPosition"] == "Absolute"
    assert record.values["Plot.Set"].endswith("16908289~1")
    hexed, general = bom.records
    assert (hexed.name, hexed.record, hexed.raw) == ("Filter", "", "545046300E5446696C746572")
    assert general.record == "" and general.raw.startswith("OpenExported")  # no Record=
    # A truncated record (seen in Altium's Mini PC example) stays raw.
    assert drill.records[0].record == "" and drill.records[0].raw.startswith("nentState")


def test_parse_record_keeps_first_value():
    record = parse_record("P1", "A=1|A=2|Record=X|B")
    assert record.values == {"A": "1", "Record": "X"} and record.record == "X"


def test_other_sections_and_groups_raw(job):
    assert job.other["OutputGroup2"] == {"Name": "Second"}
    assert job.other["Extra"] == {"Key": "Value"}


def test_decoding_falls_back_to_windows_1252(tmp_path):
    assert decode_text("\ufeff[A]\nx=µ\n".encode("utf-8")) == ("[A]\nx=µ\n", "utf-8")
    path = tmp_path / "Old.OutJob"
    path.write_bytes(("[OutputGroup1]\nOutputMedium1=Ausgabe \xb5\nOutputMedium1_Type=Publish\n"
                      "OutputType1=Gerber\n").encode("cp1252"))
    job = parse_out_job(str(path))
    assert job.encoding == "cp1252" and job.containers[0].name == "Ausgabe µ"
    assert job.outputs[0].type == "Gerber" and job.path == str(path)


def test_empty_text():
    job = parse_out_job_text("")
    assert job.outputs == [] and job.containers == [] and job.target == ""


# ---------- Altium's public examples ----------

def _example(relative: str):
    path = EXAMPLES / relative
    if not path.exists():
        pytest.skip("Altium AD25 examples not installed")
    return parse_out_job(str(path))


def test_bluetooth_sentinel_fabrication():
    job = _example(r"Bluetooth Sentinel\Fabrication.OutJob")
    assert job.header["Version"] == "1.0" and job.target == "Folder Structure"
    assert [(c.name, c.type) for c in job.containers] == [
        ("Print Job", "Printer"), ("PDF", "Publish"), ("Folder Structure", "GeneratedFiles"),
        ("Video", "Multimedia")]
    assert [(o.type, o.category, o.containers) for o in job.outputs] == [
        ("ODB", "Fabrication", {3: 1}), ("Gerber", "Fabrication", {3: 2}),
        ("NC Drill", "Fabrication", {3: 3})]
    assert [r.record for o in job.outputs for r in o.records] == [
        "ODBView", "GerberView", "DrillView"]
    assert job.outputs[2].records[0].values["OriginPosition"] == "Relative"
    assert job.container(4).publish["ReleaseManaged"] == "1"
    assert job.other["PublishSettings"] == {"GenerateTOC": "1"}


def test_enabled_mirrors_the_selected_container():
    """OutputEnabledN is 1 exactly for the outputs connected to TargetOutputMedium."""
    for relative in (r"Bluetooth Sentinel\Assembly.OutJob", r"SpiritLevel-SL1\3D Prints.OutJob",
                     r"Mini PC\Mini PC - Motherboard\Fabrication.OutJob"):
        job = _example(relative)
        selected = next(c.index for c in job.containers if c.name == job.target)
        for output in job.outputs:
            assert output.connected_to_selected is (selected in output.containers), output.name


def test_mini_pc_erc_output_keeps_its_own_levels():
    job = _example(r"Mini PC\Mini PC - Motherboard\Assembly.OutJob")
    erc = next(o for o in job.outputs if o.type == "Electrical Rules Check")
    (record,) = erc.records
    assert record.record == "ERCReportView"
    assert sum(1 for key in record.values if key.startswith("ERCType")) == 119
    assert record.values["ERCConnectionMatrixL1"] == "NNNNNNNNNNNWNNNNN"
    drawing = next(o for o in job.outputs if o.type == "PCBDrawing")
    assert drawing.document == "MiniPC_Drill.PCBDwf" and drawing.containers == {2: 2}
