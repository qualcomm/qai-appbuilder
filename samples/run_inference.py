# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
run_inference.py  –  Interactive launcher for all sample models.

The launcher script itself lives at the top of the ``samples/`` tree
(``samples/run_inference.py``) and dispatches to model scripts under
``samples/models/<domain>/<task>/<model>/python/<script>.py``.

Usage (from the samples/ directory):
    python run_inference.py                         # interactive menu
    python run_inference.py --list                  # list all available models
    python run_inference.py --model <name>          # run a specific model directly
    python run_inference.py --model <name> --args "<extra args>"
    python run_inference.py --help-model <name>     # show a model's --help and exit

Examples:
    python run_inference.py --model whisper_base_en
    python run_inference.py --model stable_diffusion_v2_1 --args "--prompt 'a cat'"
    python run_inference.py --model openai_clip --args "--text 'camping under the stars'"
    python run_inference.py --model opus_mt_zh_en --args "--text '今天天气很好'"
"""

import os
import sys
import platform
import subprocess
import argparse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

def _detect_platform():
    """Return one of: 'wos', 'x86_win', 'arm64_linux', 'x86_linux', 'unknown'."""
    system  = platform.system().lower()
    machine = platform.machine().lower()

    if system == "windows":
        if machine in ("aarch64", "arm64"):
            return "wos"          # Windows on Snapdragon
        else:
            return "x86_win"      # Regular x86_64 Windows
    if system == "linux":
        if machine in ("aarch64", "arm64"):
            return "arm64_linux"
        if machine in ("x86_64", "amd64"):
            return "x86_linux"
    return "unknown"

PLATFORM = _detect_platform()
print(f"[INFO] Detected platform: {PLATFORM}")

# ── All available models ───────────────────────────────────────────────────
# Format: (category, model_name, relative_script_path, platforms)
# platforms: list of supported OS names as returned by platform.system()
#            e.g. ["Windows", "Linux"] or None (= all platforms)
if PLATFORM in ("wos", "x86_win"):
    _ALL_MODELS = [
        # ── audio ──────────────────────────────────────────────────────────────
        ("audio",           "pipertts_en",                  r"models\audio\audio_generation\pipertts_en\python\pipertts_en.py",                        None),
        ("audio",           "melotts_zh",                    r"models\audio\audio_generation\melotts_zh\python\melotts_zh.py",                          None),
        ("audio",           "whisper_base_en",               r"models\audio\speech_recognition\whisper_base_en\python\whisper_base_en.py",              None),
        ("audio",           "whisper_tiny_en",               r"models\audio\speech_recognition\whisper_tiny_en\python\whisper_tiny_en.py",              None),
        ("audio",           "yamnet",                        r"models\audio\audio_classification\yamnet\python\yamnet.py",                              None),
        ("audio",           "zipformer",                     r"models\audio\speech_recognition\zipformer\python\zipformer.py",                          None),

        # ── computervision ─────────────────────────────────────────────────────
        ("computervision",  "aotgan",                        r"models\computer_vision\image_editing\aotgan\python\aotgan.py",                            None),
        ("computervision",  "beit",                          r"models\computer_vision\image_classification\beit\python\beit.py",                         None),
        ("computervision",  "depth_anything",                r"models\computer_vision\depth_estimation\depth_anything\python\depth_anything.py",         None),
        ("computervision",  "face_attrib_net",               r"models\computer_vision\face_recognition\face_attrib_net\python\face_attrib_net.py",       None),
        ("computervision",  "facemap_3dmm",                  r"models\computer_vision\face_recognition\facemap_3dmm\python\facemap_3dmm.py",             None),
        ("computervision",  "googlenet",                     r"models\computer_vision\image_classification\googlenet\python\googlenet.py",               None),
        ("computervision",  "inception_v3",                  r"models\computer_vision\image_classification\inception_v3\python\inception_v3.py",         None),
        ("computervision",  "lama_dilated",                  r"models\computer_vision\image_editing\lama_dilated\python\lama_dilated.py",                None),
        ("computervision",  "mediapipe_hand",                r"models\computer_vision\pose_estimation\mediapipe_hand\python\mediapipe_hand.py",          None),
        ("computervision",  "openpose",                      r"models\computer_vision\pose_estimation\openpose\python\openpose.py",                      None),
        ("computervision",  "quicksrnetmedium",              r"models\computer_vision\super_resolution\quicksrnet_medium\python\quicksrnetmedium.py",     None),
        ("computervision",  "real_esrgan_general_x4v3",      r"models\computer_vision\super_resolution\real_esrgan_general_x4v3\python\real_esrgan_general_x4v3.py", None),
        ("computervision",  "real_esrgan_x4plus",            r"models\computer_vision\super_resolution\real_esrgan_x4plus\python\real_esrgan_x4plus.py", None),
        ("computervision",  "resnet_3d",                     r"models\computer_vision\video_classification\resnet_3d\python\resnet_3d.py",               None),
        ("computervision",  "track_anything",                r"models\computer_vision\video_object_tracking\track_anything\python\track_anything.py",    None),
        ("computervision",  "unet_segmentation",             r"models\computer_vision\semantic_segmentation\unet_segmentation\python\unet_segmentation.py", None),
        ("computervision",  "yolov8_det",                    r"models\computer_vision\object_detection\yolov8_det\python\yolov8_det.py",                 None),

        # ── generativeai ───────────────────────────────────────────────────────
        ("generativeai",    "stable_diffusion_v1_5",         r"models\generative_ai\image_generation\stable_diffusion_v1_5\python\stable_diffusion_v1_5.py", None),
        ("generativeai",    "stable_diffusion_v2_1",         r"models\generative_ai\image_generation\stable_diffusion_v2_1\python\stable_diffusion_v2_1.py", None),
        ("generativeai",    "stable_diffusion_v3_5",         r"models\generative_ai\image_generation\stable_diffusion_v3_5\python\stable_diffusion_v3_5.py", None),

        # ── multimodal ─────────────────────────────────────────────────────────
        ("multimodal",      "easy_ocr",                      r"models\multimodal\image_to_text\easy_ocr\python\easy_ocr.py",                            None),
        ("multimodal",      "tr_ocr",                        r"models\multimodal\image_to_text\tr_ocr\python\tr_ocr.py",                                None),
        ("multimodal",      "nomic_embed_text",              r"models\multimodal\text_embedding\nomic_embed_text\python\nomic_embed_text.py",           None),
        ("multimodal",      "openai_clip",                   r"models\multimodal\image_classification\openai_clip\python\openai_clip.py",               None),
        ("multimodal",      "opus_mt_zh_en",                 r"models\multimodal\text_embedding\opus_mt_zh_en\python\opus_mt_zh_en.py",                None),
        ("multimodal",      "opus_mt_en_zh",                 r"models\multimodal\text_embedding\opus_mt_en_zh\python\opus_mt_en_zh.py",                None),
        # qwen_vl requires Linux (aarch64-oe-linux) runtime; not supported on WoS
        ("multimodal",      "qwen_vl",                       r"models\multimodal\vision_language_model\qwen_vl\python\qwen_vl.py",                                           ["Linux"]),
    ]
else:
    _ALL_MODELS = [
        # ── audio ──────────────────────────────────────────────────────────────
        ("audio",           "pipertts_en",                  r"models/audio/audio_generation/pipertts_en/python/pipertts_en.py",                        None),
        ("audio",           "melotts_zh",                    r"models/audio/audio_generation/melotts_zh/python/melotts_zh.py",                          None),
        ("audio",           "whisper_base_en",               r"models/audio/speech_recognition/whisper_base_en/python/whisper_base_en.py",              None),
        ("audio",           "whisper_tiny_en",               r"models/audio/speech_recognition/whisper_tiny_en/python/whisper_tiny_en.py",              None),
        ("audio",           "yamnet",                        r"models/audio/audio_classification/yamnet/python/yamnet.py",                              None),
        ("audio",           "zipformer",                     r"models/audio/speech_recognition/zipformer/python/zipformer.py",                          None),

        # ── computervision ─────────────────────────────────────────────────────
        ("computervision",  "aotgan",                        r"models/computer_vision/image_editing/aotgan/python/aotgan.py",                            None),
        ("computervision",  "beit",                          r"models/computer_vision/image_classification/beit/python/beit.py",                         None),
        ("computervision",  "depth_anything",                r"models/computer_vision/depth_estimation/depth_anything/python/depth_anything.py",         None),
        ("computervision",  "face_attrib_net",               r"models/computer_vision/face_recognition/face_attrib_net/python/face_attrib_net.py",       None),
        ("computervision",  "facemap_3dmm",                  r"models/computer_vision/face_recognition/facemap_3dmm/python/facemap_3dmm.py",             None),
        ("computervision",  "googlenet",                     r"models/computer_vision/image_classification/googlenet/python/googlenet.py",               None),
        ("computervision",  "inception_v3",                  r"models/computer_vision/image_classification/inception_v3/python/inception_v3.py",         None),
        ("computervision",  "lama_dilated",                  r"models/computer_vision/image_editing/lama_dilated/python/lama_dilated.py",                None),
        ("computervision",  "mediapipe_hand",                r"models/computer_vision/pose_estimation/mediapipe_hand/python/mediapipe_hand.py",          None),
        ("computervision",  "openpose",                      r"models/computer_vision/pose_estimation/openpose/python/openpose.py",                      None),
        ("computervision",  "quicksrnetmedium",              r"models/computer_vision/super_resolution/quicksrnet_medium/python/quicksrnetmedium.py",     None),
        ("computervision",  "real_esrgan_general_x4v3",      r"models/computer_vision/super_resolution/real_esrgan_general_x4v3/python/real_esrgan_general_x4v3.py", None),
        ("computervision",  "real_esrgan_x4plus",            r"models/computer_vision/super_resolution/real_esrgan_x4plus/python/real_esrgan_x4plus.py", None),
        ("computervision",  "resnet_3d",                     r"models/computer_vision/video_classification/resnet_3d/python/resnet_3d.py",               None),
        ("computervision",  "unet_segmentation",             r"models/computer_vision/semantic_segmentation/unet_segmentation/python/unet_segmentation.py", None),
        ("computervision",  "yolov8_det",                    r"models/computer_vision/object_detection/yolov8_det/python/yolov8_det.py",                 None),

        # ── generativeai ───────────────────────────────────────────────────────
        ("generativeai",    "stable_diffusion_v1_5",         r"models/generative_ai/image_generation/stable_diffusion_v1_5/python/stable_diffusion_v1_5.py", None),
        ("generativeai",    "stable_diffusion_v2_1",         r"models/generative_ai/image_generation/stable_diffusion_v2_1/python/stable_diffusion_v2_1.py", None),
        ("generativeai",    "stable_diffusion_v3_5",         r"models/generative_ai/image_generation/stable_diffusion_v3_5/python/stable_diffusion_v3_5.py", None),

        # ── multimodal ─────────────────────────────────────────────────────────
        ("multimodal",      "easy_ocr",                      r"models/multimodal/image_to_text/easy_ocr/python/easy_ocr.py",                            None),
        ("multimodal",      "nomic_embed_text",              r"models/multimodal/text_embedding/nomic_embed_text/python/nomic_embed_text.py",           None),
        ("multimodal",      "openai_clip",                   r"models/multimodal/image_classification/openai_clip/python/openai_clip.py",               None),
        ("multimodal",      "opus_mt_zh_en",                 r"models/multimodal/translation/opus_mt_zh_en/python/opus_mt_zh_en.py",                None),
        # qwen_vl requires Linux (aarch64-oe-linux) runtime; not supported on WoS
        ("multimodal",      "qwen_vl",                       r"models/multimodal/vision_language_model/qwen_vl/python/qwen_vl.py",                                           ["Linux"]),
    ]

# Filter models by current platform
_current_os = platform.system()
MODELS = [
    (cat, name, path)
    for cat, name, path, platforms in _ALL_MODELS
    if platforms is None or _current_os in platforms
]

# Build lookup dict: model_name -> (category, script_path)
MODEL_MAP = {name: (cat, path) for cat, name, path in MODELS}

SAMPLES_DIR = os.path.dirname(os.path.abspath(__file__))


def list_models():
    """Print all available models grouped by category."""
    current_cat = None
    idx = 1
    index_map = {}
    for cat, name, path in MODELS:
        script = os.path.join(SAMPLES_DIR, path)
        exists = os.path.exists(script)
        if cat != current_cat:
            print(f"\n  {'-'*50}")
            print(f"  {cat}")
            print(f"  {'-'*50}")
            current_cat = cat
        status = " " if exists else "!"  # '!' = script not found
        print(f" {idx:>3}. {status} {name:<25} {path}")
        index_map[idx] = name
        idx += 1
    print()
    return index_map


def run_model(model_name: str, extra_args: str = ""):
    """Run the given model's inference script."""
    if model_name not in MODEL_MAP:
        print(f"[ERROR] Unknown model: '{model_name}'")
        print("        Run with --list to see all available models.")
        sys.exit(1)

    cat, rel_path = MODEL_MAP[model_name]
    script = os.path.join(SAMPLES_DIR, rel_path)

    if not os.path.exists(script):
        print(f"[ERROR] Script not found: {script}")
        sys.exit(1)

    cmd = [sys.executable, script]
    if extra_args:
        import shlex
        cmd += shlex.split(extra_args)

    # Don't print the header banner for --help queries
    is_help = extra_args.strip() in ("--help", "-h")
    if not is_help:
        print(f"\n{'='*60}")
        print(f"  Running: {model_name}  [{cat}]")
        print(f"  Script : {rel_path}")
        if extra_args:
            print(f"  Args   : {extra_args}")
        print(f"{'='*60}\n")

    # Run from samples/ directory so relative imports work correctly
    result = subprocess.run(cmd, cwd=SAMPLES_DIR)
    return result.returncode


