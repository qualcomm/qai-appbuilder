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

#include "genie.h"
#include "genie_impl.h"
#include "log.h"
#include "utils.h"
#include "base64.h"
#include <LibAppBuilder.hpp>

#include <stb_image.h>
#include <stb_image_resize2.h>
#include <stb_image_write.h>
#include "qnn_img_helper/phi4mm_image_processor.hpp"
#include "qnn_img_helper/qwen25_image_processor.hpp"

using Impl = GenieContext::Impl;

#define GENIE_BUILDER_DEBUG 1

#ifdef WIN32
#pragma warning(push)
#pragma warning(disable:4715)
#endif

Impl::QInterface *Impl::QInterface::CreateInterface(GenieContext *context)
{
    switch (context->model_config_.get_query_type().v_)
    {
        case QueryType::TextQuery:
            return new IGeneral{context};
        case QueryType::TokenQuery:
            throw std::runtime_error("token query not impl yet");
        case QueryType::EmbeddingQuery:
            return IEmbedding::CreateInterface(context);
    }
}

#ifdef WIN32
#pragma warning(pop)
#endif

void Impl::QInterface::GenieCallBack(const char *response,
                                     const GenieDialog_SentenceCode_t sentence_code,
                                     const void *user_data)
{
    auto *self = static_cast<QInterface *>(const_cast<void *>(user_data))->context_;
    if (sentence_code == GENIE_DIALOG_SENTENCE_END
        || !response
        || !strlen(response))
    {
        return;
    }

    std::lock_guard guard(self->m_stream_lock);
    self->m_stream_answer += response;

    auto &impl = self->impl_;
    impl->CurLength += self->TokenLength(response);
    if (impl->CurLength >= impl->MaxLength)
    {
        My_Log{My_Log::Level::kError} << "g_CurLength: " << impl->CurLength << "is over and will stop self"
                << std::endl;
        self->Stop();
    }
}

bool Impl::IEmbedding::set_content(const std::string &prompt)
{
    try
    {
        json j = json::parse(prompt);
        std::string prompt_text{j["question"]};
        if (pre_process_prompt_)
        {
            prompt_text = pre_process_prompt_(std::move(prompt_text));
        }

        /* @formatter:off */
        embedded_bin_ =  DecodeImg(j["image"])
                        .BuildImgPixel()
                        .BuildPixelEmbeddings()
                        .BuildTextEmbedding(std::move(prompt_text))
                        .MergeEmbedding();
        /* @formatter:on */

        prompt_data_ = reinterpret_cast<uint8_t *>(embedded_bin_.data());
        prompt_len_ = embedded_bin_.size() * sizeof(float);
    }
    catch (std::exception &e)
    {
        My_Log{My_Log::Level::kError} << "set content failed: " << e.what() << "\n";
        return false;
    }

    return true;
}

Impl::IEmbedding &Impl::IEmbedding::BuildTextEmbedding(const std::string &question)
{
    const int32_t *buf;
    uint32_t len;
    if (!context_->GenerateTextToken(question, buf, len))
    {
        throw std::runtime_error("generate text token failed");
    }
    question_embedding_buf_.assign(buf, buf + len);
    free((void *) buf);
    return *this;
}

void Impl::IEmbedding::TokenToEmbedCallback(const int32_t token,
                                            void *embedding,
                                            const uint32_t embeddingSize,
                                            const void *userData)
{
    auto *self = static_cast<IEmbedding *>(const_cast<void *>(userData));

    const size_t lutIndex = token * embeddingSize;
    if (lutIndex + embeddingSize <= self->embedded_raw_buf_.size())
    {
        const int8_t *embeddingSrc = reinterpret_cast<const int8_t *>(self->embedded_raw_buf_.data()) + lutIndex;
        auto *embeddingDst = static_cast<int8_t *>(embedding);
        std::copy(embeddingSrc, embeddingSrc + embeddingSize, embeddingDst);
    }
    else
    {
        My_Log{My_Log::Level::kError} << "Error: T2E conversion overflow.\n";
    }
}

Impl::IEmbedding &GenieContext::Impl::IEmbedding::DecodeImg(std::string &&img_hash)
{
    img_buf_.resize(BASE64_DECODE_OUT_SIZE(img_hash.size()));
    if (Base64Decode(img_hash.data(), img_hash.size(), img_buf_.data()) == 0)
    {
        throw std::runtime_error("decode to binrary failed");
    }
    return *this;
}

