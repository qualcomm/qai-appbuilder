# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases: read / mutate per-instance :class:`ChannelSettings` (PR-202).

The four use cases here are the application-layer counterparts of the
12 legacy ``GET/POST /api/{wechat,feishu}/{config,proxy,model}`` HTTP
endpoints.  They all follow the same minimal pattern:

1. Load the aggregate from :class:`ChannelInstanceRepositoryPort.get` —
   :class:`ChannelInstanceNotFoundError` propagates naturally up to the
   route layer's error envelope when the id is unknown.
2. Compute a new :class:`ChannelSettings` via :func:`dataclasses.replace`,
   preserving every unrelated field of the existing settings (so an
   :class:`UpdateChannelProxyUseCase` call never accidentally clears
   :class:`ChannelModelConfig`).
3. Persist via :meth:`ChannelInstance.with_settings` (which serialises
   the blob into the existing ``metadata`` tuple under
   ``settings_v1`` — no new migration required).

Plaintext credentials never reach this layer.  The route layer writes
the proxy password to the SecretStore directly and only hands the
``has_password`` boolean to :class:`UpdateChannelProxyUseCase`.

No domain events are published — the existing event catalogue
(:mod:`qai.channels.domain.events`) does not declare a
``ChannelSettingsUpdatedEvent`` and PR-202 deliberately keeps the
event surface frozen.  The audit trail for settings mutations rides
on the platform's structured-log records emitted by the route layer
(``http.channels.settings_updated``); domain events are reserved for
state changes that other contexts must react to, which settings
mutations are not.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from qai.platform.time import Clock

from qai.channels.application.ports import ChannelInstanceRepositoryPort
from qai.channels.domain import (
    ChannelInstance,
    ChannelInstanceId,
    ChannelModelConfig,
    ChannelProxyConfig,
    ChannelSettings,
)


# ---------------------------------------------------------------------------
# Read: ChannelSettings
# ---------------------------------------------------------------------------


class GetChannelSettingsUseCase:
    """Return the persisted :class:`ChannelSettings` for one instance."""

    def __init__(
        self,
        *,
        instances: ChannelInstanceRepositoryPort,
        clock: Clock,
    ) -> None:
        self._instances = instances
        self._clock = clock  # kept for symmetry with mutators

    async def execute(
        self, instance_id: ChannelInstanceId
    ) -> ChannelSettings:
        instance = await self._instances.get(instance_id)
        return instance.get_settings()


# ---------------------------------------------------------------------------
# Mutate: auto_start + kind_specific (config)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateChannelConfigCommand:
    """Inbound command for :class:`UpdateChannelConfigUseCase`.

    ``kind_specific`` is *merged* into the existing settings: keys
    present in the command overwrite their counterparts on the stored
    aggregate, keys absent from the command are preserved.  This
    mirrors the legacy ``POST /api/{kind}/config`` behaviour where the
    front-end only re-sends fields it intends to change.
    """

    instance_id: ChannelInstanceId
    auto_start: bool
    kind_specific: dict[str, str]


class UpdateChannelConfigUseCase:
    """Update ``auto_start`` + ``kind_specific`` on an instance.

    Preserves the existing :class:`ChannelProxyConfig` /
    :class:`ChannelModelConfig` untouched.
    """

    def __init__(
        self,
        *,
        instances: ChannelInstanceRepositoryPort,
        clock: Clock,
    ) -> None:
        self._instances = instances
        self._clock = clock

    async def execute(
        self, command: UpdateChannelConfigCommand
    ) -> ChannelInstance:
        instance = await self._instances.get(command.instance_id)
        existing = instance.get_settings()
        merged = dict(existing.kind_specific)
        merged.update(command.kind_specific)
        new_settings = replace(
            existing,
            auto_start=command.auto_start,
            kind_specific=tuple(sorted(merged.items())),
        )
        updated = instance.with_settings(
            new_settings, now=self._clock.now()
        )
        await self._instances.save(updated)
        return updated

    async def execute_preserving_secret(
        self,
        *,
        command: UpdateChannelConfigCommand,
        app_secret_present: bool,
    ) -> ChannelInstance:
        """Update config with the "blank app_secret = preserve" rule.

        Symmetric with
        :meth:`UpdateChannelProxyUseCase.execute_preserving_password`
        (R16): encapsulates the empty-secret three-state decision for
        the Feishu ``app_secret`` so the ``POST /api/feishu/config``
        route does not inline it.

        * ``app_secret_present`` is ``True`` when the caller has just
          persisted a fresh Feishu ``app_secret`` into the SecretStore —
          the ``has_app_secret`` flag is set ``True``.
        * ``app_secret_present`` is ``False`` (the UI sent a blank
          ``app_secret`` field meaning "do not change") — the
          *previously persisted* ``has_app_secret`` flag is read back
          from the existing settings and preserved, so an unrelated
          ``app_id`` / ``auto_start`` edit never clears the saved-secret
          indicator.

        Plaintext credentials never reach this layer; the caller writes
        the secret to its store before calling this method and only the
        ``app_secret_present`` boolean crosses the boundary (consistent
        with the proxy-password path and AGENTS.md §3.3).
        """
        instance = await self._instances.get(command.instance_id)
        existing = instance.get_settings()
        if app_secret_present:
            has_app_secret = True
        else:
            has_app_secret = existing.has_app_secret
        merged = dict(existing.kind_specific)
        merged.update(command.kind_specific)
        new_settings = replace(
            existing,
            auto_start=command.auto_start,
            kind_specific=tuple(sorted(merged.items())),
            has_app_secret=has_app_secret,
        )
        updated = instance.with_settings(
            new_settings, now=self._clock.now()
        )
        await self._instances.save(updated)
        return updated


