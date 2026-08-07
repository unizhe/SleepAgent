from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from sleepagent.radar_agent.agents import ContextPacket, DialogueAgent, EvidencePacket, TaskContext
from sleepagent.radar_agent.questionnaire import (
    DEFAULT_QUESTIONNAIRE_BANK,
    QuestionnaireAnswer,
    QuestionnaireService,
    QuestionnaireTrigger,
    attach_questionnaire_entries,
    infer_questionnaire_triggers,
)
from sleepagent.radar_agent.schemas import (
    EvidenceClaim,
    EvidenceLedger,
    QuestionnaireBank,
    QuestionnairePolicy,
    RadarDataQualityStatus,
    RadarNightSummary,
    ReviewStatus,
    RiskLevel,
)
from sleepagent.radar_agent.evidence import EvidenceLedgerBuilder


NOW = datetime(2026, 7, 11, 8, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("trigger", "role"),
    [
        (QuestionnaireTrigger.TREND_CAUSE_UNKNOWN, "family"),
        (QuestionnaireTrigger.DATA_QUALITY_INSUFFICIENT, "family"),
        (QuestionnaireTrigger.WATCH_OR_ESCALATE, "family"),
        (QuestionnaireTrigger.RECENT_WORSENING_QUESTION, "elder"),
        (QuestionnaireTrigger.DOCTOR_BACKGROUND_MISSING, "doctor"),
    ],
)
def test_all_five_policy_triggers_select_one_to_three_short_questions(
    trigger: QuestionnaireTrigger,
    role: str,
) -> None:
    selection = QuestionnaireService().select(
        subject_id="elder-001",
        role=role,
        triggers=[trigger],
        now=NOW,
    )

    assert 1 <= len(selection.candidates) <= 3
    assert all(candidate.trigger == trigger for candidate in selection.candidates)
    assert all(len(candidate.prompt_text) <= 120 for candidate in selection.candidates)
    assert all(candidate.answer_type in {"choice", "scale", "short_text"} for candidate in selection.candidates)


def test_questions_have_reviewed_bank_or_registered_skill_provenance() -> None:
    service = QuestionnaireService()
    selection = service.select(
        subject_id="elder-001",
        role="family",
        triggers=[QuestionnaireTrigger.TREND_CAUSE_UNKNOWN],
        now=NOW,
    )

    assert {candidate.question_source for candidate in selection.candidates} == {"bank", "skill"}
    for candidate in selection.candidates:
        assert candidate.source_id
        assert candidate.source_version == "1.0.0"
        assert candidate.policy_id == "micro-trend_cause_unknown"
        assert candidate.policy_version == "1.0.0"

    unreviewed = DEFAULT_QUESTIONNAIRE_BANK.model_copy(update={"reviewed": False})
    bank_only = QuestionnaireService(banks=[unreviewed], skill_packs=[])
    suppressed = bank_only.select(
        subject_id="elder-001",
        role="family",
        triggers=[QuestionnaireTrigger.DATA_QUALITY_INSUFFICIENT],
        now=NOW,
    )
    assert suppressed.candidates == []


def test_policy_frequency_limit_suppresses_then_reopens_after_cooldown() -> None:
    service = QuestionnaireService()
    first = service.select(
        subject_id="elder-001",
        role="family",
        triggers=[QuestionnaireTrigger.WATCH_OR_ESCALATE],
        now=NOW,
        max_questions=1,
    )
    capture = service.capture_answers(
        first,
        [QuestionnaireAnswer(question_id=first.candidates[0].question_id, answer="轻微")],
        collected_at=NOW,
    )

    blocked = service.select(
        subject_id="elder-001",
        role="family",
        triggers=[QuestionnaireTrigger.WATCH_OR_ESCALATE],
        prior_entries=capture.entries,
        now=NOW + timedelta(hours=23),
    )
    reopened = service.select(
        subject_id="elder-001",
        role="family",
        triggers=[QuestionnaireTrigger.WATCH_OR_ESCALATE],
        prior_entries=capture.entries,
        now=NOW + timedelta(hours=25),
    )

    assert blocked.candidates == []
    assert blocked.suppressed_policy_ids == ["micro-watch_or_escalate"]
    assert reopened.candidates


