// @ts-check
/**
 * Managed agent-control UI: the conversation panel and the devices entry
 * point. Hermetic — every /api/** request is answered from fixtures; control
 * routes are layered on top of the standard conversation mocks.
 *
 * The managed-vs-view-only boundary is the core assertion: a conversation
 * without a control session must render zero control affordances.
 */

import { expect, test } from "@playwright/test";

import { metadataOnlyPrompts } from "./fixtures/conversation-scenarios.mjs";
import { installConversationMocks, seedAuth } from "./support/conversation-page.mjs";

const SESSION_ID = "11111111-2222-4333-8444-555555555555";

function managedSession(overrides = {}) {
  return {
    id: SESSION_ID,
    machine_id: "99999999-8888-4777-8666-555555555555",
    tool_id: "codex",
    adapter: "codex_app_server",
    adapter_version: null,
    native_session_id: "thr_live_1",
    document_id: metadataOnlyPrompts.docId,
    state: "active",
    state_reason: null,
    active_native_turn_id: null,
    pending_interactions: [],
    started_at: "2026-08-22T12:00:00Z",
    last_event_at: "2026-08-22T12:05:00Z",
    closed_at: null,
    created_at: "2026-08-22T12:00:00Z",
    ...overrides,
  };
}

const QUESTION_INTERACTION = {
  interaction_id: "int-q-1",
  kind: "question",
  method: "item/tool/requestUserInput",
  native_turn_id: "turn_q",
  request: {
    isBlocking: true,
    questions: [
      {
        id: "q1",
        header: "Choice",
        question: "Which path should I take?",
        options: [
          { label: "left", description: "go left" },
          { label: "right", description: "go right" },
        ],
        isOther: true,
        isSecret: false,
      },
    ],
  },
  received_at: "2026-08-22T12:04:00Z",
};

const APPROVAL_INTERACTION = {
  interaction_id: "int-a-1",
  kind: "approval",
  method: "item/commandExecution/requestApproval",
  native_turn_id: "turn_a",
  request: { command: "rm -rf ./scratch", cwd: "/tmp", reason: "cleanup" },
  received_at: "2026-08-22T12:04:30Z",
};

/**
 * @param {import('@playwright/test').Page} page
 * @param {{ sessions: () => any[], capture: Array<{path: string, body: any}>, queries?: string[], conversationRef?: string }} control
 */
async function openManagedConversation(page, control) {
  await seedAuth(page);
  await installConversationMocks(page, metadataOnlyPrompts);
  // Registered after the catch-all so Playwright matches it first.
  await page.route("**/api/control/**", async (route) => {
    const request = route.request();
    const requestUrl = new URL(request.url());
    const pathname = requestUrl.pathname;
    if (request.method() === "GET" && pathname.endsWith("/api/control/sessions")) {
      control.queries?.push(requestUrl.searchParams.get("document_id") || "");
      await route.fulfill({ status: 200, json: control.sessions() });
      return;
    }
    if (request.method() === "POST") {
      control.capture.push({ path: pathname, body: request.postDataJSON() });
      await route.fulfill({
        status: 200,
        json: {
          command: {
            id: "cmd-fixture-1",
            trace_id: "trace-fixture-1",
            kind: "agent.fixture",
            state: "queued",
            error_code: null,
            outcome: null,
          },
        },
      });
      return;
    }
    await route.fulfill({ status: 200, json: {} });
  });
  await page.goto(`/conversations/${control.conversationRef ?? metadataOnlyPrompts.docId}`);
  await page.waitForSelector("[data-conversation-viewer]", { timeout: 15000 });
}

test("native conversation URLs wait for the canonical document id before control lookup", async ({ page }) => {
  const queries = [];
  await openManagedConversation(page, {
    sessions: () => [],
    capture: [],
    queries,
    conversationRef: "claude/native-thread-fixture",
  });

  await expect.poll(() => queries.length).toBeGreaterThan(0);
  expect(new Set(queries)).toEqual(new Set([metadataOnlyPrompts.docId]));
  expect(queries).not.toContain("claude~native-thread-fixture");
});

test("view-only conversations render zero control affordances", async ({ page }) => {
  await openManagedConversation(page, { sessions: () => [], capture: [] });
  await expect(page.locator("[data-managed-session]")).toHaveCount(0);
  await expect(page.locator("[data-control-composer]")).toHaveCount(0);
});

