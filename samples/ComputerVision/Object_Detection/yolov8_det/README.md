# YOLOv8 Detection — Real-Time Object Detection on Snapdragon NPU

## Overview

**YOLOv8 Detection** is a state-of-the-art real-time object detection model, running on the Snapdragon NPU (HTP) via QAI AppBuilder. It detects and localizes 80 COCO object categories in images with bounding boxes and confidence scores.

- **Task**: Object Detection
- **Dataset**: COCO (80 classes)
- **Input**: RGB image (resized to 640×640)
- **Output**: Annotated image with bounding boxes, class labels, and confidence scores
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [yolov8_det](https://aihub.qualcomm.com/compute/models/yolov8_det)

## Model Architecture

YOLOv8 uses a CSP backbone with a decoupled detection head:
- **Input**: `float32[1, 640, 640, 3]` (NHWC format)
- **Output**:
  - `pred_boxes`: `float32[1, N, 4]` — bounding box coordinates (x1, y1, x2, y2)
  - `pred_scores`: `float32[1, N]` — confidence scores
  - `pred_class_idx`: `float32[1, N]` — class indices

## Quick Start

```bash
cd qai-appbuilder\samples
python ComputerVision\Object_Detection\yolov8_det\yolov8_det.py
```

With custom input/output images:
```bash
python ComputerVision\Object_Detection\yolov8_det\yolov8_det.py --input_image_path input.jpg --output_image_path output.png
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--input_image_path` | `input.jpg` | Path to the input image |
| `--output_image_path` | `output.png` | Path to save the annotated output image |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model is automatically downloaded on first run:
- `yolov8_det.bin` — QNN model binary

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | Image file (JPG/PNG) | Any size, resized to 640×640 |
| **Output** | Annotated image (PNG) | Bounding boxes with class labels and confidence scores |

## Pipeline Details

```
Image file
    ↓ PIL.Image.open → resize 640×640
    ↓ PILToTensor → float32 / 255
    ↓ permute → NHWC: 1×640×640×3
    ↓ YOLOv8 (NPU)
pred_boxes [1, N, 4], pred_scores [1, N], pred_class_idx [1, N]
    ↓ batched_nms (score_threshold=0.45, iou_threshold=0.7)
Filtered detections
    ↓ draw_box_from_xyxy (OpenCV)
Annotated image (PNG)
```

## Detected Classes (80 COCO categories)

Person, bicycle, car, motorcycle, airplane, bus, train, truck, boat, traffic light, fire hydrant, stop sign, parking meter, bench, bird, cat, dog, horse, sheep, cow, elephant, bear, zebra, giraffe, backpack, umbrella, handbag, tie, suitcase, frisbee, skis, snowboard, sports ball, kite, baseball bat, baseball glove, skateboard, surfboard, tennis racket, bottle, wine glass, cup, fork, knife, spoon, bowl, banana, apple, sandwich, orange, broccoli, carrot, hot dog, pizza, donut, cake, chair, couch, potted plant, bed, dining table, toilet, tv, laptop, mouse, remote, keyboard, cell phone, microwave, oven, toaster, sink, refrigerator, book, clock, vase, scissors, teddy bear, hair drier, toothbrush

## Example Output

The output image shows detected objects with green bounding boxes and labels like:
```
0.92 person
0.87 car
0.75 dog
```

## Notes

- **NMS on WoS**: This model uses `torchvision.ops.nms` which requires CUDA. On Windows on Snapdragon, this works because the model uses a custom NMS implementation. If you encounter errors, replace `torchvision.ops.nms` with the custom NMS from `common/_pose_estimation.py`.
- **ultralytics version**: Install `ultralytics==8.0.193` to avoid compatibility issues.
- NMS parameters: score threshold = 0.45, IoU threshold = 0.7.
