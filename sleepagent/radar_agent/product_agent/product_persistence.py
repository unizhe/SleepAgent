from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from sleepagent.radar_agent.persistence import RadarPersistenceStore
from sleepagent.radar_agent.product_agent.contracts import stable_hash
from sleepagent.radar_agent.product_agent.governance import (
    CareContextState,
    CommitJournalEntry,
    InMemoryCareContextStore,
    InMemoryMemoryContextStore,
    MemoryContextState,
    StaleStateError,
)
from sleepagent.radar_agent.product_agent.longitudinal_memory import (
    CandidateReviewHandle,
    DeploymentControlAttestation,
    DigestStatus,
    EpisodeDigest,
    EpisodeDigestStatusEvent,
    HandleBinding,
    InductionAttemptRecord,
    InductionInputManifest,
    InductionJob,
    InductionJobEvent,
    InductionJobState,
    InductionReceipt,
    InductionReceiptStatus,
    InMemoryLongitudinalResultStore,
    InventoryCursorBinding,
    MemoryReadReceipt,
    OfflineSkillOutcomeEnvelope,
    PendingProfileCandidate,
    PublicationJournalEntry,
    SkillOutcomeRecord,
    SubjectEpochs,
    TerminalBundle,
)

if TYPE_CHECKING:
    from sleepagent.radar_agent.product_agent.runtime_contracts import (
        ProductEpisodeRunResult,
    )


PRODUCT_STATE_PERSISTENCE_VERSION = "sleepagent-product-state-persistence.v4"
MANIFEST_KEK_ENV = "SLEEPAGENT_MANIFEST_KEK"


class PersistentCareContextStore(InMemoryCareContextStore):
    """Database-backed Care lifecycle state with process-safe CAS."""

    def __init__(self, persistence: RadarPersistenceStore) -> None:
        self.persistence = persistence
        self.lock = persistence.transaction_lock

    def get(self, subject_id: str) -> CareContextState:
        row = self.persistence.load_product_context_state(
            context_kind="care",
            subject_id=subject_id,
        )
        if row is None:
            return CareContextState(subject_id=subject_id)
        version, raw = row
        state = CareContextState.model_validate_json(raw)
        if state.subject_id != subject_id or state.version != version:
            raise ValueError("persisted Care Context identity/version mismatch")
        return state.model_copy(deep=True)

    def compare_and_set(
        self,
        subject_id: str,
        expected_version: int,
        state: CareContextState,
    ) -> None:
        if state.subject_id != subject_id:
            raise StaleStateError("Care Context subject cannot change")
        if state.version != expected_version + 1:
            raise StaleStateError("Care Context version must advance exactly once")
        changed = self.persistence.compare_and_set_product_context_state(
            context_kind="care",
            subject_id=subject_id,
            expected_version=expected_version,
            next_version=state.version,
            state_json=state.model_dump_json(),
            updated_at=datetime.now(timezone.utc),
        )
        if not changed:
            raise StaleStateError("stale Care Context")


