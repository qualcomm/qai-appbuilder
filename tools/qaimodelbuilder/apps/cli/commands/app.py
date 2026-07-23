"""``qai app <pack> [flags]`` — App Builder inference from the CLI.

Desktop App Plan §2.1.1 group L2 (D2 one-shot + D3 REPL). This is the CLI
sibling of the App Builder WebUI: same Packs, same input kinds, same
variants / params / examples surfaced from the live ``PackManifest``, same
``RunAppUseCase`` stream consumed frame-by-frame.

Two surfaces, one engine
------------------------
* **D2 one-shot** — ``qai app whisper-base --audio clip.wav`` runs a single
  inference and renders the result by ``outputSchema.kind`` (or dumps raw
  JSON with ``--json``). Progress goes to stderr (via
  :class:`apps.cli._render.RunFrameRenderer`) so stdout stays a clean pipe.
* **D3 REPL** — ``qai app whisper-base`` on an interactive TTY (no main
  input flag) drops into a long-lived session (one :func:`repl_container`
  for the whole session) where each non-slash line is the next input and
  ``/`` commands tweak model / variant / params / inspect history / export.

Both surfaces funnel through one async helper :func:`_run_once` so the
"build inputs → drive the stream → capture RunResult" path is identical
(judgement criterion 1: no duplicated run logic; judgement criterion 2:
identical observable behaviour to the WebUI run path).

The CLI does **not** run the API lifespan, so every handler that reads the
DB / runs a pack first replays the idempotent factory-Pack seed via
:func:`apps.cli.commands.pack._seed_factory_packs_if_empty` (read-only reuse
of the ``qai pack`` helper — same seed the HTTP lifespan hook calls).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.text import Text

from apps.api.di import Container
from apps.cli import _render
from apps.cli._render import RenderOptions, RunFrameRenderer, RunResult
from apps.cli._render_theme import build_console, icon
from apps.cli._repl import (
    SlashDispatcher,
    async_read_line,
    repl_container,
)
from apps.cli._runtime import run_use_case
from apps.cli._session_log import SessionLog, cleanup_repl_session
from apps.cli.commands.pack import (
    _emit,
    _resolved_config_file,
    _resolved_repo_root,
    _seed_factory_packs_if_empty,
)

__all__ = [
    "register",
    "cmd_app",
    "parse_param_assignments",
    "coerce_param_value",
    "build_inputs",
    "kind_to_input_key",
]


# ---------------------------------------------------------------------------
# argparse registration
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach the ``qai app`` parser to the top-level dispatcher."""

    app = subparsers.add_parser(
        "app",
        help="run an App Builder Pack (one-shot inference or interactive REPL)",
        description=(
            "Run a bundled App Builder Pack against a single input "
            "(one-shot) or drop into an interactive REPL session.\n\n"
            "One-shot: pass exactly one of --image / --audio / --text "
            "matching the Pack's input kind. Use '-' to read the value "
            "from stdin (binary for image/audio, text for --text).\n\n"
            "REPL: omit the input flag on an interactive terminal to open "
            "a session where each line is the next input and /commands "
            "tweak the model / variant / params (/help for the list).\n\n"
            "Omit <pack> entirely to list the available Packs."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    app.add_argument(
        "pack",
        nargs="?",
        default=None,
        metavar="<pack>",
        help="Pack id, e.g. 'whisper-base'. Omit to list available Packs.",
    )
    app.add_argument(
        "--image",
        metavar="<path>",
        help="image input path (or '-' to read binary image from stdin)",
    )
    app.add_argument(
        "--audio",
        metavar="<path>",
        help="audio input path (or '-' to read binary audio from stdin)",
    )
    app.add_argument(
        "--text",
        metavar="<str>",
        help="text input (or '-' to read the text from stdin)",
    )
    app.add_argument(
        "--variant",
        metavar="<id>",
        default=None,
        help="variant id to run (see the Pack manifest for choices)",
    )
    app.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="key=val",
        help="set a runtime param (repeatable); types coerced from manifest",
    )
    app.add_argument(
        "--json",
        action="store_true",
        help="dump the raw output JSON to stdout (skip the human render)",
    )
    app.add_argument(
        "--out",
        metavar="<path>",
        default=None,
        help=(
            "write output: copies the produced audio/image artifact to "
            "<path>, otherwise writes the output dict as JSON"
        ),
    )
    app.add_argument(
        "--save-annotated",
        metavar="<path>",
        default=None,
        help="copy an annotated/visualised artifact to <path> if produced",
    )
    app.set_defaults(handler=cmd_app)


# ---------------------------------------------------------------------------
# pure helpers (unit-testable; no IO, no container)
# ---------------------------------------------------------------------------


_KIND_TO_KEY: dict[str, str] = {
    "image": "image",
    "audio": "audio",
    "text": "text",
}


def kind_to_input_key(kind: str | None) -> str | None:
    """Map an ``inputSchema.kind`` to its inputs-dict key.

    ``image`` → ``"image"``, ``audio`` → ``"audio"``, ``text`` → ``"text"``.
    Returns ``None`` for kinds the CLI does not accept as a main flag
    (``json`` / ``video`` etc.), so the caller can surface a clear message.
    """

    if kind is None:
        return None
    return _KIND_TO_KEY.get(kind.lower())


def parse_param_assignments(assignments: list[str]) -> dict[str, str]:
    """Parse ``["k=v", ...]`` into ``{"k": "v"}`` (string values).

    Raises :class:`ValueError` for a token missing ``=`` so the handler can
    return a usage error (exit 2). Values keep their raw string form here;
    manifest-aware type coercion happens in :func:`coerce_param_value`.
    """

    out: dict[str, str] = {}
    for raw in assignments:
        if "=" not in raw:
            raise ValueError(
                f"invalid --param {raw!r}: expected key=val"
            )
        key, _, value = raw.partition("=")
        key = key.strip()
        if not key:
            raise ValueError(f"invalid --param {raw!r}: empty key")
        out[key] = value
    return out


def coerce_param_value(value: str, param_type: str | None) -> object:
    """Best-effort coerce a string param value by its manifest ``type``.

    ``bool`` → ``true/1/yes/on`` (case-insensitive) → ``True``; ``int`` /
    ``float`` parse numerically (falling back to the raw string if the parse
    fails, so a typo surfaces downstream as a runner ``INVALID_INPUT`` rather
    than a CLI crash); everything else stays a string.
    """

    t = (param_type or "").lower()
    if t in ("bool", "boolean"):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if t in ("int", "integer"):
        try:
            return int(value)
        except ValueError:
            return value
    if t in ("float", "number"):
        try:
            return float(value)
        except ValueError:
            return value
    return value


def build_inputs(
    *,
    input_key: str,
    input_value: object,
    variant_id: str | None,
    params: dict[str, object],
    no_cache: bool = True,
) -> dict[str, object]:
    """Assemble the ``RunAppUseCase`` inputs dict.

    Shape mirrors the WebUI run payload: the main value under its kind key,
    optional ``variant_id`` / ``params``, and an ``options`` block. ``noCache``
    defaults to True for the CLI so a one-shot always re-runs (an operator
    expects ``qai app ...`` to actually invoke the model, not echo a cache).
    """

    inputs: dict[str, object] = {input_key: input_value}
    if variant_id:
        inputs["variant_id"] = variant_id
    if params:
        inputs["params"] = dict(params)
    inputs["options"] = {"noCache": no_cache}
    return inputs


# ---------------------------------------------------------------------------
# manifest helpers
# ---------------------------------------------------------------------------


def _manifest_input_kind(manifest: Any) -> str | None:
    schema = getattr(manifest, "input_schema", None)
    if schema is None:
        return None
    return getattr(schema, "kind", None)


def _manifest_output_kind(manifest: Any) -> str | None:
    schema = getattr(manifest, "output_schema", None)
    if schema is None:
        return None
    return getattr(schema, "kind", None)


def _manifest_param_types(manifest: Any) -> dict[str, str | None]:
    types: dict[str, str | None] = {}
    for param in getattr(manifest, "params", ()) or ():
        name = getattr(param, "name", None)
        if name is not None:
            types[str(name)] = getattr(param, "type", None)
    return types


def _coerce_params_with_manifest(
    raw: dict[str, str], manifest: Any
) -> dict[str, object]:
    """Coerce raw ``key=val`` strings using the manifest param types."""

    types = _manifest_param_types(manifest)
    return {k: coerce_param_value(v, types.get(k)) for k, v in raw.items()}


def _hint_pack_expectations(manifest: Any, err: Any) -> None:
    """Print what the Pack expects (input kind / variants / params) to err."""

    kind = _manifest_input_kind(manifest)
    err.write(
        f"Pack '{getattr(manifest, 'model_id', '?')}' "
        f"({getattr(manifest, 'display_name', '')})\n"
    )
    if kind:
        err.write(f"  输入类型: {kind}（用 --{kind_to_input_key(kind) or kind}）\n")
    variants = getattr(manifest, "variants", ()) or ()
    if variants:
        ids = ", ".join(str(getattr(v, "id", "")) for v in variants)
        err.write(f"  可用变体 (--variant): {ids}\n")
    params = getattr(manifest, "params", ()) or ()
    if params:
        err.write("  可用参数 (--param key=val):\n")
        for p in params:
            label = getattr(p, "label", None) or getattr(p, "name", "")
            err.write(
                f"    {getattr(p, 'name', '')} ({getattr(p, 'type', '?')}) "
                f"- {label}\n"
            )
    err.flush()


# ---------------------------------------------------------------------------
# shared run engine (D2 + D3 funnel through here)
# ---------------------------------------------------------------------------


async def _run_once(
    c: Container,
    model_id_str: str,
    inputs: dict[str, object],
    opts: RenderOptions,
    *,
    err: Any = None,
    renderer: RunFrameRenderer | None = None,
) -> RunResult:
    """Drive one ``RunAppUseCase`` stream → :class:`RunResult`.

    The single source of truth for "run a pack and capture its outcome",
    shared by the one-shot handler and the REPL turn loop. Progress frames
    render to ``err`` (stderr by default); the result / error are captured on
    the returned :class:`RunResult`. A caller that needs to force-stop a
    live progress bar from outside (e.g. REPL exit cleanup) can build the
    ``renderer`` itself and pass it in; otherwise one is built internally.
    """

    from qai.app_builder.domain.value_objects import AppModelId  # noqa: PLC0415

    err = err if err is not None else sys.stderr
    result = RunResult()
    renderer = renderer if renderer is not None else RunFrameRenderer(opts, err=err)

    # ``execute`` is an ``async def`` returning the async iterator.
    iterator = await c.app_builder.run_app_use_case.execute(
        model_id=AppModelId(value=model_id_str),
        inputs=inputs,
    )
    async for frame in iterator:
        renderer.consume(frame.payload, result)
    return result


def _render_result(
    result: RunResult,
    manifest: Any,
    opts: RenderOptions,
    *,
    as_json: bool,
    out_path: str | None,
    save_annotated: str | None,
    out: Any = None,
    err: Any = None,
) -> int:
    """Render a captured :class:`RunResult`; return the process exit code."""

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    if result.error_code:
        err.write(
            _render.friendly_error(result.error_code, result.error_message)
            + "\n"
        )
        err.flush()
        return 1

    if as_json:
        out.write(
            json.dumps(result.output or {}, ensure_ascii=False, indent=2) + "\n"
        )
        out.flush()
    else:
        _render.render_run_output(
            result.output,
            _manifest_output_kind(manifest),
            opts,
            out=out,
        )

    if out_path:
        _write_output_artifact(result.output, out_path, err=err)
    if save_annotated:
        _copy_annotated_artifact(result.output, save_annotated, err=err)
    return 0


def _write_output_artifact(
    output: dict[str, Any] | None, out_path: str, *, err: Any
) -> None:
    """``--out``: copy a produced audio/image artifact, else dump JSON."""

    dest = Path(out_path)
    artifact = None
    if isinstance(output, dict):
        artifact = (
            output.get("audio_path")
            or output.get("image_path")
            or output.get("path")
        )
    if artifact and Path(str(artifact)).is_file():
        shutil.copyfile(str(artifact), dest)
        err.write(f"已写入产物: {dest}\n")
    else:
        dest.write_text(
            json.dumps(output or {}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        err.write(f"已写入输出 JSON: {dest}\n")
    err.flush()


def _copy_annotated_artifact(
    output: dict[str, Any] | None, dest_path: str, *, err: Any
) -> None:
    """``--save-annotated``: copy an annotated/visualised artifact if any."""

    artifact = None
    if isinstance(output, dict):
        artifact = (
            output.get("annotated_path")
            or output.get("annotated_image_path")
            or output.get("visualization_path")
        )
    if artifact and Path(str(artifact)).is_file():
        shutil.copyfile(str(artifact), Path(dest_path))
        err.write(f"已写入标注产物: {dest_path}\n")
    else:
        err.write("未生成标注产物（该 Pack 不产出标注图）\n")
    err.flush()


# ---------------------------------------------------------------------------
# stdin handling for ``-`` flag values
# ---------------------------------------------------------------------------


def _read_text_stdin() -> str:
    return sys.stdin.read()


def _read_binary_stdin_to_tempfile(suffix: str) -> str:
    """Read binary stdin into a NamedTemporaryFile, return its path."""

    data = sys.stdin.buffer.read()
    fd = tempfile.NamedTemporaryFile(  # noqa: SIM115 — kept for process life
        prefix="qai-app-", suffix=suffix, delete=False
    )
    try:
        fd.write(data)
        fd.flush()
    finally:
        fd.close()
    return fd.name


def _resolve_main_input(
    input_key: str, flag_value: str
) -> object:
    """Resolve a main-input flag value, expanding ``-`` to stdin.

    For ``text`` the resolved value is the raw string; for ``image`` /
    ``audio`` it is a filesystem path (a temp file when read from stdin).
    """

    if input_key == "text":
        return _read_text_stdin() if flag_value == "-" else flag_value
    # image / audio: value is a path; '-' streams binary stdin to a temp file.
    if flag_value == "-":
        suffix = ".png" if input_key == "image" else ".wav"
        return _read_binary_stdin_to_tempfile(suffix)
    # Strip surrounding quotes (common when pasting an Explorer "copy as path").
    return flag_value.strip().strip('"').strip("'")


# ---------------------------------------------------------------------------
# one-shot handler (D2)
# ---------------------------------------------------------------------------


def _selected_main_flag(args: argparse.Namespace) -> tuple[str, str] | None:
    """Return the ``(input_key, value)`` of the single provided input flag.

    ``None`` when no main input flag was given (→ REPL / list mode). Raises
    :class:`ValueError` when more than one is given (mutually exclusive).
    """

    provided: list[tuple[str, str]] = []
    if args.image is not None:
        provided.append(("image", args.image))
    if args.audio is not None:
        provided.append(("audio", args.audio))
    if args.text is not None:
        provided.append(("text", args.text))
    if not provided:
        return None
    if len(provided) > 1:
        keys = ", ".join(f"--{k}" for k, _ in provided)
        raise ValueError(f"only one input flag allowed, got: {keys}")
    return provided[0]


def cmd_app(args: argparse.Namespace) -> int:
    """``qai app`` dispatcher: list / one-shot / REPL."""

    opts = RenderOptions.from_streams(sys.stdout, sys.stderr)

    # No pack id → list available Packs.
    if not args.pack:
        return _cmd_list_packs(args)

    # Determine the main input flag (mutually exclusive).
    try:
        selected = _selected_main_flag(args)
    except ValueError as exc:
        sys.stderr.write(f"qai app: {exc}\n")
        return 2

    # No main input flag:
    #   - interactive TTY → REPL session
    #   - non-TTY (pipe / CI)  → usage error (no input to run)
    if selected is None:
        if sys.stdin.isatty():
            return _run_repl(args, opts)
        sys.stderr.write(
            "qai app: no input provided. Pass --image / --audio / --text "
            "(or '-' for stdin), or run on an interactive terminal for the "
            "REPL.\n"
        )
        return 2

    input_key, flag_value = selected

    # Parse params up-front (usage error → exit 2 before touching the DB).
    try:
        raw_params = parse_param_assignments(args.param)
    except ValueError as exc:
        sys.stderr.write(f"qai app: {exc}\n")
        return 2

    async def _go(c: Container) -> int:
        from qai.app_builder.domain.errors import (  # noqa: PLC0415
            AppModelNotFoundError as _AppModelNotFoundError,
        )

        await _seed_factory_packs_if_empty(c)

        manifest = await _load_manifest(c, args.pack)

        # Validate the provided flag matches the Pack's input kind; print a
        # helpful hint listing what the Pack expects when it doesn't.
        expected_key = (
            kind_to_input_key(_manifest_input_kind(manifest))
            if manifest is not None
            else None
        )
        if manifest is not None and expected_key and expected_key != input_key:
            sys.stderr.write(
                f"qai app: Pack '{args.pack}' expects --{expected_key} input, "
                f"got --{input_key}.\n"
            )
            _hint_pack_expectations(manifest, sys.stderr)
            return 2

        params = (
            _coerce_params_with_manifest(raw_params, manifest)
            if manifest is not None
            else dict(raw_params)
        )
        try:
            main_value = _resolve_main_input(input_key, flag_value)
        except OSError as exc:
            sys.stderr.write(f"qai app: cannot read input: {exc}\n")
            return 2

        inputs = build_inputs(
            input_key=input_key,
            input_value=main_value,
            variant_id=args.variant,
            params=params,
        )
        try:
            result = await _run_once(c, args.pack, inputs, opts)
        except _AppModelNotFoundError:
            sys.stderr.write(
                f"qai app: 未找到 Pack '{args.pack}'。"
                f"运行 `qai app`（不带参数）查看可用 Pack。\n"
            )
            return 1
        return _render_result(
            result,
            manifest,
            opts,
            as_json=args.json,
            out_path=args.out,
            save_annotated=args.save_annotated,
        )

    return run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )


async def _load_manifest(c: Container, pack: str) -> Any:
    """Load a Pack manifest, tolerating a stripped container (returns None)."""

    from qai.app_builder.domain.value_objects import AppModelId  # noqa: PLC0415

    uc = c.app_builder.get_pack_manifest_use_case
    if uc is None:
        return None
    try:
        return await uc.execute(AppModelId(value=pack))
    except Exception:  # noqa: BLE001 — unknown id / not wired; run still tries
        return None


def _pack_dir(c: Container, pack: str) -> Path | None:
    """Resolve a Pack's on-disk directory ``<repo_root>/factory/app_builder/models/<pack>``.

    Mirrors the lifespan seed root (``_resolve_seed_pack_root``). Returns
    ``None`` if it cannot be located so callers can degrade gracefully.
    """

    repo_root = getattr(c, "repo_root", None)
    if repo_root is None:
        return None
    candidate = Path(repo_root) / "factory" / "app_builder" / "models" / pack
    return candidate if candidate.is_dir() else None


_FILE_INPUT_KEYS = ("image", "audio", "video")


def _resolve_example_inputs(
    raw_inputs: dict[str, Any], pack_dir: Path | None
) -> tuple[dict[str, Any], list[str]]:
    """Resolve a manifest example's file inputs to absolute Pack-relative paths.

    Manifest examples store inputs like ``{"image": "examples/doc.jpg"}`` —
    a path relative to the Pack directory. The runner's CWD is NOT the Pack
    dir, so the raw relative path fails with "input not found". This resolves
    each file-typed input (image/audio/video) against ``pack_dir`` and reports
    any that do not exist on disk (Pack examples are sometimes scaffold-only).

    Returns ``(resolved_inputs, missing)`` where ``missing`` lists the
    ``"<key>=<path>"`` of example files that are absent.
    """

    resolved = dict(raw_inputs)
    missing: list[str] = []
    for key in _FILE_INPUT_KEYS:
        val = resolved.get(key)
        if not isinstance(val, str) or not val:
            continue
        p = Path(val)
        if not p.is_absolute() and pack_dir is not None:
            p = (pack_dir / val).resolve()
        if not p.exists():
            missing.append(f"{key}={p}")
        resolved[key] = str(p)
    return resolved, missing



def _cmd_list_packs(args: argparse.Namespace) -> int:
    """List the available Packs (id / title / input kind) as JSON on stdout."""

    async def _go(c: Container) -> dict[str, Any]:
        await _seed_factory_packs_if_empty(c)
        models = await c.app_builder.list_app_models_use_case.execute(
            include_disabled=True,
        )
        return {
            "packs": [
                {
                    "id": str(m.id),
                    "title": m.title,
                    "enabled": m.enabled,
                }
                for m in models
            ]
        }

    payload = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit(payload)
    return 0


# ---------------------------------------------------------------------------
# REPL session (D3)
# ---------------------------------------------------------------------------


class _AppReplState:
    """Mutable per-session state for the App Builder REPL."""

    __slots__ = (
        "pack",
        "manifest",
        "variant",
        "params",
        "last_result",
        "active_renderer",
    )

    def __init__(self, pack: str) -> None:
        self.pack = pack
        self.manifest: Any = None
        self.variant: str | None = None
        self.params: dict[str, object] = {}
        self.last_result: RunResult | None = None
        #: The in-flight turn's ``RunFrameRenderer`` (set only while a turn is
        #: streaming), so REPL exit cleanup can force-stop a live progress
        #: bar left running by an exception escaping mid-turn.
        self.active_renderer: RunFrameRenderer | None = None


def _run_repl(args: argparse.Namespace, opts: RenderOptions) -> int:
    """Synchronous entry: drive the async REPL session to completion."""

    return asyncio.run(_repl_main(args, opts))


async def _repl_main(args: argparse.Namespace, opts: RenderOptions) -> int:
    state = _AppReplState(args.pack)

    async with repl_container(
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    ) as c:
        # ── Session log tees every themed-console print from here on into
        #    <cli_sessions_dir>/<session_id>.log; no existing session-id
        #    concept threads through this REPL (unlike `qai build`'s
        #    conversation id), so a fresh id is minted for the log filename.
        session_log = SessionLog(c.data_paths, uuid.uuid4().hex)

        await _seed_factory_packs_if_empty(c)
        state.manifest = await _load_manifest(c, state.pack)

        dispatcher = _build_repl_dispatcher(c, state, opts)
        _print_repl_banner(state, opts)

        try:
            while True:
                try:
                    line = await async_read_line(f"app({state.pack}) › ")
                except EOFError:
                    _out_console(opts).print(Text(""))
                    break
                except KeyboardInterrupt:
                    # First Ctrl+C at the prompt: cancel the line, keep running.
                    warn = icon("warning", emoji=opts.emoji)
                    prefix = f"{warn} " if warn else ""
                    _out_console(opts).print(
                        Text(f"\n{prefix}(已取消，/exit 退出)", style="warning")
                    )
                    continue

                line = line.strip()
                if not line:
                    continue

                handled, keep_running = await dispatcher.dispatch(line)
                if handled:
                    if not keep_running:
                        break
                    continue

                # Non-slash line → main input value for one inference turn.
                await _repl_run_turn(c, state, line, opts)
        finally:
            cleanup_repl_session(session_log, active_renderer=state.active_renderer)

    return 0


def _out_console(opts: RenderOptions) -> Console:
    return build_console(color=opts.color, emoji=opts.emoji, stream=sys.stdout)


def _print_repl_banner(state: _AppReplState, opts: RenderOptions) -> None:
    kind = _manifest_input_kind(state.manifest) or "?"
    console = _out_console(opts)
    console.print(
        Text(f"App Builder 会话 — Pack: {state.pack} (输入类型: {kind})", style="heading")
    )
    console.print(Text("直接输入内容（路径或文本）回车运行；/help 查看命令，/exit 退出。"))


async def _repl_run_turn(
    c: Container, state: _AppReplState, line: str, opts: RenderOptions
) -> None:
    """Run one inference turn from a non-slash REPL line."""

    kind = _manifest_input_kind(state.manifest)
    input_key = kind_to_input_key(kind) or "text"
    value = line
    # For file inputs, users commonly paste a quoted path (e.g. from "Copy as
    # path" in Explorer); strip surrounding quotes so it resolves. Text input
    # is passed verbatim (quotes may be meaningful).
    if input_key in _FILE_INPUT_KEYS:
        value = value.strip().strip('"').strip("'")
        p = Path(value)
        if not p.exists():
            cross = icon("error", emoji=opts.emoji)
            prefix = f"{cross} " if cross else ""
            _out_console(opts).print(
                Text(
                    f"{prefix}找不到文件: {value}\n"
                    "请输入存在的文件路径（可把文件拖入终端，或用 Explorer“复制为路径”）。",
                    style="error",
                )
            )
            return
    inputs = build_inputs(
        input_key=input_key,
        input_value=value,
        variant_id=state.variant,
        params=state.params,
    )
    renderer = RunFrameRenderer(opts, err=sys.stderr)
    state.active_renderer = renderer
    try:
        result = await _run_once(c, state.pack, inputs, opts, renderer=renderer)
    except KeyboardInterrupt:
        state.active_renderer = None
        _out_console(opts).print(Text("\n(本轮已中断)", style="warning"))
        return
    state.active_renderer = None
    state.last_result = result
    _render_result(
        result,
        state.manifest,
        opts,
        as_json=False,
        out_path=None,
        save_annotated=None,
    )


def _build_repl_dispatcher(
    c: Container, state: _AppReplState, opts: RenderOptions
) -> SlashDispatcher:
    """Wire the ``/`` commands for the App Builder REPL."""

    dispatcher = SlashDispatcher(console=_out_console(opts))

    async def _model(rest: str) -> bool:
        new_pack = rest.strip()
        console = _out_console(opts)
        if not new_pack:
            console.print(Text(f"当前 Pack: {state.pack}"))
            return True
        state.pack = new_pack
        state.manifest = await _load_manifest(c, new_pack)
        state.variant = None
        ok = icon("success", emoji=opts.emoji)
        prefix = f"{ok} " if ok else ""
        console.print(Text(f"{prefix}已切换 Pack: {new_pack}", style="success"))
        _print_repl_banner(state, opts)
        return True

    async def _variant(rest: str) -> bool:
        state.variant = rest.strip() or None
        _out_console(opts).print(Text(f"变体: {state.variant or '(默认)'}"))
        return True

    async def _param(rest: str) -> bool:
        try:
            raw = parse_param_assignments([rest.strip()] if rest.strip() else [])
        except ValueError as exc:
            cross = icon("error", emoji=opts.emoji)
            prefix = f"{cross} " if cross else ""
            _out_console(opts).print(Text(f"{prefix}{exc}", style="error"))
            return True
        coerced = _coerce_params_with_manifest(raw, state.manifest)
        state.params.update(coerced)
        _out_console(opts).print(
            Text(f"参数: {json.dumps(state.params, ensure_ascii=False)}")
        )
        return True

    async def _params(rest: str) -> bool:
        _out_console(opts).print(
            Text(
                f"当前参数: {json.dumps(state.params, ensure_ascii=False)}\n"
                f"当前变体: {state.variant or '(默认)'}"
            )
        )
        return True

    async def _examples(rest: str) -> bool:
        examples = list(getattr(state.manifest, "examples", ()) or ())
        console = _out_console(opts)
        if not examples:
            console.print(Text("该 Pack 没有内置示例。", style="dim"))
            return True
        choice = rest.strip()
        if not choice:
            for i, ex in enumerate(examples):
                console.print(Text(f"  [{i}] {getattr(ex, 'name', '')}"))
            console.print(
                Text(
                    "用 /examples <序号> 运行内置示例，\n"
                    "或 /examples <序号> <你的文件路径> 用该示例的参数跑你自己的输入。"
                )
            )
            return True
        # Allow "<idx>" or "<idx> <override-input-path-or-text>".
        parts = choice.split(None, 1)
        try:
            idx = int(parts[0])
            ex = examples[idx]
        except (ValueError, IndexError):
            cross = icon("error", emoji=opts.emoji)
            prefix = f"{cross} " if cross else ""
            console.print(
                Text(
                    f"{prefix}无效的示例序号: {parts[0]!r}（用 /examples 查看可用序号）",
                    style="error",
                )
            )
            return True
        override = parts[1].strip().strip('"').strip("'") if len(parts) > 1 else ""

        ex_inputs: dict[str, Any] = dict(getattr(ex, "inputs", {}) or {})
        kind = _manifest_input_kind(state.manifest)
        input_key = kind_to_input_key(kind)

        if override:
            # Run THIS example's params/variant against the user's own input.
            if input_key is None:
                console.print(
                    Text(
                        "该 Pack 的输入类型不支持命令行覆盖，请直接用 /param 调参。",
                        style="dim",
                    )
                )
                return True
            ex_inputs[input_key] = override
        else:
            # Resolve the example's bundled file path(s) relative to the Pack
            # directory, and warn clearly if the bundled sample is missing.
            pack_dir = _pack_dir(c, state.pack)
            ex_inputs, missing = _resolve_example_inputs(ex_inputs, pack_dir)
            if missing:
                warn = icon("warning", emoji=opts.emoji)
                prefix = f"{warn} " if warn else ""
                console.print(
                    Text(
                        f"{prefix}内置示例 [{idx}] 的样例文件未随 Pack 附带：\n"
                        + "".join(f"  缺失 {m}\n" for m in missing)
                        + "可改用自己的文件运行该示例的参数：\n"
                        + f"  /examples {idx} <你的文件路径>\n"
                        + "或直接在提示符输入你的文件路径回车运行。",
                        style="warning",
                    )
                )
                return True

        ex_inputs.setdefault("options", {"noCache": True})
        if getattr(ex, "params_override", None):
            ex_inputs.setdefault("params", dict(ex.params_override))
        elif state.params:
            ex_inputs.setdefault("params", dict(state.params))
        if state.variant:
            ex_inputs.setdefault("variant_id", state.variant)
        result = await _run_once(c, state.pack, ex_inputs, opts)
        state.last_result = result
        _render_result(
            result, state.manifest, opts,
            as_json=False, out_path=None, save_annotated=None,
        )
        return True

    async def _history(rest: str) -> bool:
        uc = c.app_builder.list_runs_use_case
        console = _out_console(opts)
        if uc is None:
            console.print(Text("运行历史不可用（容器未接入 list_runs）。", style="dim"))
            return True
        runs = await uc.execute(limit=20, offset=0)
        if not runs:
            console.print(Text("暂无运行历史。", style="dim"))
            return True
        for run in runs:
            console.print(
                Text(
                    f"  {getattr(run, 'id', '?')}  "
                    f"{getattr(run, 'model_id', '?')}  "
                    f"{getattr(run, 'status', '?')}"
                )
            )
        return True

    async def _last(rest: str) -> bool:
        if state.last_result is None:
            _out_console(opts).print(Text("还没有运行结果。", style="dim"))
            return True
        _render_result(
            state.last_result, state.manifest, opts,
            as_json=False, out_path=None, save_annotated=None,
        )
        return True

    async def _out(rest: str) -> bool:
        if state.last_result is None or state.last_result.output is None:
            _out_console(opts).print(Text("没有可导出的输出。", style="dim"))
            return True
        dest = rest.strip()
        if not dest:
            _out_console(opts).print(Text("用法: /out <path>"))
            return True
        _write_output_artifact(state.last_result.output, dest, err=sys.stdout)
        return True

    async def _help(rest: str) -> bool:
        _out_console(opts).print(Text(dispatcher.render_help()))
        return True

    async def _exit(rest: str) -> bool:
        return False  # keep_running=False → leave the loop

    dispatcher.register("model", "切换 Pack: /model <pack>", _model)
    dispatcher.register("variant", "设置变体: /variant <id>", _variant)
    dispatcher.register("param", "设置参数: /param key=val", _param)
    dispatcher.register("params", "查看当前参数/变体", _params)
    dispatcher.register("examples", "列出/运行内置示例: /examples [序号]", _examples)
    dispatcher.register("history", "查看运行历史", _history)
    dispatcher.register("last", "重新打印上次输出", _last)
    dispatcher.register("out", "导出上次输出: /out <path>", _out)
    dispatcher.register("help", "查看命令列表", _help)
    dispatcher.register("exit", "退出会话", _exit, aliases=("quit",))
    return dispatcher
