import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import audio_pipeline
import meeting_search


class MeetingSearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.notes = self.root / "notes"
        self.transcripts = self.root / "transcripts"
        self.notes.mkdir()
        self.transcripts.mkdir()
        self.db = self.root / ".state" / "meeting-search.sqlite3"
        self.path_patches = [
            mock.patch.object(meeting_search.project_paths, "NOTES", self.notes),
            mock.patch.object(
                meeting_search.project_paths, "TRANSCRIPTS", self.transcripts
            ),
        ]
        for patcher in self.path_patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.path_patches):
            patcher.stop()
        self.temporary.cleanup()

    def _meeting(
        self,
        session="2026-08-12_10-31-27_meet-fwv-mugk-zje",
        *,
        title="Пріоритети BI та HR-аналітики",
        participant="Olesia Khabliuk",
        claim=(
            "Використовувати Event Router лише там, де він дає користь"
        ),
    ):
        note = self.notes / f"{session} — {title}.md"
        note.write_text(
            "\n".join([
                f"# {title}",
                "",
                "- **Дата:** 2026-08-12",
                "- **Час:** 10:31:27 CEST",
                "- **Назва зустрічі:** Meet - OK // OP weekly",
                "- **Тип зустрічі:** Планування",
                "- **Якість:** Висока",
                "",
                "**Присутні:**",
                "- Oleh Parandii",
                f"- {participant}",
                "",
                "## TL;DR",
                f"Команда визначила правило для Event Router: {claim}.",
                "",
                "## Основні тези",
                "- Обговорили архітектуру.",
                "",
                "---",
                "",
                "## Повний транскрипт",
            ]) + "\n",
            encoding="utf-8",
        )
        transcript = self.transcripts / f"{session}.md"
        transcript.write_text(
            "\n".join([
                "# Meet - OK // OP weekly",
                (
                    "[11:15:00] Oleh Parandii: Не переносимо всі процеси "
                    "в Event Router."
                ),
                (
                    f"[11:15:05] {participant}: Погоджуюсь, використовуємо "
                    "його лише там, де це корисно."
                ),
                (
                    f"[11:16:00] {participant}: Я підготую список "
                    "процесів завтра."
                ),
            ]) + "\n",
            encoding="utf-8",
        )
        work = self.transcripts / session
        work.mkdir()
        evidence = {
            "items": [
                {
                    "type": "decision",
                    "claim": claim,
                    "speaker": f"Oleh Parandii / {participant}",
                    "owners": [],
                    "deadline": "",
                    "status": "active",
                    "evidence": [{
                        "source_timestamp": "11:15:05",
                        "source_line": 3,
                        "quote": (
                            "Погоджуюсь, використовуємо його лише там, "
                            "де це корисно"
                        ),
                    }],
                },
                {
                    "type": "commitment",
                    "claim": "Підготувати список процесів",
                    "speaker": participant,
                    "owners": [participant],
                    "deadline": "завтра",
                    "status": "open",
                    "evidence": [{
                        "source_timestamp": "11:16:00",
                        "source_line": 4,
                        "quote": "Я підготую список процесів завтра",
                    }],
                },
            ]
        }
        (work / "summary-evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False), encoding="utf-8"
        )
        return note, evidence

    def test_indexes_and_searches_structured_and_transcript_content(self):
        note, _ = self._meeting()
        session = meeting_search._session_from_note(note)
        self.assertTrue(meeting_search.index_session(session, db_path=self.db))
        self.assertFalse(meeting_search.index_session(session, db_path=self.db))

        results = meeting_search.search(
            "Що вирішили про Event Router?", db_path=self.db
        )
        self.assertTrue(results)
        self.assertEqual(results[0]["session"], session)
        self.assertTrue(any(result["kind"] == "decision" for result in results))
        self.assertTrue(any("«Event»" in result["snippet"] for result in results))
        self.assertEqual(results[0]["note_path"], str(note.resolve()))

    def test_filters_by_participant_owner_kind_status_and_date(self):
        note, _ = self._meeting()
        session = meeting_search._session_from_note(note)
        meeting_search.index_session(session, db_path=self.db)

        results = meeting_search.search(
            "",
            db_path=self.db,
            participants=["Olesia"],
            owner="Olesia",
            kinds=["commitment"],
            status="open",
            date_from="2026-08-01",
            date_to="2026-08-31",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["text"], "Підготувати список процесів")
        self.assertEqual(results[0]["deadline"], "завтра")
        self.assertEqual(results[0]["owners"], "Olesia Khabliuk")
        self.assertEqual(
            meeting_search.search(
                "Event Router", db_path=self.db, participants=["Max"]
            ),
            [],
        )

    def test_changed_evidence_replaces_session_without_duplicates(self):
        note, evidence = self._meeting()
        session = meeting_search._session_from_note(note)
        meeting_search.index_session(session, db_path=self.db)
        evidence["items"][0]["claim"] = (
            "Не переносити стабільні процеси в Event Router"
        )
        evidence_path = self.transcripts / session / "summary-evidence.json"
        evidence_path.write_text(
            json.dumps(evidence, ensure_ascii=False), encoding="utf-8"
        )
        self.assertTrue(meeting_search.index_session(session, db_path=self.db))

        connection = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM meetings").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM chunks WHERE kind = 'decision'"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()
        results = meeting_search.search("стабільні процеси", db_path=self.db)
        self.assertEqual(results[0]["text"], evidence["items"][0]["claim"])

    def test_rebuild_is_atomic_and_incremental_index_removes_stale_notes(self):
        first, _ = self._meeting()
        second, _ = self._meeting(
            "2026-08-13_09-00-00_meet-abc-defg-hij",
            title="План BI",
            participant="Max Doe",
            claim="Оновити BI план",
        )
        result = meeting_search.rebuild_index(db_path=self.db)
        self.assertEqual(result, {"indexed": 2, "unchanged": 0, "removed": 0})
        self.assertEqual(
            {
                item["session"]
                for item in meeting_search.search("BI", db_path=self.db)
            },
            {
                meeting_search._session_from_note(first),
                meeting_search._session_from_note(second),
            },
        )

        second.unlink()
        result = meeting_search.index_all(db_path=self.db)
        self.assertEqual(result["removed"], 1)
        self.assertEqual(
            {item["session"] for item in meeting_search.search("BI", db_path=self.db)},
            {meeting_search._session_from_note(first)},
        )

    def test_note_indexing_failure_is_non_blocking(self):
        note, _ = self._meeting()
        with mock.patch.object(
            meeting_search, "index_session", side_effect=sqlite3.DatabaseError("bad")
        ), mock.patch.object(audio_pipeline, "log") as log:
            audio_pipeline._index_meeting_note(
                "2026-08-12_10-31-27_meet-fwv-mugk-zje", note
            )
        log.assert_called_once()
        self.assertIn(
            "Пошуковий індекс не оновлено", log.call_args.args[0]
        )


if __name__ == "__main__":
    unittest.main()
