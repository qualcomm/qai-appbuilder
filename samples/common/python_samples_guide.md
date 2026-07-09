# QAI AppBuilder Python Samples Guide

## Introduction

This guide describes how to run the Python sample inference scripts in this repository using QAI AppBuilder on Windows on Snapdragon (WoS) and other supported platforms. It covers the interactive launcher (`run_inference.py`), the shared utility modules in `common/`, and the inference patterns used by each sample category.

All sample scripts are located under `samples/` and follow a consistent `Init → Inference → Release` pattern backed by `QNNContext` from `qai_appbuilder`.

---

## 1. Quick Start — Interactive Launcher (`run_inference.py`)

`run_inference.py` is an interactive launcher that lists all available models and runs any of them with a single command. It must be run from the `samples/` directory.

```
cd qai-appbuilder\samples
```

### Usage

```
python run_inference.py                          # interactive numbered menu
python run_inference.py --list                   # list all available models
python run_inference.py --model <name>           # run a specific model directly
python run_inference.py --model <name> --args "<extra args>"
python run_inference.py --help-model <name>      # show a model's --help and exit
```

### Examples

```
python run_inference.py --model whisper_base_en
python run_inference.py --model stable_diffusion_v2_1 --args "--prompt 'a cat'"
python run_inference.py --model openai_clip --args "--text 'camping under the stars'"
python run_inference.py --model opus_mt_zh_en --args "--input-text '今天天气很好'"
python run_inference.py --model pipertts_en --args "--text 'Hello world.'"
```

### Platform Filtering

`run_inference.py` automatically filters models by the current OS. `qwen_vl` is only shown on Linux (it requires the aarch64-oe-linux QNN runtime). All other models are shown on all platforms.

### All Available Models

| # | Category | Model | Script |
|---|----------|-------|--------|
| 1 | Audio | pipertts_en | `audio\Audio_Generation\pipertts_en\pipertts_en.py` |
| 2 | Audio | whisper_base_en | `audio\Speech_Recognition\whisper_base_en\whisper_base_en.py` |
| 3 | Audio | whisper_tiny_en | `audio\Speech_Recognition\whisper_tiny_en\whisper_tiny_en.py` |
| 4 | Audio | yamnet | `audio\Audio_Classification\yamnet\yamnet.py` |
| 5 | ComputerVision | aotgan | `ComputerVision\Image_Editing\aotgan\aotgan.py` |
| 6 | ComputerVision | beit | `ComputerVision\Image_Classification\beit\beit.py` |
| 7 | ComputerVision | depth_anything | `ComputerVision\Depth_Estimation\depth_anything\depth_anything.py` |
| 8 | ComputerVision | face_attrib_net | `ComputerVision\Face_Recognition\face_attrib_net\face_attrib_net.py` |
| 9 | ComputerVision | facemap_3dmm | `ComputerVision\Face_Recognition\facemap_3dmm\facemap_3dmm.py` |
| 10 | ComputerVision | googlenet | `ComputerVision\Image_Classification\googlenet\googlenet.py` |
| 11 | ComputerVision | inception_v3 | `ComputerVision\Image_Classification\inception_v3\inception_v3.py` |
| 12 | ComputerVision | lama_dilated | `ComputerVision\Image_Editing\lama_dilated\lama_dilated.py` |
| 13 | ComputerVision | mediapipe_hand | `ComputerVision\Pose_Estimation\mediapipe_hand\mediapipe_hand.py` |
| 14 | ComputerVision | openpose | `ComputerVision\Pose_Estimation\openpose\openpose.py` |
| 15 | ComputerVision | quicksrnetmedium | `ComputerVision\Super_Resolution\quicksrnetmedium\quicksrnetmedium.py` |
| 16 | ComputerVision | real_esrgan_general_x4v3 | `ComputerVision\Super_Resolution\real_esrgan_general_x4v3\real_esrgan_general_x4v3.py` |
| 17 | ComputerVision | real_esrgan_x4plus | `ComputerVision\Super_Resolution\real_esrgan_x4plus\real_esrgan_x4plus.py` |
| 18 | ComputerVision | resnet_3d | `ComputerVision\Video_Classification\resnet_3d\resnet_3d.py` |
| 19 | ComputerVision | unet_segmentation | `ComputerVision\Semantic_Segmentation\unet_segmentation\unet_segmentation.py` |
| 20 | ComputerVision | yolov8_det | `ComputerVision\Object_Detection\yolov8_det\yolov8_det.py` |
| 21 | GenerativeAI | stable_diffusion_v1_5 | `GenerativeAI\Image_Generation\stable_diffusion_v1_5\stable_diffusion_v1_5.py` |
| 22 | GenerativeAI | stable_diffusion_v2_1 | `GenerativeAI\Image_Generation\stable_diffusion_v2_1\stable_diffusion_v2_1.py` |
| 23 | GenerativeAI | stable_diffusion_v3_5 | `GenerativeAI\Image_Generation\stable_diffusion_v3_5\stable_diffusion_v3_5.py` |
| 24 | Multimodal | easy_ocr | `Multimodal\Image_To_Text\easy_ocr\easy_ocr.py` |
| 25 | Multimodal | nomic_embed_text | `Multimodal\Text_Generation\nomic_embed_text\nomic_embed_text.py` |
| 26 | Multimodal | openai_clip | `Multimodal\Image_Classification\openai_clip\openai_clip.py` |
| 27 | Multimodal | opus_mt_zh_en | `Multimodal\Text_Generation\opus_mt_zh_en\opus_mt_zh_en.py` |
| 28 | Multimodal | qwen_vl *(Linux only)* | `Multimodal\qwen_vl\qwen_vl.py` |

