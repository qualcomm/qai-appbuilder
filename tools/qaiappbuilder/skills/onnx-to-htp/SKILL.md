---
name: onnx-to-htp
description: 在 Windows (WoS ARM64) 上，用 QAIRT SDK 的原生 CLI 工具链（qnn-onnx-converter / qnn-model-lib-generator / qnn-context-binary-generator / qnn-net-run）把任意 ONNX 模型转换并部署到 Qualcomm HTP NPU 本地推理的统一一键流程（合并自 qnn-onnx-to-htp-small 与 qnn-onnx-to-htp-v3，FP32/FP16 自适应降级 + VTCM 超限处理 + 可选量化）。当用户提到 ONNX 转 QNN、qnn-onnx-converter、qnn-net-run、QAIRT/QNN SDK、HTP、Hexagon、WoS、NPU 本地部署、context binary、model lib，或遇到 HTP prepare 报 flat_from_vtcm/VTCM/err 1002、converter 报维度转置、动态 batch 维、net.json 结构差异等问题时使用本 skill。⚠️ 如果这台机器上已经装了 QAIModelBuilder（检查是否存在 `data\config\qairt_env.json`），优先使用更完整的官方 `model-builder` skill（`run_pipeline.py` 一条命令统一处理 FP16/FP32/多种量化精度，向导式排错子 skill齐全）——本 skill 是给**没有 QAIModelBuilder、只有裸 QAIRT SDK** 的机器用的通用底层方案，两者互补不冲突。本 skill 只做 ONNX→NPU 推理这条通用主链路；若模型来源是 mmengine `.pth` 或 `.safetensors`，先用同目录的 `mmengine-ckpt-to-htp` / `safetensors-to-htp` 做导出，再把产出的 ONNX 交给本 skill。
---

# ONNX → QNN → HTP NPU 部署（Windows WoS）— 统一版

> 本 skill 合并自 `qnn-onnx-to-htp-small`（FP32 逐位一致，小模型）与 `qnn-onnx-to-htp-v3`（FP16/VTCM 超限降级、量化、动态维度，大模型/注意力模型均适用）。原两个 skill 仍保留在仓库中作为历史参考，不删改；**新任务请直接用本 skill**——它包含两者的全部经验，并新增了 FP32→FP16 自适应降级逻辑。

## 0. 先判断：这台机器该用哪个 skill？

| 检查 | 结果 | 去哪 |
|---|---|---|
| 存在 `<QAIModelBuilder安装目录>\data\config\qairt_env.json` | 有 | 优先用 `model-builder` skill（`run_pipeline.py`），功能更全、排错子 skill 齐全 |
| 只有裸 QAIRT SDK（`C:\Qualcomm\AIStack\QAIRT\<ver>`），没有 QAIModelBuilder | 是 | **本 skill** |
| 模型来源是 mmengine `.pth` 或 `.safetensors`，还没转成 ONNX | — | 先用 `mmengine-ckpt-to-htp` / `safetensors-to-htp` 导出 ONNX，再回到本 skill |

本 skill 直接操作 QAIRT SDK 原生 CLI 工具（`qnn-onnx-converter` 等），不依赖任何工厂框架，只要有 SDK 解压包就能跑，适合快速验证 / 无 QAIModelBuilder 环境 / 需要精确控制每一步细节的场景。

## 1. 核心流程（六步）

```
ONNX → [转换 qnn-onnx-converter] → 中间表示(.cpp+.bin)
     → [qnn-model-lib-generator] → model lib (.dll, ARM64 + x64)
     → [生成输入 .raw]
     → [qnn-net-run，HTP backend] → NPU 输出 .raw
     → [可选: qnn-context-binary-generator] → .bin（加速加载）
     → [onnxruntime 对比] → cosine/逐位校验 → REPORT.md
```

**精度策略（本 skill 的核心改进点）**：不再让用户先猜 FP32 还是 FP16，脚本自适应：

