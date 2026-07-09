import assert from "node:assert/strict";
import test from "node:test";

import { mergeProjectFiles } from "../src/lib/project-files.ts";

test("project file pages append in server order", () => {
  const firstPage = [{ id: "a" }, { id: "b" }];
  const secondPage = [{ id: "c" }, { id: "d" }];

  assert.deepEqual(
    mergeProjectFiles(firstPage, secondPage),
    [{ id: "a" }, { id: "b" }, { id: "c" }, { id: "d" }],
  );
});

test("overlapping pages are de-duplicated without reordering visible files", () => {
  const firstPage = [{ id: "a", title: "first" }, { id: "b", title: "second" }];
  const overlappingPage = [{ id: "b", title: "duplicate" }, { id: "c", title: "third" }];

  assert.deepEqual(
    mergeProjectFiles(firstPage, overlappingPage),
    [{ id: "a", title: "first" }, { id: "b", title: "second" }, { id: "c", title: "third" }],
  );
});

test("duplicates inside a single page only render once", () => {
  assert.deepEqual(
    mergeProjectFiles([], [{ id: "a" }, { id: "a" }, { id: "b" }]),
    [{ id: "a" }, { id: "b" }],
  );
});
