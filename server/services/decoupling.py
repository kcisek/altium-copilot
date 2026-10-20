"""Offline decoupling coverage: which capacitors decouple each IC's power pins, and gaps.

Grounds, rails, ICs and capacitors come from the compiled netlist and the sheets' pin
names. The rules were calibrated on real boards: a net is a rail by its name or by an IC
supply pin on it (a power port alone proves little, and nets named like power but
carrying EN, PG or SENSE are signals); an IC is a U or IC part, paired with a rail
through pins whose first function is a supply, reference or capacitor pin. Capacitors
are two-pin parts from a rail to ground, fitted in the variant.

With a PcbDoc, the distance from each IC power pad to the nearest fitted capacitor on
its rail is measured straight, pad edge to pad edge (warning above 5 mm by default:
88% of IC rails on real boards are within it). Pours, regions and planes are not read,
so connections through them are not seen: vias are reported as found, never as copper
that is missing.
"""

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field

try:
    from services.bom import build_parts, natural_key
    from services.pcb_geometry import point_in_polygon
except ImportError:
    from server.services.bom import build_parts, natural_key
    from server.services.pcb_geometry import point_in_polygon

CHECKS = (
    "no_capacitor", "not_fitted_only", "filtered_rail", "far_capacitor",
    "capacitor_elsewhere", "pads_far", "opposite_side_no_via", "no_small_capacitor",
    "no_bulk_capacitor", "unknown_value",
)
PCB_CHECKS = frozenset({"far_capacitor", "pads_far", "opposite_side_no_via"})
# Bulk is per rail or where it enters the board, often behind a ferrite or on a module:
# on real boards 'no bulk' held for about half the rails, so it runs only when asked.
OPT_IN_CHECKS = frozenset({"no_bulk_capacitor"})
_SEVERITY = {
    "no_capacitor": "warning", "not_fitted_only": "warning", "filtered_rail": "warning",
    "far_capacitor": "warning",
}
_CHECK_RANK = {check: rank for rank, check in enumerate(CHECKS)}
_SEVERITY_RANK = {"warning": 0, "info": 1}

MIL_PER_MM = 1 / 0.0254
DEFAULT_MAX_DISTANCE_MIL = 5 * MIL_PER_MM
VIA_NEAR_MIL = 2 * MIL_PER_MM  # a via this close to an opposite-side capacitor's pad
SMALL_FARADS = 1e-6  # under this a capacitor is a small (high-frequency) one
FINDING_LIMIT = 100
RAIL_LIMIT = 60
_THT_LAYER = 74

# ---------- names ----------

_SPLIT = re.compile(r"[_\-\s/]+")
_POWER_TOKEN = re.compile(
    r"^(\d+V\d*|\d+[.,P]\d+V?|V\d+(V\d+|P\d+)?|[PNM]\d+V\d*|[ADP]?VCC\w*|[ADP]?VDD\w*|VEE\w*|"
    r"VBATT?\w*|VBUS\w*|VIN\w*|VSYS\w*|VMAIN\w*|VPP|VIO\w*|PVIN\w*|VSUP\w*|VPWR|PWR|VDC|"
    r"VAUX\w*|VCORE\w*|VLED\w*|VMOT\w*|VUSB|VLOGIC|VDRIVE|VCHG|VREF\w*|VOUT\w*|VS|VM|VL|"
    r"VCCIO\w*|VPOS|VNEG|VBOOST|VCHARGE|VTT|VREG\w*)$"
)
_GROUND_TOKEN = re.compile(r"^(\w*GND\w*|VSS\w*|0V|GROUND)$")
_SIGNAL_TOKENS = frozenset({
    "EN", "ENA", "ENABLE", "PG", "PGOOD", "PWRGD", "PGD", "GOOD", "OK", "SENSE", "SNS", "SEN",
    "DET", "DETECT", "FB", "ON", "OFF", "FAULT", "FLT", "ALERT", "SW", "LX", "CTRL", "CTL",
    "ADC", "MON", "MEAS", "ISNS", "ISENSE", "SEL", "RST", "RESET", "INT", "IRQ", "STAT",
    "STATUS", "TX", "RX", "SDA", "SCL", "CLK", "DATA", "SIG", "GATE", "DRV", "PWM", "BST",
    "BOOT", "ILIM", "SS", "TRK", "COMP", "SYNC", "MODE", "REQ", "ACK", "CS", "DIV", "LVL",
    "CMD", "SET", "TRIG", "SDI", "SDO", "MISO", "MOSI", "SCK", "SCLK", "DIS", "SHDN",
    "NFAULT", "CC", "CC1", "CC2", "DP", "DM", "D+", "D-", "ID", "SBU1", "SBU2",
})
_SIGNAL_INSIDE = re.compile(r"SENSE|SNS|PGOOD|PWRGD|ENABLE|ALERT|FAULT")

