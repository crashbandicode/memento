import assert from "node:assert/strict";
import test from "node:test";

import {
  contextBeforeIncludingTarget,
  mergeMessagesChronologically,
  placeTargetWindow,
} from "../src/lib/conversation-message-order.ts";

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
