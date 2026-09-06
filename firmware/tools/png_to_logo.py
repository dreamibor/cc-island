#!/usr/bin/env python3
"""Convert a rendered PNG into an LVGL RGB565 .c asset for app_codex.

Pure stdlib. Two coverage rules, chosen automatically:

  - PNG with transparency (alpha channel or tRNS): coverage = alpha.
    Tint with the shape's intended color — e.g. the GLM mark ships as white
    with antialiased alpha, tinted 0xFFFFFF so it reads on the black face.
  - Opaque PNG: coverage = darkness (black shape on white background).
    Tint with the brand color — e.g. the DeepSeek whale rendered from
    tools/deepseek.svg, tinted 0x4D6BFE.

Non-square sources are aspect-fitted into the square grid and centered.

Usage:
  python3 png_to_logo.py <png> <logo_name> <0xRRGGBB> [size=48]

Rendering an SVG source first (no browser needed when svglib is installed —
gen_icons.py covers that; otherwise):

  msedge --headless=new --disable-gpu --window-size=960,960 \
      --screenshot=logo_960.png <html wrapper showing the svg at 960x960>
  python3 png_to_logo.py logo_960.png logo_deepseek 0x4D6BFE
"""
import os
import struct
import sys
import zlib

import gen_icons  # same dir; reuses to_c()


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def read_png_pixels(path):
    """Decode an 8-bit non-interlaced PNG -> (w, h, bpp, rows, has_alpha).

    Rows are expanded to RGBA (bpp=4) for ctype 2/3/6.
    """
    raw = open(path, "rb").read()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit("not a PNG")
    pos, w, h, depth, ctype = 8, 0, 0, 0, 0
    idat, plte, trns = b"", None, None
    while pos < len(raw):
        ln, typ = struct.unpack(">I4s", raw[pos:pos + 8])
        chunk = raw[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", chunk[:10])
            if depth != 8 or ctype not in (2, 3, 6) or chunk[12] != 0:
                raise SystemExit(f"unsupported PNG: depth={depth} ctype={ctype} (need 8-bit RGB/RGBA/palette)")
        elif typ == b"IDAT":
            idat += chunk
        elif typ == b"PLTE":
            plte = chunk
        elif typ == b"tRNS":
            trns = chunk
        elif typ == b"IEND":
            break
        pos += 12 + ln
    if not idat:
        raise SystemExit("no IDAT")

    src_bpp = {2: 3, 6: 4, 3: 1}[ctype]
    stride = w * src_bpp
    data = zlib.decompress(idat)
    rows, prev = [], bytearray(stride)
    off = 0
    for _ in range(h):
        f = data[off]
        line = bytearray(data[off + 1:off + 1 + stride])
        off += 1 + stride
        for x in range(stride):
            a = line[x - src_bpp] if x >= src_bpp else 0
            b = prev[x]
            c = prev[x - src_bpp] if x >= src_bpp else 0
            if f == 1:
                line[x] = (line[x] + a) & 0xFF
            elif f == 2:
                line[x] = (line[x] + b) & 0xFF
            elif f == 3:
                line[x] = (line[x] + (a + b) // 2) & 0xFF
            elif f == 4:
                line[x] = (line[x] + _paeth(a, b, c)) & 0xFF
        rows.append(bytes(line))
        prev = line

    # Expand everything to RGBA.
    out, has_alpha = [], False
    for y in range(h):
        row = bytearray(w * 4)
        for x in range(w):
            if ctype == 2:
                r, g, b = rows[y][x * 3:x * 3 + 3]
                a = 255
            elif ctype == 6:
                r, g, b, a = rows[y][x * 4:x * 4 + 4]
            else:
                idx = rows[y][x]
                r, g, b = plte[idx * 3:idx * 3 + 3]
                a = trns[idx] if trns and idx < len(trns) else 255
            if a != 255:
                has_alpha = True
            row[x * 4:x * 4 + 4] = bytes((r, g, b, a))
        out.append(bytes(row))
    return w, h, 4, out, has_alpha


def coverage_grid(path, size=48):
    """Aspect-fit the PNG into a [y][x] grid of shape coverage 0..1."""
    w, h, bpp, rows, has_alpha = read_png_pixels(path)

    def cov(x, y):
        o = x * bpp                              # rows[y] is one RGBA row
        if has_alpha:
            return rows[y][o + 3] / 255.0          # transparent PNG -> alpha mask
        r, g, b = rows[y][o], rows[y][o + 1], rows[y][o + 2]
        return (255 - (r + g + b) / 3) / 255.0     # opaque -> darkness mask

    # fit preserving aspect: source extent covered by each target cell
    scale = min(w / size, h / size)
    sw, sh = w / scale, h / scale                   # scaled extent in cells
    ox, oy = (size - sw) / 2.0, (size - sh) / 2.0   # centering offsets
    grid = [[0.0] * size for _ in range(size)]
    for ty in range(size):
        sy0, sy1 = (ty - oy) * scale, (ty + 1 - oy) * scale
        if sy1 <= 0 or sy0 >= h:
            continue
        for tx in range(size):
            sx0, sx1 = (tx - ox) * scale, (tx + 1 - ox) * scale
            if sx1 <= 0 or sx0 >= w:
                continue
            acc = area = 0.0
            for y in range(max(0, int(sy0)), min(h, int(sy1) + 1)):
                wy = min(sy1, y + 1) - max(sy0, y)
                if wy <= 0:
                    continue
                for x in range(max(0, int(sx0)), min(w, int(sx1) + 1)):
                    wx = min(sx1, x + 1) - max(sx0, x)
                    if wx <= 0:
                        continue
                    acc += cov(x, y) * wx * wy
                    area += wx * wy
            grid[ty][tx] = acc / area if area else 0.0
    return grid


def preview(grid):
    for row in grid:
        print("".join("#" if v > 0.5 else ("+" if v > 0.1 else ".") for v in row))


def main():
    if len(sys.argv) < 4:
        raise SystemExit(__doc__)
    png, name, color = sys.argv[1], sys.argv[2], int(sys.argv[3], 16)
    size = int(sys.argv[4]) if len(sys.argv) > 4 else 48
    grid = coverage_grid(png, size)
    preview(grid)
    gen_icons.to_c(name, grid, color)


if __name__ == "__main__":
    main()
