/*
 * SPDX-FileCopyrightText: 2026 wangjiacheng
 *
 * SPDX-License-Identifier: MIT
 */
#include "app_codex.h"
#include "ble/ble_nus.h"
#include <hal/hal.h>
#include <mooncake.h>
#include <mooncake_log.h>
#include <assets/assets.h>
#include <smooth_lvgl.hpp>
#include <lvgl.h>
#include <cJSON.h>
#include <cstdio>
#include <cstring>

using namespace mooncake;
using namespace smooth_ui_toolkit::lvgl_cpp;

// Brand accent colors
static constexpr uint32_t kClaudeColor   = 0xF2854D;  // vivid orange (Anthropic-ish)
static constexpr uint32_t kChatgptColor  = 0x3B9EFF;  // vivid blue
static constexpr uint32_t kGlmColor      = 0x6E56CF;  // violet (kept apart from ChatGPT blue)
static constexpr uint32_t kDeepseekColor = 0x4D6BFE;  // DeepSeek blue
static constexpr uint32_t kDetailColor   = 0x8A8A8A;  // muted text

// 5h-window utilization that triggers a haptic alert when first crossed.
static constexpr int kAlertThreshold = 80;

// DeepSeek balance (CNY) below which the watch buzzes once (docs §5.3).
static constexpr double kDsLowBalanceCny = 50.0;

namespace {

// cJSON helpers ------------------------------------------------------------- //
const char* jstr(cJSON* obj, const char* key, const char* dflt)
{
    cJSON* it = obj ? cJSON_GetObjectItem(obj, key) : nullptr;
    return (it && cJSON_IsString(it) && it->valuestring) ? it->valuestring : dflt;
}

double jdbl(cJSON* obj, const char* key, double dflt)
{
    cJSON* it = obj ? cJSON_GetObjectItem(obj, key) : nullptr;
    return (it && cJSON_IsNumber(it)) ? it->valuedouble : dflt;
}

// Watch rows ---------------------------------------------------------------- //
// Per-provider widget handles + last value, so BLE updates can refresh in place.
struct ProviderRow {
    lv_obj_t* cont   = nullptr;  // row container, for the unconfigured dim state
    lv_obj_t* bar    = nullptr;
    lv_obj_t* pct5h  = nullptr;
    lv_obj_t* detail = nullptr;  // "7d N%  reset HhMMm"
    int last_p5h     = -1;       // for threshold-crossing haptics

    void set_dim(bool dim)
    {
        if (cont) lv_obj_set_style_opa(cont, dim ? LV_OPA_40 : LV_OPA_COVER, 0);
    }

    // Rows stay dimmed with "--" until the first payload carrying their key.
    void show_placeholder()
    {
        set_dim(true);
        if (bar) lv_bar_set_value(bar, 0, LV_ANIM_OFF);
        if (pct5h) lv_label_set_text(pct5h, "--");
        if (detail) lv_label_set_text(detail, "no data");
        last_p5h = -1;
    }

    void apply(int p5h, int p7d, int reset5hMin)
    {
        set_dim(false);
        if (bar) lv_bar_set_value(bar, p5h, LV_ANIM_OFF);
        if (pct5h) {
            char b[8];
            std::snprintf(b, sizeof(b), "%d%%", p5h);
            lv_label_set_text(pct5h, b);
        }
        if (detail) {
            char b[40];
            // The bridge sends r=0 when the provider gives no reset time
            // (GLM); a genuinely imminent reset shows "?" until the next
            // push replaces the window anyway.
            if (reset5hMin > 0)
                std::snprintf(b, sizeof(b), "7d %d%%  reset %dh%02dm", p7d, reset5hMin / 60, reset5hMin % 60);
            else
                std::snprintf(b, sizeof(b), "7d %d%%  reset ?", p7d);
            lv_label_set_text(detail, b);
        }
    }
};

// DeepSeek balance row: big balance instead of a window percentage, no bar.
// Strings are kept ASCII — the factory firmware font subsets are not
// guaranteed to carry CJK/currency glyphs.
struct DsRow {
    lv_obj_t* cont = nullptr;
    lv_obj_t* bal  = nullptr;  // "110.00 CNY"
    double last_bal = -1;

