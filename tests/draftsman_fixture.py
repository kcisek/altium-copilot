"""Builds tests/fixtures/pcb-fab-drawing.PCBDwf, a synthetic Draftsman document.

The XML follows the element structure of real Draftsman files (Altium Designer 20-26,
FormatVersion 18-24) but holds made-up content. It is packed the way Altium packs it: an
lz4net LZ4Stream of varint-framed chunks holding raw LZ4 blocks. Regenerate with
`python tests/draftsman_fixture.py` after changing drawing_xml().
"""
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "pcb-fab-drawing.PCBDwf"
_MM = 96 / 25.4  # Draftsman lengths are 1/96 inch


# A 2x1 PNG (one red, one blue pixel), spelled out so the fixture does not depend on the
# zlib build that would otherwise compress it.
TINY_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAIAAAABCAIAAAB7QOjdAAAADUlEQVR4nGP4zwAE/wEHAAH/4iOeWQAAAABJRU5ErkJggg=="


def _hole(size_mm: float, plated: bool, hole_type: str, slot_mm: float = 0) -> str:
    return (
        "<LayerStackHole><BottomLayerName>Bottom Layer</BottomLayerName>"
        f"<HoleSize>{size_mm * _MM}</HoleSize><HoleType>{hole_type}</HoleType>"
        f"<IsBackdrill>false</IsBackdrill><Lenght>{slot_mm * _MM}</Lenght>"
        f"<Plated>{'true' if plated else 'false'}</Plated>"
        "<TopLayerName>Top Layer</TopLayerName></LayerStackHole>"
    )


def _title(text: str) -> str:
    return (
        '<ElementItems><ElementItemData i:type="LinkedTextDrawingItemData">'
        f"<Name>Title</Name><IsShow>true</IsShow><Title>{text}</Title>"
        "</ElementItemData></ElementItems>"
    )


def _note_element(text: str) -> str:
    return f"<NoteElement><BorderStyle>None</BorderStyle><Postfix>.</Postfix><Text>{text}</Text></NoteElement>"


def _param(name: str, value: str) -> str:
    return f"<DrawingDocumentParameterData><Name>{name}</Name><Value>{value}</Value><Version/></DrawingDocumentParameterData>"


