"""Driver/receiver logic-level compatibility candidates (heuristic).

For every signal that links ICs powered from different rails, each IC pin's I/O supply
and its voltage, and the pairs whose levels likely do not meet: a push-pull driver whose
I/O rail is below a receiver's VIH estimate (driver_low_for_receiver), a driver or a
pull-up above a receiver's absolute maximum (receiver_overvoltage,
open_drain_pullup_mismatch), and the rails whose voltage is not known (unknown_rail).
Level translators and isolators are listed as handled (translator_ok).

I/O rail per IC pin: the supply pins of each fitted IC (and module, and oscillator with a
supply pin whatever its prefix) are read by name (pin_role), classed as I/O (VDDIO,
VCCIO, VIO, VDD_IO, OVDD, VCCO, VDDQ, VL ...), digital (VDD, VCC, DVDD), analog (AVDD,
VDDA, AVIN), core (VCCINT, VCORE, PLL), backup (VBAT), power input (VIN, PVIN, VDDH, VM:
used only when a part has no other supply) or skipped (VPP, VBUS, references, a
regulator's own outputs and bias pins such as VDDB, and a supply pin whose net reaches
only the part and capacitors, its own bias output). A pin of an FPGA (or any part with I/O supplies of two or more
banks on different nets) takes its bank's supply when its name gives the bank
(IO_L1P_T0_14 and DONE_0 are bank 14 and 0 by their last token, Intel's LVDS3G_6p,
DIFFIO2L_16p, CLK_3G_1p and RZQ_3G are bank 3G, a token equal to a bank name such as
HPS); otherwise its I/O supply outside the banks (VCCPGM, medium) or the highest bank
(low), never its core supply. A level translator's or isolator's pin takes its side's
supply (A/B pins on VCCA/VCCB, 1/2 on VCC1/VCC2 or VDD1/VDD2, VL/VCC; DIR and OE on the A
side; else by the half of the numbered pins the side's supply sits in); a clock part's
outputs take its VDDO pins. Otherwise a part's only supply (high confidence), its one I/O
supply (high), its one digital supply among others (medium; low when a lone numbered I/O
supply such as VDDIO2 sits on another net, since that powers one port or bank only), or
the highest of its digital supplies (low). Voltages come from derating.net_voltages:
rails= given by the user, net names, the power tree (vref=) and divider midpoints, each
with its source and confidence.

Signals are nets joined through series resistors under 1 kohm and 0 ohm links
(pull_resistors.link_group); a fitted series resistor of 1 kohm or more to another
signal is crossed once, with the level divided when a pull-down sits behind it (counted
apart, as series_resistor_links). Each IC pin's direction comes from its electrical type
when it is not Passive (Output and HiZ drive, OpenCollector and OpenEmitter are
open-drain, Input receives, IO both), else from its name for the part's class: TX, MOSI,
SCK, CS, OUT drive from a processor or bridge, RX, MISO, IN receive; a SPI peripheral's
SDO and MISO drive, its SDI, MOSI, SCK and CS receive; a bus transceiver's TXD and DI
receive and RXD and RO drive; an Ethernet PHY's TXD receives; JTAG TDO drives and TCK,
TMS, TDI receive; I2C lines are open-drain; interrupt, alert and power-good pins of
peripherals drive open-drain or push-pull; reset, enable and mode pins of peripherals
receive; OUTPUT and INPUT name themselves. A processor GPIO named only by its port (PA5)
gets the direction its peers leave: it drives a line where every other pin receives, and
receives where another pin drives push-pull. A line with an OpenCollector pin, an I2C
name or an open-drain driver name and a fitted pull-up is open-drain: its high level is
the pull-up's rail, which every pin on it sees.

Candidates: driver_low_for_receiver (a push-pull driver's rail below 70% of a
receiver's, the usual CMOS VIH: info, warning at or below 50%, e.g. 1.8 V into 5 V);
receiver_overvoltage (a driver's rail, or a pull-up on a push-pull line, more than 0.3 V
above a receiver's, the usual absolute maximum VDD + 0.3 V: warning, info when the
receiver pin is 5 V tolerant by its name (FT, 5VT) or its part's description or family
(74LVC, 74AHC, 74VHC ...), or a series resistor of 1 kohm or more limits the current; an
output pin under such a pull-up is judged too, as it is back-driven while tri-stated or
high); open_drain_pullup_mismatch (a pull-up rail above a receiver's by more than 0.3 V:
warning, softened the same way, and info on an open-drain output, many of which are rated
above their supply; below a receiver's VIH estimate: info); unknown_rail (one row listing
the I/O and pull-up rails of unknown voltage on signals between ICs, every rail of a pair
at once and the rails behind a 0 ohm link, ferrite or inductor under the one they follow,
to pass in rails=; supply pins on nets named like signals and parts with no supply pin
read are listed apart). A pair is a warning only when one end's direction is certain (the
driver typed or named an output, or the receiver typed or named an input) and both rails
are read with medium or high confidence; two bidirectional or unknown pins are judged
both ways at info. Regulators', references' and supervisors' inputs are judged for
overvoltage only, at info, and their pins drive only when typed or named outputs; gate,
motor or LED drivers and parts powered above 5.5 V are taken to have fixed thresholds
(info only); the open-drain outputs of optocouplers, comparators, regulators and drivers
are rated apart from their supply and not judged under their pull-up.
Left out: analog nets (ADC, DAC, references, op-amp inputs, a sensor's VOUT or AOUT),
power nets, crystal lines (a crystal or resonator on them, or crystal pins no active
clock drives: a crystal pin an oscillator or buffer drives is judged at info, against the
part's highest supply), differential pairs (a net whose complement exists by
diff_pair_checks.complement, a pin named as one half of a pair, USB D+/D-, LVDS, MIPI,
MDI, CAN_H/L and the like by name; a single-ended sideband or clock pin such as PERST or
GTX_CLK is judged), a bus
transceiver's line-side pins (CANH/CANL, A/B, Y/Z, LIN, RS232 TxOUT/RxIN) and nets that
reach only one IC.

Guidance: Altium's common-errors article (a driver's VOH below its receiver's VIH was the
most frequent defect, 22%; FPGA bank voltages) and its schematic review checklist (every
logic input within its maximum ratings). Everything here is a candidate from names: the
datasheet's VOH, VIH and absolute maximum decide every finding.
"""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

try:
    from services.bom import natural_key
    from services.decoupling import (
        designator_prefix, first_function, net_kind, net_tokens, pin_role,
    )
    from services.derating import SAME_VOLTAGE_EDGES, Volt, net_voltages, read_parts
    from services.diff_pair_checks import complement, polarity_tokens
    from services.firmware_pins import (
        BRIDGE_TEXT, CRYSTAL_TEXT, IN_NAME, OUT_NAME, SIGNAL_BUS_FUNCTIONS, SLAVE_IN, SLAVE_OUT,
        XCVR_IN, XCVR_OUT, alternatives, auto_net, classify, ethernet_part, part_kind, port_like,
    )
    from services.interfaces import bus_transceiver
    from services.power_tree import build_tree, tree_data
    from services.pull_resistors import (
        COMPARATOR_TEXT, DRIVER_TEXT, LINK_OHMS, control_kind, i2c_line, link_group,
        ohms_text, open_drain_name, resolve_names,
    )
except ImportError:
    from server.services.bom import natural_key
    from server.services.decoupling import (
        designator_prefix, first_function, net_kind, net_tokens, pin_role,
    )
    from server.services.derating import SAME_VOLTAGE_EDGES, Volt, net_voltages, read_parts
    from server.services.diff_pair_checks import complement, polarity_tokens
    from server.services.firmware_pins import (
        BRIDGE_TEXT, CRYSTAL_TEXT, IN_NAME, OUT_NAME, SIGNAL_BUS_FUNCTIONS, SLAVE_IN, SLAVE_OUT,
        XCVR_IN, XCVR_OUT, alternatives, auto_net, classify, ethernet_part, part_kind, port_like,
    )
    from server.services.interfaces import bus_transceiver
    from server.services.power_tree import build_tree, tree_data
    from server.services.pull_resistors import (
        COMPARATOR_TEXT, DRIVER_TEXT, LINK_OHMS, control_kind, i2c_line, link_group,
        ohms_text, open_drain_name, resolve_names,
    )

CHECKS = (
    "driver_low_for_receiver", "receiver_overvoltage", "open_drain_pullup_mismatch",
    "unknown_rail", "translator_ok",
)
_CHECK_RANK = {check: rank for rank, check in enumerate(CHECKS)}
_SEVERITY_RANK = {"warning": 0, "info": 1}
PAGE_LIMIT = 100
RAILS_LISTED = 12  # rails of unknown voltage listed, most signals first
PINS_LISTED = 8  # pin pairs quoted per finding
TRANSLATORS_LISTED = 40

VIH_RATIO = 0.7  # the usual CMOS VIH: 0.7 x VDD
WARN_RATIO = 0.5  # a driver rail at or below half the receiver's: VOH < VIH for sure
ABS_MAX_MARGIN = 0.3  # the usual absolute maximum of an input: VDD + 0.3 V
TOLERANT_MAX_V = 5.5  # what a 5 V tolerant input takes
HIGH_SUPPLY_V = 5.5  # a part powered above this has fixed-threshold logic inputs
SAME_V = 0.05  # rails this close are the same level

HEURISTIC = (
    "Candidates from names: each IC pin's I/O supply is read from its part's supply-pin "
    "names (VDDIO, VCCO_n bank pins, translator sides, else its only or highest digital "
    "supply; VIN-like power inputs only as a last resort), its direction from its "
    "electrical type or its name, and net voltages from rails=, net names and the power "
    "tree. VOH, VIH, 5 V tolerance and absolute maximum ratings are datasheet facts: "
    "confirm every candidate in both parts' datasheets (the datasheet overrides "
    "everything here)."
)
GUIDANCE = (
    "Altium's common schematic errors article: a driver's VOH below its receiver's VIH "
    "was the most frequent defect (22%), FPGA bank voltages must match what each bank "
    "connects to. Altium's schematic review checklist: every logic input within its "
    "maximum ratings. CMOS VIH is about 0.7 x VDD, the usual absolute maximum VDD + 0.3 V."
)

# ---------- parts ----------

