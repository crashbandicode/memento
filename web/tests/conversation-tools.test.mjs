import assert from "node:assert/strict";
import test from "node:test";

import { isMetaConversationTool } from "../src/lib/conversation-tools.ts";

test("SendFeedback is always classified as an inert conversation meta-tool", () => {
  assert.equal(isMetaConversationTool("SendFeedback"), true);
  assert.equal(isMetaConversationTool("send_feedback"), true);
  assert.equal(isMetaConversationTool("Bash"), false);
  assert.equal(isMetaConversationTool(undefined), false);
});
