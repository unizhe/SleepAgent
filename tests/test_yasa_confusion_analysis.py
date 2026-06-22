import json
from pathlib import Path

import pytest

from scripts.analyze_yasa_shhs_confusion import main as confusion_main
from sleepagent.schemas import SleepStage
from sleepagent.services import (
    STAGE3_YASA_CONFUSION_ANALYSIS_SCHEMA_VERSION,
    analyze_yasa_shhs_confusion,
    build_yasa_confusion_analysis_payload,
)


def test_analyzes_yasa_shhs_confusion_and_rem_nrem_share(tmp_path: Path) -> None:
    summary_path = tmp_path / "yasa-summary.json"
    xml_path = tmp_path / "sample-profusion.xml"
    _write_yasa_summary(
        summary_path,
        [
            SleepStage.WAKE,
            SleepStage.NREM,
            SleepStage.NREM,
            SleepStage.REM,
            SleepStage.WAKE,
        ],
    )
    _write_profusion_xml(
        xml_path,
        [
            "0",
            "5",
            "2",
            "2",
            "0",
        ],
    )

    result = analyze_yasa_shhs_confusion(
        yasa_summary_path=summary_path,
        shhs_xml_path=xml_path,
    )
    payload = build_yasa_confusion_analysis_payload(result)

    assert payload["schema_version"] == STAGE3_YASA_CONFUSION_ANALYSIS_SCHEMA_VERSION
    assert payload["compared_epoch_count"] == 5
    assert payload["total_error_count"] == 2
    assert payload["accuracy"] == pytest.approx(0.6)
    assert payload["confusion_counts"]["REM"]["NREM"] == 1
    assert payload["confusion_counts"]["NREM"]["REM"] == 1
    assert payload["rem_nrem_confusion_count"] == 2
    assert payload["rem_nrem_confusion_share_of_errors"] == pytest.approx(1.0)
    assert payload["top_confusions"][0]["count"] == 1


def test_confusion_cli_writes_payload(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    summary_path = tmp_path / "yasa-summary.json"
    xml_path = tmp_path / "sample-profusion.xml"
    output_path = tmp_path / "confusion.json"
    _write_yasa_summary(summary_path, [SleepStage.WAKE, SleepStage.REM])
    _write_profusion_xml(xml_path, ["0", "5"])

    exit_code = confusion_main(
        [
            "--yasa-summary",
            str(summary_path),
            "--shhs-xml",
            str(xml_path),
            "--out",
            str(output_path),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["total_error_count"] == 0
    assert payload["rem_nrem_confusion_share_of_errors"] is None
    assert "confusion_counts" in capsys.readouterr().out


def _write_yasa_summary(path: Path, stages: list[SleepStage]) -> None:
    path.write_text(
        json.dumps(
            {
                "epochs": [
                    {
                        "start_second": index * 30.0,
                        "duration_seconds": 30.0,
                        "stage": stage.value,
                        "confidence": 0.9,
                    }
                    for index, stage in enumerate(stages)
                ]
            }
        ),
        encoding="utf-8",
    )


def _write_profusion_xml(path: Path, labels: list[str]) -> None:
    stage_block = "\n".join(f"    <SleepStage>{label}</SleepStage>" for label in labels)
    path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<CMPStudyConfig>
  <EpochLength>30</EpochLength>
  <SleepStages>
{stage_block}
  </SleepStages>
</CMPStudyConfig>
""",
        encoding="utf-8",
    )
