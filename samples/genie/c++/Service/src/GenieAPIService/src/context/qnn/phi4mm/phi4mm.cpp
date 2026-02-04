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
#include "phi4mm_image_processor.hpp"

IVisionEmbedding &QInterface::PHI4Embedding::BuildImgPixel()
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

IVisionEmbedding & QInterface::PHI4Embedding::MergeEmbedding()
{
    static const int HIDDEN_DIM = 3072;
    static const int SPECIAL_TOKEN_ID = 200010;

    size_t num_tokens = prompt_token_size_;
    embedded_bin_.resize(num_tokens * HIDDEN_DIM);

    size_t vision_idx = 0;
    FloatBufferView embedded_raw_fbuf{qnn_embedding_info_.embedded_raw_buf_};
    FloatBufferView img_embedding_fbuf{img_inferred_buf_};
    for (size_t i = 0; i < num_tokens; ++i)
    {
        int32_t token_id = prompt_token_[i];
        float *dest_ptr = &embedded_bin_[i * HIDDEN_DIM];

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

