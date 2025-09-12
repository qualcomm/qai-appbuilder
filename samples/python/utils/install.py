# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import os
import sys
import platform
import subprocess
import zipfile
import requests
import shutil
from tqdm import tqdm
from pathlib import Path
import qai_hub
import urllib.request as request

qnn_sdk_version =  {
    "2.34": "2.34.0.250424",
}

DEFAULT_SDK_VER     = "2.34"
DEFAULT_DSP_ARCH    = "73"  # For X-Elite device.
DEFAULT_LIB_ARCH = "arm64x-windows-msvc" # "aarch64-windows-msvc" # For X-Elite device.

QNN_SDK_URL = "https://softwarecenter.qualcomm.com/api/download/software/sdks/Qualcomm_AI_Runtime_Community/All/"
QAI_APPBUILDER_WHEEL = "https://github.com/quic/ai-engine-direct-helper/releases/download/vversion.0/qai_appbuilder-version.0-cp312-cp312-win_amd64.whl"
QNN_DOWNLOAD_URL = "https://softwarecenter.qualcomm.com/#/catalog/item/Qualcomm_AI_Runtime_SDK"
TEXT_RUN_SCRIPT_AGAIN = "Then run this Python script again."

QNN_SDK_ROOT="C:\\Qualcomm\\AIStack\\QAIRT\\"
HUB_ID_T="aac24f12d047e7f558d8effe4b2fdad0f5c2c341"
HUB_ID_Q="a916bc04400e033f60fdd73c615e5780e2ba206a"
QAI_HUB_CONFIG = os.path.join(Path.home(), ".qai_hub", "client.ini")
QAI_HUB_CONFIG_BACKUP = os.path.join(Path.home(), ".qai_hub", "client.ini.bk")

WGET_URL = "https://eternallybored.org/misc/wget/releases/wget-1.21.4-winarm64.zip"
ARIA2C_URL = "https://github.com/aria2/aria2/releases/download/release-1.36.0/aria2-1.36.0-win-64bit-build1.zip"

def setup_qai_hub(hub_id):
    if os.path.isfile(QAI_HUB_CONFIG):
        shutil.copy(QAI_HUB_CONFIG, QAI_HUB_CONFIG_BACKUP)
    run_command(f"qai-hub.exe configure --api_token {hub_id} > NUL", False)


def reset_qai_hub():
    if os.path.isfile(QAI_HUB_CONFIG_BACKUP):
        shutil.copy(QAI_HUB_CONFIG_BACKUP, QAI_HUB_CONFIG)

def is_file_exists(filepath):
    if os.path.exists(filepath):
        # print(f"{os.path.basename(filepath)} already exist.")
        return True
    return False

def download_qai_hubmodel(model_id, filepath, desc=None, fail=None, hub_id=HUB_ID_Q):
    ret = True

    if is_file_exists(filepath):
        return ret

    path = os.path.dirname(filepath)
    os.makedirs(path, exist_ok=True)

    if desc is not None:
        print(desc)
    else:
        print(f"Downloading {os.path.basename(filepath)}...")

    setup_qai_hub(hub_id)
    try:
        model = qai_hub.get_model(model_id)
        model.download(filename=filepath)
    except Exception as e:
        # print(str(e))
        print()
        ret = False
        if fail is not None:
            print(fail)
        else:
            print(f"Failed to download model {os.path.basename(filepath)} from AI Hub. Please try to download it manually and place it to {filepath}.")
        print("If you still can't download, please consider using proxy.")
    reset_qai_hub()

    return ret


