<br>

<div align="center">
  <h3>Run AI Models Locally on NPU — Deploy Qualcomm AI Hub Models Quickly</h3>
  <p><i> SIMPLE | EASY | FAST </i></p>
</div>
<br>

## Disclaimer
This software is provided "as is," without any express or implied warranties. The authors and contributors shall not be held liable for any damages arising from its use. The code may be incomplete or insufficiently tested. Users are solely responsible for evaluating its suitability and assume all associated risks.<br>
Note: Contributions are welcome. Please ensure thorough testing before deploying in critical systems.

## Introduction
This directory contains all QAI AppBuilder sample code organized by category. Samples cover Python inference scripts for 30+ AI models across audio, computer vision, generative AI, and multimodal domains — all running on the Snapdragon NPU (HTP) via QAI AppBuilder. Also included are Gradio web UI applications, a Genie LLM API service (Python + C++), Android apps, and shared utilities.

---

## Directory Structure

```
samples/
├── run_inference.py          # Interactive launcher for all Python inference samples
├── models/                   # AI model inference samples (organized by domain)
│   ├── audio/                # Audio models (TTS, ASR, classification)
│   ├── computer_vision/      # Computer vision models
│   ├── generative_ai/        # Generative AI models (Stable Diffusion)
│   └── multimodal/           # Multimodal models (OCR, translation, CLIP, VLM)
├── apps/                     # Complete AI applications
│   ├── webui/                # Gradio WebUI applications (image-repair, stable-diffusion, genie-chat)
│   ├── android/              # Android sample apps (genie-chat, super-resolution)
│   ├── genie-flet-ui/        # Flet-based desktop UI
│   └── story-seed/           # Automated story generation app
├── genie/                    # Genie LLM API service
│   ├── python/               # Python OpenAI-compatible service (GenieAPIService.py)
│   └── c++/                  # C++ OpenAI-compatible service (Service/, Android/)
├── shared/                   # Shared utilities
│   ├── python/               # Shared Python modules (install.py, image_processing.py, etc.)
│   └── cpp/                  # Shared C++ dependencies (OpenCV)
└── tools/                    # Utility tools (wget, aria2c)
```

---

## Quick Start — Interactive Launcher

`run_inference.py` is an interactive launcher for all Python inference samples. Run it from the `samples/` directory:

```
cd qai-appbuilder\samples
python run_inference.py                          # interactive numbered menu
python run_inference.py --list                   # list all available models
python run_inference.py --model <name>           # run a specific model directly
python run_inference.py --model <name> --args "<extra args>"
python run_inference.py --help-model <name>      # show a model's --help and exit
```

**Examples:**
```
python run_inference.py --model whisper_base_en
python run_inference.py --model stable_diffusion_v2_1 --args "--prompt 'a cat'"
python run_inference.py --model openai_clip --args "--text 'camping under the stars'"
python run_inference.py --model pipertts_en --args "--text 'Hello world.'"
```

---

## Python Environment Setup

### Step 1: Install Dependencies
Refer to [python.md](../docs/python.md) on how to set up an x64 Python environment.

You can also run the batch file from [QAI AppBuilder Launcher](../tools/launcher/) to set up the environment automatically.

### Step 2: Install Python Dependencies
```
pip install huggingface_hub==0.33.1 Pillow==10.4.0 numpy==1.26.4 torch==2.4.1 torchvision==0.19.1 torchaudio==2.4.1 transformers==4.46.3 sentencepiece diffusers==0.32.2 tqdm==4.67.1 scikit-image==0.25.2 pygame==2.6.1 ftfy==6.3.1 av==15.0.0 resampy==0.4.3 soundfile==0.13.1 easyocr==1.7.2 samplerate==0.2.1 audio2numpy==0.1.2 ultralytics==8.0.193 opencv-python-headless==4.11.0.86 opencv-python==4.10.0.84 openai-whisper==20250625 openai-clip==1.0.1 gruut==2.4.0
```

### Step 3: Set Up QAI AppBuilder and QNN Libraries
```
cd qai-appbuilder\samples\shared\python
python setup.py
```

---

## 1. Python Inference Samples (`models/`)

