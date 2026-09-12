# 本模块负责睡眠领域规则与数据语义，不依赖 HTTP 或进程装配。
"""Wake-date NightEpisode v2 contract, UUIDv7 IDs, and version dispatch."""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Callable, Literal, Mapping
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator

from sleepagent.domain.contracts import DataMode, SleepDomainContract
from sleepagent.domain.schema_versions import dispatch_versioned


UTC = timezone.utc


class EpisodeAssignmentBasis(str, Enum):
    OBSERVED_WAKE = "observed_wake"
    VENDOR_WAKE_DATE = "vendor_wake_date"
    DEADLINE_FALLBACK = "deadline_fallback"
    PROVISIONAL = "provisional"
    LEGACY_UNKNOWN = "legacy_unknown"


class EpisodeDateConfidence(str, Enum):
    OBSERVED = "observed"
    VENDOR_ASSERTED = "vendor_asserted"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class EpisodePublicationStatus(str, Enum):
    PROVISIONAL = "provisional"
    COMMITTED = "committed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class NightEpisodeV2(SleepDomainContract):
    schema_version: Literal["night_episode.v2"] = "night_episode.v2"
    night_episode_id: str
    id_scheme: Literal["uuidv7"] = "uuidv7"
    namespace_id: str
    namespace_generation: int = Field(ge=1)
    data_mode: DataMode
    run_id: str | None = None
    arm_id: str | None = None
    subject_id: str
    episode_anchor_key: str
    opening_source_idempotency_identity: str
    timezone_name: str
    boundary_policy_version: str
    collection_start_at: datetime
    bed_at: datetime | None = None
    candidate_wake_at: datetime | None = None
    latest_bed_presence_at: datetime | None = None
    wake_at: datetime | None = None
    deterministic_close_deadline_at: datetime
    bed_utc_offset_seconds: int | None = Field(default=None, ge=-64_800, le=64_800)
    wake_utc_offset_seconds: int | None = Field(default=None, ge=-64_800, le=64_800)
    bed_fold: Literal[0, 1] | None = None
    wake_fold: Literal[0, 1] | None = None
    bed_local_date: date | None = None
    wake_local_date: date | None = None
    vendor_wake_local_date: date | None = None
    episode_local_date: date | None = None
    legacy_local_sleep_date: date | None = None
    assignment_basis: EpisodeAssignmentBasis
    date_confidence: EpisodeDateConfidence
    publication_status: EpisodePublicationStatus
    date_conflict: bool = False
    current_revision: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_v2_contract(self) -> "NightEpisodeV2":
        parsed = UUID(self.night_episode_id)
        if parsed.version != 7 or parsed.variant != "specified in RFC 4122":
            raise ValueError("night_episode_id must be a canonical UUIDv7")
        if str(parsed) != self.night_episode_id:
            raise ValueError("night_episode_id must use canonical UUID text")
        try:
            timezone_info = ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone_name must be a valid IANA timezone") from exc
        if self.data_mode == DataMode.REPLAY and (
            self.run_id is None or self.arm_id is None
        ):
            raise ValueError("replay Episode requires run_id and arm_id")
        if self.data_mode == DataMode.LIVE and (
            self.run_id is not None or self.arm_id is not None
        ):
            raise ValueError("live Episode cannot carry replay run/arm")
        expected_anchor = episode_anchor_key(
            namespace_id=self.namespace_id,
            namespace_generation=self.namespace_generation,
            subject_id=self.subject_id,
            opening_source_idempotency_identity=(
                self.opening_source_idempotency_identity
            ),
        )
        if self.episode_anchor_key != expected_anchor:
            raise ValueError("episode_anchor_key does not match opening identity")
        if self.deterministic_close_deadline_at <= self.collection_start_at:
            raise ValueError("close deadline must follow collection start")
        if (
            self.latest_bed_presence_at is not None
            and self.latest_bed_presence_at < self.collection_start_at
        ):
            raise ValueError("latest bed presence cannot precede collection start")
        if self.candidate_wake_at is not None:
            if self.candidate_wake_at <= self.collection_start_at:
                raise ValueError("candidate wake must follow collection start")
            if (
                self.latest_bed_presence_at is None
                or self.latest_bed_presence_at < self.candidate_wake_at
            ):
                raise ValueError("candidate wake requires current out-of-bed evidence")
            if self.wake_at is not None:
                raise ValueError("confirmed wake cannot retain a wake candidate")
            if self.assignment_basis != EpisodeAssignmentBasis.PROVISIONAL:
                raise ValueError(
                    "only a provisional Episode can retain a wake candidate"
                )
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        for instant, local_date, offset, fold, label in (
            (
                self.bed_at,
                self.bed_local_date,
                self.bed_utc_offset_seconds,
                self.bed_fold,
                "bed",
            ),
            (
                self.wake_at,
                self.wake_local_date,
                self.wake_utc_offset_seconds,
                self.wake_fold,
                "wake",
            ),
        ):
            if instant is None:
                if local_date is not None or offset is not None or fold is not None:
                    raise ValueError(f"{label} local metadata requires {label}_at")
                continue
            local = instant.astimezone(timezone_info)
            if local_date != local.date():
                raise ValueError(f"{label}_local_date does not match timezone")
            expected_offset = int((local.utcoffset() or timezone.utc.utcoffset(local)).total_seconds())
            if offset != expected_offset or fold != local.fold:
                raise ValueError(f"{label} offset/fold does not match timezone")
        if self.assignment_basis == EpisodeAssignmentBasis.PROVISIONAL:
            if self.episode_local_date is not None:
                raise ValueError("provisional Episode cannot have canonical date")
            if self.publication_status != EpisodePublicationStatus.PROVISIONAL:
                raise ValueError("provisional basis requires provisional status")
            if self.date_confidence != EpisodeDateConfidence.UNKNOWN:
                raise ValueError("provisional date confidence must be unknown")
        elif self.assignment_basis == EpisodeAssignmentBasis.LEGACY_UNKNOWN:
            if self.legacy_local_sleep_date is None:
                raise ValueError("legacy upcast requires legacy local date")
            if self.episode_local_date is not None:
                raise ValueError("legacy date cannot masquerade as canonical wake date")
        else:
            if self.episode_local_date is None:
                raise ValueError("finalized Episode requires canonical date")
            if self.assignment_basis == EpisodeAssignmentBasis.OBSERVED_WAKE:
                if self.wake_at is None or self.wake_local_date != self.episode_local_date:
                    raise ValueError("observed wake basis requires matching wake instant")
                if self.date_confidence != EpisodeDateConfidence.OBSERVED:
                    raise ValueError("observed wake must have observed confidence")
            elif self.assignment_basis == EpisodeAssignmentBasis.VENDOR_WAKE_DATE:
                if self.vendor_wake_local_date != self.episode_local_date:
                    raise ValueError("vendor basis requires matching vendor wake date")
                if self.date_confidence != EpisodeDateConfidence.VENDOR_ASSERTED:
                    raise ValueError("vendor wake must have vendor confidence")
            elif self.assignment_basis == EpisodeAssignmentBasis.DEADLINE_FALLBACK:
                expected = self.deterministic_close_deadline_at.astimezone(
                    timezone_info
                ).date()
                if self.episode_local_date != expected:
                    raise ValueError("deadline fallback date does not match deadline")
                if self.date_confidence != EpisodeDateConfidence.ESTIMATED:
                    raise ValueError("deadline fallback must be estimated")
        if self.date_conflict != (
            self.publication_status
            == EpisodePublicationStatus.RECONCILIATION_REQUIRED
        ):
            raise ValueError("date conflict and publication status disagree")
        return self


