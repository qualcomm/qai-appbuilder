// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

import { describe, expect, it } from "vitest";

import { deriveToolPayload } from "../useChatTransport";
import type { ChatTab } from "@/stores/chatTabs";

describe("deriveToolPayload", () => {
  it("keeps the UI-only VLA Simulation mode out of chat requests", () => {
    const tab = {
      activeMode: "vla-simulation",
      toolParams: { target_lang: "fr" },
    } as unknown as ChatTab;

    expect(deriveToolPayload(tab)).toEqual({
      toolMode: null,
      toolParams: null,
    });
  });
});
