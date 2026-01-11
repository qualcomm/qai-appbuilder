//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef MODEL_TYPE_H
#define MODEL_TYPE_H

#include <vector>
#include <string>

// TODO
struct BaseEnum
{
    constexpr BaseEnum &operator=(const BaseEnum &other) noexcept
    {
        v_ = other.v_;
        return *this;
    }

    constexpr BaseEnum &operator=(int v) noexcept
    {
        v_ = v;
        return *this;
    }

    constexpr bool operator==(BaseEnum other) const noexcept
    { return v_ == other.v_; }

    constexpr bool operator<(BaseEnum other) const noexcept
    { return v_ < other.v_; }

    int v_{};
};

const int CONTEXT_SIZE = 4096;

struct QueryType : public BaseEnum
{
    enum
    {
        Unknown,
        TextQuery,
        TokenQuery,
        EmbeddingQuery
    };

    /* @formatter:off */
    constexpr const char* to_string() noexcept {
        switch (v_) {
            case TextQuery: return "TextQuery";
            case TokenQuery: return "TokenQuery";
            case EmbeddingQuery: return "EmbeddingQuery";
            default: return "Unknown";
        }
    }
    /* @formatter:on */
};

struct PromptType : public BaseEnum
{
    enum
    {
        General,
        Harmony,
        Unknown
    };

    /* @formatter:off */
    constexpr const char* to_string() noexcept {
        switch (v_) {
            case General: return "General";
            case Harmony: return "Harmony";
            default: return "Unknown";
        }
    }
    /* @formatter:on */
};

struct ModelType : public BaseEnum
{
    enum
    {
        QNN,    // Qualcomm Neural Network SDK
        MNN,    // MNN format.
        GGUF,   // Llama.cpp GGUF format.
        Unknown,
    };

    /* @formatter:off */
    constexpr const char* to_string() noexcept {
        switch (v_) {
            case QNN: return "QNN";
            case MNN: return "MNN";
            case GGUF: return "GGUF";
            default: return "Unknown";
        }
    }
    /* @formatter:on */
};

struct QNNEmbeddingType : public BaseEnum
{
    enum
    {
        Unknown,
        PHI4MM,
        QWEN2_5,
    };

    /* @formatter:off */
    constexpr const char * to_string()const noexcept {
        switch (v_) {
            case PHI4MM: return "phi4";
            case QWEN2_5: return "qwen2.5";
            default: return "Unknown";
        }
    }
    /* @formatter:on */
};

struct QNNEmbeddingInfo
{
    QNNEmbeddingType type_{QNNEmbeddingType::Unknown};
    std::string init_bin_file_;
    std::vector<std::vector<uint8_t>> bin_stack_;
    void Clean()
    {
        type_.v_ = QNNEmbeddingType::Unknown;
        init_bin_file_.clear();
        bin_stack_.clear();
    }
};

#endif //MODEL_TYPE_H