def interactive_menu():
    """Show an interactive numbered menu and run the selected model."""
    print("\n" + "="*60)
    print("  QAI AppBuilder – Sample Model Launcher")
    print("="*60)
    index_map = list_models()

    total = len(MODELS)
    while True:
        try:
            choice = input(f"  Enter model number (1-{total}) or model name, or 'q' to quit: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n[INFO] Exiting.")
            sys.exit(0)

        if choice.lower() in ("q", "quit", "exit"):
            print("[INFO] Exiting.")
            sys.exit(0)

        # Try numeric selection
        if choice.isdigit():
            idx = int(choice)
            if idx in index_map:
                model_name = index_map[idx]
                break
            else:
                print(f"  [!] Invalid number. Please enter 1-{total}.")
                continue

        # Try name selection
        if choice in MODEL_MAP:
            model_name = choice
            break

        print(f"  [!] '{choice}' not recognized. Try a number or exact model name.")

    # Ask for optional extra args
    try:
        extra = input(f"  Extra arguments for {model_name} (press Enter to skip): ").strip()
    except (KeyboardInterrupt, EOFError):
        extra = ""

    return run_model(model_name, extra)


def main():
    parser = argparse.ArgumentParser(
        description="Interactive launcher for QAI AppBuilder sample models.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="List all available models and exit.",
    )
    parser.add_argument(
        "--model", "-m",
        type=str,
        default=None,
        help="Model name to run directly (skip interactive menu).",
    )
    parser.add_argument(
        "--args", "-a",
        type=str,
        default="",
        help="Extra arguments to pass to the model script (quoted string).",
    )
    parser.add_argument(
        "--help-model", "-H",
        type=str,
        default=None,
        metavar="MODEL_NAME",
        help="Show the argument help for a specific model script and exit.",
    )
    args = parser.parse_args()

    if args.list:
        print("\nAvailable models:")
        list_models()
        sys.exit(0)

    if args.help_model:
        rc = run_model(args.help_model, "--help")
        sys.exit(rc)

    if args.model:
        rc = run_model(args.model, args.args)
        sys.exit(rc)

    # No flags → interactive menu
    rc = interactive_menu()
    sys.exit(rc)


if __name__ == "__main__":
    main()
