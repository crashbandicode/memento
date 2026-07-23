/** Persist recent search queries in localStorage. */

const MAX_SEARCH_HISTORY = 12;

export function readSearchHistory(storageKey: string): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(storageKey);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((item): item is string => typeof item === "string")
      .map((item) => item.trim())
      .filter(Boolean)
      .slice(0, MAX_SEARCH_HISTORY);
  } catch {
    return [];
  }
}

export function pushSearchHistory(storageKey: string, query: string): string[] {
  const clean = query.trim();
  if (!clean) return readSearchHistory(storageKey);
  const next = [
    clean,
    ...readSearchHistory(storageKey).filter((item) => item.toLocaleLowerCase() !== clean.toLocaleLowerCase()),
  ].slice(0, MAX_SEARCH_HISTORY);
  try {
    window.localStorage.setItem(storageKey, JSON.stringify(next));
  } catch {
    // Ignore quota / private-mode failures.
  }
  return next;
}

export const CONVERSATION_SEARCH_HISTORY_KEY = "memento.conversationSearchHistory";
export const PROMPT_NAVIGATOR_SEARCH_HISTORY_KEY = "memento.promptNavigatorSearchHistory";
