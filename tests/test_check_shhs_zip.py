from pathlib import Path
import zipfile

from scripts.check_shhs_zip import (
    SHHS_ZIP_ENV_VAR,
    inspect_shhs_zip,
    main,
    resolve_zip_path,
)


def test_inspects_synthetic_zip_member_names_only(tmp_path: Path) -> None:
    zip_path = tmp_path / "synthetic-shhs.zip"
    with zipfile.ZipFile(zip_path, mode="w") as archive:
        archive.writestr("polysomnography/edfs/shhs1/shhs1-200001.edf", "fake edf")
        archive.writestr("polysomnography/edfs/shhs1/shhs1-200002.EDF", "fake edf")
        archive.writestr(
            "polysomnography/annotations-events-nsrr/shhs1/shhs1-200001-nsrr.xml",
            "<xml />",
        )
        archive.writestr(
            "polysomnography/annotations-events-profusion/shhs1/"
            "shhs1-200001-profusion.xml",
            "<xml />",
        )
        archive.writestr("README.txt", "not data")

    result = inspect_shhs_zip(zip_path, max_entries_per_type=1)

    assert result.zip_path == zip_path.resolve()
    assert result.total_entries == 5
    assert result.edf_count == 2
    assert result.xml_count == 2
    assert result.edf_samples == ["polysomnography/edfs/shhs1/shhs1-200001.edf"]
    assert result.xml_samples == [
        "polysomnography/annotations-events-nsrr/shhs1/shhs1-200001-nsrr.xml"
    ]


def test_main_reports_missing_zip_gracefully(
    tmp_path: Path,
    capsys,
) -> None:
    missing_zip = tmp_path / "missing-shhs.zip"

    exit_code = main([str(missing_zip)])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "SHHS zip file not found" in captured.err
    assert str(missing_zip.resolve()) in captured.err
    assert SHHS_ZIP_ENV_VAR in captured.err
    assert "../data/raw/shhs.zip" in captured.err


def test_main_uses_environment_zip_path(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    zip_path = tmp_path / "synthetic-shhs.zip"
    with zipfile.ZipFile(zip_path, mode="w") as archive:
        archive.writestr("polysomnography/edfs/shhs2/shhs2-300001.edf", "fake edf")
        archive.writestr(
            "polysomnography/annotations-events-nsrr/shhs2/shhs2-300001-nsrr.xml",
            "<xml />",
        )
    monkeypatch.setenv(SHHS_ZIP_ENV_VAR, str(zip_path))

    exit_code = main(["--max-entries", "2"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "EDF entries found: 1" in captured.out
    assert "XML entries found: 1" in captured.out
    assert "No extraction performed" in captured.out
    assert "data/raw/shhs_sample" in captured.out


def test_resolve_zip_path_prefers_argument_over_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    env_zip = tmp_path / "env.zip"
    arg_zip = tmp_path / "arg.zip"
    monkeypatch.setenv(SHHS_ZIP_ENV_VAR, str(env_zip))

    assert resolve_zip_path(arg_zip) == arg_zip.resolve()
