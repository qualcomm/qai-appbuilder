# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - App Builder runs and share links survive a restart (AB-14 / AB-12, part 2).

Own module with its own backends: ``test_app_builder_runs.py`` keeps a module-scoped backend on port 8989 for its whole run, and a
restart cannot share it. See that module for scope and what is not covered.
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e._owned_backend import owned_backend
from tests.e2e.test_app_builder_runs import AB, _wait

pytestmark = pytest.mark.e2e

def test_runs_survive_a_restart() -> None:
    with owned_backend() as ob:
        cl = ob.start()
        cl.timeout = httpx.Timeout(60.0)
        rid = cl.post(f"{AB}/runs", json={"model_id": "zipformer-zh", "inputs": {}}).json()["id"]
        before = _wait(cl, rid)
        ob.stop()
        cl2 = ob.start()
        assert cl2.get(f"{AB}/runs/{rid}").json() == before


def test_share_tokens_survive_a_restart() -> None:
    with owned_backend() as ob:
        cl = ob.start()
        cl.timeout = httpx.Timeout(60.0)
        rid = cl.post(f"{AB}/runs", json={"model_id": "zipformer-zh", "inputs": {}}).json()["id"]
        run = _wait(cl, rid)
        token = cl.post(f"{AB}/share", json={"run_id": rid}).json()["token"]
        ob.stop()
        cl2 = ob.start()
        v = cl2.get(f"{AB}/share/{token}")
        assert v.status_code == 200 and v.json()["run"] == run
