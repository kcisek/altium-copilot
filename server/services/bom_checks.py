"""Offline BOM hygiene checks on a variant's BOM parts.

Thresholds come from real projects: a part without an MPN is a finding only where the
project keeps MPNs in part parameters (half its parts or more); values compare after
unit normalisation (100n = 100nF = 0.1uF, 4k7 = 4.7k); consolidation candidates are
passives of one value, size and rating under different MPNs.
"""

import re
from collections import Counter, defaultdict

try:
    from services.bom import BomPart, natural_key
except ImportError:
    from server.services.bom import BomPart, natural_key

CHECKS = (
    "duplicate_designator", "unresolved_special_string", "dnp_marker_on_fitted",
    "no_footprint", "mpn_inconsistent", "mpn_without_manufacturer", "missing_mpn",
    "placeholder_description", "stale_variant_entry", "consolidation", "not_compiled",
)
_CHECK_RANK = {check: rank for rank, check in enumerate(CHECKS)}
_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}
FINDING_LIMIT = 100
MPN_COVERAGE = 0.5  # below this share of parts with an MPN, MPNs are not kept here

_MULTIPLIERS = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1.0,
    "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "R": 1.0, "r": 1.0,
}
_VALUE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([pnuµμmkKMGRr]?)(\d*)\s*([A-Za-zΩ]*)")
_DNP_MARK = re.compile(
    r"\b(?:DNP|DNF|DNI|NOT[ _-]?FITTED|NO[ _-]?POP|DO[ _-]NOT[ _-](?:POPULATE|FIT|PLACE))\b",
    re.IGNORECASE,
)
_PLACEHOLDERS = {"", "?", "-", "--", "tbd", "tba", "n/a", "na", "none", "description", "todo"}
_GENERIC = {
    "resistor", "capacitor", "inductor", "diode", "connector", "led", "ic", "transistor",
    "crystal", "fuse", "ferrite bead", "header", "res", "cap", "ind",
}
_PASSIVE = re.compile(r"^(R|C|L|FB)\d", re.IGNORECASE)
_SIZE = re.compile(r"(?<!\d)(01005|0201|0402|0603|0805|1206|1210|1812|2010|2512)(?!\d)")
_RATING = re.compile(
    r"(\d+(?:\.\d+)?\s?k?V\b|\bX\d[RSTP]\b|\bC0G\b|\bNP0\b|\d+(?:\.\d+)?\s?%|\d+(?:\.\d+)?\s?W\b)",
    re.IGNORECASE,
)


def normalize_value(text: str | None):
    """A component value in base units ('100n' -> 1e-07), or folded text when it does
    not read as a number. 'm' is milli, 'M' mega; 'R' and a trailing digit read as a
    decimal mark (4R7, 4k7)."""
    if not text:
        return None
    match = _VALUE.match(text)
    if not match:
        return re.sub(r"\s+", " ", text).strip().lower()
    number, prefix, tail, _unit = match.groups()
    value = float(f"{number}.{tail}" if tail and "." not in number else number + tail)
    value *= _MULTIPLIERS.get(prefix, 1.0)
    return round(value, 15) if value < 1 else round(value, 9)


def _fold(text: str | None) -> str:
    return re.sub(r"[\W_]+", "", (text or "").lower())


def _finding(check, severity, issue, designators=(), **fields) -> dict:
    out = {"check": check, "severity": severity}
    if designators:
        out["designators"] = sorted(designators, key=natural_key)
    out.update(fields)
    out["issue"] = issue
    return out


