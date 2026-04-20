//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "qwen_2_5_omini.h"
#include <librosa/librosa.h>

#define DR_WAV_IMPLEMENTATION

#include <dr_wav.h>
#include <samplerate.h>
#include <log.h>
#include <utils.h>

#include <stb_image.h>
#include <stb_image_resize2.h>
#include "../qwen2_5/qwen25_image_processor.hpp"
#include "../../torch_helper/base.h"

std::vector<std::vector<float>>
QInterface::Qwen2_5OMINI::ProcessingMelSpec(std::vector<std::vector<float>> &&mel_spec)
{
    static const float min_val = 1e-10f;
//    clamp_and_log10_mel_major
    size_t n_mels = mel_spec.size();
    size_t n_frames = mel_spec[0].size();
    for (size_t m = 0; m < n_mels; ++m)
    {
        auto &row = mel_spec[m];
        for (size_t t = 0; t < n_frames; ++t)
        {
            float v = row[t];
            if (!std::isfinite(v) || v < min_val)
                v = min_val;
            row[t] = std::log10(v);
        }
    }

//    clamp_by_max_mel_major
    float gmax = -std::numeric_limits<float>::infinity();
    for (const auto &row: mel_spec)
    {
        for (float v: row)
        {
            if (std::isfinite(v) && v > gmax)
                gmax = v;
        }
    }

    static const float offset = 8.0f;
    float floor_val;
    if (!std::isfinite(gmax))
        goto ahead;
    floor_val = gmax - offset;
    for (auto &row: mel_spec)
        for (float &v: row)
            if (!std::isfinite(v) || v < floor_val)
                v = floor_val;

//    scale_shift_mel_major
    ahead:
    for (auto &row: mel_spec)
    {
        for (float &t: row)
        {
            t = (t + 4.0f) / 4.0f;
        }
    }
    return mel_spec;
}

std::vector<std::vector<float>>
QInterface::Qwen2_5OMINI::ApplyMaskTimeMajorToMelMajor(std::vector<std::vector<float>> &&time_major_mel_spec,
                                                       const std::vector<int32_t> &feature_attention_mask)
{
    size_t T = time_major_mel_spec.size();
    size_t M = time_major_mel_spec[0].size();
    if (feature_attention_mask.size() != T)
        throw std::invalid_argument("mask length != T");

    // count selected frames K
    size_t K = 0;
    for (size_t t = 0; t < T; ++t)
        if (feature_attention_mask[t])
            ++K;

    // prepare output [M][K]
    std::vector<std::vector<float>> input_features(M, std::vector<float>(K));
    if (K == 0)
        return input_features;

    size_t col = 0;
    for (size_t t = 0; t < T; ++t)
    {
        if (!feature_attention_mask[t])
            continue;
        const auto &row = time_major_mel_spec[t]; // row is length M
        for (size_t m = 0; m < M; ++m)
        {
            input_features[m][col] = row[m];
        }
        ++col;
    }
    return input_features;
}

std::vector<int64_t> QInterface::Qwen2_5OMINI::CreateChunkLengths(int64_t feature_len)
{
    constexpr int64_t CHUNK = 100 * 2;;
    int64_t chunk_num = (feature_len + CHUNK - 1) / CHUNK; // ceil division
    std::vector<int64_t> chunk_lengths(chunk_num, CHUNK);
    // adjust the last chunk to the actual remaining length
    int64_t last = feature_len - CHUNK * (chunk_num - 1);
    chunk_lengths.back() = last == 0 ? CHUNK : last;
    return chunk_lengths;
}