---

## 2. Common Utilities (`common/`)

The `common/` directory contains shared modules imported by the sample scripts. Each module provides a focused set of utilities for a specific model category.

### 2.1 `setup.py` — Environment Setup

Automates installation of QAI AppBuilder and QNN runtime libraries.

```
cd qai-appbuilder\samples\common
python setup.py
python setup.py --qnn-sdk-version 2.31.0 --dsp-arch 73
```

Internally calls `install.install_qai_appbuilder()` and `install.install_qai_runtime()` (or `install.install_qai_sdk()` as fallback).

### 2.2 `install.py` — Model Download & Device Detection

Provides helpers used by every sample script:

```python
import install

# Download a QNN model binary from Qualcomm AI Hub
ret = install.download_qai_hubmodel(soc_id, model_name, model_path,
                                    desc="Downloading...", fail="Failed!")

# Download any file from a URL (with resume support)
ret = install.download_url(url, dest_path, desc="Downloading...")

# Detect device model: returns "snapdragon_x_elite" or "snapdragon_x2_elite"
device_model = install.detect_device_model()

# Detect CPU name
cpu_name = install.get_cpu_name()
```

### 2.3 `image_processing.py` — Image Pre/Post-Processing

Core image utilities used across ComputerVision and Multimodal samples:

```python
from image_processing import (
    preprocess_PIL_image,        # PIL Image → float32 NCHW tensor [0,1]
    torch_tensor_to_PIL_image,   # float32 CHW tensor [0,1] → PIL Image
    resize_pad,                  # resize+pad torch tensor to (H,W), returns (image, scale, padding)
    pil_resize_pad,              # resize+pad PIL Image to (H,W), returns (image, scale, padding)
    pil_undo_resize_pad,         # undo resize+pad to restore original size
    preprocess_inputs,           # inpainting: image+mask → {"image": masked_NCHW, "mask": NCHW}
    app_to_net_image_inputs,     # PIL Image → (NHWC numpy list, NCHW torch tensor)
)
```

**Key functions:**

- `preprocess_PIL_image(image)` → `torch.Tensor` shape `[1, C, H, W]`, values `[0, 1]`
- `pil_resize_pad(image, (H, W))` → `(resized_image, scale, (pad_left, pad_top))` — preserves aspect ratio
- `pil_undo_resize_pad(output, orig_size, scale, padding)` → restores original dimensions
- `preprocess_inputs(image, mask)` → `{"image": masked_NCHW, "mask": NCHW}` for inpainting models

### 2.4 `_image_classification.py` — Image Classification Utilities

Used by: `beit`, `googlenet`, `inception_v3`

```python
from _image_classification import (
    ImageClassificationQNNContext,  # base QNNContext: Inference([input]) → output[0]
    init_htp_model,                 # QNNConfig.Config(HTP) + model_class(name, path)
    download_model,                 # download .bin via QAI Hub
    download_imagenet_labels,       # download labels JSON/TXT if absent
    load_imagenet_labels,           # load JSON or TXT label file → list[str]
    preprocess_for_classification,  # image_path → NHWC float32 [1,H,W,3], values [0,1]
    top_k_classifications,          # output_data + labels → formatted top-k string
)
```

