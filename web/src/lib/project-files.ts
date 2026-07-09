export interface ProjectFileIdentity {
  id: string;
}

/**
 * Append a page of project files without changing the order of files that are
 * already visible. Offset pagination can overlap when rows are added while a
 * user is browsing, so identity-based de-duplication keeps the list stable.
 */
export function mergeProjectFiles<T extends ProjectFileIdentity>(
  current: readonly T[],
  incoming: readonly T[],
): T[] {
  const seen = new Set(current.map((file) => file.id));
  const merged = [...current];

  for (const file of incoming) {
    if (seen.has(file.id)) continue;
    seen.add(file.id);
    merged.push(file);
  }

  return merged;
}
