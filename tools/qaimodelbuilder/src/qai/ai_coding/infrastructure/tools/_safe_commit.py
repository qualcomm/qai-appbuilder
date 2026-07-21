# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Defensive safe-write / commit layer for the agent's file tools.

A single incident motivated this module: a tool bug truncated a ~2162-line
file down to 26 lines via a plain non-atomic ``Path.write_text(...)`` — no
backup, no write-back verification, no guard against a replacement that
silently shrinks the file. A subsequent ``git checkout`` then lost the
uncommitted work and the content was unrecoverable.

This layer hardens every disk-mutating tool (``write`` overwrite, ``edit``,
``apply_patch``) so that class of failure (tool bug / process kill / power
loss / corrupt-but-complete write) cannot cause UNRECOVERABLE damage —
WITHOUT any user or model interaction. The defences, cheapest-first:

#. **Conservation check** (in ``edit`` / ``apply_patch``, NOT here): pure
   arithmetic on the in-memory content BEFORE any disk write — catches a
   mislanded / truncating replacement before it ever touches disk. See
   ``handlers/_edit_match.replace_block`` (``consumed``) and ``tool_edit``.
#. **Trash backup** (:func:`backup_to_trash`): the ORIGINAL pre-write bytes
   are copied to a git-independent trash dir so even a "complete but wrong"
   overwrite is recoverable.
#. **Atomic write** (:func:`atomic_write_bytes`): write to a same-directory
   temp file + ``fsync`` + ``os.replace`` — the file is either the old
   content or the new content, never a half-written truncation.
#. **Read-back verify** (:func:`verify_after_write`): re-read the file and
   assert it byte-for-byte equals what we intended, decodes as strict UTF-8,
   and contains no ``U+FFFD`` replacement character.

Filesystem IO is allowed here; there is NO asyncio — callers already wrap
these helpers in ``asyncio.to_thread``. Everything is platform-neutral
(``os.name`` branches isolate the POSIX-only parent-dir fsync).
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import stat
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("qai.ai_coding.tools.safe_commit")

__all__ = [
    "SafeWriteError",
    "atomic_write_bytes",
    "backup_to_trash",
    "verify_after_write",
    "safe_commit_text",
    "find_data_root",
    "resolve_trash_root",
    "prune_backups_for_file",
    "cleanup_trash_root",
    "active_trash_roots",
    "DEFAULT_KEEP",
    "DEFAULT_TTL_DAYS",
    "DEFAULT_TOTAL_CAP",
]


# ---------------------------------------------------------------------------
# Trash RETENTION / CLEANUP thresholds (module-level so they are easy to tune).
#
# The edit-trash is an append-only recovery LEDGER: ``backup_to_trash`` only
# ever ADDS ``.bak`` files + ``manifest.jsonl`` lines. Without retention it
# would grow unbounded, so a TRIPLE limit prunes the oldest backups whenever
# ANY of the three is exceeded:
#   * ``DEFAULT_KEEP``       — per-file max number of backups to retain.
#   * ``DEFAULT_TTL_DAYS``   — max age (any backup older than this is dropped).
#   * ``DEFAULT_TOTAL_CAP``  — total trash-tree byte cap (global cleanup only).
# Two triggers drive cleanup (see :func:`prune_backups_for_file` /
# :func:`cleanup_trash_root`): (1) a cheap per-file prune after every
# successful backup write, and (2) a global sweep at daemon startup.
# ---------------------------------------------------------------------------

DEFAULT_KEEP: int = 50
"""Per-file max number of ``.bak`` backups to keep (newest-first)."""

DEFAULT_TTL_DAYS: int = 30
"""Max backup age in days; anything older is eligible for deletion."""

DEFAULT_TOTAL_CAP: int = 512 * 1024 * 1024
"""Total edit-trash byte cap (512 MiB) enforced by the global cleanup."""


# ---------------------------------------------------------------------------
# Active-workspace trash registry (process-local).
#
# The startup sweep (``apps/api/lifespan._edit_trash_cleanup_once``) only knows
# the "predictable" roots (env override / repo_root / per-user fallback). A real
# USER WORKSPACE's ``<workspace>/.edit_trash`` (e.g. ``C:\WoS_AI\.edit_trash``)
# is discovered only at runtime when an edit lands there — and historically got
# ONLY the per-file prune (keep+TTL), never the global byte cap, so a busy
# workspace could grow unbounded.
#
# Fix (no background scheduler, no new dep): every time ``backup_to_trash``
# resolves a trash root we (1) remember it here so the startup sweep can include
# it next boot, and (2) run ``cleanup_trash_root`` (the SAME global-cap logic)
# on it ONCE per process per workspace — throttled so it is not every write.
# The sweep is best-effort and runs AFTER the backup itself succeeded, so it can
# never block or fail the actual file write and never deletes the just-written
# backup (it is the newest, well under any sane cap).
# ---------------------------------------------------------------------------

_active_trash_roots: set[str] = set()
"""Resolved string paths of trash roots written to during this process."""

