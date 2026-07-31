import {
  isValidElement,
  useMemo,
  useState,
  type ComponentPropsWithoutRef,
  type ReactNode,
} from "react";
import {
  FiArrowRight,
  FiBook,
  FiBox,
  FiDatabase,
  FiExternalLink,
  FiFile,
  FiFileText,
  FiFolder,
  FiGlobe,
  FiGrid,
  FiImage,
  FiLink,
  FiLock,
  FiPackage,
  FiSettings,
  FiTerminal,
} from "react-icons/fi";
import { SiGithub, SiGitlab } from "react-icons/si";
import {
  classifyInlineCode,
  classifySmartLink,
} from "@/lib/smart-link-classifier.mjs";
import { canvasArtifactFromLink } from "@/lib/canvas-artifact.mjs";
import { useCanvasArtifactResolver } from "@/lib/canvas-context";
import { fileIconKind } from "@/lib/file-icon.mjs";
import { useI18n } from "@/lib/i18n";
import { CanvasViewer } from "./CanvasViewer";
import styles from "./SmartLink.module.css";

type MarkdownAnchorProps = ComponentPropsWithoutRef<"a"> & { node?: unknown };
type MarkdownCodeProps = ComponentPropsWithoutRef<"code"> & { node?: unknown };

function nodeText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return nodeText(node.props.children);
  return "";
}

function providerIcon(provider: "github" | "gitlab") {
  return provider === "gitlab" ? <SiGitlab size={14} /> : <SiGithub size={14} />;
}

type FileIconKind = ReturnType<typeof fileIconKind>;

// Programming / markup languages get a distinctive letter monogram so related
// kinds are separated by *symbol* first — color is only a secondary cue, never
// the sole identifier. Conceptual / binary kinds keep pictographic line icons.
const FILE_ICON_MONOGRAMS: Partial<Record<FileIconKind, string>> = {
  typescript: "TS",
  javascript: "JS",
  python: "PY",
  rust: "RS",
  go: "GO",
  java: "JV",
  kotlin: "KT",
  csharp: "C#",
  native: "C",
  ruby: "RB",
  php: "PHP",
  lua: "LU",
  swift: "SW",
  vue: "VUE",
  html: "<>",
  stylesheet: "CSS",
  markdown: "MD",
  json: "{ }",
  xml: "XML",
  protobuf: "PB",
  pdf: "PDF",
};

function FileTypeIcon({ kind, size }: { kind: FileIconKind; size: number }) {
  const monogram = FILE_ICON_MONOGRAMS[kind];
  if (monogram) {
    return (
      <span
        className={[styles.icon, styles.fileTypeIcon, styles.monogram].join(" ")}
        data-file-icon={kind}
        aria-hidden="true"
      >
        {monogram}
      </span>
    );
  }

  let glyph: ReactNode;
  switch (kind) {
    case "package":
      glyph = <FiPackage size={size} />;
      break;
    case "docker":
      glyph = <FiBox size={size} />;
      break;
    case "config":
      glyph = <FiSettings size={size} />;
      break;
    case "shell":
    case "build":
      glyph = <FiTerminal size={size} />;
      break;
    case "sql":
    case "data":
      glyph = <FiDatabase size={size} />;
      break;
    case "image":
      glyph = <FiImage size={size} />;
      break;
    case "lock":
      glyph = <FiLock size={size} />;
      break;
    case "notebook":
      glyph = <FiBook size={size} />;
      break;
    case "directory":
      glyph = <FiFolder size={size} />;
      break;
    case "text":
      glyph = <FiFileText size={size} />;
      break;
    case "file":
    default:
      glyph = <FiFile size={size} />;
      break;
  }

  return (
    <span
      className={[styles.icon, styles.fileTypeIcon].join(" ")}
      data-file-icon={kind}
      aria-hidden="true"
    >
      {glyph}
    </span>
  );
}

function fileLabelParts(label: string): {
  label: string;
  additions?: string;
  deletions?: string;
} {
  const match = label.match(/^(.*?)(?:\s+\(?\+(\d+)\s+-(\d+)\)?)$/);
  return match
    ? { label: match[1].trim(), additions: match[2], deletions: match[3] }
    : { label };
}

function CanvasChip({
  href,
  label,
  name,
  path,
  inline = false,
}: {
  href: string;
  label: string;
  name: string;
  path: string;
  inline?: boolean;
}) {
  const { t } = useI18n();
  const resolve = useCanvasArtifactResolver();
  const [open, setOpen] = useState(false);

  const artifact = useMemo(() => {
    const resolved = resolve(path) || resolve(href) || resolve(name);
    return resolved ?? canvasArtifactFromLink(href, label || name);
  }, [resolve, path, href, name, label]);

  const openViewer = (event: React.MouseEvent) => {
    // Left-click opens the in-app viewer; modifier/middle clicks keep native
    // link behavior (open the target location in a new tab where possible).
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
    event.preventDefault();
    setOpen(true);
  };

  const shared = {
    className: [styles.link, styles.canvas].filter(Boolean).join(" "),
    "data-testid": inline ? "smart-code-canvas" : "smart-link-canvas",
    "data-canvas-name": name,
    "aria-haspopup": "dialog" as const,
    title: path || name,
    children: (
      <>
        <span className={styles.icon} aria-hidden="true"><FiGrid size={13} /></span>
        <span className={styles.canvasTag}>{t.conversation.canvas.chip}</span>
        <span className={styles.label}>{label || name}</span>
      </>
    ),
  };

  return (
    <>
      {href ? (
        <a {...shared} href={href} onClick={openViewer} />
      ) : (
        <button
          {...shared}
          type="button"
          onClick={() => setOpen(true)}
        />
      )}
      <CanvasViewer artifact={artifact} open={open} onClose={() => setOpen(false)} />
    </>
  );
}