Impl::IEmbedding &Impl::IEmbedding::BuildPixelEmbeddings()
{
#ifdef WIN32
#define BACKEND "QnnHtp.dll"
#define SYSTEM "QnnSystem.dll"
#else
#define BACKEND "libQnnHtp.so"
#define SYSTEM "libQnnSystem.so"
#endif

    static const char *model_name{"model_name"};
    static std::string perfProfile = "burst";

    LibAppBuilder app_builder;
    SetLogLevel(GENIE_LOG_LEVEL_ERROR, "");
    My_Log{} << "start to initiate: " << qnn_embedding_info_.init_bin_file_ << " ....\n";
    if (!app_builder.ModelInitialize(model_name, qnn_embedding_info_.init_bin_file_, BACKEND, SYSTEM))
    {
        throw std::runtime_error("call model initialize failed");
    }

    std::vector<uint8_t *> inputBuffers(qnn_embedding_info_.bin_stack_.size() + 1);
    inputBuffers[0] = reinterpret_cast<uint8_t *>(img_pixel_buf_.data());
    for (auto i = 0; i < qnn_embedding_info_.bin_stack_.size(); ++i)
    {
        inputBuffers[i + 1] = const_cast<uint8_t *>(qnn_embedding_info_.bin_stack_[i].data());
    }

    std::vector<uint8_t *> outputBuffers;
    std::vector<size_t> outputSize;
    if (!app_builder.ModelInference(model_name, inputBuffers, outputBuffers, outputSize, perfProfile))
    {
        app_builder.ModelDestroy(model_name);
        throw std::runtime_error("call model inference failed");
    }

    img_pixel_buf_.clear();
    img_embedding_buf_.assign(outputBuffers.at(0), outputBuffers.at(0) + outputSize.at(0));
    for (auto &outputBuffer: outputBuffers)
        free(outputBuffer);
    app_builder.ModelDestroy(model_name);
    return *this;
}

Impl::IEmbedding &GenieContext::Impl::PHI4Embedding::BuildTextEmbedding(const std::string &question)
{
    static const int NUM_IMAGE_TOKENS = 494;
    static const int32_t TOKEN_USER_START = 200021;
    static const int32_t TOKEN_IMAGE_PLACEHOLDER = 200010;
    static const int32_t TOKEN_END = 200020;
    static const int32_t TOKEN_ASSISTANT = 200019;

    IEmbedding::BuildTextEmbedding(question);
    std::vector<int32_t> new_buf;

    new_buf.reserve(1 + NUM_IMAGE_TOKENS + question_embedding_buf_.size() + 2);
    new_buf.push_back(TOKEN_USER_START);
    for (int i = 0; i < NUM_IMAGE_TOKENS; ++i)
    {
        new_buf.push_back(TOKEN_IMAGE_PLACEHOLDER);
    }

    new_buf.insert(new_buf.end(), question_embedding_buf_.begin(), question_embedding_buf_.end());
    new_buf.push_back(TOKEN_END);
    new_buf.push_back(TOKEN_ASSISTANT);
    question_embedding_buf_ = new_buf;
    return *this;
}

Impl::IEmbedding &Impl::PHI4Embedding::BuildImgPixel()
{
    using namespace phi4mm;
    static const int dynamic_hd = 12;
    ImageRGB8 img;
    if (!Phi4MMImageProcessor::loadImageRGB8_ByMEM(img_buf_.data(), img_buf_.size(), img))
    {
        throw std::runtime_error("load image from buffer failed");
    }
    img_buf_.clear();

    img = Phi4MMImageProcessor::resizeBicubicU8(img, kWidth, kHeight, -0.5f, false);
    auto out = Phi4MMImageProcessor::runPipeline(img, dynamic_hd, kWidth, 32);
    if (out.num_img_tokens.empty())
    {
        throw std::runtime_error("run pipeline for generate failed");
    }

    const auto *src = out.image_inputs.data.data();
    img_pixel_buf_.assign(src, src + out.image_inputs.data.size());
    return *this;
}

