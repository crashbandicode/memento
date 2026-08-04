import assert from "node:assert/strict";
import test from "node:test";

import { convert } from "@slackfmt/core";

test("slackfmt converts markdown to slack/texty delta ops", async () => {
  const delta = await convert(
    "## Engineer takeover\n\n**Bold** and _italic_ with a [link](https://example.com).\n\n1. First\n2. Second\n\n- bullet\n\n```ts\nconst x = 1;\n```",
    { format: "markdown" },
  );
  const parsed = JSON.parse(delta);
  assert.ok(Array.isArray(parsed.ops));
  assert.ok(parsed.ops.length > 0);
  const encoded = JSON.stringify(parsed.ops);
  assert.match(encoded, /bold|italic|link|list|code/i);
});
