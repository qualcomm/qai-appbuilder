## Build and Run GenieChat app
GenieChat is an Android application that demonstrates how to integrate large language models (LLMs) and VLM using the Genie API. It features a clean, modern user interface and supports real-time streaming responses, delivering a seamless interactive user experience.<br>
• Please build or download [GenieAPIService.apk](https://github.com/qualcomm/qai-appbuilder/releases/download/v2.42.0/GenieAPIService.apk), run it refer to [this link](https://github.com/qualcomm/qai-appbuilder/blob/main/samples/genie/c%2B%2B/docs/USAGE.MD#use-for-android) firstly.<br>
• Please download [GenieChat source codes](https://github.com/qualcomm/qai-appbuilder/tree/main/samples/android/GenieChat) and build GenieChat apk in android studio. <br>
• Then run it on Mobile device, refer to [this document](https://www.aidevhome.com/?id=50).<br>

---

## Build and Run SuperResolution sample app on Mobile Phone(Snapdragon® 8 Elite and Snapdragon® 8 Elite Gen 5)

The SuperResolution Android app demonstrates on-device **4x image super-resolution** powered by [QAI AppBuilder](https://github.com/qualcomm/qai-appbuilder) on Snapdragon-based Android devices. It runs inference entirely on the device's Neural Processing Unit (NPU / HTP) via the Qualcomm AI Runtime SDK, with no cloud dependency.

### Key Features

- **4x super-resolution** — upscales a 128×128 input image to 512×512 using Real-ESRGAN or QuickSRNet models.
- **Multiple model formats** — supports both `.bin` (QNN context binary) and `.dlc` (DLC) model files.
- **Two pre/post-processing modes** — switch at runtime between:
  - **Java (default):** pre/post-processing in Java using OpenCV for Java; the native library only runs inference.
  - **C++:** pre/post-processing inside the native library using OpenCV (C++) and xtensor; Java only passes file paths.
- **Auto model download** — if no model files are found on the device, the app automatically downloads the default model (`real_esrgan_general_x4v3`) and a sample input image.
- **Background inference** — inference runs on a background thread to keep the UI responsive, with a progress indicator shown during processing.
- **Supported devices** — Snapdragon® 8 Elite and Snapdragon® 8 Elite Gen 5.

---

### Supported model formats

The app supports two model formats:

| Format | Backend library | Notes |
|--------|----------------|-------|
| `*.bin` (QNN context binary) | `libQnnHtp.so` | Converted from DLC using the `dlc2bin` tool |
| `*.dlc` (DLC) | `libQnnHtp.so` | Downloaded directly from Qualcomm AI Hub |

The app automatically selects the correct backend based on the file extension. Both `.bin` and `.dlc` files placed under `/sdcard/AIModels/SuperResolution/` are listed in the model selector.

### Pre/post-processing modes

The app lets you choose how the model's pre/post-processing (image resize, normalization, tensor layout, and output decoding) is performed:<br>
• **Java (default):** Pre/post-processing runs in Java using OpenCV for Java. The native library only runs the model inference on a direct buffer.<br>
• **C++:** Pre/post-processing runs inside the native library using OpenCV (C++) and xtensor. Java only passes the input/output image file paths.<br>

Both modes produce the same result; the selector is provided so you can compare the two implementations. Switch modes at runtime with the "Pre/Post-processing" radio buttons on the app's main screen.<br>

> *Note: Inference runs on a background thread to keep the UI responsive. A progress indicator (spinner + status text) is shown while inference is running.*

---

Following are the detailed steps to build and run:<br>

### Prepare model files

#### Option A: Use QNN `.bin` files (recommended)

1. Download SuperResolution AI model DLC files from [Qualcomm AI Hub](https://aihub.qualcomm.com/mobile/models?domain=Computer+Vision&useCase=Super+Resolution).
2. Convert the DLC files to QNN `*.bin` format using the `dlc2bin` tool:  
   [Snapdragon® 8 Elite Mobile Devices (Android Phone and Tablet)](https://github.com/qualcomm/qai-appbuilder/blob/main/tools/convert/dlc2bin/README.md#snapdragon-8-elite-mobile-devices-android-phone-and-tablet)

#### Option B: Use  `.dlc` files directly

Download the DLC files from [Qualcomm AI Hub](https://aihub.qualcomm.com/mobile/models?domain=Computer+Vision&useCase=Super+Resolution) and push them directly to the device (no conversion needed). The app will also use `libQnnHtp.so` as the backend automatically.


### Push files to Android device

Enable Developer Mode on the Android device and connect to your PC via USB, then use adb commands to copy model files to `/sdcard/AIModels/SuperResolution/`.

**Example: push QNN `.bin` files for `real_esrgan_x4plus`**
```
adb shell mkdir /sdcard/AIModels
adb shell mkdir /sdcard/AIModels/SuperResolution/
adb push real_esrgan_x4plus.fp16.bin /sdcard/AIModels/SuperResolution/
adb push real_esrgan_x4plus-qnn_dlc-float /sdcard/AIModels/SuperResolution/
adb push real_esrgan_x4plus-qnn_dlc-w8a8 /sdcard/AIModels/SuperResolution/
```

**Example: push  `.dlc` file**
```
adb shell mkdir /sdcard/AIModels/SuperResolution/real_esrgan_x4plus-dlc
adb push real_esrgan_x4plus.dlc /sdcard/AIModels/SuperResolution/real_esrgan_x4plus-dlc/
```

Prepare an input image file and push it to any location under `/sdcard/`:
```
adb push input.jpg /sdcard/AIModels/SuperResolution/
```

**Example: directory layout on device**
```
/sdcard/AIModels/SuperResolution/
├── real_esrgan_x4plus-qnn_dlc-float/
│   ├── metadata.json
│   └── real_esrgan_x4plus.dlc
├── real_esrgan_x4plus-qnn_dlc-w8a8/
│   ├── metadata.json
│   └── real_esrgan_x4plus.dlc
├── real_esrgan_x4plus.fp16.bin
└── input.jpg

```

### Download and build SuperResolution app source codes

• Run the following command in a Windows terminal to download the source codes:<br>
```
git clone https://github.com/qualcomm/qai-appbuilder.git --recursive
```
The SuperResolution app source codes are under: [samples/android/SuperResolution](https://github.com/qualcomm/qai-appbuilder/tree/main/samples/android/SuperResolution)<br>
This single app supports both the Java (default) and C++ pre/post-processing modes, and both `.bin` and `.dlc` model formats.<br>

You can set up all required dependencies either **automatically** using the provided script, or **manually** step by step.

#### Option A: Automated setup using `setup.bat` (based on QAIRT v2.48.40.260702)

A `setup.bat` script is provided in the project root directory to automate the dependency setup steps below. It performs the following actions automatically:

1. **Checks for Android Studio** — if not installed, downloads and installs [Android Studio](https://developer.android.com/studio) silently.
2. **Downloads QAIRT SDK** (`QAIRT_v2.48.40.260702.zip`) from the [QAI AppBuilder releases](https://github.com/qualcomm/qai-appbuilder/releases/download/v2.48.40/QAIRT_v2.48.40.260702.zip), extracts it, copies `QAIAppBuilder` to `app\src\main\cpp\External\` and `arm64-v8a` libraries to `app\libs\`, then deletes the zip.
3. **Clones xtensor** (v0.25.0) and **xtl** (v0.7.7) into `app\src\main\cpp\External\`.
4. **Downloads OpenCV Android SDK** (`opencv-4.12.0-android-sdk.zip`), extracts it, copies `OpenCV-android-sdk\sdk\native` to `app\src\main\cpp\External\OpenCV`, then deletes the zip.

Run it from the project root in a Windows terminal:
```
setup.bat
```

After `setup.bat` completes, open the project in Android Studio and build the APK directly.

> *Note: If any step was already completed (e.g. the zip already downloaded, or the clone directory already exists), `setup.bat` skips that step automatically.*

#### Option B: Manual setup

• **Copy library files** from the QAIRT SDK to `SuperResolution\app\libs\arm64-v8a\`:<br>

For QNN `.bin` model support (required):
```
{QAIRT_SDK}\lib\aarch64-android\libQnnHtp.so
{QAIRT_SDK}\lib\aarch64-android\libQnnHtpNetRunExtensions.so
{QAIRT_SDK}\lib\aarch64-android\libQnnHtpPrepare.so
{QAIRT_SDK}\lib\aarch64-android\libQnnSystem.so
{QAIRT_SDK}\lib\aarch64-android\libQnnHtpV79Stub.so
{QAIRT_SDK}\lib\aarch64-android\libQnnHtpV81Stub.so
{QAIRT_SDK}\lib\hexagon-v79\unsigned\libQnnHtpV79Skel.so
{QAIRT_SDK}\lib\hexagon-v81\unsigned\libQnnHtpV81Skel.so
```

> *Note: `{QAIRT_SDK}` refers to the QAIRT SDK installation path, e.g. `C:\Qualcomm\AIStack\QAIRT\{version}`.*

• **Set up QAIAppBuilder:**<br>
Under `SuperResolution\app\src\main\cpp\External\`, create folder `QAIAppBuilder\include\`.<br>
Copy `qai-appbuilder\src\LibAppBuilder.hpp` and `qai-appbuilder\src\Lora.hpp` to that folder.<br>
Refer to [Build QAI AppBuilder for android](https://github.com/qualcomm/qai-appbuilder/blob/main/BUILD.md) to build `libappbuilder.so`, then copy it to `SuperResolution\app\src\main\cpp\External\QAIAppBuilder\`.<br>

• **Get xtensor and xtl** (used by the C++ pre/post-processing mode):<br>
```
cd samples\android\SuperResolution\app\src\main\cpp\External
git clone https://github.com/xtensor-stack/xtensor.git -b 0.25.0
git clone https://github.com/xtensor-stack/xtl.git  -b 0.7.7
```

• **Get native OpenCV** (used by the C++ pre/post-processing mode):<br>
Download [opencv-4.12.0-android-sdk.zip](https://github.com/opencv/opencv/releases/download/4.12.0/opencv-4.12.0-android-sdk.zip) and unzip.<br>
Copy folder `OpenCV-android-sdk\sdk\native` to `samples\android\SuperResolution\app\src\main\cpp\External\` and rename it to `OpenCV`.

> *Note: The Java pre/post-processing mode (default) uses the OpenCV for Java library, which is pulled automatically by Gradle (`org.opencv:opencv:4.12.0`) — no manual download is required for that mode. The xtensor/xtl and native OpenCV steps above are only needed to build the C++ pre/post-processing mode.*

• **Build** the SuperResolution APK in Android Studio.

### Run SuperResolution app

1. **Install** the SuperResolution APK. On first launch, grant "Allow access to manage all files" permission when prompted.

2. **Select a model** from the dropdown list. The app scans `/sdcard/AIModels/SuperResolution/` recursively and lists all `.bin` and `.dlc` files found. For example:
   - `real_esrgan_x4plus/real_esrgan_x4plus.fp16.bin`
   - `real_esrgan_x4plus-dlc/real_esrgan_x4plus.dlc`

3. **Choose the pre/post-processing mode** with the "Pre/Post-processing" radio buttons:
   - **Java (default)** — pre/post-processing in Java using OpenCV for Java
   - **C++** — pre/post-processing inside the native library using OpenCV + xtensor

4. **Press "SELECT INPUT IMAGE"** to select an input image from the device storage.

5. **Press "CONVERT"** to run super-resolution inference. A progress spinner and status message are displayed while inference runs in the background. When complete, the output image is shown in the preview area and saved to the same directory as the model file (e.g., `real_esrgan_x4plus.fp16_output.jpg`).

> *Note: Inference may take several seconds depending on the model and device. The UI remains responsive during inference.*

<div style="display: flex; justify-content: space-between;">
    <img src="images\Screenshot_bin_java.jpg" alt="realesrgan" width="400" >
    <img src="images\Screenshot_dlc_cplus.jpg" alt="quicksrnet" width="400" >
</div>
