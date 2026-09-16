//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef LLAMA_SPECULATIVE_CONFIG_H
#define LLAMA_SPECULATIVE_CONFIG_H

#include <cstdint>
#include <string>
#include <nlohmann/json.hpp>

using json = nlohmann::ordered_json;

// GGUF/llama.cpp 专属：投机解码（speculative decoding，Qwen3.8-27B + DFlash2）相关的 config.json
// 字段解析、硬件门槛判定与系统内存查询。原先误放在通用目录 src/model/（DraftModelConfig）与
// MNNContext（GetTotalPhysicalMemoryBytes），但这些概念只对 llama.cpp fork 的投机解码 API 有
// 意义，且只被 LLAMACppBuilder（llama_cpp.cpp）与 GGUFVerify（model_manager.cpp）使用，QNN/MNN
// 后端完全不涉及，故整体归入本目录（与 llama_cpp.h/.cpp 同级），用命名空间显式标注其专属归属。
namespace llama_speculative
{

// GGUF 模型 config.json 中可选的 draft_model 字段块：声明该模型自带投机解码（speculative
// decoding）drafter 模型。只有该字段块存在时才触发投机解码路径（GGUFVerify/LLAMACppBuilder），
// 不含该字段的模型（现有全部 GGUF 模型）完全不受影响。
//
// 字段解析被设计为不依赖 Windows API/真实硬件查询的纯函数（ParseFrom 只接受一个已解析好的
// json 对象），便于脱离 MSVC/远程 ARM64 环境、用任意本地 C++ 编译器独立单元验证。
struct DraftModelConfig
{
    bool present = false;              // config.json 是否声明了 draft_model 字段块
    std::string path;                  // drafter 权重文件路径（相对模型目录，如 "Qwen3.8-27B-DFlash2-Q4_K_M.gguf"）
    std::string spec_type;             // 投机解码类型，如 "draft-dflash"
    int spec_draft_n_max = 0;          // 每轮投机验证最多产出的 draft token 数（对应 --spec-draft-n-max）
    double spec_draft_p_min = 0.0;     // 投机接受概率下限（对应 --spec-draft-p-min）
    bool required = true;              // drafter 加载失败时是否导致整体模型加载失败（默认 true，不静默降级）

    // 从模型 config.json 解析出的 json 对象中提取 draft_model 字段块。
    // 不含该字段块、字段类型不匹配、或传入的 json 本身为空/非 object 时，均返回 present=false
    // 的默认实例，不抛异常——与 GGUFVerify 现有对 "backend" 字段的宽松解析风格一致。
    static DraftModelConfig ParseFrom(const json &model_config_json);
};

// 投机解码模型（Qwen3.8-27B + DFlash2）加载所需的最低系统总物理内存门槛。
// 阈值定为 60GB 而不是标称的 64GB：远程实测的 64GB 标称硬件在 Windows 上只上报约 63.43GB
// （固件/GPU 预留导致 GlobalMemoryStatusEx 的 ullTotalPhys 略低于标称值），60GB 留出约 4GB
// 容差，同时足以排除 32GB/48GB 机器。
constexpr uint64_t kDraftModelMinPhysicalMemoryBytes = 60ULL * 1024 * 1024 * 1024;

// 纯函数：判断总物理内存字节数是否达到给定阈值。刻意与"调用 GlobalMemoryStatusEx 拿到真实
// 硬件值"这一步（GetTotalPhysicalMemoryBytes()）分离，使得"内存不足应拒绝加载"这条路径可以
// 通过注入任意测试值独立验证，不依赖能自然复现内存不足场景的真实硬件。
inline bool MeetsPhysicalMemoryThreshold(uint64_t total_physical_bytes, uint64_t threshold_bytes)
{
    return total_physical_bytes >= threshold_bytes;
}

// 系统总物理内存字节数（与 MNNContext::GetAvailablePhysicalMemoryBytes 同一 GlobalMemoryStatusEx
// 模式，取 ullTotalPhys 而非 ullAvailPhys）。用于投机解码（draft_model）模型加载前的硬件门槛
// 检查，见 GGUFVerify::CreateIfVerifiedImpl()（model_manager.cpp）。原定义于 MNNContext，迁出
// 是因为该方法只被本文件的投机解码硬件门槛检查使用，继续放在 MNN 后端类里属反向依赖。
uint64_t GetTotalPhysicalMemoryBytes();

} // namespace llama_speculative

#endif //LLAMA_SPECULATIVE_CONFIG_H