# Supply-pin classes by the pin name's first function (+ stripped), first match wins.
_SUPPLY_CLASSES = (
    ("skip", re.compile(
        r"VPP\w*|VBUS\w*|VUSB\w*|\w*USB\w*|\w*REF\w*|VREG\w*|VCAP\w*|VDD_?OUT\w*|"
        r"\w*SENSE\w*|\w*SNS\w*|VOUT\w*|SW\d*|LX\d*|VSW\d*")),
    # A regulator's or module's power input, a radio SoC's regulator input (VDDH), a motor
    # driver's supply: used only when a part has no other supply.
    ("power_in", re.compile(r"P?VIN\w*|VDDH|VM|VMOT\w*|VBB")),
    ("core", re.compile(
        r"VCCINT\w*|VCCAUX\w*|VCCBRAM\w*|VCCPINT|VCCPAUX|V?CORE\w*|VDD_?CORE\w*|"
        r"VCC_?CORE\w*|VDDCORE\w*|VCCL\w*|VCCP|VCCERAM\w*|VCCR_\w+|VCCT_\w+|VCCH\w*|VCCPT|"
        r"\w*PLL\w*|VDD_?XTAL\w*|VDDOSC\w*|VDD_?RF\w*|VDD_?REG\w*|\w*SMPS\w*|VDDL|DVDDL|"
        r"VDDC|VDDCR\w*|VCC_?INT|VDD_?INT|VDD_?LDO\w*")),
    ("backup", re.compile(r"VBAT\w*|VBACKUP|VBK\w*|VCCBAT|V?RTC\w*|VDD_?RTC\w*|VSTBY|VDD_?SB")),
    ("analog", re.compile(
        r"AVDD\w*|VDDA\w*|AVCC\w*|VCCA\w*|VAA\w*|VDDANA|AVIN|VA|VDD_?ADC\w*|VDD_?ANA\w*|"
        r"VDD_A\d*|VCC_A")),
    ("io", re.compile(
        r"(?:VDD|VCC)_?IO\w*|VIO\w*|IOVDD\w*|IOVCC\w*|OVDD\w*|OVCC\w*|VDDO\w*|VCCO\w*|"
        r"VDDSHV\w*|VDD_?PADS?\w*|VDDPST\w*|VCCQ\w*|VDDQ\w*|VL|VLOGIC|VDRIVE|VCCPGM|"
        r"DVDDIO\w*|DVDDH|VDD_?EXT|DRVDD\w*|DOVDD\w*")),
    ("dvdd", re.compile(r"DVDD\w*|DVCC\w*|VDDD|VDD_?D|VDD_?DIG\w*")),
)
# A regulator's internal bias or bootstrap supply output.
_BIAS_PIN = re.compile(r"VDDB|VBIAS\w*|V?BOOT\w*|V?BST\w*")
# A bank-numbered I/O supply: VCCO_14, VCCIO3G, VCCIO_HPS, VDDIO2, VDDO0.
_BANK_SUPPLY = re.compile(r"(?P<head>VCCO|VCCIO|VDDIO|VDDO|VIO|IOVDD)_?(?P<bank>[A-Z0-9]{1,4})")
_VOLTAGE_ID = re.compile(r"\d*V\d*|\d+P\d+V?")
_TENTHS_ID = frozenset({"08", "09", "10", "12", "15", "18", "25", "28", "33"})
# A pin's bank in its name: Xilinx IO_L1P_T0_14 (last token), Intel LVDS3G_6p,
# DIFFIO2L_16p, CLK_3G_1p, PLL_3G_FB0, RZQ_3G; BANK2 or B14 tokens.
_XILINX_BANK = re.compile(r"IO_\w*?_(\d{1,3})")
_INTEL_BANK = re.compile(r"(?:LVDS|DIFFIO(?:_[A-Z]+)?|CLK_|PLL_|RZQ_)(\d[A-L])(?=_|[PN]\b|[PN]$|$)")
_BANK_WORD = re.compile(r"(?:BANK|B)_?(\d{1,2}[A-L]?)")

_TRANSLATOR_TEXT = re.compile(
    r"level[- ]?(?:shift|translat)|voltage[- ]?(?:level[- ]?)?translat|translat(?:or|ing|ion)|"
    r"dual[- ]supply (?:bus|transceiver|buffer)|bidirectional voltage", re.I)
_ISOLATOR_TEXT = re.compile(
    r"(?:digital|signal|I2C|I²C|SPI|CAN|RS-?485|USB|data)[- ]?isolat|\bisolator\b|"
    r"isolated (?:transceiver|interface|buffer|driver)|galvanic", re.I)
OPTO_TEXT = re.compile(r"opto-?coupler|opto-?isolat|photo-?coupler|photo-?transistor|\bopto\b",
                       re.I)
AMPLIFIER_TEXT = re.compile(r"op-?amp|operational amplifier|amplifier|\bINA\d|instrumentation",
                            re.I)
_POWER_DRIVER_TEXT = re.compile(
    r"(?:MOSFET|gate|FET|IGBT|bridge|motor|LED|relay|solenoid|stepper|laser) drivers?\b|"
    r"\bH[- ]?bridge|half[- ]bridge|full[- ]bridge|power stage|DrMOS|smart (?:high|low)[- ]side|"
    r"(?:high|low)[- ]side (?:switch|driver)|darlington|\bIPM\b|intelligent power module", re.I)
_OSCILLATOR_TEXT = re.compile(r"oscillat|\bXO\b|\bTCXO\b|\bVCXO\b|\bSPXO\b|clock generator", re.I)
# A clock generator, buffer or multiplexer whose VDDO pins power only its outputs.
_CLOCK_TEXT = re.compile(r"clock|oscillat|jitter|synthesi[sz]er|fan-?out|multiplexer|\bmux\b|"
                         r"\bbuffer\b", re.I)
_LOGIC_TEXT = re.compile(
    r"(?<![A-Z0-9])(?:SN)?74[A-Z]{0,5}\d{1,4}|\bNC7[SWZ]\w*|\bgate\b|\bbuffer\b|\binverter\b|"
    r"flip-?flop|\blatch\b|schmitt", re.I)
_TTL_TEXT = re.compile(r"(?<![A-Z0-9])(?:SN)?74(?:HCT|ACT|AHCT|VHCT|LVT|ABT|F|LS|ALS|AS|S)\d|"
                       r"TTL[- ](?:compatible|level|input)", re.I)
# Families whose inputs take 5.5 V at any supply (TI, Nexperia and onsemi logic guides).
_TOLERANT_FAMILY = re.compile(
    r"(?<![A-Z0-9])(?:SN)?74(?:LVC|LVCH|LCX|LVT|LVTH|AHC|AHCT|VHC|VHCT|LV)\w*|\bNC7[SWZ]\w*",
    re.I)
_TOLERANT_TEXT = re.compile(r"5\s?V[- ]tolerant|5\s?V[- ]tol\b|over-?voltage[- ]tolerant", re.I)
_TOLERANT_PIN = re.compile(r"(?<![A-Z0-9])(?:FT[AFU]?|5VT|5V_?TOL\w*)(?![A-Z0-9])")
_PROCESSOR_CLASSES = frozenset({"processor", "fpga"})
_FIXED_CLASSES = {
    "power": "a regulator, reference or supervisor: its control inputs have fixed thresholds",
    "driver": "a gate, motor, LED or relay driver: its logic inputs usually have fixed "
              "(TTL-compatible) thresholds, though some scale with a logic supply",
}
_MASTER_OUT = frozenset({"MOSI", "SIMO", "COPI", "PICO", "SDO", "DOUT", "SO", "DO"})
_MASTER_IN = frozenset({"MISO", "SOMI", "CIPO", "POCI", "SDI", "DIN", "SI", "DI"})
_GENERIC_OUT = re.compile(r"OUT\d+[A-Z]?|OUTPUT\d*|DOUT\d*|SDO\d*|[A-Z]OUT\d*")
_GENERIC_IN = re.compile(r"IN\d+[A-Z]?|INPUT\d*|DIN\d*|SDI\d*|[A-Z]IN\d*")
_LOGIC_OUT = re.compile(r"\d*Y\d*|Q\d*|\d+Q|NQ\d*|QN\d*|OUT\w*")
_LOGIC_IN = re.compile(r"\d*[A-D]\d*|IN\w*|OE\w*|CLK\d*|CP\d*|CLR|PRE|S|R|D\d*|\d+D")
_ANALOG_PIN = re.compile(r"[+-]?A?IN\d*[+-]|[+-]A?IN\d*|V?IN[PN]\d*|A?IN_[PN]|AIN\d*|ADC\w*|"
                         r"DAC\w*|AOUT\d*|VREF\w*|REF\w*|COMP\d*|FB\d*|VFB|ISENSE|SENSE\w*|"
                         r"[PN]\d*|OUT[+-]")
_OPTO_LED = re.compile(r"A|K|AN|KA|CA|ANODE\d*|CATHODE\d*|LED\w*|IF\d*|[AK]\d")
_CONTROL_LOW_SIDE = frozenset({"DIR", "OE", "NOE", "OEN", "OE_N", "EN", "ENA", "NEN", "ENN"})
# Name tokens of differential signalling and field buses (a pair's halves are found by
# their partner net instead).
_DIFF_TOKENS = frozenset({"CANH", "CANL", "LVDS", "TMDS", "LVPECL", "HCSL", "CML", "MDI",
                          "TRD"})
# A pin name's last token that marks one half of a pair (TX0_P, RXN, REFCLK_N, D0P, L3p),
# and interfaces that are differential whatever the pin is called.
_PAIR_END = re.compile(r"[PN]\d*|\w*\d[PN]\d*|\w*(?:TX|RX|CLK|CK)[PN]\d*")
_DIFF_FAMILY = re.compile(r"LVDS\w*|TMDS\w*|SGMII\w*|XAUI\w*|SERDES\w*|MIPI\w*|LANE\d*|"
                          r"SSTX\w*|SSRX\w*|HSTX\w*|HSRX\w*")
# A bus transceiver's line-side pins: CAN, RS-485/422 (A, B, Y, Z), LIN, RS232 line
# driver outputs and receiver inputs, IO-Link, wake and inhibit pins at battery level.
_BUS_PIN = re.compile(r"CAN_?[HL]|N?[ABYZ]\d?|[AB][+-]|LIN|BUS[HL+-]?|DATA[+-]|D[+-]|"
                      r"T\d?OUT|R\d?IN|RS485_?[AB]|C_?Q|INH|WAKE")
# An analog output of a sensor or module (a Hall or temperature sensor's VOUT).
_ANALOG_OUT = re.compile(r"V_?OUT\d*[+-]?|AOUT\d*|ANA_?OUT\d*|VO\d*")
# Parts whose open-drain outputs are rated apart from their supply (an opto's or a
# comparator's output transistor, a regulator's power-good, a driver's fault pin).
_OWN_OUTPUT_RATING = frozenset({"optocoupler", "comparator", "power", "driver"})
_ANALOG_NET = frozenset({"analog", "rf"})


def _supply_class(name: str) -> str:
    first = first_function(name).lstrip("+")
    for cls, pattern in _SUPPLY_CLASSES:
        if pattern.fullmatch(first):
            return cls
    return "digital"


def _supply_bank(name: str) -> str | None:
    first = first_function(name).lstrip("+")
    match = _BANK_SUPPLY.fullmatch(first)
    if not match:
        return None
    bank = match["bank"]
    if _VOLTAGE_ID.fullmatch(bank) or (match["head"] in ("VDDIO", "VIO", "IOVDD", "VDDO")
                                        and bank in _TENTHS_ID):
        return None
    return bank


def _v(volts: float | None) -> str:
    return "?" if volts is None else f"{round(volts, 3):g}"


@dataclass
class Supply:
    pin: str
    name: str
    net: str
    cls: str
    bank: str | None = None


@dataclass
class Rail:
    net: str | None
    supply: str  # the supply pin names, e.g. 'VDDIO' or 'VCCO_14'
    basis: str  # only_supply, io_supply, bank, translator_side, digital_supply, highest_supply, none
    confidence: str  # high, medium, low
    detail: str
    volt: Volt | None = None

    @property
    def volts(self) -> float | None:
        return self.volt.volts if self.volt else None

    def describe(self) -> dict:
        out: dict = {"rail": self.net, "volts": self.volts, "supply_pins": self.supply,
                     "basis": self.detail, "confidence": self.confidence}
        if self.volt:
            out["voltage_source"] = self.volt.source
            if self.volt.confidence != "high":
                out["voltage_confidence"] = self.volt.confidence
        return {key: value for key, value in out.items() if value not in (None, "")}


@dataclass
class Chip:
    ref: str
    cls: str
    text: str
    names: dict[str, str]
    supplies: list[Supply]
    banks: dict[str, list[Supply]] = field(default_factory=dict)
    sides: dict[str, list[Supply]] = field(default_factory=dict)
    default: Rail | None = None
    out_banks: dict[str, list[Supply]] = field(default_factory=dict)  # a clock part's VDDOn
    out_default: Rail | None = None  # a clock part's output-driver supply (VDDO)
    tolerant: str | None = None  # why its inputs take 5 V
    fixed: str | None = None  # why its inputs have fixed thresholds
    ttl: bool = False
    pins: int = 0
    pin_numbers: dict[str, int] = field(default_factory=dict)

    @property
    def basis(self) -> str:
        if self.sides:
            return "translator_side"
        if self.banks:
            return "bank"
        return self.default.basis if self.default else "none"


