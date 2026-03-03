#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

# ---------------------------------------------------------------------
# Whisper base.en ONNX Runtime inference code
# encoder:
#   input : mel (float32) [n_audio, 80, T]
#   output: n_layer_cross_k, n_layer_cross_v (float32) [6, n_audio, T, 512]
# decoder:
#   inputs : tokens(int64)[n_audio,n_tokens], offset(int64)[1],
#            in_n_layer_self_k_cache(float32)[6,n_audio,448,512],
#            in_n_layer_self_v_cache(float32)[6,n_audio,448,512],
#            n_layer_cross_k/v (float32)[6,n_audio,T,512]
#   outputs: logits(float32)[*,*,51864], out_n_layer_self_k_cache/v_cache(float32)[6,*,448,512]
# ---------------------------------------------------------------------

import argparse
import time
from typing import List, Tuple

import numpy as np
import torch
from scipy import special as scipy_special  # type: ignore
import onnxruntime as ort

# audio io
try:
    import audio2numpy as a2n
except Exception:
    a2n = None

try:
    import samplerate
except Exception:
    samplerate = None

# ---------------- Whisper constants ----------------
TOKEN_SOT = 50257
TOKEN_EOT = 50256
TOKEN_BLANK = 220
TOKEN_NO_TIMESTAMP = 50362
TOKEN_TIMESTAMP_BEGIN = 50363
TOKEN_NO_SPEECH = 50361

SAMPLE_RATE = 16000
N_FFT = 400
HOP_LENGTH = 160
N_MELS = 80
CHUNK_LENGTH = 30
MAX_AUDIO_SAMPLES = CHUNK_LENGTH * SAMPLE_RATE

NO_SPEECH_THR = 0.6

NON_SPEECH_TOKENS = [
    1, 2, 7, 8, 9, 10, 14, 25, 26, 27, 28, 29, 31, 58, 59, 60, 61, 62, 63,
    90, 91, 92, 93, 357, 366, 438, 532, 685, 705, 796, 930, 1058, 1220, 1267,
    1279, 1303, 1343, 1377, 1391, 1635, 1782, 1875, 2162, 2361, 2488, 3467,
    4008, 4211, 4600, 4808, 5299, 5855, 6329, 7203, 9609, 9959, 10563, 10786,
    11420, 11709, 11907, 13163, 13697, 13700, 14808, 15306, 16410, 16791,
    17992, 19203, 19510, 20724, 22305, 22935, 27007, 30109, 30420, 33409,
    34949, 40283, 40493, 40549, 47282, 49146, 50257, 50357, 50358, 50359,
    50360, 50361,
]

SAMPLE_BEGIN = 1
precision = 0.02
max_initial_timestamp = 1.0
max_initial_timestamp_index = int(max_initial_timestamp / precision)

# ---------------- utilities ----------------

def print_session_io(sess: ort.InferenceSession, name: str):
    print(f"\n[{name}] Inputs:")
    for i in sess.get_inputs():
        print(f"  - {i.name}: type={i.type}, shape={i.shape}")
    print(f"[{name}] Outputs:")
    for o in sess.get_outputs():
        print(f"  - {o.name}: type={o.type}, shape={o.shape}")


def read_audio(audio_path: str) -> Tuple[np.ndarray, int]:
    if a2n is not None:
        audio, sr = a2n.audio_from_file(audio_path)
        return audio, sr
    import soundfile as sf  # type: ignore
    audio, sr = sf.read(audio_path)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    return audio.astype(np.float32), int(sr)


def chunk_and_resample_audio(
    audio: np.ndarray,
    audio_sample_rate: int,
    model_sample_rate: int = SAMPLE_RATE,
    model_chunk_seconds: int = CHUNK_LENGTH,
) -> List[np.ndarray]:
    if audio_sample_rate != model_sample_rate:
        if samplerate is None:
            raise RuntimeError("don't find samplerate")
        audio = samplerate.resample(audio, model_sample_rate / audio_sample_rate)
        audio_sample_rate = model_sample_rate

    n_full = audio.shape[0] // audio_sample_rate // model_chunk_seconds
    last = audio_sample_rate * n_full * model_chunk_seconds
    if n_full == 0:
        return [audio]
    return [*np.array_split(audio[:last], n_full), audio[last:]]


def load_mel_filter(npz_path: str) -> np.ndarray:
    with np.load(npz_path) as f:
        key = f"mel_{N_MELS}"
        if key not in f:
            raise KeyError(f"{npz_path} don't find {key}")
        return f[key]


def log_mel_spectrogram(
    mel_filter: np.ndarray,
    audio_np: np.ndarray,
    pad_to_length: int,
    n_fft: int,
    hop_length: int,
) -> np.ndarray:
    
    audio = torch.from_numpy(audio_np)
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

    return (
        log_spec.unsqueeze(0)
        .detach()
        .to(dtype=torch.float32)
        .cpu()
        .numpy()
    )


