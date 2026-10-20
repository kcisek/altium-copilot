"""The check_dfm tool (server/main.py): profile and limits arguments and their errors,
units, the header, paging and the shrink loop, the review cached across calls, a
netlist-free load, the tool's wording, and Altium's MiniPC at its worst (skipped when
absent)."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

import server.main as main_mod
from server.parsers.pcb_doc import PcbData, Primitive, parse_pcb_doc
from server.parsers.prj_pcb import VariantDefinition, VariantState
from server.services.pcb_index import PcbIndex
from tests.test_dfm import (
    EXAMPLES,
    LAYERS,
    _board,
    _hole_board,
    _parts_board,
    _spacing_board,
)
from tests.test_netlist_free_load import _load, env  # noqa: F401
from tests.test_pcb_index import pcb_data  # noqa: F401

LIMIT = 60000
# Numbers the result carries without a unit suffix: counts, ratios, oz and layer counts.
# A length here would escape the mm conversion.
UNITLESS = {
    "aspect_ratio", "aspect_ratio_drilled", "copper_layers", "copper_oz", "count",
    "drill_sizes", "drills_total", "embedded_boards", "error", "examples_per_finding",
    "findings_limited_to", "groups", "info", "layer_counts", "limit", "max_aspect_ratio",
    "max_copper_oz", "max_drill_sizes", "max_microvia_aspect_ratio", "microvia_limit",
    "microvia_worst", "next_offset", "object_pairs", "objects", "offset",
    "overlapping_pairs", "plated_holes_without_ring", "total", "touching_pairs", "value",
    "warning", "worst", "worst_aspect_ratio", "worst_microvia_aspect_ratio",
    # assembly: part counts per side, fiducial counts
    "bottom", "bottom_smd", "bottom_tht", "fiducials", "fiducials_nearby", "local_parts",
    "max_placements", "max_unique_parts", "min_global_fiducials", "placements",
    "small_passives", "top", "top_smd", "top_tht", "typical_global_fiducials", "unique_parts",
}
# Assembly limits every assembly check of MiniPC misses (part spacing at its bound).
ASSEMBLY_STRICT = {
    "max_placements": 1, "max_unique_parts": 1, "min_part_spacing_mm": 5.0,
    "min_part_to_edge_mm": 10.0, "min_global_fiducials": 10, "local_fiducial_pitch_mm": 2.54,
    "min_pitch_mm": 2.54,
}
STRICT = {
    "min_trace_mm": 1.0, "min_spacing_mm": 0.3, "min_drill_mm": 5.0, "max_drill_mm": 0.15,
    "min_annular_ring_mm": 0.5, "min_copper_to_edge_mm": 5.0, "max_aspect_ratio": 1,
    "min_hole_to_hole_mm": 0.5, "min_pad_hole_to_hole_mm": 1.0, "layer_counts": [2],
    "min_board_size_mm": [500, 500], "max_drill_sizes": 1, "copper_oz": 0.5,
    "min_route_size_mm": 5.0,
}


def _dfm_module():
    """The services.dfm module main.py imported (by either package path)."""
    return sys.modules[main_mod.review_dfm.__module__]


def _call(**arguments) -> dict:
    return json.loads(main_mod.check_dfm(**arguments))


@pytest.fixture
def board(monkeypatch, tmp_path: Path):
    """A loaded project whose PcbDoc parses to test_dfm's spacing board. Returns the
    project folder and a holder: set holder["index"] to swap the board."""
    pcb_file = tmp_path / "Demo.PcbDoc"
    pcb_file.write_bytes(b"fake")
    holder = {"index": _spacing_board()}
    session = main_mod.PcbSession()
    session._load = lambda path, netlist: holder["index"]
    monkeypatch.setattr(main_mod, "_project", {
        "name": "Demo", "root_dir": str(tmp_path), "prj_pcb_path": str(tmp_path / "Demo.PrjPcb"),
        "sheets": [], "pcb_doc_paths": [str(pcb_file)],
    })
    monkeypatch.setattr(main_mod._altium, "_netlist", {"nets": {}, "pin_to_net": {}})
    monkeypatch.setattr(main_mod, "_variant_state",
                        VariantState([VariantDefinition(name="Default")]))
    monkeypatch.setattr(main_mod, "_pcb_session", session)
    return tmp_path, holder


def _swap(holder, index) -> None:
    holder["index"] = index
    main_mod._pcb_session.invalidate()


def _many_findings_index() -> PcbIndex:
    """200 track pairs, each its own width and gap: more than 100 findings with 5 mil
    limits, and long net names."""
    nets = [f"VERY_LONG_NET_NAME_FOR_SIZE_TESTING_{number:04d}" for number in range(400)]
    primitives = []
    for number in range(200):
        x, y = (number % 20) * 100.0, (number // 20) * 100.0
        width = 1.0 + number * 0.01
        gap_y = y + width + number * 0.004
        primitives += [
            Primitive(kind="track", layer=1, net=2 * number, component=None, x1=x, y1=y,
                      x2=x + 50, y2=y, width=width),
            Primitive(kind="track", layer=1, net=2 * number + 1, component=None, x1=x,
                      y1=gap_y, x2=x + 50, y2=gap_y, width=width),
        ]
    pcb = PcbData(board=_board(), nets=nets, components=[], primitives=primitives, pours=[],
                  layer_name_to_v6=dict(LAYERS))
    return PcbIndex(pcb, None)


MANY = {"profile": "none", "limits": {"min_spacing_mil": 5, "min_trace_mil": 5}}


# ---------- header, units ----------


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _keys(obj, found: set, numbers: set, unit: bool = False) -> None:
    """Every key in obj into found; keys holding numbers (or lists of numbers) into
    numbers, except inside a unit key (limit_mil {via, pth} ...)."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            found.add(key)
            if not unit and (_is_number(value) or isinstance(value, list) and value and all(
                    _is_number(v) for v in value)):
                numbers.add(key)
            _keys(value, found, numbers, unit or key.endswith(("_mil", "_mm")))
    elif isinstance(obj, list):
        for value in obj:
            _keys(value, found, numbers, unit)


