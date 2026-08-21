"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const AutoExport = require("../chrome-extension/auto-export.js");

test("finalization and RTC health changes do not duplicate an export", () => {
  const base = {
    meetingCode: "abc-defg-hij",
    startedAt: "2026-08-12T10:00:00Z",
    endedAt: "2026-08-12T10:10:00Z",
    entries: [{
      speaker: "Олег", text: "Тест", startMs: 1_000, endMs: 2_000,
    }],
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
    endedAt: "2026-08-12T10:10:03Z",
    entries: [{
      speaker: "Олег", text: "Тест", startMs: 1_000, endMs: 4_500,
    }],
    captureHealth: {
      ...base.captureHealth,
      finalizedAtMs: 20_000,
      exportedAt: "2026-08-12T10:20:00Z",
      channelState: "missing",
      disconnectCount: 1,
      lastFailureReason: "closed",
    },
  }), first);
});

test("new transcript content still triggers a new export", () => {
  const base = {
    meetingCode: "abc-defg-hij",
    startedAt: "2026-08-12T10:00:00Z",
    entries: [{ speaker: "Олег", text: "Перша репліка" }],
  };
  const first = AutoExport.signature(base);
  assert.notEqual(AutoExport.signature({
    ...base,
    entries: [
      ...base.entries,
      { speaker: "Анна", text: "Нова репліка" },
    ],
  }), first);
  assert.notEqual(AutoExport.signature({
    ...base,
    entries: [{ speaker: "Олег", text: "Змінений текст" }],
  }), first);
});

test("failed empty capture is exported as diagnostics", () => {
  const diagnostic = {
    diagnosticOnly: true,
    entries: [],
    captureHealth: {
      decodeFailures: 1,
      rtcPackets: 1,
      lastFailureReason: "decode-stall",
    },
  };
  assert.equal(AutoExport.isDiagnosticOnly(diagnostic), true);
  assert.equal(AutoExport.shouldExport(diagnostic), true);
  assert.equal(AutoExport.shouldExport({ entries: [] }), false);
  assert.equal(AutoExport.shouldExport({
    diagnosticOnly: true,
    entries: [],
    captureHealth: { lastFailureReason: "closed" },
  }), false);
});
