(function () {
  "use strict";

  if (globalThis.__meetingTranscriberRtcBridge) return;
  const Codec = globalThis.MeetingRtcCaptionCodec;
  const EVENT_NAME = "meeting-transcriber:rtc";
  const COMMAND_NAME = "meeting-transcriber:rtc-command";
  const CHANNEL_LABEL = "captions";
  const peerConnections = new Set();
  const attachedChannels = new WeakSet();
  const deviceNames = new Map();
  let enabled = false;
  let openedChannel = null;
  let packets = 0;
  let decoded = 0;
  let failures = 0;
  let collectionPackets = 0;
  let collectionResponses = 0;
  let sessionNonce = "";

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
      ...extra,
    });
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
        status({ reason: "unparsed-packet" });
        return;
      }
      decoded += 1;
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
    if (channel.label === "collections") {
      attachedChannels.add(channel);
      channel.binaryType = "arraybuffer";
      channel.addEventListener("message", (event) => {
        if (enabled) processCollectionMessage(event.data);
      });
      return;
    }
    if (channel.label !== CHANNEL_LABEL) return;
    attachedChannels.add(channel);
    openedChannel = channel;
    channel.binaryType = "arraybuffer";
    channel.addEventListener("message", (event) => {
      if (enabled) processMessage(event.data);
    });
    channel.addEventListener("open", () => status({ origin }));
    channel.addEventListener("close", () => status({ origin, reason: "closed" }));
    channel.addEventListener("error", () => status({ origin, reason: "channel-error" }));
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
    });
    status({ connectionState: peer.connectionState });
  }

  function openCaptionChannel() {
    if (!enabled) return;
    const peer = Array.from(peerConnections).find(
      (candidate) => !["closed", "failed"].includes(candidate.connectionState)
    );
    if (!peer) {
      status({ reason: "peer-missing" });
      return;
    }
    if (openedChannel && ["connecting", "open"].includes(openedChannel.readyState)) {
      status();
      return;
    }
    try {
      const channel = peer.createDataChannel(CHANNEL_LABEL, {
        ordered: true,
        maxRetransmits: 10,
      });
      attachChannel(channel, "local");
    } catch {
      status({ reason: "open-failed" });
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
    if (enabled) queueMicrotask(openCaptionChannel);
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
      openCaptionChannel();
      emitDevices();
      status();
    } else if (command === "stop") {
      enabled = false;
      status();
    } else if (command === "retry") {
      openedChannel = null;
      openCaptionChannel();
    } else if (command === "status") {
      emitDevices();
      status();
    }
  });

  globalThis.__meetingTranscriberRtcBridge = true;
})();
