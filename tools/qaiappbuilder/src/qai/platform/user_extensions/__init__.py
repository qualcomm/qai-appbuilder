# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""User extensions: appbuilder's own extension host.

Loads extensions from ``<data>/extensions/<name>/``: the chat tools they
register from ``activate(ctx)`` and the skills they ship. Design:
``docs/extension-overall-design.md`` §10.0 (the extension API).
"""

from .host import (
    CONFIG_FILENAME,
    DATA_DIRNAME,
    ExtensionContext,
    ExtensionError,
    ExtensionSecrets,
    ExtensionSkillRoot,
    ExtensionStatus,
    RegisteredTool,
    SecretStoreLike,
    ToolCall,
    ToolSink,
    UnknownExtensionError,
    UserExtensionHost,
    discover_extension_dirs,
    module_name_for,
)
from .manifest import (
    DEFAULT_SKILLS_DIR,
    MANIFEST_FILENAME,
    MANIFEST_VERSION,
    ExtensionManifest,
    ManifestError,
    SkillsDeclaration,
    ToolDeclaration,
    load_manifest,
    parse_manifest,
)

__all__ = [
    "CONFIG_FILENAME",
    "DATA_DIRNAME",
    "DEFAULT_SKILLS_DIR",
    "MANIFEST_FILENAME",
    "MANIFEST_VERSION",
    "ExtensionContext",
    "ExtensionError",
    "ExtensionManifest",
    "ExtensionSecrets",
    "ExtensionSkillRoot",
    "ExtensionStatus",
    "ManifestError",
    "RegisteredTool",
    "SecretStoreLike",
    "SkillsDeclaration",
    "ToolCall",
    "ToolDeclaration",
    "ToolSink",
    "UnknownExtensionError",
    "UserExtensionHost",
    "discover_extension_dirs",
    "load_manifest",
    "module_name_for",
    "parse_manifest",
]
