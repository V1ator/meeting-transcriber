import tempfile
import unittest
from pathlib import Path
from unittest import mock

import candidate_evaluation
import candidate_report_validator


def valid_ukrainian_report(
    *,
    status="Попереднє",
    stage="Невідомий",
    target_fit="Не застосовується",
    stage_recommendation="Продовжити",
    final_recommendation="Не застосовується — рішення попереднє",
    critical_verified="Не визначено",
    target_role="Не задано",
    target_level="Не задано",
):
    body = (
        "Кандидат продемонстрував достатню мотивацію, самостійність і здатність "
        "аналізувати неоднозначні задачі. Висновок ґрунтується лише на наведених "
        "цитатах і не використовує назву посади чи роки досвіду як заміну доказам. "
    ) * 3
    fields = f"""# Оцінка кандидата: Jane Doe

**Статус рішення:** {status}
**Етап співбесіди:** {stage}
**Продемонстрований рівень:** Рівень не визначено (Низька впевненість)
**Сигнали наступного рівня:** Не виявлено
**Цільова роль:** {target_role}
**Цільовий рівень:** {target_level}
**Відповідність цільовому рівню:** {target_fit}
**Рекомендація поточного етапу:** {stage_recommendation}
**Фінальна рекомендація щодо найму:** {final_recommendation}
**Критичні компетенції перевірено:** {critical_verified}
**Інтерв’юери:** Interviewer
"""
    return (
        fields + "\n" + "\n".join(candidate_evaluation.REQUIRED_REPORT_HEADINGS)
        + "\n" + body
    ).strip()


class CandidateRoutingTests(unittest.TestCase):
    def test_keywords_are_case_insensitive_but_not_generic_meetings(self):
        self.assertTrue(candidate_evaluation.is_candidate_meeting("Hiring | Jane Doe"))
        self.assertTrue(candidate_evaluation.is_candidate_meeting("NETWORKING: John"))
        self.assertFalse(candidate_evaluation.is_candidate_meeting("Weekly product sync"))
        self.assertFalse(candidate_evaluation.is_candidate_meeting("preinterview sync"))

    def test_automatic_routing_requires_explicit_candidate_structure(self):
        self.assertEqual(
            candidate_evaluation.explicit_candidate_name("Interview | Jane Doe"),
            "Jane Doe",
        )
        self.assertEqual(
            candidate_evaluation.explicit_candidate_name(
                "Meet - Hiring manager Interview | Stanislav Matkivskyi | Data Analyst"
            ),
            "Stanislav Matkivskyi",
        )
        self.assertEqual(
            candidate_evaluation.explicit_candidate_name("networking infra sync"),
            "",
        )
        self.assertEqual(
            candidate_evaluation.explicit_candidate_name(
                "Customer interview — churn research"
            ),
            "",
        )
        self.assertEqual(
            candidate_evaluation.candidate_name_from_title(
                "Customer interview — churn research"
            ),
            "",
        )

    def test_candidate_name_is_taken_from_title(self):
        self.assertEqual(
            candidate_evaluation.candidate_name_from_title("Interview | Jane Doe"),
            "Jane Doe",
        )
        self.assertEqual(
            candidate_evaluation.candidate_name_from_title("Hiring: Ірина Коваль"),
            "Ірина Коваль",
        )
        self.assertEqual(
            candidate_evaluation.candidate_name_from_title(
                "Meet - Hiring manager Interview | Stanislav Matkivskyi | Data Analyst"
            ),
            "Stanislav Matkivskyi",
        )

    def test_target_level_can_be_encoded_in_title(self):
        title = "Interview | Jane Doe | Senior Data Engineer"
        self.assertEqual(
            candidate_evaluation.candidate_name_from_title(title), "Jane Doe"
        )
        self.assertEqual(
            candidate_evaluation.target_level_from_title(title),
            "Senior Data Engineer",
        )
        self.assertEqual(
            candidate_evaluation.target_level_from_title(
                "Interview | Jane Doe | Data Analyst"
            ),
            "Data Analyst",
        )
        self.assertEqual(
            candidate_evaluation.split_target_role_level(
                "Senior Data Engineer", ("Junior", "Middle", "Senior")
            ),
            ("Data Engineer", "Senior"),
        )
        self.assertEqual(
            candidate_evaluation.split_target_role_level(
                "Data Analyst", ("Junior", "Middle", "Senior")
            ),
            ("Data Analyst", ""),
        )
        self.assertEqual(
            candidate_evaluation.split_target_role_level(
                "Middle/Senior Data Analyst", ("Junior", "Middle", "Senior")
            ),
            ("Middle/Senior Data Analyst", ""),
        )
        self.assertFalse(
            candidate_report_validator.has_explicit_target_level(
                "Middle/Senior Data Analyst", ("Junior", "Middle", "Senior")
            )
        )

    def test_interview_stage_can_be_explicit_or_inferred(self):
        self.assertEqual(
            candidate_evaluation.interview_stage_from_title(
                "Interview | Jane Doe | Junior Analyst | Final"
            ),
            "Final",
        )
        self.assertEqual(
            candidate_evaluation.interview_stage_from_title(
                "Hiring manager Interview | Jane Doe | Junior Analyst"
            ),
            "Hiring Manager",
        )
        self.assertEqual(
            candidate_evaluation.interview_stage_from_title(
                "Interview | Jane Doe | Junior Analyst"
            ),
            "Невідомий",
        )

    def test_participant_fallback_requires_one_unambiguous_person(self):
        with mock.patch.dict(
            candidate_evaluation.os.environ,
            {"CANDIDATE_INTERVIEWER_NAMES": "Interviewer, Anna"},
        ):
            self.assertEqual(
                candidate_evaluation.candidate_name(
                    "Networking conversation", ["Interviewer", "Jane Doe"]
                ),
                "Jane Doe",
            )
            self.assertEqual(
                candidate_evaluation.candidate_name(
                    "Networking conversation", ["Interviewer", "Jane Doe", "John Doe"]
                ),
                "",
            )


