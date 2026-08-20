/**
 * Keep the native desktop shell's address strip synchronized with the route
 * inside its cross-origin dashboard iframe.
 *
 * Next route transitions, canonical URL replacements, prompt/search anchors,
 * Back/Forward, and hash navigation do not all flow through `usePathname()`.
 * The History API also emits no browser event of its own, so the embedded app
 * must observe those two methods explicitly and report the resulting URL.
 */

export function embeddedDashboardPath(location) {
  return `${location.pathname || "/"}${location.search || ""}`;
}

export function installEmbeddedNavigationReporter(browserWindow) {
  if (!browserWindow || browserWindow.parent === browserWindow) return () => {};

  const report = () => {
    browserWindow.parent.postMessage({
      type: "memento:navigation",
      path: embeddedDashboardPath(browserWindow.location),
    }, "*");
  };
  const history = browserWindow.history;
  const originalPushState = history.pushState;
  const originalReplaceState = history.replaceState;
  const wrappedPushState = function (...args) {
    const result = originalPushState.apply(this, args);
    report();
    return result;
  };
  const wrappedReplaceState = function (...args) {
    const result = originalReplaceState.apply(this, args);
    report();
    return result;
  };

  history.pushState = wrappedPushState;
  history.replaceState = wrappedReplaceState;
  browserWindow.addEventListener("popstate", report);
  browserWindow.addEventListener("hashchange", report);
  report();

  return () => {
    browserWindow.removeEventListener("popstate", report);
    browserWindow.removeEventListener("hashchange", report);
    if (history.pushState === wrappedPushState) history.pushState = originalPushState;
    if (history.replaceState === wrappedReplaceState) history.replaceState = originalReplaceState;
  };
}
