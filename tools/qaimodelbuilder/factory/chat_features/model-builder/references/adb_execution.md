# ADB 设备部署与板端推理

当 `plan.md` 中设置了 `ADB_DEVICE_ID` / `ADB_TARGET_ARCH`，或用户明确要求板端推理时，使用 `adb_runner.py` 将模型和 QNN 运行时推送到设备并执行推理。

---

## 1. ADB 环境配置

### 1.1 安装 adb（Ubuntu 宿主机）

```bash
sudo apt-get update && sudo apt-get install -y android-tools-adb
adb version   # 验证安装
```

### 1.2 USB 调试启用（Android 设备）

1. 设备上进入 **设置 → 关于手机**，连续点击「版本号」7 次，启用开发者模式
2. 进入 **设置 → 开发者选项**，启用 **USB 调试**
3. 用 USB 线连接设备，选择「文件传输」模式
4. 首次连接时在设备上弹出的授权对话框点击「允许」

### 1.3 TCP/IP 连接（ADB-over-TCP）

```bash
# 前提：设备与宿主机在同一局域网
adb tcpip 5555                      # 切换设备到 TCP 监听模式（需先 USB 连接一次）
adb connect <device_ip>:5555        # 连接
adb -H <adb_server_host> devices    # 通过远程 ADB server 连接
```

### 1.4 验证连接

```bash
adb devices
# 期望输出（每台在线设备一行）：
# List of devices attached
# 8347dcb1	device
```

---

## 2. 目标设备 OS 与 SDK 目录映射

> **重要**：板端默认**不预装** `qnn-net-run`。每次运行前 `adb_runner.py` 会自动从 QAIRT SDK 推送对应架构的二进制到板端，无需手动操作。

| `device_os` | `target_arch`（SDK 目录名） | SDK 中 `qnn-net-run` 来源 | 推送目标 |
|---|---|---|---|
| `android`（默认） | `aarch64-android` | `$QAIRT_SDK_ROOT/bin/aarch64-android/qnn-net-run` | `<device_workdir>/<model_stem>/qnn-net-run` |
| `linux` | `aarch64-oe-linux-gcc11.2` | `$QAIRT_SDK_ROOT/bin/aarch64-oe-linux-gcc11.2/qnn-net-run` | `<device_workdir>/<model_stem>/qnn-net-run` |

同一 `target_arch` 目录也用于查找运行时库（`$QAIRT_SDK_ROOT/lib/<target_arch>/`）。

`--target_arch` 参数可显式覆盖以上默认值，适用于非标准 SDK 布局。

---

## 3. 板端目录结构（约定）

```
<device_workdir>/                    # 默认 /data/local/tmp/qai_run/
└── <model_stem>/                    # 模型名（不含扩展名）
    ├── qnn-net-run                  # 从 SDK 推送的可执行文件（chmod +x 已设置）
    ├── <model_file>                 # 模型文件（.bin 或 .dlc），直接放在工作目录根
    ├── inputs/
    │   ├── input_0.raw
    │   └── input_list.txt           # 板端绝对路径，空格分隔
    ├── <target_arch>/               # 运行时库（如 aarch64-android/）
    │   ├── libQnnHtp.so
    │   ├── libQnnSystem.so
    │   ├── libQnnHtpPrepare.so
    │   ├── libQnnHtpNetRunExtensions.so
    │   ├── libQnnHtpV73Stub.so      # 按 SoC 版本推送对应 stub
    │   └── libQnnHtpV73CalculatorStub.so
    ├── hexagon-<dsp_version>/       # ADSP skel 目录（含 unsigned/ 子目录）
    │   └── unsigned/
    │       └── libQnnHtpV73Skel.so
    └── output/                      # qnn-net-run 写出目录；adb pull 回收
        └── Result_0/
            └── output_0.raw
```

---

## 4. HTP SoC 版本 → 运行时库对照表

| SoC 系列 | `--dsp_version` | 需要的 stub 库 | hexagon 目录 |
|---|---|---|---|
| Snapdragon 8 Gen 1 / 8cx Gen 3 | `v73` | `libQnnHtpV73Stub.so`、`libQnnHtpV73CalculatorStub.so` | `hexagon-v73/` |
| Snapdragon 8 Gen 2 | `v73` | 同上 | `hexagon-v73/` |
| Snapdragon 8 Gen 3 | `v75` | `libQnnHtpV75Stub.so`、`libQnnHtpV75CalculatorStub.so` | `hexagon-v75/` |
| Snapdragon X Elite / 8 Elite | `v79` | `libQnnHtpV79Stub.so`、`libQnnHtpV79CalculatorStub.so` | `hexagon-v79/` |
| Snapdragon 8s Elite | `v81` | `libQnnHtpV81Stub.so`、`libQnnHtpV81CalculatorStub.so` | `hexagon-v81/` |

> 不确定 SoC 版本时，`adb_runner.py` 默认使用 `--dsp_version v73`，并会尝试推送 v73/v79/v81 全部 stub 变体（文件不存在时打印 `[WARN]` 不中止）。

---

## 5. 常见错误排查