_PIN_GROUND = re.compile(
    r"^(\w*GND\w*|VSS\w*|EP|EPAD|E-PAD|PAD|THERMAL\w*|TAB|SLUG|0V|GROUND|EXPOSED\w*|\d*GND)$"
)
_PIN_SUPPLY = re.compile(
    r"^(\w*VDD\w*|\w*VCC\w*|VIN(?!_?[PN]\d*$)\w*|PVIN\w*|AVIN\w*|VBAT\w*|VBUS\w*|VS[+-]?\d*|"
    r"V[+-]|VSUP\w*|VIO\w*|VL|VLOGIC|VDRIVE|VPP|VEE\w*|VNEG|VPOS|VM|VMOT\w*|VPWR|PWR|VSYS|"
    r"V\d+V\d*|\d+V\d*|V\d+P\d+|VPLUS|VSUPPLY|SUPPLY|POWER|\+?V(CC|DD)?|DVDD\w*|AVDD\w*)$"
)
_PIN_REF = re.compile(
    r"^(\w*VREF[A-Z0-9_]*|REF|REF[+-]|REFIN\d*|REFOUT\d*|REF_?[PN]|REFP|REFN|VCM|VMID|VBIAS)$"
)
_PIN_CAP = re.compile(
    r"^(VCAP\w*|CAP\d*|CAP[+-]|VREG\w*|DECOUP\w*|BYP|BYPASS|LDO_?OUT\w*|LDO_?CAP\w*|"
    r"VCORE\w*|VDDCORE\w*|VDD_?OUT\w*)$"
)
_PIN_OUT = re.compile(r"^(VOUT\w*|OUT\w*|SW\d*|LX\d*)$")
_PIN_IN = re.compile(r"^(IN\d*|INPUT)$")
_PAIR_ROLES = frozenset({"supply", "ref", "cap"})

_IC_PREFIXES = frozenset({"U", "IC"})
_FILTER_PREFIXES = frozenset({"L", "FB", "FL", "R"})
_NOT_CAP_PREFIXES = frozenset({
    "D", "U", "IC", "Q", "J", "P", "TVS", "ESD", "Z", "LED", "CN", "CON", "CONN", "CR", "F",
    "L", "FB", "R", "T", "X", "Y", "TP", "PS", "SW", "K",
})
_CAP_TEXT = re.compile(
    r"\bcap\b|\bcap[ .,_/-]|\bcaps\b|capacitor|\bmlcc\b|electrolytic|tantalum|kondensator",
    re.IGNORECASE,
)
_FERRITE_TEXT = re.compile(r"ferrite|\bbead", re.IGNORECASE)
_FARAD_TEXT = re.compile(r"\d\s*[pnuµμm]?F\b|\d[pnuµμ]\d")
_CAP_VALUE = re.compile(
    r"(?<![\w.])(\d+(?:[.,]\d+)?)\s*([pnuµμ])\s*F?(?![a-zA-Z\d])"
    r"|(?<![\w.])(\d+)([pnuµμ])(\d+)(?![\w.])"  # 4u7
)
_FARADS = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6}


def _tokens(net: str) -> list[str]:
    return [token for token in _SPLIT.split(net.upper().replace("\\", "")) if token]


def _net_kind(net: str) -> dict:
    """Whether a net name says ground, power, or carries a signal token."""
    tokens = _tokens(net)
    stripped = [token.strip("+-") for token in tokens]
    signal = any(token in _SIGNAL_TOKENS or token.rstrip("+-") in _SIGNAL_TOKENS
                 for token in tokens) or any(_SIGNAL_INSIDE.search(token) for token in tokens)
    ground = any(_GROUND_TOKEN.match(token) for token in stripped)
    power = any(_POWER_TOKEN.match(token) for token in stripped) or bool(
        re.match(r"^[+-]\d", net.strip())
    )
    return {"ground": ground, "power": power and not ground, "signal": signal}


