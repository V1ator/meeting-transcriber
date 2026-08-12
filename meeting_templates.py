#!/usr/bin/env python3
"""Deterministic meeting classification and evidence-backed note templates."""

from __future__ import annotations

import re
from typing import Any

TEMPLATE_VERSION = 1

TYPE_LABELS = {
    "general": "Звичайна зустріч",
    "standup": "Standup / статус",
    "planning": "Планування",
    "discovery": "Дослідження / інтерв’ю",
    "design_review": "Design review",
    "one_on_one": "1:1",
}

TYPE_HEADINGS = {
    "standup": "## Standup: прогрес і блокери",
    "planning": "## Планування: scope і ризики",
    "discovery": "## Інсайти дослідження",
    "design_review": "## Design review: фідбек",
    "one_on_one": "## 1:1: теми й домовленості",
}

_TITLE_PATTERNS = (
    ("one_on_one", re.compile(
        r"(?:^|\b)(?:1\s*[:x]\s*1|one[- ]on[- ]one|ван[- ]ту[- ]ван)(?:\b|$)",
        re.I,
    )),
    ("standup", re.compile(
        r"\b(?:stand[- ]?up|daily|стендап\w*|дейл[іи]|"
        r"щоденн\w*\s+(?:зустріч|синк))\b",
        re.I,
    )),
    ("design_review", re.compile(
        r"\b(?:design\s+(?:review|critique)|дизайн[- ]?рев['’]?ю|"
        r"рев['’]?ю\s+дизайн|дизайн\w*\s+(?:макет|дашборд)|"
        r"макет\w*\s+дизайн)\b",
        re.I,
    )),
    ("discovery", re.compile(
        r"\b(?:discovery|user\s+interview|customer\s+interview|"
        r"research\s+interview|дослідженн\w*|кастдев\w*|"
        r"інтерв['’]?ю\s+(?:з\s+)?(?:користувач\w*|клієнт\w*))\b",
        re.I,
    )),
    ("planning", re.compile(
        r"\b(?:sprint\s+planning|roadmap|refinement|grooming|"
        r"плануванн\w*|планування\s+спринту|роадмап\w*|беклог\w*)\b",
        re.I,
    )),
)


def detect_meeting_type(title: str, transcript: str = "") -> str:
    """Classify from a title first; use repeated transcript cues as fallback."""
    clean_title = re.sub(r"\s+", " ", str(title or "")).strip()
    for meeting_type, pattern in _TITLE_PATTERNS:
        if pattern.search(clean_title):
            return meeting_type
    sample = str(transcript or "")[:80_000]
    for meeting_type, pattern in _TITLE_PATTERNS:
        if len(pattern.findall(sample)) >= 2:
            return meeting_type
    return "general"


def meeting_type_label(meeting_type: str) -> str:
    return TYPE_LABELS.get(meeting_type, TYPE_LABELS["general"])


def template_heading(meeting_type: str) -> str | None:
    return TYPE_HEADINGS.get(meeting_type)


def _claim(item: dict[str, Any]) -> str:
    return re.sub(r"\s+", " ", str(item.get("claim", ""))).strip()


def _selected_items(
    ledger: dict[str, Any], meeting_type: str
) -> list[dict[str, Any]]:
    allowed = {
        "standup": {"completed_action", "commitment", "open_question"},
        "planning": {"proposal", "decision", "commitment", "open_question"},
        "discovery": {
            "fact", "participant_claim", "hypothesis", "recommendation",
            "open_question",
        },
        "design_review": {
            "participant_claim", "recommendation", "proposal", "decision",
            "commitment", "open_question",
        },
        "one_on_one": {
            "participant_claim", "recommendation", "decision", "commitment",
            "open_question",
        },
    }.get(meeting_type, set())
    return [
        item for item in ledger.get("items", [])
        if item.get("type") in allowed
        and item.get("status") != "superseded"
        and _claim(item)
    ][:10]


def render_template_section(meeting_type: str, ledger: dict[str, Any]) -> str:
    """Render a specialized section using only validated evidence claims."""
    heading = template_heading(meeting_type)
    if not heading:
        return ""
    prefixes = {
        "completed_action": "Зроблено",
        "commitment": "Наступний крок",
        "open_question": "Відкрите питання",
        "proposal": "Пропозиція",
        "decision": "Рішення",
        "fact": "Спостереження",
        "participant_claim": "Інсайт",
        "hypothesis": "Гіпотеза",
        "recommendation": "Рекомендація",
    }
    lines: list[str] = []
    seen: set[str] = set()
    for item in _selected_items(ledger, meeting_type):
        claim = _claim(item)
        key = claim.casefold().rstrip(". ")
        if key in seen:
            continue
        seen.add(key)
        prefix = prefixes.get(str(item.get("type")), "Теза")
        owners = [
            str(owner).strip() for owner in item.get("owners", [])
            if str(owner).strip()
        ]
        owner_suffix = f" — {' / '.join(owners)}" if owners else ""
        lines.append(f"- **{prefix}:** {claim}{owner_suffix}")
    if not lines:
        lines = ["- —"]
    return f"{heading}\n" + "\n".join(lines) + "\n"
