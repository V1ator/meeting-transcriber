from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import notion_agent


TEST_OWNER_ENV = {
    "NOTION_TASK_OWNER_NAMES": "Current User,Поточний користувач",
}


class ActionItemParserTests(unittest.TestCase):
    def setUp(self):
        owner_env = mock.patch.dict(notion_agent.os.environ, TEST_OWNER_ENV)
        owner_env.start()
        self.addCleanup(owner_env.stop)

    def test_parses_regular_and_bold_owner_bullets(self):
        note = Path("/tmp/meeting.md")
        text = """# Планування спринту (session)

## Action items
- [Поточний користувач] Підготувати звіт — дедлайн не вказано.
- **[Анна / команда]** Перевірити дані — до п'ятниці.

## Відкриті питання
- —
"""
        items = notion_agent.parse_action_items(note, text=text)
        self.assertEqual([item.involved for item in items], [
            "Поточний користувач", "Анна / команда",
        ])
        self.assertEqual(items[0].name, "Підготувати звіт")
        self.assertEqual(items[1].name, "Перевірити дані — до п'ятниці")
        self.assertEqual(items[0].meeting, "Планування спринту")
        self.assertEqual(
            items[0].source,
            "Дата невідома — Планування спринту",
        )

    def test_expands_nested_actions_with_parent_involved(self):
        note = Path("/tmp/meeting.md")
        text = """# Test

## Action items
- **[Trust & Safety команда]**
    - Перевірити модель.
    - Надіслати follow-up.

## Відкриті питання
- —
"""
        items = notion_agent.parse_action_items(note, text=text)
        self.assertEqual([item.name for item in items], [
            "Перевірити модель",
            "Надіслати follow-up",
        ])
        self.assertTrue(all(
            item.involved == "Trust & Safety команда" for item in items
        ))

    def test_uses_speaker_mapping_and_skips_placeholder(self):
        note = Path("/tmp/meeting.md")
        text = """# Test

## Мапінг спікерів

| Спікер | Ім'я |
|---|---|
| SPEAKER_00 | Марія |

## Action items
- [SPEAKER_00 / Команда] Узгодити план — без чіткого дедлайну
- —

## Відкриті питання
- —
"""
        items = notion_agent.parse_action_items(note, text=text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].involved, "Марія / Команда")
        self.assertEqual(items[0].name, "Узгодити план")

    def test_unknown_involved_marker_becomes_empty_text(self):
        note = Path("/tmp/meeting.md")
        text = """# Test

## Action items
- [—] Узгодити дату наступної зустрічі
"""
        items = notion_agent.parse_action_items(note, text=text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].involved, "")
        self.assertEqual(items[0].name, "Узгодити дату наступної зустрічі")

    def test_source_uses_meeting_date_and_name_metadata(self):
        note = Path("/tmp/2026-07-30_103000.md")
        text = """# Згенерований заголовок

- **Дата:** 2026-07-30
- **Час:** 10:30
- **Назва зустрічі:** Analytics Weekly

## Action items
- [Поточний користувач] Підготувати звіт
"""
        items = notion_agent.parse_action_items(note, text=text)
        self.assertEqual(
            items[0].source,
            "2026-07-30 — Analytics Weekly",
        )

    def test_owner_match_is_distinct_and_supports_joint_ownership(self):
        aliases = ("Current User", "Поточний користувач")
        self.assertTrue(notion_agent.is_current_user_owner(
            "Поточний користувач / команда", owner_names=aliases
        ))
        self.assertTrue(notion_agent.is_current_user_owner(
            "Anna, Current User", owner_names=aliases
        ))
        self.assertFalse(notion_agent.is_current_user_owner(
            "Олеся", owner_names=aliases
        ))
        self.assertFalse(notion_agent.is_current_user_owner(
            "", owner_names=aliases
        ))

    def test_collects_only_configured_owner_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            note = Path(directory) / "meeting.md"
            note.write_text(
                "# Test\n\n## Action items\n"
                "- [Поточний користувач] Моя задача\n"
                "- [Анна] Чужа задача\n"
                "- [—] Непризначена задача\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                notion_agent.os.environ,
                {"NOTION_TASK_OWNER_NAMES": "Current User,Поточний користувач"},
            ):
                items = notion_agent.collect_action_items([note])
        self.assertEqual([item.name for item in items], ["Моя задача"])

    def test_structured_ledger_matches_rendered_action_item_semantics(self):
        note = Path("/tmp/2026-08-11_meeting.md")
        text = """# Планування

- **Дата:** 2026-08-11
- **Назва зустрічі:** Product Weekly

## Мапінг спікерів
| Спікер | Ім'я |
|---|---|
| SPEAKER_00 | Поточний користувач |
"""
        ledger = {"items": [
            {
                "type": "commitment",
                "status": "open",
                "claim": "Підготувати звіт",
                "owners": ["SPEAKER_00"],
                "deadline": "п'ятниця",
                "commitment_strength": "soft",
            },
            {
                "type": "commitment",
                "status": "completed",
                "claim": "Завершена задача",
                "owners": ["SPEAKER_00"],
            },
        ]}
        items = notion_agent.action_items_from_ledger(note, ledger, text=text)
        self.assertEqual(len(items), 1)
        self.assertEqual(
            items[0].name,
            "Спробувати підготувати звіт — дедлайн: п'ятниця",
        )
        self.assertEqual(items[0].involved, "Поточний користувач")
        self.assertEqual(items[0].source, "2026-08-11 — Product Weekly")


