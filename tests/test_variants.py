"""Full variant support: varied parameters, alternate parts and the .PrjPcbVariants
library they are copied to (parsers/prj_pcb.py), the active variant's values in
get_component and get_sheet_context, the BOM of an alternate part, compare_variants with
its per-variant documentation check (services/variants.py), list_variants and the
variant_variations table of package_for_xfn. Synthetic INI, netlists and OutJobs."""
import json
import re
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

import server.main as main_mod
from parsers.pcb_dwf import DraftsmanDoc, DraftsmanItem
from parsers.prj_pcb import (
    VariantDefinition, VariantState, Variation, parse_prj_pcb, parse_variant_library,
)
from services.bom import BASE_VARIANT, build_parts, plan_variant, variant_names
from services.page_netlist import build_sheet_context
from services.variants import compare, export_rows, part_changes, unvalidated, view_of
from tests.sch_fixture import record
from tests.test_output_jobs import _outjob, _output

ALT = ("AltLibLink_LibIdentifierKind=Library Name|AltLibLink_LibraryIdentifier=Alt.SchLib"
       "|AltLibLink_SymbolReference=CAPX|AltLibLink_DesignItemID=CAPX"
       "|AltLibLink_UseLibraryName=True|AltLibLink_SourceLibraryName=Alt.SchLib"
       "|AltLibLink_UseDBTableName=False|AltLibLink_DatabaseTableName="
       "|AltLibLink_VaultGUID=|AltLibLink_ItemGUID=|AltLibLink_RevisionGUID="
       "|AltLibLink_Footprint=C0805|AltLibLink_IsSameFootprint=False")

# Full: R1 fitted with Value 4k7, R3 Not Fitted (a part under a sheet symbol).
# Lite: R2 Not Fitted, C1 an alternate part with its Comment varied, R10 varied by
# UniqueId while its stored designator is still 'R?'; a variant parameter.
PRJ = f"""[Design]
CurrentVariant=Lite

[ProjectVariant1]
Description=Full
UniqueId=VARONE
AllowFabrication=0
ParameterCount=0
VariationCount=2
Variation1=Designator=R1|UniqueId=\\RUID1|Kind=0|AlternatePart=
Variation2=Designator=R3|UniqueId=\\SHEET1\\RUID3|Kind=1|AlternatePart=
ParamVariationCount=1
ParamDesignator1=R1
ParamVariation1=ParameterName=Value|VariantValue=4k7

[ProjectVariant2]
Description=Lite
UniqueId=VARTWO
AllowFabrication=1
ParameterCount=1
VariationCount=3
Variation1=Designator=R2|UniqueId=\\RUID2|Kind=1|AlternatePart=
Variation2=Designator=C1|UniqueId=\\CUID1|Kind=2|AlternatePart=CAPX|{ALT}
Variation3=Designator=R10|UniqueId=\\RUID10|Kind=0|AlternatePart=
ParamVariationCount=3
ParamDesignator1=C1
ParamVariation1=ParameterName=Comment|VariantValue=10u
ParamDesignator2=R10
ParamVariation2=ParameterName=Tolerance|VariantValue=1%
ParamDesignator3=R10
ParamVariation3=ParameterName=Comment|VariantValue=22k

[Parameter3_1]
Name=Build
Value=Lite build
"""


class _FakeOle:
    """olefile.OleFileIO over {stream path: bytes}."""

    def __init__(self, streams: dict[str, bytes]):
        self.streams = streams

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def listdir(self):
        return [name.split("/") for name in self.streams]

    def openstream(self, name):
        import io
        return io.BytesIO(self.streams[name])


def _library_part(libref="CAPX", description="Cap 10u", comment="10u X5R",
                  footprint="C0805", mpn="CAP-10U") -> bytes:
    bodies = [
        f"|RECORD=1|LIBREFERENCE={libref}|DESIGNITEMID={libref}"
        f"|COMPONENTDESCRIPTION={description}|SOURCELIBRARYNAME=Alt.SchLib"
        "|LIBRARYPATH=Lib\\Alt.SchLib|PARTCOUNT=2|CURRENTPARTID=1",
        "|RECORD=34|NAME=Designator|TEXT=C?",
        f"|RECORD=41|NAME=Comment|TEXT={comment}",
        f"|RECORD=41|NAME=Manufacturer Part Number 1|TEXT={mpn}",
        "|RECORD=41|NAME=Value|TEXT=10u",
        "|RECORD=44",
        "|RECORD=45|MODELNAME=C0603|MODELTYPE=PCBLIB|ISCURRENT=F",
        f"|RECORD=45|MODELNAME={footprint}|MODELTYPE=PCBLIB|ISCURRENT=T",
    ]
    return b"".join(record(body) for body in bodies)


def _library_streams(**part) -> dict[str, bytes]:
    return {
        "FileHeader": record("|HEADER=Protel for Windows - Schematic Library Editor Binary "
                             "File Version 5.0|COMPCOUNT=2|LIBREF0=Component_1|LIBREF1=CAPX"),
        "Storage": b"",
        "Component_1/Data": record("|RECORD=1|LIBREFERENCE=Component_1"),
        "CAPX/Data": _library_part(**part),
    }


