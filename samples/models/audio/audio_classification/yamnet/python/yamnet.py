# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
#
# Unified script for yamnet inference.
# Supports five platforms (auto-detected at runtime):
#   - wos        Windows on Snapdragon (ARM64) — HTP / GPU / CPU
#   - x86_win    Windows x86_64               — CPU only, DLC only
#   - arm64_linux  ARM64 Linux                — HTP / GPU / CPU
#   - x86_linux    x86_64 Linux               — HTP / GPU / CPU
#   - unknown    (falls back to CPU)
#
# Default runtime : HTP  (x86_win is always forced to CPU)
# Default model   : .bin (precompiled HTP context binary, auto-downloaded)
#
# CLI options:
#   --bin              Use .bin precompiled HTP context binary (default)
#   --dlc              Use float .dlc model (auto-downloaded)
#   --onnx             Use models/yamnet.onnx via OnnxRuntimeContext
#                      (onnxruntime_qnn HTP EP); auto-downloaded if absent.
#   --cpu              Use CPU runtime instead of HTP
#   --gpu              Use GPU runtime instead of HTP
#   --chipset <id>     Override SoC ID used for hub-model download
#   --input_audio_path <path>  Input audio file (default: <script_dir>/../assets/input.wav)
# ---------------------------------------------------------------------

import sys
import os
import platform
import argparse
import urllib.request
import zipfile
import shutil
import time

sys.path.append(".")
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "..", "shared", "python"))

import install

import csv
from pathlib import Path
import numpy as np
import soxr
import soundfile as sf
import torch

from qai_appbuilder import (
    QNNContext,
    OnnxRuntimeContext,
    Runtime,
    LogLevel,
    ProfilingLevel,
    PerfProfile,
    QNNConfig,
)

# ---------------------------------------------------------------------
# torchaudio replacement helpers
#
# torchaudio has no prebuilt wheel for Windows on ARM64, so instead of
# depending on `torchaudio.transforms` we reimplement the two operations that
# YAMNet preprocessing needs, using only `torch` (+ `soxr` for resampling):
#   1. Spectrogram + MelScale  (== torchaudio.transforms.MelSpectrogram)
#   2. Resample                (== torchaudio.transforms.Resample)
# The math mirrors torchaudio's defaults (HTK mel scale, no mel normalization,
# centered reflect-padded STFT, Hann window) so the numerical results match.
# ---------------------------------------------------------------------


def _hz_to_mel_htk(freq: float) -> float:
    """Convert Hz to mel using the HTK formula (torchaudio default)."""
    return 2595.0 * np.log10(1.0 + freq / 700.0)


def _mel_to_hz_htk(mels: np.ndarray) -> np.ndarray:
    """Convert mel to Hz using the HTK formula (torchaudio default)."""
    return 700.0 * (10.0 ** (mels / 2595.0) - 1.0)


