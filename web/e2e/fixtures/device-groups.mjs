// @ts-check

const tool = (id, file_count) => ({ id, file_count });

function identity({
  index,
  host,
  platform,
  count,
  tools,
}) {
  const stem = `${host.replaceAll("-", "")}-${platform.toLowerCase()}-${index}`;
  return {
    id: `machine-${stem}`,
    device_id: `collector-${stem}`,
    name: `${host} (${platform})`,
    platform,
    label: `${platform} · ${platform.toLowerCase().slice(0, 3)}-${String(index).padStart(2, "0")}`,
    last_heartbeat: "2026-07-31T23:00:00.000Z",
    total_files: count,
    tools,
  };
}

export const butterbridgeIdentities = [
  identity({
    index: 1,
    host: "butterbridge",
    platform: "Linux",
    count: 1,
    tools: [tool("claude_code", 1)],
  }),
  identity({
    index: 2,
    host: "butterbridge",
    platform: "Windows",
    count: 0,
    tools: [],
  }),
  identity({
    index: 3,
    host: "butterbridge",
    platform: "Windows",
    count: 0,
    tools: [],
  }),
  identity({
    index: 4,
    host: "butterbridge",
    platform: "Windows",
    count: 0,
    tools: [],
  }),
  identity({
    index: 5,
    host: "butterbridge",
    platform: "Windows",
    count: 648,
    tools: [
      tool("claude_code", 48),
      tool("codex", 100),
      tool("cursor", 500),
    ],
  }),
  identity({
    index: 6,
    host: "butterbridge",
    platform: "Windows",
    count: 0,
    tools: [],
  }),
];

export const dreamlandIdentities = [
  identity({
    index: 1,
    host: "dreamland-yoga",
    platform: "Linux",
    count: 1306,
    tools: [
      tool("claude_code", 206),
      tool("codex", 300),
      tool("cursor", 800),
    ],
  }),
  identity({
    index: 2,
    host: "dreamland-yoga",
    platform: "Windows",
    count: 2778,
    tools: [
      tool("claude_code", 478),
      tool("codex", 700),
      tool("cursor", 1600),
    ],
  }),
];

export const deviceGroups = [
  {
    id: "host_butterbridge",
    group_id: "host_butterbridge",
    device_id: "host_butterbridge",
    name: "butterbridge",
    total_files: 649,
    tools: [
      tool("claude_code", 49),
      tool("codex", 100),
      tool("cursor", 500),
    ],
    machine_ids: butterbridgeIdentities.map((item) => item.id),
    device_ids: butterbridgeIdentities.map((item) => item.device_id),
    identities: butterbridgeIdentities,
  },
  {
    id: "host_dreamland_yoga",
    group_id: "host_dreamland_yoga",
    device_id: "host_dreamland_yoga",
    name: "dreamland-yoga",
    total_files: 4084,
    tools: [
      tool("claude_code", 684),
      tool("codex", 1000),
      tool("cursor", 2400),
    ],
    machine_ids: dreamlandIdentities.map((item) => item.id),
    device_ids: dreamlandIdentities.map((item) => item.device_id),
    identities: dreamlandIdentities,
  },
];

export const rawDevices = [...butterbridgeIdentities, ...dreamlandIdentities].map(
  (item) => ({
    id: item.id,
    device_id: item.device_id,
    name: item.name,
    last_heartbeat: item.last_heartbeat,
    created_at: "2026-07-07T12:00:00.000Z",
    document_count: item.total_files,
    tools: item.tools.map((entry) => entry.id),
  }),
);

export const groupedCursorFiles = [
  {
    id: "conversation-windows",
    title: "Windows conversation included",
    relative_path: "sessions/windows.jsonl",
    category: "conversation",
    content_type: "jsonl",
    file_size_bytes: 1024,
    activity_at: "2026-07-31T22:00:00.000Z",
    synced_at: "2026-07-31T22:00:00.000Z",
    message_count: 8,
    is_low_activity: false,
  },
  {
    id: "conversation-wsl",
    title: "WSL conversation included",
    relative_path: "sessions/wsl.jsonl",
    category: "conversation",
    content_type: "jsonl",
    file_size_bytes: 2048,
    activity_at: "2026-07-31T22:01:00.000Z",
    synced_at: "2026-07-31T22:01:00.000Z",
    message_count: 12,
    is_low_activity: false,
  },
];
