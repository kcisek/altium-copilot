"""Voltage-aware copper spacing screen (check_hv_clearance), offline: the smallest
copper-to-copper distance on each layer between nets at different potentials, against
the spacing a standard asks for at that voltage, with where it occurs.

Net voltages. check_derating's resolution (rails= given by the user wins; grounds are
0 V; a voltage in the net's name; the power tree: outputs a given Vref sets, rails joined
through 0 ohm links, ferrites, fuses and inductors, unloaded divider midpoints), plus hv=
for working voltages that are not DC rails ('L=230AC,N=0,HV_BUS=400'; hv= wins over
rails=). A converter's switch node swings between 0 V and the voltage it peaks at (its
input for a buck, its output for a boost, or the value given in rails= or hv=), so it
counts as half that plus a square wave of half that; one whose voltage is unknown is
listed to ask about. AC is given as RMS (230AC, 230VAC, 230Vrms) and converted to peak
(x sqrt 2), as both tables use peak voltage; '325pk' gives a peak, '@120' a phase in
degrees (AC nets are in phase unless a phase says otherwise). Each voltage keeps its
source and confidence. The voltage between two nets is |Va - Vb| on their DC parts plus
the peak of the difference of their AC parts, when both are known. groups= declares
isolated groups ('PRI:L,N,HV_BUS,PGND;SEC:GND,3V3', '*' wildcards), each with its own
reference: a pair across groups takes working_voltage (the barrier's working voltage,
'250AC' or '400'), or is unknown without it (a note says so). Every PCB net no group
names is in the board's own group. A pair with an unknown voltage is never judged: it is
listed when the other net is a known high-voltage one, with what it would need if the
unknown net sat at its reference.

Pairs. A pair is judged when its voltage is above threshold (default 15 V: every table's
lowest band is tiny). Its copper is searched from the side that is far from its group's
reference: a pair above threshold has a net at more than half of it (|DC| + AC peak), so
only those nets' copper (and, across groups with a working voltage, every group's nets
but the largest group's) is searched, each within twice the largest requirement it can
meet (at most 5 mm more; a user figure for a net it pairs with counts), on every signal
layer (or layer=), against every other net's copper (net= keeps the pairs of those nets
only, and searches only for them):
tracks, arcs, pads and vias with their copper on that layer, hatched pours' tracks,
poured regions, free regions and fills (dfm_geometry.net_gaps, check_dfm's copper
model). Shelved pours and pours never poured hold no copper here; internal plane layers
are not measured (their clearance is the Power Plane Clearance rule's).

Requirement per pair and layer, from the pair's peak voltage:
- standard 'ipc2221' (default, a bare board): IPC-2221B Table 6-1, internal layers B1,
  external layers B2 (uncoated, up to 3050 m), B3 above 3050 m (altitude=), B4 with a
  permanent polymer coating (coating=true); above 500 V the per-volt value times the
  volts above 500 is added to the 301-500 V value.
- standard 'ipc2221_assembly': the A columns on external layers, A6 (uncoated) or A7
  (coated) when a component pad is one side, A5 for other conductors when coated (B2 or
  B3 uncoated); above 3050 m an uncoated lead takes the larger of A6 and B3 (the table
  has no A column for it); internal layers B1.
- standard 'ipc9592': IPC-9592B's spacing for power conversion devices, by peak
  voltage, any layer: 0.13 mm below 15 V, 0.25 mm from 15 to 30 V, 0.1 + 0.01 x V mm
  from 30 to 100 V, 0.6 + 0.005 x V mm from 100 V (the standard covers below 500 V; the
  formula is carried on above it).
- min_distance= replaces the standard where it applies (a pair, every pair of a net, or
  every pair), for the pairs judged and for the requirement listed beside an unknown
  pair: IEC 62368-1 and IEC 60664-1 clearances and creepages depend on overvoltage
  category, pollution degree, material group and insulation grade, so they are not
  encoded: the user gives them this way.
A voltage between two rows of Table 6-1 (15.5 V) takes the higher row.

Table sources (the table is copyrighted by IPC and only shown as an image in Altium's
'IPC-2221 calculator for high voltage design' article, so every cell was taken from public
reproductions and cross-checked):
- Altair PollEx help, 'IPC Material' (Electrical Clearance, 6.3): the whole table, the
  rule above 500 V and its example (B1 at 600 V: 0.25 + 100 x 0.0025 = 0.50 mm).
- EMA Design Automation, 'PCB Clearance and Creepage Distance Table': the whole table,
  every cell equal to Altair's.
- Sierra Circuits (protoexpress.com), 'IPC-2221 circuit board design': both tables; two
  cells differ (B3 151-170 V 6.4, B1 above 500 V 0.025 mm/V) where three other sources
  agree (3.2, 0.0025).
- philipmcgaw.com, 'Creepage and clearance': the whole table; one cell differs (B2
  251-300 V 2.5, where three others give 1.25) and it multiplies the per-volt values by
  the whole voltage, which the worked examples of Altair and Siemens contradict.
- smps.us / smpspowersupply.com (L. Rozenblat): B1, B2, B4 by voltage, 2.5 + (V - 500) x
  0.005 mm above 500 V; viox.com's calculator: every column's 500 V value and per-volt
  increment with d = d500 + k x (V - 500); Siemens EDA blog 'PCB high voltage spacing'
  (2025): the rule above 500 V with the same B1 example.
IPC-9592B: Altium's 'IPC-2221 calculator for high voltage design' (its table, by peak
voltage) and smps.us / smpspowersupply.com (the step values, and 0.6 + 0.005 x Vpeak
kept for 100 V and above), which agree.

A screening tool: spacing standards and the product's safety standard decide. Distances
are straight lines between copper on one layer (clearance); creepage along the surface
around slots and cutouts is not computed: the straight-line copper spacing is its lower
bound, and slots, holes and cutouts in the path are listed because they lengthen it and
are not credited.
"""

import fnmatch
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

try:
    from services.bom import natural_key
    from services.decoupling import net_tokens
    from services.derating import net_voltages, read_parts
    from services.dfm_geometry import copper_model, net_gaps, openings_near
    from services.pcb_compliance import CLEARANCE_TOLERANCE_MIL, resolve_clearance_rule
    from services.pcb_rules import RuleTarget, rule_values
    from services.power_tree import build_tree, tree_data
except ImportError:
    from server.services.bom import natural_key
    from server.services.decoupling import net_tokens
    from server.services.derating import net_voltages, read_parts
    from server.services.dfm_geometry import copper_model, net_gaps, openings_near
    from server.services.pcb_compliance import CLEARANCE_TOLERANCE_MIL, resolve_clearance_rule
    from server.services.pcb_rules import RuleTarget, rule_values
    from server.services.power_tree import build_tree, tree_data

CHECKS = ("spacing_below_standard", "spacing_below_rule", "unknown_voltage_pairs",
          "creepage_not_computed")
_CHECK_RANK = {check: rank for rank, check in enumerate(CHECKS)}
_SEVERITY_RANK = {"warning": 0, "info": 1}
STANDARDS = ("ipc2221", "ipc2221_assembly", "ipc9592")
PAGE_LIMIT = 100
CACHE_SIZE = 4  # prepared results kept per index (prepare_hv_cached)
MIL_PER_MM = 1 / 0.0254
DEFAULT_THRESHOLD_V = 15.0
B3_ALTITUDE_M = 3050.0
SEARCH_MARGIN_MAX_MIL = 5.0 * MIL_PER_MM  # the search reaches past a requirement by it, at most
OPENING_NEAR_MIL = 1.0 * MIL_PER_MM  # slots and cutouts this near a gap's path are listed
UNKNOWN_PAIRS_LISTED = 25
UNKNOWN_NETS_LISTED = 20
HV_NETS_LISTED = 20
_SQRT2 = math.sqrt(2.0)

# ---------- IPC-2221B Table 6-1 ----------

