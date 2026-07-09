# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import os
import sys
import argparse
import time
import zipfile
import shutil
import threading
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
from PIL import Image

from qai_appbuilder import QNNContext, Runtime, LogLevel, QNNConfig, PerfProfile, ProfilingLevel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..', 'common'))
from install import get_cpu_name, detect_device_model

# Try to import torch-dependent modules, but make them optional
try:
    import torch
    import torch.nn.functional as F
    from transformers import CLIPTokenizer
    from diffusers import FlowMatchEulerDiscreteScheduler
    from diffusers.models.embeddings import CombinedTimestepTextProjEmbeddings
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False
    print("Warning: torch/transformers/diffusers not available, will use numpy fallbacks")

####################################################################

SOC_ID = None
cleaned_argv = []
i = 0
while i < len(sys.argv):
    if sys.argv[i] == '--chipset':
        SOC_ID = sys.argv[i + 1]
        i += 2
    else:
        cleaned_argv.append(sys.argv[i])
        i += 1

sys.argv = cleaned_argv

print(f"SOC_ID: {SOC_ID}")

MODEL_NAME = "stable_diffusion_v3_5"

####################################################################
# Device detection
####################################################################

# Detect device and set download URL accordingly
_DEVICE_MODEL = detect_device_model()

if _DEVICE_MODEL == "snapdragon_x2_elite":
    MODEL_DOWNLOAD_URL = "https://www.aidevhome.com/data/adh2/models/suggested/sd3.5_qnn_for_windows-8480.zip"
    MODEL_ZIP_NAME     = "sd3.5_qnn_for_windows-8480.zip"
    MODEL_ROOT_NAME    = "sd3.5_qnn_for_windows-8480"
else:
    # snapdragon_x_elite (default)
    MODEL_DOWNLOAD_URL = "https://www.aidevhome.com/data/adh2/models/suggested/sd3.5_qnn_for_windows-8380.zip"
    MODEL_ZIP_NAME     = "sd3.5_qnn_for_windows-8380.zip"
    MODEL_ROOT_NAME    = "sd3.5_qnn_for_windows-8380"

print(f"[INFO] Device model: {_DEVICE_MODEL}, using model: {MODEL_ZIP_NAME}")

####################################################################
# Path configuration
####################################################################
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Rename the extracted directory to 'models'
MODEL_ROOT   = os.path.join(_SCRIPT_DIR, "models")
BINS_DIR     = os.path.join(MODEL_ROOT, "serialized_binaries")

TOKENIZER_DIR   = os.path.join(MODEL_ROOT, "tokenizer")
TOKENIZER_2_DIR = os.path.join(MODEL_ROOT, "tokenizer_2")
TIME_TEXT_EMBED = os.path.join(MODEL_ROOT, "time_text_embed.pt")

TEXT_ENCODER_BIN   = os.path.join(BINS_DIR, "text_encoder.serialized.bin")
TEXT_ENCODER_2_BIN = os.path.join(BINS_DIR, "text_encoder_2.serialized.bin")
TRANSFORMER_BIN    = os.path.join(BINS_DIR, "transformer.serialized.bin")
VAE_DECODER_BIN    = os.path.join(BINS_DIR, "vae_decoder.serialized.bin")

####################################################################
# Fixed model parameters
####################################################################
LATENT_H     = 128
LATENT_W     = 128
LATENT_C     = 16
IMG_H        = 1024
IMG_W        = 1024
CLIP_SEQ_LEN = 77
T5_SEQ_LEN   = 83
CONTEXT_DIM  = 4096
PATCH_SIZE   = 2
VAE_SCALE    = 1.5305
VAE_SHIFT    = 0.0609

####################################################################
# Global objects / parameters
####################################################################
tokenizer       = None
tokenizer_2     = None
time_text_embed = None
scheduler       = None

text_encoder    = None  # CLIP-L
text_encoder_2  = None  # CLIP-G
vae_decoder     = None
transformer     = None

