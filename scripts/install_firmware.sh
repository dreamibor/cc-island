#!/usr/bin/env bash
#
# Integrate the CC Island app into a fresh M5Stack StopWatch factory-firmware
# checkout, then you can build + flash with ESP-IDF.
#
# Usage:
#   scripts/install_firmware.sh [TARGET_DIR]
#
# TARGET_DIR defaults to ./build-firmware (gitignored). Safe to re-run; every
# edit below is idempotent.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${1:-$REPO_ROOT/build-firmware}"
FACTORY_GIT="https://github.com/m5stack/M5StopWatch-UserDemo.git"

echo "==> CC Island firmware integration"
echo "    repo:   $REPO_ROOT"
echo "    target: $TARGET"

# 1. Clone the factory firmware (MIT, by M5Stack) if not already present.
if [ ! -d "$TARGET/.git" ]; then
  echo "==> Cloning factory firmware..."
  git clone "$FACTORY_GIT" "$TARGET"
fi

# 2. Fetch the firmware's component dependencies (M5GFX, lvgl, mooncake, ...).
echo "==> Fetching firmware dependencies..."
( cd "$TARGET" && python3 ./fetch_repos.py )

# 2b. ESP-IDF v6.x support: the vendored M5GFX 0.2.19 predates IDF v6 (removed
#     soc/gdma_channel.h, gpio_iomux_out, register-level GPIO symbols, ...).
#     Upstream master (0.2.28+) carries the official v6 fixes. Verified against
#     IDF v6.1; harmless for v5.x builds.
echo "==> Upgrading M5GFX to upstream master (IDF v6 fixes)..."
( cd "$TARGET/components/M5GFX" && git fetch origin master -q && git checkout -q origin/master )

# 3. Drop in the CC Island app + generated brand-logo bitmaps.
echo "==> Copying app_codex + assets..."
mkdir -p "$TARGET/main/apps/app_codex/ble" "$TARGET/main/assets/images"
cp "$REPO_ROOT"/firmware/app_codex/app_codex.h          "$TARGET/main/apps/app_codex/"
cp "$REPO_ROOT"/firmware/app_codex/app_codex.cpp        "$TARGET/main/apps/app_codex/"
cp "$REPO_ROOT"/firmware/app_codex/ble/ble_nus.h        "$TARGET/main/apps/app_codex/ble/"
cp "$REPO_ROOT"/firmware/app_codex/ble/ble_nus.cpp      "$TARGET/main/apps/app_codex/ble/"
cp "$REPO_ROOT"/firmware/assets/logo_claude.c           "$TARGET/main/assets/images/"
cp "$REPO_ROOT"/firmware/assets/logo_chatgpt.c          "$TARGET/main/assets/images/"
cp "$REPO_ROOT"/firmware/assets/logo_glm.c              "$TARGET/main/assets/images/"
cp "$REPO_ROOT"/firmware/assets/logo_deepseek.c         "$TARGET/main/assets/images/"
cp "$REPO_ROOT"/firmware/assets/icon_ccisland.c          "$TARGET/main/assets/images/"

# 4. Apply the small, idempotent edits to the factory sources.
echo "==> Registering the app (idempotent edits)..."
python3 - "$TARGET" <<'PY'
import sys, pathlib
root = pathlib.Path(sys.argv[1])

def insert_after(path, anchor, line):
    p = root / path
    text = p.read_text()
    if line.strip() in text:
        return
    out, done = [], False
    for ln in text.splitlines():
        out.append(ln)
        if not done and anchor in ln:
            out.append(line)
            done = True
    if not done:
        raise SystemExit(f"anchor not found in {path}: {anchor!r}")
    p.write_text("\n".join(out) + "\n")
    print(f"   patched {path}")

def replace_in_file(path, old, new):
    p = root / path
    text = p.read_text()
    if new in text:
        return
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old!r}")
    p.write_text(text.replace(old, new))
    print(f"   patched {path}")

# apps.h: include the app header
insert_after("main/apps/apps.h",
             '#include "app_template/app_template.h"',
             '#include "app_codex/app_codex.h"')

# main.cpp: install the app
insert_after("main/main.cpp",
             "GetMooncake().installApp(std::make_unique<AppSetup>());",
             "    GetMooncake().installApp(std::make_unique<AppCodex>());")

# assets.h: declare the images
insert_after("main/assets/assets.h",
             "LV_IMG_DECLARE(icon_watch_face);",
             "LV_IMG_DECLARE(icon_ccisland);\n"
             "LV_IMG_DECLARE(logo_claude);\n"
             "LV_IMG_DECLARE(logo_chatgpt);\n"
             "LV_IMG_DECLARE(logo_glm);\n"
             "LV_IMG_DECLARE(logo_deepseek);")

# gnu++26 (IDF v6): -Werror=deprecated-enum-enum-conversion fires on the
# factory's lv_part | lv_state style selectors — cast one operand to int.
replace_in_file("main/apps/app_alarm_clock/view/alarm_list.cpp",
                "LV_PART_INDICATOR | LV_STATE_CHECKED",
                "static_cast<int>(LV_PART_INDICATOR) | LV_STATE_CHECKED")
replace_in_file("main/apps/app_setup/workers/device.cpp",
                "LV_PART_INDICATOR | LV_STATE_CHECKED",
                "static_cast<int>(LV_PART_INDICATOR) | LV_STATE_CHECKED")

# main: same toolchain turns the v5.5-era designated-initializer shorthand
# into -Werror=missing-field-initializers — scope the relaxation to main.
replace_in_file("main/CMakeLists.txt",
                '    EMBED_TXTFILES\n        "hal/utils/config_ap/assets/badge_config_ap.html"\n)',
                '    EMBED_TXTFILES\n        "hal/utils/config_ap/assets/badge_config_ap.html"\n)\n\n'
                '# ESP-IDF v6 builds with gnu++26/GCC 15, where the v5.5-era\n'
                '# designated-initializer shorthand in this codebase trips\n'
                '# -Werror=missing-field-initializers; keep it a warning for main only.\n'
                'target_compile_options(${COMPONENT_LIB} PRIVATE -Wno-error=missing-field-initializers)')

# sdkconfig.defaults: enable NimBLE
sdk = root / "sdkconfig.defaults"
txt = sdk.read_text()
if "CONFIG_BT_NIMBLE_ENABLED=y" not in txt:
    sdk.write_text(txt.rstrip() + "\n\n# BLE (NimBLE) for CC Island usage push\n"
                   "CONFIG_BT_ENABLED=y\nCONFIG_BT_NIMBLE_ENABLED=y\n"
                   "# v6.1 NimBLE overflows its 4K default host stack once advertising\n"
                   "CONFIG_BT_NIMBLE_HOST_TASK_STACK_SIZE=8192\n")
    print("   patched sdkconfig.defaults")
PY

cat <<EOF

==> Done. Next steps:

  1. Install ESP-IDF v5.5.4 (per the factory README) or v6.1 (verified on this
     branch — see docs/windows-port-design.zh-CN.md §0) and source it:
       . ~/esp/esp-idf/export.sh
  2. Build + flash (device in your USB port):
       cd "$TARGET"
       idf.py build
       idf.py -p /dev/cu.usbmodemXXXX flash

  (If you added/changed files later, run: idf.py reconfigure)
EOF
