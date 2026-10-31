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

#include "llama_runtime_planner.h"

using json = nlohmann::ordered_json;

namespace llama_speculative
{

struct DraftModelConfig
{
    bool present = false;
    std::string path;
    std::string spec_type;
    int spec_draft_n_max = 0;
    double spec_draft_p_min = 0.0;
    bool required = true;
    std::string flash_attn;
    std::string draft_flash_attn;
    std::string samplers;
    float temp = 0.0f;
    int top_k = 1;
    float top_p = -1.0f;
    int ngram_mod_n_min = -1;
    int ngram_mod_n_match = 16;
    bool ngram_mod = true;
    int n_gpu_layers = 99;
    int fit_target_mib = -1;
    int n_threads = -1;
    int n_threads_batch = -1;
    int ffn_cpu_layers = -1;
    int max_alloc_mib = -1;
    int n_ubatch = -1;
    int n_batch = -1;
    std::string planner;

    static DraftModelConfig ParseFrom(const json &model_config_json);
};

uint64_t EstimateSpeculativeMemoryRequirement(const std::string &model_dir,
                                               const std::string &draft_relative_path,
                                               uint64_t context_size);

uint64_t GetAvailablePhysicalMemoryBytes();

uint64_t GetTotalPhysicalMemoryBytes();

uint64_t ResolveContextSizeByPhysicalMemory();

bool SpeculativeFitsFullGpu(const std::string &model_dir, const std::string &draft_relative_path,
                            uint64_t context_size, uint64_t *required_out, uint64_t *available_out);

llama_runtime::RuntimePlan PlanSpeculativeRuntime(const std::string &model_dir,
                                                  const DraftModelConfig &cfg,
                                                  uint64_t context_size);

uint32_t ReadGgufBlockCount(const std::string &gguf_path);

constexpr uint32_t kDflashFfnCpuLayersDefault = 30;

std::string BuildDflashFfnCpuOverride(uint32_t block_count, uint32_t cpu_layers);

}

#endif
