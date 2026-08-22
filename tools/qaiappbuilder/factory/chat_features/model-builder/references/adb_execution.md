# ADB Device Deployment and On-Device Inference

When `plan.md` sets `ADB_DEVICE_ID` / `ADB_TARGET_ARCH`, or the user explicitly requests on-device inference, use `adb_runner.py` to push the model and QNN runtime to the device and execute inference there.

---

## 1. ADB Environment Setup

### 1.1 Install adb (Ubuntu host)

```bash
sudo apt-get update && sudo apt-get install -y android-tools-adb
adb version   # verify install
```

### 1.2 Enable USB debugging (Android device)

1. On the device, go to **Settings → About phone** and tap the build number 7 times to enable developer mode.
2. Go to **Settings → Developer options** and enable **USB debugging**.
3. Connect the device via USB and select "File transfer" mode.
4. Accept the RSA fingerprint dialog on the device the first time it connects.

### 1.3 TCP/IP connection (ADB-over-TCP)

```bash
# Prerequisite: device and host on the same LAN
adb tcpip 5555                      # switch device to TCP listen mode (needs a USB connection first)
adb connect <device_ip>:5555        # connect
adb -H <adb_server_host> devices    # connect through a remote ADB server
```

### 1.4 Verify connectivity

```bash
adb devices
# Expected output (one line per online device):
# List of devices attached
# 8347dcb1	device
```

---

## 2. Target Device OS → SDK Directory Mapping

> **Important**: `qnn-net-run` is **not pre-installed** on the device by default. Before every run, `adb_runner.py` automatically pushes the correct arch-specific binary from the QAIRT SDK to the device — no manual step needed.

| `device_os` | `target_arch` (SDK dir name) | `qnn-net-run` source in SDK | Push destination |
|---|---|---|---|
| `android` (default) | `aarch64-android` | `$QAIRT_SDK_ROOT/bin/aarch64-android/qnn-net-run` | `<device_workdir>/<model_stem>/qnn-net-run` |
| `linux` | `aarch64-oe-linux-gcc11.2` | `$QAIRT_SDK_ROOT/bin/aarch64-oe-linux-gcc11.2/qnn-net-run` | `<device_workdir>/<model_stem>/qnn-net-run` |

The same `target_arch` directory is also used to locate the runtime libraries (`$QAIRT_SDK_ROOT/lib/<target_arch>/`).

The `--target_arch` argument explicitly overrides the defaults above and is used for non-standard SDK layouts.

---

## 3. On-Device Directory Layout (convention)

```
<device_workdir>/                    # default: /data/local/tmp/qai_run/
└── <model_stem>/                    # model name (no extension)
    ├── qnn-net-run                  # executable pushed from the SDK (chmod +x already set)
    ├── <model_file>                 # model file (.bin or .dlc), placed at the workdir root
    ├── inputs/
    │   ├── input_0.raw
    │   └── input_list.txt           # on-device absolute paths, space-separated
    ├── <target_arch>/               # runtime libs (e.g. aarch64-android/)
    │   ├── libQnnHtp.so
    │   ├── libQnnSystem.so
    │   ├── libQnnHtpPrepare.so
    │   ├── libQnnHtpNetRunExtensions.so
    │   ├── libQnnHtpV73Stub.so      # matching-SoC stub pushed
    │   └── libQnnHtpV73CalculatorStub.so
    ├── hexagon-<dsp_version>/       # ADSP skel dir (contains unsigned/ subdir)
    │   └── unsigned/
    │       └── libQnnHtpV73Skel.so
    └── output/                      # qnn-net-run writes here; adb pull retrieves
        └── Result_0/
            └── output_0.raw
```

---

## 4. HTP SoC Version → Runtime Library Table

| SoC family | `--dsp_version` | Required stub libs | Hexagon dir |
|---|---|---|---|
| Snapdragon 8 Gen 1 / 8cx Gen 3 | `v73` | `libQnnHtpV73Stub.so`, `libQnnHtpV73CalculatorStub.so` | `hexagon-v73/` |
| Snapdragon 8 Gen 2 | `v73` | same as above | `hexagon-v73/` |
| Snapdragon 8 Gen 3 | `v75` | `libQnnHtpV75Stub.so`, `libQnnHtpV75CalculatorStub.so` | `hexagon-v75/` |
| Snapdragon X Elite / 8 Elite | `v79` | `libQnnHtpV79Stub.so`, `libQnnHtpV79CalculatorStub.so` | `hexagon-v79/` |
| Snapdragon 8s Elite | `v81` | `libQnnHtpV81Stub.so`, `libQnnHtpV81CalculatorStub.so` | `hexagon-v81/` |

> When the SoC version is unknown, `adb_runner.py` defaults to `--dsp_version v73` and tries to push the v73/v79/v81 stub variants (missing files log a `[WARN]` but do not abort).

---

## 5. Common Errors

| Symptom / message | Cause | Fix |
|---|---|---|
| `adb not found on PATH` | adb not installed on the host | `sudo apt-get install -y android-tools-adb` |
| `No ADB devices connected` | no device online | check USB cable / USB debugging / `adb devices` |
| `Multiple devices connected` | multiple devices online and `--device_id` not set | pass `--device_id <serial>` to select the target |
| `qnn-net-run not found in SDK at ...` | wrong `--sdk_root` / `--target_arch` / `--device_os` | verify `$QAIRT_SDK_ROOT/bin/<target_arch>/qnn-net-run` exists |
| DSP inference fails, `ADSP_LIBRARY_PATH` unset | `qnn-net-run` cannot find the hexagon skel libs | `adb_runner.py` sets this automatically; for manual runs add `export ADSP_LIBRARY_PATH=<device_workdir>/<stem>/hexagon-<dsp>/unsigned` |
| `adb push` timeout on large files (>100 MB) | `--push_timeout` default 120 s too short | raise with `--push_timeout 300` |
| Cannot write to `/data/local/tmp` | production device without developer mode | use `adb root` (needs userdebug firmware) or switch to a devkit |
| `qnn-net-run failed (exit=1)`, stderr mentions `ADSP_LIBRARY_PATH` | hexagon skel not pushed or wrong path | verify `hexagon-<dsp>/unsigned/` exists in the SDK; check `--dsp_version` |
| `qnn-net-run failed (exit=1)`, stderr mentions `cannot open shared object` | runtime lib not pushed or wrong `LD_LIBRARY_PATH` | verify `--target_arch` matches the device arch; rerun `adb_runner.py` |

