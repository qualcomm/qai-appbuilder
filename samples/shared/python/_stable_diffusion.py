# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ─────────────────────────────────────────────────────────────────────────────
#
# Shared utilities for Stable Diffusion model inference scripts (v1.5, v2.1).
# Provides platform detection, model download, QNN initialization, tokenizer
# loading, scheduler stepping, and output processing helpers.
#

import os
import sys
import platform
import datetime
import numpy as np
import torch
from pathlib import Path
from PIL import Image
from transformers import CLIPTokenizer
from diffusers import DPMSolverMultistepScheduler
from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel,
                             PerfProfile, QNNConfig)
import install


# ─── Platform detection ───────────────────────────────────────────────────────

def detect_platform():
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


# ─── argv --chipset parsing ───────────────────────────────────────────────────

def parse_chipset_arg():
    """
    Extract --chipset <value> from sys.argv, remove it in-place, and return
    the SOC_ID string (or None if not provided).
    """
    soc_id = None
    cleaned = []
    i = 0
    while i < len(sys.argv):
        if sys.argv[i] == '--chipset' and i + 1 < len(sys.argv):
            soc_id = sys.argv[i + 1]
            i += 2
        else:
            cleaned.append(sys.argv[i])
            i += 1
    sys.argv = cleaned
    return soc_id


# ─── QNN configuration ────────────────────────────────────────────────────────

def set_qnn_config():
    """Configure QNN runtime for Stable Diffusion (HTP, ERROR log, BASIC profiling)."""
    QNNConfig.Config(Runtime.HTP, LogLevel.ERROR, ProfilingLevel.BASIC)


# ─── Model download ───────────────────────────────────────────────────────────

def download_sd_models(platform, soc_id, hub_id,
                       model_name, model_help_url,
                       model_name_vae, vae_path,
                       model_name_unet, unet_path,
                       model_name_text, text_path):
    """
    Download all three SD components (VAE, UNet, TextEncoder) via QAI Hub.
    Exits the process if any model file is missing after download.
    """
    desc = f"Downloading {model_name} model... "
    fail = (f"\nFailed to download {model_name} model. "
            f"Please prepare the models according to the steps in below link:\n{model_help_url}")

    kwargs = {"desc": desc, "fail": fail}
    if platform in ("wos", "x86_win"):
        kwargs["hub_id"] = hub_id

    install.download_qai_hubmodel(soc_id, model_name_vae,  str(vae_path),  **kwargs)
    install.download_qai_hubmodel(soc_id, model_name_unet, str(unet_path), **kwargs)
    install.download_qai_hubmodel(soc_id, model_name_text, str(text_path), **kwargs)

    if not (Path(text_path).exists() and Path(unet_path).exists() and Path(vae_path).exists()):
        print(f"\nPlease download {model_name} model from {model_help_url} "
              f"and save them to the models directory.\n")
        sys.exit(1)


def download_sd_component(soc_id, model_name, model_path, desc, fail, hub_id=None) -> bool:
    """Download a single Stable Diffusion component (text encoder, UNet, VAE) via QAI Hub."""
    model_path = Path(model_path)
    if model_path.is_file():
        return True
    model_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {"desc": desc, "fail": fail}
    if hub_id:
        kwargs["hub_id"] = hub_id
    return install.download_qai_hubmodel(soc_id, model_name, str(model_path), **kwargs)


# ─── Tokenizer loading ────────────────────────────────────────────────────────

