"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { gzipSync } = require("node:zlib");
const Codec = require("../chrome-extension/rtc-caption-codec.js");
const Model = require("../chrome-extension/caption-model.js");
const Fixtures = require("./rtc_fixtures.cjs");

test("decodes a wrapped Google Meet transcript protobuf packet", () => {
  const packet = Fixtures.encodeTranscriptFixture({
    deviceId: "spaces/demo/devices/participant-a",
    messageId: 48291,
    messageVersion: 3,
    text: "Перевіряємо RTC captions без видимого блоку CC",
    langId: 44,
  });
  assert.deepEqual(Codec.decodeTranscriptPacket(packet), {
    deviceId: "@spaces/demo/devices/participant-a",
    messageId: "48291/@spaces/demo/devices/participant-a",
    messageVersion: 3,
    langId: 44,
    text: "Перевіряємо RTC captions без видимого блоку CC",
  });
});

test("decodes an unwrapped transcript protobuf packet", () => {
  const wrapped = Fixtures.encodeTranscriptFixture({
    deviceId: "device-a",
    messageId: 7,
    messageVersion: 1,
    text: "Короткий тест",
    langId: 44,
  });
  const wrapperFields = Codec.readFields(wrapped);
  assert.equal(
    Codec.decodeTranscriptPacket(wrapperFields[0].bytes).text,
    "Короткий тест"
  );
});

test("rejects a different wrapped packet type", () => {
  assert.equal(Codec.decodeTranscriptPacket(
    new Uint8Array([18, 4, 110, 111, 112, 101])
  ), null);
});

test("rejects truncated protobuf instead of leaking partial text", () => {
  const packet = Fixtures.encodeTranscriptFixture({
    deviceId: "device-a",
    messageId: 7,
    messageVersion: 1,
    text: "Не має пройти",
    langId: 44,
  });
  assert.equal(Codec.decodeTranscriptPacket(packet.slice(0, -3)), null);
});

test("packet diagnostics preserve protobuf shape without transcript text", () => {
  const packet = Fixtures.encodeTranscriptFixture({
    deviceId: "spaces/private/devices/person-secret",
    messageId: 7,
    messageVersion: 1,
    text: "Конфіденційний текст зустрічі",
    langId: 44,
  });
  const diagnostic = Codec.describePacket(packet);
  const serialized = JSON.stringify(diagnostic);
  assert.equal(diagnostic.redacted, true);
  assert.equal(diagnostic.parsedAsProtobuf, true);
  assert.match(serialized, /device-id/);
  assert.match(serialized, /utf8/);
  assert.doesNotMatch(serialized, /person-secret/);
  assert.doesNotMatch(serialized, /Конфіденційний/);
});

test("inflates plain and three-byte-prefixed gzip packets", async () => {
  const packet = Fixtures.encodeTranscriptFixture({
    deviceId: "device-z",
    messageId: 9,
    messageVersion: 2,
    text: "Стиснутий пакет",
    langId: 44,
  });
  const gzip = new Uint8Array(gzipSync(packet));
  const prefixed = new Uint8Array(3 + gzip.length);
  prefixed.set([1, 0, 0]);
  prefixed.set(gzip, 3);
  assert.equal(
    Codec.decodeTranscriptPacket(await Codec.inflatePacket(gzip)).text,
    "Стиснутий пакет"
  );
  assert.equal(
    Codec.decodeTranscriptPacket(await Codec.inflatePacket(prefixed)).text,
    "Стиснутий пакет"
  );
});

test("rejects gzip packets that inflate beyond the safety limit", async () => {
  const bomb = new Uint8Array(Codec.MAX_INFLATED_BYTES + 1);
  const gzip = new Uint8Array(gzipSync(bomb));
  assert.equal(await Codec.inflatePacket(gzip), null);
});

test("encodes Meet media-session caption activation packets", () => {
  const command = Codec.encodeMediaSessionCaptionCommand(1, "uk-UA");
  const ack = Codec.encodeMediaSessionAck(7);
  const serverUpdate = Uint8Array.from([
    0x0a, 0x06, 0x08, 0x01, 0x22, 0x02, 0x08, 0x03,
  ]);

  assert.equal(Codec.decodeMediaSessionCommandOp(command), 1);
  assert.equal(Codec.decodeMediaSessionAckSeq(ack), 7);
  assert.equal(Codec.decodeMediaSessionServerCounter(serverUpdate), 3);
  assert.equal(
    Buffer.from(ack).toString("hex"),
    "0a060a0410071801"
  );
  assert.equal(command.length, 58);
  assert.ok(Buffer.from(command).includes(Buffer.from("uk-UA")));
  assert.ok(Buffer.from(command).includes(
    Buffer.from("client_config.caption_config")
  ));
});

test("decodes participant names from a Meet collections packet", () => {
  const packet = Fixtures.encodeMeetingCollectionFixture([
    { deviceId: "device-a", deviceName: "Participant A" },
    { deviceId: "901", deviceName: "Тестовий учасник" },
  ]);
  assert.deepEqual(Codec.decodeMeetingCollection(packet), [
    { deviceId: "device-a", deviceName: "Participant A" },
    { deviceId: "901", deviceName: "Тестовий учасник" },
  ]);
});

test("decoded RTC versions update one transcript entry deterministically", () => {
  const state = Model.createState({ meetingCode: "abc-defg-hij" });
  [
    { messageVersion: 1, text: "Аналіз" },
    { messageVersion: 3, text: "Аналіз конкурентів і ринку" },
    { messageVersion: 2, text: "Аналіз конкурентів" },
  ].forEach((update, index) => {
    const message = Codec.decodeTranscriptPacket(Fixtures.encodeTranscriptFixture({
      deviceId: "device-a",
      messageId: 42,
      langId: 44,
      ...update,
    }));
    Model.observeVersioned(state, {
      key: `rtc-${message.messageId}`,
      version: message.messageVersion,
      speaker: "Інтерв’юер",
      text: message.text,
      atMs: 1_000 + index * 100,
    });
  });
  assert.equal(state.entries.length, 1);
  assert.equal(state.entries[0].text, "Аналіз конкурентів і ринку");
});