**Preprocessing pipeline** (`preprocess_for_classification`):
```
PIL.Image.open → Resize(224) → CenterCrop(224) → PILToTensor → /255 → NHWC transpose
```

### 2.5 `_image_editing.py` — Image Inpainting/Restoration Utilities

Used by: `lama_dilated`, `aotgan`

```python
from _image_editing import (
    ImageEditingQNNContext,          # base QNNContext: Inference(image_nhwc, mask_nhwc) → flat output
    init_htp_model,                  # QNNConfig.Config(HTP) + model_class(name, path)
    download_model,                  # download .bin via QAI Hub
    preprocess_for_inpainting,       # (image_path, mask_path) → (image_NHWC, mask_NHWC, orig_image)
    postprocess_inpainted_output,    # flat output → PIL Image (reshape 512×512×3)
    run_inference_with_perf_profile, # BURST profile wrapper: model.Inference(image, mask)
    save_image,                      # save PIL Image to disk + optional show
    IMAGE_SIZE,                      # = 512 (shared constant)
    preprocess_PIL_image,            # PIL Image → float32 NCHW tensor [0,1]
    torch_tensor_to_PIL_image,       # float32 CHW tensor [0,1] → PIL Image
)
```

**Inpainting preprocessing** (`preprocess_for_inpainting`):
```
image_path + mask_path
    → PIL.Image.open
    → preprocess_inputs(PIL, PIL)   # from image_processing.py
    → {"image": NCHW, "mask": NCHW}
    → np.transpose(…, (0,2,3,1))   # NCHW → NHWC for QNN
    → returns (image_nhwc, mask_nhwc, orig_image)
```

**Inpainting postprocessing** (`postprocess_inpainted_output`):
```
flat float32 output
    → torch.from_numpy
    → reshape(512, 512, 3)          # HWC
    → unsqueeze(0)                  # NHWC [1,512,512,3]
    → torch_tensor_to_PIL_image     # clip [0,1] → uint8 → PIL Image
```

### 2.6 `_super_resolution.py` — Super Resolution Utilities

Used by: `real_esrgan_x4plus`, `real_esrgan_general_x4v3`, `quicksrnetmedium`

```python
from _super_resolution import (
    SuperResolutionQNNContext,   # base QNNContext: Inference([input]) → output[0]
    detect_platform,             # → 'wos'|'x86_win'|'arm64_linux'|'x86_linux'|'unknown'
    download_dlc,                # download+extract DLC zip
    download_bin,                # download .bin via QAI Hub
    guess_layout_from_shape,     # 4D shape → 'NCHW' or 'NHWC'
    get_image_size_from_model,   # query model input shape → int image size
    preprocess_image_for_sr,     # image_path → (orig_image, tensor, scale, padding)
    postprocess_sr_output,       # output_tensor → PIL Image (4× upscaled, unpadded)
    run_with_perf_profile,       # BURST profile wrapper
    print_model_debug_info,      # print graph name, shapes, dtypes, names
    build_arg_parser,            # build argparse with --cpu/--gpu/--bin/--w8a8/--chipset/...
)
```

**SR preprocessing** (`preprocess_image_for_sr`):
```
PIL.Image.open → pil_resize_pad(image_size) → /255 → [NCHW or NHWC] float32
```

**SR postprocessing** (`postprocess_sr_output`):
```
flat/4D output → reshape → layout conversion → clip[0,1] → ×255 → pil_undo_resize_pad(4×)
```

**Platform detection** (`detect_platform`):
```
Windows ARM64 → 'wos'
Windows x86   → 'x86_win'
Linux ARM64   → 'arm64_linux'
Linux x86     → 'x86_linux'
```

### 2.7 `_face_recognition.py` — Face Model Utilities

Used by: `face_attrib_net`, `facemap_3dmm`

```python
from _face_recognition import (
    FaceRecognitionQNNContext,       # base QNNContext: Inference([input]) → all outputs
    detect_platform,                 # same as _super_resolution.detect_platform
    init_htp_model,                  # QNNConfig.Config(HTP) + model_class(name, path)
    download_model,                  # download .bin via QAI Hub
    download_asset,                  # download asset file (npy, txt, etc.)
    preprocess_face_image,           # image_path → NHWC float32 [1,128,128,3]
    run_inference_with_perf_profile, # BURST profile wrapper
    save_face_attributes_json,       # outputs + names → JSON file
    save_image,                      # save PIL Image + show preview
)
```

