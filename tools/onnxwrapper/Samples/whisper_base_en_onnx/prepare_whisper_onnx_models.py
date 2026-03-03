#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

import os
import shutil
import stat
import time
import subprocess
import urllib.request
from urllib.error import URLError, HTTPError


# -----------------------------
# Configuration
# -----------------------------

# Hugging Face repo for sherpa-onnx Whisper base.en
GIT_REPO_URL = "https://huggingface.co/csukuangfj/sherpa-onnx-whisper-base.en"
GIT_BRANCH = "main"

# Output folder for ONNX models
MODELS_DIR = "models-onnx"

# Temp folder for git sparse checkout
TMP_REPO_DIR = "_tmp_whisper_repo"

# Only download these two ONNX files from the repo
ENCODER_ONNX = "base.en-encoder.onnx"
DECODER_ONNX = "base.en-decoder.onnx"

# Download these two assets into current directory (./)
MEL_FILTERS_URL = (
    "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/"
    "qai-hub-models/models/whisper_asr_shared/v1/openai_assets/mel_filters.npz"
)
JFK_WAV_URL = (
    "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/"
    "qai-hub-models/models/whisper_asr_shared/v1/audio/jfk.wav"
)


# -----------------------------
# Command helper
# -----------------------------

def run_cmd(cmd, cwd=None, check=True):
    """
    Run a command and optionally raise if it fails.
    """
    print(f"[CMD] {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=cwd, check=check)


# -----------------------------
# Robust Windows cleanup helpers
# -----------------------------

def _rmtree_onerror(func, path, exc_info):
    """
    Error handler for shutil.rmtree on Windows:
    - Remove read-only attribute and retry once.
    - Some git files can be read-only or temporarily locked.
    """
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        # Re-raise to allow outer retry / fallback handling
        raise


def safe_rmtree(dir_path: str):
    """
    Robustly delete a directory on Windows.
    If deletion fails due to transient file locks (e.g. antivirus/indexer/git),
    fall back to renaming the folder so the script can continue.
    """
    if not os.path.exists(dir_path):
        return

    # Best-effort: run 'git gc' to settle pack files before deletion
    git_dir = os.path.join(dir_path, ".git")
    if os.path.isdir(git_dir):
        try:
            run_cmd(["git", "gc", "--prune=now"], cwd=dir_path, check=False)
        except Exception:
            pass

    # Try a few times because Windows file locks can be transient
    for attempt in range(3):
        try:
            shutil.rmtree(dir_path, onerror=_rmtree_onerror)
            return
        except PermissionError:
            time.sleep(0.2)

    # Final fallback: rename the directory and continue
    fallback_name = f"{dir_path}_to_delete"
    try:
        if os.path.exists(fallback_name):
            shutil.rmtree(fallback_name, onerror=_rmtree_onerror)
        os.replace(dir_path, fallback_name)
        print(f"[WARN] Could not delete '{dir_path}' due to Windows file lock. Renamed to '{fallback_name}'.")
    except Exception as e:
        print(f"[WARN] Could not delete or rename '{dir_path}'. You can delete it manually later. Error: {e}")


# -----------------------------
# Download helpers
# -----------------------------

def download_url_to_cwd(url: str, output_name: str, overwrite: bool = True) -> str:
    """
    Download a file from `url` and save it into the current working directory.
    Uses a temporary file + atomic replace to avoid leaving a corrupted partial file.
    """
    final_path = os.path.join(os.getcwd(), output_name)

    if os.path.exists(final_path) and not overwrite:
        print(f"[SKIP] {output_name} already exists at: {final_path}")
        return final_path

    tmp_path = final_path + ".tmp"

    print(f"[INFO] Downloading URL: {url}")
    print(f"[INFO] Saving to: {final_path}")

    try:
        with urllib.request.urlopen(url) as resp, open(tmp_path, "wb") as f:
            chunk_size = 1024 * 1024  # 1MB
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)

        if os.path.exists(final_path):
            os.remove(final_path)
        os.replace(tmp_path, final_path)

        print(f"[OK] Downloaded: {output_name}")
        return final_path

    except (HTTPError, URLError) as e:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise RuntimeError(f"Failed to download from URL: {url}. Error: {e}") from e


def download_whisper_onnx_via_git_sparse_checkout():
    """
    Download ONLY base.en-encoder.onnx and base.en-decoder.onnx via git clone workflow,
    using sparse-checkout to avoid downloading the full repository.
    Save the two ONNX files into ./models/.
    """
    os.makedirs(MODELS_DIR, exist_ok=True)

    # Clean temp repo if exists (robustly)
    safe_rmtree(TMP_REPO_DIR)

    # Initialize an empty git repo
    run_cmd(["git", "init", TMP_REPO_DIR])

    # Add remote
    run_cmd(["git", "remote", "add", "origin", GIT_REPO_URL], cwd=TMP_REPO_DIR)

    # Try the modern sparse-checkout workflow first
    # (git >= 2.25 typically supports this)
    modern_ok = True
    try:
        run_cmd(["git", "sparse-checkout", "init", "--cone"], cwd=TMP_REPO_DIR)
        run_cmd(["git", "sparse-checkout", "set", ENCODER_ONNX, DECODER_ONNX], cwd=TMP_REPO_DIR)
    except Exception:
        modern_ok = False

    # Fallback for older git versions:
    # Use core.sparseCheckout + write patterns into .git/info/sparse-checkout
    if not modern_ok:
        run_cmd(["git", "config", "core.sparseCheckout", "true"], cwd=TMP_REPO_DIR)
        sparse_file = os.path.join(TMP_REPO_DIR, ".git", "info", "sparse-checkout")
        with open(sparse_file, "w", encoding="utf-8") as f:
            f.write(f"{ENCODER_ONNX}\n")
            f.write(f"{DECODER_ONNX}\n")

    # Pull only the selected files
    run_cmd(["git", "pull", "origin", GIT_BRANCH], cwd=TMP_REPO_DIR)

    # Move ONNX files to ./models (no extra subfolder)
    for fname in [ENCODER_ONNX, DECODER_ONNX]:
        src = os.path.join(TMP_REPO_DIR, fname)
        dst = os.path.join(MODELS_DIR, fname)

        if not os.path.exists(src):
            raise FileNotFoundError(f"Expected file not found in repo checkout: {fname}")

        if os.path.exists(dst):
            os.remove(dst)

        shutil.move(src, dst)
        print(f"[OK] Saved ONNX model: {dst}")

    # Cleanup temp repo (robustly; no crash even if Windows locks files)
    safe_rmtree(TMP_REPO_DIR)


# -----------------------------
# Main
# -----------------------------

if __name__ == "__main__":
    # 1) Download ONNX models via git sparse-checkout
    download_whisper_onnx_via_git_sparse_checkout()

    # 2) Download auxiliary assets to current directory
    mel_path = download_url_to_cwd(MEL_FILTERS_URL, "mel_filters.npz")
    wav_path = download_url_to_cwd(JFK_WAV_URL, "jfk.wav")

    print("\n[DONE] All required files are ready:")
    print("  - ONNX models (saved to ./models-onnx):")
    print(f"    * {os.path.join(MODELS_DIR, ENCODER_ONNX)}")
    print(f"    * {os.path.join(MODELS_DIR, DECODER_ONNX)}")
    print("  - Assets (saved to current directory):")
    print(f"    * {mel_path}")
    print(f"    * {wav_path}")