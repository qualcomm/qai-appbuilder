# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ─────────────────────────────────────────────────────────────────────────────
#
# Shared utilities for Speech Recognition (Whisper) model inference scripts.
# Provides model download, QNN initialization, audio preprocessing, and common
# decoding logic for Whisper encoder-decoder models.
#

import sys
import os
import numpy as np
from pathlib import Path

# scipy.special is used for log_softmax / logsumexp.
# On some WoS builds scipy fails to load (missing _fblas.dll), so we import
# lazily and fall back to pure-numpy implementations when unavailable.
try:
    from scipy import special as scipy_special
    _SCIPY_OK = True
except Exception:
    scipy_special = None
    _SCIPY_OK = False


def _log_softmax(x: np.ndarray) -> np.ndarray:
    """Numerically stable log-softmax (numpy fallback for scipy)."""
    x = x - x.max()
    return x - np.log(np.sum(np.exp(x)))


def _logsumexp(x: np.ndarray) -> float:
    """Numerically stable logsumexp (numpy fallback for scipy)."""
    x_max = x.max()
    return float(x_max + np.log(np.sum(np.exp(x - x_max))))

import install
from qai_appbuilder import (
    QNNContext,
    Runtime,
    LogLevel,
    ProfilingLevel,
    PerfProfile,
    QNNConfig,
    DataType,
)


# ─────────────────────────────────────────────────────────────────────────────
# Whisper Model Constants (from qai_hub_models)
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_RATE = 16000
CHUNK_LENGTH = 30
N_SAMPLES = CHUNK_LENGTH * SAMPLE_RATE
MEAN_DECODE_LEN = 200
AUDIO_EMB_LEN = 1500
MELS_AUDIO_LEN = AUDIO_EMB_LEN * 2

N_FFT = 400
HOP_LENGTH = 160
N_MELS = 80


# ─────────────────────────────────────────────────────────────────────────────
# Whisper constants
# ─────────────────────────────────────────────────────────────────────────────

TOKEN_SOT = 50257
TOKEN_EOT = 50256
TOKEN_BLANK = 220
TOKEN_NO_TIMESTAMP = 50362
TOKEN_TIMESTAMP_BEGIN = 50363
TOKEN_NO_SPEECH = 50361

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


# ─────────────────────────────────────────────────────────────────────────────
# Whisper Model Classes (from qai_hub_models)
# ─────────────────────────────────────────────────────────────────────────────

class CollectionModel:
    """Base class for collection models with component registration."""

    _components = {}

    @classmethod
    def add_component(cls, component_class):
        """Decorator to register model components."""
        def decorator(model_class):
            if not hasattr(model_class, '_components'):
                model_class._components = {}
            model_class._components[component_class.__name__] = component_class
            return model_class
        return decorator


class Whisper:
    """Base Whisper model class."""

    def __init__(self):
        self.mean_decode_len = MEAN_DECODE_LEN

    @classmethod
    def from_pretrained(cls, whisper_version: str = "base.en"):
        """Return a Whisper instance.

        On platforms where transformers/torch cannot be loaded (e.g. WoS with
        an incompatible torch build), we skip the HuggingFace download and
        return a plain instance — the QNN inference path only needs
        mean_decode_len, which is already set in __init__.
        """
        instance = cls()
        try:
            from transformers import WhisperForConditionalGeneration
            model_map = {
                "tiny.en": "openai/whisper-tiny.en",
                "base.en": "openai/whisper-base.en",
                "small.en": "openai/whisper-small.en",
                "medium.en": "openai/whisper-medium.en",
                "large": "openai/whisper-large",
            }
            hf_model_id = model_map.get(whisper_version, f"openai/whisper-{whisper_version}")
            instance.model = WhisperForConditionalGeneration.from_pretrained(hf_model_id)
        except Exception:
            # transformers or torch unavailable — QNN inference only needs
            # mean_decode_len which is already initialised in __init__
            pass
        return instance

    def get_encoder(self):
        """Get encoder from model."""
        if hasattr(self, 'model'):
            return self.model.get_encoder()
        return None

    def get_decoder(self):
        """Get decoder from model."""
        if hasattr(self, 'model'):
            return self.model.get_decoder()
        return None


class WhisperEncoderInf:
    """Whisper encoder inference wrapper."""
    pass


class WhisperDecoderInf:
    """Whisper decoder inference wrapper."""
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Model download helpers
# ─────────────────────────────────────────────────────────────────────────────

def download_whisper_assets(mel_filter_path: str, jfk_wav_path: str, jfk_npz_path: str):
    """Download Whisper shared assets (mel filters and audio samples).

    Parameters
    ----------
    mel_filter_path : path to mel_filters.npz
    jfk_wav_path    : path to jfk.wav
    jfk_npz_path    : path to jfk.npz
    """
    MEL_FILTER_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/whisper_asr_shared/v1/openai_assets/mel_filters.npz"
    JFK_WAV_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/whisper_asr_shared/v1/audio/jfk.wav"
    JFK_NPZ_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/whisper_asr_shared/v1/audio/jfk.npz"

    if not os.path.exists(mel_filter_path):
        install.download_url(MEL_FILTER_PATH_URL, mel_filter_path)

    if not os.path.exists(jfk_wav_path):
        install.download_url(JFK_WAV_PATH_URL, jfk_wav_path)

    if not os.path.exists(jfk_npz_path):
        install.download_url(JFK_NPZ_PATH_URL, jfk_npz_path)


