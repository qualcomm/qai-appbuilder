# test_service.py 回归测试要点

> 本文档是 `test/test_service.py`（`GenieAPIService` “0 error = 健康”权威回归凭证）文件级细节的完整归口：运行命令、`--suite` 一览与参数、判定标准、崩溃可追溯性、展示层踩坑、多模型测试方案、`builder_local_model` 协作测试、tool_calls 覆盖、测试覆盖审计遗留建议等。
> - **健康判定的权威表述**（`failed == ignored` 且 `crashed == 0`、崩溃永不豁免）见 `.junie/playbook.md` 第 4 章，本文档不重复。
> - 架构“为什么”（多模型设计原则、各后端 OOM 对比、QNN 上下文超限、tool_calls 协议实现、云网关脱敏还原）见 `docs/architecture.md`；本机远程路径速查见 `docs/local-machine-paths.md`。

## 测试环境前置
- 依赖：完全依赖模块 `GenieAPILibrary` 首先被成功构建（见 `docs/build-guide.md`）。`test_service.py` 是 Python 脚本，自身无构建步骤。
- **远程机器 ARM64 原生 Python**：远程机器 `PATH` 上默认解析到的 `python` 已实测确认是 ARM64 原生构建（`C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe`，`platform.machine()` 返回 `ARM64`），且已预装 `requests`（2.32.3）与 `psutil`（7.2.2），跑 `test_service.py` 直接用 `python ...` 即可，无需指定完整路径或安装依赖。另有备用环境 `C:\tools\Python311-arm64\Python311-arm64\python.exe`（Python 3.11.5，ARM64，未预装 `psutil`），日常回归不需要切换。
- `--exe_dir <构建产物目录>`：如 `cmake-build-release-visual-studio-arm\GenieService-win-arm64`（Linux 对应 `GenieService-linux-arm64`）。
- `--models <模型根目录>`：每个子目录含 `config.json`。测试模型位于 `C:\Users\HCKTest\Desktop\GenieEnv\models`（`gpt-oss-20b-GGUF`、`gpt-oss-20b-MNN`、`phi4-v81`、`phi4-v81-i`、`qwen2.5vl3b-8480`、`qwen2.5_omini_8480-2.42`、`qwen3-8b-8480`）。
- `--data_dir`：多模态素材目录（图片/音频）。
- 结果写入 `<out_dir>/results.json`（默认 `./test_results`）。
- **以上 `--exe_dir`/`--models`/`--data_dir` 全部是远程机器路径，本机不存在**；`test_service.py` 命令同样要走 `winrs` 在远程执行，连接信息/同步/执行/回取方式见 `docs/build-guide.md` 3.1。

## 权威回归凭证与运行模式
基于 Python 的端到端回归脚本，针对运行中的服务使用真实模型测试。统一的 `--suite` 分发体系（**`--suite` 为必传参数，不再有隐式默认值**；省略会直接报错退出），覆盖 `full`/`model`/`mnn`/`qnn`/`gguf`/`multimodal`/`multi_model`/`sampleapp`/`builder_local_model`/`graceful_shutdown` 等值。逐模型测试含 3 种 chat 请求格式轮换（OpenAI 字符串/数组、GenieAPIClient 风格 object）与多模态（图片 `image_url`、音频 `input_audio`）；多模型并发加载验证 NPU/GPU/CPU 三后端可同时在线。
- **本地模式**：`--exe_dir` + `--models`（脚本负责启动/停止进程）。
- **远程模式**：`--remote --host <ip> --port <port>`，不启动/停止进程，也不会临时改写 `service_config.json`（涉及改写配置的专项 suite 在远程模式下会自动跳过并给出说明——这是合理跳过，不是过度容忍）。

## 运行命令
### 完整自测试（`--suite full`）
```powershell
python test\test_service.py --exe_dir cmake-build-release-visual-studio-arm\GenieService-win-arm64 --models C:\Users\HCKTest\Desktop\GenieEnv\models --data_dir C:\Users\HCKTest\Desktop\GenieEnv\data --suite full
```
一次运行覆盖 `full` 套件的全部阶段（通用接口→逐模型→多模型并发加载→GGUF 显式 CPU/GPU 加载回归→`-n -1` vs `-n 30` 差异化自验证矩阵→SampleApp→推理中终止进程优雅退出验证）与全部已配置模型。**`full` 不再包含任何 Builder 阶段**——Builder 相关验证已收敛为独立的 `--suite builder_local_model`，需要用户显式指定运行。

### 专项套件示例
```powershell
python test\test_service.py --exe_dir <build> --models <models_root> --suite mnn
python test\test_service.py --exe_dir <build> --models <models_root> --suite qnn
python test\test_service.py --exe_dir <build> --models <models_root> --suite gguf
```

### 如何解读回归结果
- 健康判据（`failed == ignored` 且 `crashed == 0`、退出码非 0 属预期、崩溃永不豁免）见 `.junie/playbook.md` 第 4 章。
- **已知范围外失败**：`/images/generations` 与 `/v1/images/generations` 的 `501`（图片生成端点未实现）标记 `ignorable=True`，不代表回归退化；判定时必须同时校验 HTTP 状态码 **和** 错误信息内容两个维度（见下文「判定标准」）。
- 出现任何**非 `ignorable`** 的 `failed`、或任何 `crashed`，都属于需要立即排查的新增问题；判断前务必先看 `detail`/`ignore_reason` 字段，不要假设“crashed = 可忽略”。

## `--suite` 一览与关键参数
### `--suite` 一览
| 值 | 语义 | `--model_name` 过滤 |
|---|---|---|
| `full` | 多阶段完整回归：通用接口→逐模型→多模型并发→GGUF 显式加载→`-n -1` vs `-n 30` 差异化自验证矩阵→SampleApp→推理中终止进程优雅退出验证。**不含任何 Builder 阶段**，Builder 验证需单独运行 `builder_local_model` | 不支持 |
| `model` | 逐模型标准生命周期测试 | 支持 |
| `mnn` | 只测 `infer_backend()` 判定为 `mnn` 的模型（MNN 不支持多模态） | 支持 |
| `qnn` | 只测 `infer_backend()` 判定为 `qnn` 的模型；多模态模型自动跑多模态用例 | 支持 |
| `gguf` | 全部 GGUF 模型（或 `--gguf_model` 限定单个）× `--gguf_devices`（默认 `both`）显式加载回归 | 用 `--gguf_model` |
| `multimodal` | 用 `detect_modality()` 自动筛选支持多模态的模型 | 不支持（自动筛选） |
| `multi_model` | 多模型并发加载（NPU/GPU/CPU 三后端同驻）路由测试 | 不支持 |
| `sampleapp` | 只验证 `SampleApp.exe`（遍历全部已发现模型，逐个用 `-c` 跑基本问答），不涉及 `GenieAPIService` 的 HTTP 全量回归 | 支持 |
| `builder_local_model` | 真实驱动当前版本 QAIModelBuilder：指定安装目录→注入本地模型→Builder 发现→启动加载→直连 GenieAPIService 验证真实推理→切换→停止→异常场景 | 不支持（用 `--builder_local_models`） |
| `graceful_shutdown` | 三后端“推理中终止进程→优雅退出”专项验证 | —— |