def _same_numbers(mil, mm, converted: bool) -> None:
    """mm is mil converted (inside a *_mil key) or equal (elsewhere); issue texts and
    the profile echo, which are written in the output units, are skipped."""
    if isinstance(mil, dict):
        assert isinstance(mm, dict)
        for key, value in mil.items():
            if key in ("issue", "profile", "scope"):
                continue
            is_mil = key.endswith("_mil")
            other = key[:-4] + "_mm" if is_mil else key
            assert other in mm, other
            _same_numbers(value, mm[other], converted or is_mil)
    elif isinstance(mil, list):
        assert isinstance(mm, list) and len(mm) == len(mil)
        for first, second in zip(mil, mm):
            _same_numbers(first, second, converted)
    elif _is_number(mil) and converted:
        assert mm == pytest.approx(round(mil * 0.0254, 4), abs=1e-4)
    elif not isinstance(mil, str):
        assert mm == mil


def test_header_and_every_length_in_mm(board):
    root, holder = board
    for index in (_spacing_board(), _hole_board()):
        _swap(holder, index)
        for arguments in ({}, {"profile": "none", "limits": STRICT}):
            mil = _call(units="mil", **arguments)
            mm = _call(units="mm", **arguments)
            assert list(mil)[:5] == ["units", "pcb_doc", "origin_mil", "scope", "checks_run"]
            assert list(mm)[:4] == ["units", "pcb_doc", "origin_mm", "scope"]
            assert (mil["units"], mm["units"], mil["pcb_doc"]) == ("mil", "mm", "Demo.PcbDoc")
            assert mil["origin_mil"] == [100.0, 50.0] and mm["origin_mm"] == [2.54, 1.27]
            assert list(mil)[-2:] == list(mm)[-2:] == ["not_checked", "notes"]
            found, numbers = set(), set()
            _keys(mm, found, numbers)
            assert not [key for key in found if key.endswith("_mil")]
            assert numbers - UNITLESS == {key for key in numbers if key.endswith("_mm")}
            found, numbers = set(), set()
            _keys(mil, found, numbers)
            assert not [key for key in found if key.endswith("_mm")]
            assert numbers - UNITLESS == {key for key in numbers if key.endswith("_mil")}
            _same_numbers(mil, mm, False)
            # The profile echo comes in the output units, rounded, with keys to pass back.
            rows = {row["limit"]: row for row in mm["profile"]["limits"]}
            assert all(not key.endswith("_mil") for key in rows)
            if arguments:
                assert rows["min_spacing_mm"]["value_mm"] == 0.3
                assert {row["limit"]: row for row in mil["profile"]["limits"]}[
                    "min_spacing_mil"]["value_mil"] == pytest.approx(11.811)
    # Example locations are origin-relative in either unit.
    _swap(holder, _spacing_board())
    finding = next(f for f in _call(units="mm")["findings"] if f.get("examples"))
    assert all(len(example["at_mm"]) == 2 for example in finding["examples"])


