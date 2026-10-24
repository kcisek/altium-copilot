"""SchLib, PcbLib and IntLib readers and the project library resolver (roadmap #52),
on synthetic library files built by tests/library_fixture.py."""
import io
import struct
import zlib
from pathlib import Path

import olefile
import pytest

from server.altium import _make_enriched
from server.parsers.int_lib import decompress_embedded, parse_int_lib
from server.parsers.pcb_doc import (
    PAD_SHAPE_RECT, PAD_SHAPE_ROUND, decode_components, v7_layer_mechanical,
)
from server.parsers.pcb_lib import layer_kind, mech_kinds, parse_pcb_lib, split_primitives
from server.parsers.sch_doc import components_from_stream, read_records
from server.parsers.sch_lib import decode_binary_pin, parse_sch_lib, storage_name
from server.services import libraries as libs
from server.services.libraries import (
    REASON_DATABASE, REASON_FILE_MISSING, REASON_NO_NAME, REASON_NOT_FOUND, REASON_WORKSPACE,
    LibraryResolver, installed_libraries, load_footprint, load_symbol, project_libraries,
    resolution_summary,
)
from tests.library_fixture import (
    arc, binary_pin, body, fill, footprint_streams, int_lib_streams, ole_bytes, pad,
    pcb_lib_streams, region, sch_component, sch_lib_streams, text, text_pin, track, write_ole,
)
from tests.sch_fixture import record, stream

# ---------- OLE writer ----------


def test_the_ole_writer_round_trips_through_olefile(tmp_path):
    big = bytes(range(256)) * 40  # past the 4096-byte mini stream cutoff
    streams = {"FileHeader": b"head", "A/Data": b"x" * 100, "A/Sub/Data": big,
               "B/Empty": b"", **{f"S{i}/Data": bytes([i]) * 70 for i in range(20)}}
    with olefile.OleFileIO(str(write_ole(tmp_path / "t.ole", streams))) as ole:
        assert sorted("/".join(entry) for entry in ole.listdir()) == sorted(streams)
        for name, data in streams.items():
            assert ole.openstream(name).read() == data


# ---------- SchLib ----------


def test_decode_binary_pin_reads_the_pin_fields():
    raw = binary_pin("7", "EN", electrical=0, part=2, mode=1, x=-3, y=12, length=20,
                     conglomerate=0x04 | 2, description="enable")
    props = decode_binary_pin(raw[4:])
    assert props == {
        "record": "2", "ownerpartid": "2", "ownerpartdisplaymode": "1",
        "description": "enable", "electrical": "0", "pinconglomerate": "6",
        "pinlength": "20", "location.x": "-3", "location.y": "12", "name": "EN",
        "designator": "7", "swapidgroup": "",
    }
    assert decode_binary_pin(struct.pack("<i", 3) + b"\0" * 30) is None  # not a pin
    assert decode_binary_pin(raw[4:20]) is None  # cut short


def test_read_records_keeps_binary_records_as_placeholders_by_default():
    data = record("|RECORD=1|LIBREFERENCE=X") + binary_pin("1", "A")
    assert read_records(data) == [{"record": "1", "libreference": "X"}, {}]
    assert read_records(data, binary=decode_binary_pin)[1]["designator"] == "1"


def _sample_sch_lib() -> dict[str, bytes]:
    amp = sch_component(
        "AMP-2", [binary_pin("1", "OUT", electrical=2, part=1),
                  binary_pin("2", "IN-", electrical=0, part=1, x=-30, conglomerate=0x18 | 2),
                  binary_pin("4", "V-", electrical=7, part=0, conglomerate=0x04),
                  binary_pin("1", "OUT", electrical=2, part=1, mode=1)],
        description="made-up dual amplifier", parts=2, designator="U?",
        params={"Comment": "AMP", "comment": "ignored", "Value": "x"},
        footprints=[("FP-A", False, "", False), ("FP-B/1", True, "Made.PcbLib", False)])
    old = sch_component("OLD-1", [text_pin("1", "P", electrical=4, x=5, y=6)],
                        designator="R?", design_item_id="OLD-ITEM")
    slash = sch_component("A/B", [binary_pin("1", "K")])
    return sch_lib_streams(("AMP-2", amp), ("OLD-1", old), ("A/B", slash),
                           aliases={"OLD-1": ["OLD-ALIAS"]})


