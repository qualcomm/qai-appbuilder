# yolov8_det Sample Code

## Introduction
This is sample code for using AppBuilder to load yolov8_det QNN model to HTP and execute inference to predicts bounding boxes and classes of objects in an image.

## Setup AppBuilder environment and prepare QNN SDK libraries by referring to the links below: 
https://github.com/quic/ai-engine-direct-helper/blob/main/README.md
https://github.com/quic/ai-engine-direct-helper/blob/main/Docs/User_Guide.md

Copy the QNN libraries from QNN SDK to below path:
```
C:\ai-hub\yolov8_det\qai_libs\libQnnHtpV73Skel.so
C:\ai-hub\yolov8_det\qai_libs\QnnHtp.dll
C:\ai-hub\yolov8_det\qai_libs\QnnHtpV73Stub.dll
C:\ai-hub\yolov8_det\qai_libs\QnnSystem.dll
C:\ai-hub\yolov8_det\qai_libs\libqnnhtpv73.cat
```

## yolov8_det QNN models
Download the quantized yolov8_det QNN models from Qualcomm® AI Hub:
https://aihub.qualcomm.com/compute/models/yolov8_det

After downloaded the model, copy it to the following path:
```
"C:\ai-hub\yolov8_det\models\yolov8_det.bin"
```

## Run the sample code
Download the sample code from the following link:
https://github.com/quic/ai-engine-direct-helper/blob/main/Samples/yolov8_det/yolov8_det.py

After downloaded the sample code, please copy it to the following path:
```
C:\ai-hub\yolov8_det\
```

Copy one sample 640x640 image to following path:
```
C:\ai-hub\yolov8_det\in.jpg
```

Run the sample code:
```
python yolov8_det.py
```

## Output
The output image will be saved to the following path:
```
C:\ai-hub\yolov8_det\out.jpg
```

## Reference
You need to setup the AppBuilder environment before you run the sample code. Below is the guide on how to setup the AppBuilder environment:
https://github.com/quic/ai-engine-direct-helper/blob/main/README.md
https://github.com/quic/ai-engine-direct-helper/blob/main/Docs/User_Guide.md


