"""PCB design-rule values, scope evaluation and rule resolution.

Pure logic over parsed PcbData records: no SQLite, no MCP. PcbIndex builds a
RuleContext and asks which rules apply to a net or differential pair.

Scope expressions are Altium query language (e.g. "InNetClass('HS') And Not
IsVia"). They are evaluated with three-valued logic for one target (a net or
a pair): True (applies to the target's objects), False (cannot apply), or
Unknown (applies only to some of its objects, e.g. its vias or the part of it
inside a room). Unknown results carry the sub-expressions that decide them.
"""

import re
from dataclasses import dataclass, field

try:
    from parsers.pcb_doc import ALTIUM_UNITS_PER_MIL, PcbRule
except ImportError:
    from server.parsers.pcb_doc import ALTIUM_UNITS_PER_MIL, PcbRule

# ---------- query language ----------

_TOKEN_RE = re.compile(
    r"\s*(?:"
    r"(?P<string>'(?:[^']|'')*')"
    r"|(?P<number>-?\d+(?:\.\d+)?(?![A-Za-z_]))"
    r"|(?P<op>&&|\|\||<>|<=|>=|[()=<>,])"
    r"|(?P<ident>[A-Za-z0-9_][A-Za-z0-9_.]*)"
    r")"
)


class ScopeSyntaxError(ValueError):
    pass


@dataclass
class _Token:
    kind: str  # string | number | op | ident | end
    value: str
    start: int
    end: int


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    position = 0
    while position < len(text):
        if text[position:].strip() == "":
            break
        match = _TOKEN_RE.match(text, position)
        if not match or match.end() == position:
            raise ScopeSyntaxError(
                f"unexpected character {text[position:].strip()[:1]!r}"
            )
        kind = match.lastgroup
        value = match.group(kind)
        if kind == "string":
            value = value[1:-1].replace("''", "'")
        tokens.append(_Token(kind, value, match.start(kind), match.end()))
        position = match.end()
    tokens.append(_Token("end", "", len(text), len(text)))
    return tokens


# AST nodes are tuples whose last element is the source text of the node:
#   ("const", bool, text) | ("call", name_lower, args, text)
#   ("cmp", prop_lower, op, value, text) | ("not", node, text)
#   ("and" | "or" | "xor", left, right, text)