All inference samples follow the `Init → Inference → Release` pattern and automatically download the required QNN model binary from Qualcomm AI Hub on first run.

See [models/README.md](models/README.md) for the full model catalog.

### 1.1 Audio (`models/audio/`)

| Model | Script | Description |
|-------|--------|-------------|
| [pipertts_en](models/audio/audio_generation/pipertts_en/) | `models\audio\audio_generation\pipertts_en\python\pipertts_en.py` | English TTS: text → WAV (22050 Hz). 4-stage pipeline: G2P → Encoder → SDP → Flow → HiFi-GAN Decoder. |
| [melotts_zh](models/audio/audio_generation/melotts_zh/) | `models\audio\audio_generation\melotts_zh\python\melotts_zh.py` | Chinese TTS: text → WAV. MeloTTS Chinese voice model. |
| [whisper_base_en](models/audio/speech_recognition/whisper_base_en/) | `models\audio\speech_recognition\whisper_base_en\python\whisper_base_en.py` | English ASR: WAV → text. Whisper Base encoder-decoder, 6 layers, float16 KV cache. |
| [whisper_tiny_en](models/audio/speech_recognition/whisper_tiny_en/) | `models\audio\speech_recognition\whisper_tiny_en\python\whisper_tiny_en.py` | English ASR: WAV → text. Whisper Tiny encoder-decoder, 4 layers, faster than Base. |
| [zipformer](models/audio/speech_recognition/zipformer/) | `models\audio\speech_recognition\zipformer\python\zipformer.py` | Chinese ASR: WAV → text. Zipformer streaming speech recognition. |
| [yamnet](models/audio/audio_classification/yamnet/) | `models\audio\audio_classification\yamnet\python\yamnet.py` | Audio event classification: WAV → top-5 of 521 AudioSet classes. |

### 1.2 Computer Vision (`models/computer_vision/`)

| Model | Script | Description |
|-------|--------|-------------|
| [beit](models/computer_vision/image_classification/beit/) | `models\computer_vision\image_classification\beit\python\beit.py` | BEiT Vision Transformer: image → top-5 ImageNet-1K classes. |
| [googlenet](models/computer_vision/image_classification/googlenet/) | `models\computer_vision\image_classification\googlenet\python\googlenet.py` | GoogLeNet: image → top-5 ImageNet-1K classes. |
| [inception_v3](models/computer_vision/image_classification/inception_v3/) | `models\computer_vision\image_classification\inception_v3\python\inception_v3.py` | Inception V3: image → top-5 ImageNet-1K classes. |
| [yolov8_det](models/computer_vision/object_detection/yolov8_det/) | `models\computer_vision\object_detection\yolov8_det\python\yolov8_det.py` | YOLOv8: image → bounding boxes + 80 COCO class labels. |
| [unet_segmentation](models/computer_vision/semantic_segmentation/unet_segmentation/) | `models\computer_vision\semantic_segmentation\unet_segmentation\python\unet_segmentation.py` | U-Net: image → binary segmentation mask overlay. |
| [depth_anything](models/computer_vision/depth_estimation/depth_anything/) | `models\computer_vision\depth_estimation\depth_anything\python\depth_anything.py` | DepthAnything: image → depth heatmap. |
| [openpose](models/computer_vision/pose_estimation/openpose/) | `models\computer_vision\pose_estimation\openpose\python\openpose.py` | OpenPose: image → 18 body keypoints. |
| [mediapipe_hand](models/computer_vision/pose_estimation/mediapipe_hand/) | `models\computer_vision\pose_estimation\mediapipe_hand\python\mediapipe_hand.py` | MediaPipe Hand: image/camera → 21 hand landmarks + gesture recognition. |
| [face_attrib_net](models/computer_vision/face_recognition/face_attrib_net/) | `models\computer_vision\face_recognition\face_attrib_net\python\face_attrib_net.py` | FaceAttribNet: face image → 6 attributes (identity, liveness, glasses, mask, etc.). |
| [facemap_3dmm](models/computer_vision/face_recognition/facemap_3dmm/) | `models\computer_vision\face_recognition\facemap_3dmm\python\facemap_3dmm.py` | FaceMap 3DMM: face image → 264 3D morphable model params → 68 facial landmarks. |
| [real_esrgan_x4plus](models/computer_vision/super_resolution/real_esrgan_x4plus/) | `models\computer_vision\super_resolution\real_esrgan_x4plus\python\real_esrgan_x4plus.py` | Real-ESRGAN x4plus: image → 4× upscaled image. Supports .bin/.dlc/.onnx. |
| [real_esrgan_general_x4v3](models/computer_vision/super_resolution/real_esrgan_general_x4v3/) | `models\computer_vision\super_resolution\real_esrgan_general_x4v3\python\real_esrgan_general_x4v3.py` | Real-ESRGAN General x4v3: image → 4× upscaled image. |
| [quicksrnetmedium](models/computer_vision/super_resolution/quicksrnet_medium/) | `models\computer_vision\super_resolution\quicksrnet_medium\python\quicksrnetmedium.py` | QuickSRNet Medium: image → 4× upscaled image (lightweight). |
| [lama_dilated](models/computer_vision/image_editing/lama_dilated/) | `models\computer_vision\image_editing\lama_dilated\python\lama_dilated.py` | LaMa Dilated: image + mask → inpainted image (object removal). |
| [aotgan](models/computer_vision/image_editing/aotgan/) | `models\computer_vision\image_editing\aotgan\python\aotgan.py` | AOT-GAN: image + mask → inpainted image (GAN-based). |
| [resnet_3d](models/computer_vision/video_classification/resnet_3d/) | `models\computer_vision\video_classification\resnet_3d\python\resnet_3d.py` | 3D ResNet: MP4 video → top-5 Kinetics-400 action classes. |
| [track_anything](models/computer_vision/video_object_tracking/track_anything/) | `models\computer_vision\video_object_tracking\track_anything\python\track_anything.py` | Track-Anything: click a target in any video and track it frame-by-frame with XMem segmentation. |