test("active managed session shows the composer and sends a message", async ({ page }) => {
  const capture = [];
  await openManagedConversation(page, { sessions: () => [managedSession()], capture });

  const panel = page.locator("[data-managed-session]");
  await expect(panel).toHaveAttribute("data-managed-session-state", "active");
  await page.locator("[data-control-composer]").fill("run the test suite");
  await page.locator("[data-control-send]").click();

  await expect.poll(() => capture.length).toBeGreaterThan(0);
  expect(capture[0].path).toBe(`/api/control/sessions/${SESSION_ID}/messages`);
  expect(capture[0].body.text).toBe("run the test suite");
});

test("pending question is answerable and posts fenced answers", async ({ page }) => {
  const capture = [];
  await openManagedConversation(page, {
    sessions: () => [managedSession({ pending_interactions: [QUESTION_INTERACTION] })],
    capture,
  });

  const pending = page.locator('[data-control-pending-kind="question"]');
  await expect(pending).toBeVisible();
  await pending.locator('[data-control-question-option]', { hasText: "left" }).click();
  await pending.locator("[data-control-answer-submit]").click();

  await expect.poll(() => capture.length).toBeGreaterThan(0);
  expect(capture[0].path).toBe(
    `/api/control/sessions/${SESSION_ID}/interactions/int-q-1/answer`,
  );
  expect(capture[0].body.answers).toEqual({ q1: { answers: ["left"] } });
});

test("custom answers override option selection", async ({ page }) => {
  const capture = [];
  await openManagedConversation(page, {
    sessions: () => [managedSession({ pending_interactions: [QUESTION_INTERACTION] })],
    capture,
  });

  await page.locator("[data-control-question-custom]").fill("take the scenic route");
  await page.locator("[data-control-answer-submit]").click();

  await expect.poll(() => capture.length).toBeGreaterThan(0);
  expect(capture[0].body.answers).toEqual({
    q1: { answers: ["take the scenic route"] },
  });
});

test("approvals post the exact decision", async ({ page }) => {
  const capture = [];
  await openManagedConversation(page, {
    sessions: () => [managedSession({ pending_interactions: [APPROVAL_INTERACTION] })],
    capture,
  });

  const pending = page.locator('[data-control-pending-kind="approval"]');
  await expect(pending).toContainText("rm -rf ./scratch");
  await pending.locator("[data-control-approval-decline]").click();

  await expect.poll(() => capture.length).toBeGreaterThan(0);
  expect(capture[0].path).toBe(
    `/api/control/sessions/${SESSION_ID}/interactions/int-a-1/approval`,
  );
  expect(capture[0].body.decision).toBe("decline");
});

test("an active turn switches the composer to steer with the exact turn fence", async ({ page }) => {
  const capture = [];
  await openManagedConversation(page, {
    sessions: () => [managedSession({ active_native_turn_id: "turn_busy" })],
    capture,
  });

  const send = page.locator("[data-control-send]");
  await expect(send).toHaveAttribute("data-control-send-mode", "steer");
  await page.locator("[data-control-composer]").fill("focus on the failing tests");
  await send.click();

  await expect.poll(() => capture.length).toBeGreaterThan(0);
  expect(capture[0].path).toBe(`/api/control/sessions/${SESSION_ID}/steer`);
  expect(capture[0].body.text).toBe("focus on the failing tests");
  expect(capture[0].body.expected_turn_id).toBe("turn_busy");
});

test("permission requests grant only the selected subset", async ({ page }) => {
  const capture = [];
  const permissionInteraction = {
    interaction_id: "int-p-1",
    kind: "approval",
    method: "item/permissions/requestApproval",
    native_turn_id: "turn_p",
    request: {
      reason: "Need write access",
      permissions: {
        fileSystem: { write: ["/repo/a", "/repo/b"] },
        network: { enabled: true },
      },
    },
    received_at: "2026-08-22T12:04:45Z",
  };
  await openManagedConversation(page, {
    sessions: () => [managedSession({ pending_interactions: [permissionInteraction] })],
    capture,
  });

  const options = page.locator("[data-control-grant-option]");
  await expect(options).toHaveCount(3);
  // Deny the second write path; keep the first and network access.
  await options.nth(1).locator("input").uncheck();
  await page.locator("[data-control-approval-accept]").click();

  await expect.poll(() => capture.length).toBeGreaterThan(0);
  expect(capture[0].path).toBe(
    `/api/control/sessions/${SESSION_ID}/interactions/int-p-1/approval`,
  );
  expect(capture[0].body.decision).toBe("accept");
  expect(capture[0].body.granted_permissions).toEqual({
    fileSystem: { write: ["/repo/a"] },
    network: { enabled: true },
  });
});

