from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import meeting_quality


class MeetingQualityTests(unittest.TestCase):
    def test_healthy_meet_capture_is_high_quality(self):
        export = {
            "startedAt": "2026-08-12T08:00:00Z",
            "endedAt": "2026-08-12T08:30:00Z",
            "captureHealth": {
                "rtcOpenedAtMs": 1_200,
                "decodedCaptions": 2,
            },
        }
        entries = [
            {"kind": "caption", "speaker": "Олег", "start_ms": 1_000},
            {"kind": "caption", "speaker": "Марія", "start_ms": 3_000},
        ]
        report = meeting_quality.assess_meet_capture(export, entries)
        self.assertEqual(report["status"], "high")
        self.assertEqual(report["metrics"]["caption_turns"], 2)
        self.assertEqual(report["metrics"]["duration_seconds"], 1_800)

    def test_recovered_disconnect_and_unknown_speaker_need_attention(self):
        export = {
            "captureHealth": {
                "rtcOpenedAtMs": 1_000,
                "hadRtcUnavailable": True,
                "recovered": True,
                "disconnectCount": 1,
            },
        }
        entries = [
            {"kind": "caption", "speaker": "Учасник 116", "start_ms": 1_000},
            {"kind": "caption", "speaker": "Олег", "start_ms": 2_000},
            {"kind": "caption", "speaker": "Марія", "start_ms": 3_000},
            {"kind": "caption", "speaker": "Олег", "start_ms": 4_000},
        ]
        report = meeting_quality.assess_meet_capture(export, entries)
        self.assertEqual(report["status"], "medium")
        self.assertEqual(
            {issue["code"] for issue in report["issues"]},
            {"rtc_recovered", "rtc_disconnects", "unknown_speakers"},
        )

    def test_missing_captions_and_unrecovered_rtc_require_review(self):
        report = meeting_quality.assess_meet_capture(
            {"captureHealth": {"hadRtcUnavailable": True}}, []
        )
        self.assertEqual(report["status"], "review")
        self.assertEqual(report["status_label"], "Потребує перевірки")

    def test_diagnostic_capture_does_not_require_summary_quality(self):
        with tempfile.TemporaryDirectory() as directory:
            transcripts = Path(directory)
            session = "diagnostic"
            work = transcripts / session
            work.mkdir()
            capture = meeting_quality.assess_meet_capture(
                {
                    "captureHealth": {
                        "rtcPackets": 1,
                        "decodeFailures": 1,
                        "hadRtcUnavailable": True,
                    }
                },
                [],
            )
            (work / "manifest.json").write_text(
                json.dumps({"capture_quality": capture}), encoding="utf-8"
            )
            with mock.patch.object(
                meeting_quality.project_paths, "TRANSCRIPTS", transcripts
            ):
                report = meeting_quality.finalize_quality_report(
                    session, summary_expected=False
                )
            codes = {item["code"] for item in report["issues"]}
            self.assertIn("decode_failures", codes)
            self.assertNotIn("summary_quality_missing", codes)

    def test_final_report_combines_capture_and_summary_qa(self):
        with tempfile.TemporaryDirectory() as directory:
            transcripts = Path(directory)
            session = "meeting"
            work = transcripts / session
            work.mkdir()
            capture = {
                "source": "google-meet-live-captions",
                "status": "high",
                "issues": [],
            }
            (work / "manifest.json").write_text(
                json.dumps({"capture_quality": capture}), encoding="utf-8"
            )
            (work / "summary-evidence.json").write_text(
                json.dumps({
                    "_quality": {
                        "status": "pass",
                        "errors": [],
                        "warnings": [
                            {"code": "missing_owner", "claim": "Перша дія"},
                            {"code": "missing_owner", "claim": "Друга дія"},
                            {"code": "missing_owner", "claim": "Третя дія"},
                        ],
                    }
                }),
                encoding="utf-8",
            )
            with mock.patch.object(
                meeting_quality.project_paths, "TRANSCRIPTS", transcripts
            ):
                report = meeting_quality.finalize_quality_report(session)
            self.assertEqual(report["status"], "medium")
            self.assertEqual(
                [issue["code"] for issue in report["issues"]],
                ["missing_owner"],
            )
            rendered = "\n".join(meeting_quality.report_note_lines(report))
            self.assertEqual(
                rendered.count("У деяких action items не визначено відповідального"),
                1,
            )
            self.assertTrue((work / "quality-report.json").is_file())


if __name__ == "__main__":
    unittest.main()
