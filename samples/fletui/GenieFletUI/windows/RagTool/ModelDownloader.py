import os
import logging
import yaml
import traceback
import io
from huggingface_hub import snapshot_download
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import redirect_stdout, redirect_stderr

class ModelDownloader:
    def __init__(self, max_workers=4):
        self.max_workers = max_workers

    @staticmethod
    def safe_snapshot_download(*args, **kwargs):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return snapshot_download(*args, **kwargs)

    def download_model(self, repo_id, local_dir, required_files=None):
        os.makedirs(local_dir, exist_ok=True)
        logging.debug(f"📦 准备下载模型: {repo_id} -> {local_dir}")

        try:
            missing_files = []
            if required_files:
                for f in required_files:
                    full_path = os.path.join(local_dir, f)
                    if not os.path.exists(full_path):
                        missing_files.append(f)
                        logging.debug(f"❌ 缺失文件: {f}")
                    else:
                        logging.debug(f"✅ 已存在文件: {f}")
                if not missing_files:
                    print(f"✅ 模型 {repo_id} 已存在，跳过")
                    return f"{repo_id} 已跳过"

                print(f"⏬ 正在下载缺失文件: {missing_files}")
                self.safe_snapshot_download(
                    repo_id=repo_id,
                    local_dir=local_dir,
                    allow_patterns=missing_files,
                    local_dir_use_symlinks=False,
                    ignore_patterns=[]
                )
            else:
                print(f"⏬ 下载完整模型: {repo_id}")
                self.safe_snapshot_download(
                    repo_id=repo_id,
                    local_dir=local_dir,
                    local_dir_use_symlinks=False
                )

            logging.debug("✅ 下载成功: %s", repo_id)
            return f"{repo_id} 下载完成 ✅"
        except Exception as e:
            logging.error(f"❌ 下载失败: {str(e)}")
            traceback.print_exc()
            return f"{repo_id} 下载失败 ❌"

    def download_from_config(self, config_path: str, local_dir_base: str):
        print(f"📁 正在读取配置文件: {config_path}")
        logging.debug(f"📁 加载配置文件: {config_path}")
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        model_tasks = config.get("models", [])
        print(f"🚀 正在并行下载 {len(model_tasks)} 个模型，请稍候...\n")
        logging.debug(f"🚀 正在并行下载 {len(model_tasks)} 个模型，请稍候...")

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_model = {
                executor.submit(
                    self.download_model,
                    m["repo_id"],
                    os.path.join(local_dir_base, m["local_dir"]),
                    m.get("required_files")
                ): m["repo_id"] for m in model_tasks
            }

            for future in as_completed(future_to_model):
                result = future.result()
                print(f"📍 {result}")
                logging.debug(f"📍 {result}")