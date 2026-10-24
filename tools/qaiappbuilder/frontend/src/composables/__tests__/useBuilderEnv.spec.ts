// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BuilderEnvJob, BuilderEnvStatus } from "@/api/builderEnv";
import { ApiError } from "@/api/errors";

vi.mock("@/api/builderEnv", () => ({
  fetchBuilderEnv: vi.fn(),
  startBuilderEnvInstall: vi.fn(),
  cancelBuilderEnvInstall: vi.fn(),
}));

import * as api from "@/api/builderEnv";
import {
  POLL_IDLE_MS,
  POLL_RUNNING_MS,
  __resetBuilderEnvForTests,
  useBuilderEnv,
} from "../useBuilderEnv";

const fetchMock = vi.mocked(api.fetchBuilderEnv);
const startMock = vi.mocked(api.startBuilderEnvInstall);
const cancelMock = vi.mocked(api.cancelBuilderEnvInstall);

function job(over: Partial<BuilderEnvJob> = {}): BuilderEnvJob {
  return {
    job_id: "j1",
    state: "running",
    overall: 0,
    step: "deps",
    step_state: "running",
    message: "",
    unit: "",
    done: 0,
    total: null,
    speed: 0,
    eta: null,
    log_tail: [],
    error_code: "",
    error_message: "",
    failed_step: "",
    started_at: 1,
    finished_at: null,
    ...over,
  };
}

function status(over: Partial<BuilderEnvStatus> = {}): BuilderEnvStatus {
  return {
    ready: false,
    missing: ["config"],
    problems: [{ code: "config_missing", path: "x", key: "" }],
    sdk_root: "",
    venv: "",
    config_file: "x",
    job: null,
    installable: true,
    ...over,
  };
}

function apiError(statusCode: number, message = "boom"): ApiError {
  return new ApiError(
    { type: "x", code: "x", message },
    statusCode,
  );
}

beforeEach(() => {
  vi.useFakeTimers();
  __resetBuilderEnvForTests();
  fetchMock.mockReset();
  startMock.mockReset();
  cancelMock.mockReset();
});

afterEach(() => {
  __resetBuilderEnvForTests();
  vi.useRealTimers();
});

describe("phase", () => {
  it.each([
    [status(), "missing"],
    [status({ installable: false }), "unavailable"],
    [status({ ready: true, missing: [], problems: [] }), "ready"],
    [status({ job: job({ state: "running" }) }), "installing"],
    [status({ job: job({ state: "failed" }) }), "failed"],
    [status({ job: job({ state: "cancelled" }) }), "cancelled"],
  ] as const)("maps %#", async (s, expected) => {
    fetchMock.mockResolvedValue(s);
    const env = useBuilderEnv();
    await env.refresh();
    expect(env.phase.value).toBe(expected);
  });

  it("running wins over ready, ready wins over an old failed job", async () => {
    const env = useBuilderEnv();
    fetchMock.mockResolvedValue(status({ ready: true, job: job({ state: "running" }) }));
    await env.refresh();
    expect(env.phase.value).toBe("installing");
    fetchMock.mockResolvedValue(status({ ready: true, job: job({ state: "failed" }) }));
    await env.refresh();
    expect(env.phase.value).toBe("ready");
  });

  it("is unknown before the first answer", () => {
    expect(useBuilderEnv().phase.value).toBe("unknown");
  });

  it("is unknown (feature hidden) when the server answers 404", async () => {
    fetchMock.mockRejectedValue(apiError(404));
    const env = useBuilderEnv();
    await env.refresh();
    expect(env.phase.value).toBe("unknown");
  });

  it("keeps the last state across a transient failure", async () => {
    const env = useBuilderEnv();
    fetchMock.mockResolvedValueOnce(status());
    await env.refresh();
    fetchMock.mockRejectedValueOnce(new Error("network down"));
    await env.refresh();
    expect(env.phase.value).toBe("missing");
  });
});

describe("percent", () => {
  it("is rounded and clamped", async () => {
    const env = useBuilderEnv();
    for (const [raw, want] of [[41.6, 42], [-5, 0], [250, 100]] as const) {
      fetchMock.mockResolvedValue(status({ job: job({ overall: raw }) }));
      await env.refresh();
      expect(env.percent.value).toBe(want);
    }
  });

  it("is 0 without a job", async () => {
    fetchMock.mockResolvedValue(status());
    const env = useBuilderEnv();
    await env.refresh();
    expect(env.percent.value).toBe(0);
  });
});

