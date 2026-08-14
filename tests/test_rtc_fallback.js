"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const Fallback = require("../chrome-extension/rtc-fallback.js");

test("open RTC channel without speech does not activate recovery", () => {
  const deadline = Fallback.arm(0, 1_000, 20_000, 0, "open");
  assert.equal(deadline, 0);
  assert.equal(Fallback.isDue(21_000, 21_000, 0, "open"), false);
});

test("open RTC channel with undecodable packets activates recovery", () => {
  const deadline = Fallback.arm(
    0, 1_000, 20_000, 0, "open", 1, 1
  );
  assert.equal(deadline, 21_000);
  assert.equal(
    Fallback.isDue(deadline, 20_999, 0, "open", 1, 1), false
  );
  assert.equal(
    Fallback.isDue(deadline, 21_000, 0, "open", 1, 1), true
  );
});

test("channel errors do not move an already armed deadline", () => {
  const deadline = Fallback.arm(0, 1_000, 20_000, 0, "missing");
  assert.equal(Fallback.arm(deadline, 8_000, 20_000, 0, "closed"), deadline);
  assert.equal(Fallback.arm(deadline, 19_000, 20_000, 0, "closed"), deadline);
});

test("first decoded caption disarms startup fallback", () => {
  const deadline = Fallback.arm(0, 1_000, 20_000, 0, "connecting");
  assert.equal(
    Fallback.arm(deadline, 2_000, 20_000, 1, "open", 2, 1), 0
  );
  assert.equal(
    Fallback.isDue(deadline, 30_000, 1, "open", 2, 1), false
  );
});

test("RTC connection timeout is due when channel never opens", () => {
  const deadline = Fallback.arm(0, 1_000, 20_000, 0, "connecting");
  assert.equal(deadline, 21_000);
  assert.equal(Fallback.isDue(deadline, 20_999, 0, "connecting"), false);
  assert.equal(Fallback.isDue(deadline, 21_000, 0, "connecting"), true);
});
