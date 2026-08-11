"""Preflight classification for candidate interviews only."""

from __future__ import annotations

import re


CLASSIFIER_VERSION = 2

OUTCOME_LABELS = {
    "completed": "Повноцінна співбесіда",
    "early_terminated": "Достроково завершена",
    "insufficient_content": "Недостатньо змісту",
    "cancelled_no_show": "Скасована / кандидат не з’явився",
    "technical_failure": "Технічна проблема",
}

_EARLY_TERMINATION_PATTERNS = (
    r"краще\s+не\s+витрачати\s+час",
    r"не\s+(?:буду|хочу|варто)\s+продовж",
    r"не\s+розглядаю\s+для\s+себе",
    r"не\s+співпадає.+професійн",
    r"(?:let'?s|we\s+should)\s+(?:stop|end)\s+here",
    r"(?:withdraw|not\s+interested|not\s+a\s+fit)",
)

_CANCELLED_PATTERNS = (
    r"не\s+прийш(?:ов|ла)",
    r"не\s+з['’]?явив",
    r"no[- ]show",
    r"перенес(?:емо|ти)\s+зустріч",
    r"reschedul",
    r"cancelled",
)

_TECHNICAL_FAILURE_PATTERNS = (
    r"не\s+(?:чути|видно)",
    r"проблем\w*\s+з\s+(?:мікрофон|камер|звук|інтернет)",
    r"technical\s+(?:issue|problem)",
    r"зв['’]?язок\s+обірвав",
)


def _dialogue(transcript: str) -> list[tuple[str, str, str]]:
    result = []
    for line in transcript.splitlines():
        match = re.match(r"^\[([^]]+)\]\s+(.+?):\s*(.*)$", line.strip())
        if match:
            result.append((match.group(1), match.group(2).strip(), match.group(3).strip()))
    return result


def _candidate_from_title(title: str) -> str:
    parts = [part.strip() for part in title.split("|")]
    return parts[1] if len(parts) >= 2 else ""


def _matching(
    dialogue: list[tuple[str, str, str]],
    patterns: tuple[str, ...],
) -> tuple[str, str, str] | None:
    for timestamp, speaker, text in dialogue:
        if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns):
            return timestamp, speaker, text
    return None


def classify_candidate_interview(title: str, transcript: str) -> dict:
    """Decide whether a candidate transcript is eligible for a full scorecard."""
    dialogue = _dialogue(transcript)
    joined = "\n".join(text for _, _, text in dialogue)
    candidate = _candidate_from_title(title)
    candidate_lines = [
        (timestamp, text)
        for timestamp, speaker, text in dialogue
        if candidate and speaker.casefold() == candidate.casefold()
    ]
    candidate_word_count = sum(len(text.split()) for _, text in candidate_lines)
    evidence: list[dict[str, str]] = []

    early = _matching(dialogue, _EARLY_TERMINATION_PATTERNS)
    early_is_candidate = bool(
        early and candidate and early[1].casefold() == candidate.casefold()
    )
    early_is_near_end = bool(early and early in dialogue[-3:])
    early_has_limited_evidence = (
        len(candidate_lines) < 4 or candidate_word_count < 180
    )
    if early and early_is_candidate and early_is_near_end and early_has_limited_evidence:
        timestamp, speaker, text = early
        evidence.append({"timestamp": timestamp, "speaker": speaker, "quote": text})
        outcome = "early_terminated"
        reason = "Розмову завершено до переходу до повного оцінювання."
    else:
        cancelled = _matching(dialogue, _CANCELLED_PATTERNS)
        technical = _matching(dialogue, _TECHNICAL_FAILURE_PATTERNS)
        if cancelled and len(dialogue) <= 20:
            timestamp, speaker, text = cancelled
            evidence.append({"timestamp": timestamp, "speaker": speaker, "quote": text})
            outcome = "cancelled_no_show"
            reason = "Співбесіду скасовано, перенесено або кандидат не з’явився."
        elif technical and len(joined.split()) < 180:
            timestamp, speaker, text = technical
            evidence.append({"timestamp": timestamp, "speaker": speaker, "quote": text})
            outcome = "technical_failure"
            reason = "Змістовна частина співбесіди не відбулася через технічну проблему."
        elif len(candidate_lines) < 2 or candidate_word_count < 40:
            outcome = "insufficient_content"
            reason = "У транскрипті недостатньо відповідей кандидата для оцінювання."
        else:
            outcome = "completed"
            reason = "Транскрипт містить достатньо відповідей для повного candidate flow."

    return {
        "schema_version": CLASSIFIER_VERSION,
        "meeting_type": "hiring_interview",
        "meeting_type_label": "Співбесіда",
        "outcome": outcome,
        "outcome_label": OUTCOME_LABELS[outcome],
        "reason": reason,
        "evidence": evidence,
        "candidate_evaluation_eligible": outcome == "completed",
        "candidate_last_timestamp": candidate_lines[-1][0] if candidate_lines else "",
    }
