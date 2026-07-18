export function getApiBase(): string {
  const configuredBase = process.env.NEXT_PUBLIC_MEMENTO_API_BASE;
  if (configuredBase) return configuredBase.replace(/\/$/, "");
  if (typeof window !== "undefined") {
    const { protocol, hostname, port } = window.location;
    // If accessed via standard ports (80/443) — likely behind reverse proxy, API at same origin
    if (!port || port === "80" || port === "443") {
      return `${protocol}//${hostname}`;
    }
    // Direct access with port — API on port 8001
    return `${protocol}//${hostname}:8001`;
  }
  return "http://localhost:8001";
}

// Keep for import compat — but always call getApiBase() instead
export const API_BASE = "";

/** Authenticated fetch — wraps native fetch with the remembered/session JWT. */
export function authFetch(url: string, init?: RequestInit): Promise<Response> {
  const token = _getToken();
  const headers: Record<string, string> = {
    ...(init?.headers as Record<string, string>),
  };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  return fetch(url, { ...init, headers }).then((res) => {
    if (res.status === 401 && typeof window !== "undefined") {
      const onAuthPage = window.location.pathname.startsWith("/auth/");
      clearStoredAuthToken();
      if (!onAuthPage) {
        window.location.href = "/auth/login";
      }
    }
    return res;
  });
}

interface FetchOptions extends RequestInit {
  token?: string;
}

function _getToken(): string | null {
  return getStoredAuthToken();
}

// In-flight GET deduplication: if the same URL is requested while a previous
// request is still pending, return the same Promise. Cleared on completion.
const _inflight = new Map<string, Promise<unknown>>();

// Short-lived response cache for idempotent GETs (60s), lives for the tab session.
const _cache = new Map<string, { ts: number; data: unknown }>();
const CACHE_TTL_MS = 60_000;
const CACHE_MAX_ENTRIES = 200;

export function invalidateApiCache(prefix?: string) {
  if (!prefix) { _cache.clear(); return; }
  for (const k of _cache.keys()) if (k.startsWith(prefix)) _cache.delete(k);
}

/** Drop the cached prompt outline for one conversation after its transcript changes. */
export function invalidateConversationPrompts(id: string) {
  invalidateApiCache(`${getApiBase()}/api/conversations/${id}/prompts`);
}

/** Drop cached message pages for one conversation after its transcript changes. */
export function invalidateConversationMessages(id: string) {
  invalidateApiCache(`${getApiBase()}/api/conversations/${id}/messages`);
}

/** Drop cached within-thread search pages after the transcript changes. */
export function invalidateConversationSearch(id: string) {
  invalidateApiCache(`${getApiBase()}/api/conversations/${id}/search`);
}

function getCached<T>(cacheKey: string): T | null {
  const hit = _cache.get(cacheKey);
  if (!hit) return null;
  if (Date.now() - hit.ts >= CACHE_TTL_MS) {
    _cache.delete(cacheKey);
    return null;
  }
  return hit.data as T;
}

function setCached(cacheKey: string, data: unknown) {
  _cache.set(cacheKey, { ts: Date.now(), data });
  for (const [key, entry] of _cache) {
    if (_cache.size <= CACHE_MAX_ENTRIES && Date.now() - entry.ts < CACHE_TTL_MS) break;
    if (_cache.size > CACHE_MAX_ENTRIES || Date.now() - entry.ts >= CACHE_TTL_MS) {
      _cache.delete(key);
    }
  }
}

