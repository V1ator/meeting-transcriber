(function () {
  "use strict";

  if (globalThis.__meetingTranscriberRtcBridge) return;
  const Codec = globalThis.MeetingRtcCaptionCodec;
  const EVENT_NAME = "meeting-transcriber:rtc";
  const COMMAND_NAME = "meeting-transcriber:rtc-command";
  const CHANNEL_LABEL = "captions";
  const MEDIA_SESSION_LABEL = "media-session";
  const OPEN_RETRY_MS = 1_000;
  const ACTIVATION_RETRY_MS = 2_500;
  const MAX_ACTIVATION_ATTEMPTS = 4;
  const peerConnections = new Set();
  const attachedChannels = new WeakSet();
  const observedChannels = new Map();
  const deviceNames = new Map();
  let enabled = false;
  let openedChannel = null;
  let packets = 0;
  let decoded = 0;
  let failures = 0;
  let collectionPackets = 0;
  let collectionResponses = 0;
  let sessionNonce = "";
  let observerOnly = false;
  let openRetryTimer = null;
  let openAttempts = 0;
  let mediaSessionChannel = null;
  let mediaCommandOp = 0;
  let mediaAckSeq = 0;
  let mediaServerCounter = 0;
  let captionLanguage = "uk-UA";
  let languageActivationAttempts = 0;
  let languageActivationConfirmed = false;
  let activationServerTarget = null;
  let activationRetryTimer = null;
  let sendingMediaSessionControl = false;

  function emit(type, detail = {}) {
    if (!sessionNonce) return;
    document.dispatchEvent(new CustomEvent(EVENT_NAME, {
      detail: { type, nonce: sessionNonce, ...detail },
    }));
  }

  function status(extra = {}) {
    emit("status", {
      enabled,
      peerConnections: peerConnections.size,
      channelState: openedChannel?.readyState || "missing",
      packets,
      decoded,
      failures,
      deviceMappings: deviceNames.size,
      collectionPackets,
      collectionResponses,
      observerOnly,
      openAttempts,
      mediaSessionState: mediaSessionChannel?.readyState || "missing",
      captionLanguage,
      languageActivationAttempts,
      languageActivationConfirmed,
      mediaServerCounter,
      languageActivationState: observerOnly
        ? "unchanged"
        : decoded > 0
          ? "streaming"
          : languageActivationConfirmed
            ? "confirmed"
            : languageActivationAttempts > 0
              ? "sent"
              : "waiting-media-session",
      observedChannels: Array.from(observedChannels.values()).slice(0, 50),
      ...extra,
    });
  }

  function recordObservedChannel(channel, origin) {
    const label = String(channel?.label || "").slice(0, 120);
    const key = `${origin}:${label}`;
    const current = observedChannels.get(key) || {
      label,
      origin: String(origin || "").slice(0, 20),
      count: 0,
    };
    current.count += 1;
    observedChannels.set(key, current);
  }

  function clearOpenRetry() {
    clearTimeout(openRetryTimer);
    openRetryTimer = null;
  }

  function clearActivationRetry() {
    clearTimeout(activationRetryTimer);
    activationRetryTimer = null;
  }

  function validLanguage(value) {
    const language = String(value || "");
    return /^[a-z]{2,3}(?:-[A-Z]{2})?$/.test(language)
      ? language
      : "uk-UA";
  }

  function observeMediaSessionSend(data) {
    if (sendingMediaSessionControl) return;
    const bytes = Codec.asBytes(data);
    if (!bytes) return;
    const op = Number(Codec.decodeMediaSessionCommandOp(bytes));
    const seq = Number(Codec.decodeMediaSessionAckSeq(bytes));
    if (Number.isSafeInteger(op) && op >= 0) mediaCommandOp = op;
    if (Number.isSafeInteger(seq) && seq >= 0) mediaAckSeq = seq;
  }

  function scheduleLanguageActivationRetry() {
    clearActivationRetry();
    if (!enabled || observerOnly || decoded > 0 || languageActivationConfirmed
        || languageActivationAttempts >= MAX_ACTIVATION_ATTEMPTS) return;
    activationRetryTimer = setTimeout(() => {
      activationRetryTimer = null;
      activateCaptionLanguage("activation-retry");
    }, ACTIVATION_RETRY_MS);
  }

  function activateCaptionLanguage(reason) {
    const channel = mediaSessionChannel;
    if (!enabled || observerOnly || channel?.readyState !== "open") {
      status({ reason: reason || "media-session-pending" });
      return;
    }
    if (languageActivationAttempts >= MAX_ACTIVATION_ATTEMPTS
        || decoded > 0 || languageActivationConfirmed) {
      clearActivationRetry();
      return;
    }
    const commandOp = mediaCommandOp + 1;
    const firstAck = mediaAckSeq + 1;
    try {
      sendingMediaSessionControl = true;
      channel.send(Codec.encodeMediaSessionCaptionCommand(
        commandOp,
        captionLanguage,
      ));
      channel.send(Codec.encodeMediaSessionAck(firstAck));
      channel.send(Codec.encodeMediaSessionAck(firstAck + 1));
      mediaCommandOp = commandOp;
      mediaAckSeq = firstAck + 1;
      activationServerTarget = mediaServerCounter + 1;
      languageActivationAttempts += 1;
      status({ reason: reason || "caption-language-sent" });
      scheduleLanguageActivationRetry();
    } catch {
      status({ reason: "caption-language-send-failed" });
      scheduleLanguageActivationRetry();
    } finally {
      sendingMediaSessionControl = false;
    }
  }

  async function processMediaSessionMessage(data) {
    try {
      const source = await messageBytes(data);
      const bytes = source && await Codec.inflatePacket(source);
      const counter = Number(
        bytes && Codec.decodeMediaSessionServerCounter(bytes)
      );
      if (!Number.isSafeInteger(counter) || counter < 0) return;
      mediaServerCounter = counter;
      if (activationServerTarget !== null && counter >= activationServerTarget) {
        languageActivationConfirmed = true;
        clearActivationRetry();
        status({ reason: "caption-language-confirmed" });
      }
    } catch {
      // The media-session stream contains unrelated packets as well.
    }
  }

  function scheduleOpenRetry(reason) {
    if (!enabled || observerOnly || openRetryTimer) return;
    openRetryTimer = setTimeout(() => {
      openRetryTimer = null;
      openCaptionChannel();
    }, OPEN_RETRY_MS);
    status({ reason });
  }

  async function messageBytes(data) {
    if (data instanceof Blob) return new Uint8Array(await data.arrayBuffer());
    return Codec.asBytes(data);
  }

  async function processMessage(data) {
    packets += 1;
    try {
      const source = await messageBytes(data);
      const bytes = source && await Codec.inflatePacket(source);
      const message = bytes && Codec.decodeTranscriptPacket(bytes);
      if (!message) {
        failures += 1;
        status({
          reason: "unparsed-packet",
          unparsedPacketSample: bytes ? Codec.describePacket(bytes) : null,
        });
        return;
      }
      decoded += 1;
      clearActivationRetry();
      emit("caption", { message });
      status();
    } catch {
      failures += 1;
      status({ reason: "packet-error" });
    }
  }

  function emitDevices() {
    if (!deviceNames.size) return;
    emit("devices", {
      devices: Array.from(deviceNames, ([deviceId, deviceName]) => ({
        deviceId,
        deviceName,
      })),
    });
  }

  function registerDevices(bytes) {
    const devices = Codec.decodeMeetingCollection(bytes);
    const single = Codec.decodeDevicePacket(bytes);
    if (single) devices.push(single);
    devices.forEach(({ deviceId, deviceName }) => {
      if (deviceId && deviceName) deviceNames.set(deviceId, deviceName);
    });
    emitDevices();
    return devices.length;
  }

  async function processCollectionMessage(data) {
    collectionPackets += 1;
    try {
      const source = await messageBytes(data);
      const bytes = source && await Codec.inflatePacket(source);
      if (!bytes) return;
      registerDevices(bytes);
      status();
    } catch {
      // Device names are optional; captions continue with stable placeholders.
    }
  }

  function bytesFromBase64(value) {
    try {
      const normalized = String(value || "").trim().replace(/^["']|["']$/g, "");
      if (!normalized) return null;
      const binary = globalThis.atob(normalized);
      return Uint8Array.from(binary, (character) => character.charCodeAt(0));
    } catch {
      return null;
    }
  }

  async function inspectCollectionsResponse(response) {
    try {
      const text = await response.clone().text();
      const bytes = bytesFromBase64(text);
      if (!bytes) return;
      collectionResponses += 1;
      registerDevices(bytes);
      status();
    } catch {
      // Never affect Meet when an optional cloned response cannot be decoded.
    }
  }

  const nativeFetch = globalThis.fetch;
  if (typeof nativeFetch === "function") {
    globalThis.fetch = async function (...args) {
      const response = await nativeFetch.apply(this, args);
      let responseUrl = null;
      try {
        responseUrl = new URL(response.url || "", globalThis.location.href);
      } catch {
        responseUrl = null;
      }
      if (enabled
          && responseUrl?.origin === globalThis.location.origin
          && responseUrl.pathname.includes(
            "MeetingSpaceService/SyncMeetingSpaceCollections"
          )) {
        inspectCollectionsResponse(response);
      }
      return response;
    };
  }

  function attachChannel(channel, origin) {
    if (!channel || attachedChannels.has(channel)) {
      return;
    }
    recordObservedChannel(channel, origin);
    if (channel.label === "collections") {
      attachedChannels.add(channel);
      channel.binaryType = "arraybuffer";
      channel.addEventListener("message", (event) => {
        if (enabled) processCollectionMessage(event.data);
      });
      status({ origin, reason: "collections-channel-observed" });
      return;
    }
    if (channel.label === MEDIA_SESSION_LABEL) {
      attachedChannels.add(channel);
      mediaSessionChannel = channel;
      mediaCommandOp = 0;
      mediaAckSeq = 0;
      mediaServerCounter = 0;
      activationServerTarget = null;
      languageActivationConfirmed = false;
      const nativeSend = channel.send;
      if (typeof nativeSend === "function") {
        try {
          channel.send = function (data) {
            observeMediaSessionSend(data);
            return nativeSend.call(this, data);
          };
        } catch {
          // Some browser builds may expose send as read-only. Activation still
          // works with counters starting from the channel's initial sequence.
        }
      }
      channel.addEventListener("open", () => {
        status({ origin, reason: "media-session-open" });
        // Let Meet send its own initial sequence packets first, then continue
        // from the counters observed by the wrapped send method.
        queueMicrotask(() => activateCaptionLanguage("media-session-open"));
      });
      channel.addEventListener("message", (event) => {
        if (enabled) processMediaSessionMessage(event.data);
      });
      channel.addEventListener("close", () => {
        if (mediaSessionChannel === channel) mediaSessionChannel = null;
        clearActivationRetry();
        status({ origin, reason: "media-session-closed" });
      });
      if (channel.readyState === "open") {
        queueMicrotask(() => activateCaptionLanguage("media-session-observed"));
      }
      status({ origin, reason: "media-session-observed" });
      return;
    }
    if (channel.label !== CHANNEL_LABEL) {
      attachedChannels.add(channel);
      status({ origin, reason: "datachannel-observed" });
      return;
    }
    attachedChannels.add(channel);
    openedChannel = channel;
    channel.binaryType = "arraybuffer";
    channel.addEventListener("message", (event) => {
      if (enabled) processMessage(event.data);
    });
    channel.addEventListener("open", () => {
      clearOpenRetry();
      status({ origin });
    });
    channel.addEventListener("close", () => {
      if (openedChannel === channel) openedChannel = null;
      status({ origin, reason: "closed" });
      scheduleOpenRetry("channel-closed-retry");
    });
    channel.addEventListener("error", () => {
      status({ origin, reason: "channel-error" });
      scheduleOpenRetry("channel-error-retry");
    });
    status({ origin });
  }

  function attachPeerConnection(peer) {
    if (!peer || peerConnections.has(peer)) return;
    peerConnections.add(peer);
    peer.addEventListener("datachannel", (event) => {
      attachChannel(event.channel, "remote");
    });
    peer.addEventListener("connectionstatechange", () => {
      status({ connectionState: peer.connectionState });
      if (!["closed", "failed"].includes(peer.connectionState)) {
        scheduleOpenRetry("peer-state-retry");
      }
    });
    status({ connectionState: peer.connectionState });
    scheduleOpenRetry("peer-attached-retry");
  }

  function openCaptionChannel() {
    if (!enabled || observerOnly) return;
    clearOpenRetry();
    const peer = Array.from(peerConnections).find(
      (candidate) => !["closed", "failed"].includes(candidate.connectionState)
    );
    if (!peer) {
      status({ reason: "peer-missing" });
      scheduleOpenRetry("peer-missing-retry");
      return;
    }
    if (openedChannel && ["connecting", "open"].includes(openedChannel.readyState)) {
      status();
      if (openedChannel.readyState === "connecting") {
        scheduleOpenRetry("channel-connecting-retry");
      }
      return;
    }
    try {
      openAttempts += 1;
      const channel = peer.createDataChannel(CHANNEL_LABEL, {
        ordered: true,
        maxRetransmits: 10,
      });
      attachChannel(channel, "local");
      if (channel.readyState !== "open") {
        scheduleOpenRetry("channel-opening-retry");
      }
    } catch {
      status({ reason: "open-failed" });
      scheduleOpenRetry("open-failed-retry");
    }
  }

  const NativePeerConnection = globalThis.RTCPeerConnection;
  if (!NativePeerConnection || !Codec) {
    emit("status", { enabled: false, reason: "unsupported" });
    return;
  }

  function WrappedPeerConnection(configuration, constraints) {
    const peer = new NativePeerConnection(configuration, constraints);
    attachPeerConnection(peer);
    if (enabled && !observerOnly) queueMicrotask(openCaptionChannel);
    return peer;
  }
  WrappedPeerConnection.prototype = NativePeerConnection.prototype;
  Object.setPrototypeOf(WrappedPeerConnection, NativePeerConnection);
  globalThis.RTCPeerConnection = WrappedPeerConnection;

  const nativeCreateDataChannel = NativePeerConnection.prototype.createDataChannel;
  NativePeerConnection.prototype.createDataChannel = function (...args) {
    attachPeerConnection(this);
    const channel = nativeCreateDataChannel.apply(this, args);
    attachChannel(channel, "local");
    return channel;
  };

  document.addEventListener(COMMAND_NAME, (event) => {
    const detail = event.detail || {};
    const command = detail.type;
    const nonce = String(detail.nonce || "");
    if (command === "bind") {
      if (nonce.length < 16 || nonce.length > 128) return;
      sessionNonce = nonce;
      emit("ready");
      status();
      return;
    }
    if (!sessionNonce || nonce !== sessionNonce) return;
    if (command === "start") {
      enabled = true;
      observerOnly = false;
      captionLanguage = validLanguage(detail.languageCode);
      languageActivationAttempts = 0;
      languageActivationConfirmed = false;
      activationServerTarget = null;
      openCaptionChannel();
      activateCaptionLanguage("capture-started");
      emitDevices();
      status();
    } else if (command === "observe") {
      enabled = true;
      observerOnly = true;
      clearOpenRetry();
      clearActivationRetry();
      emitDevices();
      status({ reason: "observer-only" });
    } else if (command === "stop") {
      enabled = false;
      clearOpenRetry();
      clearActivationRetry();
      status();
    } else if (command === "retry") {
      openedChannel = null;
      if (!observerOnly) {
        languageActivationAttempts = 0;
        languageActivationConfirmed = false;
        activationServerTarget = null;
        openCaptionChannel();
        activateCaptionLanguage("manual-retry");
      }
    } else if (command === "status") {
      emitDevices();
      status();
    }
  });

  globalThis.__meetingTranscriberRtcBridge = true;
})();
