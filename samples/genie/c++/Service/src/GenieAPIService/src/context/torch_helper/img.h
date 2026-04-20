//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef IMG_H
#define IMG_H

struct Image
{
    int w, h, c;
    float *point;
    int size;
};

void save_float_image(const float *f_pixels, int width, int height, int channels,
                      const char *filename, bool prefer_hdr = false)
{
    std::string fn(filename);
    bool is_hdr = prefer_hdr || (fn.size() >= 4 && fn.substr(fn.size() - 4) == ".hdr");

    if (is_hdr)
    {
        stbi_write_hdr(filename, width, height, channels, f_pixels);
    }
    else
    {
        size_t n = static_cast<size_t>(width) * height * channels;
        std::vector<uint8_t> u8(n);
        for (size_t i = 0; i < n; ++i)
        {
            float v = std::clamp(f_pixels[i], 0.0f, 1.0f);
            u8[i] = static_cast<uint8_t>(v * 255.0f + 0.5f);
        }
        int stride = width * channels;
        stbi_write_png(filename, width, height, channels, u8.data(), stride);
    }
}

#endif //IMG_H
