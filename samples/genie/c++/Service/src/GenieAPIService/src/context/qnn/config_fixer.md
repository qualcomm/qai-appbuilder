# `config_fixer.h`：`dialog.engine` 单引擎 / eaglet 多引擎兼容

`ConfigFixer::FixConfig()` 负责重写模型 `config.json` 里 `dialog` 相关的路径字段。改动这里前必须先理解 `dialog.engine` 的两种形态。

## `dialog.engine` 既支持单引擎 object，也支持 eaglet（投机解码）多引擎 array

- **待修复路径拆成两组**：相对 `/dialog` 的 global_items（`tokenizer`/`forecast`/`lut-path`，只修一次）与相对单个 engine 对象的 engine_items（`extensions`/`ctx-bins`/`poll`/`draft-token-map`）。
- **object vs array 判定**：写入前先判定 `/dialog/engine` 是 object（legacy 单引擎）还是 array（eaglet，每个元素带 `role`：`target`/`draft`）。
- **array 时逐引擎 rebase**：array 时把 engine_items 的相对指针依次 rebase 到 `/dialog/engine/<i>/...` 对每个引擎单独修复；`use-mmap`/`allow-async-init` 注入同样按引擎逐一加 `contains()` 判定后写入。
- **`draft-token-map` 只在 `draft` 角色上存在**：`target` 角色缺失时按可选项静默跳过（非缺陷）。
- **向后兼容**：单引擎场景走原有行为，完全向后兼容。