def load_tokenizer(tokenizer_model_name, tokenizer_dir, tokenizer_help_url,
                   use_subfolder=False):
    """
    Load a CLIPTokenizer from cache or HuggingFace Hub.

    Args:
        tokenizer_model_name: HuggingFace model ID (e.g. "openai/clip-vit-large-patch14").
        tokenizer_dir: Path to local cache directory.
        tokenizer_help_url: URL shown in error message if download fails.
        use_subfolder: If True, load with subfolder="tokenizer" and revision="main"
                       (needed for SD v2.1 which stores tokenizer in a subfolder).
    Returns:
        CLIPTokenizer instance.
    """
    tokenizer_dir = Path(tokenizer_dir)

    def _is_hub_cache(d):
        """Return True if d looks like a HuggingFace Hub cache directory."""
        return (d / "CACHEDIR.TAG").exists() or any(d.glob("models--*"))

    def _is_direct_tokenizer(d):
        """Return True if d is a direct tokenizer directory (contains tokenizer_config.json)."""
        return (d / "tokenizer_config.json").exists()

    try:
        if tokenizer_dir.exists() and _is_direct_tokenizer(tokenizer_dir):
            # Directory is a direct tokenizer snapshot — load it directly.
            return CLIPTokenizer.from_pretrained(str(tokenizer_dir), local_files_only=True)
        elif tokenizer_dir.exists() and _is_hub_cache(tokenizer_dir):
            # Directory is a HuggingFace Hub cache — use cache_dir + local_files_only.
            if use_subfolder:
                return CLIPTokenizer.from_pretrained(
                    tokenizer_model_name, subfolder="tokenizer", revision="main",
                    cache_dir=str(tokenizer_dir), local_files_only=True)
            else:
                return CLIPTokenizer.from_pretrained(
                    tokenizer_model_name, cache_dir=str(tokenizer_dir), local_files_only=True)
        elif tokenizer_dir.exists() and not (tokenizer_dir / ".locks").exists():
            # Legacy: directory exists but no .locks — try local first, fall back to download.
            try:
                if use_subfolder:
                    return CLIPTokenizer.from_pretrained(
                        tokenizer_model_name, subfolder="tokenizer", revision="main",
                        cache_dir=str(tokenizer_dir), local_files_only=True)
                else:
                    return CLIPTokenizer.from_pretrained(
                        tokenizer_model_name, cache_dir=str(tokenizer_dir), local_files_only=True)
            except Exception:
                pass  # fall through to download
            if use_subfolder:
                return CLIPTokenizer.from_pretrained(
                    tokenizer_model_name, subfolder="tokenizer", revision="main",
                    cache_dir=str(tokenizer_dir))
            else:
                return CLIPTokenizer.from_pretrained(
                    tokenizer_model_name, cache_dir=str(tokenizer_dir))
        else:
            # No local cache — download from HuggingFace Hub.
            if use_subfolder:
                return CLIPTokenizer.from_pretrained(
                    tokenizer_model_name, subfolder="tokenizer", revision="main",
                    cache_dir=str(tokenizer_dir))
            else:
                return CLIPTokenizer.from_pretrained(
                    tokenizer_model_name, cache_dir=str(tokenizer_dir))
    except Exception as e:
        print(f"\n[ERROR] Tokenizer load failed: {e}")
        fail = ("\nFailed to download tokenizer model. "
                "Please prepare the tokenizer data according to the guide below:\n"
                + tokenizer_help_url + "\n")
        print(fail)
        sys.exit(1)


# ─── Tokenizer run ────────────────────────────────────────────────────────────

def run_tokenizer(tokenizer, prompt, max_length=77):
    """Tokenize a prompt and return a float32 numpy array of token IDs."""
    text_input = tokenizer(prompt, padding="max_length",
                           max_length=max_length, truncation=True)
    return np.array(text_input.input_ids, dtype=np.float32)


# ─── Scheduler ────────────────────────────────────────────────────────────────

def make_scheduler():
    """Create and return a DPMSolverMultistepScheduler with SD default settings."""
    return DPMSolverMultistepScheduler(
        num_train_timesteps=1000,
        beta_start=0.00085,
        beta_end=0.012,
        beta_schedule="scaled_linear"
    )


def get_timestep(scheduler, step):
    """Return the integer timestep value for the given denoising step index."""
    return np.int32(scheduler.timesteps.numpy()[step])


def run_scheduler(scheduler, noise_pred_uncond, noise_pred_text,
                  latent_in, timestep, text_guidance):
    """
    Apply classifier-free guidance and run one scheduler step.

    Args:
        scheduler: DPMSolverMultistepScheduler instance.
        noise_pred_uncond: Unconditional UNet output, NHWC float32 numpy array.
        noise_pred_text: Conditional UNet output, NHWC float32 numpy array.
        latent_in: Current latent, NHWC float32 numpy array.
        timestep: Current timestep integer.
        text_guidance: CFG scale (float).
    Returns:
        Updated latent as NHWC float32 numpy array.
    """
    # Convert all inputs from NHWC to NCHW
    noise_pred_uncond = np.transpose(noise_pred_uncond, (0, 3, 1, 2)).copy()
    noise_pred_text   = np.transpose(noise_pred_text,   (0, 3, 1, 2)).copy()
    latent_in         = np.transpose(latent_in,         (0, 3, 1, 2)).copy()

    # Convert to torch tensors
    noise_pred_uncond = torch.from_numpy(noise_pred_uncond)
    noise_pred_text   = torch.from_numpy(noise_pred_text)
    latent_in         = torch.from_numpy(latent_in)

    # Classifier-free guidance
    noise_pred = noise_pred_uncond + text_guidance * (noise_pred_text - noise_pred_uncond)

    # Scheduler step
    latent_out = scheduler.step(noise_pred, timestep, latent_in).prev_sample.numpy()

    # Convert latent_out from NCHW back to NHWC
    return np.transpose(latent_out, (0, 2, 3, 1)).copy()