def apply_timestamp_rules(logits: np.ndarray, tokens: List[int]) -> Tuple[np.ndarray, np.ndarray]:
    logits[TOKEN_NO_TIMESTAMP] = -np.inf

    seq = tokens[SAMPLE_BEGIN:]
    last_was_timestamp = len(seq) >= 1 and seq[-1] >= TOKEN_TIMESTAMP_BEGIN
    penultimate_was_timestamp = len(seq) < 2 or seq[-2] >= TOKEN_TIMESTAMP_BEGIN

    if last_was_timestamp:
        if penultimate_was_timestamp:
            logits[TOKEN_TIMESTAMP_BEGIN:] = -np.inf
        else:
            logits[:TOKEN_EOT] = -np.inf

    timestamps = [t for t in tokens if t >= TOKEN_TIMESTAMP_BEGIN]
    if len(timestamps) > 0:
        if last_was_timestamp and not penultimate_was_timestamp:
            timestamp_last = timestamps[-1]
        else:
            timestamp_last = timestamps[-1] + 1
        logits[TOKEN_TIMESTAMP_BEGIN:timestamp_last] = -np.inf

    if len(tokens) == SAMPLE_BEGIN:
        logits[:TOKEN_TIMESTAMP_BEGIN] = -np.inf
        last_allowed = TOKEN_TIMESTAMP_BEGIN + max_initial_timestamp_index
        logits[(last_allowed + 1):] = -np.inf

    logprobs = scipy_special.log_softmax(logits)
    timestamp_logprob = scipy_special.logsumexp(logprobs[TOKEN_TIMESTAMP_BEGIN:])
    max_text_token_logprob = logprobs[:TOKEN_TIMESTAMP_BEGIN].max()
    if timestamp_logprob > max_text_token_logprob:
        logits[:TOKEN_TIMESTAMP_BEGIN] = -np.inf

    return logits, logprobs


# ---------------- inference core ----------------

def infer_one_chunk(
    enc: ort.InferenceSession,
    dec: ort.InferenceSession,
    mel_filter: np.ndarray,
    audio: np.ndarray,
    sample_len: int = 224,
) -> str:
    mel = log_mel_spectrogram(mel_filter, audio, MAX_AUDIO_SAMPLES, N_FFT, HOP_LENGTH)

    # encoder expects float32
    t0 = time.time()
    n_layer_cross_k, n_layer_cross_v = enc.run(None, {"mel": mel})
    t1 = time.time()
    print(f"encoder time: {(t1 - t0) * 1000:.2f} ms")

    # decoder init
    tokens = np.array([[TOKEN_SOT]], dtype=np.int64)  # [1,1]
    decoded_tokens = [TOKEN_SOT]

    # caches: float32 [6, 1, 448, 512]
    k_self = np.zeros((6, 1, 448, 512), dtype=np.float32)
    v_self = np.zeros((6, 1, 448, 512), dtype=np.float32)

    for i in range(sample_len):
        offset = np.array([i], dtype=np.int64)  # shape [1]

        t0 = time.time()
        logits, k_self, v_self = dec.run(
            None,
            {
                "tokens": tokens,
                "in_n_layer_self_k_cache": k_self,
                "in_n_layer_self_v_cache": v_self,
                "n_layer_cross_k": n_layer_cross_k,
                "n_layer_cross_v": n_layer_cross_v,
                "offset": offset,
            },
        )
        t1 = time.time()
        
        # print(f"decoder step {i} time: {(t1 - t0) * 1000:.2f} ms")

        # logits shape: [1, n_tokens, vocab] 
        step_logits = logits[0, -1] if logits.ndim == 3 else logits[0]

        if i == 0:
            step_logits[[TOKEN_EOT, TOKEN_BLANK]] = -np.inf
        step_logits[NON_SPEECH_TOKENS] = -np.inf

        step_logits, logprobs = apply_timestamp_rules(step_logits, decoded_tokens)

        if i == 0:
            no_speech_prob = float(np.exp(logprobs[TOKEN_NO_SPEECH]))
            if no_speech_prob > NO_SPEECH_THR:
                break

        next_token = int(np.argmax(step_logits))
        if next_token == TOKEN_EOT:
            break

        decoded_tokens.append(next_token)
        tokens = np.array([[next_token]], dtype=np.int64)

    # decode tokens -> text
    try:
        import whisper  # type: ignore
        tokenizer = whisper.decoding.get_tokenizer(multilingual=False, language="en", task="transcribe")
        return tokenizer.decode(decoded_tokens[1:]).strip()
    except Exception:
        return "TOKENS:" + ",".join(map(str, decoded_tokens[1:]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--audio_file', type=str, required=True)
    ap.add_argument('--encoder_onnx', '--encoder', dest='encoder', type=str, required=True)
    ap.add_argument('--decoder_onnx', '--decoder', dest='decoder', type=str, required=True)
    ap.add_argument('--mel_filters', type=str, required=True)
    ap.add_argument('--providers', type=str, default='CPUExecutionProvider')
    ap.add_argument('--sample_len', type=int, default=224)
    args = ap.parse_args()

    providers = [p.strip() for p in args.providers.split(',') if p.strip()]
    so = ort.SessionOptions()
    so.log_severity_level = 0

    enc = ort.InferenceSession(args.encoder, sess_options=so, providers=providers)
    dec = ort.InferenceSession(args.decoder, sess_options=so, providers=providers)

    print_session_io(enc, 'encoder')
    print_session_io(dec, 'decoder')

    mel_filter = load_mel_filter(args.mel_filters)
    audio, sr = read_audio(args.audio_file)

    texts = []
    t0 = time.time()
    for chunk in chunk_and_resample_audio(audio, sr):
        texts.append(infer_one_chunk(enc, dec, mel_filter, chunk, sample_len=args.sample_len))
    t1 = time.time()
    print(f"inference total time: {(t1 - t0) * 1000:.2f} ms")
    print('\nTranscription:', ' '.join(texts))


if __name__ == '__main__':
    main()
