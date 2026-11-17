# Agent CLI and SDK

The QAI AppBuilder chat agent can be driven from scripts and other tools
through two surfaces:

- CLI: `qai agent run`
- Python SDK: `qai.agent_sdk`

Both run one non-interactive user turn through the existing chat runtime
(same conversation, tab, streaming, provider and MCP wiring as the app). The
CLI is a thin argparse layer over the SDK, so new `qai` command groups can
reuse the SDK instead of building another agent stack.

## CLI

    qai --config <server.toml> agent run --model <model-id> --workspace <dir> --stdin --format json

`--model` is **required**. There is no default model: a scripted caller must
name the model it wants, because the configured default may have no
credentials on the machine running the command. Without it the command exits
with code 2 and `no model specified: pass --model <model-id>`.

Prompt sources are mutually exclusive: positional text, `--stdin`, or
`--prompt-file <path>`.

Options:

- `--workspace <path>`: directory exposed to the agent (default: cwd).
- `--connect-mcp`: connect the configured MCP servers before the run.
- `--client-turn-id <id>`: caller tag, stored in the user message metadata.
  It is not an idempotency key in this build.
- `--timeout-seconds <n>`: fail the command if the run does not finish in time.
- `--format text|json|ndjson`: `ndjson` streams one frame per line and ends
  with a `{"type":"result",...}` line.
- `--output-last-message <path>`: write the final assistant text to a file.
- `--no-provider-check`: skip the configured-provider preflight.

Credentials (provider API keys, qai-service JWT) come from the configuration
selected by `--config`; the command never logs in interactively.

Exit codes: 0 completed; 1 runtime failure, stream error, timeout or provider
preflight failure; 2 usage or input error (including a missing `--model`);
130 interrupted.

## Python SDK

    from qai.agent_sdk import AgentRunRequest, QaiAgentClient

    async with QaiAgentClient(config_file=r"C:\path\server.toml") as client:
        result = await client.run(
            AgentRunRequest(prompt="Hello", model="route-1", workspace=".")
        )

`AgentRunRequest.model` is required and raises `ModelRequiredError` when empty.

## Extending

Add further verbs as more `agent_sub.add_parser(...)` calls in
`apps/cli/commands/agent.py` and keep the exit-code contract above. Agent-only
mode (`extra["agent_only"]`) removes the Model Builder framing from the system
prompt; Model Builder workflows stay under `qai build`.
