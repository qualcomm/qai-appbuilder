# gateway/ — 区域已知问题

> 本文档覆盖 `gateway/gateway/` 目录下跨多个文件共享的已知缺陷。单文件专属问题见对应的 `<basename>.md`（如 `gateway_cloud.md`/`gateway_routing.md`）。

## 【OPEN / 未解决】sticky session 淘汰不是真正的 LRU

**状态：仍未解决的已知缺陷，留待后续单独评估是否统一为正确的 LRU。**

`gateway_routing.cpp`/`gateway_overflow.cpp` 中 sticky session 淘汰不是真正的 LRU：两文件共 4 处（`gateway_routing.cpp` 约第 1008-1027 行；`gateway_overflow.cpp` 约第 218-244、628-643、818-834 行）在 `sticky_sessions_` 超过容量上限时都用 `erase(sticky_sessions_.begin())` 淘汰——这只是 `unordered_map` 的任意桶顺序，并非最久未使用。

对照实现：`gateway_incremental.cpp` 约第 223-234 行为 `session_security_states_` 专门实现了正确的 LRU 淘汰（遍历找 `last_updated` 最早者）。同一类“容量上限淘汰”问题在代码库中存在两种不同、其中一种有缺陷的实现。
