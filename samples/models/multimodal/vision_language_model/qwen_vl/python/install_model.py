# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

# ... existing imports ...
import os
import sys
import zipfile
import subprocess
import tarfile

# Model configuration
models = {
    "qwen2": {
        "model_zip_url": "https://www.aidevhome.com/data/adh2/models/suggested/qwen2vl2b.zip"
    },
    "qwen3": {
        "model_tar_url": "https://www.aidevhome.com/data/adh2/models/suggested/qwen3-vl-4b_iq9075.tar.gz"
    }
}

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

def check_model_files(dir: str) -> bool:
    """
    Check if all required model files exist in the directory
    
    Args:
        dir: Directory path to check
    
    Returns:
        True if all required files exist, False otherwise
    """
    required_files = [
        "veg.serialized.bin",
        "config.json",
        "embedding_weights_151936x1536.raw",
        "tokenizer.json"
    ]
    for file in required_files:
        file_path = os.path.join(dir, file)
        if not os.path.exists(file_path):
            print(f"Missing required file: {file}")
            return False
    return True

def download_qwen_models(model_type="qwen2", base_dir=None):
    """
    Download Qwen models (qwen2 or qwen3)
    
    Args:
        model_type: "qwen2" or "qwen3"
        base_dir: Base directory to store models. If None, uses default path.
    
    Returns:
        Model directory path if successful, None otherwise
    """
    if model_type not in models:
        print(f"Unknown model type: {model_type}")
        return None
    
    if base_dir is None:
        base_dir = os.path.join("models", "multimodal","vision_language_model","qwen_vl","models")
    
    os.makedirs(base_dir, exist_ok=True)
    proxy = None

    model_name = model_type
    print(f"\n=== Processing model: {model_name} ===")
    model_dir = os.path.join(base_dir, model_name)
    os.makedirs(model_dir, exist_ok=True)

    model_config = models[model_name]
    
    try:
        if model_type == "qwen2":
            # Download zip file
            zip_path = os.path.join(model_dir, "model.zip")
            print("Downloading model zip...")
            if not download_with_wget(model_config["model_zip_url"], zip_path, proxy):
                print("Model zip download failed.")
                return None

            print("Extracting model bin files...")
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                # Extract all files to a temporary location first
                zip_ref.extractall(model_dir)
                
                # Find the root folder in the zip and move its contents
                extracted_items = os.listdir(model_dir)
                for item in extracted_items:
                    item_path = os.path.join(model_dir, item)
                    # If it's a directory (the original folder like qwen2vl2b)
                    if os.path.isdir(item_path) and item != model_name:
                        # Move all files from the subfolder to model_dir
                        for file in os.listdir(item_path):
                            src = os.path.join(item_path, file)
                            dst = os.path.join(model_dir, file)
                            if os.path.isfile(src):
                                os.rename(src, dst)
                        # Remove the now-empty subfolder
                        os.rmdir(item_path)
            # Clean up zip file
            os.remove(zip_path)
            
        elif model_type == "qwen3":
            # Download tar.gz file
            tar_path = os.path.join(model_dir, "model.tar.gz")
            print("Downloading model tar.gz...")
            if not download_with_wget(model_config["model_tar_url"], tar_path, proxy):
                print("Model tar.gz download failed.")
                return None

            print("Extracting model files...")
            with tarfile.open(tar_path, 'r:gz') as tar_ref:
                tar_ref.extractall(model_dir)
            
            # Find the root folder in the tar and move its contents
            extracted_items = os.listdir(model_dir)
            for item in extracted_items:
                item_path = os.path.join(model_dir, item)
                # If it's a directory (the original folder)
                if os.path.isdir(item_path) and item != model_name:
                    # Move all files from the subfolder to model_dir
                    for file in os.listdir(item_path):
                        src = os.path.join(item_path, file)
                        dst = os.path.join(model_dir, file)
                        if os.path.isfile(src):
                            os.rename(src, dst)
                    # Remove the now-empty subfolder
                    os.rmdir(item_path)

            # Clean up tar file
            os.remove(tar_path)

        print(f"Model download and extraction completed for {model_type}")
        return model_dir
        
    except Exception as e:
        print(f"Failed to process model: {e}")
        import traceback
        traceback.print_exc()
        return None