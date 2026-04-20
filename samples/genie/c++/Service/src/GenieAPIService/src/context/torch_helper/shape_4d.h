//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef SHAPE_4D_H
#define SHAPE_4D_H

#include "base.h"

template<typename T>
Shape_5D<T> stack_4d_list(const std::vector<Shape_4D<T>> &inputs, int dim)
{
    if (inputs.empty())
        throw std::invalid_argument("inputs must not be empty");
    if (dim < 0 || dim > 4)
        throw std::invalid_argument("dim must be in [0..4]");

    const Shape_4D<T> &ref = inputs[0];
    if (ref.d0 < 0 || ref.d1 < 0 || ref.d2 < 0 || ref.d3 < 0)
        throw std::invalid_argument("invalid reference dims");

    // All non-new axes must match the reference
    for (size_t i = 1; i < inputs.size(); ++i)
    {
        const auto &s = inputs[i];
        if (s.d0 < 0 || s.d1 < 0 || s.d2 < 0 || s.d3 < 0)
            throw std::invalid_argument("invalid input dims");
        if (s.d0 != ref.d0)
            throw std::invalid_argument("mismatch on axis 0");
        if (s.d1 != ref.d1)
            throw std::invalid_argument("mismatch on axis 1");
        if (s.d2 != ref.d2)
            throw std::invalid_argument("mismatch on axis 2");
        if (s.d3 != ref.d3)
            throw std::invalid_argument("mismatch on axis 3");
    }

    const size_t N = inputs.size();
    // Build output dims by inserting N at position dim
    long long out_dims[5];
    int in_dims[4] = {ref.d0, ref.d1, ref.d2, ref.d3};
    for (int k = 0, j = 0; k < 5; ++k)
    {
        if (k == dim)
            out_dims[k] = static_cast<long long>(N);
        else
            out_dims[k] = static_cast<long long>(in_dims[j++]);
    }

    // Overflow checks against int
    for (int k = 0; k < 5; ++k)
        if (out_dims[k] > std::numeric_limits<int>::max())
            throw std::overflow_error("resulting dims too large");

    Shape_5D<T> out;
    out.d0 = static_cast<int>(out_dims[0]);
    out.d1 = static_cast<int>(out_dims[1]);
    out.d2 = static_cast<int>(out_dims[2]);
    out.d3 = static_cast<int>(out_dims[3]);
    out.d4 = static_cast<int>(out_dims[4]);

    // compute strides for output (row-major)
    size_t out_stride[5];
    out_stride[4] = 1;
    out_stride[3] = static_cast<size_t>(out.d4) * out_stride[4];
    out_stride[2] = static_cast<size_t>(out.d3) * out_stride[3];
    out_stride[1] = static_cast<size_t>(out.d2) * out_stride[2];
    out_stride[0] = static_cast<size_t>(out.d1) * out_stride[1];

    // input strides (for 4D inputs)
    size_t in_stride[4];
    in_stride[3] = 1;
    in_stride[2] = static_cast<size_t>(ref.d3) * in_stride[3];
    in_stride[1] = static_cast<size_t>(ref.d2) * in_stride[2];
    in_stride[0] = static_cast<size_t>(ref.d1) * in_stride[1];

    // total elements
    size_t total_elems = static_cast<size_t>(out.d0) * out.d1 * out.d2 * out.d3 * out.d4;
    out.buf.resize(total_elems);

    // Validate each input buffer size
    size_t in_elems = static_cast<size_t>(ref.d0) * ref.d1 * ref.d2 * ref.d3;
    for (const auto &s: inputs)
        if (s.buf.size() != in_elems)
            throw std::invalid_argument("input buffer size mismatch");

    // Copy: iterate over each input and over all its indices, map to output index with inserted axis
    for (size_t i = 0; i < N; ++i)
    {
        const auto &s = inputs[i];
        // nested loops over input dims: a,b,c,d correspond to input axes 0..3
        for (int a = 0; a < s.d0; ++a)
        {
            for (int b = 0; b < s.d1; ++b)
            {
                for (int c = 0; c < s.d2; ++c)
                {
                    for (int d = 0; d < s.d3; ++d)
                    {
                        size_t src_index = static_cast<size_t>(a) * in_stride[0]
                                           + static_cast<size_t>(b) * in_stride[1]
                                           + static_cast<size_t>(c) * in_stride[2]
                                           + static_cast<size_t>(d) * in_stride[3];

                        // build output multi-index by inserting i at position dim
                        int out_idx0, out_idx1, out_idx2, out_idx3, out_idx4;
                        switch (dim)
                        {
                            case 0:
                                out_idx0 = static_cast<int>(i);
                                out_idx1 = a;
                                out_idx2 = b;
                                out_idx3 = c;
                                out_idx4 = d;
                                break;
                            case 1:
                                out_idx0 = a;
                                out_idx1 = static_cast<int>(i);
                                out_idx2 = b;
                                out_idx3 = c;
                                out_idx4 = d;
                                break;
                            case 2:
                                out_idx0 = a;
                                out_idx1 = b;
                                out_idx2 = static_cast<int>(i);
                                out_idx3 = c;
                                out_idx4 = d;
                                break;
                            case 3:
                                out_idx0 = a;
                                out_idx1 = b;
                                out_idx2 = c;
                                out_idx3 = static_cast<int>(i);
                                out_idx4 = d;
                                break;
                            default: // case 4
                                out_idx0 = a;
                                out_idx1 = b;
                                out_idx2 = c;
                                out_idx3 = d;
                                out_idx4 = static_cast<int>(i);
                                break;
                        }

                        size_t dst_index = static_cast<size_t>(out_idx0) * out_stride[0]
                                           + static_cast<size_t>(out_idx1) * out_stride[1]
                                           + static_cast<size_t>(out_idx2) * out_stride[2]
                                           + static_cast<size_t>(out_idx3) * out_stride[3]
                                           + static_cast<size_t>(out_idx4) * out_stride[4];

                        out.buf[dst_index] = s.buf[src_index];
                    }
                }
            }
        }
    }

    return out;
}

