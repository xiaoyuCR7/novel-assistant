import { defineConfig, devices } from "@playwright/test";

const e2eDataDir = process.env.NOVEL_E2E_DATA_DIR;
if (!e2eDataDir) {
  throw new Error("NOVEL_E2E_DATA_DIR is required; run Playwright through npm test.");
}
const frontendPort = Number(process.env.NOVEL_E2E_FRONTEND_PORT ?? 4173);
const e2eResetToken = "novel-harness-playwright-reset-v1";

export default defineConfig({
  testDir: "./tests",
  timeout: 45_000,
  expect: { timeout: 8_000 },
  fullyParallel: false,
  workers: 1,
  reporter: "line",
  use: {
    baseURL: `http://127.0.0.1:${frontendPort}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    channel: "chrome",
    ...devices["Desktop Chrome"],
  },
  webServer: [
    {
      command: "..\\backend\\.venv\\Scripts\\python.exe -m uvicorn novel_harness.e2e_app:create_e2e_app --factory --app-dir ..\\backend\\src --host 127.0.0.1 --port 8000",
      port: 8000,
      reuseExistingServer: false,
      env: {
        NOVEL_DATA_DIR: e2eDataDir,
        NOVEL_AI_PROVIDER: "demo",
        NOVEL_LOCAL_EMBEDDING_MODEL: "",
        NOVEL_E2E_RESET_TOKEN: e2eResetToken,
      },
      timeout: 30_000,
    },
    {
      command: `npm --prefix ../frontend run dev -- --host 127.0.0.1 --port ${frontendPort} --strictPort`,
      port: frontendPort,
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
});
