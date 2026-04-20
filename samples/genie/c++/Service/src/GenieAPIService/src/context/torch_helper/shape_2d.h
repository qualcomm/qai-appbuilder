//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef SHAPE_2D_H
#define SHAPE_2D_H

#include "base.h"

Shape_1D<int64_t> Flatten(const Shape_2D<int64_t> &in)
{
    std::size_t expected = static_cast<std::size_t>(in.d0) * static_cast<std::size_t>(in.d1);
    if (in.buf.size() != expected)
        throw std::runtime_error("buffer size mismatch");

    Shape_1D<int64_t> out;
    out.d0 = static_cast<int>(expected);
    out.buf = in.buf; // copy entire buffer (row-major)
    return out;
}

template<typename T>
Shape_2D<T> MultiplyScalar(const Shape_2D<T> &in, T s)
{
    std::size_t n = static_cast<std::size_t>(in.d0) * static_cast<std::size_t>(in.d1);
    Shape_2D<int64_t> out;
    out.d0 = in.d0;
    out.d1 = in.d1;
    out.buf.resize(n);

    for (std::size_t i = 0; i < n; ++i)
    {
        out.buf[i] = in.buf[i] * s;
    }
    return out;
}

template<typename T>
Shape_2D<T> Unsqueeze(const Shape_1D<T> &in)
{
    Shape_2D<T> out;
    out.d0 = in.d0;
    out.d1 = 1;
    out.buf.resize(static_cast<size_t>(out.d0) * out.d1);
    // copy values into column 0
    for (int i = 0; i < in.d0; ++i)
    {
        out.buf[static_cast<size_t>(i) * out.d1 + 0] = in.buf[static_cast<size_t>(i)];
    }
    return out;
}

template<typename T>
Shape_1D<T> slice_keep_one_axis_2d(
        const Shape_2D<T> &in,
        int keep_axis,
        int idx_a
)
{
    // Validate input dims
    if (in.d0 <= 0 || in.d1 <= 0)
        throw std::invalid_argument("input dims must be > 0");
    if (keep_axis < 0 || keep_axis > 1)
        throw std::invalid_argument("keep_axis must be 0 or 1");

    // Determine keep length and validate fixed index
    int keep_len = (keep_axis == 0) ? in.d0 : in.d1;

    auto check_index = [&](int axis, int idx)
    {
        if (idx < 0)
            throw std::out_of_range("index must be >= 0");
        if (axis == 0 && idx >= in.d0)
            throw std::out_of_range("idx_a out of range for axis 0");
        if (axis == 1 && idx >= in.d1)
            throw std::out_of_range("idx_a out of range for axis 1");
    };

    int fixed_idx0 = 0; // will hold the fixed index for the non-kept axis

    if (keep_axis == 0)
    {
        // keep axis0, so idx_a -> axis1 (fixed)
        check_index(1, idx_a);
        fixed_idx0 = idx_a; // axis1 fixed
    }
    else // keep_axis == 1
    {
        // keep axis1, so idx_a -> axis0 (fixed)
        check_index(0, idx_a);
        fixed_idx0 = idx_a; // axis0 fixed
    }

    // Prepare output
    Shape_1D<T> out;
    out.d0 = keep_len;
    out.buf.resize(static_cast<size_t>(keep_len));

    // Input linear index helper: (i0 * d1 + i1)
    auto in_index = [&](int i0, int i1) -> size_t
    {
        return static_cast<size_t>(i0 * in.d1 + i1);
    };

    for (int k = 0; k < keep_len; ++k)
    {
        size_t src_idx;
        if (keep_axis == 0)
        {
            // src = (k, fixed_idx0) where fixed_idx0 == axis1
            src_idx = in_index(k, fixed_idx0);
        }
        else
        {
            // keep_axis == 1
            // src = (fixed_idx0, k) where fixed_idx0 == axis0
            src_idx = in_index(fixed_idx0, k);
        }
        out.buf[static_cast<size_t>(k)] = in.buf[src_idx];
    }

    return out;
}

template<typename T>
Shape_2D<T> Full(int out_d0, int out_d1, T fill_value)
{
    std::vector<T> buf;
    buf.resize(out_d0 * out_d1, fill_value);
    return Shape_2D<T>{out_d0, out_d1, std::move(buf)};
}

template<typename T>
Shape_4D<T> reshape_2d_to_4d(
        const Shape_2D<T> &in,
        int out_d0,
        int out_d1,
        int out_d2,
        int out_d3
)
{
    // Validate input dims
    if (in.d0 <= 0 || in.d1 <= 0)
        throw std::invalid_argument("input dims must be > 0");

    // Count how many -1s
    int neg1_count = 0;
    neg1_count += (out_d0 == -1);
    neg1_count += (out_d1 == -1);
    neg1_count += (out_d2 == -1);
    neg1_count += (out_d3 == -1);
    if (neg1_count > 1)
        throw std::invalid_argument("only one -1 allowed");

    // Validate provided positive dims (allow -1)
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
    long long in_total = static_cast<long long>(in.d0) * in.d1;

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
    Shape_4D<T> out;
    out.d0 = out_d0;
    out.d1 = out_d1;
    out.d2 = out_d2;
    out.d3 = out_d3;
    out.buf.resize(static_cast<size_t>(out_total));

    // Copy linear buffer (contiguous row-major)
    std::copy(in.buf.begin(), in.buf.end(), out.buf.begin());

    return out;
}

#endif //SHAPE_2D_H
