# Inference Scripts

This directory contains specialized inference scripts for different model types,
all running on Qualcomm HTP via `qai_appbuilder`.

## Scripts

| Script | Model Type | Input | Output |
|--------|-----------|-------|--------|
| [`infer_generic.py`](infer_generic.py) | Any model | Raw `.raw` files or random | `.raw` binary files |
| [`infer_classify.py`](infer_classify.py) | Image classification | Image file | Top-K class predictions |
| [`infer_detect.py`](infer_detect.py) | Object detection (YOLO/SSD) | Image file | Annotated image with boxes |
| [`infer_segment.py`](infer_segment.py) | Semantic segmentation | Image file | Segmentation overlay image |
| [`infer_sr.py`](infer_sr.py) | Super-resolution | Image file | Upscaled image |

## Quick Start

```bash
# Generic (any model, random input for verification)
python inference/infer_generic.py --model model.bin

# Image classification
python inference/infer_classify.py --model googlenet.bin --input dog.jpg --labels imagenet_labels.json

# Object detection (YOLOv8)
python inference/infer_detect.py --model yolov8_det.bin --input image.jpg --conf 0.45

# Semantic segmentation (UNet)
python inference/infer_segment.py --model unet.bin --input image.jpg --alpha 0.5

# Super-resolution (Real-ESRGAN x4plus)
python inference/infer_sr.py --model real_esrgan_x4plus.bin --input image.jpg --scale 4
```

## Common Arguments

All scripts support:
- `--runtime Htp|Cpu` — QNN runtime (default: Htp)
- `--log_level 0-5` — Log verbosity (default: 1)
- `--input_dir` + `--output_dir` — Batch processing mode

## Requirements

```bash
pip install qai-appbuilder Pillow numpy
```

## Notes

- All scripts auto-discover `data/config/qairt_env.json` (QAIModelBuilder integration)
- Input format is **NHWC** (Height, Width, Channels) — scripts handle NCHW→NHWC conversion
- Context binary (`.bin`) is **required** on `windows-arm64` for real HTP inference. On x64 hosts see `${APP_ROOT}/factory/chat_features/_shared/x64-host-notes.md` for `.bin` loading rules. `.dlc` works directly everywhere via `QNNContext`.
- Use `infer_generic.py` for models without a specialized script
