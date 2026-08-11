# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import sys
import platform

# ── WoS (Windows on Snapdragon) guard ─────────────────────────────────────
# This script is designed for Linux (aarch64-oe-linux) runtime.
# On Windows on Snapdragon (WoS), please use the WebUI implementation instead.
if platform.system() == "Windows":
    print("=" * 70)
    print("  Qwen VL is not supported on Windows on Snapdragon (WoS).")
    print()
    print("  On WoS, please refer to the WebUI implementation:")
    print("    samples\\apps\\webui\\")
    print()
    print("  The WebUI provides a full Qwen VL demo with browser-based UI")
    print("  that runs natively on Windows on Snapdragon.")
    print("=" * 70)
    sys.exit(0)

import gradio as gr
import cv2
import os
import numpy as np
from qwen2_vlm_qnn import Qwen2VLQnn
import sys
import time
import json
import argparse
from qwen3_vlm_qnn import Qwen3VLQnn 

qnn_vlm = None

def predict(image1, prompt):
    print(f"DEBUG predict: image1 type: {type(image1)}")
    if isinstance(image1, np.ndarray):
        print(f"DEBUG predict: image1 is numpy array with shape: {image1.shape}")
    elif isinstance(image1, list):
        print(f"DEBUG predict: image1 is list with {len(image1)} elements")
    else:
        print(f"DEBUG predict: image1 value: {image1}")
    
    if image1 is not None:
        cv2.imwrite("capture.jpg", cv2.cvtColor(image1, cv2.COLOR_RGB2BGR))        
        image_path="./capture.jpg"
        print(f"DEBUG predict: Calling Inference with image_path={image_path}")
        result = qnn_vlm.Inference(image_path,prompt)
        return result
    else:       
        return "Please upload an image or use webcam preview to capture video frames for prediction."
        


with gr.Blocks(title="Qwen VL Demo") as demo:
    gr.Markdown("# Qwen VL Demo (Qwen2-VL / Qwen3-VL)")

    # Row 1: Left (video preview), Right (image + prompt)
    with gr.Row():
        with gr.Column(scale=1):
            input_mode = gr.Radio(
                label="Input Source",
                choices=["Webcam", "Video File"],
                value="Webcam"
            )
            input_cam = gr.Image(
                label="Video Preview (Webcam)",
                sources="webcam",
                visible=True
            )
            input_video = gr.Video(
                label="Video File",
                sources=["upload"],
                visible=False
            )
        with gr.Column(scale=1):
            with gr.Row():
                image1 = gr.Image(
                    label="Image",
                    sources=["upload"],
                    interactive=True,
                    height=320
                )
            with gr.Row():
                interval_sec = gr.Number(
                    label="Interval (seconds)",
                    value=5,
                    precision=0
                )
            prompt = gr.Textbox(
                label="Prompt",
                placeholder="Enter your prompt here...",
                lines=3,
                value="Please describe the video content with a brief abstract and shortly content."
            )

    # Row 2: Output textbox
    with gr.Row():
        output_text = gr.Textbox(
            label="Prediction Output",
            lines=6,
            value=""
        )

    # Row 3: Button
    with gr.Row():
        btn = gr.Button("Predict")

    def capture_frame(frame):
        return frame

    input_cam.stream(
        fn=capture_frame,
        inputs=[input_cam],
        outputs=[image1],
        time_limit=None,
        stream_every=5.0,
        concurrency_limit=1,
    )

    # Toggle input components
    def toggle_input(mode):
        return (
            gr.update(visible=(mode == "Webcam")),
            gr.update(visible=(mode == "Video File")),
        )

    input_mode.change(
        toggle_input,
        inputs=[input_mode],
        outputs=[input_cam, input_video]
    )

    def process_video(video, interval):
        """
        Stream frames from the uploaded video to image1 every `interval` seconds.
        Note: This streams from the backend; it won't "follow" the browser playhead precisely,
        but it will update image1 at the requested cadence.
        """
        if video is None:
            return None
        if cv2 is None:
            return None

        path = video if isinstance(video, str) else getattr(video, "name", None)
        if not path:
            return None

        try:
            interval_val = float(interval)
        except Exception:
            interval_val = 0.0
        interval_val = max(0.0, interval_val)

        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return None

        fps = cap.get(cv2.CAP_PROP_FPS)
        if not fps or fps <= 0:
            fps = 25.0  # fallback

        # If interval=0, stream "as fast as possible" but still yield frames in order
        step_sec = interval_val if interval_val > 0 else (1.0 / fps)

        t = 0.0
        try:
            while True:
                cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
                ok, frame = cap.read()
                if not ok or frame is None:
                    break

                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                yield rgb

                t += step_sec

                # Make UI updates feel like "playing" at the chosen interval
                if interval_val > 0:
                    time.sleep(interval_val)
        finally:
            cap.release()

    input_video.change(
        process_video,
        inputs=[input_video, interval_sec],
        outputs=[image1],
        concurrency_limit=1,
    )

    btn.click(predict, inputs=[image1, prompt], outputs=[output_text])

