# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""ASK request_id → conversation-id coordination registry (apps-layer).

SEC true-scoping (PART D). The security ``PermissionRequest`` entity has no
slot for the originating conversation id and is field-locked (must not be
modified). But when a user approves an ASK with scope=session, the resulting
PathGrant MUST be keyed to the conversation that triggered the ASK.

The conversation id is available at ASK-creation time (the per-request
contextvar ``get_conversation_scope()`` bound at the ai_coding ToolPort
boundary). Since it cannot live on the request entity, the FileGuard bridge
stashes it here keyed by the minted ``request_id`` when it creates the ASK,
and the approve HTTP route reads it back to pass as ``scope_conversation_id``
to ``ApprovePermissionUseCase``.

This is a small apps-layer coordination primitive — NOT business logic — so
it lives in the apps layer (the one layer allowed to touch multiple bounded
contexts) and is *injected* into the FileGuard bridge rather than reached as a
hidden module-global. A process-wide default singleton
(:data:`ASK_CONVERSATION_REGISTRY`) is provided for the composition root and
the approve route (which both need the same instance without threading it
through every call site).

A tiny lock guards concurrent access: the asyncio loop is single-threaded, but
the native-hook ASK bridge runs on its OWN loop thread, so the map can be
written from two threads. Entries are popped on read (single-use) and
opportunistically bounded so a never-approved ASK cannot leak forever.
"""

from __future__ import annotations

import threading

__all__ = [
    "ASK_CONVERSATION_REGISTRY",
    "AskConversationRegistry",
]

_ASK_CONVERSATION_MAX = 512


class AskConversationRegistry:
    """Thread-safe, single-use ``request_id`` → conversation id map."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._map: dict[str, str] = {}

    def remember(self, request_id: str, conversation_id: str) -> None:
        if not request_id or not conversation_id:
            return
        with self._lock:
            # Opportunistic bound: drop the oldest half if the map grows
            # unbounded (never-approved ASKs). dict preserves insertion order.
            if len(self._map) >= _ASK_CONVERSATION_MAX:
                for key in list(self._map)[: _ASK_CONVERSATION_MAX // 2]:
                    self._map.pop(key, None)
            self._map[request_id] = conversation_id

    def take(self, request_id: str) -> str:
        """Pop + return the conversation id for ``request_id`` ("" if none)."""
        if not request_id:
            return ""
        with self._lock:
            return self._map.pop(request_id, "")


#: Process-wide default registry shared by the FileGuard bridge (writer) and
#: the approve route (reader). Module-level so both apps-layer sites reach the
#: same instance without threading it through DI; the FileGuard bridge injects
#: this instance by default (its constructor accepts an override for tests).
ASK_CONVERSATION_REGISTRY = AskConversationRegistry()
