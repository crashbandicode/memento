"use client";

import { renderToStaticMarkup } from "react-dom/server";
import { convert } from "@slackfmt/core";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export type ClipboardFormat = "rich" | "markdown" | "slack";

export function isAndroidClipboardHost(): boolean {
  if (typeof navigator === "undefined") return false;
  return /Android/i.test(navigator.userAgent || "");
}

function clipboardMarkdown(markdown: string): string {
  return markdown
    .replace(/<details>\s*<summary>([\s\S]*?)<\/summary>/gi, (_match, summary: string) => {
      const clean = summary.replace(/<strong>([\s\S]*?)<\/strong>/gi, "**$1**");
      return `\n### ${clean}\n`;
    })
    .replace(/<\/details>/gi, "")
    .replace(/<sub>([\s\S]*?)<\/sub>/gi, "$1");
}

async function renderMarkdownHtml(markdown: string): Promise<string> {
  // Detached concurrent roots can still be empty when read on Android. Static
  // rendering is synchronous and produces the exact semantic HTML needed by
  // both the visible copy sheet and the clipboard payload.
  return renderToStaticMarkup(
    <ReactMarkdown remarkPlugins={[remarkGfm]}>
      {clipboardMarkdown(markdown)}
    </ReactMarkdown>,
  );
}

