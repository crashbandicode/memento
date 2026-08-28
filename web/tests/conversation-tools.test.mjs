import assert from "node:assert/strict";
import test from "node:test";

import {
  filterMetaConversationInteractions,
  isMetaConversationTool,
} from "../src/lib/conversation-tools.ts";

test("SendFeedback is always classified as an inert conversation meta-tool", () => {
  assert.equal(isMetaConversationTool("SendFeedback"), true);
  assert.equal(isMetaConversationTool("send_feedback"), true);
  assert.equal(isMetaConversationTool("Bash"), false);
  assert.equal(isMetaConversationTool(undefined), false);
});

test("historic meta-tool interactions are removed from pending and inline collections", () => {
  const items = [
    { interaction: { id: "feedback", tool_name: "SendFeedback" } },
    { interaction: { id: "question", tool_name: "AskUserQuestion" } },
  ];

  assert.deepEqual(
    filterMetaConversationInteractions(items).map((item) => item.interaction.id),
    ["question"],
  );
});