1. 默认先尝试 **FP32**（转换器不加量化参数）。小模型（权重 ≲10MB，或 VTCM 预算充足）大概率直接成功，且 NPU 输出与 `onnxruntime` **逐位一致**（`qnn-onnx-to-htp-small` 的核心价值）。
2. 若 `qnn-net-run`/prepare 阶段报 **`flat_from_vtcm` / VTCM / err 1002**（VTCM 预算不够放 FP32 权重+激活，常见于大模型/注意力模型）→ 自动改用 **FP16** 转换重试（`--float_bitwidth 16`），体积减半，多数情况能过（`qnn-onnx-to-htp-v3` 的核心经验）。FP16 校验阈值降为 cosine ≥0.99（不再是逐位一致，属预期精度损失）。
3. 用户显式要求量化（INT8/A16W8 等）→ 走量化分支（`--input_list`/calibration，见 §5），校验阈值 cosine ≥0.95。

## 2. 环境事实（先探测，不要假设）

> 下表是历史实测参考值，**每次用之前重新探测**（架构/VTCM/路径可能因机器而变，这是坑表里最容易踩的坑）。

| 项 | 历史参考值（不同机器可能不同） |
|---|---|
| 宿主 | Windows on Snapdragon ARM64（WoS），HTP V81 |
| QAIRT SDK 根 | `C:\Qualcomm\AIStack\QAIRT\<version>` |
| VTCM 预算 | 实测见过 4 MB（`netrun.log` 会打印 `VTCM: total_sz=...`），预算小的机器 FP32 大模型几乎必降级 FP16 |
| 转换器 Python（x64 3.10/3.12） | 需要 `onnx`/`onnxruntime`/`numpy`，转换器是纯 python 脚本，必须显式设 `PYTHONPATH=$QAIRT_SDK_ROOT\lib\python` |
| Model lib generator / context binary generator | 在 `aarch64-windows-msvc/` 或 `arm64x-windows-msvc/`（因 SDK 版本而异，脚本里做了自动探测） |
| Cmake / 编译器 | `qnn-model-lib-generator` 内部要编译 .dll，需要 PATH 里有 `cmake` + MSVC（`cl.exe`）或 clang-cl；`env_check.ps1` 会先探测 |

**先跑 `scripts/env_check.ps1`**（合并自两个旧 skill 的探测脚本），一次性把 QAIRT 路径、Python 路径、cmake、VS 编译器、架构目录布局全部探测清楚并打印，**避免"环境发现"变成实际瓶颈**（这是 `qnn-onnx-to-htp-small/LESSONS.md` 里记录的最大教训：脚本本身跑起来很快，真正费时间的是找环境）。

## 3. 一键运行

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_qnn_deploy.ps1 `
    -Onnx C:\work\model.onnx `
    -WorkDir C:\work\model_workdir `
    [-QairtRoot C:\Qualcomm\AIStack\QAIRT\2.48.40.260702] `
    [-Python C:\path\to\python.exe] `
    [-Precision auto|fp32|fp16] `           # 默认 auto：先试 fp32，VTCM 报错自动降 fp16
    [-Quantize int8|a16w8] [-CalibList calib_list.txt] `   # 可选：量化
    [-InputList input_list.txt] `           # 可选：指定输入（默认随机生成匹配 shape 的输入）
    [-SkipContextBinary]                    # 可选：跳过生成 .bin（只到 model lib 这步）