template<typename T>
Shape_4D<T> concat_4d_list(const std::vector<Shape_4D<T>> &inputs, int dim)
{
    if (inputs.empty())
        throw std::invalid_argument("inputs must not be empty");
    if (dim < 0 || dim > 3)
        throw std::invalid_argument("dim must be 0, 1, 2, or 3");

    const Shape_4D<T> &ref = inputs[0];
    if (ref.d0 < 0 || ref.d1 < 0 || ref.d2 < 0 || ref.d3 < 0)
        throw std::invalid_argument("invalid reference dims");

    long long out_d0 = ref.d0;
    long long out_d1 = ref.d1;
    long long out_d2 = ref.d2;
    long long out_d3 = ref.d3;

    for (size_t i = 1; i < inputs.size(); ++i)
    {
        const auto &s = inputs[i];
        if (s.d0 < 0 || s.d1 < 0 || s.d2 < 0 || s.d3 < 0)
            throw std::invalid_argument("invalid input dims");
        if (dim != 0 && s.d0 != ref.d0)
            throw std::invalid_argument("mismatch on axis 0");
        if (dim != 1 && s.d1 != ref.d1)
            throw std::invalid_argument("mismatch on axis 1");
        if (dim != 2 && s.d2 != ref.d2)
            throw std::invalid_argument("mismatch on axis 2");
        if (dim != 3 && s.d3 != ref.d3)
            throw std::invalid_argument("mismatch on axis 3");

        if (dim == 0)
            out_d0 += s.d0;
        if (dim == 1)
            out_d1 += s.d1;
        if (dim == 2)
            out_d2 += s.d2;
        if (dim == 3)
            out_d3 += s.d3;
    }

    if (out_d0 > std::numeric_limits<int>::max() ||
        out_d1 > std::numeric_limits<int>::max() ||
        out_d2 > std::numeric_limits<int>::max() ||
        out_d3 > std::numeric_limits<int>::max())
        throw std::overflow_error("resulting dims too large");

    Shape_4D<T> out;
    out.d0 = static_cast<int>(out_d0);
    out.d1 = static_cast<int>(out_d1);
    out.d2 = static_cast<int>(out_d2);
    out.d3 = static_cast<int>(out_d3);

    // sizes for copying
    size_t out_d2d3 = static_cast<size_t>(out.d2) * out.d3;
    size_t out_d1d2d3 = static_cast<size_t>(out.d1) * out_d2d3;
    size_t total_elems = static_cast<size_t>(out.d0) * out_d1d2d3;
    out.buf.resize(total_elems);

    if (dim == 0)
    {
        // copy whole blocks of size d1*d2*d3
        size_t plane = out_d1d2d3; // per-d0 block size
        size_t offset_blocks = 0;
        for (const auto &s: inputs)
        {
            size_t elems = static_cast<size_t>(s.d0) * s.d1 * s.d2 * s.d3;
            if (s.buf.size() != elems)
                throw std::invalid_argument("input buffer size mismatch");
            std::copy(s.buf.begin(), s.buf.end(), out.buf.begin() + offset_blocks * plane);
            offset_blocks += static_cast<size_t>(s.d0);
        }
    }
    else if (dim == 1)
    {
        // For each batch b, copy contiguous blocks of size d2*d3 for each d1 slice
        size_t slice_len_out = static_cast<size_t>(out.d2) * out.d3;
        size_t slice_len_in; // per input
        size_t out_d1_accum = 0;
        for (const auto &s: inputs)
        {
            slice_len_in = static_cast<size_t>(s.d2) * s.d3;
            if (s.buf.size() != static_cast<size_t>(s.d0) * s.d1 * s.d2 * s.d3)
                throw std::invalid_argument("input buffer size mismatch");
            for (int b = 0; b < s.d0; ++b)
            {
                for (int r = 0; r < s.d1; ++r)
                {
                    size_t src_off = (static_cast<size_t>(b) * s.d1 + r) * slice_len_in;
                    size_t dst_off = (static_cast<size_t>(b) * out.d1 + (out_d1_accum + r)) * slice_len_out;
                    std::copy(s.buf.begin() + src_off, s.buf.begin() + src_off + slice_len_in, out.buf.begin() + dst_off);
                }
            }
            out_d1_accum += static_cast<size_t>(s.d1);
        }
    }
    else if (dim == 2)
    {
        // For each batch b and d1 r, copy contiguous blocks of size d2*d3 (each input contributes its d2*d3)
        size_t row_len_out = static_cast<size_t>(out.d2) * out.d3;
        size_t row_len_in;
        for (int b = 0; b < out.d0; ++b)
        {
            for (int r = 0; r < out.d1; ++r)
            {
                size_t dst_base = (static_cast<size_t>(b) * out.d1 + r) * row_len_out;
                size_t dst_pos = 0;
                for (const auto &s: inputs)
                {
                    row_len_in = static_cast<size_t>(s.d2) * s.d3;
                    size_t src_base = (static_cast<size_t>(b) * s.d1 + r) * row_len_in;
                    std::copy(s.buf.begin() + src_base, s.buf.begin() + src_base + row_len_in, out.buf.begin() + dst_base + dst_pos);
                    dst_pos += row_len_in;
                }
            }
        }
    }
    else // dim == 3
    {
        // Innermost axis: for each b, r, c2 append each input's d3 elements
        for (int b = 0; b < out.d0; ++b)
        {
            for (int r = 0; r < out.d1; ++r)
            {
                for (int c2 = 0; c2 < out.d2; ++c2)
                {
                    size_t dst_base = ((static_cast<size_t>(b) * out.d1 + r) * out.d2 + c2) * static_cast<size_t>(out.d3);
                    size_t dst_pos = 0;
                    for (const auto &s: inputs)
                    {
                        size_t src_base = ((static_cast<size_t>(b) * s.d1 + r) * s.d2 + c2) * static_cast<size_t>(s.d3);
                        std::copy(s.buf.begin() + src_base, s.buf.begin() + src_base + static_cast<size_t>(s.d3), out.buf.begin() + dst_base + dst_pos);
                        dst_pos += static_cast<size_t>(s.d3);
                    }
                }
            }
        }
    }

    return out;
}

