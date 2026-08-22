# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Subprocess environment builder for the streaming ``exec`` tool.

V1 parity: ``backend/tools/_security.py::_build_exec_env`` (lines 414-447).
This module reproduces ONLY the PATH / venv / PortableGit / ``PYTHONUNBUFFERED``
slice of the V1 builder — the slice that makes ``git`` and the Unix coreutils
shipped with PortableGit resolvable, makes the running interpreter's venv
``Scripts`` directory win on ``PATH``, and forces line-buffered child stdout so
``print()`` output is never lost when a child is killed on timeout.

Deliberately OUT of scope here (V1 keeps these in the same function, but they
belong to different concerns / layers in V2):

* FileGuard ``sitecustomize`` / policy env injection (V1 449-489) — sandbox /
  FileGuard subprocess auditing, owned by the security context.
* Network proxy injection (V1 492-540) — owned by the ai_coding tool DI
  wiring (``apps/api/_ai_coding_di.py``), which embeds proxy credentials from
  the SecretStore.

Cross-platform (AGENTS.md): the PortableGit lookup is keyed off
``%LOCALAPPDATA%``. On non-Windows hosts (or when the variable is empty) the
git injection is silently skipped — never crashing, never importing anything
Windows-only.

Layering (§3.5 import-linter): infrastructure may import stdlib only here; it
must NOT import ``backend.*`` / ``features.*`` / ``apps.*`` / ``interfaces.*``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["build_exec_env"]