### 关键参数
- `--rounds` 默认 `1`，必须 `>=1`（无效值直接 `sys.exit(1)`）。
- `--multimodal_rounds` 默认跟随 `--rounds`（未显式传入时二者相等），显式传入时优先于 `--rounds` 驱动图片/音频多模态用例（含动态切换稳定性用例）的独立轮次，与普通接口测试的轮次完全解耦、互不放大（`APITester.run_multimodal_rounds()`）。
- `--gguf_model` 默认 `None`（留空 = 测试全部已发现的 GGUF 模型）；`--gguf_devices` 默认 `both`。
- `--builder_python`（默认自动探测官方 `Setup.bat` 搭建的独立 venv，找不到回退 `sys.executable` 并警告）、`--builder_data_dir`（默认 `<out_dir>/qaimodelbuilder_data`，每次运行新建、非持久）、`--genie_root_path`（默认复用 `--exe_dir`）、`--builder_local_models`（默认自动挑选 qnn/gguf/mnn 各一个）：均仅 `--suite builder_local_model` 使用。
- 不支持 `--model_name` 的 suite（`multimodal`/`multi_model`/`builder_local_model`）检测到该参数被显式传入时会打印提示（不阻断执行）。
- 旧的 `--sampleapp_only`/`--only_gguf_explicit_load` 布尔 flag 及其别名映射表已彻底移除，唯一入口是 `--suite sampleapp`/`--suite gguf`。

### 如何运行单个（或若干）模型（只能筛到模型粒度）
用 `--suite model --model_name <name>`（逗号分隔可传多个）精确限定到某一个或若干模型，`mnn`/`qnn`/`sampleapp` 套件同样支持 `--model_name` 过滤；`gguf` 套件用 `--gguf_model <name>` 限定单个 GGUF 模型。脚本仍**未**提供“按具体测试用例名称”筛选的参数，只能筛到模型粒度。

## 后端与多模态判定依据
- `infer_backend(model_name)`：模型目录名含 `gguf`→`(GGUF, gpu)`；含 `mnn`→`(mnn, cpu)`；其余→`(qnn, npu)`。
- `detect_modality(model_name)`：只有 `qwen2.5vl*`/`qwen2.5_omini*`/`phi4*` 前缀判定为支持多模态（仅 `image`/`audio` 两种，不涉及“视频”）；MNN、GGUF(llama.cpp) 模型目录名从不匹配这些前缀，因此天然不会触发多模态用例——这是“MNN 和 llama.cpp 完全不支持多模态”这一设计事实在代码里的体现方式。
- `ModelInputBuilder::ProcessArray`（`model_input_builder.h`）支持 OpenAI 标准 `content=[{type:"input_audio","input_audio":{"data":<base64>}}]` 解析，写入 `model_input_.audio_`，与既有 `image_url` 分支同等处理（服务端两种多模态请求风格的区分见 `src/GenieAPIService/src/chat_request_handler/model_input_builder.md`）。

## 多模态轮次与切换稳定性检验
- 图片/音频多模态用例已从 `run_all()` 的普通轮次中完全抽离，改由独立的 `APITester.run_multimodal_rounds()` 按 `self.multimodal_rounds` 驱动；`full`/`model`/`qnn`/`multimodal` 等 `--suite` 在正常执行时自动覆盖，无需额外参数。
- 支持图片的 QNN 多模态模型在其独立多模态轮次里会自动追加一次同设备动态切换后接多模态请求的稳定性检验（`test_dynamic_switch_multimodal_stability`：先向同后端的其它已发现模型发一次纯文本请求触发切出，再向自己发一次图文请求完成“切回+推理”），无需单独 `--suite`；找不到同后端切换伙伴时精确跳过（环境只有这一个该后端模型，非缺陷）。
- 多模态用例的执行轮次由独立的 `--multimodal_rounds` 控制（默认跟随 `--rounds`，显式传入优先级更高），与普通接口测试轮次完全解耦；每一轮各自重新随机抽取图片/音频素材，抽取次数与该轮数一一对应。

## `--suite` 分发架构
- 全部 suite 通过模块级 `SUITE_HANDLERS` 字典分发，收尾统一调用 `_finalize_and_exit`（一次统计 + 生成全部报告 + 计算退出码）。新增/调整 suite 时优先复用这个字典和收尾函数，不要在 `main()` 里再手写第二套统计逻辑。
- `_run_full_suite` 的各阶段内部直接调用 `_run_model_suite`/`_run_multi_model_suite` 等专项函数，因此 `full` 套件与单独跑对应 suite 在这些阶段上的行为完全一致；`mnn`/`qnn` suite 同样是“用 `infer_backend()` 过滤模型列表后委托给 `_run_model_suite`”——新增按维度拆分的 suite 时应延续这种“专项 suite 函数 + 委托复用既有生命周期逻辑”的写法。

## 判定标准：什么是“真正该跳过” vs “过度容忍”
这是本脚本长期需要坚持的核心原则，新增/修改任何 skip/ignorable 逻辑时都要对照检查：

| 真正该跳过（保留） | 判定要件 |
|---|---|
| 环境/素材缺失 | 用精确的集合比对或目录扫描确认（`discover_models()`/`infer_backend()`/`detect_modality()`），而非猜测 |
| 功能设计上不适用 | 该模型/该模式本身不支持此功能，是文档化的设计事实（如 MNN/GGUF 不支持多模态） |
| 已知服务端占位缺陷 | 同时校验 HTTP 状态码 **和** 错误信息内容两个维度，缺一不可（如 `/images/generations` 501） |

