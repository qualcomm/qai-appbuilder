# FaceMap 3DMM — 3D Face Mapping on Snapdragon NPU

## Overview

**FaceMap 3DMM** (3D Morphable Model) is a face analysis model that reconstructs a 3D face model and projects 68 facial landmarks onto a 2D image, running on the Snapdragon NPU (HTP) via QAI AppBuilder. It estimates face shape, expression, pose, and translation parameters.

- **Task**: 3D Face Reconstruction & Landmark Detection
- **Landmarks**: 68 facial keypoints (projected from 3D to 2D)
- **Parameters**: Shape (219 coefficients), expression (39 coefficients), pitch/yaw/roll, translation, focal length
- **Input**: Face image with bounding box
- **Output**: Annotated image with 68 red landmark dots + landmark coordinates TXT file
- **Platform**: Windows on Snapdragon (WoS), x86 Windows, ARM64 Linux, x86 Linux
- **Runtime**: HTP (Hexagon NPU) or ONNX Runtime (for better accuracy)
- **AI Hub Model**: [facemap_3dmm](https://aihub.qualcomm.com/compute/models/facemap_3dmm)

## Model Architecture

FaceMap 3DMM uses a CNN to regress 3DMM parameters:
- **Input**: `float32[1, 3, 128, 128]` (NCHW format, raw [0, 255] for QNN; [0, 1] for ONNX)
- **Output**: `float32[1, 264]` — 264 parameters:
  - `alpha_id[0:219]`: Shape basis coefficients
  - `alpha_exp[219:258]`: Expression (blendshape) coefficients
  - `pitch[258]`, `yaw[259]`, `roll[260]`: Head rotation angles
  - `tX[261]`, `tY[262]`: Translation
  - `f[263]`: Focal length

## Quick Start

```bash
cd qai-appbuilder\samples
python ComputerVision\Face_Recognition\facemap_3dmm\facemap_3dmm.py
```

With a custom image:
```bash
python ComputerVision\Face_Recognition\facemap_3dmm\facemap_3dmm.py --image path\to\face.jpg
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--image` | `input.jpg` | Path to the input face image |
| `--chipset` | Auto-detected | SoC ID for model download |

## Required Input Files

The following files must be present in the script directory (auto-downloaded):
- `face_img_fbox.txt` — face bounding box coordinates (x0, x1, y0, y1)
- `meanFace.npy` — mean face shape (3×68 vertices)
- `shapeBasis.npy` — shape basis matrix (3×68 × 219)
- `blendShape.npy` — expression blendshape matrix (3×68 × 39)

## Model Download

The model and required assets are automatically downloaded on first run:
- `facemap_3dmm.bin` — QNN model binary (or ONNX float model for better accuracy)
- `face_img_fbox.txt`, `meanFace.npy`, `shapeBasis.npy`, `blendShape.npy`

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | Image file (JPG/PNG) | Face image with bounding box defined in `face_img_fbox.txt` |
| **Output** | Annotated image (`output.jpg`) | 68 red landmark dots on face |
| **Output** | Text file (`demo_output_lmk.txt`) | 68 landmark (x, y) coordinates |

## Pipeline Details

```
Face image + face_img_fbox.txt
    ↓ crop face region → resize 128×128
    ↓ FaceMap 3DMM (NPU or ONNX)
264 parameters [shape, expression, pose, translation, focal]
    ↓ de-normalize parameters
    ↓ build rotation matrix (pitch, yaw, roll)
    ↓ reconstruct 3D vertices: meanFace + shapeBasis×alpha_id + blendShape×alpha_exp
    ↓ apply rotation + translation
    ↓ project to 2D: landmark = vertices[:, 0:2] × f / tZ + 64
    ↓ scale to original image coordinates
    ↓ draw red circles (cv2.circle)
output.jpg + demo_output_lmk.txt
```

## Notes

- **ONNX vs QNN**: If `OnnxRuntimeContext` is available in qai_appbuilder, the ONNX float model is preferred for better accuracy. The QNN quantized model is used as fallback.
- The ONNX model uses normalized input [0, 1] in NCHW format.
- The QNN model uses raw [0, 255] input in NCHW format.
- The model supports both WoS (ARM64 Windows) and x86 Windows platforms.