export function SmartLink({
  node: _node,
  children,
  href = "",
  className,
  target,
  rel,
  title,
  ...props
}: MarkdownAnchorProps) {
  void _node;
  const rawLabel = nodeText(children);
  const fileParts = fileLabelParts(rawLabel);
  const info = classifySmartLink(href, fileParts.label);
  const external = /^https?:\/\//i.test(href);
  const shared = {
    ...props,
    href,
    target: target ?? (external ? "_blank" : undefined),
    rel: rel ?? (external ? "noreferrer noopener" : undefined),
  };

  if (info.kind === "canvas") {
    return (
      <CanvasChip
        href={info.href}
        label={info.label}
        name={info.name}
        path={info.path}
      />
    );
  }

  if (info.kind === "file") {
    const fileType = fileIconKind(info.path || info.label);
    return (
      <a
        {...shared}
        className={[styles.link, className].filter(Boolean).join(" ")}
        data-testid="smart-link-file"
        data-file-type={fileType}
        title={title ?? info.path}
      >
        <FileTypeIcon kind={fileType} size={14} />
        <span className={styles.label}>{info.label}</span>
        {fileParts.additions !== undefined && fileParts.deletions !== undefined && (
          <span className={styles.fileStats} data-testid="smart-file-stats" aria-label={`${fileParts.additions} additions, ${fileParts.deletions} deletions`}>
            <span className={styles.added}>+{fileParts.additions}</span>
            <span className={styles.removed}>-{fileParts.deletions}</span>
          </span>
        )}
        {external && <FiExternalLink className={styles.arrow} size={11} aria-hidden="true" />}
      </a>
    );
  }

  if (info.kind === "git-compare") {
    return (
      <a
        {...shared}
        className={[
          styles.link,
          styles.provider,
          info.provider === "gitlab" ? styles.providerGitlab : styles.providerGithub,
          className,
        ].filter(Boolean).join(" ")}
        data-testid="smart-link-git-compare"
        data-provider={info.provider}
        title={title ?? `${info.label} · ${info.refs[0]} → ${info.refs[1]} · ${info.domain}`}
      >
        <span className={styles.icon} aria-hidden="true">{providerIcon(info.provider)}</span>
        <span className={styles.label}>{info.label}</span>
        <span className={styles.ref} data-testid="smart-ref-pill">{info.refs[0]}</span>
        <FiArrowRight className={styles.arrow} size={12} aria-hidden="true" />
        <span className={styles.ref} data-testid="smart-ref-pill">{info.refs[1]}</span>
      </a>
    );
  }

  if (info.kind === "git-commit") {
    return (
      <a
        {...shared}
        className={[
          styles.link,
          styles.provider,
          info.provider === "gitlab" ? styles.providerGitlab : styles.providerGithub,
          className,
        ].filter(Boolean).join(" ")}
        data-testid="smart-link-git-commit"
        data-provider={info.provider}
        title={title ?? `${info.label} · ${info.ref} · ${info.domain}`}
      >
        <span className={styles.icon} aria-hidden="true">{providerIcon(info.provider)}</span>
        <span className={styles.label}>{info.label}</span>
        <span className={styles.ref} data-testid="smart-ref-pill">{info.ref}</span>
      </a>
    );
  }

  if (info.kind === "web") {
    const showDomain = info.label.toLowerCase() !== info.domain.toLowerCase();
    return (
      <a
        {...shared}
        className={[styles.link, className].filter(Boolean).join(" ")}
        data-testid="smart-link-web"
        title={title ?? href}
      >
        <span className={styles.icon} aria-hidden="true"><FiGlobe size={13} /></span>
        <span className={styles.label}>{info.label}</span>
        {showDomain && (
          <span className={styles.domain}>
            {info.domain}
            <FiExternalLink size={10} aria-hidden="true" />
          </span>
        )}
      </a>
    );
  }

  return (
    <a
      {...shared}
      className={[styles.link, className].filter(Boolean).join(" ")}
      data-testid="smart-link"
      title={title ?? href}
    >
      <span className={styles.icon} aria-hidden="true"><FiLink size={13} /></span>
      <span className={styles.label}>{children}</span>
    </a>
  );
}

export function SmartCode({
  node: _node,
  children,
  className,
  ...props
}: MarkdownCodeProps) {
  void _node;
  const value = nodeText(children).replace(/\n$/, "");
  const info = classifyInlineCode(value);

  if (className) {
    return <code {...props} className={className}>{children}</code>;
  }

  if (info.kind === "plain") {
    return (
      <code
        {...props}
        className={styles.inlineCode}
        data-testid="inline-code"
      >
        {children}
      </code>
    );
  }

  if (info.kind === "sha") {
    return (
      <span
        className={[styles.inlineChip, styles.inlineSha].join(" ")}
        data-testid="smart-code-sha"
        title={info.value}
      >
        {info.display}
      </span>
    );
  }

  if (info.kind === "canvas") {
    return <CanvasChip href="" label={info.display} name={info.display} path={info.path} inline />;
  }

  const fileType = fileIconKind(info.value);
  return (
    <span
      className={[styles.inlineChip, styles.inlineFile].join(" ")}
      data-testid="smart-code-file"
      data-file-type={fileType}
      title={info.value}
    >
      <FileTypeIcon kind={fileType} size={12} />
      <span className={styles.label}>{info.display}</span>
    </span>
  );
}