**Face preprocessing** (`preprocess_face_image`):
```
PIL.Image.open → resize(128,128) → /255 (if normalize=True) → add batch dim → NHWC [1,128,128,3]
```

**Output saving** (`save_face_attributes_json`):
```python
# Saves all model outputs as a JSON dict keyed by output_names
save_face_attributes_json(raw_output, ["id_feature","liveness_feature","eye_closeness",...], "output.json")
```

### 2.8 `_pose_estimation.py` — Pose Estimation Utilities

Used by: `mediapipe_hand`, `openpose`

Provides MediaPipe Hand constants and geometry helpers:

```python
from _pose_estimation import (
    HAND_LANDMARK_CONNECTIONS,       # list of (i,j) pairs for 21 hand keypoints
    DETECT_DXY, DETECT_DSCALE,       # palm detector box offset/scale constants
    WRIST_CENTER_KEYPOINT_INDEX,     # = 0
    MIDDLE_FINDER_KEYPOINT_INDEX,    # = 2
    ROTATION_VECTOR_OFFSET_RADS,     # = π/2
    MediaPipePyTorchAsRoot,          # context manager: adds blazepalm repo to sys.path
    batched_nms,                     # NMS over batches (uses torchvision.ops.nms)
    decode_preds_from_anchors,       # decode BlazePalm anchor predictions
    box_xywh_to_xyxy,                # convert box format
    box_xyxy_to_xywh,
    compute_box_corners_with_rotation,
    compute_box_affine_crop_resize_matrix,
    apply_affine_to_coordinates,
    apply_batched_affines_to_frame,
    denormalize_coordinates,
    compute_vector_rotation,
    draw_box_from_xyxy,
    draw_box_from_corners,
    draw_points,
    draw_connections,
    display_or_save_image,
    numpy_image_to_torch,
)
```

### 2.9 `_speech_recognition.py` — Whisper ASR Utilities

Used by: `whisper_base_en`, `whisper_tiny_en`

```python
from _speech_recognition import (
    # Constants
    SAMPLE_RATE,           # 16000
    CHUNK_LENGTH,          # 30 (seconds)
    N_FFT, HOP_LENGTH, N_MELS,  # 400, 160, 80
    TOKEN_SOT, TOKEN_EOT, TOKEN_BLANK,
    TOKEN_NO_TIMESTAMP, TOKEN_TIMESTAMP_BEGIN,
    TOKEN_NO_SPEECH, NO_SPEECH_THR,
    NON_SPEECH_TOKENS, SAMPLE_BEGIN,
    # Functions
    log_mel_spectrogram,         # mel_filter + audio → [1, 80, 3000] float32
    apply_timestamp_rules,       # logits + decoded_tokens → (logits, logprobs)
    download_whisper_assets,     # download mel_filters.npz + jfk.wav + jfk.npz
    download_whisper_models,     # download encoder.bin + decoder.bin via QAI Hub
    get_whisper_tokenizer,       # → whisper tokenizer (multilingual or en-only)
    # Classes
    CollectionModel,             # decorator-based model component registry
    Whisper,                     # base class with mean_decode_len
    WhisperEncoderInf,           # encoder component descriptor
    WhisperDecoderInf,           # decoder component descriptor
)
```

**Audio preprocessing** (`log_mel_spectrogram`):
```
audio [float32, 16kHz] → pad/trim to 30s → STFT (N_FFT=400, hop=160) → mel filterbank (80 bins) → log → [1, 80, 3000]
```

### 2.10 `_text_generation.py` — Text Generation Utilities

Used by: `nomic_embed_text`, `opus_mt_zh_en`

```python
from _text_generation import (
    TextGenerationQNNContext,        # base QNNContext: Inference(*inputs) → output[0] or all outputs
    download_model,                  # download .bin via QAI Hub
    init_htp_model,                  # QNNConfig.Config(HTP, WARN, BASIC) + model_class(name, path)
    get_tokenizer,                   # AutoTokenizer.from_pretrained(name, model_max_length=seq_len)
    tokenize_text,                   # text → (input_ids, attention_mask) as int32 tensors
    run_inference_with_perf_profile, # BURST profile wrapper: model.Inference(*inputs)
    convert_torch_to_numpy,          # torch.Tensor → numpy array (no-op if already numpy)
)
```

