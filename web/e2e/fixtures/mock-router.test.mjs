// @ts-check
/**
 * Browser-free coverage for the hermetic mock layer + fixture invariants.
 *
 * Run with:  node --test web/e2e/fixtures/mock-router.test.mjs
 *
 * This asserts (a) the router maps each conversation endpoint to the right
 * fixture and never falls through to a live backend, and (b) each scenario
 * still encodes the exact regression it is meant to guard. If a future fixture
 * edit accidentally weakens a scenario (e.g. drops the unwrapped question, or
 * flips a running subagent to completed), these tests fail immediately — well
 * before the slower Playwright browser run.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { resolveConversationRoute, pathnameOf } from "./mock-router.mjs";
import {
  FIXTURE_USER,
  metadataOnlyPrompts,
  parentAgentLabeling,
  permissionWrappedQuestion,
  runningSubagent,
  scenarios,
} from "./conversation-scenarios.mjs";

const base = "http://localhost:3100";

test("SSE stream is aborted so specs stay metadata-only", () => {
  const result = resolveConversationRoute({
    url: `${base}/api/events/stream`,
    scenario: metadataOnlyPrompts,
  });
  assert.deepEqual(result, { action: "abort" });
});

test("auth/me returns the seeded fixture user", () => {
  const result = resolveConversationRoute({
    url: `${base}/api/auth/me`,
    scenario: metadataOnlyPrompts,
  });
  assert.equal(result.action, "fulfill");
  assert.equal(result.status, 200);
  assert.deepEqual(result.json, FIXTURE_USER);
});

test("conversation meta resolves for the exact document path", () => {
  const result = resolveConversationRoute({
    url: `${base}/api/conversations/${permissionWrappedQuestion.docId}`,
    scenario: permissionWrappedQuestion,
  });
  assert.equal(result.action, "fulfill");
  assert.equal(result.json, permissionWrappedQuestion.meta);
});

test("messages endpoint wraps fixture messages in a MessagesResponse", () => {
  const result = resolveConversationRoute({
    url: `${base}/api/conversations/${permissionWrappedQuestion.docId}/messages?offset=0&limit=50`,
    scenario: permissionWrappedQuestion,
  });
  assert.equal(result.action, "fulfill");
  assert.equal(result.json.total, permissionWrappedQuestion.messages.length);
  assert.equal(result.json.offset, 0);
  assert.equal(result.json.messages, permissionWrappedQuestion.messages);
});

test("prompts endpoint is matched before the bare conversation route", () => {
  const result = resolveConversationRoute({
    url: `${base}/api/conversations/${metadataOnlyPrompts.docId}/prompts`,
    scenario: metadataOnlyPrompts,
  });
  assert.equal(result.action, "fulfill");
  assert.deepEqual(result.json, { prompts: metadataOnlyPrompts.prompts });
});

test("pending-interactions and latest-agent-message resolve distinctly", () => {
  const pending = resolveConversationRoute({
    url: `${base}/api/conversations/${runningSubagent.docId}/pending-interactions`,
    scenario: runningSubagent,
  });
  assert.deepEqual(pending.json, runningSubagent.pending);

  const latest = resolveConversationRoute({
    url: `${base}/api/conversations/${runningSubagent.docId}/latest-agent-message`,
    scenario: runningSubagent,
  });
  assert.deepEqual(latest.json, { line_number: runningSubagent.latestAgentLine });
});

test("unknown endpoints fall back to empty JSON, never the network", () => {
  const obj = resolveConversationRoute({
    url: `${base}/api/inbox/counts`,
    scenario: metadataOnlyPrompts,
  });
  assert.deepEqual(obj, { action: "fulfill", status: 200, json: {} });

  const list = resolveConversationRoute({
    url: `${base}/api/tools`,
    scenario: metadataOnlyPrompts,
  });
  assert.deepEqual(list.json, []);
});

test("pathnameOf tolerates relative and absolute URLs", () => {
  assert.equal(pathnameOf("/api/conversations/x/messages?a=1"), "/api/conversations/x/messages");
  assert.equal(pathnameOf("https://memento.test:8001/api/auth/me"), "/api/auth/me");
});

// --- Fixture invariants: each scenario still guards its regression -----------

test("regression #1/#3: permission wrapper is unwrapped to the real question", () => {
  const call = permissionWrappedQuestion.messages[1].tool_calls[0];
  const interaction = call.interaction;
  assert.equal(interaction.interaction_type, "permission_request");
  assert.equal(interaction.requested_tool, "AskUserQuestion");
  // The real human question + real options must be present...
  assert.equal(interaction.questions.length, 1);
  assert.match(interaction.questions[0].prompt, /Which database should power/);
  assert.deepEqual(
    interaction.questions[0].options.map((o) => o.label),
    ["PostgreSQL", "MySQL"],
  );
  // ...and a raw-envelope sentinel is present in the tool input so the spec can
  // prove the viewer replaced it with the question card instead of dumping it.
  assert.match(call.input, /RAW_PERMISSION_ENVELOPE_SHOULD_NOT_RENDER/);
  assert.match(call.input, /"permission_suggestion":"Yes"/);
});

test("regression #2: prompts exist independent of any SSE event", () => {
  assert.ok(metadataOnlyPrompts.prompts.length >= 1);
  assert.match(metadataOnlyPrompts.prompts[0].content, /flaky checkout/);
});

test("regression #4: subagent is running with a launch but no completion", () => {
  const sub = runningSubagent.meta.subagents[0];
  assert.equal(sub.status, "running");
  assert.equal(sub.completed_at, null);
  const kinds = runningSubagent.messages
    .filter((m) => m.agent_event)
    .map((m) => m.agent_event.kind);
  assert.ok(kinds.includes("started"), "a launch event must be present");
  assert.ok(!kinds.includes("completed"), "no completion event may be present");
  const snapshot = runningSubagent.messages.find((m) => m.agent_event?.kind === "snapshot");
  assert.equal(snapshot.agent_event.agents[0].status, "running");
});

test("regression #5: parent-agent origin + launch description title", () => {
  assert.equal(parentAgentLabeling.meta.user_role_origin, "parent_agent");
  assert.equal(
    parentAgentLabeling.meta.metadata.agent_launch_description,
    "Investigate checkout regression",
  );
  const dispatch = parentAgentLabeling.messages[0];
  assert.equal(dispatch.role, "user");
  assert.equal(dispatch.origin, "parent_agent");
});

test("every registered scenario has the endpoints a spec will request", () => {
  for (const [docId, scenario] of Object.entries(scenarios)) {
    assert.equal(scenario.docId, docId);
    assert.ok(scenario.meta, `${docId} meta`);
    assert.ok(Array.isArray(scenario.messages), `${docId} messages`);
    assert.ok(Array.isArray(scenario.prompts), `${docId} prompts`);
    assert.ok(scenario.pending, `${docId} pending`);
  }
});