def _chip_class(tree, ref: str, text: str, supplies: list[Supply]) -> str:
    if ref in tree.converters or ref in tree.references or ref in tree.supervisors or \
            ref in tree.not_fitted:
        return "power"
    if OPTO_TEXT.search(text):
        return "optocoupler"
    names = {first_function(supply.name) for supply in supplies}
    # VCCA/VCCB or VREF1/VREF2 pairs make a translator; VL beside VCC only with the words
    # (a serial line transceiver's VL is its logic supply).
    paired = ({"VCCA", "VCCB"} <= names
              or bool(names & {"VREF1", "VREF_1"} and names & {"VREF2", "VREF_2"}))
    if _TRANSLATOR_TEXT.search(text) or paired:
        return "translator"
    if _ISOLATOR_TEXT.search(text):
        return "isolator"
    if bus_transceiver(tree.ctx, ref):
        return "transceiver"
    if BRIDGE_TEXT.search(text):
        return "bridge"
    if ethernet_part(text):
        return "phy"
    kind = part_kind(text)
    if kind == "fpga":
        return "fpga"
    if kind in ("mcu", "radio", "module"):
        return "processor"
    if DRIVER_TEXT.search(text) or _POWER_DRIVER_TEXT.search(text):
        return "driver"
    if COMPARATOR_TEXT.search(text):
        return "comparator"
    if AMPLIFIER_TEXT.search(text):
        return "amplifier"
    output_supply = any(first_function(supply.name).startswith(("VDDO", "VCCO"))
                        for supply in supplies)
    if (_OSCILLATOR_TEXT.search(text) and len(tree.design.part_pins.get(ref) or {}) <= 8) or (
            output_supply and _CLOCK_TEXT.search(text)):
        return "clock"
    if _LOGIC_TEXT.search(text):
        return "logic"
    return "peripheral"


def _supplies(tree, ref: str, names: dict[str, str]) -> list[Supply]:
    design = tree.design
    out = []
    for pin, net in sorted((design.part_pins.get(ref) or {}).items(),
                           key=lambda item: natural_key(item[0])):
        if net in tree.grounds:
            continue
        name = names.get(pin) or ""
        role = pin_role(name)
        if role != "supply" and not (design.pin_type(ref, pin) == "Power" and role == "none"
                                     and net in design.rails):
            continue
        # A supply pin whose net reaches only the part and capacitors is its own internal
        # regulator's output (a bias or bootstrap pin such as VDDB), not a rail it runs from.
        others = {other for other, _ in tree.members(net) if other != ref}
        own = bool(others) and all(tree.kind(other) == "capacitor" for other in others)
        out.append(Supply(pin, name, net, "skip" if own else _supply_class(name),
                          _supply_bank(name)))
    return out


def _side_of_supply(chip: Chip, supply: Supply) -> str | None:
    first = first_function(supply.name)
    if chip.cls == "translator":
        if re.fullmatch(r"VCC_?A|VDD_?A", first):
            return "A"
        if re.fullmatch(r"VCC_?B|VDD_?B", first):
            return "B"
        if first == "VL":
            return "L"
        if first == "VCC":
            return "H"
    match = re.fullmatch(r"V(?:CC|DD|REF|IO|DDIO)_?([12])", first)
    if match and chip.cls in ("translator", "isolator"):
        return match[1]
    return None


def _volt(voltages: dict[str, Volt], net: str | None) -> Volt | None:
    return voltages.get(net) if net else None


def _names(supplies: list[Supply]) -> str:
    return ", ".join(dict.fromkeys(first_function(supply.name) or supply.pin
                                   for supply in supplies))


def _highest(groups: dict[str, list[Supply]], voltages: dict[str, Volt]) -> str:
    """The net of the highest known voltage (by name when none is known)."""
    return max(groups, key=lambda net: (voltages[net].volts if net in voltages else -1e9,
                                        tuple(natural_key(net))))


def _default_rail(chip: Chip, voltages: dict[str, Volt]) -> Rail:
    """A pin's rail when its bank and side say nothing (see the module notes)."""
    rail = _default_choice(chip, voltages)
    power_in = [supply for supply in chip.supplies if supply.cls == "power_in"
                and supply.net != rail.net]
    if power_in and rail.net is not None:
        rail.detail += f"; {_names(power_in)} taken as a power input"
    return rail


def _default_choice(chip: Chip, voltages: dict[str, Volt]) -> Rail:
    usable = [supply for supply in chip.supplies
              if supply.cls not in ("skip", "io_out", "power_in")] or [
        supply for supply in chip.supplies if supply.cls == "io_out"] or [
        supply for supply in chip.supplies if supply.cls == "power_in"]
    if not usable:
        return Rail(None, "", "none", "low", "no supply pin read by name")

    def by_net(items: list[Supply]) -> dict[str, list[Supply]]:
        out: dict[str, list[Supply]] = defaultdict(list)
        for item in items:
            out[item.net].append(item)
        return out

    nets = by_net(usable)
    if len(nets) == 1:
        net, items = next(iter(nets.items()))
        return Rail(net, _names(items), "only_supply", "high",
                    f"its only supply ({_names(items)})")
    ports = [item for item in usable if item.cls == "io_port"]
    for classes, basis, confidence, what, plural in (
            (("io", "io_out"), "io_supply", "high", "its I/O supply", "I/O supplies"),
            (("dvdd", "digital"), "digital_supply", "medium", "its digital supply",
             "digital supplies"),
            (("backup",), "digital_supply", "medium", "its battery-domain supply",
             "battery-domain supplies")):
        if chip.banks and "io" not in classes:
            break  # a banked part's pin sits on one of its banks, not on its core supply
        chosen = by_net([item for item in usable if item.cls in classes and (
            item.bank is None or not chip.banks)])
        if len(chosen) == 1:
            net, items = next(iter(chosen.items()))
            if chip.banks and "io" in classes:
                return Rail(net, _names(items), basis, "medium",
                            f"{what} outside its banks ({_names(items)}): the pin's bank is "
                            "not in its name")
            if ports:
                return Rail(net, _names(items), basis, "low",
                            f"{what} ({_names(items)}), a guess: {_names(ports)} powers "
                            "only some of its pins (a port or bank, which ones is in the "
                            "datasheet)")
            return Rail(net, _names(items), basis, confidence,
                        f"{what} ({_names(items)}), among others")
        if chosen:
            net = _highest(chosen, voltages)
            return Rail(net, _names(chosen[net]), "highest_supply", "low",
                        f"the highest of its {len(chosen)} {plural} "
                        f"({_names(chosen[net])})")
    if chip.banks:
        bank_nets = by_net([item for item in usable if item.bank is not None])
        net = _highest(bank_nets, voltages)
        return Rail(net, _names(bank_nets[net]), "highest_supply", "low",
                    "the highest of its bank supplies (the pin's bank is not in its name)")
    net = _highest(nets, voltages)
    return Rail(net, _names(nets[net]), "highest_supply", "low",
                f"the highest of its supplies ({_names(nets[net])}; no digital or I/O supply "
                "pin by name)")


def _tolerance(chip_text: str) -> str | None:
    if match := _TOLERANT_TEXT.search(chip_text):
        return f"its description says '{match[0]}'"
    if match := _TOLERANT_FAMILY.search(chip_text):
        return f"a {match[0][:12]} family part, whose inputs take 5.5 V at any supply"
    return None


def _output_supplies(chip: Chip, voltages: dict[str, Volt]) -> None:
    """A clock part's VDDO/VCCO pins power its outputs only: its output rail (per output
    bank when VDDO0, VDDO1 ... sit on different nets), its inputs left to the others."""
    outs = [supply for supply in chip.supplies if supply.cls == "io"
            and first_function(supply.name).startswith(("VDDO", "VCCO"))]
    if not outs or len(outs) == len([item for item in chip.supplies if item.cls != "skip"]):
        return
    for supply in outs:
        supply.cls = "io_out"
    banks: dict[str, list[Supply]] = defaultdict(list)
    for supply in outs:
        if supply.bank is not None:
            banks[supply.bank].append(supply)
    if len({items[0].net for items in banks.values()}) >= 2:
        chip.out_banks = dict(banks)
    nets: dict[str, list[Supply]] = defaultdict(list)
    for supply in outs:
        nets[supply.net].append(supply)
    if len(nets) == 1:
        net, items = next(iter(nets.items()))
        chip.out_default = Rail(net, _names(items), "io_supply", "high",
                                f"its output-driver supply ({_names(items)})",
                                _volt(voltages, net))
    else:
        net = _highest(nets, voltages)
        chip.out_default = Rail(net, _names(nets[net]), "highest_supply", "low",
                                f"the highest of its output-driver supplies "
                                f"({_names(nets[net])}; the output's bank is not in its name)",
                                _volt(voltages, net))
    for supply in outs:
        supply.bank = None  # not an I/O bank of the inputs


def _port_supplies(chip: Chip, banks: dict[str, list[Supply]]) -> None:
    """A lone numbered I/O supply (VDDIO2) beside a digital supply on another net powers
    one port or bank only (an STM32's VDDIO2 powers port G): io_port, not the part's I/O
    rail."""
    if len(banks) != 1:
        return
    ported = next(iter(banks.values()))
    nets = {supply.net for supply in ported}
    if any(supply.cls == "io" and supply not in ported for supply in chip.supplies):
        return
    if any(supply.cls in ("dvdd", "digital") and supply.net not in nets
           for supply in chip.supplies):
        for supply in ported:
            supply.cls = "io_port"


def build_chips(tree, voltages: dict[str, Volt]) -> dict[str, Chip]:
    """Every fitted IC-like part (U, IC, modules, other active parts with a supply pin,
    and oscillators with a supply pin whatever their prefix, such as Y1) with its
    supplies, banks, translator sides and default I/O rail."""
    chips: dict[str, Chip] = {}
    design = tree.design
    for ref in sorted(design.part_pins, key=natural_key):
        kind = tree.kind(ref)
        if kind not in ("ic", "other", "passive") or not tree.fitted(ref):
            continue
        names = tree.pin_names(ref)
        supplies = _supplies(tree, ref, names)
        text = tree.text(ref)
        if kind == "other" and not supplies and part_kind(text) == "other" and \
                not OPTO_TEXT.search(text):
            continue
        if kind == "passive" and not (supplies and (_OSCILLATOR_TEXT.search(text) or any(
                design.pin_type(ref, pin) == "Output" for pin in design.part_pins[ref]))):
            continue
        chip = Chip(ref, "peripheral", text, names, supplies)
        chip.cls = _chip_class(tree, ref, text, supplies)
        if chip.cls == "power":
            for supply in supplies:
                if _BIAS_PIN.fullmatch(first_function(supply.name)):
                    supply.cls = "skip"  # a regulator's own bias output, not its logic rail
        meta = tree.components.get(ref) or {}
        numbered = [pin for pin in meta.get("pin_designators") or names if pin and pin.isdigit()]
        chip.pins = len(numbered)
        chip.pin_numbers = {pin: int(pin) for pin in numbered}
        if chip.cls == "clock":
            _output_supplies(chip, voltages)
        banks: dict[str, list[Supply]] = defaultdict(list)
        for supply in supplies:
            if supply.cls == "io" and supply.bank is not None:
                banks[supply.bank].append(supply)
        if len({supply.net for items in banks.values() for supply in items}) >= 2:
            chip.banks = dict(banks)
        else:
            _port_supplies(chip, banks)
            for supply in supplies:
                supply.bank = None
        if chip.cls in ("translator", "isolator"):
            sides: dict[str, list[Supply]] = defaultdict(list)
            for supply in supplies:
                side = _side_of_supply(chip, supply)
                if side:
                    sides[side].append(supply)
            if len({items[0].net for items in sides.values()}) >= 2:
                chip.sides = dict(sides)
        chip.default = _default_rail(chip, voltages)
        chip.default.volt = _volt(voltages, chip.default.net)
        chip.tolerant = _tolerance(text)
        chip.ttl = bool(_TTL_TEXT.search(text))
        chip.fixed = _FIXED_CLASSES.get(chip.cls)
        chips[ref] = chip
    return chips