# ---------------------------------------------------------------------------
# Mutate: proxy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateChannelProxyCommand:
    """Inbound command for :class:`UpdateChannelProxyUseCase`.

    The plaintext password is **never** part of this command — the
    route layer writes it to :class:`SecretStore` *before* calling
    ``execute`` and forwards the resulting ``has_password`` bit only.
    """

    instance_id: ChannelInstanceId
    url: str
    username: str
    has_password: bool


class UpdateChannelProxyUseCase:
    """Replace :class:`ChannelProxyConfig` on an instance."""

    def __init__(
        self,
        *,
        instances: ChannelInstanceRepositoryPort,
        clock: Clock,
    ) -> None:
        self._instances = instances
        self._clock = clock

    async def execute(
        self, command: UpdateChannelProxyCommand
    ) -> ChannelInstance:
        instance = await self._instances.get(command.instance_id)
        existing = instance.get_settings()
        new_settings = replace(
            existing,
            proxy=ChannelProxyConfig(
                url=command.url,
                username=command.username,
                has_password=command.has_password,
            ),
        )
        updated = instance.with_settings(
            new_settings, now=self._clock.now()
        )
        await self._instances.save(updated)
        return updated

    async def execute_preserving_password(
        self,
        *,
        instance_id: ChannelInstanceId,
        url: str,
        username: str,
        password_present: bool,
    ) -> ChannelInstance:
        """Update proxy with the legacy "blank password = preserve" rule.

        Encapsulates the empty-password three-state decision that the
        ``POST /api/{kind}/proxy`` route previously inlined (R16):

        * ``password_present`` is ``True`` when the caller has just
          persisted a fresh password (e.g. into the SecretStore) — the
          ``has_password`` flag is set ``True``.
        * ``password_present`` is ``False`` (the UI sent a blank field
          meaning "do not change") — the *previously persisted*
          ``has_password`` flag is read back from the existing settings
          and preserved, so an unrelated url / username edit never
          clears the saved-password indicator.

        Plaintext credentials never reach this layer; the caller writes
        the secret to its store before calling this method and only the
        ``password_present`` boolean crosses the boundary (consistent
        with :class:`UpdateChannelProxyCommand`).
        """
        if password_present:
            has_password = True
        else:
            existing = await self._instances.get(instance_id)
            has_password = existing.get_settings().proxy.has_password
        return await self.execute(
            UpdateChannelProxyCommand(
                instance_id=instance_id,
                url=url,
                username=username,
                has_password=has_password,
            )
        )


# ---------------------------------------------------------------------------
# Mutate: model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateChannelModelCommand:
    """Inbound command for :class:`UpdateChannelModelUseCase`."""

    instance_id: ChannelInstanceId
    model_id: str
    model_provider: str


class UpdateChannelModelUseCase:
    """Replace :class:`ChannelModelConfig` on an instance."""

    def __init__(
        self,
        *,
        instances: ChannelInstanceRepositoryPort,
        clock: Clock,
    ) -> None:
        self._instances = instances
        self._clock = clock

    async def execute(
        self, command: UpdateChannelModelCommand
    ) -> ChannelInstance:
        instance = await self._instances.get(command.instance_id)
        existing = instance.get_settings()
        new_settings = replace(
            existing,
            model=ChannelModelConfig(
                model_id=command.model_id,
                model_provider=command.model_provider,
            ),
        )
        updated = instance.with_settings(
            new_settings, now=self._clock.now()
        )
        await self._instances.save(updated)
        return updated


__all__ = [
    "GetChannelSettingsUseCase",
    "UpdateChannelConfigCommand",
    "UpdateChannelConfigUseCase",
    "UpdateChannelProxyCommand",
    "UpdateChannelProxyUseCase",
    "UpdateChannelModelCommand",
    "UpdateChannelModelUseCase",
]
