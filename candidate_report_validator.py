"""Deterministic consistency checks for generated candidate reports."""

from __future__ import annotations

import re
from collections.abc import Iterable


REQUIRED_REPORT_FIELDS = (
    "Статус рішення",
    "Етап співбесіди",
    "Продемонстрований рівень",
    "Сигнали наступного рівня",
    "Цільова роль",
    "Цільовий рівень",
    "Відповідність цільовому рівню",
    "Рекомендація поточного етапу",
    "Фінальна рекомендація щодо найму",
    "Критичні компетенції перевірено",
)

_FINAL_HIRING_VALUES = {
    "наймати",
    "скоріше наймати",
    "скоріше не наймати",
    "не наймати",
}
_POSITIVE_HIRING_VALUES = {"наймати", "скоріше наймати"}
_DIMENSION_HEADINGS = (
    "Мотивація",
    "Релевантність досвіду",
    "Критичне мислення",
    "Рефлексія",
    "Усвідомлення упереджень",
)


def report_field(report: str, label: str) -> str:
    match = re.search(
        rf"(?mi)^\*\*{re.escape(label)}:\*\*\s*(.+?)\s*$",
        report,
    )
    return match.group(1).strip() if match else ""


def has_explicit_target_level(target: str, levels: Iterable[str]) -> bool:
    normalized = target.casefold()
    if not normalized:
        return False
    found: set[str] = set()
    for level in levels:
        level = level.strip()
        if level and len(level) >= 2 and re.search(
            rf"(?<!\w){re.escape(level.casefold())}(?!\w)", normalized
        ):
            found.add("middle" if level.casefold() == "mid" else level.casefold())
    for match in re.finditer(
        r"(?i)(?<!\w)(?:intern|trainee|junior|middle|mid|senior|lead|staff|"
        r"principal|ic\s*\d+|l\s*\d+)(?!\w)",
        target,
    ):
        value = re.sub(r"\s+", "", match.group(0)).casefold()
        found.add("middle" if value == "mid" else value)
    return len(found) == 1


def has_explicit_target_role(target: str, levels: Iterable[str]) -> bool:
    """Return true when the target contains text beyond a level token."""
    role = target
    for level in sorted(
        {value.strip() for value in levels if value.strip()},
        key=len,
        reverse=True,
    ):
        role = re.sub(
            rf"(?i)(?<!\w){re.escape(level)}(?!\w)", " ", role
        )
    role = re.sub(
        r"(?i)(?<!\w)(?:intern|trainee|junior|middle|mid|senior|lead|staff|"
        r"principal|ic\s*\d+|l\s*\d+)(?!\w)",
        " ",
        role,
    )
    return bool(re.sub(r"[\s/|,;:()\-]+", "", role))


def is_final_stage(stage: str) -> bool:
    return bool(re.search(r"(?i)(?<!\w)(?:final|фінальн\w*)(?!\w)", stage))


def _normalized_choice(value: str) -> str:
    return re.sub(r"[.!]$", "", value.strip().casefold())


def _matches_choice(value: str, choices: set[str]) -> bool:
    return any(
        value == choice
        or value.startswith(choice + " ")
        or value.startswith(choice + "—")
        for choice in choices
    )


def _section(report: str, heading: str) -> str:
    match = re.search(
        rf"(?ms)^###\s+\d+\.\s+{re.escape(heading)}\b(.*?)(?=^###\s+\d+\.|^##\s+|\Z)",
        report,
    )
    return match.group(1) if match else ""


def _numeric_score(report: str, dimension: str) -> bool:
    pattern = rf"(?mi)^\|\s*{re.escape(dimension)}\s*\|\s*([1-5])\s*\|"
    return bool(re.search(pattern, report))


def _score_and_confidence(report: str, dimension: str) -> tuple[int | None, str]:
    pattern = (
        rf"(?mi)^\|\s*{re.escape(dimension)}\s*\|\s*([1-5])\s*\|"
        rf"\s*([^|]+)\|"
    )
    match = re.search(pattern, report)
    return (int(match.group(1)), match.group(2).strip()) if match else (None, "")


def _evidence_records(evidence: str) -> dict[str, dict[str, str]]:
    records: dict[str, dict[str, str]] = {}
    pattern = re.compile(
        r"(?mi)^\[E(?P<id>[A-Za-z0-9._-]+)\]\s*"
        r"speaker=(?P<speaker>[^|\n]+)\|\s*"
        r"type=(?P<type>[^|\n]+)\|\s*"
        r"situation=(?P<situation>[^|\n]+)\|\s*"
        r"timestamp=(?P<timestamp>[^|\n]+)\|\s*"
        r"dimensions=(?P<dimensions>[^|\n]+)\|\s*"
        r"signals=(?P<signals>[^\n]+)$"
    )
    for match in pattern.finditer(evidence or ""):
        records[match.group("id")] = {
            key: match.group(key).strip().casefold()
            for key in (
                "speaker", "type", "situation", "timestamp", "dimensions", "signals"
            )
        }
    return records


