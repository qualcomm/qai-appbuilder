"""
MeloTTS-ZH inference using qai_appbuilder — FINAL WORKING VERSION

See NOTES.md next to this file for the full setup guide. In particular, this
script will NOT work on a fresh machine unless you also do:
  * Step 3 — fetch melo_config_zh.json from HuggingFace (the upstream
    melo.download_utils URL is 403).
  * Step 4 — install melotts from a patched sdist (PyPI sdist is broken).
  * Step 5 — pin transformers==4.56.2 / tokenizers==0.22.2 /
    huggingface_hub==0.34.4 (ARM64 wheel availability is narrow).
  * Step 6 — three edits to melo/text/japanese.py (including the easily
    missed line ~367 `_TAGGER = MeCab.Tagger()`).

Key inference insight:
  - flow.bin and decoder.bin use uint16 quantization (from metadata.json)
  - QNN reports dtype as 'ufp16' but the bits are actually uint16
  - Input:  quantize float32 -> uint16, then .view(float16) to pass to QNN
  - Output: receive float16 from QNN, .view(uint16) to get uint16, then dequantize

Quantization parameters from metadata.json:
  quantize:   q = clip(round(x / scale) + zero_point, 0, 65535).astype(uint16)
  dequantize: x = (q.astype(float32) - zero_point) * scale
"""

import sys, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import types, importlib.util
import numpy as np
import torch
import torch.nn.functional as F
import soundfile as sf

# ── 1. Mock unavailable native deps ───────────────────────────────────────────
_ta = types.ModuleType("torchaudio")
_ta.__spec__ = importlib.util.spec_from_loader("torchaudio", loader=None)
_ta.__version__ = "0.0.0"
sys.modules.setdefault("torchaudio", _ta)

_numba = types.ModuleType("numba")
def _jit(*a, **kw):
    def _dec(fn): return fn
    return _dec if not (len(a) == 1 and callable(a[0])) else a[0]
class _NumbaType:
    def __getitem__(self, _key): return self   # supports int32[:, ::1] chained indexing
    def __call__(self, *a, **kw): return self  # supports void(...) calls
_nb_type = _NumbaType()
_numba.jit = _jit
_numba.void = _nb_type
_numba.int32 = _numba.int64 = _numba.float32 = _numba.float64 = _nb_type
sys.modules.setdefault("numba", _numba)

# soxr stub — no win_arm64 wheel; only used by transformers.audio_utils.load_audio
# (never called for TTS).
_soxr = types.ModuleType("soxr")
_soxr.__version__ = "0.0.0"
def _soxr_resample(*a, **kw):
    raise RuntimeError("soxr stub — audio resampling not available")
_soxr.resample = _soxr_resample
_soxr.VHQ = _soxr.HQ = _soxr.MQ = _soxr.LQ = _soxr.QQ = "HQ"
sys.modules.setdefault("soxr", _soxr)

# boto3 stub — pulled in by cached_path.schemes.r2, never actually used.
if "boto3" not in sys.modules:
    try:
        import boto3  # noqa
    except ImportError:
        _b = types.ModuleType("boto3"); _b.session = types.ModuleType("boto3.session")
        _b.dynamodb = types.ModuleType("boto3.dynamodb")
        sys.modules["boto3"] = _b
        sys.modules["boto3.session"] = _b.session
        sys.modules["boto3.dynamodb"] = _b.dynamodb

# Korean stub — melo.text.__init__ imports get_bert_feature from korean even for ZH TTS
_ko = types.ModuleType("melo.text.korean")
def _ko_get_bert_feature(*a, **kw):
    raise RuntimeError("Korean TTS not supported")
_ko.get_bert_feature = _ko_get_bert_feature
sys.modules["melo.text.korean"] = _ko

# ── 1b. Patch melo.download_utils BEFORE melo.api import ──────────────────────
# The default URLs (myshell S3 openvoice basespeakers) are 403 / gated.
# We already have config.json locally, and never use the PyTorch checkpoint
# (all inference goes through QNN encoder/flow/decoder .bin).
#
# Paths follow the aihub-model-run skill convention: C:/WoS_AI/<model>/.
# Override by setting the MELOTTS_WORK_DIR env var, or edit WORK_DIR below.
WORK_DIR = os.environ.get("MELOTTS_WORK_DIR", r"C:\WoS_AI\melotts_zh")

