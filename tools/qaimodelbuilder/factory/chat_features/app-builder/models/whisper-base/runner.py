#!/usr/bin/env python
# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
whisper-base (English) - App Builder Pack runner
================================================

Adapted from the QAI AppBuilder reference sample
``samples/python/whisper_base_en/whisper_base_en.py`` and wrapped into the
App Builder Pack runner protocol (plan v3.1 sections C*.6 / I.4 / R.3).

Model: ``whisper_base_en`` (English-only, base size, ~74 M params)
       Two QNN HTP context binaries:
         - encoder.bin
         - decoder.bin
       Both built with DataType.NATIVE inputs/outputs and the
       k/v cross-/self-attention cache shapes documented in the
       reference sample.

Pipeline per 30 s audio chunk:
  1. read_audio (shared/audio_io.py) -> 16 kHz mono float32
  2. log_mel_spectrogram (numpy STFT + mel filterbank from
     assets/mel_filters.npz, key 'mel_80') -> (1, 80, 3000) float16
  3. encoder.Inference(mel)
       -> k_cache_cross  (6, 8, 64,  1500) float
       -> v_cache_cross  (6, 8, 1500, 64)  float
  4. greedy decoder loop (max 224 steps) with the standard Whisper
     timestamp-rule logits filter (suppress non-speech, suppress blank,
     pair timestamps, max-initial-timestamp clamp). Per-step inputs:
        x               (1, 1)             int32   - last token
        index           (1, 1)             int32   - decode step
        k_cache_cross   (6, 8, 64,  1500)  float32
        v_cache_cross   (6, 8, 1500, 64)   float32
        k_cache_self    (6, 8, 64,  224)   float16
        v_cache_self    (6, 8, 224, 64)    float16
       Decoder outputs:
        logits          (1, 1, 51864)
        k_cache_self    (6, 8, 64,  224)
        v_cache_self    (6, 8, 224, 64)
  5. tokenizer.decode(...) (offline tiktoken, English)
     -> chunk text
  6. concatenate chunks -> fullText + segments[]

Sticky-worker mode (plan v3.1 R.5):
This runner exposes the three-stage persistent API (``load_model`` /
``run_inference`` / ``release_model``).  ``main()`` chains them so the
file remains usable as a one-shot subprocess script as well.

Isolation: imports only NumPy, scipy.special, tiktoken,
qai_appbuilder, and the App Builder ``shared/`` utilities. No imports
from features/model-builder.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

import numpy as np

# shared/ is on PYTHONPATH (injected by backend.app_builder.runners.python_script).
from runner_protocol import (   # noqa: E402
    emit, progress, read_request, status, result, done, fail,
)
from telemetry import StageTimer   # noqa: E402


# --- Whisper constants (match the reference sample) -------------------------

ENCODER_FILENAME = "encoder.bin"
DECODER_FILENAME = "decoder.bin"

SAMPLE_RATE   = 16_000
N_FFT         = 400
HOP_LENGTH    = 160
N_MELS        = 80
CHUNK_LENGTH  = 30
MAX_AUDIO_SAMPLES = CHUNK_LENGTH * SAMPLE_RATE   # 480_000
MAX_AUDIO_SEC = 120.0

# Whisper special tokens (English-only base.en vocab, size 51864)
TOKEN_SOT             = 50257   # <|startoftranscript|>
TOKEN_EOT             = 50256   # <|endoftext|>
TOKEN_BLANK           = 220     # " "
TOKEN_NO_TIMESTAMP    = 50362   # <|notimestamps|>
TOKEN_TIMESTAMP_BEGIN = 50363   # <|0.00|>
TOKEN_NO_SPEECH       = 50361   # <|nospeech|>

NO_SPEECH_THR = 0.6

# https://github.com/openai/whisper/blob/v20230314/whisper/decoding.py#L600
NON_SPEECH_TOKENS = [
    1, 2, 7, 8, 9, 10, 14, 25, 26, 27, 28, 29, 31, 58, 59, 60, 61, 62, 63,
    90, 91, 92, 93, 357, 366, 438, 532, 685, 705, 796, 930, 1058, 1220,
    1267, 1279, 1303, 1343, 1377, 1391, 1635, 1782, 1875, 2162, 2361,
    2488, 3467, 4008, 4211, 4600, 4808, 5299, 5855, 6329, 7203, 9609,
    9959, 10563, 10786, 11420, 11709, 11907, 13163, 13697, 13700, 14808,
    15306, 16410, 16791, 17992, 19203, 19510, 20724, 22305, 22935, 27007,
    30109, 30420, 33409, 34949, 40283, 40493, 40549, 47282, 49146, 50257,
    50357, 50358, 50359, 50360, 50361,
]

# Timestamp-rule tunables (whisper/decoding.py L545)
SAMPLE_BEGIN = 1                       # first emitted token is TOKEN_SOT
PRECISION_S = 0.02
MAX_INITIAL_TIMESTAMP_S = 1.0
MAX_INITIAL_TIMESTAMP_INDEX = int(MAX_INITIAL_TIMESTAMP_S / PRECISION_S)

MAX_DECODE_TOKENS = 224                # Whisper hard cap

# Safety: wall-clock timeout and heartbeat interval for the decode loop
MAX_DECODE_WALL_S = 180.0              # 3 minutes wall-clock timeout
_HEARTBEAT_INTERVAL_S = 30.0           # progress output every 30 s


# --- Domain errors ----------------------------------------------------------