describe("polling", () => {
  it("polls fast while installing and slowly otherwise", async () => {
    const env = useBuilderEnv();
    fetchMock.mockResolvedValue(status({ job: job() }));
    env.acquire();
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(POLL_RUNNING_MS);
    expect(fetchMock).toHaveBeenCalledTimes(2);

    fetchMock.mockResolvedValue(status());
    await vi.advanceTimersByTimeAsync(POLL_RUNNING_MS); // sees "not running" now
    const after = fetchMock.mock.calls.length;
    await vi.advanceTimersByTimeAsync(POLL_RUNNING_MS * 3);
    expect(fetchMock.mock.calls.length).toBe(after); // no longer fast
    await vi.advanceTimersByTimeAsync(POLL_IDLE_MS);
    expect(fetchMock.mock.calls.length).toBe(after + 1);
    env.release();
  });

  it("stops polling when the last consumer is released", async () => {
    const env = useBuilderEnv();
    fetchMock.mockResolvedValue(status({ job: job() }));
    env.acquire();
    env.acquire();
    await vi.advanceTimersByTimeAsync(0);
    env.release();
    await vi.advanceTimersByTimeAsync(POLL_RUNNING_MS);
    const mid = fetchMock.mock.calls.length;
    expect(mid).toBeGreaterThanOrEqual(2); // one consumer left: still polling
    env.release();
    await vi.advanceTimersByTimeAsync(POLL_RUNNING_MS * 5);
    expect(fetchMock.mock.calls.length).toBe(mid);
  });

  it("does not poll at all after a 404 (feature absent)", async () => {
    fetchMock.mockRejectedValue(apiError(404));
    const env = useBuilderEnv();
    env.acquire();
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(POLL_IDLE_MS * 3);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    env.release();
  });

  it("shares one request between simultaneous refreshes", async () => {
    fetchMock.mockResolvedValue(status());
    const env = useBuilderEnv();
    await Promise.all([env.refresh(), env.refresh(), env.refresh()]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("install / cancel", () => {
  it("install posts the options, refreshes and reports success", async () => {
    startMock.mockResolvedValue({ job_id: "j1", status: "queued" });
    fetchMock.mockResolvedValue(status({ job: job() }));
    const env = useBuilderEnv();
    const ok = await env.install({ mirror: "cn", proxy: "http://p:1" });
    expect(ok).toBe(true);
    expect(startMock).toHaveBeenCalledWith({ mirror: "cn", proxy: "http://p:1" });
    expect(env.phase.value).toBe("installing");
    expect(env.busy.value).toBe(false);
    expect(env.actionError.value).toBe("");
  });

  it("a 409 (already running elsewhere) is treated as success and just shows the job", async () => {
    startMock.mockRejectedValue(apiError(409));
    fetchMock.mockResolvedValue(status({ job: job() }));
    const env = useBuilderEnv();
    expect(await env.install()).toBe(true);
    expect(env.phase.value).toBe("installing");
    expect(env.actionError.value).toBe("");
  });

  it("other start errors are surfaced and do not wedge the busy flag", async () => {
    startMock.mockRejectedValue(apiError(400, "proxy must start with http://"));
    const env = useBuilderEnv();
    expect(await env.install({ proxy: "ftp://x" })).toBe(false);
    expect(env.actionError.value).toContain("proxy must start");
    expect(env.busy.value).toBe(false);
  });

  it("ignores a second click while the first request is in flight", async () => {
    let release!: () => void;
    startMock.mockImplementation(() => new Promise((r) => { release = () => r({ job_id: "j", status: "queued" }); }));
    fetchMock.mockResolvedValue(status({ job: job() }));
    const env = useBuilderEnv();
    const first = env.install();
    expect(await env.install()).toBe(false);
    release();
    await first;
    expect(startMock).toHaveBeenCalledTimes(1);
  });

  it("cancel calls the API and refreshes", async () => {
    cancelMock.mockResolvedValue({ cancelled: true });
    fetchMock.mockResolvedValue(status({ job: job({ state: "cancelled" }) }));
    const env = useBuilderEnv();
    await env.cancel();
    expect(cancelMock).toHaveBeenCalledTimes(1);
    expect(env.phase.value).toBe("cancelled");
  });

  it("a failed cancel is reported", async () => {
    cancelMock.mockRejectedValue(new Error("nope"));
    const env = useBuilderEnv();
    await env.cancel();
    expect(env.actionError.value).toBe("nope");
    expect(env.busy.value).toBe(false);
  });

  it("a new attempt clears the previous action error", async () => {
    const env = useBuilderEnv();
    startMock.mockRejectedValueOnce(new Error("first"));
    await env.install();
    expect(env.actionError.value).toBe("first");
    startMock.mockResolvedValueOnce({ job_id: "j", status: "queued" });
    fetchMock.mockResolvedValue(status({ job: job() }));
    await env.install();
    expect(env.actionError.value).toBe("");
  });
});