def test_scope_and_default_profile(board):
    result = _call()
    assert result["scope"].startswith("Checked against Altimade's published limits (default")
    assert result["profile"]["chosen_by"] == "default"
    assert "scope" not in result["profile"]
    assert set(result["results"]) == set(result["checks_run"])
    chosen = _call(profile="ALTIMADE")
    assert chosen["profile"]["chosen_by"] == "argument"
    assert chosen["scope"].startswith("Checked against Altimade's published limits (page")


# ---------- profile argument ----------


def test_profile_errors_and_files(board):
    root, _ = board
    missing = _call(profile="jlcpcb")
    assert missing["error"] == "profile_not_found"
    assert missing["builtin"] == ["altimade", "none"] and missing["files"] == []
    assert "Never substitute another profile" in missing["hint"]
    assert _call(profile="..\\x.json")["error"] == "profile_not_found"
    (root / "acme.json").write_text(json.dumps({
        "name": "Acme 4-layer", "source": "acme.example rev C",
        "limits": {"min_spacing_mm": 0.09, "min_trace_mm": 0.09},
    }), encoding="utf-8")
    assert _call(profile="jlcpcb")["files"] == ["acme.json"]
    for name in ("acme", "acme.json"):
        acme = _call(profile=name)
        assert acme["profile"]["name"] == "Acme 4-layer"
        assert acme["profile"]["chosen_by"] == "argument"
        assert "the profile file acme.json" in acme["scope"]
        # Nothing inherited: only the file's limits are checked.
        assert acme["results"]["hole_size"]["status"] == "not_checked"
        assert "Acme" not in json.dumps(acme["findings"])  # user text stays out of issues
    # The project's fab_profile.json is the default; a broken one is an error that names
    # the way out.
    (root / "fab_profile.json").write_text('{"name": "House", "limits": {\n"min_trace_mm" 1}}',
                                           encoding="utf-8")
    broken = _call()
    assert broken["error"] == "invalid_profile" and broken["file"] == "fab_profile.json"
    assert (broken["line"], broken["column"]) == (2, 16)
    # The way out is fixing the file, not quietly checking against Altimade instead.
    assert broken["message"].endswith("Fix the file, then call again.")
    assert "altimade" not in broken["message"].lower()
    assert "Never substitute another profile" in broken["hint"]
    assert _call(profile="altimade")["profile"]["chosen_by"] == "argument"
    (root / "fab_profile.json").write_text(json.dumps({
        "name": "House", "based_on": "altimade", "limits": {"min_spacing_mil": 4},
    }), encoding="utf-8")
    house = _call()
    assert house["profile"]["chosen_by"] == "project_file"
    assert "the project's fab_profile.json (based on Altimade" in house["scope"]
    origins = {row["limit"]: row["origin"] for row in house["profile"]["limits"]}
    assert origins["min_spacing_mil"] == "file" and origins["min_trace_mil"] == "builtin"


# ---------- limits argument ----------


@pytest.mark.parametrize("limits, error, text", [
    ({"min_trace": 0.1}, "invalid_limits", "use min_trace_mm or min_trace_mil"),
    ({"max_aspect_ratio_mm": 10}, "invalid_limits", "takes no unit suffix"),
    ({"min_spacing_mm": 0.1, "min_spacing_mil": 4}, "invalid_limits", "given twice"),
    ({"min_spacing_mm": 5}, "invalid_limits", "did you mean min_spacing_mil?"),
    ({"min_trace_mil": 0.15}, "invalid_limits", "did you mean min_trace_mm?"),
    ({"layer_counts": [2, 4.5]}, "invalid_limits", "whole numbers"),
    ({"min_board_size_mm": [10, 20, 30]}, "invalid_limits", "a list of two numbers"),
    ({"bogus_mm": 1}, "invalid_limits", "unknown limit 'bogus_mm'"),
])
def test_limits_errors(board, limits, error, text):
    result = _call(limits=limits)
    assert result["error"] == error and text in result["message"]
    if "unknown" in text:
        assert "min_trace_mm" in result["valid_keys"]


