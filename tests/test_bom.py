"""Per-variant BOM and offline BOM checks: parameters, variants, grouping, checks, tools."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import server.main as main_mod
from server.altium import AltiumClient, enrich_netlist_sheets
from server.parsers.prj_pcb import (
    VariantDefinition, VariantState, Variation, parse_prj_pcb,
)
from server.parsers.sch_doc import ComponentMeta, components_from_stream
from server.services.bom import (
    BASE_VARIANT, BomPart, build_parts, compare_variants, group_lines, part_numbers,
    plan_variant, resolve, to_csv, variant_names,
)
from server.services.bom_checks import normalize_value, run_bom_checks
from tests.test_sch_doc_parser import _stream

FIXTURES = Path(__file__).parent / "fixtures"


# ---------- parameters ----------

def test_sheet_parts_keep_every_parameter_and_their_library():
    data = _stream(
        "|RECORD=1|LibReference=RES|DesignItemId=RES-0603|UniqueID=AAAAAAAA",          # 1
        "|RECORD=34|OwnerIndex=0|Name=Designator|Text=R1",                             # 2
        "|RECORD=41|OwnerIndex=0|Name=Comment|Text==Value",                            # 3
        "|RECORD=41|OwnerIndex=0|Name=Value|Text=10k",                                 # 4
        "|RECORD=41|OwnerIndex=0|Name=Manufacturer 1|Text=Yageo",                      # 5
        "|RECORD=41|OwnerIndex=0|Name=Manufacturer Part Number 1|Text=RC0603FR-0710KL",  # 6
    )
    r1 = components_from_stream(data)["R1"]
    assert (r1.lib_reference, r1.design_item_id) == ("RES", "RES-0603")
    assert r1.params == {
        "comment": "=Value", "value": "10k", "manufacturer 1": "Yageo",
        "manufacturer part number 1": "RC0603FR-0710KL",
    }


@pytest.mark.parametrize("params, expected", [
    ({"manufacturer part number 1": "A1", "manufacturer 1": "Acme", "manufacturer": "Other"},
     ("A1", "Acme", "parameter")),
    ({"manufacturer part number 2": "B2", "manufacturer part number 1": "A1",
      "manufacturer 2": "Two", "manufacturer 1": "One"}, ("A1", "One", "parameter")),
    ({"manufacturer_part_number": "X1", "manufacturer_name": "M"}, ("X1", "M", "parameter")),
    ({"manufacturer_part_1": "Y2", "mfr": "Mk"}, ("Y2", "Mk", "parameter")),
    ({"mfr pn": "Z9"}, ("Z9", None, "parameter")),
    ({"part number": "PN9"}, ("PN9", None, "part number")),
    ({"comment": "LM358", "manufacturer part number 1": "*"}, (None, None, None)),
])
def test_part_numbers(params, expected):
    assert part_numbers(params) == expected


def test_special_strings_resolve_with_hops():
    assert resolve("=Value", {"value": "=Cap", "cap": "100n"}) == ("100n", True)
    assert resolve("=Missing", {}) == ("=Missing", False)
    assert resolve("plain", {}) == ("plain", True)
    loop = {"a": "=B", "b": "=A"}
    assert resolve("=A", loop)[1] is False


# ---------- variants ----------

PRJ = """[Design]
CurrentVariant=Lite

[ProjectVariant1]
Description=Full
VariationCount=0

