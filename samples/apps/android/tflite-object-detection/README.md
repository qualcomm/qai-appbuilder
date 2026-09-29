# TFLite Object Detection (Java + Qualcomm QNN LiteRT)

This standalone Android sample shows the recommended Java TFLite integration for Qualcomm devices. It loads `model.tflite` from app assets and tries the following providers:

```text
QNN NPU -> GPU -> CPU/XNNPack
```

The activity displays the selected provider and input/output tensor metadata. Provider reporting is based on the delegates actually attached to the interpreter:

- `qnn-npu`: Qualcomm QNN LiteRT delegate
- `gpu`: TensorFlow Lite GPU delegate
- `cpu`: TensorFlow Lite XNNPack/CPU fallback

## Build

Copy a compatible model to:

```text
app/src/main/assets/model.tflite
```

The model is intentionally not committed because TFLite models can be large, proprietary, or device-specific. The build fails early with an actionable message when the asset is absent.

From this directory:

```powershell
.\gradlew.bat assembleDebug
adb install -r app\build\outputs\apk\debug\app-debug.apk
```

On Linux/macOS:

```bash
./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Run the `QAI TFLite` activity and inspect the provider shown on screen or logcat. Add `model.tflite` before running the instrumentation test:

```bash
adb shell am instrument -w \\
  com.qualcomm.qaiappbuilder.tflite.test/androidx.test.runner.AndroidJUnitRunner
```

## Dependencies and compatibility

The sample pins:

- TensorFlow Lite `2.16.1`
- TensorFlow Lite GPU delegate plugin `0.4.4`
- Qualcomm QNN runtime and LiteRT delegate `2.40.0`

Keep the QNN runtime, LiteRT delegate, QAIRT release, and target-device runtime coherent. Do not copy native QNN or TFLite C API libraries into this sample: the Java path obtains its runtime from Gradle AARs. Native TFLite C API/JNI integration belongs to a separate runtime project and is not bundled into qai-appbuilder.

This sample is restricted to `arm64-v8a`, matching Qualcomm Android QNN deployment targets.
