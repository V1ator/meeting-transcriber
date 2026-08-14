#!/usr/bin/env python3
"""Google Meet export discovery, import, retry, and quarantine."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

import audio_pipeline as audio_pipeline
import paths as project_paths
from pipeline_utils import (
    atomic_write_json,
    ensure_private_dir,
    load_dotenv,
    read_json,
    update_manifest,
    utc_now,
)

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

MEET_AUTO_IMPORT = os.environ.get("MEET_AUTO_IMPORT", "true").lower() == "true"
MEET_AUTO_SUMMARY = os.environ.get("MEET_AUTO_SUMMARY", "true").lower() == "true"
MEET_IMPORT_EXISTING = (
    os.environ.get("MEET_IMPORT_EXISTING", "false").lower() == "true"
)
MEET_DOWNLOADS_DIR = Path(
    os.environ.get("MEET_DOWNLOADS_DIR", str(Path.home() / "Downloads"))
).expanduser()
MEET_IMPORT_STABLE_SECONDS = max(
    1.0, float(os.environ.get("MEET_IMPORT_STABLE_SECONDS", "5"))
)
MAX_AUTO_RETRIES = int(os.environ.get("MAX_AUTO_RETRIES", "8"))

SessionBusy = audio_pipeline.SessionBusy
session_lock = audio_pipeline.session_lock
_is_safe_session_id = audio_pipeline._is_safe_session_id
_require_safe_session_id = audio_pipeline._require_safe_session_id
note_for = audio_pipeline.note_for
manifest_path = audio_pipeline.manifest_path
create_note_from_transcript = audio_pipeline.create_note_from_transcript
create_meet_capture_failure_note = (
    audio_pipeline.create_meet_capture_failure_note
)
handle_failure = audio_pipeline.handle_failure
log = audio_pipeline.log

_meet_export_errors: dict[Path, tuple[int, int]] = {}


def find_ready_meet_exports(*, now: float | None = None) -> list[Path]:
    """Повертає завершені Chrome downloads, готові до безпечного імпорту."""
    if not MEET_AUTO_IMPORT or not MEET_DOWNLOADS_DIR.is_dir():
        return []
    current = time.time() if now is None else now
    ready: list[tuple[float, str, Path]] = []
    for path in MEET_DOWNLOADS_DIR.glob("meet-*.json"):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        if (
            path.is_file()
            and stat.st_size > 0
            and current - stat.st_mtime >= MEET_IMPORT_STABLE_SECONDS
        ):
            ready.append((stat.st_mtime, path.name, path))
    return [item[2] for item in sorted(ready)]


def _meet_import_cutoff(*, now: float) -> float:
    state_path = project_paths.TRANSCRIPTS / ".meet-auto-import-state.json"
    state = read_json(state_path, {}) or {}
    try:
        return float(state["watching_since_epoch"])
    except (KeyError, TypeError, ValueError):
        pass

    cutoff = 0.0 if MEET_IMPORT_EXISTING else now
    ensure_private_dir(project_paths.TRANSCRIPTS)
    atomic_write_json(state_path, {
        "schema_version": 1,
        "watching_since_epoch": cutoff,
        "initialized_at": utc_now(),
        "included_existing_files": MEET_IMPORT_EXISTING,
    }, mode=0o600)
    if not MEET_IMPORT_EXISTING:
        log("Meet auto-import: наявні старі JSON пропущено; стежу за новими")
    return cutoff


def _meet_summary_due(session: str, *, now: float) -> bool:
    if not _is_safe_session_id(session):
        return False
    if note_for(session) is not None:
        return False
    manifest = read_json(manifest_path(session), {}) or {}
    status = manifest.get("status")
    attempts = int(manifest.get("processing_attempts", 0) or 0)
    retry_at = float(manifest.get("next_retry_at", 0) or 0)
    if status in {"complete", "terminal_failed"}:
        return False
    if status == "processing_failed":
        return attempts < MAX_AUTO_RETRIES and now >= retry_at
    return True


def find_ready_meet_sessions(*, now: float | None = None) -> list[str]:
    """Find imported Meet transcripts whose note generation needs a retry."""
    if not MEET_AUTO_SUMMARY:
        return []
    current = time.time() if now is None else now
    ready: list[str] = []
    for path in sorted(project_paths.RECORDINGS.glob("*.json")):
        manifest = read_json(path, {}) or {}
        if manifest.get("source") != "google-meet-live-captions":
            continue
        session = str(manifest.get("session") or path.stem)
        if not _is_safe_session_id(session):
            continue
        if note_for(session) is not None:
            continue
        if not (project_paths.TRANSCRIPTS / f"{session}.md").is_file():
            continue
        status = manifest.get("status")
        attempts = int(manifest.get("processing_attempts", 0) or 0)
        retry_at = float(manifest.get("next_retry_at", 0) or 0)
        if status in {"complete", "terminal_failed"}:
            continue
        if attempts >= MAX_AUTO_RETRIES:
            update_manifest(
                path,
                status="terminal_failed",
                stage="interrupted",
                next_retry_at=None,
                last_error="Обробку перервано під час останньої спроби",
            )
            continue
        if status == "processing" or (
            status == "processing_failed" and current >= retry_at
        ):
            ready.append(session)
    return ready


def retry_meet_session(session: str) -> Path:
    """Create a note from the durable local Meet transcript without a new export."""
    _require_safe_session_id(session)
    transcript_path = project_paths.TRANSCRIPTS / f"{session}.md"
    if not transcript_path.is_file():
        raise FileNotFoundError(f"Немає Meet-транскрипту для {session}")
    manifest = read_json(manifest_path(session), {}) or {}
    if manifest.get("source") != "google-meet-live-captions":
        raise ValueError(f"Сесія {session} не є Meet auto-import")

    with session_lock(session):
        attempts = int(manifest.get("processing_attempts", 0) or 0) + 1
        update_manifest(
            manifest_path(session),
            status="processing",
            stage="summarizing",
            processing_attempts=attempts,
            processing_started_at=utc_now(),
            next_retry_at=None,
            last_error=None,
        )
        try:
            note = create_note_from_transcript(
                session, transcript_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            from candidate_evaluation import CandidateEvaluationTerminalError

            handle_failure(
                session,
                terminal_override=isinstance(
                    exc, CandidateEvaluationTerminalError
                ),
            )
            raise
        update_manifest(
            manifest_path(session),
            status="complete",
            stage="complete",
            note=str(note),
            completed_at=utc_now(),
            next_retry_at=None,
            last_error=None,
        )
        (project_paths.FAILED / f"{session}.log").unlink(missing_ok=True)
    log(f"Meet retry: {session} → {note}")
    return note


def process_failed_meet_sessions(*, now: float | None = None) -> int:
    """Retry failed Meet notes independently of files left in Downloads."""
    processed = 0
    for session in find_ready_meet_sessions(now=now):
        try:
            retry_meet_session(session)
            processed += 1
        except SessionBusy as exc:
            log(f"  {exc}")
        except Exception as exc:
            log(f"Meet retry: {session} не завершено ({exc})")
    return processed


def _remove_imported_meet_export(source: Path) -> None:
    """Видаляє лише опрацьований Meet JSON; локальна копія вже є в transcripts."""
    try:
        source.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        log(f"Meet auto-import: не вдалося видалити {source.name} ({exc})")
        return
    log(f"Meet auto-import: видалено {source.name} із Downloads")


def _quarantine_meet_export(source: Path, reason: str) -> None:
    """Preserve an unprocessable Meet export without parsing it forever."""
    ensure_private_dir(project_paths.FAILED)
    target = project_paths.FAILED / source.name
    if target.exists():
        target = project_paths.FAILED / f"{source.stem}-{int(time.time())}{source.suffix}"
    try:
        shutil.move(str(source), str(target))
        log(f"Meet auto-import: {reason}; збережено → {target}")
    except FileNotFoundError:
        return
    except OSError as exc:
        log(f"Meet auto-import: не вдалося перемістити {source.name} ({exc})")


def _meet_export_score(data: dict[str, Any]) -> tuple[int, int]:
    """Compare export completeness without relying on file size or timestamps."""
    import meet_import

    entries = meet_import.normalized_entries(data)
    latest = max((int(item.get("end_ms", 0) or 0) for item in entries), default=0)
    return len(entries), latest


def process_meet_exports(*, now: float | None = None) -> int:
    """Імпортує нові Meet JSON та за потреби створює локальну note."""
    if not MEET_AUTO_IMPORT or not MEET_DOWNLOADS_DIR.is_dir():
        return 0

    import meet_import

    current = time.time() if now is None else now
    cutoff = _meet_import_cutoff(now=current)
    processed = 0
    for source in find_ready_meet_exports(now=current):
        session: str | None = None
        signature: tuple[int, int] | None = None
        transcript_path: Path | None = None
        refresh_transcript = False
        try:
            stat = source.stat()
            if stat.st_mtime <= cutoff:
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            data = meet_import.load_export(source)
            session = meet_import.session_id(data)
            diagnostic_only = meet_import.is_diagnostic_export(data)
            transcript_path = project_paths.TRANSCRIPTS / f"{session}.md"
            if transcript_path.exists():
                previous = read_json(
                    project_paths.TRANSCRIPTS / session / "meet-captions.json", {}
                ) or {}
                refresh_transcript = (
                    not previous or _meet_export_score(data) > _meet_export_score(previous)
                )
                if not refresh_transcript and not _meet_summary_due(session, now=current):
                    manifest = read_json(manifest_path(session), {}) or {}
                    if manifest.get("status") == "terminal_failed" and not note_for(session):
                        _quarantine_meet_export(
                            source, "сесія у terminal_failed; повторний export не повніший"
                        )
                    else:
                        log(f"Meet auto-import: {source.name} — дублікат без нових реплік")
                        _remove_imported_meet_export(source)
                    continue

            imported = False
            note: Path | None = None
            with session_lock(session):
                previous_note = note_for(session)
                if not transcript_path.exists() or refresh_transcript:
                    meet_import.import_export(
                        source, summarize=False, force=refresh_transcript
                    )
                    imported = True

                if MEET_AUTO_SUMMARY and (
                    refresh_transcript or _meet_summary_due(session, now=current)
                ):
                    ensure_private_dir(project_paths.RECORDINGS)
                    manifest = read_json(manifest_path(session), {}) or {}
                    if not manifest:
                        atomic_write_json(manifest_path(session), {
                            "schema_version": 1,
                            "session": session,
                            "status": "processing",
                            "stage": "summarizing",
                            "source": "google-meet-live-captions",
                            "source_file": str(source),
                            "created_at": utc_now(),
                            "processing_attempts": 0,
                        }, mode=0o600)
                    elif refresh_transcript:
                        update_manifest(
                            manifest_path(session), status="processing",
                            stage="summarizing", processing_attempts=0,
                            next_retry_at=None, last_error=None,
                        )
                    attempt_manifest = read_json(manifest_path(session), {}) or {}
                    update_manifest(
                        manifest_path(session), status="processing", stage="summarizing",
                        processing_attempts=(
                            int(attempt_manifest.get("processing_attempts", 0) or 0) + 1
                        ),
                        processing_started_at=utc_now(),
                    )
                    transcript = transcript_path.read_text(encoding="utf-8")
                    if diagnostic_only:
                        note = create_meet_capture_failure_note(
                            session, transcript
                        )
                    else:
                        note = create_note_from_transcript(session, transcript)
                    update_manifest(
                        manifest_path(session),
                        status="complete",
                        stage="complete",
                        capture_failed=diagnostic_only,
                        note=str(note),
                        completed_at=utc_now(),
                        next_retry_at=None,
                        last_error=None,
                    )
                    if previous_note and note != previous_note:
                        previous_note.unlink(missing_ok=True)

            if imported or note is not None:
                processed += 1
                result = note or transcript_path
                log(f"Meet auto-import: {source.name} → {result}")
                _remove_imported_meet_export(source)
            _meet_export_errors.pop(source, None)
        except SessionBusy as exc:
            log(f"  {exc}")
        except meet_import.MeetImportError as exc:
            _quarantine_meet_export(source, f"некоректний export ({exc})")
            _meet_export_errors.pop(source, None)
        except Exception as exc:
            if session and transcript_path and transcript_path.exists() and MEET_AUTO_SUMMARY:
                from candidate_evaluation import CandidateEvaluationTerminalError

                handle_failure(
                    session,
                    terminal_override=isinstance(
                        exc, CandidateEvaluationTerminalError
                    ),
                )
            elif signature is not None and _meet_export_errors.get(source) != signature:
                log(f"Meet auto-import пропущено: {source.name} ({exc})")
                _meet_export_errors[source] = signature
                while len(_meet_export_errors) > 256:
                    _meet_export_errors.pop(next(iter(_meet_export_errors)))
    return processed
