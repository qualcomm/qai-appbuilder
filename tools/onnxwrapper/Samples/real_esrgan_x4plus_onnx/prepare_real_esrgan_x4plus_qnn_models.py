# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""prepare_real_esrgan_x4plus_qnn_models.py

Self-contained downloader for Real-ESRGAN x4plus QNN model.

"""

from __future__ import annotations

import os
import sys
import platform
import subprocess
import shutil
from pathlib import Path
from typing import Dict, Optional

import qai_hub


MODEL_ID = "mnz1l2exq"
MODEL_NAME = "real_esrgan_x4plus"
MODEL_HELP_URL = (
    "https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/"
    + MODEL_NAME
    + "#"
    + MODEL_NAME
    + "-qnn-models"
)


HUB_ID_Q = "a916bc04400e033f60fdd73c615e5780e2ba206a"

# qai-hub client config paths
QAI_HUB_CONFIG = os.path.join(Path.home(), ".qai_hub", "client.ini")
QAI_HUB_CONFIG_BACKUP = os.path.join(Path.home(), ".qai_hub", "client.ini.bk")


def _get_system() -> Optional[str]:
    system = platform.system()
    if system == "Windows":
        return "Windows"
    if system == "Linux":
        return "Linux"
    return None


_SYSTEM_NAME = _get_system()


def _run(command: str, *, errdesc: str = "Error running command", live: bool = True, custom_env: Optional[dict] = None) -> str:
    run_kwargs = {
        "args": command,
        "shell": True,
        "env": os.environ if custom_env is None else custom_env,
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


def _run_command(command: str, live: bool = True) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{os.path.abspath('.')}" + os.pathsep + env.get("PYTHONPATH", "")
    stdout = _run(command, errdesc="Error running command", live=live, custom_env=env).strip()
    if stdout:
        print(stdout)


def setup_qai_hub(api_token: str) -> None:
    """Temporarily configure qai-hub using token, with backup/restore support."""
    # Backup existing config
    if os.path.isfile(QAI_HUB_CONFIG):
        try:
            shutil.copy(QAI_HUB_CONFIG, QAI_HUB_CONFIG_BACKUP)
        except Exception:
            pass

    # Configure token via CLI
    if _SYSTEM_NAME == "Windows":
        _run_command(f"qai-hub.exe configure --api_token {api_token} > NUL", live=False)
    elif _SYSTEM_NAME == "Linux":
        _run_command(f"qai-hub configure --api_token {api_token} > /dev/null", live=False)
    else:
        # Unknown OS: rely on python client session token only
        try:
            import qai_hub as hub
            hub.set_session_token(api_token)
        except Exception:
            pass


def reset_qai_hub() -> None:
    if os.path.isfile(QAI_HUB_CONFIG_BACKUP):
        try:
            shutil.copy(QAI_HUB_CONFIG_BACKUP, QAI_HUB_CONFIG)
        except Exception:
            pass


def is_file_exists(filepath: os.PathLike) -> bool:
    return os.path.exists(filepath)


def download_qai_hubmodel(
    model_id: str,
    filepath: os.PathLike,
    *,
    desc: Optional[str] = None,
    fail: Optional[str] = None,
    hub_id: str = HUB_ID_Q,
) -> bool:
    """Embedded version of install.download_qai_hubmodel()."""
    if is_file_exists(filepath):
        return True

    path = os.path.dirname(str(filepath))
    os.makedirs(path, exist_ok=True)

    if desc is not None:
        print(desc)
    else:
        print(f"Downloading {os.path.basename(str(filepath))}...")

    # Allow user override via env var (useful when HUB_ID_Q is not permitted)
    token = os.environ.get("QAI_HUB_API_TOKEN", "").strip() or hub_id

    setup_qai_hub(token)
    try:
        model = qai_hub.get_model(model_id)
        model.download(filename=str(filepath))
        return True
    except Exception as e:
        print("[DEBUG] qai_hub download exception:", repr(e), "")
        if fail is not None:
            print(fail)
        else:
            print(
                f"Failed to download model {os.path.basename(str(filepath))} from AI Hub. "
                f"Please try to download it manually and place it to {filepath}."
            )
            print("If you still can't download, please consider using proxy.")
        return False
    finally:
        reset_qai_hub()


# ---------------------------------------------------------------------
# Real-ESRGAN x4plus downloader flow
# ---------------------------------------------------------------------

def _resolve_execution_ws(cwd: Optional[os.PathLike] = None) -> Path:
    """Replicate the working directory resolution behavior used in real_esrgan_x4plus.py."""
    execution_ws = Path(cwd) if cwd is not None else Path(os.getcwd())
    return execution_ws


def ensure_real_esrgan_x4plus_qnn_model(
    cwd: Optional[os.PathLike] = None,
    *,
    force: bool = False,
) -> Dict[str, Path]:
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

    ok = download_qai_hubmodel(MODEL_ID, str(model_path), desc=desc, fail=fail)
    if not ok:
        raise RuntimeError(f"Failed to download required model for {MODEL_NAME}.")

    return {
        "execution_ws": execution_ws,
        "model_dir": model_dir,
        "model_path": model_path,
    }


def main() -> None:
    force = os.environ.get("FORCE_DOWNLOAD", "").strip() in ("1", "true", "TRUE", "yes", "YES")
    paths = ensure_real_esrgan_x4plus_qnn_model(force=force)
    print("Downloaded/verified the following files:")
    for k, v in paths.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()