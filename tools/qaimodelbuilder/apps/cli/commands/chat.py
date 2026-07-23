"""``apps.cli.commands.chat`` — generic default chat REPL (Phase 2 Step 4).

Delivery plan Phase 2 §Step 4: ``qai`` invoked with no subcommand on an
interactive terminal now drops the operator straight into a plain agentic
chat session instead of an argparse usage error. This module extracts the
generic REPL scaffolding out of ``commands/build.py`` (session creation,
streaming-turn loop, interrupt/permission wiring, slash dispatch, session
log lifecycle) minus everything specific to the Model Builder tool
(``BuildSession``, ``--model-file``/``--precision``/``--dataset``, and the
conversion-specific slash commands).

Relationship to ``qai build`` / ``qai app``
--------------------------------------------
Same long-lived-``Container`` REPL pattern (:func:`apps.cli._repl.repl_container`),
same shared rendering kernel (:class:`apps.cli._render.StreamFrameRenderer`),
same ``/`` routing (:class:`apps.cli._repl.SlashDispatcher`). Unlike ``qai
build``, this entry point is not registered as its own subcommand — it is
only reachable as the *default* action ``apps.cli.__main__.main`` dispatches
to when ``args.command is None`` on a TTY (see that module for the
non-TTY/usage-error split).

``tool_mode`` choice (judgement call)
--------------------------------------
``RichSystemPromptBuilder.build`` (``system_prompt_builder.py``) treats ANY
non-empty ``tool_mode`` string as a "feature/tool mode" and renders a
"你正在执行【<mode>】专项任务" framing sentence for unrecognised values — the
full, plain DEFAULT system prompt is only assembled when ``tool_mode`` is
falsy. So a generic default chat entry point behaves most correctly with
``tool_mode=None`` (see :data:`TOOL_MODE` below), NOT a new placeholder
string — that is the actual "generic/default" system-prompt behaviour this
entry point should offer.

Provider precheck + local-first activation (Step 6, redesigned in Step 9)
----------------------------------------------------------------------------
Reuses ``commands/build.py``'s ``_precheck_cloud_provider`` verbatim (no
change to ``qai build``'s own behaviour) — but here it is only ever a
READ-ONLY check, used solely to decide whether the welcome banner shows a
"no model configured" hint. Entering this session never blocks on, and never
silently triggers, a real network download: LOCAL-FIRST activation
(:func:`_activate_local_model`) is user-triggered from *inside* the session
via the ``/model`` command (see the ``_model`` dispatcher handler) —
confirm (or install) a local GenieAPIService + model via the same
``qai.service_release`` use cases the ``service-release install
service/model`` commands use, then register its OpenAI-compatible endpoint
as the ``"local-genie"`` provider (``UpdateProviderConfigUseCase`` +
``ProbeProviderCommand``, same shape ``commands/config.py``'s
``_setup_one_provider`` writes). Only ever runs on a TTY (the whole entry
point is already TTY-gated) and only when the user explicitly asks for it
via ``/model`` — never automatically, and never before the session's first
interactive read.

Model selection (Step 10)
----------------------------
``/model`` with zero providers configured no longer silently downloads a
single hard-coded default model. It lists the full remote catalog
(:func:`_show_installable_catalog`) and lets the user pick any entry by
1-based index or literal ``model_id`` (:func:`_resolve_catalog_choice`),
then drives local-first activation for THAT specific model
(:func:`_activate_local_model`'s optional ``model_id`` argument). The old
"first NPU entry, else the first entry" rule (below) is now only the
fallback used when no explicit model was requested (e.g. by a caller other
than ``/model`` itself).

Judgement calls (no built-in "pick something sensible" default upstream)
--------------------------------------------------------------------------
* Default service version: the catalog entry with ``is_recommended`` set,
  else the first entry ``list_service_versions_use_case`` returns.
* Default model (only when none was explicitly requested): the first
  catalog entry with ``hardware == NPU`` (GenieAPIService's always-on
  backend), else the first entry ``list_catalog_models_use_case`` returns.
* Local endpoint base url: delegated verbatim to
  ``apps/api/_local_service_endpoint_bridge.make_local_service_endpoint_provider``
  (the same bridge ``apps/api/_chat_di.py`` wires into the API-side chat
  resolver) — running daemon's actual port, else ``forge.config``, else
  ``Settings.model_runtime.default_port`` (8910). We do NOT start the daemon
  ourselves — a fresh install is not necessarily running yet, and
  ``ProbeProviderUseCase`` already performs a real connectivity check, so an
  unreachable endpoint surfaces as an ordinary graceful probe failure (step
  e), not a hang or a crash.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.metadata
import sys
from collections.abc import Awaitable, Callable
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
    is_real_tty,
    repl_container,
)
from apps.cli._session_log import SessionLog, cleanup_repl_session
from apps.cli.commands.build import _precheck_cloud_provider

__all__ = ["cmd_chat", "build_extra"]


#: See module docstring ("``tool_mode`` choice"): a generic default chat
#: session deliberately sends no tool_mode so the backend assembles its
#: normal full default system prompt, instead of the degraded "专项任务"
#: framing an arbitrary non-empty placeholder string would trigger.
TOOL_MODE: str | None = None

#: Provider id the local-first activation flow registers (see module
#: docstring "Provider precheck + local-first activation").
_LOCAL_PROVIDER_ID = "local-genie"


def build_extra() -> dict[str, Any]:
    """Build the ``StreamChatInput.extra`` dict for a default chat turn."""

    return {"tool_mode": TOOL_MODE, "tool_params": {}}


def _out_console(opts: RenderOptions) -> Console:
    return build_console(color=opts.color, emoji=opts.emoji, stream=sys.stdout)


def _err_console(opts: RenderOptions) -> Console:
    return build_console(color=opts.color, emoji=opts.emoji, stream=sys.stderr)


# ---------------------------------------------------------------------------
# Handler (sync argparse boundary → async REPL)
# ---------------------------------------------------------------------------


def cmd_chat(args: argparse.Namespace) -> int:
    """Sync entry point. Runs the async REPL via :func:`asyncio.run`."""

    return asyncio.run(_run_chat(args))


def _print_no_model_guidance(opts: RenderOptions) -> None:
    warn = icon("warning", emoji=opts.emoji)
    prefix = f"{warn} " if warn else ""
    _err_console(opts).print(
        Text(
            f"{prefix}当前没有可用的模型：本地模型激活未能完成，"
            "且尚未配置云端 provider。请运行 qai config setup 配置云端 "
            "provider，或运行 /model 重试本地激活。",
            style="warning",
        )
    )


async def _local_service_and_model_present(
    c: Any, *, requested_model_id: str | None = None
) -> tuple[bool, str | None]:
    """Idempotency check: is a usable local install already on disk?

    Reuses the same use cases ``service-release status versions/models``
    call (:func:`apps.cli.commands.service_release.cmd_status_versions` /
    ``cmd_status_models``). Returns ``(service_installed, installed_model_id)``
    — ``installed_model_id`` is ``None`` when no model is installed.

    When ``requested_model_id`` is given (Step 10: an explicit ``/model``
    choice), the check is scoped to that SPECIFIC model — a different
    already-installed model must not short-circuit the caller into skipping
    the requested download. Without it (legacy default-pick callers), any
    already-installed model counts, matching the original behaviour.
    """

    versions_status = (
        await c.service_release.get_versions_local_status_use_case.execute()
    )
    models_status = await c.service_release.get_models_local_status_use_case.execute()
    service_installed = any(
        item.installed for item in versions_status.versions.values()
    )
    if requested_model_id:
        item = models_status.models.get(requested_model_id)
        installed_model_id = (
            requested_model_id if item is not None and item.installed else None
        )
    else:
        installed_model_id = next(
            (mid for mid, item in models_status.models.items() if item.installed),
            None,
        )
    return service_installed, installed_model_id


async def _drain_download(
    iterator: Any, *, opts: RenderOptions | None = None, label: str = "下载中"
) -> Any:
    """Await a ``DownloadProgress`` stream to its terminal frame.

    Mirrors ``_render.RunFrameRenderer``'s TTY/non-TTY progress split
    (delivery plan Step 1): on a real terminal (``opts.color``) a Rich
    progress bar refreshes in place (``auto_refresh=False`` + explicit
    ``refresh()``, that renderer's proven pattern); otherwise progress
    degrades to periodic plain lines. Without ``opts`` at all (legacy
    silent drain), a real download's ONLY visible feedback in this entry
    point would be raw ``structlog``/``httpx`` log lines.
    """

    from qai.service_release.domain.value_objects import DownloadStatus

    if opts is None:
        final = None
        async for progress in iterator:
            final = progress
            if progress.status in (
                DownloadStatus.DONE,
                DownloadStatus.ERROR,
                DownloadStatus.CANCELLED,
            ):
                break
        return final

    console = _out_console(opts)
    terminal_statuses = (
        DownloadStatus.DONE,
        DownloadStatus.ERROR,
        DownloadStatus.CANCELLED,
    )
    final = None

    if not opts.color:
        last_pct = -1.0
        async for progress in iterator:
            final = progress
            pct = progress.percent
            if pct != last_pct:
                console.print(Text(f"… {label} {pct}%", style="dim"))
                last_pct = pct
            if progress.status in terminal_statuses:
                break
        return final

    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        TextColumn,
        TransferSpeedColumn,
    )

    bar = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        console=console,
        transient=False,
        auto_refresh=False,
    )
    bar.start()
    task_id = bar.add_task(label, total=None)
    try:
        async for progress in iterator:
            final = progress
            if progress.total_bytes:
                bar.update(
                    task_id,
                    total=progress.total_bytes,
                    completed=progress.downloaded_bytes,
                )
            bar.refresh()
            if progress.status in terminal_statuses:
                break
    finally:
        bar.stop()
    return final


async def _install_default_service(c: Any, opts: RenderOptions) -> None:
    """Download + install the recommended (else first) service version.

    Reuses ``service-release install service``'s own use cases
    (:class:`InstallServiceCommand` / ``install_service_use_case``) verbatim;
    the download step reuses ``StreamServiceDownloadUseCase`` (the same
    engine ``service-release aria2c`` drives) since ``install`` itself only
    accepts an already-downloaded archive.
    """

    from qai.service_release.application.use_cases import (
        InstallServiceCommand,
        StartServiceDownloadCommand,
    )
    from qai.service_release.domain.value_objects import DownloadStatus

    versions = await c.service_release.list_service_versions_use_case.execute()
    if not versions:
        raise RuntimeError("远程版本目录为空，无法安装 GenieAPIService")
    chosen = next((v for v in versions if v.is_recommended), versions[0])
    download_url = chosen.download_url or (
        chosen.packages[0].download_url if chosen.packages else ""
    )

    iterator = c.service_release.stream_service_download_use_case.execute(
        StartServiceDownloadCommand(
            version=chosen.version,
            download_url=download_url,
            checksum_sha256=chosen.checksum_sha256,
        )
    )
    progress = await _drain_download(
        iterator,
        opts=opts,
        label=f"下载 GenieAPIService {chosen.version}",
    )
    if progress is None or progress.status != DownloadStatus.DONE:
        error = progress.error if progress is not None else "未知错误"
        raise RuntimeError(f"下载 GenieAPIService {chosen.version} 失败: {error}")

    await c.service_release.install_service_use_case.execute(
        InstallServiceCommand(save_path=progress.save_path, version=chosen.version)
    )


async def _install_default_model(
    c: Any, opts: RenderOptions, *, model_id: str | None = None
) -> str:
    """Download + install a catalog model. Returns the installed ``model_id``.

    Reuses ``service-release install model``'s own use cases verbatim. When
    ``model_id`` is given (Step 10: an explicit ``/model`` choice), installs
    exactly that catalog entry (raises if it is not found); otherwise falls
    back to the first NPU entry (GenieAPIService's always-on backend, §5 of
    the project playbook), else the first entry.
    """

    from qai.service_release.application.use_cases import (
        InstallModelCommand,
        StartModelDownloadCommand,
    )
    from qai.service_release.domain.value_objects import (
        DownloadStatus,
        ModelHardware,
    )

    models = await c.service_release.list_catalog_models_use_case.execute()
    if not models:
        raise RuntimeError("远程模型目录为空，无法安装默认模型")
    if model_id:
        chosen = next((m for m in models if m.model_id == model_id), None)
        if chosen is None:
            raise RuntimeError(f"远程模型目录中未找到 model_id={model_id!r}")
    else:
        chosen = next(
            (m for m in models if m.hardware == ModelHardware.NPU), models[0]
        )
    download_url = chosen.download_url or (
        chosen.variants[0].download_url if chosen.variants else ""
    )
    checksum = chosen.checksum_sha256 or (
        chosen.variants[0].checksum_sha256 if chosen.variants else ""
    )

    iterator = c.service_release.stream_model_download_use_case.execute(
        StartModelDownloadCommand(
            model_id=chosen.model_id,
            download_url=download_url,
            checksum_sha256=checksum,
        )
    )
    progress = await _drain_download(
        iterator, opts=opts, label=f"下载模型 {chosen.model_id}"
    )
    if progress is None or progress.status != DownloadStatus.DONE:
        error = progress.error if progress is not None else "未知错误"
        raise RuntimeError(f"下载模型 {chosen.model_id} 失败: {error}")

    await c.service_release.install_model_use_case.execute(
        InstallModelCommand(save_path=progress.save_path, model_id=chosen.model_id)
    )
    return chosen.model_id


async def _register_local_provider(
    c: Any, *, model_id: str
) -> tuple[bool, str, str]:
    """Register + probe the local endpoint as the ``"local-genie"`` provider.

    Base-url resolution reuses
    :func:`apps.api._local_service_endpoint_bridge.make_local_service_endpoint_provider`
    verbatim (the same bridge ``apps/api/_chat_di.py`` wires as
    ``ProviderAwareModelResolver``'s ``local_endpoint_provider``): running
    daemon's real port, else ``forge.config``, else the typed
    ``Settings.model_runtime.default_port`` — never a hard-coded literal.
    Returns ``(ok, base_url, error)``.
    """

    from apps.api._local_service_endpoint_bridge import (
        make_local_service_endpoint_provider,
    )
    from qai.model_catalog.application.use_cases.probe_provider import (
        ProbeProviderCommand,
    )
    from qai.model_catalog.application.use_cases.update_provider_config import (
        UpdateProviderConfigCommand,
    )

    base_url = await make_local_service_endpoint_provider(c)()
    if not base_url:
        return False, "", "无法确定本地服务地址"

    await c.model_catalog.update_provider_config_use_case.execute(
        UpdateProviderConfigCommand(
            provider_id=_LOCAL_PROVIDER_ID,
            config={
                "base_url": base_url,
                "default_model": model_id,
                "models": [{"model_id": model_id, "name": model_id}],
            },
        )
    )
    probe = await c.model_catalog.probe_provider_use_case.execute(
        ProbeProviderCommand(provider_id=_LOCAL_PROVIDER_ID)
    )
    return probe.ok, base_url, probe.error


async def _activate_local_model(
    c: Any, opts: RenderOptions, *, model_id: str | None = None
) -> bool:
    """Best-effort local-first activation for a zero-provider chat session.

    Only ever runs on a TTY (defensive re-check; the caller is already
    TTY-gated per ``__main__.main``'s ``args.command is None`` branch — see
    module docstring). Returns ``True`` once the ``"local-genie"`` provider
    is registered and probed reachable; ``False`` on any failure (missing
    catalog data, download/install error, Ctrl+C, or a probe failure — e.g.
    a freshly installed daemon that is not actually running yet), in which
    case the caller falls back to :func:`_print_no_model_guidance`.

    ``model_id`` (Step 10) optionally pins activation to one SPECIFIC
    catalog model — e.g. the user's ``/model <choice>`` pick
    (:func:`_resolve_catalog_choice`) — instead of the fallback default pick
    :func:`_install_default_model` otherwise applies.
    """

    if not sys.stdin.isatty():
        return False

    console = _out_console(opts)
    service_installed, installed_model_id = await _local_service_and_model_present(
        c, requested_model_id=model_id
    )

    if not (service_installed and installed_model_id):
        model_desc = f"模型 {model_id}" if model_id else "默认版本/模型"
        console.print(
            Text(
                "尚未检测到本地 GenieAPIService 安装或可用模型；"
                f"即将下载并安装{model_desc}（真实网络下载，可能耗时较久）。",
                style="warning",
            )
        )
        try:
            if not service_installed:
                await _install_default_service(c, opts)
            if not installed_model_id:
                installed_model_id = await _install_default_model(
                    c, opts, model_id=model_id
                )
        except KeyboardInterrupt:
            warn = icon("warning", emoji=opts.emoji)
            prefix = f"{warn} " if warn else ""
            _err_console(opts).print(
                Text(f"{prefix}已中止本地模型激活。", style="warning")
            )
            return False
        except Exception as exc:  # noqa: BLE001 — activation must never crash
            cross = icon("error", emoji=opts.emoji)
            prefix = f"{cross} " if cross else ""
            _err_console(opts).print(
                Text(
                    f"{prefix}本地模型激活失败: {type(exc).__name__}: {exc}",
                    style="error",
                )
            )
            return False

    assert installed_model_id is not None  # narrowed above: pre-existing or just installed
    try:
        ok, base_url, error = await _register_local_provider(
            c, model_id=installed_model_id
        )
    except Exception as exc:  # noqa: BLE001 — activation must never crash
        cross = icon("error", emoji=opts.emoji)
        prefix = f"{cross} " if cross else ""
        _err_console(opts).print(
            Text(
                f"{prefix}注册本地 provider 失败: {type(exc).__name__}: {exc}",
                style="error",
            )
        )
        return False

    if not ok:
        cross = icon("error", emoji=opts.emoji)
        prefix = f"{cross} " if cross else ""
        _err_console(opts).print(
            Text(f"{prefix}本地服务探测失败: {error}", style="error")
        )
        return False

    success = icon("success", emoji=opts.emoji)
    prefix = f"{success} " if success else ""
    console.print(
        Text(f"{prefix}已激活本地模型 provider（{base_url}）。", style="success")
    )
    return True


async def _run_chat(args: argparse.Namespace) -> int:
    repo_root: Path | None = getattr(args, "repo_root", None)
    config_file: Path | None = getattr(args, "config_file", None)

    opts = RenderOptions.from_streams(sys.stdout, sys.stderr)
    log_level_for_session = "WARNING"

    async with repl_container(
        config_file=config_file, repo_root=repo_root, log_level=log_level_for_session
    ) as c:
        # ── Provider check is READ-ONLY here (Step 9 redesign — see module
        #    docstring): entering the session must never block on, or
        #    silently trigger, a real network download. It only decides
        #    whether the banner below shows a "no model configured" hint;
        #    local activation itself is user-triggered via `/model`.
        has_provider = await _precheck_cloud_provider(c)

        from qai.chat.application.use_cases.conversation_management import (
            CreateConversationInput,
        )
        from qai.chat.application.use_cases.tab_management import OpenTabInput

        conv = await c.chat.create_conversation_use_case.execute(
            CreateConversationInput(title="Chat")
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

        # ── Defensive permission bridge (chat path normally never fires; see
        #    ``_repl.PermissionBridge`` docstring). Subscribe so an event is
        #    queued not lost; we never block a turn waiting on it.
        perm_bridge = PermissionBridge()
        await perm_bridge.subscribe(c)

        interrupts = InterruptController()

        if is_real_tty():
            from apps.cli._tui.app import QaiReplApp  # noqa: PLC0415
            from apps.cli._tui.logging_bridge import make_warning_notifier  # noqa: PLC0415
            from qai.platform.logging import configure_logging  # noqa: PLC0415

            try:
                version = importlib.metadata.version("qaimodelbuilder")
            except importlib.metadata.PackageNotFoundError:
                version = "0.0.0.dev0"
            app = QaiReplApp(
                c=c,
                conversation_id=conversation_id,
                tab_id=tab_id,
                interrupts=interrupts,
                perm_bridge=perm_bridge,
                opts=opts,
                session_id=conv_id_str,
                version=version,
                has_provider=has_provider,
                dispatcher_factory=_make_dispatcher_factory(c=c, opts=opts),
                run_turn=_make_run_turn(c=c, opts=opts),
            )
            # ── Redirect structlog output at the session log file instead
            #    of the raw terminal stderr for the session's duration (root
            #    cause: the process-startup ``configure_logging`` call above
            #    captured a direct reference to ``sys.stderr`` AT THAT
            #    MOMENT, so later reassigning ``sys.stderr`` — as this
            #    session's ``TranscriptStream`` does — has no effect on
            #    already-configured structlog output). ``app`` must exist
            #    first so the warning-notifier processor has a live
            #    ``LogPanel`` to forward into.
            configure_logging(
                level=log_level_for_session,  # type: ignore[arg-type]
                fmt="console",
                stream=session_log.log_stream,
                extra_processors=[make_warning_notifier(app)],
            )
            try:
                await app.run_async()
                return 0
            finally:
                # Restore process-wide logging to the same settings the
                # process-startup ``configure_logging`` call (above, inside
                # ``repl_container``) used, for whatever runs after.
                configure_logging(
                    level=log_level_for_session,  # type: ignore[arg-type]
                    fmt="console",
                    stream=sys.stderr,
                )
                await perm_bridge.unsubscribe()
                cleanup_repl_session(session_log)

        _print_banner(conv_id_str, opts, has_provider=has_provider)

        dispatcher = _build_dispatcher(
            c=c,
            renderer=renderer,
            opts=opts,
        )

        try:
            return await _repl_loop(
                c=c,
                dispatcher=dispatcher,
                conversation_id=conversation_id,
                tab_id=tab_id,
                renderer=renderer,
                interrupts=interrupts,
                perm_bridge=perm_bridge,
                opts=opts,
            )
        finally:
            await perm_bridge.unsubscribe()
            cleanup_repl_session(session_log)


def _print_banner(
    conv_id: str, opts: RenderOptions, *, has_provider: bool = True
) -> None:
    from rich.panel import Panel

    console = _out_console(opts)
    agent = icon("agent", emoji=opts.emoji)
    prefix = f"{agent} " if agent else ""
    body = Text()
    body.append(f"{prefix}QAI Agent\n", style="agent")
    body.append(f"会话 id: {conv_id}\n", style="dim")
    body.append("直接输入自然语言即可对话；输入 ", style="dim")
    body.append("/", style="heading")
    body.append(" 弹出命令列表（", style="dim")
    body.append("/model", style="heading")
    body.append(" 查看/切换模型，", style="dim")
    body.append("/help", style="heading")
    body.append(" 查看全部）。", style="dim")
    if not has_provider:
        body.append("\n尚未配置任何模型 — 输入 ", style="warning")
        body.append("/model", style="heading")
        body.append(" 激活本地模型或查看配置方式。", style="warning")
    console.print(Panel(body, border_style="agent", expand=False))


# ---------------------------------------------------------------------------
# Streaming a turn
# ---------------------------------------------------------------------------


async def _stream_turn(
    *,
    c: Any,
    text: str,
    conversation_id: Any,
    tab_id: Any,
    renderer: StreamFrameRenderer,
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

    interrupts.reset()
    request = StreamChatInput(
        tab_id=tab_id,
        conversation_id=conversation_id,
        user_message=MessageContent(text=text),
        model_hint=_ID_HOLDER.get("model_hint"),
        extra=build_extra(),
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


#: Holder so ``/clear`` can rebind the frozen ConversationId / TabId VOs the
#: REPL loop reads each turn (set up in :func:`_run_chat` before the loop) —
#: mirrors ``commands/build.py``'s ``_ID_HOLDER`` pattern. ``model_catalog``
#: (Step 10) caches the last remote catalog listing :func:`/model` showed, so
#: a follow-up ``/model <n>`` can resolve a numeric index against it (see
#: :func:`_resolve_catalog_choice`).
_ID_HOLDER: dict[str, Any] = {}


def _format_size(size_bytes: int) -> str:
    """Human-readable size for the ``/model`` catalog table (MB/GB)."""

    if size_bytes <= 0:
        return "-"
    mb = size_bytes / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb:.0f} MB"


async def _show_installable_catalog(c: Any, opts: RenderOptions) -> None:
    """Zero-provider ``/model`` (no args): list downloadable catalog models.

    Step 10: replaces the old behaviour of silently activating one
    hard-coded default model — the remote catalog has many entries (e.g.
    several model families across NPU/GPU/CPU), all of which should be
    choosable. Caches the shown list in ``_ID_HOLDER["model_catalog"]`` so a
    follow-up ``/model <n>`` can resolve a numeric index against exactly
    this listing (:func:`_resolve_catalog_choice`); a literal ``model_id``
    always works too, regardless of this cache.
    """

    try:
        models = await c.service_release.list_catalog_models_use_case.execute()
    except Exception as exc:  # noqa: BLE001 — robust print, no trace
        cross = icon("error", emoji=opts.emoji)
        prefix = f"{cross} " if cross else ""
        _err_console(opts).print(
            Text(
                f"{prefix}读取远程模型目录失败: {type(exc).__name__}: {exc}",
                style="error",
            )
        )
        return

    if not models:
        _out_console(opts).print(Text("远程模型目录为空。", style="dim"))
        return

    _ID_HOLDER["model_catalog"] = list(models)

    from rich.table import Table

    table = Table(title="可下载的本地模型（尚未配置任何 provider）")
    table.add_column("#")
    table.add_column("model_id")
    table.add_column("name")
    table.add_column("family")
    table.add_column("parameter_size")
    table.add_column("hardware")
    table.add_column("size")
    for idx, model in enumerate(models, start=1):
        table.add_row(
            str(idx),
            model.model_id,
            model.name,
            model.family or "-",
            model.parameter_size or "-",
            model.hardware.value,
            _format_size(model.size_bytes),
        )
    _out_console(opts).print(table)
    _out_console(opts).print(
        Text(
            "用法: /model <序号或 model_id> 下载并激活该模型（若本地 "
            "GenieAPIService 尚未安装也会一并安装）。",
            style="dim",
        )
    )


def _resolve_catalog_choice(rest: str) -> str:
    """Resolve ``/model <arg>``'s argument to a concrete ``model_id``.

    ``<arg>`` is either a 1-based index into the catalog listing last shown
    by :func:`_show_installable_catalog` (``_ID_HOLDER["model_catalog"]``),
    or a literal ``model_id`` string — the latter always works, even if the
    catalog was never listed this session (e.g. a returning user who
    already knows the id they want).
    """

    cached = _ID_HOLDER.get("model_catalog") or []
    if rest.isdigit():
        idx = int(rest)
        if 1 <= idx <= len(cached):
            return cached[idx - 1].model_id
    return rest


def _render_provider_table(
    opts: RenderOptions, rows: list[dict], *, current: str | None
) -> None:
    """Render the "configured provider" table shared by both ``/model``
    branches that end up with a non-empty ``rows`` (no-args listing, and the
    refresh right after a successful Step 10 catalog-driven activation).
    """

    from rich.table import Table

    table = Table(title="已配置的模型 provider")
    table.add_column("provider")
    table.add_column("base_url")
    table.add_column("models")
    table.add_column("api_key")
    table.add_column("当前")
    for row in rows:
        provider_id = str(row.get("provider_id", ""))
        config = row.get("config") or {}
        base_url = str(config.get("base_url", ""))
        model_ids = [
            str(m.get("model_id", "")) for m in config.get("models", []) or []
        ]
        has_key = bool(config.get("has_api_key"))
        is_current = current == provider_id or current in model_ids
        table.add_row(
            provider_id,
            base_url,
            ", ".join(model_ids) or str(config.get("default_model", "")),
            "✓" if has_key else "-",
            "★" if is_current else "",
        )
    _out_console(opts).print(table)
    _out_console(opts).print(
        Text("用法: /model <provider_id 或 model_id> 切换当前回合使用的模型", style="dim")
    )


def _build_dispatcher(
    *,
    c: Any,
    renderer: StreamFrameRenderer,
    opts: RenderOptions,
    tui_app: Any | None = None,
) -> SlashDispatcher:
    """Register the minimal generic ``/`` command set for this entry point.

    Only the handlers that are genuinely generic (``/help``/``/history``/
    ``/clear``/``/show``/``/exit``) are wired here, mirroring
    ``commands/build.py``'s implementations of the same commands rather than
    reinventing them — no Model-Builder-specific commands.

    ``tui_app`` (the persistent ``apps.cli._tui.app.QaiReplApp`` session, set
    only on a real TTY) redirects ``/show`` to a modal screen instead of the
    ``prompt_toolkit`` pager, which would otherwise fight the already-running
    Textual app over the terminal.
    """

    d = SlashDispatcher(console=_out_console(opts))

    async def _help(_rest: str) -> bool:
        _out_console(opts).print(Text(d.render_help()))
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

    async def _clear(_rest: str) -> bool:
        from qai.chat.application.use_cases.conversation_management import (
            CreateConversationInput,
        )
        from qai.chat.application.use_cases.tab_management import OpenTabInput
        from qai.chat.domain.ids import ConversationId, TabId

        conv = await c.chat.create_conversation_use_case.execute(
            CreateConversationInput(title="Chat")
        )
        tab = await c.chat.open_tab_use_case.execute(
            OpenTabInput(conversation_id=conv.id.value)
        )
        new_conv = ConversationId.of(conv.id.value)
        new_tab = tab.id if not isinstance(tab.id, str) else TabId.of(tab.id)
        _ID_HOLDER["conversation_id"] = new_conv
        _ID_HOLDER["tab_id"] = new_tab
        ok = icon("success", emoji=opts.emoji)
        prefix = f"{ok} " if ok else ""
        _out_console(opts).print(
            Text(f"{prefix}已开启新会话: {conv.id.value}", style="success")
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
        if tui_app is not None:
            from apps.cli._tui.screens import FoldedContentScreen  # noqa: PLC0415

            await tui_app.push_screen(
                FoldedContentScreen(folded_text, title=f"/show {shown_idx}")
            )
        else:
            await show_pager(folded_text, title=f"/show {shown_idx}")
        return True

    async def _model(rest: str) -> bool:
        rest = rest.strip()
        try:
            rows = await c.model_catalog.list_provider_configs_use_case.execute()
        except Exception as exc:  # noqa: BLE001 — robust print, no trace
            cross = icon("error", emoji=opts.emoji)
            prefix = f"{cross} " if cross else ""
            _err_console(opts).print(
                Text(
                    f"{prefix}读取 provider 列表失败: {type(exc).__name__}: {exc}",
                    style="error",
                )
            )
            return True

        if not rest:
            if not rows:
                # Step 10: list the remote catalog for the user to CHOOSE
                # from, instead of the old behaviour of silently activating
                # one hard-coded default model (see module docstring
                # "Model selection").
                await _show_installable_catalog(c, opts)
                return True
            _render_provider_table(opts, rows, current=_ID_HOLDER.get("model_hint"))
            return True

        if not rows:
            # Zero providers + an argument → resolve it against the last
            # shown catalog (index or literal model_id) and drive
            # local-first activation for THAT specific model (Step 10).
            requested_model_id = _resolve_catalog_choice(rest)
            activated = await _activate_local_model(
                c, opts, model_id=requested_model_id
            )
            if not activated:
                _print_no_model_guidance(opts)
                return True
            try:
                rows = await c.model_catalog.list_provider_configs_use_case.execute()
            except Exception:  # noqa: BLE001 — activation already reported success
                return True
            if rows:
                _render_provider_table(
                    opts, rows, current=_ID_HOLDER.get("model_hint")
                )
            return True

        _ID_HOLDER["model_hint"] = rest
        ok = icon("success", emoji=opts.emoji)
        prefix = f"{ok} " if ok else ""
        _out_console(opts).print(
            Text(f"{prefix}后续回合将使用模型: {rest}", style="success")
        )
        return True

    async def _exit(_rest: str) -> bool:
        return False  # request REPL exit

    d.register("help", "显示全部命令", _help, aliases=("?",))
    d.register("model", "[<序号/provider/model id>] 查看/切换模型；无 provider 时列出可下载模型", _model)
    d.register("history", "打印会话历史消息", _history)
    d.register("show", "[<n>] 全屏查看折叠的工具结果（不带参数查看最近一次）", _show)
    d.register("clear", "开启新会话", _clear)
    d.register("exit", "退出会话", _exit, aliases=("quit",))
    return d


# ---------------------------------------------------------------------------
# Persistent TUI wiring (apps.cli._tui.app.QaiReplApp closures)
# ---------------------------------------------------------------------------


def _make_dispatcher_factory(
    *, c: Any, opts: RenderOptions
) -> Callable[[Any], SlashDispatcher]:
    """Build the ``QaiReplApp`` ``dispatcher_factory`` closure for this entry point.

    Seeds ``_ID_HOLDER`` the same way the non-TTY ``_repl_loop`` does before
    its first read, then builds the dispatcher wired with ``tui_app``. Called
    once, inside ``QaiReplApp.__init__`` (after ``renderer`` exists).
    """

    def factory(app: Any) -> SlashDispatcher:
        _ID_HOLDER["conversation_id"] = app._conversation_id
        _ID_HOLDER["tab_id"] = app._tab_id
        _ID_HOLDER["model_hint"] = None
        _ID_HOLDER["model_catalog"] = None
        return _build_dispatcher(c=c, renderer=app.renderer, opts=opts, tui_app=app)

    return factory


def _make_run_turn(
    *, c: Any, opts: RenderOptions
) -> Callable[[Any, str], Awaitable[None]]:
    """Build the ``QaiReplApp`` ``run_turn`` closure for this entry point.

    Wraps ``_run_turn_with_interrupt`` + the post-turn permission resolve —
    the same pair ``_repl_loop`` calls per natural-language line.
    """

    async def run_turn(app: Any, line: str) -> None:
        await _run_turn_with_interrupt(
            c=c,
            text=line,
            renderer=app.renderer,
            interrupts=app._interrupts,
            opts=opts,
        )
        if app._perm_bridge is not None:
            await _resolve_permissions(c, app._perm_bridge, app._allow_set)

    return run_turn


# ---------------------------------------------------------------------------
# REPL loop
# ---------------------------------------------------------------------------


async def _repl_loop(
    *,
    c: Any,
    dispatcher: SlashDispatcher,
    conversation_id: Any,
    tab_id: Any,
    renderer: StreamFrameRenderer,
    interrupts: InterruptController,
    perm_bridge: Any = None,
    opts: RenderOptions,
) -> int:
    """Drive the interactive loop until ``/exit`` / EOF / double Ctrl+C.

    Same interrupt/permission discipline as ``commands/build.py``'s
    ``_repl_loop``: a first Ctrl+C cancels the current turn, a second within
    the interrupt window exits; Ctrl+D (EOFError) exits cleanly.

    Only used by the non-real-TTY fallback path — the real-TTY session is
    now driven entirely by the persistent ``apps.cli._tui.app.QaiReplApp``
    (see ``_run_chat``).
    """

    _ID_HOLDER["conversation_id"] = conversation_id
    _ID_HOLDER["tab_id"] = tab_id
    _ID_HOLDER["model_hint"] = None
    _ID_HOLDER["model_catalog"] = None

    from apps.cli._repl import build_slash_completer, is_slash_command

    completer = build_slash_completer(dispatcher)
    allow_set: set[str] = set()

    while True:
        try:
            line = await async_read_line("chat › ", completer=completer)
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

        if is_slash_command(line):
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
            renderer=renderer,
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
    renderer: StreamFrameRenderer,
    interrupts: InterruptController,
    opts: RenderOptions,
) -> None:
    """Run one turn; convert a Ctrl+C during the turn into a turn cancel."""

    turn = asyncio.ensure_future(
        _stream_turn(
            c=c,
            text=text,
            conversation_id=_ID_HOLDER["conversation_id"],
            tab_id=_ID_HOLDER["tab_id"],
            renderer=renderer,
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


async def _resolve_permissions(c: Any, perm_bridge: Any, allow_set: set[str]) -> None:
    """Drain + interactively resolve queued permission requests (D5).

    Best-effort: a resolver error never breaks the REPL.
    """

    from apps.cli._repl import resolve_pending_permissions

    with contextlib.suppress(Exception):
        await resolve_pending_permissions(
            c, perm_bridge, allow_set=allow_set
        )