def test_limits_null_lists_and_suffixes(board):
    # null removes a limit: Altimade's spacing is not checked.
    result = _call(profile="altimade", limits={"min_spacing_mm": None})
    assert result["results"]["spacing"]["status"] == "not_checked"
    rows = {row["limit"]: row for row in result["profile"]["limits"]}
    assert rows["min_spacing_mil"]["value_mil"] is None
    assert rows["min_spacing_mil"]["origin"] == "argument"
    # Lists: layer counts and an orientation-free board size in mm.
    sized = _call(profile="none", limits={
        "layer_counts": [2, 4], "min_board_size_mm": [2.54, 5.08],
        "max_board_size_mm": [377.952, 100], "max_aspect_ratio": 8,
    })
    assert sized["results"]["layer_count"]["status"] == "pass"  # 4 copper layers
    assert sized["results"]["board_size"]["status"] == "pass"
    assert _call(profile="none", limits={"layer_counts": [2, 6]})["results"]["layer_count"][
        "status"] == "fail"
    # The same limit in mil or mm checks the same.
    in_mil = _call(profile="none", limits={"min_spacing_mil": 5})
    in_mm = _call(profile="none", limits={"min_spacing_mm": 0.127})
    assert in_mil["results"]["spacing"]["objects"] == in_mm["results"]["spacing"]["objects"]


def _through_mcp(arguments: dict) -> dict:
    content = asyncio.run(main_mod.mcp.call_tool("check_dfm", arguments))
    blocks = content[0] if isinstance(content, tuple) else content
    return json.loads(blocks[0].text)


def test_the_limits_schema_takes_nulls_lists_and_floats(board):
    """The MCP argument schema (pydantic) lets nulls, lists and mm sizes through to
    check_dfm's own validation."""
    result = _through_mcp({"profile": "none", "limits": {
        "min_spacing_mm": 0.1, "max_aspect_ratio": None, "layer_counts": [2, 4],
        "min_board_size_mm": [2.54, 377.952],
    }})
    assert result["profile"]["chosen_by"] == "argument"
    rows = {row["limit"]: row for row in result["profile"]["limits"]}
    assert rows["layer_counts"]["value"] == [2, 4]
    assert _through_mcp({"limits": {"min_trace": 0.1}})["error"] == "invalid_limits"
    # A JSON string is parsed by FastMCP first, as for other object arguments.
    assert _through_mcp({"profile": "none", "limits": '{"min_trace_mm": 0.09}'})[
        "profile"]["limits"][0]["limit"] == "min_trace_mil"


@pytest.mark.parametrize("limits", [
    {"min_trace_mm": True}, {"max_drill_sizes": True}, {"layer_counts": [True, 2]},
    {"min_trace_mm": "0.1"}, {"layer_counts": "4"}, {"min_trace_mm": "0.1mm"},
    {"min_trace_mm": {"value": 0.1}}, {"min_board_size_mm": [[1, 2]]},
    {"max_drill_sizes": 10 ** 400}, {"min_trace_mil": 10 ** 400},
    [["min_trace_mm", 0.1]], "min_trace_mm=0.1", 5,
])
def test_limits_of_the_wrong_type_answer_invalid_limits_through_mcp(board, limits):
    """Through FastMCP's argument model nothing is coerced (true is not 1, "0.1" is not
    0.1) and nothing fails with a pydantic error: check_dfm answers invalid_limits."""
    result = _through_mcp({"profile": "none", "limits": limits})
    assert result["error"] == "invalid_limits", result
    assert "pydantic" not in result["message"]


def test_the_limits_schema_still_describes_an_object():
    tools = {tool.name: tool for tool in asyncio.run(main_mod.mcp.list_tools())}
    schema = tools["check_dfm"].inputSchema["properties"]["limits"]
    kinds = [option.get("type") for option in schema["anyOf"]]
    assert kinds == ["object", "null"] and schema["default"] is None
    values = schema["anyOf"][0]["additionalProperties"]["anyOf"]
    assert [value["type"] for value in values] == ["number", "array", "null"]


# ---------- other errors ----------


def test_errors_before_any_review(board, monkeypatch):
    assert _call(checks=["spacing", "bogus"])["error"] == "invalid_checks"
    assert _call(units="inch")["error"] == "invalid_units"
    monkeypatch.setattr(main_mod, "_project", None)
    assert _call()["error"] == "no_project"


def test_without_a_netlist(env):
    root, _, loads = env
    _load(root)
    result = _call()
    assert "error" not in result and result["profile"]["chosen_by"] == "default"
    assert result["pcb_doc"] == "Board.PcbDoc"
    assert all(netlist is None for netlist in loads)


