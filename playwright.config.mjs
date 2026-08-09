import { defineConfig } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(fileURLToPath(import.meta.url));
const frontend = path.join(root, "frontend");
const dataDir = path.join(root, ".playwright-data");
const E2E_TOKEN = process.env.E2E_API_TOKEN || "e2e-playwright-token";

export default defineConfig({
  testDir: "tests/e2e",
  timeout: 60_000,
  retries: 0,
  use: {
    baseURL: "http://127.0.0.1:18999",
    headless: true,
  },
  webServer: {
    command: `python -m uvicorn app.main:app --host 127.0.0.1 --port 18999`,
    cwd: path.join(root, "backend"),
    url: "http://127.0.0.1:18999/api/healthz",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: {
      ...process.env,
      FRONTEND_DIR: frontend,
      DATA_DIR: dataDir,
      TORRENT_PORT: "0",
      API_TOKEN: E2E_TOKEN,
      // Auth must be on for this suite — do not allow unauthenticated private API.
      ALLOW_UNAUTHENTICATED_API: "",
      E2E_API_TOKEN: E2E_TOKEN,
    },
  },
});
