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

class WatermarkProviderHost
{
public:
    static WatermarkProviderHost& Instance();

    bool HasTokenHook() const;

    bool HasTextHook() const;

    GenieWatermarkTokenHook* CreateTokenHook(uint32_t n_vocab, uint64_t seed) const;

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

#endif
