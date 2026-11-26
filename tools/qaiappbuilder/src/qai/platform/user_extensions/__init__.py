# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""User extensions: QAIModelBuilder-compatible extension host (stage 1).

Stage 1 loads extensions from ``<data>/extensions/<name>/`` and wires the
chat tools they declare. Design: ``docs/extension-overall-design.md``.
"""

from .host import (
    ExtensionContractError,
    ExtensionStatus,
    RegisteredTool,
    ToolSink,
    UserExtensionContext,
    UserExtensionHost,
    discover_extension_dirs,
)
from .manifest import (
    MANIFEST_FILENAME,
    SUPPORTED_API_VERSION,
    ExtensionManifest,
    ManifestError,
    ToolDeclaration,
    load_manifest,
    parse_manifest,
)

__all__ = [
    "MANIFEST_FILENAME",
    "SUPPORTED_API_VERSION",
    "ExtensionContractError",
    "ExtensionManifest",
    "ExtensionStatus",
    "ManifestError",
    "RegisteredTool",
    "ToolDeclaration",
    "ToolSink",
    "UserExtensionContext",
    "UserExtensionHost",
    "discover_extension_dirs",
    "load_manifest",
    "parse_manifest",
]
