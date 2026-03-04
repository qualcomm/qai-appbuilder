# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Self-contained model and asset downloader for Facemap 3DMM sample.

Usage
-----
CLI:
    python facemap_3dmm_downloader.py

"""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path
from typing import Dict, Optional

import requests
from tqdm import tqdm
import urllib.request as request
import qai_hub


MODEL_ID = "mqyy9zd9q"
HUB_ID_H = "ox06ibpbkxb4pr0mcyfe7wqgx5pf5r0cm3rf3dzi"
MODEL_NAME = "facemap_3dmm"
MODEL_HELP_URL = (
    "https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/"
    + MODEL_NAME
    + "#"
    + MODEL_NAME
    + "-qnn-models"
)

FACE_IMG_FBOX_PATH_URL = (
    "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/"
    "facemap_3dmm/v1/face_img_fbox.txt"
)
MEANFACE_PATH_URL = (
    "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/"
    "facemap_3dmm/v1/meanFace.npy"
)
SHAPEBASIS_PATH_URL = (
    "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/"
    "facemap_3dmm/v1/shapeBasis.npy"
)
BLENDSHAPE_PATH_URL = (
    "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/"
    "facemap_3dmm/v1/blendShape.npy"
)



def _get_system() -> Optional[str]:
    system = platform.system()
    if system == "Windows":
        return "Windows"
    if system == "Linux":
        return "Linux"
    return None


_SYSTEM_NAME = _get_system()


def _run(command: str, *, errdesc: str = "Error running command", live: bool = True) -> str:
    run_kwargs = {
        "args": command,
        "shell": True,
        "env": os.environ,
        "errors": "ignore",
    }
    if not live:
        run_kwargs["stdout"] = run_kwargs["stderr"] = subprocess.PIPE

    result = subprocess.run(**run_kwargs)
    if result.returncode != 0:
        error_bits = [
            f"{errdesc}.",
            f"Command: {command}",
            f"Error code: {result.returncode}",
        ]
        if getattr(result, "stdout", None):
            error_bits.append(f"stdout: {result.stdout}")
        if getattr(result, "stderr", None):
            error_bits.append(f"stderr: {result.stderr}")
        raise RuntimeError("".join(error_bits))
    return (getattr(result, "stdout", None) or "")


def _is_file_exists(filepath: os.PathLike) -> bool:
    return os.path.exists(filepath)


def _verify_package(url: str, filepath: os.PathLike, filesize: Optional[int] = None) -> bool:
    """Verify if a downloaded file is already complete (by size)."""
    if os.path.exists(filepath):
        local_size = os.path.getsize(filepath)
        if filesize is not None:
            actual_size = filesize
        else:
            resp = request.urlopen(url)
            actual_size = int(resp.headers.get("Content-Length", 0))
        return actual_size == local_size
    return False


def download_url_requests(
    url: str,
    filepath: os.PathLike,
    *,
    filesize: Optional[int] = None,
    desc: Optional[str] = None,
    fail: Optional[str] = None,
    chunk_size: int = 8192,
) -> bool:
    """Download a URL to filepath using requests (fallback)."""
    # Disable warnings for insecure request since install.py sets verify=False.
    from requests.packages.urllib3.exceptions import InsecureRequestWarning

    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

    if _verify_package(url, filepath, filesize):
        return True

    path = os.path.dirname(str(filepath))
    os.makedirs(path, exist_ok=True)

    try:
        if desc:
            print(desc)
        response = requests.get(url, stream=True, verify=False)
        if response.status_code != 200:
            raise ValueError(f"Unable to download file at {url}")
        total_size = int(response.headers.get("content-length", 0))
        with tqdm(total=total_size, unit="B", unit_scale=True, desc=os.path.basename(str(filepath))) as bar:
            with open(filepath, "wb") as f:
                for data in response.iter_content(chunk_size=chunk_size):
                    f.write(data)
                    bar.update(len(data))
        return True
    except Exception:
        print()
        if fail:
            print(fail)
        else:
            print(f"Failed to download file from {url}. Please try to download it manually and place it to {filepath}.")
            print("If you still can't download, please consider using proxy.")
        return False


def download_url_wget(
    url: str,
    filepath: os.PathLike,
    *,
    filesize: Optional[int] = None,
    desc: Optional[str] = None,
    fail: Optional[str] = None,
) -> bool:
    """Download a URL to filepath using wget if available."""
    if _verify_package(url, filepath, filesize):
        return True

    path = os.path.dirname(str(filepath))
    if path:
        os.makedirs(path, exist_ok=True)

    if fail is None:
        fail = f"Failed to download file from {url}. Please try to download it manually and place it to {filepath}."
        fail += "If you still can't download, please consider using proxy."

    try:
        # Use system wget on Linux; on Windows, the original install.py expects a bundled wget.exe.
        if _SYSTEM_NAME == "Linux":
            command = f'"wget" --no-check-certificate -q --show-progress --continue -P "{path}" -O "{filepath}" {url}'
            if desc:
                print(desc)
            print(command)
            _run(command, errdesc=fail, live=True)
            return True

        if _SYSTEM_NAME == "Windows":
            # Keep same search behavior as install.py (tools\wget\wget.exe or ..\python\tools\wget\wget.exe)
            wget_exe = "tools\wget\wget.exe"
            if not os.path.exists(wget_exe):
                wget_exe = "..\python\tools\wget\wget.exe"
            if not os.path.exists(wget_exe):
                # No bundled wget.exe -> let caller fall back
                return False
            command = f'"{wget_exe}" --no-check-certificate -q --show-progress --continue -P "{path}" -O "{filepath}" {url}'
            if desc:
                print(desc)
            print(command)
            _run(command, errdesc=fail, live=True)
            return True

        return False
    except Exception:
        return False


def download_url(url: str, filepath: os.PathLike, *, desc: Optional[str] = None, fail: Optional[str] = None) -> bool:
    """Default download function (install.py uses wget). Falls back to requests."""
    ok = download_url_wget(url, filepath, desc=desc, fail=fail)
    if ok:
        return True
    return download_url_requests(url, filepath, desc=desc, fail=fail)


def setup_qai_hub(hub_id: str) -> None:
    """Configure qai-hub client with a token (best-effort).

    install.py rewrites ~/.qai_hub/client.ini via `qai-hub configure --api_token`.
    Here we keep the same command paths (qai-hub.exe on Windows).
    """
    if _SYSTEM_NAME == "Windows":
        _run(f"qai-hub.exe configure --api_token {hub_id} > NUL", errdesc="Failed to configure qai-hub", live=False)
    elif _SYSTEM_NAME == "Linux":
        _run(f"qai-hub configure --api_token {hub_id} > /dev/null", errdesc="Failed to configure qai-hub", live=False)


def download_qai_hubmodel(
    model_id: str,
    filepath: os.PathLike,
    *,
    desc: Optional[str] = None,
    fail: Optional[str] = None,
    hub_id: str = HUB_ID_H,
) -> bool:
    """Download a model from QAI Hub to filepath.

    Mirrors install.py behavior:
    - Skip if already exists
    - Configure qai-hub client with token
    - qai_hub.get_model(model_id).download(filename=filepath)
    """
    if _is_file_exists(filepath):
        return True

    os.makedirs(os.path.dirname(str(filepath)), exist_ok=True)

    if desc:
        print(desc)
    else:
        print(f"Downloading {os.path.basename(str(filepath))}...")

    try:
        setup_qai_hub(hub_id)
        model = qai_hub.get_model(model_id)
        model.download(filename=str(filepath))
        return True
    except Exception:
        print()
        if fail:
            print(fail)
        else:
            print(
                f"Failed to download model {os.path.basename(str(filepath))} from AI Hub. "
                f"Please try to download it manually and place it to {filepath}."
                "If you still can't download, please consider using proxy."
            )
        return False


# ---------------------------------------------------------------------
# Facemap 3DMM downloader 
# ---------------------------------------------------------------------

def _resolve_execution_ws(cwd: Optional[os.PathLike] = None) -> Path:
    """Replicates the original sample's working directory resolution."""
    execution_ws = Path(cwd) if cwd is not None else Path(os.getcwd())
    return execution_ws


