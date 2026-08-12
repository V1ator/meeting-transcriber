#!/usr/bin/env python3
"""Audio-session processing and durable meeting-note creation."""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import paths as project_paths
import summary_pipeline as summary_pipeline
from pipeline_utils import (
    atomic_write_json,
    atomic_write_text,
    audio_info,
    audio_signal_info,
    ensure_private_dir,
    load_dotenv,
    read_json,
    update_manifest,
    utc_now,
)

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

AUDIO_PIPELINE_ENABLED = (
    os.environ.get("AUDIO_PIPELINE_ENABLED", "true").lower() == "true"
)
OLLAMA_MODEL = summary_pipeline.OLLAMA_MODEL
OLLAMA_NUM_CTX = summary_pipeline.OLLAMA_NUM_CTX
CANDIDATE_EVALUATION_ENABLED = (
    os.environ.get("CANDIDATE_EVALUATION_ENABLED", "false").lower() == "true"
)
CANDIDATE_TARGET_LEVEL = os.environ.get("CANDIDATE_TARGET_LEVEL", "").strip()
CANDIDATE_OLLAMA_THINK = (
    os.environ.get("CANDIDATE_OLLAMA_THINK", "true").lower() == "true"
)
ROTATE_DAYS = int(os.environ.get("ROTATE_DAYS", "5"))
MIN_SESSION_SECONDS = float(os.environ.get("MIN_SESSION_SECONDS", "10"))
SILENT_RECORDING_PEAK_DBFS = float(
    os.environ.get("SILENT_RECORDING_PEAK_DBFS", "-70")
)
MAX_AUTO_RETRIES = int(os.environ.get("MAX_AUTO_RETRIES", "8"))
STABLE_SECONDS = 60
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")

_sha256_text = summary_pipeline._sha256_text
_validated_evidence_ledger = summary_pipeline._validated_evidence_ledger
_deduplicate_evidence_items = summary_pipeline._deduplicate_evidence_items
_normalize_evidence_lifecycle = summary_pipeline._normalize_evidence_lifecycle
_ground_claim_language = summary_pipeline._ground_claim_language
_summary_quality_report = summary_pipeline._summary_quality_report
_render_grounded_sections = summary_pipeline._render_grounded_sections
_valid_summary = summary_pipeline._valid_summary
SUMMARY_TEMPLATE = summary_pipeline.SUMMARY_TEMPLATE
PROMPT_FINGERPRINT = summary_pipeline.PROMPT_FINGERPRINT
SUMMARY_EXTRACT_THINK = summary_pipeline.SUMMARY_EXTRACT_THINK
SUMMARY_RECONCILE_THINK = summary_pipeline.SUMMARY_RECONCILE_THINK


class SessionBusy(RuntimeError):
    pass


@contextlib.contextmanager
def session_lock(session: str):
    _require_safe_session_id(session)
    lock_path = project_paths.TRANSCRIPTS / session / ".processing.lock"
    ensure_private_dir(lock_path.parent)
    handle = lock_path.open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SessionBusy(f"Сесію {session} вже обробляє інший процес") from exc
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def log(message: str) -> None:
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def _is_safe_session_id(session: str) -> bool:
    return bool(
        session
        and session not in {".", ".."}
        and Path(session).name == session
        and SESSION_ID_PATTERN.fullmatch(session)
    )


def _require_safe_session_id(session: str) -> str:
    if not _is_safe_session_id(session):
        raise ValueError(f"Некоректний session ID: {session!r}")
    return session


def note_for(session: str) -> Path | None:
    if not _is_safe_session_id(session):
        return None
    hits = sorted(project_paths.NOTES.glob(f"{session}*.md"))
    return hits[0] if hits else None


def manifest_path(session: str) -> Path:
    _require_safe_session_id(session)
    return project_paths.RECORDINGS / f"{session}.json"


def _legacy_ready(mic: Path, sys_wav: Path, now: float) -> bool:
    return all(now - path.stat().st_mtime >= STABLE_SECONDS for path in (mic, sys_wav))


