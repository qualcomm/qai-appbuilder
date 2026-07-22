# model_manager.cpp / .h 实现要点

> 本文档只收敛 `model_manager.cpp/.h` 单文件专属的实现细节与踩坑。`model/` 子系统共享的多模型加载架构、后端限制、`service_config.json` 字段、已知限制见同目录 `model.md`；`config.json` vs `service_config.json` 的核心语义区分等项目级“为什么”见 `docs/architecture.md`。

## 模板回退：`prompt.json` 允许前缀回退，`config.json` 只允许精确匹配

`ModelManager::LoadModel()`（多模型/动态切换路径）里两类模板文件的回退语义**不同**：

- `prompt.json` 缺失时依次尝试精确匹配已知模型模板、再尝试一个硬编码前缀数组（`allam-7b-ssd`/`qwen`/`phi` 等）+ 前缀搜索 `RootDir/config/` 参考模板目录。
- 而 `known_model_path_`（驱动 `ModeVerifierImpl::CreateIfVerified()` 里 `config.json` 的模板回退）只在**精确匹配**命中时才被赋值，前缀匹配命中时保持为空——避免像 `Qwen`（可同时前缀匹配 `Qwen2.0-7B-SSD`/`Qwen2.0-7B`/`qwen2.5vl3b` 等多个候选）这类有歧义的模型名悄悄借用某个不确定的模板 `config.json`。
- 命中前缀但没有精确匹配时，该模型的 `config.json` 视为不存在，按下方 GGUF 的可选语义处理，其它后端仍会因缺少必需的 `config.json` 而加载失败（是确定性的拒绝，不是不确定的凑合）。

## GGUF 的 `config.json` 完全缺失时自动创建空文件占位

`GGUFVerify::CreateIfVerifiedImpl()`（`model_manager.cpp`）本身就把 GGUF 的 `config.json` 视为可选（缺失/为空/解析失败都静默忽略，只有显式 `{"backend":"cpu"}` 才强制走 CPU），文件不存在时直接在模型自己目录下创建一个空文件，而不是像 `prompt.json` 那样借用其它模型的模板——空文件本身即满足“可选配置”的语义，后续按默认设备（优先 GPU，失败回退 CPU）加载。

## 模型注册表写入顺序：轮询等待几乎总是死代码

全仓库仅两处模型注册表写入点（`ModelManager::LoadModel()` 多模型路径、单模型路径注册逻辑）都是**先把 `LoadedModel::is_loaded` 置为 `true`，再原子写入 `loaded_models_` 注册表**——只要 `GetModel()`/`GetDefaultModel()` 能查到，`is_loaded` 必然已是 `true`；因此任何“模型可能仍在后台加载，轮询等待一段时间”的循环在当前实现下都是永远不会生效的死等待，对不存在的模型名只会稳定等满整个超时窗口，还容易与客户端自身的读超时贴脸竞速产生偶发连接异常。排查“等待型”逻辑时应在同一文件/同一类的其它方法里搜索相同模式，不要只关注最初被报告的那一处——同一缺陷模式在同一份代码里出现两次以上并不罕见。

## 同设备单实例是硬限制（`UnloadModelsByDevice`）

如果任务要求“同时跑两个 GPU 模型”之类的场景，先确认是否触碰 `UnloadModelsByDevice` 这条既定限制（见同目录 `model.md` 的“已知限制”一节），不要默认当作 bug 去动代码——除非用户明确要求做架构级改动。

## `~ModelManager` 双引用析构竞态（`STATUS_HEAP_CORRUPTION`）

ModelManager 持有同一模型资源的两份 shared_ptr 引用时，等待保护必须覆盖真正的最后一份引用释放点，不能只覆盖其中一份：单模型模式下 `LoadSingleModel()` 会把同一个 `shared_ptr<ContextBase>` 同时存入 `genieModelHandle` 与 `loaded_models_[model_name_]->context`（引用计数为 2）；`Clean()` 内部“等待 NPU/HTP 驱动异步释放”的等待逻辑只在它把 `genieModelHandle` 置空、且这恰好是最后一份引用时才真正生效——如果 `loaded_models_` 那份引用仍存在，`~GenieContext()` 根本不会在 `Clean()` 内部执行，等待形同虚设，真正的析构会被推迟到 `loaded_models_` 自身被清空/析构的那一刻，而那个时刻往往完全没有等待保护。已通过 minidump 复现确认这会在 atexit 析构链（进程退出、从未显式调用过 `UnloadModel()` 的场景）引发竞态型堆损坏（`STATUS_HEAP_CORRUPTION`）。

修复方式是显式定义 `~ModelManager()`：先在锁内清空 `loaded_models_`（并记录其中是否含 QNN 后端条目），再处理 `genieModelHandle`，确保无论哪一份引用恰好是最后一份，其等待都在本析构函数内完成，而不是被推迟到编译器生成的隐式成员析构阶段（那时已无法插入等待）。

教训：调整这类清理顺序前必须用循环压力复现来验证——曾经尝试的一次“先清空注册表再清理全局句柄”的重排序在此场景下引入了一个新的、100% 确定性复现的 ucrtbase.dll 崩溃（比原问题更严重），说明这类涉及共享引用计数时序的改动，正确与错误的差异可能非常微妙，切不可仅凭推理就认定已修复，必须反复实测。

## 【已知开放问题】反向依赖 `chat_request_handler/summary_cache.h` 的架构耦合

> 尚未解决的既有设计问题，留待后续单独评估，不影响当前可观察行为。

`chat_request_handler/summary_cache.h`/`.cpp` 除被 `long_text_summarizer.h`、`model_input_builder.h` 引用外，还被 `model` 层的 `model_manager.cpp` 引用——即更底层的 `model` 模块反向依赖了更上层的 `chat_request_handler` 模块的文件。这是一处顺手发现的既有设计问题（模块级合并审计时为避免这处反向耦合被进一步加重而保留 `summary_cache` 独立成文件，未与其合并），不影响当前可观察行为，留待后续单独评估是否需要将 `summary_cache` 下沉到更低层的共享模块（如 `model`/`common`）以消除这层反向依赖。
