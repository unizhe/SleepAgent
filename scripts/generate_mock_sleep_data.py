import argparse
import json

from sleepagent.preprocessing import generate_mock_sleep_analysis


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate mock SleepAgent analysis data.")
    parser.add_argument("--record-id", default="mock-shhs-0001")
    parser.add_argument("--subject-id", default="mock-subject-0001")
    parser.add_argument("--duration-hours", type=float, default=8.0)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    result = generate_mock_sleep_analysis(
        record_id=args.record_id,
        subject_id=args.subject_id,
        duration_hours=args.duration_hours,
        seed=args.seed,
    )
    print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