def pin_role(name: str | None) -> str:
    """ground, supply, ref, cap, out, in, other, or none, from the pin name's first
    function ('I/O, VREF 1' is an I/O pin, not a reference)."""
    if not name:
        return "none"
    first = next((part for part in re.split(r"[/,]", name.upper().replace("\\", "")
                                            .replace(" ", "")) if part), "").strip("_-")
    if _SIGNAL_INSIDE.search(first):
        return "other"  # VCCSENSE, VDD_PGOOD: they measure or report a rail
    for role, pattern in (("ground", _PIN_GROUND), ("ref", _PIN_REF), ("cap", _PIN_CAP),
                          ("supply", _PIN_SUPPLY), ("out", _PIN_OUT), ("in", _PIN_IN)):
        if pattern.match(first):
            return role
    return "other"


def _prefix(designator: str) -> str:
    match = re.match(r"[A-Za-z_]+", designator or "")
    return match.group(0).upper().strip("_") if match else ""


def capacitance(*texts: str | None) -> float | None:
    """Farads from the first text holding a capacitance ('100nF/16V', '4u7', '0.1 µF')."""
    for text in texts:
        for match in _CAP_VALUE.finditer(text or ""):
            if match.group(1):
                number, prefix = match.group(1).replace(",", "."), match.group(2)
            else:
                number, prefix = f"{match.group(3)}.{match.group(5)}", match.group(4)
            try:
                return float(number) * _FARADS[prefix]
            except ValueError:
                continue
    return None


def _farads_text(value: float | None) -> str | None:
    if value is None:
        return None
    for unit, scale in (("uF", 1e-6), ("nF", 1e-9), ("pF", 1e-12)):
        if value >= scale:
            return f"{value / scale:.3g}{unit}"
    return f"{value:.3g}F"


# ---------- the design ----------

@dataclass
class _Cap:
    designator: str
    rail: str  # the net it decouples (the non-ground one)
    other: str  # ground, or the other net of a capacitor between two nets
    to_ground: bool
    farads: float | None
    fitted: bool

    def small(self) -> bool:
        return self.farads is not None and self.farads < SMALL_FARADS

    def describe(self) -> dict:
        out = {"designator": self.designator, "value": _farads_text(self.farads)}
        if not self.fitted:
            out["not_fitted"] = True
        return out


@dataclass
class _Pair:
    ic: str
    rail: str
    pins: list[str] = field(default_factory=list)  # IC pin designators on the rail


@dataclass
class _Design:
    grounds: set[str]
    rails: dict[str, list[str]]  # rail -> evidence ('name', 'ic_pin')
    ics: set[str]
    pairs: list[_Pair]
    caps: dict[str, list[_Cap]]  # rail -> capacitors to ground
    between: dict[str, list[_Cap]]  # net -> capacitors to another non-ground net
    filters: dict[str, list[tuple[str, str]]]  # net -> (filter part, the other net)
    ic_nets: dict[str, set[str]]