**Key functions:**

- `TextGenerationQNNContext.Inference(*input_data)` — accepts variable number of inputs; returns `output[0]` if single output, else full list
- `init_htp_model(model_path, model_class, instance_name)` — configures HTP runtime and instantiates model
- `get_tokenizer(name, seq_len)` — loads `AutoTokenizer` from HuggingFace with `model_max_length=seq_len`
- `tokenize_text(tokenizer, text, seq_len)` — returns `(input_ids, attention_mask)` as `int32` torch tensors, padded to `seq_len`
- `run_inference_with_perf_profile(model, *inputs)` — wraps inference in `PerfProfile.BURST` / `RelPerfProfileGlobal()`

**Usage pattern** (as in `nomic_embed_text.py` and `opus_mt_zh_en.py`):
```python
class MyModel(TextGenerationQNNContext):
    def Inference(self, input_ids, attention_mask):
        return super().Inference(input_ids, attention_mask)

model = init_htp_model(model_path, MyModel, "my_model")
output = run_inference_with_perf_profile(model, input_ids, attention_mask)
```

### 2.11 `_stable_diffusion.py` — Stable Diffusion Utilities (v1.5/v2.1)

Used by: `stable_diffusion_v1_5`, `stable_diffusion_v2_1`

```python
from _stable_diffusion import (
    set_qnn_config,              # QNNConfig.Config(HTP, WARN, BASIC)
    download_sd_component,       # download one SD component .bin via QAI Hub
    generate_initial_latent,     # seed → NHWC float32 [1,64,64,4] random latent
    decode_vae_output,           # VAE output → PIL Image (512×512)
)
```

### 2.12 `_genai_sd.py` — Generative AI Stable Diffusion Utilities

Used by: `stable_diffusion_v1_5`, `stable_diffusion_v2_1`

```python
from _genai_sd import (
    # QNN context classes
    TextEncoderQNNContext,       # Inference([tokens]) → [1,77,768] float32
    UnetQNNContext,              # Inference(latent, timestep, text_emb) → [1,64,64,4]
    VaeDecoderQNNContext,        # Inference(latent) → flat float32
    # Helpers
    detect_platform,             # same as _super_resolution.detect_platform
    init_htp_model,              # QNNConfig.Config(HTP) + model_class(name, path)
    download_model,              # download .bin via QAI Hub
    get_tokenizer,               # CLIPTokenizer.from_pretrained(name)
    tokenize_prompt,             # prompt → int32 [1,77] token IDs
    get_scheduler,               # DPMSolverMultistepScheduler
    run_inference_with_perf_profile,  # BURST profile wrapper
    get_timestep_embedding_input,     # timestep → float32 embedding
    latent_to_image,             # VAE latent → uint8 NHWC image array
)
```

---

## 3. Inference Pattern Used by All Samples

Every sample script follows this three-function pattern:

```python
from qai_appbuilder import QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig

# 1. Define model class
class MyModel(QNNContext):
    def Inference(self, input_data):
        return super().Inference([input_data])[0]

# 2. Init: download model, configure QNN, instantiate
def Init():
    global my_model
    # model_download() — calls install.download_qai_hubmodel(...)
    QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)
    my_model = MyModel("my_model", "models/my_model.bin")

# 3. Inference: preprocess → BURST → run → release → postprocess
def Inference(input_path):
    input_data = preprocess(input_path)          # → NHWC float32 numpy array
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
    output = my_model.Inference(input_data)
    PerfProfile.RelPerfProfileGlobal()
    return postprocess(output)

# 4. Release resources
def Release():
    global my_model
    del my_model
```

**Key points:**
- `QNNConfig.Config(Runtime.HTP, ...)` must be called before instantiating any `QNNContext`.
- `PerfProfile.BURST` maximizes NPU clock speed; always release it after inference.
- Model input is always a **numpy array** (not torch tensor). QNN returns numpy arrays.
- Most models expect **NHWC** format `[N, H, W, C]`. Convert from PyTorch NCHW with `np.transpose(x, (0,2,3,1))`.

---

## 4. Sample Categories and Their Inference Patterns

### 4.1 Audio