def test_parse_sch_lib_reads_components_pins_parameters_and_models(tmp_path):
    lib = parse_sch_lib(str(write_ole(tmp_path / "a.SchLib", _sample_sch_lib())))
    assert lib.header_names == ["AMP-2", "OLD-1", "A/B"]
    assert lib.warnings == []
    amp = lib.find("AMP-2")
    assert (amp.description, amp.part_count, amp.designator, amp.comment) == (
        "made-up dual amplifier", 2, "U?", "AMP")
    assert amp.parameters == {"Comment": "AMP", "Value": "x"}  # the first of a name wins
    out, inverting, supply, alternate = amp.pins
    assert (out.designator, out.name, out.electrical, out.part, out.display_mode) == (
        "1", "OUT", "Output", 1, 0)
    assert (inverting.x, inverting.length, inverting.orientation) == (-300.0, 300.0, 2)
    assert (inverting.end_x, inverting.end_y) == (-600.0, 0.0)  # pointing left
    assert (supply.electrical, supply.part, supply.hidden) == ("Power", 0, True)
    assert alternate.display_mode == 1
    assert [(m.name, m.current, m.library) for m in amp.footprints] == [
        ("FP-A", False, ""), ("FP-B/1", True, "Made.PcbLib")]
    assert amp.current_footprint.name == "FP-B/1"
    old = lib.find("old-1")  # a name differing only in case
    assert old.lib_reference == "OLD-1" and old.aliases == ["OLD-ALIAS"]
    assert (old.design_item_id, old.designator) == ("OLD-ITEM", "R?")
    assert [(p.designator, p.name, p.electrical, p.x, p.y) for p in old.pins] == [
        ("1", "P", "Passive", 50.0, 60.0)]
    assert lib.find("OLD-ALIAS") is old
    slash = lib.find("A/B")
    assert slash.storage == "A_B"  # '/' is not allowed in a storage name
    assert lib.find("nothing") is None and lib.find("") is None


def test_parse_sch_lib_reads_only_the_named_components(tmp_path):
    path = str(write_ole(tmp_path / "a.SchLib", _sample_sch_lib()))
    assert parse_sch_lib(path, names={"amp-2"}).names == ["AMP-2"]
    assert parse_sch_lib(path, names={"a/b"}).names == ["A/B"]
    assert parse_sch_lib(path, names={"old-alias"}).names == ["OLD-1"]
    assert parse_sch_lib(path, names=set()).names == []
    assert parse_sch_lib(path, names=set()).header_names == ["AMP-2", "OLD-1", "A/B"]


def test_parse_sch_lib_finds_a_component_whose_storage_name_differs(tmp_path):
    streams = sch_lib_streams(("NAME-ONE", sch_component("NAME-ONE", [])))
    streams["Renamed/Data"] = streams.pop("NAME-ONE/Data")
    path = str(write_ole(tmp_path / "a.SchLib", streams))
    assert parse_sch_lib(path, names={"name-one"}).names == ["NAME-ONE"]


def test_storage_name():
    assert storage_name("Ab/C") == "ab_c"
    assert storage_name("X" * 40) == "x" * 31


# ---------- PcbLib ----------

LAYER_PROPS = {
    "LAYER59MECHKIND": "CourtyardTop",  # Mechanical 3, legacy key
    "V9_CACHE_LAYER73_LAYERID": str(0x01020005), "V9_CACHE_LAYER73_MECHKIND": "AssemblyTop",
    "V9_CACHE_LAYER90_LAYERID": str(0x01020015), "V9_CACHE_LAYER90_MECHKIND": "CourtyardBottom",
    "LAYER60MECHKIND": "NotSet",
}
SQUARE = [(-50.0, -40.0), (50.0, -40.0), (50.0, 40.0), (-50.0, 40.0)]


