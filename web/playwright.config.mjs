// @ts-check
import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright configuration for the Memento web regression suite (Workstream D).
 *
 * These specs are hermetic: every `/api/**` request is intercepted and answered
 * from `e2e/fixtures`, so no live backend, database, or collector is required.
 * A local `next dev` server renders the REAL app so the tests exercise the real
 * ConversationViewer / SubagentBadge / prompt navigator rendering paths.
 *
 * PREREQUISITES (kept out of package.json intentionally — this is an additive,
 * test-only harness that must not perturb the app's dependency set):
 *   1. Node.js >= 20 (Next 16 and Playwright both require it; the repo's system
 *      node is 18, so use nvm/fnm or the Windows Node 24 toolchain).
 *   2. npm i -D @playwright/test
 *   3. npx playwright install chromium      (NO `install-deps`, NO sudo)
 *
 * RUN:
 *   npx playwright test -c playwright.config.mjs            # all specs
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
    baseURL: "http://localhost:3100",
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
    command: "npm run dev -- -p 3100",
    url: "http://localhost:3100/auth/login",
    reuseExistingServer: true,
    timeout: 120_000,
    // Same-origin API base → intercepted requests need no CORS negotiation.
    env: { NEXT_PUBLIC_MEMENTO_API_BASE: "http://localhost:3100" },
  },
});