def find_ready_sessions() -> list[str]:
    if not AUDIO_PIPELINE_ENABLED:
        return []
    ready: set[str] = set()
    now = time.time()
    for mic in project_paths.RECORDINGS.glob("*_mic.wav"):
        session = mic.name.removesuffix("_mic.wav")
        if not _is_safe_session_id(session):
            continue
        sys_wav = project_paths.RECORDINGS / f"{session}_sys.wav"
        if not sys_wav.exists() or note_for(session):
            continue
        manifest = read_json(manifest_path(session), {}) or {}
        status = manifest.get("status")
        attempts = int(manifest.get("processing_attempts", 0))
        retry_at = float(manifest.get("next_retry_at", 0) or 0)
        if status == "processing_failed":
            if attempts < MAX_AUTO_RETRIES and now >= retry_at:
                ready.add(session)
            elif attempts >= MAX_AUTO_RETRIES:
                update_manifest(
                    manifest_path(session), status="terminal_failed",
                    stage="retry_limit", next_retry_at=None,
                    last_error="Вичерпано автоматичні спроби обробки",
                )
                log(f"  ПОМИЛКА: {session}; вичерпано {attempts} спроб")
        elif status == "processing":
            if attempts < MAX_AUTO_RETRIES:
                ready.add(session)
            else:
                update_manifest(
                    manifest_path(session), status="terminal_failed",
                    stage="interrupted", next_retry_at=None,
                    last_error="Обробку перервано під час останньої спроби",
                )
                log(f"  ПОМИЛКА: {session}; обробку перервано {attempts} разів")
        elif status == "recorded":
            ready.add(session)
        elif status in {"recording", "recording_failed", "terminal_failed", "complete"}:
            continue
        elif _legacy_ready(mic, sys_wav, now):
            ready.add(session)
    return sorted(ready)


def _ensure_manifest(session: str) -> dict:
    path = manifest_path(session)
    manifest = read_json(path, {}) or {}
    if manifest:
        return manifest
    mic = project_paths.RECORDINGS / f"{session}_mic.wav"
    sys_wav = project_paths.RECORDINGS / f"{session}_sys.wav"
    manifest = {
        "schema_version": 1,
        "session": session,
        "status": "recorded",
        "created_at": utc_now(),
        "legacy": True,
        "tracks": {"mic": audio_info(mic), "sys": audio_info(sys_wav)},
    }
    atomic_write_json(path, manifest)
    return manifest


def _quality_warning(session: str) -> str | None:
    transcript_manifest = read_json(project_paths.TRANSCRIPTS / session / "manifest.json", {}) or {}
    quality = transcript_manifest.get("quality") or {}
    ratio = float(quality.get("unknown_speaker_ratio", 0) or 0)
    local_ratio = float(quality.get("local_unknown_speaker_ratio", 0) or 0)
    scale = float((quality.get("sync") or {}).get("scale", 1) or 1)
    warnings = []
    if ratio > 0.15:
        warnings.append(f"{ratio:.0%} реплік співрозмовників без speaker label")
    if local_ratio > 0.15:
        warnings.append(f"{local_ratio:.0%} локальних реплік без speaker label")
    if abs(scale - 1.0) > 0.005:
        warnings.append(f"значна корекція clock drift: ×{scale:.6f}")
    return "; ".join(warnings) if warnings else None


def _write_short_note(session: str, duration: float) -> Path:
    ensure_private_dir(project_paths.NOTES)
    note = project_paths.NOTES / f"{session} — Короткий запис.md"
    atomic_write_text(note, (
        f"# Короткий запис ({session})\n\n"
        f"Запис тривав {duration:.1f} с, що менше порога "
        f"{MIN_SESSION_SECONDS:.0f} с. Транскрипцію та summary пропущено, "
        "щоб не створювати галюцинації на тиші.\n"
    ))
    return note