def _pin_bank(banks: dict, name: str, digits: bool = False) -> tuple[str, str] | None:
    """(bank, how) a pin's name gives, when that bank is one of the part's: a vendor's
    bank pattern in any alternative first (IO_L1P_T0_14, LVDS3G_6p), then a BANK2 or B14
    token, then a last token or a whole token equal to a bank (DONE_0, HPS_CLK1); with
    digits, the one number in the name (a clock output CLK2A on VDDO2)."""
    alts = alternatives(name)
    for alt in alts:
        if (match := _XILINX_BANK.fullmatch(alt)) and match[1] in banks:
            return match[1], "its name's bank number"
        for match in _INTEL_BANK.finditer(alt):
            if match[1] in banks:
                return match[1], "its name's bank"
    for alt in alts:
        for token in alt.split("_"):
            if (match := _BANK_WORD.fullmatch(token)) and match[1] in banks:
                return match[1], "its name's bank"
    for alt in alts:
        tokens = [token for token in alt.split("_") if token]
        if len(tokens) > 1 and tokens[-1] in banks:
            return tokens[-1], "its name's bank suffix"
        for token in tokens:
            if not token.isdigit() and len(token) > 1 and token in banks:
                return token, "its name's bank"
    if digits:
        numbers = {number for alt in alts for number in re.findall(r"\d+", alt)}
        if len(numbers) == 1 and (number := numbers.pop()) in banks:
            return number, "the output number in its name"
    return None


def _pin_side(chip: Chip, pin: str, name: str) -> tuple[str, str] | None:
    """(side, how) of a translator's or isolator's signal pin."""
    sides = set(chip.sides)
    tokens = [token for alt in alternatives(name) for token in net_tokens(alt)]
    if {"A", "B"} & sides:
        for token in tokens:
            if re.fullmatch(r"A\d*|\d+A", token) and "A" in sides:
                return "A", "an A-side pin by its name"
            if re.fullmatch(r"B\d*|\d+B", token) and "B" in sides:
                return "B", "a B-side pin by its name"
    if {"L", "H"} & sides:
        for token in tokens:
            if token.startswith("VL"):
                return "L", "a VL-side pin by its name"
            if token.startswith("VCC"):
                return "H", "a VCC-side pin by its name"
    if {"1", "2"} & sides:
        for token in tokens:
            if (match := re.fullmatch(r"[A-Z]+_?([12])", token)) and match[1] in sides:
                return match[1], f"a side-{match[1]} pin by its name"
    if tokens and set(tokens) & _CONTROL_LOW_SIDE:
        low = "A" if "A" in sides else "L" if "L" in sides else None
        if low:
            return low, "a direction or enable input, referenced to the A (low) side"
    number = chip.pin_numbers.get(pin)
    if number is not None and chip.pins >= 4:
        half = chip.pins / 2
        lower = [side for side, items in chip.sides.items()
                 if all(chip.pin_numbers.get(item.pin, 0) and chip.pin_numbers[item.pin] <= half
                        for item in items)]
        upper = [side for side, items in chip.sides.items()
                 if all(chip.pin_numbers.get(item.pin, 0) > half for item in items)]
        if len(lower) == 1 and len(upper) == 1:
            side = lower[0] if number <= half else upper[0]
            return side, "the half of the pins its side's supply sits in"
    return None


def pin_rail(chip: Chip, pin: str, voltages: dict[str, Volt], direction: str | None = None
             ) -> Rail:
    """The I/O rail of one of a part's signal pins, with basis and confidence; a clock
    part's output pins take its output-driver supply (VDDO)."""
    name = chip.names.get(pin) or ""
    if chip.out_default is not None and direction == "out":
        if chip.out_banks and (found := _pin_bank(chip.out_banks, name, digits=True)):
            bank, how = found
            items = chip.out_banks[bank]
            return Rail(items[0].net, _names(items), "bank", "high",
                        f"output bank {bank} by {how}: {_names(items)}",
                        _volt(voltages, items[0].net))
        return chip.out_default
    if chip.sides:
        found = _pin_side(chip, pin, name)
        if found:
            side, how = found
            items = chip.sides[side]
            confidence = "medium" if how.startswith("the half") else "high"
            return Rail(items[0].net, _names(items), "translator_side", confidence,
                        f"{how}: {_names(items)}", _volt(voltages, items[0].net))
        default = chip.default
        return Rail(default.net, default.supply, "translator_side", "low",
                    f"a translator pin whose side its name does not give: {default.detail}",
                    default.volt)
    if chip.banks:
        found = _pin_bank(chip.banks, name)
        if found:
            bank, how = found
            items = chip.banks[bank]
            return Rail(items[0].net, _names(items), "bank", "high",
                        f"bank {bank} by {how}: {_names(items)}", _volt(voltages, items[0].net))
    return chip.default


# ---------- pin directions ----------

@dataclass
class End:
    chip: Chip
    pin: str
    name: str
    net: str
    direction: str  # out, od, od_out, in, bidir, unknown
    certain: bool
    basis: str
    rail: Rail
    tolerant: str | None = None

    @property
    def ref(self) -> str:
        return self.chip.ref

    def label(self) -> str:
        name = " ".join(self.name.split())
        if len(name) > 32:
            name = name[:30] + ".."
        return f"{self.ref}.{self.pin}" + (f" ({name})" if name and name != self.pin else "")


_EXCLUDE = {"analog", "crystal", "differential", "nc", "power"}


def _tokens(alt: str) -> set[str]:
    out = set()
    for token in net_tokens(alt):
        out.add(token)
        if len(token) > 2 and token[0] == "N" and token[1].isalpha():
            out.add(token[1:])
    return out


def _pair_half(alt: str, lower_pairs: frozenset[str] = frozenset()) -> bool:
    """A pin name of one half of a differential pair (TX0_P, REFCLK_N, D0P, CLK+, or a
    name in lower_pairs, which the pin wrote with Intel's lower-case p or n suffix: CHTp)
    or of a differential-only interface (LVDS, TMDS, MIPI, SGMII)."""
    if alt.endswith(("+", "-")) or any(alt.endswith(pair) for pair in lower_pairs):
        return True
    tokens = net_tokens(alt)
    return bool(tokens) and (bool(_PAIR_END.fullmatch(tokens[-1])) or any(
        _DIFF_FAMILY.fullmatch(token) for token in tokens))


def _alt_direction(chip: Chip, alt: str, lower_pairs: frozenset[str] = frozenset()
                   ) -> tuple[str, bool, str] | None:
    """(direction, certain, basis) one alternative of a pin name gives for its part's
    class, or (an exclusion reason, True, basis) for analog, crystal and differential pins."""
    cls = chip.cls
    master = cls in _PROCESSOR_CLASSES or cls == "bridge"
    tokens = _tokens(alt)
    found = classify(alt, pin=True)
    what = {"processor": "a processor's", "fpga": "an FPGA's", "bridge": "a bridge's",
            "transceiver": "a bus transceiver's", "phy": "a PHY's"}.get(cls, "a peripheral's")
    if cls == "optocoupler":
        if _OPTO_LED.fullmatch(alt):
            return "analog", True, "an optocoupler's LED pin (current-driven)"
        if re.fullmatch(r"C\d*|E\d*|VO\d*|OUT\d*|COL\w*|EMIT\w*|VOUT\d*", alt):
            return "od", True, "an optocoupler's output transistor"
    if cls in ("amplifier", "comparator") and _ANALOG_PIN.fullmatch(alt):
        return "analog", True, f"an {'op-amp' if cls == 'amplifier' else 'comparator'} input"
    if cls == "amplifier":
        return "analog", True, "an amplifier's pin"
    if cls == "comparator" and re.fullmatch(r"\w*OUT\w*|\d*Y", alt):
        return "od", False, "a comparator output (often open-drain)"
    if cls == "transceiver" and _BUS_PIN.fullmatch(alt):
        return "differential", True, (f"a bus transceiver's line-side pin ({alt}): "
                                      "differential or field-bus levels")
    if cls not in ("logic", "clock", "comparator", "power") and _ANALOG_OUT.fullmatch(alt):
        return "analog", True, f"an analog output ({alt})"
    if found:
        function, role = found
        if function in ("analog", "rf"):
            return "analog", True, f"an analog pin ({alt})"
        if function == "crystal":
            return "crystal", True, f"a crystal pin ({alt})"
        if function in ("usb", "high_speed"):
            if _pair_half(alt, lower_pairs) or (function == "usb"
                                                and role in ("dp", "dm", None)):
                return "differential", True, f"a differential or high-speed pin ({alt})"
            return None  # a single-ended clock or sideband line (GTX_CLK, PERST, SFP_LOS)
        if function == "nc":
            return "nc", True, "not connected"
        if function == "i2c":
            return "od", True, f"an I2C line ({alt})"
        if function in ("uart", "debug_uart", "can"):
            if cls == "transceiver":
                if any(XCVR_IN.fullmatch(token) for token in tokens):
                    return "in", True, f"a bus transceiver's driver input ({alt})"
                if any(XCVR_OUT.fullmatch(token) for token in tokens):
                    return "out", True, f"a bus transceiver's receiver output ({alt})"
                return None
            direction = {"tx": "out", "rx": "in", "rts": "out", "cts": "in"}.get(role or "")
            if cls == "phy" and role in ("tx", "rx"):
                # An Ethernet PHY's TXD takes the MAC's data, its RXD drives the MAC.
                return ("in" if role == "tx" else "out"), True, f"{what} {alt} (MII side)"
            if role == "de":
                direction = "out" if master else "in"
            if direction:
                return direction, True, f"{what} {alt}"
            return None
        if function == "spi":
            if master:
                if tokens & _MASTER_OUT or role in ("sck", "cs"):
                    return "out", True, f"{what} SPI {alt} (master)"
                if tokens & _MASTER_IN:
                    return "in", True, f"{what} SPI {alt} (master)"
            else:
                if tokens & SLAVE_OUT:
                    return "out", True, f"{what} SPI {alt}"
                if tokens & SLAVE_IN or role in ("sck", "cs"):
                    return "in", True, f"{what} SPI {alt}"
            return None
        if function == "qspi":
            if role in ("clk", "cs"):
                return ("out" if master else "in"), True, f"{what} QSPI {alt}"
            return "bidir", True, f"{what} QSPI data line ({alt})"
        if function == "sdio":
            if role == "clk":
                return ("out" if master else "in"), True, f"{what} SDIO clock ({alt})"
            return "bidir", True, f"{what} SDIO line ({alt})"
        if function == "ethernet":
            if role == "mdio":
                return "bidir", True, f"{what} MDIO"
            if role in ("tx", "mdc"):
                return ("in" if cls == "phy" else "out"), True, f"{what} {alt}"
            if role == "rx":
                return ("out" if cls == "phy" else "in"), True, f"{what} {alt}"
            return None
        if function in ("jtag", "swd"):
            if cls == "bridge":
                return None
            if role in ("tdo", "swo"):
                return "out", True, f"{what} {alt} (a debug target's output)"
            if role == "swdio":
                return "bidir", True, f"{what} SWDIO"
            return "in", True, f"{what} {alt} (a debug target's input)"
        if function == "i2s":
            if tokens & {"SDO", "DOUT", "DO"}:
                return "out", True, f"{what} {alt}"
            if tokens & {"SDI", "DIN", "DI"}:
                return "in", True, f"{what} {alt}"
            return None
        if function == "reset":
            if cls == "power":
                return "od_out", False, f"a supervisor's or regulator's {alt} output"
            return "in", True, f"{what} reset input ({alt})"
        if function in ("enable", "boot_strap", "programming", "button", "wakeup"):
            if master and function == "enable":
                return None
            return "in", True, f"{what} {function.replace('_', ' ')} input ({alt})"
        if function == "interrupt":
            if master:
                return "in", True, f"{what} interrupt input ({alt})"
            return "od_out", False, f"{what} {alt} output (open-drain or push-pull)"
        if function in ("pwm_timer", "led"):
            if master:
                return "out", False, f"{what} {alt}"
            return None
    if cls == "logic":
        if _LOGIC_OUT.fullmatch(alt):
            return "out", True, f"a logic part's output ({alt})"
        if _LOGIC_IN.fullmatch(alt):
            return "in", True, f"a logic part's input ({alt})"
    if cls == "transceiver":
        if any(XCVR_IN.fullmatch(token) for token in tokens):
            return "in", True, f"a transceiver's logic input ({alt})"
        if any(XCVR_OUT.fullmatch(token) for token in tokens):
            return "out", True, f"a transceiver's logic output ({alt})"
    if any(OUT_NAME.fullmatch(token) or _GENERIC_OUT.fullmatch(token) for token in tokens):
        return "out", True, f"{what} output ({alt})"
    if any(IN_NAME.fullmatch(token) or _GENERIC_IN.fullmatch(token) for token in tokens):
        return "in", True, f"{what} input ({alt})"
    if not master and control_kind(alt, pin=True):
        return "in", True, f"{what} control input ({alt})"
    if not master and open_drain_name(alt, pin=True):
        return "od_out", False, f"{what} {alt} output (open-drain or push-pull)"
    return None