class _UserError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --- Model auto-download (whisper_medium, v0.55.0) --------------------------
#
# edition-dual-form §4.2: whisper is upgraded base -> medium. V2 previously
# shipped ``whisper-base`` WITHOUT a downloader; we now reuse the shared
# weight-download helper (same one zipformer/melotts use — judgment-1: reuse,
# not re-create) to fetch the device-specific whisper_medium QNN context-binary
# zip from qai-hub public assets, picking ``x_elite`` / ``x2_elite`` via the
# shared CPU detection. The download routes through the global proxy when one
# is configured (HTTPS_PROXY / ALL_PROXY injected by the apps/api wiring root —
# 缺口 10) and connects directly otherwise. Idempotent: a run with both .bin
# files present skips the network entirely.

# Single source of truth for the download metadata (tag / required+optional
# file lists / per-device zip URLs) is the sibling ``weights.json``. It is
# loaded by RELATIVE PATH (``factory/`` is not a package — the runner reaches
# shared modules via sys.path injection, not package import) so the SAME
# metadata + extraction is reusable by both this runner subprocess and a
# future API-side downloader (single source of truth, no drift).
_WEIGHTS_CONFIG_PATH = Path(__file__).resolve().parent / "weights.json"


def _load_weights_config() -> dict:
    """Load + parse this pack's sibling ``weights.json`` (download metadata).

    Returns the parsed dict with ``tag`` / ``required_files`` /
    ``optional_files`` / ``download_configs``. Raises ``_UserError`` with a
    clear message if the file is missing or unparseable so the failure maps to
    ``voiceInput.weightsMissing`` rather than crashing the runner.
    """
    try:
        with open(_WEIGHTS_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise _UserError(
            "WEIGHTS_NOT_INSTALLED",
            f"failed to load weights config {_WEIGHTS_CONFIG_PATH}: {e}. "
            "Ensure the pack ships weights.json alongside runner.py, or "
            f"manually place the whisper_medium .bin files at the model dir.",
        ) from e


def _ensure_weights_downloaded(model_dir: Path) -> None:
    """Download + extract whisper_medium weights into ``model_dir`` if missing.

    Thin wrapper over the shared ``weight_downloader.ensure_weights_downloaded``;
    re-raises its structured failure as ``_UserError("WEIGHTS_NOT_INSTALLED")``
    so the SSE error event maps to ``voiceInput.weightsMissing`` on the
    frontend (hard-constraint ②: a download failure never crashes the app).

    Download metadata (tag / file lists / per-device URLs) comes from the
    pack's ``weights.json`` (single source of truth).
    """
    try:
        from weight_downloader import (  # type: ignore[import-not-found]
            WeightDownloadError,
            ensure_weights_downloaded,
        )
    except ImportError as e:
        raise _UserError(
            "WEIGHTS_NOT_INSTALLED",
            f"weight_downloader import failed: {e}. Ensure shared/ is on "
            "PYTHONPATH, or manually place the whisper_medium .bin files at "
            f"{model_dir}.",
        ) from e
    cfg = _load_weights_config()
    try:
        ensure_weights_downloaded(
            model_dir,
            download_configs=cfg["download_configs"],
            required_files=cfg["required_files"],
            optional_files=cfg.get("optional_files", ()),
            tag=cfg["tag"],
            progress_cb=progress,
        )
    except WeightDownloadError as e:
        raise _UserError("WEIGHTS_NOT_INSTALLED", e.message) from e


# --- Path resolution --------------------------------------------------------

def _resolve_repo_root(req: dict[str, Any]) -> Path:
    raw = req.get("repoRoot") or "."
    p = Path(raw)
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    if p.is_dir():
        return p
    return Path(__file__).resolve().parents[4]


def _resolve_pack_dir(req: dict[str, Any], repo_root: Path) -> Path:
    raw = req.get("packDir")
    if raw:
        p = Path(raw)
        if not p.is_absolute():
            p = (repo_root / p).resolve()
        if p.is_dir():
            return p
    return Path(__file__).resolve().parent


def _resolve_input_audio(req: dict[str, Any], repo_root: Path, pack_dir: Path) -> Path:
    inputs = req.get("inputs") or {}
    raw = inputs.get("audio")
    if not raw:
        raise _UserError(
            "INVALID_INPUT",
            "inputs.audio is required (path to source audio file)",
        )
    p = Path(raw)
    if not p.is_absolute():
        for base in (repo_root, pack_dir, Path.cwd()):
            cand = (base / p).resolve()
            if cand.is_file():
                return cand
        p = (Path.cwd() / p).resolve()
    if not p.is_file():
        raise _UserError("INVALID_INPUT", f"input audio not found: {raw}")
    return p


def _resolve_weights(repo_root: Path, pack_dir: Path) -> Tuple[Path, Path]:
    """Resolve encoder/decoder.bin, auto-downloading whisper_medium if missing.

    Search order: canonical ``<repo>/models/whisper-base/`` (preferred, also
    the auto-download target) → pack-internal ``weights/`` (manual staging
    only) → device-specific zip download from qai-hub public assets. The
    download is idempotent and routes through the global proxy when configured
    (缺口 10). On failure raises ``WEIGHTS_NOT_INSTALLED`` (hard-constraint ②:
    never crashes the app).
    """
    canonical_dir = (repo_root / "models" / "whisper-base").resolve()
    pack_weights_dir = (pack_dir / "weights").resolve()

    def _present(d: Path) -> tuple[Path | None, Path | None]:
        enc = d / ENCODER_FILENAME
        dec = d / DECODER_FILENAME
        return (enc if enc.is_file() else None, dec if dec.is_file() else None)

    # 1. Canonical location (preferred).
    enc, dec = _present(canonical_dir)
    if enc is not None and dec is not None:
        return enc.resolve(), dec.resolve()

    # 2. Pack-internal weights/ (manual staging only; never auto-download here).
    penc, pdec = _present(pack_weights_dir)
    if penc is not None and pdec is not None:
        return penc.resolve(), pdec.resolve()

    # 3. Auto-download fallback (whisper_medium, v0.55.0). Raises
    #    _UserError("WEIGHTS_NOT_INSTALLED", ...) on network/extraction failure.
    _ensure_weights_downloaded(canonical_dir)

    # 4. Re-check; if STILL missing, give up with a structured error.
    enc, dec = _present(canonical_dir)
    if enc is not None and dec is not None:
        return enc.resolve(), dec.resolve()

    missing: list[str] = []
    if enc is None:
        missing.append(
            ENCODER_FILENAME
            + f" (canonical: {canonical_dir / ENCODER_FILENAME}; "
            + f"pack: {pack_weights_dir / ENCODER_FILENAME})"
        )
    if dec is None:
        missing.append(
            DECODER_FILENAME
            + f" (canonical: {canonical_dir / DECODER_FILENAME}; "
            + f"pack: {pack_weights_dir / DECODER_FILENAME})"
        )
    raise _UserError(
        "WEIGHTS_NOT_INSTALLED",
        (
            "Whisper (medium) weights still missing after auto-download "
            "attempt:\n  - "
            + "\n  - ".join(missing)
            + f"\nManually place the .bin files at {canonical_dir}, or check "
              "network connectivity / proxy settings and re-run."
        ),
    )


def _resolve_mel_filter(pack_dir: Path) -> Path:
    mel = pack_dir / "assets" / "mel_filters.npz"
    if not mel.is_file():
        placeholder = pack_dir / "assets" / "mel_filters.npz.placeholder"
        hint = (
            f" (found placeholder {placeholder}; replace it with the real "
            "80-channel mel filterbank .npz)"
            if placeholder.is_file() else ""
        )
        raise _UserError(
            "ASSETS_NOT_INSTALLED",
            f"mel_filters.npz missing (expected at {mel}){hint}",
        )
    return mel


# --- Param validation -------------------------------------------------------

def _validate_params(params: dict[str, Any]) -> Tuple[bool]:
    """The English-only base.en model only supports task=transcribe and
    language=en. We accept and ignore other values rather than failing,
    but reject obviously invalid types so the user sees a clear error."""
    task = str(params.get("task", "transcribe"))
    if task not in ("transcribe", "translate"):
        raise _UserError(
            "INVALID_INPUT",
            f"params.task must be 'transcribe' or 'translate', got {task!r}",
        )
    if task == "translate":
        # base.en cannot translate (it is English-only); fail early so the
        # caller switches to the multilingual whisper-base model instead.
        raise _UserError(
            "INVALID_INPUT",
            "params.task='translate' is not supported by whisper_base_en "
            "(English-only). Use a multilingual whisper variant for translation.",
        )
    vad = bool(params.get("vad", True))   # accepted, not implemented in MVP
    return (vad,)


# --- Audio chunking (matches sample's chunk_and_resample_audio) -------------

def _chunk_audio_30s(samples: np.ndarray) -> List[Tuple[np.ndarray, float, float]]:
    """Split a 16 kHz mono signal into <=30 s windows.

    Returns a list of (chunk_samples, start_sec, end_sec). The encoder runs
    on full-length 30 s chunks; we leave the last (potentially short)
    chunk un-padded - log_mel_spectrogram pads to MAX_AUDIO_SAMPLES.
    """
    out: List[Tuple[np.ndarray, float, float]] = []
    if samples.size == 0:
        return out
    n_total = samples.shape[0]
    n_full = n_total // MAX_AUDIO_SAMPLES
    last_full = n_full * MAX_AUDIO_SAMPLES

    if n_full == 0:
        out.append((samples.astype(np.float32, copy=False), 0.0, n_total / SAMPLE_RATE))
        return out

    for i, seg in enumerate(np.array_split(samples[:last_full], n_full)):
        t0 = i * CHUNK_LENGTH
        out.append((seg.astype(np.float32, copy=False), float(t0), float(t0 + CHUNK_LENGTH)))

    tail = samples[last_full:]
    if tail.size:
        t0 = n_full * CHUNK_LENGTH
        out.append((tail.astype(np.float32, copy=False), float(t0), float(t0 + tail.size / SAMPLE_RATE)))
    return out


# --- Mel-spectrogram (numpy, replaces torch.stft to eliminate JIT cold-start)

# Pre-compute the Hann window once at module load (cheap, avoids per-call alloc)
_HANN_WINDOW = 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(N_FFT) / N_FFT).astype(np.float32)


