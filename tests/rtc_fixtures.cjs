"use strict";

function encodeVarint(value) {
  let remaining = BigInt(value);
  const output = [];
  do {
    let byte = Number(remaining & 0x7fn);
    remaining >>= 7n;
    if (remaining) byte |= 0x80;
    output.push(byte);
  } while (remaining);
  return output;
}

function encodeField(field, wire, payload) {
  return [...encodeVarint(BigInt(field * 8 + wire)), ...payload];
}

function encodeStringField(field, value) {
  const bytes = new TextEncoder().encode(value);
  return encodeField(field, 2, [...encodeVarint(bytes.length), ...bytes]);
}

function encodeVarintField(field, value) {
  return encodeField(field, 0, encodeVarint(value));
}

function wrapBytes(field, bytes) {
  return new Uint8Array(encodeField(
    field, 2, [...encodeVarint(bytes.length), ...bytes]
  ));
}

function encodeTranscriptFixture(message) {
  const inner = new Uint8Array([
    ...encodeStringField(1, message.deviceId),
    ...encodeVarintField(2, message.messageId),
    ...encodeVarintField(3, message.messageVersion),
    ...encodeStringField(6, message.text),
    ...encodeVarintField(8, message.langId),
  ]);
  return wrapBytes(1, inner);
}

function encodeMeetingCollectionFixture(devices) {
  const records = devices.flatMap((device) => {
    const record = new Uint8Array([
      ...encodeStringField(1, device.deviceId),
      ...encodeStringField(2, device.deviceName),
    ]);
    return [...wrapBytes(2, record)];
  });
  return wrapBytes(2, wrapBytes(2, new Uint8Array(records)));
}

module.exports = { encodeMeetingCollectionFixture, encodeTranscriptFixture };
