# Object Detection

Object detection models that locate and classify multiple objects in an image.

## Models

| Model | Architecture | Input Shape | Output | AI Hub |
|-------|-------------|-------------|--------|--------|
| [yolov8_det](yolov8_det/) | YOLOv8 | NHWC `[1, 640, 640, 3]` | Bounding boxes + 80 COCO class labels | [Link](https://aihub.qualcomm.com/compute/models/yolov8_det) |

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model yolov8_det --args "--input_image_path input.jpg --output_image_path output.png"
```

> **Note:** Use `ultralytics==8.0.193` for compatibility.
