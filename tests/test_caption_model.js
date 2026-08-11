"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const Model = require("../chrome-extension/caption-model.js");
const CaptionToggle = require("../chrome-extension/caption-toggle.js");
const AutoExport = require("../chrome-extension/auto-export.js");

test("incremental text replaces one live caption", () => {
  const state = Model.createState({ meetingCode: "abc-defg-hij" });
  Model.observe(state, {
    key: "node-1",
    speaker: "Інтерв’юер",
    text: "Перевіряємо",
    atMs: 1000,
    observedAt: "2026-07-28T08:00:00.000Z",
  });
  Model.observe(state, {
    key: "node-1",
    speaker: "Інтерв’юер",
    text: "Перевіряємо українські субтитри",
    atMs: 1800,
  });
  assert.equal(state.entries.length, 1);
  assert.equal(state.entries[0].text, "Перевіряємо українські субтитри");
});

test("a recreated Meet node does not duplicate the lingering caption", () => {
  const state = Model.createState({ meetingCode: "abc-defg-hij" });
  Model.observe(state, {
    key: "old-node",
    speaker: "Анна",
    text: "Так, усе працює",
    atMs: 2000,
    observedAt: "2026-07-28T08:00:00.000Z",
  });
  Model.finalize(state, "old-node", 2500);
  Model.observe(state, {
    key: "new-node",
    speaker: "Анна",
    text: "Так, усе працює",
    atMs: 3000,
  });
  assert.equal(state.entries.length, 1);
  assert.equal(state.entries[0].endMs, 3000);
});

test("a long historical caption replay is ignored after a DOM rebuild", () => {
  const state = Model.createState({ meetingCode: "abc-defg-hij" });
  const longText =
    "Це довга завершена репліка, яку Google Meet повторно показав після повної перебудови DOM captions.";
  Model.observe(state, {
    key: "old-node",
    speaker: "Інтерв’юер & 5 others",
    text: longText,
    atMs: 1_000,
  });
  Model.finalize(state, "old-node", 2_000);
  Model.observe(state, {
    key: "rebuilt-node",
    speaker: "Інтерв’юер & 5 others",
    text: longText,
    atMs: 240_000,
  });
  Model.finalize(state, "rebuilt-node", 241_000);
  assert.equal(state.entries.length, 1);
  assert.equal(state.entries[0].speaker, "Інтерв’юер");
  assert.equal(state.entries[0].endMs, 2_000);
});

test("a replay scan also ignores its historical short captions", () => {
  const state = Model.createState({ meetingCode: "abc-defg-hij" });
  const longText =
    "Це довга завершена репліка, яка дає змогу визначити пакетне відтворення старого DOM у Google Meet.";
  Model.observe(state, {
    key: "short-old",
    speaker: "Анна",
    text: "Всім привіт",
    atMs: 1_000,
  });
  Model.finalize(state, "short-old", 1_500);
  Model.observe(state, {
    key: "long-old",
    speaker: "Інтерв’юер",
    text: longText,
    atMs: 2_000,
  });
  Model.finalize(state, "long-old", 3_000);

  assert.equal(Model.isHistoricalReplay(state, "Інтерв’юер", longText), true);
  Model.observe(state, {
    key: "short-rebuilt",
    speaker: "Анна",
    text: "Всім привіт",
    atMs: 240_000,
    replayScan: true,
  });
  Model.observe(state, {
    key: "long-rebuilt",
    speaker: "Інтерв’юер",
    text: longText,
    atMs: 240_000,
    replayScan: true,
  });
  assert.equal(state.entries.length, 2);
});

test("a partial caption is replaced by its full version within 90 seconds", () => {
  const state = Model.createState({ meetingCode: "abc-defg-hij" });
  Model.observe(state, {
    key: "partial-node",
    speaker: "Анна",
    text: "Починаємо великий тест",
    atMs: 1_000,
  });
  Model.finalize(state, "partial-node", 2_000);
  Model.observe(state, {
    key: "full-node",
    speaker: "Анна",
    text: "Починаємо великий тест локальної транскрипції",
    atMs: 62_000,
  });
  assert.equal(state.entries.length, 1);
  assert.equal(
    state.entries[0].text,
    "Починаємо великий тест локальної транскрипції"
  );
});

test("a near-immediate long caption replay tolerates a corrected word", () => {
  const state = Model.createState({ meetingCode: "abc-defg-hij" });
  const partial =
    "У нас є сирі сорси і білдер який збирає таблицю з актуальними даними для кривих сезонності і метрики розрізі";
  const expanded =
    "У нас є сирі сорси і білдер який збирає таблицю з актуальними даними для кривих сезонності і метрики в розрізі а далі розраховуються прогнози";
  Model.observe(state, {
    key: "partial-node",
    speaker: "Ігор",
    text: partial,
    atMs: 1_000,
  });
  Model.finalize(state, "partial-node", 10_000);
  Model.observe(state, {
    key: "expanded-node",
    speaker: "Ігор",
    text: expanded,
    atMs: 10_100,
  });
  assert.equal(state.entries.length, 1);
  assert.equal(state.entries[0].text, expanded);
});

