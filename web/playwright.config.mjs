// @ts-check
import { defineConfig, devices } from "@playwright/test";

const port = Number.parseInt(process.env.MEMENTO_E2E_PORT ?? "3100", 10);
if (!Number.isInteger(port) || port < 1 || port > 65_535) {
  throw new Error(`Invalid MEMENTO_E2E_PORT: ${process.env.MEMENTO_E2E_PORT}`);
}
const baseURL = `http://localhost:${port}`;
const reuseExistingServer = process.env.MEMENTO_E2E_REUSE_SERVER === "1";

/**
 * Playwright configuration for the Memento web regression suite (Workstream D).
 *
 * These specs are hermetic: every `/api/**` request is intercepted and answered
 * from `e2e/fixtures`, so no live backend, database, or collector is required.
 * A local `next dev` server renders the REAL app so the tests exercise the real
 * ConversationViewer / SubagentBadge / prompt navigator rendering paths.
 *
 * PREREQUISITES:
 *   1. Node.js >= 20 (Next 16 and Playwright both require it; the repo's system
 *      node is 18, so use the user-local Node 24 install under ~/.local).
 *   2. npm ci
 *   3. npx playwright install chromium      (NO `install-deps`, NO sudo)
 *
 * RUN:
 *   ./run-playwright.ps1                                     # durable WSL setup
 *   npx playwright test -c playwright.config.mjs            # Node >= 20 on PATH
 *   npx playwright test -c playwright.config.mjs --list     # enumerate only
 *
 * The suite runs single-worker on purpose to keep machine load low (no large
 * parallel browser matrices).
 */
export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.mjs",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  timeout: 30_000,
  expect: { timeout: 7_500 },
  use: {
    baseURL,
    viewport: { width: 1440, height: 900 },
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    command: `npm run dev -- -H 127.0.0.1 -p ${port}`,
    url: `${baseURL}/auth/login`,
    reuseExistingServer,
    timeout: 120_000,
    // Same-origin API base → intercepted requests need no CORS negotiation.
    env: { NEXT_PUBLIC_MEMENTO_API_BASE: baseURL },
  },
});