std::vector<std::vector<std::vector<float>>>
QInterface::Qwen2_5OMINI::SplitMelMajorByLengths(std::vector<std::vector<float>> &&input_features,
                                                 const std::vector<int64_t> &chunk_lengths)
{
    size_t M = input_features.size();
    size_t T = input_features[0].size();

    // sanity: sum lengths == T
    int64_t sum = 0;
    for (auto l: chunk_lengths)
        sum += l;
    if (static_cast<size_t>(sum) != T)
        throw std::runtime_error("chunk lengths do not sum to T");

    std::vector<std::vector<std::vector<float>>> chunks;
    chunks.reserve(chunk_lengths.size());

    size_t t0 = 0;
    for (auto len: chunk_lengths)
    {
        std::vector<std::vector<float>> block(M, std::vector<float>(len));
        for (size_t m = 0; m < M; ++m)
        {
            // copy features[m][t0 .. t0+len-1] -> block[m][0..len-1]
            for (size_t k = 0; k < static_cast<size_t>(len); ++k)
                block[m][k] = input_features[m][t0 + k];
        }
        chunks.push_back(std::move(block));
        t0 += len;
    }
    return chunks;
}

std::tuple<std::vector<float>, std::vector<float>>
QInterface::Qwen2_5OMINI::MakePaddedAndMask(const std::vector<std::vector<std::vector<float>>> &tensor_list,
                                            const std::vector<int64_t> &tensor_len)
{

    int dim = tensor_list[0].size();
    int max_len = tensor_list[0][0].size();
    size_t N = tensor_list.size();
    if (tensor_len.size() != N)
        throw std::runtime_error("tensor_len size mismatch");

    // 验证每个块内部一致性
    for (size_t i = 0; i < N; ++i)
    {
        if (tensor_list[i].size() != dim)
            throw std::runtime_error("dim mismatch in chunk");
        size_t len_i = tensor_list[i][0].size();
        for (size_t m = 1; m < dim; ++m)
            if (tensor_list[i][m].size() != len_i)
                throw std::runtime_error("inconsistent row length inside chunk");
        if (len_i != static_cast<size_t>(tensor_len[i]))
            throw std::runtime_error("tensor_len mismatch actual len");
        if (len_i > max_len)
            throw std::runtime_error("len_i > max_len");
    }

    // allocate and init
    std::vector<float> padded_flat;      // 输出: size N*dim*max_len
    std::vector<float> batch_mask_flat; // 输出: size N*max_len
    padded_flat.assign(N * dim * max_len, 0.0f);
    batch_mask_flat.assign(N * max_len, 0);

    auto padded_offset = [&](size_t i, size_t m, size_t k) -> size_t
    {
        // layout: (i, m, k) -> ((i * dim + m) * max_len + k)
        return (i * dim + m) * max_len + k;
    };
    auto mask_offset = [&](size_t i, size_t k) -> size_t
    {
        return i * max_len + k;
    };

    for (size_t i = 0; i < N; ++i)
    {
        size_t len_i = static_cast<size_t>(tensor_len[i]);
        // set mask 1 for first len_i positions
        for (size_t k = 0; k < len_i; ++k)
            batch_mask_flat[mask_offset(i, k)] = 1;

        // copy each mel row: tensor_list[i][m][0..len_i-1] -> padded_flat[(i,m,0..len_i-1)]
        for (size_t m = 0; m < dim; ++m)
        {
            const float *src = tensor_list[i][m].data();
            float *dst = padded_flat.data() + padded_offset(i, m, 0);
            std::memcpy(dst, src, len_i * sizeof(float));
            // 剩余位置已为 padding_value
        }
    }

    static int new_max_len = 10;
    return make_tuple(std::move(PaddedInputFeatures(std::move(padded_flat), N, dim, max_len)),
                      std::move(MakeUpdatedPaddedMaskFrom3D(
                              MakeMask3DFromFlat(std::move(batch_mask_flat), N, max_len), new_max_len)
                      ));
}

std::vector<float>
QInterface::Qwen2_5OMINI::PaddedInputFeatures(std::vector<float> &&padded_feature_flat,
                                              size_t N,
                                              size_t C,
                                              size_t T)
{
    static const size_t max_len_channels = 10;
    static const float padding_value = 0.0f;
    std::vector<float> padded_tensor_flat;
    padded_tensor_flat.assign(max_len_channels * C * T, padding_value);

    if (N > max_len_channels)
        throw std::runtime_error("N > max_len_channels; cannot fit");

    // copy padded_feature[:feature_len, :, :] -> out_padded_tensor[:feature_len, :, :]
    for (size_t i = 0; i < N; ++i)
    {
        for (size_t c = 0; c < C; ++c)
        {
            const float *src = padded_feature_flat.data() + idx3(i, c, 0, C, T);
            float *dst = padded_tensor_flat.data() + idx3(i, c, 0, C, T);
            std::memcpy(dst, src, T * sizeof(float));
        }
    }
    return padded_tensor_flat;
}