_COMPARISON_OPS = {"=", "<>", "<", ">", "<=", ">=", "like"}


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.tokens = _tokenize(text)
        self.position = 0

    def peek(self) -> _Token:
        return self.tokens[self.position]

    def take(self) -> _Token:
        token = self.tokens[self.position]
        self.position += 1
        return token

    def word(self) -> str:
        token = self.peek()
        return token.value.lower() if token.kind == "ident" else ""

    def source(self, start: int) -> str:
        end = self.tokens[self.position - 1].end
        return self.text[start:end].strip()

    def parse(self):
        node = self.parse_loose_or()
        if self.peek().kind != "end":
            raise ScopeSyntaxError(f"unexpected {self.peek().value!r}")
        return node

    # Precedence, loosest first: || then && then Or/Xor then And then Not.
    def parse_loose_or(self):
        start = self.peek().start
        node = self.parse_loose_and()
        while self.peek().kind == "op" and self.peek().value == "||":
            self.take()
            node = ("or", node, self.parse_loose_and(), self.source(start))
        return node

    def parse_loose_and(self):
        start = self.peek().start
        node = self.parse_or()
        while self.peek().kind == "op" and self.peek().value == "&&":
            self.take()
            node = ("and", node, self.parse_or(), self.source(start))
        return node

    def parse_or(self):
        start = self.peek().start
        node = self.parse_and()
        while self.word() in ("or", "xor"):
            operator = self.take().value.lower()
            node = (operator, node, self.parse_and(), self.source(start))
        return node

    def parse_and(self):
        start = self.peek().start
        node = self.parse_not()
        while self.word() == "and":
            self.take()
            node = ("and", node, self.parse_not(), self.source(start))
        return node

    def parse_not(self):
        start = self.peek().start
        if self.word() == "not":
            self.take()
            operand = self.parse_not()
            return ("not", operand, self.source(start))
        return self.parse_comparison()

    def parse_comparison(self):
        start = self.peek().start
        node = self.parse_primary()
        token = self.peek()
        operator = (
            token.value
            if token.kind == "op" and token.value in _COMPARISON_OPS
            else ("like" if self.word() == "like" else None)
        )
        if operator is None:
            return node
        self.take()
        value_token = self.take()
        if value_token.kind not in ("string", "number", "ident"):
            raise ScopeSyntaxError("comparison needs a value")
        prop = node[1] if node[0] == "call" and not node[2] else "?"
        return ("cmp", prop, operator, value_token.value, self.source(start))

    def parse_primary(self):
        start = self.peek().start
        token = self.take()
        if token.kind == "op" and token.value == "(":
            node = self.parse_loose_or()
            closing = self.take()
            if closing.kind != "op" or closing.value != ")":
                raise ScopeSyntaxError("missing ')'")
            return node
        if token.kind != "ident":
            raise ScopeSyntaxError(f"unexpected {token.value!r}")
        name = token.value.lower()
        if name in ("true", "false"):
            return ("const", name == "true", self.source(start))
        args: list[str] = []
        if self.peek().kind == "op" and self.peek().value == "(":
            self.take()
            if not (self.peek().kind == "op" and self.peek().value == ")"):
                while True:
                    argument = self.take()
                    if argument.kind not in ("string", "number", "ident"):
                        raise ScopeSyntaxError("bad argument")
                    args.append(argument.value)
                    separator = self.take()
                    if separator.kind == "op" and separator.value == ")":
                        break
                    if separator.kind != "op" or separator.value != ",":
                        raise ScopeSyntaxError("expected ',' or ')'")
            else:
                self.take()
        return ("call", name, args, self.source(start))


_PARSE_CACHE: dict[str, tuple] = {}


def parse_scope(text: str):
    """Parse a scope expression into an AST; raises ScopeSyntaxError."""
    if text not in _PARSE_CACHE:
        _PARSE_CACHE[text] = _Parser(text).parse()
    return _PARSE_CACHE[text]


def _wildcard_matches(pattern: str, value: str) -> bool:
    if "*" not in pattern and "?" not in pattern:
        return pattern.lower() == value.lower()
    regex = "".join(
        ".*" if char == "*" else "." if char == "?" else re.escape(char)
        for char in pattern
    )
    return re.fullmatch(regex, value, re.IGNORECASE) is not None


def _any_matches(patterns: list[str], values) -> bool:
    return any(
        _wildcard_matches(pattern, value)
        for pattern in patterns
        for value in values
    )


# ---------- evaluation ----------


@dataclass
class RuleContext:
    """Lookup tables the evaluator needs, built once per PCB index."""

    net_classes: dict[str, set[str]] = field(default_factory=dict)
    pair_classes: dict[str, set[str]] = field(default_factory=dict)
    component_classes: dict[str, set[str]] = field(default_factory=dict)
    pad_classes: dict[str, set[str]] = field(default_factory=dict)
    superclasses: dict[str, set[str]] = field(default_factory=dict)
    pairs: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    net_names: dict[str, str] = field(default_factory=dict)
    rooms: set[str] = field(default_factory=set)


@dataclass
class RuleTarget:
    """A net or differential pair, with the facts its scopes are tested on.

    Names are lower-case. components/footprints/pads describe the pads the
    target's nets connect to, so component-scoped rules can be ruled out.
    """

    kind: str  # "net" | "diff_pair"
    name: str
    nets: set[str]
    pairs: set[str]
    components: set[str] = field(default_factory=set)
    footprints: set[str] = field(default_factory=set)
    pads: set[str] = field(default_factory=set)


# Result: (value, conditions) with value True / False / None (unknown).
_TRUE = (True, ())
_FALSE = (False, ())


