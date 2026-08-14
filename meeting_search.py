#!/usr/bin/env python3
"""Локальний повнотекстовий пошук у зустрічах."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import paths as project_paths
from pipeline_utils import ensure_private_dir, read_json, utc_now


SCHEMA_VERSION = 1
INDEX_CONTENT_VERSION = 1
INDEX_FILENAME = "meeting-search.sqlite3"
SESSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
TRANSCRIPT_LINE = re.compile(r"^\[([^]]+)]\s+(.+?):\s*(.*)$")
WORD_PATTERN = re.compile(r"[^\W_]+", flags=re.UNICODE)
SEARCH_STOPWORDS = {
    "а", "але", "в", "ви", "до", "з", "за", "і", "із", "й", "ми",
    "на", "не", "про", "та", "у", "що", "як", "які", "яка", "який",
    "було", "були", "вирішили", "обговорювали", "зустріч", "зустрічі",
    "meeting", "meetings", "the", "what", "about",
}
CHUNK_LABELS = {
    "title": "Назва",
    "tldr": "TL;DR",
    "fact": "Факт",
    "participant_claim": "Позиція учасника",
    "recommendation": "Рекомендація",
    "hypothesis": "Гіпотеза",
    "proposal": "Пропозиція",
    "decision": "Рішення",
    "commitment": "Action item",
    "completed_action": "Виконана дія",
    "open_question": "Відкрите питання",
    "transcript": "Транскрипт",
    "chat": "Чат",
}
SEARCHABLE_KINDS = tuple(CHUNK_LABELS)


def default_index_path() -> Path:
    return project_paths.TRANSCRIPTS.parent / ".state" / INDEX_FILENAME


def _normalized(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _require_session(session: str) -> str:
    if not SESSION_PATTERN.fullmatch(session):
        raise ValueError(f"Некоректний session ID: {session!r}")
    return session


def _prepare_database_file(path: Path) -> None:
    ensure_private_dir(path.parent)
    if path.is_symlink():
        raise RuntimeError(
            f"Пошуковий індекс не може бути symlink: {path}"
        )
    if not path.exists():
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(descriptor)
    path.chmod(0o600)


def _connect(path: Path | None = None) -> sqlite3.Connection:
    index_path = path or default_index_path()
    _prepare_database_file(index_path)
    connection = sqlite3.connect(index_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = DELETE")
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version not in {0, SCHEMA_VERSION}:
        connection.close()
        raise RuntimeError(
            f"Непідтримувана версія пошукового індексу: {version}; "
            "запустіть команду rebuild"
        )
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS meetings (
            id INTEGER PRIMARY KEY,
            session TEXT NOT NULL UNIQUE,
            meeting_date TEXT NOT NULL DEFAULT '',
            meeting_time TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            meeting_type TEXT NOT NULL DEFAULT '',
            quality TEXT NOT NULL DEFAULT '',
            participants_json TEXT NOT NULL DEFAULT '[]',
            note_path TEXT NOT NULL,
            transcript_path TEXT NOT NULL DEFAULT '',
            evidence_path TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL,
            indexed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS participants (
            meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            PRIMARY KEY (meeting_id, normalized_name)
        );

        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            kind TEXT NOT NULL,
            speaker TEXT NOT NULL DEFAULT '',
            owners TEXT NOT NULL DEFAULT '',
            owners_normalized TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            deadline TEXT NOT NULL DEFAULT '',
            timestamp TEXT NOT NULL DEFAULT '',
            source_line INTEGER NOT NULL DEFAULT 0,
            text TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS chunks_meeting_idx
            ON chunks(meeting_id, ordinal);
        CREATE INDEX IF NOT EXISTS chunks_kind_status_idx
            ON chunks(kind, status);
        CREATE INDEX IF NOT EXISTS participants_name_idx
            ON participants(normalized_name);

        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            text,
            speaker,
            owners,
            content='chunks',
            content_rowid='id',
            tokenize='unicode61 remove_diacritics 2'
        );

        CREATE TRIGGER IF NOT EXISTS chunks_after_insert AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, text, speaker, owners)
            VALUES (new.id, new.text, new.speaker, new.owners);
        END;
        CREATE TRIGGER IF NOT EXISTS chunks_after_delete AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, text, speaker, owners)
            VALUES ('delete', old.id, old.text, old.speaker, old.owners);
        END;
        CREATE TRIGGER IF NOT EXISTS chunks_after_update AFTER UPDATE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, text, speaker, owners)
            VALUES ('delete', old.id, old.text, old.speaker, old.owners);
            INSERT INTO chunks_fts(rowid, text, speaker, owners)
            VALUES (new.id, new.text, new.speaker, new.owners);
        END;
    """)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    connection.commit()
    index_path.chmod(0o600)
    return connection


