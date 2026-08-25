/**
 * Stable, shareable navigation state for a single conversation.
 *
 * Schema:
 *   line=<positive server line>       stable message anchor (legacy compatible)
 *   pos=<0..1000>                     position within that message, in thousandths
 *   q=<committed query>               at most 256 Unicode code points
 *   scope=messages                    reserved search scope
 *   match=<positive 1-based ordinal>  selected result in chronological order
 *   hit=<positive server line>        stable identity for the selected result
 *
 * Unknown parameters are deliberately preserved by the serializer.
 */

export const MAX_CONVERSATION_QUERY_LENGTH = 256;
export const MAX_CONVERSATION_LINE = 2_147_483_647;
export const MAX_CONVERSATION_MATCH = 100_000;
export const CONVERSATION_POSITION_SCALE = 1_000;

export type ConversationSearchScope = "messages";

export interface ConversationUrlState {
  line: number | null;
  position: number | null;
  query: string;
  scope: ConversationSearchScope | null;
  match: number | null;
  hit: number | null;
}

export type ConversationHistoryReason =
  | "initial"
  | "popstate"
  | "passive-scroll"
  | "search-submit"
  | "search-clear"
  | "search-select"
  | "search-next"
  | "search-previous"
  | "prompt-jump"
  | "latest-agent"
  | "pending-interaction"
  | "pinned-message"
  | "normalize";

export type ConversationHistoryMode = "none" | "push" | "replace";

export const EMPTY_CONVERSATION_URL_STATE: ConversationUrlState = Object.freeze({
  line: null,
  position: null,
  query: "",
  scope: null,
  match: null,
  hit: null,
});

const MANAGED_PARAMS = ["line", "pos", "q", "scope", "match", "hit"] as const;

function positiveInteger(
  value: string | null,
  maximum: number,
): number | null {
  if (!value || !/^[1-9]\d*$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed <= maximum ? parsed : null;
}

function boundedInteger(
  value: string | null,
  minimum: number,
  maximum: number,
): number | null {
  if (value === null || !/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= minimum && parsed <= maximum
    ? parsed
    : null;
}

export function sanitizeConversationQuery(value: string): string {
  return Array.from(value.trim())
    .slice(0, MAX_CONVERSATION_QUERY_LENGTH)
    .join("");
}

export function normalizeConversationPosition(value: number | null | undefined): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return Math.max(0, Math.min(CONVERSATION_POSITION_SCALE, Math.round(value)));
}

function toSearchParams(
  input: URLSearchParams | URL | string,
): URLSearchParams {
  if (input instanceof URLSearchParams) return input;
  if (input instanceof URL) return input.searchParams;
  const queryIndex = input.indexOf("?");
  return new URLSearchParams(queryIndex >= 0 ? input.slice(queryIndex + 1) : input);
}

export function parseConversationUrlState(
  input: URLSearchParams | URL | string,
): ConversationUrlState {
  const params = toSearchParams(input);
  const query = sanitizeConversationQuery(params.get("q") || "");
  const requestedScope = params.get("scope");
  const scope: ConversationSearchScope | null = query
    ? requestedScope === null || requestedScope === "messages"
      ? "messages"
      : null
    : null;

  return {
    line: positiveInteger(params.get("line"), MAX_CONVERSATION_LINE),
    position: boundedInteger(
      params.get("pos"),
      0,
      CONVERSATION_POSITION_SCALE,
    ),
    query,
    scope,
    match: query && scope
      ? positiveInteger(params.get("match"), MAX_CONVERSATION_MATCH)
      : null,
    hit: query && scope
      ? positiveInteger(params.get("hit"), MAX_CONVERSATION_LINE)
      : null,
  };
}

export function serializeConversationUrlState(
  current: URLSearchParams,
  state: ConversationUrlState,
): URLSearchParams {
  const next = new URLSearchParams(current);
  MANAGED_PARAMS.forEach((key) => next.delete(key));

  const line = state.line === null
    ? null
    : positiveInteger(String(state.line), MAX_CONVERSATION_LINE);
  if (line !== null) next.set("line", String(line));
  const position = normalizeConversationPosition(state.position);
  if (line !== null && position !== null && position > 0) {
    next.set("pos", String(position));
  }

  const query = sanitizeConversationQuery(state.query);
  if (query) {
    next.set("q", query);
    next.set("scope", "messages");
    const match = state.match === null
      ? null
      : positiveInteger(String(state.match), MAX_CONVERSATION_MATCH);
    const hit = state.hit === null
      ? null
      : positiveInteger(String(state.hit), MAX_CONVERSATION_LINE);
    if (match !== null) next.set("match", String(match));
    if (hit !== null) next.set("hit", String(hit));
  }
  return next;
}

export function clearConversationSearchState(
  state: ConversationUrlState,
): ConversationUrlState {
  return {
    ...state,
    query: "",
    scope: null,
    match: null,
    hit: null,
  };
}

export function conversationUrlStatesEqual(
  left: ConversationUrlState,
  right: ConversationUrlState,
): boolean {
  return left.line === right.line
    && left.position === right.position
    && left.query === right.query
    && left.scope === right.scope
    && left.match === right.match
    && left.hit === right.hit;
}

/**
 * Explicit user navigation gets a Back/Forward entry. Passive anchor tracking
 * only replaces the current entry, while restores never write history.
 */
export function decideConversationHistoryMode(
  reason: ConversationHistoryReason,
  current: ConversationUrlState,
  next: ConversationUrlState,
): ConversationHistoryMode {
  if (reason === "initial" || reason === "popstate") return "none";
  if (conversationUrlStatesEqual(current, next)) return "none";
  if (reason === "passive-scroll" || reason === "normalize") return "replace";
  return "push";
}
