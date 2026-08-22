# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases: list / save / delete ai_coding credentials.

Backs the legacy ``GET / POST / DELETE /api/cc/credentials`` routes.
All credential values flow through
:class:`qai.platform.persistence.secrets.SecretStore` per v2.7 §3.3
("API key, channel app secret 等任何长期凭据**必须**走 SecretStore");
nothing is persisted in plain-text in :class:`Settings`,
``forge_config.json``, or any other config surface.

Service namespace
-----------------
All ai_coding credentials live under the SecretStore service name
``"ai_coding"``.  Per-key naming uses the legacy ``cc::<VAR_NAME>``
prefix so the existing OS-keyring entries from a legacy install can
be migrated 1:1 without a rename.

Whitelist
---------
The set of allowed credential keys is centralised in this module so
the route layer can re-use it for input validation and the GET
response can list the same set.  Adding a new credential variable
involves a one-line addition to :data:`CC_CREDENTIAL_VARS` here.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass

from qai.platform.persistence.secrets import SecretStore

__all__ = [
    "CC_CREDENTIAL_VARS",
    "CC_SECRET_SERVICE",
    "OC_CREDENTIAL_VARS",
    "OC_SECRET_SERVICE",
    "CredentialStatus",
    "CredentialsStatusResult",
    "DeleteCredentialCommand",
    "DeleteCredentialUseCase",
    "GetCodingCredentialsUseCase",
    "SaveCodingCredentialsCommand",
    "SaveCodingCredentialsResult",
    "SaveCodingCredentialsUseCase",
]


#: SecretStore service namespace for ai_coding credentials.  All
#: keys land under ``("ai_coding", "<VAR_NAME>")``.
CC_SECRET_SERVICE = "ai_coding"

#: SecretStore service namespace for OpenCode-side credentials
#: (PR-105).  Kept distinct from :data:`CC_SECRET_SERVICE` so a
#: rotated CC key doesn't accidentally clobber an OC key — they live
#: under different OS keyring entries.
OC_SECRET_SERVICE = "ai_coding_oc"


#: The whitelist of credential environment variable names the
#: ai_coding (Claude Code) context cares about.  Every entry flows
#: through :class:`SecretStore` per v2.7 §3.3.  The set is a superset
#: of the legacy ``backend.ai_coding.api_routes._CC_CREDENTIAL_VARS``:
#: it keeps the V1 secret vars (incl. ``ANTHROPIC_FOUNDRY_API_KEY``
#: for the Azure AI Foundry auth scheme) and additionally manages the
#: AWS Bedrock / Google Vertex credential vars surfaced by the V2
#: WebUI's auth-scheme picker.  Adding a new credential variable is a
#: one-line addition here.
CC_CREDENTIAL_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    # Azure AI Foundry secret key (V1 parity — the legacy
    # ``_CC_CREDENTIAL_VARS`` includes it; the V2 WebUI's Azure auth
    # scheme exposes it as a secret input that must persist to the
    # SecretStore, not plain-text config).
    "ANTHROPIC_FOUNDRY_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
)


#: The whitelist of OpenCode-side credential environment variable
#: names (PR-105).  OpenCode reads its own auth env (``OPENCODE_*``)
#: plus shared model-provider keys; mirroring the legacy backend's
#: implicit set.  Adding a new variable involves a one-line edit
#: here + a one-line edit in the route layer's whitelist.
OC_CREDENTIAL_VARS: tuple[str, ...] = (
    "OPENCODE_API_KEY",
    "OPENCODE_BASE_URL",
    "OPENCODE_USERNAME",
    "OPENCODE_PASSWORD",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
)


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class CredentialStatus:
    """Per-variable status surfaced via ``GET /credentials``.

    Mirrors the legacy wire shape: ``{in_store, in_env, configured}``
    where ``configured`` is the OR of the two — caller can be
    configured either via the OS-managed SecretStore or via a
    pre-set process environment variable.
    """

    var_name: str
    in_store: bool
    in_env: bool

    @property
    def configured(self) -> bool:
        return self.in_store or self.in_env


