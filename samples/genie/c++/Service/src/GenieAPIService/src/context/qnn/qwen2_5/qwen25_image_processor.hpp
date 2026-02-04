//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once
/*
 * qwen25_image_processor.hpp
 *
 * 封装：读取图片 → Alpha白底合成 → smart_resize(按28对齐) → 缩放 → patch展开并CLIP归一化 → 写出raw
 * 说明：
 *  1) 依赖 stb_image / stb_image_resize2（头文件实现库），请在“一个且仅一个”编译单元中：
 *       #define STB_IMAGE_IMPLEMENTATION
 *       #define STB_IMAGE_RESIZE_IMPLEMENTATION
 *     然后 #include 本头文件；本头文件会包含 stb 的头文件。
 *  2) 所有逻辑与原始附件保持一致（常量、顺序、归一化等）。
 *
 *  cl /O2 /std:c++20 main.cpp /EHsc
 *  main.exe input.jpg pixel_values.raw
 */

//#define STB_IMAGE_IMPLEMENTATION
//#define STB_IMAGE_RESIZE_IMPLEMENTATION

#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

// stb headers（注意：需在某个.cpp里先#define STB_*_IMPLEMENTATION）

namespace qwen2_5
{
    class Qwen25ImageProcessor
    {
    public:
        // --- 常量：与原文件一致 ---
        static constexpr int PATCH_SIZE = 14; // 每个patch的尺寸（14x14）
        static constexpr int TEMPORAL_PATCH_SIZE = 2; // T=2（两个时间帧，复制同一帧）
        static constexpr int MERGE_SIZE = 2; // 外层merge尺寸=2（用于patch排序）
        static constexpr int SPATIAL_MERGE_SIZE = 2; // 常量（qwen_vl_utils）
        static constexpr int PATCH_FACTOR = PATCH_SIZE * SPATIAL_MERGE_SIZE; // =28，对齐因子

        // CLIP 的 mean/std（与原文件一致）
        static constexpr float MEAN[3] = {0.48145466f, 0.45782750f, 0.40821073f}; //
        static constexpr float STD[3] = {0.26862954f, 0.26130258f, 0.27577711f}; //

        // 用于承载RGB(A)图像
        struct ImageU8
        {
            int w = 0, h = 0, c = 0; // c=3或4
            std::vector<uint8_t> data; // 行主序，交错排列
        };

    public:
        Qwen25ImageProcessor() = default;

        // 高层API：读入图片并写出 pixel_values.raw
        // request_h / request_w 是“期望的输入尺寸”，会通过 smart_resize 对齐到 28 的倍数。
        void ProcessToRaw(const std::string &input_image_path,
                          const std::string &output_raw_path,
                          int request_h,
                          int request_w) const
        {
            // 1) 读取（多格式） → RGB(A)
            ImageU8 img = LoadImage_STB(input_image_path);

            // 2) Alpha白底合成 → RGB
            ImageU8 rgb = ToRGBWhiteBackground(img);

            // 3) smart_resize：将“请求的尺寸”调整到可被28整除，并满足像素上下限规则（与原实现一致）。
            auto [rh, rw] = smart_resize(request_h, request_w, PATCH_FACTOR);

            // 4) 用 stb_image_resize 进行缩放（线性）到 rh x rw。
            ImageU8 rgb_resized = ResizeRGB_STB(rgb, rw, rh);

            // 5) patch化+归一化+展平 → 写出 raw
            GeneratePixelValues(rgb_resized, output_raw_path);
        }

        // 可选：如果你希望获得内存中的结果，而不是直接写文件
        // 会返回 (rows, cols) 并把数据写入 out（float32，行主序）
        void ProcessToBuffer(const std::string &input_image_path,
                             int request_h,
                             int request_w,
                             std::vector<float> &out,
                             int &rows, int &cols) const
        {
            ImageU8 img = LoadImage_STB(input_image_path);
            ImageU8 rgb = ToRGBWhiteBackground(img);
            auto [rh, rw] = smart_resize(request_h, request_w, PATCH_FACTOR);
            ImageU8 rgb_resized = ResizeRGB_STB(rgb, rw, rh);
            GeneratePixelValuesToBuffer(rgb_resized, out, rows, cols);
        }

        void ProcessToBuffer(uint8_t *png_bin_buf,
                             unsigned long dwLen,
                             int request_h,
                             int request_w,
                             std::vector<float> &out,
                             int &rows, int &cols) const
        {
            ImageU8 img = LoadImage_STB(png_bin_buf, dwLen);
            ImageU8 rgb = ToRGBWhiteBackground(img);
            auto [rh, rw] = smart_resize(request_h, request_w, PATCH_FACTOR);
            ImageU8 rgb_resized = ResizeRGB_STB(rgb, rw, rh);
            GeneratePixelValuesToBuffer(rgb_resized, out, rows, cols);
        }

        // 仅计算 smart_resize 的结果尺寸（便于调用方预估）
        static std::pair<int, int> SmartResizeOnly(int request_h, int request_w)
        {
            return smart_resize(request_h, request_w, PATCH_FACTOR);
        }

    private:
        // --- 工具函数（与原始实现一致） ---
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
            const double max_ratio = 200.0; // 与原实现一致的长宽比限制
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
            const int min_pixels = 4 * factor * factor; // 与原实现一致：下限像素 = 4 * factor^2
            const int max_pixels = 16384 * factor * factor; // 与原实现一致：上限像素 = 16384 * factor^2
            return smart_resize_impl(height, width, factor, min_pixels, max_pixels);
        }

