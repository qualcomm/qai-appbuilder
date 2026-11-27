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
    ActionCall,
    ActionResult,
    ActionValidationError,
    BuiltinExtensionError,
    ExtensionContext,
    ExtensionError,
    ExtensionSecrets,
    ExtensionSkillRoot,
    ExtensionStatus,
    PromptFragment,
    RegisteredTool,
    RouteRequest,
    RouteResponse,
    SecretStoreLike,
    ToolCall,
    ToolSink,
    UnknownExtensionError,
    UserExtensionHost,
    call_handler,
    discover_extension_dirs,
    module_name_for,
    run_action,
)
from .installer import ExtensionInstaller, InstallError, StagedExtension
from .manifest import (
    DEFAULT_SKILLS_DIR,
    MANIFEST_FILENAME,
    MANIFEST_VERSION,
    PROMPT_MAX_CHARS,
    PROMPT_WHEN,
    ExtensionManifest,
    ManifestError,
    PromptDeclaration,
    SettingDeclaration,
    SkillsDeclaration,
    ToolDeclaration,
    load_manifest,
    parse_manifest,
)
from .settings import SettingsValidationError, settings_view, update_settings
from .state import STATE_FILENAME, ExtensionStateStore

__all__ = [
    "CONFIG_FILENAME",
    "DATA_DIRNAME",
    "DEFAULT_SKILLS_DIR",
    "MANIFEST_FILENAME",
    "MANIFEST_VERSION",
    "PROMPT_MAX_CHARS",
    "PROMPT_WHEN",
    "STATE_FILENAME",
    "ActionCall",
    "ActionResult",
    "ActionValidationError",
    "BuiltinExtensionError",
    "ExtensionContext",
    "ExtensionError",
    "ExtensionInstaller",
    "ExtensionManifest",
    "ExtensionSecrets",
    "ExtensionSkillRoot",
    "ExtensionStateStore",
    "ExtensionStatus",
    "InstallError",
    "ManifestError",
    "PromptDeclaration",
    "PromptFragment",
    "RegisteredTool",
    "RouteRequest",
    "RouteResponse",
    "SecretStoreLike",
    "SettingDeclaration",
    "SettingsValidationError",
    "SkillsDeclaration",
    "StagedExtension",
    "ToolCall",
    "ToolDeclaration",
    "ToolSink",
    "UnknownExtensionError",
    "UserExtensionHost",
    "call_handler",
    "discover_extension_dirs",
    "load_manifest",
    "module_name_for",
    "parse_manifest",
    "run_action",
    "settings_view",
    "update_settings",
]
