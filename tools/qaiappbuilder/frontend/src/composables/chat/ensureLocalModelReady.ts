// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

import { loadServiceModel } from "@/api/serviceControl";
import { useServiceRuntimeStore } from "@/stores/serviceRuntime";

export interface EnsureLocalModelResult {
  ok: boolean;
  message?: string;
}

function isReady(name: string): boolean {
  const runtime = useServiceRuntimeStore();
  if (runtime.lifecycle !== "running") return false;
  const loaded = (runtime.status.model ?? "").toLowerCase();
  return loaded !== "" && loaded === name.toLowerCase();
}

/**
 * Called when a turn is sent with a local model: make sure GenieAPIService is
 * running with that model loaded, starting / loading it if not. Selecting a
 * model never starts the service; this is the only place that does.
 */
export async function ensureLocalModelReady(
  name: string,
  hooks?: { onStarting?: () => void },
): Promise<EnsureLocalModelResult> {
  const runtime = useServiceRuntimeStore();
  await runtime.refresh();
  if (isReady(name)) return { ok: true };

  // Past this point the user has to wait: tell the caller so it can show a popup.
  hooks?.onStarting?.();

  // A start / load / stop already in flight (any surface): let it finish first.
  if (runtime.isTransitional) {
    await runtime.settle();
    if (isReady(name)) return { ok: true };
  }

  runtime.begin("starting", name);
  try {
    const res = (await loadServiceModel({ model_name: name })) as {
      ok?: boolean;
      message?: string;
    };
    if (res.ok === false) {
      return { ok: false, message: res.message };
    }
  } catch (e) {
    return { ok: false, message: e instanceof Error ? e.message : String(e) };
  } finally {
    await runtime.end();
  }
  await runtime.settle();
  if (isReady(name)) return { ok: true };
  return { ok: false, message: runtime.status.model ? undefined : "model not loaded" };
}
