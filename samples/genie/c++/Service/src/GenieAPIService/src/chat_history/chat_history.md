# chat_history 文件级说明

> 本文件是 `chat_history.cpp`/`.h` 的就近说明，只承载与该文件强绑定的已知行为性问题。测试侧覆盖见 `test/test_service.md`。

## OPEN / 已知行为性问题：`ChatHistory::Limit()` 从未被调用，`-n` 数值不参与裁剪

> 状态：OPEN（真实的行为性问题，文档语义与实现不符）——按既有规范只记录、不修复、不新增测试，留待后续单独立项评估是否修复。

- **`ChatHistory::Limit(size_t max_size)`（`chat_history.cpp:191-198`）从未被任何代码调用，`-n` 的具体数值本身从未参与裁剪运算**。
- CLI 帮助文本 `-n/--num_response: The number of rounds saved in the historical record`（`config.h:79-80`）描述的“历史记录保存 N 轮”语义并未真正实现——非 `-1` 模式下无论传 `5`/`30`/`1000`，历史都无限增长、从不裁剪。
- `GetUserMessage()` 内部日志已自行承认 `"returning all messages (no compression)"`。