def _log_mel_spectrogram(mel_filter: np.ndarray, audio_np: np.ndarray) -> np.ndarray:
    """Compute Whisper log-mel spectrogram using numpy only (no torch.stft).

    Numerically equivalent to the original torch.stft implementation:
      - Same Hann window, N_FFT=400, HOP_LENGTH=160
      - Power spectrum (magnitude^2), drop last frame to match torch output
      - log10 clamp + normalise identical to Whisper reference

    Output: float16 array shaped (1, N_MELS, n_frames).
    For a 30 s 16 kHz input padded to 480_000 samples: (1, 80, 3000).
    """
    # 1. Zero-pad to MAX_AUDIO_SAMPLES, then center-pad N_FFT//2 on each side
    #    (torch.stft uses center=True by default, which pads N_FFT//2 on both ends)
    audio = audio_np.astype(np.float32, copy=False)
    pad = MAX_AUDIO_SAMPLES - len(audio)
    if pad > 0:
        audio = np.pad(audio, (0, pad))
    center_pad = N_FFT // 2
    audio = np.pad(audio, (center_pad, center_pad), mode='reflect')

    # 2. Frame the signal: shape (n_frames, N_FFT)
    n_frames = 1 + (len(audio) - N_FFT) // HOP_LENGTH
    shape = (n_frames, N_FFT)
    strides = (audio.strides[0] * HOP_LENGTH, audio.strides[0])
    frames = np.lib.stride_tricks.as_strided(audio, shape=shape, strides=strides)

    # 3. Apply Hann window and compute real FFT
    windowed = frames * _HANN_WINDOW          # (n_frames, N_FFT)
    fft_out  = np.fft.rfft(windowed, n=N_FFT) # (n_frames, N_FFT//2+1) complex

    # 4. Power spectrum: (N_FFT//2+1, n_frames), drop last time frame
    #    to match torch.stft(...)[..., :-1]
    magnitudes = (fft_out.real ** 2 + fft_out.imag ** 2)  # (n_frames, N_FFT//2+1)
    magnitudes = magnitudes[:-1, :].T.astype(np.float32)   # (N_FFT//2+1, n_frames-1)

    # 5. Mel filterbank  →  (N_MELS, n_frames)
    mel_spec = mel_filter.astype(np.float32) @ magnitudes

    # 6. Log-compress + normalise (identical to Whisper reference)
    log_spec = np.log10(np.maximum(mel_spec, 1e-10))
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    log_spec = (log_spec + 4.0) / 4.0

    # 7. Add batch dim, cast to float16
    return log_spec[np.newaxis].astype(np.float16)