async function apiFetch<T>(path: string, opts: FetchOptions = {}): Promise<T> {
  const { token, ...init } = opts;
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init.headers as Record<string, string>),
  };
  // Public endpoints never attach Authorization and never redirect on 401.
  // Landing page + install bootstrap probes live here.
  const isPublic = path.startsWith("/api/public/");

  // Auto-attach JWT from localStorage (explicit token param takes priority,
  // except for public endpoints which never carry credentials).
  const jwt = isPublic ? undefined : (token || _getToken());
  if (jwt) {
    headers["Authorization"] = `Bearer ${jwt}`;
  }

  const base = getApiBase();
  const method = (init.method || "GET").toUpperCase();
  const cacheKey = method === "GET" && init.cache !== "no-store"
    ? `${base}${path}`
    : null;

  if (cacheKey) {
    const hit = getCached<T>(cacheKey);
    if (hit) return hit;
    const pending = _inflight.get(cacheKey) as Promise<T> | undefined;
    if (pending) return pending;
  }

  const run = (async () => {
    const res = await fetch(`${base}${path}`, { ...init, headers });
    if (!res.ok) {
      if (res.status === 401 && typeof window !== "undefined" && !isPublic) {
        const onAuthPage = window.location.pathname.startsWith("/auth/");
        const onLanding = window.location.pathname === "/" || window.location.pathname === "/splash";
        clearStoredAuthToken();
        if (!onAuthPage && !onLanding) window.location.href = "/auth/login";
        throw new Error("Unauthorized");
      }
      const text = await res.text().catch(() => "");
      throw new Error(`API ${res.status}: ${text}`);
    }
    const data = (await res.json()) as T;
    if (cacheKey) setCached(cacheKey, data);
    return data;
  })();

  if (cacheKey) {
    _inflight.set(cacheKey, run);
    // Avoid `.finally()` here: it creates a second rejected promise on HTTP
    // errors, which surfaces as an unhandled browser `pageerror` even when the
    // caller correctly catches the original request (for example stale JWT).
    void run.then(
      () => _inflight.delete(cacheKey),
      () => _inflight.delete(cacheKey),
    );
  }
  return run;
}

// --- Types ---

export interface ToolSummary {
  id: string;
  display_name: string;
  icon: string | null;
  total_files: number;
  total_size_bytes: number;
  last_sync_at: string | null;
}

export interface ToolDetail extends ToolSummary {
  categories: Record<string, number>;
}

export interface ProjectSummary {
  id: string;
  slug: string;
  title: string;
  tool_id: string;
  source_path: string;
  document_count: number;
  created_at: string;
  updated_at: string | null;
}

export interface DocumentSummary {
  id: string;
  relative_path: string;
  category: string;
  content_type: string;
  title: string | null;
  file_size_bytes: number;
  activity_at?: string | null;
  synced_at: string;
  ai_summary?: string | null;
  device_name?: string | null;
  message_count?: number | null;
  is_low_activity?: boolean | null;
  subagent_count?: number;
  is_subagent_orphan?: boolean;
}

export interface DocumentDetail {
  id: string;
  tool_id: string;
  project_id: string | null;
  relative_path: string;
  category: string;
  content_type: string;
  title: string | null;
  content: string | null;
  content_hash: string;
  file_size_bytes: number;
  metadata: Record<string, unknown>;
  ai_summary: string | null;
  synced_at: string;
  created_at: string;
  updated_at: string | null;
}

export interface ConversationSubagentSummary {
  id: string;
  session_id: string | null;
  title: string;
  agent_nickname?: string | null;
  agent_path?: string | null;
  agent_depth?: number | null;
  parent_thread_id?: string | null;
  relative_path?: string | null;
  timestamp?: string | null;
  activity_at?: string | null;
  synced_at?: string | null;
}

export interface ConversationTask {
  id: string;
  content: string;
  status: "pending" | "in_progress" | "completed" | "blocked" | "cancelled" | string;
  active_form?: string;
}

export interface ConversationTaskState {
  version: number;
  source: string;
  revision: number;
  is_current?: boolean;
  completed_count: number;
  total_count: number;
  active_task_id?: string;
  tasks: ConversationTask[];
}

export interface ConversationMeta {
  id: string;
  tool_id: string;
  title: string | null;
  relative_path: string;
  metadata: Record<string, unknown>;
  active_task_state?: ConversationTaskState | null;
  message_count: number;
  subagent_count?: number;
  is_subagent_orphan?: boolean;
  subagents?: ConversationSubagentSummary[];
  activity_at?: string | null;
  synced_at: string;
}

export interface ConversationMarkdownExportSettings {
  start_at?: string | null;
  end_at?: string | null;
  prompt_range?: string;
  query?: string;
  tool_ids?: string[];
  project_ids?: string[];
  include_subagents?: boolean;
  include_low_activity?: boolean;
  include_tools: boolean;
  include_thinking: boolean;
  include_session_context: boolean;
  include_timestamps: boolean;
  output?: "zip" | "combined";
  max_threads?: number;
}

