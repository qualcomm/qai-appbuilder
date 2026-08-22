# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""LLM-backed :class:`SmartApprovalPort` adapter (PR-092 §2.1 C-7 / §17.5 #8).

Reads ``Settings.security.smart_approval_llm_endpoint`` /
``smart_approval_llm_model`` and asks the configured chat-completion
endpoint to classify a permission request as ``APPROVE`` / ``DENY``
(``REJECT``) / ``UNDECIDED``. Mirrors the legacy
``backend/security/smart_approval.py:14-117`` ``evaluate_risk`` helper
with three differences:

* The legacy helper used ``low_risk`` / ``high_risk`` / ``uncertain``;
  this adapter speaks the
  :class:`qai.security.application.ports.SmartApprovalDecision`
  taxonomy directly (``APPROVE`` / ``REJECT`` / ``UNDECIDED``).
* All HTTP / JSON / timeout failures collapse to ``UNDECIDED`` so a
  flapping LLM endpoint never auto-denies real user requests.
* The adapter is async-native and uses ``httpx.AsyncClient`` rather
  than the legacy ``httpx`` blocking helper wrapped in ``asyncio.to_thread``.

The adapter is wired in :func:`apps.api._security_di.build_security_services`
**after** the existing :class:`SettingsSmartApprovalAdapter`; the LLM
adapter takes precedence whenever both ``smart_approval_llm_endpoint``
and ``smart_approval_llm_model`` are configured.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING

import httpx

from qai.platform.qgenie_user_agent import apply_qgenie_user_agent
from qai.security.application.ports import SmartApprovalDecision
from qai.security.domain.value_objects import AceMask, Resource, Subject

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.config.settings import SecuritySettings

__all__ = ["SmartApprovalLLMAdapter"]


_LOGGER = logging.getLogger("qai.security.smart_approval_llm")

#: L-Sec-5 (fileguard-audit-2026-07-26) — default TTL for cached
#: APPROVE / REJECT verdicts. 60s matches the typical per-conversation
#: burst window: a subject repeatedly asking to touch the same resource
#: within a minute reuses the last decision; anything older is re-asked
#: so a policy tweak or context shift is observed promptly. UNDECIDED is
#: intentionally NEVER cached — the LLM signalled it needs a human, and
#: a follow-up call after operator context is added must retry.
_DEFAULT_CACHE_TTL_SECONDS = 60.0
_DEFAULT_CACHE_MAXSIZE = 1024

_PROMPT = """You are a security evaluator for an AI Agent system.
Evaluate the risk level of the following operation:

- Subject kind: {subject_kind}
- Subject identifier: {subject_identifier}
- Resource kind: {resource_kind}
- Resource identifier: {resource_identifier}
- Requested permissions: read={read} write={write} execute={execute} delete={delete}

Risk classification rules:
- "approve": Read-only operations on non-sensitive paths, listing
  directories, grep/search within allowed project paths, running
  analysis scripts in project dir.
- "reject": Deleting files, formatting disks, writing to system
  directories (C:\\Windows, C:\\Program Files, /etc/, /usr/), reading
  credentials (.env, .ssh/), network uploads with sensitive data,
  installing packages globally, stopping system services.
- "undecided": Operations that could be either safe or dangerous
  depending on context (writing to an unknown path, running an
  unfamiliar script).

Respond with EXACTLY one word: approve, reject, or undecided"""