| 过度容忍（已修复，不要再引入类似模式） | 说明 |
|---|---|
| 宽泛字符串子串匹配批量豁免 | 用 `"not found"`/`"unavailable"` 这类极易在不同错误里出现的通用词一旦命中就大范围豁免——豁免必须由具体测试方法自己基于精确信号（如 `failure_reason`）判定，不能由外部一次性探测结果批量下发 |
| 异常被吞、既不算 failed 也不算 skipped | `except Exception` 只记 `CrashEvent` 不产生 `TestResult`，不进入 `_finalize_and_exit` 的统计和退出码——**任何套件级异常处理都必须同时产生 `TestResult(crashed=True)` 和 `CrashEvent`** |
| 严重问题标记为 skipped 而非 crashed | 服务启动失败是最严重的信号之一，不该计入不影响退出码的 `skipped` 桶 |
| 默认参数让整类测试从不真正执行 | 默认值使某类测试永远只留 1 条占位记录（如曾经的 `--enable_builder_openclaw` 默认 `False`） |
| 基于猜测而非精确校验的归因 | 仅凭集合成员关系/计数条件就套用“可能是 OOM/资源不足”，而不核实真实原因（如未核实设备是否真的被请求过） |
| 未验证的场景被误标为通过 | 该用例实际没有执行验证，却记为 `passed=True` 而非 `skipped=True` |
| 参数校验缺失导致静默异常行为 | 无效输入不报错，只是悄悄产生空结果或被误判为别的原因 |
| **崩溃被当作可容忍的设计事实去豁免** | 进程崩溃永远是异常信号，不该被当作可依赖的设计事实去豁免其它模型的连带失败；正常（非崩溃）的优雅失败不会造成连坐，只有真的崩溃才会——即使最初触发者是 MNN OOM，级联崩溃也**不可**自动标记为 `ignorable=True`。诊断性标注（如 `ignore_reason="mnn_oom_cascade"`）可以保留提示疑似关联，但不能被解读为已豁免 |
| 分类式无条件豁免 | 仅凭设备类型/模型名关键字（如 `ignorable=device=="gpu"`）就无条件洗白任何异常，与异常真实原因无关——只有异常消息命中确凿的资源不足关键词时才可附加诊断提示，不能整类豁免 |

**MNN 内存不足的正确处理方式**：只要表现为可通过错误码精确识别的优雅失败（服务端 `failure_reason=insufficient_memory`），应归类为 `skipped=True`（环境资源约束，不是代码缺陷，不计入 failed 统计）；一旦进程真的崩溃（不论最初触发者是谁），必须始终归类为 `crashed=True` 且 `ignorable=False`——崩溃永远不因“最初触发者是内存不足”而被降级为 skip。完整设计见 `docs/architecture.md`（各后端 OOM 对比与多模型设计原则）。

**该识别必须覆盖全部“依赖模型已加载”的下游用例，否则同一次 OOM 会被拆散计成多条独立的 `failed`**（真实案例：一次 `full` 回归里 `gpt-oss-20b-MNN` 因 `insufficient_memory` 被优雅拒绝后，`/profile`、`/contextsize`、`/textsplitter`、tool_call 重试熔断测试、`SAMPLEAPP: basic_chat` 这 5 类用例分别报了 6 条独立 `failed`，根因全部是同一次 OOM 的级联症状，而非 5 个新缺陷）。`test_service.py` 用共享辅助 `_is_cascading_mnn_oom(svc, model_name)`（判定该模型是 mnn 后端且本次进程生命周期内已记录过针对它的 `insufficient_memory` 事件）识别 `/profile`/`/contextsize`/`/textsplitter` 这类自身响应体不会重新携带 `failure_reason` 的辅助接口；tool_call 重试熔断测试与直接聊天补全一样直接从响应体提取 `failure_reason`；`SampleApp.exe` 没有 HTTP 响应体，改用共享标记常量 `_MNN_INSUFFICIENT_MEMORY_MARKER = "insufficient memory to load MNN model"` 匹配其自身 stdout 日志中 `MnnVerifier` 打印的同一段精确文本。新增任何依赖“模型已加载”的用例时，应复用这两个信号源之一，不要重新发明识别逻辑。

## 崩溃可追溯性设计
如果测试脚本报告“崩溃”，必须能直接说清楚“谁、哪一步”出的错，不能依赖事后猜测：
- `ServiceManager`/`QAIModelBuilderManager` 提供 `get_exit_code()`，**必须在 `restart()`/`_force_kill()` 把 `self.process` 置空之前调用**，否则拿到的是新进程的信息而不是真正崩溃那个进程的证据——这是一个容易踩的调用顺序坑。
- 裸退出码需要翻译为人类可读诊断（Windows NTSTATUS 对照表，如 `0xC0000005` 空指针/越界访问、`0xC0000374` 堆损坏、`STATUS_STACK_OVERFLOW`、`STATUS_DLL_NOT_FOUND`），并拼进崩溃事件的日志尾部，默认展开可见（退出码是判定“这是谁的错误”最直接的证据，不该藏在需要额外点击才能看到的地方）。
- **崩溃确认需要宽限期重试，不能只探测一次**：进程真正崩溃到 OS 完全回收句柄/端口之间存在短暂延迟（Windows 上尤其明显）。捕获 `ConnectionError`/`Timeout` 后只做一次立即的 `is_alive()` 探测就得出结论，会让真正致死的那次请求被误判为“进程仍存活的普通异常”，而让稍晚发出的下一条无关请求背了崩溃的锅（`crash_events` 表里的 `endpoint` 指向错误“元凶”）。正确做法：捕获异常后先立即探测一次，若判定“存活”则在一个短暂宽限期内（如 2 秒）轮询重试，让真正致死的那次请求本身被正确标记为 `crashed=True`。
- 请求异常处理不能只吞掉异常返回 `None`：应记录 `f"{type(e).__name__}: {e}"` 这类具体异常类型+消息，供 detail 展示，否则“连接错误/超时”这种模糊提示无法区分究竟是 `ConnectionError` 还是 `ReadTimeout`。
- **同请求跨宿主对照测试**：对支持图片的模型，用与 HTTP 多模态测试完全相同的请求体，经 `SampleApp.exe`（`GenieAPILibrary` 宿主）转发给同一份共享引擎代码，可直接对照 `GenieAPIService.exe`（exe 宿主）是否复现同一次崩溃。判定：两边都崩溃→共享引擎代码本身的真实 bug，与宿主/驱动无关；只有一边崩溃→该次崩溃与某个宿主特有的胶水代码路径相关；两边都不崩溃→这次未复现，不代表问题不存在。
- **连续重启之间必须等待端口真正释放，不能只等待“新端口可连接”**：`graceful_shutdown` 这类在同一函数内连续对多个后端重启服务的测试，若上一个进程刚被强制结束、下一次 `start()` 紧接着立即发起，监听端口可能仍处于 TIME_WAIT/延迟释放阶段——服务自身的 `isPortAvailable()` 检测到端口“仍被占用”会打印 `service already exist.` 并以 `exit_code=0` 立即退出，被测试框架误判为“启动失败”甚至计入崩溃统计。修复是新增 `wait_port_closed()`（与既有 `wait_port_open()` 对称，轮询直到端口不可连接）并在每次 `start()` 前调用；超时仍视为“未确认释放”但不阻塞太久，真正的失败原因仍会在 `start()` 自身日志里体现。

