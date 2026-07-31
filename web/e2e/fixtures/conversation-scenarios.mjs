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

const SMART_LINK_MARKDOWN = [
  "This intentionally long fixture keeps the rich links behind the conversation expand control. ".repeat(7),
  "",
  "- Edited [`docs/HANDOFF.md +40 -0`](docs/HANDOFF.md)",
  "- Production config: `config/prod.toml`",
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
 *   - Cursor  → read-only SOURCE preview (transcript embedded the TSX).
 *   - Codex   → sandboxed EMBED of a self-contained HTML export.
 *   - Claude  → link-only, no server descriptor → UNSUPPORTED fallback.
 * These are simulated snapshots, not live data.
 */
const CURSOR_CANVAS_PATH =
  "/Users/patrick/.cursor/projects/memento/canvases/billing-review.canvas.tsx";
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
          source_kind: "source",
          source: CURSOR_CANVAS_SOURCE,
          source_language: "tsx",
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

/** All scenarios keyed by docId, for the mock router + node tests. */
export const scenarios = {
  [permissionWrappedQuestion.docId]: permissionWrappedQuestion,
  [metadataOnlyPrompts.docId]: metadataOnlyPrompts,
  [runningSubagent.docId]: runningSubagent,
  [parentAgentLabeling.docId]: parentAgentLabeling,
  [claudeSmartLinks.docId]: claudeSmartLinks,
  [cursorSmartLinks.docId]: cursorSmartLinks,
  [codexSmartLinks.docId]: codexSmartLinks,
  [cursorCanvas.docId]: cursorCanvas,
  [codexCanvas.docId]: codexCanvas,
  [claudeCanvas.docId]: claudeCanvas,
};
