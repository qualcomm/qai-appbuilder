//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef SHAPE_3D_H
#define SHAPE_3D_H

#include "base.h"

// Concatenate a list of Shape_3D along axis `dim` (0,1,2).
// Throws on empty input, mismatched non-concat dims, or overflow.
template<typename T>
Shape_3D<T> concat_3d_list(const std::vector<Shape_3D<T>> &inputs, int dim)
{
    if (inputs.empty())
        throw std::invalid_argument("inputs must not be empty");
    if (dim < 0 || dim > 2)
        throw std::invalid_argument("dim must be 0, 1, or 2");

    // Use first tensor as reference for non-concat dims
    const Shape_3D<T> &ref = inputs[0];
    if (ref.d0 < 0 || ref.d1 < 0 || ref.d2 < 0)
        throw std::invalid_argument("invalid reference dims");

    // Validate and compute output dims
    long long out_d0 = ref.d0;
    long long out_d1 = ref.d1;
    long long out_d2 = ref.d2;

    for (size_t i = 1; i < inputs.size(); ++i)
    {
        const auto &s = inputs[i];
        if (s.d0 < 0 || s.d1 < 0 || s.d2 < 0)
            throw std::invalid_argument("invalid input dims");
        if (dim != 0 && s.d0 != ref.d0)
            throw std::invalid_argument("mismatch on axis 0");
        if (dim != 1 && s.d1 != ref.d1)
            throw std::invalid_argument("mismatch on axis 1");
        if (dim != 2 && s.d2 != ref.d2)
            throw std::invalid_argument("mismatch on axis 2");
        if (dim == 0)
            out_d0 += s.d0;
        if (dim == 1)
            out_d1 += s.d1;
        if (dim == 2)
            out_d2 += s.d2;
    }

    if (out_d0 > std::numeric_limits<int>::max() ||
        out_d1 > std::numeric_limits<int>::max() ||
        out_d2 > std::numeric_limits<int>::max())
        throw std::overflow_error("resulting dims too large");

    Shape_3D<T> out;
    out.d0 = static_cast<int>(out_d0);
    out.d1 = static_cast<int>(out_d1);
    out.d2 = static_cast<int>(out_d2);

    // total elements and per-plane size
    size_t plane = static_cast<size_t>(out.d1) * out.d2;
    // For axis0 concat, plane is d1*d2; for axis1 concat, plane_per_slice = d2; for axis2 concat, innermost
    size_t total_elems = static_cast<size_t>(out.d0) * out.d1 * out.d2;
    out.buf.resize(total_elems);

    // Copy inputs into out buffer
    if (dim == 0)
    {
        // copy whole planes sequentially
        size_t offset_planes = 0;
        for (const auto &s: inputs)
        {
            size_t elems = static_cast<size_t>(s.d0) * s.d1 * s.d2;
            if (s.buf.size() != elems)
                throw std::invalid_argument("input buffer size mismatch");
            std::copy(s.buf.begin(), s.buf.end(), out.buf.begin() + offset_planes * plane);
            offset_planes += static_cast<size_t>(s.d0);
        }
    }
    else if (dim == 1)
    {
        // iterate over output d0, for each input append its d1 slices
        // For each input, for each b in [0..d0-1], copy its d1 rows of length d2 into correct position.
        size_t out_d1_accum = 0;
        for (const auto &s: inputs)
        {
            // s.d1 rows to place starting at out_d1_accum
            for (int b = 0; b < s.d0; ++b)
            {
                for (int r = 0; r < s.d1; ++r)
                {
                    // source offset: ((b * s.d1) + r) * s.d2
                    size_t src_off = (static_cast<size_t>(b) * s.d1 + r) * static_cast<size_t>(s.d2);
                    // dest offset: ((b * out.d1) + (out_d1_accum + r)) * out.d2
                    size_t dst_off = (static_cast<size_t>(b) * out.d1 + (out_d1_accum + r)) * static_cast<size_t>(out.d2);
                    std::copy(s.buf.begin() + src_off, s.buf.begin() + src_off + s.d2, out.buf.begin() + dst_off);
                }
            }
            out_d1_accum += static_cast<size_t>(s.d1);
        }
    }
    else
    { // dim == 2
        // concatenate along innermost axis: for each b and r, append each input's row
        for (int b = 0; b < out.d0; ++b)
        {
            for (int r = 0; r < out.d1; ++r)
            {
                size_t dst_base = (static_cast<size_t>(b) * out.d1 + r) * static_cast<size_t>(out.d2);
                size_t dst_pos = 0;
                for (const auto &s: inputs)
                {
                    // source row length = s.d2, source base = ((b * s.d1) + r) * s.d2
                    size_t src_base = (static_cast<size_t>(b) * s.d1 + r) * static_cast<size_t>(s.d2);
                    std::copy(s.buf.begin() + src_base, s.buf.begin() + src_base + s.d2, out.buf.begin() + dst_base + dst_pos);
                    dst_pos += static_cast<size_t>(s.d2);
                }
            }
        }
    }

    return out;
}