class SyncTests(unittest.TestCase):
    def setUp(self):
        owner_env = mock.patch.dict(notion_agent.os.environ, TEST_OWNER_ENV)
        owner_env.start()
        self.addCleanup(owner_env.stop)

    def test_candidate_feedback_task_has_stable_identity(self):
        first = notion_agent.evaluation_feedback_item(
            Path("/tmp/Jane_2026-07-31.md"),
            candidate="Jane Doe",
            meeting_title="Interview | Jane Doe",
            meeting_date="2026-07-31",
            evaluation_id="session-1",
        )
        second = notion_agent.evaluation_feedback_item(
            Path("/tmp/renamed.md"),
            candidate="Jane Doe",
            meeting_title="Interview | Jane Doe",
            meeting_date="2026-07-31",
            evaluation_id="session-1",
        )
        self.assertEqual(first.name, "Дати фідбек по кандидату: Jane Doe")
        self.assertEqual(first.source, "2026-07-31 — Interview | Jane Doe")
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.kind, "candidate-feedback")
        self.assertEqual(first.involved, "Current User")

    def test_create_task_always_sets_inbox_status(self):
        config = notion_agent.NotionConfig(
            enabled=True,
            api_key="secret",
            data_source_id="data-source",
        )
        client = notion_agent.NotionClient(config)
        item = notion_agent.ActionItem(
            note=Path("/tmp/meeting.md"),
            meeting="Meeting",
            source="2026-07-30 — Meeting",
            name="Підготувати звіт",
            involved="Поточний користувач",
            fingerprint="fingerprint",
        )
        with mock.patch.object(
            client, "_request", return_value={"id": "page-1"}
        ) as request:
            client.create_task(item)

        payload = request.call_args.args[2]
        self.assertEqual(
            payload["properties"]["Status"],
            {"status": {"name": "Inbox"}},
        )
        self.assertEqual(
            payload["properties"]["Source"]["rich_text"][0]["text"]["content"],
            "2026-07-30 — Meeting",
        )

    def test_trashes_only_non_owner_tasks_from_selected_note(self):
        class FakeClient:
            def __init__(self):
                self.trashed = []

            def trash_page(self, page_id):
                self.trashed.append(page_id)
                return {"id": page_id, "in_trash": True}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcripts = root / "transcripts"
            transcripts.mkdir()
            state_path = transcripts / ".notion-sync-state.json"
            lock_path = transcripts / ".notion-sync.lock"
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "tasks": {
                    "mine": {
                        "page_id": "page-mine", "note": "meeting.md",
                        "involved": "Поточний користувач", "name": "mine",
                    },
                    "other": {
                        "page_id": "page-other", "note": "meeting.md",
                        "involved": "Anna", "name": "other",
                    },
                    "unassigned": {
                        "page_id": "page-empty", "note": "meeting.md",
                        "involved": "", "name": "empty",
                    },
                    "different-note": {
                        "page_id": "page-different", "note": "other.md",
                        "involved": "Anna", "name": "different",
                    },
                },
            }), encoding="utf-8")
            client = FakeClient()
            with (
                mock.patch.object(notion_agent, "TRANSCRIPTS", transcripts),
                mock.patch.object(notion_agent, "STATE_PATH", state_path),
                mock.patch.object(notion_agent, "LOCK_PATH", lock_path),
                mock.patch.dict(
                    notion_agent.os.environ,
                    {"NOTION_TASK_OWNER_NAMES": "Current User,Поточний користувач"},
                ),
            ):
                count = notion_agent.trash_non_owner_tasks_for_note(
                    Path("/tmp/meeting.md"), client=client
                )
                saved = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(count, 2)
        self.assertEqual(client.trashed, ["page-other", "page-empty"])
        self.assertNotIn("trashed_at", saved["tasks"]["mine"])
        self.assertTrue(saved["tasks"]["other"]["in_trash"])
        self.assertTrue(saved["tasks"]["unassigned"]["in_trash"])
        self.assertNotIn("trashed_at", saved["tasks"]["different-note"])

    def test_http_client_retries_rate_limit(self):
        config = notion_agent.NotionConfig(True, "secret", "source")
        client = notion_agent.NotionClient(config)

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"ok": true}'

        limited = urllib.error.HTTPError(
            "https://api.notion.test", 429, "rate limited",
            {"Retry-After": "0"}, io.BytesIO(b'{"message":"slow down"}'),
        )
        with mock.patch.object(
            client._opener, "open", side_effect=[limited, Response()]
        ) as opener, mock.patch.object(notion_agent.time, "sleep"):
            self.assertEqual(client._request("GET", "/test"), {"ok": True})
        self.assertEqual(opener.call_count, 2)

    def test_notion_client_blocks_redirects(self):
        config = notion_agent.NotionConfig(True, "secret", "source")
        client = notion_agent.NotionClient(config)
        request = urllib.request.Request(
            "https://api.notion.com/v1/pages",
            headers={"Authorization": "Bearer secret"},
        )
        with self.assertRaisesRegex(urllib.error.HTTPError, "redirect заблоковано"):
            notion_agent._NoRedirectHandler().redirect_request(
                request,
                io.BytesIO(),
                302,
                "Found",
                {"Location": "https://evil.example/steal"},
                "https://evil.example/steal",
            )

    def test_corrupt_state_recovers_from_backup_without_losing_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / ".notion-sync-state.json"
            backup_path = Path(directory) / ".notion-sync-state.backup.json"
            state_path.write_text("{broken", encoding="utf-8")
            backup = {"schema_version": 1, "tasks": {"fingerprint": {"page_id": "1"}}}
            backup_path.write_text(json.dumps(backup), encoding="utf-8")
            with mock.patch.object(notion_agent, "STATE_PATH", state_path):
                restored = notion_agent._load_state()
            self.assertIn("fingerprint", restored["tasks"])
            self.assertEqual(json.loads(state_path.read_text()), backup)
            self.assertEqual(len(list(Path(directory).glob("*.corrupt-*.json"))), 1)

    def test_corrupt_state_without_backup_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / ".notion-sync-state.json"
            state_path.write_text("{broken", encoding="utf-8")
            with mock.patch.object(notion_agent, "STATE_PATH", state_path):
                with self.assertRaisesRegex(notion_agent.NotionSyncError, "Пошкоджено"):
                    notion_agent._load_state()

    def test_failed_candidate_feedback_is_queued_and_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pending_path = root / ".notion-pending.json"
            config = notion_agent.NotionConfig(True, "secret", "source")
            with mock.patch.object(
                notion_agent.NotionConfig, "from_env", return_value=config
            ), mock.patch.object(
                notion_agent, "PENDING_PATH", pending_path
            ), mock.patch.object(
                notion_agent, "sync_items", side_effect=RuntimeError("offline")
            ):
                notion_agent.sync_evaluation_feedback_if_enabled(
                    root / "Jane.md", candidate="Jane",
                    meeting_title="Interview | Jane", meeting_date="2026-08-01",
                    evaluation_id="session-1", logger=lambda _: None,
                )
            queued = json.loads(pending_path.read_text(encoding="utf-8"))
            self.assertEqual(len(queued["items"]), 1)

            empty_result = notion_agent.SyncResult(0, 0, 0, 0)
            with mock.patch.object(
                notion_agent.NotionConfig, "from_env", return_value=config
            ), mock.patch.object(
                notion_agent, "PENDING_PATH", pending_path
            ), mock.patch.object(
                notion_agent, "sync_notes", return_value=(empty_result, [])
            ), mock.patch.object(
                notion_agent, "sync_items", return_value=(
                    notion_agent.SyncResult(1, 1, 1, 0), []
                )
            ):
                self.assertEqual(
                    notion_agent.retry_deferred_if_enabled(now=10**12), 1
                )
            queued = json.loads(pending_path.read_text(encoding="utf-8"))
            self.assertEqual(queued["items"], {})

    def test_sync_writes_state_and_does_not_create_duplicates(self):
        class FakeClient:
            def __init__(self):
                self.validations = 0
                self.created = []

            def validate_schema(self):
                self.validations += 1

            def create_task(self, item):
                self.created.append(item)
                return {
                    "id": f"page-{len(self.created)}",
                    "url": "https://notion.test/page",
                }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notes = root / "notes"
            transcripts = root / "transcripts"
            notes.mkdir()
            note = notes / "meeting.md"
            note.write_text(
                "# Test\n\n## Action items\n"
                "- [Поточний користувач] Підготувати звіт — дедлайн не вказано\n\n"
                "## Відкриті питання\n- —\n",
                encoding="utf-8",
            )
            client = FakeClient()
            config = notion_agent.NotionConfig(
                enabled=True,
                api_key="secret",
                data_source_id="data-source",
            )
            with (
                mock.patch.object(notion_agent, "TRANSCRIPTS", transcripts),
                mock.patch.object(
                    notion_agent, "STATE_PATH",
                    transcripts / ".notion-sync-state.json",
                ),
                mock.patch.object(
                    notion_agent, "LOCK_PATH",
                    transcripts / ".notion-sync.lock",
                ),
            ):
                first, _ = notion_agent.sync_notes(
                    [note], config=config, client=client
                )
                second, _ = notion_agent.sync_notes(
                    [note], config=config, client=client
                )

            self.assertEqual(first.created, 1)
            self.assertEqual(second.created, 0)
            self.assertEqual(second.skipped, 1)
            self.assertEqual(len(client.created), 1)
            self.assertEqual(client.validations, 1)

    def test_dry_run_never_calls_notion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcripts = root / "transcripts"
            note = root / "meeting.md"
            note.write_text(
                "# Test\n\n## Action items\n- [Поточний користувач] Зробити тест\n",
                encoding="utf-8",
            )
            with (
                mock.patch.object(notion_agent, "TRANSCRIPTS", transcripts),
                mock.patch.object(
                    notion_agent, "STATE_PATH",
                    transcripts / ".notion-sync-state.json",
                ),
                mock.patch.object(
                    notion_agent, "LOCK_PATH",
                    transcripts / ".notion-sync.lock",
                ),
            ):
                result, pending = notion_agent.sync_notes([note], dry_run=True)

            self.assertEqual(result.pending, 1)
            self.assertEqual(result.created, 0)
            self.assertEqual(len(pending), 1)


if __name__ == "__main__":
    unittest.main()
