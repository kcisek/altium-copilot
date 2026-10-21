"""check_fab_drawing: Draftsman drawings classified, their notes checked against the
fabrication and assembly checklists, and what they state or cache cross-checked with the
design. Synthetic DraftsmanDoc and BoardFacts objects cover each item and check; a
synthetic .PCBDwf covers the parser's new caches; Altium's public AD25 examples are
snapshot-checked when installed."""
import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import main
from parsers.pcb_dwf import DraftsmanDoc, DraftsmanItem, parse_pcb_dwf
from parsers.prj_pcb import VariantDefinition, VariantState
from parsers.pcb_doc import PcbLayer
from services.fab_drawing import (
    CHECKS,
    KB,
    BoardFacts,
    board_facts,
    build_fab_drawing,
    file_history,
    page_fab_drawing,
)
from services.output_jobs import draftsman_outputs
from tests.draftsman_fixture import _hole, _param, lz4_stream
from tests.test_output_jobs import _outjob, _output, _project

EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")
PCB = r"C:\Projects\Board\Board.PcbDoc"
UPDATED = "2026-10-01T12:00:00.0000000Z"
UPDATED_TS = datetime(2026, 10, 1, 12, tzinfo=timezone.utc).timestamp()

FAB_NOTES = [
    "Fabricate to IPC-6012 Class 2, acceptability per IPC-A-600 Class 2.",
    "Material: FR-4, Tg 170 min, UL 94 V-0.",
    "Surface finish: ENIG per IPC-4552.",
    "Solder mask: green LPI both sides. Silkscreen: white epoxy ink.",
    "Finished board thickness 1.60 mm +/- 10%.",
    "4 layer board, 1 oz finished copper on outer layers.",
    "Controlled impedance: 50 ohm single-ended +/- 10%.",
    "Hole position tolerance +/- 0.075 mm, layer-to-layer registration per IPC-6012.",
    "100% electrical test per IPC-D-356.",
]
STACK = [
    {"layer": "Top Solder", "type": "SolderMask", "material": "Solder Resist", "thickness_mm": 0.02},
    {"layer": "Top Layer", "type": "Signal", "material": "Copper", "weight": "1oz",
     "thickness_mm": 0.035},
    {"layer": "Dielectric 1", "type": "Dielectric", "material": "FR-4", "thickness_mm": 0.5},
    {"layer": "L2", "type": "Plane", "material": "Copper", "weight": "0.5oz", "thickness_mm": 0.0175},
    {"layer": "Core", "type": "Dielectric", "material": "FR-4", "thickness_mm": 0.385},
    {"layer": "L3", "type": "Plane", "material": "Copper", "weight": "0.5oz", "thickness_mm": 0.0175},
    {"layer": "Dielectric 2", "type": "Dielectric", "material": "FR-4", "thickness_mm": 0.55},
    {"layer": "Bottom Layer", "type": "Signal", "material": "Copper", "weight": "1oz",
     "thickness_mm": 0.035},
    {"layer": "Bottom Solder", "type": "SolderMask", "material": "Solder Resist",
     "thickness_mm": 0.04},
]
DRILLS = [{"size_mm": 0.3, "plated": True, "type": "Via", "span": "Top Layer - Bottom Layer",
           "count": 10},
          {"size_mm": 1.0, "plated": True, "type": "Pad", "span": "Top Layer - Bottom Layer",
           "count": 4},
          {"size_mm": 3.2, "plated": False, "type": "Pad", "span": "Top Layer - Bottom Layer",
           "count": 2}]
BOARD_DRILLS = [{"type": "Via", "size_mm": 0.3, "plated": True, "slot_length_mm": None, "count": 10},
                {"type": "Pad", "size_mm": 1.0, "plated": True, "slot_length_mm": None, "count": 4},
                {"type": "Pad", "size_mm": 3.2, "plated": False, "slot_length_mm": None, "count": 2}]
PROFILE = {"profile": "SE50", "differential": False, "target_ohm": 50.0, "tolerance_pct": 10.0,
           "layer": "Top Layer", "width_mm": 0.3, "gap_mm": None, "calculated_ohm": 50.1,
           "substack": None}


def _note(lines, page=1, title="Notes:"):
    return DraftsmanItem(page=page, kind="note", title=title, lines=list(lines))


def _view(view, side=None, page=1, variant=None, options=None):
    return DraftsmanItem(page=page, kind="view", view=view, side=side, variant=variant,
                         options=options or {})


FAB_VIEWS = [_view("fabrication view", "Top"), _view("drill table"),
             _view("layer stack legend")]


def _doc(items, **fields) -> DraftsmanDoc:
    values = {"pages": ["A3"], "items": list(items), "source_pcb": PCB, "last_update": UPDATED,
              "document_variant": "[No Variations]", "layer_stack": STACK,
              "drill_table": DRILLS}
    values.update(fields)
    return DraftsmanDoc(**values)


def _fab_doc(notes=FAB_NOTES, **fields) -> DraftsmanDoc:
    return _doc([_note(notes)] + FAB_VIEWS, **fields)


def _facts(**fields) -> BoardFacts:
    values = {"pcb_doc": "Board.PcbDoc", "path": PCB, "saved": UPDATED_TS - 60,
              "copper_layers": 4, "total_mm": 1.6, "laminate_mm": 1.54,
              "copper_mm": [("Top Layer", 0.035), ("L2", 0.0175), ("L3", 0.0175),
                            ("Bottom Layer", 0.035)],
              "materials": ["FR-4", "FR-4", "FR-4"], "impedance": [dict(PROFILE)],
              "drills": [dict(row) for row in BOARD_DRILLS],
              "parts": {"top": 10, "bottom": 2}}
    values.update(fields)
    return BoardFacts(**values)


def _run(docs, facts="default", outputs=(), active=None, bom=None, document=None):
    facts = _facts() if facts == "default" else facts
    project = {"name": "Board", "pcb_doc_paths": [PCB],
               "pcb_dwf_paths": [rf"C:\Projects\Board\{name}" for name in docs]}

    def read(path):
        value = docs[PureWindowsPath(path).name]
        if isinstance(value, Exception):
            raise value
        return value

    def read_board(path):
        return (facts, None) if facts is not None else (None, "PCB document not found on disk")
    return build_fab_drawing(project, read, read_board, list(outputs), active, bom, document)


def _detail(model, name="Fab.PCBDwf"):
    return next(d for d in model["details"] if d["drawing"] == name)


def _item(model, item, name="Fab.PCBDwf"):
    return next(i for i in _detail(model, name)["checklist"] if i["item"] == item)


def _findings(model, check):
    return [f for f in model["findings"] if f["check"] == check]


# ---------- classification and the checklist ----------

def test_complete_fab_drawing_finds_every_item():
    model = _run({"Fab.PCBDwf": _fab_doc()})
    detail = _detail(model)
    assert detail["kind"] == "fabrication" and "also" not in detail
    statuses = {i["item"]: i["status"] for i in detail["checklist"]}
    assert statuses == {
        "ipc_class": "found", "material": "found", "tg": "found", "flammability": "found",
        "surface_finish": "found", "mask_colour": "found", "silkscreen_colour": "found",
        "thickness": "found", "copper_weight": "found", "impedance": "found",
        "drill_table": "found", "registration_tolerances": "found", "netlist_test": "found",
        "rigid_flex": "not_applicable",
    }
    assert _item(model, "ipc_class")["detail"] == "class 2"
    assert _item(model, "surface_finish")["detail"] == "ENIG"
    assert _item(model, "mask_colour")["detail"] == "green"
    assert _item(model, "silkscreen_colour")["detail"] == "white"
    assert _item(model, "thickness")["found"][0] == {
        "text": "Finished board thickness 1.60 mm +/- 10%.", "where": "page 1, note 'Notes:' line 5"}
    assert detail["cross_checks"]["thickness"]["stated"][0]["tolerance_mm"] == 0.16
    assert {k: v["result"] for k, v in detail["cross_checks"].items()} == {
        "thickness": "match", "layer_count": "match", "materials": "match",
        "copper_weight": "match", "impedance": "match", "drill_table": "match",
        "layer_stack_legend": "match", "source_pcb": "match", "stale": "current"}
    assert model["findings"] == []
    assert "suggested_notes" not in detail
    assert model["drawing_kinds"] == {"fabrication": 1}


