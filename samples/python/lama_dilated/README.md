# lama_dilated Sample Code

## Introduction
This is sample code for using AppBuilder to load lama_dilated QNN model to HTP and execute inference to erase and in-paint part of given input image.

## Setup AppBuilder environment and prepare QNN SDK libraries by referring to the links below: 
https://github.com/quic/ai-engine-direct-helper/blob/main/README.md
https://github.com/quic/ai-engine-direct-helper/blob/main/Docs/User_Guide.md

Copy the QNN libraries from QNN SDK to below path:
```
C:\ai-hub\lama_dilated\qai_libs\libQnnHtpV73Skel.so
C:\ai-hub\lama_dilated\qai_libs\QnnHtp.dll
C:\ai-hub\lama_dilated\qai_libs\QnnHtpV73Stub.dll
C:\ai-hub\lama_dilated\qai_libs\QnnSystem.dll
C:\ai-hub\lama_dilated\qai_libs\libqnnhtpv73.cat
```

## lama_dilated QNN models
Download the quantized lama_dilated QNN models from Qualcomm® AI Hub:
https://aihub.qualcomm.com/compute/models/lama_dilated

After downloaded the model, copy it to the following path:
```
"C:\ai-hub\lama_dilated\models\lama_dilated.bin"
```

## Run the sample code
Download the sample code from the following link:
https://github.com/quic/ai-engine-direct-helper/blob/main/Samples/lama_dilated/lama_dilated.py

After downloaded the sample code, please copy it to the following path:
```
C:\ai-hub\lama_dilated\
```

Copy one sample 512x512 image and mask to following path:
```
C:\ai-hub\lama_dilated\test_input_image.png
C:\ai-hub\lama_dilated\test_input_mask.png
```

Run the sample code:	
```
python lama_dilated.py
```

## Output
The output image will be saved to the following path:
```
C:\ai-hub\lama_dilated\out.png
```

## Reference
You need to setup the AppBuilder environment before you run the sample code. Below is the guide on how to setup the AppBuilder environment:
https://github.com/quic/ai-engine-direct-helper/blob/main/README.md
https://github.com/quic/ai-engine-direct-helper/blob/main/Docs/User_Guide.md


