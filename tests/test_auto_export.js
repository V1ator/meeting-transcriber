"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const AutoExport = require("../chrome-extension/auto-export.js");

test("health changes trigger export but dynamic timestamps do not", () => {
  const base = {
    meetingCode: "abc-defg-hij",
    startedAt: "2026-08-12T10:00:00Z",
    entries: [{ speaker: "Олег", text: "Тест" }],
    captureHealth: {
      channelState: "open",
      decodedCaptions: 1,
      finalizedAtMs: 10_000,
      exportedAt: "2026-08-12T10:10:00Z",
    },
  };
  const first = AutoExport.signature(base);
  assert.equal(AutoExport.signature({
    ...base,
    captureHealth: {
      ...base.captureHealth,
      finalizedAtMs: 20_000,
      exportedAt: "2026-08-12T10:20:00Z",
    },
  }), first);
  assert.notEqual(AutoExport.signature({
    ...base,
    captureHealth: {
      ...base.captureHealth,
      disconnectCount: 1,
    },
  }), first);
});
