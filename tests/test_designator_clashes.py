"""One designator naming different components on a sheet: only the last one's metadata is
loaded, so the clash is reported in set_project_dir / refresh_netlist diagnostics."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from altium import AltiumClient
from server.parsers.sch_doc import components_from_stream
from tests.test_sch_doc_parser import _part, _parts_stream

STREAMS = {
    "Power": _parts_stream(
        _part("U5", "AAAA0001", "1", lib="LDO", part_count="2", pins={"1": "VIN"}),
        _part("U5", "BBBB0001", "1", pins={"1": "OUT1"}),
        _part("R1", "CCCC0001", "1", lib="RES", part_count="2"),
    ),
    "Logic": _parts_stream(
        _part("U7", "DDDD0001", "1", pins={"1": "OUT1"}),
        _part("U7", "DDDD0002", "2", pins={"7": "OUT2"}),
    ),
}


def _fake_parse(path: str, clashes=None):
    return components_from_stream(STREAMS[Path(path).stem], source=path, clashes=clashes)


def _clash(sheet: str, n: int) -> dict:
    return {"sheet": sheet, "refdes": f"U{n}", "lib_references": ["LDO", "LM339"]}


def _enrich(tmp_path, *sheets: str) -> dict:
    paths = []
    for sheet in sheets:
        path = tmp_path / f"{sheet}.SchDoc"
        path.write_bytes(b"")
        paths.append(str(path))
    netlist = {"nets": {}, "pin_to_net": {"U5": {"1": "N1"}, "R1": {}, "U7": {"7": "N2"}}}
    with patch("altium.parse_sch_doc", side_effect=_fake_parse):
        AltiumClient()._enrich_from_schdocs(paths, netlist)
    return netlist


def test_enrichment_records_designator_clashes_per_sheet(tmp_path):
    netlist = _enrich(tmp_path, "Logic", "Power")
    assert netlist["designator_clashes"] == [
        {"sheet": "Power", "refdes": "U5", "lib_references": ["LDO", "LM339"]},
    ]
    assert netlist["components"]["U5"]["unique_id"] == "BBBB0001"  # the later one, as before
    assert netlist["sheet_errors"] == []


def test_enrichment_without_clashes_has_no_clash_key(tmp_path):
    netlist = _enrich(tmp_path, "Logic")
    assert "designator_clashes" not in netlist


def test_load_diagnostics_lists_clashes():
    import main
    diagnostics = main._load_diagnostics({"designator_clashes": [_clash("Power", 5)]})
    assert diagnostics["designator_clash_count"] == 1
    assert diagnostics["designator_clashes"] == [_clash("Power", 5)]
    assert "Only the last one's metadata" in diagnostics["designator_clashes_hint"]
    assert "listed" not in diagnostics["designator_clashes_hint"]


def test_load_diagnostics_caps_long_clash_lists():
    import main
    clashes = [_clash("Power", n) for n in range(main._MAX_DESIGNATOR_CLASHES + 5)]
    diagnostics = main._load_diagnostics({"designator_clashes": clashes})
    assert diagnostics["designator_clash_count"] == len(clashes)
    assert diagnostics["designator_clashes"] == clashes[:main._MAX_DESIGNATOR_CLASHES]
    assert (f"Only the first {main._MAX_DESIGNATOR_CLASHES} are listed"
            in diagnostics["designator_clashes_hint"])


def test_load_diagnostics_without_clashes():
    import main
    assert not any(key.startswith("designator_clash") for key in main._load_diagnostics({}))


def test_refresh_netlist_reports_designator_clashes(tmp_path):
    project = {"prj_pcb_path": str(tmp_path / "proj.PrjPcb"), "root_dir": str(tmp_path)}
    with patch("main._project", project), \
         patch("main._altium") as mock_altium, \
         patch("main._variant_state", MagicMock()), \
         patch("main._pcb_session"), \
         patch("main.upsert_registry_entry"):
        mock_altium._netlist = {"nets": {}, "pin_to_net": {}, "components": {},
                                "designator_clashes": [_clash("Power", 5)]}
        mock_altium.generate_netlist.return_value = True
        from main import refresh_netlist
        result = json.loads(refresh_netlist())
    assert result["refreshed"] is True
    assert result["designator_clash_count"] == 1
    assert result["designator_clashes"] == [_clash("Power", 5)]
    assert "refresh_netlist" in result["designator_clashes_hint"]


def test_refresh_netlist_cache_hit_mentions_designator_clashes():
    import main
    clashes = [_clash("Power", n) for n in range(main._MAX_DESIGNATOR_CLASHES + 2)]
    with patch("main._project", {"prj_pcb_path": "/fake/proj.PrjPcb"}), \
         patch("main._altium") as mock_altium, \
         patch("main._variant_state", MagicMock()):
        mock_altium._netlist = {"nets": {}, "pin_to_net": {}, "components": {},
                                "designator_clashes": clashes}
        mock_altium.generate_netlist.return_value = False
        result = main.refresh_netlist()
    assert "up to date" in result
    assert ("Sheet 'Power': U0 names 2 different components (LDO, LM339); "
            "only the last one is loaded") in result
    assert result.count("different components") == main._MAX_DESIGNATOR_CLASHES
    assert "... and 2 more designator clashes" in result
