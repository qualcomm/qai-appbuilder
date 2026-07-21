# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use case: register a new channel instance + its credentials.

Wires together :class:`ChannelInstance.create` with credential
persistence via :class:`CredentialsResolverPort` so the secret never
appears in the persisted ``ChannelInstance`` row — only the
:class:`CredentialsRef` does.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.platform.ids import IdGenerator
from qai.platform.time import Clock

from qai.channels.application.ports import (
    ChannelInstanceRepositoryPort,
    CredentialsResolverPort,
)
from qai.channels.domain import (
    ChannelInstance,
    ChannelInstanceId,
    ChannelKind,
    CredentialsRef,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RegisterChannelInstanceCommand:
    """Inbound command for :class:`RegisterChannelInstanceUseCase`.

    ``secret_key_use_instance_id`` (default ``False``) is an opt-in
    that makes the use case ignore ``secret_key`` and use the freshly
    generated :class:`ChannelInstanceId` value as the SecretStore
    record key.  Feishu uses this so the ``credentials_ref`` lands at
    ``(FEISHU_APP_SECRET_SERVICE, instance_id)`` — the SAME single
    namespace the outbound token cache + inbound WebSocket transport
    read from, plus the namespace ``POST /api/feishu/config`` writes
    to.  WeChat keeps the legacy behaviour (``False``) where the
    caller picks the key.
    """

    kind: ChannelKind
    name: str
    secret_service: str
    secret_key: str
    secret_value: str
    metadata: tuple[tuple[str, str], ...] = ()
    secret_key_use_instance_id: bool = False


class RegisterChannelInstanceUseCase:
    """Persist a new :class:`ChannelInstance` and store its secret.

    The secret is stored under the supplied ``CredentialsRef`` *before*
    the instance row is committed so a partial failure leaves no
    dangling row referring to a missing secret.  Adapters layered on
    top of these ports may run the two writes in a single transaction
    if their backend supports it (PR-026).
    """

    def __init__(
        self,
        *,
        instances: ChannelInstanceRepositoryPort,
        credentials: CredentialsResolverPort,
        ids: IdGenerator,
        clock: Clock,
    ) -> None:
        self._instances = instances
        self._credentials = credentials
        self._ids = ids
        self._clock = clock

    async def execute(
        self, command: RegisterChannelInstanceCommand
    ) -> ChannelInstance:
        instance_id = ChannelInstanceId.generate(self._ids)
        # Feishu opts into "use the generated instance_id as the
        # SecretStore record key" so the ref points at the SAME
        # ``(FEISHU_APP_SECRET_SERVICE, instance_id)`` slot used by the
        # outbound token cache, the inbound WebSocket transport, and
        # the ``POST /api/feishu/config`` writer — single namespace,
        # zero divergence between read and write paths.
        secret_key = (
            instance_id.value
            if command.secret_key_use_instance_id
            else command.secret_key
        )
        ref = CredentialsRef(
            service=command.secret_service, key=secret_key
        )
        await self._credentials.store(ref, command.secret_value)
        now = self._clock.now()
        instance = ChannelInstance.create(
            instance_id=instance_id,
            kind=command.kind,
            name=command.name,
            credentials_ref=ref,
            now=now,
            metadata=command.metadata,
        )
        await self._instances.save(instance)
        return instance


__all__ = [
    "RegisterChannelInstanceCommand",
    "RegisterChannelInstanceUseCase",
]