// Concatenate two Shape_4D along axis dim (0..3).
// Throws if dims on non-concatenated axes do not match or dim out of range.
Shape_4D<float> concat_shape4d(const Shape_4D<float> &A, const Shape_4D<float> &B, int dim)
{
    if (dim < 0 || dim > 3)
        throw std::invalid_argument("dim must be 0..3");
    if (A.d0 <= 0 || A.d1 <= 0 || A.d2 <= 0 || A.d3 <= 0)
        throw std::invalid_argument("A dims must be > 0");
    if (B.d0 <= 0 || B.d1 <= 0 || B.d2 <= 0 || B.d3 <= 0)
        throw std::invalid_argument("B dims must be > 0");

    // Check matching dims on non-concat axes
    if (dim != 0 && A.d0 != B.d0)
        throw std::invalid_argument("mismatch on axis 0");
    if (dim != 1 && A.d1 != B.d1)
        throw std::invalid_argument("mismatch on axis 1");
    if (dim != 2 && A.d2 != B.d2)
        throw std::invalid_argument("mismatch on axis 2");
    if (dim != 3 && A.d3 != B.d3)
        throw std::invalid_argument("mismatch on axis 3");

    // Compute output dims
    Shape_4D<float> out;
    out.d0 = (dim == 0) ? (A.d0 + B.d0) : A.d0;
    out.d1 = (dim == 1) ? (A.d1 + B.d1) : A.d1;
    out.d2 = (dim == 2) ? (A.d2 + B.d2) : A.d2;
    out.d3 = (dim == 3) ? (A.d3 + B.d3) : A.d3;

    long long total = static_cast<long long>(out.d0) * out.d1 * out.d2 * out.d3;
//    if (total > static_cast<long long>(std::numeric_limits<size_t>::max()))
//        throw std::overflow_error("output size too large");
    out.buf.resize(static_cast<size_t>(total));

    // Helpers: linear index in row-major
    auto idx = [](int i0, int i1, int i2, int i3, int D1, int D2, int D3) -> size_t
    {
        return static_cast<size_t>((((i0 * D1 + i1) * D2 + i2) * D3) + i3);
    };

    // Copy by mapping each output index to source A or B
    for (int o0 = 0; o0 < out.d0; ++o0)
    {
        for (int o1 = 0; o1 < out.d1; ++o1)
        {
            for (int o2 = 0; o2 < out.d2; ++o2)
            {
                for (int o3 = 0; o3 < out.d3; ++o3)
                {
                    float val;
                    switch (dim)
                    {
                        case 0:
                        {
                            if (o0 < A.d0)
                            {
                                size_t s = idx(o0, o1, o2, o3, A.d1, A.d2, A.d3);
                                val = A.buf[s];
                            }
                            else
                            {
                                int b0 = o0 - A.d0;
                                size_t s = idx(b0, o1, o2, o3, B.d1, B.d2, B.d3);
                                val = B.buf[s];
                            }
                            break;
                        }
                        case 1:
                        {
                            if (o1 < A.d1)
                            {
                                size_t s = idx(o0, o1, o2, o3, A.d1, A.d2, A.d3);
                                val = A.buf[s];
                            }
                            else
                            {
                                int b1 = o1 - A.d1;
                                size_t s = idx(o0, b1, o2, o3, B.d1, B.d2, B.d3);
                                val = B.buf[s];
                            }
                            break;
                        }
                        case 2:
                        {
                            if (o2 < A.d2)
                            {
                                size_t s = idx(o0, o1, o2, o3, A.d1, A.d2, A.d3);
                                val = A.buf[s];
                            }
                            else
                            {
                                int b2 = o2 - A.d2;
                                size_t s = idx(o0, o1, b2, o3, B.d1, B.d2, B.d3);
                                val = B.buf[s];
                            }
                            break;
                        }
                        case 3:
                        {
                            if (o3 < A.d3)
                            {
                                size_t s = idx(o0, o1, o2, o3, A.d1, A.d2, A.d3);
                                val = A.buf[s];
                            }
                            else
                            {
                                int b3 = o3 - A.d3;
                                size_t s = idx(o0, o1, o2, b3, B.d1, B.d2, B.d3);
                                val = B.buf[s];
                            }
                            break;
                        }
                        default:
                            throw std::logic_error("unreachable");
                    }
                    size_t d = idx(o0, o1, o2, o3, out.d1, out.d2, out.d3);
                    out.buf[d] = val;
                }
            }
        }
    }

    return out;
}

