"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const Model = require("../chrome-extension/caption-model.js");

function state() {
  return Model.createState({
    meetingCode: "abc-defg-hij",
    meetingTitle: "Тестова зустріч",
    startedAt: "2026-08-12T10:00:00.000Z",
  });
}

test("normalizes text and participant names", () => {
  assert.equal(Model.normalizeText("  один   два\n"), "один два");
  assert.equal(Model.cleanSpeaker("Олег & 2 others"), "Олег");
  assert.equal(Model.cleanSpeaker("Олег та ще 2 учасники"), "Олег");
});

test("participants are unique ignoring case", () => {
  const current = state();
  assert.equal(Model.addParticipant(current, "Олег"), true);
  assert.equal(Model.addParticipant(current, "олег"), false);
  assert.deepEqual(current.participants, ["Олег"]);
});

test("chat observation is deduplicated after state restoration", () => {
  const current = state();
  Model.observe(current, {
    key: "chat-1",
    kind: "chat",
    speaker: "Марія",
    text: "https://example.com",
    atMs: 1_000,
  });
  Model.finalize(current, "chat-1", 1_000);

  const restored = Model.createState(JSON.parse(JSON.stringify(current)));
  const duplicate = Model.observe(restored, {
    key: "chat-reloaded",
    kind: "chat",
    speaker: "Марія",
    text: "https://example.com",
    atMs: 2_000,
  });
  assert.equal(restored.entries.length, 1);
  assert.equal(duplicate.id, restored.entries[0].id);
});

test("RTC keeps the highest message version", () => {
  const current = state();
  const base = {
    key: "rtc-message-1",
    speaker: "Олег",
    atMs: 1_000,
  };
  Model.observeVersioned(current, { ...base, version: 1, text: "аналіз" });
  Model.observeVersioned(current, {
    ...base,
    version: 3,
    text: "аналіз завершено",
    atMs: 2_000,
  });
  const stale = Model.observeVersioned(current, {
    ...base,
    version: 2,
    text: "аналіз майже",
    atMs: 3_000,
  });
  assert.equal(stale, null);
  assert.equal(current.entries.length, 1);
  assert.equal(current.entries[0].text, "аналіз завершено");
  assert.equal(current.sourceVersions[base.key], 3);
});

test("duplicate RTC packets for one message create one entry", () => {
  const current = state();
  const packet = {
    key: "rtc-message-1",
    version: 4,
    speaker: "Марія",
    text: "Погоджуємо наступний крок",
    atMs: 4_000,
  };
  Model.observeVersioned(current, packet);
  Model.observeVersioned(current, packet);
  assert.equal(current.entries.length, 1);
});

test("different RTC message IDs remain raw for Python normalization", () => {
  const current = state();
  Model.observeVersioned(current, {
    key: "rtc-message-1",
    version: 1,
    speaker: "Олег",
    text: "Перша частина",
    atMs: 1_000,
  });
  Model.observeVersioned(current, {
    key: "rtc-message-2",
    version: 1,
    speaker: "Олег",
    text: "друга частина",
    atMs: 1_500,
  });
  assert.deepEqual(
    Model.compactEntries(current.entries).map((entry) => entry.text),
    ["Перша частина", "друга частина"]
  );
});

test("a later RTC version updates a finalized entry", () => {
  const current = state();
  const base = {
    key: "rtc-message-1",
    speaker: "Олег",
    atMs: 1_000,
  };
  Model.observeVersioned(current, { ...base, version: 1, text: "Початок" });
  Model.finalize(current, base.key, 2_000);
  Model.observeVersioned(current, {
    ...base,
    version: 2,
    text: "Початок завершено",
    atMs: 3_000,
  });
  assert.equal(current.entries.length, 1);
  assert.equal(current.entries[0].text, "Початок завершено");
});

test("RTC version state survives storage restoration", () => {
  const current = state();
  Model.observeVersioned(current, {
    key: "rtc-message-1",
    version: 5,
    speaker: "Олег",
    text: "Фінальна версія",
    atMs: 1_000,
  });
  const restored = Model.createState(JSON.parse(JSON.stringify(current)));
  assert.equal(Model.observeVersioned(restored, {
    key: "rtc-message-1",
    version: 4,
    speaker: "Олег",
    text: "Стара версія",
    atMs: 2_000,
  }), null);
  assert.equal(restored.entries[0].text, "Фінальна версія");
});

test("invalid RTC versions are ignored", () => {
  const current = state();
  for (const version of [-1, 1.5, Number.NaN]) {
    assert.equal(Model.observeVersioned(current, {
      key: "rtc-message",
      version,
      speaker: "Олег",
      text: "Репліка",
      atMs: 1_000,
    }), null);
  }
  assert.equal(current.entries.length, 0);
});

test("export preserves raw RTC turns and deduplicates exact chat", () => {
  const current = state();
  current.entries.push(
    {
      id: 1,
      speaker: "Олег",
      text: "Перша RTC репліка",
      startMs: 1_000,
      endMs: 1_500,
      captureSource: "rtc",
    },
    {
      id: 2,
      speaker: "Олег",
      text: "Друга RTC репліка",
      startMs: 1_600,
      endMs: 2_000,
      captureSource: "rtc",
    },
    {
      id: 3,
      kind: "chat",
      speaker: "Марія",
      text: "Посилання",
      startMs: 2_100,
      endMs: 2_100,
    },
    {
      id: 4,
      kind: "chat",
      speaker: "Марія",
      text: "Посилання",
      startMs: 2_200,
      endMs: 2_200,
    }
  );
  const exported = Model.exportState(current, "2026-08-12T10:30:00.000Z");
  assert.deepEqual(exported.entries.map((entry) => entry.text), [
    "Перша RTC репліка",
    "Друга RTC репліка",
    "Посилання",
  ]);
  assert.equal(exported.entries[2].kind, "chat");
  assert.deepEqual(exported.participants, ["Олег", "Марія"]);
});
