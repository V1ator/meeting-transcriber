#!/usr/bin/env python3
"""Sync action items from local meeting notes to a Notion task board."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pipeline_utils import (
    atomic_write_json,
    ensure_private_dir,
    load_dotenv,
    read_json,
    utc_now,
)


BASE = Path(__file__).parent
NOTES = BASE / "notes"
TRANSCRIPTS = BASE / "transcripts"
STATE_PATH = TRANSCRIPTS / ".notion-sync-state.json"
PENDING_PATH = TRANSCRIPTS / ".notion-pending.json"
LOCK_PATH = TRANSCRIPTS / ".notion-sync.lock"
NOTION_API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
MAX_API_ATTEMPTS = 5
CREATE_INTERVAL_SECONDS = 0.35
NAME_PROPERTY = "Name"
INVOLVED_PROPERTY = "Involved"
SOURCE_PROPERTY = "Source"
STATUS_PROPERTY = "Status"
GENERATED_STATUS = "Inbox"
DEFAULT_TASK_OWNER_NAMES = (
    "Current User",
    "Поточний користувач",
)

load_dotenv(BASE / ".env")


class NotionSyncError(RuntimeError):
    """Actionable Notion configuration or API failure."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward the Notion bearer token to a redirected host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "Notion API redirect заблоковано",
            headers,
            fp,
        )


@dataclass(frozen=True)
class NotionConfig:
    enabled: bool
    api_key: str
    data_source_id: str

    @classmethod
    def from_env(cls) -> "NotionConfig":
        return cls(
            enabled=os.environ.get("NOTION_SYNC_ENABLED", "false").lower() == "true",
            api_key=os.environ.get("NOTION_API_KEY", "").strip(),
            data_source_id=os.environ.get("NOTION_DATA_SOURCE_ID", "").strip(),
        )

    def validate(self) -> None:
        missing = []
        if not self.api_key:
            missing.append("NOTION_API_KEY")
        if not self.data_source_id:
            missing.append("NOTION_DATA_SOURCE_ID")
        if missing:
            raise NotionSyncError(
                "Не заповнено Notion-конфігурацію: " + ", ".join(missing)
            )


@dataclass(frozen=True)
class ActionItem:
    note: Path
    meeting: str
    source: str
    name: str
    involved: str
    fingerprint: str
    kind: str = "meeting-action"


@dataclass(frozen=True)
class SyncResult:
    found: int
    pending: int
    created: int
    skipped: int


def _section(text: str, heading: str) -> str:
    match = re.search(
        rf"(?ms)^{re.escape(heading)}[ \t]*\n(.*?)(?=^##[ \t]|\Z)",
        text,
    )
    return match.group(1) if match else ""


def _meeting_title(text: str, fallback: str) -> str:
    match = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    if not match:
        return fallback
    title = match.group(1).strip()
    return re.sub(r"\s+\([^)]+\)\s*$", "", title).strip() or fallback


def _metadata_value(text: str, label: str) -> str:
    match = re.search(
        rf"(?m)^-\s+\*\*{re.escape(label)}:\*\*\s*(.+?)\s*$",
        text,
    )
    return match.group(1).strip() if match else ""


def _source_value(text: str, note: Path, fallback_title: str) -> str:
    date_value = _metadata_value(text, "Дата")
    if not date_value:
        match = re.match(r"^(\d{4}-\d{2}-\d{2})", note.name)
        date_value = match.group(1) if match else "Дата невідома"
    meeting = _metadata_value(text, "Назва зустрічі") or fallback_title
    return f"{date_value} — {meeting}"


def _speaker_mapping(text: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for raw_line in _section(text, "## Мапінг спікерів").splitlines():
        if not raw_line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in raw_line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        label, name = cells[0], cells[1]
        if (
            label
            and name
            and label.lower() != "спікер"
            and set(label) != {"-"}
            and set(name) != {"-"}
        ):
            mapping[label] = name
    return mapping


def _plain_markdown(value: str) -> str:
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"(\*\*|__|`)", "", value)
    return re.sub(r"\s+", " ", value).strip()


def configured_task_owner_names() -> tuple[str, ...]:
    raw = os.environ.get("NOTION_TASK_OWNER_NAMES", "").strip()
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    return values or DEFAULT_TASK_OWNER_NAMES


