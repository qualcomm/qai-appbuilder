# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Child-process environment construction for background processes.

This module is the single project-level source of truth for the env handed
to ``subprocess.Popen`` when spawning a background process via the
``qai.platform.background_process`` adapter. It enforces three invariants
required by the design doc
(``docs/90-refactor/background-process-design.md`` section 1.1 "UTF-8
injection" + section 10 "Credential strip policy") and the project-level
``AGENTS.md`` rules:

1.  **UTF-8 locale injection** (``AGENTS.md`` section 3.10, encoding
    iron-rule). The child must read/write UTF-8 regardless of the parent
    console's code page (Windows PS5.1 is GBK/CP1252 by default), so we
    force ``LC_ALL`` / ``LANG`` / ``LANGUAGE`` / ``PYTHONIOENCODING`` /
    ``PYTHONUTF8``. This prevents the mojibake / U+FFFD class of bugs
    documented in ``AGENTS.md`` section 3.10.

2.  **Credential strip** (``AGENTS.md`` section 3.3 - secrets must flow
    through ``qai.platform.persistence.secrets.SecretStore``, **never**
    via process env). Long-lived API keys, AWS session tokens, channel
    secrets, etc. must not leak into spawned child processes (LLM tool
    invocations, sandboxed user commands, etc.) where they can be read
    via ``os.environ``, dumped by a hostile tool, or end up in process
    listings on multi-user machines. ``build_child_env`` removes every
    name in :data:`CREDENTIAL_ENV_NAMES` plus any name matching
    :data:`SENSITIVE_NAME_PATTERN` (defensive catch-all for unknown
    names like ``MY_VENDOR_API_KEY``).

3.  **Defense-in-depth iron-rule 5** (``AGENTS.md`` section 🔴 rule 5):
    even if a credential slips through because the env var was renamed,
    the regex tail-catch (:data:`SENSITIVE_NAME_PATTERN`) still strips
    it as long as its name contains one of the canonical sensitive
    tokens.

Truth-source pointers for :data:`CREDENTIAL_ENV_NAMES`
------------------------------------------------------

The CC / OC credential name lists below are kept in lock-step with the
canonical lists in the ai_coding context:

* ``src/qai/ai_coding/application/use_cases/manage_coding_credentials.py``
  lines 69-83: ``CC_CREDENTIAL_VARS`` (``ANTHROPIC_API_KEY`` /
  ``ANTHROPIC_AUTH_TOKEN`` / ``ANTHROPIC_BASE_URL`` /
  ``ANTHROPIC_FOUNDRY_API_KEY`` / ``AWS_ACCESS_KEY_ID`` /
  ``AWS_SECRET_ACCESS_KEY`` / ``AWS_SESSION_TOKEN`` /
  ``GOOGLE_APPLICATION_CREDENTIALS`` / ``GOOGLE_CLOUD_PROJECT``) and
  ``OC_CREDENTIAL_VARS`` (``OPENCODE_API_KEY`` / ``OPENCODE_BASE_URL`` /
  ``OPENCODE_USERNAME`` / ``OPENCODE_PASSWORD`` / ``OPENAI_API_KEY``).

We deliberately do **not** ``import`` those constants here: ``platform``
sits below every business context (see ``AGENTS.md`` section 3.2 +
import-linter ``context-isolation`` contract) and importing from
``qai.ai_coding`` would create a reverse dependency that breaks the
layering. Instead this module is the project-level strip list. If
someone later wants a single source of truth, the correct direction is
**ai_coding imports from here**, not the other way around. Keep the
lists in sync manually until that refactor lands.

Server-auth env stripping
-------------------------

The ``KILO_SERVER_PASSWORD`` / ``KILO_SERVER_USERNAME`` /
``KILO_BACKGROUND_PROCESS_PORTS`` names are stripped defensively (in
case a shim carrying those names ever runs in the same env), and
``TERM=dumb`` is injected unconditionally to disable ANSI colour
escapes in child stdout.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

__all__ = [
    "CREDENTIAL_ENV_NAMES",
    "SENSITIVE_NAME_PATTERN",
    "build_child_env",
]


# CC credential vars - mirrors
# src/qai/ai_coding/application/use_cases/manage_coding_credentials.py:69-77
# (CC_CREDENTIAL_VARS). Kept in sync manually; see module docstring.
_CC_CREDENTIAL_VARS: frozenset[str] = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_FOUNDRY_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
    }
)


