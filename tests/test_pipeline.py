from __future__ import annotations

import argparse
import io
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline_utils
import mic_watch
import record
import transcribe
import watch_and_process as watcher


class AtomicIOTests(unittest.TestCase):
    def test_private_directory_helper_repairs_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "private"
            target.mkdir(mode=0o755)
            pipeline_utils.ensure_private_dir(target)
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    def test_private_directory_helper_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            link = root / "private"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                pipeline_utils.ensure_private_dir(link)

    def test_atomic_text_and_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text_path = root / "nested" / "note.md"
            pipeline_utils.atomic_write_text(text_path, "готово\n")
            self.assertEqual(text_path.read_text(), "готово\n")
            json_path = root / "state.json"
            pipeline_utils.atomic_write_json(json_path, {"status": "recorded"})
            self.assertEqual(json.loads(json_path.read_text())["status"], "recorded")

    def test_normalized_audio_is_mono_16khz_and_cached(self):
        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            target = root / "processing" / "source.wav"
            sf.write(source, np.zeros((48_000, 2), dtype="float32"), 48_000)
            pipeline_utils.normalized_audio(source, target)
            first_mtime = target.stat().st_mtime_ns
            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
            info = pipeline_utils.audio_info(target)
            self.assertEqual(info["channels"], 1)
            self.assertEqual(info["sample_rate"], 16_000)
            pipeline_utils.normalized_audio(source, target)
            self.assertEqual(target.stat().st_mtime_ns, first_mtime)

    def test_audio_signal_info_detects_digital_silence_and_signal(self):
        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            silent, signal = root / "silent.wav", root / "signal.wav"
            sf.write(silent, np.zeros(16_000, dtype="float32"), 16_000)
            sf.write(signal, np.full(16_000, 0.5, dtype="float32"), 16_000)
            self.assertLess(pipeline_utils.audio_signal_info(silent)["peak_dbfs"], -200)
            self.assertAlmostEqual(
                pipeline_utils.audio_signal_info(signal)["rms_dbfs"], -6.02, places=1
            )

    def test_source_hash_is_reused_when_size_and_mtime_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache = root / "audio.wav", root / "cache.json"
            source.write_bytes(b"unchanged audio")
            cheap = pipeline_utils.file_fingerprint(source, with_hash=False)
            expected = {**cheap, "sha256": "trusted-hash"}
            pipeline_utils.atomic_write_json(
                cache, {"_meta": {"source": expected}, "segments": []}
            )
            with mock.patch.object(
                transcribe, "file_fingerprint",
                wraps=pipeline_utils.file_fingerprint,
            ) as fingerprint:
                self.assertEqual(
                    transcribe._source_fingerprint(source, cache), expected
                )
            fingerprint.assert_called_once_with(source, with_hash=False)


class SilenceMonitorTests(unittest.TestCase):
    def test_float_and_integer_pcm_levels(self):
        float_pcm = struct.pack("<4f", 0.5, -0.5, 0.5, -0.5)
        int_pcm = struct.pack("<4h", 16_384, -16_384, 16_384, -16_384)
        self.assertAlmostEqual(
            record.pcm_rms_dbfs(float_pcm, "float", 32), -6.0206, places=3
        )
        self.assertAlmostEqual(
            record.pcm_rms_dbfs(int_pcm, "signed_integer", 16),
            -6.0206,
            places=3,
        )
        self.assertLess(record.pcm_rms_dbfs(b"\0" * 16, "float", 32), -200)

    def test_popup_requires_both_tracks_and_respects_repeat_interval(self):
        monitor = record.AudioActivityMonitor(
            silence_seconds=90,
            min_record_seconds=120,
            repeat_seconds=600,
            started_at=100,
        )
        monitor.observe_mic(-100, now=100)
        self.assertFalse(monitor.should_prompt(now=220))
        monitor.observe_system(-100, now=100)
        self.assertFalse(monitor.should_prompt(now=219.9))
        self.assertTrue(monitor.should_prompt(now=220))
        monitor.mark_prompted(now=220)
        self.assertFalse(monitor.should_prompt(now=819.9))
        self.assertTrue(monitor.should_prompt(now=820))

    def test_activity_on_either_track_postpones_popup(self):
        monitor = record.AudioActivityMonitor(
            silence_seconds=90,
            min_record_seconds=0,
            repeat_seconds=600,
            started_at=0,
        )
        monitor.observe_mic(-10, now=100)
        monitor.observe_system(-100, now=0)
        self.assertFalse(monitor.should_prompt(now=189.9))
        self.assertTrue(monitor.should_prompt(now=190))

    def test_unanswered_popup_auto_stops_after_five_minutes_silence(self):
        monitor = record.AudioActivityMonitor(
            silence_seconds=90,
            min_record_seconds=0,
            repeat_seconds=600,
            auto_stop_seconds=300,
            started_at=0,
        )
        monitor.observe_mic(-100, now=0)
        monitor.observe_system(-100, now=0)
        monitor.mark_prompted(now=90)
        monitor.mark_unanswered(now=120)
        self.assertFalse(monitor.should_auto_stop(now=299.9))
        self.assertTrue(monitor.should_auto_stop(now=300))

    def test_activity_cancels_scheduled_auto_stop(self):
        monitor = record.AudioActivityMonitor(
            silence_seconds=90,
            min_record_seconds=0,
            auto_stop_seconds=300,
            started_at=0,
        )
        monitor.observe_mic(-100, now=0)
        monitor.observe_system(-100, now=0)
        monitor.mark_unanswered(now=120)
        monitor.observe_system(-10, now=250)
        self.assertFalse(monitor.should_auto_stop(now=400))

    def test_continue_snoozes_popup_for_ten_minutes(self):
        monitor = record.AudioActivityMonitor(
            silence_seconds=90,
            min_record_seconds=0,
            repeat_seconds=600,
            auto_stop_seconds=300,
            started_at=0,
        )
        monitor.observe_mic(-100, now=0)
        monitor.observe_system(-100, now=0)
        monitor.mark_prompted(now=90)
        monitor.mark_continued(now=120)
        self.assertFalse(monitor.should_prompt(now=719.9))
        self.assertTrue(monitor.should_prompt(now=720))
        self.assertFalse(monitor.should_auto_stop(now=720))

    def test_snapshot_uses_json_safe_value_before_first_buffer(self):
        monitor = record.AudioActivityMonitor(started_at=0)
        self.assertIsNone(monitor.snapshot()["last_mic_dbfs"])
        self.assertIsNone(monitor.snapshot()["last_system_dbfs"])

    def test_dialog_result_requires_explicit_finish_click(self):
        process = mock.Mock()
        process.poll.return_value = 0
        process.stdout = io.StringIO(
            "button returned:Завершити запис, gave up:false\n"
        )
        with mock.patch.object(record.subprocess, "Popen", return_value=process):
            stop_event = mock.Mock(is_set=lambda: False)
            self.assertEqual(
                record.ask_finish_after_silence(stop_event), "finish"
            )

        process.stdout = io.StringIO(
            "button returned:Завершити запис, gave up:true\n"
        )
        with mock.patch.object(record.subprocess, "Popen", return_value=process):
            stop_event = mock.Mock(is_set=lambda: False)
            self.assertEqual(
                record.ask_finish_after_silence(stop_event), "timeout"
            )