| 错误信息 / 现象 | 原因 | 解决办法 |
|---|---|---|
| `adb not found on PATH` | 宿主机未安装 adb | `sudo apt-get install -y android-tools-adb` |
| `No ADB devices connected` | 无设备在线 | 检查 USB 线 / USB 调试是否开启 / 运行 `adb devices` 确认 |
| `Multiple devices connected` | 多台设备已连接且未指定 `--device_id` | 用 `--device_id <serial>` 指定目标设备 |
| `qnn-net-run not found in SDK at ...` | `--sdk_root` 或 `--target_arch` / `--device_os` 有误 | 检查 `$QAIRT_SDK_ROOT/bin/<target_arch>/qnn-net-run` 是否存在 |
| `ADSP_LIBRARY_PATH` 未设置导致 DSP 推理失败 | `qnn-net-run` 找不到 hexagon skel 库 | `adb_runner.py` 已自动设置；若手动运行需加 `export ADSP_LIBRARY_PATH=<device_workdir>/<stem>/hexagon-<dsp>/unsigned` |
| `adb push` 超时（大文件 >100 MB） | `--push_timeout` 默认 120s 不足 | 增大 `--push_timeout 300` |
| `/data/local/tmp` 写入权限不足 | 生产设备未开启开发者模式 | 使用 `adb root`（需 userdebug 固件），或换用开发板 |
| `qnn-net-run failed (exit=1)`，stderr 含 `ADSP_LIBRARY_PATH` | hexagon skel 未推送或路径不对 | 确认 `hexagon-<dsp>/unsigned/` 目录存在于 SDK；检查 `--dsp_version` |
| `qnn-net-run failed (exit=1)`，stderr 含 `cannot open shared object` | 运行时库未推送或 `LD_LIBRARY_PATH` 设置有误 | 确认 `--target_arch` 与设备架构一致；重新运行 `adb_runner.py` |

---

## 6. 与 SSH 远程执行的区别

| 对比项 | ADB 部署（`adb_runner.py`） | SSH 远程执行（`RETMOE_DEVICE_INFO`） |
|---|---|---|
| **连接方式** | USB / TCP via `adb` 协议 | TCP via OpenSSH |
| **典型目标设备** | Android 手机 / 嵌入式板（有 ADB daemon） | Linux 开发机 / 嵌入式 Linux（有 sshd） |
| **前置配置** | 设备开启 USB 调试或 ADB TCP 监听 | SSH 密钥 / 用户名密码；`RETMOE_DEVICE_INFO` 文件 |
| **qnn-net-run 推送** | 自动从 QAIRT SDK 推送，板端无需预装 | 板端需预装 QAIRT 环境，或通过 SCP 手动部署 |
| **文件传输** | `adb push` / `adb pull` | `scp` / `rsync` |
| **板端执行** | `adb shell` + 环境变量注入 | `ssh <cmd>` + `source <envsetup.sh>` |
| **plan.md 配置项** | `ADB_DEVICE_ID`、`ADB_TARGET_ARCH`、`ADB_DSP_VERSION` | `RETMOE_DEVICE_INFO` 文件路径 |
| **参考文档** | 本文档 (`adb_execution.md`) | [`remote_execution.md`](remote_execution.md) |

---

## 7. 快速使用示例

### Android 设备（默认）

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

### Linux embedded 设备

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

### 列出已连接设备

```bash
python ${APP_ROOT}/factory/chat_features/model-builder/scripts/adb_runner.py --list_devices
# SERIAL                         STATE
# ----------------------------------------
# 8347dcb1                       device
```

---

## 8. DLC 直接板端推理（可选，精度验证用）

`adb_runner.py` 支持直接将 `.dlc` 推送到板端执行，作为 `.bin` context binary 路径的**可选补充**。

### 两种路径的选择

| 路径 | 格式 | 适用场景 | 前置步骤 |
|------|------|---------|---------|
| **默认推荐** | `.bin` | 性能 benchmark、最终部署 | Step 4 → Step 6（context binary 生成） |
| **可选补充** | `.dlc` | 精度快速验证、量化迭代 | Step 4（或 Step 5），可跳过 Step 6 |

`.bin` 路径：板端加载快（已编译），延迟低，适合性能测量。
`.dlc` 路径：省去 100–300 s 的 context binary 生成，量化参数调整后立即重跑，板端 JIT 编译后执行（首次加载慢，不适合 benchmark）。

### .dlc 命令示例

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

### 板端实际执行的命令（adb_runner.py 自动生成）

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

### DLC 模式额外推送的库

| 库文件 | SDK 来源 | 说明 |
|--------|---------|------|
| `libQnnModelDlc.so` | `$QAIRT_SDK_ROOT/lib/<target_arch>/libQnnModelDlc.so` | DLC 适配层，作为 `--model` 参数传入 `qnn-net-run`；`.bin` 路径不推送此库 |

若 SDK 中不存在 `libQnnModelDlc.so`，打印 `[WARN]` 但不中止（部分旧版 QAIRT SDK 可能不含此文件）。

### 与 .bin 模式对比（参考）

| 对比项 | `.bin` context binary | `.dlc` |
|--------|----------------------|--------|
| `qnn-net-run` 参数 | `--retrieve_context model.bin` | `--model libQnnModelDlc.so --dlc_path model.dlc` |
| 额外库 | 无 | `libQnnModelDlc.so` |
| 板端首次加载 | 快（已编译） | 慢（JIT，约 10–60 s 视模型大小） |
| 推荐场景 | 性能测试、部署 | 精度验证、迭代调参 |