# ---------- paging, shrinking, the cache ----------


def test_paging_and_next_offset(board, monkeypatch):
    _, holder = board
    _swap(holder, _many_findings_index())
    # Every page fits; next_offset walks through every finding once.
    issues, offset = [], 0
    while True:
        text = main_mod.check_dfm(offset=offset, **MANY)
        assert len(text) <= LIMIT
        page = json.loads(text)
        assert page["offset"] == offset
        assert len(page["findings"]) == min(page.get("findings_limited_to", 100),
                                            page["total"] - offset)
        issues += [finding["issue"] for finding in page["findings"]]
        if not page["has_more"]:
            assert "next_offset" not in page
            break
        assert page["next_offset"] == offset + len(page["findings"])
        offset = page["next_offset"]
    assert page["total"] > 100 and len(issues) == len(set(issues)) == page["total"]
    # A smaller result size: fewer findings and examples per page, next_offset follows.
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 20000)
    trimmed_text = main_mod.check_dfm(**MANY)
    trimmed = json.loads(trimmed_text)
    assert len(trimmed_text) <= 20000
    assert trimmed["findings_limited_to"] in (40, 15, 5)
    assert trimmed["next_offset"] == trimmed["findings_limited_to"]
    assert all(len(f.get("examples", [])) <= trimmed["examples_per_finding"]
               for f in trimmed["findings"])


def test_never_returns_an_oversized_string(board, monkeypatch):
    _, holder = board
    _swap(holder, _hole_board())
    full = _call(profile="none", limits=STRICT)
    assert full["drills"] and "drills_total" not in full
    # Too small for 5 findings: the verdict without findings, and how to get them.
    smallest_page = len(main_mod.check_dfm(profile="none", limits=STRICT, offset=10 ** 6))
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", smallest_page - 200)
    brief_text = main_mod.check_dfm(profile="none", limits=STRICT)
    brief = json.loads(brief_text)
    assert len(brief_text) <= smallest_page - 200
    assert "findings" not in brief and brief["total"] == full["total"]
    assert brief["results"] == full["results"] and brief["drills_total"] == len(full["drills"])
    assert "checks narrowed to one check" in brief["findings_omitted"]
    # Smaller still: the statuses alone.
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 1500)
    last_text = main_mod.check_dfm(profile="none", limits=STRICT)
    last = json.loads(last_text)
    assert len(last_text) <= 1500
    assert last["results"] == {check: entry["status"] for check, entry in full["results"].items()}


def test_drill_table_is_cut_before_the_verdict_alone(board, monkeypatch):
    _, holder = board
    _swap(holder, _hole_board())
    page = _call(profile="none", limits=STRICT, offset=10 ** 6)
    assert len(page["drills"]) == 7
    monkeypatch.setattr(main_mod, "DRILL_ROWS_LISTED", 2)
    size = len(main_mod.check_dfm(profile="none", limits=STRICT, offset=10 ** 6))
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", size - 1)
    cut = _call(profile="none", limits=STRICT, offset=10 ** 6)
    assert len(cut["drills"]) == 2 and cut["drills_total"] == 7
    assert (cut["findings_limited_to"], cut["examples_per_finding"]) == (5, 0)


def test_review_is_computed_once_per_board_profile_and_units(board, monkeypatch):
    root, holder = board
    runs = []
    review = _dfm_module()._Review
    original = review.run

    def counted(self):
        runs.append(self)
        return original(self)

    monkeypatch.setattr(review, "run", counted)
    _call()
    _call(offset=1)
    _call(units="mil")  # the board's display unit is mil: the same review
    _call(profile=None, limits={})
    assert len(runs) == 1
    assert len(holder["index"]._dfm_cache) == 1
    _call(units="mm")
    _call(limits={"min_spacing_mil": 4})
    assert len(runs) == 3
    # A profile file edited between calls is read again and reviewed again.
    (root / "fab_profile.json").write_text(json.dumps({"limits": {"min_trace_mil": 3}}),
                                           encoding="utf-8")
    _call()
    (root / "fab_profile.json").write_text(json.dumps({"limits": {"min_trace_mil": 4}}),
                                           encoding="utf-8")
    _call()
    assert len(runs) == 5
    # A board saved again is a new index, so a new review.
    _swap(holder, _spacing_board())
    _call(profile="altimade")
    assert len(runs) == 6