def build_exec_env(*, guard_token: str | None = None, allow_x86: bool = False) -> dict[str, str]:
    """Build the subprocess environment for the exec tool.

    Returns a mutable copy of ``os.environ`` with:

    * the running interpreter's venv ``Scripts`` (``Path(sys.executable).parent``)
      prepended to ``PATH`` so project-local console scripts (``qai``, ``pip``,
      ``ruff`` …) resolve first;
    * ``PYTHONHOME`` / ``PYTHONPATH`` popped so a child Python does not inherit
      a stale interpreter home / module search path;
    * ``PYTHONUNBUFFERED=1`` so child Python uses line-buffered stdout even when
      stdout is a pipe (otherwise full buffering loses ``print()`` output if the
      child is killed on timeout);
    * PortableGit's bin directories under
      ``%LOCALAPPDATA%\\QAIModelBuilder\\git`` prepended to ``PATH`` — only the
      directories that exist, in the order ``cmd`` / ``bin`` / **System32** /
      ``usr\\bin`` / ``clangarm64\\bin``. ``cmd`` is first so ``git`` resolves
      to the wrapper rather than the raw ``bin\\git.exe``; ``System32`` is
      interposed BEFORE ``usr\\bin`` so the 8 PortableGit coreutils that
      collide by name with semantically-incompatible Windows built-ins
      (``find`` / ``sort`` / ``timeout`` / ``tar`` / ``whoami`` / ``hostname``
      / ``expand`` / ``reset``) resolve to the Windows built-in in cmd
      pipelines, while the ~240 Unix-only tools (``grep`` / ``sed`` / ``awk`` /
      ``ls`` / ``cat`` …) still resolve to ``usr\\bin`` (no useful tool lost).

    The function is platform-neutral: a missing / empty ``LOCALAPPDATA``
    gracefully skips the git injection (no crash, no Windows-only import).

    Args:
        guard_token: Optional FileGuard guard-token. When non-empty it is
            injected as ``QAI_FILEGUARD_GUARD_TOKEN`` so the native
            ``guard64.dll`` marks the spawned ``exec`` child (and its whole
            subtree, via env inheritance) as a *guarded* process routed
            through the ASK pipeline (2026-07-06 guard-only reversal, see
            ``docs/90-refactor/DESIGN-fileguard-guard-only-agent-exec-tree-
            2026-07-06.md`` section 4.2). ``None`` (guard disabled / not
            started) injects nothing → the child is bypassed (allow-all),
            the safe non-guarding default. The token comes from the
            ``apps/api`` composition root; it is added ONLY to this child
            env copy, never to the host ``os.environ``.
    """
    env = os.environ.copy()

    # venv Scripts first on PATH.
    venv_scripts = str(Path(sys.executable).parent)
    env["PATH"] = venv_scripts + os.pathsep + env.get("PATH", "")

    # Do not let children inherit a stale interpreter home / path.
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)

    # Force line-buffered stdout in child Python processes.
    env["PYTHONUNBUFFERED"] = "1"

    # ALWAYS-ON child-process protected-path guard (independent of FileGuard /
    # OS sandbox). Inject the protected-paths child hook dir on PYTHONPATH +
    # ``QAI_PROTECTED_PATHS`` so a child interpreter (e.g. the model-builder
    # pipeline) cannot write into the Qualcomm / QAIRT SDK tree. Mirrors the
    # ai_coding-context exec env builder (``handlers/exec.py::_build_exec_env``).
    try:
        from qai.platform import child_process_audit_sentinel, protected_paths

        env["PYTHONPATH"] = str(Path(child_process_audit_sentinel.__file__).resolve().parent)
        env["QAI_PROTECTED_PATHS"] = protected_paths.env_value()
    except Exception:  # noqa: BLE001 — never let env wiring break exec
        pass

    # PortableGit (git for Windows) bin directories, if installed. Order
    # matters on two axes:
    #
    #   * ``cmd`` first so ``git`` resolves to the PortableGit wrapper, not the
    #     raw ``bin\git.exe``.
    #   * ``System32`` is interposed BEFORE ``usr\bin`` (the MSYS coreutils
    #     dir). PortableGit's ``usr\bin`` ships ~250 Unix tools, EIGHT of which
    #     collide by name with Windows built-ins that have INCOMPATIBLE
    #     semantics: ``find sort timeout tar whoami hostname expand reset``.
    #     The streaming exec path strips the ``cmd /c`` wrapper and lets
    #     ``cmd.exe`` interpret the command, so a piped ``... | find /c "x"``
    #     (Windows: "count matching lines") would otherwise resolve to the GNU
    #     ``find`` and treat ``/c`` as a START PATH == the C: drive root,
    #     recursing the WHOLE disk (the 2026-06-21 runaway-output incident).
    #     Putting ``System32`` ahead of ``usr\bin`` makes those 8 collide-by-
    #     name tools resolve to the Windows built-in (correct for cmd
    #     pipelines) while the ~240 Unix-only tools (``grep sed awk ls cat wc``
    #     …) still resolve to ``usr\bin`` — no useful Git/Unix tool is lost.
    #     This fixes a latent V1 defect (V1 ``_build_exec_env`` prepended
    #     ``usr\bin`` ahead of everything too); per AGENTS.md 🟡🟡 a V1 defect
    #     must be corrected in V2, never carried forward.
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        git_root = Path(local_app_data) / "QAIModelBuilder" / "git"
        system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        git_bin_candidates = [
            git_root / "cmd",
            git_root / "bin",
            # System32 BEFORE usr\bin so the 8 collide-by-name Windows built-ins
            # win over the GNU coreutils of the same name in cmd pipelines.
            system32,
            git_root / "usr" / "bin",
            git_root / "clangarm64" / "bin",
        ]
        # Only prepend git dirs when PortableGit is actually present; without it
        # there is nothing to shadow, so leave the inherited PATH untouched.
        if (git_root / "cmd").is_dir() or (git_root / "usr" / "bin").is_dir():
            prefix_parts = [str(p) for p in git_bin_candidates if p.is_dir()]
            if prefix_parts:
                env["PATH"] = (
                    os.pathsep.join(prefix_parts) + os.pathsep + env["PATH"]
                )

    # FileGuard guard-token marker (2026-07-06 guard-only reversal). Set on
    # the child env copy only — never the host os.environ — so the native
    # guard64.dll guards this exec subtree. ``None`` injects nothing.
    if guard_token:
        env["QAI_FILEGUARD_GUARD_TOKEN"] = guard_token
    # x86 process escape hatch: propagate QAI_GUARD_ALLOW_X86=1 when the user
    # enables "Allow 32-bit processes" in Security settings so the native
    # guard64 HookedCreateProcessW does not terminate x86 children.
    if allow_x86:
        env["QAI_GUARD_ALLOW_X86"] = "1"

    return env
