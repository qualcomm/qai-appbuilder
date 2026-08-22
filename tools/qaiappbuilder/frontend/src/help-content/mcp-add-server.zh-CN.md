# MCP 服务器：添加与排查

**MCP**（Model Context Protocol）是一种让 AI 助手连接外部工具与数据源的开放协议。每个 MCP 服务器都是一个独立的进程，QAI ModelBuilder 通过 stdio 或 HTTP 通道与它对话，把它暴露的工具装载到当前会话可用的工具集里。

## 添加一台服务器

打开 **+ 添加**（Add）按钮进入表单，逐项填入下面的字段：

![MCP 添加表单示意](/help-images/mcp-add-server/wireframe.svg)

- **Name**：给这台服务器起一个易读、独一无二的短名（例如 `filesystem-home`）。它只在本机显示，不会发到远端。
- **Command**：可执行文件名或绝对路径。必须存在于当前 PATH，或者填**完整路径**（例如 `C:\Program Files\nodejs\node.exe`）。
- **Arguments**：命令行参数，一行一个更清晰。常见示例：`-y @modelcontextprotocol/server-filesystem C:\Users\me\projects`。
- **Environment variables**：如需给子进程注入变量，按 `KEY=VALUE` 一行一条填写。密钥类变量不会被记录到日志。

保存前请先点击 **🔌 测试连通性**：QAI ModelBuilder 会尝试启动该进程、握手、列出它提供的工具，并把握手错误直接回显在按钮下方。

## 连不通？按此排查

![MCP 连通性排查流程图](/help-images/mcp-add-server/troubleshoot-flowchart.svg)

1. **命令在 PATH 中吗？** 打开新终端输入 `where <命令>`（Windows）或 `which <命令>`（macOS/Linux）。找不到就装上或改成绝对路径。
2. **子进程真的启动了吗？** 观察面板底部的日志区，若看到 `ENOENT` / `command not found` 属第 1 类；看到 `Server exited` / `handshake timeout` 通常是**参数错误**或依赖缺失（例如 `npx` 首次运行时下载超时）。
3. **企业代理拦截了外网？** MCP 官方 registry 里的服务器初次运行会拉包。请在系统环境变量里设置 `HTTPS_PROXY` / `HTTP_PROXY`，或者把这些变量填进本服务器的 env vars 后再重启。
4. **仍然失败？** 记录下"测试连通性"里返回的最后 20 行日志，附到反馈或 issue 中。

## 常见错误码

| 现象 | 含义 | 处理 |
| :--- | :--- | :--- |
| `ENOENT` | 找不到可执行文件 | 检查 Command 拼写 / PATH / 绝对路径 |
| `handshake timeout` | 进程启动了但未在 5 秒内回握手 | 通常是首次下载慢，重试；或参数不对 |
| `EACCES` | 权限被拒绝 | Windows：检查杀软；\*nix：`chmod +x` |
| `port in use` | HTTP 型服务器端口冲突 | 换端口 / 关掉占用端口的进程 |

> 建议先从官方 registry（**Filesystem** / **GitHub** / **Fetch**）挑一个试通，再拓展到自研或第三方服务器。
