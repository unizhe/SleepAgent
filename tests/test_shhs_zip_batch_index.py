from pathlib import Path
import zipfile

from sleepagent.preprocessing import (
    build_shhs_zip_record_index,
    extract_shhs_zip_record,
    select_complete_shhs_zip_records,
)


def test_builds_zip_record_index_and_selects_complete_pairs(tmp_path: Path) -> None:
    zip_path = tmp_path / "shhs.zip"
    with zipfile.ZipFile(zip_path, mode="w") as archive:
        archive.writestr("polysomnography/edfs/shhs1/shhs1-200001.edf", "edf-1")
        archive.writestr("polysomnography/edfs/shhs1/shhs1-200002.edf", "edf-2")
        archive.writestr(
            "polysomnography/annotations-events-profusion/shhs1/"
            "shhs1-200001-profusion.xml",
            "<xml />",
        )
        archive.writestr(
            "polysomnography/annotations-events-nsrr/shhs1/shhs1-200001-nsrr.xml",
            "<xml />",
        )
        archive.writestr("polysomnography/edfs/shhs2/shhs2-300001.edf", "edf-3")
        archive.writestr(
            "polysomnography/annotations-events-profusion/shhs2/"
            "shhs2-300001-profusion.xml",
            "<xml />",
        )

    records = build_shhs_zip_record_index(zip_path)
    selected = select_complete_shhs_zip_records(records, limit=2, visit="shhs1")

    assert [record.record_id for record in records] == [
        "shhs1-200001",
        "shhs1-200002",
        "shhs2-300001",
    ]
    assert [record.record_id for record in selected] == ["shhs1-200001"]
    assert selected[0].edf_member == "polysomnography/edfs/shhs1/shhs1-200001.edf"
    assert selected[0].profusion_xml_member == (
        "polysomnography/annotations-events-profusion/shhs1/"
        "shhs1-200001-profusion.xml"
    )


def test_extracts_selected_zip_record_to_shhs_like_root(tmp_path: Path) -> None:
    zip_path = tmp_path / "shhs.zip"
    output_root = tmp_path / "shhs_sample"
    with zipfile.ZipFile(zip_path, mode="w") as archive:
        archive.writestr("polysomnography/edfs/shhs1/shhs1-200001.edf", "edf-1")
        archive.writestr(
            "polysomnography/annotations-events-profusion/shhs1/"
            "shhs1-200001-profusion.xml",
            "<xml />",
        )
        archive.writestr(
            "polysomnography/annotations-events-nsrr/shhs1/shhs1-200001-nsrr.xml",
            "<xml />",
        )

    record = select_complete_shhs_zip_records(
        build_shhs_zip_record_index(zip_path),
        limit=1,
    )[0]
    paths = extract_shhs_zip_record(
        zip_path=zip_path,
        record=record,
        output_root=output_root,
    )

    assert paths["edf"].read_text(encoding="utf-8") == "edf-1"
    assert paths["profusion_xml"].name == "shhs1-200001-profusion.xml"
    assert paths["nsrr_xml"].name == "shhs1-200001-nsrr.xml"
