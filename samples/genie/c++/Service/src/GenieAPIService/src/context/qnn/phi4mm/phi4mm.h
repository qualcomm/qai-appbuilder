//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================


#ifndef PHI_4_EMBEDDING_H
#define PHI_4_EMBEDDING_H

#include "../genie_interface.h"

class QInterface::PHI4Embedding : public IVisionEmbedding
{
public:
    explicit PHI4Embedding(GenieContext *context) : IVisionEmbedding(context), IEmbedding(context)
    {
        const char *kRawPromptTemplate{"<|system|> You are a helpful assistant. <|end|>"
                                    "<|user|>%s %s <|end|><|assistant|>"};

        std::string endoftext10_list;
        int len = 494 * strlen("<|endoftext10|>");
        endoftext10_list.reserve(len + 1);
        for (int i = 0; i < 494; ++i)
        {
            endoftext10_list.append("<|endoftext10|>");
        }

        kPromptTemplate.resize(strlen(kRawPromptTemplate) + endoftext10_list.size());
        sprintf(kPromptTemplate.data(), kRawPromptTemplate, endoftext10_list.c_str(), "%s");
        kWidth = 448;
        kHeight = 364;
    }

    IVisionEmbedding &BuildImgPixel() final;

    IVisionEmbedding &MergeEmbedding() final;
};

#endif //PHI_4_EMBEDDING_H