_capped_trash_roots: set[str] = set()
"""Trash roots already given a global-cap sweep this process (throttle: once)."""


def _trash_root_key(root: Path) -> str:
    try:
        return str(root.resolve())
    except (OSError, ValueError):  # pragma: no cover — defensive
        return str(root)


def active_trash_roots() -> list[Path]:
    """Snapshot of trash roots written to this process (for the startup sweep).

    Returned as ``Path`` objects; the caller (lifespan) de-duplicates against
    its own predictable roots. Never raises.
    """
    return [Path(p) for p in sorted(_active_trash_roots)]


def _maybe_cap_workspace_trash(root: Path) -> None:
    """Run the global-cap sweep on ``root`` at most once per process.

    Best-effort, NEVER raises (cleanup must never break / block a write). The
    first time a given workspace trash root is seen this process it gets a full
    :func:`cleanup_trash_root` sweep (per-file keep+TTL + global byte cap +
    manifest compaction); subsequent commits to the same workspace skip it
    (throttle), so the cap is bounded work, not per-write work.
    """
    key = _trash_root_key(root)
    if key in _capped_trash_roots:
        return
    _capped_trash_roots.add(key)
    try:
        # Read thresholds from the module-level constants AT CALL TIME so they
        # stay runtime-tunable (matches the per-file prune in ``backup_to_trash``).
        cleanup_trash_root(
            root,
            keep_per_file=DEFAULT_KEEP,
            ttl_days=DEFAULT_TTL_DAYS,
            total_cap_bytes=DEFAULT_TOTAL_CAP,
        )
    except Exception as exc:  # noqa: BLE001 — cap sweep must never raise
        logger.warning(
            "edit-trash workspace cap sweep failed for %s: %s (ignored)",
            root,
            exc,
        )



class SafeWriteError(Exception):
    """Raised when a safe write / verify / rollback step fails.

    Distinct from the tool-layer ``ToolError`` so the handlers can wrap it
    with their own model-facing message; carries a clear technical reason.
    """


# ---------------------------------------------------------------------------
# (a) atomic_write_bytes
# ---------------------------------------------------------------------------


