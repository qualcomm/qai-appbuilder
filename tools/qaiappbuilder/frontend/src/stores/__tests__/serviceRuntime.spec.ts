// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

const fetchServiceStatus = vi.fn();
vi.mock("@/api/serviceControl", () => ({
  fetchServiceStatus: (...args: unknown[]) => fetchServiceStatus(...args),
}));

import { deriveLifecycle, useServiceRuntimeStore } from "@/stores/serviceRuntime";

describe("deriveLifecycle", () => {
  it("backend stopping is stopping even though running is false", () => {
    expect(deriveLifecycle({ state: "stopping", running: false }, null)).toBe("stopping");
  });

  it("running with ready=false is loading", () => {
    expect(deriveLifecycle({ state: "running", running: true, ready: false }, null)).toBe(
      "loading",
    );
  });

  it("running with ready=true/null is running", () => {
    expect(deriveLifecycle({ state: "running", running: true, ready: true }, null)).toBe(
      "running",
    );
    expect(deriveLifecycle({ state: "running", running: true, ready: null }, null)).toBe(
      "running",
    );
  });

  it("pending overlay wins over a stale backend snapshot", () => {
    expect(deriveLifecycle({ state: "stopped", running: false }, "starting")).toBe("starting");
    expect(deriveLifecycle({ state: "running", running: true }, "stopping")).toBe("stopping");
  });

  it("falls back to running flag when state is absent", () => {
    expect(deriveLifecycle({ running: true }, null)).toBe("running");
    expect(deriveLifecycle({ running: false }, null)).toBe("stopped");
  });
});

describe("useServiceRuntimeStore.waitForStopped", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    fetchServiceStatus.mockReset();
  });

  it("resolves true only once the backend reports the process gone", async () => {
    fetchServiceStatus
      .mockResolvedValueOnce({ state: "stopping", running: false })
      .mockResolvedValueOnce({ state: "stopped", running: false });
    const store = useServiceRuntimeStore();
    await expect(store.waitForStopped(5000)).resolves.toBe(true);
    expect(fetchServiceStatus).toHaveBeenCalledTimes(2);
  });

  it("resolves false when the process never exits", async () => {
    fetchServiceStatus.mockResolvedValue({ state: "running", running: true });
    const store = useServiceRuntimeStore();
    await expect(store.waitForStopped(600)).resolves.toBe(false);
  });
});
