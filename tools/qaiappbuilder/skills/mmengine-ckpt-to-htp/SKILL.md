---
name: mmengine-ckpt-to-htp
description: 在 Windows (WoS ARM64) 上把一个 mmengine 格式的 .pth checkpoint（顶层为 {"meta":..., "state_dict":...} 的字典，常见于 mmdetection/mmyolo/mmsegmentation/mmpretrain 等 OpenMMLab 框架训练产物）自动探测结构、生成/填写 ONNX 导出脚本，产出经过 cosine 校验的 FP32 ONNX，然后移交给 model-builder skill 完成 QNN 转换与 HTP 推理验证。当用户提到 mmengine checkpoint、.pth 里有 meta+state_dict、OpenMMLab 模型转换、不知道某个 .pth 是什么格式/怎么转 QNN 时使用本 skill。⚠️ 若探测结果是 YOLO-World V2.1 (backbone.text_model.* + bbox_head.*)，优先用已验证的 `mmyolo-ckpt-to-htp` skill（同目录，专项且已跑通全链路）；本 skill 是没有专项 skill 覆盖时的通用前端。本 skill 只做 Step 0（探测）+ Step 1（导出 ONNX），Step 2 起（Inspect ONNX IO、算子 patch、run_pipeline.py 转换、量化、context binary、HTP 推理、验证报告）一律移交 `model-builder` skill，不重复实现。
---

# mmengine checkpoint (.pth) → ONNX → 移交 model-builder → QNN HTP

## 0. 本 skill 的边界（先读）

这是一个**薄前端 skill**，只覆盖 8 步核心流程（见 `model-builder/SKILL.md`）里的：

- **Step 0（本 skill 新增，不在 model-builder 里）**：探测 `.pth` 是否为 mmengine 格式、拿到 key 结构、猜测模型家族。
- **Step 1（导出 ONNX）**：按 `model-builder/references/model_export_validation.md` 的规范生成并跑通导出脚本，产出 FP32 ONNX + cosine 校验。

**Step 2 及以后（Inspect ONNX IO、算子 patch、`run_pipeline.py` 转换、量化、context binary、HTP 推理、`REPORT.md`）一律移交 `model-builder` skill**——不要在这里重新实现，直接在 §4 按提示切过去。

判定优先级：
1. 若探测出是 **YOLO-World V2.1**（键名含 `backbone.text_model.*` 且 `bbox_head.*`）→ 停下，改用 `mmyolo-ckpt-to-htp` skill（路径 `<skills_jygpu>/mmyolo-ckpt-to-htp/`，与本 skill 所在的 `<skills_jygpu>/skills/` 是同一仓库的上一级目录；已端到端验证，含 CLIP bake、einsum patch 等专属知识）。
2. 其他 mmengine 格式模型 → 继续本 skill。

## 1. 何时触发

- 用户给了一个 `.pth` 文件，`torch.load()` 后顶层是 `dict`，含 `meta` 和 `state_dict` 两个键（mmengine 的标准 checkpoint 格式）。
- 用户不确定这个 `.pth` 是什么格式、要不要转 QNN、怎么转。
- 常见来源：mmdetection / mmyolo / mmsegmentation / mmpretrain / mmagic 等 OpenMMLab 系列框架训练输出。

**不适用**：`.pth` 里直接是扁平 `state_dict`（没有 `meta` 键）、或是 `torch.save(model)` 存的整个模型对象——这些不是 mmengine 格式，探测脚本会报出来，此时按普通 PyTorch 模型手写导出脚本，仍可参考本 skill §3 的导出规范。

## 2. glymur 机器环境（2026-09-21 探测确认）

> 权威来源始终是 `${APP_ROOT}\data\config\qairt_env.json`（`APP_ROOT` = QAIModelBuilder 安装目录）。下表是在 glymur（`WIN-NGP1M14RV2A`，Snapdragon X2 Elite，HTP v81）上探测到的当前值，**每次用之前用 `Get-Content qairt_env.json` 核对一次，路径变了以 json 为准**。

