// @ts-check
/**
 * Deterministic, self-contained fixture scenarios for the Memento conversation
 * viewer regression suite (Workstream D).
 *
 * These objects mirror the API/SSE contract declared in
 * `web/src/lib/api-client.ts` (ConversationMeta, ConversationMessage,
 * QuestionInteraction, ConversationAgentEvent, MessagesResponse, etc.). They are
 * intentionally NOT sourced from live production data: every scenario is a fixed
 * snapshot so the Playwright specs are hermetic and reproducible.
 *
 * Each scenario has the shape:
 *   {
 *     docId: string,                              // conversation document id
 *     meta: ConversationMeta,                     // GET /api/conversations/:id
 *     messages: ConversationMessage[],            // GET /api/conversations/:id/messages
 *     prompts: ConversationPrompt[],              // GET /api/conversations/:id/prompts
 *     pending: PendingConversationInteractionsResponse, // .../pending-interactions
 *     latestAgentLine: number | null,            // .../latest-agent-message
 *   }
 *
 * @typedef {import("../../src/lib/api-client").ConversationMeta} ConversationMeta
 * @typedef {import("../../src/lib/api-client").ConversationMessage} ConversationMessage
 * @typedef {import("../../src/lib/api-client").ConversationPrompt} ConversationPrompt
 * @typedef {import("../../src/lib/api-client").QuestionInteraction} QuestionInteraction
 * @typedef {import("../../src/lib/api-client").PendingConversationInteractionsResponse} PendingConversationInteractionsResponse
 */

/** Fixed clock so timestamps never drift between runs. */
const T0 = "2026-07-31T12:00:00.000Z";
const T1 = "2026-07-31T12:00:05.000Z";
const T2 = "2026-07-31T12:00:10.000Z";
const T3 = "2026-07-31T12:00:15.000Z";

/** Shared authenticated user returned by GET /api/auth/me. */
export const FIXTURE_USER = {
  id: "user-fixture-1",
  email: "regression@memento.test",
  name: "Regression Harness",
  role: "admin",
  status: "active",
  collector_token: null,
  totp_enabled: false,
};

/** Bearer token seeded into localStorage before the app boots. */
export const FIXTURE_TOKEN = "fixture-jwt-token";

const EMPTY_PENDING = { count: 0, interactions: [], inferred_responses: [] };

/**
 * Regression #1 + #3 — wrapper unwrap.
 *
 * A Claude `PermissionRequest(AskUserQuestion)` tool call. The raw tool `input`
 * carries the permission envelope (a bare "Yes" suggestion + JSON). The server
 * is expected to UNWRAP it into `interaction.questions[0]`, the real human
 * question with real options. The viewer must render exactly ONE question card
 * showing the real prompt/options — never the raw envelope ("Yes"/JSON dump)
 * and never a duplicate wrapper + question pair.
 */
export const permissionWrappedQuestion = {
  docId: "conv-permission-wrap",
  meta: /** @type {ConversationMeta} */ ({
    id: "conv-permission-wrap",
    tool_id: "claude_code",
    title: "Approve database choice",
    relative_path: "claude/sessions/approve-db.jsonl",
    metadata: {},
    message_count: 2,
    subagent_count: 0,
    pending_question_count: 1,
    synced_at: T2,
    activity_at: T2,
  }),
  messages: /** @type {ConversationMessage[]} */ ([
    {
      id: 1,
      line_number: 1,
      role: "user",
      content: "Set up the database layer for the new service.",
      timestamp: T0,
    },
    {
      id: 2,
      line_number: 2,
      role: "assistant",
      model: "claude-opus-4-1",
      content: "",
      tool_calls: [
        {
          name: "AskUserQuestion",
          // The raw permission envelope. If the unwrap regressed, THIS text
          // (sentinel included) would leak into the DOM as a raw tool dump.
          input: JSON.stringify({
            tool_name: "AskUserQuestion",
            permission_suggestion: "Yes",
            sentinel: "RAW_PERMISSION_ENVELOPE_SHOULD_NOT_RENDER",
            raw: { approve: true },
          }),
          interaction: /** @type {QuestionInteraction} */ ({
            kind: "question",
            id: "int-approve-db",
            source: "claude_code",
            tool_name: "AskUserQuestion",
            interaction_type: "permission_request",
            requested_tool: "AskUserQuestion",
            questions: [
              {
                id: "db",
                header: "DATABASE",
                prompt: "Which database should power the new service?",
                type: "single_select",
                allow_custom: false,
                options: [
                  { id: "postgres", label: "PostgreSQL" },
                  { id: "mysql", label: "MySQL" },
                ],
              },
            ],
          }),
        },
      ],
      timestamp: T1,
    },
  ]),
  prompts: [
    { id: 1, line_number: 1, content: "Set up the database layer for the new service.", timestamp: T0 },
  ],
  pending: EMPTY_PENDING,
  latestAgentLine: 2,
};

/**
 * A normalized historical tool row can own both its question and its response.
 * The viewer must not add a second DOM navigation anchor for the same line.
 */