def atomic_write_bytes(
    path: Path, data: bytes, *, max_replace_retries: int = 6
) -> None:
    """Atomically write ``data`` to ``path`` (temp file + fsync + replace).

    The file at ``path`` is, at every instant observable by another process,
    either its previous content or the complete new content — never a
    half-written truncation. Implementation:

    * Write to a SAME-DIRECTORY temp file (so ``os.replace`` is an atomic
      rename within one filesystem, never a cross-device copy).
    * ``flush`` + ``os.fsync`` the data to stable storage before the rename.
    * Preserve the original file mode (chmod the temp to the old file's
      ``st_mode & 0o777`` when it existed and is not a symlink).
    * ``os.replace`` with retry + backoff on transient ``PermissionError`` /
      ``OSError`` (Windows editors / AV / indexers briefly hold handles).
      Abort IMMEDIATELY (no retry) on ``errno.EXDEV`` (cross-filesystem —
      cannot happen for a same-dir temp, so it signals a real bug).
    * Best-effort parent-directory fsync ONLY on POSIX; never let a dir-fsync
      failure break the write.
    * On ANY exception the temp file is unlinked in ``finally`` — a
      half-written ``.tmp`` is never left behind.

    Raises :class:`SafeWriteError` on final failure (after retries).
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)

    # Capture the original mode (best-effort) so the replacement keeps it.
    orig_mode: int | None = None
    try:
        st = os.lstat(path)
        if not stat.S_ISLNK(st.st_mode):
            orig_mode = stat.S_IMODE(st.st_mode)
    except (FileNotFoundError, OSError):
        orig_mode = None

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

        if orig_mode is not None:
            try:
                os.chmod(tmp_path, orig_mode)
            except OSError:  # pragma: no cover — best-effort mode preserve
                pass

        _replace_with_retry(tmp_path, path, max_replace_retries)

        # The temp file no longer exists after a successful replace; clear the
        # reference so the finally-unlink does not remove the real target.
        tmp_path = None  # type: ignore[assignment]

        # POSIX-only: fsync the parent dir so the rename itself is durable.
        if os.name != "nt":
            _fsync_dir_best_effort(parent)
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:  # pragma: no cover — temp may already be gone
                pass


def _replace_with_retry(
    tmp_path: Path, dest: Path, max_replace_retries: int
) -> None:
    """``os.replace(tmp, dest)`` with backoff on transient holds.

    Windows editors / AV / search indexers can hold a brief handle on the
    destination, making ``os.replace`` raise ``PermissionError`` (or a
    generic ``OSError``) momentarily. We retry with a short escalating
    backoff. ``errno.EXDEV`` (cross-device) is fatal immediately — a
    same-directory temp can never be cross-device, so EXDEV means a real bug.
    """
    delay = 0.02
    last_exc: BaseException | None = None
    for attempt in range(max_replace_retries + 1):
        try:
            os.replace(str(tmp_path), str(dest))
            return
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.EXDEV:
                raise SafeWriteError(
                    f"atomic write aborted: temp file {tmp_path} is on a "
                    f"different filesystem than {dest} (EXDEV) — this should "
                    "never happen for a same-directory temp file."
                ) from exc
            last_exc = exc
            if attempt >= max_replace_retries:
                break
            time.sleep(delay)
            delay = min(delay * 2, 0.5)
    raise SafeWriteError(
        f"atomic write failed: could not replace {dest} after "
        f"{max_replace_retries + 1} attempts: {last_exc}"
    ) from last_exc


def _fsync_dir_best_effort(directory: Path) -> None:
    """Best-effort fsync of a directory (POSIX). Swallow every error."""
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:  # pragma: no cover — POSIX-only path
        return
    try:
        os.fsync(dir_fd)
    except OSError:  # pragma: no cover — best-effort
        pass
    finally:
        try:
            os.close(dir_fd)
        except OSError:  # pragma: no cover
            pass


# ---------------------------------------------------------------------------
# repo-data discovery (robust, no hardcoded "../..")
# ---------------------------------------------------------------------------


def find_data_root(start: Path) -> Path | None:
    """Find the project ``data/`` dir by walking up from ``start``.

    The repo root is identified by a STRUCTURAL marker — the directory that
    contains BOTH ``src/`` and ``apps/`` (State-Truth-First §5.5: never
    "fixed N levels up" / "default relative path"). ``data/`` is the runtime
    sibling of those. Returns the ``data`` path (created lazily by the
    caller) or ``None`` when no such root is found.

    NOTE: this is NO LONGER the trash-root resolver — see
    :func:`resolve_trash_root`. The dev-repo ``data/`` marker only exists in
    the development checkout, so using it for trash silently DISABLED the
    recovery net in real user workspaces (e.g. ``C:\\WoS_AI``) that have no
    ``src/``+``apps/`` structure. Kept as a generic helper for any caller that
    genuinely wants the dev-repo ``data/`` (none in-tree at present).
    """
    try:
        cur = start.resolve()
    except (OSError, ValueError):  # pragma: no cover — defensive
        cur = start
    if cur.is_file():
        cur = cur.parent
    for candidate in (cur, *cur.parents):
        try:
            if (candidate / "src").is_dir() and (candidate / "apps").is_dir():
                return candidate / "data"
        except OSError:  # pragma: no cover — defensive
            continue
    return None


# ---------------------------------------------------------------------------
# trash-root resolution — FOLLOWS the edited file's workspace (State-Truth-
# First): the recovery backup must live in the SAME workspace as the file it
# protects, so it is co-located and discoverable, and so editing files in a
# NORMAL user workspace (no ``src/``+``apps/`` marker) is NOT silently skipped.
# ---------------------------------------------------------------------------

# Env-var seam so tests can pin the trash root to a tmp_path and never touch
# the real repo / real user dirs. Empty / unset => normal resolution chain.
TRASH_ROOT_ENV = "QAI_EDIT_TRASH_ROOT"

# The trash directory name (a sibling of the workspace's own files, NOT under
# ``data/``). The repo root's ``/.edit_trash/`` is gitignored.
_TRASH_DIRNAME = ".edit_trash"


def _is_dir_writable(directory: Path) -> bool:
    """True if ``directory`` exists (or can be created) and is writable.

    Best-effort, never raises: the trash layer must degrade gracefully.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(str(directory), os.W_OK)


def _user_app_trash_dir() -> Path | None:
    """Last-resort per-user app trash dir (platform-neutral).

    Windows: ``%LOCALAPPDATA%\\QAIModelBuilder\\edit_trash``.
    POSIX: ``$XDG_DATA_HOME/qai/edit_trash`` else ``~/.qai/edit_trash``.

    Returns ``None`` only when even the home/appdata cannot be resolved.
    """
    try:
        if os.name == "nt":
            base = os.environ.get("LOCALAPPDATA")
            if base:
                return Path(base) / "QAIModelBuilder" / "edit_trash"
            return Path.home() / "QAIModelBuilder" / "edit_trash"
        xdg = os.environ.get("XDG_DATA_HOME")
        if xdg:
            return Path(xdg) / "qai" / "edit_trash"
        return Path.home() / ".qai" / "edit_trash"
    except (OSError, RuntimeError):  # pragma: no cover — defensive
        return None


def _walk_up_for_workspace(edited_path: Path) -> Path | None:
    """Fallback A: derive a sensible workspace root from the EDITED FILE.

    Prefer the nearest ancestor that is a VCS/workspace root (contains
    ``.git``); else fall back to the file's own parent directory. Never
    raises; returns ``None`` only when nothing resolves.
    """
    try:
        cur = edited_path.resolve()
    except (OSError, ValueError):  # pragma: no cover — defensive
        cur = edited_path
    if cur.is_file() or not cur.exists():
        cur = cur.parent
    for candidate in (cur, *cur.parents):
        try:
            if (candidate / ".git").exists():
                return candidate
        except OSError:  # pragma: no cover — defensive
            continue
    # No VCS marker — keep it simple and robust: the file's parent directory.
    return cur if str(cur) else None


