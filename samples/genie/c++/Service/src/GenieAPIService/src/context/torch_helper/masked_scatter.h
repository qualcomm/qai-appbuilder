//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef MASKED_SCATTER_H
#define MASKED_SCATTER_H
/*
 * masked_scatter.h
 *
 * 从 Qwen2_5::MergeEmbedding() 抽取的通用逻辑：按 image_token_id 在 prompt_token 中出现的
 * 位置，把视觉 encoder 输出的 embedding 合并进文本 embedding 序列，支持两种分支
 * （与原实现完全一致，行为未做任何改写）：
 *  1) 图像 token 数量与视觉特征条目数量精确匹配：逐行直接替换。
 *  2) 仅存在 1 个占位 token：展开为 N 个连续的 image_token_id，并对 embedding 做 slice+concat。
 * 仅供 Qwen3VL 调用，qwen_2_5.cpp/h 保持原样不做任何改动。
 */

#include <algorithm>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

#include "base.h"

namespace torch_helper
{
    template<typename T>
    void MaskedScatterMergeEmbedding(const int32_t *prompt_token,
                                     size_t token_count,
                                     int32_t image_token_id,
                                     const std::vector<T> &embedded_raw_fbuf,
                                     const BufferView<T> &img_embedding_fbuf,
                                     std::vector<T> &embedded_bin)
    {
        size_t n_image_tokens = 0;
        for (size_t i = 0; i < token_count; ++i)
        {
            if (prompt_token[i] == image_token_id)
            {
                ++n_image_tokens;
            }
        }

        if (embedded_raw_fbuf.size() % token_count != 0)
        {
            throw std::runtime_error(std::string{"embeddings raw buf length is not divisible by sequence length, "}
                                     + "embedded_raw_fbuf.size_: " + std::to_string(embedded_raw_fbuf.size()) + " "
                                     + "num_tokens: " + std::to_string(token_count));
        }
        const size_t D = embedded_raw_fbuf.size() / token_count;
        if (img_embedding_fbuf.size_ % D != 0)
        {
            throw std::runtime_error("image embeds buf length is not divisible by embed dim D");
        }
        const size_t N_feat = img_embedding_fbuf.size_ / D;

        if (n_image_tokens != N_feat)
        {
            if (n_image_tokens != 1 || n_image_tokens == 0)
            {
                throw std::runtime_error("expected exactly 1 image token placeholder for expansion");
            }

            size_t pos = token_count;
            for (size_t i = 0; i < token_count; ++i)
            {
                if (prompt_token[i] == image_token_id)
                {
                    pos = i;
                    break;
                }
            }
            if (pos == token_count)
            {
                throw std::runtime_error("Image token placeholder not found");
            }

            std::vector<int32_t> new_input_ids;
            new_input_ids.reserve(token_count - 1 + N_feat);
            new_input_ids.insert(new_input_ids.end(), prompt_token, prompt_token + pos);
            for (size_t k = 0; k < N_feat; ++k)
            {
                new_input_ids.push_back(image_token_id);
            }
            new_input_ids.insert(new_input_ids.end(), prompt_token + pos + 1, prompt_token + token_count);

            const size_t left_count = pos * D;
            const size_t mid_count = N_feat * D;

            embedded_bin.reserve(left_count + mid_count + (token_count - pos - 1) * D);
            embedded_bin.insert(embedded_bin.end(), embedded_raw_fbuf.data(), embedded_raw_fbuf.data() + left_count);
            embedded_bin.insert(embedded_bin.end(), img_embedding_fbuf.pointer_, img_embedding_fbuf.pointer_ + mid_count);
            embedded_bin.insert(embedded_bin.end(),
                                embedded_raw_fbuf.data() + (pos + 1) * D,
                                embedded_raw_fbuf.data() + embedded_raw_fbuf.size());

            std::vector<size_t> seq_pos;
            for (size_t i = 0; i < new_input_ids.size(); ++i)
            {
                if (new_input_ids[i] == image_token_id)
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
                const T *src = &img_embedding_fbuf.pointer_[j * D];
                T *dst = &embedded_bin[row * D];
                std::copy(src, src + D, dst);
            }
        }
        else
        {
            embedded_bin.assign(embedded_raw_fbuf.data(), embedded_raw_fbuf.data() + embedded_raw_fbuf.size());
            size_t matched = 0;
            for (size_t i = 0; i < token_count; ++i)
            {
                if (prompt_token[i] == image_token_id)
                {
                    const T *src = &img_embedding_fbuf.pointer_[matched * D];
                    T *dst = &embedded_bin[i * D];
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
    }
}

#endif //MASKED_SCATTER_H
