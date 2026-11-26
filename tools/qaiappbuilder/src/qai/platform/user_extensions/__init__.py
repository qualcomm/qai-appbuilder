# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""User extensions: QAIModelBuilder-compatible extension host (stage 1).

Stage 1 loads extensions from ``<data>/extensions/<name>/`` and wires the
chat tools they declare and the skills they ship. Design:
``docs/extension-overall-design.md``.
"""

from .host import (
    ExtensionContractError,
    ExtensionSkillRoot,
    ExtensionStatus,
    RegisteredTool,
    ToolSink,
    UserExtensionContext,
    UserExtensionHost,
    discover_extension_dirs,
)
from .manifest import (
    DEFAULT_SKILLS_DIR,
    HOST_EDITION,
    MANIFEST_FILENAME,
    SUPPORTED_API_VERSION,
    ExtensionManifest,
    ManifestError,
    SkillsDeclaration,
    ToolDeclaration,
    load_manifest,
    parse_manifest,
)

__all__ = [
    "DEFAULT_SKILLS_DIR",
    "HOST_EDITION",
    "MANIFEST_FILENAME",
    "SUPPORTED_API_VERSION",
    "ExtensionContractError",
    "ExtensionManifest",
    "ExtensionSkillRoot",
    "ExtensionStatus",
    "ManifestError",
    "RegisteredTool",
    "SkillsDeclaration",
    "ToolDeclaration",
    "ToolSink",
    "UserExtensionContext",
    "UserExtensionHost",
    "discover_extension_dirs",
    "load_manifest",
    "parse_manifest",
]