def _write(tmp_path: Path, text: str = PRJ, library: bool = True) -> Path:
    path = tmp_path / "Demo.PrjPcb"
    path.write_text(text, encoding="utf-8")
    if library:
        (tmp_path / "Demo.PrjPcbVariants").write_bytes(b"ole")
    return path


def _parse(tmp_path: Path, text: str = PRJ, library: bool = True, streams=None):
    path = _write(tmp_path, text, library)
    with patch("olefile.OleFileIO", lambda p: _FakeOle(streams or _library_streams())):
        return parse_prj_pcb(str(path))


def _component(uid, value=None, comment=None, lib="RES", footprint="R0603", **extra):
    params = {key: text for key, text in (("value", value), ("comment", comment)) if text}
    params.update(extra.pop("params", {}))
    return {
        "unique_id": uid, "unique_ids": [uid], "kind": 0, "sheet": "Main",
        "description": extra.pop("description", "Resistor"), "params": params,
        "value": value, "mpn": params.get("manufacturer part number 1") or comment,
        "lib_reference": lib, "footprint": footprint, "pins": {}, **extra,
    }


def _netlist() -> dict:
    """R1, R2, R3 (under a sheet symbol), R10 and C1, all compiled."""
    components = {
        "R1": _component("RUID1", "10k", "=Value"),
        "R2": _component("RUID2", "10k", "=Value"),
        "R3": {**_component("RUID3", "1k", "1k"), "unique_paths": ["\\SHEET1\\RUID3"]},
        "R10": _component("RUID10", "100k", "100k", params={"tolerance": "5%"}),
        "C1": _component("CUID1", "100n", "100n", lib="CAP", footprint="C0402",
                         description="Capacitor",
                         params={"manufacturer part number 1": "CAP-100N"}),
    }
    return {
        "nets": {}, "pin_to_net": {},
        "parts": {name: {"footprint": meta["footprint"], "comment": meta["params"].get("comment")}
                  for name, meta in components.items()},
        "components": components,
    }


@pytest.fixture
def data(tmp_path):
    return _parse(tmp_path)


@pytest.fixture
def state(data):
    """The variants bound to _netlist() as a load binds them."""
    variant_state = VariantState(data.variants)
    main_mod._resolve_dnp_uids(variant_state, _netlist())
    return variant_state


# ---------- parser ----------

def test_each_variation_kind_is_parsed(data):
    full, lite = data.variants
    assert [(v.designator, v.kind, v.kind_name) for v in full.variations] == [
        ("R1", 0, "fitted"), ("R3", 1, "not fitted")]
    assert full.dnp_refdes == ["R3"] and lite.dnp_refdes == ["R2"]
    assert full.varied_parameters() == [
        {"designator": "R1", "unique_id": "\\RUID1", "parameter": "Value", "value": "4k7"}]
    assert (full.unique_id, full.allow_fabrication, full.parameters) == ("VARONE", False, {})
    assert (lite.unique_id, lite.allow_fabrication) == ("VARTWO", True)
    assert lite.parameters == {"Build": "Lite build"}
    assert [v.designator for v in lite.not_fitted()] == ["R2"]
    assert lite.param_variations["R10"] == {"tolerance": ("Tolerance", "1%"),
                                            "comment": ("Comment", "22k")}


def test_alternate_part_identity_and_library_copy(data):
    (alternate,) = data.variants[1].alternate_parts()
    assert alternate == {
        "designator": "C1", "unique_id": "\\CUID1", "lib_reference": "CAPX",
        "design_item_id": "CAPX", "library": "Alt.SchLib", "footprint": "C0805",
        "same_footprint": False,
        "link": {"LibIdentifierKind": "Library Name", "LibraryIdentifier": "Alt.SchLib",
                 "UseLibraryName": "True"},
        "library_copy": True, "description": "Cap 10u", "comment": "10u X5R",
        "value": "10u",
    }
    assert data.variant_library_path.endswith("Demo.PrjPcbVariants")
    assert data.variant_library_error == ""
    part = data.variants[1].variations[1].library_part
    assert (part.footprint, part.footprints, part.storage) == ("C0805", ["C0603", "C0805"], "CAPX")
    assert part.params["manufacturer part number 1"] == ("Manufacturer Part Number 1", "CAP-10U")


def test_same_footprint_alternate_keeps_the_base_footprint(tmp_path):
    text = PRJ.replace("AltLibLink_IsSameFootprint=False", "AltLibLink_IsSameFootprint=True")
    (alternate,) = _parse(tmp_path, text).variants[1].alternate_parts()
    assert alternate["same_footprint"] is True and "footprint" not in alternate