// Split a Shape_3D into a vector of Shape_2D by slicing along axis 0.
// For input shape [N, H, W] returns N elements each of shape [H, W].
template<typename T>
std::vector<Shape_2D<T>> SplitShape3d(const Shape_3D<T> &in)
{
    if (in.d0 <= 0 || in.d1 <= 0 || in.d2 <= 0)
        throw std::invalid_argument("input dims must be > 0");

    const int N = in.d0;
    const int H = in.d1;
    const int W = in.d2;
    const size_t slice_size = static_cast<size_t>(H) * static_cast<size_t>(W);

    std::vector<Shape_2D<T>> out;
    out.reserve(static_cast<size_t>(N));

    for (int i = 0; i < N; ++i)
    {
        Shape_2D<T> s;
        s.d0 = H;
        s.d1 = W;
        s.buf.resize(slice_size);

        size_t offset = static_cast<size_t>(i) * slice_size;
        std::copy(in.buf.begin() + offset, in.buf.begin() + offset + slice_size, s.buf.begin());

        out.push_back(std::move(s));
    }

    return out;
}


Shape_1D<float> slice_keep_one_axis(
        const Shape_3D<float> &in,
        int keep_axis,
        int idx_a,
        int idx_b
)
{
    // Validate input dims
    if (in.d0 <= 0 || in.d1 <= 0 || in.d2 <= 0)
        throw std::invalid_argument("input dims must be > 0");
    if (keep_axis < 0 || keep_axis > 2)
        throw std::invalid_argument("keep_axis must be 0, 1, or 2");

    // Determine sizes and validate fixed indices
    int keep_len = (keep_axis == 0) ? in.d0 : (keep_axis == 1) ? in.d1 : in.d2;
    // Validate idx_a and idx_b against the two fixed axes in axis order
    // axis order: 0,1,2. For each axis != keep_axis we check the corresponding idx.
    auto check_index = [&](int axis, int idx)
    {
        if (idx < 0)
            throw std::out_of_range("index must be >= 0");
        if (axis == 0 && idx >= in.d0)
            throw std::out_of_range("idx_a/idx_b out of range for axis 0");
        if (axis == 1 && idx >= in.d1)
            throw std::out_of_range("idx_a/idx_b out of range for axis 1");
        if (axis == 2 && idx >= in.d2)
            throw std::out_of_range("idx_a/idx_b out of range for axis 2");
    };

    // Map idx_a and idx_b to the two non-kept axes in axis order
    int fixed_idx0 = 0, fixed_idx1 = 0; // will hold indices for axis0 and axis2 when needed
    if (keep_axis == 0)
    {
        // keep axis0, so idx_a -> axis1, idx_b -> axis2
        check_index(1, idx_a);
        check_index(2, idx_b);
        fixed_idx0 = idx_a; // axis1 fixed
        fixed_idx1 = idx_b; // axis2 fixed
    }
    else if (keep_axis == 1)
    {
        // keep axis1, so idx_a -> axis0, idx_b -> axis2
        check_index(0, idx_a);
        check_index(2, idx_b);
        fixed_idx0 = idx_a; // axis0 fixed
        fixed_idx1 = idx_b; // axis2 fixed
    }
    else
    { // keep_axis == 2
        // keep axis2, so idx_a -> axis0, idx_b -> axis1
        check_index(0, idx_a);
        check_index(1, idx_b);
        fixed_idx0 = idx_a; // axis0 fixed
        fixed_idx1 = idx_b; // axis1 fixed
    }

    // Prepare output
    Shape_1D<float> out;
    out.d0 = keep_len;
    out.buf.resize(static_cast<size_t>(keep_len));

    // Input linear index helper: ((b * d1 + h) * d2 + w)
    auto in_index = [&](int i0, int i1, int i2) -> size_t
    {
        return static_cast<size_t>(((i0 * in.d1 + i1) * in.d2) + i2);
    };

    // Fill output by iterating over the kept axis and using fixed indices for others
    for (int k = 0; k < keep_len; ++k)
    {
        size_t src_idx = 0;
        if (keep_axis == 0)
        {
            // src = (k, fixed_idx0, fixed_idx1)  where fixed_idx0==axis1, fixed_idx1==axis2
            src_idx = in_index(k, fixed_idx0, fixed_idx1);
        }
        else if (keep_axis == 1)
        {
            // src = (fixed_idx0, k, fixed_idx1)  where fixed_idx0==axis0, fixed_idx1==axis2
            src_idx = in_index(fixed_idx0, k, fixed_idx1);
        }
        else
        { // keep_axis == 2
            // src = (fixed_idx0, fixed_idx1, k)  where fixed_idx0==axis0, fixed_idx1==axis1
            src_idx = in_index(fixed_idx0, fixed_idx1, k);
        }
        out.buf[static_cast<size_t>(k)] = in.buf[src_idx];
    }

    return out;
}

