import gradio as gr
import cv2
import os
from vlm_inference import Qwen2VLQnn
import sys
import time
import json

global qwen2_vl_qnn

def predict(image1, prompt):
    if image1 is not None:
        cv2.imwrite("capture.jpg", cv2.cvtColor(image1, cv2.COLOR_RGB2BGR))        
        images=["./capture.jpg"]         
        result = qwen2_vl_qnn.Inference(images,prompt)
        return result
    else:       
        return "Please upload an image or use webcam preview to capture video frames for prediction."
        


with gr.Blocks(title="Qwen2-VL Demo") as demo:
    gr.Markdown("# Qwen2-VL Demo")

    # Row 1: Left (video preview), Right (image + prompt)
    with gr.Row():
        with gr.Column(scale=1):
            input_mode = gr.Radio(
                label="输入来源 / Input Source",
                choices=["Webcam", "Video File"],
                value="Webcam"
            )
            input_cam = gr.Image(
                label="视频预览 / Video Preview (Webcam)",
                sources="webcam",
                visible=True
            )
            input_video = gr.Video(
                label="视频文件 / Video File",
                sources=["upload"],
                visible=False
            )
        with gr.Column(scale=1):
            with gr.Row():
                image1 = gr.Image(
                    label="图片 / Image",
                    sources=["upload"],
                    interactive=True,
                    height=320
                )
            with gr.Row():
                interval_sec = gr.Number(
                    label="截取间隔（秒） / Interval (s)",
                    value=5,
                    precision=0
                )
            prompt = gr.Textbox(
                label="提示词 / Prompt",
                placeholder="在此输入提示词…",
                lines=3,
                value="Please describe the video content with a brief abstract and shortly content."
            )

    # Row 2: Output textbox
    with gr.Row():
        output_text = gr.Textbox(
            label="预测输出 / Prediction Output",
            lines=6,
            value=""
        )

    # Row 3: Button
    with gr.Row():
        btn = gr.Button("运行预测 / Predict")

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


if __name__ == "__main__":
    if len(sys.argv) > 1:
        qwen2_vl_model_dir = sys.argv[1]
    else:
        print("Usage: python demo_app.py <model_path>")
        sys.exit(1)

    if(not check_model_files(qwen2_vl_model_dir)):
        print(f"Model files are missing in {qwen2_vl_model_dir}. Please check the directory and try again.")
        sys.exit(1)
    
    
    veg_model_path = os.path.join(qwen2_vl_model_dir, "veg.serialized.bin")
    llm_model_config_path = os.path.join(qwen2_vl_model_dir, "config.json")
    look_up_table_path = os.path.join(qwen2_vl_model_dir, "embedding_weights_151936x1536.raw")    
    lib_runtime = "aarch64-oe-linux-gcc11.2" 

    with open(llm_model_config_path, "r", encoding="utf-8") as f:
        config_data = json.load(f)
    print("Loaded config.json:", config_data)
    config_data["dialog"]["tokenizer"]["path"] = os.path.join(qwen2_vl_model_dir, "tokenizer.json")
    config_data["dialog"]["engine"]["backend"]["extensions"] = os.path.join(qwen2_vl_model_dir, "htp_backend_ext_config.json")
    config_data["dialog"]["engine"]["model"]["binary"]["ctx-bins"] = [os.path.join(qwen2_vl_model_dir, "weight_sharing_model_1_of_1.serialized.bin")]
    # Save the updated config_data back to the original JSON file
    with open(llm_model_config_path, "w", encoding="utf-8") as f:
        json.dump(config_data, f, ensure_ascii=False, indent=2)

    qnn_runtime_path = os.environ.get("QNN_SDK_ROOT", "")
    if not qnn_runtime_path:
        print("Please set QNN_SDK_ROOT environment variable to QNN SDK path.")
        sys.exit(1)
    qnn_runtime_path=f"{qnn_runtime_path}/lib/{lib_runtime}"

    
    qwen2_vl_qnn=Qwen2VLQnn(veg_model_path, 
                            llm_model_config_path, 
                            look_up_table_path, 
                            qnn_runtime_path)   
    qwen2_vl_qnn.Init()
    demo.queue()
    demo.launch(server_name="0.0.0.0", server_port=7861, show_error=True)