def _write_silent_note(session: str, duration: float) -> Path:
    ensure_private_dir(project_paths.NOTES)
    note = project_paths.NOTES / f"{session} — Аудіосигнал відсутній.md"
    existing = note_for(session)
    if existing is not None and existing != note:
        existing.replace(note)
    atomic_write_text(note, (
        f"# Аудіосигнал відсутній ({session})\n\n"
        f"Запис тривав {duration:.1f} с, але обидві доріжки не мають "
        "аудіосигналу. ASR, діаризацію та summary пропущено, "
        "щоб не створювати галюцинації на тиші.\n"
    ))
    transcript = project_paths.TRANSCRIPTS / f"{session}.md"
    if transcript.exists():
        atomic_write_text(
            transcript,
            f"# Транскрипт {session}\n\n"
            "— Аудіосигнал відсутній; транскрипцію пропущено.\n",
        )
    return note


def _cleanup_processing_audio(session: str) -> None:
    audio_dir = project_paths.TRANSCRIPTS / session / "audio"
    if not audio_dir.exists():
        return
    for path in audio_dir.iterdir():
        if path.is_file():
            path.unlink(missing_ok=True)
    try:
        audio_dir.rmdir()
    except OSError:
        pass


def _session_date_time(session: str) -> tuple[str, str]:
    match = re.match(
        r"^(\d{4}-\d{2}-\d{2})_(\d{2})-?(\d{2})(?:-?(\d{2}))?",
        session,
    )
    if not match:
        return "—", "—"
    date, hour, minute, second = match.groups()
    time_value = f"{hour}:{minute}"
    if second is not None:
        time_value += f":{second}"
    return date, time_value


def _meeting_note_metadata(
    session: str,
    transcript: str,
    generated_title: str,
) -> tuple[str, str, str, list[str]]:
    """Витягує factual metadata; не просить LLM вигадувати учасників."""
    meeting_date, meeting_time = _session_date_time(session)
    started_match = re.search(
        r"(?m)^-\s+\*\*Час початку:\*\*\s*(.+?)\s*$",
        transcript,
    )
    if started_match:
        started = re.search(
            r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)"
            r"(?:\s+([A-Za-z][A-Za-z0-9_+:/-]*))?",
            started_match.group(1),
        )
        if started:
            meeting_date = started.group(1)
            meeting_time = started.group(2)
            if started.group(3):
                meeting_time += f" {started.group(3)}"

    title_match = re.search(
        r"(?m)^-\s+\*\*Назва зустрічі:\*\*\s*(.+?)\s*$",
        transcript,
    )
    meeting_title = title_match.group(1).strip() if title_match else ""
    if not meeting_title:
        heading = re.search(r"(?m)^#\s+(.+?)\s*$", transcript)
        if heading and not heading.group(1).lower().startswith("транскрипт"):
            meeting_title = heading.group(1).strip()
    meeting_title = meeting_title or generated_title or "Зустріч"

    participants: list[str] = []
    lines = transcript.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != "**Учасники:**":
            continue
        for participant_line in lines[index + 1:]:
            if not participant_line.strip() or participant_line.startswith("## "):
                break
            match = re.match(r"^-\s+(.+?)\s*$", participant_line)
            if match:
                participants.append(match.group(1).strip())
        break

    if not participants:
        for line in lines:
            match = re.match(r"^\[[^]]+\]\s+(.+?):\s", line)
            if not match:
                continue
            participant = re.sub(
                r"\s*&\s*\d+\s+others\s*$", "", match.group(1),
                flags=re.IGNORECASE,
            )
            participant = re.sub(r"\s+\(chat\)\s*$", "", participant).strip()
            if participant:
                participants.append(participant)

    unique_participants = []
    seen = set()
    for participant in participants:
        key = participant.casefold()
        if key not in seen:
            seen.add(key)
            unique_participants.append(participant)
    return meeting_date, meeting_time, meeting_title, unique_participants


