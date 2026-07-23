#!/usr/bin/env python
# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
zipformer-zh - App Builder Pack runner
======================================

Streaming Zipformer RNN-T Mandarin ASR runner adapted from the
QAI AppBuilder reference sample
``samples/python/zipformer/zipformer.py`` and wrapped into the App
Builder Pack runner protocol (plan v3.1 sections C*.6 / I.4 / R.4).

Model: ``zipformer`` streaming RNN-T (sherpa-onnx, k2-fsa) ported to
       three QNN HTP context binaries with ``DataType.NATIVE`` I/O:
         - encoder.bin   (chunked Zipformer encoder w/ 5x7=35 cache tensors)
         - decoder.bin   (RNN-T prediction network, 2-token context)
         - joiner.bin    (encoder x decoder -> vocab logits)

Pipeline
--------
  1. read_audio (shared/audio_io.py) -> 16 kHz mono float32
  2. kaldi_native_fbank.OnlineFbank  -> (T, 80) log-mel features
  3. for each chunk of OFFSET=64 frames:
       chunk = features[:, start:start+SEGMENT(=71), :]  (zero-pad to 71)
       enc_inputs = [chunk] + 35 cache tensors (cached_len/avg/key/val/val2/conv1/conv2 per layer)
       enc_outputs = encoder.run(enc_inputs)         -> 36 tensors
       new_state    = enc_outputs[:35]                # next-chunk cache
       encoder_out  = enc_outputs[35]   shape (1, 16, 512)
       for t in 0..16:
         logits = joiner.run([encoder_out[t], decoder_out])  shape (6254,)
         tid = argmax(logits)
         if tid != BLANK_ID:
            hyp.append(tid)
            decoder_out = decoder.run([hyp[-2:]])     # update prediction
  4. tokens -> text via assets/tokens.txt (sherpa-onnx ``<token> <id>`` format)
  5. emit fullText + segments[]   (single segment in MVP, covering the whole clip)

Sticky-worker mode (plan v3.1 R.5)
----------------------------------
This runner exposes the three-stage persistent API (``load_model`` /
``run_inference`` / ``release_model``).  ``main()`` chains them so the
file remains usable as a one-shot subprocess script as well.

Hard isolation rule
-------------------
NO module under ``features.model_builder`` is imported. App Builder Packs
are self-contained; ``shared/`` is the only allowed cross-Pack
dependency (loaded via PYTHONPATH).

Algorithm simplifications (MVP, called out for v1.x follow-up)
--------------------------------------------------------------
* Single segment per clip: ``segments=[{start:0, end:duration, text:...}]``.
  No silence / punctuation-driven segment splitting yet.
* No VAD; ``params.vad`` is parsed but currently a no-op.
* No hotword biasing; ``params.hotwords`` is parsed but ignored.
* No streaming output - we run the streaming encoder chunk-by-chunk
  internally (so the cache-state pipeline matches the model export) but
  emit a single result event at the end. v1.x may emit per-chunk
  partial results once the front end can render them.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple

import numpy as np

# shared/ is on PYTHONPATH (injected by backend.app_builder.runners.python_script).
from runner_protocol import emit, progress, read_request, status, result, done, fail   # noqa: E402
from telemetry import StageTimer   # noqa: E402


# --- Streaming Zipformer constants (must match the QNN export) --------------

ENCODER_FILENAME = "encoder.bin"
DECODER_FILENAME = "decoder.bin"
JOINER_FILENAME  = "joiner.bin"

SAMPLE_RATE = 16_000
N_MELS      = 80
HIGH_FREQ   = -400.0      # kaldi_native_fbank: subtract 400 Hz from Nyquist
MAX_AUDIO_SEC = 120.0     # manifest.inputSchema.constraints.maxSec

# Decode-loop geometry (from the reference sample).
DECODE_CHUNK_SIZE = 32
OFFSET            = DECODE_CHUNK_SIZE * 2          # 64 frames
SEGMENT           = OFFSET + 7                     # 71 frames
ENCODER_FRAMES_PER_CHUNK = 16                      # encoder_out chunk shape (1, 16, 512)

BLANK_ID     = 0
CONTEXT_SIZE = 2          # decoder takes the last 2 emitted tokens

# Safety limits for the decode loop (prevent runaway HTP stalls).
# MAX_DECODE_WALL_S: hard wall-clock cap on the entire _streaming_decode call.
# If the NPU enters a degraded state (power-throttled, context-corrupted after
# idle-release + reload), individual encoder/joiner calls can take 100x longer
# than normal. This timeout ensures the worker eventually responds rather than
# being killed by the host's 300s stdout-read timeout.
MAX_DECODE_WALL_S: float = 180.0    # 3 minutes (well under the host 300s limit)

# Heartbeat interval: emit a progress event every N seconds so the
# StickyWorkerHost's 300s stdout-read timeout never fires during legitimate
# long-running decodes.
_HEARTBEAT_INTERVAL_S: float = 30.0