## 实现约定与展示层踩坑
- **流式测试双重超时防线（易错点）**：`requests` 库的 `timeout` 参数对流式响应（`stream=True` + `iter_lines`）只约束“两次数据到达之间的间隔”，不约束整个读取循环的总时长——如果服务端一直在（哪怕很慢地）持续吐出数据，读取循环可以运行很久都不会触发 `timeout` 异常。任何涉及流式/长轮询的读取循环都必须同时设置“单次等待超时”和“总时长上限”两道防线（本项目用 `MAX_STREAM_SECONDS` 兜底，超时后主动调用 `/stop` 截断连接）。
- **CPU 占用率展示需要按逻辑核心数归一化**：`psutil.Process.cpu_percent()` 原始语义是“单核=100%”，多线程后端（如 MNN 跑满 7-8 个核心）原始值可轻松到 700+%，而报告应展示“占整机总算力的百分比”口径（与任务管理器一致）。展示层必须调用归一化函数，`results.json` 里保存的原始采样值不受影响。
- **矩阵/报告里的模型行不能因为设备后缀不一致而被拆散**：GGUF 同一个模型可能被服务实际选中 CPU 或 GPU，若不同阶段产生的 `TestResult.model_name` 用不同命名约定（裸名 vs 带 `(CPU)`/`(GPU)` 后缀），会在矩阵/报告里被人为拆成孤立多行。涉及设备的 suite（Builder E2E、SampleApp 等）都需统一这套后缀约定；用裸模型名做子串匹配的地方（如“多模型路由”矩阵列）需先还原设备后缀再匹配。
- **报告新增区块必须复用既有视觉语言**：`_common_css` 已有的 Material Design 变量（`--primary`/`--success`/`--danger`/`--warning`）与 `.summary-card`/`.pivot-table`/`.status-*`/`.badge-*` 类名体系，不引入新配色或字体系统；Header 需展示本次运行的完整命令行（超长命令行用 `<details>` 折叠，复用等宽字体样式）。
- **（已废弃并移除）曾有一套“重复输出检测”（滑动窗口精确文本比较算法，`_detect_repetition`）**：因结构性无法识别“结构重复但具体文字不完全相同”的模型失控模式（如条目编号递增的幻觉式枚举），已被证明不可靠并从 C++/Python 两侧彻底移除，改为服务端 `[MODEL_DEFECT]` 日志标记 + 测试侧扫描的机制（见 `src/GenieAPIService/src/context/qnn/qnn.md`）。新增任何“模型输出异常”类判定都应复用这套日志标记机制，不要重新发明客户端文本比较算法。
- **测试完备性矩阵对“环境受限的失败”（如 MNN 内存不足）展示为“跳过并注明原因”的独立展示态**，但这只是**矩阵单元格展示层面**的收紧，不改变 `TestResult.passed`/`crashed`/`ignorable`/`skipped` 字段本身，不影响 `failed`/`crashed` 统计口径和退出码判定。
- **矩阵单元格判定“失败”必须同时排除 `passed`/`ignorable`/`skipped`**：类别内混有已通过用例与一条因模型能力局限被标记 `skipped=True`（非 `ignorable=True`）的用例时，若漏排除 `skipped`，会把整个类别误判为“失败”。排除后若已无真正失败项，应判定为“跳过”（原因取自被跳过用例的 `detail`）。
- **“通用接口”兜底类别必须排除全部专属列用例（`tool_calls`/`SampleApp`/`Builder`/`GGUF显式加载`）**，否则会被重复计入并污染判定：判定逻辑统一走共享方法 `_has_dedicated_matrix_category(name)`；新增任何专属矩阵列（`MATRIX_CATEGORIES` 新增一列）时，必须同步把该列的匹配条件加入这个方法。
- **`_finalize_and_exit()` 控制台汇总行的四个桶必须互斥**，否则 `passed+failed+crashed+skipped != total`：多处 `TestResult` 同时设置 `passed=True, skipped=True`（能力局限跳过）或 `crashed=True, skipped=True`（`is_over_restart_limit()` 占位记录），若按独立 `sum(...)` 分别统计会重复计入。已改为与 `generate_summary_html()` 的 `summary_all` 同一套互斥优先级（`SKIP > CRASHED > PASSED > FAILED`）逐条分类，保证四桶之和恒等于 `total`；该优先级对崩溃检测安全——唯一同时带 `crashed=True, skipped=True` 的记录只在 `is_over_restart_limit()` 触发（此前已有更早一条纯 `crashed=True` 记录先行捕获真实崩溃），不会让汇总行的 `崩溃==0` 掩盖真实崩溃。
- `discover_models`/`discover_models_remote` 对“含权重文件但缺 `config.json`”的目录、以及远程 `/models` 返回异常状态码/缺字段的情况，都会打印 `WARNING` 而不是完全静默跳过——本地环境确实出现过被这样漏测的模型目录，新增任何模型发现/过滤逻辑都应保留这类可见性告警。

