# QNN 后端上下文（`context/qnn/`）

本目录（`genie.cpp`/`genie_interface.cpp` 等）QNN/Genie 后端相关的易错点与设计。跨后端的 OOM 表现对比、多模型并发加载等项目级设计见 `docs/architecture.md`；测试侧对崩溃/模型能力问题的分类判定见 `test/test_service.md`。

## QNN 后端上下文超限的优雅处理（`GENIE_STATUS_WARNING_CONTEXT_EXCEEDED`）

- QNN/Genie SDK 对话推理接口（`GenieDialog_query`/`GenieDialog_embeddingQuery`）在模型输出耗尽真实上下文窗口时，会返回官方文档明确定义的常量 `GENIE_STATUS_WARNING_CONTEXT_EXCEEDED`（值为 4，`GenieCommon.h`，归类为 WARNING 而非 ERROR，语义为 "Context Limit was exceeded"）——这是一个**合法的、预期内**的 SDK 返回值，不是未定义/异常状态。
- `GenieContext::inference_thread()`（`genie.cpp`）把该状态与既有的 `GENIE_STATUS_WARNING_ABORTED` 同等对待：不设置 `inference_succeed_ = false`，`Query()` 仍走正常成功路径，已生成内容（`m_stream_answer` 里尚未被回调消费的部分）按原有逻辑原样返回给调用方——非流式场景下客户端收到 `200` + 已生成的完整内容，流式场景下收到正常的流结束，不再是之前“整体丢弃、返回 500”的处理方式。同时设置 `stopped_by_output_limit_ = true`，使 `response_dispatcher.cpp` 正确报告 `finish_reason="length"`。
- 这类情况本质上是**模型输出质量的缺陷**（模型没能在耗尽真实上下文前主动收尾，常见诱因是陷入某种“结构重复但内容不断变化”的幻觉式枚举循环——例如反复罗列一张发票的更多条目、条目编号持续递增但每次文字不完全相同，因此不能靠“完全相同文本重复”这类精确文本匹配去提前识别，见下方教训），而不是服务端代码缺陷，因此仍以 `kError` 级别打一条统一格式的日志：
  ```
  [MODEL_DEFECT] reason=sdk_context_exceeded model=<model_name> token_size=<N>
  ```
  （`genie_interface.cpp` 里既有的、按估算 token 数主动喊停的分支——`cur_length_ >= kContextSize_`——同样补充了一条 `reason=self_estimated_limit_exceeded` 的 `[MODEL_DEFECT]` 日志，区分“SDK 自己报告真实超限” vs “我们自己的估算提前触顶”两种不同触发源）。`test_service.py` 侧按字节偏移扫描服务 stdout 日志里新出现的这个标记（`_scan_model_defect_log`/`_scan_model_defect_text`），命中后把结果标记为 `model_capability_issue=True`（与既有的 tool_calls 模型能力局限诊断共用同一对字段），不影响 `passed`/退出码——与测试侧“模型输出质量问题不应算作服务缺陷”的判定原则一致，详见 `test/test_service.md`。

## 教训：不要用逐字滑动窗口去预测模型失控

早期修复尝试过给流式回调加一套“检测到完全相同的文本片段连续重复就提前喊停”的滑动窗口算法（仿照测试脚本自身用于诊断的 `_detect_repetition`），但真实复现（`qwen2.5_omini_8480-2.42` + 特定发票图片）证明该模型的失控模式是“结构重复、具体文字不完全相同”（如条目编号递增），逐字精确匹配的滑动窗口无论把窗口开多大都无法捕捉到这种模式，因此该方案已被放弃并从代码中彻底移除（C++ 侧与 `test_service.py` 侧均已清空，不留残留字段）——**接受 SDK 自身报告的失败状态、优雅处理其结果，比试图在客户端预测各种可能的模型失控模式更稳健**。
