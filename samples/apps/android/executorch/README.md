# ExecuTorch Android integration

This directory reserves the Android arm64-v8a integration point for static
single-method `.pte` programs loaded through the qai-appbuilder native
ExecuTorch context.

The native build must provide an Android-built `EXECUTORCH_ROOT` containing the
ExecuTorch headers and arm64-v8a libraries. CPU/XNNPACK programs use the
XNNPACK backend; Qualcomm programs require the ExecuTorch Qualcomm backend and
QNN runtime libraries. Do not use a Linux host SDK for an Android build.

Model assets must be stored uncompressed when an application uses mmap or file
descriptors. Configure the consuming Android application with:

```kotlin
androidResources {
    noCompress.add("pte")
}
```

The Android C++ integration is exposed through `ExecuTorchContext`; Java/Kotlin
applications should bridge to it through their own JNI layer or use the
application's ExecuTorch Android distribution directly.
