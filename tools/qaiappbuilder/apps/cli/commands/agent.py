# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Non-interactive QAI AppBuilder agent CLI commands (``qai agent``).

``qai agent run`` is the first scriptable entry point of the ``qai`` CLI. It
is a thin argparse layer over :mod:`qai.agent_sdk`; all behaviour that is not
about argument parsing or output formatting lives in the SDK so other
``qai`` command groups can reuse it.

Extending: add new verbs (``qai agent list``, ``qai agent resume`` ...) as
further ``agent_sub.add_parser(...)`` calls inside :func:`register`, each with
its own ``set_defaults(handler=...)``. Keep the exit-code contract of
:func:`cmd_agent_run`: 0 ok, 1 runtime failure, 2 usage error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from qai.agent_sdk import AgentFrame, AgentRunRequest, AgentRunResult, QaiAgentClient

__all__ = ["build_extra", "cmd_agent_run", "read_prompt", "register"]

_MODEL_REQUIRED_MESSAGE = (
    "no model specified: pass --model <model-id> (there is no default model)"
)


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach the agent command group to the top-level dispatcher."""

    agent = subparsers.add_parser(
        "agent",
        help="run the QAI AppBuilder chat agent from scripts or other tools",
        description=(
            "Run one non-interactive QAI AppBuilder chat-agent turn. "
            "Prompt text can come from an argument, --prompt-file, or stdin."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    agent_sub = agent.add_subparsers(
        dest="agent_command",
        required=True,
        metavar="<agent-command>",
    )

    run = agent_sub.add_parser(
        "run",
        help="run one agent turn",
        description=(
            "Run one QAI AppBuilder agent turn and print either final text, "
            "a JSON result, or newline-delimited frame events."
        ),
    )
    run.add_argument(
        "prompt",
        nargs="?",
        help="prompt text. Omit with --stdin, or omit on a pipe to read stdin.",
    )
    run.add_argument(
        "--stdin",
        action="store_true",
        dest="read_stdin",
        help="read the prompt from stdin",
    )
    run.add_argument(
        "--prompt-file",
        type=Path,
        metavar="<path>",
        help="read the prompt from a UTF-8 text file",
    )
    run.add_argument(
        "--model",
        metavar="<model-id>",
        help=(
            "model id passed to the agent runtime (required; there is no "
            "default model)"
        ),
    )
    run.add_argument(
        "--provider-file",
        type=Path,
        metavar="<path>",
        help=(
            "JSON file describing the model provider(s) for this run only "
            "(base_url, api_key or ${ENV_NAME}, models). Held in memory, "
            "never saved; the model must be listed in it or in the saved providers"
        ),
    )
    run.add_argument(
        "--conversation-id",
        metavar="<id>",
        help="continue an existing QAI conversation instead of creating one",
    )
    run.add_argument(
        "--client-turn-id",
        metavar="<id>",
        help="caller tag for this user turn, stored in the message metadata",
    )
    run.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        metavar="<path>",
        help=(
            "workspace directory exposed to the agent; defaults to the "
            "current directory"
        ),
    )
    run.add_argument(
        "--title",
        default="QAI Agent",
        help="title for a newly created conversation",
    )
    run.add_argument(
        "--system-prompt",
        help="per-request system prompt override",
    )
    run.add_argument(
        "--system-prompt-file",
        type=Path,
        metavar="<path>",
        help="read a per-request system prompt override from a UTF-8 file",
    )
    run.add_argument(
        "--tool-mode",
        metavar="<mode>",
        help="optional QAI tool mode for specialized agent routing",
    )
    run.add_argument(
        "--tool-param",
        action="append",
        default=[],
        metavar="key=json",
        help="set one tool_params entry; value is parsed as JSON when possible",
    )
    run.add_argument(
        "--extra-json",
        default=None,
        metavar="<json-object>",
        help="merge an additional StreamChatInput.extra JSON object",
    )
    run.add_argument(
        "--format",
        choices=("text", "json", "ndjson"),
        default="text",
        help="stdout format",
    )
    run.add_argument(
        "--include-frames",
        action="store_true",
        help="include all stream frames in the final JSON result",
    )
    run.add_argument(
        "--output-last-message",
        type=Path,
        metavar="<path>",
        help="write only the final assistant text to this UTF-8 file",
    )
    run.add_argument(
        "--no-provider-check",
        action="store_true",
        help="skip the configured-provider preflight",
    )
    run.add_argument(
        "--connect-mcp",
        action="store_true",
        help="connect configured MCP servers before starting the agent turn",
    )
    run.add_argument(
        "--timeout-seconds",
        type=_positive_float,
        metavar="<seconds>",
        help="abort the agent turn if it does not finish within this many seconds",
    )
    run.set_defaults(handler=cmd_agent_run)


def cmd_agent_run(args: argparse.Namespace) -> int:
    """Argparse boundary for agent run."""

    try:
        prompt = read_prompt(args, stdin=sys.stdin)
        extra = build_extra(
            extra_json=args.extra_json,
            system_prompt=args.system_prompt,
            system_prompt_file=args.system_prompt_file,
            tool_mode=args.tool_mode,
            tool_params=args.tool_param,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"qai agent run: {exc}\n")
        return 2
    if not (isinstance(args.model, str) and args.model.strip()):
        # Never fall back to the configured default model: a scripted caller
        # must say which model it wants (the default's credentials may not
        # exist on this machine).
        sys.stderr.write(f"qai agent run: {_MODEL_REQUIRED_MESSAGE}\n")
        return 2
    args.model = args.model.strip()
    provider_file = getattr(args, "provider_file", None)
    if provider_file is not None:
        # Validate up front so a bad file is a usage error (2) with a clear
        # message, not a traceback from deep inside container start-up.
        from apps.api._provider_file_overlay import ProviderFileError, load_provider_file

        try:
            load_provider_file(provider_file)
        except ProviderFileError as exc:
            sys.stderr.write(f"qai agent run: --provider-file: {exc}\n")
            return 2
    return asyncio.run(_run_agent(args, prompt=prompt, extra=extra))


async def _run_agent(
    args: argparse.Namespace,
    *,
    prompt: str,
    extra: Mapping[str, Any],
) -> int:
    emitted_text = False

    async def _on_frame(frame: AgentFrame) -> None:
        nonlocal emitted_text
        if args.format == "ndjson":
            _emit_json_line({"type": "frame", "frame": frame.to_dict()})
            return
        if args.format == "text" and frame.frame_type == "chunk":
            text = frame.payload.get("text")
            if isinstance(text, str) and text:
                sys.stdout.write(text)
                sys.stdout.flush()
                emitted_text = True

    async with QaiAgentClient(
        repo_root=getattr(args, "repo_root", None),
        config_file=getattr(args, "config_file", None),
        provider_file=getattr(args, "provider_file", None),
    ) as client:
        if not args.no_provider_check and not await client.has_configured_provider():
            sys.stderr.write(
                "qai agent run: no model provider is configured; run "
                "qai config setup or pass --no-provider-check for a local "
                "model/resolver configuration.\n"
            )
            return 1
        run_request = client.run(
            AgentRunRequest(
                prompt=prompt,
                model=args.model,
                conversation_id=args.conversation_id,
                client_turn_id=args.client_turn_id,
                title=args.title,
                workspace=args.workspace,
                connect_mcp=args.connect_mcp,
                extra=extra,
            ),
            on_frame=_on_frame if args.format in {"text", "ndjson"} else None,
            include_frames=args.include_frames or args.format == "ndjson",
        )
        try:
            if args.timeout_seconds is None:
                result = await run_request
            else:
                result = await asyncio.wait_for(
                    run_request,
                    timeout=args.timeout_seconds,
                )
        except TimeoutError:
            sys.stderr.write(
                f"qai agent run: timed out after {args.timeout_seconds:g} seconds\n"
            )
            return 1

    if args.output_last_message is not None:
        args.output_last_message.parent.mkdir(parents=True, exist_ok=True)
        args.output_last_message.write_text(result.final_text, encoding="utf-8")

    if args.format == "json":
        _emit_json(result.to_dict(include_frames=args.include_frames))
    elif args.format == "ndjson":
        _emit_json_line(
            {
                "type": "result",
                "result": result.to_dict(include_frames=False),
            }
        )
    elif not emitted_text and result.final_text:
        sys.stdout.write(result.final_text)
        sys.stdout.flush()
    if args.format == "text" and emitted_text:
        sys.stdout.write("\n")
        sys.stdout.flush()
    if result.status != "completed" and args.format != "ndjson":
        sys.stderr.write(f"qai agent run: {_format_result_error(result)}\n")
    return 0 if result.status == "completed" else 1


def read_prompt(args: argparse.Namespace, *, stdin: Any) -> str:
    """Resolve prompt text from exactly one CLI source."""

    sources = [
        bool(getattr(args, "prompt", None)),
        bool(getattr(args, "prompt_file", None)),
        bool(getattr(args, "read_stdin", False)),
    ]
    piped_stdin = (
        not any(sources)
        and hasattr(stdin, "isatty")
        and not stdin.isatty()
    )
    if sum(1 for present in sources if present) + int(bool(piped_stdin)) != 1:
        raise ValueError("provide exactly one prompt source: prompt, --prompt-file, or --stdin")
    if getattr(args, "prompt", None):
        prompt = str(args.prompt)
    elif getattr(args, "prompt_file", None) is not None:
        prompt = args.prompt_file.read_text(encoding="utf-8-sig")
    else:
        prompt = stdin.read()
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be non-empty")
    return prompt


def build_extra(
    *,
    extra_json: str | None = None,
    system_prompt: str | None = None,
    system_prompt_file: Path | None = None,
    tool_mode: str | None = None,
    tool_params: list[str] | None = None,
) -> dict[str, Any]:
    """Build the per-request StreamChatInput.extra dictionary."""

    extra: dict[str, Any] = {}
    if extra_json:
        parsed = json.loads(extra_json)
        if not isinstance(parsed, dict):
            raise ValueError("--extra-json must be a JSON object")
        extra.update(parsed)
    if system_prompt and system_prompt_file is not None:
        raise ValueError("use only one of --system-prompt or --system-prompt-file")
    if system_prompt_file is not None:
        system_prompt = system_prompt_file.read_text(encoding="utf-8-sig")
    if system_prompt and system_prompt.strip():
        extra["system_prompt"] = system_prompt
    if tool_mode and tool_mode.strip():
        if _is_model_builder_tool_mode(tool_mode):
            raise ValueError(
                "qai agent run is agent-only; use qai build for Model Builder "
                "workflows"
            )
        extra["tool_mode"] = tool_mode.strip()
    params = _parse_tool_params(tool_params or [])
    if params:
        extra["tool_params"] = params
    if _is_model_builder_tool_mode(extra.get("tool_mode")):
        raise ValueError(
            "qai agent run is agent-only; use qai build for Model Builder "
            "workflows"
        )
    return extra


def _parse_tool_params(values: list[str]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for raw in values:
        if "=" not in raw:
            raise ValueError(f"invalid --tool-param {raw!r}: expected key=json")
        key, _, value = raw.partition("=")
        key = key.strip()
        if not key:
            raise ValueError(f"invalid --tool-param {raw!r}: empty key")
        params[key] = _parse_json_value(value)
    return params


def _parse_json_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _is_model_builder_tool_mode(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return value.strip().lower() in {
        "model-build",
        "model_build",
        "model_builder",
    }


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _format_result_error(result: AgentRunResult) -> str:
    if result.error is None:
        return f"agent run finished with status {result.status}"
    code = result.error.get("code")
    message = result.error.get("message")
    if isinstance(code, str) and code and isinstance(message, str) and message:
        return f"{code}: {message}"
    if isinstance(message, str) and message:
        return message
    if isinstance(code, str) and code:
        return code
    return f"agent run finished with status {result.status}"


def _emit_json(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _emit_json_line(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.write("\n")
    sys.stdout.flush()
