from pathlib import Path


def _read_text(path) -> str:
    """The file's text: UTF-8 when it decodes, else the Windows ANSI code page (cp1252),
    which keeps characters like µ and Ω that a replacement decode would lose."""
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _split_at(member: str, parts: dict) -> int:
    """Where a 'REFDES-PIN' member splits. Pins may hold '-' themselves (a battery's
    '-' pin: 'BT1--'), so the rightmost '-' whose prefix is a known part wins; without
    one (no part list), the last '-'."""
    index = len(member)
    while parts:
        index = member.rfind("-", 0, index)
        if index <= 0:
            break
        if member[:index] in parts:
            return index
    return member.rfind("-")


def parse_protel_netlist(path) -> dict:
    """Parse a Protel .NET file into nets, an inverted pin index and the part list.

    Returns:
        {
            "nets": {net_name: [(refdes, pin), ...]},
            "pin_to_net": {refdes: {pin: net_name}},
            "parts": {refdes: {"footprint": str, "comment": str}},
        }

    A part block is positional: '[', designator, footprint, comment, three
    description lines, ']'. Any line may be empty. parts lists every compiled
    part, unconnected ones included; parts compile-masked or not compiled are
    absent.
    """
    nets: dict[str, list[tuple[str, str]]] = {}
    pin_to_net: dict[str, dict[str, str]] = {}
    parts: dict[str, dict[str, str]] = {}

    lines = _read_text(path).splitlines()

    block: list[str] | None = None
    in_net_block = False
    current_net: str | None = None

    for line in lines:
        s = line.strip()

        if s == "[" and not in_net_block:
            block = []
            continue
        if block is not None:
            if s == "]":
                if block and block[0]:
                    parts[block[0]] = {
                        "footprint": block[1] if len(block) > 1 else "",
                        "comment": block[2] if len(block) > 2 else "",
                    }
                block = None
            else:
                block.append(s)
            continue

        if s == "(":
            in_net_block = True
            current_net = None
            continue
        if s == ")":
            in_net_block = False
            current_net = None
            continue
        if not in_net_block or not s:
            continue

        if current_net is None:
            current_net = s
            nets[current_net] = []
        else:
            idx = _split_at(s, parts)
            if idx <= 0:
                continue
            refdes = s[:idx]
            pin = s[idx + 1:]
            nets[current_net].append((refdes, pin))
            pin_to_net.setdefault(refdes, {})[pin] = current_net

    return {"nets": nets, "pin_to_net": pin_to_net, "parts": parts}
