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

Shape_4D<float> concat_shape4d(const Shape_4D<float> &A, const Shape_4D<float> &B, int dim)
{
    if (dim < 0 || dim > 3)
        throw std::invalid_argument("dim must be 0..3");
    if (A.d0 <= 0 || A.d1 <= 0 || A.d2 <= 0 || A.d3 <= 0)
        throw std::invalid_argument("A dims must be > 0");
    if (B.d0 <= 0 || B.d1 <= 0 || B.d2 <= 0 || B.d3 <= 0)
        throw std::invalid_argument("B dims must be > 0");

    if (dim != 0 && A.d0 != B.d0)
        throw std::invalid_argument("mismatch on axis 0");
    if (dim != 1 && A.d1 != B.d1)
        throw std::invalid_argument("mismatch on axis 1");
    if (dim != 2 && A.d2 != B.d2)
        throw std::invalid_argument("mismatch on axis 2");
    if (dim != 3 && A.d3 != B.d3)
        throw std::invalid_argument("mismatch on axis 3");

    Shape_4D<float> out;
    out.d0 = (dim == 0) ? (A.d0 + B.d0) : A.d0;
    out.d1 = (dim == 1) ? (A.d1 + B.d1) : A.d1;
    out.d2 = (dim == 2) ? (A.d2 + B.d2) : A.d2;
    out.d3 = (dim == 3) ? (A.d3 + B.d3) : A.d3;

    long long total = static_cast<long long>(out.d0) * out.d1 * out.d2 * out.d3;
    out.buf.resize(static_cast<size_t>(total));

    auto idx = [](int i0, int i1, int i2, int i3, int D1, int D2, int D3) -> size_t
    {
        return static_cast<size_t>((((i0 * D1 + i1) * D2 + i2) * D3) + i3);
    };

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

template<typename T>
Shape_3D<T> Slice4DTo3D(
        const Shape_4D<T> &in,
        int idx0,
        int start1, int end1, int step1,
        int start2, int end2, int step2,
        int start3, int end3, int step3)
{

    if (in.d0 <= 0 || in.d1 <= 0 || in.d2 <= 0 || in.d3 <= 0)
        throw std::invalid_argument("input dims must be > 0");

    if (idx0 < 0 || idx0 >= in.d0)
        throw std::out_of_range("idx0 out of range");

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

    int out0 = compute_len(start1, end1, step1);
    int out1 = compute_len(start2, end2, step2);
    int out2 = compute_len(start3, end3, step3);

    long long total = static_cast<long long>(out0) * out1 * out2;
    if (total < 0)
        throw std::overflow_error("output size overflow");

    Shape_3D<T> out;
    out.d0 = out0;
    out.d1 = out1;
    out.d2 = out2;
    out.buf.resize(static_cast<size_t>(total));

    auto in_index = [&](int i0, int i1, int i2, int i3) -> size_t
    {
        return static_cast<size_t>((((i0 * in.d1 + i1) * in.d2 + i2) * in.d3) + i3);
    };
    auto out_index = [&](int i0, int i1, int i2) -> size_t
    {
        return static_cast<size_t>(((i0 * out.d1 + i1) * out.d2) + i2);
    };

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

Shape_4D<float> slice_shape4d_range(
        const Shape_4D<float> &in,
        int start0, int end0, int step0,
        int start1, int end1, int step1,
        int start2, int end2, int step2,
        int start3, int end3, int step3
)
{

    if (in.d0 <= 0 || in.d1 <= 0 || in.d2 <= 0 || in.d3 <= 0)
        throw std::invalid_argument("input dims must be > 0");

    if (end0 == -1)
        end0 = in.d0;
    if (end1 == -1)
        end1 = in.d1;
    if (end2 == -1)
        end2 = in.d2;
    if (end3 == -1)
        end3 = in.d3;

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

    long long total = static_cast<long long>(out0) * out1 * out2 * out3;

    Shape_4D<float> out;
    out.d0 = out0;
    out.d1 = out1;
    out.d2 = out2;
    out.d3 = out3;
    out.buf.resize(static_cast<size_t>(total));

    auto in_index = [&](int i0, int i1, int i2, int i3) -> size_t
    {
        return static_cast<size_t>((((i0 * in.d1 + i1) * in.d2 + i2) * in.d3) + i3);
    };
    auto out_index = [&](int i0, int i1, int i2, int i3) -> size_t
    {
        return static_cast<size_t>((((i0 * out.d1 + i1) * out.d2 + i2) * out.d3) + i3);
    };

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

Shape_4D<float> repeat_shape4d(const Shape_4D_View<float> &in, int r0, int r1, int r2, int r3)
{

    if (in.d0 <= 0 || in.d1 <= 0 || in.d2 <= 0 || in.d3 <= 0)
        throw std::invalid_argument("input dims must be > 0");
    if (r0 <= 0 || r1 <= 0 || r2 <= 0 || r3 <= 0)
        throw std::invalid_argument("repeat factors must be > 0");

    long long out_d0 = static_cast<long long>(in.d0) * r0;
    long long out_d1 = static_cast<long long>(in.d1) * r1;
    long long out_d2 = static_cast<long long>(in.d2) * r2;
    long long out_d3 = static_cast<long long>(in.d3) * r3;
    long long out_total = out_d0 * out_d1 * out_d2 * out_d3;

    Shape_4D<float> out;
    out.d0 = static_cast<int>(out_d0);
    out.d1 = static_cast<int>(out_d1);
    out.d2 = static_cast<int>(out_d2);
    out.d3 = static_cast<int>(out_d3);
    out.buf.resize(static_cast<size_t>(out_total));

    const size_t in_s0 = static_cast<size_t>(in.d1) * in.d2 * in.d3;
    const size_t in_s1 = static_cast<size_t>(in.d2) * in.d3;
    const size_t in_s2 = static_cast<size_t>(in.d3);
    const size_t in_s3 = 1;

    const size_t out_s0 = static_cast<size_t>(out.d1) * out.d2 * out.d3;
    const size_t out_s1 = static_cast<size_t>(out.d2) * out.d3;
    const size_t out_s2 = static_cast<size_t>(out.d3);
    const size_t out_s3 = 1;

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

Shape_4D<float> permute_shape4d(Shape_4D<float> &in, std::array<int, 4> perm)
{

    std::array<int, 4> check = perm;
    std::sort(check.begin(), check.end());
    for (int i = 0; i < 4; ++i)
    {
        if (check[i] != i)
            throw std::invalid_argument("perm must be a permutation of 0..3");
    }

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

    Shape_4D<float> out;
    out.d0 = dim(in, perm[0]);
    out.d1 = dim(in, perm[1]);
    out.d2 = dim(in, perm[2]);
    out.d3 = dim(in, perm[3]);

    const size_t out_size = static_cast<size_t>(out.d0) * out.d1 * out.d2 * out.d3;
    out.buf.resize(out_size);

    std::array<int, 4> inv;
    for (int new_ax = 0; new_ax < 4; ++new_ax)
        inv[perm[new_ax]] = new_ax;

    auto idx_in = [&](int i0, int i1, int i2, int i3) -> size_t
    {
        return static_cast<size_t>((((i0 * in.d1 + i1) * in.d2 + i2) * in.d3) + i3);
    };
    auto idx_out = [&](int i0, int i1, int i2, int i3) -> size_t
    {
        return static_cast<size_t>((((i0 * out.d1 + i1) * out.d2 + i2) * out.d3) + i3);
    };

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

Shape_3D<float> reshape_4d_to_3d(
        Shape_4D<float> &in,
        int out_d0,
        int out_d1,
        int out_d2
)
{

    if (in.d0 <= 0 || in.d1 <= 0 || in.d2 <= 0 || in.d3 <= 0)
        throw std::invalid_argument("input dims must be > 0");

    if (out_d0 <= 0)
        throw std::invalid_argument("out_d0 must be > 0");
    if (out_d2 <= 0)
        throw std::invalid_argument("out_d2 must be > 0");
    if (out_d1 == 0 || out_d1 < -1)
        throw std::invalid_argument("out_d1 must be > 0 or -1 to infer");

    long long in_total = static_cast<long long>(in.d0) * in.d1 * in.d2 * in.d3;

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

    long long out_total = static_cast<long long>(out_d0) * out_d1 * out_d2;
    if (in_total != out_total)
        throw std::invalid_argument("total number of elements must match for reshape");

    Shape_3D<float> out;
    out.d0 = out_d0;
    out.d1 = out_d1;
    out.d2 = out_d2;
    out.buf.resize(static_cast<size_t>(out_total));

    std::copy(in.buf.begin(), in.buf.end(), out.buf.begin());

    return out;
}

Shape_6D<float> reshape_4d_to_6d(
        const Shape_4D<float> &sub_img,
        int out_d0,
        int out_d1,
        int out_d2,
        int out_d3,
        int out_d4,
        int out_d5
)
{

    if (out_d0 <= 0 || out_d1 <= 0 || out_d2 <= 0 || out_d3 <= 0 || out_d4 <= 0 || out_d5 <= 0)
    {
        throw std::invalid_argument("all output dims must be > 0");
    }

    int expected_H = out_d1 * out_d2;
    int expected_H2 = out_d3 * out_d4;
    if (expected_H != expected_H2)
    {
        throw std::invalid_argument("inconsistent H: out_d1*out_d2 must equal out_d3*out_d4");
    }
    int expected_C = out_d5;

    if (sub_img.d0 != out_d0 || sub_img.d1 != expected_H || sub_img.d2 != expected_H2 || sub_img.d3 != expected_C)
    {
        throw std::invalid_argument("sub_img dimensions must be (B_, H, H, C) matching the provided target dims");
    }

    Shape_6D<float> out;
    out.d0 = out_d0;
    out.d1 = out_d1;
    out.d2 = out_d2;
    out.d3 = out_d3;
    out.d4 = out_d4;
    out.d5 = out_d5;
    out.buf.resize(static_cast<size_t>(out.d0) * out.d1 * out.d2 * out.d3 * out.d4 * out.d5);

    auto idx4 = [&](int i0, int i1, int i2, int i3) -> size_t
    {
        return static_cast<size_t>((((i0 * sub_img.d1 + i1) * sub_img.d2 + i2) * sub_img.d3) + i3);
    };
    auto idx6 = [&](int i0, int i1, int i2, int i3, int i4, int i5) -> size_t
    {
        return static_cast<size_t>(((((((i0 * out.d1 + i1) * out.d2 + i2) * out.d3 + i3) * out.d4 + i4) * out.d5) + i5));
    };

    int B_ = out.d0;
    int H = expected_H;
    int r = out.d2;
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
Shape_4D<T> Expand(
        const Shape_4D<T> &in,
        int out_d0,
        int out_d1,
        int out_d2,
        int out_d3
)
{

    if (in.d0 <= 0 || in.d1 <= 0 || in.d2 <= 0 || in.d3 <= 0)
        throw std::invalid_argument("input dims must be > 0");

    if (out_d0 == -1)
        out_d0 = in.d0;
    if (out_d1 == -1)
        out_d1 = in.d1;
    if (out_d2 == -1)
        out_d2 = in.d2;
    if (out_d3 == -1)
        out_d3 = in.d3;

    if (out_d0 <= 0 || out_d1 <= 0 || out_d2 <= 0 || out_d3 <= 0)
        throw std::invalid_argument("output dims must be > 0");

    if ((in.d0 != out_d0 && in.d0 != 1) ||
        (in.d1 != out_d1 && in.d1 != 1) ||
        (in.d2 != out_d2 && in.d2 != 1) ||
        (in.d3 != out_d3 && in.d3 != 1))
    {
        throw std::invalid_argument("incompatible sizes for expand: non-singleton dimension cannot be expanded");
    }

    long long in_total = static_cast<long long>(in.d0) * in.d1 * in.d2 * in.d3;
    long long out_total = static_cast<long long>(out_d0) * out_d1 * out_d2 * out_d3;

    if (static_cast<long long>(in.buf.size()) != in_total)
        throw std::invalid_argument("input buffer size does not match input dimensions");

    Shape_4D<T> out;
    out.d0 = out_d0;
    out.d1 = out_d1;
    out.d2 = out_d2;
    out.d3 = out_d3;
    out.buf.resize(static_cast<size_t>(out_total));

    if (in.d0 == out_d0 && in.d1 == out_d1 && in.d2 == out_d2 && in.d3 == out_d3)
    {
        std::copy(in.buf.begin(), in.buf.end(), out.buf.begin());
        return out;
    }

    long long in_stride0 = static_cast<long long>(in.d1) * in.d2 * in.d3;
    long long in_stride1 = static_cast<long long>(in.d2) * in.d3;
    long long in_stride2 = static_cast<long long>(in.d3);

    long long out_stride0 = static_cast<long long>(out.d1) * out.d2 * out.d3;
    long long out_stride1 = static_cast<long long>(out.d2) * out.d3;
    long long out_stride2 = static_cast<long long>(out.d3);

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

#endif
