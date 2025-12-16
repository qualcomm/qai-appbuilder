# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import os
import subprocess
# import winreg
from Install_Helper import *

def download_with_wget(url, dest_path, proxy=None):
    try:
        cmd = ["wget", "-O", dest_path, url]
        if proxy:
            cmd.extend(["-e", f"use_proxy=yes", "-e", f"http_proxy={proxy}", "-e", f"https_proxy={proxy}"])
        subprocess.run(cmd, check=True)
        return True
    except Exception as e:
        print(f"[wget] Download failed: {e}")
        return False

def install_QAIRT():
    extract_root = os.path.join("ai-engine-direct-helper", "samples", "qai_libs")
    zip_file = os.path.join(extract_root, "QAIRT_Runtime_2.38.0_v73.zip")

    if os.path.exists(extract_root):
        print("tools directory already exists")
    else:
        print("Creating tools directory...")
        os.makedirs(extract_root, exist_ok=True)
    
    if os.path.exists(zip_file):
        print("QAIRT_Runtime_2.38.0_v73.zip already exists")
    else:
        print("Downloading QAIRT_Runtime_2.38.0_v73.zip...")
        download_url = "https://github.com/quic/ai-engine-direct-helper/releases/download/v2.38.0/QAIRT_Runtime_2.38.0_v73.zip"
        
        if download_with_wget(download_url, zip_file):
            print("download successfully")
        else:
            print("download failed")
            sys.exit(1)

def is_git_installed():
    try:
        subprocess.run(["git", "--version"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

def install_git():
    try:
        # 更新包列表
        subprocess.run(["sudo", "apt-get", "update"], check=True)
        # 安装 Git
        subprocess.run(["sudo", "apt-get", "install", "-y", "git"], check=True)
        print("Git installed successfully.")
    except subprocess.CalledProcessError as e:
        print(f"Error installing Git: {e}")
        sys.exit(1)


def clone_or_update_repo():
    try:
        repo_dir = "ai-engine-direct-helper"

        if not os.path.exists(repo_dir):
            print("Cloning ai-engine-direct-helper repository...")
            
            subprocess.run(["git", "clone", "https://github.com/quic/ai-engine-direct-helper.git", "--recursive"], check=True)
            print(f"Repository cloned to {repo_dir}")
        else:
            print(f"ai-engine-direct-helper repository already exists at {repo_dir}. Pulling latest changes...")
            
            subprocess.run(["git", "pull"], check=True, cwd=repo_dir)
            print("Latest changes pulled.")

        return True

    except subprocess.CalledProcessError as e:
        print(f"Git update failed: {e}")
        return False
    except Exception as e:
        print(f"Error occurred: {e}")
        return False

def install_qai_appbuilder():
    extract_root = os.path.join("ai-engine-direct-helper", "samples", "qai_libs")
    zip_file = os.path.join(extract_root, "qai_appbuilder-2.38.0-cp312-cp312-linux_aarch64.whl")

    if os.path.exists(extract_root):
        print("tools directory already exists")
    else:
        print("Creating tools directory...")
        os.makedirs(extract_root, exist_ok=True)
    
    if os.path.exists(zip_file):
        print("qai_appbuilder-2.38.0-cp312-cp312-linux_aarch64.whl already exists")
    else:
        print("Downloading qai_appbuilder-2.38.0-cp312-cp312-linux_aarch64.whl...")

        download_url = "https://github.com/quic/ai-engine-direct-helper/releases/download/v2.38.0/qai_appbuilder-2.38.0-cp312-cp312-linux_aarch64.whl"
        
        if download_with_wget(download_url, zip_file):
            print("download successfully")
        else:
            print("download failed")
            sys.exit(1)


def main():
    # install_tools()

    print("Checking Git...")
    if is_git_installed():
        print("Git is already installed. Skipping installation.")
    else:
        install_git()

    clone_or_update_repo()

    # copy_tools()
    # install_qai_appbuilder()
    # install_GenieAPIService()
    # install_QAIRT()

if __name__ == "__main__":
    main()
