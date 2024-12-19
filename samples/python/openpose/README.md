# openpose Sample Code

## Introduction
This is sample code for using AppBuilder to load openpose QNN model to HTP and execute inference to pestimate body and hand pose in an image and return location and confidence for each of 19 joints..

## Setup AppBuilder environment and prepare QNN SDK libraries by referring to the links below: 
https://github.com/quic/ai-engine-direct-helper/blob/main/README.md
https://github.com/quic/ai-engine-direct-helper/blob/main/Docs/User_Guide.md

Copy the QNN libraries from QNN SDK to below path:
```
C:\ai-hub\openpose\qai_libs\libQnnHtpV73Skel.so
C:\ai-hub\openpose\qai_libs\QnnHtp.dll
C:\ai-hub\openpose\qai_libs\QnnHtpV73Stub.dll
C:\ai-hub\openpose\qai_libs\QnnSystem.dll
C:\ai-hub\openpose\qai_libs\libqnnhtpv73.cat
```

## openpose QNN models
Download the quantized openpose QNN models from Qualcomm® AI Hub:
https://aihub.qualcomm.com/compute/models/openpose

After downloaded the model, copy it to the following path:
```
"C:\ai-hub\openpose\models\openpose.bin"
```

## Run the sample code
Download the sample code from the following link:
https://github.com/quic/ai-engine-direct-helper/blob/main/Samples/openpose/openpose.py

After downloaded the sample code, please copy it to the following path:
```
C:\ai-hub\openpose\
```

Copy one sample image to following path:
```
C:\ai-hub\openpose\in.jpg
```

Run the sample code:	
```
python openpose.py
```

## Output
The output image will be saved to the following path:
```
C:\ai-hub\openpose\out.jpg
```

## Reference
You need to setup the AppBuilder environment before you run the sample code. Below is the guide on how to setup the AppBuilder environment:
https://github.com/quic/ai-engine-direct-helper/blob/main/README.md
https://github.com/quic/ai-engine-direct-helper/blob/main/Docs/User_Guide.md