def pin_direction(chip: Chip, pin: str, electrical: str | None, net: str
                  ) -> tuple[str, bool, str]:
    """(direction, certain, basis) of an IC pin on a signal: out, od (open-drain), od_out
    (an output that may be either), in, bidir, unknown, or an exclusion (analog, crystal,
    differential, nc, power)."""
    name = chip.names.get(pin) or ""
    role = pin_role(name)
    if role in ("ref", "cap", "ground") or (role == "supply" and electrical != "IO"):
        return "power", True, f"a {role} pin"
    if electrical == "Power":
        return "power", True, "typed Power"
    by_name = None
    processor = chip.cls in _PROCESSOR_CLASSES
    every = alternatives(name)
    # A processor pin with a port or generic IO name is a GPIO first: its alternate
    # functions (ADC, LVDS, OSC) do not make the net analog or differential.
    gpio = processor and any(port_like(alt) for alt in every)
    alts = [alt for alt in every if alt and not port_like(alt)]
    # Names written with Intel's lower-case pair suffix (CHTp, L3n), before alternatives()
    # upper-cases them.
    lower_pairs = frozenset(token.upper() for token in re.findall(
        r"[A-Za-z0-9]*[A-Z0-9][pn](?![A-Za-z0-9])", name.replace("_", " ")))
    hits = [hit for alt in alts if (hit := _alt_direction(chip, alt, lower_pairs))]
    excluded = [hit for hit in hits if hit[0] in _EXCLUDE]
    if excluded and not gpio and (len(excluded) == len(hits) or not processor):
        return excluded[0]
    hits = [hit for hit in hits if hit[0] not in _EXCLUDE]
    if hits and len({hit[0] for hit in hits}) == 1:
        direction, certain, basis = hits[0]
        # Several functions that agree on the direction: likely, not certain.
        by_name = (direction, certain and len(hits) == 1 and not excluded, basis)
    if electrical in ("OpenCollector", "OpenEmitter"):
        return "od", True, f"typed {electrical}"
    if electrical == "Output":
        return "out", True, "typed Output"
    if electrical == "HiZ":
        return "out", True, "typed HiZ (a tri-state output)"
    if electrical == "Input":
        return "in", True, "typed Input"
    if electrical == "IO":
        if by_name and by_name[0] == "od":
            return by_name
        return "bidir", True, "typed IO"
    if by_name:
        return by_name
    if hits:
        return "unknown", False, f"its name lists functions of both directions ({name[:40]})"
    if chip.cls in _PROCESSOR_CLASSES:
        found = classify(net)
        if found and found[0] in ("uart", "debug_uart") and found[1] in ("tx", "rx"):
            return ("out" if found[1] == "tx" else "in"), False, \
                f"net name {net}, read from the processor's side"
        if found and found[0] == "spi" and found[1]:
            return ("in" if found[1] == "miso" else "out"), False, \
                f"net name {net}, read from the processor's side (master)"
        return "unknown", False, "a processor GPIO whose name gives no direction"
    return "unknown", False, "typed Passive and its name gives no direction"


# ---------- signals ----------

@dataclass
class Signal:
    nets: list[str]
    ends: list[End] = field(default_factory=list)
    connectors: list[str] = field(default_factory=list)
    pull_ups: list = field(default_factory=list)  # pull_resistors.Pull
    pull_downs: list = field(default_factory=list)
    mode: str = "push_pull"  # push_pull, open_drain, open_drain_unpulled
    excluded: str | None = None
    why: str | None = None
    crossings: list[dict] = field(default_factory=list)  # series resistors judged across

    @property
    def name(self) -> str:
        return self.nets[0]

    def refs(self) -> set[str]:
        return {end.ref for end in self.ends}


def _pair_index(nets) -> dict[str, list[tuple[str, str]]]:
    """Each net's polarity keys (diff_pair_checks.polarity_tokens), for _partner."""
    index: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for net in nets:
        for key, polarity, _ in polarity_tokens(net):
            index[key].append((net, polarity))
    return index


def _partner(net: str, index: dict[str, list[tuple[str, str]]]) -> bool:
    """A differential half: a net whose complement (P/N, +/-, DP/DM, CAN H/L and the other
    styles diff_pair_checks.complement reads) exists; an active-low RESET_N alone is no
    pair."""
    for key, polarity, _ in polarity_tokens(net):
        for other, other_polarity in index.get(key, ()):
            if other != net and other_polarity != polarity and complement(net, other):
                return True
    return False


def _excluded_net(net: str, index: dict[str, list[tuple[str, str]]]) -> str | None:
    """differential (a pair whose partner exists, a field bus, LVDS, TMDS ...) or analog
    (ADC, DAC, sense, temperature names, unless a control or interrupt word is in it)."""
    tokens = set(net_tokens(net))
    if _partner(net, index) or tokens & _DIFF_TOKENS:
        return "differential"
    found = classify(net)
    if found and found[0] == "usb" and found[1] in ("dp", "dm"):
        return "differential"
    if found and found[0] in _ANALOG_NET and not (open_drain_name(net) or control_kind(net)):
        return "analog"
    return None


def _crystal(tree, ref: str, chips: dict[str, Chip]) -> bool:
    """A crystal or resonator: not an oscillator with a supply pin, whatever its prefix."""
    if ref in chips and chips[ref].supplies:
        return False
    prefix = designator_prefix(ref)
    if prefix in ("Y", "XTAL", "XT", "XC"):
        return True
    return tree.kind(ref) in ("passive", "other", "connector") and bool(
        CRYSTAL_TEXT.search(tree.text(ref))) and not _OSCILLATOR_TEXT.search(tree.text(ref))


def _oscillator_rail(chip: Chip, pin: str, voltages: dict[str, Volt]) -> Rail:
    """A crystal-oscillator pin's rail, low confidence: its oscillator often runs from an
    analog or I/O supply other than the pin's bank, so the highest of the part's I/O,
    digital and analog supplies of known voltage (else the pin's own rail)."""
    known = {supply.net: supply for supply in chip.supplies
             if supply.cls in ("io", "io_out", "io_port", "digital", "dvdd", "analog")
             and supply.net in voltages}
    if not known:
        rail = pin_rail(chip, pin, voltages, "in")
        return Rail(rail.net, rail.supply, rail.basis, "low",
                    f"{rail.detail}; a crystal-oscillator pin, whose oscillator may run from "
                    "another supply", rail.volt)
    net = max(known, key=lambda name: (voltages[name].volts, tuple(natural_key(name))))
    items = [supply for supply in chip.supplies if supply.net == net]
    return Rail(net, _names(items), "highest_supply", "low",
                f"the highest of its supplies ({_names(items)}): a crystal-oscillator pin, "
                "whose oscillator supply the datasheet gives", voltages[net])


def _end(chip: Chip, pin: str, net: str, direction: str, certain: bool, basis: str,
         rail: Rail) -> End:
    end = End(chip, pin, chip.names.get(pin) or "", net, direction, certain, basis, rail)
    end.tolerant = ("its pin name marks it 5 V tolerant"
                    if _TOLERANT_PIN.search(end.name.upper()) else chip.tolerant)
    return end


def _build_signal(tree, chips: dict[str, Chip], group: list[str], voltages, pairs,
                  stats: Counter) -> Signal:
    design = tree.design
    signal = Signal(list(group))
    for net in group:
        reason = _excluded_net(net, pairs)
        if reason:
            signal.excluded, signal.why = reason, f"by the net name {net}"
            return signal
    crystal_pins: list[tuple[Chip, str, str, str]] = []
    for net in group:
        for ref, pin in design.nets.get(net, ()):
            if not tree.fitted(ref):
                continue
            if _crystal(tree, ref, chips):
                signal.excluded, signal.why = "crystal", f"{ref} is a crystal"
                return signal
            if tree.kind(ref) == "connector":
                if ref not in signal.connectors:
                    signal.connectors.append(ref)
                continue
            chip = chips.get(ref)
            if chip is None:
                continue
            electrical = design.pin_type(ref, pin)
            direction, certain, basis = pin_direction(chip, pin, electrical, net)
            if direction == "crystal":
                crystal_pins.append((chip, pin, net, basis))
                continue
            if direction in ("analog", "differential"):
                signal.excluded, signal.why = direction, f"{ref}.{pin}: {basis}"
                return signal
            if direction in ("nc", "power"):
                stats[f"pins_skipped_{direction}"] += 1
                continue
            signal.ends.append(_end(chip, pin, net, direction, certain, basis,
                                    pin_rail(chip, pin, voltages, direction)))
    if crystal_pins:
        # A crystal pin (XTAL_IN, CLKIN) on a line an active clock drives (an oscillator,
        # a clock buffer): judged as an input whose oscillator supply is a guess.
        if not any(end.direction == "out" for end in signal.ends):
            chip, pin, _, basis = crystal_pins[0]
            signal.excluded, signal.why = "crystal", f"{chip.ref}.{pin}: {basis}"
            return signal
        for chip, pin, net, basis in crystal_pins:
            signal.ends.append(_end(chip, pin, net, "in", False,
                                    f"{basis}, driven by an active clock",
                                    _oscillator_rail(chip, pin, voltages)))
    for net in group:
        for pull in design.pulls.get(net, ()):
            if not pull.fitted:
                continue
            if pull.direction == "up":
                signal.pull_ups.append(pull)
            elif pull.direction == "down":
                signal.pull_downs.append(pull)
    open_drain = any(end.direction == "od" for end in signal.ends) or any(
        i2c_line(net) for net in group) or (signal.pull_ups and any(
            end.direction == "od_out" for end in signal.ends)) or (signal.pull_ups and any(
                open_drain_name(net) for net in group))
    if open_drain:
        signal.mode = "open_drain" if signal.pull_ups else "open_drain_unpulled"
    if len(signal.refs()) < 2:
        signal.excluded = "one_ic"
    return signal


def build_signals(tree, chips: dict[str, Chip], voltages, stats: Counter
                  ) -> tuple[list[Signal], dict[str, Signal]]:
    """Every signal (nets joined through links) that is not a rail or a ground, with its
    IC ends; and the signal of each net."""
    design = tree.design
    pairs = _pair_index(design.nets)
    signals: list[Signal] = []
    of_net: dict[str, Signal] = {}
    for net in sorted(design.nets, key=lambda name: (name.upper().startswith("NET"),
                                                     natural_key(name))):
        if net in of_net or design.fixed(net):
            continue
        group = [member for member in link_group(design, net) if member not in of_net]
        signal = _build_signal(tree, chips, group, voltages, pairs, stats)
        signals.append(signal)
        for member in group:
            of_net[member] = signal
    return signals, of_net