std::vector<std::vector<std::vector<float>>>
QInterface::Qwen2_5OMINI::MakeMask3DFromFlat(std::vector<float> &&batch_mask_flat, size_t N, size_t L)
{
    std::vector<std::vector<std::vector<float>>> mask3d;
    mask3d.resize(N);
    for (size_t i = 0; i < N; ++i)
    {
        mask3d[i].resize(1);
        mask3d[i][0].assign(batch_mask_flat.begin() + i * L,
                            batch_mask_flat.begin() + i * L + L);
    }
    return mask3d;
}

std::vector<float>
QInterface::Qwen2_5OMINI::MakeUpdatedPaddedMaskFrom3D(const std::vector<std::vector<std::vector<float>>> &padded_mask,
                                                      size_t max_len)
{
    size_t N = padded_mask.size();
    size_t B = padded_mask[0].size();
    size_t T = padded_mask[0][0].size();

    // allocate output (max_len * B * T), init 0.0f
    std::vector<float> out(max_len * B * T, 0.0f);

    // set first min(max_len, N) slices to 1.0f
    size_t upto = std::min(max_len, N);
    for (size_t i = 0; i < upto; ++i)
    {
        size_t base = (i * B + 0) * T;
        std::fill_n(out.data() + base, T, 1.0f);
    }
    return out;
}

std::vector<std::vector<int32_t>>
QInterface::Qwen2_5OMINI::MakeBatchMaskAfterCnn(const std::vector<int64_t> &tensor_len)
{
    size_t N = tensor_len.size();

    // 计算下采样后每个样本的长度： (L-1)//2 + 1
    std::vector<int64_t> lens_after;
    lens_after.reserve(N);
    for (auto L: tensor_len)
    {
        if (L <= 0)
            throw std::runtime_error("invalid length");
        int64_t newL = (L - 1) / 2 + 1;
        lens_after.push_back(newL);
    }

    // 找到最大长度，作为每行长度
    int64_t max_len = *std::max_element(lens_after.begin(), lens_after.end());
    if (max_len <= 0)
        throw std::runtime_error("computed max_len <= 0");

    // 分配二维 mask，初始化为 0
    std::vector<std::vector<int32_t>> mask;
    mask.assign(N, std::vector<int32_t>(static_cast<size_t>(max_len), 0));

    // 填充每行前 L 个为 1
    for (size_t i = 0; i < N; ++i)
    {
        int64_t L = lens_after[i];
        for (int64_t k = 0; k < L; ++k)
        {
            mask[i][static_cast<size_t>(k)] = 1;
        }
    }

    return mask;
}

std::vector<float>
QInterface::Qwen2_5OMINI::AttentionToPadded(std::vector<float> &&attention_mask,
                                            size_t H,
                                            std::vector<int32_t> &cu_seqlens,
                                            size_t P_H,
                                            size_t P_W)
{
    // zero_attention_blocks
    static const int batch = 1;
    if (attention_mask.size() != 1 * H * H)
        throw std::runtime_error("attention_mask size mismatch");

    if (cu_seqlens.size() < 2)
        throw std::runtime_error("cu_seqlens too short");

    // operate on the single plane n=0
    for (size_t t = 1; t < cu_seqlens.size(); ++t)
    {
        int32_t s = cu_seqlens[t - 1];
        int32_t e = cu_seqlens[t];

        if (s < 0 || e < s || static_cast<size_t>(e) > H)
        {
            My_Log{} << "cu_seqlens out of range: " << s << ", " << e << ", " << H << "\n";
            continue;
        }

        for (int32_t r = s; r < e; ++r)
        {
            for (int32_t c = s; c < e; ++c)
            {
                size_t pos = idx3(0, static_cast<size_t>(r), static_cast<size_t>(c), H, H);
                attention_mask[pos] = 0.0f;
            }
        }
    }

    std::vector<float> padded_attention_mask(P_H * P_W, -100.0f);
    if (padded_attention_mask.size() != batch * P_H * P_W)
        throw std::runtime_error("padded size mismatch");
    if (H > P_H || H > P_W)
        throw std::runtime_error("attention_mask larger than padded target");

    for (size_t b = 0; b < batch; ++b)
    {
        for (size_t i = 0; i < H; ++i)
        {
            for (size_t j = 0; j < H; ++j)
            {
                size_t src = idx3(0, i, j, H, H);
                size_t dst = idx3(b, i, j, P_H, P_W);
                padded_attention_mask[dst] = attention_mask[src];
            }
        }
    }
    return padded_attention_mask;
}