---

## 6. ADB vs. SSH Remote Execution

| Aspect | ADB deploy (`adb_runner.py`) | SSH remote (`RETMOE_DEVICE_INFO`) |
|---|---|---|
| **Transport** | USB / TCP via the `adb` protocol | TCP via OpenSSH |
| **Typical target** | Android phone / embedded board with ADB daemon | Linux dev box / embedded Linux with sshd |
| **Prerequisites** | USB debugging or ADB TCP listener enabled | SSH keys / username-password; `RETMOE_DEVICE_INFO` file |
| **qnn-net-run push** | Auto-pushed from the QAIRT SDK; no on-device pre-install | On-device QAIRT environment required, or deploy manually via SCP |
| **File transfer** | `adb push` / `adb pull` | `scp` / `rsync` |
| **On-device execution** | `adb shell` + env-var injection | `ssh <cmd>` + `source <envsetup.sh>` |
| **plan.md config keys** | `ADB_DEVICE_ID`, `ADB_TARGET_ARCH`, `ADB_DSP_VERSION` | path to the `RETMOE_DEVICE_INFO` file |
| **Reference doc** | This document (`adb_execution.md`) | `remote_execution.md` |

---

## 7. Quick-Start Examples

### Android device (default)

```bash
python ${APP_ROOT}/factory/chat_features/model-builder/scripts/adb_runner.py \
  --model /workspace/model/inception_v3_fp16_contextbin.bin \
  --inputs /workspace/inputs/input_0.raw \
  --output_dir /workspace/outputs \
  --sdk_root $QAIRT_SDK_ROOT \
  --backend htp \
  --device_os android \
  --dsp_version v73
```

### Linux embedded device

```bash
python ${APP_ROOT}/factory/chat_features/model-builder/scripts/adb_runner.py \
  --model /workspace/model/model_fp16_contextbin.bin \
  --inputs /workspace/inputs/input_0.raw \
  --output_dir /workspace/outputs \
  --sdk_root $QAIRT_SDK_ROOT \
  --backend htp \
  --device_os linux \
  --dsp_version v73

```

### List connected devices

```bash
python ${APP_ROOT}/factory/chat_features/model-builder/scripts/adb_runner.py --list_devices
# SERIAL                         STATE
# ----------------------------------------
# 8347dcb1                       device
```

---

## 8. Direct DLC On-Device Inference (optional, for accuracy validation)

`adb_runner.py` supports pushing a `.dlc` directly to the device for execution, as an **optional complement** to the `.bin` context-binary path.

### Choosing between the two paths

| Path | Format | Use case | Prerequisites |
|------|--------|----------|---------------|
| **Default (recommended)** | `.bin` | Performance benchmarking, final deployment | Step 4 → Step 6 (context binary generation) |
| **Optional complement** | `.dlc` | Fast accuracy validation, quantization iteration | Step 4 (or Step 5); Step 6 can be skipped |

`.bin` path: loads fast on the device (already compiled), low latency, suited for performance measurement.
`.dlc` path: skips 100–300 s of context-binary generation, re-runs immediately after tweaking quantization params; the board JIT-compiles on load (slow first inference, not suitable for benchmarking).

### DLC command example

```bash
python ${APP_ROOT}/factory/chat_features/model-builder/scripts/adb_runner.py \
  --model /workspace/model/inception_v3_fp16.dlc \
  --inputs /workspace/inputs/input_0.raw \
  --output_dir /workspace/outputs \
  --sdk_root $QAIRT_SDK_ROOT \
  --backend htp \
  --device_os android \
  --dsp_version v73
```

### Actual on-device command (auto-generated by adb_runner.py)

```bash
qnn-net-run \
    --model     aarch64-android/libQnnModelDlc.so \
    --dlc_path  inception_v3_fp16.dlc \
    --backend   aarch64-android/libQnnHtp.so \
    --input_list inputs/input_list.txt \
    --output_dir output \
    --profiling_level basic \
    --perf_profile burst
```

### Extra library pushed in DLC mode

| Library | SDK source | Notes |
|---------|-----------|-------|
| `libQnnModelDlc.so` | `$QAIRT_SDK_ROOT/lib/<target_arch>/libQnnModelDlc.so` | DLC adapter, passed as `--model` to `qnn-net-run`; the `.bin` path does NOT push this lib |

If `libQnnModelDlc.so` is missing from the SDK, a `[WARN]` is logged but the run continues (some older QAIRT SDK releases may not ship this file).

### Comparison with `.bin` mode (reference)

| Aspect | `.bin` context binary | `.dlc` |
|--------|-----------------------|--------|
| `qnn-net-run` args | `--retrieve_context model.bin` | `--model libQnnModelDlc.so --dlc_path model.dlc` |
| Extra libs | none | `libQnnModelDlc.so` |
| First on-device load | fast (already compiled) | slow (JIT, ~10–60 s depending on model size) |
| Recommended for | performance testing, deployment | accuracy validation, quantization iteration |
