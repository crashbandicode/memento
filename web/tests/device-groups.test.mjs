import assert from "node:assert/strict";
import test from "node:test";

import {
  butterbridgeIdentities,
  deviceGroups,
  dreamlandIdentities,
} from "../e2e/fixtures/device-groups.mjs";


test("sidebar regression fixture preserves the production-like identity churn", () => {
  assert.equal(butterbridgeIdentities.length, 6);
  assert.equal(dreamlandIdentities.length, 2);
  assert.deepEqual(
    butterbridgeIdentities.map((identity) => identity.total_files),
    [1, 0, 0, 0, 648, 0],
  );
  assert.equal(new Set(
    butterbridgeIdentities.map((identity) => identity.label),
  ).size, 6);
});

test("visible host counts aggregate all child identities once", () => {
  assert.deepEqual(
    deviceGroups.map((group) => [group.name, group.total_files]),
    [
      ["butterbridge", 649],
      ["dreamland-yoga", 4084],
    ],
  );
});
