"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const Codec = require("../chrome-extension/rtc-caption-codec.js");

const source = fs.readFileSync(
  path.join(__dirname, "..", "chrome-extension", "rtc-bridge.js"),
  "utf8"
);

class FakeEventTarget {
  constructor() {
    this.listeners = new Map();
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  dispatchEvent(event) {
    (this.listeners.get(event.type) || []).forEach((listener) => listener(event));
    return true;
  }
}

function harness() {
  const document = new FakeEventTarget();
  const channels = [];
  const timers = new Map();
  let nextTimer = 1;

  class FakeChannel extends FakeEventTarget {
    constructor(label, options = {}) {
      super();
      this.label = label;
      this.options = options;
      this.readyState = "connecting";
      this.binaryType = "blob";
      this.sent = [];
    }

    send(data) {
      this.sent.push(data);
    }
  }

  class FakePeerConnection extends FakeEventTarget {
    constructor() {
      super();
      this.connectionState = "new";
    }

    createDataChannel(label, options) {
      const channel = new FakeChannel(label, options);
      channels.push(channel);
      return channel;
    }
  }

  class FakeCustomEvent {
    constructor(type, options = {}) {
      this.type = type;
      this.detail = options.detail;
    }
  }

  const context = {
    Blob: class FakeBlob {},
    CustomEvent: FakeCustomEvent,
    MeetingRtcCaptionCodec: Codec,
    RTCPeerConnection: FakePeerConnection,
    URL,
    clearTimeout(id) { timers.delete(id); },
    console,
    document,
    location: { href: "https://meet.google.com/abc-defg-hij" },
    queueMicrotask(callback) { callback(); },
    setTimeout(callback) {
      const id = nextTimer;
      nextTimer += 1;
      timers.set(id, callback);
      return id;
    },
  };
  context.globalThis = context;
  vm.runInNewContext(source, context);

  const nonce = "12345678-1234-1234-1234-123456789abc";
  function command(type, extra = {}) {
    document.dispatchEvent(new FakeCustomEvent(
      "meeting-transcriber:rtc-command",
      { detail: { type, nonce, ...extra } }
    ));
  }
  command("bind");

  function runTimers() {
    const pending = Array.from(timers.values());
    timers.clear();
    pending.forEach((callback) => callback());
  }

  return { channels, command, context, runTimers };
}

test("direct mode opens a captions data channel without native CC", () => {
  const runtime = harness();
  runtime.command("start");
  new runtime.context.RTCPeerConnection();

  assert.equal(runtime.channels.length, 1);
  assert.equal(runtime.channels[0].label, "captions");
  assert.equal(runtime.channels[0].options.ordered, true);
});

test("direct mode activates Ukrainian captions over media-session", () => {
  const runtime = harness();
  runtime.command("start", { languageCode: "uk-UA" });
  const peer = new runtime.context.RTCPeerConnection();
  const mediaSession = peer.createDataChannel("media-session");
  mediaSession.readyState = "open";
  mediaSession.dispatchEvent({ type: "open" });

  assert.equal(mediaSession.sent.length, 3);
  assert.equal(Codec.decodeMediaSessionCommandOp(mediaSession.sent[0]), 1);
  assert.equal(Codec.decodeMediaSessionAckSeq(mediaSession.sent[1]), 1);
  assert.equal(Codec.decodeMediaSessionAckSeq(mediaSession.sent[2]), 2);
  assert.ok(Buffer.from(mediaSession.sent[0]).includes(Buffer.from("uk-UA")));
});

test("observer mode never creates its own captions data channel", () => {
  const runtime = harness();
  runtime.command("observe");
  new runtime.context.RTCPeerConnection();
  runtime.runTimers();

  assert.equal(runtime.channels.length, 0);
});

test("a closed direct captions channel is opened again", () => {
  const runtime = harness();
  runtime.command("start");
  new runtime.context.RTCPeerConnection();
  const first = runtime.channels[0];
  first.readyState = "open";
  first.dispatchEvent({ type: "open" });
  first.readyState = "closed";
  first.dispatchEvent({ type: "close" });
  runtime.runTimers();

  assert.equal(runtime.channels.length, 2);
  assert.equal(runtime.channels[1].label, "captions");
});
