# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""prepare_yolov8_det_qnn_models.py

Self-contained downloader for YOLOv8-det QNN model.

"""

from __future__ import annotations

import os
import platform
import subprocess
import shutil
from pathlib import Path
from typing import Dict, Optional

import qai_hub

# ---------------------------------------------------------------------
# Constants (from <File>yolov8_det.py</File>)
# ---------------------------------------------------------------------
MODEL_ID = "mqp35e9lm"
MODEL_NAME = "yolov8_det"
MODEL_HELP_URL = (
    "https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/"
    + MODEL_NAME
    + "#"
    + MODEL_NAME
    + "-qnn-models"
)

# ---------------------------------------------------------------------
# Default AI Hub token (from <File>install.py</File>)
# Allow override via env var QAI_HUB_API_TOKEN.
# ---------------------------------------------------------------------
HUB_ID_Q = "a916bc04400e033f60fdd73c615e5780e2ba206a"

QAI_HUB_CONFIG = os.path.join(Path.home(), ".qai_hub", "client.ini")
QAI_HUB_CONFIG_BACKUP = os.path.join(Path.home(), ".qai_hub", "client.ini.bk")


def _get_system() -> Optional[str]:
    sysname = platform.system()
    if sysname == "Windows":
        return "Windows"
    if sysname == "Linux":
        return "Linux"
    return None


_SYSTEM = _get_system()


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


def setup_qai_hub(api_token: str) -> None:
    """Configure qai-hub client with a token, backing up existing config."""
    # backup existing config
    if os.path.isfile(QAI_HUB_CONFIG):
        try:
            shutil.copy(QAI_HUB_CONFIG, QAI_HUB_CONFIG_BACKUP)
        except Exception:
            pass

    if _SYSTEM == "Windows":
        _run(f"qai-hub.exe configure --api_token {api_token} > NUL", errdesc="Failed to configure qai-hub", live=False)
    elif _SYSTEM == "Linux":
        _run(f"qai-hub configure --api_token {api_token} > /dev/null", errdesc="Failed to configure qai-hub", live=False)
    else:
        # fallback: set session token if CLI isn't available
        try:
            import qai_hub as hub
            hub.set_session_token(api_token)
        except Exception:
            pass


def reset_qai_hub() -> None:
    """Restore backed up qai-hub config if it exists."""
    if os.path.isfile(QAI_HUB_CONFIG_BACKUP):
        try:
            shutil.copy(QAI_HUB_CONFIG_BACKUP, QAI_HUB_CONFIG)
        except Exception:
            pass


def _resolve_execution_ws(cwd: Optional[os.PathLike] = None) -> Path:
    """Match <File>yolov8_det.py</File> workspace resolution."""
    execution_ws = Path(cwd) if cwd is not None else Path(os.getcwd())
    return execution_ws


def download_qai_hub_model(model_id: str, filepath: Path, *, desc: str, fail: str) -> bool:
    if filepath.exists():
        return True

    filepath.parent.mkdir(parents=True, exist_ok=True)
    print(desc)

    token = os.environ.get("QAI_HUB_API_TOKEN", "").strip() or HUB_ID_Q

    try:
        setup_qai_hub(token)
        model = qai_hub.get_model(model_id)
        model.download(filename=str(filepath))
        return True
    except Exception as e:
        print("[DEBUG] qai_hub download exception:", repr(e), "")
        print(fail)
        return False
    finally:
        reset_qai_hub()


def ensure_yolov8_det_qnn_model(cwd: Optional[os.PathLike] = None, *, force: bool = False) -> Dict[str, Path]:
    execution_ws = _resolve_execution_ws(cwd)
    model_dir = execution_ws / "models-qnn"
    model_path = model_dir / f"{MODEL_NAME}.bin"

    if force and model_path.exists():
        try:
            model_path.unlink()
        except Exception:
            pass

    desc = f"Downloading {MODEL_NAME} model... "
    fail = (
        f"Failed to download {MODEL_NAME} model. Please prepare the model according to the steps in below link:"
        f"{MODEL_HELP_URL}"
    )

    ok = download_qai_hub_model(MODEL_ID, model_path, desc=desc, fail=fail)
    if not ok:
        raise RuntimeError(f"Failed to download required model for {MODEL_NAME}.")

    return {
        "execution_ws": execution_ws,
        "model_dir": model_dir,
        "model_path": model_path,
    }


def main() -> None:
    force = os.environ.get("FORCE_DOWNLOAD", "").strip() in ("1", "true", "TRUE", "yes", "YES")
    paths = ensure_yolov8_det_qnn_model(force=force)
    print("Downloaded/verified the following files:")
    for k, v in paths.items():
        print(f" {k}: {v}")


if __name__ == "__main__":
    main()