# --- Whisper timestamp rule (whisper/decoding.py L545) ----------------------

def _apply_timestamp_rules(logits: np.ndarray, tokens: list[int]):
    from scipy import special as scipy_special

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
    if timestamps:
        if last_was_timestamp and not penultimate_was_timestamp:
            timestamp_last = timestamps[-1]
        else:
            timestamp_last = timestamps[-1] + 1
        logits[TOKEN_TIMESTAMP_BEGIN:timestamp_last] = -np.inf

    if len(tokens) == SAMPLE_BEGIN:
        logits[:TOKEN_TIMESTAMP_BEGIN] = -np.inf
        last_allowed = TOKEN_TIMESTAMP_BEGIN + MAX_INITIAL_TIMESTAMP_INDEX
        logits[(last_allowed + 1):] = -np.inf

    logprobs = scipy_special.log_softmax(logits)
    timestamp_logprob = scipy_special.logsumexp(logprobs[TOKEN_TIMESTAMP_BEGIN:])
    max_text_token_logprob = logprobs[:TOKEN_TIMESTAMP_BEGIN].max()
    if timestamp_logprob > max_text_token_logprob:
        logits[:TOKEN_TIMESTAMP_BEGIN] = -np.inf

    return logits, logprobs


# --- Tokenizer (faster-whisper + tiktoken, fully offline) -------------------
#
# faster-whisper's Tokenizer class only needs three methods from the underlying
# tokenizers.Tokenizer object: encode / decode / token_to_id.  Rather than
# pulling in the full HuggingFace tokenizers pipeline (which requires a
# network-fetched tokenizer.json), we build a minimal duck-typed wrapper
# around tiktoken.Encoding loaded from the gpt2.tiktoken vocab file that ships
# inside the openai-whisper package — completely offline.
#
# Why not import whisper directly?
#   openai-whisper >= 20240930 added whisper/timing.py which does a hard
#   `import numba` at module load time.  numba requires llvmlite which has no
#   pre-built ARM64 Windows wheel, so `import whisper` fails on this platform.
#   This implementation avoids that import entirely.

class _TiktokenDecoder:
    """Minimal duck-type shim that satisfies faster_whisper.Tokenizer's
    interface using a tiktoken.Encoding built from the local gpt2.tiktoken
    vocab file bundled with openai-whisper."""

    def __init__(self, encoding) -> None:
        self._enc = encoding
        # Build reverse map for token_to_id (special tokens included)
        self._token_to_id: dict[str, int] = {}

    # --- faster_whisper.Tokenizer calls these three methods only ---

    def decode(self, ids: list[int]) -> str:
        # Filter out Whisper special tokens (>= TOKEN_EOT = 50256) before
        # passing to tiktoken — they are above the GPT-2 vocab range and
        # tiktoken raises KeyError on them.  The runner already strips SOT
        # (50257) before calling decode; timestamp tokens can appear when the
        # no-timestamp suppression fires late, so we drop them here.
        TEXT_TOKEN_LIMIT = 50256   # TOKEN_EOT; anything >= this is special
        text_ids = [t for t in ids if t < TEXT_TOKEN_LIMIT]
        if not text_ids:
            return ""
        # errors="replace" keeps the runner alive on rare out-of-vocab bytes
        return self._enc.decode(text_ids, errors="replace")

    def encode(self, text: str):
        return self._enc.encode(text)

    def token_to_id(self, token: str) -> int:
        if token in self._token_to_id:
            return self._token_to_id[token]
        raise KeyError(f"token not found: {token!r}")