#### pipertts_en — Text-to-Speech
4-stage pipeline: G2P (gruut) → Encoder → SDP → Flow → HiFi-GAN Decoder.
Each stage is a separate `QNNContext` binary. Models auto-downloaded from S3 based on detected device.
```
python run_inference.py --model pipertts_en --args "--text 'Hello world.'"
```

#### whisper_base_en / whisper_tiny_en — Speech Recognition
Encoder-decoder ASR. Uses `_speech_recognition.py` for mel spectrogram and tokenizer.
Input: WAV file (any sample rate, auto-resampled to 16 kHz, chunked at 30s).
Uses `DataType.NATIVE` for float16 KV cache.
```
python run_inference.py --model whisper_base_en --args "--audio_file input.wav"
```

#### yamnet — Audio Classification
Single-model inference. Input: WAV → VGGish log mel spectrogram patches `[N,1,96,64]`.
Output: top-5 of 521 AudioSet classes.
```
python run_inference.py --model yamnet
```

### 4.2 ComputerVision

#### Image Classification (beit, googlenet, inception_v3)
All use `_image_classification.py`. Input: image → NHWC `[1,224,224,3]`. Output: top-5 ImageNet classes.
```
python run_inference.py --model beit --args "--image input.jpg"
```

#### Video Classification (resnet_3d)
Input: MP4 video → `[1,T,112,112,3]` NHWC-T. Output: top-5 Kinetics-400 actions.
```
python run_inference.py --model resnet_3d --args "--video input.mp4"
```

#### Object Detection (yolov8_det)
Input: image → NHWC `[1,640,640,3]`. Output: boxes + scores + class indices → NMS → annotated image.
```
python run_inference.py --model yolov8_det --args "--input_image_path input.jpg --output_image_path output.png"
```

#### Segmentation (unet_segmentation)
Input: image → NHWC `[1,640,1280,3]`. Output: binary mask `[1,2,640,1280]` → argmax → overlay.
```
python run_inference.py --model unet_segmentation --args "--image input.jpg"
```

#### Depth Estimation (depth_anything)
Input: image → NHWC `[1,518,518,3]`. Output: depth map → plasma colormap heatmap.
```
python run_inference.py --model depth_anything --args "--image input.jpg"
```

#### Pose Estimation (openpose, mediapipe_hand)
- `openpose`: PAF + heatmap → 18 body keypoints. Uses `_pose_estimation.py`.
- `mediapipe_hand`: 2-stage (hand detector + landmark detector). Supports camera + gesture recognition.
```
python run_inference.py --model openpose --args "--input_image_path input.png --output_image_path output.png"
python run_inference.py --model mediapipe_hand
```

#### Face Analysis (face_attrib_net, facemap_3dmm)
Both use `_face_recognition.py`.
- `face_attrib_net`: input `[1,128,128,3]` → 6 attribute outputs → JSON.
- `facemap_3dmm`: input `[1,3,128,128]` NCHW → 264 3DMM params → 68 landmarks on image.
```
python run_inference.py --model face_attrib_net --args "--image input.bmp"
python run_inference.py --model facemap_3dmm --args "--image input.jpg"
```

#### Super Resolution (real_esrgan_x4plus, real_esrgan_general_x4v3, quicksrnetmedium)
All use `_super_resolution.py`. Input: image → 4× upscaled output.
- `real_esrgan_x4plus`: supports `--bin`, `--dlc`, `--onnx`, `--w8a8`, `--cpu`, `--gpu`.
- `real_esrgan_general_x4v3`: runs directly (no CLI args), uses `input.jpg` → `output.jpg`.
- `quicksrnetmedium`: uses `pil_resize_pad` + `pil_undo_resize_pad`.
```
python run_inference.py --model real_esrgan_x4plus --args "--input_image_path input.jpg"
python run_inference.py --model quicksrnetmedium --args "--image input.png"
```

#### Image Inpainting (lama_dilated, aotgan)
Both use `_image_editing.py`. Input: `input.png` + `mask.png` → inpainted `output.png`.
Both run directly (no CLI args needed).
```
python run_inference.py --model lama_dilated
python run_inference.py --model aotgan
```

### 4.3 GenerativeAI

