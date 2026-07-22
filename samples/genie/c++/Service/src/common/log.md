# `log.h` 日志级别默认值与 `kDebug` 可见性

> 本文件是 `src/common/log.h` 的文件级归口，只覆盖 `My_Log{}` 的默认级别与 `kDebug` 在 exe/dll 两种形态下的可见性机制。项目里三组互不重叠的独立调试开关字段（`CloudModelConfig::log_debug` / `EnterpriseCloudModelConfig::log_debug`、`ResponseTools::log_inference_stream`、`sensitivity_detection.debug_log_matches`）为何不能当作重复逻辑合并，属项目级“为什么”，见 `docs/architecture.md` 5.3 第二段。

### `My_Log{}` 默认级别是 `kWarning`，不是 `kInfo`
`My_Log{}` 不显式指定级别时，默认级别是 `kWarning`（`src/common/log.h` 中 `explicit My_Log(Level lev = kWarning)`），不是 `kInfo`——审查任何一行裸写 `My_Log{} << ...` 的日志时不要凭直觉假设它是低优先级输出，它一直以 `kWarning` 级别打印，比看起来的更显眼。

### `kDebug` 在 exe 与 dll 下都默认不可见，但生效路径不同
`kDebug` 级别在两种编译形态下都默认不可见，但生效路径不同：`GenieAPIService.exe` 默认日志级别来自 `-d/--loglevel` CLI 选项默认值 `kWarning`（数值 2），`kDebug`（数值 4）大于该阈值会被过滤；`GenieAPILibrary.dll`（库模式）默认更严格——`log_level_` 默认 `-1` 时 `My_Log::Init` 从不会被调用，`Level_` 保持初始值 `kAlways`（数值 0），过滤比 exe 模式更严格。
