# MCP servers: adding and troubleshooting

**MCP** (Model Context Protocol) is an open protocol that lets an AI assistant plug into external tools and data sources. Each MCP server is a separate process; QAI ModelBuilder talks to it over stdio (or HTTP), and loads its tools into the current chat session's toolset.

## Adding a server

Click **+ Add** to open the form and fill in each field:

![MCP add-server form wireframe](/help-images/mcp-add-server/wireframe.svg)

- **Name**: a short, unique display name (for example `filesystem-home`). Local-only — never sent to any remote.
- **Command**: the executable name or an absolute path. Must exist on your current `PATH`, or provide the **full path** (e.g. `C:\Program Files\nodejs\node.exe`).
- **Arguments**: the command-line arguments. One per line reads best. Example: `-y @modelcontextprotocol/server-filesystem C:\Users\me\projects`.
- **Environment variables**: if the process needs env vars, add them as `KEY=VALUE` (one per line). Secret-looking values are not logged.

Before saving, click **🔌 Test connectivity**. QAI ModelBuilder will spawn the process, complete the handshake, list the tools it advertises, and echo any handshake error just under the button.

## Not connecting? Walk this checklist

![MCP connectivity troubleshoot flowchart](/help-images/mcp-add-server/troubleshoot-flowchart.svg)

1. **Is the command on `PATH`?** In a fresh terminal, run `where <command>` (Windows) or `which <command>` (macOS/Linux). If nothing prints, install it or switch to an absolute path.
2. **Did the child process actually start?** Watch the log area at the bottom of the panel. `ENOENT` / `command not found` → category 1. `Server exited` / `handshake timeout` → usually **wrong arguments** or a missing dependency (e.g. `npx` slow first-time downloads).
3. **Is a corporate proxy blocking the internet?** Servers from the official MCP registry may fetch packages on first run. Set `HTTPS_PROXY` / `HTTP_PROXY` in your OS environment, or put them into this server's env vars and restart.
4. **Still failing?** Copy the last ~20 lines from the "Test connectivity" output and attach them to your bug report.

## Common error codes

| Symptom | Meaning | Fix |
| :--- | :--- | :--- |
| `ENOENT` | Executable not found | Check Command spelling / PATH / use absolute path |
| `handshake timeout` | Process started but did not respond within 5 s | Usually slow first-time download; retry, or fix args |
| `EACCES` | Permission denied | Windows: check antivirus. *nix: `chmod +x` |
| `port in use` | HTTP-mode server port conflict | Change port / stop the other listener |

> Tip: start with one server from the official registry (**Filesystem**, **GitHub**, or **Fetch**) to confirm the toolchain works, then move on to bespoke or third-party servers.