# Upper limit of each row (V, DC or AC peak) and its label; above the last row the
# per-volt value is added for every volt above 500.
IPC2221_LIMITS_V = (15.0, 30.0, 50.0, 100.0, 150.0, 170.0, 250.0, 300.0, 500.0)
IPC2221_ROWS = ("0-15 V", "16-30 V", "31-50 V", "51-100 V", "101-150 V", "151-170 V",
                "171-250 V", "251-300 V", "301-500 V")
# Minimum conductor spacing, mm, per row (sources: the module docstring).
IPC2221_MM = {
    "B1": (0.05, 0.05, 0.1, 0.1, 0.2, 0.2, 0.2, 0.2, 0.25),
    "B2": (0.1, 0.1, 0.6, 0.6, 0.6, 1.25, 1.25, 1.25, 2.5),
    "B3": (0.1, 0.1, 0.6, 1.5, 3.2, 3.2, 6.4, 12.5, 12.5),
    "B4": (0.05, 0.05, 0.13, 0.13, 0.4, 0.4, 0.4, 0.4, 0.8),
    "A5": (0.13, 0.13, 0.13, 0.13, 0.4, 0.4, 0.4, 0.4, 0.8),
    "A6": (0.13, 0.25, 0.4, 0.5, 0.8, 0.8, 0.8, 0.8, 1.5),
    "A7": (0.13, 0.13, 0.13, 0.13, 0.4, 0.4, 0.4, 0.8, 0.8),
}
IPC2221_PER_VOLT_MM = {"B1": 0.0025, "B2": 0.005, "B3": 0.025, "B4": 0.00305,
                       "A5": 0.00305, "A6": 0.00305, "A7": 0.00305}
IPC2221_COLUMNS = {
    "B1": "internal conductors, any elevation",
    "B2": "external conductors, uncoated, sea level to 3050 m",
    "B3": "external conductors, uncoated, above 3050 m",
    "B4": "external conductors with a permanent polymer coating, any elevation",
    "A5": "external conductors with conformal coating over the assembly, any elevation",
    "A6": "external component leads and terminations, uncoated, sea level to 3050 m",
    "A7": "external component leads and terminations with conformal coating, any elevation",
}
_STANDARD_NAMES = {"ipc2221": "IPC-2221B Table 6-1",
                   "ipc2221_assembly": "IPC-2221B Table 6-1 (assembly columns)",
                   "ipc9592": "IPC-9592B"}

SCREENING = (
    "A screening check, not a safety assessment: the product's spacing and safety "
    "standards decide. IPC-2221B Table 6-1 and IPC-9592B are functional spacing minimums, "
    "not safety isolation (reinforced or basic insulation to IEC 62368-1 or IEC 60664-1 "
    "needs its own clearance and creepage: give it as min_distance). Distances are "
    "straight lines between copper on one layer; creepage along the surface around slots "
    "and cutouts is not computed. Voltages from names and the power tree are guesses: for "
    "high voltage, give them in hv= or rails=."
)
SOURCES = (
    "IPC-2221B Table 6-1 values from public reproductions, every cell in at least two "
    "agreeing sources: Altair PollEx help 'IPC Material' and EMA Design Automation's 'PCB "
    "Clearance and Creepage Distance Table' (identical), with Sierra Circuits' and "
    "philipmcgaw.com's tables outvoted where they differ (three cells); the rule above 500 V "
    "(the per-volt value times the volts above 500, added to the 301-500 V value) from "
    "Altair's and Siemens EDA's worked example (B1 at 600 V: 0.50 mm), viox.com's "
    "calculator and smpspowersupply.com. IPC-9592B from Altium's 'IPC-2221 calculator for "
    "high voltage design' and smps.us, which agree."
)


def ipc2221_mm(column: str, volts: float) -> tuple[float, str]:
    """(minimum spacing in mm, row label) of an IPC-2221B Table 6-1 column at a peak
    voltage: the first row whose upper limit the voltage does not pass, and above 500 V
    the 301-500 V value plus the per-volt value for every volt above 500."""
    values = IPC2221_MM[column]
    for limit, label, value in zip(IPC2221_LIMITS_V, IPC2221_ROWS, values):
        if volts <= limit:
            return value, label
    per_volt = IPC2221_PER_VOLT_MM[column]
    return (values[-1] + per_volt * (volts - IPC2221_LIMITS_V[-1]),
            f">500 V ({values[-1]:g} mm + {per_volt:g} mm/V above 500 V)")


def ipc9592_mm(volts: float) -> tuple[float, str]:
    """(minimum spacing in mm, rule) of IPC-9592B at a peak voltage."""
    if volts < 15.0:
        return 0.13, "Vpeak < 15 V"
    if volts < 30.0:
        return 0.25, "15 V <= Vpeak < 30 V"
    if volts < 100.0:
        return 0.1 + 0.01 * volts, "30 V <= Vpeak < 100 V: 0.1 + 0.01 x Vpeak"
    return 0.6 + 0.005 * volts, "Vpeak >= 100 V: 0.6 + 0.005 x Vpeak"


@dataclass(frozen=True)
class Options:
    """How requirements are looked up."""

    standard: str = "ipc2221"
    coating: bool = False
    altitude_m: float = 0.0

    def columns(self, layer_type: str, lead: bool) -> tuple[str, ...]:
        """The Table 6-1 columns for copper on a layer type ('inner' or 'outer'), lead
        when a component pad is one side; the larger requirement of several applies."""
        if layer_type == "inner":
            return ("B1",)
        high = self.altitude_m > B3_ALTITUDE_M
        if self.standard == "ipc2221_assembly":
            if lead:
                if self.coating:
                    return ("A7",)
                return ("A6", "B3") if high else ("A6",)
            if self.coating:
                return ("A5",)
        if self.coating:
            return ("B4",)
        return ("B3",) if high else ("B2",)

    def requirement(self, volts: float, layer_type: str, lead: bool) -> dict:
        """{standard, column, band, required_mm} at a peak voltage."""
        if self.standard == "ipc9592":
            value, band = ipc9592_mm(volts)
            return {"standard": _STANDARD_NAMES["ipc9592"], "column": None, "band": band,
                    "required_mm": value}
        best = None
        for column in self.columns(layer_type, lead):
            value, band = ipc2221_mm(column, volts)
            if best is None or value > best[0]:
                best = (value, band, column)
        return {"standard": _STANDARD_NAMES[self.standard], "column": best[2],
                "band": best[1], "required_mm": best[0]}

    def widest_mm(self, volts: float) -> float:
        """The largest requirement at a voltage over every layer type and side."""
        return max(self.requirement(volts, layer_type, lead)["required_mm"]
                   for layer_type in ("inner", "outer") for lead in (False, True))


# ---------- voltages ----------

@dataclass(frozen=True)
class Level:
    """A net's voltage against its group's reference: a DC part and an AC part (peak, in
    phase with the others unless phase says otherwise), with its source and confidence."""

    dc: float
    ac_peak: float = 0.0
    phase: float = 0.0
    source: str = "hv"  # hv, user (rails=), ground, name, power_tree, divider
    confidence: str = "high"
    basis: str = ""
    switching: bool = False  # a switch node: half its peak plus a square wave of half

    @property
    def extreme(self) -> float:
        """The largest |V| it reaches against the reference."""
        return abs(self.dc) + self.ac_peak

    def text(self) -> str:
        if self.switching:
            return f"0 to {_v(self.dc + math.copysign(self.ac_peak, self.dc))} V (switch node)"
        if self.ac_peak:
            ac = f"{_v(self.ac_peak / _SQRT2)} V AC ({_v(self.ac_peak)} V peak" + (
                f", {self.phase:g} deg)" if self.phase else ")")
            return ac if not self.dc else f"{_v(self.dc)} V DC + {ac}"
        return f"{_v(self.dc)} V"

    def swinging(self) -> "Level":
        """As a switch node: from 0 V to its DC value (half of it plus a square wave of
        half)."""
        half = abs(self.dc) / 2.0
        return Level(math.copysign(half, self.dc), half, 0.0, self.source, self.confidence,
                     self.basis, switching=True)