| 项 | 值 |
|---|---|
| 主机 | `WIN-NGP1M14RV2A`，ARM64（WoS），Snapdragon X2 Elite，HTP **v81** |
| `APP_ROOT`（QAIModelBuilder） | `C:\Users\HCKTest\Downloads\0831\QAIModelBuilder_v3.1.18` |
| `qairt_sdk_root` | `C:\Qualcomm\AIStack\QAIRT\2.48.40.260702` |
| `python_x64_venv`（**导出 ONNX 用这个**） | `C:\Users\HCKTest\AppData\Local\QAIModelBuilder\envs\.venv_x64_310`（Python 3.10.21；已装 torch 2.14/onnx 1.19/onnxruntime 1.23/numpy/transformers/safetensors/**mmcv/mmdet/mmengine/mmyolo/ultralytics/timm**） |
| `python_runtime_venv`（推理用，本 skill 不直接用） | `.venv_arm64_313`（Python 3.13.15） |
| 工作区根 `${WORKSPACE}` | 默认 `C:\WoS_AI`（以系统提示里的 Working Directory 为准，不要硬编码） |
| `model-builder` SKILL.md | `${APP_ROOT}\factory\chat_features\model-builder\SKILL.md` |

**好消息**：glymur 的 `python_x64_venv` 已经装好了 `mmcv`/`mmdet`/`mmengine`/`mmyolo`/`ultralytics`/`timm`/`transformers`，大多数 OpenMMLab 系列模型的导出**不需要额外装包**（B2 不太会触发）。真缺包时按 model-builder 的 B2 规则：停下、说明包名和原因、征得同意后才装。

## 3. 执行步骤

### Step 0 — 探测 checkpoint 结构

```powershell
$x64py = "C:\Users\HCKTest\AppData\Local\QAIModelBuilder\envs\.venv_x64_310\Scripts\python.exe"
& $x64py "<本skill目录>\scripts\probe_mmengine_ckpt.py" "<ckpt路径>.pth" --full
```

脚本输出：
- 是否为 mmengine 格式（`meta` + `state_dict` 都在）
- `meta` 内容（常含 `CLASSES`、训练配置摘要等，对确定输入/输出契约很有用）
- key 前缀直方图（如 `backbone.* : 156`、`bbox_head.* : 42`）+ 总参数量 + dtype 分布
- 启发式家族猜测（检测器/分割器/SR 网络/YOLO-World 等）
- 当前 venv 里 `mmengine`/`mmcv`/`mmdet`/`mmyolo`/`ultralytics`/`timm`/`transformers` 是否装好

**不是 mmengine 格式** → 脚本会明确报出来（没有 `meta`+`state_dict` 组合）；此时确认这不是本 skill 的适用范围，转而按普通 PyTorch checkpoint 处理（跳到 §3 Step1 手写导出脚本仍适用）。

**猜出是 YOLO-World V2.1** → 停，改用 `mmyolo-ckpt-to-htp` skill（§0）。

### Step 1 — 生成导出脚本，产出 FP32 ONNX

拷贝 `scripts/export_onnx_template.py` 到工作区，按 Step 0 探测到的 key 前缀和家族猜测，**填写 `build_model()`**（唯一必须动的函数）：

- 根据 key 前缀确定用哪个框架/类：
  - `backbone.*`/`neck.*`/`bbox_head.*` + venv 里有 `mmdet`/`mmyolo` → 可尝试框架自带的 `build_detector`（需要对应 config，往往还要再拉一份仓库代码，成本高），**但更推荐**：手写一个纯 PyTorch 的 `nn.Module`，按 key 名对齐（`load_state_dict(strict=False)` 后检查 `missing=0 unexpected=0`），无需装 `mmcv`/编译依赖——`mmyolo-ckpt-to-htp` skill 里 YOLO-World 的做法就是这个思路，可以直接读那个 skill 的 `scripts/export_mmyolo_onnx.py` 作为参考写法（只读，不要改它）。
  - `decode_head.*` → mmsegmentation 风格分割器，同理。
  - 其它前缀 → 按 Step 0 的 key 直方图和 `meta` 内容手动判断。
- 若模型有训练专用分支（aux head、dropout）—— 一律 `model.eval()` + 关掉（`model.aux_logits=False` 等模式），避免导出出现无法 resolve 的动态 shape（`ReshapeOp::calculateShape` 报错），这条规则和 model-builder 的 `export-troubleshooting` sub-skill 一致。

导出规则（`export_onnx_template.py` 已经内置，不用改）：
- **FP32 only**（永不 FP16），`opset_version=18`，`do_constant_folding=False`
- 导出后自动跑 PyTorch vs ONNX（onnxruntime CPUExecutionProvider）cosine 校验，**阈值 ≥0.9999**，不过阈值直接报错退出，不允许悄悄跳过

```powershell
$x64py = "C:\Users\HCKTest\AppData\Local\QAIModelBuilder\envs\.venv_x64_310\Scripts\python.exe"
& $x64py "<工作区>\export_onnx.py" `
    --ckpt "<ckpt路径>.pth" `
    --out  "C:\WoS_AI\<model_name>\<model_name>.onnx" `
    --input_shape 1,3,640,640
```

期望输出末尾：`PyTorch vs ONNX cosine: 0.99999xxx` + `EXPORT_OK`。

> 若导出遇到 `basicsr`/`functional_tensor` 或算子相关报错，别在本 skill 里现场发明修法——那是 model-builder 的 `export-troubleshooting` sub-skill 的责任范围（`${APP_ROOT}\factory\chat_features\model-builder\troubleshooting\export-troubleshooting\SKILL.md`），照它的坑表处理。

### Step 2 — 初始化 model-builder 工作区并移交

```powershell
$x64py = "C:\Users\HCKTest\AppData\Local\QAIModelBuilder\envs\.venv_x64_310\Scripts\python.exe"
& $x64py "<APP_ROOT>\factory\chat_features\model-builder\scripts\qai_workspace_init.py" <model_name> --wos-ai-root C:\WoS_AI
```

把 Step 1 产出的 `<model_name>.onnx`（+ 可能的 `.onnx.data`）放进 `C:\WoS_AI\<model_name>\`（`qai_workspace_init.py` 已建好目录骨架和 `plan.md`）。

**然后明确移交**：完整读一遍 `${APP_ROOT}\factory\chat_features\model-builder\SKILL.md`，从 **Core Workflow Step 2**（`qai_inspect_onnxio.py`）开始接着做，Boundary Decision 的 Q3 答案是"是，有自定义 ONNX/PyTorch 模型要转换"。**不要**把本 skill 的路径/脚本带过去（本 skill 的产出只是一个标准 FP32 ONNX 文件，model-builder 不需要知道它是怎么导出的）。

> ⚠️ **这机器没装 QAIModelBuilder 怎么办？** 检查 `<QAIModelBuilder安装目录>\\data\\config\\qairt_env.json` 是否存在——不存在则说明这台机器只有裸 QAIRT SDK。这时改用同目录的 `onnx-to-htp` skill（路径 `skills_jygpu/skills/onnx-to-htp/`）完成 Step2 及以后的转换/部署：直接把本 skill 导出的 FP32 ONNX 传给 `onnx-to-htp/scripts/run_qnn_deploy.ps1 -Onnx <导出的.onnx>`，它会自适应 FP32/FP16 并完成 QNN 转换 + HTP 推理验证。

## 4. 坑清单

| # | 现象 | 原因 | 处理 |
|---|---|---|---|
| 1 | `torch.load()` 报 `weights_only` 相关警告/报错（新版 torch 默认 `weights_only=True`） | mmengine ckpt 里 `meta` 常含非 tensor 对象（dict/list/字符串），新版 torch 默认的安全模式会拒绝 | probe/export 脚本已显式传 `weights_only=False`（本 checkpoint 来源可信时才这样做） |
| 2 | key 前缀猜不出家族，`build_model()` 无从下手 | 自定义/小众 OpenMMLab 衍生框架 | 用 `--full` 看全部 key，按人类可读的模块名（`backbone`/`neck`/`head` 常见叫法）反推对应的官方类；找不到就问用户"这个 ckpt 来自哪个训练仓库/config" |
| 3 | `load_state_dict(strict=False)` 后 `missing`/`unexpected` 很多 | 手写的 `nn.Module` 结构和 checkpoint 不匹配 | 别继续往下走，说明这是结构对不上，不是数值问题；重新核对 key 前缀和子模块命名 |
| 4 | 导出报 `Einsum`/`GridSample`/`ScatterND` 等不支持算子 | 是 **QNN 转换**的问题，不是 ONNX 导出的问题 | 这是 Step 2 之后的事，不在本 skill 处理——移交后让 model-builder 的 `operator-patching` sub-skill 处理 |
| 5 | cosine 卡在 0.999x，到不了 0.9999 | 通常是 `build_model()` 里某个层写错（如 stride/padding/激活函数不一致） | 停，别用"能用就行"的心态放过，回去对齐层定义；这类误差到了 QNN 侧只会更大 |

## 5. 附带脚本

- `scripts/probe_mmengine_ckpt.py` — Step 0 探测，见 §3
- `scripts/export_onnx_template.py` — Step 1 导出脚本骨架，`build_model()` 需要按模型填写，其余部分（导出参数、cosine 校验）已经写好不用改
- `scripts/validate_cosine.py` — 通用 cosine/PSNR 比对小工具（两个 `.npy` 文件），排错时可用