std::vector<int32_t> QInterface::Qwen2_5OMINI::ComputeCuSeqlensFromMask(const std::vector<std::vector<int32_t>> &mask)
{
    size_t N = mask.size();
    // compute per-row sums (use int64_t accumulator to avoid intermediate overflow)
    std::vector<int64_t> lens;
    lens.reserve(N);
    for (size_t i = 0; i < N; ++i)
    {
        const auto &row = mask[i];
        // optional: allow empty row -> sum 0
        int64_t s = 0;
        for (int k: row)
            s += static_cast<int64_t>(k);
        if (s < 0)
            throw std::runtime_error("negative sum in mask row");
        lens.push_back(s);
    }

    // compute prefix sums and build cu_seqlens (int32_t)
    std::vector<int32_t> cu;
    cu.resize(N + 1);
    cu[0] = 0;
    int64_t acc = 0;
    const auto INT32_MAX_LL = static_cast<int64_t>(std::numeric_limits<int32_t>::max());
    for (size_t i = 0; i < N; ++i)
    {
        acc += lens[i];
        if (acc > INT32_MAX_LL)
            throw std::runtime_error("cu_seqlens overflow int32");
        cu[i + 1] = static_cast<int32_t>(acc);
    }
    return cu;
}

IAudioEmbedding &QInterface::Qwen2_5OMINI::BuildAudioSamples()
{
    drwav_uint64 frames;
    uint32_t channels;
    uint32_t sampleRate;

    float *pcm_float = drwav_open_memory_and_read_pcm_frames_f32(
            audio_buf_.data(),
            audio_buf_.size(),
            &channels,
            &sampleRate,
            &frames,
            nullptr);

    if (!pcm_float)
    {
        throw std::runtime_error("open wav file buffer failed");
    }

    uint64_t pcm_len{frames * channels};

    // 1) Convert to mono by averaging channels
    float *mono;
    uint64_t mono_len{};
    if (channels == 1)
    {
        mono = pcm_float;
    }
    else
    {
        mono = new float[pcm_len];
        mono_len = pcm_len;

        for (uint64_t i = 0; i < frames; ++i)
        {
            float sum = 0.0f;
            const float *ptr = &pcm_float[i * channels];
            for (int c = 0; c < channels; ++c)
                sum += ptr[c];
            mono[i] = sum / static_cast<float>(channels);
        }
    }

    // 2) Prepare libsamplerate SRC_DATA
    const int target_sr = 16000;
    double src_ratio = static_cast<double>(target_sr) / static_cast<double>(sampleRate);
    uint64_t out_frames_est = std::ceil(frames * src_ratio);
    if (kPaddingMaxLength <= out_frames_est)
    {
        throw std::runtime_error(std::to_string(out_frames_est) + "  is bigger then kPaddingMaxLength");
    }
    audio_sample_buf_.resize(out_frames_est);
    SRC_DATA src_data;
    src_data.data_in = mono;
    src_data.input_frames = static_cast<long>(frames);
    src_data.data_out = audio_sample_buf_.data();
    src_data.output_frames = static_cast<long>(out_frames_est);
    src_data.src_ratio = src_ratio;
    src_data.end_of_input = 1;

    int err = src_simple(&src_data, SRC_SINC_BEST_QUALITY, 1); // 1 channel (mono)
    auto clean{[&mono_len, &pcm_float, &mono]()
               {
                   if (mono_len)
                   {
                       delete[] mono;
                   }
                   drwav_free(pcm_float, nullptr);
               }};
    if (err != 0)
    {
        clean();
        throw std::runtime_error(std::string("libsamplerate error: ") + src_strerror(err));
    }

    uint32_t rescaled_length = (out_frames_est - 1) / 160 + 1;
    uint32_t input_length = (rescaled_length - 1) / 2 + 1;
    IAudioEmbedding::token_index_ = (input_length - 2) / 2 + 1;
    clean();
    return *this;
}

