"""Turn pictures stored in Altium documents into image bytes an MCP client can show.

Altium keeps schematic pictures as uncompressed BMP (32 or 8 bits per pixel in practice),
which MCP clients do not accept, so they are re-encoded as PNG here without adding an
imaging dependency. Downscaling only happens when the PNG would be too large.
"""
import struct
import zlib

MAX_IMAGE_BYTES = 3_500_000  # stays under the 5 MB image limit after base64 overhead
MAX_SIDE_PX = 4096
# The API rejects larger images, and keeps rejecting a conversation that holds one.
MAX_PASSTHROUGH_SIDE_PX = 8000

_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
)


def image_format(data: bytes) -> str | None:
    """"png", "jpeg", "gif", "webp" or "bmp" from the file signature, else None."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return next((name for magic, name in _MAGIC if data.startswith(magic)), None)


def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a PNG, GIF or JPEG header, or None if it cannot be read."""
    fmt = image_format(data)
    try:
        if fmt == "png":
            return struct.unpack_from(">II", data, 16)
        if fmt == "gif":
            return struct.unpack_from("<HH", data, 6)
        if fmt == "jpeg":
            pos = 2
            while pos + 9 <= len(data) and data[pos] == 0xFF:
                marker = data[pos + 1]
                # SOFn frame headers (C0-CF except DHT C4, JPG C8, DAC CC): height, width
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    height, width = struct.unpack_from(">HH", data, pos + 5)
                    return width, height
                pos += 2 + struct.unpack_from(">H", data, pos + 2)[0]
    except struct.error:
        return None
    return None


def _png(width: int, height: int, raw: bytes) -> bytes:
    def chunk(tag: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(tag + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def bmp_to_png(bmp: bytes, max_bytes: int = MAX_IMAGE_BYTES, max_side: int = MAX_SIDE_PX) -> bytes:
    """Re-encode an uncompressed 8/24/32-bit BMP as RGB PNG, dropping alpha.

    Keeps every `step`-th pixel when the picture is larger than `max_side` or its PNG
    is larger than `max_bytes`. Raises ValueError for other BMP variants.
    """
    if len(bmp) < 54 or bmp[:2] != b"BM":
        raise ValueError("not a BMP image")
    pixel_offset = struct.unpack_from("<I", bmp, 10)[0]
    header_size, width, height, _, bpp, compression = struct.unpack_from("<IiiHHI", bmp, 14)
    standard_masks = struct.pack("<III", 0x00FF0000, 0x0000FF00, 0x000000FF)
    if bpp not in (8, 24, 32) or compression not in (0, 3) or (
        compression == 3 and bmp[54:66] != standard_masks
    ):
        raise ValueError(f"unsupported BMP ({bpp} bits per pixel, compression {compression})")
    if width <= 0 or height == 0:
        raise ValueError("empty BMP image")
    top_down = height < 0
    height = abs(height)
    row_size = (bpp * width + 31) // 32 * 4
    channels = []
    if bpp == 8:
        colors = struct.unpack_from("<I", bmp, 46)[0] or 256
        palette = bmp[14 + header_size : 14 + header_size + 4 * colors].ljust(1024, b"\0")
        channels = [palette[c::4] for c in (2, 1, 0)]  # translate tables for R, G, B

    step = max(1, -(-max(width, height) // max_side))
    while True:
        out_width = -(-width // step)
        raw = bytearray()
        for y in range(0, height, step):
            start = pixel_offset + (y if top_down else height - 1 - y) * row_size
            line = bytearray(out_width * 3)
            if bpp == 8:
                indexes = bmp[start : start + width : step]
                for c in range(3):
                    line[c::3] = indexes.translate(channels[c])
            else:
                size = bpp // 8
                row = bmp[start : start + width * size]
                line[0::3] = row[2 :: size * step]
                line[1::3] = row[1 :: size * step]
                line[2::3] = row[0 :: size * step]
            raw += b"\0" + line  # filter type 0 per scanline
        png = _png(out_width, -(-height // step), bytes(raw))
        if len(png) <= max_bytes or out_width <= 64:
            return png
        step += 1