def _metadata_value(note_text: str, label: str) -> str:
    match = re.search(
        rf"(?m)^-\s+\*\*{re.escape(label)}:\*\*\s*(.*?)\s*$",
        note_text,
    )
    return _clean_text(match.group(1)) if match else ""


def _note_title(note_text: str, session: str) -> str:
    match = re.search(r"(?m)^#\s+(.+?)\s*$", note_text)
    return _clean_text(match.group(1)) if match else session


def _participants(note_text: str) -> list[str]:
    lines = note_text.splitlines()
    start = next((
        index + 1 for index, line in enumerate(lines)
        if line.strip() in {"**Присутні:**", "**Учасники:**"}
    ), None)
    if start is None:
        return []
    participants: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            if participants:
                break
            continue
        if stripped.startswith("##") or not stripped.startswith("-"):
            break
        participant = _clean_text(stripped.removeprefix("-"))
        if participant and participant != "—":
            participants.append(participant)
    return list(dict.fromkeys(participants))


def _markdown_section(note_text: str, heading: str) -> str:
    match = re.search(
        rf"(?ms)^{re.escape(heading)}\s*\n(.*?)(?=^##\s|^---\s*$|\Z)",
        note_text,
    )
    if not match:
        return ""
    lines = [
        line.strip().removeprefix("- ")
        for line in match.group(1).splitlines()
        if line.strip() and line.strip() != "- —"
    ]
    return _clean_text(" ".join(lines))


