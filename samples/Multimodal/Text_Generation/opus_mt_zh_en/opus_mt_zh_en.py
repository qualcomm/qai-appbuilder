# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
#
# OpusMT-Zh-En Inference Script
# Chinese -> English Neural Machine Translation on Snapdragon X Elite / X2 Elite NPU
#
# Model: Helsinki-NLP/opus-mt-zh-en (MarianMT)
# Runtime: VOICE_AI (encoder.bin + decoder.bin via qai_appbuilder.QNNContext)
# Device: Snapdragon X Elite / X2 Elite (HTP v73/v79)
#

import os
import sys
import time
import zipfile
import argparse
import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.append(".")
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "common"))

import install
from install import detect_device_model
from _text_generation import (
    TextGenerationQNNContext,
    init_htp_model,
    run_inference_with_perf_profile,
)
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Model Configuration
# ─────────────────────────────────────────────────────────────────────────────

MODEL_NAME = "opus_mt_zh_en"
MODEL_HELP_URL = "https://github.com/qualcomm/qai-appbuilder/tree/main/samples/python/" + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"

MODEL_URLS = {
    "snapdragon_x2_elite": "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/opus_mt_zh_en/releases/v0.57.1/opus_mt_zh_en-voice_ai-float-qualcomm_snapdragon_x2_elite.zip",
    "snapdragon_x_elite":  "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/opus_mt_zh_en/releases/v0.57.1/opus_mt_zh_en-voice_ai-float-qualcomm_snapdragon_x_elite.zip",
}

HF_MODEL_ID = "Helsinki-NLP/opus-mt-zh-en"

MAX_SEQ_LEN = 256   # encoder fixed input length
MAX_GEN_LEN = 256   # max decoder steps
NUM_LAYERS  = 6
NUM_HEADS   = 8
HEAD_DIM    = 64

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

execution_ws = Path(os.path.dirname(os.path.abspath(__file__)))
model_dir    = execution_ws / "models"
tokenizer_dir = model_dir / "tokenizer"

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

# ─────────────────────────────────────────────────────────────────────────────
# Global State
# ─────────────────────────────────────────────────────────────────────────────

encoder_model = None
decoder_model = None
tokenizer     = None

# ─────────────────────────────────────────────────────────────────────────────
# QNN Model Classes
# ─────────────────────────────────────────────────────────────────────────────

class OpusMTEncoder(TextGenerationQNNContext):
    """Encoder QNN model wrapper for OpusMT."""

    def Inference(self, input_ids, attention_mask):
        return super().Inference(input_ids, attention_mask)


class OpusMTDecoder(TextGenerationQNNContext):
    """Decoder QNN model wrapper for OpusMT."""

    def Inference(self, *inputs):
        return super().Inference(*inputs)


# ─────────────────────────────────────────────────────────────────────────────
# Model Download Helper
# ─────────────────────────────────────────────────────────────────────────────

def download_opus_model() -> Path:
    """Download and extract the encoder/decoder zip for the detected device.
    Returns the path to the extracted model subdirectory."""
    device_model = detect_device_model()
    print(f"[INFO] Detected device: {device_model}")

    url = MODEL_URLS.get(device_model, MODEL_URLS["snapdragon_x_elite"])
    zip_name      = url.split("/")[-1]
    model_subdir  = zip_name.replace(".zip", "")
    model_subdir_path = model_dir / model_subdir

    encoder_bin = model_subdir_path / "encoder.bin"
    decoder_bin = model_subdir_path / "decoder.bin"

    if encoder_bin.exists() and decoder_bin.exists():
        print(f"[INFO] Model already exists: {model_subdir_path}")
        return model_subdir_path

    model_dir.mkdir(parents=True, exist_ok=True)
    zip_path = model_dir / zip_name

    print(f"[INFO] Downloading model from:\n       {url}")
    ret = install.download_url(url, str(zip_path),
                               desc=f"Downloading {MODEL_NAME} model...",
                               fail=f"\nFailed to download {MODEL_NAME} model. Please prepare the model according to the steps in below link:\n{MODEL_HELP_URL}")
    if not ret or not zip_path.exists():
        print(f"[ERROR] Failed to download model. Please download manually:\n  {url}")
        sys.exit(1)

    print(f"[INFO] Extracting {zip_name} ...")
    try:
        with zipfile.ZipFile(str(zip_path), "r") as zf:
            zf.extractall(str(model_dir))
    except Exception as e:
        print(f"[ERROR] Extraction failed: {e}")
        if zip_path.exists():
            zip_path.unlink()
        sys.exit(1)

    if zip_path.exists():
        zip_path.unlink()
        print(f"[INFO] Removed archive: {zip_name}")

    if not encoder_bin.exists() or not decoder_bin.exists():
        print(f"[ERROR] Model files not found after extraction in: {model_subdir_path}")
        sys.exit(1)

    print(f"[INFO] Model ready: {model_subdir_path}")
    return model_subdir_path