test("fuzzy long captions outside the tight window remain separate", () => {
  const state = Model.createState({ meetingCode: "abc-defg-hij" });
  const first =
    "Ми перевіряємо довгу аналітичну репліку про структуру метрик та налаштування майбутнього дашборду";
  const second =
    "Ми перевіряємо довгу аналітичну репліку про структуру нових метрик та налаштування майбутнього дашборду окремо";
  Model.observe(state, {
    key: "first-node",
    speaker: "Ігор",
    text: first,
    atMs: 1_000,
  });
  Model.finalize(state, "first-node", 2_000);
  Model.observe(state, {
    key: "second-node",
    speaker: "Ігор",
    text: second,
    atMs: 6_001,
  });
  assert.equal(state.entries.length, 2);
});

test("short repeated replies remain separate captions", () => {
  const state = Model.createState({ meetingCode: "abc-defg-hij" });
  Model.observe(state, {
    key: "node-1",
    speaker: "Анна",
    text: "Так",
    atMs: 1000,
    observedAt: "2026-07-28T08:00:00.000Z",
  });
  Model.finalize(state, "node-1", 1500);
  Model.observe(state, {
    key: "node-2",
    speaker: "Анна",
    text: "Так",
    atMs: 2000,
  });
  assert.equal(state.entries.length, 2);
});

test("speaker change on one DOM node creates another entry", () => {
  const state = Model.createState({ meetingCode: "abc-defg-hij" });
  Model.observe(state, {
    key: "node-1",
    speaker: "Інтерв’юер",
    text: "Перша репліка",
    atMs: 1000,
    observedAt: "2026-07-28T08:00:00.000Z",
  });
  Model.observe(state, {
    key: "node-1",
    speaker: "Анна",
    text: "Друга репліка",
    atMs: 2000,
  });
  assert.deepEqual(
    state.entries.map((entry) => entry.speaker),
    ["Інтерв’юер", "Анна"]
  );
});

test("RTC keeps the highest message version when packets arrive out of order", () => {
  const state = Model.createState({ meetingCode: "abc-defg-hij" });
  const base = {
    key: "rtc-42/@device-a",
    speaker: "Інтерв’юер",
    atMs: 1_000,
  };
  Model.observeVersioned(state, { ...base, version: 1, text: "аналіз" });
  Model.observeVersioned(state, {
    ...base,
    version: 3,
    text: "аналіз конкурентів і ринку",
    atMs: 1_200,
  });
  const stale = Model.observeVersioned(state, {
    ...base,
    version: 2,
    text: "аналіз конкурентів",
    atMs: 1_400,
  });
  assert.equal(stale, null);
  assert.equal(state.entries.length, 1);
  assert.equal(state.entries[0].text, "аналіз конкурентів і ринку");
  assert.equal(state.sourceVersions[base.key], 3);
});

test("duplicate RTC packets from two channels create one entry", () => {
  const state = Model.createState({ meetingCode: "abc-defg-hij" });
  const packet = {
    key: "rtc-77/@device-a",
    version: 4,
    speaker: "Інтерв’юер",
    text: "Одна завершена репліка",
    atMs: 2_000,
  };
  assert.ok(Model.observeVersioned(state, packet));
  assert.equal(Model.observeVersioned(state, packet), null);
  assert.equal(state.entries.length, 1);
});

test("different RTC message IDs are not heuristically merged", () => {
  const state = Model.createState({ meetingCode: "abc-defg-hij" });
  Model.observeVersioned(state, {
    key: "rtc-10/@device-a",
    version: 1,
    speaker: "Інтерв’юер",
    text: "Потрібно провести аналіз конкурентів",
    atMs: 1_000,
  });
  Model.observeVersioned(state, {
    key: "rtc-11/@device-a",
    version: 1,
    speaker: "Інтерв’юер",
    text: "Потрібно провести аналіз конкурентів і ринку",
    atMs: 5_000,
  });
  assert.equal(state.entries.length, 2);
  assert.equal(Model.exportState(state).entries.length, 2);
});

