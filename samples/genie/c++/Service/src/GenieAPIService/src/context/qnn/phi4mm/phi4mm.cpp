//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#define STB_IMAGE_IMPLEMENTATION
#define STB_IMAGE_WRITE_IMPLEMENTATION
#define STB_IMAGE_RESIZE2_IMPLEMENTATION

#include <stb_image.h>
#include <stb_image_resize2.h>
#include <stb_image_write.h>

#include "phi4mm.h"

#include "../../torch_helper/shape_6d.h"
#include "../../torch_helper/shape_5d.h"
#include "../../torch_helper/shape_4d.h"
#include "../../torch_helper/shape_3d.h"
#include "../../torch_helper/shape_2d.h"
#include "../../torch_helper/img.h"
#include <set>
#include <log.h>
#include <utils.h>

std::pair<int, int> find_closest_aspect_ratio(
        double aspect_ratio,
        const std::vector<std::pair<int, int>> &target_ratios,
        int width,
        int height,
        int image_size)
{
    double best_ratio_diff = std::numeric_limits<double>::infinity();
    std::pair<int, int> best_ratio = {1, 1};
    double area = static_cast<double>(width) * static_cast<double>(height);

    for (const auto &ratio: target_ratios)
    {
        double target_aspect_ratio = static_cast<double>(ratio.first) / static_cast<double>(ratio.second);
        double ratio_diff = std::abs(aspect_ratio - target_aspect_ratio);

        if (ratio_diff < best_ratio_diff)
        {
            best_ratio_diff = ratio_diff;
            best_ratio = ratio;
        }
        else if (ratio_diff == best_ratio_diff)
        {
            // tie-breaker: same as Python: if area > 0.5 * image_size * image_size * ratio[0] * ratio[1]
            double threshold = 0.5 * static_cast<double>(image_size) * static_cast<double>(image_size) * static_cast<double>(ratio.first) * static_cast<double>(ratio.second);
            if (area > threshold)
            {
                best_ratio = ratio;
            }
        }
    }

    return best_ratio;
}

std::vector<std::pair<int, int>> QInterface::PHI4Embedding::GenerateTargetRatios()
{
    int min_num = 1;
    int max_num = DynamicHD;

    // 1) collect unique pairs (i,j)
    std::set<std::pair<int, int>> target_set;
    for (int n = min_num; n <= max_num; ++n)
    {
        for (int i = 1; i <= n; ++i)
        {
            for (int j = 1; j <= n; ++j)
            {
                int prod = i * j;
                if (prod >= min_num && prod <= max_num)
                {
                    target_set.insert({i, j});
                }
            }
        }
    }

    // 2) move to vector and sort by product (then by i, then by j)
    std::vector<std::pair<int, int>> target_ratios(target_set.begin(), target_set.end());
    std::sort(target_ratios.begin(), target_ratios.end(),
              [](const std::pair<int, int> &a, const std::pair<int, int> &b)
              {
                  int pa = a.first * a.second;
                  int pb = b.first * b.second;
                  if (pa != pb)
                      return pa < pb;
                  if (a.first != b.first)
                      return a.first < b.first;
                  return a.second < b.second;
              });

    return target_ratios;
}

Shape_2D<float> make_attention_mask(
        const std::pair<int, int> &target_aspect_ratio,
        int padding_width,
        int padding_height,
        int mask_size)
{
    int cols = static_cast<int>(mask_size * target_aspect_ratio.first);
    int rows = static_cast<int>(mask_size * target_aspect_ratio.second);

    Shape_2D<float> out;
    out.d0 = rows;
    out.d1 = cols;
    out.buf.assign(static_cast<size_t>(rows) * static_cast<size_t>(cols), 1.0f);

    int zero_cols = 0;
    int zero_rows = 0;
    if (padding_width >= 14)
    {
        zero_cols = static_cast<int>(std::floor(padding_width / 14.0));
        if (zero_cols > cols)
            zero_cols = cols;
    }
    if (padding_height >= 14)
    {
        zero_rows = static_cast<int>(std::floor(padding_height / 14.0));
        if (zero_rows > rows)
            zero_rows = rows;
    }

    // zero last zero_cols columns
    if (zero_cols > 0)
    {
        int start_col = cols - zero_cols;
        for (int r = 0; r < rows; ++r)
        {
            size_t base = static_cast<size_t>(r) * static_cast<size_t>(cols);
            for (int c = start_col; c < cols; ++c)
            {
                out.buf[base + static_cast<size_t>(c)] = 0.0f;
            }
        }
    }

    // zero last zero_rows rows
    if (zero_rows > 0)
    {
        int start_row = rows - zero_rows;
        for (int r = start_row; r < rows; ++r)
        {
            size_t base = static_cast<size_t>(r) * static_cast<size_t>(cols);
            for (int c = 0; c < cols; ++c)
            {
                out.buf[base + static_cast<size_t>(c)] = 0.0f;
            }
        }
    }

    // ensure at least one element remains non-zero
    double sum = 0.0;
    for (float v: out.buf)
        sum += static_cast<double>(v);
    if (sum <= 0.0)
    {
        throw std::runtime_error("attention_mask sum is zero or negative");
    }

//    out.buf = File::ReadFile<float>("attention_mask.raw");
    return out;
}

