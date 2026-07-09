<br>

<div align="center">
  <h3>Run AI models locally on NPU — deploy Qualcomm AI Hub models quickly.</h3>
  <p><i> SIMPLE | EASY | FAST </i></p>
</div>
<br>

## Disclaimer
This software is provided "as is," without any express or implied warranties. The authors and contributors shall not be held liable for any damages arising from its use. The code may be incomplete or insufficiently tested. Users are solely responsible for evaluating its suitability and assume all associated risks.<br>
Note: Contributions are welcome. Please ensure thorough testing before deploying in critical systems.

## Introduction
This directory contains all QAI AppBuilder sample code organized by category. Samples cover Python inference scripts for 28 AI models, Gradio web UI applications, a Genie LLM API service, Android apps, and C++ samples — all running on the Snapdragon NPU (HTP) via QAI AppBuilder.

---

## Directory Structure

```
samples/
├── run_inference.py          # Interactive launcher for all Python inference samples
├── common/                   # Shared utilities for all Python samples
├── audio/                    # Audio inference samples (TTS, ASR, classification)
├── ComputerVision/           # Computer vision inference samples
├── GenerativeAI/             # Generative AI inference samples (Stable Diffusion)
├── Multimodal/               # Multimodal inference samples (OCR, translation, CLIP, VLM)
├── apps/                     # Complete AI applications (WebUI, StorySeed, FletUI)
├── genie/                    # Genie LLM API service (Python + C++)
├── android/                  # Android sample apps (GenieChat, SuperResolution)
├── c++/                      # C++ inference samples
└── tools/                    # Utility tools (wget)
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

> **ARM64 Windows note for `easyocr`:** On ARM64 Windows (Python 3.13+), `easyocr` cannot be installed directly because its dependency `Shapely` has no prebuilt ARM64 wheel. Use this two-step install instead:
> ```
> pip install scikit-image python-bidi pyclipper ninja imageio tifffile lazy-loader
> pip install easyocr --no-deps
> ```

### Step 3: Set Up QAI AppBuilder and QNN Libraries
```
cd qai-appbuilder\samples\common
python setup.py
```

---

## 1. Python Inference Samples

All inference samples follow the `Init → Inference → Release` pattern and automatically download the required QNN model binary from Qualcomm AI Hub on first run.

### 1.1 Audio (`audio/`)

| Model | Script | Description |
|-------|--------|-------------|
| [pipertts_en](audio/Audio_Generation/pipertts_en/) | `audio\Audio_Generation\pipertts_en\pipertts_en.py` | English TTS: text → WAV (22050 Hz). 4-stage pipeline: G2P (gruut) → Encoder → SDP → Flow → HiFi-GAN Decoder. Auto-downloads device-specific model zip. |
| [whisper_base_en](audio/Speech_Recognition/whisper_base_en/) | `audio\Speech_Recognition\whisper_base_en\whisper_base_en.py` | English ASR: WAV → text. Whisper Base encoder-decoder, 6 layers, float16 KV cache. Auto-chunks audio at 30s. |
| [whisper_tiny_en](audio/Speech_Recognition/whisper_tiny_en/) | `audio\Speech_Recognition\whisper_tiny_en\whisper_tiny_en.py` | English ASR: WAV → text. Whisper Tiny encoder-decoder, 4 layers, faster than Base. |
| [yamnet](audio/Audio_Classification/yamnet/) | `audio\Audio_Classification\yamnet\yamnet.py` | Audio event classification: WAV → top-5 of 521 AudioSet classes. VGGish log mel spectrogram preprocessing. |

**Run examples:**
```
python run_inference.py --model pipertts_en --args "--text 'Hello world.'"
python run_inference.py --model whisper_base_en --args "--audio_file input.wav"
python run_inference.py --model yamnet
```

### 1.2 Computer Vision (`ComputerVision/`)

#### Image Classification

| Model | Script | Description |
|-------|--------|-------------|
| [beit](ComputerVision/Image_Classification/beit/) | `ComputerVision\Image_Classification\beit\beit.py` | BEiT Vision Transformer: image → top-5 ImageNet-1K classes. Input: NHWC `[1,224,224,3]`. |
| [googlenet](ComputerVision/Image_Classification/googlenet/) | `ComputerVision\Image_Classification\googlenet\googlenet.py` | GoogLeNet (Inception v1): image → top-5 ImageNet-1K classes. Input: NHWC `[1,224,224,3]`. |
| [inception_v3](ComputerVision/Image_Classification/inception_v3/) | `ComputerVision\Image_Classification\inception_v3\inception_v3.py` | Inception V3: image → top-5 ImageNet-1K classes. Input: NHWC `[1,224,224,3]`. |
| [resnet_3d](ComputerVision/Video_Classification/resnet_3d/) | `ComputerVision\Video_Classification\resnet_3d\resnet_3d.py` | 3D ResNet: MP4 video → top-5 Kinetics-400 action classes. Input: NHWC-T `[1,T,112,112,3]`. |

#### Object Detection & Segmentation

| Model | Script | Description |
|-------|--------|-------------|
| [yolov8_det](ComputerVision/Object_Detection/yolov8_det/) | `ComputerVision\Object_Detection\yolov8_det\yolov8_det.py` | YOLOv8: image → bounding boxes + 80 COCO class labels. Input: NHWC `[1,640,640,3]`. NMS post-processing. |
| [unet_segmentation](ComputerVision/Semantic_Segmentation/unet_segmentation/) | `ComputerVision\Semantic_Segmentation\unet_segmentation\unet_segmentation.py` | U-Net: image → binary segmentation mask overlay. Input: NHWC `[1,640,1280,3]`. |

#### Depth Estimation

| Model | Script | Description |
|-------|--------|-------------|
| [depth_anything](ComputerVision/Depth_Estimation/depth_anything/) | `ComputerVision\Depth_Estimation\depth_anything\depth_anything.py` | DepthAnything (ViT+DPT): image → depth heatmap (plasma colormap). Input: NHWC `[1,518,518,3]`. |

#### Pose Estimation

| Model | Script | Description |
|-------|--------|-------------|
| [openpose](ComputerVision/Pose_Estimation/openpose/) | `ComputerVision\Pose_Estimation\openpose\openpose.py` | OpenPose: image → 18 body keypoints (PAF + heatmap). Input: NHWC `[1,224,224,3]`. |
| [mediapipe_hand](ComputerVision/Pose_Estimation/mediapipe_hand/) | `ComputerVision\Pose_Estimation\mediapipe_hand\mediapipe_hand.py` | MediaPipe Hand: image/camera → 21 hand landmarks + gesture recognition (Play/Pause/Stop/seek). 2-stage: BlazePalm detector + landmark detector. |

#### Face Analysis

| Model | Script | Description |
|-------|--------|-------------|
| [face_attrib_net](ComputerVision/Face_Recognition/face_attrib_net/) | `ComputerVision\Face_Recognition\face_attrib_net\face_attrib_net.py` | FaceAttribNet: face image → 6 attributes (identity, liveness, eye closeness, glasses, mask, sunglasses) → JSON. Input: NHWC `[1,128,128,3]`. |
| [facemap_3dmm](ComputerVision/Face_Recognition/facemap_3dmm/) | `ComputerVision\Face_Recognition\facemap_3dmm\facemap_3dmm.py` | FaceMap 3DMM: face image → 264 3D morphable model params → 68 facial landmarks on image. Supports ONNX float model for better accuracy. |

#### Super Resolution

| Model | Script | Description |
|-------|--------|-------------|
| [real_esrgan_x4plus](ComputerVision/Super_Resolution/real_esrgan_x4plus/) | `ComputerVision\Super_Resolution\real_esrgan_x4plus\real_esrgan_x4plus.py` | Real-ESRGAN x4plus: image → 4× upscaled image. Supports `--bin`, `--dlc`, `--onnx` (FP16, auto-generated), `--w8a8`, `--cpu`, `--gpu`. |
| [real_esrgan_general_x4v3](ComputerVision/Super_Resolution/real_esrgan_general_x4v3/) | `ComputerVision\Super_Resolution\real_esrgan_general_x4v3\real_esrgan_general_x4v3.py` | Real-ESRGAN General x4v3: image → 4× upscaled image. Input: 512×512 (WoS) or 128×128 (Linux). |
| [quicksrnetmedium](ComputerVision/Super_Resolution/quicksrnetmedium/) | `ComputerVision\Super_Resolution\quicksrnetmedium\quicksrnetmedium.py` | QuickSRNet Medium: image → 4× upscaled image (lightweight). Uses `pil_resize_pad` + `pil_undo_resize_pad`. |

#### Image Inpainting

| Model | Script | Description |
|-------|--------|-------------|
| [lama_dilated](ComputerVision/Image_Editing/lama_dilated/) | `ComputerVision\Image_Editing\lama_dilated\lama_dilated.py` | LaMa Dilated: image + mask → inpainted image (object removal). Input: NHWC `[1,512,512,3]` + `[1,512,512,1]`. |
| [aotgan](ComputerVision/Image_Editing/aotgan/) | `ComputerVision\Image_Editing\aotgan\aotgan.py` | AOT-GAN: image + mask → inpainted image (GAN-based). Input: NHWC `[1,512,512,3]` + `[1,512,512,1]`. |

**Run examples:**
```
python run_inference.py --model beit --args "--image input.jpg"
python run_inference.py --model yolov8_det --args "--input_image_path input.jpg --output_image_path output.png"
python run_inference.py --model mediapipe_hand
python run_inference.py --model real_esrgan_x4plus --args "--input_image_path input.jpg"
python run_inference.py --model lama_dilated
```

### 1.3 Generative AI (`GenerativeAI/`)

| Model | Script | Description |
|-------|--------|-------------|
| [stable_diffusion_v1_5](GenerativeAI/Image_Generation/stable_diffusion_v1_5/) | `GenerativeAI\Image_Generation\stable_diffusion_v1_5\stable_diffusion_v1_5.py` | SD v1.5: text → 512×512 image. TextEncoder (768-dim) + UNet + VAE. Tokenizer: `openai/clip-vit-large-patch14`. Quantization: w8a16. |
| [stable_diffusion_v2_1](GenerativeAI/Image_Generation/stable_diffusion_v2_1/) | `GenerativeAI\Image_Generation\stable_diffusion_v2_1\stable_diffusion_v2_1.py` | SD v2.1: text → 512×512 image. TextEncoder (1024-dim) + UNet + VAE. Tokenizer: `stabilityai/stable-diffusion-2-1-base`. Uses `QNNShareMemory`. |
| [stable_diffusion_v3_5](GenerativeAI/Image_Generation/stable_diffusion_v3_5/) | `GenerativeAI\Image_Generation\stable_diffusion_v3_5\stable_diffusion_v3_5.py` | SD v3.5 Medium: text → 1024×1024 image. CLIP-L + CLIP-G + MM-DiT Transformer + VAE. FlowMatch scheduler, 8 steps. Auto-downloads device-specific zip. |

**Run examples:**
```
python run_inference.py --model stable_diffusion_v2_1 --args "--prompt 'spectacular view of northern lights from Alaska'"
python run_inference.py --model stable_diffusion_v3_5 --args "--prompt 'a cat holding a sign' --steps 8"
```

**Manual model download for SD v1.5 / v2.1:**
Download from [AI Hub](https://aihub.qualcomm.com/compute/models/stable_diffusion_v1_5) and save to `GenerativeAI\Image_Generation\stable_diffusion_v1_5\models\`. Select runtime: **Qualcomm® AI Engine Direct**, device: **Snapdragon® X Elite**. Three files needed: `TextEncoderQuantizable`, `UnetQuantizable`, `VaeDecoderQuantizable`.

### 1.4 Multimodal (`Multimodal/`)

| Model | Script | Description |
|-------|--------|-------------|
| [easy_ocr](Multimodal/Image_To_Text/easy_ocr/) | `Multimodal\Image_To_Text\easy_ocr\easy_ocr.py` | EasyOCR: image → detected text (English + Chinese Simplified). 2-stage: CRAFT detector + CRNN recognizer. 6719-char vocabulary. SimSun font rendering. |
| [nomic_embed_text](Multimodal/Text_Generation/nomic_embed_text/) | `Multimodal\Text_Generation\nomic_embed_text\nomic_embed_text.py` | NomicEmbedText: text → 768-dim embedding vector (BERT-based). Saved as `embeddings.npy`. For semantic search / RAG. |
| [openai_clip](Multimodal/Image_Classification/openai_clip/) | `Multimodal\Image_Classification\openai_clip\openai_clip.py` | OpenAI CLIP (ViT-B/16): images + text query → similarity scores → display most relevant image. |
| [opus_mt_zh_en](Multimodal/Text_Generation/opus_mt_zh_en/) | `Multimodal\Text_Generation\opus_mt_zh_en\opus_mt_zh_en.py` | OpusMT: Chinese text → English translation (MarianMT encoder-decoder). Greedy decoding, max 256 tokens. Auto-downloads device-specific zip. |
| [qwen_vl](Multimodal/qwen_vl/) *(Linux only)* | `Multimodal\qwen_vl\qwen_vl.py` | Qwen2-VL / Qwen3-VL: image/video/camera + question → answer. Gradio web UI. Requires manual model download and QNN SDK setup. |

**Run examples:**
```
python run_inference.py --model easy_ocr --args "--Image_Path input.png"
python run_inference.py --model openai_clip --args "--text 'mountain'"
python run_inference.py --model opus_mt_zh_en --args "--input-text '人工智能正在改变世界'"
python run_inference.py --model nomic_embed_text --args "--text 'hello world'"
```

---

## 2. Common Utilities (`common/`)

Shared modules imported by all Python inference samples:

| File | Used By | Description |
|------|---------|-------------|
| `setup.py` | — | Automated environment setup: installs QAI AppBuilder + QNN runtime libraries |
| `install.py` | All samples | `download_qai_hubmodel()`, `download_url()`, `detect_device_model()`, `get_cpu_name()` |
| `image_processing.py` | CV + Multimodal | `preprocess_PIL_image`, `pil_resize_pad`, `pil_undo_resize_pad`, `preprocess_inputs`, `resize_pad`, `app_to_net_image_inputs` |
| `_image_classification.py` | beit, googlenet, inception_v3 | `ImageClassificationQNNContext`, `preprocess_for_classification`, `top_k_classifications`, `load_imagenet_labels` |
| `_image_editing.py` | lama_dilated, aotgan | `ImageEditingQNNContext`, `download_model`, `init_htp_model`, `preprocess_for_inpainting`, `postprocess_inpainted_output`, `run_inference_with_perf_profile`, `save_image`, `IMAGE_SIZE` |
| `_super_resolution.py` | real_esrgan_x4plus, real_esrgan_general_x4v3, quicksrnetmedium | `SuperResolutionQNNContext`, `detect_platform`, `preprocess_image_for_sr`, `postprocess_sr_output`, `build_arg_parser` |
| `_face_recognition.py` | face_attrib_net, facemap_3dmm | `FaceRecognitionQNNContext`, `preprocess_face_image`, `save_face_attributes_json`, `download_asset` |
| `_pose_estimation.py` | mediapipe_hand, openpose | `HAND_LANDMARK_CONNECTIONS`, `MediaPipePyTorchAsRoot`, `batched_nms`, geometry helpers |
| `_speech_recognition.py` | whisper_base_en, whisper_tiny_en | `log_mel_spectrogram`, `apply_timestamp_rules`, `download_whisper_models`, `get_whisper_tokenizer`, Whisper model classes |
| `_text_generation.py` | nomic_embed_text, opus_mt_zh_en | `TextGenerationQNNContext`, `init_htp_model`, `run_inference_with_perf_profile`, `get_tokenizer`, `tokenize_text` |
| `_stable_diffusion.py` | stable_diffusion_v1_5, v2_1 | `set_qnn_config`, `download_sd_component`, `generate_initial_latent`, `decode_vae_output` |
| `_genai_sd.py` | stable_diffusion_v1_5, v2_1 | `TextEncoderQNNContext`, `UnetQNNContext`, `VaeDecoderQNNContext`, `get_tokenizer`, `tokenize_prompt`, `get_scheduler` |

For detailed API documentation, see [common/python_samples_guide.md](common/python_samples_guide.md).

---

## 3. Web UI Applications (`apps/`)

### 3.1 WebUI Apps (`apps/webui/`)

Gradio-based web applications running on the Snapdragon NPU.

**Install additional dependencies:**
```
pip install gradio==5.35.0
```

**Run from `samples/` directory:**

| App | Command | Port | Description |
|-----|---------|------|-------------|
| [ImageRepairApp](apps/webui/ImageRepairApp.py) | `python apps\webui\ImageRepairApp.py` | 8977 | Image super-resolution app using Real-ESRGAN x4plus. Upload image → 4× enhanced output with before/after slider comparison. |
| [StableDiffusionApp](apps/webui/StableDiffusionApp.py) | `python apps\webui\StableDiffusionApp.py` | 8978 | Text-to-image app using Stable Diffusion v2.1. Supports prompt, negative prompt, steps, guidance, seed, batch count. |
| [GenieWebUI](apps/webui/GenieWebUI.py) | `python apps\webui\GenieWebUI.py` | — | LLM chat app. Connects to GenieAPIService via OpenAI-compatible API. Start GenieAPIService first. |

You can also launch them via batch files: `start_ImageRepairApp.bat`, `start_StableDiffusionApp.bat`, `start_GeneWebUI.bat`.

See [apps/webui/README.md](apps/webui/README.md) for details.

### 3.2 StorySeed App (`apps/StorySeed/`)

An AI application that automatically generates English stories and posts them to Xiaohongshu (小红书).

- Randomly selects 4 words from 200 elementary English words
- Uses Genie LLM (Qwen2.0-7B-SSD) to generate a story
- Uses Stable Diffusion to generate an illustration
- Automatically publishes to Xiaohongshu via Chrome automation

**Run:**
```
cd qai-appbuilder\samples
python apps\StorySeed\StorySeed.py
```

See [apps/StorySeed/README.md](apps/StorySeed/README.md) for setup instructions.

### 3.3 FletUI App (`apps/fletui/`)

A Flet-based desktop UI application. Flet allows building web, desktop, and mobile apps in Python. See [apps/fletui/README.md](apps/fletui/README.md).

---

## 4. Genie LLM API Service (`genie/`)

### 4.1 Python Genie Service (`genie/python/`)

An OpenAI-compatible LLM API service running large language models on the Snapdragon NPU via the Genie SDK.

**Features:**
- OpenAI-compatible REST API (port 8910)
- Stream and non-stream response modes
- Tool/function calling support
- Thinking mode (for supported models)
- Model switching at runtime
- Image generation via Stable Diffusion v2.1

**Install dependencies:**
```
pip install uvicorn==0.34.0 pydantic_settings==2.10.1 fastapi==0.115.8 langchain==0.3.19 langchain_core==0.3.45 langchain_community==0.3.18 sse_starlette==2.2.1 pypdf==5.3.0 python-pptx==1.0.2 docx2txt==0.8 openai==1.63.2 json-repair==0.47.4
```

**Start service:**
```
cd qai-appbuilder\samples
python genie\python\GenieAPIService.py --modelname "IBM-Granite-v3.1-8B" --loadmodel --profile
```

**Client samples:**

| Script | Description |
|--------|-------------|
| `GenieAPIClient.py` | Text generation client: `python genie\python\GenieAPIClient.py --prompt "How to fish?" --stream` |
| `GenieAPIClientTools.py` | Tool/function calling client |
| `GenieAPIClientImage.py` | Image generation client (calls SD v2.1 via service) |
| `GenieSample.py` | Direct Genie SDK usage (no API service) |

**Supported LLM models:**

| Model | Source |
|-------|--------|
| IBM Granite v3.1 8B | [Qualcomm AI Hub](https://aihub.qualcomm.com/compute/models/ibm_granite_v3_1_8b_instruct) |
| Phi 3.5 Mini Instruct | [Qualcomm AI Hub](https://aihub.qualcomm.com/compute/models/phi_3_5_mini_instruct) |
| Qwen2-7B-SSD | [aidevhome.com](https://www.aidevhome.com/data/adh2/models/suggested/Qwen2.0-7B-SSD-8380-2.34.zip) |
| Llama 3.2 3B | [aidevhome.com](https://www.aidevhome.com/data/adh2/models/suggested/llama3.2-3b-8380-qnn2.37.zip) |
| Llama 3.1 8B | [aidevhome.com](https://www.aidevhome.com/data/adh2/models/suggested/llama3.1-8b-8380-qnn2.38.zip) |

See [genie/python/README.md](genie/python/README.md) for full setup instructions.

### 4.2 C++ Genie Service (`genie/c++/`)

The recommended production-grade Genie API service implemented in C++. Supports Android and Linux builds.

See [genie/c++/README.md](genie/c++/README.md) for build instructions.

---

## 5. Android Samples (`android/`)

### GenieChat Android App

An Android application demonstrating LLM and VLM integration using the Genie API. Features real-time streaming responses and a modern UI.

- Download [GenieAPIService.apk](https://github.com/qualcomm/qai-appbuilder/releases/download/v2.42.0/GenieAPIService.apk) and run it first
- Build [GenieChat source code](android/GenieChat/) in Android Studio
- Supports Snapdragon mobile devices

### SuperResolution Android App

Demonstrates image super-resolution using Real-ESRGAN x4plus and QuickSRNet Medium on Android (Snapdragon 8 Elite / 8 Elite Gen 5).

- Supports `fp16` and `int8` model variants
- Models pushed to `/sdcard/AIModels/SuperResolution/`
- Two app variants: [SuperResolution](android/SuperResolution/) and [SuperResolution2](android/SuperResolution2/)

See [android/README.md](android/README.md) for detailed build and run instructions.

---

## 6. C++ Samples (`c++/`)

C++ inference samples for real_esrgan_x4plus and beit. See the `c++/` directory for source code and build instructions.

---

## Notes

1. **Model auto-download**: All Python inference samples automatically download the required QNN model binary from Qualcomm AI Hub on first run. An internet connection is required.

2. **NMS on WoS**: `torchvision.ops.nms` requires CUDA and may not work on Windows on Snapdragon. The `_pose_estimation.py` module provides a compatible implementation.

3. **ultralytics version**: For `yolov8_det`, use `ultralytics==8.0.193`:
   ```
   pip install ultralytics==8.0.193
   ```

4. **HuggingFace access**: If HuggingFace is not accessible, set:
   ```
   set HF_ENDPOINT=https://hf-api.gitee.com
   ```

5. **Platform support**: HTP (NPU) runtime is only available on Snapdragon hardware. On x86 Windows, models fall back to CPU + DLC format.

6. **Detailed guide**: See [common/python_samples_guide.md](common/python_samples_guide.md) for a complete developer guide covering all common utilities, inference patterns, and data format notes.
