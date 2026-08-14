(function () {
  "use strict";

  const Model = globalThis.MeetingCaptionModel;
  const CaptionControl = globalThis.MeetingCaptionControl;
  const AutoExport = globalThis.MeetingAutoExport;
  const RtcFallback = globalThis.MeetingRtcFallback;
  const STORAGE_PREFIX = "meeting-transcriber:";
  const DIAGNOSTIC_STORAGE_PREFIX = "meeting-transcriber:diagnostic:";
  const SETTINGS_KEY = "meeting-transcriber:settings";
  const FINALIZE_DELAY_MS = 1800;
  const RTC_FALLBACK_DELAY_MS = 20_000;
  const RTC_EVENT_NAME = "meeting-transcriber:rtc";
  const RTC_COMMAND_NAME = "meeting-transcriber:rtc-command";
  const RTC_SESSION_NONCE = globalThis.crypto.randomUUID();
  const WIDGET_EDGE_MARGIN = 8;
  const MAX_SPEAKER_ALIASES = 500;
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
  let sessionStorageKey = "";
  let state = null;
  let paused = false;
  let saveTimer = null;
  let settingsSaveTimer = null;
  let startedEpoch = Date.now();
  let autoExportEnabled = true;
  let autoAudioFallbackEnabled = true;
  let passiveDiagnosticMode = false;
  let passiveSession = false;
  let captureChat = true;
  let speakerAliases = {};
  let speakerNameAliases = {};
  let rtcUnavailable = false;
  let rtcFallbackTimer = null;
  let rtcFallbackDeadline = 0;
  let rtcFallbackRequiresReconnect = false;
  let audioFallbackRequestState = "idle";
  let audioFallbackResetTimer = null;
  let audioFallbackAttempted = false;
  let audioFallbackRequestedAutomatically = false;
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
  let lastCaptionActivityEpoch = 0;
  let compactedState = null;
  let compactedRevision = -1;
  let compactedEntries = [];
  const finalizeTimers = new Map();
  const chatSeenKeys = new Set();
  const memoryStorage = {};
  const rtcSpeakerNames = new Map();

  function currentMeetingCode() {
    const match = location.pathname.match(/^\/([a-z]{3}-[a-z]{4}-[a-z]{3})(?:\/|$)/i);
    if (match) return match[1].toLowerCase();
    if (["localhost", "127.0.0.1"].includes(location.hostname)) {
      return new URLSearchParams(location.search).get("meetingCode") || "";
    }
    return "";
  }

  function storageKey() {
    return sessionStorageKey || `${STORAGE_PREFIX}${meetingCode}`;
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

  function sanitizedAliasMap(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value)
      .slice(-MAX_SPEAKER_ALIASES)
      .map(([key, name]) => [String(key).slice(0, 500), Model.cleanSpeaker(name)])
      .filter(([key, name]) => (
        safeAliasKey(key) && name !== "Невідомий"
      )));
  }

  function safeAliasKey(value) {
    const key = String(value || "");
    return Boolean(key) && !["__proto__", "prototype", "constructor"].includes(key);
  }

  function speakerNameKey(value) {
    return Model.cleanSpeaker(value).toLocaleLowerCase();
  }

  function canonicalSpeakerName(value) {
    const cleaned = Model.cleanSpeaker(value);
    return speakerNameAliases[speakerNameKey(cleaned)] || cleaned;
  }

  function captureHealth() {
    if (!state.captureHealth || typeof state.captureHealth !== "object") {
      state.captureHealth = {};
    }
    return state.captureHealth;
  }

  function refreshCaptureHealth() {
    const health = captureHealth();
    health.channelState = rtcStatus.channelState;
    health.decodedCaptions = Math.max(
      Number(health.decodedCaptions) || 0,
      Number(rtcStatus.decoded) || 0
    );
    health.decodeFailures = Math.max(
      Number(health.decodeFailures) || 0,
      Number(rtcStatus.failures) || 0
    );
    health.rtcPackets = Math.max(
      Number(health.rtcPackets) || 0,
      Number(rtcStatus.packets) || 0
    );
    health.finalizedAtMs = elapsedMs();
    health.captureMode = passiveDiagnosticMode ? "rtc-observer" : "rtc-direct";
    health.captionActivationState = passiveDiagnosticMode
      ? "unchanged"
      : rtcStatus.languageActivationState || "pending";
    health.captionActivationReason = passiveDiagnosticMode
      ? "passive-diagnostic-mode"
      : "direct-rtc-media-session";
    health.captionEnableAttempts = passiveDiagnosticMode
      ? 0
      : Math.max(0, Number(rtcStatus.languageActivationAttempts) || 0);
    health.mediaSessionState = String(
      rtcStatus.mediaSessionState || "missing"
    ).slice(0, 40);
    health.captionLanguage = String(rtcStatus.captionLanguage || "").slice(0, 20);
    health.captionLanguageConfirmed = Boolean(
      rtcStatus.languageActivationConfirmed
    );
    health.captionsActivatedByExtension = false;
    health.nativeCaptionDisplayState = "unchanged";
    health.nativeCaptionHideReason = passiveDiagnosticMode
      ? "passive-diagnostic-mode"
      : "direct-rtc-no-dom-changes";
    health.nativeCaptionsHiddenByExtension = false;
    health.passiveDiagnosticMode = passiveDiagnosticMode;
    health.rtcObserverOnly = Boolean(rtcStatus.observerOnly);
    health.rtcChannelOpenAttempts = Math.max(
      Number(health.rtcChannelOpenAttempts) || 0,
      Number(rtcStatus.openAttempts) || 0
    );
    health.observedDataChannels = Array.isArray(rtcStatus.observedChannels)
      ? rtcStatus.observedChannels.slice(0, 50).map((channel) => ({
        label: String(channel?.label || "").slice(0, 120),
        origin: String(channel?.origin || "").slice(0, 20),
        count: Math.max(0, Math.min(Number(channel?.count) || 0, 10_000)),
      }))
      : [];
    if (CaptionControl?.diagnose) {
      health.captionControlDiagnostic = CaptionControl.diagnose(document);
      health.captionControlSeen =
        health.captionControlDiagnostic.possibleControls > 0;
    }
    return health;
  }

  function sanitizedPacketDiagnostic(value) {
    const allowedKinds = new Set([
      "bytes", "device-id", "fixed32", "fixed64", "language-code",
      "message", "utf8", "varint",
    ]);
    let remaining = 48;
    function fields(items, depth = 0) {
      if (!Array.isArray(items) || depth > 5) return [];
      const result = [];
      for (const item of items) {
        if (remaining <= 0 || !item || typeof item !== "object") break;
        const field = Number(item.field);
        const wire = Number(item.wire);
        const kind = String(item.kind || "");
        if (!Number.isInteger(field) || field <= 0
            || ![0, 1, 2, 5].includes(wire) || !allowedKinds.has(kind)) continue;
        remaining -= 1;
        const clean = { field, wire, kind };
        if (Number.isInteger(item.length) && item.length >= 0) {
          clean.length = Math.min(item.length, 1_000_000);
        }
        if (Number.isInteger(item.characters) && item.characters >= 0) {
          clean.characters = Math.min(item.characters, 20_000);
        }
        const nested = fields(item.fields, depth + 1);
        if (nested.length) clean.fields = nested;
        result.push(clean);
      }
      return result;
    }
    if (!value || typeof value !== "object" || value.redacted !== true) return null;
    const byteLength = Number(value.byteLength);
    if (!Number.isInteger(byteLength) || byteLength <= 0) return null;
    return {
      schemaVersion: 1,
      byteLength: Math.min(byteLength, 1_000_000),
      parsedAsProtobuf: Boolean(value.parsedAsProtobuf),
      fields: fields(value.fields),
      redacted: true,
    };
  }

  async function loadState() {
    const settingsStored = await storageGet([SETTINGS_KEY]);
    const settings = settingsStored[SETTINGS_KEY] || {};
    passiveDiagnosticMode = settings.passiveDiagnosticMode === true;
    passiveSession = passiveDiagnosticMode;
    sessionStorageKey = passiveDiagnosticMode
      ? `${DIAGNOSTIC_STORAGE_PREFIX}${meetingCode}`
      : `${STORAGE_PREFIX}${meetingCode}`;
    const sessionStored = await storageGet([storageKey()]);
    let seed = sessionStored[storageKey()] || {};
    const seedStartedAt = Date.parse(seed.startedAt || "");
    if (Number.isFinite(seedStartedAt)
        && Date.now() - seedStartedAt > 12 * 60 * 60 * 1000) {
      seed = {};
      await storageRemove(storageKey());
    }
    speakerAliases = sanitizedAliasMap(settings.speakerAliases);
    speakerNameAliases = sanitizedAliasMap(
      settings.speakerNameAliases
    );
    rtcSpeakerNames.clear();
    Object.entries(speakerAliases).forEach(([deviceId, name]) => {
      rtcSpeakerNames.set(deviceId, canonicalSpeakerName(name));
    });
    state = Model.createState({
      ...seed,
      meetingCode,
      meetingTitle: pageMeetingTitle(),
      language: seed.language || "uk",
    });
    if (!state.startedAt) state.startedAt = new Date(startedEpoch).toISOString();
    if (state.startedAt) {
      const parsed = Date.parse(state.startedAt);
      if (Number.isFinite(parsed)) startedEpoch = parsed;
    }
    autoExportEnabled = settings.autoExportEnabled !== false;
    autoAudioFallbackEnabled =
      settings.autoAudioFallbackEnabled !== false;
    captureChat = settings.captureChat !== false;
    const knownParticipants = (state.participants || []).map(canonicalSpeakerName);
    state.entries.forEach((entry) => {
      const alias = entry.speakerId
        ? rtcSpeakerNames.get(normalizeRtcDeviceId(entry.speakerId))
        : null;
      entry.speaker = alias || canonicalSpeakerName(entry.speaker);
    });
    state.participants = [];
    knownParticipants.forEach((name) => Model.addParticipant(state, name));
    state.entries.forEach((entry) => Model.addParticipant(state, entry.speaker));
    const savedPosition = settings.widgetPosition;
    if (Number.isFinite(savedPosition?.x) && Number.isFinite(savedPosition?.y)) {
      widgetPosition = { x: savedPosition.x, y: savedPosition.y };
    }
  }

  async function saveSettings() {
    await storageSet({
      [SETTINGS_KEY]: {
        autoExportEnabled,
        autoAudioFallbackEnabled,
        passiveDiagnosticMode,
        captureChat,
        speakerAliases,
        speakerNameAliases,
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

  function scheduleFinalize(key) {
    clearTimeout(finalizeTimers.get(key));
    finalizeTimers.set(key, setTimeout(() => {
      Model.finalize(state, key, elapsedMs());
      finalizeTimers.delete(key);
      scheduleSave();
      render();
    }, FINALIZE_DELAY_MS));
  }

  function rtcCommand(type) {
    const configuredLanguage = String(state?.language || "uk");
    const languageCode = configuredLanguage.includes("-")
      ? configuredLanguage
      : configuredLanguage.toLowerCase() === "uk"
        ? "uk-UA"
        : configuredLanguage;
    document.dispatchEvent(new CustomEvent(RTC_COMMAND_NAME, {
      detail: { type, nonce: RTC_SESSION_NONCE, languageCode },
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

  function rememberDeviceAlias(deviceId, name) {
    const normalized = normalizeRtcDeviceId(deviceId);
    const tail = normalized.split("/").pop();
    let changed = false;
    [normalized, tail].filter(safeAliasKey).forEach((key) => {
      if (speakerAliases[key] === name) return;
      speakerAliases[key] = name;
      changed = true;
    });
    if (Object.keys(speakerAliases).length > MAX_SPEAKER_ALIASES) {
      speakerAliases = Object.fromEntries(
        Object.entries(speakerAliases).slice(-MAX_SPEAKER_ALIASES)
      );
    }
    if (changed) scheduleSettingsSave();
  }

  function registerRtcSpeaker(deviceId, name) {
    const normalized = normalizeRtcDeviceId(deviceId);
    const cleaned = canonicalSpeakerName(name);
    if (!normalized || cleaned === "Невідомий") return false;
    const fallbackName = fallbackNameForDevice(normalized);
    const tail = normalized.split("/").pop();
    rtcSpeakerNames.set(normalized, cleaned);
    rtcSpeakerNames.set(tail, cleaned);
    rememberDeviceAlias(normalized, cleaned);
    if (state) {
      let changed = false;
      state.entries.forEach((entry) => {
        const entryDevice = normalizeRtcDeviceId(entry.speakerId);
        if (
          entry.speaker !== cleaned
          && (
            entry.speaker === fallbackName
            || entryDevice === normalized
            || (tail && entryDevice.split("/").pop() === tail)
          )
        ) {
          entry.speaker = cleaned;
          changed = true;
        }
      });
      const participantCount = (state.participants || []).length;
      state.participants = (state.participants || []).filter(
        (participant) => speakerNameKey(participant) !== speakerNameKey(fallbackName)
      );
      changed = state.participants.length !== participantCount
        || Model.addParticipant(state, cleaned)
        || changed;
      if (changed) {
        state.revision += 1;
        scheduleSave();
      }
    }
    return true;
  }

  function renameSpeaker(speakerId, currentName, requestedName) {
    const cleaned = Model.cleanSpeaker(requestedName);
    const previous = Model.cleanSpeaker(currentName);
    if (
      cleaned === "Невідомий"
      || speakerNameKey(cleaned) === speakerNameKey(previous)
    ) return false;
    const previousKey = speakerNameKey(previous);
    if (!/^Учасник\s+\S+/iu.test(previous) && safeAliasKey(previousKey)) {
      speakerNameAliases[previousKey] = cleaned;
    }
    if (Object.keys(speakerNameAliases).length > MAX_SPEAKER_ALIASES) {
      speakerNameAliases = Object.fromEntries(
        Object.entries(speakerNameAliases).slice(-MAX_SPEAKER_ALIASES)
      );
    }
    if (speakerId) registerRtcSpeaker(speakerId, cleaned);
    state.entries.forEach((entry) => {
      if (
        entry.speaker === previous
        || (speakerId && normalizeRtcDeviceId(entry.speakerId)
          === normalizeRtcDeviceId(speakerId))
      ) entry.speaker = cleaned;
    });
    state.participants = (state.participants || []).filter(
      (participant) => speakerNameKey(participant) !== speakerNameKey(previous)
    );
    Model.addParticipant(state, cleaned);
    state.revision += 1;
    scheduleSettingsSave();
    scheduleSave();
    render();
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
    return canonicalSpeakerName(cleaned);
  }

  function processRtcCaption(message) {
    if (paused || !state) return;
    const text = Model.normalizeText(message?.text);
    const deviceId = normalizeRtcDeviceId(message?.deviceId);
    const messageId = String(message?.messageId || "");
    const messageVersion = Number(message?.messageVersion);
    if (!text || !deviceId || !messageId
        || text.length > 20_000 || deviceId.length > 500 || messageId.length > 500
        || !Number.isSafeInteger(messageVersion) || messageVersion < 0) return;
    if (!state.startedAt) startedEpoch = Date.now();
    rtcUnavailable = false;
    rtcFallbackDeadline = 0;
    rtcFallbackRequiresReconnect = false;
    clearTimeout(rtcFallbackTimer);
    lastCaptionActivityEpoch = Date.now();
    const atMs = elapsedMs();
    const health = captureHealth();
    if (!Number.isFinite(health.firstCaptionMs)) health.firstCaptionMs = atMs;
    health.lastCaptionMs = atMs;
    health.decodedCaptions = Math.max(
      Number(health.decodedCaptions) || 0,
      Number(rtcStatus.decoded) || 0,
    );
    if (health.hadRtcUnavailable) health.recovered = true;
    const key = `rtc-${messageId}`;
    const item = Model.observeVersioned(state, {
      key,
      version: messageVersion,
      speaker: participantNameForDevice(deviceId),
      speakerId: deviceId,
      text,
      atMs,
      observedAt: new Date().toISOString(),
    });
    if (!item) return;
    scheduleFinalize(key);
    scheduleSave();
    render();
  }

  async function requestAudioFallback({ automatic = false } = {}) {
    if (passiveDiagnosticMode) return;
    if (audioFallbackRequestState === "pending"
        || audioFallbackRequestState === "sent") return;
    if (automatic && audioFallbackAttempted) return;
    audioFallbackAttempted = true;
    audioFallbackRequestedAutomatically = automatic;
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

  function activateRtcFallback(reason) {
    if (passiveDiagnosticMode) {
      const health = refreshCaptureHealth();
      health.lastFailureReason = reason || "rtc-unavailable";
      scheduleSave();
      render();
      return;
    }
    rtcUnavailable = true;
    const health = refreshCaptureHealth();
    health.hadRtcUnavailable = true;
    health.lastFailureReason = reason || "rtc-unavailable";
    scheduleSave();
    render();
    if (autoAudioFallbackEnabled) {
      requestAudioFallback({ automatic: true }).catch(() => {});
    }
  }

  function scheduleRtcFallback() {
    clearTimeout(rtcFallbackTimer);
    if (passiveDiagnosticMode) {
      rtcFallbackDeadline = 0;
      return;
    }
    const now = Date.now();
    const effectiveDecoded = rtcFallbackRequiresReconnect
      ? 0
      : rtcStatus.decoded;
    rtcFallbackDeadline = RtcFallback.arm(
      rtcFallbackDeadline,
      now,
      RTC_FALLBACK_DELAY_MS,
      effectiveDecoded,
      rtcStatus.channelState,
      rtcStatus.packets,
      rtcStatus.failures
    );
    if (!rtcFallbackDeadline) return;
    rtcFallbackTimer = setTimeout(() => {
      if (!RtcFallback.isDue(
        rtcFallbackDeadline,
        Date.now(),
        rtcFallbackRequiresReconnect ? 0 : rtcStatus.decoded,
        rtcStatus.channelState,
        rtcStatus.packets,
        rtcStatus.failures
      )) {
        scheduleRtcFallback();
        return;
      }
      const decodeStalled = rtcStatus.decoded === 0
        && rtcStatus.packets > 0 && rtcStatus.failures > 0;
      activateRtcFallback(decodeStalled ? "decode-stall" : "startup-timeout");
    }, Math.max(0, rtcFallbackDeadline - now));
  }

  function handleRtcEvent(event) {
    const detail = event.detail || {};
    if (detail.nonce !== RTC_SESSION_NONCE) return;
    if (detail.type === "ready") {
      rtcStatus.ready = true;
      rtcCommand(passiveDiagnosticMode ? "observe" : "start");
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
    const previousChannelState = rtcStatus.channelState;
    rtcStatus = {
      ...rtcStatus,
      ...detail,
      ready: rtcStatus.ready,
      reason: detail.reason || "",
    };
    const health = captureHealth();
    health.channelState = rtcStatus.channelState;
    health.decodeFailures = Math.max(
      Number(health.decodeFailures) || 0,
      Number(detail.failures) || 0,
    );
    health.rtcPackets = Math.max(
      Number(health.rtcPackets) || 0,
      Number(detail.packets) || 0,
    );
    const packetDiagnostic = sanitizedPacketDiagnostic(
      detail.unparsedPacketSample
    );
    if (packetDiagnostic && !health.unparsedPacketSample) {
      health.unparsedPacketSample = packetDiagnostic;
    }
    const decodeStalled = Number(detail.decoded) === 0
      && Number(detail.packets) > 0 && Number(detail.failures) > 0;
    if (
      detail.reason === "unsupported"
      || (detail.failures >= 3 && detail.decoded === 0)
    ) {
      rtcFallbackDeadline = 0;
      clearTimeout(rtcFallbackTimer);
      activateRtcFallback(detail.reason || "decode-failures");
    } else if (decodeStalled) {
      health.lastFailureReason = detail.reason || "decode-stall";
      scheduleRtcFallback();
    } else if (["closed", "channel-error"].includes(detail.reason)) {
      if (previousChannelState === "open") {
        health.disconnectCount = (Number(health.disconnectCount) || 0) + 1;
      }
      health.lastFailureReason = detail.reason;
      rtcFallbackRequiresReconnect = true;
      if (!passiveDiagnosticMode) {
        rtcCommand("retry");
        scheduleRtcFallback();
      }
    } else if (detail.channelState === "open") {
      if (!Number.isFinite(health.rtcOpenedAtMs)) {
        health.rtcOpenedAtMs = elapsedMs();
      }
      if (health.hadRtcUnavailable) health.recovered = true;
      rtcUnavailable = false;
      rtcFallbackDeadline = 0;
      rtcFallbackRequiresReconnect = false;
      clearTimeout(rtcFallbackTimer);
    }
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
      const speaker = canonicalSpeakerName(
        speakerNode?.getAttribute("data-sender-name")
        || speakerNode?.textContent
      );
      const text = textNode.textContent;
      if (speaker === "Невідомий" || !Model.normalizeText(text)) return;
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
      if (Model.addParticipant(state, canonicalSpeakerName(name))) changed = true;
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
      if (Model.addParticipant(state, canonicalSpeakerName(name))) changed = true;
    });
    if (changed) scheduleSave();
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

  function diagnosticTimestamp() {
    return new Date().toISOString().replace(/[:.]/g, "-");
  }

  function exportDiagnostic() {
    scanChat();
    scanParticipants();
    const health = refreshCaptureHealth();
    const exported = Model.exportState(state);
    const diagnostic = {
      ...exported,
      diagnosticSnapshot: {
        generatedAt: new Date().toISOString(),
        extensionVersion: globalThis.chrome?.runtime?.getManifest?.().version || "",
        browserLanguage: String(globalThis.navigator?.language || ""),
        documentLanguage: String(document.documentElement?.lang || ""),
        viewport: {
          width: Math.max(0, Math.round(window.innerWidth || 0)),
          height: Math.max(0, Math.round(window.innerHeight || 0)),
        },
        captionControl: health.captionControlDiagnostic || null,
        passiveDiagnosticMode,
        rtcObserverOnly: Boolean(rtcStatus.observerOnly),
        observedDataChannels: health.observedDataChannels,
      },
    };
    download(
      `meeting-transcriber-diagnostic-${meetingCode}-${diagnosticTimestamp()}.json`,
      `${JSON.stringify(diagnostic, null, 2)}\n`,
      "application/json"
    );
  }

  function autoExportJson() {
    if (passiveSession || passiveDiagnosticMode
        || !autoExportEnabled || !state) return false;
    scanChat();
    scanParticipants();
    refreshCaptureHealth();
    const exported = Model.exportState(state);
    if (!AutoExport.shouldExport(exported)) return false;
    const signature = AutoExport.signature(exported);
    if (signature === lastExportSignature) return false;
    // Flush the latest in-memory captions immediately so the background
    // fallback does not read a state delayed by the normal 250 ms debounce.
    storageSet({ [storageKey()]: state }).catch(() => {});
    downloadJsonExport(exported);
    return true;
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
      waiting: "Підключаю RTC…",
      connecting: "Підключаю RTC…",
      capturing: "Запис captions",
      "capturing-rtc": "Запис RTC captions",
      "rtc-unavailable": "RTC недоступний",
      passive: "Пасивна діагностика",
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
    activity.textContent = passiveDiagnosticMode
      ? "Лише спостереження"
      : paused
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
      const speaker = document.createElement("button");
      speaker.type = "button";
      speaker.className = "mt-speaker";
      speaker.dataset.action = "rename-speaker";
      speaker.dataset.speaker = entry.speaker;
      speaker.dataset.speakerId = entry.speakerId || "";
      speaker.title = "Натисніть, щоб виправити ім’я";
      speaker.textContent = entry.kind === "chat"
        ? `${entry.speaker} (chat)`
        : entry.speaker;
      const text = document.createElement("span");
      text.textContent = entry.text;
      row.append(speaker, text);
      preview.append(row);
    });
    const rtcWarning = root.querySelector("[data-role=rtc-warning]");
    rtcWarning.hidden = passiveDiagnosticMode || !rtcUnavailable;
    root.querySelector('[data-action="passive-mode"]').checked =
      passiveDiagnosticMode;
    root.querySelector("[data-role=mode-reload-notice]").hidden =
      passiveSession === passiveDiagnosticMode;
    root.querySelector("[data-role=passive-notice]").hidden =
      !passiveDiagnosticMode;
    root.querySelector("[data-role=warning-title]").textContent =
      "RTC captions не підключилися";
    const audioButton = root.querySelector('[data-action="audio-fallback"]');
    const audioMessage = root.querySelector("[data-role=audio-message]");
    const autoAudioToggle = root.querySelector(
      '[data-action="auto-audio-fallback"]'
    );
    autoAudioToggle.checked = autoAudioFallbackEnabled;
    if (audioFallbackRequestState === "pending") {
      audioButton.disabled = true;
      audioButton.textContent = "Запускаю…";
      audioMessage.textContent = audioFallbackRequestedAutomatically
        ? "RTC не працює — автоматично запускаю резервний аудіозапис."
        : "Надсилаю команду локальному аудіомодулю.";
    } else if (audioFallbackRequestState === "sent") {
      audioButton.disabled = true;
      audioButton.textContent = "Запит надіслано";
      audioMessage.textContent = audioFallbackRequestedAutomatically
        ? "Резервний аудіозапис запущено автоматично. Підтвердьте режим у системному вікні."
        : "Підтвердьте режим запису у системному вікні.";
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
    setStatus(
      passiveDiagnosticMode
        ? "passive"
        : rtcUnavailable
        ? "rtc-unavailable"
        : rtcStatus.channelState === "open" || rtcStatus.decoded > 0
        ? "capturing-rtc"
        : "connecting"
    );
  }

  function mountWidget() {
    if (widget()) return;
    const root = document.createElement("aside");
    root.id = "meeting-transcriber-widget";
    root.dataset.status = "waiting";
    root.innerHTML = `
      <header>
        <div><i></i><span data-role="status">Підключаю RTC…</span></div>
        <button data-action="collapse" title="Згорнути">−</button>
      </header>
      <section>
        <div class="mt-meta"><span data-role="count">0 реплік</span><span data-role="activity">Очікую на текст</span></div>
        <label class="mt-mode-toggle">
          <input type="checkbox" data-action="passive-mode">
          <span>Пасивна діагностика</span>
        </label>
        <div class="mt-mode-reload" data-role="mode-reload-notice" hidden>
          Режим змінено — перезавантажте вкладку Meet.
        </div>
        <div class="mt-passive" data-role="passive-notice" hidden>
          Tactiq керує Meet. Розширення лише спостерігає; автоматичний export та аудіорезерв вимкнені.
        </div>
        <div class="mt-warning" data-role="rtc-warning" hidden>
          <strong data-role="warning-title">RTC captions не підключилися</strong>
          <span data-role="audio-message">Щоб не втратити зустріч, увімкніть резервний запис звуку.</span>
          <button data-action="audio-fallback">Запустити аудіозапис</button>
          <label class="mt-auto-audio">
            <input type="checkbox" data-action="auto-audio-fallback" checked>
            <span>Автоматично запускати аудіо при збої RTC</span>
          </label>
        </div>
        <div class="mt-preview" data-role="preview"></div>
        <div class="mt-actions">
          <button data-action="pause">Пауза</button>
          <button data-action="diagnostic">Діагностика</button>
        </div>
      </section>`;
    root.addEventListener("click", async (event) => {
      const action = event.target.closest("button")?.dataset.action;
      if (action === "rename-speaker") {
        const button = event.target.closest("button");
        const requested = globalThis.prompt?.(
          "Ім’я учасника",
          button.dataset.speaker || ""
        );
        if (requested) {
          renameSpeaker(
            button.dataset.speakerId || "",
            button.dataset.speaker || "",
            requested
          );
        }
        return;
      }
      if (action === "collapse") {
        root.classList.toggle("collapsed");
        if (widgetPosition) applyWidgetPosition(root);
      }
      if (action === "pause") {
        paused = !paused;
        event.target.textContent = paused ? "Продовжити" : "Пауза";
        render();
      }
      if (action === "diagnostic") exportDiagnostic();
      if (action === "audio-fallback") {
        await requestAudioFallback();
      }
    });
    root.addEventListener("change", async (event) => {
      const action = event.target?.dataset.action;
      if (action === "passive-mode") {
        event.target.disabled = true;
        const stored = await storageGet([SETTINGS_KEY]);
        await storageSet({
          [SETTINGS_KEY]: {
            ...(stored[SETTINGS_KEY] || {}),
            passiveDiagnosticMode: Boolean(event.target.checked),
          },
        });
        event.target.disabled = false;
        return;
      }
      if (action === "auto-audio-fallback") {
        autoAudioFallbackEnabled = Boolean(event.target.checked);
        scheduleSettingsSave();
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
    notifyBackground("meeting-transcriber:register", {
      passiveDiagnosticMode: passiveSession,
    });
    mountWidget();
    render();
    scanChat();
    scanParticipants();
    rtcCommand(passiveDiagnosticMode ? "observe" : "start");
    rtcCommand("status");
    if (!passiveDiagnosticMode) scheduleRtcFallback();
    setInterval(() => {
      scanChat();
      scanParticipants();
    }, 1500);
  }

  document.addEventListener("click", (event) => {
    if (event.isTrusted && AutoExport.findLeaveControl(event.target)) autoExportJson();
  }, true);
  window.addEventListener("pagehide", autoExportJson);

  globalThis.chrome?.storage?.onChanged?.addListener((changes, areaName) => {
    if (areaName !== "local" || !changes[SETTINGS_KEY]) return;
    const enabled = changes[SETTINGS_KEY].newValue?.passiveDiagnosticMode === true;
    if (enabled === passiveDiagnosticMode) return;
    passiveDiagnosticMode = enabled;
    clearTimeout(rtcFallbackTimer);
    rtcFallbackDeadline = 0;
    rtcUnavailable = false;
    rtcCommand(enabled ? "observe" : "start");
    if (!enabled) {
      scheduleRtcFallback();
    }
    render();
  });

  initialize().catch((error) => {
    console.error("Meeting Transcriber initialization failed", error);
  });
})();
