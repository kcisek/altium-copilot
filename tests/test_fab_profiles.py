"""Fab profiles for check_dfm (services/fab_profiles): the built-in Altimade and none
profiles, profile files in the project folder, the limits argument, and the echo."""

import json
import os
import shutil
from pathlib import Path

import pytest

from server.services.fab_profiles import (
    _MAX_MM, _MIN_MM, ALTIMADE, ALTIMADE_URL, DEFAULTS, FALLBACKS, FILES_LISTED,
    HINT_FLOOR_MIL, LIMIT_KEYS, MAX_FILE_BYTES, MM, SERVICE_LIMITS, VALID_KEYS,
    builtin_profile, external_key, load_profile, parse_limits,
)

TEMPLATE = Path(__file__).parent.parent / "examples" / "fab_profile.json"


def _write(folder: Path, name: str, data, raw: bytes | None = None) -> Path:
    path = folder / name
    path.write_bytes(raw if raw is not None else json.dumps(data).encode())
    return path


def _ok(*args, **kwargs):
    profile, error = load_profile(*args, **kwargs)
    assert error is None, error
    return profile


def _error(*args, **kwargs) -> dict:
    profile, error = load_profile(*args, **kwargs)
    assert profile is None
    return error


# ---------- vocabulary ----------

def test_vocabulary_suffixes_lengths_only():
    assert "min_trace_mm" in VALID_KEYS and "min_trace_mil" in VALID_KEYS
    assert "min_trace" not in VALID_KEYS
    assert "max_board_size_mm" in VALID_KEYS and "min_board_size_mil" in VALID_KEYS
    for bare in ("max_aspect_ratio", "max_microvia_aspect_ratio", "tolerance_percent",
                 "layer_counts", "max_drill_sizes", "copper_oz"):
        assert bare in VALID_KEYS
        assert f"{bare}_mm" not in VALID_KEYS
    assert len(VALID_KEYS) == len(set(VALID_KEYS))
    assert all(a in LIMIT_KEYS and b in LIMIT_KEYS for a, b in FALLBACKS.items())
    assert not set(FALLBACKS.values()) & set(FALLBACKS)  # one step, no chains
    assert set(SERVICE_LIMITS) <= set(LIMIT_KEYS) and set(DEFAULTS) <= set(LIMIT_KEYS)
    assert external_key("min_trace", True) == "min_trace_mm"
    assert external_key("min_trace", False) == "min_trace_mil"
    assert external_key("max_aspect_ratio", True) == "max_aspect_ratio"
    # Every length and board size has both plausibility bounds, the lower one below.
    suffixed = {name for name, kind in LIMIT_KEYS.items() if kind in ("length", "size")}
    assert set(_MAX_MM) == set(_MIN_MM) == suffixed
    assert all(0 < _MIN_MM[name] < _MAX_MM[name] for name in suffixed)
    assert len(VALID_KEYS) == len(LIMIT_KEYS) + len(suffixed)  # _mm and _mil each


def test_hole_to_copper_and_hole_to_edge_keys():
    for name, generic in (("min_hole_to_copper", "min_spacing"),
                          ("min_hole_to_edge", "min_copper_to_edge")):
        assert LIMIT_KEYS[name] == "length" and FALLBACKS[name] == generic
        assert f"{name}_mm" in VALID_KEYS and f"{name}_mil" in VALID_KEYS
        # Altimade publishes neither: they follow its generic limit.
        altimade = builtin_profile("altimade")
        assert altimade.resolved(name) == generic
        assert altimade.get(name) == altimade.get(generic)
        assert altimade.published(name) == altimade.published(generic)
        # Set, they win; null stops the fallback.
        own = _ok("altimade", None, {f"{name}_mm": 0.3})
        assert own.get(name) == pytest.approx(0.3 * MM) and own.origin(name) == "argument"
        assert own.get(generic) == altimade.get(generic)
        rows = {row["limit"]: row for row in own.echo(True)["limits"]}
        assert rows[f"{name}_mm"] == {"limit": f"{name}_mm", "value_mm": 0.3,
                                      "origin": "argument"}
        removed = _ok("altimade", None, {f"{name}_mil": None})
        assert removed.get(name) is None and removed.is_set(generic)
        # Not echoed when only the fallback supplies it.
        assert f"{name}_mm" not in {row["limit"] for row in altimade.echo(True)["limits"]}


# ---------- built-ins ----------