def _sample_pcb_lib() -> dict[str, bytes]:
    chip = footprint_streams("CHIP-1", [
        pad("1", -40.0, 0.0, 30.0, 40.0),
        pad("2", 40.0, 0.0, 30.0, 40.0, shape=1),
        track(59, -60.0, -50.0, 60.0, -50.0, width=2.0),  # courtyard
        track(59, 60.0, -50.0, 60.0, 50.0, width=2.0),
        arc(61, 0.0, 0.0, 20.0, width=4.0),  # assembly (Mechanical 5)
        track(33, -50.0, 30.0, 50.0, 30.0),  # overlay
        text(33, 0.0, 60.0, ".Designator", designator=True),
        body(SQUARE, layer=58, OVERALLHEIGHT="35mil", STANDOFFHEIGHT="1mil",
             **{"MODEL.NAME": "made-up.step", "MODEL.EMBED": "TRUE", "MODELID": "{G}",
                "MODEL.MODELTYPE": "1", "V7_LAYER": "MECHANICAL2"}),
    ], height="40mil", description="made-up chip")
    tht = footprint_streams("THT/2", [
        pad("1", 0.0, 0.0, 60.0, 60.0, layer=74, hole=35.0, plated=True),
        pad("MH", 200.0, 0.0, 0.0, 0.0, layer=74, hole=120.0, plated=False),  # bare hole
    ])
    return pcb_lib_streams(chip, tht, names=["CHIP-1", "THT/2"], layer_props=LAYER_PROPS)


def test_parse_pcb_lib_reads_footprints(tmp_path):
    lib = parse_pcb_lib(str(write_ole(tmp_path / "a.PcbLib", _sample_pcb_lib())))
    assert lib.names == ["CHIP-1", "THT/2"]
    assert lib.mech_kinds == {3: "CourtyardTop", 5: "AssemblyTop", 21: "CourtyardBottom"}
    chip = lib.find("chip-1")
    assert (chip.name, chip.description, chip.height_mil) == ("CHIP-1", "made-up chip", 40.0)
    assert chip.records == {"arcs": 1, "pads": 2, "tracks": 3, "texts": 1, "bodies": 1}
    assert chip.skipped == {} and chip.unread_bytes == 0
    first, second = chip.pads
    assert (first.pad_name, first.x1, first.y1, first.width, first.height, first.shape,
            first.layer, first.hole) == ("1", -40.0, 0.0, 30.0, 40.0, PAD_SHAPE_RECT, 1, 0.0)
    assert second.shape == PAD_SHAPE_ROUND
    assert chip.pad_designators == ["1", "2"]
    assert [t.text for t in chip.texts if t.is_designator] == [".Designator"]
    (model,) = chip.bodies
    assert (model.model_name, model.model_embedded, model.model_id, model.v7_layer) == (
        "made-up.step", True, "{G}", "MECHANICAL2")
    assert chip.body_height_mil == 35.0 and chip.models == ["made-up.step"]
    (courtyard,) = chip.outline("courtyard", "top")
    assert (courtyard.side, courtyard.mechanical, courtyard.primitives) == ("top", 3, 2)
    assert courtyard.box == pytest.approx((-61.0, -51.0, 61.0, 51.0))
    (assembly,) = chip.outline("assembly")
    assert assembly.box == pytest.approx((-22.0, -22.0, 22.0, 22.0), abs=0.5)
    assert chip.outline("courtyard", "bottom") == []
    tht = lib.find("THT/2")
    assert tht.storage == "THT_2"
    (hole_pad,) = tht.pads
    assert (hole_pad.layer, hole_pad.hole, hole_pad.plated) == (74, 35.0, True)
    assert [(h.pad_name, h.hole, h.plated) for h in tht.holes] == [("MH", 120.0, False)]
    assert tht.pad_designators == ["1", "MH"]
    assert (tht.height_mil, tht.body_height_mil, tht.models) == (0.0, None, [])


def test_parse_pcb_lib_reads_only_the_named_footprints(tmp_path):
    path = str(write_ole(tmp_path / "a.PcbLib", _sample_pcb_lib()))
    assert [fp.name for fp in parse_pcb_lib(path, names={"tht/2"}).footprints] == ["THT/2"]
    assert parse_pcb_lib(path, names=set()).footprints == []
    assert parse_pcb_lib(path, names=set()).names == ["CHIP-1", "THT/2"]


def test_split_primitives_stops_at_an_unknown_type_or_a_cut_record():
    data = track(1, 0, 0, 1, 1) + pad("1", 0, 0, 1, 1)
    streams, counts, unread = split_primitives(data)
    assert counts == {4: 1, 2: 1} and unread == 0
    assert split_primitives(data + b"\x63\0\0\0\0")[2] == 5  # type 99
    _, counts, unread = split_primitives(data[:-3])
    assert counts == {4: 1} and unread == len(data) - 3 - len(track(1, 0, 0, 1, 1))


