//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef SHAPE_5D_H
#define SHAPE_5D_H

#include "base.h"

// Permute a Shape_5D according to runtime permutation perm.
// perm must be length 5 and contain a permutation of {0,1,2,3,4}.
// perm[j] == old_axis_index for new axis j (same semantics as PyTorch permute).
Shape_5D<float> permute_shape5d(const Shape_5D<float> &in, const std::array<int, 5> &perm)
{
    // Validate perm is a permutation of 0..4
    std::array<int, 5> check = perm;
    std::sort(check.begin(), check.end());
    for (int i = 0; i < 5; ++i)
    {
        if (check[i] != i)
            throw std::invalid_argument("perm must be a permutation of 0..4");
    }

    // Helper to read axis dim from Shape_5D
    auto dim = [&](const Shape_5D<float> &s, int axis) -> int
    {
        switch (axis)
        {
            case 0:
                return s.d0;
            case 1:
                return s.d1;
            case 2:
                return s.d2;
            case 3:
                return s.d3;
            case 4:
                return s.d4;
            default:
                throw std::out_of_range("axis");
        }
    };

    // Build output dims: out.dj = in.d[perm[j]]
    Shape_5D<float> out;
    out.d0 = dim(in, perm[0]);
    out.d1 = dim(in, perm[1]);
    out.d2 = dim(in, perm[2]);
    out.d3 = dim(in, perm[3]);
    out.d4 = dim(in, perm[4]);

    // Resize output buffer
    const size_t out_size = static_cast<size_t>(out.d0) * out.d1 * out.d2 * out.d3 * out.d4;
    out.buf.resize(out_size);

    // Compute inverse permutation inv[old_axis] = new_axis
    std::array<int, 5> inv;
    for (int new_ax = 0; new_ax < 5; ++new_ax)
        inv[perm[new_ax]] = new_ax;

    // Index helpers (row-major)
    auto idx_in = [&](int i0, int i1, int i2, int i3, int i4) -> size_t
    {
        return static_cast<size_t>(
                ((((i0 * in.d1 + i1) * in.d2 + i2) * in.d3 + i3) * in.d4) + i4
        );
    };

    auto idx_out = [&](int i0, int i1, int i2, int i3, int i4) -> size_t
    {
        return static_cast<size_t>(
                ((((i0 * out.d1 + i1) * out.d2 + i2) * out.d3 + i3) * out.d4) + i4
        );
    };

    // Iterate over output indices and read from input using inverse mapping:
    // For each output index out_idx[0..4], source index src[k] = out_idx[ inv[k] ].
    for (int o0 = 0; o0 < out.d0; ++o0)
    {
        for (int o1 = 0; o1 < out.d1; ++o1)
        {
            for (int o2 = 0; o2 < out.d2; ++o2)
            {
                for (int o3 = 0; o3 < out.d3; ++o3)
                {
                    for (int o4 = 0; o4 < out.d4; ++o4)
                    {
                        int out_idx[5] = {o0, o1, o2, o3, o4};
                        int s0 = out_idx[inv[0]];
                        int s1 = out_idx[inv[1]];
                        int s2 = out_idx[inv[2]];
                        int s3 = out_idx[inv[3]];
                        int s4 = out_idx[inv[4]];
                        size_t s = idx_in(s0, s1, s2, s3, s4);
                        size_t d = idx_out(o0, o1, o2, o3, o4);
                        out.buf[d] = in.buf[s];
                    }
                }
            }
        }
    }

    return out;
}

// Reshape contiguous Shape_5D -> contiguous Shape_2D
// Allows exactly one of out_d0/out_d1 to be -1 (to infer that dimension).
Shape_2D<float> reshape_5d_to_2d(
        const Shape_5D<float> &in,
        int out_d0,
        int out_d1
)
{
    // Validate input dims
    if (in.d0 <= 0 || in.d1 <= 0 || in.d2 <= 0 || in.d3 <= 0 || in.d4 <= 0)
        throw std::invalid_argument("input dims must be > 0");

    // Count how many -1s
    int neg1_count = 0;
    neg1_count += (out_d0 == -1);
    neg1_count += (out_d1 == -1);
    if (neg1_count > 1)
        throw std::invalid_argument("only one -1 allowed");

    // Validate provided positive dims (allow exactly one -1)
    auto check_positive_or_neg1 = [](int v, const char *name)
    {
        if (v == 0 || v < -1)
            throw std::invalid_argument(std::string(name) + " must be > 0 or -1");
    };
    check_positive_or_neg1(out_d0, "out_d0");
    check_positive_or_neg1(out_d1, "out_d1");

    // Compute input total
    long long in_total = static_cast<long long>(in.d0) * in.d1 * in.d2 * in.d3 * in.d4;

    // If one dim is -1, infer it
    if (neg1_count == 1)
    {
        long long prod = 1;
        if (out_d0 != -1)
            prod *= static_cast<long long>(out_d0);
        if (out_d1 != -1)
            prod *= static_cast<long long>(out_d1);

        if (prod == 0)
            throw std::invalid_argument("invalid provided dims (zero product)");

        if (in_total % prod != 0)
            throw std::invalid_argument("cannot infer dimension: sizes not divisible");

        long long inferred = in_total / prod;
        if (inferred > static_cast<long long>(std::numeric_limits<int>::max()))
            throw std::overflow_error("inferred dimension too large");

        if (out_d0 == -1)
            out_d0 = static_cast<int>(inferred);
        else
            out_d1 = static_cast<int>(inferred);
    }

    // Now all dims must be positive
    if (out_d0 <= 0 || out_d1 <= 0)
        throw std::invalid_argument("output dims must be > 0");

    // Final total check
    long long out_total = static_cast<long long>(out_d0) * out_d1;
    if (in_total != out_total)
        throw std::invalid_argument("total number of elements must match for reshape");

    // Prepare output
    Shape_2D<float> out;
    out.d0 = out_d0;
    out.d1 = out_d1;
    out.buf.resize(static_cast<size_t>(out_total));

    // Copy linear buffer (contiguous row-major)
    std::copy(in.buf.begin(), in.buf.end(), out.buf.begin());

    return out;
}

#endif //SHAPE_5D_H