def _candidate_interview_classification(
    session: str,
    transcript: str,
    meeting_title: str,
) -> dict[str, Any]:
    """Classify candidate-interview completeness before running a scorecard."""
    from meeting_classifier import CLASSIFIER_VERSION, classify_candidate_interview

    work_dir = project_paths.TRANSCRIPTS / session
    ensure_private_dir(work_dir)
    cache_path = work_dir / "candidate-interview-classification.json"
    meta = {
        "schema_version": CLASSIFIER_VERSION,
        "transcript_sha256": _sha256_text(transcript),
        "title": meeting_title,
    }
    cached = read_json(cache_path, {}) or {}
    if cached.get("_meta") == meta and isinstance(cached.get("classification"), dict):
        classification = cached["classification"]
        log(
            "  Candidate preflight: "
            f"{classification.get('outcome_label', '—')} — кеш"
        )
        return classification

    classification = classify_candidate_interview(meeting_title, transcript)
    atomic_write_json(
        cache_path,
        {"_meta": meta, "classification": classification},
        mode=0o600,
    )
    transcript_manifest = work_dir / "manifest.json"
    if transcript_manifest.exists():
        update_manifest(
            transcript_manifest,
            candidate_interview_classification=classification,
        )
    processing_manifest = manifest_path(session)
    if processing_manifest.exists():
        update_manifest(
            processing_manifest,
            candidate_interview_classification=classification,
        )
    log(
        f"  Candidate preflight: {classification['outcome_label']}"
    )
    return classification


def create_note_from_transcript(session: str, transcript: str) -> Path:
    """Створює локальні summary/title/note для вже готового транскрипту."""
    meeting_date, _, meeting_title, participants = _meeting_note_metadata(
        session, transcript, ""
    )
    if CANDIDATE_EVALUATION_ENABLED:
        from candidate_evaluation import explicit_candidate_name, is_candidate_meeting

        routing_title = meeting_title if meeting_title != "Зустріч" else session
        if explicit_candidate_name(routing_title):
            return create_candidate_evaluation_from_transcript(
                session,
                transcript,
                meeting_date=meeting_date,
                meeting_title=routing_title,
                participants=participants,
            )
        if is_candidate_meeting(routing_title):
            log(
                "  Candidate routing пропущено: потрібна назва "
                "`Interview | Candidate Name`; створюю звичайну нотатку"
            )

    update_manifest(manifest_path(session), status="processing", stage="summarizing")
    log("  Ollama summary...")
    summary = summary_pipeline.summarize(session, transcript)
    update_manifest(manifest_path(session), status="processing", stage="title")
    title = summary_pipeline.make_title(session, summary)

    ensure_private_dir(project_paths.NOTES)
    note = project_paths.NOTES / (f"{session} — {title}.md" if title else f"{session}.md")
    meeting_date, meeting_time, meeting_title, participants = (
        _meeting_note_metadata(session, transcript, title)
    )
    header = [
        f"# {title or meeting_title}",
        "",
        f"- **Дата:** {meeting_date}",
        f"- **Час:** {meeting_time}",
        f"- **Назва зустрічі:** {meeting_title}",
        "",
        "**Присутні:**",
        *(f"- {participant}" for participant in participants),
    ]
    if not participants:
        header.append("- —")
    header.append("")
    warning = _quality_warning(session)
    if warning:
        header += [f"> ⚠️ Автоматична перевірка якості: {warning}.", ""]
    speakers = sorted(set(re.findall(
        r"(?:SPEAKER|LOCAL)_\d+|LOCAL_UNKNOWN|UNKNOWN", transcript
    )))
    has_me = bool(re.search(r"^\[[^]]+\] Я:", transcript, flags=re.MULTILINE))
    if speakers or has_me:
        header += ["## Мапінг спікерів", "", "| Спікер | Ім'я |",
                   "|---|---|"]
        if has_me:
            header += ["| Я | |"]
        header += [f"| {speaker} | |" for speaker in speakers]
        header += [""]
    atomic_write_text(
        note,
        "\n".join(header) + f"\n{summary}\n\n---\n\n"
        f"## Повний транскрипт\n\n{transcript}\n",
    )
    # Notion є зовнішнім необов'язковим sink: його помилка не повинна
    # скасовувати готову локальну нотатку.
    from notion_agent import sync_note_if_enabled
    evidence = read_json(
        project_paths.TRANSCRIPTS / session / "summary-evidence.json", {}
    ) or {}
    sync_note_if_enabled(note, ledger=evidence, logger=log)
    return note


