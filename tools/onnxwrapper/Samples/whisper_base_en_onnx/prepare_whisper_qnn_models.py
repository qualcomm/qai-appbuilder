#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

import os
import shutil
import platform
from pathlib import Path

import requests
from tqdm import tqdm
import urllib.request as request
import qai_hub



HUB_ID_Q = "a916bc04400e033f60fdd73c615e5780e2ba206a"  # default hub token used by download_qai_hubmodel
QAI_HUB_CONFIG = os.path.join(Path.home(), ".qai_hub", "client.ini")
QAI_HUB_CONFIG_BACKUP = os.path.join(Path.home(), ".qai_hub", "client.ini.bk")


def get_system():
    system = platform.system()
    if system == "Windows":
        return "Windows"
    elif system == "Linux":
        return "Linux"
    return None


SYSTEM_NAME = get_system()


def run_command(command: str, live: bool = True):
    """
    Minimal runner for qai-hub configure commands.
    Here we only need to invoke the CLI configure for qai-hub.
    """
    import subprocess

    run_kwargs = {
        "args": command,
        "shell": True,
        "env": os.environ.copy(),
        "errors": "ignore",
    }
    if not live:
        run_kwargs["stdout"] = run_kwargs["stderr"] = subprocess.PIPE

    result = subprocess.run(**run_kwargs)
    if result.returncode != 0:
        raise RuntimeError(f"Error running command: {command}\nReturn code: {result.returncode}")
    return (result.stdout or "") if hasattr(result, "stdout") else ""


def setup_qai_hub(hub_id: str):
    # Backup existing config
    if os.path.isfile(QAI_HUB_CONFIG):
        shutil.copy(QAI_HUB_CONFIG, QAI_HUB_CONFIG_BACKUP)

    # Configure token
    if SYSTEM_NAME == "Windows":
        # uses qai-hub.exe on Windows
        run_command(f"qai-hub.exe configure --api_token {hub_id} > NUL", live=False)
    elif SYSTEM_NAME == "Linux":
        run_command(f"qai-hub configure --api_token {hub_id} > /dev/null", live=False)


def reset_qai_hub():
    if os.path.isfile(QAI_HUB_CONFIG_BACKUP):
        shutil.copy(QAI_HUB_CONFIG_BACKUP, QAI_HUB_CONFIG)


def is_file_exists(filepath: str) -> bool:
    return os.path.exists(filepath)


def verify_package(url, filepath, filesize=None, desc=None, fail=None):
    """
    verify if package is ready.
    1. file exists
    2. file size matches remote Content-Length (or provided filesize)
    """
    if os.path.exists(filepath):
        local_size = os.path.getsize(filepath)
        if filesize is not None:
            actual_size = filesize
        else:
            response = request.urlopen(url)
            actual_size = int(response.headers["Content-Length"])
        if actual_size == local_size:
            return True
        else:
            print()
            print(f"The file '{filepath}' is not ready. Please wait for downloading to complete.")
            if desc is not None:
                print(desc)
            else:
                print(f"Downloading {os.path.basename(filepath)}...")
            return False
    return False


def download_url_requests(url, filepath, filesize=None, desc=None, fail=None, chunk_size=8192):
    # Disable warning for insecure request since we set 'verify=False'.
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

    if verify_package(url, filepath, filesize, desc, fail):
        return True

    os.makedirs(os.path.dirname(filepath), exist_ok=True) if os.path.dirname(filepath) else None

    try:
        response = requests.get(url, stream=True, verify=False)
        if response.status_code != 200:
            raise ValueError(f"Unable to download file at {url}")
        total_size = int(response.headers.get("content-length", 0))

        if desc is not None:
            print(desc)

        with tqdm(total=total_size, unit="B", unit_scale=True, desc=os.path.basename(filepath)) as bar:
            with open(filepath, "wb") as f:
                for data in response.iter_content(chunk_size=chunk_size):
                    if not data:
                        continue
                    f.write(data)
                    bar.update(len(data))
        return True
    except Exception:
        print()
        if fail is not None:
            print(fail)
        else:
            print(f"Failed to download file from {url}. Please try to download it manually and place it to {filepath}.")
            print("If you still can't download, please consider using proxy.")
        return False


def download_url(url, filepath, desc=None, fail=None):
    # we use requests-based downloader here while keeping the same signature.
    return download_url_requests(url, filepath, desc=desc, fail=fail)


def download_qai_hubmodel(model_id, filepath, desc=None, fail=None, hub_id=HUB_ID_Q):
    if is_file_exists(filepath):
        return True

    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    if desc is not None:
        print(desc)
    else:
        print(f"Downloading {os.path.basename(filepath)}...")

    setup_qai_hub(hub_id)

    try:
        model = qai_hub.get_model(model_id)
        model.download(filename=filepath)
        return True
    except Exception:
        print()
        if fail is not None:
            print(fail)
        else:
            print(f"Failed to download model {os.path.basename(filepath)} from AI Hub. "
                  f"Please try to download it manually and place it to {filepath}.")
            print("If you still can't download, please consider using proxy.")
        return False
    finally:
        reset_qai_hub()




MODEL_NAME = "whisper_base_en"
ENCODER_MODEL_ID = "mqvvjzzeq"
DECODER_MODEL_ID = "mq8ylzzpm"
ENCODER_MODEL_NAME = "whisper_base_en-whisperencoder-snapdragon_x_elite"
DECODER_MODEL_NAME = "whisper_base_en-whisperdecoder-snapdragon_x_elite"

MODEL_HELP_URL = (
    "https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/"
    + MODEL_NAME
    + "#"
    + MODEL_NAME
    + "-qnn-models"
)

WHISPER_VERSION = "base.en"
MEL_FILTER_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/whisper_asr_shared/v1/openai_assets/mel_filters.npz"
JFK_WAV_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/whisper_asr_shared/v1/audio/jfk.wav"
JFK_NPZ_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/whisper_asr_shared/v1/audio/jfk.npz"

execution_ws = os.getcwd()

model_dir = execution_ws + "\\\\" + "models-qnn"
encoder_model_path = model_dir + "\\\\" + ENCODER_MODEL_NAME + ".bin"
decoder_model_path = model_dir + "\\\\" + DECODER_MODEL_NAME + ".bin"
jfk_wav_path = execution_ws + "\\\\" + "jfk.wav"
jfk_npz_path = execution_ws + "\\\\" + "jfk.npz"
mel_filter_path = execution_ws + "\\\\" + "mel_filters.npz"


def model_download():
    ret = True

    if not os.path.exists(mel_filter_path):
        ret = download_url(MEL_FILTER_PATH_URL, mel_filter_path)
    if not os.path.exists(jfk_wav_path):
        ret = download_url(JFK_WAV_PATH_URL, jfk_wav_path)
    if not os.path.exists(jfk_npz_path):
        ret = download_url(JFK_NPZ_PATH_URL, jfk_npz_path)

    desc = f"Downloading {MODEL_NAME} model... "
    fail = (
        f"\nFailed to download {MODEL_NAME} model. Please prepare the model according to the steps in below link:\n"
        f"{MODEL_HELP_URL}"
    )

    ret = download_qai_hubmodel(ENCODER_MODEL_ID, encoder_model_path, desc=desc, fail=fail)
    ret = download_qai_hubmodel(DECODER_MODEL_ID, decoder_model_path, desc=desc, fail=fail)

    if not ret:
        exit()



if __name__ == "__main__":
    model_download()
    print("\n[DONE] All files are ready:")