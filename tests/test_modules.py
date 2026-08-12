from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import modules
import record
import audio_pipeline as audio


class ModuleConfigurationTests(unittest.TestCase):
    def test_candidate_module_is_disabled_without_local_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            missing_env = Path(directory) / ".env"
            self.assertEqual(
                modules.module_states(missing_env),
                {"audio": True, "candidates": False, "notion": False},
            )

    def test_defaults_and_env_updates_preserve_unrelated_config(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "# приватний конфіг\nNOTION_API_KEY=secret\n"
                "CANDIDATE_EVALUATION_ENABLED=true # old\n",
                encoding="utf-8",
            )
            self.assertEqual(
                modules.module_states(env_path),
                {"audio": True, "candidates": True, "notion": False},
            )

            modules.update_env(
                {"audio": False, "candidates": False, "notion": True},
                env_path,
            )

            text = env_path.read_text(encoding="utf-8")
            self.assertIn("# приватний конфіг", text)
            self.assertIn("NOTION_API_KEY=secret", text)
            self.assertIn("AUDIO_PIPELINE_ENABLED=false", text)
            self.assertIn("CANDIDATE_EVALUATION_ENABLED=false", text)
            self.assertIn("NOTION_SYNC_ENABLED=true", text)
            self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)

    def test_aliases_and_desired_services(self):
        self.assertEqual(
            modules.resolve_names(["candidate", "recording", "notion"]),
            ["candidates", "audio", "notion"],
        )
        self.assertEqual(
            modules.desired_launch_agents(
                {"audio": False, "candidates": True, "notion": True}
            ),
            {modules.WATCHER_LABEL},
        )
        self.assertIn(
            modules.MIC_LABEL,
            modules.desired_launch_agents(
                {"audio": True, "candidates": False, "notion": False}
            ),
        )

    def test_apply_unloads_audio_agent_but_restarts_watcher(self):
        states = {"audio": False, "candidates": True, "notion": False}
        with mock.patch.object(modules, "_service_loaded", return_value=True), \
                mock.patch.object(modules, "_bootout") as bootout, \
                mock.patch.object(modules, "_bootstrap", return_value=True) as bootstrap, \
                mock.patch.object(modules, "_set_launchctl_enabled") as set_enabled:
            self.assertTrue(modules.apply_modules(states))

        self.assertEqual(
            [call.args[0] for call in bootout.call_args_list],
            [modules.WATCHER_LABEL, modules.MIC_LABEL],
        )
        bootstrap.assert_called_once_with(modules.WATCHER_LABEL)
        self.assertEqual(
            set_enabled.call_args_list,
            [
                mock.call(modules.WATCHER_LABEL, True),
                mock.call(modules.MIC_LABEL, False),
            ],
        )


class AudioModuleGateTests(unittest.TestCase):
    def test_watcher_leaves_audio_queue_untouched_when_disabled(self):
        with mock.patch.object(audio, "AUDIO_PIPELINE_ENABLED", False):
            self.assertEqual(audio.find_ready_sessions(), [])
            with self.assertRaisesRegex(RuntimeError, "Модуль audio вимкнено"):
                audio.process_session("session")

    def test_direct_recorder_exits_before_requesting_permissions(self):
        with mock.patch.object(record, "AUDIO_PIPELINE_ENABLED", False), \
                mock.patch.object(record, "ensure_microphone_permission") as permission:
            with self.assertRaisesRegex(SystemExit, "AUDIO_MODULE_DISABLED"):
                record.main()
        permission.assert_not_called()


if __name__ == "__main__":
    unittest.main()