#### stable_diffusion_v1_5 / stable_diffusion_v2_1
3-component pipeline: TextEncoder + UNet + VAE Decoder. Uses `_genai_sd.py` and `_stable_diffusion.py`.
- v1.5: text embedding `[1,77,768]`, tokenizer: `openai/clip-vit-large-patch14`
- v2.1: text embedding `[1,77,1024]`, tokenizer: `stabilityai/stable-diffusion-2-1-base`
- Scheduler: DPMSolverMultistepScheduler, 20 steps, CFG=7.5
```
python run_inference.py --model stable_diffusion_v2_1 --args "--prompt 'northern lights'"
```

#### stable_diffusion_v3_5
4-component pipeline: CLIP-L + CLIP-G + MM-DiT Transformer + VAE Decoder.
Output: 1024×1024. Scheduler: FlowMatchEulerDiscreteScheduler, 8 steps, CFG=3.5.
Model auto-downloaded as zip (device-specific: 8380 for X Elite, 8480 for X2 Elite).
```
python run_inference.py --model stable_diffusion_v3_5 --args "--prompt 'a cat' --steps 8"
```

### 4.4 Multimodal

#### easy_ocr — OCR (English + Chinese)
2-stage: CRAFT detector + CRNN recognizer. Input: image → annotated image with text.
Character set: 6719 chars (English + Chinese Simplified). Uses SimSun font for rendering.
```
python run_inference.py --model easy_ocr --args "--Image_Path input.png"
```

#### nomic_embed_text — Text Embedding
Input: text → BERT tokenizer `[1,128]` → embedding `[1,768]` → saved as `embeddings.npy`.
Uses `_text_generation.py` (shared with other text models).
```
python run_inference.py --model nomic_embed_text --args "--text 'hello world'"
```

#### openai_clip — Image-Text Similarity
Input: images directory + text query → similarity score per image → display most relevant.
Uses CLIP ViT-B/16 preprocessor. Model input: image `[1,224,224,3]` + text `[1,77]`.
```
python run_inference.py --model openai_clip --args "--text 'mountain'"
```

#### opus_mt_zh_en — Chinese→English Translation
Encoder-decoder (MarianMT). Encoder: `[1,256]` tokens → cross-KV cache.
Decoder: autoregressive, greedy, max 256 steps. Vocab: 65001 tokens.
Model auto-downloaded as zip (device-specific).
```
python run_inference.py --model opus_mt_zh_en --args "--input-text '人工智能正在改变世界'"
```

#### qwen_vl — Vision Language Model *(Linux only)*
Qwen2-VL / Qwen3-VL with Gradio web UI. Supports image, video, and camera input.
Requires manual model download and QNN SDK environment setup.
```
python run_inference.py --model qwen_vl   # Linux only
```

---

## 5. Data Format Notes

### NHWC vs NCHW

QNN models typically expect **NHWC** (batch, height, width, channel) numpy arrays.
PyTorch tensors are in **NCHW** format. Convert between them:

```python
# PyTorch NCHW → QNN NHWC input
input_nhwc = np.transpose(torch_tensor.numpy(), (0, 2, 3, 1))

# QNN NHWC output → PyTorch NCHW for post-processing
output_nchw = np.transpose(qnn_output, (0, 3, 1, 2))
```

### Model Input/Output Types

- All QNN model inputs and outputs are **numpy float32 arrays** (unless `DataType.NATIVE` is used for float16 models like Whisper).
- Use `model.getInputShapes()`, `model.getInputDataType()`, `model.getOutputShapes()`, `model.getOutputDataType()` to inspect model I/O at runtime.

### Checking Input Specs

Use the `See more metrics` option on the [Qualcomm AI Hub](https://aihub.qualcomm.com/compute/models/) model page to verify the expected input shape and data type before writing preprocessing code.

---

## 6. Platform Support

| Platform | HTP (NPU) | GPU | CPU | DLC | BIN |
|----------|-----------|-----|-----|-----|-----|
| WoS (ARM64 Windows) | ✅ | ✅ | ✅ | ✅ | ✅ |
| x86 Windows | ❌ | ❌ | ✅ | ✅ | ❌ |
| ARM64 Linux | ✅ | ✅ | ✅ | ✅ | ✅ |
| x86 Linux | ✅ | ✅ | ✅ | ✅ | ✅ |

`detect_platform()` (in `_super_resolution.py`, `_face_recognition.py`, `_genai_sd.py`) returns one of:
`'wos'` | `'x86_win'` | `'arm64_linux'` | `'x86_linux'` | `'unknown'`


