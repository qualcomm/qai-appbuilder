# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Implementation-mode resource budgets — constants + frozen config (DISC-1 §22.5).

DISC-1 step2 "safe ground floor": the per-run / per-item resource caps an
implementation-mode turn will be bounded by.  Centralised here as named constants
plus a frozen+slots :class:`ImplementationBudget` dataclass (mirrors
:mod:`convergence_defaults`'s "key constants + frozen dataclass"范本).

🔴 ZERO consumption in step2.  Nothing reads these caps yet — step3 wires them into
the implementation turn (e.g. ``MAX_AGENT_ROUNDS`` →
``_run_speaker_turn(max_rounds_override=...)``, ``MAX_TOOL_CALLS`` /
``MAX_EXEC_SECONDS`` into the sandbox guard).  Defining them now keeps the safe
defaults reviewable in isolation, decoupled from the (not-yet-wired) executor.

Layering: ``application/use_cases`` — stdlib only; no ports / domain / adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "MAX_AGENT_ROUNDS",
    "MAX_TOOL_CALLS",
    "MAX_EXEC_SECONDS",
    "MAX_FILE_EDITS",
    "MAX_TOTAL_FILE_EDITS",
    "MAX_TOTAL_EXEC_CALLS",
    "MAX_TOTAL_RUNTIME_SECONDS",
    "MAX_TOTAL_CHANGED_FILES",
    "MAX_TOTAL_FILE_EDITS_KEY",
    "MAX_TOTAL_EXEC_CALLS_KEY",
    "MAX_TOTAL_RUNTIME_SECONDS_KEY",
    "MAX_TOTAL_CHANGED_FILES_KEY",
    "BUDGET_FIELD_BOUNDS",
    "ImplementationBudget",
    "ImplementationRunBudgetExceeded",
    "ImplementationRunBudgetTracker",
    "resolve_implementation_budget",
]


# ---------------------------------------------------------------------------
# Per-item (single implementation work-item) caps
# ---------------------------------------------------------------------------
#: Max agent rounds (tool→observe→think loops) for ONE implementation item.
MAX_AGENT_ROUNDS: Final[int] = 12
#: Max tool calls for ONE implementation item.
MAX_TOOL_CALLS: Final[int] = 40
#: Max wall-clock seconds of sandboxed ``exec`` for ONE implementation item.
MAX_EXEC_SECONDS: Final[int] = 300
#: Max distinct file edits for ONE implementation item.
MAX_FILE_EDITS: Final[int] = 30


# ---------------------------------------------------------------------------
# Per-run (whole implementation run, may span multiple items) caps
# ---------------------------------------------------------------------------
#: Max file edits across an entire implementation run.
MAX_TOTAL_FILE_EDITS: Final[int] = 80
#: Max ``exec`` calls across an entire implementation run.
MAX_TOTAL_EXEC_CALLS: Final[int] = 120
#: Max wall-clock seconds across an entire implementation run.
MAX_TOTAL_RUNTIME_SECONDS: Final[int] = 1800
#: Max distinct files changed across an entire implementation run.
MAX_TOTAL_CHANGED_FILES: Final[int] = 60


# ---------------------------------------------------------------------------
# meta["discussion"] keys for the user-configurable RUN-level caps (TODO-2).
# Only the four RUN-level caps are user-tunable from the UI (the per-item caps
# stay code constants — they bound a single turn and rarely need tuning); a
# missing key resolves to the constant default at read time (legacy untouched).
# ---------------------------------------------------------------------------
MAX_TOTAL_FILE_EDITS_KEY: Final[str] = "impl_max_total_file_edits"
MAX_TOTAL_EXEC_CALLS_KEY: Final[str] = "impl_max_total_exec_calls"
MAX_TOTAL_RUNTIME_SECONDS_KEY: Final[str] = "impl_max_total_runtime_seconds"
MAX_TOTAL_CHANGED_FILES_KEY: Final[str] = "impl_max_total_changed_files"