Image pad_image_hwc(
        const Image &in,
        int pad_right,
        int pad_bottom)
{
    const std::vector<float> fill = {255.0f, 255.0f, 255.0f};
    int src_w = in.w, src_h = in.h, channels = in.c;
    float *src = in.point;
    int pad_left = 0, pad_top = 0;

    int dst_w = src_w + pad_left + pad_right;
    int dst_h = src_h + pad_top + pad_bottom;


    int size = dst_w * dst_h * channels;
    float *dst = new float[size];
//    std::vector<float> dst(static_cast<size_t>(dst_w) * dst_h * channels);

    // fill whole dst with fill color
    for (int y = 0; y < dst_h; ++y)
    {
        for (int x = 0; x < dst_w; ++x)
        {
            size_t base = (static_cast<size_t>(y) * dst_w + x) * channels;
            for (int c = 0; c < channels; ++c)
            {
                dst[base + c] = fill[c];
            }
        }
    }

    // copy src into dst at offset (pad_top, pad_left)
    for (int y = 0; y < src_h; ++y)
    {
        for (int x = 0; x < src_w; ++x)
        {
            size_t src_idx = (static_cast<size_t>(y) * src_w + x) * channels;
            size_t dst_idx = (static_cast<size_t>(y + pad_top) * dst_w + (x + pad_left)) * channels;
            std::memcpy(&dst[dst_idx], &src[src_idx], static_cast<size_t>(channels) * sizeof(float));
        }
    }

    return {dst_w, dst_h, 3, dst, size};
}

Image resize_srgb_float_pipeline(float *pixels, int org_w, int org_h, int comp, int tar_w, int tar_h)
{
    Image out{tar_w, tar_h, comp};
    out.size = tar_w * tar_h * comp;
    out.point = new float[out.size];
    float *out_f = out.point;

    if (!stbir_quick_resize_helper(pixels, org_w, org_h, 0,
                                   out.point, tar_w, tar_h, 0,
                                   STBIR_RGB,
                                   STBIR_TYPE_FLOAT,
                                   STBIR_EDGE_REFLECT,
                                   STBIR_FILTER_MITCHELL))
    {
        throw std::runtime_error("resize img failed");
    }
    return out;
}

// <img_buf, attention_mask>
std::pair<Image, Shape_2D<float>> QInterface::PHI4Embedding::DynamicPreprocess()
{
    const stbir_pixel_layout excepted_layout{STBIR_RGB};
    int ori_w = 0, ori_h = 0, comp = 0;
    auto *f_pixels = stbi_loadf_from_memory(img_buf_.data(), img_buf_.size(), &ori_w, &ori_h, &comp, excepted_layout);

    My_Log{} << "comp: " << comp << "\n";
    auto aspect_ratio = (float) ori_w / ori_h;
    auto target_ratios = GenerateTargetRatios();
    int max_num = DynamicHD;

    float w_crop_num = ceil((float) ori_w / (float) kWidth);
    float h_crop_num = ceil((float) ori_h / (float) kHeight);

    std::pair<int, int> best_ratio;
    if (w_crop_num * h_crop_num > max_num)
    {
        best_ratio = find_closest_aspect_ratio(aspect_ratio, target_ratios, ori_w, ori_h, kHeight);
    }
    else
    {
        best_ratio = {int(w_crop_num), int(h_crop_num)};
    }

    int tar_w = kWidth * best_ratio.first;
    int tar_h = kHeight * best_ratio.second;

    float ratio_width = float(tar_w) / (float) ori_w;
    float ratio_height = float(tar_h) / (float) ori_h;

    std::pair<int, int> new_size; // <w, h>
    int padding_width, padding_height;
    if (ratio_width < ratio_height)
    {
        new_size = {tar_w, int(ori_h * ratio_width)};
        padding_width = 0;
        padding_height = tar_h - int(ori_h * ratio_width);
    }
    else
    {
        new_size = {int(ori_w * ratio_height), tar_h};
        padding_width = tar_w - int(ori_w * ratio_height);
        padding_height = 0;
    }

    auto attention_mask = make_attention_mask(best_ratio, padding_width, padding_height, kMaskSize);

    auto resized_img = resize_srgb_float_pipeline(f_pixels, ori_w, ori_h, comp, new_size.first, new_size.second);
    stbi_image_free(f_pixels);

    auto padded_img = pad_image_hwc(resized_img, padding_width, padding_height);
    stbi_image_free(resized_img.point);
    return {padded_img,
            make_attention_mask(best_ratio, padding_width, padding_height, kMaskSize)};
}

Shape_3D<float> ImageToTensorNormalized(const Image &img)
{
    const stbir_pixel_layout excepted_layout{STBIR_RGB};
    int ori_w = img.w, ori_h = img.h, comp = img.c;

    float *f_pixels = img.point;
    size_t np = static_cast<size_t>(ori_w) * ori_h;
    std::vector<float> chw(3 * np);

    for (size_t i = 0; i < np; ++i)
    {
        float r = f_pixels[i * 3 + 0];
        float g = f_pixels[i * 3 + 1];
        float b = f_pixels[i * 3 + 2];

        // Option A: return values in [0,1] and let Python Normalize
        chw[0 * np + i] = r;
        chw[1 * np + i] = g;
        chw[2 * np + i] = b;
    }
    return Shape_3D<float>{excepted_layout, ori_h, ori_w, chw};
}

