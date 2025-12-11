//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef MODEL_TYPE_H
#define MODEL_TYPE_H

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

struct QueryType : public BaseEnum
{
    enum
    {
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

struct ModelStyle : public BaseEnum
{
    enum
    {
        General,
        Harmony,
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
        UNKNOW,
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

#endif //MODEL_TYPE_H