class PersistentMemoryContextStore(InMemoryMemoryContextStore):
    """Database-backed governed long-term Memory authority."""

    def __init__(
        self,
        persistence: RadarPersistenceStore,
        *,
        control_clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(control_clock=control_clock)
        self.persistence = persistence
        self.lock = persistence.transaction_lock

    def get(self, subject_id: str) -> MemoryContextState:
        row = self.persistence.load_product_context_state(
            context_kind="memory",
            subject_id=subject_id,
        )
        if row is None:
            return MemoryContextState(subject_id=subject_id)
        version, raw = row
        state = MemoryContextState.model_validate_json(raw)
        if state.subject_id != subject_id or state.version != version:
            raise ValueError("persisted Memory Context identity/version mismatch")
        return state.model_copy(deep=True)

    def compare_and_set(
        self,
        subject_id: str,
        expected_version: int,
        state: MemoryContextState,
    ) -> None:
        if state.subject_id != subject_id:
            raise StaleStateError("Memory Context subject cannot change")
        if state.version != expected_version + 1:
            raise StaleStateError("Memory Context version must advance exactly once")
        changed = self.persistence.compare_and_set_product_context_state(
            context_kind="memory",
            subject_id=subject_id,
            expected_version=expected_version,
            next_version=state.version,
            state_json=state.model_dump_json(),
            updated_at=datetime.now(timezone.utc),
        )
        if not changed:
            raise StaleStateError("stale Memory Context")


class PersistentCommitJournal:
    """Durable reservation/finalization journal for state and external effects."""

    def __init__(self, persistence: RadarPersistenceStore) -> None:
        self.persistence = persistence

    def get(self, idempotency_key: str) -> CommitJournalEntry | None:
        raw = self.persistence.load_product_commit_journal_entry(
            idempotency_key
        )
        if raw is None:
            return None
        return CommitJournalEntry.model_validate_json(raw)

    def reserve(self, entry: CommitJournalEntry) -> bool:
        if entry.state != "pending" or entry.receipt is not None:
            raise ValueError("commit reservation must be pending")
        return self.persistence.reserve_product_commit_journal_entry(
            idempotency_key=entry.idempotency_key,
            tool_name=entry.tool_name,
            input_hash=entry.input_hash,
            fact_snapshot_hash=entry.fact_snapshot_hash,
            entry_json=entry.model_dump_json(),
            created_at=entry.created_at,
        )

    def finalize(self, entry: CommitJournalEntry) -> None:
        if entry.state != "final" or entry.receipt is None:
            raise ValueError("final commit journal entry requires a receipt")
        changed = self.persistence.finalize_product_commit_journal_entry(
            idempotency_key=entry.idempotency_key,
            tool_name=entry.tool_name,
            input_hash=entry.input_hash,
            fact_snapshot_hash=entry.fact_snapshot_hash,
            entry_json=entry.model_dump_json(),
            updated_at=entry.updated_at,
        )
        if not changed:
            raise StaleStateError("commit journal reservation changed")


class _ManifestEnvelopeCipher:
    def __init__(
        self,
        *,
        dialect: str,
        key_material: str | bytes | None = None,
    ) -> None:
        configured = key_material or os.getenv(MANIFEST_KEK_ENV)
        if configured is None:
            if dialect != "sqlite":
                raise ValueError(
                    f"{MANIFEST_KEK_ENV} is required for persistent Manifest storage"
                )
            configured = "sleepagent-local-sqlite-manifest-key.v1"
        raw = configured if isinstance(configured, bytes) else configured.encode()
        self._kek = hashlib.sha256(raw).digest()
        self.key_id = "manifest-kek:" + hashlib.sha256(self._kek).hexdigest()[:16]

    def encrypt(self, manifest: InductionInputManifest) -> tuple[str, str, str, str]:
        data_key = os.urandom(32)
        data_nonce = os.urandom(12)
        wrap_nonce = os.urandom(12)
        aad = manifest.manifest_id.encode()
        ciphertext = AESGCM(data_key).encrypt(
            data_nonce,
            manifest.model_dump_json().encode(),
            aad,
        )
        wrapped = AESGCM(self._kek).encrypt(
            wrap_nonce,
            data_key,
            aad,
        )
        return (
            base64.b64encode(ciphertext).decode(),
            base64.b64encode(data_nonce).decode(),
            base64.b64encode(wrap_nonce + wrapped).decode(),
            self.key_id,
        )

    def decrypt(
        self,
        manifest_id: str,
        envelope: tuple[str, str, str, str],
    ) -> InductionInputManifest:
        ciphertext, nonce, wrapped, key_id = envelope
        if key_id != self.key_id:
            raise ValueError("Manifest KEK identity changed")
        wrapped_bytes = base64.b64decode(wrapped)
        aad = manifest_id.encode()
        data_key = AESGCM(self._kek).decrypt(
            wrapped_bytes[:12],
            wrapped_bytes[12:],
            aad,
        )
        plaintext = AESGCM(data_key).decrypt(
            base64.b64decode(nonce),
            base64.b64decode(ciphertext),
            aad,
        )
        return InductionInputManifest.model_validate_json(plaintext)


class PersistentProductEpisodeResultStore(InMemoryLongitudinalResultStore):
    """Durable atomic Result/Manifest/Job store with encrypted Manifest payloads."""

    def __init__(
        self,
        persistence: RadarPersistenceStore,
        *,
        manifest_key: str | bytes | None = None,
    ) -> None:
        super().__init__()
        self.persistence = persistence
        self.lock = persistence.transaction_lock
        self._cipher = _ManifestEnvelopeCipher(
            dialect=persistence.dialect,
            key_material=manifest_key,
        )
        self._batching = False
        self._bootstrap_longitudinal_authority()
        self._hydrate()

    def _bootstrap_longitudinal_authority(self) -> None:
        raw_state = self.persistence.load_product_longitudinal_operational_state()
        state = {} if raw_state is None else json.loads(raw_state)
        self.persistence.bootstrap_product_longitudinal_authority(
            retrieval_policy_epoch=int(
                state.get("retrieval_policy_epoch", 0)
            ),
            digest_read_enabled=bool(state.get("digest_enabled", False)),
            subject_epochs=[
                {
                    "subject_id": item["subject_id"],
                    "privacy_epoch": int(item.get("privacy_epoch", 0)),
                    "authorization_epoch": int(
                        item.get("authorization_epoch", 0)
                    ),
                }
                for item in state.get("epochs", [])
            ],
            updated_at=datetime.now(timezone.utc),
        )

    def _authority_snapshot(self, subject_id: str) -> dict[str, object]:
        subject = self.persistence.load_product_longitudinal_subject_epochs(
            subject_id
        )
        control = self.persistence.load_product_longitudinal_control()
        return {
            **subject,
            **control,
        }

    def current_epochs(self, subject_id: str) -> SubjectEpochs:
        if self._batching and subject_id in self._epochs:
            return self._epochs[subject_id].model_copy(deep=True)
        snapshot = self._authority_snapshot(subject_id)
        epochs = SubjectEpochs(
            subject_id=subject_id,
            privacy_epoch=int(snapshot["privacy_epoch"]),
            authorization_epoch=int(snapshot["authorization_epoch"]),
            retrieval_policy_epoch=int(
                snapshot["retrieval_policy_epoch"]
            ),
        )
        self._epochs[subject_id] = epochs
        self._retrieval_policy_epoch = epochs.retrieval_policy_epoch
        self._digest_enabled = bool(snapshot["digest_read_enabled"])
        return epochs.model_copy(deep=True)

    def latest_terminal_revision(self, episode_id: str) -> int:
        return self.persistence.latest_product_terminal_revision(episode_id)

    def latest(self, episode_id: str) -> ProductEpisodeRunResult:
        """Return the latest durable revision, including peer-process writes."""

        history = self.history(episode_id)
        if not history:
            raise KeyError(f"unknown Episode result: {episode_id}")
        return history[-1]

    def history(self, episode_id: str) -> list[ProductEpisodeRunResult]:
        """Refresh one Episode from durable storage before exposing history.

        A Product runtime bundle keeps an in-memory projection for longitudinal
        work, but that projection cannot be the authority for Episode revisions:
        another already-running worker may have appended a result through a
        different store instance.  Refreshing the requested Episode keeps the
        fast in-memory projection while making continuation and revision checks
        observe those durable peer writes.
        """

        self._refresh_episode_results(episode_id)
        with self.lock:
            return [
                item.model_copy(deep=True)
                for item in self._results.get(episode_id, [])
            ]

    def _refresh_episode_results(self, episode_id: str) -> None:
        if not episode_id:
            raise ValueError("episode_id is required")

        from sleepagent.radar_agent.product_agent.runtime_contracts import (
            ProductEpisodeRunResult,
        )

        with self.lock:
            durable: list[tuple[str, ProductEpisodeRunResult]] = []
            for raw in self.persistence.list_product_episode_result_json(
                episode_id
            ):
                result = ProductEpisodeRunResult.model_validate_json(raw)
                if result.receipt.episode_id != episode_id:
                    raise ValueError(
                        "persisted nonterminal Episode identity mismatch"
                    )
                result_id = stable_hash(result.model_dump(mode="json"))
                durable.append((result_id, result))

            for result_id, raw in (
                self.persistence.list_product_terminal_result_rows()
            ):
                result = ProductEpisodeRunResult.model_validate_json(raw)
                if result.receipt.episode_id != episode_id:
                    continue
                canonical_id = stable_hash(result.model_dump(mode="json"))
                if result_id != canonical_id:
                    raise ValueError(
                        "persisted terminal Result identity mismatch"
                    )
                durable.append((result_id, result))

            by_revision: dict[int, tuple[str, ProductEpisodeRunResult]] = {}
            for result_id, result in durable:
                revision = result.receipt.receipt_revision
                existing = by_revision.get(revision)
                if existing is not None:
                    if existing[0] != result_id:
                        raise ValueError(
                            "episode receipt revision already binds different "
                            "durable content"
                        )
                    continue
                by_revision[revision] = (result_id, result)

            for key in [
                key for key in self._result_identity if key[0] == episode_id
            ]:
                del self._result_identity[key]
            if by_revision:
                ordered = [
                    by_revision[revision]
                    for revision in sorted(by_revision)
                ]
                self._results[episode_id] = [
                    result.model_copy(deep=True) for _, result in ordered
                ]
                for result_id, result in ordered:
                    self._result_identity[
                        (episode_id, result.receipt.receipt_revision)
                    ] = result_id
            else:
                self._results.pop(episode_id, None)

    def _refresh_authoritative_records(self) -> None:
        self._digests = {
            item.digest_id: item
            for item in map(
                EpisodeDigest.model_validate_json,
                self.persistence.list_product_episode_digest_json(),
            )
        }
        self._digest_events = {}
        for item in map(
            EpisodeDigestStatusEvent.model_validate_json,
            self.persistence.list_product_episode_digest_event_json(),
        ):
            self._digest_events.setdefault(item.digest_id, []).append(item)
        self._pending_candidates = {
            (item.subject_id, item.candidate_hash): item
            for item in map(
                PendingProfileCandidate.model_validate_json,
                self.persistence.list_product_pending_candidate_json(),
            )
        }
        self._skill_outcomes = {
            item.outcome_id: item
            for item in map(
                SkillOutcomeRecord.model_validate_json,
                self.persistence.list_product_skill_outcome_json(),
            )
        }
        durable_offline = (
            self.persistence.list_product_offline_skill_outcome_rows()
        )
        if durable_offline:
            self._offline_outcomes = {}
            self._offline_subjects = {}
            self._offline_lineage = {}
            for subject_id, source_result_hash, raw in durable_offline:
                item = OfflineSkillOutcomeEnvelope.model_validate_json(raw)
                self._offline_outcomes[item.envelope_id] = item
                self._offline_subjects[item.envelope_id] = subject_id
                self._offline_lineage[item.envelope_id] = source_result_hash

    def append_terminal_bundle(
        self,
        result: ProductEpisodeRunResult,
        *,
        subject_id: str,
        now: datetime | None = None,
    ) -> TerminalBundle:
        self._refresh_episode_results(result.receipt.episode_id)
        result_id = stable_hash(result.model_dump(mode="json"))
        key = (result.receipt.episode_id, result.receipt.receipt_revision)
        with self.lock:
            existing_id = self._result_identity.get(key)
            missing_external_bundle = (
                existing_id == result_id and result_id not in self._bundles
            )
        if missing_external_bundle:
            # The Result was appended through another live store instance.
            # Hydrate its atomic Manifest/Job bundle before taking the normal
            # idempotent in-memory path below.
            self._reset_from_persistence()
        bundle = super().append_terminal_bundle(
            result,
            subject_id=subject_id,
            now=now,
        )
        creation_event = next(
            item
            for item in reversed(self._job_events)
            if item.job_id == bundle.job.job_id
        )
        ciphertext, nonce, wrapped, key_id = self._cipher.encrypt(bundle.manifest)
        try:
            self.persistence.append_product_terminal_bundle(
                terminal_result_id=bundle.terminal_result_id,
                episode_id=result.receipt.episode_id,
                receipt_revision=result.receipt.receipt_revision,
                subject_id=subject_id,
                source_result_hash=bundle.terminal_result_id,
                result_json=result.model_dump_json(),
                terminal_recorded_at=bundle.terminal_recorded_at,
                manifest_id=bundle.manifest.manifest_id,
                manifest_hash=str(bundle.manifest.manifest_hash),
                ciphertext=ciphertext,
                nonce=nonce,
                wrapped_data_key=wrapped,
                key_id=key_id,
                manifest_expires_at=bundle.manifest.expires_at,
                job_id=bundle.job.job_id,
                job_idempotency_key=bundle.job.idempotency_key,
                job_state=bundle.job.state.value,
                job_json=bundle.job.model_dump_json(),
                job_event_id=creation_event.event_id,
                job_event_json=creation_event.model_dump_json(),
                operational_state_json=self._state_json(),
            )
        except Exception:
            self._reset_from_persistence()
            raise
        return bundle

    def _reset_from_persistence(self) -> None:
        """Discard speculative in-memory state after a durable write failure."""

        persistence = self.persistence
        transaction_lock = self.lock
        cipher = self._cipher
        retention_policy = self.retention_policy
        InMemoryLongitudinalResultStore.__init__(
            self,
            retention_policy=retention_policy,
        )
        self.persistence = persistence
        self.lock = transaction_lock
        self._cipher = cipher
        self._batching = False
        self._hydrate()

    def orphan_terminal_count(self) -> int:
        return self.persistence.count_product_terminal_orphans()

    def append_nonterminal(
        self,
        result: ProductEpisodeRunResult,
        *,
        subject_id: str,
    ) -> None:
        self._refresh_episode_results(result.receipt.episode_id)
        super().append_nonterminal(result, subject_id=subject_id)
        try:
            self.persistence.append_product_nonterminal_result(
                result_id=stable_hash(result.model_dump(mode="json")),
                episode_id=result.receipt.episode_id,
                result_json=result.model_dump_json(),
                recorded_at=datetime.now(timezone.utc),
            )
            self._save_state()
        except Exception:
            # Discard the speculative in-memory append.  If the durable outcome
            # was actually committed before an acknowledgement failure, hydrate
            # will recover it; otherwise no phantom revision remains.
            self._reset_from_persistence()
            raise

    def append(self, result: ProductEpisodeRunResult) -> None:
        raise ValueError(
            "legacy append is fenced; pass trusted subject_id to the terminal boundary"
        )

    def reserve_publication(
        self,
        **kwargs: object,
    ) -> tuple[PublicationJournalEntry, bool]:
        command_hash = str(kwargs["command_hash"])
        episode_id = str(kwargs["episode_id"])
        draft_hash = str(kwargs["draft_hash"])
        raw_now = kwargs.get("now")
        created_at = (
            raw_now
            if isinstance(raw_now, datetime)
            else datetime.now(timezone.utc)
        )
        intent_id = f"publication:{command_hash}"
        entry = PublicationJournalEntry(
            intent_id=intent_id,
            episode_id=episode_id,
            command_hash=command_hash,
            draft_hash=draft_hash,
            state="reserved",
            created_at=created_at,
            updated_at=created_at,
        )
        created = self.persistence.reserve_product_publication_entry(
            intent_id=intent_id,
            episode_id=episode_id,
            draft_hash=draft_hash,
            entry_json=entry.model_dump_json(),
            created_at=created_at,
        )
        if not created:
            raw = self.persistence.load_product_publication_entry(intent_id)
            if raw is None:
                raise ValueError("publication reservation disappeared")
            entry = PublicationJournalEntry.model_validate_json(raw)
        self._publication_journal[intent_id] = entry
        return entry.model_copy(deep=True), created

    def complete_publication(self, **kwargs: object) -> PublicationJournalEntry:
        intent_id = str(kwargs["intent_id"])
        delivered = bool(kwargs["delivered"])
        raw_now = kwargs.get("now")
        completed_at = (
            raw_now
            if isinstance(raw_now, datetime)
            else datetime.now(timezone.utc)
        )
        raw = self.persistence.load_product_publication_entry(intent_id)
        if raw is None:
            raise KeyError("publication intent was not reserved")
        current = PublicationJournalEntry.model_validate_json(raw)
        if current.state != "reserved":
            if current.delivered != delivered:
                raise ValueError("publication outcome conflicts with journal")
            self._publication_journal[intent_id] = current
            return current
        entry = current.model_copy(
            update={
                "state": "delivered" if delivered else "failed",
                "delivered": delivered,
                "updated_at": completed_at,
            }
        )
        changed = self.persistence.finalize_product_publication_entry(
            intent_id=intent_id,
            delivered=delivered,
            entry_json=entry.model_dump_json(),
            updated_at=completed_at,
        )
        if not changed:
            raw = self.persistence.load_product_publication_entry(intent_id)
            if raw is None:
                raise ValueError("publication finalization disappeared")
            entry = PublicationJournalEntry.model_validate_json(raw)
            if entry.delivered != delivered:
                raise ValueError("publication outcome conflicts with journal")
        self._publication_journal[intent_id] = entry
        return entry.model_copy(deep=True)

    def complete_induction(self, **kwargs: object) -> None:
        job = kwargs["job"]
        now = kwargs["now"]
        if (
            not isinstance(job, InductionJob)
            or not isinstance(now, datetime)
            or job.lease_owner is None
            or not self.persistence.product_induction_lease_matches(
                job_id=job.job_id,
                lease_owner=job.lease_owner,
                attempt_count=job.attempt_count,
                now=now,
            )
        ):
            raise ValueError("durable Induction lease changed before commit")
        manifest = self.get_manifest(job.input_manifest_ref)
        if manifest is None:
            raise ValueError("source_manifest_expired")
        self._refresh_authoritative_records()
        self._epochs[manifest.subject_id] = SubjectEpochs(
            subject_id=manifest.subject_id,
            privacy_epoch=manifest.privacy_epoch,
            authorization_epoch=manifest.authorization_epoch,
            retrieval_policy_epoch=manifest.retrieval_policy_epoch,
        )
        self._batching = True
        try:
            super().complete_induction(**kwargs)
        finally:
            self._batching = False
        try:
            self._save_state(
                expected_job_lease={
                    "job_id": job.job_id,
                    "lease_owner": job.lease_owner,
                    "attempt_count": job.attempt_count,
                    "processing_generation": job.processing_generation,
                    "as_of": now.isoformat(),
                },
                expected_authority={
                    "subject_id": manifest.subject_id,
                    "privacy_epoch": manifest.privacy_epoch,
                    "authorization_epoch": manifest.authorization_epoch,
                    "retrieval_policy_epoch": (
                        manifest.retrieval_policy_epoch
                    ),
                },
                expected_terminal_revision={
                    "episode_id": job.episode_id,
                    "receipt_revision": job.source_receipt_revision,
                },
            )
        except Exception:
            self._reset_from_persistence()
            raise

    def lease_next_job(self, **kwargs: object) -> InductionJob | None:
        worker_id = str(kwargs["worker_id"])
        now = kwargs["now"]
        if not isinstance(now, datetime):
            raise TypeError("lease time must be datetime")
        lease_seconds = int(kwargs.get("lease_seconds", 30))
        candidates = sorted(
            (
                job
                for job in self._jobs.values()
                if (
                    job.state
                    in {
                        InductionJobState.PENDING,
                        InductionJobState.RETRYABLE_FAILED,
                    }
                    and job.next_attempt_at <= now
                )
                or (
                    job.state == InductionJobState.LEASED
                    and job.lease_expires_at is not None
                    and job.lease_expires_at <= now
                )
            ),
            key=lambda item: (item.created_at, item.job_id),
        )
        for current in candidates:
            lease_expires_at = now + timedelta(seconds=lease_seconds)
            updated = current.model_copy(
                update={
                    "state": InductionJobState.LEASED,
                    "attempt_count": current.attempt_count + 1,
                    "lease_owner": worker_id,
                    "lease_expires_at": lease_expires_at,
                    "updated_at": now,
                }
            )
            lease_event = InductionJobEvent(
                event_id=(
                    "job-event:"
                    + stable_hash(
                        (
                            current.job_id,
                            updated.processing_generation,
                            updated.attempt_count,
                            "leased",
                        )
                    )
                ),
                job_id=current.job_id,
                from_state=current.state,
                to_state=InductionJobState.LEASED,
                attempt_count=updated.attempt_count,
                reason_code=(
                    "lease_recovered"
                    if current.state == InductionJobState.LEASED
                    else "leased"
                ),
                created_at=now,
            )
            if not self.persistence.compare_and_set_product_induction_lease(
                job_id=current.job_id,
                expected_attempt_count=current.attempt_count,
                worker_id=worker_id,
                now=now,
                lease_expires_at=lease_expires_at,
                updated_job_json=updated.model_dump_json(),
                event_id=lease_event.event_id,
                event_json=lease_event.model_dump_json(),
            ):
                self._jobs = {
                    item.job_id: item
                    for item in map(
                        InductionJob.model_validate_json,
                        self.persistence.list_product_induction_job_json(),
                    )
                }
                continue
            self._jobs[current.job_id] = updated
            self._job_events.append(lease_event)
            self._save_state()
            return updated.model_copy(deep=True)
        return None

    def fail_induction(self, **kwargs: object) -> InductionReceipt | None:
        job = kwargs["job"]
        now = kwargs["now"]
        if (
            not isinstance(job, InductionJob)
            or not isinstance(now, datetime)
            or job.lease_owner is None
            or not self.persistence.product_induction_lease_matches(
                job_id=job.job_id,
                lease_owner=job.lease_owner,
                attempt_count=job.attempt_count,
                now=now,
            )
        ):
            raise ValueError("durable Induction lease changed before failure")
        receipt = super().fail_induction(**kwargs)
        self._save_state(
            expected_job_lease={
                "job_id": job.job_id,
                "lease_owner": job.lease_owner,
                "attempt_count": job.attempt_count,
                "processing_generation": job.processing_generation,
                "as_of": now.isoformat(),
            }
        )
        return receipt

    def replay_dead_letter(self, **kwargs: object) -> InductionJob:
        job = super().replay_dead_letter(**kwargs)
        self._save_state()
        return job

    def transition_digest_status(self, *args: object, **kwargs: object) -> None:
        super().transition_digest_status(*args, **kwargs)
        self._save_state()

    def digest_status(self, digest_id: str) -> DigestStatus:
        if not self._batching:
            self._refresh_authoritative_records()
        return super().digest_status(digest_id)

    def list_active_digests(
        self,
        subject_id: str,
        *,
        as_of: datetime,
    ) -> list[EpisodeDigest]:
        if not self._batching:
            self._refresh_authoritative_records()
        return super().list_active_digests(subject_id, as_of=as_of)

    def list_pending_candidates(
        self,
        subject_id: str,
        *,
        as_of: datetime,
    ) -> list[PendingProfileCandidate]:
        if not self._batching:
            self._refresh_authoritative_records()
        return super().list_pending_candidates(subject_id, as_of=as_of)

    def pending_candidate(
        self,
        subject_id: str,
        candidate_hash: str,
        *,
        as_of: datetime,
    ) -> PendingProfileCandidate | None:
        if not self._batching:
            self._refresh_authoritative_records()
        return super().pending_candidate(
            subject_id,
            candidate_hash,
            as_of=as_of,
        )

    def skill_outcomes(self) -> list[SkillOutcomeRecord]:
        if not self._batching:
            self._refresh_authoritative_records()
        return super().skill_outcomes()

    def offline_outcomes(self) -> list[OfflineSkillOutcomeEnvelope]:
        if not self._batching:
            self._refresh_authoritative_records()
        return super().offline_outcomes()

    def save_memory_read_receipt(self, receipt: MemoryReadReceipt) -> None:
        super().save_memory_read_receipt(receipt)
        self._save_state()

    def create_handle(self, binding: HandleBinding) -> None:
        super().create_handle(binding)
        self._save_state()

    def issue_cursor(self, binding: InventoryCursorBinding) -> None:
        super().issue_cursor(binding)
        self._save_state()

    def create_candidate_review_handle(
        self,
        binding: CandidateReviewHandle,
    ) -> None:
        super().create_candidate_review_handle(binding)
        self._save_state()

    def digest_read_enabled(self) -> bool:
        control = self.persistence.load_product_longitudinal_control()
        self._retrieval_policy_epoch = int(
            control["retrieval_policy_epoch"]
        )
        self._digest_enabled = bool(control["digest_read_enabled"])
        return self._digest_enabled

    def enable_digest_read(self, attestation: object) -> None:
        if not isinstance(attestation, DeploymentControlAttestation):
            raise TypeError("deployment attestation is required")
        attestation.require_release_ready()
        if self.orphan_terminal_count() != 0:
            raise ValueError("terminal orphan scan is not zero")
        for _ in range(3):
            control = self.persistence.load_product_longitudinal_control()
            if self.persistence.compare_and_set_product_longitudinal_control(
                expected_retrieval_policy_epoch=int(
                    control["retrieval_policy_epoch"]
                ),
                expected_control_revision=int(control["control_revision"]),
                next_retrieval_policy_epoch=int(
                    control["retrieval_policy_epoch"]
                ),
                digest_read_enabled=True,
                updated_at=datetime.now(timezone.utc),
            ):
                self._digest_enabled = True
                self._retrieval_policy_epoch = int(
                    control["retrieval_policy_epoch"]
                )
                self._deployment_attestations.append(
                    attestation.model_copy(deep=True)
                )
                break
        else:
            raise ValueError("digest enable control CAS failed")
        self._save_state()

    def kill_switch(self, **kwargs: object) -> int:
        now = kwargs["now"]
        if not isinstance(now, datetime):
            raise TypeError("kill-switch time must be datetime")
        for _ in range(3):
            control = self.persistence.load_product_longitudinal_control()
            epoch = int(control["retrieval_policy_epoch"]) + 1
            if self.persistence.compare_and_set_product_longitudinal_control(
                expected_retrieval_policy_epoch=int(
                    control["retrieval_policy_epoch"]
                ),
                expected_control_revision=int(control["control_revision"]),
                next_retrieval_policy_epoch=epoch,
                digest_read_enabled=False,
                updated_at=now,
            ):
                break
        else:
            raise ValueError("kill-switch control CAS failed")
        self._retrieval_policy_epoch = epoch
        self._digest_enabled = False
        self._handles.clear()
        self._cursors.clear()
        self._candidate_review_handles.clear()
        for subject_id, epochs in list(self._epochs.items()):
            self._epochs[subject_id] = epochs.model_copy(
                update={"retrieval_policy_epoch": epoch}
            )
        self._save_state()
        return epoch

    def apply_privacy_action(self, **kwargs: object) -> SubjectEpochs:
        subject_id = str(kwargs["subject_id"])
        for _ in range(3):
            self._refresh_authoritative_records()
            expected = self._authority_snapshot(subject_id)
            self._epochs[subject_id] = SubjectEpochs(
                subject_id=subject_id,
                privacy_epoch=int(expected["privacy_epoch"]),
                authorization_epoch=int(expected["authorization_epoch"]),
                retrieval_policy_epoch=int(
                    expected["retrieval_policy_epoch"]
                ),
            )
            self._batching = True
            try:
                epochs = super().apply_privacy_action(**kwargs)
            finally:
                self._batching = False
            try:
                self._save_state(
                    expected_authority=expected,
                    next_authority={
                        "privacy_epoch": epochs.privacy_epoch,
                        "authorization_epoch": (
                            epochs.authorization_epoch
                        ),
                    },
                )
                return epochs
            except ValueError as exc:
                self._reset_from_persistence()
                if "CAS failed" not in str(exc):
                    raise
        raise ValueError("privacy action authority CAS retry exhausted")

    def apply_authorization_change(self, **kwargs: object) -> SubjectEpochs:
        subject_id = str(kwargs["subject_id"])
        for _ in range(3):
            self._refresh_authoritative_records()
            expected = self._authority_snapshot(subject_id)
            self._epochs[subject_id] = SubjectEpochs(
                subject_id=subject_id,
                privacy_epoch=int(expected["privacy_epoch"]),
                authorization_epoch=int(expected["authorization_epoch"]),
                retrieval_policy_epoch=int(
                    expected["retrieval_policy_epoch"]
                ),
            )
            self._batching = True
            try:
                epochs = super().apply_authorization_change(**kwargs)
            finally:
                self._batching = False
            try:
                self._save_state(
                    expected_authority=expected,
                    next_authority={
                        "privacy_epoch": epochs.privacy_epoch,
                        "authorization_epoch": (
                            epochs.authorization_epoch
                        ),
                    },
                )
                return epochs
            except ValueError as exc:
                self._reset_from_persistence()
                if "CAS failed" not in str(exc):
                    raise
        raise ValueError("authorization action authority CAS retry exhausted")

    def expire_pending_candidates(self, **kwargs: object) -> int:
        count = super().expire_pending_candidates(**kwargs)
        if count:
            self._save_state()
        return count

    def purge_handles(self, **kwargs: object) -> int:
        count = super().purge_handles(**kwargs)
        if count:
            self._save_state()
        return count

    def purge_exact_handles(self, handles: tuple[str, ...]) -> int:
        count = super().purge_exact_handles(handles)
        if count:
            self._save_state()
        return count

    def purge_expired_manifests(self, *, now: datetime) -> int:
        expiring = [
            manifest_id
            for manifest_id, manifest in self._manifests.items()
            if manifest.expires_at <= now
        ]
        count = super().purge_expired_manifests(now=now)
        for manifest_id in expiring:
            self.persistence.purge_product_manifest_envelope(
                manifest_id=manifest_id,
                purge_receipt_ref=(
                    "manifest-purge:" + stable_hash((manifest_id, now.isoformat()))
                ),
                purged_at=now,
            )
        self._save_state()
        return count

    def _save_state(
        self,
        *,
        expected_job_lease: dict[str, object] | None = None,
        expected_authority: dict[str, object] | None = None,
        next_authority: dict[str, object] | None = None,
        expected_terminal_revision: dict[str, object] | None = None,
    ) -> None:
        if self._batching:
            return
        updated_at = datetime.now(timezone.utc)
        self.persistence.sync_product_longitudinal_records(
            state_json=self._state_json(),
            updated_at=updated_at,
            expected_job_lease=expected_job_lease,
            expected_authority=expected_authority,
            next_authority=next_authority,
            expected_terminal_revision=expected_terminal_revision,
            jobs=[
                {
                    "job_id": item.job_id,
                    "idempotency_key": item.idempotency_key,
                    "episode_id": item.episode_id,
                    "terminal_result_id": item.terminal_result_id,
                    "state": item.state.value,
                    "attempt_count": item.attempt_count,
                    "processing_generation": item.processing_generation,
                    "lease_owner": item.lease_owner,
                    "lease_expires_at": (
                        item.lease_expires_at.isoformat()
                        if item.lease_expires_at
                        else None
                    ),
                    "next_attempt_at": item.next_attempt_at.isoformat(),
                    "updated_at": item.updated_at.isoformat(),
                    "json": item.model_dump_json(),
                }
                for item in self._jobs.values()
            ],
            job_events=[
                {
                    "event_id": item.event_id,
                    "job_id": item.job_id,
                    "json": item.model_dump_json(),
                    "created_at": item.created_at.isoformat(),
                }
                for item in self._job_events
            ],
            receipts=[
                {
                    "receipt_id": item.receipt_id,
                    "job_id": item.job_id,
                    "processing_generation": item.processing_generation,
                    "status": item.status.value,
                    "json": item.model_dump_json(),
                    "completed_at": item.completed_at.isoformat(),
                }
                for item in self._receipts.values()
            ],
            digests=[
                {
                    "digest_id": item.digest_id,
                    "digest_hash": item.digest_hash,
                    "subject_id": item.subject_id,
                    "episode_id": item.episode_id,
                    "source_receipt_revision": item.source_receipt_revision,
                    "json": item.model_dump_json(),
                    "expires_at": item.expires_at.isoformat(),
                }
                for item in self._digests.values()
            ],
            digest_events=[
                {
                    "event_id": item.event_id,
                    "digest_id": item.digest_id,
                    "status_sequence": item.status_sequence,
                    "json": item.model_dump_json(),
                    "created_at": item.created_at.isoformat(),
                }
                for events in self._digest_events.values()
                for item in events
            ],
            read_receipts=[
                {
                    "receipt_id": item.receipt_id,
                    "subject_id": item.subject_id,
                    "json": item.model_dump_json(),
                    "completed_at": item.completed_at.isoformat(),
                }
                for item in self._memory_read_receipts.values()
            ],
            candidates=[
                {
                    "candidate_hash": item.candidate_hash,
                    "subject_id": item.subject_id,
                    "status": item.status,
                    "json": item.model_dump_json(),
                    "expires_at": item.expires_at.isoformat(),
                }
                for item in self._pending_candidates.values()
            ],
            skill_outcomes=[
                {
                    "outcome_id": item.outcome_id,
                    "episode_id": item.episode_id,
                    "status": item.status,
                    "json": item.model_dump_json(),
                    "created_at": item.created_at.isoformat(),
                }
                for item in self._skill_outcomes.values()
            ],
            offline_outcomes=[
                {
                    "envelope_id": item.envelope_id,
                    "subject_id": self._offline_subjects[item.envelope_id],
                    "source_result_hash": self._offline_lineage[
                        item.envelope_id
                    ],
                    "withdrawn": item.withdrawn,
                    "json": item.model_dump_json(),
                    "updated_at": updated_at.isoformat(),
                }
                for item in self._offline_outcomes.values()
            ],
        )

    def _state_json(self) -> str:
        payload = {
            "bundles": [
                {
                    "terminal_result_id": bundle.terminal_result_id,
                    "terminal_recorded_at": bundle.terminal_recorded_at.isoformat(),
                    "manifest_id": bundle.manifest.manifest_id,
                    "job_id": bundle.job.job_id,
                }
                for bundle in self._bundles.values()
            ],
            "manifest_purged": sorted(self._manifest_purged),
            "jobs": [item.model_dump(mode="json") for item in self._jobs.values()],
            "job_events": [item.model_dump(mode="json") for item in self._job_events],
            "attempts": [item.model_dump(mode="json") for item in self._attempts],
            "receipts": [item.model_dump(mode="json") for item in self._receipts.values()],
            "semantic_receipts": self._semantic_receipt_by_job,
            "digests": [item.model_dump(mode="json") for item in self._digests.values()],
            "digest_events": [
                item.model_dump(mode="json")
                for events in self._digest_events.values()
                for item in events
            ],
            "pending_candidates": [
                item.model_dump(mode="json") for item in self._pending_candidates.values()
            ],
            "skill_outcomes": [
                item.model_dump(mode="json") for item in self._skill_outcomes.values()
            ],
            "offline_outcomes": [
                item.model_dump(mode="json") for item in self._offline_outcomes.values()
            ],
            "offline_subjects": self._offline_subjects,
            "offline_lineage": self._offline_lineage,
            "memory_read_receipts": [
                item.model_dump(mode="json")
                for item in self._memory_read_receipts.values()
            ],
            "handles": [item.model_dump(mode="json") for item in self._handles.values()],
            "cursors": [item.model_dump(mode="json") for item in self._cursors.values()],
            "candidate_review_handles": [
                item.model_dump(mode="json")
                for item in self._candidate_review_handles.values()
            ],
            "deployment_attestations": [
                item.model_dump(mode="json")
                for item in self._deployment_attestations
            ],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def _hydrate(self) -> None:
        from sleepagent.radar_agent.product_agent.runtime_contracts import (
            ProductEpisodeRunResult,
        )

        for raw in self.persistence.list_all_product_nonterminal_result_json():
            result = ProductEpisodeRunResult.model_validate_json(raw)
            result_id = stable_hash(result.model_dump(mode="json"))
            self._results.setdefault(result.receipt.episode_id, []).append(result)
            self._result_identity[
                (result.receipt.episode_id, result.receipt.receipt_revision)
            ] = result_id
        for result_id, raw in self.persistence.list_product_terminal_result_rows():
            result = ProductEpisodeRunResult.model_validate_json(raw)
            self._results.setdefault(result.receipt.episode_id, []).append(result)
            self._result_identity[
                (result.receipt.episode_id, result.receipt.receipt_revision)
            ] = result_id
        raw_state = self.persistence.load_product_longitudinal_operational_state()
        if raw_state is None:
            return
        state = json.loads(raw_state)
        self._manifest_purged = set(state.get("manifest_purged", []))
        self._jobs = {
            item.job_id: item
            for item in map(InductionJob.model_validate, state.get("jobs", []))
        }
        for job in self._jobs.values():
            if job.input_manifest_ref in self._manifest_purged:
                continue
            envelope = self.persistence.load_product_manifest_envelope(
                job.input_manifest_ref
            )
            if envelope is not None:
                self._manifests[job.input_manifest_ref] = self._cipher.decrypt(
                    job.input_manifest_ref,
                    envelope,
                )
        self._job_events = list(
            map(InductionJobEvent.model_validate, state.get("job_events", []))
        )
        durable_jobs = list(
            map(
                InductionJob.model_validate_json,
                self.persistence.list_product_induction_job_json(),
            )
        )
        if durable_jobs:
            self._jobs = {item.job_id: item for item in durable_jobs}
        durable_events = list(
            map(
                InductionJobEvent.model_validate_json,
                self.persistence.list_product_induction_job_event_json(),
            )
        )
        if durable_events:
            self._job_events = durable_events
        self._attempts = list(
            map(InductionAttemptRecord.model_validate, state.get("attempts", []))
        )
        self._receipts = {
            item.receipt_id: item
            for item in map(InductionReceipt.model_validate, state.get("receipts", []))
        }
        self._semantic_receipt_by_job = dict(state.get("semantic_receipts", {}))
        self._digests = {
            item.digest_id: item
            for item in map(EpisodeDigest.model_validate, state.get("digests", []))
        }
        self._digest_events = {}
        for event in map(
            EpisodeDigestStatusEvent.model_validate,
            state.get("digest_events", []),
        ):
            self._digest_events.setdefault(event.digest_id, []).append(event)
        self._pending_candidates = {
            (item.subject_id, item.candidate_hash): item
            for item in map(
                PendingProfileCandidate.model_validate,
                state.get("pending_candidates", []),
            )
        }
        self._skill_outcomes = {
            item.outcome_id: item
            for item in map(
                SkillOutcomeRecord.model_validate,
                state.get("skill_outcomes", []),
            )
        }
        self._offline_outcomes = {
            item.envelope_id: item
            for item in map(
                OfflineSkillOutcomeEnvelope.model_validate,
                state.get("offline_outcomes", []),
            )
        }
        self._offline_subjects = dict(state.get("offline_subjects", {}))
        self._offline_lineage = dict(state.get("offline_lineage", {}))
        self._memory_read_receipts = {
            item.receipt_id: item
            for item in map(
                MemoryReadReceipt.model_validate,
                state.get("memory_read_receipts", []),
            )
        }
        durable_receipts = list(
            map(
                InductionReceipt.model_validate_json,
                self.persistence.list_product_induction_receipt_json(),
            )
        )
        if durable_receipts:
            self._receipts = {
                item.receipt_id: item for item in durable_receipts
            }
            self._semantic_receipt_by_job = {
                item.job_id: item.receipt_id
                for item in durable_receipts
                if item.status
                in {
                    InductionReceiptStatus.SUCCEEDED,
                    InductionReceiptStatus.EXCLUDED,
                }
            }
        durable_digests = list(
            map(
                EpisodeDigest.model_validate_json,
                self.persistence.list_product_episode_digest_json(),
            )
        )
        if durable_digests:
            self._digests = {item.digest_id: item for item in durable_digests}
        durable_digest_events = list(
            map(
                EpisodeDigestStatusEvent.model_validate_json,
                self.persistence.list_product_episode_digest_event_json(),
            )
        )
        if durable_digest_events:
            self._digest_events = {}
            for item in durable_digest_events:
                self._digest_events.setdefault(item.digest_id, []).append(item)
        durable_candidates = list(
            map(
                PendingProfileCandidate.model_validate_json,
                self.persistence.list_product_pending_candidate_json(),
            )
        )
        if durable_candidates:
            self._pending_candidates = {
                (item.subject_id, item.candidate_hash): item
                for item in durable_candidates
            }
        durable_skill_outcomes = list(
            map(
                SkillOutcomeRecord.model_validate_json,
                self.persistence.list_product_skill_outcome_json(),
            )
        )
        if durable_skill_outcomes:
            self._skill_outcomes = {
                item.outcome_id: item for item in durable_skill_outcomes
            }
        durable_offline = (
            self.persistence.list_product_offline_skill_outcome_rows()
        )
        if durable_offline:
            self._offline_outcomes = {}
            self._offline_subjects = {}
            self._offline_lineage = {}
            for subject_id, source_result_hash, raw in durable_offline:
                item = OfflineSkillOutcomeEnvelope.model_validate_json(raw)
                self._offline_outcomes[item.envelope_id] = item
                self._offline_subjects[item.envelope_id] = subject_id
                self._offline_lineage[item.envelope_id] = source_result_hash
        durable_read_receipts = list(
            map(
                MemoryReadReceipt.model_validate_json,
                self.persistence.list_product_memory_read_receipt_json(),
            )
        )
        if durable_read_receipts:
            self._memory_read_receipts = {
                item.receipt_id: item for item in durable_read_receipts
            }
        self._handles = {
            item.handle: item
            for item in map(HandleBinding.model_validate, state.get("handles", []))
        }
        self._cursors = {
            item.cursor: item
            for item in map(
                InventoryCursorBinding.model_validate,
                state.get("cursors", []),
            )
        }
        self._candidate_review_handles = {
            item.handle: item
            for item in map(
                CandidateReviewHandle.model_validate,
                state.get("candidate_review_handles", []),
            )
        }
        control = self.persistence.load_product_longitudinal_control()
        self._retrieval_policy_epoch = int(
            control["retrieval_policy_epoch"]
        )
        self._digest_enabled = bool(control["digest_read_enabled"])
        self._epochs = {
            str(item["subject_id"]): SubjectEpochs(
                subject_id=str(item["subject_id"]),
                privacy_epoch=int(item["privacy_epoch"]),
                authorization_epoch=int(item["authorization_epoch"]),
                retrieval_policy_epoch=self._retrieval_policy_epoch,
            )
            for item in (
                self.persistence.list_product_longitudinal_subject_epochs()
            )
        }
        self._deployment_attestations = list(
            map(
                DeploymentControlAttestation.model_validate,
                state.get("deployment_attestations", []),
            )
        )
        for raw in self.persistence.load_product_publication_entries():
            entry = PublicationJournalEntry.model_validate_json(raw)
            self._publication_journal[entry.intent_id] = entry
        for binding in state.get("bundles", []):
            manifest = self._manifests.get(binding["manifest_id"])
            job = self._jobs.get(binding["job_id"])
            if manifest is None or job is None:
                continue
            bundle = TerminalBundle(
                terminal_result_id=binding["terminal_result_id"],
                terminal_recorded_at=binding["terminal_recorded_at"],
                manifest=manifest,
                job=job,
            )
            self._bundles[bundle.terminal_result_id] = bundle


__all__ = [
    "PRODUCT_STATE_PERSISTENCE_VERSION",
    "PersistentCareContextStore",
    "PersistentCommitJournal",
    "PersistentMemoryContextStore",
    "PersistentProductEpisodeResultStore",
]
