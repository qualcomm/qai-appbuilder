//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================


#include "qwen_2_5_omini.h"

#define DR_WAV_IMPLEMENTATION
//
//#include <dr_wav.h>
//#include <samplerate.h>

IAudioEmbedding &QInterface::Qwen2_5OMINI::BuildAudioSamples()
{
    throw std::runtime_error("not impl yet");
//    uint64_t frames;
//    uint32_t channels;
//    uint32_t sampleRate;
//
//    float *pcm_float = drwav_open_memory_and_read_pcm_frames_f32(
//            audio_buf_.data(),
//            audio_buf_.size(),
//            &channels,
//            &sampleRate,
//            &frames,
//            nullptr);
//
//    if (!pcm_float)
//    {
//        throw std::runtime_error("open wav file buffer failed");
//    }
//
//    uint64_t pcm_len{frames * channels};
//
//    // 1) Convert to mono by averaging channels
//    float *mono;
//    uint64_t mono_len{};
//    if (channels == 1)
//    {
//        mono = pcm_float;
//    }
//    else
//    {
//        mono = new float[pcm_len];
//        mono_len = pcm_len;
//
//        for (uint64_t i = 0; i < frames; ++i)
//        {
//            float sum = 0.0f;
//            const float *ptr = &pcm_float[i * channels];
//            for (int c = 0; c < channels; ++c)
//                sum += ptr[c];
//            mono[i] = sum / static_cast<float>(channels);
//        }
//    }
//
//    // 2) Prepare libsamplerate SRC_DATA
//    const int target_sr = 16000;
//    double src_ratio = static_cast<double>(target_sr) / static_cast<double>(sampleRate);
//    uint64_t out_frames_est = static_cast<uint64_t>(std::ceil(frames * src_ratio));
//    audio_sample_buf_.resize(out_frames_est);
//    SRC_DATA src_data;
//    src_data.data_in = mono;
//    src_data.input_frames = static_cast<long>(frames);
//    src_data.data_out = audio_sample_buf_.data();
//    src_data.output_frames = static_cast<long>(out_frames_est);
//    src_data.src_ratio = src_ratio;
//    src_data.end_of_input = 1;
//
//    int err = src_simple(&src_data, SRC_SINC_BEST_QUALITY, 1); // 1 channel (mono)
//    auto clean{[&mono_len, &pcm_float, &mono]()
//               {
//                   if (mono_len)
//                   {
//                       delete[] mono;
//                   }
//                   drwav_free(pcm_float, nullptr);
//               }};
//    if (err != 0)
//    {
//        clean();
//        throw std::runtime_error(std::string("libsamplerate error: ") + src_strerror(err));
//    }
//
//    clean();
//    return *this;
}

