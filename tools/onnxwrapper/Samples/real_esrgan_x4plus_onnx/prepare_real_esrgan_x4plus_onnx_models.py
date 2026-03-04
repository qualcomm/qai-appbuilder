# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
Prepare Real-ESRGAN x4plus ONNX model files.

Behavior
--------
1) Download `real_esrgan_x4plus-onnx-float.zip` to `<current_dir>/models-onnx/`.
2) Extract the zip into the same folder (keep original filenames).
3) Delete the zip after extraction (Windows-safe with retries).
"""

from __future__ import annotations
import os
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

import requests
from tqdm import tqdm

# =========================
# Model configuration
# =========================
MODEL_NAME = "real_esrgan_x4plus"

ONNX_ZIP_FILENAME = "real_esrgan_x4plus-onnx-float.zip"

ONNX_ZIP_URL = (
    "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/"
    "qai-hub-models/models/real_esrgan_x4plus/releases/v0.47.0/"
    "real_esrgan_x4plus-onnx-float.zip"
)


def _resolve_base_dir(cwd: Optional[os.PathLike] = None) -> Path:
    """Use `cwd` if provided, else current working directory."""
    return Path(cwd) if cwd is not None else Path(os.getcwd())


def _verify_complete(url: str, filepath: Path) -> bool:
    """True if file exists and matches remote Content-Length (if available)."""
    if not filepath.exists():
        return False
    try:
        head = requests.head(url, allow_redirects=True, timeout=15)
        if head.status_code >= 400:
            return True
        remote_len = head.headers.get("Content-Length")
        if remote_len is None:
            return True
        return filepath.stat().st_size == int(remote_len)
    except Exception:
        return True


def download_url(url: str, filepath: Path, *, desc: Optional[str] = None) -> bool:
    filepath.parent.mkdir(parents=True, exist_ok=True)

    if _verify_complete(url, filepath):
        return True

    print(desc or f"Downloading {filepath.name} ...")

    try:
        with requests.get(url, stream=True, allow_redirects=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))

            with tqdm(total=total, unit="B", unit_scale=True, desc=filepath.name) as bar:
                with open(filepath, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        f.write(chunk)
                        bar.update(len(chunk))
        return True
    except Exception as e:
        print(f"\nFailed to download from {url}\nReason: {e}")
        return False


def _delete_with_retries(path: Path, retries: int = 10, delay_s: float = 0.25) -> None:
    """Windows-safe delete with retries and a rename fallback."""
    for _ in range(retries):
        try:
            if path.exists():
                path.unlink()
            return
        except PermissionError:
            time.sleep(delay_s)

    if path.exists():
        try:
            tmp = path.with_suffix(path.suffix + ".deleting")
            if tmp.exists():
                tmp.unlink()
            path.rename(tmp)
            tmp.unlink()
        except Exception:
            pass


def ensure_real_esrgan_x4plus_onnx(
    cwd: Optional[os.PathLike] = None,
    *,
    onnx_zip_url: str = ONNX_ZIP_URL,
    unzip: bool = True,
    force: bool = False,
) -> Dict[str, object]:
    """Download + extract Real-ESRGAN x4plus ONNX zip into ./models-onnx."""
    base_dir = _resolve_base_dir(cwd)
    model_dir = base_dir / "models-onnx"
    model_dir.mkdir(parents=True, exist_ok=True)

    onnx_zip_path = model_dir / ONNX_ZIP_FILENAME

    if force and onnx_zip_path.exists():
        _delete_with_retries(onnx_zip_path)

    ok = download_url(
        onnx_zip_url,
        onnx_zip_path,
        desc=f"Downloading {MODEL_NAME} ONNX zip ..."
    )
    if not ok:
        raise RuntimeError(f"Failed to download ONNX zip for {MODEL_NAME}.")

    extracted_files: List[Path] = []

    if unzip:
        with zipfile.ZipFile(onnx_zip_path, "r") as zf:
            zf.extractall(path=model_dir)
            for name in zf.namelist():
                if name.lower().endswith(".onnx"):
                    extracted_files.append(model_dir / name)

        _delete_with_retries(onnx_zip_path)

    return {
        "base_dir": base_dir,
        "model_dir": model_dir,
        "extracted_files": extracted_files,
    }


def main() -> None:
    paths = ensure_real_esrgan_x4plus_onnx(unzip=True)
    print("Extracted the following files:")
    for p in paths.get("extracted_files", []):
        print(f"  {p}")


if __name__ == "__main__":
    main()