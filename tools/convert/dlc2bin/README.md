## Convert DLC model to QNN(*.bin) format:

### Install QAIRT SDK:
Download [QAIRT SDK](https://softwarecenter.qualcomm.com/api/download/software/sdks/Qualcomm_AI_Runtime_Community/All/2.36.0.250627/v2.36.0.250627.zip) and unzip it to: C:\Qualcomm\AIStack\QAIRT\2.36.0.250627\

### Get Config Files
Copy config files to DLC model folder: <br>
[DLC2BIN Config (v73)](https://github.com/quic/ai-engine-direct-helper/tree/main/tools/convert/dlc2bin/config/v73)

### Copy QNN Files
Copy the files below from 'C:\Qualcomm\AIStack\QAIRT\2.36.0.250627\lib\hexagon-v73\unsigned' to DLC model folder:
```
libqnnhtpv73.cat
libQnnHtpV73Skel.so
```

### Convert the Fp16 & Int8 models to QNN:
We use the [RealESRGAN](https://aihub.qualcomm.com/compute/models/real_esrgan_x4plus) model as sample:
```
Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.36.0.250627\
Set PATH=%QNN_SDK_ROOT%lib\aarch64-windows-msvc;%QNN_SDK_ROOT%bin\aarch64-windows-msvc;%PATH%

Fp16:
qnn-context-binary-generator.exe --log_level error --backend QnnHtp.dll --model QnnModelDlc.dll --dlc_path real_esrgan_x4plus-real-esrgan-x4plus-float.dlc --binary_file "real_esrgan_x4plus.fp16" --output_dir output --config_file config_local.json

Int8:
qnn-context-binary-generator.exe --log_level error --backend QnnHtp.dll --model QnnModelDlc.dll --dlc_path real_esrgan_x4plus-real-esrgan-x4plus-w8a8.dlc --binary_file "real_esrgan_x4plus.int8" --output_dir output --config_file config_local.json
```