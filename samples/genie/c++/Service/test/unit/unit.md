# `test/unit/`

> QAIModelBuilder 相关一切 pytest 单测的**唯一放置位置**（唯一规则，见 `.junie/playbook.md` 第 2/4 章）——不得在 `QAIModelBuilder/` 内部另建 `tests/` 目录。子目录结构镜像被测源码路径：`test/unit/apps/cli/` ↔ `QAIModelBuilder/apps/cli/`，`test/unit/qai/platform/config/` ↔ `QAIModelBuilder/src/qai/platform/config/`；新增测试时延续这个镜像约定，不要另起结构。

## 与 `test_service.py`/`test_builder_cli.py` 的本质区别：白盒，非黑盒，未与 QAIModelBuilder 解耦
本目录下的测试直接 `import apps.cli.*`/`qai.platform.*` 等 QAIModelBuilder 内部包，不是 `test_builder_cli.py` 那种子进程 + HTTP 驱动的黑盒集成测试。**物理位置搬到 `test/` 下，并不等于脱离了 QAIModelBuilder**：运行时仍必须使用 QAIModelBuilder 自己的专属 venv（由 `QAIModelBuilder/Setup.bat` 搭建），因为这些测试依赖该 venv 里安装的 `rich`/`prompt_toolkit`/`pytest-asyncio` 等包，以及该 venv 对 `QAIModelBuilder` 的 editable 安装（把 `QAIModelBuilder` 项目根与 `src/` 加入 `sys.path`）——Service 项目自身不装、也不需要装这些 Python 依赖。

## pytest 配置归属：`QAIModelBuilder/pyproject.toml`，本目录不放配置文件
`[tool.pytest.ini_options]`（`testpaths = ["../test/unit"]`，以及全部 `markers`/`filterwarnings`/`asyncio_mode` 设置）唯一定义在 `QAIModelBuilder/pyproject.toml`；本目录本身不放 `conftest.py`/`pytest.ini`。QAIModelBuilder 官方 venv 的固定探测路径见 `docs/build-guide.md`。

**推荐用法（不依赖 `QAIModelBuilder` 目录联接是否存在，稳定可靠）**：显式传路径 + `-c` 指定配置文件，从 `Service/` 根目录跑：
```
<venv>\Scripts\python.exe -m pytest -q -c QAIModelBuilder\pyproject.toml test\unit
```

**已知坑点：`testpaths = ["../test/unit"]` 裸跑（不带路径参数）依赖通过目录联接访问，走真实路径会失败**——`QAIModelBuilder` 是指向 `tools\qaimodelbuilder` 的 NTFS 目录联接（junction，非 git 跟踪，重新 clone/`git clean -fd` 后可能消失，需要 `mklink /J` 手动重建），pytest 把 `testpaths` 里的相对路径按"当前 cwd 字符串本身"（不解析联接）拼接：只有 `cd` 进联接别名 `Service\QAIModelBuilder` 再跑裸 `pytest -q`，`../test/unit` 才能解析到正确的 `Service/test/unit`；`cd` 进它背后的真实路径 `tools\qaimodelbuilder` 时，`../test/unit` 会解析成不存在的 `tools\test\unit`，导致收集不到任何用例——又因为 `pyproject.toml` 里 `filterwarnings = ["error", ...]` 把 pytest 自身的 `PytestConfigWarning`（"No files were found in testpaths"）也升级成硬错误，表现为直接抛异常退出而不是安静地退化成空测试集。**因此不要依赖裸 `pytest` + `testpaths` 自动发现，优先用上面「推荐用法」的显式路径形式。**

## 目录内容
- `apps/cli/`：`qai` CLI 渲染层（`_render.py`/`_repl.py`/`_pager.py`）、会话落盘日志、默认对话入口（`commands/chat.py`）与只读工具桥接（`_chat_tool_bridge.py`/`_chat_build_tool_bridge.py`）的单测。
- `qai/platform/config/`：`DataPaths`（如 `cli_sessions_dir`）相关单测。

## 健康判据
与项目其余测试体系一致：全部通过即视为健康；任何非预期的 `failed`/`error`（含 collection error）都需要排查，不豁免。
