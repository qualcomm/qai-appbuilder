# response_dispatcher 文件级说明

> 本文件是 `response_dispatcher.cpp`/`.h` 的就近说明，只承载与该文件强绑定的可复用修复模式与已知覆盖边界。tool_calls 协议本身的「为什么」（原「5. 架构与设计要点 > 5.5」）见 `docs/architecture.md`；`numResponse == -1` 三条行为分支同样见 `docs/architecture.md`；测试侧覆盖见 `test/test_service.md`。

## 可复用修复模式：全局句柄/全局单例被后加载模型覆盖

- **可复用的修复模式是 `ResponseDispatcher::GetEffectiveHandle()`**——优先取每模型独立的配置句柄，只有在单模型模式（无独立实例配置）时才回退到全局句柄，句柄为空时走结构化失败而不是继续解引用。
- 这个“每模型独立配置优先，全局配置只做单模型模式兜底”的模式，值得在类似“全局单例 vs 多实例”问题里复用。

## OPEN / 已知覆盖边界：畸形工具调用死循环检测机制未被自动化断言覆盖

> 状态：OPEN（已知关注点，非缺陷）——这是该机制本身的性质决定的覆盖边界。

- 畸形工具调用死循环检测机制位于 `response_dispatcher.cpp:623-670+`，围绕 `"unknow"` 兜底标记设计（协议“为什么”见 `docs/architecture.md`，原「5. 架构与设计要点 > 5.5」）。
- 该机制依赖模型输出真实的畸形 JSON（如字面换行符导致解析失败）才能触发，且需要连续命中 `kMaxConsecutiveUnknowToolCalls` 次才会真正引导模型退出循环，过于依赖不可控的模型行为，无法构造确定性输入必现触发。
- 测试侧（`test_tool_call_model_invocation_probe`/`test_tool_call_streaming_argument_integrity_probe`）只是把命中 `"unknow"` 标记的单次输出归类为 `skipped=True`，并未验证死循环检测本身的引导退出逻辑——这不是缺陷，是该机制本身的性质决定的覆盖边界（测试细节见 `test/test_service.md`）。