def _load_tokenizer(pack_dir: Path):
    """Build a Whisper English tokenizer from the gpt2.tiktoken vocab file.

    Resolution order for the vocab file:
      1. pack_dir/assets/gpt2.tiktoken  (Pack-local copy, preferred)
      2. <openai-whisper site-packages>/whisper/assets/gpt2.tiktoken
    """
    import base64
    try:
        import tiktoken
    except ImportError as e:
        raise _UserError(
            "TOKENIZER_LOAD_ERROR",
            f"tiktoken is not installed: {e}. Run: pip install tiktoken",
        ) from e

    # --- locate gpt2.tiktoken ---
    candidates: list[Path] = [
        pack_dir / "assets" / "gpt2.tiktoken",
    ]
    # also probe the openai-whisper install (if present) without importing it
    try:
        import importlib.util as _ilu
        spec = _ilu.find_spec("whisper")
        if spec and spec.origin:
            candidates.append(Path(spec.origin).parent / "assets" / "gpt2.tiktoken")
    except Exception:
        pass

    vocab_path = next((c for c in candidates if c.is_file()), None)
    if vocab_path is None:
        raise _UserError(
            "TOKENIZER_LOAD_ERROR",
            "gpt2.tiktoken vocab file not found. Tried:\n  "
            + "\n  ".join(str(c) for c in candidates)
            + "\nInstall openai-whisper or place gpt2.tiktoken in "
              f"{pack_dir / 'assets'}.",
        )

    # --- parse tiktoken file (each line: '<base64_token> <rank>') ---
    try:
        ranks: dict[bytes, int] = {}
        for line in vocab_path.read_bytes().splitlines():
            if not line:
                continue
            token_b64, rank_s = line.split()
            ranks[base64.b64decode(token_b64)] = int(rank_s)
    except Exception as e:
        raise _UserError(
            "TOKENIZER_LOAD_ERROR",
            f"failed to parse {vocab_path}: {e}",
        ) from e

    # --- build tiktoken.Encoding (Whisper uses the GPT-2 BPE pattern) ---
    try:
        enc = tiktoken.Encoding(
            name="whisper_gpt2_offline",
            # GPT-2 split pattern (same as openai-whisper uses internally)
            pat_str=(
                r"'s|'t|'re|'ve|'m|'ll|'d"
                r"| ?\w+| ?\d+| ?[^\s\w\d]+"
                r"|\s+(?!\S)|\s+"
            ),
            mergeable_ranks=ranks,
            special_tokens={},   # Whisper special tokens are above vocab range;
                                 # runner decodes only text tokens (< TOKEN_EOT)
        )
    except Exception as e:
        raise _UserError(
            "TOKENIZER_LOAD_ERROR",
            f"tiktoken.Encoding construction failed: {e}",
        ) from e

    return _TiktokenDecoder(enc)


# --- Mel filterbank loader --------------------------------------------------

def _load_mel_filter(mel_npz_path: Path) -> np.ndarray:
    """Load the 80-channel Whisper mel filterbank from .npz (key 'mel_80')."""
    with np.load(str(mel_npz_path)) as data:
        return np.asarray(data[f"mel_{N_MELS}"], dtype=np.float32)


# --- OOM heuristic ----------------------------------------------------------

