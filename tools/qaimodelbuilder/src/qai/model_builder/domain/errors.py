# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain errors raised by ``qai.model_builder.application`` use cases.

All exceptions inherit a single base so callers (HTTP route + bridge)
can catch the whole hierarchy when surfacing them as ``QaiError``
envelopes.
"""

from __future__ import annotations

__all__ = [
    "ModelBuilderError",
    "WorkspaceNotReadyError",
    "MissingContextBinError",
    "InvalidPrecisionError",
    "ManifestGenerationError",
    "MissingQaiAppBuilderError",
    "SmokeTestFailedError",
]


class ModelBuilderError(Exception):
    """Base class for all domain errors in this context.

    The optional ``code`` slot lets adapters / routes surface a stable
    machine-readable error tag without having to ``isinstance``-check
    every subclass.
    """

    code: str = "model_builder.error"


class WorkspaceNotReadyError(ModelBuilderError):
    """The workspace directory is missing required artefacts.

    Raised by :class:`WorkspaceReaderPort` adapters when the model
    workspace lacks ``output/``, ``plan.md`` / ``qai_plan.md``, or
    cannot be located under the configured WoS_AI root.
    """

    code = "model_builder.workspace_not_ready"


class MissingContextBinError(ModelBuilderError):
    """The default precision has no usable ``.bin`` under ``output/``.

    The ``ExportPackUseCase`` raises this when the requested
    ``default_precision`` does not resolve to a context binary that is
    both present and at least :data:`MIN_CONTEXT_BIN_SIZE` bytes.
    """

    code = "model_builder.missing_context_bin"


class InvalidPrecisionError(ModelBuilderError):
    """A precision token is not in the supported allow-list.

    The router-level allow-list is sourced from
    :data:`qai.model_builder.domain.value_objects.Precision.ALL_LABELS`
    plus their plan-form aliases.
    """

    code = "model_builder.invalid_precision"


class ManifestGenerationError(ModelBuilderError):
    """``manifest.json`` could not be assembled.

    Surfaces structural failures: missing taxonomy, malformed
    ``inference_manifest.json``, or contract extraction blowing up
    with a non-recoverable native error.
    """

    code = "model_builder.manifest_generation_failed"


class MissingQaiAppBuilderError(ModelBuilderError):
    """The ``qai_appbuilder`` runtime is not importable on this host.

    The export pipeline cross-checks each ``.bin`` against the live
    native API (single source of truth for shape / dtype). When the
    runtime is unavailable we refuse to ship an unvalidated Pack —
    parity with the legacy ``features/model-builder/scripts/qai_pack_export.py``
    behaviour (``SystemExit`` from
    :func:`_extract_and_smoke_test_contract`).
    """

    code = "model_builder.qai_appbuilder_missing"


class SmokeTestFailedError(ModelBuilderError):
    """Zero-tensor inference of the context binary failed.

    The ``.bin`` loaded but invocation crashed; the legacy script
    treats this as a hard abort (the Pack would crash AppBuilder at
    first run). New code surfaces the same hard abort to the caller.
    """

    code = "model_builder.smoke_test_failed"