def is_current_user_owner(
    involved: str,
    *,
    owner_names: Iterable[str] | None = None,
) -> bool:
    """Match a configured person as a distinct owner, including joint ownership."""
    normalized = _plain_markdown(involved).casefold()
    if not normalized:
        return False
    aliases = owner_names or configured_task_owner_names()
    return any(
        bool(re.search(
            rf"(?<!\w){re.escape(_plain_markdown(alias).casefold())}(?!\w)",
            normalized,
        ))
        for alias in aliases
        if _plain_markdown(alias)
    )


def _is_user_task(item: ActionItem) -> bool:
    return item.kind == "candidate-feedback" or is_current_user_owner(item.involved)


def _resolve_involved(value: str, mapping: dict[str, str]) -> str:
    resolved = _plain_markdown(value)
    if _is_placeholder(resolved):
        return ""
    for label, name in sorted(mapping.items(), key=lambda pair: -len(pair[0])):
        resolved = re.sub(rf"\b{re.escape(label)}\b", name, resolved)
    return resolved


def _clean_task_name(value: str) -> str:
    value = _plain_markdown(value)
    value = re.sub(
        r"\s*[—–-]\s*(?:дедлайн|термін)\s+"
        r"(?:не\s+(?:вказан(?:о|ий)|звучав|визначен(?:о|ий))|відсутній)"
        r"(?:\s*\([^)]*\))?\.?\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\s*[—–-]\s*без\s+(?:чіткого\s+)?дедлайну\.?\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return value.strip(" \t—–-.")


def _is_placeholder(value: str) -> bool:
    normalized = _plain_markdown(value).strip(" «»—–-.").lower()
    return normalized in {
        "",
        "немає",
        "відсутні",
        "відсутнє",
        "не визначено",
        "не було",
    }


def _split_owner(value: str) -> tuple[str, str]:
    match = re.match(r"^\s*(?:\*\*)?\[([^\]]+)\](?:\*\*)?\s*(.*)$", value)
    if not match:
        return "", value.strip()
    return match.group(1).strip(), match.group(2).strip()


def _fingerprint(note: Path, name: str, involved: str) -> str:
    stable = "\n".join((
        note.name,
        re.sub(r"\s+", " ", name).casefold(),
        re.sub(r"\s+", " ", involved).casefold(),
    ))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def parse_action_items(note: Path, *, text: str | None = None) -> list[ActionItem]:
    """Parse top-level and nested Markdown bullets from ## Action items."""
    content = note.read_text(encoding="utf-8") if text is None else text
    section = _section(content, "## Action items")
    if not section:
        return []

    mapping = _speaker_mapping(content)
    meeting = _meeting_title(content, note.stem)
    source = _source_value(content, note, meeting)
    parsed: list[tuple[str, str]] = []
    parent_owner = ""
    current_index: int | None = None

    for raw_line in section.splitlines():
        top = re.match(r"^[-*]\s+(.*)$", raw_line)
        nested = re.match(r"^\s{2,}[-*]\s+(.*)$", raw_line)
        if top:
            owner, body = _split_owner(top.group(1))
            if body:
                parsed.append((owner, body))
                current_index = len(parsed) - 1
                parent_owner = ""
            else:
                parent_owner = owner
                current_index = None
            continue
        if nested:
            owner, body = _split_owner(nested.group(1))
            parsed.append((owner or parent_owner, body))
            current_index = len(parsed) - 1
            continue
        continuation = raw_line.strip()
        if continuation and current_index is not None:
            owner, body = parsed[current_index]
            parsed[current_index] = (owner, f"{body} {continuation}")

    result = []
    for owner, raw_name in parsed:
        if _is_placeholder(raw_name):
            continue
        name = _clean_task_name(raw_name)
        if not name:
            continue
        involved = _resolve_involved(owner, mapping)
        result.append(ActionItem(
            note=note,
            meeting=meeting,
            source=source,
            name=name,
            involved=involved,
            fingerprint=_fingerprint(note, name, involved),
        ))
    return result


def action_items_from_ledger(
    note: Path,
    ledger: dict[str, Any],
    *,
    text: str | None = None,
) -> list[ActionItem]:
    """Build tasks from the structured summary ledger without re-parsing Markdown."""
    content = note.read_text(encoding="utf-8") if text is None else text
    raw_items = ledger.get("items")
    if not isinstance(raw_items, list):
        return []
    mapping = _speaker_mapping(content)
    meeting = _meeting_title(content, note.stem)
    source = _source_value(content, note, meeting)
    result: list[ActionItem] = []
    seen: set[str] = set()
    for raw in raw_items:
        if (
            not isinstance(raw, dict)
            or raw.get("type") != "commitment"
            or raw.get("status") != "open"
        ):
            continue
        claim = _plain_markdown(str(raw.get("claim", "")))
        key = re.sub(r"\s+", " ", claim).casefold()
        if not claim or key in seen:
            continue
        seen.add(key)
        if (
            raw.get("commitment_strength") == "soft"
            and not re.match(r"(?i)^(спробувати|постаратися|планувати)\b", claim)
        ):
            claim = f"Спробувати {claim[:1].lower() + claim[1:]}"
        deadline = _plain_markdown(str(raw.get("deadline", "")))
        name = _clean_task_name(
            f"{claim} — дедлайн: {deadline}" if deadline else claim
        )
        owners = raw.get("owners")
        if not isinstance(owners, list):
            owners = []
        involved = _resolve_involved(
            " / ".join(
                _plain_markdown(str(owner))
                for owner in owners
                if _plain_markdown(str(owner))
            ),
            mapping,
        )
        result.append(ActionItem(
            note=note,
            meeting=meeting,
            source=source,
            name=name,
            involved=involved,
            fingerprint=_fingerprint(note, name, involved),
        ))
    return result


class NotionClient:
    def __init__(self, config: NotionConfig, *, timeout: float = 20.0):
        config.validate()
        self.config = config
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Notion-Version": NOTION_VERSION,
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            NOTION_API_URL + path,
            data=body,
            headers=headers,
            method=method,
        )
        for attempt in range(MAX_API_ATTEMPTS):
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                retryable = exc.code in {409, 429, 500, 502, 503, 504}
                if retryable and attempt + 1 < MAX_API_ATTEMPTS:
                    try:
                        delay = float(exc.headers.get("Retry-After", ""))
                    except (TypeError, ValueError):
                        delay = min(2 ** attempt, 30)
                    time.sleep(max(0.25, delay))
                    continue
                try:
                    error = json.loads(exc.read().decode("utf-8"))
                    message = error.get("message") or error.get("code") or str(exc)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    message = str(exc)
                raise NotionSyncError(
                    f"Notion API HTTP {exc.code}: {message}"
                ) from exc
            except urllib.error.URLError as exc:
                if attempt + 1 < MAX_API_ATTEMPTS:
                    time.sleep(min(2 ** attempt, 30))
                    continue
                raise NotionSyncError(
                    f"Notion API недоступний: {exc.reason}"
                ) from exc
        raise NotionSyncError("Notion API: вичерпано повторні спроби")

    def validate_schema(self) -> None:
        data = self._request(
            "GET", f"/data_sources/{self.config.data_source_id}"
        )
        properties = data.get("properties") or {}
        expected = {
            NAME_PROPERTY: "title",
            INVOLVED_PROPERTY: "rich_text",
            SOURCE_PROPERTY: "rich_text",
            STATUS_PROPERTY: "status",
        }
        errors = []
        for name, expected_type in expected.items():
            actual = (properties.get(name) or {}).get("type")
            if actual != expected_type:
                errors.append(f"{name}: очікується {expected_type}, отримано {actual or 'немає'}")
        if errors:
            raise NotionSyncError(
                "Несумісна схема Notion data source: " + "; ".join(errors)
            )
        status_options = (
            (properties.get(STATUS_PROPERTY) or {})
            .get("status", {})
            .get("options", [])
        )
        if GENERATED_STATUS not in {
            option.get("name") for option in status_options
        }:
            raise NotionSyncError(
                f"У Notion немає статусу {GENERATED_STATUS!r}"
            )

    def create_task(self, item: ActionItem) -> dict[str, Any]:
        properties: dict[str, Any] = {
            NAME_PROPERTY: {
                "title": [{"text": {"content": item.name[:2000]}}],
            },
            INVOLVED_PROPERTY: {
                "rich_text": (
                    [{"text": {"content": item.involved[:2000]}}]
                    if item.involved else []
                ),
            },
            SOURCE_PROPERTY: {
                "rich_text": [{"text": {"content": item.source[:2000]}}],
            },
            STATUS_PROPERTY: {
                "status": {"name": GENERATED_STATUS},
            },
        }
        return self._request("POST", "/pages", {
            "parent": {
                "type": "data_source_id",
                "data_source_id": self.config.data_source_id,
            },
            "properties": properties,
        })

    def trash_page(self, page_id: str) -> dict[str, Any]:
        """Move a page to Notion trash; the operation remains recoverable there."""
        return self._request(
            "PATCH",
            f"/pages/{page_id}",
            {"in_trash": True},
        )


