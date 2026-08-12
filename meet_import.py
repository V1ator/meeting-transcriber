#!/usr/bin/env python3
"""Імпорт локального JSON із Chrome-розширення Google Meet captions."""

from __future__ import annotations

import argparse
import datetime
import json
import re
import time
from pathlib import Path
from typing import Any

import paths as project_paths

from pipeline_utils import (
    atomic_write_json,
    atomic_write_text,
    ensure_private_dir,
    update_manifest,
    utc_now,
)

BASE = Path(__file__).parent
MAX_IMPORT_BYTES = 10 * 1024 * 1024
MAX_ENTRIES = 20_000
MAX_TEXT_CHARS = 20_000
MAX_TOTAL_TEXT_CHARS = 2_000_000
MAX_FUZZY_TOKENS = 120
MAX_RELATED_SCAN = 50
NORMALIZE_TIMEOUT_SECONDS = 15.0
EXACT_MERGE_WINDOW_MS = 3_000
FUZZY_MERGE_WINDOW_MS = 3_000
PARTIAL_MERGE_WINDOW_MS = 90_000
FUZZY_PREFIX_SIMILARITY = 0.9
SHORT_CORRECTION_MIN_WORDS = 5
SHORT_CORRECTION_SIMILARITY = 0.8
REPLAY_WINDOW_MS = 90_000
REPLAY_MIN_WORDS = 6
REPLAY_BURST_MIN_WORDS = 4
REPLAY_BURST_WINDOW_MS = 100
TURN_GAP_MS = 2_500
MAX_TURN_MS = 90_000
MAX_TURN_WORDS = 300


class MeetImportError(ValueError):
    """The browser export is invalid or exceeds safe processing limits."""


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise MeetImportError("Нормалізація Meet export перевищила ліміт часу")


def _session_pipeline_module():
    # Lazy import keeps normalization reusable without loading audio processing.
    import audio_pipeline

    return audio_pipeline


def _clean_inline(value: Any, *, fallback: str = "") -> str:
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text or fallback


def _clean_speaker(value: Any) -> str:
    speaker = _clean_inline(value, fallback="Невідомий")[:200]
    speaker = re.sub(r"\s*&\s*\d+\s+others?\s*$", "", speaker, flags=re.I)
    speaker = re.sub(
        r"\s+(?:і|та|и)\s+ще\s+\d+\s+(?:учасник\w*|люд\w*)\s*$",
        "",
        speaker,
        flags=re.I,
    )
    return speaker.strip() or "Невідомий"


def _long_text(text: str) -> bool:
    return len(text) >= 80 or len(text.split()) >= 12


def _word_tokens(value: str) -> list[str]:
    return re.findall(r"[^\W_]+", value.casefold(), flags=re.UNICODE)


def _lcs_length(
    left: list[str], right: list[str], *, deadline: float | None = None
) -> int:
    _check_deadline(deadline)
    row = [0] * (len(right) + 1)
    for left_index, left_token in enumerate(left):
        if left_index % 8 == 0:
            _check_deadline(deadline)
        diagonal = 0
        for index, right_token in enumerate(right, start=1):
            previous = row[index]
            if left_token == right_token:
                row[index] = diagonal + 1
            else:
                row[index] = max(row[index], row[index - 1])
            diagonal = previous
    return row[-1]


