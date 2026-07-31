import assert from "node:assert/strict";
import test from "node:test";

import {
  classifyInlineCode,
  classifySmartLink,
  displayFileName,
  looksLikeFilePath,
} from "../src/lib/smart-link-classifier.mjs";

test("repo paths become file links with a compact filename", () => {
  assert.equal(looksLikeFilePath("docs/regression-handoff.md"), true);
  assert.equal(displayFileName("docs/regression-handoff.md"), "regression-handoff.md");
  assert.deepEqual(
    classifySmartLink("docs/HANDOFF.md", "docs/HANDOFF.md"),
    {
      kind: "file",
      href: "docs/HANDOFF.md",
      label: "HANDOFF.md",
      path: "docs/HANDOFF.md",
    },
  );
});

test("GitLab compares expose provider and ref pills", () => {
  const info = classifySmartLink(
    "https://gitlab.com/acme/service/-/compare/f54a57bd...13ab85e7",
    "Rescue baseline → current FastAPI",
  );
  assert.equal(info.kind, "git-compare");
  assert.equal(info.provider, "gitlab");
  assert.deepEqual(info.refs, ["f54a57bd", "13ab85e7"]);
});

test("GitHub commit links expose a shortened commit pill", () => {
  const info = classifySmartLink(
    "https://github.com/acme/service/commit/9c216b8aa55aa55aa55aa55aa55aa55aa55aa55a",
    "Release commit",
  );
  assert.equal(info.kind, "git-commit");
  assert.equal(info.provider, "github");
  assert.equal(info.ref, "9c216b8aa5");
});

test("generic URLs retain their label and add a domain cue", () => {
  assert.deepEqual(
    classifySmartLink("https://memento.babypotatofarm.com/status", "Memento deployment"),
    {
      kind: "web",
      href: "https://memento.babypotatofarm.com/status",
      label: "Memento deployment",
      domain: "memento.babypotatofarm.com",
    },
  );
});

test("inline paths and SHAs become semantic chips without linkifying prose", () => {
  assert.deepEqual(
    classifyInlineCode("config/prod.toml"),
    { kind: "file", value: "config/prod.toml", display: "prod.toml" },
  );
  assert.deepEqual(
    classifyInlineCode("9c216b8"),
    { kind: "sha", value: "9c216b8", display: "9c216b8" },
  );
  assert.deepEqual(classifyInlineCode("npm test"), { kind: "plain", value: "npm test" });
});

test("canvas links become a distinct canvas kind, not a generic file", () => {
  const info = classifySmartLink(
    "/Users/p/.cursor/projects/ws/canvases/billing-review.canvas.tsx",
    "billing-review",
  );
  assert.equal(info.kind, "canvas");
  assert.equal(info.name, "billing-review");
  assert.equal(info.path, "/Users/p/.cursor/projects/ws/canvases/billing-review.canvas.tsx");
});

test("http canvas links expose the host as a domain", () => {
  const info = classifySmartLink(
    "https://canvas.example.com/artifacts/report.canvas.tsx",
    "report",
  );
  assert.equal(info.kind, "canvas");
  assert.equal(info.domain, "canvas.example.com");
  assert.equal(info.name, "report");
});

test("plain .tsx files stay file links (only .canvas.tsx is a canvas)", () => {
  assert.equal(classifySmartLink("src/App.tsx", "src/App.tsx").kind, "file");
});

test("inline canvas paths become a canvas chip", () => {
  assert.deepEqual(
    classifyInlineCode("canvases/audit.canvas.tsx"),
    { kind: "canvas", value: "canvases/audit.canvas.tsx", display: "audit", path: "canvases/audit.canvas.tsx" },
  );
});

test("unsafe canvas targets never resolve to the canvas kind", () => {
  // `canvas` is the only kind that opens the in-app viewer / iframe embed, so an
  // unsafe target must degrade to any other (inert) kind — never `canvas`.
  assert.notEqual(classifySmartLink("../../secret.canvas.tsx", "secret").kind, "canvas");
  assert.notEqual(classifySmartLink("a/../../b.canvas.tsx", "b").kind, "canvas");
  assert.notEqual(classifySmartLink("javascript:evil.canvas.tsx", "evil").kind, "canvas");
  assert.notEqual(classifyInlineCode("../secret.canvas.tsx").kind, "canvas");
  assert.notEqual(classifyInlineCode("a\\..\\b.canvas.tsx").kind, "canvas");
  // A safe target still resolves to a canvas chip.
  assert.equal(classifyInlineCode("canvases/ok.canvas.tsx").kind, "canvas");
});
