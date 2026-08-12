(function (root, factory) {
  const api = factory();
  root.MeetingCaptionModel = api;
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function normalizeText(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }

  function cleanSpeaker(value) {
    return normalizeText(value)
      .replace(/\s*&\s*\d+\s+others?\s*$/i, "")
      .replace(
        /\s+(?:і|та|и)\s+ще\s+\d+\s+(?:учасник[\p{L}\p{N}_]*|люд[\p{L}\p{N}_]*)\s*$/iu,
        ""
      )
      .trim() || "Невідомий";
  }

  function entryKind(value) {
    return value === "chat" ? "chat" : "caption";
  }

  function addParticipant(state, value) {
    const participant = cleanSpeaker(value);
    if (participant === "Невідомий") return false;
    if (!Array.isArray(state.participants)) state.participants = [];
    const key = participant.toLocaleLowerCase();
    if (state.participants.some(
      (candidate) => cleanSpeaker(candidate).toLocaleLowerCase() === key
    )) return false;
    state.participants.push(participant);
    return true;
  }

  function createState(seed) {
    const source = seed && typeof seed === "object" ? seed : {};
    const entries = Array.isArray(source.entries) ? source.entries : [];
    const highestEntryId = entries.reduce(
      (highest, entry) => Math.max(highest, Number(entry?.id) || 0), 0
    );
    const state = {
      schemaVersion: 1,
      source: "google-meet-live-captions",
      meetingCode: normalizeText(source.meetingCode),
      meetingTitle: normalizeText(source.meetingTitle) || "Google Meet",
      language: normalizeText(source.language) || "uk",
      startedAt: source.startedAt || null,
      endedAt: source.endedAt || null,
      participants: [],
      entries,
      activeKeys: source.activeKeys && typeof source.activeKeys === "object"
        ? { ...source.activeKeys } : {},
      sourceKeys: source.sourceKeys && typeof source.sourceKeys === "object"
        ? { ...source.sourceKeys } : {},
      sourceVersions: source.sourceVersions
        && typeof source.sourceVersions === "object"
        ? { ...source.sourceVersions } : {},
      nextId: Math.max(
        Number.isInteger(source.nextId) ? source.nextId : 1,
        highestEntryId + 1
      ),
      revision: Number.isInteger(source.revision) ? source.revision : 0,
    };
    if (Array.isArray(source.participants)) {
      source.participants.forEach((participant) => addParticipant(state, participant));
    }
    state.entries.forEach((entry) => addParticipant(state, entry?.speaker));
    return state;
  }

  function exactEntry(state, speaker, text, kind) {
    for (let index = state.entries.length - 1; index >= 0; index -= 1) {
      const entry = state.entries[index];
      if (cleanSpeaker(entry.speaker) === speaker
          && normalizeText(entry.text) === text
          && entryKind(entry.kind) === kind) return entry;
    }
    return null;
  }

  function observe(state, observation) {
    const key = normalizeText(observation.key);
    const speaker = cleanSpeaker(observation.speaker);
    const text = normalizeText(observation.text);
    const kind = entryKind(observation.kind);
    const atMs = Math.max(0, Number(observation.atMs) || 0);
    if (!key || text.length < 2) return null;
    addParticipant(state, speaker);
    if (!state.startedAt) {
      state.startedAt = observation.observedAt || new Date().toISOString();
    }

    let entry = state.entries.find(
      (candidate) => candidate.id === state.activeKeys[key]
    );
    if (!entry && kind === "chat") {
      entry = exactEntry(state, speaker, text, kind);
      if (entry) return entry;
    }
    if (!entry) {
      entry = {
        id: state.nextId++,
        speaker,
        text,
        kind,
        startMs: atMs,
        endMs: atMs,
        final: false,
      };
      state.entries.push(entry);
    } else {
      entry.speaker = speaker;
      entry.text = text;
      entry.kind = kind;
      entry.endMs = Math.max(entry.endMs, atMs);
      entry.final = false;
    }
    state.activeKeys[key] = entry.id;
    state.revision += 1;
    return entry;
  }

  function observeVersioned(state, observation) {
    const key = normalizeText(observation.key);
    const speaker = cleanSpeaker(observation.speaker);
    const text = normalizeText(observation.text);
    const kind = entryKind(observation.kind);
    const atMs = Math.max(0, Number(observation.atMs) || 0);
    const version = Number(observation.version);
    if (!key || text.length < 2 || !Number.isSafeInteger(version)
        || version < 0) return null;
    addParticipant(state, speaker);
    if (!state.startedAt) {
      state.startedAt = observation.observedAt || new Date().toISOString();
    }
    if (!state.sourceKeys || typeof state.sourceKeys !== "object") {
      state.sourceKeys = {};
    }
    if (!state.sourceVersions || typeof state.sourceVersions !== "object") {
      state.sourceVersions = {};
    }
    const previousVersion = Number(state.sourceVersions[key]);
    if (Number.isSafeInteger(previousVersion) && version <= previousVersion) {
      return null;
    }

    let entry = state.entries.find(
      (candidate) => candidate.id === state.sourceKeys[key]
    );
    if (!entry) {
      entry = {
        id: state.nextId++,
        speaker,
        text,
        kind,
        captureSource: "rtc",
        startMs: atMs,
        endMs: atMs,
        final: false,
      };
      state.entries.push(entry);
    } else {
      entry.speaker = speaker;
      entry.text = text;
      entry.kind = kind;
      entry.captureSource = "rtc";
      entry.endMs = Math.max(entry.endMs, atMs);
      entry.final = false;
    }
    state.sourceKeys[key] = entry.id;
    state.sourceVersions[key] = version;
    state.activeKeys[key] = entry.id;
    state.revision += 1;
    return entry;
  }

  function finalize(state, key, atMs) {
    const id = state.activeKeys[key];
    if (id === undefined) return null;
    const entry = state.entries.find((candidate) => candidate.id === id);
    if (entry) {
      const changed = entry.endMs < Math.max(0, Number(atMs) || 0) || !entry.final;
      entry.endMs = Math.max(entry.endMs, Math.max(0, Number(atMs) || 0));
      entry.final = true;
      if (changed) state.revision += 1;
    }
    delete state.activeKeys[key];
    return entry || null;
  }

  function compactEntries(entries) {
    const result = [];
    const exactChats = new Set();
    (Array.isArray(entries) ? entries : []).forEach((source) => {
      const speaker = cleanSpeaker(source?.speaker);
      const text = normalizeText(source?.text);
      const kind = entryKind(source?.kind);
      if (text.length < 2) return;
      const startMs = Math.max(0, Number(source?.startMs) || 0);
      const endMs = Math.max(startMs, Number(source?.endMs) || startMs);
      if (kind === "chat") {
        const key = `${speaker}\u0000${text.toLocaleLowerCase()}`;
        if (exactChats.has(key)) return;
        exactChats.add(key);
      }
      result.push({ speaker, text, kind, startMs, endMs });
    });
    return result;
  }

  function exportState(state, endedAt) {
    const participants = [];
    const participantState = { participants };
    (state.participants || []).forEach(
      (participant) => addParticipant(participantState, participant)
    );
    state.entries.forEach(
      (entry) => addParticipant(participantState, entry?.speaker)
    );
    return {
      schemaVersion: 1,
      source: "google-meet-live-captions",
      meetingCode: state.meetingCode,
      startedAt: state.startedAt || new Date().toISOString(),
      meetingTitle: state.meetingTitle,
      participants,
      language: state.language,
      endedAt: endedAt || new Date().toISOString(),
      entries: compactEntries(state.entries).map((entry) => ({
        speaker: entry.speaker,
        text: entry.text,
        ...(entry.kind === "chat" ? { kind: "chat" } : {}),
        startMs: Math.round(entry.startMs),
        endMs: Math.round(entry.endMs),
      })),
    };
  }

  return {
    addParticipant,
    cleanSpeaker,
    compactEntries,
    createState,
    exportState,
    finalize,
    normalizeText,
    observe,
    observeVersioned,
  };
});
