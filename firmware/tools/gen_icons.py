#!/usr/bin/env python3
"""Render brand SVGs to LVGL v9 RGB565 C arrays for app_codex.

Pipeline: SVG --(svglib/reportlab, scaled via dpi)--> black-on-white raster
--> coverage mask --> tint with brand color on black --> RGB565 LE --> .c file
matching the format of main/assets/images/icon_stopwatch.c.

If svglib/reportlab/Pillow are unavailable, pass `--fallback` to rasterize the
two procedural marks (GLM "Z", DeepSeek whale) with a dependency-free polygon
sampler instead — that path needs nothing beyond the stdlib.

Usage:
    python gen_icons.py                  # everything (needs svglib+reportlab+Pillow)
    python gen_icons.py glm deepseek     # subset
    python gen_icons.py --fallback       # deps-free glm + deepseek only
"""
import math
import os
import sys

SRC = os.path.dirname(os.path.abspath(__file__))
# Output dir for the generated LVGL .c files. Defaults to the repo's
# firmware/assets/ (committed copies); override with CC_ISLAND_ASSETS to write
# straight into a factory-firmware checkout's main/assets/images/.
OUT = os.environ.get(
    "CC_ISLAND_ASSETS", os.path.normpath(os.path.join(SRC, "..", "assets"))
)

CLAUDE = 0xF2854D
CODEX = 0x3B9EFF
GLM = 0x6E56CF
DEEPSEEK = 0x4D6BFE

SIZE = 48

try:
    from svglib.svglib import svg2rlg
    from reportlab.graphics import renderPM
    from PIL import Image

    HAVE_DEPS = True
except ImportError:
    HAVE_DEPS = False


def render_coverage_grid(svg_name, size):
    """Coverage grid [y][x] of 0..1 floats from a monochrome (black) SVG."""
    d = svg2rlg(os.path.join(SRC, svg_name))
    dpi = 72.0 * size / float(d.width)
    pil = renderPM.drawToPIL(d, dpi=dpi, bg=0xFFFFFF).convert("L")
    if pil.size != (size, size):
        pil = pil.resize((size, size), Image.LANCZOS)
    # black shape on white bg -> invert so shape = high coverage
    pil = pil.point(lambda p: 255 - p)
    px = pil.load()
    return [[px[x, y] / 255.0 for x in range(size)] for y in range(size)]


# --------------------------------------------------------------------------- #
# Dependency-free fallback: procedural coverage for the two new marks.
# --------------------------------------------------------------------------- #
Z_POLYGON = [(6, 8), (42, 8), (42, 15), (18, 37), (42, 37),
             (42, 44), (6, 44), (6, 37), (30, 15), (6, 15)]

# Flattened whale outline (the deepseek.svg path, translated +2 in x).
_WHALE_SEGS = [
    ((45, 25), (45, 17), (33, 11), (25, 13)),
    ((25, 13), (15, 15), (9, 21), (8, 27)),
    ((8, 27), (5, 23), (2, 21), (0, 17)),
    ((0, 17), (0, 22), (2, 28), (6, 30)),
    ((6, 30), (2, 32), (1, 36), (2, 39)),
    ((2, 39), (6, 36), (10, 34), (13, 33)),
    ((13, 33), (20, 37), (34, 36), (41, 31)),
    ((41, 31), (45, 29), (45, 27), (45, 25)),
]
_WHALE_EYE = (36, 23, 1.8)


def _cubic(p0, p1, p2, p3, t):
    mt = 1 - t
    x = mt**3 * p0[0] + 3 * mt**2 * t * p1[0] + 3 * mt * t**2 * p2[0] + t**3 * p3[0]
    y = mt**3 * p0[1] + 3 * mt**2 * t * p1[1] + 3 * mt * t**2 * p2[1] + t**3 * p3[1]
    return x, y


def _flatten():
    pts = []
    for p0, p1, p2, p3 in _WHALE_SEGS:
        for i in range(16):
            pts.append(_cubic(p0, p1, p2, p3, i / 16))
    return pts