# OC credential vars - mirrors
# src/qai/ai_coding/application/use_cases/manage_coding_credentials.py:78-83
# (OC_CREDENTIAL_VARS). Kept in sync manually; see module docstring.
_OC_CREDENTIAL_VARS: frozenset[str] = frozenset(
    {
        "OPENCODE_API_KEY",
        "OPENCODE_BASE_URL",
        "OPENCODE_USERNAME",
        "OPENCODE_PASSWORD",
        "OPENAI_API_KEY",
    }
)


# Server-auth env names + the background-process port hint.
# Defensive: if a shim carrying these names ever runs under our parent
# env, these would leak the local dev server's basic-auth pair / port
# list into the child.
_KILO_DEFENSIVE_VARS: frozenset[str] = frozenset(
    {
        "KILO_SERVER_PASSWORD",
        "KILO_SERVER_USERNAME",
        "KILO_BACKGROUND_PROCESS_PORTS",
    }
)


# Misc commonly-leaked secret-bearing env vars that don't match the
# CC/OC lists but are well-known (third-party CI tokens, container
# registry auth, etc.). Names that already match
# :data:`SENSITIVE_NAME_PATTERN` are still listed here so the strip is
# unambiguous if someone later weakens the regex.
_MISC_CREDENTIAL_VARS: frozenset[str] = frozenset(
    {
        # GitHub / GitLab / generic CI
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITLAB_TOKEN",
        # HuggingFace
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        # Container registries
        "DOCKER_PASSWORD",
        "DOCKER_AUTH_CONFIG",
        # NPM / pip indexes
        "NPM_TOKEN",
        "PIP_INDEX_URL",  # may carry inline basic-auth
        # Cloud generic
        "AZURE_CLIENT_SECRET",
        "AZURE_TENANT_ID",
        # SSH
        "SSH_AUTH_SOCK",
    }
)


CREDENTIAL_ENV_NAMES: frozenset[str] = (
    _CC_CREDENTIAL_VARS
    | _OC_CREDENTIAL_VARS
    | _KILO_DEFENSIVE_VARS
    | _MISC_CREDENTIAL_VARS
)
"""Project-level strip list of credential-bearing env var names.

The union of CC + OC (see ``manage_coding_credentials.py`` lines 69-83),
the server-auth defensive names, and a small set of
well-known third-party CI / registry / cloud secrets.

Note: ``platform`` cannot ``import`` from ``qai.ai_coding`` (context
isolation, ``AGENTS.md`` section 3.2). If a unified source is ever
needed, ``ai_coding`` should import **from here**, not the other way
around.
"""


SENSITIVE_NAME_PATTERN: re.Pattern[str] = re.compile(
    r"(API_KEY|SECRET|TOKEN|AUTH_TOKEN|PASSWORD|PRIVATE_KEY)",
    re.IGNORECASE,
)
"""Defensive regex catch-all for credential-shaped env var names.

Anything containing one of these substrings (case-insensitive) is
stripped even if it is not in :data:`CREDENTIAL_ENV_NAMES`. Catches
vendor-specific names we have not enumerated (``MY_VENDOR_API_KEY``,
``FOO_OAUTH_TOKEN``, etc.).
"""