# User-configurable parameters (default values managed by add_argument)
user_prompt          = ""
user_negative_prompt = ""
user_seed            = None
user_step            = None
user_cfg             = None

model_inited = False

####################################################################
# qai_appbuilder model wrappers
####################################################################

def ensure_aligned_buffer(arr, alignment=32):
    """Ensure buffer meets QNN alignment requirements (default 32-byte alignment)"""
    arr = np.ascontiguousarray(arr, dtype=np.float32)
    # Check if already aligned
    if arr.ctypes.data % alignment == 0:
        return arr
    # If not aligned, create a new aligned buffer
    aligned = np.empty(arr.shape, dtype=np.float32, order='C')
    aligned[:] = arr
    return aligned


class TextEncoderCLIPL(QNNContext):
    """CLIP-L text encoder via qai_appbuilder"""
    def __init__(self, model_name, model_path):
        super().__init__(model_name, model_path)

    def Inference(self, token_ids_f32):
        inp = np.ascontiguousarray(token_ids_f32.flatten(), dtype=np.float32)
        outputs = super().Inference([inp])
        # 768 = text_embeds, 77*768 = hidden_states
        if outputs[0].size == 768:
            return outputs[1], outputs[0]  # hidden_states, text_embeds
        else:
            return outputs[0], outputs[1]


class TextEncoderCLIPG(QNNContext):
    """CLIP-G text encoder via qai_appbuilder"""
    def __init__(self, model_name, model_path):
        super().__init__(model_name, model_path)

    def Inference(self, token_ids_f32):
        inp = np.ascontiguousarray(token_ids_f32.flatten(), dtype=np.float32)
        outputs = super().Inference([inp])
        # 1280 = text_embeds, 77*1280 = hidden_states
        if outputs[0].size == 1280:
            return outputs[1], outputs[0]
        else:
            return outputs[0], outputs[1]


class VaeDecoder(QNNContext):
    """VAE Decoder via qai_appbuilder"""
    def __init__(self, model_name, model_path):
        super().__init__(model_name, model_path)

    def Inference(self, sample_nhwc):
        inp = np.ascontiguousarray(sample_nhwc.flatten(), dtype=np.float32)
        outputs = super().Inference([inp])
        return outputs[0]


class TransformerQNN(QNNContext):
    """SD3.5 Transformer via qai_appbuilder"""
    def __init__(self, model_name, model_path):
        super().__init__(model_name, model_path)

    def Inference(self, temb, hidden_states_nhwc, encoder_hidden_states):
        # Ensure all inputs are float32 and C-contiguous
        temb = np.ascontiguousarray(temb, dtype=np.float32)
        hidden_states_nhwc = np.ascontiguousarray(hidden_states_nhwc, dtype=np.float32)
        encoder_hidden_states = np.ascontiguousarray(encoder_hidden_states, dtype=np.float32)

        # Flatten all inputs to 1D
        temb_flat = temb.reshape(-1)
        hidden_states_flat = hidden_states_nhwc.reshape(-1)
        encoder_hidden_states_flat = encoder_hidden_states.reshape(-1)

        # Determine the correct input order by matching flattened sizes to model input shapes.
        # Model input shapes are queried once and cached to avoid repeated calls.
        input_shapes = self.getInputShapes()

        # Build a size->flat_array map for the three inputs
        size_to_array = {
            temb_flat.size: temb_flat,
            hidden_states_flat.size: hidden_states_flat,
            encoder_hidden_states_flat.size: encoder_hidden_states_flat,
        }

        # Order inputs according to the model's declared input shapes
        import math
        ordered_inputs = []
        for shape in input_shapes:
            n = math.prod(shape)
            if n not in size_to_array:
                raise ValueError(
                    f"No input array with size {n} (shape {shape}). "
                    f"Available sizes: {list(size_to_array.keys())}"
                )
            ordered_inputs.append(size_to_array[n])

        try:
            output_data = super().Inference(ordered_inputs)[0]

            # Reshape output to (1, 4096, 64) for unpatchify_noise_pred
            expected_size = 1 * 4096 * 64  # 262144
            if output_data.size == expected_size:
                output_data = output_data.reshape(1, 4096, 64)
            else:
                print(f"[WARNING] Transformer output size {output_data.size} != {expected_size}, returning as-is")

            return output_data
        except Exception as e:
            print(f"[ERROR] Transformer inference failed: {e}")
            import traceback
            traceback.print_exc()
            raise


