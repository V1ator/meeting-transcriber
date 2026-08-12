#!/usr/bin/env python3
"""Coordinate Meet import, audio processing, retries, and local sinks."""

from __future__ import annotations

import argparse
import time

import audio_pipeline
import meet_pipeline
import paths as project_paths
import summary_pipeline
from pipeline_utils import ensure_private_dir, update_manifest

POLL_SECONDS = 30
NOTION_RETRY_SECONDS = 300

SessionBusy = audio_pipeline.SessionBusy
log = audio_pipeline.log
manifest_path = audio_pipeline.manifest_path
find_ready_sessions = audio_pipeline.find_ready_sessions
process_session = audio_pipeline.process_session
handle_failure = audio_pipeline.handle_failure
rotate_old_wavs = audio_pipeline.rotate_old_wavs
refresh_note_transcript = audio_pipeline.refresh_note_transcript
refresh_summary_render = audio_pipeline.refresh_summary_render
evaluate_candidate_session = audio_pipeline.evaluate_candidate_session
_is_safe_session_id = audio_pipeline._is_safe_session_id

process_meet_exports = meet_pipeline.process_meet_exports
process_failed_meet_sessions = meet_pipeline.process_failed_meet_sessions
retry_meet_session = meet_pipeline.retry_meet_session

OLLAMA_MODEL = summary_pipeline.OLLAMA_MODEL
MEET_AUTO_IMPORT = meet_pipeline.MEET_AUTO_IMPORT
MEET_AUTO_SUMMARY = meet_pipeline.MEET_AUTO_SUMMARY
MEET_DOWNLOADS_DIR = meet_pipeline.MEET_DOWNLOADS_DIR


def _validate_session(value: str) -> str:
    if not _is_safe_session_id(value):
        raise argparse.ArgumentTypeError("Некоректний session ID")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--retry", type=_validate_session, metavar="SESSION")
    parser.add_argument("--refresh-note", type=_validate_session, metavar="SESSION")
    parser.add_argument(
        "--refresh-summary-render", type=_validate_session, metavar="SESSION"
    )
    parser.add_argument("--retry-meet", type=_validate_session, metavar="SESSION")
    parser.add_argument(
        "--evaluate-candidate", type=_validate_session, metavar="SESSION"
    )
    args = parser.parse_args()
    for directory in (project_paths.RECORDINGS, project_paths.TRANSCRIPTS, project_paths.NOTES, project_paths.FAILED):
        ensure_private_dir(directory)

    if args.refresh_summary_render:
        note = refresh_summary_render(args.refresh_summary_render)
        log(f"Оновлено deterministic summary у note: {note}")
        return

    if args.refresh_note:
        note = refresh_note_transcript(args.refresh_note)
        log(f"Оновлено транскрипт у note: {note}")
        return

    if args.evaluate_candidate:
        report = evaluate_candidate_session(args.evaluate_candidate)
        log(f"Candidate evaluation готова: {report}")
        return

    if args.retry_meet:
        note = retry_meet_session(args.retry_meet)
        log(f"Meet note готова: {note}")
        return

    if args.retry:
        update_manifest(manifest_path(args.retry), status="recorded",
                        processing_attempts=0, next_retry_at=None, last_error=None)
        try:
            process_session(args.retry)
        except SessionBusy:
            raise
        except Exception:
            handle_failure(args.retry)
            raise
        return

    log(f"Watch-folder: {project_paths.RECORDINGS} (модель: {OLLAMA_MODEL})")
    if MEET_AUTO_IMPORT:
        log(
            f"Meet auto-import: {MEET_DOWNLOADS_DIR} "
            f"(summary: {'так' if MEET_AUTO_SUMMARY else 'ні'})"
        )
    next_notion_retry = 0.0
    while True:
        try:
            process_meet_exports()
        except Exception as exc:
            log(f"Meet auto-import: неочікувана помилка циклу ({exc!r})")
        try:
            process_failed_meet_sessions()
        except Exception as exc:
            log(f"Meet retry: неочікувана помилка циклу ({exc!r})")
        for session in find_ready_sessions():
            try:
                process_session(session)
            except SessionBusy as exc:
                log(f"  {exc}")
            except Exception:
                handle_failure(session)
        now = time.time()
        if now >= next_notion_retry:
            try:
                from notion_agent import retry_deferred_if_enabled

                retry_deferred_if_enabled(logger=log, now=now)
            except Exception as exc:
                log(f"  Notion retry: неочікувана помилка ({exc!r})")
            next_notion_retry = now + NOTION_RETRY_SECONDS
        rotate_old_wavs()
        if args.once:
            break
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
