# gateway_routing.cpp — 已知问题

> tool_calls 协议“为什么”与流式 vs 云网关路径区别属项目级知识，见 `docs/architecture.md`（5.5）。

## 【OPEN / 未解决】OpenClaw header 识别断层

**状态：仍未解决的已知缺口，非本仓库当前已修复问题。**

`GenieAPIService` 侧的 `DetectClientSource()`（`gateway_routing.cpp`）只认 `QAIAgentForge`/`OpenClaw` 两种取值，`ResolvePromptPolicy()` 的策略矩阵里 `OPENCLAW` 与 `UNKNOWN` 在全 `LOCAL` 后端目标下产出的策略完全一致——这个 GenieAPIService 自身的识别缺口本身仍未解决。

此前唯一间接触碰过这条路径的 `QAIModelBuilderIntegrationTester`（旧版 `--suite builder_integration`）已随该套件被彻底移除，当前的 `--suite builder_local_model` 专注本地模型加载链路，不再涉及 `X-Genie-Client`/OpenClaw 身份识别，因此这个缺口现在完全没有任何测试路径触及。

若要真正验证，需绕开 Builder 直连 `GenieAPIService` 并把路由目标切到云端，才能观察到真正不同的 `PromptProcessingPolicy`；是否需要新增这类专项验证待用户确认设计意图。