test("distinct RTC message IDs preserve legitimately repeated text", () => {
  const state = Model.createState({ meetingCode: "abc-defg-hij" });
  const text = "Це окрема довга репліка, яку учасник справді повторив ще раз";
  Model.observeVersioned(state, {
    key: "rtc-20/@device-a",
    version: 1,
    speaker: "Інтерв’юер",
    text,
    atMs: 1_000,
  });
  Model.observeVersioned(state, {
    key: "rtc-21/@device-a",
    version: 1,
    speaker: "Інтерв’юер",
    text,
    atMs: 5_000,
  });
  assert.equal(Model.exportState(state).entries.length, 2);
});

test("a later RTC version updates a finalized entry in place", () => {
  const state = Model.createState({ meetingCode: "abc-defg-hij" });
  const key = "rtc-91/@device-a";
  Model.observeVersioned(state, {
    key,
    version: 1,
    speaker: "Анна",
    text: "Коротка версія",
    atMs: 1_000,
  });
  Model.finalize(state, key, 3_000);
  Model.observeVersioned(state, {
    key,
    version: 2,
    speaker: "Анна",
    text: "Повна виправлена версія",
    atMs: 4_000,
  });
  assert.equal(state.entries.length, 1);
  assert.equal(state.entries[0].text, "Повна виправлена версія");
  assert.equal(state.entries[0].final, false);
});

test("RTC version state survives storage restoration", () => {
  const first = Model.createState({ meetingCode: "abc-defg-hij" });
  const key = "rtc-105/@device-a";
  Model.observeVersioned(first, {
    key,
    version: 5,
    speaker: "Інтерв’юер",
    text: "Актуальний текст",
    atMs: 1_000,
  });
  const restored = Model.createState(JSON.parse(JSON.stringify(first)));
  assert.equal(Model.observeVersioned(restored, {
    key,
    version: 4,
    speaker: "Інтерв’юер",
    text: "Застарілий текст",
    atMs: 2_000,
  }), null);
  Model.observeVersioned(restored, {
    key,
    version: 6,
    speaker: "Інтерв’юер",
    text: "Нова коректна редакція",
    atMs: 3_000,
  });
  assert.equal(restored.entries.length, 1);
  assert.equal(restored.entries[0].text, "Нова коректна редакція");
});

test("export strips internal state", () => {
  const state = Model.createState({
    meetingCode: "abc-defg-hij",
    meetingTitle: "Планування релізу",
    language: "uk",
    participants: ["Інтерв’юер", "Анна", "інтерв’юер"],
  });
  Model.observe(state, {
    key: "node-1",
    speaker: "Інтерв’юер",
    text: "Готово",
    atMs: 500,
    observedAt: "2026-07-28T08:00:00.000Z",
  });
  const exported = Model.exportState(state, "2026-07-28T08:30:00.000Z");
  assert.equal(exported.source, "google-meet-live-captions");
  assert.equal(exported.entries.length, 1);
  assert.equal(exported.meetingTitle, "Планування релізу");
  assert.deepEqual(exported.participants, ["Інтерв’юер", "Анна"]);
  assert.equal(Object.hasOwn(exported, "activeKeys"), false);
  assert.equal(Object.hasOwn(exported, "sourceKeys"), false);
  assert.equal(Object.hasOwn(exported, "sourceVersions"), false);
  assert.equal(Object.hasOwn(exported.entries[0], "id"), false);
  assert.equal(Object.hasOwn(exported.entries[0], "captureSource"), false);
});

test("export compacts legacy duplicates and preserves chat kind", () => {
  const longText =
    "Це довга завершена репліка, яка випадково була збережена двічі після перебудови інтерфейсу Meet.";
  const state = Model.createState({
    meetingCode: "abc-defg-hij",
    entries: [
      { speaker: "Анна", text: "Всім привіт", startMs: 500, endMs: 700 },
      { speaker: "Інтерв’юер & 3 others", text: longText, startMs: 1_000, endMs: 2_000 },
      { speaker: "Анна", text: "Всім привіт", startMs: 90_000, endMs: 90_000 },
      { speaker: "Інтерв’юер & 3 others", text: longText, startMs: 90_000, endMs: 91_000 },
      { speaker: "Марія", text: "Питання", kind: "chat", startMs: 3_000, endMs: 3_000 },
    ],
  });
  const exported = Model.exportState(state);
  assert.equal(exported.entries.length, 3);
  assert.equal(exported.entries[1].speaker, "Інтерв’юер");
  assert.equal(exported.entries[2].kind, "chat");
});

