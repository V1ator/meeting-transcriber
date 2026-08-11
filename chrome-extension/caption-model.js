(function (root, factory) {
  const api = factory();
  root.MeetingCaptionModel = api;
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const EXACT_MERGE_WINDOW_MS = 3_000;
  const FUZZY_MERGE_WINDOW_MS = 3_000;
  const PARTIAL_MERGE_WINDOW_MS = 90_000;
  const FUZZY_PREFIX_SIMILARITY = 0.9;
  const SHORT_CORRECTION_MIN_WORDS = 5;
  const SHORT_CORRECTION_SIMILARITY = 0.8;
  const REPLAY_WINDOW_MS = 90_000;
  const REPLAY_MIN_WORDS = 6;
  const REPLAY_BURST_MIN_WORDS = 4;
  const REPLAY_BURST_WINDOW_MS = 100;
  const TURN_GAP_MS = 2_500;
  const MAX_TURN_MS = 90_000;
  const MAX_TURN_WORDS = 300;

  function normalizeText(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }

  function cleanSpeaker(value) {
    return normalizeText(value)
      .replace(/\s*&\s*\d+\s+others?\s*$/i, "")
      .replace(/\s+(?:і|та|и)\s+ще\s+\d+\s+(?:учасник\w*|люд\w*)\s*$/iu, "")
      .trim() || "Невідомий";
  }

  function entryKind(value) {
    return value === "chat" ? "chat" : "caption";
  }

  function addParticipant(state, value) {
    const normalized = normalizeText(value);
    if (!normalized) return false;
    const participant = cleanSpeaker(normalized);
    if (participant === "Невідомий") return false;
    if (!Array.isArray(state.participants)) state.participants = [];
    const key = participant.toLocaleLowerCase();
    if (state.participants.some(
      (candidate) => cleanSpeaker(candidate).toLocaleLowerCase() === key
    )) return false;
    state.participants.push(participant);
    return true;
  }

  function isLongText(text) {
    return text.length >= 80 || text.split(/\s+/).length >= 12;
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
      activeKeys: {},
      replayKeys: {},
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

  function wordTokens(value) {
    return normalizeText(value).toLocaleLowerCase().match(/[\p{L}\p{N}]+/gu) || [];
  }

  function lcsLength(left, right) {
    const row = new Array(right.length + 1).fill(0);
    for (const leftToken of left) {
      let diagonal = 0;
      for (let index = 1; index <= right.length; index += 1) {
        const previous = row[index];
        if (leftToken === right[index - 1]) {
          row[index] = diagonal + 1;
        } else {
          row[index] = Math.max(row[index], row[index - 1]);
        }
        diagonal = previous;
      }
    }
    return row[right.length];
  }

  function fuzzyExpansion(left, right) {
    const leftTokens = wordTokens(left);
    const rightTokens = wordTokens(right);
    let shorterText = left;
    let shorterTokens = leftTokens;
    let longerText = right;
    let longerTokens = rightTokens;
    if (leftTokens.length > rightTokens.length) {
      shorterText = right;
      shorterTokens = rightTokens;
      longerText = left;
      longerTokens = leftTokens;
    }
    if (!isLongText(shorterText) || !shorterTokens.length
        || longerTokens.length < shorterTokens.length) return null;
    const allowance = Math.max(2, Math.ceil(shorterTokens.length * 0.1));
    const prefix = longerTokens.slice(0, shorterTokens.length + allowance);
    const similarity = lcsLength(shorterTokens, prefix) / shorterTokens.length;
    return similarity >= FUZZY_PREFIX_SIMILARITY ? longerText : null;
  }

  function shortCorrection(left, right) {
    const leftTokens = wordTokens(left);
    const rightTokens = wordTokens(right);
    if (leftTokens.length < SHORT_CORRECTION_MIN_WORDS
        || rightTokens.length < SHORT_CORRECTION_MIN_WORDS) return null;
    const shorter = Math.min(leftTokens.length, rightTokens.length);
    const similarity = lcsLength(leftTokens, rightTokens) / shorter;
    if (similarity < SHORT_CORRECTION_SIMILARITY) return null;
    if (leftTokens[0] !== rightTokens[0] || leftTokens[1] !== rightTokens[1]) {
      return null;
    }
    return right;
  }

  function edgeMatch(left, right) {
    const leftTokens = wordTokens(left);
    const rightTokens = wordTokens(right);
    let shorterText = left;
    let shorterTokens = leftTokens;
    let longerText = right;
    let longerTokens = rightTokens;
    if (leftTokens.length > rightTokens.length) {
      shorterText = right;
      shorterTokens = rightTokens;
      longerText = left;
      longerTokens = leftTokens;
    }
    if (!shorterTokens.length) return null;
    const prefix = longerTokens.slice(0, shorterTokens.length);
    const suffix = longerTokens.slice(-shorterTokens.length);
    const equal = (candidate) => candidate.length === shorterTokens.length
      && candidate.every((token, index) => token === shorterTokens[index]);
    return equal(prefix) || equal(suffix) ? longerText : null;
  }

  function mergeRelatedText(left, right, allowFuzzy = false) {
    if (!left || !right) return null;
    if (left === right) return left;
    if (left.startsWith(right)) return left;
    if (right.startsWith(left)) return right;
    const contained = edgeMatch(left, right);
    if (contained) return contained;
    return allowFuzzy
      ? fuzzyExpansion(left, right) || shortCorrection(left, right)
      : null;
  }

  function dropRecentReplays(entries) {
    const candidates = new Array(entries.length).fill(false);
    const wordCounts = new Array(entries.length).fill(0);
    const lastSeen = new Map();
    entries.forEach((source, index) => {
      const speaker = cleanSpeaker(source.speaker);
      const text = normalizeText(source.text);
      const kind = entryKind(source.kind);
      const startMs = Math.max(0, Number(source.startMs) || 0);
      wordCounts[index] = wordTokens(text).length;
      const key = `${kind}\u0000${speaker}\u0000${text.toLocaleLowerCase()}`;
      const previous = lastSeen.get(key);
      if (source.captureSource !== "rtc" && kind === "caption"
          && previous !== undefined
          && startMs - previous <= REPLAY_WINDOW_MS) candidates[index] = true;
      lastSeen.set(key, startMs);
    });

    const drop = new Set();
    for (let start = 0; start < entries.length;) {
      const startMs = Math.max(0, Number(entries[start]?.startMs) || 0);
      let end = start + 1;
      while (end < entries.length
          && Math.max(0, Number(entries[end]?.startMs) || 0) - startMs
            <= REPLAY_BURST_WINDOW_MS) end += 1;
      let burstAnchor = false;
      let burstReplays = 0;
      for (let index = start; index < end; index += 1) {
        if (!candidates[index]) continue;
        if (wordCounts[index] >= REPLAY_MIN_WORDS) burstAnchor = true;
        if (wordCounts[index] >= REPLAY_BURST_MIN_WORDS) burstReplays += 1;
      }
      for (let index = start; index < end; index += 1) {
        if (!candidates[index]) continue;
        if (burstAnchor || (wordCounts[index] >= REPLAY_BURST_MIN_WORDS
            && burstReplays >= 2)) drop.add(index);
      }
      start = end;
    }
    return entries.filter((_, index) => !drop.has(index));
  }

  function stitchTurnText(left, right) {
    const related = mergeRelatedText(left, right, true);
    if (related) return related;
    const leftWords = normalizeText(left).split(/\s+/);
    const rightWords = normalizeText(right).split(/\s+/);
    const leftTokens = wordTokens(left);
    const rightTokens = wordTokens(right);
    const maxOverlap = Math.min(leftTokens.length, rightTokens.length, 20);
    for (let size = maxOverlap; size > 0; size -= 1) {
      if (leftTokens.slice(-size).every(
        (token, index) => token === rightTokens[index]
      )) return [...leftWords, ...rightWords.slice(size)].join(" ");
    }
    const leftLast = wordTokens(leftWords.at(-1) || "")[0];
    const rightFirst = wordTokens(rightWords[0] || "")[0];
    const shortFragments = new Set(["я", "і", "а", "у", "в", "з", "й", "ж", "б"]);
    if (leftLast && rightFirst
        && (leftLast.length >= 2 || !shortFragments.has(leftLast))
        && rightFirst.startsWith(leftLast)) {
      leftWords[leftWords.length - 1] = rightWords[0];
      return [...leftWords, ...rightWords.slice(1)].join(" ");
    }
    return `${left} ${right}`.trim();
  }

  function assembleTurns(entries) {
    const turns = [];
    entries.forEach((source) => {
      const item = { ...source };
      const current = turns.at(-1);
      if (!current) {
        turns.push(item);
        return;
      }
      const combinedWords = wordTokens(current.text).length + wordTokens(item.text).length;
      const repeatedShortReply = current.text.toLocaleLowerCase()
        === item.text.toLocaleLowerCase() && wordTokens(item.text).length <= 3;
      const canMerge = current.kind === "caption" && item.kind === "caption"
        && current.speaker === item.speaker
        && item.startMs <= current.endMs + TURN_GAP_MS
        && item.endMs - current.startMs <= MAX_TURN_MS
        && combinedWords <= MAX_TURN_WORDS
        && !repeatedShortReply;
      if (!canMerge) {
        turns.push(item);
        return;
      }
      current.text = stitchTurnText(current.text, item.text);
      current.endMs = Math.max(current.endMs, item.endMs);
    });
    return turns;
  }

  function recentRelatedEntry(state, speaker, text, kind, atMs) {
    for (let index = state.entries.length - 1; index >= 0; index -= 1) {
      const entry = state.entries[index];
      const ageMs = atMs - entry.endMs;
      if (ageMs > PARTIAL_MERGE_WINDOW_MS) return null;
      if (entry.speaker !== speaker || entryKind(entry.kind) !== kind) continue;
      if (text.length >= 4 && entry.text === text
          && ageMs <= EXACT_MERGE_WINDOW_MS) return entry;
      if (text.length >= 4 && entry.text !== text
          && mergeRelatedText(
            entry.text, text, ageMs <= FUZZY_MERGE_WINDOW_MS
          )) return entry;
    }
    return null;
  }

  function exactEntry(state, speaker, text, kind) {
    for (let index = state.entries.length - 1; index >= 0; index -= 1) {
      const entry = state.entries[index];
      if (entry.speaker === speaker && entryKind(entry.kind) === kind
          && entry.text === text) return entry;
    }
    return null;
  }

  function isHistoricalReplay(state, speaker, text, kind = "caption") {
    const normalized = normalizeText(text);
    if (!isLongText(normalized)) return false;
    return Boolean(exactEntry(
      state, cleanSpeaker(speaker), normalized, entryKind(kind)
    ));
  }

  function replayEntry(state, speaker, text, kind, replayScan) {
    if (!replayScan && kind !== "chat" && !isLongText(text)) return null;
    return exactEntry(state, speaker, text, kind);
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
    if (entry && state.replayKeys[key]) {
      if (entry.speaker === speaker && entry.text === text
          && entryKind(entry.kind) === kind) return entry;
      delete state.activeKeys[key];
      delete state.replayKeys[key];
      entry = null;
    }
    if (entry && (entry.speaker !== speaker || entryKind(entry.kind) !== kind)) {
      entry.final = true;
      entry = null;
    }
    if (!entry) {
      entry = recentRelatedEntry(state, speaker, text, kind, atMs);
    }
    if (!entry) {
      entry = replayEntry(
        state, speaker, text, kind, observation.replayScan === true
      );
      if (entry) {
        state.activeKeys[key] = entry.id;
        state.replayKeys[key] = true;
        return entry;
      }
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
      entry.text = mergeRelatedText(entry.text, text, true) || text;
      entry.kind = kind;
      entry.endMs = Math.max(entry.endMs, atMs);
      entry.final = false;
    }
    state.revision += 1;
    state.activeKeys[key] = entry.id;
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
    delete state.replayKeys[key];
    state.revision += 1;
    return entry;
  }

  function finalize(state, key, atMs) {
    const id = state.activeKeys[key];
    if (id === undefined) return null;
    const entry = state.entries.find((candidate) => candidate.id === id);
    if (entry && !state.replayKeys[key]) {
      const changed = entry.endMs < Math.max(0, Number(atMs) || 0) || !entry.final;
      entry.endMs = Math.max(entry.endMs, Math.max(0, Number(atMs) || 0));
      entry.final = true;
      if (changed) state.revision += 1;
    }
    delete state.activeKeys[key];
    delete state.replayKeys[key];
    return entry || null;
  }

  function compactEntries(entries) {
    entries = dropRecentReplays(entries);
    const result = [];
    const longExact = new Set();
    const replayIndexes = new Set();
    const allExact = new Set();
    for (let start = 0; start < entries.length;) {
      const startMs = Math.max(0, Number(entries[start]?.startMs) || 0);
      let end = start + 1;
      while (end < entries.length
          && Math.max(0, Number(entries[end]?.startMs) || 0) === startMs) {
        end += 1;
      }
      const group = entries.slice(start, end);
      const replayBatch = group.some((source) => {
        if (source.captureSource === "rtc") return false;
        const speaker = cleanSpeaker(source.speaker);
        const text = normalizeText(source.text);
        const kind = entryKind(source.kind);
        return isLongText(text)
          && allExact.has(`${kind}\u0000${speaker}\u0000${text}`);
      });
      group.forEach((source, offset) => {
        const speaker = cleanSpeaker(source.speaker);
        const text = normalizeText(source.text);
        const kind = entryKind(source.kind);
        const key = `${kind}\u0000${speaker}\u0000${text}`;
        if (source.captureSource !== "rtc" && replayBatch && allExact.has(key)) {
          replayIndexes.add(start + offset);
        }
        allExact.add(key);
      });
      start = end;
    }

    entries.forEach((source, sourceIndex) => {
      if (replayIndexes.has(sourceIndex)) return;
      const speaker = cleanSpeaker(source.speaker);
      const text = normalizeText(source.text);
      const kind = entryKind(source.kind);
      if (text.length < 2) return;
      const startMs = Math.max(0, Number(source.startMs) || 0);
      const endMs = Math.max(startMs, Number(source.endMs) || startMs);
      const exactKey = `${kind}\u0000${speaker}\u0000${text}`;
      if (source.captureSource !== "rtc"
          && (kind === "chat" || isLongText(text))
          && longExact.has(exactKey)) return;

      let related = null;
      if (source.captureSource !== "rtc") {
        for (let index = result.length - 1; index >= 0; index -= 1) {
          const candidate = result[index];
          if (startMs - candidate.endMs > PARTIAL_MERGE_WINDOW_MS) break;
          if (candidate.speaker !== speaker || candidate.kind !== kind) continue;
          if (text.length >= 4 && candidate.text === text
              && startMs - candidate.endMs <= EXACT_MERGE_WINDOW_MS) {
            related = candidate;
            break;
          }
          if (candidate.text !== text && text.length >= 4
              && mergeRelatedText(
                candidate.text,
                text,
                startMs - candidate.endMs <= FUZZY_MERGE_WINDOW_MS
              )) {
            related = candidate;
            break;
          }
        }
      }
      if (related) {
        related.text = mergeRelatedText(
          related.text,
          text,
          startMs - related.endMs <= FUZZY_MERGE_WINDOW_MS
        ) || (text.length > related.text.length ? text : related.text);
        related.endMs = Math.max(related.endMs, endMs);
        if (kind === "chat" || isLongText(related.text)) {
          longExact.add(`${kind}\u0000${speaker}\u0000${related.text}`);
        }
        return;
      }
      result.push({
        speaker,
        text,
        kind,
        startMs,
        endMs,
        ...(source.captureSource ? { captureSource: source.captureSource } : {}),
      });
      if (source.captureSource !== "rtc"
          && (kind === "chat" || isLongText(text))) longExact.add(exactKey);
    });
    return assembleTurns(result);
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
    isHistoricalReplay,
    normalizeText,
    observe,
    observeVersioned,
  };
});