def drawing_xml() -> str:
    holes = (
        _hole(0.3, True, "Via") * 3
        + _hole(3.2, False, "Pad") * 2
        + _hole(1.0, True, "Pad", slot_mm=2.5)
    )
    padding = " " * 300  # long runs give the encoder overlapping matches
    layers = (
        "<StackLayerBoardLayerData><Comment/><IdInBoard>1</IdInBoard><Material>Copper</Material>"
        "<Name>Top Layer</Name><Note/></StackLayerBoardLayerData>"
        "<StackLayerBoardLayerData><Comment>Controlled impedance core</Comment><IdInBoard>100</IdInBoard>"
        "<Material>FR-4</Material><Name>Dielectric 1</Name><Note>Use Isola 370HR</Note></StackLayerBoardLayerData>"
    )
    return (
        '<Document xmlns="http://schemas.datacontract.org/2004/07/Altium.Designer.PcbDrawing.DataSerialization.V1"'
        ' xmlns:i="http://www.w3.org/2001/XMLSchema-instance">'
        "<Id>0</Id><BillOfMaterialsInformation i:nil=\"true\"/>"
        # The drawing's copy of the board: cut out before parsing. The undeclared "q:"
        # prefix makes the document unparseable unless the cut happens.
        "<BoardAssemblyInformation><Components><q:Part>board copy</q:Part></Components></BoardAssemblyInformation>"
        "<DocumentId>ABCDEFGH</DocumentId>"
        f"<DrillTableInformation><Id>0</Id><Holes/><HolesV2>{holes}</HolesV2></DrillTableInformation>"
        "<FormatVersion>24</FormatVersion><LastUpdate>2026-10-01T12:00:00.0000000Z</LastUpdate>"
        "<LayerStackInformation><BoardLayerStackData><BoardLayerStack><Name>Board Layer Stack</Name>"
        f"<Stack>{layers}</Stack></BoardLayerStack></BoardLayerStackData>"
        # Draftsman serialises the stack a second time; it must not double the notes.
        "<LayerStack><TupleOfstringArrayOfStackLayerBoardLayerDataA8rmZfqX><m_Item1>Board Layer Stack</m_Item1>"
        f"<m_Item2>{layers}</m_Item2></TupleOfstringArrayOfStackLayerBoardLayerDataA8rmZfqX></LayerStack>"
        "</LayerStackInformation>"
        "<Pages>"
        "<Page><Id>1</Id><Items>"
        '<Item i:type="Note"><Id>2</Id><Anchor>Top Left</Anchor><OriginatedFromTemplate>false</OriginatedFromTemplate>'
        f"{_title('Notes:')}<Elements>"
        + _note_element("Board material FR-4, Tg 150 or higher")
        + _note_element("Surface finish ENIG")
        + _note_element("")
        + _note_element("Final thickness =Total_Thickness, part DEMO-v=PrjRev, =Unknown_Param kept")
        + "</Elements><Title>Notes:</Title><TitleVisible>true</TitleVisible></Item>"
        '<Item i:type="Text"><Id>3</Id><OriginatedFromTemplate>false</OriginatedFromTemplate>'
        f"<Rect><Height>20</Height><Width>200</Width><X>10</X><Y>10</Y></Rect><Text>Do not scale this drawing{padding}</Text></Item>"
        '<Item i:type="Text"><Id>4</Id><OriginatedFromTemplate>true</OriginatedFromTemplate>'
        "<Text>Drawn by: J. Doe</Text></Item>"
        '<Item i:type="Table"><Id>5</Id><OriginatedFromTemplate>false</OriginatedFromTemplate>'
        f"{_title('Impedance Table')}<IsShowTitle>true</IsShowTitle><Payload>&lt;Table/&gt;</Payload><Rows>"
        "<ArrayOfTableCell><TableCell><Text>Layer</Text></TableCell><TableCell><Text>Impedance</Text></TableCell></ArrayOfTableCell>"
        "<ArrayOfTableCell><TableCell><Text/></TableCell><TableCell><Text/></TableCell></ArrayOfTableCell>"
        "<ArrayOfTableCell><TableCell><Text>L1</Text></TableCell><TableCell><Text>50 ohm</Text></TableCell></ArrayOfTableCell>"
        "</Rows></Item>"
        '<Item i:type="Picture"><Id>6</Id><OriginatedFromTemplate>false</OriginatedFromTemplate>'
        f"<Bitmap>{TINY_PNG}</Bitmap><Bitmap2/><MaintainAspectRatio>true</MaintainAspectRatio>"
        f"<Rect><Height>{25 * _MM}</Height><Width>{50 * _MM}</Width><X>0</X><Y>0</Y></Rect></Item>"
        '<Item i:type="BoardFabricationViewData"><Id>7</Id><OriginatedFromTemplate>false</OriginatedFromTemplate>'
        "<LayerV7Id>1</LayerV7Id><Scale><Denominator>1</Denominator><Numenator>3</Numenator></Scale>"
        "<ShowTitle>true</ShowTitle><Title>=ViewLayerName (Scale =ViewScale)</Title>"
        "<UseCustomScale>true</UseCustomScale><ViewSide>Top</ViewSide></Item>"
        '<Item i:type="Line"><Id>8</Id></Item>'
        "</Items><StandardSizeName>A4</StandardSizeName><TemplateFileName/></Page>"
        "<Page><Id>9</Id><Items>"
        '<Item i:type="BoardAssemblyViewData"><Id>10</Id><OriginatedFromTemplate>false</OriginatedFromTemplate>'
        "<Scale><Denominator>1</Denominator><Numenator>2</Numenator></Scale>"
        "<Title>View from =ViewSide side (Scale =ViewScale)</Title><UseCustomScale>false</UseCustomScale>"
        "<ViewSide>Bottom</ViewSide></Item>"
        '<Item i:type="LayerStackLegendView"><Id>11</Id>'
        f"{_title('Layer Stack Legend')}</Item>"
        "</Items><StandardSizeName>A3</StandardSizeName></Page>"
        "<Page><Id>12</Id><Items/><StandardSizeName/></Page>"
        "</Pages>"
        "<Parameters>"
        + _param("Revision", "B")
        + "<DrawingDocumentParameterData><Name>SheetTotal</Name><Value/><Version/></DrawingDocumentParameterData>"
        "</Parameters>"
        # Values the drawing cached from the board and project for its special strings.
        "<ParametersData><LastUpdate>0001-01-01T00:00:00</LastUpdate>"
        f"<PCBParameters>{_param('Total_Thickness', '1.6mm')}</PCBParameters>"
        f"<ProjectParameters>{_param('PrjRev', '3')}</ProjectParameters>"
        "</ParametersData>"
        "<SourceDocumentName>C:\\Projects\\Demo\\Demo.PcbDoc</SourceDocumentName>"
        "</Document>"
    )


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _length(out: bytearray, value: int) -> None:
    while value >= 255:
        out.append(255)
        value -= 255
    out.append(value)


def lz4_block(data: bytes) -> bytes:
    """Greedy LZ4 block encoder (4-byte hash matches), enough to produce valid blocks."""
    out = bytearray()
    table: dict[bytes, int] = {}
    i = anchor = 0
    end = len(data) - 12  # LZ4: the last 12 bytes start no match, the last 5 are literals

    def sequence(literals: bytes, offset: int = 0, match: int = 0) -> None:
        token = min(len(literals), 15) << 4 | (min(match - 4, 15) if offset else 0)
        out.append(token)
        if len(literals) >= 15:
            _length(out, len(literals) - 15)
        out.extend(literals)
        if offset:
            out.extend(offset.to_bytes(2, "little"))
            if match - 4 >= 15:
                _length(out, match - 19)

    while i < end:
        key = data[i : i + 4]
        candidate = table.get(key)
        table[key] = i
        if candidate is None or i - candidate > 65535:
            i += 1
            continue
        match = 4
        while i + match < len(data) - 5 and data[candidate + match] == data[i + match]:
            match += 1
        sequence(data[anchor:i], i - candidate, match)
        i += match
        anchor = i
    sequence(data[anchor:])
    return bytes(out)


def lz4_stream(data: bytes, chunk_size: int = 1 << 20, raw_chunks: tuple[int, ...] = ()) -> bytes:
    """lz4net LZ4Stream framing; chunk numbers in raw_chunks are stored uncompressed."""
    out = bytearray()
    for n, start in enumerate(range(0, len(data), chunk_size)):
        chunk = data[start : start + chunk_size]
        if n in raw_chunks:
            out += _varint(0) + _varint(len(chunk)) + chunk
        else:
            block = lz4_block(chunk)
            out += _varint(1) + _varint(len(chunk)) + _varint(len(block)) + block
    return bytes(out)


def build() -> bytes:
    return lz4_stream(drawing_xml().encode("utf-8"), chunk_size=2048, raw_chunks=(1,))


if __name__ == "__main__":
    FIXTURE.write_bytes(build())
    print(f"wrote {FIXTURE} ({FIXTURE.stat().st_size} bytes)")