####################################################################
# Pre-processing
####################################################################

def tokenize(prompt, tok, max_length=CLIP_SEQ_LEN):
    ids = tok(prompt, padding="max_length", max_length=max_length, truncation=True).input_ids
    return np.array(ids, dtype=np.float32).reshape(1, max_length)


def build_encoder_hidden_states(hidden1_flat, hidden2_flat):
    """CLIP-L(1,77,768) + CLIP-G(1,77,1280) -> (1,160,4096)"""
    h1 = hidden1_flat.reshape(1, CLIP_SEQ_LEN, -1)
    h2 = hidden2_flat.reshape(1, CLIP_SEQ_LEN, -1)
    clip_combined = np.concatenate([h1, h2], axis=-1)
    # Pad to CONTEXT_DIM
    pad_width = ((0, 0), (0, 0), (0, CONTEXT_DIM - clip_combined.shape[-1]))
    clip_padded = np.pad(clip_combined, pad_width, mode='constant', constant_values=0)
    t5_zeros = np.zeros((1, T5_SEQ_LEN, CONTEXT_DIM), dtype=np.float32)
    return np.concatenate([clip_padded, t5_zeros], axis=1).astype(np.float32)


def build_pooled_projections(pooled1_flat, pooled2_flat):
    """CLIP-L pooled(768) + CLIP-G pooled(1280) -> (1,2048) array"""
    p1 = pooled1_flat.reshape(1, -1)
    p2 = pooled2_flat.reshape(1, -1)
    return np.concatenate([p1, p2], axis=-1).astype(np.float32)


def get_temb(timestep_value, pooled_proj):
    if not TORCH_AVAILABLE:
        raise RuntimeError("torch is required for get_temb")
    t = torch.tensor([timestep_value], dtype=torch.float32)
    with torch.no_grad():
        temb = time_text_embed(t, torch.from_numpy(pooled_proj).float())
    result = temb.numpy().astype(np.float32)
    print(f"[DEBUG] get_temb output shape: {result.shape}")
    return result


####################################################################
# Post-processing
####################################################################

def unpatchify_noise_pred(noise_pred_flat):
    """(1,4096,64) patchified -> (1,16,128,128) NCHW"""
    h_p = LATENT_H // PATCH_SIZE
    w_p = LATENT_W // PATCH_SIZE
    x = noise_pred_flat.reshape(1, h_p, w_p, PATCH_SIZE, PATCH_SIZE, LATENT_C)
    x = x.transpose(0, 5, 1, 3, 2, 4)
    return x.reshape(1, LATENT_C, LATENT_H, LATENT_W).astype(np.float32)


def preprocess_latent_for_vae(latent_nchw):
    scaled = (latent_nchw / VAE_SCALE + VAE_SHIFT).astype(np.float32)
    return scaled.transpose(0, 2, 3, 1)


def postprocess_vae_output(output_flat):
    img = output_flat.reshape(1, IMG_H, IMG_W, 3)
    img = img / 2.0 + 0.5
    img = np.clip(img * 255.0, 0.0, 255.0).astype(np.uint8)
    return Image.fromarray(img[0], mode="RGB")


def scheduler_step(noise_pred_nchw, timestep, latent_nchw):
    if not TORCH_AVAILABLE:
        raise RuntimeError("torch is required for scheduler_step")
    out = scheduler.step(
        torch.from_numpy(noise_pred_nchw), timestep, torch.from_numpy(latent_nchw)
    )
    return out.prev_sample.numpy().astype(np.float32)


