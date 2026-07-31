/**
 * @typedef {
 *   "file" | "text" | "markdown" | "typescript" | "javascript" | "python"
 *   | "rust" | "go" | "java" | "kotlin" | "csharp" | "native" | "ruby"
 *   | "php" | "lua" | "swift" | "html" | "stylesheet" | "vue" | "json"
 *   | "data" | "config" | "shell" | "sql" | "image" | "package"
 *   | "docker" | "lock" | "protobuf" | "xml" | "build"
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
  const basename = fileBasename(value);

  if (
    /^(?:package(?:-lock)?\.json|npm-shrinkwrap\.json|pnpm-lock\.ya?ml|yarn\.lock|bun\.lockb?)$/.test(basename)
    || /^(?:cargo\.(?:toml|lock)|composer\.json|go\.(?:mod|sum)|pom\.xml|pyproject\.toml)$/.test(basename)
    || /^(?:requirements(?:\.[\w.-]+)?\.txt|gemfile(?:\.lock)?)$/.test(basename)
  ) {
    return "package";
  }
  if (/^(?:dockerfile(?:\.[\w.-]+)?|(?:docker-)?compose(?:\.[\w.-]+)?\.ya?ml)$/.test(basename)) {
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
    case "ts":
    case "tsx":
    case "mts":
    case "cts":
      return "typescript";
    case "js":
    case "jsx":
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
    case "csv":
      return "data";
    case "yaml":
    case "yml":
    case "toml":
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