# ---------- wording ----------


def test_tool_wording_triggers_and_metadata():
    tools = {tool.name: tool for tool in asyncio.run(main_mod.mcp.list_tools())}
    tool = tools["check_dfm"]
    description = " ".join(tool.description.split())
    for phrase in ('"is this board manufacturable?"', '"DFM check"', "JLCPCB",
                   "meets <profile>'s limits on the checks run", 'profile="none" with limits',
                   "check_rule_compliance", "get_drc_report", "check_vias", "check_pcb_health",
                   "next_offset", "add origin_mil for query_pcb_region"):
        assert phrase in description, phrase
    assert tool.annotations.readOnlyHint is True
    assert tool.meta["anthropic/maxResultSizeChars"] == LIMIT
    instructions = main_mod.SERVER_INSTRUCTIONS
    assert instructions.index("18. check_pcb_health") < instructions.index("19. check_dfm")
    assert "a fab or drill review" not in instructions
    assert "fab review" not in " ".join(tools["check_vias"].description.split())
    manifest = json.loads((Path(__file__).parents[1] / "manifest.json").read_text("utf-8"))
    entries = {entry["name"]: entry["description"] for entry in manifest["tools"]}
    assert "fab review" not in entries["check_vias"] and "fabricator" in entries["check_dfm"]
    readme = (Path(__file__).parents[1] / "README.md").read_text("utf-8")
    assert "Any fab issues?" not in readme and "`check_dfm`" in readme


def test_readme_not_modelled_is_scoped_to_the_rule_checks():
    # check_dfm models pour copper, regions, fills, slots and per-layer pads: the
    # paragraph must not list them as unmodelled for every check.
    readme = (Path(__file__).parents[1] / "README.md").read_text("utf-8")
    paragraph = " ".join(readme[readme.index("Not modelled"):].split("\n\n")[0].split())
    assert paragraph.startswith("Not modelled by the rule checks (`check_rule_compliance`")
    assert "`check_dfm` checks pad holes, slots and rings per layer" in paragraph
    assert "only `check_dfm` checks pad holes and rings" not in paragraph


def _item(instructions: str, number: int) -> list[str]:
    """The lines of numbered item `number` in the server instructions."""
    lines = instructions.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{number}. "))
    end = next(i for i in range(start + 1, len(lines)) if not lines[i].startswith("    "))
    return lines[start:end]


def test_status_is_read_with_severity():
    """A check whose only misses are info (Altimade's ordering limits while it is only
    the default) is advisory: the docstring and item 19 define each status, and say to
    answer from status and severity together."""
    tools = {tool.name: tool for tool in asyncio.run(main_mod.mcp.list_tools())}
    description = " ".join(tools["check_dfm"].description.split())
    assert "answer from status and severity together" in description
    for phrase in ("Status is fail (a miss of severity error or warning)",
                   "advisory (only info misses", "not a fabrication miss, report it as a note",
                   "at most 1 mil", "spacing counts places"):
        assert phrase in description, phrase
    item = " ".join(" ".join(_item(main_mod.SERVER_INSTRUCTIONS, 19)).split())
    assert "status and severity together" in item
    assert "status advisory: its only misses are info" in item
    assert "Answer from results, quote" not in item


def test_advisory_status_through_the_tool(board):
    """Copper weight is its own check; on the default profile a miss of Altimade's
    service limits only is advisory, never fail."""
    _, holder = board
    _swap(holder, _heavy_copper_index())
    default = _call(checks=["layer_count", "copper_weight"])["results"]
    assert {check: (entry["status"], entry.get("severity")) for check, entry in
            default.items()} == {"layer_count": ("pass", None),
                                 "copper_weight": ("advisory", "info")}
    chosen = _call(profile="altimade", checks=["copper_weight"])["results"]
    assert (chosen["copper_weight"]["status"], chosen["copper_weight"]["severity"]) == (
        "fail", "warning")


def _heavy_copper_index() -> PcbIndex:
    pcb = PcbData(board=_board(copper=2.8), nets=[], components=[], primitives=[],
                  pours=[], layer_name_to_v6=dict(LAYERS))
    return PcbIndex(pcb, None)


def test_items_16_19_and_20_are_wrapped():
    for number in (16, 19, 20):
        lines = _item(main_mod.SERVER_INSTRUCTIONS, number)
        assert len(lines) > 2
        assert max(len(line) for line in lines) <= 90, number


