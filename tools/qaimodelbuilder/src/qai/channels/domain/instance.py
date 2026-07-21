# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""ChannelInstance aggregate root.

A :class:`ChannelInstance` represents one configured Feishu / WeChat
bot owned by a user.  It is the unified replacement for the
legacy ``feishu_channel.py`` / ``wechat_channel.py`` facades and their
SCC sub-packages — both providers share **one** aggregate
discriminated by :class:`~qai.channels.domain.kinds.ChannelKind`.

Invariants enforced here:

* ``credentials_ref`` is always a :class:`CredentialsRef` value object;
  the aggregate **never** stores plaintext password / token / cookie.
  Resolution to plaintext is the application layer's job via
  :class:`~qai.channels.application.ports.CredentialsResolverPort`.
* ``status`` transitions through a strict state machine; illegal
  transitions raise :class:`~qai.channels.domain.errors.ChannelInstanceStateError`.
* All mutating methods return a *new* instance (the aggregate is
  ``frozen=True``).  This is the same pattern used by the chat
  context's ``ConversationTab`` aggregate (PR-021) and keeps domain
  logic side-effect-free.
* Timestamps are tz-aware UTC (``ensure_aware_utc``).
* No module-level state, no monkey-patching, no third-party imports
  beyond :mod:`qai.platform`.

PR-202 — settings & bindings
----------------------------

The user-managed configuration (auto-start / proxy / default model /
kind-specific app_id / WebUI binding map) is stored as JSON-encoded
strings inside the existing ``metadata`` tuple under two reserved
keys: :data:`_SETTINGS_KEY` and :data:`_BINDINGS_KEY`.  The repository
layer (``channel_instance_repository``) already serialises the whole
``metadata`` tuple as ``metadata_json``, so PR-202 needs **no new
migration**.

The :meth:`get_settings` / :meth:`with_settings` (and the bindings
mirrors) are the ONLY supported mutation paths — they preserve every
non-reserved metadata key untouched, so legacy free-form metadata
written by the original ``register`` use case continues to round-trip.

Lifecycle::

    stopped --start_requested--> starting
    starting --transport_running--> running
    starting --transport_failed--> error
    running --stop_requested--> stopping
    running --transport_failed--> error
    stopping --transport_stopped--> stopped
    error --acknowledge--> stopped
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime

from qai.platform.io_validator import (
    assert_max_length,
    assert_non_empty,
)
from qai.platform.time import ensure_aware_utc

from .errors import (
    ChannelInstanceAlreadyRunningError,
    ChannelInstanceStateError,
)
from .ids import ChannelInstanceId
from .kinds import ChannelKind
from .value_objects import (
    ChannelBindings,
    ChannelModelConfig,
    ChannelProxyConfig,
    ChannelSettings,
    ChannelStatus,
    CredentialsRef,
)

_MAX_NAME_LENGTH = 256
_MAX_REASON_LENGTH = 1024

#: Reserved metadata key for the JSON-encoded :class:`ChannelSettings` blob.
_SETTINGS_KEY = "settings_v1"

#: Reserved metadata key for the JSON-encoded :class:`ChannelBindings` blob.
_BINDINGS_KEY = "bindings_v1"


