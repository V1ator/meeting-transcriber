"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const extension = path.join(__dirname, "..", "chrome-extension");

test("extension never captures or hides native Meet caption DOM", () => {
  const content = fs.readFileSync(path.join(extension, "content.js"), "utf8");
  const styles = fs.readFileSync(path.join(extension, "content.css"), "utf8");
  const manifest = JSON.parse(
    fs.readFileSync(path.join(extension, "manifest.json"), "utf8")
  );
  const scripts = manifest.content_scripts.flatMap((entry) => entry.js || []);

  for (const obsolete of [
    "caption-toggle.js",
    "meeting-transcriber-hide-native-captions",
    "data-meeting-transcriber-caption-region",
    "data-meeting-transcriber-caption-shell",
    ".nMcdL",
    ".ygicle",
  ]) {
    assert.equal(content.includes(obsolete), false, obsolete);
    assert.equal(styles.includes(obsolete), false, obsolete);
    assert.equal(scripts.includes(obsolete), false, obsolete);
  }
  assert.match(content, /CHAT_TEXT_SELECTOR/);
  assert.match(content, /PARTICIPANT_NAME_SELECTOR/);
});
