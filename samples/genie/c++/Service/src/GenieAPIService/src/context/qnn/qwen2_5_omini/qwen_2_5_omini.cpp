//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================


#include "qwen_2_5_omini.h"

#define DR_WAV_IMPLEMENTATION

// #include <dr_wav.h>
// #include <samplerate.h>

IAudioEmbedding &QInterface::Qwen2_5OMINI::BuildAudioSamples()
{
    throw std::runtime_error("not impl yet");
    // uint64_t frames;
    // uint32_t channels;
    // uint32_t sampleRate;

    // float *pcm_float = drwav_open_memory_and_read_pcm_frames_f32(
    //         audio_buf_.data(),
    //         audio_buf_.size(),
    //         &channels,
    //         &sampleRate,
    //         &frames,
    //         nullptr);

    // if (!pcm_float)
    // {
    //     throw std::runtime_error("open wav file buffer failed");
    // }

    // uint64_t pcm_len{frames * channels};

    // // 1) Convert to mono by averaging channels
    // float *mono;
    // uint64_t mono_len{};
    // if (channels == 1)
    // {
    //     mono = pcm_float;
    // }
    // else
    // {
    //     mono = new float[pcm_len];
    //     mono_len = pcm_len;

    //     for (uint64_t i = 0; i < frames; ++i)
    //     {
    //         float sum = 0.0f;
    //         const float *ptr = &pcm_float[i * channels];
    //         for (int c = 0; c < channels; ++c)
    //             sum += ptr[c];
    //         mono[i] = sum / static_cast<float>(channels);
    //     }
    // }

    // // 2) Prepare libsamplerate SRC_DATA
    // const int target_sr = 16000;
    // double src_ratio = static_cast<double>(target_sr) / static_cast<double>(sampleRate);
    // uint64_t out_frames_est = static_cast<uint64_t>(std::ceil(frames * src_ratio));
    // audio_sample_buf_.resize(out_frames_est);
    // SRC_DATA src_data;
    // src_data.data_in = mono;
    // src_data.input_frames = static_cast<long>(frames);
    // src_data.data_out = audio_sample_buf_.data();
    // src_data.output_frames = static_cast<long>(out_frames_est);
    // src_data.src_ratio = src_ratio;
    // src_data.end_of_input = 1;

    // int err = src_simple(&src_data, SRC_SINC_BEST_QUALITY, 1); // 1 channel (mono)
    // auto clean{[&mono_len, &pcm_float, &mono]()
    //            {
    //                if (mono_len)
    //                {
    //                    delete[] mono;
    //                }
    //                drwav_free(pcm_float, nullptr);
    //            }};
    // if (err != 0)
    // {
    //     clean();
    //     throw std::runtime_error(std::string("libsamplerate error: ") + src_strerror(err));
    // }

    // clean();
    return *this;
}

IEmbedding &QInterface::Qwen2_5OMINI::MergeEmbedding()
{
    throw std::runtime_error("not impl yet");
}


