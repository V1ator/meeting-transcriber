(function () {
  "use strict";

  const Model = globalThis.MeetingCaptionModel;
  const CaptionToggle = globalThis.MeetingCaptionToggle;
  const AutoExport = globalThis.MeetingAutoExport;
  const RtcFallback = globalThis.MeetingRtcFallback;
  const STORAGE_PREFIX = "meeting-transcriber:";
  const SETTINGS_KEY = "meeting-transcriber:settings";
  const HIDE_NATIVE_CLASS = "meeting-transcriber-hide-native-captions";
  const FINALIZE_DELAY_MS = 1800;
  const AUTO_ENABLE_RETRY_MS = 8_000;
  const AUTO_ENABLE_MAX_ATTEMPTS = 3;
  const RTC_FALLBACK_DELAY_MS = 20_000;
  const RTC_EVENT_NAME = "meeting-transcriber:rtc";
  const RTC_COMMAND_NAME = "meeting-transcriber:rtc-command";
  const RTC_SESSION_NONCE = globalThis.crypto.randomUUID();
  const WIDGET_EDGE_MARGIN = 8;
  const KNOWN_REGION_SELECTOR = '[role="region"].vNKgIf.UDinHf';
  const CAPTION_HOST_MARKER = "data-meeting-transcriber-caption-host";
  const CAPTION_SHELL_MARKER = "data-meeting-transcriber-caption-shell";
  const STAGE_MARKER = "data-meeting-transcriber-expanded-stage";
  const STAGE_FILL_MARKER = "data-meeting-transcriber-stage-fill";
  const STAGE_VIDEO_MARKER = "data-meeting-transcriber-stage-video";
  const STAGE_TILE_MARKER = "data-meeting-transcriber-stage-tile";
  const ENTRY_SELECTOR = ".nMcdL";
  const SPEAKER_SELECTOR = ".NWpY1d";
  const TEXT_SELECTOR = ".ygicle";
  const CHAT_TEXT_SELECTOR =
    ".oIy2qc, [data-meeting-transcriber-chat-text]";
  const CHAT_SPEAKER_SELECTOR =
    ".YTbUzc, [data-sender-name], [data-meeting-transcriber-chat-speaker]";
  const CHAT_CONTAINER_SELECTOR =
    "[data-message-id], [data-meeting-transcriber-chat-message], [role=listitem], .z38b6";
  const PARTICIPANT_NAME_SELECTOR = [
    "[data-participant-name]",
    "[data-self-name]",
    "[data-meeting-transcriber-participant-name]",
  ].join(", ");
  const PARTICIPANT_CONTAINER_SELECTOR =
    "[data-participant-id], [data-requested-participant-id]";

  let meetingCode = "";
  let state = null;
  let paused = false;
  let observer = null;
  let observedRegion = null;
  let scanTimer = null;
  let saveTimer = null;
  let settingsSaveTimer = null;
  let startedEpoch = Date.now();
  let nodeSerial = 1;
  let autoEnableCaptions = true;
  let hideNativeCaptions = true;
  let autoExportEnabled = true;
  let captureChat = true;
  let rtcCaptureEnabled = true;
  let domFallbackEnabled = false;
  let rtcFallbackActive = false;
  let rtcUnavailable = false;
  let rtcFallbackTimer = null;
  let rtcFallbackDeadline = 0;
  let rtcFallbackRequiresReconnect = false;
  let audioFallbackRequestState = "idle";
  let audioFallbackResetTimer = null;
  let rtcStatus = {
    ready: false,
    channelState: "missing",
    peerConnections: 0,
    packets: 0,
    decoded: 0,
    failures: 0,
    reason: "",
  };
  let widgetPosition = null;
  let lastExportSignature = "";
  let autoEnableAttempts = 0;
  let autoEnablePendingUntil = 0;
  let captionsWereEnabled = false;
  let lastCaptionActivityEpoch = 0;
  let lastCaptionFingerprint = "";
  let compactedState = null;
  let compactedRevision = -1;
  let compactedEntries = [];
  const nodeKeys = new WeakMap();
  const liveKeys = new Set();
  const finalizeTimers = new Map();
  const chatSeenKeys = new Set();
  const memoryStorage = {};
  const rtcSpeakerNames = new Map();

  // RTC is the default path. Never inherit native-caption hiding from a
  // previous content-script instance unless diagnostic DOM capture is active.
  document.documentElement.classList.remove(HIDE_NATIVE_CLASS);

  function currentMeetingCode() {
    const match = location.pathname.match(/^\/([a-z]{3}-[a-z]{4}-[a-z]{3})(?:\/|$)/i);
    if (match) return match[1].toLowerCase();
    if (["localhost", "127.0.0.1"].includes(location.hostname)) {
      return new URLSearchParams(location.search).get("meetingCode") || "";
    }
    return "";
  }

  function storageKey() {
    return `${STORAGE_PREFIX}${meetingCode}`;
  }

  function pageMeetingTitle() {
    return document.title
      .replace(/\s*[-–—]\s*Google Meet\s*$/i, "")
      .trim() || "Google Meet";
  }

  function memoryGet(keys) {
    return Object.fromEntries(
      keys.filter((key) => Object.hasOwn(memoryStorage, key))
        .map((key) => [key, memoryStorage[key]])
    );
  }

  async function storageGet(keys) {
    try {
      const area = globalThis.chrome?.storage?.local;
      if (!area) return memoryGet(keys);
      const stored = await area.get(keys);
      Object.assign(memoryStorage, stored);
      return stored;
    } catch {
      return memoryGet(keys);
    }
  }

  async function storageSet(values) {
    Object.assign(memoryStorage, values);
    try {
      await globalThis.chrome?.storage?.local?.set(values);
    } catch {
      // The extension may have been reloaded while this Meet tab stayed open.
    }
  }

  async function storageRemove(key) {
    delete memoryStorage[key];
    try {
      await globalThis.chrome?.storage?.local?.remove(key);
    } catch {
      // Keep the in-memory session usable until the Meet tab is refreshed.
    }
  }

  async function pruneOldMeetingStates() {
    const area = globalThis.chrome?.storage?.local;
    if (!area) return;
    try {
      const stored = await area.get(null);
      const cutoff = Date.now() - 12 * 60 * 60 * 1000;
      const stale = Object.entries(stored)
        .filter(([key, value]) => (
          key.startsWith(STORAGE_PREFIX)
          && key !== SETTINGS_KEY
          && key !== storageKey()
          && Date.parse(value?.startedAt || "") < cutoff
        ))
        .map(([key]) => key);
      if (stale.length) await area.remove(stale);
    } catch {
      // Cleanup is best-effort and must not interrupt capture.
    }
  }

  function elapsedMs() {
    return Math.max(0, Date.now() - startedEpoch);
  }

  async function loadState() {
    const stored = await storageGet([storageKey(), SETTINGS_KEY]);
    let seed = stored[storageKey()] || {};
    const seedStartedAt = Date.parse(seed.startedAt || "");
    if (Number.isFinite(seedStartedAt)
        && Date.now() - seedStartedAt > 12 * 60 * 60 * 1000) {
      seed = {};
      await storageRemove(storageKey());
    }
    state = Model.createState({
      ...seed,
      meetingCode,
      meetingTitle: pageMeetingTitle(),
      language: seed.language || "uk",
    });
    if (state.startedAt) {
      const parsed = Date.parse(state.startedAt);
      if (Number.isFinite(parsed)) startedEpoch = parsed;
    }
    autoEnableCaptions = stored[SETTINGS_KEY]?.autoEnableCaptions !== false;
    hideNativeCaptions = stored[SETTINGS_KEY]?.hideNativeCaptions !== false;
    autoExportEnabled = stored[SETTINGS_KEY]?.autoExportEnabled !== false;
    captureChat = stored[SETTINGS_KEY]?.captureChat !== false;
    rtcCaptureEnabled = stored[SETTINGS_KEY]?.rtcCaptureEnabled !== false;
    domFallbackEnabled = stored[SETTINGS_KEY]?.domFallbackEnabled === true;
    const savedPosition = stored[SETTINGS_KEY]?.widgetPosition;
    if (Number.isFinite(savedPosition?.x) && Number.isFinite(savedPosition?.y)) {
      widgetPosition = { x: savedPosition.x, y: savedPosition.y };
    }
    applyNativeCaptionVisibility();
  }

  async function saveSettings() {
    await storageSet({
      [SETTINGS_KEY]: {
        autoEnableCaptions,
        hideNativeCaptions,
        autoExportEnabled,
        captureChat,
        rtcCaptureEnabled,
        domFallbackEnabled,
        widgetPosition,
      },
    });
  }

  function scheduleSettingsSave() {
    clearTimeout(settingsSaveTimer);
    settingsSaveTimer = setTimeout(() => {
      saveSettings().catch((error) => {
        console.error("Meeting Transcriber failed to save settings", error);
      });
    }, 200);
  }

  function scheduleSave() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(async () => {
      await storageSet({ [storageKey()]: state });
    }, 250);
  }

  function keyForNode(node) {
    if (!nodeKeys.has(node)) nodeKeys.set(node, `caption-${nodeSerial++}`);
    return nodeKeys.get(node);
  }

  function scheduleFinalize(key) {
    clearTimeout(finalizeTimers.get(key));
    finalizeTimers.set(key, setTimeout(() => {
      Model.finalize(state, key, elapsedMs());
      finalizeTimers.delete(key);
      scheduleSave();
      render();
    }, FINALIZE_DELAY_MS));
  }

  function processEntry(entry, replayScan = false) {
    const textNode = entry.querySelector(TEXT_SELECTOR);
    if (!textNode) return null;
    if (!state.startedAt) startedEpoch = Date.now();
    const key = keyForNode(entry);
    const item = Model.observe(state, {
      key,
      speaker: entry.querySelector(SPEAKER_SELECTOR)?.textContent,
      text: textNode.textContent,
      replayScan,
      atMs: elapsedMs(),
      observedAt: new Date().toISOString(),
    });
    if (!item) return null;
    scheduleFinalize(key);
    return key;
  }

  function rtcCommand(type) {
    document.dispatchEvent(new CustomEvent(RTC_COMMAND_NAME, {
      detail: { type, nonce: RTC_SESSION_NONCE },
    }));
  }

  function normalizeRtcDeviceId(value) {
    return String(value || "").replace(/^@/, "");
  }

  function fallbackNameForDevice(deviceId) {
    const normalized = normalizeRtcDeviceId(deviceId);
    const tail = normalized.split("/").pop();
    return `Учасник ${String(tail || normalized).slice(-6)}`;
  }

  function registerRtcSpeaker(deviceId, name) {
    const normalized = normalizeRtcDeviceId(deviceId);
    const cleaned = Model.cleanSpeaker(name);
    if (!normalized || cleaned === "Невідомий") return false;
    const fallbackName = fallbackNameForDevice(normalized);
    rtcSpeakerNames.set(normalized, cleaned);
    rtcSpeakerNames.set(normalized.split("/").pop(), cleaned);
    if (state) {
      state.entries.forEach((entry) => {
        if (entry.speaker === fallbackName) entry.speaker = cleaned;
      });
      state.participants = (state.participants || []).filter(
        (participant) => participant !== fallbackName
      );
      Model.addParticipant(state, cleaned);
      scheduleSave();
    }
    return true;
  }

  function participantNameForDevice(deviceId) {
    const normalized = normalizeRtcDeviceId(deviceId);
    if (!normalized) return "Невідомий";
    if (rtcSpeakerNames.has(normalized)) return rtcSpeakerNames.get(normalized);

    const tail = normalized.split("/").pop();
    const fallbackName = fallbackNameForDevice(normalized);
    const container = Array.from(
      document.querySelectorAll(PARTICIPANT_CONTAINER_SELECTOR)
    ).find((candidate) => {
      const id = candidate.getAttribute("data-participant-id")
        || candidate.getAttribute("data-requested-participant-id")
        || "";
      return id === normalized
        || id === `@${normalized}`
        || (tail && (id.endsWith(`/${tail}`) || id.endsWith(`@${tail}`)));
    });
    if (!container) return fallbackName;
    const named = container.matches(PARTICIPANT_NAME_SELECTOR)
      ? container
      : container.querySelector(PARTICIPANT_NAME_SELECTOR);
    const name = named?.getAttribute("data-participant-name")
      || named?.getAttribute("data-self-name")
      || named?.getAttribute("data-meeting-transcriber-participant-name")
      || named?.textContent
      || container.querySelector("img[alt]")?.getAttribute("alt");
    const cleaned = Model.cleanSpeaker(name);
    if (cleaned === "Невідомий") return fallbackName;
    registerRtcSpeaker(normalized, cleaned);
    return cleaned;
  }

  function processRtcCaption(message) {
    if (!rtcCaptureEnabled || paused || !state) return;
    const text = Model.normalizeText(message?.text);
    const deviceId = normalizeRtcDeviceId(message?.deviceId);
    const messageId = String(message?.messageId || "");
    const messageVersion = Number(message?.messageVersion);
    if (!text || !deviceId || !messageId
        || text.length > 20_000 || deviceId.length > 500 || messageId.length > 500
        || !Number.isSafeInteger(messageVersion) || messageVersion < 0) return;
    if (!state.startedAt) startedEpoch = Date.now();
    rtcFallbackActive = false;
    rtcUnavailable = false;
    rtcFallbackDeadline = 0;
    rtcFallbackRequiresReconnect = false;
    clearTimeout(rtcFallbackTimer);
    applyNativeCaptionVisibility();
    lastCaptionActivityEpoch = Date.now();
    const key = `rtc-${messageId}`;
    const item = Model.observeVersioned(state, {
      key,
      version: messageVersion,
      speaker: participantNameForDevice(deviceId),
      text,
      atMs: elapsedMs(),
      observedAt: new Date().toISOString(),
    });
    if (!item) return;
    scheduleFinalize(key);
    scheduleSave();
    render();
  }

  function scheduleRtcFallback() {
    clearTimeout(rtcFallbackTimer);
    if (!rtcCaptureEnabled) return;
    const now = Date.now();
    const effectiveDecoded = rtcFallbackRequiresReconnect
      ? 0
      : rtcStatus.decoded;
    rtcFallbackDeadline = RtcFallback.arm(
      rtcFallbackDeadline,
      now,
      RTC_FALLBACK_DELAY_MS,
      effectiveDecoded,
      rtcStatus.channelState
    );
    if (!rtcFallbackDeadline) return;
    rtcFallbackTimer = setTimeout(() => {
      if (!RtcFallback.isDue(
        rtcFallbackDeadline,
        Date.now(),
        rtcFallbackRequiresReconnect ? 0 : rtcStatus.decoded,
        rtcStatus.channelState
      )) {
        scheduleRtcFallback();
        return;
      }
      const recovery = RtcFallback.recovery(domFallbackEnabled);
      rtcUnavailable = recovery.rtcUnavailable;
      rtcFallbackActive = recovery.domFallbackActive;
      applyNativeCaptionVisibility();
      if (rtcFallbackActive) maybeEnableCaptions();
      render();
    }, Math.max(0, rtcFallbackDeadline - now));
  }

  function handleRtcEvent(event) {
    const detail = event.detail || {};
    if (detail.nonce !== RTC_SESSION_NONCE) return;
    if (detail.type === "ready") {
      rtcStatus.ready = true;
      if (rtcCaptureEnabled) rtcCommand("start");
      render();
      return;
    }
    if (detail.type === "caption") {
      processRtcCaption(detail.message);
      return;
    }
    if (detail.type === "devices") {
      const devices = Array.isArray(detail.devices) ? detail.devices.slice(0, 500) : [];
      devices.forEach(({ deviceId, deviceName }) => {
        if (String(deviceId || "").length > 500
            || String(deviceName || "").length > 500) return;
        registerRtcSpeaker(deviceId, deviceName);
      });
      render();
      return;
    }
    if (detail.type !== "status") return;
    rtcStatus = {
      ...rtcStatus,
      ...detail,
      ready: rtcStatus.ready,
      reason: detail.reason || "",
    };
    if (
      detail.reason === "unsupported"
      || (detail.failures >= 3 && detail.decoded === 0)
    ) {
      const recovery = RtcFallback.recovery(domFallbackEnabled);
      rtcUnavailable = recovery.rtcUnavailable;
      rtcFallbackActive = recovery.domFallbackActive;
      rtcFallbackDeadline = 0;
      clearTimeout(rtcFallbackTimer);
      applyNativeCaptionVisibility();
      if (rtcFallbackActive) maybeEnableCaptions();
    } else if (["closed", "channel-error"].includes(detail.reason)) {
      rtcFallbackRequiresReconnect = true;
      rtcCommand("retry");
      scheduleRtcFallback();
    } else if (detail.channelState === "open") {
      rtcFallbackActive = false;
      rtcUnavailable = false;
      rtcFallbackDeadline = 0;
      rtcFallbackRequiresReconnect = false;
      clearTimeout(rtcFallbackTimer);
      applyNativeCaptionVisibility();
    }
    render();
  }

  function findCaptionRegion() {
    const known = document.querySelector(KNOWN_REGION_SELECTOR);
    const semantic = Array.from(document.querySelectorAll(
      '[role="region"], [aria-live="polite"], [aria-live="assertive"]'
    )).find((candidate) => {
      const label = [
        candidate.getAttribute("aria-label"),
        candidate.getAttribute("data-tooltip"),
        candidate.getAttribute("title"),
      ].filter(Boolean).join(" ");
      return CaptionToggle.isCaptionLabel(label);
    });
    const region = known || semantic || Array.from(
      document.querySelectorAll('[role="region"]')
    ).find((candidate) => candidate.querySelector(ENTRY_SELECTOR)) || null;
    if (region) {
      region.setAttribute("data-meeting-transcriber-caption-region", "");
      region.setAttribute(
        "aria-hidden",
        rtcFallbackActive && hideNativeCaptions ? "true" : "false"
      );
      markCaptionSurface(region);
    }
    return region;
  }

  function markCaptionSurface(region) {
    const preferredHost = region.closest('[jsname="tgaKEf"]');
    const surface = preferredHost && ![document.body, document.documentElement].includes(
      preferredHost
    ) ? preferredHost : region;
    document.querySelectorAll(`[${CAPTION_HOST_MARKER}]`).forEach((element) => {
      if (element !== surface) element.removeAttribute(CAPTION_HOST_MARKER);
    });
    surface.setAttribute(CAPTION_HOST_MARKER, "");

    const shells = new Set();
    let candidate = surface.parentElement;
    for (let depth = 0; candidate && depth < 3; depth += 1) {
      if ([document.body, document.documentElement].includes(candidate)) break;
      if (candidate.matches("main, [role=main], [role=toolbar]")) break;
      if (candidate.querySelector("video, canvas, [data-participant-id]")) break;
      const unrelatedControl = Array.from(candidate.querySelectorAll(
        "button, [role=button]"
      )).some((control) => {
        const label = [
          control.getAttribute("aria-label"),
          control.getAttribute("data-tooltip"),
          control.getAttribute("title"),
          control.textContent,
        ].filter(Boolean).join(" ");
        return !CaptionToggle.isCaptionLabel(label);
      });
      if (unrelatedControl) break;
      const bounds = candidate.getBoundingClientRect();
      if (bounds.height > 360 || bounds.height > window.innerHeight * 0.45) break;
      shells.add(candidate);
      candidate = candidate.parentElement;
    }
    document.querySelectorAll(`[${CAPTION_SHELL_MARKER}]`).forEach((element) => {
      if (!shells.has(element)) element.removeAttribute(CAPTION_SHELL_MARKER);
    });
    shells.forEach((element) => element.setAttribute(CAPTION_SHELL_MARKER, ""));
  }

  function clearLegacyMeetingLayoutOverrides() {
    // Стабільний режим не змінює геометрію Meet. Очищення потрібне після
    // reload із версій, які ставили layout-маркери та inline CSS.
    document.querySelectorAll(`[${STAGE_MARKER}]`).forEach((stage) => {
      stage.removeAttribute(STAGE_MARKER);
      stage.style.removeProperty("--meeting-transcriber-stage-bottom");
    });
    document.querySelectorAll(
      `[${STAGE_FILL_MARKER}], [${STAGE_VIDEO_MARKER}], [${STAGE_TILE_MARKER}]`
    ).forEach((element) => {
      element.removeAttribute(STAGE_FILL_MARKER);
      element.removeAttribute(STAGE_VIDEO_MARKER);
      element.removeAttribute(STAGE_TILE_MARKER);
      element.style.removeProperty("--meeting-transcriber-tile-left");
      element.style.removeProperty("--meeting-transcriber-tile-top");
      element.style.removeProperty("--meeting-transcriber-tile-width");
      element.style.removeProperty("--meeting-transcriber-tile-height");
    });
  }

  function applyNativeCaptionVisibility() {
    const hideCaptions = domFallbackEnabled
      && rtcFallbackActive
      && hideNativeCaptions;
    document.documentElement.classList.toggle(
      HIDE_NATIVE_CLASS, hideCaptions
    );
    if (!domFallbackEnabled || !rtcFallbackActive) {
      document.querySelectorAll(
        `[data-meeting-transcriber-caption-region], `
        + `[${CAPTION_HOST_MARKER}], [${CAPTION_SHELL_MARKER}]`
      ).forEach((element) => {
        element.setAttribute("aria-hidden", "false");
        element.removeAttribute("data-meeting-transcriber-caption-region");
        element.removeAttribute(CAPTION_HOST_MARKER);
        element.removeAttribute(CAPTION_SHELL_MARKER);
      });
      clearLegacyMeetingLayoutOverrides();
      return;
    }
    const region = findCaptionRegion();
    if (region) {
      region.setAttribute("aria-hidden", hideCaptions ? "true" : "false");
    }
    clearLegacyMeetingLayoutOverrides();
  }

  function scan() {
    if (paused || !state) return;
    if (!rtcFallbackActive) {
      setStatus(
        rtcUnavailable || !rtcCaptureEnabled
          ? "rtc-unavailable"
          : rtcStatus.channelState === "open" || rtcStatus.decoded > 0
          ? "capturing-rtc"
          : "connecting"
      );
      return;
    }
    const region = findCaptionRegion();
    if (!region) {
      setStatus("waiting");
      return;
    }
    setStatus("capturing");
    const current = new Set();
    const entries = Array.from(region.querySelectorAll(ENTRY_SELECTOR));
    const fingerprint = entries.map((entry) => [
      entry.querySelector(SPEAKER_SELECTOR)?.textContent || "",
      entry.querySelector(TEXT_SELECTOR)?.textContent || "",
    ].join("\u0000")).join("\u0001");
    if (fingerprint && fingerprint !== lastCaptionFingerprint) {
      lastCaptionFingerprint = fingerprint;
      lastCaptionActivityEpoch = Date.now();
    }
    const replayScan = entries.some((entry) => Model.isHistoricalReplay(
      state,
      entry.querySelector(SPEAKER_SELECTOR)?.textContent,
      entry.querySelector(TEXT_SELECTOR)?.textContent
    ));
    entries.forEach((entry) => {
      const key = processEntry(entry, replayScan);
      if (key) current.add(key);
    });
    liveKeys.forEach((key) => {
      if (!current.has(key)) {
        clearTimeout(finalizeTimers.get(key));
        finalizeTimers.delete(key);
        Model.finalize(state, key, elapsedMs());
      }
    });
    liveKeys.clear();
    current.forEach((key) => liveKeys.add(key));
    scheduleSave();
    render();
  }

  function chatMessageKey(container, speaker, text) {
    const stableId = container?.getAttribute("data-message-id")
      || container?.getAttribute("data-meeting-transcriber-chat-message");
    return stableId || `${speaker}\u0000${Model.normalizeText(text)}`;
  }

  function scanChat() {
    if (paused || !captureChat || !state) return;
    let added = false;
    document.querySelectorAll(CHAT_TEXT_SELECTOR).forEach((textNode) => {
      const container = textNode.closest(CHAT_CONTAINER_SELECTOR)
        || textNode.parentElement;
      const speakerNode = container?.querySelector(CHAT_SPEAKER_SELECTOR);
      const speaker = speakerNode?.getAttribute("data-sender-name")
        || speakerNode?.textContent;
      const text = textNode.textContent;
      if (!speaker || !Model.normalizeText(text)) return;
      const semanticKey = chatMessageKey(container, Model.cleanSpeaker(speaker), text);
      if (chatSeenKeys.has(semanticKey)) return;
      chatSeenKeys.add(semanticKey);
      const key = `chat-${semanticKey}`;
      const item = Model.observe(state, {
        key,
        kind: "chat",
        speaker,
        text,
        atMs: elapsedMs(),
        observedAt: new Date().toISOString(),
      });
      if (!item) return;
      Model.finalize(state, key, elapsedMs());
      added = true;
    });
    if (added) {
      scheduleSave();
      render();
    }
  }

  function scanParticipants() {
    if (!state) return;
    let changed = false;
    const title = pageMeetingTitle();
    if (title !== "Google Meet" && state.meetingTitle !== title) {
      state.meetingTitle = title;
      changed = true;
    }
    document.querySelectorAll(PARTICIPANT_NAME_SELECTOR).forEach((element) => {
      const name = element.getAttribute("data-participant-name")
        || element.getAttribute("data-self-name")
        || element.getAttribute("data-meeting-transcriber-participant-name")
        || element.textContent;
      if (Model.addParticipant(state, name)) changed = true;
    });
    document.querySelectorAll(PARTICIPANT_CONTAINER_SELECTOR).forEach((container) => {
      const named = container.matches(PARTICIPANT_NAME_SELECTOR)
        ? container
        : container.querySelector(PARTICIPANT_NAME_SELECTOR);
      const avatar = container.querySelector("img[alt]");
      const name = named?.getAttribute("data-participant-name")
        || named?.getAttribute("data-self-name")
        || named?.getAttribute("data-meeting-transcriber-participant-name")
        || named?.textContent
        || avatar?.getAttribute("alt");
      if (Model.addParticipant(state, name)) changed = true;
    });
    if (changed) scheduleSave();
  }

  function connectObserver() {
    if (!domFallbackEnabled || !rtcFallbackActive) {
      if (observer) observer.disconnect();
      observer = null;
      observedRegion = null;
      return;
    }
    const region = findCaptionRegion();
    if (region === observedRegion) return;
    if (observer) observer.disconnect();
    observedRegion = region;
    if (!region) return;
    observer = new MutationObserver(() => {
      clearTimeout(scanTimer);
      scanTimer = setTimeout(scan, 80);
    });
    observer.observe(region, { childList: true, subtree: true, characterData: true });
    scan();
  }

  function maybeEnableCaptions() {
    if (!domFallbackEnabled || !rtcFallbackActive) return;
    const region = findCaptionRegion();
    if (region) {
      captionsWereEnabled = true;
      autoEnablePendingUntil = 0;
      return;
    }
    if (!autoEnableCaptions || captionsWereEnabled
        || autoEnableAttempts >= AUTO_ENABLE_MAX_ATTEMPTS) {
      return;
    }
    if (Date.now() < autoEnablePendingUntil) return;
    const button = CaptionToggle.findEnableButton(document);
    if (!button) return;
    autoEnableAttempts += 1;
    autoEnablePendingUntil = Date.now() + AUTO_ENABLE_RETRY_MS;
    setStatus("enabling");
    button.click();
  }

  function download(filename, content, type) {
    const url = URL.createObjectURL(new Blob([content], { type }));
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function notifyBackground(type, extra = {}) {
    try {
      const request = globalThis.chrome?.runtime?.sendMessage?.({
        type,
        meetingCode,
        storageKey: storageKey(),
        ...extra,
      });
      request?.catch?.(() => {});
    } catch {
      // Direct JSON export still works if the extension context was invalidated.
    }
  }

  function downloadJsonExport(exported) {
    const signature = AutoExport.signature(exported);
    download(
      AutoExport.filename(exported),
      `${JSON.stringify(exported, null, 2)}\n`,
      "application/json"
    );
    lastExportSignature = signature;
    notifyBackground("meeting-transcriber:exported", { signature });
  }

  function exportJson() {
    scanParticipants();
    downloadJsonExport(Model.exportState(state));
  }

  function autoExportJson() {
    if (!autoExportEnabled || !state) return false;
    scan();
    scanParticipants();
    const exported = Model.exportState(state);
    if (!exported.entries.length) return false;
    const signature = AutoExport.signature(exported);
    if (signature === lastExportSignature) return false;
    // Flush the latest in-memory captions immediately so the background
    // fallback does not read a state delayed by the normal 250 ms debounce.
    storageSet({ [storageKey()]: state }).catch(() => {});
    downloadJsonExport(exported);
    return true;
  }

  function formatOffset(milliseconds) {
    const total = Math.floor(milliseconds / 1000);
    const hours = String(Math.floor(total / 3600)).padStart(2, "0");
    const minutes = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
    const seconds = String(total % 60).padStart(2, "0");
    return `${hours}:${minutes}:${seconds}`;
  }

  function formatMeetingTime(value) {
    const parsed = new Date(value);
    if (!Number.isFinite(parsed.getTime())) return String(value || "");
    return new Intl.DateTimeFormat("uk-UA", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      timeZoneName: "short",
    }).format(parsed);
  }

  function exportMarkdown() {
    scanParticipants();
    const exported = Model.exportState(state);
    const title = exported.meetingTitle || "Google Meet";
    const participants = exported.participants?.length
      ? exported.participants
      : ["Не визначено"];
    const lines = [
      `# ${title}`,
      "",
      `- **Час початку:** ${formatMeetingTime(exported.startedAt)}`,
      `- **Назва зустрічі:** ${title}`,
      "",
      "**Учасники:**",
      ...participants.map((participant) => `- ${participant}`),
      "",
      "## Транскрипт",
      "",
    ];
    exported.entries.forEach((entry) => {
      const speaker = entry.kind === "chat"
        ? `${entry.speaker} (chat)`
        : entry.speaker;
      lines.push(`[${formatOffset(entry.startMs)}] ${speaker}: ${entry.text}`);
    });
    download(`meet-${meetingCode}.md`, `${lines.join("\n")}\n`, "text/markdown");
  }

  function widget() {
    return document.getElementById("meeting-transcriber-widget");
  }

  function clampWidgetPosition(root, x, y) {
    const rect = root.getBoundingClientRect();
    const maxX = Math.max(
      WIDGET_EDGE_MARGIN,
      window.innerWidth - rect.width - WIDGET_EDGE_MARGIN
    );
    const maxY = Math.max(
      WIDGET_EDGE_MARGIN,
      window.innerHeight - rect.height - WIDGET_EDGE_MARGIN
    );
    return {
      x: Math.min(Math.max(x, WIDGET_EDGE_MARGIN), maxX),
      y: Math.min(Math.max(y, WIDGET_EDGE_MARGIN), maxY),
    };
  }

  function applyWidgetPosition(root, position = widgetPosition) {
    if (!position) {
      root.style.removeProperty("left");
      root.style.removeProperty("top");
      root.style.removeProperty("right");
      return;
    }
    widgetPosition = clampWidgetPosition(root, position.x, position.y);
    root.style.left = `${widgetPosition.x}px`;
    root.style.top = `${widgetPosition.y}px`;
    root.style.right = "auto";
  }

  function enableWidgetDragging(root) {
    const handle = root.querySelector("header");
    let drag = null;

    handle.title =
      "Перетягніть віджет. Подвійний клік повертає стандартну позицію.";

    handle.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.target.closest("button")) return;
      const rect = root.getBoundingClientRect();
      drag = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        widgetX: rect.left,
        widgetY: rect.top,
      };
      root.classList.add("dragging");
      handle.setPointerCapture(event.pointerId);
      event.preventDefault();
    });

    handle.addEventListener("pointermove", (event) => {
      if (!drag || event.pointerId !== drag.pointerId) return;
      applyWidgetPosition(root, {
        x: drag.widgetX + event.clientX - drag.startX,
        y: drag.widgetY + event.clientY - drag.startY,
      });
      scheduleSettingsSave();
    });

    const finishDrag = (event) => {
      if (!drag || event.pointerId !== drag.pointerId) return;
      if (handle.hasPointerCapture(event.pointerId)) {
        handle.releasePointerCapture(event.pointerId);
      }
      drag = null;
      root.classList.remove("dragging");
      saveSettings().catch((error) => {
        console.error("Meeting Transcriber failed to save widget position", error);
      });
    };
    handle.addEventListener("pointerup", finishDrag);
    handle.addEventListener("pointercancel", finishDrag);

    handle.addEventListener("dblclick", (event) => {
      if (event.target.closest("button")) return;
      widgetPosition = null;
      applyWidgetPosition(root);
      saveSettings().catch((error) => {
        console.error("Meeting Transcriber failed to reset widget position", error);
      });
    });

    window.addEventListener("resize", () => {
      if (!widgetPosition) return;
      applyWidgetPosition(root);
      scheduleSettingsSave();
    });
  }

  function setStatus(status) {
    const root = widget();
    if (!root) return;
    root.dataset.status = paused ? "paused" : status;
    const labels = {
      waiting: "Увімкніть CC",
      connecting: "Підключаю RTC…",
      enabling: "Вмикаю CC…",
      capturing: "Запис captions",
      "capturing-rtc": "Запис RTC captions",
      "rtc-unavailable": "RTC недоступний",
      paused: "Пауза",
    };
    root.querySelector("[data-role=status]").textContent =
      labels[root.dataset.status] || labels.waiting;
  }

  function render() {
    const root = widget();
    if (!root || !state) return;
    if (compactedState !== state || compactedRevision !== state.revision) {
      compactedState = state;
      compactedRevision = state.revision;
      compactedEntries = Model.compactEntries(state.entries);
    }
    const visibleEntries = compactedEntries;
    root.querySelector("[data-role=count]").textContent =
      `${visibleEntries.length} реплік`;
    const activity = root.querySelector("[data-role=activity]");
    activity.textContent = paused
      ? "Запис призупинено"
      : lastCaptionActivityEpoch
        ? `Оновлено ${new Date(lastCaptionActivityEpoch).toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
        })}`
        : "Очікую на текст";
    const preview = root.querySelector("[data-role=preview]");
    preview.replaceChildren();
    visibleEntries.slice(-4).forEach((entry) => {
      const row = document.createElement("div");
      const speaker = document.createElement("strong");
      speaker.textContent = entry.kind === "chat"
        ? `${entry.speaker} (chat)`
        : entry.speaker;
      const text = document.createElement("span");
      text.textContent = entry.text;
      row.append(speaker, text);
      preview.append(row);
    });
    const rtcWarning = root.querySelector("[data-role=rtc-warning]");
    const showRtcWarning = rtcUnavailable && !rtcFallbackActive;
    rtcWarning.hidden = !showRtcWarning;
    const audioButton = root.querySelector('[data-action="audio-fallback"]');
    const audioMessage = root.querySelector("[data-role=audio-message]");
    if (audioFallbackRequestState === "pending") {
      audioButton.disabled = true;
      audioButton.textContent = "Запускаю…";
      audioMessage.textContent = "Надсилаю команду локальному аудіомодулю.";
    } else if (audioFallbackRequestState === "sent") {
      audioButton.disabled = true;
      audioButton.textContent = "Запит надіслано";
      audioMessage.textContent = "Підтвердьте режим запису у системному вікні.";
    } else if (audioFallbackRequestState === "failed") {
      audioButton.disabled = false;
      audioButton.textContent = "Спробувати ще раз";
      audioMessage.textContent =
        "Локальний аудіомодуль недоступний. Скористайтеся звичним хоткеєм.";
    } else {
      audioButton.disabled = false;
      audioButton.textContent = "Запустити аудіозапис";
      audioMessage.textContent =
        "Щоб не втратити зустріч, увімкніть резервний запис звуку.";
    }
    if (!rtcFallbackActive) {
      setStatus(
        rtcUnavailable || !rtcCaptureEnabled
          ? "rtc-unavailable"
          : rtcStatus.channelState === "open" || rtcStatus.decoded > 0
          ? "capturing-rtc"
          : "connecting"
      );
    } else {
      setStatus(findCaptionRegion() ? "capturing" : "waiting");
    }
  }

  function mountWidget() {
    if (widget()) return;
    const root = document.createElement("aside");
    root.id = "meeting-transcriber-widget";
    root.dataset.status = "waiting";
    root.innerHTML = `
      <header>
        <div><i></i><span data-role="status">Увімкніть CC</span></div>
        <button data-action="collapse" title="Згорнути">−</button>
      </header>
      <section>
        <div class="mt-meta"><span data-role="count">0 реплік</span><span data-role="activity">Очікую на текст</span></div>
        <div class="mt-warning" data-role="rtc-warning" hidden>
          <strong>RTC captions не підключилися</strong>
          <span data-role="audio-message">Щоб не втратити зустріч, увімкніть резервний запис звуку.</span>
          <button data-action="audio-fallback">Запустити аудіозапис</button>
        </div>
        <div class="mt-preview" data-role="preview"></div>
        <div class="mt-actions">
          <button data-action="pause">Пауза</button>
          <button data-action="json">Зберегти зараз</button>
        </div>
      </section>`;
    root.addEventListener("click", async (event) => {
      const action = event.target.closest("button")?.dataset.action;
      if (action === "collapse") {
        root.classList.toggle("collapsed");
        if (widgetPosition) applyWidgetPosition(root);
      }
      if (action === "pause") {
        paused = !paused;
        event.target.textContent = paused ? "Продовжити" : "Пауза";
        if (!paused) scan();
        render();
      }
      if (action === "json") exportJson();
      if (action === "audio-fallback") {
        clearTimeout(audioFallbackResetTimer);
        audioFallbackRequestState = "pending";
        render();
        try {
          const response = await globalThis.chrome?.runtime?.sendMessage?.({
            type: "meeting-transcriber:start-backup-audio",
            meetingCode,
          });
          audioFallbackRequestState = response?.ok ? "sent" : "failed";
        } catch {
          audioFallbackRequestState = "failed";
        }
        render();
        if (audioFallbackRequestState === "sent") {
          audioFallbackResetTimer = setTimeout(() => {
            audioFallbackRequestState = "idle";
            render();
          }, 30_000);
        }
      }
    });
    document.body.append(root);
    applyWidgetPosition(root);
    enableWidgetDragging(root);
  }

  async function initialize() {
    meetingCode = currentMeetingCode();
    if (!meetingCode) return;
    await loadState();
    await pruneOldMeetingStates();
    document.addEventListener(RTC_EVENT_NAME, handleRtcEvent);
    rtcCommand("bind");
    notifyBackground("meeting-transcriber:register");
    mountWidget();
    applyNativeCaptionVisibility();
    render();
    connectObserver();
    scanChat();
    scanParticipants();
    if (rtcCaptureEnabled) {
      rtcCommand("start");
      rtcCommand("status");
      scheduleRtcFallback();
    } else {
      const recovery = RtcFallback.recovery(domFallbackEnabled);
      rtcUnavailable = recovery.rtcUnavailable;
      rtcFallbackActive = recovery.domFallbackActive;
      applyNativeCaptionVisibility();
      if (rtcFallbackActive) maybeEnableCaptions();
    }
    setInterval(() => {
      connectObserver();
      maybeEnableCaptions();
      scanChat();
      scanParticipants();
    }, 1500);
  }

  document.addEventListener("click", (event) => {
    if (event.isTrusted && AutoExport.findLeaveControl(event.target)) autoExportJson();
  }, true);
  window.addEventListener("pagehide", autoExportJson);

  initialize().catch((error) => {
    console.error("Meeting Transcriber initialization failed", error);
  });
})();
