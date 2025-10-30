## Convert DLC model to QNN(*.bin) format:

### Install QAIRT SDK:
Download [QAIRT SDK](https://softwarecenter.qualcomm.com/api/download/software/sdks/Qualcomm_AI_Runtime_Community/All/2.36.0.250627/v2.36.0.250627.zip) and unzip it to: C:\Qualcomm\AIStack\QAIRT\2.36.0.250627\


## Snapdragon® X Elite Devices (Windows on Snapdragon)
### Get Config Files
Copy config files to DLC model folder: <br>
[DLC2BIN Config (v73)](https://github.com/quic/ai-engine-direct-helper/tree/main/tools/convert/dlc2bin/config/v73_wos)

### Copy QNN Files
Copy the files below from 'C:\Qualcomm\AIStack\QAIRT\2.36.0.250627\lib\hexagon-v73\unsigned' to DLC model folder:
```
libqnnhtpv73.cat
libQnnHtpV73Skel.so
```

NOTE: Please ensure that there are no spaces and non-English characters in the model path. <br>

### Convert the Fp16 & Int8 models to QNN:
We use the [RealESRGAN](https://aihub.qualcomm.com/compute/models/real_esrgan_x4plus) model as sample. Make sure to rename the 'real_esrgan_x4plus' in 'extensions.conf' to your model name.
```
Set QNN_SDK_ROOT=C:\Qualcomm\AIStack\QAIRT\2.36.0.250627\
Set PATH=%QNN_SDK_ROOT%lib\aarch64-windows-msvc;%QNN_SDK_ROOT%bin\aarch64-windows-msvc;%PATH%

Fp16:
qnn-context-binary-generator.exe --log_level error --backend QnnHtp.dll --model QnnModelDlc.dll --dlc_path real_esrgan_x4plus-real-esrgan-x4plus-float.dlc --binary_file "real_esrgan_x4plus.fp16" --output_dir output --config_file config_local.json

Int8:
qnn-context-binary-generator.exe --log_level error --backend QnnHtp.dll --model QnnModelDlc.dll --dlc_path real_esrgan_x4plus-real-esrgan-x4plus-w8a8.dlc --binary_file "real_esrgan_x4plus.int8" --output_dir output --config_file config_local.json
```

## Snapdragon® 8 Elite Mobile Devices (Android Phone and Tablet)
### Get Config Files
Copy config files to DLC model folder. We name the folder as 'DLC2BIN': <br>
| Device                | Files | Download Link |
|-----------------------|-------|---------------|
| Snapdragon® 8 Elite   | v79   | [download](https://github.com/quic/ai-engine-direct-helper/tree/main/tools/convert/dlc2bin/config/v79_mobile) |
| Snapdragon® 8 Elite Gen 5   | v81   | [download](https://github.com/quic/ai-engine-direct-helper/tree/main/tools/convert/dlc2bin/config/v81_mobile) |

### Push Files to Android Device
Enabling Developer Mode in Android device and connect to your PC frist, then run below commands to copy the binary and model files to Android device: <br>
```
adb shell mkdir /data/local/tmp/DLC2BIN
adb push D:\AppBuilder\DLC2BIN /data/local/tmp/

adb push C:\Qualcomm\AIStack\QAIRT\2.36.0.250627\bin\aarch64-android\qnn-context-binary-generator /data/local/tmp/DLC2BIN/
adb push C:\Qualcomm\AIStack\QAIRT\2.36.0.250627\bin\aarch64-android\qnn-context-binary-utility /data/local/tmp/DLC2BIN/

adb push C:\Qualcomm\AIStack\QAIRT\2.36.0.250627\lib\aarch64-android\libQnnHtp.so /data/local/tmp/DLC2BIN/
adb push C:\Qualcomm\AIStack\QAIRT\2.36.0.250627\lib\aarch64-android\libQnnHtpPrepare.so /data/local/tmp/DLC2BIN/
adb push C:\Qualcomm\AIStack\QAIRT\2.36.0.250627\lib\aarch64-android\libQnnSystem.so /data/local/tmp/DLC2BIN/
adb push C:\Qualcomm\AIStack\QAIRT\2.36.0.250627\lib\aarch64-android\libQnnHtpV79Stub.so /data/local/tmp/DLC2BIN/
adb push C:\Qualcomm\AIStack\QAIRT\2.36.0.250627\lib\aarch64-android\libQnnModelDlc.so /data/local/tmp/DLC2BIN/
adb push C:\Qualcomm\AIStack\QAIRT\2.36.0.250627\lib\aarch64-android\libQnnHtpNetRunExtensions.so /data/local/tmp/DLC2BIN/

adb push C:\Qualcomm\AIStack\QAIRT\2.36.0.250627\lib\hexagon-v79\unsigned\libQnnHtpV79Skel.so /data/local/tmp/DLC2BIN/
```

You can find below files in your devices:
```
sun:/data/local/tmp/DLC2BIN $ ls -l
total 163983
-rw-rw-rw- 1 shell shell      139 2025-08-05 20:02 config_local.json
-rw-rw-rw- 1 shell shell      435 2025-08-03 14:51 extensions.conf
-rw-rw-rw- 1 shell shell  2395776 2025-08-06 21:23 libQnnHtp.so
-rw-rw-rw- 1 shell shell   751608 2025-08-06 21:23 libQnnHtpNetRunExtensions.so
-rw-rw-rw- 1 shell shell 58470536 2025-08-06 21:23 libQnnHtpPrepare.so
-rw-rw-rw- 1 shell shell  9536980 2025-08-06 21:23 libQnnHtpV79Skel.so
-rw-rw-rw- 1 shell shell   709608 2025-08-06 21:23 libQnnHtpV79Stub.so
-rw-rw-rw- 1 shell shell  2459080 2025-08-06 21:23 libQnnModelDlc.so
-rw-rw-rw- 1 shell shell  2528832 2025-08-06 21:23 libQnnSystem.so
-rwxrwxrwx 1 shell shell  3563352 2025-08-06 21:22 qnn-context-binary-generator
-rwxrwxrwx 1 shell shell   824336 2025-08-06 21:22 qnn-context-binary-utility
-rw-rw-rw- 1 shell shell 67841179 2025-08-02 00:29 real_esrgan_x4plus-real-esrgan-x4plus-float.dlc
-rw-rw-rw- 1 shell shell 18619376 2025-08-02 00:29 real_esrgan_x4plus-real-esrgan-x4plus-w8a8.dlc
```

### Convert the Fp16 & Int8 models to QNN:
Run below commands to convert the model to BIN format: <br>
```
adb shell
cd /data/local/tmp/DLC2BIN
chmod +x qnn-context-binary-generator
chmod +x qnn-context-binary-utility

export LD_LIBRARY_PATH=/data/local/tmp/DLC2BIN/
export PATH=$LD_LIBRARY_PATH:$PATH

./qnn-context-binary-generator --log_level error --backend libQnnHtp.so --model libQnnModelDlc.so --dlc_path real_esrgan_x4plus-real-esrgan-x4plus-float.dlc --binary_file "real_esrgan_x4plus.fp16" --output_dir output --config_file config_local.json

./qnn-context-binary-generator --log_level error --backend libQnnHtp.so --model libQnnModelDlc.so --dlc_path real_esrgan_x4plus-real-esrgan-x4plus-w8a8.dlc --binary_file "real_esrgan_x4plus.int8" --output_dir output --config_file config_local.json
```

### Check model information:
Run below commands to check the model information: <br>
```
./qnn-context-binary-utility --context_binary output/real_esrgan_x4plus.fp16.bin --json_file info.fp16.json
./qnn-context-binary-utility --context_binary output/real_esrgan_x4plus.int8.bin --json_file info.int8.json
```

### Reference
The configuration file extensions.conf has "soc_id" and "dsp_arch" item, you can find the value for Snapdragon devices here: <br>
[Supported Snapdragon devices](https://docs.qualcomm.com/bundle/publicresource/topics/80-63442-50/overview.html#supported-snapdragon-devices)