# ─────────────────────────────────────────────────────────────────────────────
# Tokenizer Helper
# ─────────────────────────────────────────────────────────────────────────────

def get_marian_tokenizer(cache_dir: Path):
    """Load MarianTokenizer from local HuggingFace cache or download it."""
    import ssl
    ssl._create_default_https_context = ssl._create_unverified_context
    os.environ["TRANSFORMERS_VERBOSITY"] = "error"
    from transformers import MarianTokenizer

    def _find_snapshot(base_dir: Path):
        """Return snapshot dir containing tokenizer_config.json, or None."""
        if (base_dir / "tokenizer_config.json").is_file():
            return base_dir
        # HuggingFace cache layout: base_dir/models--<org>--<name>/snapshots/<hash>/
        org, name = HF_MODEL_ID.split("/")
        snapshots_root = base_dir / f"models--{org}--{name}" / "snapshots"
        if snapshots_root.is_dir():
            for snap in snapshots_root.iterdir():
                if (snap / "tokenizer_config.json").is_file():
                    return snap
        return None

    snap_dir = _find_snapshot(cache_dir) if cache_dir.exists() else None
    if snap_dir:
        print(f"      Loading tokenizer from: {snap_dir}")
        return MarianTokenizer.from_pretrained(str(snap_dir))

    print(f"      Tokenizer not found locally, downloading from HuggingFace...")
    cache_dir.mkdir(parents=True, exist_ok=True)
    return MarianTokenizer.from_pretrained(HF_MODEL_ID, cache_dir=str(cache_dir))


# ─────────────────────────────────────────────────────────────────────────────
# Initialization
# ─────────────────────────────────────────────────────────────────────────────

def Init():
    """Initialize models and tokenizer."""
    global encoder_model, decoder_model, tokenizer

    # Step 1: Download model
    print("[1/4] Checking / downloading model...")
    model_subdir = download_opus_model()

    encoder_bin = model_subdir / "encoder.bin"
    decoder_bin = model_subdir / "decoder.bin"

    # Step 2: Load tokenizer
    print("[2/4] Loading tokenizer...")
    tokenizer = get_marian_tokenizer(tokenizer_dir)
    print(f"      Vocab={len(tokenizer)}, BOS={tokenizer.pad_token_id}, "
          f"EOS={tokenizer.eos_token_id}, PAD={tokenizer.pad_token_id}")

    # Step 3: Load encoder and decoder onto NPU via init_htp_model
    print("[3/4] Loading encoder onto NPU...")
    encoder_model = init_htp_model(encoder_bin, OpusMTEncoder, "encoder")
    print(f"      Encoder inputs : {encoder_model.getInputName()}")
    print(f"      Encoder outputs: {encoder_model.getOutputName()}")

    print("[4/4] Loading decoder onto NPU...")
    decoder_model = init_htp_model(decoder_bin, OpusMTDecoder, "decoder")
    print(f"      Decoder inputs : {decoder_model.getInputName()}")
    print(f"      Decoder outputs: {decoder_model.getOutputName()}")


# ─────────────────────────────────────────────────────────────────────────────
# Translation
# ─────────────────────────────────────────────────────────────────────────────