def test_role_adaptation_changes_wording_without_changing_question_identity() -> None:
    service = QuestionnaireService()
    elder = service.select(
        subject_id="elder-001",
        role="elder",
        triggers=[QuestionnaireTrigger.DATA_QUALITY_INSUFFICIENT],
        now=NOW,
        max_questions=1,
    ).candidates[0]
    family = service.select(
        subject_id="elder-001",
        role="family",
        triggers=[QuestionnaireTrigger.DATA_QUALITY_INSUFFICIENT],
        now=NOW,
        max_questions=1,
    ).candidates[0]

    assert elder.question_id == family.question_id == "q-device-placement"
    assert elder.prompt_text != family.prompt_text
    assert elder.options == family.options
    assert elder.source_id == family.source_id


def test_policy_controls_role_allowlist_question_allowlist_and_turn_size() -> None:
    policy = QuestionnairePolicy(
        policy_id="family-watch-only",
        version="1.0.0",
        trigger=QuestionnaireTrigger.WATCH_OR_ESCALATE.value,
        allowed_question_ids=["q-daytime-sleepiness"],
        applicable_roles=["family"],
        max_questions_per_turn=1,
        cooldown_hours=12,
    )
    service = QuestionnaireService(policies=[policy])

    family = service.select(
        subject_id="elder-001",
        role="family",
        triggers=[QuestionnaireTrigger.WATCH_OR_ESCALATE],
        now=NOW,
    )
    elder = service.select(
        subject_id="elder-001",
        role="elder",
        triggers=[QuestionnaireTrigger.WATCH_OR_ESCALATE],
        now=NOW,
    )

    assert [item.question_id for item in family.candidates] == ["q-daytime-sleepiness"]
    assert elder.candidates == []


def test_tone_rewriter_can_only_rewrite_issued_text_not_create_questions() -> None:
    rewriter = RecordingToneRewriter()
    plain = QuestionnaireService().select(
        subject_id="elder-001",
        role="family",
        triggers=[QuestionnaireTrigger.DATA_QUALITY_INSUFFICIENT],
        now=NOW,
        max_questions=2,
    )
    rewritten = QuestionnaireService(tone_rewriter=rewriter).select(
        subject_id="elder-001",
        role="family",
        triggers=[QuestionnaireTrigger.DATA_QUALITY_INSUFFICIENT],
        now=NOW,
        max_questions=2,
    )

    assert [item.question_id for item in rewritten.candidates] == [item.question_id for item in plain.candidates]
    assert [item.options for item in rewritten.candidates] == [item.options for item in plain.candidates]
    assert [item.source_id for item in rewritten.candidates] == [item.source_id for item in plain.candidates]
    assert all(item.prompt_text.startswith("请问，") for item in rewritten.candidates)
    assert len(rewriter.calls) == len(rewritten.candidates)