IEmbedding &QInterface::Qwen2_5OMINI::MergeEmbedding()
{
//    static const int32_t rows{151936};
//    static const size_t cols{2048};
//    const unsigned long token_count = prompt_token_.size();
//    FloatBufferView tmp_raw_fbuf{qnn_embedding_info_.embedded_raw_buf_};
//    std::vector<uint8_t> out;
//    out.reserve(token_count * cols * sizeof(float));
//    for (int32_t &id: question_embedding_buf_)
//    {
//        const float *p = tmp_raw_fbuf.pointer_ + id * cols;
//        const auto *b = reinterpret_cast<const uint8_t *>(p);
//        out.insert(out.end(), b, b + cols * sizeof(float));
//    }
//
//    FloatBufferView embedded_raw_fbuf{out};
//    uint64_t n_audio_tokens{};
//    for (int32_t &id: question_embedding_buf_)
//    {
////        My_Log{} << id << " ";
//
//        if (id == 151646)
//        {
//            ++n_audio_tokens;
//        }
//    }
////    My_Log{} << n_audio_tokens;
    throw std::runtime_error("not impl yet");
//    FloatBufferView img_embedding_fbuf{img_inferred_buf_};

//    // 2) 从 inputs_embeds 推断 D（嵌入维度）
//    if (embedded_raw_fbuf.size_ % token_count != 0)
//    {
//        throw std::runtime_error(std::string{"embeddings raw buf length is not divisible by sequence length, "}
//                                 + "embedded_raw_fbuf.size_: " + std::to_string(embedded_raw_fbuf.size_) + " "
//                                 + "num_tokens: " + std::to_string(question_embedding_buf_.size()));
//    }
//
//    //  3) 将 image_embeds_flat 视作 [N_feat, D]
//    const size_t D = embedded_raw_fbuf.size_ / token_count;
//    if (img_embedding_fbuf.size_ % D != 0)
//    {
//        throw std::runtime_error("image embeds buf length is not divisible by embed dim D");
//    }
//    const size_t N_feat = img_embedding_fbuf.size_ / D;
//
//    // 4) 统计当前序列中的图像 token 数量
//    size_t n_image_tokens = 0;
//    for (size_t i = 0; i < token_count; ++i)
//    {
//        if (question_embedding_buf_[i] == rows)
//        {
//            ++n_image_tokens;
//        }
//    }
//
//    // 5) 图像 token 数量与特征条目数量的匹配 / 扩展（与原脚本相同）
//    // 若不匹配：仅支持存在恰好 1 个占位 token，将其展开为 N_feat 个连续的 image_token_id，
//    // 并对 inputs_embeds 进行 slice + concat，然后再进行替换。
//    std::vector<float> output_bin_buf;  // 最终的 inputs_embeds_np（展平）
//    std::vector<int32_t> new_input_ids; // 若发生扩展，更新后的 input_ids
//
//    if (n_image_tokens != N_feat)
//    {
//        // 约束：batch_size == 1 且恰好 1 个占位 token
//        if (n_image_tokens != 1)
//        {
//            throw std::runtime_error("expected exactly 1 image token placeholder for expansion");
//        }
//
//        size_t pos = token_count; // 初始化为非法值
//        for (size_t i = 0; i < token_count; ++i)
//        {
//            if (question_embedding_buf_[i] == rows)
//            {
//                pos = i;
//                break;
//            }
//        }
//        if (pos == token_count)
//        {
//            throw std::runtime_error("Image token placeholder not found");
//        }
//
//        // 构造新的 input_ids：左段 + N_feat 个 image_token_id + 右段
//        new_input_ids.reserve(token_count - 1 + N_feat);
//        new_input_ids.insert(new_input_ids.end(),
//                             question_embedding_buf_.begin(),
//                             question_embedding_buf_.begin() + pos);
//        for (size_t k = 0; k < N_feat; ++k)
//        {
//            new_input_ids.push_back(rows);
//        }
//        new_input_ids.insert(new_input_ids.end(),
//                             question_embedding_buf_.begin() + pos + 1,
//                             question_embedding_buf_.end());
//
//        // slice + concat 拼接嵌入：
//        // 左段 = inputs_embeds_flat[0 : pos*D]
//        // 中段 = image_embeds_flat[0 : N_feat*D]
//        // 右段 = inputs_embeds_flat[(pos+1)*D : S*D]
//        const size_t left_count = pos * D;
//        const size_t mid_count = N_feat * D;
//        const size_t right_count = (token_count - pos - 1) * D;
//
//        output_bin_buf.reserve(left_count + mid_count + right_count);
//        output_bin_buf.insert(output_bin_buf.end(),
//                              embedded_raw_fbuf.pointer_,
//                              embedded_raw_fbuf.pointer_ + left_count);
//        output_bin_buf.insert(output_bin_buf.end(),
//                              img_embedding_fbuf.pointer_,
//                              img_embedding_fbuf.pointer_ + mid_count);
//        output_bin_buf.insert(output_bin_buf.end(),
//                              embedded_raw_fbuf.pointer_ + (pos + 1) * D,
//                              embedded_raw_fbuf.pointer_ + embedded_raw_fbuf.size_);
//
//        // 再次替换：将 new_input_ids 中所有 image_token_id 对应的行，用 image_embeds 逐条覆盖
//        // （与 Python 保持一致的“顺序映射”逻辑）
//        {
//            // 收集新序列中所有 image_token_id 的位置
//            std::vector<size_t> seq_pos;
//            for (size_t i = 0; i < new_input_ids.size(); ++i)
//            {
//                if (new_input_ids[i] == rows)
//                {
//                    seq_pos.push_back(i);
//                }
//            }
//            if (seq_pos.size() != N_feat)
//            {
//                throw std::runtime_error("after expansion, number of image tokens != number of image features");
//            }
//            // 用 image_embeds_flat 覆盖 outputs_embeds_flat 相应行
//            for (size_t j = 0; j < N_feat; ++j)
//            {
//                size_t row = seq_pos[j];
//                const float *src = &img_embedding_fbuf.pointer_[j * D];
//                float *dst = &output_bin_buf[row * D];
//                std::copy(src, src + D, dst);
//            }
//        }
//
//        // 覆盖 input_ids
//        //        question_embedding_buf_.assign(reinterpret_cast<int32_t *>(new_input_ids.data()),
//        //                                       reinterpret_cast<int32_t *>(new_input_ids.data()) +
//        //                                       (new_input_ids.size() * (sizeof(int64_t) / sizeof(int32_t))));
//        question_embedding_buf_.swap(new_input_ids);
//    }
//    else
//    {
//        // 数量一致：直接替换（batch_size == 1）
//        // 先复制原嵌入
//        output_bin_buf.assign(embedded_raw_fbuf.pointer_, embedded_raw_fbuf.pointer_ + embedded_raw_fbuf.size_);
//        size_t matched = 0;
//        for (size_t i = 0; i < token_count; ++i)
//        {
//            if (question_embedding_buf_[i] == rows)
//            {
//                // 将第 matched 条图像特征写入到第 i 行
//                const float *src = &img_embedding_fbuf.pointer_[matched * D];
//                float *dst = &output_bin_buf[i * D];
//                std::copy(src, src + D, dst);
//                ++matched;
//                if (matched > N_feat)
//                {
//                    throw std::runtime_error("more image tokens than features");
//                }
//            }
//        }
//        if (matched != N_feat)
//        {
//            throw std::runtime_error("number of image tokens != number of image features");
//        }
//    }

}

//IEmbedding &QInterface::Qwen2_5OMINI::BuildTextEmbedding(const std::string &question)
//{
//    static const int NUM_IMAGE_TOKENS = 494;
//    static const int32_t TOKEN_USER_START = 200021;
//    static const int32_t TOKEN_IMAGE_PLACEHOLDER = 200010;
//    static const int32_t TOKEN_END = 200020;
//    static const int32_t TOKEN_ASSISTANT = 200019;
//
//    IEmbedding::BuildTextEmbedding(question);
//    std::vector<int32_t> new_buf;
//
//    new_buf.reserve(1 + NUM_IMAGE_TOKENS + question_embedding_buf_.size() + 2);
//    new_buf.push_back(TOKEN_USER_START);
//    for (int i = 0; i < NUM_IMAGE_TOKENS; ++i)
//    {
//        new_buf.push_back(TOKEN_IMAGE_PLACEHOLDER);
//    }
//
//    new_buf.insert(new_buf.end(), question_embedding_buf_.begin(), question_embedding_buf_.end());
//    new_buf.push_back(TOKEN_END);
//    new_buf.push_back(TOKEN_ASSISTANT);
//    question_embedding_buf_ = new_buf;
//    return *this;
//}

