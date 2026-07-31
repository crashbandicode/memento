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