def _v(volts: float) -> str:
    return f"{round(volts, 2):g}"


def pair_peak(a: Level, b: Level) -> float:
    """The peak voltage between two nets: their DC difference plus the peak of the
    difference of their AC parts (a switch node's square wave is in no phase with
    anything: the two AC peaks add)."""
    if a.switching or b.switching:
        return abs(a.dc - b.dc) + a.ac_peak + b.ac_peak
    swing = math.sqrt(max(0.0, a.ac_peak ** 2 + b.ac_peak ** 2 - 2.0 * a.ac_peak * b.ac_peak
                          * math.cos(math.radians(a.phase - b.phase))))
    return abs(a.dc - b.dc) + swing


_LEVEL = re.compile(
    r"([+-]?\d+(?:\.\d+)?)(K?V)?(ACRMS|ACPK|ACPEAK|AC|RMS|PK|PEAK|DC)?(?:@([+-]?\d+(?:\.\d+)?))?")
_LEVEL_HINT = ("e.g. '400', '-48', '230AC' or '230VAC' (RMS, taken as 325 V peak), "
               "'325pk' (an AC peak), '230AC@120' (a phase in degrees) or '1.2kV'")
MAX_VOLTS = 20000.0


def parse_level(text: str) -> tuple[float, float, float] | None:
    """'230AC' -> (0, 325.27, 0); '400' -> (400, 0, 0); '325pk' -> (0, 325, 0); '-48' ->
    (-48, 0, 0); '230AC@120' -> (0, 325.27, 120): (dc, ac peak, phase), or None."""
    match = _LEVEL.fullmatch(re.sub(r"\s+", "", str(text)).upper())
    if not match:
        return None
    volts = float(match[1]) * (1000.0 if match[2] == "KV" else 1.0)
    kind = match[3] or "DC"
    if match[4] and kind == "DC":
        return None
    if kind == "DC":
        return (volts, 0.0, 0.0) if abs(volts) <= MAX_VOLTS else None
    if volts < 0:
        return None
    peak = volts if kind in ("ACPK", "ACPEAK", "PK", "PEAK") else volts * _SQRT2
    phase = float(match[4]) if match[4] else 0.0
    return (0.0, peak, phase) if peak <= MAX_VOLTS * _SQRT2 else None


def parse_hv(text: str | None) -> tuple[dict[str, tuple] | None, dict | None]:
    """'L=230AC,N=0,HV_BUS=400' -> {name: (dc, ac peak, phase)}; names listed before a
    value share it ('L,L_F=230AC'). (None, None) when empty."""
    error = {"error": "invalid_hv",
             "message": "hv takes net working voltages, e.g. 'L=230AC,N=0,HV_BUS=400' (names "
                        f"before a value share it), each {_LEVEL_HINT}, up to 20 kV."}
    if text is None or not str(text).strip():
        return None, None
    out: dict[str, tuple] = {}
    pending: list[str] = []
    for item in re.split(r"[,;]", str(text)):
        item = item.strip()
        if not item:
            continue
        name, sep, value = item.partition("=")
        if not sep:
            if ":" in item:
                return None, error
            pending.append(item)
            continue
        level = parse_level(value)
        if level is None or not name.strip():
            return None, error
        for each in [*pending, name.strip()]:
            out[each] = level
        pending = []
    if pending or not out:
        return None, error
    return out, None


def parse_working_voltage(text) -> tuple[float | None, dict | None]:
    """The peak working voltage across declared groups ('250AC' -> 353.6, '400')."""
    if text is None or not str(text).strip():
        return None, None
    level = parse_level(text)
    if level is None or not (abs(level[0]) + level[1]) > 0:
        return None, {"error": "invalid_working_voltage",
                      "message": f"working_voltage is the voltage across the groups, {_LEVEL_HINT}."}
    return abs(level[0]) + level[1], None


def parse_groups(text: str | None) -> tuple[list[tuple[str, list[str]]] | None, dict | None]:
    """'PRI:L,N,HV_BUS,PGND;SEC:GND,3V3' -> [('PRI', [...]), ('SEC', [...])]; a group
    without a name is G1, G2...; '*' and '?' are wildcards."""
    error = {"error": "invalid_groups",
             "message": "groups takes isolated groups of nets separated by ';', each "
                        "'NAME:net,net' or 'net,net', with * and ? wildcards, e.g. "
                        "'PRI:L,N,HV_*,PGND;SEC:GND,3V3'."}
    if text is None or not str(text).strip():
        return None, None
    out: list[tuple[str, list[str]]] = []
    for number, chunk in enumerate(str(text).split(";"), start=1):
        if not chunk.strip():
            continue
        name, sep, rest = chunk.partition(":")
        if not sep:
            name, rest = f"G{number}", chunk
        names = [part.strip() for part in rest.split(",") if part.strip()]
        name = name.strip()
        if not name or not names or any(name == other for other, _ in out):
            return None, error
        out.append((name, names))
    return (out, None) if out else (None, error)


def parse_min_distance(text, mil_per_unit: float) -> tuple[dict | None, dict | None]:
    """'2.5' (every pair judged), 'HV_BUS=4' (every pair of that net), 'L&N=3' (that
    pair), comma-separated, in the call's units -> {'all': mil, 'nets': {...}, 'pairs':
    {...}}."""
    error = {"error": "invalid_min_distance",
             "message": "min_distance takes a spacing in units for every pair judged ('2.5'), "
                        "per net ('HV_BUS=4') or per pair ('L&N=3'), comma-separated, "
                        "each a positive number."}
    if text is None or not str(text).strip():
        return None, None
    out: dict = {"all": None, "nets": {}, "pairs": {}}
    for item in str(text).split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, value = item.rpartition("=")
        try:
            amount = float(value)
        except ValueError:
            return None, error
        if not (math.isfinite(amount) and amount > 0):
            return None, error
        mil = amount * mil_per_unit
        if not sep:
            out["all"] = mil
        elif "&" in name:
            first, _, second = name.partition("&")
            if not first.strip() or not second.strip():
                return None, error
            out["pairs"][(first.strip(), second.strip())] = mil
        elif name.strip():
            out["nets"][name.strip()] = mil
        else:
            return None, error
    return out, None


# ---------- HV-looking nets ----------

# Words that say high voltage anywhere in a name, and short ones only as the whole name or
# followed by one word of a mains line's own (L, N_F, AC_IN, LINE_L), so a differential
# pair's TX_N, an enable L_EN or a status AC_OK is not one.
_HV_WORDS = frozenset({
    "HV", "HVDC", "HVAC", "HVBUS", "HVIN", "HVOUT", "VHV", "MAINS", "LIVE", "NEUTRAL",
    "PHASE", "DCLINK", "DCBUS", "BULK", "VBULK", "PFC", "PRI", "PRIMARY",
})
_HV_LEAD_WORDS = frozenset({"L", "N", "L1", "L2", "L3", "PE", "AC", "ACL", "ACN", "ACIN",
                            "VAC", "LINE"})
_HV_LEAD_SUFFIXES = _HV_LEAD_WORDS | frozenset({
    "F", "FILT", "FILTER", "FILTERED", "IN", "INPUT", "RAW", "FUSED", "FUSE", "EMI", "PROT",
    "RECT", "HOT", "LIVE", "NEUTRAL", "MAINS", "1", "2", "3",
})
# Words split by a separator count too (DC_LINK, HV-BUS).
_HV_COMPOUNDS = ("DCLINK", "DCBUS", "HVDC", "HVBUS")
# Volts as a word of their own: 400V_BUS and 230VAC, not U100VCC.
_HIGH_VOLTS_IN_NAME = re.compile(r"(?<![0-9.])(\d{2,5})(?:V|VAC|VDC|VRMS)(?![0-9A-Z])")
_KILOVOLTS_IN_NAME = re.compile(r"(?<![0-9.])\d+(?:[.P]\d+)?KV(?![0-9A-Z])")
_MAINS_CONNECTOR_TEXT = re.compile(
    r"mains|IEC ?60320|power (?:entry|inlet)|\bAC (?:inlet|input|power)|line cord", re.I)
