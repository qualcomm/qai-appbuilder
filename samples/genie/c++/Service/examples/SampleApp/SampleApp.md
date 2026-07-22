# `SampleApp.exe` 的 `--config` 语义与设计

> 本文件是 `SampleApp.cpp` 的文件级归口，只覆盖 `SampleApp.exe` 特有的命令行/加载语义。exe/dll 编译形态区分、库模式 qnn-only 限制、`config.json` vs `service_config.json` 的完整区分等项目级“为什么”见 `docs/architecture.md`（5.1.1 / 5.1.2 / 5.1.3）。

### `--config` 始终指向单模型 `config.json`，不是 `service_config.json`
- `--config <path>` 始终指向**某个模型目录下的原始 `config.json`**（单模型 dialog/engine 配置），**不是** `service_config.json`。

### 多模型自动附加只在 `cwd` 下能发现 `service_config.json` 时触发
- 多模型清单加载是自动附加能力：只要在宿主进程的**当前工作目录**（`RootDir`）下能发现 `service_config.json` 就会触发，与 `--config` 参数值本身无关。**测试或调用 `SampleApp.exe` 时必须用 `cwd=<exe_dir>` 启动**，否则临时/自定义的 `service_config.json` 不会被发现，多模型逻辑等于没生效。

### 无模型宏；两个占位 `api_loadmodel` 入参是刻意为之
- `SampleApp.cpp` 不含任何模型宏（历史上曾用 `#define MODEL_TYPE_*` 等宏在编译期选内联配置，已彻底移除）：唯一的模型来源是命令行 `--config <path>`，未传即打印用法并以非 0 退出。`api_loadmodel(model_path, model_name, hwinfo)` 的后两个入参传占位值（空 vector、固定字符串 `"NPU"`）是刻意为之——引擎在 `-c` 单模型加载路径下从不引用这两个参数，无论模型实际后端是什么。

### 非 qnn 主模型无需任何引擎改动
- 非 qnn 主模型无需任何引擎改动就能跑通：`LoadSingleModel()` → `ModeVerifier::TryCreate(backend="auto")` 会根据模型自身 `config.json` 自动识别 QNN/MNN/GGUF；`GENIEAPI_EXPORTS` 的 qnn-only 限制只作用于**附加**多模型加载，从不影响 `-c` 指定的 primary 模型。
