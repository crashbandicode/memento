// @ts-check
/**
 * Pure request → response resolver for the hermetic Memento conversation mocks.
 *
 * This module has NO Playwright dependency on purpose: the exact same function
 * that decides what every intercepted `/api/**` request returns is also
 * exercised by `mock-router.test.mjs` under `node --test`. That gives the mock
 * layer real, browser-free test coverage even in environments where Chromium
 * cannot launch.
 *
 * `resolveConversationRoute` never reaches the network: unknown endpoints fall
 * back to empty JSON so a spec can never accidentally hit a live backend.
 */

import { FIXTURE_TOKEN, FIXTURE_USER } from "./conversation-scenarios.mjs";

/**
 * @typedef {(
 *   | { action: "abort" }
 *   | { action: "fulfill", status: number, json: unknown }
 * )} MockResult
 */

/** Extract just the pathname from a full or relative URL. */
export function pathnameOf(url) {
  try {
    return new URL(url, "http://mock.local").pathname;
  } catch {
    return url;
  }
}

/**
 * Resolve an intercepted API request to a deterministic response.
 *
 * @param {{ url: string, method?: string, scenario: any }} args
 * @returns {MockResult}
 */
export function resolveConversationRoute({ url, method = "GET", scenario }) {
  const pathname = pathnameOf(url);
  const upperMethod = method.toUpperCase();

  // The SSE stream is intentionally inert — reproduces a metadata-only ingest
  // with no live event replay. Aborting fires the hook's benign reconnect path.
  if (pathname.endsWith("/api/events/stream")) {
    return { action: "abort" };
  }
  if (pathname.endsWith("/api/events/session")) {
    return { action: "fulfill", status: 200, json: { ok: true } };
  }

  // --- Auth / shell bootstrap -------------------------------------------------
  if (pathname.endsWith("/api/auth/me")) {
    return { action: "fulfill", status: 200, json: FIXTURE_USER };
  }
  if (pathname.endsWith("/api/auth/refresh")) {
    return {
      action: "fulfill",
      status: 200,
      json: {
        access_token: FIXTURE_TOKEN,
        token_type: "bearer",
        user_id: FIXTURE_USER.id,
        role: FIXTURE_USER.role,
      },
    };
  }
  if (pathname.endsWith("/api/auth/registration-mode")) {
    return {
      action: "fulfill",
      status: 200,
      json: { mode: "closed", has_any_user: true, github_enabled: false },
    };
  }
  if (pathname.endsWith("/api/devices")) {
    return { action: "fulfill", status: 200, json: [] };
  }

  // --- Conversation endpoints (order: most specific first) --------------------
  if (/\/api\/conversations\/[^/]+\/prompts$/.test(pathname)) {
    return {
      action: "fulfill",
      status: 200,
      json: { prompts: scenario.prompts ?? [] },
    };
  }
  if (/\/api\/conversations\/[^/]+\/pending-interactions$/.test(pathname)) {
    return {
      action: "fulfill",
      status: 200,
      json: scenario.pending ?? { count: 0, interactions: [], inferred_responses: [] },
    };
  }
  if (/\/api\/conversations\/[^/]+\/latest-agent-message$/.test(pathname)) {
    return {
      action: "fulfill",
      status: 200,
      json: { line_number: scenario.latestAgentLine ?? null },
    };
  }
  if (/\/api\/conversations\/[^/]+\/messages$/.test(pathname)) {
    const messages = scenario.messages ?? [];
    return {
      action: "fulfill",
      status: 200,
      json: { total: messages.length, offset: 0, limit: 50, messages },
    };
  }
  if (/\/api\/conversations\/[^/]+\/search$/.test(pathname)) {
    return {
      action: "fulfill",
      status: 200,
      json: {
        query: "",
        results: [],
        next_after_line: null,
        has_more: false,
        corrected_query: null,
      },
    };
  }
  if (/\/api\/conversations\/[^/]+$/.test(pathname)) {
    return { action: "fulfill", status: 200, json: scenario.meta };
  }

  // --- Safe default: never hit a real backend --------------------------------
  // GET collections default to an empty array; everything else to empty object.
  if (upperMethod === "GET" && /\/(files|projects|tools|daily)$/.test(pathname)) {
    return { action: "fulfill", status: 200, json: [] };
  }
  return { action: "fulfill", status: 200, json: {} };
}