def _design(netlist: dict, plan, project_params: dict | None) -> _Design:
    components = netlist.get("components") or {}
    compiled = netlist.get("parts") or {}
    pin_to_net = netlist.get("pin_to_net") or {}
    names = set(compiled) if compiled else {
        name for name, meta in components.items() if not meta.get("compile_masked")
    }
    refs = [name for name in pin_to_net if name in names]
    nets: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for ref in refs:
        for pin, net in pin_to_net[ref].items():
            nets[net].append((ref, pin))

    def pin_name(ref: str, pin: str) -> str:
        return (((components.get(ref) or {}).get("pins") or {}).get(pin) or {}).get("name") or ""

    ics = {ref for ref in refs if _prefix(ref) in _IC_PREFIXES}
    kinds = {net: _net_kind(net) for net in nets}
    grounds = set()
    for net, members in nets.items():
        roles = [pin_role(pin_name(ref, pin)) for ref, pin in members if ref in ics]
        named = [role for role in roles if role not in ("none", "other")]
        by_pins = bool(named) and named.count("ground") * 2 >= len(named)
        if (kinds[net]["ground"] and not kinds[net]["signal"]) or (
            by_pins and not kinds[net]["power"]
        ):
            grounds.add(net)
    rails: dict[str, list[str]] = {}
    for net, members in nets.items():
        if net in grounds:
            continue
        evidence = []
        if kinds[net]["power"] and not kinds[net]["signal"]:
            evidence.append("name")
        if any(ref in ics and pin_role(pin_name(ref, pin)) in _PAIR_ROLES
               for ref, pin in members):
            evidence.append("ic_pin")
        if evidence:
            rails[net] = evidence

    parts = {part.designator: part for part in build_parts(netlist, plan, project_params)[0]}
    not_fitted = getattr(plan, "not_fitted", set()) or set()
    caps: dict[str, list[_Cap]] = defaultdict(list)
    between: dict[str, list[_Cap]] = defaultdict(list)
    filters: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for ref in refs:
        connected = pin_to_net[ref]
        if len(connected) != 2:
            continue
        meta = components.get(ref) or {}
        part = parts.get(ref)
        varied = (getattr(plan, "params", {}) or {}).get(ref) or {}
        params = {**(meta.get("params") or {}), **{key: value for key, (_, value) in varied.items()}}
        text = " ".join(filter(None, (
            meta.get("description"), meta.get("lib_reference"),
            (compiled.get(ref) or {}).get("comment"), meta.get("value"),
            params.get("capacitance"),
        )))
        prefix = _prefix(ref)
        net_a, net_b = connected.values()
        if prefix in _FILTER_PREFIXES or (_FERRITE_TEXT.search(text) and prefix not in _IC_PREFIXES):
            if net_a != net_b:
                filters[net_a].append((ref, net_b))
                filters[net_b].append((ref, net_a))
            continue
        is_cap = prefix == "C" or (
            prefix.startswith("C") and prefix not in _NOT_CAP_PREFIXES
            and (_CAP_TEXT.search(text) or _FARAD_TEXT.search(text))
        )
        if not is_cap or net_a == net_b:
            continue
        farads = capacitance(
            part.value if part else params.get("value"), params.get("capacitance"),
            part.comment if part else (compiled.get(ref) or {}).get("comment"),
            part.description if part else meta.get("description"),
        )
        fitted = part.fitted if part else ref not in not_fitted
        if net_a in grounds and net_b in grounds:
            continue
        if net_a in grounds or net_b in grounds:
            rail = net_b if net_a in grounds else net_a
            other = net_a if net_a in grounds else net_b
            caps[rail].append(_Cap(ref, rail, other, True, farads, fitted))
        else:
            between[net_a].append(_Cap(ref, net_a, net_b, False, farads, fitted))
            between[net_b].append(_Cap(ref, net_b, net_a, False, farads, fitted))

    pairs: dict[tuple[str, str], _Pair] = {}
    ic_nets: dict[str, set[str]] = defaultdict(set)
    for ic in sorted(ics, key=natural_key):
        pins = pin_to_net[ic]
        roles = {pin: pin_role(pin_name(ic, pin)) for pin in pins}
        unnamed = all(role == "none" for role in roles.values())
        for pin, net in sorted(pins.items(), key=lambda item: natural_key(item[0])):
            ic_nets[ic].add(net)
            if net not in rails:
                continue
            role = roles[pin]
            named_rail = "name" in rails[net]
            if role in _PAIR_ROLES or (role == "in" and named_rail) or (unnamed and named_rail):
                pairs.setdefault((ic, net), _Pair(ic, net)).pins.append(pin)
    return _Design(grounds, rails, ics, list(pairs.values()), caps, between, filters, ic_nets)


# ---------- findings ----------

def _finding(check: str, issue: str, designators=(), **fields) -> dict:
    out = {"check": check, "severity": _SEVERITY.get(check, "info")}
    if designators:
        out["designators"] = sorted(set(designators), key=natural_key)
    out.update({key: value for key, value in fields.items() if value is not None})
    out["issue"] = issue
    return out


def _names(items) -> str:
    items = sorted(set(items), key=natural_key)
    return ", ".join(items[:8]) + (f" and {len(items) - 8} more" if len(items) > 8 else "")


def _feeders(design: _Design, rail: str) -> list[tuple[str, str]]:
    """Filters (ferrite, inductor, resistor) linking a rail to a rail with capacitors."""
    return [
        (part, other) for part, other in design.filters.get(rail, [])
        if other not in design.grounds
        and (other in design.rails or any(cap.fitted for cap in design.caps.get(other, [])))
    ]