def _unknown(node) -> tuple:
    return (None, (node[-1],))


def _class_member(
    ctx: RuleContext,
    classes: dict[str, set[str]],
    kind: str,
    class_patterns: list[str],
    names: set[str],
) -> bool:
    """Is any of names in a class matching one of class_patterns?"""
    if not names:
        return False
    for pattern in class_patterns:
        for class_name in ctx.superclasses.get(kind, set()):
            if _wildcard_matches(pattern, class_name):
                return True
        for class_name, members in classes.items():
            if _wildcard_matches(pattern, class_name) and members & names:
                return True
    return False


_COMPONENT_ONLY = {
    "isbga", "islcc", "issoic", "isthrucomponent", "issmtcomponent",
    "iscomponent", "iscomment", "isdesignator", "istext", "isfree",
    "iskeepout", "iscoordinate", "isdimension", "isroom",
}
_POLYGON_ONLY = {
    "inpoly", "inpolygon", "ispoly", "ispolygon", "innamedpolygon",
    "isnamedpolygon", "inpolygonclass", "issplitplane",
}
_OBJECT_TYPES = {
    "isvia", "ispad", "istrack", "isarc", "isfill", "isregion",
    "isthrupin", "issmtpin", "isthrupad", "issmtpad",
}
_LAYER_PREDICATES = {
    "onlayer", "ontoplayer", "onbottomlayer", "onmultilayer", "onmid",
    "onplane", "onsignal", "oncopper", "onoutside", "oninside",
}
_REGION_PREDICATES = {
    "withinroom", "touchesroom", "inroom", "inregion", "inregionabsolute",
    "inregionrelative", "touchesregion",
}


def _eval_call(node, target: RuleTarget, ctx: RuleContext) -> tuple:
    name, args = node[1], node[2]
    is_net = target.kind == "net"
    if name == "all":
        return _TRUE
    if name == "innet":
        if is_net:
            return _TRUE if _any_matches(args, target.nets) else _FALSE
        # A pair's member nets match InNet, but the pair object does not.
        return _unknown(node) if _any_matches(args, target.nets) else _FALSE
    if name == "innetclass":
        member = _class_member(ctx, ctx.net_classes, "net", args, target.nets)
        if not member:
            return _FALSE
        return _TRUE if is_net else _unknown(node)
    if name == "indifferentialpair":
        return _TRUE if _any_matches(args, target.pairs) else _FALSE
    if name == "indifferentialpairclass":
        member = _class_member(
            ctx, ctx.pair_classes, "diff_pair", args, target.pairs
        )
        return _TRUE if member else _FALSE
    if name == "inanynet":
        return _TRUE if is_net else _unknown(node)
    if name == "inanydifferentialpair":
        return _TRUE if target.pairs else _FALSE
    if name == "isnet":
        return _TRUE if is_net else _FALSE
    if name == "isdifferentialpair":
        return _FALSE if is_net else _TRUE
    if name in _COMPONENT_ONLY or name in _POLYGON_ONLY:
        # These select components, text or pour children, never a net's
        # routing or the pair object itself.
        return _FALSE
    if name in ("incomponent", "hasfootprint", "inanycomponent"):
        values = (
            target.components
            if name == "incomponent"
            else target.footprints
        )
        if name == "inanycomponent":
            return _unknown(node) if target.components else _FALSE
        return _unknown(node) if _any_matches(args, values) else _FALSE
    if name == "incomponentclass":
        member = _class_member(
            ctx, ctx.component_classes, "component", args, target.components
        )
        return _unknown(node) if member else _FALSE
    if name == "haspad":
        return _unknown(node) if _any_matches(args, target.pads) else _FALSE
    if name == "inpadclass":
        member = _class_member(
            ctx, ctx.pad_classes, "pad", args, target.pads
        )
        return _unknown(node) if member else _FALSE
    if name in _OBJECT_TYPES:
        return _unknown(node) if is_net else _FALSE
    if name in _LAYER_PREDICATES:
        # Layer names in scopes may be renamed or standard names; never
        # rule a layer out, just report the condition.
        return _unknown(node)
    if name in _REGION_PREDICATES:
        if (
            name in ("withinroom", "touchesroom", "inroom")
            and args
            and not _any_matches(args, ctx.rooms)
        ):
            return _FALSE
        return _unknown(node)
    # xSignals, from-tos and anything not modelled: decided elsewhere.
    return _unknown(node)


