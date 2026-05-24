import test from "node:test";
import assert from "node:assert/strict";

import { formatLicenseKey, makeLicenseKey, normalizeLicenseKey } from "./index.js";

test("normalizes license keys for lookup", () => {
  assert.equal(normalizeLicenseKey(" csw-ab12c de34f-gh56j-kl78m "), "CSWAB12CDE34FGH56JKL78M");
});

test("formats normalized keys for customers", () => {
  assert.equal(formatLicenseKey("CSWABCDE12345FGHIJ67890"), "CSW-ABCDE-12345-FGHIJ-67890");
});

test("generated license keys use the public support-friendly format", () => {
  const key = makeLicenseKey();
  assert.match(key, /^CSW-[A-Z2-9]{5}-[A-Z2-9]{5}-[A-Z2-9]{5}-[A-Z2-9]{5}$/u);
});
