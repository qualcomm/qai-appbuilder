# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain layer for ``qai.command_policy``.

Pure Python entities — no framework dependencies (domain-purity contract
per import-linter forbidden set).

``CommandProfile`` is a named execution constraint matched against a
command's binary. It classifies a command into one of three actions
(2026-07-06 guard-rail redesign, user decision):

* ``ALLOW`` — proceed normally;
* ``ASK``   — dangerous-but-possibly-intended (``ask_args`` / ``ask_rules``
  / ``ask_always`` matched, e.g. ``git push --force`` / ``curl`` /
  ``powershell -EncodedCommand``): the caller pops a permission dialog and
  lets the *user* decide;
* ``DENY``  — hard block (``hard_deny_args`` / legacy ``denied_args``
  matched): the caller raises and feeds a corrective reason back to the LLM.

``allowed_args`` (a flag-admission list) is **deprecated** and its logic
has been removed from ``classify`` — all profiles leave it empty. The field
is retained only for API DTO backward-compat. ``io_constraints.input_dirs``
has been removed from all profiles (file-read protection is handled by the
native OS hook). ``io_constraints.output_dirs`` is also currently unused
(retained as a no-op in ``check_io_constraints`` for potential future use).
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

__all__ = [
    "ExecAction",
    "CommandProfile",
    "extract_args",
    "extract_binary",
]


class ExecAction(str, Enum):
    """The action a profile assigns to a command.

    ``str``-valued so it serialises cleanly in logs / SSE payloads.
    """

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


#: Shell operators that terminate a command's own argument list — args
#: after these belong to a piped/chained command (V1 ``_extract_args``).
_SHELL_SEPARATORS: Final[frozenset[str]] = frozenset(
    {"|", ">", ">>", "<", "&&", "||", "&", ";", "2>", "2>>", "2>&1"}
)

#: Flags that take a path value under the next token, used by the
#: ``output_dirs`` check to locate output path args.
_OUTPUT_PATH_FLAGS: Final[frozenset[str]] = frozenset(
    {
        "--output_path", "--output-path", "--output_dir", "--output-dir",
        "--output", "-o", "--out", "--out_dir", "--out-dir",
        "--binary_file", "--output-root", "--output_root",
    }
)


def _arg_matches_pattern(arg: str, pattern: str) -> bool:
    """Return ``True`` iff ``arg`` matches a danger ``pattern`` (token-level).

    Unlike the old naive ``pattern in arg`` substring test (which could
    never match a pattern containing a space, e.g. ``"push --force"``,
    because ``extract_args`` already split on whitespace), this compares
    at the token level:

    * exact match (case-insensitive): ``--force`` matches ``--force``;
    * flag-with-value prefix: pattern ``-c core.sshCommand=`` matches
      ``-c core.sshCommand=/tmp/x`` — but because tokens are split on
      whitespace, a space-containing pattern is normalised so that its
      first whitespace-delimited piece is treated as the flag and the
      remainder as a required value-prefix on the *following* token; the
      caller passes the joined ``flag value`` when it detects the pattern
      has a space (see :meth:`CommandProfile._match_danger`).

    This helper handles the single-token case; multi-token patterns are
    resolved in :meth:`CommandProfile._match_danger`.
    """
    a = arg.lower()
    p = pattern.lower()
    if a == p:
        return True
    # ``--flag=value`` form: pattern ``--flag`` matches ``--flag=...``.
    if "=" not in p and a.startswith(p + "="):
        return True
    # value-prefix form: pattern ``-c core.sshcommand=`` matches token
    # ``-c core.sshcommand=/x`` once flag+value were joined by caller.
    if a.startswith(p):
        return True
    return False