def _candidate_generation_options(system: str) -> tuple[int, bool]:
    if "витягуєш докази" in system:
        return 1200, False
    if "стискаєш доказову базу" in system:
        return 1800, False
    if "reasoning-калібрування" in system:
        return (
            6144 if CANDIDATE_OLLAMA_THINK else 3000,
            CANDIDATE_OLLAMA_THINK,
        )
    if "форматуєш фінальний hiring report" in system:
        return 6144, False
    return (
        6144 if CANDIDATE_OLLAMA_THINK else 4096,
        CANDIDATE_OLLAMA_THINK,
    )


def create_candidate_evaluation_from_transcript(
    session: str,
    transcript: str,
    *,
    meeting_date: str,
    meeting_title: str,
    participants: list[str],
    classification: dict[str, Any] | None = None,
) -> Path:
    """Run the locally installed candidate skill and create one feedback task."""
    import candidate_evaluation
    from notion_agent import sync_evaluation_feedback_if_enabled

    candidate = candidate_evaluation.candidate_name(meeting_title, participants)
    classification = classification or _candidate_interview_classification(
        session, transcript, meeting_title
    )
    interviewers = [
        participant for participant in participants
        if participant.casefold() != candidate.casefold()
    ]
    update_manifest(
        manifest_path(session), status="processing", stage="candidate-evaluation"
    )
    log(f"  Candidate evaluation: {candidate or 'імʼя не визначено'}")

    if not classification.get("candidate_evaluation_eligible", False):
        report = candidate_evaluation.create_non_evaluation_report(
            candidate=candidate,
            meeting_date=meeting_date,
            meeting_title=meeting_title,
            interview_stage=candidate_evaluation.interview_stage_from_title(
                meeting_title
            ),
            classification=classification,
        )
        path = candidate_evaluation.save_report(
            report,
            candidate=candidate,
            meeting_date=meeting_date,
            evaluation_id=session,
            replace_existing=True,
        )
        log(
            "  Candidate: повну оцінку пропущено — "
            f"{classification.get('outcome_label', 'недостатньо даних')}; "
            "задачу на hiring feedback не створено"
        )
        return path

    report = candidate_evaluation.evaluate(
        transcript,
        candidate=candidate,
        target_level=(
            candidate_evaluation.target_level_from_title(meeting_title)
            or CANDIDATE_TARGET_LEVEL
        ),
        levels=candidate_evaluation.configured_levels(),
        meeting_title=meeting_title,
        meeting_date=meeting_date,
        interviewers=interviewers,
        generate=lambda prompt, system: summary_pipeline.ollama_generate(
            prompt, system=system,
            num_predict=_candidate_generation_options(system)[0],
            think=_candidate_generation_options(system)[1],
        ),
        cache_path=project_paths.TRANSCRIPTS / session / "candidate-evaluation-cache.json",
        cache_profile={
            "model": OLLAMA_MODEL,
            "num_ctx": OLLAMA_NUM_CTX,
            "evidence_think": False,
            # Keep the cache profile stable across the two-stage finalization:
            # `final_think` now applies to the calibration brief, while report
            # formatting is deliberately non-reasoning.
            "final_think": CANDIDATE_OLLAMA_THINK,
        },
        progress=lambda message: log(f"  Candidate: {message}"),
    )
    path = candidate_evaluation.save_report(
        report,
        candidate=candidate,
        meeting_date=meeting_date,
        evaluation_id=session,
        replace_existing=True,
    )
    sync_evaluation_feedback_if_enabled(
        path,
        candidate=candidate,
        meeting_title=meeting_title,
        meeting_date=meeting_date,
        evaluation_id=session,
        logger=log,
    )
    return path


