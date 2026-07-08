import assert from "node:assert/strict";
import test from "node:test";

import {
  conversationSessionKey,
  mergeConversationSessions,
} from "../src/lib/conversation-sessions.ts";

test("session identity prefers the logical thread UUID", () => {
  assert.equal(
    conversationSessionKey({
      session_id: "thread-1",
      conversation_id: "document-1",
    }),
    "thread-1",
  );
});

test("multi-host document copies collapse to one logical session", () => {
  const firstPage = [{
    session_id: "thread-1",
    conversation_id: "host-a-document",
    title: "older copy",
  }];
  const overlappingPage = [{
    session_id: "thread-1",
    conversation_id: "host-b-document",
    title: "newer copy",
  }];

  assert.deepEqual(
    mergeConversationSessions(firstPage, overlappingPage),
    [overlappingPage[0]],
  );
});

test("document UUID remains the fallback for legacy sessions", () => {
  const sessions = [
    { session_id: "", conversation_id: "document-1" },
    { session_id: "", conversation_id: "document-2" },
  ];

  assert.deepEqual(mergeConversationSessions([], sessions), sessions);
});

test("an orphan card is replaced when its root arrives", () => {
  const orphan = {
    logical_session_id: "root-thread",
    session_id: "child-thread",
    conversation_id: "child-document",
    title: "orphan child",
  };
  const root = {
    logical_session_id: "root-thread",
    session_id: "root-thread",
    conversation_id: "root-document",
    title: "canonical root",
  };

  assert.deepEqual(mergeConversationSessions([orphan], [root]), [root]);
});
