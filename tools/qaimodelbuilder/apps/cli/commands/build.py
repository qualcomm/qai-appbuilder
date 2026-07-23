"""``qai build`` — Model Builder agentic chat session (interactive REPL, D4/L1).

Desktop App Plan §2.1.1 group L1 + cli-interactive-design §3.2 / §3.4. This is
the CLI surface for the *Model Builder* experience the WebUI exposes through
``ModeFrameModelBuilder.vue`` — a streaming, agentic chat session whose turns
carry ``tool_mode == "model-build"`` plus three tool params (model file paths /
quantisation precision / dataset path). The Agent (a cloud LLM) drives the
conversion conversation; the user steers it with natural language plus a small
set of slash commands that mirror the six WebUI controls.

Relationship to the rest of the CLI
------------------------------------
Unlike ``qai conv`` / ``qai config`` (one-shot ``qai <verb>`` via
:func:`apps.cli._runtime.run_use_case`), ``qai build`` is a **long-lived REPL**:
it opens ONE :class:`~apps.api.di.Container` (via
:func:`apps.cli._repl.repl_container`) and keeps it alive for the whole session
so the chat stream + EventBus subscriptions stay live across many turns. All
terminal rendering is delegated to the shared kernel
(:class:`apps.cli._render.StreamFrameRenderer`) and all ``/`` routing to
:class:`apps.cli._repl.SlashDispatcher` — this file holds *no* business logic
and *no* frame formatting, only session-state plumbing.

``--model-file`` vs ``--llm`` (deliberate disambiguation — 判据1)
----------------------------------------------------------------
cli-interactive-design §3.2 shows ``qai build --model ./yolov8n.pt`` (the file
to convert) AND notes ``--model <model-id>`` can pick the Agent's cloud LLM —
the same flag name for two unrelated things. That ambiguity would bite an
operator the moment they tried to pass both. We split it cleanly:

* ``--model-file`` / ``-f <path>``  → the model FILE to convert
  (→ ``tool_params.model_paths``; repeatable).
* ``--llm <model-id>``              → the Agent's cloud LLM
  (→ ``StreamChatInput.model_hint``).

This is clearer than V1's single overloaded knob (V1 had no CLI at all) while
keeping behaviour aligned with the WebUI's model-build tool params.

Permission handling (research finding 2026-06-11)
--------------------------------------------------
Chat streaming exec does **not** trigger the ai_coding ``PERMISSION_REQUESTED``
gate — it relies on ``file_guard`` (default OFF) and has no interactive
permission path. So ``qai build`` will not normally receive permission events.
We still wire :class:`apps.cli._repl.PermissionBridge` *defensively* (subscribe
so that IF an event ever fires it is queued rather than lost), but we never
block a turn waiting on permissions. The full decide-loop
(``decide_permission_use_case``) is D5's concern.

待 SDK / 待后端
---------------
* Real LLM round-trips need a configured cloud provider (precheck below) and
  network; the conversion itself needs QAIRT + a model runner. Neither is
  available in the smoke test, which exercises only the non-streaming surface.
* ``/promote`` (export to a pack) and ``/workspace`` / ``/status`` full wiring
  depend on Model Builder backend use cases not yet reachable from the CLI;
  they print a clear hint rather than crash (see handlers).

Exit codes
----------
* 0   — clean ``/exit`` / EOF / normal end.
* 1   — precheck failed (no cloud provider configured).
* 130 — SIGINT (the top-level dispatcher maps ``KeyboardInterrupt`` → 130).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.text import Text

from apps.cli._pager import show_pager
from apps.cli._render import RenderOptions, StreamFrameRenderer
from apps.cli._render_theme import build_console, icon
from apps.cli._repl import (
    InterruptController,
    PermissionBridge,
    SlashDispatcher,
    async_read_line,
    repl_container,
)
from apps.cli._session_log import SessionLog, cleanup_repl_session

__all__ = ["register", "build_extra", "QUANT_PRECISIONS"]


#: The seven quantisation precision levels the Model Builder tool accepts
#: (cli-interactive-design / ModeFrameModelBuilder.vue). ``--precision`` stores
#: the raw CSV string into ``tool_params.quant_precision`` (design uses a single
#: string field), so callers can pass ``fp16`` or ``fp16,w8a8`` verbatim.
QUANT_PRECISIONS = (
    "fp32",
    "fp16",
    "w8a16",
    "w8a8",
    "w8a8b8",
    "w4a16",
    "w4a8",
)

#: The canonical request-side tool_mode string. The system-prompt builder
#: normalises the aliases ``model-build`` / ``model_build`` / ``model_builder``
#: (``system_prompt_builder.py:_MODEL_BUILD_MODES``); the frontend wire value is
#: the hyphenated ``model-build``, which we send for parity.
TOOL_MODE = "model-build"


# ---------------------------------------------------------------------------
# Session state + pure extra-builder (unit-testable, no I/O)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BuildSession:
    """Mutable per-session Model Builder state steered by slash commands.

    Mirrors the six ``ModeFrameModelBuilder.vue`` controls: model file(s),
    quant precision, dataset path, and the batch/interactive mode toggle.
    """

    model_paths: list[str] = field(default_factory=list)
    quant_precision: str | None = None
    dataset_path: str | None = None
    mode: str = "interactive"  # "batch" | "interactive"
    last_user_message: str | None = None


def build_extra(session: BuildSession) -> dict[str, Any]:
    """Build the ``StreamChatInput.extra`` dict from current session state.

    Pure function (no I/O) so the smoke test can assert the exact shape::

        {"tool_mode": "model-build",
         "tool_params": {"model_paths": [...], "quant_precision": "...",
                         "dataset_path": "..."}}

    Only keys that are actually set are emitted into ``tool_params`` so a
    half-configured session does not send empty/None values the backend would
    have to defend against.
    """

    tool_params: dict[str, Any] = {}
    if session.model_paths:
        tool_params["model_paths"] = list(session.model_paths)
    if session.quant_precision:
        tool_params["quant_precision"] = session.quant_precision
    if session.dataset_path:
        tool_params["dataset_path"] = session.dataset_path
    return {"tool_mode": TOOL_MODE, "tool_params": tool_params}


def _out_console(opts: RenderOptions) -> Console:
    return build_console(color=opts.color, emoji=opts.emoji, stream=sys.stdout)


def _err_console(opts: RenderOptions) -> Console:
    return build_console(color=opts.color, emoji=opts.emoji, stream=sys.stderr)


def _params_summary(session: BuildSession) -> str:
    """Human-readable one-block summary of the current session params."""

    files = ", ".join(session.model_paths) if session.model_paths else "(未设置)"
    prec = session.quant_precision or "(未设置)"
    ds = session.dataset_path or "(未设置)"
    return (
        f"  模型文件 : {files}\n"
        f"  量化精度 : {prec}\n"
        f"  数据集   : {ds}\n"
        f"  模式     : {session.mode}"
    )


# ---------------------------------------------------------------------------
# argparse registration
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``qai build`` command (group L1)."""

    p = subparsers.add_parser(
        "build",
        help="Model Builder 交互式会话（agentic 模型转换 REPL）",
        description=(
            "进入 Model Builder agentic 聊天会话：用自然语言 + 斜杠命令引导云端 "
            "Agent 完成模型转换。--model-file 指定要转换的模型文件（可多次），"
            "--llm 指定 Agent 使用的云端大模型。会话内用 /model /precision "
            "/dataset /mode /run 调整参数，/help 查看全部命令。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--model-file",
        "-f",
        action="append",
        default=None,
        metavar="<path>",
        dest="model_files",
        help=(
            "要转换的模型文件路径（→ tool_params.model_paths）。可重复传入多个。"
            "注意：这是被转换的文件，不是 Agent 的大模型（后者用 --llm）。"
        ),
    )
    p.add_argument(
        "--llm",
        default=None,
        metavar="<model-id>",
        dest="llm",
        help=(
            "Agent 使用的云端大模型 id（→ model_hint）。CLI 不支持本地 LLM。"
        ),
    )
    p.add_argument(
        "--precision",
        default=None,
        metavar="<csv>",
        dest="precision",
        help=(
            "量化精度（→ tool_params.quant_precision），如 fp16 或 fp16,w8a8。"
            f"可选级别: {', '.join(QUANT_PRECISIONS)}。"
        ),
    )
    p.add_argument(
        "--dataset",
        default=None,
        metavar="<path>",
        dest="dataset",
        help="校准/评测数据集路径（→ tool_params.dataset_path）。",
    )
    p.add_argument(
        "--mode",
        choices=("batch", "interactive"),
        default="interactive",
        dest="initial_mode",
        help="初始模式：interactive（默认）逐轮交互；batch 偏向一次性转换。",
    )
    p.add_argument(
        "--resume",
        nargs="?",
        const="__latest__",
        default=None,
        metavar="<conversation-id>",
        dest="resume",
        help=(
            "续接已有的 model-build 会话：给出 conversation-id 续接指定会话；"
            "不带参数则尽力续接最近一个（best-effort）。"
        ),
    )
    p.set_defaults(handler=cmd_build)


# ---------------------------------------------------------------------------
# Handler (sync argparse boundary → async REPL)
# ---------------------------------------------------------------------------


def cmd_build(args: argparse.Namespace) -> int:
    """Sync entry point. Runs the async REPL via :func:`asyncio.run`."""

    return asyncio.run(_run_build(args))


async def _precheck_cloud_provider(c: Any) -> bool:
    """Return True iff at least one cloud provider is configured.

    ``model_catalog`` is a different bounded context, but ``apps/cli`` is the
    cross-context entry layer (same pattern ``config.py`` uses for
    ``qai config provider list``).
    """

    try:
        rows = await c.model_catalog.list_provider_configs_use_case.execute()
    except Exception:  # noqa: BLE001 — precheck must never mask with a trace
        return False
    return bool(rows)


async def _resolve_resume_conversation(c: Any, resume: str) -> str | None:
    """Best-effort resolve a conversation id to resume.

    A specific id is returned verbatim (the backend validates it on open).
    ``__latest__`` tries ``list_conversations_use_case`` and picks the most
    recently updated row; if that path is unavailable we return ``None`` so the
    caller falls back to creating a fresh conversation (resume-latest is
    documented as best-effort).
    """

    if resume != "__latest__":
        return resume
    lister = getattr(c.chat, "list_conversations_use_case", None)
    if lister is None:
        return None
    try:
        from qai.chat.application.use_cases.conversation_management import (
            ListConversationsInput,
        )

        rows = await lister.execute(ListConversationsInput(limit=1, offset=0))
    except Exception:  # noqa: BLE001 — best-effort; fall back to new conv
        return None
    if not rows:
        return None
    row = rows[0]
    conv = getattr(row, "conversation", None)
    conv_id = getattr(conv, "id", None)
    return getattr(conv_id, "value", None)


async def _run_build(args: argparse.Namespace) -> int:
    repo_root: Path | None = getattr(args, "repo_root", None)
    config_file: Path | None = getattr(args, "config_file", None)

    opts = RenderOptions.from_streams(sys.stdout, sys.stderr)

    async with repl_container(
        config_file=config_file, repo_root=repo_root
    ) as c:
        # ── Cloud-provider precheck (MUST run before any interactive read so
        #    a non-tty / no-provider invocation exits cleanly without hanging).
        if not await _precheck_cloud_provider(c):
            sys.stderr.write(
                "未检测到云端模型，请先运行 qai config setup 配置 provider\n"
            )
            sys.stderr.flush()
            return 1

        # ── Conversation FIRST, then tab (open_tab does NOT auto-create one).
        from qai.chat.application.use_cases.conversation_management import (
            CreateConversationInput,
        )
        from qai.chat.application.use_cases.tab_management import OpenTabInput

        resume_target = getattr(args, "resume", None)
        conv_id_str: str | None = None
        if resume_target is not None:
            conv_id_str = await _resolve_resume_conversation(c, resume_target)

        if conv_id_str is None:
            conv = await c.chat.create_conversation_use_case.execute(
                CreateConversationInput(title="Model Build")
            )
            conv_id_str = conv.id.value
        tab = await c.chat.open_tab_use_case.execute(
            OpenTabInput(conversation_id=conv_id_str)
        )

        from qai.chat.domain.ids import ConversationId, TabId

        conversation_id = ConversationId.of(conv_id_str)
        tab_id: Any = tab.id if not isinstance(tab.id, str) else TabId.of(tab.id)

        # ── Session log tees every themed-console print from here on into
        #    <cli_sessions_dir>/<conv_id>.log; must open before the first
        #    print (banner below) and before `renderer` is built so its
        #    captured stdout/stderr reference is already teed.
        session_log = SessionLog(c.data_paths, conv_id_str)
        renderer = StreamFrameRenderer(opts, out=sys.stdout, err=sys.stderr)

        # ── Session state from initial flags.
        session = BuildSession(
            model_paths=list(getattr(args, "model_files", None) or []),
            quant_precision=getattr(args, "precision", None),
            dataset_path=getattr(args, "dataset", None),
            mode=getattr(args, "initial_mode", "interactive") or "interactive",
        )
        model_hint: str | None = getattr(args, "llm", None)

        # ── Defensive permission bridge (chat path normally never fires; see
        #    module docstring). Subscribe so an event is queued not lost; we
        #    never block a turn on it. Full decide-loop is D5.
        perm_bridge = PermissionBridge()
        await perm_bridge.subscribe(c)

        interrupts = InterruptController()

        _print_banner(conv_id_str, session, model_hint, opts)

        dispatcher = _build_dispatcher(
            c=c,
            session=session,
            conversation_id=conversation_id,
            tab_id=tab_id,
            renderer=renderer,
            model_hint=model_hint,
            interrupts=interrupts,
            opts=opts,
        )

        try:
            return await _repl_loop(
                c=c,
                dispatcher=dispatcher,
                session=session,
                conversation_id=conversation_id,
                tab_id=tab_id,
                renderer=renderer,
                model_hint=model_hint,
                interrupts=interrupts,
                perm_bridge=perm_bridge,
                opts=opts,
            )
        finally:
            await perm_bridge.unsubscribe()
            cleanup_repl_session(session_log)


def _print_banner(
    conv_id: str,
    session: BuildSession,
    model_hint: str | None,
    opts: RenderOptions,
) -> None:
    console = _out_console(opts)
    console.print(Text("Model Builder 会话已就绪。", style="heading"))
    console.print(Text(f"  会话 id  : {conv_id}"))
    if model_hint:
        console.print(Text(f"  Agent LLM: {model_hint}"))
    console.print(Text(_params_summary(session)))
    console.print(
        Text("输入自然语言与 Agent 对话，或用斜杠命令调整参数（/help 查看全部）。")
    )


# ---------------------------------------------------------------------------
# Streaming a turn
# ---------------------------------------------------------------------------


async def _stream_turn(
    *,
    c: Any,
    text: str,
    session: BuildSession,
    conversation_id: Any,
    tab_id: Any,
    renderer: StreamFrameRenderer,
    model_hint: str | None,
    interrupts: InterruptController,
    opts: RenderOptions,
) -> None:
    """Send one user message and render the streamed frames.

    A first Ctrl+C aborts this turn (via ``stop_chat_use_case`` keyed by
    ``tab_id``) without exiting the REPL; the renderer's partial output is left
    as-is. Errors during streaming surface through an ``error`` frame from the
    backend; an unexpected exception is reported to stderr (not a traceback).
    """

    from qai.chat.application.use_cases.streaming import StreamChatInput
    from qai.chat.domain.content import MessageContent

    session.last_user_message = text
    interrupts.reset()
    request = StreamChatInput(
        tab_id=tab_id,
        conversation_id=conversation_id,
        user_message=MessageContent(text=text),
        model_hint=model_hint,
        extra=build_extra(session),
    )

    async def _consume() -> None:
        iterator = await c.chat.stream_chat_use_case.execute(request)
        async for frame in iterator:
            renderer.render(frame)

    task = asyncio.ensure_future(_consume())
    try:
        await task
    except asyncio.CancelledError:
        # Turn was cancelled by Ctrl+C; abort the backend stream by tab id.
        await _abort_turn(c, tab_id)
        warn = icon("warning", emoji=opts.emoji)
        prefix = f"{warn} " if warn else ""
        _err_console(opts).print(
            Text(f"\n{prefix}（已中断当前回合）", style="warning")
        )
    except Exception as exc:  # noqa: BLE001 — REPL must survive a bad turn
        cross = icon("error", emoji=opts.emoji)
        prefix = f"{cross} " if cross else ""
        _err_console(opts).print(
            Text(
                f"\n{prefix}回合失败: {type(exc).__name__}: {exc}",
                style="error",
            )
        )


async def _abort_turn(c: Any, tab_id: Any) -> None:
    """Abort the in-flight stream for ``tab_id`` (best-effort)."""

    stop = getattr(c.chat, "stop_chat_use_case", None)
    if stop is None:
        return
    with contextlib.suppress(Exception):
        result = stop.execute(tab_id)
        if asyncio.iscoroutine(result):
            await result


# ---------------------------------------------------------------------------
# Slash command wiring
# ---------------------------------------------------------------------------


def _build_dispatcher(
    *,
    c: Any,
    session: BuildSession,
    conversation_id: Any,
    tab_id: Any,
    renderer: StreamFrameRenderer,
    model_hint: str | None,
    interrupts: InterruptController,
    opts: RenderOptions,
) -> SlashDispatcher:
    """Register every ``/`` command (cli-interactive-design §3.4).

    Commands that map directly onto session state (/model /precision /dataset
    /params /mode /clear /exit /help /stop /retry /run) are fully wired. The
    ones that need Model Builder backend use cases not reachable from the CLI
    (/status /workspace /promote /history) print a clear "尚未接通" note rather
    than crash — keeping the REPL robust (判据2: no regression vs the WebUI's
    own progressive availability).
    """

    d = SlashDispatcher(console=_out_console(opts))

    async def _help(_rest: str) -> bool:
        _out_console(opts).print(Text(d.render_help()))
        return True

    async def _model(rest: str) -> bool:
        rest = rest.strip()
        console = _out_console(opts)
        if not rest:
            cur = ", ".join(session.model_paths) if session.model_paths else "(未设置)"
            console.print(Text(f"当前模型文件: {cur}"))
        else:
            session.model_paths = [p for p in rest.split() if p]
            ok = icon("success", emoji=opts.emoji)
            prefix = f"{ok} " if ok else ""
            console.print(
                Text(
                    f"{prefix}已设置模型文件: {', '.join(session.model_paths)}",
                    style="success",
                )
            )
        return True

    async def _precision(rest: str) -> bool:
        rest = rest.strip()
        if not rest:
            _out_console(opts).print(
                Text(f"当前量化精度: {session.quant_precision or '(未设置)'}")
            )
            return True
        invalid = [
            lvl
            for lvl in (s.strip() for s in rest.split(","))
            if lvl and lvl not in QUANT_PRECISIONS
        ]
        if invalid:
            cross = icon("error", emoji=opts.emoji)
            prefix = f"{cross} " if cross else ""
            _err_console(opts).print(
                Text(
                    f"{prefix}无效精度级别: {', '.join(invalid)}；"
                    f"可选: {', '.join(QUANT_PRECISIONS)}",
                    style="error",
                )
            )
            return True
        session.quant_precision = rest
        ok = icon("success", emoji=opts.emoji)
        prefix = f"{ok} " if ok else ""
        _out_console(opts).print(
            Text(f"{prefix}已设置量化精度: {session.quant_precision}", style="success")
        )
        return True

    async def _dataset(rest: str) -> bool:
        rest = rest.strip()
        console = _out_console(opts)
        if not rest:
            console.print(Text(f"当前数据集: {session.dataset_path or '(未设置)'}"))
        else:
            session.dataset_path = rest
            ok = icon("success", emoji=opts.emoji)
            prefix = f"{ok} " if ok else ""
            console.print(
                Text(f"{prefix}已设置数据集: {session.dataset_path}", style="success")
            )
        return True

    async def _params(_rest: str) -> bool:
        _out_console(opts).print(Text("当前会话参数:\n" + _params_summary(session)))
        return True

    async def _mode(rest: str) -> bool:
        rest = rest.strip().lower()
        console = _out_console(opts)
        if rest not in ("batch", "interactive"):
            console.print(
                Text(f"当前模式: {session.mode}（用 /mode batch|interactive 切换）")
            )
        else:
            session.mode = rest
            ok = icon("success", emoji=opts.emoji)
            prefix = f"{ok} " if ok else ""
            console.print(Text(f"{prefix}已切换模式: {session.mode}", style="success"))
        return True

    async def _run(_rest: str) -> bool:
        if not session.model_paths:
            cross = icon("error", emoji=opts.emoji)
            prefix = f"{cross} " if cross else ""
            _err_console(opts).print(
                Text(
                    f"{prefix}尚未设置模型文件，请先 /model <path> 或启动时用 --model-file。",
                    style="error",
                )
            )
            return True
        instruction = _standard_run_instruction(session)
        await _stream_turn(
            c=c,
            text=instruction,
            session=session,
            conversation_id=conversation_id,
            tab_id=tab_id,
            renderer=renderer,
            model_hint=model_hint,
            interrupts=interrupts,
            opts=opts,
        )
        return True

    async def _retry(_rest: str) -> bool:
        if not session.last_user_message:
            cross = icon("error", emoji=opts.emoji)
            prefix = f"{cross} " if cross else ""
            _err_console(opts).print(
                Text(f"{prefix}没有可重发的上一条消息。", style="error")
            )
            return True
        await _stream_turn(
            c=c,
            text=session.last_user_message,
            session=session,
            conversation_id=conversation_id,
            tab_id=tab_id,
            renderer=renderer,
            model_hint=model_hint,
            interrupts=interrupts,
            opts=opts,
        )
        return True

    async def _stop(_rest: str) -> bool:
        await _abort_turn(c, tab_id)
        ok = icon("success", emoji=opts.emoji)
        prefix = f"{ok} " if ok else ""
        _out_console(opts).print(
            Text(f"{prefix}已请求中止当前回合。", style="success")
        )
        return True

    async def _clear(_rest: str) -> bool:
        # Start a fresh conversation + tab in place (re-bind handled by the
        # loop closure capturing mutable holders is overkill here; we instead
        # mutate the VO objects the loop already references).
        from qai.chat.application.use_cases.conversation_management import (
            CreateConversationInput,
        )
        from qai.chat.application.use_cases.tab_management import OpenTabInput
        from qai.chat.domain.ids import ConversationId, TabId

        conv = await c.chat.create_conversation_use_case.execute(
            CreateConversationInput(title="Model Build")
        )
        tab = await c.chat.open_tab_use_case.execute(
            OpenTabInput(conversation_id=conv.id.value)
        )
        # Rebind the captured ids via the holder the loop reads each turn
        # (the VOs are frozen, so we swap the holder entries rather than mutate).
        new_conv = ConversationId.of(conv.id.value)
        new_tab = tab.id if not isinstance(tab.id, str) else TabId.of(tab.id)
        _ID_HOLDER["conversation_id"] = new_conv
        _ID_HOLDER["tab_id"] = new_tab
        session.last_user_message = None
        ok = icon("success", emoji=opts.emoji)
        prefix = f"{ok} " if ok else ""
        _out_console(opts).print(
            Text(f"{prefix}已开启新会话: {conv.id.value}", style="success")
        )
        return True

    async def _history(_rest: str) -> bool:
        getter = getattr(c.chat, "get_conversation_messages_use_case", None)
        if getter is None:
            _out_console(opts).print(
                Text("历史读取尚未接通（CLI 暂不可达该用例）。", style="dim")
            )
            return True
        try:
            from qai.chat.application.use_cases.conversation_management import (
                GetConversationMessagesInput,
            )

            page = await getter.execute(
                GetConversationMessagesInput(
                    conversation_id=_ID_HOLDER["conversation_id"],
                    cursor=None,
                    limit=50,
                )
            )
        except Exception as exc:  # noqa: BLE001 — robust print, no trace
            cross = icon("error", emoji=opts.emoji)
            prefix = f"{cross} " if cross else ""
            _err_console(opts).print(
                Text(
                    f"{prefix}读取历史失败: {type(exc).__name__}: {exc}",
                    style="error",
                )
            )
            return True
        items = list(getattr(page, "items", ()) or ())
        if not items:
            _out_console(opts).print(Text("（暂无历史消息）", style="dim"))
            return True
        console = _out_console(opts)
        for msg in items:
            role = getattr(getattr(msg, "role", None), "value", "?")
            content = getattr(msg, "content", "")
            console.print(Text(f"[{role}] {content}"))
        return True

    async def _status(_rest: str) -> bool:
        _out_console(opts).print(
            Text(
                "运行状态查询尚未接通（依赖 Model Builder 后端用例）。"
                "可用 /params 查看当前参数。",
                style="dim",
            )
        )
        return True

    async def _workspace(_rest: str) -> bool:
        _out_console(opts).print(
            Text("工作区查看尚未接通（依赖 Model Builder 后端用例）。", style="dim")
        )
        return True

    async def _promote(_rest: str) -> bool:
        _out_console(opts).print(
            Text(
                "导出/晋升为 pack 尚未在 CLI 接通。"
                "转换产物就绪后可运行: qai pack import <artifact>",
                style="dim",
            )
        )
        return True

    async def _show(rest: str) -> bool:
        rest = rest.strip()
        idx: int | None
        if rest:
            try:
                idx = int(rest)
            except ValueError:
                cross = icon("error", emoji=opts.emoji)
                prefix = f"{cross} " if cross else ""
                _err_console(opts).print(
                    Text(f"{prefix}用法: /show [<折叠序号>]", style="error")
                )
                return True
        else:
            idx = None
        folded_text = renderer.folded(idx)
        if folded_text is None:
            _out_console(opts).print(
                Text(
                    "没有可展开的内容（尚无折叠的工具结果，或序号超出范围）。",
                    style="dim",
                )
            )
            return True
        shown_idx = idx if idx is not None else renderer.last_fold_index
        await show_pager(folded_text, title=f"/show {shown_idx}")
        return True

    async def _exit(_rest: str) -> bool:
        return False  # request REPL exit

    d.register("help", "显示全部命令", _help, aliases=("?",))
    d.register("model", "<path...> 设置/查看要转换的模型文件", _model)
    d.register("precision", "<csv> 设置/查看量化精度", _precision)
    d.register("dataset", "<path> 设置/查看数据集", _dataset)
    d.register("params", "查看当前模型/精度/数据集/模式", _params)
    d.register("mode", "batch|interactive 切换模式", _mode)
    d.register("run", "用当前参数发起一次标准转换指令", _run)
    d.register("retry", "重发上一条用户消息", _retry)
    d.register("stop", "中止当前回合", _stop)
    d.register("status", "查看运行状态（尚未接通）", _status)
    d.register("workspace", "查看工作区（尚未接通）", _workspace)
    d.register("promote", "导出为 pack（提示用 qai pack import）", _promote)
    d.register("history", "打印会话历史消息", _history)
    d.register("show", "[<n>] 全屏查看折叠的工具结果（不带参数查看最近一次）", _show)
    d.register("clear", "开启新会话", _clear)
    d.register("exit", "退出会话", _exit, aliases=("quit",))
    return d


#: Holder so ``/clear`` can rebind the frozen ConversationId / TabId VOs the
#: REPL loop reads each turn (set up in :func:`_run_build` before the loop).
_ID_HOLDER: dict[str, Any] = {}


def _standard_run_instruction(session: BuildSession) -> str:
    """Build the natural-language instruction ``/run`` sends to the Agent.

    The Agent's system prompt already renders the model-build tool_params
    (``system_prompt_builder._render_model_build_params``); this message just
    asks it to proceed, so the transcript reads naturally.
    """

    files = "、".join(session.model_paths)
    parts = [f"请将模型文件 {files} 转换为可在 NPU 上运行的格式"]
    if session.quant_precision:
        parts.append(f"，量化精度 {session.quant_precision}")
    if session.dataset_path:
        parts.append(f"，使用数据集 {session.dataset_path} 做校准/评测")
    parts.append("。")
    return "".join(parts)


# ---------------------------------------------------------------------------
# REPL loop
# ---------------------------------------------------------------------------


async def _repl_loop(
    *,
    c: Any,
    dispatcher: SlashDispatcher,
    session: BuildSession,
    conversation_id: Any,
    tab_id: Any,
    renderer: StreamFrameRenderer,
    model_hint: str | None,
    interrupts: InterruptController,
    perm_bridge: Any = None,
    opts: RenderOptions,
) -> int:
    """Drive the interactive loop until ``/exit`` / EOF / double Ctrl+C.

    Ctrl+C: a first press cancels the current turn (handled inside
    :func:`_stream_turn`); pressing it again at the prompt within the
    interrupt window exits. Ctrl+D (EOFError) / closed stdin exits cleanly.

    After each natural-language turn, any permission events the backend
    published mid-stream are drained + resolved interactively (D5; the chat
    path rarely fires one — see module docstring).
    """

    _ID_HOLDER["conversation_id"] = conversation_id
    _ID_HOLDER["tab_id"] = tab_id

    is_slash = _import_is_slash()
    allow_set: set[str] = set()

    while True:
        try:
            line = await async_read_line("build › ")
        except EOFError:
            _out_console(opts).print(Text("\n再见。", style="dim"))
            return 0
        except KeyboardInterrupt:
            # A bare Ctrl+C at the prompt: second within the window exits.
            if interrupts.signal():
                _out_console(opts).print(Text("\n再见。", style="dim"))
                return 0
            warn = icon("warning", emoji=opts.emoji)
            prefix = f"{warn} " if warn else ""
            _err_console(opts).print(
                Text(f"\n{prefix}（再次按 Ctrl+C 退出）", style="warning")
            )
            continue

        if line is None:
            continue
        stripped = line.strip()
        if not stripped:
            continue

        if is_slash(line):
            _handled, keep = await dispatcher.dispatch(line)
            if not keep:
                _out_console(opts).print(Text("再见。", style="dim"))
                return 0
            continue

        # Natural-language turn → stream. Wrap so a turn-level Ctrl+C cancels
        # the turn (not the whole REPL).
        await _run_turn_with_interrupt(
            c=c,
            text=line,
            session=session,
            renderer=renderer,
            model_hint=model_hint,
            interrupts=interrupts,
            opts=opts,
        )

        # Resolve any permission requests queued during the turn (D5).
        if perm_bridge is not None:
            await _resolve_permissions(c, perm_bridge, allow_set)


async def _run_turn_with_interrupt(
    *,
    c: Any,
    text: str,
    session: BuildSession,
    renderer: StreamFrameRenderer,
    model_hint: str | None,
    interrupts: InterruptController,
    opts: RenderOptions,
) -> None:
    """Run one turn; convert a Ctrl+C during the turn into a turn cancel."""

    turn = asyncio.ensure_future(
        _stream_turn(
            c=c,
            text=text,
            session=session,
            conversation_id=_ID_HOLDER["conversation_id"],
            tab_id=_ID_HOLDER["tab_id"],
            renderer=renderer,
            model_hint=model_hint,
            interrupts=interrupts,
            opts=opts,
        )
    )
    try:
        await turn
    except KeyboardInterrupt:
        # Cancel the turn task; _stream_turn handles the abort + message.
        turn.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await turn


def _import_is_slash() -> Any:
    """Import ``is_slash_command`` lazily (kept off module import for clarity)."""

    from apps.cli._repl import is_slash_command

    return is_slash_command


async def _resolve_permissions(c: Any, perm_bridge: Any, allow_set: set[str]) -> None:
    """Drain + interactively resolve queued permission requests (D5).

    Delegates to the shared kernel resolver, which prompts (custom terminal
    confirm, §3.9.2) and decides via ai_coding ``decide_permission_use_case``.
    Best-effort: a resolver error never breaks the REPL.
    """

    from apps.cli._repl import resolve_pending_permissions

    with contextlib.suppress(Exception):
        await resolve_pending_permissions(
            c, perm_bridge, allow_set=allow_set
        )