Shape_3D<float> patchify_attention_mask(
        const Shape_2D<float> &mask,
        int mask_res)
{
    int h = mask.d0;
    int w = mask.d1;

    if (h % mask_res != 0 || w % mask_res != 0)
        throw std::invalid_argument("h and w must be divisible by mask_res");

    int blocks_h = (float) h / mask_res;
    int blocks_w = (float) w / mask_res;
    int N = blocks_h * blocks_w;
    Shape_3D<float> out;
    out.d0 = N;
    out.d1 = mask_res;
    out.d2 = mask_res;
    out.buf.assign(static_cast<size_t>(N) * mask_res * mask_res, 0.0f);

    // Input indexing: mask.buf[y * W_mask + x]
    // Output indexing: out.buf[(n * mask_res + y) * mask_res + x]
    int n = 0;
    for (int by = 0; by < blocks_h; ++by)
    {
        for (int bx = 0; bx < blocks_w; ++bx)
        {
            for (int y = 0; y < mask_res; ++y)
            {
                int src_y = by * mask_res + y;
                size_t src_base = static_cast<size_t>(src_y) * w;
                size_t dst_base = static_cast<size_t>(n) * mask_res * mask_res + static_cast<size_t>(y) * mask_res;
                for (int x = 0; x < mask_res; ++x)
                {
                    int src_x = bx * mask_res + x;
                    out.buf[dst_base + static_cast<size_t>(x)] = mask.buf[src_base + static_cast<size_t>(src_x)];
                }
            }
            ++n;
        }
    }

    return out;
}

Shape_4D<float> unsqueeze_batch(const Shape_3D<float> &in)
{
    if (in.d0 <= 0 || in.d1 <= 0 || in.d2 <= 0)
        throw std::invalid_argument("invalid input dims");
    size_t expected = static_cast<size_t>(in.d0) * in.d1 * in.d2;
    if (in.buf.size() != expected)
        throw std::invalid_argument("input buffer size mismatch");

    Shape_4D<float> out;
    out.d0 = 1;       // batch size 1
    out.d1 = in.d0;   // C
    out.d2 = in.d1;   // H
    out.d3 = in.d2;   // W
    out.buf = in.buf; // copy buffer; layout becomes N x C x H x W with N=1

    return out;
}

Shape_4D<float> QInterface::PHI4Embedding::GenerateGlobalImg(const Image &img)
{
    Image standard_img = resize_srgb_float_pipeline(img.point, img.w, img.h, img.c, kWidth, kHeight);
    save_float_image(standard_img.point, standard_img.w, standard_img.h, 3, "resized_srgb3.png");
    return unsqueeze_batch(ImageToTensorNormalized(standard_img));
}

Shape_3D<float> ones_mask_3d(int mask_res)
{
    if (mask_res <= 0)
        throw std::invalid_argument("mask_res must be positive");

    Shape_3D<float> out;
    out.d0 = 1;
    out.d1 = mask_res;
    out.d2 = mask_res;
    out.buf.assign(static_cast<size_t>(out.d0) * out.d1 * out.d2, 1.0f);
    return out;
}

int get_per_size(const Shape_4D<float> &s)
{
    return (s.d1) * s.d2 * s.d3;
}

int get_per_size(const Shape_3D<float> &s)
{
    return (s.d1) * s.d2;
}

template<typename T>
int get_per_size(const Shape_2D<T> &s)
{
    return (s.d1);
}

template<typename T>
T truncate_inplace(T s, int start, int end)
{
    int N = s.d0;
    if (start < 0)
        start += N;
    if (end < 0)
        end += N;

    if (start < 0)
        start = 0;
    if (end < 0)
        end = 0;
    if (start > N)
        start = N;
    if (end > N)
        end = N;

    if (start >= end)
    {
        // empty result
        s.buf.clear();
        s.d0 = 0;
        return s;
    }

    int keep = end - start;
    std::size_t per_batch = get_per_size(s);
    std::size_t new_size = static_cast<std::size_t>(keep) * per_batch;
    std::size_t offset = static_cast<std::size_t>(start) * per_batch;

    if (offset != 0)
    {
        // move the kept prefix to the beginning
        // use std::move to avoid element-wise copy if T is movable
        std::move(s.buf.begin() + offset, s.buf.begin() + offset + new_size, s.buf.begin());
    }
    s.buf.resize(new_size);
    s.d0 = keep;
    return s;
}

Shape_3D<uint8_t> to_bool_mask(const Shape_3D<float> &src)
{
    Shape_3D<uint8_t> out;
    out.d0 = src.d0;
    out.d1 = src.d1;
    out.d2 = src.d2;

    std::size_t total = static_cast<std::size_t>(src.d0) * src.d1 * src.d2;
    out.buf.resize(total);

    for (std::size_t i = 0; i < total; ++i)
    {
        out.buf[i] = (src.buf[i] != 0.0f) ? 1u : 0u;
    }
    return out;
}