_SURGE_PART_TEXT = re.compile(r"varistor|\bMOV\b|\bVDR\b|gas discharge|\bGDT\b", re.I)
_SAFETY_CAP_TEXT = re.compile(
    r"class[- ]?[XY]\d?\b|\b[XY][12]\b|safety cap|across[- ]the[- ]line|line[- ]bypass|"
    r"\b(?:2[3-9]\d|[3-9]\d\d)\s?V\s?AC\b", re.I)


def hv_name_reason(net: str) -> str | None:
    """Why a net's name looks like high voltage (a word such as HV or MAINS; L, N, PE, AC
    or LINE as the whole name or before a mains line's word such as L_F or AC_IN; 60 V or
    more, or kilovolts, as a word of the name), or None."""
    upper = net.upper()
    match = _HIGH_VOLTS_IN_NAME.search(upper)
    if match and int(match[1]) >= 60:
        return f"its name holds {int(match[1])} V"
    if _KILOVOLTS_IN_NAME.search(upper):
        return "its name holds kilovolts"
    tokens = [token.strip("+-") for token in net_tokens(net) if token.strip("+-")]
    joined = re.sub(r"[^A-Z0-9]", "", upper)
    words = sorted((set(tokens) & _HV_WORDS)
                   | {word for word in _HV_COMPOUNDS if word in joined})
    if tokens and tokens[0] in _HV_LEAD_WORDS and (
            len(tokens) == 1 or (len(tokens) == 2 and tokens[1] in _HV_LEAD_SUFFIXES)):
        words = sorted({*words, tokens[0]})
    if words:
        return f"its name holds {', '.join(words)}"
    return None


def _hv_looking(tree, names: set[str]) -> dict[str, list[str]]:
    """Net name -> reasons it may carry high voltage: its name, a power input on a
    connector, a mains connector's pin, a varistor's or X/Y safety capacitor's pin, a
    converter's switch node (it swings as far as the converter's input or output)."""
    found: dict[str, list[str]] = defaultdict(list)
    for name in names:
        reason = hv_name_reason(name)
        if reason:
            found[name].append(reason)
    for source in tree.sources:
        if source.kind == "connector" and not source.usb and source.net in names:
            found[source.net].append(f"a power input on {source.part}")
    for conv in tree.converters.values():
        for net in conv.switch_nodes:
            if conv.fitted and net in names:
                found[net].append(f"the switch node of {conv.ref} ({conv.kind})")
    ctx = tree.ctx
    for ref, kind in ctx.kind.items():
        if not ctx.fitted.get(ref, True):
            continue
        text = ctx.text.get(ref, "")
        if kind == "connector" and _MAINS_CONNECTOR_TEXT.search(text):
            why = f"a pin of mains connector {ref}"
        elif (kind == "capacitor" and _SAFETY_CAP_TEXT.search(text)) or (
                kind in ("esd", "passive", "resistor", "other")
                and _SURGE_PART_TEXT.search(text)):
            why = f"a pin of {ref}, a mains-rated part"
        else:
            continue
        for net in set((tree.design.part_pins.get(ref) or {}).values()):
            if net in names and net not in tree.grounds and why not in found[net]:
                found[net].append(why)
    return dict(found)


# ---------- the check ----------