async function richClipboardHtml(markdown: string): Promise<string> {
  const container = document.createElement("div");
  container.innerHTML = await renderMarkdownHtml(markdown);

  const styles: Record<string, Partial<CSSStyleDeclaration>> = {
    h1: { fontSize: "26px", lineHeight: "1.2", margin: "22px 0 12px", color: "#111827" },
    h2: { fontSize: "21px", lineHeight: "1.25", margin: "20px 0 10px", color: "#111827" },
    h3: { fontSize: "17px", lineHeight: "1.3", margin: "16px 0 8px", color: "#1f2937" },
    p: { margin: "8px 0", lineHeight: "1.55" },
    blockquote: { margin: "12px 0", padding: "8px 12px", borderLeft: "3px solid #8b5cf6", background: "#f5f3ff", color: "#374151" },
    pre: { margin: "10px 0", padding: "12px", borderRadius: "8px", background: "#111827", color: "#f9fafb", whiteSpace: "pre-wrap", wordBreak: "break-word" },
    code: { fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace", fontSize: "0.9em" },
    table: { width: "100%", borderCollapse: "collapse", margin: "12px 0" },
    th: { padding: "7px 9px", border: "1px solid #d1d5db", background: "#f3f4f6", textAlign: "left" },
    td: { padding: "7px 9px", border: "1px solid #d1d5db", verticalAlign: "top" },
    hr: { margin: "20px 0", border: "0", borderTop: "1px solid #e5e7eb" },
    a: { color: "#7c3aed" },
  };
  for (const [selector, properties] of Object.entries(styles)) {
    container.querySelectorAll<HTMLElement>(selector).forEach((element) => Object.assign(element.style, properties));
  }
  container.querySelectorAll<HTMLElement>("pre code").forEach((element) => {
    element.style.color = "inherit";
    element.style.background = "transparent";
  });
  return `<article style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.55;color:#1f2937;max-width:760px">${container.innerHTML}</article>`;
}

/** Slack-oriented semantic HTML for paste targets that understand text/html. */
export async function slackClipboardHtml(markdown: string): Promise<string> {
  const container = document.createElement("div");
  container.innerHTML = await renderMarkdownHtml(markdown);

  // Slack cannot represent a paragraph "nakedly" indented beneath a list
  // item. Markdown permits this:
  //
  //   1. Heading
  //
  //      Supporting paragraph
  //
  // Convert every supporting direct paragraph into a nested bullet so Slack
  // preserves the hierarchy instead of flattening or dropping it.
  container.querySelectorAll("li").forEach((item) => {
    const paragraphs = Array.from(item.children).filter(
      (child): child is HTMLParagraphElement => child.tagName === "P",
    );
    for (const paragraph of paragraphs.slice(1)) {
      const nestedList = document.createElement("ul");
      const nestedItem = document.createElement("li");
      nestedItem.innerHTML = paragraph.innerHTML;
      nestedList.appendChild(nestedItem);
      paragraph.replaceWith(nestedList);
    }
  });

  container.querySelectorAll("h1,h2,h3,h4,h5,h6").forEach((heading) => {
    const paragraph = document.createElement("p");
    const strong = document.createElement("b");
    strong.innerHTML = heading.innerHTML;
    paragraph.appendChild(strong);
    heading.replaceWith(paragraph);
  });

  container.querySelectorAll("strong").forEach((el) => {
    const replacement = document.createElement("b");
    replacement.innerHTML = el.innerHTML;
    el.replaceWith(replacement);
  });
  container.querySelectorAll("em").forEach((el) => {
    const replacement = document.createElement("i");
    replacement.innerHTML = el.innerHTML;
    el.replaceWith(replacement);
  });

  container.querySelectorAll("table").forEach((table) => {
    const pre = document.createElement("pre");
    const code = document.createElement("code");
    code.textContent = table.innerText.replace(/\n{3,}/g, "\n\n").trim();
    pre.appendChild(code);
    table.replaceWith(pre);
  });

  container.querySelectorAll("*").forEach((el) => {
    for (const attr of Array.from(el.attributes)) {
      if (attr.name === "href" || attr.name === "start") continue;
      el.removeAttribute(attr.name);
    }
  });

  // Tailwind preflight suppresses list markers globally. Restore them inline
  // so the preview is honest and the browser serializes visible list styles
  // into the HTML clipboard payload Android Slack imports.
  container.querySelectorAll<HTMLOListElement>("ol").forEach((list) => {
    list.style.listStyleType = "decimal";
    list.style.listStylePosition = "outside";
    list.style.paddingLeft = "28px";
    list.style.margin = "7px 0 10px";
  });
  container.querySelectorAll<HTMLUListElement>("ul").forEach((list) => {
    list.style.listStyleType = "disc";
    list.style.listStylePosition = "outside";
    list.style.paddingLeft = "28px";
    list.style.margin = "5px 0";
  });
  container.querySelectorAll<HTMLLIElement>("li").forEach((item) => {
    item.style.display = "list-item";
    item.style.margin = "3px 0";
  });
  container.querySelectorAll<HTMLParagraphElement>("li > p").forEach((paragraph) => {
    paragraph.style.margin = "0 0 5px";
  });

  return `<div>${container.innerHTML}</div>`;
}

function decodeEntityText(value: string): string {
  if (!value.includes("&")) return value;
  const textarea = document.createElement("textarea");
  let decoded = value;
  // Some conversation exports contain already-escaped entities, which the
  // Markdown renderer escapes once more. Decode the bounded pair of layers.
  for (let pass = 0; pass < 4 && decoded.includes("&"); pass += 1) {
    textarea.innerHTML = decoded;
    const next = textarea.value;
    if (next === decoded) break;
    decoded = next;
  }
  return decoded;
}

function inlineSlackText(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) {
    return decodeEntityText(node.textContent || "");
  }
  if (!(node instanceof HTMLElement)) return "";

  const children = () => Array.from(node.childNodes).map(inlineSlackText).join("");
  const tag = node.tagName;
  if (tag === "BR") return "\n";
  if (tag === "A") {
    const label = children().trim();
    const href = node.getAttribute("href") || "";
    if (!href || label === href) return href || label;
    return `${label} (${href})`;
  }
  if (tag === "CODE" && node.parentElement?.tagName !== "PRE") {
    return `\`${decodeEntityText(node.textContent || "")}\``;
  }
  if (tag === "B" || tag === "STRONG") return `*${children()}*`;
  if (tag === "I" || tag === "EM") return `_${children()}_`;
  if (tag === "DEL" || tag === "S") return `~${children()}~`;
  return children();
}

function slackListText(list: HTMLOListElement | HTMLUListElement, depth: number): string {
  const ordered = list.tagName === "OL";
  const start = ordered ? Number.parseInt(list.getAttribute("start") || "1", 10) : 1;
  const items = Array.from(list.children).filter(
    (child): child is HTMLLIElement => child.tagName === "LI",
  );

  return items.map((item, index) => {
    const nestedLists = Array.from(item.children).filter(
      (child): child is HTMLOListElement | HTMLUListElement => (
        child.tagName === "OL" || child.tagName === "UL"
      ),
    );
    const primaryNodes = Array.from(item.childNodes).filter(
      (child) => !(
        child instanceof HTMLElement
        && (child.tagName === "OL" || child.tagName === "UL")
      ),
    );
    const primary = primaryNodes
      .map((child) => (
        child instanceof HTMLElement && child.tagName === "P"
          ? `${Array.from(child.childNodes).map(inlineSlackText).join("")} `
          : inlineSlackText(child)
      ))
      .join("")
      .replace(/\s+/g, " ")
      .trim();
    const indent = "  ".repeat(depth);
    const marker = ordered ? `${start + index}.` : "•";
    const line = `${indent}${marker} ${primary}`.trimEnd();
    const nested = nestedLists.map((child) => slackListText(child, depth + 1));
    return [line, ...nested].filter(Boolean).join("\n");
  }).join("\n");
}