# ---------- judging ----------

@dataclass
class _Pair:
    driver: End
    receiver: End
    level: float  # the driver's high level at the receiver
    level_basis: str
    series: tuple[str, float | None] | None = None  # (resistor, ohms) crossed
    pull_up: list | None = None  # the pull-ups that set an open-drain level
    idle: bool = False  # a pull-up on a push-pull line: its level when nothing drives
    # A pin under the pull-up that does not receive: 'output' (a push-pull output, back-
    # driven when it is tri-stated or high) or 'od_output' (an open-drain output).
    exposed: str | None = None


def _pulled_pins(signal: Signal) -> list[tuple[End, str | None]]:
    """(pin, exposed) of each pin a pull-up reaches: receivers (exposed None), push-pull
    outputs ('output') and open-drain outputs ('od_output'), but not the outputs of
    optocouplers, comparators, regulators and drivers, which are rated apart from their
    supply."""
    out: list[tuple[End, str | None]] = []
    for end in signal.ends:
        if end.direction in ("in", "bidir", "unknown"):
            out.append((end, None))
        elif end.direction == "od":
            found = classify(end.name, pin=True)
            if (not end.basis.startswith("typed") and "output" not in end.basis) or (
                    found and (found[0] == "i2c" or found == ("ethernet", "mdio"))):
                out.append((end, None))  # an I2C or MDIO line: it receives as well
            elif end.chip.cls not in _OWN_OUTPUT_RATING:
                out.append((end, "od_output"))
        elif end.direction == "od_out":
            if end.chip.cls not in _OWN_OUTPUT_RATING:
                out.append((end, "od_output"))
        elif end.direction == "out":
            out.append((end, "output"))
    return out


def _drives(end: End) -> bool:
    """A pin that may drive a line; a regulator's or supervisor's pin only when typed or
    named an output (its other pins are inputs, analog or its own supply)."""
    if end.chip.cls == "power" and not end.certain:
        return False
    return end.direction in ("out", "bidir", "unknown", "od_out")


def _receives(end: End) -> bool:
    return end.direction in ("in", "bidir", "unknown", "od")


def _certain_pair(driver: End, receiver: End) -> bool:
    return (driver.certain and driver.direction == "out") or (
        receiver.certain and receiver.direction == "in")


def _push_pull_pairs(drivers: list[End], receivers: list[End]
                     ) -> list[tuple[End, End]]:
    """(driver, receiver) pairs of a push-pull line: when a certain push-pull output is on
    it, the bidirectional and unknown pins only receive."""
    certain_out = [end for end in drivers if end.direction == "out" and end.certain]
    if certain_out:
        drivers = certain_out + [end for end in drivers if end.direction == "out"
                                 and end not in certain_out]
    out = []
    for driver in drivers:
        if not _drives(driver) or driver.direction == "od":
            continue
        for receiver in receivers:
            if receiver.ref == driver.ref or not _receives(receiver):
                continue
            if receiver.direction == "od":
                continue  # an open-drain pin on a push-pull line: a library slip
            out.append((driver, receiver))
    return out


def _cap(severity: str, notes: list[str], driver: End | None, receiver: End,
         certain: bool) -> str:
    if severity != "warning":
        return severity
    if not certain:
        notes.append("the direction is a guess (both pins bidirectional or of unknown "
                     "direction): judged both ways")
        return "info"
    for end in filter(None, (driver, receiver)):
        if end.rail.confidence == "low":
            notes.append(f"{end.ref}'s I/O rail is a low-confidence guess ({end.rail.detail})")
            return "info"
        if end.rail.volt and end.rail.volt.confidence == "low":
            notes.append(f"{end.rail.net}'s voltage is a low-confidence reading")
            return "info"
    return severity


def _end_row(end: End) -> dict:
    out = {"designator": end.ref, "part_class": end.chip.cls, **end.rail.describe()}
    return out


def _judge(pair: _Pair) -> list[dict]:
    driver, receiver = pair.driver, pair.receiver
    v_drv, v_rcv = pair.level, receiver.rail.volts
    if v_rcv is None or v_rcv <= 0 or v_drv is None:
        return []
    out = []
    pulled = pair.pull_up is not None
    certain = _certain_pair(driver, receiver) if not pulled else True
    open_drain = pulled and not pair.idle
    source = (f"pulled up to {_v(v_drv)} V ({pair.level_basis})" if open_drain else
              f"pulled up to {_v(v_drv)} V ({pair.level_basis}) whenever no driver holds the "
              "line" if pulled else f"{driver.ref} drives {_v(v_drv)} V ({pair.level_basis})")
    # Overvoltage: above VDD + 0.3 V of the receiver.
    limit = v_rcv + ABS_MAX_MARGIN
    if v_drv > limit + 1e-9:
        check = "open_drain_pullup_mismatch" if open_drain else "receiver_overvoltage"
        notes: list[str] = []
        severity = "warning"
        softened = None
        if receiver.tolerant and v_drv <= TOLERANT_MAX_V + 1e-9:
            severity, softened = "info", f"{receiver.ref}'s input is 5 V tolerant: {receiver.tolerant}"
        elif pair.series is not None:
            resistor, ohms = pair.series
            current = (v_drv - limit) / ohms * 1000 if ohms else None
            severity = "info"
            softened = (f"{resistor} ({ohms_text(ohms) or '?'} ohm) in series limits the clamp "
                        "current" + (f" to about {current:.2g} mA" if current else "")
                        + ": confirm the input's injection-current limit")
        if receiver.chip.fixed and severity == "warning":
            severity = "info"
            softened = f"{receiver.ref} is {receiver.chip.fixed}; its pin may be rated to its input supply"
        if pair.exposed == "od_output" and severity == "warning":
            severity = "info"
            softened = ("an open-drain output: many are clamped to their own supply, some are "
                        "rated higher (fail-safe or 5 V tolerant open-drain outputs)")
        severity = _cap(severity, notes, None if pulled else driver, receiver, certain)
        what = {"output": ", an output (back-driven through its clamp while it is tri-stated "
                          "or drives high),",
                "od_output": ", an open-drain output,"}.get(pair.exposed or "", "")
        issue = (f"{source} into {receiver.label()}{what} whose I/O supply is {_v(v_rcv)} V "
                 f"({receiver.rail.net}): above the usual absolute maximum of VDD + 0.3 V "
                 f"({_v(limit)} V).")
        if softened:
            issue += f" Softened: {softened}."
        if notes:
            issue += " Info only: " + "; ".join(notes) + "."
        issue += (" Confirm the receiver pin's absolute maximum (and whether it is 5 V "
                  "tolerant) in its datasheet.")
        out.append({"check": check, "severity": severity, "issue": issue,
                    "kind": "above_abs_max", "softened": softened})
        return out
    if receiver.chip.cls == "power" or pair.idle or pair.exposed:
        # A regulator's EN or a supervisor's input: thresholds fixed by the part. A pull-up
        # on a push-pull line: the drivers set the high level. An output reads nothing.
        return out
    # Too low: below the receiver's VIH estimate.
    if receiver.chip.ttl:
        vih, what = 2.0, "a TTL-compatible VIH of about 2.0 V"
    else:
        vih, what = VIH_RATIO * v_rcv, f"the usual CMOS VIH of {VIH_RATIO:.0%} of its supply"
    if v_drv < vih - 1e-9 and abs(v_drv - v_rcv) > SAME_V:
        check = "open_drain_pullup_mismatch" if open_drain else "driver_low_for_receiver"
        notes = []
        if open_drain:
            severity = "info"  # a pull-up rail below VIH: the receiver may still read it
        elif receiver.chip.ttl:
            severity = "warning" if v_drv < vih - 0.2 else "info"
        else:
            severity = "warning" if v_drv <= WARN_RATIO * v_rcv + 1e-9 else "info"
        if receiver.chip.fixed or v_rcv > HIGH_SUPPLY_V:
            if severity == "warning":
                notes.append(f"{receiver.ref} is " + (receiver.chip.fixed or
                                                      f"powered at {_v(v_rcv)} V: its logic "
                                                      "inputs likely have fixed thresholds"))
            severity = "info"
        severity = _cap(severity, notes, None if open_drain else driver, receiver, certain)
        issue = (f"{source} into {receiver.label()}, whose I/O supply is {_v(v_rcv)} V "
                 f"({receiver.rail.net}): {v_drv / v_rcv:.0%} of it, below {what} "
                 f"({_v(vih)} V): likely VOH < VIH.")
        if notes:
            issue += " Info only: " + "; ".join(notes) + "."
        issue += (" Confirm the driver's VOH" + (" (the pull-up rail)" if open_drain else "")
                  + " and the receiver's VIH in their datasheets.")
        out.append({"check": check, "severity": severity, "issue": issue,
                    "kind": "below_vih", "vih_estimate": round(vih, 3)})
    return out


def _signal_pairs(signal: Signal, stats: Counter) -> list[_Pair]:
    pairs: list[_Pair] = []
    by_rail: dict[str, list] = defaultdict(list)
    for pull in signal.pull_ups:
        by_rail[pull.other].append(pull)
    if signal.mode == "open_drain":
        # The pull-up sets the high level every pin on the line sees; outputs are judged
        # for overvoltage only.
        for rail, pulls in by_rail.items():
            stats["open_drain_levels"] += 1
            for end, exposed in _pulled_pins(signal):
                pairs.append(_Pair(end, end, None, rail, pull_up=pulls, exposed=exposed))
        return pairs
    if signal.mode == "open_drain_unpulled":
        stats["open_drain_without_pull_up"] += 1
    drivers = [end for end in signal.ends if _drives(end)]
    receivers = [end for end in signal.ends if _receives(end)]
    for driver, receiver in _push_pull_pairs(drivers, receivers):
        pairs.append(_Pair(driver, receiver, driver.rail.volts, driver.rail.net or "?"))
    # A pull-up on a push-pull line sets the level whenever no driver holds it (tri-state,
    # reset), and back-drives an output while it is high: judged for overvoltage only.
    for rail, pulls in by_rail.items():
        for end, exposed in _pulled_pins(signal):
            if end.direction not in ("od", "od_out"):
                pairs.append(_Pair(end, end, None, rail, pull_up=pulls, idle=True,
                                   exposed=exposed))
    return pairs


def _series_pairs(signal: Signal, other: Signal, resistor: str, ohms: float | None
                  ) -> list[_Pair]:
    """Pairs across one series resistor of 1 kohm or more: the drivers on one side and the
    receivers on the other; a pull-down behind the resistor divides the level."""
    out = []
    for near, far in ((signal, other), (other, signal)):
        drivers = [end for end in near.ends if end.direction == "out" or (
            end.direction in ("bidir", "unknown") and not any(
                item.direction == "out" for item in near.ends))]
        receivers = [end for end in far.ends if _receives(end) and end.direction != "od"]
        if not drivers or not receivers or far.pull_ups:
            continue
        downs = [pull for pull in far.pull_downs if pull.ohms]
        if len(downs) != len(far.pull_downs):
            continue  # a pull-down of unknown value: the level is not known
        for driver in drivers:
            if driver.rail.volts is None:
                level = None
                basis = driver.rail.net or "?"
            elif downs and ohms:
                bottom = 1 / sum(1 / pull.ohms for pull in downs)
                level = driver.rail.volts * bottom / (ohms + bottom)
                basis = (f"{driver.rail.net} {_v(driver.rail.volts)} V divided by {resistor} "
                         f"and {', '.join(pull.resistor for pull in downs)} to "
                         f"{_v(level)} V")
            else:
                level = driver.rail.volts
                basis = driver.rail.net or "?"
            for receiver in receivers:
                if receiver.ref == driver.ref:
                    continue
                pair = _Pair(driver, receiver, level, basis,
                             series=None if downs else (resistor, ohms))
                out.append(pair)
    return out


