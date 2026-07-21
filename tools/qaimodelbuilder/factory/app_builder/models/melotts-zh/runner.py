#!/usr/bin/env python
# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
melotts-zh · App Builder Pack runner (v2.0, full-NPU pipeline)
==============================================================

QNN HTP Chinese text-to-speech runner using the REAL MeloTTS-ZH full-NPU
inference pipeline. All 4 neural network stages run on NPU via qai_appbuilder:
  - NPU bert_wrapper.bin: BERT hidden-state extraction [1,200,768]
  - NPU encoder.bin: text encoding + duration prediction (enc_p + SDP + DP)
  - NPU flow.bin (or flow_short.bin): flow reverse pass (z_p → z)
  - NPU decoder.bin: streaming waveform generation (T=128 chunks)

CPU-only operations: G2P (jieba + pypinyin + cn2an) + attention alignment (numpy).

Implements the single-line JSON request → line-JSON event protocol defined in
``shared/runner_protocol.py`` (plan v3.1 · C★.6, R.5).

Hard isolation rule:
    NO module under ``features.model_builder`` is imported here. This runner
    is self-contained using only shared/ utilities, pack-local files, and
    qai_appbuilder.
"""

from __future__ import annotations

import json
import os
import sys
import time
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# NOTE: stdout protection (fd-level redirect to prevent native library stdout
# pollution from corrupting JSON events) is handled by _runner_bootstrap.py
# BEFORE this runner.py is loaded. Pack runners do NOT need to implement any
# stdout protection themselves.

# Ensure the Pack directory (where runner.py lives) is on sys.path so that
# pack-local modules (bert_tokenizer_local, melo_zh_local, etc.) can be imported.
_THIS_DIR = str(Path(__file__).resolve().parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

# ── Vendored NLTK data ───────────────────────────────────────────────────────
# scripts/setup/predeploy_tts_runtime.py (run by Setup.bat) populates
# <repo_root>/vendor/nltk_data/ with averaged_perceptron_tagger[_eng] and
# cmudict.  Prepend that directory to ``nltk.data.path`` BEFORE any code path
# that may import g2p_en (which itself imports nltk and triggers a lookup).
# Without this injection, the first inference on a fresh machine would either
# block on a network download or hard-fail offline.
#
# We use a fixed relative jump (parents[4]) instead of waiting for the JSON
# request that carries ``repoRoot``, because the G2P warm-up thread in
# ``main()`` is started in parallel with NPU model loading and may begin
# importing g2p_en immediately — by then the request has already been read,
# but routing through it would couple two unrelated pieces of state.  The
# fixed path keeps this guard pure and side-effect-free.
try:
    import nltk as _nltk  # type: ignore[import-not-found]
    _VENDOR_NLTK = (
        Path(__file__).resolve().parents[4] / "vendor" / "nltk_data"
    )
    if _VENDOR_NLTK.is_dir():
        _vendor_nltk_str = str(_VENDOR_NLTK)
        if _vendor_nltk_str not in _nltk.data.path:
            _nltk.data.path.insert(0, _vendor_nltk_str)
except ImportError:
    # nltk is a transitive dep of g2p_en; if it's missing here the runner
    # will still import, and the real failure will be reported by the G2P
    # warm-up step with a clear FRONTEND_DEP_MISSING error.
    pass

import numpy as np

# shared/ is injected on PYTHONPATH by backend.app_builder.runners.python_script
from runner_protocol import (
    emit, read_request, status, progress, metrics, result, done, fail,
)
from telemetry import StageTimer

# ── Constants ────────────────────────────────────────────────────────────────

SAMPLE_RATE = 44100
MAX_SEQ_LEN = 512
UPSAMPLED_MAX_SEQ_LEN = 1536  # MAX_SEQ_LEN * 3
DECODER_Z_TIME_DIM = 64
UPSAMPLE_FACTOR = 512
BERT_MAX_LEN = 200

# Decoder streaming parameters for decoder.bin T=64
# metadata.json: decoder z input [1, 192, 64], audio output [1, 1, 32768]
DEC_MAIN_LEN = 40
DEC_OVERLAP = 12

# Short-flow fast path thresholds
FLOW_SHORT_S = 256
FLOW_SHORT_US = 768

# Decoder T presets: T → (main_len, max_overlap)
DECODER_T_PRESETS: dict[int, tuple[int, int]] = {
    64: (40, 12),
    128: (104, 12),
    192: (168, 12),
    256: (232, 12),
}

MAX_INPUT_CHARS = 500
SPEAKER_ID = 1  # ZH speaker from spk2id

# Safety: wall-clock timeout and heartbeat interval for the decoder loop
MAX_DECODE_WALL_S = 180.0              # 3 minutes wall-clock timeout
_HEARTBEAT_INTERVAL_S = 30.0           # progress output every 30 s

THIS_DIR = Path(__file__).resolve().parent


# ── Domain errors ────────────────────────────────────────────────────────────

class _RunnerError(Exception):
    """Structured user-visible error with an error-event ``code``."""
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ── Helpers ──────────────────────────────────────────────────────────────────

def intersperse(lst: list, item) -> list:
    """Insert item between every element and at both ends."""
    result_list = [item] * (len(lst) * 2 + 1)
    result_list[1::2] = lst
    return result_list


def build_y_mask(y_lengths: int, max_len: int) -> np.ndarray:
    """Build y_mask of shape (1, 1, max_len), float32."""
    arange = np.arange(max_len, dtype=np.int64)
    mask = (arange < y_lengths).astype(np.float32)
    return mask.reshape(1, 1, -1)


def generate_attn_squeezed(
    w_ceil: np.ndarray, x_mask: np.ndarray, y_mask: np.ndarray
) -> np.ndarray:
    """Reproduce generate_path in numpy.

    Args:
        w_ceil: (1, 1, T_x) duration per phone
        x_mask: (1, 1, T_x) phone mask
        y_mask: (1, 1, T_y) frame mask

    Returns: attn_squeezed (1, T_y, T_x), float32.
    """
    duration = w_ceil
    attn_mask = (
        x_mask[:, :, np.newaxis, :].astype(np.float32)
        * y_mask[:, :, :, np.newaxis].astype(np.float32)
    )
    b, _, t_y, t_x = attn_mask.shape

    cum_duration = np.cumsum(duration, axis=-1).reshape(b * t_x)
    x = np.arange(t_y, dtype=cum_duration.dtype)
    path = (x[None, :] < cum_duration[:, None]).astype(attn_mask.dtype)
    path = path.reshape(b, t_x, t_y)

    pad_path = np.pad(path, ((0, 0), (1, 0), (0, 0)), mode="constant")[:, :-1]
    path = path - pad_path

    attn = path[:, np.newaxis, :, :].transpose(0, 1, 3, 2) * attn_mask
    return attn.squeeze(1).astype(np.float32)


# ── QNN context wrappers ─────────────────────────────────────────────────────

def _import_qai():
    """Import qai_appbuilder; raise _RunnerError if unavailable."""
    try:
        from qai_appbuilder import (
            DataType,
            LogLevel,
            PerfProfile,
            ProfilingLevel,
            QNNConfig,
            QNNContext,
            Runtime,
        )
        return DataType, LogLevel, PerfProfile, ProfilingLevel, QNNConfig, QNNContext, Runtime
    except ImportError as e:
        raise _RunnerError(
            "QAI_APPBUILDER_UNAVAILABLE",
            f"qai_appbuilder import failed: {e}. "
            "Ensure qai_appbuilder is installed in the ARM64 venv.",
        ) from e


class _BertWrapper:
    """NPU BERT wrapper: input_ids/token_type_ids/attention_mask [1,200] → hidden [1,200,768]."""

    def __init__(self, ctx):
        self.ctx = ctx

    def infer(self, input_ids: np.ndarray, token_type_ids: np.ndarray,
              attention_mask: np.ndarray) -> np.ndarray:
        outputs = self.ctx.Inference([input_ids, token_type_ids, attention_mask])
        return outputs[0]  # [1, 200, 768]


class _Encoder:
    """NPU Encoder (enc_p + SDP + DP).

    Input order (per metadata.json):
        sid, bert, ja_bert, x, tone, language, x_lengths, noise_scale_w, sdp_ratio, length_scale
    Outputs: m_p, logs_p, w_ceil, y_lengths, x_mask, g
    """

    def __init__(self, ctx):
        self.ctx = ctx

    def infer(self, x, x_lengths, tone, sid, language, bert, ja_bert,
              sdp_ratio, length_scale, noise_scale_w):
        # Order must match metadata.json inputs:
        # sid, bert, ja_bert, x, tone, language, x_lengths, noise_scale_w, sdp_ratio, length_scale
        outputs = self.ctx.Inference([
            sid, bert, ja_bert, x, tone, language,
            x_lengths, noise_scale_w, sdp_ratio, length_scale,
        ])
        return outputs  # [m_p, logs_p, w_ceil, y_lengths, x_mask, g]


class _Flow:
    """NPU Flow reverse pass.

    Inputs: attn_squeezed, logs_p, noise_scale, m_p, y_mask, g
    Output: z [1, 192, 1536]
    """

    def __init__(self, ctx):
        self.ctx = ctx

    def infer(self, attn_squeezed, logs_p, noise_scale, m_p, y_mask, g):
        outputs = self.ctx.Inference(
            [attn_squeezed, logs_p, noise_scale, m_p, y_mask, g]
        )
        return outputs[0]


class _FlowShort:
    """Short-path flow: phone_slot=256, frame_slot=768.

    Input order discovered via getInputName(); fallback order:
        m_p, logs_p, y_mask, attn_squeezed, g, noise_scale
    """

    INPUT_KEYS = ("m_p", "logs_p", "y_mask", "attn_squeezed", "g", "noise_scale")

    def __init__(self, ctx):
        self.ctx = ctx

    def infer(self, m_p, logs_p, y_mask, attn_squeezed, g, noise_scale):
        bag = {
            "m_p": m_p,
            "logs_p": logs_p,
            "y_mask": y_mask,
            "attn_squeezed": attn_squeezed,
            "g": g,
            "noise_scale": noise_scale,
        }
        order = self.ctx.getInputName()
        if order and all(k in bag for k in order):
            payload = [bag[k] for k in order]
        else:
            payload = [bag[k] for k in self.INPUT_KEYS]
        outputs = self.ctx.Inference(payload)
        return outputs[0]


class _Decoder:
    """NPU Decoder (HiFi-GAN vocoder).

    Inputs: g [1,256,1], z [1,192,T]  (or [z,g] depending on .bin variant)
    Output: audio [1, 1, T*512]
    """

    def __init__(self, ctx):
        self.ctx = ctx
        # Probe input order once
        names = self.ctx.getInputName()
        self._z_first = names and names[0] == "z"

    def infer(self, g: np.ndarray, z: np.ndarray) -> np.ndarray:
        if self._z_first:
            outputs = self.ctx.Inference([z, g])
        else:
            outputs = self.ctx.Inference([g, z])
        return outputs[0]


# ── Model context (persistent worker mode) ───────────────────────────────────

@dataclass
class ModelContext:
    """Holds loaded NPU models and G2P dependencies for reuse across inferences."""
    bert_wrapper: _BertWrapper
    encoder: _Encoder
    flow: _Flow
    flow_short: Optional[_FlowShort]
    decoder: _Decoder
    bert_tokenizer: Any
    symbol_to_id: dict
    model_dir: Path
    repo_root: Path
    _contexts: list  # raw QNNContext refs for release
    load_stages: list = None  # type: ignore[assignment]  # StageTimer stages from load_model
    perf_burst_active: bool = False  # HTP BURST held resident across the model lifecycle


# ── Path resolution ──────────────────────────────────────────────────────────

# Required NPU context-binary files (must all be present for inference).
REQUIRED_MODEL_FILES: tuple[str, ...] = (
    "encoder.bin", "flow.bin", "decoder.bin", "bert_wrapper.bin",
)

# Optional model files (auto-downloaded if present in the zip, but inference
# falls back to the long flow path without flow_short.bin).
OPTIONAL_MODEL_FILES: tuple[str, ...] = (
    "flow_short.bin", "bert_zh_tokenizer.bin", "bert_normalizer.bin",
    "metadata.json",
)

# edition-dual-form §4.2: melotts_zh ships a downloader for the first time in
# V2 (mirroring whisper_medium / zipformer). The download metadata (tag /
# required+optional file lists / per-device zip URLs) now lives in the sibling
# ``weights.json`` (single source of truth), loaded by RELATIVE PATH because
# ``factory/`` is not a package — runners reach shared modules via sys.path
# injection. This makes the SAME metadata + extraction reusable by both this
# runner subprocess and a future API-side downloader. Platform is picked via
# shared CPU detection (x_elite / x2_elite). The download routes through the
# global proxy when configured (HTTPS_PROXY / ALL_PROXY injected by the
# apps/api wiring root — 缺口 10). The REQUIRED_/OPTIONAL_MODEL_FILES constants
# above remain for the inference path and MUST stay in sync with weights.json
# (asserted by test).
_WEIGHTS_CONFIG_PATH = Path(__file__).resolve().parent / "weights.json"


def _load_weights_config() -> dict:
    """Load + parse this pack's sibling ``weights.json`` (download metadata).

    Returns the parsed dict with ``tag`` / ``required_files`` /
    ``optional_files`` / ``download_configs``. Raises ``_RunnerError`` with a
    clear message if the file is missing or unparseable.
    """
    try:
        with open(_WEIGHTS_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise _RunnerError(
            "WEIGHTS_NOT_INSTALLED",
            f"failed to load weights config {_WEIGHTS_CONFIG_PATH}: {e}. "
            "Ensure the pack ships weights.json alongside runner.py, or "
            "manually place the .bin files at the model dir.",
        ) from e


def _ensure_weights_downloaded(model_dir: Path) -> None:
    """Download + extract melotts_zh weights into ``model_dir`` if missing.

    Thin wrapper over the shared ``weight_downloader.ensure_weights_downloaded``
    helper; re-raises its structured failure as
    ``_RunnerError("WEIGHTS_NOT_INSTALLED")`` so the SSE error event maps to
    ``voiceInput.weightsMissing`` on the frontend. Idempotent: a re-entry
    with all 4 required .bin files present never touches the network.
    Hard-constraint ②: a download failure never crashes the app.

    Download metadata (tag / file lists / per-device URLs) comes from the
    pack's ``weights.json`` (single source of truth).
    """
    try:
        from weight_downloader import (  # type: ignore[import-not-found]
            WeightDownloadError,
            ensure_weights_downloaded,
        )
    except ImportError as e:
        raise _RunnerError(
            "WEIGHTS_NOT_INSTALLED",
            f"weight_downloader import failed: {e}. Ensure shared/ is on "
            f"PYTHONPATH, or manually place the .bin files at {model_dir}.",
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
        raise _RunnerError("WEIGHTS_NOT_INSTALLED", e.message) from e


def _resolve_repo_root(req: dict[str, Any]) -> Path:
    raw = req.get("repoRoot") or "."
    p = Path(raw)
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    if p.is_dir():
        return p
    return Path(__file__).resolve().parents[4]


def _resolve_model_dir(repo_root: Path) -> Path:
    """Locate the NPU model directory containing the .bin files.

    Search order: canonical ``<repo>/models/melotts-zh/`` -> pack-local
    ``weights/``. When neither directory exists yet (e.g. fresh checkout), we
    return the canonical path so the auto-downloader can populate it; the
    follow-on ``_validate_model_files`` triggers the download.
    """
    # Primary: <repo_root>/models/melotts-zh/
    model_dir = repo_root / "models" / "melotts-zh"
    if model_dir.is_dir():
        return model_dir
    # Fallback: pack-local weights/
    local_weights = THIS_DIR / "weights"
    if local_weights.is_dir():
        return local_weights
    # Neither exists yet -> return canonical so the downloader can mkdir
    # and populate. (V1 raised here; we now defer to _validate_model_files,
    # which will either succeed via auto-download or raise the structured
    # WEIGHTS_NOT_INSTALLED itself.)
    return model_dir


def _validate_model_files(model_dir: Path) -> dict[str, Path]:
    """Validate required model .bin files exist. Returns paths dict.

    When one or more REQUIRED files are missing, attempts a lazy auto-download
    of the device-specific melotts_zh zip into ``model_dir`` (idempotent;
    routes through the global proxy when configured — 缺口 10) before giving
    up. On download failure raises ``WEIGHTS_NOT_INSTALLED`` (hard-constraint
    ②: never crashes the app).
    """
    required = list(REQUIRED_MODEL_FILES)

    def _missing(d: Path) -> list[str]:
        return [name for name in required if not (d / name).is_file()]

    # Auto-download fallback when any required NPU bin is missing.
    if _missing(model_dir):
        _ensure_weights_downloaded(model_dir)

    paths: dict[str, Path] = {}
    missing = []
    for name in required:
        p = model_dir / name
        if not p.is_file():
            missing.append(name)
        else:
            paths[name.replace(".bin", "")] = p

    if missing:
        raise _RunnerError(
            "WEIGHTS_NOT_INSTALLED",
            f"Missing required model files in {model_dir}: {', '.join(missing)}. "
            "Run Setup.bat, or check network connectivity / proxy settings "
            "and re-run.",
        )

    # Optional flow_short
    flow_short_path = model_dir / "flow_short.bin"
    if flow_short_path.is_file():
        paths["flow_short"] = flow_short_path

    return paths


# ── Input validation ─────────────────────────────────────────────────────────

def _validate_inputs(req: dict[str, Any]) -> str:
    """Validate and return the input text."""
    inputs = req.get("inputs") or {}
    text_raw = inputs.get("text")
    if text_raw is None:
        raise _RunnerError("INVALID_INPUT", "inputs.text is required (Chinese text to synthesize)")
    if not isinstance(text_raw, str):
        raise _RunnerError(
            "INVALID_INPUT",
            f"inputs.text must be a string, got {type(text_raw).__name__}",
        )
    text = text_raw.strip()
    if not text:
        raise _RunnerError("INVALID_INPUT", "inputs.text is empty after trimming")
    if len(text) > MAX_INPUT_CHARS:
        raise _RunnerError(
            "INVALID_INPUT",
            f"inputs.text length {len(text)} exceeds {MAX_INPUT_CHARS} chars. "
            "Split the text into multiple synthesis requests.",
        )
    return text


def _get_params(req: dict[str, Any]) -> dict[str, Any]:
    """Extract synthesis parameters with defaults."""
    params = req.get("params") or {}
    return {
        "noise_scale": float(params.get("noise_scale", 0.667)),
        "length_scale": float(params.get("length_scale", 1.0)),
        "noise_scale_w": float(params.get("noise_scale_w", 0.8)),
        "sdp_ratio": float(params.get("sdp_ratio", 0.2)),
        "speed": float(params.get("speed", 1.0)),
    }


# ── G2P frontend ─────────────────────────────────────────────────────────────

def _run_g2p(text: str, symbol_to_id: dict[str, int]) -> tuple:
    """Run the G2P pipeline: text → phone_ids, tone_ids, lang_ids, word2ph, norm_text.

    Uses the local melo_zh_local package (placed alongside runner.py).
    """
    try:
        from melo_zh_local import clean_text, cleaned_text_to_sequence
    except ImportError as e:
        raise _RunnerError(
            "FRONTEND_DEP_MISSING",
            f"melo_zh_local import failed: {e}. Ensure the melo_zh_local/ package "
            "is placed alongside runner.py.",
        ) from e

    try:
        norm_text, phones_list, tones_list, word2ph = clean_text(text, "ZH")

        # Convert to integer sequences (ZH → ZH_MIX_EN internally, lang_id=3)
        phone_ids, tone_ids, lang_ids = cleaned_text_to_sequence(
            phones_list, tones_list, "ZH", symbol_to_id
        )

        # Apply intersperse with blank (0)
        phone_ids = intersperse(phone_ids, 0)
        tone_ids = intersperse(tone_ids, 0)
        lang_ids = intersperse(lang_ids, 0)  # produces [0, 3, 0, 3, ...] pattern

        # Adjust word2ph for intersperse (each count doubles, first +1)
        for i in range(len(word2ph)):
            word2ph[i] = word2ph[i] * 2
        word2ph[0] += 1

        return phone_ids, tone_ids, lang_ids, word2ph, norm_text

    except _RunnerError:
        raise
    except Exception as e:
        raise _RunnerError(
            "FRONTEND_ERROR",
            f"G2P text frontend failed: {type(e).__name__}: {e}",
        ) from e


# ── BERT feature extraction (NPU) ───────────────────────────────────────────

def _run_bert_npu(
    bert_wrapper: _BertWrapper,
    bert_tokenizer,
    norm_text: str,
    word2ph: list[int],
) -> np.ndarray:
    """Run NPU BERT and expand hidden states by word2ph.

    Returns: ja_bert [768, T_phone] where T_phone = sum(word2ph).
    """
    # Tokenize
    enc = bert_tokenizer(
        norm_text,
        padding="max_length",
        max_length=BERT_MAX_LEN,
        truncation=True,
        return_tensors="np",
    )
    input_ids = enc["input_ids"].astype(np.int32)            # [1, 200]
    token_type_ids = enc["token_type_ids"].astype(np.int32)  # [1, 200]
    attention_mask = enc["attention_mask"].astype(np.int32)   # [1, 200]

    # NPU inference
    hidden = bert_wrapper.infer(input_ids, token_type_ids, attention_mask)
    hidden = np.asarray(hidden, dtype=np.float32)  # [1, 200, 768]

    # Get actual token count (exclude padding)
    n_tokens = int(attention_mask.sum())
    hidden = hidden[0, :n_tokens]  # [n_tokens, 768]

    # Expand by word2ph: each word's BERT embedding is repeated for its phones
    n_use = min(len(word2ph), n_tokens)
    phone_features = []
    for i in range(n_use):
        repeat_feat = np.tile(hidden[i:i + 1], (word2ph[i], 1))  # [word2ph[i], 768]
        phone_features.append(repeat_feat)

    # Concatenate: [T_phone, 768] → transpose → [768, T_phone]
    phone_level_feature = np.concatenate(phone_features, axis=0)
    ja_bert = phone_level_feature.T.copy()  # [768, T_phone]

    return ja_bert


# ── Streaming decoder ────────────────────────────────────────────────────────

def _streaming_decode(
    decoder: _Decoder,
    z: np.ndarray,
    g: np.ndarray,
    y_lengths: int,
    decoder_time_dim: int,
    main_len: int,
    dec_overlap: int,
    *,
    cancel_event: Optional[threading.Event] = None,
    max_wall_s: float = 180.0,
    heartbeat_interval_s: float = 30.0,
) -> tuple[np.ndarray, int]:
    """Streaming decoder: split z into chunks and decode with overlap.

    decoder.bin expects z with time dim = decoder_time_dim.
    Each chunk produces decoder_time_dim * UPSAMPLE_FACTOR audio samples;
    only the central main_len * UPSAMPLE_FACTOR samples are kept.
    """
    decode_start = time.monotonic()
    last_heartbeat = decode_start

    # First chunk: frames [0, main_len + dec_overlap)
    z_buf = np.zeros(
        [z.shape[0], z.shape[1], decoder_time_dim], dtype=np.float32
    )
    first_take = main_len + dec_overlap
    z_buf[:, :, :first_take] = z[:, :, :first_take]
    audio_chunk = decoder.infer(g.astype(np.float32), z_buf)
    audio_chunk = np.asarray(audio_chunk, dtype=np.float32).reshape(-1)
    audio = audio_chunk[: main_len * UPSAMPLE_FACTOR]

    total = main_len
    chunk_idx = 1
    while total < y_lengths:
        # -- Safety: timeout / cancel / heartbeat --
        now = time.monotonic()
        elapsed = now - decode_start
        if elapsed > max_wall_s:
            raise _RunnerError(
                "DECODE_TIMEOUT",
                f"streaming decode exceeded wall-clock limit of {max_wall_s:.0f}s",
            )
        if cancel_event is not None and cancel_event.is_set():
            break
        if now - last_heartbeat >= heartbeat_interval_s:
            progress("infer", min(95.0, 72.0 + chunk_idx),
                     message=f"decoding chunk {chunk_idx} ({elapsed:.1f}s)")
            last_heartbeat = now
        # -- end safety --
        start = total - dec_overlap
        end = total + main_len + dec_overlap
        z_slice = z[:, :, start:end].astype(np.float32)
        # Pad slice to decoder's fixed time dim
        z_buf = np.zeros(
            [z.shape[0], z.shape[1], decoder_time_dim], dtype=np.float32
        )
        take = min(z_slice.shape[2], decoder_time_dim)
        z_buf[:, :, :take] = z_slice[:, :, :take]
        audio_chunk = decoder.infer(g.astype(np.float32), z_buf)
        audio_chunk = np.asarray(audio_chunk, dtype=np.float32).reshape(-1)
        slice_start = dec_overlap * UPSAMPLE_FACTOR
        slice_end = (main_len + dec_overlap) * UPSAMPLE_FACTOR
        audio = np.concatenate([audio, audio_chunk[slice_start:slice_end]])
        total += main_len
        chunk_idx += 1

    length = y_lengths * UPSAMPLE_FACTOR
    return audio[:length], chunk_idx


# ── Persistent worker mode API ───────────────────────────────────────────────

def load_model(cmd: dict) -> ModelContext:
    """Load all NPU models and G2P dependencies for reuse across inferences.

    Args:
        cmd: Command dict with keys: modelId, variantId, modelDir, repoRoot,
             packDir, variantContextBins.

    Returns:
        ModelContext holding all loaded resources.
    """
    repo_root = _resolve_repo_root(cmd)

    # Resolve model directory
    raw_model_dir = cmd.get("modelDir")
    if raw_model_dir:
        model_dir = Path(raw_model_dir)
        if not model_dir.is_absolute():
            model_dir = (Path.cwd() / model_dir).resolve()
        if not model_dir.is_dir():
            model_dir = _resolve_model_dir(repo_root)
    else:
        model_dir = _resolve_model_dir(repo_root)

    # Validate model files
    model_paths = _validate_model_files(model_dir)

    # Load symbol_to_id mapping
    sym_json = THIS_DIR / "melo_symbol_to_id.json"
    if not sym_json.is_file():
        raise _RunnerError(
            "ASSETS_NOT_INSTALLED",
            f"Missing {sym_json}. Export from melo: "
            "json.dump(tts.model.symbol_to_id, open(...))",
        )
    with open(sym_json, "r", encoding="utf-8") as f:
        symbol_to_id = json.load(f)

    # Load BERT tokenizer
    try:
        from bert_tokenizer_local import BertTokenizerLocal
    except ImportError as e:
        raise _RunnerError(
            "FRONTEND_DEP_MISSING",
            f"bert_tokenizer_local import failed: {e}. "
            "Ensure bert_tokenizer_local.py is placed alongside runner.py.",
        ) from e

    tok_bin = model_dir / "bert_zh_tokenizer.bin"
    norm_bin = model_dir / "bert_normalizer.bin"
    if not tok_bin.is_file() or not norm_bin.is_file():
        raise _RunnerError(
            "ASSETS_NOT_INSTALLED",
            f"BERT tokenizer binaries not found: {tok_bin}, {norm_bin}. "
            "Place bert_zh_tokenizer.bin and bert_normalizer.bin in the model directory.",
        )
    bert_tokenizer = BertTokenizerLocal(tok_bin, norm_bin)

    # Import qai_appbuilder
    (DataType, LogLevel, PerfProfile, ProfilingLevel,
     QNNConfig, QNNContext, Runtime) = _import_qai()

    # Initialize QNN. Use keyword arguments because qai_appbuilder builds in
    # the field can expose either a leading optional qnn_lib_path or the newer
    # runtime-first convenience shape; positional args silently shift
    # runtime=LogLevel.WARN (an int) on the former and break backend path
    # construction ("can only concatenate str (not int) to str").
    QNNConfig.Config(
        runtime=Runtime.HTP,
        log_level=LogLevel.WARN,
        profiling_level=ProfilingLevel.BASIC,
    )

    # G2P warmup runs in a background thread WHILE NPU models load.
    # This IS effective parallelism because QNNContext() is a C extension
    # that releases the GIL during model_initialize. So the Python-bound
    # G2P warmup (jieba + cache load) and C-bound NPU load truly run in parallel.
    g2p_warmup_done = threading.Event()
    g2p_warmup_err: list = []
    g2p_warmup_time_ms: list = []

    def _warmup_g2p() -> None:
        _t0 = time.time()
        try:
            from melo_zh_local import clean_text as _clean_text
            _clean_text("你好hello", "ZH")
        except Exception as e:  # noqa: BLE001
            g2p_warmup_err.append(e)
        finally:
            g2p_warmup_time_ms.append((time.time() - _t0) * 1000)
            g2p_warmup_done.set()

    g2p_warmup_thread = threading.Thread(
        target=_warmup_g2p, name="g2p-warmup", daemon=True
    )
    g2p_warmup_thread.start()

    # Load all QNNContext models (timed)
    _npu_load_t0 = time.time()
    contexts: list = []

    bert_ctx = QNNContext(
        "melotts_bert",
        str(model_paths["bert_wrapper"]),
        input_data_type=DataType.FLOAT,
        output_data_type=DataType.FLOAT,
    )
    contexts.append(bert_ctx)
    bert_wrapper = _BertWrapper(bert_ctx)

    encoder_ctx = QNNContext(
        "melotts_encoder",
        str(model_paths["encoder"]),
        input_data_type=DataType.FLOAT,
        output_data_type=DataType.FLOAT,
    )
    contexts.append(encoder_ctx)
    encoder = _Encoder(encoder_ctx)

    flow_ctx = QNNContext(
        "melotts_flow",
        str(model_paths["flow"]),
        input_data_type=DataType.FLOAT,
        output_data_type=DataType.FLOAT,
    )
    contexts.append(flow_ctx)
    flow = _Flow(flow_ctx)

    flow_short = None
    if "flow_short" in model_paths:
        flow_short_ctx = QNNContext(
            "melotts_flow_short",
            str(model_paths["flow_short"]),
            input_data_type=DataType.FLOAT,
            output_data_type=DataType.FLOAT,
        )
        contexts.append(flow_short_ctx)
        flow_short = _FlowShort(flow_short_ctx)

    decoder_ctx = QNNContext(
        "melotts_decoder",
        str(model_paths["decoder"]),
        input_data_type=DataType.FLOAT,
        output_data_type=DataType.FLOAT,
    )
    contexts.append(decoder_ctx)
    decoder = _Decoder(decoder_ctx)

    _npu_load_ms = (time.time() - _npu_load_t0) * 1000

    # Wait for G2P warmup to finish
    g2p_warmup_done.wait()
    if g2p_warmup_err:
        raise _RunnerError(
            "FRONTEND_ERROR",
            f"G2P warmup failed: {type(g2p_warmup_err[0]).__name__}: "
            f"{g2p_warmup_err[0]}",
        )

    # Build load timing stages for metrics
    _g2p_ms = g2p_warmup_time_ms[0] if g2p_warmup_time_ms else 0
    _load_stages = [
        {"name": "npu_model_load", "latencyMs": round(_npu_load_ms, 2)},
        {"name": "g2p_warmup", "latencyMs": round(_g2p_ms, 2)},
    ]

    # HTP BURST is made resident for the whole model lifecycle (Plan A):
    # set once here after all contexts (bert/encoder/flow/decoder) are
    # created, released once in release_model. This avoids ramping the HTP
    # clock up/down on every run_inference for session/streaming workloads.
    _perf_burst_active = False
    try:
        _t_perf = time.monotonic()
        PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
        _perf_burst_active = True
        print(f"[perf-diag] melotts SetPerfProfileGlobal(BURST) took {(time.monotonic()-_t_perf)*1000:.1f}ms", file=sys.stderr, flush=True)
    except Exception:  # pylint: disable=broad-except
        pass

    return ModelContext(
        bert_wrapper=bert_wrapper,
        encoder=encoder,
        flow=flow,
        flow_short=flow_short,
        decoder=decoder,
        bert_tokenizer=bert_tokenizer,
        symbol_to_id=symbol_to_id,
        model_dir=model_dir,
        repo_root=repo_root,
        _contexts=contexts,
        load_stages=_load_stages,
        perf_burst_active=_perf_burst_active,
    )


def run_inference(model_ctx: ModelContext, cmd: dict) -> None:
    """Run the TTS inference pipeline using pre-loaded models.

    Args:
        model_ctx: ModelContext returned by load_model().
        cmd: Run command dict with keys: runId, inputs, params, options.
             Optional: _cancel_event (threading.Event) for cancellation.
    """
    run_id = str(cmd.get("runId") or "melotts").strip() or "melotts"
    cancel_event = cmd.get("_cancel_event")

    # Validate inputs
    text = _validate_inputs(cmd)
    synth_params = _get_params(cmd)

    status("running")

    timer = StageTimer(device="htp")

    # HTP BURST is now resident for the whole model lifecycle (set in
    # load_model, released in release_model), so run_inference no longer
    # toggles the perf profile per run.
    # Stage 1: G2P (CPU) — already warmed, no thread needed
    with timer.stage("g2p"):
        phone_ids, tone_ids, lang_ids, word2ph, norm_text = _run_g2p(
            text, model_ctx.symbol_to_id
        )
        phone_len = len(phone_ids)

    progress("infer", 25)

    # Stage 2: NPU BERT feature extraction
    with timer.stage("bert_infer"):
        ja_bert_np = _run_bert_npu(
            model_ctx.bert_wrapper, model_ctx.bert_tokenizer, norm_text, word2ph
        )

        ja_bert_padded = np.zeros((1, 768, MAX_SEQ_LEN), dtype=np.float32)
        t_phone = min(ja_bert_np.shape[1], MAX_SEQ_LEN)
        ja_bert_padded[0, :, :t_phone] = ja_bert_np[:, :t_phone]

        bert_padded = np.zeros((1, 1024, MAX_SEQ_LEN), dtype=np.float32)

    progress("infer", 35)

    # Stage 3: NPU Encoder
    with timer.stage("encoder_infer"):
        x_np = np.zeros((1, MAX_SEQ_LEN), dtype=np.int32)
        x_np[0, :phone_len] = phone_ids

        tone_np = np.zeros((1, MAX_SEQ_LEN), dtype=np.int32)
        tone_np[0, :phone_len] = tone_ids

        lang_np = np.zeros((1, MAX_SEQ_LEN), dtype=np.int32)
        lang_np[0, :phone_len] = lang_ids

        x_lengths_np = np.array([phone_len], dtype=np.int32)
        sid_np = np.array([SPEAKER_ID], dtype=np.int32)

        sdp_ratio_np = np.array([synth_params["sdp_ratio"]], dtype=np.float32)
        length_scale_np = np.array([synth_params["length_scale"]], dtype=np.float32)
        noise_scale_w_np = np.array([synth_params["noise_scale_w"]], dtype=np.float32)

        encoder_outputs = model_ctx.encoder.infer(
            x_np, x_lengths_np, tone_np, sid_np, lang_np,
            bert_padded, ja_bert_padded,
            sdp_ratio_np, length_scale_np, noise_scale_w_np,
        )

        m_p = np.asarray(encoder_outputs[0], dtype=np.float32)
        logs_p = np.asarray(encoder_outputs[1], dtype=np.float32)
        w_ceil = np.asarray(encoder_outputs[2], dtype=np.float32)
        y_lengths_raw = np.asarray(encoder_outputs[3], dtype=np.float32)
        x_mask = np.asarray(encoder_outputs[4], dtype=np.float32)
        g = np.asarray(encoder_outputs[5], dtype=np.float32)

        y_len_int = int(np.round(y_lengths_raw.flatten()[0]))

        if y_len_int <= 0 or y_len_int > UPSAMPLED_MAX_SEQ_LEN:
            raise _RunnerError(
                "INFER_ERROR",
                f"Encoder produced invalid y_lengths={y_len_int}; "
                f"expected 1..{UPSAMPLED_MAX_SEQ_LEN}",
            )

    progress("infer", 50)

    # Stage 4: Build attention alignment (numpy, CPU)
    with timer.stage("attn"):
        y_mask_np = build_y_mask(y_len_int, UPSAMPLED_MAX_SEQ_LEN).astype(np.float32)
        attn_squeezed = generate_attn_squeezed(
            w_ceil, x_mask, y_mask_np
        ).astype(np.float32)

    progress("infer", 55)

    # Stage 5: NPU Flow
    with timer.stage("flow_infer"):
        noise_scale_np = np.array(
            [synth_params["noise_scale"]], dtype=np.float32
        )

        use_short = (
            model_ctx.flow_short is not None
            and phone_len <= FLOW_SHORT_S
            and y_len_int <= FLOW_SHORT_US
        )

        if use_short:
            m_p_s = m_p[:, :, :FLOW_SHORT_S]
            logs_p_s = logs_p[:, :, :FLOW_SHORT_S]
            y_mask_s = y_mask_np[:, :, :FLOW_SHORT_US]
            attn_s = attn_squeezed[:, :FLOW_SHORT_US, :FLOW_SHORT_S]
            z_short = model_ctx.flow_short.infer(
                m_p_s, logs_p_s, y_mask_s, attn_s, g, noise_scale_np,
            )
            z_short = np.asarray(z_short, dtype=np.float32)
            z = np.zeros(
                (z_short.shape[0], z_short.shape[1], UPSAMPLED_MAX_SEQ_LEN),
                dtype=np.float32,
            )
            z[:, :, :FLOW_SHORT_US] = z_short
        else:
            z = model_ctx.flow.infer(
                attn_squeezed, logs_p, noise_scale_np, m_p, y_mask_np, g
            )
            z = np.asarray(z, dtype=np.float32)

        z = z * y_mask_np

    progress("infer", 70)

    # Check cancellation before decoder (most expensive stage)
    if cancel_event and cancel_event.is_set():
        return

    # Stage 6: NPU Decoder (streaming chunks)
    progress("infer", 72, message="starting decoder streaming")
    with timer.stage("decoder_infer"):
        decoder_time_dim = DECODER_Z_TIME_DIM
        main_len = DEC_MAIN_LEN
        dec_overlap = DEC_OVERLAP

        audio, chunks_decoded = _streaming_decode(
            model_ctx.decoder, z, g, y_len_int,
            decoder_time_dim, main_len, dec_overlap,
            cancel_event=cancel_event,
        )

    # Check cancellation after decoder
    if cancel_event and cancel_event.is_set():
        return

    progress("infer", 95)

    # 7. Write output WAV (timed as postprocess)
    _post_t0 = time.time()
    audio = audio.astype(np.float32)
    peak = np.max(np.abs(audio))
    if peak > 0:
        audio = audio / peak * 0.95

    out_dir = model_ctx.repo_root / "data" / "outputs"
    out_path = out_dir / f"tts-{run_id}.wav"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        from audio_io import write_wav
    except ImportError:
        import wave
        clipped = np.clip(audio, -1.0, 1.0)
        pcm16 = (clipped * 32767.0).astype(np.int16)
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm16.tobytes())
    else:
        write_wav(out_path, audio, SAMPLE_RATE)
    _post_ms = (time.time() - _post_t0) * 1000

    progress("infer", 100)

    # Compute duration and build result
    duration_s = len(audio) / SAMPLE_RATE

    try:
        rel = out_path.resolve().relative_to(model_ctx.repo_root.resolve())
        rel_str = str(rel).replace("\\", "/")
    except ValueError:
        rel_str = str(out_path).replace("\\", "/")

    # Emit metrics — include all stages: inference + postprocess + model_load ref
    timer_summary = timer.summary()
    # Add postprocess as an additional stage
    if "stages" in timer_summary:
        timer_summary["stages"].append({"name": "postprocess", "latencyMs": round(_post_ms, 2)})
    # Recalculate total latency to include postprocess
    timer_summary["latencyMs"] = round(timer_summary.get("latencyMs", 0) + _post_ms, 2)
    # Include model load stages for the first-run context (informational)
    if model_ctx.load_stages:
        timer_summary["loadStages"] = model_ctx.load_stages
    timer_summary["phoneme_count"] = phone_len
    timer_summary["char_count"] = len(text)
    timer_summary["duration_s"] = round(duration_s, 3)
    timer_summary["chunks_decoded"] = chunks_decoded
    timer_summary["sample_rate"] = SAMPLE_RATE
    timer_summary["flow_path"] = "short" if use_short else "long"
    emit({"type": "metrics", **timer_summary})

    # Emit result
    result({
        "audio_path": rel_str,
        "duration_s": round(duration_s, 3),
        "sample_rate": SAMPLE_RATE,
        "phoneme_count": phone_len,
        "chunks_decoded": chunks_decoded,
    })


def release_model(model_ctx: ModelContext) -> None:
    """Release all QNNContext objects held by the model context.

    QNNContext cleanup happens via reference counting (C++ destructor invoked
    when the Python wrapper's refcount hits 0).  We clear all references in
    the ModelContext so the GC can collect them.
    """
    # Release the resident HTP BURST profile BEFORE contexts are destroyed
    # (mirrors the set at the end of load_model). Plan A: BURST tracks the
    # model's resident lifecycle rather than each individual run.
    if getattr(model_ctx, "perf_burst_active", False):
        try:
            from qai_appbuilder import PerfProfile  # type: ignore[import-not-found]
            _t_perf_rel = time.monotonic()
            PerfProfile.RelPerfProfileGlobal()
            print(f"[perf-diag] melotts RelPerfProfileGlobal took {(time.monotonic()-_t_perf_rel)*1000:.1f}ms", file=sys.stderr, flush=True)
        except Exception:  # pylint: disable=broad-except
            pass
        model_ctx.perf_burst_active = False

    # Clear the list of raw context references — this drops the refcounts.
    model_ctx._contexts.clear()
    # Also null out the wrapper attributes so nothing accidentally re-uses them.
    model_ctx.bert_wrapper = None  # type: ignore[assignment]
    model_ctx.encoder = None  # type: ignore[assignment]
    model_ctx.flow = None  # type: ignore[assignment]
    model_ctx.flow_short = None
    model_ctx.decoder = None  # type: ignore[assignment]


# ── Main pipeline (one-shot entry) ──────────────────────────────────────────

def main() -> None:
    req = read_request()

    status("preparing")

    # Load models (stages 2-6 of original pipeline + G2P warmup)
    model_ctx = load_model(req)

    progress("infer", 15)
    status("running")

    # Run inference (stages 1-7 of the pipeline)
    try:
        run_inference(model_ctx, req)
    finally:
        release_model(model_ctx)


# ── Entrypoint ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        main()
        done()
        sys.exit(0)
    except SystemExit:
        raise
    except _RunnerError as ue:
        fail(code=ue.code, message=ue.message)
        sys.exit(1)
    except Exception as e:
        fail(
            code="INFER_ERROR",
            message=str(e),
            traceback=traceback.format_exc(limit=20),
        )
        sys.exit(1)
