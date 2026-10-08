---
name: safetensors-to-htp
description: 在 Windows (WoS ARM64) 上把一个 .safetensors 权重文件（Hugging Face / diffusers / 通用 safetensors 序列化格式的模型权重）自动探测张量结构、生成/填写 ONNX 导出脚本，产出经过 cosine 校验的 FP32 ONNX，然后移交给 model-builder skill 完成 QNN 转换与 HTP 推理验证。当用户提到 .safetensors 模型转 QNN/HTP、不知道某个 .safetensors 是什么模型/怎么转、Hugging Face 权重转 NPU 推理时使用本 skill。⚠️ 若探测结果是 Real-ESRGAN x4plus / RRDBNet（键名含 body.*.rdb1/rdb2/rdb3、conv_first、conv_up1/conv_up2），优先用已验证的 `realesrgan-safetensors-to-htp` skill（同目录，专项且已跑通全链路，含 basicsr 绕过、pre_pad 陷阱等专属知识）。本 skill 是没有专项 skill 覆盖时的通用前端。本 skill 只做 Step 0（探测）+ Step 1（导出 ONNX），Step 2 起（Inspect ONNX IO、算子 patch、run_pipeline.py 转换、量化、context binary、HTP 推理、验证报告）一律移交 `model-builder` skill，不重复实现。
---

# .safetensors → ONNX → 移交 model-builder → QNN HTP

## 0. 本 skill 的边界（先读）

这是一个**薄前端 skill**，只覆盖 8 步核心流程（见 `model-builder/SKILL.md`）里的：

- **Step 0（本 skill 新增，不在 model-builder 里）**：探测 `.safetensors` 的张量列表、键名前缀、shape、dtype，猜测模型家族。
- **Step 1（导出 ONNX）**：按 `model-builder/references/model_export_validation.md` 的规范生成并跑通导出脚本，产出 FP32 ONNX + cosine 校验。

**Step 2 及以后（Inspect ONNX IO、算子 patch、`run_pipeline.py` 转换、量化、context binary、HTP 推理、`REPORT.md`）一律移交 `model-builder` skill**——不要在这里重新实现，直接在 §4 按提示切过去。

判定优先级：
1. 若探测出是 **Real-ESRGAN x4plus / RRDBNet**（键名含 `body.*.rdb1`/`body.*.rdb2`/`body.*.rdb3`、`conv_first`、`conv_up1`/`conv_up2`、`conv_hr`、`conv_last`）→ 停下，改用 `realesrgan-safetensors-to-htp` skill（路径 `<skills_jygpu>/realesrgan-safetensors-to-htp/`，与本 skill 所在的 `<skills_jygpu>/skills/` 是同一仓库的上一级目录；已端到端验证，含 basicsr 依赖绕过、pre_pad 陷阱等专属知识）。
2. 其他 `.safetensors` 模型 → 继续本 skill。

## 1. 何时触发

- 用户给了一个 `.safetensors` 文件，不确定是什么模型、要不要转 QNN、怎么转。
- 常见来源：Hugging Face Hub 上的模型权重、diffusers 系列（UNet/VAE/text encoder 拆分保存）、单文件保存的 CV 模型权重。
- `.safetensors` 本身只是一个"张量名 → 张量"的容器（无计算图、无 Python 类信息），**必须知道对应的模型类定义**才能重建 `nn.Module` 并加载权重——这是本 skill Step 0 要解决的核心问题。

**不适用**：文件根本打不开 / 不是合法 safetensors 格式（探测脚本会直接报错）；此时确认文件本身有没有问题。

## 2. glymur 机器环境（2026-09-21 探测确认）

> 权威来源始终是 `${APP_ROOT}\data\config\qairt_env.json`（`APP_ROOT` = QAIModelBuilder 安装目录）。下表是在 glymur（`WIN-NGP1M14RV2A`，Snapdragon X2 Elite，HTP v81）上探测到的当前值，**每次用之前用 `Get-Content qairt_env.json` 核对一次，路径变了以 json 为准**。

