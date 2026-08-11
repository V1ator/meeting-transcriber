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
