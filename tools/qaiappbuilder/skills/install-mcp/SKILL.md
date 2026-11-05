---
name: install-mcp
description: Install, register, test and manage MCP (Model Context Protocol) servers, including installing a local Python .whl file as an MCP server. Use the mcp_manage tool.
tags: mcp, install, wheel, whl, server, model context protocol
use_for: Installing an MCP server from a local .whl file, adding or removing an MCP server, enabling or disabling an MCP server, checking why an MCP server is not connected, turning the MCP master switch on
---

# Install / manage an MCP server

Use the **`mcp_manage`** tool for everything here. Do NOT hand-edit `mcp_servers.json`,
do NOT `pip install` into the host environment yourself, and do NOT call the
`/api/chat/mcp/...` HTTP endpoints with `exec` / `web_fetch` — `mcp_manage` does all of
that correctly (own venv, entry-point lookup, registration, connection test).

## Installing a local `.whl` as an MCP server

1. **Confirm with the user** (use the `question` tool) before installing anything:
   - the full path of the `.whl` file, and
   - the server name to register it under (suggest one derived from the package name,
     letters / digits / `_` `.` `-` only, e.g. `quts_skill_validator`).
   Installing a package runs third-party code on the user's machine — never skip this.
2. **Find the documented launch command.** Look for a README next to the `.whl`
   (`read` the folder / `README*.md`) and note the `command` + `args` of its MCP config
   example. Many stdio servers only work when started with a flag — typically
   **`--stdio`** — and registering without it is the most common reason a freshly
   installed server "does not connect". If the README simply is not there, try `--stdio`
   first. If reading it is **blocked** (`[FileGuard]`), follow the "blocked" rule in
   *When something goes wrong* below — stop and ask the user to authorize the folder.
3. `mcp_manage(action="status")` — see whether the MCP master switch is ON and whether a
   server with that name already exists (re-installing the same name upgrades it).
4. `mcp_manage(action="install_wheel", name="<server name>", wheel_path="<absolute path to .whl>", args=["--stdio"])`
   - Use the `args` you found in the README (not always `--stdio`); environment variables
     the server needs go in `env={...}`.
   - **Large results:** results over the model's normal per-result budget (30K characters
     for qwen/deepseek/glm, 50K for claude/gpt) are cut head+tail and the full text is saved to
     a file named in the footer (`[full_output_saved] path=...` -> `read` it). For a server whose
     tools return whole documents, pass `max_result_chars=<n>` (10000-2000000) so they arrive
     intact; leave it unset otherwise.
   - **Timeout:** the server is registered with `timeout_s=600` (the maximum): each request may
     stay silent that long, so long-running tools are fine. Connecting / listing tools is always
     cut off at 120s, so a server that starts but does not speak MCP (e.g. missing `--stdio`)
     fails within about two minutes, not ten. Only pass `timeout_s` yourself if the README says
     otherwise.
   - A wheel can ship several console scripts (a server, a REST API, a CLI, ...). The tool
     picks the obvious MCP server itself; if it answers that the choice is ambiguous, ask
     the user which one, then repeat with `entry="<script>"`.
5. **If the result says the master switch is OFF**, the server was saved but not started.
   Explain that MCP is disabled by default for safety, then **ask the user** (`question`)
   whether to turn it on. Only if they agree: `mcp_manage(action="set_global_enabled", enabled=true)`.
   Never turn it on without asking.
6. `mcp_manage(action="test", name="<server name>")` — confirm it connected and list its tools.
7. Tell the user the result. **A newly connected server's tools become callable from the
   NEXT turn**, not this one — say so, and use them in the following message.

## Other actions

| Goal | Call |
|---|---|
| See everything | `mcp_manage(action="status")` |
| Register a server by hand | `mcp_manage(action="add", name=..., transport="stdio", command=..., args=[...])` (or `transport="http"/"sse"` with `url=...`) |
| Reconnect / diagnose | `mcp_manage(action="test", name=...)` |
| Turn one server off / on | `mcp_manage(action="disable"/"enable", name=...)` |
| Uninstall | `mcp_manage(action="remove", name=...)` |

## When something goes wrong

- **`[FileGuard]` / "blocked" / permission message while reading the `.whl`** — the file is
  outside the folders the app may access. **Stop.** Tell the user which path was blocked and
  ask them to authorize it in *Security → Allow Lists*, then retry. Do not copy the file
  elsewhere or use another tool to get around the block.
- **`install_wheel FAILED at step 'install_wheel'`** — read the quoted pip/uv output. Typical
  causes: a dependency has no wheel for this platform (e.g. native code on ARM64), or no
  network access to the package index. Report the actual error; do not guess.
- **`install_wheel FAILED at step 'resolve_entry'`** — either the wheel ships several console
  scripts and it is unclear which starts the MCP server (the message lists them: ask the user,
  then retry with `entry="<script>"`), or the `entry` you gave does not exist. Nothing was
  installed or registered yet.
- **Registered but `NOT connected`, with an `error:` line** — show the user that error. A
  handshake timeout usually means the program started but is not in MCP stdio mode (missing
  `--stdio`-style flag — re-run `install_wheel` with the right `args`), or it crashed on
  start (it may need an external program installed, which the README will mention);
  re-check with `action="test"`.
- **`tool call timed out after ...s` / `session was reset`** — the call ran longer than the
  server's `timeout_s`. The session was reset and the tool was **not** retried, because it may
  already have done part of its work. Tell the user, ask whether to check the result or run it
  again, and do not re-run it on your own.
- **Status shows `(disabled)`** — the per-server switch is off: `action="enable"`.