def _content_hash(
    note_text: str, transcript_text: str, evidence: dict[str, Any]
) -> str:
    payload = "\0".join((
        str(INDEX_CONTENT_VERSION),
        note_text,
        transcript_text,
        json.dumps(evidence, ensure_ascii=False, sort_keys=True),
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _evidence_chunks(evidence: dict[str, Any]) -> Iterable[dict[str, Any]]:
    items = evidence.get("items")
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        claim = _clean_text(item.get("claim"))
        kind = _clean_text(item.get("type"))
        if not claim or kind not in CHUNK_LABELS:
            continue
        owners_value = item.get("owners")
        owners = [
            _clean_text(owner) for owner in owners_value
            if _clean_text(owner)
        ] if isinstance(owners_value, list) else []
        proofs = [
            proof for proof in item.get("evidence", [])
            if isinstance(proof, dict)
        ]
        timestamps = [
            _clean_text(proof.get("source_timestamp") or proof.get("timestamp"))
            for proof in proofs
            if _clean_text(proof.get("source_timestamp") or proof.get("timestamp"))
        ]
        source_lines = [
            int(proof.get("source_line", 0) or 0)
            for proof in proofs
            if str(proof.get("source_line", "")).isdigit()
        ]
        yield {
            "kind": kind,
            "speaker": _clean_text(item.get("speaker")),
            "owners": " / ".join(dict.fromkeys(owners)),
            "owners_normalized": " | ".join(
                dict.fromkeys(_normalized(owner) for owner in owners)
            ),
            "status": _clean_text(item.get("status")),
            "deadline": _clean_text(item.get("deadline")),
            "timestamp": timestamps[0] if timestamps else "",
            "source_line": min(source_lines) if source_lines else 0,
            "text": claim,
        }


def _transcript_chunks(transcript_text: str) -> Iterable[dict[str, Any]]:
    for line_number, line in enumerate(transcript_text.splitlines(), start=1):
        match = TRANSCRIPT_LINE.match(line.strip())
        if not match:
            continue
        timestamp, speaker, text = match.groups()
        text = _clean_text(text)
        if not text:
            continue
        kind = "chat" if speaker.endswith(" (chat)") else "transcript"
        yield {
            "kind": kind,
            "speaker": speaker.removesuffix(" (chat)").strip(),
            "owners": "",
            "owners_normalized": "",
            "status": "",
            "deadline": "",
            "timestamp": timestamp.strip(),
            "source_line": line_number,
            "text": text,
        }


def _note_for_session(session: str) -> Path | None:
    matches = list(project_paths.NOTES.glob(f"{session}*.md"))
    return max(matches, key=lambda path: path.stat().st_mtime_ns, default=None)


def _session_from_note(path: Path) -> str:
    session = path.stem.partition(" — ")[0]
    return _require_session(session)


def index_session(
    session: str,
    *,
    note_path: Path | None = None,
    db_path: Path | None = None,
    connection: sqlite3.Connection | None = None,
) -> bool:
    """Upsert one completed meeting. Returns False when nothing changed."""
    session = _require_session(session)
    note = note_path or _note_for_session(session)
    if note is None or not note.is_file():
        raise FileNotFoundError(f"Немає готової нотатки для {session}")
    transcript = project_paths.TRANSCRIPTS / f"{session}.md"
    evidence_path = project_paths.TRANSCRIPTS / session / "summary-evidence.json"
    note_text = note.read_text(encoding="utf-8")
    transcript_text = (
        transcript.read_text(encoding="utf-8") if transcript.is_file() else ""
    )
    evidence = read_json(evidence_path, {}) or {}
    if not isinstance(evidence, dict):
        evidence = {}
    fingerprint = _content_hash(note_text, transcript_text, evidence)
    owns_connection = connection is None
    database = connection or _connect(db_path)
    try:
        existing = database.execute(
            "SELECT content_hash FROM meetings WHERE session = ?", (session,)
        ).fetchone()
        if existing and existing["content_hash"] == fingerprint:
            return False

        participants = _participants(note_text)
        title = _note_title(note_text, session)
        meeting_date = _metadata_value(note_text, "Дата") or session[:10]
        meeting_time = _metadata_value(note_text, "Час")
        meeting_type = _metadata_value(note_text, "Тип зустрічі")
        quality = _metadata_value(note_text, "Якість")
        chunks: list[dict[str, Any]] = [{
            "kind": "title",
            "speaker": "",
            "owners": "",
            "owners_normalized": "",
            "status": "",
            "deadline": "",
            "timestamp": "",
            "source_line": 0,
            "text": title,
        }]
        tldr = _markdown_section(note_text, "## TL;DR")
        if tldr:
            chunks.append({
                "kind": "tldr",
                "speaker": "",
                "owners": "",
                "owners_normalized": "",
                "status": "",
                "deadline": "",
                "timestamp": "",
                "source_line": 0,
                "text": tldr,
            })
        chunks.extend(_evidence_chunks(evidence))
        chunks.extend(_transcript_chunks(transcript_text))

        with database:
            database.execute("DELETE FROM meetings WHERE session = ?", (session,))
            cursor = database.execute("""
                INSERT INTO meetings (
                    session, meeting_date, meeting_time, title, meeting_type,
                    quality, participants_json, note_path, transcript_path,
                    evidence_path, content_hash, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                session,
                meeting_date,
                meeting_time,
                title,
                meeting_type,
                quality,
                json.dumps(participants, ensure_ascii=False),
                str(note.resolve()),
                str(transcript.resolve()) if transcript.is_file() else "",
                str(evidence_path.resolve()) if evidence_path.is_file() else "",
                fingerprint,
                utc_now(),
            ))
            meeting_id = int(cursor.lastrowid)
            database.executemany(
                "INSERT INTO participants (meeting_id, name, normalized_name) "
                "VALUES (?, ?, ?)",
                [
                    (meeting_id, participant, _normalized(participant))
                    for participant in participants
                ],
            )
            database.executemany("""
                INSERT INTO chunks (
                    meeting_id, ordinal, kind, speaker, owners,
                    owners_normalized, status, deadline, timestamp,
                    source_line, text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (
                    meeting_id,
                    ordinal,
                    chunk["kind"],
                    chunk["speaker"],
                    chunk["owners"],
                    chunk["owners_normalized"],
                    chunk["status"],
                    chunk["deadline"],
                    chunk["timestamp"],
                    chunk["source_line"],
                    chunk["text"],
                )
                for ordinal, chunk in enumerate(chunks)
            ])
        return True
    finally:
        if owns_connection:
            database.close()


def _indexable_notes() -> dict[str, Path]:
    selected: dict[str, Path] = {}
    if not project_paths.NOTES.is_dir():
        return selected
    for note in project_paths.NOTES.glob("*.md"):
        try:
            session = _session_from_note(note)
        except ValueError:
            continue
        previous = selected.get(session)
        if previous is None or note.stat().st_mtime_ns > previous.stat().st_mtime_ns:
            selected[session] = note
    return selected


def index_all(*, db_path: Path | None = None) -> dict[str, int]:
    notes = _indexable_notes()
    database = _connect(db_path)
    indexed = unchanged = 0
    try:
        for session, note in sorted(notes.items()):
            if index_session(session, note_path=note, connection=database):
                indexed += 1
            else:
                unchanged += 1
        stale = [
            row["session"] for row in database.execute("SELECT session FROM meetings")
            if row["session"] not in notes
        ]
        with database:
            database.executemany(
                "DELETE FROM meetings WHERE session = ?",
                [(session,) for session in stale],
            )
        return {"indexed": indexed, "unchanged": unchanged, "removed": len(stale)}
    finally:
        database.close()


def rebuild_index(*, db_path: Path | None = None) -> dict[str, int]:
    index_path = db_path or default_index_path()
    ensure_private_dir(index_path.parent)
    temporary = index_path.with_name(f".{index_path.name}.rebuild-{os.getpid()}")
    if temporary.exists():
        raise FileExistsError(f"Тимчасовий індекс уже існує: {temporary}")
    try:
        result = index_all(db_path=temporary)
        os.replace(temporary, index_path)
        index_path.chmod(0o600)
        return result
    finally:
        temporary.unlink(missing_ok=True)


def _fts_terms(query: str) -> list[str]:
    tokens = [_normalized(token) for token in WORD_PATTERN.findall(query)]
    meaningful = [token for token in tokens if token not in SEARCH_STOPWORDS]
    return list(dict.fromkeys(meaningful or tokens))


def _fts_query(terms: list[str], operator: str = "AND") -> str:
    safe_terms = [
        f'"{term.replace(chr(34), chr(34) * 2)}"*' if len(term) >= 4
        else f'"{term.replace(chr(34), chr(34) * 2)}"'
        for term in terms
    ]
    return f" {operator} ".join(safe_terms)


def search(
    query: str = "",
    *,
    db_path: Path | None = None,
    participants: list[str] | None = None,
    owner: str = "",
    kinds: list[str] | None = None,
    status: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = 20,
) -> list[dict[str, Any]]:
    if limit < 1 or limit > 200:
        raise ValueError("limit має бути в межах 1–200")
    invalid_kinds = set(kinds or []) - set(SEARCHABLE_KINDS)
    if invalid_kinds:
        raise ValueError(f"Невідомі типи фрагментів: {sorted(invalid_kinds)}")
    database = _connect(db_path)
    try:
        filters: list[str] = []
        filter_parameters: list[Any] = []
        for participant in participants or []:
            filters.append("""
                EXISTS (
                    SELECT 1 FROM participants p
                    WHERE p.meeting_id = m.id
                    AND p.normalized_name LIKE ? ESCAPE '\\'
                )
            """)
            filter_parameters.append(f"%{_like_value(_normalized(participant))}%")
        if owner:
            filters.append("c.owners_normalized LIKE ? ESCAPE '\\'")
            filter_parameters.append(f"%{_like_value(_normalized(owner))}%")
        if kinds:
            placeholders = ", ".join("?" for _ in kinds)
            filters.append(f"c.kind IN ({placeholders})")
            filter_parameters.extend(kinds)
        if status:
            filters.append("c.status = ?")
            filter_parameters.append(status)
        if date_from:
            filters.append("m.meeting_date >= ?")
            filter_parameters.append(date_from)
        if date_to:
            filters.append("m.meeting_date <= ?")
            filter_parameters.append(date_to)
        where_suffix = " AND " + " AND ".join(filters) if filters else ""

        terms = _fts_terms(query)
        if terms:
            statement = f"""
                SELECT
                    m.session, m.meeting_date, m.meeting_time, m.title,
                    m.meeting_type, m.quality, m.participants_json, m.note_path,
                    c.kind, c.speaker, c.owners, c.status, c.deadline,
                    c.timestamp, c.source_line, c.text,
                    snippet(chunks_fts, 0, '«', '»', ' … ', 24) AS snippet,
                    bm25(chunks_fts, 1.0, 0.7, 0.8) +
                        CASE c.kind
                            WHEN 'decision' THEN -0.8
                            WHEN 'commitment' THEN -0.6
                            WHEN 'open_question' THEN -0.4
                            WHEN 'tldr' THEN -0.2
                            ELSE 0
                        END AS score
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                JOIN meetings m ON m.id = c.meeting_id
                WHERE chunks_fts MATCH ?{where_suffix}
                ORDER BY score ASC, m.meeting_date DESC, c.ordinal ASC
                LIMIT ?
            """

            def execute(operator: str) -> list[sqlite3.Row]:
                return list(database.execute(
                    statement,
                    [_fts_query(terms, operator), *filter_parameters, limit],
                ))

            rows = execute("AND")
            if not rows and len(terms) > 1:
                rows = execute("OR")
        else:
            statement = f"""
                SELECT
                    m.session, m.meeting_date, m.meeting_time, m.title,
                    m.meeting_type, m.quality, m.participants_json, m.note_path,
                    c.kind, c.speaker, c.owners, c.status, c.deadline,
                    c.timestamp, c.source_line, c.text,
                    CASE WHEN length(c.text) > 240
                        THEN substr(c.text, 1, 237) || '…'
                        ELSE c.text END AS snippet,
                    0 AS score
                FROM chunks c
                JOIN meetings m ON m.id = c.meeting_id
                WHERE 1 = 1{where_suffix}
                ORDER BY m.meeting_date DESC, m.meeting_time DESC, c.ordinal ASC
                LIMIT ?
            """
            rows = list(database.execute(
                statement, [*filter_parameters, limit]
            ))
        return [_search_result(row) for row in rows]
    finally:
        database.close()


def _like_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search_result(row: sqlite3.Row) -> dict[str, Any]:
    try:
        participants = json.loads(row["participants_json"])
    except (json.JSONDecodeError, TypeError):
        participants = []
    return {
        "session": row["session"],
        "date": row["meeting_date"],
        "time": row["meeting_time"],
        "title": row["title"],
        "meeting_type": row["meeting_type"],
        "quality": row["quality"],
        "participants": participants,
        "note_path": row["note_path"],
        "kind": row["kind"],
        "kind_label": CHUNK_LABELS.get(row["kind"], row["kind"]),
        "speaker": row["speaker"],
        "owners": row["owners"],
        "status": row["status"],
        "deadline": row["deadline"],
        "timestamp": row["timestamp"],
        "source_line": row["source_line"],
        "text": row["text"],
        "snippet": _clean_text(row["snippet"]),
        "score": round(float(row["score"]), 6),
    }


def _print_results(results: list[dict[str, Any]]) -> None:
    if not results:
        print("Нічого не знайдено.")
        return
    for index, result in enumerate(results, start=1):
        date = " ".join(
            value for value in (result["date"], result["time"]) if value
        )
        context = " · ".join(
            value for value in (
                result["kind_label"],
                result["speaker"] or result["owners"],
                result["timestamp"],
            ) if value
        )
        print(f"{index}. {date} — {result['title']}")
        if context:
            print(f"   {context}")
        print(f"   {result['snippet']}")
        print(f"   {result['note_path']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="нестандартний шлях до індексу",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser("index", help="оновити індекс")
    index_parser.add_argument("session", nargs="?")
    subparsers.add_parser("rebuild", help="перебудувати індекс з нуля")

    search_parser = subparsers.add_parser("search", help="знайти зустрічі")
    search_parser.add_argument("query", nargs="?", default="")
    search_parser.add_argument("--participant", action="append", default=[])
    search_parser.add_argument("--owner", default="")
    search_parser.add_argument(
        "--kind", action="append", choices=SEARCHABLE_KINDS, default=[]
    )
    search_parser.add_argument("--status", default="")
    search_parser.add_argument("--from", dest="date_from", default="")
    search_parser.add_argument("--to", dest="date_to", default="")
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.add_argument("--json", action="store_true")

    args = parser.parse_args()
    if args.command == "index":
        if args.session:
            changed = index_session(args.session, db_path=args.db)
            print("Проіндексовано: 1" if changed else "Змін немає.")
        else:
            result = index_all(db_path=args.db)
            print(
                f"Проіндексовано: {result['indexed']}; "
                f"без змін: {result['unchanged']}; "
                f"видалено застарілих: {result['removed']}."
            )
        return
    if args.command == "rebuild":
        result = rebuild_index(db_path=args.db)
        print(f"Індекс перебудовано, зустрічей: {result['indexed']}.")
        return
    results = search(
        args.query,
        db_path=args.db,
        participants=args.participant,
        owner=args.owner,
        kinds=args.kind,
        status=args.status,
        date_from=args.date_from,
        date_to=args.date_to,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        _print_results(results)


if __name__ == "__main__":
    main()