def test_mech_kinds_and_layer_kind():
    assert mech_kinds({"LAYER72MECHKIND": "ComponentCenterTop", "LAYER12MECHKIND": "X",
                       "LAYERV7_3LAYERID": str(0x01020014),
                       "LAYERV7_3MECHKIND": "AssemblyBottom",
                       "LAYER_V8_1LAYERID": "16973830", "LAYER_V8_1MECHKIND": "Odd"}) == {
        16: "ComponentCenterTop", 20: "AssemblyBottom"}
    assert layer_kind("CourtyardTop") == ("courtyard", "top")
    assert layer_kind("3DBodyBottom") == ("3d_body", "bottom")
    assert layer_kind("RouteToolPath") == ("routetoolpath", None)
    assert v7_layer_mechanical("MECHANICAL17") == 17 and v7_layer_mechanical("TOP") is None


def test_mechanical_layers_above_16_come_from_the_v7_layer_id(tmp_path):
    # A primitive on Mechanical 17 or higher has v6 layer 72 (Mechanical 16); the
    # track's, arc's and fill's V7 layer id and the region's V7_LAYER name the layer.
    footprint = footprint_streams("V7", [
        pad("1", 0.0, 0.0, 20.0, 20.0),
        track(72, -30.0, -30.0, 30.0, -30.0, width=2.0, mechanical=17),
        arc(72, 0.0, 0.0, 40.0, width=2.0, mechanical=17),
        fill(72, -5.0, -5.0, 5.0, 5.0, mechanical=17),
        region([(50.0, 0.0), (60.0, 0.0), (60.0, 10.0)], layer=72, v7_layer="MECHANICAL17"),
        track(72, -1.0, 0.0, 1.0, 0.0, width=2.0),  # Mechanical 16, no V7 id
        track(72, -2.0, 0.0, 2.0, 0.0, width=2.0, mechanical=16),
    ])
    props = {"LAYER72MECHKIND": "ComponentCenterTop",
             "V9_CACHE_LAYER91_LAYERID": str(0x01020011),
             "V9_CACHE_LAYER91_MECHKIND": "CourtyardTop"}
    lib = parse_pcb_lib(str(write_ole(tmp_path / "v7.PcbLib", pcb_lib_streams(
        footprint, names=["V7"], layer_props=props))))
    found = lib.find("V7")
    assert [line.mechanical for line in found.tracks] == [17, None, 16]
    assert {chord.mechanical for chord in found.arcs} == {17}
    assert (found.fills[0].mechanical, found.regions[0].mechanical) == (17, 17)
    (courtyard,) = found.outline("courtyard")
    assert courtyard.mechanical == 17
    assert courtyard.box == pytest.approx((-41.0, -41.0, 60.0, 41.0), abs=0.5)
    (center,) = found.outline("component_center")
    assert (center.mechanical, center.primitives) == (16, 2)
    assert center.box == pytest.approx((-3.0, -1.0, 3.0, 1.0))


def test_an_older_body_with_its_heights_the_other_way_round(tmp_path):
    # Without MODEL.EXTRUDED.MAXZ a body may hold STANDOFFHEIGHT above OVERALLHEIGHT; it
    # is read as standing from the lower to the higher, as its placed copies are.
    footprint = footprint_streams("OLD", [
        pad("1", 0.0, 0.0, 20.0, 20.0),
        body(SQUARE, OVERALLHEIGHT="0mil", STANDOFFHEIGHT="400mil"),
        body(SQUARE, OVERALLHEIGHT="20mil", STANDOFFHEIGHT="30mil",
             **{"MODEL.EXTRUDED.MAXZ": "20mil", "MODEL.EXTRUDED.MINZ": "30mil"}),
    ])
    found = parse_pcb_lib(str(write_ole(tmp_path / "old.PcbLib", pcb_lib_streams(
        footprint, names=["OLD"])))).find("OLD")
    assert [(b.overall_height_mil, b.standoff_height_mil) for b in found.bodies] == [
        (400.0, 0.0), (20.0, 30.0)]  # with MAXZ the record is taken as written
    assert found.body_height_mil == 400.0


# ---------- IntLib ----------


def test_decompress_embedded():
    inner = ole_bytes({"FileHeader": b"x"})
    assert decompress_embedded(b"\x02" + zlib.compress(inner)) == inner
    assert decompress_embedded(inner) == inner
    with pytest.raises(zlib.error):
        decompress_embedded(b"\x02not zlib")