def resolve_trash_root(
    workspace_root: Path | None, edited_path: Path
) -> Path | None:
    """Resolve the directory under which the edit-trash tree lives.

    Resolution chain (almost never ``None`` — trash is the LAST-RESORT
    recovery net; we only give up when even a per-user dir is unwritable):

    #. **Test seam** — ``$QAI_EDIT_TRASH_ROOT`` (if set & writable) wins so
       tests can pin trash to ``tmp_path`` and never touch real dirs.
    #. **Primary** — ``<workspace_root>/.edit_trash`` where ``workspace_root``
       is the per-request active workspace (e.g. ``C:\\WoS_AI``). No
       ``src/``/``apps/``/``data/`` marker required: the backup lives in the
       SAME workspace as the edited file. (When the agent legitimately edits
       inside the dev repo, this is ``<dev-repo>/.edit_trash`` — the REPO ROOT
       ``.edit_trash`` sibling of ``src/``, NOT ``data/.edit_trash``.)
    #. **Fallback A** — ``workspace_root`` missing/empty: walk up from the
       edited file to a ``.git`` root (else the file's parent) and use its
       ``/.edit_trash``.
    #. **Fallback B** — neither writable: a per-user app dir
       (``%LOCALAPPDATA%\\QAIModelBuilder\\edit_trash`` / ``~/.qai/edit_trash``).
       Files are laid out under a flattened absolute-path bucket so different
       workspaces never collide.
    #. **None** — only when NONE of the above is writable (a real loss of the
       recovery net; the caller logs at WARNING).
    """
    # 1. Test / operator override seam.
    env_override = os.environ.get(TRASH_ROOT_ENV)
    if env_override:
        cand = Path(env_override)
        if _is_dir_writable(cand):
            return cand

    # 2. Primary: the per-request active workspace.
    if workspace_root:
        try:
            ws = Path(workspace_root)
        except (TypeError, ValueError):  # pragma: no cover — defensive
            ws = None  # type: ignore[assignment]
        if ws is not None:
            cand = ws / _TRASH_DIRNAME
            if _is_dir_writable(cand):
                return cand

    # 3. Fallback A: derive a workspace from the edited file itself.
    derived = _walk_up_for_workspace(edited_path)
    if derived is not None:
        cand = derived / _TRASH_DIRNAME
        if _is_dir_writable(cand):
            return cand

    # 4. Fallback B: last-resort per-user app dir.
    user_dir = _user_app_trash_dir()
    if user_dir is not None and _is_dir_writable(user_dir):
        return user_dir

    # 5. Give up — recovery net unavailable.
    return None


# ---------------------------------------------------------------------------
# (b) backup_to_trash
# ---------------------------------------------------------------------------


def _short_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _relative_for_trash(layout_base: Path | None, path: Path) -> Path:
    """Best-effort path for laying out the trash tree under its root.

    When ``layout_base`` is given and ``path`` is under it, lay the backup out
    relative to that base (co-located, human-readable). Otherwise (different
    drive / outside the base / no base — e.g. the per-user fallback root) fall
    back to a flattened anchor-stripped form so the layout NEVER escapes the
    trash dir and NEVER raises.
    """
    if layout_base is not None:
        try:
            return path.resolve().relative_to(layout_base.resolve())
        except (ValueError, OSError):
            pass
    # Not under the layout base — flatten drive/anchor so it stays a relative
    # path inside the trash tree.
    parts = [p for p in path.parts if p not in ("/", "\\")]
    cleaned = [
        p.replace(":", "").replace("\\", "_").replace("/", "_")
        for p in parts
    ]
    return Path(*cleaned) if cleaned else Path(path.name)


