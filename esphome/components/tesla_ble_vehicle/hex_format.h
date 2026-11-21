// SPDX-License-Identifier: AGPL-3.0-only
#pragma once

// ESPHome 2026.11 removed the legacy format_hex declarations from helpers.h.
#if __has_include(<esphome/core/alloc_helpers.h>)
#include <esphome/core/alloc_helpers.h>
#else
#include <esphome/core/helpers.h>
#endif