| 项 | 值 |
|---|---|
| 主机 | `WIN-NGP1M14RV2A`，ARM64（WoS），Snapdragon X2 Elite，HTP **v81** |
| `APP_ROOT`（QAIModelBuilder） | `C:\Users\HCKTest\Downloads\0831\QAIModelBuilder_v3.1.18` |
| `qairt_sdk_root` | `C:\Qualcomm\AIStack\QAIRT\2.48.40.260702` |
| `python_x64_venv`（**探测 + 导出 ONNX 都用这个**） | `C:\Users\HCKTest\AppData\Local\QAIModelBuilder\envs\.venv_x64_310`（Python 3.10.21；已装 torch 2.14/onnx 1.19/onnxruntime 1.23/numpy/**safetensors 0.8.0**/transformers 4.36/timm/ultralytics） |
| `python_runtime_venv`（推理用，本 skill 不直接用） | `.venv_arm64_313`（Python 3.13.15；也已装 safetensors 0.8.0） |
| 工作区根 `${WORKSPACE}` | 默认 `C:\WoS_AI`（以系统提示里的 Working Directory 为准，不要硬编码） |
| `model-builder` SKILL.md | `${APP_ROOT}\factory\chat_features\model-builder\SKILL.md` |

**好消息**：`safetensors` 库和 `transformers`/`timm` 在 `python_x64_venv` 里都已经装好，大部分 Hugging Face 系列模型的探测和导出不需要额外装包。真缺包时按 model-builder 的 B2 规则：停下、说明包名和原因、征得同意后才装。

## 3. 执行步骤

### Step 0 — 探测 safetensors 结构

```powershell
$x64py = "C:\Users\HCKTest\AppData\Local\QAIModelBuilder\envs\.venv_x64_310\Scripts\python.exe"
& $x64py "<本skill目录>\scripts\probe_safetensors.py" "<权重路径>.safetensors" --full
```

脚本输出：
- 文件头（`metadata`，有时含 `format`/`modelspec.*` 等线索，如果是 diffusers/civitai 导出的会有更多提示）
- 张量列表：名字、shape、dtype，总参数量
- 键名前缀直方图（如 `conv_first : 1`、`body.0.rdb1.* : 5`）
- 启发式家族猜测（Real-ESRGAN/RRDBNet、ResNet 系列、Transformer 系列、diffusers UNet 等）
- 当前 venv 里 `transformers`/`diffusers`/`timm`/`safetensors` 是否装好

**猜出是 Real-ESRGAN x4plus / RRDBNet** → 停，改用 `realesrgan-safetensors-to-htp` skill（§0）。

### Step 1 — 生成导出脚本，产出 FP32 ONNX

拷贝 `scripts/export_onnx_template.py` 到工作区，按 Step 0 探测到的键名前缀和家族猜测，**填写 `build_model()`**（唯一必须动的函数）：

- 若键名前缀能对应到一个 **`transformers`/`timm` 里现成的类**（如 `resnet.*`、`vit.*` 等常见命名），优先尝试直接用库里的类 + `from_pretrained`/`load_state_dict`，成本最低。
- 若是自定义/小众结构（如手搓的 CNN、GAN 生成器），需要手写一个 `nn.Module`，按键名对齐子模块，参考 `realesrgan-safetensors-to-htp/scripts/` 里 RRDBNet 手搓的写法思路（只读，不要照抄结构——那是 Real-ESRGAN 专属的）。
- 加载后必须用 `strict=False` 检查 `missing`/`unexpected` 都为空（或只有可忽略的 buffer），再继续。
- 训练专用分支（dropout、aux head）一律 `model.eval()` + 关掉。

导出规则（`export_onnx_template.py` 已经内置，不用改，与 `mmengine-ckpt-to-htp` 用的是同一套规范）：
- **FP32 only**（永不 FP16），`opset_version=18`，`do_constant_folding=False`
- 导出后自动跑 PyTorch vs ONNX（onnxruntime CPUExecutionProvider）cosine 校验，**阈值 ≥0.9999**，不过阈值直接报错退出，不允许悄悄跳过

```powershell
$x64py = "C:\Users\HCKTest\AppData\Local\QAIModelBuilder\envs\.venv_x64_310\Scripts\python.exe"
& $x64py "<工作区>\export_onnx.py" `
    --weights "<权重路径>.safetensors" `
    --out     "C:\WoS_AI\<model_name>\<model_name>.onnx" `
    --input_shape 1,3,512,512