export const sameRowQuestionResponse = {
  docId: "conv-same-row-question-response",
  meta: /** @type {ConversationMeta} */ ({
    id: "conv-same-row-question-response",
    tool_id: "cursor",
    title: "Cancelled question",
    relative_path: "cursor/sessions/cancelled-question.jsonl",
    metadata: {},
    message_count: 2,
    subagent_count: 0,
    pending_question_count: 0,
    synced_at: T2,
    activity_at: T2,
  }),
  messages: /** @type {ConversationMessage[]} */ ([
    {
      id: 1,
      line_number: 1,
      role: "user",
      content: "Ask me which database to use.",
      timestamp: T0,
    },
    {
      id: 2,
      line_number: 2,
      role: "tool",
      content: "Status: cancelled\n\n{}",
      interaction: /** @type {QuestionInteraction} */ ({
        kind: "question",
        id: "int-cancelled-db",
        source: "cursor",
        tool_name: "ask_question",
        questions: [
          {
            id: "db",
            header: "DATABASE",
            prompt: "Which database should power the service?",
            type: "single_select",
            allow_custom: false,
            options: [
              { id: "postgres", label: "PostgreSQL" },
              { id: "mysql", label: "MySQL" },
            ],
          },
        ],
      }),
      interaction_response: {
        kind: "question_response",
        interaction_id: "int-cancelled-db",
        status: "cancelled",
        answers: [],
        raw_text: "{}",
      },
      timestamp: T1,
    },
  ]),
  prompts: [
    { id: 1, line_number: 1, content: "Ask me which database to use.", timestamp: T0 },
  ],
  pending: EMPTY_PENDING,
  latestAgentLine: 2,
};

/**
 * Regression #2 — missing live prompt on metadata-only ingest (no SSE).
 *
 * The prompt outline is delivered purely by GET .../prompts. The SSE stream is
 * inert (aborted by the mock), reproducing a metadata-only ingest with no live
 * event replay. The prompt navigator must still list the prompts from the
 * initial fetch alone.
 */
export const metadataOnlyPrompts = {
  docId: "conv-metadata-only",
  meta: /** @type {ConversationMeta} */ ({
    id: "conv-metadata-only",
    tool_id: "claude_code",
    title: "Flaky checkout investigation",
    relative_path: "claude/sessions/flaky-checkout.jsonl",
    metadata: {},
    message_count: 2,
    subagent_count: 0,
    synced_at: T2,
    activity_at: T2,
  }),
  messages: /** @type {ConversationMessage[]} */ ([
    {
      id: 1,
      line_number: 1,
      role: "user",
      content: "Investigate the flaky checkout test and find the root cause.",
      timestamp: T0,
    },
    {
      id: 2,
      line_number: 2,
      role: "user",
      content: "Also check the retry backoff configuration.",
      timestamp: T1,
    },
  ]),
  prompts: [
    { id: 1, line_number: 1, content: "Investigate the flaky checkout test and find the root cause.", timestamp: T0 },
    { id: 2, line_number: 2, content: "Also check the retry backoff configuration.", timestamp: T1 },
  ],
  pending: EMPTY_PENDING,
  latestAgentLine: null,
};

/**
 * Regression #4 — subagent shown "complete" while still running.
 *
 * The subagent metadata reports `status: "running"` and a launch (`started`)
 * agent event is present, but there is NO completion for it. A live snapshot
 * still lists the agent as running. The UI must surface "Running", never
 * "Completed", for this subagent (both in the SubagentBadge panel and the
 * inline agent snapshot card).
 */
export const runningSubagent = {
  docId: "conv-running-subagent",
  meta: /** @type {ConversationMeta} */ ({
    id: "conv-running-subagent",
    tool_id: "claude_code",
    title: "Harden ingest pipeline",
    relative_path: "claude/sessions/harden-ingest.jsonl",
    metadata: {},
    message_count: 3,
    subagent_count: 1,
    is_subagent_orphan: false,
    subagents: [
      {
        id: "child-ingest-1",
        session_id: "child-ingest-1",
        agent_tool_use_id: "toolu_child_ingest_1",
        title: "Harden ingest pipeline",
        agent_nickname: "brave-otter",
        agent_path: "root/harden-ingest",
        agent_depth: 1,
        status: "running",
        document_ready: true,
        model: "claude-opus-4-1",
        reasoning_effort: "high",
        started_at: T1,
        completed_at: null,
        last_event_at: T2,
      },
    ],
    synced_at: T3,
    activity_at: T3,
  }),
  messages: /** @type {ConversationMessage[]} */ ([
    {
      id: 1,
      line_number: 1,
      role: "user",
      content: "Launch a subagent to harden the ingest pipeline.",
      timestamp: T0,
    },
    {
      id: 2,
      line_number: 2,
      role: "tool",
      content: "[Task]",
      // The launch/dispatch event for the subagent.
      agent_event: {
        version: 1,
        kind: "started",
        status: "async_launched",
        activity_type: "subagent",
        label: "Harden ingest pipeline",
        agent_path: "root/harden-ingest",
        agent_tool_use_id: "toolu_child_ingest_1",
        model: "claude-opus-4-1",
        reasoning_effort: "high",
        started_at: T1,
        result_summary: "INTERNAL_ASYNC_LAUNCH_METADATA_MUST_NOT_RENDER",
      },
      timestamp: T1,
    },
    {
      id: 3,
      line_number: 3,
      role: "tool",
      content: "[Task]",
      // A later snapshot: the agent is STILL running (no completion arrived).
      agent_event: {
        version: 1,
        kind: "snapshot",
        activity_type: "subagent",
        agents: [
          {
            agent_path: "root/harden-ingest",
            label: "Harden ingest pipeline",
            status: "running",
          },
        ],
      },
      timestamp: T2,
    },
  ]),
  prompts: [
    { id: 1, line_number: 1, content: "Launch a subagent to harden the ingest pipeline.", timestamp: T0 },
  ],
  pending: EMPTY_PENDING,
  latestAgentLine: 3,
};

