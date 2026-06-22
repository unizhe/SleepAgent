from sleepagent.preprocessing import generate_mock_sleep_analysis
from sleepagent.schemas import ReportKnowledgeChunk, RetrievedReportKnowledgeChunk, RiskLevel
from sleepagent.services import MEDICAL_DISCLAIMER, generate_mock_sleep_report


class FakeReportRetriever:
    def __init__(self) -> None:
        self.calls = []

    def retrieve(self, query, *, top_k: int = 3):
        self.calls.append((query, top_k))
        return [
            RetrievedReportKnowledgeChunk(
                chunk=ReportKnowledgeChunk(
                    chunk_id="ahi-basic",
                    title="AHI",
                    content="AHI should be interpreted as a risk signal.",
                    source="unit test",
                    topic_tags=["ahi"],
                ),
                score=1.0,
                matched_terms=["ahi"],
            )
        ]


def test_generate_mock_sleep_report_contains_two_audiences() -> None:
    analysis = generate_mock_sleep_analysis(
        record_id="record-report",
        subject_id="subject-report",
        duration_hours=0.5,
        seed=7,
    )

    report = generate_mock_sleep_report(analysis)

    assert report.summary.record_id == "record-report"
    assert report.summary.subject_id == "subject-report"
    assert report.summary.ahi == analysis.respiratory_summary.ahi
    assert "模拟睡眠报告" in report.elder_report
    assert "AHI 只是帮助判断风险的线索" in report.elder_report
    assert "AHI=" in report.professional_report
    assert "synthetic mock data" in report.professional_report
    assert "Retrieved local context chunks:" in report.professional_report
    assert "ahi-basic" in report.professional_report
    assert report.medical_disclaimer == MEDICAL_DISCLAIMER
    assert report.care_suggestions


def test_mock_sleep_report_keeps_analysis_generated_at() -> None:
    analysis = generate_mock_sleep_analysis(duration_hours=0.5, seed=8)

    report = generate_mock_sleep_report(analysis)

    assert report.generated_at == analysis.generated_at


def test_mock_sleep_report_uses_rag_safety_context_for_high_risk() -> None:
    analysis = generate_mock_sleep_analysis(duration_hours=0.5, seed=9).model_copy(
        update={"risk_level": RiskLevel.HIGH}
    )

    report = generate_mock_sleep_report(analysis)

    assert "urgent-symptoms-safety" in report.professional_report
    assert any("急诊评估" in suggestion for suggestion in report.care_suggestions)


def test_mock_sleep_report_can_use_injected_retriever_without_contract_change() -> None:
    analysis = generate_mock_sleep_analysis(duration_hours=0.5, seed=10)
    retriever = FakeReportRetriever()

    payload = generate_mock_sleep_report(
        analysis,
        retriever=retriever,
    ).model_dump(mode="json")

    assert retriever.calls
    assert "ahi" in retriever.calls[0][0]
    assert set(payload) == {
        "summary",
        "elder_report",
        "professional_report",
        "care_suggestions",
        "medical_disclaimer",
        "generated_at",
    }
    assert "ahi-basic" in payload["professional_report"]