def _note_paths(notes: Iterable[Path] | None = None) -> list[Path]:
    paths = list(notes) if notes is not None else list(NOTES.glob("*.md"))
    return sorted(
        (path.resolve() for path in paths if path.is_file()),
        key=lambda path: path.name,
    )


def _load_state() -> dict[str, Any]:
    def valid(path: Path) -> dict[str, Any] | None:
        state = read_json(path, None)
        if (
            not isinstance(state, dict)
            or state.get("schema_version") != 1
            or not isinstance(state.get("tasks"), dict)
        ):
            return None
        return state

    backup_path = STATE_PATH.with_name(".notion-sync-state.backup.json")
    primary = valid(STATE_PATH)
    backup = valid(backup_path)
    if primary is not None and (
        backup is None or len(primary["tasks"]) >= len(backup["tasks"])
    ):
        if backup is None:
            atomic_write_json(backup_path, primary, mode=0o600)
        return primary
    if backup is not None:
        if STATE_PATH.exists() and primary is None:
            corrupt = STATE_PATH.with_name(
                f"{STATE_PATH.stem}.corrupt-{int(time.time())}.json"
            )
            shutil.copy2(STATE_PATH, corrupt)
            os.chmod(corrupt, 0o600)
        atomic_write_json(STATE_PATH, backup, mode=0o600)
        return backup
    if not STATE_PATH.exists():
        return {"schema_version": 1, "tasks": {}}
    corrupt = STATE_PATH.with_name(
        f"{STATE_PATH.stem}.corrupt-{int(time.time())}.json"
    )
    shutil.copy2(STATE_PATH, corrupt)
    os.chmod(corrupt, 0o600)
    raise NotionSyncError(
        f"Пошкоджено Notion sync state; копію збережено у {corrupt}"
    )


