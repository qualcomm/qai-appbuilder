# inception_v3 Sample Code

## Introduction
This is sample code for using AppBuilder to load inception_v3 QNN model to HTP and execute inference to classify images from the Imagenet dataset. 
It can also be used as a backbone in building more complex models for specific use cases.

## Setup AppBuilder environment and prepare QNN SDK libraries by referring to the links below: 
https://github.com/quic/ai-engine-direct-helper/blob/main/README.md <br>
https://github.com/quic/ai-engine-direct-helper/blob/main/Docs/User_Guide.md

Copy the QNN libraries from QNN SDK to below path:
```
C:\ai-hub\inception_v3\qai_libs\libQnnHtpV73Skel.so
C:\ai-hub\inception_v3\qai_libs\QnnHtp.dll
C:\ai-hub\inception_v3\qai_libs\QnnHtpV73Stub.dll
C:\ai-hub\inception_v3\qai_libs\QnnSystem.dll
C:\ai-hub\inception_v3\qai_libs\libqnnhtpv73.cat
```

## inception_v3 QNN models
Download the quantized inception_v3 QNN models from Qualcomm® AI Hub:
https://aihub.qualcomm.com/compute/models/inception_v3

After downloaded the model, copy it to the following path:
```
"C:\ai-hub\inception_v3\models\inception_v3.bin"
```

## Run the sample code
Download the sample code from the following link:
https://github.com/quic/ai-engine-direct-helper/blob/main/Samples/inception_v3/inception_v3.py

After downloaded the sample code, please copy it to the following path:
```
C:\ai-hub\inception_v3\
```

Copy one sample image to following path:
```
C:\ai-hub\inception_v3\in.jpg
```

Run the sample code:	
```
python inception_v3.py
```

## Output
The output will be shown in the terminal:
```
Top 5 predictions for image:

Samoyed 0.999897
Alaskan tundra wolf  0.00828
Arctic fox  0.00118
husky 0.00026
Pyrenean Mountain Dog 0.000204
```

## Reference
You need to setup the AppBuilder environment before you run the sample code. Below is the guide on how to setup the AppBuilder environment:
https://github.com/quic/ai-engine-direct-helper/blob/main/README.md
https://github.com/quic/ai-engine-direct-helper/blob/main/Docs/User_Guide.md