def test_missing_must_haves_are_warnings_with_suggested_lines():
    model = _run({"Fab.PCBDwf": _doc([_note(["Do not scale this drawing."])] + FAB_VIEWS,
                                     layer_stack=[])})
    (required,) = _findings(model, "required_items")
    assert required["severity"] == "warning"
    assert [e["item"] for e in required["examples"]] == [
        "ipc_class", "material", "surface_finish", "thickness"]
    suggested = {e["item"]: e["suggested_note"] for e in required["examples"]}
    # The design's own numbers where the files give them, <...> where they do not.
    assert suggested["thickness"] == ("Finished board thickness 1.60 mm +/- <tolerance, e.g. "
                                      "10%> (the layer stack's total).")
    assert suggested["material"].startswith("Laminate: FR-4 per IPC-4101/<slash sheet>")
    assert "<ENIG per IPC-4552" in suggested["surface_finish"]
    assert "tin-lead" in _item(model, "surface_finish")["detail"]
    (other,) = _findings(model, "other_items")
    assert other["severity"] == "info"
    copper = next(e for e in other["examples"] if e["item"] == "copper_weight")
    assert copper["suggested_note"] == ("Copper weights per the layer stack: outer 1 oz, inner "
                                        "0.5 oz (4 copper layers).")
    impedance = next(e for e in other["examples"] if e["item"] == "impedance")
    assert "50 ohm single-ended +/-10%" in impedance["suggested_note"]
    assert len(_detail(model)["suggested_notes"]) == _detail(model)["checklist_summary"]["missing"]


def test_items_named_only_in_the_layer_stack_legend():
    stack = [dict(row, tg="170") if row["type"] == "Dielectric" else row for row in STACK]
    model = _run({"Fab.PCBDwf": _doc([_note(["Do not scale."])] + FAB_VIEWS, layer_stack=stack)})
    assert _item(model, "material")["status"] == "found"
    assert "layer stack legend: FR-4" in _item(model, "material")["detail"]
    assert _item(model, "tg")["detail"].startswith("only in the layer stack legend: Tg 170")
    assert _item(model, "copper_weight")["detail"] == "only in the layer stack legend: 0.5oz, 1oz"


def test_partial_items_say_what_is_missing():
    notes = ["Fabricate per IPC-6012.", "Solder mask both sides.", "Board thickness 1.6 mm.",
             "Material FR-4, high Tg.", "Impedance controlled on layer 1, 50 ohm."]
    model = _run({"Fab.PCBDwf": _fab_doc(notes)})
    assert _item(model, "ipc_class")["status"] == "missing"
    assert _item(model, "ipc_class")["detail"] == "an IPC specification is named without a class"
    assert _item(model, "mask_colour")["detail"] == "named without a colour"
    assert _item(model, "thickness")["detail"] == "no tolerance is stated"
    assert _item(model, "tg")["detail"] == "Tg is mentioned without a value"
    assert _item(model, "impedance")["detail"] == "impedance is noted without a tolerance"


def test_finish_families_and_their_spellings():
    for text, family in (("Finish: immersion silver", "ImAg"), ("Finish HASL lead free", "LF-HASL"),
                         ("ENEPIG both sides", "ENEPIG"), ("Finish: OSP", "OSP"),
                         ("Electroless nickel, immersion gold", "ENIG"),
                         ("ImSn finish", "ImSn"), ("Finish: HASL", "HASL")):
        model = _run({"Fab.PCBDwf": _fab_doc([text])})
        assert _item(model, "surface_finish")["detail"] == family, text


def test_material_names_with_a_suffix_and_spec_revisions():
    # Hyphenated product names and an IPC-4101 revision letter are named laminates.
    model = _run({"Fab.PCBDwf": _fab_doc(["Material: FR4-TG170"], layer_stack=[])})
    assert _item(model, "material")["status"] == "found" and "detail" not in _item(model, "material")
    assert _item(model, "tg")["status"] == "found"
    assert _detail(model)["cross_checks"]["materials"]["result"] == "match"
    product = _run({"Fab.PCBDwf": _fab_doc(["Laminate S1000-2 or equivalent."], layer_stack=[])},
                   facts=_facts(materials=["S1000-2"]))
    assert _detail(product)["cross_checks"]["materials"]["result"] == "match"
    spec = _run({"Fab.PCBDwf": _fab_doc(["Laminate per IPC-4101E/126."], layer_stack=[])})
    assert _item(spec, "material")["status"] == "found" and _item(spec, "tg")["status"] == "found"


def test_ipc_class_spellings_and_whose_class_it_is():
    colon = _run({"Fab.PCBDwf": _fab_doc(["Fabricate per IPC-6012, Class: 3."])})
    assert _item(colon, "ipc_class")["detail"] == "class 3"
    # One line naming the board's and the assembly's class: each keeps its own.
    notes = FAB_NOTES + ["Bare board to IPC-6012 Class 2, assembly to IPC-A-610 Class 3.",
                         "Solder mask per IPC-SM-840 Class 3."]
    model = _run({"Fab.PCBDwf": _doc([_note(notes), _view("assembly view", "Top"),
                                      _view("assembly view", "Bottom")] + FAB_VIEWS)})
    assert _item(model, "ipc_class")["detail"] == "class 2"
    assert not _findings(model, "contradiction")
    assert _detail(model)["also"] == ["assembly"]
    assert _item(model, "assembly_standard")["detail"] == "class 3"


def test_words_that_are_no_colour_and_finishes_that_are_not_wanted():
    notes = FAB_NOTES + ["Keep silkscreen clear of pads.", "Solder mask clear of the annular ring.",
                         "Blue peelable mask over the edge fingers.",
                         "Surface finish ENIG, no HASL.", "HASL is not acceptable."]
    model = _run({"Fab.PCBDwf": _fab_doc(notes)})
    assert _item(model, "silkscreen_colour")["detail"] == "white"
    assert _item(model, "mask_colour")["detail"] == "green"
    assert _item(model, "surface_finish")["detail"] == "ENIG"
    assert not _findings(model, "contradiction")
    lead_free = _run({"Fab.PCBDwf": _fab_doc(["Surface finish: HASL LF."])})
    assert _item(lead_free, "surface_finish")["detail"] == "LF-HASL"


def test_thickness_phrasings():
    def cross(notes, **facts):
        model = _run({"Fab.PCBDwf": _fab_doc(notes)}, facts=_facts(**facts))
        return model, _detail(model)["cross_checks"].get("thickness")
    # A value before "thick".
    model, result = cross(["PCB 1.6 mm thick."])
    assert _item(model, "thickness")["status"] == "found" and result["result"] == "match"
    # A tolerance on its own line is the stated thickness's tolerance, not a thickness.
    for line, tolerance in (("Thickness tolerance: +/- 10%", 0.16),
                            ("Board thickness tolerance +/- 0.1 mm", 0.1)):
        model, result = cross(["Board thickness: 1.6 mm", line])
        assert [r["stated_mm"] for r in result["stated"]] == [1.6], line
        assert result["stated"][0]["tolerance_mm"] == tolerance
        assert not _findings(model, "thickness") and not _findings(model, "contradiction")
    # A maximum or a minimum is a bound, not the nominal thickness.
    model, result = cross(["Max. board thickness 2 mm."])
    assert result["result"] == "match" and result["stated"][0]["bound"] == "max"
    model, result = cross(["Minimum board thickness 2 mm."])
    assert result["result"] == "mismatch" and _findings(model, "thickness")
    # A number that is not the thickness's own (no unit, words between) is no value.
    model, result = cross(["Board thickness per the stack table on sheet 2."])
    assert result is None
    assert _item(model, "thickness")["detail"] == "thickness is mentioned without a value"
    model, result = cross(["4 layers, 1.6mm thick, FR-4."])
    assert [r["stated_mm"] for r in result["stated"]] == [1.6] and not _findings(model, "thickness")
    # Another tolerance later in the line is not the thickness's.
    model, result = cross(["Board thickness 1.6 mm, hole tolerance +/- 0.05 mm."], total_mm=1.5,
                          laminate_mm=1.45)
    assert result["stated"][0]["tolerance_mm"] is None and result["result"] == "match"
    model, result = cross(["PCB thickness 1.6 mm nominal +/- 0.1 mm."])
    assert result["stated"][0]["tolerance_mm"] == 0.1


def test_suggested_lines_do_not_invent_values():
    model = _run({"Fab.PCBDwf": _fab_doc(["Do not scale."])}, facts=_facts(substacks=2, flex=True))
    suggested = {e["item"]: e["suggested_note"]
                 for f in model["findings"] if f["check"] in ("required_items", "other_items")
                 for e in f["examples"]}
    # A rigid-flex stack's total is no region's thickness; the rating is to agree.
    assert suggested["thickness"] == "Finished board thickness <thickness> mm +/- <tolerance, e.g. 10%>."
    assert suggested["flammability"] == "Flammability rating UL 94 <V-0>."


