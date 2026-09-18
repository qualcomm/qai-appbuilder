//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================
//
// NOTE: this is a self-contained copy of src/common/watermark_provider_abi.h
// from the GenieAPIService repository, shipped alongside this independent
// plugin project so it can be built without checking out the full repo.
// Keep it in sync with the original if the ABI ever changes.
//
//==============================================================================

#ifndef WATERMARK_PROVIDER_ABI_H
#define WATERMARK_PROVIDER_ABI_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// ─────────────────────────────────────────────────────────────────────────────
// ABI version sentinel — bump when the VTable layout changes incompatibly.
// ─────────────────────────────────────────────────────────────────────────────
#define GENIE_WATERMARK_ABI_VERSION 1

// ─────────────────────────────────────────────────────────────────────────────
// Capability bits.  A plugin sets the corresponding bit and guarantees all
// required function pointers for that capability are non-NULL.
// ─────────────────────────────────────────────────────────────────────────────
typedef enum
{
    GENIE_WM_CAP_NONE       = 0,
    GENIE_WM_CAP_TOKEN_HOOK = 1u << 0,  // per-token logit scoring (GGUF backend)
    GENIE_WM_CAP_TEXT_HOOK  = 1u << 1,  // post-generation text rewrite
} GenieWatermarkCapabilities;

// ─────────────────────────────────────────────────────────────────────────────
// Opaque per-model-instance state owned by the plugin.
// ─────────────────────────────────────────────────────────────────────────────
typedef struct GenieWatermarkTokenHook GenieWatermarkTokenHook;

// ─────────────────────────────────────────────────────────────────────────────
// VTable exported by the plugin DLL.
//
// Layout rules (forward-compat COM pattern):
//   struct_size  — must equal sizeof(GenieWatermarkProviderVTable) as compiled
//                  by the *plugin*; host rejects mismatches.
//   abi_version  — must equal GENIE_WATERMARK_ABI_VERSION; host rejects mismatches.
//   capabilities — bit mask of GenieWatermarkCapabilities the plugin implements.
//
// For each set capability bit the host verifies that every required function
// pointer listed below is non-NULL; if any is NULL the capability bit is
// silently cleared and a WARNING is logged.
// ─────────────────────────────────────────────────────────────────────────────
typedef struct
{
    // Forward-compatibility guard — must be the very first field.
    size_t   struct_size;
    int      abi_version;
    uint32_t capabilities;  // GenieWatermarkCapabilities bitmask

    // ── token-level hook (GENIE_WM_CAP_TOKEN_HOOK) ──────────────────────────
    // All five pointers must be non-NULL when the capability bit is set.

    // Create per-model-instance state.
    //   n_vocab  vocabulary size of the model being loaded
    //   seed     deterministic seed (use 0 for the HF public-key default)
    GenieWatermarkTokenHook* (*token_hook_create)(uint32_t n_vocab, uint64_t seed);

    // Called once per sampling step, *before* a token is chosen.
    //   logits   parallel array of logit values (length n); modify in-place
    //   ids      parallel array of candidate token ids (length n); read-only
    //   n        number of candidates
    void (*token_hook_apply)(GenieWatermarkTokenHook* hook,
                             float* logits, const int32_t* ids, int32_t n);

    // Notify the hook that token accepted_token_id was committed.
    void (*token_hook_accept)(GenieWatermarkTokenHook* hook,
                              int32_t accepted_token_id);

    // Reset internal state (e.g. start of a new generation sequence).
    void (*token_hook_reset)(GenieWatermarkTokenHook* hook);

    // Destroy the hook instance created by token_hook_create.
    void (*token_hook_free)(GenieWatermarkTokenHook* hook);

    // ── text-level hook (GENIE_WM_CAP_TEXT_HOOK) ─────────────────────────────
    // Both pointers must be non-NULL when the capability bit is set.

    // Rewrite the finished generation text.
    //   input_utf8  NUL-terminated UTF-8 string
    //   returns     a newly-allocated NUL-terminated UTF-8 string (caller must
    //               release via text_hook_free_string)
    char* (*text_hook_apply)(const char* input_utf8);

    // Release a string returned by text_hook_apply.
    void  (*text_hook_free_string)(char* s);

} GenieWatermarkProviderVTable;

// ─────────────────────────────────────────────────────────────────────────────
// The single symbol the plugin DLL must export.
// Returns a pointer to a statically-allocated (process-lifetime) VTable, or
// NULL on internal initialisation failure.
// ─────────────────────────────────────────────────────────────────────────────
#ifdef _WIN32
#ifdef GENIE_WATERMARK_PLUGIN_IMPL
// Used by the plugin itself when compiling the DLL.
__declspec(dllexport) const GenieWatermarkProviderVTable* GenieWatermarkProvider_GetVTable(void);
#else
// Used by the host (GenieAPIService / GenieAPILibrary) — imported via GetProcAddress,
// so no dllimport decoration needed here; the declaration is kept for documentation.
const GenieWatermarkProviderVTable* GenieWatermarkProvider_GetVTable(void);
#endif
#else
const GenieWatermarkProviderVTable* GenieWatermarkProvider_GetVTable(void);
#endif

#ifdef __cplusplus
} // extern "C"
#endif

#endif // WATERMARK_PROVIDER_ABI_H