def test_variant_library_absent_unreadable_or_not_needed(tmp_path):
    folder = tmp_path / "a"
    folder.mkdir()
    bare = _parse(folder, library=False)
    assert bare.variant_library_path == "" and bare.variants[1].variations[1].library_part is None
    folder = tmp_path / "b"
    folder.mkdir()
    path = _write(folder)

    def broken(_):
        raise OSError("not an OLE file")

    with patch("olefile.OleFileIO", broken):
        data = parse_prj_pcb(str(path))
    assert data.variant_library_error == "OSError: not an OLE file"
    (alternate,) = data.variants[1].alternate_parts()
    assert alternate["library_copy"] is False
    assert alternate["library_error"] == "OSError: not an OLE file"
    folder = tmp_path / "c"
    folder.mkdir()
    no_alternates = _write(folder, PRJ.replace("|Kind=2|", "|Kind=0|"))
    with patch("olefile.OleFileIO", broken):  # never opened
        assert parse_prj_pcb(str(no_alternates)).variant_library_path == ""


def test_keys_written_with_spaces_around_the_equals_sign(tmp_path):
    """Some saved projects write every key line 'Key = Value'; the variation lines read
    the same as 'Key=Value' (the other keys always did, through _get_field)."""
    spaced = re.sub(r"^((?:Param)?(?:Variation|Designator)\d+|Description|UniqueId)=",
                    r"\1 = ", PRJ, flags=re.MULTILINE)
    assert "Variation1 = Designator=R1" in spaced and "ParamDesignator1 = R1" in spaced
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    compact, loose = _parse(tmp_path / "a"), _parse(tmp_path / "b", spaced)
    for left, right in zip(compact.variants, loose.variants, strict=True):
        assert (right.name, right.dnp_refdes, right.variations, right.param_variations) == (
            left.name, left.dnp_refdes, left.variations, left.param_variations)
    assert loose.variants[1].dnp_refdes == ["R2"]
    assert loose.variants[1].param_variations["R10"]["comment"] == ("Comment", "22k")


def test_alternate_reference_prefers_the_design_item_over_the_symbol(tmp_path):
    """AlternatePart, then AltLibLink_DesignItemID, then SymbolReference, everywhere: the
    identity, the library copy matched and the BOM (corpus: AlternatePart and
    DesignItemID are the part's library reference, SymbolReference neither)."""
    text = PRJ.replace("|Kind=2|AlternatePart=CAPX|", "|Kind=2|AlternatePart=|").replace(
        "AltLibLink_SymbolReference=CAPX", "AltLibLink_SymbolReference=SYMX")
    streams = _library_streams()
    streams["SYMX/Data"] = _library_part(libref="SYMX", comment="wrong part")
    data = _parse(tmp_path, text, streams=streams)
    (alternate,) = data.variants[1].alternate_parts()
    assert alternate["lib_reference"] == "CAPX" and alternate["comment"] == "10u X5R"
    assert alternate["link"]["SymbolReference"] == "SYMX"
    netlist = _netlist()
    plan = plan_variant(data.variants[1], netlist, variant_names(netlist))
    c1 = next(p for p in build_parts(netlist, plan)[0] if p.designator == "C1")
    assert (c1.alternate_part, c1.lib_reference) == ("CAPX", "CAPX")


def test_library_parse_skips_the_default_component_and_bad_storages(tmp_path):
    streams = _library_streams()
    streams["Odd/Data"] = record("|RECORD=41|NAME=Comment|TEXT=x")  # no RECORD=1 first
    with patch("olefile.OleFileIO", lambda p: _FakeOle(streams)):
        parts = parse_variant_library("any")
    assert [part.lib_reference for part in parts] == ["CAPX"]


def test_old_format_stays_dnp_only(tmp_path):
    text = ("[Variation1]\nVariantName=Old\n[CompVar1]\nVariantKind=3\nRefDesignator1=R5\n"
            "[CompVar2]\nVariantKind=1\nRefDesignator1=R6\n")
    (variant,) = _parse(tmp_path, text, library=False).variants
    assert (variant.name, variant.dnp_refdes, variant.variations) == ("Old", ["R5"], [])


# ---------- binding and the active variant's values ----------

def test_parameter_variations_follow_the_unique_id(data):
    netlist = _netlist()
    # R10 was renamed R11 since the variation was made: Altium follows the UniqueId.
    netlist["components"]["R11"] = netlist["components"].pop("R10")
    netlist["parts"]["R11"] = netlist["parts"].pop("R10")
    variant_state = VariantState(data.variants)
    main_mod._resolve_dnp_uids(variant_state, netlist)
    lite = variant_state.get("Lite")
    assert set(lite.bound_params) == {"C1", "R11"}
    params, alternate = lite.variation_of("R11")
    assert params["comment"] == ("Comment", "22k") and alternate is None
    variant_state.set_variant("Lite")
    result = json.loads(main_mod._get_component_impl(netlist, variant_state, "R11"))
    assert result["variant_changes"]["varied_parameters"][0]["parameter"] == "Comment"


