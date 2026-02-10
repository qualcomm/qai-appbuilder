## Build and Run GenieChat app
GenieChat is an Android application that demonstrates how to integrate large language models (LLMs) and VLM using the Genie API. It features a clean, modern user interface and supports real-time streaming responses, delivering a seamless interactive user experience.<br>
• Please build or download [GenieAPIService.apk](https://github.com/quic/ai-engine-direct-helper/releases/download/v2.42.0/GenieAPIService.apk), run it refer to [this link](https://github.com/quic/ai-engine-direct-helper/blob/main/samples/genie/c%2B%2B/docs/USAGE.MD#use-for-android) firstly.<br>
• Please download [GenieChat source codes](https://github.com/quic/ai-engine-direct-helper/tree/main/samples/android/GenieChat) and build GenieChat apk in android studio. <br>
• Then run it on Mobile device, refer to [this document](https://www.aidevhome.com/?id=50).<br>

## Build and Run SuperResolution sample app on Mobile Phone(Snapdragon® 8 Elite and and Snapdragon® 8 Elite Gen 5)

Please install [Qualcomm® AI Runtime SDK](https://softwarecenter.qualcomm.com/#/catalog/item/Qualcomm_AI_Runtime_SDK) firstly.<br>
> *Note: For Snapdragon® 8 Elite Gen 5 devices, please install the [Qualcomm® AI Runtime SDK](https://softwarecenter.qualcomm.com/#/catalog/item/Qualcomm_AI_Runtime_SDK) version 2.40.1.251119 or newer. * <br>

The SuperResolution sample app demostrates how to convert an image using [SuperResolution AI models](https://aihub.qualcomm.com/mobile/models?domain=Computer+Vision&useCase=Super+Resolution) on android mobile devices.<br>

Following are the detailed steps to build and run:<br>

### Convert DLC model to QNN(*.bin) format
Firstly, download SuperResolution AI models dlc files from [SuperResolution AI models](https://aihub.qualcomm.com/mobile/models?domain=Computer+Vision&useCase=Super+Resolution). 

Then, covert the dlc files into QNN(*.bin) files, according to this Link: [Snapdragon® 8 Elite Mobile Devices (Android Phone and Tablet)](https://github.com/quic/ai-engine-direct-helper/blob/main/tools/convert/dlc2bin/README.md#snapdragon-8-elite-mobile-devices-android-phone-and-tablet).

### Push files to Android device
You can copy files into device under MTP mode or use adb commands. 

If use adb commands, please enable Developer Mode in Android device and connect to your PC, then run below commands to copy above models (.bin) files into this directory /sdcard/AIModels/SuperResolution/, take model real_esrgan_x4plus as an example:
```
adb shell mkdir /sdcard/AIModels
adb shell mkdir /sdcard/AIModels/SuperResolution/
adb shell mkdir /sdcard/AIModels/SuperResolution/real_esrgan_x4plus
adb push real_esrgan_x4plus.fp16.bin /sdcard/AIModels/SuperResolution/real_esrgan_x4plus
adb push real_esrgan_x4plus.int8.bin /sdcard/AIModels/SuperResolution/real_esrgan_x4plus
```

Prepare the input image file(input.jpg) and push it into anywhere under /sdcard/ of android device, for example:
```
sun:/sdcard/AIModels/SuperResolution/real_esrgan_x4plus $ ls -l
-rw-rw---- 1 u0_a225 media_rw     9572 2025-06-28 00:19 input.jpg
-rw-rw---- 1 u0_a225 media_rw 39841792 2025-06-28 00:37 real_esrgan_x4plus.fp16.bin
-rw-rw---- 1 u0_a225 media_rw 22237184 2025-06-28 00:37 real_esrgan_x4plus.int8.bin
```
You can also push the input image files and .bin files of other SuperResolution Models, take model quicksrnetmedium as an example:
```
sun:/sdcard/AIModels/SuperResolution/quicksrnetmedium $ ls -l
-rw-rw---- 1 u0_a225 media_rw 217088 2025-06-28 00:46 quicksrnetmedium.fp16.bin
-rw-rw---- 1 u0_a225 media_rw 143360 2025-06-28 00:46 quicksrnetmedium.int8.bin
-rw-rw---- 1 u0_a225 media_rw  16536 2025-06-28 00:47 super_resolution_input.jpg
```

### Download and build SuperResolution app source codes
• Run below command in Windows terminal to download SuperResolution source codes:<br>
```
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive
```
One SuperResolution app source codes are under this folder: [samples/android/SuperResolution](https://github.com/quic/ai-engine-direct-helper/tree/main/samples/android/SuperResolution)<br>
Another SuperResolution app source codes are under this folder: [samples/android/SuperResolution2](https://github.com/quic/ai-engine-direct-helper/tree/main/samples/android/SuperResolution2).<br>
You can use one of above two SuperResolution apps source codes.<br>

• Copy following 8 .so files from QAIRT SDK to folder SuperResolution\app\libs\arm64-v8a\ :<br>
```
C:\Qualcomm\AIStack\QAIRT\{Qualcomm® AI Runtime SDK version}\lib\aarch64-android\libQnnHtp.so 
C:\Qualcomm\AIStack\QAIRT\{Qualcomm® AI Runtime SDK version}\lib\aarch64-android\libQnnHtpNetRunExtensions.so 
C:\Qualcomm\AIStack\QAIRT\{Qualcomm® AI Runtime SDK version}\lib\aarch64-android\libQnnHtpPrepare.so 
C:\Qualcomm\AIStack\QAIRT\{Qualcomm® AI Runtime SDK version}\lib\aarch64-android\libQnnSystem.so
C:\Qualcomm\AIStack\QAIRT\{Qualcomm® AI Runtime SDK version}\lib\aarch64-android\libQnnHtpV79Stub.so
C:\Qualcomm\AIStack\QAIRT\{Qualcomm® AI Runtime SDK version}\lib\aarch64-android\libQnnHtpV81Stub.so
C:\Qualcomm\AIStack\QAIRT\{Qualcomm® AI Runtime SDK version}\lib\hexagon-v79\unsigned\libQnnHtpV79Skel.so
C:\Qualcomm\AIStack\QAIRT\{Qualcomm® AI Runtime SDK version}\lib\hexagon-v81\unsigned\libQnnHtpV81Skel.so

```
• Under folder SuperResolution\app\src\main\cpp\External\, create new folder QAIAppBuilder and subfolder include.<br>
Please Copy 2 files ai-engine-direct-helper\src\LibAppBuilder.hpp and ai-engine-direct-helper\src\Lora.hpp to SuperResolution\app\src\main\cpp\External\QAIAppBuilder\include\.<br>
Please refer to [Build QAI AppBuilder for android](https://github.com/quic/ai-engine-direct-helper/blob/main/BUILD.md) to build appbuilder, and copy generated file libappbuilder.so to folder SuperResolution\app\src\main\cpp\External\QAIAppBuilder\.<br>

• Get source codes of xtensor and xtl from github:<br>
```
cd samples\android\SuperResolution\app\src\main\cpp\External
git clone https://github.com/xtensor-stack/xtensor.git -b 0.25.0
git clone https://github.com/xtensor-stack/xtl.git  -b 0.7.7
```
• Get source codes of OpenCV<br>
Download https://github.com/opencv/opencv/releases/download/4.12.0/opencv-4.12.0-android-sdk.zip, and unzip.<br>
Only copy folder 'OpenCV-android-sdk\sdk\native' to 'samples\android\SuperResolution\app\src\main\cpp\External', rename 'native' folder to 'OpenCV'.

• Build SuperResolution apk in android studio.

### Run SuperResolution app
• Install SuperResolution apk, check "Allow access to manager all files" in "All files access" UI when launch SuperResolution app at the first time. 

• Select one of Models which you have pushed into directory /sdcard/AIModels/SuperResolution/ before, such as quicksrnetmedium/quicksrnetmedium.int8. 

• Press button "SELECT INPUT IMAGE" to select an input image, for example super_resolution_input.jpg.

• Press button "CONVERT" to convert selected input image using selected model, then you can see the preview of output image, for example quicksrnetmedium.int8_output.jpg.
<div style="display: flex; justify-content: space-between;">
    <img src="images\real_esrgan_x4plus.jpg" alt="realesrgan" width="400" >
    <img src="images\quicksrnetmedium.jpg" alt="quicksrnet" width="400" >
</div>