def test_rigid_flex_items_only_on_a_flex_stack():
    notes = FAB_NOTES + ["Coverlay on both sides of the flex, stiffener under J1."]
    model = _run({"Fab.PCBDwf": _fab_doc(notes)}, facts=_facts(flex=True, substacks=2))
    item = _item(model, "rigid_flex")
    assert item["status"] == "found"
    assert item["detail"] == ("found: coverlay, stiffener; not found: IPC-6013, bend, "
                              "flex material")
    assert _detail(model)["cross_checks"]["thickness"]["result"] == "not_checked"
    rigid = _run({"Fab.PCBDwf": _fab_doc()})
    assert _item(rigid, "rigid_flex")["status"] == "not_applicable"


def test_impedance_not_applicable_without_profiles_or_notes():
    notes = [n for n in FAB_NOTES if "impedance" not in n]
    model = _run({"Fab.PCBDwf": _fab_doc(notes)}, facts=_facts(impedance=[]))
    assert _item(model, "impedance")["status"] == "not_applicable"


def test_assembly_and_other_drawings():
    assembly = _doc([_note(["Assemble per IPC-A-610 Class 2.", "Do not populate DNP parts."]),
                     _view("assembly view", "Top"),
                     _view("bill of materials", options={"data_source": "Project"})],
                    layer_stack=[], drill_table=[])
    other = _doc([_view("realistic view", "Top")], layer_stack=[], drill_table=[])
    model = _run({"Asm.PCBDwf": assembly, "Other.PCBDwf": other})
    detail = _detail(model, "Asm.PCBDwf")
    assert detail["kind"] == "assembly"
    statuses = {i["item"]: i["status"] for i in detail["checklist"]}
    assert statuses == {"assembly_standard": "found", "bom_table": "found",
                        "assembly_views": "missing"}
    assert _item(model, "assembly_views", "Asm.PCBDwf")["detail"] == (
        "parts on bottom and top; no assembly view of the bottom side")
    assert _detail(model, "Other.PCBDwf")["kind"] == "other"
    assert _detail(model, "Other.PCBDwf")["checklist"] == []
    assert model["drawing_kinds"] == {"assembly": 1, "other": 1}
    assert not _findings(model, "required_items")


def test_a_fab_drawing_with_assembly_notes_gets_both_checklists():
    notes = FAB_NOTES + ["Assemble per IPC-A-610 Class 3.", "Solder paste per stencil.",
                         "Reflow profile per J-STD-020.", "Conformal coating after test."]
    model = _run({"Fab.PCBDwf": _doc([_note(notes), _view("assembly view", "Top"),
                                      _view("assembly view", "Bottom")] + FAB_VIEWS)})
    detail = _detail(model)
    assert detail["kind"] == "fabrication" and detail["also"] == ["assembly"]
    assert {"ipc_class", "assembly_standard"} <= {i["item"] for i in detail["checklist"]}


# ---------- cross-checks ----------

def test_stated_thickness_and_layer_count_that_contradict_the_stack_are_errors():
    notes = ["Board thickness 1.0 mm +/- 0.1 mm.", "6 layer PCB."]
    model = _run({"Fab.PCBDwf": _fab_doc(notes)})
    (thickness,) = _findings(model, "thickness")
    assert thickness["severity"] == "error"
    assert thickness["examples"][0]["stated_mm"] == 1.0
    assert thickness["examples"][0]["tolerance_mm"] == 0.1
    assert "stack totals 1.600 mm" in thickness["issue"]
    (layers,) = _findings(model, "layer_count")
    assert layers["severity"] == "error"
    assert layers["examples"][0]["stated"] == 6 and layers["examples"][0]["copper_layers"] == 4
    assert model["summary"]["error"] == 2


def test_layer_phrasings_that_are_no_layer_count():
    notes = FAB_NOTES + ["Double-sided SMT assembly, reflow both sides.",
                         "Outer 2 layers 1 oz, inner 2 layers 0.5 oz.",
                         "4 layer rigid-flex: 4 layer rigid section, 2 layer flex section."]
    model = _run({"Fab.PCBDwf": _fab_doc(notes)})
    assert not _findings(model, "layer_count")
    assert _detail(model)["cross_checks"]["layer_count"]["stated"] == [4]
    two = _run({"Fab.PCBDwf": _fab_doc(["Double-sided PCB."])})
    assert _findings(two, "layer_count")[0]["examples"][0]["stated"] == 2
    # "rigid-flex" describes the whole board: its count is the stack's.
    six = _run({"Fab.PCBDwf": _fab_doc(["6 layer rigid-flex PCB."])})
    assert _findings(six, "layer_count")[0]["examples"][0]["stated"] == 6


def test_thickness_units_and_copper_to_copper_height():
    # 62 with no unit reads as mil (1.575 mm); 0.062" as inches.
    for text in ("PCB thickness: 62", 'Overall thickness 0.062" +/- 10%', "Board thickness 1.54 mm"):
        model = _run({"Fab.PCBDwf": _fab_doc([text])})
        assert _detail(model)["cross_checks"]["thickness"]["result"] == "match", text
    model = _run({"Fab.PCBDwf": _fab_doc(["PCB thickness: 62"])})
    assert _detail(model)["cross_checks"]["thickness"]["stated"][0]["unit_assumed"] is True
    # Without a unit, 0.2 reads as inches or mm: the reading nearest the stack is taken.
    thin = _run({"Fab.PCBDwf": _fab_doc(["PCB thickness: 0.2 +/- 10%"])},
                facts=_facts(total_mm=0.2, laminate_mm=0.18))
    (row,) = _detail(thin)["cross_checks"]["thickness"]["stated"]
    assert (row["stated_mm"], row["tolerance_mm"], row["result"]) == (0.2, 0.02, "match")
    # The tolerance's unit is the value's.
    unit = _run({"Fab.PCBDwf": _fab_doc(["Board thickness 1.6 +/- 0.1 mm"])})
    assert "unit_assumed" not in _detail(unit)["cross_checks"]["thickness"]["stated"][0]


def test_thickness_beside_other_thicknesses_and_tolerances_without_a_unit():
    model = _run({"Fab.PCBDwf": _fab_doc(["Final PCB thickness 1.6 +/- 0.16, copper thickness "
                                          "35 um", "Copper thickness 0.07 mm"])})
    (stated,) = _detail(model)["cross_checks"]["thickness"]["stated"]
    assert (stated["stated_mm"], stated["tolerance_mm"]) == (1.6, 0.16)
    tg = _run({"Fab.PCBDwf": _fab_doc(["FR-4, Tg: > 150 degC"])})
    assert "detail" not in _item(tg, "tg")


def test_trace_thickness_in_an_impedance_note_is_not_the_board_thickness():
    notes = ["Controlled impedance 50 ohm - thickness 0,17 mm, 100 ohm - thickness 0,1 mm"]
    model = _run({"Fab.PCBDwf": _fab_doc(notes)})
    assert _item(model, "thickness")["status"] == "missing"
    assert "thickness" not in _detail(model)["cross_checks"]


def test_contradicting_notes():
    notes = FAB_NOTES + ["Finish: HASL lead free.", "Final thickness 0.8 mm.",
                         "Accept per IPC-A-600 Class 3.", "Solder mask: blue."]
    model = _run({"Fab.PCBDwf": _fab_doc(notes)})
    (finding,) = _findings(model, "contradiction")
    assert finding["severity"] == "warning"
    what = {e["what"]: e["values"] for e in finding["examples"]}
    assert what == {"surface finish": ["ENIG", "LF-HASL"], "board thickness (mm)": [0.8, 1.6],
                    "IPC class": ["2", "3"], "mask colour": ["blue", "green"]}


def test_enig_with_gold_fingers_is_no_contradiction():
    model = _run({"Fab.PCBDwf": _fab_doc(FAB_NOTES + ["Hard gold on the edge fingers."])})
    assert not _findings(model, "contradiction")
    assert _item(model, "surface_finish")["detail"] == "ENIG, hard gold"


