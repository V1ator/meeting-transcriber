"""Preflight classification for candidate interviews only."""

from __future__ import annotations

import re


CLASSIFIER_VERSION = 5

OUTCOME_LABELS = {
    "completed": "Повноцінна співбесіда",
    "early_terminated": "Достроково завершена",
    "insufficient_content": "Недостатньо змісту",
    "cancelled_no_show": "Скасована / кандидат не з’явився",
    "technical_failure": "Технічна проблема",
    "identity_unresolved": "Потрібна перевірка кандидата",
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

_CYRILLIC_TRANSLITERATION = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g",
        "д": "d", "е": "e", "є": "ie", "ж": "zh", "з": "z",
        "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k",
        "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
        "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
        "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
        "ь": "", "ю": "iu", "я": "ia", "ы": "y", "э": "e",
        "ъ": "", "ё": "io",
    }
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


def _name_key(name: str) -> str:
    """Return a script-independent key for Latin/Cyrillic participant names."""
    latinized = name.casefold().translate(_CYRILLIC_TRANSLITERATION)
    return re.sub(r"[^a-z0-9]+", "", latinized)


def _name_parts(name: str) -> list[str]:
    """Return transliterated name parts while preserving first/last-name boundaries."""
    return [
        key
        for part in re.split(r"\s+", name.strip())
        if (key := _name_key(part))
    ]


def _first_name_key(name: str) -> str:
    """Normalize common Ukrainian initial-letter transliteration variants."""
    key = _name_key(name)
    for prefix in ("ie", "ye", "iu", "yu", "ia", "ya", "yi"):
        if key.startswith(prefix):
            return key[1:]
    return key


def _first_last_pairs(parts: list[str]) -> tuple[tuple[str, str], ...]:
    """Return plausible first/surname pairs for normal and Meet-reversed names."""
    if len(parts) < 2:
        return ()
    return (
        (_first_name_key(parts[0]), parts[-1]),
        (_first_name_key(parts[-1]), parts[0]),
    )


def same_person(left: str, right: str) -> bool:
    left_parts = _name_parts(left)
    right_parts = _name_parts(right)
    if not left_parts or not right_parts:
        return False
    if left_parts == right_parts:
        return True
    if len(left_parts) < 2 or len(right_parts) < 2:
        return False

    # The surname remains strict; the first name tolerates established
    # Ukrainian variants such as Evhenii/Ievhenii/Yevhenii. Google Meet may
    # also display the same participant as either `First Last` or `Last First`.
    return bool(
        set(_first_last_pairs(left_parts))
        & set(_first_last_pairs(right_parts))
    )


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
        if candidate and same_person(speaker, candidate)
    ]
    candidate_word_count = sum(len(text.split()) for _, text in candidate_lines)
    evidence: list[dict[str, str]] = []

    early = _matching(dialogue, _EARLY_TERMINATION_PATTERNS)
    early_is_candidate = bool(
        early and candidate and same_person(early[1], candidate)
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
        elif not candidate_lines and len(dialogue) >= 8 and len(joined.split()) >= 180:
            outcome = "identity_unresolved"
            reason = (
                "Транскрипт містить змістовну розмову, але спікера кандидата "
                "не вдалося надійно зіставити з ім’ям у назві зустрічі."
            )
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