# Auto-pick the extracted chipset subdir (X Elite / X2 Elite / X Plus all
# extract to a chipset-suffixed folder).
def _find_model_dir(work_dir):
    if not os.path.isdir(work_dir):
        raise FileNotFoundError(f"WORK_DIR does not exist: {work_dir}")
    prefix = "melotts_zh-voice_ai-mixed_with_float-qualcomm_snapdragon_"
    for name in os.listdir(work_dir):
        full = os.path.join(work_dir, name)
        if os.path.isdir(full) and name.startswith(prefix):
            return full
    raise FileNotFoundError(
        f"No '{prefix}*' subdir under {work_dir}. Did you extract the ZIP?"
    )

MODEL_DIR = _find_model_dir(WORK_DIR)
MELO_CONFIG_JSON = os.path.join(WORK_DIR, "melo_config_zh.json")

_dl = types.ModuleType("melo.download_utils")
def _load_or_download_config(locale):
    from melo import utils as _mu
    return _mu.get_hparams_from_file(MELO_CONFIG_JSON)
def _load_or_download_model(locale, device):
    # Return a dummy checkpoint. The QNN encoder.bin already contains all
    # trained weights; the CPU torch model is only kept alive so that
    # get_text_for_tts_infer() can access tts_obj.hps and tts_obj.symbol_to_id.
    return {"model": {}}
_dl.load_or_download_config = _load_or_download_config
_dl.load_or_download_model = _load_or_download_model
sys.modules["melo.download_utils"] = _dl

# ── 2. qai_appbuilder ──────────────────────────────────────────────────────────
from qai_appbuilder import QNNContext, Runtime, LogLevel, ProfilingLevel, QNNConfig, PerfProfile
QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

# ── 3. Quantization parameters from metadata.json ─────────────────────────────
FLOW_IN_QUANT = {
    "attn_squeezed": (0.000030518509447574615, 32768),
    "logs_p":        (0.00003420173015911132,  22619),
    "noise_scale":   (0.000010177767762797885, 0),
    "m_p":           (0.00007962718518683687,  34950),
    "y_mask":        (0.000015259021893143654, 0),
    "g":             (0.000020079150999663398, 36547),
}
FLOW_OUT_QUANT = {
    "z": (0.00029126188019290566, 32329),
}
DEC_IN_QUANT = {
    "g": (0.000020079150999663398, 36547),
    "z": (0.0002923276915680617,   32350),
}
DEC_OUT_QUANT = {
    "audio": (0.000013070309250906575, 35961),
}

def quantize(x: np.ndarray, scale: float, zero_point: int) -> np.ndarray:
    """float32 -> uint16 viewed as float16 (for QNN native dtype)"""
    q = np.round(x.astype(np.float32) / scale) + zero_point
    return np.clip(q, 0, 65535).astype(np.uint16).view(np.float16)

def dequantize(raw_f16: np.ndarray, scale: float, zero_point: int) -> np.ndarray:
    """QNN native output (float16 bits containing uint16) -> float32"""
    q = raw_f16.view(np.uint16)
    return (q.astype(np.float32) - zero_point) * scale

# ── 4. Constants ───────────────────────────────────────────────────────────────
MAX_SEQ_LEN           = 512
UPSAMPLED_MAX_SEQ_LEN = 1536
MAX_DEC_SEQ_LEN       = 64
UPSAMPLE_FACTOR       = 512
SAMPLE_RATE           = 44100

# ── 5. QNN model wrappers ──────────────────────────────────────────────────────
class EncoderModel(QNNContext):
    """float32 I/O"""
    def Inference(self, sid, bert, ja_bert, x, tone, language,
                  x_lengths, noise_scale_w, sdp_ratio, length_scale):
        out = super().Inference([sid, bert, ja_bert, x, tone, language,
                                  x_lengths, noise_scale_w, sdp_ratio, length_scale])
        return [np.array(o) for o in out]

class FlowModel(QNNContext):
    """uint16 quantized I/O (QNN reports ufp16, bits are actually uint16)"""
    def Inference(self, attn_sq_f, logs_p_f, noise_scale_f, m_p_f, y_mask_f, g_f):
        inp = [
            quantize(attn_sq_f,    *FLOW_IN_QUANT["attn_squeezed"]),
            quantize(logs_p_f,     *FLOW_IN_QUANT["logs_p"]),
            quantize(noise_scale_f,*FLOW_IN_QUANT["noise_scale"]),
            quantize(m_p_f,        *FLOW_IN_QUANT["m_p"]),
            quantize(y_mask_f,     *FLOW_IN_QUANT["y_mask"]),
            quantize(g_f,          *FLOW_IN_QUANT["g"]),
        ]
        out = super().Inference(inp)
        return dequantize(np.array(out[0]), *FLOW_OUT_QUANT["z"])  # [1,192,1536] float32