def _default_settings_for(kind: ChannelKind) -> ChannelSettings:
    """Return the factory-default :class:`ChannelSettings` for ``kind``.

    Encodes the V1 out-of-the-box product defaults so a freshly
    registered instance (one that has never persisted ``settings_v1``)
    matches V1's user-perceived behaviour:

    * **WeChat** — ``auto_start=True`` (V1 ``forge_config.json``
      ``wechat_channel.auto_connect: true``: personal WeChat auto-connects
      on service start).
    * **Feishu / everything else** — ``auto_start=False`` (V1 has no
      ``auto_connect`` default for Feishu).

    This is a domain rule (a per-kind factory default), kept framework-free
    in the domain layer. The generic VO default
    (:attr:`ChannelSettings.auto_start` = ``False``) is intentionally left
    unchanged so Feishu is never affected. Once a user saves settings the
    persisted ``settings_v1`` value wins, so this is a factory default that
    users can override — exactly V1's semantics.
    """
    return ChannelSettings(auto_start=(kind is ChannelKind.WECHAT))


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelInstance:
    """Aggregate root for a single channel deployment.

    Construct via :meth:`create` for a fresh instance, or via
    :meth:`rehydrate` when loading from persistence.
    """

    instance_id: ChannelInstanceId
    kind: ChannelKind
    name: str
    credentials_ref: CredentialsRef
    status: ChannelStatus = ChannelStatus.STOPPED
    created_at: datetime
    updated_at: datetime
    last_error: str = ""
    metadata: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def __post_init__(self) -> None:
        assert_non_empty(self.name, name="ChannelInstance.name")
        assert_max_length(
            self.name,
            max_length=_MAX_NAME_LENGTH,
            name="ChannelInstance.name",
        )
        assert_max_length(
            self.last_error,
            max_length=_MAX_REASON_LENGTH,
            name="ChannelInstance.last_error",
        )
        # Normalise tz on read-only timestamps.
        for attr in ("created_at", "updated_at"):
            current = getattr(self, attr)
            normalised = ensure_aware_utc(current)
            if normalised is not current:
                object.__setattr__(self, attr, normalised)

    @classmethod
    def create(
        cls,
        *,
        instance_id: ChannelInstanceId,
        kind: ChannelKind,
        name: str,
        credentials_ref: CredentialsRef,
        now: datetime,
        metadata: tuple[tuple[str, str], ...] = (),
    ) -> ChannelInstance:
        """Factory for a brand-new (``stopped``) instance."""

        return cls(
            instance_id=instance_id,
            kind=kind,
            name=name,
            credentials_ref=credentials_ref,
            status=ChannelStatus.STOPPED,
            created_at=now,
            updated_at=now,
            last_error="",
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------
    def request_start(self, *, now: datetime) -> ChannelInstance:
        """Move from ``stopped`` → ``starting``.

        Raises:
            ChannelInstanceAlreadyRunningError: if currently ``starting``
                / ``running`` / ``stopping``.
            ChannelInstanceStateError: if currently ``error``.
        """

        if self.status in (
            ChannelStatus.STARTING,
            ChannelStatus.RUNNING,
            ChannelStatus.STOPPING,
        ):
            raise ChannelInstanceAlreadyRunningError(
                self.instance_id.value,
                current_status=self.status.value,
            )
        if self.status is ChannelStatus.ERROR:
            raise ChannelInstanceStateError(
                f"channel instance {self.instance_id.value!r} is in error state; "
                "must be acknowledged before restart",
                current_status=self.status.value,
                attempted="request_start",
            )
        return replace(
            self,
            status=ChannelStatus.STARTING,
            updated_at=ensure_aware_utc(now),
            last_error="",
        )

    def mark_running(self, *, now: datetime) -> ChannelInstance:
        """Move from ``starting`` → ``running``."""

        if self.status is not ChannelStatus.STARTING:
            raise ChannelInstanceStateError(
                f"cannot mark instance {self.instance_id.value!r} running "
                f"from status {self.status.value!r}",
                current_status=self.status.value,
                attempted="mark_running",
            )
        return replace(
            self,
            status=ChannelStatus.RUNNING,
            updated_at=ensure_aware_utc(now),
            last_error="",
        )

    def request_stop(self, *, now: datetime) -> ChannelInstance:
        """Move from ``running`` → ``stopping``."""

        if self.status is not ChannelStatus.RUNNING:
            raise ChannelInstanceStateError(
                f"cannot stop instance {self.instance_id.value!r} "
                f"from status {self.status.value!r}",
                current_status=self.status.value,
                attempted="request_stop",
            )
        return replace(
            self,
            status=ChannelStatus.STOPPING,
            updated_at=ensure_aware_utc(now),
        )

    def mark_stopped(self, *, now: datetime) -> ChannelInstance:
        """Move from ``stopping`` → ``stopped``."""

        if self.status is not ChannelStatus.STOPPING:
            raise ChannelInstanceStateError(
                f"cannot mark instance {self.instance_id.value!r} stopped "
                f"from status {self.status.value!r}",
                current_status=self.status.value,
                attempted="mark_stopped",
            )
        return replace(
            self,
            status=ChannelStatus.STOPPED,
            updated_at=ensure_aware_utc(now),
            last_error="",
        )

    def mark_error(self, *, now: datetime, reason: str) -> ChannelInstance:
        """Move into ``error`` state from ``starting`` / ``running`` /
        ``stopping``.

        ``reason`` is a short human-readable string preserved for the
        UI; structured details should be logged separately.
        """

        assert_non_empty(reason, name="ChannelInstance.reason")
        assert_max_length(
            reason,
            max_length=_MAX_REASON_LENGTH,
            name="ChannelInstance.reason",
        )
        if self.status not in (
            ChannelStatus.STARTING,
            ChannelStatus.RUNNING,
            ChannelStatus.STOPPING,
        ):
            raise ChannelInstanceStateError(
                f"cannot transition instance {self.instance_id.value!r} "
                f"to error from status {self.status.value!r}",
                current_status=self.status.value,
                attempted="mark_error",
            )
        return replace(
            self,
            status=ChannelStatus.ERROR,
            updated_at=ensure_aware_utc(now),
            last_error=reason,
        )

    def acknowledge_error(self, *, now: datetime) -> ChannelInstance:
        """Move from ``error`` → ``stopped``, clearing ``last_error``."""

        if self.status is not ChannelStatus.ERROR:
            raise ChannelInstanceStateError(
                f"cannot acknowledge non-error instance "
                f"{self.instance_id.value!r} (status={self.status.value!r})",
                current_status=self.status.value,
                attempted="acknowledge_error",
            )
        return replace(
            self,
            status=ChannelStatus.STOPPED,
            updated_at=ensure_aware_utc(now),
            last_error="",
        )

    def reset_to_stopped(self, *, now: datetime) -> ChannelInstance:
        """Force any *active* (non-error) status back to ``stopped``.

        **Process-boundary reset** — NOT part of the normal start/stop state
        machine.  When the API process restarts, every in-memory transport
        (Feishu WS client / WeChat long-poll bot) is gone, so any persisted
        ``running`` / ``starting`` / ``stopping`` status is necessarily
        **stale and untrue** (State-Truth-First 铁律1: the persisted status no
        longer reflects a real live connection).  The lifespan startup calls
        this on boot — before channel auto-start — so the DB truth matches V1
        / v0.5 semantics where the connection status is an in-memory global
        that is always ``stopped`` after a restart until a real
        ``start_channel`` reconnects (V1 ``feishu_channel.py`` / ``wechat_
        channel.py`` ``_status`` is never persisted).

        Idempotent and safe for the already-terminal states:

        * ``running`` / ``starting`` / ``stopping`` → ``stopped`` (the reset).
        * ``stopped`` → returns ``self`` unchanged (no spurious write / event).
        * ``error`` → returns ``self`` unchanged: ERROR requires an explicit
          user acknowledge before it can leave the error state
          (:meth:`acknowledge_error`); auto-restart of a known-broken channel
          is intentionally avoided (mirrors the auto-start ERROR skip), so a
          boot-time reset must NOT silently clear an error the user has not
          seen.

        Normal start/stop flows keep using ``request_start`` / ``mark_running``
        / ``request_stop`` / ``mark_stopped`` / ``mark_error`` — this method is
        only for the process-boundary recovery path.
        """
        if self.status in (
            ChannelStatus.STOPPED,
            ChannelStatus.ERROR,
        ):
            return self
        return replace(
            self,
            status=ChannelStatus.STOPPED,
            updated_at=ensure_aware_utc(now),
            last_error="",
        )


    # ------------------------------------------------------------------
    # Predicates
    # ------------------------------------------------------------------
    def is_running(self) -> bool:
        return self.status is ChannelStatus.RUNNING

    def is_active(self) -> bool:
        """``True`` while the transport is or *should be* live."""

        return self.status in (
            ChannelStatus.STARTING,
            ChannelStatus.RUNNING,
            ChannelStatus.STOPPING,
        )

    # ------------------------------------------------------------------
    # Settings & bindings (PR-202)
    # ------------------------------------------------------------------
    def get_settings(self) -> ChannelSettings:
        """Return the persisted :class:`ChannelSettings` for this instance.

        When the instance has never persisted ``settings_v1`` (freshly
        registered, or registered before PR-202), fall back to the
        per-kind factory defaults (:func:`_default_settings_for`) so the
        user-perceived defaults match V1 (WeChat auto-connects on boot,
        Feishu does not). A persisted ``settings_v1`` always wins.
        """
        for k, v in self.metadata:
            if k == _SETTINGS_KEY:
                return _decode_settings(v)
        return _default_settings_for(self.kind)

    def with_settings(
        self, settings: ChannelSettings, *, now: datetime
    ) -> "ChannelInstance":
        """Return a new instance with ``settings`` persisted into metadata.

        Preserves every non-reserved metadata key untouched.
        """
        encoded = _encode_settings(settings)
        new_metadata = _replace_metadata_key(
            self.metadata, _SETTINGS_KEY, encoded
        )
        return replace(
            self,
            metadata=new_metadata,
            updated_at=ensure_aware_utc(now),
        )

    def get_bindings(self) -> ChannelBindings:
        """Return the persisted :class:`ChannelBindings` for this instance."""
        for k, v in self.metadata:
            if k == _BINDINGS_KEY:
                return _decode_bindings(v)
        return ChannelBindings()

    def with_bindings(
        self, bindings: ChannelBindings, *, now: datetime
    ) -> "ChannelInstance":
        """Return a new instance with ``bindings`` persisted into metadata."""
        encoded = _encode_bindings(bindings)
        new_metadata = _replace_metadata_key(
            self.metadata, _BINDINGS_KEY, encoded
        )
        return replace(
            self,
            metadata=new_metadata,
            updated_at=ensure_aware_utc(now),
        )


# ---------------------------------------------------------------------------
# Settings / bindings JSON helpers (module-private)
# ---------------------------------------------------------------------------


def _replace_metadata_key(
    metadata: tuple[tuple[str, str], ...],
    key: str,
    value: str,
) -> tuple[tuple[str, str], ...]:
    """Return a new metadata tuple with ``key`` set to ``value``.

    Order: every non-matching entry first (preserving original order),
    then the new ``(key, value)`` pair appended.  This guarantees a
    stable equality semantic regardless of whether the key existed.
    """
    kept = tuple((k, v) for k, v in metadata if k != key)
    return kept + ((key, value),)


def _encode_settings(settings: ChannelSettings) -> str:
    return json.dumps(
        {
            "auto_start": bool(settings.auto_start),
            "proxy": {
                "url": settings.proxy.url,
                "username": settings.proxy.username,
                "has_password": bool(settings.proxy.has_password),
            },
            "model": {
                "model_id": settings.model.model_id,
                "model_provider": settings.model.model_provider,
            },
            "kind_specific": {k: v for k, v in settings.kind_specific},
            "has_app_secret": bool(settings.has_app_secret),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _decode_settings(blob: str) -> ChannelSettings:
    if not blob:
        return ChannelSettings()
    try:
        decoded = json.loads(blob)
    except (TypeError, ValueError):
        return ChannelSettings()
    if not isinstance(decoded, dict):
        return ChannelSettings()
    proxy_raw = decoded.get("proxy") or {}
    model_raw = decoded.get("model") or {}
    kind_specific_raw = decoded.get("kind_specific") or {}
    return ChannelSettings(
        auto_start=bool(decoded.get("auto_start", False)),
        proxy=ChannelProxyConfig(
            url=str(proxy_raw.get("url", "")),
            username=str(proxy_raw.get("username", "")),
            has_password=bool(proxy_raw.get("has_password", False)),
        ),
        model=ChannelModelConfig(
            model_id=str(model_raw.get("model_id", "")),
            model_provider=str(model_raw.get("model_provider", "")),
        ),
        kind_specific=tuple(
            (str(k), str(v))
            for k, v in (kind_specific_raw.items() if isinstance(kind_specific_raw, dict) else ())
        ),
        # Backward compatible: legacy blobs without the key default to
        # ``False`` (no app_secret recorded) so pre-existing instances
        # are never reported as having a saved Feishu secret.
        has_app_secret=bool(decoded.get("has_app_secret", False)),
    )


def _encode_bindings(bindings: ChannelBindings) -> str:
    return json.dumps(
        {c: u for c, u in bindings.entries},
        ensure_ascii=False,
        sort_keys=True,
    )


def _decode_bindings(blob: str) -> ChannelBindings:
    if not blob:
        return ChannelBindings()
    try:
        decoded = json.loads(blob)
    except (TypeError, ValueError):
        return ChannelBindings()
    if not isinstance(decoded, dict):
        return ChannelBindings()
    entries = tuple(
        (str(c), str(u))
        for c, u in decoded.items()
        if isinstance(c, str) and isinstance(u, str)
    )
    return ChannelBindings(entries=entries)


__all__ = ["ChannelInstance"]
