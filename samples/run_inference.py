# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
run_inference.py  –  Interactive launcher for all sample models.

Usage (from the samples/ directory):
    python common\run_inference.py                  # interactive menu
    python common\run_inference.py --list           # list all available models
    python common\run_inference.py --model <name>   # run a specific model directly
    python common\run_inference.py --model <name> --args "<extra args>"

Examples:
    python common\run_inference.py --model whisper_base_en
    python common\run_inference.py --model stable_diffusion_v2_1 --args "--prompt 'a cat'"
    python common\run_inference.py --model openai_clip --args "--text 'camping under the stars'"
    python common\run_inference.py --model opus_mt_zh_en --args "--input-text '今天天气很好'"
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
        ("audio",           "pipertts_en",                  r"audio\audio_generation\pipertts_en\pipertts_en.py",                        None),
        ("audio",           "whisper_base_en",               r"audio\speech_recognition\whisper_base_en\whisper_base_en.py",              None),
        ("audio",           "whisper_tiny_en",               r"audio\speech_recognition\whisper_tiny_en\whisper_tiny_en.py",              None),
        ("audio",           "yamnet",                        r"audio\audio_classification\yamnet\yamnet.py",                              None),

        # ── computervision ─────────────────────────────────────────────────────
        ("computervision",  "aotgan",                        r"computervision\image_editing\aotgan\aotgan.py",                            None),
        ("computervision",  "beit",                          r"computervision\image_classification\beit\beit.py",                         None),
        ("computervision",  "depth_anything",                r"computervision\depth_estimation\depth_anything\depth_anything.py",         None),
        ("computervision",  "face_attrib_net",               r"computervision\face_recognition\face_attrib_net\face_attrib_net.py",       None),
        ("computervision",  "facemap_3dmm",                  r"computervision\face_recognition\facemap_3dmm\facemap_3dmm.py",             None),
        ("computervision",  "googlenet",                     r"computervision\image_classification\googlenet\googlenet.py",               None),
        ("computervision",  "inception_v3",                  r"computervision\image_classification\inception_v3\inception_v3.py",         None),
        ("computervision",  "lama_dilated",                  r"computervision\image_editing\lama_dilated\lama_dilated.py",                None),
        ("computervision",  "mediapipe_hand",                r"computervision\pose_estimation\mediapipe_hand\mediapipe_hand.py",          None),
        ("computervision",  "openpose",                      r"computervision\pose_estimation\openpose\openpose.py",                      None),
        ("computervision",  "quicksrnetmedium",              r"computervision\super_resolution\quicksrnetmedium\quicksrnetmedium.py",     None),
        ("computervision",  "real_esrgan_general_x4v3",      r"computervision\super_resolution\real_esrgan_general_x4v3\real_esrgan_general_x4v3.py", None),
        ("computervision",  "real_esrgan_x4plus",            r"computervision\super_resolution\real_esrgan_x4plus\real_esrgan_x4plus.py", None),
        ("computervision",  "resnet_3d",                     r"computervision\video_classification\resnet_3d\resnet_3d.py",               None),
        ("computervision",  "unet_segmentation",             r"computervision\semantic_segmentation\unet_segmentation\unet_segmentation.py", None),
        ("computervision",  "yolov8_det",                    r"computervision\object_detection\yolov8_det\yolov8_det.py",                 None),

        # ── generativeai ───────────────────────────────────────────────────────
        ("generativeai",    "stable_diffusion_v1_5",         r"generativeai\image_generation\stable_diffusion_v1_5\stable_diffusion_v1_5.py", None),
        ("generativeai",    "stable_diffusion_v2_1",         r"generativeai\image_generation\stable_diffusion_v2_1\stable_diffusion_v2_1.py", None),
        ("generativeai",    "stable_diffusion_v3_5",         r"generativeai\image_generation\stable_diffusion_v3_5\stable_diffusion_v3_5.py", None),

        # ── multimodal ─────────────────────────────────────────────────────────
        ("multimodal",      "easy_ocr",                      r"multimodal\image_to_text\easy_ocr\easy_ocr.py",                            None),
        ("multimodal",      "nomic_embed_text",              r"multimodal\text_generation\nomic_embed_text\nomic_embed_text.py",           None),
        ("multimodal",      "openai_clip",                   r"multimodal\image_classification\openai_clip\openai_clip.py",               None),
        ("multimodal",      "opus_mt_zh_en",                 r"multimodal\text_generation\opus_mt_zh_en\opus_mt_zh_en.py",                None),
        # qwen_vl requires Linux (aarch64-oe-linux) runtime; not supported on WoS
        ("multimodal",      "qwen_vl",                       r"multimodal\qwen_vl\qwen_vl.py",                                           ["Linux"]),
    ]