class DecoderModel(QNNContext):
    """uint16 quantized I/O"""
    def Inference(self, g_f, z_f):
        inp = [
            quantize(g_f, *DEC_IN_QUANT["g"]),
            quantize(z_f, *DEC_IN_QUANT["z"]),
        ]
        out = super().Inference(inp)
        return dequantize(np.array(out[0]), *DEC_OUT_QUANT["audio"])  # [1,1,32768] float32

# ── 6. generate_path ──────────────────────────────────────────────────────────
def generate_path(duration, mask):
    b, _, t_y, t_x = mask.shape
    cum_duration = torch.cumsum(duration, -1)
    cum_flat = cum_duration.view(b * t_x)
    x_range = torch.arange(t_y, dtype=cum_flat.dtype)
    path = (x_range.unsqueeze(0) < cum_flat.unsqueeze(1)).to(mask.dtype)
    path = path.view(b, t_x, t_y)
    path = path - F.pad(path, [0, 0, 1, 0, 0, 0])[:, :-1]
    return path.unsqueeze(1).transpose(2, 3) * mask

# ── 7. Load MeloTTS for text preprocessing ────────────────────────────────────
print("[INFO] Loading MeloTTS object for text preprocessing...")
from melo.api import TTS as MeloTTS
# The CPU torch model weights are never used (all inference runs on QNN .bin);
# relax strict-loading so the empty dummy checkpoint is accepted.
import melo.models as _melo_models
_orig_load_sd = _melo_models.SynthesizerTrn.load_state_dict
def _lax_load_sd(self, state_dict, strict=True, **kw):
    return _orig_load_sd(self, state_dict, strict=False, **kw)
_melo_models.SynthesizerTrn.load_state_dict = _lax_load_sd

tts_obj = MeloTTS("ZH", device="cpu")
speaker_id = next(iter(tts_obj.hps.data.spk2id.values()))
print(f"[INFO] Speaker ID: {speaker_id}")

# ── 8. Load QNN models ─────────────────────────────────────────────────────────
print("[INFO] Loading encoder.bin ...")
encoder_model = EncoderModel(
    "encoder", os.path.join(MODEL_DIR, "encoder.bin"),
    input_data_type="float", output_data_type="float",
)
print("[INFO] Loading flow.bin ...")
flow_model = FlowModel(
    "flow", os.path.join(MODEL_DIR, "flow.bin"),
    input_data_type="native", output_data_type="native",
)
print("[INFO] Loading decoder.bin ...")
decoder_model = DecoderModel(
    "decoder", os.path.join(MODEL_DIR, "decoder.bin"),
    input_data_type="native", output_data_type="native",
)
print("[INFO] All models loaded.")

# ── 9. Text preprocessing ──────────────────────────────────────────────────────
def preprocess_text(text):
    from melo.utils import get_text_for_tts_infer
    bert_feat, ja_bert_feat, phones, tones, lang_ids = get_text_for_tts_infer(
        text, tts_obj.language, tts_obj.hps, "cpu", tts_obj.symbol_to_id
    )
    phone_len = phones.size(0)
    print(f"[PRE]   phone_len={phone_len}")

    def pad2d(t, n):
        c = t.size(1)
        return F.pad(t, (0, max(0, n-c)))[:, :n]

    def pad1d(t, n):
        c = t.size(0)
        return F.pad(t, (0, max(0, n-c)))[:n]

    phones       = pad1d(phones,       MAX_SEQ_LEN)
    tones        = pad1d(tones,        MAX_SEQ_LEN)
    lang_ids     = pad1d(lang_ids,     MAX_SEQ_LEN)
    bert_feat    = pad2d(bert_feat,    MAX_SEQ_LEN)   # [1024, 512]
    ja_bert_feat = pad2d(ja_bert_feat, MAX_SEQ_LEN)   # [768, 512]

    return phones, tones, lang_ids, bert_feat, ja_bert_feat, min(phone_len, MAX_SEQ_LEN)

