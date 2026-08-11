from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from sleepagent.persistence import RadarPersistenceStore
from sleepagent.sleep_domain import (
    AuthorityConfigurationError,
    AuthorityRuntimeConfig,
    CollectionWindowDerivation,
    CompatibilityMigrationController,
    CutoverPhase,
    CutoverPhaseError,
    DataAuthority,
    DataMode,
    DataSufficiency,
    DeploymentMode,
    DeviceBindingReference,
    DomainNamespace,
    LegacyImportManifest,
    LegacyRecordEvidence,
    LegacyWebhookImporter,
    NightEpisode,
    NightEpisodeRevision,
    NightEpisodeState,
    NightRevisionCause,
    ProviderAccountRecord,
    RawPayloadEncryptionPolicy,
    ReadAuthorityMode,
    ShadowComparisonStatus,
    SignatureVerificationState,
    SleepDomainRepository,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 30, 1, 0, tzinfo=UTC)
LIVE = DomainNamespace("live:migration-test", DataMode.LIVE)
QUARANTINE = DomainNamespace(
    "replay:legacy-quarantine:migration-test",
    DataMode.REPLAY,
)


def _repository() -> tuple[SleepDomainRepository, sqlite3.Connection]:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    store = RadarPersistenceStore.connect_sqlite(connection)
    repository = SleepDomainRepository(
        store,
        raw_payload_policy=RawPayloadEncryptionPolicy(
            key_id="migration-test-key",
            key=Fernet.generate_key(),
            retention_period=timedelta(days=7),
            production=False,
        ),
    )
    repository.save_provider_account(
        ProviderAccountRecord(
            namespace=LIVE,
            provider_account_id="perceptor-account",
            provider_id="perceptor",
            configuration_fingerprint="a" * 64,
            status="enabled",
            metadata={"environment": "migration-test"},
            created_at=NOW,
        )
    )
    return repository, connection


