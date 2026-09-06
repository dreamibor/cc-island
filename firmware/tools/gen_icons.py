#!/usr/bin/env python3
"""Render brand marks to LVGL v9 RGB565 C arrays for app_codex.

Pipeline: PNG source art -> 48x48 coverage grid -> tint with brand color on
black -> RGB565 LE -> .c file matching the format of
main/assets/images/icon_stopwatch.c.

All sources are PNGs shipped in tools/ (pure-stdlib conversion via
png_to_logo.py — alpha mask when the PNG has transparency, darkness mask when
opaque, aspect-fitted into the grid):
  claude.png    official Claude mark (browser render of claude.svg)
  openai.png    official OpenAI mark (browser render of openai.svg)
  glm.png       official GLM "Z" mark, white with antialiased alpha
  deepseek.png  official DeepSeek whale (render of the official favicon SVG)

GLM is tinted kGlmColor (violet) to match the row brand color.

Output dir defaults to the repo's firmware/assets/ (committed copies); override
with CC_ISLAND_ASSETS to write straight into a factory-firmware checkout.

Usage:
    python gen_icons.py            # all four row logos
    python gen_icons.py icon       # also regenerate the launcher icon
    python gen_icons.py glm icon   # subset + launcher
"""
import os
import sys

SRC = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get(
    "CC_ISLAND_ASSETS", os.path.normpath(os.path.join(SRC, "..", "assets"))
)

CLAUDE = 0xF2854D
CHATGPT = 0x3B9EFF   # row color for the ChatGPT row (was "Codex")
GLM = 0x6E56CF
DEEPSEEK = 0x4D6BFE

SIZE = 48

# name -> (png file, color)
SOURCES = {
    "claude": ("claude.png", CLAUDE),
    "chatgpt": ("openai.png", CHATGPT),
    "glm": ("glm.png", GLM),
    "deepseek": ("deepseek.png", DEEPSEEK),
}


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
    canvas = [[(0, 0, 0)] * 200 for _ in range(200)]
    for (name, (_, color)), (ox, oy) in zip(SOURCES.items(), ((12, 12), (108, 12), (12, 108), (108, 108))):
        grid = png_coverage_grid(SOURCES[name][0], 80)
        for y in range(80):
            for x in range(80):
                a = grid[y][x]
                canvas[y + oy][x + ox] = (
                    int(((color >> 16) & 0xFF) * a),
                    int(((color >> 8) & 0xFF) * a),
                    int((color & 0xFF) * a),
                )
    data = bytearray()
    for row in canvas:
        for (r, g, b) in row:
            px565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            data += bytes((px565 & 0xFF, (px565 >> 8) & 0xFF))
    up = "ICON_CHATGPT"
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
        f"LV_ATTRIBUTE_IMAGE_{up} uint8_t icon_chatgpt_map[] = {{",
    ]
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        lines.append("    " + ", ".join(f"0x{byte:02x}" for byte in chunk) + ",")
    lines += [
        "};", "",
        "const lv_image_dsc_t icon_chatgpt = {",
        "    .header.cf    = LV_COLOR_FORMAT_RGB565,",
        "    .header.magic = LV_IMAGE_HEADER_MAGIC,",
        "    .header.w     = 200,",
        "    .header.h     = 200,",
        "    .data_size    = 200 * 200 * 2,",
        "    .data         = icon_chatgpt_map,",
        "};", "",
    ]
    with open(os.path.join(OUT, "icon_chatgpt.c"), "w") as f:
        f.write("\n".join(lines))
    print("  icon_chatgpt.c  200x200")


def main():
    os.makedirs(OUT, exist_ok=True)
    names = [a for a in sys.argv[1:] if not a.startswith("--")]
    want_icon = "icon" in names
    names = [n for n in names if n != "icon"] or list(SOURCES)
    unknown = [n for n in names if n not in SOURCES]
    if unknown:
        raise SystemExit(f"unknown logo(s): {', '.join(unknown)}")

    print("Generating row logos (48x48):")
    for name in names:
        file, color = SOURCES[name]
        to_c(f"logo_{name}", png_coverage_grid(file, SIZE), color)

    if want_icon:
        print("Generating launcher icon (200x200):")
        make_launcher()


if __name__ == "__main__":
    main()
