import os
import sys
import shutil
import subprocess
import requests
import urllib.request as request
from tqdm import tqdm
import zipfile
from py3_wget import download_file

WGET_URL = "https://eternallybored.org/misc/wget/releases/wget-1.21.4-winarm64.zip"
ARIA2C_URL = "https://github.com/aria2/aria2/releases/download/release-1.36.0/aria2-1.36.0-win-64bit-build1.zip"
TEXT_RUN_SCRIPT_AGAIN = "Then run this Python script again."

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

def install_tools():
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
        if not os.path.exists(aria2c_zip_path):
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

def is_tool_available(tool_name):
    return shutil.which(tool_name) is not None

def ensure_windows_tools():
    required_tools = ["aria2c", "wget"]
    missing = [tool for tool in required_tools if not is_tool_available(tool)]
    if missing:
        print(f"Missing required tools: {', '.join(missing)}\n")
        print("Please install the missing tools from the following links:")
        if "aria2c" in missing:
            print("aria2c: https://github.com/aria2/aria2/releases")
        if "wget" in missing:
            print("wget: https://eternallybored.org/misc/wget/")
        sys.exit(1)

def download_with_aria2c(url, dest_path, proxy=None):
    try:
        cmd = ["aria2c", "--console-log-level=error", "-x", "16", "-s", "16", "-o", os.path.basename(dest_path), "-d", os.path.dirname(dest_path), url]
        if proxy:
            cmd.extend(["--all-proxy", proxy])
        cmd.append("--continue=true")
        subprocess.run(cmd, check=True)
        return True
    except Exception as e:
        print(f"[aria2c] Download failed: {e}")
        return False

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

def download_with_requests(url, dest_path, proxy=None):
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        with requests.get(url, stream=True, timeout=60, proxies=proxies) as response:
            response.raise_for_status()
            total = int(response.headers.get('Content-Length', 0))
            with open(dest_path, 'wb') as out_file, tqdm(
                total=total, unit='B', unit_scale=True, desc=os.path.basename(dest_path)
            ) as pbar:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        out_file.write(chunk)
                        pbar.update(len(chunk))
        return True
    except Exception as e:
        print(f"[requests] Download failed: {e}")
        return False

def download_file_with_progress(url, dest_path, proxy=None):
    print(f"Downloading: {url}")
    print("Using aria2c...")
    if download_with_aria2c(url, dest_path, proxy):
        return True
    print("aria2c failed, trying wget...")
    if download_with_wget(url, dest_path, proxy):
        return True
    print("wget failed, trying requests...")
    return download_with_requests(url, dest_path, proxy)