def _save_state(state: dict[str, Any]) -> None:
    """Keep a complete second copy so corruption cannot reset idempotency."""
    backup_path = STATE_PATH.with_name(".notion-sync-state.backup.json")
    atomic_write_json(backup_path, state, mode=0o600)
    atomic_write_json(STATE_PATH, state, mode=0o600)


def _load_pending() -> dict[str, Any]:
    pending = read_json(PENDING_PATH, {}) or {}
    if not isinstance(pending, dict) or pending.get("schema_version") != 1:
        return {"schema_version": 1, "items": {}}
    if not isinstance(pending.get("items"), dict):
        pending["items"] = {}
    return pending


def _safe_error(error: Exception) -> str:
    return re.sub(
        r"\b(?:hf_|ntn_)[A-Za-z0-9_-]+",
        "<redacted>",
        f"{error.__class__.__name__}: {error}",
    )


def _defer_item(item: ActionItem, error: Exception) -> None:
    pending = _load_pending()
    previous = pending["items"].get(item.fingerprint, {})
    attempts = int(previous.get("attempts", 0) or 0) + 1
    pending["items"][item.fingerprint] = {
        "note": str(item.note),
        "meeting": item.meeting,
        "source": item.source,
        "name": item.name,
        "involved": item.involved,
        "kind": item.kind,
        "attempts": attempts,
        "next_retry_at": time.time() + min(3600, 60 * 2 ** min(attempts - 1, 6)),
        "last_error": _safe_error(error),
        "deferred_at": utc_now(),
    }
    atomic_write_json(PENDING_PATH, pending, mode=0o600)


