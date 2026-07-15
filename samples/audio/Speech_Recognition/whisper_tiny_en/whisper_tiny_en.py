# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import sys
import os
sys.path.append(".")
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..", "common"))

import install
from _speech_recognition import (
    log_mel_spectrogram,
    apply_timestamp_rules,
    download_whisper_assets,
    download_whisper_models,
    get_whisper_tokenizer,
    CollectionModel,
    Whisper,
    WhisperEncoderInf,
    WhisperDecoderInf,
    CHUNK_LENGTH,
    HOP_LENGTH,
    N_FFT,
    N_MELS,
    SAMPLE_RATE,
    TOKEN_SOT,
    TOKEN_EOT,
    TOKEN_BLANK,
    TOKEN_NO_TIMESTAMP,
    TOKEN_TIMESTAMP_BEGIN,
    TOKEN_NO_SPEECH,
    NO_SPEECH_THR,
    NON_SPEECH_TOKENS,
    SAMPLE_BEGIN,
)

import numpy as np
import time
import argparse
import audio2numpy as a2n

from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig, DataType)

####################################################################

MODEL_NAME = "whisper_tiny_en"
ENCODER_MODEL_NAME = "whisper_tiny_en-whisperencoder-snapdragon_x_elite"
DECODER_MODEL_NAME = "whisper_tiny_en-whisperdecoder-snapdragon_x_elite"
MODEL_HELP_URL = "https://github.com/qualcomm/qai-appbuilder/blob/main/samples/audio/Speech_Recognition/whisper_tiny_en/README.md"
WHISPER_VERSION = "tiny.en"

####################################################################

SOC_ID = None
cleaned_argv = []
i = 0
while i < len(sys.argv):
    if sys.argv[i] == '--chipset':
        SOC_ID = sys.argv[i + 1]
        i += 2
    else:
        cleaned_argv.append(sys.argv[i])
        i += 1

sys.argv = cleaned_argv

print(f"SOC_ID: {SOC_ID}")

# Resolve execution workspace to audio/whisper_tiny_en directory
_script_dir = os.path.dirname(os.path.abspath(__file__))
execution_ws = _script_dir

model_dir = os.path.join(execution_ws, "models")
encoder_model_path = os.path.join(model_dir, ENCODER_MODEL_NAME + ".bin")
decoder_model_path = os.path.join(model_dir, DECODER_MODEL_NAME + ".bin")

jfk_wav_path = os.path.join(execution_ws, "jfk.wav")
jfk_npz_path = os.path.join(execution_ws, "jfk.npz")
mel_filter_path = os.path.join(execution_ws, "mel_filters.npz")

####################################################################

SAMPLE_RATE = 16000
MAX_AUDIO_SAMPLES = CHUNK_LENGTH * SAMPLE_RATE

####################################################################
encoder = None
decoder = None
whisper_tiny_en = None
mel_filter = None


# Encoder/Decoder class which inherited from the class QNNContext.
class Encoder(QNNContext):
    def Inference(self, input_data):
        input_datas = [input_data]
        output_data = super().Inference(input_datas)
        k_cache_cross = output_data[0]
        k_cache_cross = k_cache_cross.reshape(4, 6, 64, 1500)
        v_cache_cross = output_data[1]
        v_cache_cross = v_cache_cross.reshape(4, 6, 1500, 64)
        return k_cache_cross, v_cache_cross


class Decoder(QNNContext):
    def Inference(self, x, index, k_cache_cross, v_cache_cross, k_cache_self, v_cache_self):
        input_datas = [x, index, k_cache_cross, v_cache_cross, k_cache_self, v_cache_self]
        output_data = super().Inference(input_datas)
        logits = output_data[0]
        logits = logits.reshape(1, 1, 51864)
        k_cache = output_data[1]
        k_cache = k_cache.reshape(4, 6, 64, 224)
        v_cache = output_data[2]
        v_cache = v_cache.reshape(4, 6, 224, 64)
        return logits, k_cache, v_cache


@CollectionModel.add_component(WhisperEncoderInf)
@CollectionModel.add_component(WhisperDecoderInf)
class WhisperTinyEn(Whisper):
    @classmethod
    def from_pretrained(cls):
        return super().from_pretrained(WHISPER_VERSION)


def model_download():
    # Download shared Whisper assets (mel filters, sample audio)
    download_whisper_assets(mel_filter_path, jfk_wav_path, jfk_npz_path)

    # Download QNN model binaries via download_qai_hubmodel
    ret = download_whisper_models(
        soc_id=SOC_ID,
        encoder_model_name=ENCODER_MODEL_NAME,
        decoder_model_name=DECODER_MODEL_NAME,
        encoder_model_path=encoder_model_path,
        decoder_model_path=decoder_model_path,
        model_name=MODEL_NAME,
        model_help_url=MODEL_HELP_URL,
    )

    if not ret:
        exit()


def Init():
    global encoder, decoder, whisper_tiny_en, mel_filter

    model_download()

    with np.load(mel_filter_path) as f:
        mel_filter = f[f"mel_{N_MELS}"]

    whisper_tiny_en = WhisperTinyEn.from_pretrained()

    # Config AppBuilder environment.
    QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for Decoder
    decoder = Decoder("whisper_decoder", decoder_model_path, input_data_type=DataType.NATIVE, output_data_type=DataType.NATIVE)

    # Instance for Encoder
    encoder = Encoder("whisper_encoder", encoder_model_path, input_data_type=DataType.NATIVE, output_data_type=DataType.NATIVE)