class MicrophoneModeTests(unittest.TestCase):
    def test_recording_process_lock_rejects_second_recorder(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            record, "PROCESS_LOCK_PATH", Path(directory) / "record.lock"
        ):
            first = record.acquire_process_lock()
            try:
                with self.assertRaisesRegex(RuntimeError, "RECORDING_ALREADY_RUNNING"):
                    record.acquire_process_lock()
            finally:
                first.close()

    def test_paused_auto_start_keeps_manual_queue_without_polling_microphone(self):
        with (
            mock.patch.object(mic_watch, "MIC_AUTO_START", False),
            mock.patch.object(mic_watch, "consume_control_requests", return_value=0)
            as consume,
            mock.patch.object(mic_watch, "mic_in_use") as mic_in_use,
            mock.patch.object(mic_watch, "ask_to_record") as ask_to_record,
            mock.patch.object(
                mic_watch.time, "sleep", side_effect=KeyboardInterrupt
            ),
        ):
            with self.assertRaises(KeyboardInterrupt):
                mic_watch.main()
        consume.assert_called_once_with()
        mic_in_use.assert_not_called()
        ask_to_record.assert_not_called()

    def test_control_queue_executes_only_known_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            requests = Path(directory)
            (requests / "1.request").write_text("toggle\n")
            (requests / "2.request").write_text("unknown\n")
            with (
                mock.patch.object(mic_watch, "REQUEST_DIR", requests),
                mock.patch.object(mic_watch.subprocess, "run") as run,
            ):
                self.assertEqual(mic_watch.consume_control_requests(), 1)
            run.assert_called_once_with([str(mic_watch.TOGGLE)], check=False)
            self.assertEqual(list(requests.glob("*.request")), [])

    def test_control_queue_drops_stale_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            requests = Path(directory)
            request = requests / "old.request"
            request.write_text("toggle\n")
            old = mic_watch.time.time() - mic_watch.REQUEST_MAX_AGE_SECONDS - 1
            mic_watch.os.utime(request, (old, old))
            with (
                mock.patch.object(mic_watch, "REQUEST_DIR", requests),
                mock.patch.object(mic_watch.subprocess, "run") as run,
            ):
                self.assertEqual(mic_watch.consume_control_requests(), 0)
            run.assert_not_called()
            self.assertFalse(request.exists())

    def test_cli_mode_overrides_env_fallback(self):
        with mock.patch.dict(
            record.os.environ,
            {"RECORD_MIC_MODE": "multiple", "RECORD_AEC": "false"},
        ):
            self.assertEqual(record.resolve_mic_speaker_mode([]), "multiple")
            self.assertEqual(record.resolve_mic_speaker_mode(["--raw"]), "single")
        with mock.patch.dict(
            record.os.environ,
            {"RECORD_MIC_MODE": "single", "RECORD_AEC": "true"},
        ):
            self.assertEqual(record.resolve_mic_speaker_mode([]), "single")
            self.assertEqual(
                record.resolve_mic_speaker_mode(["--speakers"]), "multiple"
            )

    def test_legacy_aec_alias_selects_multiple_without_live_aec(self):
        with mock.patch.dict(record.os.environ, {}, clear=True):
            self.assertEqual(
                record.resolve_mic_speaker_mode(["--aec"]), "multiple"
            )
        with mock.patch.dict(record.os.environ, {"RECORD_AEC": "true"}, clear=True):
            self.assertEqual(record.resolve_mic_speaker_mode([]), "multiple")

    def test_conflicting_or_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            record.resolve_mic_speaker_mode(["--speakers", "--raw"])
        with self.assertRaises(ValueError):
            record.resolve_mic_speaker_mode(["--other"])

    def test_start_popup_returns_selected_mode(self):
        cases = {
            "button returned:Навушники, gave up:false": "--raw",
            "button returned:Динаміки, gave up:false": "--speakers",
            "button returned:Динаміки, gave up:true": None,
            "button returned:Пропустити, gave up:false": None,
        }
        for output, expected in cases.items():
            result = mock.Mock(stdout=output)
            with mock.patch.object(mic_watch.subprocess, "run", return_value=result):
                self.assertEqual(mic_watch.ask_to_record(), expected)