@dataclass(frozen=True, slots=True, kw_only=True)
class CredentialsStatusResult:
    """Return shape of :class:`GetCodingCredentialsUseCase`."""

    statuses: tuple[CredentialStatus, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class SaveCodingCredentialsCommand:
    """Input for :class:`SaveCodingCredentialsUseCase`.

    ``credentials`` keys MUST be in :data:`CC_CREDENTIAL_VARS`; the
    use case skips unknown keys silently (route layer can return a
    400 envelope upstream if it prefers strict validation).

    Value semantics (mirrors legacy):

    * empty string → delete the secret + clear the env var.
    * ``"****"``    → masked placeholder, skip (don't overwrite).
    * any other    → save to SecretStore + inject into ``os.environ``.
    """

    credentials: dict[str, str]


@dataclass(frozen=True, slots=True, kw_only=True)
class SaveCodingCredentialsResult:
    """Aggregate outcome of a credential bulk-save call."""

    saved: tuple[str, ...]
    deleted: tuple[str, ...]
    skipped: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class DeleteCredentialCommand:
    """Input for :class:`DeleteCredentialUseCase`."""

    var_name: str


# ---------------------------------------------------------------------------
# Use cases
# ---------------------------------------------------------------------------


class GetCodingCredentialsUseCase:
    """Application service for listing credential configuration status."""

    def __init__(
        self,
        *,
        secret_store: SecretStore,
        var_names: Iterable[str] = CC_CREDENTIAL_VARS,
        env_lookup=os.environ.get,
        # PR-105: service namespace is now injectable so the OC
        # credentials UC can reuse the same class with the
        # ``OC_SECRET_SERVICE`` namespace.  Default preserves the
        # PR-104b ``CC_SECRET_SERVICE`` shape.
        service: str = CC_SECRET_SERVICE,
    ) -> None:
        self._secret_store = secret_store
        self._var_names = tuple(var_names)
        self._env_lookup = env_lookup
        self._service = service

    async def execute(self) -> CredentialsStatusResult:
        statuses: list[CredentialStatus] = []
        for var in self._var_names:
            in_store = self._secret_store.exists(self._service, var)
            in_env = bool(self._env_lookup(var))
            statuses.append(
                CredentialStatus(
                    var_name=var,
                    in_store=in_store,
                    in_env=in_env,
                )
            )
        return CredentialsStatusResult(statuses=tuple(statuses))


class SaveCodingCredentialsUseCase:
    """Application service for bulk-saving credentials.

    The route layer is expected to filter unknown keys in input
    validation; this use case defensively skips them (recording
    them in :attr:`SaveCodingCredentialsResult.skipped`) so a
    misconfigured client never leaks a secret to an unintended
    SecretStore namespace.
    """

    def __init__(
        self,
        *,
        secret_store: SecretStore,
        var_names: Iterable[str] = CC_CREDENTIAL_VARS,
        # Test seam: a pure dict can stand in for ``os.environ``.
        environ: dict[str, str] | None = None,
        # PR-105: see :class:`GetCodingCredentialsUseCase` for the
        # rationale behind making the service namespace injectable.
        service: str = CC_SECRET_SERVICE,
    ) -> None:
        self._secret_store = secret_store
        self._allowed = frozenset(var_names)
        self._environ = environ if environ is not None else os.environ
        self._service = service

    async def execute(
        self, command: SaveCodingCredentialsCommand
    ) -> SaveCodingCredentialsResult:
        saved: list[str] = []
        deleted: list[str] = []
        skipped: list[str] = []

        for var, raw in command.credentials.items():
            if var not in self._allowed:
                skipped.append(var)
                continue

            value = (raw or "").strip()
            if value == "****":
                # Masked placeholder — preserve the existing value.
                skipped.append(var)
                continue

            if not value:
                # Empty string → delete + clear env var (idempotent).
                if self._secret_store.exists(self._service, var):
                    self._secret_store.delete(self._service, var)
                self._environ.pop(var, None)
                deleted.append(var)
                continue

            self._secret_store.set(self._service, var, value)
            self._environ[var] = value
            saved.append(var)

        return SaveCodingCredentialsResult(
            saved=tuple(saved),
            deleted=tuple(deleted),
            skipped=tuple(skipped),
        )


class DeleteCredentialUseCase:
    """Application service for deleting a single credential."""

    def __init__(
        self,
        *,
        secret_store: SecretStore,
        var_names: Iterable[str] = CC_CREDENTIAL_VARS,
        environ: dict[str, str] | None = None,
        # PR-105: see :class:`GetCodingCredentialsUseCase` for the
        # rationale behind making the service namespace injectable.
        service: str = CC_SECRET_SERVICE,
    ) -> None:
        self._secret_store = secret_store
        self._allowed = frozenset(var_names)
        self._environ = environ if environ is not None else os.environ
        self._service = service

    async def execute(self, command: DeleteCredentialCommand) -> None:
        if command.var_name not in self._allowed:
            from qai.platform.errors import ValidationError

            raise ValidationError(
                code="ai_coding.unknown_credential_var",
                message=(
                    f"unknown credential variable: {command.var_name!r}; "
                    f"must be one of {sorted(self._allowed)}"
                ),
                field_errors={"var_name": [command.var_name]},
            )
        # Idempotent: deleting a non-existent secret should NOT raise.
        # The platform :class:`SecretStore.delete` raises
        # ``NotFoundError`` on absent keys; we swallow that since the
        # post-condition (``not exists``) holds either way.
        if self._secret_store.exists(self._service, command.var_name):
            self._secret_store.delete(self._service, command.var_name)
        self._environ.pop(command.var_name, None)