def test_materials_named_against_the_stack():
    model = _run({"Fab.PCBDwf": _fab_doc(["Material: Rogers RO4350B"])})
    (finding,) = _findings(model, "materials")
    assert finding["severity"] == "warning"
    assert "default dielectric FR-4" in finding["issue"]
    assert {e["named"] for e in finding["examples"]} == {"Rogers", "RO4350B"}
    matched = _run({"Fab.PCBDwf": _fab_doc(["Material: Rogers RO4350B"])},
                   facts=_facts(materials=["RO4350B"]))
    assert _detail(matched)["cross_checks"]["materials"]["result"] == "match"
    # FR-4 is a grade: a product name in the stack is one, unless it is a PTFE or polyimide.
    grade = _run({"Fab.PCBDwf": _fab_doc(["Material: FR4"])}, facts=_facts(materials=["IT-180A"]))
    assert _detail(grade)["cross_checks"]["materials"]["result"] == "match"
    ptfe = _run({"Fab.PCBDwf": _fab_doc(["Material: FR4"])}, facts=_facts(materials=["RO4350B"]))
    assert _detail(ptfe)["cross_checks"]["materials"]["result"] == "mismatch"
    # "isolation" is no Isola laminate.
    plain = _run({"Fab.PCBDwf": _fab_doc(["Continuity and isolation test."])})
    assert "materials" not in _detail(plain)["cross_checks"]


def test_copper_weight_far_from_the_stack():
    model = _run({"Fab.PCBDwf": _fab_doc(["Copper: 3 oz all layers."])})
    (finding,) = _findings(model, "copper_weight")
    assert finding["examples"][0]["stated_oz"] == 3.0
    assert finding["examples"][0]["stack_oz"] == [0.5, 1.0]
    half = _run({"Fab.PCBDwf": _fab_doc(["Inner copper 1/2 oz, outer ½ oz."])})
    assert _detail(half)["cross_checks"]["copper_weight"]["stated_oz"] == [0.5]


def test_impedance_targets_in_the_notes_against_the_profiles():
    notes = ["Controlled impedance: 50 ohm single-ended, 90 ohm differential, +/- 10%."]
    model = _run({"Fab.PCBDwf": _fab_doc(notes)})
    (finding,) = _findings(model, "impedance")
    assert finding["severity"] == "warning"
    assert [e["stated_ohm"] for e in finding["examples"]] == [90.0]
    assert _detail(model)["cross_checks"]["impedance"]["notes"]["lsm_targets_ohm"] == [50.0]
    # A resistor value outside an impedance note is not a target.
    doc = _doc([_note(FAB_NOTES), _note(["R5 is 100 ohm."], title="Rework:")] + FAB_VIEWS)
    assert not _findings(_run({"Fab.PCBDwf": doc}), "impedance")
    # Without profiles nothing contradicts the notes: info, not checked.
    bare = _run({"Fab.PCBDwf": _fab_doc(notes)}, facts=_facts(impedance=[]))
    (finding,) = _findings(bare, "impedance")
    assert finding["severity"] == "info" and "no impedance profiles" in finding["issue"]
    assert _detail(bare)["cross_checks"]["impedance"]["result"] == "not_checked"


def test_impedance_table_lines_against_the_profiles():
    line = {"id": "7", "layer": "Top Layer", "type": "Coated Microstrip", "target_ohm": 50.0,
            "tolerance": "10%", "calculated_ohm": 50.0, "width_mm": 0.25, "lower_width_mm": 0.25}
    pair = {"id": "8", "layer": "L3", "type": "Edge-Coupled Offset Stripline", "target_ohm": 100.0,
            "tolerance": "10%", "calculated_ohm": 100.0, "width_mm": 0.1, "lower_width_mm": 0.1,
            "gap_mm": 0.15}
    doc = _doc([_note(FAB_NOTES), _view("impedance table")] + FAB_VIEWS,
               impedance_lines=[line, pair])
    model = _run({"Fab.PCBDwf": doc})
    (finding,) = [f for f in _findings(model, "impedance") if "table" in f["issue"]]
    statuses = {(e["layer"], e["status"]) for e in finding["examples"]}
    assert statuses == {("Top Layer", "width or gap changed"),
                        ("L3", "not in the Layer Stack Manager")}
    changed = next(e for e in finding["examples"] if e["layer"] == "Top Layer")
    assert changed["table_width_mm"] == 0.25 and changed["lsm_width_mm"] == 0.3
    table = _detail(model)["cross_checks"]["impedance"]["table"]
    assert table == {"result": "mismatch", "lines": 2, "lsm_profile_layers": 1, "not_in_lsm": 1,
                     "changed": 1, "not_in_table": 0}
    current = _doc([_note(FAB_NOTES), _view("impedance table")] + FAB_VIEWS,
                   impedance_lines=[dict(line, width_mm=0.3)])
    assert _detail(_run({"Fab.PCBDwf": current}))["cross_checks"]["impedance"]["result"] == "match"
    # A pair's gap; a profile the table leaves out; only the lines the table shows.
    diff = {"profile": "D100", "differential": True, "target_ohm": 100.0, "tolerance_pct": 10.0,
            "layer": "L3", "width_mm": 0.1, "gap_mm": 0.2, "calculated_ohm": 100.0,
            "substack": None}
    notes = FAB_NOTES + ["100 ohm differential +/- 10%."]
    facts = _facts(impedance=[dict(PROFILE), diff])
    gap = _doc([_note(notes), _view("impedance table")] + FAB_VIEWS, impedance_lines=[pair])
    (finding,) = _findings(_run({"Fab.PCBDwf": gap}, facts=facts), "impedance")
    assert {(e["layer"], e["status"]) for e in finding["examples"]} == {
        ("L3", "width or gap changed"), ("Top Layer", "not in the table")}
    assert next(e for e in finding["examples"] if e["layer"] == "L3")["lsm_gap_mm"] == 0.2
    shown = _doc([_note(notes), _view("impedance table", options={"visible_lines": [7]})]
                 + FAB_VIEWS, impedance_lines=[dict(line, width_mm=0.3), pair])
    table = _detail(_run({"Fab.PCBDwf": shown}, facts=facts))["cross_checks"]["impedance"]["table"]
    assert table["result"] == "match" and table["lines"] == 1 and table["not_in_table"] == 0


def test_drill_table_against_the_board_holes():
    board = _facts(drills=[dict(BOARD_DRILLS[0], count=12), BOARD_DRILLS[1],
                           {"type": "Pad", "size_mm": 0.8, "plated": True, "slot_length_mm": 2.0,
                            "count": 2}])
    model = _run({"Fab.PCBDwf": _fab_doc()}, facts=board)
    (finding,) = _findings(model, "drill_table")
    assert finding["severity"] == "warning"
    assert finding["examples"] == [
        {"type": "Via", "size_mm": 0.3, "plated": True, "table_count": 10, "board_count": 12},
        {"type": "Pad", "size_mm": 3.2, "plated": False, "table_count": 2, "board_count": 0},
        {"type": "Pad", "size_mm": 0.8, "plated": True, "slot_length_mm": 2.0, "table_count": 0,
         "board_count": 2},
    ]
    # Rows of one size over two spans sum up; back drills are not in the board's table.
    split = DRILLS[:1] + [dict(DRILLS[1], count=1), dict(DRILLS[1], span="L2 - L3", count=3),
                          DRILLS[2], dict(DRILLS[0], backdrill=True, count=5)]
    assert _detail(_run({"Fab.PCBDwf": _fab_doc(drill_table=split)}))["cross_checks"][
        "drill_table"]["result"] == "match"
    panel = _run({"Fab.PCBDwf": _fab_doc()}, facts=_facts(embedded_boards=2))
    assert _detail(panel)["cross_checks"]["drill_table"]["result"] == "not_checked"
    empty = _run({"Fab.PCBDwf": _fab_doc(drill_table=[])})
    assert "no cached holes" in _findings(empty, "drill_table")[0]["issue"]


def test_layer_stack_legend_against_the_stack():
    six = STACK[:2] + [STACK[2], dict(STACK[3], layer="L2"), STACK[4], dict(STACK[3], layer="L4"),
                       STACK[4]] + STACK[5:]
    model = _run({"Fab.PCBDwf": _fab_doc(layer_stack=six)})
    (finding,) = _findings(model, "layer_stack_legend")
    what = {e["what"]: (e["legend"], e["stack"]) for e in finding["examples"]}
    assert what["copper layers"] == (5, 4)
    assert what["total thickness (mm)"][1] == 1.6
    # Materials between the outer coppers, as the stack's are read.
    rogers = [dict(row, material="RO4350B") if row["layer"] == "Core" else row for row in STACK]
    (finding,) = _findings(_run({"Fab.PCBDwf": _fab_doc(layer_stack=rogers)}), "layer_stack_legend")
    assert finding["examples"] == [{"what": "dielectric materials", "legend": ["FR-4", "RO4350B"],
                                    "stack": ["FR-4"]}]
    # A legacy stack has no thicknesses or materials to compare.
    legacy = _detail(_run({"Fab.PCBDwf": _fab_doc(layer_stack=rogers)}, facts=_facts(legacy=True)))
    assert legacy["cross_checks"]["layer_stack_legend"]["result"] == "not_checked"
    assert legacy["cross_checks"]["thickness"]["result"] == "not_checked"