```

成功时输出：
- FP32 路径：`PASS: NPU output BIT-EXACT with onnxruntime reference` + `SUCCESS`
- FP16/量化路径：`PASS: NPU output cosine=<x> >= threshold` + `SUCCESS`
- 失败（含自动降级后仍失败）：打印诊断信息 + exit code 非 0，**不会静默返回一个"看起来正常"但实际不对的结果**

换模型只需换 `-Onnx`/`-WorkDir`（`WorkDir` 建议用新目录，避免旧产物干扰）。

## 4. 六步细节（脚本已自动处理，此处供排错时对照）

### Step 0 — 架构检测
探测宿主是 ARM64 还是 x64、QAIRT SDK 版本目录布局（`aarch64-windows-msvc` vs `arm64x-windows-msvc` 命名在不同版本不一样）。**转换器和 model-lib-generator 常在不同的架构子目录**——不要假设都在一处。

### Step 1 — ONNX → QNN 中间表示
```powershell
$env:PYTHONPATH = "$QairtRoot\lib\python"
& $Python "$QairtRoot\bin\<arch>\qnn-onnx-converter" -i model.onnx -o "$WorkDir\model" [--float_bitwidth 16]
```
产出 `model`（无扩展名，实为 `.cpp` 源码）+ `model.bin`（权重 raw）。**输出参数是不带扩展名的前缀**，容易误以为出错。

**转换器可能转置输入/输出维度**（坑，`qnn-onnx-to-htp-v3` 实测：ONNX `[1,1152,768]` → QNN `[1,768,1152]`，`permute_order_to_src: [0,2,1]`）——生成输入数据、解析输出时必须按转换后的实际 shape 来，不能想当然按原 ONNX shape。

### Step 2 — Model lib（.dll）
```powershell
Copy-Item "$WorkDir\model" "$WorkDir\model.cpp" -Force   # generator 要求 .cpp 扩展名
& $Python "$QairtRoot\bin\<arch>\qnn-model-lib-generator" -c "$WorkDir\model.cpp" -b "$WorkDir\model.bin" -o "$WorkDir\mlib"
```
产出 `mlib\ARM64\model.dll` +（如需要 x64 侧调试）`mlib\x64\model.dll`。**必须用 x64 工具链跑 `qnn-net-run`**（下一步）——ARM64 工具链在 prepare 阶段会崩溃，这是两个旧 skill 都踩过的必踩坑，脚本已经固定走 x64。

### Step 3 — 生成输入 .raw
按转换后的实际输入 shape（Step 1 探测到的，不是原 ONNX 声明的）生成 float32 `.raw` 文件。**动态维度**（如 batch 维为 `-1`/`?`）需要显式指定具体值，脚本通过 `-InputList`/`--input_dim`-等价参数处理，不能留空跑。

### Step 4 — NPU 推理
```powershell
& "$QairtRoot\bin\x86_64-windows-msvc\qnn-net-run.exe" `
    --model "$WorkDir\mlib\x64\model.dll" `
    --backend "$QairtRoot\lib\x86_64-windows-msvc\QnnHtp.dll" `
    --input_list input_list.txt --output_dir "$WorkDir\out_npu"
```
若这一步报 **`flat_from_vtcm` / VTCM / err 1002**（VTCM 预算不足）→ 触发自动降级重跑 Step 1（FP16）。

**排错口诀**：先跑 CPU 后端（`--backend ...QnnCpu.dll`）做 sanity check。若 CPU 后端也跟参考不符 → 问题在**转换环节**（维度/布局），不在 NPU 本身；若只有 HTP 后端不符 → 是 NPU 特有的问题（精度/VTCM/算子实现差异）。

### Step 5 — 可选 context binary（加速加载）
```powershell
& "$QairtRoot\bin\x86_64-windows-msvc\qnn-context-binary-generator.exe" `
    --model "$WorkDir\mlib\x64\model.dll" --backend "...\QnnHtp.dll" --binary_file "$WorkDir\model.bin"
