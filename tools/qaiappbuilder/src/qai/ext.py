# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""The public SDK for appbuilder user extensions.

This is the ONLY module an extension may import from appbuilder::

    from qai.ext import ExtensionContext, ExtensionError, ToolCall

Everything else under ``qai.*`` (and ``apps.*`` / ``interfaces.*``) is
internal and may change in any release. An extension gets everything else it
needs from the ``ctx`` passed to ``activate(ctx)`` and the ``call`` passed to
its tool handlers; this module only gives those objects importable names, for
type annotations and for catching :class:`ExtensionError`.

The exported names are part of the extension API (design doc §4): renaming
one, or removing / changing a documented member, is a breaking change that
follows the versioning rule of design doc §4.8.

Importing this module loads only the stdlib-only extension host, never the
chat or application layers.
"""

from __future__ import annotations

from qai.platform.user_extensions.host import (
    ExtensionContext,
    ExtensionError,
    ExtensionSecrets,
    ToolCall,
)

__all__ = [
    "ExtensionContext",
    "ExtensionError",
    "ExtensionSecrets",
    "ToolCall",
]