def _fuzzy_expansion(
    left: str, right: str, *, deadline: float | None = None
) -> str | None:
    left_tokens = _word_tokens(left)
    right_tokens = _word_tokens(right)
    shorter_text, shorter_tokens = left, left_tokens
    longer_text, longer_tokens = right, right_tokens
    if len(left_tokens) > len(right_tokens):
        shorter_text, shorter_tokens = right, right_tokens
        longer_text, longer_tokens = left, left_tokens
    if (
        not _long_text(shorter_text)
        or not shorter_tokens
        or len(longer_tokens) < len(shorter_tokens)
        or len(shorter_tokens) > MAX_FUZZY_TOKENS
        or len(longer_tokens) > MAX_FUZZY_TOKENS
    ):
        return None
    allowance = max(2, (len(shorter_tokens) + 9) // 10)
    prefix = longer_tokens[:len(shorter_tokens) + allowance]
    similarity = _lcs_length(
        shorter_tokens, prefix, deadline=deadline
    ) / len(shorter_tokens)
    return longer_text if similarity >= FUZZY_PREFIX_SIMILARITY else None


def _short_correction(
    left: str, right: str, *, deadline: float | None = None
) -> str | None:
    left_tokens = _word_tokens(left)
    right_tokens = _word_tokens(right)
    if (
        len(left_tokens) < SHORT_CORRECTION_MIN_WORDS
        or len(right_tokens) < SHORT_CORRECTION_MIN_WORDS
        or len(left_tokens) > MAX_FUZZY_TOKENS
        or len(right_tokens) > MAX_FUZZY_TOKENS
    ):
        return None
    shorter = min(len(left_tokens), len(right_tokens))
    # Unrelated captions cannot be corrections. Check the stable prefix before
    # paying for the quadratic LCS comparison.
    if left_tokens[:2] != right_tokens[:2]:
        return None
    similarity = _lcs_length(
        left_tokens, right_tokens, deadline=deadline
    ) / shorter
    if similarity < SHORT_CORRECTION_SIMILARITY:
        return None
    # A correction normally keeps the beginning of the caption and changes a
    # word or adds its final part. Two matching leading tokens prevent nearby
    # but unrelated sentences from being collapsed.
    return right


def _contains_at_edge(left: str, right: str) -> str | None:
    left_tokens = _word_tokens(left)
    right_tokens = _word_tokens(right)
    shorter_text, shorter_tokens = left, left_tokens
    longer_text, longer_tokens = right, right_tokens
    if len(left_tokens) > len(right_tokens):
        shorter_text, shorter_tokens = right, right_tokens
        longer_text, longer_tokens = left, left_tokens
    if not shorter_tokens:
        return None
    if (
        longer_tokens[:len(shorter_tokens)] == shorter_tokens
        or longer_tokens[-len(shorter_tokens):] == shorter_tokens
    ):
        return longer_text
    return None


def _merge_related_text(
    left: str,
    right: str,
    *,
    allow_fuzzy: bool = False,
    deadline: float | None = None,
) -> str | None:
    if not left or not right:
        return None
    if left == right or left.startswith(right):
        return left
    if right.startswith(left):
        return right
    edge_match = _contains_at_edge(left, right)
    if edge_match:
        return edge_match
    if not allow_fuzzy:
        return None
    _check_deadline(deadline)
    return _fuzzy_expansion(
        left, right, deadline=deadline
    ) or _short_correction(left, right, deadline=deadline)


def _drop_recent_replays(
    entries: list[dict[str, Any]], *, deadline: float | None = None
) -> list[dict[str, Any]]:
    candidates = [False] * len(entries)
    word_counts = [0] * len(entries)
    last_seen: dict[tuple[str, str, str], int] = {}
    for index, item in enumerate(entries):
        _check_deadline(deadline)
        tokens = _word_tokens(item["text"])
        word_counts[index] = len(tokens)
        key = (item["kind"], item["speaker"], item["text"].casefold())
        previous = last_seen.get(key)
        if (
            item["kind"] == "caption"
            and previous is not None
            and item["start_ms"] - previous <= REPLAY_WINDOW_MS
        ):
            candidates[index] = True
        last_seen[key] = item["start_ms"]

    drop: set[int] = set()
    group_start = 0
    while group_start < len(entries):
        _check_deadline(deadline)
        group_end = group_start + 1
        while (
            group_end < len(entries)
            and entries[group_end]["start_ms"] - entries[group_start]["start_ms"]
            <= REPLAY_BURST_WINDOW_MS
        ):
            group_end += 1
        burst_anchor = any(
            candidates[index] and word_counts[index] >= REPLAY_MIN_WORDS
            for index in range(group_start, group_end)
        )
        burst_replays = sum(
            candidates[index]
            and word_counts[index] >= REPLAY_BURST_MIN_WORDS
            for index in range(group_start, group_end)
        )
        for index in range(group_start, group_end):
            if not candidates[index]:
                continue
            if (
                burst_anchor
                or (
                    word_counts[index] >= REPLAY_BURST_MIN_WORDS
                    and burst_replays >= 2
                )
            ):
                drop.add(index)
        group_start = group_end
    return [item for index, item in enumerate(entries) if index not in drop]


def _stitch_turn_text(
    left: str, right: str, *, deadline: float | None = None
) -> str:
    related = _merge_related_text(
        left, right, allow_fuzzy=True, deadline=deadline
    )
    if related:
        return related
    left_words = left.split()
    right_words = right.split()
    left_tokens = _word_tokens(left)
    right_tokens = _word_tokens(right)
    max_overlap = min(len(left_tokens), len(right_tokens), 20)
    for size in range(max_overlap, 0, -1):
        if left_tokens[-size:] == right_tokens[:size]:
            return " ".join([*left_words, *right_words[size:]])
    if left_words and right_words:
        left_last = _word_tokens(left_words[-1])
        right_first = _word_tokens(right_words[0])
        short_fragments = {"я", "і", "а", "у", "в", "з", "й", "ж", "б"}
        if (
            left_last and right_first
            and (len(left_last[0]) >= 2 or left_last[0] not in short_fragments)
            and right_first[0].startswith(left_last[0])
        ):
            left_words[-1] = right_words[0]
            return " ".join([*left_words, *right_words[1:]])
    return f"{left} {right}".strip()


def _assemble_turns(
    entries: list[dict[str, Any]], *, deadline: float | None = None
) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for item in entries:
        _check_deadline(deadline)
        if not turns:
            turns.append(item.copy())
            continue
        current = turns[-1]
        combined_words = len(_word_tokens(current["text"])) + len(
            _word_tokens(item["text"])
        )
        can_merge = (
            item["kind"] == "caption"
            and current["kind"] == "caption"
            and item["speaker"] == current["speaker"]
            and item["start_ms"] <= current["end_ms"] + TURN_GAP_MS
            and item["end_ms"] - current["start_ms"] <= MAX_TURN_MS
            and combined_words <= MAX_TURN_WORDS
            and not (
                current["text"].casefold() == item["text"].casefold()
                and len(_word_tokens(item["text"])) <= 3
            )
        )
        if not can_merge:
            turns.append(item.copy())
            continue
        current["text"] = _stitch_turn_text(
            current["text"], item["text"], deadline=deadline
        )
        current["end_ms"] = max(current["end_ms"], item["end_ms"])
    return turns


def _parse_datetime(value: Any) -> datetime.datetime:
    raw = _clean_inline(value)
    if not raw:
        raise MeetImportError("У JSON відсутній startedAt")
    try:
        parsed = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MeetImportError(f"Некоректний startedAt: {raw}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _meeting_code(value: Any) -> str:
    code = _clean_inline(value, fallback="meet").lower()
    code = re.sub(r"[^a-z0-9-]+", "-", code).strip("-")
    return code[:40] or "meet"


def session_id(data: dict[str, Any]) -> str:
    started = _parse_datetime(data.get("startedAt")).astimezone()
    return f"{started:%Y-%m-%d_%H-%M-%S}_meet-{_meeting_code(data.get('meetingCode'))}"


def validate_export(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise MeetImportError("Коренем JSON має бути object")
    if data.get("schemaVersion") != 1:
        raise MeetImportError("Підтримується лише schemaVersion=1")
    if data.get("source") != "google-meet-live-captions":
        raise MeetImportError("Невідоме джерело транскрипту")
    _parse_datetime(data.get("startedAt"))
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        raise MeetImportError("Транскрипт не містить реплік")
    if len(entries) > MAX_ENTRIES:
        raise MeetImportError(f"Забагато реплік: {len(entries)}")
    total_text_chars = sum(
        min(len(str(item.get("text", ""))), MAX_TEXT_CHARS)
        for item in entries
        if isinstance(item, dict)
    )
    if total_text_chars > MAX_TOTAL_TEXT_CHARS:
        raise MeetImportError(
            f"Забагато тексту в репліках: {total_text_chars} символів"
        )
    return data


def load_export(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_IMPORT_BYTES:
        raise MeetImportError(
            f"JSON перевищує {MAX_IMPORT_BYTES // (1024 * 1024)} MB"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MeetImportError(f"Некоректний JSON: {exc}") from exc
    return validate_export(data)


def normalized_entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    deadline = time.monotonic() + NORMALIZE_TIMEOUT_SECONDS
    prepared: list[dict[str, Any]] = []
    for index, raw in enumerate(data["entries"]):
        _check_deadline(deadline)
        if not isinstance(raw, dict):
            continue
        speaker = _clean_speaker(raw.get("speaker"))
        text = _clean_inline(raw.get("text"))[:MAX_TEXT_CHARS]
        kind = "chat" if raw.get("kind") == "chat" else "caption"
        if not text:
            continue
        try:
            start_ms = max(0, int(float(raw.get("startMs", 0))))
            end_ms = max(start_ms, int(float(raw.get("endMs", start_ms))))
        except (TypeError, ValueError):
            raise MeetImportError(
                f"Некоректний таймкод у репліці {index + 1}"
            ) from None
        prepared.append({
            "speaker": speaker,
            "text": text,
            "kind": kind,
            "start_ms": start_ms,
            "end_ms": end_ms,
        })

    # Browser and legacy exports are expected to be chronological, but sorting
    # here keeps negative ages from stretching replay windows indefinitely.
    # Python's stable sort preserves capture order for equal timestamps.
    prepared.sort(key=lambda item: item["start_ms"])
    prepared = _drop_recent_replays(prepared, deadline=deadline)
    _check_deadline(deadline)

    replay_indexes: set[int] = set()
    all_exact: set[tuple[str, str, str]] = set()
    group_start = 0
    while group_start < len(prepared):
        _check_deadline(deadline)
        start_ms = prepared[group_start]["start_ms"]
        group_end = group_start + 1
        while (
            group_end < len(prepared)
            and prepared[group_end]["start_ms"] == start_ms
        ):
            group_end += 1
        group = prepared[group_start:group_end]
        replay_batch = any(
            _long_text(item["text"])
            and (item["kind"], item["speaker"], item["text"]) in all_exact
            for item in group
        )
        for offset, item in enumerate(group):
            key = (item["kind"], item["speaker"], item["text"])
            if replay_batch and key in all_exact:
                replay_indexes.add(group_start + offset)
            all_exact.add(key)
        group_start = group_end

    result: list[dict[str, Any]] = []
    seen_exact: set[tuple[str, str, str]] = set()
    for index, item in enumerate(prepared):
        _check_deadline(deadline)
        if index in replay_indexes:
            continue
        speaker = item["speaker"]
        text = item["text"]
        kind = item["kind"]
        start_ms = item["start_ms"]
        end_ms = item["end_ms"]
        exact_key = (kind, speaker, text)
        if (kind == "chat" or _long_text(text)) and exact_key in seen_exact:
            continue

        related = None
        for candidate in reversed(result[-MAX_RELATED_SCAN:]):
            _check_deadline(deadline)
            age_ms = start_ms - candidate["end_ms"]
            if age_ms > PARTIAL_MERGE_WINDOW_MS:
                break
            if candidate["speaker"] != speaker or candidate["kind"] != kind:
                continue
            if (len(text) >= 4 and text == candidate["text"]
                    and age_ms <= EXACT_MERGE_WINDOW_MS):
                related = candidate
                break
            if (
                text != candidate["text"]
                and len(text) >= 4
                and _merge_related_text(
                    candidate["text"],
                    text,
                    allow_fuzzy=age_ms <= FUZZY_MERGE_WINDOW_MS,
                    deadline=deadline,
                )
            ):
                related = candidate
                break
        if related:
            related["text"] = _merge_related_text(
                related["text"],
                text,
                allow_fuzzy=start_ms - related["end_ms"] <= FUZZY_MERGE_WINDOW_MS,
                deadline=deadline,
            ) or (
                text if len(text) > len(related["text"]) else related["text"]
            )
            related["end_ms"] = max(related["end_ms"], end_ms)
            if kind == "chat" or _long_text(related["text"]):
                seen_exact.add((kind, speaker, related["text"]))
            continue
        result.append(item)
        if kind == "chat" or _long_text(text):
            seen_exact.add(exact_key)
    if not result:
        raise MeetImportError("Після нормалізації не залишилося реплік")
    _check_deadline(deadline)
    return _assemble_turns(result, deadline=deadline)


def render_markdown(data: dict[str, Any], entries: list[dict[str, Any]]) -> str:
    started = _parse_datetime(data["startedAt"]).astimezone()
    title = _clean_inline(data.get("meetingTitle"), fallback="Google Meet")
    participants: list[str] = []
    seen_participants: set[str] = set()
    raw_participants = data.get("participants")
    if not isinstance(raw_participants, list):
        raw_participants = []
    for raw in [*raw_participants, *(entry["speaker"] for entry in entries)]:
        participant = _clean_speaker(raw)
        key = participant.casefold()
        if participant == "Невідомий" or key in seen_participants:
            continue
        seen_participants.add(key)
        participants.append(participant)
    lines = [
        f"# {title}",
        "",
        f"- **Час початку:** {started:%Y-%m-%d %H:%M:%S %Z}",
        f"- **Назва зустрічі:** {title}",
        "",
        "**Учасники:**",
        *(f"- {participant}" for participant in participants),
        "",
        "## Транскрипт",
        "",
    ]
    for entry in entries:
        timestamp = started + datetime.timedelta(milliseconds=entry["start_ms"])
        speaker = entry["speaker"]
        if entry["kind"] == "chat":
            speaker = f"{speaker} (chat)"
        lines.append(
            f"[{timestamp:%H:%M:%S}] {speaker}: {entry['text']}"
        )
    return "\n".join(lines) + "\n"


def import_export(path: Path, *, summarize: bool = True, force: bool = False) -> Path:
    data = load_export(path)

    session = session_id(data)
    transcript_path = project_paths.TRANSCRIPTS / f"{session}.md"
    if transcript_path.exists() and not force:
        raise FileExistsError(
            f"Сесію {session} вже імпортовано; використайте --force для оновлення"
        )

    entries = normalized_entries(data)
    transcript = render_markdown(data, entries)
    ensure_private_dir(project_paths.TRANSCRIPTS)
    work_dir = ensure_private_dir(project_paths.TRANSCRIPTS / session)
    atomic_write_json(work_dir / "meet-captions.json", data, mode=0o600)
    atomic_write_text(transcript_path, transcript, mode=0o600)
    atomic_write_json(work_dir / "manifest.json", {
        "schema_version": 1,
        "session": session,
        "completed_at": utc_now(),
        "source": "google-meet-live-captions",
        "quality": {
            "segments": len(entries),
            "speakers": sorted({entry["speaker"] for entry in entries}),
            "browser_caption_language": data.get("language"),
        },
        "output": str(transcript_path),
    }, mode=0o600)

    if not summarize:
        return transcript_path

    session_pipeline = _session_pipeline_module()
    ensure_private_dir(project_paths.RECORDINGS)
    atomic_write_json(session_pipeline.manifest_path(session), {
        "schema_version": 1,
        "session": session,
        "status": "processing",
        "stage": "summarizing",
        "source": "google-meet-live-captions",
        "created_at": utc_now(),
    }, mode=0o600)
    note = session_pipeline.create_note_from_transcript(session, transcript)
    update_manifest(
        session_pipeline.manifest_path(session),
        status="complete",
        stage="complete",
        note=str(note),
        completed_at=utc_now(),
    )
    return note


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_export", type=Path)
    parser.add_argument(
        "--no-summary",
        action="store_true",
        help="лише створити Markdown-транскрипт, не запускати Ollama",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    result = import_export(
        args.json_export,
        summarize=not args.no_summary,
        force=args.force,
    )
    print(f"Готово: {result}")


if __name__ == "__main__":
    main()