```
⚠️ 生成器会**自动追加 `.bin` 后缀**——传入 `model.bin` 实际产出 `model.bin.bin`，脚本已处理改名，手工排错时注意。ctx-run 与 dll-run 输出应逐位一致（同精度下）。

### Step 6 — 校验 + 报告
```python
import numpy as np
a = np.fromfile("out_npu/Result_0/<out>.raw", dtype=np.float32).flatten()
b = np.fromfile("ref_output.raw", dtype=np.float32).flatten()   # onnxruntime CPUExecutionProvider 参考输出
cosine = np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))
max_abs_diff = np.max(np.abs(a - b))
```
**阈值**：FP32 逐位一致（`max_abs_diff` 应为 0 或机器精度级别的微小值）；FP16 cosine ≥0.99；INT8/量化 cosine ≥0.95。不过阈值直接报失败，**不允许"看起来差不多就算了"**。写 `REPORT.md` 记录精度分支（fp32/fp16/量化）、VTCM 情况、cosine/逐位结果。

## 5. 可选：量化

若模型太大/追求更快推理速度，可用 `-Quantize int8` 或 `-Quantize a16w8` + `-CalibList <calib_list.txt>`：
- `qnn-onnx-converter` 加 `--input_list <calib_list.txt> --quantization_overrides ...`（具体参数版本间有差异，脚本按探测到的 SDK 版本适配）。
- 校准数据要求：**真实多样本**（覆盖不同类别/场景），不要用单张图片重复凑数——这条规则和 `model-builder` 的量化规范一致。
- 量化后校验阈值 cosine ≥0.95，不达标先检查校准数据多样性，再考虑 per-channel/CLE 等手段（若本 skill 的量化能力不够用，改用 `model-builder`——它的 `run_pipeline.py --precision w8a8 --cle --per_channel` 等选项更完整）。

## 6. 坑表（症状 → 原因 → 修法）

| # | 症状 | 原因 | 修法 |
|---|---|---|---|
| 1 | ARM64 工具链跑 `qnn-net-run` 在 prepare 阶段崩溃/挂起 | ARM64 native 工具链对 HTP prepare 支持不完整 | 固定用 x64 工具链（`bin\x86_64-windows-msvc\`），脚本已内置 |
| 2 | 转换器把输入/输出维度转置了，和原 ONNX shape 不一致 | `qnn-onnx-converter` 按内部布局做了 permute | 用 Step1 转换后打印的实际 shape 生成输入/解析输出，不要用原 ONNX shape 硬编码 |
| 3 | prepare 阶段报 `No constructible op for ... 'q::flat_from_vtcm'` / `finalize err 1002` | VTCM 预算不够放 FP32 权重+激活 | 自动降级 FP16（脚本 `-Precision auto` 默认行为），体积减半通常能过 |
| 4 | 动态维度（batch=-1 等）导致转换或 net-run 报错 | 转换器/net-run 需要具体数值才能分配 buffer | 显式指定具体维度值（`-InputList`/`--input_dim` 等价参数），不要留通配 |
| 5 | 生成的 context binary 文件名不对（多了一层 `.bin`） | 生成器自动追加 `.bin` 后缀 | `X.bin` 实际产出 `X.bin.bin`，脚本已处理改名，手工排错时注意 |
| 6 | 脚本里 `powershell -Command` 的 `$env:` 被吃掉 / 子进程报 `WinError 6`（invalid stdin handle） | 外层 shell 展开了 `$`；harness 环境下子进程 stdin 句柄无效 | 用 `.ps1` + `-File` 调用（不要 `-Command`）；Python `subprocess` 加 `stdin=subprocess.DEVNULL` |
| 7 | `netrun.log` 打开是乱码 | 该日志是 UTF-16LE 编码 | 读取时显式指定 `encoding="utf-16-le"` |
| 8 | `net.json`（转换产物的图描述）里有的张量带 `is_graph_input` 字段，有的模型没有 | schema 因模型/SDK 版本而异（如 Inception v3 实测没有该字段） | 稳健识别图输入的方法：`produced = {o for n in nodes for o in n["output_tensors"]}`，再取不在 `produced` 里的张量作为输入，不要死认 `is_graph_input` 字段 |
| 9 | "环境发现"（找 Python/cmake/VS/QAIRT 路径）比实际转换脚本跑得还久 | 路径分散在好几个地方，且因机器/SDK版本而异 | 先跑 `env_check.ps1` 一次性探测清楚，别一个个手动试 |
| 10 | model-lib-generator 编译 .dll 失败，报找不到 cmake/编译器 | PATH 没有 cmake，或 MSVC/clang-cl 没被发现 | `env_check.ps1` 会显式探测并报告；缺失时提示用户装或指定 `-Cmake`/`-VsRoot` |

## 7. 附带脚本

- `scripts/env_check.ps1` — 环境探测（QAIRT 路径、Python、cmake、编译器、架构目录布局），合并自两个旧 skill 的探测脚本
- `scripts/run_qnn_deploy.ps1` — 六步一键流程主脚本，含 FP32→FP16 自适应降级、可选量化、校验、`REPORT.md` 生成
- `scripts/gen_ctx.ps1` / `scripts/gen_mlib.ps1` — 单独跑 Step2/Step5 的辅助脚本（排错时可单步调用，不必每次跑完整六步）
- `scripts/compare_output.py` — Step6 的 cosine/逐位校验小工具，独立于主脚本可单独调用

## 8. 与其它 skill 的关系

```
mmengine-ckpt-to-htp ─┐
safetensors-to-htp   ─┼─→ 产出 FP32 ONNX ─→  本 skill (onnx-to-htp)  或  model-builder skill
（自动探测+导出前端）  ┘                        (裸 QAIRT CLI 工具链)    (QAIModelBuilder 工厂框架，功能更全)

mmyolo-ckpt-to-htp / realesrgan-safetensors-to-htp
（YOLO-World / Real-ESRGAN 专项，端到端已验证，不经过本 skill 的通用路径）
```
