// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { SHOW_SAMPLING_PARAMS } from "../samplingParams";
import { DEFAULT_MODEL_PARAMS } from "@/stores/_chatTabsTypes";

const read = (rel: string): string =>
  readFileSync(resolve(__dirname, "../..", rel), "utf8");

describe("sampling parameter UI is hidden", () => {
  it("is off, and the retained tab state still means 'send nothing'", () => {
    expect(SHOW_SAMPLING_PARAMS).toBe(false);
    expect(DEFAULT_MODEL_PARAMS.useDefaults).toBe(true);
  });

  it("every entry point and the wire builder are gated by the constant", () => {
    const composer = read("components/chat/ChatComposer.vue");
    expect(composer).toMatch(/v-if="SHOW_SAMPLING_PARAMS"[\s\S]{0,80}model-params-wrap/);
    const panel = read("components/chat/CloudModelsPanel.vue");
    expect(panel).toMatch(/<template v-if="SHOW_SAMPLING_PARAMS">/);
    const transport = read("composables/chat/useChatTransport.ts");
    expect(transport.match(/SHOW_SAMPLING_PARAMS &&/g)?.length).toBe(2);
  });
});