IAudioEmbedding &QInterface::Qwen2_5OMINI::BuildAudioInferredInput()
{
    static const int hop = 160;
    static const int mel = 128;
    static constexpr int64_t CHUNK = 100 * 2;

    int audio_sample_size = audio_sample_buf_.size();
    audio_sample_buf_.resize(kPaddingMaxLength);
    for (auto i = audio_sample_size - 1; i < audio_sample_buf_.size(); ++i)
    {
        audio_sample_buf_[i] = 0;
    }

    std::vector<int32_t> feature_attention_mask;
    feature_attention_mask.reserve((kPaddingMaxLength / hop));
    for (auto i = 0; i < audio_sample_buf_.size();)
    {
        if (i < audio_sample_size)
        {
            feature_attention_mask.push_back(1);
        }
        else
        {
            feature_attention_mask.push_back(0);
        }
        i += hop;
    }

    // _torch_extract_fbank_features
    std::vector<std::vector<float>> mel_spec = librosa::Feature::melspectrogram(audio_sample_buf_,
                                                                                16000,
                                                                                400,
                                                                                hop,
                                                                                "hann",
                                                                                true,
                                                                                "reflect",
                                                                                2.0f,
                                                                                mel,
                                                                                0,
                                                                                8000);
    if (mel_spec.empty())
    {
        throw std::runtime_error("librosa error: melspectrogram failed");
    }
    mel_spec.pop_back();

    mel_spec = ProcessingMelSpec(std::move(mel_spec));
    auto input_features = ApplyMaskTimeMajorToMelMajor(std::move(mel_spec), feature_attention_mask);
    auto tensor_len = CreateChunkLengths(input_features[0].size());

    size_t half = input_features[0].size() / 2;
    auto tensor_list = SplitMelMajorByLengths(std::move(input_features), tensor_len);
    std::tie(padded_feature_, padded_mask_) = MakePaddedAndMask(tensor_list, tensor_len);

    std::vector<std::vector<int32_t>> padded_mask_after_cnn = MakeBatchMaskAfterCnn(tensor_len);
    std::vector<float> attention_mask(1 * half * half, -100.0f);

    static const int max_attention_mask_dim = 1000;

    std::vector<int32_t> cu_seqlens = ComputeCuSeqlensFromMask(padded_mask_after_cnn);
    padded_attention_mask_ = AttentionToPadded(std::move(attention_mask), half, cu_seqlens);
    IAudioEmbedding::input_buffers_[0][0] = reinterpret_cast<uint8_t *>(padded_feature_.data());
    IAudioEmbedding::input_buffers_[0][1] = reinterpret_cast<uint8_t *>(padded_mask_.data());
    IAudioEmbedding::input_buffers_[0][2] = reinterpret_cast<uint8_t *>(padded_attention_mask_.data());
    return *this;
}

IVisionEmbedding &QInterface::Qwen2_5OMINI::BuildImgPixel()
{
    using namespace qwen2_5;
    int rows = 0, cols = 0;
    Qwen25ImageProcessor proc;
    proc.ProcessToBuffer(img_buf_.data(), img_buf_.size(), kHeight, kWidth, img_pixel_buf_, rows, cols);
    img_buf_.clear();
    File::WriteBinaryFile<float>(img_pixel_buf_, "img_pixel.raw");
    return *this;
}

