(function (root, factory) {
  const api = factory();
  root.MeetingAudioControl = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const START_URL = "http://127.0.0.1:43119/recording/start";

  function startRequest() {
    return {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Meeting-Transcriber": "audio-control-v1",
      },
      body: JSON.stringify({ command: "start" }),
    };
  }

  return { START_URL, startRequest };
});