def verify_package(url, filepath, filesize, desc=None, fail=None):
     # verify if package is ready.
     #  1. package exists.
     #  2. package size is correct.

    is_continue = False

    if os.path.exists(filepath):
        local_size = os.path.getsize(filepath)
        actual_size = 0

        if filesize is not None:
            actual_size = filesize
        else:
            response = request.urlopen(url)
            actual_size = int(response.headers["Content-Length"])

        if actual_size == local_size:   # file is ready for using.
            # print(f"{filepath} is ready for using.")
            return True
        else:
            is_continue = True

    print()

    if is_continue:
        print(f"The file '{filepath}' is not ready. Please wait for downloading to complete.")

    if desc is not None:
        print(desc)
    else:
        print(f"Downloading {os.path.basename(filepath)}...")

    return False


# import wget

# class tqdmWget(tqdm):
#    last_block = 0
#    def update_progress(self, block_num=1, block_size=1, total_size=None):
#        if total_size is not None:
#            self.total = total_size
#        self.update((block_num - self.last_block) * block_size)
#        self.last_block = block_num

from py3_wget import download_file

def download_url_pywget(url, filepath, filesize=None, desc=None, fail=None):
    ret = True

    # Create an unverified SSLContext - a context with disables all certificate verification.
    import ssl
    ssl._create_default_https_context = ssl._create_unverified_context

    if verify_package(url, filepath, filesize):
        return ret

    path = os.path.dirname(filepath)
    os.makedirs(path, exist_ok=True)

    try:
        # wget.download(url, filepath, wget.bar_adaptive)
        #with tqdmWget(unit='B', unit_scale=True, unit_divisor=1024, desc=os.path.basename(filepath)) as t:
        #    def download_callback(blocks, block_size, total_size, bar_function):
        #        t.update_progress(blocks, block_size, total_size)
        #    wget.callback_progress = download_callback
        #    wget.download(url, filepath, wget.bar_adaptive)
        download_file(url, output_path=filepath, overwrite=True)

    except Exception as e:
        # print(str(e))
        print()
        ret = False
        if fail is not None:
            print(fail)
        else:
            print(f"Failed to download file from {url}. Please try to download it manually and place it to {filepath}.")
        print("If you still can't download, please consider using proxy.")

    return ret


def download_url_requests(url, filepath, filesize=None, desc=None, fail=None, chunk_size=8192):
    ret = True

    # Disable warning for insecure request since we set 'verify=False'.
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

    if verify_package(url, filepath, filesize):
        return ret

    path = os.path.dirname(filepath)
    os.makedirs(path, exist_ok=True)

    try:
        response = requests.get(url, stream=True, verify=False)
        if response.status_code != 200:
            raise ValueError(f"Unable to download file at {url}")

        total_size = int(response.headers.get('content-length', 0))

        with tqdm(total=total_size, unit='B', unit_scale=True, desc=os.path.basename(filepath)) as bar:
            with open(filepath, 'wb') as file:
                for data in response.iter_content(chunk_size=chunk_size):
                    file.write(data)
                    bar.update(len(data))

    except Exception as e:
        #print(str(e))
        print()
        ret = False
        if fail is not None:
            print(fail)
        else:
            print(f"Failed to download file from {url}. Please try to download it manually and place it to {filepath}.")
        print("If you still can't download, please consider using proxy.")

    return ret


def download_url_wget(url, filepath, filesize=None, desc=None, fail=None):
    ret = True

    if verify_package(url, filepath, filesize):
        return ret

    if fail is None:
        fail = f"Failed to download file from {url}. Please try to download it manually and place it to {filepath}."
    fail += "\nIf you still can't download, please consider using proxy."

    path = os.path.dirname(filepath)
    name = os.path.basename(filepath)

    if len(path) > 0:
        os.makedirs(path, exist_ok=True)

    try:
        wget_exe_path = "tools\\wget\\wget.exe"
        if not os.path.exists(wget_exe_path):
            wget_exe_path = "..\\python\\tools\\wget\\wget.exe"

        if not os.path.exists(wget_exe_path):
            print(f"wget.exe not found. Please download it manually from '{WGET_URL}' and unzip it to '{wget_exe_path}'")
            return

        command = f'"{wget_exe_path}" --no-check-certificate -q --show-progress --continue -P "{path}" -O "{filepath}" {url}'
        # print(command)
        result = run(command, desc=desc, errdesc=fail, live=True)
        #print(result)

    except Exception as e:
        # print(str(e))
        ret = False

    return ret