// Slice a 4D tensor by selecting a single index on axis0 and slicing axes 1..3.
// - idx0: the single index on axis0 (0-based). Must satisfy 0 <= idx0 < in.d0.
// - start1,end1,step1: slice parameters for axis1 (end==-1 means to the end).
// - start2,end2,step2: slice parameters for axis2 (defaults to full axis).
// - start3,end3,step3: slice parameters for axis3 (defaults to full axis).
template<typename T>
Shape_3D<T> Slice4DTo3D(
        const Shape_4D<T> &in,
        int idx0,
        int start1, int end1, int step1,
        int start2, int end2, int step2,
        int start3, int end3, int step3)
{
    // basic validation
    if (in.d0 <= 0 || in.d1 <= 0 || in.d2 <= 0 || in.d3 <= 0)
        throw std::invalid_argument("input dims must be > 0");

    if (idx0 < 0 || idx0 >= in.d0)
        throw std::out_of_range("idx0 out of range");

    // normalize -1 to full length
    if (end1 == -1)
        end1 = in.d1;
    if (end2 == -1)
        end2 = in.d2;
    if (end3 == -1)
        end3 = in.d3;

    auto check_range = [](int start, int end, int step, int dim, const char *name)
    {
        if (step == 0)
            throw std::invalid_argument(std::string(name) + " step must be != 0");
        if (start < 0 || start > dim)
            throw std::out_of_range(std::string(name) + " start out of range");
        if (end < 0 || end > dim)
            throw std::out_of_range(std::string(name) + " end out of range");
        if (start > end)
            throw std::invalid_argument(std::string(name) + " start must be <= end");
        if (step < 0)
            throw std::invalid_argument(std::string(name) + " negative step not supported");
    };

    check_range(start1, end1, step1, in.d1, "axis1");
    check_range(start2, end2, step2, in.d2, "axis2");
    check_range(start3, end3, step3, in.d3, "axis3");

    auto compute_len = [](int start, int end, int step) -> int
    {
        if (start >= end)
            return 0;
        int span = end - start;
        return (span + step - 1) / step;
    };

    int out0 = compute_len(start1, end1, step1); // becomes d0 of 3D
    int out1 = compute_len(start2, end2, step2); // becomes d1 of 3D
    int out2 = compute_len(start3, end3, step3); // becomes d2 of 3D

    long long total = static_cast<long long>(out0) * out1 * out2;
    if (total < 0)
        throw std::overflow_error("output size overflow");

    Shape_3D<T> out;
    out.d0 = out0;
    out.d1 = out1;
    out.d2 = out2;
    out.buf.resize(static_cast<size_t>(total));

    // index helpers (row-major)
    auto in_index = [&](int i0, int i1, int i2, int i3) -> size_t
    {
        return static_cast<size_t>((((i0 * in.d1 + i1) * in.d2 + i2) * in.d3) + i3);
    };
    auto out_index = [&](int i0, int i1, int i2) -> size_t
    {
        return static_cast<size_t>(((i0 * out.d1 + i1) * out.d2) + i2);
    };

    // copy
    for (int o0 = 0, s1 = start1; o0 < out0; ++o0, s1 += step1)
    {
        for (int o1 = 0, s2 = start2; o1 < out1; ++o1, s2 += step2)
        {
            for (int o2 = 0, s3 = start3; o2 < out2; ++o2, s3 += step3)
            {
                out.buf[out_index(o0, o1, o2)] = in.buf[in_index(idx0, s1, s2, s3)];
            }
        }
    }

    return out;
}