def _create_mel_filterbank(
    n_freqs: int,
    f_min: float,
    f_max: float,
    n_mels: int,
    sample_rate: int,
) -> torch.Tensor:
    """Build a mel filterbank matching torchaudio.functional.melscale_fbanks
    with mel_scale="htk" and norm=None.

    Returns
    -------
    torch.Tensor of shape (n_freqs, n_mels).
    """
    # frequency of each STFT bin
    all_freqs = np.linspace(0, sample_rate // 2, n_freqs)

    # equally spaced points on the mel scale
    m_min = _hz_to_mel_htk(f_min)
    m_max = _hz_to_mel_htk(f_max)
    m_pts = np.linspace(m_min, m_max, n_mels + 2)
    f_pts = _mel_to_hz_htk(m_pts)          # (n_mels + 2,)

    # slopes between each filter's frequency points
    f_diff = np.diff(f_pts)                 # (n_mels + 1,)
    slopes = f_pts[np.newaxis, :] - all_freqs[:, np.newaxis]  # (n_freqs, n_mels+2)

    down_slopes = -slopes[:, :-2] / f_diff[np.newaxis, :-1]
    up_slopes = slopes[:, 2:] / f_diff[np.newaxis, 1:]
    fb = np.maximum(0.0, np.minimum(down_slopes, up_slopes))  # (n_freqs, n_mels)

    return torch.from_numpy(fb.astype(np.float32))


def _resample(waveform: torch.Tensor, orig_sr: int, target_sr: int) -> torch.Tensor:
    """Resample a [channels, time] torch tensor using soxr.

    Replaces torchaudio.transforms.Resample. soxr resamples along axis=0, so
    the [channels, time] tensor is transposed before/after the call.
    """
    if orig_sr == target_sr:
        return waveform
    x = waveform.detach().cpu().numpy().astype(np.float32)
    y = soxr.resample(x.T, orig_sr, target_sr).T
    return torch.from_numpy(np.ascontiguousarray(y, dtype=np.float32))

# ─────────────────────────────────────────────────────────────────────────────
# Model metadata
# ─────────────────────────────────────────────────────────────────────────────

# Constants previously from qai_hub_models.models.yamnet.model
YAMNET_PROXY_REPOSITORY = "https://github.com/w-hc/torch_audioset.git"
YAMNET_PROXY_REPO_COMMIT = "e8852c5"
MODEL_ASSET_VERSION = 1

SAMPLE_RATE = 16000
CHUNK_LENGTH = 0.98

MODEL_ID = "mm65xwe5n"
MODEL_NAME = "yamnet"
MODEL_HELP_URL = "https://github.com/qualcomm/qai-appbuilder/blob/main/samples/audio/Audio_Classification/yamnet/README.md"
YAMNET_CLASSES_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/yamnet/v1/yamnet_class_map.csv"
YAMNET_CLASSES_FILE = "yamnet_class_map.csv"

# Public DLC download URL (v0.59.0)
MODEL_DLC_FLOAT_URL = (
    "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/"
    "qai-hub-models/models/yamnet/releases/v0.59.0/"
    "yamnet-qnn_dlc-float.zip"
)

# Public ONNX download URL (v0.59.0)
MODEL_ONNX_FLOAT_URL = (
    "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/"
    "qai-hub-models/models/yamnet/releases/v0.59.0/"
    "yamnet-onnx-float.zip"
)

INPUT_WAV_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/yamnet/v1/speech_whistling2.wav"

# ─────────────────────────────────────────────────────────────────────────────
# Platform / device detection
# ─────────────────────────────────────────────────────────────────────────────

def _detect_platform():
    """Return one of: 'wos', 'x86_win', 'arm64_linux', 'x86_linux', 'unknown'."""
    system  = platform.system().lower()
    machine = platform.machine().lower()

    if system == "windows":
        if machine in ("aarch64", "arm64"):
            return "wos"          # Windows on Snapdragon
        else:
            return "x86_win"      # Regular x86_64 Windows
    if system == "linux":
        if machine in ("aarch64", "arm64"):
            return "arm64_linux"
        if machine in ("x86_64", "amd64"):
            return "x86_linux"
    return "unknown"

PLATFORM = _detect_platform()
print(f"[INFO] Detected platform: {PLATFORM}")

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

# Always resolve paths relative to this script's directory
execution_ws = Path(os.path.dirname(os.path.abspath(__file__)))

model_dir = execution_ws / ".." / "models"
model_path = model_dir / "{}.bin".format(MODEL_NAME)

yamnet_classes_path = model_dir / YAMNET_CLASSES_FILE

input_wav_path = execution_ws / ".." / "assets" / "input.wav"

# ─────────────────────────────────────────────────────────────────────────────
# Global state
# ─────────────────────────────────────────────────────────────────────────────

yamnet = None

# ─────────────────────────────────────────────────────────────────────────────
# Model class
# ─────────────────────────────────────────────────────────────────────────────

# YAMNET class which inherited from the class QNNContext.
class YamNet(QNNContext):
    def Inference(self, input_data):
        input_datas = [input_data]
        output_data = super().Inference(input_datas)
        return output_data


# ─────────────────────────────────────────────────────────────────────────────
# Model download helpers
# ─────────────────────────────────────────────────────────────────────────────

def _download_file(dest_path: Path, url: str, zip_filename: str, file_ext: str):
    """Download and extract a zip archive to dest_path if it does not already exist.

    Parameters
    ----------
    dest_path    : destination path for the extracted file
    url          : HTTPS URL of the zip archive
    zip_filename : local filename to use while downloading the zip
    file_ext     : file extension to look for inside the zip (e.g. '.dlc', '.onnx')

    Note: For ONNX models that use external data files (e.g. model.onnx + model.data),
    ALL files in the same directory as the located target file are copied to dest_path's
    parent directory so that the external data references remain valid.
    """
    if dest_path.is_file():
        print(f"[INFO] Model already exists: {dest_path}")
        return

    zip_path = execution_ws / zip_filename

    print(f"[INFO] Downloading model from:\n  {url}")
    try:
        urllib.request.urlretrieve(url, str(zip_path))
        print(f"[INFO] Download complete: {zip_path}")
    except Exception as e:
        print(f"[ERROR] Failed to download model: {e}")
        sys.exit(1)

    extract_dir = execution_ws / "_extract_tmp"
    print(f"[INFO] Extracting {zip_filename} ...")
    try:
        with zipfile.ZipFile(str(zip_path), "r") as zf:
            zf.extractall(str(extract_dir))
    except Exception as e:
        print(f"[ERROR] Failed to extract zip: {e}")
        zip_path.unlink(missing_ok=True)
        sys.exit(1)

    # Locate the target file inside the extracted tree
    found_file = None
    for root, _dirs, files in os.walk(str(extract_dir)):
        for fname in files:
            if fname.endswith(file_ext):
                found_file = Path(root) / fname
                break
        if found_file:
            break

    if not found_file:
        print(f"[ERROR] No {file_ext} file found in the extracted zip.")
        shutil.rmtree(str(extract_dir), ignore_errors=True)
        zip_path.unlink(missing_ok=True)
        sys.exit(1)

    model_dir.mkdir(parents=True, exist_ok=True)

    # Copy the primary model file to dest_path
    shutil.copy2(str(found_file), str(dest_path))
    print(f"[INFO] Copied {file_ext} to: {dest_path}")

    # Copy ALL sibling files in the same directory (e.g. ONNX external data files
    # like yamnet.data that are referenced by the .onnx model via relative paths).
    source_dir = found_file.parent
    for sibling in source_dir.iterdir():
        if sibling.is_file() and sibling != found_file:
            sibling_dest = dest_path.parent / sibling.name
            shutil.copy2(str(sibling), str(sibling_dest))
            print(f"[INFO] Copied companion file: {sibling.name} -> {sibling_dest}")

    # Cleanup
    shutil.rmtree(str(extract_dir), ignore_errors=True)
    zip_path.unlink(missing_ok=True)
    print(f"[INFO] Removed temporary zip: {zip_path}")


def _download_dlc_float(dlc_path: Path):
    """Download the float DLC."""
    _download_file(
        dlc_path,
        url=MODEL_DLC_FLOAT_URL,
        zip_filename="yamnet-qnn_dlc-float.zip",
        file_ext=".dlc",
    )


def _download_onnx_float(onnx_path: Path):
    """Download the float ONNX model."""
    _download_file(
        onnx_path,
        url=MODEL_ONNX_FLOAT_URL,
        zip_filename="yamnet-onnx-float.zip",
        file_ext=".onnx",
    )


def _download_bin(bin_path: Path, soc_id):
    """Download .bin model via QAI Hub if bin_path does not exist."""
    if bin_path.is_file():
        print(f"[INFO] BIN model already exists: {bin_path}")
        return

    desc = f"Downloading {MODEL_NAME} model... "
    fail = (
        f"\nFailed to download {MODEL_NAME} model. "
        f"Please prepare the model according to:\n{MODEL_HELP_URL}"
    )

    ret = install.download_qai_hubmodel(soc_id, MODEL_NAME, str(bin_path), desc=desc, fail=fail)

    if not ret:
        sys.exit(1)


def _ensure_classes_file():
    """Download the YAMNet class map CSV if not already present."""
    if not yamnet_classes_path.is_file():
        model_dir.mkdir(parents=True, exist_ok=True)
        print(f"[INFO] Downloading YAMNet class map from:\n  {YAMNET_CLASSES_URL}")
        ret = install.download_url(YAMNET_CLASSES_URL, yamnet_classes_path)
        if not ret:
            print(f"[ERROR] Failed to download YAMNet class map.")
            sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# ONNX / OnnxRuntimeContext init path (WoS HTP via onnxruntime_qnn)
# ─────────────────────────────────────────────────────────────────────────────

def _init_onnx_htp(onnx_path: Path, use_cpu: bool = False):
    """Initialise inference using OnnxRuntimeContext (onnxruntime_qnn HTP EP).

    If the ONNX model file does not exist, it is automatically downloaded.

    OnnxRuntimeContext (from qai_appbuilder.qnncontext) automatically:
      - Imports onnxruntime_qnn and registers the QNN Execution Provider.
      - Selects the HTP (NPU) backend via QAI_ORTQNN_BACKEND=htp (default).
      - Enables FP16 precision on HTP (enable_htp_fp16_precision=1).
      - Falls back to EPContext precompile workaround on layout-transform errors.
      - Falls back to CPU if QNN EP is unavailable.

    Environment variables that influence behaviour (all optional):
      QAI_ORT_ONNX_USE_ORTQNN   : "1" (default) to use onnxruntime_qnn
      QAI_ORTQNN_BACKEND         : "htp" (default) | "cpu" | "gpu"
      QAI_ORTQNN_ENABLE_HTP_FP16 : "1" (default) to enable FP16 precision
      QAI_ORTQNN_TRY_CONTEXT_CACHE : "1" (default) to retry with EPContext
    """
    global yamnet

    # Auto-download the ONNX model if it is missing.
    # Also re-download if the companion .data file is absent (ONNX external data format).
    data_path = onnx_path.with_suffix(".data")
    onnx_missing = not onnx_path.is_file()
    data_missing = not data_path.is_file()

    if onnx_missing or data_missing:
        if onnx_missing:
            print(f"[INFO] ONNX model not found: {onnx_path}")
        if data_missing:
            print(f"[INFO] ONNX external data file not found: {data_path}")
            # Remove the incomplete .onnx so _download_file will re-download
            if onnx_path.is_file():
                onnx_path.unlink()
        print(f"[INFO] Downloading float ONNX model ...")
        _download_onnx_float(onnx_path)

    # Ensure onnxruntime_qnn is used (OnnxRuntimeContext checks this env var).
    os.environ.setdefault("QAI_ORT_ONNX_USE_ORTQNN", "1")
    # Default backend: HTP (NPU).
    os.environ.setdefault("QAI_ORTQNN_BACKEND", "htp")
    # Enable FP16 precision on HTP for better performance.
    os.environ.setdefault("QAI_ORTQNN_ENABLE_HTP_FP16", "1")
    # Allow EPContext precompile workaround on layout-transform failures.
    os.environ.setdefault("QAI_ORTQNN_TRY_CONTEXT_CACHE", "1")

    print(f"[INFO] Loading ONNX model via OnnxRuntimeContext: {onnx_path}")
    yamnet = OnnxRuntimeContext(MODEL_NAME, str(onnx_path), use_cpu)

    provider_mode = yamnet.getProviderMode()
    print(f"[INFO] OnnxRuntimeContext provider mode: {provider_mode}")
    if provider_mode != "qnn-htp":
        print("[WARN] Model is NOT running on HTP (NPU). "
              "Check that onnxruntime_qnn is installed and the QNN EP is available.")

# ─────────────────────────────────────────────────────────────────────────────
# Init / Inference / Release
# ─────────────────────────────────────────────────────────────────────────────

def Init(use_cpu: bool = False, use_gpu: bool = False,
         use_bin: bool = False, use_dlc: bool = False,
         use_onnx: bool = False, soc_id=None):
    """Initialise the runtime and load the model.

    Parameters
    ----------
    use_cpu   : Use CPU runtime.  Mutually exclusive with use_gpu.
                Always forced True on x86_win.
    use_gpu   : Use GPU runtime.  Ignored on x86_win.
    use_bin   : Explicitly use .bin precompiled HTP context binary (default).
    use_dlc   : Use float .dlc model instead of .bin.
                On x86_win this is always the case regardless of this flag.
    use_onnx  : Use models/yamnet.onnx via OnnxRuntimeContext
                (onnxruntime_qnn HTP EP). The float ONNX is auto-downloaded
                if absent.
    soc_id    : SoC ID for hub-model downloader.
    """
    global yamnet

    model_dir.mkdir(parents=True, exist_ok=True)

    # Always ensure the class map CSV is available
    _ensure_classes_file()

    # ── x86_win: always CPU + DLC, ignore --bin / --gpu / --onnx ─────────────
    if PLATFORM == "x86_win":
        if use_gpu:
            print("[WARN] GPU runtime is not supported on x86_win; falling back to CPU.")
        if use_bin:
            print("[WARN] .bin model is not supported on x86_win; using .dlc.")
        if use_onnx:
            print("[WARN] --onnx is not supported on x86_win; using .dlc.")
        use_cpu  = True
        use_bin  = False
        use_onnx = False
        use_dlc  = True   # force DLC on x86_win

    # ── ONNX path: explicit --onnx flag ──────────────────────────────────────
    # When --onnx is requested, always use OnnxRuntimeContext regardless of
    # --cpu / --gpu.  The use_cpu flag is forwarded to OnnxRuntimeContext which
    # selects CPUExecutionProvider (--cpu) or the QNN HTP EP (default).
    if use_onnx:
        onnx_path = model_dir / f"{MODEL_NAME}.onnx"
        if use_cpu:
            print("[INFO] Runtime: CPU (onnxruntime CPUExecutionProvider via OnnxRuntimeContext)")
        elif use_gpu:
            print("[INFO] Runtime: GPU (fallback to QNNExecutionProvider currently)")
        else:
            print("[INFO] Runtime: HTP (onnxruntime_qnn via OnnxRuntimeContext)")
        _init_onnx_htp(onnx_path, use_cpu)
        return

    # ── Decide runtime ────────────────────────────────────────────────────────
    if use_cpu:
        runtime = Runtime.CPU
        print("[INFO] Runtime: CPU")
    elif use_gpu:
        runtime = Runtime.GPU
        print("[INFO] Runtime: GPU")
    else:
        runtime = Runtime.HTP
        print("[INFO] Runtime: HTP")

    # ── Decide model file ─────────────────────────────────────────────────────
    dlc_path = model_dir / f"{MODEL_NAME}.dlc"
    bin_path = model_dir / f"{MODEL_NAME}.bin"

    if use_dlc:
        # User explicitly requested .dlc (float DLC model)
        if not dlc_path.is_file():
            print(f"[INFO] DLC model not found ({dlc_path.name}), downloading...")
            _download_dlc_float(dlc_path)
        model_path_to_use = dlc_path
        print(f"[INFO] Using DLC model: {model_path_to_use}")
    else:
        # Default: .bin (precompiled HTP context binary)
        if not bin_path.is_file():
            print("[INFO] BIN model not found, downloading via hub...")
            try:
                _download_bin(bin_path, soc_id)
            except SystemExit:
                # Hub download failed; fall back to float DLC
                print("[INFO] Hub download failed, falling back to float DLC...")
                if not dlc_path.is_file():
                    _download_dlc_float(dlc_path)
                model_path_to_use = dlc_path
                print(f"[INFO] Using DLC model (fallback): {model_path_to_use}")
                _finish_init(model_path_to_use, runtime)
                return
        model_path_to_use = bin_path
        print(f"[INFO] Using BIN model: {model_path_to_use}")

    _finish_init(model_path_to_use, runtime)


def _finish_init(model_path_to_use: Path, runtime):
    """Configure QNN and instantiate the model."""
    global yamnet

    # ── Configure QNN ─────────────────────────────────────────────────────────
    QNNConfig.Config(runtime, LogLevel.WARN, ProfilingLevel.BASIC)

    # ── Instantiate model ─────────────────────────────────────────────────────
    yamnet = YamNet("yamnet", str(model_path_to_use))


def Inference(input_audio_path):
    # Load the audio.
    audio, audio_sample_rate = load_audiofile(input_audio_path)

    for segment in chunk_and_resample_audio(audio, audio_sample_rate):
        segment = torch.tensor(segment)
        patches, spectrogram = preprocessing_yamnet_from_source(segment)
        input_patches = patches.numpy()

    is_onnx = isinstance(yamnet, OnnxRuntimeContext) or (
        hasattr(yamnet, "isOnnxModel") and yamnet.isOnnxModel()
    )

    # Burst the HTP (only for QNNContext path).
    if not is_onnx:
        PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    print(f"[INFO] Starting inference...")
    t0 = time.perf_counter()

    accu = []
    if is_onnx:
        # OnnxRuntimeContext.Inference(input_list) -> list of output arrays
        raw_pred = yamnet.Inference([input_patches])
        # raw_pred is a list; take the first output tensor
        accu.append(raw_pred[0])
    else:
        raw_pred = yamnet.Inference(input_patches)
        accu.append(raw_pred)

    elapsed = time.perf_counter() - t0
    print(f"[INFO] Inference completed in {elapsed * 1000:.1f} ms ({elapsed:.2f} seconds)")

    accu = np.stack(accu)

    # Reset the HTP.
    if not is_onnx:
        PerfProfile.RelPerfProfileGlobal()

    # show the Top 5 predictions for this audio
    result = post_process(accu)

    return result


def post_process(accuracy):
    print("accuracy shape:", np.array(accuracy).shape)
    mean_scores = np.mean(accuracy, axis=1)  # Average over the time dimension

    # Squeeze out the batch or redundant dimensions and reduce to a 1D vector [C]
    mean_scores = np.squeeze(mean_scores)    # e.g. [C]
    mean_scores = mean_scores.ravel()

    top_N = 5
    top_class_indices = np.argsort(mean_scores)[::-1][:top_N]  # 1D index

    actions = parse_category_meta()  # list[str], length is C
    top5_classes = [actions[int(idx)] for idx in top_class_indices]
    top5_classes_str = " | ".join(top5_classes)

    print(f"Top 5 predictions:\n{top5_classes_str}\n")
    return top5_classes_str


def Release():
    global yamnet

    # Release the resources.
    del yamnet


# ─────────────────────────────────────────────────────────────────────────────
# Audio preprocessing helpers
# ─────────────────────────────────────────────────────────────────────────────

def preprocessing_yamnet_from_source(waveform_for_torch: torch.Tensor):
    """
    Args:
        waveform (torch.Tensor): Tensor of audio of dimension (..., time)

    Returns:
        patches : batched torch tsr of shape [N, C, T]
        spectrogram :  Mel frequency spectrogram of size (..., ``n_mels``, time)
    """
    #  This is a _log_ mel-spectrogram transform that adheres to the transform
    #  used by Google's vggish model input processing pipeline
    patches, spectrogram = WaveformToInput().wavform_to_log_mel(
        waveform_for_torch, SAMPLE_RATE
    )

    return patches, spectrogram


def parse_category_meta():
    """Read the class name definition file and return a list of strings."""
    accu = []
    with open(yamnet_classes_path) as csv_file:
        reader = csv.reader(csv_file)
        next(reader)  # Skip header
        for (inx, category_id, category_name) in reader:
            accu.append(category_name)
    return accu


def chunk_and_resample_audio(
    audio: np.ndarray,
    audio_sample_rate: int,
    model_sample_rate=SAMPLE_RATE,
    model_chunk_seconds=CHUNK_LENGTH,
) -> list[np.ndarray]:
    """
    Parameters
    ----------
    audio: str
        Raw audio numpy array of shape [# of samples]

    audio_sample_rate: int
        Sample rate of audio array, in samples / sec.

    model_sample_rate: int
        Sample rate (samples / sec) required to run Yamnet. The audio file
        will be resampled to use this rate.

    model_chunk_seconds: int
        Split the audio in to N sequences of this many seconds.
        The final split may be shorter than this many seconds.

    Returns
    -------
        List of audio arrays, chunked into N arrays of model_chunk_seconds seconds.
    """
    if audio_sample_rate != model_sample_rate:
        # `audio` has shape [channels, samples] (channel-first), while soxr
        # resamples along axis=0 (the sample/time axis). Transpose to
        # [samples, channels] before resampling and transpose back after.
        audio = soxr.resample(
            audio.T, audio_sample_rate, model_sample_rate
        ).T
        audio_sample_rate = model_sample_rate
    number_of_full_length_audio_chunks = int(
        audio.shape[1] // audio_sample_rate // model_chunk_seconds
    )
    last_sample_in_full_length_audio_chunks = int(
        audio_sample_rate * number_of_full_length_audio_chunks * model_chunk_seconds
    )
    if number_of_full_length_audio_chunks == 0:
        return [audio]

    return [
        *np.array_split(
            audio[:, :last_sample_in_full_length_audio_chunks],
            number_of_full_length_audio_chunks,
            axis=1,
        ),
    ]


def load_audiofile(path: str | Path):
    """
    Decode the WAV file.
        Parameters:
            path: Path of the input audio.

        Returns:
            x: Reads audio sample from path and converts to torch tensor.
            sr : sampling rate of audio samples

    """
    x, sr = sf.read(path, dtype="int16", always_2d=True)
    x = x / 2**15
    x = x.T.astype(np.float32)
    # Convert to mono and the sample rate expected by YAMNet.
    if x.shape[0] > 1:
        x = np.mean(x, axis=1)
    return x, sr


class CommonParams():
    # for STFT
    TARGET_SAMPLE_RATE = 16000
    STFT_WINDOW_LENGTH_SECONDS = 0.025
    STFT_HOP_LENGTH_SECONDS = 0.010

    # for log mel spectrogram
    NUM_MEL_BANDS = 64
    MEL_MIN_HZ = 125
    MEL_MAX_HZ = 7500
    LOG_OFFSET = 0.001  # NOTE 0.01 for vggish, and 0.001 for yamnet

    # convert input audio to segments
    PATCH_WINDOW_IN_SECONDS = 0.96

    # largest feedforward chunk size at test time
    VGGISH_CHUNK_SIZE = 128
    YAMNET_CHUNK_SIZE = 256

    # num of data loading threads
    NUM_LOADERS = 4

    #YAMNetParams
    PATCH_HOP_SECONDS = 0.48
    PATCH_WINDOW_SECONDS = 0.96


class VGGishLogMelSpectrogram(torch.nn.Module):
    '''
    This is a _log_ mel-spectrogram transform that adheres to the transform
    used by Google's vggish model input processing pipeline.

    Reimplemented with pure torch (no torchaudio dependency). It reproduces
    torchaudio.transforms.MelSpectrogram with the same defaults:
      * centered, reflect-padded Hann-window STFT
      * power spectrogram (power=2.0)
      * HTK mel scale, no mel normalization
    '''

    def __init__(self, sample_rate, n_fft, win_length, hop_length,
                 f_min, f_max, n_mels):
        super().__init__()
        self.n_fft = n_fft
        self.win_length = win_length
        self.hop_length = hop_length
        self.register_buffer("window", torch.hann_window(win_length))
        fb = _create_mel_filterbank(
            n_freqs=n_fft // 2 + 1,
            f_min=f_min,
            f_max=f_max,
            n_mels=n_mels,
            sample_rate=sample_rate,
        )
        self.register_buffer("mel_fb", fb)

    def forward(self, waveform):
        r"""
        Args:
            waveform (torch.Tensor): Tensor of audio of dimension (..., time)

        Returns:
            torch.Tensor: Mel frequency spectrogram of size (..., ``n_mels``, time)
        """
        # STFT -> power spectrogram (torchaudio default power=2.0)
        stft = torch.stft(
            waveform,
            n_fft=self.n_fft,
            hop_length=self.hop_length,
            win_length=self.win_length,
            window=self.window,
            center=True,
            pad_mode="reflect",
            normalized=False,
            return_complex=True,
        )
        specgram = stft.abs() ** 2   # (..., n_freqs, time)

        # NOTE at mel_features.py:98, googlers used np.abs on fft output and
        # as a result, the output is just the norm of spectrogram raised to power 1
        # For torchaudio.MelSpectrogram, however, the default
        # power for its spectrogram is 2.0. Hence we need to sqrt it.
        specgram = specgram ** 0.5

        # Apply mel filterbank: (n_freqs, n_mels)^T @ (..., n_freqs, time)
        # -> (..., n_mels, time)
        mel_specgram = torch.matmul(
            self.mel_fb.transpose(0, 1), specgram
        )
        mel_specgram = torch.log(mel_specgram + CommonParams.LOG_OFFSET)
        return mel_specgram


class WaveformToInput(torch.nn.Module):
    #def __init__(self):
        #super().__init__()
    global mel_trans_ope
    audio_sample_rate = CommonParams.TARGET_SAMPLE_RATE
    window_length_samples = int(round(
        audio_sample_rate * CommonParams.STFT_WINDOW_LENGTH_SECONDS
    ))
    hop_length_samples = int(round(
        audio_sample_rate * CommonParams.STFT_HOP_LENGTH_SECONDS
    ))
    fft_length = 2 ** int(np.ceil(np.log(window_length_samples) / np.log(2.0)))
    assert window_length_samples == 400
    assert hop_length_samples == 160
    assert fft_length == 512
    mel_trans_ope = VGGishLogMelSpectrogram(
        CommonParams.TARGET_SAMPLE_RATE, n_fft=fft_length,
        win_length=window_length_samples, hop_length=hop_length_samples,
        f_min=CommonParams.MEL_MIN_HZ,
        f_max=CommonParams.MEL_MAX_HZ,
        n_mels=CommonParams.NUM_MEL_BANDS
    )
    # note that the STFT filtering logic is exactly the same as that of a
    # conv kernel. It is the center of the kernel, not the left edge of the
    # kernel that is aligned at the start of the signal.

    def __call__(self, waveform, sample_rate):
        '''
        Args:
            waveform: torch tsr [num_audio_channels, num_time_steps]
            sample_rate: per second sample rate
        Returns:
            batched torch tsr of shape [N, C, T]
                '''
        x = waveform.mean(axis=0, keepdims=True)  # average over channels
        x = _resample(x, sample_rate, CommonParams.TARGET_SAMPLE_RATE)
        x = mel_trans_ope(x)
        x = x.squeeze(dim=0).T  # # [1, C, T] -> [T, C]

        window_size_in_frames = int(round(
            CommonParams.PATCH_WINDOW_IN_SECONDS / CommonParams.STFT_HOP_LENGTH_SECONDS
        ))
        num_chunks = x.shape[0] // window_size_in_frames

        # reshape into chunks of non-overlapping sliding window
        num_frames_to_use = num_chunks * window_size_in_frames
        x = x[:num_frames_to_use]
        # [num_chunks, 1, window_size, num_freq]
        x = x.reshape(num_chunks, 1, window_size_in_frames, x.shape[-1])
        return x

    def wavform_to_log_mel(self, waveform, sample_rate):
        '''
        Args:
            waveform: torch tsr [num_audio_channels, num_time_steps]
            sample_rate: per second sample rate
        Returns:
            batched torch tsr of shape [N, C, T]
                '''
        x = waveform.mean(axis=0, keepdims=True)  # average over channels
        x = _resample(x, sample_rate, CommonParams.TARGET_SAMPLE_RATE)
        x = mel_trans_ope(x)
        x = x.squeeze(dim=0).T  # # [1, C, T] -> [T, C]
        spectrogram = x.cpu().numpy().copy()

        window_size_in_frames = int(round(
            CommonParams.PATCH_WINDOW_IN_SECONDS / CommonParams.STFT_HOP_LENGTH_SECONDS
        ))

        if CommonParams.PATCH_HOP_SECONDS == CommonParams.PATCH_WINDOW_SECONDS:
            num_chunks = x.shape[0] // window_size_in_frames

            # reshape into chunks of non-overlapping sliding window
            num_frames_to_use = num_chunks * window_size_in_frames
            x = x[:num_frames_to_use]
            # [num_chunks, 1, window_size, num_freq]
            x = x.reshape(num_chunks, 1, window_size_in_frames, x.shape[-1])
        else:  # generate chunks with custom sliding window length `patch_hop_seconds`
            patch_hop_in_frames = int(round(
                CommonParams.PATCH_HOP_SECONDS / CommonParams.STFT_HOP_LENGTH_SECONDS
            ))
            # TODO performance optimization with zero copy
            patch_hop_num_chunks = (x.shape[0] - window_size_in_frames) // patch_hop_in_frames + 1
            num_frames_to_use = window_size_in_frames + (patch_hop_num_chunks - 1) * patch_hop_in_frames
            x = x[:num_frames_to_use]
            x_in_frames = x.reshape(-1, x.shape[-1])
            x_output = np.empty((patch_hop_num_chunks, window_size_in_frames, x.shape[-1]))
            for i in range(patch_hop_num_chunks):
                start_frame = i * patch_hop_in_frames
                x_output[i] = x_in_frames[start_frame: start_frame + window_size_in_frames]
            x = x_output.reshape(patch_hop_num_chunks, 1, window_size_in_frames, x.shape[-1])
            x = torch.tensor(x, dtype=torch.float32)
        return x, spectrogram


# ─────────────────────────────────────────────────────────────────────────────
# Debug helpers
# ─────────────────────────────────────────────────────────────────────────────

def getGraphName():
    print("[DEBUG] graph_name     :", yamnet.getGraphName())

def getInputShapes():
    print("[DEBUG] input_shapes   :", yamnet.getInputShapes())

def getInputDataType():
    print("[DEBUG] input_dataType :", yamnet.getInputDataType())

def getOutputShapes():
    print("[DEBUG] output_shapes  :", yamnet.getOutputShapes())

def getOutputDataType():
    print("[DEBUG] output_dataType:", yamnet.getOutputDataType())

def getInputName():
    print("[DEBUG] input_name     :", yamnet.getInputName())

def getOutputName():
    print("[DEBUG] output_name    :", yamnet.getOutputName())


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def main(input=None, use_cpu=False, use_gpu=False,
         use_bin=False, use_dlc=False, use_onnx=False,
         soc_id=None):

    if input is None:
        if not os.path.exists(input_wav_path):
            ret = install.download_url(INPUT_WAV_PATH_URL, input_wav_path)
        input = input_wav_path

    Init(use_cpu=use_cpu, use_gpu=use_gpu,
         use_bin=use_bin, use_dlc=use_dlc,
         use_onnx=use_onnx, soc_id=soc_id)

    # Print model debug info
    getGraphName()
    getInputShapes()
    getInputDataType()
    getOutputShapes()
    getOutputDataType()
    getInputName()
    getOutputName()

    result = Inference(input)

    Release()

    return result


if __name__ == "__main__":
    _platform_note = {
        "wos":         "Windows on Snapdragon (ARM64) -- supports HTP / GPU / CPU",
        "x86_win":     "Windows x86_64 -- CPU only, DLC only",
        "arm64_linux": "ARM64 Linux -- supports HTP / GPU / CPU",
        "x86_linux":   "x86_64 Linux -- supports HTP / GPU / CPU",
        "unknown":     "Unknown platform -- falls back to CPU",
    }.get(PLATFORM, PLATFORM)

    parser = argparse.ArgumentParser(
        description=(
            f"yamnet unified inference script\n"
            f"Detected platform : {PLATFORM}  ({_platform_note})\n"
            f"Default runtime   : {'CPU (forced)' if PLATFORM == 'x86_win' else 'HTP'}\n"
            f"Default model     : .bin (precompiled HTP context binary, auto-downloaded)\n"
            f"                    Use --dlc for float DLC (auto-downloaded),\n"
            f"                    --onnx for float ONNX (auto-downloaded)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--cpu",
        action="store_true",
        help="Use CPU runtime instead of HTP (always active on x86_win)",
    )
    parser.add_argument(
        "--gpu",
        action="store_true",
        help="Use GPU runtime instead of HTP (not supported on x86_win)",
    )
    parser.add_argument(
        "--onnx",
        action="store_true",
        help=(
            "Use models/yamnet.onnx via OnnxRuntimeContext "
            "(onnxruntime_qnn HTP EP). The float ONNX is auto-downloaded "
            "if the file is absent."
        ),
    )
    parser.add_argument(
        "--bin",
        action="store_true",
        help=(
            "Use .bin precompiled HTP context binary (default behaviour)"
        ),
    )
    parser.add_argument(
        "--dlc",
        action="store_true",
        help=(
            "Use float .dlc model (auto-downloaded; "
            "always active on x86_win)"
        ),
    )
    parser.add_argument(
        "--chipset",
        default=None,
        metavar="SOC_ID",
        help="SoC ID for hub-model download (e.g. '43')",
    )
    parser.add_argument(
        "--input_audio_path",
        default=None,
        help="Path to the input audio file (default: <script_dir>/../assets/input.wav)",
    )

    args = parser.parse_args()

    # --cpu and --gpu are mutually exclusive
    if args.cpu and args.gpu:
        parser.error("--cpu and --gpu are mutually exclusive.")

    # --bin and --dlc are mutually exclusive
    if getattr(args, "bin") and args.dlc:
        parser.error("--bin and --dlc are mutually exclusive.")

    # --onnx is mutually exclusive with --bin and --dlc
    if args.onnx and (getattr(args, "bin") or args.dlc):
        parser.error("--onnx cannot be combined with --bin or --dlc.")

    main(
        input         = args.input_audio_path,
        use_cpu       = args.cpu,
        use_gpu       = args.gpu,
        use_bin       = getattr(args, "bin"),
        use_dlc       = args.dlc,
        use_onnx      = args.onnx,
        soc_id        = args.chipset,
    )