def _check_mpn(parts: list[BomPart], findings: list[dict]) -> None:
    counted = [part for part in parts if part.kind != 1]  # mechanical parts often have none
    with_mpn = [part for part in counted if part.mpn]
    coverage = len(with_mpn) / len(counted) if counted else 1.0
    if counted and coverage < MPN_COVERAGE:
        findings.append(_finding(
            "missing_mpn", "info",
            f"Only {round(coverage * 100)}% of the parts carry a manufacturer part number "
            "parameter, so MPNs are not kept in the schematic here (perhaps in ActiveBOM, "
            "which is not read): the BOM groups those parts by Comment and footprint.",
            coverage_percent=round(coverage * 100),
        ))
    else:
        groups: dict[tuple, list[BomPart]] = defaultdict(list)
        for part in counted:
            if not part.mpn:
                groups[(part.comment or part.value or "", part.footprint or "")].append(part)
        for (comment, footprint), members in groups.items():
            findings.append(_finding(
                "missing_mpn", "warning",
                f"No manufacturer part number on {comment or 'parts'} ({footprint or 'no '
                'footprint'}).",
                [part.designator for part in members], comment=comment or None,
            ))
    by_mpn: dict[str, list[BomPart]] = defaultdict(list)
    for part in with_mpn:
        if part.mpn_source == "parameter" and not part.manufacturer:
            by_mpn[part.mpn].append(part)
    for mpn, members in by_mpn.items():
        findings.append(_finding(
            "mpn_without_manufacturer", "warning",
            f"{mpn} has no manufacturer.", [part.designator for part in members], mpn=mpn,
        ))


def _check_consistency(parts: list[BomPart], findings: list[dict]) -> None:
    by_mpn: dict[str, list[BomPart]] = defaultdict(list)
    for part in parts:
        if part.mpn:
            by_mpn[_fold(part.mpn)].append(part)
    for members in by_mpn.values():
        if len(members) < 2:
            continue
        mpn = members[0].mpn
        for name, normal in (
            ("value", normalize_value), ("description", _fold), ("footprint", _fold),
        ):
            values = {normal(getattr(part, name)) for part in members if getattr(part, name)}
            if len(values) < 2:
                continue
            shown = sorted({getattr(part, name) for part in members if getattr(part, name)})
            findings.append(_finding(
                "mpn_inconsistent", "info" if name == "footprint" else "warning",
                f"{mpn} has different {name}s: {', '.join(shown[:4])}"
                + (" (another footprint model of the same part?)" if name == "footprint" else "."),
                [part.designator for part in members], mpn=mpn, field=name,
            ))