    void set_dim(bool dim)
    {
        if (cont) lv_obj_set_style_opa(cont, dim ? LV_OPA_40 : LV_OPA_COVER, 0);
    }

    void show_placeholder()
    {
        set_dim(true);
        if (bal) lv_label_set_text(bal, "--");
        last_bal = -1;
    }

    // Returns true when the low-balance alert fires (first crossing below
    // kDsLowBalanceCny).
    bool apply(const char* cur, double balance)
    {
        set_dim(false);
        if (bal) {
            char b[24];
            std::snprintf(b, sizeof(b), "%.2f %s", balance, cur ? cur : "CNY");
            lv_label_set_text(bal, b);
        }

        bool crossed = (last_bal >= 0 && last_bal >= kDsLowBalanceCny && balance < kDsLowBalanceCny);
        last_bal = balance;
        return crossed;
    }
};

lv_obj_t* s_root = nullptr;
lv_obj_t* s_page1 = nullptr;
lv_obj_t* s_page2 = nullptr;
// Persists across app open/close within a boot ("glance at page 2 again").
int s_page = 0;
ProviderRow s_claude;
ProviderRow s_chatgpt;
ProviderRow s_glm;
DsRow s_ds;

void show_page(int idx)
{
    if (!s_page1 || !s_page2) return;
    if (idx == 0) {
        lv_obj_remove_flag(s_page1, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(s_page2, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(s_page1, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(s_page2, LV_OBJ_FLAG_HIDDEN);
    }
}

// Touch left/right half to flip between the two provider pages.
void on_screen_click(lv_event_t* e)
{
    lv_indev_t* indev = lv_indev_get_act();
    if (!indev) return;
    lv_point_t p;
    lv_indev_get_point(indev, &p);
    s_page = (p.x >= LV_HOR_RES / 2) ? (s_page + 1) % 2 : (s_page + 1) % 2;
    show_page(s_page);
    GetHAL().vibrate(30, 60);  // light flip tick
}

// Container styling shared by both row builders (transparent, non-scrollable).
lv_obj_t* make_row_container(lv_obj_t* parent, int y_center)
{
    lv_obj_t* cont = lv_obj_create(parent);
    lv_obj_set_size(cont, 300, 140);
    lv_obj_align(cont, LV_ALIGN_CENTER, 0, y_center);
    lv_obj_set_style_bg_opa(cont, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(cont, 0, 0);
    lv_obj_set_style_outline_width(cont, 0, 0);
    lv_obj_set_style_shadow_width(cont, 0, 0);
    lv_obj_set_style_radius(cont, 0, 0);
    lv_obj_set_style_pad_all(cont, 0, 0);
    lv_obj_remove_flag(cont, LV_OBJ_FLAG_SCROLLABLE);
    // Clicks on a row still reach the root's page-flip handler.
    lv_obj_add_flag(cont, LV_OBJ_FLAG_EVENT_BUBBLE);
    return cont;
}

// Build one window row inside `parent`, vertically centered at y_center.
ProviderRow build_row(lv_obj_t* parent, int y_center, const lv_image_dsc_t* logo, const char* name, uint32_t color)
{
    ProviderRow row;
    row.cont = make_row_container(parent, y_center);
    lv_obj_t* cont = row.cont;

    // Official brand logo (48x48 RGB565 bitmap)
    lv_obj_t* icon = lv_image_create(cont);
    lv_image_set_src(icon, logo);
    lv_obj_align(icon, LV_ALIGN_TOP_LEFT, 0, 2);

    // Provider name (brand-colored to clearly distinguish the rows)
    lv_obj_t* name_lbl = lv_label_create(cont);
    lv_label_set_text(name_lbl, name);
    lv_obj_set_style_text_font(name_lbl, &MontserratSemiBold26, 0);
    lv_obj_set_style_text_color(name_lbl, lv_color_hex(color), 0);
    lv_obj_align(name_lbl, LV_ALIGN_TOP_LEFT, 56, 8);

    // Big 5h percentage (right aligned)
    row.pct5h = lv_label_create(cont);
    lv_obj_set_style_text_font(row.pct5h, &lv_font_maple_mono_medium_28, 0);
    lv_obj_set_style_text_color(row.pct5h, lv_color_hex(color), 0);
    lv_obj_align(row.pct5h, LV_ALIGN_TOP_RIGHT, 0, 6);

    // 5h utilization bar — dim brand-tinted track + solid brand indicator
    row.bar = lv_bar_create(cont);
    lv_obj_set_size(row.bar, 300, 24);
    lv_obj_align(row.bar, LV_ALIGN_TOP_MID, 0, 52);
    lv_bar_set_range(row.bar, 0, 100);
    lv_obj_set_style_radius(row.bar, 12, LV_PART_MAIN);
    lv_obj_set_style_bg_color(row.bar, lv_color_hex(color), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(row.bar, LV_OPA_30, LV_PART_MAIN);
    lv_obj_set_style_shadow_width(row.bar, 0, LV_PART_MAIN);
    lv_obj_set_style_radius(row.bar, 12, LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(row.bar, lv_color_hex(color), LV_PART_INDICATOR);
    lv_obj_set_style_bg_opa(row.bar, LV_OPA_COVER, LV_PART_INDICATOR);
    lv_obj_add_flag(row.bar, LV_OBJ_FLAG_EVENT_BUBBLE);

    // 7d + reset line
    row.detail = lv_label_create(cont);
    lv_obj_set_style_text_font(row.detail, &lv_font_maple_mono_medium_24, 0);
    lv_obj_set_style_text_color(row.detail, lv_color_hex(kDetailColor), 0);
    lv_obj_align(row.detail, LV_ALIGN_TOP_MID, 0, 84);

    return row;
}

// DeepSeek balance row: same frame as build_row, but the big right label is
// the balance and the bar slot stays empty.
DsRow build_bal_row(lv_obj_t* parent, int y_center, const lv_image_dsc_t* logo, const char* name, uint32_t color)
{
    DsRow row;
    row.cont = make_row_container(parent, y_center);
    lv_obj_t* cont = row.cont;

    lv_obj_t* icon = lv_image_create(cont);
    lv_image_set_src(icon, logo);
    lv_obj_align(icon, LV_ALIGN_TOP_LEFT, 0, 2);

    lv_obj_t* name_lbl = lv_label_create(cont);
    lv_label_set_text(name_lbl, name);
    lv_obj_set_style_text_font(name_lbl, &MontserratSemiBold26, 0);
    lv_obj_set_style_text_color(name_lbl, lv_color_hex(color), 0);
    lv_obj_align(name_lbl, LV_ALIGN_TOP_LEFT, 56, 8);

    // Big balance (right aligned): "110.00 CNY"
    row.bal = lv_label_create(cont);
    lv_obj_set_style_text_font(row.bal, &lv_font_maple_mono_medium_28, 0);
    lv_obj_set_style_text_color(row.bal, lv_color_hex(color), 0);
    lv_obj_align(row.bal, LV_ALIGN_TOP_RIGHT, 0, 6);

    return row;
}

// Pull one provider's window fields out of a parsed JSON object and refresh
// its row. Returns true if the 5h window just crossed the alert threshold up.
bool update_from_json(ProviderRow& row, cJSON* obj)
{
    if (!cJSON_IsObject(obj)) return false;
    int h = static_cast<int>(jdbl(obj, "h", 0));
    int d = static_cast<int>(jdbl(obj, "d", 0));
    int r = static_cast<int>(jdbl(obj, "r", 0));

    row.apply(h, d, r);
    bool crossed = (row.last_p5h >= 0 && row.last_p5h < kAlertThreshold && h >= kAlertThreshold);
    row.last_p5h = h;
    return crossed;
}

// Returns true if the DeepSeek low-balance alert fired.
bool update_ds_from_json(DsRow& row, cJSON* obj)
{
    if (!cJSON_IsObject(obj)) return false;
    const char* cur = jstr(obj, "cur", "CNY");
    double bal = jdbl(obj, "bal", 0);

    bool crossed = false;
    {
        LvglLockGuard lock;
        crossed = row.apply(cur, bal);
    }
    return crossed;
}

}  // namespace

AppCodex::AppCodex()
{
    setAppInfo().name = "CC Island";
    setAppInfo().icon = (void*)&icon_chatgpt;
}

void AppCodex::onCreate()
{
    mclog::tagInfo(getAppInfo().name, "on create");
}

void AppCodex::onOpen()
{
    mclog::tagInfo(getAppInfo().name, "on open");

    _key_manager = std::make_unique<input::KeyManager>();

    // Bring up BLE NUS (idempotent — only the first open actually starts it).
    ble_nus::start("CC Island");

    LvglLockGuard lock;

    // Full-screen black root; touch flips between the two provider pages.
    s_root = lv_obj_create(lv_screen_active());
    lv_obj_set_size(s_root, LV_PCT(100), LV_PCT(100));
    lv_obj_set_style_bg_color(s_root, lv_color_black(), 0);
    lv_obj_set_style_border_width(s_root, 0, 0);
    lv_obj_set_style_shadow_width(s_root, 0, 0);
    lv_obj_set_style_radius(s_root, 0, 0);
    lv_obj_set_style_pad_all(s_root, 0, 0);
    lv_obj_remove_flag(s_root, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_event_cb(s_root, on_screen_click, LV_EVENT_CLICKED, nullptr);

    auto make_page = [&]() {
        lv_obj_t* page = lv_obj_create(s_root);
        lv_obj_set_size(page, LV_PCT(100), LV_PCT(100));
        lv_obj_set_style_bg_opa(page, LV_OPA_TRANSP, 0);
        lv_obj_set_style_border_width(page, 0, 0);
        lv_obj_set_style_pad_all(page, 0, 0);
        lv_obj_remove_flag(page, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(page, LV_OBJ_FLAG_EVENT_BUBBLE);
        return page;
    };
    s_page1 = make_page();
    s_page2 = make_page();

    // Page 1: the original two window rows (unchanged layout).
    s_claude  = build_row(s_page1, -80, &logo_claude, "Claude", kClaudeColor);
    s_chatgpt = build_row(s_page1, 80, &logo_chatgpt, "ChatGPT", kChatgptColor);

    // Page 2: GLM window row + DeepSeek balance row.
    s_glm = build_row(s_page2, -80, &logo_glm, "GLM", kGlmColor);
    s_ds  = build_bal_row(s_page2, 80, &logo_deepseek, "DeepSeek", kDeepseekColor);

    // Dimmed "--" placeholders until each provider's first BLE push arrives.
    s_claude.show_placeholder();
    s_chatgpt.show_placeholder();
    s_glm.show_placeholder();
    s_ds.show_placeholder();
    show_page(s_page);
}

void AppCodex::onRunning()
{
    if (_key_manager) {
        input::KeyEvent ev = _key_manager->update();
        if (ev == input::KeyEvent::GoHome) {
            close();
            return;
        }
        // Blue button (G1) -> ask the bridge to push a fresh reading now.
        if (ev == input::KeyEvent::GoNext) {
            ble_nus::request_refresh();
            GetHAL().vibrate(60, 80);  // tactile "got it"
        }
    }

    // Apply the latest usage line pushed over BLE, if any. Rows whose key is
    // absent (provider unconfigured) keep their dimmed "--" placeholder.
    char line[256];
    if (ble_nus::poll_line(line, sizeof(line))) {
        cJSON* root = cJSON_Parse(line);
        if (root) {
            bool a = update_from_json(s_claude, cJSON_GetObjectItem(root, "c"));
            bool b = update_from_json(s_chatgpt, cJSON_GetObjectItem(root, "x"));
            bool g = update_from_json(s_glm, cJSON_GetObjectItem(root, "g"));
            bool d = update_ds_from_json(s_ds, cJSON_GetObjectItem(root, "ds"));
            cJSON_Delete(root);
            if (a || b || g || d) GetHAL().vibrate(250, 100);
        }
    }
}

void AppCodex::onClose()
{
    mclog::tagInfo(getAppInfo().name, "on close");

    _key_manager.reset();

    LvglLockGuard lock;
    if (s_root) {
        lv_obj_delete(s_root);
        s_root = nullptr;
    }
    s_page1 = nullptr;
    s_page2 = nullptr;
    // s_page intentionally survives: reopening the app shows the last page.
    s_claude = ProviderRow{};
    s_chatgpt = ProviderRow{};
    s_glm = ProviderRow{};
    s_ds = DsRow{};
}