def _eval_cmp(node, target: RuleTarget) -> tuple:
    prop, value = node[1], str(node[3]).lower()
    if prop == "objectkind":
        if value == "net":
            return _TRUE if target.kind == "net" else _FALSE
        if value in ("differential pair", "differentialpair"):
            return _TRUE if target.kind == "diff_pair" else _FALSE
        if value in ("text", "component", "dimension", "coordinate", "room",
                     "polygon", "embedded board", "board"):
            return _FALSE
        return _unknown(node) if target.kind == "net" else _FALSE
    if prop in ("stringtext", "hide", "textheight", "textwidth"):
        return _FALSE
    if prop == "comppincount":
        return _unknown(node) if target.components else _FALSE
    return _unknown(node)


def evaluate(node, target: RuleTarget, ctx: RuleContext) -> tuple:
    """Kleene-logic evaluation: (True | False | None, conditions)."""
    tag = node[0]
    if tag == "const":
        return _TRUE if node[1] else _FALSE
    if tag == "call":
        return _eval_call(node, target, ctx)
    if tag == "cmp":
        return _eval_cmp(node, target)
    if tag == "not":
        value, conditions = evaluate(node[1], target, ctx)
        if value is None:
            return (None, (node[-1],))
        return (not value, ())
    left = evaluate(node[1], target, ctx)
    right = evaluate(node[2], target, ctx)
    if tag == "and":
        if left[0] is False or right[0] is False:
            return _FALSE
        if left[0] and right[0]:
            return _TRUE
        return (None, left[1] + right[1])
    if tag == "or":
        if left[0] or right[0]:
            return _TRUE
        if left[0] is False and right[0] is False:
            return _FALSE
        return (None, left[1] + right[1])
    # xor
    if left[0] is None or right[0] is None:
        return (None, left[1] + right[1])
    return (left[0] != right[0], ())


def evaluate_scope(text: str, target: RuleTarget, ctx: RuleContext) -> dict:
    """Evaluate one scope string; never raises."""
    try:
        node = parse_scope(text)
    except ScopeSyntaxError as error:
        return {
            "matches": "unknown",
            "conditions": [text],
            "parse_error": str(error),
        }
    value, conditions = evaluate(node, target, ctx)
    if value is None:
        return {
            "matches": "conditional",
            "conditions": list(dict.fromkeys(conditions)),
        }
    return {"matches": "yes" if value else "no"}


# Membership functions whose arguments name objects that must exist.
_REFERENCE_FUNCTIONS = {
    "innet": "nets",
    "innetclass": "net_classes",
    "indifferentialpair": "pairs",
    "indifferentialpairclass": "pair_classes",
    "incomponentclass": "component_classes",
    "inpadclass": "pad_classes",
    "withinroom": "rooms",
    "touchesroom": "rooms",
}


def stale_references(text: str, ctx: RuleContext) -> list[str]:
    """Names in a scope that match no net, class, pair or room on the board.

    Such a term matches nothing, which usually means the rule is stale (the
    class or pair was renamed or deleted) and silently not applied.
    """
    try:
        node = parse_scope(text)
    except ScopeSyntaxError:
        return []
    existing = {
        "nets": set(ctx.net_names),
        "net_classes": set(ctx.net_classes) | ctx.superclasses.get("net", set()),
        "pairs": set(ctx.pairs),
        "pair_classes": (
            set(ctx.pair_classes) | ctx.superclasses.get("diff_pair", set())
        ),
        "component_classes": (
            set(ctx.component_classes)
            | ctx.superclasses.get("component", set())
        ),
        "pad_classes": set(ctx.pad_classes) | ctx.superclasses.get("pad", set()),
        "rooms": ctx.rooms,
    }
    missing: list[str] = []

    def walk(current):
        if current[0] == "call":
            table = _REFERENCE_FUNCTIONS.get(current[1])
            if table:
                for argument in current[2]:
                    if not any(
                        _wildcard_matches(argument, name)
                        for name in existing[table]
                    ):
                        missing.append(f"{current[1]}('{argument}')")
        elif current[0] in ("not",):
            walk(current[1])
        elif current[0] in ("and", "or", "xor"):
            walk(current[1])
            walk(current[2])

    walk(node)
    return list(dict.fromkeys(missing))


