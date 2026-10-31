//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "llama_watermark_sampler.h"
#include <cstddef>
#include <vector>

namespace {

struct WatermarkSamplerData {
    const GenieWatermarkProviderVTable* vtable;
    GenieWatermarkTokenHook*             hook;
    uint32_t                             n_vocab;
    uint64_t                             seed;
};

static const char* wm_name(const llama_sampler * /*smpl*/)
{
    return "genie_watermark";
}

static void wm_accept(llama_sampler * smpl, llama_token token)
{
    auto* d = static_cast<WatermarkSamplerData*>(smpl->ctx);
    d->vtable->token_hook_accept(d->hook, static_cast<int32_t>(token));
}

static void wm_apply(llama_sampler * smpl, llama_token_data_array * cur_p)
{
    if (cur_p->size == 0) { return; }
    auto* d = static_cast<WatermarkSamplerData*>(smpl->ctx);
    const size_t n = cur_p->size;
    std::vector<int32_t> ids(n);
    std::vector<float>   logits(n);
    for (size_t i = 0; i < n; ++i) {
        ids[i]    = static_cast<int32_t>(cur_p->data[i].id);
        logits[i] = cur_p->data[i].logit;
    }
    d->vtable->token_hook_apply(d->hook, logits.data(), ids.data(), static_cast<int32_t>(n));
    for (size_t i = 0; i < n; ++i) {
        cur_p->data[i].logit = logits[i];
    }
    cur_p->sorted = false;
}

static llama_sampler * wm_clone(const llama_sampler * smpl);
static void wm_reset(llama_sampler * smpl);
static void wm_free(llama_sampler * smpl);

static llama_sampler_i kWatermarkSamplerIface = {
    wm_name,
    wm_accept,
    wm_apply,
    wm_reset,
    wm_clone,
    wm_free,
    nullptr,
    nullptr,
    nullptr,
    nullptr,
};

static llama_sampler * wm_clone(const llama_sampler * smpl)
{

    const auto* d = static_cast<const WatermarkSamplerData*>(smpl->ctx);
    auto* new_data = new WatermarkSamplerData{
        d->vtable,
        d->vtable->token_hook_create(d->n_vocab, d->seed),
        d->n_vocab,
        d->seed
    };
    return llama_sampler_init(&kWatermarkSamplerIface, static_cast<llama_sampler_context_t>(new_data));
}

static void wm_reset(llama_sampler * smpl)
{
    auto* d = static_cast<WatermarkSamplerData*>(smpl->ctx);
    d->vtable->token_hook_reset(d->hook);
}

static void wm_free(llama_sampler * smpl)
{
    auto* d = static_cast<WatermarkSamplerData*>(smpl->ctx);
    if (d) {
        d->vtable->token_hook_free(d->hook);
        delete d;
    }
}

}

llama_sampler* create_watermark_sampler(const GenieWatermarkProviderVTable* vtable,
                                        GenieWatermarkTokenHook*             hook,
                                        uint32_t                             n_vocab,
                                        uint64_t                             seed)
{
    auto* d = new WatermarkSamplerData{vtable, hook, n_vocab, seed};
    return llama_sampler_init(&kWatermarkSamplerIface, static_cast<llama_sampler_context_t>(d));
}