#: Per-field clamp bounds ``(key, default, min, max)`` for the run-level caps.
#: The resolver coerces each meta value into ``[min, max]`` (defends against a
#: malformed / hostile meta value disabling the safety cap or exhausting RAM).
BUDGET_FIELD_BOUNDS: Final[tuple[tuple[str, int, int, int], ...]] = (
    (MAX_TOTAL_FILE_EDITS_KEY, MAX_TOTAL_FILE_EDITS, 1, 100000),
    (MAX_TOTAL_EXEC_CALLS_KEY, MAX_TOTAL_EXEC_CALLS, 1, 100000),
    (MAX_TOTAL_RUNTIME_SECONDS_KEY, MAX_TOTAL_RUNTIME_SECONDS, 1, 86400),
    (MAX_TOTAL_CHANGED_FILES_KEY, MAX_TOTAL_CHANGED_FILES, 1, 100000),
)


@dataclass(frozen=True, slots=True)
class ImplementationBudget:
    """Resource caps for an implementation-mode run (DISC-1 §22.5).

    🔴 NOT consumed in step2.  step3 reads these fields when编排 the
    implementation turn (``max_agent_rounds`` →
    ``_run_speaker_turn(max_rounds_override=...)`` etc.).  Field defaults equal the
    module-level constants so the dataclass is the convenient bundle and the
    constants stay the individually-importable single source of truth.
    """

    max_agent_rounds: int = MAX_AGENT_ROUNDS
    max_tool_calls: int = MAX_TOOL_CALLS
    max_exec_seconds: int = MAX_EXEC_SECONDS
    max_file_edits: int = MAX_FILE_EDITS
    max_total_file_edits: int = MAX_TOTAL_FILE_EDITS
    max_total_exec_calls: int = MAX_TOTAL_EXEC_CALLS
    max_total_runtime_seconds: int = MAX_TOTAL_RUNTIME_SECONDS
    max_total_changed_files: int = MAX_TOTAL_CHANGED_FILES


# ---------------------------------------------------------------------------
# Run-level cumulative budget tracker (DISC-1 §22.5 — step3a, pure counter)
# ---------------------------------------------------------------------------
#: Tool names that mutate files on disk (each call ⇒ one file edit + the target
#: path is recorded as a "changed file").  Mirrors the ai_coding file_broker's
#: write-tool set (``write`` / ``edit`` / ``apply_patch``).
_FILE_EDIT_TOOLS: Final[frozenset[str]] = frozenset({"write", "edit", "apply_patch"})
#: Tool name that runs a sandboxed shell command (each call ⇒ one exec call).
_EXEC_TOOL: Final[str] = "exec"
#: Argument keys that hold a target file path (mirrors ai_coding file_broker
#: ``"path"`` plus the common ``"file_path"`` alias).
_PATH_ARG_KEYS: Final[tuple[str, ...]] = ("path", "file_path")


class ImplementationRunBudgetExceeded(Exception):
    """Raised when a run-level implementation budget cap is hit (DISC-1 §22.5).

    Carries which cap was exceeded (:attr:`reason`) plus the offending
    ``metric``/``limit`` values so the caller (step3c) can pause the run with an
    auditable message instead of silently continuing (§22.5 :1349 — a run-level
    budget hit ⇒ run ``phase=paused``, never silent continuation).
    """

    def __init__(self, *, reason: str, metric: float, limit: float) -> None:
        self.reason = reason
        self.metric = metric
        self.limit = limit
        super().__init__(
            f"implementation run budget exceeded: {reason} "
            f"(metric={metric} > limit={limit})"
        )