####################################################################
# Model download / extraction
####################################################################

def model_exist():
    return (os.path.exists(TEXT_ENCODER_BIN) and
            os.path.exists(TEXT_ENCODER_2_BIN) and
            os.path.exists(TRANSFORMER_BIN) and
            os.path.exists(VAE_DECODER_BIN))


# Number of download threads / chunk size
DOWNLOAD_THREADS    = 8
DOWNLOAD_CHUNK_SIZE = 16 * 1024 * 1024  # 16 MB per chunk


def _get_remote_filesize(url):
    """Get the remote file size and whether Range (resume) is supported. Returns (total_size, accept_ranges)."""
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            accept = resp.headers.get("Accept-Ranges", "").lower() == "bytes"
            return total, accept
    except Exception:
        return 0, False


def _download_range(url, start, end, out_path, progress, max_retries=5):
    """Download byte range [start, end] to the target file at the given offset, with retry support for integrity."""
    expected = end - start + 1
    cur = start  # current absolute offset of successfully written data

    for attempt in range(max_retries):
        if cur > end:
            return  # already done

        headers = {"Range": f"bytes={cur}-{end}"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                with open(out_path, "r+b") as f:
                    f.seek(cur)
                    while True:
                        buf = resp.read(1024 * 256)
                        if not buf:
                            break
                        f.write(buf)
                        cur += len(buf)
                        progress(len(buf))
        except Exception:
            if attempt == max_retries - 1:
                raise
            time.sleep(1.0 * (attempt + 1))
            continue

        if cur > end:
            return  # download complete
        # Not fully downloaded, retry from breakpoint
        time.sleep(0.5 * (attempt + 1))

    raise RuntimeError(
        f"Chunk [{start}-{end}] download incomplete: expected {expected} bytes, got {cur - start} bytes")


def _multithread_download(url, dest, total_size, num_threads):
    """Multi-threaded chunked download."""
    # Pre-allocate file
    with open(dest, "wb") as f:
        f.truncate(total_size)

    # Build chunk tasks
    tasks = []
    pos = 0
    while pos < total_size:
        end = min(pos + DOWNLOAD_CHUNK_SIZE - 1, total_size - 1)
        tasks.append((pos, end))
        pos = end + 1

    downloaded = {"n": 0}
    lock = threading.Lock()
    t_start = time.time()

    def progress(nbytes):
        with lock:
            downloaded["n"] += nbytes
            done = downloaded["n"]
            percent = min(100, done * 100 // total_size)
            elapsed = max(time.time() - t_start, 1e-6)
            speed = done / elapsed / (1024 * 1024)
            sys.stdout.write(
                f"\r  Download progress: {percent}% "
                f"({done // (1024*1024)}/{total_size // (1024*1024)} MB) "
                f"{speed:.1f} MB/s")
            sys.stdout.flush()

    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [
            executor.submit(_download_range, url, s, e, dest, progress)
            for (s, e) in tasks
        ]
        for fut in as_completed(futures):
            fut.result()  # raise exception if any

    sys.stdout.write("\n")


def _singlethread_download(url, dest):
    """Single-threaded fallback download."""
    def _hook(count, block_size, total_size):
        if total_size > 0:
            done = count * block_size
            percent = min(100, done * 100 // total_size)
            sys.stdout.write(
                f"\r  Download progress: {percent}% ({done // (1024*1024)}/{total_size // (1024*1024)} MB)")
            sys.stdout.flush()

    urllib.request.urlretrieve(url, dest, _hook)
    sys.stdout.write("\n")


def cleanup_redundant_model_files():
    """Remove redundant files under the models directory that are not needed at runtime:
    - qai_appbuilder-*.whl installer packages
    - libs subdirectory (QNN libraries are bundled with qai_appbuilder, this directory is not needed)
        """
    # Remove qai_appbuilder whl installer packages
    if os.path.isdir(MODEL_ROOT):
        for name in os.listdir(MODEL_ROOT):
            if name.startswith("qai_appbuilder-") and name.endswith(".whl"):
                whl_path = os.path.join(MODEL_ROOT, name)
                try:
                    os.remove(whl_path)
                    print(f"Removed redundant file: {whl_path}")
                except OSError as e:
                    print(f"Failed to remove {whl_path}: {e}")

    # Remove libs subdirectory
    libs_dir = os.path.join(MODEL_ROOT, "libs")
    if os.path.isdir(libs_dir):
        shutil.rmtree(libs_dir, ignore_errors=True)
        print(f"Removed redundant subdirectory: {libs_dir}")


def model_download():
    # If the models directory and all model files already exist, skip download
    if model_exist():
        print(f"Model already exists: {MODEL_ROOT}")
        cleanup_redundant_model_files()
        return True

    zip_path = os.path.join(_SCRIPT_DIR, MODEL_ZIP_NAME)

    if not os.path.exists(zip_path):
        print(f"Downloading model: {MODEL_DOWNLOAD_URL}")
        total_size, accept_ranges = _get_remote_filesize(MODEL_DOWNLOAD_URL)
        try:
            if accept_ranges and total_size > DOWNLOAD_CHUNK_SIZE:
                print(f"  Multi-threaded download (threads={DOWNLOAD_THREADS}, total size={total_size // (1024*1024)} MB)")
                _multithread_download(MODEL_DOWNLOAD_URL, zip_path, total_size, DOWNLOAD_THREADS)
            else:
                print("  Server does not support chunked download, using single-threaded download")
                _singlethread_download(MODEL_DOWNLOAD_URL, zip_path)
        except Exception as e:
            print(f"\nMulti-threaded download failed ({e}), falling back to single-threaded download...")
            if os.path.exists(zip_path):
                os.remove(zip_path)
            try:
                _singlethread_download(MODEL_DOWNLOAD_URL, zip_path)
            except Exception as e2:
                print(f"\nModel download failed: {e2}")
                if os.path.exists(zip_path):
                    os.remove(zip_path)
                sys.exit(1)

        # Verify downloaded file size integrity
        if total_size > 0:
            actual = os.path.getsize(zip_path)
            if actual != total_size:
                print(f"\nDownloaded file size mismatch (expected {total_size}, got {actual}), deleting and exiting. Please re-run.")
                os.remove(zip_path)
                sys.exit(1)

    print(f"Extracting to: {_SCRIPT_DIR}")
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            bad = zf.testzip()
            if bad is not None:
                raise zipfile.BadZipFile(f"Archive integrity check failed: {bad}")
            zf.extractall(_SCRIPT_DIR)
    except Exception as e:
        print(f"Extraction failed: {e}")
        # Remove corrupted zip so it can be re-downloaded next time
        if os.path.exists(zip_path):
            os.remove(zip_path)
            print("Corrupted archive removed. Please re-run to download again.")
        sys.exit(1)

    # Copy extracted contents into the 'models' subdirectory, then remove the extracted dir
    extracted_dir = os.path.join(_SCRIPT_DIR, MODEL_ROOT_NAME)
    if os.path.exists(extracted_dir) and extracted_dir != MODEL_ROOT:
        print(f"Copying '{MODEL_ROOT_NAME}' to 'models'...")
        os.makedirs(MODEL_ROOT, exist_ok=True)
        for item in os.listdir(extracted_dir):
            src = os.path.join(extracted_dir, item)
            dst = os.path.join(MODEL_ROOT, item)
            if os.path.isdir(src):
                if os.path.exists(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        shutil.rmtree(extracted_dir)
        print(f"Copied model files to: {MODEL_ROOT}")
    elif extracted_dir == MODEL_ROOT:
        print(f"Extracted directly to: {MODEL_ROOT}")

    # Remove zip archive
    if os.path.exists(zip_path):
        os.remove(zip_path)
        print(f"Removed archive: {MODEL_ZIP_NAME}")

    if not model_exist():
        print(f"Model files not found after extraction, please check directory: {MODEL_ROOT}")
        sys.exit(1)

    # Clean up redundant files not needed at runtime (whl packages, libs directory)
    cleanup_redundant_model_files()

    print("Model is ready.")
    return True


####################################################################
# Model initialization
####################################################################

def SetQNNConfig():
    QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC, "")


def model_initialize():
    global model_inited
    global tokenizer, tokenizer_2, time_text_embed, scheduler
    global text_encoder, text_encoder_2, vae_decoder, transformer

    if model_inited:
        return True

    # Download and extract model
    model_download()

    # QNN runtime configuration
    SetQNNConfig()

    # ------ Load CPU auxiliary components ------
    print("Loading auxiliary components (tokenizer, time_text_embed, scheduler)...")
    if not TORCH_AVAILABLE:
        raise RuntimeError("torch, transformers, and diffusers are required. Please install them with: pip install torch transformers diffusers")

    os.environ["TOKENIZERS_PARALLELISM"] = "0"
    tokenizer   = CLIPTokenizer.from_pretrained(TOKENIZER_DIR)
    tokenizer_2 = CLIPTokenizer.from_pretrained(TOKENIZER_2_DIR)

    ckpt = torch.load(TIME_TEXT_EMBED, map_location="cpu", weights_only=True)
    sd = ckpt["state_dict"]
    embedding_dim = sd["timestep_embedder.linear_1.weight"].shape[0]
    pooled_projection_dim = sd["text_embedder.linear_1.weight"].shape[1]
    time_text_embed = CombinedTimestepTextProjEmbeddings(
        embedding_dim=embedding_dim,
        pooled_projection_dim=pooled_projection_dim,
    )
    time_text_embed.load_state_dict(sd)
    time_text_embed.eval()

    scheduler = FlowMatchEulerDiscreteScheduler(num_train_timesteps=1000, shift=3.0)

    # ------ Load QNN models ------
    print("Loading CLIP-L / CLIP-G text encoders...")
    text_encoder   = TextEncoderCLIPL("text_encoder", TEXT_ENCODER_BIN)
    text_encoder_2 = TextEncoderCLIPG("text_encoder_2", TEXT_ENCODER_2_BIN)

    print("Loading VAE decoder...")
    vae_decoder = VaeDecoder("vae_decoder", VAE_DECODER_BIN)

    print("Loading transformer...")
    transformer = TransformerQNN("transformer", TRANSFORMER_BIN)

    model_inited = True
    return True


####################################################################
# Parameter setup
####################################################################

def setup_parameters(prompt, negative_prompt, seed, step, cfg):
    global user_prompt, user_negative_prompt, user_seed, user_step, user_cfg

    user_prompt          = prompt
    user_negative_prompt = negative_prompt
    user_seed            = int(seed)
    user_step            = int(step)
    user_cfg             = float(cfg)

    assert isinstance(user_step, int), "user_step should be of type int"
    assert isinstance(user_cfg, float), "user_cfg should be of type float"


####################################################################
# Inference execution
####################################################################

def model_execute(callback, image_path, show_image=True, save_image=True):
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Set timesteps
    scheduler.set_timesteps(user_step)
    timesteps = scheduler.timesteps

    # ------ Text encoding ------
    print("Text encoding (CLIP-L + CLIP-G)...")
    # Positive prompt
    t1c = tokenize(user_prompt, tokenizer)
    t2c = tokenize(user_prompt, tokenizer_2)
    h1c, p1c = text_encoder.Inference(t1c)
    h2c, p2c = text_encoder_2.Inference(t2c)
    cond_hs = build_encoder_hidden_states(h1c, h2c)
    cond_pooled = build_pooled_projections(p1c, p2c)

    # Negative prompt
    neg = user_negative_prompt or ""
    t1u = tokenize(neg, tokenizer)
    t2u = tokenize(neg, tokenizer_2)
    h1u, p1u = text_encoder.Inference(t1u)
    h2u, p2u = text_encoder_2.Inference(t2u)
    uncond_hs = build_encoder_hidden_states(h1u, h2u)
    uncond_pooled = build_pooled_projections(p1u, p2u)

    # ------ Initialize latent ------
    np.random.seed(user_seed)
    latent = np.random.randn(1, LATENT_C, LATENT_H, LATENT_W).astype(np.float32)

    # ------ Denoising loop ------
    print(f"Denoising loop ({user_step} steps, CFG={user_cfg})...")
    for i, t in enumerate(timesteps):
        t_val = t.item()
        print(f"Step {i + 1}/{user_step} timestep={t_val:.2f} Running...")
        step_t0 = time.time()

        cond_temb = get_temb(t_val, cond_pooled)
        uncond_temb = get_temb(t_val, uncond_pooled)

        latent_nhwc = latent.transpose(0, 2, 3, 1).astype(np.float32)

        print(f"[DEBUG] Before transformer inference:")
        print(f"  cond_temb shape: {cond_temb.shape}")
        print(f"  latent_nhwc shape: {latent_nhwc.shape}")
        print(f"  cond_hs shape: {cond_hs.shape}")

        noise_cond_flat = transformer.Inference(cond_temb, latent_nhwc, cond_hs)
        noise_uncond_flat = transformer.Inference(uncond_temb, latent_nhwc, uncond_hs)

        noise_cond = unpatchify_noise_pred(noise_cond_flat)
        noise_uncond = unpatchify_noise_pred(noise_uncond_flat)
        noise_pred = noise_uncond + user_cfg * (noise_cond - noise_uncond)

        latent = scheduler_step(noise_pred, t, latent)
        print(f"  ({time.time() - step_t0:.1f}s)")

        callback(i)

    # ------ VAE decoding ------
    print("VAE decoding...")
    sample_nhwc = preprocess_latent_for_vae(latent)
    output_flat = vae_decoder.Inference(sample_nhwc)

    if len(output_flat) == 0:
        callback(None)
        PerfProfile.RelPerfProfileGlobal()
        return None

    output_image = postprocess_vae_output(output_flat)

    PerfProfile.RelPerfProfileGlobal()

    out_path = image_path
    if save_image:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
        output_image.save(out_path)
        print(f"\nDone! Image saved to: {out_path}")
        callback(str(out_path))

    if show_image:
        output_image.show()

    if save_image:
        return out_path
    else:
        return output_image


####################################################################
# Release model
####################################################################

def model_destroy():
    global text_encoder, text_encoder_2, vae_decoder, transformer

    del text_encoder
    del text_encoder_2
    del vae_decoder
    del transformer


####################################################################
# Callback
####################################################################

def modelExecuteCallback(result):
    if (result is None) or isinstance(result, str):
        if result is None:
            print("Image generates failed.")
        else:
            print("Image saved to '" + result + "'")
    else:
        result = (result + 1) * 100
        result = int(result / user_step)
        # Progress percentage, can be used in GUI


####################################################################
# add_argument
####################################################################

def add_argument(parser):
    parser.add_argument("--prompt",          default="A cat holding a sign that says hello world", type=str)
    parser.add_argument("--negative_prompt", default="", type=str)
    parser.add_argument("--steps",           default=8, type=int)
    parser.add_argument("--cfg",             default=3.5, type=float)
    parser.add_argument("--seed",            default=42, type=int)
    parser.add_argument("--output",          default=os.path.join(_SCRIPT_DIR, "output.png"), type=str)
    return parser


####################################################################

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SD3.5 Medium PC inference (qai_appbuilder)")
    add_argument(parser)
    args = parser.parse_args()

    model_initialize()

    time_start = time.time()

    setup_parameters(args.prompt, args.negative_prompt, args.seed, args.steps, args.cfg)
    model_execute(modelExecuteCallback, args.output, show_image=True, save_image=True)

    time_end = time.time()
    print("time consumes for inference {}(s)".format(str(time_end - time_start)))

    model_destroy()