std::vector<float> Impl::PHI4Embedding::MergeEmbedding()
{
    static const int HIDDEN_DIM = 3072;
    static const int SPECIAL_TOKEN_ID = 200010;
    std::vector<float> output_bin_buf;
    size_t num_tokens = question_embedding_buf_.size();
    output_bin_buf.resize(num_tokens * HIDDEN_DIM);

    size_t vision_idx = 0;
    FloatBufferView embedded_raw_fbuf{embedded_raw_buf_};
    FloatBufferView img_embedding_fbuf{img_embedding_buf_};

    for (size_t i = 0; i < num_tokens; ++i)
    {
        int32_t token_id = question_embedding_buf_[i];
        float *dest_ptr = &output_bin_buf[i * HIDDEN_DIM];

        if (token_id == SPECIAL_TOKEN_ID)
        {
            // Use vision embedding
            if ((vision_idx + 1) * HIDDEN_DIM > img_embedding_fbuf.size_)
            {
                throw std::runtime_error("not enough vision embeddings for special tokens");
            }

            const float *src_ptr = &img_embedding_fbuf.pointer_[vision_idx * HIDDEN_DIM];
            std::memcpy(dest_ptr, src_ptr, HIDDEN_DIM * sizeof(float));
            vision_idx++;
        }
        else
        {
            if ((size_t) (token_id + 1) * HIDDEN_DIM > embedded_raw_fbuf.size_)
            {
                throw std::runtime_error(
                    std::string{"token id is out of bounds, "}
                    + "embedding_weights_fbuf size: " + std::to_string(embedded_raw_fbuf.size_) + ", "
                    + "(token_id + 1) * HIDDEN_DIM: " + std::to_string((token_id + 1) * HIDDEN_DIM));
            }

            const float *src_ptr = &embedded_raw_fbuf.pointer_[token_id * HIDDEN_DIM];
            std::memcpy(dest_ptr, src_ptr, HIDDEN_DIM * sizeof(float));
        }
    }

    return output_bin_buf;
}

template<typename T>
std::vector<T> ReadBinaryFile(const std::string &path)
{
    std::ifstream ifs(path, std::ios::binary);
    if (!ifs)
    {
        throw std::runtime_error("Failed to open file: " + path);
    }
    ifs.seekg(0, std::ios::end);
    std::streampos end = ifs.tellg();
    ifs.seekg(0, std::ios::beg);
    std::streamsize bytes = end - ifs.tellg();
    if (bytes % sizeof(T) != 0)
    {
        throw std::runtime_error("File size not aligned with type size: " + path);
    }
    size_t count = static_cast<size_t>(bytes / sizeof(T));
    std::vector<T> data(count);
    if (!ifs.read(reinterpret_cast<char *>(data.data()), bytes))
    {
        throw std::runtime_error("Failed to read file: " + path);
    }
    return data;
}

Impl::IEmbedding &GenieContext::Impl::Qwen2_5Embedding::BuildImgPixel()
{
    using namespace qwen2_5;
    int rows = 0, cols = 0;
    Qwen25ImageProcessor proc;
    proc.ProcessToBuffer(img_buf_.data(), img_buf_.size(), kHeight, kWidth, img_pixel_buf_, rows, cols);
    img_buf_.clear();
    return *this;
}

