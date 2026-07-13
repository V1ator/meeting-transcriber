from __future__ import annotations

import io
import json
import struct
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

import pipeline_utils
import mic_watch
import record
import transcribe
import watch_and_process as watcher


class AtomicIOTests(unittest.TestCase):
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


class SilenceMonitorTests(unittest.TestCase):
    def test_aec_buffer_meter_reads_pyobjc_pointer(self):
        import ctypes

        try:
            import AVFAudio as AV
        except ImportError:
            import AVFoundation as AV
        fmt = AV.AVAudioFormat.alloc().initStandardFormatWithSampleRate_channels_(
            48_000, 2
        )
        buffer = AV.AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(fmt, 16)
        buffer.setFrameLength_(16)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            pointer = buffer.floatChannelData()
        channels = ctypes.cast(
            pointer.pointerAsInteger,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_float)),
        )
        for index in range(16):
            channels[0][index] = 0.0
            channels[1][index] = 0.5
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.assertAlmostEqual(record.av_buffer_rms_dbfs(buffer), -6.0206, places=3)

    def test_aec_finalizer_selects_loudest_file_channel(self):
        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aec.wav"
            audio = np.column_stack([
                np.zeros(16_000, dtype="float32"),
                np.full(16_000, 0.25, dtype="float32"),
                np.full(16_000, 0.05, dtype="float32"),
            ])
            sf.write(path, audio, 16_000, subtype="FLOAT")
            selected, levels = record._loudest_file_channel(path)
            self.assertEqual(selected, 1)
            self.assertGreater(levels[1], levels[2])

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
            self.assertTrue(record.ask_finish_after_silence(stop_event))

        process.stdout = io.StringIO(
            "button returned:Завершити запис, gave up:true\n"
        )
        with mock.patch.object(record.subprocess, "Popen", return_value=process):
            stop_event = mock.Mock(is_set=lambda: False)
            self.assertFalse(record.ask_finish_after_silence(stop_event))


class MicrophoneModeTests(unittest.TestCase):
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
        with mock.patch.dict(record.os.environ, {"RECORD_AEC": "true"}):
            self.assertTrue(record.resolve_aec_mode([]))
            self.assertFalse(record.resolve_aec_mode(["--raw"]))
        with mock.patch.dict(record.os.environ, {"RECORD_AEC": "false"}):
            self.assertFalse(record.resolve_aec_mode([]))
            self.assertTrue(record.resolve_aec_mode(["--aec"]))

    def test_conflicting_or_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            record.resolve_aec_mode(["--aec", "--raw"])
        with self.assertRaises(ValueError):
            record.resolve_aec_mode(["--other"])

    def test_start_popup_returns_selected_mode(self):
        cases = {
            "button returned:Навушники, gave up:false": "--raw",
            "button returned:Динаміки, gave up:false": "--aec",
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


class WatcherStateTests(unittest.TestCase):
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
            with mock.patch.object(watcher, "NOTES", notes), \
                 mock.patch.object(watcher, "TRANSCRIPTS", transcripts):
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
            with mock.patch.object(watcher, "NOTES", notes), \
                 mock.patch.object(watcher, "TRANSCRIPTS", transcripts):
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
            with mock.patch.object(watcher, "RECORDINGS", recordings), \
                 mock.patch.object(watcher, "NOTES", notes), \
                 mock.patch.object(watcher, "FAILED", failed):
                self.assertEqual(watcher.find_ready_sessions(), [])
                pipeline_utils.atomic_write_json(manifest, {"status": "recorded"})
                self.assertEqual(watcher.find_ready_sessions(), [session])

    def test_summary_structure_validation(self):
        complete = "\n".join(watcher.REQUIRED_HEADINGS)
        self.assertTrue(watcher._valid_summary(complete))
        self.assertFalse(watcher._valid_summary("## TL;DR\nтільки одна секція"))

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
            with mock.patch.object(watcher, "RECORDINGS", recordings), \
                 mock.patch.object(watcher, "FAILED", failed), \
                 mock.patch.object(watcher, "NOTES", notes):
                try:
                    raise RuntimeError("secret hf_SUPERSECRET")
                except RuntimeError:
                    watcher.handle_failure(session)
            manifest = json.loads((recordings / f"{session}.json").read_text())
            error_file = failed / f"{session}.log"
            self.assertEqual(manifest["status"], "processing_failed")
            self.assertEqual(manifest["processing_attempts"], 1)
            self.assertNotIn("hf_SUPERSECRET", error_file.read_text())
            self.assertEqual(error_file.stat().st_mode & 0o777, 0o600)

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
            with mock.patch.object(watcher, "RECORDINGS", recordings), \
                 mock.patch.object(watcher, "TRANSCRIPTS", transcripts), \
                 mock.patch.object(watcher, "NOTES", notes), \
                 mock.patch.object(watcher, "FAILED", failed), \
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
            with mock.patch.object(watcher, "RECORDINGS", recordings), \
                 mock.patch.object(watcher, "TRANSCRIPTS", transcripts), \
                 mock.patch.object(watcher, "NOTES", notes), \
                 mock.patch.object(watcher, "FAILED", failed), \
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
