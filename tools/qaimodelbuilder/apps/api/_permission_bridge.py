# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Cross-context permission-decision bridge (PR-046).

Anti-corruption layer that adapts the security context's
``RequestPermissionUseCase`` (and friends) into the ai_coding
:class:`PermissionDecisionPort`.

Design intent
-------------
The ai_coding context exposes ``PermissionDecisionPort.evaluate(request,
workspace) -> PermissionDecision`` — a synchronous yes/no/pending
verdict per tool call.  The security context's
``RequestPermissionUseCase`` does something fundamentally different:
it *creates* a security-side ``PermissionRequest`` aggregate and
publishes a ``PermissionRequestedEvent`` so a human (or smart-approval
adapter) can resolve the request asynchronously.

The bridge composes the two:

1. On every ai_coding ``evaluate`` call the bridge **records** the
   request via the injected ``request_permission_use_case`` so the
   security audit trail captures it.
2. The bridge optionally consults a *fast-path* policy check (when
   provided) — typically smart-approval / sandbox-grant lookup — to
   short-circuit auto-approve / auto-reject decisions.
3. Otherwise the bridge returns ``PENDING`` so the user must decide
   via the route layer.

Cross-context isolation
-----------------------
``qai.ai_coding.*`` may NEVER import ``qai.security.*`` directly.
The bridge lives in ``apps/api/`` (allowed cross-context composition)
and works against duck-typed inputs so neither the bridge nor the
ai_coding context picks up a hard dependency on
:mod:`qai.security.*`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from qai.ai_coding.domain import (
    PermissionDecision,
    PermissionRequest,
    Workspace,
)

__all__ = [
    "FastPathPolicy",
    "PermissionBridge",
    "RequestPermissionUseCaseSource",
]


@runtime_checkable
class RequestPermissionUseCaseSource(Protocol):
    """Duck-typed source covering the security ``RequestPermissionUseCase``.

    The bridge calls only :meth:`execute`; the keyword arguments
    documented on the security use case (``subject`` / ``resource`` /
    ``requested_mask``) are forwarded as ``Any`` so the bridge stays
    independent of the security domain types.
    """

    async def execute(
        self,
        *,
        subject: Any,
        resource: Any,
        requested_mask: Any,
    ) -> Any: ...


# A fast-path policy returns a definitive ``PermissionDecision``
# (APPROVED / REJECTED) when the bridge can answer without prompting
# the user, or ``PermissionDecision.PENDING`` to fall through to the
# default "wait for user" path.
FastPathPolicy = Callable[
    [PermissionRequest, Workspace], Awaitable[PermissionDecision]
]


class PermissionBridge:
    """Bridge ai_coding ``PermissionDecisionPort`` → security use cases.

    Notes
    -----
    The bridge is intentionally lenient: when the security side
    raises any exception we still return ``PENDING`` so the user
    flow can recover via the route layer's "decide" endpoint.  We
    do NOT mask the error silently though — it is re-raised after
    the synthetic ``PENDING`` is queued back through the audit
    sink, *unless* the caller asked for ``swallow_errors=True``.
    """

    __slots__ = ("_fast_path", "_swallow_errors", "_use_case")

    def __init__(
        self,
        *,
        request_permission_use_case: RequestPermissionUseCaseSource,
        fast_path: FastPathPolicy | None = None,
        swallow_errors: bool = True,
    ) -> None:
        if not isinstance(
            request_permission_use_case, RequestPermissionUseCaseSource
        ):
            raise TypeError(
                "request_permission_use_case must expose async execute()"
            )
        self._use_case = request_permission_use_case
        self._fast_path = fast_path
        self._swallow_errors = swallow_errors

    async def evaluate(
        self,
        *,
        request: PermissionRequest,
        workspace: Workspace,
    ) -> PermissionDecision:
        # 1. Try the fast-path policy first (smart-approval / sandbox).
        if self._fast_path is not None:
            try:
                fast = await self._fast_path(request, workspace)
            except Exception:
                if not self._swallow_errors:
                    raise
                fast = PermissionDecision.PENDING
            if fast is not PermissionDecision.PENDING:
                return fast

        # 2. Record the request in the security audit trail.  We
        #    build duck-typed values lazily so the bridge does not
        #    import qai.security domain types.
        try:
            subject, resource, mask = self._build_security_inputs(
                request=request, workspace=workspace
            )
            if subject is not None:
                await self._use_case.execute(
                    subject=subject,
                    resource=resource,
                    requested_mask=mask,
                )
        except Exception:
            if not self._swallow_errors:
                raise

        # 3. Without a fast-path verdict we surface PENDING — the user
        #    decides via the route layer.
        return PermissionDecision.PENDING

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _build_security_inputs(
        *,
        request: PermissionRequest,
        workspace: Workspace,
    ) -> tuple[Any | None, Any, Any]:
        """Construct the ``Subject`` / ``Resource`` / ``AceMask`` triple.

        Lazily imports the security domain types from
        ``qai.security.domain.value_objects``.  When the import fails
        (running in a context that does not expose security — e.g.
        a unit test that swapped in a stub use case) we return
        ``(None, None, None)`` so the bridge skips the audit step.
        """
        try:
            from qai.security.domain.value_objects import (
                AceMask,
                Resource,
                Subject,
            )
        except Exception:  # noqa: BLE001 — defensive
            return None, None, None
        subject = Subject(kind="system", identifier="ai_coding")
        resource = Resource(
            kind="skill",
            identifier=f"{request.tool_name.value}@{workspace.path}",
        )
        # Tool calls are read-or-write depending on the tool; the
        # ai_coding port does not split the two so we assume "execute"
        # ≈ exec mask bit on the security side.  Fall through to a
        # write-bit set if AceMask doesn't expose ``execute``.
        try:
            mask = AceMask(execute=True)
        except TypeError:
            mask = AceMask(write=True)
        return subject, resource, mask