function slackBlockText(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return (node.textContent || "").trim();
  if (!(node instanceof HTMLElement)) return "";

  if (node.tagName === "OL" || node.tagName === "UL") {
    return slackListText(node as HTMLOListElement | HTMLUListElement, 0);
  }
  if (node.tagName === "PRE") {
    return `\`\`\`\n${node.textContent?.replace(/\n+$/, "") || ""}\n\`\`\``;
  }
  if (node.tagName === "BLOCKQUOTE") {
    return Array.from(node.childNodes)
      .map(slackBlockText)
      .join("\n")
      .split("\n")
      .map((line) => `> ${line}`)
      .join("\n");
  }
  if (node.tagName === "HR") return "────────";
  if (node.tagName === "P" || /^H[1-6]$/.test(node.tagName)) {
    return Array.from(node.childNodes).map(inlineSlackText).join("").trim();
  }
  return Array.from(node.childNodes).map(slackBlockText).filter(Boolean).join("\n\n");
}

function slackHtmlToPlainText(html: string): string {
  const container = document.createElement("div");
  container.innerHTML = html;
  return Array.from(container.childNodes)
    .map(slackBlockText)
    .filter(Boolean)
    .join("\n\n")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

/**
 * Readable Android fallback. Slack Android coerces browser clipboard content
 * to text/plain, so preserve hierarchy explicitly instead of losing markers.
 */
export async function slackClipboardPlainText(markdown: string): Promise<string> {
  return slackHtmlToPlainText(await slackClipboardHtml(markdown));
}

function copyWithExecCommand(payload: Record<string, string>): boolean {
  const onCopy = (event: ClipboardEvent) => {
    for (const [mime, value] of Object.entries(payload)) {
      event.clipboardData?.setData(mime, value);
    }
    event.preventDefault();
  };
  document.addEventListener("copy", onCopy);
  try {
    const selection = window.getSelection();
    const probe = document.createElement("span");
    const plain = payload["text/plain"] || " ";
    probe.textContent = plain.slice(0, 1) || " ";
    probe.setAttribute("aria-hidden", "true");
    probe.style.cssText = "position:fixed;left:0;top:0;width:1px;height:1px;opacity:0;pointer-events:none;white-space:pre";
    document.body.appendChild(probe);
    const range = document.createRange();
    range.selectNodeContents(probe);
    selection?.removeAllRanges();
    selection?.addRange(range);
    const ok = document.execCommand("copy");
    selection?.removeAllRanges();
    probe.remove();
    return ok;
  } catch {
    return false;
  } finally {
    document.removeEventListener("copy", onCopy);
  }
}

/**
 * Copy a live DOM subtree the way a user long-press copy would.
 * Android Chrome's ClipboardItem(text/html) is a known silent no-op across apps;
 * native selection copy is what Slack Android actually receives as rich text.
 */
export function copyDomSelection(
  host: HTMLElement,
  opts?: { html?: string; plain?: string },
): boolean {
  const selection = window.getSelection();
  const onCopy = opts?.html
    ? (event: ClipboardEvent) => {
        event.clipboardData?.setData("text/html", opts.html || "");
        event.clipboardData?.setData("text/plain", opts.plain || host.innerText || "");
        event.preventDefault();
      }
    : null;

  if (onCopy) document.addEventListener("copy", onCopy);
  try {
    const range = document.createRange();
    range.selectNodeContents(host);
    selection?.removeAllRanges();
    selection?.addRange(range);
    if (typeof host.focus === "function") {
      try { host.focus({ preventScroll: true } as FocusOptions); } catch { host.focus(); }
    }
    // First try: let the browser serialize the selected HTML itself (no preventDefault).
    if (!onCopy) {
      return document.execCommand("copy");
    }
    // Second try path when caller provides explicit payloads.
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    if (onCopy) document.removeEventListener("copy", onCopy);
    selection?.removeAllRanges();
  }
}

function copyHtmlViaOffscreenSelection(html: string, plain: string): boolean {
  const host = document.createElement("div");
  host.setAttribute("contenteditable", "true");
  host.setAttribute("aria-hidden", "true");
  host.innerHTML = html;
  // Avoid opacity:0 — Android Chrome often skips "invisible" copies.
  host.style.cssText = [
    "position:fixed",
    "left:0",
    "top:0",
    "width:min(92vw, 420px)",
    "height:auto",
    "max-height:40vh",
    "overflow:auto",
    "opacity:0.01",
    "z-index:2147483646",
    "pointer-events:none",
    "background:#fff",
    "color:#111",
    "padding:8px",
    "white-space:normal",
  ].join(";");
  document.body.appendChild(host);
  try {
    // Prefer native HTML serialization from the selection (no clipboardData override).
    if (copyDomSelection(host)) return true;
    // Fallback: force both MIME types in the copy event.
    return copyDomSelection(host, { html, plain });
  } finally {
    host.remove();
  }
}

async function writePlainClipboard(text: string): Promise<void> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
  } catch {
    // Permissions-Policy can reject writeText even when the method exists.
  }
  if (!copyWithExecCommand({ "text/plain": text })) {
    throw new Error("Clipboard write was blocked by the browser.");
  }
}

