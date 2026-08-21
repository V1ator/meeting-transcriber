"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const extension = path.join(__dirname, "..", "chrome-extension");

test("direct RTC mode never controls native Meet captions or layout", () => {
  const content = fs.readFileSync(path.join(extension, "content.js"), "utf8");
  const styles = fs.readFileSync(path.join(extension, "content.css"), "utf8");
  const bridge = fs.readFileSync(path.join(extension, "rtc-bridge.js"), "utf8");
  const manifest = JSON.parse(
    fs.readFileSync(path.join(extension, "manifest.json"), "utf8")
  );
  const scripts = manifest.content_scripts.flatMap((entry) => entry.js || []);
  assert.equal(manifest.version, "0.24.1");
  assert.equal(manifest.action.default_popup, "popup.html");
  for (const popupFile of ["popup.html", "popup.css", "popup.js"]) {
    assert.equal(fs.existsSync(path.join(extension, popupFile)), true, popupFile);
  }

  for (const obsolete of [
    "caption-toggle.js",
    "meeting-transcriber-hide-native-captions",
    "data-meeting-transcriber-caption-region",
    "data-meeting-transcriber-caption-shell",
    "data-meeting-transcriber-native-caption-overlay",
    ".nMcdL",
    ".ygicle",
  ]) {
    assert.equal(content.includes(obsolete), false, obsolete);
    assert.equal(styles.includes(obsolete), false, obsolete);
    assert.equal(scripts.includes(obsolete), false, obsolete);
  }
  assert.ok(scripts.includes("caption-control.js"));
  assert.match(content, /MeetingCaptionControl/);
  assert.equal(content.includes("maybeEnableCaptions"), false);
  assert.equal(content.includes("maybeHideNativeCaptions"), false);
  assert.equal(content.includes("autoEnableCaptions"), false);
  assert.equal(content.includes("autoHideNativeCaptions"), false);
  assert.equal(content.includes("button.click()"), false);
  assert.equal(styles.includes("native-caption-overlay"), false);
  assert.match(content, /captureMode = passiveDiagnosticMode/);
  assert.match(content, /direct-rtc-media-session/);
  assert.match(content, /direct-rtc-no-dom-changes/);
  assert.match(bridge, /createDataChannel\(CHANNEL_LABEL/);
  assert.match(bridge, /MEDIA_SESSION_LABEL/);
  assert.match(bridge, /encodeMediaSessionCaptionCommand/);
  assert.match(bridge, /OPEN_RETRY_MS/);
  assert.match(bridge, /scheduleOpenRetry/);
  assert.match(bridge, /peer-missing-retry/);
  assert.match(content, /meeting-transcriber-diagnostic-/);
  assert.match(content, /data-action="diagnostic"/);
  assert.match(content, /CHAT_TEXT_SELECTOR/);
  assert.match(content, /PARTICIPANT_NAME_SELECTOR/);
});

test("passive mode observes Tactiq without controlling Meet or exporting", () => {
  const content = fs.readFileSync(path.join(extension, "content.js"), "utf8");
  const bridge = fs.readFileSync(path.join(extension, "rtc-bridge.js"), "utf8");
  const background = fs.readFileSync(path.join(extension, "background.js"), "utf8");
  const popup = fs.readFileSync(path.join(extension, "popup.js"), "utf8");

  assert.match(content, /passiveDiagnosticMode/);
  assert.match(content, /data-action="passive-mode"/);
  assert.match(content, /mode-reload-notice/);
  assert.match(content, /DIAGNOSTIC_STORAGE_PREFIX/);
  assert.match(content, /passiveSession \|\| passiveDiagnosticMode/);
  assert.match(content, /if \(passiveDiagnosticMode\) return;/);
  assert.match(content, /rtcCommand\(passiveDiagnosticMode \? "observe" : "start"\)/);
  assert.match(content, /passive-diagnostic-mode/);
  assert.match(bridge, /command === "observe"/);
  assert.match(bridge, /observedChannels/);
  assert.match(content, /observedDataChannels/);
  const observeBranch = bridge.match(
    /command === "observe"\) \{([\s\S]*?)\} else if \(command === "stop"/
  )?.[1] || "";
  assert.match(observeBranch, /observerOnly = true/);
  assert.equal(observeBranch.includes("openCaptionChannel"), false);
  assert.match(background, /registration\.passiveDiagnosticMode === true/);
  assert.match(background, /stored\[SETTINGS_KEY\]\?\.passiveDiagnosticMode === true/);
  assert.match(popup, /\.\.\.\(stored\[SETTINGS_KEY\] \|\| \{\}\)/);
});

test("widget supports persistent speaker aliases and RTC health export", () => {
  const content = fs.readFileSync(path.join(extension, "content.js"), "utf8");
  const model = fs.readFileSync(path.join(extension, "caption-model.js"), "utf8");
  assert.match(content, /speakerAliases/);
  assert.match(content, /speakerNameAliases/);
  assert.match(content, /rename-speaker/);
  assert.match(content, /refreshCaptureHealth/);
  assert.match(content, /autoAudioFallbackEnabled/);
  assert.match(content, /auto-audio-fallback/);
  assert.match(model, /captureHealth/);
  assert.match(model, /diagnosticOnly/);
});