class TranscriptionQualityTests(unittest.TestCase):
    def test_mic_speaker_labels_are_namespaced_as_local(self):
        segments = [
            {"start": 0, "end": 1, "speaker": "SPEAKER_00", "text": "a"},
            {"start": 1, "end": 2, "speaker": "SPEAKER_01", "text": "b"},
            {"start": 2, "end": 3, "speaker": "UNKNOWN", "text": "c"},
        ]
        localized = transcribe.localize_mic_speakers(segments)
        self.assertEqual(
            [item["speaker"] for item in localized],
            ["LOCAL_00", "LOCAL_01", "LOCAL_UNKNOWN"],
        )

    def test_periodic_generic_hallucination_is_fully_removed(self):
        segments = [
            {"start": second, "end": second + 2, "text": "Дякую."}
            for second in (120, 150, 180, 210, 240)
        ] + [{"start": 245, "end": 247, "text": "Реальна фраза"}]
        clean, dropped = transcribe.drop_periodic_repetitions(segments)
        self.assertEqual(dropped, 5)
        self.assertEqual([item["text"] for item in clean], ["Реальна фраза"])

    def test_periodic_long_phrase_keeps_first_occurrence(self):
        phrase = "Ти напевно що на фейбу шукав"
        segments = [
            {"start": second, "end": second + 2, "text": phrase}
            for second in (172, 194, 224, 254, 284)
        ]
        clean, dropped = transcribe.drop_periodic_repetitions(segments)
        self.assertEqual(dropped, 4)
        self.assertEqual(len(clean), 1)
        self.assertEqual(clean[0]["start"], 172)

    def test_non_periodic_replies_are_preserved(self):
        segments = [
            {"start": second, "end": second + 1, "text": "Дякую"}
            for second in (0, 5, 70, 140)
        ]
        clean, dropped = transcribe.drop_periodic_repetitions(segments)
        self.assertEqual(dropped, 0)
        self.assertEqual(clean, segments)

    def test_obvious_pyannote_micro_clusters_are_collapsed(self):
        segments = [
            {"start": 0, "end": 1000, "speaker": "SPEAKER_02", "text": "main"},
            {"start": 1001, "end": 1009, "speaker": "SPEAKER_00", "text": "a"},
            {"start": 1010, "end": 1019, "speaker": "SPEAKER_01", "text": "b"},
            {"start": 1020, "end": 1021, "speaker": "UNKNOWN", "text": "c"},
        ]
        clean, quality = transcribe.collapse_fragmented_speakers(segments)
        self.assertTrue(quality["collapsed"])
        self.assertEqual({item["speaker"] for item in clean}, {"SPEAKER_00"})

    def test_two_speaker_case_is_never_auto_collapsed(self):
        segments = [
            {"start": 0, "end": 1000, "speaker": "SPEAKER_00", "text": "main"},
            {"start": 1001, "end": 1005, "speaker": "SPEAKER_01", "text": "short"},
        ]
        clean, quality = transcribe.collapse_fragmented_speakers(segments)
        self.assertFalse(quality["collapsed"])
        self.assertEqual(clean, segments)

    def test_short_replies_are_never_deduplicated(self):
        mic = [{"start": 1, "end": 2, "speaker": "Я", "text": "Так"}]
        system = [{"start": 1, "end": 2, "speaker": "SPEAKER_00", "text": "Так"}]
        kept, dropped = transcribe.dedup_mic(mic, system)
        self.assertEqual(kept, mic)
        self.assertEqual(dropped, 0)

    def test_long_near_duplicate_is_removed(self):
        text = "потрібно перевірити цей звіт до понеділка"
        mic = [{"start": 1, "end": 4, "speaker": "Я", "text": text}]
        system = [{"start": 1.2, "end": 4.1, "speaker": "SPEAKER_00", "text": text}]
        kept, dropped = transcribe.dedup_mic(mic, system)
        self.assertEqual(kept, [])
        self.assertEqual(dropped, 1)

    def test_asr_timestamps_are_clipped_and_silence_is_dropped(self):
        segments = [
            {"start": 0, "end": 30, "text": "тест"},
            {"start": 1, "end": 2, "text": "галюцинація",
             "no_speech_prob": 0.95, "avg_logprob": -1.2},
        ]
        clean, dropped = transcribe.sanitize_asr_segments(segments, 3.5)
        self.assertEqual(len(clean), 1)
        self.assertEqual(clean[0]["end"], 3.5)
        self.assertEqual(dropped, 1)

    def test_high_no_speech_probability_is_dropped_even_with_confident_text(self):
        segments = [{
            "start": 1,
            "end": 2,
            "text": "Дякую за перегляд!",
            "no_speech_prob": 0.8,
            "avg_logprob": -0.07,
        }]
        clean, dropped = transcribe.sanitize_asr_segments(segments, 3.0)
        self.assertEqual(clean, [])
        self.assertEqual(dropped, 1)

    def test_word_level_diarization_splits_speaker_change(self):
        segments = [{
            "start": 0,
            "end": 1,
            "text": "привіт так",
            "words": [
                {"start": 0.0, "end": 0.4, "word": "привіт"},
                {"start": 0.6, "end": 1.0, "word": "так"},
            ],
        }]
        turns = [(0.0, 0.5, "SPEAKER_00"), (0.5, 1.1, "SPEAKER_01")]
        result = transcribe.assign_word_speakers(segments, turns)
        self.assertEqual([item["speaker"] for item in result],
                         ["SPEAKER_00", "SPEAKER_01"])

    def test_manifest_timeline_correction(self):
        source = [{"start": 10.0, "end": 20.0, "speaker": "Я", "text": "x"}]
        result, sync = transcribe.correct_mic_timeline(
            source,
            mic_duration=100,
            sys_duration=102,
            session_manifest={"timing": {
                "mic_start_offset_seconds": 0.5,
                "mic_time_scale": 1.01,
            }},
        )
        self.assertAlmostEqual(result[0]["start"], 10.6)
        self.assertAlmostEqual(result[0]["end"], 20.7)
        self.assertEqual(sync["method"], "recording-manifest")


class MeetingNoteMetadataTests(unittest.TestCase):
    def test_uses_explicit_meet_metadata_and_all_participants(self):
        transcript = """# Weekly sync

- **Час початку:** 2026-07-30 10:30:40 CEST
- **Назва зустрічі:** Analytics Weekly

**Учасники:**
- Інтерв’юер
- Анна
- Марія

## Транскрипт

[10:30:40] Інтерв’юер: Привіт
"""
        metadata = watcher._meeting_note_metadata(
            "2026-07-30_10-30-40_meet-abc-defg-hij",
            transcript,
            "Згенерована назва",
        )
        self.assertEqual(metadata, (
            "2026-07-30",
            "10:30:40 CEST",
            "Analytics Weekly",
            ["Інтерв’юер", "Анна", "Марія"],
        ))

    def test_falls_back_to_session_title_and_unique_speakers(self):
        transcript = """# Транскрипт

[14:32] SPEAKER_00: Перша репліка
[14:33] SPEAKER_01: Друга репліка
[14:34] SPEAKER_00: Ще одна репліка
"""
        metadata = watcher._meeting_note_metadata(
            "2026-07-07_1432",
            transcript,
            "Планування робіт",
        )
        self.assertEqual(metadata, (
            "2026-07-07",
            "14:32",
            "Планування робіт",
            ["SPEAKER_00", "SPEAKER_01"],
        ))


