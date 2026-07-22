# model/ 子系统：多模型并发加载与路由

> 本文档是 `model/` 目录下多个文件共享的区域级知识归口：库模式后端限制、多模型并发加载架构、`service_config.json` 字段、以及已知限制。单文件（`model_manager.cpp/.h`）专属的实现细节见同目录 `model_manager.md`。
>
> `config.json` vs `service_config.json` 的核心语义区分、以及其它项目级“为什么”见 `docs/architecture.md`；多模型的**测试方案**（两两降级验证、每设备最小内存候选选型等）见 `test/test_service.md`。

## 库模式（`GENIEAPI_EXPORTS`）的 qnn-only 后端限制

- `ModelManager::LoadModel(...)` 内用 `#ifdef GENIEAPI_EXPORTS` 包裹一次大小写不敏感的 `backend != "qnn"` 判定，非 qnn 直接记录 warning 后 `return false`。`GenieAPIService.exe` 编译单元未定义该宏，三后端（qnn/mnn/GGUF）行为不受影响。
- `api_interface::api_loadmodel()` 单模型加载成功后，**同步**追加调用 `model_manager->LoadAllModelsFromConfig()`，只记录日志、不影响 `api_loadmodel()` 本身的返回值——多模型清单加载失败绝不能拖累单模型加载的成功状态。
- 库模式“附加加载只允许 qnn”这条路径目前没有自动化负向测试覆盖。如需验证，需手动在 `exe_dir` 下放一份含非 qnn 条目的 `service_config.json` 再跑 `SampleApp.exe`，不要假设现有 `--suite sampleapp` 覆盖了这个场景。

## 多模型并发加载架构概览

服务可在单实例中同时加载多个不同类型的模型（QNN/NPU、MNN/CPU、GGUF/GPU），通过 OpenAI 兼容的 `"model"` 字段路由请求。

```
GenieAPIService.exe -c models/<model_name>/config.json -l -p 8910
  → InitializeConfig()          解析 -c 参数（model_name/model_path/models_root）
                                 读取 service_config.json 的路由/云端/安全等配置（始终执行）
  → LoadAllModelsFromConfig()   仅在 -l 存在时才执行，读取 exe 目录下的 service_config.json
      → 推导 models_base_dir（从 -c 参数向上两级）
      → 遍历 service_config.json["models"]，enabled=true 才 LoadModel(name, backend, device, context_size)
      → 设置 default_model_name_（来自 service_config.json["default_model"]）
```

（`-c` 指向的单模型 `config.json` 与自动发现的服务级 `service_config.json` 是两个完全不同的文件，语义区分见 `docs/architecture.md`。）

模型注册表（`model_manager.h`）：`std::unordered_map<std::string, std::shared_ptr<LoadedModel>> loaded_models_`，配合 `models_mutex_` 保护读写；`LoadedModel` 含 `config`/`backend`（`qnn`/`mnn`/`GGUF`）/`device`（`npu`/`cpu`/`gpu`）/`context`（懒加载的 `ContextBase*`）。

请求路由：`ChatRequestHandler::ChatCompletions()` 取 `body["model"]`（缺失则用 `default_model_name_`）→ `ModelManager::GetModel(model_name)` 查表 → 找到则用该模型 `context` 推理，未找到返回 `404 Model not found or unavailable`。

并发安全：模型注册/查找由 `models_mutex_` 保护；每个模型的推理由自身 `context` 内的 `inference_mutex_` 串行化（同一模型的并发请求排队，不同模型完全并行）。

## `service_config.json` 关键字段

| 字段 | 说明 |
|---|---|
| `name` | 模型唯一标识，客户端 `"model"` 字段用此名称 |
| `path` | 模型目录（相对路径以 models_base_dir 为基准） |
| `backend` | `"qnn"`（Genie SDK）/ `"mnn"`（MNN）/ `"GGUF"`（llama.cpp） |
| `device` | `"npu"` / `"cpu"` / `"gpu"` |
| `context_size` | 上下文长度，0 表示用模型默认值 |
| `enabled` | false 时跳过加载，不影响其他模型（内存受限环境下用它临时禁用高内存模型，如 MNN） |

## 已知限制（均非缺陷，无需再花时间去“修复”）

1. **同一模型不支持真正并发推理**：并发请求会被 `inference_mutex_` 串行化。
2. **MNN 内存不足可能崩溃**：已通过加载时预检查（`MnnVerifier::CreateIfVerifiedImpl`，见 `docs/architecture.md` MNN 内存预检查一节）与生成循环内运行时低内存熔断两层防护降低概率，但不能完全消除；内存受限环境建议将该模型 `enabled=false`。
3. **NPU 资源竞争**：多个 QNN/NPU 模型同时推理时 HTP 资源可能竞争，延迟增加。
4. **模型切换延迟**：首次请求某模型时 context 尚未初始化会有额外初始化延迟。
5. **同一设备同一时刻只驻留一个模型实例**：`ModelManager::UnloadModelsByDevice(device)` 在动态加载/切换模型时，会先卸载同一 `device`（`npu`/`gpu`/`cpu`）上所有已加载模型，再加载新模型。同一种后端类型不支持同时驻留多个实例。**跨设备**的多模型同时驻留（npu+gpu+cpu 各一个）不受此限制，已被验证可行。
6. **三后端同时驻留受宿主机总内存上限约束**：npu（HTP）+gpu（GGUF）+cpu（MNN）三种设备类型互不触碰 `UnloadModelsByDevice`，但它们各自的内存占用仍从同一台机器的总资源池中扣除（尤其是共享统一内存架构的设备）。当三者估算占用之和超过可用内存时，最后一个尝试加载的后端（通常是估算占用最大的 MNN）会因内存不足而完全没有机会被验证到——这不是逻辑缺陷，而是真实的资源约束，因此需要测试方案里的“两两降级验证”兜底，以及“每设备优先选最小内存候选”这条选型策略（见 `test/test_service.md`）。
7. **（已修复）`service_config.json` 自动加载清单曾缺少同设备互斥检查**：`ModelManager::LoadAllModelsFromConfig()`（服务启动时读取 `service_config.json` 附加加载模型的路径）与 `LoadModel()` 此前均未检查目标 `device` 是否已被另一模型占用——`LoadModel()` 本身不会像 `ChatCompletions()` 动态切换路径那样先调用 `UnloadModelsByDevice()`，因此若清单里有第二个同设备（如同为 `npu`）的模型条目，会在同一物理 QNN/HTP 会话上静默叠加加载，破坏已驻留模型的硬件状态，导致该设备后续请求返回错乱结果（且不产生任何错误日志，排查难度高）。已在 `LoadAllModelsFromConfig()` 中新增设备占用检查，与已加载模型 `device` 冲突时跳过（而非顶替），使自动加载清单只补充“尚未被占用”的设备。

模型加载耗时不区分“冷/热启动”：全部后端（含 `gpt-oss-20b-GGUF` 这类约 20B 参数的大模型）从加载到可响应均为秒级到数十秒级，测试脚本对所有模型统一使用同一套端口/请求等待预算，不再为任何特定模型设置特殊的超长等待窗口。若观察到某次加载耗时远超正常量级（如慢十倍以上、稳定复现），应优先排查进程级调试配置（如意外遗留的堆内存校验），参见 `docs/troubleshooting.md` 的 GFlags/Page Heap 教训——历史上这正是导致 `GenieAPIService.exe` 加载被误判为“大模型冷加载天然很慢”的真正根因。
