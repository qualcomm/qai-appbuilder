# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Immutable QAIRT switch job model: phases, candidate attempts, diagnostics.

These types describe a single switch job's shape without any I/O, process,
or framework dependency (see the API Contract's ``GET .../{job_id}`` phase
list and its allowlisted public-diagnostics fields). ``rolled_back`` and
``rollback_failed`` are distinct terminal :class:`Phase` values so a client
can never conflate a successful automatic recovery with a recovery that
itself failed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from qai.platform.qairt_switch.versioning import AppBuilderVersion, QairtVersion


class Phase(StrEnum):
    """A switch job's execution phase (API Contract, ``GET .../{job_id}``)."""

    VALIDATING_TARGET = "validating_target"
    CHECKING_EXTENSION = "checking_extension"
    STOPPING_WORKER = "stopping_worker"
    PROBING_CANDIDATE = "probing_candidate"
    DOWNLOADING = "downloading"
    INSTALLING = "installing"
    VERIFYING_EXTENSION = "verifying_extension"
    ACTIVATING = "activating"
    STARTING_WORKER = "starting_worker"
    VERIFYING_RUNTIME = "verifying_runtime"
    COMPLETE = "complete"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"


#: Phases after which no further phase transition occurs.
TERMINAL_PHASES: frozenset[Phase] = frozenset(
    {Phase.COMPLETE, Phase.ROLLED_BACK, Phase.ROLLBACK_FAILED}
)

#: The single phase representing an unqualified success.
SUCCESS_PHASES: frozenset[Phase] = frozenset({Phase.COMPLETE})


class CandidateOutcome(StrEnum):
    """The result of probing/installing one AppBuilder candidate version."""

    MISSING = "missing"
    INSTALLED = "installed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CandidateAttempt:
    """One bounded attempt against a single AppBuilder candidate version."""

    candidate: AppBuilderVersion
    outcome: CandidateOutcome
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class DownloadProgress:
    """Byte-level progress for the wheel currently downloading, if any."""

    bytes_downloaded: int
    total_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.bytes_downloaded < 0:
            raise ValueError("bytes_downloaded must be non-negative")
        if self.total_bytes is not None and self.total_bytes < 0:
            raise ValueError("total_bytes must be non-negative")


@dataclass(frozen=True, slots=True)
class SwitchDiagnostics:
    """Allowlisted public diagnostics for a switch job (API Contract).

    Deliberately excludes local paths, environment, commands, tokens,
    control characters, and raw stdout/stderr: those are internal
    classification data only and must be privacy-scrubbed before ever
    reaching a value stored here.
    """

    phase: Phase
    target_version: QairtVersion
    active_version: QairtVersion | None
    architecture: str
    correlation_id: str
    issue_url: str
    attempts: tuple[CandidateAttempt, ...] = field(default_factory=tuple)
    progress: DownloadProgress | None = None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    @property
    def succeeded(self) -> bool:
        return self.phase in SUCCESS_PHASES


__all__ = [
    "SUCCESS_PHASES",
    "TERMINAL_PHASES",
    "CandidateAttempt",
    "CandidateOutcome",
    "DownloadProgress",
    "Phase",
    "SwitchDiagnostics",
]
