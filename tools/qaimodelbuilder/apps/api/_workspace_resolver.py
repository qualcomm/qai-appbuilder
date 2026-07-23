# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Single resolution point for the model-builder *workspace root*.

The workspace root (historically the hard-coded ``C:\\WoS_AI``) is the
directory under which the ``model-builder`` skill places every model
artifact. It is consumed by four bounded contexts — ``chat`` (system
prompt / SKILL.md injection), ``model_builder`` (workspace reader /
initializer traversal guard), ``security`` (sandbox allow/block lists +
persistent ACL via the ``${WORKSPACE}`` placeholder) and the frontend
(``modelWorkdir`` extractor). To keep those consumers from each
re-deriving the value (and drifting), this module is the **one place**
that computes it.

Resolution order (later wins is NOT used — first hit wins):

1. ``forge.config`` → ``workspace.model_root`` (WebUI-editable override,
   persisted in ``<data>/config/forge_config.json`` — the same blob the
   chat-hooks engine and other operator config already read). This lets
   the user change the root from the Settings page without restarting.
2. the typed platform default ``Settings.workspace.model_root``
   (``C:/WoS_AI``), itself overridable via ``server.toml`` /
   ``QAI_WORKSPACE__MODEL_ROOT``.

Living in ``apps/api`` (the wiring layer) makes this a legitimate
cross-context bridge: each consumer receives a plain ``str`` / ``Path``
and never imports another context (AGENTS.md §3.2).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:  # pragma: no cover
    from .di import Container

__all__ = [
    "DEFAULT_WORKSPACE_ROOT",
    "resolve_workspace_root",
    "read_forge_config_workspace_root",
    "conversation_meta_workspace",
    "build_session_workspace_resolver",
    "build_explicit_session_workspace_resolver",
    "resolve_global_allow_paths",
    "resolve_read_only_allow_paths",
    "resolve_runtime_exec_allow_paths",
    "resolve_system_exec_allow_paths",
    "resolve_file_guard_masked_paths",
]

#: Canonical fallback when neither the forge.config override nor the
#: platform Settings field yields a usable value. Mirrors
#: ``WorkspaceSettings.model_root`` so the two never drift.
#: On non-Windows platforms the conventional Windows root is unusable, so
#: fall back to ``~/WoS_AI`` (expanded at import time).
DEFAULT_WORKSPACE_ROOT: str = (
    "C:/WoS_AI"
    if sys.platform == "win32"
    else os.path.abspath("IQ_AI")
)


def _clean(value: object) -> str:
    """Return a usable workspace-root string, or ``""`` when unusable.

    A blank / whitespace-only / literal ``"null"`` value is rejected so a
    corrupted config row can never collapse the root to a relative path
    (which would pollute the process CWD == repo root — the exact bug
    this whole feature fixes).
    """
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if not cleaned or cleaned.lower() == "null":
        return ""
    return cleaned