@dataclass(slots=True)
class CommandProfile:
    """An execution profile defining danger classification + io constraints.

    Fields (backward-compatible superset of the original 7):

    * ``name`` / ``description`` — identity.
    * ``match_glob`` — legacy single glob (kept for back-compat).
    * ``match_globs`` — **new**: a list of globs; a binary matches iff any
      glob matches. Lets a profile match both the bare invocation
      (``git`` / ``git.exe``) *and* the full path
      (``**/git/bin/git.exe``) — the bare form is how the LLM actually
      writes commands, which the single ``**/...`` glob never matched.
    * ``allowed_args`` — **deprecated** flag-admission list (see module
      docstring); still honoured if populated but new profiles leave empty.
    * ``denied_args`` — legacy hard-deny list (kept; DENY semantics).
    * ``hard_deny_args`` — **new**: dangerous flags that hard-block (DENY).
    * ``ask_args`` — **new**: dangerous-but-possibly-intended flags that
      trigger a user permission dialog (ASK), e.g. ``--force`` / ``--exec``
      / ``-c core.sshCommand=`` / ``--debug_host``.
    * ``ask_rules`` — **new**: structured *subcommand-aware* ASK rules for
      programs (like ``git``) whose danger lives in a **subcommand +
      flag/positional combination**, not a bare flag. ``ask_args`` matches
      a flag anywhere; ``ask_rules`` matches only when the first positional
      token is a given subcommand AND (optionally) a dangerous flag /
      positional is present — so ``git reset --hard`` → ASK while ``git
      reset --soft`` / ``git checkout -b`` stay ALLOW (no false-reject /
      dialog fatigue). Each rule is a dict::

          {
            "subcommand": "reset",         # required: 1st positional token
            "any_flags": ["--hard", "--keep"],   # optional: trigger flags
            "positional_any": [".", "--"], # optional: trigger positionals
            "reason": "…why dangerous…",   # shown in the dialog
          }

      Rule fires when ``subcommand`` matches AND — if ``any_flags`` /
      ``positional_any`` are given — at least one of them is present; if
      neither is given, the subcommand alone fires (the subcommand is
      inherently destructive, e.g. ``stash`` / ``rebase``).
    * ``io_constraints`` — input/output directory path constraints (DENY
      on escape).
    * ``source_skill`` — provenance (does not affect matching).
    """

    name: str
    description: str = ""
    match_glob: str = ""
    allowed_args: list[str] = field(default_factory=list)
    denied_args: list[str] = field(default_factory=list)
    io_constraints: dict = field(default_factory=dict)
    source_skill: str = ""
    match_globs: list[str] = field(default_factory=list)
    hard_deny_args: list[str] = field(default_factory=list)
    ask_args: list[str] = field(default_factory=list)
    ask_rules: list[dict] = field(default_factory=list)
    #: When True, ANY invocation whose binary matches this profile is ASK
    #: (user decides), regardless of args — for whole programs whose mere use
    #: is a non-file risk the native file layer cannot backstop (network
    #: exfil / registry / LOLBins: curl, wget, reg, powershell, certutil,
    #: bitsadmin, ...). This is what makes "non-file risks stay covered by the
    #: command policy" TRUE when enforce_exec skips the command-level Gate ③
    #: for native-active exec. Ranks below hard_deny/io/ask_args/ask_rules
    #: (a dangerous flag DENY/ASK still wins), above the ALLOW fallthrough.
    ask_always: bool = False

    def matches_binary(self, binary_path: str) -> bool:
        """Return ``True`` iff this profile matches ``binary_path``.

        Case-insensitive ``fnmatch`` on the forward-slash-normalised path.
        Tries every glob in ``match_globs`` first (any hit → match), then
        falls back to the legacy single ``match_glob``. This lets a
        profile match both the bare binary name (``git``, how the LLM
        writes it) and the full path (``**/git/bin/git.exe``).
        """
        normalized = binary_path.replace("\\", "/").lower()
        globs = list(self.match_globs)
        if self.match_glob:
            globs.append(self.match_glob)
        for glob in globs:
            if glob and fnmatch.fnmatch(normalized, glob.lower()):
                return True
        return False

    def _match_danger(
        self, args: list[str], patterns: list[str]
    ) -> tuple[bool, str]:
        """Return ``(matched, matched_pattern)`` for danger ``patterns``.

        Handles both single-token patterns (``--force``) and
        space-containing patterns (``-c core.sshCommand=``): for the
        latter the pattern's first whitespace piece is the flag and the
        rest is a value-prefix; the flag may appear either as a single
        joined token (``-c=core.sshCommand=x``) or as the flag token
        immediately followed by its value token (``-c`` then
        ``core.sshCommand=x``).
        """
        for pattern in patterns:
            pat = pattern.strip()
            if not pat:
                continue
            pieces = pat.split(None, 1)
            if len(pieces) == 1:
                # Single-token danger flag.
                for arg in args:
                    if _arg_matches_pattern(arg, pat):
                        return (True, pattern)
            else:
                flag, value_prefix = pieces[0], pieces[1]
                fl = flag.lower()
                vp = value_prefix.lower()
                for i, arg in enumerate(args):
                    al = arg.lower()
                    # Joined single token: ``-c core.sshCommand=x`` never
                    # occurs (split on space), but ``--foo=bar`` might.
                    if al == fl or al.startswith(fl + "="):
                        # Value may be in same token after '=' or next arg.
                        after = al.split("=", 1)[1] if "=" in al else ""
                        if after.startswith(vp):
                            return (True, pattern)
                        if i + 1 < len(args) and args[i + 1].lower().startswith(vp):
                            return (True, pattern)
        return (False, "")

    def _match_ask_rules(self, args: list[str]) -> tuple[bool, str]:
        """Return ``(matched, reason)`` for subcommand-aware ``ask_rules``.

        A rule fires when the first positional token (the subcommand)
        equals ``rule["subcommand"]`` AND, if the rule constrains further,
        at least one of its ``any_flags`` / ``positional_any`` is present.
        A rule with neither ``any_flags`` nor ``positional_any`` fires on
        the subcommand alone (inherently destructive subcommand).

        Positional detection ignores flags: the "subcommand" is the first
        non-flag token, and ``positional_any`` is checked against the
        non-flag tokens that follow it — so ``git checkout .`` fires but
        ``git checkout -b foo`` (no ``.``/``--`` positional) does not.
        """
        if not self.ask_rules:
            return (False, "")
        # First non-flag token = subcommand; collect the tokens that follow
        # it. We keep BOTH the following non-flag positionals AND the raw
        # following tokens, because a ``positional_any`` trigger may itself
        # look like a flag (notably ``--``, git's "end of options / paths
        # follow" separator, which is a danger signal for ``checkout --``).
        subcommand = ""
        tokens_after: list[str] = []
        seen_sub = False
        for arg in args:
            if not seen_sub:
                if arg.startswith("-"):
                    continue
                subcommand = arg.lower()
                seen_sub = True
                continue
            tokens_after.append(arg)
        if not subcommand:
            return (False, "")

        for rule in self.ask_rules:
            if not isinstance(rule, dict):
                continue
            rsub = str(rule.get("subcommand", "")).lower()
            if not rsub or rsub != subcommand:
                continue
            any_flags = [str(f) for f in rule.get("any_flags", []) if f]
            positional_any = [str(p) for p in rule.get("positional_any", []) if p]
            reason = str(rule.get("reason", "") or "")

            # No further constraint → subcommand alone fires.
            if not any_flags and not positional_any:
                return (True, reason)

            # any_flags: reuse token-level flag matcher against all args.
            if any_flags:
                matched, _ = self._match_danger(args, any_flags)
                if matched:
                    return (True, reason)

            # positional_any: any following token (positional OR a
            # flag-shaped separator like ``--``) equals a trigger.
            if positional_any:
                trigger_lower = {p.lower() for p in positional_any}
                for tok in tokens_after:
                    if tok.lower() in trigger_lower:
                        return (True, reason)
        return (False, "")

    def classify(
        self,
        args: list[str],
        project_root: str,
        workspace: str = "",
        temp_dir: str = "",
    ) -> tuple[ExecAction, str]:
        """Classify ``args`` into ``ALLOW`` / ``ASK`` / ``DENY`` + reason.

        Order (first match wins):

        1. ``hard_deny_args`` / legacy ``denied_args`` → ``DENY``.
        2. ``io_constraints`` output_dirs escape → ``DENY`` (only qnn_converter
           uses this; input_dirs removed — all profiles now use native hook for
           file-read protection).
        3. ``ask_args`` → ``ASK`` (user decides via permission dialog).
        4. ``ask_rules`` (subcommand-aware) → ``ASK``.
        5. ``ask_always`` → ``ASK`` (binary-level, e.g. curl/reg).
        6. otherwise → ``ALLOW``.

        ``allowed_args`` (deprecated flag-admission list) is no longer
        checked here — all profiles have empty allowed_args and the field
        is kept only for API DTO backward-compat.
        """
        matched, pattern = self._match_danger(
            args, list(self.hard_deny_args) + list(self.denied_args)
        )
        if matched:
            return (
                ExecAction.DENY,
                f"命令被拒绝：参数命中硬拒规则（{pattern}），"
                f"该操作具破坏性且不被允许。",
            )

        io_violation, io_reason = self.check_io_constraints(
            args, project_root, workspace=workspace, temp_dir=temp_dir
        )
        if io_violation:
            return (ExecAction.DENY, io_reason)

        ask_matched, ask_pattern = self._match_danger(args, list(self.ask_args))
        if ask_matched:
            return (
                ExecAction.ASK,
                f"该命令带有高风险参数（{ask_pattern}），可能造成"
                f"破坏性或非预期后果，需要你确认后才能执行。",
            )

        rule_matched, rule_reason = self._match_ask_rules(args)
        if rule_matched:
            return (
                ExecAction.ASK,
                rule_reason
                or "该命令为破坏性操作，需要你确认后才能执行。",
            )

        if self.ask_always:
            return (
                ExecAction.ASK,
                "该命令可访问网络/注册表/执行外部程序等文件守卫无法覆盖的"
                "操作，需要你确认后才能执行。",
            )

        return (ExecAction.ALLOW, "")

    def check_io_constraints(
        self,
        args: list[str],
        project_root: str,
        workspace: str = "",
        temp_dir: str = "",
    ) -> tuple[bool, str]:
        """Return ``(violation, reason)`` for output path args.

        Only ``output_dirs`` is enforced (``input_dirs`` has been removed from
        all profiles — file-read protection is handled by the native OS hook
        which intercepts at the file-system level regardless of the program).
        Expands ``${PROJECT_ROOT}`` / ``${WORKSPACE}`` / ``${TEMP}`` /
        ``${LOCALAPPDATA}`` / ``${APPDATA}`` / ``${USERPROFILE}``.

        Note: as of 2026-07-09 no profile currently populates ``output_dirs``
        either (qnn_converter's output_dirs was removed — native op-mask on
        C:\\Qualcomm already blocks SDK-tree writes at the file-system layer,
        making the command-level check redundant). This method is retained as
        a no-op for now in case a future profile reintroduces output_dirs.
        """
        if not self.io_constraints:
            return (False, "")
        output_dirs = self.io_constraints.get("output_dirs", [])
        if not output_dirs:
            return (False, "")

        def _expand(pattern: str) -> str:
            result = (
                pattern.replace("${PROJECT_ROOT}", project_root)
                .replace("${WORKSPACE}", workspace or project_root)
                .replace("${TEMP}", temp_dir or os.environ.get("TEMP", ""))
            )
            for _var in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
                _placeholder = "${" + _var + "}"
                if _placeholder in result:
                    _val = os.environ.get(_var, "")
                    if _val:
                        result = result.replace(_placeholder, _val)
            return result

        output_path_args: list[str] = []
        _skip_next = False
        _capture_output = False
        for a in args:
            if _skip_next:
                _skip_next = False
                if _capture_output:
                    _capture_output = False
                    _v = a
                    if len(_v) >= 2 and _v[0] == _v[-1] and _v[0] in ('"', "'"):
                        _v = _v[1:-1]
                    if _v:
                        output_path_args.append(_v)
                continue
            al = a.lower()
            if "=" in a:
                _flag_part = al.split("=", 1)[0]
                if _flag_part in _OUTPUT_PATH_FLAGS:
                    _v = a.split("=", 1)[1]
                    if len(_v) >= 2 and _v[0] == _v[-1] and _v[0] in ('"', "'"):
                        _v = _v[1:-1]
                    if _v:
                        output_path_args.append(_v)
                    continue
            if al in _OUTPUT_PATH_FLAGS:
                _skip_next = True
                _capture_output = True
                continue

        def _within(candidate: str, dirs: list[str]) -> bool:
            expanded = [_expand(p) for p in dirs]
            _c = candidate
            if len(_c) >= 2 and _c[0] == _c[-1] and _c[0] in ('"', "'"):
                _c = _c[1:-1]
            normalized = _c.replace("\\", "/")
            for pattern in expanded:
                pat_normalized = pattern.replace("\\", "/")
                if fnmatch.fnmatch(normalized, pat_normalized):
                    return True
                dir_pattern = pat_normalized.rstrip("/*").rstrip("/**")
                if dir_pattern and normalized.lower().startswith(
                    dir_pattern.lower()
                ):
                    return True
            return False

        for out_arg in output_path_args:
            if out_arg and not _within(out_arg, output_dirs):
                return (
                    True,
                    f"输出路径 '{out_arg}' 不在 profile '{self.name}' "
                    f"的 io_constraints.output_dirs 允许范围内。",
                )
        return (False, "")


