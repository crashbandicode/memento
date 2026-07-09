import assert from "node:assert/strict";
import test from "node:test";

import {
  createAppRouteHistory,
  getBackFallback,
  recordAppRoute,
} from "../src/lib/app-navigation.ts";

test("detail routes have deterministic parents", () => {
  const cases = [
    ["/conversations/conversation-id", "/app"],
    ["/documents/document-id", "/app"],
    ["/profile", "/app"],
    ["/daily/2026-07-09", "/daily"],
    ["/tools/codex", "/tools"],
    ["/projects/project-id", "/projects"],
    ["/projects/project-id/conversations", "/projects/project-id"],
    ["/projects/project-id/timeline", "/projects/project-id"],
    ["/devices/device-id/tools/codex", "/devices"],
    [
      "/devices/device-id/tools/codex/projects/project-id",
      "/devices/device-id/tools/codex",
    ],
  ];

  for (const [pathname, expected] of cases) {
    assert.equal(getBackFallback(pathname), expected, pathname);
  }
});

test("top-level hubs do not render a redundant Back control", () => {
  for (const pathname of [
    "/app",
    "/projects",
    "/tools",
    "/daily",
    "/search",
    "/devices",
    "/memory",
    "/inbox",
    "/admin",
  ]) {
    assert.equal(getBackFallback(pathname), null, pathname);
  }
});

test("route history preserves a trusted Forward entry after Back", () => {
  let history = createAppRouteHistory("/app");
  history = recordAppRoute(history, "/search");
  history = recordAppRoute(history, "/conversations/example");
  assert.deepEqual(history, {
    entries: ["/app", "/search", "/conversations/example"],
    index: 2,
  });

  history = recordAppRoute(history, "/search", "back");
  assert.equal(history.index, 1);
  assert.equal(history.entries[history.index + 1], "/conversations/example");

  history = recordAppRoute(history, "/conversations/example", "forward");
  assert.equal(history.index, 2);
});

test("new navigation after Back clears the stale Forward branch", () => {
  let history = createAppRouteHistory("/app");
  history = recordAppRoute(history, "/search");
  history = recordAppRoute(history, "/conversations/example");
  history = recordAppRoute(history, "/search", "back");
  history = recordAppRoute(history, "/projects");

  assert.deepEqual(history, {
    entries: ["/app", "/search", "/projects"],
    index: 2,
  });
});

test("an ambiguous unmarked pop resets instead of choosing the wrong direction", () => {
  const history = {
    entries: ["/a", "/duplicate", "/c", "/duplicate"],
    index: 2,
  };

  assert.deepEqual(recordAppRoute(history, "/duplicate", "pop"), {
    entries: ["/duplicate"],
    index: 0,
  });
});

test("a direct-load fallback replaces the untrusted timeline", () => {
  const history = {
    entries: ["/conversations/example", "/search"],
    index: 1,
  };

  assert.deepEqual(recordAppRoute(history, "/app", "replace"), {
    entries: ["/app"],
    index: 0,
  });
});

test("route history remains bounded", () => {
  let history = createAppRouteHistory("/app");

  for (let index = 0; index < 60; index += 1) {
    history = recordAppRoute(history, `/projects/${index}`);
  }
  assert.equal(history.entries.length, 50);
  assert.equal(history.index, 49);
  assert.equal(history.entries.at(-1), "/projects/59");
});
