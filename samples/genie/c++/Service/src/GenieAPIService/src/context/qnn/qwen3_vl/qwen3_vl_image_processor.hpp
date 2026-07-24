//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once
/*
 * qwen3_vl_image_processor.hpp
 *
 * 封装：float全流程解码图片 → Alpha白底合成(float空间) → smart_resize(按32对齐) →
 *      float缩放 → patch展开并归一化到[-1,1] → 写出buffer
 * 说明：
 *  1) 依赖 stb_image / stb_image_resize2（头文件实现库），STB_IMAGE_IMPLEMENTATION /
 *     STB_IMAGE_RESIZE2_IMPLEMENTATION 已由 phi4mm.cpp 在同一链接目标内定义一次，
 *     本文件不重复定义。
 *  2) 图像解码/缩放全程走 float 通道（同 phi4mm.cpp 的 stbi_loadf + stbir_quick_resize_helper
 *     流水线），避免"先转 uint8 再手动归一化"引入的二次量化损失。
 *  3) patchify 展平顺序（外层 ho,wo,mh,mw；内层 c,t,ph,pw）与 qwen25_image_processor.hpp
 *     一致（数学公式同源，仅 PATCH_SIZE/MEAN/STD/对齐因子不同）。
 */

#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <stdexcept>
#include <utility>
#include <vector>

#include <stb_image.h>
#include <stb_image_resize2.h>

namespace qwen3_vl
{
    class Qwen3VLImageProcessor
    {
    public:
        // --- 常量：与 Qwen3-VL preprocessor_config.json / vision_config 一致 ---
        static constexpr int PATCH_SIZE = 16;
        static constexpr int TEMPORAL_PATCH_SIZE = 2;
        static constexpr int MERGE_SIZE = 2;
        static constexpr int SPATIAL_MERGE_SIZE = 2;
        static constexpr int PATCH_FACTOR = PATCH_SIZE * SPATIAL_MERGE_SIZE; // =32，对齐因子

        // 纯线性映射到[-1,1]（与 CLIP mean/std 不同）
        static constexpr float MEAN[3] = {0.5f, 0.5f, 0.5f};
        static constexpr float STD[3] = {0.5f, 0.5f, 0.5f};

        // 用于承载 float [0,1] 空间的 RGB(A) 图像
        struct ImageF32
        {
            int w = 0, h = 0, c = 0; // c=3或4
            std::vector<float> data; // 行主序，交错排列
        };

    public:
        Qwen3VLImageProcessor() = default;

        void ProcessToBuffer(uint8_t *png_bin_buf,
                             unsigned long dwLen,
                             int request_h,
                             int request_w,
                             std::vector<float> &out,
                             int &rows, int &cols) const
        {
            ImageF32 img = LoadImageF32_STB(png_bin_buf, dwLen);
            ImageF32 rgb = ToRGBWhiteBackground(img);
            auto [rh, rw] = smart_resize(request_h, request_w, PATCH_FACTOR);
            ImageF32 rgb_resized = ResizeRGB_STB(rgb, rw, rh);
            GeneratePixelValuesToBuffer(rgb_resized, out, rows, cols);
        }

        // 仅计算 smart_resize 的结果尺寸（便于调用方预估）
        static std::pair<int, int> SmartResizeOnly(int request_h, int request_w)
        {
            return smart_resize(request_h, request_w, PATCH_FACTOR);
        }

    private:
        // --- 工具函数（与 qwen25_image_processor.hpp 相同的数学公式，仅对齐因子不同） ---
        static inline int round_by_factor(int number, int factor)
        {
            return static_cast<int>(std::llround(static_cast<double>(number) / factor) * factor);
        }

        static inline int ceil_by_factor(int number, int factor)
        {
            return static_cast<int>(std::ceil(static_cast<double>(number) / factor) * factor);
        }

        static inline int floor_by_factor(int number, int factor)
        {
            return static_cast<int>(std::floor(static_cast<double>(number) / factor) * factor);
        }