// Slice a 4D tensor with ranges [start: end: step] for each axis.
// If stepX == 0 -> invalid. end is exclusive. Use end == -1 to mean "until input dim".
Shape_4D<float> slice_shape4d_range(
        const Shape_4D<float> &in,
        int start0, int end0, int step0,
        int start1, int end1, int step1,
        int start2, int end2, int step2,
        int start3, int end3, int step3
)
{
    // Validate input dims
    if (in.d0 <= 0 || in.d1 <= 0 || in.d2 <= 0 || in.d3 <= 0)
        throw std::invalid_argument("input dims must be > 0");

    // Normalize end == -1 to mean full length
    if (end0 == -1)
        end0 = in.d0;
    if (end1 == -1)
        end1 = in.d1;
    if (end2 == -1)
        end2 = in.d2;
    if (end3 == -1)
        end3 = in.d3;

    // Validate ranges and steps
    auto check_range = [](int start, int end, int step, int dim, const char *name)
    {
        if (step == 0)
            throw std::invalid_argument("step must be != 0");
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
    check_range(start3, end3, step3, in.d3, "axis3");

    // Compute output sizes (matching Python slicing: iterate start, start+step, ... < end)
    auto compute_len = [](int start, int end, int step) -> int
    {
        if (start >= end)
            return 0;
        int span = end - start;
        int len = (span + step - 1) / step;
        return len;
    };
    int out0 = compute_len(start0, end0, step0);
    int out1 = compute_len(start1, end1, step1);
    int out2 = compute_len(start2, end2, step2);
    int out3 = compute_len(start3, end3, step3);

    // Check for overflow when allocating
    long long total = static_cast<long long>(out0) * out1 * out2 * out3;
//    if (total > static_cast<long long>(std::numeric_limits<size_t>::max()))
//        throw std::overflow_error("output size too large");

    // Prepare output
    Shape_4D<float> out;
    out.d0 = out0;
    out.d1 = out1;
    out.d2 = out2;
    out.d3 = out3;
    out.buf.resize(static_cast<size_t>(total));

    // Index helpers (row-major)
    auto in_index = [&](int i0, int i1, int i2, int i3) -> size_t
    {
        return static_cast<size_t>((((i0 * in.d1 + i1) * in.d2 + i2) * in.d3) + i3);
    };
    auto out_index = [&](int i0, int i1, int i2, int i3) -> size_t
    {
        return static_cast<size_t>((((i0 * out.d1 + i1) * out.d2 + i2) * out.d3) + i3);
    };

    // Copy values
    for (int o0 = 0, s0 = start0; o0 < out0; ++o0, s0 += step0)
    {
        for (int o1 = 0, s1 = start1; o1 < out1; ++o1, s1 += step1)
        {
            for (int o2 = 0, s2 = start2; o2 < out2; ++o2, s2 += step2)
            {
                for (int o3 = 0, s3 = start3; o3 < out3; ++o3, s3 += step3)
                {
                    out.buf[out_index(o0, o1, o2, o3)] = in.buf[in_index(s0, s1, s2, s3)];
                }
            }
        }
    }

    return out;
}

// Repeat a contiguous Shape_4D by runtime repeat factors (r0,r1,r2,r3).
// Semantics: out.shape = (in.d0 * r0, in.d1 * r1, in.d2 * r2, in.d3 * r3)
// Equivalent to PyTorch: out = in.repeat(r0, r1, r2, r3)
Shape_4D<float> repeat_shape4d(const Shape_4D_View<float> &in, int r0, int r1, int r2, int r3)
{
    // Validate input and repeat factors
    if (in.d0 <= 0 || in.d1 <= 0 || in.d2 <= 0 || in.d3 <= 0)
        throw std::invalid_argument("input dims must be > 0");
    if (r0 <= 0 || r1 <= 0 || r2 <= 0 || r3 <= 0)
        throw std::invalid_argument("repeat factors must be > 0");

//    printf("%d %d %d %d", in.d0,  in.d1, in.d2 ,in.d3);
    // Compute output dims and total size (use long long to avoid overflow)
    long long out_d0 = static_cast<long long>(in.d0) * r0;
    long long out_d1 = static_cast<long long>(in.d1) * r1;
    long long out_d2 = static_cast<long long>(in.d2) * r2;
    long long out_d3 = static_cast<long long>(in.d3) * r3;
    long long out_total = out_d0 * out_d1 * out_d2 * out_d3;
//    printf("    %lld", static_cast<long long>(std::numeric_limits<size_t>::max()));
    // 18432 >   -1
//    if (out_total > static_cast<long long>(std::numeric_limits<size_t>::max()))
//        throw std::overflow_error("output size too large");

    // Prepare output
    Shape_4D<float> out;
    out.d0 = static_cast<int>(out_d0);
    out.d1 = static_cast<int>(out_d1);
    out.d2 = static_cast<int>(out_d2);
    out.d3 = static_cast<int>(out_d3);
    out.buf.resize(static_cast<size_t>(out_total));

    // Precompute input and output strides (elements)
    // input row-major strides: s0 = d1*d2*d3, s1 = d2*d3, s2 = d3, s3 = 1
    const size_t in_s0 = static_cast<size_t>(in.d1) * in.d2 * in.d3;
    const size_t in_s1 = static_cast<size_t>(in.d2) * in.d3;
    const size_t in_s2 = static_cast<size_t>(in.d3);
    const size_t in_s3 = 1;

    // output row-major strides
    const size_t out_s0 = static_cast<size_t>(out.d1) * out.d2 * out.d3;
    const size_t out_s1 = static_cast<size_t>(out.d2) * out.d3;
    const size_t out_s2 = static_cast<size_t>(out.d3);
    const size_t out_s3 = 1;

    // Efficient copy: iterate over output indices and map to source indices by modulo
    // We optimize inner-most loop by copying contiguous blocks when possible:
    // For a fixed (o0,o1,o2), the inner o3 runs contiguous in memory; source o3 is (o3 % in.d3).
    for (int o0 = 0; o0 < out.d0; ++o0)
    {
        int s0 = o0 % in.d0;
        size_t out_base0 = static_cast<size_t>(o0) * out_s0;
        size_t in_base0 = static_cast<size_t>(s0) * in_s0;
        for (int o1 = 0; o1 < out.d1; ++o1)
        {
            int s1 = o1 % in.d1;
            size_t out_base1 = out_base0 + static_cast<size_t>(o1) * out_s1;
            size_t in_base1 = in_base0 + static_cast<size_t>(s1) * in_s1;
            for (int o2 = 0; o2 < out.d2; ++o2)
            {
                int s2 = o2 % in.d2;
                size_t out_base2 = out_base1 + static_cast<size_t>(o2) * out_s2;
                size_t in_base2 = in_base1 + static_cast<size_t>(s2) * in_s2;

                // Now copy the contiguous run along axis3.
                // We can copy in chunks: when in.d3 divides the output run, we can copy repeated blocks.
                // But simplest and efficient enough: copy element-wise for axis3.
                for (int o3 = 0; o3 < out.d3; ++o3)
                {
                    int s3 = o3 % in.d3;
                    size_t out_idx = out_base2 + static_cast<size_t>(o3);
                    size_t in_idx = in_base2 + static_cast<size_t>(s3);
                    out.buf[out_idx] = in.buf[in_idx];
                }
            }
        }
    }

    return out;
}

// Permute a Shape_4D according to runtime permutation perm (length 4).
// perm[j] == old_axis_index for new axis j (PyTorch semantics).
Shape_4D<float> permute_shape4d(Shape_4D<float> &in, std::array<int, 4> perm)
{
    // Validate perm is a permutation of {0,1,2,3}
    std::array<int, 4> check = perm;
    std::sort(check.begin(), check.end());
    for (int i = 0; i < 4; ++i)
    {
        if (check[i] != i)
            throw std::invalid_argument("perm must be a permutation of 0..3");
    }

    // Helper to get axis size by index
    auto dim = [&](const Shape_4D<float> &s, int axis) -> int
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
            default:
                throw std::out_of_range("axis");
        }
    };

    // Build output dims: out.dj = in.d[perm[j]]
    Shape_4D<float> out;
    out.d0 = dim(in, perm[0]);
    out.d1 = dim(in, perm[1]);
    out.d2 = dim(in, perm[2]);
    out.d3 = dim(in, perm[3]);

    // Resize output buffer
    const size_t out_size = static_cast<size_t>(out.d0) * out.d1 * out.d2 * out.d3;
    out.buf.resize(out_size);

    // Compute inverse permutation inv[old_axis] = new_axis
    std::array<int, 4> inv;
    for (int new_ax = 0; new_ax < 4; ++new_ax)
        inv[perm[new_ax]] = new_ax;

    // Row-major linear index helpers
    auto idx_in = [&](int i0, int i1, int i2, int i3) -> size_t
    {
        return static_cast<size_t>((((i0 * in.d1 + i1) * in.d2 + i2) * in.d3) + i3);
    };
    auto idx_out = [&](int i0, int i1, int i2, int i3) -> size_t
    {
        return static_cast<size_t>((((i0 * out.d1 + i1) * out.d2 + i2) * out.d3) + i3);
    };

    // Iterate over output indices and read from input using inverse mapping:
    // For each output index out_idx[0..3], source index src[k] = out_idx[ inv[k] ].
    for (int o0 = 0; o0 < out.d0; ++o0)
    {
        for (int o1 = 0; o1 < out.d1; ++o1)
        {
            for (int o2 = 0; o2 < out.d2; ++o2)
            {
                for (int o3 = 0; o3 < out.d3; ++o3)
                {
                    int out_idx0 = o0;
                    int out_idx1 = o1;
                    int out_idx2 = o2;
                    int out_idx3 = o3;
                    int s0 = (inv[0] == 0) ? out_idx0 : (inv[0] == 1) ? out_idx1 : (inv[0] == 2) ? out_idx2 : out_idx3;
                    int s1 = (inv[1] == 0) ? out_idx0 : (inv[1] == 1) ? out_idx1 : (inv[1] == 2) ? out_idx2 : out_idx3;
                    int s2 = (inv[2] == 0) ? out_idx0 : (inv[2] == 1) ? out_idx1 : (inv[2] == 2) ? out_idx2 : out_idx3;
                    int s3 = (inv[3] == 0) ? out_idx0 : (inv[3] == 1) ? out_idx1 : (inv[3] == 2) ? out_idx2 : out_idx3;
                    size_t s = idx_in(s0, s1, s2, s3);
                    size_t d = idx_out(o0, o1, o2, o3);
                    out.buf[d] = in.buf[s];
                }
            }
        }
    }

    return out;
}