def test_captured_answers_become_provenanced_entries_and_enter_ledger() -> None:
    service = QuestionnaireService()
    selection = service.select(
        subject_id="elder-001",
        role="family",
        triggers=[QuestionnaireTrigger.WATCH_OR_ESCALATE],
        now=NOW,
        max_questions=1,
    )
    candidate = selection.candidates[0]
    capture = service.capture_answers(
        selection,
        [QuestionnaireAnswer(question_id=candidate.question_id, answer="1次")],
        collected_at=NOW,
    )
    ledger = attach_questionnaire_entries(_ledger(), capture.entries)
    entry = ledger.questionnaire_entries[0]

    assert entry.question_source in {"bank", "skill"}
    assert entry.source_id == candidate.source_id
    assert entry.source_version == candidate.source_version
    assert entry.policy_id == candidate.policy_id
    assert entry.policy_version == candidate.policy_version
    assert entry.trigger == QuestionnaireTrigger.WATCH_OR_ESCALATE.value
    assert entry.evidence_ref in ledger.canonical_evidence_refs
    assert ledger.derived_metrics["questionnaire_entry_count"] == 1

    builder = EvidenceLedgerBuilder(
        ledger_id="ledger-from-builder",
        task_id="task-questionnaire",
    )
    builder.add_questionnaire_entry(entry)
    built = builder.build()
    assert built.questionnaire_entries == [entry]
    assert entry.evidence_ref in built.canonical_evidence_refs

    with pytest.raises(ValueError, match="issued selection"):
        service.capture_answers(
            selection,
            [QuestionnaireAnswer(question_id="invented-by-llm", answer="yes")],
        )


def test_trigger_inference_and_dialogue_use_policy_not_free_question_generation() -> None:
    context = _context(
        role="doctor",
        quality=RadarDataQualityStatus.PARTIAL,
        user_question="为什么最近变差？",
        trend_cause_unknown=True,
    )
    ledger = _ledger(risk=RiskLevel.ESCALATE, uncertainty="trend cause uncertain")
    context = context.model_copy(
        update={
            "evidence_packet": context.evidence_packet.model_copy(
                update={"evidence_ledger": ledger}
            )
        }
    )
    triggers = infer_questionnaire_triggers(context, ledger)
    result = DialogueAgent().run(context)
    candidates = result.output_payload["dialogue"]["questionnaire_candidates"]

    assert set(triggers) == set(QuestionnaireTrigger)
    assert 1 <= len(candidates) <= 3
    assert all(item["question_source"] in {"bank", "skill"} for item in candidates)
    assert all(item["source_version"] == "1.0.0" for item in candidates)


class RecordingToneRewriter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def rewrite_question(self, *, text: str, role: str) -> str:
        self.calls.append((text, role))
        return f"请问，{text}"


def _ledger(
    *,
    risk: RiskLevel = RiskLevel.WATCH,
    uncertainty: str | None = None,
) -> EvidenceLedger:
    ref = "night-summary:radar-001:2026-07-10"
    claim = EvidenceClaim(
        claim_id="claim-questionnaire",
        task_id="task-questionnaire",
        text="近期夜间离床次数有所增加。",
        evidence_refs=[ref],
        confidence=0.7,
        risk_level=risk,
        generated_by="trend",
        review_status=ReviewStatus.REVIEWED,
    )
    return EvidenceLedger(
        ledger_id="ledger-questionnaire",
        task_id="task-questionnaire",
        canonical_evidence_refs=[ref],
        derived_metrics={"risk_level": risk.value},
        claims=[claim],
        confidence=0.7,
        uncertainty=uncertainty,
        review_status=ReviewStatus.REVIEWED,
    )


def _context(
    *,
    role: str = "family",
    quality: RadarDataQualityStatus = RadarDataQualityStatus.GOOD,
    user_question: str = "",
    trend_cause_unknown: bool = False,
) -> ContextPacket:
    summary = RadarNightSummary(
        radar_device_id="radar-001",
        subject_id="elder-001",
        night_of=date(2026, 7, 10),
        data_coverage_ratio=0.7 if quality != RadarDataQualityStatus.GOOD else 0.95,
        data_quality_status=quality,
        source_report_ref="night-summary:radar-001:2026-07-10",
    )
    return ContextPacket(
        task_context=TaskContext(
            task_id="task-questionnaire",
            trace_id="trace-questionnaire",
            role=role,
            purpose="chat",
        ),
        evidence_packet=EvidencePacket(
            night_summaries=[summary],
            data_quality={
                "user_question": user_question,
                "trend_cause_unknown": trend_cause_unknown,
            },
        ),
    )
