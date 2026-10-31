//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef LLAMA_WATERMARK_SAMPLER_H
#define LLAMA_WATERMARK_SAMPLER_H

#include "watermark_provider_abi.h"
#include <llama.h>
#include <cstdint>

llama_sampler* create_watermark_sampler(const GenieWatermarkProviderVTable* vtable,
                                        GenieWatermarkTokenHook*             hook,
                                        uint32_t                             n_vocab,
                                        uint64_t                             seed);

#endif