# ─── Parameter validation ─────────────────────────────────────────────────────

def validate_and_prepare_parameters(prompt, un_prompt, seed, step, text_guidance):
    """
    Validate generation parameters and resolve seed=-1 to a random value.

    Returns:
        (prompt, un_prompt, seed_int64, step, text_guidance)
    """
    seed = np.int64(seed)
    if seed == -1:
        seed = np.random.randint(low=0, high=9999999999, size=None, dtype=np.int64)

    assert isinstance(seed, np.int64),  "seed should be of type int64"
    assert isinstance(step, int),       "step should be of type int"
    assert isinstance(text_guidance, float), "text_guidance should be of type float"
    assert 5.0 <= text_guidance <= 15.0, "text_guidance should be a float from [5.0, 15.0]"

    return prompt, un_prompt, seed, step, text_guidance


# ─── Latent initialization ────────────────────────────────────────────────────

def generate_initial_latent(seed=42, channels=4, height=64, width=64):
    """Generate random noise latent as NHWC float32 numpy array."""
    latent = torch.randn((1, channels, height, width),
                         generator=torch.manual_seed(int(seed))).numpy()
    return latent.transpose(0, 2, 3, 1)


# ─── VAE output decoding ──────────────────────────────────────────────────────

def decode_vae_output(output_data, image_size=512):
    """Convert VAE decoder raw output to a PIL Image."""
    output = np.clip(output_data * 255.0, 0.0, 255.0).astype(np.uint8)
    return Image.fromarray(output.reshape(image_size, image_size, -1), mode="RGB")


# ─── Image saving ─────────────────────────────────────────────────────────────

def save_output_image(image, image_dir, seed, image_size=512):
    """
    Save a PIL Image to image_dir with a timestamped filename.

    Returns:
        Path object of the saved file.
    """
    image_dir = Path(image_dir)
    image_dir.mkdir(parents=True, exist_ok=True)
    formatted_time = datetime.datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    image_path = image_dir / "{}_{}_{}.jpg".format(formatted_time, str(seed), str(image_size))
    image.save(image_path)
    return image_path


# ─── QNN model classes ────────────────────────────────────────────────────────

class TextEncoderBase(QNNContext):
    """
    Base QNNContext for the CLIP text encoder.
    Subclasses must set `_embed_dim` (768 for v1.5, 1024 for v2.1).
    """
    _embed_dim = 768  # override in subclass

    def Inference(self, input_data):
        output_data = super().Inference([input_data])[0]
        return output_data.reshape((1, 77, self._embed_dim))


class UnetBase(QNNContext):
    """QNNContext for the UNet denoiser (shared between v1.5 and v2.1)."""

    def Inference(self, input_data_1, input_data_2, input_data_3):
        input_data_1 = input_data_1.reshape(input_data_1.size)
        input_data_3 = input_data_3.reshape(input_data_3.size)
        output_data = super().Inference([input_data_1, input_data_2, input_data_3])[0]
        return output_data.reshape(1, 64, 64, 4)


class VaeDecoderBase(QNNContext):
    """QNNContext for the VAE decoder (shared between v1.5 and v2.1)."""

    def Inference(self, input_data):
        input_data = input_data.reshape(input_data.size)
        return super().Inference([input_data])[0]


# ─── Default callback ─────────────────────────────────────────────────────────

def default_execute_callback(result, user_step):
    """
    Default progress/result callback for model_execute.
    Prints progress percentage or the saved image path.
    """
    if result is None or isinstance(result, str):
        if result is None:
            print("Image generation failed.")
        else:
            print("Image saved to '{}'".format(result))
    else:
        pct = int((result + 1) * 100 / user_step)
        print(f"  Progress: {pct}%")
