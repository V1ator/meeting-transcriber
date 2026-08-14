(function (root, factory) {
  const api = factory();
  root.MeetingRtcCaptionCodec = api;
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const MAX_INFLATED_BYTES = 1_000_000;
  const MAX_DIAGNOSTIC_FIELDS = 48;
  const MAX_DIAGNOSTIC_DEPTH = 5;

  const textDecoder = new TextDecoder("utf-8", { fatal: true });

  function asBytes(value) {
    if (value instanceof Uint8Array) return value;
    if (value instanceof ArrayBuffer) return new Uint8Array(value);
    if (ArrayBuffer.isView(value)) {
      return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
    }
    return null;
  }

  function readVarint(bytes, start, end = bytes.length) {
    let value = 0n;
    let shift = 0n;
    let offset = start;
    while (offset < end && shift <= 63n) {
      const byte = bytes[offset++];
      value |= BigInt(byte & 0x7f) << shift;
      if ((byte & 0x80) === 0) {
        return { value, offset };
      }
      shift += 7n;
    }
    return null;
  }

  function encodeVarint(value) {
    let remaining = BigInt(Math.max(0, Number(value) || 0));
    const result = [];
    do {
      let byte = Number(remaining & 0x7fn);
      remaining >>= 7n;
      if (remaining) byte |= 0x80;
      result.push(byte);
    } while (remaining);
    return result;
  }

  function concatBytes(...parts) {
    const length = parts.reduce((total, part) => total + part.length, 0);
    const result = new Uint8Array(length);
    let offset = 0;
    parts.forEach((part) => {
      result.set(part, offset);
      offset += part.length;
    });
    return result;
  }

  function varintField(number, value) {
    return Uint8Array.from([
      ...encodeVarint(number << 3),
      ...encodeVarint(value),
    ]);
  }

  function bytesField(number, value) {
    const bytes = asBytes(value) || new Uint8Array();
    return concatBytes(
      Uint8Array.from(encodeVarint((number << 3) | 2)),
      Uint8Array.from(encodeVarint(bytes.length)),
      bytes,
    );
  }

  function stringField(number, value) {
    return bytesField(number, new TextEncoder().encode(String(value || "")));
  }

  function safeNumber(value) {
    return value <= BigInt(Number.MAX_SAFE_INTEGER)
      ? Number(value)
      : value.toString();
  }

  function readFields(bytes, start = 0, end = bytes.length) {
    const fields = [];
    let offset = start;
    while (offset < end) {
      const tag = readVarint(bytes, offset, end);
      if (!tag || tag.value === 0n) return null;
      offset = tag.offset;
      const field = Number(tag.value >> 3n);
      const wire = Number(tag.value & 7n);
      if (!field) return null;

      if (wire === 0) {
        const decoded = readVarint(bytes, offset, end);
        if (!decoded) return null;
        fields.push({ field, wire, value: decoded.value });
        offset = decoded.offset;
      } else if (wire === 1) {
        if (offset + 8 > end) return null;
        fields.push({ field, wire, bytes: bytes.slice(offset, offset + 8) });
        offset += 8;
      } else if (wire === 2) {
        const length = readVarint(bytes, offset, end);
        if (!length || length.value > BigInt(end - length.offset)) return null;
        const lengthNumber = Number(length.value);
        offset = length.offset;
        fields.push({
          field,
          wire,
          bytes: bytes.slice(offset, offset + lengthNumber),
        });
        offset += lengthNumber;
      } else if (wire === 5) {
        if (offset + 4 > end) return null;
        fields.push({ field, wire, bytes: bytes.slice(offset, offset + 4) });
        offset += 4;
      } else {
        return null;
      }
    }
    return fields;
  }

  function fieldValue(fields, number, wire) {
    return fields?.find((item) => item.field === number && item.wire === wire);
  }

  function decodeString(bytes) {
    try {
      return textDecoder.decode(bytes).trim();
    } catch {
      return "";
    }
  }

  function printableString(bytes) {
    let value = "";
    try {
      value = textDecoder.decode(bytes);
    } catch {
      return "";
    }
    if (!value || value.length > 20_000) return "";
    const controls = Array.from(value).filter((character) => {
      const code = character.codePointAt(0);
      return code < 32 && ![9, 10, 13].includes(code);
    }).length;
    return controls / value.length <= 0.05 ? value : "";
  }

  function diagnosticStringKind(value) {
    if (/^@?(?:spaces\/)?[^\s/]+\/devices\/[^\s/]+$/i.test(value)) {
      return "device-id";
    }
    if (/^[a-z]{2,3}(?:-[A-Z]{2})?$/.test(value)) return "language-code";
    return "utf8";
  }

  function describeFields(bytes, depth, budget) {
    if (depth > MAX_DIAGNOSTIC_DEPTH || budget.remaining <= 0) return null;
    const fields = readFields(bytes);
    if (!fields) return null;
    const result = [];
    for (const item of fields) {
      if (budget.remaining <= 0) break;
      budget.remaining -= 1;
      const described = { field: item.field, wire: item.wire };
      if (item.wire === 0) {
        described.kind = "varint";
      } else if (item.wire === 1) {
        described.kind = "fixed64";
      } else if (item.wire === 5) {
        described.kind = "fixed32";
      } else if (item.wire === 2) {
        described.length = item.bytes.length;
        const printable = printableString(item.bytes);
        if (printable) {
          described.kind = diagnosticStringKind(printable);
          described.characters = Array.from(printable).length;
        } else {
          const nested = describeFields(item.bytes, depth + 1, budget);
          described.kind = nested ? "message" : "bytes";
          if (nested) described.fields = nested;
        }
      }
      result.push(described);
    }
    return result;
  }

  function describePacket(value) {
    const bytes = asBytes(value);
    if (!bytes?.length) return null;
    const fields = describeFields(bytes, 0, {
      remaining: MAX_DIAGNOSTIC_FIELDS,
    });
    return {
      schemaVersion: 1,
      byteLength: bytes.length,
      parsedAsProtobuf: Boolean(fields),
      fields: fields || [],
      redacted: true,
    };
  }

  function decodeTranscriptMessage(bytes) {
    const fields = readFields(bytes);
    if (!fields) return null;
    const device = fieldValue(fields, 1, 2);
    const messageId = fieldValue(fields, 2, 0);
    const messageVersion = fieldValue(fields, 3, 0);
    const text = fieldValue(fields, 6, 2);
    const language = fieldValue(fields, 8, 0);
    if (!device || !messageId || !messageVersion || !text || !language) return null;

    const deviceId = decodeString(device.bytes);
    const captionText = decodeString(text.bytes);
    if (!deviceId || !captionText) return null;
    return {
      deviceId: deviceId.startsWith("@") ? deviceId : `@${deviceId}`,
      messageId: `${safeNumber(messageId.value)}/@${deviceId.replace(/^@/, "")}`,
      messageVersion: safeNumber(messageVersion.value),
      langId: safeNumber(language.value),
      text: captionText,
    };
  }

  function decodeTranscriptPacket(value) {
    const bytes = asBytes(value);
    if (!bytes?.length) return null;

    const wrapper = readFields(bytes);
    if (wrapper) {
      // Current Meet packets wrap the transcript message in field 1. A string
      // in field 2 marks a different packet type and must not be interpreted.
      if (fieldValue(wrapper, 2, 2)) return null;
      const nested = fieldValue(wrapper, 1, 2);
      const decoded = nested && decodeTranscriptMessage(nested.bytes);
      if (decoded) return decoded;
    }
    return decodeTranscriptMessage(bytes);
  }

  function encodeMediaSessionCaptionCommand(op, language) {
    const captionConfig = concatBytes(
      stringField(1, language),
      stringField(2, language),
    );
    const clientConfig = bytesField(9, captionConfig);
    const updateMask = stringField(1, "client_config.caption_config");
    const captionUpdate = concatBytes(
      bytesField(1, clientConfig),
      bytesField(2, updateMask),
    );
    const command = concatBytes(
      varintField(1, op),
      bytesField(3, captionUpdate),
    );
    return bytesField(1, bytesField(2, command));
  }

  function encodeMediaSessionAck(seq) {
    const ack = concatBytes(
      varintField(2, seq),
      varintField(3, 1),
    );
    return bytesField(1, bytesField(1, ack));
  }

  function decodeMediaSessionCommandOp(value) {
    const bytes = asBytes(value);
    const packet = bytes && nestedFields(readFields(bytes), 1);
    const envelope = packet && nestedFields(packet, 2);
    const op = envelope && fieldValue(envelope, 1, 0);
    return op ? safeNumber(op.value) : null;
  }

  function decodeMediaSessionAckSeq(value) {
    const bytes = asBytes(value);
    const packet = bytes && nestedFields(readFields(bytes), 1);
    const envelope = packet && nestedFields(packet, 1);
    const seq = envelope && fieldValue(envelope, 2, 0);
    return seq ? safeNumber(seq.value) : null;
  }

  function decodeMediaSessionServerCounter(value) {
    const bytes = asBytes(value);
    const envelope = bytes && nestedFields(readFields(bytes), 1);
    const update = envelope && nestedFields(envelope, 4);
    const counter = update && fieldValue(update, 1, 0);
    return counter ? safeNumber(counter.value) : null;
  }

  function nestedFields(fields, number) {
    const item = fieldValue(fields, number, 2);
    return item ? readFields(item.bytes) : null;
  }

  function decodeDeviceRecord(bytes) {
    const fields = readFields(bytes);
    const device = fieldValue(fields, 1, 2);
    const name = fieldValue(fields, 2, 2);
    const deviceId = device && decodeString(device.bytes);
    const deviceName = name && decodeString(name.bytes);
    return deviceId && deviceName ? { deviceId, deviceName } : null;
  }

  function decodeDevicePacket(value) {
    const bytes = asBytes(value);
    const level1 = bytes && nestedFields(readFields(bytes), 1);
    const level2 = level1 && nestedFields(level1, 2);
    const level3 = level2 && nestedFields(level2, 13);
    const level4 = level3 && nestedFields(level3, 1);
    const record = level4 && fieldValue(level4, 2, 2);
    return record ? decodeDeviceRecord(record.bytes) : null;
  }

  function decodeMeetingCollection(value) {
    const bytes = asBytes(value);
    const level1 = bytes && nestedFields(readFields(bytes), 2);
    const level2 = level1 && nestedFields(level1, 2);
    if (!level2) return [];
    return level2
      .filter((item) => item.field === 2 && item.wire === 2)
      .map((item) => decodeDeviceRecord(item.bytes))
      .filter(Boolean);
  }

  function isGzip(bytes) {
    return bytes?.length >= 3
      && bytes[0] === 0x1f
      && bytes[1] === 0x8b
      && bytes[2] === 0x08;
  }

  async function inflatePacket(value) {
    let bytes = asBytes(value);
    if (!bytes) return null;
    if (!isGzip(bytes) && isGzip(bytes.slice(3))) bytes = bytes.slice(3);
    if (!isGzip(bytes)) return bytes;
    if (typeof DecompressionStream !== "function") return null;
    try {
      const reader = new Blob([bytes])
        .stream()
        .pipeThrough(new DecompressionStream("gzip"))
        .getReader();
      const chunks = [];
      let total = 0;
      while (true) {
        const { value: chunk, done } = await reader.read();
        if (done) break;
        total += chunk.byteLength;
        if (total > MAX_INFLATED_BYTES) {
          await reader.cancel();
          return null;
        }
        chunks.push(chunk);
      }
      const inflated = new Uint8Array(total);
      let offset = 0;
      chunks.forEach((chunk) => {
        inflated.set(chunk, offset);
        offset += chunk.byteLength;
      });
      return inflated;
    } catch {
      return null;
    }
  }

  return {
    asBytes,
    decodeDevicePacket,
    decodeMediaSessionAckSeq,
    decodeMediaSessionCommandOp,
    decodeMediaSessionServerCounter,
    decodeMeetingCollection,
    decodeTranscriptPacket,
    describePacket,
    encodeMediaSessionAck,
    encodeMediaSessionCaptionCommand,
    inflatePacket,
    MAX_INFLATED_BYTES,
    readFields,
  };
});
