const APP_HOME = "/app";

/**
 * Return a deterministic parent for pages that have meaningful detail
 * hierarchy. Top-level hubs intentionally return null: the sidebar is their
 * primary navigation and showing a Back control there would imply a parent
 * that does not exist.
 */
export function getBackFallback(pathname: string): string | null {
  const segments = pathname.split("/").filter(Boolean);
  const [section, id, subsection, subsectionId, leaf] = segments;

  if (section === "conversations" && id) return APP_HOME;
  if (section === "documents" && id) return APP_HOME;
  if (section === "profile" && segments.length === 1) return APP_HOME;

  if (section === "daily" && id) return "/daily";
  if (section === "tools" && id) return "/tools";

  if (section === "projects" && id) {
    if (subsection === "conversations" || subsection === "timeline") {
      return `/projects/${id}`;
    }
    return "/projects";
  }

  if (section === "devices" && id && subsection === "tools" && subsectionId) {
    if (leaf === "projects" && segments[5]) {
      return `/devices/${id}/tools/${subsectionId}`;
    }
    return "/devices";
  }

  return null;
}

/**
 * Maintain a small in-memory timeline of routes observed by the mounted app
 * shell. Explicit and browser pop navigations move its cursor; a new push
 * truncates any stale Forward branch. This gives the header a trustworthy
 * signal without relying on window.history.length, which can include pages
 * from another origin.
 */
export interface AppRouteHistory {
  entries: string[];
  index: number;
}

export type AppRouteNavigation = "push" | "back" | "forward" | "pop" | "replace";

export function createAppRouteHistory(pathname: string): AppRouteHistory {
  return { entries: pathname ? [pathname] : [], index: pathname ? 0 : -1 };
}

export function recordAppRoute(
  history: AppRouteHistory,
  pathname: string,
  navigation: AppRouteNavigation = "push",
): AppRouteHistory {
  if (!pathname) return history;
  if (history.index < 0 || history.entries.length === 0) {
    return createAppRouteHistory(pathname);
  }
  if (history.entries[history.index] === pathname) return history;

  if (navigation === "replace") return createAppRouteHistory(pathname);

  if (navigation !== "push") {
    const previousIndex = findPrevious(history.entries, pathname, history.index);
    const nextIndex = history.entries.indexOf(pathname, history.index + 1);

    if (navigation === "back" && previousIndex >= 0) {
      return { ...history, index: previousIndex };
    }
    if (navigation === "forward" && nextIndex >= 0) {
      return { ...history, index: nextIndex };
    }
    if (navigation === "pop") {
      // Without an entry marker, duplicate adjacent paths are directionally
      // ambiguous. Resetting is safer than exposing the wrong Back/Forward.
      if (previousIndex === history.index - 1 && nextIndex === history.index + 1) {
        return createAppRouteHistory(pathname);
      }
      if (previousIndex === history.index - 1) return { ...history, index: previousIndex };
      if (nextIndex === history.index + 1) return { ...history, index: nextIndex };
      if (previousIndex >= 0) return { ...history, index: previousIndex };
      if (nextIndex >= 0) return { ...history, index: nextIndex };
    }

    // The browser reached an app route from before this shell was mounted.
    // Start a new trusted history rather than guessing about external entries.
    return createAppRouteHistory(pathname);
  }

  const entries = [...history.entries.slice(0, history.index + 1), pathname].slice(-50);
  return { entries, index: entries.length - 1 };
}

function findPrevious(entries: readonly string[], pathname: string, index: number): number {
  for (let candidate = index - 1; candidate >= 0; candidate -= 1) {
    if (entries[candidate] === pathname) return candidate;
  }
  return -1;
}