export interface MarkdownExportDownload {
  blob: Blob;
  filename: string;
  exportedThreads: number | null;
  matchingThreads: number | null;
  truncated: boolean;
}

export interface ExportDiagnostics {
  step_count?: number;
  assistant_response_count?: number;
  assistant_thinking_count?: number;
  assistant_thinking_only_count?: number;
  assistant_fallback_count?: number;
  step_fetch_failed?: boolean;
  endpoint_count?: number;
  pb_shell_only?: boolean;
  generator_metadata_messages?: number;
  transcript_messages?: number;
  messages_truncated?: boolean;
  offline_vscdb_messages?: number;
  offline_vscdb_assistant_messages?: number;
  offline_vscdb_system_messages?: number;
  chat_export_messages?: number;
  chat_export_user_messages?: number;
  chat_export_action_messages?: number;
  offline_pb_transcript_messages?: number;
  offline_pb_messages?: number;
  offline_pb_total_messages?: number;
  offline_pb_string_count?: number;
  pb_file_present?: boolean;
  brain_file_count?: number;
  browser_recording_frame_count?: number;
  browser_recording_highlight_count?: number;
}

export interface QuestionOption {
  id: string;
  label: string;
  description?: string;
  short_label?: string;
}

export interface QuestionItem {
  id: string;
  header?: string;
  prompt: string;
  type: "single_select" | "multi_select" | "free_text";
  allow_custom: boolean;
  options: QuestionOption[];
}

export interface QuestionInteraction {
  kind: "question";
  id: string;
  source: string;
  tool_name: string;
  questions: QuestionItem[];
}

export interface QuestionAnswer {
  question_id: string;
  text: string;
  selected_option_ids: string[];
}

export interface QuestionInteractionResponse {
  kind: "question_response";
  interaction_id: string;
  status: "answered" | "cancelled";
  answers: QuestionAnswer[];
  raw_text: string;
}

export interface ConversationToolCall {
  name: string;
  input: string;
  interaction?: QuestionInteraction;
}

export interface ConversationAttachment {
  type: "image" | "file";
  name: string;
}

export interface ConversationMessage {
  id: number;
  line_number: number;
  message_type?: string | null;
  role: string | null;
  content: string;
  thinking?: string | null;
  model?: string | null;
  reasoning_effort?: string | null;
  session_context?: string | null;
  attachments?: ConversationAttachment[];
  tool_name?: string;
  tool_input?: string;
  tool_calls?: ConversationToolCall[];
  interaction?: QuestionInteraction | null;
  interaction_response?: QuestionInteractionResponse | null;
  task_state?: ConversationTaskState | null;
  raw_type?: string;
  metadata?: Record<string, unknown>;
  timestamp: string | null;
}

export interface MessagesResponse {
  total: number;
  offset: number;
  limit: number;
  messages: ConversationMessage[];
}

export interface LatestAgentMessageResponse {
  line_number: number | null;
}

export interface ConversationPrompt {
  id: number;
  line_number: number;
  content: string;
  timestamp: string | null;
}

export interface ConversationPromptsResponse {
  prompts: ConversationPrompt[];
}

export interface DailyDate {
  date: string;
  document_count: number;
  tools?: string[];
}

export interface DeviceSummary {
  id: string;
  name: string;
  device_id: string;
  last_heartbeat: string | null;
  created_at?: string;
  collector_version?: string | null;
  document_count?: number;
  total_files?: number;
  tools?: string[];
}

export interface DailyDetail {
  date: string;
  total_documents: number;
  tools: Record<string, DocumentSummary[]>;
  summaries: { id: string; tool_id: string | null; title: string; summary: string; highlights: unknown }[];
}

export interface SearchResult {
  query: string;
  total: number;
  offset: number;
  limit: number;
  results: {
    id: string;
    tool_id: string;
    relative_path: string;
    category: string;
    title: string | null;
    snippet: string;
    file_size_bytes: number;
    activity_at?: string | null;
    synced_at: string;
    subagent_count?: number;
    is_subagent_orphan?: boolean;
    subagents?: ConversationSubagentSummary[];
    matched_subagent_id?: string | null;
  }[];
}

