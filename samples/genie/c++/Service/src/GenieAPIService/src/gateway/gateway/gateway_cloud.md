# gateway_cloud.cpp — 云网关脱敏还原流式重建机制

> 本文档是 `gateway_cloud.cpp` 云端流式脱敏还原这条路径的完整归口。协议层的“为什么”（tool_calls 的服务端协议实现、流式 vs 云网关两条路径的本质区别）属于项目级架构知识，见 `docs/architecture.md`（5.5）。

## 机制：`enable_stream_restore` 驱动的流式脱敏还原缓冲（`gateway_cloud.cpp:682-786`）

`enable_stream_restore`（由 `desensitization.restore_response_enabled` 与 `restore_stream_enabled` 共同决定）驱动的一套流式脱敏还原缓冲机制：云端模型响应里的敏感内容（如真实文件路径）在发送前已被替换为 mock 值，流式返回时需要把 mock 值还原回真实值，且要正确处理 `delta.tool_calls` 里 `function.arguments` 跨 chunk 边界被拆散的情况（`args_buffers`，第 1008-1011 行）。

## 三个触发前提（三者同时成立才会执行）

`routing.enabled=true` + 请求被路由到一个真实配置的云端模型 + 脱敏还原开关（`restore_response_enabled`/`restore_stream_enabled`）同时打开。当前测试环境没有云模型凭据和路由配置，无法真正复现这一精确场景。

## finish-chunk 合并修复迹象（`gateway_cloud.cpp:784-786`）

第 784-786 行注释直接写明——在 flush 前，先把当前 finish chunk 自身携带的 args 片段合并进缓冲区，否则 finish chunk 的最后一段 args 会丢失。这与本项目历史上出现过的“工具调用参数在流式传输末尾被截断”问题高度吻合，代码里已存在这段专门为此设计的合并逻辑，说明该问题很可能已被修复。

## 当前测试的替代覆盖方式

本项目在当前环境唯一可达的本地模型（qnn/mnn/gguf）路径上有 `test_tool_call_streaming_argument_integrity_probe`（见 `test/test_service.md` 的 tool_calls 协议测试覆盖），但判定标准只针对与内容无关的机制事实（聚合到的分片数是否 >1、拼接结果是否结构完整），不再对模型复述的具体文字逐字比较——这不是对原始云网关事件的直接复现（本地路径当前实现是整段 `tool_calls` 一次性发送，从不经过 `args_buffers` 这条跨 chunk 拼接逻辑，见 `docs/architecture.md` 5.5），而是同一份测试代码在可达路径上按机制事实做的预防性观察；若日后真的接入云端凭据、请求被路由到这条真正会拆分参数的路径，同一份测试代码无需修改即可自然覆盖到拼接逻辑本身。
