//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef LLAMA_PLACEMENT_H
#define LLAMA_PLACEMENT_H

#include <algorithm>
#include <cstdint>
#include <string>
#include <vector>

namespace llama_placement
{

using Tier = std::vector<std::string>;

constexpr uint64_t kUnifiedMemHeadroom = 4ULL * 1024 * 1024 * 1024;

struct Inputs
{
    std::string primary;
    bool htp_available = false;
    bool npu_slot_free = true;
    bool has_draft = false;
    uint64_t need_bytes = 0;
    uint64_t gpu_budget = 0;
    uint64_t npu_budget = 0;
    uint64_t avail_phys_bytes = 0;
};

inline Tier SpillChain(const Inputs &in)
{
    if (in.primary == "cpu")
    {
        return {"cpu"};
    }
    Tier chain;
    if (in.primary == "npu" && in.htp_available)
    {
        chain = {"npu", "gpu"};
    }
    else
    {
        chain = {"gpu"};
        if (in.htp_available && in.npu_slot_free && !in.has_draft)
        {
            chain.emplace_back("npu");
        }
    }
    chain.emplace_back("cpu");
    return chain;
}

inline uint64_t WithMargin(uint64_t budget)
{
    constexpr uint64_t kDeviceMarginBytes = 512ULL * 1024 * 1024;
    return budget > kDeviceMarginBytes ? budget - kDeviceMarginBytes : 0;
}

inline uint64_t BudgetOf(const Inputs &in, const std::string &device)
{
    return device == "gpu" ? in.gpu_budget : (device == "npu" ? in.npu_budget : 0);
}

inline bool KnownTooSmall(const Inputs &in, const Tier &accelerators)
{
    if (in.need_bytes == 0)
    {
        return false;
    }
    uint64_t capacity = 0;
    for (const auto &device: accelerators)
    {
        const uint64_t budget = BudgetOf(in, device);
        if (budget == 0)
        {
            return false;
        }
        capacity += budget;
    }
    return in.need_bytes > capacity;
}

inline bool UnifiedMemoryTooSmall(const Inputs &in)
{
    if (in.avail_phys_bytes == 0 || in.need_bytes == 0)
    {
        return false;
    }
    return in.avail_phys_bytes <= kUnifiedMemHeadroom ||
           in.need_bytes > in.avail_phys_bytes - kUnifiedMemHeadroom;
}

inline bool SharesUnifiedMemory(const Tier &tier)
{
    return tier.size() > 1 && std::find(tier.begin(), tier.end(), "npu") != tier.end();
}

inline std::vector<Tier> PlanTiers(const Inputs &in, std::vector<Tier> *dropped = nullptr)
{
    std::vector<Tier> tiers;
    const auto add = [&tiers](const Tier &tier)
    {
        if (!tier.empty() && std::find(tiers.begin(), tiers.end(), tier) == tiers.end())
        {
            tiers.push_back(tier);
        }
    };

    const Tier chain = SpillChain(in);
    const Tier accelerators(chain.begin(), chain.end() - 1);
    const bool uses_npu = std::find(accelerators.begin(), accelerators.end(), "npu") != accelerators.end();

    for (size_t n = 1; n <= accelerators.size(); ++n)
    {
        const Tier prefix(accelerators.begin(), accelerators.begin() + n);
        if (!KnownTooSmall(in, prefix))
        {
            add(prefix);
        }
    }
    if (!accelerators.empty() && uses_npu && KnownTooSmall(in, accelerators))
    {
        Tier with_cpu = accelerators;
        with_cpu.emplace_back("cpu");
        add(with_cpu);
    }
    if (uses_npu)
    {
        if (!KnownTooSmall(in, {"gpu"}))
        {
            add({"gpu"});
        }
        add({"gpu", "cpu"});
    }
    else if (!accelerators.empty())
    {
        Tier with_cpu = accelerators;
        with_cpu.emplace_back("cpu");
        add(with_cpu);
    }
    add({"cpu"});

    if (UnifiedMemoryTooSmall(in))
    {
        std::vector<Tier> kept;
        for (const auto &tier: tiers)
        {
            if (!SharesUnifiedMemory(tier))
            {
                kept.push_back(tier);
            }
            else if (dropped)
            {
                dropped->push_back(tier);
            }
        }
        tiers.swap(kept);
    }
    return tiers;
}

struct Split
{
    int n_gpu_layers = 99;
    std::vector<uint64_t> shares;
};

inline Split ComputeSplit(const std::vector<uint64_t> &budgets, uint64_t need, uint32_t n_layers,
                          bool cpu_share = false)
{
    Split split;
    split.shares.assign(budgets.size(), 0);
    if (budgets.empty() || need == 0)
    {
        return split;
    }
    uint64_t remaining = need;
    for (size_t i = 0; i < budgets.size(); ++i)
    {
        split.shares[i] = std::min(budgets[i], remaining);
        remaining -= split.shares[i];
    }
    if (remaining > 0)
    {
        const double placed = static_cast<double>(need - remaining) / static_cast<double>(need);
        const int layers = static_cast<int>(placed * static_cast<double>(n_layers + 1));
        split.n_gpu_layers = std::max(1, std::min(layers, static_cast<int>(n_layers)));
    }
    else if (cpu_share && n_layers > 1)
    {
        split.n_gpu_layers = std::max(1, static_cast<int>(n_layers * 9 / 10));
        for (auto &share: split.shares)
        {
            share -= share / 10;
        }
    }
    else if (budgets.size() >= 2 && split.shares.back() == 0)
    {
        const uint64_t moved = need / 10;
        split.shares.front() -= moved;
        split.shares.back() = moved;
    }
    return split;
}

}

#endif
