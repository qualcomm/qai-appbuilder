# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import os
import subprocess
import winreg
from Install_Helper import *

def copy_tools():
    try:
        script_dir = os.path.dirname(".")
        source_base = os.path.join(script_dir, "tools")
        target_base = os.path.join(script_dir, "ai-engine-direct-helper", "samples", "tools")
        
        dirs_to_check = ["aria2c", "wget"]        
        os.makedirs(target_base, exist_ok=True)
        
        for dir_name in dirs_to_check:
            source_dir = os.path.join(source_base, dir_name)
            target_dir = os.path.join(target_base, dir_name)

            if not os.path.exists(source_dir):
                print(f"folder doesnt exist - {source_dir}")
                continue

            if os.path.exists(target_dir):
                print(f"folder already exists - {target_dir}")
                continue
                
            shutil.copytree(source_dir, target_dir)
            print(f"Copy {source_dir} to {target_dir}")

        return True
        
    except Exception as e:
        print(f"Error: {e}")
        return False

def install_GenieAPIService():
    tools_dir = "tools"
    zip_file = os.path.join(tools_dir, "GenieAPIService_2.34.zip")
    extract_root = os.path.join("ai-engine-direct-helper", "samples")
    extract_dir = os.path.join("ai-engine-direct-helper", "samples", "GenieAPIService")

    if os.path.exists(tools_dir):
        print("tools directory already exists")
    else:
        print("Creating tools directory...")
        os.makedirs(tools_dir, exist_ok=True)
    
    if os.path.exists(zip_file):
        print("GenieAPIService_2.34.zip already exists")
    else:
        print("Downloading GenieAPIService...")
        download_url = "https://github.com/quic/ai-engine-direct-helper/releases/download/v2.34.0/GenieAPIService_2.34.zip"
        
        if download_file_with_progress(download_url, zip_file):
            print("download successfully")
        else:
            print("download failed")
            sys.exit(1)

    if os.path.exists(extract_dir):
        print("GenieAPIService already exists")
    else:
        print("Extracting GenieAPIService...")
        try:
            with zipfile.ZipFile(zip_file, 'r') as zip_ref:
                zip_ref.extractall(extract_root)
            print("Extraction completed.")
        except Exception as e:
            print(f"Error extracting file: {e}")
            sys.exit(1)
    
    print("GenieAPIService downloaded and extracted.")
    android_root = os.path.join(extract_root, "Android")
    if os.path.exists(android_root):
        shutil.rmtree(android_root)

def install_QAIRT():
    extract_root = os.path.join("ai-engine-direct-helper", "samples", "qai_libs")
    zip_file = os.path.join(extract_root, "QAIRT_Runtime_2.34_v73.zip")

    if os.path.exists(extract_root):
        print("tools directory already exists")
    else:
        print("Creating tools directory...")
        os.makedirs(extract_root, exist_ok=True)
    
    if os.path.exists(zip_file):
        print("QAIRT_Runtime_2.34_v73.zip already exists")
    else:
        print("Downloading QAIRT_Runtime_2.34_v73.zip...")
        download_url = "https://github.com/quic/ai-engine-direct-helper/releases/download/v2.34.0/QAIRT_Runtime_2.34_v73.zip"
        
        if download_file_with_progress(download_url, zip_file):
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
    url = "https://github.com/git-for-windows/git/releases/download/v2.50.0.windows.2/PortableGit-2.50.0.2-arm64.7z.exe"
    filename = "tools/Git-2.50.0.2-arm64.7z.exe"
    download_file_with_progress(url, filename)
    print("Installing Git...")
    
    try:
        cmd = ["7z", "x", "tools/Git-2.50.0.2-arm64.7z.exe", "-otools/Git", "-bso0", "-bse0", "-bsp1"]
        result = subprocess.run(cmd, check=True)
        print("Installing Git successfully.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Unzip failed: {e.stderr}")
        return False
    except Exception as e:
        print(f"Error occurred: {e}")
        return False

def clone_or_update_repo():
    try:
        repo_dir = "ai-engine-direct-helper"

        if not os.path.exists(repo_dir):
            print("Cloning ai-engine-direct-helper repository...")
            
            subprocess.run(["git", "clone", "https://github.com/quic/ai-engine-direct-helper.git", "--depth=1"], check=True)
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

def is_vc_redist_installed():
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"
        )
        winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False

def install_vc_redist():
    url = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
    filename = "tools/vc_redist.x64.exe"
    print("Downloading Visual C++ Redistributable...")
    download_file_with_progress(url, filename)
    print("Installing Visual C++ Redistributable...")
    try:
        subprocess.run([filename, "/quiet", "/norestart"], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error occurred while installing Visual C++ Redistributable: {e}")
    os.remove(filename)
    print("Installation complete.")

def main():
    install_tools()

    print("Checking Visual C++ Redistributable...")
    if is_vc_redist_installed():
        print("Visual C++ Redistributable is already installed. Skipping installation.")
    else:
        install_vc_redist()

    print("Checking Git...")
    if is_git_installed():
        print("Git is already installed. Skipping installation.")
    else:
        install_git()

    clone_or_update_repo()

    copy_tools()
    install_GenieAPIService()
    install_QAIRT()

if __name__ == "__main__":
    main()