def test_parse_int_lib_reads_the_embedded_libraries(tmp_path):
    streams = int_lib_streams(_sample_sch_lib(), _sample_pcb_lib())
    path = str(write_ole(tmp_path / "a.IntLib", streams))
    lib = parse_int_lib(path)
    assert lib.warnings == [] and lib.other_models == {"CKT": 1}
    holder, component = lib.find_component("amp-2")
    assert holder.path == f"{path}|SchLib/0.schlib" and len(component.pins) == 4
    _, footprint = lib.find_footprint("CHIP-1")
    assert len(footprint.pads) == 2
    assert lib.index == {"components": {"SchLib/0.schlib": ["AMP-2", "OLD-1", "A/B"]},
                         "footprints": {"PCBLib/0.PcbLib": ["CHIP-1", "THT/2"]}}
    assert lib.find_component("nothing") is None and lib.find_footprint("nothing") is None
    partial = parse_int_lib(path, components={"old-1"}, footprints=set())
    assert [lib.names for lib in partial.sch_libs] == [["OLD-1"]]
    assert partial.pcb_libs[0].footprints == [] and partial.pcb_libs[0].names == [
        "CHIP-1", "THT/2"]


def test_parse_int_lib_reports_an_unreadable_embedded_library(tmp_path):
    streams = int_lib_streams(_sample_sch_lib())
    streams["PCBLib/0.PcbLib"] = b"\x02broken"
    lib = parse_int_lib(str(write_ole(tmp_path / "a.IntLib", streams)))
    assert len(lib.sch_libs) == 1 and lib.pcb_libs == []
    assert lib.warnings and lib.warnings[0].startswith("PCBLib/0.PcbLib: error")


# ---------- Library fields of placed parts ----------


def test_schematic_components_keep_their_library_links():
    sheet = stream([
        "|RECORD=1|LIBREFERENCE=AMP|DESIGNITEMID=AMP-ITEM|SOURCELIBRARYNAME=Made.SchLib"
        "|LIBRARYPATH=*|CURRENTPARTID=1|PARTCOUNT=3|UNIQUEID=AAAAAAAA",
        "|RECORD=34|OWNERINDEX=1|NAME=Designator|TEXT=U1",
        "|RECORD=44|OWNERINDEX=1",
        "|RECORD=45|OWNERINDEX=3|MODELNAME=FP-A|MODELTYPE=PCBLIB|ISCURRENT=T"
        "|MODELDATAFILE0=Made.PcbLib",
        "|RECORD=1|LIBREFERENCE=AMP|SOURCELIBRARYNAME=Made.SchLib|CURRENTPARTID=2"
        "|PARTCOUNT=3|UNIQUEID=BBBBBBBB|VAULTGUID={V}|DATABASETABLENAME=Table",
        "|RECORD=34|OWNERINDEX=5|NAME=Designator|TEXT=U1",
    ])
    meta = components_from_stream(sheet)["U1"]
    assert (meta.library, meta.library_path, meta.footprint, meta.footprint_library) == (
        "Made.SchLib", "*", "FP-A", "Made.PcbLib")
    # The second part's Workspace and database links are merged in.
    assert (meta.workspace_part, meta.database_table) == (True, "Table")
    entry = _make_enriched(meta, {})
    assert (entry["library"], entry["footprint_library"], entry["workspace_part"],
            entry["footprint_from_component_library"]) == (
        "Made.SchLib", "Made.PcbLib", True, False)


def test_pcb_components_keep_their_library_links():
    (component,) = decode_components([{
        "PATTERN": "FP-A", "SOURCEFOOTPRINTLIBRARY": "Made.PcbLib",
        "SOURCECOMPONENTLIBRARY": "Made.SchLib", "SOURCELIBREFERENCE": "AMP-ITEM",
        "HEIGHT": "1mm", "ITEMGUID": "{I}"}])
    assert (component.footprint_library, component.component_library,
            component.lib_reference, component.workspace_part) == (
        "Made.PcbLib", "Made.SchLib", "AMP-ITEM", True)
    assert component.height_mil == pytest.approx(1 / 0.0254)


# ---------- Resolution ----------


