# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain model for a *standalone fullstack app project* (plan §6.1).

An "app project" is a self-contained FastAPI + frontend application that
the App Builder feature generates on disk under
``data/app_builder/<app_id>/`` and that the host can list, run (as a
managed subprocess), and package. It is a distinct concept from the
:class:`~qai.app_builder.domain.app_model.AppModelDefinition` registry
entry (a single-model runner pack); an app project may *bundle* one or
more models but is itself a deployable web application.

Everything here is pure domain: frozen dataclasses / ``Literal`` status
types and domain errors. No framework, no I/O, no YAML — the
infrastructure repository (``app_project_repository.py``) owns parsing
``app.yaml`` into these value objects.

VOs / entities defined (plan §6.1, §1.4):

* :class:`AppProjectId` — validated app id (dir name).
* :data:`AppProjectStatus` — run-status ``Literal``.
* :class:`AppProjectModelRef` — one bundled model reference.
* :class:`AppProjectDefinition` — the parsed ``app.yaml`` + on-disk facts.
* :class:`AppProjectRunInfo` — managed-run status snapshot (Phase 3).

Domain errors carry a stable :attr:`code` (plan §5.7) so the application
/ interface layers route on the machine code, not the subclass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from qai.platform.errors import DomainError

__all__ = [
    "AppProjectAlreadyRunningError",
    "AppProjectDefinition",
    "AppProjectId",
    "AppProjectInvalidError",
    "AppProjectModelRef",
    "AppProjectNoBindablePortError",
    "AppProjectDeleteFailedError",
    "AppProjectNotFoundError",
    "AppProjectNotRunningError",
    "AppProjectPackageFailedError",
    "AppProjectPortInUseError",
    "AppProjectRunInfo",
    "AppProjectStartFailedError",
    "AppProjectStatus",
]


#: Highest valid TCP port number (inclusive).
_MAX_PORT = 65535

_APP_PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
"""Allowed app-id alphabet (plan §1.4 / §7.0).

Lowercase letters / digits / underscore / hyphen; must start with a
letter or digit; total length 2..64. App ids double as the on-disk
directory name (``data/app_builder/<app_id>/``) so the narrow alphabet
is a first line of path-traversal defence — no separators, no ``..``, no
drive letters can satisfy the regex.
"""


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
AppProjectStatus = Literal[
    "stopped",
    "starting",
    "running",
    "ready",
    "failed",
    "packaging",
]
"""Managed-run status of an app project.

``stopped`` — no managed process (the only status Phase 2 ever reports,
since listing is read-only). ``starting`` → ``running`` → ``ready``:
the subprocess spawned, is up, and passed its ``/health`` probe (``ready``
means health passed; ``running`` means the process is alive but health
has not yet been confirmed). ``failed`` — spawn / readiness failed.
``packaging`` — a packaging job is in progress (packaging is a separate
axis from the run lifecycle per plan §5.3, but surfaced through the same
status field for the simple single-op UI).
"""


# ---------------------------------------------------------------------------
# Model reference (one row of ``app.yaml`` ``models:``)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class AppProjectModelRef:
    """A single model bundled by an app project (plan §1.4 ``models[]``).

    ``pack_dir`` / ``model_dir`` are the *raw* strings from ``app.yaml``;
    they typically contain ``${APP_ROOT}`` placeholders and are NOT
    expanded by the domain / repository layer — the process manager
    (Phase 3) expands them when building the child environment.
    """

    id: str
    title: str
    builtin: bool
    pack_dir: str | None = None
    model_dir: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("AppProjectModelRef.id must be a non-empty str")
        if not isinstance(self.title, str):
            raise TypeError("AppProjectModelRef.title must be str")
        if not isinstance(self.builtin, bool):
            raise TypeError("AppProjectModelRef.builtin must be bool")
        for name, value in (
            ("pack_dir", self.pack_dir),
            ("model_dir", self.model_dir),
        ):
            if value is not None and not isinstance(value, str):
                raise TypeError(f"AppProjectModelRef.{name} must be str or None")