def _in_poly(pts, x, y):
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def fallback_coverage_grid(kind, size):
    """Coverage grid via 4x4 supersampling; stdlib only."""
    poly = Z_POLYGON if kind == "glm" else _flatten()
    grid = [[0.0] * size for _ in range(size)]
    ss = 4
    for y in range(size):
        for x in range(size):
            hit = 0
            for sy in range(ss):
                for sx in range(ss):
                    px_pt = x + (sx + 0.5) / ss
                    py_pt = y + (sy + 0.5) / ss
                    if _in_poly(poly, px_pt, py_pt):
                        if kind == "deepseek":
                            ex, ey, er = _WHALE_EYE
                            if (px_pt - ex) ** 2 + (py_pt - ey) ** 2 < er * er:
                                continue  # eye hole
                        hit += 1
            grid[y][x] = hit / (ss * ss)
    return grid


# --------------------------------------------------------------------------- #
# Emit
# --------------------------------------------------------------------------- #
def to_c(name, grid, color):
    """Emit `<name>.c`: grid of 0..1 coverage tinted with `color` on black."""
    h = len(grid)
    w = len(grid[0])
    r, g, b = (color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF
    data = bytearray()
    for y in range(h):
        for x in range(w):
            a = grid[y][x]
            data += bytes((int(r * a), int(g * a), int(b * a)))
    v = data  # per-pixel RGB, packed to RGB565 LE below
    rgb565 = bytearray()
    for i in range(0, len(v), 3):
        rr, gg, bb = v[i], v[i + 1], v[i + 2]
        px = ((rr & 0xF8) << 8) | ((gg & 0xFC) << 3) | (bb >> 3)
        rgb565 += bytes((px & 0xFF, (px >> 8) & 0xFF))  # little-endian
    up = name.upper()
    lines = [
        "#ifdef __has_include",
        '#if __has_include("lvgl.h")',
        "#ifndef LV_LVGL_H_INCLUDE_SIMPLE",
        "#define LV_LVGL_H_INCLUDE_SIMPLE",
        "#endif", "#endif", "#endif", "",
        "#if defined(LV_LVGL_H_INCLUDE_SIMPLE)",
        '#include "lvgl.h"', "#else", '#include "lvgl/lvgl.h"', "#endif", "",
        "#ifndef LV_ATTRIBUTE_MEM_ALIGN", "#define LV_ATTRIBUTE_MEM_ALIGN", "#endif", "",
        f"#ifndef LV_ATTRIBUTE_IMAGE_{up}", f"#define LV_ATTRIBUTE_IMAGE_{up}", "#endif", "",
        f"const LV_ATTRIBUTE_MEM_ALIGN LV_ATTRIBUTE_LARGE_CONST "
        f"LV_ATTRIBUTE_IMAGE_{up} uint8_t {name}_map[] = {{",
    ]
    for i in range(0, len(rgb565), 16):
        chunk = rgb565[i:i + 16]
        lines.append("    " + ", ".join(f"0x{byte:02x}" for byte in chunk) + ",")
    lines += [
        "};", "",
        f"const lv_image_dsc_t {name} = {{",
        "    .header.cf    = LV_COLOR_FORMAT_RGB565,",
        "    .header.magic = LV_IMAGE_HEADER_MAGIC,",
        f"    .header.w     = {w},",
        f"    .header.h     = {h},",
        f"    .data_size    = {w * h} * 2,",
        f"    .data         = {name}_map,",
        "};", "",
    ]
    with open(os.path.join(OUT, f"{name}.c"), "w") as f:
        f.write("\n".join(lines))
    print(f"  {name}.c  {w}x{h}  ({len(rgb565)} bytes)")


def main():
    os.makedirs(OUT, exist_ok=True)
    args = sys.argv[1:]
    use_fallback = "--fallback" in args
    args = [a for a in args if not a.startswith("--")]
    if use_fallback:
        args = [a for a in args if a in ("glm", "deepseek")]  # procedural marks only
    if not args:
        args = ["glm", "deepseek"] if use_fallback else ["claude", "codex", "glm", "deepseek"]

    print("Generating row logos (48x48):")
    for name in args:
        if name == "claude":
            to_c("logo_claude", render_coverage_grid("claude.svg", SIZE), CLAUDE)
        elif name == "codex":
            to_c("logo_codex", render_coverage_grid("openai.svg", SIZE), CODEX)
        elif name == "glm":
            grid = fallback_coverage_grid("glm", SIZE) if use_fallback \
                else render_coverage_grid("glm.svg", SIZE)
            to_c("logo_glm", grid, GLM)
        elif name == "deepseek":
            grid = fallback_coverage_grid("deepseek", SIZE) if use_fallback \
                else render_coverage_grid("deepseek.svg", SIZE)
            to_c("logo_deepseek", grid, DEEPSEEK)
        else:
            raise SystemExit(f"unknown logo: {name}")

    if use_fallback:
        return

    print("Generating launcher icon (200x200):")
    def paste(dst, src_grid, color, ox, oy, size):
        for y in range(size):
            for x in range(size):
                a = src_grid[y][x]
                dst[y + oy][x + ox] = (
                    int(((color >> 16) & 0xFF) * a),
                    int(((color >> 8) & 0xFF) * a),
                    int((color & 0xFF) * a),
                )

    canvas = [[(0, 0, 0)] * 200 for _ in range(200)]
    paste(canvas, render_coverage_grid("claude.svg", 80), CLAUDE, 12, 12, 80)
    paste(canvas, render_coverage_grid("openai.svg", 80), CODEX, 108, 12, 80)
    paste(canvas, render_coverage_grid("glm.svg", 80), GLM, 12, 108, 80)
    paste(canvas, render_coverage_grid("deepseek.svg", 80), DEEPSEEK, 108, 108, 80)
    # Reuse to_c by wrapping the RGB canvas as a "coverage-like" structure is
    # not possible (it is already RGB), so emit the launcher inline.
    data = bytearray()
    for row in canvas:
        for (rr, gg, bb) in row:
            px565 = ((rr & 0xF8) << 8) | ((gg & 0xFC) << 3) | (bb >> 3)
            data += bytes((px565 & 0xFF, (px565 >> 8) & 0xFF))
    up = "ICON_CODEX"
    lines = [
        "#ifdef __has_include",
        '#if __has_include("lvgl.h")',
        "#ifndef LV_LVGL_H_INCLUDE_SIMPLE",
        "#define LV_LVGL_H_INCLUDE_SIMPLE",
        "#endif", "#endif", "#endif", "",
        "#if defined(LV_LVGL_H_INCLUDE_SIMPLE)",
        '#include "lvgl.h"', "#else", '#include "lvgl/lvgl.h"', "#endif", "",
        "#ifndef LV_ATTRIBUTE_MEM_ALIGN", "#define LV_ATTRIBUTE_MEM_ALIGN", "#endif", "",
        f"#ifndef LV_ATTRIBUTE_IMAGE_{up}", f"#define LV_ATTRIBUTE_IMAGE_{up}", "#endif", "",
        f"const LV_ATTRIBUTE_MEM_ALIGN LV_ATTRIBUTE_LARGE_CONST "
        f"LV_ATTRIBUTE_IMAGE_{up} uint8_t icon_codex_map[] = {{",
    ]
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        lines.append("    " + ", ".join(f"0x{byte:02x}" for byte in chunk) + ",")
    lines += [
        "};", "",
        "const lv_image_dsc_t icon_codex = {",
        "    .header.cf    = LV_COLOR_FORMAT_RGB565,",
        "    .header.magic = LV_IMAGE_HEADER_MAGIC,",
        "    .header.w     = 200,",
        "    .header.h     = 200,",
        "    .data_size    = 200 * 200 * 2,",
        "    .data         = icon_codex_map,",
        "};", "",
    ]
    with open(os.path.join(OUT, "icon_codex.c"), "w") as f:
        f.write("\n".join(lines))
    print("  icon_codex.c  200x200")


if __name__ == "__main__":
    main()
