"""``apps.cli._chat_build_tool_bridge`` — CLI-exclusive ``qai_build`` tool (Step 7).

Registers ONE mutating, potentially long-running tool onto a
:class:`~apps.api.di.Container`'s chat tool registry (``container.chat.tools``)
so the default chat entry point's Agent (``apps/cli/commands/chat.py``) can
delegate a model-file conversion/quantisation request into the SAME
agentic Model Builder turn ``qai build`` drives
(``commands/build.py``'s ``BuildSession``/``build_extra``) — unlike Step 5's
4 read-only tools (:mod:`apps.cli._chat_tool_bridge`), which each wrap a
single stateless use-case call, this handler programmatically drives ONE
non-interactive turn through the SAME ``c.chat.stream_chat_use_case.execute``
call ``_stream_turn`` uses, in a fresh conversation/tab, and folds the result
into a summary (final assistant text + key tool calls) for the calling
turn's ``tool_result`` frame — analogous to how the built-in ``agent`` tool
lets one turn delegate to a sub-agent, but reusing the CLI's own
``model-build`` tool_mode instead of the ai_coding sub-agent kernel.

CLI-exclusive: wired from :func:`apps.cli._runtime.cli_container` only —
never imported from ``apps/api/_chat_di.py`` (the WebUI/API tool set stays
unaffected).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container
    from qai.chat.application.ports import ToolInvocationRequest

__all__ = ["register_cli_build_tool", "QAI_BUILD_SCHEMA"]


QAI_BUILD_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "qai_build",
        "description": (
            "Delegate a model file conversion/quantisation task to the "
            "Model Builder Agent (the same agentic session 'qai build' "
            "drives). Drives ONE agentic turn to completion and returns a "
            "summary of what the Model Builder Agent did (its final reply "
            "plus the key tool calls it made). This is a MUTATING, "
            "potentially long-running operation — only call it when the "
            "user actually wants a model file converted/quantised."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "model_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Model file path(s) to convert.",
                },
                "precision": {
                    "type": "string",
                    "description": (
                        "Quantisation precision, e.g. 'fp16' or 'fp16,w8a8'. Optional."
                    ),
                },
                "dataset": {
                    "type": "string",
                    "description": "Calibration/evaluation dataset path. Optional.",
                },
                "instruction": {
                    "type": "string",
                    "description": (
                        "Natural-language instruction describing what to do, "
                        "e.g. 'convert this model to int8 and validate it'."
                    ),
                },
            },
            "required": ["model_paths", "instruction"],
        },
    },
}


def register_cli_build_tool(container: "Container") -> str:
    """Register the ``qai_build`` delegation tool onto ``container.chat.tools``.

    Mirrors :func:`apps.cli._chat_tool_bridge.register_cli_tools`'s closure
    pattern (over ``container``), but the handler drives a nested agentic
    turn instead of a stateless use-case call — see module docstring.
    Returns the registered tool name (for tests / diagnostics).
    """

    tools = container.chat.tools

    async def _qai_build(request: "ToolInvocationRequest") -> Any:
        from apps.cli.commands.build import _precheck_cloud_provider  # noqa: PLC0415

        if not await _precheck_cloud_provider(container):
            return {
                "ok": False,
                "error": "未配置任何云端 provider，无法调用 Model Builder Agent",
            }

        args = request.arguments
        model_paths = [str(p) for p in (args.get("model_paths") or []) if p]
        if not model_paths:
            return {"ok": False, "error": "model_paths 不能为空"}
        instruction = str(args.get("instruction") or "").strip()
        if not instruction:
            return {"ok": False, "error": "instruction 不能为空"}

        return await _drive_model_build_turn(
            container,
            model_paths=model_paths,
            precision=args.get("precision"),
            dataset=args.get("dataset"),
            instruction=instruction,
        )

    tools.register("qai_build", _qai_build, schema=QAI_BUILD_SCHEMA)
    return "qai_build"


async def _drive_model_build_turn(
    container: "Container",
    *,
    model_paths: list[str],
    precision: str | None,
    dataset: str | None,
    instruction: str,
) -> dict[str, Any]:
    """Drive ONE nested model-build agentic turn to completion, summarised.

    Reuses ``commands/build.py``'s ``BuildSession``/``build_extra`` verbatim
    for the ``extra`` shape, and the same ``stream_chat_use_case.execute``
    call ``_stream_turn`` uses — a programmatically-driven turn (no REPL, no
    user typing) in a fresh conversation/tab, run to natural completion
    (``end``/``error`` frame), then folded into a summary dict.
    """

    from apps.cli.commands.build import BuildSession, build_extra
    from qai.chat.application.use_cases.conversation_management import (
        CreateConversationInput,
    )
    from qai.chat.application.use_cases.streaming import StreamChatInput
    from qai.chat.application.use_cases.tab_management import OpenTabInput
    from qai.chat.domain.content import MessageContent
    from qai.chat.domain.ids import ConversationId, TabId

    session = BuildSession(
        model_paths=model_paths,
        quant_precision=precision,
        dataset_path=dataset,
    )

    conv = await container.chat.create_conversation_use_case.execute(
        CreateConversationInput(title="Model Build (delegated)")
    )
    tab = await container.chat.open_tab_use_case.execute(
        OpenTabInput(conversation_id=conv.id.value)
    )
    conversation_id = ConversationId.of(conv.id.value)
    tab_id: Any = tab.id if not isinstance(tab.id, str) else TabId.of(tab.id)

    request = StreamChatInput(
        tab_id=tab_id,
        conversation_id=conversation_id,
        user_message=MessageContent(text=instruction),
        model_hint=None,
        extra=build_extra(session),
    )

    final_text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    error: dict[str, Any] | None = None

    try:
        iterator = await container.chat.stream_chat_use_case.execute(request)
        async for frame in iterator:
            ftype = getattr(getattr(frame, "frame_type", None), "value", None) or str(
                getattr(frame, "frame_type", "")
            )
            payload = getattr(frame, "payload", {}) or {}
            if ftype == "chunk":
                final_text_parts.append(str(payload.get("text", "")))
            elif ftype == "tool_call":
                tool_calls.append(
                    {
                        "tool_name": payload.get("tool_name"),
                        "arguments": payload.get("arguments"),
                    }
                )
            elif ftype == "tool_result" and not payload.get("partial"):
                if tool_calls:
                    tool_calls[-1]["result"] = payload.get("result")
            elif ftype == "error":
                error = {"code": payload.get("code"), "message": payload.get("message")}
    except Exception as exc:  # noqa: BLE001 — tool result must never crash
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "conversation_id": conv.id.value,
        }

    return {
        "ok": error is None,
        "final_text": "".join(final_text_parts),
        "tool_calls": tool_calls,
        "error": error,
        "conversation_id": conv.id.value,
    }
