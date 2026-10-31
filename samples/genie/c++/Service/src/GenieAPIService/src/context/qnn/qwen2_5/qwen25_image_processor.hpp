//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once

#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace qwen2_5
{
    class Qwen25ImageProcessor
    {
    public:

        static constexpr int PATCH_SIZE = 14;
        static constexpr int TEMPORAL_PATCH_SIZE = 2;
        static constexpr int MERGE_SIZE = 2;
        static constexpr int SPATIAL_MERGE_SIZE = 2;
        static constexpr int PATCH_FACTOR = PATCH_SIZE * SPATIAL_MERGE_SIZE;

        static constexpr float MEAN[3] = {0.48145466f, 0.45782750f, 0.40821073f};
        static constexpr float STD[3] = {0.26862954f, 0.26130258f, 0.27577711f};

        struct ImageU8
        {
            int w = 0, h = 0, c = 0;
            std::vector<uint8_t> data;
        };

    public:
        Qwen25ImageProcessor() = default;

        void ProcessToRaw(const std::string &input_image_path,
                          const std::string &output_raw_path,
                          int request_h,
                          int request_w) const
        {

            ImageU8 img = LoadImage_STB(input_image_path);

            ImageU8 rgb = ToRGBWhiteBackground(img);

            auto [rh, rw] = smart_resize(request_h, request_w, PATCH_FACTOR);

            ImageU8 rgb_resized = ResizeRGB_STB(rgb, rw, rh);

            GeneratePixelValues(rgb_resized, output_raw_path);
        }

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

        static std::pair<int, int> SmartResizeOnly(int request_h, int request_w)
        {
            return smart_resize(request_h, request_w, PATCH_FACTOR);
        }

    private:

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

        static ImageU8 LoadImage_STB(const std::string &path)
        {
            int w = 0, h = 0, n = 0;
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
                    STBIR_RGB))
            {
                throw std::runtime_error("stb_image_resize: resize failed");
            }
            return out;
        }

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
        }

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

            const int grid_h = H / PATCH_SIZE;
            const int grid_w = W / PATCH_SIZE;
            const int grid_h_outer = grid_h / MERGE_SIZE;
            const int grid_w_outer = grid_w / MERGE_SIZE;
            const int T = TEMPORAL_PATCH_SIZE;

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

            rows = 1 * grid_h * grid_w;
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