### 报告/结果解读常见误判（不要当成 bug）
- **看到 report.html 里出现 `_multi_model_`/`_global_` 这类下划线占位符文本，一律视为需要立即修复的 bug**：这是内部聚合用的占位 `model_name`，绝不应以字面文本渗透进任何渲染出的报告；正确做法是复用 `ReportGenerator.INTERNAL_PLACEHOLDER_MODEL_NAMES`/`_is_internal_placeholder_model()`，不要再手写排除元组（详见下文「两两降级共存矩阵」）。此外，排查“报告里出现了不该出现的内容”类问题时，先确认打开的报告文件的生成时间戳，避免把一份过时/无关的历史报告误当成最新回归结果。
- **看到“两两同时驻留验证未通过”一类失败提示引用了“崩溃事件日志”，但报告里根本没有崩溃事件日志区块，不要以为是渲染漏了——先确认这次回归 `crashed` 计数是否真的是 0**：`report.html` 的“崩溃事件日志”区块只在 `crash_events` 非空时才渲染；`0` 崩溃时该区块本就不存在，此时该组合真实失败原因是非崩溃的优雅失败（如 MNN `failure_reason=insufficient_memory`），应去看同区块下方的“逐项检查记录”表格。判定“是否真崩溃”的权威依据是 `results.json`/汇总卡片里的 `crashed` 计数，而不是某条失败提示文案写了什么。
- **看到“并发与故障恢复”分类里 `MULTI: concurrent requests to different models` 被跳过、理由是“仅 1 个模型加载成功”，不要立即当成 bug——先看这次回归有没有触发两两降级**：两两降级循环结束后进程里可能只剩最后一次重启的入口模型在跑，是重启序列设计的必然结果，不是代码缺陷（含“已加载 backend/device”表格为何仍显示 2 个模型不矛盾，见下文「两两降级共存矩阵」的已知局限）。

## QAIModelBuilder 协作测试（`--suite builder_local_model`）
`QAIModelBuilder` 是与 `GenieAPIService` 协作的外部 Builder 应用（模型管理/网关前端）。`--suite builder_local_model` **真实驱动当前版本**的 Builder 进程（真实子进程，非 mock），走通“指定 GenieAPIService 安装目录 → 注入本地模型 → Builder 发现 → 启动加载 → 直连 GenieAPIService 验证真实推理 → 切换模型 → 停止 → 异常场景”全链路（本仓库不负责构建 `QAIModelBuilder`）。**被测对象是 GenieAPIService 本身（真实推理行为是否正确），Builder 只是启动/管理它的环境载体**——所有 Builder 侧 API 交互都是手段层，断言密度集中在“GenieAPIService 加载指定模型后是否真正可用”这一目标层。

早期版本的 `--suite builder_integration`（针对 Builder 重构前 `backend/main.py` 扁平结构编写、启动路径和健康检查端点均已不存在）已被彻底移除。

```powershell
python test\test_service.py --exe_dir <build> --models <models_root> --suite builder_local_model
```
- **零侵入 Builder 源码**：全部通过 Builder 已支持的官方外部接口达成——环境变量（`QAI_AUTH__ENABLED=false` 关闭 Okta 登录门禁；`QAI_DATA__DATA_DIR` 指向隔离数据目录）、CLI 启动入口（`<builder_python_exe> -m apps.api --host <host> --port <builder_port>`，debug-only 直接启动路径，`cwd=<builder_dir>`，`PYTHONPATH=src;.`）、HTTP API、纯文件系统操作（`mklink /J`）。**CSRF 双提交防护保持开启**，由内置 `_CsrfSession` 完成真实握手（对安全方法发 GET 收获 `qai_csrf` cookie，后续非安全方法自动附加 `Cookie` + `X-QAI-CSRF` 头）。
- 健康检查端点是 `GET /api/system/health`（DDD 重构后的新端点，取代已不存在的旧 `/api/health`）。
- `--builder_python`/`--builder_data_dir`/`--genie_root_path`/`--builder_local_models` 驱动“指定安装目录 + 注入本地模型”（默认值见上文「关键参数」）。

要点与踩坑（均为真实驱动当前版本 Builder 时实测发现，且均属 Builder 自身行为/缺陷，未修改 Builder 源码）：
- **【关键发现】`POST /api/forge-config` 写 `genie_service.root_path` 永远不会被服务启动路径读取**：该端点走 `SaveDocumentUseCase → KvUserPrefsRepository`，持久化到 SQLite `kv_user_prefs` 表；而服务启动时真正解析安装目录的 `_make_install_dir_provider` 读取的是磁盘上的 `<data_dir>/config/forge_config.json` 纯 JSON 文件——两套完全独立、互不联通的持久化后端。`GET /api/forge-config` 回读一致只是因为读写发生在同一套（错误的）存储上，容易被误判为“配置已生效”。**正确做法**：用 `mklink /J` 把 GenieAPIService 安装目录联接到 `<data_dir>/bin/<name>`，触发 Builder 官方标注的“自动发现已安装版本”自愈机制（`_make_install_dir_provider` 每次请求都会实时扫描该目录）。
- **【关键发现】Builder 的 `service_config.json` V1 兼容同步逻辑会误导后端判定**：Builder 每次 `POST /api/service/start` 都会把 exe 目录下 `service_config.json` 里遗留的旧 qnn 条目改名为本次请求的模型名，但**不更新 `backend` 字段**，导致 GenieAPIService 被误导按 qnn 强制加载 GGUF/MNN 模型而失败。规避方式：每次 `start` 前临时清空该共享文件的 `models[]`、结束后自动恢复原内容，且**必须在每一次 `start` 之前都重新执行清空**（不能只做一次），因为 Builder 会在两次 start 之间悄悄把条目补回去。
- **无效安装目录场景 Builder 返回的是未处理异常导致的 `500`（`internal.unexpected`），而不是结构化 `4xx`**：Builder 自身的一个真实缺陷（异常处理粒度不足），按“零侵入”约束未修复，仅在测试脚本侧记录与容忍，不视为回归失败。
- Builder 固定以 `loglevel=3`（`kInfo`）启动 `GenieAPIService.exe`，且结构性无法同步读取子进程日志（`GET /api/service/logs` 只是异步 SSE 流，无同步/一次性读取接口）；验证“CLI 参数真的生效”应优先使用 `GET /api/service/status` 返回体的 `command` 字段（完整启动命令行字符串，`shlex.split` 解析），而不是尝试解析日志。
- **当前验证覆盖范围**：端到端链路已验证 GGUF 主加载与 MNN 切换目标的直连推理路径；QNN 后端模型目前只验证了注入/发现，尚未验证一次真实的 QNN 加载路径，留待后续补齐。

**与 QAIModelBuilder 自身三端一致性测试的区分**：本 `--suite builder_local_model` 被测对象是 `GenieAPIService`（Builder 只是环境载体）；验证 Builder 自身 CLI/HTTP API/WebUI 三种外皮是否共用同一内核、微信飞书 channel 全链路、已知缺口回归的是另一套完全独立的测试体系（`test/test_builder_cli.py` + `QAIModelBuilder/frontend/e2e/`），跨套件约定见 `docs/qaimodelbuilder-testing.md`，各套件文件级细节见 `test/test_builder_cli.md` 与 `QAIModelBuilder/frontend/e2e/app-builder-consistency.md`。

