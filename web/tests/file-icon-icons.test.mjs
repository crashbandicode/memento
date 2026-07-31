import assert from "node:assert/strict";
import test from "node:test";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
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

import { FILE_ICON_KINDS, fileIconLabel } from "../src/lib/file-icon.mjs";
import {
  FILE_ICON_COMPONENTS,
  fileIconComponent,
} from "../src/lib/file-icon-icons.mjs";

test("every file kind maps to a real react-icons vector component", () => {
  for (const kind of FILE_ICON_KINDS) {
    const Comp = FILE_ICON_COMPONENTS[kind];
    assert.equal(typeof Comp, "function", `${kind} maps to an icon component`);
    assert.equal(fileIconComponent(kind), Comp, `${kind} resolves to its component`);
  }
  // Unknown kinds fall back to the generic file glyph, not a crash / monogram.
  assert.equal(fileIconComponent("totally-unknown"), FILE_ICON_COMPONENTS.file);
  assert.equal(fileIconComponent("totally-unknown"), FiFile);
});

test("brand-logo kinds use the established recognizable Simple Icons logo", () => {
  const expected = {
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
  };
  for (const [kind, Comp] of Object.entries(expected)) {
    assert.equal(fileIconComponent(kind), Comp, `${kind} uses its brand logo`);
  }
});

test("non-brand kinds use conceptual pictograms (gear, terminal, database, …)", () => {
  const expected = {
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
    pdf: FiFileText,
    text: FiFile,
    file: FiFile,
  };
  for (const [kind, Comp] of Object.entries(expected)) {
    assert.equal(fileIconComponent(kind), Comp, `${kind} uses a conceptual pictogram`);
  }
});

test("no known kind renders a text/monogram glyph — only SVG paths", () => {
  for (const kind of FILE_ICON_KINDS) {
    const Comp = fileIconComponent(kind);
    const markup = renderToStaticMarkup(
      React.createElement(Comp, { size: 14, "aria-label": fileIconLabel(kind) }),
    );
    assert.match(markup, /^<svg[\s>]/, `${kind} renders an <svg>`);
    // An SVG built from <path>/<circle>/etc. carries no <text> node …
    assert.doesNotMatch(markup, /<text[\s>]/i, `${kind} has no <text> element`);
    assert.doesNotMatch(markup, /<tspan[\s>]/i, `${kind} has no <tspan> element`);
    // … and therefore no visible letters at all once tags are stripped.
    const visibleText = markup.replace(/<[^>]+>/g, "").trim();
    assert.equal(visibleText, "", `${kind} icon renders no letters (got "${visibleText}")`);
  }
});