def download_whisper_models(soc_id: str,
                            encoder_model_name: str, decoder_model_name: str,
                            encoder_model_path: str, decoder_model_path: str,
                            model_name: str, model_help_url: str) -> bool:
    """Download Whisper encoder and decoder models.

    Parameters
    ----------
    soc_id              : SoC ID for model lookup (e.g. 'wos', '9075')
    encoder_model_name  : model name key for encoder (used for hub lookup)
    decoder_model_name  : model name key for decoder (used for hub lookup)
    encoder_model_path  : destination path for encoder .bin
    decoder_model_path  : destination path for decoder .bin
    model_name          : model name for display
    model_help_url      : help URL for display on failure

    Returns
    -------
    bool : True on success, False on failure
    """
    desc = f"Downloading {model_name} model... "
    fail = f"\nFailed to download {model_name} model. Please prepare the model according to the steps in below link:\n{model_help_url}"

    ret = install.download_qai_hubmodel(soc_id, encoder_model_name, encoder_model_path, desc=desc, fail=fail)
    ret = install.download_qai_hubmodel(soc_id, decoder_model_name, decoder_model_path, desc=desc, fail=fail)

    return ret


# ─────────────────────────────────────────────────────────────────────────────
# QNN model base classes
# ─────────────────────────────────────────────────────────────────────────────

class WhisperEncoder(QNNContext):
    """Base class for Whisper encoder with simplified inference."""

    def Inference(self, input_data):
        input_datas = [input_data]
        output_data = super().Inference(input_datas)
        return output_data


class WhisperDecoder(QNNContext):
    """Base class for Whisper decoder with simplified inference."""

    def Inference(self, x, index, k_cache_cross, v_cache_cross, k_cache_self, v_cache_self):
        input_datas = [x, index, k_cache_cross, v_cache_cross, k_cache_self, v_cache_self]
        output_data = super().Inference(input_datas)
        return output_data


# ─────────────────────────────────────────────────────────────────────────────
# Audio preprocessing
# ─────────────────────────────────────────────────────────────────────────────

def _stft_numpy(audio: np.ndarray, n_fft: int, hop_length: int) -> np.ndarray:
    """Pure-numpy STFT using Hann window.

    Returns complex spectrogram of shape (n_fft//2+1, n_frames).
    """
    window = 0.5 * (1.0 - np.cos(2.0 * np.pi * np.arange(n_fft) / n_fft))  # Hann
    n_frames = 1 + (len(audio) - n_fft) // hop_length
    frames = np.lib.stride_tricks.as_strided(
        audio,
        shape=(n_fft, n_frames),
        strides=(audio.strides[0], audio.strides[0] * hop_length),
    ).copy()
    frames *= window[:, np.newaxis]
    spectrum = np.fft.rfft(frames, n=n_fft, axis=0)   # (n_fft//2+1, n_frames)
    return spectrum


def log_mel_spectrogram(
    mel_filter: np.ndarray,
    audio_np: np.ndarray,
    pad_to_length: int,
    n_fft: int,
    hop_length: int,
) -> np.ndarray:
    """Compute log mel spectrogram from audio.

    Uses torch if available (faster), otherwise falls back to a pure-numpy
    implementation so the function works on platforms where torch cannot load
    (e.g. Windows on Snapdragon with an incompatible torch build).

    Parameters
    ----------
    mel_filter      : mel filter bank  (n_mels, n_fft//2+1)
    audio_np        : audio samples as float32 numpy array
    pad_to_length   : pad audio to this length before STFT
    n_fft           : FFT size
    hop_length      : hop length for STFT

    Returns
    -------
    np.ndarray : log mel spectrogram, shape (1, n_mels, n_frames), float16
    """
    # ── Pad audio ─────────────────────────────────────────────────────────
    audio = audio_np.astype(np.float32)
    if pad_to_length is not None and len(audio) < pad_to_length:
        audio = np.pad(audio, (0, pad_to_length - len(audio)))

    # ── Try torch first (preferred: uses optimised STFT) ──────────────────
    try:
        import torch
        t_audio = torch.from_numpy(audio)
        window  = torch.hann_window(n_fft)
        stft    = torch.stft(t_audio, n_fft, hop_length, window=window, return_complex=True)
        magnitudes = stft[..., :-1].abs() ** 2          # drop last frame
        mel_spec   = torch.from_numpy(mel_filter) @ magnitudes
        log_spec   = torch.clamp(mel_spec, min=1e-10).log10()
        log_spec   = torch.maximum(log_spec, log_spec.max() - 8.0)
        log_spec   = (log_spec + 4.0) / 4.0
        return (
            log_spec.unsqueeze(0).detach()
            .to(dtype=torch.float16).cpu().numpy()
        )
    except Exception:
        pass  # torch unavailable or broken — fall through to numpy path

    # ── Pure-numpy fallback ───────────────────────────────────────────────
    spectrum   = _stft_numpy(audio, n_fft, hop_length)  # (n_fft//2+1, n_frames)
    magnitudes = np.abs(spectrum[:, :-1]) ** 2           # drop last frame
    mel_spec   = mel_filter @ magnitudes                 # (n_mels, n_frames)
    log_spec   = np.log10(np.maximum(mel_spec, 1e-10))
    log_spec   = np.maximum(log_spec, log_spec.max() - 8.0)
    log_spec   = (log_spec + 4.0) / 4.0
    return log_spec[np.newaxis].astype(np.float16)       # (1, n_mels, n_frames)


