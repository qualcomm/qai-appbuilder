# unet_segmentation Sample Code

## Introduction
This is sample code for using AppBuilder to load unet_segmentation QNN model to HTP and execute inference to produce a segmentation mask for an image.

## Setup AppBuilder environment and prepare QNN SDK libraries by referring to the links below: 
https://github.com/quic/ai-engine-direct-helper/blob/main/README.md
https://github.com/quic/ai-engine-direct-helper/blob/main/Docs/User_Guide.md

Copy the QNN libraries from QNN SDK to below path:
```
C:\ai-hub\unet_segmentation\qai_libs\libQnnHtpV73Skel.so
C:\ai-hub\unet_segmentation\qai_libs\QnnHtp.dll
C:\ai-hub\unet_segmentation\qai_libs\QnnHtpV73Stub.dll
C:\ai-hub\unet_segmentation\qai_libs\QnnSystem.dll
C:\ai-hub\unet_segmentation\qai_libs\libqnnhtpv73.cat
```

## unet_segmentation QNN models
Download the quantized unet_segmentation QNN models from Qualcomm® AI Hub:
https://aihub.qualcomm.com/compute/models/unet_segmentation

After downloaded the model, copy it to the following path:
```
"C:\ai-hub\unet_segmentation\models\unet_segmentation.bin"
```

## Run the sample code
Download the sample code from the following link:
https://github.com/quic/ai-engine-direct-helper/blob/main/Samples/unet_segmentation/unet_segmentation.py

After downloaded the sample code, please copy it to the following path:
```
C:\ai-hub\unet_segmentation\
```

Copy one sample image to following path:
```
C:\ai-hub\unet_segmentation\in.jpg
```

Run the sample code:	
```
python unet_segmentation.py
```

## Output
The output image will be saved to the following path:
```
C:\ai-hub\unet_segmentation\out.jpg
```

## Reference
You need to setup the AppBuilder environment before you run the sample code. Below is the guide on how to setup the AppBuilder environment:
https://github.com/quic/ai-engine-direct-helper/blob/main/README.md
https://github.com/quic/ai-engine-direct-helper/blob/main/Docs/User_Guide.md