def Inference(audio_path):
    # Read and preprocess the audio.
    audio, audio_sample_rate = a2n.audio_from_file(audio_path)

    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    result = " ".join(
        transcribe_single_chunk(x)
        for x in chunk_and_resample_audio(audio, audio_sample_rate)
    )

    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()

    # show the generated text
    print("Transcription:", result)


def transcribe_single_chunk(audio: np.ndarray):
    # Preprocess: compute log mel spectrogram using shared utility
    mel_input = log_mel_spectrogram(
        mel_filter, audio, MAX_AUDIO_SAMPLES, N_FFT, HOP_LENGTH
    )

    k_cache_cross, v_cache_cross = encoder.Inference(mel_input)

    print("Decoder Inference k_cache_cross type", type(k_cache_cross), "shape ", k_cache_cross.shape, "type ", k_cache_cross.dtype)
    print("Decoder Inference v_cache_cross type", type(v_cache_cross), "shape ", v_cache_cross.shape, "type ", v_cache_cross.dtype)

    # Start decoding
    # Whisper requires prefix tokens before autoregressive decoding:
    #   step 0: input=TOKEN_SOT        → feeds start-of-transcript token
    #   step 1: input=TOKEN_NO_TIMESTAMP → tells model to skip timestamps
    # decoded_tokens tracks all tokens including prefix for apply_timestamp_rules.
    decoded_tokens = [TOKEN_SOT]
    sample_len = whisper_tiny_en.mean_decode_len  # mean # of tokens to sample

    logits = np.zeros((1, 1, 51864,)).astype(np.float32)
    k_cache_self = np.zeros((4, 6, 64, 224,)).astype(np.float16)
    v_cache_self = np.zeros((4, 6, 224, 64,)).astype(np.float16)

    # Feed prefix tokens first
    prefix_tokens = [TOKEN_SOT, TOKEN_NO_TIMESTAMP]
    for pi, prefix_tok in enumerate(prefix_tokens):
        x = np.array([[prefix_tok]], dtype=np.int32)
        index = np.array([[pi]], dtype=np.int32)
        logits, k_cache_self, v_cache_self = decoder.Inference(
            x, index, k_cache_cross, v_cache_cross, k_cache_self, v_cache_self
        )
        decoded_tokens.append(prefix_tok)

    sum_logprobs = 0
    print("start decode sample_len ", sample_len)
    for i in range(sample_len):
        # index continues after prefix tokens
        index = np.array([[len(prefix_tokens) + i]], dtype=np.int32)
        x = np.array([[decoded_tokens[-1]]], dtype=np.int32)

        logits, k_cache_self, v_cache_self = decoder.Inference(
            x, index, k_cache_cross, v_cache_cross, k_cache_self, v_cache_self
        )

        # logit has shape (51864,)
        logits = logits[0, -1]  # consider only the last token

        # Filters
        # SuppressBlank
        if i == 0:
            logits[[TOKEN_EOT, TOKEN_BLANK]] = -np.inf
        # SuppressTokens
        logits[NON_SPEECH_TOKENS] = -np.inf

        # Postprocess: apply timestamp rules using shared utility
        logits, logprobs = apply_timestamp_rules(logits, decoded_tokens)
        assert isinstance(logprobs, np.ndarray)

        if i == 0:
            # detect no_speech
            no_speech_prob = np.exp(logprobs[TOKEN_NO_SPEECH])
            if no_speech_prob > NO_SPEECH_THR:
                break

        # temperature = 0
        next_token = np.argmax(logits)
        if next_token == TOKEN_EOT:
            break

        sum_logprobs += logprobs[next_token]
        decoded_tokens.append(int(next_token))

    tokenizer = get_whisper_tokenizer(
        multilingual=False, language="en", task="transcribe"
    )

    # Skip prefix tokens when decoding
    n_prefix = 1 + len(prefix_tokens)  # TOKEN_SOT + TOKEN_SOT + TOKEN_NO_TIMESTAMP
    text = tokenizer.decode(decoded_tokens[n_prefix:])
    return text.strip()


def Release():
    global decoder, encoder, whisper_tiny_en

    # Release the resources.
    del(decoder)
    del(encoder)
    del(whisper_tiny_en)


def chunk_and_resample_audio(
    audio: np.ndarray,
    audio_sample_rate: int,
    model_sample_rate=SAMPLE_RATE,
    model_chunk_seconds=CHUNK_LENGTH,
) -> list:
    from scipy import signal

    if audio_sample_rate != model_sample_rate:
        num_samples = int(len(audio) * model_sample_rate / audio_sample_rate)
        audio = signal.resample(audio, num_samples)
        audio_sample_rate = model_sample_rate

    number_of_full_length_audio_chunks = (
        audio.shape[0] // audio_sample_rate // model_chunk_seconds
    )
    last_sample_in_full_length_audio_chunks = (
        audio_sample_rate * number_of_full_length_audio_chunks * model_chunk_seconds
    )

    if number_of_full_length_audio_chunks == 0:
        return [audio]

    return [
        *np.array_split(
            audio[:last_sample_in_full_length_audio_chunks],
            number_of_full_length_audio_chunks,
        ),
        audio[last_sample_in_full_length_audio_chunks:],
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--audio_file",
        type=str,
        default=jfk_wav_path,
        help="Audio file path ",
    )
    args = parser.parse_args()

    Init()

    Inference(args.audio_file)

    Release()


if __name__ == '__main__':
    main()