def _project(tmp_path: Path, search: str = "") -> str:
    """A project listing Made.SchLib, Made.PcbLib (in Libs) and a missing library, with
    an optional [SearchPath1] section."""
    (tmp_path / "Libs").mkdir(parents=True)
    write_ole(tmp_path / "Libs" / "Made.SchLib", _sample_sch_lib())
    write_ole(tmp_path / "Libs" / "Made.PcbLib", _sample_pcb_lib())
    prj = tmp_path / "Board.PrjPcb"
    prj.write_text("[Design]\n[Document1]\nDocumentPath=Board.SchDoc\n"
                   "[Document2]\nDocumentPath=Libs\\Made.SchLib\n"
                   "[Document3]\nDocumentPath=Libs\\Made.PcbLib\n"
                   "[Document4]\nDocumentPath=Gone.PcbLib\n" + search, encoding="utf-8")
    return str(prj)


def _component(**fields) -> dict:
    return {"lib_reference": "", "design_item_id": "", "footprint": None, "library": "",
            "workspace_part": False, "database_table": "", "footprint_library": "", **fields}


def test_project_libraries_lists_documents_search_paths_and_installed(tmp_path):
    other = tmp_path / "Shared" / "Deep"
    other.mkdir(parents=True)
    write_ole(other / "Extra.PcbLib", _sample_pcb_lib())
    (other / "notes.txt").write_text("x")
    prj = _project(tmp_path, f"[SearchPath1]\nPath={tmp_path / 'Shared'}\\*.*\n"
                             "IncludeSubFolders=1\n[SearchPath2]\nPath=Nowhere\n")
    found = project_libraries(prj, installed=[str(tmp_path / "Missing.IntLib")])
    assert [(Path(lib.path).name, lib.kind, lib.exists) for lib in found.project_libraries] == [
        ("Made.SchLib", "SchLib", True), ("Made.PcbLib", "PcbLib", True),
        ("Gone.PcbLib", "PcbLib", False)]
    first, second = found.search_paths
    assert (first.pattern, first.include_subfolders, first.exists) == ("*.*", True, True)
    assert (second.pattern, second.exists) == ("*.*", False)
    assert [Path(lib.path).name for lib in found.search_path_libraries] == ["Extra.PcbLib"]
    assert [lib.exists for lib in found.installed] == [False]
    assert [Path(lib.path).name for lib in found.available()] == [
        "Made.SchLib", "Made.PcbLib", "Extra.PcbLib"]
    no_subfolders = project_libraries(
        _project(tmp_path / "p2", f"[SearchPath1]\nPath={tmp_path / 'Shared'}\n"), installed=[])
    assert no_subfolders.search_path_libraries == []


def test_installed_libraries_are_searched_before_the_search_paths(tmp_path):
    # Altium searches the Available File-based Libraries tab by tab: Project, Installed,
    # then Search Path.
    shared = tmp_path / "Shared"
    shared.mkdir()
    one_pad = footprint_streams("ONLY-X", [pad("1", 0.0, 0.0, 20.0, 20.0)])
    two_pads = footprint_streams("ONLY-X", [pad("1", 0.0, 0.0, 20.0, 20.0),
                                            pad("2", 50.0, 0.0, 20.0, 20.0)])
    write_ole(shared / "Shared.PcbLib", pcb_lib_streams(one_pad, names=["ONLY-X"]))
    installed = write_ole(tmp_path / "Installed.PcbLib", pcb_lib_streams(two_pads, names=["ONLY-X"]))
    prj = _project(tmp_path / "p", f"[SearchPath1]\nPath={shared}\\*.*\n")
    libraries = project_libraries(prj, installed=[str(installed)])
    assert [Path(lib.path).name for lib in libraries.available()] == [
        "Made.SchLib", "Made.PcbLib", "Installed.PcbLib", "Shared.PcbLib"]
    hit, _ = LibraryResolver(libraries).find_footprint("ONLY-X")
    assert (Path(hit.path).name, hit.origin) == ("Installed.PcbLib", "installed")
    assert libs.find_library_file("Shared.PcbLib", libraries).origin == "search path"


def test_installed_libraries_reads_altium_folders_and_the_installed_list(tmp_path):
    library = tmp_path / "AD99" / "Library" / "Vendor"
    library.mkdir(parents=True)
    write_ole(library / "Vendor.IntLib", int_lib_streams(_sample_sch_lib()))
    (library / "readme.txt").write_text("x")
    listed = tmp_path / "Mine.SchLib"
    write_ole(listed, _sample_sch_lib())
    profile = tmp_path / "Profile" / "Altium Designer {X}" / "Prefs"
    profile.mkdir(parents=True)
    (profile / "IntLib_Installed.ini").write_text(
        f"[IntegratedLibraries]\nLibrary0={listed}\nLibrary1={tmp_path / 'gone.IntLib'}\n"
        f"Library2={library / 'Vendor.IntLib'}\n", encoding="utf-8")
    found = installed_libraries((str(tmp_path / "AD*" / "Library"),), str(tmp_path / "Profile"))
    assert [Path(path).name for path in found] == ["Vendor.IntLib", "Mine.SchLib"]