def _resolve_open_drain(pair: _Pair, voltages: dict[str, Volt]) -> _Pair | None:
    rail = pair.level_basis
    volt = voltages.get(rail)
    if volt is None:
        return None
    resistors = ", ".join(pull.resistor for pull in pair.pull_up)
    return _Pair(pair.driver, pair.receiver, volt.volts, f"{rail} through {resistors}",
                 pull_up=pair.pull_up, idle=pair.idle, exposed=pair.exposed)


# ---------- the check ----------

def _finding_key(row: dict) -> tuple:
    return (row["check"], row["severity"], row["driver_key"], row["receiver"]["designator"],
            row["receiver"].get("rail"), row["receiver"].get("pin_role"), row.get("softened"),
            row.get("kind"))


def _group(rows: list[dict]) -> list[dict]:
    grouped: dict[tuple, dict] = {}
    for row in rows:
        key = _finding_key(row)
        if key in grouped:
            entry = grouped[key]
            for net in row["nets"]:
                if net not in entry["nets"]:
                    entry["nets"].append(net)
            entry["pins"] += [pin for pin in row["pins"] if pin not in entry["pins"]]
            entry["signal_keys"] |= row["signal_keys"]
            if "pull_up" in row:
                entry["pull_up"]["designators"] = sorted(
                    {*entry["pull_up"]["designators"], *row["pull_up"]["designators"]},
                    key=natural_key)
        else:
            grouped[key] = {**row, "nets": list(row["nets"]), "pins": list(row["pins"]),
                            "signal_keys": set(row["signal_keys"])}
            if "pull_up" in row:
                grouped[key]["pull_up"] = dict(row["pull_up"])
    out = []
    for row in grouped.values():
        row.pop("driver_key", None)
        if row.get("softened") is None:
            row.pop("softened", None)
        if len(row["pins"]) > PINS_LISTED:
            row["pins_total"] = len(row["pins"])
            row["pins"] = row["pins"][:PINS_LISTED]
        row["nets"] = sorted(row["nets"], key=natural_key)
        signals = len(row.pop("signal_keys"))
        if signals > 1:
            row["count"] = signals  # signals (or lines across a series resistor) grouped
        out.append(row)
    return out


def _translator_rows(chips: dict[str, Chip], signals: list[Signal]) -> list[dict]:
    on: Counter = Counter()
    for signal in signals:
        if signal.excluded in (None, "one_ic"):
            for ref in signal.refs():
                on[ref] += 1
    rows = []
    for ref, chip in chips.items():
        if chip.cls not in ("translator", "isolator"):
            continue
        row: dict = {"check": "translator_ok", "designator": ref, "part_class": chip.cls}
        if chip.sides:
            row["sides"] = {side: {"rail": items[0].net, "supply_pins": _names(items)}
                            for side, items in sorted(chip.sides.items())}
            row["issue"] = (f"{ref} ({chip.cls}) bridges "
                            + " and ".join(f"{items[0].net}" for _, items in
                                           sorted(chip.sides.items()))
                            + ": its pins are judged against their own side's supply.")
        elif not chip.supplies:
            row["issue"] = (f"{ref} reads as a {chip.cls} but none of its pins is named as a "
                            "supply: its sides are not known, so its pins are not judged. "
                            "Check which rail each side sits on by hand.")
        else:
            row["issue"] = (f"{ref} reads as a {chip.cls} but its side supplies are not on two "
                            "different nets (or not named VCCA/VCCB, VCC1/VCC2, VDD1/VDD2, "
                            "VL/VCC): its pins are judged against its default rail.")
        row["signals"] = on.get(ref, 0)
        rows.append(row)
    return rows


def _signal_named(net: str) -> bool:
    """A net named like a signal (PA1, SPI_MOSI), not like a rail: a supply pin on it is a
    library or netlist slip, not a rail to ask the user about."""
    if net_kind(net)["power"]:
        return False
    found = classify(net)
    return port_like(net) or bool(found and found[0] in SIGNAL_BUS_FUNCTIONS and found[1])


def _unknown_row(tree, unknown: dict[str, set[str]], parts: dict[str, set[str]],
                 no_supply: dict[str, set[str]]) -> dict:
    """The unknown_rail row: the rails of unknown voltage to ask about, most signals first,
    each with the rails joined to it through 0 ohm links, ferrites, inductors or fuses
    (rails= for it sets them too); supply pins on nets named like signals; parts with no
    supply pin read by name."""
    switch_nodes = {net for conv in tree.converters.values() for net in conv.switch_nodes}
    parent: dict[str, str] = {}

    def root(net: str) -> str:
        while parent.get(net, net) != net:
            net = parent[net]
        return net

    for edge in tree.edges:
        if edge.kind in SAME_VOLTAGE_EDGES and not {edge.src, edge.dst} & switch_nodes:
            parent[root(edge.src)] = root(edge.dst)
    groups: dict[str, list[str]] = defaultdict(list)
    signal_named = []
    for net in unknown:
        if _signal_named(net):
            signal_named.append(net)
        else:
            groups[root(net)].append(net)
    rails = []
    for members in groups.values():
        found = set().union(*(unknown[net] for net in members))
        lead = min(members, key=lambda net: (auto_net(net), -len(unknown[net]),
                                             natural_key(net)))
        row = {"rail": lead, "signals": len(found),
               "example_nets": sorted(found, key=natural_key)[:3]}
        if len(members) > 1:
            row["joined_rails"] = sorted(set(members) - {lead}, key=natural_key)
        rails.append(row)
    rails.sort(key=lambda row: (-row["signals"], natural_key(row["rail"])))
    out: dict = {"check": "unknown_rail", "severity": "info", "rails": rails[:RAILS_LISTED],
                 "rails_total": len(rails), "issue": ""}
    if rails:
        out["issue"] = (f"{len(rails)} I/O or pull-up rail(s) on signals between ICs have no "
                        "known voltage (neither rails=, the net name nor the power tree gives "
                        "one): ask the user and pass rails='NAME=volts' to judge their "
                        "signals.")
        if any("joined_rails" in row for row in rails):
            out["issue"] += (" joined_rails sit behind a 0 ohm link, ferrite, inductor or "
                             "fuse and take the listed rail's voltage.")
    if signal_named:
        out["supply_pins_on_signal_nets"] = [
            {"net": net, "parts": sorted(parts[net], key=natural_key)[:6],
             "signals": len(unknown[net])} for net in sorted(signal_named, key=natural_key)]
        out["issue"] += (f" {len(signal_named)} net(s) named like a signal carry a part's "
                         "supply pin: check the part's pin mapping in its library or the "
                         "netlist rather than giving the net a voltage.")
    if no_supply:
        out["parts_without_supply_pin"] = sorted(no_supply, key=natural_key)[:20]
        out["issue"] += (f" {len(no_supply)} part(s) on such signals have no supply pin read "
                         "by name: give their rail in rails= by its net, or check them by "
                         "hand.")
    out["issue"] = out["issue"].strip()
    return out


def prepare_logic_levels(
    netlist: dict,
    plan=None,
    project_params: dict | None = None,
    checks: list[str] | None = None,
    parts: list[str] | None = None,
    nets: list[str] | None = None,
    rails: dict[str, float] | None = None,
    vref: dict[str, float] | float | None = None,
) -> dict:
    """Read every IC, signal and voltage once and judge the pairs; render_logic_levels
    pages the result. An {"error": ...} dict for an unknown check, part or net."""
    requested = [check.strip().lower() for check in checks or [] if check.strip()]
    if invalid := [check for check in requested if check not in CHECKS]:
        return {"error": "invalid_checks",
                "message": f"Unknown checks {invalid}; use {list(CHECKS)}."}
    tree = build_tree(netlist, plan, project_params)
    notes: list[str] = []
    wanted_parts, unknown_parts = resolve_names(parts, tree.design.part_pins)
    if parts and not wanted_parts:
        return {"error": "part_not_found",
                "message": f"No part {unknown_parts} in the compiled netlist; use "
                           "search_components to find the designator."}
    wanted_nets, unknown_nets = resolve_names(nets, tree.design.nets)
    if nets and not wanted_nets:
        return {"error": "net_not_found",
                "message": f"No net {unknown_nets} in the compiled netlist; use query_net to "
                           "find the real name."}
    if unknown_parts or unknown_nets:
        notes.append(f"Not found: {', '.join([*unknown_parts, *unknown_nets])}.")
    data = tree_data(tree, vref)
    checked, _, _ = read_parts(tree, netlist, plan)
    voltages, unknown_rails = net_voltages(tree, data, rails, checked)
    if unknown_rails:
        notes.append(f"rails names no net {unknown_rails}: ignored (use query_net to find "
                     "the real name).")
    stats: Counter = Counter()
    chips = build_chips(tree, voltages)
    signals, of_net = build_signals(tree, chips, voltages, stats)
    rows: list[dict] = []
    unknown: dict[str, set[str]] = defaultdict(set)  # rail -> signals
    unknown_parts: dict[str, set[str]] = defaultdict(set)  # rail -> parts (or pull-ups)
    no_supply: dict[str, set[str]] = defaultdict(set)  # part -> signals
    judged_signals: set = set()
    between_rails: set = set()

    def judge(pair: _Pair, signal_nets: list[str], key, connectors: list[str]) -> None:
        missing = False  # every rail of unknown voltage on the pair is listed, not the first
        if pair.pull_up is not None:
            resolved = _resolve_open_drain(pair, voltages)
            if resolved is None:
                unknown[pair.level_basis].add(signal_nets[0])
                unknown_parts[pair.level_basis].update(pull.resistor for pull in pair.pull_up)
                missing = True
            else:
                pair = resolved
        for end in ([pair.receiver] if pair.pull_up is not None else
                    [pair.driver, pair.receiver]):
            if end.rail.net is None:
                no_supply[end.ref].add(signal_nets[0])
                missing = True
            elif end.rail.volts is None:
                unknown[end.rail.net].add(signal_nets[0])
                unknown_parts[end.rail.net].add(end.ref)
                missing = True
        if missing or pair.level is None:
            return
        judged_signals.add(key)
        if not (pair.idle or pair.exposed) and abs(pair.level - pair.receiver.rail.volts) > SAME_V:
            between_rails.add(key)
        for result in _judge(pair):
            driver = pair.driver
            row: dict = {"check": result["check"], "severity": result["severity"]}
            if pair.pull_up is not None:
                row["pull_up"] = {"designators": sorted({pull.resistor for pull in pair.pull_up},
                                                        key=natural_key),
                                  "rail": pair.pull_up[0].other, "volts": round(pair.level, 3)}
                row["driver_key"] = ("pull_up", pair.pull_up[0].other)
            else:
                row["driver"] = {**_end_row(driver), "high_level": round(pair.level, 3)}
                if pair.level_basis != driver.rail.net:
                    row["driver"]["level_basis"] = pair.level_basis
                row["driver_key"] = (driver.ref, driver.rail.net, round(pair.level, 3))
            row["receiver"] = _end_row(pair.receiver)
            if pair.exposed:
                row["receiver"]["pin_role"] = {"output": "push-pull output",
                                               "od_output": "open-drain output"}[pair.exposed]
            if pair.series:
                row["series"] = {"designator": pair.series[0],
                                 "value": ohms_text(pair.series[1])}
            row["nets"] = list(signal_nets)
            row["signal_keys"] = {key}
            arrow = (f"{', '.join(row['pull_up']['designators'])} to {pair.pull_up[0].other}"
                     if pair.pull_up is not None else driver.label())
            row["pins"] = [f"{arrow} -> {pair.receiver.label()}"]
            row["direction_basis"] = (
                f"{pair.receiver.label()}: {pair.receiver.basis}" if pair.pull_up is not None
                else f"{driver.label()}: {driver.basis}; {pair.receiver.label()}: "
                     f"{pair.receiver.basis}")
            for name in ("kind", "softened", "vih_estimate"):
                if result.get(name) is not None:
                    row[name] = result[name]
            if connectors:
                row["offboard"] = connectors[:4]  # the line also leaves the board here
            row["issue"] = result["issue"]
            rows.append(row)

    excluded: Counter = Counter()
    for index, signal in enumerate(signals):
        if signal.excluded:
            excluded[signal.excluded] += 1
            continue
        stats["signals_checked"] += 1
        stats[f"mode_{signal.mode}"] += 1
        for pair in _signal_pairs(signal, stats):
            judge(pair, signal.nets, index, signal.connectors)
    # Across one series resistor of 1 kohm or more: counted on their own (series_resistor_
    # links), not as signals.
    seen_cross: set[tuple[int, int]] = set()
    index_of = {id(signal): index for index, signal in enumerate(signals)}
    for index, signal in enumerate(signals):
        if signal.excluded not in (None, "one_ic") or signal.mode != "push_pull" or                 not signal.ends:
            continue
        for net in signal.nets:
            for resistor, other_net, ohms in tree.design.series.get(net, ()):
                if ohms is None or ohms < LINK_OHMS:
                    continue  # a link (one signal) or a resistor of unknown value
                other = of_net.get(other_net)
                if other is None or other is signal or other.excluded not in (None, "one_ic")                         or other.mode != "push_pull" or not other.ends:
                    continue
                key = tuple(sorted((index, index_of[id(other)])))
                if key in seen_cross:
                    continue
                seen_cross.add(key)
                pairs = _series_pairs(signal, other, resistor, ohms)
                if not pairs:
                    continue
                stats["series_resistor_links"] += 1
                before = len(judged_signals)
                for pair in pairs:
                    judge(pair, [*signal.nets[:1], *other.nets[:1]], ("series", *key),
                          [*signal.connectors, *other.connectors])
                if len(judged_signals) > before:
                    stats["series_resistor_links_judged"] += 1
                for near, far in ((signal, other), (other, signal)):
                    near.crossings.append({"through": resistor, "value": ohms_text(ohms),
                                           "to_nets": far.nets[:3],
                                           "judged": len(judged_signals) > before})
    findings = _group(rows)
    wanted = set(requested or CHECKS)
    translators = _translator_rows(chips, signals) if "translator_ok" in wanted else []
    if unknown or no_supply:
        findings.append(_unknown_row(tree, unknown, unknown_parts, no_supply))
    unknown_rails = [row for row in findings if row["check"] == "unknown_rail"]
    keep_parts = wanted_parts
    if wanted_parts is not None or wanted_nets is not None:
        def kept(row: dict) -> bool:
            refs = {row.get("driver", {}).get("designator"),
                    row.get("receiver", {}).get("designator")}
            return bool((wanted_parts and refs & wanted_parts) or (
                wanted_nets and set(row.get("nets", ())) & wanted_nets))
        findings = [row for row in findings if row["check"] == "unknown_rail" or kept(row)]
        translators = [row for row in translators
                       if not wanted_parts or row["designator"] in wanted_parts]
    findings = [row for row in findings if row["check"] in wanted]
    findings.sort(key=lambda row: (_SEVERITY_RANK[row["severity"]], _CHECK_RANK[row["check"]],
                                   natural_key((row.get("driver") or row.get("receiver")
                                                or {}).get("designator") or "~")))
    summary = _summary(chips, excluded, stats, judged_signals, between_rails)
    part_rows = pin_rows = None
    if keep_parts:
        part_rows, pin_rows = [], []
        for ref in sorted(keep_parts, key=natural_key):
            if ref in chips:
                row, pins = _part_row(chips[ref], signals, voltages)
                part_rows.append(row)
                pin_rows += pins
        for ref in sorted(keep_parts - set(chips), key=natural_key):
            notes.append(f"{ref} is not a fitted IC with supply pins in variant {tree.variant}: "
                         "not checked as one.")
    signal_rows = None
    if wanted_nets:
        chosen = []
        for net in sorted(wanted_nets, key=natural_key):
            signal = of_net.get(net)
            if signal is not None and signal not in chosen:
                chosen.append(signal)
            elif signal is None:
                notes.append(f"{net} is a rail or a ground: not a signal.")
        signal_rows = [_signal_row(signal) for signal in chosen]
    return {"variant": tree.variant, "summary": summary, "findings": findings,
            "translators": translators, "wanted": wanted, "notes": notes,
            "parts": part_rows, "part_pins": pin_rows, "signals": signal_rows,
            "unknown_rails": unknown_rails}


