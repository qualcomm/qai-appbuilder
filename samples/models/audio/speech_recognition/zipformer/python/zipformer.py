# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
Zipformer ASR Inference Script (standalone, qai_appbuilder / NPU)
=================================================================
Model   : Zipformer (qualcomm/Zipformer on AI Hub / Hugging Face)
Format  : QNN_CONTEXT_BINARY (.bin) — encoder.bin / decoder.bin / joiner.bin
Runtime : qai_appbuilder.QNNContext on the HTP (NPU)
Task    : Streaming RNN-T ASR, Mandarin + English mixed
Pipeline: fbank features -> chunked streaming encoder -> frame-wise greedy joiner

The three .bin files are QNN context binaries loaded directly by
``qai_appbuilder.QNNContext`` (runtime="Htp", NATIVE dtype I/O). This is the
same NPU-inference path used by the other aihub-model-run models
(beit / melotts_zh / resnet50) and by the App Builder runner
``factory/chat_features/app-builder/models/zipformer-zh/runner.py`` — this script is a
standalone port of that reference algorithm.

Usage:
  python zipformer.py --audio <path_to_16k_mono.wav>
  python zipformer.py --audio a.wav --model_dir <dir with encoder/decoder/joiner.bin> --tokens <tokens.txt>

The audio is auto-resampled to 16 kHz mono if needed (scipy / soundfile).
Models are auto-downloaded on first run from the AI Hub public S3 bucket.
"""

import sys
import os

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Make samples/shared/python importable ─────────────────────────────────────
# Script lives at models/audio/speech_recognition/zipformer/python/
# shared/python is six levels up from here.
sys.path.append(".")
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "..", "..", "..", "..", "shared", "python"))

import time
import zipfile
import argparse
import numpy as np

# ─── qai_appbuilder (NPU inference) ──────────────────────────────────────────
# QNNConfig.Config() MUST be the first qai_appbuilder call in the process
# (otherwise QNNContext construction dereferences uninitialized state and the
# process exits with 0xC0000005 — see SKILL.md Issue 9).
from qai_appbuilder import (   # noqa: E402
    QNNContext, QNNConfig, Runtime, LogLevel, ProfilingLevel, PerfProfile,
)

QNNConfig.Config(Runtime.HTP, LogLevel.ERROR, ProfilingLevel.OFF)

# ─── Script / working directory ──────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# WORK_DIR defaults to the `models/` folder that is a SIBLING of this script's
# python/ directory (same convention as the sibling melotts_zh.py sample).
# Override with the ZIPFORMER_WORK_DIR env var.
WORK_DIR = os.environ.get("ZIPFORMER_WORK_DIR",
                          os.path.join(_SCRIPT_DIR, "..", "models"))

# ─── AI Hub download URLs (QNN_CONTEXT_BINARY, v0.55.0) ──────────────────────
_MODEL_PREFIX = "zipformer-qnn_context_binary-float-qualcomm_snapdragon_"
_MODEL_URLS = {
    "snapdragon_x_elite": (
        "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/"
        "models/zipformer/releases/v0.55.0/"
        "zipformer-qnn_context_binary-float-qualcomm_snapdragon_x_elite.zip"
    ),
    "snapdragon_x2_elite": (
        "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/"
        "models/zipformer/releases/v0.55.0/"
        "zipformer-qnn_context_binary-float-qualcomm_snapdragon_x2_elite.zip"
    ),
}

# tokens.txt — bundled in the official qai_hub_models GitHub repo
# (not included in the QNN_CONTEXT_BINARY ZIP, must be downloaded separately)
_TOKENS_URL = (
    "https://raw.githubusercontent.com/qualcomm/ai-hub-models/main/"
    "src/qai_hub_models/models/zipformer/tokens.txt"
)

# Test audio options (tried in order; first successful download is used):
#
#  1. sherpa-onnx Chinese speech test audio (~5.6 s, 16 kHz mono) — DEFAULT
#     Source: https://huggingface.co/csukuangfj/
#             sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23
_TEST_AUDIO_URLS = [
    (
        "https://huggingface.co/csukuangfj/"
        "sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23/"
        "resolve/main/test_wavs/0.wav",
        "test_zh.wav",
    ),
    #  2. Official AI Hub public asset — English (Common Voice), ~15 s, 241 KB
    #     Used by the official qai_hub_models demo.py:
    #       CachedWebModelAsset.from_asset_store("hf_whisper_asr_shared", "1",
    #                                            "audio/common_voice_en_19653650.wav")
    #     Direct S3 URL (publicly accessible, no auth required):
    (
        "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/"
        "qai-hub-models/models/hf_whisper_asr_shared/v1/audio/"
        "common_voice_en_19653650.wav",
        "test_en.wav",
    ),
]

_REQUIRED_MODELS = ["encoder.bin", "decoder.bin", "joiner.bin"]

ENCODER_FILENAME = "encoder.bin"
DECODER_FILENAME = "decoder.bin"
JOINER_FILENAME  = "joiner.bin"
TOKENS_FILENAME  = "tokens.txt"

# ─── Streaming Zipformer constants (must match the QNN export) ───────────────
SAMPLE_RATE = 16000
N_MELS = 80
HIGH_FREQ = -400.0          # kaldi_native_fbank: Nyquist - 400 Hz

DECODE_CHUNK_SIZE = 32
OFFSET = DECODE_CHUNK_SIZE * 2          # 64 frames — chunk stride
SEGMENT = OFFSET + 7                    # 71 frames — encoder input window
ENCODER_FRAMES_PER_CHUNK = 16           # encoder_out chunk shape (1, 16, 512)

BLANK_ID = 0
CONTEXT_SIZE = 2                        # decoder consumes the last 2 tokens

# 5 encoder layers; per-layer (n, key_history, value_history). 7 cache tensors
# per layer = 35 cache tensors total in the encoder I/O.
LAYER_CONFIGS = [
    (2, 128, 128),   # layer 0
    (4,  64,  64),   # layer 1
    (3,  32,  32),   # layer 2
    (2,  16,  16),   # layer 3
    (4,  64,  64),   # layer 4
]


# ─── Auto-download helpers ────────────────────────────────────────────────────

def _find_model_dir(work_dir):
    """Return the extracted chipset-suffixed subdir under work_dir, or None."""
    if not os.path.isdir(work_dir):
        return None
    for name in os.listdir(work_dir):
        full = os.path.join(work_dir, name)
        if os.path.isdir(full) and name.startswith(_MODEL_PREFIX):
            if all(os.path.isfile(os.path.join(full, m)) for m in _REQUIRED_MODELS):
                return full
    return None


def _download_models(work_dir):
    """Detect the chipset, download + extract the AI Hub ZIP, and fetch the
    test audio. Returns (model_dir, tokens_path, test_audio_path).

    Mirrors the auto-download approach of the sibling melotts_zh.py sample so
    the script works out of the box without a pre-populated C:\\WoS_AI folder.
    """
    import install

    os.makedirs(work_dir, exist_ok=True)

    model_dir = _find_model_dir(work_dir)

    if model_dir is None:
        device_model = install.detect_device_model()
        print(f"[INFO] Detected device model: {device_model}")
        url = _MODEL_URLS.get(device_model)
        if url is None:
            print(f"[WARN] Unknown device model '{device_model}', "
                  f"falling back to snapdragon_x_elite.")
            url = _MODEL_URLS["snapdragon_x_elite"]

        zip_path = os.path.join(work_dir, os.path.basename(url))
        print(f"[INFO] Downloading Zipformer models: {url}")
        ret = install.download_url(
            url, zip_path,
            desc=f"Downloading Zipformer models for {device_model}...",
            fail=(f"\nFailed to download Zipformer models from:\n  {url}\n"
                  f"Please download manually and extract to: {work_dir}"),
        )
        if not ret:
            raise RuntimeError(f"Model download failed from {url}")

        print(f"[INFO] Extracting models to {work_dir} ...")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(work_dir)
        try:
            os.remove(zip_path)
        except OSError:
            pass

        model_dir = _find_model_dir(work_dir)
        if model_dir is None:
            raise FileNotFoundError(
                f"No '{_MODEL_PREFIX}*' subdir with {_REQUIRED_MODELS} under "
                f"{work_dir} after extraction. Check the ZIP contents."
            )

    # tokens.txt — not included in the QNN_CONTEXT_BINARY ZIP; download from GitHub.
    # Search order: model_dir → work_dir → download to work_dir.
    tokens_path = os.path.join(model_dir, TOKENS_FILENAME)
    if not os.path.isfile(tokens_path):
        tokens_path = os.path.join(work_dir, TOKENS_FILENAME)
    if not os.path.isfile(tokens_path):
        print(f"[INFO] Downloading tokens.txt: {_TOKENS_URL}")
        ret = install.download_url(
            _TOKENS_URL, tokens_path,
            desc="Downloading Zipformer tokens.txt (vocabulary)...",
            fail=(f"\nFailed to download tokens.txt from:\n  {_TOKENS_URL}\n"
                  f"Please download manually and save to: {tokens_path}"),
        )
        if not ret or not os.path.isfile(tokens_path):
            print("[WARN] tokens.txt download failed — output will show token IDs instead of text.")
            tokens_path = tokens_path  # keep path; load_tokens() will warn

    # Test audio — try each URL in _TEST_AUDIO_URLS until one succeeds
    test_audio_path = None
    for audio_url, audio_filename in _TEST_AUDIO_URLS:
        candidate = os.path.join(work_dir, audio_filename)
        if os.path.isfile(candidate):
            test_audio_path = candidate
            break
        print(f"[INFO] Downloading test audio: {audio_url}")
        ret = install.download_url(
            audio_url, candidate,
            desc=f"Downloading Zipformer test audio ({audio_filename})...",
            fail=(f"\nFailed to download test audio from:\n  {audio_url}\n"
                  f"Trying next source..."),
        )
        if ret and os.path.isfile(candidate):
            test_audio_path = candidate
            break
        print(f"[WARN] Download failed for {audio_url}, trying next source...")

    if test_audio_path is None:
        print("[WARN] All test audio downloads failed. "
              "You can still run with --audio <your_wav>.")

    return model_dir, tokens_path, test_audio_path


# ─── Encoder streaming cache state ───────────────────────────────────────────

def init_state():
    """Build the 35-tensor zero-initialized encoder cache (per-layer order).

    Per layer (n, kh, vh), 7 tensors in this exact order:
      cached_len (n,1) int32, cached_avg (n,1,384), cached_key (n,kh,1,192),
      cached_val (n,vh,1,96), cached_val2 (n,vh,1,96),
      cached_conv1 (n,1,384,30), cached_conv2 (n,1,384,30)  -- all float32.
    """
    state = []
    for n, kh, vh in LAYER_CONFIGS:
        state.append(np.zeros((n, 1),          dtype=np.int32))
        state.append(np.zeros((n, 1, 384),     dtype=np.float32))
        state.append(np.zeros((n, kh, 1, 192), dtype=np.float32))
        state.append(np.zeros((n, vh, 1,  96), dtype=np.float32))
        state.append(np.zeros((n, vh, 1,  96), dtype=np.float32))
        state.append(np.zeros((n, 1, 384, 30), dtype=np.float32))
        state.append(np.zeros((n, 1, 384, 30), dtype=np.float32))
    return state   # 35 tensors


def parse_encoder_outputs(outputs):
    """Split the 36-tensor encoder output into (encoder_out, new_state).

    The QNN export emits the 35 cache tensors first, then encoder_out
    (shape (1, 16, 512)) LAST (index 35). Order is fixed by the model export.
    """
    outputs = [np.array(o) for o in outputs]
    if len(outputs) != 36:
        raise RuntimeError(f"unexpected encoder output count: got {len(outputs)}, want 36")
    encoder_out = outputs[35].astype(np.float32)
    new_state = outputs[:35]
    return encoder_out, new_state


# ─── QNNContext model wrappers ───────────────────────────────────────────────

class EncoderModel(QNNContext):
    """36 inputs (1 chunk + 35 cache) -> 36 outputs (35 new cache + encoder_out)."""

    def Inference(self, chunk, state):
        inputs = [np.ascontiguousarray(chunk, dtype=np.float32)]
        for s in state:
            inputs.append(np.ascontiguousarray(s))
        return super().Inference(inputs)


class DecoderModel(QNNContext):
    """y (1,2) int32 -> decoder_out (1,512) float32."""

    def Inference(self, y):
        out = super().Inference([np.ascontiguousarray(y, dtype=np.int32)])
        return np.array(out[0], dtype=np.float32).reshape(1, 512)


class JoinerModel(QNNContext):
    """encoder_out (1,512) + decoder_out (1,512) -> logit (1,6254) float32."""

    def Inference(self, enc_frame, dec_out):
        out = super().Inference([
            np.ascontiguousarray(enc_frame, dtype=np.float32),
            np.ascontiguousarray(dec_out, dtype=np.float32),
        ])
        return np.array(out[0], dtype=np.float32).reshape(-1)


# ─── Feature extraction (kaldi_native_fbank, fallback to numpy) ──────────────

def compute_fbank(waveform, sr):
    """80-channel kaldi-style log-mel fbank, shape (T, 80) float32.

    Prefers ``kaldi_native_fbank`` (matches the sherpa-onnx training recipe:
    dither=0, snip_edges=False, high_freq=Nyquist-400). Falls back to a numpy
    STFT implementation only if kaldi_native_fbank is unavailable.
    """
    if sr != SAMPLE_RATE:
        import scipy.signal
        n = int(len(waveform) * SAMPLE_RATE / sr)
        waveform = scipy.signal.resample(waveform, n).astype(np.float32)
        sr = SAMPLE_RATE

    try:
        import kaldi_native_fbank as knf
    except ImportError:
        return _compute_fbank_numpy(waveform, sr)

    opts = knf.FbankOptions()
    opts.frame_opts.dither = 0
    opts.frame_opts.snip_edges = False
    opts.frame_opts.samp_freq = sr
    opts.mel_opts.num_bins = N_MELS
    opts.mel_opts.high_freq = HIGH_FREQ

    extractor = knf.OnlineFbank(opts)
    extractor.accept_waveform(sampling_rate=sr, waveform=waveform.astype(np.float32, copy=False))
    n_ready = extractor.num_frames_ready
    if n_ready <= 0:
        return np.zeros((0, N_MELS), dtype=np.float32)
    frames = np.empty((n_ready, N_MELS), dtype=np.float32)
    for i in range(n_ready):
        frames[i] = extractor.get_frame(i)
    return frames


def _compute_fbank_numpy(waveform, sr):
    """Numpy STFT log-mel fallback (used only when kaldi_native_fbank missing)."""
    def hz_to_mel(hz):
        return 2595.0 * np.log10(1.0 + hz / 700.0)

    def mel_to_hz(mel):
        return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)

    n_fft, hop, win = 512, 160, 400
    n_mels, fmin, fmax = N_MELS, 20, 8000
    mels = np.linspace(hz_to_mel(fmin), hz_to_mel(fmax), n_mels + 2)
    freqs = mel_to_hz(mels)
    bin_freqs = np.linspace(0, sr / 2, n_fft // 2 + 1)
    fb = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float32)
    for m in range(1, n_mels + 1):
        for k, f in enumerate(bin_freqs):
            if freqs[m - 1] <= f <= freqs[m]:
                fb[m - 1, k] = (f - freqs[m - 1]) / (freqs[m] - freqs[m - 1])
            elif freqs[m] < f <= freqs[m + 1]:
                fb[m - 1, k] = (freqs[m + 1] - f) / (freqs[m + 1] - freqs[m])

    waveform = np.concatenate([[waveform[0]], waveform[1:] - 0.97 * waveform[:-1]])
    waveform = np.pad(waveform, win // 2, mode="reflect")
    window = np.hanning(win).astype(np.float32)
    n_frames = (len(waveform) - win) // hop + 1
    frames = np.lib.stride_tricks.as_strided(
        waveform, shape=(n_frames, win),
        strides=(waveform.strides[0] * hop, waveform.strides[0]),
    ).copy()
    frames *= window
    frames = np.pad(frames, ((0, 0), (0, n_fft - win)))
    spec = np.abs(np.fft.rfft(frames, n=n_fft)) ** 2
    mel = spec @ fb.T
    log_mel = np.log(np.maximum(mel, 1e-10))
    log_mel -= log_mel.mean(axis=0, keepdims=True)
    return log_mel.astype(np.float32)


# ─── Token vocabulary ────────────────────────────────────────────────────────

def load_tokens(path):
    """Load sherpa-onnx tokens.txt ('<token> <id>' per line). Returns id->token list."""
    if not os.path.isfile(path):
        print(f"    [WARN] tokens.txt not found at {path} — will show token IDs")
        return None
    id_to_token = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.rsplit(" ", 1)
            if len(parts) == 2 and parts[1].lstrip("-").isdigit():
                id_to_token[int(parts[1])] = parts[0]
    if not id_to_token:
        return None
    size = max(id_to_token) + 1
    return [id_to_token.get(i, "") for i in range(size)]


def decode_tokens(token_ids, vocab):
    if vocab is None:
        return " ".join(str(t) for t in token_ids)
    pieces = []
    for tid in token_ids:
        tok = vocab[tid] if 0 <= tid < len(vocab) else ""
        if len(tok) >= 2 and tok.startswith("<") and tok.endswith(">"):
            continue   # suppress <blk> / <sos/eos> / <unk> specials
        pieces.append(tok)
    text = "".join(pieces).replace("\u2581", " ")   # SentencePiece word-start
    while "  " in text:
        text = text.replace("  ", " ")
    return text.strip()


# ─── Streaming RNN-T greedy decode ───────────────────────────────────────────

def streaming_decode(encoder, decoder, joiner, features):
    """Chunked streaming encoder + frame-wise greedy joiner. features: (T, 80)."""
    T = features.shape[0]
    if T == 0:
        return []
    feats = features[np.newaxis, ...].astype(np.float32, copy=False)   # (1, T, 80)

    # Prime decoder with [blank, blank].
    hyp = [BLANK_ID] * CONTEXT_SIZE
    decoder_out = decoder.Inference(np.array([hyp], dtype=np.int32))   # (1, 512)

    state = init_state()   # 35 tensors

    for start in range(0, T, OFFSET):
        chunk = feats[:, start:start + SEGMENT, :]            # (1, <=71, 80)
        if chunk.shape[1] < SEGMENT:
            chunk = np.pad(chunk, ((0, 0), (0, SEGMENT - chunk.shape[1]), (0, 0)),
                           mode="constant", constant_values=0.0)
        chunk = np.ascontiguousarray(chunk, dtype=np.float32)         # (1, 71, 80)

        enc_outputs = encoder.Inference(chunk, state)
        encoder_out, state = parse_encoder_outputs(enc_outputs)       # (1,16,512)
        if encoder_out.ndim == 3:
            encoder_out = encoder_out[0]                              # (16, 512)

        for t in range(encoder_out.shape[0]):
            logits = joiner.Inference(encoder_out[t:t + 1], decoder_out)   # (6254,)
            tid = int(np.argmax(logits))
            if tid != BLANK_ID:
                hyp.append(tid)
                decoder_out = decoder.Inference(
                    np.array([hyp[-CONTEXT_SIZE:]], dtype=np.int32)
                )

    return hyp[CONTEXT_SIZE:]   # drop the priming [blank, blank]


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    # ── Auto-download models on first run ─────────────────────────────────────
    print("[INFO] Checking / downloading Zipformer models ...")
    model_dir, tokens_path, test_audio_path = _download_models(WORK_DIR)
    print(f"[INFO] Model dir : {model_dir}")
    print(f"[INFO] Tokens    : {tokens_path}")

    parser = argparse.ArgumentParser(
        description="Zipformer streaming ASR inference (QNN_CONTEXT_BINARY + qai_appbuilder / NPU)"
    )
    parser.add_argument(
        "--audio",
        default=test_audio_path or "",
        help="Path to input WAV (16 kHz mono preferred). "
             "Defaults to the auto-downloaded test_zh.wav.",
    )
    parser.add_argument(
        "--model_dir",
        default=model_dir,
        help="Directory holding encoder.bin / decoder.bin / joiner.bin. "
             "Defaults to the auto-downloaded model directory.",
    )
    parser.add_argument(
        "--tokens",
        default=tokens_path,
        help="Path to sherpa-onnx tokens.txt. "
             "Defaults to the tokens.txt inside the downloaded model directory.",
    )
    args = parser.parse_args()

    if not args.audio:
        print("[ERROR] No audio file specified and test audio download failed.\n"
              "        Please pass --audio <path_to_16k_mono.wav>")
        sys.exit(1)

    encoder_path = os.path.join(args.model_dir, ENCODER_FILENAME)
    decoder_path = os.path.join(args.model_dir, DECODER_FILENAME)
    joiner_path  = os.path.join(args.model_dir, JOINER_FILENAME)
    for p in (encoder_path, decoder_path, joiner_path):
        if not os.path.isfile(p):
            print(f"[ERROR] model file not found: {p}\n"
                  f"        Download the QNN_CONTEXT_BINARY package (see NOTES.md) "
                  f"and pass --model_dir, or let the script auto-download it.")
            sys.exit(1)

    print("=" * 60)
    print("  Zipformer streaming ASR — qai_appbuilder / QNNContext (NPU)")
    print("=" * 60)

    # [1] Load audio.
    print(f"\n[1] Loading audio: {args.audio}")
    import soundfile as sf
    waveform, sr = sf.read(args.audio)
    if waveform.ndim > 1:
        waveform = waveform.mean(axis=1)
    waveform = np.asarray(waveform, dtype=np.float32)
    duration = len(waveform) / sr
    print(f"    Duration : {duration:.2f} s  |  Samples: {len(waveform)}  |  SR: {sr}")

    # Sanity: warn on silence / synthetic input (empty result is expected then).
    energy = float(np.sqrt(np.mean(waveform.astype(np.float32) ** 2))) if waveform.size else 0.0
    if energy < 1e-3:
        print("    [WARN] audio looks like silence/synthetic — empty result is expected")

    # [2] Features.
    print("\n[2] Extracting 80-dim log-mel features ...")
    t0 = time.perf_counter()
    features = compute_fbank(waveform, sr)
    print(f"    Shape    : {features.shape}  ({time.perf_counter() - t0:.3f}s)")

    # [3] Tokens.
    print("\n[3] Loading vocabulary ...")
    vocab = load_tokens(args.tokens)
    print(f"    Vocab size: {len(vocab) if vocab else 'N/A (token IDs only)'}")

    # [4] Load the three QNN context binaries on the NPU.
    print("\n[4] Loading QNN context binaries (encoder/decoder/joiner.bin) on HTP ...")
    t0 = time.perf_counter()
    encoder = EncoderModel("zipformer_encoder", encoder_path,
                           input_data_type="native", output_data_type="native")
    decoder = DecoderModel("zipformer_decoder", decoder_path,
                           input_data_type="native", output_data_type="native")
    joiner  = JoinerModel("zipformer_joiner",  joiner_path,
                          input_data_type="native", output_data_type="native")
    print(f"    Load time: {time.perf_counter() - t0:.2f}s")

    # [5] Inference (BURST perf around the decode loop).
    print("\n[5] Running streaming greedy search ...")
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
    t0 = time.perf_counter()
    try:
        token_ids = streaming_decode(encoder, decoder, joiner, features)
    finally:
        PerfProfile.RelPerfProfileGlobal()
    infer_time = time.perf_counter() - t0
    rtf = infer_time / duration if duration > 0 else float("nan")

    text = decode_tokens(token_ids, vocab)

    print("\n" + "=" * 60)
    print("  RECOGNITION RESULT")
    print("=" * 60)
    print(f"  Text     : {text}")
    print(f"  Tokens   : {len(token_ids)}")
    print(f"  Audio    : {duration:.2f}s")
    print(f"  Infer    : {infer_time:.3f}s")
    print(f"  RTF      : {rtf:.4f}  (< 1.0 = faster than real-time)")
    print("=" * 60)

    # Release NPU contexts.
    del encoder, decoder, joiner


if __name__ == "__main__":
    main()