class WatcherStateTests(unittest.TestCase):
    def test_watcher_log_includes_local_date_and_time(self):
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            watcher.log("операція почалась")
        self.assertRegex(
            output.getvalue(),
            r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] операція почалась\n$",
        )

    def test_ollama_think_can_be_overridden_for_candidate_flow(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"response":"ok"}'

        with mock.patch.object(watcher, "OLLAMA_THINK", False), \
             mock.patch.object(
                 watcher.urllib.request, "urlopen", return_value=Response()
             ) as urlopen:
            self.assertEqual(
                watcher.ollama_generate("prompt", think=True, num_predict=1234),
                "ok",
            )
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertTrue(payload["think"])
        self.assertEqual(payload["options"]["num_predict"], 1234)

    def test_candidate_reasoning_is_reserved_for_final_assessment(self):
        with mock.patch.object(watcher, "CANDIDATE_OLLAMA_THINK", True):
            self.assertEqual(
                watcher._candidate_generation_options(
                    "Ти витягуєш докази з транскрипту"
                ),
                (1200, False),
            )
            self.assertEqual(
                watcher._candidate_generation_options(
                    "Ти неупереджений hiring assessor"
                ),
                (6144, True),
            )
            self.assertEqual(
                watcher._candidate_generation_options(
                    "Ти форматуєш фінальний hiring report"
                ),
                (6144, False),
            )

    def test_candidate_title_routes_to_alternative_flow(self):
        transcript = """# Interview | Jane Doe

- **Назва зустрічі:** Interview | Jane Doe

**Учасники:**
- Interviewer
- Jane Doe

## Транскрипт

[10:00] Jane Doe: Hello
"""
        expected = Path("/tmp/Jane_Doe_2026-07-31.md")
        with mock.patch.object(watcher, "CANDIDATE_EVALUATION_ENABLED", True), \
             mock.patch.object(
                 watcher,
                 "create_candidate_evaluation_from_transcript",
                 return_value=expected,
             ) as create:
            result = watcher.create_note_from_transcript(
                "2026-07-31_100000_meet-abc-defg-hij", transcript
            )
        self.assertEqual(result, expected)
        self.assertEqual(create.call_args.kwargs["meeting_title"], "Interview | Jane Doe")

    def test_early_terminated_candidate_skips_llm_and_notion_feedback(self):
        import candidate_evaluation

        transcript = """# Interview | Jane Doe | Data Analyst

[10:00] Jane Doe: Краще не витрачати час одне одного.
[10:01] Interviewer: Добре, завершимо тут.
"""
        classification = {
            "meeting_type_label": "Співбесіда",
            "outcome": "early_terminated",
            "outcome_label": "Достроково завершена",
            "reason": "Розмову завершено до основної частини.",
            "evidence": [{
                "timestamp": "10:00",
                "speaker": "Jane Doe",
                "quote": "Краще не витрачати час одне одного.",
            }],
            "candidate_evaluation_eligible": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            transcripts = root / "transcripts"
            evaluations = root / "candidate_evaluations"
            recordings.mkdir()
            transcripts.mkdir()
            with mock.patch.object(watcher.project_paths, "RECORDINGS", recordings), \
                    mock.patch.object(watcher.project_paths, "TRANSCRIPTS", transcripts), \
                    mock.patch.object(candidate_evaluation, "EVALUATIONS", evaluations), \
                    mock.patch.object(watcher, "ollama_generate") as generate, \
                    mock.patch(
                        "notion_agent.sync_evaluation_feedback_if_enabled"
                    ) as notion_sync:
                path = watcher.create_candidate_evaluation_from_transcript(
                    "session",
                    transcript,
                    meeting_date="2026-08-11",
                    meeting_title="Interview | Jane Doe | Data Analyst",
                    participants=["Jane Doe", "Interviewer"],
                    classification=classification,
                )
            self.assertTrue(path.is_file())
            self.assertIn("Не проводилося", path.read_text(encoding="utf-8"))
            generate.assert_not_called()
            notion_sync.assert_not_called()

    def test_ambiguous_interview_keyword_falls_back_to_regular_note(self):
        transcript = """# Customer interview — churn research

- **Назва зустрічі:** Customer interview — churn research

## Транскрипт

[10:00] Customer: We discuss churn
"""
        summary = "\n".join(watcher.REQUIRED_HEADINGS)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("notes", "recordings", "transcripts"):
                (root / name).mkdir()
            with mock.patch.object(watcher, "CANDIDATE_EVALUATION_ENABLED", True), \
                    mock.patch.object(watcher.project_paths, "NOTES", root / "notes"), \
                    mock.patch.object(watcher.project_paths, "RECORDINGS", root / "recordings"), \
                    mock.patch.object(watcher.project_paths, "TRANSCRIPTS", root / "transcripts"), \
                    mock.patch.object(watcher, "summarize", return_value=summary), \
                    mock.patch.object(watcher, "make_title", return_value="Churn research"), \
                    mock.patch.object(
                        watcher, "create_candidate_evaluation_from_transcript"
                    ) as candidate_flow, \
                    mock.patch("notion_agent.sync_note_if_enabled"):
                note = watcher.create_note_from_transcript("session", transcript)
            self.assertTrue(note.is_file())
            candidate_flow.assert_not_called()

    def test_refresh_note_replaces_transcript_and_collapsed_speaker_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notes, transcripts = root / "notes", root / "transcripts"
            notes.mkdir()
            (transcripts / "session").mkdir(parents=True)
            note = notes / "session — Test.md"
            note.write_text(
                "# Test\n\n## Мапінг спікерів\n\n"
                "| Спікер | Ім'я |\n|---|---|\n| Я | |\n"
                "| SPEAKER_01 | |\n| SPEAKER_02 | |\n\n"
                "## Action items\n- [SPEAKER_02] дія\n"
                "\n---\n\n## Повний транскрипт\n\nold\n"
            )
            (transcripts / "session.md").write_text(
                "# Транскрипт\n\n[00:00] SPEAKER_00: new\n"
            )
            pipeline_utils.atomic_write_json(
                transcripts / "session" / "manifest.json",
                {"quality": {"speaker_collapse": {
                    "collapsed": True,
                    "dominant_label": "SPEAKER_02",
                    "merged_labels": ["SPEAKER_01"],
                }}},
            )
            with mock.patch.object(watcher.project_paths, "NOTES", notes), \
                 mock.patch.object(watcher.project_paths, "TRANSCRIPTS", transcripts):
                watcher.refresh_note_transcript("session")
            refreshed = note.read_text()
            self.assertIn("[SPEAKER_00] дія", refreshed)
            self.assertIn("[00:00] SPEAKER_00: new", refreshed)
            self.assertNotIn("SPEAKER_01", refreshed)
            self.assertNotIn("SPEAKER_02", refreshed)
            self.assertNotIn("old", refreshed)

    def test_refresh_note_adds_missing_local_speaker_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notes, transcripts = root / "notes", root / "transcripts"
            notes.mkdir()
            (transcripts / "session").mkdir(parents=True)
            note = notes / "session — Test.md"
            note.write_text(
                "# Test\n\n## TL;DR\nSummary\n"
                "\n---\n\n## Повний транскрипт\n\nold\n"
            )
            (transcripts / "session.md").write_text(
                "# Транскрипт\n\n[00:00] LOCAL_00: a\n[00:01] LOCAL_01: b\n"
            )
            pipeline_utils.atomic_write_json(
                transcripts / "session" / "manifest.json", {"quality": {}}
            )
            with mock.patch.object(watcher.project_paths, "NOTES", notes), \
                 mock.patch.object(watcher.project_paths, "TRANSCRIPTS", transcripts):
                watcher.refresh_note_transcript("session")
            refreshed = note.read_text()
            self.assertIn("| LOCAL_00 | |", refreshed)
            self.assertIn("| LOCAL_01 | |", refreshed)

    def test_remote_ollama_requires_explicit_opt_in(self):
        with mock.patch.object(watcher, "OLLAMA_URL", "https://example.com"), \
             mock.patch.object(watcher, "ALLOW_REMOTE_OLLAMA", False):
            with self.assertRaises(RuntimeError):
                watcher._assert_private_ollama()

    def test_local_ollama_is_allowed(self):
        with mock.patch.object(watcher, "OLLAMA_URL", "http://127.0.0.1:11434"), \
             mock.patch.object(watcher, "ALLOW_REMOTE_OLLAMA", False):
            watcher._assert_private_ollama()

    def test_manifest_controls_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            notes = root / "notes"
            failed = root / "failed"
            recordings.mkdir()
            notes.mkdir()
            failed.mkdir()
            session = "2026-01-01_120000"
            (recordings / f"{session}_mic.wav").write_bytes(b"mic")
            (recordings / f"{session}_sys.wav").write_bytes(b"sys")
            manifest = recordings / f"{session}.json"
            pipeline_utils.atomic_write_json(manifest, {"status": "recording"})
            with mock.patch.object(watcher.project_paths, "RECORDINGS", recordings), \
                 mock.patch.object(watcher.project_paths, "NOTES", notes), \
                 mock.patch.object(watcher.project_paths, "FAILED", failed):
                self.assertEqual(watcher.find_ready_sessions(), [])
                pipeline_utils.atomic_write_json(manifest, {"status": "recorded"})
                self.assertEqual(watcher.find_ready_sessions(), [session])

    def test_rotation_handles_complete_and_legacy_sessions_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            transcripts = root / "transcripts"
            notes = root / "notes"
            for path in (recordings, transcripts, notes):
                path.mkdir()
            now = 2_000_000_000.0
            old = now - 6 * 86400

            for session in ("legacy", "complete", "failed"):
                for track in ("mic", "sys"):
                    wav = recordings / f"{session}_{track}.wav"
                    wav.write_bytes(b"audio")
                    watcher.os.utime(wav, (old, old))
                (notes / f"{session} — Note.md").write_text("note")
                (transcripts / f"{session}.md").write_text("transcript")

            pipeline_utils.atomic_write_json(
                recordings / "complete.json", {"status": "complete"}
            )
            pipeline_utils.atomic_write_json(
                recordings / "failed.json", {"status": "recording_failed"}
            )

            with mock.patch.object(watcher.project_paths, "RECORDINGS", recordings), \
                 mock.patch.object(watcher.project_paths, "TRANSCRIPTS", transcripts), \
                 mock.patch.object(watcher.project_paths, "NOTES", notes), \
                 mock.patch.object(watcher.project_paths, "FAILED", root / "failed"), \
                 mock.patch.object(watcher, "ROTATE_DAYS", 5), \
                 mock.patch.object(watcher.time, "time", return_value=now):
                watcher.rotate_old_wavs()

            self.assertFalse(any(recordings.glob("legacy_*.wav")))
            self.assertFalse(any(recordings.glob("complete_*.wav")))
            self.assertEqual(len(list(recordings.glob("failed_*.wav"))), 2)

    def test_rotation_rejects_parent_directory_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            transcripts = root / "transcripts"
            notes = root / "notes"
            failed = root / "failed"
            for path in (recordings, transcripts, notes, failed):
                path.mkdir()
            marker = root / "must-survive.txt"
            marker.write_text("safe", encoding="utf-8")
            malicious = recordings / "...json"
            pipeline_utils.atomic_write_json(malicious, {
                "status": "complete",
                "candidate_evaluation": str(marker),
            })
            old = 2_000_000_000.0 - 6 * 86400
            watcher.os.utime(malicious, (old, old))

            with mock.patch.object(watcher.project_paths, "RECORDINGS", recordings), \
                 mock.patch.object(watcher.project_paths, "TRANSCRIPTS", transcripts), \
                 mock.patch.object(watcher.project_paths, "NOTES", notes), \
                 mock.patch.object(watcher.project_paths, "FAILED", failed), \
                 mock.patch.object(watcher, "ROTATE_DAYS", 5), \
                 mock.patch.object(watcher.time, "time", return_value=2_000_000_000.0):
                watcher.rotate_old_wavs()

            self.assertTrue(root.is_dir())
            self.assertEqual(marker.read_text(encoding="utf-8"), "safe")
            self.assertTrue(malicious.exists())

    def test_session_validation_rejects_dot_segments(self):
        for value in (".", "..", "../escape", "*", ""):
            with self.subTest(value=value):
                with self.assertRaises((ValueError, argparse.ArgumentTypeError)):
                    watcher._validate_session(value)

    def test_summary_structure_validation(self):
        complete = """## TL;DR
Коротко.

## Основні тези
- Теза

## Рішення
- —

## Action items
- —

## Відкриті питання
- —
"""
        self.assertTrue(watcher._valid_summary(complete))
        self.assertFalse(watcher._valid_summary("## TL;DR\nтільки одна секція"))
        self.assertFalse(watcher._valid_summary(complete + "\n## Зайва секція\n- ні"))

    def test_evidence_validation_drops_fabricated_quotes(self):
        transcript = "[00:10] Максим: Пропоную почати з ринку США."
        raw = {
            "items": [
                {
                    "type": "recommendation",
                    "claim": "Почати з ринку США",
                    "speaker": "Максим",
                    "evidence": [
                        {"timestamp": "00:10", "quote": "Це вже остаточне рішення"}
                    ],
                }
            ]
        }
        ledger, dropped = watcher._validated_evidence_ledger(raw, transcript)
        self.assertEqual(ledger, {"items": []})
        self.assertEqual(dropped, 1)

    def test_evidence_validation_keeps_quote_and_clears_wrong_timestamp(self):
        transcript = "[00:10] Максим: Пропоную почати з ринку США."
        raw = {
            "items": [{
                "type": "recommendation",
                "claim": "Почати з ринку США",
                "speaker": "Максим",
                "evidence": [{
                    "timestamp": "99:99",
                    "quote": "Пропоную почати з ринку США",
                }],
            }]
        }
        ledger, dropped = watcher._validated_evidence_ledger(raw, transcript)
        self.assertEqual(dropped, 0)
        self.assertEqual(ledger["items"][0]["evidence"][0]["timestamp"], "")

    def test_commitment_owner_must_be_the_evidence_speaker(self):
        transcript = (
            "[00:10] External Participant: Потрібно підготувати звіт завтра.\n"
            "[00:11] Current User: Я перевірю дані окремо."
        )
        raw = {"items": [{
            "type": "commitment",
            "claim": "Підготувати звіт",
            "speaker": "Current User",
            "owners": ["Current User"],
            "status": "open",
            "commitment_strength": "strong",
            "evidence": [{
                "timestamp": "00:10",
                "quote": "Потрібно підготувати звіт завтра",
            }],
        }]}
        ledger, dropped = watcher._validated_evidence_ledger(raw, transcript)
        self.assertEqual(dropped, 0)
        self.assertEqual(ledger["items"][0]["speaker"], "External Participant")
        self.assertEqual(ledger["items"][0]["owners"], [])

    def test_commitment_keeps_owner_who_spoke_the_evidence(self):
        transcript = "[00:10] Current User: Я підготую звіт завтра."
        raw = {"items": [{
            "type": "commitment",
            "claim": "Підготувати звіт",
            "speaker": "External Participant",
            "owners": ["Current User"],
            "status": "open",
            "commitment_strength": "strong",
            "evidence": [{
                "timestamp": "00:10",
                "quote": "Я підготую звіт завтра",
            }],
        }]}
        ledger, _ = watcher._validated_evidence_ledger(raw, transcript)
        self.assertEqual(ledger["items"][0]["speaker"], "Current User")
        self.assertEqual(ledger["items"][0]["owners"], ["Current User"])

    def test_context_generation_cannot_bypass_decision_reconciliation(self):
        transcript = "[00:10] Максим: Домовились почати з ринку США."
        raw = json.dumps({
            "items": [{
                "type": "decision",
                "claim": "Почати з ринку США",
                "speaker": "Максим",
                "owners": [],
                "deadline": "",
                "status": "active",
                "commitment_strength": "not_applicable",
                "confidence": "high",
                "evidence": [{
                    "timestamp": "00:10",
                    "quote": "Домовились почати з ринку США",
                }],
            }]
        }, ensure_ascii=False)
        generate = mock.Mock(side_effect=[raw, json.dumps({"items": []})])
        with mock.patch.object(watcher, "ollama_generate", generate):
            ledger = watcher._generate_evidence_ledger(
                "context prompt",
                transcript,
                stage="context",
                allowed_types=watcher.CONTEXT_EVIDENCE_TYPES,
            )
        self.assertEqual(ledger, {"items": []})
        self.assertEqual(generate.call_count, 2)

    def test_evidence_json_repair_keeps_full_output_budget(self):
        generate = mock.Mock(side_effect=[
            '{"items":[',
            json.dumps({"items": []}),
        ])
        with mock.patch.object(watcher, "ollama_generate", generate):
            ledger = watcher._generate_evidence_ledger(
                "merge prompt",
                "transcript",
                stage="context evidence merge",
                num_predict=4096,
                allowed_types=watcher.CONTEXT_EVIDENCE_TYPES,
            )
        self.assertEqual(ledger, {"items": []})
        self.assertEqual(
            [call.kwargs["num_predict"] for call in generate.call_args_list],
            [4096, 4096],
        )

    def test_critical_merge_has_larger_output_budget(self):
        self.assertEqual(watcher.SUMMARY_CRITICAL_MERGE_NUM_PREDICT, 8192)
        self.assertEqual(watcher.SUMMARY_CRITICAL_RECONCILE_NUM_PREDICT, 8192)

    def test_grounded_sections_do_not_promote_recommendations_or_completed_work(self):
        draft = """## TL;DR
Обговорили запуск.

## Основні тези
- Тези.

## Рішення
- Запускатися у США

## Action items
- [Максим] Надіслати Meta Ads Library

## Відкриті питання
- GDPR
"""
        ledger = {
            "items": [
                {"type": "recommendation", "status": "active", "claim": "Розглянути США"},
                {"type": "hypothesis", "status": "active", "claim": "У США нижчі регуляторні бар'єри"},
                {"type": "decision", "status": "active", "claim": "Проводити щотижневий sync"},
                {
                    "type": "commitment",
                    "status": "open",
                    "claim": "Проаналізувати конкурентів",
                    "owners": ["Інтерв’юер"],
                    "deadline": "",
                },
                {
                    "type": "commitment",
                    "status": "open",
                    "claim": "Організувати зустріч із creative team",
                    "owners": ["Максим"],
                    "deadline": "наступного тижня",
                    "commitment_strength": "soft",
                },
                {
                    "type": "completed_action",
                    "status": "completed",
                    "claim": "Надіслати Meta Ads Library",
                    "owners": ["Максим"],
                },
            ]
        }
        summary = watcher._render_grounded_sections(draft, ledger)
        self.assertNotIn("Обговорили запуск.", summary)
        self.assertIn("Явні рішення: Проводити щотижневий sync.", summary)
        decisions = watcher._summary_section(summary, "## Рішення")
        actions = watcher._summary_section(summary, "## Action items")
        self.assertEqual(decisions, "- Проводити щотижневий sync")
        self.assertIn("[Інтерв’юер] Проаналізувати конкурентів", actions)
        self.assertIn("Спробувати організувати", actions)
        self.assertIn("дедлайн: наступного тижня", actions)
        self.assertNotIn("США", decisions)
        self.assertNotIn("Meta Ads Library", actions)

    def test_summary_builds_and_persists_evidence_ledger(self):
        transcript = "[00:10] Інтерв’юер: Домовились проводити щотижневий sync."
        raw_ledger = json.dumps({
            "items": [{
                "type": "decision",
                "claim": "Проводити щотижневий sync",
                "speaker": "Інтерв’юер",
                "owners": [],
                "deadline": "",
                "status": "active",
                "commitment_strength": "not_applicable",
                "confidence": "high",
                "evidence": [{
                    "timestamp": "00:10",
                    "quote": "Домовились проводити щотижневий sync",
                }],
            }]
        }, ensure_ascii=False)
        with tempfile.TemporaryDirectory() as directory:
            transcripts = Path(directory) / "transcripts"
            session = "meeting"
            (transcripts / session).mkdir(parents=True)
            empty_ledger = json.dumps({"items": []})
            generate = mock.Mock(side_effect=[
                raw_ledger,
                empty_ledger,
                "Рішення явно підтверджене і не скасоване.",
                raw_ledger,
            ])
            with mock.patch.object(watcher.project_paths, "TRANSCRIPTS", transcripts), \
                    mock.patch.object(watcher, "ollama_generate", generate), \
                    mock.patch.object(watcher, "SUMMARY_EXTRACT_THINK", False), \
                    mock.patch.object(watcher, "SUMMARY_RECONCILE_THINK", True):
                summary = watcher.summarize(session, transcript)
                second = watcher.summarize(session, transcript)
            evidence = json.loads(
                (transcripts / session / "summary-evidence.json").read_text()
            )
        self.assertEqual(summary, second)
        self.assertEqual(generate.call_count, 4)
        self.assertEqual(
            [call.kwargs["think"] for call in generate.call_args_list],
            [False, False, True, False],
        )
        self.assertFalse(generate.call_args_list[2].kwargs["json_mode"])
        self.assertTrue(generate.call_args_list[3].kwargs["json_mode"])
        self.assertEqual(evidence["items"][0]["type"], "decision")
        self.assertTrue(watcher._valid_summary(summary))

    def test_reconciliation_supersedes_rejected_locale_limit(self):
        transcript = """[14:09:49] Andrii: Давайте візьмемо ікс локалей, не всі п'ятнадцять.
[14:10:13] Oleksandr: Обмежувати смислу немає, робимо паралельно все, що влазить.
"""
        proposed = {
            "type": "proposal",
            "claim": "Обмежити роботу кількома локалями",
            "speaker": "Andrii",
            "owners": [],
            "deadline": "",
            "status": "open",
            "commitment_strength": "not_applicable",
            "confidence": "high",
            "evidence": [{
                "timestamp": "14:09:49",
                "quote": "Давайте візьмемо ікс локалей, не всі п'ятнадцять",
            }],
        }
        current = {
            "type": "decision",
            "claim": "Не встановлювати жорсткого ліміту локалей",
            "speaker": "Oleksandr",
            "owners": [],
            "deadline": "",
            "status": "active",
            "commitment_strength": "not_applicable",
            "confidence": "high",
            "evidence": [{
                "timestamp": "14:10:13",
                "quote": "Обмежувати смислу немає, робимо паралельно все, що влазить",
            }],
        }
        reconciled_proposal = {
            **proposed,
            "status": "superseded",
            "evidence": proposed["evidence"] + current["evidence"],
        }
        critical_raw = json.dumps(
            {"items": [proposed, current]}, ensure_ascii=False
        )
        reconciled_raw = json.dumps(
            {"items": [reconciled_proposal, current]}, ensure_ascii=False
        )
        empty_ledger = json.dumps({"items": []})
        with tempfile.TemporaryDirectory() as directory:
            transcripts = Path(directory) / "transcripts"
            session = "localization"
            (transcripts / session).mkdir(parents=True)
            generate = mock.Mock(side_effect=[
                critical_raw,
                empty_ledger,
                "Пізніша репліка відхиляє початкове обмеження локалей.",
                reconciled_raw,
            ])
            with mock.patch.object(watcher.project_paths, "TRANSCRIPTS", transcripts), \
                    mock.patch.object(watcher, "ollama_generate", generate), \
                    mock.patch.object(watcher, "SUMMARY_EXTRACT_THINK", False), \
                    mock.patch.object(watcher, "SUMMARY_RECONCILE_THINK", True):
                summary = watcher.summarize(session, transcript)
            reasoning_call = generate.call_args_list[2]
            reconcile_call = generate.call_args_list[3]
            evidence = json.loads(
                (transcripts / session / "summary-evidence.json").read_text()
            )

        decisions = watcher._summary_section(summary, "## Рішення")
        self.assertEqual(decisions, "- Не встановлювати жорсткого ліміту локалей")
        self.assertNotIn("Обмежити роботу кількома локалями", decisions)
        self.assertTrue(reasoning_call.kwargs["think"])
        self.assertFalse(reasoning_call.kwargs["json_mode"])
        self.assertFalse(reconcile_call.kwargs["think"])
        self.assertTrue(reconcile_call.kwargs["json_mode"])
        self.assertIn("не всі п'ятнадцять", reconcile_call.args[0])
        self.assertIn("Обмежувати смислу немає", reconcile_call.args[0])
        self.assertEqual(evidence["items"][0]["status"], "superseded")

    def test_failure_is_redacted_and_scheduled_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            failed = root / "failed"
            notes = root / "notes"
            for path in (recordings, failed, notes):
                path.mkdir()
            session = "2026-01-01_120000"
            pipeline_utils.atomic_write_json(
                recordings / f"{session}.json",
                {"status": "recorded", "processing_attempts": 0},
            )
            with mock.patch.object(watcher.project_paths, "RECORDINGS", recordings), \
                 mock.patch.object(watcher.project_paths, "FAILED", failed), \
                 mock.patch.object(watcher.project_paths, "NOTES", notes):
                try:
                    raise RuntimeError("secret hf_SUPERSECRET ntn_NOTIONSECRET")
                except RuntimeError:
                    watcher.handle_failure(session)
            manifest = json.loads((recordings / f"{session}.json").read_text())
            error_file = failed / f"{session}.log"
            self.assertEqual(manifest["status"], "processing_failed")
            self.assertEqual(manifest["processing_attempts"], 1)
            self.assertNotIn("hf_SUPERSECRET", error_file.read_text())
            self.assertNotIn("ntn_NOTIONSECRET", error_file.read_text())
            self.assertEqual(error_file.stat().st_mode & 0o777, 0o600)

    def test_interrupted_processing_stops_after_retry_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recordings, notes = root / "recordings", root / "notes"
            recordings.mkdir()
            notes.mkdir()
            session = "interrupted"
            for track in ("mic", "sys"):
                (recordings / f"{session}_{track}.wav").write_bytes(b"audio")
            pipeline_utils.atomic_write_json(
                recordings / f"{session}.json",
                {
                    "status": "processing",
                    "processing_attempts": watcher.MAX_AUTO_RETRIES,
                },
            )
            with mock.patch.object(watcher.project_paths, "RECORDINGS", recordings), \
                    mock.patch.object(watcher.project_paths, "NOTES", notes), \
                    mock.patch.object(watcher, "AUDIO_PIPELINE_ENABLED", True):
                self.assertEqual(watcher.find_ready_sessions(), [])
            manifest = json.loads(
                (recordings / f"{session}.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "terminal_failed")

    def test_failed_processing_stops_after_retry_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recordings, notes = root / "recordings", root / "notes"
            recordings.mkdir()
            notes.mkdir()
            session = "retry-exhausted"
            for track in ("mic", "sys"):
                (recordings / f"{session}_{track}.wav").write_bytes(b"audio")
            pipeline_utils.atomic_write_json(
                recordings / f"{session}.json",
                {
                    "status": "processing_failed",
                    "processing_attempts": watcher.MAX_AUTO_RETRIES,
                    "next_retry_at": 0,
                },
            )
            with mock.patch.object(watcher.project_paths, "RECORDINGS", recordings), \
                    mock.patch.object(watcher.project_paths, "NOTES", notes), \
                    mock.patch.object(watcher, "AUDIO_PIPELINE_ENABLED", True):
                self.assertEqual(watcher.find_ready_sessions(), [])
            manifest = json.loads(
                (recordings / f"{session}.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "terminal_failed")
            self.assertEqual(manifest["stage"], "retry_limit")

    def test_short_recording_finishes_without_asr_or_llm(self):
        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            transcripts = root / "transcripts"
            notes = root / "notes"
            failed = root / "failed"
            for path in (recordings, transcripts, notes, failed):
                path.mkdir()
            session = "2026-01-01_120000"
            for track in ("mic", "sys"):
                sf.write(recordings / f"{session}_{track}.wav",
                         np.zeros(16_000 * 2, dtype="float32"), 16_000)
            pipeline_utils.atomic_write_json(
                recordings / f"{session}.json", {"status": "recorded"}
            )
            with mock.patch.object(watcher.project_paths, "RECORDINGS", recordings), \
                 mock.patch.object(watcher.project_paths, "TRANSCRIPTS", transcripts), \
                 mock.patch.object(watcher.project_paths, "NOTES", notes), \
                 mock.patch.object(watcher.project_paths, "FAILED", failed), \
                 mock.patch.object(watcher, "MIN_SESSION_SECONDS", 10):
                watcher.process_session(session)
            manifest = json.loads((recordings / f"{session}.json").read_text())
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(len(list(notes.glob("*.md"))), 1)

    def test_digital_silence_finishes_without_asr_or_llm(self):
        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recordings = root / "recordings"
            transcripts = root / "transcripts"
            notes = root / "notes"
            failed = root / "failed"
            for path in (recordings, transcripts, notes, failed):
                path.mkdir()
            session = "2026-01-01_130000"
            for track in ("mic", "sys"):
                sf.write(
                    recordings / f"{session}_{track}.wav",
                    np.zeros(16_000 * 12, dtype="float32"),
                    16_000,
                )
            pipeline_utils.atomic_write_json(
                recordings / f"{session}.json", {"status": "recorded"}
            )
            with mock.patch.object(watcher.project_paths, "RECORDINGS", recordings), \
                 mock.patch.object(watcher.project_paths, "TRANSCRIPTS", transcripts), \
                 mock.patch.object(watcher.project_paths, "NOTES", notes), \
                 mock.patch.object(watcher.project_paths, "FAILED", failed), \
                 mock.patch.object(watcher, "MIN_SESSION_SECONDS", 10), \
                 mock.patch.object(watcher.subprocess, "run") as run:
                watcher.process_session(session)
            manifest = json.loads((recordings / f"{session}.json").read_text())
            self.assertEqual(manifest["stage"], "silent-recording")
            self.assertLess(manifest["signal"]["mic"]["peak_dbfs"], -200)
            self.assertFalse(run.called)
            self.assertIn("Аудіосигнал відсутній", next(notes.glob("*.md")).read_text())


if __name__ == "__main__":
    unittest.main()