# ---------- rule kinds ----------

# Binary rules test a pair of objects; scope1 and scope2 each pick one side.
BINARY_KINDS = frozenset({
    "Clearance", "ComponentClearance", "HoleToHoleClearance",
    "ShortCircuit", "Creepage", "PolygonConnect", "SilkToSilkClearance",
    "SilkToSolderMaskClearance", "MinimumSolderMaskSliver",
    "BoardOutlineClearance", "SilkToBoardRegionClearance",
})

# Kinds that concern a net's routing; get_rules_for_net reports these.
NET_RULE_KINDS = (
    "Clearance", "Width", "RoutingVias", "DiffPairsRouting", "Length",
    "MatchedLengths", "RoutingLayers", "RoutingTopology", "RoutingPriority",
    "RoutingCorners", "PlaneConnect", "PlaneClearance", "PolygonConnect",
    "HoleSize", "ShortCircuit", "UnRoutedNet", "NetAntennae", "SupplyNets",
    "Creepage", "BackDrilling",
)

# Kinds where every matching rule applies, not just the highest priority.
ALL_MATCHING_KINDS = frozenset({"MatchedLengths"})

ROOM_KIND = "RoomDefinition"

# Explicitly named values per kind: raw key -> (output name, type).
# Types: mil (length string), ohm, float, int, bool, str. Unlisted keys
# still appear, under other / other_mil.
_KIND_VALUES: dict[str, dict[str, tuple[str, str]]] = {
    "Clearance": {
        "GENERICCLEARANCE": ("clearance_mil", "mil"),
        "IGNOREPADTOPADCLEARANCEINFOOTPRINT": (
            "ignore_pad_to_pad_in_footprint", "bool",
        ),
        "GAP": ("", "skip"),
    },
    "BoardOutlineClearance": {
        "GENERICCLEARANCE": ("clearance_mil", "mil"),
        "GAP": ("", "skip"),
    },
    "Width": {
        "MINLIMIT": ("min_width_mil", "mil"),
        "PREFEREDWIDTH": ("preferred_width_mil", "mil"),
        "MAXLIMIT": ("max_width_mil", "mil"),
        "MINIMP": ("min_impedance_ohm", "ohm"),
        "FAVIMP": ("preferred_impedance_ohm", "ohm"),
        "MAXIMP": ("max_impedance_ohm", "ohm"),
    },
    "DiffPairsRouting": {
        "MINLIMIT": ("min_gap_mil", "mil"),
        "MOSTFREQGAP": ("preferred_gap_mil", "mil"),
        "MAXLIMIT": ("max_gap_mil", "mil"),
        "MAXUNCOUPLEDLENGTH": ("max_uncoupled_length_mil", "mil"),
        "IMPEDANCEPROFILEDRIVEN": ("impedance_profile_driven", "bool"),
        "IMPEDANCEPROFILEVALUE": ("impedance_profile_ohm", "ohm"),
    },
    "RoutingVias": {
        "MINWIDTH": ("min_diameter_mil", "mil"),
        "WIDTH": ("preferred_diameter_mil", "mil"),
        "MAXWIDTH": ("max_diameter_mil", "mil"),
        "MINHOLEWIDTH": ("min_hole_mil", "mil"),
        "HOLEWIDTH": ("preferred_hole_mil", "mil"),
        "MAXHOLEWIDTH": ("max_hole_mil", "mil"),
        "VIASTYLE": ("via_style", "str"),
    },
    "Length": {
        "MINLIMIT": ("min_length_mil", "mil"),
        "MAXLIMIT": ("max_length_mil", "mil"),
    },
    "MatchedLengths": {
        "TOLERANCE": ("tolerance_mil", "mil"),
    },
    "HoleSize": {
        "MINLIMIT": ("min_hole_mil", "mil"),
        "MAXLIMIT": ("max_hole_mil", "mil"),
        "MINPERCENT": ("min_percent", "float"),
        "MAXPERCENT": ("max_percent", "float"),
        "ABSOLUTEVALUES": ("absolute_values", "bool"),
    },
    "PlaneClearance": {"CLEARANCE": ("clearance_mil", "mil")},
    "Height": {
        "MINHEIGHT": ("min_height_mil", "mil"),
        "PREFHEIGHT": ("preferred_height_mil", "mil"),
        "MAXHEIGHT": ("max_height_mil", "mil"),
    },
    "MinimumAnnularRing": {"MINIMUMRING": ("min_ring_mil", "mil")},
    "HoleToHoleClearance": {"GAP": ("clearance_mil", "mil")},
    "ComponentClearance": {"GAP": ("clearance_mil", "mil")},
}

