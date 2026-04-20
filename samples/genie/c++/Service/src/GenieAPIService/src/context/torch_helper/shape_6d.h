//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef SHAPE_6D_H
#define SHAPE_6D_H

#include "base.h"
#include <log.h>

template<typename T>
void Shape_6D<T>::Shape()
{
    My_Log{} << "[" << d0 << ", " << d1 << ", "
             << d2 << ", " << d3 << ", "
             << d4 << ", " << d5 << "]";
}

// Reshape in: (d0, d1, d2, d3, d4, d5) -> out: (out_d0, out_d1, out_d2)
// Use out_d1 = -1 to compute it as product(d1..d4).
Shape_3D<float> reshape_6d_to_3d(
        const Shape_6D<float> &in,
        int out_d0,   // expected to equal in.d0 (B_)
        int out_d1,   // -1 or desired flattened dim
        int out_d2    // comp_dim, expected to equal in.d5
)
{
    // Basic validation
    if (in.d0 <= 0 || in.d1 <= 0 || in.d2 <= 0 || in.d3 <= 0 || in.d4 <= 0 || in.d5 <= 0)
    {
        throw std::invalid_argument("all input dims must be > 0");
    }
    if (out_d0 <= 0)
        throw std::invalid_argument("out_d0 must be > 0");
    if (out_d2 <= 0)
        throw std::invalid_argument("out_d2 must be > 0");

    if (out_d0 != in.d0)
        throw std::invalid_argument("out_d0 must equal in.d0");

    // compute flattened dim if requested
    long long flat = static_cast<long long>(in.d1) * in.d2 * in.d3 * in.d4;
    if (out_d1 == -1)
    {
        if (flat > static_cast<long long>(std::numeric_limits<int>::max()))
            throw std::overflow_error("flattened dimension too large");
        out_d1 = static_cast<int>(flat);
    }
    else
    {
        if (static_cast<long long>(out_d1) != flat)
        {
            throw std::invalid_argument("provided out_d1 does not match product of in.d1..in.d4");
        }
    }

    if (out_d2 != in.d5)
        throw std::invalid_argument("out_d2 (comp_dim) must equal in.d5");

    // Prepare output Shape_3D: (out_d0, out_d1, out_d2)
    Shape_3D<float> out;
    out.d0 = out_d0;
    out.d1 = out_d1;
    out.d2 = out_d2;
    out.buf.resize(static_cast<size_t>(out.d0) * out.d1 * out.d2);

    // Index helpers (row-major)
    auto idx6 = [&](int i0, int i1, int i2, int i3, int i4, int i5) -> size_t
    {
        return static_cast<size_t>(
                (((((i0 * in.d1 + i1) * in.d2 + i2) * in.d3 + i3) * in.d4 + i4) * in.d5) + i5
        );
    };
    auto idx3 = [&](int i0, int i1, int i2) -> size_t
    {
        return static_cast<size_t>(((i0 * out.d1 + i1) * out.d2) + i2);
    };

    // Flatten axes (d1,d2,d3,d4) in row-major order:
    // flattened_index = (((i1 * d2 + i2) * d3 + i3) * d4 + i4)
    for (int b = 0; b < in.d0; ++b)
    {
        for (int i1 = 0; i1 < in.d1; ++i1)
        {
            for (int i2 = 0; i2 < in.d2; ++i2)
            {
                for (int i3 = 0; i3 < in.d3; ++i3)
                {
                    for (int i4 = 0; i4 < in.d4; ++i4)
                    {
                        int flat_idx = (((i1 * in.d2 + i2) * in.d3 + i3) * in.d4) + i4;
                        for (int c = 0; c < in.d5; ++c)
                        {
                            size_t s = idx6(b, i1, i2, i3, i4, c);
                            size_t d = idx3(b, flat_idx, c);
                            out.buf[d] = in.buf[s];
                        }
                    }
                }
            }
        }
    }

    return out;
}