def build_child_env(
    *,
    base: dict[str, str] | None = None,
    bgp_id: str | None = None,
    token: str | None = None,
    guard_token: str | None = None,
) -> dict[str, str]:
    """Build the env dict for a background-process child.

    Returns a **new** ``dict`` - the caller's ``base`` is never mutated.

    Steps (applied in order):

    1. Start from a copy of ``base`` (defaults to ``os.environ`` if
       ``base`` is ``None``).
    2. Remove every key in :data:`CREDENTIAL_ENV_NAMES`.
    3. Remove every remaining key whose name matches
       :data:`SENSITIVE_NAME_PATTERN` (defensive tail-catch).
    4. Inject ``TERM=dumb`` to disable ANSI escapes in child output.
    5. Inject the UTF-8 locale block
       (``LC_ALL`` / ``LANG`` / ``LANGUAGE`` / ``PYTHONIOENCODING`` /
       ``PYTHONUTF8``) per ``AGENTS.md`` section 3.10 and design doc
       section 1.1.
    6. If ``bgp_id`` is non-empty, inject
       ``QAI_BACKGROUND_PROCESS_ID`` (namespaced under ``QAI_`` because
       this is our project).
    7. If ``token`` is non-empty, inject
       ``QAI_BACKGROUND_PROCESS_TOKEN``.
    8. If ``guard_token`` is non-empty, inject
       ``QAI_FILEGUARD_GUARD_TOKEN`` so the native ``guard64.dll`` marks
       this child (and its whole subtree, via env inheritance) as a
       *guarded* process routed through the ASK pipeline (see
       ``docs/90-refactor/DESIGN-fileguard-guard-only-agent-exec-tree-
       2026-07-06.md`` section 4.2). ``None`` injects nothing → the child
       is bypassed (allow-all), the safe non-guarding default.

    Note ordering: strip happens **before** injection so a malicious /
    misconfigured ``base`` cannot smuggle in a custom
    ``LC_ALL=POSIX`` or ``TERM=xterm-256color`` etc. The ``guard_token``
    injection in particular MUST come after the
    :data:`SENSITIVE_NAME_PATTERN` strip (step 3) — that regex matches
    ``TOKEN`` and would otherwise remove our freshly-set marker.

    Args:
        base: Source environment. Defaults to a copy of ``os.environ``.
            Never mutated.
        bgp_id: Optional background-process id (e.g. ``"bgp_abc123"``).
            Injected as ``QAI_BACKGROUND_PROCESS_ID`` if non-empty.
        token: Optional auth token for the child to call back to the
            manager. Injected as ``QAI_BACKGROUND_PROCESS_TOKEN`` if
            non-empty.
        guard_token: Optional FileGuard guard-token. Injected as
            ``QAI_FILEGUARD_GUARD_TOKEN`` if non-empty so the spawned
            subtree is guarded; ``None`` (guard disabled / not started)
            injects nothing.

    Returns:
        A new ``dict[str, str]`` suitable for ``subprocess.Popen(env=...)``.
    """
    env: dict[str, str] = dict(base if base is not None else os.environ)

    # Step 2: strip known credential names.
    for name in CREDENTIAL_ENV_NAMES:
        env.pop(name, None)

    # Step 3: defensive regex tail-catch. Materialise the key list first
    # because we mutate the dict during iteration.
    for name in list(env.keys()):
        if SENSITIVE_NAME_PATTERN.search(name):
            env.pop(name, None)

    # Step 4: ANSI off.
    env["TERM"] = "dumb"

    # Step 5: UTF-8 locale block (AGENTS.md section 3.10 + design doc
    # section 1.1).
    env["LC_ALL"] = "C.UTF-8"
    env["LANG"] = "C.UTF-8"
    env["LANGUAGE"] = "C.UTF-8"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    # Step 6 + 7: namespaced injection under ``QAI_``.
    if bgp_id:
        env["QAI_BACKGROUND_PROCESS_ID"] = bgp_id
    if token:
        env["QAI_BACKGROUND_PROCESS_TOKEN"] = token

    # Step 8: FileGuard guard-token marker. MUST be set after the
    # SENSITIVE_NAME_PATTERN strip above (that regex matches ``TOKEN``);
    # setting it here injects the marker into THIS child's env only (never
    # the host os.environ) so the native guard64.dll guards this subtree.
    if guard_token:
        env["QAI_FILEGUARD_GUARD_TOKEN"] = guard_token

    # Step 9 (2026-07-09): ALWAYS-ON child-process protected-path guard —
    # parity with the exec tool's env builder
    # (``qai.tools.infrastructure.exec_env`` / ``handlers/exec.py``). Inject the
    # protected-paths child hook dir on PYTHONPATH + ``QAI_PROTECTED_PATHS`` so
    # a background child interpreter (LLM-started OR App Builder preview) cannot
    # write into the Qualcomm / QAIRT SDK tree (2026-06-16 incident backstop).
    # This closes the gap where background children lacked the sitecustomize
    # hook exec children have. PYTHONPATH is APPENDED (not overwritten) so any
    # inherited / later-layered PYTHONPATH (e.g. App Builder's model/pack roots
    # in ``StartInput.extra_env``) coexists with the hook dir.
    try:
        from qai.platform import child_process_audit_sentinel, protected_paths

        hook_dir = str(
            Path(child_process_audit_sentinel.__file__).resolve().parent
        )
        existing_pp = env.get("PYTHONPATH", "")
        if existing_pp:
            # hook dir first so the sitecustomize is discovered, then keep any
            # inherited entries.
            env["PYTHONPATH"] = hook_dir + os.pathsep + existing_pp
        else:
            env["PYTHONPATH"] = hook_dir
        env["QAI_PROTECTED_PATHS"] = protected_paths.env_value()
    except Exception:  # noqa: BLE001 — never let env wiring break a spawn
        pass

    return env
