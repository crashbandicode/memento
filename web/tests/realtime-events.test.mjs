import assert from "node:assert/strict";
import test from "node:test";

import {
  buildEventStreamUrl,
  conversationInvalidationForEvent,
  eventInvalidatesDashboard,
  NO_CONVERSATION_INVALIDATION,
} from "../src/lib/realtime-events.ts";


function event(data, type = "file_synced") {
  return { id: "100-0", type, data, timestamp: 1 };
}


test("reconnect URL carries the last processed event ID", () => {
  assert.equal(
    buildEventStreamUrl("https://memento.test", "1712345678901-12"),
    "https://memento.test/api/events/stream?cursor=1712345678901-12",
  );
  assert.equal(
    buildEventStreamUrl("https://memento.test", ""),
    "https://memento.test/api/events/stream",
  );
});


test("exact conversation updates invalidate only declared resources", () => {
  const invalidation = conversationInvalidationForEvent(
    event({
      document_id: "current",
      category: "conversation",
      changes: ["conversation.messages", "conversation.search"],
    }),
    "current",
  );

  assert.deepEqual(invalidation, {
    messages: true,
    metadata: false,
    pendingInteractions: false,
    prompts: false,
    search: true,
  });
});


test("unrelated conversation updates trigger no fetches", () => {
  const invalidation = conversationInvalidationForEvent(
    event({
      document_id: "other",
      tool_id: "codex",
      category: "conversation",
      relative_path: "sessions/other.jsonl",
      changes: [
        "conversation.messages",
        "conversation.metadata",
        "conversation.pending_interactions",
        "conversation.prompts",
      ],
    }),
    "current",
    { toolId: "codex", relativePath: "sessions/current.jsonl" },
  );

  assert.deepEqual(invalidation, NO_CONVERSATION_INVALIDATION);
});


test("linked child updates refresh parent metadata without transcript fetches", () => {
  const invalidation = conversationInvalidationForEvent(
    event({
      document_id: "child",
      tool_id: "claude_code",
      category: "conversation",
      relative_path: "projects/root/subagents/agent-child.jsonl",
      changes: ["conversation.metadata"],
    }),
    "parent",
    {
      toolId: "claude_code",
      relativePath: "projects/root/root.jsonl",
    },
  );

  assert.deepEqual(invalidation, {
    messages: false,
    metadata: true,
    pendingInteractions: false,
    prompts: false,
    search: false,
  });
});


test("legacy events and replay resets retain safe rolling-upgrade behavior", () => {
  assert.deepEqual(
    conversationInvalidationForEvent(
      event({ document_id: "current", category: "conversation" }),
      "current",
    ),
    {
      messages: true,
      metadata: true,
      pendingInteractions: true,
      prompts: true,
      search: true,
    },
  );
  assert.deepEqual(
    conversationInvalidationForEvent(
      event({ reason: "replay_trimmed" }, "realtime_reset"),
      "current",
    ),
    {
      messages: true,
      metadata: true,
      pendingInteractions: true,
      prompts: true,
      search: true,
    },
  );
});


test("dashboard refreshes only for dashboard-scoped updates or resets", () => {
  assert.equal(
    eventInvalidatesDashboard(event({
      changes: ["conversation.pending_interactions"],
    })),
    false,
  );
  assert.equal(
    eventInvalidatesDashboard(event({ changes: ["dashboard"] })),
    true,
  );
  assert.equal(
    eventInvalidatesDashboard(
      event({ reason: "replay_expired" }, "realtime_reset"),
    ),
    true,
  );
});
