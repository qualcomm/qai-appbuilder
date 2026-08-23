# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain layer for ``qai.remote_deploy``.

Pure Python entities — no framework dependencies (domain-purity contract
per import-linter forbidden set).

Entities
--------
* ``RemoteHost``     — connection parameters for a remote target (IP, user,
  auth). Immutable value object; carries no mutable state.
* ``RemoteInstance`` — a running QAI ModelBuilder process on a remote host,
  identified by ``instance_id`` (UUID), with its URL and lifecycle state.
* ``DeploymentState``— lifecycle enum: CONNECTING / INSTALLING / STARTING /
  RUNNING / STOPPED / FAILED.

Auth is intentionally opaque at the domain layer: the ``auth_ref`` field is
a string token that the infrastructure layer resolves via ``SecretStore``.
Passwords / passphrases NEVER appear as plain text in domain objects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "AuthMethod",
    "DeploymentState",
    "RemoteHost",
    "RemoteInstance",
]


class AuthMethod(str, Enum):
    """Authentication method for SSH connections."""

    PASSWORD = "password"
    PRIVATE_KEY = "private_key"


class DeploymentState(str, Enum):
    """Lifecycle state of a remote QAI ModelBuilder instance."""

    CONNECTING = "connecting"
    INSTALLING = "installing"
    STARTING = "starting"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RemoteHost:
    """Immutable connection parameters for a remote target.

    ``auth_ref`` is a SecretStore key — never the raw credential.
    ``key_path`` is the filesystem path to the private key file (only
    meaningful when ``auth_method == AuthMethod.PRIVATE_KEY``).
    """

    host: str
    port: int
    username: str
    auth_method: AuthMethod
    auth_ref: str  # SecretStore key for password or key passphrase
    key_path: str = ""  # path to private key file (private_key auth only)

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("host must not be empty")
        if not (1 <= self.port <= 65535):
            raise ValueError(f"port must be 1-65535, got {self.port}")
        if not self.username:
            raise ValueError("username must not be empty")
        if self.auth_method == AuthMethod.PRIVATE_KEY and not self.key_path:
            raise ValueError("key_path is required for private_key auth")


@dataclass(slots=True)
class RemoteInstance:
    """A running (or stopped) QAI ModelBuilder instance on a remote host."""

    instance_id: str
    host: str
    port: int
    username: str
    state: DeploymentState
    remote_url: str = ""
    error_message: str = ""
    # pid of the remote process (0 = unknown)
    remote_pid: int = 0
    # log lines captured during install/start (bounded buffer)
    log_lines: list[str] = field(default_factory=list)

    @property
    def is_running(self) -> bool:
        return self.state == DeploymentState.RUNNING

    def append_log(self, line: str, *, max_lines: int = 500) -> None:
        self.log_lines.append(line)
        if len(self.log_lines) > max_lines:
            self.log_lines = self.log_lines[-max_lines:]