def translate(text: str, verbose: bool = True) -> str:
    """Translate Chinese text to English using encoder+decoder on NPU."""
    BOS_TOKEN_ID = tokenizer.pad_token_id
    EOS_TOKEN_ID = tokenizer.eos_token_id

    if verbose:
        print(f"\n[Translate] Input : {text}")

    # Tokenize
    enc_inputs = tokenizer(
        text,
        return_tensors="np",
        padding="max_length",
        truncation=True,
        max_length=MAX_SEQ_LEN,
    )
    input_ids      = enc_inputs["input_ids"].astype(np.int32)       # [1, 256]
    attention_mask = enc_inputs["attention_mask"].astype(np.int32)  # [1, 256]

    # Encoder inference (with BURST perf profile)
    enc_input_names = encoder_model.getInputName()
    enc_name_to_tensor = {
        "input_ids":              input_ids,
        "encoder_attention_mask": attention_mask,
    }
    enc_in_list = [enc_name_to_tensor[n] for n in enc_input_names]

    t_enc = time.time()
    enc_outputs = run_inference_with_perf_profile(encoder_model, *enc_in_list)
    enc_time = time.time() - t_enc

    # enc_outputs is a list when there are multiple outputs
    if not isinstance(enc_outputs, (list, tuple)):
        enc_outputs = [enc_outputs]
    enc_out_names = encoder_model.getOutputName()
    enc_out_dict  = dict(zip(enc_out_names, enc_outputs))

    # Decoder auto-regressive loop
    dec_input_names = decoder_model.getInputName()

    past_self_key = [np.zeros((1, NUM_HEADS, MAX_SEQ_LEN - 1, HEAD_DIM), dtype=np.float32) for _ in range(NUM_LAYERS)]
    past_self_val = [np.zeros((1, NUM_HEADS, MAX_SEQ_LEN - 1, HEAD_DIM), dtype=np.float32) for _ in range(NUM_LAYERS)]

    cross_key = [np.array(enc_out_dict[f"block_{i}_cross_key_states"],   dtype=np.float32) for i in range(NUM_LAYERS)]
    cross_val = [np.array(enc_out_dict[f"block_{i}_cross_value_states"], dtype=np.float32) for i in range(NUM_LAYERS)]

    generated_tokens = []
    current_token = np.array([[BOS_TOKEN_ID]], dtype=np.int32)  # [1, 1]

    t_dec = time.time()

    for step in range(MAX_GEN_LEN):
        position = np.array([step], dtype=np.int32)  # [1]

        dec_name_to_tensor = {
            "input_ids":              current_token,
            "position":               position,
            "encoder_attention_mask": attention_mask,
        }
        for i in range(NUM_LAYERS):
            dec_name_to_tensor[f"block_{i}_past_self_key_states"]   = past_self_key[i]
            dec_name_to_tensor[f"block_{i}_past_self_value_states"] = past_self_val[i]
            dec_name_to_tensor[f"block_{i}_cross_key_states"]       = cross_key[i]
            dec_name_to_tensor[f"block_{i}_cross_value_states"]     = cross_val[i]

        dec_in_list = [dec_name_to_tensor[n] for n in dec_input_names]
        dec_outputs = decoder_model.Inference(*dec_in_list)

        if not isinstance(dec_outputs, (list, tuple)):
            dec_outputs = [dec_outputs]
        dec_out_names = decoder_model.getOutputName()
        dec_out_dict  = dict(zip(dec_out_names, dec_outputs))

        # Greedy decode
        logits = np.array(dec_out_dict["logits"], dtype=np.float32)  # [1, 1, vocab]
        next_token = int(np.argmax(logits[0, 0]))

        if next_token == EOS_TOKEN_ID:
            break

        generated_tokens.append(next_token)
        current_token[0, 0] = next_token

        # Update self-attention KV cache
        for i in range(NUM_LAYERS):
            psk = np.array(dec_out_dict[f"block_{i}_present_self_key_states"],   dtype=np.float32)
            psv = np.array(dec_out_dict[f"block_{i}_present_self_value_states"], dtype=np.float32)
            if step < MAX_SEQ_LEN - 1:
                past_self_key[i][:, :, step:step + 1, :] = psk
                past_self_val[i][:, :, step:step + 1, :] = psv

    dec_time = time.time() - t_dec

    translated = tokenizer.decode(generated_tokens, skip_special_tokens=True).strip()

    if verbose:
        print(f"[Translate] Output: {translated}")
        print(f"[Timing]    Encoder: {enc_time * 1000:.0f}ms | "
              f"Decoder: {dec_time * 1000:.0f}ms ({len(generated_tokens)} tokens, "
              f"{dec_time / max(len(generated_tokens), 1) * 1000:.0f}ms/tok)")

    return translated


# ─────────────────────────────────────────────────────────────────────────────
# Cleanup
# ─────────────────────────────────────────────────────────────────────────────

def Release():
    """Release model resources."""
    global encoder_model, decoder_model
    if encoder_model is not None:
        del encoder_model
    if decoder_model is not None:
        del decoder_model


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main(input_text=None):
    """Main entry point."""
    Init()

    print("\n" + "=" * 60)
    print("Models loaded. Starting inference...")
    print("=" * 60)

    if input_text:
        translate(input_text, verbose=True)
    else:
        test_sentences = [
            "我爱中国。",
            "今天天气很好。",
            "人工智能正在改变世界。",
            "高通骁龙芯片是移动计算的先驱。",
            "这款模型在NPU上运行，速度非常快。",
        ]

        results = []
        for sentence in test_sentences:
            result = translate(sentence, verbose=True)
            results.append((sentence, result))

        print("\n" + "=" * 60)
        print("Summary of translations:")
        print("=" * 60)
        for zh, en in results:
            print(f"  ZH: {zh}")
            print(f"  EN: {en}")
            print()

    Release()
    print("[DONE] OpusMT-Zh-En inference completed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OpusMT Chinese→English translation on NPU")
    parser.add_argument(
        "--input-text",
        type=str,
        default=None,
        help="Chinese text to translate. If not provided, runs built-in test sentences.",
    )
    args = parser.parse_args()

    main(args.input_text)