def evaluate_candidate_session(session: str) -> Path:
    """Manually evaluate an existing transcript, even if a normal note exists."""
    transcript_path = project_paths.TRANSCRIPTS / f"{session}.md"
    if not transcript_path.is_file():
        raise FileNotFoundError(f"Немає транскрипту для {session}")
    transcript = transcript_path.read_text(encoding="utf-8")
    meeting_date, _, meeting_title, participants = _meeting_note_metadata(
        session, transcript, ""
    )
    from candidate_evaluation import is_candidate_meeting

    if not is_candidate_meeting(meeting_title):
        raise ValueError(
            f"Назва {meeting_title!r} не містить candidate keyword"
        )
    with session_lock(session):
        report = create_candidate_evaluation_from_transcript(
            session,
            transcript,
            meeting_date=meeting_date,
            meeting_title=meeting_title,
            participants=participants,
        )
    update_manifest(
        manifest_path(session),
        status="complete",
        stage="candidate-evaluation-complete",
        candidate_evaluation=str(report),
        completed_at=utc_now(),
        next_retry_at=None,
        last_error=None,
    )
    return report


def _process_session(session: str) -> None:
    log(f"Обробляю {session}")
    manifest = _ensure_manifest(session)
    attempts = int(manifest.get("processing_attempts", 0) or 0) + 1
    update_manifest(
        manifest_path(session), status="processing", stage="starting",
        processing_attempts=attempts, processing_started_at=utc_now(),
    )
    mic = project_paths.RECORDINGS / f"{session}_mic.wav"
    sys_wav = project_paths.RECORDINGS / f"{session}_sys.wav"
    duration = max(audio_info(mic)["duration"], audio_info(sys_wav)["duration"])
    if duration < MIN_SESSION_SECONDS:
        note = _write_short_note(session, duration)
        update_manifest(manifest_path(session), status="complete", stage="short-recording",
                        note=str(note), completed_at=utc_now(), next_retry_at=None)
        log(f"  Короткий запис → {note}")
        return


    mic_signal = audio_signal_info(mic)
    sys_signal = audio_signal_info(sys_wav)
    if max(mic_signal["peak_dbfs"], sys_signal["peak_dbfs"]) < SILENT_RECORDING_PEAK_DBFS:
        note = _write_silent_note(session, duration)
        update_manifest(
            manifest_path(session),
            status="complete",
            stage="silent-recording",
            note=str(note),
            completed_at=utc_now(),
            signal={"mic": mic_signal, "sys": sys_signal},
            next_retry_at=None,
        )
        log(f"  Цифрова тиша → {note}")
        return

    update_manifest(manifest_path(session), status="processing", stage="transcribing",
                    processing_started_at=utc_now())
    subprocess.run(
        [sys.executable, str(BASE / "transcribe.py"), str(project_paths.RECORDINGS / session)],
        check=True,
    )
    transcript_path = project_paths.TRANSCRIPTS / f"{session}.md"
    transcript = transcript_path.read_text(encoding="utf-8")
    note = create_note_from_transcript(session, transcript)
    update_manifest(manifest_path(session), status="complete", stage="complete",
                    note=str(note), completed_at=utc_now(), processing_attempts=0,
                    next_retry_at=None, last_error=None)
    _cleanup_processing_audio(session)
    log(f"  Готово: {note}")


def process_session(session: str) -> None:
    if not AUDIO_PIPELINE_ENABLED:
        raise RuntimeError(
            "Модуль audio вимкнено. Увімкніть: "
            ".venv/bin/python3 modules.py enable audio"
        )
    with session_lock(session):
        _process_session(session)