def _schematic_findings(design: _Design, pairs: list[_Pair], variant: str) -> list[dict]:
    findings: list[dict] = []
    by_rail: dict[str, list[_Pair]] = defaultdict(list)
    for pair in pairs:
        by_rail[pair.rail].append(pair)
    unknown: set[str] = set()
    for rail, rail_pairs in sorted(by_rail.items(), key=lambda item: natural_key(item[0])):
        ics = [pair.ic for pair in rail_pairs]
        pins = {pair.ic: pair.pins for pair in rail_pairs}
        shown = _names(ics)
        caps = design.caps.get(rail, [])
        fitted = [cap for cap in caps if cap.fitted]
        unknown.update(cap.designator for cap in fitted if cap.farads is None)
        feeders = _feeders(design, rail)
        if fitted:
            if all(cap.farads is not None and not cap.small() for cap in fitted):
                findings.append(_finding(
                    "no_small_capacitor",
                    f"{rail} ({shown}) has only capacitors of 1 µF or more "
                    f"({_names(c.designator for c in fitted)}); MCU and logic guides usually "
                    "add a 100 nF ceramic per power pin, while many regulators ask only for "
                    "1-10 µF: check the datasheet.",
                    [*ics, *(cap.designator for cap in fitted)], rail=rail, ics=ics,
                ))
            elif not feeders and all(cap.farads is not None and cap.small() for cap in fitted):
                findings.append(_finding(
                    "no_bulk_capacitor",
                    f"{rail} ({shown}) has only capacitors under 1 µF; guides put a bulk "
                    "capacitor (1-10 µF or more) per rail or where it enters the board, "
                    "which may be elsewhere on this rail's source.",
                    [*ics, *(cap.designator for cap in fitted)], rail=rail, ics=ics,
                ))
            continue
        absent = [cap for cap in caps if not cap.fitted]
        if absent:
            findings.append(_finding(
                "not_fitted_only",
                f"{rail} ({shown}) is decoupled only by "
                f"{_names(c.designator for c in absent)}, which "
                f"{'is' if len(absent) == 1 else 'are'} Not Fitted in variant {variant}.",
                [*ics, *(cap.designator for cap in absent)], rail=rail, ics=ics,
                pins=pins,
            ))
            continue
        elsewhere = [
            cap for cap in design.between.get(rail, [])
            if cap.fitted and (cap.other in design.rails
                               or any(cap.other in design.ic_nets[ic] for ic in ics))
        ]
        if elsewhere:
            targets = ", ".join(
                f"{cap.designator} to {cap.other}" for cap in
                sorted(elsewhere, key=lambda cap: natural_key(cap.designator))[:6]
            )
            findings.append(_finding(
                "capacitor_elsewhere",
                f"{rail} ({shown}) has no capacitor to ground, only to other nets: "
                f"{targets} (a bootstrap or reference capacitor, or decoupling to another "
                "rail). Check that this is what the datasheet asks for.",
                [*ics, *(cap.designator for cap in elsewhere)], rail=rail, ics=ics,
            ))
        elif feeders:
            upstream = "; ".join(f"{part} from {other}" for part, other in feeders[:4])
            findings.append(_finding(
                "filtered_rail",
                f"{rail} ({shown}) is fed through {upstream} but has no capacitor of its "
                "own: the capacitors upstream sit behind the filter, so the pins get no "
                "local decoupling.",
                [*ics, *(part for part, _ in feeders)], rail=rail, ics=ics, pins=pins,
            ))
        else:
            findings.append(_finding(
                "no_capacitor",
                f"{rail} ({shown}) has no capacitor to ground in variant {variant}: "
                "every IC power pin usually wants one, close to the pin.",
                ics, rail=rail, ics=ics, pins=pins,
            ))
    if unknown:
        findings.append(_finding(
            "unknown_value",
            f"The value of {len(unknown)} decoupling capacitors could not be read "
            "(Value, Capacitance, Comment, description), so they are not counted as small "
            "or bulk.",
            unknown,
        ))
    return findings


# ---------- PCB ----------