/**
 * Production regression distilled from dreamland-yoga at 2026-08-01 09:42.
 * Two real agents have distinct source identities and descriptions; the stale
 * threshold launch/result pair has already coalesced into one lifecycle card.
 */
export const dreamlandParallelSubagents = {
  docId: "conv-dreamland-parallel-subagents",
  meta: /** @type {ConversationMeta} */ ({
    id: "conv-dreamland-parallel-subagents",
    tool_id: "claude_code",
    title: "Compare bjobs active jobs with event log collections",
    relative_path: "projects/lsf/c2badf82.jsonl",
    metadata: { session_id: "c2badf82-0183-4c85-9191-d76222f66ede" },
    message_count: 6,
    subagent_count: 2,
    is_subagent_orphan: false,
    subagents: [
      {
        id: "child-bjobs",
        session_id: "agent-a21ae9586acc8afb8",
        agent_id: "a21ae9586acc8afb8",
        agent_tool_use_id: "toolu_017BqzhgcFNzkezabn2PCGRe",
        title: "#131 attribute-grouped bjobs capture",
        status: "running",
        document_ready: true,
        model: "claude-opus-4-8",
        reasoning_effort: "xhigh",
        started_at: "2026-08-01T13:33:46.914Z",
      },
      {
        id: "child-stale",
        session_id: "agent-aa53b331b57f1bde5",
        agent_id: "aa53b331b57f1bde5",
        agent_tool_use_id: "toolu_01JHkBjvPchXVuCH6Z2LiQiv",
        title: "Tune stale threshold to CLEAN_PERIOD",
        status: "running",
        document_ready: true,
        model: "claude-opus-4-8",
        reasoning_effort: "xhigh",
        started_at: "2026-08-01T13:42:23.098Z",
      },
    ],
    synced_at: "2026-08-01T13:42:42.641Z",
    activity_at: "2026-08-01T13:42:42.641Z",
  }),
  messages: /** @type {ConversationMessage[]} */ ([
    {
      id: 1,
      line_number: 30486,
      role: "user",
      content: "Take #131 bjobs speedup in parallel.",
      timestamp: "2026-08-01T13:33:40.000Z",
    },
    {
      id: 2,
      line_number: 30487,
      role: "tool",
      content: "#131 attribute-grouped bjobs capture started",
      agent_event: {
        version: 2,
        source: "claude_agent",
        kind: "started",
        status: "async_launched",
        activity_type: "subagent",
        label: "#131 attribute-grouped bjobs capture",
        agent_tool_use_id: "toolu_017BqzhgcFNzkezabn2PCGRe",
        agent_thread_id: "a21ae9586acc8afb8",
        model: "claude-opus-4-8",
        model_family: "anthropic",
        reasoning_effort: "xhigh",
        started_at: "2026-08-01T13:33:46.914Z",
      },
      timestamp: "2026-08-01T13:33:46.914Z",
    },
    {
      id: 3,
      line_number: 30517,
      role: "user",
      content: "Tune the stale monitor based on CLEAN_PERIOD in parallel.",
      timestamp: "2026-08-01T13:40:20.407Z",
    },
    {
      id: 4,
      line_number: 30521,
      role: "assistant",
      model: "claude-opus-4-8",
      reasoning_effort: "xhigh",
      content: "I will launch the stale-threshold work in an isolated worktree.",
      timestamp: "2026-08-01T13:41:55.070Z",
    },
    {
      id: 5,
      line_number: 30522,
      role: "tool",
      content: "Tune stale threshold to CLEAN_PERIOD started",
      agent_event: {
        version: 2,
        source: "claude_agent",
        kind: "started",
        status: "async_launched",
        activity_type: "subagent",
        label: "Tune stale threshold to CLEAN_PERIOD",
        agent_tool_use_id: "toolu_01JHkBjvPchXVuCH6Z2LiQiv",
        agent_thread_id: "aa53b331b57f1bde5",
        model: "claude-opus-4-8",
        model_family: "anthropic",
        reasoning_effort: "xhigh",
        started_at: "2026-08-01T13:42:23.098Z",
      },
      timestamp: "2026-08-01T13:42:23.098Z",
    },
    {
      id: 6,
      line_number: 30524,
      role: "assistant",
      model: "claude-opus-4-8",
      reasoning_effort: "xhigh",
      content: "Two agents now running in parallel (#131 bjobs speedup; stale-threshold tuning, isolated).",
      timestamp: "2026-08-01T13:42:42.641Z",
    },
  ]),
  prompts: [
    { id: 1, line_number: 30486, content: "Take #131 bjobs speedup in parallel.", timestamp: "2026-08-01T13:33:40.000Z" },
    { id: 3, line_number: 30517, content: "Tune the stale monitor based on CLEAN_PERIOD in parallel.", timestamp: "2026-08-01T13:40:20.407Z" },
  ],
  pending: EMPTY_PENDING,
  latestAgentLine: 30524,
};

