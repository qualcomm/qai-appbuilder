# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Settings-driven :class:`SmartApprovalPort` adapter (PR-040).

Minimum-viable implementation: when
``settings.security.smart_approval_enabled`` is ``False`` (the default),
this adapter unconditionally returns
:attr:`SmartApprovalDecision.UNDECIDED` so every request is routed to
a human reviewer (the request stays PENDING in
``security_permission_request``).

When the flag is ``True`` the adapter still returns ``UNDECIDED``
because the heuristic is intentionally not implemented in-process —
wiring a real heuristic would require a new ``EvaluatorPort`` (LLM /
moderation API) and an ``EmbeddingClientPort``, neither of which are
part of this product's authorisation surface. The flag exists as a
forward-compatibility hook so adopters can flip it on without
refactoring the DI graph if they integrate an external heuristic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from qai.security.application.ports import (
    SmartApprovalDecision,
)
from qai.security.domain.value_objects import AceMask, Resource, Subject

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.config.settings import SecuritySettings

__all__ = ["SettingsSmartApprovalAdapter"]


class SettingsSmartApprovalAdapter:
    """Configuration-driven :class:`SmartApprovalPort` placeholder."""

    __slots__ = ("_enabled",)

    def __init__(self, *, settings: "SecuritySettings") -> None:
        # Snapshot the flag at wire time; runtime reconfiguration is
        # deliberately not supported in PR-040 (would require a settings
        # observer that we don't ship yet).
        self._enabled = bool(settings.smart_approval_enabled)

    async def evaluate(
        self,
        *,
        subject: Subject,
        resource: Resource,
        requested_mask: AceMask,
    ) -> SmartApprovalDecision:
        # Always abstain — see module docstring. The arguments are
        # accepted to satisfy the Protocol; future heuristic versions
        # will inspect them.
        del subject, resource, requested_mask
        return SmartApprovalDecision.UNDECIDED

    @property
    def enabled(self) -> bool:
        """Expose the snapshot flag for tests / diagnostics."""

        return self._enabled