def _pcb_findings(index, design: _Design, pairs: list[_Pair], max_mil: float,
                  stats: dict) -> list[dict]:
    db = index.db
    by_name: dict[str, dict] = {}
    for row in db.execute("SELECT * FROM components"):
        name = row["sch_refdes"]
        if name and name not in by_name:
            by_name[name] = dict(row)
    wanted = {pair.ic for pair in pairs}
    for pair in pairs:
        wanted.update(cap.designator for cap in design.caps.get(pair.rail, []))
    ids = [by_name[name]["id"] for name in wanted if name in by_name]
    pads: dict[int, list] = defaultdict(list)
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        rows = db.execute(
            "SELECT p.*, n.name AS net_name FROM prims p LEFT JOIN nets n ON n.id = p.net_id "
            f"WHERE p.kind = 'pad' AND p.component IN ({','.join('?' * len(chunk))})", chunk,
        )
        for row in rows:
            pads[row["component"]].append(row)
    links: dict[str, tuple[list, list]] = {}

    def links_of(net: str) -> tuple[list, list]:
        """Same-net vias and through-hole pads, and the net's pour outlines."""
        if net not in links:
            net_id = index.net_id(net)
            vias = list(db.execute(
                "SELECT * FROM prims WHERE net_id = ? AND (kind = 'via' OR "
                "(kind = 'pad' AND layer = ?))", (net_id, _THT_LAYER),
            )) if net_id is not None else []
            pours = [dict(row) for row in db.execute(
                "SELECT layer, vertices FROM pours WHERE net_id = ?", (net_id,),
            )] if net_id is not None else []
            links[net] = (vias, pours)
        return links[net]

    findings: list[dict] = []
    missing: set[str] = set()
    distances: list[float] = []
    for pair in pairs:
        ic_row = by_name.get(pair.ic)
        if ic_row is None:
            missing.add(pair.ic)
            continue
        on_rail = [pad for pad in pads[ic_row["id"]] if pad["net_name"] == pair.rail]
        power = [pad for pad in on_rail if pad["pad_name"] in pair.pins] or on_rail
        if not power:
            stats["net_differs"] += 1
            continue
        candidates = []  # (cap, pad)
        for cap in design.caps.get(pair.rail, []):
            row = by_name.get(cap.designator)
            if row is None:
                if cap.fitted:
                    missing.add(cap.designator)
                continue
            for pad in pads[row["id"]]:
                if pad["net_name"] == pair.rail:
                    candidates.append((cap, pad, row))
        fitted = [item for item in candidates if item[0].fitted]
        if not fitted:
            continue
        small = [item for item in fitted if item[0].small()]
        pool = small or fitted

        def nearest(pad, items):
            return min(((index._primitive_distance(pad, cap_pad), cap, cap_pad, row)
                        for cap, cap_pad, row in items), key=lambda item: item[0])

        per_pad = [nearest(pad, pool) for pad in power]
        best = min(per_pad, key=lambda item: item[0])
        distance, cap, cap_pad, cap_row = best
        stats["pairs_measured"] += 1
        distances.append(distance)
        nearest_cap = {**cap.describe(), "distance_mil": round(distance, 1),
                       "side": cap_row["side"]}
        basis = "capacitors under 1 µF" if small else "all fitted capacitors"
        absent = [item for item in candidates if not item[0].fitted]
        closer = None
        if absent:
            gap, absent_cap, _, _ = min(
                (nearest(pad, absent) for pad in power), key=lambda item: item[0],
            )
            if gap < distance:
                closer = {"designator": absent_cap.designator, "distance_mil": round(gap, 1)}
        if distance > max_mil:
            text = (
                f"{pair.ic} on {pair.rail}: the nearest capacitor ({basis}) is "
                f"{cap.designator} ({_farads_text(cap.farads) or 'value unknown'}), "
                f"{distance / MIL_PER_MM:.1f} mm from its nearest power pad (straight line, "
                "pad edge to pad edge). Vendor guides ask for 'as close as possible', often "
                "a few mm; on boards with closely spaced planes the via connection matters "
                "more than the distance."
            )
            if closer:
                text += f" {closer['designator']} would be nearer but is Not Fitted."
            findings.append(_finding(
                "far_capacitor", text, [pair.ic, cap.designator], ic=pair.ic,
                rail=pair.rail, nearest=nearest_cap, ic_side=ic_row["side"],
                not_fitted_nearer=closer,
            ))
        elif len(power) > 1:
            far = [item for item in per_pad if item[0] > max_mil]
            if far:
                worst = max(far, key=lambda item: item[0])
                findings.append(_finding(
                    "pads_far",
                    f"{pair.ic} on {pair.rail}: {len(far)} of {len(power)} power pads have "
                    f"no capacitor within {max_mil / MIL_PER_MM:.1f} mm (the farthest "
                    f"{worst[0] / MIL_PER_MM:.1f} mm, to {worst[1].designator}). Large parts "
                    "often share capacitors between pads; check the datasheet's per-pin "
                    "advice.",
                    [pair.ic], ic=pair.ic, rail=pair.rail, pads_far=len(far),
                    pads=len(power), farthest_mil=round(worst[0], 1),
                ))
        if cap_row["side"] != ic_row["side"] and cap_pad["layer"] != _THT_LAYER:
            vias, pours = links_of(pair.rail)
            via_near = any(
                via["id"] != cap_pad["id"]
                and index._primitive_distance(cap_pad, via) <= VIA_NEAR_MIL
                for via in vias
            )
            poured = any(
                pour["layer"] == cap_pad["layer"]
                and point_in_polygon(cap_pad["x1"], cap_pad["y1"], json.loads(pour["vertices"]))
                for pour in pours
            )
            if not via_near and not poured:
                findings.append(_finding(
                    "opposite_side_no_via",
                    f"{cap.designator}, the nearest capacitor to {pair.ic} on {pair.rail}, "
                    f"is on the other side ({cap_row['side']}) and no {pair.rail} via was "
                    "found within 2 mm of its pad, nor a pour around it (regions and planes "
                    "are not read, so the connection may still be there).",
                    [pair.ic, cap.designator], ic=pair.ic, rail=pair.rail,
                    nearest=nearest_cap,
                ))
    if distances:
        stats["within_mm"] = {
            f"{mm}": sum(1 for value in distances if value <= mm * MIL_PER_MM)
            for mm in (1, 3, 5, 10)
        }
    if missing:
        stats["not_on_pcb"] = sorted(missing, key=natural_key)[:30]
        stats["not_on_pcb_count"] = len(missing)
    return findings


