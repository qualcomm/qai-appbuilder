# Shared Utilities

This directory contains shared utilities used by all Python inference samples in `models/`.

## Directory Structure

```
shared/
├── python/     # Shared Python modules
└── cpp/        # Shared C++ dependencies
```

## Python Utilities (`python/`)

| File | Used By | Description |
|------|---------|-------------|
| `setup.py` | — | Automated environment setup: installs QAI AppBuilder + QNN runtime libraries |
| `install.py` | All samples | `download_qai_hubmodel()`, `download_url()`, `detect_device_model()`, `get_cpu_name()`, `validate_chipset()` |
| `image_processing.py` | CV + Multimodal | `preprocess_PIL_image`, `pil_resize_pad`, `pil_undo_resize_pad`, `preprocess_inputs`, `resize_pad` |
| `_image_classification.py` | beit, googlenet, inception_v3 | `ImageClassificationQNNContext`, `preprocess_for_classification`, `top_k_classifications`, `load_imagenet_labels` |
| `_image_editing.py` | lama_dilated, aotgan | `ImageEditingQNNContext`, `download_model`, `init_htp_model`, `preprocess_for_inpainting`, `postprocess_inpainted_output` |
| `_super_resolution.py` | real_esrgan_x4plus, real_esrgan_general_x4v3, quicksrnetmedium | `SuperResolutionQNNContext`, `detect_platform`, `preprocess_image_for_sr`, `postprocess_sr_output` |
| `_face_recognition.py` | face_attrib_net, facemap_3dmm | `FaceRecognitionQNNContext`, `preprocess_face_image`, `save_face_attributes_json`, `download_asset` |
| `_pose_estimation.py` | mediapipe_hand, openpose | `HAND_LANDMARK_CONNECTIONS`, `MediaPipePyTorchAsRoot`, `batched_nms`, geometry helpers |
| `_speech_recognition.py` | whisper_base_en, whisper_tiny_en | `log_mel_spectrogram`, `apply_timestamp_rules`, `download_whisper_models`, `get_whisper_tokenizer`, Whisper model classes |
| `_text_generation.py` | nomic_embed_text, opus_mt_zh_en | `TextGenerationQNNContext`, `init_htp_model`, `run_inference_with_perf_profile`, `get_tokenizer`, `tokenize_text` |
| `_stable_diffusion.py` | stable_diffusion_v1_5, stable_diffusion_v2_1 | `set_qnn_config`, `download_sd_component`, `generate_initial_latent`, `decode_vae_output` |
| `_genai_sd.py` | stable_diffusion_v1_5, stable_diffusion_v2_1 | `TextEncoderQNNContext`, `UnetQNNContext`, `VaeDecoderQNNContext`, `get_tokenizer`, `tokenize_prompt`, `get_scheduler` |
| `python_samples_guide.md` | — | Complete developer guide for all shared utilities |

## Setup

Run from the `samples/` directory:

```bash
cd qai-appbuilder\samples\shared\python
python setup.py
```

This automatically:
1. Downloads and installs the QAI AppBuilder Python package
2. Downloads and installs the required QNN runtime libraries for your device

## C++ Dependencies (`cpp/`)

| Directory | Description |
|-----------|-------------|
| `cpp/opencv/` | OpenCV library documentation for C++ samples |

See [cpp/README.md](cpp/README.md) for C++ build instructions.