class CandidateEvaluationTests(unittest.TestCase):
    def test_evidence_grounding_repairs_ellipsis_at_one_timestamp(self):
        transcript = (
            "[13:21:38] Jane: Я зробила систему для контролю витрат і вона "
            "допомогла команді уникнути зайвих платежів."
        )
        evidence = """[E1.1] speaker=candidate | type=candidate_self_report | situation=impact | timestamp=13:21:38 | dimensions=Experience relevance | signals=impact
"Я зробила систему для контролю витрат ... уникнути зайвих платежів"
"""
        grounded, dropped = candidate_evaluation._ground_evidence_ledger(
            evidence, transcript
        )
        self.assertEqual(dropped, [])
        self.assertIn(
            "я зробила систему для контролю витрат і вона допомогла команді "
            "уникнути зайвих платежів",
            grounded,
        )

    def test_evidence_grounding_drops_mismatch_and_cross_turn_stitch(self):
        transcript = "\n".join([
            "[13:07:34] Jane: Я працювала з Google інструментами.",
            "[13:32:54] Jane: Інкрементально оновлюємо лише нові рядки.",
            "[13:33:13] Jane: Це потрібно для ефективності.",
        ])
        evidence = """[E1.1] speaker=candidate | type=candidate_self_report | situation=tools | timestamp=13:07:34 | dimensions=Motivation | signals=
"Я працювала з Gogole інструментами"

[E1.2] speaker=candidate | type=candidate_live | situation=dbt | timestamp=13:32:54 | dimensions=Critical thinking | signals=
"Інкрементально оновлюємо лише нові рядки ... Це потрібно для ефективності"
"""
        grounded, dropped = candidate_evaluation._ground_evidence_ledger(
            evidence, transcript
        )
        self.assertEqual(grounded, "")
        self.assertEqual(dropped, ["1.1", "1.2"])

    def test_report_normalization_grounds_quotes_caps_confidence_and_ids(self):
        evidence = """[E1.1] speaker=candidate | type=candidate_self_report | situation=case_one | timestamp=00:01:00 | dimensions=Motivation | signals=impact
"Це точна цитата кандидата"

[E1.2] speaker=candidate | type=candidate_live | situation=case_two | timestamp=00:02:00 | dimensions=Motivation | signals=impact
"Друга точна цитата"
"""
        report = """| Мотивація | 3 | Висока | опис |

### 1. Мотивація — 3 (Висока)
> [E1.1] «Неточний переказ» (00:01:00)
> [E1.2] «Друга точна цитата» (00:02:00)

## Основні ризики найму
| Ризик | Статус | Доказ і умова прояву |
|---|---|---|
| Ризик | Умовний | E1.1: умова |
"""
        normalized = candidate_evaluation._normalize_generated_report(
            report, evidence
        )
        self.assertIn("| Мотивація | 3 | Середня |", normalized)
        self.assertIn("Мотивація — 3 (Середня)", normalized)
        self.assertIn("«Це точна цитата кандидата»", normalized)
        self.assertIn("[E1.1]: умова", normalized)
        self.assertIn("## Заходи зниження ризиків", normalized)

    def test_report_normalization_splits_grouped_evidence_ids(self):
        report = (
            "| Ризик | Умовний висновок | [E2.1, E2.3] опис; "
            "E1.4 додатково; [E4.]7] legacy; [E5.1, [E5.2]; "
            "[E6.1]2]; Analytics [Engineer] |"
        )
        normalized = candidate_evaluation._normalize_report_evidence_ids(report)
        self.assertEqual(
            normalized,
            "| Ризик | Умовний висновок | [E2.1], [E2.3] опис; "
            "[E1.4] додатково; [E4.7] legacy; [E5.1], [E5.2]; "
            "[E6.12]; Analytics Engineer |",
        )
        self.assertNotIn("[E2.]3", normalized)

    def test_report_normalization_renames_risk_mitigation_alias(self):
        report = "## Мітигації ризиків\n\n- Перевірити кейсом."
        normalized = candidate_evaluation._ensure_risk_mitigation_section(report)
        self.assertIn("## Заходи зниження ризиків", normalized)
        self.assertNotIn("## Мітигації ризиків", normalized)

    def test_single_evidence_chunk_skips_lossy_consolidation(self):
        part = """[E1.1] speaker=candidate | type=candidate_live | situation=case | timestamp=00:01:00 | dimensions=motivation | signals=none
"Дослівна цитата кандидата"
"""
        generate = mock.Mock()
        result = candidate_evaluation._consolidate_evidence(
            [part], generate=generate
        )
        self.assertEqual(result, part)
        generate.assert_not_called()

    def test_lossy_consolidation_falls_back_to_source_ledgers(self):
        parts = [
            f"[E{index}.1] speaker=candidate | type=candidate_live | "
            f"situation=case{index} | timestamp=00:0{index}:00 | "
            "dimensions=motivation | signals=none\n\"Цитата\""
            for index in (1, 2)
        ]
        result = candidate_evaluation._consolidate_evidence(
            parts,
            generate=mock.Mock(return_value="| ID | Переказ |\n| E1.1 | ... |"),
        )
        self.assertEqual(result, "\n\n".join(parts))

    def test_deterministic_consolidation_deduplicates_same_observation(self):
        record = (
            "[E1B.1] speaker=candidate | type=candidate_live | situation=sql | "
            "timestamp=10:01:00 | dimensions=critical_thinking | "
            "signals=self_correction | phase=update | support=general\n"
            "\"Я змінив відповідь після перевірки.\""
        )
        generate = mock.Mock()
        result = candidate_evaluation._consolidate_evidence(
            [record, record.replace("E1B.1", "E2B.1")],
            generate=generate,
        )
        self.assertEqual(result.count("Я змінив відповідь"), 1)
        generate.assert_not_called()

    def test_non_evaluation_report_has_no_scores_or_grade(self):
        report = candidate_evaluation.create_non_evaluation_report(
            candidate="Jane Doe",
            meeting_date="2026-01-15",
            meeting_title="Hiring manager Interview | Jane Doe | Data Analyst",
            interview_stage="Hiring Manager",
            classification={
                "meeting_type_label": "Співбесіда",
                "outcome": "early_terminated",
                "outcome_label": "Достроково завершена",
                "reason": "Кандидат вирішив не продовжувати процес.",
                "evidence": [{
                    "timestamp": "10:02:00",
                    "speaker": "Jane Doe",
                    "quote": "Дякую, але не хочу продовжувати співбесіду.",
                }],
            },
        )
        self.assertIn("**Статус оцінювання:** Не проводилося", report)
        self.assertIn("**Професійний рівень:** Не визначався", report)
        self.assertIn("Стандартну задачу на hiring feedback не створювати", report)
        self.assertNotIn("| Мотивація |", report)

    def test_rejects_english_report_with_ukrainian_headings(self):
        report = (
            "\n".join(candidate_evaluation.REQUIRED_REPORT_HEADINGS)
            + "\n"
            + ("The candidate has relevant experience but needs more evidence. " * 20)
        )
        generate = mock.Mock(
            side_effect=[
                "evidence", "NO_EVIDENCE", "calibration", report, report
            ]
        )
        with self.assertRaisesRegex(
            candidate_evaluation.CandidateEvaluationError,
            "україномовний аналітичний текст",
        ):
            candidate_evaluation.evaluate(
                "[00:01] Jane: hello",
                candidate="Jane Doe",
                target_level="Junior",
                levels=("Junior",),
                meeting_title="Interview | Jane Doe",
                meeting_date="2026-07-31",
                interviewers=["Interviewer"],
                generate=generate,
            )

    def test_ukrainian_report_allows_english_technical_terms(self):
        report = valid_ukrainian_report() + "\n" + (
            "SQL dashboard data pipeline ownership stakeholder " * 15
        )
        self.assertTrue(candidate_evaluation._is_ukrainian_report(report))

    def test_numeric_score_requires_timestamped_dimension_evidence(self):
        report = valid_ukrainian_report() + "\n| Мотивація | 3 | Середня | опис |"
        errors = candidate_report_validator.validate_candidate_report(
            report,
            candidate="Jane Doe",
            target="",
            interview_stage="Невідомий",
            levels=("Junior",),
        )
        self.assertIn(
            "числова оцінка `Мотивація` без таймкодованої цитати",
            errors,
        )

    def test_report_quote_must_match_evidence_and_transcript(self):
        report = valid_ukrainian_report() + """
| Мотивація | 3 | Середня | опис |

### 1. Мотивація — 3 (Середня)
> [E1.1] «Кандидат вигадав іншу цитату» (00:01:00)
"""
        evidence = """[E1.1] speaker=candidate | type=candidate_live | situation=case | timestamp=00:01:00 | dimensions=motivation | signals=none
"Це справжня цитата кандидата"
"""
        errors = candidate_report_validator.validate_candidate_report(
            report,
            candidate="Jane Doe",
            target="",
            interview_stage="Невідомий",
            levels=("Junior",),
            evidence=evidence,
            transcript="[00:01:00] Jane: Це справжня цитата кандидата",
        )
        self.assertIn(
            "цитата `[E1.1]` не підтверджена evidence ledger", errors
        )
        self.assertIn("цитата `[E1.1]` не знайдена у транскрипті", errors)

    def test_report_rejects_malformed_evidence_ids(self):
        report = valid_ukrainian_report() + (
            "\n| Middle | Відповідає | Середня | "
            "Докази [E2.2, [E2.3] | - |"
        )
        errors = candidate_report_validator.validate_candidate_report(
            report,
            candidate="Jane Doe",
            target="",
            interview_stage="Невідомий",
            levels=("Junior", "Middle"),
        )
        self.assertIn("некоректно відформатовані evidence ID", errors)

    def test_target_fit_requires_both_role_and_explicit_level(self):
        report = valid_ukrainian_report(
            target_role="Data Scientist",
            target_level="Не задано",
            target_fit="Частково",
        )
        errors = candidate_report_validator.validate_candidate_report(
            report,
            candidate="Jane Doe",
            target="Data Scientist",
            interview_stage="Невідомий",
            levels=("Junior", "Middle", "Senior"),
        )
        self.assertIn(
            "target fit задано без однозначних target role і level", errors
        )

    def test_isabella_style_reflection_and_bias_proxies_are_rejected(self):
        report = valid_ukrainian_report() + """
| Рефлексія | 4 | Середня | опис |
| Усвідомлення упереджень | 3 | Низька | опис |

### 4. Рефлексія — 4 (Середня)
> [E1] «Я попереджала керівництво заздалегідь» (16:40:02)

### 5. Усвідомлення упереджень — 3 (Низька)
> [E1] «Я не погодилася з VP» (16:40:02)
"""
        evidence = """[E1] speaker=candidate | type=candidate_self_report | situation=gabi_finetuning | timestamp=16:40:02 | dimensions=critical_thinking | signals=stakeholder_challenge
> Я попереджала керівництво заздалегідь
"""
        errors = candidate_report_validator.validate_candidate_report(
            report,
            candidate="Jane Doe",
            target="",
            interview_stage="Невідомий",
            levels=("Junior", "Middle", "Senior"),
            evidence=evidence,
        )
        self.assertIn(
            "рефлексія 3+ без ланцюжка own_mistake → lesson → behavior_change",
            errors,
        )
        self.assertIn(
            "числова оцінка bias awareness без прямого bias-сигналу", errors
        )

    def test_high_confidence_requires_independent_situations_and_live_evidence(self):
        report = valid_ukrainian_report() + """
| Критичне мислення | 4 | Висока | опис |

### 3. Критичне мислення — 4 (Висока)
> [E1] «Перша цитата» (16:40:02)
> [E2] «Друга цитата» (16:41:02)
> [E3] «Третя цитата» (16:42:02)
"""
        evidence = "\n".join(
            f"[E{index}] speaker=candidate | type=candidate_self_report | "
            f"situation=same_project | timestamp=16:4{index}:02 | "
            "dimensions=critical_thinking | signals=stakeholder_challenge"
            for index in range(1, 4)
        )
        errors = candidate_report_validator.validate_candidate_report(
            report,
            candidate="Jane Doe",
            target="",
            interview_stage="Невідомий",
            levels=("Junior", "Middle", "Senior"),
            evidence=evidence,
        )
        self.assertTrue(
            any("висока впевненість `Критичне мислення`" in item for item in errors)
        )

    def test_missing_evidence_cannot_be_reported_as_below(self):
        report = valid_ukrainian_report() + """
| Lead | Нижче | Висока | немає доказів | відсутні дані про лідерство |
"""
        errors = candidate_report_validator.validate_candidate_report(
            report,
            candidate="Jane Doe",
            target="",
            interview_stage="Невідомий",
            levels=("Junior", "Middle", "Senior", "Lead"),
        )
        self.assertIn(
            "рівень `Lead` позначено Нижче лише через відсутність даних", errors
        )

    def test_direct_negative_signal_can_support_below_level(self):
        report = valid_ukrainian_report() + """
| Middle | Нижче | Середня | Труднощі з оптимізацією [E3.1]. | SQL не перевірено. |
"""
        evidence = (
            "[E3.1] speaker=candidate | type=candidate_live | situation=case | "
            "timestamp=10:01:00 | dimensions=critical_thinking | "
            "signals=missed_optimization"
        )
        errors = candidate_report_validator.validate_candidate_report(
            report,
            candidate="Jane Doe",
            target="",
            interview_stage="Невідомий",
            levels=("Junior", "Middle"),
            evidence=evidence,
        )
        self.assertNotIn(
            "рівень `Middle` позначено Нижче лише через відсутність даних", errors
        )

    def test_reflection_cannot_be_missing_when_lesson_is_in_ledger(self):
        report = valid_ukrainian_report() + (
            "\n| Рефлексія | Недостатньо даних | Низька | Не перевірено |"
        )
        evidence = (
            "[E1B.1] speaker=candidate | type=candidate_self_report | "
            "situation=project_lesson | timestamp=10:01:00 | "
            "dimensions=reflection | signals=lesson"
        )
        errors = candidate_report_validator.validate_candidate_report(
            report,
            candidate="Jane Doe",
            target="",
            interview_stage="Невідомий",
            levels=("Junior",),
            evidence=evidence,
        )
        self.assertIn(
            "рефлексію позначено Недостатньо даних попри прямий "
            "lesson/behavior_change evidence",
            errors,
        )

    def test_debrief_affinity_must_be_named_in_bias_self_check(self):
        report = valid_ukrainian_report().replace(
            "## Самоперевірка упереджень оцінювача",
            "## Самоперевірка упереджень оцінювача\n\n"
            "- **Similarity:** Не виявлено.\n"
            "- **Внесені коригування:** Не потрібні.",
        )
        errors = candidate_report_validator.validate_candidate_report(
            report,
            candidate="Jane Doe",
            target="",
            interview_stage="Невідомий",
            levels=("Junior",),
            interviewer_debrief=(
                "[11:59:11] Інтерв'юер: Мені він понравився, це такий типаж."
            ),
        )
        self.assertIn(
            "self-check не відобразив similarity/affinity bias з debrief",
            errors,
        )

    def test_unverified_competency_is_not_a_hiring_risk(self):
        report = valid_ukrainian_report().replace(
            "## Основні ризики найму",
            "## Основні ризики найму\n\n"
            "| Ризик | Статус | Доказ і умова прояву |\n"
            "|---|---|---|\n"
            "| SQL не перевірено | Умовний | Немає доказів [E1P.1] |",
        )
        errors = candidate_report_validator.validate_candidate_report(
            report,
            candidate="Jane Doe",
            target="",
            interview_stage="Невідомий",
            levels=("Junior",),
        )
        self.assertIn(
            "неперевірену компетенцію помилково оформлено як ризик найму",
            errors,
        )

    def test_debrief_is_isolated_from_candidate_evidence(self):
        report = valid_ukrainian_report()
        generate = mock.Mock(
            side_effect=["evidence", "NO_EVIDENCE", "calibration", report]
        )
        candidate_evaluation.evaluate(
            "[00:01] Jane: hello",
            candidate="Jane Doe",
            target_level="",
            levels=("Trainee", "Junior"),
            meeting_title="Interview | Jane Doe",
            meeting_date="2026-08-17",
            interviewers=["Interviewer"],
            interviewer_debrief="[00:02] Interviewer: Обговорення завершено.",
            generate=generate,
        )
        self.assertNotIn(
            "Обговорення завершено", generate.call_args_list[0].args[0]
        )
        self.assertIn(
            "<INTERVIEWER_DEBRIEF>", generate.call_args_list[2].args[0]
        )

    def test_explicit_trainee_target_is_added_to_configured_ladder(self):
        report = valid_ukrainian_report(
            target_role="Data Analyst",
            target_level="Trainee",
            target_fit="Частково",
        )
        generate = mock.Mock(
            side_effect=[
                "evidence", "NO_EVIDENCE", "calibration", report, report
            ]
        )
        candidate_evaluation.evaluate(
            "[00:01] Jane: hello",
            candidate="Jane Doe",
            target_level="Trainee Data Analyst",
            levels=("Junior", "Middle"),
            meeting_title="Interview | Jane Doe | Trainee Data Analyst",
            meeting_date="2026-08-17",
            interviewers=["Interviewer"],
            generate=generate,
        )
        self.assertIn(
            "Levels to compare: Trainee, Junior, Middle",
            generate.call_args_list[-1].args[0],
        )

    def test_target_level_is_optional_for_level_comparison(self):
        report = valid_ukrainian_report()
        generate = mock.Mock(side_effect=[
            "quoted evidence", "NO_EVIDENCE", "calibration", report
        ])
        result = candidate_evaluation.evaluate(
            "[00:01] Jane: hello",
            candidate="Jane Doe",
            target_level="",
            levels=("Junior", "Middle", "Senior"),
            meeting_title="Interview | Jane Doe",
            meeting_date="2026-07-31",
            interviewers=["Interviewer"],
            generate=generate,
        )
        self.assertEqual(result, report)
        final_prompt = generate.call_args_list[-1].args[0]
        self.assertIn("Target level: Not supplied", final_prompt)
        self.assertIn("Levels to compare: Junior, Middle, Senior", final_prompt)
        self.assertNotIn("<ANCHORS>", final_prompt)
        self.assertNotIn("<BIAS_CHECKLIST>", final_prompt)
        self.assertNotIn("<LEVEL_ANCHORS>", final_prompt)

    def test_evaluation_uses_evidence_pass_then_validates_report(self):
        report = valid_ukrainian_report(
            target_role="Analyst", target_level="Senior", target_fit="Частково"
        )
        generate = mock.Mock(side_effect=[
            "quoted evidence", "NO_EVIDENCE", "calibration", report
        ])
        result = candidate_evaluation.evaluate(
            "[00:01] Jane: hello",
            candidate="Jane Doe",
            target_level="Senior Analyst",
            meeting_title="Interview | Jane Doe",
            meeting_date="2026-07-31",
            interviewers=["Interviewer"],
            generate=generate,
        )
        self.assertEqual(result, report)
        self.assertEqual(generate.call_count, 4)
        self.assertIn("<TRANSCRIPT_CHUNK", generate.call_args_list[0].args[0])
        self.assertIn("live tasks", generate.call_args_list[1].args[0])
        self.assertIn("<EXTRACTED_EVIDENCE>", generate.call_args_list[2].args[0])

    def test_rejects_final_hire_on_non_final_stage(self):
        report = valid_ukrainian_report(
            target_fit="Відповідає",
            final_recommendation="Наймати",
            critical_verified="Так",
            target_role="Data Analyst",
            target_level="Junior",
        )
        generate = mock.Mock(
            side_effect=[
                "evidence", "NO_EVIDENCE", "calibration", report, report
            ]
        )
        with self.assertRaisesRegex(
            candidate_evaluation.CandidateEvaluationError,
            "нефінальному етапі",
        ):
            candidate_evaluation.evaluate(
                "[00:01] Jane: hello",
                candidate="Jane Doe",
                target_level="Junior Data Analyst",
                levels=("Junior",),
                meeting_title="Interview | Jane Doe | Junior Data Analyst",
                meeting_date="2026-08-10",
                interviewers=["Interviewer"],
                generate=generate,
            )

    def test_final_hire_requires_verified_critical_competencies(self):
        report = valid_ukrainian_report(
            status="Фінальне",
            stage="Final",
            target_fit="Відповідає",
            stage_recommendation="Не застосовується — фінальний етап",
            final_recommendation="Наймати",
            critical_verified="Ні",
            target_role="Data Analyst",
            target_level="Junior",
        )
        generate = mock.Mock(
            side_effect=[
                "evidence", "NO_EVIDENCE", "calibration", report, report
            ]
        )
        with self.assertRaisesRegex(
            candidate_evaluation.CandidateEvaluationError,
            "неперевіреними критичними компетенціями",
        ):
            candidate_evaluation.evaluate(
                "[00:01] Jane: hello",
                candidate="Jane Doe",
                target_level="Junior Data Analyst",
                levels=("Junior",),
                meeting_title="Interview | Jane Doe | Junior Data Analyst | Final",
                meeting_date="2026-08-10",
                interviewers=["Interviewer"],
                generate=generate,
            )

    def test_accepts_consistent_final_hire(self):
        report = valid_ukrainian_report(
            status="Фінальне",
            stage="Final",
            target_fit="Відповідає",
            stage_recommendation="Не застосовується — фінальний етап",
            final_recommendation="Наймати",
            critical_verified="Так",
            target_role="Data Analyst",
            target_level="Junior",
        )
        generate = mock.Mock(
            side_effect=["evidence", "NO_EVIDENCE", "calibration", report]
        )
        result = candidate_evaluation.evaluate(
            "[00:01] Jane: hello",
            candidate="Jane Doe",
            target_level="Junior Data Analyst",
            levels=("Junior",),
            meeting_title="Interview | Jane Doe | Junior Data Analyst | Final",
            meeting_date="2026-08-10",
            interviewers=["Jane Doe", "Interviewer"],
            generate=generate,
        )
        self.assertEqual(result, report)
        final_prompt = generate.call_args_list[-1].args[0]
        self.assertIn("Interviewers: Interviewer", final_prompt)
        self.assertNotIn("Interviewers: Jane Doe", final_prompt)

    def test_rejects_final_decision_for_target_without_level(self):
        report = valid_ukrainian_report(
            status="Фінальне",
            stage="Final",
            target_fit="Відповідає",
            stage_recommendation="Не застосовується — фінальний етап",
            final_recommendation="Наймати",
            critical_verified="Так",
            target_role="Data Analyst",
            target_level="Не задано",
        )
        generate = mock.Mock(
            side_effect=[
                "evidence", "NO_EVIDENCE", "calibration", report, report
            ]
        )
        with self.assertRaisesRegex(
            candidate_evaluation.CandidateEvaluationError,
            "target level",
        ):
            candidate_evaluation.evaluate(
                "[00:01] Jane: hello",
                candidate="Jane Doe",
                target_level="Data Analyst",
                levels=("Junior", "Middle"),
                meeting_title="Interview | Jane Doe | Data Analyst | Final",
                meeting_date="2026-08-10",
                interviewers=["Interviewer"],
                generate=generate,
            )

    def test_evidence_cache_skips_completed_model_calls(self):
        report = valid_ukrainian_report()
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "cache.json"
            first = mock.Mock(
                side_effect=[
                    "quoted evidence", "NO_EVIDENCE", "calibration", report
                ]
            )
            candidate_evaluation.evaluate(
                "[00:01] Jane: hello",
                candidate="Jane Doe",
                target_level="",
                levels=("Junior", "Middle"),
                meeting_title="Interview | Jane Doe",
                meeting_date="2026-07-31",
                interviewers=["Interviewer"],
                generate=first,
                cache_path=cache_path,
            )
            second = mock.Mock(return_value=report)
            candidate_evaluation.evaluate(
                "[00:01] Jane: hello",
                candidate="Jane Doe",
                target_level="",
                levels=("Junior", "Middle"),
                meeting_title="Interview | Jane Doe",
                meeting_date="2026-07-31",
                interviewers=["Interviewer"],
                generate=second,
                cache_path=cache_path,
            )
        self.assertEqual(second.call_count, 1)
        self.assertIn("<EXTRACTED_EVIDENCE>", second.call_args.args[0])

    def test_invalid_report_is_repaired_locally_without_rerunning_evidence(self):
        report = valid_ukrainian_report()
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "cache.json"
            invalid = "English report without the required structure"
            first = mock.Mock(
                side_effect=[
                    "evidence", "NO_EVIDENCE", "calibration", invalid, report
                ]
            )
            common = dict(
                transcript="[00:01] Jane: hello",
                candidate="Jane Doe",
                target_level="",
                levels=("Junior",),
                meeting_title="Interview | Jane Doe",
                meeting_date="2026-07-31",
                interviewers=["Interviewer"],
                cache_path=cache_path,
            )
            result = candidate_evaluation.evaluate(**common, generate=first)
            self.assertEqual(result, report)
            self.assertEqual(first.call_count, 5)
            repair_prompt = first.call_args_list[-1].args[0]
            self.assertIn("<VALIDATION_ERRORS>", repair_prompt)
            self.assertIn("<INVALID_REPORT>", repair_prompt)
            cache = candidate_evaluation.read_json(cache_path)
        self.assertNotIn("invalid_report", cache)
        self.assertNotIn("invalid_report_at", cache)
        self.assertNotIn("invalid_report_errors", cache)

    def test_second_invalid_report_in_local_repair_becomes_terminal(self):
        invalid = "English report without the required structure"
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "cache.json"
            common = dict(
                transcript="[00:01] Jane: hello",
                candidate="Jane Doe",
                target_level="",
                levels=("Junior",),
                meeting_title="Interview | Jane Doe",
                meeting_date="2026-07-31",
                interviewers=["Interviewer"],
                cache_path=cache_path,
            )
            generate = mock.Mock(side_effect=[
                "evidence", "NO_EVIDENCE", "calibration", invalid, invalid
            ])
            with self.assertRaises(
                candidate_evaluation.CandidateEvaluationTerminalError
            ):
                candidate_evaluation.evaluate(**common, generate=generate)
            self.assertEqual(generate.call_count, 5)
            failed_cache = candidate_evaluation.read_json(cache_path)
            self.assertIn("invalid_report", failed_cache)
            self.assertTrue(failed_cache["invalid_report_errors"])

    def test_generation_profile_invalidates_evidence_cache(self):
        report = valid_ukrainian_report()
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "cache.json"
            first = mock.Mock(
                side_effect=["evidence", "NO_EVIDENCE", "calibration", report]
            )
            common = dict(
                transcript="[00:01] Jane: hello",
                candidate="Jane Doe",
                target_level="",
                levels=("Junior", "Middle"),
                meeting_title="Interview | Jane Doe",
                meeting_date="2026-07-31",
                interviewers=["Interviewer"],
                cache_path=cache_path,
            )
            candidate_evaluation.evaluate(
                **common, generate=first, cache_profile={"think": False}
            )
            second = mock.Mock(
                side_effect=[
                    "new evidence", "NO_EVIDENCE", "new calibration", report
                ]
            )
            candidate_evaluation.evaluate(
                **common, generate=second, cache_profile={"think": True}
            )
        self.assertEqual(second.call_count, 4)

    def test_reports_stage_progress_and_cache_hits(self):
        report = valid_ukrainian_report(target_level="Junior")
        progress = []
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "cache.json"
            common = dict(
                transcript="[00:01] Jane: hello",
                candidate="Jane Doe",
                target_level="Junior",
                levels=("Junior",),
                meeting_title="Interview | Jane Doe",
                meeting_date="2026-07-31",
                interviewers=["Interviewer"],
                cache_path=cache_path,
                progress=progress.append,
            )
            generate = mock.Mock(
                side_effect=["evidence", "NO_EVIDENCE", "calibration", report]
            )
            candidate_evaluation.evaluate(**common, generate=generate)
            generate = mock.Mock(return_value=report)
            candidate_evaluation.evaluate(**common, generate=generate)

        self.assertTrue(any("evidence 1/1 profile — аналіз" in item for item in progress))
        self.assertTrue(any("evidence 1/1 behavior — кеш" in item for item in progress))
        self.assertTrue(any("консолідація evidence — детерміноване" in item for item in progress))
        self.assertEqual(sum("фінальний звіт — готово" in item for item in progress), 2)

    def test_retries_evidence_chunk_when_all_quotes_are_not_verbatim(self):
        transcript = "[10:01:00] Jane Doe: Я перевірила дані та змінила рішення."
        invalid = (
            "[E1.1] speaker=candidate | type=candidate_live | situation=s1 | "
            "timestamp=10:01:00 | dimensions=critical_thinking | signals=lesson\n"
            '"Я проаналізувала інформацію та переглянула рішення."'
        )
        valid = (
            "[E1.1] speaker=candidate | type=candidate_live | situation=s1 | "
            "timestamp=10:01:00 | dimensions=critical_thinking | signals=lesson\n"
            '"Я перевірила дані та змінила рішення."'
        )
        generate = mock.Mock(side_effect=[invalid, valid, "NO_EVIDENCE"])
        progress = []

        result = candidate_evaluation._extract_evidence(
            transcript,
            levels=("Junior",),
            generate=generate,
            progress=progress.append,
        )

        self.assertEqual(result, [valid])
        self.assertEqual(generate.call_count, 3)
        self.assertIn("<VALIDATION_ERRORS>", generate.call_args_list[1].args[0])
        self.assertTrue(any("повтор через недослівні" in item for item in progress))

    def test_normalizes_ambiguous_speaker_and_evidence_ranges(self):
        transcript = "[10:01:00] Jane Doe: Точна відповідь кандидата."
        ledger = (
            "[E3.1] speaker=candidate|interviewer | type=candidate_live | "
            "situation=s1 | timestamp=10:01:00 | dimensions=Critical Thinking | "
            "signals=\n\"Точна відповідь кандидата.\""
        )
        grounded, dropped = candidate_evaluation._ground_evidence_ledger(
            ledger, transcript
        )
        self.assertFalse(dropped)
        self.assertIn("speaker=candidate |", grounded)
        self.assertEqual(
            candidate_evaluation._normalize_report_evidence_ids(
                "Докази [E3.1-E3.3]."
            ),
            "Докази [E3.1], [E3.2], [E3.3].",
        )
        self.assertEqual(
            candidate_evaluation._normalize_report_evidence_ids(
                "Докази [E2.4-E3.3]."
            ),
            "Докази [E2.4], [E3.3].",
        )

    def test_report_append_is_idempotent_per_evaluation_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(candidate_evaluation, "EVALUATIONS", root):
                path = candidate_evaluation.save_report(
                    "# First",
                    candidate="Jane Doe",
                    meeting_date="2026-07-31",
                    evaluation_id="session-1",
                )
                candidate_evaluation.save_report(
                    "# Duplicate",
                    candidate="Jane Doe",
                    meeting_date="2026-07-31",
                    evaluation_id="session-1",
                )
                candidate_evaluation.save_report(
                    "# Second round",
                    candidate="Jane Doe",
                    meeting_date="2026-07-31",
                    evaluation_id="session-2",
                )

            text = path.read_text(encoding="utf-8")
            self.assertIn("# First", text)
            self.assertNotIn("# Duplicate", text)
            self.assertIn("# Second round", text)
            self.assertEqual(text.count("evaluation-id:"), 2)

    def test_report_replace_updates_only_matching_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(candidate_evaluation, "EVALUATIONS", Path(directory)):
                path = candidate_evaluation.save_report(
                    "# First", candidate="Jane", meeting_date="2026-07-31",
                    evaluation_id="round-1",
                )
                candidate_evaluation.save_report(
                    "# Second", candidate="Jane", meeting_date="2026-07-31",
                    evaluation_id="round-2",
                )
                candidate_evaluation.save_report(
                    "# First updated", candidate="Jane", meeting_date="2026-07-31",
                    evaluation_id="round-1", replace_existing=True,
                )
            text = path.read_text(encoding="utf-8")
            self.assertIn("# First updated", text)
            self.assertNotIn("# First\n", text)
            self.assertIn("# Second", text)
            self.assertEqual(text.count("evaluation-id:"), 2)


if __name__ == "__main__":
    unittest.main()
