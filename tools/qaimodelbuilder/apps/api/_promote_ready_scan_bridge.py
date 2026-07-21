# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Turn-end promote-ready scan bridge (apps/api wiring root).

Adapts :class:`qai.app_builder.application.use_cases.deferred_routes.ImportScanBinsUseCase`
onto :class:`qai.chat.application.ports.PromoteReadyScanPort` so
:class:`qai.chat.application.use_cases.streaming.StreamChatUseCase` can perform
turn-end promote-ready detection (persist ``Conversation.detected_model`` per
migration 057) WITHOUT the chat context ever importing ``qai.app_builder``.

Why here (apps/api layer)
-------------------------
``qai.chat`` MUST NOT import ``qai.app_builder`` (context-isolation
import-linter contract). Exactly like :mod:`_workspace_grant_bridge`
(chat → security) and :mod:`_appbuilder_tools_bridge` (chat tool registry →
app_builder run use case), this module lives under ``apps/api`` — the only
layer allowed to depend on multiple bounded contexts — and translates between
the two sides. The chat context only ever names the ``PromoteReadyScanPort``
Protocol + the ``PromoteReadyVariant`` DTO.

Lazy container resolution
-------------------------
:class:`~apps.api.di.Container._wire` builds ``chat`` (line 320) BEFORE
``app_builder`` (line 321), so ``container.app_builder`` does NOT yet exist
when :func:`build_chat_services` instantiates :class:`StreamChatUseCase` and
its ``promote_ready_scan`` port is captured. We therefore hand the port a
reference to the (not-yet-finished) container and dereference
``container.app_builder.import_scan_bins_use_case`` LAZILY inside
:meth:`AppBuilderPromoteReadyScanAdapter.scan` — by the time a real turn ends
the whole container is fully wired. This mirrors :mod:`_workspace_grant_bridge`,
which also captures ``container`` and does ``getattr(container, "security",
None)`` at call time.

Best-effort
-----------
:meth:`scan` NEVER raises: a missing ``app_builder`` namespace, missing
``import_scan_bins_use_case``, a bad ``model_workdir``, or any transient scan
error returns an empty tuple. The turn-end detector treats that as "scanned,
nothing promotable" and does not clobber a prior successful detection with an
empty result if the workspace path itself was absent from the summary — that
distinction is enforced upstream in ``StreamChatUseCase._detect_promote_ready``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from qai.chat.application.ports import (
    PromoteReadyScanPort,
    PromoteReadyVariant,
)
from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container

__all__ = ["AppBuilderPromoteReadyScanAdapter"]

_log = get_logger(__name__)


class AppBuilderPromoteReadyScanAdapter(PromoteReadyScanPort):
    """Adapter: :class:`ImportScanBinsUseCase` → :class:`PromoteReadyScanPort`.

    Holds a reference to the apps container so ``container.app_builder`` can be
    resolved lazily at scan time (chat is built before app_builder). Returns an
    empty tuple on any error — never raises — so turn-end detection is fully
    best-effort per AGENTS.md §5.
    """

    __slots__ = ("_container",)

    def __init__(self, container: "Container") -> None:
        self._container = container

    async def scan(
        self, model_workdir: str
    ) -> tuple[PromoteReadyVariant, ...]:
        """Scan ``model_workdir/output/`` for promote-eligible variants.

        Returns an empty tuple when:
          * ``model_workdir`` is blank;
          * the container has no ``app_builder`` namespace / no
            ``import_scan_bins_use_case`` (unwired test container);
          * the directory does not exist / has no matching bins;
          * any transient scan error occurs.

        Never raises. The turn-end detector distinguishes "scanned, no variants"
        from "not scanned" via its own bookkeeping (workdir extraction step),
        so an empty tuple here safely means "the scan ran and found nothing".
        """
        if not model_workdir or not isinstance(model_workdir, str):
            return ()
        app_builder = getattr(self._container, "app_builder", None)
        uc = getattr(app_builder, "import_scan_bins_use_case", None)
        if uc is None:
            return ()
        try:
            results = await uc.execute(model_workdir=model_workdir)
        except Exception:  # noqa: BLE001 — best-effort per AGENTS.md §5
            _log.debug(
                "promote_ready_scan.scan_failed",
                model_workdir=model_workdir,
                exc_info=True,
            )
            return ()
        out: list[PromoteReadyVariant] = []
        for r in results:
            precision = getattr(r, "precision", None)
            label = getattr(r, "label", None)
            if not isinstance(precision, str) or not precision:
                continue
            if not isinstance(label, str) or not label:
                continue
            out.append(
                PromoteReadyVariant(precision=precision, label=label)
            )
        return tuple(out)