def test_altimade_values_published_and_sources():
    profile = builtin_profile("altimade")
    assert profile.name == "Altimade" and profile.source == ALTIMADE_URL
    assert profile.source_updated == "2026-10-13"
    expected_mm = {"min_trace": 0.075, "min_spacing": 0.127, "min_annular_ring": 0.1016,
                   "min_copper_to_edge": 0.254, "min_drill": 0.101, "min_route_size": 1.0}
    for name, mm in expected_mm.items():
        assert profile.get(name) == pytest.approx(mm * MM)
        assert profile.origin(name) == "builtin"
        assert profile.source_of(name) == ALTIMADE_URL
    assert profile.get("max_aspect_ratio") == 10.0
    assert profile.get("layer_counts") == (2, 4, 6, 8)
    assert profile.get("min_board_size") == pytest.approx((100.0, 100.0))
    assert profile.get("max_board_size") == pytest.approx((14880.0, 14880.0))
    assert profile.get("copper_oz") == 1.0
    assert profile.published("min_trace") == "2.955 mil (0.075mm)"
    assert profile.published("min_annular_ring") == "4 mil (0.1016mm)"
    assert profile.published("min_drill") == "3.995 mil (0.101mm)"
    assert profile.published("max_aspect_ratio") == "10"
    assert profile.published("max_board_size") == "0.10 - 14.88 in (2.54 - 377.952mm)"
    # Not published by Altimade: the checks do not run.
    for name in ("max_drill", "min_hole_to_hole", "min_pad_hole_to_hole",
                 "max_microvia_aspect_ratio", "max_drill_sizes"):
        assert profile.get(name) is None and not profile.is_set(name)
        assert profile.origin(name) is None and profile.published(name) is None
    assert "board_thickness" in profile.not_checked
    assert set(ALTIMADE["limits"]) <= set(LIMIT_KEYS)


def test_specific_limits_fall_back_to_the_generic_one():
    profile = builtin_profile("altimade")
    for specific in ("min_via_drill", "min_pth_drill", "min_npth_drill"):
        assert profile.resolved(specific) == "min_drill"
        assert profile.get(specific) == profile.get("min_drill")
        assert profile.origin(specific) == "builtin"
        assert profile.published(specific) == "3.995 mil (0.101mm)"
    assert profile.get("min_via_annular_ring") == profile.get("min_annular_ring")
    assert profile.get("min_pth_annular_ring") == profile.get("min_annular_ring")
    with pytest.raises(KeyError):
        profile.get("min_slot_width")


def test_none_profile_and_defaults():
    profile = _ok("NONE", None)
    assert profile.key == "none" and profile.chosen_by == "argument"
    assert profile.effective() == {}
    assert profile.get("tolerance_percent") == 2.0 and profile.get("drill_allowance") == 0.0
    assert not profile.is_set("tolerance_percent")
    assert profile.origin("tolerance_percent") == "default"
    assert profile.not_checked == {}
    assert profile.scope == "Profile none and no limits: no fabricator limit is checked."
    profile = _ok("none", None, {"min_spacing_mm": 0.1, "max_aspect_ratio": 8})
    assert profile.get("min_spacing") == pytest.approx(0.1 * MM)
    assert profile.get("min_trace") is None and profile.get("layer_counts") is None
    assert profile.scope == "Checked only against the limits argument (2 limits, profile none)."


# ---------- resolution ----------

def test_default_is_altimade_without_a_project_file(tmp_path):
    for root in (None, tmp_path):
        profile = _ok(None, root)
        assert profile.key == "altimade" and profile.chosen_by == "default"
        assert "(default: no fabricator chosen)" in profile.scope
    assert _ok("  ", tmp_path).chosen_by == "default"
    (tmp_path / "fab_profile.json").mkdir()  # a folder is not a profile
    assert _ok(None, tmp_path).chosen_by == "default"


def test_project_file_is_the_default(tmp_path):
    _write(tmp_path, "fab_profile.json", {"name": "Acme", "limits": {"min_trace_mm": 0.1}})
    profile = _ok(None, tmp_path)
    assert profile.chosen_by == "project_file" and profile.file == "fab_profile.json"
    assert profile.name == "Acme" and profile.origin("min_trace") == "file"
    assert profile.scope == "Checked against the limits in the project's fab_profile.json."
    # A built-in named explicitly wins over the project file.
    assert _ok("altimade", tmp_path).chosen_by == "argument"


