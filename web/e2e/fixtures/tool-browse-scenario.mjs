// @ts-check

const T0 = "2026-07-31T13:00:00.000Z";

export const BUTTERBRIDGE_DEVICE_ID = "device-butterbridge";
export const YOGA_DEVICE_ID = "device-yoga";

const devices = [
  {
    id: "machine-butterbridge",
    device_id: BUTTERBRIDGE_DEVICE_ID,
    name: "butterbridge (Windows)",
    last_heartbeat: T0,
    total_files: 1,
  },
  {
    id: "machine-yoga",
    device_id: YOGA_DEVICE_ID,
    name: "dreamland-yoga (Windows)",
    last_heartbeat: T0,
    total_files: 3,
  },
];

const files = [
  {
    id: "claude-conversation-1",
    relative_path: "claude/projects/memento/session-1.jsonl",
    category: "conversation",
    content_type: "jsonl",
    title: "Fix mobile conversation browsing",
    file_size_bytes: 4096,
    activity_at: T0,
    synced_at: T0,
    device_name: "dreamland-yoga (Windows)",
    message_count: 12,
    is_low_activity: false,
    subagent_count: 0,
    is_subagent_orphan: false,
  },
  {
    id: "claude-conversation-2",
    relative_path: "claude/projects/memento/session-2.jsonl",
    category: "conversation",
    content_type: "jsonl",
    title: "Verify all-device dashboard scope",
    file_size_bytes: 3072,
    activity_at: "2026-07-31T12:55:00.000Z",
    synced_at: "2026-07-31T12:55:00.000Z",
    device_name: "dreamland-yoga (Windows)",
    message_count: 8,
    is_low_activity: false,
    subagent_count: 0,
    is_subagent_orphan: false,
  },
  {
    id: "claude-conversation-3",
    relative_path: "claude/projects/memento/session-3.jsonl",
    category: "conversation",
    content_type: "jsonl",
    title: "Keep tool counts consistent",
    file_size_bytes: 2048,
    activity_at: "2026-07-31T12:50:00.000Z",
    synced_at: "2026-07-31T12:50:00.000Z",
    device_name: "dreamland-yoga (Windows)",
    message_count: 5,
    is_low_activity: false,
    subagent_count: 0,
    is_subagent_orphan: false,
  },
];

function dashboard(totalFiles) {
  return {
    tools: [
      {
        id: "claude_code",
        display_name: "Claude Code",
        total_files: totalFiles,
        last_sync_at: T0,
        categories: totalFiles === 1 ? { config: 1 } : { conversation: 3 },
        today_count: totalFiles,
        conversation_count: totalFiles === 1 ? 0 : 3,
      },
    ],
    recent_conversations: [],
    daily: [],
    tool_daily: {},
    devices,
    stats: {
      total_documents: totalFiles,
      total_projects: totalFiles === 1 ? 0 : 1,
      total_tools: 1,
      total_devices: devices.length,
      today_total: totalFiles,
      today_conversations: totalFiles === 1 ? 0 : 3,
    },
  };
}

export const mobileToolBrowse = {
  devices,
  hierarchyDevices: [],
  dashboard: dashboard(3),
  dashboardByDevice: {
    [BUTTERBRIDGE_DEVICE_ID]: dashboard(1),
  },
  toolDetails: {
    claude_code: {
      id: "claude_code",
      display_name: "Claude Code",
      icon: null,
      total_files: 3,
      total_size_bytes: 9216,
      last_sync_at: T0,
      categories: { conversation: 3 },
    },
  },
  toolDetailsByDevice: {
    [BUTTERBRIDGE_DEVICE_ID]: {
      claude_code: {
        id: "claude_code",
        display_name: "Claude Code",
        icon: null,
        total_files: 1,
        total_size_bytes: 512,
        last_sync_at: T0,
        categories: { config: 1 },
      },
    },
  },
  toolFiles: {
    claude_code: files,
  },
  toolFilesByDevice: {
    [BUTTERBRIDGE_DEVICE_ID]: {
      claude_code: [
        {
          id: "claude-config-1",
          relative_path: "claude/settings.json",
          category: "config",
          content_type: "json",
          title: "settings.json",
          file_size_bytes: 512,
          activity_at: null,
          synced_at: T0,
          device_name: "butterbridge (Windows)",
          message_count: null,
          is_low_activity: null,
          subagent_count: 0,
          is_subagent_orphan: false,
        },
      ],
    },
  },
  projects: [],
};
