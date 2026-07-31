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

interface TaskSnapshotTask {
  id: string;
  content: string;
  status: string;
  active_form?: string;
}

interface TaskSnapshot {
  source: string;
  is_current?: boolean;
  total_count: number;
  tasks: TaskSnapshotTask[];
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

/**
 * The mutable Cursor task snapshot is intentionally transported as message
 * line 1 so the server can project active task state. Once that same state is
 * rendered in the pinned task card, rendering its carrier row again produces
 * a misleading historical "Task update" duplicate.
 */
export function isMirroredActiveTaskMessage(
  message: { task_state?: TaskSnapshot | null },
  activeTaskState?: TaskSnapshot | null,
): boolean {
  const messageState = message.task_state;
  if (
    !messageState?.is_current
    || !activeTaskState
    || messageState.source !== activeTaskState.source
    || messageState.total_count !== activeTaskState.total_count
    || messageState.tasks.length !== activeTaskState.tasks.length
  ) {
    return false;
  }
  return messageState.tasks.every((task, index) => {
    const activeTask = activeTaskState.tasks[index];
    return Boolean(
      activeTask
      && task.id === activeTask.id
      && task.content === activeTask.content
      && task.status === activeTask.status
      && (task.active_form || "") === (activeTask.active_form || ""),
    );
  });
}
