# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Registry helpers for the kind-keyed channels infrastructure (PR-047).

The channels application layer treats transports / signature verifiers
/ payload parsers as **single ports with multiple kind-keyed adapters**.
At wiring time we pick exactly one adapter per :class:`ChannelKind`
and register them in a :class:`_KindRegistry`.

Use cases call:

* :class:`ChannelTransportPort` via a ``transport_factory(instance) ->
  ChannelTransportPort`` closure that delegates to the transport
  registry.
* :class:`WebhookSignatureVerifierPort` / :class:`WebhookPayloadParserPort`
  directly — the verifier / parser instances expose a ``verify(kind,
  ...)`` / ``parse(kind, ...)`` shape, so we need an aggregator that
  receives the call and forwards to the right kind-keyed instance.

The aggregators (:class:`KindDispatchedSignatureVerifier`,
:class:`KindDispatchedPayloadParser`) implement the corresponding
ports so the channels use cases see a single object.

This module lives under ``adapters/`` because it's pure composition —
no IO, no third-party deps, no httpx; the *real* IO happens inside
the per-kind adapter classes registered into it.
"""

from __future__ import annotations

from typing import Mapping, TYPE_CHECKING

from qai.channels.application.ports import (
    ChannelTransportPort,
    WebhookPayloadParserPort,
    WebhookSignatureVerifierPort,
)
from qai.channels.domain import (
    ChannelInstance,
    ChannelKind,
    ChannelKindNotSupportedError,
    WebhookPayload,
)

if TYPE_CHECKING:  # pragma: no cover
    pass

__all__ = [
    "ChannelTransportRegistry",
    "KindDispatchedSignatureVerifier",
    "KindDispatchedPayloadParser",
]


class ChannelTransportRegistry:
    """Kind-keyed registry of :class:`ChannelTransportPort` adapters.

    Exposes both a callable interface (``registry(kind) -> transport``)
    used by :class:`OutboundReplyDispatcher` / lifecycle use cases and
    a ``transports`` mapping for tests + route ``GET /status`` reads.
    """

    __slots__ = ("transports",)

    def __init__(
        self, transports: Mapping[ChannelKind, ChannelTransportPort]
    ) -> None:
        # Snapshot to a regular dict so callers can't mutate at
        # runtime — wiring happens once at composition root.
        self.transports: dict[ChannelKind, ChannelTransportPort] = dict(
            transports
        )

    def __call__(self, kind: ChannelKind) -> ChannelTransportPort:
        try:
            return self.transports[kind]
        except KeyError as exc:
            raise ChannelKindNotSupportedError(kind.value) from exc

    def for_instance(
        self, instance: ChannelInstance
    ) -> ChannelTransportPort:
        return self(instance.kind)


class KindDispatchedSignatureVerifier:
    """:class:`WebhookSignatureVerifierPort` aggregator.

    Holds one verifier per :class:`ChannelKind`; ``verify(kind, ...)``
    routes the call to the matching verifier or raises
    :class:`ChannelKindNotSupportedError` when no verifier is wired.
    """

    __slots__ = ("_verifiers",)

    def __init__(
        self,
        verifiers: Mapping[ChannelKind, WebhookSignatureVerifierPort],
    ) -> None:
        self._verifiers: dict[
            ChannelKind, WebhookSignatureVerifierPort
        ] = dict(verifiers)

    def verify(
        self,
        kind: ChannelKind,
        raw_body: bytes,
        headers: dict[str, str],
        *,
        instance_id: str | None = None,
    ) -> None:
        try:
            verifier = self._verifiers[kind]
        except KeyError as exc:
            raise ChannelKindNotSupportedError(kind.value) from exc
        # PR-201: forward instance_id when available so per-instance
        # secret resolvers can pick the correct signing secret.
        # Legacy verifier impls that ignore the kw-arg remain
        # contract-compliant per the additive Protocol change.
        verifier.verify(
            kind, raw_body, headers, instance_id=instance_id
        )


class KindDispatchedPayloadParser:
    """:class:`WebhookPayloadParserPort` aggregator.

    Mirror of :class:`KindDispatchedSignatureVerifier` for parsers.
    """

    __slots__ = ("_parsers",)

    def __init__(
        self, parsers: Mapping[ChannelKind, WebhookPayloadParserPort]
    ) -> None:
        self._parsers: dict[ChannelKind, WebhookPayloadParserPort] = dict(
            parsers
        )

    def parse(
        self,
        kind: ChannelKind,
        raw_body: bytes,
        headers: dict[str, str],
        *,
        instance_id: str | None = None,
    ) -> WebhookPayload:
        try:
            parser = self._parsers[kind]
        except KeyError as exc:
            raise ChannelKindNotSupportedError(kind.value) from exc
        # PR-097: forward instance_id so per-instance parsers can
        # resolve the right crypto material.
        # Legacy parser impls that ignore the kw-arg remain
        # contract-compliant per the additive Protocol change.
        return parser.parse(
            kind, raw_body, headers, instance_id=instance_id
        )
