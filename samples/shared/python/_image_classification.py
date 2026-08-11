# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ─────────────────────────────────────────────────────────────────────────────
#
# Shared utilities for Image Classification model inference scripts.
# Provides model download, QNN initialization, preprocessing, and postprocessing.
#

import json
import torch
import numpy as np
from pathlib import Path
from PIL import Image
from qai_appbuilder import QNNContext, Runtime, LogLevel, ProfilingLevel, QNNConfig
import install


class ImageClassificationQNNContext(QNNContext):
    """Base class for image classification QNN models."""

    def Inference(self, input_data):
        return super().Inference([input_data])[0]


def init_htp_model(model_path, model_class, instance_name):
    """Configure QNN and instantiate an image classification model."""
    QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)
    return model_class(instance_name, str(model_path))


def download_model(soc_id, model_name, model_path, help_url, hub_id=None) -> bool:
    """Download model via QAI Hub if not already present."""
    model_path = Path(model_path)
    if model_path.is_file():
        return True
    model_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {
        "desc": f"Downloading {model_name}... ",
        "fail": f"\nFailed to download {model_name}.\n{help_url}"
    }
    if hub_id:
        kwargs["hub_id"] = hub_id
    return install.download_qai_hubmodel(soc_id, model_name, str(model_path), **kwargs)


def download_imagenet_labels(url, local_path) -> bool:
    """Download ImageNet labels file if not already present."""
    local_path = Path(local_path)
    if local_path.is_file():
        return True
    local_path.parent.mkdir(parents=True, exist_ok=True)
    return install.download_url(url, str(local_path))


def load_imagenet_labels(path) -> list:
    """Load ImageNet labels from JSON or text file."""
    path = Path(path)
    if path.suffix == ".json":
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    else:
        with open(path, "r", encoding="utf-8") as f:
            return [line.strip() for line in f]


def preprocess_for_classification(image_path, image_size=224):
    """Preprocess image for classification: resize, crop, normalize to NHWC float32."""
    import torchvision.transforms as T
    img = Image.open(str(image_path)).convert("RGB")
    tensor = T.Compose([
        T.Resize(image_size),
        T.CenterCrop(image_size),
        T.PILToTensor()
    ])(img)
    normalized = tensor.float().unsqueeze(0).numpy() / 255.0
    return np.transpose(normalized, (0, 2, 3, 1))


def top_k_classifications(output_data, labels, k=5) -> str:
    """Format top-k classification results as a readable string."""
    probs = torch.softmax(torch.from_numpy(output_data).squeeze(0), dim=0)
    vals, idxs = torch.topk(probs, k)
    lines = []
    for v, i in zip(vals, idxs):
        idx = i.item()
        label = labels[idx] if idx < len(labels) else f"class_{idx}"
        lines.append(f"  {label}: {v.item():.4f}")
    return "\n".join(lines)
