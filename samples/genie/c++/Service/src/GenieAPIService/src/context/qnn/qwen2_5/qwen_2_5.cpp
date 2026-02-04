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

IVisionEmbedding &QInterface::Qwen2_5::BuildImgPixel()
{
    using namespace qwen2_5;
    int rows = 0, cols = 0;
    Qwen25ImageProcessor proc;
    proc.ProcessToBuffer(img_buf_.data(), img_buf_.size(), kHeight, kWidth, img_pixel_buf_, rows, cols);
    img_buf_.clear();
    return *this;
}

IVisionEmbedding & QInterface::Qwen2_5::MergeEmbedding()
{
    static const int32_t rows{151655};
    static const size_t cols{2048};
    const unsigned long token_count = prompt_token_size_;
    FloatBufferView tmp_raw_fbuf{qnn_embedding_info_.embedded_raw_buf_};

    std::vector<float> embedded_raw_fbuf;
    embedded_raw_fbuf.resize(token_count * cols);
    float *dest_ptr;
    for (uint32_t i = 0; i < prompt_token_size_; ++i)
    {
        dest_ptr = &embedded_raw_fbuf[i * cols];
        float *src_ptr = &tmp_raw_fbuf.pointer_[prompt_token_[i] * cols];
        std::memcpy(dest_ptr, src_ptr, cols * sizeof(float));
    }

    FloatBufferView img_embedding_fbuf{img_inferred_buf_};

    // 统计当前序列中的图像 token 数量
    size_t n_image_tokens = 0;
    for (size_t i = 0; i < token_count; ++i)
    {
        if (prompt_token_[i] == rows)
        {
            ++n_image_tokens;
        }
    }

    // 2) 从 inputs_embeds 推断 D（嵌入维度）
    if (embedded_raw_fbuf.size() % token_count != 0)
    {
        throw std::runtime_error(std::string{"embeddings raw buf length is not divisible by sequence length, "}
                                 + "embedded_raw_fbuf.size_: " + std::to_string(embedded_raw_fbuf.size()) + " "
                                 + "num_tokens: " + std::to_string(prompt_token_size_));
    }

    //  3) 将 image_embeds_flat 视作 [N_feat, D]
    const size_t D = embedded_raw_fbuf.size() / token_count;
    if (img_embedding_fbuf.size_ % D != 0)
    {
        throw std::runtime_error("image embeds buf length is not divisible by embed dim D");
    }
    const size_t N_feat = img_embedding_fbuf.size_ / D;

    // 5) 图像 token 数量与特征条目数量的匹配 / 扩展（与原脚本相同）
    // 若不匹配：仅支持存在恰好 1 个占位 token，将其展开为 N_feat 个连续的 image_token_id，
    // 并对 inputs_embeds 进行 slice + concat，然后再进行替换。
    std::vector<int32_t> new_input_ids; // 若发生扩展，更新后的 input_ids
    if (n_image_tokens != N_feat)
    {
        // 约束：batch_size == 1 且恰好 1 个占位 token
        if (n_image_tokens != 1 || n_image_tokens == 0)
        {
            throw std::runtime_error("expected exactly 1 image token placeholder for expansion");
        }

        size_t pos = token_count; // 初始化为非法值
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

        // 构造新的 input_ids：左段 + N_feat 个 image_token_id + 右段
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

        // slice + concat 拼接嵌入：
        // 左段 = inputs_embeds_flat[0 : pos*D]
        // 中段 = image_embeds_flat[0 : N_feat*D]
        // 右段 = inputs_embeds_flat[(pos+1)*D : S*D]
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
                              embedded_raw_fbuf.data() + embedded_raw_fbuf.size());  // question_embedding_buf_.end()

        // 再次替换：将 new_input_ids 中所有 image_token_id 对应的行，用 image_embeds 逐条覆盖
        // （与 Python 保持一致的“顺序映射”逻辑）
        // 收集新序列中所有 image_token_id 的位置
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
        // 用 image_embeds_flat 覆盖 outputs_embeds_flat 相应行
        for (size_t j = 0; j < N_feat; ++j)
        {
            size_t row = seq_pos[j];
            const float *src = &img_embedding_fbuf.pointer_[j * D];
            float *dst = &embedded_bin_[row * D];
            std::copy(src, src + D, dst);
        }

        // We do not need it now.
        //        free(prompt_token_);
        //        prompt_token_ = static_cast<int32_t *>(malloc(new_input_ids.size()));
        //        memcpy(prompt_token_, new_input_ids.data(), new_input_ids.size());
        //        prompt_token_.swap(new_input_ids);
    }
    else
    {
        // 数量一致：直接替换（batch_size == 1）
        // 先复制原嵌入
        embedded_bin_.assign(embedded_raw_fbuf.data(), embedded_raw_fbuf.data() + embedded_raw_fbuf.size());
        size_t matched = 0;
        for (size_t i = 0; i < token_count; ++i)
        {
            if (prompt_token_[i] == rows)
            {
                // 将第 matched 条图像特征写入到第 i 行
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

    return *this;   // 最终的 inputs_embeds_np（展平）
}