class SmartApprovalLLMAdapter:
    """LLM-backed :class:`SmartApprovalPort` implementation."""

    __slots__ = (
        "_endpoint",
        "_model",
        "_api_key",
        "_timeout",
        "_ssl_verify_provider",
        "_cache",
        "_cache_ttl",
        "_cache_maxsize",
        "_cache_locks",
        "_clock",
    )

    def __init__(
        self,
        *,
        settings: "SecuritySettings",
        api_key: str = "",
        timeout: float = 5.0,
        ssl_verify_provider: "Callable[[], bool] | None" = None,
        cache_ttl_seconds: float | None = None,
        cache_maxsize: int = _DEFAULT_CACHE_MAXSIZE,
        clock: "Callable[[], float] | None" = None,
    ) -> None:
        self._endpoint: str = (
            settings.smart_approval_llm_endpoint or ""
        ).rstrip("/")
        self._model: str = settings.smart_approval_llm_model or ""
        self._api_key = api_key
        self._timeout = float(timeout)
        # 缺口 fix — previously hardcoded ``verify=False``. Route through the
        # live Settings.ssl_verify provider so the global toggle governs this
        # classifier call; read at request time (hot-applies). When unset the
        # prior ``verify=False`` behaviour is preserved.
        self._ssl_verify_provider = ssl_verify_provider
        # L-Sec-5 (fileguard-audit-2026-07-26) — bounded TTL cache of
        # APPROVE / REJECT verdicts. Prevents an unbounded bill amplification
        # when the same subject asks about the same resource in a tight loop
        # (e.g. a script iterating a directory). UNDECIDED is never cached
        # — it's the "need human" signal and the operator may have added
        # context by the next call. Keyed on (subject.kind, subject.id,
        # resource.kind, resource.id, mask_tuple); ``OrderedDict`` gives a
        # cheap LRU (move-to-end on hit, popitem(last=False) on overflow).
        ttl = float(
            cache_ttl_seconds
            if cache_ttl_seconds is not None
            else getattr(settings, "smart_approval_llm_cache_ttl_seconds", None)
            or _DEFAULT_CACHE_TTL_SECONDS
        )
        self._cache_ttl = max(0.0, ttl)
        self._cache_maxsize = max(0, int(cache_maxsize))
        self._cache: "OrderedDict[tuple, tuple[float, SmartApprovalDecision]]" = (
            OrderedDict()
        )
        # L-Sec-5 review fix (2026-07-28): per-event-loop coalescing lock.
        # This adapter is a process-wide singleton reached from TWO loops —
        # the main uvicorn loop (ai_coding PermissionBridge / _file_guard_
        # bridge) AND the dedicated native-guard-ask-loop thread created in
        # _native_hook_bridge (its own asyncio loop driven by
        # run_coroutine_threadsafe). A single asyncio.Lock binds to the loop
        # that first touched it; awaiting it from the other loop raises
        # RuntimeError ("bound to a different event loop"), which would
        # propagate out of evaluate() and silently disable the classifier on
        # that loop. Key the lock by running-loop id so each loop gets its
        # own; the coalescing guarantee (one in-flight httpx per key) still
        # holds within each loop, which is where concurrency actually races.
        self._cache_locks: "dict[int, asyncio.Lock]" = {}
        self._clock = clock if clock is not None else time.monotonic

    @property
    def is_configured(self) -> bool:
        """``True`` when both endpoint and model are populated."""

        return bool(self._endpoint) and bool(self._model)

    def _cache_key(
        self,
        subject: Subject,
        resource: Resource,
        requested_mask: AceMask,
    ) -> tuple:
        return (
            subject.kind,
            subject.identifier,
            resource.kind,
            resource.identifier,
            requested_mask.read,
            requested_mask.write,
            requested_mask.execute,
            requested_mask.delete,
        )

    def _cache_get(self, key: tuple) -> "SmartApprovalDecision | None":
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, decision = entry
        if self._clock() >= expires_at:
            # Expired — evict eagerly so a stale hit never wins a race.
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return decision

    def _loop_lock(self) -> "asyncio.Lock":
        """Return the coalescing lock bound to the CURRENT running loop.

        Created lazily per loop id so an adapter shared across the main
        uvicorn loop and the native-guard-ask-loop thread never awaits a
        lock bound to a foreign loop (RuntimeError). Bounded implicitly by
        the process's loop count (2 in practice).
        """
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
        lock = self._cache_locks.get(loop_id)
        if lock is None:
            lock = asyncio.Lock()
            self._cache_locks[loop_id] = lock
        return lock

    def _cache_put(
        self, key: tuple, decision: "SmartApprovalDecision"
    ) -> None:
        if self._cache_maxsize == 0 or self._cache_ttl == 0.0:
            return
        # Only cache decided verdicts — UNDECIDED means "ask a human", so
        # caching it would defeat the intent (a follow-up call after new
        # context needs to actually re-ask the LLM).
        if decision is SmartApprovalDecision.UNDECIDED:
            return
        self._cache[key] = (self._clock() + self._cache_ttl, decision)
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_maxsize:
            self._cache.popitem(last=False)

    async def evaluate(
        self,
        *,
        subject: Subject,
        resource: Resource,
        requested_mask: AceMask,
    ) -> SmartApprovalDecision:
        if not self.is_configured:
            return SmartApprovalDecision.UNDECIDED

        key = self._cache_key(subject, resource, requested_mask)
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        # Serialise the miss path so two concurrent evaluate() calls for the
        # same key coalesce into ONE httpx round-trip. Re-check the cache
        # inside the lock so the second caller reuses the first caller's
        # freshly-cached decision.
        async with self._loop_lock():
            cached = self._cache_get(key)
            if cached is not None:
                return cached

            prompt = _PROMPT.format(
                subject_kind=subject.kind,
                subject_identifier=subject.identifier,
                resource_kind=resource.kind,
                resource_identifier=resource.identifier,
                read=requested_mask.read,
                write=requested_mask.write,
                execute=requested_mask.execute,
                delete=requested_mask.delete,
            )
            payload = {
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 10,
                "temperature": 0.0,
            }
            headers = {"Content-Type": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            # QGenie bills a request to one of two independent daily allowances
            # from this header alone, so without it an approval check always
            # lands in ``Api`` even while the user has switched to the other
            # side. This adapter is configured with a fixed endpoint and has no
            # per-model catalog entry to read, so it follows the default bucket
            # (the same one the streaming path uses absent a stored preference).
            # No-op for every non-QGenie endpoint. Ten output tokens per call,
            # so the point is keeping the accounting whole, not the volume.
            apply_qgenie_user_agent(headers, self._endpoint, None)

            url = f"{self._endpoint}/chat/completions"
            # Live read of the global SSL toggle (prior default preserved:
            # no provider → verify=False); a runtime toggle hot-applies per
            # call.
            verify = (
                self._ssl_verify_provider()
                if self._ssl_verify_provider is not None
                else False
            )
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout, verify=verify
                ) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                content = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                    .lower()
                )
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                _LOGGER.info(
                    "smart_approval_llm: request failed (%s); returning UNDECIDED",
                    exc,
                )
                return SmartApprovalDecision.UNDECIDED
            except Exception as exc:  # pragma: no cover - hardening
                _LOGGER.warning(
                    "smart_approval_llm: unexpected error (%s); returning UNDECIDED",
                    exc,
                )
                return SmartApprovalDecision.UNDECIDED

            if "approve" in content:
                decision = SmartApprovalDecision.APPROVE
            elif "reject" in content or "deny" in content or "high_risk" in content:
                decision = SmartApprovalDecision.REJECT
            else:
                decision = SmartApprovalDecision.UNDECIDED
            self._cache_put(key, decision)
            return decision
