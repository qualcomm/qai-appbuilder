# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``qai config`` subcommands — user-prefs documents + cloud-provider wizard.

Desktop App Plan §2.1.1 group A + cli-interactive-design §4bis. Two surfaces:

* ``qai config get/set`` — thin wrappers over
  :class:`qai.user_prefs.application.use_cases.LoadDocumentUseCase` /
  :class:`SaveDocumentUseCase` (unchanged from D1).
* ``qai config setup`` + ``qai config provider {list|add|edit|remove|test}``
  — the interactive cloud-model provider configuration wizard. This replaced
  the removed ``qai model provider …`` / ``qai model cloud-list`` commands
  (CLI does not manage on-device LLMs; see design §4bis).

Provider configs are written through the model_catalog
:class:`UpdateProviderConfigUseCase`, which (post D0.5.0) extracts the
``api_key`` into the SecretStore and strips it from the persisted config —
so the CLI never writes a plaintext key and shares the exact same store the
WebUI Chat surface + chat inference read from (unified config, §4bis.4).
Connectivity is verified via :class:`ProbeProviderUseCase` (real request,
truth-from-real-state).

Behavioural contract (get/set)
------------------------------
* ``qai config get <key>`` — prints the document JSON to ``stdout``.
* ``qai config set <key> <json-value>`` — shallow-merge a JSON object.

Provider commands degrade gracefully when not on a TTY: ``add``/``edit``
accept ``--type`` / ``--base-url`` / ``--default-model`` flags and read the
api_key from stdin (``--api-key-stdin``) so they are scriptable / CI-safe and
the key never appears in argv / shell history.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from apps.api.di import Container
from apps.cli._runtime import cli_container, run_use_case

__all__ = [
    "register",
    "cmd_config_get",
    "cmd_config_set",
    "cmd_config_setup",
    "cmd_provider_list",
    "cmd_provider_add",
    "cmd_provider_edit",
    "cmd_provider_remove",
    "cmd_provider_test",
]


# ---------------------------------------------------------------------------
# Provider type presets (default endpoints) — mirrors the WebUI wizard.
# ---------------------------------------------------------------------------

_PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "anthropic": {
        "label": "Anthropic (Claude)",
        "base_url": "https://api.anthropic.com",
        "default_model": "claude-4-6-sonnet",
    },
    "openai_compat": {
        "label": "OpenAI 兼容",
        "base_url": "https://api.openai.com",
        "default_model": "gpt-4o",
    },
    "ollama": {
        "label": "Ollama",
        "base_url": "http://localhost:11434",
        "default_model": "",
    },
    "generic_cloud": {
        "label": "通用云端",
        "base_url": "",
        "default_model": "",
    },
}