def _resolve(names, universe: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Given names -> the real (casefolded match) names; the unmatched ones."""
    out, missing = {}, []
    for name in names:
        real = universe.get(name.strip().casefold())
        if real is None:
            missing.append(name)
        else:
            out[name] = real
    return out, missing


def _finding(check: str, severity: str, issue: str, **fields) -> dict:
    out: dict = {"check": check, "severity": severity}
    out.update({key: value for key, value in fields.items() if value not in (None, [], {})})
    out["issue"] = issue
    return out


def _length(mil: float, units: str) -> str:
    return f"{mil:.1f} mil" if units == "mil" else f"{mil / MIL_PER_MM:.2f} mm"


# Clearance matrix object names of copper (ClearanceObj_<name>).
_COPPER_OBJECTS = frozenset({"Track", "Arc", "SMDPad", "THPad", "Via", "Fill", "Poly",
                             "Region"})


def _rule_brief(rule: dict | None) -> dict | None:
    """A board rule entry (_Board.rule) for a finding: its name (or its candidates')
    and the least spacing it may allow."""
    if not rule:
        return None
    names = rule["rule"] if rule.get("rule") else ", ".join(
        candidate["rule"] for candidate in rule.get("candidates") or [])
    return {"rule": names, "allows_mil": _rule_allows(rule),
            **({} if rule.get("rule") else {"depends_on_objects": True})}


def _rule_allows(rule: dict | None) -> float | None:
    """The least spacing a board rule entry (_Board.rule) may allow, or None."""
    if not rule:
        return None
    entries = [rule] if rule.get("rule") else rule.get("candidates") or []
    allowed = [entry.get("smallest_mil", entry.get("clearance_mil")) for entry in entries]
    allowed = [value for value in allowed if value is not None]
    return min(allowed) if allowed else None


class _Board:
    """The PCB side: nets, layer types, origin, Clearance rules."""

    def __init__(self, index):
        self.index = index
        self.pcb = index.pcb
        self.model = copper_model(index)
        self.names = list(self.pcb.nets)
        self.ids: dict[str, int] = {}
        for net_id, name in enumerate(self.names):
            self.ids.setdefault(name, net_id)
        self.origin = (self.pcb.board.origin_x or 0.0, self.pcb.board.origin_y or 0.0)
        self.rules = [rule for rule in self.pcb.rules if rule.kind == "Clearance"]
        self._targets: dict[int, RuleTarget] = {}
        self._rule_cache: dict[tuple, dict | None] = {}
        self._signal = index.signal_layers()
        self._substacks = getattr(self.pcb.board, "substacks", None)
        self._layer_names: dict[int, str] = {}

    def layer_type(self, layer: int) -> str:
        return "outer" if layer in self.model.outer else "inner"

    def layer_name(self, layer: int) -> str:
        if layer not in self._layer_names:
            self._layer_names[layer] = self.index.layer_name(layer)
        return self._layer_names[layer]

    def at(self, point) -> dict:
        return {"x_mil": round(point[0] - self.origin[0], 3),
                "y_mil": round(point[1] - self.origin[1], 3)}

    def part(self, copper) -> str | None:
        if copper.component is None:
            return None
        return self.index.designator(copper.component)

    def _target(self, net: int) -> RuleTarget:
        if net not in self._targets:
            name = self.names[net]
            self._targets[net] = self.index.rule_target(name, kind="net") or RuleTarget(
                kind="net", name=name, nets={name.lower()}, pairs=set())
        return self._targets[net]

    def _values(self, rule) -> dict:
        values = rule_values(rule, self._signal, self._substacks)
        clearance = values.get("clearance_mil")
        out = {"rule": rule.name, "clearance_mil": clearance}
        # Its object matrix's copper-to-copper cells (not copper to hole).
        matrix = [value for key, value in (values.get("object_clearances_mil") or {}).items()
                  if value is not None and set(key.split("-")) <= _COPPER_OBJECTS]
        if matrix and clearance is not None and min(matrix) < clearance - 1e-6:
            out["smallest_mil"] = min(matrix)  # it allows less between some objects
        return out

    def rule(self, a: int, b: int) -> dict | None:
        """The board's Clearance rule for two nets, resolved per net by Altium's priority
        order: {rule, clearance_mil, smallest_mil (its object matrix's smallest, when
        lower)}; when higher rules apply only to some objects, {rule: None, candidates:
        those rules then the one that applies otherwise}. None without one."""
        key = (a, b)
        if key in self._rule_cache:
            return self._rule_cache[key]
        out = None
        if self.rules:
            rule, undecided = resolve_clearance_rule(
                self.rules, self._target(a), self._target(b), self.index.rule_context)
            if undecided:
                by_name = {item.name: item for item in self.rules}
                candidates = [self._values(by_name[entry["rule"]]) for entry in undecided
                              if entry["rule"] in by_name]
                if rule is not None:
                    candidates.append({**self._values(rule), "otherwise": True})
                out = {"rule": None, "candidates": candidates}
            elif rule is not None:
                out = self._values(rule)
        self._rule_cache[key] = out
        return out


def prepare_hv(
    netlist: dict,
    plan=None,
    project_params: dict | None = None,
    *,
    index,
    rails: dict[str, float] | None = None,
    hv: dict[str, tuple] | None = None,
    vref=None,
    groups: list[tuple[str, list[str]]] | None = None,
    working_voltage: float | None = None,
    threshold: float = DEFAULT_THRESHOLD_V,
    options: Options | None = None,
    min_distance: dict | None = None,
    nets: list[str] | None = None,
    layer: int | None = None,
    checks: list[str] | None = None,
    units: str = "mm",
) -> dict:
    """Read the voltages, search the copper and judge every pair once; render_hv pages
    the result. index is the board's PcbIndex (required). An {"error": ...} dict for an
    unknown check, net or standard."""
    options = options or Options()
    requested = [check.strip().lower() for check in checks or [] if check.strip()]
    if invalid := [check for check in requested if check not in CHECKS]:
        return {"error": "invalid_checks",
                "message": f"Unknown checks {invalid}; use {list(CHECKS)}."}
    if options.standard not in STANDARDS:
        return {"error": "invalid_standard",
                "message": f"standard must be one of {list(STANDARDS)}."}
    wanted = set(requested or CHECKS)
    board = _Board(index)
    notes: list[str] = []

    # --- voltages, on the schematic's names ---
    tree = build_tree(netlist, plan, project_params)
    data = tree_data(tree, vref)
    parts, _, _ = read_parts(tree, netlist, plan)
    volts, unknown_rails = net_voltages(tree, data, rails, parts)
    if unknown_rails:
        notes.append(f"rails names no net {unknown_rails}: ignored (query_net finds the real "
                     "name).")
    # A switch node swings from 0 V to the voltage it is given, whoever gives it (rails=
    # and hv= as well as the power tree), unless hv= gives it an AC part of its own.
    switch_nodes = {net.casefold() for conv in tree.converters.values()
                    for net in conv.switch_nodes}
    levels_by_name: dict[str, Level] = {}
    for net, volt in volts.items():
        found = Level(volt.volts, 0.0, 0.0, volt.source, volt.confidence, volt.basis)
        levels_by_name[net.casefold()] = (
            found.swinging() if net.casefold() in switch_nodes else found)
    pcb_names = {name.casefold(): name for name in board.names}
    universe = {**{name.casefold(): name for name in tree.design.nets}, **pcb_names}
    if hv:
        resolved, missing = _resolve(hv, universe)
        if missing:
            notes.append(f"hv names no net {missing}: ignored (query_net finds the real name).")
        for given, real in resolved.items():
            dc, ac, phase = hv[given]
            found = Level(dc, ac, phase, "hv", "high", "given in hv")
            levels_by_name[real.casefold()] = (
                found.swinging() if real.casefold() in switch_nodes and not ac else found)
    given_switches = sum(1 for item in levels_by_name.values()
                         if item.switching and item.source in ("hv", "user"))
    if given_switches:
        notes.append(f"{given_switches} converter switch node(s) given in hv= or rails= "
                     "are taken as swinging from 0 V to the voltage given.")

    # --- the PCB's nets: level and group ---
    level: dict[int, Level] = {}
    for net_id, name in enumerate(board.names):
        found = levels_by_name.get(name.casefold())
        if found is not None:
            level[net_id] = found
    group_of: dict[int, str | None] = {}
    group_names = [name for name, _ in groups or ()]
    if groups:
        used: set[tuple[str, str]] = set()
        for net_id, name in enumerate(board.names):
            group_of[net_id] = None
            folded = name.casefold()
            for group, patterns in groups:
                hit = next((pattern for pattern in patterns
                            if fnmatch.fnmatchcase(folded, pattern.casefold())), None)
                if hit is not None:
                    group_of[net_id] = group
                    used.add((group, hit))
                    break
        idle = [pattern for group, patterns in groups for pattern in patterns
                if (group, pattern) not in used]
        if idle:
            notes.append(f"groups names no PCB net {idle}: ignored.")
        if working_voltage is None:
            notes.append("Pairs across the declared groups are not judged without "
                         "working_voltage= (the barrier's working voltage, e.g. '250AC' or "
                         "'400'): they are listed, among unknown_voltage_pairs, only near a "
                         "known high-voltage net.")
    if working_voltage is not None and not groups:
        notes.append("working_voltage applies across groups: without groups= it is unused.")

    def group(net_id: int) -> str | None:
        return group_of.get(net_id)

    # --- overrides ---
    over_all = over_nets = over_pairs = None
    if min_distance:
        over_all = min_distance.get("all")
        over_nets, missing = {}, []
        for given, mil in (min_distance.get("nets") or {}).items():
            real = pcb_names.get(given.casefold())
            if real is None:
                missing.append(given)
            else:
                over_nets[board.ids[real]] = mil
        over_pairs = {}
        for (first, second), mil in (min_distance.get("pairs") or {}).items():
            a, b = pcb_names.get(first.casefold()), pcb_names.get(second.casefold())
            if a is None or b is None:
                missing.append(f"{first}&{second}")
                continue
            ids = sorted((board.ids[a], board.ids[b]))
            over_pairs[tuple(ids)] = mil
        if missing:
            notes.append(f"min_distance names no PCB net {missing}: ignored.")

    def override(a: int, b: int) -> float | None:
        key = (a, b) if a <= b else (b, a)
        if over_pairs and key in over_pairs:
            return over_pairs[key]
        if over_nets and (a in over_nets or b in over_nets):
            return max(over_nets.get(a, 0.0), over_nets.get(b, 0.0))
        return over_all

    # --- pair voltages and reaches ---
    def pair_voltage(a: int, b: int) -> tuple[float | None, str]:
        if group(a) != group(b):
            if working_voltage is not None:
                return working_voltage, "working"
            return None, "cross_group"
        la, lb = level.get(a), level.get(b)
        if la is None or lb is None:
            return None, "unknown"
        return pair_peak(la, lb), "known"

    def widest_mil(volts: float, a: int, b: int) -> float:
        """The user's figure for the pair, else the standard's largest requirement."""
        given = override(a, b)
        if given is not None:
            return given
        return options.widest_mm(volts) * MIL_PER_MM

    def search(required_mil: float) -> float:
        return required_mil + min(required_mil, SEARCH_MARGIN_MAX_MIL)

    def hypothetical(a: int, b: int) -> float | None:
        """The voltage a pair would have with its unknown side at its reference."""
        known = [level[net] for net in (a, b) if net in level]
        if not known:
            return None
        return sum(item.extreme for item in known)

    def pair_reach(a: int, b: int) -> float | None:
        """How far apart a pair's copper is searched (None: not judged nor listed)."""
        volts, kind = pair_voltage(a, b)
        if kind not in ("known", "working"):
            volts = hypothetical(a, b)
        if volts is None or volts <= threshold:
            return None
        return search(widest_mil(volts, a, b))

    # Sources: nets at more than half the threshold from their reference, and across
    # groups with a working voltage every group's nets but the largest group's.
    bounds: dict[str | None, list[float]] = {}
    for net_id, item in level.items():
        low, high = item.dc - item.ac_peak, item.dc + item.ac_peak
        span = bounds.setdefault(group(net_id), [low, high])
        span[0], span[1] = min(span[0], low), max(span[1], high)
    vmax: dict[int, float] = {}
    for net_id, item in level.items():
        if item.extreme > threshold / 2.0:
            low, high = bounds[group(net_id)]
            widest = max(item.extreme, high - (item.dc - item.ac_peak),
                         (item.dc + item.ac_peak) - low)
            if widest > threshold:  # some pair of it may be judged, or listed
                vmax[net_id] = widest
    cross_sources = 0
    members = Counter(group(net_id) for net_id in range(len(board.names)))
    if groups and working_voltage is not None and working_voltage > threshold:
        largest = max(members, key=lambda name: (members[name], name is None))
        for net_id in range(len(board.names)):
            if group(net_id) != largest:
                vmax[net_id] = max(vmax.get(net_id, 0.0), working_voltage)
                cross_sources += 1
    reach_by_net: dict[int, float] = {}
    for net_id, volts in vmax.items():
        widest = options.widest_mm(volts) * MIL_PER_MM
        given = [value for value in (over_all, (over_nets or {}).get(net_id)) if value]
        given += [mil for key, mil in (over_pairs or {}).items() if net_id in key]
        reach = search(max([widest, *given]))
        # A figure given for a net it pairs with ('GND=4'): as far as that pair needs.
        for other in over_nets or ():
            if other != net_id:
                reach = max(reach, pair_reach(net_id, other) or 0.0)
        reach_by_net[net_id] = reach

    # --- filters ---
    wanted_nets = None
    searched_reach = pair_reach
    if nets:
        resolved, missing = _resolve(nets, pcb_names)
        if not resolved:
            return {"error": "net_not_found",
                    "message": f"No PCB net {missing}; use query_net or get_net_pcb to find the "
                               "real name."}
        if missing:
            notes.append(f"Not on the PcbDoc: {', '.join(missing)}.")
        wanted_nets = {board.ids[real] for real in resolved.values()}

        def searched_reach(a: int, b: int) -> float | None:
            return pair_reach(a, b) if a in wanted_nets or b in wanted_nets else None

        # Every pair judged has a source side: search from the wanted sources and from the
        # sources that pair with a wanted net no one searches from.
        plain = wanted_nets - set(reach_by_net)
        reach_by_net = {net: reach for net, reach in reach_by_net.items()
                        if net in wanted_nets or any(pair_reach(net, other) for other in plain)}
    layers = None
    if layer is not None:
        layers = {layer}

    # --- the copper ---
    gaps = net_gaps(index, reach_by_net, searched_reach, layers) if reach_by_net else None
    if gaps is not None and gaps.stopped:
        notes.append("The copper search stopped at its work limit: pairs may be missing (name "
                     "nets in net= or a layer in layer= to narrow it).")

    # --- judging ---
    pairs: dict[tuple[int, int], dict] = {}
    unknown: dict[tuple[int, int], dict] = {}

    def requirement(volts: float, a: int, b: int, layer_type: str, lead: bool) -> dict:
        """{standard, column, band, required_mil (, standard_mil under a user figure)}."""
        table = options.requirement(volts, layer_type, lead)
        table_mil = table.pop("required_mm") * MIL_PER_MM
        given = override(a, b)
        if given is None:
            return {**table, "required_mil": table_mil}
        return {"standard": "min_distance (user)", "column": None, "band": None,
                "required_mil": given, "standard_mil": table_mil}

    for record in gaps.records if gaps else ():
        a, b = record.first.net, record.second.net
        if wanted_nets is not None and not ({a, b} & wanted_nets):
            continue
        volts, kind = pair_voltage(a, b)
        layer_type = board.layer_type(record.layer)
        lead = record.first.kind == "pad" or record.second.kind == "pad"
        if kind in ("known", "working"):
            if volts <= threshold:
                continue
            need = requirement(volts, a, b, layer_type, lead)
            entry = pairs.setdefault((a, b), {"volts": volts, "kind": kind, "layers": {}})
            slot = entry["layers"].setdefault(record.layer, {"min": None, "worst": None})
            if slot["min"] is None or record.gap_mil < slot["min"][0].gap_mil:
                slot["min"] = (record, need)
            shortfall = record.gap_mil - need["required_mil"]
            if slot["worst"] is None or shortfall < slot["worst"][2]:
                slot["worst"] = (record, need, shortfall)
        else:
            guess = hypothetical(a, b)
            if guess is None or guess <= threshold:
                continue
            need = requirement(guess, a, b, layer_type, lead)
            need_mil = need["required_mil"]
            entry = unknown.get((a, b))
            if entry is None or record.gap_mil - need_mil < entry["shortfall"]:
                unknown[(a, b)] = {"record": record, "kind": kind, "guess": guess,
                                   "required_mil": need_mil, "standard": need["standard"],
                                   "shortfall": record.gap_mil - need_mil}

    def volt_text(net: int) -> str:
        item = level.get(net)
        if item is None:
            return f"{board.names[net]} unknown"
        return f"{board.names[net]} {item.text()} ({item.source})"

    def pair_basis(a: int, b: int, kind: str) -> str:
        if kind == "working":
            return (f"across groups {group(a) or 'board'} / {group(b) or 'board'}: the "
                    f"working voltage, {_v(working_voltage)} V peak")
        return f"{volt_text(a)} to {volt_text(b)}"

    def confidence(a: int, b: int, kind: str) -> str:
        if kind == "working":
            return "high"
        ranks = {"high": 0, "medium": 1, "low": 2}
        return max((level[a].confidence, level[b].confidence), key=ranks.get)

    def objects(record) -> list[str]:
        return [record.first.kind, record.second.kind]

    def parts(record) -> list[str]:
        found = {board.part(side) for side in (record.first, record.second)}
        return sorted((name for name in found if name), key=natural_key)

    def where(record) -> dict:
        """The middle of the gap, on true arcs (GapRecord.ends)."""
        start, end = record.ends
        return board.at(((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0))

    # --- rows and findings ---
    findings: list[dict] = []
    rows: list[dict] = []
    openings_listed: list[dict] = []
    below_rule: dict[tuple, dict] = {}  # a board rule (or its candidates) -> its pairs
    by_type: dict[str, Counter] = {"outer": Counter(), "inner": Counter()}
    min_by_type: dict[str, float] = {}
    for (a, b), entry in pairs.items():
        names = [board.names[a], board.names[b]]
        kind = entry["kind"]
        rule = board.rule(a, b)
        layer_rows = []
        worst_ratio = math.inf
        widest_need = 0.0
        closest = None
        for layer_id in sorted(entry["layers"], key=board.model.position.get):
            slot = entry["layers"][layer_id]
            record, need = slot["min"]
            worst, worst_need, shortfall = slot["worst"]
            layer_type = board.layer_type(layer_id)
            below = shortfall < -CLEARANCE_TOLERANCE_MIL
            by_type[layer_type]["pairs"] += 1
            by_type[layer_type]["below"] += below
            min_by_type[layer_type] = min(min_by_type.get(layer_type, math.inf), record.gap_mil)
            widest_need = max(widest_need, worst_need["required_mil"])
            ratio = worst.gap_mil / worst_need["required_mil"] if worst_need["required_mil"] \
                else math.inf
            worst_ratio = min(worst_ratio, ratio)
            row = {"layer": board.layer_name(layer_id), "layer_type": layer_type,
                   "gap_mil": round(record.gap_mil, 3),
                   "required_mil": round(need["required_mil"], 3),
                   "status": "below" if below else "ok"}
            if worst is not record:  # a pad's requirement differs (assembly columns)
                row["worst_gap_mil"] = round(worst.gap_mil, 3)
                row["worst_required_mil"] = round(worst_need["required_mil"], 3)
            layer_rows.append(row)
            if ratio <= worst_ratio:
                closest = {"layer": row["layer"], "layer_type": layer_type,
                           "gap_mil": round(worst.gap_mil, 3),
                           "required_mil": round(worst_need["required_mil"], 3),
                           "standard": worst_need["standard"], "column": worst_need["column"],
                           "band": worst_need["band"], "objects": objects(worst),
                           "parts": parts(worst), "at": where(worst)}
            if below and "spacing_below_standard" in wanted:
                start, end = worst.ends
                openings = openings_near(index, start, end, OPENING_NEAR_MIL)
                listed = [{"kind": item["kind"], "in_path": item["in_path"],
                           "distance_mil": round(item["distance_mil"], 3),
                           "at": board.at(item["at"])} for item in openings]
                if listed:
                    openings_listed.append({"nets": names, "layer": row["layer"],
                                            "openings": listed})
                marks = sorted(worst.marks)
                need_text = (f"{worst_need['standard']} {worst_need['column']} "
                             f"({worst_need['band']})" if worst_need["column"] else
                             f"{worst_need['standard']}"
                             + (f" ({worst_need['band']})" if worst_need["band"] else ""))
                findings.append(_finding(
                    "spacing_below_standard", "warning",
                    f"{names[0]} and {names[1]} ({_v(entry['volts'])} V peak) come "
                    f"{_length(worst.gap_mil, units)} apart on {row['layer']} ({layer_type}), below the "
                    f"{_length(worst_need['required_mil'], units)} of {need_text}"
                    + (" - the copper touches" if "touching" in worst.marks else "")
                    + (": a slot, hole or cutout lies in the path, which lengthens creepage "
                       "but not clearance" if any(item["in_path"] for item in listed) else "")
                    + ".",
                    nets=names, voltage_v=round(entry["volts"], 2),
                    voltage=pair_basis(a, b, kind), confidence=confidence(a, b, kind),
                    layer=row["layer"], layer_type=layer_type, objects=objects(worst),
                    parts=parts(worst), gap_mil=round(worst.gap_mil, 3),
                    required_mil=round(worst_need["required_mil"], 3),
                    standard=worst_need["standard"], column=worst_need["column"],
                    band=worst_need["band"],
                    standard_required_mil=round(worst_need["standard_mil"], 3)
                    if "standard_mil" in worst_need else None,
                    at=where(worst),
                    board_rule=_rule_brief(rule), openings=listed, marks=marks,
                    ratio=round(worst.gap_mil / worst_need["required_mil"], 3)
                    if worst_need["required_mil"] else None))
        pair_row = {"nets": names, "voltage_v": round(entry["volts"], 2),
                    "voltage": pair_basis(a, b, kind), "confidence": confidence(a, b, kind),
                    "worst_ratio": round(worst_ratio, 3) if math.isfinite(worst_ratio) else None,
                    "closest": closest, "layers": layer_rows, "board_rule": rule}
        rows.append(pair_row)
        rule_mil = _rule_allows(rule)
        if rule_mil is not None and rule_mil < widest_need - CLEARANCE_TOLERANCE_MIL:
            key = (rule.get("rule"), tuple(candidate["rule"] for candidate in
                                           rule.get("candidates") or ()))
            held = below_rule.setdefault(key, {"rule": rule, "allows": rule_mil, "pairs": []})
            held["pairs"].append({
                "nets": names, "voltage_v": round(entry["volts"], 2),
                "required_mil": round(widest_need, 3),
                **({"standard": "min_distance (user)"} if override(a, b) is not None else {})})
    rows.sort(key=lambda row: (row["worst_ratio"] if row["worst_ratio"] is not None
                               else math.inf, natural_key(row["nets"][0])))
    # One info per board rule (or set of candidate rules) whose spacing is below what some
    # pairs it governs need.
    for held in below_rule.values() if "spacing_below_rule" in wanted else ():
        rule, allows = held["rule"], held["allows"]
        decided = bool(rule.get("rule"))
        names_text = rule["rule"] if decided else ", ".join(
            candidate["rule"] for candidate in rule["candidates"])
        listed = sorted(held["pairs"], key=lambda item: (
            -item["required_mil"], natural_key(item["nets"][0]), natural_key(item["nets"][1])))
        top = listed[0]
        findings.append(_finding(
            "spacing_below_rule", "info",
            (f"The board's Clearance rule {names_text} lets " if decided else
             f"The board's Clearance rules {names_text} (depending on the objects) let ")
            + f"{len(listed)} pair(s) of nets at different voltages come "
            f"{_length(allows, units)} apart, below what they need where their copper is "
            f"close (up to {_length(top['required_mil'], units)}, {top['nets'][0]} and "
            f"{top['nets'][1]} at {_v(top['voltage_v'])} V peak): Altium's DRC will not "
            "catch spacing between them that the standard (or min_distance) rejects.",
            rule=names_text, rule_clearance_mil=round(allows, 3),
            required_mil=top["required_mil"],
            depends_on_objects=None if decided else True,
            pairs=listed[:UNKNOWN_PAIRS_LISTED], pairs_total=len(listed),
            ratio=round(allows / top["required_mil"], 3) if top["required_mil"] else None))

    # Unknown voltages: HV-looking nets to ask about, and pairs near a known HV net.
    known_names = {board.names[net] for net in level}
    hv_looking = _hv_looking(tree, set(pcb_names.values()) - known_names)
    if wanted_nets is not None:
        hv_looking = {name: reasons for name, reasons in hv_looking.items()
                      if board.ids.get(name) in wanted_nets}
    hv_nets_unknown = [
        {"net": name, "why": reasons} for name, reasons in sorted(
            hv_looking.items(), key=lambda item: (-len(item[1]), natural_key(item[0])))]
    unknown_rows = []
    for (a, b), entry in sorted(unknown.items(), key=lambda item: (
            item[1]["shortfall"], item[0])):
        record = entry["record"]
        unknown_side = [board.names[net] for net in (a, b) if net not in level]
        row = {"nets": [board.names[a], board.names[b]],
               "known": [volt_text(net) for net in (a, b) if net in level],
               "unknown": unknown_side,
               "reason": ("across groups without working_voltage" if entry["kind"] ==
                          "cross_group" else "voltage unknown"),
               "layer": board.layer_name(record.layer), "gap_mil": round(record.gap_mil, 3),
               "if_at_reference_v": round(entry["guess"], 2),
               "if_at_reference_required_mil": round(entry["required_mil"], 3),
               "below_if_at_reference": entry["shortfall"] < -CLEARANCE_TOLERANCE_MIL,
               "at": where(record)}
        if entry["standard"] == "min_distance (user)":
            row["required_by"] = entry["standard"]
        unknown_rows.append(row)
    if "unknown_voltage_pairs" in wanted and (hv_nets_unknown or unknown_rows):
        closer = sum(1 for row in unknown_rows if row["below_if_at_reference"])
        findings.append(_finding(
            "unknown_voltage_pairs", "info",
            (f"{len(hv_nets_unknown)} net(s) look like high voltage but have no known "
             "voltage (listed with why); " if hv_nets_unknown else "")
            + (f"{len(unknown_rows)} pair(s) of a net of unknown voltage and a high-voltage "
               f"net have copper close together ({closer} closer than required if the "
               "unknown net sat at its reference, 0 V); " if unknown_rows else "")
            + "these pairs are not judged: give their voltages in hv= or rails= (and groups= "
              "with working_voltage= across an isolation barrier).",
            hv_looking_nets=hv_nets_unknown[:UNKNOWN_NETS_LISTED],
            hv_looking_total=len(hv_nets_unknown),
            pairs=unknown_rows[:UNKNOWN_PAIRS_LISTED], pairs_total=len(unknown_rows)))
    if "creepage_not_computed" in wanted and pairs:
        findings.append(_finding(
            "creepage_not_computed", "info",
            "Distances are straight lines between copper on one layer (clearance). Creepage "
            "along the board surface is not computed: the straight-line spacing is its lower "
            "bound when no slot, hole or cutout lies between the copper, and those listed "
            "here (in or within 1 mm of the path of a spacing finding) lengthen it and are "
            "not credited. Check creepage against the product's safety standard.",
            openings=openings_listed[:UNKNOWN_PAIRS_LISTED],
            openings_total=len(openings_listed)))
    findings.sort(key=lambda finding: (_SEVERITY_RANK[finding["severity"]],
                                       _CHECK_RANK[finding["check"]],
                                       finding.get("ratio", math.inf) if
                                       finding.get("ratio") is not None else math.inf))

    # --- summary ---
    sources = Counter(item.source for item in level.values())
    # Pairs of known voltage above threshold, counted per distinct level in a group (a
    # board has few) rather than per pair of nets.
    distinct: dict[tuple, list] = {}
    for net_id, item in level.items():
        key = (group(net_id), item.dc, item.ac_peak, item.phase, item.switching)
        distinct.setdefault(key, [item, 0])[1] += 1
    above = 0
    by_level = list(distinct.items())
    for position, ((group_a, *_), (item_a, count_a)) in enumerate(by_level):
        if pair_peak(item_a, item_a) > threshold:  # two switch nodes
            above += count_a * (count_a - 1) // 2
        for (group_b, *_), (item_b, count_b) in by_level[position + 1:]:
            if group_a == group_b and pair_peak(item_a, item_b) > threshold:
                above += count_a * count_b
    counts = list(members.values())
    across = (sum(counts) ** 2 - sum(count * count for count in counts)) // 2
    cross = across if groups and working_voltage is not None and \
        working_voltage > threshold else 0
    hv_rows = sorted(((net, level[net]) for net in vmax if net in level),
                     key=lambda item: (-item[1].extreme, natural_key(board.names[item[0]])))
    severity = Counter(finding["severity"] for finding in findings)
    by_check: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    for finding in findings:
        by_check[finding["check"]][finding["severity"]] += 1
    summary = {
        "net_voltages": {
            "known_by_source": {source: sources[source] for source in
                                ("hv", "user", "ground", "name", "power_tree", "divider")
                                if sources[source]},
            "ac_nets": sum(1 for item in level.values() if item.ac_peak and not item.switching),
            "switch_nodes": sum(1 for item in level.values() if item.switching),
            "unknown": len(board.names) - len(level),
        },
        "hv_nets": [{"net": board.names[net], "voltage": item.text(), "source": item.source,
                     "confidence": item.confidence, **({"group": group(net)} if groups else {})}
                    for net, item in hv_rows[:HV_NETS_LISTED]],
        "hv_nets_total": len(hv_rows),
        "pairs": {
            "above_threshold": above + cross,
            "searched_from_nets": len(reach_by_net),
            "measured": len(pairs),
            "below_standard": sum(1 for row in rows if any(
                layer_row["status"] == "below" for layer_row in row["layers"])),
            "unknown_voltage_nearby": len(unknown_rows),
        },
        "by_layer_type": {
            layer_type: {"pair_layers": counts["pairs"], "below": counts["below"],
                         **({"min_gap_mil": round(min_by_type[layer_type], 3)}
                            if layer_type in min_by_type else {})}
            for layer_type, counts in by_type.items()},
        "findings": {check: dict(levels) for check, levels in by_check.items()},
        "severity_counts": {level_name: severity[level_name] for level_name in ("warning", "info")},
        "unknown_hv_nets": [row["net"] for row in hv_nets_unknown[:UNKNOWN_NETS_LISTED]],
        "geometry": {"distance_calls": gaps.distance_calls if gaps else 0,
                     "stopped": bool(gaps and gaps.stopped),
                     "no_net_copper_nearby": gaps.no_net_owners if gaps else 0,
                     "net_tie_pairs_skipped": gaps.net_tie_pairs if gaps else 0},
    }
    if groups:
        summary["groups"] = {
            "declared": group_names,
            "members": dict(Counter(group(net_id) or "board" for net_id in
                                    range(len(board.names)))),
            "working_voltage_v": round(working_voltage, 2) if working_voltage else None,
            "pairs_across": across, "pairs_across_judged": bool(cross),
            "cross_group_sources": cross_sources}
    assumptions = {
        "standard": _STANDARD_NAMES[options.standard], "threshold_v": threshold,
        "coating": options.coating, "altitude_m": options.altitude_m,
        "columns": ({"inner": "B1", "outer": "/".join(dict.fromkeys(
            options.columns("outer", False) + options.columns("outer", True)))}
                    if options.standard != "ipc9592" else None),
        "min_distance": ({"all_mil": over_all, "nets": len(over_nets or {}),
                          "pairs": len(over_pairs or {})} if min_distance else None),
        "voltages": "peak (AC RMS x 1.414); AC nets in phase unless a phase is given",
    }
    pcb = board.pcb
    if getattr(pcb.board, "plane_nets", None):
        notes.append(f"{len(pcb.board.plane_nets)} internal plane layer(s) are not measured: "
                     "their spacing to other nets' holes is the Power Plane Clearance rule's.")
    shelved = sum(1 for pour in pcb.pours if pour.shelved)
    poured = {area.pour_id for area in pcb.areas if area.pour_id is not None} | {
        row[0] for row in index.db.execute(
            "SELECT DISTINCT poured_by FROM prims WHERE poured_by IS NOT NULL")}
    empty = sum(1 for pour in pcb.pours if not pour.shelved and pour.pour_id not in poured
                and (pour.polygon_type or "").strip().lower() != "split plane")
    if shelved or empty:
        notes.append(f"{shelved} shelved and {empty} unpoured pour(s) hold no copper and are "
                     "not measured: repour and save to include them.")
    if "Regions6" not in (getattr(pcb, "area_streams", ()) or ()) and pcb.pours:
        notes.append("The board's poured regions were not read: solid pour copper is missing "
                     "from the measurement.")
    if any(finding.get("column") in ("B2", "B3") for finding in findings):
        notes.append("External copper was judged uncoated (B2/B3). If the outer layers carry "
                     "a permanent polymer coating, coating=true judges them by B4; whether a "
                     "solder mask counts as one is for the product's standard to say.")
    if not hv and not rails:
        notes.append("No hv= or rails= given: voltages come from net names and the power tree, "
                     "which are unreliable for high voltage. Ask the user for the working "
                     "voltages and pass them in hv=.")
    return {"variant": tree.variant, "summary": summary, "assumptions": assumptions,
            "findings": findings, "pairs": rows, "notes": notes, "wanted": wanted}


def prepare_hv_cached(netlist: dict, plan=None, project_params: dict | None = None, *,
                      index, **kwargs) -> dict:
    """prepare_hv, cached on the index per netlist, variant, project parameters and
    arguments (the last CACHE_SIZE), so paging and repeat calls reuse it: treat the result
    as read-only. A new PcbDoc or netlist builds a new index, and with it a new cache."""
    key = repr((plan, project_params, sorted(kwargs.items())))
    cache = getattr(index, "_hv_cache", None)
    if cache is None:
        cache = index._hv_cache = {}
    held = cache.get(key)
    if held is not None and held[0] is netlist:
        return held[1]
    prepared = prepare_hv(netlist, plan, project_params, index=index, **kwargs)
    if "error" not in prepared:
        if len(cache) >= CACHE_SIZE:
            cache.pop(next(iter(cache)))
        cache[key] = (netlist, prepared)
    return prepared


def render_hv(prepared: dict, offset: int = 0, limit: int = PAGE_LIMIT) -> dict:
    """One page: the summary first, then findings (warnings first) and pairs (worst
    margin first) paged together on one offset."""
    offset = max(0, int(offset or 0))
    page = slice(offset, offset + limit)
    findings, pairs = prepared["findings"], prepared["pairs"]
    result = {
        "variant": prepared["variant"],
        "summary": prepared["summary"],
        "assumptions": prepared["assumptions"],
        "checks_run": [check for check in CHECKS if check in prepared["wanted"]],
        "screening": SCREENING,
        "sources": SOURCES,
        "findings": findings[page],
        "findings_total": len(findings),
        "pairs": pairs[page],
        "pairs_total": len(pairs),
        "offset": offset,
    }
    result["has_more"] = offset + limit < max(len(findings), len(pairs))
    if result["has_more"]:
        result["next_offset"] = offset + limit
    result["notes"] = list(prepared["notes"])
    return result


def run_hv_clearance_check(netlist: dict, plan=None, project_params: dict | None = None, *,
                           index, offset: int = 0, limit: int = PAGE_LIMIT, **kwargs) -> dict:
    """The check in one call: prepare_hv and one page of render_hv."""
    prepared = prepare_hv(netlist, plan, project_params, index=index, **kwargs)
    if "error" in prepared:
        return prepared
    return render_hv(prepared, offset, limit)