# ---------- BOM table ----------

def _bom_doc(variant=None, source="Project", lines=None):
    lines = lines or [{"comment": "10k", "designators": ["R1", "R2"]},
                      {"comment": "100n", "designators": ["C1"]}]
    view = _view("bill of materials", variant=variant,
                 options={"data_source": source, "view_mode": "Base", "show_not_fitted": False})
    data = [{"data_source": source, "view_mode": "Base", "variant": None,
             "show_not_fitted": False, "lines": lines}]
    return _doc([_view("assembly view", "Top"), _view("assembly view", "Bottom"), view],
                layer_stack=[], drill_table=[], bom_data=data)


def _bom(parts):
    def read(variant):
        if variant not in ("[No Variations]", "Prod"):
            return {"error": "variant_not_found", "message": f"No variant '{variant}'."}
        return {"variant": variant, "parts": dict(parts)}
    return read


def test_bom_table_parts_against_the_bom():
    model = _run({"Asm.PCBDwf": _bom_doc()},
                 bom=_bom({"R1": "10k", "R2": "4k7", "C1": "100n", "C2": "1u"}))
    (finding,) = _findings(model, "bom_parts")
    assert finding["severity"] == "warning"
    assert finding["examples"] == [
        {"page": 1, "title": None, "in_bom_not_in_table": ["C2"]},
        {"page": 1, "title": None, "comment_differs": [
            {"designator": "R2", "table": "10k", "bom": "4k7"}]}]
    table = _detail(model, "Asm.PCBDwf")["cross_checks"]["bom_table"]["tables"][0]
    assert table["parts"]["table_parts"] == 3 and table["parts"]["bom_parts"] == 4
    same = _run({"Asm.PCBDwf": _bom_doc()}, bom=_bom({"R1": "10k", "R2": "10k", "C1": "100n"}))
    assert _detail(same, "Asm.PCBDwf")["cross_checks"]["bom_table"]["result"] == "match"
    assert not _findings(same, "bom_parts") and not _findings(same, "bom_variant")


def test_board_only_parts_of_a_plate_bom_are_info():
    lines = [{"comment": "10k", "designators": ["R1"]}, {"comment": "Fiducial", "designators": ["FID1"]}]
    model = _run({"Asm.PCBDwf": _bom_doc(source="Plate", lines=lines)}, bom=_bom({"R1": "10k"}))
    (finding,) = _findings(model, "bom_parts")
    assert finding["severity"] == "info"
    assert finding["examples"][0]["in_table_not_in_bom"] == ["FID1"]


def test_bom_parts_need_a_netlist():
    model = _run({"Asm.PCBDwf": _bom_doc()})
    assert {"check": "bom_parts", "reason": model["not_checked"][0]["reason"]} == \
        model["not_checked"][0]
    assert "no netlist" in model["not_checked"][0]["reason"]
    tables = _detail(model, "Asm.PCBDwf")["cross_checks"]["bom_table"]
    assert tables["result"] == "variant_only"
    assert tables["tables"][0]["parts"]["result"] == "not_checked"


def test_bom_table_variant_against_the_outjob_and_the_active_variant():
    outputs = [{"outjob": "Asm.OutJob", "output": "Assembly Drawing",
                "drawing": r"C:\Projects\Board\Asm.PCBDwf", "variant": "Prod", "connected": True}]
    model = _run({"Asm.PCBDwf": _bom_doc()}, outputs=outputs, active="Prod")
    warning, info = sorted(_findings(model, "bom_variant"), key=lambda f: f["severity"] != "warning")
    assert warning["severity"] == "warning" and warning["source"] == KB["bom_variant"]
    assert warning["examples"][0]["table_variant"] == "[No Variations]"
    assert warning["examples"][0]["outjob_variant"] == "Prod"
    assert info["severity"] == "info" and info["examples"][0]["active_variant"] == "Prod"
    assert _detail(model, "Asm.PCBDwf")["printed_by"] == [
        {"outjob": "Asm.OutJob", "output": "Assembly Drawing", "variant": "Prod", "connected": True}]
    # A view following the document ("Document") shows the drawing's variant.
    follows = _run({"Asm.PCBDwf": _bom_doc(variant="Document")}, outputs=outputs, active="Prod")
    assert _findings(follows, "bom_variant")[0]["examples"][0]["table_variant"] == "[No Variations]"
    own = _run({"Asm.PCBDwf": _bom_doc(variant="Prod")}, outputs=outputs, active="Prod")
    assert not _findings(own, "bom_variant")


def test_bom_variant_names_and_the_cached_set_a_table_prints():
    # An OutJob or project without variants is the table's [No Variations].
    for name in ("", None, "None", "[No Variations]"):
        outputs = [{"outjob": "Asm.OutJob", "output": "Assembly Drawing",
                    "drawing": r"C:\Projects\Board\Asm.PCBDwf", "variant": name, "connected": True}]
        assert not _findings(_run({"Asm.PCBDwf": _bom_doc()}, outputs=outputs), "bom_variant")
    # The cached set of another variant, when the table's has none, still has parts.
    doc = _bom_doc(variant="Prod")
    model = _run({"Asm.PCBDwf": doc}, bom=_bom({"R1": "10k", "R2": "10k", "C1": "100n"}))
    table = _detail(model, "Asm.PCBDwf")["cross_checks"]["bom_table"]["tables"][0]
    assert table["variant"] == "Prod" and table["cached_parts"] == 3
    assert table["parts"]["result"] == "match"


def test_bom_variant_mismatch_without_a_netlist_stays_a_mismatch():
    outputs = [{"outjob": "Asm.OutJob", "output": "Assembly Drawing",
                "drawing": r"C:\Projects\Board\Asm.PCBDwf", "variant": "Prod", "connected": True}]
    model = _run({"Asm.PCBDwf": _bom_doc()}, outputs=outputs)
    assert _findings(model, "bom_variant")[0]["severity"] == "warning"
    assert _detail(model, "Asm.PCBDwf")["cross_checks"]["bom_table"]["result"] == "mismatch"


def test_bom_table_showing_not_fitted_parts():
    lines = [{"comment": "10k", "designators": ["R1", "R2"]},
             {"comment": "100n", "designators": ["C1"]}, {"comment": "1u", "designators": ["C9"]}]

    def bom(variant):
        return {"variant": variant, "parts": {"R1": "10k", "R2": "10k", "C1": "100n"},
                "not_fitted": {"C9": "1u"}}
    shown = _bom_doc(lines=lines)
    shown.items[-1].options["show_not_fitted"] = True
    model = _run({"Asm.PCBDwf": shown}, bom=bom)
    assert not _findings(model, "bom_parts")
    assert _detail(model, "Asm.PCBDwf")["cross_checks"]["bom_table"]["result"] == "match"
    # Not shown: a not-fitted part in the table is from an older BOM.
    hidden = _run({"Asm.PCBDwf": _bom_doc(lines=lines)}, bom=bom)
    assert _findings(hidden, "bom_parts")[0]["examples"][0]["in_table_not_in_bom"] == ["C9"]


def test_fab_bom_reads_fitted_and_not_fitted_parts():
    from tests.test_bom import _netlist
    state = VariantState([VariantDefinition(name="Lite", dnp_refdes=["R2"])])
    read = main._fab_bom({"parameters": {}}, _netlist(), state)
    lite = read("Lite")
    assert lite["parts"] == {"C1": "100n", "R1": "10k", "R3": "1k"}
    assert lite["not_fitted"] == {"R2": "10k"}
    assert read("[No Variations]")["not_fitted"] == {}
    assert read("Nope")["error"] == "variant_not_found"
    assert main._fab_bom({}, None, state) is None


def test_bom_table_without_cached_parts():
    doc = _bom_doc()
    doc.bom_data = []
    model = _run({"Asm.PCBDwf": doc})
    (finding,) = _findings(model, "bom_parts")
    assert "no cached parts" in finding["issue"]


# ---------- source board, dates, errors ----------

