MESSAGES: dict[str, str] = {
    "_meta.locale": "English",
    "cli.build.help.summary": "Model Builder interactive session (agentic model-conversion REPL)",
    "cli.build.help.description": (
        "Enter a Model Builder agentic chat session: use natural language plus slash "
        "commands to guide the cloud Agent through model conversion. --model-file "
        "specifies the model file(s) to convert (may be repeated); --llm picks the "
        "cloud LLM the Agent uses. Inside the session use /model /precision "
        "/dataset /mode /run to tweak parameters, or /help to list every command."
    ),
    "cli.build.help.model_file": (
        "Path to the model file to convert (→ tool_params.model_paths). May be "
        "passed multiple times. Note: this is the file being converted, not the "
        "Agent's LLM (that's --llm)."
    ),
    "cli.build.help.llm": "Cloud LLM id used by the Agent (→ model_hint). The CLI does not support local LLMs.",
    "cli.build.help.precision": (
        "Quantization precision (→ tool_params.quant_precision), e.g. fp16 or "
        "fp16,w8a8. Available levels: {levels}."
    ),
    "cli.build.help.dataset": "Calibration/evaluation dataset path (→ tool_params.dataset_path).",
    "cli.build.help.mode": "Initial mode: interactive (default) turn-by-turn; batch leans toward one-shot conversion.",
    "cli.build.help.resume": (
        "Resume an existing model-build session: pass a conversation-id to resume "
        "that specific session; without an argument, best-effort resume the most recent one."
    ),
    "cli.build.label.unset": "(not set)",
    "cli.build.params.summary": (
        "  Model file : {files}\n"
        "  Precision  : {prec}\n"
        "  Dataset    : {ds}\n"
        "  Mode       : {mode}"
    ),
    "cli.build.error.no_cloud_provider": "No cloud model detected. Run qai config setup first to configure a provider.\n",
    "cli.build.banner.ready": "Model Builder session ready.\n",
    "cli.build.banner.conv_id": "  Session id : {conv_id}\n",
    "cli.build.banner.agent_llm": "  Agent LLM  : {model_hint}\n",
    "cli.build.banner.hint": "Chat with the Agent in natural language, or use slash commands to adjust parameters (/help for the full list).\n",
    "cli.build.hint.turn_interrupted": "\n(current turn interrupted)\n",
    "cli.build.error.turn_failed": "\nTurn failed: {exc_type}: {exc}\n",
    "cli.build.model.current": "Current model file: {cur}\n",
    "cli.build.model.set": "Model file set: {files}\n",
    "cli.build.precision.current": "Current precision: {prec}\n",
    "cli.build.precision.invalid": "Invalid precision level: {invalid}; available: {levels}\n",
    "cli.build.precision.set": "Precision set: {prec}\n",
    "cli.build.dataset.current": "Current dataset: {ds}\n",
    "cli.build.dataset.set": "Dataset set: {ds}\n",
    "cli.build.params.header": "Current session parameters:\n",
    "cli.build.mode.current": "Current mode: {mode} (use /mode batch|interactive to switch)\n",
    "cli.build.mode.set": "Mode switched: {mode}\n",
    "cli.build.error.no_model": "No model file set. Use /model <path> first, or start with --model-file.\n",
    "cli.build.error.no_last_message": "No previous message to resend.\n",
    "cli.build.stop.requested": "Stop requested for the current turn.\n",
    "cli.build.clear.new": "Started a new session: {conv_id}\n",
    "cli.build.history.unavailable": "History reading is not wired up yet (CLI can't reach that use case).\n",
    "cli.build.error.history_failed": "Failed to read history: {exc_type}: {exc}\n",
    "cli.build.history.empty": "(no history messages)\n",
    "cli.build.status.unavailable": (
        "Run-status query is not wired up yet (depends on the Model Builder backend "
        "use case). Use /params to see current parameters.\n"
    ),
    "cli.build.workspace.unavailable": "Workspace view is not wired up yet (depends on the Model Builder backend use case).\n",
    "cli.build.promote.unavailable": (
        "Export/promote to pack is not wired up in the CLI yet. "
        "Once the conversion artifact is ready you can run: qai pack import <artifact>\n"
    ),
    "cli.build.cmd.help.help": "Show every command",
    "cli.build.cmd.help.model": "<path...> set/view the model file to convert",
    "cli.build.cmd.help.precision": "<csv> set/view quantization precision",
    "cli.build.cmd.help.dataset": "<path> set/view the dataset",
    "cli.build.cmd.help.params": "View current model/precision/dataset/mode",
    "cli.build.cmd.help.mode": "batch|interactive switch mode",
    "cli.build.cmd.help.run": "Kick off a standard conversion using current parameters",
    "cli.build.cmd.help.retry": "Resend the last user message",
    "cli.build.cmd.help.stop": "Abort the current turn",
    "cli.build.cmd.help.status": "View run status (not wired up yet)",
    "cli.build.cmd.help.workspace": "View the workspace (not wired up yet)",
    "cli.build.cmd.help.promote": "Export as pack (hints qai pack import)",
    "cli.build.cmd.help.history": "Print session history messages",
    "cli.build.cmd.help.clear": "Start a new session",
    "cli.build.cmd.help.exit": "Exit the session",
    "cli.build.run.head": "Please convert model file {files} into a format that runs on the NPU",
    "cli.build.run.precision_suffix": ", with quantization precision {precision}",
    "cli.build.run.dataset_suffix": ", using dataset {dataset} for calibration/evaluation",
    "cli.build.repl.goodbye": "Goodbye.\n",
    "cli.build.repl.ctrl_c_hint": "\n(press Ctrl+C again to exit)\n",
    "cli.build.prompt.repl": "build › ",
    "cli.config.provider.label.anthropic": "Anthropic (Claude)",
    "cli.config.provider.label.openai_compat": "OpenAI-compatible",
    "cli.config.provider.label.ollama": "Ollama",
    "cli.config.provider.label.generic_cloud": "Generic cloud",
    "cli.config.provider.prompt.type_header": "provider type: ",
    "cli.config.provider.prompt.type_choice": "Choose (Enter for anthropic) › ",
    "cli.config.provider.prompt.endpoint": "Endpoint (Enter for default {base_url}) › ",
    "cli.config.provider.prompt.default_model": "Default model (Enter for {default_model}) › ",
    "cli.config.provider.warn.saved_probe_failed": (
        "⚠ provider {provider_id} saved, but connectivity test failed: {error}\n"
    ),
    "cli.config.setup.error.needs_tty": (
        "qai config setup requires an interactive terminal; for scripted use run "
        "qai config provider add --type ... --api-key-stdin\n"
    ),
    "cli.config.setup.welcome": "Welcome to the QAIModelBuilder configuration wizard. I'll walk you through setup step by step.\n\n",
    "cli.config.setup.step1_header": "[1/2] Cloud model provider\n",
    "cli.config.setup.prompt.add_provider": "  Would you like to add a cloud AI model? (used by Chat / Model Builder Agent)",
    "cli.config.setup.prompt.add_another": "  Add another provider?",
    "cli.config.setup.step2_header": "\n[2/2] Default preferences\n",
    "cli.config.setup.prompt.language": "  Interface language [English] › ",
    "cli.config.setup.done": (
        "  ✓ Done! Configuration saved. Run qai config provider to review, "
        "or qai build / qai app to get started.\n"
    ),
    "cli.config.setup.skipped_no_id": "  (skipped: no provider id given)\n",
    "cli.config.setup.probing": "  ✓ Testing connectivity…\n",
    "cli.config.setup.probe_ok": (
        "  ✓ Saved provider {provider_id} ({status} OK, available models: {models})\n"
    ),
    "cli.config.setup.no_model_list": "(no model list)",
    "cli.config.setup.probe_failed": (
        "  ⚠ provider {provider_id} saved, but connectivity test failed: {error}\n"
    ),
    "cli.app.hint.input_kind": "  Input type: {kind} (use --{flag})\n",
    "cli.app.hint.variants": "  Available variants (--variant): {ids}\n",
    "cli.app.hint.params_header": "  Available parameters (--param key=val):\n",
    "cli.app.ok.wrote_artifact": "Wrote artifact: {dest}\n",
    "cli.app.ok.wrote_output_json": "Wrote output JSON: {dest}\n",
    "cli.app.ok.wrote_annotated": "Wrote annotated artifact: {dest}\n",
    "cli.app.error.no_annotated": "No annotated artifact produced (this Pack does not emit annotated images)\n",
    "cli.app.error.pack_not_found": "qai app: Pack '{pack}' not found. Run `qai app` (no arguments) to list available Packs.\n",
    "cli.app.hint.cancel_prompt": "\n(cancelled, /exit to quit)\n",
    "cli.app.hint.repl_banner": "App Builder session — Pack: {pack} (input type: {kind})\nType your input (path or text) and press Enter to run; /help for commands, /exit to quit.\n",
    "cli.app.error.file_missing": "File not found: {value}\nPlease enter a path that exists (you can drag the file into the terminal, or use Explorer's \"Copy as path\").\n",
    "cli.app.hint.turn_interrupted": "\n(this turn was interrupted)\n",
    "cli.app.ok.current_pack": "Current Pack: {pack}\n",
    "cli.app.ok.switched_pack": "Switched Pack: {pack}\n",
    "cli.app.ok.variant_set": "Variant: {variant}\n",
    "cli.app.hint.variant_default": "(default)",
    "cli.app.ok.params_set": "Parameters: {params}\n",
    "cli.app.ok.params_status": "Current parameters: {params}\nCurrent variant: {variant}\n",
    "cli.app.hint.no_examples": "This Pack has no built-in examples.\n",
    "cli.app.hint.examples_usage": "Use /examples <index> to run a built-in example,\nor /examples <index> <your file path> to run that example's parameters against your own input.\n",
    "cli.app.error.bad_example_index": "Invalid example index: {idx} (use /examples to list valid indices)\n",
    "cli.app.error.no_input_override": "This Pack's input type does not support command-line override; use /param to adjust parameters directly.\n",
    "cli.app.error.example_files_missing_header": "Sample files for built-in example [{idx}] are not bundled with the Pack:\n",
    "cli.app.error.example_files_missing_item": "  missing {m}\n",
    "cli.app.hint.example_override_usage": "You can run this example's parameters against your own file:\n  /examples {idx} <your file path>\nOr just type your file path at the prompt and press Enter.\n",
    "cli.app.error.history_unavailable": "Run history is unavailable (container has no list_runs wired up).\n",
    "cli.app.hint.no_history": "No run history yet.\n",
    "cli.app.hint.no_last_result": "No run results yet.\n",
    "cli.app.hint.no_output_to_export": "No output to export.\n",
    "cli.app.hint.out_usage": "Usage: /out <path>\n",
    "cli.app.help.model": "Switch Pack: /model <pack>",
    "cli.app.help.variant": "Set variant: /variant <id>",
    "cli.app.help.param": "Set parameter: /param key=val",
    "cli.app.help.params": "View current parameters/variant",
    "cli.app.help.examples": "List/run built-in examples: /examples [index]",
    "cli.app.help.history": "View run history",
    "cli.app.help.last": "Reprint last output",
    "cli.app.help.out": "Export last output: /out <path>",
    "cli.app.help.help": "View the command list",
    "cli.app.help.exit": "Exit the session",
    "channels.tool.icon.success": "\u2705",
    "channels.tool.icon.error": "\u274c",
    "channels.tool.icon.pending": "\u23f3",
    "channels.tool.batch_header": "\U0001f504 Tool call progress (batch {batch_index}):",
    "channels.tool.action.read": "\U0001f4d6 Read: {path}",
    "channels.tool.action.write": "\u270f\ufe0f Write: {path}",
    "channels.tool.action.edit": "\U0001f527 Edit: {path}",
    "channels.tool.action.multi_edit": "\U0001f527 Multi-edit: {path}",
    "channels.tool.action.glob": "\U0001f50d Search: {pattern}",
    "channels.tool.action.grep": "\U0001f50e Grep: {pattern}",
    "channels.tool.action.bash": "\U0001f4bb Run: {command}",
    "channels.tool.action.ls": "\U0001f4c2 List dir: {path}",
    "channels.tool.action.todo_write": "\U0001f4dd Update Todo",
    "channels.tool.action.todo_read": "\U0001f4cb Read Todo",
    "channels.tool.action.web_fetch": "\U0001f310 Fetch page: {url}",
    "channels.tool.action.web_search": "\U0001f50d Web search: {query}",
    "channels.tool.action.bg_process": "\U0001f680 Background process {label}{secondary}",
    "channels.tool.action.fallback": "\U0001f528 {name}",
    "channels.conversation.error.unknown_verb": "⚠️ Unknown session command: /{verb}",
    "channels.conversation.error.invalid_limit": "⚠️ The argument must be a positive integer, e.g. /list 10",
    "channels.conversation.error.list_failed": "⚠️ Failed to load history: {exc}",
    "channels.conversation.error.invalid_index": "⚠️ The index must be a positive integer, e.g. /use 2",
    "channels.conversation.error.index_min": "⚠️ Indexes start at 1",
    "channels.conversation.error.use_failed": "⚠️ Failed to switch session: {exc}",
    "channels.conversation.error.status_failed": "⚠️ Failed to fetch status: {exc}",
    "channels.conversation.error.empty_name": "⚠️ Name cannot be empty",
    "channels.conversation.error.rename_failed": "⚠️ Rename failed: {exc}",
    "channels.conversation.error.delete_failed": "⚠️ Delete failed: {exc}",
    "channels.conversation.hint.no_history": "📋 No session history yet\n\nSend /new to start a new session",
    "channels.conversation.hint.use_usage": "Usage: /use <index>\n\nSend /list to view session history",
    "channels.conversation.hint.rename_usage": "Usage: /rename <new name>\n\nExample: /rename Project discussion",
    "channels.conversation.list.header": "📋 Session history (last {count}):\n",
    "channels.conversation.list.round_suffix": " · {round_count} rounds",
    "channels.conversation.list.footer_marker": "▶=current session",
    "channels.conversation.list.footer_hint": "💡 /use <index> to switch to a session",
    "channels.conversation_adapter.label.untitled": "(untitled)",
    "channels.conversation_adapter.label.follow_global": "Follow global setting",
    "channels.conversation_adapter.list.empty": "📋 No session history yet",
    "channels.conversation_adapter.error.index_out_of_range": (
        "⚠️ Index {index} is out of range ({total} sessions in history)\n\n"
        "Send /list to view the list"
    ),
    "channels.conversation_adapter.ok.switched": (
        "✅ Switched to session: {title}\n"
        "{rounds} rounds of chat history\n\n"
        "Just send a message to continue the conversation."
    ),
    "channels.conversation_adapter.status.no_active": "📊 No active session\n\nJust send a message to start a new conversation.",
    "channels.conversation_adapter.status.header": "📊 Current session status",
    "channels.conversation_adapter.status.name": "Name: {title}",
    "channels.conversation_adapter.status.rounds": "Rounds: {rounds}",
    "channels.conversation_adapter.status.model": "Current model: {model}",
    "channels.conversation_adapter.status.tool_calls": "Tool calls: {count}",
    "channels.conversation_adapter.status.context": "Context size: {tokens} tokens (incl. history)",
    "channels.conversation_adapter.status.conv_id": "Session ID: {conv_id}…",
    "channels.conversation_adapter.status.hint": "\n💡 /rename <name> to rename · /delete to delete the current session",
    "channels.conversation_adapter.error.no_active_conv": "⚠️ No active session. Send a message first to start a conversation.",
    "channels.conversation_adapter.error.rename_unavailable": "⚠️ Rename is not available right now.",
    "channels.conversation_adapter.ok.renamed": "✅ Current session renamed to: {new_name}",
    "channels.conversation_adapter.ok.deleted": "🗑 Current session deleted. A new session has started.",
    "channels.conversation_adapter.time.unknown": "unknown time",
    "channels.conversation_adapter.time.just_now": "just now",
    "channels.conversation_adapter.time.minutes_ago": "{n} min ago",
    "channels.conversation_adapter.time.hours_ago": "{n} h ago",
    "channels.conversation_adapter.time.days_ago": "{n} d ago",
    "channels.help.main": (
        "\U0001f4d6 WeChat / Feishu / Chat command help\n"
        "\n"
        "\u2328\ufe0f Regular chat commands:\n"
        "\n"
        "  /help  (/h)\n"
        "    Show this help info.\n"
        "\n"
        "  /new  (/n)\n"
        "    Save the current chat history and start a new session. History stays visible in Chat.\n"
        "\n"
        "  /clear  (/cl)\n"
        "    Delete the current chat history (without saving) and start a new session. History is permanently removed.\n"
        "\n"
        "  /list [N]  (/l [N])\n"
        "    Show the most recent N sessions (default 5): name, time, and rounds.\n"
        "\n"
        "  /use <index>  (/u <index>)\n"
        "    Switch to the session with the given index and continue chatting.\n"
        "\n"
        "  /status  (/s)\n"
        "    Show the current session status (name, rounds).\n"
        "\n"
        "  /rename <new-name>  (/rn <new-name>)\n"
        "    Rename the current session.\n"
        "\n"
        "  /delete  (/del)\n"
        "    Delete the current session (unrecoverable) and start a new one.\n"
        "\n"
        "  /stop  (/st)\n"
        "    Immediately stop the currently running task (both regular chat and Claude Code tasks are supported).\n"
        "\n"
        "  /models  (/ms)\n"
        "    List all available models (local + cloud) and show which one is in use.\n"
        "\n"
        "  /model  (/m)\n"
        "    Show the model used by the current session.\n"
        "\n"
        "  /model <index>  (/m <index>)\n"
        "    Switch model by /models index; a model_id also works. Send /model 0 to follow the global setting.\n"
        "\n"
        "  /compact  (/c)\n"
        "    Compaction commands for the current conversation.\n"
        "    /compact           force-compress now, showing before/after occupancy\n"
        "    /compact status    view current token usage, digest size + escalation hint\n"
        "    /compact migrate   fork a fresh conversation seeded from the current digest\n"
        "    /compact clear     drop the compaction checkpoint (digest + ledger + counter)\n"
        "\n"
        "  /reboot  (/r)\n"
        "    Restart the QAIModelBuilder service; the WeChat channel will reconnect automatically once restarted.\n"
        "\n"
        "\U0001f510 File access grant commands (FileGuard):\n"
        "\n"
        "  /grant read <path>  (/g read <path>)\n"
        "    Grant read access to the given path for the current session (cleared automatically when the session ends).\n"
        "\n"
        "  /grant write <path>  (/g write <path>)\n"
        "    Grant write access to the given path for the current session.\n"
        "\n"
        "  /grant exec <path>  (/g exec <path>)\n"
        "    Grant permission to run commands under the given path for the current session.\n"
        "\n"
        "  /grant list  (/g list)\n"
        "    List the paths already granted in the current session.\n"
        "\n"
        "  /grant revoke <op> <path>\n"
        "    Revoke a specific grant. Example: /grant revoke read C:/WoS_AI/data\n"
        "\n"
        "  \U0001f4a1 Grants are valid only within the current session and are cleared automatically when it ends.\n"
        "  \U0001f4a1 If an AI tool call is denied, pre-grant the path with /grant, or ask an admin to enable\n"
        "     WebUI popup approval in Settings > Security > IM channel grant settings.\n"
        "\n"
        "\U0001f916 Claude Code AI coding assistant commands (alias /code):\n"
        "\n"
        "  /cc new <dir-path> [session-name]\n"
        "    Create a new Claude Code session bound to the given project directory.\n"
        "\n"
        "  /cc list  (/cc l)\n"
        "    List all your Claude Code sessions (ID, name, status).\n"
        "\n"
        "  /cc use <index>  (/cc u <index>)\n"
        "    Switch session by /cc list index (e.g. /cc use 1).\n"
        "\n"
        "  /cc use <ID-prefix>\n"
        "    Switch session by ID prefix (the first 8 characters are enough).\n"
        "\n"
        "  /cc status  (/cc s)\n"
        "    Show current Claude Code session status, rounds, and tool-call count.\n"
        "\n"
        "  /cc models  (/cc ms)\n"
        "    List available Claude Code models (with index) and show the current one.\n"
        "\n"
        "  /cc model  (/cc m)\n"
        "    Show the current Claude Code model.\n"
        "\n"
        "  /cc model <index>  (/cc m <index>)\n"
        "    Switch the Claude Code model by /cc models index.\n"
        "\n"
        "  /cc fork  (/cc f)\n"
        "    Fork the current session as a new branch (keeps history; a new session ID is minted on the next message).\n"
        "\n"
        "  /cc stop  (/cc st)\n"
        "    Stop the currently running Claude Code task; you can send a new message right after to continue.\n"
        "\n"
        "  /cc cd [dir-path]\n"
        "    Show the current working directory (no argument), or change the CC session's working directory.\n"
        "\n"
        "  /cc rename <new-name>  (/cc r <new-name>)\n"
        "    Rename the current Claude Code session.\n"
        "\n"
        "  /cc close  (/cc c)\n"
        "    Leave CC mode (the session is kept; use /cc use to re-enter).\n"
        "\n"
        "  /cc delete  (/cc d)\n"
        "    Permanently delete the current Claude Code session (unrecoverable).\n"
        "\n"
        "  /cc help  (/cc h)\n"
        "    Show Claude Code command help.\n"
        "\n"
        "  <plain message> (when a CC session is active)\n"
        "    Just send a message to chat with Claude Code — no /cc prefix needed.\n"
        "    Send /new to switch back to regular AI chat mode (CC session is unaffected).\n"
        "\n"
        "\U0001f537 OpenCode AI coding assistant commands:\n"
        "\n"
        "  /oc new <dir-path> [session-name]\n"
        "    Create a new OpenCode session bound to the given project directory.\n"
        "\n"
        "  /oc list  (/oc l)\n"
        "    List all your OpenCode sessions (ID, name, status, model).\n"
        "\n"
        "  /oc use <index>  (/oc u <index>)\n"
        "    Switch session by /oc list index.\n"
        "\n"
        "  /oc status  (/oc s)\n"
        "    Show current OpenCode session status, rounds, and tool-call count.\n"
        "\n"
        "  /oc models  (/oc ms)\n"
        "    List available OpenCode models.\n"
        "\n"
        "  /oc model [index]  (/oc m [index])\n"
        "    Show / switch the OpenCode model.\n"
        "\n"
        "  /oc stop  (/oc st)\n"
        "    Stop the currently running OpenCode task.\n"
        "\n"
        "  /oc rename <new-name>  (/oc r <new-name>)\n"
        "    Rename the current OpenCode session.\n"
        "\n"
        "  /oc close  (/oc c)\n"
        "    Leave OC mode.\n"
        "\n"
        "  /oc delete  (/oc d)\n"
        "    Permanently delete the current OpenCode session (unrecoverable).\n"
        "\n"
        "  /oc help  (/oc h)\n"
        "    Show OpenCode command help.\n"
        "\n"
        "\U0001f4a1 Tip: when the local model is unavailable, if a cloud model is configured the system switches over automatically and notifies you ahead of time.\n"
        "\U0001f4a1 Claude Code must be enabled and its auth configured under Settings > AI Coding.\n"
        "\U0001f4a1 OpenCode must be enabled and its service endpoint configured under Settings > AI Coding > OpenCode."
    ),
    "channels.help.cc": (
        "\U0001f916 Claude Code command help (alias /code)\n"
        "\n"
        "  /cc new <dir-path> [session-name]\n"
        "    Create a new Claude Code session bound to the given project directory.\n"
        "\n"
        "  /cc list  (/cc l)\n"
        "    List all your Claude Code sessions.\n"
        "\n"
        "  /cc use <index>  (/cc u <index>)\n"
        "    Switch session by /cc list index.\n"
        "\n"
        "  /cc use <ID-prefix>\n"
        "    Switch session by ID prefix (the first 8 characters are enough).\n"
        "\n"
        "  /cc status  (/cc s)\n"
        "    Show current session status, rounds, and tool-call count.\n"
        "\n"
        "  /cc models  (/cc ms)\n"
        "    List available Claude Code models (with index) and show the current one.\n"
        "\n"
        "  /cc model [index]  (/cc m [index])\n"
        "    Show / switch the Claude Code model.\n"
        "\n"
        "  /cc fork  (/cc f)\n"
        "    Fork the current session as a new branch (keeps history).\n"
        "\n"
        "  /cc stop  (/cc st)\n"
        "    Stop the currently running Claude Code task.\n"
        "\n"
        "  /cc cd [dir-path]\n"
        "    Show the current working directory, or change the CC session's working directory.\n"
        "\n"
        "  /cc rename <new-name>  (/cc r <new-name>)\n"
        "    Rename the current session.\n"
        "\n"
        "  /cc close  (/cc c)\n"
        "    Leave CC mode (the session is kept).\n"
        "\n"
        "  /cc delete  (/cc d)\n"
        "    Permanently delete the current session (unrecoverable).\n"
        "\n"
        "  /cc help  (/cc h)\n"
        "    Show this help.\n"
        "\n"
        "\U0001f4a1 After creating a session, just send a message to chat with Claude Code\n"
        "\U0001f4a1 /cc fork lets you save progress at a key point and then explore different directions\n"
        "\U0001f4a1 After /cc stop you can send a new message immediately to continue\n"
        "\U0001f4a1 After /cc close the session is kept and can be re-entered any time via /cc use\n"
        "\U0001f4a1 Send /new to switch back to regular AI chat mode"
    ),
    "channels.help.oc": (
        "\U0001f537 OpenCode command help\n"
        "\n"
        "  /oc new <dir-path> [session-name]\n"
        "    Create a new OpenCode session.\n"
        "\n"
        "  /oc list  (/oc l)\n"
        "    List all your OpenCode sessions (ID, name, status, model).\n"
        "\n"
        "  /oc use <index>  (/oc u <index>)\n"
        "    Switch session by /oc list index.\n"
        "\n"
        "  /oc use <ID-prefix>\n"
        "    Switch session by ID prefix.\n"
        "\n"
        "  /oc status  (/oc s)\n"
        "    Show current session status, rounds, and tool-call count.\n"
        "\n"
        "  /oc models  (/oc ms)\n"
        "    List available OpenCode models.\n"
        "\n"
        "  /oc model [index]  (/oc m [index])\n"
        "    Show / switch the OpenCode model.\n"
        "\n"
        "  /oc stop  (/oc st)\n"
        "    Stop the currently running OpenCode task.\n"
        "\n"
        "  /oc rename <new-name>  (/oc r <new-name>)\n"
        "    Rename the current session.\n"
        "\n"
        "  /oc close  (/oc c)\n"
        "    Leave OC mode (the session is kept).\n"
        "\n"
        "  /oc delete  (/oc d)\n"
        "    Permanently delete the current session (unrecoverable).\n"
        "\n"
        "  /oc help  (/oc h)\n"
        "    Show this help.\n"
        "\n"
        "\U0001f4a1 After creating a session, just send a message to chat with OpenCode\n"
        "\U0001f4a1 After /oc stop you can send a new message immediately to continue\n"
        "\U0001f4a1 After /oc close the session is kept and can be re-entered any time via /oc use\n"
        "\U0001f4a1 Send /new to switch back to regular AI chat mode\n"
        "\U0001f4a1 OpenCode must be enabled and its service endpoint configured under Settings > AI Coding > OpenCode"
    ),
    # TODO: channels.ai_coding.* pending translation (see docs/30-ui-ux/i18n-implementation-plan.md)
    "channels.ai_coding.session.stopped_send_new": "\u23f9\ufe0f Current task stopped. You can send a new message to continue.",
    "channels.ai_coding.session.no_active_task": "\u2139\ufe0f No AI coding task is currently running.",
    "channels.ai_coding.session.usage_new": "\u2753 Usage: /{verb} new <directory path> [session name]",
    "channels.ai_coding.session.dir_not_found": "\u26a0\ufe0f Directory not found: {workspace_path}",
    "channels.ai_coding.session.created": "\u2705 Created /{verb} session {sid}\nWorking directory: {workspace_path}",
    "channels.ai_coding.session.switched": "\u2705 Switched to session {session_id}",
    "channels.ai_coding.session.no_active": "\u2139\ufe0f No active AI coding session.",
    "channels.ai_coding.session.usage_rename": "\u2753 Usage: /{verb} rename [session id] <new title>",
    "channels.ai_coding.session.renamed": "\u2705 Renamed to: {new_title}",
    "channels.ai_coding.session.closed": "\u2705 Closed session {sid} (history preserved; use /{verb} list to reopen)",
    "channels.ai_coding.session.deleted": "\u2705 Deleted session {sid}",
    "channels.ai_coding.session.usage_cd": "\u2753 Usage: /{verb} cd <new working directory>",
    "channels.ai_coding.session.current_dir_usage": "\U0001f4c1 Current working directory: {cur_dir}\n\nUsage: /{verb} cd <new working directory>",
    "channels.ai_coding.session.cd_ok": "\u2705 Switched working directory: {new_path}",
    "channels.ai_coding.session.no_upstream_no_fork": "\u26a0\ufe0f The current session has no conversation yet (no upstream connection); cannot fork",
    "channels.ai_coding.session.forked": "\u2705 Forked into new session {new_sid}",
    "channels.ai_coding.session.stopped": "\u23f9\ufe0f Current task stopped.",
    "channels.ai_coding.session.opencode_aborted": "\u23f9\ufe0f Aborted the OpenCode task. You can send a new message to continue.",
    "channels.ai_coding.session.no_running_task": "\u2139\ufe0f No task is currently running.",
    "channels.ai_coding.session.models_header": "\U0001f4cb /{verb} available models:",
    "channels.ai_coding.session.model_not_in_list": "\n\u2139\ufe0f Current model: {current} (not in list)",
    "channels.ai_coding.session.models_hint": "\nSend /{verb} model <index> or /{verb} model <model name> to switch models",
    "channels.ai_coding.session.model_current": "\U0001f5a5\ufe0f /{verb} current model: {current}\n\nSend /{verb} models to view available models\nSend /{verb} model <index|model name> to switch",
    "channels.ai_coding.session.model_switched": "\u2705 /{verb} model switched to: {new_model}",
    "channels.ai_coding.error.command_failed": "\u26a0\ufe0f /{verb} {sub} failed: {exc}",
    "channels.ai_coding.error.index_out_of_range": "\u26a0\ufe0f Index {ref} is out of range ({count} sessions total). Send /cc list to view.",
    "channels.ai_coding.error.multiple_matches": "\u26a0\ufe0f Multiple sessions matched; please provide a longer ID prefix.",
    "channels.ai_coding.error.model_index_out_of_range": "\u26a0\ufe0f Index {arg} is out of range ({count} models total). Send /{verb} models to view.",
    "channels.ai_coding.error.model_unavailable": "\u26a0\ufe0f /{verb} model is unavailable (config not registered).",
    "channels.ai_coding.usage.no_content": "(AI coding assistant returned no content)",
    "channels.ai_coding.usage.turn_warning_with_count": "⚠️ The current session has reached {tc} turns; consider creating a new session soon.",
    "channels.ai_coding.usage.turn_warning_generic": "⚠️ The current session has many turns; consider creating a new session soon.",
    "channels.ai_coding.progress.subagent_started_desc": "\U0001f916 Subtask started: {desc}",
    "channels.ai_coding.progress.subagent_started": "\U0001f916 Subtask started",
    "channels.ai_coding.progress.subagent_done_desc": "\u2705 Subtask completed: {desc}",
    "channels.ai_coding.progress.subagent_done": "\u2705 Subtask completed",
    "channels.ai_coding.progress.subagent_error_desc": "\u26a0\ufe0f Subtask error: {err}",
    "channels.ai_coding.progress.subagent_error": "\u26a0\ufe0f Subtask error",
    "channels.ai_coding.progress.tool_count": "{tool_uses} tools",
    "channels.ai_coding.progress.subagent_running": "\u23f3 Subtask running [{task_short}]",
    "channels.chat.compact.unavailable_module": "\u26a0\ufe0f /compact is unavailable: chat module is disabled",
    "channels.chat.compact.unavailable": "\u26a0\ufe0f /compact is unavailable",
    "channels.chat.compact.failed": "\u26a0\ufe0f /compact failed: {error}",
    "channels.chat.compact.done": "\u2705 /compact: {before_tokens} \u2192 {after_tokens} tokens (saved {savings}); {ratio_after_pct}% of {budget_tokens} budget{escalation_suffix}.",
    "channels.chat.compact.status_ok": "\U0001f4ca /compact status: {used_tokens} / {budget_tokens} tokens ({ratio_pct}%). Digest: {digest_bytes} bytes. Ledger: {ledger_count} entries.",
    "channels.chat.compact.status_escalate": "\u26a0\ufe0f /compact status: {used_tokens} / {budget_tokens} tokens ({ratio_pct}%). Digest: {digest_bytes} bytes. Ledger: {ledger_count} entries. Handoff suggested \u2014 use /compact migrate.",
    "channels.chat.compact.migrate_ok": "\u2705 /compact migrate: forked to a new conversation ({new_conversation_id}) seeded from the current digest.",
    "channels.chat.compact.migrate_no_digest": "\u26a0\ufe0f /compact migrate: no session summary is available yet \u2014 run /compact (or let the session grow) so a summary can be generated first.",
    "channels.chat.compact.clear_ok": "\u2705 /compact clear: dropped the compaction checkpoint (digest, ledger, counter).",
    "channels.chat.compact.clear_none": "\u2139\ufe0f /compact clear: no compaction checkpoint was in place.",
    "channels.chat.compact.no_savings": "\u2139\ufe0f /compact: already compacted \u2014 nothing more to reclaim ({tokens} tokens, {ratio_pct}% of {budget_tokens} budget).",
    "channels.chat.compact.empty_history": "\u2139\ufe0f /compact: the current conversation is empty \u2014 nothing to compact.",
    "channels.chat.compact.invalid": "\u26a0\ufe0f /compact: unknown sub-command. Use /compact, /compact status, /compact migrate, or /compact clear.",
    "channels.chat.compact.escalation_suffix": " \u2014 handoff suggested, use /compact migrate",
    "channels.chat.compact.no_conversation": "\u2139\ufe0f /compact: no active conversation for this channel user yet \u2014 send a message first.",
    "channels.chat.warning.turn_count": "\u26a0\ufe0f The current session has reached {turn_count} turns; consider clearing history or starting a new session soon.",
    "channels.chat.warning.turn_generic": "\u26a0\ufe0f The current session has many turns; consider starting a new session soon.",
    "channels.chat.subagent.label_multi": "[Subagent {index}/{total}]",
    "channels.chat.subagent.label_solo": "[Subagent {index}]",
    "channels.chat.subagent.start_line": "\n{label} starting...\nTask: {preview}\n",
    "channels.chat.subagent.done": "  \u2705 Subagent {index} completed ({rounds} rounds)\n",
    "channels.chat.subagent.error": "  \u274c Subagent {index} error: {message}\n",
    "channels.chat.agent_summary.header": "\n---\n\U0001f4cb Main agent summary:\n",
    "channels.chat.label.wechat": "WeChat",
    "channels.chat.label.feishu": "Feishu",
    "channels.dispatch.error.invalid_command": "\u26a0\ufe0f Invalid command format: {exc}",
    "channels.dispatch.error.bridge_unavailable": "\u26a0\ufe0f The processing service is temporarily unavailable. Please try again later.",
    "channels.dispatch.error.application": "\u26a0\ufe0f {exc}",
    "channels.dispatch.error.unhandled": "An error occurred while processing the message. Please try again later.",
    "channels.dispatch.error.conversation_uc_unavailable": "\u26a0\ufe0f Session management commands are currently unavailable",
    "channels.dispatch.thinking_ack": "Thinking\u2026",
    "channels.dispatch.stop.stopped": "\u23f9\ufe0f Current task stopped. You can send a new message to continue.",
    "channels.dispatch.stop.no_task": "\u2139\ufe0f No task is currently running.",
    "channels.dispatch.chat.empty_reply": "(Model returned no content)",
    "channels.dispatch.ai_coding.empty_reply": "(AI coding assistant returned no content)",
    "channels.dispatch.new.cleared": "Current session cleared \U0001f5d1",
    "channels.dispatch.new.opened": "New session started \u2728",
    "channels.dispatch.model.unavailable": "\u26a0\ufe0f /model is currently unavailable",
    "channels.dispatch.model.default_label": "default (unspecified)",
    "channels.dispatch.model.show": (
        "\U0001f5a5\ufe0f Current model: {cur}\n\n"
        "Send /models to view available models\n"
        "Send /model <index|model name> to switch\n"
        "Send /model 0 to restore the platform default"
    ),
    "channels.dispatch.model.index_out_of_range": (
        "\u26a0\ufe0f Index {arg} is out of range"
        " ({total} models total). Send /models to view."
    ),
    "channels.dispatch.model.failed": "\u26a0\ufe0f /model failed: {exc}",
    "channels.dispatch.model.reset_ok": "\u2705 Restored the platform default model",
    "channels.dispatch.model.switched": "\u2705 Switched model: {model_id}{auto_load_msg}",
    "channels.dispatch.model.autoload": "\n\nLoading model {model_name}\u2026",
    "channels.dispatch.model.autoload_with_status": "\n\nLoading model {model_name} ({status})\u2026",
    "channels.dispatch.model.local_unavailable_fallback": "\u26a0\ufe0f Local model is currently unavailable; automatically switched to cloud model: {fallback_id}",
    "channels.dispatch.models.none": "\u2139\ufe0f No models are currently available.",
    "channels.dispatch.models.header": "\U0001f4cb Available models ({total} total):",
    "channels.dispatch.models.local_header": "[Local models]",
    "channels.dispatch.models.cloud_header": "[Cloud models]",
    "channels.dispatch.models.status_running": "running",
    "channels.dispatch.models.status_unloaded": "not loaded",
    "channels.dispatch.models.item": "  [{idx}] {name}  ({status})",
    "channels.dispatch.models.cloud_item": "  [{idx}] {cid}",
    "channels.dispatch.models.current": "\nCurrent model: {current}",
    "channels.dispatch.models.hint": "\nSend /model <index> to switch models; /model 0 restores the default",
    "channels.dispatch.grant.unavailable": "\u26a0\ufe0f /grant is unavailable: security module not connected",
    "channels.dispatch.grant.usage": (
        "\u26a0\ufe0f /grant usage:\n"
        "  /grant read <path>    — grant read permission\n"
        "  /grant write <path>   — grant write permission\n"
        "  /grant exec <path>    — grant execute permission\n"
        "  /grant list           — list current grants\n"
        "  /grant revoke <op> <path> — revoke a grant\n\n"
        "  Example: /grant read C:/WoS_AI/data"
    ),
    "channels.dispatch.grant.revoke_usage": (
        "\u26a0\ufe0f /grant revoke usage:\n"
        "  /grant revoke <op> <path>\n"
        "  where op \u2208 read / write / exec"
    ),
    "channels.dispatch.grant.invalid_op": (
        "\u26a0\ufe0f Invalid operation type: {op}"
        " (must be read / write / exec)"
    ),
    "channels.dispatch.grant.needs_op_and_path": "\u26a0\ufe0f /grant requires both a path and an operation (read / write / exec)",
    "channels.dispatch.grant.usage_add": (
        "\u26a0\ufe0f /grant usage: /grant <op> <path>\n"
        "  where op \u2208 read / write / exec\n"
        "  Example: /grant read C:/WoS_AI/data"
    ),
    "channels.dispatch.reboot.unavailable": "\u26a0\ufe0f /reboot is currently unavailable",
    "channels.dispatch.reboot.failed": "\u26a0\ufe0f /reboot failed: {exc}",
    "channels.dispatch.reboot.scheduled": "\u2705 Restart requested. The process will exit within seconds and be relaunched by the supervisor.",
    "channels.dispatch.image.download_failed": "Received an image (download failed, cannot display)",
    "channels.ai_coding_notify.sync.question": "[WebUI question]\n{question}",
    "channels.ai_coding_notify.sync.reply": "[Claude Code reply]\n{reply}",
    "channels.ai_coding_notify.task_done.with_summary": "\u2705 Task completed\n{summary}",
    "channels.ai_coding_notify.task_done.plain": "\u2705 Task completed",
    "channels.grant.error.no_active_session": "\u26a0\ufe0f /grant requires an active /cc or /oc session first",
    "channels.grant.error.needs_path": "\u26a0\ufe0f /grant requires a path",
    "channels.grant.error.needs_op": "\u26a0\ufe0f /grant requires an operation (read / write / exec)",
    "channels.grant.error.needs_op_and_path": "\u26a0\ufe0f /grant requires both a path and an operation (read / write / exec)",
    "channels.grant.error.revoke_needs_path": "\u26a0\ufe0f /grant revoke requires a path",
    "channels.grant.error.module_disabled": "\u26a0\ufe0f Security module has /grant disabled",
    "channels.grant.error.grant_failed": "\u26a0\ufe0f /grant failed: {exc}",
    "channels.grant.error.revoke_failed": "\u26a0\ufe0f /grant revoke failed: {exc}",
    "channels.grant.error.list_failed": "\u26a0\ufe0f /grant list failed: {exc}",
    "channels.grant.list.empty": "\u2139\ufe0f The current session has no granted paths.",
    "channels.grant.list.header": "\U0001f4cb Current session grants:",
    "channels.grant.ok.granted": "\u2705 Granted: {path} ({op})",
    "channels.grant.ok.revoked": "\u2705 Revoked: {path}",
    "channels.grant.revoke.not_found": "\u2139\ufe0f The current session has no grant for {path}.",
    # ── Context-overflow recovery (chat streaming) ────────────────────────
    "chat.context_recovery.compressing": "Context is full \u2014 compressing the conversation history and retrying automatically\u2026",
    "chat.context_recovery.succeeded": "Context was compressed ({before_tokens} \u2192 {after_tokens} tokens) and your message was resent in full. Earlier history is now summarised, so this reply took a little longer.",
    "chat.context_recovery.failed_no_room": "Context is full and cannot be compressed any further. Send /compact to compress the history manually, or /new to continue in a fresh session \u2014 your message was not lost.",
    "chat.context_recovery.unavailable_mid_tool": "Context filled up in the middle of a tool run, which cannot be compressed automatically. Once this turn stops, send /compact to compress the history, or /new to continue in a fresh session.",
}
