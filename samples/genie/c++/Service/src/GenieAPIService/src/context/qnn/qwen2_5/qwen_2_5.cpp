//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include <stb_image.h>
#include <stb_image_resize2.h>

#include "qwen_2_5.h"
#include "qwen25_image_processor.hpp"
#include "../../torch_helper/base.h"

IVisionEmbedding &QInterface::Qwen2_5::BuildImgPixel()
{
    using namespace qwen2_5;
    int rows = 0, cols = 0;
    Qwen25ImageProcessor proc;
    proc.ProcessToBuffer(img_buf_.data(), img_buf_.size(), kHeight, kWidth, img_pixel_buf_, rows, cols);
    img_buf_.clear();
    return *this;
}

IVisionEmbedding &QInterface::Qwen2_5::MergeEmbedding()
{
    static const int32_t rows{151655};
    const unsigned long token_count = prompt_token_size_;
    BufferView<float> tmp_raw_fbuf{qnn_embedding_info_.embedded_raw_buf_};

    std::vector<float> embedded_raw_fbuf;
    embedded_raw_fbuf.resize(token_count * cols_);
    float *dest_ptr;
    for (uint32_t i = 0; i < prompt_token_size_; ++i)
    {
        dest_ptr = &embedded_raw_fbuf[i * cols_];
        float *src_ptr = &tmp_raw_fbuf.pointer_[prompt_token_[i] * cols_];
        std::memcpy(dest_ptr, src_ptr, cols_ * sizeof(float));
    }

    if (img_inferred_buffers_.empty())
    {
        embedded_bin_ = std::move(embedded_raw_fbuf);
        input_data_ = reinterpret_cast<uint8_t*>(embedded_bin_.data());
        input_len_ = embedded_bin_.size() * sizeof(float);
        return *this;
    }

    BufferView<float> img_embedding_fbuf{img_inferred_buffers_[0]};

    size_t n_image_tokens = 0;
    for (size_t i = 0; i < token_count; ++i)
    {
        if (prompt_token_[i] == rows)
        {
            ++n_image_tokens;
        }
    }

    if (embedded_raw_fbuf.size() % token_count != 0)
    {
        throw std::runtime_error(std::string{"embeddings raw buf length is not divisible by sequence length, "}
                                 + "embedded_raw_fbuf.size_: " + std::to_string(embedded_raw_fbuf.size()) + " "
                                 + "num_tokens: " + std::to_string(prompt_token_size_));
    }

    const size_t D = embedded_raw_fbuf.size() / token_count;
    if (img_embedding_fbuf.size_ % D != 0)
    {
        throw std::runtime_error("image embeds buf length is not divisible by embed dim D");
    }
    const size_t N_feat = img_embedding_fbuf.size_ / D;

    std::vector<int32_t> new_input_ids;
    if (n_image_tokens != N_feat)
    {

        if (n_image_tokens != 1 || n_image_tokens == 0)
        {
            throw std::runtime_error("expected exactly 1 image token placeholder for expansion");
        }

        size_t pos = token_count;
        for (size_t i = 0; i < token_count; ++i)
        {
            if (prompt_token_[i] == rows)
            {
                pos = i;
                break;
            }
        }
        if (pos == token_count)
        {
            throw std::runtime_error("Image token placeholder not found");
        }

        new_input_ids.reserve(token_count - 1 + N_feat);
        new_input_ids.insert(new_input_ids.end(),
                             prompt_token_,
                             prompt_token_ + pos);

        for (size_t k = 0; k < N_feat; ++k)
        {
            new_input_ids.push_back(rows);
        }
        new_input_ids.insert(new_input_ids.end(),
                             prompt_token_ + pos + 1,
                             prompt_token_ + token_count);

        const size_t left_count = pos * D;
        const size_t mid_count = N_feat * D;
        const size_t right_count = (token_count - pos - 1) * D;

        embedded_bin_.reserve(left_count + mid_count + right_count);
        embedded_bin_.insert(embedded_bin_.end(),
                             embedded_raw_fbuf.data(),
                             embedded_raw_fbuf.data() + left_count);

        embedded_bin_.insert(embedded_bin_.end(),
                             img_embedding_fbuf.pointer_,
                             img_embedding_fbuf.pointer_ + mid_count);

        embedded_bin_.insert(embedded_bin_.end(),
                             embedded_raw_fbuf.data() + (pos + 1) * D,
                             embedded_raw_fbuf.data() + embedded_raw_fbuf.size());

        std::vector<size_t> seq_pos;
        for (size_t i = 0; i < new_input_ids.size(); ++i)
        {
            if (new_input_ids[i] == rows)
            {
                seq_pos.push_back(i);
            }
        }
        if (seq_pos.size() != N_feat)
        {
            throw std::runtime_error("after expansion, number of image tokens != number of image features");
        }

        for (size_t j = 0; j < N_feat; ++j)
        {
            size_t row = seq_pos[j];
            const float *src = &img_embedding_fbuf.pointer_[j * D];
            float *dst = &embedded_bin_[row * D];
            std::copy(src, src + D, dst);
        }
    }
    else
    {

        embedded_bin_.assign(embedded_raw_fbuf.data(), embedded_raw_fbuf.data() + embedded_raw_fbuf.size());
        size_t matched = 0;
        for (size_t i = 0; i < token_count; ++i)
        {
            if (prompt_token_[i] == rows)
            {

                const float *src = &img_embedding_fbuf.pointer_[matched * D];
                float *dst = &embedded_bin_[i * D];
                std::copy(src, src + D, dst);
                ++matched;
                if (matched > N_feat)
                {
                    throw std::runtime_error("more image tokens than features");
                }
            }
        }
        if (matched != N_feat)
        {
            throw std::runtime_error("number of image tokens != number of image features");
        }
    }

    input_data_ = reinterpret_cast<uint8_t*>(embedded_bin_.data());
    input_len_ = embedded_bin_.size() * sizeof(float);
    return *this;
}