// Reshape contiguous Shape_4D -> contiguous Shape_3D
// Allows out_d1 == -1 to infer that dimension from total size.
// Parameters are non-const ints: out_d0, out_d1, out_d2
Shape_3D<float> reshape_4d_to_3d(
        Shape_4D<float> &in,
        int out_d0,
        int out_d1, // may be -1 to infer
        int out_d2
)
{
    // Validate input dims
    if (in.d0 <= 0 || in.d1 <= 0 || in.d2 <= 0 || in.d3 <= 0)
        throw std::invalid_argument("input dims must be > 0");

    // Validate provided output dims (out_d1 may be -1)
    if (out_d0 <= 0)
        throw std::invalid_argument("out_d0 must be > 0");
    if (out_d2 <= 0)
        throw std::invalid_argument("out_d2 must be > 0");
    if (out_d1 == 0 || out_d1 < -1)
        throw std::invalid_argument("out_d1 must be > 0 or -1 to infer");

    // Compute totals using long long to avoid overflow
    long long in_total = static_cast<long long>(in.d0) * in.d1 * in.d2 * in.d3;

    // If out_d1 == -1, infer it: out_d1 = in_total / (out_d0 * out_d2)
    long long denom = static_cast<long long>(out_d0) * out_d2;
    if (denom == 0)
        throw std::invalid_argument("invalid output dims (denominator zero)");
    if (out_d1 == -1)
    {
        if (in_total % denom != 0)
            throw std::invalid_argument("cannot infer out_d1: sizes not divisible");
        long long inferred = in_total / denom;
        if (inferred > static_cast<long long>(std::numeric_limits<int>::max()))
            throw std::overflow_error("inferred out_d1 too large");
        out_d1 = static_cast<int>(inferred);
    }

    // Now check totals match
    long long out_total = static_cast<long long>(out_d0) * out_d1 * out_d2;
    if (in_total != out_total)
        throw std::invalid_argument("total number of elements must match for reshape");

    // Prepare output
    Shape_3D<float> out;
    out.d0 = out_d0;
    out.d1 = out_d1;
    out.d2 = out_d2;
    out.buf.resize(static_cast<size_t>(out_total));

    // For contiguous -> contiguous reshape the linear order is identical: copy buffer
    std::copy(in.buf.begin(), in.buf.end(), out.buf.begin());

    return out;
}

