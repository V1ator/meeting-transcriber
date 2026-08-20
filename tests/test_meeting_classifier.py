import unittest

import meeting_classifier


class CandidateInterviewClassifierTests(unittest.TestCase):
    def test_early_terminated_interview_is_not_evaluation_eligible(self):
        transcript = """[10:00:00] Jane Doe: Привіт
[10:02:00] Jane Doe: Дякую, але не хочу продовжувати співбесіду.
[10:02:15] Interviewer: Зрозуміло, дякую за розмову.
"""
        result = meeting_classifier.classify_candidate_interview(
            "Hiring manager Interview | Jane Doe | Data Analyst",
            transcript,
        )
        self.assertEqual(result["outcome"], "early_terminated")
        self.assertFalse(result["candidate_evaluation_eligible"])
        self.assertEqual(result["candidate_last_timestamp"], "10:02:00")
        self.assertTrue(result["evidence"])

    def test_completed_interview_remains_evaluation_eligible(self):
        transcript = "\n".join(
            ["[10:00] Jane Doe: Розповім про свій проєкт і власний внесок."]
            + [
                f"[10:{index:02d}] Jane Doe: Приклад {index} з деталями та результатом роботи."
                for index in range(1, 8)
            ]
            + ["[10:09] Interviewer: Дякую, переходимо до наступного етапу."]
        )
        result = meeting_classifier.classify_candidate_interview(
            "Interview | Jane Doe | Senior Data Analyst",
            transcript,
        )
        self.assertEqual(result["outcome"], "completed")
        self.assertTrue(result["candidate_evaluation_eligible"])

    def test_transliterated_title_matches_cyrillic_caption_speaker(self):
        transcript = "\n".join(
            [
                f"[10:{index:02d}] Катерина Лисенко: Детальна відповідь {index} "
                "про власний досвід, рішення, результат і зроблені висновки."
                for index in range(1, 8)
            ]
            + ["[10:09] Oleh Parandii: Дякую, співбесіду завершено."]
        )
        result = meeting_classifier.classify_candidate_interview(
            "Hiring manager Interview | Kateryna Lysenko | Data Analyst",
            transcript,
        )
        self.assertEqual(result["outcome"], "completed")
        self.assertTrue(result["candidate_evaluation_eligible"])
        self.assertEqual(result["candidate_last_timestamp"], "10:07")
        self.assertTrue(
            meeting_classifier.same_person("Катерина Лисенко", "Kateryna Lysenko")
        )

    def test_initial_ye_transliteration_variants_match_with_exact_surname(self):
        self.assertTrue(
            meeting_classifier.same_person("Євгеній Мартиненко", "Evhenii Martynenko")
        )
        self.assertTrue(
            meeting_classifier.same_person("Євгеній Мартиненко", "Yevhenii Martynenko")
        )
        self.assertFalse(
            meeting_classifier.same_person("Євгеній Інший", "Evhenii Martynenko")
        )

    def test_meet_reversed_cyrillic_name_matches_title(self):
        self.assertTrue(
            meeting_classifier.same_person("Крочак Сергій", "Serhii Krochak")
        )
        self.assertFalse(
            meeting_classifier.same_person("Інший Сергій", "Serhii Krochak")
        )

    def test_evhenii_interview_is_evaluation_eligible(self):
        transcript = "\n".join(
            [
                f"[10:{index:02d}] Євгеній Мартиненко: Детальна відповідь {index} "
                "про досвід, власні дії, отриманий результат і висновки."
                for index in range(1, 8)
            ]
            + ["[10:09] Oleh Parandii: Дякую, співбесіду завершено."]
        )
        result = meeting_classifier.classify_candidate_interview(
            "Bar-Raising Interview | Evhenii Martynenko | BI Analyst",
            transcript,
        )
        self.assertEqual(result["outcome"], "completed")
        self.assertTrue(result["candidate_evaluation_eligible"])
        self.assertEqual(result["candidate_last_timestamp"], "10:07")

    def test_long_interview_with_unresolved_identity_requires_review(self):
        transcript = "\n".join(
            [
                f"[10:{index:02d}] Невідомий Спікер: Детальна відповідь {index} "
                + "із контекстом, власними діями, результатом і висновками. " * 4
                for index in range(1, 9)
            ]
        )
        result = meeting_classifier.classify_candidate_interview(
            "Interview | Jane Doe | Data Analyst",
            transcript,
        )
        self.assertEqual(result["outcome"], "identity_unresolved")
        self.assertFalse(result["candidate_evaluation_eligible"])

    def test_too_few_candidate_answers_are_insufficient(self):
        result = meeting_classifier.classify_candidate_interview(
            "Interview | Jane Doe | Data Analyst",
            "[10:00] Interviewer: Привіт.\n[10:01] Jane Doe: Привіт.",
        )
        self.assertEqual(result["outcome"], "insufficient_content")
        self.assertFalse(result["candidate_evaluation_eligible"])

    def test_one_early_stop_phrase_does_not_cancel_a_full_interview(self):
        answers = [
            f"[10:{index:02d}] Jane Doe: Детальна відповідь {index} "
            + "із контекстом власними діями складним рішенням результатом і висновками. " * 4
            for index in range(1, 8)
        ]
        answers.insert(3, "[10:04] Jane Doe: У тому експерименті ми вирішили не продовжувати старий підхід.")
        answers.append("[10:10] Interviewer: Дякую, співбесіду завершено.")
        result = meeting_classifier.classify_candidate_interview(
            "Interview | Jane Doe | Senior Data Analyst",
            "\n".join(answers),
        )
        self.assertEqual(result["outcome"], "completed")
        self.assertTrue(result["candidate_evaluation_eligible"])


if __name__ == "__main__":
    unittest.main()