def test_resolve_netlist_parts(tmp_path):
    prj = _project(tmp_path)
    installed = tmp_path / "Installed.IntLib"
    write_ole(installed, int_lib_streams(_sample_sch_lib(), _sample_pcb_lib()))
    resolver = LibraryResolver(project_libraries(prj, installed=[str(installed)]))
    netlist = {"components": {
        "U1": _component(lib_reference="AMP", design_item_id="AMP-2", library="Made.SchLib",
                         footprint="FP-X/1"),
        "U2": _component(lib_reference="amp-2", library="Made.SchLib", footprint="chip-1",
                         footprint_library="Made.PcbLib"),
        "U3": _component(lib_reference="AMP-2", library="Elsewhere.SchLib", footprint="THT/2"),
        "U4": _component(lib_reference="NOPE", library="Made.SchLib", footprint=None),
        "U5": _component(lib_reference="AMP-2", library="Workspace Content", workspace_part=True,
                         footprint="CHIP-1"),
        "U6": _component(lib_reference="AMP-2", library="Parts.DbLib", footprint="CHIP-1"),
        "U7": _component(lib_reference="AMP-2", library="Made.SchLib", database_table="T",
                         footprint="CHIP-1", footprint_library="Gone.PcbLib"),
        "U8": _component(lib_reference="OLD-1", library="Installed.IntLib", footprint="CHIP-1"),
        "U9": _component(lib_reference="OLD-ALIAS", library="", footprint="nothing-here"),
    }}
    found = {r.designator: r for r in resolver.resolve_netlist(netlist)}
    u1, u2, u3, u4, u5, u6, u7, u8, u9 = (found[f"U{i}"] for i in range(1, 10))
    # The Design Item ID names the library component; the footprint is not in a library.
    assert (Path(u1.symbol.path).name, u1.symbol.name, u1.symbol.origin, u1.symbol.named) == (
        "Made.SchLib", "AMP-2", "project", True)
    assert (u1.footprint_hit, u1.footprint_reason) == (None, REASON_NOT_FOUND)
    assert (u2.symbol.name, u2.symbol.case_differs) == ("AMP-2", True)
    assert (u2.footprint_hit.name, u2.footprint_hit.case_differs, u2.footprint_hit.named) == (
        "CHIP-1", True, True)
    # Placed from a library the project does not have: not looked up elsewhere, but a
    # symbol of that name in another library is given as a hint.
    assert (u3.symbol, u3.symbol_reason) == (None, REASON_FILE_MISSING)
    assert Path(u3.symbol_elsewhere.path).name == "Made.SchLib"
    assert (Path(u3.footprint_hit.path).name, u3.footprint_hit.named) == ("Made.PcbLib", False)
    assert (u4.symbol_reason, u4.footprint_reason) == (REASON_NOT_FOUND, REASON_NO_NAME)
    assert (u5.symbol_reason, u5.footprint_reason) == (REASON_WORKSPACE, REASON_WORKSPACE)
    assert u6.symbol_reason == REASON_DATABASE and u6.footprint_hit is not None
    # A database part's footprint is still looked up; its named library is missing, so
    # every available library is searched.
    assert u7.symbol_reason == REASON_DATABASE
    assert (Path(u7.footprint_hit.path).name, u7.footprint_hit.named) == ("Made.PcbLib", False)
    # An IntLib part: its symbol and its footprint come from the IntLib.
    assert (Path(u8.symbol.path).name, u8.symbol.origin) == ("Installed.IntLib", "installed")
    assert (Path(u8.footprint_hit.path).name, u8.footprint_hit.named) == (
        "Installed.IntLib", True)
    # No library named: every available library is searched (aliases count).
    assert (Path(u9.symbol.path).name, u9.symbol.name) == ("Made.SchLib", "OLD-ALIAS")
    assert u9.footprint_reason == REASON_NOT_FOUND
    summary = resolution_summary(list(found.values()))
    assert summary["parts"] == 9
    assert summary["symbols"] == {"found (project)": 3, "found (installed)": 1,
                                  REASON_FILE_MISSING: 1, REASON_NOT_FOUND: 1,
                                  REASON_WORKSPACE: 1, REASON_DATABASE: 2}
    symbol = load_symbol(u1.symbol)
    assert [pin.designator for pin in symbol.pins] == ["1", "2", "4", "1"]
    assert load_symbol(u8.symbol).lib_reference == "OLD-1"
    assert len(load_footprint(u2.footprint_hit).pads) == 2
    assert load_footprint(u8.footprint_hit).name == "CHIP-1"