def retry_deferred_if_enabled(*, logger=print, now: float | None = None) -> int:
    """Retry only tasks explicitly queued after a failed live sync."""
    config = NotionConfig.from_env()
    if not config.enabled:
        return 0
    current = time.time() if now is None else now
    created = 0
    pending = _load_pending()
    changed = False
    for fingerprint, raw in list(pending["items"].items()):
        if float(raw.get("next_retry_at", 0) or 0) > current:
            continue
        kind = str(raw.get("kind", "meeting-action"))
        involved = str(raw.get("involved", ""))
        if kind != "candidate-feedback" and not is_current_user_owner(involved):
            pending["items"].pop(fingerprint, None)
            changed = True
            continue
        item = ActionItem(
            note=Path(str(raw.get("note", ""))),
            meeting=str(raw.get("meeting", "")),
            source=str(raw.get("source", "")),
            name=str(raw.get("name", "")),
            involved=involved,
            fingerprint=fingerprint,
            kind=kind,
        )
        try:
            result, _ = sync_items([item], config=config)
            created += result.created
            pending["items"].pop(fingerprint, None)
            changed = True
        except Exception as exc:
            attempts = int(raw.get("attempts", 0) or 0) + 1
            raw["attempts"] = attempts
            raw["next_retry_at"] = current + min(
                3600, 60 * 2 ** min(attempts - 1, 6)
            )
            raw["last_error"] = _safe_error(exc)
            changed = True
    if changed:
        atomic_write_json(PENDING_PATH, pending, mode=0o600)
    if created:
        logger(f"  Notion retry: створено задач — {created}")
    return created


def collect_action_items(
    notes: Iterable[Path] | None = None,
) -> list[ActionItem]:
    items = []
    for note in _note_paths(notes):
        items.extend(
            item for item in parse_action_items(note)
            if is_current_user_owner(item.involved)
        )
    return items


def sync_notes(
    notes: Iterable[Path] | None = None,
    *,
    dry_run: bool = False,
    config: NotionConfig | None = None,
    client: NotionClient | None = None,
) -> tuple[SyncResult, list[ActionItem]]:
    """Sync notes once and return the summary plus items pending at scan time."""
    items = collect_action_items(notes)
    return sync_items(items, dry_run=dry_run, config=config, client=client)


