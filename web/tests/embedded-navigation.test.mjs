import assert from "node:assert/strict";
import test from "node:test";

import {
  embeddedDashboardPath,
  installEmbeddedNavigationReporter,
} from "../src/lib/embedded-navigation.mjs";

function fakeWindow() {
  const sent = [];
  const listeners = new Map();
  const location = { pathname: "/conversations/legacy-id", search: "?line=9", hash: "" };
  const parent = { postMessage: (payload) => sent.push(payload) };
  const history = {
    pushState(_state, _unused, url) {
      const next = new URL(url, "https://memento.test");
      location.pathname = next.pathname;
      location.search = next.search;
    },
    replaceState(_state, _unused, url) {
      const next = new URL(url, "https://memento.test");
      location.pathname = next.pathname;
      location.search = next.search;
    },
  };
  const browserWindow = {
    parent,
    location,
    history,
    addEventListener(type, listener) { listeners.set(type, listener); },
    removeEventListener(type, listener) {
      if (listeners.get(type) === listener) listeners.delete(type);
    },
  };
  return { browserWindow, sent, listeners, history };
}

test("embedded path includes the query but never leaks the fragment", () => {
  assert.equal(embeddedDashboardPath({
    pathname: "/conversations/cursor/native-id",
    search: "?line=9&pos=1000",
    hash: "#private-token",
  }), "/conversations/cursor/native-id?line=9&pos=1000");
});

test("reports canonical history replacement and subsequent navigation", () => {
  const { browserWindow, sent, listeners, history } = fakeWindow();
  const originalPush = history.pushState;
  const originalReplace = history.replaceState;
  const uninstall = installEmbeddedNavigationReporter(browserWindow);

  assert.deepEqual(sent.at(-1), {
    type: "memento:navigation",
    path: "/conversations/legacy-id?line=9",
  });

  history.replaceState({}, "", "/conversations/cursor/native-id?line=9");
  assert.deepEqual(sent.at(-1), {
    type: "memento:navigation",
    path: "/conversations/cursor/native-id?line=9",
  });

  history.pushState({}, "", "/conversations/cursor/native-id?line=21&pos=500");
  assert.equal(sent.at(-1).path, "/conversations/cursor/native-id?line=21&pos=500");

  browserWindow.location.pathname = "/app";
  browserWindow.location.search = "";
  listeners.get("popstate")();
  assert.equal(sent.at(-1).path, "/app");

  uninstall();
  assert.equal(history.pushState, originalPush);
  assert.equal(history.replaceState, originalReplace);
  assert.equal(listeners.size, 0);
});

test("top-level pages do not install or report", () => {
  const { browserWindow, sent, history } = fakeWindow();
  browserWindow.parent = browserWindow;
  const originalPush = history.pushState;
  const uninstall = installEmbeddedNavigationReporter(browserWindow);
  assert.equal(sent.length, 0);
  assert.equal(history.pushState, originalPush);
  uninstall();
});