# 5 encoder layers, per-layer cache shape configuration.
# Each tuple is (n_heads_or_count, key_history, value_history). Drives 7 cache
# tensors per layer = 35 cache tensors total in the encoder I/O.
LAYER_CONFIGS: List[Tuple[int, int, int]] = [
    (2, 128, 128),  # layer 0
    (4,  64,  64),  # layer 1
    (3,  32,  32),  # layer 2
    (2,  16,  16),  # layer 3
    (4,  64,  64),  # layer 4
]


# --- Domain errors ----------------------------------------------------------

class _UserError(Exception):
    """Structured user-visible error; ``code`` is forwarded to SSE error events."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --- Model auto-download (fallback when weights missing) --------------------
#
# Mirrors the QAI AppBuilder reference sample
# (samples/python/zipformer/zipformer.py · ensure_model_files): when the
# canonical models/zipformer-zh/ directory is missing one or more of the
# 3 context bins, we fetch the device-specific zip from qai-hub public
# assets, extract it, and copy encoder/decoder/joiner.bin into the canonical
# location. Subsequent runs short-circuit on the existence check.
#
# edition-dual-form §4.2: URL bumped v0.54.0 -> v0.55.0. The download +
# platform-detection + extract logic now lives in the shared
# ``weight_downloader`` helper (judgment-1: reuse, not re-create — whisper /
# melotts share the exact same path) and routes through the global proxy when
# configured (HTTPS_PROXY / ALL_PROXY injected by the apps/api wiring root —
# 缺口 10), connecting directly otherwise.

REQUIRED_BIN_FILES: Tuple[str, ...] = (ENCODER_FILENAME, DECODER_FILENAME, JOINER_FILENAME)

# Optional auxiliary files that ship in the zip and are nice to keep around
# (metadata.json describes the QAIRT version + quant scheme). Not required for
# inference; copied if present, never blocks success.
OPTIONAL_BIN_FILES: Tuple[str, ...] = ("metadata.json",)

# Single source of truth for the download metadata (tag / required+optional
# file lists / per-device zip URLs) is the sibling ``weights.json``. Loaded by
# RELATIVE PATH (``factory/`` is not a package — runners reach shared modules
# via sys.path injection, not package import) so the SAME metadata + extraction
# is reusable by both this runner subprocess and a future API-side downloader.
# The REQUIRED_/OPTIONAL_ constants above remain for the inference path
# resolution helpers and MUST stay in sync with weights.json (asserted by test).
_WEIGHTS_CONFIG_PATH = Path(__file__).resolve().parent / "weights.json"


def _load_weights_config() -> dict:
    """Load + parse this pack's sibling ``weights.json`` (download metadata).

    Returns the parsed dict with ``tag`` / ``required_files`` /
    ``optional_files`` / ``download_configs``. Raises ``_UserError`` with a
    clear message if the file is missing or unparseable.
    """
    try:
        with open(_WEIGHTS_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise _UserError(
            "WEIGHTS_NOT_INSTALLED",
            f"failed to load weights config {_WEIGHTS_CONFIG_PATH}: {e}. "
            "Ensure the pack ships weights.json alongside runner.py, or "
            "manually place the 3 .bin files at the model dir.",
        ) from e


def _ensure_weights_downloaded(model_dir: Path) -> None:
    """Download + extract zipformer weights into ``model_dir`` if missing.

    Thin wrapper over the shared ``weight_downloader.ensure_weights_downloaded``;
    re-raises its structured failure as ``_UserError("WEIGHTS_NOT_INSTALLED")``.
    Idempotent: a re-entry with all 3 .bin files present never touches the
    network. ``progress`` events are protocol-safe in both ``load_model``
    (sticky) and ``main()`` (one-shot) contexts.

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
            f"PYTHONPATH, or manually place the 3 .bin files at {model_dir}.",
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
    """Resolve ``inputs.audio`` honoring the ``data.rel_path || data.path || data.url``
    priority documented in plan §S.4."""
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


def _resolve_weights(repo_root: Path, pack_dir: Path) -> Tuple[Path, Path, Path]:
    """Find encoder/decoder/joiner.bin under repo-root models/<id>/ first
    (canonical install location). If any are missing AND the pack-internal
    weights/ directory does not have them either, automatically download the
    device-specific zip from qai-hub public assets and extract into the
    canonical models/<id>/ location. The download path mirrors the QAI
    AppBuilder reference sample's ``ensure_model_files`` and is fully
    idempotent: a subsequent run with all 3 .bin files present skips the
    network round-trip entirely.
    """
    canonical_dir = (repo_root / "models" / "zipformer-zh").resolve()
    pack_weights_dir = (pack_dir / "weights").resolve()

    def _all_present(d: Path) -> bool:
        return all((d / name).is_file() for name in REQUIRED_BIN_FILES)

    # 1. Canonical location (preferred).
    if _all_present(canonical_dir):
        return tuple(canonical_dir / n for n in REQUIRED_BIN_FILES)   # type: ignore[return-value]

    # 2. Pack-internal weights/ — only used if the user manually staged
    #    files there. We do NOT auto-download into weights/; downloads
    #    always land in the canonical models/<id>/ directory.
    if _all_present(pack_weights_dir):
        return tuple(pack_weights_dir / n for n in REQUIRED_BIN_FILES)   # type: ignore[return-value]

    # 3. Auto-download fallback. _ensure_weights_downloaded raises
    #    _UserError("WEIGHTS_NOT_INSTALLED", ...) on network/extraction
    #    failure, with hints for manual placement.
    _ensure_weights_downloaded(canonical_dir)

    # 4. Re-check; if STILL missing, give up with a structured error.
    if _all_present(canonical_dir):
        return tuple(canonical_dir / n for n in REQUIRED_BIN_FILES)   # type: ignore[return-value]

    missing = [
        f"{n} (canonical: {canonical_dir / n}; pack: {pack_weights_dir / n})"
        for n in REQUIRED_BIN_FILES
        if not (canonical_dir / n).is_file()
    ]
    raise _UserError(
        "WEIGHTS_NOT_INSTALLED",
        (
            "Zipformer-zh weights still missing after auto-download attempt:\n  - "
            + "\n  - ".join(missing)
            + f"\nManually place the 3 .bin files at {canonical_dir}, or check "
              "network connectivity and re-run."
        ),
    )


