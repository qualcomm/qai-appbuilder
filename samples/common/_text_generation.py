# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ─────────────────────────────────────────────────────────────────────────────
#
# Shared utilities for Text Generation model inference scripts.
# Provides model download, QNN initialization, tokenization, and inference helpers
# for text embedding and translation models.
#

import os
import sys
import torch
import numpy as np
from pathlib import Path
from typing import cast, Tuple
from transformers import AutoTokenizer

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
# QNN Context Classes
# ─────────────────────────────────────────────────────────────────────────────

class TextGenerationQNNContext(QNNContext):
    """Base QNN context for text generation models (embeddings, translation, etc.)."""

    def Inference(self, *input_data):
        """Run inference with variable number of inputs."""
        input_datas = list(input_data)
        output_data = super().Inference(input_datas)
        return output_data[0] if len(output_data) == 1 else output_data


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

def get_tokenizer(tokenizer_name: str, seq_len: int = 128):
    """Load a tokenizer from HuggingFace."""
    return AutoTokenizer.from_pretrained(tokenizer_name, model_max_length=seq_len)


def tokenize_text(tokenizer, text: str, seq_len: int = 128) -> Tuple[torch.Tensor, torch.Tensor]:
    """Tokenize text and return input_ids and attention_mask as int32 tensors."""
    inputs = tokenizer(text, padding="max_length", return_tensors="pt")
    input_ids = cast(torch.Tensor, inputs["input_ids"].to(torch.int32))
    attention_mask = cast(torch.Tensor, inputs["attention_mask"].to(torch.int32))
    return input_ids, attention_mask


# ─────────────────────────────────────────────────────────────────────────────
# Inference Helpers
# ─────────────────────────────────────────────────────────────────────────────

def run_inference_with_perf_profile(model_instance, *input_data):
    """Run inference wrapped in HTP BURST perf profile."""
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
    output = model_instance.Inference(*input_data)
    PerfProfile.RelPerfProfileGlobal()
    return output


def convert_torch_to_numpy(tensor):
    """Convert torch tensor to numpy array if needed."""
    if isinstance(tensor, torch.Tensor):
        return tensor.detach().cpu().numpy()
    return tensor
