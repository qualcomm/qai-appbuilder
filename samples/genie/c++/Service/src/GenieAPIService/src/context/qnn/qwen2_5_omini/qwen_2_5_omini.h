//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef QWEN_2_5_OMINI_H
#define QWEN_2_5_OMINI_H

#include "../genie_interface.h"

class QInterface::Qwen2_5OMINI : public IMultiModal
{
public:
    explicit Qwen2_5OMINI(GenieContext *context) : IMultiModal(context),
                                                   IAudioEmbedding(context),
                                                   IEmbedding(context)
    {
        IAudioEmbedding::kPromptTemplate = "<|im_start|>system\n"
                                           "You are Qwen, a virtual human developed by the Qwen Team, Alibaba Group, capable of perceiving auditory and visual inputs, as well as generating text and speech.<|im_end|>\n"
                                           "<|im_start|>user\n"
                                           "%s<|audio_bos|><|AUDIO|><|audio_eos|><|im_end|>\n"
                                           "<|im_start|>assistant\n";

    }

    IAudioEmbedding &BuildAudioSamples() override;

    IEmbedding & MergeEmbedding() override;

    IVisionEmbedding &BuildImgPixel() override {return *this;}
};

#endif //QWEN_2_5_OMINI_H
