# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ─────────────────────────────────────────────────────────────────────────────
#
# Shared utilities for Stable Diffusion model inference scripts (v1.5, v2.1).
# Provides model download, QNN initialization, and output processing.
#

import os
import numpy as np
import torch
from pathlib import Path
from PIL import Image
from qai_appbuilder import QNNContext, Runtime, LogLevel, ProfilingLevel, QNNConfig
import install


def set_qnn_config():
    """Configure QNN runtime for Stable Diffusion."""
    QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)


def download_sd_component(soc_id, model_name, model_path, desc, fail, hub_id=None) -> bool:
    """Download a Stable Diffusion component (text encoder, UNet, VAE) via QAI Hub."""
    model_path = Path(model_path)
    if model_path.is_file():
        return True
    model_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {"desc": desc, "fail": fail}
    if hub_id:
        kwargs["hub_id"] = hub_id
    return install.download_qai_hubmodel(soc_id, model_name, str(model_path), **kwargs)


def generate_initial_latent(seed=42, channels=4, height=64, width=64):
    """Generate random noise latent as NHWC numpy array."""
    latent = torch.randn((1, channels, height, width), generator=torch.manual_seed(seed)).numpy()
    return latent.transpose(0, 2, 3, 1)


def decode_vae_output(output_data, image_size=512):
    """Convert VAE decoder output to PIL Image."""
    output = np.clip(output_data * 255.0, 0.0, 255.0).astype(np.uint8)
    return Image.fromarray(output.reshape(image_size, image_size, -1), mode="RGB")
