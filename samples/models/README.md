# Models

This directory contains Python inference samples for 28 AI models organized by domain. Each model runs on the Snapdragon NPU (HTP) via QAI AppBuilder and follows the `Init → Inference → Release` pattern.

## Directory Structure

```
models/
├── audio/                    # Audio models
│   ├── audio-classification/ # Sound event classification
│   ├── audio-generation/     # Text-to-speech
│   └── speech-recognition/   # Automatic speech recognition
├── computer-vision/          # Computer vision models
│   ├── depth-estimation/     # Monocular depth estimation
│   ├── face-recognition/     # Face analysis and 3D modeling
│   ├── image-classification/ # Image classification (ImageNet)
│   ├── image-editing/        # Image inpainting
│   ├── object-detection/     # Object detection
│   ├── pose-estimation/      # Human pose and hand landmark detection
│   ├── semantic-segmentation/# Semantic segmentation
│   ├── super-resolution/     # Image super-resolution
│   └── video-classification/ # Video action recognition
├── generative-ai/            # Generative AI models
│   └── image-generation/     # Text-to-image (Stable Diffusion)
└── multimodal/               # Multimodal models
    ├── image-classification/ # Vision-language classification (CLIP)
    ├── image-to-text/        # OCR
    ├── text-embedding/       # Text embedding for RAG
    ├── translation/          # Machine translation
    └── vision-language-model/# Vision-language models (Qwen-VL)
```

Each model directory has the following structure:
```
<model-name>/
├── README.md       # Model documentation
├── python/         # Python inference script(s)
├── assets/         # Input test assets (images, audio, etc.)
└── models/         # Model binary files (.bin, .dlc) — downloaded at runtime
```

## Model Catalog

### Audio