class ImplementationRunBudgetTracker:
    """Stateful, per-run cumulative resource counter (DISC-1 §22.5 — step3a).

    Tracks file edits / ``exec`` calls / wall-clock runtime / distinct files
    touched across an entire implementation run (which may span multiple items
    and multiple serially-implementing roles).  When any cumulative metric
    exceeds its :class:`ImplementationBudget` cap, the offending mutating call
    :meth:`record_tool_call` / :meth:`record_runtime` raises
    :class:`ImplementationRunBudgetExceeded` — guarding against the *accumulated*
    blast radius of several roles implementing back-to-back (§22.5 M2-re).

    🔴 ZERO consumption in step3a.  This is a PURE counter — it never actually
    runs a tool, only counts.  step3c wires it in: after each tool call the
    executor feeds the call into :meth:`record_tool_call`; on a raise the run is
    moved to ``phase=paused`` (§22.5 :1349, not silently continued).

    Like :class:`...convergence_controller.ConvergenceController`, this is a
    PER-RUN instance (one constructed per implementation run), so the counters it
    carries are legitimate transient per-run state — NOT global mutable state.

    Layering: ``application/use_cases`` — stdlib only; no ports / domain /
    adapters / fastapi imports.
    """

    __slots__ = (
        "_budget",
        "_total_file_edits",
        "_total_exec_calls",
        "_total_runtime_seconds",
        "_changed_files",
        "_pending_args",
    )

    def __init__(self, budget: ImplementationBudget = ImplementationBudget()) -> None:
        self._budget = budget
        self._total_file_edits = 0
        self._total_exec_calls = 0
        self._total_runtime_seconds = 0.0
        # Distinct file paths touched by mutating tools — a set so re-editing the
        # same file counts once toward "changed files" while every edit still
        # accrues toward "file edits".
        self._changed_files: set[str] = set()
        # Pending tool-call args buffered by tool name (set on ``record_tool_call``
        # / consumed on the matching ``record_tool_result``), so the changed-file
        # path is known when the SUCCESSFUL result arrives. Calls/results are
        # sequential per speaker turn, so a last-write-wins map is sufficient.
        self._pending_args: dict[str, dict] = {}

    @property
    def budget(self) -> ImplementationBudget:
        """The caps this tracker enforces."""
        return self._budget

    def record_tool_call(
        self, *, tool_name: str, args: dict | None = None
    ) -> None:
        """Note a tool call's args WITHOUT counting it yet (State-Truth-First).

        A ``TOOL_CALL`` frame means the model *attempted* a tool — but the call
        may be rejected / error out (``ToolInvocationResult.ok=False`` ⇒ no file
        actually written / command actually run). Counting here would consume the
        run budget on *attempts* and let a high-failure run pause before any real
        work happened. So this only buffers the args; the actual file-edit /
        exec-call accounting happens in :meth:`record_tool_result` when the
        SUCCESSFUL result arrives. Kept as a method (not removed) so callers and
        tests have a stable entry point.
        """
        if tool_name in _FILE_EDIT_TOOLS or tool_name == _EXEC_TOOL:
            self._pending_args[tool_name] = dict(args or {})

    def record_tool_result(self, *, tool_name: str, ok: bool) -> None:
        """Account for one SUCCESSFUL tool result, then re-check the caps.

        Only a successful (``ok``) ``write`` / ``edit`` / ``apply_patch`` ⇒ ``+1``
        file edit (and the buffered target path joins the distinct changed-files
        set); a successful ``exec`` ⇒ ``+1`` exec call. A failed result counts
        nothing (the file was not written / the command did not run). Any other
        tool is a no-op for the budget.

        Raises :class:`ImplementationRunBudgetExceeded` the moment a cap is hit.
        """
        args = self._pending_args.pop(tool_name, None)
        if not ok:
            return
        if tool_name in _FILE_EDIT_TOOLS:
            self._total_file_edits += 1
            path = self._extract_path(args)
            if path is not None:
                self._changed_files.add(path)
        elif tool_name == _EXEC_TOOL:
            self._total_exec_calls += 1
        self.check()

    def record_runtime(self, seconds: float) -> None:
        """Accumulate wall-clock runtime, then re-check the run-level caps."""
        self._total_runtime_seconds += seconds
        self.check()

    def check(self) -> None:
        """Validate every cumulative metric against its cap (poll-friendly).

        Raises :class:`ImplementationRunBudgetExceeded` for the first cap hit.
        """
        if self._total_file_edits > self._budget.max_total_file_edits:
            raise ImplementationRunBudgetExceeded(
                reason="total_file_edits",
                metric=self._total_file_edits,
                limit=self._budget.max_total_file_edits,
            )
        if self._total_exec_calls > self._budget.max_total_exec_calls:
            raise ImplementationRunBudgetExceeded(
                reason="total_exec_calls",
                metric=self._total_exec_calls,
                limit=self._budget.max_total_exec_calls,
            )
        if len(self._changed_files) > self._budget.max_total_changed_files:
            raise ImplementationRunBudgetExceeded(
                reason="total_changed_files",
                metric=len(self._changed_files),
                limit=self._budget.max_total_changed_files,
            )
        if self._total_runtime_seconds > self._budget.max_total_runtime_seconds:
            raise ImplementationRunBudgetExceeded(
                reason="total_runtime_seconds",
                metric=self._total_runtime_seconds,
                limit=self._budget.max_total_runtime_seconds,
            )

    def snapshot(self) -> dict:
        """Return the current cumulative values (for audit / log / control panel).

        §22.5 "变更可审计": a plain dict the control-panel summary / structured
        log can surface without reaching into private state.
        """
        return {
            "total_file_edits": self._total_file_edits,
            "total_exec_calls": self._total_exec_calls,
            "total_runtime_seconds": self._total_runtime_seconds,
            "total_changed_files": len(self._changed_files),
        }

    @staticmethod
    def _extract_path(args: dict | None) -> str | None:
        """Pull the target file path from tool args (``path`` / ``file_path``)."""
        if not args:
            return None
        for key in _PATH_ARG_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value:
                return value
        return None


