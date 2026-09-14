// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Unit tests for {@link usePermissionAlert} — the background-tab title
 * badge for pending authorization requests.
 *
 * `document.hidden` / `document.visibilityState` are read-only in the DOM,
 * so each test shadows them on the instance via `Object.defineProperty`.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { nextTick, ref } from "vue";

import {
  usePermissionAlert,
  type PermissionAlertHandle,
} from "./usePermissionAlert";

const BASE_TITLE = "QAI AppBuilder";

function setHidden(hidden: boolean): void {
  Object.defineProperty(document, "hidden", {
    configurable: true,
    get: () => hidden,
  });
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => (hidden ? "hidden" : "visible"),
  });
}

describe("usePermissionAlert", () => {
  let handle: PermissionAlertHandle | null = null;

  beforeEach(() => {
    document.title = BASE_TITLE;
    setHidden(false);
  });

  afterEach(() => {
    handle?.stop();
    handle = null;
  });

  it("prefixes the title when a request arrives while hidden", async () => {
    const count = ref(0);
    handle = usePermissionAlert(count);
    setHidden(true);

    count.value = 1;
    await nextTick();

    expect(document.title).toBe(`🔔 (1) ${BASE_TITLE}`);
  });

  it("does not badge while the tab is visible", async () => {
    const count = ref(0);
    handle = usePermissionAlert(count);

    count.value = 1;
    await nextTick();

    expect(document.title).toBe(BASE_TITLE);
  });

  it("updates the count without stacking the prefix", async () => {
    const count = ref(0);
    handle = usePermissionAlert(count);
    setHidden(true);

    count.value = 1;
    await nextTick();
    count.value = 2;
    await nextTick();

    expect(document.title).toBe(`🔔 (2) ${BASE_TITLE}`);
  });

  it("clears the badge when the queue drains", async () => {
    const count = ref(0);
    handle = usePermissionAlert(count);
    setHidden(true);

    count.value = 1;
    await nextTick();
    count.value = 0;
    await nextTick();

    expect(document.title).toBe(BASE_TITLE);
  });

  it("clears the badge when the tab becomes visible again", async () => {
    const count = ref(0);
    handle = usePermissionAlert(count);
    setHidden(true);

    count.value = 1;
    await nextTick();
    expect(document.title).toBe(`🔔 (1) ${BASE_TITLE}`);

    setHidden(false);
    document.dispatchEvent(new Event("visibilitychange"));

    expect(document.title).toBe(BASE_TITLE);
  });

  it("badges an already-pending queue when the tab is hidden", () => {
    const count = ref(1);
    handle = usePermissionAlert(count);

    setHidden(true);
    document.dispatchEvent(new Event("visibilitychange"));

    expect(document.title).toBe(`🔔 (1) ${BASE_TITLE}`);
  });

  it("strips the badge on stop", async () => {
    const count = ref(0);
    handle = usePermissionAlert(count);
    setHidden(true);

    count.value = 1;
    await nextTick();

    handle.stop();
    handle = null;

    expect(document.title).toBe(BASE_TITLE);
  });
});
