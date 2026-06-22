"""Local SHHS file path conventions and lightweight record discovery."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


SHHS_ROOT_ENV_VAR = "SLEEPAGENT_SHHS_ROOT"

SHHSVisit = Literal["shhs1", "shhs2"]
SHHSAnnotationKind = Literal["nsrr", "profusion"]
SHHSFileRole = Literal["edf", "nsrr_annotation", "profusion_annotation"]

_RECORD_ID_PATTERN = re.compile(
    r"^(?P<visit>shhs[12])-(?P<subject_id>\d+)"
    r"(?P<annotation_suffix>-(?:nsrr|profusion))?"
    r"(?P<extension>\.(?:edf|xml))?$",
    flags=re.IGNORECASE,
)


class SHHSPathError(ValueError):
    """Raised when SHHS local path inputs cannot be normalized safely."""


@dataclass(frozen=True)
class SHHSRecordPaths:
    """Expected local paths for a single SHHS PSG record."""

    root: Path
    visit: SHHSVisit
    record_id: str
    edf_path: Path
    nsrr_annotation_path: Path
    profusion_annotation_path: Path

    def annotation_path(self, kind: SHHSAnnotationKind = "nsrr") -> Path:
        if kind == "nsrr":
            return self.nsrr_annotation_path
        if kind == "profusion":
            return self.profusion_annotation_path
        raise SHHSPathError("kind must be one of: nsrr, profusion.")

    @property
    def paths_by_role(self) -> dict[SHHSFileRole, Path]:
        return {
            "edf": self.edf_path,
            "nsrr_annotation": self.nsrr_annotation_path,
            "profusion_annotation": self.profusion_annotation_path,
        }

    @property
    def exists_by_role(self) -> dict[SHHSFileRole, bool]:
        return {role: path.exists() for role, path in self.paths_by_role.items()}

    def missing_roles(
        self,
        required_roles: tuple[SHHSFileRole, ...] = ("edf", "nsrr_annotation"),
    ) -> list[SHHSFileRole]:
        paths = self.paths_by_role
        return [role for role in required_roles if not paths[role].exists()]


def resolve_shhs_root(root: str | Path | None = None) -> Path:
    """Resolve the local SHHS root from an argument or SLEEPAGENT_SHHS_ROOT."""
    if root is None:
        root_value = os.getenv(SHHS_ROOT_ENV_VAR)
        if not root_value:
            raise SHHSPathError(
                f"Provide root or set {SHHS_ROOT_ENV_VAR} to the local SHHS directory."
            )
        root = root_value
    return Path(root).expanduser().resolve()


def normalize_shhs_visit(visit: str | int) -> SHHSVisit:
    """Normalize visit identifiers such as 1, '1', or 'SHHS1'."""
    normalized = str(visit).strip().lower().replace(" ", "")
    if normalized in {"1", "visit1", "v1", "shhs1"}:
        return "shhs1"
    if normalized in {"2", "visit2", "v2", "shhs2"}:
        return "shhs2"
    raise SHHSPathError("visit must identify SHHS visit 1 or visit 2.")


def normalize_shhs_record_id(record_id: str, visit: str | int | None = None) -> str:
    """Normalize an SHHS record id from ids or official EDF/XML file names."""
    raw_record_id = str(record_id).strip()
    matched = _RECORD_ID_PATTERN.match(raw_record_id)

    if matched:
        parsed_visit = normalize_shhs_visit(matched.group("visit"))
        subject_id = matched.group("subject_id")
        if visit is not None and normalize_shhs_visit(visit) != parsed_visit:
            raise SHHSPathError(
                f"record_id {raw_record_id!r} does not match visit {visit!r}."
            )
        return f"{parsed_visit}-{subject_id}"

    if raw_record_id.isdigit():
        if visit is None:
            raise SHHSPathError("Bare numeric SHHS record ids require a visit.")
        return f"{normalize_shhs_visit(visit)}-{raw_record_id}"

    raise SHHSPathError(f"Cannot normalize SHHS record id {record_id!r}.")


def infer_visit_from_record_id(record_id: str) -> SHHSVisit:
    normalized_record_id = normalize_shhs_record_id(record_id)
    return normalize_shhs_visit(normalized_record_id.split("-", maxsplit=1)[0])


def build_shhs_record_paths(
    root: str | Path | None,
    record_id: str,
    visit: str | int | None = None,
) -> SHHSRecordPaths:
    """Build expected local EDF/XML paths for one SHHS record."""
    resolved_root = resolve_shhs_root(root)
    normalized_record_id = normalize_shhs_record_id(record_id, visit=visit)
    normalized_visit = infer_visit_from_record_id(normalized_record_id)

    return SHHSRecordPaths(
        root=resolved_root,
        visit=normalized_visit,
        record_id=normalized_record_id,
        edf_path=(
            resolved_root
            / "polysomnography"
            / "edfs"
            / normalized_visit
            / f"{normalized_record_id}.edf"
        ),
        nsrr_annotation_path=(
            resolved_root
            / "polysomnography"
            / "annotations-events-nsrr"
            / normalized_visit
            / f"{normalized_record_id}-nsrr.xml"
        ),
        profusion_annotation_path=(
            resolved_root
            / "polysomnography"
            / "annotations-events-profusion"
            / normalized_visit
            / f"{normalized_record_id}-profusion.xml"
        ),
    )


def discover_shhs_records(
    root: str | Path | None,
    visit: str | int | None = None,
) -> list[SHHSRecordPaths]:
    """Discover locally downloaded SHHS records by file name only."""
    resolved_root = resolve_shhs_root(root)
    visits: tuple[SHHSVisit, ...]
    if visit is None:
        visits = ("shhs1", "shhs2")
    else:
        visits = (normalize_shhs_visit(visit),)

    record_ids: set[str] = set()
    for normalized_visit in visits:
        record_ids.update(_discover_record_ids_for_visit(resolved_root, normalized_visit))

    return [
        build_shhs_record_paths(resolved_root, record_id)
        for record_id in sorted(record_ids, key=_record_sort_key)
    ]


def _discover_record_ids_for_visit(root: Path, visit: SHHSVisit) -> set[str]:
    candidates: set[str] = set()
    search_specs = [
        (root / "polysomnography" / "edfs" / visit, f"{visit}-*.edf"),
        (
            root / "polysomnography" / "annotations-events-nsrr" / visit,
            f"{visit}-*-nsrr.xml",
        ),
        (
            root / "polysomnography" / "annotations-events-profusion" / visit,
            f"{visit}-*-profusion.xml",
        ),
    ]

    for directory, pattern in search_specs:
        if not directory.exists():
            continue
        for path in directory.glob(pattern):
            if path.is_file():
                candidates.add(normalize_shhs_record_id(path.name))

    return candidates


def _record_sort_key(record_id: str) -> tuple[str, int | str]:
    visit, subject_id = record_id.split("-", maxsplit=1)
    return visit, int(subject_id) if subject_id.isdigit() else subject_id