def _legacy_database(
    path: Path,
    *,
    raw_id: str = "legacy-raw-1",
    with_record: bool = True,
) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE perceptor_raw_events (
          id INTEGER PRIMARY KEY,
          raw_event_id TEXT UNIQUE,
          vendor TEXT,
          message_id TEXT,
          event_type TEXT,
          device_identifier TEXT,
          received_at TEXT,
          raw_payload_json TEXT,
          data_payload_json TEXT,
          normalization_status TEXT,
          unnormalized_reason TEXT
        );
        CREATE TABLE perceptor_normalized_events (
          id INTEGER PRIMARY KEY,
          raw_event_id TEXT,
          vendor TEXT,
          device_identifier TEXT,
          message_id TEXT,
          event_type TEXT,
          normalized_event_type TEXT,
          normalized_payload_json TEXT,
          normalized_at TEXT
        );
        """
    )
    if with_record:
        connection.execute(
            """
            INSERT INTO perceptor_raw_events (
              id, raw_event_id, vendor, message_id, event_type,
              device_identifier, received_at, raw_payload_json,
              data_payload_json, normalization_status, unnormalized_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                1,
                raw_id,
                "perceptor",
                "legacy-message-1",
                "VitalSignsDataEvent",
                "vendor-device-1",
                NOW.isoformat(),
                '{"private":"secret-legacy-value"}',
                '{"HeartRate":61}',
                "normalized",
            ),
        )
        connection.execute(
            """
            INSERT INTO perceptor_normalized_events (
              id, raw_event_id, vendor, device_identifier, message_id,
              event_type, normalized_event_type,
              normalized_payload_json, normalized_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                raw_id,
                "perceptor",
                "vendor-device-1",
                "legacy-message-1",
                "VitalSignsDataEvent",
                "radar_vital_snapshot",
                '{"heart_rate_bpm":61}',
                (NOW + timedelta(seconds=1)).isoformat(),
            ),
        )
    connection.commit()
    connection.close()


def _importer(repository: SleepDomainRepository) -> LegacyWebhookImporter:
    return LegacyWebhookImporter(
        repository=repository,
        confirmed_namespace=LIVE,
        quarantine_namespace=QUARANTINE,
        provider_account_id="perceptor-account",
        environment="migration-test",
    )


def _complete_manifest(raw_id: str) -> LegacyImportManifest:
    return LegacyImportManifest(
        records=(
            LegacyRecordEvidence(
                legacy_raw_event_id=raw_id,
                source_data_mode=DataMode.LIVE,
                signature_verification=SignatureVerificationState.VERIFIED,
                signature_representation="canonical_parameters",
                signature_profile="perceptor-hmac.v1",
                signed_payload_sha256=hashlib.sha256(
                    b'{"private":"secret-legacy-value"}'
                ).hexdigest(),
                request_signed_at=NOW - timedelta(seconds=2),
                event_occurred_at=NOW - timedelta(seconds=1),
                trustworthy_event_time=True,
                device_binding_id="binding-1",
                provider_device_identifier_sha256=hashlib.sha256(
                    b"vendor-device-1"
                ).hexdigest(),
                adapter_resolution_lock_id="adapter-lock-1",
                compatibility_profile_id="perceptor-profile-v1",
                evidence_reference="acceptance:legacy-signature-review:1",
                evidence_sha256="b" * 64,
            ),
        )
    )


def _install_committed_episode(
    repository: SleepDomainRepository,
    connection: sqlite3.Connection,
) -> None:
    episode = NightEpisode(
        night_episode_id="night-1",
        data_mode=DataMode.LIVE,
        subject_id="elder-1",
        timezone_name="UTC",
        local_sleep_date=date(2026, 7, 29),
        night_key="elder-1:2026-07-29:policy-v1",
        collection_start_at=NOW,
        collection_end_at=NOW + timedelta(hours=8),
        collection_window_derivation=CollectionWindowDerivation.EXTERNAL_COMMAND,
        binding_references=(
            DeviceBindingReference(
                device_binding_id="binding-1",
                binding_version=1,
                device_id="device-1",
            ),
        ),
        data_sufficiency=DataSufficiency.DATA_INSUFFICIENT,
        pinned_adapter_versions={"perceptor-v1": "1.0.0"},
        pinned_observation_schema_versions=("sleep_observation.v1",),
        pinned_policy_versions={"quality": "quality-v1"},
        state=NightEpisodeState.ANALYZED,
        created_at=NOW,
        updated_at=NOW,
    )
    repository.create_night_episode(LIVE, episode)
    revision = NightEpisodeRevision(
        night_episode_revision_id="night-revision-1",
        night_episode_id=episode.night_episode_id,
        data_mode=DataMode.LIVE,
        subject_id=episode.subject_id,
        revision_number=1,
        revision_cause=NightRevisionCause.INITIAL_PUBLICATION,
        observation_ids=(),
        observation_set_sha256=hashlib.sha256(b"empty-observation-set").hexdigest(),
        data_sufficiency=DataSufficiency.DATA_INSUFFICIENT,
        created_at=NOW,
    )
    committed = episode.model_copy(
        update={
            "night_episode_revision_ids": (
                revision.night_episode_revision_id,
            ),
            "current_night_episode_revision_id": (
                revision.night_episode_revision_id
            ),
        }
    )
    connection.execute(
        """
        INSERT INTO sleep_domain_night_episode_revisions (
          night_episode_revision_id, namespace_id, data_mode,
          night_episode_id, subject_id, revision_number, parent_revision_id,
          revision_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (
            revision.night_episode_revision_id,
            LIVE.namespace_id,
            LIVE.data_mode.value,
            revision.night_episode_id,
            revision.subject_id,
            revision.revision_number,
            revision.model_dump_json(),
            revision.created_at.isoformat(),
        ),
    )
    connection.execute(
        """
        UPDATE sleep_domain_night_episodes
        SET current_revision_id = ?, current_revision_number = 1,
            cas_version = 1, episode_json = ?
        WHERE night_episode_id = ?
        """,
        (
            revision.night_episode_revision_id,
            committed.model_dump_json(),
            committed.night_episode_id,
        ),
    )
    connection.commit()


def _configuration(
    authority: ReadAuthorityMode,
) -> AuthorityRuntimeConfig:
    return AuthorityRuntimeConfig(
        deployment_mode=DeploymentMode.TEST,
        read_authority=authority,
        provider_mode="disabled",
        database_url=None,
    )


def test_import_without_proof_is_encrypted_idempotent_quarantine(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy.sqlite3"
    _legacy_database(source)
    repository, connection = _repository()
    importer = _importer(repository)

    first = importer.import_sqlite(source, imported_at=NOW)
    second = importer.import_sqlite(source, imported_at=NOW + timedelta(minutes=1))

    assert second == first
    assert first.imported_record_count == 1
    assert first.quarantined_record_count == 1
    row = connection.execute(
        """
        SELECT namespace_id, data_mode, signature_verification,
               encrypted_payload, raw_metadata_json
        FROM sleep_domain_raw_inbox
        """
    ).fetchone()
    assert row[:3] == (
        QUARANTINE.namespace_id,
        DataMode.REPLAY.value,
        SignatureVerificationState.UNKNOWN.value,
    )
    assert b"secret-legacy-value" not in bytes(row[3])
    assert "secret-legacy-value" not in str(row[4])
    audit = connection.execute(
        """
        SELECT legacy_normalized_event_type,
               legacy_normalized_payload_sha256, disposition, detail_code
        FROM sleep_domain_legacy_import_records
        """
    ).fetchone()
    assert audit[0] == "radar_vital_snapshot"
    assert audit[1] == hashlib.sha256(
        b'{"heart_rate_bpm":61}'
    ).hexdigest()
    assert audit[2] == "quarantined"
    assert "data_mode_unconfirmed" in audit[3]
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    assert source_connection.execute(
        "SELECT COUNT(*) FROM perceptor_raw_events"
    ).fetchone() == (1,)
    source_connection.close()


def test_import_with_complete_external_proof_enters_live_pending_normalization(
    tmp_path: Path,
) -> None:
    source = tmp_path / "confirmed.sqlite3"
    _legacy_database(source, raw_id="legacy-confirmed")
    repository, connection = _repository()

    report = _importer(repository).import_sqlite(
        source,
        manifest=_complete_manifest("legacy-confirmed"),
        imported_at=NOW,
    )

    assert report.quarantined_record_count == 0
    assert connection.execute(
        """
        SELECT namespace_id, data_mode, signature_verification
        FROM sleep_domain_raw_inbox
        """
    ).fetchone() == (
        LIVE.namespace_id,
        DataMode.LIVE.value,
        SignatureVerificationState.VERIFIED.value,
    )
    assert connection.execute(
        "SELECT disposition FROM sleep_domain_legacy_import_records"
    ).fetchone() == ("normalization_pending",)
    assert connection.execute(
        "SELECT status FROM sleep_domain_normalization_work"
    ).fetchone() == ("pending",)


def test_concurrent_import_returns_one_stable_result(
    tmp_path: Path,
) -> None:
    source = tmp_path / "concurrent.sqlite3"
    _legacy_database(source)
    repository, connection = _repository()
    importer = _importer(repository)

    with ThreadPoolExecutor(max_workers=2) as executor:
        reports = list(
            executor.map(
                lambda offset: importer.import_sqlite(
                    source,
                    imported_at=NOW + timedelta(seconds=offset),
                ),
                range(2),
            )
        )

    assert reports[0] == reports[1]
    assert connection.execute(
        "SELECT COUNT(*) FROM sleep_domain_raw_inbox"
    ).fetchone() == (1,)
    assert connection.execute(
        "SELECT COUNT(*) FROM sleep_domain_legacy_import_records"
    ).fetchone() == (1,)


def test_orphan_normalized_row_and_signed_payload_mismatch_are_quarantined(
    tmp_path: Path,
) -> None:
    orphan_source = tmp_path / "orphan.sqlite3"
    _legacy_database(orphan_source, with_record=False)
    source_connection = sqlite3.connect(orphan_source)
    source_connection.execute(
        """
        INSERT INTO perceptor_normalized_events (
          id, raw_event_id, vendor, device_identifier, message_id,
          event_type, normalized_event_type,
          normalized_payload_json, normalized_at
        ) VALUES (1, 'orphan-raw', 'perceptor', 'vendor-device-1',
                  'orphan-message', 'VitalSignsDataEvent',
                  'radar_vital_snapshot', '{"heart_rate_bpm":61}', ?)
        """,
        (NOW.isoformat(),),
    )
    source_connection.commit()
    source_connection.close()
    repository, connection = _repository()

    report = _importer(repository).import_sqlite(
        orphan_source,
        imported_at=NOW,
    )

    assert report.quarantined_record_count == 1
    assert "original_raw_payload_missing" in connection.execute(
        "SELECT detail_code FROM sleep_domain_legacy_import_records"
    ).fetchone()[0]

    mismatch_source = tmp_path / "mismatch.sqlite3"
    _legacy_database(mismatch_source, raw_id="mismatch-raw")
    mismatch_repository, mismatch_connection = _repository()
    manifest = _complete_manifest("mismatch-raw")
    mismatched = manifest.model_copy(
        update={
            "records": (
                manifest.records[0].model_copy(
                    update={"signed_payload_sha256": "f" * 64}
                ),
            )
        }
    )
    mismatch_report = _importer(mismatch_repository).import_sqlite(
        mismatch_source,
        manifest=mismatched,
        imported_at=NOW,
    )
    assert mismatch_report.quarantined_record_count == 1
    assert "signed_payload_mismatch" in mismatch_connection.execute(
        "SELECT detail_code FROM sleep_domain_legacy_import_records"
    ).fetchone()[0]


def test_expand_backfill_shadow_cutover_and_rollback_rehearsal(
    tmp_path: Path,
) -> None:
    repository, connection = _repository()
    _install_committed_episode(repository, connection)
    controller = CompatibilityMigrationController(repository)
    empty_source = tmp_path / "empty-legacy.sqlite3"
    _legacy_database(empty_source, with_record=False)

    expanded = controller.advance_phase(
        LIVE,
        phase=CutoverPhase.EXPANDED,
        configuration=_configuration(ReadAuthorityMode.LEGACY_COMPATIBILITY),
        actor_id="migration-operator",
        reason_code="expand_schema",
        occurred_at=NOW,
    )
    assert expanded.selected_authority == DataAuthority.LEGACY_COMPATIBILITY
    _importer(repository).import_sqlite(empty_source, imported_at=NOW)
    backfill = controller.backfill_current_revisions(LIVE)
    assert backfill.current_revision_count == 1
    controller.advance_phase(
        LIVE,
        phase=CutoverPhase.BACKFILLED,
        configuration=_configuration(ReadAuthorityMode.LEGACY_COMPATIBILITY),
        actor_id="migration-operator",
        reason_code="backfill_complete",
        occurred_at=NOW + timedelta(seconds=1),
    )
    comparison = controller.compare_current(
        LIVE,
        night_episode_id="night-1",
        compared_at=NOW + timedelta(seconds=2),
    )
    assert comparison.status == ShadowComparisonStatus.MATCHED
    controller.advance_phase(
        LIVE,
        phase=CutoverPhase.SHADOW_VERIFIED,
        configuration=_configuration(ReadAuthorityMode.LEGACY_COMPATIBILITY),
        actor_id="migration-operator",
        reason_code="shadow_match",
        occurred_at=NOW + timedelta(seconds=3),
    )
    controller.finalize_current_compatibility(LIVE)
    controller.advance_phase(
        LIVE,
        phase=CutoverPhase.CUTOVER,
        configuration=_configuration(ReadAuthorityMode.CANONICAL),
        actor_id="migration-operator",
        reason_code="configuration_cutover",
        occurred_at=NOW + timedelta(seconds=4),
    )
    controller.advance_phase(
        LIVE,
        phase=CutoverPhase.ROLLBACK_REHEARSED,
        configuration=_configuration(ReadAuthorityMode.CANONICAL),
        actor_id="migration-operator",
        reason_code="rollback_probe_passed",
        occurred_at=NOW + timedelta(seconds=5),
    )
    final = controller.advance_phase(
        LIVE,
        phase=CutoverPhase.ROLLED_BACK,
        configuration=_configuration(ReadAuthorityMode.LEGACY_COMPATIBILITY),
        actor_id="migration-operator",
        reason_code="rollback_rehearsal_complete",
        occurred_at=NOW + timedelta(seconds=6),
    )

    assert final.selected_authority == DataAuthority.LEGACY_COMPATIBILITY
    assert final.state_version == 6
    assert connection.execute(
        "SELECT COUNT(*) FROM sleep_domain_authority_cutover_events"
    ).fetchone() == (6,)
    comparison_json = connection.execute(
        "SELECT comparison_json FROM sleep_domain_shadow_read_comparisons"
    ).fetchone()[0]
    assert "elder-1" not in comparison_json
    assert "values_persisted" in comparison_json
    assert connection.execute(
        """
        SELECT source_night_episode_revision_id,
               compatibility_projection_version
        FROM radar_night_summaries
        """
    ).fetchone() == ("night-revision-1", "radar-current-night.v1")


def test_cutover_phase_is_serialized_and_shadow_failure_blocks(
    tmp_path: Path,
) -> None:
    repository, connection = _repository()
    _install_committed_episode(repository, connection)
    controller = CompatibilityMigrationController(repository)
    empty_source = tmp_path / "empty.sqlite3"
    _legacy_database(empty_source, with_record=False)

    def expand_once() -> str:
        try:
            return controller.advance_phase(
                LIVE,
                phase=CutoverPhase.EXPANDED,
                configuration=_configuration(
                    ReadAuthorityMode.LEGACY_COMPATIBILITY
                ),
                actor_id="operator",
                reason_code="concurrent_expand",
                occurred_at=NOW,
            ).phase.value
        except CutoverPhaseError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: expand_once(), range(2)))
    assert sorted(outcomes) == ["expanded", "rejected"]
    assert controller.get_state(LIVE).state_version == 1  # type: ignore[union-attr]

    _importer(repository).import_sqlite(empty_source, imported_at=NOW)
    controller.backfill_current_revisions(LIVE)
    controller.advance_phase(
        LIVE,
        phase=CutoverPhase.BACKFILLED,
        configuration=_configuration(ReadAuthorityMode.LEGACY_COMPATIBILITY),
        actor_id="operator",
        reason_code="backfilled",
        occurred_at=NOW + timedelta(seconds=1),
    )
    row = connection.execute(
        "SELECT summary_json FROM radar_night_summaries"
    ).fetchone()
    summary_json = row[0].replace(
        '"data_coverage_ratio":0.0',
        '"data_coverage_ratio":0.5',
    )
    connection.execute(
        """
        UPDATE radar_night_summaries
        SET summary_json = ?, data_coverage_ratio = 0.5
        """,
        (summary_json,),
    )
    connection.commit()
    failed = controller.compare_current(
        LIVE,
        night_episode_id="night-1",
        compared_at=NOW + timedelta(seconds=2),
    )
    assert failed.status == ShadowComparisonStatus.FAILED
    with pytest.raises(CutoverPhaseError, match="zero failures"):
        controller.advance_phase(
            LIVE,
            phase=CutoverPhase.SHADOW_VERIFIED,
            configuration=_configuration(
                ReadAuthorityMode.LEGACY_COMPATIBILITY
            ),
            actor_id="operator",
            reason_code="must_not_pass",
            occurred_at=NOW + timedelta(seconds=3),
        )


def test_production_authority_rejects_sqlite_replay_and_fake() -> None:
    repository, _ = _repository()
    config = AuthorityRuntimeConfig(
        deployment_mode=DeploymentMode.PRODUCTION,
        read_authority=ReadAuthorityMode.CANONICAL,
        provider_mode="fake",
        database_url="sqlite:////tmp/live.sqlite3",
    )
    with pytest.raises(AuthorityConfigurationError):
        config.validate(
            namespace=LIVE,
            repository=repository,
            raw_payload_policy=repository.raw_payload_policy,
        )
    with pytest.raises(AuthorityConfigurationError):
        config.validate(
            namespace=QUARANTINE,
            repository=repository,
            raw_payload_policy=repository.raw_payload_policy,
        )