def download_url(url, filepath, desc=None, fail=None):
    return download_url_wget(url, filepath, desc, fail)


def run_command(command, live: bool = True):
    try:
        env = os.environ.copy()
        env['PYTHONPATH'] = f"{os.path.abspath('.')}{os.pathsep}{env.get('PYTHONPATH', '')}"

        stdout = run(command, errdesc=f"Error running command", live=live, custom_env=env).strip()
        if stdout:
            print(stdout)
    except Exception as e:
        print(str(e))
        exit()


def run(command, desc=None, errdesc=None, custom_env=None, live: bool = True) -> str:

    run_kwargs = {
        "args": command,
        "shell": True,
        "env": os.environ if custom_env is None else custom_env,
        "errors": 'ignore',
    }

    if not live:
        run_kwargs["stdout"] = run_kwargs["stderr"] = subprocess.PIPE

    result = subprocess.run(**run_kwargs)

    if result.returncode != 0:
        error_bits = [
            f"{errdesc or 'Error running command'}.",
            f"Command: {command}",
            f"Error code: {result.returncode}",
        ]
        if result.stdout:
            error_bits.append(f"stdout: {result.stdout}")

        if result.stderr:
            error_bits.append(f"stderr: {result.stderr}")

        raise RuntimeError("\n".join(error_bits))

    return (result.stdout or "")


def is_installed(package):
    try:
        import importlib.metadata
        import importlib.util
        dist = importlib.metadata.distribution(package)
    except importlib.metadata.PackageNotFoundError as e:
        # print(e)
        try:
            spec = importlib.util.find_spec(package)
        except ModuleNotFoundError:
            return None
        return None

    return dist


def run_pip(command, desc=None, live=False):
    python = sys.executable
    return run(f'"{python}" -m pip {command} ', desc=f"Installing {desc}", errdesc=f"Couldn't install {desc}", live=live)


def run_uninstall_pip(command, desc=None, live=False):
    python = sys.executable
    return run(f'"{python}" -m pip {command} ', desc=f"Uninstalling {desc}", errdesc=f"Couldn't install {desc}", live=live)


def install_download_tools():
    tool_path = "tools"

    os.makedirs(tool_path, exist_ok=True)

    # install wget.exe
    wget_path = tool_path + "\\wget"
    wget_zip_path = tool_path + "\\wget.zip"
    wget_exe_path = wget_path + "\\wget.exe"
    if not os.path.exists(wget_exe_path):
        fail = f"Failed to download tool from '{WGET_URL}'. Please download it manually and unzip it to '{tool_path}'. " + TEXT_RUN_SCRIPT_AGAIN
        desc = f"Downloading '{WGET_URL}' to {wget_path}"
        ret = download_url_pywget(WGET_URL, wget_zip_path, desc=desc, fail=fail)
        if not ret:
            exit()
        print(f"Install 'wget.exe' to {wget_exe_path}")
        with zipfile.ZipFile(wget_zip_path, 'r') as zip_ref:
            zip_ref.extractall(wget_path)
            print()

    # install aria2c.exe
    aria2c_path = tool_path + "\\aria2c"
    aria2c_zip_path = tool_path + "\\aria2c.zip"
    aria2c_exe_path = aria2c_path + "\\aria2c.exe"
    if not os.path.exists(aria2c_exe_path):
        fail = f"Failed to download tool from '{ARIA2C_URL}'. Please download it manually and unzip it to '{tool_path}'. " + TEXT_RUN_SCRIPT_AGAIN
        desc = f"Downloading '{ARIA2C_URL}' to {aria2c_path}"
        ret = download_url_pywget(ARIA2C_URL, aria2c_zip_path, desc=desc, fail=fail)
        if not ret:
            exit()
        print(f"Install 'aria2c.exe' to {aria2c_exe_path}")
        with zipfile.ZipFile(aria2c_zip_path, 'r') as zip_ref:
            zip_ref.extractall(aria2c_path)
            print()

        # Move all files from subdirectories to aria2c_path.
        for root, dirs, files in os.walk(aria2c_path):
            if root == aria2c_path:
                continue
            for file in files:
                src = os.path.join(root, file)
                dst = os.path.join(aria2c_path, file)
                shutil.move(src, dst)


