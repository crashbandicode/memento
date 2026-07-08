export interface IdentifiedConversationSession {
  logical_session_id?: string | null;
  conversation_id: string;
  session_id: string;
}

/**
 * Merge paginated session responses without duplicating a logical thread when
 * pages overlap or several machines upload the same conversation. The
 * session UUID is stable across hosts; the document UUID is not.
 */
export function mergeConversationSessions<T extends IdentifiedConversationSession>(
  current: readonly T[],
  incoming: readonly T[],
): T[] {
  const merged = new Map<string, T>();

  for (const session of current) {
    merged.set(conversationSessionKey(session), session);
  }
  for (const session of incoming) {
    merged.set(conversationSessionKey(session), session);
  }

  return [...merged.values()];
}

export function conversationSessionKey(
  session: IdentifiedConversationSession,
): string {
  return (
    session.logical_session_id
    || session.session_id
    || session.conversation_id
  );
}
