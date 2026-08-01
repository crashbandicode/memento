import assert from "node:assert/strict";
import test from "node:test";

import {
  EMPTY_CONVERSATION_URL_STATE,
  MAX_CONVERSATION_QUERY_LENGTH,
  clearConversationSearchState,
  decideConversationHistoryMode,
  parseConversationUrlState,
  serializeConversationUrlState,
} from "../src/lib/conversation-url-state.ts";

test("conversation URL state round-trips Unicode search and stable anchors", () => {
  const query = "修复 refresh 🧭 café";
  const state = {
    line: 8_421,
    position: 617,
    query,
    scope: "messages",
    match: 23,
    hit: 8_421,
  };

  const params = serializeConversationUrlState(
    new URLSearchParams("device=laptop"),
    state,
  );
  assert.equal(params.get("q"), query);
  assert.equal(params.get("device"), "laptop");
  assert.deepEqual(parseConversationUrlState(params), state);
});

test("invalid and unknown managed values degrade safely", () => {
  assert.deepEqual(
    parseConversationUrlState(
      "line=-4&pos=1001&q=needle&scope=documents&match=0&hit=wat",
    ),
    {
      line: null,
      position: null,
      query: "needle",
      scope: null,
      match: null,
      hit: null,
    },
  );

  assert.deepEqual(
    parseConversationUrlState("line=12.5&pos=NaN&match=3"),
    EMPTY_CONVERSATION_URL_STATE,
  );
});

test("query length is capped by Unicode code points without splitting emoji", () => {
  const overlong = `${"🧭".repeat(MAX_CONVERSATION_QUERY_LENGTH)}suffix`;
  const parsed = parseConversationUrlState(
    new URLSearchParams({ q: overlong, scope: "messages" }),
  );
  assert.equal(Array.from(parsed.query).length, MAX_CONVERSATION_QUERY_LENGTH);
  assert.equal(parsed.query.endsWith("🧭"), true);
});

test("serialization preserves unrelated query parameters", () => {
  const params = serializeConversationUrlState(
    new URLSearchParams(
      "device=desktop&tab=activity&line=old&q=old&match=9&hit=9",
    ),
    {
      line: 77,
      position: 0,
      query: "new",
      scope: "messages",
      match: 2,
      hit: 77,
    },
  );

  assert.equal(
    params.toString(),
    "device=desktop&tab=activity&line=77&q=new&scope=messages&match=2&hit=77",
  );
});

test("clearing search removes only search parameters", () => {
  const withSearch = {
    line: 44,
    position: 250,
    query: "needle",
    scope: "messages",
    match: 4,
    hit: 44,
  };
  const params = serializeConversationUrlState(
    new URLSearchParams("device=mobile&q=stale"),
    clearConversationSearchState(withSearch),
  );

  assert.equal(params.get("device"), "mobile");
  assert.equal(params.get("line"), "44");
  assert.equal(params.get("pos"), "250");
  for (const key of ["q", "scope", "match", "hit"]) {
    assert.equal(params.has(key), false);
  }
});

test("history policy pushes explicit actions and replaces passive scrolling", () => {
  const current = { ...EMPTY_CONVERSATION_URL_STATE, line: 10 };
  const next = { ...current, line: 20, position: 400 };

  assert.equal(
    decideConversationHistoryMode("passive-scroll", current, next),
    "replace",
  );
  assert.equal(
    decideConversationHistoryMode("prompt-jump", current, next),
    "push",
  );
  assert.equal(
    decideConversationHistoryMode("search-next", current, next),
    "push",
  );
  assert.equal(
    decideConversationHistoryMode("popstate", current, next),
    "none",
  );
  assert.equal(
    decideConversationHistoryMode("search-select", current, current),
    "none",
  );
});
