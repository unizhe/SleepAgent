from pathlib import Path

import pytest

from sleepagent.preprocessing import (
    SHHS_ROOT_ENV_VAR,
    SHHSPathError,
    build_shhs_record_paths,
    discover_shhs_records,
    normalize_shhs_record_id,
    normalize_shhs_visit,
    resolve_shhs_root,
)


def test_normalizes_shhs_visits_and_official_file_names() -> None:
    assert normalize_shhs_visit(1) == "shhs1"
    assert normalize_shhs_visit("SHHS2") == "shhs2"
    assert normalize_shhs_record_id("shhs1-200001.edf") == "shhs1-200001"
    assert normalize_shhs_record_id("shhs1-200001-nsrr.xml") == "shhs1-200001"
    assert normalize_shhs_record_id("shhs1-200001-profusion.xml") == "shhs1-200001"
    assert normalize_shhs_record_id("200001", visit="shhs1") == "shhs1-200001"


def test_rejects_ambiguous_or_mismatched_record_ids() -> None:
    with pytest.raises(SHHSPathError, match="require a visit"):
        normalize_shhs_record_id("200001")

    with pytest.raises(SHHSPathError, match="does not match"):
        normalize_shhs_record_id("shhs1-200001.edf", visit="shhs2")


def test_resolves_root_argument_or_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit_root = tmp_path / "explicit" / "shhs"
    env_root = tmp_path / "env" / "shhs"
    monkeypatch.setenv(SHHS_ROOT_ENV_VAR, str(env_root))

    assert resolve_shhs_root(explicit_root) == explicit_root.resolve()
    assert resolve_shhs_root() == env_root.resolve()


def test_builds_expected_nsrr_download_layout_paths(tmp_path: Path) -> None:
    paths = build_shhs_record_paths(tmp_path / "shhs", "200001", visit=1)

    assert paths.record_id == "shhs1-200001"
    assert paths.visit == "shhs1"
    assert paths.edf_path == (
        tmp_path / "shhs" / "polysomnography" / "edfs" / "shhs1" / "shhs1-200001.edf"
    ).resolve()
    assert paths.annotation_path("nsrr") == (
        tmp_path
        / "shhs"
        / "polysomnography"
        / "annotations-events-nsrr"
        / "shhs1"
        / "shhs1-200001-nsrr.xml"
    ).resolve()
    assert paths.annotation_path("profusion") == (
        tmp_path
        / "shhs"
        / "polysomnography"
        / "annotations-events-profusion"
        / "shhs1"
        / "shhs1-200001-profusion.xml"
    ).resolve()


def test_discovers_record_manifests_by_filename_only(tmp_path: Path) -> None:
    root = tmp_path / "shhs"
    (root / "polysomnography" / "edfs" / "shhs1").mkdir(parents=True)
    (root / "polysomnography" / "annotations-events-nsrr" / "shhs1").mkdir(parents=True)
    (root / "polysomnography" / "annotations-events-profusion" / "shhs2").mkdir(
        parents=True
    )

    (root / "polysomnography" / "edfs" / "shhs1" / "shhs1-200002.edf").touch()
    (
        root
        / "polysomnography"
        / "annotations-events-nsrr"
        / "shhs1"
        / "shhs1-200001-nsrr.xml"
    ).touch()
    (
        root
        / "polysomnography"
        / "annotations-events-profusion"
        / "shhs2"
        / "shhs2-300001-profusion.xml"
    ).touch()

    records = discover_shhs_records(root)

    assert [record.record_id for record in records] == [
        "shhs1-200001",
        "shhs1-200002",
        "shhs2-300001",
    ]
    assert records[0].exists_by_role == {
        "edf": False,
        "nsrr_annotation": True,
        "profusion_annotation": False,
    }
    assert records[0].missing_roles() == ["edf"]
    assert discover_shhs_records(root, visit="shhs2")[0].record_id == "shhs2-300001"