# ---------------------------------------------------------------------------
# Resolver: meta["discussion"] → ImplementationBudget (TODO-2, user-tunable run caps)
# ---------------------------------------------------------------------------
def _coerce_cap(value: object, *, default: int, lo: int, hi: int) -> int:
    """Coerce a meta value into an int cap clamped to ``[lo, hi]``.

    A missing / non-numeric / out-of-range value falls back to ``default``
    (then clamped), so a malformed or hostile meta entry can neither disable the
    safety cap nor request an absurd (RAM-exhausting) one. ``bool`` is rejected
    explicitly (``True``/``False`` are ``int`` subclasses but never a valid cap).
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        n = value
    elif isinstance(value, float) and value == value:  # finite, not NaN
        n = int(value)
    else:
        return default
    return max(lo, min(hi, n))


def resolve_implementation_budget(
    discussion: dict | None,
) -> ImplementationBudget:
    """Resolve a per-conversation :class:`ImplementationBudget` from meta (TODO-2).

    Reads the four user-tunable RUN-level caps from ``meta["discussion"]``
    (:data:`BUDGET_FIELD_BOUNDS`), each coerced + clamped to a safe range; an
    ABSENT key resolves to its constant default (legacy conversations untouched).
    The four PER-ITEM caps stay code constants (not exposed) — they bound a
    single turn and are intentionally not user-tunable from the UI.

    Pure function over a stdlib dict; the orchestrator constructs the run-level
    :class:`ImplementationRunBudgetTracker` with the result.
    """
    d = discussion or {}
    return ImplementationBudget(
        max_total_file_edits=_coerce_cap(
            d.get(MAX_TOTAL_FILE_EDITS_KEY),
            default=MAX_TOTAL_FILE_EDITS,
            lo=1,
            hi=100000,
        ),
        max_total_exec_calls=_coerce_cap(
            d.get(MAX_TOTAL_EXEC_CALLS_KEY),
            default=MAX_TOTAL_EXEC_CALLS,
            lo=1,
            hi=100000,
        ),
        max_total_runtime_seconds=_coerce_cap(
            d.get(MAX_TOTAL_RUNTIME_SECONDS_KEY),
            default=MAX_TOTAL_RUNTIME_SECONDS,
            lo=1,
            hi=86400,
        ),
        max_total_changed_files=_coerce_cap(
            d.get(MAX_TOTAL_CHANGED_FILES_KEY),
            default=MAX_TOTAL_CHANGED_FILES,
            lo=1,
            hi=100000,
        ),
    )
