// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

import { beforeEach, describe, expect, it } from "vitest";

import { normalizeServerUrl, useVlaSimulation } from "../useVlaSimulation";

const STORAGE_KEY = "qai_vla_simulation_settings";

beforeEach(() => {
  localStorage.removeItem(STORAGE_KEY);
  useVlaSimulation().saveServerUrl("");
});

describe("VLA Simulation server URL", () => {
  it("accepts HTTP(S) addresses and rejects other schemes or malformed values", () => {
    expect(normalizeServerUrl(" http://192.0.2.10:8080/sim/ ")).toBe(
      "http://192.0.2.10:8080/sim/",
    );
    expect(normalizeServerUrl("https://sim.example.test/ui")).toBe(
      "https://sim.example.test/ui",
    );
    expect(normalizeServerUrl("file:///tmp/simulator.html")).toBeNull();
    expect(normalizeServerUrl("not an address")).toBeNull();
  });

  it("persists the normalized address and clears it when saved empty", () => {
    const settings = useVlaSimulation();

    expect(settings.saveServerUrl(" http://192.0.2.10:8080/ ")).toBe(true);
    expect(settings.serverUrl.value).toBe("http://192.0.2.10:8080/");
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}")).toEqual({
      serverUrl: "http://192.0.2.10:8080/",
    });

    expect(settings.saveServerUrl("")).toBe(true);
    expect(settings.serverUrl.value).toBe("");
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}")).toEqual({
      serverUrl: "",
    });
  });

  it("does not replace a usable server address with an unsupported scheme", () => {
    const settings = useVlaSimulation();
    settings.saveServerUrl("https://sim.example.test/");

    expect(settings.saveServerUrl("javascript:alert(1)")).toBe(false);
    expect(settings.serverUrl.value).toBe("https://sim.example.test/");
  });
});
