# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""DI wiring for the platform ``computer`` (desktop-control) sub-module.

A platform shared-kernel module (mirrors ``_background_process_di.py``).
Builds a per-conversation-owner controller factory backed by
:class:`~qai.platform.computer.supervisor.ComputerSupervisor`, each worker
subprocess assigned to a shared Win32 Job Object so a hard parent death
reaps every worker at OS level.

The tool is OFF by default; ``wire_computer_tool_into_chat`` only registers
the LLM tool when ``settings.computer.enabled`` is True.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from qai.platform.computer import (
    ComputerSupervisor,
    register_computer_controller,
)
from qai.platform.computer.ports import DesktopControllerPort
from qai.platform.computer.types import SessionOptions
from qai.platform.logging import get_logger
from qai.platform.process import ProcessKillGroup

if TYPE_CHECKING:  # pragma: no cover
    from ._chat_di import ChatServices
    from .di import Container

__all__ = [
    "ComputerServices",
    "build_computer_services",
    "wire_computer_tool_into_chat",
]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ComputerServices:
    """Namespace bundling the desktop controller factory + its kill group.

    Frozen so the wired graph cannot be mutated post-build. The kill group
    is held here so its lifetime is bound to the Container (avoiding GC of
    the Win32 Job Object handle while workers are still assigned to it).
    ``controllers`` caches one supervisor per conversation owner id.
    """

    kill_group: ProcessKillGroup
    options: SessionOptions
    controllers: dict[str, DesktopControllerPort] = field(default_factory=dict)

    def controller_for_owner(self, owner_id: str) -> DesktopControllerPort:
        """Return (lazily build + owner-register) the owner's controller."""
        existing = self.controllers.get(owner_id)
        if existing is not None:
            return existing
        controller = ComputerSupervisor(
            options=self.options,
            kill_group=self.kill_group,
        )
        self.controllers[owner_id] = controller
        register_computer_controller(owner_id, controller)
        return controller


def build_computer_services(container: "Container") -> ComputerServices:
    """Wire ``container.computer``.

    Reads the 5 ``computer.*`` settings into a :class:`SessionOptions`.
    Workers are lazy — no subprocess is spawned here.
    """
    cfg = container.settings.computer
    options = SessionOptions(
        backend=cfg.backend,
        display=cfg.display,
        max_width=cfg.max_width,
        max_height=cfg.max_height,
    )
    return ComputerServices(
        kill_group=ProcessKillGroup(),
        options=options,
    )


def wire_computer_tool_into_chat(
    *,
    chat: "ChatServices",
    container: "Container",
) -> tuple[str, ...]:
    """Post-build hook: register the ``computer`` LLM tool on chat.

    Registration is UNCONDITIONAL: the tool is always placed on the chat
    registry so the ``computer.enabled`` toggle can take effect per-turn
    without a restart. The actual gate lives in the streaming use case,
    which drops ``computer`` from a turn's advertised tool set whenever
    the runtime flag (forge_config override > ``settings.computer.enabled``)
    is off — mirroring how the ``chat_hooks_enabled`` toggle re-reads each
    turn. Best-effort: a build/registration failure degrades to no tool;
    chat startup is never blocked.
    """

    from ._chat_computer_tool_bridge import register_computer_tool_into_chat

    _file_guard: object | None = None
    try:
        from ._file_guard_bridge import build_file_guard

        _file_guard = build_file_guard(container)
    except Exception:  # noqa: BLE001 — never block tool registration
        _file_guard = None

    save_screenshot = _build_screenshot_saver(container)

    return register_computer_tool_into_chat(
        tools=chat.tools,
        controller_for_owner=container.computer.controller_for_owner,
        save_screenshot=save_screenshot,
        file_guard=_file_guard,
    )


def _build_screenshot_saver(container: "Container") -> Any:
    """Build the screenshot-saver closure over the chat image-upload store.

    Returns ``Callable[[b64, conv_id, msg_id], Awaitable[str|None]]`` that
    persists the PNG and yields its ``/api/images/files/...`` URL so the
    chat streaming loop can inject it as a vision block. ``None`` when the
    store is unavailable (the handler then returns a text-only envelope).
    """
    try:
        store = container.chat.image_upload_store
    except Exception:  # noqa: BLE001
        return None
    if store is None:
        return None

    from qai.chat.application.ports import ImageUploadRequest

    import itertools
    import time

    # Per-capture uniqueness counter. The image store derives the on-disk
    # filename from ``conversation_id`` + ``message_id`` and SKIPS writing when
    # that file already exists (correct for a real chat message's images, which
    # are written once). But every ``computer`` screenshot in a conversation
    # arrives with the SAME ``message_id`` (the tab id / "screenshot"), so a
    # fixed id collided: the 2nd+ capture matched the existing file, was NOT
    # written, and returned the FIRST capture's URL — the reported "每次都显示
    # 第一次截图" bug (both the UI and the model saw a stale first frame even
    # though native captured a fresh, differently-sized PNG each time). Make the
    # id unique per capture so each screenshot is its own file + URL.
    _seq = itertools.count()

    async def _save(b64: str, conversation_id: str, message_id: str) -> str | None:
        base = message_id or "screenshot"
        unique_id = f"{base}-{time.strftime('%H%M%S')}-{next(_seq)}"
        try:
            result = await store.save_base64(
                ImageUploadRequest(
                    conversation_id=conversation_id or "computer",
                    message_id=unique_id,
                    base64_data=b64,
                    mime_type="image/png",
                )
            )
            url = getattr(result, "url", None)
            return url if isinstance(url, str) and url else None
        except Exception:  # noqa: BLE001 — degrade to text-only
            return None

    return _save