template<typename T>
Shape_2D<T> reshape_3d_to_2d(
        const Shape_3D<T> &in,
        int out_d0,
        int out_d1
)
{
    // Validate input dims
    if (in.d0 <= 0 || in.d1 <= 0 || in.d2 <= 0)
        throw std::invalid_argument("input dims must be > 0");

    // Count how many -1s
    int neg1_count = 0;
    neg1_count += (out_d0 == -1);
    neg1_count += (out_d1 == -1);
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

    // Compute input total
    long long in_total = static_cast<long long>(in.d0) * in.d1 * in.d2;

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
    Shape_2D<T> out;
    out.d0 = out_d0;
    out.d1 = out_d1;
    out.buf.resize(static_cast<size_t>(out_total));

    // Copy linear buffer (contiguous row-major)
    std::copy(in.buf.begin(), in.buf.end(), out.buf.begin());

    return out;
}

// Reshape contiguous Shape_3D -> contiguous Shape_4D
// Allows exactly one of out_d0/out_d1/out_d2/out_d3 to be -1 (to infer that dimension).
Shape_4D<float> reshape_3d_to_4d(
        Shape_3D<float> &in,
        int out_d0,
        int out_d1,
        int out_d2,
        int out_d3
)
{
    // Validate input dims
    if (in.d0 <= 0 || in.d1 <= 0 || in.d2 <= 0)
        throw std::invalid_argument("input dims must be > 0");

    // Count how many -1s
    int neg1_count = 0;
    neg1_count += (out_d0 == -1);
    neg1_count += (out_d1 == -1);
    neg1_count += (out_d2 == -1);
    neg1_count += (out_d3 == -1);
    if (neg1_count > 1)
        throw std::invalid_argument("only one -1 allowed");

    // Validate provided positive dims
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
    long long in_total = static_cast<long long>(in.d0) * in.d1 * in.d2;

    // If one dim is -1, infer it
    if (neg1_count == 1)
    {
        // compute product of provided positive dims
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

template<typename T>
Shape_4D<T> reshape_3d_to_4d(Shape_3D<T> &src, bool allow_move = true)
{
    if (src.d0 <= 0 || src.d1 <= 0 || src.d2 <= 0)
        throw std::invalid_argument("invalid source dimensions");

    // N must be a perfect square: N = H * H
    int N = src.d1;
    int H = static_cast<int>(std::lround(std::sqrt(static_cast<double>(N))));
    if (H * H != N)
        throw std::invalid_argument("second dimension is not a perfect square");

    // Validate buffer size
    std::size_t expected = static_cast<std::size_t>(src.d0) * static_cast<std::size_t>(src.d1) * static_cast<std::size_t>(src.d2);
    if (src.buf.size() != expected)
        throw std::invalid_argument("buffer size does not match dimensions");

    Shape_4D<T> out;
    out.d0 = src.d0;
    out.d1 = H;
    out.d2 = H;
    out.d3 = src.d2;

    if (allow_move)
    {
        // zero-copy: transfer ownership of the underlying vector
        out.buf = std::move(src.buf);
        // leave src in a valid empty state
        src.buf.clear();
        src.d0 = src.d1 = src.d2 = 0;
    }
    else
    {
        // copy the data (no reordering required because reshape is just reinterpretation)
        out.buf = src.buf;
    }

    return out;
}

Shape_5D<float> reshape_3d_to_5d(
        const Shape_3D<float> &in,
        int out_d0, int out_d1, int out_d2, int out_d3, int out_d4
)
{
    // Validate positive dims
    if (in.d0 <= 0 || in.d1 <= 0 || in.d2 <= 0)
        throw std::invalid_argument("input dims must be > 0");
    if (out_d0 <= 0 || out_d1 <= 0 || out_d2 <= 0 || out_d3 <= 0 || out_d4 <= 0)
        throw std::invalid_argument("output dims must be > 0");

    // Check total element counts match
    long long in_total = static_cast<long long>(in.d0) * in.d1 * in.d2;
    long long out_total = static_cast<long long>(out_d0) * out_d1 * out_d2 * out_d3 * out_d4;
    if (in_total != out_total)
        throw std::invalid_argument("total number of elements must match for reshape");

    // Prepare output container
    Shape_5D<float> out;
    out.d0 = out_d0;
    out.d1 = out_d1;
    out.d2 = out_d2;
    out.d3 = out_d3;
    out.d4 = out_d4;
    out.buf.resize(static_cast<size_t>(out_total));

    // Precompute multipliers for destination unflattening (row-major)
    long long dest_stride0 = static_cast<long long>(out_d1) * out_d2 * out_d3 * out_d4;
    long long dest_stride1 = static_cast<long long>(out_d2) * out_d3 * out_d4;
    long long dest_stride2 = static_cast<long long>(out_d3) * out_d4;
    long long dest_stride3 = static_cast<long long>(out_d4);
    // dest_stride4 = 1

    // Iterate over input logical indices and map to destination indices
    for (int b = 0; b < in.d0; ++b)
    {
        for (int f = 0; f < in.d1; ++f)
        {
            for (int c = 0; c < in.d2; ++c)
            {
                // source linear index (row-major)
                long long g = (static_cast<long long>(b) * in.d1 + f) * in.d2 + c;

                // unflatten g into destination multi-index
                long long rem = g;
                int i0 = static_cast<int>(rem / dest_stride0);
                rem %= dest_stride0;
                int i1 = static_cast<int>(rem / dest_stride1);
                rem %= dest_stride1;
                int i2 = static_cast<int>(rem / dest_stride2);
                rem %= dest_stride2;
                int i3 = static_cast<int>(rem / dest_stride3);
                rem %= dest_stride3;
                int i4 = static_cast<int>(rem); // rem < out_d4

                // compute destination linear index (row-major)
                long long dst_idx = ((((static_cast<long long>(i0) * out.d1 + i1) * out.d2 + i2) * out.d3 + i3) * out.d4) + i4;
                out.buf[static_cast<size_t>(dst_idx)] = in.buf[static_cast<size_t>(g)];
            }
        }
    }

    return out;
}

Shape_6D<float> reshape_3d_to_6d(
        const Shape_3D<float> &in,
        int out_d0, int out_d1, int out_d2, int out_d3, int out_d4, int out_d5
)
{
    // Validate positive dims
    if (in.d0 <= 0 || in.d1 <= 0 || in.d2 <= 0)
        throw std::invalid_argument("input dims must be > 0");
    if (out_d0 <= 0 || out_d1 <= 0 || out_d2 <= 0 || out_d3 <= 0 || out_d4 <= 0 || out_d5 <= 0)
        throw std::invalid_argument("output dims must be > 0");

    // Check total element counts match
    // in_total = in.d0 * in.d1 * in.d2
    // out_total = out_d0 * out_d1 * out_d2 * out_d3 * out_d4 * out_d5
    long long in_total = static_cast<long long>(in.d0) * in.d1 * in.d2;
    long long out_total = static_cast<long long>(out_d0) * out_d1 * out_d2 * out_d3 * out_d4 * out_d5;
    if (in_total != out_total)
        throw std::invalid_argument("total number of elements must match for reshape");

    // Prepare output container
    Shape_6D<float> out;
    out.d0 = out_d0;
    out.d1 = out_d1;
    out.d2 = out_d2;
    out.d3 = out_d3;
    out.d4 = out_d4;
    out.d5 = out_d5;
    out.buf.resize(static_cast<size_t>(out_total));

    // Precompute multipliers for destination unflattening (row-major)
    // dest_stride[k] = product of dims after axis k
    long long dest_stride0 = static_cast<long long>(out_d1) * out_d2 * out_d3 * out_d4 * out_d5;
    long long dest_stride1 = static_cast<long long>(out_d2) * out_d3 * out_d4 * out_d5;
    long long dest_stride2 = static_cast<long long>(out_d3) * out_d4 * out_d5;
    long long dest_stride3 = static_cast<long long>(out_d4) * out_d5;
    long long dest_stride4 = static_cast<long long>(out_d5);
    // dest_stride5 = 1

    // Iterate over input logical indices (b, flat, c) and map to destination indices
    // linear index in input buffer (row-major) is: g = ((b * in.d1 + f) * in.d2) + c
    for (int b = 0; b < in.d0; ++b)
    {
        for (int f = 0; f < in.d1; ++f)
        {
            for (int c = 0; c < in.d2; ++c)
            {
                // source linear index
                long long g = (static_cast<long long>(b) * in.d1 + f) * in.d2 + c;
                // compute destination multi-index by unflattening g
                long long rem = g;
                int i0 = static_cast<int>(rem / dest_stride0);
                rem %= dest_stride0;
                int i1 = static_cast<int>(rem / dest_stride1);
                rem %= dest_stride1;
                int i2 = static_cast<int>(rem / dest_stride2);
                rem %= dest_stride2;
                int i3 = static_cast<int>(rem / dest_stride3);
                rem %= dest_stride3;
                int i4 = static_cast<int>(rem / dest_stride4);
                rem %= dest_stride4;
                int i5 = static_cast<int>(rem); // rem < out_d5

                // compute destination linear index (row-major)
                long long dst_idx = (((((static_cast<long long>(i0) * out.d1 + i1) * out.d2 + i2) * out.d3 + i3) * out.d4 + i4) * out.d5) + i5;
                out.buf[static_cast<size_t>(dst_idx)] = in.buf[static_cast<size_t>(g)];
            }
        }
    }

    return out;
}

Shape_3D<float> slice_shape3d_range(
        const Shape_3D<float> &in,
        int start0, int end0, int step0,
        int start1, int end1, int step1,
        int start2, int end2, int step2
)
{
    if (in.d0 <= 0 || in.d1 <= 0 || in.d2 <= 0)
        throw std::invalid_argument("input dims must be > 0");

    // Normalize end == -1 to mean full length
    if (end0 == -1)
        end0 = in.d0;
    if (end1 == -1)
        end1 = in.d1;
    if (end2 == -1)
        end2 = in.d2;

    auto check_range = [](int start, int end, int step, int dim, const char *name)
    {
        if (step <= 0)
            throw std::invalid_argument(std::string(name) + " step must be > 0");
        if (start < 0 || start > dim)
            throw std::out_of_range(std::string(name) + " start out of range");
        if (end < 0 || end > dim)
            throw std::out_of_range(std::string(name) + " end out of range");
        if (start > end)
            throw std::invalid_argument(std::string(name) + " start must be <= end");
    };

    check_range(start0, end0, step0, in.d0, "axis0");
    check_range(start1, end1, step1, in.d1, "axis1");
    check_range(start2, end2, step2, in.d2, "axis2");

    auto compute_len = [](int start, int end, int step) -> int
    {
        if (start >= end)
            return 0;
        int span = end - start;
        return (span + step - 1) / step;
    };

    int out0 = compute_len(start0, end0, step0);
    int out1 = compute_len(start1, end1, step1);
    int out2 = compute_len(start2, end2, step2);

    long long total = static_cast<long long>(out0) * out1 * out2;
    if (total > static_cast<long long>(std::numeric_limits<size_t>::max()));
//        throw std::overflow_error("output size too large");

    Shape_3D<float> out;
    out.d0 = out0;
    out.d1 = out1;
    out.d2 = out2;
    out.buf.resize(static_cast<size_t>(total));

    auto in_index = [&](int i0, int i1, int i2) -> size_t
    {
        return static_cast<size_t>(((i0 * in.d1 + i1) * in.d2) + i2);
    };
    auto out_index = [&](int i0, int i1, int i2) -> size_t
    {
        return static_cast<size_t>(((i0 * out.d1 + i1) * out.d2) + i2);
    };

    for (int o0 = 0, s0 = start0; o0 < out0; ++o0, s0 += step0)
    {
        for (int o1 = 0, s1 = start1; o1 < out1; ++o1, s1 += step1)
        {
            for (int o2 = 0, s2 = start2; o2 < out2; ++o2, s2 += step2)
            {
                out.buf[out_index(o0, o1, o2)] = in.buf[in_index(s0, s1, s2)];
            }
        }
    }

    return out;
}

#endif //SHAPE_3D_H
