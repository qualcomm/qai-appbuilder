# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import sys
import os
sys.path.append(".")
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "common"))
import install

import numpy as np
import samplerate
import time
import argparse
import audio2numpy as a2n

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

from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)

####################################################################


MODEL_NAME="whisper_base_en"
ENCODER_MODEL_ID = "mqvvjzzeq"
DECODER_MODEL_ID = "mq8ylzzpm"
ENCODER_MODEL_NAME = "whisper_base_en-whisperencoder-snapdragon_x_elite"
DECODER_MODEL_NAME = "whisper_base_en-whisperdecoder-snapdragon_x_elite"
MODEL_HELP_URL = "https://github.com/qualcomm/qai-appbuilder/tree/main/samples/python/" + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"
WHISPER_VERSION = "base.en"
####################################################################

execution_ws = os.getcwd()

if not "python" in execution_ws:
    execution_ws = execution_ws + "\\" + "python"

if not MODEL_NAME in execution_ws:
    execution_ws = execution_ws + "\\" + MODEL_NAME

model_dir = execution_ws + "\\models"
encoder_model_path = model_dir + "\\" + ENCODER_MODEL_NAME + ".bin"
decoder_model_path = model_dir + "\\" + DECODER_MODEL_NAME + ".bin"

jfk_wav_path = execution_ws + "\\jfk.wav"
jfk_npz_path = execution_ws + "\\jfk.npz"
mel_filter_path = execution_ws + "\\mel_filters.npz"
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

# Whisper constants
SAMPLE_RATE = 16000

MAX_AUDIO_SAMPLES=CHUNK_LENGTH * SAMPLE_RATE

# Above this prob we deem there's no speech in the audio
NO_SPEECH_THR = 0.6

# https://github.com/openai/whisper/blob/v20230314/whisper/decoding.py#L545
precision = 0.02  # in second
max_initial_timestamp = 1.0  # in second
max_initial_timestamp_index = int(max_initial_timestamp / precision)

####################################################################
encoder=None
decoder=None
whisper_base_en=None
mel_filter=None

# Encoder/Decoder class which inherited from the class QNNContext.
class Encoder(QNNContext):
    def Inference(self, input_data):
        input_datas=[input_data]
        output_data = super().Inference(input_datas) 
        k_cache_cross = output_data[0]
        k_cache_cross = k_cache_cross.reshape(6, 8, 64, 1500)
        v_cache_cross = output_data[1]
        v_cache_cross = v_cache_cross.reshape(6, 8, 1500, 64)        
        return k_cache_cross, v_cache_cross

class Decoder(QNNContext):
    def Inference(self, x, index, k_cache_cross, v_cache_cross, k_cache_self, v_cache_self):
        if x.dtype != np.float32:
            x = np.asarray(x, dtype=np.float32, order="C")
        else:
            x = np.ascontiguousarray(x)
        if index.dtype != np.float32:
            index = np.asarray(index, dtype=np.float32, order="C")
        else:
            index = np.ascontiguousarray(index)

        input_datas=[x, index, k_cache_cross, v_cache_cross, k_cache_self, v_cache_self]

        output_data = super().Inference(input_datas)
        logits = output_data[0]
        logits = logits.reshape(1, 1, 51864)
        k_cache = output_data[1]
        k_cache = k_cache.reshape(6, 8, 64, 224)
        v_cache = output_data[2]
        v_cache = v_cache.reshape(6, 8, 224, 64)
        return logits, k_cache, v_cache


@CollectionModel.add_component(WhisperEncoderInf)
@CollectionModel.add_component(WhisperDecoderInf)
class WhisperBaseEn(Whisper):
    @classmethod
    def from_pretrained(cls):
        return super().from_pretrained(WHISPER_VERSION) 

def model_download():
    # Download shared Whisper assets (mel filters, sample audio)
    download_whisper_assets(mel_filter_path, jfk_wav_path, jfk_npz_path)

    # Download QNN model binaries
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
    global encoder,decoder,whisper_base_en,mel_filter

    model_download()

    with np.load(mel_filter_path) as f:
       mel_filter = f[f"mel_{N_MELS}"]

    whisper_base_en = WhisperBaseEn.from_pretrained() 

    # Config AppBuilder environment.
    QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for Decoder 
    decoder = Decoder("whisper_decoder", decoder_model_path)
    # Instance for Encoder 
    encoder = Encoder("whisper_encoder", encoder_model_path)

    print()
    print("Model decoder:")
    print("input_dataType: ",decoder.getInputDataType())
    print("output_dataType: ",decoder.getOutputDataType())
    print()

    print("Model encoder:")
    print("input_dataType: ",encoder.getInputDataType())
    print("output_dataType: ", encoder.getOutputDataType())
    print()

