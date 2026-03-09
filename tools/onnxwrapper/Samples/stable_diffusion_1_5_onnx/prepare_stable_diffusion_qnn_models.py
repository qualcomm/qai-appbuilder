#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

# ---------------------------------------------------------------------
# Download Stable Diffusion 1.5 QNN model assets into ./models-qnn
#
# Directory layout:
# models-qnn/
# ├── scheduler        (directory only; scheduler is constructed in code, no HF download)
# ├── text_encoder
# ├── tokenizer        (downloaded via CLIPTokenizer.from_pretrained with HF_ENDPOINT mirror)
# ├── unet
# ├── vae_decoder
# └── vae_encoder      (directory only, no model download)
#
# QNN binaries are downloaded from Qualcomm AI Hub using qai_hub SDK.
# 
#
# Requirements:
#   pip install -U qai_hub transformers
# ---------------------------------------------------------------------

import os
import shutil
import argparse
import platform
import subprocess
from pathlib import Path


MODEL_NAME = "stable_diffusion_v1_5"

# AI Hub model IDs
MODEL_ID_TEXT_ENCODER = "mn4ke9yzm"
MODEL_ID_UNET = "mqvvdydeq"
MODEL_ID_VAE_DECODER = "mmx0424em"

# QNN binary filenames
TEXT_ENCODER_MODEL_NAME = MODEL_NAME + "_w8a16_quantized-textencoderquantizable-qualcomm_snapdragon_x_elite.bin"
UNET_MODEL_NAME = MODEL_NAME + "_w8a16_quantized-unetquantizable-qualcomm_snapdragon_x_elite.bin"
VAE_DECODER_MODEL_NAME = MODEL_NAME + "_w8a16_quantized-vaedecoderquantizable-qualcomm_snapdragon_x_elite.bin"
# Hugging Face tokenizer source (same as reference script)
TOKENIZER_MODEL_NAME = "openai/clip-vit-large-patch14"

# Hugging Face mirror endpoint used in reference script to avoid 401 gated access
DEFAULT_HF_ENDPOINT = "https://hf-api.gitee.com"


# ============================================================
# Utils
# ============================================================
def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


def run_cmd(cmd: str, silent: bool = False):
    kwargs = {
        "args": cmd,
        "shell": True,
        "env": os.environ.copy(),
    }
    if silent:
        kwargs["stdout"] = kwargs["stderr"] = subprocess.DEVNULL
    ret = subprocess.run(**kwargs)
    if ret.returncode != 0:
        raise RuntimeError(f"Command failed: {cmd}")


def get_system():
    return platform.system()


# ============================================================
# AI Hub helpers 
# ============================================================
def configure_qai_hub(api_token: str | None):
    if not api_token:
        return

    if get_system() == "Windows":
        try:
            run_cmd(f"qai-hub.exe configure --api_token {api_token}", silent=True)
        except Exception:
            run_cmd(f"qai-hub configure --api_token {api_token}", silent=True)
    else:
        run_cmd(f"qai-hub configure --api_token {api_token}", silent=True)


def download_qai_hub_model(model_id: str, out_file: Path):
    if out_file.exists() and out_file.stat().st_size > 0:
        print(f"[AIHub] Exists, skip: {out_file.name}")
        return True

    ensure_dir(out_file.parent)
    print(f"[AIHub] Downloading {out_file.name} (model_id={model_id})")

    try:
        import qai_hub
    except ImportError:
        raise RuntimeError("Missing dependency: qai_hub (pip install -U qai_hub)")

    model = qai_hub.get_model(model_id)
    model.download(filename=str(out_file))

    if not out_file.exists() or out_file.stat().st_size == 0:
        raise RuntimeError(f"Download failed: {out_file}")

    return True


