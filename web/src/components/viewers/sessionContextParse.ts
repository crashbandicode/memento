export type ContextUsageCategory = {
  name: string;
  tokens: string;
  percentage: number;
};

export type ContextMcpTool = {
  tool: string;
  server: string;
  tokens: string;
};

export type ContextUsageReport = {
  categories: ContextUsageCategory[];
  mcpTools: ContextMcpTool[];
  modelLabel?: string;
  modelId?: string;
  totalLabel?: string;
  suggestion?: { title: string; detail?: string };
  remainder?: string;
};

function parsePercentage(raw: string): number {
  const match = String(raw || "").replace(/,/g, "").match(/(-?\d+(?:\.\d+)?)\s*%?/);
  return match ? Math.max(0, Number(match[1])) : 0;
}

function splitMarkdownTables(content: string): Array<{ heading: string; headers: string[]; rows: string[][] }> {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const tables: Array<{ heading: string; headers: string[]; rows: string[][] }> = [];
  let heading = "";
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    const headingMatch = line.match(/^#{1,6}\s+(.+?)\s*$/);
    if (headingMatch) {
      heading = headingMatch[1].trim();
      i += 1;
      continue;
    }

    if (!/^\|.+\|/.test(line.trim())) {
      i += 1;
      continue;
    }

    const headerCells = line
      .trim()
      .replace(/^\|/, "")
      .replace(/\|$/, "")
      .split("|")
      .map((cell) => cell.trim());
    const next = lines[i + 1] || "";
    if (!/^\|?\s*:?-{3,}/.test(next.trim())) {
      i += 1;
      continue;
    }

    i += 2;
    const rows: string[][] = [];
    while (i < lines.length && /^\|.+\|/.test(lines[i].trim())) {
      rows.push(
        lines[i]
          .trim()
          .replace(/^\|/, "")
          .replace(/\|$/, "")
          .split("|")
          .map((cell) => cell.trim()),
      );
      i += 1;
    }
    tables.push({ heading, headers: headerCells, rows });
    heading = "";
  }

  return tables;
}

function findColumn(headers: string[], ...names: string[]): number {
  const normalized = headers.map((h) => h.toLowerCase());
  for (const name of names) {
    const idx = normalized.findIndex((h) => h === name || h.includes(name));
    if (idx >= 0) return idx;
  }
  return -1;
}