export interface ConversationSearchHit {
  id: number;
  line_number: number;
  role: "user" | "assistant" | string;
  snippet: string;
  timestamp: string | null;
  score: number;
  match_type: "exact" | "full_text" | "fuzzy" | string;
}

export interface ConversationSearchResponse {
  query: string;
  results: ConversationSearchHit[];
  next_after_line: number | null;
  has_more: boolean;
  corrected_query: string | null;
}

export interface GlobalMessageSearchHit extends ConversationSearchHit {
  matched_document_id: string;
  is_subagent_hit: boolean;
}

export interface GlobalMessageSearchGroup {
  id: string;
  tool_id: string;
  relative_path: string;
  title: string | null;
  activity_at: string | null;
  subagent_count: number;
  is_subagent_orphan: boolean;
  subagents: ConversationSubagentSummary[];
  hits: GlobalMessageSearchHit[];
}

export interface GlobalMessageSearchResponse {
  query: string;
  results: GlobalMessageSearchGroup[];
  next_cursor: string | null;
  has_more: boolean;
  corrected_query: string | null;
}

// Timeline
export interface TimelinePreviewMessage {
  id: number;
  line_number: number;
  role: string;
  content: string;
  tool_name?: string;
  timestamp: string | null;
}

export interface TimelineArtifact {
  id: string;
  title: string;
  doc_type: string;
  content_preview: string | null;
  file_size_bytes: number;
}

export interface TimelineConversation {
  id: string;
  title: string;
  message_count: number;
  preview_messages: TimelinePreviewMessage[];
  file_size_bytes: number;
  subagent_count?: number;
  is_subagent_orphan?: boolean;
  subagents?: ConversationSubagentSummary[];
}

export interface TimelineEvent {
  // Common
  type: string; // "session" (grouped) or category name (standalone)
  tool_id: string;
  tool_name: string;
  title?: string;
  timestamp: string;
  // Session-grouped events
  session_id?: string;
  conversation?: TimelineConversation;
  artifacts?: TimelineArtifact[];
  // Standalone events (non-session)
  id?: string;
  relative_path?: string;
  content_type?: string;
  ai_summary?: string | null;
  file_size_bytes?: number;
  preview_messages?: TimelinePreviewMessage[];
  message_count?: number;
  subagent_count?: number;
  is_subagent_orphan?: boolean;
  subagents?: ConversationSubagentSummary[];
  content_preview?: string;
}

export interface TimelineResponse {
  project: {
    id: string;
    slug: string;
    title: string;
    tool_id: string;
    source_path: string;
  };
  total: number;
  offset: number;
  limit: number;
  events: TimelineEvent[];
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  user_id: string;
  role: string;
}

export interface UserInfo {
  id: string;
  email: string;
  name: string | null;
  role: string;
  status: string;
  collector_token?: string | null;
  totp_enabled?: boolean;
}

// --- API functions ---

async function markdownExportDownload(
  path: string,
  init?: RequestInit,
): Promise<MarkdownExportDownload> {
  const response = await authFetch(`${getApiBase()}${path}`, init);
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json() as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      const text = await response.text();
      if (text) detail = text;
    }
    throw new Error(detail);
  }
  const disposition = response.headers.get("Content-Disposition") || "";
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const regular = disposition.match(/filename="?([^";]+)"?/i)?.[1];
  const filename = encoded
    ? decodeURIComponent(encoded)
    : regular || `memento-conversations-${new Date().toISOString().slice(0, 10)}.zip`;
  const exported = response.headers.get("X-Memento-Exported-Threads");
  const matching = response.headers.get("X-Memento-Matching-Threads");
  return {
    blob: await response.blob(),
    filename,
    exportedThreads: exported === null ? null : Number(exported),
    matchingThreads: matching === null ? null : Number(matching),
    truncated: response.headers.get("X-Memento-Truncated") === "true",
  };
}

export interface PublicStats {
  total_documents: number;
  total_messages: number;
  total_devices: number;
  total_tools: number;
}