def test_stale_and_foreign_drawings():
    later = _facts(saved=UPDATED_TS + 3 * 86400)
    model = _run({"Fab.PCBDwf": _fab_doc(source_pcb=r"D:\Old\Other.PcbDoc")}, facts=later)
    (stale,) = _findings(model, "stale")
    assert stale["severity"] == "warning" and "3 days" in stale["issue"]
    assert stale["examples"][0]["cached_views"] == ["drill table", "fabrication view",
                                                    "layer stack legend"]
    (source,) = _findings(model, "source_pcb")
    assert source["severity"] == "warning"
    copied = _run({"Fab.PCBDwf": _fab_doc(source_pcb=r"D:\Old\Board.PcbDoc")})
    assert _findings(copied, "source_pcb")[0]["severity"] == "info"
    assert _detail(copied)["cross_checks"]["stale"]["result"] == "current"


def test_stale_from_git_history():
    """In a clone, file times are the checkout's: the board's last commit counts, and a
    board committed with the drawing cannot be told apart."""
    def history(commits):
        return lambda path: commits.get(PureWindowsPath(path).name)
    later = {"Board.PcbDoc": {"time": UPDATED_TS + 86400, "basis": "last commit", "commit": "b"},
             "Fab.PCBDwf": {"time": UPDATED_TS + 60, "basis": "last commit", "commit": "a"}}
    model = build_fab_drawing(
        {"name": "Board", "pcb_doc_paths": [PCB], "pcb_dwf_paths": [r"C:\Projects\Board\Fab.PCBDwf"]},
        lambda path: _fab_doc(), lambda path: (_facts(saved=UPDATED_TS + 9e6), None), [], None,
        history=history(later))
    (stale,) = _findings(model, "stale")
    assert "The board was committed 24.0 hours after" in stale["issue"]
    assert _detail(model)["cross_checks"]["stale"]["board_changed_basis"] == "last commit"
    # The notes say how the board's time is read.
    assert any("last commit" in note and "Git clone" in note for note in model["notes"])
    together = dict(later, **{"Fab.PCBDwf": dict(later["Board.PcbDoc"])})
    model = build_fab_drawing(
        {"name": "Board", "pcb_doc_paths": [PCB], "pcb_dwf_paths": [r"C:\Projects\Board\Fab.PCBDwf"]},
        lambda path: _fab_doc(), lambda path: (_facts(), None), [], None,
        history=history(together))
    assert not _findings(model, "stale")
    assert "same commit" in _detail(model)["cross_checks"]["stale"]["note"]


def test_without_a_board_the_checklist_still_runs():
    model = _run({"Fab.PCBDwf": _fab_doc()}, facts=None)
    assert _item(model, "thickness")["status"] == "found"
    (missing,) = [n for n in model["not_checked"] if n["check"] == "design cross-checks"]
    assert missing["drawing"] == "Fab.PCBDwf"
    assert model["boards"] == [{"pcb_doc": "Board.PcbDoc", "error": "PCB document not found on disk"}]
    assert not [f for f in model["findings"] if f["severity"] == "error"]


def test_unreadable_drawing_and_document_filter():
    model = _run({"Bad.PCBDwf": ValueError("not an Altium Draftsman document"),
                  "Fab.PCBDwf": _fab_doc()})
    (finding,) = _findings(model, "unreadable")
    assert finding["severity"] == "error" and finding["drawing"] == "Bad.PCBDwf"
    one = _run({"Bad.PCBDwf": ValueError("x"), "Fab.PCBDwf": _fab_doc()}, document="fab")
    assert [d["drawing"] for d in one["details"]] == ["Fab.PCBDwf"]
    missing = _run({"Fab.PCBDwf": _fab_doc()}, document="nope")
    assert missing["error"] == "document_not_found" and missing["drawings"] == ["Fab.PCBDwf"]


def test_long_lines_are_quoted_around_the_match():
    line = "Note: " + "x" * 150 + " surface finish ENIG " + "y" * 150
    found = _item(_run({"Fab.PCBDwf": _fab_doc([line])}), "surface_finish")["found"][0]["text"]
    assert found.startswith("...") and found.endswith("...") and "ENIG" in found
    assert len(found) <= 110 + 6


def test_assembly_views_and_registration_edge_cases():
    # Sides unknown (no parts read): any assembly view will do.
    doc = _doc([_note(["Assemble per IPC-A-610 Class 2."]), _view("assembly view", "Top"),
                _view("bill of materials")], layer_stack=[], drill_table=[])
    model = _run({"Asm.PCBDwf": doc}, facts=_facts(parts={}))
    assert _item(model, "assembly_views", "Asm.PCBDwf") == {
        "item": "assembly_views", "status": "found", "detail": "views: top"}
    bare = _doc([_note(["Assemble per IPC-A-610 Class 2."]), _view("bill of materials"),
                 _view("bill of materials")], layer_stack=[], drill_table=[])
    assert _item(_run({"Asm.PCBDwf": bare}, facts=_facts(parts={})), "assembly_views",
                 "Asm.PCBDwf")["status"] == "missing"
    # Impedance and thickness tolerances are no registration or profile tolerances.
    notes = [n for n in FAB_NOTES if "position" not in n] + ["Board edge tolerance on impedance +/- 10%."]
    assert _item(_run({"Fab.PCBDwf": _fab_doc(notes)}), "registration_tolerances")["status"] == \
        "missing"


def test_a_drawing_is_checked_against_its_own_board():
    panel = r"C:\Projects\Board\Panel.PcbDoc"
    project = {"name": "Board", "pcb_doc_paths": [PCB, panel],
               "pcb_dwf_paths": [r"C:\Projects\Board\Fab.PCBDwf"]}
    read = []

    def read_board(path):
        read.append(PureWindowsPath(path).name)
        return _facts(pcb_doc=PureWindowsPath(path).name, path=path), None
    # Made from a same-named board in another folder: the project's board of that name.
    doc = _fab_doc(source_pcb=r"D:\Old\Panel.PcbDoc")
    model = build_fab_drawing(project, lambda path: doc, read_board, [], None)
    assert read == ["Panel.PcbDoc"] and _detail(model)["checked_against"] == "Panel.PcbDoc"
    assert _findings(model, "source_pcb")[0]["severity"] == "info"


def test_paging():
    docs = {f"D{i}.PCBDwf": _fab_doc(["Do not scale."]) for i in range(3)}
    model = _run(docs)
    first = page_fab_drawing(model, 0, 2, 1)
    assert first["has_more"] and first["next_offset"] == 2
    assert [d["drawing"] for d in first["details"]] == ["D0.PCBDwf", "D1.PCBDwf"]
    required = _findings(first, "required_items")[0]
    assert len(required["examples"]) == 1 and required["examples_shown"] == 1
    second = page_fab_drawing(model, 2, 2, 1)
    assert set(second) == {"project", "drawings_total", "drawing_kinds", "offset", "has_more",
                           "details"}


def test_checks_are_ranked():
    assert len(set(CHECKS)) == len(CHECKS)
    assert set(KB) <= set(CHECKS)


# ---------- the board's facts and file history ----------

def _index(stackup_source="v9", components=True):
    layers = [
        PcbLayer(None, "Top Overlay", "non-stack", 0),
        PcbLayer(None, "Top Solder", "dielectric", 1, diel_height_mil=0.4, material="Solder Resist"),
        PcbLayer(1, "Top Layer", "copper", 2, copper_thick_mil=1.4, v7_id=16842753),
        PcbLayer(None, "Core", "dielectric", 3, diel_height_mil=59.0, material="FR-4"),
        PcbLayer(32, "Bottom Layer", "copper", 4, copper_thick_mil=1.4, v7_id=16842784),
        PcbLayer(None, "Bottom Solder", "dielectric", 5, diel_height_mil=0.4,
                 material="Solder Resist"),
    ]

    def entry(v7, width, enabled=True, gap=None):
        return SimpleNamespace(enabled=enabled, layer_v7=v7, width_mil=width, gap_mil=gap,
                               calculated_ohm=50.2, substack_id="ABC")
    profiles = [SimpleNamespace(name="SE50", differential=False, target_ohm=50.0,
                                tolerance_pct=10.0,
                                layers=[entry(16842753, 5.0), entry(16842784, 5.0, enabled=False)])]
    board = SimpleNamespace(stackup_source=stackup_source, layers=layers,
                            substacks=[{"id": "abc", "name": "Board", "is_flex": False}],
                            impedance_profiles=profiles)
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    if components:
        db.execute("CREATE TABLE components (side TEXT)")
        db.executemany("INSERT INTO components VALUES (?)",
                       [("Top",), ("Top",), ("Top",), ("Bottom",), (None,)])
    return SimpleNamespace(pcb=SimpleNamespace(board=board, embedded_boards=0), db=db)