def test_get_component_shows_the_active_variants_value(state):
    netlist = _netlist()
    result = json.loads(main_mod._get_component_impl(netlist, state, "R1"))  # Full
    assert result["value"] == "4k7" and result["dnp"] is False
    assert result["variant_changes"] == {
        "variant": "Full",
        "fields": {"value": {"variant": "4k7", "base": "10k"}},
        "varied_parameters": [{"parameter": "Value", "value": "4k7", "base": "10k"}],
    }
    plain = json.loads(main_mod._get_component_impl(netlist, state, "R2"))
    assert "variant_changes" not in plain and plain["value"] == "10k"
    dnp = json.loads(main_mod._get_component_impl(netlist, state, "R3"))
    assert dnp["dnp"] is True and "variant_changes" not in dnp


def test_get_component_reports_an_alternate_part(state):
    state.set_variant("Lite")
    result = json.loads(main_mod._get_component_impl(_netlist(), state, "C1"))
    changes = result["variant_changes"]
    assert result["description"] == "Cap 10u" and result["value"] == "10u"
    assert result["mpn"] == "CAP-10U"
    assert changes["fields"]["lib_reference"] == {"variant": "CAPX", "base": "CAP"}
    assert changes["fields"]["footprint"] == {"variant": "C0805", "base": "C0402"}
    assert changes["alternate_part"]["base_lib_reference"] == "CAP"
    assert changes["alternate_part"]["library"] == "Alt.SchLib"
    assert changes["varied_parameters"] == [
        {"parameter": "Comment", "value": "10u", "base": "100n"}]


def test_unbound_variant_uses_stored_designators(data):
    state = VariantState(data.variants)  # no netlist bound yet
    state.set_variant("Lite")
    result = json.loads(main_mod._get_component_impl(_netlist(), state, "C1"))
    assert result["variant_changes"]["alternate_part"]["lib_reference"] == "CAPX"


def test_part_changes_without_variation_is_none():
    assert part_changes({"value": "1k"}, "V", {}, None) is None


def test_variations_equal_to_the_base_change_nothing():
    """Description and Footprint are the part's fields, not parameters: a variation
    equal to them (or any variation equal to the base value) is no change."""
    meta = _component("RUID1", "10k", "10k", description="Resistor", footprint="R0603")
    same = {"description": ("Description", "Resistor"), "footprint": ("Footprint", "R0603"),
            "comment": ("Comment", "10k")}
    assert part_changes(meta, "V", same, None) is None
    changed = part_changes(meta, "V", {**same, "description": ("Description", "Thin film")},
                           None)
    assert changed["fields"] == {"description": {"variant": "Thin film", "base": "Resistor"}}
    assert changed["varied_parameters"] == [
        {"parameter": "Description", "value": "Thin film", "base": "Resistor"}]


def test_a_not_fitted_part_shows_its_base_values(tmp_path):
    """Variations stored on a part the variant does not fit do not apply (get_bom reports
    them as stale): get_component, the sheet, list_variants and the snapshot leave them
    out."""
    text = PRJ.replace("ParamVariationCount=3\n", "ParamVariationCount=4\n").replace(
        "ParamVariation3=ParameterName=Comment|VariantValue=22k\n",
        "ParamVariation3=ParameterName=Comment|VariantValue=22k\n"
        "ParamDesignator4=R2\nParamVariation4=ParameterName=Comment|VariantValue=DEAD\n")
    state = VariantState(_parse(tmp_path, text).variants)
    netlist = _netlist()
    main_mod._resolve_dnp_uids(state, netlist)
    state.set_variant("Lite")
    r2 = json.loads(main_mod._get_component_impl(netlist, state, "R2"))
    assert r2["dnp"] is True and r2["mpn"] == "=Value" and "variant_changes" not in r2
    assert "R2|=Value|Resistor|10k [DNP]\n\n" in build_sheet_context(netlist, "Main", state)
    lite = json.loads(main_mod._list_variants_impl(state))["variants"][1]
    assert lite["varied_components"] == ["C1", "R10"]
    assert all(row["refdes"] != "R2" for row in export_rows(state.variants, netlist))
    plan = plan_variant(state.get("Lite"), netlist, variant_names(netlist))
    assert {"designator": "R2", "issue": "varied_but_not_fitted",
            "detail": "parameter variations on a part the variant does not fit"} in plan.stale


def test_search_components_shows_the_active_variants_values(state):
    netlist = _netlist()
    base = json.loads(main_mod._search_components_impl(netlist, "^R1$", "refdes"))
    assert base["results"][0]["value"] == "10k"
    full = json.loads(main_mod._search_components_impl(netlist, "^R1$", "refdes", state))
    assert full["results"][0]["value"] == "4k7"
    state.set_variant("Lite")
    found = json.loads(main_mod._search_components_impl(netlist, "CAP-10U", "mpn", state))
    assert found["results"][0]["refdes"] == ["C1"]
    assert found["results"][0]["description"] == "Cap 10u"
    missing = json.loads(main_mod._search_components_impl(netlist, "CAP-10U", "mpn"))
    assert missing["match_count"] == 0