void QInterface::Qwen2_5OMINI::MaskedScatter(const std::vector<uint8_t> &mask,
                                             const FloatBufferView &features)
{
    if (mask.size() != embedded_bin_.size())
        throw std::runtime_error("mask and embedded_bin must have same flattened length");

    size_t total_elems = mask.size();
    if (total_elems % cols_ != 0)
        throw std::runtime_error("mask length not divisible by cols");
    size_t seq_rows = total_elems / cols_;

    size_t ones_total = 0;
    bool rowwise = true;
    for (size_t r = 0; r < seq_rows; ++r)
    {
        size_t base = r * cols_;
        bool all0 = true, all1 = true;
        for (size_t c = 0; c < cols_; ++c)
        {
            uint8_t v = mask[base + c];
            if (v)
                all0 = false;
            else
                all1 = false;
            if (v)
                ++ones_total;
            if (!all0 && !all1)
            {
                rowwise = false;
                break;
            }
        }
        if (!rowwise)
            break;
    }

    if (rowwise && (ones_total % cols_ == 0) && (features.size_ == ones_total))
    {
        // fast row-wise copy
        size_t feat_row = 0;
        for (size_t r = 0; r < seq_rows; ++r)
        {
            size_t base = r * cols_;
            if (!mask[base])
                continue;
            std::memcpy(embedded_bin_.data() + base,
                        features.pointer_ + feat_row * cols_,
                        cols_ * sizeof(float));
            ++feat_row;
        }
        return;
    }

    // element-wise path: require audio_features.size() == number of ones
    if (features.size_ != ones_total)
    {
        My_Log{} << "audio/image features length must equal number of ones in mask "
                 << features.size_ << ", " << ones_total << "\n";
    }

    size_t vi = 0;
    for (size_t i = 0; i < total_elems; ++i)
    {
        if (mask[i])
            embedded_bin_[i] = features.pointer_[vi++];
    }
}

void QInterface::Qwen2_5OMINI::MergeImpl(int token_index,
                                         int standard_index,
                                         std::vector<uint8_t> &inferred_buf)
{
    if (embedded_bin_.size() / cols_ < token_index)
        throw std::runtime_error("qnn audio/image outputs size is smaller than it token count");

    inferred_buf.resize(token_index * cols_ * sizeof(float));

    /* @formatter:off */
    std::vector<uint8_t> mask(prompt_token_size_ * cols_, 0);
    /* @formatter:on */

    int count{};
    for (auto i = 0; i < prompt_token_size_; ++i)
    {
        if (prompt_token_[i] == standard_index)
        {
            ++count;
            size_t base = i * cols_;
            std::fill_n(mask.data() + base, cols_, static_cast<uint8_t>(1));
        }
    }

    MaskedScatter(mask, FloatBufferView{inferred_buf});
}

IEmbedding &QInterface::Qwen2_5OMINI::MergeEmbedding()
{
    static const int32_t rows{151936};
    static const uint32_t kAudioTokenIndex{151646};
    static const int32_t kVisionTokenIndex{151655};

    FloatBufferView tmp_raw_fbuf{qnn_embedding_info_.embedded_raw_buf_};

    embedded_bin_.resize(prompt_token_size_ * cols_);
    float *dest_ptr;
    for (uint32_t i = 0; i < prompt_token_size_; ++i)
    {
        dest_ptr = &embedded_bin_[i * cols_];
        float *src_ptr = &tmp_raw_fbuf.pointer_[prompt_token_[i] * cols_];
        std::memcpy(dest_ptr, src_ptr, cols_ * sizeof(float));
    }

    if (!audio_inferred_buf_.empty())
    {
        MergeImpl(IAudioEmbedding::token_index_, kAudioTokenIndex, audio_inferred_buf_[0]);
    }

    if (!img_inferred_buffers_.empty())
    {
        MergeImpl(IVisionEmbedding::token_index_, kVisionTokenIndex, img_inferred_buffers_[0]);
    }

    return *this;
}