### 1.3 Generative AI (`models/generative_ai/`)

| Model | Script | Description |
|-------|--------|-------------|
| [stable_diffusion_v1_5](models/generative_ai/image_generation/stable_diffusion_v1_5/) | `models\generative_ai\image_generation\stable_diffusion_v1_5\python\stable_diffusion_v1_5.py` | SD v1.5: text → 512×512 image. TextEncoder + UNet + VAE. |
| [stable_diffusion_v2_1](models/generative_ai/image_generation/stable_diffusion_v2_1/) | `models\generative_ai\image_generation\stable_diffusion_v2_1\python\stable_diffusion_v2_1.py` | SD v2.1: text → 512×512 image. Uses QNNShareMemory. |
| [stable_diffusion_v3_5](models/generative_ai/image_generation/stable_diffusion_v3_5/) | `models\generative_ai\image_generation\stable_diffusion_v3_5\python\stable_diffusion_v3_5.py` | SD v3.5 Medium: text → 1024×1024 image. MM-DiT Transformer, FlowMatch scheduler. |

### 1.4 Multimodal (`models/multimodal/`)

| Model | Script | Description |
|-------|--------|-------------|
| [easy_ocr](models/multimodal/image_to_text/easy_ocr/) | `models\multimodal\image_to_text\easy_ocr\python\easy_ocr.py` | EasyOCR: image → detected text (English + Chinese). 2-stage: CRAFT + CRNN. |
| [openai_clip](models/multimodal/image_classification/openai_clip/) | `models\multimodal\image_classification\openai_clip\python\openai_clip.py` | OpenAI CLIP (ViT-B/16): images + text query → similarity scores. |
| [nomic_embed_text](models/multimodal/text_embedding/nomic_embed_text/) | `models\multimodal\text_embedding\nomic_embed_text\python\nomic_embed_text.py` | NomicEmbedText: text → 768-dim embedding vector (BERT-based). For RAG/semantic search. |
| [opus_mt_zh_en](models/multimodal/text_embedding/opus_mt_zh_en/) | `models\multimodal\text_embedding\opus_mt_zh_en\python\opus_mt_zh_en.py` | OpusMT: Chinese text → English translation (MarianMT). |
| [opus_mt_en_zh](models/multimodal/text_embedding/opus_mt_en_zh/) | `models\multimodal\text_embedding\opus_mt_en_zh\python\opus_mt_en_zh.py` | OpusMT: English text → Chinese translation (MarianMT). |
| [qwen_vl](models/multimodal/vision_language_model/qwen_vl/) *(Linux only)* | `models\multimodal\vision_language_model\qwen_vl\python\qwen_vl.py` | Qwen2-VL / Qwen3-VL: image/video + question → answer. Gradio web UI. |