def extract_binary(command: str) -> str:
    """Extract the binary/executable path from ``command`` (V1 parity)."""
    cmd = command.strip()
    if not cmd:
        return ""
    if cmd.startswith('"'):
        end_quote = cmd.find('"', 1)
        if end_quote > 0:
            return cmd[1:end_quote]
    parts = cmd.split()
    return parts[0] if parts else ""


def extract_args(command: str) -> list[str]:
    """Extract args (after the binary) from ``command`` (V1 parity).

    Truncates at shell operators so piped/chained command args are not
    attributed to this binary (V1 ``_extract_args``).
    """
    cmd = command.strip()
    if not cmd:
        return []
    if cmd.startswith('"'):
        end_quote = cmd.find('"', 1)
        if end_quote > 0:
            remainder = cmd[end_quote + 1:].strip()
            parts = remainder.split() if remainder else []
        else:
            parts = []
    else:
        _all_parts = cmd.split()
        parts = _all_parts[1:] if len(_all_parts) > 1 else []

    result: list[str] = []
    for tok in parts:
        if tok in _SHELL_SEPARATORS:
            break
        if tok.endswith("2>&1") and tok != "2>&1":
            _pre = tok[:-4].rstrip()
            if _pre:
                result.append(_pre)
            break
        result.append(tok)
    return result
