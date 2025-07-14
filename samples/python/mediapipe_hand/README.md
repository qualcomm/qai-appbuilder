# Introduction 
This is sample code for using QAI AppBuilder to run mediapipe_hand on NPU.
This application supports camera frame online inference (default) and jpg image inference with end_user mode (default) or demo mode.

# 1. End user mode vs. demo mode

## 1 end_user mode
 - Only draw gesture result. Draw nothing if gesture result is none.
 - In camera scenario, inference 1/30 frame.

## 2 demo mode
 - draw 21 keypoints, connnections, ROI, palm rectangle and gesture result (if have).
 - In camera scenario, inference every frame.
<br><br>

# 2. Command
go to <ai-engine-direct-helper> path:
```
cd <ai-engine-direct-helper>\samples\
```

## Option 1: launch camera app and recognize hand (end_user mode by default) without controlling audio playback:
```
python python\mediapipe_hand\mediapipe_hand.py
```

## Option 2: launch camera app and recognize hand to control audio playback:
```
python python\mediapipe_hand\mediapipe_hand.py --audiofile <audio path>
```

## Option 3: launch camera app and recognize hand (demo mode):
```
python python\mediapipe_hand\mediapipe_hand.py --DemoMode True
```

## Option 4: inference sample jpg image and display result:
```
python python\mediapipe_hand\mediapipe_hand.py --imagefile <image path>
```

## Option 5: inference sample jpg image and display result with demo mode:
```
python python\mediapipe_hand\mediapipe_hand.py --imagefile <image path> --DemoMode True
```

## Option 6: inference sample jpg image and save result into jpg image file:
```
python python\mediapipe_hand\mediapipe_hand.py --imagefile <image path> --displayPredict False
```