def install_clean(directory, zip_name):
    for filename in os.listdir(directory):
        if filename.startswith(zip_name) and filename.endswith('.tmp'):
            filepath = os.path.join(directory, filename)
            os.remove(filepath)
            print(f"Deleted file: {filepath}")


def setup_qai_env(version, lib_arch = DEFAULT_LIB_ARCH, dsp_arch = DEFAULT_DSP_ARCH, qnn_libs_dir="qai_libs"):
    if version in qnn_sdk_version:
        full_version = qnn_sdk_version[version]
        qnn_root_path = QNN_SDK_ROOT + full_version

        SDK_lib_dir = qnn_root_path + "\\lib\\" + lib_arch
        SDK_hexagon_dir = qnn_root_path + "\\lib\\hexagon-v{}\\unsigned".format(dsp_arch)

        if not os.path.exists(qnn_libs_dir):
            os.makedirs(qnn_libs_dir, exist_ok=True)

        libs = [
            "QnnHtp.dll",
            "QnnSystem.dll",
            "QnnHtpPrepare.dll",
            "QnnHtpNetRunExtensions.dll",
            "QnnHtpV{}Stub.dll".format(dsp_arch),
        ]

        hexagon_libs = [
            "libQnnHtpV{}Skel.so".format(dsp_arch),
            "libqnnhtpv73.cat",
        ]

        for lib in libs:
            if os.path.isfile(os.path.join(qnn_libs_dir, lib)):
                os.remove(os.path.join(qnn_libs_dir, lib))
            shutil.copy(os.path.join(SDK_lib_dir, lib), qnn_libs_dir)

        for lib in hexagon_libs:
            if os.path.isfile(os.path.join(qnn_libs_dir, lib)):
                os.remove(os.path.join(qnn_libs_dir, lib))
            shutil.copy(os.path.join(SDK_hexagon_dir, lib), qnn_libs_dir)


def install_qai_runtime(version, lib_arch = DEFAULT_LIB_ARCH, dsp_arch = DEFAULT_DSP_ARCH, qnn_libs_dir="qai_libs"):
    if version in qnn_sdk_version:
        ret = True

        zip_name = f"QAIRT_Runtime_{version}_v{dsp_arch}.zip"
        url = f"https://github.com/quic/ai-engine-direct-helper/releases/download/v{version}.0/" + zip_name
        qnn_zip_path = os.path.join(qnn_libs_dir, zip_name)

        if not os.path.exists(qnn_libs_dir):
            os.makedirs(qnn_libs_dir, exist_ok=True)

        desc = f"Downloading QAIRT runtime libraries to {qnn_libs_dir}\n"\
               f"If the downloading speed is too slow, please download it manually from below link and copy it to path {qnn_libs_dir}." + TEXT_RUN_SCRIPT_AGAIN + f"\n{url}"
        fail = f"Failed to download file from {url}: \n\t1. Please try again a few times.\n\t2. If still doesn't work, please try to download it manually"\
               f" from {url} and copy it to path {qnn_libs_dir}. " + TEXT_RUN_SCRIPT_AGAIN

        if not os.path.exists(qnn_zip_path):
            print(desc)
            ret = download_url(url, qnn_zip_path, desc=desc, fail=fail)

        if not ret:
            exit()

        print(f"Install QAIRT runtime libraries to '{qnn_libs_dir}'")
        with zipfile.ZipFile(qnn_zip_path, 'r') as zip_ref:
            zip_ref.extractall(qnn_libs_dir)

        src_dir = qnn_libs_dir + "/" + lib_arch
        for filename in os.listdir(src_dir):
            src_file = os.path.join(src_dir, filename)
            if os.path.isfile(src_file):
                shutil.copy2(src_file, qnn_libs_dir)

        print(f"Install QAIRT runtime libraries to '{qnn_libs_dir}' successfully.")

        return qnn_zip_path
    else:
        keys_list = list(qnn_sdk_version.keys())
        print("Supported versions are:")
        print(keys_list)
        return None