def _is_oom(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return ("memory" in msg and ("out" in msg or "alloc" in msg or "exhaust" in msg)) \
        or "oom" in msg


# --- Per-chunk transcription (mirrors transcribe_single_chunk) --------------

def _transcribe_chunk(
    encoder, decoder, tokenizer,
    mel_input: np.ndarray, sample_len: int,
    cancel_event: Optional[threading.Event] = None,
    wall_clock_deadline: Optional[float] = None,
    heartbeat_cb: Optional[Callable] = None,
) -> Tuple[str, float]:
    """Run the encoder once + greedy decode loop; return (text, mean_prob)."""
    # Encoder
    try:
        out = encoder.run([mel_input])
        if not isinstance(out, (list, tuple)) or len(out) < 2:
            raise _UserError("INFER_ERROR", "encoder returned unexpected output shape")
        k_cache_cross = np.asarray(out[0]).reshape(6, 8, 64, 1500)
        v_cache_cross = np.asarray(out[1]).reshape(6, 8, 1500, 64)
    except _UserError:
        raise
    except MemoryError as e:
        raise _UserError("OUT_OF_MEMORY", f"encoder OOM: {e}") from e
    except Exception as e:
        if _is_oom(e):
            raise _UserError("OUT_OF_MEMORY", str(e)) from e
        raise _UserError("INFER_ERROR", f"encoder.Inference failed: {e}") from e

    # Decoder loop (greedy, with timestamp rules + non-speech suppression)
    x = np.array([[TOKEN_SOT]], dtype=np.int32)
    decoded_tokens: list[int] = [TOKEN_SOT]
    k_cache_self = np.zeros((6, 8, 64, 224), dtype=np.float16)
    v_cache_self = np.zeros((6, 8, 224, 64), dtype=np.float16)
    sum_logprobs = 0.0
    step_count = 0
    _last_hb = time.monotonic()

    for i in range(sample_len):
        # -- Safety: timeout / cancel / heartbeat --
        now = time.monotonic()
        if wall_clock_deadline is not None and now > wall_clock_deadline:
            raise _UserError(
                "DECODE_TIMEOUT",
                f"whisper decode exceeded wall-clock limit of {MAX_DECODE_WALL_S:.0f}s",
            )
        if cancel_event is not None and cancel_event.is_set():
            break
        if heartbeat_cb is not None and now - _last_hb >= _HEARTBEAT_INTERVAL_S:
            heartbeat_cb("infer", (i / max(1, sample_len)) * 100.0,
                         message=f"decoding token {i}/{sample_len}")
            _last_hb = now
        # -- end safety --
        index = np.array([[i]], dtype=np.int32)
        try:
            out = decoder.run([x, index, k_cache_cross, v_cache_cross,
                               k_cache_self, v_cache_self])
            if not isinstance(out, (list, tuple)) or len(out) < 3:
                raise _UserError("INFER_ERROR", "decoder returned unexpected output shape")
            logits3      = np.asarray(out[0]).reshape(1, 1, 51864)
            k_cache_self = np.asarray(out[1]).reshape(6, 8, 64, 224)
            v_cache_self = np.asarray(out[2]).reshape(6, 8, 224, 64)
        except _UserError:
            raise
        except MemoryError as e:
            raise _UserError("OUT_OF_MEMORY", f"decoder OOM at step {i}: {e}") from e
        except Exception as e:
            if _is_oom(e):
                raise _UserError("OUT_OF_MEMORY", str(e)) from e
            raise _UserError("INFER_ERROR", f"decoder.Inference failed at step {i}: {e}") from e

        logits = logits3[0, -1].astype(np.float32, copy=False)
        # SuppressBlank on first step
        if i == 0:
            logits[[TOKEN_EOT, TOKEN_BLANK]] = -np.inf
        # SuppressTokens (non-speech)
        logits[NON_SPEECH_TOKENS] = -np.inf

        logits, logprobs = _apply_timestamp_rules(logits, decoded_tokens)

        if i == 0:
            no_speech_prob = float(np.exp(logprobs[TOKEN_NO_SPEECH]))
            if no_speech_prob > NO_SPEECH_THR:
                break

        next_token = int(np.argmax(logits))
        if next_token == TOKEN_EOT:
            break
        sum_logprobs += float(logprobs[next_token])
        step_count += 1

        x = np.array([[next_token]], dtype=np.int32)
        decoded_tokens.append(next_token)

    text = tokenizer.decode(decoded_tokens[1:]).strip()   # drop SOT
    if step_count > 0:
        mean_prob = float(np.exp(sum_logprobs / step_count))
    else:
        mean_prob = 0.0
    return text, max(0.0, min(1.0, mean_prob))


# --- Optional HTP burst-perf hint -------------------------------------------

def _set_perf_burst() -> Any:
    """Best-effort: switch HTP into BURST perf profile for the inference loop.
    Returns a token usable to reset later, or None if the API is unavailable.
    """
    try:
        import qai_appbuilder as qai   # type: ignore[import-not-found]
        PerfProfile = getattr(qai, "PerfProfile", None)
        if PerfProfile is None:
            return None
        burst = getattr(PerfProfile, "BURST", None)
        setter = getattr(PerfProfile, "SetPerfProfileGlobal", None)
        if burst is None or not callable(setter):
            return None
        import time as _time
        t0 = _time.monotonic()
        setter(burst)
        elapsed_ms = (_time.monotonic() - t0) * 1000
        print(f"[perf-diag] SetPerfProfileGlobal(BURST) took {elapsed_ms:.1f}ms", file=sys.stderr, flush=True)
        return PerfProfile
    except Exception:   # pylint: disable=broad-except
        return None


def _release_perf_burst(token: Any) -> None:
    if token is None:
        return
    try:
        rel = getattr(token, "RelPerfProfileGlobal", None)
        if callable(rel):
            import time as _time
            t0 = _time.monotonic()
            rel()
            elapsed_ms = (_time.monotonic() - t0) * 1000
            print(f"[perf-diag] RelPerfProfileGlobal took {elapsed_ms:.1f}ms", file=sys.stderr, flush=True)
    except Exception:   # pylint: disable=broad-except
        pass


# --- Model context (persistent worker mode) ---------------------------------

@dataclass
class ModelContext:
    """Holds loaded Whisper resources for reuse across inferences.

    Populated once by ``load_model()`` and consumed by ``run_inference()``;
    ``release_model()`` tears the QNN contexts down.
    """
    encoder: Any                       # qnn_helper.QnnContext
    decoder: Any                       # qnn_helper.QnnContext
    tokenizer: _TiktokenDecoder
    mel_filter: np.ndarray             # (N_MELS, N_FFT//2) float32
    encoder_path: Path
    decoder_path: Path
    mel_path: Path
    pack_dir: Path
    repo_root: Path
    _contexts: list = field(default_factory=list)   # raw refs, used by release_model
    load_stages: list = field(default_factory=list) # one-time load timings
    perf_token: Any = None                           # HTP BURST token; held resident across the model lifecycle


# --- Persistent-worker API: load_model / run_inference / release_model ------

def load_model(cmd: dict) -> ModelContext:
    """Load encoder + decoder QNN contexts and CPU-side assets.

    This function MUST NOT touch the runner protocol (no read_request /
    status / done / fail).  It is meant to be invoked once by the sticky
    worker host (or by ``main()`` in one-shot mode) and then reused across
    many ``run_inference`` calls.

    Args:
        cmd: command/request dict.  Recognised keys:
             - ``repoRoot``    (str, optional) — workspace root
             - ``packDir``     (str, optional) — pack directory
             - ``modelDir``    (str, optional) — overrides repoRoot/models/...
             - ``modelId`` / ``variantId`` / ``variantContextBins`` —
               accepted for protocol parity, currently unused (whisper-base
               has a single variant with fixed encoder/decoder filenames).

    Returns:
        ModelContext holding all loaded resources.
    """
    repo_root = _resolve_repo_root(cmd)
    pack_dir = _resolve_pack_dir(cmd, repo_root)

    # Allow the caller to pin an explicit model directory; if it does not
    # contain the expected .bin files we fall back to the default search.
    raw_model_dir = cmd.get("modelDir")
    encoder_path: Optional[Path] = None
    decoder_path: Optional[Path] = None
    if raw_model_dir:
        md = Path(raw_model_dir)
        if not md.is_absolute():
            md = (Path.cwd() / md).resolve()
        if md.is_dir():
            enc_cand = (md / ENCODER_FILENAME).resolve()
            dec_cand = (md / DECODER_FILENAME).resolve()
            if enc_cand.is_file() and dec_cand.is_file():
                encoder_path, decoder_path = enc_cand, dec_cand

    if encoder_path is None or decoder_path is None:
        encoder_path, decoder_path = _resolve_weights(repo_root, pack_dir)

    mel_path = _resolve_mel_filter(pack_dir)

    # CPU-side assets: tokenizer + mel filterbank.  These are cheap relative
    # to NPU context creation but we still time them so the metrics surfaced
    # on the first run reflect real cold-start cost.
    _t_tok0 = time.time()
    tokenizer = _load_tokenizer(pack_dir)
    _tok_ms = (time.time() - _t_tok0) * 1000

    _t_mel0 = time.time()
    mel_filter = _load_mel_filter(mel_path)
    _mel_ms = (time.time() - _t_mel0) * 1000

    # NPU contexts via shared/qnn_helper.  Map low-level errors to the same
    # _UserError codes the original runner used so downstream tools see no
    # behavioural change.
    try:
        from qnn_helper import QnnContext   # noqa: WPS433
    except Exception as e:
        raise _UserError(
            "QAI_APPBUILDER_UNAVAILABLE",
            f"qnn_helper import failed: {e}. Ensure shared/ is on PYTHONPATH.",
        ) from e

    contexts: list = []

    _t_enc0 = time.time()
    try:
        encoder_ctx = QnnContext.load(
            encoder_path,
            runtime="Htp", log_level=1,
            input_data_type="native",
            output_data_type="native",
        )
    except FileNotFoundError as e:
        raise _UserError("WEIGHTS_NOT_INSTALLED", str(e)) from e
    except NotImplementedError as e:
        raise _UserError("QAI_APPBUILDER_UNAVAILABLE", str(e)) from e
    except Exception as e:
        if _is_oom(e):
            raise _UserError("OUT_OF_MEMORY", str(e)) from e
        raise _UserError("INFER_ERROR", f"failed to load encoder: {e}") from e
    contexts.append(encoder_ctx)
    _enc_ms = (time.time() - _t_enc0) * 1000

    _t_dec0 = time.time()
    try:
        decoder_ctx = QnnContext.load(
            decoder_path,
            runtime="Htp", log_level=1,
            input_data_type="native",
            output_data_type="native",
        )
    except FileNotFoundError as e:
        # encoder already loaded — release before bubbling up
        try:
            encoder_ctx.close()
        except Exception:
            pass
        raise _UserError("WEIGHTS_NOT_INSTALLED", str(e)) from e
    except NotImplementedError as e:
        try:
            encoder_ctx.close()
        except Exception:
            pass
        raise _UserError("QAI_APPBUILDER_UNAVAILABLE", str(e)) from e
    except Exception as e:
        try:
            encoder_ctx.close()
        except Exception:
            pass
        if _is_oom(e):
            raise _UserError("OUT_OF_MEMORY", str(e)) from e
        raise _UserError("INFER_ERROR", f"failed to load decoder: {e}") from e
    contexts.append(decoder_ctx)
    _dec_ms = (time.time() - _t_dec0) * 1000

    load_stages = [
        {"name": "load_tokenizer", "latencyMs": round(_tok_ms, 2)},
        {"name": "load_mel_filter", "latencyMs": round(_mel_ms, 2)},
        {"name": "load_encoder", "latencyMs": round(_enc_ms, 2),
         "model": ENCODER_FILENAME},
        {"name": "load_decoder", "latencyMs": round(_dec_ms, 2),
         "model": DECODER_FILENAME},
    ]

    # HTP BURST is made resident for the whole model lifecycle (Plan A):
    # set once here after all contexts are created, released once in
    # release_model. This avoids ramping the HTP clock up/down on every
    # run_inference for streaming/session workloads (many audio chunks).
    perf_token = _set_perf_burst()
    print(f"[whisper-diag] perf_burst: {'SET' if perf_token else 'UNAVAILABLE'}", file=sys.stderr, flush=True)

    return ModelContext(
        encoder=encoder_ctx,
        decoder=decoder_ctx,
        tokenizer=tokenizer,
        mel_filter=mel_filter,
        encoder_path=encoder_path,
        decoder_path=decoder_path,
        mel_path=mel_path,
        pack_dir=pack_dir,
        repo_root=repo_root,
        _contexts=contexts,
        load_stages=load_stages,
        perf_token=perf_token,
    )


def run_inference(model_ctx: ModelContext, cmd: dict) -> None:
    """Transcribe an audio file using already-loaded models.

    Emits the full event stream (status / metrics / result) but does NOT
    call ``done()`` — the worker host owns the lifecycle event for sticky
    mode.  ``main()`` calls ``done()`` itself in one-shot mode.

    Args:
        model_ctx: ModelContext returned by ``load_model()``.
        cmd: per-run command dict carrying ``inputs``, ``params``, options.
             ``repoRoot``/``packDir`` from the load command are reused if
             the run command omits them.
    """
    # Path resolution — fall back to the load-time values so a sticky worker
    # that is given a minimal run command still works.
    repo_root = _resolve_repo_root(cmd) if cmd.get("repoRoot") else model_ctx.repo_root
    pack_dir = (
        _resolve_pack_dir(cmd, repo_root) if cmd.get("packDir") else model_ctx.pack_dir
    )

    audio_path = _resolve_input_audio(cmd, repo_root, pack_dir)
    (_vad,) = _validate_params(cmd.get("params") or {})

    status("running")

    # Audio decode + resample to 16 kHz mono.
    try:
        from audio_io import read_audio   # type: ignore[import-not-found]
    except ImportError as e:
        raise _UserError(
            "INFER_ERROR",
            f"audio_io import failed: {e}. Ensure shared/ is on PYTHONPATH.",
        ) from e
    try:
        samples, sr = read_audio(audio_path, target_sample_rate=SAMPLE_RATE, mono=True)
    except FileNotFoundError as e:
        raise _UserError("INVALID_INPUT", str(e)) from e
    except Exception as e:
        raise _UserError("AUDIO_DECODE_ERROR", f"failed to decode audio {audio_path}: {e}") from e
    if samples.ndim != 1:
        samples = np.asarray(samples).reshape(-1).astype(np.float32, copy=False)
    duration_s = float(samples.shape[0]) / float(sr)
    if duration_s > MAX_AUDIO_SEC:
        raise _UserError(
            "AUDIO_TOO_LONG",
            f"audio duration {duration_s:.1f}s exceeds the {MAX_AUDIO_SEC:.0f}s limit "
            "set by the manifest. Trim the file or split externally.",
        )

    timer = StageTimer(device="htp")

    sample_len = MAX_DECODE_TOKENS
    cancel_event = cmd.get("_cancel_event")
    wall_clock_deadline = time.monotonic() + MAX_DECODE_WALL_S

    chunks = _chunk_audio_30s(samples)
    segments: list[dict[str, Any]] = []
    full_text_parts: list[str] = []

    encoder = model_ctx.encoder
    decoder = model_ctx.decoder
    tokenizer = model_ctx.tokenizer
    mel_filter = model_ctx.mel_filter

    # HTP BURST is now resident for the whole model lifecycle (set in
    # load_model, released in release_model), so run_inference no longer
    # toggles the perf profile per run.
    try:
        for chunk_idx, (chunk_samples, t_start, t_end) in enumerate(chunks):
            progress("infer", (chunk_idx / max(1, len(chunks))) * 100.0,
                     message=f"transcribing chunk {chunk_idx+1}/{len(chunks)}")
            # Pre-process: log-mel spectrogram (CPU).
            with timer.stage("preprocess", accumulate=True):
                try:
                    mel = _log_mel_spectrogram(mel_filter, chunk_samples)
                except Exception as e:
                    raise _UserError(
                        "INFER_ERROR",
                        f"mel-spectrogram preprocess failed (chunk {chunk_idx}): {e}",
                    ) from e
            # Inference: encoder + decoder on QNN HTP.
            # mel has shape (1, 80, 3000) float16. Some encoder exports want
            # the leading batch dim, others want (80, 3000); we pass the
            # canonical (1, 80, 3000) and let qai_appbuilder reshape.
            with timer.stage("infer", accumulate=True):
                text, conf = _transcribe_chunk(
                    encoder, decoder, tokenizer, mel, sample_len,
                    cancel_event=cancel_event,
                    wall_clock_deadline=wall_clock_deadline,
                    heartbeat_cb=progress,
                )

            seg = {
                "start": round(float(t_start), 3),
                "end":   round(float(t_end),   3),
                "text":  text,
                "conf":  round(conf, 4),
            }
            segments.append(seg)
            if text:
                full_text_parts.append(text)
    except _UserError:
        raise
    except MemoryError as e:
        raise _UserError("OUT_OF_MEMORY", str(e)) from e
    except Exception as e:
        if _is_oom(e):
            raise _UserError("OUT_OF_MEMORY", str(e)) from e
        raise _UserError("INFER_ERROR", f"inference loop failed: {e}") from e

    with timer.stage("postprocess"):
        full_text = " ".join(full_text_parts)

    metrics_payload: dict[str, Any] = {
        "type": "metrics",
        **timer.summary(),
        "segment_count":  len(segments),
        "total_audio_s":  round(duration_s, 3),
        "language":       "en",
    }
    # Attach the one-time load timings only on the FIRST inference after
    # load_model. The sticky worker reuses the same ModelContext across many
    # run_inference calls — clearing load_stages here ensures the metric is
    # surfaced exactly once (the cold-start cost) rather than redundantly
    # appearing in every subsequent fast-path inference.
    if model_ctx.load_stages:
        metrics_payload["loadStages"] = model_ctx.load_stages
        model_ctx.load_stages = []
    emit(metrics_payload)

    result({
        "language": "en",
        "task":     "transcribe",
        "fullText": full_text,
        "segments": segments,
    })


def release_model(model_ctx: ModelContext) -> None:
    """Tear down all QNN contexts held by ``model_ctx``.

    Calls ``close()`` on every raw context (idempotent in qnn_helper) and
    nulls out the public attributes so the GC can reclaim everything.
    Errors during teardown are swallowed — the worker is shutting down
    and surfacing them would only mask the original failure.
    """
    # Release the resident HTP BURST profile BEFORE contexts are destroyed
    # (mirrors the set at the end of load_model). Plan A: BURST tracks the
    # model's resident lifecycle rather than each individual run.
    _release_perf_burst(getattr(model_ctx, "perf_token", None))
    model_ctx.perf_token = None

    for ctx in list(model_ctx._contexts):
        close = getattr(ctx, "close", None)
        if callable(close):
            try:
                close()
            except Exception:   # pylint: disable=broad-except
                pass
    model_ctx._contexts.clear()
    model_ctx.encoder = None      # type: ignore[assignment]
    model_ctx.decoder = None      # type: ignore[assignment]
    # Drop bulky CPU-side caches too so a long-running worker does not
    # retain mel filters / tokenizer pages after the model is unloaded.
    model_ctx.tokenizer = None    # type: ignore[assignment]
    model_ctx.mel_filter = None   # type: ignore[assignment]


# --- Main (one-shot entry, sticky-mode-compatible) --------------------------

def main() -> None:
    """One-shot entrypoint: load → infer → release.

    Sticky-worker hosts call ``load_model`` / ``run_inference`` /
    ``release_model`` themselves; this function only runs when the
    runner is launched as a stand-alone subprocess.
    """
    req = read_request()

    status("preparing")
    model_ctx = load_model(req)

    try:
        run_inference(model_ctx, req)
    finally:
        release_model(model_ctx)


# --- Entrypoint with structured failure handling ----------------------------

if __name__ == "__main__":
    try:
        main()
        done()
        sys.exit(0)
    except SystemExit:
        raise
    except _UserError as ue:
        fail(code=ue.code, message=ue.message)
        sys.exit(1)
    except Exception as e:   # pylint: disable=broad-except
        fail(
            code="INFER_ERROR",
            message=str(e),
            traceback=traceback.format_exc(limit=20),
        )
        sys.exit(1)