test("export drops a recent RTC replay burst but keeps real short replies", () => {
  const first = "Перевіряємо новий локальний режим транскрипції";
  const second = "Після цього переходимо до наступного питання";
  const state = Model.createState({
    meetingCode: "abc-defg-hij",
    entries: [
      { speaker: "Інтерв’юер", text: first, startMs: 1_000, endMs: 4_000 },
      { speaker: "Інтерв’юер", text: second, startMs: 4_100, endMs: 7_000 },
      { speaker: "Інтерв’юер", text: first, startMs: 6_900, endMs: 7_100 },
      { speaker: "Інтерв’юер", text: second, startMs: 6_950, endMs: 7_100 },
      { speaker: "Анна", text: "Так", startMs: 8_000, endMs: 8_200 },
      { speaker: "Анна", text: "Так", startMs: 9_000, endMs: 9_200 },
    ],
  });
  const exported = Model.exportState(state);
  assert.equal(exported.entries.length, 3);
  assert.equal(exported.entries[0].text, `${first} ${second}`);
  assert.deepEqual(exported.entries.slice(1).map((entry) => entry.text), ["Так", "Так"]);
});

test("export replaces a short corrected caption before assembling the turn", () => {
  const state = Model.createState({
    meetingCode: "abc-defg-hij",
    entries: [
      {
        speaker: "Інтерв’юер",
        text: "здається знайшов не знаюто справа добре",
        startMs: 1_000,
        endMs: 3_000,
      },
      { speaker: "Інтерв’юер", text: "дякую як в тебе", startMs: 2_900, endMs: 4_000 },
      {
        speaker: "Інтерв’юер",
        text: "здається знайшов не знаю справа добре добре",
        startMs: 4_100,
        endMs: 5_000,
      },
    ],
  });
  const exported = Model.exportState(state);
  assert.equal(exported.entries.length, 1);
  assert.equal(
    exported.entries[0].text,
    "здається знайшов не знаю справа добре добре дякую як в тебе"
  );
});

test("export assembles adjacent fragments until the speaker changes", () => {
  const state = Model.createState({
    meetingCode: "abc-defg-hij",
    entries: [
      { speaker: "Інтерв’юер", text: "аналітичному інже", startMs: 1_000, endMs: 3_000 },
      {
        speaker: "Інтерв’юер",
        text: "інженеру сьогодні був цікавий кандидат",
        startMs: 3_100,
        endMs: 5_000,
      },
      { speaker: "Анна", text: "Зрозуміло", startMs: 5_200, endMs: 6_000 },
      { speaker: "Інтерв’юер", text: "Продовжимо окремо", startMs: 6_100, endMs: 7_000 },
    ],
  });
  const exported = Model.exportState(state);
  assert.equal(exported.entries.length, 3);
  assert.equal(
    exported.entries[0].text,
    "аналітичному інженеру сьогодні був цікавий кандидат"
  );
  assert.deepEqual(exported.entries.map((entry) => entry.speaker), ["Інтерв’юер", "Анна", "Інтерв’юер"]);
});

test("caption toggle recognizes enable labels and rejects disable labels", () => {
  assert.equal(
    CaptionToggle.isEnableCaptionLabel("Turn on captions (c)", null), true
  );
  assert.equal(
    CaptionToggle.isEnableCaptionLabel("Увімкнути субтитри (c)", null), true
  );
  assert.equal(
    CaptionToggle.isEnableCaptionLabel("Włącz napisy", null), true
  );
  assert.equal(
    CaptionToggle.isEnableCaptionLabel("Turn off captions (c)", null), false
  );
  assert.equal(
    CaptionToggle.isEnableCaptionLabel("Вимкнути субтитри (c)", null), false
  );
});

test("caption regions are recognized by accessible labels", () => {
  assert.equal(CaptionToggle.isCaptionLabel("Captions"), true);
  assert.equal(CaptionToggle.isCaptionLabel("Субтитри"), true);
  assert.equal(CaptionToggle.isCaptionLabel("Napisy na żywo"), true);
  assert.equal(CaptionToggle.isCaptionLabel("Meeting controls"), false);
});

test("auto-export recognizes Meet leave controls", () => {
  assert.equal(AutoExport.isLeaveCallLabel("Leave call"), true);
  assert.equal(AutoExport.isLeaveCallLabel("Вийти з виклику"), true);
  assert.equal(AutoExport.isLeaveCallLabel("Залишити зустріч"), true);
  assert.equal(AutoExport.isLeaveCallLabel("Turn off captions"), false);
});

test("auto-export signature ignores export time but changes with captions", () => {
  const first = {
    meetingCode: "abc-defg-hij",
    startedAt: "2026-07-29T10:00:00.000Z",
    exportedAt: "2026-07-29T11:00:00.000Z",
    entries: [{ speaker: "Інтерв’юер", text: "Привіт" }],
  };
  const later = {
    ...first,
    exportedAt: "2026-07-29T11:30:00.000Z",
  };
  assert.equal(AutoExport.signature(first), AutoExport.signature(later));
  later.entries = [...later.entries, { speaker: "Анна", text: "Вітаю" }];
  assert.notEqual(AutoExport.signature(first), AutoExport.signature(later));
});
