# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
pipertts_en.py 
==============================================================================
PiperTTS-EN full HTP inference script (based on qai_appbuilder / QNNContext).

Usage (run from samples/ directory):
    python Audio/Audio_Generation/pipertts_en/pipertts_en.py
    python Audio/Audio_Generation/pipertts_en/pipertts_en.py --text "Hello world."

Optional arguments:
    --text          Input text (default: built-in demo text)
    --models_dir    Model directory (default: models/ under script directory)
    --noise_scale   Noise scale (default 0.667)
    --noise_scale_w Duration noise scale (default 0.8)
    --length_scale  Speech rate, >1 is slower (default 1.0)
    --out           Output WAV file path (default <script_dir>/output.wav)
    --skip_download Skip model download (use when models are already prepared)

Dependencies (only G2P requires extra packages, rest are standard library):
    gruut  (pip install gruut)
"""

import sys
import os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Add utils to path (when running from samples/ directory)
sys.path.append(".")
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "common"))

import argparse
import time
import wave
import zipfile
import numpy as np

# -- Path configuration -------------------------------------------------------
_SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_MODELS = os.path.join(_SCRIPT_DIR, "models")

# -- Audio parameters ---------------------------------------------------------
SAMPLE_RATE = 22050

# -- Model fixed dimensions (from official ref_shared_model.py) ---------------
ENC_TEXT_MAX    = 512   # MAX_SEQ_LEN
FLOW_T_MEL      = 1536  # UPSAMPLED_MAX_SEQ_LEN = MAX_SEQ_LEN * 3
FEAT_DIM        = 192   # ENCODER_HIDDEN_DIM
UPSAMPLE_FACTOR = 256   # UPSAMPLE_FACTOR
DEC_SEQ_OVERLAP = 12    # DEC_SEQ_OVERLAP
MAX_DEC_SEQ_LEN = 40    # MAX_DEC_SEQ_LEN
DEC_SEQ_LEN     = MAX_DEC_SEQ_LEN + 2 * DEC_SEQ_OVERLAP  # = 64

# -- Model download URLs (by device model) ------------------------------------
_MODEL_URLS = {
    "snapdragon_x_elite": (
        "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/"
        "models/pipertts_en/releases/v0.57.1/"
        "pipertts_en-voice_ai-float-qualcomm_snapdragon_x_elite.zip"
    ),
    "snapdragon_x2_elite": (
        "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/"
        "models/pipertts_en/releases/v0.57.1/"
        "pipertts_en-voice_ai-float-qualcomm_snapdragon_x2_elite.zip"
    ),
}
_REQUIRED_MODELS = ["encoder.bin", "sdp.bin", "flow.bin", "decoder.bin"]

# -- Piper phoneme vocabulary (from official piper-voices voice config) --------
# IPA char -> piper encoder input id
# '_'=0(PAD), '^'=1(BOS), '$'=2(EOS), ' '=3(space)
PIPER_PHONEME_ID_MAP = {
    '_': 0, '^': 1, '$': 2, ' ': 3, '!': 4, "'": 5,
    '(': 6, ')': 7, ',': 8, '-': 9, '.': 10, ':': 11,
    ';': 12, '?': 13, 'a': 14, 'b': 15, 'c': 16, 'd': 17,
    'e': 18, 'f': 19, 'h': 20, 'i': 21, 'j': 22, 'k': 23,
    'l': 24, 'm': 25, 'n': 26, 'o': 27, 'p': 28, 'q': 29,
    'r': 30, 's': 31, 't': 32, 'u': 33, 'v': 34, 'w': 35,
    'x': 36, 'y': 37, 'z': 38, '\u00e6': 39, '\u00e7': 40, '\u00f0': 41,
    '\u00f8': 42, '\u0127': 43, '\u014b': 44, '\u0153': 45, '\u01c0': 46, '\u01c1': 47,
    '\u01c2': 48, '\u01c3': 49, '\u0250': 50, '\u0251': 51, '\u0252': 52, '\u0253': 53,
    '\u0254': 54, '\u0255': 55, '\u0256': 56, '\u0257': 57, '\u0258': 58, '\u0259': 59,
    '\u025a': 60, '\u025b': 61, '\u025c': 62, '\u025e': 63, '\u025f': 64, '\u0260': 65,
    '\u0261': 66, '\u0262': 67, '\u0263': 68, '\u0264': 69, '\u0265': 70, '\u0266': 71,
    '\u0267': 72, '\u0268': 73, '\u026a': 74, '\u026b': 75, '\u026c': 76, '\u026d': 77,
    '\u026e': 78, '\u026f': 79, '\u0270': 80, '\u0271': 81, '\u0272': 82, '\u0273': 83,
    '\u0274': 84, '\u0275': 85, '\u0276': 86, '\u0278': 87, '\u0279': 88, '\u027a': 89,
    '\u027b': 90, '\u027d': 91, '\u027e': 92, '\u0280': 93, '\u0281': 94, '\u0282': 95,
    '\u0283': 96, '\u0284': 97, '\u0288': 98, '\u0289': 99, '\u028a': 100, '\u028b': 101,
    '\u028c': 102, '\u028d': 103, '\u028e': 104, '\u028f': 105, '\u0290': 106, '\u0291': 107,
    '\u0292': 108, '\u0294': 109, '\u0295': 110, '\u0298': 111, '\u0299': 112, '\u029b': 113,
    '\u029c': 114, '\u029d': 115, '\u029f': 116, '\u02a1': 117, '\u02a2': 118, '\u02b2': 119,
    '\u02c8': 120, '\u02cc': 121, '\u02d0': 122, '\u02d1': 123, '\u02de': 124, '\u03b2': 125,
    '\u03b8': 126, '\u03c7': 127, '\u1d4b': 128, '\u2c71': 129, '0': 130, '1': 131,
    '2': 132, '3': 133, '4': 134, '5': 135, '6': 136, '7': 137,
    '8': 138, '9': 139, '\u0327': 140, '\u0303': 141, '\u032a': 142,
    '\u032f': 143, '\u0329': 144, '\u02b0': 145, '\u02e4': 146, '\u03b5': 147,
    '\u2193': 148, '#': 149, '"': 150, '\u2191': 151, '\u033a': 152, '\u033b': 153,
}
PIPER_BOS_ID = PIPER_PHONEME_ID_MAP['^']   # 1
PIPER_EOS_ID = PIPER_PHONEME_ID_MAP['$']   # 2


# ============================================================================
# 0. Model Download
# ============================================================================

def download_models(models_dir: str = _DEFAULT_MODELS) -> bool:
    """
    Detect device model and download the corresponding PiperTTS-EN model zip,
    then extract to models_dir. Uses install.py functions.

    Returns:
        True  - models are ready (already exist or downloaded successfully)
        False - download failed
    """
    import install

    # Check if all required models already exist
    all_exist = all(
        os.path.isfile(os.path.join(models_dir, m))
        for m in _REQUIRED_MODELS
    )
    if all_exist:
        print(f"[INFO] Models already exist in {models_dir}, skipping download.")
        return True

    # Detect device model
    device_model = install.detect_device_model()
    print(f"[INFO] Detected device model: {device_model}")

    url = _MODEL_URLS.get(device_model)
    if url is None:
        print(f"[WARN] Unknown device model '{device_model}', "
              f"falling back to snapdragon_x_elite.")
        url = _MODEL_URLS["snapdragon_x_elite"]

    # Prepare download path
    os.makedirs(models_dir, exist_ok=True)
    zip_filename = os.path.basename(url)
    zip_path = os.path.join(models_dir, zip_filename)

    # Download zip
    desc = f"Downloading PiperTTS-EN models for {device_model}..."
    fail = (
        f"\nFailed to download PiperTTS-EN models from:\n  {url}\n"
        f"Please download manually and extract to: {models_dir}"
    )
    print(f"[INFO] Downloading: {url}")
    ret = install.download_url(url, zip_path, desc=desc, fail=fail)
    if not ret:
        return False

    # Extract zip - only .bin files, placed directly in models_dir
    print(f"[INFO] Extracting models to {models_dir} ...")
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for member in zf.namelist():
                if member.endswith('.bin'):
                    filename = os.path.basename(member)
                    target_path = os.path.join(models_dir, filename)
                    with zf.open(member) as src, open(target_path, 'wb') as dst:
                        dst.write(src.read())
                    print(f"[INFO]   Extracted: {filename}")
        print(f"[OK]  Extraction complete.")
    except Exception as e:
        print(f"[ERROR] Extraction failed: {e}")
        return False

    # Delete zip file after successful extraction
    try:
        os.remove(zip_path)
        print(f"[INFO] Deleted zip file: {zip_path}")
    except Exception as e:
        print(f"[WARN] Could not delete zip file {zip_path}: {e}")

    # Verify extraction result
    missing = [m for m in _REQUIRED_MODELS
               if not os.path.isfile(os.path.join(models_dir, m))]
    if missing:
        print(f"[ERROR] Missing model files after extraction: {missing}")
        print(f"        Please check zip contents or extract manually to {models_dir}")
        return False

    print(f"[OK]  All model files ready: {_REQUIRED_MODELS}")
    return True


# ============================================================================
# 1. G2P: gruut -> IPA -> piper phoneme ids
# ============================================================================

_GRUUT_BOUNDARY_MAP = {
    '|':      PIPER_PHONEME_ID_MAP[' '],   # minor boundary -> space (3)
    '\u2016': PIPER_PHONEME_ID_MAP[' '],   # major boundary -> space (3)
}
_GRUUT_SKIP_CHARS = {'\u0361'}  # combining tie in t-tesh, d-dz

_PUNCT_MAP = {
    ',': PIPER_PHONEME_ID_MAP[','],   # 8
    '.': PIPER_PHONEME_ID_MAP['.'],   # 10
    '?': PIPER_PHONEME_ID_MAP['?'],   # 13
    '!': PIPER_PHONEME_ID_MAP['!'],   # 4
    ':': PIPER_PHONEME_ID_MAP[':'],   # 11
    ';': PIPER_PHONEME_ID_MAP[';'],   # 12
}


def text_to_piper_ids(text: str, lang: str = "en-us",
                      verbose: bool = False) -> np.ndarray:
    """
    Text -> piper phoneme ids (BOS + phonemes + EOS).
    Uses gruut for G2P (espeak-ng compatible, pure Python).
    Format: BOS word1 SPACE PUNCT SPACE word2 EOS
    """
    try:
        import gruut
    except ImportError:
        raise ImportError("gruut not installed. Run: pip install gruut")

    # Collect (word_text, phonemes_list, is_break) entries
    word_entries = []
    for sent in gruut.sentences(text, lang=lang):
        for word in sent:
            if word.phonemes:
                word_entries.append((word.text, word.phonemes, word.is_break))

    if verbose:
        print(f"  [G2P] gruut words+phonemes ({len(word_entries)}):")
        for wt, ph, is_break in word_entries:
            break_marker = " [BREAK]" if is_break else ""
            print(f"    {repr(wt):20s} -> {ph}{break_marker}")

    # Map to piper ids
    ids = [PIPER_BOS_ID]
    skipped = []
    prev_was_word = False  # track whether to insert space between words

    for word_text, phonemes, is_break in word_entries:
        if is_break:
            # Punctuation word: SPACE PUNCT SPACE
            if prev_was_word:
                ids.append(PIPER_PHONEME_ID_MAP[' '])
            for ch in word_text:
                if ch in _PUNCT_MAP:
                    ids.append(_PUNCT_MAP[ch])
            ids.append(PIPER_PHONEME_ID_MAP[' '])
            prev_was_word = False
        else:
            # Regular word: insert space between adjacent words
            if prev_was_word:
                ids.append(PIPER_PHONEME_ID_MAP[' '])
            # Map phonemes one by one
            for p in phonemes:
                if p in _GRUUT_BOUNDARY_MAP:
                    ids.append(_GRUUT_BOUNDARY_MAP[p])
                elif p in PIPER_PHONEME_ID_MAP:
                    ids.append(PIPER_PHONEME_ID_MAP[p])
                else:
                    # Decompose char by char (e.g. 'oU' -> 'o'=27, 'U'=100)
                    found_any = False
                    for ch in p:
                        if ch in _GRUUT_SKIP_CHARS:
                            continue  # silently skip combining tie
                        if ch in PIPER_PHONEME_ID_MAP:
                            ids.append(PIPER_PHONEME_ID_MAP[ch])
                            found_any = True
                        else:
                            skipped.append(repr(ch))
                    if not found_any:
                        skipped.append(repr(p))
            prev_was_word = True

    # Remove trailing space
    while ids and ids[-1] == PIPER_PHONEME_ID_MAP[' ']:
        ids.pop()
    ids.append(PIPER_EOS_ID)

    if verbose:
        if skipped:
            print(f"  [G2P] Skipped unknown phoneme chars: {skipped}")
        id_to_ph = {v: k for k, v in PIPER_PHONEME_ID_MAP.items()}
        print(f"  [G2P] piper ids ({len(ids)}): {ids}")
        print(f"  [G2P] phonemes: {[id_to_ph.get(i,'?') for i in ids]}")

    return np.array(ids, dtype=np.int32)


# ============================================================================
# 2. Model Loading
# ============================================================================

class PiperTTSModels:
    """Holds encoder / sdp / flow / decoder QNN context binaries."""

    def __init__(self, models_dir: str):
        from qai_appbuilder import QNNContext, QNNConfig, Runtime, LogLevel
        QNNConfig.Config(runtime=Runtime.HTP, log_level=LogLevel.ERROR)
        self._ctx = {}
        names = ["encoder", "sdp", "flow", "decoder"]
        print(f"[INFO] Loading {len(names)} QNN context binaries ...")
        t0 = time.perf_counter()
        for name in names:
            path = os.path.join(models_dir, f"{name}.bin")
            if not os.path.exists(path):
                raise FileNotFoundError(f"Model file not found: {path}")
            self._ctx[name] = QNNContext(model_name=name, model_path=path)
        print(f"[OK]  Loaded ({time.perf_counter()-t0:.2f}s)")

    def infer(self, name: str, inputs: list) -> list:
        return self._ctx[name].Inference(inputs)


# ============================================================================
# 3. Text Encoder
# ============================================================================

def run_encoder(models: PiperTTSModels, phoneme_ids: np.ndarray):
    """
    piper phoneme ids -> (m_p, logs_p, x_encoded, x_mask)

    encoder.bin I/O:
      in : x [1,512] int32, x_lengths [1] int32
      out[0]: m_p       [1,192,512]
      out[1]: logs_p    [1,192,512]
      out[2]: x_encoded [1,192,512]
      out[3]: x_mask    [1,1,512]
    """
    n = len(phoneme_ids)
    x = np.zeros((1, ENC_TEXT_MAX), dtype=np.int32)
    x[0, :n] = phoneme_ids
    out = models.infer("encoder", [x, np.array([n], dtype=np.int32)])
    return (np.array(out[0]),   # m_p
            np.array(out[1]),   # logs_p
            np.array(out[2]),   # x_encoded
            np.array(out[3]))   # x_mask


# ============================================================================
# 4. Stochastic Duration Predictor
# ============================================================================

def run_sdp(models: PiperTTSModels, x_encoded, x_mask,
            noise_scale_w=0.8, length_scale=1.0):
    """
    sdp.bin I/O:
      in : x_encoded [1,192,512], x_mask [1,1,512],
           noise_scale_w [1], length_scale [1]
      out: y_lengths [1], w_ceil [1,1,512]
    """
    out = models.infer("sdp", [
        x_encoded, x_mask,
        np.array([noise_scale_w], dtype=np.float32),
        np.array([length_scale],  dtype=np.float32),
    ])
    return np.array(out[0]), np.array(out[1])


# ============================================================================
# 5. Attention Matrix + Flow
# ============================================================================

def generate_path(w_ceil: np.ndarray, n_phones: int,
                  y_len_sdp: int,
                  t_mel_max: int = FLOW_T_MEL,
                  t_text_max: int = ENC_TEXT_MAX):
    """
    Generate hard-attention matrix attn_squeezed [1, t_mel_max, t_text_max].
    Uses y_len_sdp (from SDP output) as y_len to avoid rounding errors.
    """
    durs  = np.clip(np.round(w_ceil[0, 0, :n_phones]).astype(np.int32), 0, None)
    y_len = min(int(y_len_sdp), t_mel_max)

    attn = np.zeros((t_mel_max, t_text_max), dtype=np.float32)
    t = 0
    for i, d in enumerate(durs):
        for _ in range(int(d)):
            if t >= y_len:
                break
            if i < t_text_max:
                attn[t, i] = 1.0
            t += 1
        if t >= y_len:
            break

    return attn[np.newaxis], y_len   # [1, t_mel_max, t_text_max], int


def run_flow(models: PiperTTSModels,
             attn_squeezed, m_p, logs_p, y_len,
             noise_scale=0.667):
    """
    flow.bin I/O:
      in : attn_squeezed [1,1536,512], m_p [1,192,512], logs_p [1,192,512],
           noise_scale [1], y_mask [1,1,1536]
      out: z [1, 192, 1536]
    """
    y_mask = np.zeros((1, 1, FLOW_T_MEL), dtype=np.float32)
    y_mask[0, 0, :y_len] = 1.0
    out = models.infer("flow", [
        attn_squeezed, m_p, logs_p,
        np.array([noise_scale], dtype=np.float32),
        y_mask,
    ])
    return np.array(out[0])   # [1, 192, 1536]


# ============================================================================
# 6. HiFi-GAN Decoder (overlap chunking)
# ============================================================================

def run_decoder(models: PiperTTSModels,
                z: np.ndarray, y_len: int) -> np.ndarray:
    """
    z [1, 192, 1536] -> audio waveform float32.

    Overlap chunking:
      - First chunk: z[:, :, 0 : MAX_DEC_SEQ_LEN + DEC_SEQ_OVERLAP]
        output: first MAX_DEC_SEQ_LEN * UPSAMPLE_FACTOR samples
      - Subsequent chunks: z[:, :, t-overlap : t+MAX+overlap]
        output: [overlap*256 : (overlap+actual_frames)*256]
    """
    audio_chunks = []

    # First chunk
    z_buf = np.zeros((1, FEAT_DIM, DEC_SEQ_LEN), dtype=np.float32)
    first_end = min(MAX_DEC_SEQ_LEN + DEC_SEQ_OVERLAP, z.shape[2])
    z_buf[0, :, :first_end] = z[0, :, :first_end]
    out = models.infer("decoder", [z_buf])
    audio_chunk = np.array(out[0])[0, 0]
    audio_chunks.append(audio_chunk[:MAX_DEC_SEQ_LEN * UPSAMPLE_FACTOR])

    total_dec_seq_len = MAX_DEC_SEQ_LEN

    # Subsequent chunks: process all remaining frames up to y_len
    while total_dec_seq_len < y_len:
        s = total_dec_seq_len - DEC_SEQ_OVERLAP
        e = total_dec_seq_len + MAX_DEC_SEQ_LEN + DEC_SEQ_OVERLAP

        z_slice = z[:, :, s:min(e, z.shape[2])]
        if z_slice.shape[2] < DEC_SEQ_LEN:
            z_slice = np.pad(z_slice,
                             ((0, 0), (0, 0), (0, DEC_SEQ_LEN - z_slice.shape[2])))

        out = models.infer("decoder", [z_slice])
        audio_chunk = np.array(out[0])[0, 0]

        # Compute actual valid frames for this chunk
        remaining = y_len - total_dec_seq_len
        actual_frames = min(MAX_DEC_SEQ_LEN, remaining)

        start_s = DEC_SEQ_OVERLAP * UPSAMPLE_FACTOR
        end_s   = (DEC_SEQ_OVERLAP + actual_frames) * UPSAMPLE_FACTOR
        audio_chunks.append(audio_chunk[start_s:end_s])

        # Advance by actual frames processed (v9 fix)
        total_dec_seq_len += actual_frames

    if not audio_chunks:
        return np.zeros(MAX_DEC_SEQ_LEN * UPSAMPLE_FACTOR, dtype=np.float32)

    audio = np.concatenate(audio_chunks)
    return audio[:y_len * UPSAMPLE_FACTOR]


# ============================================================================
# 7. Save WAV
# ============================================================================

def save_wav(audio: np.ndarray, path: str,
             sample_rate: int = SAMPLE_RATE) -> None:
    """float32 audio -> 16-bit PCM WAV (peak-normalized to 0.95)."""
    peak = np.abs(audio).max()
    if peak > 1e-6:
        audio = audio / peak * 0.95
    audio_i16 = np.clip(audio * 32767, -32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_i16.tobytes())
    dur = len(audio_i16) / sample_rate
    rms = np.sqrt(np.mean((audio_i16.astype(np.float32) / 32767) ** 2))
    print(f"[OK]  WAV: {path}")
    print(f"      {sample_rate} Hz  {dur:.2f}s  {len(audio_i16)} samples  RMS={rms:.4f}")


# ============================================================================
# 8. Full TTS Pipeline
# ============================================================================

def tts(models: PiperTTSModels,
        text: str,
        out_path: str,
        noise_scale: float   = 0.667,
        noise_scale_w: float = 0.8,
        length_scale: float  = 1.0,
        verbose: bool        = False) -> np.ndarray:
    """End-to-end TTS: text -> WAV."""

    print(f"\n[TTS] '{text}'")
    t0 = time.perf_counter()

    # Step 1: G2P (gruut)
    t = time.perf_counter()
    phoneme_ids = text_to_piper_ids(text, verbose=verbose)
    print(f"  [1/4] G2P        {time.perf_counter()-t:.3f}s  "
          f"-> {len(phoneme_ids)} piper ids")

    # Step 2: Text Encoder
    t = time.perf_counter()
    m_p, logs_p, x_encoded, x_mask = run_encoder(models, phoneme_ids)
    n_real = int(x_mask.sum())
    print(f"  [2/4] Encoder    {time.perf_counter()-t:.3f}s  "
          f"-> m_p{list(m_p.shape)}  x_mask_nonzero={n_real}")

    # Step 3: SDP
    t = time.perf_counter()
    y_lengths, w_ceil = run_sdp(models, x_encoded, x_mask,
                                 noise_scale_w=noise_scale_w,
                                 length_scale=length_scale)
    y_len_sdp = int(float(y_lengths[0]))
    print(f"  [3/4] SDP        {time.perf_counter()-t:.3f}s  "
          f"-> y_lengths={y_len_sdp}")

    # Step 4: Flow + Decoder
    t = time.perf_counter()
    attn_squeezed, y_len = generate_path(w_ceil, len(phoneme_ids),
                                          y_len_sdp=y_len_sdp)
    z = run_flow(models, attn_squeezed, m_p, logs_p, y_len,
                 noise_scale=noise_scale)
    print(f"  [4/4] Flow+Dec   ", end="", flush=True)

    # Step 5: HiFi-GAN Decoder (overlap chunking)
    audio = run_decoder(models, z, y_len)
    dur   = len(audio) / SAMPLE_RATE
    print(f"{time.perf_counter()-t:.3f}s  "
          f"-> {len(audio)} samples ({dur:.2f}s)")

    total = time.perf_counter() - t0
    rtf   = total / max(dur, 1e-6)
    print(f"\n  Total inference: {total:.3f}s  RTF: {rtf:.3f}  "
          f"({'faster' if rtf < 1 else 'slower'} than real-time {1/rtf:.1f}x)")

    save_wav(audio, out_path)
    return audio


# ============================================================================
# 9. Entry Point
# ============================================================================

def parse_args():
    p = argparse.ArgumentParser(description="PiperTTS-EN HTP inference v10")
    p.add_argument("--text",
                   default="This is the demo of pipertts_en on WoS.")
    p.add_argument("--models_dir",    default=_DEFAULT_MODELS)
    p.add_argument("--out",
                   default=os.path.join(_SCRIPT_DIR, "output.wav"))
    p.add_argument("--noise_scale",   type=float, default=0.667)
    p.add_argument("--noise_scale_w", type=float, default=0.8)
    p.add_argument("--length_scale",  type=float, default=1.0)
    p.add_argument("--verbose",       action="store_true")
    p.add_argument("--skip_download", action="store_true",
                   help="Skip model download (use when models are already prepared)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()

    print("=" * 62)
    print("  PiperTTS-EN HTP inference  (v10 - auto model download)")
    print("=" * 62)
    print(f"  models_dir    : {args.models_dir}")
    print(f"  out           : {args.out}")
    print(f"  noise_scale   : {args.noise_scale}")
    print(f"  noise_scale_w : {args.noise_scale_w}")
    print(f"  length_scale  : {args.length_scale}")
    print()

    # Auto-download models (unless user specifies --skip_download)
    if not args.skip_download:
        ok = download_models(args.models_dir)
        if not ok:
            print("[ERROR] Model download failed, exiting.")
            sys.exit(1)
    else:
        print("[INFO] Skipping model download (--skip_download).")

    models = PiperTTSModels(args.models_dir)

    tts(models,
        text          = args.text,
        out_path      = args.out,
        noise_scale   = args.noise_scale,
        noise_scale_w = args.noise_scale_w,
        length_scale  = args.length_scale,
        verbose       = args.verbose)

    print()
    print("=" * 62)
    print(f"  Done! Output: {args.out}")
    print("=" * 62)