// Create Shape_6D from Shape_4D using explicit target dims:
// target dims must be passed in the order: (B_, Hblk, r, Hblk, r, C)
// where Hblk == H / r
Shape_6D<float> reshape_4d_to_6d(
        const Shape_4D<float> &sub_img, // expected layout: (B_, H, H, C)
        int out_d0, // B_
        int out_d1, // H // r
        int out_d2, // r
        int out_d3, // H // r
        int out_d4, // r
        int out_d5  // C
)
{
    // Basic validation of non-negative dims
    if (out_d0 <= 0 || out_d1 <= 0 || out_d2 <= 0 || out_d3 <= 0 || out_d4 <= 0 || out_d5 <= 0)
    {
        throw std::invalid_argument("all output dims must be > 0");
    }

    // Validate input dims match expected flattened size
    // Input expected: d0 == out_d0, d1 == out_d1*out_d2, d2 == out_d3*out_d4, d3 == out_d5
    int expected_H = out_d1 * out_d2; // H
    int expected_H2 = out_d3 * out_d4; // H (should equal expected_H)
    if (expected_H != expected_H2)
    {
        throw std::invalid_argument("inconsistent H: out_d1*out_d2 must equal out_d3*out_d4");
    }
    int expected_C = out_d5;

    if (sub_img.d0 != out_d0 || sub_img.d1 != expected_H || sub_img.d2 != expected_H2 || sub_img.d3 != expected_C)
    {
        throw std::invalid_argument("sub_img dimensions must be (B_, H, H, C) matching the provided target dims");
    }

    // Prepare output container
    Shape_6D<float> out;
    out.d0 = out_d0;
    out.d1 = out_d1;
    out.d2 = out_d2;
    out.d3 = out_d3;
    out.d4 = out_d4;
    out.d5 = out_d5;
    out.buf.resize(static_cast<size_t>(out.d0) * out.d1 * out.d2 * out.d3 * out.d4 * out.d5);

    // Index helpers (row-major)
    auto idx4 = [&](int i0, int i1, int i2, int i3) -> size_t
    {
        return static_cast<size_t>((((i0 * sub_img.d1 + i1) * sub_img.d2 + i2) * sub_img.d3) + i3);
    };
    auto idx6 = [&](int i0, int i1, int i2, int i3, int i4, int i5) -> size_t
    {
        return static_cast<size_t>(((((((i0 * out.d1 + i1) * out.d2 + i2) * out.d3 + i3) * out.d4 + i4) * out.d5) + i5));
    };

    // Mapping:
    // For each b,h,w,c:
    //   h1 = h / r  (out_d1)
    //   r1 = h % r  (out_d2)
    //   h2 = w / r  (out_d3)
    //   r2 = w % r  (out_d4)
    //   out[b,h1,r1,h2,r2,c] = sub_img[b,h,w,c]
    int B_ = out.d0;
    int H = expected_H; // H = out_d1 * out_d2
    int r = out.d2;     // out_d2 == out_d4 (should be equal by earlier check)
    int C = out.d5;

    for (int b = 0; b < B_; ++b)
    {
        for (int h = 0; h < H; ++h)
        {
            int h1 = h / r;
            int r1 = h % r;
            for (int w = 0; w < H; ++w)
            {
                int h2 = w / r;
                int r2 = w % r;
                for (int c = 0; c < C; ++c)
                {
                    size_t s = idx4(b, h, w, c);
                    size_t d = idx6(b, h1, r1, h2, r2, c);
                    out.buf[d] = sub_img.buf[s];
                }
            }
        }
    }

    return out;
}

template<typename T>
Shape_4D<T> MaskedFillSelf(const Shape_4D<bool> &mask, T value)
{
    Shape_4D<T> tensor;
    tensor.d0 = mask.d0;
    tensor.d1 = mask.d1;
    tensor.d2 = mask.d2;
    tensor.d3 = mask.d3;
    tensor.buf.resize(mask.buf.size(), 0);

    long long total = static_cast<long long>(tensor.d0) * tensor.d1 * tensor.d2 * tensor.d3;
    for (long long i = 0; i < total; ++i)
    {
        if (mask.buf[static_cast<size_t>(i)])
            tensor.buf[static_cast<size_t>(i)] = value;
    }
    return tensor;
}

// Return a new Shape_4D<bool> where each element is (in != 0)
Shape_4D<bool> ToBool(const Shape_4D<float> &in)
{
    if (in.d0 <= 0 || in.d1 <= 0 || in.d2 <= 0 || in.d3 <= 0)
        throw std::invalid_argument("input dims must be > 0");

    long long total = static_cast<long long>(in.d0) * in.d1 * in.d2 * in.d3;
    if (static_cast<long long>(in.buf.size()) != total)
        throw std::invalid_argument("input buffer size does not match dimensions");

    Shape_4D<bool> out;
    out.d0 = in.d0;
    out.d1 = in.d1;
    out.d2 = in.d2;
    out.d3 = in.d3;
    out.buf.resize(static_cast<size_t>(total));

    for (long long i = 0; i < total; ++i)
        out.buf[static_cast<size_t>(i)] = (in.buf[static_cast<size_t>(i)] != 0.0f);

    return out;
}

