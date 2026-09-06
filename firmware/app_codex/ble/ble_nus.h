/*
 * SPDX-FileCopyrightText: 2026 wangjiacheng
 *
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include <stdint.h>

// Bump whenever this component's GATT layout changes: the persisted BLE
// address is rotated so Windows does not serve a stale cached database.
// Version 2 also migrates devices that were paired while bonds were RAM-only.
// Those Windows records can never reconnect after the watch loses the key, so
// the first bond-persistent firmware must rotate the identity once.
static constexpr uint8_t kGattDbVersion = 2;

namespace ble_nus {

// Bring up the NimBLE host once and start advertising the Nordic UART Service
// under the given device name. Idempotent — safe to call on every app open.
void start(const char* device_name);

// If a complete '\n'-terminated line has arrived since the last call, copy it
// (without the newline) into `out` and return true. Otherwise return false.
bool poll_line(char* out, int out_size);

// Notify the connected central (the Mac bridge) to push a fresh reading now.
// No-op if nothing is connected.
void request_refresh();

}  // namespace ble_nus
