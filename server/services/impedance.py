"""Layer Stack Manager impedance profiles: per-layer solutions, rule usage and
consistency issues, from the parsed PcbDoc (no MCP, no SQLite).

A profile is a target (e.g. 90 ohm differential +/-10%) solved per signal layer
into a trace width (and gap) with a calculated impedance. DiffPairsRouting and
Width rules can be profile-driven (IMPEDANCEPROFILEDRIVEN/IMPEDANCEPROFILEID).
"""

try:
    from services.pcb_rules import rule_values
except ImportError:
    from server.services.pcb_rules import rule_values

# PROPAGATION_SPEED (~0.13-0.18) is a delay in ns/inch (= ps/mil), not a
# speed: read that way, striplines come out at their laminate Dk and
# microstrips below it, as the physics requires. Altium does not document it.
PROPAGATION_UNIT_NOTE = (
    "delay_ps_per_inch is the stack manager's stored propagation value, read as "
    "ns/inch (= ps/mil); Altium does not document the unit, and this reading matches "
    "the stack Dk (striplines at the laminate Dk, microstrips below it)."
)
_MATCH_TOLERANCE_MIL = 0.01


def _layer_names(board) -> dict[int, str]:
    return {layer.v7_id: layer.name for layer in board.layers if layer.v7_id is not None}


def profile_refs(board) -> dict[str, dict]:
    """Profile id (upper) -> short identity for rule values."""
    return {
        profile.id.upper(): {
            "name": profile.name,
            "type": "differential" if profile.differential else "single-ended",
            "target_ohm": profile.target_ohm,
            "tolerance_pct": profile.tolerance_pct,
        }
        for profile in board.impedance_profiles
    }


def _profile_rules(rules, profile_id: str) -> list:
    return [
        rule
        for rule in rules
        if rule.properties.get("IMPEDANCEPROFILEID", "").strip().upper()
        == profile_id.upper()
    ]


def _within(profile, calculated: float | None) -> bool | None:
    if calculated is None or not profile.target_ohm or profile.tolerance_pct is None:
        return None
    deviation = abs(calculated - profile.target_ohm) / profile.target_ohm * 100.0
    return deviation <= profile.tolerance_pct + 1e-9


def impedance_profiles(index) -> list[dict]:
    """Profiles with per-layer solutions, the rules using them, and issues."""
    board = index.pcb.board
    names = _layer_names(board)
    substacks = {substack["id"].upper(): substack["name"] for substack in board.substacks}
    signal_layers = index.signal_layers()
    result = []
    for profile in board.impedance_profiles:
        rules = _profile_rules(index.pcb.rules, profile.id)
        layers = []
        issues: list[str] = []
        by_layer: dict[str, dict] = {}
        for entry in profile.layers:
            if not entry.enabled:
                continue
            name = names.get(entry.layer_v7, f"layer {entry.layer_v7}")
            row: dict = {"layer": name}
            reference = [
                names[v7] for v7 in (entry.ref_top_v7, entry.ref_bottom_v7) if v7 in names
            ]
            row["reference_layers"] = reference
            if entry.substack_id and len(substacks) > 1:
                row["substack"] = substacks.get(entry.substack_id.upper(), entry.substack_id)
            for key in ("width_mil", "min_width_mil", "max_width_mil"):
                value = getattr(entry, key)
                if value is not None:
                    row[key] = value
            if profile.differential:
                for key in ("gap_mil", "min_gap_mil", "max_gap_mil"):
                    value = getattr(entry, key)
                    if value is not None:
                        row[key] = value
            if entry.calculated_ohm is not None:
                row["calculated_ohm"] = round(entry.calculated_ohm, 2)
            if entry.error_pct is not None:
                row["error_pct"] = round(entry.error_pct, 2)
            within = _within(profile, entry.calculated_ohm)
            if within is not None:
                row["within_tolerance"] = within
                if not within:
                    issues.append(
                        f"calculated impedance on {name} is {entry.calculated_ohm:.1f} ohm, "
                        f"outside {profile.target_ohm:g} ohm +/-{profile.tolerance_pct:g}%"
                    )
            if entry.propagation_speed:
                row["delay_ps_per_inch"] = round(entry.propagation_speed * 1000.0, 1)
            layers.append(row)
            if not entry.substack_id or len(substacks) <= 1:
                by_layer[name.lower()] = row
        used_by = []
        for rule in rules:
            used_by.append({"rule": rule.name, "kind": rule.kind, "enabled": rule.enabled})
            if rule.properties.get("IMPEDANCEPROFILEDRIVEN", "").strip().upper() != "TRUE":
                continue
            values = rule_values(rule, signal_layers, board.substacks)
            for layer_name, fields in (values.get("layers_mil") or {}).items():
                solved = by_layer.get(layer_name.lower())
                if not solved:
                    continue
                for rule_key, profile_key in (
                    ("preferred_width", "width_mil"),
                    ("preferred_gap", "gap_mil"),
                ):
                    if rule_key in fields and profile_key in solved and abs(
                        fields[rule_key] - solved[profile_key]
                    ) > _MATCH_TOLERANCE_MIL:
                        issues.append(
                            f"rule {rule.name} {rule_key.replace('_', ' ')} on {layer_name} "
                            f"is {fields[rule_key]:g} mil but the profile solves "
                            f"{solved[profile_key]:g} mil"
                        )
        if not rules:
            issues.append("no design rule uses this profile")
        entry_out = {
            "name": profile.name,
            "type": "differential" if profile.differential else "single-ended",
            "target_ohm": profile.target_ohm,
            "tolerance_pct": profile.tolerance_pct,
            "used_by_rules": used_by,
            "layers": layers,
        }
        if issues:
            entry_out["issues"] = issues
        result.append(entry_out)
    return result


def snapshot_line(index) -> str:
    """One bounded line for the constraints review snapshot."""
    profiles = impedance_profiles(index)
    if not profiles:
        return "- Impedance profiles: none defined (impedance cannot be confirmed from the files)"
    parts = []
    for profile in profiles[:10]:
        tolerance = (
            f" +/-{profile['tolerance_pct']:g}%" if profile["tolerance_pct"] is not None else ""
        )
        target = f"{profile['target_ohm']:g} ohm" if profile["target_ohm"] is not None else "?"
        used = len(profile["used_by_rules"])
        flags = f"used by {used} rule{'s' if used != 1 else ''}" if used else "unused"
        if any(not issue.startswith("no design rule") for issue in profile.get("issues", [])):
            flags += ", has issues"
        parts.append(f"{profile['name'][:40]} ({profile['type']} {target}{tolerance}, {flags})")
    more = f", +{len(profiles) - 10} more" if len(profiles) > 10 else ""
    return f"- Impedance profiles ({len(profiles)}): " + "; ".join(parts) + more
