# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``qai auth`` subcommands — headless SSO via OAuth 2.0 Device Flow.

Commands
--------
``qai auth device-login``   Start an RFC 8628 Device Authorization flow.
                            Prints the ``user_code`` + verification URL to
                            stdout (SSH terminal), polls Okta until the user
                            completes authorization on another device, verifies
                            the ``id_token``, and stores the ``refresh_token``
                            in the SecretStore (keyring → Fernet auto-fallback).

``qai auth status``         Show whether a stored Okta refresh_token exists.

``qai auth logout``         Clear the stored refresh_token from SecretStore.

This module follows the exact pattern of ``apps/cli/commands/config.py``:
  * ``register(subparsers)`` wires argparse
  * each ``cmd_*`` function takes ``argparse.Namespace`` and returns ``int``
  * async work is driven through ``run_use_case()`` (sync bridge)
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from apps.cli._runtime import run_use_case

if TYPE_CHECKING:
    from apps.api.di import Container

__all__ = [
    "register",
    "cmd_device_login",
    "cmd_auth_status",
    "cmd_auth_logout",
]

# SecretStore namespace for Okta device-flow credentials.
_SERVICE = "qai.auth.okta"
_KEY_REFRESH = "refresh_token"


# ---------------------------------------------------------------------------
# argparse registration
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Attach ``qai auth`` subparsers to the top-level dispatcher."""
    auth = subparsers.add_parser(
        "auth",
        help=(
            "Headless SSO via OAuth 2.0 Device Flow (RFC 8628). "
            "Use on servers / IoT boards that have no local browser."
        ),
    )
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)

    # qai auth device-login
    dl = auth_sub.add_parser(
        "device-login",
        help=(
            "Start a Device Authorization flow. Prints user_code + URL, "
            "polls until authorized, then persists the refresh_token."
        ),
    )
    dl.add_argument(
        "--qrcode",
        action="store_true",
        default=False,
        help=(
            "Print an ASCII QR code for the verification URL "
            "(requires: pip install qrcode). "
            "Useful on IoT boards with a small display."
        ),
    )
    dl.set_defaults(handler=cmd_device_login)

    # qai auth status
    st = auth_sub.add_parser(
        "status",
        help="Show whether a stored Okta refresh_token exists.",
    )
    st.set_defaults(handler=cmd_auth_status)

    # qai auth logout
    lo = auth_sub.add_parser(
        "logout",
        help="Clear the stored Okta refresh_token from SecretStore.",
    )
    lo.set_defaults(handler=cmd_auth_logout)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def cmd_device_login(args: argparse.Namespace) -> int:
    """Full device-flow: authorize → display → poll → verify → persist."""

    async def _run(c: "Container") -> None:
        from interfaces.http.auth.device_flow import device_authorize, poll_for_token
        from interfaces.http.auth.jwt import verify_id_token
        from interfaces.http.middleware.auth import public_user
        from qai.platform.errors import NotFoundError

        cfg = c.settings.auth

        # ── (1)(2) Request device_code + user_code from Okta ─────────────────
        dev = await device_authorize(
            client_id=cfg.client_id,
            issuer=cfg.issuer,
            scopes=cfg.scopes,
            ssl_verify=cfg.ssl_verify,
        )

        # ── (3) Display to the operator (SSH terminal is the simple case) ─────
        print()
        print("  ╔══════════════════════════════════════════════════════════╗")
        print("  ║          Qualcomm Okta — Device Authorization            ║")
        print("  ╠══════════════════════════════════════════════════════════╣")
        print(f"  ║  Code : {dev['user_code']:<51}║")
        print(f"  ║  URL  : {dev['verification_uri']:<51}║")
        print("  ╠══════════════════════════════════════════════════════════╣")
        print("  ║  Or scan / copy the full link (code pre-filled):         ║")
        print(f"  ║  {dev['verification_uri_complete']:<58}║")
        print("  ╚══════════════════════════════════════════════════════════╝")
        print()

        if args.qrcode:
            _print_qrcode(dev["verification_uri_complete"])

        # ── (4)(5) Poll until granted / denied / expired ──────────────────────
        print("  Waiting for authorization on another device …", flush=True)
        token_resp = await poll_for_token(
            client_id=cfg.client_id,
            issuer=cfg.issuer,
            device_code=dev["device_code"],
            interval=int(dev.get("interval", 5)),
            ssl_verify=cfg.ssl_verify,
            max_seconds=cfg.device_poll_max_seconds,
        )

        # ── (6) Verify id_token — reuse interfaces/http/auth/jwt.py directly ──
        claims = verify_id_token(
            token_resp["id_token"],
            client_id=cfg.client_id,
            issuer=cfg.issuer,
            ssl_verify=cfg.ssl_verify,
        )
        user = public_user(claims)

        # Email domain allow-list (same check as AuthMiddleware)
        if cfg.allowed_email_domains:
            email = user.get("email") or ""
            domain = email.split("@")[-1].lower()
            allowed = {d.lower() for d in cfg.allowed_email_domains}
            if domain not in allowed:
                raise RuntimeError(
                    f"Email domain {domain!r} is not in the allowed-domain list"
                )

        # ── (7) Persist refresh_token → SecretStore (keyring → Fernet) ───────
        refresh = token_resp.get("refresh_token", "")
        display = user.get("display_name") or user.get("username") or email
        if refresh:
            c.secret_store.set(_SERVICE, _KEY_REFRESH, refresh)
            print(f"  Login successful — {display}")
            print("  refresh_token saved; future sessions will renew silently.")
        else:
            print(f"  Login successful — {display}")
            print(
                "  Warning: Okta did not return a refresh_token. "
                "You may need to re-login when the access_token expires. "
                "(Check that offline_access scope is enabled on the Okta client.)"
            )

    run_use_case(
        _run,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )
    return 0


def cmd_auth_status(args: argparse.Namespace) -> int:
    """Print a brief summary of the stored Okta credentials."""

    async def _run(c: "Container") -> int:
        from qai.platform.errors import NotFoundError

        try:
            rt = c.secret_store.get(_SERVICE, _KEY_REFRESH)
        except NotFoundError:
            print(
                "  No stored Okta credentials found. "
                "Run `qai auth device-login` to authenticate."
            )
            return 1
        print(
            f"  Okta refresh_token is present "
            f"({len(rt)} chars). Device-flow login has been completed."
        )
        return 0

    return run_use_case(
        _run,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )


def cmd_auth_logout(args: argparse.Namespace) -> int:
    """Remove the stored Okta refresh_token from SecretStore."""

    async def _run(c: "Container") -> int:
        from qai.platform.errors import NotFoundError

        try:
            c.secret_store.delete(_SERVICE, _KEY_REFRESH)
            print("  Okta refresh_token cleared.")
        except NotFoundError:
            print("  No stored credentials to remove.")
        return 0

    return run_use_case(
        _run,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )


# ---------------------------------------------------------------------------
# Optional QR-code helper
# ---------------------------------------------------------------------------


def _print_qrcode(url: str) -> None:
    """Print an ASCII QR code to stdout if the ``qrcode`` package is installed."""
    try:
        import qrcode  # type: ignore[import]

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except ImportError:
        print(
            "  (Install `pip install qrcode` to display an ASCII QR code here.)"
        )