export const api = {
  getPublicStats: () => apiFetch<PublicStats>("/api/public/stats"),
  getTools: () => apiFetch<ToolSummary[]>("/api/tools"),
  getProjects: () => apiFetch<ProjectSummary[]>("/api/projects"),
  getTool: (id: string) => apiFetch<ToolDetail>(`/api/tools/${id}`),
  getToolFiles: (id: string, category?: string, offset = 0, limit = 50) => {
    const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    if (category) params.set("category", category);
    return apiFetch<DocumentSummary[]>(`/api/tools/${id}/files?${params}`);
  },
  getDocument: (id: string) => apiFetch<DocumentDetail>(`/api/documents/${id}`),
  getConversation: (id: string) => apiFetch<ConversationMeta>(`/api/conversations/${id}`),
  exportConversationMarkdown: (
    id: string,
    settings: ConversationMarkdownExportSettings,
  ) => {
    const params = new URLSearchParams();
    if (settings.start_at) params.set("start_at", settings.start_at);
    if (settings.end_at) params.set("end_at", settings.end_at);
    if (settings.prompt_range) params.set("prompt_range", settings.prompt_range);
    params.set("include_tools", String(settings.include_tools));
    params.set("include_thinking", String(settings.include_thinking));
    params.set("include_session_context", String(settings.include_session_context));
    params.set("include_timestamps", String(settings.include_timestamps));
    return markdownExportDownload(`/api/exports/conversations/${id}?${params}`);
  },
  exportConversationsMarkdown: (settings: ConversationMarkdownExportSettings) =>
    markdownExportDownload("/api/exports/conversations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    }),
  getMessages: (id: string, offset = 0, limit = 50) =>
    apiFetch<MessagesResponse>(`/api/conversations/${id}/messages?offset=${offset}&limit=${limit}`),
  getLatestMessages: (id: string, limit = 200) =>
    apiFetch<MessagesResponse>(`/api/conversations/${id}/messages?tail=true&limit=${limit}`),
  getLatestAgentMessage: (id: string) =>
    apiFetch<LatestAgentMessageResponse>(
      `/api/conversations/${id}/latest-agent-message`,
      { cache: "no-store" },
    ),
  getMessagesAround: (id: string, lineNumber: number, contextBefore = 0, limit = 50) => {
    const params = new URLSearchParams({
      line_number: String(lineNumber),
      context_before: String(contextBefore),
      limit: String(limit),
    });
    return apiFetch<MessagesResponse>(`/api/conversations/${id}/messages?${params}`);
  },
  getPrompts: (id: string) =>
    apiFetch<ConversationPromptsResponse>(`/api/conversations/${id}/prompts`),
  searchConversation: (
    id: string,
    q: string,
    afterLine?: number | null,
    limit = 50,
    signal?: AbortSignal,
  ) => {
    const params = new URLSearchParams({ q, limit: String(limit) });
    if (typeof afterLine === "number") params.set("after_line", String(afterLine));
    return apiFetch<ConversationSearchResponse>(
      `/api/conversations/${id}/search?${params}`,
      { signal },
    );
  },
  getDailyDates: (days = 30, signal?: AbortSignal) => {
    const tz = new Date().getTimezoneOffset();
    return apiFetch<DailyDate[]>(`/api/daily?days=${days}&tz_offset=${tz}`, { signal });
  },
  getDaily: (date: string, signal?: AbortSignal) => {
    const tz = new Date().getTimezoneOffset();
    return apiFetch<DailyDetail>(`/api/daily/${date}?tz_offset=${tz}`, { signal });
  },
  getDevices: () => apiFetch<DeviceSummary[]>("/api/devices"),
  search: (q: string, tool?: string, offset = 0, limit = 20) => {
    const params = new URLSearchParams({ q, offset: String(offset), limit: String(limit) });
    if (tool) params.set("tool", tool);
    return apiFetch<SearchResult>(`/api/search?${params}`);
  },
  searchMessages: (
    q: string,
    options: {
      tool?: string;
      deviceId?: string | null;
      cursor?: string | null;
      limit?: number;
      signal?: AbortSignal;
    } = {},
  ) => {
    const params = new URLSearchParams({
      q,
      limit: String(options.limit ?? 20),
    });
    if (options.tool) params.set("tool", options.tool);
    if (options.deviceId) params.set("device_id", options.deviceId);
    if (options.cursor) params.set("cursor", options.cursor);
    return apiFetch<GlobalMessageSearchResponse>(`/api/search/messages?${params}`, {
      signal: options.signal,
    });
  },
  register: (email: string, password: string, name?: string, inviteCode?: string) =>
    apiFetch<UserInfo>("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({ email, password, name, invite_code: inviteCode }),
    }),
  getRegistrationMode: () =>
    apiFetch<{ mode: "open" | "invite_only" | "closed"; has_any_user: boolean; github_enabled: boolean }>("/api/auth/registration-mode"),
  login: (email: string, password: string, totpCode?: string) =>
    apiFetch<TokenResponse>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password, totp_code: totpCode || null }),
    }),
  getMe: (token: string) => apiFetch<UserInfo>("/api/auth/me", { token }),
  refreshToken: (token: string) =>
    apiFetch<TokenResponse>("/api/auth/refresh", { method: "POST", token }),
  createEventSession: (token: string) =>
    apiFetch<{ ok: boolean }>("/api/events/session", {
      method: "POST",
      token,
      credentials: "include",
    }),
  clearEventSession: () =>
    apiFetch<{ ok: boolean }>("/api/events/session", {
      method: "DELETE",
      credentials: "include",
    }),
  // === Account-level backup/restore ===
  //
  // exportData hits a binary endpoint so we go around apiFetch's
  // JSON-only flow: fresh fetch with the Bearer header, response.blob()
  // to get the .zip body, then trigger a download via a hidden <a>.
  //
  // importData uploads a multipart body — also off the apiFetch path
  // because that helper hardcodes Content-Type: application/json.
  exportData: async (token: string, includeLogs: boolean): Promise<{ blob: Blob; filename: string; counts: string }> => {
    const url = `${getApiBase()}/api/data/export?include_access_logs=${includeLogs ? "true" : "false"}`;
    const res = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
    if (!res.ok) throw new Error(`Export failed: HTTP ${res.status} ${await res.text()}`);
    const cd = res.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename="?([^";]+)"?/i);
    const filename = m?.[1] ?? `memento-export-${new Date().toISOString().slice(0, 10)}.zip`;
    return {
      blob: await res.blob(),
      filename,
      counts: res.headers.get("X-Memento-Counts") || "",
    };
  },
  importData: async (token: string, file: File): Promise<{
    ok: boolean;
    machine_id: string;
    counts: Record<string, number>;
    warnings: string[];
  }> => {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(`${getApiBase()}/api/data/import`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: form,
    });
    if (!res.ok) {
      const txt = await res.text();
      throw new Error(`Import failed: HTTP ${res.status} ${txt}`);
    }
    return await res.json();
  },
  rotateCollectorToken: (token: string) =>
    apiFetch<UserInfo>("/api/auth/me/rotate-collector-token", { method: "POST", token }),
  setupTotp: (token: string, password: string) =>
    apiFetch<{ secret: string; provisioning_uri: string }>("/api/auth/me/totp/setup", {
      method: "POST", token, body: JSON.stringify({ password }),
    }),
  confirmTotp: (token: string, password: string, code: string) =>
    apiFetch<UserInfo>("/api/auth/me/totp/confirm", {
      method: "POST", token, body: JSON.stringify({ password, code }),
    }),
  disableTotp: (token: string, password: string, code: string) =>
    apiFetch<UserInfo>("/api/auth/me/totp/disable", {
      method: "POST", token, body: JSON.stringify({ password, code }),
    }),
  getProjectTimeline: (projectId: string, offset = 0, limit = 50, category?: string, order = "desc") => {
    const params = new URLSearchParams({ offset: String(offset), limit: String(limit), order });
    if (category) params.set("category", category);
    return apiFetch<TimelineResponse>(`/api/projects/${projectId}/timeline?${params}`);
  },
};
import { clearStoredAuthToken, getStoredAuthToken } from "./auth-storage";