## 原生 C++ 测试子项目已移除
`test/CMakeLists.txt`（曾定义 `project(test_tool_call_path)`）是一个完全独立、不随根项目编译的 CMake 子项目，现已被**完全移除，不再存在于仓库中**。当前 `test/` 目录下唯一的测试凭证就是 `test_service.py`（及其运行产生的产物目录）；主要验证路径始终是 `test\test_service.py`。

## tool_calls / function calling 协议测试覆盖
`tool_calls`（OpenAI function calling 协议）测试与 `-n` 取值完全无关，覆盖 `model`/`qnn`/`full` 等主套件（`APITester.run_all()` 的测试列表里，紧跟在 `/images/generations` 之后、`/reload` 之前）；协议的服务端实现清单见 `docs/architecture.md`（tool_calls / function calling 一节）：
- **确定性契约测试（不依赖模型真实决策，必须 PASS）**：`test_tool_call_retry_limit_enforcement`（构造超过 `agent_routing.max_tool_call_retries` 阈值的连续 `role:"tool"` 占位消息，断言 400 + `error.message` 含 `"maximum allowed tool call retries"`；阈值优先从 `exe_dir` 下实际生效的 `service_config.json` 动态读取，读取不到才回退代码默认值 10）、`test_tool_call_round_trip_with_result`（手动构造 assistant `tool_calls` → `role:"tool"` 回填结果 → 追问 user 的两轮对话，断言 200 + 回复非空）。
- **机会性观察验证（能触发则做 schema/参数完整性校验，未触发则 `skipped=True`，不计入 `failed`）**：`test_tool_call_model_invocation_probe`（非流式，强指令要求模型调用预定义工具 `"read"`，触发后校验 `tool_calls` 数组的 `id`/`type`/`function.name`/`function.arguments` 结构完整性）、`test_tool_call_streaming_argument_integrity_probe`（流式，要求模型携带一个 120+ 字符的长 `path` 值调用 `"read"`，按 `index` 聚合全部 `delta.tool_calls` 片段重建 `function.arguments`）。
- **`"unknow"` 兜底标记被两个机会性用例识别为“模型能力局限，非服务端缺陷”**：服务端 `ResponseTools::convertToolCallJson` 对无法解析的模型输出会把 `function.name` 置为 `"unknow"`（见 `src/GenieAPIService/src/response/response_dispatcher.md` 与 `docs/architecture.md`）；两个机会性用例命中该精确信号时归类为 `skipped=True`，判定依据是精确字面值匹配（`"unknow"`），不是宽泛容忍。
- **失败判定边界：只对服务端确定性保证的字段判失败，`function.arguments` 内容能否解析为合法 JSON 单独降级为 skip**：本地模型（qnn/mnn/gguf）普遍能力较弱，`function.arguments` 的具体文本完全取决于模型自身输出——已用源码核实 `ResponseTools::convertToolCallJson`（`response_tools.cpp` 约 260-278 行）在多轮修复尝试全部失败后会原样保留模型输出字符串作为 `arguments`，不是服务端可控的确定性契约。因此 `test_tool_call_model_invocation_probe` 把 `tool_calls[].id`/`type`/`function.name`（非空，含 `"unknow"` 兜底）三者的结构性错误保留为真正的 `failed`，而把 `function.arguments` 无法 `json.loads` 解析单独降级为 `skipped=True`。
- **`test_tool_call_streaming_argument_integrity_probe` 的判定只针对与内容无关的机制事实**：本地后端（qnn/mnn/gguf）的 `tool_calls` 参数从不会被拆成多个 SSE chunk，而是整段一次性到达——本地路径根本不存在“跨 chunk 拼接”。现改为：只要聚合到的分片数（`arg_frag_count`）<=1（本地路径始终如此），只要求拼接结果结构完整（合法 JSON、含 `path` 字段），内容是否逐字一致降级为观察记录（`model_capability_issue`），不影响 `passed`；只有分片数>1（真正触发跨 chunk 拼接，目前只有云网关敏感信息还原路径——见 `src/GenieAPIService/src/gateway/gateway/gateway_cloud.md`——会真正拆分）且连合法 JSON/`path` 字段都凑不出来时，才判定为真实拼接缺陷（`failed`）。测试代码本身不需要为本地/云端各写一份，区别只在于这次请求实际触发了哪条服务端代码路径。
- 实测（`phi4-v81`/`qwen3-8b-8480`/`qwen2.5_omini_8480-2.42`）确认：两个确定性契约测试稳定 `passed=True`；机会性用例在不同模型/轮次下可能 `passed=True` 或 `skipped=True`（取决于模型能力，非服务端缺陷）。
- **外部参考实现 `GenieAPIClientTools.py`（qai-appbuilder-main 配套仓库）评估结论：已评估、决定不整合**——它依赖本项目从未使用的 `openai`/`colorama` 库，是纯手动调试脚本（无 `TestResult`/断言框架），定义的示例工具与本项目 `prompt_optimizer.cpp` 预定义工具表（`read`/`write`/`edit`/`exec`/`browser`/`cron`/`web_search`/`web_fetch`/`image`）完全不重合；其 round-trip 调用模式的价值已被 `test_tool_call_round_trip_with_result`/`test_tool_call_model_invocation_probe` 吸收，无需接入或后续重复调查。

## 多模型测试方案
- 目标：`GET /models` 返回全部已加载模型列表；对每个已加载模型发路由验证；并发请求不同模型验证并发安全；MNN/大内存模型加载失败时其他模型仍可正常服务；三后端未能全部同时驻留时至少两两可同时驻留。多模型加载的服务端设计“为什么”见 `docs/architecture.md` 与 `src/GenieAPIService/src/model/model.md`。
- 实现：多模型测试在 `full` 的阶段3执行——启动后查询 `/models` 获取实际加载列表，对每个模型发 non-stream chat 请求，用 `ThreadPoolExecutor` 并发发送；MNN 自身可精确识别的内存不足优雅失败标记 `skipped=True`，进程真实崩溃/级联崩溃仍标记 `ignorable=False`；三后端未能全部同时驻留时自动触发两两降级验证。
- **默认模型路由回归监控**：`MultiModelTester.test_default_model_routing()`（检查项 `MULTI: default model route (no 'model' field) after dynamic switches`）仅在确实触发过跨设备动态切换（`models_by_device` 长度 >1）时执行，发一个不带 `model` 字段的请求，只监控“不崩溃/不返回 5xx”这条安全底线——因为 `ChatRequestHandler::ChatCompletions` 每次动态切换成功都会 `SetDefaultModel(modelName)`，把默认模型覆写为“最后一次被动态路由到的模型”，若该模型恰好被驱逐，`GetDefaultModel()` 会退化为 `unordered_map` 的任意元素；本检查不假定具体路由到哪个模型（既定但不确定的实现细节）。
- **`test_concurrent_requests` 内容非空校验**：不再只判断 `status_code==200` 就算成功，还要求 `choices[0].message.content` 非空，避免“全部 200 但回复都是空字符串”这类假通过。
- **`test_models_list`（`GET /models`）字段取值校验**：对每条已加载模型记录的 `backend`/`device` 字段是否落在预期取值集合（`{qnn,mnn,gguf}`/`{npu,cpu,gpu}`）做校验，命中异常值直接降级为失败，而不是只判断列表非空就算通过。

