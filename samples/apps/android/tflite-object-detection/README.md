# TFLite Object Detection (Java + Qualcomm QNN LiteRT)

This standalone Android sample shows the recommended Java TFLite integration for Qualcomm devices. It loads `model.tflite` from app assets and tries the following providers:

```text
QNN NPU -> GPU -> CPU/XNNPack
```

The activity displays the selected provider and input/output tensor metadata. Provider reporting is based on the delegates actually attached to the interpreter:

- `qnn-npu`: Qualcomm QNN LiteRT delegate
- `gpu`: TensorFlow Lite GPU delegate
- `cpu`: TensorFlow Lite XNNPack/CPU fallback

The sample does not bundle a test image. It provides `PICK IMAGE` plus `RUN
DETECTION` buttons; select an image from the Android document picker before
running detection. It letterboxes the selected image to
640x640, runs the YOLOX model, and draws the detected boxes, labels, scores,
provider, and inference time. `labels.txt` contains the COCO label mapping.

## Build

Download the **TFLite W8A8/UINT8** `yolox.tflite` model from [Qualcomm AI Hub YOLOX](https://aihub.qualcomm.com/models/yolox?domain=Computer+Vision&useCase=Object+Detection), then copy it to:

```text
app/src/main/assets/model.tflite
```

For example, if the model was downloaded under `C:\models`:

```powershell
Copy-Item C:\models\yolox.tflite app\src\main\assets\model.tflite
```

The model is intentionally not committed because TFLite models can be large, proprietary, or device-specific. The build fails early with an actionable message when the asset is absent.

This sample supports only the UINT8 YOLOX export with a `[1,640,640,3]` UINT8
input and UINT8 outputs shaped `[1,N,4]`, `[1,N]`, and `[1,N]`. It reads the
boxes and scores quantization parameters from the loaded model. Float, FP16,
or differently-shaped YOLOX exports are rejected at startup with an actionable
message.

From this directory:

```powershell
.\gradlew.bat assembleDebug
adb install -r app\build\outputs\apk\debug\app-debug.apk
```

`model.tflite` is packaged in the APK, so `adb install` copies it to the
Android target device. Do not use `adb push` for this sample: it loads the
model only from its packaged assets.

On Linux/macOS:

```bash
./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

For the image demo, install the debug APK, tap `PICK IMAGE`, select an image,
then tap `RUN DETECTION`. Android's document picker does not need storage
permissions.

## Dependencies and compatibility

The sample pins:

- TensorFlow Lite `2.16.1`
- TensorFlow Lite GPU delegate plugin `0.4.4`
- Qualcomm QNN runtime and LiteRT delegate `2.40.0`

Keep the QNN runtime, LiteRT delegate, QAIRT release, and target-device runtime coherent. Do not copy native QNN or TFLite C API libraries into this sample: the Java path obtains its runtime from Gradle AARs. Native TFLite C API/JNI integration belongs to a separate runtime project and is not bundled into qai-appbuilder.

This sample is restricted to `arm64-v8a`, matching Qualcomm Android QNN deployment targets.

## QNN HTP runtime

The Android device supplies the FastRPC client libraries. The manifest declares
`libadsprpc.so` and `libcdsprpc.so` as optional native libraries, matching the
SuperResolution sample. Do not copy either library from Hexagon SDK or QAIRT
into this APK: Android must resolve the client from the device vendor image.

The QNN runtime and LiteRT delegate are provided by the Gradle AARs. If the
device image does not expose a working DSP/HTP transport, the app reports the
QNN error and falls back to GPU or CPU; this cannot be repaired by adding a
userspace `.so` to the APK.