---

## 2. Shared Utilities (`shared/`)

Shared modules imported by all Python inference samples:

| File | Description |
|------|-------------|
| `shared/python/setup.py` | Automated environment setup: installs QAI AppBuilder + QNN runtime libraries |
| `shared/python/install.py` | `download_qai_hubmodel()`, `download_url()`, `detect_device_model()` |
| `shared/python/image_processing.py` | `pil_resize_pad`, `pil_undo_resize_pad`, `preprocess_PIL_image` |
| `shared/python/_image_classification.py` | Shared image classification utilities |
| `shared/python/_image_editing.py` | Shared image editing/inpainting utilities |
| `shared/python/_super_resolution.py` | Shared super-resolution utilities |
| `shared/python/_face_recognition.py` | Shared face recognition utilities |
| `shared/python/_pose_estimation.py` | Shared pose estimation utilities |
| `shared/python/_speech_recognition.py` | Shared Whisper ASR utilities |
| `shared/python/_text_generation.py` | Shared text generation utilities |
| `shared/python/_stable_diffusion.py` | Shared Stable Diffusion utilities |

See [shared/python/python_samples_guide.md](shared/python/python_samples_guide.md) for detailed API documentation.

---

## 3. Applications (`apps/`)

### 3.1 WebUI Apps (`apps/webui/`)

Gradio-based web applications running on the Snapdragon NPU.

| App | Command | Port | Description |
|-----|---------|------|-------------|
| [ImageRepairApp](apps/webui/image-repair/) | `python apps\webui\image-repair\ImageRepairApp.py` | 8977 | Image super-resolution using Real-ESRGAN General x4v3. Before/after slider comparison. |
| [StableDiffusionApp](apps/webui/stable-diffusion/) | `python apps\webui\stable-diffusion\StableDiffusionApp.py` | 8978 | Text-to-image using Stable Diffusion v2.1. |
| [GenieWebUI](apps/webui/genie-chat/) | `python apps\webui\genie-chat\GenieWebUI.py` | 50000 | LLM chat app. Connects to GenieAPIService. |

### 3.2 Android Apps (`apps/android/`)

- **GenieChat**: Android LLM chat app using the Genie API
- **SuperResolution**: Android image super-resolution app

### 3.3 Genie Flet UI (`apps/genie-flet-ui/`)

A Flet-based desktop UI application for Windows and Android.

### 3.4 StorySeed (`apps/story-seed/`)

An AI application that automatically generates English stories and posts them to Xiaohongshu (小红书).

---

## 4. Genie LLM API Service (`genie/`)

An OpenAI-compatible LLM API service running large language models on the Snapdragon NPU via the Genie SDK.

**Start service:**
```
cd qai-appbuilder\samples
python genie\python\GenieAPIService.py --modelname "IBM-Granite-v3.1-8B" --loadmodel --profile
```

See [genie/python/README.md](genie/python/README.md) for full setup instructions.

---

## Notes

1. **Model auto-download**: All Python inference samples automatically download the required QNN model binary from Qualcomm AI Hub on first run.

2. **Platform support**: HTP (NPU) runtime is only available on Snapdragon hardware. On x86 Windows, models fall back to CPU + DLC format.

3. **ultralytics version**: For `yolov8_det`, use `ultralytics==8.0.193`.

4. **HuggingFace access**: If HuggingFace is not accessible, set:
   ```
   set HF_ENDPOINT=https://hf-api.gitee.com
   ```

5. **Detailed guide**: See [shared/python/python_samples_guide.md](shared/python/python_samples_guide.md) for a complete developer guide.