else:
    _ALL_MODELS = [
        # ── audio ──────────────────────────────────────────────────────────────
        ("audio",           "pipertts_en",                  r"audio/Audio_Generation/pipertts_en/pipertts_en.py",                        None),
        ("audio",           "whisper_base_en",               r"audio/Speech_Recognition/whisper_base_en/whisper_base_en.py",              None),
        ("audio",           "whisper_tiny_en",               r"audio/Speech_Recognition/whisper_tiny_en/whisper_tiny_en.py",              None),
        ("audio",           "yamnet",                        r"audio/Audio_Classification/yamnet/yamnet.py",                              None),

        # ── computervision ─────────────────────────────────────────────────────
        ("computervision",  "aotgan",                        r"ComputerVision/Image_Editing/aotgan/aotgan.py",                            None),
        ("computervision",  "beit",                          r"ComputerVision/Image_Classification/beit/beit.py",                         None),
        ("computervision",  "depth_anything",                r"ComputerVision/Depth_Estimation/depth_anything/depth_anything.py",         None),
        ("computervision",  "face_attrib_net",               r"ComputerVision/Face_Recognition/face_attrib_net/face_attrib_net.py",       None),
        ("computervision",  "facemap_3dmm",                  r"ComputerVision/Face_Recognition/facemap_3dmm/facemap_3dmm.py",             None),
        ("computervision",  "googlenet",                     r"ComputerVision/Image_Classification/googlenet/googlenet.py",               None),
        ("computervision",  "inception_v3",                  r"ComputerVision/Image_Classification/inception_v3/inception_v3.py",         None),
        ("computervision",  "lama_dilated",                  r"ComputerVision/Image_Editing/lama_dilated/lama_dilated.py",                None),
        ("computervision",  "mediapipe_hand",                r"ComputerVision/Pose_Estimation/mediapipe_hand/mediapipe_hand.py",          None),
        ("computervision",  "openpose",                      r"ComputerVision/Pose_Estimation/openpose/openpose.py",                      None),
        ("computervision",  "quicksrnetmedium",              r"ComputerVision/Super_Resolution/quicksrnetmedium/quicksrnetmedium.py",     None),
        ("computervision",  "real_esrgan_general_x4v3",      r"ComputerVision/Super_Resolution/real_esrgan_general_x4v3/real_esrgan_general_x4v3.py", None),
        ("computervision",  "real_esrgan_x4plus",            r"ComputerVision/Super_Resolution/real_esrgan_x4plus/real_esrgan_x4plus.py", None),
        ("computervision",  "resnet_3d",                     r"ComputerVision/Video_Classification/resnet_3d/resnet_3d.py",               None),
        ("computervision",  "unet_segmentation",             r"ComputerVision/Semantic_Segmentation/unet_segmentation/unet_segmentation.py", None),
        ("computervision",  "yolov8_det",                    r"ComputerVision/Object_Detection/yolov8_det/yolov8_det.py",                 None),

        # ── generativeai ───────────────────────────────────────────────────────
        ("generativeai",    "stable_diffusion_v1_5",         r"GenerativeAI/Image_Generation/stable_diffusion_v1_5/stable_diffusion_v1_5.py", None),
        ("generativeai",    "stable_diffusion_v2_1",         r"GenerativeAI/Image_Generation/stable_diffusion_v2_1/stable_diffusion_v2_1.py", None),
        ("generativeai",    "stable_diffusion_v3_5",         r"GenerativeAI/Image_Generation/stable_diffusion_v3_5/stable_diffusion_v3_5.py", None),

        # ── multimodal ─────────────────────────────────────────────────────────
        ("multimodal",      "easy_ocr",                      r"Multimodal/Image_To_Text/easy_ocr/easy_ocr.py",                            None),
        ("multimodal",      "nomic_embed_text",              r"Multimodal/Text_Generation/nomic_embed_text/nomic_embed_text.py",           None),
        ("multimodal",      "openai_clip",                   r"Multimodal/Image_Classification/openai_clip/openai_clip.py",               None),
        ("multimodal",      "opus_mt_zh_en",                 r"Multimodal/text_generation/opus_mt_zh_en/opus_mt_zh_en.py",                None),
        # qwen_vl requires Linux (aarch64-oe-linux) runtime; not supported on WoS
        ("multimodal",      "qwen_vl",                       r"Multimodal/qwen_vl/qwen_vl.py",                                           ["Linux"]),
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
        status = "  " if exists else " !"  # '!' = script not found
        print(f"  {idx:>3}. {status} {name:<40} {path}")
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
