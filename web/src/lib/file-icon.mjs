/**
 * @typedef {
 *   "file" | "text" | "markdown" | "typescript" | "javascript" | "react"
 *   | "python" | "rust" | "go" | "java" | "kotlin" | "csharp" | "native"
 *   | "ruby" | "php" | "lua" | "swift" | "html" | "stylesheet" | "vue"
 *   | "json" | "yaml" | "toml" | "data" | "config" | "shell" | "sql"
 *   | "image" | "package" | "docker" | "lock" | "protobuf" | "xml" | "build"
 *   | "pdf" | "notebook" | "directory"
 * } FileIconKind
 */

function safeDecode(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function fileBasename(value) {
  const normalized = safeDecode(value)
    .trim()
    .replace(/^file:\/\//i, "")
    .split("#", 1)[0]
    .split("?", 1)[0]
    .replace(/[\\/]+$/, "");
  return (normalized.split(/[\\/]/).at(-1) || normalized).toLowerCase();
}

/**
 * Pick a semantic icon kind from the whole basename before falling back to the
 * extension. Basename overrides keep package manifests, Dockerfiles, and build
 * files visually distinct from ordinary JSON, YAML, or text files.
 *
 * @param {string} value
 * @returns {FileIconKind}
 */
export function fileIconKind(value) {
  const bare = safeDecode(String(value))
    .trim()
    .replace(/^file:\/\//i, "")
    .split("#", 1)[0]
    .split("?", 1)[0];
  // A trailing separator (e.g. `src/components/`) marks a directory reference.
  if (/[/\\]$/.test(bare) && bare.replace(/[/\\]+$/, "")) {
    return "directory";
  }

  const basename = fileBasename(value);

  if (
    /^(?:package(?:-lock)?\.json|npm-shrinkwrap\.json|pnpm-lock\.ya?ml|yarn\.lock|bun\.lockb?)$/.test(basename)
    || /^(?:cargo\.(?:toml|lock)|composer\.json|go\.(?:mod|sum)|pom\.xml|pyproject\.toml)$/.test(basename)
    || /^(?:requirements(?:\.[\w.-]+)?\.txt|gemfile(?:\.lock)?)$/.test(basename)
  ) {
    return "package";
  }
  if (/^(?:(?:docker|container)file(?:\.[\w.-]+)?|(?:docker-)?compose(?:\.[\w.-]+)?\.ya?ml)$/.test(basename)) {
    return "docker";
  }
  if (/^tsconfig(?:\.[\w.-]+)?\.json$/.test(basename)) return "typescript";
  if (/^jsconfig(?:\.[\w.-]+)?\.json$/.test(basename)) return "javascript";
  if (/^(?:readme|agents|claude|handoff)(?:\.[\w.-]+)?$/.test(basename)) return "markdown";
  if (/^(?:license|notice|authors)(?:\.[\w.-]+)?$/.test(basename)) return "text";
  if (/^(?:makefile|gnumakefile|cmakelists\.txt|procfile|tiltfile|rakefile)$/.test(basename)) {
    return "build";
  }
  if (/^\.(?:env(?:\..+)?|editorconfig|gitignore|dockerignore|npmrc|yarnrc)$/.test(basename)) {
    return "config";
  }

  const extension = basename.includes(".") ? basename.split(".").at(-1) : "";
  switch (extension) {
    // `.tsx`/`.jsx` are React components; give them the React mark so a JSX view
    // reads as React, not as plain TS/JS. (`*.canvas.tsx` never reaches here — it
    // is classified as the dedicated Canvas chip before the file kind.)
    case "tsx":
    case "jsx":
      return "react";
    case "ts":
    case "mts":
    case "cts":
      return "typescript";
    case "js":
    case "mjs":
    case "cjs":
      return "javascript";
    case "py":
      return "python";
    case "rs":
      return "rust";
    case "go":
      return "go";
    case "java":
      return "java";
    case "kt":
    case "kts":
      return "kotlin";
    case "cs":
      return "csharp";
    case "c":
    case "cc":
    case "cpp":
    case "h":
    case "hpp":
      return "native";
    case "rb":
      return "ruby";
    case "php":
      return "php";
    case "lua":
      return "lua";
    case "swift":
      return "swift";
    case "html":
    case "htm":
      return "html";
    case "css":
    case "scss":
      return "stylesheet";
    case "vue":
      return "vue";
    case "md":
    case "mdx":
      return "markdown";
    case "json":
    case "jsonl":
      return "json";
    case "ipynb":
      return "notebook";
    case "pdf":
      return "pdf";
    case "csv":
      return "data";
    case "yaml":
    case "yml":
      return "yaml";
    case "toml":
      return "toml";
    case "ini":
    case "plist":
    case "properties":
      return "config";
    case "sh":
    case "bash":
    case "zsh":
    case "fish":
    case "ps1":
      return "shell";
    case "sql":
      return "sql";
    case "svg":
    case "png":
    case "jpg":
    case "jpeg":
    case "gif":
    case "webp":
      return "image";
    case "lock":
      return "lock";
    case "proto":
      return "protobuf";
    case "xml":
      return "xml";
    case "log":
    case "txt":
      return "text";
    default:
      return "file";
  }
}

/**
 * Human-readable type label for every {@link FileIconKind}. Used as the icon's
 * accessible name (`role="img"` / `aria-label`) so assistive tech announces the
 * *type* — never a two-letter monogram — alongside the unchanged filename text.
 *
 * @type {Record<FileIconKind, string>}
 */
export const FILE_ICON_LABELS = {
  file: "File",
  text: "Text file",
  markdown: "Markdown file",
  typescript: "TypeScript file",
  javascript: "JavaScript file",
  react: "React component",
  python: "Python file",
  rust: "Rust file",
  go: "Go file",
  java: "Java file",
  kotlin: "Kotlin file",
  csharp: "C# file",
  native: "C / C++ file",
  ruby: "Ruby file",
  php: "PHP file",
  lua: "Lua file",
  swift: "Swift file",
  html: "HTML file",
  stylesheet: "Stylesheet",
  vue: "Vue component",
  json: "JSON file",
  yaml: "YAML file",
  toml: "TOML file",
  data: "Data file",
  config: "Config file",
  shell: "Shell script",
  sql: "SQL / database file",
  image: "Image file",
  package: "Package manifest",
  docker: "Docker / container file",
  lock: "Lock file",
  protobuf: "Protocol Buffers file",
  xml: "XML file",
  build: "Build file",
  pdf: "PDF document",
  notebook: "Jupyter notebook",
  directory: "Directory",
};

/**
 * Every {@link FileIconKind}, derived from the label map so the two never drift.
 * Tests iterate this to assert full icon/label coverage.
 *
 * @type {FileIconKind[]}
 */
export const FILE_ICON_KINDS = /** @type {FileIconKind[]} */ (
  Object.keys(FILE_ICON_LABELS)
);

/**
 * Accessible type label for a kind, with a safe fallback for unknown values.
 *
 * @param {FileIconKind | string} kind
 * @returns {string}
 */
export function fileIconLabel(kind) {
  return FILE_ICON_LABELS[/** @type {FileIconKind} */ (kind)] ?? FILE_ICON_LABELS.file;
}