def test_named_file_with_or_without_extension(tmp_path):
    _write(tmp_path, "acme std.json", {"limits": {"min_spacing_mil": 4}})
    for name in ("acme std", "acme std.json", " acme std "):
        profile = _ok(name, tmp_path)
        assert profile.key == "acme std.json" and profile.chosen_by == "argument"
        assert profile.name == "acme std"  # the file stem when name is absent
        assert profile.get("min_spacing") == 4.0
    assert _ok("Altimade", tmp_path).key == "altimade"


def test_profile_not_found_lists_builtins_files_and_hint(tmp_path):
    _write(tmp_path, "acme.json", {})
    _write(tmp_path, "notes.txt", {})
    error = _error("jlcpcb", tmp_path)
    assert error["error"] == "profile_not_found"
    assert error["builtin"] == ["altimade", "none"]
    assert error["files"] == ["acme.json"] and error["files_total"] == 1
    assert "Never substitute another profile" in error["hint"]
    assert "profile='none'" in error["hint"]
    assert "jlcpcb.json" in error["message"]
    error = _error("jlcpcb", None)
    assert error["error"] == "profile_not_found" and error["files"] == []
    assert error["files_total"] == 0


def test_profile_not_found_says_when_the_file_list_is_cut(tmp_path):
    for number in range(25):
        _write(tmp_path, f"f{number:02d}.json", {})
    _write(tmp_path, "zz_fab.json", {})
    _write(tmp_path, "Acme Profile.json", {})
    error = _error("jlcpcb", tmp_path)
    assert len(error["files"]) == FILES_LISTED and error["files_total"] == 27
    # Names that look like fab profiles come first, so the cut keeps them.
    assert error["files"][:3] == ["Acme Profile.json", "zz_fab.json", "f00.json"]


@pytest.mark.parametrize("name", [
    "D:x.json", "C:x.json", "x.json:ads", "x:ads", "..\\x.json", "../x.json", "sub\\x.json",
    "sub/x.json", "..", ".x.json", "CON.json", "con", "nul.JSON", "COM1.json", "lpt9",
    "CON .json", "a" * 90, "x\n",
])
def test_profile_names_cannot_leave_the_project_folder(tmp_path, name):
    root = tmp_path / "project"
    (root / "sub").mkdir(parents=True)
    _write(tmp_path, "x.json", {"limits": {}})
    _write(root / "sub", "x.json", {"limits": {}})
    error = _error(name, root)
    assert error["error"] == "profile_not_found"


@pytest.mark.parametrize("name", ["CON", "aux.txt", "NUL.json", "COM1", "lpt9", "CON .json"])
def test_device_names_are_refused_as_device_names(tmp_path, name):
    message = _error(name, tmp_path)["message"]
    assert "Windows device name" in message and "letters, digits" not in message


def test_folder_named_like_a_profile_is_not_one(tmp_path):
    (tmp_path / "acme.json").mkdir()
    assert _error("acme", tmp_path)["error"] == "profile_not_found"