def refresh_note_transcript(session: str) -> Path:
    """Оновлює speaker labels і повний транскрипт без повторного Ollama summary."""
    note = note_for(session)
    transcript_path = project_paths.TRANSCRIPTS / f"{session}.md"
    if note is None or not transcript_path.exists():
        raise FileNotFoundError(f"Немає note/transcript для {session}")

    text = note.read_text(encoding="utf-8")
    marker = "\n---\n\n## Повний транскрипт\n\n"
    summary, separator, _ = text.partition(marker)
    if not separator:
        raise ValueError(f"У note {session} немає секції повного транскрипту")

    transcript = transcript_path.read_text(encoding="utf-8")
    transcript_manifest = read_json(project_paths.TRANSCRIPTS / session / "manifest.json", {}) or {}
    collapse = ((transcript_manifest.get("quality") or {})
                .get("speaker_collapse") or {})
    if collapse.get("collapsed"):
        old_labels = set(collapse.get("merged_labels") or [])
        dominant = collapse.get("dominant_label")
        if dominant:
            old_labels.add(str(dominant))
        for label in old_labels:
            summary = summary.replace(label, "SPEAKER_00")

    speakers = sorted(set(re.findall(
        r"(?:SPEAKER|LOCAL)_\d+|LOCAL_UNKNOWN|UNKNOWN", transcript
    )))
    mapping = ["## Мапінг спікерів", "", "| Спікер | Ім'я |",
               "|---|---|"]
    if re.search(r"^\[[^]]+\] Я:", transcript, flags=re.MULTILINE):
        mapping += ["| Я | |"]
    mapping += [f"| {speaker} | |" for speaker in speakers]
    replacement = "\n".join(mapping) + "\n"
    summary, changed = re.subn(
        r"## Мапінг спікерів\n\n(?:\|.*\n)+",
        replacement,
        summary,
        count=1,
    )
    if changed == 0:
        title, separator, body = summary.partition("\n\n")
        if not separator:
            raise ValueError(f"Не вдалося додати mapping у note {session}")
        summary = title + "\n\n" + replacement + "\n" + body

    atomic_write_text(note, summary.rstrip() + marker + transcript)
    return note


def refresh_summary_render(session: str) -> Path:
    """Re-run deterministic summary QA/rendering without another model call."""
    _require_safe_session_id(session)
    note = note_for(session)
    transcript_path = project_paths.TRANSCRIPTS / f"{session}.md"
    cache_path = project_paths.TRANSCRIPTS / session / "summary-cache.json"
    evidence_path = project_paths.TRANSCRIPTS / session / "summary-evidence.json"
    if note is None or not transcript_path.is_file() or not cache_path.is_file():
        raise FileNotFoundError(f"Немає note/transcript/summary cache для {session}")
    transcript = transcript_path.read_text(encoding="utf-8")
    cache = read_json(cache_path, {}) or {}
    raw_ledger = cache.get("ledger")
    if not isinstance(raw_ledger, dict):
        raw_ledger = read_json(evidence_path, {}) or {}
    ledger, dropped = _validated_evidence_ledger(raw_ledger, transcript)
    items = _deduplicate_evidence_items(ledger.get("items", []))
    items = _normalize_evidence_lifecycle(items)
    items = _ground_claim_language(items)
    items = _deduplicate_evidence_items(items)
    ledger = {"items": items}
    quality = _summary_quality_report(ledger)
    if dropped:
        quality["warnings"].append({
            "code": "dropped_during_refresh",
            "claim": str(dropped),
        })
    if quality["status"] != "pass":
        raise ValueError("Summary quality gate не пройдено під час refresh")
    _, _, meeting_title, _ = _meeting_note_metadata(session, transcript, "")
    summary_title = str(cache.get("title", "")).strip() or meeting_title
    summary = _render_grounded_sections(
        SUMMARY_TEMPLATE, ledger, meeting_title=summary_title
    )
    if not _valid_summary(summary):
        raise ValueError("Оновлений summary не пройшов structural validation")

    meta = {
        "schema_version": 8,
        "prompt_fingerprint": PROMPT_FINGERPRINT,
        "model": OLLAMA_MODEL,
        "num_ctx": OLLAMA_NUM_CTX,
        "extract_think": SUMMARY_EXTRACT_THINK,
        "reconcile_think": SUMMARY_RECONCILE_THINK,
        "transcript_sha256": _sha256_text(transcript),
    }
    cache["_meta"] = meta
    cache["ledger"] = ledger
    cache["quality"] = quality
    cache["summary"] = summary
    atomic_write_json(cache_path, cache, mode=0o600)
    atomic_write_json(
        evidence_path,
        {"_meta": meta, "_quality": quality, **ledger},
        mode=0o600,
    )

    note_text = note.read_text(encoding="utf-8")
    refreshed, count = re.subn(
        r"(?ms)^## TL;DR\n.*?(?=^---\s*$)",
        summary.rstrip() + "\n\n",
        note_text,
        count=1,
    )
    if count != 1:
        raise ValueError(f"У note {session} не знайдено summary-блок")
    atomic_write_text(note, refreshed)
    return note