test("an active turn exposes interrupt and posts it", async ({ page }) => {
  const capture = [];
  await openManagedConversation(page, {
    sessions: () => [managedSession({ active_native_turn_id: "turn_busy" })],
    capture,
  });

  await expect(page.locator("[data-control-active-turn]")).toBeVisible();
  await page.locator("[data-control-interrupt]").click();

  await expect.poll(() => capture.length).toBeGreaterThan(0);
  expect(capture[0].path).toBe(`/api/control/sessions/${SESSION_ID}/interrupt`);
});

test("failed sessions surface the reason and hide the composer", async ({ page }) => {
  await openManagedConversation(page, {
    sessions: () => [
      managedSession({ state: "failed", state_reason: "adapter.process_failed" }),
    ],
    capture: [],
  });

  const panel = page.locator("[data-managed-session]");
  await expect(panel).toHaveAttribute("data-managed-session-state", "failed");
  await expect(panel).toContainText("adapter.process_failed");
  await expect(page.locator("[data-control-composer]")).toHaveCount(0);
});

test("mobile viewport keeps the managed panel inside the screen", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openManagedConversation(page, {
    sessions: () => [managedSession({ pending_interactions: [QUESTION_INTERACTION] })],
    capture: [],
  });

  const panel = page.locator("[data-managed-session]");
  await expect(panel).toBeVisible();
  const box = await panel.boundingBox();
  expect(box).not.toBeNull();
  expect((box?.x ?? 0) >= 0).toBe(true);
  expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(391);
});

test("devices page can start a managed Codex session", async ({ page }) => {
  const capture = [];
  await seedAuth(page);
  await installConversationMocks(page, metadataOnlyPrompts);
  await page.route("**/api/devices", async (route) => {
    await route.fulfill({
      status: 200,
      json: [
        {
          id: "99999999-8888-4777-8666-555555555555",
          name: "butterbridge (Windows)",
          device_id: "device-fixture",
          collector_version: "0.0.42",
          last_heartbeat: new Date().toISOString(),
          created_at: "2026-08-01T00:00:00Z",
          document_count: 12,
          tools: ["codex", "claude_code"],
          managed_agents: ["codex"],
        },
      ],
    });
  });
  await page.route("**/api/devices/*/discovery", async (route) => {
    await route.fulfill({
      status: 200,
      json: { device_id: "x", tools: { codex: { root: "C:/Users/intpa/.codex" } } },
    });
  });
  await page.route("**/api/control/sessions", async (route) => {
    const request = route.request();
    if (request.method() === "POST") {
      capture.push({ body: request.postDataJSON() });
      await route.fulfill({
        status: 200,
        json: {
          session: managedSession({ state: "starting", document_id: null }),
          command: { id: "cmd-1", trace_id: "t-1", kind: "agent.session.start", state: "queued", error_code: null, outcome: null },
        },
      });
      return;
    }
    await route.fulfill({ status: 200, json: [] });
  });

  await page.goto("/devices");
  await page.locator("[data-start-codex-session]").click();
  await expect(page.locator("[data-start-control-session]")).toBeVisible();
  await page.locator("[data-start-session-message]").fill("MEMENTO test objective");
  await page.locator("[data-start-session-approval-policy]").selectOption("untrusted");
  await page.locator("[data-start-session-sandbox]").selectOption("workspace-write");
  await page.locator("[data-start-session-submit]").click();

  await expect.poll(() => capture.length).toBeGreaterThan(0);
  expect(capture[0].body.machine_id).toBe("99999999-8888-4777-8666-555555555555");
  expect(capture[0].body.initial_message).toBe("MEMENTO test objective");
  expect(capture[0].body.approval_policy).toBe("untrusted");
  expect(capture[0].body.sandbox).toBe("workspace-write");
  await expect(page.locator("[data-start-session-status]")).toBeVisible();
});
