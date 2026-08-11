"use strict";

importScripts("caption-model.js", "auto-export.js");

const SETTINGS_KEY = "meeting-transcriber:settings";
const TAB_KEY_PREFIX = "meeting-transcriber:tab:";

function tabKey(tabId) {
  return `${TAB_KEY_PREFIX}${tabId}`;
}

chrome.runtime.onMessage.addListener((message, sender) => {
  const tabId = sender.tab?.id;
  if (!Number.isInteger(tabId) || !message?.meetingCode) return;

  if (message.type === "meeting-transcriber:register") {
    chrome.storage.session.set({
      [tabKey(tabId)]: {
        meetingCode: message.meetingCode,
        storageKey: message.storageKey,
        lastExportSignature: "",
      },
    });
  }

  if (message.type === "meeting-transcriber:exported") {
    chrome.storage.session.get(tabKey(tabId)).then((stored) => {
      const registration = stored[tabKey(tabId)];
      if (!registration) return;
      registration.lastExportSignature = message.signature || "";
      return chrome.storage.session.set({ [tabKey(tabId)]: registration });
    });
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  exportClosedTab(tabId).catch((error) => {
    console.error("Meeting Transcriber auto-export failed", error);
  });
});

async function exportClosedTab(tabId) {
  // Let content.js finish its immediate storage flush and exported-signature
  // message before the service worker evaluates the fallback export.
  await new Promise((resolve) => setTimeout(resolve, 350));
  const key = tabKey(tabId);
  const registered = await chrome.storage.session.get(key);
  const registration = registered[key];
  await chrome.storage.session.remove(key);
  if (!registration) return;

  const stored = await chrome.storage.local.get([
    registration.storageKey,
    SETTINGS_KEY,
  ]);
  if (stored[SETTINGS_KEY]?.autoExportEnabled === false) return;

  const state = stored[registration.storageKey];
  if (!state?.entries?.length) return;
  const exported = MeetingCaptionModel.exportState(state);
  const signature = MeetingAutoExport.signature(exported);
  if (signature === registration.lastExportSignature) return;

  const content = `${JSON.stringify(exported, null, 2)}\n`;
  const url = `data:application/json;charset=utf-8,${encodeURIComponent(content)}`;
  await chrome.downloads.download({
    url,
    filename: MeetingAutoExport.filename(exported),
    conflictAction: "uniquify",
    saveAs: false,
  });
}