# ============================================================
# Tokenizer download 
# ============================================================
def prepare_tokenizer(tokenizer_dir: Path, hf_endpoint: str | None = None):
    """Prepare tokenizer files under models-qnn/tokenizer.

    This follows the same *loading strategy* used by <File>stable_diffusion_v2_1.py</File>:
      1) If tokenizer_dir exists and no .locks -> try loading strictly local.
      2) Otherwise -> download/cache via CLIPTokenizer.from_pretrained(..., subfolder='tokenizer', cache_dir=tokenizer_dir)

    Additionally, this function is hardened for partially-populated cache directories on Windows:
      - If local load fails (e.g. vocab_file is NoneType), we fall back to re-download.
      - After download, we ensure vocab.json + merges.txt exist at tokenizer_dir root.
    """
    ensure_dir(tokenizer_dir)

    os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    os.environ["HF_ENDPOINT"] = hf_endpoint or DEFAULT_HF_ENDPOINT
    print(f"[HF] Using HF_ENDPOINT={os.environ['HF_ENDPOINT']}")

    try:
        from transformers import CLIPTokenizer
    except ImportError:
        raise RuntimeError("Missing dependency: transformers (pip install -U transformers)")

    def _ensure_vocab_merges_at_root():
        vocab = tokenizer_dir / "vocab.json"
        merges = tokenizer_dir / "merges.txt"
        if vocab.exists() and merges.exists():
            return
        found_vocab = list(tokenizer_dir.rglob("vocab.json"))
        found_merges = list(tokenizer_dir.rglob("merges.txt"))
        if found_vocab and found_merges:
            shutil.copy2(found_vocab[0], vocab)
            shutil.copy2(found_merges[0], merges)
        if not vocab.exists() or not merges.exists():
            raise RuntimeError(f"Tokenizer files not found in {tokenizer_dir}. Expected vocab.json and merges.txt")

    # 1) Try local load if dir exists and not locked
    locks_dir = tokenizer_dir / ".locks"
    tried_local = False
    if tokenizer_dir.exists() and not locks_dir.exists():
        tried_local = True
        try:
            print(f"[Tokenizer] Loading local tokenizer from: {tokenizer_dir}")
            CLIPTokenizer.from_pretrained(str(tokenizer_dir), local_files_only=True)
            _ensure_vocab_merges_at_root()
            print(f"[HF] Tokenizer ready: {tokenizer_dir}")
            return
        except Exception as e:
            # Local cache might be partial or missing required fields (e.g., vocab_file None)
            print(f"[Tokenizer] Local load failed, will re-download. Reason: {e}")

    # 2) Download/cache from HF (mirror) into cache_dir
    try:
        print(f"[Tokenizer] Downloading tokenizer to: {tokenizer_dir}")

        CLIPTokenizer.from_pretrained(
            TOKENIZER_MODEL_NAME,
            cache_dir=str(tokenizer_dir),
            local_files_only=False,
        )

        _ensure_vocab_merges_at_root()
        print(f"[HF] Tokenizer ready: {tokenizer_dir}")
    except Exception as e:
        raise RuntimeError(
            "Failed to download tokenizer model via CLIPTokenizer.from_pretrained."
            f"HF_ENDPOINT={os.environ.get('HF_ENDPOINT')}"
            f"Error: {e}"
        )

# ============================================================
# Main
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="Download Stable Diffusion 1.5 QNN models into ./models-qnn"
    )
    parser.add_argument("--out_dir", type=str, default="models-qnn", help="Output directory")
    parser.add_argument(
        "--api_token",
        type=str,
        default=os.environ.get("AIHUB_API_TOKEN", ""),
        help="Qualcomm AI Hub API token (or env AIHUB_API_TOKEN)",
    )
    parser.add_argument(
        "--hf_endpoint",
        type=str,
        default=os.environ.get("HF_ENDPOINT", ""),
        help="Optional Hugging Face endpoint mirror (default uses hf-api.gitee.com)",
    )

    args = parser.parse_args()
    out_root = Path.cwd() / args.out_dir

    # Create directory structure
    dirs = {
        "scheduler": out_root / "scheduler",
        "text_encoder": out_root / "text_encoder",
        "tokenizer": out_root / "tokenizer",
        "unet": out_root / "unet",
        "vae_decoder": out_root / "vae_decoder",
        "vae_encoder": out_root / "vae_encoder",  # directory only
    }
    for d in dirs.values():
        ensure_dir(d)

    # 1) Prepare tokenizer (NO snapshot_download to avoid 401)
    prepare_tokenizer(dirs["tokenizer"], hf_endpoint=(args.hf_endpoint or None))

    # 2) AI Hub configuration
    configure_qai_hub(args.api_token.strip() or None)

    # 3) Download QNN models
    download_qai_hub_model(
        MODEL_ID_TEXT_ENCODER,
        dirs["text_encoder"] / TEXT_ENCODER_MODEL_NAME,
    )
    download_qai_hub_model(
        MODEL_ID_UNET,
        dirs["unet"] / UNET_MODEL_NAME,
    )
    download_qai_hub_model(
        MODEL_ID_VAE_DECODER,
        dirs["vae_decoder"] / VAE_DECODER_MODEL_NAME,
    )

    # vae_encoder: directory only (intentionally no model download)
    readme = dirs["vae_encoder"] / "README.txt"
    if not readme.exists():
        readme.write_text(
            "VAE encoder QNN model is not provided in the reference SD 2.1 QNN set.\n",
            encoding="utf-8",
        )

    # scheduler: directory only (scheduler is constructed in inference code)
    note = dirs["scheduler"] / "README.txt"
    if not note.exists():
        note.write_text(
            "Scheduler files are not downloaded. Use DPMSolverMultistepScheduler(...) in code.\n",
            encoding="utf-8",
        )

    print("\n[OK] All assets downloaded.")
    print(f"Root directory: {out_root}")


if __name__ == "__main__":
    main()