/**
 * Regression #5 — parent-agent labeling + launch description titles.
 *
 * This conversation IS a subagent thread (user_role_origin = parent_agent) and
 * carries an `agent_launch_description` used as its human title. The header must
 * render "Subagent · <launch description>" (+ codename), and the parent agent's
 * dispatch message must render as a labeled "Parent agent" card — not as the
 * human's own chat input.
 */
export const parentAgentLabeling = {
  docId: "conv-parent-agent",
  meta: /** @type {ConversationMeta} */ ({
    id: "conv-parent-agent",
    tool_id: "claude_code",
    title: "Investigate checkout regression",
    relative_path: "claude/sessions/investigate-checkout/subagents/child.jsonl",
    metadata: {
      agent_launch_description: "Investigate checkout regression",
      agent_path: "root/investigate-checkout",
      agent_nickname: "brave-otter",
    },
    user_role_origin: "parent_agent",
    message_count: 2,
    subagent_count: 0,
    synced_at: T2,
    activity_at: T2,
  }),
  messages: /** @type {ConversationMessage[]} */ ([
    {
      id: 1,
      line_number: 1,
      role: "user",
      origin: "parent_agent",
      content: "Please investigate the checkout regression and report the root cause.",
      timestamp: T0,
    },
    {
      id: 2,
      line_number: 2,
      role: "assistant",
      model: "claude-opus-4-1",
      content: "Starting the investigation into the checkout regression now.",
      timestamp: T1,
    },
  ]),
  prompts: [],
  pending: EMPTY_PENDING,
  latestAgentLine: 2,
};

export const cursorParentAgentLabeling = {
  ...parentAgentLabeling,
  docId: "conv-cursor-parent-agent",
  meta: {
    ...parentAgentLabeling.meta,
    id: "conv-cursor-parent-agent",
    tool_id: "cursor",
    relative_path: (
      "projects/demo/agent-transcripts/root-thread/"
      + "subagents/cursor-child.jsonl"
    ),
  },
};

const CURSOR_CURRENT_TASK_STATE = {
  version: 1,
  source: "cursor",
  revision: 1,
  is_current: true,
  completed_count: 1,
  total_count: 2,
  active_task_id: "2",
  tasks: [
    { id: "1", content: "Audit source and API identities", status: "completed" },
    { id: "2", content: "Verify desktop and mobile UI", status: "in_progress" },
  ],
};

/**
 * Distilled from Cursor thread 32034817: a mutable current-task carrier at
 * line 1 plus interleaved child launches/completions. The task carrier powers
 * the pinned card but must not render as a second historical update; child
 * events must retain their own thread IDs even when completion order differs.
 */
export const cursorThreadProjection = {
  docId: "conv-cursor-thread-projection",
  meta: /** @type {ConversationMeta} */ ({
    id: "conv-cursor-thread-projection",
    tool_id: "cursor",
    title: "Cursor thread projection",
    relative_path: (
      "projects/demo/agent-transcripts/root-thread/root-thread.jsonl"
    ),
    metadata: { session_id: "root-thread", is_subagent: false },
    active_task_state: CURSOR_CURRENT_TASK_STATE,
    message_count: 7,
    subagent_count: 2,
    is_subagent_orphan: false,
    subagents: [
      {
        id: "cursor-child-doc-a",
        session_id: "cursor-child-a",
        title: "Audit source ordering",
        parent_thread_id: "root-thread",
        status: "completed",
        document_ready: true,
        user_role_origin: "parent_agent",
        completed_at: T3,
      },
      {
        id: "cursor-child-doc-b",
        session_id: "cursor-child-b",
        title: "Audit UI projection",
        parent_thread_id: "root-thread",
        status: "completed",
        document_ready: true,
        user_role_origin: "parent_agent",
        completed_at: T2,
      },
    ],
    synced_at: T3,
    activity_at: T3,
  }),
  messages: /** @type {ConversationMessage[]} */ ([
    {
      id: 1,
      line_number: 1,
      message_type: "cursor_state_task",
      raw_type: "cursor_state_task",
      role: "tool",
      content: "1 of 2 tasks complete",
      tool_name: "Task progress 1/2",
      task_state: CURSOR_CURRENT_TASK_STATE,
      timestamp: T0,
    },
    {
      id: 2,
      line_number: 2,
      role: "user",
      content: "Launch parallel source and UI audits.",
      timestamp: T0,
    },
    {
      id: 3,
      line_number: 3,
      role: "tool",
      content: "[Subagent]",
      agent_event: {
        version: 1,
        kind: "started",
        activity_type: "subagent",
        agent_thread_id: "cursor-child-a",
        label: "Audit source ordering",
      },
      timestamp: T1,
    },
    {
      id: 4,
      line_number: 4,
      role: "tool",
      content: "[Subagent]",
      agent_event: {
        version: 1,
        kind: "started",
        activity_type: "subagent",
        agent_thread_id: "cursor-child-b",
        label: "Audit UI projection",
      },
      timestamp: T1,
    },
    {
      id: 5,
      line_number: 5,
      role: "assistant",
      content: "Both audits are running in parallel.",
      timestamp: T1,
    },
    {
      id: 6,
      line_number: 6,
      role: "tool",
      content: "[Subagent completed]",
      agent_event: {
        version: 1,
        kind: "completed",
        activity_type: "subagent",
        agent_thread_id: "cursor-child-b",
        label: "Audit UI projection",
      },
      timestamp: T2,
    },
    {
      id: 7,
      line_number: 7,
      role: "tool",
      content: "[Subagent completed]",
      agent_event: {
        version: 1,
        kind: "completed",
        activity_type: "subagent",
        agent_thread_id: "cursor-child-a",
        label: "Audit source ordering",
      },
      timestamp: T3,
    },
  ]),
  prompts: [
    {
      id: 2,
      line_number: 2,
      content: "Launch parallel source and UI audits.",
      timestamp: T0,
    },
  ],
  pending: EMPTY_PENDING,
  latestAgentLine: 7,
};