// Broadcast-add for two common patterns:
// 1) in_h shape [N,1] and in_w shape [M] -> out shape [N,M], out[i,j] = in_h[i,0] + in_w[j]
// 2) in_h shape [1,M] and in_w shape [N] -> out shape [N,M], out[i,j] = in_w[i] + in_h[0,j]
Shape_2D<int64_t> BroadcastAddHplusW(const Shape_2D<int64_t> &in_h, const Shape_1D<int64_t> &in_w)
{
    // Validate buffers
    if (in_h.buf.size() != static_cast<std::size_t>(in_h.d0) * static_cast<std::size_t>(in_h.d1))
        throw std::runtime_error("in_h buffer size mismatch");
    if (in_w.buf.size() != static_cast<std::size_t>(in_w.d0))
        throw std::runtime_error("in_w buffer size mismatch");

    Shape_2D<int64_t> out;

    // Case A: in_h is [N,1], broadcast in_w across columns -> out [N, M]
    if (in_h.d1 == 1)
    {
        int N = in_h.d0;
        int M = in_w.d0;
        out.d0 = N;
        out.d1 = M;
        out.buf.resize(static_cast<std::size_t>(N) * static_cast<std::size_t>(M));
        for (int i = 0; i < N; ++i)
        {
            int64_t hval = in_h.buf[static_cast<std::size_t>(i) * in_h.d1 + 0];
            std::size_t row_off = static_cast<std::size_t>(i) * static_cast<std::size_t>(M);
            for (int j = 0; j < M; ++j)
            {
                out.buf[row_off + static_cast<std::size_t>(j)] = hval + in_w.buf[static_cast<std::size_t>(j)];
            }
        }
        return out;
    }

    // Case B: in_h is [1,M], broadcast in_w across rows -> out [N, M]
    if (in_h.d0 == 1)
    {
        int N = in_w.d0;
        int M = in_h.d1;
        out.d0 = N;
        out.d1 = M;
        out.buf.resize(static_cast<std::size_t>(N) * static_cast<std::size_t>(M));
        for (int i = 0; i < N; ++i)
        {
            int64_t wval = in_w.buf[static_cast<std::size_t>(i)];
            std::size_t row_off = static_cast<std::size_t>(i) * static_cast<std::size_t>(M);
            for (int j = 0; j < M; ++j)
            {
                int64_t hval = in_h.buf[static_cast<std::size_t>(0) * in_h.d1 + static_cast<std::size_t>(j)];
                out.buf[row_off + static_cast<std::size_t>(j)] = wval + hval;
            }
        }
        return out;
    }

    // Unsupported broadcasting pattern
    throw std::invalid_argument("unsupported shapes for broadcasting: expected in_h.d1==1 or in_h.d0==1");
}

// Assign pos_ids into position_ids[batch_idx][mask.view(-1)]
// mask: Shape_2D<uint8_t> treated as boolean (nonzero = true)
// position_ids: Shape_2D<int64_t> with shape [B, L]
// pos_ids: Shape_1D<int64_t> with length == number of true elements in mask
void AssignWithMask(
        Shape_2D<int64_t> &position_ids,
        int batch_idx,
        const Shape_2D<uint8_t> &p_attn_mask,
        const Shape_1D<int64_t> &pos_ids)
{
    // Validate position_ids shape and batch index
    if (position_ids.d0 <= 0 || position_ids.d1 <= 0)
        throw std::invalid_argument("position_ids dims must be > 0");
    if (batch_idx < 0 || batch_idx >= position_ids.d0)
        throw std::out_of_range("batch_idx out of range");
    if (static_cast<size_t>(position_ids.buf.size()) != static_cast<size_t>(position_ids.d0) * position_ids.d1)
        throw std::runtime_error("position_ids buffer size mismatch");

    // Validate mask shape and buffer
    if (p_attn_mask.d0 <= 0 || p_attn_mask.d1 <= 0)
        throw std::invalid_argument("p_attn_mask dims must be > 0");
    if (static_cast<size_t>(p_attn_mask.buf.size()) != static_cast<size_t>(p_attn_mask.d0) * p_attn_mask.d1)
        throw std::runtime_error("p_attn_mask buffer size mismatch");

    // Flatten mask and collect indices where mask != 0
    std::vector<int> indices;
    indices.reserve(static_cast<size_t>(p_attn_mask.d0) * p_attn_mask.d1);
    for (int i = 0; i < p_attn_mask.d0; ++i)
    {
        for (int j = 0; j < p_attn_mask.d1; ++j)
        {
            size_t flat_idx = static_cast<size_t>(i) * p_attn_mask.d1 + static_cast<size_t>(j);
            if (p_attn_mask.buf[flat_idx] != 0)
            {
                // flat index in range [0, L-1]
                indices.push_back(static_cast<int>(flat_idx));
            }
        }
    }

    // Validate pos_ids length matches number of true elements
    if (static_cast<int>(indices.size()) != pos_ids.d0)
    {
        throw std::runtime_error("pos_ids length does not match number of true elements in p_attn_mask");
    }
    if (static_cast<size_t>(pos_ids.buf.size()) != static_cast<size_t>(pos_ids.d0))
        throw std::runtime_error("pos_ids buffer size mismatch");

    // Perform assignment: position_ids[batch_idx, indices[k]] = pos_ids[k]
    size_t row_offset = static_cast<size_t>(batch_idx) * static_cast<size_t>(position_ids.d1);
    for (size_t k = 0; k < indices.size(); ++k)
    {
        int col = indices[k];
        if (col < 0 || col >= position_ids.d1)
        {
            throw std::out_of_range("mask index out of range for position_ids columns");
        }
        position_ids.buf[row_offset + static_cast<size_t>(col)] = pos_ids.buf[k];
    }
}

