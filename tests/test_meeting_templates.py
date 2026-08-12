from __future__ import annotations

import unittest

import meeting_templates
import summary_pipeline


class MeetingTemplateTests(unittest.TestCase):
    def test_classifies_common_meeting_titles(self):
        cases = {
            "Daily standup": "standup",
            "Планування спринту": "planning",
            "Інтерв’ю з користувачем": "discovery",
            "Design review дашборда": "design_review",
            "1:1 Олег / Марія": "one_on_one",
            "Операційна зустріч": "general",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(
                    meeting_templates.detect_meeting_type(title), expected
                )

    def test_template_uses_only_relevant_evidence(self):
        ledger = {"items": [
            {
                "type": "participant_claim",
                "status": "active",
                "claim": "Користувачеві складно знайти фільтр",
            },
            {
                "type": "hypothesis",
                "status": "active",
                "claim": "Фільтр варто перенести вище",
            },
            {
                "type": "decision",
                "status": "superseded",
                "claim": "Прибрати фільтр",
            },
        ]}
        section = meeting_templates.render_template_section("discovery", ledger)
        self.assertIn("## Інсайти дослідження", section)
        self.assertIn("Користувачеві складно", section)
        self.assertIn("Гіпотеза", section)
        self.assertNotIn("Прибрати фільтр", section)

    def test_general_meeting_has_no_extra_section(self):
        self.assertEqual(
            meeting_templates.render_template_section("general", {"items": []}),
            "",
        )

    def test_specialized_summary_keeps_core_sections_valid(self):
        summary = summary_pipeline._render_grounded_sections(
            summary_pipeline.SUMMARY_TEMPLATE,
            {"items": [{
                "type": "proposal",
                "status": "open",
                "claim": "Скоротити scope першого релізу",
            }]},
            meeting_title="Планування спринту",
        )
        summary += "\n" + meeting_templates.render_template_section(
            "planning",
            {"items": [{
                "type": "proposal",
                "status": "open",
                "claim": "Скоротити scope першого релізу",
            }]},
        )
        self.assertTrue(summary_pipeline._valid_summary(summary, "planning"))
        self.assertFalse(summary_pipeline._valid_summary(summary))


if __name__ == "__main__":
    unittest.main()
