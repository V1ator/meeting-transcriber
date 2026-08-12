from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import meet_import
import pipeline_utils
import audio_pipeline as audio
import meet_pipeline as meet


def sample_export() -> dict:
    return {
        "schemaVersion": 1,
        "source": "google-meet-live-captions",
        "meetingCode": "abc-defg-hij",
        "meetingTitle": "Планування релізу",
        "participants": ["Інтерв’юер", "Анна", "Марія"],
        "language": "uk",
        "startedAt": "2026-07-28T08:00:00.000Z",
        "endedAt": "2026-07-28T08:30:00.000Z",
        "entries": [
            {
                "speaker": "Інтерв’юер",
                "text": "Починаємо перевірку",
                "startMs": 1_000,
                "endMs": 2_000,
            },
            {
                "speaker": "Інтерв’юер",
                "text": "Починаємо перевірку розширення",
                "startMs": 2_100,
                "endMs": 3_000,
            },
            {
                "speaker": "Анна",
                "text": "Працює",
                "startMs": 5_000,
                "endMs": 5_500,
            },
        ],
    }


class MeetImportTests(unittest.TestCase):
    def test_rejects_oversized_normalized_text(self):
        data = sample_export()
        data["entries"] = [{
            "speaker": "Participant",
            "text": "x" * meet_import.MAX_TEXT_CHARS,
            "startMs": index,
            "endMs": index,
        } for index in range(meet_import.MAX_TOTAL_TEXT_CHARS // meet_import.MAX_TEXT_CHARS + 1)]
        with self.assertRaisesRegex(meet_import.MeetImportError, "Забагато тексту"):
            meet_import.validate_export(data)

    def test_fuzzy_matching_skips_excessive_token_sequences(self):
        left = " ".join(f"word{index}" for index in range(meet_import.MAX_FUZZY_TOKENS + 1))
        right = left + " extra"
        with mock.patch.object(meet_import, "_lcs_length") as lcs:
            self.assertIsNone(meet_import._fuzzy_expansion(left, right))
        lcs.assert_not_called()

    def test_normalization_has_a_wall_clock_budget(self):
        data = sample_export()
        with mock.patch.object(
            meet_import.time, "monotonic",
            side_effect=[0.0, meet_import.NORMALIZE_TIMEOUT_SECONDS + 1],
        ):
            with self.assertRaisesRegex(meet_import.MeetImportError, "ліміт часу"):
                meet_import.normalized_entries(data)

    def test_lcs_checks_deadline_during_expensive_comparison(self):
        tokens = [f"word{index}" for index in range(meet_import.MAX_FUZZY_TOKENS)]
        with mock.patch.object(
            meet_import.time,
            "monotonic",
            side_effect=[0.0, meet_import.NORMALIZE_TIMEOUT_SECONDS + 1],
        ):
            with self.assertRaisesRegex(meet_import.MeetImportError, "ліміт часу"):
                meet_import._lcs_length(
                    tokens, tokens, deadline=meet_import.NORMALIZE_TIMEOUT_SECONDS
                )

    def test_normalizes_incremental_caption_updates(self):
        data = meet_import.validate_export(sample_export())
        entries = meet_import.normalized_entries(data)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["text"], "Починаємо перевірку розширення")
        self.assertEqual(entries[1]["speaker"], "Анна")

    def test_out_of_order_entries_are_sorted_before_replay_grouping(self):
        data = sample_export()
        data["entries"] = [
            {"speaker": "Анна", "text": "Третя репліка", "startMs": 52_000, "endMs": 53_000},
            {"speaker": "Анна", "text": "Перша репліка", "startMs": 0, "endMs": 1_000},
            {"speaker": "Ігор", "text": "Друга репліка", "startMs": 10_000, "endMs": 11_000},
        ]
        entries = meet_import.normalized_entries(data)
        self.assertEqual(
            [item["start_ms"] for item in entries], [0, 10_000, 52_000]
        )

    def test_markdown_starts_with_meeting_metadata_and_participants(self):
        data = sample_export()
        entries = meet_import.normalized_entries(data)
        markdown = meet_import.render_markdown(data, entries)
        self.assertTrue(markdown.startswith("# Планування релізу\n"))
        self.assertIn("**Час початку:**", markdown)
        self.assertIn("**Назва зустрічі:** Планування релізу", markdown)
        self.assertIn("**Учасники:**\n- Інтерв’юер\n- Анна\n- Марія", markdown)
        self.assertIn("## Транскрипт", markdown)

    def test_removes_long_replays_and_cleans_speaker_suffix(self):
        data = sample_export()
        long_text = (
            "Це довга завершена репліка, яку Google Meet повторно показав "
            "після повної перебудови DOM captions."
        )
        data["entries"] = [
            {
                "speaker": "Інтерв’юер & 5 others",
                "text": long_text,
                "startMs": 1_000,
                "endMs": 2_000,
            },
            {
                "speaker": "Інтерв’юер & 5 others",
                "text": long_text,
                "startMs": 240_000,
                "endMs": 241_000,
            },
        ]
        entries = meet_import.normalized_entries(data)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["speaker"], "Інтерв’юер")

    def test_replay_batch_removes_its_short_historical_entries(self):
        data = sample_export()
        long_text = (
            "Це довга завершена репліка, яка дає змогу визначити пакетне "
            "відтворення старого DOM у Google Meet."
        )
        data["entries"] = [
            {"speaker": "Анна", "text": "Всім привіт", "startMs": 500, "endMs": 700},
            {"speaker": "Інтерв’юер", "text": long_text, "startMs": 1_000, "endMs": 2_000},
            {
                "speaker": "Анна",
                "text": "Всім привіт",
                "startMs": 90_000,
                "endMs": 90_000,
            },
            {
                "speaker": "Інтерв’юер",
                "text": long_text,
                "startMs": 90_000,
                "endMs": 91_000,
            },
        ]
        entries = meet_import.normalized_entries(data)
        self.assertEqual(len(entries), 2)

    def test_merges_partial_caption_after_one_minute_and_keeps_short_replies(self):
        data = sample_export()
        data["entries"] = [
            {
                "speaker": "Анна",
                "text": "Починаємо великий тест",
                "startMs": 1_000,
                "endMs": 2_000,
            },
            {
                "speaker": "Анна",
                "text": "Починаємо великий тест локальної транскрипції",
                "startMs": 62_000,
                "endMs": 63_000,
            },
            {
                "speaker": "Анна",
                "text": "Так",
                "startMs": 70_000,
                "endMs": 70_500,
            },
            {
                "speaker": "Анна",
                "text": "Так",
                "startMs": 80_000,
                "endMs": 80_500,
            },
        ]
        entries = meet_import.normalized_entries(data)
        self.assertEqual(len(entries), 3)
        self.assertEqual(
            entries[0]["text"],
            "Починаємо великий тест локальної транскрипції",
        )

    def test_merges_near_immediate_long_replay_with_a_corrected_word(self):
        data = sample_export()
        partial = (
            "У нас є сирі сорси і білдер який збирає таблицю з актуальними "
            "даними для кривих сезонності і метрики розрізі"
        )
        expanded = (
            "У нас є сирі сорси і білдер який збирає таблицю з актуальними "
            "даними для кривих сезонності і метрики в розрізі а далі "
            "розраховуються прогнози"
        )
        data["entries"] = [
            {
                "speaker": "Ігор",
                "text": partial,
                "startMs": 1_000,
                "endMs": 10_000,
            },
            {
                "speaker": "Ігор",
                "text": expanded,
                "startMs": 10_100,
                "endMs": 15_000,
            },
        ]
        entries = meet_import.normalized_entries(data)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["text"], expanded)
        self.assertEqual(entries[0]["start_ms"], 1_000)
        self.assertEqual(entries[0]["end_ms"], 15_000)

    def test_keeps_fuzzy_long_captions_outside_tight_window_separate(self):
        data = sample_export()
        data["entries"] = [
            {
                "speaker": "Ігор",
                "text": (
                    "Ми перевіряємо довгу аналітичну репліку про структуру "
                    "метрик та налаштування майбутнього дашборду"
                ),
                "startMs": 1_000,
                "endMs": 2_000,
            },
            {
                "speaker": "Ігор",
                "text": (
                    "Ми перевіряємо довгу аналітичну репліку про структуру "
                    "нових метрик та налаштування майбутнього дашборду окремо"
                ),
                "startMs": 6_001,
                "endMs": 8_000,
            },
        ]
        entries = meet_import.normalized_entries(data)
        self.assertEqual(len(entries), 2)

    def test_drops_recent_rtc_replays_but_keeps_short_replies(self):
        data = sample_export()
        data["entries"] = [
            {
                "speaker": "Інтерв’юер",
                "text": "Перевіряємо новий локальний режим транскрипції",
                "startMs": 1_000,
                "endMs": 4_000,
            },
            {
                "speaker": "Інтерв’юер",
                "text": "Після цього переходимо до наступного питання",
                "startMs": 4_100,
                "endMs": 7_000,
            },
            {
                "speaker": "Інтерв’юер",
                "text": "Перевіряємо новий локальний режим транскрипції",
                "startMs": 6_900,
                "endMs": 7_100,
            },
            {
                "speaker": "Інтерв’юер",
                "text": "Після цього переходимо до наступного питання",
                "startMs": 6_950,
                "endMs": 7_100,
            },
            {"speaker": "Анна", "text": "Так", "startMs": 8_000, "endMs": 8_200},
            {"speaker": "Анна", "text": "Так", "startMs": 9_000, "endMs": 9_200},
        ]
        entries = meet_import.normalized_entries(data)
        self.assertEqual(len(entries), 3)
        self.assertEqual(
            entries[0]["text"],
            "Перевіряємо новий локальний режим транскрипції "
            "Після цього переходимо до наступного питання",
        )
        self.assertEqual([entry["text"] for entry in entries[1:]], ["Так", "Так"])

    def test_replaces_a_short_corrected_caption(self):
        data = sample_export()
        data["entries"] = [
            {
                "speaker": "Інтерв’юер",
                "text": "здається знайшов не знаюто справа добре",
                "startMs": 1_000,
                "endMs": 3_000,
            },
            {
                "speaker": "Інтерв’юер",
                "text": "дякую як в тебе",
                "startMs": 2_900,
                "endMs": 4_000,
            },
            {
                "speaker": "Інтерв’юер",
                "text": "здається знайшов не знаю справа добре добре",
                "startMs": 4_100,
                "endMs": 5_000,
            },
        ]
        entries = meet_import.normalized_entries(data)
        self.assertEqual(len(entries), 1)
        self.assertEqual(
            entries[0]["text"],
            "здається знайшов не знаю справа добре добре дякую як в тебе",
        )

    def test_assembles_caption_fragments_until_the_speaker_changes(self):
        data = sample_export()
        data["entries"] = [
            {
                "speaker": "Інтерв’юер",
                "text": "аналітичному інже",
                "startMs": 1_000,
                "endMs": 3_000,
            },
            {
                "speaker": "Інтерв’юер",
                "text": "інженеру сьогодні був цікавий кандидат",
                "startMs": 3_100,
                "endMs": 5_000,
            },
            {
                "speaker": "Анна",
                "text": "Зрозуміло",
                "startMs": 5_200,
                "endMs": 6_000,
            },
            {
                "speaker": "Інтерв’юер",
                "text": "Продовжимо окремо",
                "startMs": 6_100,
                "endMs": 7_000,
            },
        ]
        entries = meet_import.normalized_entries(data)
        self.assertEqual(len(entries), 3)
        self.assertEqual(
            entries[0]["text"],
            "аналітичному інженеру сьогодні був цікавий кандидат",
        )
        self.assertEqual([entry["speaker"] for entry in entries], ["Інтерв’юер", "Анна", "Інтерв’юер"])

    def test_chat_is_deduplicated_and_labeled_in_markdown(self):
        data = sample_export()
        data["entries"] = [
            {
                "speaker": "Марія",
                "text": "Питання з чату",
                "kind": "chat",
                "startMs": 1_000,
                "endMs": 1_000,
            },
            {
                "speaker": "Марія",
                "text": "Питання з чату",
                "kind": "chat",
                "startMs": 30_000,
                "endMs": 30_000,
            },
        ]
        entries = meet_import.normalized_entries(data)
        self.assertEqual(len(entries), 1)
        markdown = meet_import.render_markdown(data, entries)
        self.assertIn("Марія (chat): Питання з чату", markdown)

    def test_rejects_unknown_source(self):
        data = sample_export()
        data["source"] = "untrusted"
        with self.assertRaisesRegex(ValueError, "Невідоме джерело"):
            meet_import.validate_export(data)

    def test_imports_markdown_without_models(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "export.json"
            source.write_text(json.dumps(sample_export()), encoding="utf-8")
            transcripts = root / "transcripts"
            with mock.patch.object(meet_import.project_paths, "TRANSCRIPTS", transcripts):
                result = meet_import.import_export(source, summarize=False)
            text = result.read_text(encoding="utf-8")
            self.assertIn("Інтерв’юер: Починаємо перевірку розширення", text)
            self.assertIn("Анна: Працює", text)
            manifest = json.loads(
                (transcripts / result.stem / "manifest.json").read_text()
            )
            self.assertEqual(manifest["source"], "google-meet-live-captions")
            self.assertEqual(manifest["quality"]["segments"], 2)

    def test_summary_path_uses_shared_note_builder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "export.json"
            source.write_text(json.dumps(sample_export()), encoding="utf-8")
            transcripts = root / "transcripts"
            recordings = root / "recordings"
            note = root / "notes" / "ready.md"
            with (
                mock.patch.object(meet_import.project_paths, "TRANSCRIPTS", transcripts),
                mock.patch.object(meet.project_paths, "TRANSCRIPTS", transcripts),
                mock.patch.object(meet.project_paths, "RECORDINGS", recordings),
                mock.patch.object(audio, "create_note_from_transcript", return_value=note
                ) as create_note,
            ):
                result = meet_import.import_export(source)
            self.assertEqual(result, note)
            create_note.assert_called_once()
            manifest = json.loads(next(recordings.glob("*.json")).read_text())
            self.assertEqual(manifest["status"], "complete")

    def test_watcher_finds_only_stable_meet_downloads(self):
        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory)
            stable = downloads / "meet-abc-defg-hij-old.json"
            recent = downloads / "meet-abc-defg-hij-new.json"
            unrelated = downloads / "export.json"
            for path in (stable, recent, unrelated):
                path.write_text("{}", encoding="utf-8")
            os.utime(stable, (900, 900))
            os.utime(recent, (998, 998))
            os.utime(unrelated, (900, 900))

            with (
                mock.patch.object(meet, "MEET_AUTO_IMPORT", True),
                mock.patch.object(meet, "MEET_DOWNLOADS_DIR", downloads),
                mock.patch.object(meet, "MEET_IMPORT_STABLE_SECONDS", 5),
            ):
                ready = meet.find_ready_meet_exports(now=1_000)

            self.assertEqual(ready, [stable])

    def test_watcher_auto_imports_and_summarizes_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloads = root / "Downloads"
            transcripts = root / "transcripts"
            recordings = root / "recordings"
            notes = root / "notes"
            downloads.mkdir()
            source = downloads / "meet-abc-defg-hij-export.json"
            source.write_text(json.dumps(sample_export()), encoding="utf-8")
            os.utime(source, (900, 900))
            expected_session = meet_import.session_id(sample_export())
            note = notes / f"{expected_session} — Готово.md"

            def create_note(session: str, transcript: str) -> Path:
                self.assertEqual(session, expected_session)
                self.assertIn("Починаємо перевірку розширення", transcript)
                note.parent.mkdir(parents=True, exist_ok=True)
                note.write_text("# Готово\n", encoding="utf-8")
                return note

            with (
                mock.patch.object(meet, "MEET_AUTO_IMPORT", True),
                mock.patch.object(meet, "MEET_AUTO_SUMMARY", True),
                mock.patch.object(meet, "MEET_IMPORT_EXISTING", True),
                mock.patch.object(meet, "MEET_DOWNLOADS_DIR", downloads),
                mock.patch.object(meet, "MEET_IMPORT_STABLE_SECONDS", 5),
                mock.patch.object(meet.project_paths, "TRANSCRIPTS", transcripts),
                mock.patch.object(meet.project_paths, "RECORDINGS", recordings),
                mock.patch.object(meet.project_paths, "NOTES", notes),
                mock.patch.object(meet.project_paths, "FAILED", root / "failed"),
                mock.patch.object(meet_import.project_paths, "TRANSCRIPTS", transcripts),
                mock.patch.object(meet, "create_note_from_transcript", side_effect=create_note
                ) as note_builder,
            ):
                self.assertEqual(meet.process_meet_exports(now=1_000), 1)
                self.assertEqual(meet.process_meet_exports(now=1_001), 0)

            note_builder.assert_called_once()
            self.assertFalse(source.exists())
            self.assertTrue(
                (transcripts / f"{expected_session}.md").exists()
            )
            manifest = json.loads(
                (recordings / f"{expected_session}.json").read_text()
            )
            self.assertEqual(manifest["status"], "complete")

    def test_failed_meet_note_retries_without_download_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcripts = root / "transcripts"
            recordings = root / "recordings"
            notes = root / "notes"
            failed = root / "failed"
            for path in (transcripts, recordings, notes, failed):
                path.mkdir()
            session = "2026-08-07_10-39-47_meet-fwv-mugk-zje"
            transcript = transcripts / f"{session}.md"
            transcript.write_text("# Meet\n\n[10:39] Interviewer: Домовились.\n")
            pipeline_utils.atomic_write_json(
                recordings / f"{session}.json",
                {
                    "session": session,
                    "source": "google-meet-live-captions",
                    "status": "processing_failed",
                    "processing_attempts": 1,
                    "next_retry_at": 900,
                },
            )
            expected_note = notes / f"{session} — Готово.md"

            def create_note(_session, _transcript):
                expected_note.write_text("# Готово\n")
                return expected_note

            with (
                mock.patch.object(meet, "MEET_AUTO_SUMMARY", True),
                mock.patch.object(meet.project_paths, "TRANSCRIPTS", transcripts),
                mock.patch.object(meet.project_paths, "RECORDINGS", recordings),
                mock.patch.object(meet.project_paths, "NOTES", notes),
                mock.patch.object(meet.project_paths, "FAILED", failed),
                mock.patch.object(meet, "create_note_from_transcript", side_effect=create_note
                ) as note_builder,
            ):
                self.assertEqual(
                    meet.find_ready_meet_sessions(now=1_000), [session]
                )
                self.assertEqual(
                    meet.process_failed_meet_sessions(now=1_000), 1
                )
                self.assertEqual(meet.find_ready_meet_sessions(now=1_001), [])

            note_builder.assert_called_once()
            self.assertTrue(expected_note.is_file())
            manifest = json.loads(
                (recordings / f"{session}.json").read_text()
            )
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["processing_attempts"], 2)

    def test_failed_meet_note_waits_until_retry_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcripts, recordings, notes = (
                root / "transcripts", root / "recordings", root / "notes"
            )
            for path in (transcripts, recordings, notes):
                path.mkdir()
            session = "meet-session"
            (transcripts / f"{session}.md").write_text("transcript")
            pipeline_utils.atomic_write_json(
                recordings / f"{session}.json",
                {
                    "session": session,
                    "source": "google-meet-live-captions",
                    "status": "processing_failed",
                    "processing_attempts": 1,
                    "next_retry_at": 1_100,
                },
            )
            with (
                mock.patch.object(meet, "MEET_AUTO_SUMMARY", True),
                mock.patch.object(meet.project_paths, "TRANSCRIPTS", transcripts),
                mock.patch.object(meet.project_paths, "RECORDINGS", recordings),
                mock.patch.object(meet.project_paths, "NOTES", notes),
            ):
                self.assertEqual(meet.find_ready_meet_sessions(now=1_000), [])
                self.assertEqual(
                    meet.find_ready_meet_sessions(now=1_100), [session]
                )

    def test_watcher_skips_existing_backlog_then_imports_new_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloads = root / "Downloads"
            transcripts = root / "transcripts"
            downloads.mkdir()
            source = downloads / "meet-abc-defg-hij-export.json"
            source.write_text(json.dumps(sample_export()), encoding="utf-8")
            os.utime(source, (900, 900))

            with (
                mock.patch.object(meet, "MEET_AUTO_IMPORT", True),
                mock.patch.object(meet, "MEET_AUTO_SUMMARY", False),
                mock.patch.object(meet, "MEET_IMPORT_EXISTING", False),
                mock.patch.object(meet, "MEET_DOWNLOADS_DIR", downloads),
                mock.patch.object(meet, "MEET_IMPORT_STABLE_SECONDS", 5),
                mock.patch.object(meet.project_paths, "TRANSCRIPTS", transcripts),
                mock.patch.object(meet_import.project_paths, "TRANSCRIPTS", transcripts),
            ):
                self.assertEqual(meet.process_meet_exports(now=1_000), 0)
                self.assertFalse(list(transcripts.glob("*_meet-*.md")))

                os.utime(source, (1_001, 1_001))
                self.assertEqual(meet.process_meet_exports(now=1_010), 1)

            self.assertFalse(source.exists())
            self.assertEqual(len(list(transcripts.glob("*_meet-*.md"))), 1)

    def test_watcher_keeps_download_when_summary_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloads = root / "Downloads"
            transcripts = root / "transcripts"
            recordings = root / "recordings"
            notes = root / "notes"
            downloads.mkdir()
            source = downloads / "meet-abc-defg-hij-export.json"
            source.write_text(json.dumps(sample_export()), encoding="utf-8")
            os.utime(source, (900, 900))

            with (
                mock.patch.object(meet, "MEET_AUTO_IMPORT", True),
                mock.patch.object(meet, "MEET_AUTO_SUMMARY", True),
                mock.patch.object(meet, "MEET_IMPORT_EXISTING", True),
                mock.patch.object(meet, "MEET_DOWNLOADS_DIR", downloads),
                mock.patch.object(meet, "MEET_IMPORT_STABLE_SECONDS", 5),
                mock.patch.object(meet.project_paths, "TRANSCRIPTS", transcripts),
                mock.patch.object(meet.project_paths, "RECORDINGS", recordings),
                mock.patch.object(meet.project_paths, "NOTES", notes),
                mock.patch.object(meet.project_paths, "FAILED", root / "failed"),
                mock.patch.object(meet_import.project_paths, "TRANSCRIPTS", transcripts),
                mock.patch.object(meet, "create_note_from_transcript",
                    side_effect=RuntimeError("summary failed"),
                ),
            ):
                self.assertEqual(meet.process_meet_exports(now=1_000), 0)

            self.assertTrue(source.exists())
            self.assertEqual(len(list(transcripts.glob("*_meet-*.md"))), 1)

    def test_watcher_reimports_fuller_export_of_same_meeting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloads, transcripts = root / "Downloads", root / "transcripts"
            recordings, notes = root / "recordings", root / "notes"
            downloads.mkdir()
            source = downloads / "meet-abc-defg-hij-export.json"
            first = sample_export()
            source.write_text(json.dumps(first), encoding="utf-8")
            os.utime(source, (900, 900))
            session = meet_import.session_id(first)
            note = notes / f"{session} — Готово.md"

            def create_note(_session, transcript):
                note.parent.mkdir(parents=True, exist_ok=True)
                note.write_text(transcript, encoding="utf-8")
                return note

            patches = (
                mock.patch.object(meet, "MEET_AUTO_IMPORT", True),
                mock.patch.object(meet, "MEET_AUTO_SUMMARY", True),
                mock.patch.object(meet, "MEET_IMPORT_EXISTING", True),
                mock.patch.object(meet, "MEET_DOWNLOADS_DIR", downloads),
                mock.patch.object(meet, "MEET_IMPORT_STABLE_SECONDS", 5),
                mock.patch.object(meet.project_paths, "TRANSCRIPTS", transcripts),
                mock.patch.object(meet.project_paths, "RECORDINGS", recordings),
                mock.patch.object(meet.project_paths, "NOTES", notes),
                mock.patch.object(meet.project_paths, "FAILED", root / "failed"),
                mock.patch.object(meet_import.project_paths, "TRANSCRIPTS", transcripts),
                mock.patch.object(meet, "create_note_from_transcript", side_effect=create_note
                ),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], patches[7], patches[8], patches[9], \
                    patches[10]:
                self.assertEqual(meet.process_meet_exports(now=1_000), 1)
                fuller = sample_export()
                fuller["entries"].append({
                    "speaker": "Анна",
                    "text": "Новий повніший хвіст зустрічі",
                    "startMs": 8_000,
                    "endMs": 10_000,
                })
                source.write_text(json.dumps(fuller), encoding="utf-8")
                os.utime(source, (1_001, 1_001))
                self.assertEqual(meet.process_meet_exports(now=1_010), 1)

            transcript = (transcripts / f"{session}.md").read_text(encoding="utf-8")
            self.assertIn("Новий повніший хвіст зустрічі", transcript)
            self.assertIn("Новий повніший хвіст зустрічі", note.read_text())
            self.assertFalse(source.exists())

    def test_terminal_failed_duplicate_is_quarantined_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloads, transcripts = root / "Downloads", root / "transcripts"
            recordings, notes, failed = (
                root / "recordings", root / "notes", root / "failed"
            )
            for path in (downloads, transcripts, recordings, notes):
                path.mkdir()
            data = sample_export()
            session = meet_import.session_id(data)
            source = downloads / "meet-abc-defg-hij-export.json"
            source.write_text(json.dumps(data), encoding="utf-8")
            os.utime(source, (900, 900))
            with mock.patch.object(meet_import.project_paths, "TRANSCRIPTS", transcripts):
                meet_import.import_export(source, summarize=False)
            pipeline_utils.atomic_write_json(
                recordings / f"{session}.json",
                {"status": "terminal_failed", "processing_attempts": 8},
            )
            with mock.patch.object(meet, "MEET_AUTO_IMPORT", True), \
                    mock.patch.object(meet, "MEET_AUTO_SUMMARY", True), \
                    mock.patch.object(meet, "MEET_IMPORT_EXISTING", True), \
                    mock.patch.object(meet, "MEET_DOWNLOADS_DIR", downloads), \
                    mock.patch.object(meet, "MEET_IMPORT_STABLE_SECONDS", 5), \
                    mock.patch.object(meet.project_paths, "TRANSCRIPTS", transcripts), \
                    mock.patch.object(meet.project_paths, "RECORDINGS", recordings), \
                    mock.patch.object(meet.project_paths, "NOTES", notes), \
                    mock.patch.object(meet.project_paths, "FAILED", failed), \
                    mock.patch.object(meet_import.project_paths, "TRANSCRIPTS", transcripts):
                self.assertEqual(meet.process_meet_exports(now=1_000), 0)
            self.assertFalse(source.exists())
            self.assertEqual(len(list(failed.glob("meet-*.json"))), 1)

    def test_invalid_auto_import_is_quarantined_instead_of_retried_forever(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloads = root / "Downloads"
            transcripts = root / "transcripts"
            failed = root / "failed"
            downloads.mkdir()
            source = downloads / "meet-crafted-export.json"
            source.write_text('{"schemaVersion": 1, "entries": []}', encoding="utf-8")
            os.utime(source, (900, 900))
            with mock.patch.object(meet, "MEET_AUTO_IMPORT", True), \
                    mock.patch.object(meet, "MEET_IMPORT_EXISTING", True), \
                    mock.patch.object(meet, "MEET_DOWNLOADS_DIR", downloads), \
                    mock.patch.object(meet, "MEET_IMPORT_STABLE_SECONDS", 5), \
                    mock.patch.object(meet.project_paths, "TRANSCRIPTS", transcripts), \
                    mock.patch.object(meet.project_paths, "FAILED", failed), \
                    mock.patch.object(meet_import.project_paths, "TRANSCRIPTS", transcripts):
                self.assertEqual(meet.process_meet_exports(now=1_000), 0)
            self.assertFalse(source.exists())
            self.assertTrue((failed / source.name).is_file())


if __name__ == "__main__":
    unittest.main()