# ─────────────────────────────────────────────────────────────────────────────
# Decoding logic
# ─────────────────────────────────────────────────────────────────────────────

def apply_timestamp_rules(
    logits: np.ndarray, tokens: list[int]
) -> tuple[np.ndarray, np.ndarray]:
    """Apply timestamp rules to logits during decoding.

    Parameters
    ----------
    logits : model output logits
    tokens : list of decoded tokens so far

    Returns
    -------
    tuple : (modified logits, log probabilities)
    """
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

    if _SCIPY_OK:
        logprobs = scipy_special.log_softmax(logits)
        timestamp_logprob = scipy_special.logsumexp(logprobs[TOKEN_TIMESTAMP_BEGIN:])
    else:
        logprobs = _log_softmax(logits)
        timestamp_logprob = _logsumexp(logprobs[TOKEN_TIMESTAMP_BEGIN:])
    max_text_token_logprob = logprobs[:TOKEN_TIMESTAMP_BEGIN].max()

    if timestamp_logprob > max_text_token_logprob:
        logits[:TOKEN_TIMESTAMP_BEGIN] = -np.inf

    return logits, logprobs


# ─────────────────────────────────────────────────────────────────────────────
# Performance profiling
# ─────────────────────────────────────────────────────────────────────────────

def run_with_perf_profile(model_instance, input_data):
    """Run inference with HTP burst performance profile.

    Parameters
    ----------
    model_instance : QNN model instance
    input_data     : input data

    Returns
    -------
    output from model inference
    """
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
    output = model_instance.Inference(input_data)
    PerfProfile.RelPerfProfileGlobal()
    return output


# ─────────────────────────────────────────────────────────────────────────────
# Tokenizer helper
# ─────────────────────────────────────────────────────────────────────────────

def get_whisper_tokenizer(multilingual=False, language="en", task="transcribe"):
    """Get Whisper tokenizer.

    Parameters
    ----------
    multilingual : bool
        Whether to use multilingual model
    language : str
        Language code (e.g., 'en')
    task : str
        Task type ('transcribe' or 'translate')

    Returns
    -------
    tokenizer : WhisperTokenizer
        Tokenizer for decoding tokens
    """
    try:
        from transformers import WhisperTokenizer
        model_id = "openai/whisper-tiny.en" if language == "en" else "openai/whisper-tiny"
        return WhisperTokenizer.from_pretrained(model_id)
    except Exception:
        # Fallback to tiktoken-based tokenizer if transformers fails
        try:
            import tiktoken
            return TiktokenWhisperTokenizer()
        except ImportError:
            return SimpleWhisperTokenizer()


class TiktokenWhisperTokenizer:
    """Whisper tokenizer using tiktoken with special token handling."""

    def __init__(self):
        import tiktoken
        self.enc = tiktoken.get_encoding("gpt2")
        # Whisper special tokens
        self.special_tokens = {
            50257: "<|startoftranscript|>",
            50256: "<|endoftext|>",
            50362: "<|notimestamps|>",
            50363: "<|timestamp_begin|>",
            50361: "<|nospeech|>",
        }

    def decode(self, tokens):
        """Decode token IDs to text, handling special tokens."""
        # Filter out special tokens
        regular_tokens = [t for t in tokens if t not in self.special_tokens and t < 50257]
        if not regular_tokens:
            return ""
        try:
            return self.enc.decode(regular_tokens)
        except Exception:
            return ""


class SimpleWhisperTokenizer:
    """Simple Whisper tokenizer for offline use."""

    def __init__(self):
        # Basic token to text mapping for common Whisper tokens
        self.token_map = {
            50257: "<|startoftranscript|>",
            50256: "<|endoftext|>",
            50362: "<|notimestamps|>",
            50363: "<|timestamp_begin|>",
            50361: "<|nospeech|>",
        }

    def decode(self, tokens):
        """Decode token IDs to text."""
        text_parts = []
        for token_id in tokens:
            if token_id in self.token_map:
                # Skip special tokens
                continue
            elif token_id < 50257:
                # Regular token - use basic decoding
                try:
                    # Try to decode as UTF-8
                    text_parts.append(chr(token_id) if token_id < 256 else "")
                except:
                    pass
        return "".join(text_parts).strip()


