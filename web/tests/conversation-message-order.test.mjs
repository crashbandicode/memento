import assert from "node:assert/strict";
import test from "node:test";

import {
  coalesceTaskStateCallResults,
  contextBeforeIncludingTarget,
  isMirroredActiveTaskMessage,
  mergeMessagesChronologically,
  placeTargetWindow,
} from "../src/lib/conversation-message-order.ts";

const taskSnapshot = {
  source: "claude_code",
  revision: 70,
  completed_count: 17,
  total_count: 20,
  tasks: [{ id: "18", content: "Write findings", status: "pending" }],
};

test("adjacent task call and result render as one finalized snapshot", () => {
  const call = {
    id: 1,
    line_number: 1045,
    role: "tool",
    message_type: "tool_use",
    tool_name: "TaskCreate",
    tool_call_id: "toolu_task_18",
    task_state: { ...taskSnapshot, revision: 69 },
  };
  const result = {
    id: 2,
    line_number: 1046,
    role: "tool",
    message_type: "tool_result",
    tool_name: "Tool result",
    tool_call_id: "toolu_task_18",
    task_state: taskSnapshot,
  };

  assert.deepEqual(coalesceTaskStateCallResults([call, result]), [result]);
});

test("task snapshots are not coalesced across a gap or mismatched call id", () => {
  const call = {
    id: 1,
    line_number: 10,
    role: "tool",
    message_type: "tool_use",
    tool_name: "TaskUpdate",
    tool_call_id: "call-one",
    task_state: taskSnapshot,
  };
  const result = {
    id: 2,
    line_number: 12,
    role: "tool",
    message_type: "tool_result",
    tool_name: "Tool result",
    tool_call_id: "call-two",
    task_state: taskSnapshot,
  };

  assert.deepEqual(coalesceTaskStateCallResults([call, result]), [call, result]);
});

test("orphaned task rows and ordinary tool pairs remain visible", () => {
  const orphanedResult = {
    id: 1,
    line_number: 20,
    role: "tool",
    message_type: "tool_result",
    tool_name: "Tool result",
    tool_call_id: "orphan",
    task_state: taskSnapshot,
  };
  const ordinaryCall = {
    id: 2,
    line_number: 21,
    role: "tool",
    message_type: "tool_use",
    tool_name: "Read",
    tool_call_id: "read-one",
  };
  const ordinaryResult = {
    id: 3,
    line_number: 22,
    role: "tool",
    message_type: "tool_result",
    tool_name: "Tool result",
    tool_call_id: "read-one",
  };

  assert.deepEqual(
    coalesceTaskStateCallResults([orphanedResult, ordinaryCall, ordinaryResult]),
    [orphanedResult, ordinaryCall, ordinaryResult],
  );
});

test("live reparses replace rows by server line instead of database id", () => {
  const stale = [
    { id: 1001, line_number: 10, content: "old ten" },
    { id: 1002, line_number: 11, content: "old eleven" },
  ];
  const refreshed = [
    { id: 9001, line_number: 10, content: "new ten" },
    { id: 9002, line_number: 11, content: "new eleven" },
  ];

  assert.deepEqual(
    mergeMessagesChronologically(stale, refreshed),
    refreshed,
  );
});

test("server lines determine order even when timestamps move backwards", () => {
  const rows = mergeMessagesChronologically(
    [{ id: "later-id", line_number: 20, timestamp: "2026-07-31T00:00:00Z" }],
    [{ id: "earlier-id", line_number: 21, timestamp: "2026-07-20T00:00:00Z" }],
  );

  assert.deepEqual(rows.map((row) => row.line_number), [20, 21]);
});

test("duplicate source ids remain distinct when server lines differ", () => {
  const rows = mergeMessagesChronologically([], [
    { id: 1, line_number: 30, source_id: "repeated" },
    { id: 2, line_number: 31, source_id: "repeated" },
  ]);

  assert.equal(rows.length, 2);
  assert.deepEqual(rows.map((row) => row.line_number), [30, 31]);
});

test("around-window context cannot put the target beyond the response limit", () => {
  assert.equal(contextBeforeIncludingTarget(200, 120), 119);
  assert.equal(contextBeforeIncludingTarget(12, 120), 12);
  assert.equal(contextBeforeIncludingTarget(12, 1), 0);
});

test("a disjoint target window stays detached from the prompt window", () => {
  const current = [
    { id: 1, line_number: 88 },
    { id: 2, line_number: 487 },
  ];
  const incoming = {
    offset: 837,
    messages: [
      { id: 3, line_number: 838 },
      { id: 4, line_number: 839 },
    ],
  };

  assert.deepEqual(placeTargetWindow(current, 487, incoming), {
    messages: current,
    contiguousEnd: 487,
    detached: {
      offset: 837,
      endOffset: 839,
      messages: incoming.messages,
    },
  });
});

test("an overlapping target window extends the contiguous range", () => {
  const placed = placeTargetWindow(
    [
      { id: "old-10", line_number: 10 },
      { id: "old-11", line_number: 11 },
    ],
    11,
    {
      offset: 10,
      messages: [
        { id: "new-11", line_number: 11 },
        { id: "new-12", line_number: 12 },
      ],
    },
  );

  assert.equal(placed.contiguousEnd, 12);
  assert.equal(placed.detached, null);
  assert.deepEqual(
    placed.messages.map((row) => [row.line_number, row.id]),
    [
      [10, "old-10"],
      [11, "new-11"],
      [12, "new-12"],
    ],
  );
});

test("the current Cursor task carrier is not rendered twice", () => {
  const current = {
    source: "cursor",
    is_current: true,
    total_count: 2,
    tasks: [
      { id: "1", content: "Audit source", status: "completed" },
      { id: "2", content: "Verify UI", status: "in_progress" },
    ],
  };

  assert.equal(
    isMirroredActiveTaskMessage({ task_state: current }, current),
    true,
  );
  assert.equal(
    isMirroredActiveTaskMessage({
      task_state: {
        ...current,
        is_current: false,
      },
    }, current),
    false,
  );
  assert.equal(
    isMirroredActiveTaskMessage({
      task_state: {
        ...current,
        tasks: [
          current.tasks[0],
          { id: "2", content: "Verify UI", status: "completed" },
        ],
      },
    }, current),
    false,
  );
});
