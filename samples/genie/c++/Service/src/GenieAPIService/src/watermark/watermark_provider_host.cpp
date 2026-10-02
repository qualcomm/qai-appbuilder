//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "watermark_provider_host.h"
#include "log.h"

#include <mutex>
#include <cstdlib>
#include <string>

#ifdef _WIN32
#include <windows.h>
#endif

// ─────────────────────────────────────────────────────────────────────────────
// Anchor: address of this function is passed to GetModuleHandleExA so Windows
// can identify which loaded module (exe or dll) owns this translation unit.
// ─────────────────────────────────────────────────────────────────────────────
static void SentinelFn() {}

// ─────────────────────────────────────────────────────────────────────────────
// WatermarkProviderHost singleton
// ─────────────────────────────────────────────────────────────────────────────

static std::once_flag s_init_flag;

WatermarkProviderHost& WatermarkProviderHost::Instance()
{
    static WatermarkProviderHost s_instance;
    std::call_once(s_init_flag, []()
    {
        s_instance.Load();
    });
    return s_instance;
}

WatermarkProviderHost::WatermarkProviderHost() = default;

bool WatermarkProviderHost::HasTokenHook() const
{
    return has_token_hook_;
}

bool WatermarkProviderHost::HasTextHook() const
{
    return has_text_hook_;
}

GenieWatermarkTokenHook* WatermarkProviderHost::CreateTokenHook(uint32_t n_vocab,
                                                                uint64_t seed) const
{
    if (!has_token_hook_)
    {
        return nullptr;
    }
    return vtable_->token_hook_create(n_vocab, seed);
}

const GenieWatermarkProviderVTable* WatermarkProviderHost::GetVTable() const
{
    return vtable_;
}

// ─────────────────────────────────────────────────────────────────────────────
// Platform-specific loading
// ─────────────────────────────────────────────────────────────────────────────

#ifdef _WIN32

void WatermarkProviderHost::Load()
{
    const char* env = std::getenv("GENIE_WATERMARK_ENABLE");
    if (!env || env[0] == '\0' || (env[0] == '0' && env[1] == '\0'))
    {
        return;
    }

    // Locate the module (exe or dll) that owns this code by using the
    // address of a local function as an anchor.
    HMODULE hmod = nullptr;
    if (!GetModuleHandleExA(
            GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
            GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
            reinterpret_cast<LPCSTR>(&SentinelFn),
            &hmod) || !hmod)
    {
        My_Log{My_Log::Level::kWarning}
            << "[Watermark] GetModuleHandleExA failed, watermark plugin not loaded\n";
        return;
    }

    char module_path[MAX_PATH] = {};
    if (!GetModuleFileNameA(hmod, module_path, MAX_PATH))
    {
        My_Log{My_Log::Level::kWarning}
            << "[Watermark] GetModuleFileNameA failed, watermark plugin not loaded\n";
        return;
    }

    std::string plugin_path(module_path);
    auto sep = plugin_path.find_last_of("\\/");
    if (sep == std::string::npos)
    {
        My_Log{My_Log::Level::kWarning}
            << "[Watermark] Cannot determine module directory from \""
            << module_path << "\", watermark plugin not loaded\n";
        return;
    }
    plugin_path = plugin_path.substr(0, sep + 1) + "GenieWatermarkProvider.dll";

    HMODULE hplugin = LoadLibraryA(plugin_path.c_str());
    if (!hplugin)
    {
        My_Log{My_Log::Level::kWarning}
            << "[Watermark] LoadLibraryA(\"" << plugin_path
            << "\") failed (error=" << GetLastError()
            << "), watermark disabled\n";
        return;
    }

    using GetVTableFn = const GenieWatermarkProviderVTable* (*)();
    auto get_vtable_fn = reinterpret_cast<GetVTableFn>(
        GetProcAddress(hplugin, "GenieWatermarkProvider_GetVTable"));
    if (!get_vtable_fn)
    {
        My_Log{My_Log::Level::kWarning}
            << "[Watermark] GenieWatermarkProvider_GetVTable not exported by \""
            << plugin_path << "\", watermark disabled\n";
        FreeLibrary(hplugin);
        return;
    }

    const GenieWatermarkProviderVTable* vt = get_vtable_fn();
    if (!vt)
    {
        My_Log{My_Log::Level::kWarning}
            << "[Watermark] GenieWatermarkProvider_GetVTable returned NULL, watermark disabled\n";
        FreeLibrary(hplugin);
        return;
    }

    if (vt->struct_size != sizeof(GenieWatermarkProviderVTable))
    {
        My_Log{My_Log::Level::kWarning}
            << "[Watermark] VTable struct_size mismatch: plugin="
            << vt->struct_size << " host=" << sizeof(GenieWatermarkProviderVTable)
            << ", watermark disabled\n";
        FreeLibrary(hplugin);
        return;
    }

    if (vt->abi_version != GENIE_WATERMARK_ABI_VERSION)
    {
        My_Log{My_Log::Level::kWarning}
            << "[Watermark] ABI version mismatch: plugin="
            << vt->abi_version << " host=" << GENIE_WATERMARK_ABI_VERSION
            << ", watermark disabled\n";
        FreeLibrary(hplugin);
        return;
    }

    uint32_t validated_caps = vt->capabilities;

    if (validated_caps & GENIE_WM_CAP_TOKEN_HOOK)
    {
        if (!vt->token_hook_create || !vt->token_hook_apply ||
            !vt->token_hook_accept || !vt->token_hook_reset ||
            !vt->token_hook_free)
        {
            My_Log{My_Log::Level::kWarning}
                << "[Watermark] GENIE_WM_CAP_TOKEN_HOOK declared but one or more"
                   " required function pointers are NULL, capability disabled\n";
            validated_caps &= ~static_cast<uint32_t>(GENIE_WM_CAP_TOKEN_HOOK);
        }
    }

    if (validated_caps & GENIE_WM_CAP_TEXT_HOOK)
    {
        if (!vt->text_hook_apply || !vt->text_hook_free_string)
        {
            My_Log{My_Log::Level::kWarning}
                << "[Watermark] GENIE_WM_CAP_TEXT_HOOK declared but one or more"
                   " required function pointers are NULL, capability disabled\n";
            validated_caps &= ~static_cast<uint32_t>(GENIE_WM_CAP_TEXT_HOOK);
        }
    }

    // DLL is now adopted for the process lifetime; do not FreeLibrary.
    vtable_         = vt;
    has_token_hook_ = (validated_caps & GENIE_WM_CAP_TOKEN_HOOK) != 0;
    has_text_hook_  = (validated_caps & GENIE_WM_CAP_TEXT_HOOK)  != 0;
}

#else  // !_WIN32

void WatermarkProviderHost::Load()
{
    // Non-Windows: watermark plugin loading requires Win32 APIs; disabled.
}

#endif // _WIN32