| Domain | Model | Task | AI Hub |
|--------|-------|------|--------|
| Audio Classification | [yamnet](audio/audio_classification/yamnet/) | WAV → top-5 of 521 AudioSet classes | [Link](https://aihub.qualcomm.com/compute/models/yamnet) |
| Audio Generation | [pipertts_en](audio/audio_generation/pipertts_en/) | Text → WAV (22050 Hz, English TTS) | [Link](https://aihub.qualcomm.com/compute/models/pipertts_en) |
| Speech Recognition | [whisper_base_en](audio/speech_recognition/whisper_base_en/) | WAV → English text (Base, 6 layers) | [Link](https://aihub.qualcomm.com/compute/models/whisper_base_en) |
| Speech Recognition | [whisper_tiny_en](audio/speech_recognition/whisper_tiny_en/) | WAV → English text (Tiny, 4 layers, faster) | [Link](https://aihub.qualcomm.com/compute/models/whisper_tiny_en) |

### Computer Vision

| Domain | Model | Task | AI Hub |
|--------|-------|------|--------|
| Image Classification | [beit](computer_vision/image_classification/beit/) | Image → top-5 ImageNet-1K (Vision Transformer) | [Link](https://aihub.qualcomm.com/compute/models/beit) |
| Image Classification | [googlenet](computer_vision/image_classification/googlenet/) | Image → top-5 ImageNet-1K (GoogLeNet) | [Link](https://aihub.qualcomm.com/compute/models/googlenet) |
| Image Classification | [inception_v3](computer_vision/image_classification/inception_v3/) | Image → top-5 ImageNet-1K (Inception V3) | [Link](https://aihub.qualcomm.com/compute/models/inception_v3) |
| Object Detection | [yolov8_det](computer_vision/object_detection/yolov8_det/) | Image → bounding boxes + 80 COCO classes | [Link](https://aihub.qualcomm.com/compute/models/yolov8_det) |
| Semantic Segmentation | [unet_segmentation](computer_vision/semantic_segmentation/unet_segmentation/) | Image → binary segmentation mask | [Link](https://aihub.qualcomm.com/compute/models/unet_segmentation) |
| Depth Estimation | [depth_anything](computer_vision/depth_estimation/depth_anything/) | Image → depth heatmap (ViT+DPT) | [Link](https://aihub.qualcomm.com/compute/models/depth_anything) |
| Pose Estimation | [openpose](computer_vision/pose_estimation/openpose/) | Image → 18 body keypoints | [Link](https://aihub.qualcomm.com/compute/models/openpose) |
| Pose Estimation | [mediapipe_hand](computer_vision/pose_estimation/mediapipe_hand/) | Image/camera → 21 hand landmarks + gesture | [Link](https://aihub.qualcomm.com/compute/models/mediapipe_hand) |
| Face Recognition | [face_attrib_net](computer_vision/face_recognition/face_attrib_net/) | Face → 6 attributes (liveness, glasses, mask, etc.) | [Link](https://aihub.qualcomm.com/compute/models/face_attrib_net) |
| Face Recognition | [facemap_3dmm](computer_vision/face_recognition/facemap_3dmm/) | Face → 264 3DMM params → 68 landmarks | [Link](https://aihub.qualcomm.com/compute/models/facemap_3dmm) |
| Super Resolution | [real_esrgan_x4plus](computer_vision/super_resolution/real_esrgan_x4plus/) | Image → 4× upscaled (.bin/.dlc/.onnx) | [Link](https://aihub.qualcomm.com/compute/models/real_esrgan_x4plus) |
| Super Resolution | [real_esrgan_general_x4v3](computer_vision/super_resolution/real_esrgan_general_x4v3/) | Image → 4× upscaled (general purpose) | [Link](https://aihub.qualcomm.com/compute/models/real_esrgan_general_x4v3) |
| Super Resolution | [quicksrnetmedium](computer_vision/super_resolution/quicksrnet_medium/) | Image → 4× upscaled (lightweight) | [Link](https://aihub.qualcomm.com/compute/models/quicksrnetmedium) |
| Image Editing | [lama_dilated](computer_vision/image_editing/lama_dilated/) | Image + mask → inpainted image (LaMa) | [Link](https://aihub.qualcomm.com/compute/models/lama_dilated) |
| Image Editing | [aotgan](computer_vision/image_editing/aotgan/) | Image + mask → inpainted image (AOT-GAN) | [Link](https://aihub.qualcomm.com/compute/models/aotgan) |
| Video Classification | [resnet_3d](computer_vision/video_classification/resnet_3d/) | MP4 video → top-5 Kinetics-400 actions | [Link](https://aihub.qualcomm.com/compute/models/resnet_3d) |

### Generative AI

| Domain | Model | Task | AI Hub |
|--------|-------|------|--------|
| Image Generation | [stable_diffusion_v1_5](generative_ai/image_generation/stable_diffusion_v1_5/) | Text → 512×512 image (SD v1.5) | [Link](https://aihub.qualcomm.com/compute/models/stable_diffusion_v1_5) |
| Image Generation | [stable_diffusion_v2_1](generative_ai/image_generation/stable_diffusion_v2_1/) | Text → 512×512 image (SD v2.1) | [Link](https://aihub.qualcomm.com/compute/models/stable_diffusion_v2_1) |
| Image Generation | [stable_diffusion_v3_5](generative_ai/image_generation/stable_diffusion_v3_5/) | Text → 1024×1024 image (SD v3.5 Medium) | — |

### Multimodal

| Domain | Model | Task | AI Hub |
|--------|-------|------|--------|
| Image to Text | [easy_ocr](multimodal/image_to_text/easy_ocr/) | Image → text (English + Chinese OCR) | [Link](https://aihub.qualcomm.com/compute/models/easy_ocr) |
| Image Classification | [openai_clip](multimodal/image_classification/openai_clip/) | Images + text → similarity scores (CLIP ViT-B/16) | [Link](https://aihub.qualcomm.com/compute/models/openai_clip) |
| Text Embedding | [nomic_embed_text](multimodal/text_embedding/nomic_embed_text/) | Text → 768-dim embedding (BERT-based, for RAG) | [Link](https://aihub.qualcomm.com/compute/models/nomic_embed_text) |
| Translation | [opus_mt_zh_en](multimodal/translation/opus_mt_zh_en/) | Chinese text → English (MarianMT) | [Link](https://aihub.qualcomm.com/compute/models/opus_mt_zh_en) |
| Vision Language Model | [qwen_vl](multimodal/vision_language_model/qwen_vl/) *(Linux only)* | Image/video + question → answer (Qwen2-VL/Qwen3-VL) | — |

## Running Models

All models are run from the `samples/` directory using `run_inference.py`:

```bash
cd qai-appbuilder\samples
python run_inference.py --model <model_name>
```

Or run the script directly:
```bash
cd qai-appbuilder\samples
python models\audio\audio_classification\yamnet\python\yamnet.py
```

## Platform Support

| Platform | Runtime | Notes |
|----------|---------|-------|
| Windows on Snapdragon (WoS) | HTP (NPU) | Full support, all models |
| x86 Windows | CPU + DLC | Fallback mode, slower |
| ARM64 Linux | HTP (NPU) | Requires `--chipset` argument |
| x86 Linux | HTP (NPU) | Requires `--chipset` argument |