def ensure_facemap_3dmm_assets(
    cwd: Optional[os.PathLike] = None,
    *,
    hub_id: str = HUB_ID_H,
    model_id: str = MODEL_ID,
    force: bool = False,
) -> Dict[str, Path]:
    """Download Facemap 3DMM QNN model and associated assets if missing."""

    execution_ws = _resolve_execution_ws(cwd)
    model_dir = execution_ws / "models-qnn"
    model_path = model_dir / f"{MODEL_NAME}.bin"

    face_img_fbox_path = execution_ws / "face_img_fbox.txt"
    meanFace_path = execution_ws / "meanFace.npy"
    shapeBasis_path = execution_ws / "shapeBasis.npy"
    blendShape_path = execution_ws / "blendShape.npy"

    if force:
        for p in [face_img_fbox_path, meanFace_path, shapeBasis_path, blendShape_path, model_path]:
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass

    ret = True
    if not face_img_fbox_path.exists():
        ret = download_url(FACE_IMG_FBOX_PATH_URL, face_img_fbox_path)
    if ret and (not meanFace_path.exists()):
        ret = download_url(MEANFACE_PATH_URL, meanFace_path)
    if ret and (not shapeBasis_path.exists()):
        ret = download_url(SHAPEBASIS_PATH_URL, shapeBasis_path)
    if ret and (not blendShape_path.exists()):
        ret = download_url(BLENDSHAPE_PATH_URL, blendShape_path)

    desc = f"Downloading {MODEL_NAME} model... "
    fail = (
        f"Failed to download {MODEL_NAME} model. Please prepare the model according to the steps in below link:"
        f"{MODEL_HELP_URL}"
    )
    if ret:
        ret = download_qai_hubmodel(
            model_id,
            model_path,
            desc=desc,
            fail=fail,
            hub_id=hub_id,
        )

    if not ret:
        raise RuntimeError(f"Failed to download required assets/model for {MODEL_NAME}.")

    return {
        "execution_ws": execution_ws,
        "model_path": model_path,
        "face_img_fbox_path": face_img_fbox_path,
        "meanFace_path": meanFace_path,
        "shapeBasis_path": shapeBasis_path,
        "blendShape_path": blendShape_path,
    }


def main() -> None:
    paths = ensure_facemap_3dmm_assets()
    print("Downloaded/verified the following files:")
    for k, v in paths.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()