Shape_2D<int64_t> QInterface::PHI4Embedding::compute_position_ids(
        const Shape_4D<float> &image_transformed,
        const Shape_3D<uint8_t> &patch_attention_mask)
{
    Shape_1D<float> boundaries = Arange<float>(1.0f / float(kMaskSize), 1.0f, 1.0f / float(kMaskSize));
    Shape_2D<int64_t> position_ids = Full<int64_t>(image_transformed.d0,
                                                   image_transformed.d2 / kPatchSize * image_transformed.d3 / kPatchSize,
                                                   0);

    std::vector<Shape_2D<uint8_t>> patch_attention_mask_lists = SplitShape3d(patch_attention_mask);
    std::vector<Shape_1D<int64_t>> pos_ids_lists;
    for (auto i = 0; i < patch_attention_mask_lists.size(); ++i)
    {
        Shape_2D<uint8_t> &p_attn_mask = patch_attention_mask_lists[i];

        // p_attn_mask[:, 0]
        int nb_patches_h = Sum(slice_keep_one_axis_2d(p_attn_mask, 0, 0));

        // p_attn_mask[0]
        int nb_patches_w = Sum(slice_keep_one_axis_2d(p_attn_mask, 1, 0));
        if (!nb_patches_w || !nb_patches_h)
            continue;

        Shape_1D<float> fractional_coords_h = Arange<float>(0.0f, 1.0f - 1e-6, 1.0f / float(nb_patches_h));
        Shape_1D<int64_t> bucket_coords_h = Bucketize(fractional_coords_h, boundaries);

        Shape_1D<float> fractional_coords_w = Arange<float>(0.0f, 1.0f - 1e-6, 1.0f / float(nb_patches_w));
        Shape_1D<int64_t> bucket_coords_w = Bucketize(fractional_coords_w, boundaries);

        Shape_2D<int64_t> bucket_coords_h_2d = Unsqueeze(bucket_coords_h);
        bucket_coords_h_2d = MultiplyScalar<int64_t>(bucket_coords_h_2d, kMaskSize);
        bucket_coords_h_2d = BroadcastAddHplusW(bucket_coords_h_2d, bucket_coords_w);

        AssignWithMask(position_ids, i, p_attn_mask, Flatten(bucket_coords_h_2d));
    }
    return position_ids;
}

Shape_2D<uint8_t> flatten_copy(const Shape_3D<uint8_t> &src)
{
    Shape_2D<uint8_t> out;
    out.d0 = src.d0;
    out.d1 = src.d1 * src.d2;
    out.buf = src.buf; // copy entire buffer; layout is already batch-major row-major
    return out;
}

Shape_4D<float> make_attn_mask_4d(const Shape_2D<uint8_t> &mask2d, float mask_value)
{
    size_t total = static_cast<std::size_t>(mask2d.d0) * mask2d.d1;
    bool need_expand = false;
    for (std::size_t i = 0; i < total; ++i)
    {
        if (mask2d.buf[i] == 0u)
        {
            need_expand = true;
            break;
        }
    }
    if (!need_expand)
    {
//        throw std::runtime_error("mask2d need to expand");
        return {};
    }

    int batch = mask2d.d0;
    int seq_len = mask2d.d1;

    Shape_4D<float> out;
    out.d0 = batch;
    out.d1 = 1;
    out.d2 = seq_len;
    out.d3 = seq_len;
    out.buf.assign(static_cast<std::size_t>(batch) * out.d1 * out.d2 * out.d3, 0.0f);

    // For each batch b, for each target row i (0..seq_len-1), and for each column j (0..seq_len-1):
    // expand_mask[b,0,i,j] = mask2d[b, j] (reshape+expand semantics)
    // inverted_mask = 1.0 - expand_mask -> equals 1 when mask2d[b,j] == 0
    // final[b,0,i,j] = (inverted_mask == 1) ? mask_value : 0.0f
    for (int b = 0; b < batch; ++b)
    {
        std::size_t base_mask = static_cast<std::size_t>(b) * seq_len;
        std::size_t base_out = static_cast<std::size_t>(b) * out.d1 * out.d2 * out.d3; // equals b * seq_len * seq_len
        for (int i = 0; i < seq_len; ++i)
        {
            // row i in the expanded matrix; each column j depends only on mask2d[b, j]
            std::size_t row_offset = base_out + static_cast<std::size_t>(i) * out.d3;
            for (int j = 0; j < seq_len; ++j)
            {
                uint8_t m = mask2d.buf[base_mask + static_cast<std::size_t>(j)];
                // inverted_mask = 1 - m (treat nonzero as 1)
                bool inverted = (m == 0u);
                out.buf[row_offset + static_cast<std::size_t>(j)] = inverted ? mask_value : 0.0f;
            }
        }
    }

    return out;
}

int compute_num_img_tokens(
        Shape_2D<float> &mask)
{
    const int base_tokens = 256;
    const int extra_token = 1;
    const int tail_tokens = 16;
    int result;
    // sum all elements
    double total_sum = 0.0;
    std::size_t total_elems = static_cast<std::size_t>(mask.d0) * mask.d1;
    for (std::size_t i = 0; i < total_elems; ++i)
        total_sum += static_cast<double>(mask.buf[i]);

    // sum first column mask[:,0]
    double first_col_sum = 0.0;
    for (int r = 0; r < mask.d0; ++r)
    {
        std::size_t idx = static_cast<std::size_t>(r) * mask.d1 + 0;
        first_col_sum += static_cast<double>(mask.buf[idx]);
    }

    result = static_cast<int64_t>(base_tokens) + static_cast<int64_t>(extra_token) + static_cast<int64_t>(total_sum) + static_cast<int64_t>(first_col_sum) + static_cast<int64_t>(tail_tokens);
    return result;
}