_LAYER_KEY_RE = re.compile(
    r"(TOPLAYER|BOTTOMLAYER|MIDLAYER(\d+))_(?:(\{[^}]+\})_)?"
    r"(MINWIDTH|MAXWIDTH|PREFWIDTH|MINGAP|MAXGAP|PREFGAP)"
)
_ROUTING_LAYER_KEY_RE = re.compile(
    r"(TOP LAYER|BOTTOM LAYER|MID LAYER (\d+))_V5"
)
_LAYER_FIELDS = {
    "MINWIDTH": "min_width",
    "PREFWIDTH": "preferred_width",
    "MAXWIDTH": "max_width",
    "MINGAP": "min_gap",
    "PREFGAP": "preferred_gap",
    "MAXGAP": "max_gap",
}
_GAP_FALLBACK = {
    "min_gap": "MINLIMIT",
    "preferred_gap": "MOSTFREQGAP",
    "max_gap": "MAXLIMIT",
}
_WIDTH_FALLBACK = {
    "min_width": "MINLIMIT",
    "preferred_width": "PREFEREDWIDTH",
    "max_width": "MAXLIMIT",
}
_MIL_VALUE_RE = re.compile(r"^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?mil$")
_ROOM_VERTEX_RE = re.compile(r"(VX|VY|CX|CY|SA|EA|R|KIND)\d+")


def _mil(raw: str | None) -> float | None:
    if raw is None or not _MIL_VALUE_RE.match(raw.strip()):
        return None
    return float(raw.strip()[:-3])


def _typed(raw: str, kind: str):
    if kind == "mil":
        return _mil(raw)
    if kind == "bool":
        return raw.strip().upper() == "TRUE"
    if kind in ("ohm", "float"):
        try:
            return float(raw)
        except ValueError:
            return None
    if kind == "int":
        try:
            return int(raw)
        except ValueError:
            return None
    return raw


def _v5_layer_id(match: re.Match) -> int:
    """Map a V5 slot token to the board's v6 layer id (1 top, 32 bottom)."""
    if match.group(1).startswith("TOP"):
        return 1
    if match.group(1).startswith("BOTTOM"):
        return 32
    return int(match.group(2)) + 1


def _plain_value(raw: str):
    stripped = raw.strip()
    if stripped.upper() in ("TRUE", "FALSE"):
        return stripped.upper() == "TRUE"
    try:
        number = float(stripped)
    except ValueError:
        return raw
    return int(number) if re.fullmatch(r"-?\d+", stripped) else number


