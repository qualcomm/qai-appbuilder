# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import sys
import os
sys.path.append(".")
sys.path.append("python")
import utils.install as install

import numpy as np
import samplerate
import torch
import whisper  # type: ignore
import time
from scipy import special as scipy_special  # type: ignore
import audio2numpy as a2n
import argparse

from qai_hub_models.models._shared.whisper.model import (
    CHUNK_LENGTH,
    HOP_LENGTH,
    N_FFT,
    N_MELS,
    SAMPLE_RATE,
)
from qai_hub_models.models._shared.whisper.model import (
    CollectionModel,
    Whisper,
    WhisperDecoderInf,
    WhisperEncoderInf,
)

from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)

####################################################################


MODEL_NAME="whisper_base_en"
ENCODER_MODEL_ID = "mqvvjzzeq"
DECODER_MODEL_ID = "mq8ylzzpm"
ENCODER_MODEL_NAME = "whisper_base_en-whisperencoder-snapdragon_x_elite"
DECODER_MODEL_NAME = "whisper_base_en-whisperdecoder-snapdragon_x_elite"
MODEL_HELP_URL = "https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/" + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"
WHISPER_VERSION = "base.en"
MEL_FILTER_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/whisper_asr_shared/v1/openai_assets/mel_filters.npz"
JFK_WAV_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/whisper_asr_shared/v1/audio/jfk.wav"
JFK_NPZ_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/whisper_asr_shared/v1/audio/jfk.npz"
####################################################################

execution_ws = os.getcwd()
qnn_dir = execution_ws + "\\qai_libs"

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


# Whisper constants
TOKEN_SOT = 50257  # Start of transcript
TOKEN_EOT = 50256  # end of transcript
TOKEN_BLANK = 220  # " "
TOKEN_NO_TIMESTAMP = 50362
TOKEN_TIMESTAMP_BEGIN = 50363
TOKEN_NO_SPEECH = 50361
SAMPLE_RATE = 16000

MAX_AUDIO_SAMPLES=CHUNK_LENGTH * SAMPLE_RATE #

# Above this prob we deem there's no speech in the audio
NO_SPEECH_THR = 0.6

# https://github.com/openai/whisper/blob/v20230314/whisper/decoding.py#L600
NON_SPEECH_TOKENS = [
    1,
    2,
    7,
    8,
    9,
    10,
    14,
    25,
    26,
    27,
    28,
    29,
    31,
    58,
    59,
    60,
    61,
    62,
    63,
    90,
    91,
    92,
    93,
    357,
    366,
    438,
    532,
    685,
    705,
    796,
    930,
    1058,
    1220,
    1267,
    1279,
    1303,
    1343,
    1377,
    1391,
    1635,
    1782,
    1875,
    2162,
    2361,
    2488,
    3467,
    4008,
    4211,
    4600,
    4808,
    5299,
    5855,
    6329,
    7203,
    9609,
    9959,
    10563,
    10786,
    11420,
    11709,
    11907,
    13163,
    13697,
    13700,
    14808,
    15306,
    16410,
    16791,
    17992,
    19203,
    19510,
    20724,
    22305,
    22935,
    27007,
    30109,
    30420,
    33409,
    34949,
    40283,
    40493,
    40549,
    47282,
    49146,
    50257,
    50357,
    50358,
    50359,
    50360,
    50361,
]

SAMPLE_BEGIN = 1  # first token is TOKEN_SOT

####################################################################

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
    ret = True

    if not os.path.exists(mel_filter_path):
        ret = install.download_url(MEL_FILTER_PATH_URL, mel_filter_path)
        
    if not os.path.exists(jfk_wav_path):
        ret = install.download_url(JFK_WAV_PATH_URL, jfk_wav_path)

    if not os.path.exists(jfk_npz_path):
        ret = install.download_url(JFK_NPZ_PATH_URL, jfk_npz_path)

    desc = f"Downloading {MODEL_NAME} model... "
    fail = f"\nFailed to download {MODEL_NAME} model. Please prepare the model according to the steps in below link:\n{MODEL_HELP_URL}"
    ret = install.download_qai_hubmodel(ENCODER_MODEL_ID, encoder_model_path, desc=desc, fail=fail)
    ret = install.download_qai_hubmodel(DECODER_MODEL_ID, decoder_model_path, desc=desc, fail=fail)

    if not ret:
        exit()

