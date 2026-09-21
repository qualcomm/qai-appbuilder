//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef WATERMARK_PROVIDER_HOST_H
#define WATERMARK_PROVIDER_HOST_H

#include "watermark_provider_abi.h"
#include <cstdint>

// ─────────────────────────────────────────────────────────────────────────────
// WatermarkProviderHost
//
// Process-lifetime lazy-loading singleton.  On first access it reads the
// GENIE_WATERMARK_ENABLE environment variable:
//
//   • unset / empty / exactly "0"  → disabled; all Has*() return false.
//   • any other non-empty value    → enabled; attempts to locate and load
//                                    GenieWatermarkProvider.dll from the same
//                                    directory as the module that owns this
//                                    translation unit (either GenieAPIService.exe
//                                    or GenieAPILibrary.dll).
//
// Any loading or validation failure silently falls back to the null
// implementation (Has*() return false) and logs a single WARNING.
//
// After the first call the vtable pointer is read-only for the process
// lifetime; no locking is required for HasTokenHook() / HasTextHook() /
// GetVTable() / CreateTokenHook().
// ─────────────────────────────────────────────────────────────────────────────
class WatermarkProviderHost
{
public:
    static WatermarkProviderHost& Instance();

    // Returns true when the loaded plugin declared GENIE_WM_CAP_TOKEN_HOOK
    // and all five required function pointers were validated non-NULL.
    bool HasTokenHook() const;

    // Returns true when the loaded plugin declared GENIE_WM_CAP_TEXT_HOOK
    // and both required function pointers were validated non-NULL.
    bool HasTextHook() const;

    // Factory: allocate per-model-instance token hook state.
    // Returns nullptr when HasTokenHook() is false.
    // Caller must eventually pass the returned pointer to vtable->token_hook_free.
    GenieWatermarkTokenHook* CreateTokenHook(uint32_t n_vocab, uint64_t seed) const;

    // Direct VTable access for callers that need to invoke individual
    // function pointers (e.g. llama_cpp.cpp adapter).
    // Returns nullptr when no plugin was loaded.
    const GenieWatermarkProviderVTable* GetVTable() const;

private:
    WatermarkProviderHost();
    ~WatermarkProviderHost() = default;
    WatermarkProviderHost(const WatermarkProviderHost&) = delete;
    WatermarkProviderHost& operator=(const WatermarkProviderHost&) = delete;

    void Load();

    const GenieWatermarkProviderVTable* vtable_{nullptr};
    bool has_token_hook_{false};
    bool has_text_hook_{false};
};

#endif // WATERMARK_PROVIDER_HOST_H