def rule_values(
    rule: PcbRule,
    signal_layers: dict[int, str],
) -> dict:
    """Decode a rule's kind-specific values.

    signal_layers maps v6 id -> display name for the board's routable copper
    layers; per-layer values for slots the board does not have are dropped.
    Lengths are in mils under *_mil keys; unrecognised keys are kept.
    """
    props = rule.properties
    known = _KIND_VALUES.get(rule.kind, {})
    values: dict = {}
    other_mil: dict[str, float] = {}
    other: dict = {}
    per_layer: dict[int, dict[str, float]] = {}
    substack_layers: dict[str, dict[int, dict[str, float]]] = {}
    routing_layers: dict[str, bool] = {}
    for key, raw in props.items():
        if key in known:
            name, kind = known[key]
            if kind != "skip":
                typed = _typed(raw, kind)
                if typed is not None:
                    values[name] = typed
            continue
        layer_match = _LAYER_KEY_RE.fullmatch(key)
        if layer_match:
            v6_id = _v5_layer_id(layer_match)
            value = _mil(raw)
            if v6_id in signal_layers and value is not None:
                target = (
                    substack_layers.setdefault(layer_match.group(3), {})
                    if layer_match.group(3)
                    else per_layer
                )
                target.setdefault(v6_id, {})[
                    _LAYER_FIELDS[layer_match.group(4)]
                ] = value
            continue
        routing_match = _ROUTING_LAYER_KEY_RE.fullmatch(key)
        if routing_match:
            v6_id = _v5_layer_id(routing_match)
            if v6_id in signal_layers:
                routing_layers[signal_layers[v6_id]] = (
                    raw.strip().upper() == "TRUE"
                )
            continue
        if key == "OBJECTCLEARANCES":
            matrix = _object_clearances(raw)
            if matrix:
                values["object_clearances_mil"] = matrix
            continue
        if rule.kind == ROOM_KIND and _ROOM_VERTEX_RE.fullmatch(key):
            continue
        mil = _mil(raw)
        if mil is not None:
            other_mil[key.lower()] = mil
        else:
            other[key.lower()] = _plain_value(raw)

    if rule.kind in ("Width", "DiffPairsRouting") and (
        per_layer or rule.kind == "DiffPairsRouting"
    ):
        fallback = (
            _GAP_FALLBACK if rule.kind == "DiffPairsRouting" else _WIDTH_FALLBACK
        )
        layers_out: dict[str, dict[str, float]] = {}
        for v6_id, layer_name in signal_layers.items():
            entry = dict(per_layer.get(v6_id, {}))
            for field_name, base_key in fallback.items():
                if field_name not in entry:
                    base = _mil(props.get(base_key))
                    if base is not None:
                        entry[field_name] = base
            if entry:
                layers_out[layer_name] = entry
        if layers_out:
            values["layers_mil"] = layers_out
    if substack_layers:
        values["substack_layers_mil"] = {
            guid: {
                signal_layers[v6_id]: fields
                for v6_id, fields in layers.items()
            }
            for guid, layers in substack_layers.items()
        }
    if routing_layers:
        values["routing_layers"] = routing_layers
    if rule.kind == ROOM_KIND:
        values.update(room_geometry(rule))
    if other_mil:
        values["other_mil"] = other_mil
    if other:
        values["other"] = other
    return values


def _object_clearances(raw: str) -> dict[str, float]:
    """Decode the sparse clearance matrix (raw 1/10000 mil, no unit)."""
    matrix: dict[str, float] = {}
    for cell in raw.split(";"):
        pair, _, amount = cell.partition(":")
        if not amount.strip():
            continue
        try:
            value = int(amount.strip()) / ALTIUM_UNITS_PER_MIL
        except ValueError:
            continue
        names = [
            part.replace("ClearanceObj_", "")
            for part in pair.split("-ClearanceObj_")
        ]
        if len(names) == 2:
            matrix[f"{names[0].replace('ClearanceObj_', '')}-{names[1]}"] = (
                value
            )
    return matrix