def test_sheet_context_shows_the_variant_line(state):
    netlist = _netlist()
    text = build_sheet_context(netlist, "Main", state)
    assert "R1|=Value|Resistor|4k7\n  variant Full: value 4k7 (base 10k)\n" in text
    state.set_variant("Lite")
    text = build_sheet_context(netlist, "Main", state)
    assert "C1|CAP-10U|Cap 10u|10u" in text
    line = next(line for line in text.splitlines() if line.startswith("  variant Lite"))
    assert "lib_reference CAPX (base CAP)" in line and "alternate part CAPX from Alt.SchLib" in line
    assert ("R10|22k|Resistor|100k\n  variant Lite: mpn 22k (base 100k); Comment=22k (base "
            "100k); Tolerance=1% (base 5%)\n") in text


def test_sheet_context_unchanged_without_variations():
    state = VariantState([VariantDefinition(name="Default")])
    text = build_sheet_context(_netlist(), "Main", state)
    assert "variant" not in text


# ---------- BOM ----------

def test_bom_takes_the_alternates_library_copy(data):
    netlist = _netlist()
    plan = plan_variant(data.variants[1], netlist, variant_names(netlist))
    parts = {part.designator: part for part in build_parts(netlist, plan)[0]}
    c1 = parts["C1"]
    assert (c1.comment, c1.description, c1.mpn, c1.footprint) == (
        "10u", "Cap 10u", "CAP-10U", "C0805")
    assert (c1.alternate_part, c1.alternate_library, c1.lib_reference) == ("CAPX", True, "CAPX")
    assert parts["R10"].comment == "22k" and not parts["R2"].fitted


def test_bom_alternate_without_library_copy_keeps_base_data(tmp_path):
    data = _parse(tmp_path, library=False)
    netlist = _netlist()
    plan = plan_variant(data.variants[1], netlist, variant_names(netlist))
    c1 = next(p for p in build_parts(netlist, plan)[0] if p.designator == "C1")
    assert (c1.description, c1.mpn, c1.alternate_library) == ("Capacitor", "CAP-100N", False)
    assert c1.footprint == "C0805" and c1.comment == "10u"


def test_get_bom_applies_variations(state, monkeypatch, tmp_path):
    _patch_main(monkeypatch, tmp_path, state)
    full = json.loads(main_mod.get_bom(variant="Full", group_by="comment"))
    r1 = next(line for line in full["lines"] if "R1" in line["designators"])
    assert r1["value"] == "4k7" and r1["varied_parameters"] == ["Value"]
    lite = json.loads(main_mod.get_bom(variant="Lite"))
    c1 = next(line for line in lite["lines"] if "C1" in line["designators"])
    assert c1["alternate_parts"] == {"C1": "CAPX"} and c1["mpn"] == "CAP-10U"
    assert "PrjPcbVariants" in lite["alternate_parts_note"]
    checked = json.loads(main_mod.check_bom(variant="Lite"))
    assert checked["variant"] == "Lite"


# ---------- compare_variants ----------

def _patch_main(monkeypatch, tmp_path, state, netlist=None):
    netlist = netlist if netlist is not None else _netlist()
    prj = tmp_path / "Demo.PrjPcb"
    if not prj.exists():
        prj.write_text(PRJ, encoding="utf-8")
    monkeypatch.setattr(main_mod, "_project", {
        "name": "Demo", "root_dir": str(tmp_path), "prj_pcb_path": str(prj),
        "sheets": [{"name": "Main", "path": str(tmp_path / "Main.SchDoc")}],
        "pcb_doc_paths": [], "pcb_dwf_paths": [],
    })
    monkeypatch.setattr(main_mod._altium, "_netlist", netlist)
    monkeypatch.setattr(main_mod, "_variant_state", state)
    monkeypatch.setattr(main_mod, "_design_notes", main_mod.DesignNotesCache())
    return netlist


def test_compare_two_variants(state, monkeypatch, tmp_path):
    _patch_main(monkeypatch, tmp_path, state)
    result = json.loads(main_mod.compare_variants("Full", "Lite"))
    assert (result["a"], result["b"], result["binding"]) == ("Full", "Lite", "unique_id")
    assert result["summary"] == {"fitted": 2, "values": 4, "parameters": 2,
                                 "alternate_parts": 1, "variant_parameters": 1}
    diff = result["differences"]
    assert diff["fitted"] == [
        {"designator": "R2", "a": "fitted", "b": "not fitted"},
        {"designator": "R3", "a": "not fitted", "b": "fitted"},
    ]
    # C1's alternate brings its library copy's Value and MPN (not varied by the variant).
    assert diff["values"] == [
        {"designator": "C1", "parameter": "Comment", "a": "100n", "b": "10u", "base": "100n"},
        {"designator": "C1", "parameter": "Value", "a": "100n", "b": "10u", "base": "100n"},
        {"designator": "R1", "parameter": "Value", "a": "4k7", "b": "10k", "base": "10k"},
        {"designator": "R10", "parameter": "Comment", "a": "100k", "b": "22k", "base": "100k"},
    ]
    assert diff["parameters"] == [
        {"designator": "C1", "parameter": "Manufacturer Part Number 1", "a": "CAP-100N",
         "b": "CAP-10U", "base": "CAP-100N"},
        {"designator": "R10", "parameter": "Tolerance", "a": "5%", "b": "1%", "base": "5%"}]
    (alternate,) = diff["alternate_parts"]
    assert alternate["a"] == {"part": "base", "lib_reference": "CAP", "footprint": "C0402"}
    assert alternate["b"]["part"] == "alternate" and alternate["b"]["footprint"] == "C0805"
    assert diff["variant_parameters"] == [{"parameter": "Build", "a": None, "b": "Lite build"}]
    swapped = json.loads(main_mod.compare_variants("Lite", "Full"))
    assert swapped["differences"]["fitted"][0] == {"designator": "R2", "a": "not fitted",
                                                   "b": "fitted"}
    assert swapped["summary"] == result["summary"]