export async function copySlackPlainTextToClipboard(markdown: string): Promise<string> {
  const text = await slackClipboardPlainText(markdown);
  await writePlainClipboard(text);
  return text;
}

async function copySlackClipboard(markdown: string): Promise<boolean> {
  const plain = clipboardMarkdown(markdown);
  const html = await slackClipboardHtml(markdown);

  let slackDelta: string | null = null;
  try {
    slackDelta = await convert(plain, { format: "markdown" });
  } catch {
    slackDelta = null;
  }

  // Android Chrome/Slack coercively expose only text/plain across apps. Use a
  // fallback that keeps explicit numbers, bullets, URLs, and code markers.
  if (isAndroidClipboardHost()) {
    try {
      await writePlainClipboard(slackHtmlToPlainText(html));
      return true;
    } catch {
      return false;
    }
  }

  const payload: Record<string, string> = {
    "text/plain": plain,
    "text/html": html,
  };
  if (slackDelta) payload["slack/texty"] = slackDelta;

  if (copyWithExecCommand(payload)) return true;

  if (typeof ClipboardItem !== "undefined" && navigator.clipboard?.write) {
    try {
      await navigator.clipboard.write([new ClipboardItem({
        "text/plain": new Blob([plain], { type: "text/plain" }),
        "text/html": new Blob([html], { type: "text/html" }),
      })]);
      return true;
    } catch {
      // Fall through.
    }
  }

  return copyHtmlViaOffscreenSelection(html, plain);
}

/** Copy Markdown directly, as styled HTML, or as Slack-native rich text. */
export async function copyMarkdownToClipboard(
  markdown: string,
  format: ClipboardFormat,
): Promise<ClipboardFormat> {
  if (format === "markdown") {
    await writePlainClipboard(markdown);
    return "markdown";
  }

  if (format === "slack") {
    if (await copySlackClipboard(markdown)) {
      return "slack";
    }
    await writePlainClipboard(clipboardMarkdown(markdown));
    return "markdown";
  }

  let html: string | null = null;
  try {
    html = await richClipboardHtml(markdown);
  } catch {
    html = null;
  }

  if (html && typeof ClipboardItem !== "undefined" && navigator.clipboard?.write) {
    try {
      await navigator.clipboard.write([new ClipboardItem({
        "text/plain": new Blob([markdown], { type: "text/plain" }),
        "text/html": new Blob([html], { type: "text/html" }),
      })]);
      return "rich";
    } catch {
      // Fall through.
    }
  }

  if (html && copyWithExecCommand({ "text/plain": markdown, "text/html": html })) {
    return "rich";
  }

  if (html && copyHtmlViaOffscreenSelection(html, markdown)) {
    return "rich";
  }

  await writePlainClipboard(markdown);
  return "markdown";
}
