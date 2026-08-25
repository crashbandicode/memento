"use client";

export const COPY_OMIT_ROLE_PREFIX_KEY = "memento.copyOmitRolePrefix";

export function readCopyOmitRolePrefix(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(COPY_OMIT_ROLE_PREFIX_KEY) === "true";
  } catch {
    return false;
  }
}

export function writeCopyOmitRolePrefix(value: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(COPY_OMIT_ROLE_PREFIX_KEY, String(value));
  } catch {
    // The in-memory toggle remains usable when persistent storage is unavailable.
  }
}

/** Remove only the per-message role/timestamp headings from an exported thread. */
export function omitExportRolePrefixes(markdown: string): string {
  return markdown.replace(
    /^(?:## Prompt \d+ — You|### (?:Assistant|You|Parent agent|System|Tool|Developer|Your response))(?: · [^\r\n]+)?\r?\n\r?\n/gm,
    "",
  );
}