# ---------------------------------------------------------------------------
# App id
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class AppProjectId:
    """Validated identifier of an app project (== on-disk dir name).

    Alphabet: ``[a-z0-9][a-z0-9_-]{1,63}`` (plan §1.4). Construction
    raises :class:`ValueError` on any invalid value; the repository
    translates that into an :class:`AppProjectNotFoundError` /
    :class:`AppProjectInvalidError` at the boundary.
    """

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError(
                f"AppProjectId.value must be str, got {type(self.value).__name__}"
            )
        if not _APP_PROJECT_ID_RE.match(self.value):
            raise ValueError(
                "AppProjectId.value must match ^[a-z0-9][a-z0-9_-]{1,63}$ "
                f"(length 2-64), got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


# ---------------------------------------------------------------------------
# App definition (parsed ``app.yaml`` + on-disk facts)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class AppProjectDefinition:
    """The parsed ``app.yaml`` for one app project, plus on-disk facts.

    Fields map to the ``app.yaml`` schema (plan §1.4). ``models`` is a
    tuple (immutable / hashable). ``path`` is the absolute app directory
    and ``modified_at`` its mtime (float epoch seconds) — both are
    filled by the repository from the filesystem, not from the YAML.
    """

    id: AppProjectId
    name: str
    description: str
    models: tuple[AppProjectModelRef, ...] = field(default_factory=tuple)
    app_module: str = "backend.main:app"
    health_path: str = "/health"
    frontend_path: str = "/"
    host: str = "127.0.0.1"
    preferred_port: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    package_include_models: bool = True
    package_include_outputs: bool = False
    path: str
    modified_at: float

    def __post_init__(self) -> None:  # noqa: PLR0912 - cohesive per-field validation gate; splitting would scatter the checks
        if not isinstance(self.id, AppProjectId):
            raise TypeError("AppProjectDefinition.id must be an AppProjectId")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("AppProjectDefinition.name must be a non-empty str")
        if not isinstance(self.description, str):
            raise TypeError("AppProjectDefinition.description must be str")
        if not isinstance(self.models, tuple):
            raise TypeError("AppProjectDefinition.models must be a tuple")
        for i, m in enumerate(self.models):
            if not isinstance(m, AppProjectModelRef):
                raise TypeError(
                    f"AppProjectDefinition.models[{i}] must be AppProjectModelRef"
                )
        for name, value in (
            ("app_module", self.app_module),
            ("health_path", self.health_path),
            ("frontend_path", self.frontend_path),
            ("host", self.host),
            ("path", self.path),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"AppProjectDefinition.{name} must be a non-empty str"
                )
        if self.preferred_port is not None and (
            not isinstance(self.preferred_port, int)
            or isinstance(self.preferred_port, bool)
            or not (1 <= self.preferred_port <= _MAX_PORT)
        ):
            raise ValueError(
                "AppProjectDefinition.preferred_port must be an int in 1..65535 or None"
            )
        for name, value in (
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
        ):
            if value is not None and not isinstance(value, str):
                raise TypeError(f"AppProjectDefinition.{name} must be str or None")
        if not isinstance(self.package_include_models, bool):
            raise TypeError(
                "AppProjectDefinition.package_include_models must be bool"
            )
        if not isinstance(self.package_include_outputs, bool):
            raise TypeError(
                "AppProjectDefinition.package_include_outputs must be bool"
            )
        if not isinstance(self.modified_at, (int, float)) or isinstance(
            self.modified_at, bool
        ):
            raise TypeError("AppProjectDefinition.modified_at must be a number")


# ---------------------------------------------------------------------------
# Managed-run snapshot (Phase 3)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class AppProjectRunInfo:
    """Snapshot of an app project's managed-run state (plan §5.3).

    Phase 2 never constructs a non-stopped instance (listing is
    read-only); the shape is defined now so Phase 3's process manager /
    use cases can return it without a follow-up domain change.
    """

    app_id: str
    status: AppProjectStatus
    port: int | None = None
    url: str | None = None
    pid: int | None = None
    process_id: str | None = None
    manual_command: str | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.app_id, str) or not self.app_id:
            raise ValueError("AppProjectRunInfo.app_id must be a non-empty str")


# ---------------------------------------------------------------------------
# Domain errors (plan §5.7 — stable codes)
# ---------------------------------------------------------------------------
class AppProjectNotFoundError(DomainError):
    """Raised when an app project directory cannot be located."""

    default_code = "app_builder.app_not_found"


class AppProjectInvalidError(DomainError):
    """Raised when ``app.yaml`` is missing or fails validation."""

    default_code = "app_builder.app_invalid"


class AppProjectAlreadyRunningError(DomainError):
    """Raised when a start is requested for an already-managed app."""

    default_code = "app_builder.app_already_running"


class AppProjectNotRunningError(DomainError):
    """Raised when a stop / logs op targets an app with no managed process."""

    default_code = "app_builder.app_not_running"


class AppProjectStartFailedError(DomainError):
    """Raised when spawning the app process fails or readiness times out."""

    default_code = "app_builder.app_start_failed"


class AppProjectPackageFailedError(DomainError):
    """Raised when packaging the app project fails."""

    default_code = "app_builder.package_failed"


class AppProjectPortInUseError(DomainError):
    """Raised when a user-specified port cannot be bound (Phase 3)."""

    default_code = "app_builder.port_in_use"


class AppProjectNoBindablePortError(DomainError):
    """Raised when the automatic port pool is exhausted (Phase 3)."""

    default_code = "app_builder.no_bindable_port"


class AppProjectDeleteFailedError(DomainError):
    """Raised when deleting an app project directory fails (IO error)."""

    default_code = "app_builder.delete_failed"