# ---------- entry point ----------

def _match(requested: list[str] | None, available) -> tuple[set[str] | None, list[str]]:
    if not requested:
        return None, []
    folded = {name.casefold(): name for name in available}
    found = {folded[name.strip().casefold()] for name in requested
             if name.strip().casefold() in folded}
    unknown = [name for name in requested if name.strip().casefold() not in folded]
    return found, unknown


def run_decoupling_check(
    netlist: dict,
    plan=None,
    project_params: dict | None = None,
    index=None,
    pcb_unavailable: str | None = None,
    checks: list[str] | None = None,
    ics: list[str] | None = None,
    rails: list[str] | None = None,
    max_distance_mil: float = DEFAULT_MAX_DISTANCE_MIL,
    offset: int = 0,
    limit: int = FINDING_LIMIT,
    rail_limit: int = RAIL_LIMIT,
) -> dict:
    requested = [check.strip().lower() for check in checks or [] if check.strip()]
    invalid = [check for check in requested if check not in CHECKS]
    if invalid:
        return {"error": "invalid_checks",
                "message": f"Unknown checks {invalid}; use {list(CHECKS)}."}
    design = _design(netlist, plan, project_params)
    pairs = design.pairs
    notes: list[str] = []
    wanted_ics, unknown_ics = _match(ics, design.ics | {pair.ic for pair in pairs}
                                     | set(netlist.get("pin_to_net") or {}))
    if ics and not wanted_ics:
        return {"error": "ic_not_found",
                "message": f"No part {unknown_ics} in the compiled netlist."}
    if wanted_ics is not None:
        if extra := wanted_ics - design.ics:
            # A part named explicitly is checked like an IC (e.g. a connector or module).
            design.ics |= extra
            pin_to_net = netlist.get("pin_to_net") or {}
            for part in sorted(extra, key=natural_key):
                for pin, net in pin_to_net.get(part, {}).items():
                    design.ic_nets[part].add(net)
                    if net in design.rails and "name" in design.rails[net]:
                        pair = next((p for p in pairs if (p.ic, p.rail) == (part, net)), None)
                        if pair is None:
                            pair = _Pair(part, net)
                            pairs.append(pair)
                        pair.pins.append(pin)
        pairs = [pair for pair in pairs if pair.ic in wanted_ics]
    wanted_rails, unknown_rails = _match(rails, design.rails)
    if rails and not wanted_rails:
        return {"error": "rail_not_found",
                "message": f"No power rail {unknown_rails}; rails are named or reach an IC "
                "supply pin."}
    if wanted_rails is not None:
        pairs = [pair for pair in pairs if pair.rail in wanted_rails]
    if unknown_ics or unknown_rails:
        notes.append(f"Not found: {', '.join([*unknown_ics, *unknown_rails])}.")
    variant = getattr(plan, "name", None) or "[No Variations]"
    findings = _schematic_findings(design, pairs, variant)
    stats: dict = defaultdict(int)
    not_checked: dict[str, str] = {}
    if index is not None:
        findings += _pcb_findings(index, design, pairs, max_distance_mil, stats)
    else:
        for check in CHECKS:
            if check in PCB_CHECKS:
                not_checked[check] = pcb_unavailable or "no PcbDoc"
    wanted = set(requested or set(CHECKS) - OPT_IN_CHECKS)
    findings = [finding for finding in findings if finding["check"] in wanted]
    findings.sort(key=lambda finding: (
        _SEVERITY_RANK[finding["severity"]], _CHECK_RANK[finding["check"]],
        natural_key(finding.get("rail") or ""), natural_key(finding.get("ic") or ""),
    ))
    rail_rows = _rail_rows(design, pairs)
    fitted_caps = {cap.designator for rail in {p.rail for p in pairs}
                   for cap in design.caps.get(rail, []) if cap.fitted}
    absent_caps = {cap.designator for rail in {p.rail for p in pairs}
                   for cap in design.caps.get(rail, []) if not cap.fitted}
    overview = {
        "ics": len({pair.ic for pair in pairs}),
        "rails": len(rail_rows),
        "ic_rail_pairs": len(pairs),
        "decoupling_capacitors": len(fitted_caps),
        "not_fitted_capacitors": len(absent_caps),
    }
    severities = {level: sum(1 for f in findings if f["severity"] == level)
                  for level in ("warning", "info")}
    summary: dict[str, int] = defaultdict(int)
    for finding in findings:
        summary[finding["check"]] += 1
    offset = max(0, int(offset or 0))
    result: dict = {
        "variant": variant,
        "checks_run": [check for check in CHECKS
                       if check in wanted and check not in not_checked],
        "overview": overview,
        "summary": dict(summary),
        "severity_counts": severities,
    }
    if index is not None:
        result["pcb"] = {
            "pairs_measured": stats["pairs_measured"],
            "within_mm": stats.get("within_mm", {}),
            "max_distance_mil": round(max_distance_mil, 3),
            "measured": "straight line, pad edge to pad edge, from each IC power pad to "
                        "the nearest fitted capacitor on its rail (under 1 µF when there "
                        "is one)",
        }
        for key in ("not_on_pcb", "not_on_pcb_count"):
            if key in stats:
                result["pcb"][key] = stats[key]
        if stats["net_differs"]:
            result["pcb"]["pairs_with_other_net_on_pcb"] = stats["net_differs"]
            notes.append("Some IC power pins have another net on the PCB than in the "
                         "netlist: run check_sch_pcb_sync.")
    result["rails_total"] = len(rail_rows)
    result["rails"] = rail_rows[:rail_limit]
    result.update({
        "findings": findings[offset:offset + limit],
        "total": len(findings),
        "offset": offset,
        "has_more": offset + limit < len(findings),
    })
    if result["has_more"]:
        result["next_offset"] = offset + limit
    if not_checked:
        result["not_checked"] = not_checked
    notes.append(
        "Rails are nets named like power or reaching an IC supply, reference or capacitor "
        "pin; ICs are U and IC parts (pass ic= for others, e.g. a connector). How many "
        "capacitors a part needs is in its datasheet, not checked here."
    )
    result["notes"] = notes
    return result