def _summary(chips: dict[str, Chip], excluded: Counter, stats: Counter, judged: set,
             between: set) -> dict:
    """Counts: judged and between_different_levels count signals (a driver's level that
    differs from a receiver's rail, pull-ups on push-pull lines left out); lines across a
    series resistor of 1 kohm or more are counted apart (series_resistor_links)."""
    by_basis: Counter = Counter()
    by_confidence: Counter = Counter()
    classes: Counter = Counter()
    known = 0
    for chip in chips.values():
        classes[chip.cls] += 1
        by_basis[chip.basis] += 1
        if chip.default:
            by_confidence[chip.default.confidence] += 1
            if chip.default.volts is not None:
                known += 1
    return {
        "ics": {"total": len(chips),
                "with_io_rail": sum(1 for chip in chips.values() if chip.basis != "none"),
                "rail_voltage_known": known,
                "by_basis": dict(sorted(by_basis.items())),
                "by_confidence": dict(sorted(by_confidence.items())),
                "by_class": dict(sorted(classes.items()))},
        "signals": {"checked": stats["signals_checked"],
                    "judged": sum(1 for key in judged if isinstance(key, int)),
                    "between_different_levels": sum(1 for key in between
                                                    if isinstance(key, int)),
                    "open_drain": stats["mode_open_drain"],
                    "open_drain_without_pull_up": stats["mode_open_drain_unpulled"],
                    "series_resistor_links": stats["series_resistor_links"],
                    "series_resistor_links_judged": stats["series_resistor_links_judged"],
                    "excluded": dict(sorted(excluded.items()))},
    }


def _part_row(chip: Chip, signals: list[Signal], voltages: dict[str, Volt]
              ) -> tuple[dict, list[dict]]:
    """A named part's supplies, banks and sides, and one row per pin on a signal (paged
    apart, as part_pins)."""
    by_net: dict[str, list[Supply]] = defaultdict(list)
    for supply in chip.supplies:
        by_net[supply.net].append(supply)
    row: dict = {"designator": chip.ref, "part_class": chip.cls,
                 "supplies": [{"net": net, "supply_pins": _names(items),
                               "class": items[0].cls,
                               "volts": voltages[net].volts if net in voltages else None}
                              for net, items in by_net.items()],
                 "default_rail": chip.default.describe() if chip.default else None}
    if chip.banks:
        row["banks"] = {bank: items[0].net for bank, items in sorted(chip.banks.items())}
    if chip.sides:
        row["sides"] = {side: items[0].net for side, items in sorted(chip.sides.items())}
    if chip.tolerant:
        row["five_volt_tolerant"] = chip.tolerant
    if chip.fixed:
        row["fixed_thresholds"] = chip.fixed
    pins = [end for signal in signals if signal.excluded in (None, "one_ic")
            for end in signal.ends if end.ref == chip.ref]
    pins.sort(key=lambda end: natural_key(end.pin))
    row["pins_on_signals"] = len(pins)
    return row, [{"designator": chip.ref, "pin": end.label(), "net": end.net,
                  "direction": end.direction, "direction_basis": end.basis,
                  "rail": end.rail.net, "volts": end.rail.volts,
                  "rail_basis": end.rail.detail, "confidence": end.rail.confidence}
                 for end in pins]


def _signal_row(signal: Signal) -> dict:
    row: dict = {"nets": signal.nets, "mode": signal.mode}
    if signal.excluded:
        row["excluded"] = signal.excluded + (f" ({signal.why})" if signal.why else "")
        if signal.excluded == "one_ic" and any(item["judged"] for item in signal.crossings):
            row["excluded"] += ": judged across its series resistor instead (series_crossings)"
    if signal.crossings:
        row["series_crossings"] = signal.crossings
    row["ends"] = [{"pin": end.label(), "direction": end.direction, "certain": end.certain,
                    "direction_basis": end.basis, **end.rail.describe()}
                   for end in signal.ends]
    if signal.pull_ups:
        row["pull_ups"] = [pull.describe() for pull in signal.pull_ups]
    if signal.connectors:
        row["connectors"] = signal.connectors
    return row


def render_logic_levels(prepared: dict, offset: int = 0, limit: int = PAGE_LIMIT) -> dict:
    """One page of a prepared check: summary first, then findings (and the named parts,
    their pins and the named signals) paged together on one offset."""
    findings = prepared["findings"]
    by_check: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in findings:
        by_check[row["check"]][row["severity"]] += 1
    offset = max(0, int(offset or 0))
    page = slice(offset, offset + limit)
    summary = dict(prepared["summary"])
    summary["candidates"] = {check: dict(levels) for check, levels in by_check.items()}
    translators = prepared["translators"]
    summary["translators_handled"] = sum(1 for row in translators if "sides" in row)
    if len(translators) > summary["translators_handled"]:
        summary["translators_sides_unknown"] = len(translators) - summary["translators_handled"]
    unknown = prepared["unknown_rails"]
    # The rails to ask the user about, most signals first (the unknown_rail row has them all).
    summary["unknown_rails"] = unknown[0]["rails"] if unknown else []
    for name in ("supply_pins_on_signal_nets", "parts_without_supply_pin"):
        if unknown and unknown[0].get(name):
            summary[name] = unknown[0][name]
    result: dict = {
        "variant": prepared["variant"],
        "summary": summary,
        "severity_counts": {level: sum(1 for row in findings if row["severity"] == level)
                            for level in ("warning", "info")},
        "checks_run": [check for check in CHECKS if check in prepared["wanted"]],
        "heuristic": HEURISTIC,
        "guidance": GUIDANCE,
        "findings": findings[page],
        "total": len(findings),
    }
    lengths = [len(findings)]
    if translators:
        result["translators"] = translators[:TRANSLATORS_LISTED]
        if len(translators) > TRANSLATORS_LISTED:
            result["translators_total"] = len(translators)
    for name in ("parts", "part_pins", "signals"):
        rows = prepared.get(name)
        if rows is not None:
            result[f"{name}_total"] = len(rows)
            result[name] = rows[page]
            lengths.append(len(rows))
    result["offset"] = offset
    result["has_more"] = offset + limit < max(lengths)
    if result["has_more"]:
        result["next_offset"] = offset + limit
    notes = list(prepared["notes"])
    notes.append(
        "Candidates only: VOH, VIH, 5 V tolerance and absolute maximum ratings come from the "
        "datasheets, which override this. unknown_rails lists I/O and pull-up rails of "
        "unknown voltage: ask the user and pass rails='NAME=volts'.")
    if prepared.get("parts") is None and prepared.get("signals") is None:
        notes.append("Name parts (part=) for their supplies and each pin's I/O rail, or nets "
                     "(net=) for each signal's pins, directions and rails.")
    result["notes"] = notes
    return result


def run_logic_level_check(
    netlist: dict,
    plan=None,
    project_params: dict | None = None,
    checks: list[str] | None = None,
    parts: list[str] | None = None,
    nets: list[str] | None = None,
    rails: dict[str, float] | None = None,
    vref: dict[str, float] | float | None = None,
    offset: int = 0,
    limit: int = PAGE_LIMIT,
) -> dict:
    """The logic-level check in one call: prepare_logic_levels and one page of
    render_logic_levels. Heuristic: see the module notes."""
    prepared = prepare_logic_levels(netlist, plan, project_params, checks, parts, nets,
                                    rails, vref)
    if "error" in prepared:
        return prepared
    return render_logic_levels(prepared, offset, limit)
