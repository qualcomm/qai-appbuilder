# YOLOv8 Detection — Object Detection on Snapdragon NPU

## Overview

**YOLOv8** is a state-of-the-art real-time object detection model, running on the Snapdragon NPU (HTP) via QAI AppBuilder.

- **Task**: Object Detection (80 COCO classes)
- **Input**: RGB image (resized to 640×640)
- **Output**: Bounding boxes with class labels and confidence scores
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [yolov8_det](https://aihub.qualcomm.com/compute/models/yolov8_det)

## Model Architecture

- **Architecture**: YOLOv8 (CSPDarknet backbone + PANet neck + detection head)
- **Input shape**: NHWC `[1, 640, 640, 3]`
- **Output**: Bounding boxes `[x1, y1, x2, y2, confidence, class_id]`
- **Post-processing**: NMS (Non-Maximum Suppression)

## Requirements

```
pip install ultralytics==8.0.193 torch torchvision Pillow numpy opencv-python
```

> **Important:** Use `ultralytics==8.0.193` for compatibility.

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model yolov8_det --args "--input_image_path path\to\image.jpg --output_image_path output.png"
```

Or run directly:
```bash
python models\computer_vision\object_detection\yolov8_det\python\yolov8_det.py --input_image_path path\to\image.jpg
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--input_image_path` | `assets/input.jpg` (auto-downloaded) | Path to the input image |
| `--output_image_path` | `python/output.png` | Path to save the output image with detections |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model is automatically downloaded on first run to `models/`:
- `yolov8_det.bin` — QNN model binary