# ---------------------------------------------------------------------------
# argparse registration
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach ``qai config`` subparsers to the top-level dispatcher."""

    config = subparsers.add_parser(
        "config",
        help="user preferences + cloud-provider configuration wizard",
        description=(
            "Read/update persisted user-preference documents (the same "
            "store the WebUI Settings page edits) and run the interactive "
            "cloud-model provider configuration wizard."
        ),
    )
    config_sub = config.add_subparsers(
        dest="config_command", required=True, metavar="<subcommand>"
    )

    # ── get / set (unchanged) ───────────────────────────────────────────
    get_p = config_sub.add_parser(
        "get", help="print the JSON document for <key> (or {} if absent)"
    )
    get_p.add_argument("key", help="prefs document key, e.g. 'ui' or 'forge'")
    get_p.set_defaults(handler=cmd_config_get)

    set_p = config_sub.add_parser(
        "set",
        help="shallow-merge the given JSON object into the document at <key>",
    )
    set_p.add_argument("key", help="prefs document key, e.g. 'ui' or 'forge'")
    set_p.add_argument(
        "value",
        help=(
            "JSON object to merge into the document, e.g. "
            "'{\"theme\":\"dark\"}'"
        ),
    )
    set_p.set_defaults(handler=cmd_config_set)

    # ── setup wizard ────────────────────────────────────────────────────
    setup_p = config_sub.add_parser(
        "setup",
        help="interactive one-stop configuration wizard (providers + prefs)",
    )
    setup_p.set_defaults(handler=cmd_config_setup)

    # ── provider group ──────────────────────────────────────────────────
    provider_p = config_sub.add_parser(
        "provider",
        help="cloud-model providers (list/add/edit/remove/test)",
        description=(
            "Configure the cloud AI providers used by Chat / Model Builder. "
            "api_key is stored in the OS-keyring-backed SecretStore (never "
            "written to disk in plaintext) and shared with the WebUI."
        ),
    )
    provider_sub = provider_p.add_subparsers(
        dest="provider_command", required=True, metavar="<subcommand>"
    )

    list_p = provider_sub.add_parser(
        "list", help="list configured providers (api_key masked)"
    )
    list_p.set_defaults(handler=cmd_provider_list)

    add_p = provider_sub.add_parser(
        "add", help="add a provider (wizard on a TTY, flags otherwise)"
    )
    _add_provider_flags(add_p)
    add_p.set_defaults(handler=cmd_provider_add)

    edit_p = provider_sub.add_parser(
        "edit", help="edit an existing provider"
    )
    edit_p.add_argument("provider_id", help="provider id to edit")
    _add_provider_flags(edit_p, require_id_arg=False)
    edit_p.set_defaults(handler=cmd_provider_edit)

    remove_p = provider_sub.add_parser(
        "remove", help="remove a provider config (and its stored key)"
    )
    remove_p.add_argument("provider_id", help="provider id to remove")
    remove_p.add_argument(
        "--yes",
        action="store_true",
        help="confirm removal (required: CLI is non-interactive by default)",
    )
    remove_p.set_defaults(handler=cmd_provider_remove)

    test_p = provider_sub.add_parser(
        "test", help="probe a provider's connectivity (real request)"
    )
    test_p.add_argument("provider_id", help="provider id to test")
    test_p.set_defaults(handler=cmd_provider_test)


def _add_provider_flags(
    parser: argparse.ArgumentParser, *, require_id_arg: bool = True
) -> None:
    if require_id_arg:
        parser.add_argument(
            "provider_id",
            nargs="?",
            default=None,
            help="provider id (prompted if omitted on a TTY)",
        )
    parser.add_argument(
        "--type",
        dest="provider_type",
        choices=sorted(_PROVIDER_PRESETS),
        default=None,
        help="provider type preset (anthropic/openai_compat/ollama/generic_cloud)",
    )
    parser.add_argument(
        "--base-url", dest="base_url", default=None, help="API base URL"
    )
    parser.add_argument(
        "--default-model",
        dest="default_model",
        default=None,
        help="default model id for this provider",
    )
    parser.add_argument(
        "--api-key-stdin",
        dest="api_key_stdin",
        action="store_true",
        help="read api_key from stdin (never appears in argv / history)",
    )
    parser.add_argument(
        "--no-test",
        dest="no_test",
        action="store_true",
        help="skip the post-save connectivity probe",
    )


# ---------------------------------------------------------------------------
# get / set handlers (unchanged from D1)
# ---------------------------------------------------------------------------


def _emit_doc(doc: Any) -> None:
    sys.stdout.buffer.write(
        json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")
    )
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()


def _runtime_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "config_file": getattr(args, "config_file", None),
        "repo_root": getattr(args, "repo_root", None),
    }


def cmd_config_get(args: argparse.Namespace) -> int:
    async def _go(c: Container) -> dict[str, Any]:
        return await c.user_prefs.load_document_use_case.execute(args.key)

    _emit_doc(run_use_case(_go, **_runtime_kwargs(args)))
    return 0


def cmd_config_set(args: argparse.Namespace) -> int:
    try:
        parsed = json.loads(args.value)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"invalid JSON: {exc}\n")
        return 2
    if not isinstance(parsed, dict):
        sys.stderr.write(
            "invalid JSON: top-level value must be a JSON object "
            f"(got {type(parsed).__name__})\n"
        )
        return 2

    async def _go(c: Container) -> dict[str, Any]:
        return await c.user_prefs.save_document_use_case.execute(
            args.key, updates=parsed
        )

    _emit_doc(run_use_case(_go, **_runtime_kwargs(args)))
    return 0


# ---------------------------------------------------------------------------
# Interactive helpers (prompt_toolkit; degrade to input() / flags)
# ---------------------------------------------------------------------------


def _is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:  # noqa: BLE001
        return False


def _prompt(text: str, *, default: str = "", secret: bool = False) -> str:
    """Prompt for one line of input (prompt_toolkit if available)."""
    try:
        from prompt_toolkit import prompt as ptk_prompt  # noqa: PLC0415

        value = ptk_prompt(text, is_password=secret)
    except Exception:  # noqa: BLE001 — no terminal / import failure
        sys.stdout.write(text)
        sys.stdout.flush()
        value = sys.stdin.readline().rstrip("\n").rstrip("\r")
    return value or default


def _confirm(text: str, *, default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    ans = _prompt(text + suffix).strip().lower()
    if not ans:
        return default
    return ans in ("y", "yes")


def _read_api_key_stdin() -> str:
    raw = sys.stdin.read()
    return raw.rstrip("\n").rstrip("\r")


# ---------------------------------------------------------------------------
# provider command handlers
# ---------------------------------------------------------------------------


def cmd_provider_list(args: argparse.Namespace) -> int:
    async def _go(c: Container) -> dict[str, Any]:
        rows = await c.model_catalog.list_provider_configs_use_case.execute()
        return {"providers": rows}

    _emit_doc(run_use_case(_go, **_runtime_kwargs(args)))
    return 0


def _resolve_provider_fields(
    args: argparse.Namespace,
    *,
    existing: dict[str, Any] | None,
    interactive: bool,
) -> tuple[str, dict[str, Any], str | None]:
    """Resolve (provider_id, config, api_key) from flags + optional prompts.

    Returns the provider id, the non-secret config dict to persist (with
    ``api_key`` injected only if supplied — the use case strips it to the
    SecretStore), and the raw api_key (or ``None`` to leave unchanged).
    """
    existing = existing or {}
    provider_id = getattr(args, "provider_id", None)
    ptype = args.provider_type
    base_url = args.base_url
    default_model = args.default_model
    api_key: str | None = None

    if args.api_key_stdin:
        api_key = _read_api_key_stdin()

    if interactive:
        if not provider_id:
            provider_id = _prompt("provider id › ").strip()
        if not ptype:
            sys.stdout.write(
                "provider 类型: "
                + ", ".join(
                    f"{i + 1}) {_PROVIDER_PRESETS[k]['label']}"
                    for i, k in enumerate(sorted(_PROVIDER_PRESETS))
                )
                + "\n"
            )
            choice = _prompt("选择 (回车默认 anthropic) › ").strip()
            keys = sorted(_PROVIDER_PRESETS)
            if choice.isdigit() and 1 <= int(choice) <= len(keys):
                ptype = keys[int(choice) - 1]
            elif choice in _PROVIDER_PRESETS:
                ptype = choice
            else:
                ptype = "anthropic"
        preset = _PROVIDER_PRESETS.get(ptype, {})
        if base_url is None:
            base_url = _prompt(
                f"Endpoint (回车用默认 {preset.get('base_url', '')}) › ",
                default=preset.get("base_url", ""),
            )
        if api_key is None:
            api_key = _prompt("API Key › ", secret=True) or None
        if default_model is None:
            default_model = _prompt(
                f"默认模型 (回车用 {preset.get('default_model', '')}) › ",
                default=preset.get("default_model", ""),
            )

    if not provider_id:
        # Fall back to the type as the id when scripting without an id.
        provider_id = ptype or existing.get("__id__") or ""

    preset = _PROVIDER_PRESETS.get(ptype or "", {})
    config: dict[str, Any] = dict(existing)
    config.pop("api_key", None)  # never echo the mask back into a write
    if base_url is not None:
        config["base_url"] = base_url
    elif "base_url" not in config and preset.get("base_url"):
        config["base_url"] = preset["base_url"]
    if default_model:
        # Track the default model in the provider's model list (chat reads
        # config["models"][].model_id to route).
        models = config.get("models")
        if not isinstance(models, list):
            models = []
        if not any(
            isinstance(m, dict) and m.get("model_id") == default_model
            for m in models
        ):
            models.append({"model_id": default_model, "name": default_model})
        config["models"] = models
        config["default_model"] = default_model
    if api_key:
        # Injected into config; UpdateProviderConfigUseCase extracts it to the
        # SecretStore and strips it before persisting (D0.5.0).
        config["api_key"] = api_key

    return provider_id, config, api_key


def _save_and_maybe_test(
    args: argparse.Namespace, provider_id: str, config: dict[str, Any]
) -> int:
    from qai.model_catalog.application.use_cases.probe_provider import (  # noqa: PLC0415
        ProbeProviderCommand,
    )
    from qai.model_catalog.application.use_cases.update_provider_config import (  # noqa: PLC0415
        UpdateProviderConfigCommand,
    )

    if not provider_id:
        sys.stderr.write("provider id is required\n")
        return 2

    do_test = not getattr(args, "no_test", False)

    async def _go(c: Container) -> dict[str, Any]:
        await c.model_catalog.update_provider_config_use_case.execute(
            UpdateProviderConfigCommand(
                provider_id=provider_id, config=config
            )
        )
        result: dict[str, Any] = {"saved": provider_id}
        if do_test:
            probe = await c.model_catalog.probe_provider_use_case.execute(
                ProbeProviderCommand(provider_id=provider_id)
            )
            result["test"] = {
                "ok": probe.ok,
                "status": probe.status,
                "models": list(probe.model_ids),
                "error": probe.error,
            }
        return result

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit_doc(result)
    test = result.get("test")
    if test is not None and not test.get("ok"):
        # Saved but unreachable: warn (non-fatal — key/endpoint may be fixed
        # later). Exit 0 since the config persisted.
        sys.stderr.write(
            f"⚠ provider {provider_id!r} 已保存但连通性测试失败: "
            f"{test.get('error')}\n"
        )
    return 0


def cmd_provider_add(args: argparse.Namespace) -> int:
    interactive = _is_tty() and not args.api_key_stdin
    provider_id, config, _ = _resolve_provider_fields(
        args, existing=None, interactive=interactive
    )
    return _save_and_maybe_test(args, provider_id, config)


def cmd_provider_edit(args: argparse.Namespace) -> int:
    provider_id = args.provider_id

    async def _load(c: Container) -> dict[str, Any] | None:
        return await c.model_catalog.provider_registry.get_provider_config(
            provider_id
        )

    existing = run_use_case(_load, **_runtime_kwargs(args))
    if existing is None:
        sys.stderr.write(f"provider {provider_id!r} not found\n")
        return 1
    interactive = _is_tty() and not args.api_key_stdin
    # provider_id is fixed for edit; ensure resolver keeps it.
    args.provider_id = provider_id
    _, config, _ = _resolve_provider_fields(
        args, existing=existing, interactive=interactive
    )
    return _save_and_maybe_test(args, provider_id, config)


def cmd_provider_remove(args: argparse.Namespace) -> int:
    if not args.yes:
        sys.stderr.write(
            "removal not confirmed; pass --yes to remove "
            f"provider {args.provider_id!r}\n"
        )
        return 2
    provider_id = args.provider_id

    async def _go(c: Container) -> dict[str, Any]:
        # The registry has no delete; persist an empty config and drop the
        # stored key. (model_catalog has no remove-provider use case; clearing
        # config + secret is the equivalent "forget this provider" action.)
        from qai.model_catalog.application.use_cases.update_provider_config import (  # noqa: PLC0415
            UpdateProviderConfigCommand,
        )

        await c.model_catalog.update_provider_config_use_case.execute(
            UpdateProviderConfigCommand(provider_id=provider_id, config={})
        )
        store = getattr(c, "secret_store", None)
        if store is not None:
            try:
                if store.exists("qai.model_catalog.provider", provider_id):
                    store.delete("qai.model_catalog.provider", provider_id)
            except Exception:  # noqa: BLE001 — best effort
                pass
        return {"removed": provider_id}

    _emit_doc(run_use_case(_go, **_runtime_kwargs(args)))
    return 0


def cmd_provider_test(args: argparse.Namespace) -> int:
    from qai.model_catalog.application.use_cases.probe_provider import (  # noqa: PLC0415
        ProbeProviderCommand,
    )

    provider_id = args.provider_id

    async def _go(c: Container) -> dict[str, Any]:
        probe = await c.model_catalog.probe_provider_use_case.execute(
            ProbeProviderCommand(provider_id=provider_id)
        )
        return {
            "provider_id": provider_id,
            "ok": probe.ok,
            "status": probe.status,
            "models": list(probe.model_ids),
            "error": probe.error,
        }

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit_doc(result)
    return 0 if result.get("ok") else 1


# ---------------------------------------------------------------------------
# setup wizard
# ---------------------------------------------------------------------------


def cmd_config_setup(args: argparse.Namespace) -> int:
    """Interactive one-stop wizard: providers + basic preferences.

    Runs against a single long-lived container so multiple provider adds +
    the prefs write share one DB session. Ctrl+C exits non-destructively
    (already-saved providers persist).
    """
    if not _is_tty():
        sys.stderr.write(
            "qai config setup 需要交互式终端；脚本场景请用 "
            "qai config provider add --type ... --api-key-stdin\n"
        )
        return 2

    async def _wizard() -> int:
        async with cli_container(**_runtime_kwargs(args)) as c:
            sys.stdout.write(
                "欢迎使用 QAIModelBuilder 配置向导。我将逐步帮你完成配置。\n\n"
            )
            # [1] cloud providers
            sys.stdout.write("[1/2] 云端模型 Provider\n")
            if _confirm(
                "  你要添加云端 AI 模型吗？(用于 Chat / Model Builder Agent)"
            ):
                while True:
                    await _setup_one_provider(c, args)
                    if not _confirm("  还要添加另一个 provider 吗？", default=False):
                        break
            # [2] preferences
            sys.stdout.write("\n[2/2] 默认偏好\n")
            lang = _prompt("  界面语言 [简体中文] › ", default="zh-CN")
            await c.user_prefs.save_document_use_case.execute(
                "ui", updates={"language": lang}
            )
            sys.stdout.write(
                "  ✓ 完成！配置已保存。运行 qai config provider 查看，"
                "qai build / qai app 开始使用。\n"
            )
            return 0

    return asyncio.run(_wizard())


async def _setup_one_provider(c: Container, args: argparse.Namespace) -> None:
    from qai.model_catalog.application.use_cases.probe_provider import (  # noqa: PLC0415
        ProbeProviderCommand,
    )
    from qai.model_catalog.application.use_cases.update_provider_config import (  # noqa: PLC0415
        UpdateProviderConfigCommand,
    )

    # Reuse the field resolver in interactive mode (build a transient ns).
    ns = argparse.Namespace(
        provider_id=None,
        provider_type=None,
        base_url=None,
        default_model=None,
        api_key_stdin=False,
        no_test=False,
    )
    provider_id, config, _ = _resolve_provider_fields(
        ns, existing=None, interactive=True
    )
    if not provider_id:
        sys.stdout.write("  (跳过：未提供 provider id)\n")
        return
    await c.model_catalog.update_provider_config_use_case.execute(
        UpdateProviderConfigCommand(provider_id=provider_id, config=config)
    )
    sys.stdout.write("  ✓ 正在测试连通性…\n")
    probe = await c.model_catalog.probe_provider_use_case.execute(
        ProbeProviderCommand(provider_id=provider_id)
    )
    if probe.ok:
        models = ", ".join(probe.model_ids[:5]) or "(无模型列表)"
        sys.stdout.write(
            f"  ✓ 已保存 provider {provider_id!r}（{probe.status} OK，"
            f"可用模型: {models}）\n"
        )
    else:
        sys.stdout.write(
            f"  ⚠ provider {provider_id!r} 已保存，但连通性测试失败: "
            f"{probe.error}\n"
        )