def test_link_out_of_the_project_folder_is_refused(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = _write(tmp_path, "outside.json", {"limits": {"min_trace_mm": 0.1}})
    try:
        os.symlink(outside, root / "acme.json")
        os.symlink(outside, root / "fab_profile.json")
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links not available")
    for name in ("acme", None):
        error = _error(name, root)
        assert error["error"] == "invalid_profile" and "project folder" in error["message"]


def test_file_resolving_outside_the_project_folder_is_refused(tmp_path, monkeypatch):
    # The same check without symbolic links: a path that resolves elsewhere.
    root = tmp_path / "project"
    root.mkdir()
    _write(root, "acme.json", {"limits": {}})
    outside = _write(tmp_path, "outside.json", {"limits": {}})
    resolve = Path.resolve

    def fake_resolve(self, strict=False):
        return outside if self.name == "acme.json" else resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", fake_resolve)
    error = _error("acme", root)
    assert error["error"] == "invalid_profile" and "project folder" in error["message"]


# ---------- file format ----------

def test_based_on_absent_inherits_nothing(tmp_path):
    _write(tmp_path, "a.json", {"limits": {"min_trace_mm": 0.09}})
    profile = _ok("a", tmp_path)
    assert profile.based_on is None and profile.get("min_spacing") is None
    assert profile.get("layer_counts") is None and profile.not_checked == {}
    _write(tmp_path, "b.json", {"based_on": "none", "limits": {}})
    assert _ok("b", tmp_path).effective() == {}


def test_based_on_altimade_inherits(tmp_path):
    _write(tmp_path, "a.json", {"based_on": "Altimade",
                                "limits": {"min_trace_mm": 0.09, "min_via_drill_mm": 0.2}})
    profile = _ok("a", tmp_path)
    assert profile.based_on == "altimade"
    assert profile.get("min_trace") == pytest.approx(0.09 * MM)
    assert profile.origin("min_trace") == "file" and profile.published("min_trace") is None
    assert profile.get("min_spacing") == pytest.approx(0.127 * MM)
    assert profile.origin("min_spacing") == "builtin"
    assert profile.get("min_via_drill") == pytest.approx(0.2 * MM)
    assert profile.get("min_pth_drill") == pytest.approx(0.101 * MM)  # generic, inherited
    # Altimade's not_checked texts speak of ordering from Altimade: built-in only.
    assert profile.not_checked == {}
    assert "board_thickness" in builtin_profile("altimade").not_checked
    assert profile.scope == ("Checked against the limits in the profile file a.json (based "
                             "on Altimade's published limits).")
    _write(tmp_path, "c.json", {"based_on": "acme"})
    error = _error("c", tmp_path)
    assert error["error"] == "invalid_profile" and "based_on" in error["message"]


def test_null_removes_and_stops_the_fallback(tmp_path):
    _write(tmp_path, "a.json", {"based_on": "altimade", "limits": {
        "min_spacing_mm": None, "min_npth_drill_mm": None, "layer_counts": None}})
    profile = _ok("a", tmp_path)
    assert profile.get("min_spacing") is None and profile.origin("min_spacing") is None
    assert profile.get("layer_counts") is None
    assert profile.get("min_npth_drill") is None and not profile.is_set("min_npth_drill")
    assert profile.get("min_via_drill") == pytest.approx(0.101 * MM)
    rows = {row["limit"]: row for row in profile.echo(True)["limits"]}
    assert rows["min_spacing_mm"]["value_mm"] is None
    assert rows["min_spacing_mm"]["origin"] == "file"
    assert rows["layer_counts"]["value"] is None


def test_underscore_keys_and_comment_are_ignored(tmp_path):
    _write(tmp_path, "a.json", {
        "comment": ["anything"], "_note": 1, "_limits": "x",
        "limits": {"comment": "x", "_min_trace": "x", "_x_mm": None, "min_trace_mil": 3}})
    assert _ok("a", tmp_path).effective() == {"min_trace": 3.0}


def test_unknown_keys_list_the_valid_keys(tmp_path):
    _write(tmp_path, "a.json", {"nmae": "x"})
    error = _error("a", tmp_path)
    assert error["error"] == "invalid_profile" and "'nmae'" in error["message"]
    assert "limits" in error["valid_keys"] and error["file"] == "a.json"
    _write(tmp_path, "b.json", {"limits": {"min_slot_width_mm": 1}})
    error = _error("b", tmp_path)
    assert error["error"] == "invalid_profile" and "min_trace_mm" in error["valid_keys"]


def test_bad_json_names_line_and_column(tmp_path):
    _write(tmp_path, "fab_profile.json", None, b'{\n  "limits": {\n    "min_trace_mm" 0.1\n')
    error = _error(None, tmp_path)
    assert error["error"] == "invalid_profile" and error["file"] == "fab_profile.json"
    assert (error["line"], error["column"]) == (3, 20)
    assert "line 3, column 20" in error["message"]


@pytest.mark.parametrize("raw", [
    b'{"nmae": "x"}', b'{"limits": {"min_trace_mm" 0.1}}', b'{"limits": {"min_trace_mm": 9}}',
])
def test_invalid_profile_never_steers_to_another_profile(tmp_path, raw):
    # The way out is fixing the file; Altimade only when the user asks to ignore it.
    _write(tmp_path, "fab_profile.json", None, raw)
    error = _error(None, tmp_path)
    assert error["error"] == "invalid_profile"
    assert error["message"].endswith("Fix the file, then call again.")
    assert "altimade" not in error["message"].lower()
    assert "Never substitute another profile" in error["hint"]
    assert "only when the user asks to ignore the file" in error["hint"]
    assert list(error)[-1] == "hint"


@pytest.mark.parametrize("digits", [20, 400, 5000])
@pytest.mark.parametrize("key", ["max_drill_sizes", "min_trace_mil", "layer_counts",
                                 "max_board_size_mm", "max_aspect_ratio", "copper_oz"])
def test_huge_integers_in_a_file_are_invalid_profile(tmp_path, key, digits):
    number = b"9" * digits
    value = b"[" + number + b"]" if key == "layer_counts" else number
    _write(tmp_path, "fab_profile.json", None, b'{"limits": {"' + key.encode() + b'": '
           + value + b"}}")
    error = _error(None, tmp_path)  # no OverflowError or Python digit-limit advice
    assert error["error"] == "invalid_profile"
    assert f"a number in the file is too long ({digits} digits)" in error["message"]
    assert "set_int_max_str_digits" not in error["message"]


@pytest.mark.parametrize("limits", [
    {"max_drill_sizes": 10 ** 400}, {"min_trace_mil": 10 ** 400}, {"layer_counts": [10 ** 400]},
    {"max_board_size_mm": [10 ** 400, 1]}, {"max_aspect_ratio": 10 ** 400},
    {"copper_oz": -10 ** 400}, {"tolerance_percent": 10 ** 5000},
])
def test_huge_integers_in_limits_are_invalid_limits(limits):
    assert _error("none", None, limits)["error"] == "invalid_limits"


@pytest.mark.parametrize("raw, text", [
    (b'{"name": "a", "name": "b"}', "duplicate key"),
    (b'{"limits": {"min_trace_mm": NaN}}', "NaN"),
    (b'[1, 2]', "one JSON object"),
    (b'{"name": 5}', "name must be a string"),
    (b'{"limits": [1]}', "limits must be an object"),
    (b'{"name": "\xff\xfe"}', "not UTF-8"),
    (b'{"limits": {"min_trace": 0.1}}', "min_trace_mm or min_trace_mil"),
])
def test_invalid_profile_files(tmp_path, raw, text):
    _write(tmp_path, "a.json", None, raw)
    error = _error("a", tmp_path)
    assert error["error"] == "invalid_profile" and text in error["message"]


def test_utf8_bom_and_size_limit(tmp_path):
    _write(tmp_path, "a.json", None, b'\xef\xbb\xbf{"name": "Acme \xc3\xa9"}')
    assert _ok("a", tmp_path).name == "Acme é"
    _write(tmp_path, "b.json", None, b" " * (MAX_FILE_BYTES + 1) + b"{}")
    assert "larger than" in _error("b", tmp_path)["message"]


def test_name_and_source_are_truncated(tmp_path):
    _write(tmp_path, "a.json", {"name": "N" * 300, "source": "line one\n" + "s" * 500,
                                "source_updated": "rev C"})
    profile = _ok("a", tmp_path)
    assert len(profile.name) == 80 and profile.name.endswith("...")
    assert len(profile.source) == 200 and "\n" not in profile.source
    assert profile.source_updated == "rev C"
    assert "N" * 10 not in profile.scope


# ---------- limits ----------

def test_overrides_beat_file_beat_builtin(tmp_path):
    _write(tmp_path, "fab_profile.json", {"based_on": "altimade", "source": "acme page",
                                          "limits": {"min_spacing_mm": 0.1,
                                                     "min_via_drill_mm": 0.2}})
    profile = _ok(None, tmp_path, {"min_spacing_mil": 3, "max_aspect_ratio": None})
    assert profile.get("min_spacing") == 3.0 and profile.origin("min_spacing") == "argument"
    assert profile.source_of("min_spacing") is None
    assert profile.get("min_via_drill") == pytest.approx(0.2 * MM)
    assert profile.origin("min_via_drill") == "file"
    assert profile.source_of("min_via_drill") == "acme page"
    assert profile.origin("min_trace") == "builtin"
    assert profile.get("max_aspect_ratio") is None
    assert profile.argument_count == 2
    assert profile.scope.endswith("with 2 limits from the limits argument.")
    # Arguments also apply on top of the default Altimade.
    profile = _ok(None, None, {"min_trace_mm": 0.1})
    assert profile.chosen_by == "default" and profile.origin("min_trace") == "argument"
    assert "with 1 limit from the limits argument" in profile.scope


def test_limit_values_convert_to_mil():
    values = parse_limits({
        "min_trace_mm": 0.1, "min_spacing_mil": 4, "max_board_size_mm": [400, 300],
        "min_board_size_mil": 500, "layer_counts": [4.0, 2, 4], "max_drill_sizes": 12.0,
        "max_aspect_ratio": 8, "tolerance_percent": 0, "copper_oz": 2, "drill_allowance_mm": 0.1,
    })
    assert values["min_trace"] == pytest.approx(0.1 * MM)
    assert values["min_spacing"] == 4.0
    assert values["max_board_size"] == pytest.approx((300 * MM, 400 * MM))
    assert values["min_board_size"] == (500.0, 500.0)
    assert values["layer_counts"] == (2, 4)
    assert values["max_drill_sizes"] == 12 and isinstance(values["max_drill_sizes"], int)
    assert values["max_aspect_ratio"] == 8.0 and values["tolerance_percent"] == 0.0
    assert values["drill_allowance"] == pytest.approx(0.1 * MM)
    assert parse_limits(None) == {} and parse_limits({}) == {}


@pytest.mark.parametrize("limits, text", [
    ({"min_trace": 0.1}, "'min_trace' is a length: use min_trace_mm or min_trace_mil"),
    ({"max_board_size": 300}, "use max_board_size_mm or max_board_size_mil"),
    ({"max_aspect_ratio_mm": 8}, "takes no unit suffix: use max_aspect_ratio"),
    ({"min_trace_mm": 0.1, "min_trace_mil": 4}, "given twice"),
    ({"min_trace_mm": "0.1mm"}, "must be a number"),
    ({"min_trace_mm": True}, "must be a number"),
    ({"min_trace_mm": [0.1]}, "must be a number"),
    ({"min_trace_mm": float("nan")}, "must be a number"),
    ({"min_trace_mm": float("inf")}, "must be a number"),
    ({"min_trace_mm": -0.1}, "zero or more"),
    ({"max_drill_mm": 0}, "positive"),
    ({"max_aspect_ratio": 0}, "positive"),
    ({"max_board_size_mm": [1, 2, 3]}, "[a, b]"),
    ({"max_board_size_mm": [100, "x"]}, "a number or [a, b]"),
    ({"layer_counts": []}, "copper layer counts"),
    ({"layer_counts": [2.5]}, "copper layer counts"),
    ({"layer_counts": [0, 2]}, "copper layer counts"),
    ({"max_drill_sizes": 2.5}, "whole number"),
    ({"tolerance_percent": 25}, "between 0 and 20"),
    ({"copper_oz": 0}, "oz weight"),
])
def test_invalid_limits(limits, text):
    error = _error(None, None, limits)
    assert error["error"] == "invalid_limits" and text in error["message"], error


def test_unknown_limit_lists_valid_keys():
    error = _error("none", None, {"min_slot_width_mm": 1})
    assert error["error"] == "invalid_limits" and "min_slot_width_mm" in error["message"]
    assert error["valid_keys"] == list(VALID_KEYS)
    assert _error("none", None, [1])["error"] == "invalid_limits"


@pytest.mark.parametrize("value, text", [
    (True, "not true/false"), ("0.1", "not text"), ({"value": 0.1}, "not an object"),
    ([[1, 2]], "not a list"), (float("nan"), "not a usable number"),
])
def test_wrong_types_say_what_they_are(value, text):
    for key in ("min_trace_mm", "max_aspect_ratio"):
        message = _error("none", None, {key: value})["message"]
        assert message.startswith(f"{key} must be a number (null removes it)") and text in message


@pytest.mark.parametrize("key, ok, bad", [
    ("min_spacing", 1.0, 1.2),
    ("min_hole_to_hole", 1.0, 1.5),
    ("min_hole_to_copper", 1.0, 1.5),
    ("min_copper_to_edge", 5.0, 5.1),
    ("min_hole_to_edge", 5.0, 5.1),
    ("min_trace", 1.0, 1.2),
    ("min_drill", 10.0, 10.5),
    ("max_drill", 10.0, 12.0),
])
def test_upper_bounds_ask_about_mil(key, ok, bad):
    assert _ok("none", None, {f"{key}_mm": ok}).get(key) == pytest.approx(ok * MM)
    message = _error("none", None, {f"{key}_mm": bad})["message"]
    assert "above any fab capability" in message and f"did you mean {key}_mil?" in message
    message = _error("none", None, {f"{key}_mil": bad * MM + 1})["message"]
    assert "above any fab capability" in message and "did you mean" not in message


def test_heavy_copper_spacing_is_accepted_in_mm():
    # 0.6 mm is a heavy-copper spacing, not mil typed as mm.
    assert _ok("none", None, {"min_spacing_mm": 0.6}).get("min_spacing") == pytest.approx(
        0.6 * MM)


@pytest.mark.parametrize("key, value, other", [
    ("min_spacing_mil", 0.15, "min_spacing_mm"),
    ("min_trace_mil", 0.1, "min_trace_mm"),
    ("min_annular_ring_mil", 0.1, "min_annular_ring_mm"),
    ("min_drill_mil", 0.2, "min_drill_mm"),
    ("min_hole_to_copper_mil", 0.2, "min_hole_to_copper_mm"),
    ("min_hole_to_edge_mil", 0.3, "min_hole_to_edge_mm"),
    ("min_copper_to_edge_mil", 0.25, "min_copper_to_edge_mm"),
    ("max_drill_mil", 3, "max_drill_mm"),
    ("min_board_size_mil", [2.54, 2.54], "min_board_size_mm"),
    ("max_board_size_mil", [500, 400], "max_board_size_mm"),
])
def test_lower_bounds_catch_mm_typed_as_mil(key, value, other):
    # Such a limit would pass nearly any board; the mm reading is the likely one.
    message = _error("none", None, {key: value})["message"]
    assert "below any fab capability" in message and f"did you mean {other}?" in message
    assert _ok("none", None, {other: value}).is_set(other.rsplit("_", 1)[0])


def test_lower_bounds_keep_zero_and_tiny_mm_values_get_no_hint():
    assert _ok("none", None, {"min_copper_to_edge_mm": 0}).get("min_copper_to_edge") == 0
    message = _error("none", None, {"min_trace_mm": 0.005})["message"]
    assert "below any fab capability (at least 0.0127 mm = 0.5 mil)." in message
    assert "did you mean" not in message


@pytest.mark.parametrize("name", sorted(_MAX_MM))
def test_a_suggested_unit_never_gives_a_near_zero_limit(name):
    """Following 'did you mean <other unit>?' is always accepted, and never yields a
    limit below HINT_FLOOR_MIL (the reported false pass: 0.6 under _mm suggested _mil,
    a 0.015 mm spacing that passed the board)."""
    values = [0.001, 0.003, 0.01, 0.1, 0.15, 0.3, 0.6, 0.8, 1.2, 3, 5.5, 12, 30, 60, 250,
              900, 2500, 5000, 90000]
    suggested = 0
    for unit, other in (("mm", "mil"), ("mil", "mm")):
        for value in values:
            _, error = load_profile("none", None, {f"{name}_{unit}": value})
            if error is None or f"did you mean {name}_{other}?" not in error["message"]:
                continue
            suggested += 1
            limit = _ok("none", None, {f"{name}_{other}": value}).get(name)
            assert min(limit if isinstance(limit, tuple) else (limit,)) >= HINT_FLOOR_MIL
    assert suggested  # every length has a slip the hint fixes


# ---------- severity, echo, cache ----------

def test_service_limit_severity(tmp_path):
    default = _ok(None, None)
    chosen = _ok("altimade", None)
    for key in SERVICE_LIMITS:
        assert default.severity(key) == "info"
    assert chosen.severity("layer_counts") == "error"
    assert chosen.severity("max_board_size") == "error"
    assert chosen.severity("copper_oz") == "warning"
    assert _ok(None, None, {"layer_counts": [2, 4]}).severity("layer_counts") == "error"
    _write(tmp_path, "fab_profile.json", {"based_on": "altimade"})
    assert _ok(None, tmp_path).severity("layer_counts") == "error"
    with pytest.raises(KeyError):
        default.severity("min_trace")


def test_echo_rows_in_mm_and_mil():
    profile = _ok(None, None, {"max_drill_mm": 6.35, "min_spacing_mm": None})
    echo = profile.echo(True)
    assert echo["name"] == "Altimade" and echo["chosen_by"] == "default"
    assert echo["source"] == ALTIMADE_URL and echo["source_updated"] == "2026-10-13"
    assert echo["scope"] == profile.scope
    rows = {row["limit"]: row for row in echo["limits"]}
    assert rows["min_trace_mm"] == {"limit": "min_trace_mm", "value_mm": 0.075,
                                    "published": "2.955 mil (0.075mm)", "origin": "builtin"}
    assert rows["min_drill_mm"]["value_mm"] == 0.101
    assert rows["max_board_size_mm"]["value_mm"] == [377.952, 377.952]
    assert rows["layer_counts"]["value"] == [2, 4, 6, 8]
    assert rows["max_aspect_ratio"] == {"limit": "max_aspect_ratio", "value": 10.0,
                                        "published": "10", "origin": "builtin"}
    assert rows["max_drill_mm"] == {"limit": "max_drill_mm", "value_mm": 6.35,
                                     "origin": "argument"}
    assert rows["min_spacing_mm"] == {"limit": "min_spacing_mm", "value_mm": None,
                                      "origin": "argument"}
    assert rows["tolerance_percent"] == {"limit": "tolerance_percent", "value": 2.0,
                                         "origin": "default"}
    assert "drill_allowance_mm" not in rows and "max_drill_sizes" not in rows
    mil = {row["limit"]: row for row in profile.echo(False)["limits"]}
    assert mil["min_trace_mil"]["value_mil"] == 2.953
    assert mil["max_board_size_mil"]["value_mil"] == [14880.0, 14880.0]
    json.dumps(echo)


def test_echo_keys_feed_back_as_limits():
    original = builtin_profile("altimade")
    for to_mm in (True, False):
        rows = original.echo(to_mm)["limits"]
        limits = {row["limit"]: row.get("value_mm", row.get("value_mil", row.get("value")))
                  for row in rows}
        again = _ok("none", None, limits)
        for name, value in original.effective().items():
            assert again.get(name) == pytest.approx(value, abs=0.001), name


def test_scope_sentences():
    assert _ok("altimade", None).scope == (
        "Checked against Altimade's published limits (page updated 2026-10-13); other "
        "fabricators differ.")
    assert _ok(None, None).scope == (
        "Checked against Altimade's published limits (default: no fabricator chosen); other "
        "fabricators differ. Pass profile or limits for yours.")


def test_cache_key_tracks_values_and_origins():
    assert _ok(None, None).cache_key() == _ok(None, None).cache_key()
    assert _ok(None, None).cache_key() != _ok("altimade", None).cache_key()
    assert (_ok("none", None, {"min_trace_mm": 0.1}).cache_key()
            != _ok("none", None, {"min_trace_mm": 0.12}).cache_key())
    hash(_ok(None, None, {"layer_counts": [2]}).cache_key())


# ---------- template ----------

def test_example_template_loads_standalone_and_blank(tmp_path):
    shutil.copy(TEMPLATE, tmp_path / "fab_profile.json")
    profile = _ok(None, tmp_path)
    assert profile.chosen_by == "project_file" and profile.based_on is None
    # Nothing inherited and nothing invented: every limit waits for the fab's numbers.
    assert profile.effective() == {} and profile.not_checked == {}
    assert all(row["origin"] in ("file", "default") for row in profile.echo(True)["limits"])
    assert "Altimade" not in profile.scope
    shutil.copy(TEMPLATE, tmp_path / "example.json")
    assert _ok("example", tmp_path).chosen_by == "argument"
    data = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    assert "based_on" not in data
    assert any("fill in" in line for line in data["comment"])
    # Every limit is named: the blanks, and the specific ones that fall back.
    limits = data["limits"]
    blanks = {key for key in limits if not key.startswith("_")}
    named = blanks | set(limits["_specific_limits"])
    assert named == {external_key(name, True) for name in LIMIT_KEYS}
    assert {key.removesuffix("_mm") for key in limits["_specific_limits"]} == set(FALLBACKS)
    assert not blanks & {external_key(name, True) for name in FALLBACKS}
    assert all(limits[key] is None for key in blanks)


def test_example_template_filled_in_keeps_the_fallbacks(tmp_path):
    data = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    data["limits"].update({"min_drill_mm": 0.2, "min_spacing_mm": 0.1,
                           "min_copper_to_edge_mm": 0.3, "max_board_size_mm": [500, 400]})
    _write(tmp_path, "fab_profile.json", data)
    profile = _ok(None, tmp_path)
    for specific in ("min_via_drill", "min_pth_drill", "min_npth_drill"):
        assert profile.get(specific) == pytest.approx(0.2 * MM)
    assert profile.get("min_hole_to_copper") == pytest.approx(0.1 * MM)
    assert profile.get("min_hole_to_edge") == pytest.approx(0.3 * MM)
    assert profile.get("max_board_size") == pytest.approx((400 * MM, 500 * MM))
    assert profile.get("min_trace") is None and profile.get("tolerance_percent") == 2.0