def _rail_rows(design: _Design, pairs: list[_Pair]) -> list[dict]:
    by_rail: dict[str, list[_Pair]] = defaultdict(list)
    for pair in pairs:
        by_rail[pair.rail].append(pair)
    rows = []
    for rail, rail_pairs in by_rail.items():
        caps = design.caps.get(rail, [])
        fitted = [cap for cap in caps if cap.fitted]
        row: dict = {
            "rail": rail,
            "evidence": design.rails.get(rail, []),
            "ics": sorted({pair.ic for pair in rail_pairs}, key=natural_key),
            "power_pins": sum(len(pair.pins) for pair in rail_pairs),
            "capacitors": len(fitted),
            "under_1uF": sum(1 for cap in fitted if cap.small()),
            "from_1uF": sum(1 for cap in fitted if cap.farads is not None and not cap.small()),
        }
        total = sum(cap.farads for cap in fitted if cap.farads is not None)
        if total:
            row["total_uF"] = round(total * 1e6, 3)
        if unknown := sum(1 for cap in fitted if cap.farads is None):
            row["value_unknown"] = unknown
        if absent := [cap.designator for cap in caps if not cap.fitted]:
            row["not_fitted"] = sorted(absent, key=natural_key)
        if feeders := _feeders(design, rail):
            row["fed_through"] = [f"{part} from {other}" for part, other in feeders[:4]]
        rows.append(row)
    rows.sort(key=lambda row: (-row["power_pins"], natural_key(row["rail"])))
    return rows