def _normalized_quote(value: str) -> str:
    value = value.casefold().replace("…", " ")
    value = re.sub(r"\.{3,}", " ", value)
    return re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).strip()


def _evidence_quotes(evidence: str) -> dict[str, list[str]]:
    headers = list(re.finditer(
        r"(?mi)^\[E(?P<id>[A-Za-z0-9._-]+)\]\s+speaker=.*$",
        evidence or "",
    ))
    result: dict[str, list[str]] = {}
    for index, match in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(evidence)
        block = evidence[match.end():end]
        quotes: list[str] = []
        for raw in block.splitlines():
            line = raw.strip().removeprefix(">").strip()
            if not line:
                continue
            quoted = re.fullmatch(r'[«"](.+?)[»"]', line)
            if quoted:
                normalized = _normalized_quote(quoted.group(1))
            else:
                normalized = _normalized_quote(line)
            if normalized:
                quotes.append(normalized)
            # The extraction contract puts the verbatim quote immediately
            # after its metadata line. Later prose must not become evidence.
            break
        result[match.group("id")] = quotes
    return result


def _report_quotes(report: str) -> list[tuple[str, str]]:
    return [
        (match.group("id"), _normalized_quote(match.group("quote")))
        for match in re.finditer(
            r'(?mi)^>\s*\[E(?P<id>[A-Za-z0-9._-]+)\]\s*'
            r'[«"](?P<quote>.*?)[»"]',
            report,
        )
    ]


def _cited_evidence_ids(section: str) -> set[str]:
    return set(re.findall(r"\[E([A-Za-z0-9._-]+)\]", section))