def backup_to_trash(
    workspace_root: Path | None,
    path: Path,
    original_bytes: bytes,
    *,
    tool: str,
    meta: dict[str, Any],
    trash_root: Path | None = None,
) -> Path | None:
    """Save the ORIGINAL (pre-write) content to a git-independent trash dir.

    The trash ROOT is resolved by :func:`resolve_trash_root` and FOLLOWS the
    edited file's workspace — NOT the dev repo. Layout::

        <trash_root>/files/<relative-or-flattened-path>/<UTC>-p<pid>-<hash>.bak

    where ``<trash_root>`` is ``<workspace_root>/.edit_trash`` for a normal
    user workspace, ``<.git-root>/.edit_trash`` / ``<file-parent>/.edit_trash``
    when no workspace was supplied, or a per-user app dir as the last resort.
    ``<hash>`` is the sha256 short hash of ``original_bytes``. The backup
    itself is written via :func:`atomic_write_bytes` so it is never
    half-written. A JSON line is appended to ``<trash_root>/manifest.jsonl``.

    ``trash_root`` may be passed explicitly (tests / operator override); when
    ``None`` it is resolved from ``workspace_root`` + ``path``.

    Returns the backup path, or ``None`` when NO trash root is writable at all
    (degrades gracefully — the atomic write + verify remain the primary safety
    net; trash is the RECOVERY net). Skipping the recovery net is a real loss
    of safety, so it is logged at WARNING. A backup failure NEVER blocks the
    actual write.
    """
    try:
        root = trash_root
        if root is None:
            root = resolve_trash_root(workspace_root, path)
        if root is None:
            logger.warning(
                "edit-trash SKIPPED (recovery net unavailable): no writable "
                "trash root from workspace=%s / file=%s",
                workspace_root,
                path,
            )
            return None

        # Lay the backup out relative to the trash root's parent workspace
        # when the file lives there; otherwise flatten (per-user fallback /
        # cross-drive) so it stays inside the trash tree.
        layout_base = root.parent if root.name == _TRASH_DIRNAME else None
        rel = _relative_for_trash(layout_base, path)
        backup_dir = root / "files" / rel
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y%m%dT%H%M%S%fZ")
        short = _short_hash(original_bytes)
        backup_name = f"{ts}-p{os.getpid()}-{short}.bak"
        backup_path = backup_dir / backup_name

        atomic_write_bytes(backup_path, original_bytes)

        new_bytes = meta.get("new_bytes_blob")
        record: dict[str, Any] = {
            "timestamp": now.isoformat(),
            "tool": tool,
            "path": str(rel).replace("\\", "/"),
            "backup_path": str(backup_path),
            "original_sha256": hashlib.sha256(original_bytes).hexdigest(),
            "original_bytes": len(original_bytes),
            "original_lines": original_bytes.count(b"\n"),
        }
        if isinstance(new_bytes, (bytes, bytearray)):
            nb = bytes(new_bytes)
            record["new_sha256"] = hashlib.sha256(nb).hexdigest()
            record["new_bytes"] = len(nb)
            record["new_lines"] = nb.count(b"\n")
        # Pass through caller meta (edits count etc.); never let the internal
        # bytes blob leak into the manifest.
        for k, v in meta.items():
            if k == "new_bytes_blob":
                continue
            record[k] = v

        _append_manifest_line(root, record)

        # Per-file retention (cheap, single-dir): prune THIS file's old
        # backups right after the successful write. Best-effort + never
        # deletes the backup just written (it is the newest), so a cleanup
        # failure can never lose the recovery point we just created. A
        # failure here is swallowed by the enclosing ``except`` below — the
        # backup itself already succeeded, so cleanup must not undo it.
        # The thresholds are read from the module-level constants at call time
        # (NOT via default-arg binding) so they stay runtime-tunable.
        prune_backups_for_file(
            backup_dir,
            keep=DEFAULT_KEEP,
            ttl_days=DEFAULT_TTL_DAYS,
            just_written=backup_path,
        )

        # Record this trash root as ACTIVE (so the next startup sweep includes
        # it) and give it a global-cap sweep ONCE per process (throttled). This
        # bounds a busy USER WORKSPACE's trash by ``DEFAULT_TOTAL_CAP`` too, not
        # just the per-file keep+TTL above. Best-effort: runs only AFTER the
        # backup succeeded, never blocks/raises the write, never deletes the
        # backup just written (newest, well under the cap).
        _active_trash_roots.add(_trash_root_key(root))
        _maybe_cap_workspace_trash(root)

        return backup_path
    except Exception as exc:  # noqa: BLE001 — trash must never block the write
        logger.warning(
            "edit-trash backup failed for %s (tool=%s): %s — proceeding "
            "(atomic write + verify remain active)",
            path,
            tool,
            exc,
        )
        return None


