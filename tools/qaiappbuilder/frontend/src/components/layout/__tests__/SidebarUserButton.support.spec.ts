// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/** Sidebar user menu: "Contact support" and "Logs > copy / export backend log". */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createI18n } from "vue-i18n";
import { createPinia, setActivePinia } from "pinia";

vi.mock("@/diagnostics/backendLog", () => ({
  COPY_LOG_LINES: 2000,
  EXPORT_LOG_LINES: 20000,
  fetchBackendLogText: vi.fn(),
  requestContactSupport: vi.fn(),
  copyText: vi.fn(),
  downloadText: vi.fn(),
}));

import en from "@/locales/en";
import SidebarUserButton from "@/components/layout/SidebarUserButton.vue";
import { useAuthStore } from "@/stores/auth";
import { useConfirmStore } from "@/stores/confirm";
import { useToastStore } from "@/stores/toast";
import { useConfirm } from "@/composables/useConfirm";
import {
  copyText,
  downloadText,
  fetchBackendLogText,
  requestContactSupport,
} from "@/diagnostics/backendLog";

const EMAIL = "qaiappbuilder.support@qti.qualcomm.com";
const mounted: VueWrapper[] = [];

function i18n() {
  return createI18n<false>({
    legacy: false,
    locale: "en",
    fallbackLocale: "en",
    missingWarn: false,
    fallbackWarn: false,
    messages: { en },
  });
}

async function openMenu(): Promise<VueWrapper> {
  const wrapper = mount(SidebarUserButton, { global: { plugins: [i18n()] }, attachTo: document.body });
  mounted.push(wrapper);
  await wrapper.find('[data-testid="sidebar-user-btn"]').trigger("click");
  return wrapper;
}

function click(testId: string): void {
  document.querySelector<HTMLElement>(`[data-testid="${testId}"]`)!.click();
}

function toastKinds(): string[] {
  return useToastStore().items.map((t) => `${t.kind}:${t.message}`);
}

beforeEach(() => {
  setActivePinia(createPinia());
  const auth = useAuthStore();
  auth.authEnabled = true;
  auth.authenticated = true;
  auth.user = { username: "u", display_name: "U", email: "u@x.com" } as never;
  vi.mocked(fetchBackendLogText).mockResolvedValue("backend log lines");
  vi.mocked(copyText).mockResolvedValue(true);
});

afterEach(() => {
  while (mounted.length > 0) mounted.pop()?.unmount();
  document.body.innerHTML = "";
  vi.clearAllMocks();
});

describe("contact support", () => {
  it("is shown in the external edition too", async () => {
    await openMenu();
    expect(document.querySelector('[data-testid="sidebar-user-contact-support"]')).not.toBeNull();
  });

  it("does nothing more when the backend opened the mail client", async () => {
    vi.mocked(requestContactSupport).mockResolvedValue({ opened: true, email: EMAIL });
    const wrapper = await openMenu();
    click("sidebar-user-contact-support");
    await flushPromises();
    await wrapper.vm.$nextTick();
    expect(useConfirmStore().visible).toBe(false);
  });

  it("shows the address and copies it when no mail client is available", async () => {
    vi.mocked(requestContactSupport).mockResolvedValue({ opened: false, email: EMAIL });
    await openMenu();
    click("sidebar-user-contact-support");
    await flushPromises();
    const confirmStore = useConfirmStore();
    expect(confirmStore.visible).toBe(true);
    expect(confirmStore.message).toContain(EMAIL);
    useConfirm().accept();
    await flushPromises();
    expect(copyText).toHaveBeenCalledWith(EMAIL);
    expect(toastKinds()).toContain("success:Support address copied");
  });

  it("still shows the address when the request fails", async () => {
    vi.mocked(requestContactSupport).mockRejectedValue(new Error("offline"));
    await openMenu();
    click("sidebar-user-contact-support");
    await flushPromises();
    expect(useConfirmStore().message).toContain(EMAIL);
  });
});

describe("copy / export log", () => {
  async function openLogs(): Promise<void> {
    await openMenu(); // copy / export are top-level items, no "Logs" sub-menu
  }

  it("copies the last 2000 lines of the backend log", async () => {
    await openLogs();
    click("sidebar-user-copy-backend-log");
    await flushPromises();
    expect(fetchBackendLogText).toHaveBeenCalledWith(2000);
    expect(copyText).toHaveBeenCalledWith("backend log lines");
    expect(toastKinds()).toContain("success:Backend log copied to clipboard");
  });

  it("reports a failed copy", async () => {
    vi.mocked(copyText).mockResolvedValue(false);
    await openLogs();
    click("sidebar-user-copy-backend-log");
    await flushPromises();
    expect(toastKinds()).toContain("error:Could not copy the backend log");
  });

  it("exports up to 20000 lines as a file", async () => {
    await openLogs();
    click("sidebar-user-export-backend-log");
    await flushPromises();
    expect(fetchBackendLogText).toHaveBeenCalledWith(20000);
    expect(downloadText).toHaveBeenCalledWith("backend log lines");
    expect(toastKinds()).toContain("success:Backend log exported");
  });

  it("reports a failed export", async () => {
    vi.mocked(fetchBackendLogText).mockRejectedValue(new Error("401"));
    await openLogs();
    click("sidebar-user-export-backend-log");
    await flushPromises();
    expect(toastKinds()).toContain("error:Could not export the backend log");
  });
});