/**
 * Live Claude AskUserQuestion payload captured from dreamland-yoga.
 *
 * It deliberately models a metadata-only pending prompt: message_id/line_number
 * are zero until the corresponding transcript row is persisted.
 */
export const claudeSideTailLivePrompt = {
  docId: "conv-claude-side-tail-live",
  meta: /** @type {ConversationMeta} */ ({
    id: "conv-claude-side-tail-live",
    tool_id: "claude_code",
    title: "Compare bjobs active jobs with event log collections",
    relative_path: "claude/sessions/side-tail-live.jsonl",
    metadata: {},
    message_count: 2,
    subagent_count: 0,
    pending_question_count: 1,
    synced_at: T3,
    activity_at: T3,
  }),
  messages: /** @type {ConversationMessage[]} */ ([
    {
      id: 1,
      line_number: 1,
      role: "user",
      content: "Investigate whether the accept/switch side-tail is still needed.",
      timestamp: T0,
    },
    {
      id: 2,
      line_number: 2,
      role: "assistant",
      model: "claude-opus-4-8",
      reasoning_effort: "xhigh",
      content: "I traced JOB_ACCEPT, JOB_SWITCH, and JOB_START forwarding.",
      timestamp: T1,
    },
  ]),
  prompts: [
    {
      id: 1,
      line_number: 1,
      content: "Investigate whether the accept/switch side-tail is still needed.",
      timestamp: T0,
    },
    {
      id: 2,
      line_number: 2,
      content: "Review the live queue evidence before changing the parser.",
      timestamp: T1,
    },
  ],
  pending: /** @type {PendingConversationInteractionsResponse} */ ({
    count: 1,
    inferred_responses: [],
    interactions: [{
      document_id: "conv-claude-side-tail-live",
      message_id: 0,
      line_number: 0,
      model: "claude-opus-4-8",
      reasoning_effort: "xhigh",
      timestamp: T2,
      interaction: {
        kind: "question",
        id: "toolu-side-tail",
        source: "claude_code",
        tool_name: "AskUserQuestion",
        questions: [
          {
            id: "Side-tail",
            header: "Side-tail",
            prompt: "Proceed to eliminate the accept/switch side-tail and source forwarding from JOB_START?",
            type: "single_select",
            allow_custom: true,
            options: [
              {
                id: "Yes — delete it, verify on real data first",
                label: "Yes — delete it, verify on real data first",
                description: "Delete the accept_only reader, {site}.job_accept cursor, EOF-seed, durable-snapshot gate, and the JOB_ACCEPT/JOB_SWITCH handlers. Source forwarding from JOB_START. But first confirm on real ETL/master lsb.stream that JOB_START src/dst cluster populate for forwarded jobs and STATUS2 queue reflects a bswitch.",
              },
              {
                id: "Yes — delete it now",
                label: "Yes — delete it now",
                description: "Same deletion, but proceed on the struct/parser evidence without the live-data pre-check (verify after via SLO queue/cluster match%).",
              },
              {
                id: "Not yet — keep side-tail",
                label: "Not yet — keep side-tail",
                description: "Leave the side-tail as-is for now (D's un-routing stands); revisit the elimination later. Ship E + the MODIFY2 guard fix only.",
              },
            ],
          },
          {
            id: "Queue freshness",
            header: "Queue freshness",
            prompt: "JOB_SWITCH queue freshness once the side-tail is gone?",
            type: "single_select",
            allow_custom: true,
            options: [
              {
                id: "Accept STATUS2-cadence queue",
                label: "Accept STATUS2-cadence queue",
                description: "Rely on JOB_STATUS2/FINISH2 for the current queue (verify match% via SLO). Simplest; small freshness loss on bswitch'd jobs between status samples.",
              },
              {
                id: "Keep switch-instant precision",
                label: "Keep switch-instant precision",
                description: "Retain a mechanism for switch-time queue precision rather than waiting for the next STATUS2. More code; preserves exact bswitch timing.",
              },
            ],
          },
        ],
      },
    }],
  }),
  latestAgentLine: 2,
};