## 每设备优先选择磁盘占用最小的候选模型（提高多类型同时加载被有效验证的概率）
- **动机**：早期 `_run_multi_model_suite` 里阶段3入口模型（`_pick_stage3_model`）与 `models_by_device`（驱动 `ensure_multi_backend_loaded`/两两降级）都简单取“模型目录名字母序中第一个匹配该设备/该后端类型的模型”，与“哪个候选内存占用最小”无关。当某设备恰好有多个候选（典型是 NPU/QNN）时，字母序选中的可能是偏大的模型，不必要地挤占留给 GGUF/MNN 的可用内存，让“多类型同时加载”更容易因内存不足只能验证到 skip。
- **实现**：新增模块级函数 `_estimate_model_dir_size_bytes(models_root, model_name)`，递归汇总模型目录下全部文件大小之和，作为内存占用的粗略代理指标（仅用于测试脚本选型，不影响服务端真实内存预检查）。`_pick_stage3_model`/`models_by_device` 构建逻辑改为：本地模式下同一设备/同一“非 GGUF/MNN”候选集合中优先选估算值最小的模型；远程模式无法访问对端磁盘文件大小，退化为“字母序第一个发现”。
- **效果（已实测）**：本机模型集下 NPU 设备原会选 `phi4-v81`（约 5.24GB），改为选 `phi4-v81-i`（约 3.41GB，同类最小），为动态加载 GGUF/MNN 腾出更多内存；GPU（`gpt-oss-20b-GGUF`）与 CPU（`gpt-oss-20b-MNN`）各只有 1 个候选，选型不变。这是“能选的都选最省内存的那个”，是最大化“至少两两可用”这条底线被真正测试到的概率的启发式优化，而非保证“内存约束一定消失”——某设备只有 1 个候选或候选本身就大到怎么搭配都不足（如本机 `gpt-oss-20b-MNN` 预估约 18.7GB）时仍会合理判为 `skip`。
- **不应有的误解**：这不是“记住之前验证过能同时加载的组合”这种带记忆/持久化状态的机制，而是每次运行时用磁盘大小这个静态、可复现的代理指标做一次性选型；同一模型目录集合下多次运行选型结果确定性。

