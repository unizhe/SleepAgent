from __future__ import annotations

import argparse
import json

from sleepagent.preprocessing import generate_mock_sleep_analysis
from sleepagent.services import (
    DEEPSEEK_API_KEY_ENV,
    DeepSeekAPIError,
    DeepSeekChatClient,
    DeepSeekMissingAPIKeyError,
    DeepSeekReportGeneratorConfig,
    build_deepseek_report_request_preview,
    build_report_from_llm_draft,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a guarded live DeepSeek smoke for Stage 7 report generation."
    )
    parser.add_argument("--duration-hours", type=float, default=0.5)
    parser.add_argument("--seed", type=int, default=21)
    parser.add_argument("--model", default=None)
    parser.add_argument("--api-key-env", default=DEEPSEEK_API_KEY_ENV)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()

    analysis = generate_mock_sleep_analysis(
        duration_hours=args.duration_hours,
        seed=args.seed,
    )
    config = DeepSeekReportGeneratorConfig(
        model=args.model or DeepSeekReportGeneratorConfig().model,
        api_key_env=args.api_key_env,
        timeout_seconds=args.timeout_seconds,
    )
    preview = build_deepseek_report_request_preview(analysis, config=config)
    client = DeepSeekChatClient(
        api_key_env=config.api_key_env,
        base_url=config.base_url,
        timeout_seconds=config.timeout_seconds,
    )

    try:
        draft = client.create_report_draft(
            messages=preview["messages"],
            config=config,
        )
    except DeepSeekMissingAPIKeyError as exc:
        print(str(exc))
        return 2
    except DeepSeekAPIError as exc:
        print(str(exc))
        return 1

    report = build_report_from_llm_draft(analysis, draft)
    payload = {
        "model": config.model,
        "record_id": report.summary.record_id,
        "subject_id": report.summary.subject_id,
        "risk_level": report.summary.risk_level.value,
        "elder_report_chars": len(report.elder_report),
        "professional_report_chars": len(report.professional_report),
        "care_suggestion_count": len(report.care_suggestions),
        "response_contract_fields": sorted(report.model_dump(mode="json").keys()),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
