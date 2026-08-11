# Computer Vision Models

This directory contains Python inference samples for computer vision AI models running on the Snapdragon NPU (HTP) via QAI AppBuilder.

## Models

| Subcategory | Model | Task |
|-------------|-------|------|
| [Image Classification](image_classification/) | [beit](image_classification/beit/) | Image → top-5 ImageNet-1K (Vision Transformer) |
| [Image Classification](image_classification/) | [googlenet](image_classification/googlenet/) | Image → top-5 ImageNet-1K (GoogLeNet) |
| [Image Classification](image_classification/) | [inception_v3](image_classification/inception_v3/) | Image → top-5 ImageNet-1K (Inception V3) |
| [Object Detection](object_detection/) | [yolov8_det](object_detection/yolov8_det/) | Image → bounding boxes + 80 COCO class labels |
| [Semantic Segmentation](semantic_segmentation/) | [unet_segmentation](semantic_segmentation/unet_segmentation/) | Image → binary segmentation mask overlay |
| [Depth Estimation](depth_estimation/) | [depth_anything](depth_estimation/depth_anything/) | Image → depth heatmap (plasma colormap) |
| [Pose Estimation](pose_estimation/) | [openpose](pose_estimation/openpose/) | Image → 18 body keypoints |
| [Pose Estimation](pose_estimation/) | [mediapipe_hand](pose_estimation/mediapipe_hand/) | Image/camera → 21 hand landmarks + gesture |
| [Face Recognition](face_recognition/) | [face_attrib_net](face_recognition/face_attrib_net/) | Face → 6 attributes (liveness, glasses, mask, etc.) |
| [Face Recognition](face_recognition/) | [facemap_3dmm](face_recognition/facemap_3dmm/) | Face → 264 3DMM params → 68 facial landmarks |
| [Super Resolution](super_resolution/) | [real_esrgan_x4plus](super_resolution/real_esrgan_x4plus/) | Image → 4× upscaled (.bin/.dlc/.onnx) |
| [Super Resolution](super_resolution/) | [real_esrgan_general_x4v3](super_resolution/real_esrgan_general_x4v3/) | Image → 4× upscaled (general purpose) |
| [Super Resolution](super_resolution/) | [quicksrnetmedium](super_resolution/quicksrnet_medium/) | Image → 4× upscaled (lightweight) |
| [Image Editing](image_editing/) | [lama_dilated](image_editing/lama_dilated/) | Image + mask → inpainted image (LaMa) |
| [Image Editing](image_editing/) | [aotgan](image_editing/aotgan/) | Image + mask → inpainted image (AOT-GAN) |
| [Video Classification](video_classification/) | [resnet_3d](video_classification/resnet_3d/) | MP4 video → top-5 Kinetics-400 action classes |

## Quick Start

```bash
cd qai-appbuilder\samples

# Image classification
python run_inference.py --model beit --args "--image path/to/image.jpg"

# Object detection
python run_inference.py --model yolov8_det --args "--input_image_path input.jpg --output_image_path output.png"

# Super resolution
python run_inference.py --model real_esrgan_x4plus --args "--input_image_path input.jpg"

# Image inpainting
python run_inference.py --model lama_dilated

# Hand gesture recognition
python run_inference.py --model mediapipe_hand
```

## Dependencies

```
pip install torch torchvision Pillow numpy opencv-python ultralytics==8.0.193
```