def Init():
    global encoder,decoder,whisper_base_en,mel_filter

    model_download()

    with np.load(mel_filter_path) as f:
       mel_filter = f[f"mel_{N_MELS}"]

    whisper_base_en =WhisperBaseEn.from_pretrained() 

    # Config AppBuilder environment.
    QNNConfig.Config(qnn_dir, Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for Decoder 
    decoder = Decoder("whisper_decoder", decoder_model_path)
        # Instance for Encoder 
    encoder = Encoder("whisper_encoder", encoder_model_path)

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
    k_cache_cross, v_cache_cross = encoder.Inference(mel_input)
    time_end = time.time()
    print("time consumes for encoder {}(s)".format(str(time_end - time_start)))
    #k_cache_cross = output[0]
    #v_cache_cross = output[1] 
    print("Decoder Inference k_cache_cross type", type(k_cache_cross), "shape ", k_cache_cross.shape, "type ", k_cache_cross.dtype);
    print("Decoder Inference v_cache_cross type", type(v_cache_cross), "shape ", v_cache_cross.shape, "type ", v_cache_cross.dtype);
    #k_cache_cross, v_cache_cross = self.encoder(mel_input)
    #print("mel_input", mel_input)
    # Start decoding
    # coreml only takes float tensors
    x = np.array([[TOKEN_SOT]])
    decoded_tokens = [TOKEN_SOT]
    sample_len = whisper_base_en.mean_decode_len  # mean # of tokens to sample

    logits = np.zeros(
        (
            1,
            1,
            51864,
        )
    ).astype(np.float32)
    k_cache_self = np.zeros(
        (
            6,
            8,
            64,
            224,
        )
    ).astype(np.float32)
    v_cache_self = np.zeros(
        (
            6,
            8,
            224,
            64,
        )
    ).astype(np.float32)
        
    sum_logprobs = 0
    print("start decode sample_len ", sample_len)
    for i in range(sample_len):
        # Using i to index inside the decoder model hurts the
        # the model performance.
        # index - used to get positional embedding correctly.
        #print("start decode ")
        index = torch.zeros([1, 1], dtype=torch.int32)
        index[0, 0] = i
        #print("start Inference ")
        time_start = time.time()
        logits, k_cache_self, v_cache_self = decoder.Inference(
            x, index, k_cache_cross, v_cache_cross, k_cache_self, v_cache_self
        )
        time_end = time.time()
        print("time consumes for decoder {}(s)".format(str(time_end - time_start)))
        #decoder_out = self.decoder(
        #    x, index, k_cache_cross, v_cache_cross, k_cache_self, v_cache_self
        #)
        #print("self.decoder.Inference done")
        # logit has shape (1, decoded_len, 51864)

        #logits = decoder_out[0]
        #k_cache_self = decoder_out[1]
        #v_cache_self = decoder_out[2]

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
        x = np.array([[next_token]])
        decoded_tokens.append(int(next_token))

    tokenizer = whisper.decoding.get_tokenizer(
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



def apply_timestamp_rules(
    logits: np.ndarray, tokens: list[int]
) -> tuple[np.ndarray, float | np.ndarray]:
    # Require producing timestamp
    logits[TOKEN_NO_TIMESTAMP] = -np.inf

    # timestamps have to appear in pairs, except directly before EOT
    seq = tokens[SAMPLE_BEGIN:]
    last_was_timestamp = len(seq) >= 1 and seq[-1] >= TOKEN_TIMESTAMP_BEGIN
    penultimate_was_timestamp = len(seq) < 2 or seq[-2] >= TOKEN_TIMESTAMP_BEGIN
    if last_was_timestamp:
        if penultimate_was_timestamp:  # has to be non-timestamp
            logits[TOKEN_TIMESTAMP_BEGIN:] = -np.inf
        else:  # cannot be normal text tokens
            logits[:TOKEN_EOT] = -np.inf

    timestamps = [t for t in tokens if t >= TOKEN_TIMESTAMP_BEGIN]
    if len(timestamps) > 0:
        # timestamps shouldn't decrease; forbid timestamp tokens smaller than the last
        # also force each segment to have a nonzero length, to   prevent infinite looping
        if last_was_timestamp and not penultimate_was_timestamp:
            timestamp_last = timestamps[-1]
        else:
            timestamp_last = timestamps[-1] + 1
        logits[TOKEN_TIMESTAMP_BEGIN:timestamp_last] = -np.inf

    if len(tokens) == SAMPLE_BEGIN:
        # suppress generating non-timestamp tokens at the beginning
        logits[:TOKEN_TIMESTAMP_BEGIN] = -np.inf

        # apply the `max_initial_timestamp` option
        last_allowed = TOKEN_TIMESTAMP_BEGIN + max_initial_timestamp_index
        logits[(last_allowed + 1) :] = -np.inf

    # if sum of probability over timestamps is above any other token, sample timestamp
    logprobs = scipy_special.log_softmax(logits)
    timestamp_logprob = scipy_special.logsumexp(logprobs[TOKEN_TIMESTAMP_BEGIN:])
    max_text_token_logprob = logprobs[:TOKEN_TIMESTAMP_BEGIN].max()
    if timestamp_logprob > max_text_token_logprob:
        # Mask out all but timestamp tokens
        logits[:TOKEN_TIMESTAMP_BEGIN] = -np.inf

    return logits, logprobs


# Adopted from https://github.com/openai/whisper/blob/main/whisper/audio.py
def log_mel_spectrogram(
    mel_filter: np.ndarray,
    audio_np: np.ndarray,
    pad_to_length: int,
    n_fft: int,
    hop_length: int,
) -> np.ndarray:
    audio = torch.from_numpy(audio_np)
    assert isinstance(audio, torch.Tensor)

    if pad_to_length is not None:
        padding = pad_to_length - len(audio)
        if padding > 0:
            audio = torch.nn.functional.pad(audio, (0, padding))
    window = torch.hann_window(n_fft)
    stft = torch.stft(audio, n_fft, hop_length, window=window, return_complex=True)
    magnitudes = stft[..., :-1].abs() ** 2

    mel_spec = torch.from_numpy(mel_filter) @ magnitudes

    log_spec = torch.clamp(mel_spec, min=1e-10).log10()
    log_spec = torch.maximum(log_spec, log_spec.max() - 8.0)
    log_spec = (log_spec + 4.0) / 4.0
    return log_spec.unsqueeze(0).detach().float().numpy()


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



        
def load_demo_audio() -> tuple[np.ndarray, int]:
#    TEST_AUDIO_PATH.fetch()
    with np.load("jfk.npz") as f:
        return f["audio"], SAMPLE_RATE


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