def _pcb_component(**fields):
    (component,) = decode_components([{"PATTERN": "CHIP-1", **fields}])
    return component


def test_resolve_pcb_parts_reads_a_workspace_footprint_from_its_cache(tmp_path):
    prj = _project(tmp_path)
    cache = tmp_path / "Cache" / "Released"
    cache.mkdir(parents=True)
    write_ole(cache / "Cached.PcbLib", _sample_pcb_lib())
    resolver = LibraryResolver(project_libraries(prj, installed=[]))
    results = resolver.resolve_pcb([
        _pcb_component(SOURCEFOOTPRINTLIBRARY="Made.PcbLib", SOURCECOMPONENTLIBRARY="Made.SchLib",
                       SOURCELIBREFERENCE="AMP-2", SOURCEDESIGNATOR="U1"),
        _pcb_component(SOURCEFOOTPRINTLIBRARY=str(cache / "Cached.PcbLib"),
                       SOURCECOMPONENTLIBRARY="Some Vault", VAULTGUID="{V}",
                       SOURCEDESIGNATOR="U2"),
        _pcb_component(PATTERN="Nope", SOURCEFOOTPRINTLIBRARY=str(cache / "Cached.PcbLib"),
                       ITEMGUID="{I}", SOURCEDESIGNATOR="U3"),
        _pcb_component(SOURCEFOOTPRINTLIBRARY="Some Vault", SOURCECOMPONENTLIBRARY="Some Vault",
                       SOURCEDESIGNATOR="U4"),
        _pcb_component(SOURCEFOOTPRINTLIBRARY="Gone.PcbLib", PATTERN="Absent",
                       SOURCEDESIGNATOR="U5"),
    ])
    u1, u2, u3, u4, u5 = results
    assert (u1.designator, u1.symbol.name, u1.footprint_hit.origin) == ("U1", "AMP-2", "project")
    assert (u2.symbol_reason, u2.footprint_hit.origin) == (REASON_WORKSPACE, "workspace cache")
    assert u3.footprint_reason == REASON_NOT_FOUND
    # A Workspace name with no GUID (as Altium's own content vault parts are written).
    assert (u4.symbol_reason, u4.footprint_reason) == (REASON_WORKSPACE, REASON_WORKSPACE)
    assert (u5.symbol_reason, u5.footprint_reason) == (REASON_NO_NAME, REASON_FILE_MISSING)


def test_an_unreadable_library_is_reported_not_raised(tmp_path):
    prj = _project(tmp_path)
    (tmp_path / "Libs" / "Made.PcbLib").write_bytes(b"not an OLE file")
    libs._INDEX_CACHE.clear()
    resolver = LibraryResolver(project_libraries(prj, installed=[]))
    (result,) = resolver.resolve_netlist({"components": {"U1": _component(
        lib_reference="AMP-2", library="Made.SchLib", footprint="CHIP-1")}})
    assert result.symbol is not None
    assert result.footprint_reason == REASON_NOT_FOUND
    assert [Path(path).name for path in result.errors] == ["Made.PcbLib"]


def test_library_index_is_cached_by_size_and_time(tmp_path, monkeypatch):
    path = str(write_ole(tmp_path / "Made.SchLib", _sample_sch_lib()))
    first = libs.library_index(path)
    assert first.components == ["AMP-2", "OLD-1", "A/B", "OLD-ALIAS"]
    monkeypatch.setattr(libs, "parse_sch_lib", lambda *a, **k: pytest.fail("not cached"))
    assert libs.library_index(path) is first
    assert libs.library_index(str(tmp_path / "missing.SchLib")).error


def test_embedded_libraries_are_read_from_memory():
    data = ole_bytes(_sample_pcb_lib())
    with olefile.OleFileIO(io.BytesIO(data)) as ole:
        from server.parsers.pcb_lib import pcb_lib_from_ole
        assert pcb_lib_from_ole(ole).names == ["CHIP-1", "THT/2"]
