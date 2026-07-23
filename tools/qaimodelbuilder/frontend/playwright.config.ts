import { defineConfig, devices } from "@playwright/test";

// E2E consistency tests for the WebUI (Vue3 + Vite) frontend, verifying it
// renders/behaves consistently with the CLI/HTTP API layers already covered
// by test/test_builder_cli.py (module C). See
// .junie/plans/qai-webui-e2e-consistency-test.md for the full design.
//
// The dev server is expected to already point at an isolated Builder test
// instance via QAI_DEV_BACKEND_HTTP (see vite.config.ts); this config does
// not manage the Builder process itself.
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: false,
  retries: 0,
  reporter: [["list"], ["json", { outputFile: "e2e-report/results.json" }]],
  use: {
    baseURL: process.env.QAI_E2E_BASE_URL ?? "http://127.0.0.1:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
  webServer: process.env.QAI_E2E_SKIP_WEBSERVER
    ? undefined
    : {
        command: "pnpm dev",
        url: "http://127.0.0.1:5173",
        reuseExistingServer: true,
        timeout: 60_000,
      },
});