def episode_anchor_key(
    *,
    namespace_id: str,
    namespace_generation: int,
    subject_id: str,
    opening_source_idempotency_identity: str,
) -> str:
    encoded = "\0".join(
        (
            namespace_id,
            str(namespace_generation),
            subject_id,
            opening_source_idempotency_identity,
        )
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class UUID7Generator:
    """Fork-aware, rollback-tolerant UUIDv7 generator for server-side IDs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pid = os.getpid()
        self._last_ms = -1
        self._last_random = 0

    def __call__(self, now: datetime | None = None) -> str:
        supplied_ms = _unix_milliseconds(now)
        with self._lock:
            current_pid = os.getpid()
            if current_pid != self._pid:
                self._pid = current_pid
                self._last_ms = -1
                self._last_random = 0
            if supplied_ms > self._last_ms:
                timestamp_ms = supplied_ms
                random_bits = secrets.randbits(74)
            else:
                timestamp_ms = self._last_ms
                random_bits = self._last_random + 1
                if random_bits >= 1 << 74:
                    timestamp_ms += 1
                    random_bits = secrets.randbits(74)
            self._last_ms = timestamp_ms
            self._last_random = random_bits
            return _format_uuid7(timestamp_ms, random_bits)


def uuid7_from_parts(timestamp_ms: int, random_bits: int) -> str:
    """Deterministic constructor used for algorithm conformance tests."""

    if not 0 <= timestamp_ms < 1 << 48:
        raise ValueError("UUIDv7 timestamp must fit 48 bits")
    if not 0 <= random_bits < 1 << 74:
        raise ValueError("UUIDv7 randomness must fit 74 bits")
    return _format_uuid7(timestamp_ms, random_bits)


def open_episode_v2(
    *,
    namespace_id: str,
    namespace_generation: int,
    data_mode: DataMode,
    run_id: str | None,
    arm_id: str | None,
    subject_id: str,
    opening_source_idempotency_identity: str,
    timezone_name: str,
    boundary_policy_version: str,
    collection_start_at: datetime,
    deterministic_close_deadline_at: datetime,
    bed_at: datetime | None = None,
    id_generator: Callable[[datetime | None], str] | None = None,
) -> NightEpisodeV2:
    generator = id_generator or UUID7Generator()
    bed_values = _local_metadata(bed_at, timezone_name)
    return NightEpisodeV2(
        night_episode_id=generator(collection_start_at),
        namespace_id=namespace_id,
        namespace_generation=namespace_generation,
        data_mode=data_mode,
        run_id=run_id,
        arm_id=arm_id,
        subject_id=subject_id,
        episode_anchor_key=episode_anchor_key(
            namespace_id=namespace_id,
            namespace_generation=namespace_generation,
            subject_id=subject_id,
            opening_source_idempotency_identity=(
                opening_source_idempotency_identity
            ),
        ),
        opening_source_idempotency_identity=(
            opening_source_idempotency_identity
        ),
        timezone_name=timezone_name,
        boundary_policy_version=boundary_policy_version,
        collection_start_at=collection_start_at,
        bed_at=bed_at,
        latest_bed_presence_at=bed_at,
        bed_local_date=bed_values[0],
        bed_utc_offset_seconds=bed_values[1],
        bed_fold=bed_values[2],
        deterministic_close_deadline_at=deterministic_close_deadline_at,
        assignment_basis=EpisodeAssignmentBasis.PROVISIONAL,
        date_confidence=EpisodeDateConfidence.UNKNOWN,
        publication_status=EpisodePublicationStatus.PROVISIONAL,
        created_at=collection_start_at,
        updated_at=collection_start_at,
    )


def finalize_episode_date(
    episode: NightEpisodeV2,
    *,
    committed_at: datetime,
    wake_at: datetime | None = None,
    vendor_wake_local_date: date | None = None,
    canonical_date_available: Callable[[date], bool] = lambda _value: True,
) -> NightEpisodeV2:
    if wake_at is not None:
        wake_values = _local_metadata(wake_at, episode.timezone_name)
        episode_date = wake_values[0]
        basis = EpisodeAssignmentBasis.OBSERVED_WAKE
        confidence = EpisodeDateConfidence.OBSERVED
    elif vendor_wake_local_date is not None:
        wake_values = (None, None, None)
        episode_date = vendor_wake_local_date
        basis = EpisodeAssignmentBasis.VENDOR_WAKE_DATE
        confidence = EpisodeDateConfidence.VENDOR_ASSERTED
    else:
        wake_values = (None, None, None)
        episode_date = episode.deterministic_close_deadline_at.astimezone(
            ZoneInfo(episode.timezone_name)
        ).date()
        basis = EpisodeAssignmentBasis.DEADLINE_FALLBACK
        confidence = EpisodeDateConfidence.ESTIMATED
    if episode_date is None:  # wake metadata is present in this branch by construction.
        raise ValueError("episode date could not be derived")
    conflict = not canonical_date_available(episode_date)
    return NightEpisodeV2.model_validate(
        {
            **episode.model_dump(mode="python"),
            "wake_at": wake_at,
            "candidate_wake_at": None,
            "wake_local_date": wake_values[0],
            "wake_utc_offset_seconds": wake_values[1],
            "wake_fold": wake_values[2],
            "vendor_wake_local_date": vendor_wake_local_date,
            "episode_local_date": episode_date,
            "assignment_basis": basis,
            "date_confidence": confidence,
            "publication_status": (
                EpisodePublicationStatus.RECONCILIATION_REQUIRED
                if conflict
                else EpisodePublicationStatus.COMMITTED
            ),
            "date_conflict": conflict,
            "current_revision": episode.current_revision + 1,
            "updated_at": committed_at,
        }
    )


def upcast_night_episode(payload: Mapping[str, Any]) -> NightEpisodeV2:
    readers: Mapping[
        str,
        Callable[[Mapping[str, Any]], NightEpisodeV2],
    ] = {
        "night_episode.v1": _upcast_night_episode_v1,
        "night_episode.v2": NightEpisodeV2.model_validate,
    }
    return dispatch_versioned(
        payload,
        family="night_episode",
        readers=readers,
    )


def _upcast_night_episode_v1(payload: Mapping[str, Any]) -> NightEpisodeV2:
    created_at = _datetime(payload["created_at"])
    start = _datetime(payload["collection_start_at"])
    deadline_raw = payload.get("report_deadline_at")
    if deadline_raw is None:
        raise ValueError("legacy Episode without deadline cannot be upcast safely")
    deadline = _datetime(deadline_raw)
    namespace_id = str(payload.get("namespace_id", "legacy:unknown"))
    generation = int(payload.get("namespace_generation", 1))
    subject_id = str(payload["subject_id"])
    opening_identity = str(
        payload.get("night_key") or payload["night_episode_id"]
    )
    generator = UUID7Generator()
    return NightEpisodeV2(
        night_episode_id=generator(created_at),
        namespace_id=namespace_id,
        namespace_generation=generation,
        data_mode=DataMode(str(payload["data_mode"])),
        run_id=payload.get("run_id"),
        arm_id=payload.get("arm_id"),
        subject_id=subject_id,
        episode_anchor_key=episode_anchor_key(
            namespace_id=namespace_id,
            namespace_generation=generation,
            subject_id=subject_id,
            opening_source_idempotency_identity=opening_identity,
        ),
        opening_source_idempotency_identity=opening_identity,
        timezone_name=str(payload["timezone_name"]),
        boundary_policy_version=str(
            payload.get("boundary_policy_version", "legacy.v1")
        ),
        collection_start_at=start,
        deterministic_close_deadline_at=deadline,
        legacy_local_sleep_date=date.fromisoformat(
            str(payload["local_sleep_date"])
        ),
        assignment_basis=EpisodeAssignmentBasis.LEGACY_UNKNOWN,
        date_confidence=EpisodeDateConfidence.UNKNOWN,
        publication_status=EpisodePublicationStatus.PROVISIONAL,
        created_at=created_at,
        updated_at=_datetime(payload["updated_at"]),
    )


def _local_metadata(
    value: datetime | None,
    timezone_name: str,
) -> tuple[date | None, int | None, Literal[0, 1] | None]:
    if value is None:
        return None, None, None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Episode instants must include a timezone offset")
    local = value.astimezone(ZoneInfo(timezone_name))
    offset = local.utcoffset()
    if offset is None:
        raise ValueError("timezone offset is unavailable")
    fold: Literal[0, 1] = 1 if local.fold else 0
    return local.date(), int(offset.total_seconds()), fold


def _unix_milliseconds(value: datetime | None) -> int:
    if value is None:
        return time.time_ns() // 1_000_000
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UUIDv7 time must include a timezone offset")
    return int(value.astimezone(UTC).timestamp() * 1_000)


def _format_uuid7(timestamp_ms: int, random_bits: int) -> str:
    rand_a = random_bits >> 62
    rand_b = random_bits & ((1 << 62) - 1)
    integer = (
        (timestamp_ms << 80)
        | (0x7 << 76)
        | (rand_a << 64)
        | (0b10 << 62)
        | rand_b
    )
    return str(UUID(int=integer))


def _datetime(value: object) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("NightEpisode timestamps must include an offset")
    return parsed


__all__ = [
    "EpisodeAssignmentBasis",
    "EpisodeDateConfidence",
    "EpisodePublicationStatus",
    "NightEpisodeV2",
    "UUID7Generator",
    "episode_anchor_key",
    "finalize_episode_date",
    "open_episode_v2",
    "upcast_night_episode",
    "uuid7_from_parts",
]