        static std::pair<int, int> smart_resize_impl(
                int height, int width, int factor,
                int min_pixels, int max_pixels)
        {
            const double max_ratio = 200.0;
            if (static_cast<double>(std::max(height, width)) / std::min(height, width) > max_ratio)
            {
                throw std::runtime_error("aspect ratio too large");
            }

            int h_bar = std::max(factor, round_by_factor(height, factor));
            int w_bar = std::max(factor, round_by_factor(width, factor));
            long long area = 1LL * h_bar * w_bar;

            if (area > max_pixels)
            {
                double beta = std::sqrt((static_cast<double>(height) * width) / max_pixels);
                h_bar = floor_by_factor(static_cast<int>(height / beta), factor);
                w_bar = floor_by_factor(static_cast<int>(width / beta), factor);
            }
            else
                if (area < min_pixels)
                {
                    double beta =
                            std::sqrt(static_cast<double>(min_pixels) / (static_cast<double>(height) * width));
                    h_bar = ceil_by_factor(static_cast<int>(height * beta), factor);
                    w_bar = ceil_by_factor(static_cast<int>(width * beta), factor);
                }
            return {h_bar, w_bar};
        }

        static inline std::pair<int, int> smart_resize(int height, int width, int factor)
        {
            const int min_pixels = 4 * factor * factor;
            const int max_pixels = 16384 * factor * factor;
            return smart_resize_impl(height, width, factor, min_pixels, max_pixels);
        }

        // 解码为 float [0,1] 像素：stbi_ldr_to_hdr_gamma(1.0f) 关闭默认 gamma 变换
        // （与 phi4mm.cpp 的 float 解码流水线一致），仅在这一次解码期间临时切换该全局状态。
        static ImageF32 LoadImageF32_STB(uint8_t *buf, unsigned long dwLen)
        {
            stbi_ldr_to_hdr_gamma(1.0f);
            int w = 0, h = 0, n = 0;
            float *pixels = stbi_loadf_from_memory(buf, dwLen, &w, &h, &n, 0);
            stbi_ldr_to_hdr_gamma(2.2f);
            if (!pixels) { throw std::runtime_error("stb_image: failed to decode image"); }

            ImageF32 out;
            if (n == 3)
            {
                out.w = w;
                out.h = h;
                out.c = 3;
                out.data.assign(pixels, pixels + static_cast<size_t>(w) * h * 3);
            }
            else
                if (n == 4)
                {
                    out.w = w;
                    out.h = h;
                    out.c = 4;
                    out.data.assign(pixels, pixels + static_cast<size_t>(w) * h * 4);
                }
                else
                    if (n == 1)
                    {
                        // 灰度 → RGB
                        out.w = w;
                        out.h = h;
                        out.c = 3;
                        out.data.resize(static_cast<size_t>(w) * h * 3);
                        for (size_t i = 0; i < static_cast<size_t>(w) * h; ++i)
                        {
                            float g = pixels[i];
                            out.data[i * 3 + 0] = g;
                            out.data[i * 3 + 1] = g;
                            out.data[i * 3 + 2] = g;
                        }
                    }
                    else
                        if (n == 2)
                        {
                            // 灰度+Alpha → RGBA
                            out.w = w;
                            out.h = h;
                            out.c = 4;
                            out.data.resize(static_cast<size_t>(w) * h * 4);
                            for (size_t i = 0; i < static_cast<size_t>(w) * h; ++i)
                            {
                                float g = pixels[i * 2 + 0];
                                float a = pixels[i * 2 + 1];
                                out.data[i * 4 + 0] = g;
                                out.data[i * 4 + 1] = g;
                                out.data[i * 4 + 2] = g;
                                out.data[i * 4 + 3] = a;
                            }
                        }
                        else
                        {
                            stbi_image_free(pixels);
                            throw std::runtime_error("Unsupported channel count from stb_image");
                        }
            stbi_image_free(pixels);
            return out;
        }

        // RGBA → RGB 的白底合成（float [0,1] 空间）：c=3直接返回，c=4执行 alpha over 白色。
        static ImageF32 ToRGBWhiteBackground(const ImageF32 &in)
        {
            if (in.c == 3) return in;
            if (in.c != 4) throw std::runtime_error("ToRGBWhiteBackground: unsupported channels");

            ImageF32 out;
            out.w = in.w;
            out.h = in.h;
            out.c = 3;
            out.data.resize(static_cast<size_t>(out.w) * out.h * 3);
            for (int i = 0; i < in.w * in.h; ++i)
            {
                float r = in.data[i * 4 + 0];
                float g = in.data[i * 4 + 1];
                float b = in.data[i * 4 + 2];
                float a = in.data[i * 4 + 3];
                out.data[i * 3 + 0] = r * a + 1.0f * (1.0f - a);
                out.data[i * 3 + 1] = g * a + 1.0f * (1.0f - a);
                out.data[i * 3 + 2] = b * a + 1.0f * (1.0f - a);
            }
            return out;
        }

