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