template<typename T>
Shape_3D<float> extract_global_img_feature(const Shape_4D<T> &src)
{
    int batch_idx = 0;
    if (src.d0 <= 0 || src.d1 <= 0 || src.d2 <= 0 || src.d3 <= 0)
        throw std::invalid_argument("invalid source dimensions");

    // normalize batch index (allow negative indexing)
    if (batch_idx < 0)
        batch_idx += src.d0;
    if (batch_idx < 0 || batch_idx >= src.d0)
        throw std::out_of_range("batch_idx out of range");

    // We want the first token along d1: token_start = 0, token_count = 1
    const int token_start = 0;
    const int token_count = 1;

    // size of one token (d2 * d3)
    std::size_t per_token = static_cast<std::size_t>(src.d2) * static_cast<std::size_t>(src.d3);

    // offset in elements: ((batch_idx * d1) + token_start) * per_token
    std::size_t offset = (static_cast<std::size_t>(batch_idx) * static_cast<std::size_t>(src.d1) + static_cast<std::size_t>(token_start)) * per_token;

    std::size_t new_size = static_cast<std::size_t>(token_count) * per_token;

    if (offset + new_size > src.buf.size())
        throw std::out_of_range("source buffer too small for requested slice");

    Shape_3D<T> out;
    out.d0 = 1;            // batch dimension preserved as 1 (matches Python result [1, 256, 1152])
    out.d1 = src.d2;       // channels / height (256)
    out.d2 = src.d3;       // width (1152)
    out.buf.assign(src.buf.begin() + offset, src.buf.begin() + offset + new_size);
    return out;
}

Shape_3D<float> slice_image_attention_mask_range(
        const Shape_3D<float> &in,
        int start0,   // inclusive
        int end0,     // exclusive
        int start_h,
        int step_h,
        int start_w,
        int step_w
)
{
    // Basic validation
    if (in.d0 <= 0 || in.d1 <= 0 || in.d2 <= 0)
        throw std::invalid_argument("input dims must be > 0");
    if (start0 < 0 || end0 < 0 || start0 > end0)
        throw std::invalid_argument("invalid start0/end0");
    if (end0 > in.d0)
        throw std::out_of_range("end0 exceeds input d0");
    if (start_h < 0 || start_h >= in.d1)
        throw std::out_of_range("start_h out of range");
    if (start_w < 0 || start_w >= in.d2)
        throw std::out_of_range("start_w out of range");
    if (step_h <= 0 || step_w <= 0)
        throw std::invalid_argument("steps must be > 0");

    int out_d0 = end0 - start0;

    // compute output spatial sizes by iterating until we exceed bounds (matches Python slicing behavior)
    int out_h = 0;
    for (int h = start_h; h < in.d1; h += step_h)
        ++out_h;
    int out_w = 0;
    for (int w = start_w; w < in.d2; w += step_w)
        ++out_w;

    Shape_3D<float> out;
    out.d0 = out_d0;
    out.d1 = out_h;
    out.d2 = out_w;
    out.buf.resize(static_cast<size_t>(out_d0) * out_h * out_w);

    auto in_index = [&](int b, int h, int w) -> size_t
    {
        return static_cast<size_t>(((b * in.d1 + h) * in.d2) + w);
    };
    auto out_index = [&](int b, int h, int w) -> size_t
    {
        return static_cast<size_t>(((b * out.d1 + h) * out.d2) + w);
    };

    for (int ob = 0; ob < out_d0; ++ob)
    {
        int sb = start0 + ob; // source b index
        for (int ih = 0, sh = start_h; sh < in.d1; ++ih, sh += step_h)
        {
            for (int iw = 0, sw = start_w; sw < in.d2; ++iw, sw += step_w)
            {
                out.buf[out_index(ob, ih, iw)] = in.buf[in_index(sb, sh, sw)];
            }
        }
    }

    return out;
}

std::pair<std::vector<int32_t>, std::vector<int32_t>>
nonzero_eq_2d(const Shape_2D_View<int32_t> &in, int value)
{
    if (in.d0 <= 0 || in.d1 <= 0)
        throw std::invalid_argument("input dims must be > 0");
    if (in.size != static_cast<size_t>(in.d0) * in.d1)
        throw std::invalid_argument("buffer size mismatch");

    std::vector<int32_t> rows;
    std::vector<int32_t> cols;
    rows.reserve(128);
    cols.reserve(128);

    for (int i = 0; i < in.d0; ++i)
    {
        size_t base = static_cast<size_t>(i) * in.d1;
        for (int j = 0; j < in.d1; ++j)
        {
            if (in.buf[base + j] == value)
            {
                rows.push_back(i);
                cols.push_back(j);
            }
        }
    }
    return {std::move(rows), std::move(cols)};
}

