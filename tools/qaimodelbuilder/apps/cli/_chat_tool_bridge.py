"""``apps.cli._chat_tool_bridge`` — CLI-exclusive read-only chat tools (Step 5).

Registers 4 read-only tools onto a :class:`~apps.api.di.Container`'s chat
tool registry (``container.chat.tools``), wrapping the SAME use-case calls
the ``qai skill list`` / ``qai pack list`` / ``qai service status`` /
``qai run list`` argparse handlers already make — no shell-out, no CLI
arg re-parsing.

This module is CLI-exclusive: it is wired from :func:`apps.cli._runtime.
cli_container` and must NOT be imported from ``apps/api/_chat_di.py`` (the
WebUI/API tool set stays unaffected — these tools would otherwise leak into
the API's chat surface).

Mirrors the batch-registration pattern of ``apps.api._chat_tool_bridge``,
but there each handler wraps an ``qai.ai_coding`` handler; here each
handler wraps a ``Container`` use-case execution directly via closure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container
    from qai.chat.application.ports import ToolInvocationRequest

__all__ = [
    "register_cli_tools",
    "QAI_SKILL_LIST_SCHEMA",
    "QAI_PACK_LIST_SCHEMA",
    "QAI_SERVICE_STATUS_SCHEMA",
    "QAI_RUN_LIST_SCHEMA",
]


QAI_SKILL_LIST_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "qai_skill_list",
        "description": (
            "List every skill known to this CLI installation (live FS scan "
            "merged with per-skill mode overrides) — the same data "
            "'qai skill list' prints. Read-only; takes no arguments."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

QAI_PACK_LIST_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "qai_pack_list",
        "description": (
            "List every App Builder Pack registered in this installation "
            "(built-in + user-imported, including disabled ones) — the "
            "same data 'qai pack list' prints. Read-only; takes no "
            "arguments."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

QAI_SERVICE_STATUS_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "qai_service_status",
        "description": (
            "Report the current GenieAPIService daemon status (running / "
            "stopped, loaded model, host / port) — the same data "
            "'qai service status' prints. Read-only; takes no arguments."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

QAI_RUN_LIST_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "qai_run_list",
        "description": (
            "List recent App Builder inference runs across all models, "
            "newest first — the same data 'qai run list' prints. "
            "Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": (
                        "Maximum number of runs to return. Defaults to 20."
                    ),
                },
            },
        },
    },
}


def register_cli_tools(container: "Container") -> tuple[str, ...]:
    """Register the 4 read-only CLI tools onto ``container.chat.tools``.

    Called from :func:`apps.cli._runtime.cli_container` after
    :meth:`Container.build` succeeds, so any CLI-side chat agent sharing
    this container (``qai build``'s agent, the default chat entry point)
    can call them. Each handler closes over ``container`` — the same
    pattern ``apps.api._chat_di.build_chat_services`` uses for its
    ``skill`` / ``list_subagents`` handlers (closure over the DI
    container, resolved at registration time).

    Returns the 4 registered tool names (for tests / diagnostics).
    """

    tools = container.chat.tools

    async def _qai_skill_list(request: "ToolInvocationRequest") -> Any:
        return await container.user_prefs.list_skills_use_case.execute()

    async def _qai_pack_list(request: "ToolInvocationRequest") -> Any:
        from apps.cli.commands.pack import (  # noqa: PLC0415
            _model_to_dict,
            _seed_factory_packs_if_empty,
        )

        await _seed_factory_packs_if_empty(container)
        models = await container.app_builder.list_app_models_use_case.execute(
            include_disabled=True,
        )
        return {"items": [_model_to_dict(m) for m in models]}

    async def _qai_service_status(request: "ToolInvocationRequest") -> Any:
        return await container.model_runtime.get_status_use_case.execute()

    async def _qai_run_list(request: "ToolInvocationRequest") -> Any:
        from apps.cli.commands.run import _run_to_dict  # noqa: PLC0415

        limit = request.arguments.get("limit")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            limit = 20
        uc = container.app_builder.list_runs_use_case
        if uc is None:
            return {"items": []}
        runs = await uc.execute(limit=limit, offset=0)
        return {"items": [_run_to_dict(r) for r in runs]}

    tools.register("qai_skill_list", _qai_skill_list, schema=QAI_SKILL_LIST_SCHEMA)
    tools.register("qai_pack_list", _qai_pack_list, schema=QAI_PACK_LIST_SCHEMA)
    tools.register(
        "qai_service_status", _qai_service_status, schema=QAI_SERVICE_STATUS_SCHEMA
    )
    tools.register("qai_run_list", _qai_run_list, schema=QAI_RUN_LIST_SCHEMA)

    return (
        "qai_skill_list",
        "qai_pack_list",
        "qai_service_status",
        "qai_run_list",
    )