```

期望输出末尾：`PyTorch vs ONNX cosine: 0.99999xxx` + `EXPORT_OK`。

> 若导出遇到算子相关报错（如 `GridSample`），别在本 skill 里现场发明修法——那是 model-builder 的 `operator-patching` sub-skill（Step 2 之后）的责任范围。`basicsr`/`functional_tensor` 这类导入报错属于 model-builder 的 `export-troubleshooting` sub-skill（`${APP_ROOT}\factory\chat_features\model-builder\troubleshooting\export-troubleshooting\SKILL.md`）。

### Step 2 — 初始化 model-builder 工作区并移交

```powershell
$x64py = "C:\Users\HCKTest\AppData\Local\QAIModelBuilder\envs\.venv_x64_310\Scripts\python.exe"
& $x64py "<APP_ROOT>\factory\chat_features\model-builder\scripts\qai_workspace_init.py" <model_name> --wos-ai-root C:\WoS_AI
```

把 Step 1 产出的 `<model_name>.onnx`（+ 可能的 `.onnx.data`，大权重模型 torch 2.x 会拆成两个文件，必须放在同一目录）放进 `C:\WoS_AI\<model_name>\`（`qai_workspace_init.py` 已建好目录骨架和 `plan.md`）。

**然后明确移交**：完整读一遍 `${APP_ROOT}\factory\chat_features\model-builder\SKILL.md`，从 **Core Workflow Step 2**（`qai_inspect_onnxio.py`）开始接着做，Boundary Decision 的 Q3 答案是"是，有自定义 ONNX/PyTorch 模型要转换"。**不要**把本 skill 的路径/脚本带过去（本 skill 的产出只是一个标准 FP32 ONNX 文件，model-builder 不需要知道它是怎么导出的）。

> ⚠️ **这机器没装 QAIModelBuilder 怎么办？** 检查 `<QAIModelBuilder安装目录>\\data\\config\\qairt_env.json` 是否存在——不存在则说明这台机器只有裸 QAIRT SDK。这时改用同目录的 `onnx-to-htp` skill（路径 `skills_jygpu/skills/onnx-to-htp/`）完成 Step2 及以后的转换/部署：直接把本 skill 导出的 FP32 ONNX 传给 `onnx-to-htp/scripts/run_qnn_deploy.ps1 -Onnx <导出的.onnx>`，它会自适应 FP32/FP16 并完成 QNN 转换 + HTP 推理验证。

## 4. 坑清单

| # | 现象 | 原因 | 处理 |
|---|---|---|---|
| 1 | `safetensors.torch.load_file()` 报错/乱码 | 文件损坏，或其实是别的格式改了后缀 | 用 `probe_safetensors.py` 打印文件头，若连 header 都解不出来，跟用户确认文件来源 |
| 2 | 键名前缀猜不出对应哪个库的类 | 自定义结构，或权重来自某个不常见仓库 | 看键名里是否有能搜到的独特字符串（如某篇论文的模块名），或问用户"这个 safetensors 来自哪个 repo/项目" |
| 3 | 只有权重、没有对应的模型定义代码 | safetensors 格式本身不含计算图 | 这是本质限制——必须让用户提供模型定义（论文代码/HF repo 里的 modeling 文件），或用 `transformers`/`timm` 里的现成类去凑，无法凭空由权重反推结构 |
| 4 | `load_state_dict(strict=False)` 后 `missing`/`unexpected` 很多 | 手写的 `nn.Module` 和权重键名不匹配（大小写、层编号偏移等） | 别继续往下走；核对键名前缀直方图，逐层对齐 |
| 5 | cosine 卡在 0.999x，到不了 0.9999 | `build_model()` 某层写错（常见于 padding/stride/激活函数不一致，或权重加载时 transpose 方向搞反） | 停，回去对齐层定义；这类误差到了 QNN 侧只会更大 |
| 6 | diffusers 模型（UNet/VAE/text encoder）分开保存成多个 `.safetensors` | diffusers 惯例，一个完整 pipeline 拆成好几个子模型 | 每个子模型分别走一次本 skill 的 Step 0/1（各自导出一个 ONNX），后续在 model-builder 侧也是分别转换；不要试图一次性揉在一个 ONNX 里 |

## 5. 附带脚本

- `scripts/probe_safetensors.py` — Step 0 探测，见 §3
- `scripts/export_onnx_template.py` — Step 1 导出脚本骨架，`build_model()` 需要按模型填写，其余部分（导出参数、cosine 校验）已经写好不用改，与 `mmengine-ckpt-to-htp` 的模板逻辑一致（分别维护，避免互相影响）
- `scripts/validate_cosine.py` — 通用 cosine/PSNR 比对小工具（两个 `.npy` 文件），排错时可用