def test_board_facts_of_a_pcb_index(tmp_path):
    path = tmp_path / "Board.PcbDoc"
    path.write_bytes(b"x")
    drills = [{"kind": "via", "hole_mil": 11.811, "plated": True, "span": "Top - Bottom", "count": 10},
              {"kind": "bare_hole", "hole_mil": 125.984, "plated": False, "span": None, "count": 2},
              {"kind": "pad", "hole_mil": 39.37, "slot_length_mil": 78.74, "plated": True,
               "span": "Top - Bottom", "count": 2}]
    facts = board_facts(_index(), drills, str(path))
    assert facts.pcb_doc == "Board.PcbDoc" and facts.saved is not None and not facts.legacy
    assert facts.copper_layers == 2 and facts.materials == ["FR-4"]
    # The total counts the solder mask, copper to copper does not.
    assert round(facts.total_mm, 4) == round(62.6 * 0.0254, 4)
    assert round(facts.laminate_mm, 4) == round(61.8 * 0.0254, 4)
    assert facts.impedance == [{"profile": "SE50", "differential": False, "target_ohm": 50.0,
                                "tolerance_pct": 10.0, "layer": "Top Layer", "width_mm": 0.127,
                                "gap_mm": None, "calculated_ohm": 50.2, "substack": "Board"}]
    assert facts.drills == [
        {"type": "Via", "size_mm": 0.3, "plated": True, "slot_length_mm": None, "count": 10},
        {"type": "Pad", "size_mm": 3.2, "plated": False, "slot_length_mm": None, "count": 2},
        {"type": "Pad", "size_mm": 1.0, "plated": True, "slot_length_mm": 2.0, "count": 2}]
    assert facts.parts == {"top": 3, "bottom": 1}
    assert facts.summary() == {
        "pcb_doc": "Board.PcbDoc", "copper_layers": 2, "stack_total_mm": 1.59,
        "copper_to_copper_mm": 1.57, "copper_oz": [1.0], "dielectric_materials": ["FR-4"],
        "flex": False, "impedance_targets_ohm": ["50 single-ended"]}
    legacy = board_facts(_index("legacy", components=False), None, str(tmp_path / "Gone.PcbDoc"))
    assert legacy.legacy and legacy.total_mm is None and legacy.drills is None
    assert legacy.saved is None and legacy.parts == {} and legacy.copper_layers == 2
    assert legacy.summary()["stack"].startswith("legacy layer stack")


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_file_history_in_and_outside_a_git_clone(tmp_path, monkeypatch):
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.com")
        monkeypatch.setenv(f"GIT_{role}_DATE", "2026-01-02T03:04:05Z")
    clone = tmp_path / "clone"
    clone.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=clone, check=True, capture_output=True)
    git("init", "-q")
    (clone / "Board.PcbDoc").write_text("v1", encoding="utf-8")
    git("add", "Board.PcbDoc")
    git("commit", "-q", "-m", "board")
    # A checkout writes every file: a clean file has its last commit's time.
    clean = file_history(str(clone / "Board.PcbDoc"))
    assert clean["basis"] == "last commit" and len(clean["commit"]) == 40
    assert clean["time"] == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc).timestamp()
    (clone / "Board.PcbDoc").write_text("v2", encoding="utf-8")
    edited = file_history(str(clone / "Board.PcbDoc"))
    assert edited["basis"] == "file time (changed since its last commit)" and not edited["commit"]
    plain = tmp_path / "copy" / "Board.PcbDoc"
    plain.parent.mkdir()
    plain.write_text("v1", encoding="utf-8")
    assert file_history(str(plain))["basis"] == "file time"
    assert file_history(str(tmp_path / "missing.PcbDoc")) is None


# ---------- OutJob drawings ----------

def test_draftsman_outputs_name_the_drawing_and_variant(tmp_path):
    outputs = [_output("PCBDrawing", "Fab Drawing", category="Documentation", doc="Fab.PCBDwf",
                       variant="Prod"),
               _output("Gerber", "Gerber Files")]
    project = _project(tmp_path, {"Fab.OutJob": _outjob(outputs)},
                       documents=("a.SchDoc", "Board.PcbDoc", "Fab.PCBDwf"))
    (row,) = draftsman_outputs(project)
    assert row["outjob"] == "Fab.OutJob" and row["output"] == "Fab Drawing"
    assert PureWindowsPath(row["drawing"]).name == "Fab.PCBDwf"
    assert row["variant"] == "Prod" and row["connected"] is True


# ---------- the parser's caches ----------

_NS = ('<Document xmlns="http://schemas.datacontract.org/2004/07/Altium.Designer.PcbDrawing.'
       'DataSerialization.V1" xmlns:i="http://www.w3.org/2001/XMLSchema-instance">')
_DIU = 96 / 25.4


def _stack_layer(name, goal, material, thickness_mm, weight="", tg=""):
    return (f"<StackLayerBoardLayerData><Comment/><GlassTransTemp>{tg}</GlassTransTemp>"
            f"<GoalType>{goal}</GoalType><IdInBoard>1</IdInBoard><Material>{material}</Material>"
            f"<Name>{name}</Name><Note/><Thickness>{thickness_mm * _DIU}</Thickness>"
            f"<Weight>{weight}</Weight><Dk>4.2</Dk></StackLayerBoardLayerData>")


def _component(designator, comment):
    return (f"<BoardComponent><Comment>{comment}</Comment><ComponentKind>Standard</ComponentKind>"
            f"<Designator>{designator}</Designator><NamedParameters><DisplayValues>"
            "<Designator>not this one</Designator></DisplayValues></NamedParameters>"
            "<Side>Top</Side></BoardComponent>")


def _drawing_xml() -> str:
    line = ("<TransmissionLineData><CalculatedImpedance>89.6</CalculatedImpedance>"
            f"<Clearance>0</Clearance><Gap>{0.15 * _DIU}</Gap><Id>3</Id>"
            "<LineType>Edge-Coupled Coated Microstrip</LineType>"
            f"<LowerTraceWidth>{0.11 * _DIU}</LowerTraceWidth>"
            "<ReferenceLayers><string>L2</string></ReferenceLayers><SubStack>Board</SubStack>"
            "<TargetImpedance>90</TargetImpedance><TargetTolerance>10%</TargetTolerance>"
            f"<TraceLayer>Top Layer</TraceLayer><UpperTraceWidth>{0.1 * _DIU}</UpperTraceWidth>"
            "</TransmissionLineData>")
    bom_set = ("<BomDataSet><ColumnsAliases/><Options><DataSource>Project</DataSource>"
               "<ShowNotFitted>false</ShowNotFitted><Variant>Prod</Variant><VariantV2/>"
               "<ViewMode>Base</ViewMode></Options><Rows>"
               f"<BomGroup><Components>{_component('R1', '10k')}{_component('R2', '10k')}"
               "</Components><NamedParameters/></BomGroup>"
               f"<BomGroup><Components>{_component('C1', 'A &amp; B')}</Components></BomGroup>"
               "</Rows></BomDataSet>")
    return (
        _NS + "<BillOfMaterialsInformation><BoardComponents/><BomData2>" + bom_set
        + "</BomData2></BillOfMaterialsInformation>"
        "<DocumentOptions><AssemblyVariantV2>Prod</AssemblyVariantV2></DocumentOptions>"
        f"<DrillTableInformation><HolesV2>{_hole(0.3, True, 'Via')}</HolesV2></DrillTableInformation>"
        "<LastUpdate>2026-10-01T12:00:00.0000000Z</LastUpdate>"
        "<LayerStackInformation><BoardLayerStackData><BoardLayerStack><IsFlex>true</IsFlex>"
        "<Name>Board</Name><Stack>"
        + _stack_layer("Top Layer", "Signal", "Copper", 0.035, weight="1oz")
        + _stack_layer("Core", "Dielectric", "FR-4", 1.5, tg="170")
        + "</Stack></BoardLayerStack></BoardLayerStackData>"
        f"<TransmissionLineStructure><Lines>{line}</Lines></TransmissionLineStructure>"
        "</LayerStackInformation><Pages><Page><Items>"
        '<Item i:type="BillOfMaterialsView"><Id>5</Id><CurrentVariation><Variant>Document</Variant>'
        "</CurrentVariation><DataSourceKind>Project</DataSourceKind><ShowNotFitted>false</ShowNotFitted>"
        "<Title>BOM</Title><ViewMode>Base</ViewMode><ViewSide>All</ViewSide></Item>"
        '<Item i:type="TransmissionLineTableData"><Id>6</Id><Title>Impedance</Title>'
        "<VisibleLines><int>3</int></VisibleLines></Item>"
        "</Items><StandardSizeName>A4</StandardSizeName></Page></Pages>"
        f"<Parameters>{_param('Revision', 'B')}</Parameters>"
        "<SourceDocumentName>C:\\Projects\\Board\\Board.PcbDoc</SourceDocumentName></Document>"
    )