export function parseContextUsageReport(content: string): ContextUsageReport | null {
  const text = content.trim();
  if (!text) return null;

  const looksLikeUsage =
    /Estimated usage by category/i.test(text)
    || (/Free space/i.test(text) && /Messages/i.test(text) && /\|\s*Category\s*\|/i.test(text));
  if (!looksLikeUsage) return null;

  const tables = splitMarkdownTables(text);
  const categories: ContextUsageCategory[] = [];
  const mcpTools: ContextMcpTool[] = [];

  for (const table of tables) {
    const catIdx = findColumn(table.headers, "category");
    const tokenIdx = findColumn(table.headers, "tokens", "token");
    const pctIdx = findColumn(table.headers, "percentage", "percent", "%");
    const toolIdx = findColumn(table.headers, "tool");
    const serverIdx = findColumn(table.headers, "server");

    if (catIdx >= 0 && (tokenIdx >= 0 || pctIdx >= 0)) {
      for (const row of table.rows) {
        const name = row[catIdx]?.trim();
        if (!name) continue;
        categories.push({
          name,
          tokens: (tokenIdx >= 0 ? row[tokenIdx] : "") || "—",
          percentage: pctIdx >= 0 ? parsePercentage(row[pctIdx] || "0") : 0,
        });
      }
      continue;
    }

    if (toolIdx >= 0) {
      for (const row of table.rows) {
        const tool = row[toolIdx]?.trim();
        if (!tool) continue;
        mcpTools.push({
          tool,
          server: (serverIdx >= 0 ? row[serverIdx] : "") || "",
          tokens: (tokenIdx >= 0 ? row[tokenIdx] : "") || "0",
        });
      }
    }
  }

  if (categories.length === 0) return null;

  const labeledModel = text.match(/\*\*Model:\*\*\s*([^\n*]+)/i)?.[1]?.trim();
  const labeledTokens = text.match(/\*\*Tokens:\*\*\s*([^\n*]+)/i)?.[1]?.trim();
  const modelMatch = text.match(
    /(?:^|\n)\s*(?:#{1,3}\s*)?(Opus[\w.\s-]*|Sonnet[\w.\s-]*|Haiku[\w.\s-]*)\s*\n\s*`?([a-z0-9._-]+)`?\s*\n\s*([0-9.]+\s*[km]?\/[0-9.]+\s*[km]?\s*tokens?\s*\(\d+(?:\.\d+)?%\))/im,
  );
  const totalMatch = text.match(
    /([0-9.]+\s*[km]?\s*\/\s*[0-9.]+\s*[km]?\s*(?:tokens?)?\s*\(\d+(?:\.\d+)?%\))/i,
  );

  const modelId = labeledModel || modelMatch?.[2]?.trim();
  const totalLabel = labeledTokens || modelMatch?.[3]?.trim() || totalMatch?.[1]?.trim();
  const modelLabel = modelMatch?.[1]?.trim() || prettyModelLabel(modelId);

  let suggestion: ContextUsageReport["suggestion"];
  const fileReads = text.match(/File reads using[^\n]+/i)?.[0]?.trim();
  if (fileReads) {
    suggestion = {
      title: fileReads,
      detail: text.match(/If you are re-reading files[^\n]*/i)?.[0]?.trim(),
    };
  } else {
    const lines = text.split(/\n/);
    for (let i = 0; i < lines.length; i += 1) {
      const line = lines[i].trim();
      if (!/^(?:ℹ️|i\b|Suggestions?)\b/i.test(line)) continue;
      const title = line.replace(/^(?:ℹ️|i\b|Suggestions?)\s*[:.\-]?\s*/i, "").trim() || lines[i + 1]?.trim();
      const detail = title === lines[i + 1]?.trim() ? lines[i + 2]?.trim() : lines[i + 1]?.trim();
      if (title) {
        suggestion = { title, detail: detail && detail !== title ? detail : undefined };
      }
      break;
    }
  }

  const remainderParts: string[] = [];
  for (const block of text.split(/\n(?=#{1,6}\s)/)) {
    const trimmed = block.trim();
    if (!trimmed) continue;
    if (/Estimated usage by category/i.test(trimmed)) continue;
    if (/^#{1,6}\s*MCP\s+Tools?/i.test(trimmed) && mcpTools.length > 0) continue;
    if (/^#{1,6}\s*(Context Usage|\/context)/i.test(trimmed)) continue;
    if (trimmed.startsWith("|") && /Category|Tool/i.test(trimmed)) continue;
    if (/File reads using/i.test(trimmed)) continue;
    remainderParts.push(trimmed);
  }

  return {
    categories,
    mcpTools,
    modelLabel,
    modelId,
    totalLabel,
    suggestion,
    remainder: remainderParts.length ? remainderParts.join("\n\n") : undefined,
  };
}

function prettyModelLabel(modelId: string | undefined): string | undefined {
  if (!modelId) return undefined;
  const opus = modelId.match(/opus[-_]?(\d+(?:\.\d+)?)/i);
  if (opus) return `Opus ${opus[1]}`;
  const sonnet = modelId.match(/sonnet[-_]?(\d+(?:\.\d+)?)/i);
  if (sonnet) return `Sonnet ${sonnet[1]}`;
  const haiku = modelId.match(/haiku[-_]?(\d+(?:\.\d+)?)/i);
  if (haiku) return `Haiku ${haiku[1]}`;
  return modelId;
}

export function looksLikeMarkdownContext(content: string): boolean {
  const text = content.trim();
  if (!text) return false;
  if (/^#{1,6}\s+\S/m.test(text)) return true;
  if (/^\|.+\|\s*$/m.test(text) && /^\|?\s*:?-{3,}/m.test(text)) return true;
  if (/^```/m.test(text)) return true;
  if (/^\*\*[^*]+\*\*/m.test(text)) return true;
  return false;
}

export function contextUsageSummary(content: string): string | null {
  const report = parseContextUsageReport(content);
  if (!report) return null;
  const used = report.categories
    .filter((c) => !/free space/i.test(c.name))
    .reduce((sum, c) => sum + c.percentage, 0);
  const messages = report.categories.find((c) => /messages/i.test(c.name));
  const parts = [`Context usage · ${used.toFixed(used >= 10 ? 0 : 1)}%`];
  if (messages) parts.push(`messages ${messages.percentage.toFixed(0)}%`);
  if (report.mcpTools.length) parts.push(`${report.mcpTools.length} MCP tools`);
  return parts.join(" · ");
}
