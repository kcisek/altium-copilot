"""BMP -> PNG re-encoding for pictures embedded in schematics."""
import random
import struct
import zlib

import pytest

from services.images import bmp_to_png, image_format


def _bmp(width: int, height: int, pixels: list[list[tuple]], bpp: int = 32,
         palette: list[tuple] | None = None, top_down: bool = False, compression: int = 0) -> bytes:
    """Build a BMP from rows of (r, g, b) or palette indexes, listed top row first."""
    row_size = (bpp * width + 31) // 32 * 4
    rows = pixels if top_down else pixels[::-1]
    data = bytearray()
    for row in rows:
        line = bytearray()
        for px in row:
            if bpp == 8:
                line.append(px)
            else:
                r, g, b = px
                line += bytes([b, g, r]) + (b"\x80" if bpp == 32 else b"")
        data += line.ljust(row_size, b"\0")
    colors = b"".join(bytes([b, g, r, 0]) for r, g, b in palette or [])
    offset = 14 + 40 + len(colors)
    info = struct.pack("<IiiHHIIiiII", 40, width, -height if top_down else height, 1, bpp,
                       compression, len(data), 2835, 2835, len(palette or []), 0)
    return b"BM" + struct.pack("<IHHI", offset + len(data), 0, 0, offset) + info + colors + bytes(data)


def _pixels(png: bytes) -> list[list[tuple]]:
    """Decode an 8-bit RGB PNG with filter type 0 (what bmp_to_png writes)."""
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    pos, idat, width, height = 8, b"", 0, 0
    while pos < len(png):
        length, tag = struct.unpack_from(">I4s", png, pos)
        body = png[pos + 8 : pos + 8 + length]
        if tag == b"IHDR":
            width, height, depth, color = struct.unpack_from(">IIBB", body)
            assert (depth, color) == (8, 2)
        elif tag == b"IDAT":
            idat += body
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = 1 + width * 3
    rows = []
    for y in range(height):
        line = raw[y * stride : (y + 1) * stride]
        assert line[0] == 0
        rows.append([tuple(line[1 + 3 * x : 4 + 3 * x]) for x in range(width)])
    return rows


RED, GREEN, BLUE, WHITE = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)
PICTURE = [[RED, GREEN, BLUE], [WHITE, RED, GREEN]]


@pytest.mark.parametrize("bpp", [24, 32])
def test_bottom_up_rgb(bpp):
    assert _pixels(bmp_to_png(_bmp(3, 2, PICTURE, bpp=bpp))) == PICTURE


def test_top_down():
    assert _pixels(bmp_to_png(_bmp(3, 2, PICTURE, top_down=True))) == PICTURE


def test_bitfields_with_standard_masks():
    bmp = bytearray(_bmp(3, 2, PICTURE, compression=3))
    masks = struct.pack("<III", 0x00FF0000, 0x0000FF00, 0x000000FF)
    # BI_BITFIELDS keeps its masks after the 40-byte header; shift the pixel data along.
    bmp = bmp[:54] + masks + bmp[54:]
    struct.pack_into("<I", bmp, 10, 54 + 12)
    assert _pixels(bmp_to_png(bytes(bmp))) == PICTURE


def test_palette_8bit():
    palette = [BLUE, WHITE]
    bmp = _bmp(3, 2, [[0, 1, 0], [1, 1, 0]], bpp=8, palette=palette)
    assert _pixels(bmp_to_png(bmp)) == [[BLUE, WHITE, BLUE], [WHITE, WHITE, BLUE]]


def test_downscale_keeps_every_nth_pixel():
    picture = [[(x * 10, y * 10, 0) for x in range(5)] for y in range(4)]
    out = _pixels(bmp_to_png(_bmp(5, 4, picture), max_side=2))
    assert out == [[(0, 0, 0), (30, 0, 0)], [(0, 30, 0), (30, 30, 0)]]


def test_downscale_until_under_byte_limit():
    rng = random.Random(1)
    picture = [[tuple(rng.randrange(256) for _ in range(3)) for _ in range(400)] for _ in range(8)]
    png = bmp_to_png(_bmp(400, 8, picture), max_bytes=4000)
    assert len(png) <= 4000
    assert len(_pixels(png)[0]) < 400


@pytest.mark.parametrize("bpp,compression", [(16, 0), (8, 1), (32, 2)])
def test_unsupported_variants(bpp, compression):
    bmp = bytearray(_bmp(2, 2, [[RED, RED], [RED, RED]]))
    struct.pack_into("<HI", bmp, 28, bpp, compression)
    with pytest.raises(ValueError, match="unsupported"):
        bmp_to_png(bytes(bmp))


def test_not_a_bmp():
    with pytest.raises(ValueError):
        bmp_to_png(b"\x89PNG\r\n\x1a\n" + b"\0" * 60)


def test_truncated_bmp_raises():
    with pytest.raises(ValueError):
        bmp_to_png(_bmp(3, 2, PICTURE)[:-10])


@pytest.mark.parametrize("data,expected", [
    (b"\x89PNG\r\n\x1a\nrest", "png"),
    (b"\xff\xd8\xff\xe0rest", "jpeg"),
    (b"GIF89a", "gif"),
    (b"RIFF\0\0\0\0WEBPVP8 ", "webp"),
    (b"BM\0\0", "bmp"),
    (b"%PDF-1.7", None),
])
def test_image_format(data, expected):
    assert image_format(data) == expected
