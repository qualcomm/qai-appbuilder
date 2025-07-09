import os
import sys
import zipfile
from Install_Helper import *

# Model configuration
models = {
    "IBM-Granite-v3.1-8B": {
        "model_zip_url": "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/ibm_granite_v3_1_8b_instruct/v1/snapdragon_x_elite/models.zip",
        "tokenizer_url": "https://gitee.com/hf-models/granite-3.1-8b-base/raw/main/tokenizer.json"
    }
}

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

        print("Extracting model bin files...")
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

if __name__ == "__main__":
    main()
