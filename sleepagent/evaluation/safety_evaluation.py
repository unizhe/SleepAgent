from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from sleepagent.services.safety import check_dialogue_safety


class DialogueSafetyEvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(..., min_length=1)
    question: str = Field(..., min_length=1)
    answer: str = Field(..., min_length=1)
    required_flags: list[str] = Field(default_factory=list)
    required_phrases: list[str] = Field(default_factory=list)


class DialogueSafetyEvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    passed: bool
    safety_flags: list[str] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
    missing_flags: list[str] = Field(default_factory=list)
    missing_phrases: list[str] = Field(default_factory=list)


def evaluate_dialogue_safety_cases(
    cases: list[DialogueSafetyEvaluationCase],
) -> list[DialogueSafetyEvaluationResult]:
    results: list[DialogueSafetyEvaluationResult] = []
    for case in cases:
        safety_result = check_dialogue_safety(case.answer)
        missing_flags = [
            flag for flag in case.required_flags if flag not in safety_result.safety_flags
        ]
        missing_phrases = [
            phrase for phrase in case.required_phrases if phrase not in case.answer
        ]
        results.append(
            DialogueSafetyEvaluationResult(
                case_id=case.case_id,
                passed=not (
                    safety_result.blocked_reasons
                    or missing_flags
                    or missing_phrases
                ),
                safety_flags=safety_result.safety_flags,
                blocked_reasons=safety_result.blocked_reasons,
                missing_flags=missing_flags,
                missing_phrases=missing_phrases,
            )
        )
    return results
