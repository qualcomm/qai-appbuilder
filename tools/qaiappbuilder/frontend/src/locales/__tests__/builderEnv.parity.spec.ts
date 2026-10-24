// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

import { describe, expect, it } from "vitest";

import en from "@/locales/en/builderEnv";
import zhCn from "@/locales/zh-CN/builderEnv";
import zhTw from "@/locales/zh-TW/builderEnv";

/** Every dotted leaf key of a nested object. */
function keys(o: unknown, prefix = ""): string[] {
  if (o === null || typeof o !== "object") return [prefix];
  return Object.entries(o as Record<string, unknown>).flatMap(([k, v]) =>
    keys(v, prefix ? `${prefix}.${k}` : k),
  );
}

function leaves(o: unknown, prefix = ""): Array<[string, string]> {
  if (typeof o === "string") return [[prefix, o]];
  return Object.entries(o as Record<string, unknown>).flatMap(([k, v]) =>
    leaves(v, prefix ? `${prefix}.${k}` : k),
  );
}

describe("builderEnv locale parity", () => {
  it("zh-CN and zh-TW have exactly the same keys as en", () => {
    expect(keys(zhCn).sort()).toEqual(keys(en).sort());
    expect(keys(zhTw).sort()).toEqual(keys(en).sort());
  });

  it("no empty strings", () => {
    for (const [name, loc] of [["en", en], ["zh-CN", zhCn], ["zh-TW", zhTw]] as const) {
      for (const [k, v] of leaves(loc)) expect(v.trim(), `${name}:${k}`).not.toBe("");
    }
  });

  it("placeholders match across languages", () => {
    const ph = (s: string) => (s.match(/\{\w+\}/g) ?? []).sort();
    const base = Object.fromEntries(leaves(en));
    for (const [name, loc] of [["zh-CN", zhCn], ["zh-TW", zhTw]] as const) {
      for (const [k, v] of leaves(loc)) expect(ph(v), `${name}:${k}`).toEqual(ph(base[k] ?? ""));
    }
  });

  it("covers every installer step id and every error code the backend can send", () => {
    const steps = ["python310", "venv", "deps", "sdk_download", "sdk_extract", "config", "verify"];
    for (const s of steps) expect(Object.keys(en.step)).toContain(s);
    const codes = ["network", "disk_space", "uv_missing", "python_install", "venv", "deps", "extract",
      "permission", "verify", "already_running", "cancelled", "spawn_failed", "unsupported_os", "internal"];
    for (const c of codes) expect(Object.keys(en.error)).toContain(c);
    for (const p of ["config", "sdk", "venv"]) expect(Object.keys(en.part)).toContain(p);
  });

  it("the Chinese files contain no mojibake / replacement characters", () => {
    for (const loc of [zhCn, zhTw]) {
      for (const [k, v] of leaves(loc)) {
        expect(v, k).not.toMatch(/\uFFFD|\?{3,}|Ã|â€/);
      }
    }
  });
});
