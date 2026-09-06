#!/usr/bin/env python3
"""Render brand marks to LVGL v9 RGB565 C arrays for app_codex.

Pipeline: source art -> 48x48 coverage grid -> tint with brand color on black
-> RGB565 LE -> .c file matching the format of main/assets/images/icon_stopwatch.c.

Sources:
  - claude / codex: monochrome SVGs via svglib+reportlab+Pillow (needs deps).
  - glm / deepseek: PNGs shipped in tools/ (glm.png = official white mark with
    alpha; deepseek.png = official favicon art rendered to 960x960) via the
    stdlib-only converter in png_to_logo.py — no third-party deps needed.

Output dir defaults to the repo's firmware/assets/ (committed copies); override
with CC_ISLAND_ASSETS to write straight into a factory-firmware checkout.

Usage:
    python gen_icons.py                  # everything (claude/codex need the deps)
    python gen_icons.py glm deepseek     # stdlib-only subset
"""
import os
import sys

SRC = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get(
    "CC_ISLAND_ASSETS", os.path.normpath(os.path.join(SRC, "..", "assets"))
)

CLAUDE = 0xF2854D
CODEX = 0x3B9EFF
GLM = 0x6E56CF
DEEPSEEK = 0x4D6BFE
# The provided GLM mark is white; on the black watch face it is used as-is
# (white), per its official dark-background treatment — only the row text
# carries kGlmColor. Kept here for reference/regeneration tooling.
GLM_LOGO_COLOR = 0xFFFFFF

SIZE = 48

try:
    from svglib.svglib import svg2rlg
    from reportlab.graphics import renderPM
    from PIL import Image

    HAVE_DEPS = True
except ImportError:
    HAVE_DEPS = False

# name -> (kind, file, color)
SOURCES = {
    "claude": ("svg", "claude.svg", CLAUDE),
    "codex": ("svg", "openai.svg", CODEX),
    "glm": ("png", "glm.png", GLM_LOGO_COLOR),
    "deepseek": ("png", "deepseek.png", DEEPSEEK),
}


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


def png_coverage_grid(png_name, size):
    """Coverage grid via the stdlib converter (alpha or darkness, aspect-fit)."""
    import png_to_logo
    return png_to_logo.coverage_grid(os.path.join(SRC, png_name), size)


# --------------------------------------------------------------------------- #
# Emit
# --------------------------------------------------------------------------- #
def to_c(name, grid, color):
    """Emit `<name>.c`: grid of 0..1 coverage tinted with `color` on black."""
    h = len(grid)
    w = len(grid[0])
    r, g, b = (color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF
    rgb565 = bytearray()
    for y in range(h):
        for x in range(w):
            a = grid[y][x]
            px565 = ((int(r * a) & 0xF8) << 8) | ((int(g * a) & 0xFC) << 3) | (int(b * a) >> 3)
            rgb565 += bytes((px565 & 0xFF, (px565 >> 8) & 0xFF))  # little-endian
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


def make_launcher():
    """200x200 launcher icon: the four row logos in a 2x2 grid."""
    from PIL import Image

    canvas = Image.new("RGB", (200, 200), (0, 0, 0))
    for (name, (kind, file, color)), (ox, oy) in zip(
        SOURCES.items(), ((12, 12), (108, 12), (12, 108), (108, 108))
    ):
        grid = (render_coverage_grid(file, 80) if kind == "svg"
                else png_coverage_grid(file, 80))
        rgb = Image.new("RGB", (80, 80), (0, 0, 0))
        px = rgb.load()
        for y in range(80):
            for x in range(80):
                a = grid[y][x]
                px[x, y] = (int(((color >> 16) & 0xFF) * a),
                            int(((color >> 8) & 0xFF) * a),
                            int((color & 0xFF) * a))
        canvas.paste(rgb, (ox, oy))
    to_c("icon_codex", _grid_from_image(canvas, 200), 0xFFFFFF)


def _grid_from_image(img, size):
    px = img.load()
    return [[sum(px[x, y]) / (3 * 255.0) for x in range(size)] for y in range(size)]


def main():
    os.makedirs(OUT, exist_ok=True)
    names = [a for a in sys.argv[1:] if not a.startswith("--")]
    want_icon = "icon" in names
    names = [n for n in names if n != "icon"] or list(SOURCES)
    unknown = [n for n in names if n not in SOURCES]
    if unknown:
        raise SystemExit(f"unknown logo(s): {', '.join(unknown)}")
    svg_names = [n for n in names if SOURCES[n][0] == "svg"]
    if svg_names and not HAVE_DEPS:
        raise SystemExit(
            "svglib/reportlab/Pillow not installed — required for: "
            + ", ".join(svg_names)
            + "\n  pip install svglib reportlab pillow\n"
            "(glm / deepseek work without deps: their sources are PNGs)"
        )

    print("Generating row logos (48x48):")
    for name in names:
        kind, file, color = SOURCES[name]
        grid = (render_coverage_grid(file, SIZE) if kind == "svg"
                else png_coverage_grid(file, SIZE))
        to_c(f"logo_{name}", grid, color)

    if want_icon:
        print("Generating launcher icon (200x200):")
        make_launcher()


if __name__ == "__main__":
    main()
