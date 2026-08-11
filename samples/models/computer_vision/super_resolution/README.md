# Super Resolution

Image super-resolution models that upscale low-resolution images by 4× using deep learning.

## Models

| Model | Architecture | Input | Output | AI Hub |
|-------|-------------|-------|--------|--------|
| [real_esrgan_x4plus](real_esrgan_x4plus/) | Real-ESRGAN (RRDBNet, 23 blocks) | Any image | 4× upscaled image. Supports `.bin`, `.dlc`, `.onnx` (FP16 auto-generated) | [Link](https://aihub.qualcomm.com/compute/models/real_esrgan_x4plus) |
| [real_esrgan_general_x4v3](real_esrgan_general_x4v3/) | Real-ESRGAN General v3 | Any image | 4× upscaled image (general purpose, better for photos) | [Link](https://aihub.qualcomm.com/compute/models/real_esrgan_general_x4v3) |
| [quicksrnetmedium](quicksrnet_medium/) | QuickSRNet Medium | Any image | 4× upscaled image (lightweight, faster) | [Link](https://aihub.qualcomm.com/compute/models/quicksrnetmedium) |

## Quick Start

```bash
cd qai-appbuilder\samples

# Real-ESRGAN x4plus (default: float DLC)
python run_inference.py --model real_esrgan_x4plus --args "--input_image_path input.jpg"

# With precompiled HTP binary
python run_inference.py --model real_esrgan_x4plus --args "--bin --input_image_path input.jpg"

# With FP16 ONNX (auto-generated)
python run_inference.py --model real_esrgan_x4plus --args "--onnx --input_image_path input.jpg"

# Real-ESRGAN General x4v3
python run_inference.py --model real_esrgan_general_x4v3 --args "--input_image_path input.jpg"

# QuickSRNet Medium
python run_inference.py --model quicksrnetmedium --args "--input_image_path input.jpg"
```
