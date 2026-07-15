# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ─────────────────────────────────────────────────────────────────────────────
#
# Nomic Embed Text Inference Script
# Text embedding model on Snapdragon X Elite / X2 Elite NPU
#

import os
import sys
sys.path.append(".")
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "common"))

import install
import argparse
from typing import cast
import torch
import numpy as np
from pathlib import Path

from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)
from _text_generation import (
    TextGenerationQNNContext,
    download_model,
    init_htp_model,
    get_tokenizer,
    tokenize_text,
    run_inference_with_perf_profile,
)

# ─────────────────────────────────────────────────────────────────────────────
# Model Configuration
# ─────────────────────────────────────────────────────────────────────────────

MODEL_ID = "mn03prw8n"
MODEL_NAME = "nomic_embed_text"
MODEL_HELP_URL = "https://github.com/qualcomm/qai-appbuilder/blob/main/samples/multimodal/Text_Generation/nomic_embed_text/README.md"
SEQ_LEN = 128

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

execution_ws = Path(os.path.dirname(os.path.abspath(__file__)))
model_dir = execution_ws / "models"
model_path = model_dir / f"{MODEL_NAME}.bin"
output_dir = execution_ws

# ─────────────────────────────────────────────────────────────────────────────
# SOC ID Parsing
# ─────────────────────────────────────────────────────────────────────────────

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

# ─────────────────────────────────────────────────────────────────────────────
# Global State
# ─────────────────────────────────────────────────────────────────────────────

nomic_embed_text = None
tokenizer = None


# ─────────────────────────────────────────────────────────────────────────────
# QNN Model Class
# ─────────────────────────────────────────────────────────────────────────────

class NomicEmbedText(TextGenerationQNNContext):
    """Nomic Embed Text QNN model wrapper."""

    def Inference(self, input_ids, attention_mask):
        if isinstance(input_ids, torch.Tensor):
            input_ids = input_ids.detach().cpu().numpy()
        if isinstance(attention_mask, torch.Tensor):
            attention_mask = attention_mask.detach().cpu().numpy()
        return super().Inference(input_ids, attention_mask)


# ─────────────────────────────────────────────────────────────────────────────
# Initialization
# ─────────────────────────────────────────────────────────────────────────────

def Init():
    """Initialize model and tokenizer."""
    global nomic_embed_text, tokenizer

    # Download model
    if not download_model(SOC_ID, MODEL_NAME, model_path, MODEL_HELP_URL):
        sys.exit(1)

    # Load tokenizer
    tokenizer = get_tokenizer("bert-base-uncased", seq_len=SEQ_LEN)

    # Configure QNN
    QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instantiate model
    nomic_embed_text = init_htp_model(model_path, NomicEmbedText, "nomic_embed_text")


# ─────────────────────────────────────────────────────────────────────────────
# Inference
# ─────────────────────────────────────────────────────────────────────────────

def Inference(input_text):
    """Run inference on input text and return embeddings."""
    # Tokenize
    input_ids, attention_mask = tokenize_text(tokenizer, input_text, SEQ_LEN)

    # Run inference with performance profile
    output_embeddings = run_inference_with_perf_profile(
        nomic_embed_text, input_ids, attention_mask
    )

    # Save output
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / "embeddings.npy"
    np.save(output_file, output_embeddings)
    print(f"Embeddings saved to: {output_file}")
    print(f"Embeddings shape: {output_embeddings.shape}")
    print(f"Embeddings:\n{output_embeddings}")

    return output_embeddings


# ─────────────────────────────────────────────────────────────────────────────
# Cleanup
# ─────────────────────────────────────────────────────────────────────────────

def Release():
    """Release model resources."""
    global nomic_embed_text
    if nomic_embed_text is not None:
        del nomic_embed_text


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main(input_text=None):
    """Main entry point."""
    if input_text is None:
        input_text = "hello!"

    Init()
    Inference(input_text)
    Release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Encode text to embeddings.")
    parser.add_argument('--text', help='Text to encode', default=None)
    args = parser.parse_args()

    main(args.text)