IVisionEmbedding &QInterface::PHI4Embedding::BuildImgPixel()
{
    stbi_ldr_to_hdr_gamma(1.0f);
    int base_resolution = kHeight;
    auto [img, attention_mask] = DynamicPreprocess();

    Shape_3D<float> hd_images = ImageToTensorNormalized(img);

    std::pair<int, int> image_size = {hd_images.d1, hd_images.d2};
    crop_h_ = image_size.first / kHeight;
    crop_w_ = image_size.second / kWidth;

    Shape_4D<float> global_image = GenerateGlobalImg(img);
    stbi_image_free(img.point);
    auto global_attention_mask = ones_mask_3d(kMaskSize);

    Shape_6D<float> hd_images_reshape_6d = reshape_3d_to_6d(hd_images, 1, 3, crop_h_, kHeight, crop_w_, kWidth);
    hd_images_reshape_6d = permute_shape6d(hd_images_reshape_6d, {0, 2, 4, 1, 3, 5});
    Shape_4D<float> hd_images_reshape_4d = reshape_6d_to_4d(hd_images_reshape_6d, -1, 3, kHeight, kWidth);

    Shape_3D<float> attention_masks_reshape = patchify_attention_mask(attention_mask, kMaskSize);

    Shape_3D<float> attention_masks_reshape_tmp = slice_shape3d_range(attention_masks_reshape,
                                                                      0, -1, 1,
                                                                      0, -1, 2,
                                                                      0, -1, 2);

    Shape_5D<float> downsample_attention_masks_5d = reshape_3d_to_5d(attention_masks_reshape_tmp, 1, crop_h_, crop_w_,
                                                                     kMaskSize / 2 + kMaskSize % 2,
                                                                     kMaskSize / 2 + kMaskSize % 2);

    downsample_attention_masks_5d = permute_shape5d(downsample_attention_masks_5d, {0, 1, 3, 2, 4});
    Shape_2D<float> downsample_attention_masks = reshape_5d_to_2d(downsample_attention_masks_5d,
                                                                  downsample_attention_masks_5d.d1 * downsample_attention_masks_5d.d2,
                                                                  downsample_attention_masks_5d.d3 * downsample_attention_masks_5d.d4);

    token_index_ = compute_num_img_tokens(downsample_attention_masks);


    hd_images_reshape_4d = concat_shape4d(global_image, hd_images_reshape_4d, 0);

    auto hd_masks_reshape = concat_3d_list<float>({global_attention_mask, attention_masks_reshape}, 0);
    auto &image_transformed = hd_images_reshape_4d;
    auto &image_attention_mask = hd_masks_reshape;

    // ------------------------

    int max_crops = image_transformed.d0;
    valid_crops_ = std::min(max_crops, 1 + crop_h_ * crop_w_);

    image_transformed = truncate_inplace(image_transformed, 0, valid_crops_);
    image_attention_mask = truncate_inplace(image_attention_mask, 0, valid_crops_);
    auto img_attn_mask_bool = to_bool_mask(image_attention_mask);

    Shape_2D<uint8_t> flat_patch_mask = flatten_copy(img_attn_mask_bool);
    auto attn_mask_4d = make_attn_mask_4d(flat_patch_mask, -10000.0f);
    auto position_ids = compute_position_ids(image_transformed, img_attn_mask_bool);

    for (auto i = 0; i < valid_crops_; ++i)
    {
        std::vector<float> float_dst;
        crop_pixels_.emplace_back(truncate_inplace(image_transformed, i, i + 1));
        if (attn_mask_4d.buf.empty())
        {
            auto crop_patch_mask = truncate_inplace(image_attention_mask, i, i + 1);
            auto crop_patch_mask2 = reshape_3d_to_2d(crop_patch_mask, crop_patch_mask.d0, -1);
            auto crop_patch_mask4 = reshape_2d_to_4d(crop_patch_mask2, crop_patch_mask2.d0, 1, 1, crop_patch_mask2.d1);
            crop_patch_mask4 = Expand<float>(crop_patch_mask4, -1, 1, crop_patch_mask2.d1, -1);
            crop_attention_mask_.emplace_back(crop_patch_mask4);
        }
        else
        {
            crop_attention_mask_.emplace_back(truncate_inplace(attn_mask_4d, i, i + 1));
        }

        Shape_2D<int64_t> src = truncate_inplace(position_ids, i, i + 1);
        float_dst.reserve(src.buf.size());
        for (int64_t j: src.buf)
        {
            float_dst.push_back(static_cast<float>(j));
        }
        crop_position_ids_.emplace_back(Shape_2D<float>{src.d0, src.d1, std::move(float_dst)});
    }

    int B = crop_h_ * crop_w_;
    image_attention_mask = slice_image_attention_mask_range(image_attention_mask, 1, B + 1, 0, 2, 0, 2);
    Shape_4D<float> image_attention_mask4 = reshape_3d_to_4d(image_attention_mask, crop_h_, crop_w_, H, H);
    image_attention_mask4 = permute_shape4d(image_attention_mask4, {0, 2, 1, 3});
    Shape_3D<float> image_attention_mask3 = reshape_4d_to_3d(image_attention_mask4, 1, crop_h_ * H, crop_w_ * H);

    // [0, : ,0]
    useful_height_ = Sum(slice_keep_one_axis(image_attention_mask3, 1, 0, 0));
    // [0, 0, :]
    useful_width_ = Sum(slice_keep_one_axis(image_attention_mask3, 2, 0, 0));
    return *this;
}

