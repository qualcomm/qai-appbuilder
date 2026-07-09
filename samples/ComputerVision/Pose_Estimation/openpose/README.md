# OpenPose — Human Body Pose Estimation on Snapdragon NPU

## Overview

**OpenPose** is a real-time multi-person body pose estimation model, running on the Snapdragon NPU (HTP) via QAI AppBuilder. It detects 18 human body keypoints (joints) from a single RGB image.

- **Task**: Human Body Pose Estimation
- **Keypoints**: 18 body joints (nose, neck, shoulders, elbows, wrists, hips, knees, ankles, eyes, ears)
- **Input**: RGB image (resized to 224×224)
- **Output**: Annotated image with keypoints drawn on detected body joints
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [openpose](https://aihub.qualcomm.com/compute/models/openpose)

## Model Architecture

OpenPose uses a multi-stage CNN with Part Affinity Fields (PAF):
- **Input**: `float32[1, 224, 224, 3]` (NHWC format)
- **Output**:
  - `paf`: Part Affinity Fields `[1, 28, 28, 38]` — limb direction vectors
  - `heatmap`: Joint heatmaps `[1, 28, 28, 19]` — joint location confidence maps

## Quick Start

```bash
cd qai-appbuilder\samples
python ComputerVision\Pose_Estimation\openpose\openpose.py --input_image_path input.png --output_image_path output.png
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--input_image_path` | `input.png` | Path to the input image |
| `--output_image_path` | `output.png` | Path to save the annotated output image |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

The model is automatically downloaded on first run:
- `openpose.bin` — QNN model binary

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | Image file (JPG/PNG) | Any size, resized+padded to 224×224 |
| **Output** | Annotated image (PNG) | Green dots at detected keypoints |

## Pipeline Details

```
Image file
    ↓ PIL.Image.open
    ↓ pil_resize_pad → 224×224 (preserves aspect ratio)
    ↓ preprocess_PIL_image → float32 NCHW
    ↓ np.transpose → NHWC: 1×224×224×3
    ↓ OpenPose (NPU)
PAF [1,28,28,38] + Heatmap [1,28,28,19]
    ↓ bicubic upsample to original size
    ↓ gaussian_filter (sigma=3)
    ↓ peak detection (local maxima)
    ↓ Part Affinity Field matching
Keypoints + skeleton connections
    ↓ draw_keypoints (confidence > 0.8)
    ↓ pil_undo_resize_pad → original size
Annotated image (PNG)
```

## Body Keypoints

| Index | Joint | Index | Joint |
| ----- | ----- | ----- | ----- |
| 0 | Nose | 9 | Left hip |
| 1 | Neck | 10 | Left knee |
| 2 | Right shoulder | 11 | Left ankle |
| 3 | Right elbow | 12 | Right hip |
| 4 | Right wrist | 13 | Right knee |
| 5 | Left shoulder | 14 | Right ankle |
| 6 | Left elbow | 15 | Right eye |
| 7 | Left wrist | 16 | Left eye |
| 8 | Mid hip | 17 | Right ear |

## Notes

- Keypoints with confidence < 0.8 are not drawn.
- The post-processing uses the original PyTorch OpenPose algorithm from [Hzzone/pytorch-openpose](https://github.com/Hzzone/pytorch-openpose).
- The model uses `scipy.ndimage.gaussian_filter` for heatmap smoothing.
