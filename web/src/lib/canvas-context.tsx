"use client";

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  type ReactNode,
} from "react";
import { normalizeCanvasTarget, type CanvasArtifact } from "./canvas-artifact.mjs";

type CanvasResolver = (key: string) => CanvasArtifact | undefined;

const CanvasArtifactContext = createContext<CanvasResolver>(() => undefined);

function key(value: string | null | undefined): string {
  return normalizeCanvasTarget(String(value ?? "")).toLowerCase();
}

/**
 * Provides server-validated {@link CanvasArtifact} descriptors to the canvas
 * chips rendered (deep) inside a single message's Markdown. A chip looks itself
 * up by path/href/name; when no descriptor is found it falls back to a
 * link-only, source-unavailable view. The map is memoized per message.
 */
export function CanvasArtifactProvider({
  artifacts,
  children,
}: {
  artifacts?: CanvasArtifact[] | null;
  children: ReactNode;
}) {
  const map = useMemo(() => {
    const next = new Map<string, CanvasArtifact>();
    for (const artifact of artifacts ?? []) {
      if (!artifact) continue;
      for (const candidate of [artifact.path, artifact.href, artifact.name]) {
        const normalized = key(candidate);
        if (normalized && !next.has(normalized)) next.set(normalized, artifact);
      }
    }
    return next;
  }, [artifacts]);

  const resolver = useCallback<CanvasResolver>(
    (lookup: string) => map.get(key(lookup)),
    [map],
  );

  return (
    <CanvasArtifactContext.Provider value={resolver}>
      {children}
    </CanvasArtifactContext.Provider>
  );
}

/** Resolve a canvas link (by path/href/name) to a server-validated descriptor. */
export function useCanvasArtifactResolver(): CanvasResolver {
  return useContext(CanvasArtifactContext);
}