# ---------- assembly and the manufacturing review ----------

BOM_NETLIST = {
    "nets": {}, "pin_to_net": {},
    "components": {
        "R1": {"kind": 0, "params": {"comment": "10k"}, "footprint": "R0402"},
        "R2": {"kind": 0, "params": {"comment": "10k"}, "footprint": "R0402"},
        "R3": {"kind": 0, "params": {"comment": "22k"}, "footprint": "R0402"},
        "J1": {"kind": 0, "params": {"comment": "hdr"}, "footprint": "HDR"},
    },
}


def test_assembly_follows_the_active_variant_and_the_bom(board, monkeypatch):
    """Not Fitted parts of the active variant are not placed; with a netlist the unique
    parts are the BOM's lines (R3 is 22k: 3 lines, where footprint and the PCB's Comment
    give 2), without one they are approximate."""
    _, holder = board
    _swap(holder, _parts_board())  # R1-R3 (Comment 10k) and J1
    state = VariantState([VariantDefinition(name="Lite", dnp_refdes=["R1"]),
                          VariantDefinition(name="Full")])
    monkeypatch.setattr(main_mod, "_variant_state", state)
    monkeypatch.setattr(main_mod._altium, "_netlist", BOM_NETLIST)
    arguments = {"checks": ["part_counts"], "profile": "none",
                 "limits": {"max_placements": 2, "max_unique_parts": 2}}
    lite = _call(**arguments)
    entry = lite["results"]["part_counts"]
    assert entry["value"] == {"placements": 3, "unique_parts": 3}
    assert "unique_parts_approximate" not in entry and entry["status"] == "fail"
    assert lite["board_minimums"]["unique_parts"] == {"value": 3, "approximate": False}
    note = next(note for note in lite["notes"] if "Assembly checks" in note)
    assert "active variant 'Lite'" in note and "No netlist" not in note
    state.set_variant("Full")  # the cached review is per variant
    full = _call(**arguments)
    assert full["results"]["part_counts"]["value"] == {"placements": 4, "unique_parts": 3}
    monkeypatch.setattr(main_mod._altium, "_netlist", None)  # netlist-free
    free = _call(**arguments)
    assert free["results"]["part_counts"]["value"] == {"placements": 4, "unique_parts": 2}
    assert free["results"]["part_counts"]["unique_parts_approximate"] is True
    assert any("No netlist is loaded" in note for note in free["notes"])


def test_unique_parts_count_only_the_fitted_lines(board, monkeypatch):
    """R3 (22k) has a BOM line of its own: Not Fitted, the line goes (3 lines to 2)."""
    _, holder = board
    _swap(holder, _parts_board())
    monkeypatch.setattr(main_mod, "_variant_state", VariantState(
        [VariantDefinition(name="NoR3", dnp_refdes=["R3"])]))
    monkeypatch.setattr(main_mod._altium, "_netlist", BOM_NETLIST)
    result = _call(checks=["part_counts"], profile="none", limits={"max_unique_parts": 1})
    assert result["results"]["part_counts"]["value"] == {"placements": 3, "unique_parts": 2}
    assert result["board_minimums"]["unique_parts"] == {"value": 2, "approximate": False}


def test_the_cached_review_follows_a_variants_not_fitted_list(board, monkeypatch):
    """The same variant name with another Not Fitted list (the project edited and
    reloaded, the index kept) is another review."""
    _, holder = board
    _swap(holder, _parts_board())
    monkeypatch.setattr(main_mod._altium, "_netlist", None)  # the index is kept
    monkeypatch.setattr(main_mod, "_variant_state", VariantState(
        [VariantDefinition(name="Lite", dnp_refdes=[])]))
    arguments = {"checks": ["part_counts"], "profile": "none", "limits": {"max_placements": 9}}
    assert _call(**arguments)["results"]["part_counts"]["value"]["placements"] == 4
    monkeypatch.setattr(main_mod, "_variant_state", VariantState(
        [VariantDefinition(name="Lite", dnp_refdes=["R1", "R2"])]))
    assert _call(**arguments)["results"]["part_counts"]["value"]["placements"] == 2


