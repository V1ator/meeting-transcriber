"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const AudioControl = require("../chrome-extension/audio-control.js");

test("backup audio control is restricted to the loopback recorder endpoint", () => {
  assert.equal(
    AudioControl.START_URL,
    "http://127.0.0.1:43119/recording/start"
  );
});

test("backup audio request uses a non-simple authenticated POST", () => {
  const request = AudioControl.startRequest();
  assert.equal(request.method, "POST");
  assert.equal(
    request.headers["X-Meeting-Transcriber"],
    "audio-control-v1"
  );
  assert.deepEqual(JSON.parse(request.body), { command: "start" });
});
