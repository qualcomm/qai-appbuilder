# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``apps.cli.commands`` — argparse handler modules for the ``qai`` CLI.

Each submodule exposes one or more handler functions of shape
``def cmd_xxx(args: argparse.Namespace) -> int`` plus a ``register(parser)``
hook that the top-level ``apps.cli.__main__`` dispatcher calls to attach the
subparsers. Keeping the registration in the same module as the handlers
gives each command group a single self-contained file (Desktop App Plan
§2.1.1 / §2.5).
"""

from __future__ import annotations

__all__: list[str] = []
