# aotgan Sample Code

## Introduction
This is sample code for using AppBuilder to load aotgan QNN model to HTP and execute inference to erase and in-paint part of given input image.

## Setup AppBuilder environment and prepare QNN SDK libraries by referring to the links below: 
https://github.com/quic/ai-engine-direct-helper/blob/main/README.md
https://github.com/quic/ai-engine-direct-helper/blob/main/Docs/User_Guide.md

Copy the QNN libraries from QNN SDK to below path:
```
C:\ai-hub\aotgan\qai_libs\libQnnHtpV73Skel.so
C:\ai-hub\aotgan\qai_libs\QnnHtp.dll
C:\ai-hub\aotgan\qai_libs\QnnHtpV73Stub.dll
C:\ai-hub\aotgan\qai_libs\QnnSystem.dll
C:\ai-hub\aotgan\qai_libs\libqnnhtpv73.cat
```

## aotgan QNN models
Download the quantized aotgan QNN models from Qualcomm® AI Hub:
https://aihub.qualcomm.com/compute/models/aotgan

After downloaded the model, copy it to the following path:
```
"C:\ai-hub\aotgan\models\aotgan.bin"
```

## Run the sample code
Download the sample code from the following link:
https://github.com/quic/ai-engine-direct-helper/blob/main/Samples/aotgan/aotgan.py

After downloaded the sample code, please copy it to the following path:
```
C:\ai-hub\aotgan\
```

Copy one sample 512x512 image and mask to following path:
```
C:\ai-hub\aotgan\test_input_image.png
C:\ai-hub\aotgan\test_input_mask.png
```

Run the sample code:	
```
python aotgan.py
```

## Output
The output image will be saved to the following path:
```
C:\ai-hub\aotgan\out.png
```

## Reference
You need to setup the AppBuilder environment before you run the sample code. Below is the guide on how to setup the AppBuilder environment:
https://github.com/quic/ai-engine-direct-helper/blob/main/README.md
https://github.com/quic/ai-engine-direct-helper/blob/main/Docs/User_Guide.md