def test_bom_line_comment_ties_keep_the_first():
    from parsers.pcb_dwf import _bom_data
    groups = "".join(f"<BomGroup><Components>{_component(f'R{n}', f'x{n}')}"
                     f"{_component(f'S{n}', f'y{n}')}</Components></BomGroup>" for n in range(24))
    xml = (f"<BomData2><BomDataSet><Options><DataSource>Project</DataSource></Options><Rows>"
           f"{groups}</Rows></BomDataSet></BomData2>")
    (data,) = _bom_data(bytearray(xml.encode("utf-8")))
    # Not a set's order, which changes from run to run.
    assert [line["comment"] for line in data["lines"]] == [f"x{n}" for n in range(24)]


def test_parser_reads_the_caches_the_checks_use(tmp_path):
    path = tmp_path / "Fab.PCBDwf"
    path.write_bytes(lz4_stream(_drawing_xml().encode("utf-8"), chunk_size=512))
    doc = parse_pcb_dwf(str(path))
    assert doc.flex is True and doc.document_variant == "Prod"
    assert doc.layer_stack == [
        {"layer": "Top Layer", "type": "Signal", "material": "Copper", "weight": "1oz",
         "thickness_mm": 0.035, "dk": 4.2},
        {"layer": "Core", "type": "Dielectric", "material": "FR-4", "tg": "170",
         "thickness_mm": 1.5, "dk": 4.2}]
    assert doc.impedance_lines == [{
        "id": "3", "layer": "Top Layer", "type": "Edge-Coupled Coated Microstrip",
        "target_ohm": 90.0, "tolerance": "10%", "calculated_ohm": 89.6, "width_mm": 0.1,
        "lower_width_mm": 0.11, "gap_mm": 0.15, "substack": "Board", "reference_layers": ["L2"]}]
    assert doc.bom_data == [{
        "data_source": "Project", "view_mode": "Base", "variant": "Prod", "show_not_fitted": False,
        "lines": [{"comment": "10k", "designators": ["R1", "R2"]},
                  {"comment": "A & B", "designators": ["C1"]}]}]
    bom, table = [i for i in doc.items if i.kind == "view"]
    assert (bom.view, bom.variant, bom.options) == (
        "bill of materials", "Document",
        {"data_source": "Project", "view_mode": "Base", "show_not_fitted": False})
    assert (table.view, table.options) == ("impedance table", {"visible_lines": [3]})


# ---------- the tool ----------

def test_tool_needs_a_project():
    with patch("main._project", None):
        assert json.loads(main.check_fab_drawing())["error"] == "no_project"


def test_tool_renders():
    project = {"name": "Board", "prj_pcb_path": r"C:\nowhere\Board.PrjPcb",
               "pcb_doc_paths": [PCB], "pcb_dwf_paths": [r"C:\Projects\Board\Fab.PCBDwf"]}
    cache = main.DesignNotesCache()
    with patch("main._project", project), \
         patch("main._altium") as altium, \
         patch("main._variant_state", VariantState([VariantDefinition(name="Prod")])), \
         patch.object(cache, "draftsman", lambda path: _fab_doc()), \
         patch("main._design_notes", cache), \
         patch("main._fab_board_reader", lambda project, netlist: lambda path: (_facts(), None)):
        altium._netlist = None
        result = json.loads(main.check_fab_drawing())
        assert result["drawings_total"] == 1 and result["active_variant"] == "Prod"
        assert result["details"][0]["kind"] == "fabrication"
        assert result["not_checked"][0]["check"] == "bom_parts"
        error = json.loads(main.check_fab_drawing(document="x"))
        assert error["error"] == "document_not_found"


def test_fab_board_reader(tmp_path):
    first, other, gone = (str(tmp_path / f"{n}.PcbDoc") for n in ("Board", "Other", "Gone"))
    Path(other).write_bytes(b"x")
    project = {"pcb_doc_paths": [first, other, gone]}
    drills = [{"kind": "via", "hole_mil": 11.811, "plated": True, "count": 10}]
    with patch.object(main._pcb_session, "get", return_value=(_index(), None)), \
         patch("main.review_dfm", return_value={"drills": drills}) as dfm, \
         patch("main.parse_pcb_doc", return_value=None), \
         patch("main.PcbIndex", return_value=_index("legacy")):
        read = main._fab_board_reader(project, None)
        facts, error = read(first)
        assert error is None and not facts.legacy and facts.drills[0]["type"] == "Via"
        assert dfm.call_args[0][2] == ["drill_count"]
        # Another board of the project is parsed for the call; a missing one says so.
        assert read(other)[0].legacy and read(other)[0].pcb_doc == "Other.PcbDoc"
        assert read(gone) == (None, "PCB document not found on disk: Gone.PcbDoc")
    with patch.object(main._pcb_session, "get", return_value=(None, {"message": "no PCB"})):
        assert main._fab_board_reader(project, None)(first) == (None, "no PCB")
    with patch.object(main._pcb_session, "get", return_value=(_index(), None)), \
         patch("main.review_dfm", side_effect=RuntimeError("boom")):
        facts, _ = main._fab_board_reader(project, None)(first)
        assert facts.drills is None  # only the drill-table cross-check is lost


def test_instructions_name_the_tool_after_the_cut():
    assert main.SERVER_INSTRUCTIONS.index("check_fab_drawing") > 4096


# ---------- Altium's public examples ----------

@pytest.fixture(scope="module")
def minipc():
    prj = EXAMPLES / "Mini PC" / "Mini PC - Motherboard" / "MiniPC.PrjPcb"
    if not prj.is_file():
        pytest.skip("Altium's Mini PC example is not installed")
    from parsers.pcb_doc import parse_pcb_doc
    from parsers.prj_pcb import parse_prj_pcb
    from services.dfm import review_dfm
    from services.fab_drawing import board_facts
    from services.fab_profiles import load_profile
    from services.pcb_index import PcbIndex
    data = parse_prj_pcb(str(prj))
    project = {"name": "MiniPC", "prj_pcb_path": str(prj), "pcb_doc_paths": data.pcb_doc_paths,
               "pcb_dwf_paths": data.pcb_dwf_paths}

    def read_board(path):
        index = PcbIndex(parse_pcb_doc(path), None)
        drills = review_dfm(index, load_profile("none", None)[0], ["drill_count"])["drills"]
        return board_facts(index, drills, path), None
    return build_fab_drawing(project, main.DesignNotesCache().draftsman, read_board,
                             draftsman_outputs(project), "Default")


def test_minipc_fab_drawing(minipc):
    detail = _detail(minipc, "MiniPC_Drill.PCBDwf")
    assert detail["kind"] == "fabrication"
    statuses = {i["item"]: i["status"] for i in detail["checklist"]}
    assert [k for k, v in statuses.items() if v == "missing"] == ["tg", "thickness"]
    assert _item(minipc, "ipc_class", "MiniPC_Drill.PCBDwf")["detail"] == "class 2"
    assert _item(minipc, "surface_finish", "MiniPC_Drill.PCBDwf")["detail"] == "ENIG"
    assert _item(minipc, "mask_colour", "MiniPC_Drill.PCBDwf")["detail"] == "blue"
    assert _item(minipc, "silkscreen_colour", "MiniPC_Drill.PCBDwf")["detail"] == "white"
    cross = {k: v["result"] for k, v in detail["cross_checks"].items()}
    # The drill table and the legend match the board; the notes' impedance targets cannot
    # be confirmed (the Layer Stack Manager has no profiles: info, not a contradiction);
    # the drawing was made from the AD22 copy.
    assert cross["drill_table"] == "match" and cross["layer_stack_legend"] == "match"
    assert cross["materials"] == "match" and cross["impedance"] == "not_checked"
    assert detail["cross_checks"]["impedance"]["notes"]["stated_ohm"] == [40, 50, 80, 85, 100]
    assert {f["severity"] for f in minipc["findings"] if f["check"] == "impedance"} == {"info"}
    assert detail["cross_checks"]["source_pcb"]["same_name_elsewhere"] is True
    assert _detail(minipc, "MiniPC_board_assembly.PCBDwf")["kind"] == "assembly"
    assert not [f for f in minipc["findings"] if f["severity"] == "error"]