def install_qai_sdk(version):
    if version in qnn_sdk_version:
        full_version = qnn_sdk_version[version]
        zip_name = "v" + full_version + ".zip"

        url = QNN_SDK_URL + full_version + "/" + zip_name
        qnn_zip_path = QNN_SDK_ROOT + zip_name
        qnn_root_path = QNN_SDK_ROOT + full_version

        if os.path.exists(qnn_root_path):
            print(f"QNN SDK {version} already installed at {qnn_root_path}")
            return

        if not os.path.exists(QNN_SDK_ROOT):
            os.makedirs(QNN_SDK_ROOT, exist_ok=True)

        # install_clean(QNN_SDK_ROOT, zip_name)
        desc = f"Downloading QNN SDK to {qnn_zip_path}\n"\
               f"If the downloading speed is too slow, please download it manually from {QNN_DOWNLOAD_URL} and install it. " + TEXT_RUN_SCRIPT_AGAIN
        fail = f"Failed to download file from {url}: \n\t1. Please try again a few times.\n\t2. If still doesn't work, please try to download it manually"\
               f" from {QNN_DOWNLOAD_URL} and install it. " + TEXT_RUN_SCRIPT_AGAIN

        print("If the download speed is too slow, you can download them from one of below URLs and install it manually before you run this 'setup.py'.")
        print("https://softwarecenter.qualcomm.com/#/catalog/item/Qualcomm_AI_Runtime_SDK")
        print("https://qpm.qualcomm.com/#/main/tools/details/Qualcomm_AI_Runtime_SDK")

        ret = download_url(url, qnn_zip_path, desc=desc, fail=fail)
        # install_clean(QNN_SDK_ROOT, zip_name)
        if not ret:
            exit()

        print(f"Install QNN SDK to '{QNN_SDK_ROOT}'")
        with zipfile.ZipFile(qnn_zip_path, 'r') as zip_ref:
            zip_ref.extractall(QNN_SDK_ROOT)

        shutil.move(
            os.path.join(QNN_SDK_ROOT, "qairt", full_version),
            QNN_SDK_ROOT,
        )
        shutil.rmtree(os.path.join(QNN_SDK_ROOT, "qairt"))
        # os.remove(qnn_zip_path)  # remove downloaded package.

        # run_command(f"setx QNN_SDK_ROOT {qnn_root_path}")
        print(f"Installed QNN SDK {version} to '{qnn_root_path}' successfully.")

        return qnn_zip_path
    else:
        keys_list = list(qnn_sdk_version.keys())
        print("Supported versions are:")
        print(keys_list)
        return None


def install_qai_appbuilder(version, lib_arch):
    if version in qnn_sdk_version:
        qai_appbuilder_wheel = QAI_APPBUILDER_WHEEL.replace("version", version)
        if lib_arch == "aarch64-windows-msvc":
            qai_appbuilder_wheel = qai_appbuilder_wheel.replace("win_amd64", "win_arm64")

        dist = is_installed("qai_appbuilder")
        version_install = version + ".0"

        if dist and (dist.version != version_install):
            run_uninstall_pip(f"uninstall qai_appbuilder -y", "QAI AppBuilder " + dist.version)

        if (not dist) or (dist.version != version_install) :
            run_pip(f"install {qai_appbuilder_wheel}", "QAI AppBuilder " + version_install)