def sync_items(
    items: Iterable[ActionItem],
    *,
    dry_run: bool = False,
    config: NotionConfig | None = None,
    client: NotionClient | None = None,
) -> tuple[SyncResult, list[ActionItem]]:
    """Sync already-constructed tasks through the same idempotent state store."""
    items = [item for item in items if _is_user_task(item)]
    ensure_private_dir(TRANSCRIPTS)
    LOCK_PATH.touch(mode=0o600, exist_ok=True)

    with LOCK_PATH.open("r+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = _load_state()
        synced = state["tasks"]
        pending = [item for item in items if item.fingerprint not in synced]
        if dry_run:
            return (
                SyncResult(
                    found=len(items),
                    pending=len(pending),
                    created=0,
                    skipped=len(items) - len(pending),
                ),
                pending,
            )

        active_config = config or NotionConfig.from_env()
        if not active_config.enabled:
            raise NotionSyncError(
                "NOTION_SYNC_ENABLED=false; використайте --dry-run або увімкніть sync"
            )
        notion = client or NotionClient(active_config)
        if pending:
            notion.validate_schema()
        created = 0
        for item in pending:
            page = notion.create_task(item)
            page_id = page.get("id")
            if not page_id:
                raise NotionSyncError("Notion не повернув ID створеної сторінки")
            synced[item.fingerprint] = {
                "page_id": page_id,
                "url": page.get("url"),
                "note": item.note.name,
                "meeting": item.meeting,
                "source": item.source,
                "name": item.name,
                "involved": item.involved,
                "synced_at": utc_now(),
            }
            _save_state(state)
            created += 1
            if created < len(pending):
                time.sleep(CREATE_INTERVAL_SECONDS)

        return (
            SyncResult(
                found=len(items),
                pending=len(pending),
                created=created,
                skipped=len(items) - len(pending),
            ),
            pending,
        )


def trash_non_owner_tasks_for_note(
    note: Path,
    *,
    config: NotionConfig | None = None,
    client: NotionClient | None = None,
) -> int:
    """Trash integration-created tasks for one note unless assigned to the user."""
    ensure_private_dir(TRANSCRIPTS)
    LOCK_PATH.touch(mode=0o600, exist_ok=True)
    with LOCK_PATH.open("r+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = _load_state()
        targets = [
            raw
            for raw in state["tasks"].values()
            if raw.get("note") == note.name
            and raw.get("page_id")
            and not raw.get("trashed_at")
            and not is_current_user_owner(str(raw.get("involved", "")))
        ]
        if not targets:
            return 0
        notion = client or NotionClient(config or NotionConfig.from_env())
        trashed = 0
        for raw in targets:
            notion.trash_page(str(raw["page_id"]))
            raw["trashed_at"] = utc_now()
            raw["in_trash"] = True
            _save_state(state)
            trashed += 1
        return trashed


def evaluation_feedback_item(
    report: Path,
    *,
    candidate: str,
    meeting_title: str,
    meeting_date: str,
    evaluation_id: str,
) -> ActionItem:
    """Build one stable feedback task for a candidate evaluation."""
    fingerprint = hashlib.sha256(
        f"candidate-feedback\n{evaluation_id}".encode("utf-8")
    ).hexdigest()
    return ActionItem(
        note=report,
        meeting=meeting_title,
        source=f"{meeting_date} — {meeting_title}",
        name=f"Дати фідбек по кандидату: {candidate}",
        involved=configured_task_owner_names()[0],
        fingerprint=fingerprint,
        kind="candidate-feedback",
    )


def sync_evaluation_feedback_if_enabled(
    report: Path,
    *,
    candidate: str,
    meeting_title: str,
    meeting_date: str,
    evaluation_id: str,
    logger=print,
) -> SyncResult | None:
    """Create the candidate-feedback task using the existing Notion integration."""
    config = NotionConfig.from_env()
    if not config.enabled:
        return None
    item = evaluation_feedback_item(
        report,
        candidate=candidate,
        meeting_title=meeting_title,
        meeting_date=meeting_date,
        evaluation_id=evaluation_id,
    )
    try:
        result, _ = sync_items([item], config=config)
        if result.created:
            logger("  Notion: створено задачу на фідбек")
        return result
    except Exception as exc:
        _defer_item(item, exc)
        logger(f"  Notion sync фідбеку відкладено: {exc}")
        return None


def sync_note_if_enabled(
    note: Path,
    *,
    ledger: dict[str, Any] | None = None,
    logger=print,
) -> SyncResult | None:
    config = NotionConfig.from_env()
    if not config.enabled:
        return None
    try:
        note.resolve().relative_to(NOTES.resolve())
    except ValueError:
        # Захист від випадкової відправки довільного Markdown поза notes/.
        return None
    structured = (
        isinstance(ledger, dict) and isinstance(ledger.get("items"), list)
    )
    items = (
        action_items_from_ledger(note, ledger)
        if structured
        else collect_action_items([note])
    )
    try:
        result, _ = sync_items(items, config=config)
        if result.created:
            logger(f"  Notion: створено задач — {result.created}")
        return result
    except Exception as exc:
        for item in items:
            _defer_item(item, exc)
        logger(f"  Notion sync відкладено: {exc}")
        return None


def _validate_note(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = NOTES / path
    path = path.resolve()
    try:
        path.relative_to(NOTES.resolve())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Note має бути всередині notes/") from exc
    if not path.is_file() or path.suffix.lower() != ".md":
        raise argparse.ArgumentTypeError(f"Note не знайдено: {path}")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="показати нові задачі без звернення до Notion (default)",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="створити нові задачі в Notion",
    )
    mode.add_argument(
        "--trash-non-owner",
        action="store_true",
        help="перемістити в кошик створені інтеграцією задачі не мого owner",
    )
    parser.add_argument(
        "--note",
        action="append",
        type=_validate_note,
        help="обробити конкретну note з notes/; можна повторювати",
    )
    args = parser.parse_args()
    dry_run = not args.apply

    if args.trash_non_owner:
        if not args.note:
            parser.error("--trash-non-owner потребує щонайменше один --note")
        try:
            trashed = sum(trash_non_owner_tasks_for_note(note) for note in args.note)
        except NotionSyncError as exc:
            print(f"Помилка: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        print(f"Переміщено в кошик Notion: {trashed}")
        return

    try:
        result, pending = sync_notes(args.note, dry_run=dry_run)
    except NotionSyncError as exc:
        print(f"Помилка: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if dry_run:
        for item in pending:
            involved = f" [{item.involved}]" if item.involved else ""
            print(f"- {item.note.name}{involved}: {item.name}")
    print(
        f"Знайдено: {result.found}; нових: {result.pending}; "
        f"створено: {result.created}; вже синхронізовано: {result.skipped}"
    )


if __name__ == "__main__":
    main()
