import assert from "node:assert/strict";
import test from "node:test";

import { fileIconKind } from "../src/lib/file-icon.mjs";

test("file icon kinds follow source extensions", () => {
  assert.equal(fileIconKind("src/components/SmartLink.tsx"), "typescript");
  assert.equal(fileIconKind("scripts/release.ps1"), "shell");
  assert.equal(fileIconKind("docs/HANDOFF.md"), "markdown");
  assert.equal(fileIconKind("assets/logo.svg"), "image");
  assert.equal(fileIconKind("data/report.sql?plain=1"), "sql");
});

test("whole basenames override generic manifest extensions", () => {
  assert.equal(fileIconKind("package.json"), "package");
  assert.equal(fileIconKind("config/tsconfig.build.json"), "typescript");
  assert.equal(fileIconKind("deploy/Dockerfile.production"), "docker");
  assert.equal(fileIconKind("docker-compose.override.yml"), "docker");
  assert.equal(fileIconKind("Makefile"), "build");
});

test("file icon matching handles encoded URLs and Windows paths", () => {
  assert.equal(fileIconKind("https://example.test/src/My%20View.vue#L8"), "vue");
  assert.equal(fileIconKind("C:\\repo\\config\\prod.toml"), "config");
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