def _append_manifest_line(trash_root: Path, record: dict[str, Any]) -> None:
    """Append one JSON line to ``<trash_root>/manifest.jsonl`` (append-only).

    Uses a plain append (the daemon is single-process for trash writes; a
    torn append at most loses the LAST line, never corrupts earlier records),
    which is the right durability/cost trade-off for a recovery LEDGER.
    """
    trash_root.mkdir(parents=True, exist_ok=True)
    manifest = trash_root / "manifest.jsonl"
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(manifest, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:  # pragma: no cover — best-effort durability
            pass


# ---------------------------------------------------------------------------
# (b2) trash RETENTION / CLEANUP — bound the append-only recovery ledger.
# ---------------------------------------------------------------------------

# ``.bak`` filenames are ``<UTC>-p<pid>-<hash>.bak`` (see ``backup_to_trash``);
# the leading timestamp sorts chronologically, but mtime is the authoritative
# age signal (robust to clock skew / a hand-copied file), so cleanup keys on it.
_BAK_SUFFIX = ".bak"


def _bak_entries(backup_dir: Path) -> list[tuple[float, int, Path]]:
    """Return ``(mtime, size, path)`` for every ``*.bak`` in ``backup_dir``.

    Best-effort: a missing dir / unreadable entry yields an empty or partial
    list, never an exception. Sorted newest-first (largest mtime first).
    """
    out: list[tuple[float, int, Path]] = []
    try:
        with os.scandir(backup_dir) as it:
            for entry in it:
                try:
                    if not entry.name.endswith(_BAK_SUFFIX):
                        continue
                    if not entry.is_file():
                        continue
                    st = entry.stat()
                    out.append((st.st_mtime, st.st_size, Path(entry.path)))
                except OSError:  # pragma: no cover — vanished mid-scan
                    continue
    except OSError:
        return out
    out.sort(key=lambda t: t[0], reverse=True)
    return out


def _unlink_best_effort(path: Path) -> bool:
    """Delete ``path``; log + swallow any error. Returns True on success."""
    try:
        path.unlink()
        return True
    except OSError as exc:  # pragma: no cover — write-locked / vanished
        logger.warning("edit-trash cleanup could not delete %s: %s", path, exc)
        return False


def prune_backups_for_file(
    backup_dir: Path,
    *,
    keep: int = DEFAULT_KEEP,
    ttl_days: int = DEFAULT_TTL_DAYS,
    just_written: Path | None = None,
) -> int:
    """Prune one file's backup dir: drop backups beyond ``keep`` AND past TTL.

    A backup is deleted when it is BOTH past the ``keep`` newest AND older than
    ``ttl_days`` — i.e. we always retain the ``keep`` most-recent backups
    regardless of age, and within the older tail we additionally drop anything
    over the TTL. (Keeping ``keep`` newest unconditionally means a burst of
    edits in one day is never lost to TTL, while a long-idle file's stale
    backups still expire.)

    Best-effort, NEVER raises (backup/cleanup must never break the actual file
    write); ``just_written`` (the backup created by the current commit) is
    never deleted. Returns the number of backups removed.
    """
    try:
        entries = _bak_entries(backup_dir)
        if not entries:
            return 0
        keep_n = max(0, keep)
        cutoff = time.time() - max(0, ttl_days) * 86400.0
        just = None
        if just_written is not None:
            try:
                just = just_written.resolve()
            except (OSError, ValueError):  # pragma: no cover — defensive
                just = just_written
        removed = 0
        # entries[:keep_n] are the newest survivors; only consider the tail.
        for mtime, _size, path in entries[keep_n:]:
            if mtime >= cutoff:
                continue  # within TTL — keep it even though it's past ``keep``
            if just is not None:
                try:
                    if path.resolve() == just:
                        continue  # never delete the just-written backup
                except (OSError, ValueError):  # pragma: no cover — defensive
                    pass
            if _unlink_best_effort(path):
                removed += 1
        if removed:
            logger.info(
                "edit-trash pruned %d old backup(s) in %s (keep=%d ttl=%dd)",
                removed,
                backup_dir,
                keep_n,
                ttl_days,
            )
        return removed
    except Exception as exc:  # noqa: BLE001 — cleanup must never raise
        logger.warning(
            "edit-trash per-file prune failed for %s: %s (ignored)",
            backup_dir,
            exc,
        )
        return 0


def _iter_all_bak_dirs(files_root: Path) -> list[Path]:
    """All leaf dirs (under ``<trash_root>/files``) that contain ``*.bak``.

    Best-effort walk; never raises. A trash tree mirrors the edited file's
    relative path, so backups live in nested dirs — we collect every dir that
    directly holds at least one ``.bak``.
    """
    dirs: list[Path] = []
    try:
        for dirpath, _dirnames, filenames in os.walk(files_root):
            if any(n.endswith(_BAK_SUFFIX) for n in filenames):
                dirs.append(Path(dirpath))
    except OSError:  # pragma: no cover — defensive
        pass
    return dirs


def cleanup_trash_root(
    trash_root: Path,
    *,
    keep_per_file: int = DEFAULT_KEEP,
    ttl_days: int = DEFAULT_TTL_DAYS,
    total_cap_bytes: int = DEFAULT_TOTAL_CAP,
) -> int:
    """Global edit-trash sweep: enforce per-file keep + TTL + total size cap.

    Run once at daemon startup (see ``apps/api/lifespan.py``). Phases:

    #. **Per-file keep + TTL** — :func:`prune_backups_for_file` on every
       backup dir under ``<trash_root>/files`` (no ``just_written`` to spare).
    #. **Total size cap** — if the surviving backups still exceed
       ``total_cap_bytes``, delete OLDEST backups across ALL files until under
       the cap (newest are retained — they are the most likely recovery
       targets).
    #. **Manifest compaction** — rewrite ``<trash_root>/manifest.jsonl``
       dropping every line whose ``backup_path`` no longer exists, so the
       ledger does not grow forever. Unparseable / partial lines are skipped
       (robust to a torn final append). The rewrite is atomic
       (:func:`atomic_write_bytes`).

    Best-effort, NEVER raises; a missing ``trash_root`` is a no-op. Returns the
    number of ``.bak`` files removed (compaction line drops not counted).
    """
    try:
        root = Path(trash_root)
        if not root.exists():
            return 0

        files_root = root / "files"
        removed = 0

        # Phase 1: per-file keep + TTL on every backup dir.
        for backup_dir in _iter_all_bak_dirs(files_root):
            removed += prune_backups_for_file(
                backup_dir, keep=keep_per_file, ttl_days=ttl_days
            )

        # Phase 2: global total-size cap — delete oldest across all files.
        if total_cap_bytes >= 0:
            survivors: list[tuple[float, int, Path]] = []
            for backup_dir in _iter_all_bak_dirs(files_root):
                survivors.extend(_bak_entries(backup_dir))
            total = sum(size for _m, size, _p in survivors)
            if total > total_cap_bytes:
                # Oldest first (ascending mtime) so we drop the least-likely
                # recovery targets until under the cap.
                survivors.sort(key=lambda t: t[0])
                for _mtime, size, path in survivors:
                    if total <= total_cap_bytes:
                        break
                    if _unlink_best_effort(path):
                        removed += 1
                        total -= size
                logger.info(
                    "edit-trash global cap enforced for %s "
                    "(cap=%d bytes, removed-in-phase reaching %d total deletes)",
                    root,
                    total_cap_bytes,
                    removed,
                )

        # Phase 3: compact the manifest (drop lines whose backup is gone).
        _compact_manifest(root)

        return removed
    except Exception as exc:  # noqa: BLE001 — global cleanup must never raise
        logger.warning(
            "edit-trash global cleanup failed for %s: %s (ignored)",
            trash_root,
            exc,
        )
        return 0


def _compact_manifest(trash_root: Path) -> None:
    """Rewrite ``manifest.jsonl`` keeping only lines whose backup still exists.

    Robust to a corrupt / partial line (skipped, not fatal). The rewrite is
    atomic so a crash mid-compaction never leaves a torn ledger. Best-effort:
    any failure is logged + swallowed. When every line is dropped the manifest
    is rewritten empty (not unlinked) so future appends keep a stable target.
    """
    manifest = trash_root / "manifest.jsonl"
    try:
        raw = manifest.read_bytes()
    except FileNotFoundError:
        return
    except OSError as exc:  # pragma: no cover — unreadable ledger
        logger.warning("edit-trash manifest read failed for %s: %s", manifest, exc)
        return

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:  # pragma: no cover — defensive
        logger.warning(
            "edit-trash manifest %s is not valid UTF-8; leaving as-is", manifest
        )
        return

    kept_lines: list[str] = []
    dropped = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except (ValueError, TypeError):
            # Corrupt / partial line — drop it (it points at nothing usable).
            dropped += 1
            continue
        backup_path = record.get("backup_path") if isinstance(record, dict) else None
        if not isinstance(backup_path, str) or not backup_path:
            dropped += 1
            continue
        try:
            exists = Path(backup_path).exists()
        except OSError:  # pragma: no cover — defensive
            exists = False
        if exists:
            kept_lines.append(stripped)
        else:
            dropped += 1

    if dropped == 0:
        return  # nothing to compact — avoid a needless rewrite

    new_text = ("\n".join(kept_lines) + "\n") if kept_lines else ""
    try:
        atomic_write_bytes(manifest, new_text.encode("utf-8"))
        logger.info(
            "edit-trash manifest compacted for %s (dropped %d dead/corrupt line(s), "
            "kept %d)",
            trash_root,
            dropped,
            len(kept_lines),
        )
    except Exception as exc:  # noqa: BLE001 — compaction must never raise
        logger.warning(
            "edit-trash manifest compaction write failed for %s: %s (ignored)",
            manifest,
            exc,
        )



# ---------------------------------------------------------------------------
# (c) verify_after_write
# ---------------------------------------------------------------------------


def verify_after_write(path: Path, expected_bytes: bytes) -> None:
    """Read ``path`` back and assert it equals ``expected_bytes`` + is clean.

    Three checks, each with a clear, distinct error:

    #. Byte-for-byte equality with what we intended to write (catches a short
       / truncated / tampered write — the 2162→26 failure mode at the disk
       level). Reports both byte lengths on mismatch.
    #. Decodes as STRICT UTF-8 (AGENTS.md §3.10 — never accept a file that is
       not valid UTF-8).
    #. Contains no ``U+FFFD`` replacement character — its presence means the
       content was produced through a lossy decode (double-encoding / mojibake
       pipeline) even if the bytes round-trip; the message says exactly that,
       not a generic "decode failed".
    """
    try:
        actual = path.read_bytes()
    except OSError as exc:
        raise SafeWriteError(
            f"post-write verify could not read {path} back: {exc}"
        ) from exc

    if actual != expected_bytes:
        raise SafeWriteError(
            f"post-write verify FAILED for {path}: on-disk size "
            f"{len(actual)} bytes != expected {len(expected_bytes)} bytes "
            "(the write did not land exactly as intended)."
        )

    try:
        text = actual.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SafeWriteError(
            f"post-write verify FAILED for {path}: content is not valid "
            f"UTF-8: {exc}"
        ) from exc

    if "\ufffd" in text:
        raise SafeWriteError(
            f"post-write verify FAILED for {path}: content contains "
            "replacement character U+FFFD (the content was produced through "
            "a lossy / double-encoding pipeline)."
        )


# ---------------------------------------------------------------------------
# (d) safe_commit_text — orchestration for edit / write(overwrite)
# ---------------------------------------------------------------------------


def _reject_unsafe_target(path: Path) -> None:
    """Reject symlinks, special files, and read-only existing files.

    Raises :class:`SafeWriteError` (the handler wraps it into a model-facing
    error). The atomic ``os.replace`` would otherwise FOLLOW a symlink target
    or fail confusingly on a fifo/socket/device; we reject up front with a
    clear reason. Path safety (workspace containment) is the caller's job
    (``resolve_under_workspace``); this only guards the FILE KIND + writability.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return  # new file — nothing to reject
    except OSError as exc:  # pragma: no cover — defensive
        raise SafeWriteError(f"cannot stat {path}: {exc}") from exc

    mode = st.st_mode
    if stat.S_ISLNK(mode):
        raise SafeWriteError(
            f"refusing to write through symlink: {path}"
        )
    if not stat.S_ISREG(mode):
        kind = "directory" if stat.S_ISDIR(mode) else "special file"
        raise SafeWriteError(
            f"refusing to write {kind} (not a regular file): {path}"
        )
    # Read-only existing file: os.replace can still clobber it on some
    # platforms, but the user marked it read-only deliberately — reject.
    if not os.access(str(path), os.W_OK):
        raise SafeWriteError(
            f"refusing to overwrite read-only file: {path}"
        )


def safe_commit_text(
    *,
    path: Path,
    new_text: str,
    original_text: str,
    line_ending: str,
    workspace_root: Path | None,
    tool: str,
    edits: int,
    meta: dict[str, Any] | None = None,
    restore_line_ending: Any,
) -> None:
    """Commit ``new_text`` to ``path`` through the full safety pipeline.

    Order (the in-process per-path lock is ALREADY held by the caller — we do
    NOT add another):

    #. Reject symlink / special file / read-only existing file.
    #. Compute ``expected_bytes = restore_line_ending(new_text, line_ending)``
       encoded as UTF-8.
    #. Backup the ORIGINAL content to trash (best-effort, never blocks).
    #. :func:`atomic_write_bytes`.
    #. :func:`verify_after_write`.
    #. On write/verify failure, attempt rollback from the just-written trash
       backup (restore the original via :func:`atomic_write_bytes`) and
       re-raise — surfacing the failure (State-Truth-First) rather than
       pretending success.

    ``restore_line_ending`` is injected (the pure helper from ``_edit_match``)
    to keep this module free of a handler import cycle.
    """
    _reject_unsafe_target(path)

    expected_bytes = restore_line_ending(new_text, line_ending).encode("utf-8")
    original_bytes = restore_line_ending(original_text, line_ending).encode(
        "utf-8"
    )

    backup_meta: dict[str, Any] = dict(meta or {})
    backup_meta["edits"] = edits
    backup_meta["new_bytes_blob"] = expected_bytes

    backup_path = backup_to_trash(
        workspace_root, path, original_bytes, tool=tool, meta=backup_meta
    )

    try:
        atomic_write_bytes(path, expected_bytes)
        verify_after_write(path, expected_bytes)
    except Exception as exc:  # noqa: BLE001 — rollback then re-raise
        _attempt_rollback(path, original_bytes, backup_path, tool, exc)
        raise


def _attempt_rollback(
    path: Path,
    original_bytes: bytes,
    backup_path: Path | None,
    tool: str,
    cause: BaseException,
) -> None:
    """Restore ``original_bytes`` after a failed commit; surface any failure.

    A rollback that itself fails MUST NOT be swallowed (State-Truth-First):
    the on-disk file may be in a damaged state and the operator needs to know
    the trash backup is the recovery source. We log at ERROR with the trash
    path and chain the original cause; the caller re-raises the original
    error so the tool reports failure (never a false success).
    """
    try:
        atomic_write_bytes(path, original_bytes)
        verify_after_write(path, original_bytes)
        logger.warning(
            "%s: write failed and was ROLLED BACK to original content for "
            "%s (cause: %s)",
            tool,
            path,
            cause,
        )
    except Exception as rb_exc:  # noqa: BLE001 — surface, do not swallow
        logger.error(
            "%s: write FAILED and ROLLBACK ALSO FAILED for %s "
            "(write cause: %s; rollback error: %s). RECOVER MANUALLY from "
            "trash backup: %s",
            tool,
            path,
            cause,
            rb_exc,
            backup_path if backup_path is not None else "<no backup written>",
        )