## 两两降级验证（三后端因内存不足无法同时驻留时的兜底能力）
- **动机**：当 `ensure_multi_backend_loaded`（依次加载 npu→gpu→cpu）未能让三设备全部同时驻留（`missing_devices` 非空，典型是先加载 qnn 再加载 gguf 后内存已耗尽、mnn 完全没被尝试），不能让 mnn 就此完全没被验证——设计底线是“至少任意两个后端可同时驻留”，需显式验证并在报告中体现。
- **实现**（`MultiModelTester.ensure_pairwise_backends_loaded`，仅本地模式）：对 `npu+gpu`/`npu+cpu`/`gpu+cpu` 三种组合逐一验证——每个组合测试前用该组合第一个设备对应的模型作为入口重启服务（获得干净内存基线），再动态路由加载第二个设备的模型，最后查询 `/models` 确认两设备是否都在已加载列表。返回 `(results, matrix, extra)`，`matrix[pair_key]` 为 **`"pass"`/`"fail"`/`"skip"` 三态字符串**：环境缺模型、以及入口/追加模型因服务端优雅拒绝（`failure_reason=insufficient_memory`）未能加载，统一归 `"skip"`（非代码缺陷）；重启失败/请求过程中进程崩溃精确标记 `"fail"` 且 `crashed=True`（崩溃永不豁免）；非崩溃但未成功路由/未达预期驻留状态同样标 `"fail"`。
- **判定标准**：三后端已全部同时驻留成功时两两组合天然成立（`"pass"`），跳过独立验证；否则要求全部“可测试”的两两组合（排除因缺模型或内存不足 `"skip"` 的组合）都验证通过才算“至少两两可用”达标——任意可测试组合判 `"fail"` 都是需要关注的真实问题。远程模式或未提供本地模型根目录时无法重启服务，精确标 `skipped=True`（而非悄悄跳过不留痕迹）。
- **结果汇总**：`MultiModelTester._build_coexistence_matrix_result` 把“三后端同时驻留”与“三种两两组合”的最终状态整理为 `coexistence_matrix` 结构（`{"triple": {...}, "pairs": {"npu+gpu": {...}, ...}}`），`reason` 是主报告用极短标签（如“内存不足,已跳过”），`detail` 是完整原始说明，写入 `MULTI: backend coexistence matrix (triple + pairwise fallback)` 单条汇总 `TestResult.response_data`。
- **展示已拆分为“主报告摘要 + 独立详情页”两层**：`report.html`“多类型同时加载”区块只渲染 `reason` 短标签 + 已加载 backend/device 单张对齐表格 + 指向详情页的链接；每个组合的完整 `detail`（原始错误文本）与逐项检查记录表格移到独立生成的 `report_multi_backend_coexistence.html`（`ReportGenerator._generate_multi_backend_detail_html`），避免高密度原始文本挤进主报告。
- **已知局限**：两两降级验证期间的服务重启会启动全新进程（新 PID），而阶段3的 `PerfMonitor` 仍绑定重启前旧 PID，因此两两降级阶段采集到的 CPU/内存样本不代表新进程真实占用——这是“MNN OOM 自动重启”场景就已存在的既知局限，本兜底验证复用同一限制。
- **已知局限：两两降级循环结束后服务最终运行状态由“最后一个被尝试的组合”决定，会影响 `run_all()` 后续所有检查看到的模型数量**（`PAIRWISE_COMBOS = (("npu","gpu"), ("npu","cpu"), ("gpu","cpu"))`，最后是 `gpu+cpu`）。若最后一个组合的第二个模型（`cpu`/MNN）因 `insufficient_memory` 加载失败，循环直接 `continue`，进程里只剩最后那次重启的入口模型（`gpu`，如 `gpt-oss-20b-GGUF`）在跑；紧接着的 `_get_loaded_models()`（驱动 `GET /models`、逐模型路由验证、`test_concurrent_requests`）看到的就是这个“只剩 1 个”的残留状态，即使更早的某个组合曾同时加载过 2 个模型。**这不是代码缺陷，是重启序列设计的必然结果**：“已加载 backend/device”表格能同时展示 2 个模型，是因为它汇总的是本轮全部 `TestResult.response_data["loaded_models"]` 的并集（历史上任意时刻出现过的模型），而非循环结束时刻的最终状态；两者口径不同，报告里出现“共存矩阵/已加载表显示 2 个模型”而“并发测试因只有 1 个模型被跳过”并不矛盾。若日后想让逐模型路由/并发测试覆盖到曾短暂加载过的全部模型，需改造成“记录并集”而非“只看循环结束时一次查询”，属行为增强。
- **失败原因文案必须区分“真崩溃”与“非崩溃优雅失败/跳过”**：`ensure_pairwise_backends_loaded` 除三态 `pair_matrix` 外还返回第三个值 `pair_extra`（`{"npu+cpu": {"crashed": bool, "reason_detail": str, "skip_reason": "missing_model"/"memory"/None}, ...}`，仅在组合不是 `"pass"` 时才有条目），记录该组合是入口/追加模型请求本身触发了进程崩溃（`r_entry.crashed`/`r_second.crashed`/重启失败，均已产生 `CrashEvent`），还是进程从未崩溃、只是服务端优雅拒绝/缺模型/未成功路由。`_build_coexistence_matrix_result` 据此生成的 `matrix["pairs"][pair_key]["detail"]` 只有在 `crashed=True` 时才在详情页引用“崩溃事件日志”（该区块必然渲染，因已产生 `CrashEvent`）；非崩溃场景直接给出 `reason_detail` 真实原因并指向“逐项检查记录”表格。新增/修改任何“组合失败”类展示文案前，都要先确认引用的区块在这次结果下**真的会渲染**。
- **【硬性规则】`_multi_model_`（多模型并发聚合占位 `model_name`）绝不允许作为字面文本出现在 `report.html`/`report_<model>.html`/`report_multi_backend_coexistence.html` 里**：它只是内部过滤用占位符，历史上曾反复以“模型名”身份泄漏进性能对比表、模型链接卡片等用户可见位置（根因是排除逻辑在 `generate_summary_html`/`generate_model_reports` 两处各自手写、新增占位名易漏改一处）。现已收敛为 `ReportGenerator.INTERNAL_PLACEHOLDER_MODEL_NAMES`/`_is_internal_placeholder_model()` 单一来源 + `_assert_no_placeholder_leak()` 写盘前兜底扫描（命中即抛异常阻断生成，新增详情页写盘前也同样接受此扫描）。以后新增/修改任何多模型聚合结果展示逻辑，都不要再手写排除元组。
- **“逐项检查记录”表格新增“说明”列**，把每条 `MULTI: ...` 检查项名称翻译成人类可读解释（如 `MULTI: backend coexistence matrix (triple + pairwise fallback)` → “汇总三后端同时驻留与三种两两降级组合的验证结果”）：实现是 `ReportGenerator._describe_multi_check(name)`（先查精确匹配表 `_MULTI_CHECK_DESCRIPTIONS_EXACT`，再查前缀匹配表 `_MULTI_CHECK_DESCRIPTIONS_PREFIX`，未命中留空而不报错）。新增该阶段任何检查项都必须同步补充映射。
- **“逐项检查记录”表格按类别分组展示**：`ReportGenerator._categorize_multi_check(name)`（与 `_describe_multi_check` 同一套“精确优先、前缀兜底”实现）把每条检查项归入 `_MULTI_CHECK_CATEGORIES` 固定的四个分类（单模型路由验证/后端共存与两两降级验证/通用接口与异常路由/并发与故障恢复），各分类渲染为独立 `.group-section`，未归类项落入兜底分类 `其它检查项`。新增检查项时应同步在 `_MULTI_CHECK_CATEGORIES` 归类。

## 测试覆盖审计遗留建议（`MultiModelTester` 侧，未实施，需用户确认后再动手）
1. 驱逐生效性反向验证缺失：从未在触发同设备驱逐后，立刻对被驱逐的旧模型重新发请求，实证注册表确实已清空、旧模型需重新走“磁盘扫描+动态加载”路径，而不是命中残留脏缓存条目。
2. 加载顺序敏感性完全未测试：`models_by_device` 的迭代顺序完全由发现顺序决定，从未反转顺序重跑验证“无论先加载谁，最终共存结果都一致”。
3. 驱逐后资源释放缺乏量化验证：`UnloadModelsByDevice` 声称析构会在返回前完成、内存立即释放，但没有测试在驱逐前后用 `PerfMonitor` 采样对比进程内存。
4. 同设备驱逐时“请求进行中”的竞态场景未测试：从未构造“模型 A 推理进行中，另一请求同时触发同设备驱逐 A 并加载 B”的场景来验证 `shared_ptr`/`models_mutex_` 的理论安全性。
5. 可选：针对“极端高频连续切换后请求挂起”问题，补一个独立的、明确标注“压力测试/非常规回归强度”的专项 case，用于长期跟踪。
6. **同一 GGUF backend 同时以 GPU 和 CPU 两种 device 分别加载并同时驻留的场景未测试**：现有“两两降级验证”的 `gpu+cpu` 组合在本机模型集下通常是 GGUF(gpu) + MNN(cpu)，从未验证“两个 GGUF 实例分别指定 `device=gpu`/`device=cpu` 同时驻留”——这与“同一设备同一时刻只驻留一个模型实例”的限制不冲突（gpu/cpu 是两个不同 device slot），但 llama.cpp 引擎内部是否存在跨实例共享的全局状态（线程池、内存分配器、模型加载锁）在这种“同 backend 不同 device 并发”搭配下的行为尚无实测证据。需额外配置一个显式 `backend=GGUF, device=cpu` 的模型条目来验证：两者能否成功同时加载、`GET /models` 是否正确分别列出两个 `device` 值、并发请求两者是否互不干扰/不崩溃。
