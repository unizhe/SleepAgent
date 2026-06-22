"""Build paired SHHS EDF/XML indexes from a local zip archive."""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from sleepagent.preprocessing.shhs_paths import normalize_shhs_record_id


SHHS_ZIP_BATCH_SCHEMA_VERSION = "stage3.shhs_zip_record_index.v1"

_EDF_PATTERN = re.compile(
    r"polysomnography/edfs/(?P<visit>shhs[12])/(?P<record_id>shhs[12]-\d+)\.edf$",
    re.IGNORECASE,
)
_NSRR_PATTERN = re.compile(
    r"polysomnography/annotations-events-nsrr/(?P<visit>shhs[12])/"
    r"(?P<record_id>shhs[12]-\d+)-nsrr\.xml$",
    re.IGNORECASE,
)
_PROFUSION_PATTERN = re.compile(
    r"polysomnography/annotations-events-profusion/(?P<visit>shhs[12])/"
    r"(?P<record_id>shhs[12]-\d+)-profusion\.xml$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SHHSZipRecord:
    """Paired SHHS member names for one record in the raw zip."""

    record_id: str
    visit: str
    edf_member: str
    nsrr_xml_member: str | None
    profusion_xml_member: str | None

    @property
    def has_profusion_pair(self) -> bool:
        return self.edf_member != "" and self.profusion_xml_member is not None


def build_shhs_zip_record_index(zip_path: str | Path) -> list[SHHSZipRecord]:
    """Build a filename-only EDF/XML pairing index from the SHHS zip."""
    path = Path(zip_path).expanduser().resolve()
    records: dict[str, dict[str, str | None]] = {}

    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename
            for role, pattern in (
                ("edf_member", _EDF_PATTERN),
                ("nsrr_xml_member", _NSRR_PATTERN),
                ("profusion_xml_member", _PROFUSION_PATTERN),
            ):
                match = pattern.match(name)
                if match is None:
                    continue
                record_id = normalize_shhs_record_id(match.group("record_id"))
                visit = match.group("visit").lower()
                record = records.setdefault(
                    record_id,
                    {
                        "visit": visit,
                        "edf_member": "",
                        "nsrr_xml_member": None,
                        "profusion_xml_member": None,
                    },
                )
                record[role] = name

    return [
        SHHSZipRecord(
            record_id=record_id,
            visit=str(values["visit"]),
            edf_member=str(values["edf_member"]),
            nsrr_xml_member=_optional_str(values["nsrr_xml_member"]),
            profusion_xml_member=_optional_str(values["profusion_xml_member"]),
        )
        for record_id, values in sorted(records.items(), key=lambda item: _record_sort_key(item[0]))
    ]


def select_complete_shhs_zip_records(
    records: list[SHHSZipRecord],
    *,
    limit: int,
    visit: str | None = None,
    require_profusion: bool = True,
) -> list[SHHSZipRecord]:
    """Select complete records for local YASA batch reproduction."""
    if limit < 1:
        raise ValueError("limit must be at least 1.")

    selected: list[SHHSZipRecord] = []
    normalized_visit = visit.lower() if visit is not None else None
    for record in records:
        if normalized_visit is not None and record.visit != normalized_visit:
            continue
        if not record.edf_member:
            continue
        if require_profusion and record.profusion_xml_member is None:
            continue
        selected.append(record)
        if len(selected) >= limit:
            break
    return selected


def extract_shhs_zip_record(
    *,
    zip_path: str | Path,
    record: SHHSZipRecord,
    output_root: str | Path,
    include_nsrr: bool = True,
) -> dict[str, Path]:
    """Extract one paired record to a local SHHS-like root."""
    root = Path(output_root).expanduser().resolve()
    members = [record.edf_member]
    if record.profusion_xml_member is not None:
        members.append(record.profusion_xml_member)
    if include_nsrr and record.nsrr_xml_member is not None:
        members.append(record.nsrr_xml_member)

    paths: dict[str, Path] = {}
    with zipfile.ZipFile(Path(zip_path).expanduser().resolve()) as archive:
        for member in members:
            target = root / member
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                archive.extract(member, path=root)
            if member == record.edf_member:
                paths["edf"] = target
            elif member == record.profusion_xml_member:
                paths["profusion_xml"] = target
            elif member == record.nsrr_xml_member:
                paths["nsrr_xml"] = target
    return paths


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _record_sort_key(record_id: str) -> tuple[str, int | str]:
    visit, subject_id = record_id.split("-", maxsplit=1)
    return visit, int(subject_id) if subject_id.isdigit() else subject_id