def room_geometry(rule: PcbRule) -> dict:
    """Side, vertex count and bounding box of a RoomDefinition rule."""
    props = rule.properties
    xs: list[float] = []
    ys: list[float] = []
    index = 0
    while f"VX{index}" in props and f"VY{index}" in props:
        x, y = _mil(props[f"VX{index}"]), _mil(props[f"VY{index}"])
        if x is not None and y is not None:
            xs.append(x)
            ys.append(y)
        index += 1
    geometry: dict = {
        "side": "bottom" if props.get("LAYER", "").upper() == "BOTTOM" else "top",
        "vertex_count": len(xs),
    }
    if xs:
        geometry["bbox_mil"] = {
            "minx_mil": min(xs),
            "miny_mil": min(ys),
            "maxx_mil": max(xs),
            "maxy_mil": max(ys),
        }
    return geometry


# ---------- resolution ----------


def rule_header(rule: PcbRule) -> dict:
    """Compact identity of a rule for listings."""
    header: dict = {
        "name": rule.name,
        "kind": rule.kind,
        "priority": rule.priority,
        "enabled": rule.enabled,
        "scope": rule.scope1,
    }
    if rule.kind in BINARY_KINDS:
        header["scope2"] = rule.scope2
    if rule.comment:
        header["comment"] = rule.comment
    if rule.from_schematic:
        header["from_schematic"] = True
    return header


def resolve_kind(
    rules: list[PcbRule],
    target: RuleTarget,
    ctx: RuleContext,
    describe,
) -> dict:
    """Resolve the rules of one kind for a target.

    rules must all be of one kind. describe(rule) renders the effective rule.
    Unary kinds: the first enabled rule by priority whose scope matches
    applies; higher-priority rules that match only conditionally are listed,
    because for some of the target's objects they win instead.
    Binary kinds: the winner depends on the other object, so every rule with
    a side matching the target is listed in priority order.
    """
    kind = rules[0].kind
    ordered = sorted(
        (rule for rule in rules if rule.enabled),
        key=lambda rule: rule.priority,
    )
    result: dict = {"kind": kind}
    if kind in BINARY_KINDS:
        candidates = []
        for rule in ordered:
            side1 = evaluate_scope(rule.scope1, target, ctx)
            side2 = evaluate_scope(rule.scope2, target, ctx)
            if side1["matches"] == "no" and side2["matches"] == "no":
                continue
            candidate = describe(rule)
            if side1["matches"] != "no":
                candidate["target_side"] = "scope1"
                candidate["other_side"] = rule.scope2
                matched = side1
            else:
                candidate["target_side"] = "scope2"
                candidate["other_side"] = rule.scope1
                matched = side2
            if matched["matches"] != "yes":
                candidate["conditions"] = matched["conditions"]
            candidates.append(candidate)
        result["candidates"] = candidates
        result["note"] = (
            "Binary rule: the first candidate (by priority) whose other "
            "side matches the other object applies to that pair of objects."
        )
        return result

    effective = []
    conditional = []
    for rule in ordered:
        side = evaluate_scope(rule.scope1, target, ctx)
        if side["matches"] == "no":
            continue
        if side["matches"] == "yes":
            effective.append(describe(rule))
            if kind not in ALL_MATCHING_KINDS:
                break
            continue
        entry = rule_header(rule)
        entry["conditions"] = side["conditions"]
        if "parse_error" in side:
            entry["parse_error"] = side["parse_error"]
        conditional.append(entry)
    if kind in ALL_MATCHING_KINDS:
        result["applicable"] = effective
        if conditional:
            result["applicable_to_some_objects"] = conditional
    else:
        result["effective"] = effective[0] if effective else None
        if conditional:
            # With an effective rule these win for some objects; without one
            # they are the only rules that can apply at all.
            key = (
                "overridden_for_some_objects_by"
                if effective
                else "applicable_to_some_objects"
            )
            result[key] = conditional
    if not effective:
        disabled = [rule.name for rule in rules if not rule.enabled]
        if disabled:
            result["disabled_rules"] = disabled
    return result
