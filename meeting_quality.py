#!/usr/bin/env python3
"""Deterministic capture and summary quality reports for meeting notes."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import paths as project_paths
from pipeline_utils import atomic_write_json, read_json, update_manifest, utc_now

REPORT_SCHEMA_VERSION = 1

STATUS_LABELS = {
    "high": "Висока",
    "medium": "Середня",
    "review": "Потребує перевірки",
}

_UNKNOWN_SPEAKER = re.compile(
    r"^(?:невідомий|unknown|local_unknown|учасник(?:\s+\S+)?)$",
    flags=re.IGNORECASE,
)

_SUMMARY_ISSUE_MESSAGES = {
    "missing_owner": "У деяких action items не визначено відповідального.",
    "missing_source_anchor": "Для частини тез бракує точного місця у транскрипті.",
    "duplicate_claim": "У summary залишилися дублікати змістовно однакових тез.",
    "unaccepted_decision": "Як рішення позначено тезу без явного підтвердження.",
    "dropped_during_refresh": "Під час повторної перевірки відкинуто непідтверджені тези.",
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _timestamp(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _issue(code: str, message: str, severity: str = "warning") -> dict[str, str]:
    return {"code": code, "message": message, "severity": severity}


def _deduplicate_issues(
    issues: list[dict[str, str]],
) -> list[dict[str, str]]:
    unique: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in issues:
        key = (
            str(item.get("code", "")),
            str(item.get("message", "")),
            str(item.get("severity", "warning")),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _status(issues: list[dict[str, str]]) -> str:
    if any(item["severity"] == "error" for item in issues):
        return "review"
    if issues:
        return "medium"
    return "high"


def _unknown_speaker(value: Any) -> bool:
    return bool(_UNKNOWN_SPEAKER.match(str(value or "").strip()))


def assess_meet_capture(
    export: dict[str, Any], entries: list[dict[str, Any]]
) -> dict[str, Any]:
    """Assess a normalized Google Meet RTC captions export."""
    captions = [entry for entry in entries if entry.get("kind") != "chat"]
    unknown = sum(_unknown_speaker(entry.get("speaker")) for entry in captions)
    ratio = unknown / len(captions) if captions else 1.0
    starts = sorted(
        int(_number(entry.get("start_ms"))) for entry in captions
    )
    max_gap = max(
        (right - left for left, right in zip(starts, starts[1:])),
        default=0,
    ) / 1000
    health = export.get("captureHealth")
    health_present = isinstance(health, dict) and bool(health)
    if not isinstance(health, dict):
        health = {}
    started = _timestamp(export.get("startedAt"))
    ended = _timestamp(export.get("endedAt") or health.get("exportedAt"))
    duration = (
        max(0.0, (ended - started).total_seconds())
        if started and ended else 0.0
    )
    open_delay = _number(health.get("rtcOpenedAtMs")) / 1000
    disconnects = int(_number(health.get("disconnectCount")))
    failures = int(_number(health.get("decodeFailures")))
    unavailable = bool(health.get("hadRtcUnavailable"))
    recovered = bool(health.get("recovered"))

    issues: list[dict[str, str]] = []
    if not health_present:
        issues.append(_issue(
            "capture_health_missing",
            "Export не містить технічного RTC health-знімка.",
        ))
    if not captions:
        issues.append(_issue(
            "no_captions",
            "У файлі немає жодної репліки live captions.",
            "error",
        ))
    if unavailable and not recovered:
        issues.append(_issue(
            "rtc_unavailable",
            "RTC captions були недоступні й до завершення не відновилися.",
            "error",
        ))
    elif unavailable:
        issues.append(_issue(
            "rtc_recovered",
            "RTC captions тимчасово були недоступні, але запис відновився.",
        ))
    if disconnects:
        issues.append(_issue(
            "rtc_disconnects",
            f"RTC-канал перепідключався: {disconnects}.",
        ))
    if failures >= 3:
        issues.append(_issue(
            "decode_failures",
            f"Зафіксовано помилки декодування RTC: {failures}.",
        ))
    if open_delay > 30:
        issues.append(_issue(
            "slow_rtc_start",
            f"RTC captions підключилися через {open_delay:.0f} с після старту.",
        ))
    if ratio > 0.35:
        issues.append(_issue(
            "unknown_speakers_high",
            f"Не визначено спікера у {ratio:.0%} реплік.",
            "error",
        ))
    elif ratio > 0.15:
        issues.append(_issue(
            "unknown_speakers",
            f"Не визначено спікера у {ratio:.0%} реплік.",
        ))

    report_status = _status(issues)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "source": "google-meet-live-captions",
        "status": report_status,
        "status_label": STATUS_LABELS[report_status],
        "metrics": {
            "caption_turns": len(captions),
            "chat_messages": len(entries) - len(captions),
            "duration_seconds": round(duration, 1),
            "unknown_speaker_ratio": round(ratio, 4),
            "max_caption_gap_seconds": round(max_gap, 1),
            "rtc_open_delay_seconds": round(open_delay, 1),
            "rtc_first_caption_seconds": round(
                _number(health.get("firstCaptionMs")) / 1000, 1
            ),
            "rtc_last_caption_seconds": round(
                _number(health.get("lastCaptionMs")) / 1000, 1
            ),
            "rtc_decoded_captions": int(
                _number(health.get("decodedCaptions"))
            ),
            "rtc_disconnects": disconnects,
            "rtc_decode_failures": failures,
            "rtc_channel_state": str(health.get("channelState", "")),
        },
        "issues": issues,
    }


def assess_audio_capture(manifest: dict[str, Any]) -> dict[str, Any]:
    """Convert the existing audio diarization metrics into one report shape."""
    quality = manifest.get("quality")
    quality_present = isinstance(quality, dict) and bool(quality)
    if not isinstance(quality, dict):
        quality = {}
    unknown = _number(quality.get("unknown_speaker_ratio"))
    local_unknown = _number(quality.get("local_unknown_speaker_ratio"))
    scale = _number((quality.get("sync") or {}).get("scale"), 1.0)
    issues: list[dict[str, str]] = []
    if not quality_present:
        issues.append(_issue(
            "audio_quality_missing",
            "Маніфест не містить метрик якості аудіо й діаризації.",
        ))
    if unknown > 0.35:
        issues.append(_issue(
            "unknown_speakers_high",
            f"Не визначено спікера у {unknown:.0%} реплік співрозмовників.",
            "error",
        ))
    elif unknown > 0.15:
        issues.append(_issue(
            "unknown_speakers",
            f"Не визначено спікера у {unknown:.0%} реплік співрозмовників.",
        ))
    if local_unknown > 0.35:
        issues.append(_issue(
            "local_speaker_unknown_high",
            f"Не визначено локального спікера у {local_unknown:.0%} реплік.",
            "error",
        ))
    elif local_unknown > 0.15:
        issues.append(_issue(
            "local_speaker_unknown",
            f"Не визначено локального спікера у {local_unknown:.0%} реплік.",
        ))
    if abs(scale - 1.0) > 0.005:
        issues.append(_issue(
            "clock_drift",
            f"Застосовано помітну корекцію синхронізації: ×{scale:.6f}.",
        ))
    report_status = _status(issues)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "source": "local-audio",
        "status": report_status,
        "status_label": STATUS_LABELS[report_status],
        "metrics": {
            "unknown_speaker_ratio": round(unknown, 4),
            "local_unknown_speaker_ratio": round(local_unknown, 4),
            "sync_scale": round(scale, 8),
        },
        "issues": issues,
    }


def finalize_quality_report(session: str) -> dict[str, Any]:
    """Combine capture and summary QA, persist it, and return it."""
    work_dir = project_paths.TRANSCRIPTS / session
    manifest = read_json(work_dir / "manifest.json", {}) or {}
    capture = manifest.get("capture_quality")
    if not isinstance(capture, dict):
        capture = assess_audio_capture(manifest)
    evidence = read_json(work_dir / "summary-evidence.json", {}) or {}
    summary_quality = evidence.get("_quality")
    if not isinstance(summary_quality, dict):
        summary_quality = {}

    issues = list(capture.get("issues") or [])
    if not summary_quality:
        issues.append(_issue(
            "summary_quality_missing",
            "Не знайдено результат детермінованої перевірки summary.",
        ))
    for item in summary_quality.get("warnings") or []:
        code = str(item.get("code", "summary_warning"))
        issues.append(_issue(
            code,
            _SUMMARY_ISSUE_MESSAGES.get(code, f"Summary QA: {code}."),
        ))
    for item in summary_quality.get("errors") or []:
        code = str(item.get("code", "summary_error"))
        issues.append(_issue(
            code,
            _SUMMARY_ISSUE_MESSAGES.get(code, f"Summary QA: {code}."),
            "error",
        ))
    if summary_quality.get("status") not in {None, "pass"} and not (
        summary_quality.get("errors") or []
    ):
        issues.append(_issue(
            "summary_quality",
            "Детермінована перевірка summary потребує уваги.",
            "error",
        ))

    issues = _deduplicate_issues(issues)
    report_status = _status(issues)
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": utc_now(),
        "session": session,
        "source": capture.get("source", manifest.get("source", "local-audio")),
        "status": report_status,
        "status_label": STATUS_LABELS[report_status],
        "capture": capture,
        "summary": summary_quality,
        "issues": issues,
    }
    report_path = work_dir / "quality-report.json"
    atomic_write_json(report_path, report, mode=0o600)
    manifest_path = work_dir / "manifest.json"
    if manifest_path.exists():
        update_manifest(
            manifest_path,
            quality_report={
                "status": report_status,
                "status_label": STATUS_LABELS[report_status],
                "file": report_path.name,
            },
        )
    return report


def report_note_lines(report: dict[str, Any]) -> list[str]:
    """Render concise note metadata; full details remain in quality-report.json."""
    lines = [f"- **Якість:** {report.get('status_label', '—')}"]
    issues = _deduplicate_issues(report.get("issues") or [])
    if issues:
        messages = "; ".join(
            str(item.get("message", "")).rstrip(".")
            for item in issues[:3]
        )
        if len(issues) > 3:
            messages += f"; і ще {len(issues) - 3}"
        lines += ["", f"> ⚠️ Автоматична перевірка якості: {messages}."]
    return lines