        // 使用 stb_image_resize2 的 medium api（stbir_resize，跨编译单元可用，不依赖实现宏）
        // 进行缩放（float RGB）。注意：必须用 STBIR_FILTER_CATMULLROM，不能用 STBIR_FILTER_MITCHELL
        // —— 在特定放大比例下会导致 stb 库内部越界访问，必现崩溃（Phi4-mm 已踩过的坑）。
        static ImageF32 ResizeRGB_STB(const ImageF32 &in, int out_w, int out_h)
        {
            if (in.c != 3) throw std::runtime_error("ResizeRGB_STB expects RGB input");
            ImageF32 out;
            out.w = out_w;
            out.h = out_h;
            out.c = 3;
            out.data.resize(static_cast<size_t>(out_w) * out_h * 3);

            if (!stbir_resize(
                    in.data.data(), in.w, in.h, 0,
                    out.data.data(), out_w, out_h, 0,
                    STBIR_RGB, STBIR_TYPE_FLOAT, STBIR_EDGE_REFLECT, STBIR_FILTER_CATMULLROM))
            {
                throw std::runtime_error("stb_image_resize: resize failed");
            }
            return out;
        }

        // 生成 pixel_values（float32）：patchify 展平顺序与 qwen25_image_processor.hpp 一致
        // （外层 ho,wo,mh,mw；内层 c,t,ph,pw），仅 patch_size 与归一化公式不同。
        static void GeneratePixelValuesToBuffer(const ImageF32 &rgb,
                                                std::vector<float> &out,
                                                int &rows, int &cols)
        {
            assert(rgb.c == 3);
            const int H = rgb.h;
            const int W = rgb.w;
            if (H % PATCH_SIZE != 0 || W % PATCH_SIZE != 0)
            {
                throw std::runtime_error("H/W not divisible by patch_size");
            }

            const int grid_h = H / PATCH_SIZE;
            const int grid_w = W / PATCH_SIZE;
            const int grid_h_outer = grid_h / MERGE_SIZE;
            const int grid_w_outer = grid_w / MERGE_SIZE;
            const int T = TEMPORAL_PATCH_SIZE;

            // 归一化并复制到两个时间帧（t=0与t=1相同），像素已是 float [0,1]，无需再除以255。
            std::vector<float> norm(static_cast<size_t>(T) * 3 * static_cast<size_t>(H) * static_cast<size_t>(W));
            auto idx4 = [&](int t, int c, int y, int x)
            {
                return (static_cast<size_t>(t) * 3 + c) * static_cast<size_t>(H) * static_cast<size_t>(W)
                       + static_cast<size_t>(y) * static_cast<size_t>(W) + static_cast<size_t>(x);
            };
            for (int y = 0; y < H; ++y)
            {
                for (int x = 0; x < W; ++x)
                {
                    const float *p = &rgb.data[(y * W + x) * 3];
                    for (int c = 0; c < 3; ++c)
                    {
                        float v = (p[c] - MEAN[c]) / STD[c];
                        norm[idx4(0, c, y, x)] = v;
                        norm[idx4(1, c, y, x)] = v;
                    }
                }
            }

            // 展平顺序：外层 ho, wo, mh, mw；内层 c, t, ph, pw —— 与qwen25_image_processor.hpp一致。
            rows = 1 * grid_h * grid_w; // grid_t=1
            cols = 3 * T * PATCH_SIZE * PATCH_SIZE;
            out.assign(static_cast<size_t>(rows) * cols, 0.0f);

            int r = 0;
            for (int ho = 0; ho < grid_h_outer; ++ho)
            {
                for (int wo = 0; wo < grid_w_outer; ++wo)
                {
                    for (int mh = 0; mh < MERGE_SIZE; ++mh)
                    {
                        for (int mw = 0; mw < MERGE_SIZE; ++mw)
                        {
                            int py0 = (ho * MERGE_SIZE + mh) * PATCH_SIZE;
                            int px0 = (wo * MERGE_SIZE + mw) * PATCH_SIZE;

                            int k = 0;
                            for (int c = 0; c < 3; ++c)
                            {
                                for (int t = 0; t < T; ++t)
                                {
                                    for (int ph = 0; ph < PATCH_SIZE; ++ph)
                                    {
                                        for (int pw = 0; pw < PATCH_SIZE; ++pw)
                                        {
                                            out[static_cast<size_t>(r) * cols + k] =
                                                    norm[idx4(t, c, py0 + ph,
                                                              px0 + pw)];
                                            ++k;
                                        }
                                    }
                                }
                            }
                            ++r;
                        }
                    }
                }
            }
            assert(r == rows);
        }
    };
}
