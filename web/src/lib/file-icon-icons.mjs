/**
 * Single source of truth mapping every {@link import("./file-icon.mjs").FileIconKind}
 * to a real, recognizable vector icon component from the existing (tree-shakeable)
 * `react-icons` dependency — never a two-letter text monogram.
 *
 * Two icon families are used, both already shipped by `react-icons`:
 *   - Simple Icons (`Si*`) for languages/formats with an established brand logo
 *     (Python, TypeScript, React, Rust, Docker, …) so the recognizable silhouette
 *     identifies the type at a glance.
 *   - Feather (`Fi*`) conceptual pictograms (gear, terminal, database, folder,
 *     code brackets, document, …) for kinds without an established brand mark.
 *
 * The renderer (`SmartLink`/`SmartCode`) and the unit tests both import this map,
 * so the drawn glyph and the asserted metadata can never drift apart.
 */
import {
  FiCode,
  FiDatabase,
  FiFile,
  FiFileText,
  FiFolder,
  FiGrid,
  FiImage,
  FiLock,
  FiPackage,
  FiSettings,
  FiTerminal,
  FiTool,
} from "react-icons/fi";
import {
  SiCplusplus,
  SiCss,
  SiDocker,
  SiGo,
  SiHtml5,
  SiJavascript,
  SiJson,
  SiJupyter,
  SiKotlin,
  SiLua,
  SiMarkdown,
  SiOpenjdk,
  SiPhp,
  SiPython,
  SiReact,
  SiRuby,
  SiRust,
  SiSharp,
  SiSwift,
  SiToml,
  SiTypescript,
  SiVuedotjs,
  SiXml,
  SiYaml,
} from "react-icons/si";

/**
 * @typedef {import("./file-icon.mjs").FileIconKind} FileIconKind
 * @typedef {import("react-icons").IconType} IconType
 */

/**
 * Kind → concrete vector icon component. Brand logos preserve their recognizable
 * silhouette; the rest are conceptual pictograms. No entry is a text monogram.
 *
 * @type {Record<FileIconKind, IconType>}
 */
export const FILE_ICON_COMPONENTS = {
  // Brand logos (recognizable silhouettes).
  python: SiPython,
  typescript: SiTypescript,
  javascript: SiJavascript,
  react: SiReact,
  rust: SiRust,
  go: SiGo,
  java: SiOpenjdk,
  kotlin: SiKotlin,
  csharp: SiSharp,
  native: SiCplusplus,
  ruby: SiRuby,
  php: SiPhp,
  lua: SiLua,
  swift: SiSwift,
  html: SiHtml5,
  stylesheet: SiCss,
  vue: SiVuedotjs,
  markdown: SiMarkdown,
  json: SiJson,
  yaml: SiYaml,
  toml: SiToml,
  xml: SiXml,
  docker: SiDocker,
  notebook: SiJupyter,
  // Conceptual pictograms (no established brand mark).
  config: FiSettings,
  shell: FiTerminal,
  build: FiTool,
  sql: FiDatabase,
  data: FiGrid,
  image: FiImage,
  lock: FiLock,
  package: FiPackage,
  protobuf: FiCode,
  directory: FiFolder,
  // A rendered/paginated document keeps the lined-page glyph; plain text and the
  // generic fallback use the blank page, so PDF stays visually distinct.
  pdf: FiFileText,
  text: FiFile,
  file: FiFile,
};

/**
 * The vector icon component for a kind, falling back to the generic file glyph.
 *
 * @param {FileIconKind | string} kind
 * @returns {IconType}
 */
export function fileIconComponent(kind) {
  return (
    FILE_ICON_COMPONENTS[/** @type {FileIconKind} */ (kind)]
    ?? FILE_ICON_COMPONENTS.file
  );
}