# ── 10. Full TTS pipeline ──────────────────────────────────────────────────────
def synthesize(text: str, output_path: str,
               noise_scale: float = 0.667,
               length_scale: float = 1.0,
               noise_scale_w: float = 0.8,
               sdp_ratio: float = 0.2):

    print(f"\n[TTS] Input: {text}")

    # Step 1: Preprocess
    print("[TTS] Step 1/4: Text preprocessing...")
    phones, tones, lang_ids, bert_feat, ja_bert_feat, phone_len = preprocess_text(text)

    sid        = np.array([speaker_id],    dtype=np.int32)
    bert       = bert_feat.unsqueeze(0).numpy().astype(np.float32)    # [1,1024,512]
    ja_bert    = ja_bert_feat.unsqueeze(0).numpy().astype(np.float32) # [1,768,512]
    x          = phones.unsqueeze(0).numpy().astype(np.int32)
    tone       = tones.unsqueeze(0).numpy().astype(np.int32)
    language   = lang_ids.unsqueeze(0).numpy().astype(np.int32)
    x_lengths  = np.array([phone_len], dtype=np.int32)
    ns_w       = np.array([noise_scale_w], dtype=np.float32)
    sdp_r      = np.array([sdp_ratio],     dtype=np.float32)
    len_scale  = np.array([length_scale],  dtype=np.float32)

    # Raise HTP to BURST ONCE and hold it across ALL NPU stages (encoder →
    # flow → decoder). Do NOT Set/Rel per stage — that ramps the HTP clock
    # up/down between stages. Released once after the last stage below.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
    try:
        # Step 2: Encoder
        print("[TTS] Step 2/4: Encoder...")
        enc_out = encoder_model.Inference(
            sid, bert, ja_bert, x, tone, language, x_lengths, ns_w, sdp_r, len_scale
        )

        m_p      = enc_out[0]  # [1,192,512]
        logs_p   = enc_out[1]  # [1,192,512]
        w_ceil   = enc_out[2]  # [1,1,512]
        y_lengths= enc_out[3]  # [1]
        x_mask   = enc_out[4]  # [1,1,512]
        g        = enc_out[5]  # [1,256,1]
        y_len_int = int(y_lengths[0])
        print(f"[TTS]   y_lengths={y_len_int}, m_p=[{m_p.min():.3f},{m_p.max():.3f}]")

        # Build y_mask and attn_squeezed (CPU work; BURST stays held).
        y_mask_np = (
            torch.unsqueeze(
                torch.arange(UPSAMPLED_MAX_SEQ_LEN) < torch.tensor([y_len_int]).unsqueeze(-1),
                dim=1
            ).float().numpy()
        )  # [1,1,1536]

        attn_mask = (torch.from_numpy(x_mask).unsqueeze(2) *
                     torch.from_numpy(y_mask_np).unsqueeze(-1))
        attn = generate_path(torch.from_numpy(w_ceil), attn_mask)
        attn_squeezed = attn.squeeze(1).float().numpy()  # [1,1536,512]
        noise_scale_arr = np.array([noise_scale], dtype=np.float32)

        # Step 3: Flow
        print("[TTS] Step 3/4: Flow...")
        z = flow_model.Inference(attn_squeezed, logs_p, noise_scale_arr, m_p, y_mask_np, g)
        print(f"[TTS]   z shape={z.shape}, range=[{z.min():.3f}, {z.max():.3f}], mean={z.mean():.4f}")

        # Step 4: Decoder (chunked)
        print("[TTS] Step 4/4: Decoder (chunked)...")
        audio_chunks = []
        z_total = z.shape[2]  # 1536
        for start in range(0, z_total, MAX_DEC_SEQ_LEN):
            end = start + MAX_DEC_SEQ_LEN
            if end > z_total:
                z_slice = np.zeros([1, z.shape[1], MAX_DEC_SEQ_LEN], dtype=np.float32)
                z_slice[:, :, : z_total - start] = z[:, :, start:z_total]
            else:
                z_slice = z[:, :, start:end]
            chunk = decoder_model.Inference(g, z_slice)
            audio_chunks.append(chunk.flatten())
    finally:
        PerfProfile.RelPerfProfileGlobal()

    audio = np.concatenate(audio_chunks)
    audio = audio[: y_len_int * UPSAMPLE_FACTOR]

    # Sanity checks
    nan_count = np.isnan(audio).sum()
    if nan_count > 0:
        print(f"[WARN] audio has {nan_count} NaN — replacing with 0")
        audio = np.nan_to_num(audio, nan=0.0, posinf=0.0, neginf=0.0)

    peak = np.abs(audio).max()
    print(f"[TTS]   audio length={len(audio)}, range=[{audio.min():.4f}, {audio.max():.4f}]")

    if peak > 1.0:
        audio = audio / peak * 0.9

    sf.write(output_path, audio, samplerate=SAMPLE_RATE)
    dur = len(audio) / SAMPLE_RATE
    print(f"[TTS] Saved {dur:.2f}s -> {output_path}")
    return output_path


# ── 11. Run ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    TEXT   = "中文是中国的语言文字，包括汉语和汉字。"
    OUTPUT = os.path.join(WORK_DIR, "output.wav")
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)

    synthesize(TEXT, OUTPUT)
    print(f"\n[DONE] -> {OUTPUT}")
    del encoder_model, flow_model, decoder_model
