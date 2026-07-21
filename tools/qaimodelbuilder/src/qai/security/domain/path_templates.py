# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Pure-domain ``${VAR}`` placeholder expansion for path patterns
(U-003b / 6-H11).

V1 truth source
---------------
The legacy backend expanded a fixed set of placeholders in two places:

* ``backend/security/skill_policy.py:77-106`` (``_expand_placeholders``)
  resolved ``${PROJECT_ROOT}`` / ``${TEMP}`` / ``${HOME}`` plus any
  ``${ENV_VAR}`` present in :data:`os.environ` before a skill's
  declared read/write/exec paths were matched.
* ``backend/security/persistent_acl.py:_resolve_template`` (mirrored in
  V2 at ``qai.security.application.use_cases.persistent_acl_admin.``
  ``resolve_path_template``) expanded ``${PROJECT_ROOT}`` /
  ``${APPDATA}`` / ``${LOCALAPPDATA}`` / ``${USERPROFILE}``.

Both reached into process state (``os.getcwd`` / ``os.environ`` /
``tempfile.gettempdir`` / ``Path.home``) at the call site. Reading the
environment is a side effect, and the security ``domain`` layer is
side-effect-free by contract (AGENTS.md §3.2 / §3.5 domain-purity:
``domain`` must not perform I/O). To keep this logic pure and unit-
testable while still living in the domain, the expansion helpers here
take the variable bindings as an explicit ``env`` mapping that the
*caller* (application / adapter layer) populates from
:data:`os.environ` and friends. The domain never touches the process
environment itself.

Public surface
--------------
* :data:`PLACEHOLDER_NAMES` — the canonical placeholder names this
  module recognises (``PROJECT_ROOT`` / ``TEMP`` / ``HOME`` / ``APPDATA``
  / ``WORKSPACE``). ``WORKSPACE`` is the configurable model-builder
  workspace root (historically the hard-coded ``C:/WoS_AI``); its value is
  *not* derived from the environment — the caller injects it from
  configuration (``apps.api._workspace_resolver.resolve_workspace_root``)
  when building the ``env`` mapping, so the domain stays side-effect-free.
* :func:`expand_placeholders` — substitute ``${VAR}`` tokens in a single
  string using the supplied ``env`` mapping.

Behaviour notes (V1 parity):

* Substitution is literal token replacement (``str.replace``), matching
  the V1 loop; it is *not* anchored, so a token can appear anywhere in
  the string.
* Unknown ``${...}`` tokens with no binding in ``env`` are left
  untouched (V1 left non-substituted tokens in place when the env var
  was absent; the persistent-acl variant collapsed *known* vars to ``""``
  but only for its four fixed names — here the caller controls the
  mapping, so "absent key" == "leave token verbatim").
* Non-string input is returned unchanged (defensive parity with V1's
  ``if not isinstance(raw, str): return raw``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping

__all__ = [
    "PLACEHOLDER_NAMES",
    "expand_placeholders",
]

# The canonical placeholder names V1 recognised across its skill
# policy + persistent-acl loaders, plus the V2 ``WORKSPACE`` root.
# ``HOME`` is the V1 alias for the user profile directory
# (``Path.home()`` / ``%USERPROFILE%``); callers map it accordingly when
# building the ``env`` argument. ``WORKSPACE`` is the configurable
# model-builder workspace root — it is *not* an env var; the caller
# injects its resolved value from configuration.
PLACEHOLDER_NAMES: tuple[str, ...] = (
    "PROJECT_ROOT",
    "TEMP",
    "HOME",
    "APPDATA",
    "WORKSPACE",
)

# Matches a ``${NAME}`` token where NAME is a typical env-var identifier
# (letters / digits / underscore). Anchoring to this charset avoids
# treating an accidental ``${`` in a path as a malformed token.
_TOKEN_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_placeholders(
    raw: str,
    env: Mapping[str, str],
) -> str:
    """Return ``raw`` with every ``${VAR}`` token replaced from ``env``.

    ``env`` carries the variable bindings the caller resolved from the
    process environment (``os.environ`` / ``os.getcwd`` /
    ``tempfile.gettempdir`` / ``Path.home``). The domain layer never
    reads those itself — keeping this function pure (no I/O, no global
    state) satisfies the security ``domain`` side-effect-free contract.

    A token whose name is absent from ``env`` is left verbatim (V1
    parity: an unresolved placeholder stays in the string rather than
    collapsing to an empty path that could match unintended roots).

    Non-string input is returned unchanged.
    """
    if not isinstance(raw, str):
        return raw  # type: ignore[unreachable]

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        value = env.get(name)
        if value is None:
            return match.group(0)
        return value

    return _TOKEN_RE.sub(_sub, raw)