def cleanup():
    pass

def check_model_files(dir:str) -> bool:
    required_files = [
        "veg.serialized.bin",
        "config.json",
        "embedding_weights_151936x1536.raw",
        "tokenizer.json"
    ]
    for file in required_files:
        if not os.path.exists(os.path.join(dir, file)):
            return False
    return True

def load_qwen2_vlm(qwen2_vl_model_dir:str):
    veg_model_path = os.path.join(qwen2_vl_model_dir, "veg.serialized.bin")
    llm_model_config_path = os.path.join(qwen2_vl_model_dir, "config.json")
    look_up_table_path = os.path.join(qwen2_vl_model_dir, "embedding_weights_151936x1536.raw")    
    lib_runtime = "aarch64-oe-linux-gcc11.2" 

    with open(llm_model_config_path, "r", encoding="utf-8") as f:
        config_data = json.load(f)
    #print("Loaded config.json:", config_data)
    config_data["dialog"]["tokenizer"]["path"] = os.path.join(qwen2_vl_model_dir, "tokenizer.json")
    config_data["dialog"]["engine"]["backend"]["extensions"] = os.path.join(qwen2_vl_model_dir, "htp_backend_ext_config.json")
    config_data["dialog"]["engine"]["model"]["binary"]["ctx-bins"] = [os.path.join(qwen2_vl_model_dir, "weight_sharing_model_1_of_1.serialized.bin")]
    # Save the updated config_data back to the original JSON file
    with open(llm_model_config_path, "w", encoding="utf-8") as f:
        json.dump(config_data, f, ensure_ascii=False, indent=2)

    # qnn_runtime_path = os.environ.get("QNN_SDK_ROOT", "")
    # if not qnn_runtime_path:
    #     print("Please set QNN_SDK_ROOT environment variable to QNN SDK path.")
    #     sys.exit(1)
    # qnn_runtime_path=f"{qnn_runtime_path}/lib/{lib_runtime}"
    qnn_runtime_path = "Htp"
    global qnn_vlm
    qnn_vlm=Qwen2VLQnn(veg_model_path, 
                            llm_model_config_path, 
                            look_up_table_path, 
                            qnn_runtime_path)   
    qnn_vlm.Init()
    return qnn_vlm

