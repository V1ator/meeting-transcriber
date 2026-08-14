"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const Control = require("../chrome-extension/caption-control.js");

function fakeControl(attributes = {}, options = {}) {
  return {
    tagName: options.tagName || "BUTTON",
    textContent: options.textContent || "",
    disabled: Boolean(options.disabled),
    getAttribute(name) {
      return attributes[name] ?? null;
    },
    getClientRects() {
      return options.visible === false ? [] : [{}];
    },
    closest(selector) {
      return selector === "#meeting-transcriber-widget" && options.inWidget
        ? {}
        : null;
    },
  };
}

function fakeDocument(controls) {
  return {
    querySelectorAll() {
      return controls;
    },
  };
}

test("recognizes whether the native Meet caption control is on or off", () => {
  const cases = [
    ["Turn on captions (c)", "disabled"],
    ["Turn off captions (c)", "enabled"],
    ["Увімкнути субтитри", "disabled"],
    ["Вимкнути субтитри", "enabled"],
    ["Субтитри вимкнені", "disabled"],
    ["Włącz napisy", "disabled"],
    ["Wyłącz napisy", "enabled"],
  ];
  for (const [label, expected] of cases) {
    assert.equal(
      Control.captionState(fakeControl({ "aria-label": label })),
      expected,
      label
    );
  }
});

test("uses aria-pressed only for controls identified as captions", () => {
  assert.equal(Control.captionState(fakeControl({
    "aria-label": "Captions",
    "aria-pressed": "false",
  })), "disabled");
  assert.equal(Control.captionState(fakeControl({
    "aria-label": "Microphone",
    "aria-pressed": "false",
  })), "unknown");
});

test("recognizes state labels and Meet mute-state attributes", () => {
  assert.equal(Control.captionState(fakeControl({
    "aria-label": "Captions are off",
  })), "disabled");
  assert.equal(Control.captionState(fakeControl({
    "aria-label": "Captions",
    "data-is-muted": "false",
  })), "enabled");
});

test("finds a visible enable control and never selects the extension widget", () => {
  const hidden = fakeControl(
    { "aria-label": "Turn on captions" },
    { visible: false }
  );
  const widget = fakeControl(
    { "aria-label": "Turn on captions" },
    { inWidget: true }
  );
  const alreadyEnabled = fakeControl({ "aria-label": "Turn off captions" });
  const enable = fakeControl({ "data-tooltip": "Turn on captions" });
  const documentRoot = fakeDocument([hidden, widget, alreadyEnabled, enable]);

  assert.equal(Control.findCaptionButton(documentRoot), alreadyEnabled);
  assert.equal(Control.findEnableButton(documentRoot), enable);
});

test("does not guess when a caption control has no state signal", () => {
  const ambiguous = fakeControl({ "aria-label": "Captions settings" });
  assert.equal(Control.captionState(ambiguous), "unknown");
  assert.equal(Control.findCaptionButton(fakeDocument([ambiguous])), null);
});

test("diagnostics contain only structural caption-control details", () => {
  const control = fakeControl({
    "aria-label": "Captions are off",
    "aria-pressed": "false",
  });
  const diagnostic = Control.diagnose(fakeDocument([control]));
  assert.equal(diagnostic.possibleControls, 1);
  assert.deepEqual(diagnostic.controls[0], {
    tag: "button",
    role: "",
    label: "Captions are off",
    state: "disabled",
    visible: true,
    disabled: false,
    ariaPressed: "false",
    ariaChecked: "",
    dataIsMuted: "",
    dataState: "",
  });
  assert.deepEqual(diagnostic.captionRegions, []);
});

function fakeLayoutElement({
  tagName = "DIV",
  role = "",
  height = 0,
  media = false,
  interactive = [],
  contained = [],
  className = "",
} = {}) {
  return {
    tagName,
    className,
    parentElement: null,
    getAttribute(name) {
      return name === "role" ? role : null;
    },
    getBoundingClientRect() {
      return { x: 0, y: 0, width: 1200, height };
    },
    querySelector(selector) {
      return media && selector.includes("video") ? {} : null;
    },
    querySelectorAll(selector) {
      return selector.includes("button") ? interactive : [];
    },
    contains(element) {
      return contained.includes(element);
    },
  };
}

test("selects the outermost caption-only surface before the Meet stage", () => {
  const region = fakeLayoutElement({ height: 50 });
  const scrollBox = fakeLayoutElement({ height: 204 });
  const captionShell = fakeLayoutElement({ height: 216 });
  const absoluteOverlay = fakeLayoutElement({ height: 216 });
  const stage = fakeLayoutElement({ height: 0, media: true });
  region.parentElement = scrollBox;
  scrollBox.parentElement = captionShell;
  captionShell.parentElement = absoluteOverlay;
  absoluteOverlay.parentElement = stage;
  const documentRoot = {
    body: fakeLayoutElement({ tagName: "BODY" }),
    documentElement: fakeLayoutElement({ tagName: "HTML" }),
    defaultView: { innerHeight: 796 },
  };

  assert.equal(
    Control.findCaptionVisualSurface(region, documentRoot),
    absoluteOverlay
  );
});

test("does not hide an ancestor containing controls or most of the viewport", () => {
  const meetingButton = {};
  const region = fakeLayoutElement({ height: 50 });
  const captionShell = fakeLayoutElement({ height: 180 });
  const controls = fakeLayoutElement({
    height: 220,
    interactive: [meetingButton],
  });
  region.parentElement = captionShell;
  captionShell.parentElement = controls;
  const documentRoot = {
    defaultView: { innerHeight: 800 },
  };
  assert.equal(Control.findCaptionVisualSurface(region, documentRoot), captionShell);

  controls.querySelectorAll = () => [];
  controls.getBoundingClientRect = () => ({ height: 500 });
  assert.equal(Control.findCaptionVisualSurface(region, documentRoot), captionShell);
});

test("caption-local controls do not prevent selecting the outer shell", () => {
  const captionButton = {};
  const region = fakeLayoutElement({
    height: 50,
    contained: [captionButton],
  });
  const scrollBox = fakeLayoutElement({
    height: 204,
    interactive: [captionButton],
    contained: [captionButton],
  });
  const outerShell = fakeLayoutElement({
    height: 216,
    interactive: [captionButton],
    contained: [captionButton],
  });
  const meetingButton = {};
  const stage = fakeLayoutElement({
    height: 0,
    interactive: [captionButton, meetingButton],
  });
  region.parentElement = scrollBox;
  scrollBox.parentElement = outerShell;
  outerShell.parentElement = stage;

  assert.equal(Control.findCaptionVisualSurface(region, {
    defaultView: { innerHeight: 796 },
  }), outerShell);
});
