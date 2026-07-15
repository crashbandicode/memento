const TOKEN_KEY = "dr_token";
const REMEMBER_KEY = "dr_remember_me";

function browserStorage(): Pick<Window, "localStorage" | "sessionStorage"> | null {
  if (typeof window === "undefined") return null;
  return window;
}

function read(storage: Storage, key: string): string | null {
  try {
    return storage.getItem(key);
  } catch {
    return null;
  }
}

function remove(storage: Storage, key: string) {
  try {
    storage.removeItem(key);
  } catch {
    // Storage may be disabled by the browser; auth will remain in memory.
  }
}

/** Checked by default, including upgrades from builds that predate the toggle. */
export function getRememberMePreference(): boolean {
  const storage = browserStorage();
  if (!storage) return true;
  return read(storage.localStorage, REMEMBER_KEY) !== "0";
}

export function setRememberMePreference(remember: boolean) {
  const storage = browserStorage();
  if (!storage) return;
  try {
    storage.localStorage.setItem(REMEMBER_KEY, remember ? "1" : "0");
  } catch {
    // A blocked preference store should not prevent login.
  }
}

/** Read either a durable remembered session or a tab-lifetime session. */
export function getStoredAuthToken(): string | null {
  const storage = browserStorage();
  if (!storage) return null;
  return (
    read(storage.localStorage, TOKEN_KEY)
    || read(storage.sessionStorage, TOKEN_KEY)
  );
}

export function clearStoredAuthToken() {
  const storage = browserStorage();
  if (!storage) return;
  remove(storage.localStorage, TOKEN_KEY);
  remove(storage.sessionStorage, TOKEN_KEY);
}

/**
 * Persist a JWT in exactly one storage tier. Returns the tier actually used so
 * callers can remain functional when a privacy mode blocks localStorage.
 */
export function storeAuthToken(token: string, remember = true): "local" | "session" | "memory" {
  const storage = browserStorage();
  if (!storage) return "memory";
  clearStoredAuthToken();
  setRememberMePreference(remember);

  const preferred = remember ? storage.localStorage : storage.sessionStorage;
  try {
    preferred.setItem(TOKEN_KEY, token);
    return remember ? "local" : "session";
  } catch {
    // A remembered login may safely degrade to tab scope. An explicitly
    // session-only login must never broaden itself to durable local storage.
    if (!remember) return "memory";
    try {
      storage.sessionStorage.setItem(TOKEN_KEY, token);
      return "session";
    } catch {
      return "memory";
    }
  }
}

/** Replace a refreshed JWT without silently changing its persistence scope. */
export function replaceStoredAuthToken(token: string) {
  const storage = browserStorage();
  if (!storage) return;
  const remembered = Boolean(read(storage.localStorage, TOKEN_KEY));
  const sessionOnly = !remembered && Boolean(read(storage.sessionStorage, TOKEN_KEY));
  const useLocal = remembered || (!sessionOnly && getRememberMePreference());
  clearStoredAuthToken();
  try {
    (useLocal ? storage.localStorage : storage.sessionStorage).setItem(TOKEN_KEY, token);
  } catch {
    if (!useLocal) return;
    try {
      storage.sessionStorage.setItem(TOKEN_KEY, token);
    } catch {
      // AuthProvider still holds the refreshed token in memory.
    }
  }
}