def _check_consolidation(parts: list[BomPart], findings: list[dict]) -> None:
    groups: dict[tuple, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for part in parts:
        prefix = _PASSIVE.match(part.designator)
        if not prefix or not part.mpn:
            continue
        value = normalize_value(part.value or part.comment)
        size = _SIZE.search(" ".join(filter(None, (part.footprint, part.description))))
        if value is None or not size:
            continue
        rating = frozenset(
            token.replace(" ", "").lower()
            for token in _RATING.findall(" ".join(filter(None, (part.description, part.comment))))
        )
        key = (prefix[1].upper(), value, size[1], rating)
        groups[key][part.mpn].append(part.designator)
    for (kind, _value, size, _rating), by_mpn in groups.items():
        if len(by_mpn) < 2:
            continue
        mpns = sorted(by_mpn)
        common = len(_common_prefix(mpns))
        if common >= 0.6 * min(len(mpn) for mpn in mpns):
            continue  # one series: packaging or tolerance suffixes
        findings.append(_finding(
            "consolidation", "info",
            f"{len(mpns)} part numbers for one {kind} value in {size} with the same "
            f"ratings: {', '.join(mpns[:4])}. Consider one part (or check they are "
            "deliberate second sources).",
            [d for designators in by_mpn.values() for d in designators], mpns=mpns,
        ))


def _common_prefix(texts: list[str]) -> str:
    first, last = min(texts), max(texts)
    size = 0
    while size < min(len(first), len(last)) and first[size] == last[size]:
        size += 1
    return first[:size]


def _check_parts(parts: list[BomPart], findings: list[dict]) -> None:
    unresolved = [part for part in parts if part.unresolved]
    for part in unresolved:
        findings.append(_finding(
            "unresolved_special_string", "warning",
            f"{part.designator}: {', '.join(part.unresolved)} names no parameter of the part.",
            [part.designator],
        ))
    marked = [
        part for part in parts if part.fitted
        and any(_DNP_MARK.search(text or "") for text in (part.value, part.comment))
    ]
    if marked:
        findings.append(_finding(
            "dnp_marker_on_fitted", "warning",
            "These parts say 'do not populate' in their Value or Comment but are fitted in "
            "this variant: make them Not Fitted in the variant, or fix the text.",
            [part.designator for part in marked],
        ))
    missing = [part for part in parts if part.kind != 1 and not part.footprint]
    if missing:
        findings.append(_finding(
            "no_footprint", "warning",
            "These BOM parts have no footprint.", [part.designator for part in missing],
        ))
    placeholder = defaultdict(list)
    generic = defaultdict(list)
    for part in parts:
        text = (part.description or "").strip()
        if text.lower() in _PLACEHOLDERS:
            placeholder[text].append(part.designator)
        elif text.lower() in _GENERIC and not part.mpn:
            generic[text].append(part.designator)
    for text, designators in placeholder.items():
        findings.append(_finding(
            "placeholder_description", "warning",
            f"Description is {'empty' if not text else repr(text)}.", designators,
        ))
    for text, designators in generic.items():
        findings.append(_finding(
            "placeholder_description", "info",
            f"Description is only '{text}' and there is no MPN to say which part.",
            designators,
        ))


def run_bom_checks(
    parts: list[BomPart],
    netlist: dict,
    plan=None,
    checks: list[str] | None = None,
    offset: int = 0,
    limit: int = FINDING_LIMIT,
) -> dict:
    requested = [check.strip().lower() for check in checks or [] if check.strip()]
    unknown = [check for check in requested if check not in CHECKS]
    if unknown:
        return {"error": "invalid_checks",
                "message": f"Unknown checks {unknown}; use {list(CHECKS)}."}
    fitted = [part for part in parts if part.fitted]
    findings: list[dict] = []
    _check_parts(fitted, findings)
    _check_mpn(fitted, findings)
    _check_consistency(fitted, findings)
    _check_consolidation(fitted, findings)
    for clash in netlist.get("designator_clashes") or []:
        # A clash can be with a compile-masked copy, which the compiled design leaves out.
        findings.append(_finding(
            "duplicate_designator", "warning",
            f"{clash.get('refdes')} names {len(clash.get('lib_references') or [])} different "
            f"parts on sheet {clash.get('sheet')}: only one can be in the BOM, unless the "
            "others are compile-masked.",
            [clash.get("refdes")] if clash.get("refdes") else [],
        ))
    for entry in getattr(plan, "stale", []) or []:
        findings.append(_finding(
            "stale_variant_entry", "info",
            f"Variant entry for {entry['designator']}: {entry['detail']}.",
            [entry["designator"]] if entry.get("designator") else [], reason=entry["issue"],
        ))
    compiled = netlist.get("parts") or {}
    if compiled:
        uncompiled = [name for name in netlist.get("components") or {} if name not in compiled]
        if uncompiled:
            findings.append(_finding(
                "not_compiled", "info",
                f"{len(uncompiled)} sheet parts are not in the compiled design (compile "
                "masks, or sheets outside the project) and are not in the BOM.",
                uncompiled[:30], count=len(uncompiled),
            ))
    wanted = set(requested or CHECKS)
    findings = [finding for finding in findings if finding["check"] in wanted]
    findings.sort(key=lambda finding: (
        _SEVERITY_RANK[finding["severity"]], _CHECK_RANK[finding["check"]], finding["issue"],
    ))
    severities = Counter(finding["severity"] for finding in findings)
    offset = max(0, int(offset or 0))
    result = {
        "checks_run": list(requested or CHECKS),
        "summary": dict(Counter(finding["check"] for finding in findings)),
        "severity_counts": {
            level: severities.get(level, 0) for level in ("error", "warning", "info")
        },
        "findings": findings[offset : offset + limit],
        "total": len(findings),
        "offset": offset,
        "has_more": offset + limit < len(findings),
    }
    if result["has_more"]:
        result["next_offset"] = offset + limit
    return result
