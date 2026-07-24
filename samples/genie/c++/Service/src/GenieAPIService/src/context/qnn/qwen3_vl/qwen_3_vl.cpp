//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include <algorithm>

#include <stb_image.h>
#include <stb_image_resize2.h>

#include "qwen_3_vl.h"
#include "qwen3_vl_image_processor.hpp"
#include "../../torch_helper/masked_scatter.h"
#include <log.h>

IVisionEmbedding &QInterface::Qwen3VL::BuildImgPixel()
{
    using namespace qwen3_vl;
    int rows = 0, cols = 0;
    Qwen3VLImageProcessor proc;
    proc.ProcessToBuffer(img_buf_.data(), img_buf_.size(), kHeight, kWidth, img_pixel_buf_, rows, cols);
    img_buf_.clear();
    return *this;
}

IEmbedding &QInterface::Qwen3VL::CustomBuild(ModelInput &model_input)
{
    // 只做解码+图像预处理+prompt占位符拼接；BuildVisionInferredInput()/
    // BuildInferredBuffer() 推迟到 MergeEmbedding()，见 qwen_3_vl.h 注释。
    dynamic_cast<IVisionEmbedding &>(Decode(model_input.image_, img_buf_))
            .BuildImgPixel()
            .PaddingVisionPrompt();
    return *this;
}

IVisionEmbedding &QInterface::Qwen3VL::BuildVisionInferredInput()
{
    input_buffers_[0][0] = reinterpret_cast<uint8_t *>(img_pixel_buf_.data());

    // mask：标记 prompt_token_ 中每个位置是否为图像占位符 token，长度取
    // kContextSize_（对齐 vision_encoder.bin 导出时使用的静态 mask 形状，
    // 参考 _shared/lm_driver/qwen3_vl.py:157 get_sample_vision_inputs()
    // 里 torch.zeros((1, 2048), dtype=torch.bool)；具体 dtype/长度未经远程
    // 环境验证，见 qwen3_vl.md 风险记录）。
    static const int32_t image_token_id{151655};
    mask_buf_.assign(kContextSize_, 0);
    const uint32_t fill_count = std::min<uint32_t>(prompt_token_size_, static_cast<uint32_t>(kContextSize_));
    for (uint32_t i = 0; i < fill_count; ++i)
    {
        if (prompt_token_[i] == image_token_id)
        {
            mask_buf_[i] = 1;
        }
    }
    if (prompt_token_size_ > static_cast<uint32_t>(kContextSize_))
    {
        My_Log("[Qwen3VL DIAG] mask truncated: token_count=" + std::to_string(prompt_token_size_)
               + " > kContextSize_=" + std::to_string(kContextSize_), My_Log::Level::kWarning);
    }

    // MergeEmbedding() 可能在同一个 Qwen3VL 实例上跨多个请求被反复调用，
    // input_buffers_[0] 在构造函数里只按 bin_stacks_ 数量分配一次；这里必须
    // 按下标覆写而非 push_back，否则第二次图像请求会重复追加 mask 槽位。
    const size_t mask_slot = infer_resource_->bin_stacks_.size() + 1;
    if (input_buffers_[0].size() <= mask_slot)
    {
        input_buffers_[0].resize(mask_slot + 1);
    }
    input_buffers_[0][mask_slot] = mask_buf_.data();
    return *this;
}

IVisionEmbedding &QInterface::Qwen3VL::MergeEmbedding()
{
    static const int32_t image_token_id{151655};
    const unsigned long token_count = prompt_token_size_;
    BufferView<float> tmp_raw_fbuf{qnn_embedding_info_.embedded_raw_buf_};

    std::vector<float> embedded_raw_fbuf;
    embedded_raw_fbuf.resize(token_count * cols_);
    float *dest_ptr;
    for (uint32_t i = 0; i < prompt_token_size_; ++i)
    {
        dest_ptr = &embedded_raw_fbuf[i * cols_];
        float *src_ptr = &tmp_raw_fbuf.pointer_[prompt_token_[i] * cols_];
        std::memcpy(dest_ptr, src_ptr, cols_ * sizeof(float));
    }

    if (img_pixel_buf_.empty())
    {
        embedded_bin_ = std::move(embedded_raw_fbuf);
        input_data_ = reinterpret_cast<uint8_t*>(embedded_bin_.data());
        input_len_ = embedded_bin_.size() * sizeof(float);
        My_Log("[Qwen3VL DIAG] text-only embedding built: token_count=" + std::to_string(token_count)
               + " cols_=" + std::to_string(cols_)
               + " embedded_bin_.size()=" + std::to_string(embedded_bin_.size())
               + " input_len_=" + std::to_string(input_len_)
               + " sample[0..3]=" + std::to_string(embedded_bin_[0]) + "," + std::to_string(embedded_bin_[1])
               + "," + std::to_string(embedded_bin_[2]) + "," + std::to_string(embedded_bin_[3]),
               My_Log::Level::kWarning);
        return *this;
    }

    // prompt_token_ 此时已由 BuildTextEmbedding() 填好，才能正确构造 mask，
    // 因此视觉编码器的实际推理调用推迟到这里执行（见 CustomBuild() 注释）。
    BuildVisionInferredInput();
    BuildInferredBuffer(infer_resource_, input_buffers_, img_inferred_buffers_, deepstack_buffers_);

    BufferView<float> img_embedding_fbuf{img_inferred_buffers_[0]};
    torch_helper::MaskedScatterMergeEmbedding(prompt_token_, token_count, image_token_id,
                                              embedded_raw_fbuf, img_embedding_fbuf, embedded_bin_);

    // deepstack 注入：经查阅公开的 GenieDialog.h（QAIRT SDK 80-63442-10，
    // https://docs.qualcomm.com/doc/80-63442-10/topic/api-rst_program_listing_file_include_Genie_GenieDialog_h.html）
    // 确认 GenieDialog_embeddingQuery 等全部 Dialog 查询 API 都只接受单个扁平
    // embeddings 缓冲区，没有任何具名附加张量（如 visual_pos_masks /
    // deepstack_visual_embeds_i）的传入通道。因此 deepstack_buffers_（已在
    // BuildInferredBuffer 中随主输出一起捕获，见其诊断日志核实真实输出个数）
    // 目前无法真正注入文本生成器，安全降级为仅使用上面的 masked_scatter 结果，
    // 与降级前行为完全一致。详见 qwen3_vl.md 风险记录。
    if (!deepstack_buffers_.empty())
    {
        My_Log("[Qwen3VL DIAG] deepstack features captured (" + std::to_string(deepstack_buffers_.size())
               + " extra buffer(s)) but not injected: GenieDialog_embeddingQuery has no named "
               + "auxiliary-tensor input channel; falling back to masked_scatter-only embedding.",
               My_Log::Level::kWarning);
    }

    input_data_ = reinterpret_cast<uint8_t*>(embedded_bin_.data());
    input_len_ = embedded_bin_.size() * sizeof(float);
    return *this;
}