const SMART_LINK_MARKDOWN = [
  "This intentionally long fixture keeps the rich links behind the conversation expand control. ".repeat(7),
  "",
  "- Edited [`docs/HANDOFF.md +40 -0`](docs/HANDOFF.md)",
  "- Source: [SmartLink.tsx](src/components/viewers/SmartLink.tsx)",
  "- Engine: [engine.py](services/engine.py)",
  "- Core: [mod.rs](src/core/mod.rs)",
  "- Service: [main.go](cmd/server/main.go)",
  "- Manifest: [package.json](package.json)",
  "- Container: [Dockerfile](Dockerfile)",
  "- Asset: [logo.svg](assets/logo.svg)",
  "- Settings: [settings.json](data/settings.json)",
  "- Pipeline: [ci.yaml](.github/workflows/ci.yaml)",
  "- Notebook: [analysis.ipynb](notebooks/analysis.ipynb)",
  "- Report: [architecture.pdf](docs/architecture.pdf)",
  "- Module directory: `src/components/`",
  "- Production config: `config/prod.toml`",
  "- Release script: `scripts/release.ps1`",
  "- Refresh entry point: `run_refresh_active_via_bjobs`",
  "- [Rescue baseline → current FastAPI](https://gitlab.com/crashbandicode/memento/-/compare/f54a57bd...13ab85e7)",
  "- [Release commit](https://github.com/crashbandicode/memento/commit/9c216b8aa55aa55aa55aa55aa55aa55aa55aa55a)",
  "- Deployed revision: `9c216b8`",
  "- [Memento deployment](https://memento.babypotatofarm.com/status)",
  "",
  "```python",
  "run_refresh_active_via_bjobs()",
  "```",
].join("\n");

function smartLinkScenario(toolId, suffix) {
  return {
    docId: `conv-smart-links-${suffix}`,
    meta: {
      id: `conv-smart-links-${suffix}`,
      tool_id: toolId,
      title: `Smart links from ${toolId}`,
      relative_path: `${suffix}/sessions/smart-links.jsonl`,
      metadata: {},
      message_count: 2,
      subagent_count: 0,
      synced_at: T2,
      activity_at: T2,
    },
    messages: [
      {
        id: 1,
        line_number: 1,
        role: "user",
        content: "Show the affected files, compare, commit, and deployment link.",
        timestamp: T0,
      },
      {
        id: 2,
        line_number: 2,
        role: "assistant",
        model: `${toolId}-fixture-model`,
        content: SMART_LINK_MARKDOWN,
        timestamp: T1,
      },
    ],
    prompts: [
      {
        id: 1,
        line_number: 1,
        content: "Show the affected files, compare, commit, and deployment link.",
        timestamp: T0,
      },
    ],
    pending: EMPTY_PENDING,
    latestAgentLine: 2,
  };
}

export const claudeSmartLinks = smartLinkScenario("claude_code", "claude");
export const cursorSmartLinks = smartLinkScenario("cursor", "cursor");
export const codexSmartLinks = smartLinkScenario("codex", "codex");
export const smartLinkScenarios = [claudeSmartLinks, cursorSmartLinks, codexSmartLinks];

/**
 * Canvas artifacts across the three tools. Each scenario exercises a different
 * viewer mode so the shared detection + secure-viewer layer is covered end to
 * end:
 *   - Cursor  → captured INTERACTIVE preview with authenticated source.
 *   - Codex   → sandboxed EMBED of a self-contained HTML export.
 *   - Claude  → link-only, no server descriptor → UNSUPPORTED fallback.
 * These are simulated snapshots, not live data.
 */
const CURSOR_CANVAS_PATH =
  "C:/Users/patrick/.cursor/projects/memento/canvases/billing-review.canvas.tsx";
const CURSOR_CANVAS_SOURCE = [
  'import { Canvas, Card, Metric } from "cursor/canvas";',
  "",
  "export default function BillingReview() {",
  "  return (",
  "    <Canvas>",
  '      <Card title="Monthly recurring revenue">',
  '        <Metric label="MRR" value="$48,200" />',
  "      </Card>",
  "    </Canvas>",
  "  );",
  "}",
].join("\n");
const CURSOR_CANVAS_ARTIFACT_ID = "11111111-1111-4111-8111-111111111111";
const CURSOR_CANVAS_SHELL = [
  "<!doctype html><html><head>",
  '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; connect-src \'none\'; frame-src \'none\'; script-src \'unsafe-inline\'">',
  "</head><body>",
  '<main id="captured-canvas-marker">Captured Cursor canvas rendered</main>',
  "</body></html>",
].join("");