def Inference(audio_path):
    # Read and preprocess the audio.
    audio, audio_sample_rate = a2n.audio_from_file(audio_path)

    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    result=" ".join(
            transcribe_single_chunk(x)
            for x in chunk_and_resample_audio(audio, audio_sample_rate)
        )

    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()
    
    # show the generated text
    print("Transcription:",result)
    

def transcribe_single_chunk(audio: np.ndarray):
    mel_input = log_mel_spectrogram(
            mel_filter, audio, MAX_AUDIO_SAMPLES, N_FFT, HOP_LENGTH
    )
    k_cache_cross = np.zeros(
        (
            6,
            8,
            64,
            1500,
        )
    ).astype(np.float32)
    v_cache_cross = np.zeros(
        (
            6,
            8,
            1500,
            64,
        )
    ).astype(np.float32)
    time_start = time.time()
    print("mel_input", mel_input.dtype)
    k_cache_cross, v_cache_cross = encoder.Inference(mel_input)
    time_end = time.time()
    print("time consumes for encoder {:.2f}(ms)".format((time_end - time_start) * 1000))
    print("Decoder Inference k_cache_cross type", type(k_cache_cross), "shape ", k_cache_cross.shape, "type ", k_cache_cross.dtype);
    print("Decoder Inference v_cache_cross type", type(v_cache_cross), "shape ", v_cache_cross.shape, "type ", v_cache_cross.dtype);

    # Start decoding
    x = np.array([[TOKEN_SOT]], dtype=np.float32)
    decoded_tokens = [TOKEN_SOT]
    sample_len = whisper_base_en.mean_decode_len  # mean # of tokens to sample

    logits = np.zeros((1, 1, 51864,)).astype(np.float32)
    k_cache_self = np.zeros((6, 8, 64, 224,)).astype(np.float32)
    v_cache_self = np.zeros((6, 8, 224, 64,)).astype(np.float32)
        
    sum_logprobs = 0
    print("start decode sample_len ", sample_len)
    for i in range(sample_len):
        index = np.array([[i]], dtype=np.float32)

        time_start = time.time()
        print(x.dtype, index.dtype, k_cache_cross.dtype, v_cache_cross.dtype, k_cache_self.dtype, v_cache_self.dtype)
        logits, k_cache_self, v_cache_self = decoder.Inference(
            x, index, k_cache_cross, v_cache_cross, k_cache_self, v_cache_self
        )

        time_end = time.time()
        print("time consumes for decoder {:.2f}(ms)".format((time_end - time_start) * 1000))

        # logit has shape (51864,)
        logits = logits[0, -1]  # consider only the last token

        # Filters
        # SuppressBlank
        if i == 0:
            logits[[TOKEN_EOT, TOKEN_BLANK]] = -np.inf
        # SuppressTokens
        logits[NON_SPEECH_TOKENS] = -np.inf

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
        x = np.array([[next_token]], dtype=np.float32)
        decoded_tokens.append(int(next_token))

    tokenizer = get_whisper_tokenizer(
        multilingual=False, language="en", task="transcribe"
    )

    text = tokenizer.decode(decoded_tokens[1:])  # remove TOKEN_SOT
    return text.strip()



def Release():
    global decoder,encoder,whisper_base_en
    

    # Release the resources.
    del(decoder)
    del(encoder)
    del(whisper_base_en)


def chunk_and_resample_audio(
    audio: np.ndarray,
    audio_sample_rate: int,
    model_sample_rate=SAMPLE_RATE,
    model_chunk_seconds=CHUNK_LENGTH,
) -> list[np.ndarray]:
    if audio_sample_rate != model_sample_rate:
        audio = samplerate.resample(audio, model_sample_rate / audio_sample_rate)
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
        default=execution_ws+"\\jfk.wav",
        help="Audio file path ",
    )
    args = parser.parse_args()

    Init()

    Inference(args.audio_file)

    Release()
    

if __name__ == '__main__':
    main()
