import assert from "node:assert/strict";
import test from "node:test";

import { isParentAgentMessage } from "../src/lib/conversation-origin.ts";
import { clawDelegateGroupCount, partitionDashboardRecent } from "../src/lib/dashboard-recent.ts";

test("explicit human origin is You even inside a parent-agent thread", () => {
  assert.equal(
    isParentAgentMessage({ role: "user", origin: "human" }, "parent_agent"),
    false,
  );
});

test("explicit parent_agent origin wins over a human thread", () => {
  assert.equal(
    isParentAgentMessage({ role: "user", origin: "parent_agent" }, null),
    true,
  );
});

test("unset origin falls back to thread-level parent_agent", () => {
  assert.equal(
    isParentAgentMessage({ role: "user" }, "parent_agent"),
    true,
  );
  assert.equal(
    isParentAgentMessage({ role: "user" }, null),
    false,
  );
});

test("Recent collapses claw delegates out of the ten-slot list", () => {
  const rows = [
    { id: "a", orchestration: null, is_low_activity: false, pending_question_count: 0 },
    { id: "b", orchestration: "claw", is_low_activity: false, pending_question_count: 0 },
    { id: "c", orchestration: "claw", is_low_activity: true, pending_question_count: 0 },
    { id: "d", orchestration: "claw", is_low_activity: false, pending_question_count: 1 },
  ];
  const partitioned = partitionDashboardRecent(rows);
  assert.deepEqual(partitioned.active.map((row) => row.id), ["a"]);
  assert.deepEqual(partitioned.clawDelegates.map((row) => row.id), ["b", "c"]);
  assert.deepEqual(partitioned.attention.map((row) => row.id), ["d"]);
  assert.deepEqual(partitioned.lowActivity.map((row) => row.id), []);
});

test("claw group count uses the server aggregate over a bounded sample", () => {
  const sample = [
    { id: "c1" },
    { id: "c2" },
  ];
  assert.equal(clawDelegateGroupCount(sample, 21), 21);
  assert.equal(clawDelegateGroupCount(sample), 2);
});