export const cursorCanvas = {
  docId: "conv-canvas-cursor",
  meta: /** @type {ConversationMeta} */ ({
    id: "conv-canvas-cursor",
    tool_id: "cursor",
    title: "Billing review canvas",
    relative_path: "cursor/sessions/billing-canvas.jsonl",
    metadata: {},
    message_count: 2,
    subagent_count: 0,
    synced_at: T2,
    activity_at: T2,
  }),
  messages: /** @type {ConversationMessage[]} */ ([
    {
      id: 1,
      line_number: 1,
      role: "user",
      content: "Summarize the billing numbers in a canvas.",
      timestamp: T0,
    },
    {
      id: 2,
      line_number: 2,
      role: "assistant",
      model: "cursor-fixture-model",
      content: `I built the canvas at [billing-review](${CURSOR_CANVAS_PATH}). Open it beside the chat.`,
      canvases: [
        {
          name: "billing-review",
          path: CURSOR_CANVAS_PATH,
          href: CURSOR_CANVAS_PATH,
          source_kind: "interactive",
          artifact_id: CURSOR_CANVAS_ARTIFACT_ID,
          render_url: `/api/canvas-artifacts/${CURSOR_CANVAS_ARTIFACT_ID}/render`,
          source_url: `/api/canvas-artifacts/${CURSOR_CANVAS_ARTIFACT_ID}/source`,
          capture_status: "renderable",
        },
      ],
      timestamp: T1,
    },
  ]),
  prompts: [
    { id: 1, line_number: 1, content: "Summarize the billing numbers in a canvas.", timestamp: T0 },
  ],
  pending: EMPTY_PENDING,
  latestAgentLine: 2,
  canvasArtifacts: {
    [CURSOR_CANVAS_ARTIFACT_ID]: {
      render: CURSOR_CANVAS_SHELL,
      source: CURSOR_CANVAS_SOURCE,
    },
  },
};

const CODEX_CANVAS_PATH =
  "/Users/patrick/.cursor/projects/memento/canvases/latency-report.canvas.tsx";
const CODEX_CANVAS_HTML = [
  "<!doctype html>",
  "<html><head><title>Latency report</title></head>",
  '<body><main id="codex-canvas-marker">Codex canvas rendered</main>',
  "<script>document.getElementById('codex-canvas-marker').dataset.ready='1';</script>",
  "</body></html>",
].join("\n");

export const codexCanvas = {
  docId: "conv-canvas-codex",
  meta: /** @type {ConversationMeta} */ ({
    id: "conv-canvas-codex",
    tool_id: "codex",
    title: "Latency report canvas",
    relative_path: "codex/sessions/latency-canvas.jsonl",
    metadata: {},
    message_count: 2,
    subagent_count: 0,
    synced_at: T2,
    activity_at: T2,
  }),
  messages: /** @type {ConversationMessage[]} */ ([
    {
      id: 1,
      line_number: 1,
      role: "user",
      content: "Render the latency report as a canvas.",
      timestamp: T0,
    },
    {
      id: 2,
      line_number: 2,
      role: "assistant",
      model: "codex-fixture-model",
      content: `Exported the interactive report to [latency-report](${CODEX_CANVAS_PATH}).`,
      canvases: [
        {
          name: "latency-report",
          path: CODEX_CANVAS_PATH,
          href: CODEX_CANVAS_PATH,
          source_kind: "embed",
          html: CODEX_CANVAS_HTML,
        },
      ],
      timestamp: T1,
    },
  ]),
  prompts: [
    { id: 1, line_number: 1, content: "Render the latency report as a canvas.", timestamp: T0 },
  ],
  pending: EMPTY_PENDING,
  latestAgentLine: 2,
};

const CLAUDE_CANVAS_PATH =
  "/Users/patrick/.cursor/projects/memento/canvases/security-audit.canvas.tsx";

export const claudeCanvas = {
  docId: "conv-canvas-claude",
  meta: /** @type {ConversationMeta} */ ({
    id: "conv-canvas-claude",
    tool_id: "claude_code",
    title: "Security audit canvas",
    relative_path: "claude/sessions/audit-canvas.jsonl",
    metadata: {},
    message_count: 2,
    subagent_count: 0,
    synced_at: T2,
    activity_at: T2,
  }),
  messages: /** @type {ConversationMessage[]} */ ([
    {
      id: 1,
      line_number: 1,
      role: "user",
      content: "Put the audit findings in a canvas.",
      timestamp: T0,
    },
    {
      id: 2,
      line_number: 2,
      role: "assistant",
      model: "claude-opus-4-1",
      // No `canvases` descriptor: the transcript only references the artifact,
      // so the chip resolves link-only and the viewer must show the honest
      // "preview unavailable" fallback.
      content: `Findings are in the canvas at [security-audit](${CLAUDE_CANVAS_PATH}).`,
      timestamp: T1,
    },
  ]),
  prompts: [
    { id: 1, line_number: 1, content: "Put the audit findings in a canvas.", timestamp: T0 },
  ],
  pending: EMPTY_PENDING,
  latestAgentLine: 2,
};

export const canvasScenarios = [cursorCanvas, codexCanvas, claudeCanvas];

/**
 * URL-navigation regression fixture: 260 rows force around-window loading,
 * repeated Unicode search hits exercise stable ordinals, and line 180 is tall
 * enough to verify normalized intra-message restoration across viewports.
 */