template<typename T>
Shape_4D<T> InvertMask(const Shape_4D<T> &in)
{
    if (in.d0 <= 0 || in.d1 <= 0 || in.d2 <= 0 || in.d3 <= 0)
        throw std::invalid_argument("input dims must be > 0");

    long long total = static_cast<long long>(in.d0) * in.d1 * in.d2 * in.d3;
    if (static_cast<long long>(in.buf.size()) != total)
        throw std::invalid_argument("input buffer size does not match dimensions");

    Shape_4D<T> out;
    out.d0 = in.d0;
    out.d1 = in.d1;
    out.d2 = in.d2;
    out.d3 = in.d3;
    out.buf.resize(static_cast<size_t>(total));

    std::transform(in.buf.begin(), in.buf.end(), out.buf.begin(),
                   [](const T &v) { return static_cast<T>(1) - v; });

    return out;
}

template<typename T>
Shape_4D<T> Expand(
        const Shape_4D<T> &in,
        int out_d0,
        int out_d1,
        int out_d2,
        int out_d3
)
{
    // Validate input dims
    if (in.d0 <= 0 || in.d1 <= 0 || in.d2 <= 0 || in.d3 <= 0)
        throw std::invalid_argument("input dims must be > 0");

    // Interpret -1 as "keep input size"
    if (out_d0 == -1)
        out_d0 = in.d0;
    if (out_d1 == -1)
        out_d1 = in.d1;
    if (out_d2 == -1)
        out_d2 = in.d2;
    if (out_d3 == -1)
        out_d3 = in.d3;

    // Validate output dims
    if (out_d0 <= 0 || out_d1 <= 0 || out_d2 <= 0 || out_d3 <= 0)
        throw std::invalid_argument("output dims must be > 0");

    // Check broadcastability: if input dim != output dim, input dim must be 1
    if ((in.d0 != out_d0 && in.d0 != 1) ||
        (in.d1 != out_d1 && in.d1 != 1) ||
        (in.d2 != out_d2 && in.d2 != 1) ||
        (in.d3 != out_d3 && in.d3 != 1))
    {
        throw std::invalid_argument("incompatible sizes for expand: non-singleton dimension cannot be expanded");
    }

    // Compute totals (use long long to avoid overflow)
    long long in_total = static_cast<long long>(in.d0) * in.d1 * in.d2 * in.d3;
    long long out_total = static_cast<long long>(out_d0) * out_d1 * out_d2 * out_d3;

    // Sanity: input buffer size must match in_total
    if (static_cast<long long>(in.buf.size()) != in_total)
        throw std::invalid_argument("input buffer size does not match input dimensions");

    // Prepare output
    Shape_4D<T> out;
    out.d0 = out_d0;
    out.d1 = out_d1;
    out.d2 = out_d2;
    out.d3 = out_d3;
    out.buf.resize(static_cast<size_t>(out_total));

    // If shapes are identical, just copy
    if (in.d0 == out_d0 && in.d1 == out_d1 && in.d2 == out_d2 && in.d3 == out_d3)
    {
        std::copy(in.buf.begin(), in.buf.end(), out.buf.begin());
        return out;
    }

    // Precompute input strides (row-major)
    long long in_stride0 = static_cast<long long>(in.d1) * in.d2 * in.d3;
    long long in_stride1 = static_cast<long long>(in.d2) * in.d3;
    long long in_stride2 = static_cast<long long>(in.d3);
    // in_stride3 = 1

    // Precompute output strides (row-major)
    long long out_stride0 = static_cast<long long>(out.d1) * out.d2 * out.d3;
    long long out_stride1 = static_cast<long long>(out.d2) * out.d3;
    long long out_stride2 = static_cast<long long>(out.d3);
    // out_stride3 = 1

    // Fill output by mapping each output index to the corresponding input index
    for (int i0 = 0; i0 < out.d0; ++i0)
    {
        int si0 = (in.d0 == 1) ? 0 : i0;
        long long out_off0 = static_cast<long long>(i0) * out_stride0;
        long long in_off0 = static_cast<long long>(si0) * in_stride0;

        for (int i1 = 0; i1 < out.d1; ++i1)
        {
            int si1 = (in.d1 == 1) ? 0 : i1;
            long long out_off1 = out_off0 + static_cast<long long>(i1) * out_stride1;
            long long in_off1 = in_off0 + static_cast<long long>(si1) * in_stride1;

            for (int i2 = 0; i2 < out.d2; ++i2)
            {
                int si2 = (in.d2 == 1) ? 0 : i2;
                long long out_off2 = out_off1 + static_cast<long long>(i2) * out_stride2;
                long long in_off2 = in_off1 + static_cast<long long>(si2) * in_stride2;

                for (int i3 = 0; i3 < out.d3; ++i3)
                {
                    int si3 = (in.d3 == 1) ? 0 : i3;
                    long long dst_idx = out_off2 + i3;
                    long long src_idx = in_off2 + si3;

                    out.buf[static_cast<size_t>(dst_idx)] = in.buf[static_cast<size_t>(src_idx)];
                }
            }
        }
    }

    return out;
}

#endif //SHAPE_4D_H