def read_forge_config_workspace_root(forge_config_path: Path) -> str:
    """Read ``workspace.model_root`` from a ``forge_config.json`` file.

    Returns ``""`` when the file is absent / malformed / the key is unset
    or unusable. Never raises — workspace resolution must not break
    startup or request handling (callers fall back to the platform
    default).
    """
    try:
        if not forge_config_path.is_file():
            return ""
        raw = json.loads(forge_config_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — config read is best-effort
        return ""
    workspace = ((raw or {}).get("workspace")) or {}
    if not isinstance(workspace, dict):
        return ""
    return _clean(workspace.get("model_root"))


def resolve_workspace_root(container: "Container") -> str:
    """Resolve the effective model-builder workspace root for ``container``.

    forge.config override (WebUI-editable) → platform Settings default.
    Always returns a non-blank, usable string.
    """
    forge_config_path = (
        container.data_paths.root / "config" / "forge_config.json"
    )
    override = read_forge_config_workspace_root(forge_config_path)
    if override:
        return override

    settings_default = _clean(
        getattr(
            getattr(container.settings, "workspace", None),
            "model_root",
            "",
        )
    )
    return settings_default or DEFAULT_WORKSPACE_ROOT


def resolve_global_allow_paths(container: "Container") -> tuple[str, ...]:
    """Resolve the FileGuard three-state GLOBAL allow prefixes for ``container``.

    Single source of truth (State-Truth-First) shared by BOTH FileGuard
    layers — the native ``guard64.dll`` allow (white) prefix seed
    (:func:`apps.api._native_hook_rules.seed_native_guard_rules`) and the
    in-process Python ``CheckPermissionUseCase`` prefix ALLOW short-circuit
    (injected via ``_security_di``). These prefixes are ALWAYS ALLOWED
    (read / write / execute, op-agnostic, subtree-covering) for ANY session
    without prompting.

    Resolved set (de-duplicated, order-preserving) — GLOBAL (session-
    independent) prefixes only:

    1. the runtime data root + its sibling models root — ``data_paths.root``
       (== ``<repo_root>/data`` in dev, ``%LOCALAPPDATA%\\QAIModelBuilder\\data``
       on an installed user machine) + ``data_paths.root.parent / "models"``.
       This is the data directory THIS process actually reads/writes
       (``container.data_paths.root`` — the single truth source), so runtime
       config / blobs / models under it are never prompted, for ANY session.
    2. the per-user data/models roots — ``%LOCALAPPDATA%/QAIModelBuilder/data``
       + ``%LOCALAPPDATA%/QAIModelBuilder/models`` — using
       :func:`qai.platform.config.paths.resolve_localappdata_qai_root` (the
       pair is skipped when ``LOCALAPPDATA`` is unset — non-Windows / stripped
       environments);
    3. any operator-configured ``Settings.security.global_allow_paths``.

    NOTE: the model-builder *workspace* root (``C:/WoS_AI`` and any per-session
    override) is deliberately NOT included here. The workspace is a
    SESSION-scoped whitelist — each conversation may set its OWN workspace
    path (default ``C:/WoS_AI``), so it is covered per-session by the
    workspace session grant + the ``workspace_allow_provider`` (session
    isolation). Only the native ``guard64.dll`` layer seeds the whole
    workspace root as a global prefix, because the native hook cannot express
    session scope; the Python tool layer keeps it session-scoped. Putting the
    workspace data/models here would (a) be redundant with the session
    provider and (b) wrongly leak one conversation's default-workspace
    data/models to every session.

    Never raises — a resolution fault degrades to the fields that DID
    resolve so a config hiccup cannot wedge startup / permission checks.
    """
    from qai.platform.config.paths import resolve_localappdata_qai_root

    roots: list[str] = []

    def _add(value: object) -> None:
        cleaned = _clean(value if isinstance(value, str) else str(value or ""))
        if cleaned:
            roots.append(cleaned)

    # 1) the RUNTIME data root this process actually uses + its sibling
    #    models root. ``container.data_paths.root`` is the single truth source
    #    (== <repo_root>/data in dev). Its parent is the repo/user root, so
    #    ``<root_parent>/models`` is the co-located models tree. This is the
    #    fix for the "reading <repo_root>/data/config/*.json still prompts"
    #    gap — the process's OWN data dir must be on the allow list. GLOBAL
    #    (session-independent), unlike the per-session workspace.
    try:
        data_root = getattr(getattr(container, "data_paths", None), "root", None)
    except Exception:  # noqa: BLE001
        data_root = None
    if data_root is not None:
        # Absolutise: ``data_paths.root`` is frequently a RELATIVE path
        # (e.g. ``WindowsPath('data')`` when the process CWD is the repo root),
        # but the FileGuard receives ABSOLUTE resource paths — a relative allow
        # prefix would never prefix-match. Resolve against CWD so both layers
        # compare absolute paths (State-Truth-First — use the real on-disk
        # location, not a relative assumption). ``resolve()`` also normalises
        # ``..`` / separators. Falls back to the raw path if resolution fails.
        try:
            dr = Path(str(data_root)).resolve()
        except Exception:  # noqa: BLE001 — degrade to the unresolved path
            # Extremely rare (odd/too-long path, transient FS error). We keep
            # the raw path so SOMETHING is still seeded; the in-process check
            # additionally absolutises the *target* side
            # (``_path_under_any_prefix`` → ``_absolutise``), so an unresolved
            # relative prefix here still has a chance to match once the target
            # is absolutised against the same CWD.
            dr = Path(str(data_root))
        _add(str(dr))
        _add(str(dr.parent / "models"))

    # 2) %LOCALAPPDATA%/QAIModelBuilder/{data,models} (skipped if env unset)
    try:
        appdata_root = resolve_localappdata_qai_root()
    except Exception:  # noqa: BLE001
        appdata_root = None
    if appdata_root is not None:
        _add(str(appdata_root / "data"))
        _add(str(appdata_root / "models"))

    # 3) operator-configured extra global-allow prefixes
    try:
        extra = getattr(
            getattr(container.settings, "security", None),
            "global_allow_paths",
            (),
        )
    except Exception:  # noqa: BLE001
        extra = ()
    for path in extra or ():
        _add(path)

    # 4) Python-runtime EXECUTE roots (interpreter dir + sys.prefix +
    #    sys.base_prefix). The API spawns its OWN worker subprocesses by
    #    re-executing the interpreter; with the native guard ON that spawn is an
    #    EXECUTE event which the READ-only whitelist cannot allow. These roots
    #    are op-agnostic-allowed (read + execute) here so the app's worker
    #    machinery is never blocked (see ``resolve_runtime_exec_allow_paths``).
    #    Tightly scoped to the interpreter roots — NOT the whole system.
    try:
        for rt in resolve_runtime_exec_allow_paths(container):
            _add(rt)
    except Exception:  # noqa: BLE001 — degrade to no runtime-exec prefixes
        pass

    # De-duplicate case-insensitively while preserving first-seen order.
    seen: set[str] = set()
    unique: list[str] = []
    for path in roots:
        key = path.replace("\\", "/").casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return tuple(unique)


def _dedupe_paths(paths: "list[str]") -> tuple[str, ...]:
    """De-duplicate path strings case-insensitively, first-seen order."""
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        key = path.replace("\\", "/").casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return tuple(unique)


def _repo_root(container: "Container") -> "Path | None":
    """Best-effort repo/user root = ``data_paths.root`` parent (absolutised).

    ``container.data_paths.root`` is the single truth source (``<repo_root>/data``
    in dev). Its parent is the repo root. Falls back to ``container.repo_root``
    when the data path is unavailable. Returns ``None`` on total failure.
    """
    try:
        data_root = getattr(getattr(container, "data_paths", None), "root", None)
    except Exception:  # noqa: BLE001
        data_root = None
    if data_root is not None:
        try:
            return Path(str(data_root)).resolve().parent
        except Exception:  # noqa: BLE001 — degrade to the unresolved parent
            return Path(str(data_root)).parent
    repo_root = getattr(container, "repo_root", None)
    if repo_root:
        try:
            return Path(str(repo_root)).resolve()
        except Exception:  # noqa: BLE001
            return Path(str(repo_root))
    return None


# ---------------------------------------------------------------------------
# FileGuard base-environment path config — factory/config/file_guard_paths.json
# ---------------------------------------------------------------------------
# A RUNTIME BASELINE (not a user preference): declares the paths the tool's own
# runtime legitimately needs to read / write / execute. Loaded once at startup
# and seeded onto the native OP-MASKED whitelist (guard64.dll white_ops) +
# enforced by the Python permission check. Deliberately NOT UI-editable (not in
# the runtime-config switch set) — edit the file directly to change the base
# environment. Each entry ``{path, read, write, execute}``; ``path`` may contain
# ``%ENV%`` variables (expanded at load; an entry whose var is unset is skipped).
#
# Per-op precision is native (方案 B — the op-masked whitelist): an op whose flag
# is true is allowed (skip callback); an op whose flag is false FALLS THROUGH to
# the callback / ASK (it is NOT force-allowed, and NOT hard-denied here — only
# the black list hard-denies). So "read + execute but not write" (C:\Qualcomm)
# is expressed exactly, without abusing the black list to deny reads.
_FILE_GUARD_PATHS_RELPATH = ("factory", "config", "file_guard_paths.json")
_PROGRAM_PATH_ALLOWLIST_RELPATH = (
    "factory",
    "config",
    "program_path_allowlist.json",
)

# Op-mask bits — MUST match the native rule.h kMask* constants and the bridge's
# AceMask semantics: READ=1, WRITE=2, EXECUTE=4, DELETE=8.
_MASK_READ = 1 << 0
_MASK_WRITE = 1 << 1
_MASK_EXECUTE = 1 << 2
_MASK_DELETE = 1 << 3


def _expand_env_path(raw: str) -> str:
    """Expand ``%ENV%`` variables in ``raw``; return "" if any var is unset.

    ``os.path.expandvars`` leaves an unknown ``%VAR%`` literal in place; we
    treat that as "environment not present" and skip the entry (return "") so a
    stripped environment never seeds a bogus ``%LOCALAPPDATA%\\...`` literal.
    """
    import os

    if not raw:
        return ""
    expanded = os.path.expandvars(raw)
    if "%" in expanded:
        # an unexpanded %VAR% remained -> the variable is unset; skip.
        return ""
    return expanded


def resolve_file_guard_masked_paths(
    container: "Container",
) -> "tuple[tuple[str, int], ...]":
    """Load & op-mask-classify ``factory/config/file_guard_paths.json``.

    Returns ``((abs_path, mask), ...)`` — each entry's path (``%ENV%``-expanded,
    absolutised) paired with its op-mask bitfield (READ=1, WRITE=2, EXECUTE=4;
    DELETE is implied by WRITE for config purposes and not separately settable).
    Seeded onto the native op-masked whitelist and mirrored by the Python
    permission check. De-duplicated by path (last mask wins). Never raises — a
    missing / malformed file (or any per-entry fault) degrades to whatever
    parsed cleanly (empty on total failure), so a config typo cannot wedge boot.
    """
    import json

    root = _repo_root(container)
    if root is None:
        return ()
    cfg_path = root.joinpath(*_FILE_GUARD_PATHS_RELPATH)
    try:
        text = cfg_path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 — file absent / unreadable -> no entries
        return ()
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001 — malformed JSON -> no entries
        return ()

    entries = data.get("entries", []) if isinstance(data, dict) else []
    by_key: "dict[str, tuple[str, int]]" = {}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        raw_path = entry.get("path", "")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        expanded = _expand_env_path(raw_path.strip())
        if not expanded:
            continue
        try:
            abs_path = str(Path(expanded).resolve())
        except Exception:  # noqa: BLE001 — keep the expanded (unresolved) path
            abs_path = expanded
        mask = 0
        if bool(entry.get("read", False)):
            mask |= _MASK_READ
        if bool(entry.get("write", False)):
            mask |= _MASK_WRITE | _MASK_DELETE  # write implies delete class
        if bool(entry.get("execute", False)):
            mask |= _MASK_EXECUTE
        if mask == 0:
            continue  # no permission requested -> nothing to seed
        key = abs_path.replace("\\", "/").casefold()
        by_key[key] = (abs_path, mask)  # last entry for a path wins
    return tuple(by_key.values())


def resolve_program_allowlist_paths(
    container: "Container",
) -> "tuple[tuple[str, int], ...]":
    """Load ``factory/config/program_path_allowlist.json`` → op-masked paths.

    2026-07-08 — per-program固定副产品路径放行. Some programs (e.g. powershell)
    write their own runtime dirs on startup (PSReadLine history, module cache,
    profile probes) unrelated to the user's command; those writes otherwise
    pop a native FileGuard dialog. This config declares, per program, the
    FIXED paths to allow so the noise is silenced without hardcoding — adding a
    program is a config edit, not a code change.

    Returns ``((abs_path, mask), ...)`` exactly like
    :func:`resolve_file_guard_masked_paths` (same op-mask bits, same %ENV%
    expansion, same de-dupe). The ``program`` / ``binary_globs`` fields are
    documentation/grouping ONLY — the native layer is a process-global prefix
    allowlist with no per-process rules, so the paths are seeded globally.
    Never raises — a missing / malformed file degrades to whatever parsed.

    SAFETY: entries must be program-owned runtime SUBDIRECTORIES (history /
    cache / profile), never a bare user root; this loader additionally SKIPS
    any expanded path that resolves to a drive root or a top-level user root
    (``%USERPROFILE%`` / ``%APPDATA%`` / ``%LOCALAPPDATA%`` themselves) so a
    mis-authored config cannot open a whole user tree.
    """
    import json

    root = _repo_root(container)
    if root is None:
        return ()
    cfg_path = root.joinpath(*_PROGRAM_PATH_ALLOWLIST_RELPATH)
    try:
        text = cfg_path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 — file absent / unreadable -> no entries
        return ()
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001 — malformed JSON -> no entries
        return ()

    # Guard rails: reject an expanded path that is a drive root or a bare
    # top-level user root (defence against an over-wide config entry).
    too_wide: set[str] = set()
    for var in ("%USERPROFILE%", "%APPDATA%", "%LOCALAPPDATA%"):
        ex = _expand_env_path(var)
        if ex:
            try:
                too_wide.add(str(Path(ex).resolve()).replace("\\", "/").casefold())
            except Exception:  # noqa: BLE001
                too_wide.add(ex.replace("\\", "/").casefold())

    programs = data.get("programs", []) if isinstance(data, dict) else []
    by_key: "dict[str, tuple[str, int]]" = {}
    for prog in programs if isinstance(programs, list) else []:
        if not isinstance(prog, dict):
            continue
        paths = prog.get("paths", [])
        for entry in paths if isinstance(paths, list) else []:
            if not isinstance(entry, dict):
                continue
            raw_path = entry.get("path", "")
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            expanded = _expand_env_path(raw_path.strip())
            if not expanded:
                continue
            try:
                abs_path = str(Path(expanded).resolve())
            except Exception:  # noqa: BLE001
                abs_path = expanded
            norm = abs_path.replace("\\", "/").rstrip("/").casefold()
            # Reject over-wide targets (defence against a mis-authored config
            # opening a whole user tree). Three independent guards:
            #   (a) drive root ("c:") or no path component at all;
            #   (b) equal to OR an ANCESTOR of a known user root
            #       (%USERPROFILE% / %APPDATA% / %LOCALAPPDATA%) — this catches
            #       ``%APPDATA%\..`` (= C:\Users\x\AppData) and bare roots that
            #       an exact-match check would miss;
            #   (c) minimum depth: at least 3 path components below the drive
            #       (e.g. C:/Users/x/AppData has 3 → still rejected as too
            #       shallow; a real program subdir like
            #       C:/Users/x/AppData/Roaming/Microsoft/... is deeper). This
            #       makes "program-owned SUBDIRECTORY only" enforceable rather
            #       than a blacklist of known roots.
            if "/" not in norm or norm.endswith(":"):
                continue
            if norm in too_wide or any(
                root.startswith(norm + "/") for root in too_wide
            ):
                continue  # equal to or an ancestor of a known user root
            # depth = path components after the drive anchor (drop "c:").
            depth = len([seg for seg in norm.split("/")[1:] if seg])
            if depth < 3:
                continue  # too shallow to be a program-owned subdir
            mask = 0
            if bool(entry.get("read", False)):
                mask |= _MASK_READ
            if bool(entry.get("write", False)):
                mask |= _MASK_WRITE | _MASK_DELETE
            if bool(entry.get("execute", False)):
                mask |= _MASK_EXECUTE
            if mask == 0:
                continue
            by_key[norm] = (abs_path, mask)
    return tuple(by_key.values())


def _collect_system_read_prefixes() -> list[str]:
    """Collect the OS + Python-runtime READ surface prefixes (absolutised).

    ``%SystemRoot%`` (fallback ``C:\\Windows``), the Python runtime prefixes
    (``sys.prefix`` / ``sys.base_prefix`` / the interpreter directory /
    ``site.getsitepackages()``) and ``%ProgramFiles%`` / ``%ProgramFiles(x86)%``.
    These are the paths LLM-spawned subprocesses read en masse; seeding them
    read-only prevents an ASK flood while still routing writes to ASK.

    Never raises — each source is best-effort; a failing source is skipped.
    """
    import os
    import site
    import sys

    out: list[str] = []

    def _add_abs(value: object) -> None:
        if not value:
            return
        try:
            out.append(str(Path(str(value)).resolve()))
        except Exception:  # noqa: BLE001 — skip an unresolvable entry
            out.append(str(value))

    # %SystemRoot% (Windows dir) — fall back to the conventional location.
    _add_abs(os.environ.get("SystemRoot") or r"C:\Windows")

    # Python runtime prefixes.
    _add_abs(getattr(sys, "prefix", ""))
    _add_abs(getattr(sys, "base_prefix", ""))
    try:
        _add_abs(Path(sys.executable).parent)
    except Exception:  # noqa: BLE001
        pass
    try:
        for sp in site.getsitepackages():
            _add_abs(sp)
    except Exception:  # noqa: BLE001 — getsitepackages may be absent in venvs
        pass

    # Program Files (both 32/64-bit views).
    _add_abs(os.environ.get("ProgramFiles") or r"C:\Program Files")
    _add_abs(os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)")

    # T4/T6: Windows scanner / Python toolchain surfaces that startup-time
    # subprocesses (Defender on-access scanner, pip, uv) read en masse.
    # Best-effort: env-unset / dir-missing entries are simply skipped.
    _add_abs(os.environ.get("ProgramData") or r"C:\ProgramData")
    _add_abs(os.environ.get("APPDATA"))
    appdata = os.environ.get("APPDATA")
    if appdata:
        _add_abs(str(Path(appdata) / "uv"))
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        _add_abs(str(Path(localappdata) / "uv"))
    sysroot = os.environ.get("SystemRoot") or r"C:\Windows"
    _add_abs(str(Path(sysroot) / "System32" / "CatRoot"))
    _add_abs(str(Path(sysroot) / "System32" / "catroot2"))

    return out


def _collect_path_env_read_prefixes() -> list[str]:
    """Collect the host ``%PATH%`` search directories as READ surface prefixes.

    Splits ``os.environ["PATH"]`` on ``os.pathsep`` and returns each entry that
    resolves to an existing directory (absolutised). These are the directories
    LLM-spawned subprocesses scan when resolving tools on ``PATH`` (compilers,
    ``ollama``, ``vcpkg``, per-user ``bin`` dirs, …); seeding them read-only
    lets a guarded child READ tools / files that really exist under them
    without an ASK popup. Only READ is granted here — writes / executes /
    deletes to these dirs still route through ASK (this feeds the read-only
    whitelist only, never the op-agnostic / exec allow sets).

    Only *existing directories* are kept: non-existent / non-directory PATH
    entries are skipped so garbage PATH fragments are never whitelisted and a
    subprocess probing a *missing* directory still flows through the existing
    probe-passthrough heuristic untouched. Results are de-duplicated
    case-insensitively, first-seen order.

    Never raises — best-effort; on any fault the partially-collected list is
    returned (mirrors :func:`_collect_system_read_prefixes`).
    """
    import os

    out: list[str] = []
    try:
        raw = os.environ.get("PATH", "") or ""
        seen: set[str] = set()
        for entry in raw.split(os.pathsep):
            entry = entry.strip()
            if not entry:
                continue
            try:
                resolved = str(Path(entry).resolve())
            except Exception:  # noqa: BLE001 — skip an unresolvable entry
                continue
            try:
                if not os.path.isdir(resolved):
                    continue
            except Exception:  # noqa: BLE001 — treat a stat fault as skip
                continue
            key = resolved.replace("\\", "/").casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(resolved)
    except Exception:  # noqa: BLE001 — degrade to what was collected so far
        return out
    return out


def resolve_read_only_allow_paths(container: "Container") -> tuple[str, ...]:
    """Resolve the FileGuard op-aware READ-ONLY allow prefixes for ``container``.

    Single source of truth (State-Truth-First) shared by BOTH FileGuard
    layers — the native ``guard64.dll`` op-aware read-only whitelist seed
    (``add_read_only_allow_rule`` -> ``AddReadOnlyWhiteRules``, which only
    skips the callback for READ events) and the in-process Python
    ``CheckPermissionUseCase`` read-only ALLOW short-circuit (which matches
    only read-only requests). Read is allowed without prompting; write / edit
    / delete / execute still go through ASK (never silently permitted, never
    a hard deny). ``protected_write_paths`` (black) still wins over any match.

    Resolved set (de-duplicated, order-preserving):

    1. the two business dirs under ``<repo_root>`` — ``skills`` +
       ``factory/chat_features`` (the latter is a prefix that already
       covers every feature under it, e.g. ``factory/chat_features/app-builder``);
       (``<repo_root>`` = ``data_paths.root`` parent, absolutised);
    2. the system read surface (``%SystemRoot%`` + the Python runtime prefixes
       + ``%ProgramFiles%`` / ``%ProgramFiles(x86)%``) — see
       :func:`_collect_system_read_prefixes`;
    2b. the host ``%PATH%`` search dirs that exist — see
       :func:`_collect_path_env_read_prefixes` (read-only only);
    3. any operator-configured ``Settings.security.read_only_allow_paths``
       (business extras) + ``Settings.security.system_read_allow_paths``
       (system extras).

    Never raises — a resolution fault degrades to the sources that DID resolve
    so a config hiccup cannot wedge startup / permission checks.
    """
    paths: list[str] = []

    def _add(value: object) -> None:
        cleaned = _clean(value if isinstance(value, str) else str(value or ""))
        if cleaned:
            paths.append(cleaned)

    # 1) business read-only dirs under <repo_root>.
    root = _repo_root(container)
    if root is not None:
        _add(str(root / "skills"))
        _add(str(root / "factory" / "chat_features"))

    # 2) system read surface (OS + Python runtime).
    try:
        for sysp in _collect_system_read_prefixes():
            _add(sysp)
    except Exception:  # noqa: BLE001 — degrade to no system prefixes
        pass

    # 2b) host %PATH% search dirs (READ-only). Subprocesses scan every PATH
    #     entry when resolving tools; seeding the existing ones read-only
    #     removes the ASK flood for those reads. Read-only channel only —
    #     writes / executes to PATH dirs still route through ASK (this never
    #     touches global-allow / exec sets). Only existing dirs are added.
    try:
        for pathp in _collect_path_env_read_prefixes():
            _add(pathp)
    except Exception:  # noqa: BLE001 — degrade to no PATH prefixes
        pass

    # 3) operator-configured extras (business + system knobs).
    try:
        security = getattr(container.settings, "security", None)
    except Exception:  # noqa: BLE001
        security = None
    for attr in ("read_only_allow_paths", "system_read_allow_paths"):
        try:
            extra = getattr(security, attr, ()) or ()
        except Exception:  # noqa: BLE001
            extra = ()
        for path in extra:
            _add(path)

    # 4) the tool's OWN install tree (<repo_root>) as read-only. The app spawns
    #    worker subprocesses (StickyWorker / one-shot runners) that must READ
    #    their own source (``<repo_root>\src\qai\...`` bootstrap + modules) and
    #    the factory assets; without this the guarded worker's imports are
    #    denied and it dies at the spawn handshake. Read-only (white_ro): the
    #    source tree needs read, never write from a guarded child (writes to
    #    data/models go through the op-agnostic global-allow of those subdirs).
    #
    # Phase 2 revision (2026-07-06): the earlier "ancestor chain up to drive
    # root" (added to solve the startup ``cmd /c ver`` per-level ASK cascade)
    # was REMOVED. That cascade is already covered by the Phase 1 trust-token
    # main-line (host-spawned children carry QAI_FILEGUARD_TRUST_TOKEN, are
    # classified as TrustedInfra in DllMain, and pass through undetermined
    # ops WITHOUT ASK). Keeping the ancestor chain here had the unintended
    # side effect of covering the ENTIRE drive root (``C:\``) in the
    # application-layer ``check_permission`` read whitelist — which shares
    # this SAME resolver — so AI tool reads of arbitrary paths under ``C:\``
    # (e.g. ``C:\tmp\<file>``, ``C:\Users\<name>\...``) short-circuited to
    # ALLOW at the app layer and never showed the ASK popup the operator
    # expected. Removing the ancestor chain restores the ASK popup for
    # tool-driven reads of paths outside the whitelist while the trust-token
    # main-line still covers legitimate host-spawned subprocess traversal.
    root = _repo_root(container)
    if root is not None:
        _add(str(root))

    return _dedupe_paths(paths)


def resolve_runtime_exec_allow_paths(container: "Container") -> tuple[str, ...]:
    """Resolve the Python-runtime EXECUTE allow prefixes for ``container``.

    The API process spawns its OWN worker subprocesses by re-executing the
    Python interpreter (StickyWorker persistent runner + one-shot runners via
    ``asyncio.create_subprocess_exec``). With the native guard ON by default
    (2026-07-06) that spawn triggers a CreateProcessW hook → an EXECUTE event
    for the interpreter image. The read-only whitelist only allows READ, so the
    interpreter execute would fall through to the ASK pipeline and — at startup
    with no operator to answer — time out fail-closed and BLOCK the worker
    (regression: ``sticky_worker_spawn_failed``). These interpreter directories
    must therefore be OP-AGNOSTIC allow (read + execute), seeded onto the native
    FULL white list and the Python global-allow short-circuit. Scoped tightly to
    the interpreter roots (``sys.executable`` dir + ``sys.prefix`` +
    ``sys.base_prefix``) — NOT the whole system — so this does not widen the
    write/exec surface beyond the app's own trusted runtime. A child's file ops
    OUTSIDE these roots are still gated per-path; ``protected_write_paths``
    (black) still wins.

    ``container`` accepted for resolver-signature symmetry (runtime-derived).

    Never raises — best-effort; a failing source is skipped.
    """
    import sys as _sys

    _ = container  # runtime-environment derived, not container derived
    paths: list[str] = []

    def _add_abs(value: object) -> None:
        if not value:
            return
        try:
            paths.append(str(Path(str(value)).resolve()))
        except Exception:  # noqa: BLE001 — skip an unresolvable entry
            paths.append(str(value))

    try:
        _add_abs(Path(_sys.executable).parent)
    except Exception:  # noqa: BLE001
        pass
    _add_abs(getattr(_sys, "prefix", ""))
    _add_abs(getattr(_sys, "base_prefix", ""))
    return _dedupe_paths(paths)


def resolve_system_exec_allow_paths(
    container: "Container",
) -> tuple[tuple[str, int], ...]:
    """Resolve system directories that host-spawned subprocesses need to
    EXECUTE (cmd.exe, PowerShell, system tools). Returned as (path, mask)
    tuples for op-mask seeding (READ + EXECUTE, deliberately NOT WRITE).

    The startup ASK storm root cause included ``cmd.exe /c ver`` being
    blocked because the readonly-allow list only permits READ, not EXECUTE.
    This op-mask allows execute so host-spawned subprocesses can run
    system commands without the ASK pipeline; writes to system dirs are
    still gated (mask has no WRITE bit).

    Never raises — degrades to empty tuple on failure.
    """
    _ = container  # environment-derived
    import os as _os

    # Op-mask bits (must match native/file-guard/guard/rule.h — see the
    # existing masked-path config file_guard_paths.json where
    # C:\Qualcomm uses mask 5 = READ|EXECUTE). Adopt the SAME convention.
    READ = 1
    EXECUTE = 4
    mask = READ | EXECUTE
    out: list[tuple[str, int]] = []

    def _add_abs(value: object) -> None:
        if not value:
            return
        try:
            p = str(Path(str(value)).resolve())
            out.append((p, mask))
        except Exception:  # noqa: BLE001
            out.append((str(value), mask))

    sysroot = _os.environ.get("SystemRoot") or r"C:\Windows"
    _add_abs(str(Path(sysroot) / "System32"))
    _add_abs(sysroot)
    # de-dupe by path
    seen: set[str] = set()
    dedup: list[tuple[str, int]] = []
    for p, m in out:
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        dedup.append((p, m))
    return tuple(dedup)


def conversation_meta_workspace(meta: Any) -> str:
    """Extract a usable ``workspace`` from a conversation ``meta`` mapping.

    Returns ``""`` when ``meta`` is missing / not a dict / the key is unset
    or unusable (blank / literal ``"null"``).
    """
    if not isinstance(meta, dict):
        return ""
    return _clean(meta.get("workspace"))


def build_session_workspace_resolver(
    container: "Container",
) -> Callable[[Any], Awaitable[str | None]]:
    """Build an async ``(conversation_id) -> str`` workspace resolver.

    Resolution order per session:

    1. the conversation's own ``meta.workspace`` (set via the per-session
       workspace UI), when usable;
    2. the global configured workspace (``resolve_workspace_root``).

    The returned coroutine never raises — any repository error falls back
    to the global workspace so tool dispatch is never broken by a config /
    storage hiccup. ``conversation_id`` may be ``None`` (→ global).
    """
    async def _resolve(conversation_id: Any) -> str | None:
        global_root = resolve_workspace_root(container)
        if conversation_id is None:
            return global_root
        try:
            chat = getattr(container, "chat", None)
            repo = getattr(chat, "conversations", None)
            if repo is None:
                return global_root
            conv = await repo.get(conversation_id)
            session_ws = conversation_meta_workspace(getattr(conv, "meta", None))
            return session_ws or global_root
        except Exception:  # noqa: BLE001 — best-effort; never break dispatch
            return global_root

    return _resolve


def build_explicit_session_workspace_resolver(
    container: "Container",
) -> Callable[[Any], Awaitable[str | None]]:
    """Build an async ``(conversation_id) -> str | None`` resolver that returns
    ONLY a conversation's EXPLICIT ``meta.workspace`` (no global fallback).

    Unlike :func:`build_session_workspace_resolver` (which falls back to the
    global workspace root when a conversation has no per-session workspace),
    this returns ``None`` / ``""`` when the conversation did NOT explicitly set
    its own working directory. This is the resolver the FileGuard three-state
    SESSION-SCOPED workspace-subtree ALLOW must use: session isolation requires
    that only conversations which explicitly set a working directory get the
    subtree ALLOW, so two default conversations never share one subtree via the
    global root (that global surface, if any, is covered by
    ``global_allow_paths`` instead). ``conversation_id`` ``None`` → ``None``.

    Never raises — a repository error degrades to ``None`` (no session allow),
    so a storage hiccup can only make the check STRICTER (fall through to
    policy / grant / ASK), never wider.
    """
    async def _resolve(conversation_id: Any) -> str | None:
        if conversation_id is None:
            return None
        try:
            chat = getattr(container, "chat", None)
            repo = getattr(chat, "conversations", None)
            if repo is None:
                return None
            conv = await repo.get(conversation_id)
            return conversation_meta_workspace(getattr(conv, "meta", None)) or None
        except Exception:  # noqa: BLE001 — best-effort; degrade to no allow
            return None

    return _resolve