Shape_3D<float> QInterface::PHI4Embedding::Compose()
{
    const int R = 1;

    int B = crop_h_ * crop_w_;
    std::vector<Shape_3D<float>> per_crop_features;

    for (const auto &buffer: img_inferred_buffers_)
    {
        if (buffer.size() != 1 * 256 * C * sizeof(float))
        {
            throw std::runtime_error("inferred_buffers is not in the corrent shape: " + std::to_string(buffer.size()));
        }
        auto buf_view = FloatBufferView{buffer};
        std::vector<float> buf;
        buf.assign(buf_view.pointer_, buf_view.pointer_ + buf_view.size_);
        per_crop_features.emplace_back(Shape_3D<float>{1, 256, C, std::move(buf)});
    }

    Shape_3D<float> patch_feature = concat_3d_list(per_crop_features, 0);
    Shape_4D<float> patch_feature_view = reshape_3d_to_4d(patch_feature, 1, -1, H * H, C);

    Shape_3D<float> global_img_feature = extract_global_img_feature(patch_feature_view);

    Shape_4D<float> glb_img = reshape_3d_to_4d(global_img_feature);
    Shape_6D<float> glb_img6 = reshape_4d_to_6d(glb_img, 1, H / R, R, H / R, R, C);
    glb_img6 = permute_shape6d(glb_img6, {0, 1, 3, 2, 4, 5});

    Shape_4D<float> glb_img4 = reshape_6d_to_4d(glb_img6, 1, H / R, H / R, R * R * C);
    glb_img4 = concat_shape4d(glb_img4, glb_gn_repeat_, 2);

    Shape_3D<float> glb_img3 = reshape_4d_to_3d(glb_img4, 1, -1, C);

    // patch_feature[0, 1:]
    Shape_3D<float> sub_img3 = Slice4DTo3D(
            patch_feature_view,
            /*idx0*/ 0,
            /*axis1*/ 1, -1, 1,   // 1:
            /*axis2*/ 0, -1, 1,   // :
            /*axis3*/ 0, -1, 1    // :
    );

    // sub_img[:self.B_]
    sub_img3 = slice_shape3d_range(
            sub_img3,
            /*axis0*/ 0, B, 1,
            /*axis1*/ 0, -1, 1,
            /*axis2*/ 0, -1, 1
    );

    Shape_4D<float> sub_img = reshape_3d_to_4d(sub_img3, B, H, H, C);

    Shape_6D<float> sub_img6 = reshape_4d_to_6d(sub_img, B, H / R, R, H / R, R, C);
    sub_img6 = permute_shape6d(sub_img6, {0, 1, 3, 2, 4, 5});
    sub_img3 = reshape_6d_to_3d(sub_img6, B, -1, C);

    sub_img6 = reshape_3d_to_6d(sub_img3, 1, crop_h_, crop_w_, H, H, C);
    sub_img6 = permute_shape6d(sub_img6, {0, 1, 3, 2, 4, 5});

    Shape_4D<float> sub_img4 = reshape_6d_to_4d(sub_img6, 1, crop_h_ * H, crop_w_ * H, C);
    sub_img4 = slice_shape4d_range(
            sub_img4,
            0, -1, 1,
            0, useful_height_, 1,
            0, useful_width_, 1,
            0, -1, 1
    );

    Shape_4D<float> temp_sub_repeat = repeat_shape4d(sub_gn_, 1, useful_height_, 1, 1);
    sub_img4 = concat_shape4d(sub_img4, temp_sub_repeat, 2);
    sub_img3 = reshape_4d_to_3d(sub_img4, 1, -1, C);

    return concat_3d_list<float>({sub_img3, glb_gn_, glb_img3}, 1);
}

IVisionEmbedding &QInterface::PHI4Embedding::MergeEmbedding()
{
    const int HIDDEN_DIM = C;
    const int SPECIAL_TOKEN_ID = 200010;
    Shape_3D<float> img_inferred_buffer;
    std::vector<unsigned char> buf;

    if (!img_inferred_buffers_.empty())
    {
        img_inferred_buffer = Compose();

        auto [expected_tokens, _] =
                nonzero_eq_2d(Shape_2D_View<int32_t>{1, static_cast<int>(prompt_token_size_),
                                                     prompt_token_, static_cast<int>(prompt_token_size_)}, SPECIAL_TOKEN_ID);

        if (expected_tokens.size() != img_inferred_buffer.d1)
        {
            throw std::runtime_error("vision embeddings tokens is not correct: "
                                     + std::to_string(expected_tokens.size()) + " ,"
                                     + std::to_string(img_inferred_buffer.d1));
        }
    }

    FloatBufferView embedded_raw_fbuf{qnn_embedding_info_.embedded_raw_buf_};
    size_t num_tokens = prompt_token_size_;
    embedded_bin_.resize(num_tokens * HIDDEN_DIM);
    int vision_idx = 0;

    for (size_t i = 0; i < num_tokens; ++i)
    {
        int32_t token_id = prompt_token_[i];
        float *dest_ptr = &embedded_bin_[i * HIDDEN_DIM];

        if (token_id == SPECIAL_TOKEN_ID)
        {
            // Use vision embedding
            if ((vision_idx + 1) * HIDDEN_DIM > img_inferred_buffer.buf.size())
            {
                throw std::runtime_error("not enough vision embeddings for special tokens");
            }

            const float *src_ptr = &img_inferred_buffer.buf.data()[vision_idx * HIDDEN_DIM];
            std::memcpy(dest_ptr, src_ptr, HIDDEN_DIM * sizeof(float));
            vision_idx++;
        }
        else
        {
            if ((size_t) (token_id + 1) * HIDDEN_DIM > embedded_raw_fbuf.size_)
            {
                throw std::runtime_error(std::string{"token id is out of bounds, "}
                                         + "embedding_weights_fbuf size: " + std::to_string(embedded_raw_fbuf.size_) + ", "
                                         + "(token_id + 1) * HIDDEN_DIM: " + std::to_string((token_id + 1) * HIDDEN_DIM));
            }
            const float *src_ptr = &embedded_raw_fbuf.pointer_[token_id * HIDDEN_DIM];
            std::memcpy(dest_ptr, src_ptr, HIDDEN_DIM * sizeof(float));
        }
    }
    return *this;
}

QInterface::PHI4Embedding::~PHI4Embedding()
{
    stbi_ldr_to_hdr_gamma(2.2f);
}