[ProjectVariant2]
Description=Lite
VariationCount=3
Variation1=Designator=R2|UniqueId=\\RUID2|Kind=1|AlternatePart=
Variation2=Designator=R9|UniqueId=\\RUID3|Kind=1|AlternatePart=NP
Variation3=Designator=C1|UniqueId=\\SHEET\\CUID1|Kind=2|AlternatePart=CAP|AltLibLink_Footprint=C0402|AltLibLink_IsSameFootprint=False
ParamVariationCount=2
ParamDesignator1=C1
ParamVariation1=ParameterName=Manufacturer Part Number 1|VariantValue=GRM155
ParamDesignator2=R1
ParamVariation2=ParameterName=Comment|VariantValue=4k7
"""


@pytest.fixture
def variants(tmp_path):
    path = tmp_path / "Demo.PrjPcb"
    path.write_text(PRJ, encoding="utf-8")
    return parse_prj_pcb(str(path))


def test_variant_parsing(variants):
    assert variants.current_variant == "Lite"
    full, lite = variants.variants
    assert full.variations == [] and lite.dnp_refdes == ["R2", "R9"]  # Kind=1 with 'NP'
    alternate = lite.variations[2]
    assert (alternate.kind, alternate.uid_tail, alternate.alt["Footprint"]) == (2, "CUID1", "C0402")
    assert lite.param_variations == {
        "C1": {"manufacturer part number 1": ("Manufacturer Part Number 1", "GRM155")},
        "R1": {"comment": ("Comment", "4k7")},
    }
    state = VariantState(variants.variants)
    assert state.get("lite") is lite and len(state.variants) == 2


def _component(uid, **params):
    return {
        "unique_id": uid, "unique_ids": [uid], "kind": 0, "sheet": "Main",
        "description": params.pop("description", None), "params": params,
    }


def _netlist():
    """R1, R2 (10k 0603), C1 (100n), TP1 (Standard (No BOM)); R9 renamed to R3 since."""
    return {
        "nets": {}, "pin_to_net": {},
        "parts": {
            "R1": {"footprint": "R0603", "comment": "10k"},
            "R2": {"footprint": "R0603", "comment": "10k"},
            "R3": {"footprint": "R0603", "comment": "1k"},
            "C1": {"footprint": "C0603", "comment": "100n"},
            "TP1": {"footprint": "TP", "comment": "TP"},
        },
        "components": {
            "R1": _component("RUID1", comment="=Value", value="10k",
                             **{"manufacturer 1": "Yageo",
                                "manufacturer part number 1": "RC0603FR-0710KL"}),
            "R2": _component("RUID2", comment="=Value", value="10k",
                             **{"manufacturer 1": "Yageo",
                                "manufacturer part number 1": "RC0603FR-0710KL"}),
            "R3": _component("RUID3", comment="1k", value="1k",
                             **{"manufacturer 1": "Yageo",
                                "manufacturer part number 1": "RC0603FR-071KL"}),
            "C1": {**_component("CUID1", comment="100n", value="100n",
                                **{"manufacturer part number 1": "CL10B104"}),
                   "unique_paths": ["\\SHEET\\CUID1"]},
            "TP1": {**_component("TUID"), "kind": 5},
            "MASKED": _component("MUID", comment="ghost"),  # not compiled
        },
    }


def test_bom_of_the_base_design():
    parts, excluded = build_parts(_netlist())
    assert [p.designator for p in parts] == ["C1", "R1", "R2", "R3"]  # MASKED not compiled
    assert excluded == {"standard (no BOM)": ["TP1"]}
    r1 = parts[1]
    assert (r1.comment, r1.mpn, r1.manufacturer) == ("10k", "RC0603FR-0710KL", "Yageo")
    lines = group_lines(parts)
    assert [(line["quantity"], line["designators"]) for line in lines] == [
        (1, ["C1"]), (2, ["R1", "R2"]), (1, ["R3"]),
    ]
    assert lines[0]["comment"] == "100n" and "manufacturer" not in lines[0]


def test_a_variant_binds_by_unique_id(variants):
    netlist = _netlist()
    lite = variants.variants[1]
    plan = plan_variant(lite, netlist, list(netlist["parts"]))
    # R9's UniqueId now belongs to R3: Altium follows the UniqueId.
    assert plan.not_fitted == {"R2", "R3"}
    assert {(e["designator"], e["issue"]) for e in plan.stale} == {("R9", "designator_changed")}
    parts, _ = build_parts(netlist, plan)
    fitted = {p.designator: p for p in parts if p.fitted}
    assert set(fitted) == {"C1", "R1"}
    assert fitted["C1"].mpn == "GRM155" and fitted["C1"].overrides
    assert fitted["R1"].comment == "4k7"  # the varied Comment replaces '=Value'
    changes = compare_variants(netlist, [lite], list(netlist["parts"]))
    (diff,) = changes
    assert diff["not_fitted"] == ["R2", "R3"]
    assert {c["designator"] for c in diff["changed"]} == {"C1", "R1"}


def test_stale_variant_entries():
    variant = VariantDefinition(
        name="V",
        variations=[
            Variation(designator="R7", unique_id="\\GONE", kind=1),
            Variation(designator="R1", unique_id="\\NOPE", kind=1),
        ],
        param_variations={"R2": {"comment": ("Comment", "x")}, "Q9": {"comment": ("Comment", "y")}},
    )
    variant.variations.append(Variation(designator="R2", unique_id="\\RUID2", kind=1))
    plan = plan_variant(variant, _netlist(), list(_netlist()["parts"]))
    issues = {(e["designator"], e["issue"]) for e in plan.stale}
    assert issues == {
        ("R7", "no_such_part"), ("R1", "unique_id_not_found"), ("Q9", "no_such_part"),
        ("R2", "varied_but_not_fitted"),
    }
    assert plan.not_fitted == {"R1", "R2"}


def test_csv_quotes_and_sorts():
    netlist = _netlist()
    netlist["components"]["C1"]["description"] = 'CAP 100nF, 16V "X7R" µ'
    lines = group_lines(build_parts(netlist)[0])
    text = to_csv(lines)
    header, first = text.splitlines()[:2]
    assert header.startswith("Comment,Description,Designator,Footprint,Quantity")
    assert '"CAP 100nF, 16V ""X7R"" µ"' in first
    assert '"R1, R2"' in text


# ---------- checks ----------

def test_value_normalisation():
    assert normalize_value("100n") == normalize_value("0.1uF") == normalize_value("100nF")
    assert normalize_value("4k7") == normalize_value("4.7k") == 4700.0
    assert normalize_value("4R7") == 4.7 and normalize_value("1M") != normalize_value("1m")
    assert normalize_value("abc") == "abc"
    # Upper-case units, comma decimals, thousands, a leading dot, 3V3, MEG.
    assert normalize_value("100NF") == normalize_value("100nF")
    assert normalize_value("10UF") != normalize_value("10PF")
    assert normalize_value("4,7k") == 4700.0 and normalize_value("4,99k") == 4990.0
    assert normalize_value("1,000pF") == normalize_value("1nF")
    assert normalize_value(".1uF") == normalize_value("0.1uF")
    assert normalize_value("3V3") == normalize_value("3.3V") == 3.3
    assert normalize_value("1MEG") == normalize_value("1M")
    # Part numbers and E notation stay text instead of reading as their first digits.
    assert normalize_value("0402WGF1000TCE") != normalize_value("0402WGF4701TCE")
    assert normalize_value("4E7") != normalize_value("4E3")
    assert isinstance(normalize_value("1N4148"), str)


def _checks(netlist, **kwargs):
    parts, _ = build_parts(netlist, kwargs.pop("plan", None))
    return run_bom_checks(parts, netlist, kwargs.pop("plan_for_checks", None), limit=1000,
                          **kwargs)


def test_a_clean_bom_has_no_findings():
    netlist = _netlist()
    netlist["components"]["C1"]["params"]["manufacturer 1"] = "Samsung"
    for name, text in (("R1", "RES 10K 0603"), ("R2", "RES 10K 0603"), ("R3", "RES 1K 0603"),
                       ("C1", "CAP 100N 0603")):
        netlist["components"][name]["description"] = text
    result = _checks(netlist)
    assert result["findings"] == [{
        "check": "not_compiled", "severity": "info", "designators": ["MASKED"], "count": 1,
        "issue": "1 sheet parts are not in the compiled design (compile masks, or sheets "
        "outside the project) and are not in the BOM.",
    }]


def test_mpn_and_description_checks():
    netlist = _netlist()
    components = netlist["components"]
    components["R2"]["params"]["value"] = "4k7"  # same MPN as R1, another value
    components["R3"]["params"]["value"] = "DNP"
    components["C1"]["description"] = "?"
    result = _checks(netlist)
    by_check = {}
    for finding in result["findings"]:
        by_check.setdefault(finding["check"], []).append(finding)
    assert by_check["mpn_inconsistent"][0]["designators"] == ["R1", "R2"]
    assert by_check["dnp_marker_on_fitted"][0]["designators"] == ["R3"]
    assert by_check["mpn_without_manufacturer"][0]["mpn"] == "CL10B104"
    assert by_check["placeholder_description"][0]["severity"] == "warning"


def test_missing_mpn_depends_on_coverage():
    netlist = _netlist()
    for name in ("R1", "R2", "R3"):
        for key in list(netlist["components"][name]["params"]):
            if "manufacturer" in key:
                del netlist["components"][name]["params"][key]
    (low,) = [f for f in _checks(netlist)["findings"] if f["check"] == "missing_mpn"]
    assert low["severity"] == "info" and low["coverage_percent"] == 25
    netlist = _netlist()
    del netlist["components"]["R3"]["params"]["manufacturer part number 1"]
    (high,) = [f for f in _checks(netlist)["findings"] if f["check"] == "missing_mpn"]
    assert (high["severity"], high["designators"]) == ("warning", ["R3"])


def test_consolidation_candidates():
    netlist = _netlist()
    netlist["components"]["R3"]["params"].update(
        {"value": "10k", "manufacturer part number 1": "ERJ-3EKF1002V"},
    )
    for name in ("R1", "R2", "R3"):
        netlist["components"][name]["description"] = "RES 10K 1% 0603"
    (finding,) = [f for f in _checks(netlist)["findings"] if f["check"] == "consolidation"]
    assert finding["mpns"] == ["ERJ-3EKF1002V", "RC0603FR-0710KL"]
    assert "the same ratings" in finding["issue"]
    netlist["components"]["R3"]["description"] = "RES 10K 0.1% 0603"  # another rating
    assert not [f for f in _checks(netlist)["findings"] if f["check"] == "consolidation"]
    # One series (packaging suffixes) is not a candidate.
    netlist["components"]["R3"]["description"] = "RES 10K 1% 0603"
    netlist["components"]["R3"]["params"]["manufacturer part number 1"] = "RC0603FR-0710KP"
    assert not [f for f in _checks(netlist)["findings"] if f["check"] == "consolidation"]


def _consolidation(parts):
    return [f for f in run_bom_checks(parts, {})["findings"] if f["check"] == "consolidation"]


def test_consolidation_reads_metric_sizes_and_current_ratings():
    caps = [BomPart(designator="C7", value="1nF", footprint="CAPC0603X33N", mpn="AAA1"),
            BomPart(designator="C8", value="1nF", footprint="C0603", mpn="BBB2")]
    assert not _consolidation(caps)  # IPC CAPC0603 is a metric 0603: an imperial 0201
    caps[0].footprint = "CAPC1608X90N"
    (finding,) = _consolidation(caps)
    assert "0603" in finding["issue"] and "no rating" in finding["issue"]
    coils = [BomPart(designator="L1", value="10uH", footprint="IND_1210", mpn="LLL1",
                     description="Inductor 10uH 20% 1A"),
             BomPart(designator="L2", value="10uH", footprint="IND_1210", mpn="MMM2",
                     description="Inductor 10uH 20% 3.5A")]
    assert not _consolidation(coils)  # current ratings differ
    for coil in coils:
        coil.description = "Inductor"
    assert not _consolidation(coils)  # no rating to compare: not suggested


def test_generic_descriptions_without_mpn():
    parts = [BomPart(designator="R1", value="10k", footprint="R0603", description="Resistor")]
    (finding,) = [f for f in run_bom_checks(parts, {})["findings"]
                  if f["check"] == "placeholder_description"]
    assert finding["severity"] == "info" and finding["designators"] == ["R1"]


def test_unresolved_strings_footprints_and_clashes():
    netlist = _netlist()
    netlist["components"]["R1"]["params"]["comment"] = "=Rating"
    netlist["parts"]["C1"]["footprint"] = ""
    netlist["designator_clashes"] = [
        {"sheet": "Main", "refdes": "R2", "lib_references": ["RES", "CAP"]},
    ]
    checks = {f["check"] for f in _checks(netlist)["findings"]}
    assert {"unresolved_special_string", "no_footprint", "duplicate_designator"} <= checks
    result = _checks(netlist, checks=["no_footprint"])
    assert [f["check"] for f in result["findings"]] == ["no_footprint"]
    assert _checks(netlist, checks=["nope"])["error"] == "invalid_checks"


def test_fixture_sheets():
    netlist = {"nets": {}, "pin_to_net": {}}
    AltiumClient()._enrich_from_schdocs(
        [str(FIXTURES / "sheet1.SchDoc"), str(FIXTURES / "sheet2.SchDoc")], netlist, [],
    )
    parts, _ = build_parts(netlist)
    r11 = next(p for p in parts if p.designator == "R11")
    assert (r11.mpn, r11.manufacturer, r11.footprint) == (
        "ERJ-3EKF1000V", "Panasonic Electronic Components", "R0603_0.55MM",
    )
    # D1 keeps its part number only in Comment: no MPN, but its Comment in the BOM.
    d1 = next(p for p in parts if p.designator == "D1")
    assert (d1.mpn, d1.comment) == (None, "DFLS130L-7")
    missing = [f for f in run_bom_checks(parts, netlist)["findings"]
               if f["check"] == "missing_mpn"]
    assert sorted(d for f in missing for d in f["designators"]) == ["D1", "D16", "D20", "Q1"]


# ---------- tools ----------

@pytest.fixture
def bom_project(monkeypatch, tmp_path, variants):
    netlist = _netlist()
    state = VariantState(variants.variants)
    monkeypatch.setattr(main_mod, "_project", {
        "name": "Demo", "root_dir": str(tmp_path), "prj_pcb_path": str(tmp_path / "Demo.PrjPcb"),
        "sheets": [], "pcb_doc_paths": [],
    })
    monkeypatch.setattr(main_mod._altium, "_netlist", netlist)
    monkeypatch.setattr(main_mod, "_variant_state", state)
    return netlist


def test_get_bom_tool(bom_project):
    result = json.loads(main_mod.get_bom())  # the active variant: Full
    assert result["variant"] == "Full" and result["parts"]["in_bom"] == 4
    assert result["parts"]["excluded_by_kind"] == {"standard (no BOM)": 1}
    lite = json.loads(main_mod.get_bom(variant="Lite", include_not_fitted=True))
    assert lite["parts"]["not_fitted"] == 2
    assert [line["designators"] for line in lite["not_fitted"]] == [["R2"], ["R3"]]
    assert lite["stale_variant_entries"] == 1
    base = json.loads(main_mod.get_bom(variant=BASE_VARIANT, format="csv"))
    assert base["variant"] == BASE_VARIANT and base["csv"].startswith("Comment,")
    compared = json.loads(main_mod.get_bom(compare_variants=True))
    assert [v["variant"] for v in compared["variants"]] == ["Lite"]
    assert json.loads(main_mod.get_bom(variant="Nope"))["error"] == "variant_not_found"


def test_bom_tools_page_and_trim(bom_project, monkeypatch):
    for index in range(10, 160):
        name = f"R{index}"
        bom_project["parts"][name] = {"footprint": "R0603", "comment": f"{index}k"}
        bom_project["components"][name] = _component(
            f"U{index}", comment=f"{index}k", description="RES " * 20,
        )
    result = json.loads(main_mod.get_bom())
    assert (len(result["lines"]), result["next_offset"]) == (100, 100)
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 12000)
    trimmed = json.loads(main_mod.get_bom())
    assert trimmed["lines_limited_to"] in (40, 15)
    checked = json.loads(main_mod.check_bom())
    assert checked["summary"]["missing_mpn"] >= 1
    assert json.loads(main_mod.check_bom(checks=["x"]))["error"] == "invalid_checks"


def test_bom_tools_need_a_project(monkeypatch):
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.get_bom())["error"] == "no_project"
    assert json.loads(main_mod.check_bom())["error"] == "no_project"


def test_tool_descriptions_mention_paging():
    assert "next_offset" in main_mod.get_bom.__doc__
    assert "next_offset" in main_mod.check_bom.__doc__


def test_dnp_flags_bind_like_the_bom(bom_project):
    state = main_mod._variant_state
    main_mod._resolve_dnp_uids(state, bom_project)
    state.set_variant("Lite")
    # R9's UniqueId is on R3 now: every tool's DNP flag follows it, as get_bom does.
    assert state.is_dnp("R2") and state.is_dnp("R3") and not state.is_dnp("R9")
    assert json.loads(main_mod.list_variants())["variants"][1]["dnp_components"] == ["R2", "R3"]
    assert json.loads(main_mod.get_bom())["not_fitted_designators"] == ["R2", "R3"]


def test_designator_only_variants_and_variant_names(bom_project):
    state = main_mod._variant_state
    state._variants.append(VariantDefinition(name="Base", dnp_refdes=["R1", "TP1"]))
    # A project's own 'Base' wins over the base-design alias; the alias still works.
    based = json.loads(main_mod.get_bom(variant="Base"))
    assert based["variant"] == "Base" and based["not_fitted_designators"] == ["R1"]
    assert json.loads(main_mod.get_bom(variant=" none "))["variant"] == BASE_VARIANT
    assert json.loads(main_mod.get_bom(variant=" Lite"))["variant"] == "Lite"
    compared = json.loads(main_mod.get_bom(compare_variants=True))["variants"]
    # TP1 (Standard (No BOM)) was never in the BOM, so it is no BOM difference.
    assert {v["variant"]: v["not_fitted"] for v in compared}["Base"] == ["R1"]


def test_parameter_variations_follow_their_variation():
    netlist = _netlist()
    variant = VariantDefinition(
        name="V", variations=[Variation(designator="R1", unique_id="\\RUID2", kind=0)],
        param_variations={"R1": {"comment": ("Comment", "VARIED")}},
    )
    plan = plan_variant(variant, netlist, variant_names(netlist))
    assert set(plan.params) == {"R2"}  # the line's UniqueId is R2's
    parts = {p.designator: p for p in build_parts(netlist, plan)[0]}
    assert (parts["R1"].comment, parts["R2"].comment) == ("10k", "VARIED")
    variant.variations[0].kind = 1
    plan = plan_variant(variant, netlist, variant_names(netlist))
    assert ("R2", "varied_but_not_fitted") in {(e["designator"], e["issue"]) for e in plan.stale}


def test_an_ambiguous_unique_id_is_reported():
    netlist = {
        "nets": {}, "pin_to_net": {}, "parts": {"R1_A": {}, "R1_B": {}},
        "components": {"R1_A": _component("CH"), "R1_B": _component("CH")},
    }
    for designator in ("R?", "R1_X"):
        variant = VariantDefinition(name="V", variations=[
            Variation(designator=designator, unique_id="\\SYM\\CH", kind=1),
        ])
        plan = plan_variant(variant, netlist, variant_names(netlist))
        assert plan.not_fitted == {"R1_A", "R1_B"}
        assert [e["issue"] for e in plan.stale] == ["ambiguous_unique_id"]
    variant.variations[0].designator = "R1_B"
    plan = plan_variant(variant, netlist, variant_names(netlist))
    assert (plan.not_fitted, plan.stale) == ({"R1_B"}, [])


def test_alternate_parts_take_the_variant_footprint(variants):
    netlist = _netlist()
    lite = variants.variants[1]
    plan = plan_variant(lite, netlist, variant_names(netlist))
    c1 = next(p for p in build_parts(netlist, plan)[0] if p.designator == "C1")
    assert (c1.footprint, c1.alternate_part) == ("C0402", "CAP")
    assert group_lines([c1])[0]["alternate_parts"] == {"C1": "CAP"}
    (diff,) = compare_variants(netlist, [lite], variant_names(netlist))
    changed = next(c for c in diff["changed"] if c["designator"] == "C1")
    assert changed["footprint"] == {"from": "C0603", "to": "C0402"}
    assert changed["alternate_part"] == "CAP"


def test_refresh_reads_variants_saved_since_load(bom_project, monkeypatch, tmp_path):
    (tmp_path / "Demo.PrjPcb").write_text(
        PRJ.replace(
            "Description=Full\nVariationCount=0",
            "Description=Full\nVariationCount=1\nVariation1=Designator=R1|UniqueId=\\RUID1|Kind=1",
        ) + "\n[ProjectVariant3]\nDescription=New\n",
        encoding="utf-8",
    )
    altium = MagicMock(_netlist=bom_project)
    altium.generate_netlist.return_value = True
    monkeypatch.setattr(main_mod, "_altium", altium)
    monkeypatch.setattr(main_mod, "_pcb_session", MagicMock())
    monkeypatch.setattr(main_mod, "upsert_registry_entry", MagicMock())
    main_mod._variant_state.set_variant("Lite")
    main_mod.refresh_netlist()
    state = main_mod._variant_state
    assert state.active.name == "Lite" and state.get("New") is not None
    assert json.loads(main_mod.get_bom(variant="Full"))["not_fitted_designators"] == ["R1"]


# ---------- compile masks ----------

def _masked_sheet(*components) -> bytes:
    """A sheet with one compile mask over x < 100; components are (designator, uid,
    value, x) single-part resistors."""
    records = ["|RECORD=211|Location.X=0|Location.Y=0|Corner.X=100|Corner.Y=100"]
    for designator, uid, value, x in components:
        owner = len(records)  # the RECORD=1's position (header first) minus one
        records += [
            f"|RECORD=1|LibReference=RES|UniqueID={uid}|CurrentPartId=1|PartCount=2"
            f"|Location.X={x}|Location.Y=50",
            f"|RECORD=34|OwnerIndex={owner}|Name=Designator|Text={designator}",
            f"|RECORD=41|OwnerIndex={owner}|Name=Value|Text={value}",
        ]
    return _stream(*records)


@pytest.mark.parametrize("order", [1, -1])
def test_a_compile_masked_copy_never_replaces_the_live_part(order):
    clashes = []
    copies = [("R1", "LIVEAAAA", "10k", 500), ("R1", "MASKAAAA", "1k", 50)][::order]
    r1 = components_from_stream(_masked_sheet(*copies), clashes=clashes)["R1"]
    assert (r1.unique_id, r1.value, r1.compile_masked, clashes) == ("LIVEAAAA", "10k", False, [])
    alone = components_from_stream(_masked_sheet(("R2", "MASKBBBB", "1k", 50)))["R2"]
    assert alone.compile_masked
    components_from_stream(
        _masked_sheet(("R4", "MASKCCCC", "1k", 50), ("R4", "MASKDDDD", "2k", 60)),
        clashes=clashes,
    )
    assert clashes == []  # neither masked copy is compiled
    clashes = []
    components_from_stream(
        _masked_sheet(("R3", "LIVECCCC", "1k", 500), ("R3", "LIVEDDDD", "2k", 600)),
        clashes=clashes,
    )
    assert [clash.refdes for clash in clashes] == ["R3"]  # two live parts still clash


@pytest.mark.parametrize("order", [1, -1])
def test_masked_copies_on_other_sheets_keep_the_live_fields(order):
    live = ComponentMeta(refdes="R1", unique_id="LIVE", unique_ids=["LIVE"],
                         params={"value": "10k"}, footprint="R0603")
    masked = ComponentMeta(refdes="R1", unique_id="MASK", unique_ids=["MASK"], kind=5,
                           params={"value": "1k"}, footprint="R0402", compile_masked=True)
    netlist = {"nets": {}, "pin_to_net": {}, "parts": {"R1": {"footprint": "R0603"}}}
    enrich_netlist_sheets(netlist, [("A", {"R1": live}), ("B", {"R1": masked})][::order])
    r1 = netlist["components"]["R1"]
    assert (r1["params"], r1["kind"], r1["footprint"], r1["sheet"]) == (
        {"value": "10k"}, 0, "R0603", "A",
    )
    assert sorted(r1["unique_ids"]) == ["LIVE", "MASK"]


def test_sheet_only_boms_leave_compile_masked_parts_out():
    netlist = _netlist()
    del netlist["parts"]
    netlist["components"]["MASKED"]["compile_masked"] = True
    parts, _ = build_parts(netlist)
    assert "MASKED" not in {p.designator for p in parts}
    (finding,) = [f for f in run_bom_checks(parts, netlist)["findings"]
                  if f["check"] == "not_compiled"]
    assert finding["designators"] == ["MASKED"]


def test_duplicate_designators_outside_the_compiled_design_are_skipped():
    netlist = _netlist()
    netlist["designator_clashes"] = [
        {"sheet": "Main", "refdes": "R2", "lib_references": ["RES", "CAP"]},
        {"sheet": "Old", "refdes": "Q7", "lib_references": ["A", "B"]},
    ]
    found = [f["designators"] for f in _checks(netlist)["findings"]
             if f["check"] == "duplicate_designator"]
    assert found == [["R2"]]


# ---------- part numbers, lines and CSV ----------

def test_part_numbers_resolve_project_parameters_and_csv_cells_stay_text():
    netlist = _netlist()
    params = netlist["components"]["R3"]["params"]
    del params["manufacturer part number 1"]
    params.update({"part number": "=ProjPN", "manufacturer 1": "=Maker", "comment": "=Missing"})
    netlist["components"]["R3"]["description"] = "+5V LDO"
    r3 = next(p for p in build_parts(netlist, project_params={"ProjPN": "PN-7"})[0]
              if p.designator == "R3")
    assert (r3.mpn, r3.manufacturer, r3.unresolved) == ("PN-7", "=Maker", ["=Missing", "=Maker"])
    text = to_csv(group_lines([r3]))
    assert "'=Missing" in text and "'+5V LDO" in text and "'=Maker" in text


def test_placeholder_part_numbers():
    netlist = _netlist()
    for name, text in (("R1", "N/A"), ("R2", "-"), ("R3", "TBD")):
        netlist["components"][name]["params"]["manufacturer part number 1"] = text
    parts = {p.designator: p for p in build_parts(netlist)[0]}
    assert (parts["R1"].mpn, parts["R1"].mpn_placeholder) == (None, "N/A")
    findings = _checks(netlist)["findings"]
    # 'N/A' and '-' say there is none on purpose; 'TBD' still needs one.
    missing = [d for f in findings if f["check"] == "missing_mpn" for d in f["designators"]]
    assert missing == ["R3"]
    assert [f["mpn"] for f in findings if f["check"] == "mpn_without_manufacturer"] == ["CL10B104"]
    assert not [f for f in findings if f["check"] == "mpn_inconsistent"]
    line = next(line for line in group_lines(list(parts.values())) if "R1" in line["designators"])
    assert line["mpn_placeholder"] == "N/A" and "N/A" in to_csv([line])


def test_lines_map_logical_designators_and_list_what_varies():
    parts = [BomPart(designator=d, comment="49R9", footprint="R0402", logical=logical,
                     mpn=mpn, description=text, mpn_source="part number")
             for d, logical, mpn, text in (("C3", None, "AAA", "49.9 Ohm"),
                                           ("C27", "C3", "BBB", "39 Ohm"),
                                           ("C28", "C4", "AAA", "49.9 Ohm"))]
    (line,) = group_lines(parts, "comment")
    assert line["logical_designators"] == {"C27": "C3", "C28": "C4"}
    assert line["varies"] == {"description": ["49.9 Ohm", "39 Ohm"], "mpn": ["AAA", "BBB"]}
    assert line["mpn_source"] == "part number"
    assert "AAA / BBB" in to_csv([line])


def test_comment_fallback_and_description_variation():
    netlist = _netlist()
    params = netlist["components"]["R3"]["params"]
    del params["comment"]
    variant = VariantDefinition(
        name="V", param_variations={"R3": {"description": ("Description", "RES 1K 0.1%")}},
    )
    plan = plan_variant(variant, netlist, variant_names(netlist))
    r3 = next(p for p in build_parts(netlist, plan)[0] if p.designator == "R3")
    assert (r3.comment, r3.description) == ("1k", "RES 1K 0.1%")  # Comment from the .NET
    assert group_lines([r3])[0]["varied_parameters"] == ["Description"]


def test_check_bom_tool_paths(bom_project, monkeypatch):
    assert json.loads(main_mod.check_bom(variant="Nope"))["error"] == "variant_not_found"
    lite = json.loads(main_mod.check_bom(variant="Lite"))
    assert lite["summary"]["stale_variant_entry"] == 1
    for index in range(10, 160):
        bom_project["parts"][f"R{index}"] = {"footprint": "R0603"}
        bom_project["components"][f"R{index}"] = _component(f"U{index}", comment=f"=Nope{index}")
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 9000)
    checked = json.loads(main_mod.check_bom())
    assert checked["findings_limited_to"] in (40, 15) and "next_offset" in checked
    monkeypatch.setitem(bom_project, "components", {})
    assert json.loads(main_mod.get_bom())["error"] == "no_components_loaded"
    assert json.loads(main_mod.check_bom())["error"] == "no_components_loaded"