def _resolve_tokens(pack_dir: Path) -> Path:
    """Resolve assets/tokens.txt; reject obvious placeholder files early."""
    tokens_path = pack_dir / "assets" / "tokens.txt"
    if not tokens_path.is_file():
        raise _UserError(
            "ASSETS_NOT_INSTALLED",
            (
                f"tokens.txt not found at {tokens_path}. Place the real "
                "sherpa-onnx zipformer Mandarin tokens.txt (one token per "
                "line, '<token> <id>' format, ~6000 entries / ~50 KB) at "
                "assets/tokens.txt."
            ),
        )

    try:
        size = tokens_path.stat().st_size
    except OSError:
        size = 0
    if size < 1024:
        try:
            head = tokens_path.read_text(encoding="utf-8", errors="replace")[:512]
        except OSError:
            head = ""
        looks_placeholder = (
            "placeholder" in head.lower()
            or "TODO" in head
            or (head.strip().startswith("#") and "\n" not in head.strip())
        )
        if looks_placeholder or size < 256:
            raise _UserError(
                "ASSETS_NOT_INSTALLED",
                (
                    f"tokens.txt at {tokens_path} is a placeholder "
                    f"(size={size} B). Replace it with the real sherpa-onnx "
                    "zipformer Mandarin vocabulary (~6000 entries; ~50 KB)."
                ),
            )
    return tokens_path


# --- Token vocabulary -------------------------------------------------------

