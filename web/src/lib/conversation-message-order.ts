export interface ServerOrderedMessage {
  id: string | number;
  line_number: number;
}

export interface ServerMessageWindow<T extends ServerOrderedMessage> {
  offset: number;
  messages: T[];
}

export interface DetachedServerMessageWindow<T extends ServerOrderedMessage>
  extends ServerMessageWindow<T> {
  endOffset: number;
}

/**
 * Merge refreshed pages by the server's stable within-document identity.
 *
 * Database IDs can change after an authoritative reparse, timestamps can move
 * backwards, and source IDs may legitimately repeat. The normalized line is
 * the only identity shared by pagination, prompt jumps, and live refreshes.
 */
export function mergeMessagesChronologically<T extends ServerOrderedMessage>(
  current: T[],
  incoming: T[],
): T[] {
  const byLine = new Map(
    current.map((message) => [message.line_number, message]),
  );
  incoming.forEach((message) => byLine.set(message.line_number, message));
  return Array.from(byLine.values()).sort((left, right) => {
    const lineDifference = left.line_number - right.line_number;
    return lineDifference || String(left.id).localeCompare(String(right.id));
  });
}

/** Ensure a bounded around-window contains the requested server line. */
export function contextBeforeIncludingTarget(
  preferredContextBefore: number,
  limit: number,
): number {
  return Math.max(0, Math.min(preferredContextBefore, limit - 1));
}

/**
 * Keep a disjoint target window detached instead of advancing the contiguous
 * offset across unread rows.
 */
export function placeTargetWindow<T extends ServerOrderedMessage>(
  current: T[],
  contiguousEnd: number,
  incoming: ServerMessageWindow<T>,
): {
  messages: T[];
  contiguousEnd: number;
  detached: DetachedServerMessageWindow<T> | null;
} {
  const incomingEnd = incoming.offset + incoming.messages.length;
  if (incoming.offset <= contiguousEnd) {
    return {
      messages: mergeMessagesChronologically(current, incoming.messages),
      contiguousEnd: Math.max(contiguousEnd, incomingEnd),
      detached: null,
    };
  }
  return {
    messages: current,
    contiguousEnd,
    detached: {
      offset: incoming.offset,
      endOffset: incomingEnd,
      messages: incoming.messages,
    },
  };
}