def test_compare_against_the_base_design(state, monkeypatch, tmp_path):
    _patch_main(monkeypatch, tmp_path, state)
    result = json.loads(main_mod.compare_variants())  # the active Full vs the base
    assert (result["a"], result["b"]) == ("Full", BASE_VARIANT)
    assert result["differences"]["fitted"] == [
        {"designator": "R3", "a": "not fitted", "b": "fitted"}]
    assert result["differences"]["values"][0]["b"] == "10k"
    reverse = json.loads(main_mod.compare_variants("[No Variations]", "Full"))
    assert reverse["a"] == BASE_VARIANT and reverse["summary"] == result["summary"]
    same = json.loads(main_mod.compare_variants("Full", "full"))
    assert same["differences_total"] == 0 and same["differences"] == {}


def test_fitted_in_one_lists_that_sides_variations():
    full = VariantDefinition(name="A", variations=[Variation("R2", "\\RUID2", 0)],
                             param_variations={"R2": {"value": ("Value", "1k")}})
    lite = VariantDefinition(name="B", dnp_refdes=["R2"],
                             variations=[Variation("R2", "\\RUID2", 1)])
    rows = compare(view_of(full, _netlist()), view_of(lite, _netlist()), _netlist())
    assert rows == [{"group": "fitted", "designator": "R2", "a": "fitted", "b": "not fitted",
                     "fitted_with_varied_parameters": ["Value=1k"]}]


def test_description_and_footprint_compare_against_the_parts_fields():
    """Equal to the part's description or footprint: no row; different: the row carries
    the part's field as the other side's and the base value."""
    variant = VariantDefinition(name="A", param_variations={
        "R1": {"description": ("Description", "Resistor"), "footprint": ("Footprint", "R0603")},
        "R2": {"description": ("Description", "Thin film"), "footprint": ("Footprint", "R0402")},
    })
    netlist = _netlist()
    rows = compare(view_of(variant, netlist), view_of(None, netlist), netlist)
    assert rows == [
        {"group": "parameters", "designator": "R2", "parameter": "Description",
         "a": "Thin film", "b": "Resistor", "base": "Resistor"},
        {"group": "parameters", "designator": "R2", "parameter": "Footprint",
         "a": "R0402", "b": "R0603", "base": "R0603"},
    ]
    assert [(r["refdes"], r["parameter"], r["base_value"]) for r in export_rows(
        [variant], netlist)][2:] == [("R2", "Description", "Resistor"),
                                     ("R2", "Footprint", "R0603")]


def test_unvalidated_note_only_for_unsampled_kinds():
    comment = {"group": "values", "designator": "R1", "parameter": "Comment", "a": "1k",
               "b": "2k"}
    value = {**comment, "parameter": "Value"}
    reselected = {"group": "alternate_parts", "designator": "C1", "b": {"part": "base"},
                  "a": {"part": "alternate", "same_as_base_part": True}}
    other = {**reselected, "a": {"part": "alternate", "lib_reference": "CAPX"}}
    assert not unvalidated([comment, reselected])
    assert unvalidated([value]) and unvalidated([other])
    assert unvalidated([{"group": "fitted", "designator": "C1", "a": "fitted",
                         "b": "not fitted", "fitted_as_alternate": other["a"]}])


def test_an_unreadable_variant_library_is_reported(tmp_path, monkeypatch):
    path = _write(tmp_path)

    def broken(_):
        raise OSError("not an OLE file")

    with patch("olefile.OleFileIO", broken):
        state = VariantState(parse_prj_pcb(str(path)).variants)
    main_mod._resolve_dnp_uids(state, _netlist())
    _patch_main(monkeypatch, tmp_path, state)
    note = json.loads(main_mod.get_bom(variant="Lite"))["alternate_parts_note"]
    assert note.endswith("The .PrjPcbVariants file could not be read (OSError: not an OLE "
                         "file).")
    compared = json.loads(main_mod.compare_variants("Lite"))
    assert any("could not be read (OSError: not an OLE file)" in n for n in compared["notes"])
    lite = json.loads(main_mod.list_variants())["variants"][1]
    assert lite["alternate_parts"][0]["library_error"] == "OSError: not an OLE file"


