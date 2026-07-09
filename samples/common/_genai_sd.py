# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ─────────────────────────────────────────────────────────────────────────────
#
# Shared utilities for Stable Diffusion model inference scripts.
# Provides model download, QNN initialization, tokenization, scheduling,
# and inference helpers for text-to-image generation.
#

import os
import sys
import platform
import numpy as np
import torch
from pathlib import Path
from typing import Tuple

from transformers import CLIPTokenizer
from diffusers import DPMSolverMultistepScheduler
from diffusers.models.embeddings import get_timestep_embedding, TimestepEmbedding

from qai_appbuilder import (
    QNNContext,
    Runtime,
    LogLevel,
    ProfilingLevel,
    PerfProfile,
    QNNConfig,
)
import install


# ─────────────────────────────────────────────────────────────────────────────
# Platform Detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_platform():
    """Return one of: 'wos', 'x86_win', 'arm64_linux', 'x86_linux', 'unknown'."""
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "windows":
        if machine in ("aarch64", "arm64"):
            return "wos"
        else:
            return "x86_win"
    if system == "linux":
        if machine in ("aarch64", "arm64"):
            return "arm64_linux"
        if machine in ("x86_64", "amd64"):
            return "x86_linux"
    return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# QNN Context Classes
# ─────────────────────────────────────────────────────────────────────────────

class TextEncoderQNNContext(QNNContext):
    """QNN context for Stable Diffusion text encoder."""

    def Inference(self, input_data):
        input_datas = [input_data]
        output_data = super().Inference(input_datas)[0]
        # Output of Text encoder should be of shape (1, 77, 768)
        output_data = output_data.reshape((1, 77, 768))
        return output_data


class UnetQNNContext(QNNContext):
    """QNN context for Stable Diffusion UNet."""

    def Inference(self, input_data_1, input_data_2, input_data_3):
        # Reshape arrays to 1 dimensionality before sending to network
        input_data_1 = input_data_1.reshape(input_data_1.size)
        input_data_3 = input_data_3.reshape(input_data_3.size)

        input_datas = [input_data_1, input_data_2, input_data_3]
        output_data = super().Inference(input_datas)[0]

        output_data = output_data.reshape(1, 64, 64, 4)
        return output_data


class VaeDecoderQNNContext(QNNContext):
    """QNN context for Stable Diffusion VAE decoder."""

    def Inference(self, input_data):
        input_data = input_data.reshape(input_data.size)
        input_datas = [input_data]
        output_data = super().Inference(input_datas)[0]
        return output_data


# ─────────────────────────────────────────────────────────────────────────────
# Model Download Helpers
# ─────────────────────────────────────────────────────────────────────────────

def download_model(soc_id: str, model_name: str, model_path, help_url: str, hub_id=None) -> bool:
    """Download a QAI hub model .bin; returns True if successful."""
    model_path = Path(model_path)
    if model_path.is_file():
        return True
    model_path.parent.mkdir(parents=True, exist_ok=True)
    desc = f"Downloading {model_name} model... "
    fail = f"\nFailed to download {model_name} model. Please prepare the model according to the steps in below link:\n{help_url}"
    kwargs = {"desc": desc, "fail": fail}
    if hub_id:
        kwargs["hub_id"] = hub_id
    return install.download_qai_hubmodel(soc_id, model_name, str(model_path), **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# QNN Configuration
# ─────────────────────────────────────────────────────────────────────────────

def init_htp_model(model_path, model_class, instance_name):
    """Configure HTP runtime and instantiate model."""
    QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)
    return model_class(instance_name, str(model_path))


# ─────────────────────────────────────────────────────────────────────────────
# Tokenization Helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_tokenizer(tokenizer_name: str = "openai/clip-vit-large-patch14"):
    """Load CLIP tokenizer for Stable Diffusion."""
    return CLIPTokenizer.from_pretrained(tokenizer_name)


def tokenize_prompt(tokenizer, prompt: str, max_length: int = 77) -> np.ndarray:
    """Tokenize prompt text and return token IDs as int32 numpy array."""
    tokens = tokenizer(
        prompt,
        padding="max_length",
        max_length=max_length,
        truncation=True,
        return_tensors="pt",
    )
    return tokens.input_ids.to(torch.int32).numpy()


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler Helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_scheduler(num_inference_steps: int = 20):
    """Create DPMSolver scheduler for Stable Diffusion."""
    return DPMSolverMultistepScheduler.from_pretrained(
        "stabilityai/stable-diffusion-1-5",
        subfolder="scheduler",
        num_inference_steps=num_inference_steps,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Inference Helpers
# ─────────────────────────────────────────────────────────────────────────────

def run_inference_with_perf_profile(model_instance, *input_data):
    """Run inference wrapped in HTP BURST perf profile."""
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
    output = model_instance.Inference(*input_data)
    PerfProfile.RelPerfProfileGlobal()
    return output


def get_timestep_embedding_input(timestep: int, embedding_dim: int = 320) -> np.ndarray:
    """Generate timestep embedding for UNet input."""
    embedding = get_timestep_embedding(torch.tensor([timestep]), embedding_dim)
    return embedding.numpy().astype(np.float32)


# ─────────────────────────────────────────────────────────────────────────────
# Image Processing Helpers
# ─────────────────────────────────────────────────────────────────────────────

def latent_to_image(vae_output: np.ndarray, scale_factor: float = 0.18215) -> np.ndarray:
    """Convert VAE latent output to image array."""
    # VAE output is typically (1, 3, 512, 512) in NCHW format
    # Scale and clip to [0, 1]
    image = vae_output / scale_factor
    image = np.clip(image, -1, 1)
    image = (image + 1) / 2  # Convert from [-1, 1] to [0, 1]
    image = np.transpose(image, (0, 2, 3, 1))  # NCHW to NHWC
    return (image * 255).astype(np.uint8)