def _redacted_traceback() -> str:
    return re.sub(
        r"\b(?:hf_|ntn_)[A-Za-z0-9_-]+",
        "<redacted>",
        traceback.format_exc(),
    )


def handle_failure(session: str) -> None:
    _require_safe_session_id(session)
    path = manifest_path(session)
    manifest = read_json(path, {}) or {"schema_version": 1, "session": session}
    attempts = max(1, int(manifest.get("processing_attempts", 0) or 0))
    terminal = attempts >= MAX_AUTO_RETRIES
    retry_at = None if terminal else time.time() + min(3600, 60 * 2 ** (attempts - 1))
    trace = _redacted_traceback()
    ensure_private_dir(project_paths.FAILED)
    error_file = project_paths.FAILED / f"{session}.log"
    atomic_write_text(error_file, trace, mode=0o600)
    update_manifest(
        path,
        schema_version=manifest.get("schema_version", 1),
        session=session,
        status="terminal_failed" if terminal else "processing_failed",
        stage="failed",
        processing_attempts=attempts,
        next_retry_at=retry_at,
        last_error=trace.splitlines()[-1] if trace.splitlines() else "unknown",
    )
    if terminal:
        log(f"  ПОМИЛКА: {session}; вичерпано {attempts} спроб → {error_file}")
    else:
        wait = int(retry_at - time.time())
        log(f"  ПОМИЛКА: {session}; retry #{attempts + 1} через ~{wait} с")


def rotate_old_wavs() -> None:
    if ROTATE_DAYS <= 0:
        return
    cutoff = time.time() - ROTATE_DAYS * 86400
    for wav in project_paths.RECORDINGS.glob("*.wav"):
        session = wav.name.removesuffix("_mic.wav").removesuffix("_sys.wav")
        if not _is_safe_session_id(session):
            continue
        session_manifest = manifest_path(session)
        manifest = read_json(session_manifest, {}) or {}
        complete = manifest.get("status") == "complete"
        legacy_complete = (
            not session_manifest.exists()
            and (project_paths.TRANSCRIPTS / f"{session}.md").exists()
            and note_for(session) is not None
        )
        if (wav.stat().st_mtime < cutoff and note_for(session)
                and (complete or legacy_complete)):
            wav.unlink()
            log(f"Ротація: видалено {wav.name}")

    # Final notes and merged transcripts are durable; bulky intermediate model
    # caches and completed manifests are disposable after the same retention age.
    for session_manifest in project_paths.RECORDINGS.glob("*.json"):
        manifest = read_json(session_manifest, {}) or {}
        if manifest.get("status") != "complete":
            continue
        session = session_manifest.stem
        if not _is_safe_session_id(session):
            continue
        output_exists = note_for(session) is not None
        candidate_output = manifest.get("candidate_evaluation")
        if candidate_output:
            output_exists = output_exists or Path(str(candidate_output)).is_file()
        try:
            old_enough = session_manifest.stat().st_mtime < cutoff
        except FileNotFoundError:
            continue
        if not output_exists or not old_enough:
            continue
        transcript_root = project_paths.TRANSCRIPTS.resolve()
        cache_dir = project_paths.TRANSCRIPTS / session
        resolved_cache = cache_dir.resolve()
        if (
            resolved_cache.parent == transcript_root
            and not cache_dir.is_symlink()
            and cache_dir.is_dir()
        ):
            shutil.rmtree(resolved_cache)
            log(f"Ротація: видалено cache {cache_dir.name}/")
        if not any(project_paths.RECORDINGS.glob(f"{session}_*.wav")):
            session_manifest.unlink(missing_ok=True)

    failed_cutoff = time.time() - max(30, ROTATE_DAYS) * 86400
    for error_file in project_paths.FAILED.glob("*.log"):
        try:
            if error_file.stat().st_mtime < failed_cutoff:
                error_file.unlink()
                log(f"Ротація: видалено старий error log {error_file.name}")
        except FileNotFoundError:
            continue