def validate_candidate_report(
    report: str,
    *,
    candidate: str,
    target: str,
    interview_stage: str,
    levels: Iterable[str],
    evidence: str = "",
    transcript: str = "",
) -> list[str]:
    """Return actionable report-policy violations without judging semantic scores."""
    errors: list[str] = []
    fields = {label: report_field(report, label) for label in REQUIRED_REPORT_FIELDS}
    errors.extend(
        f"поле `{label}`"
        for label, value in fields.items()
        if not value
    )
    if errors:
        return errors

    decision_status = fields["Статус рішення"].casefold()
    final_recommendation = _normalized_choice(
        fields["Фінальна рекомендація щодо найму"]
    )
    critical_verified = fields["Критичні компетенції перевірено"].casefold()
    target_fit = fields["Відповідність цільовому рівню"].casefold()
    reported_target_role = fields["Цільова роль"].casefold()
    reported_target_level = fields["Цільовий рівень"].casefold()
    explicit_target_level = has_explicit_target_level(target, levels)
    explicit_target_role = has_explicit_target_role(target, levels)
    final_stage = is_final_stage(interview_stage)
    reported_final_stage = is_final_stage(fields["Етап співбесіди"])

    if reported_final_stage != final_stage:
        errors.append("етап у звіті не узгоджений з метаданими зустрічі")

    if "фіналь" in decision_status and not final_stage:
        errors.append("фінальний статус на нефінальному етапі")

    has_final_recommendation = _matches_choice(
        final_recommendation, _FINAL_HIRING_VALUES
    )
    if not final_stage and has_final_recommendation:
        errors.append("фінальна hiring-рекомендація на нефінальному етапі")

    if has_final_recommendation:
        if "фіналь" not in decision_status:
            errors.append("фінальна hiring-рекомендація має попередній статус")
        if not explicit_target_level:
            errors.append("фінальна рекомендація без однозначного target level")
        if not explicit_target_role:
            errors.append("фінальна рекомендація без однозначної target role")
        if not critical_verified.startswith("так"):
            errors.append("фінальна рекомендація з неперевіреними критичними компетенціями")

    if _matches_choice(final_recommendation, _POSITIVE_HIRING_VALUES):
        if not any(value in target_fit for value in ("відповідає", "перевищує")):
            errors.append("позитивна hiring-рекомендація не узгоджена з target fit")

    if not (explicit_target_role and explicit_target_level):
        if not target_fit.startswith("не застосовується"):
            errors.append("target fit задано без однозначних target role і level")
    elif target_fit.startswith("не застосовується"):
        errors.append("target fit відсутній попри однозначні target role і level")

    not_supplied_tokens = ("не задан", "not supplied", "—")
    if explicit_target_role and any(
        token in reported_target_role for token in not_supplied_tokens
    ):
        errors.append("звіт втратив задану target role")
    if explicit_target_level and any(
        token in reported_target_level for token in not_supplied_tokens
    ):
        errors.append("звіт втратив заданий target level")
    if not explicit_target_level and not any(
        token in reported_target_level for token in not_supplied_tokens
    ):
        errors.append("звіт вигадав target level")

    interviewers = report_field(report, "Інтерв’юери")
    if candidate.strip() and candidate.casefold() in interviewers.casefold():
        errors.append("кандидат помилково вказаний серед інтерв’юерів")

    repeated_claim = re.search(
        r"(?i)повторюван\w*\s+(?:патерн|шаблон)|стійк\w*\s+(?:патерн|шаблон)|"
        r"систематичн\w*\s+(?:поведін|прояв|помил)",
        report,
    )
    timestamps = set(
        re.findall(r"(?<!\d)(?:\d{1,2}:)?\d{1,2}:\d{2}(?!\d)", report)
    )
    if repeated_claim and len(timestamps) < 2:
        errors.append("повторюваний патерн заявлено без двох незалежних прикладів")

    records = _evidence_records(evidence)
    evidence_quotes = _evidence_quotes(evidence)
    normalized_transcript = _normalized_quote(transcript)
    for evidence_id, quote in _report_quotes(report):
        supported = any(
            quote and quote in source
            for source in evidence_quotes.get(evidence_id, [])
        )
        if evidence and not supported:
            errors.append(f"цитата `[E{evidence_id}]` не підтверджена evidence ledger")
        if transcript and quote and quote not in normalized_transcript:
            errors.append(f"цитата `[E{evidence_id}]` не знайдена у транскрипті")
    for dimension in _DIMENSION_HEADINGS:
        score, confidence = _score_and_confidence(report, dimension)
        if score is None:
            continue
        section = _section(report, dimension)
        if not section or not re.search(
            r"(?<!\d)(?:\d{1,2}:)?\d{1,2}:\d{2}(?!\d)", section
        ):
            errors.append(f"числова оцінка `{dimension}` без таймкодованої цитати")
        cited_ids = _cited_evidence_ids(section)
        if evidence and not cited_ids:
            errors.append(f"числова оцінка `{dimension}` без evidence ID")
        cited_records = [records[value] for value in cited_ids if value in records]
        if evidence and cited_ids and not cited_records:
            errors.append(f"`{dimension}` посилається на невідомі evidence ID")

        if "висок" in confidence.casefold() or "high" in confidence.casefold():
            situations = {
                item["situation"] for item in cited_records if item["situation"]
            }
            has_strong_observation = any(
                item["type"] in {"candidate_live", "corroborated"}
                for item in cited_records
            )
            if len(situations) < 3 or not has_strong_observation:
                errors.append(
                    f"висока впевненість `{dimension}` без трьох незалежних "
                    "situations і live/corroborated evidence"
                )

        if dimension == "Рефлексія" and score >= 3 and cited_records:
            by_situation: dict[str, set[str]] = {}
            for item in cited_records:
                by_situation.setdefault(item["situation"], set()).update(
                    value.strip() for value in item["signals"].split(",")
                )
            required = {"own_mistake", "lesson", "behavior_change"}
            if not any(required <= signals for signals in by_situation.values()):
                errors.append(
                    "рефлексія 3+ без ланцюжка own_mistake → lesson → behavior_change"
                )

        if dimension == "Усвідомлення упереджень" and cited_records:
            bias_signals = {
                value.strip()
                for item in cited_records
                for value in item["signals"].split(",")
            }
            if not bias_signals.intersection(
                {"bias_probe", "bias_update", "bias_mitigation", "observed_bias"}
            ):
                errors.append(
                    "числова оцінка bias awareness без прямого bias-сигналу"
                )

    level_rows = re.findall(
        r"(?mi)^\|\s*([^|]+)\|\s*(Нижче|Below)\s*\|([^\n]+)$", report
    )
    for level, _, remainder in level_rows:
        if re.search(r"(?i)не перевір|немає даних|відсутн\w+(?:\s+дан|\s+доказ)", remainder):
            if not re.search(r"(?i)супереч|contradict|прям\w+\s+негатив", remainder):
                errors.append(
                    f"рівень `{level.strip()}` позначено Нижче лише через відсутність даних"
                )

    risk_match = re.search(
        r"(?ms)^##\s+Основні ризики найму\s*(.*?)(?=^##\s+|\Z)", report
    )
    risk_section = risk_match.group(1) if risk_match else ""
    risk_rows = [
        line for line in risk_section.splitlines()
        if line.lstrip().startswith("|")
        and "---" not in line
        and "Ризик" not in line
    ]
    for row in risk_rows:
        if not re.search(r"\[E[A-Za-z0-9._-]+\]", row):
            errors.append("ризик найму без evidence ID")

    return errors
