//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef MODEL_TYPE_H
#define MODEL_TYPE_H

#include "base_enum.h"
#if defined(__linux__) && !defined(__ANDROID__)
    // gcc on Linux does not transitively include <cstdint> via the headers
    // below the way MSVC and the Android NDK do.
    #include <cstdint>
#endif
#include <vector>
#include <string>
#include <unordered_map>

const int DEFAULT_CONTEXT_SIZE = 4096;

struct ModelInput
{
    std::string system_;
    std::string text_;
    std::string image_;
    std::string audio_;
};

struct PromptType : public BaseEnum
{
    using BaseEnum::operator=;
    enum
    {
        General,
        Harmony,
        Unknown
    };

    constexpr const char *to_string() noexcept
    {
        switch (v_)
        {
            case General:
                return "General";
            case Harmony:
                return "Harmony";
            default:
                return "Unknown";
        }
    }
};

struct ModelType : public BaseEnum
{
    using BaseEnum::operator=;
    enum
    {
        Unknown,
        Text = 1,
        Vision = 1 << 1,
        Audio = 1 << 2,
    };

    std::string to_string() const
    {
        if (v_ == Unknown)
            return "[Unknown]";

        std::string str{"["};
        if (v_ & Text)
        {
            str += "Text ";
        }

        if (v_ & Vision)
        {
            str += "Vision ";
        }
        if (v_ & Audio)
        {
            str += "Audio ";
        }
        str.append("]");
        return str;
    }
};

namespace std
{
    template<>
    struct std::hash<ModelType>
    {
        size_t operator()(ModelType const &m) const noexcept { return std::hash<int>{}(int(m)); }
    };
}

struct ModelFormat : public BaseEnum
{
    enum
    {
        QNN,
        MNN,
        GGUF,
        Unknown,
    };

    constexpr const char *to_string() noexcept
    {
        switch (v_)
        {
            case QNN:
                return "QNN";
            case MNN:
                return "MNN";
            case GGUF:
                return "GGUF";
            default:
                return "Unknown";
        }
    }
};

struct EmbeddingDataType : BaseEnum
{
    using BaseEnum::operator=;
    enum
    {
        None,
        INT8,
        FLOAT32,
    };

    constexpr const char *to_string() noexcept
    {
        switch (v_)
        {
            case INT8:
                return "INT8";
            case FLOAT32:
                return "FLOAT32";
            default:
                return "Unknown";
        }
    }
};

struct QNNEmbeddingType : public BaseEnum
{
    using BaseEnum::operator=;
    enum
    {
        None,
        PHI4MM,
        QWEN2_5,
        QWEN2_5_OMINI,
    };

    constexpr const char *to_string() const noexcept
    {
        switch (v_)
        {
            case PHI4MM:
                return "phi4";
            case QWEN2_5:
                return "qwen2.5";
            case QWEN2_5_OMINI:
                return "qwen2.5-omini";
            default:
                return "none";
        }
    }
};

class LibAppBuilder;

struct QNNEmbedding
{
    ModelType model_types_{};
    EmbeddingDataType data_type{};
    QNNEmbeddingType embedding_type_{};
    std::vector<uint8_t> embedded_raw_buf_;
    struct InferResource
    {
        LibAppBuilder *app_builder_;
        std::string tag_;
        std::vector<std::vector<uint8_t>> bin_stacks_;
        std::vector<std::vector<uint8_t>> tails_bin_stacks_;
    };

    std::unordered_map<ModelType, InferResource> infer_resources_;

    static LibAppBuilder *LibAppbuilderCreator(const std::string &serialized_file, const std::string &tag);

    void Clean();
};

#endif //MODEL_TYPE_H