std::vector<float> GenieContext::Impl::Qwen2_5Embedding::MergeEmbedding()
{
    static const int32_t rows{151655};
    static const size_t cols{2048};
    const unsigned long token_count = question_embedding_buf_.size();

    FloatBufferView tmp_raw_fbuf{embedded_raw_buf_};
    std::vector<uint8_t> out;
    out.reserve(token_count * cols * sizeof(float));
    for (int32_t &id: question_embedding_buf_)
    {
        const float *p = tmp_raw_fbuf.pointer_ + id * cols;
        const auto *b = reinterpret_cast<const uint8_t *>(p);
        out.insert(out.end(), b, b + cols * sizeof(float));
    }

    FloatBufferView embedded_raw_fbuf{out};
    FloatBufferView img_embedding_fbuf{img_embedding_buf_};

    // 2) 从 inputs_embeds 推断 D（嵌入维度）
    if (embedded_raw_fbuf.size_ % token_count != 0)
    {
        throw std::runtime_error(std::string{"embeddings raw buf length is not divisible by sequence length, "}
                                 + "embedded_raw_fbuf.size_: " + std::to_string(embedded_raw_fbuf.size_) + " "
                                 + "num_tokens: " + std::to_string(question_embedding_buf_.size()));
    }

    //  3) 将 image_embeds_flat 视作 [N_feat, D]
    const size_t D = embedded_raw_fbuf.size_ / token_count;
    if (img_embedding_fbuf.size_ % D != 0)
    {
        throw std::runtime_error("image embeds buf length is not divisible by embed dim D");
    }
    const size_t N_feat = img_embedding_fbuf.size_ / D;

    // 4) 统计当前序列中的图像 token 数量
    size_t n_image_tokens = 0;
    for (size_t i = 0; i < token_count; ++i)
    {
        if (question_embedding_buf_[i] == rows)
        {
            ++n_image_tokens;
        }
    }

    // 5) 图像 token 数量与特征条目数量的匹配 / 扩展（与原脚本相同）
    // 若不匹配：仅支持存在恰好 1 个占位 token，将其展开为 N_feat 个连续的 image_token_id，
    // 并对 inputs_embeds 进行 slice + concat，然后再进行替换。
    std::vector<float> output_bin_buf; // 最终的 inputs_embeds_np（展平）
    std::vector<int32_t> new_input_ids; // 若发生扩展，更新后的 input_ids

    if (n_image_tokens != N_feat)
    {
        // 约束：batch_size == 1 且恰好 1 个占位 token
        if (n_image_tokens != 1)
        {
            throw std::runtime_error("expected exactly 1 image token placeholder for expansion");
        }

        size_t pos = token_count; // 初始化为非法值
        for (size_t i = 0; i < token_count; ++i)
        {
            if (question_embedding_buf_[i] == rows)
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
                             question_embedding_buf_.begin(),
                             question_embedding_buf_.begin() + pos);
        for (size_t k = 0; k < N_feat; ++k)
        {
            new_input_ids.push_back(rows);
        }
        new_input_ids.insert(new_input_ids.end(),
                             question_embedding_buf_.begin() + pos + 1,
                             question_embedding_buf_.end());

        // slice + concat 拼接嵌入：
        // 左段 = inputs_embeds_flat[0 : pos*D]
        // 中段 = image_embeds_flat[0 : N_feat*D]
        // 右段 = inputs_embeds_flat[(pos+1)*D : S*D]
        const size_t left_count = pos * D;
        const size_t mid_count = N_feat * D;
        const size_t right_count = (token_count - pos - 1) * D;

        output_bin_buf.reserve(left_count + mid_count + right_count);
        output_bin_buf.insert(output_bin_buf.end(),
                              embedded_raw_fbuf.pointer_,
                              embedded_raw_fbuf.pointer_ + left_count);
        output_bin_buf.insert(output_bin_buf.end(),
                              img_embedding_fbuf.pointer_,
                              img_embedding_fbuf.pointer_ + mid_count);
        output_bin_buf.insert(output_bin_buf.end(),
                              embedded_raw_fbuf.pointer_ + (pos + 1) * D,
                              embedded_raw_fbuf.pointer_ + embedded_raw_fbuf.size_);

        // 再次替换：将 new_input_ids 中所有 image_token_id 对应的行，用 image_embeds 逐条覆盖
        // （与 Python 保持一致的“顺序映射”逻辑）
        {
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
                float *dst = &output_bin_buf[row * D];
                std::copy(src, src + D, dst);
            }
        }

        // 覆盖 input_ids
        //        question_embedding_buf_.assign(reinterpret_cast<int32_t *>(new_input_ids.data()),
        //                                       reinterpret_cast<int32_t *>(new_input_ids.data()) +
        //                                       (new_input_ids.size() * (sizeof(int64_t) / sizeof(int32_t))));
        question_embedding_buf_.swap(new_input_ids);
    }
    else
    {
        // 数量一致：直接替换（batch_size == 1）
        // 先复制原嵌入
        output_bin_buf.assign(embedded_raw_fbuf.pointer_, embedded_raw_fbuf.pointer_ + embedded_raw_fbuf.size_);
        size_t matched = 0;
        for (size_t i = 0; i < token_count; ++i)
        {
            if (question_embedding_buf_[i] == rows)
            {
                // 将第 matched 条图像特征写入到第 i 行
                const float *src = &img_embedding_fbuf.pointer_[matched * D];
                float *dst = &output_bin_buf[i * D];
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

    return output_bin_buf;
}
