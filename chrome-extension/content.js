(function () {
  "use strict";

  const Model = globalThis.MeetingCaptionModel;
  const AutoExport = globalThis.MeetingAutoExport;
  const RtcFallback = globalThis.MeetingRtcFallback;
  const STORAGE_PREFIX = "meeting-transcriber:";
  const SETTINGS_KEY = "meeting-transcriber:settings";
  const FINALIZE_DELAY_MS = 1800;
  const RTC_FALLBACK_DELAY_MS = 20_000;
  const RTC_EVENT_NAME = "meeting-transcriber:rtc";
  const RTC_COMMAND_NAME = "meeting-transcriber:rtc-command";
  const RTC_SESSION_NONCE = globalThis.crypto.randomUUID();
  const WIDGET_EDGE_MARGIN = 8;
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
  let saveTimer = null;
  let settingsSaveTimer = null;
  let startedEpoch = Date.now();
  let autoExportEnabled = true;
  let captureChat = true;
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
    autoExportEnabled = stored[SETTINGS_KEY]?.autoExportEnabled !== false;
    captureChat = stored[SETTINGS_KEY]?.captureChat !== false;
    const savedPosition = stored[SETTINGS_KEY]?.widgetPosition;
    if (Number.isFinite(savedPosition?.x) && Number.isFinite(savedPosition?.y)) {
      widgetPosition = { x: savedPosition.x, y: savedPosition.y };
    }
  }

  async function saveSettings() {
    await storageSet({
      [SETTINGS_KEY]: {
        autoExportEnabled,
        captureChat,
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
      rtcUnavailable = true;
      render();
    }, Math.max(0, rtcFallbackDeadline - now));
  }

  function handleRtcEvent(event) {
    const detail = event.detail || {};
    if (detail.nonce !== RTC_SESSION_NONCE) return;
    if (detail.type === "ready") {
      rtcStatus.ready = true;
      rtcCommand("start");
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
      rtcUnavailable = true;
      rtcFallbackDeadline = 0;
      clearTimeout(rtcFallbackTimer);
    } else if (["closed", "channel-error"].includes(detail.reason)) {
      rtcFallbackRequiresReconnect = true;
      rtcCommand("retry");
      scheduleRtcFallback();
    } else if (detail.channelState === "open") {
      rtcUnavailable = false;
      rtcFallbackDeadline = 0;
      rtcFallbackRequiresReconnect = false;
      clearTimeout(rtcFallbackTimer);
    }
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
    scanChat();
    scanParticipants();
    downloadJsonExport(Model.exportState(state));
  }

  function autoExportJson() {
    if (!autoExportEnabled || !state) return false;
    scanChat();
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
    rtcWarning.hidden = !rtcUnavailable;
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
    setStatus(
      rtcUnavailable
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
    render();
    scanChat();
    scanParticipants();
    rtcCommand("start");
    rtcCommand("status");
    scheduleRtcFallback();
    setInterval(() => {
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