def load_qwwen3_vlm(qwen3_vl_model_dir:str):
    # This is a placeholder for loading Qwen3-VL model, which would be similar to load_qwen2_vlm but with adjustments for the different model files and config structure.
    veg_model_path = os.path.join(qwen3_vl_model_dir, "qwen3_vl_4b_vit.serialized.bin")
    llm_model_config_path = os.path.join(qwen3_vl_model_dir, "qwen3-vl-e2t-htp.json")
    look_up_table_path = os.path.join(qwen3_vl_model_dir, "embedding_fp32.bin")  
    lib_runtime = "aarch64-oe-linux-gcc11.2" 

    with open(llm_model_config_path, "r", encoding="utf-8") as f:
        config_data = json.load(f)
    #print("Loaded config.json:", config_data)
    config_data["dialog"]["embedding"]["lut-path"] = look_up_table_path
    config_data["dialog"]["tokenizer"]["path"] = os.path.join(qwen3_vl_model_dir, "tokenizer.json")
    config_data["dialog"]["engine"]["backend"]["extensions"] = os.path.join(qwen3_vl_model_dir, "htp_backend_ext_config.json")
    #print(f"Set tokenizer path to {config_data["dialog"]["engine"]["model"]["binary"]["ctx-bins"]}")

    ctx_bins = config_data["dialog"]["engine"]["model"]["binary"]["ctx-bins"]

    config_data["dialog"]["engine"]["model"]["binary"]["ctx-bins"] = [
        os.path.join(qwen3_vl_model_dir, filename) for filename in ctx_bins
    ]    
   
    # Save the updated config_data back to the original JSON file
    with open(llm_model_config_path, "w", encoding="utf-8") as f:
        json.dump(config_data, f, ensure_ascii=False, indent=2)

    # qnn_runtime_path = os.environ.get("QNN_SDK_ROOT", "")
    # if not qnn_runtime_path:
    #     print("Please set QNN_SDK_ROOT environment variable to QNN SDK path.")
    #     sys.exit(1)
    # qnn_runtime_path=f"{qnn_runtime_path}/lib/{lib_runtime}"
    qnn_runtime_path = "Htp"

    global qnn_vlm
    qnn_vlm=Qwen3VLQnn(veg_model_path, 
                            llm_model_config_path, 
                            look_up_table_path, 
                            qnn_runtime_path)   
    qnn_vlm.Init()

if __name__ == "__main__":
    # Create command-line argument parser
    parser = argparse.ArgumentParser(
        description="Qwen VL Demo - Support for Qwen2-VL and Qwen3-VL models",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Use Qwen2-VL model (default)
  python demo_app.py /path/to/qwen2_vl_model
  
  # Use Qwen3-VL model
  python demo_app.py --model qwen3 --path /path/to/qwen3_vl_model
  
  # Specify server port
  python demo_app.py --model qwen2 --path /path/to/qwen2_vl_model --port 8080
        """
    )
    
    parser.add_argument(
        "path",
        nargs="?",
        type=str,
        help="Model directory path"
    )
    
    parser.add_argument(
        "--model",
        type=str,
        choices=["qwen2", "qwen3"],
        default="qwen2",
        help="Select model type (default: qwen2)"
    )
    
    parser.add_argument(
        "--path",
        type=str,
        dest="model_path",
        help="Model directory path (optional, overrides positional argument)"
    )
    
    parser.add_argument(
        "--port",
        type=int,
        default=7861,
        help="Server port (default: 7861)"
    )
    
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Server host address (default: 0.0.0.0)"
    )
    
    args = parser.parse_args()
    
    # Determine model path: prioritize --path, then use positional argument
    model_dir = args.model_path if args.model_path else args.path
    
    if not model_dir:
        parser.print_help()
        print("\nError: Model path is required")
        sys.exit(1)
    
    if not os.path.exists(model_dir):
        print(f"Error: Model directory does not exist: {model_dir}")
        sys.exit(1)
    
    # Load the corresponding model based on the selected model type
    print(f"Loading model: {args.model.upper()}")
    print(f"Model path: {model_dir}")
    
    try:
        if args.model == "qwen2":
            load_qwen2_vlm(model_dir)
            print("Qwen2-VL model loaded successfully")
        elif args.model == "qwen3":
            load_qwwen3_vlm(model_dir)
            print("Qwen3-VL model loaded successfully")
    except Exception as e:
        print(f"Model loading failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    
    # Start Gradio service
    print(f"\nStarting Gradio service...")
    print(f"Server address: http://{args.host}:{args.port}")
    demo.queue()
    demo.launch(server_name=args.host, server_port=args.port, show_error=True)