def test_natural_designator_order():
    variant = VariantDefinition(name="V", dnp_refdes=["R10", "R2", "R1"])
    netlist = {"components": {}, "parts": {}}
    rows = compare(view_of(variant, netlist), view_of(None, netlist), netlist)
    assert [row["designator"] for row in rows] == ["R1", "R2", "R10"]


def test_compare_without_a_netlist_uses_stored_designators(data, monkeypatch, tmp_path):
    state = VariantState(data.variants)
    main_mod._resolve_dnp_uids(state, None)
    _patch_main(monkeypatch, tmp_path, state)
    monkeypatch.setattr(main_mod._altium, "_netlist", None)
    result = json.loads(main_mod.compare_variants("Lite"))
    assert result["binding"] == "stored_designators"
    assert "No netlist is loaded" in result["notes"][0]
    assert result["differences"]["values"][0] == {
        "designator": "C1", "parameter": "Comment", "a": "10u", "b": None}


def test_compare_errors(state, monkeypatch, tmp_path):
    _patch_main(monkeypatch, tmp_path, state)
    assert json.loads(main_mod.compare_variants("Nope"))["error"] == "variant_not_found"
    assert json.loads(main_mod.compare_variants("Full", "Nope"))["error"] == "variant_not_found"
    monkeypatch.setattr(main_mod, "_variant_state",
                        VariantState([VariantDefinition(name="Default")]))
    assert json.loads(main_mod.compare_variants())["error"] == "no_variants"
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.compare_variants())["error"] == "no_project"


def test_compare_pages_and_shrinks(state, monkeypatch, tmp_path):
    netlist = _patch_main(monkeypatch, tmp_path, state)
    many = [f"R{index}" for index in range(100, 260)]
    for name in many:
        netlist["components"][name] = _component(f"UID{name}", "1k", "1k")
    state.variants[0].dnp_refdes = many  # bound by designator below
    state.variants[0].variations = []
    state.variants[0].param_variations = {}
    first = json.loads(main_mod.compare_variants("Full"))
    assert (first["differences_total"], first["next_offset"]) == (160, 100)
    assert len(first["differences"]["fitted"]) == 100 and "documentation" in first
    second = json.loads(main_mod.compare_variants("Full", offset=100))
    assert second["has_more"] is False and "documentation" not in second
    assert second["differences"]["fitted"][0]["designator"] == "R200"
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 4000)
    trimmed = json.loads(main_mod.compare_variants("Full"))
    assert trimmed["differences_limited_to"] in (40, 15, 5, 1)
    assert len(json.dumps(trimmed, indent=2)) <= 4000 or trimmed["differences_limited_to"] == 1


def test_stale_entries_are_reported(data, monkeypatch, tmp_path):
    netlist = _netlist()
    del netlist["components"]["R2"], netlist["parts"]["R2"]
    state = VariantState(data.variants)
    main_mod._resolve_dnp_uids(state, netlist)
    _patch_main(monkeypatch, tmp_path, state, netlist)
    result = json.loads(main_mod.compare_variants("Lite"))
    assert result["stale_variant_entries"]["a"][0]["issue"] == "no_such_part"


# ---------- documentation per variant ----------

def _documented_project(tmp_path: Path) -> Path:
    outjob = _outjob([
        _output("BOM_PartType", "Bill of Materials", category="Report", variant="Full"),
        _output("Pick Place", "Pick and Place", category="Assembly", variant="Full"),
        _output("PCBDrawing", "Draftsman", category="Documentation", doc="Asm.PCBDwf"),
        _output("BOM_PartType", "Lite BOM", category="Report", variant="Lite", links={}),
    ], variant="[No Variations]")
    (tmp_path / "Release.OutJob").write_text(outjob, encoding="utf-8")
    (tmp_path / "Asm.PCBDwf").write_bytes(b"drawing")
    (tmp_path / "Board.PcbDoc").write_bytes(b"board")
    documents = "".join(f"[Document{i}]\nDocumentPath={name}\n" for i, name in enumerate(
        ("Board.PcbDoc", "Asm.PCBDwf", "Release.OutJob"), 1))
    prj = tmp_path / "Demo.PrjPcb"
    prj.write_text(documents + PRJ, encoding="utf-8")
    return prj


def _drawing() -> DraftsmanDoc:
    return DraftsmanDoc(
        document_variant="Lite",
        items=[DraftsmanItem(page=1, kind="view", view="assembly view", variant="Document"),
               DraftsmanItem(page=1, kind="note", title="Notes",
                             lines=["Build variant Full for production", "Text =VariantName"])],
    )


