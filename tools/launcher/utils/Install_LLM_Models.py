import os
import sys
import json
import zipfile
import shutil
import subprocess
import requests
from tqdm import tqdm

# Model configuration
models = {
    "IBM-Granite-v3.1-8B": {
        "model_zip_url": "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/ibm_granite_v3_1_8b_instruct/v1/snapdragon_x_elite/models.zip",
        "tokenizer_url": "https://gitee.com/hf-models/granite-3.1-8b-base/raw/main/tokenizer.json"
    }
}

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
        cmd = ["aria2c", "-x", "16", "-s", "16", "-o", os.path.basename(dest_path), "-d", os.path.dirname(dest_path), url]
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

def update_config_json(config_path, model_dir):
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        if isinstance(config.get('model_files'), list):
            config['model_files'] = [f for f in os.listdir(model_dir) if f.endswith(".serialized.bin")]
        if 'tokenizer' in config:
            config['tokenizer'] = "tokenizer.json"
        if 'htp_backend_ext_config' in config:
            config['htp_backend_ext_config'] = "htp_backend_ext_config.json"
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)
        print("Updated config.json successfully.")
    except Exception as e:
        print(f"Failed to update config.json: {e}")
        sys.exit(1)

def main():
    ensure_windows_tools()
    base_dir = os.path.join("ai-engine-direct-helper", "samples", "genie", "python", "models")
    os.makedirs(base_dir, exist_ok=True)
    proxy = None

    for model_name, urls in models.items():
        print(f"\n=== Processing model: {model_name} ===")
        model_dir = os.path.join(base_dir, model_name)
        os.makedirs(model_dir, exist_ok=True)

        zip_path = os.path.join(model_dir, "model.zip")
        print("Downloading model zip...")
        if not download_file_with_progress(urls["model_zip_url"], zip_path, proxy):
            print("Model zip download failed.")
            sys.exit(1)

        print("Extracting .serialized.bin files...")
        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                for file in zip_ref.namelist():
                    if file.endswith(".serialized.bin"):
                        zip_ref.extract(file, model_dir)
        except Exception as e:
            print(f"Failed to extract zip file: {e}")
            sys.exit(1)

        tokenizer_path = os.path.join(model_dir, "tokenizer.json")
        print("Downloading tokenizer.json...")
        if not download_file_with_progress(urls["tokenizer_url"], tokenizer_path, proxy):
            print("Tokenizer download failed.")
            sys.exit(1)

        # config_path = os.path.join(model_dir, "config.json")
        # if os.path.exists(config_path):
            #print("Updating config.json...")
            # update_config_json(config_path, model_dir)

    print("\nAll models processed successfully.")

if __name__ == "__main__":
    main()