// Reshape contiguous Shape_6D -> contiguous Shape_4D
// Allows exactly one of out_d0/out_d1/out_d2/out_d3 to be -1 (to infer that dimension).
Shape_4D<float> reshape_6d_to_4d(
        const Shape_6D<float> &in,
        int out_d0,
        int out_d1,
        int out_d2,
        int out_d3
)
{
    // Validate input dims
    if (in.d0 <= 0 || in.d1 <= 0 || in.d2 <= 0 || in.d3 <= 0 || in.d4 <= 0 || in.d5 <= 0)
        throw std::invalid_argument("input dims must be > 0");

    // Count how many -1s
    int neg1_count = 0;
    neg1_count += (out_d0 == -1);
    neg1_count += (out_d1 == -1);
    neg1_count += (out_d2 == -1);
    neg1_count += (out_d3 == -1);
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
    check_positive_or_neg1(out_d2, "out_d2");
    check_positive_or_neg1(out_d3, "out_d3");

    // Compute input total
    long long in_total = static_cast<long long>(in.d0) * in.d1 * in.d2 * in.d3 * in.d4 * in.d5;

    // If one dim is -1, infer it
    if (neg1_count == 1)
    {
        long long prod = 1;
        if (out_d0 != -1)
            prod *= static_cast<long long>(out_d0);
        if (out_d1 != -1)
            prod *= static_cast<long long>(out_d1);
        if (out_d2 != -1)
            prod *= static_cast<long long>(out_d2);
        if (out_d3 != -1)
            prod *= static_cast<long long>(out_d3);

        if (prod == 0)
            throw std::invalid_argument("invalid provided dims (zero product)");

        if (in_total % prod != 0)
            throw std::invalid_argument("cannot infer dimension: sizes not divisible");

        long long inferred = in_total / prod;
        if (inferred > static_cast<long long>(std::numeric_limits<int>::max()))
            throw std::overflow_error("inferred dimension too large");

        if (out_d0 == -1)
            out_d0 = static_cast<int>(inferred);
        else if (out_d1 == -1)
            out_d1 = static_cast<int>(inferred);
        else if (out_d2 == -1)
            out_d2 = static_cast<int>(inferred);
        else
            out_d3 = static_cast<int>(inferred);
    }

    // Now all dims must be positive
    if (out_d0 <= 0 || out_d1 <= 0 || out_d2 <= 0 || out_d3 <= 0)
        throw std::invalid_argument("output dims must be > 0");

    // Final total check
    long long out_total = static_cast<long long>(out_d0) * out_d1 * out_d2 * out_d3;
    if (in_total != out_total)
        throw std::invalid_argument("total number of elements must match for reshape");

    // Prepare output
    Shape_4D<float> out;
    out.d0 = out_d0;
    out.d1 = out_d1;
    out.d2 = out_d2;
    out.d3 = out_d3;
    out.buf.resize(static_cast<size_t>(out_total));

    // Copy linear buffer (contiguous row-major)
    std::copy(in.buf.begin(), in.buf.end(), out.buf.begin());

    return out;
}

// Permute a Shape_6D according to runtime permutation perm.
// perm must be length 6 and contain a permutation of {0,1,2,3,4,5}.
// perm[j] == old_axis_index for new axis j (same semantics as PyTorch permute).
Shape_6D<float> permute_shape6d(const Shape_6D<float> &in, const std::array<int, 6> &perm)
{
    // Validate perm is a permutation of 0..5
    std::array<int, 6> check = perm;
    std::sort(check.begin(), check.end());
    for (int i = 0; i < 6; ++i)
    {
        if (check[i] != i)
            throw std::invalid_argument("perm must be a permutation of 0..5");
    }

    // Build output dims: out.dj = in.d[perm[j]]
    Shape_6D<float> out;
    auto dim = [&](const Shape_6D<float> &s, int axis) -> int
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
            case 5:
                return s.d5;
            default:
                throw std::out_of_range("axis");
        }
    };
    out.d0 = dim(in, perm[0]);
    out.d1 = dim(in, perm[1]);
    out.d2 = dim(in, perm[2]);
    out.d3 = dim(in, perm[3]);
    out.d4 = dim(in, perm[4]);
    out.d5 = dim(in, perm[5]);

    // Resize output buffer
    const size_t out_size = static_cast<size_t>(out.d0) * out.d1 * out.d2 * out.d3 * out.d4 * out.d5;
    out.buf.resize(out_size);

    // Compute inverse permutation inv[old_axis] = new_axis
    std::array<int, 6> inv;
    for (int new_ax = 0; new_ax < 6; ++new_ax)
        inv[perm[new_ax]] = new_ax;

    // Index helpers (row-major)
    auto idx_in = [&](int i0, int i1, int i2, int i3, int i4, int i5) -> size_t
    {
        return static_cast<size_t>(
                (((((i0 * in.d1 + i1) * in.d2 + i2) * in.d3 + i3) * in.d4 + i4) * in.d5) + i5
        );
    };

    auto idx_out = [&](int i0, int i1, int i2, int i3, int i4, int i5) -> size_t
    {
        return static_cast<size_t>(
                (((((i0 * out.d1 + i1) * out.d2 + i2) * out.d3 + i3) * out.d4 + i4) * out.d5) + i5
        );
    };

    // Iterate over output indices and read from input using inverse mapping:
    // For each output index out_idx[0..5], source index src[k] = out_idx[ inv[k] ].
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
                        for (int o5 = 0; o5 < out.d5; ++o5)
                        {
                            // build output index array
                            int out_idx[6] = {o0, o1, o2, o3, o4, o5};
                            // compute source indices using inverse permutation
                            int s0 = out_idx[inv[0]];
                            int s1 = out_idx[inv[1]];
                            int s2 = out_idx[inv[2]];
                            int s3 = out_idx[inv[3]];
                            int s4 = out_idx[inv[4]];
                            int s5 = out_idx[inv[5]];
                            size_t s = idx_in(s0, s1, s2, s3, s4, s5);
                            size_t d = idx_out(o0, o1, o2, o3, o4, o5);
                            out.buf[d] = in.buf[s];
                        }
                    }
                }
            }
        }
    }

    return out;
}

#endif //SHAPE_6D_H