def test_documentation_check(state, monkeypatch, tmp_path):
    prj = _documented_project(tmp_path)
    _patch_main(monkeypatch, tmp_path, state)
    main_mod._project["prj_pcb_path"] = str(prj)
    main_mod._project["pcb_dwf_paths"] = [str(tmp_path / "Asm.PCBDwf")]
    monkeypatch.setattr(main_mod._design_notes, "draftsman", lambda path: _drawing())
    docs = json.loads(main_mod.compare_variants("Full", "Lite"))["documentation"]
    full, lite = docs["per_variant"]
    assert full["bom"] == ["Release.OutJob: Bill of Materials"]
    assert full["pick_and_place"] == ["Release.OutJob: Pick and Place"]
    assert full["outputs_missing"] == ["assembly_drawing"]
    assert full["draftsman_set_to_variant"] == [] and full["draftsman_notes_mention"] == ["Asm.PCBDwf"]
    # The Draftsman output prints its drawing's own variant; the Lite BOM is not connected.
    assert lite["assembly_drawing"] == ["Release.OutJob: Draftsman"]
    assert lite["outputs_missing"] == ["bom", "pick_and_place"]
    assert lite["draftsman_set_to_variant"] == ["Asm.PCBDwf"]
    drawing = docs["draftsman"]["Asm.PCBDwf"]
    assert drawing["prints_variant_name"] is True and drawing["drawing_variant"] == "Lite"
    assert docs["outjobs"] == 1


def test_documentation_survives_unreadable_outjobs(state, monkeypatch, tmp_path):
    _patch_main(monkeypatch, tmp_path, state)

    def broken(project, read_drawing):
        raise ValueError("bad OutJob")

    monkeypatch.setattr(main_mod, "variant_outputs", broken)
    docs = json.loads(main_mod.compare_variants())["documentation"]
    assert docs["outjob_error"] == "ValueError: bad OutJob"
    assert docs["per_variant"][0]["outputs_missing"] == ["bom", "pick_and_place",
                                                         "assembly_drawing"]


def test_a_note_names_a_variant_only_next_to_the_word_variant():
    """A variant named like an ordinary word ("Finish") is not counted where the word is
    used as one; title-block texts placed by the template are left out, a document
    parameter about the variant counts."""
    from services.variants import draftsman_variants

    def mentions(*items, parameters=None, names=("Q", "Finish")):
        doc = DraftsmanDoc(items=list(items), parameters=parameters or {})
        return draftsman_variants([("D.PCBDwf", doc)], list(names))["D.PCBDwf"]["mentions"]

    def text(value, template=False):
        return DraftsmanItem(page=1, kind="text", text=value, from_template=template)

    assert mentions(text("Section Q of R"), text("Surface finish: ENIG")) == []
    assert mentions(text("Fit for variant Q")) == ["Q"]
    assert mentions(text("Finish variant only"), text("VARIANT: q")) == ["Q", "Finish"]
    assert mentions(text("Variant: Finish", template=True)) == []
    assert mentions(parameters={"Variant": "Finish", "Finish": "ENIG"}) == ["Finish"]
    assert draftsman_variants([("X.PCBDwf", None)], ["Q"]) == {"X.PCBDwf": {"readable": False}}
    doc = DraftsmanDoc(items=[text("=VariantName", template=True)])
    assert draftsman_variants([("T.PCBDwf", doc)], ["Q"])["T.PCBDwf"]["prints_variant_name"]


# ---------- list_variants and the snapshot ----------

def test_list_variants_reports_variations(state):
    result = json.loads(main_mod._list_variants_impl(state))
    full, lite = result["variants"]
    assert full["varied_components"] == ["R1"] and full["varied_parameter_count"] == 1
    assert "alternate_parts" not in full and "allow_fabrication" not in full
    assert lite["varied_components"] == ["C1", "R10"] and lite["varied_parameter_count"] == 3
    assert lite["alternate_parts"] == [{"designator": "C1", "lib_reference": "CAPX",
                                        "library": "Alt.SchLib", "footprint": "C0805",
                                        "same_footprint": False}]
    assert lite["allow_fabrication"] is True and lite["parameters"] == {"Build": "Lite build"}
    plain = json.loads(main_mod._list_variants_impl(
        VariantState([VariantDefinition(name="P", dnp_refdes=["R1"])])))
    assert set(plain["variants"][0]) == {"name", "dnp_count", "dnp_components", "is_active"}


def test_snapshot_variant_variations_table(state, tmp_path):
    netlist = _netlist()
    rows = export_rows(state.variants, netlist)
    assert [(r["variant"], r["refdes"], r["kind"], r.get("parameter")) for r in rows] == [
        ("Full", "R1", "parameter", "Value"),
        ("Lite", "C1", "parameter", "Comment"),
        ("Lite", "C1", "alternate_part", None),
        ("Lite", "R10", "parameter", "Comment"),
        ("Lite", "R10", "parameter", "Tolerance"),
    ]
    project = {"name": "Demo", "root_dir": str(tmp_path), "sheets": [{"name": "Main"}],
               "prj_pcb_path": str(tmp_path / "Demo.PrjPcb")}
    from export import export_project
    path = export_project(project, netlist, state, "0.0.1", variant_variations=rows)
    with sqlite3.connect(path) as conn:
        stored = conn.execute("SELECT variant, refdes, kind, value, base_value, data FROM "
                              "variant_variations WHERE kind='alternate_part'").fetchall()
    assert stored[0][:5] == ("Lite", "C1", "alternate_part", "CAPX", "CAP")
    assert json.loads(stored[0][5])["footprint"] == "C0805"