def test_manufacturing_review_tool(board, monkeypatch):
    root, _ = board
    review = main_mod.pcb_manufacturing_review()
    with pytest.raises(ValueError):
        json.loads(review)  # a prompt, not JSON
    assert "Demo" in review and "Phase 3" in review and "check_dfm(" in review
    for check in ("part_counts", "tombstoning", "rules_vs_profile"):
        assert check in review
    monkeypatch.setattr(main_mod._altium, "_netlist", None)
    assert "Netlist-free load" in main_mod.pcb_manufacturing_review()
    project = dict(main_mod._project, pcb_doc_paths=[])
    monkeypatch.setattr(main_mod, "_project", project)
    missing = json.loads(main_mod.pcb_manufacturing_review())
    assert missing["error"] and "get_bom and check_bom" in missing["hint"]
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.pcb_manufacturing_review())["error"] == "no_project"
    tools = {tool.name: tool for tool in asyncio.run(main_mod.mcp.list_tools())}
    tool = tools["pcb_manufacturing_review"]
    assert tool.annotations.readOnlyHint is True and tool.title == "PCB Manufacturing Review"


def test_assembly_wording_manifest_and_readme():
    tools = {tool.name: tool for tool in asyncio.run(main_mod.mcp.list_tools())}
    description = " ".join(tools["check_dfm"].description.split())
    for phrase in ('"can it be assembled?"', '"DFA check"', "fiducials", "tombstoning",
                   "part spacing", '"too many unique parts for Altimade?"',
                   "its Not Fitted parts left out", "is measured, not judged: ask the assembler",
                   "a full manufacturing review (pcb_manufacturing_review)"):
        assert phrase in description, phrase
    review = " ".join(tools["pcb_manufacturing_review"].description.split())
    assert "'is this board ready to order?'" in review and "check_dfm" in review
    item = " ".join(" ".join(_item(main_mod.SERVER_INSTRUCTIONS, 19)).split())
    assert '"can it be assembled?"' in item and "tombstoning" in item
    twenty = " ".join(" ".join(_item(main_mod.SERVER_INSTRUCTIONS, 20)).split())
    assert twenty.startswith('20. pcb_manufacturing_review — "is this board ready to order?"')
    assert main_mod.SERVER_INSTRUCTIONS.index("20. pcb_manufacturing_review") > 4096
    manifest = json.loads((Path(__file__).parents[1] / "manifest.json").read_text("utf-8"))
    entries = {entry["name"]: entry["description"] for entry in manifest["tools"]}
    assert "go/no-go" in entries["pcb_manufacturing_review"]
    assert "fiducials" in entries["check_dfm"] and "tombstoning" in entries["check_dfm"]
    readme = (Path(__file__).parents[1] / "README.md").read_text("utf-8")
    modes = readme[readme.index("## Specialist modes"):]
    assert "| `pcb_manufacturing_review` |" in modes.split("\n\n## ")[0]
    assert '*"Can it be assembled? Any tombstoning risk? Enough fiducials?"*' in readme


def test_every_check_and_limit_rule_is_in_the_docstring():
    tools = {tool.name: tool for tool in asyncio.run(main_mod.mcp.list_tools())}
    description = " ".join(tools["check_dfm"].description.split())
    for check in _dfm_module().VALID_CHECKS:
        assert check in description, check
    for phrase in ("min_hole_to_copper falls back to min_spacing",
                   "min_hole_to_edge to min_copper_to_edge", "never text",
                   "(chosen_by project_file)", "(chosen_by default)"):
        assert phrase in description, phrase


# ---------- Altium's examples ----------


@pytest.fixture(scope="module")
def minipc_index():
    path = next(EXAMPLES.rglob("MiniPC.PcbDoc"), None) if EXAMPLES.is_dir() else None
    if path is None:
        pytest.skip("Altium example boards not installed")
    return PcbIndex(parse_pcb_doc(str(path)), None)


def test_minipc_worst_case_fits_through_the_tool(board, minipc_index):
    _, holder = board
    _swap(holder, minipc_index)
    for units in ("mm", "mil"):
        text = main_mod.check_dfm(profile="none", limits={**STRICT, **ASSEMBLY_STRICT},
                                  units=units)
        assert len(text) <= LIMIT
        result = json.loads(text)
        assert result["has_more"] and result["findings_limited_to"] in (40, 15, 5)
        assert {check: entry["status"] for check, entry in result["results"].items()
                if entry["status"] != "fail"} == {"drill_count": "advisory",
                                                  "assembly_sides": "advisory"}
    default = json.loads(main_mod.check_dfm())
    assert len(json.dumps(default, indent=2)) <= LIMIT
    assert default["results"]["spacing"]["objects"] == 14001  # places, not object pairs
