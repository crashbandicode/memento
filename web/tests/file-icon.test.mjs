import assert from "node:assert/strict";
import test from "node:test";

import {
  FILE_ICON_KINDS,
  FILE_ICON_LABELS,
  fileIconKind,
  fileIconLabel,
} from "../src/lib/file-icon.mjs";

test("file icon kinds follow source extensions", () => {
  assert.equal(fileIconKind("src/lib/api-client.ts"), "typescript");
  assert.equal(fileIconKind("scripts/release.ps1"), "shell");
  assert.equal(fileIconKind("docs/HANDOFF.md"), "markdown");
  assert.equal(fileIconKind("assets/logo.svg"), "image");
  assert.equal(fileIconKind("data/report.sql?plain=1"), "sql");
});

test("JSX/TSX components map to the React kind, plain TS/JS stay language kinds", () => {
  assert.equal(fileIconKind("src/components/SmartLink.tsx"), "react");
  assert.equal(fileIconKind("src/components/Widget.jsx"), "react");
  assert.equal(fileIconKind("src/index.ts"), "typescript");
  assert.equal(fileIconKind("src/index.mts"), "typescript");
  assert.equal(fileIconKind("src/index.js"), "javascript");
  assert.equal(fileIconKind("src/index.mjs"), "javascript");
});

test("YAML and TOML split out from the generic config kind", () => {
  assert.equal(fileIconKind("ci/workflow.yml"), "yaml");
  assert.equal(fileIconKind("k8s/deploy.yaml"), "yaml");
  assert.equal(fileIconKind("config/prod.toml"), "toml");
  // Plain settings formats stay the generic gear config kind.
  assert.equal(fileIconKind("app/settings.ini"), "config");
  assert.equal(fileIconKind(".editorconfig"), "config");
});

test("whole basenames override generic manifest extensions", () => {
  assert.equal(fileIconKind("package.json"), "package");
  assert.equal(fileIconKind("config/tsconfig.build.json"), "typescript");
  assert.equal(fileIconKind("deploy/Dockerfile.production"), "docker");
  // A compose file keeps the Docker kind even though `.yml` now maps to yaml.
  assert.equal(fileIconKind("docker-compose.override.yml"), "docker");
  // A lockfile keeps the package kind even though `.yaml` now maps to yaml.
  assert.equal(fileIconKind("pnpm-lock.yaml"), "package");
  assert.equal(fileIconKind("Makefile"), "build");
});

test("file icon matching handles encoded URLs and Windows paths", () => {
  assert.equal(fileIconKind("https://example.test/src/My%20View.vue#L8"), "vue");
  assert.equal(fileIconKind("C:\\repo\\config\\prod.toml"), "toml");
});

test("documents, notebooks, containers, and directories get dedicated kinds", () => {
  assert.equal(fileIconKind("docs/architecture-review.pdf"), "pdf");
  assert.equal(fileIconKind("analysis/Untitled.ipynb"), "notebook");
  assert.equal(fileIconKind("deploy/Containerfile"), "docker");
  assert.equal(fileIconKind("deploy/Containerfile.production"), "docker");
  assert.equal(fileIconKind("src/components/"), "directory");
  assert.equal(fileIconKind("C:\\repo\\dist\\"), "directory");
  // A directory named like a manifest is still a directory, not a package.
  assert.equal(fileIconKind("vendor/package.json/"), "directory");
});

test("extension matching is case-insensitive and unknown types fall back", () => {
  assert.equal(fileIconKind("SRC/MAIN.PY"), "python");
  assert.equal(fileIconKind("Notes.MD"), "markdown");
  assert.equal(fileIconKind("DEPLOY/DOCKERFILE"), "docker");
  assert.equal(fileIconKind("archive.bin"), "file");
  assert.equal(fileIconKind("mystery"), "file");
  assert.equal(fileIconKind(""), "file");
});

test("every kind has a descriptive accessible label — never a monogram", () => {
  // Labels are the icon's accessible name. They must be human-readable type
  // names, never the two-letter monograms the shipped design used (PY/MD/TS…).
  const monogram = /^[A-Za-z#<>{}. ]{1,3}$/;
  for (const kind of FILE_ICON_KINDS) {
    const label = FILE_ICON_LABELS[kind];
    assert.equal(typeof label, "string", `${kind} has a label`);
    assert.ok(label.length >= 4, `${kind} label "${label}" is descriptive`);
    assert.ok(
      !monogram.test(label),
      `${kind} label "${label}" must not be a bare monogram`,
    );
  }
  // A representative sample of the kinds that used to render as text monograms.
  assert.equal(fileIconLabel("python"), "Python file");
  assert.equal(fileIconLabel("markdown"), "Markdown file");
  assert.equal(fileIconLabel("typescript"), "TypeScript file");
  assert.equal(fileIconLabel("react"), "React component");
  assert.equal(fileIconLabel("rust"), "Rust file");
  // Unknown kinds fall back to a safe generic label.
  assert.equal(fileIconLabel("totally-unknown"), FILE_ICON_LABELS.file);
});