const URL_NAVIGATION_QUERY = "navigation needle 🧭";
const URL_NAVIGATION_PROMPT_LINES = new Set(
  Array.from({ length: 11 }, (_, index) => 1 + index * 25),
);
const URL_NAVIGATION_MESSAGES = Array.from({ length: 260 }, (_, index) => {
  const line = index + 1;
  const isPrompt = URL_NAVIGATION_PROMPT_LINES.has(line);
  const timestamp = new Date(Date.parse(T0) + line * 1_000).toISOString();
  let content = `Deterministic conversation row ${line}.`;
  if (line % 13 === 0) {
    content += ` Repeated ${URL_NAVIGATION_QUERY} match at stable line ${line}.`;
  }
  let toolCalls;
  if (line === 180) {
    content = `Long structured message: ${URL_NAVIGATION_QUERY}.`;
    toolCalls = Array.from({ length: 24 }, (_, toolIndex) => ({
      name: "ReadFile",
      input: JSON.stringify({ path: `/fixture/segment-${toolIndex + 1}.md` }),
      result: `Deterministic tool result ${toolIndex + 1} for URL position restoration.`,
    }));
  }
  return /** @type {ConversationMessage} */ ({
    id: 10_000 + line,
    line_number: line,
    role: isPrompt ? "user" : line % 2 === 0 ? "assistant" : "tool",
    model: line % 2 === 0 ? "cursor-fixture-model" : undefined,
    content,
    tool_calls: toolCalls,
    timestamp,
  });
});

export const urlNavigationLargeThread = {
  docId: "conv-url-navigation-large",
  searchQuery: URL_NAVIGATION_QUERY,
  longMessageLine: 180,
  deepLinkLine: 217,
  meta: /** @type {ConversationMeta} */ ({
    id: "conv-url-navigation-large",
    tool_id: "cursor",
    title: "Large URL navigation regression",
    relative_path: "cursor/sessions/url-navigation-large.jsonl",
    metadata: {},
    message_count: URL_NAVIGATION_MESSAGES.length,
    subagent_count: 0,
    synced_at: T3,
    activity_at: T3,
  }),
  messages: URL_NAVIGATION_MESSAGES,
  prompts: URL_NAVIGATION_MESSAGES
    .filter((message) => URL_NAVIGATION_PROMPT_LINES.has(message.line_number))
    .map((message) => ({
      id: message.id,
      line_number: message.line_number,
      content: message.content,
      timestamp: message.timestamp,
    })),
  pending: EMPTY_PENDING,
  latestAgentLine: 260,
};

export const urlNavigationCanvasThread = {
  ...urlNavigationLargeThread,
  docId: "conv-url-navigation-canvas",
  meta: {
    ...urlNavigationLargeThread.meta,
    id: "conv-url-navigation-canvas",
    title: "Canvas URL navigation regression",
    relative_path: "cursor/sessions/url-navigation-canvas.jsonl",
  },
  messages: urlNavigationLargeThread.messages.map((message) =>
    message.line_number === urlNavigationLargeThread.longMessageLine
      ? {
        ...message,
        content: `${message.content} [billing-review](${CURSOR_CANVAS_PATH})`,
        canvases: [
          {
            name: "billing-review",
            path: CURSOR_CANVAS_PATH,
            href: CURSOR_CANVAS_PATH,
            source_kind: "interactive",
            artifact_id: CURSOR_CANVAS_ARTIFACT_ID,
            render_url: `/api/canvas-artifacts/${CURSOR_CANVAS_ARTIFACT_ID}/render`,
            source_url: `/api/canvas-artifacts/${CURSOR_CANVAS_ARTIFACT_ID}/source`,
            capture_status: "renderable",
          },
        ],
      }
      : message
  ),
  canvasArtifacts: {
    [CURSOR_CANVAS_ARTIFACT_ID]: {
      render: CURSOR_CANVAS_SHELL,
      source: CURSOR_CANVAS_SOURCE,
    },
  },
};

/** All scenarios keyed by docId, for the mock router + node tests. */
export const scenarios = {
  [permissionWrappedQuestion.docId]: permissionWrappedQuestion,
  [metadataOnlyPrompts.docId]: metadataOnlyPrompts,
  [runningSubagent.docId]: runningSubagent,
  [parentAgentLabeling.docId]: parentAgentLabeling,
  [cursorParentAgentLabeling.docId]: cursorParentAgentLabeling,
  [cursorThreadProjection.docId]: cursorThreadProjection,
  [claudeSideTailLivePrompt.docId]: claudeSideTailLivePrompt,
  [claudeSmartLinks.docId]: claudeSmartLinks,
  [cursorSmartLinks.docId]: cursorSmartLinks,
  [codexSmartLinks.docId]: codexSmartLinks,
  [cursorCanvas.docId]: cursorCanvas,
  [codexCanvas.docId]: codexCanvas,
  [claudeCanvas.docId]: claudeCanvas,
  [urlNavigationLargeThread.docId]: urlNavigationLargeThread,
  [urlNavigationCanvasThread.docId]: urlNavigationCanvasThread,
};
