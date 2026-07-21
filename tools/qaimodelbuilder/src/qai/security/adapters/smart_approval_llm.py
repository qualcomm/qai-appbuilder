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

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

import httpx

from qai.security.application.ports import SmartApprovalDecision
from qai.security.domain.value_objects import AceMask, Resource, Subject

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.config.settings import SecuritySettings

__all__ = ["SmartApprovalLLMAdapter"]


_LOGGER = logging.getLogger("qai.security.smart_approval_llm")


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
    )

    def __init__(
        self,
        *,
        settings: "SecuritySettings",
        api_key: str = "",
        timeout: float = 5.0,
        ssl_verify_provider: "Callable[[], bool] | None" = None,
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

    @property
    def is_configured(self) -> bool:
        """``True`` when both endpoint and model are populated."""

        return bool(self._endpoint) and bool(self._model)

    async def evaluate(
        self,
        *,
        subject: Subject,
        resource: Resource,
        requested_mask: AceMask,
    ) -> SmartApprovalDecision:
        if not self.is_configured:
            return SmartApprovalDecision.UNDECIDED

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

        url = f"{self._endpoint}/chat/completions"
        # Live read of the global SSL toggle (prior default preserved: no
        # provider → verify=False); a runtime toggle hot-applies per call.
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
            return SmartApprovalDecision.APPROVE
        if "reject" in content or "deny" in content or "high_risk" in content:
            return SmartApprovalDecision.REJECT
        return SmartApprovalDecision.UNDECIDED