class _TokenTable:
    """sherpa-onnx ``tokens.txt`` loader + detokenizer.

    Format::
        <token> <id>     # one entry per line, id ascending; <blk> at id 0.

    Detokenization rules (zh + light BPE for English carry-over):
      * SentencePiece word-start ``__`` (U+2581) -> insert space before token.
      * WordPiece continuation ``##`` -> strip prefix, no space.
      * Specials ``<blk>`` / ``<sos/eos>`` / ``<unk>`` etc -> suppressed.
      * Plain Chinese characters -> emit as-is (no inter-char spaces).
    """

    def __init__(self, id_to_token: dict[int, str]) -> None:
        self._id_to_token = id_to_token
        self.vocab_size = (max(id_to_token) + 1) if id_to_token else 0

    @classmethod
    def load(cls, path: Path) -> "_TokenTable":
        try:
            raw_lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            raise _UserError(
                "TOKENIZER_LOAD_ERROR",
                f"failed to read tokens.txt at {path}: {e}",
            ) from e

        id_to_token: dict[int, str] = {}
        auto_id = 0
        for ln, raw in enumerate(raw_lines, start=1):
            line = raw.rstrip("\r\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            # sherpa-onnx format is "<token> <id>"; some files use space-only
            # token (rare). rsplit on the LAST whitespace so tokens that
            # themselves contain spaces would still parse - but in practice
            # sherpa-onnx tokens never contain spaces.
            parts = line.rsplit(" ", 1)
            try:
                if len(parts) == 2 and parts[1].lstrip("-").isdigit():
                    token, tid = parts[0], int(parts[1])
                else:
                    token, tid = line.split()[0], auto_id
                if tid < 0:
                    raise ValueError(f"negative id {tid}")
                if tid in id_to_token:
                    raise ValueError(
                        f"duplicate id {tid} at line {ln} "
                        f"({id_to_token[tid]!r} vs {token!r})"
                    )
                id_to_token[tid] = token
            except (ValueError, IndexError) as e:
                raise _UserError(
                    "TOKENIZER_LOAD_ERROR",
                    f"malformed tokens.txt line {ln}: {raw!r} ({e})",
                ) from e
            auto_id += 1

        if not id_to_token:
            raise _UserError(
                "TOKENIZER_LOAD_ERROR",
                f"tokens.txt at {path} contains no usable entries",
            )

        return cls(id_to_token)

    def detokenize(self, token_ids: List[int]) -> str:
        out: list[str] = []
        for tid in token_ids:
            tok = self._id_to_token.get(tid)
            if tok is None:
                continue
            # Suppress all <...> specials.
            if len(tok) >= 2 and tok.startswith("<") and tok.endswith(">"):
                continue
            # SentencePiece word-start: replace with leading space + rest.
            if tok.startswith("\u2581"):
                rest = tok[1:]
                if out and not out[-1].endswith(" "):
                    out.append(" ")
                if rest:
                    out.append(rest)
                continue
            # WordPiece continuation: strip ##.
            if tok.startswith("##"):
                out.append(tok[2:])
                continue
            out.append(tok)
        text = "".join(out)
        # Collapse double spaces from <blk> insertions or BPE artifacts.
        while "  " in text:
            text = text.replace("  ", " ")
        return text.strip()


# --- Streaming-encoder cache state ------------------------------------------

def _init_state() -> List[np.ndarray]:
    """Build the 35-tensor zero-initialized cache state for a fresh stream.

    Layout per layer (n, kh, vh) from LAYER_CONFIGS, 7 tensors per layer:
      cached_len   : (n, 1)              int32
      cached_avg   : (n, 1, 384)         float32
      cached_key   : (n, kh, 1, 192)     float32
      cached_val   : (n, vh, 1, 96)      float32
      cached_val2  : (n, vh, 1, 96)      float32
      cached_conv1 : (n, 1, 384, 30)     float32
      cached_conv2 : (n, 1, 384, 30)     float32
    """
    state: List[np.ndarray] = []
    for n, kh, vh in LAYER_CONFIGS:
        state.append(np.zeros((n, 1),                dtype=np.int32))
        state.append(np.zeros((n, 1, 384),           dtype=np.float32))
        state.append(np.zeros((n, kh, 1, 192),       dtype=np.float32))
        state.append(np.zeros((n, vh, 1,  96),       dtype=np.float32))
        state.append(np.zeros((n, vh, 1,  96),       dtype=np.float32))
        state.append(np.zeros((n, 1, 384, 30),       dtype=np.float32))
        state.append(np.zeros((n, 1, 384, 30),       dtype=np.float32))
    return state   # 35 tensors


def _parse_encoder_outputs(outputs: list) -> Tuple[np.ndarray, List[np.ndarray]]:
    """Split the 36-tensor encoder output into (encoder_out, new_state).

    The QNN export emits the 35 cache tensors first, then the encoder hidden
    output last (shape (1, 16, 512)). Order is fixed by the model export.
    """
    if not outputs:
        raise _UserError("INFER_ERROR", "encoder.run returned no outputs")
    outputs = list(outputs)
    if len(outputs) != 36:
        raise _UserError(
            "INFER_ERROR",
            f"unexpected encoder output count: got {len(outputs)}, want 36",
        )
    encoder_out = np.asarray(outputs[35], dtype=np.float32)
    new_state = [np.asarray(outputs[i]) for i in range(35)]
    return encoder_out, new_state


# --- Param validation -------------------------------------------------------

def _validate_params(params: dict[str, Any]) -> Tuple[str, bool, str]:
    language = str(params.get("language", "zh"))
    if language != "zh":
        raise _UserError(
            "INVALID_INPUT",
            f"params.language must be 'zh' (zipformer-zh is Mandarin-only), got {language!r}",
        )

    vad_raw = params.get("vad", True)
    if isinstance(vad_raw, str):
        vad = vad_raw.strip().lower() in ("1", "true", "yes", "on")
    else:
        vad = bool(vad_raw)

    hotwords = params.get("hotwords", "")
    if hotwords is None:
        hotwords = ""
    if not isinstance(hotwords, str):
        raise _UserError(
            "INVALID_INPUT",
            f"params.hotwords must be a string (newline-separated), got {type(hotwords).__name__}",
        )

    return language, vad, hotwords


# --- OOM heuristic ----------------------------------------------------------

def _is_oom(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return ("memory" in msg and ("out" in msg or "alloc" in msg or "exhaust" in msg)) \
        or "oom" in msg


# --- Feature extraction -----------------------------------------------------

def _compute_fbank(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """80-channel kaldi-style log-mel fbank using ``kaldi_native_fbank``.

    Returns shape ``(T, 80)`` float32. Settings match the sherpa-onnx
    Mandarin zipformer training recipe (snip_edges=False, dither=0,
    high_freq=-400 i.e. Nyquist - 400 Hz).
    """
    try:
        import kaldi_native_fbank as knf   # type: ignore[import-not-found]
    except ImportError as e:
        raise _UserError(
            "INFER_ERROR",
            (
                f"kaldi_native_fbank import failed: {e}. "
                "pip install kaldi_native_fbank (ARM64 wheel must match the "
                "Python in the configured venv)."
            ),
        ) from e

    opts = knf.FbankOptions()
    opts.frame_opts.dither     = 0
    opts.frame_opts.snip_edges = False
    opts.frame_opts.samp_freq  = sample_rate
    opts.mel_opts.num_bins     = N_MELS
    opts.mel_opts.high_freq    = HIGH_FREQ

    extractor = knf.OnlineFbank(opts)
    extractor.accept_waveform(sampling_rate=sample_rate, waveform=samples.astype(np.float32, copy=False))
    n_ready = extractor.num_frames_ready
    if n_ready <= 0:
        return np.zeros((0, N_MELS), dtype=np.float32)
    frames = np.empty((n_ready, N_MELS), dtype=np.float32)
    for i in range(n_ready):
        frames[i] = extractor.get_frame(i)
    return frames


# --- Streaming RNN-T greedy decode ------------------------------------------

def _streaming_decode(
    encoder, decoder, joiner,
    features: np.ndarray,
    blank_id: int = BLANK_ID,
    context_size: int = CONTEXT_SIZE,
    cancel_event: Optional[threading.Event] = None,
) -> List[int]:
    """Run the chunked streaming encoder + frame-wise greedy joiner.

    ``features`` shape: (T, 80). Behavior matches the reference sample's
    main loop (chunk_size=64, segment=71, encoder_out=16 frames per chunk).

    Per-call cache state is allocated fresh (``_init_state``) so that
    repeated invocations from a sticky worker do not bleed cache state
    across utterances.

    Safety:
      * Wall-clock timeout (``MAX_DECODE_WALL_S``): raises ``_UserError``
        if the HTP stalls and total decode time exceeds the limit.
      * Cancel event: if ``cancel_event`` is set (by the sticky worker host
        via op:cancel), decode stops early and returns partial results.
      * Heartbeat: emits a ``progress`` event every ``_HEARTBEAT_INTERVAL_S``
        so the host's 300s stdout-read timeout never fires during legitimate
        long-running inferences.
    """
    T = features.shape[0]
    if T == 0:
        return []

    import sys as _sys

    # Prepend batch dim so encoder gets (1, T, 80) and we can slice on axis 1.
    features = features[np.newaxis, ...].astype(np.float32, copy=False)  # (1, T, 80)

    # Prime decoder with [blank, blank] -> initial decoder embedding.
    hyp: List[int] = [blank_id] * context_size
    decoder_input = np.array([hyp], dtype=np.int32)            # (1, 2)
    dec_out = decoder.run([decoder_input])
    if not dec_out:
        raise _UserError("INFER_ERROR", "decoder.run returned no outputs (priming step)")
    decoder_out = np.asarray(dec_out[0], dtype=np.float32)     # (1, 512)

    state = _init_state()                                       # 35 tensors

    decode_start = time.monotonic()
    last_heartbeat = decode_start
    total_chunks = max(1, (T + OFFSET - 1) // OFFSET)
    chunks_done = 0
    # Diagnostic accumulators: split NPU time vs total (CPU overhead = total - npu).
    _npu_ns = 0
    _npu_calls = 0
    _enc_times: list = []   # per-chunk encoder.run() latency (ms)

    # Pad-template for the last (possibly short) chunk; allocated once.
    for start in range(0, T, OFFSET):
        # --- Safety checks at chunk boundary ---
        now = time.monotonic()
        elapsed = now - decode_start

        # Wall-clock timeout: prevent runaway HTP stalls.
        if elapsed > MAX_DECODE_WALL_S:
            raise _UserError(
                "DECODE_TIMEOUT",
                f"streaming decode exceeded wall-clock limit "
                f"({MAX_DECODE_WALL_S:.0f}s) at chunk {chunks_done}/{total_chunks} "
                f"(elapsed={elapsed:.1f}s). The HTP may be in a degraded state; "
                f"consider restarting the worker.",
            )

        # Cancel event: stop early if the host sent op:cancel.
        if cancel_event is not None and cancel_event.is_set():
            break

        # Heartbeat: emit progress so the host knows we're alive.
        if now - last_heartbeat >= _HEARTBEAT_INTERVAL_S:
            progress("infer", (chunks_done / max(1, total_chunks)) * 100.0,
                     message=f"decoding chunk {chunks_done}/{total_chunks} ({elapsed:.1f}s)")
            last_heartbeat = now

        chunk = features[:, start:start + SEGMENT, :]           # (1, <=71, 80)
        if chunk.shape[1] < SEGMENT:
            pad_len = SEGMENT - chunk.shape[1]
            chunk = np.pad(
                chunk,
                pad_width=((0, 0), (0, pad_len), (0, 0)),
                mode="constant",
                constant_values=0.0,
            )
        chunk = np.ascontiguousarray(chunk, dtype=np.float32)   # (1, 71, 80)

        # Encoder I/O: 1 chunk + 35 cache tensors -> 36 outputs (35 new cache + encoder_out).
        enc_inputs = [chunk] + [np.ascontiguousarray(s) for s in state]
        try:
            _t_npu = time.perf_counter_ns()
            enc_outputs = encoder.run(enc_inputs)
            _enc_ms = (time.perf_counter_ns() - _t_npu) / 1e6
            _npu_ns += int(_enc_ms * 1e6)
            _npu_calls += 1
            _enc_times.append(_enc_ms)
        except MemoryError as e:
            raise _UserError("OUT_OF_MEMORY", f"encoder OOM at frame {start}: {e}") from e
        except Exception as e:
            if _is_oom(e):
                raise _UserError("OUT_OF_MEMORY", str(e)) from e
            raise _UserError("INFER_ERROR", f"encoder.run failed at frame {start}: {e}") from e

        encoder_out, state = _parse_encoder_outputs(enc_outputs)
        # encoder_out shape (1, 16, 512) -> (16, 512)
        if encoder_out.ndim == 3:
            encoder_out = encoder_out[0]
        if encoder_out.ndim != 2:
            raise _UserError(
                "INFER_ERROR",
                f"unexpected encoder_out shape after squeeze: {encoder_out.shape}",
            )

        # Frame-by-frame greedy joiner over the 16 encoder frames.
        for t in range(encoder_out.shape[0]):
            cur_enc = encoder_out[t:t + 1]                      # (1, 512)
            try:
                _t_npu = time.perf_counter_ns()
                jo = joiner.run([
                    np.ascontiguousarray(cur_enc, dtype=np.float32),
                    np.ascontiguousarray(decoder_out, dtype=np.float32),
                ])
                _npu_ns += time.perf_counter_ns() - _t_npu
                _npu_calls += 1
            except MemoryError as e:
                raise _UserError("OUT_OF_MEMORY", f"joiner OOM: {e}") from e
            except Exception as e:
                if _is_oom(e):
                    raise _UserError("OUT_OF_MEMORY", str(e)) from e
                raise _UserError("INFER_ERROR", f"joiner.run failed: {e}") from e
            if not jo:
                raise _UserError("INFER_ERROR", "joiner.run returned no outputs")
            logits = np.asarray(jo[0], dtype=np.float32).reshape(-1)
            tid = int(np.argmax(logits))

            if tid != blank_id:
                hyp.append(tid)
                # Refresh decoder embedding from the last `context_size` tokens.
                decoder_input = np.array([hyp[-context_size:]], dtype=np.int32)
                try:
                    dec_out = decoder.run([decoder_input])
                except MemoryError as e:
                    raise _UserError("OUT_OF_MEMORY", f"decoder OOM: {e}") from e
                except Exception as e:
                    if _is_oom(e):
                        raise _UserError("OUT_OF_MEMORY", str(e)) from e
                    raise _UserError("INFER_ERROR", f"decoder.run failed: {e}") from e
                if not dec_out:
                    raise _UserError("INFER_ERROR", "decoder.run returned no outputs")
                decoder_out = np.asarray(dec_out[0], dtype=np.float32)

        chunks_done += 1

    elapsed_total = time.monotonic() - decode_start
    _npu_ms = _npu_ns / 1e6
    _cpu_ms = elapsed_total * 1000 - _npu_ms
    _enc_str = ",".join(f"{t:.0f}" for t in _enc_times)
    print(
        f"[zipformer-diag] _streaming_decode DONE: chunks_done={chunks_done}/{total_chunks} "
        f"elapsed={elapsed_total:.2f}s tokens={len(hyp) - context_size} "
        f"npu={_npu_ms:.1f}ms/{_npu_calls}calls cpu_overhead={_cpu_ms:.1f}ms "
        f"enc_per_chunk_ms=[{_enc_str}]",
        file=_sys.stderr, flush=True,
    )
    return hyp[context_size:]   # drop the priming [blank, blank]


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
    """Holds loaded zipformer-zh resources for reuse across inferences.

    Populated once by ``load_model()`` and consumed by ``run_inference()``;
    ``release_model()`` tears the QNN contexts down.

    Three QNN context binaries are held: ``encoder`` (chunked streaming
    Zipformer with 35-tensor cache I/O), ``decoder`` (RNN-T prediction
    network), and ``joiner`` (encoder x decoder -> vocab logits).  The
    streaming cache state itself is *not* part of the ModelContext --
    each ``run_inference`` invocation initializes a fresh 35-tensor zero
    state via ``_init_state()`` so that utterances do not bleed cache
    across sticky-worker calls.
    """
    encoder: Any                       # qnn_helper.QnnContext
    decoder: Any                       # qnn_helper.QnnContext
    joiner:  Any                       # qnn_helper.QnnContext
    tokens: _TokenTable
    encoder_path: Path
    decoder_path: Path
    joiner_path:  Path
    tokens_path:  Path
    pack_dir:  Path
    repo_root: Path
    _contexts: list = field(default_factory=list)   # raw refs, used by release_model
    load_stages: list = field(default_factory=list) # one-time load timings
    perf_token: Any = None                           # HTP BURST token; held resident across the model lifecycle


# --- Persistent-worker API: load_model / run_inference / release_model ------

def load_model(cmd: dict) -> ModelContext:
    """Load encoder + decoder + joiner QNN contexts and CPU-side assets.

    This function MUST NOT touch the runner protocol (no ``read_request`` /
    ``status`` / ``done`` / ``fail``).  ``progress`` events emitted by the
    optional weight-download path are intentionally retained -- they are
    informational and protocol-safe in both one-shot and sticky modes.

    Args:
        cmd: command/request dict.  Recognised keys:
             - ``repoRoot``    (str, optional) -- workspace root
             - ``packDir``     (str, optional) -- pack directory
             - ``modelDir``    (str, optional) -- overrides repoRoot/models/...
             - ``modelId`` / ``variantId`` / ``variantContextBins`` --
               accepted for protocol parity, currently unused (zipformer-zh
               has a single variant with fixed encoder/decoder/joiner
               filenames).

    Returns:
        ModelContext holding all loaded resources.
    """
    repo_root = _resolve_repo_root(cmd)
    pack_dir = _resolve_pack_dir(cmd, repo_root)

    # Allow the caller to pin an explicit model directory; if it does not
    # contain the expected .bin files we fall back to the default search
    # (which itself includes the auto-download flow).
    raw_model_dir = cmd.get("modelDir")
    encoder_path: Optional[Path] = None
    decoder_path: Optional[Path] = None
    joiner_path:  Optional[Path] = None
    if raw_model_dir:
        md = Path(raw_model_dir)
        if not md.is_absolute():
            md = (Path.cwd() / md).resolve()
        if md.is_dir():
            enc_cand = (md / ENCODER_FILENAME).resolve()
            dec_cand = (md / DECODER_FILENAME).resolve()
            joi_cand = (md / JOINER_FILENAME).resolve()
            if enc_cand.is_file() and dec_cand.is_file() and joi_cand.is_file():
                encoder_path, decoder_path, joiner_path = enc_cand, dec_cand, joi_cand

    if encoder_path is None or decoder_path is None or joiner_path is None:
        encoder_path, decoder_path, joiner_path = _resolve_weights(repo_root, pack_dir)

    # Token table is lightweight (host-side); resolve+load up front so we
    # surface ASSETS_NOT_INSTALLED before initializing the NPU contexts.
    tokens_path = _resolve_tokens(pack_dir)

    _t_tok0 = time.time()
    tokens = _TokenTable.load(tokens_path)
    _tok_ms = (time.time() - _t_tok0) * 1000

    # Lazy-import shared QNN helper so the import error becomes a structured
    # _UserError rather than a bare traceback.
    try:
        from qnn_helper import QnnContext   # noqa: WPS433
    except Exception as e:
        raise _UserError(
            "QAI_APPBUILDER_UNAVAILABLE",
            f"qnn_helper import failed: {e}. Ensure shared/ is on PYTHONPATH.",
        ) from e

    contexts: list = []

    # All three contexts (encoder / decoder / joiner) use DataType.NATIVE
    # for best performance — tensors are passed in their original dtype
    # (int32 for cache lengths / token ids, float32 for features / cache
    # values) without unnecessary conversion.
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

    # 2. Decoder.
    _t_dec0 = time.time()
    try:
        decoder_ctx = QnnContext.load(
            decoder_path,
            runtime="Htp", log_level=1,
            input_data_type="native",
            output_data_type="native",
        )
    except FileNotFoundError as e:
        try:
            encoder_ctx.close()
        except Exception:   # pylint: disable=broad-except
            pass
        raise _UserError("WEIGHTS_NOT_INSTALLED", str(e)) from e
    except NotImplementedError as e:
        try:
            encoder_ctx.close()
        except Exception:   # pylint: disable=broad-except
            pass
        raise _UserError("QAI_APPBUILDER_UNAVAILABLE", str(e)) from e
    except Exception as e:
        try:
            encoder_ctx.close()
        except Exception:   # pylint: disable=broad-except
            pass
        if _is_oom(e):
            raise _UserError("OUT_OF_MEMORY", str(e)) from e
        raise _UserError("INFER_ERROR", f"failed to load decoder: {e}") from e
    contexts.append(decoder_ctx)
    _dec_ms = (time.time() - _t_dec0) * 1000

    # 3. Joiner.
    _t_joi0 = time.time()
    try:
        joiner_ctx = QnnContext.load(
            joiner_path,
            runtime="Htp", log_level=1,
            input_data_type="native",
            output_data_type="native",
        )
    except FileNotFoundError as e:
        for c in (decoder_ctx, encoder_ctx):
            try:
                c.close()
            except Exception:   # pylint: disable=broad-except
                pass
        raise _UserError("WEIGHTS_NOT_INSTALLED", str(e)) from e
    except NotImplementedError as e:
        for c in (decoder_ctx, encoder_ctx):
            try:
                c.close()
            except Exception:   # pylint: disable=broad-except
                pass
        raise _UserError("QAI_APPBUILDER_UNAVAILABLE", str(e)) from e
    except Exception as e:
        for c in (decoder_ctx, encoder_ctx):
            try:
                c.close()
            except Exception:   # pylint: disable=broad-except
                pass
        if _is_oom(e):
            raise _UserError("OUT_OF_MEMORY", str(e)) from e
        raise _UserError("INFER_ERROR", f"failed to load joiner: {e}") from e
    contexts.append(joiner_ctx)
    _joi_ms = (time.time() - _t_joi0) * 1000

    load_stages = [
        {"name": "load_tokens", "latencyMs": round(_tok_ms, 2)},
        {"name": "load_encoder", "latencyMs": round(_enc_ms, 2),
         "model": ENCODER_FILENAME},
        {"name": "load_decoder", "latencyMs": round(_dec_ms, 2),
         "model": DECODER_FILENAME},
        {"name": "load_joiner", "latencyMs": round(_joi_ms, 2),
         "model": JOINER_FILENAME},
    ]

    # HTP BURST is made resident for the whole model lifecycle (Plan A):
    # set once here after all contexts are created, released once in
    # release_model. This avoids ramping the HTP clock up/down on every
    # run_inference for streaming/session workloads (many audio chunks).
    perf_token = _set_perf_burst()

    return ModelContext(
        encoder=encoder_ctx,
        decoder=decoder_ctx,
        joiner=joiner_ctx,
        tokens=tokens,
        encoder_path=encoder_path,
        decoder_path=decoder_path,
        joiner_path=joiner_path,
        tokens_path=tokens_path,
        pack_dir=pack_dir,
        repo_root=repo_root,
        _contexts=contexts,
        load_stages=load_stages,
        perf_token=perf_token,
    )


def run_inference(model_ctx: ModelContext, cmd: dict) -> None:
    """Transcribe an audio file using already-loaded models.

    Emits the full event stream (status / metrics / result) but does NOT
    call ``done()`` -- the worker host owns the lifecycle event for sticky
    mode.  ``main()`` calls ``done()`` itself in one-shot mode.

    Args:
        model_ctx: ModelContext returned by ``load_model()``.
        cmd: per-run command dict carrying ``inputs``, ``params``, options.
             ``repoRoot``/``packDir`` from the load command are reused if
             the run command omits them.
    """
    # Path resolution -- fall back to load-time values so a sticky worker
    # that is given a minimal run command still works.
    repo_root = _resolve_repo_root(cmd) if cmd.get("repoRoot") else model_ctx.repo_root
    pack_dir = (
        _resolve_pack_dir(cmd, repo_root) if cmd.get("packDir") else model_ctx.pack_dir
    )

    # 1. Validate inputs / params before touching heavy resources.
    audio_path = _resolve_input_audio(cmd, repo_root, pack_dir)
    language, _vad, _hotwords = _validate_params(cmd.get("params") or {})

    status("running")

    # 2. Read audio (host-side; surface decode errors before NPU work).
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
    if samples.size == 0:
        raise _UserError("AUDIO_DECODE_ERROR", "decoded audio is empty (0 samples)")

    timer = StageTimer(device="htp")

    encoder = model_ctx.encoder
    decoder = model_ctx.decoder
    joiner  = model_ctx.joiner
    tokens  = model_ctx.tokens

    # HTP BURST is now resident for the whole model lifecycle (set in
    # load_model, released in release_model), so run_inference no longer
    # toggles the perf profile per run.

    # 3. Compute fbank features (CPU, host-side).
    with timer.stage("preprocess"):
        try:
            features = _compute_fbank(samples, sr)              # (T, 80)
        except Exception as e:
            raise _UserError(
                "INFER_ERROR",
                f"fbank feature computation failed: {e}",
            ) from e

    # 4. Streaming encoder + frame-by-frame greedy joiner. HTP burst mode
    #    is held resident across the model lifecycle (see load_model).
    with timer.stage("infer"):
        # Extract the cancel event injected by the sticky-worker bootstrap
        # (threading.Event set when op:cancel arrives for this run).
        cancel_ev = cmd.get("_cancel_event")
        try:
            token_ids = _streaming_decode(
                encoder, decoder, joiner,
                features,
                blank_id=BLANK_ID,
                context_size=CONTEXT_SIZE,
                cancel_event=cancel_ev,
            )
        except _UserError:
            raise
        except MemoryError as e:
            raise _UserError("OUT_OF_MEMORY", f"streaming decode OOM: {e}") from e
        except Exception as e:
            if _is_oom(e):
                raise _UserError("OUT_OF_MEMORY", str(e)) from e
            raise _UserError("INFER_ERROR", f"streaming decode failed: {e}") from e

    with timer.stage("postprocess"):
        full_text = tokens.detokenize(token_ids)

    # 5. Compose output. MVP: single segment covering the whole clip.
    segments: List[dict[str, Any]] = [{
        "start": 0.0,
        "end":   round(float(duration_s), 3),
        "text":  full_text,
    }]

    metrics_payload: dict[str, Any] = {
        "type": "metrics",
        **timer.summary(),
        "token_count":    int(len(token_ids)),
        "feature_frames": int(features.shape[0]),
        "total_audio_s":  round(duration_s, 3),
        "language":       language,
    }
    # Attach the one-time load timings only on the FIRST inference after
    # load_model. The sticky worker reuses the same ModelContext across many
    # run_inference calls -- clearing load_stages here ensures the metric is
    # surfaced exactly once (the cold-start cost) rather than redundantly
    # appearing in every subsequent fast-path inference.
    if model_ctx.load_stages:
        metrics_payload["loadStages"] = model_ctx.load_stages
        model_ctx.load_stages = []
    emit(metrics_payload)

    result({
        "fullText": full_text,
        "segments": segments,
    })


def release_model(model_ctx: ModelContext) -> None:
    """Tear down all QNN contexts held by ``model_ctx``.

    Calls ``close()`` on every raw context (idempotent in qnn_helper) and
    nulls out the public attributes so the GC can reclaim everything.
    Errors during teardown are swallowed -- the worker is shutting down
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
    model_ctx.joiner = None       # type: ignore[assignment]
    # Drop bulky CPU-side caches too so a long-running worker does not
    # retain the token table after the model is unloaded.
    model_ctx.tokens = None       # type: ignore[assignment]


# --- Main (one-shot entry, sticky-mode-compatible) --------------------------

def main() -> None:
    """One-shot entrypoint: load -> infer -> release.

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