        // 读取图片（多格式）：返回 RGB 或 RGBA；灰度/灰度+Alpha 也统一到这两类（与原实现一致）。
        static ImageU8 LoadImage_STB(const std::string &path)
        {
            int w = 0, h = 0, n = 0; // n: 原始通道数（1/2/3/4）
            stbi_uc *pixels = stbi_load(path.c_str(), &w, &h, &n, 0);
            if (!pixels) { throw std::runtime_error(std::string("stb_image: failed to load image: ") + path); }

            ImageU8 out;
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
                            uint8_t g = pixels[i];
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
                                uint8_t g = pixels[i * 2 + 0];
                                uint8_t a = pixels[i * 2 + 1];
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

        static ImageU8 LoadImage_STB(uint8_t *png_bin_buf, unsigned long dwLen)
        {
            int w = 0, h = 0, n = 0;
            stbi_uc *pixels = stbi_load_from_memory(png_bin_buf, dwLen, &w, &h, &n, 0);
            if (!pixels) { throw std::runtime_error(std::string("stb_image: failed to load image: ")); }

            ImageU8 out;
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
                            uint8_t g = pixels[i];
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
                                uint8_t g = pixels[i * 2 + 0];
                                uint8_t a = pixels[i * 2 + 1];
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

        // RGBA → RGB 的白底合成（与原实现一致）：c=3直接返回，c=4执行 alpha over 白色。
        static ImageU8 ToRGBWhiteBackground(const ImageU8 &in)
        {
            if (in.c == 3) return in;
            if (in.c != 4) throw std::runtime_error("ToRGBWhiteBackground: unsupported channels");

            ImageU8 out;
            out.w = in.w;
            out.h = in.h;
            out.c = 3;
            out.data.resize(static_cast<size_t>(out.w) * out.h * 3);
            for (int i = 0; i < in.w * in.h; ++i)
            {
                uint8_t r = in.data[i * 4 + 0];
                uint8_t g = in.data[i * 4 + 1];
                uint8_t b = in.data[i * 4 + 2];
                uint8_t a = in.data[i * 4 + 3];
                float af = a / 255.0f;
                out.data[i * 3 + 0] = static_cast<uint8_t>(std::lround(r * af + 255.0f * (1.0f - af)));
                out.data[i * 3 + 1] = static_cast<uint8_t>(std::lround(g * af + 255.0f * (1.0f - af)));
                out.data[i * 3 + 2] = static_cast<uint8_t>(std::lround(b * af + 255.0f * (1.0f - af)));
            }
            return out;
        }

        // 使用 stb_image_resize 进行缩放（uint8 RGB，线性滤波）。
        static ImageU8 ResizeRGB_STB(const ImageU8 &in, int out_w, int out_h)
        {
            if (in.c != 3) throw std::runtime_error("ResizeRGB_STB expects RGB input");
            ImageU8 out;
            out.w = out_w;
            out.h = out_h;
            out.c = 3;
            out.data.resize(static_cast<size_t>(out_w) * out_h * 3);

            if (!stbir_resize_uint8_linear(
                    in.data.data(), in.w, in.h, 0,
                    out.data.data(), out_w, out_h, 0,
                    STBIR_RGB)) // 目标色彩空间：RGB
            {
                throw std::runtime_error("stb_image_resize: resize failed");
            }
            return out;
        }

        // 生成 pixel_values（float32），写文件；形状(rows, cols)与原实现一致。
        static void GeneratePixelValues(const ImageU8 &rgb, const std::string &out_path)
        {
            int rows = 0, cols = 0;
            std::vector<float> out;
            GeneratePixelValuesToBuffer(rgb, out, rows, cols);

            std::ofstream ofs(out_path, std::ios::binary);
            if (!ofs) throw std::runtime_error(std::string("Cannot open output: ") + out_path);
            ofs.write(reinterpret_cast<const char *>(out.data()),
                      static_cast<std::streamsize>(out.size() * sizeof(float)));
            ofs.close();

            // 你也可以在这里打印，但库函数通常不做IO；留给调用方处理。
            // std::cout << "pixel_values shape = (" << rows << "," << cols << ")\n";
            // std::cout << "bytes = " << (out.size() * sizeof(float)) << "\n";
        }

        // 生成到内存缓冲：便于自定义写出或进一步处理。
        static void GeneratePixelValuesToBuffer(const ImageU8 &rgb,
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

            const int grid_h = H / PATCH_SIZE; // 例如 46（当H=644）
            const int grid_w = W / PATCH_SIZE; // 例如 46（当W=644）
            const int grid_h_outer = grid_h / MERGE_SIZE; // 例如 23（当grid_h=46）
            const int grid_w_outer = grid_w / MERGE_SIZE; // 例如 23（当grid_w=46）
            const int T = TEMPORAL_PATCH_SIZE; // 2

            // 先做CLIP归一化并复制到两个时间帧（t=0与t=1相同）。
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
                    const uint8_t *p = &rgb.data[(y * W + x) * 3];
                    for (int c = 0; c < 3; ++c)
                    {
                        float v = (p[c] * (1.0f / 255.0f) - MEAN[c]) / STD[c];
                        norm[idx4(0, c, y, x)] = v;
                        norm[idx4(1, c, y, x)] = v;
                    }
                }
            }

            // 展平顺序：外层 ho, wo, mh, mw；内层 c, t, ph, pw —— 与原实现一致。
            rows = 1 * grid_h * grid_w; // grid_t=1（与原一致）
            cols = 3 * T * PATCH_SIZE * PATCH_SIZE; // =1176
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
