# real_esrgan_general_x4v3 Sample Code

## Introduction
This is sample code for using AppBuilder to load real_esrgan_general_x4v3 QNN model to HTP and execute inference to upscale an image with minimal loss in quality. 

## Setup AppBuilder environment and prepare QNN SDK libraries by referring to the links below: 
https://github.com/quic/ai-engine-direct-helper/blob/main/README.md <br>
https://github.com/quic/ai-engine-direct-helper/blob/main/Docs/User_Guide.md

Copy the QNN libraries from QNN SDK to below path:
```
C:\ai-hub\real_esrgan_general_x4v3\qai_libs\libQnnHtpV73Skel.so
C:\ai-hub\real_esrgan_general_x4v3\qai_libs\QnnHtp.dll
C:\ai-hub\real_esrgan_general_x4v3\qai_libs\QnnHtpV73Stub.dll
C:\ai-hub\real_esrgan_general_x4v3\qai_libs\QnnSystem.dll
C:\ai-hub\real_esrgan_general_x4v3\qai_libs\libqnnhtpv73.cat
```

## real_esrgan_general_x4v3 QNN models
The quantized real_esrgan_general_x4v3 QNN model's input resolution is 128x128 from Qualcomm® AI Hub:
https://aihub.qualcomm.com/compute/models/real_esrgan_general_x4v3

The input resolution is too small, we'll use AI Hub API to generate 512x512 QNN model.
You can refer to below links on how to setup AI Hub envirinment and how to use AI Hub API:
https://aihub.qualcomm.com/get-started
http://app.aihub.qualcomm.com/docs/

a. Install the AI Hub Python packages:
```
pip install qai-hub qai_hub_models
```

b. Use below commmand to generate QNN model which suppor 515x512 input resolution:
```
python -m qai_hub_models.models.real_esrgan_general_x4v3.export --device "Snapdragon X Elite CRD" --target-runtime qnn --height 512 --width 512
```

Part of the above command output as below, you can download the model through the link 'https://app.aihub.qualcomm.com/jobs/j1p86jxog/':
```
Optimizing model real_esrgan_general_x4v3 to run on-device
Uploading model: 100%|█████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████| 64.7M/64.7M [00:21<00:00, 3.15MB/s]
Scheduled compile job (j1p86jxog) successfully. To see the status and results:
    https://app.aihub.qualcomm.com/jobs/j1p86jxog/
```
After downloaded the model, copy it to the following path:
```
C:\ai-hub\real_esrgan_general_x4v3\models\real_esrgan_general_x4v3.bin
```

## Run the sample code
Download the sample code from the following link:
https://github.com/quic/ai-engine-direct-helper/blob/main/Samples/real_esrgan_general_x4v3/real_esrgan_general_x4v3.py

After downloaded the sample code, please copy it to the following path:
```
C:\ai-hub\real_esrgan_general_x4v3\
```

Copy one sample 512x512 image to following path:
```
C:\ai-hub\real_esrgan_general_x4v3\in.jpg
```

Run the sample code:
```
python real_esrgan_general_x4v3.py
```

## Output
The output image will be saved to the following path:
```
C:\ai-hub\real_esrgan_general_x4v3\out.jpg
```

## Reference
You need to setup the AppBuilder environment before you run the sample code. Below is the guide on how to setup the AppBuilder environment:
https://github.com/quic/ai-engine-direct-helper/blob/main/README.md
https://github.com/quic/ai-engine-direct-helper/blob/main/Docs/User_Guide.md
