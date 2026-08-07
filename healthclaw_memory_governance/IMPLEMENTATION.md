# Longitudinal memory governance operations

This implementation borrows HealthClaw's separation of shared governance,
personal state, and Episode-derived memory, but keeps SleepAgent's existing
four-role `1+2+1` architecture. No HealthClaw source code, directory layout,
medical task stack, personal SOP store, or automatic Skill mutation is used.

## Runtime boundaries

- `ProductEpisodeRunResult` is audit-only. It is not a Tool source or resolver.
- A terminal write atomically creates the canonical result revision, an
  encrypted allowlisted `InductionInputManifest`, and one `InductionJob`.
- `DeterministicInductionWorker` reads only the Manifest repository. It has no
  provider/model dependency and cannot read raw Episode audit payloads.
- Generic memory writes go only through `DeterministicCommitController` and
  always create `GovernedMemoryItemV2`. Rows without a discriminator load as
  `LegacyMemoryItemV1` and are limited to explicit elder inventory review.
- `memory.read` is purpose- and selector-bound. Evidence is the only Agent that
  can query `EpisodeDigest`; Care and Safety have no direct longitudinal read.
- A Digest is an untrusted hint. Evidence must call
  `memory.resolve_source`; an unavailable canonical resolver fails closed.
- Pending induction candidates are nonoperative. Explicit elder review yields a
  short-lived handle; preparing one yields the existing typed
  `MemoryChangeCandidate`, which still needs the existing exact confirmation
  and Commit Controller path. An explicitly declined candidate ID is frozen
  into the Manifest and excluded from later offline candidate projection.
- Production Skill Registry remains read-only to induction and offline outcome
  flows.

## Deployment and cutover

1. Apply migration `007_longitudinal_memory_governance`.
2. Configure `SLEEPAGENT_MANIFEST_KEK` for PostgreSQL. The runtime refuses
   persistent Manifest storage without it. The deterministic SQLite key is for
   local tests only.
3. Deploy the new writer and fence the legacy result writer. Terminal calls to
   the legacy append API fail.
4. Verify `count_product_terminal_orphans() == 0`.
5. Run the deterministic worker in shadow mode and verify Job event/attempt,
   Receipt, encrypted Manifest purge, supersede, and privacy propagation.
6. Run the isolated longitudinal benchmark. Synthetic results are engineering
   release evidence only, not evidence of clinical effectiveness.
7. Construct `DeploymentControlAttestation` from independently verified
   encryption, backup crypto-expiry, least-privilege, publication-journal,
   writer-fencing, orphan-scan, and benchmark evidence. Digest reads remain
   disabled unless every gate passes.

## Retention and privacy

- Digest payload: at most 90 days from transaction-owned terminal time.
- Pending candidate: at most 30 days.
- Manifest plaintext: at most 7 days; purge destroys ciphertext, nonce, and
  wrapped data key and prevents replay from falling back to raw audit.
- Job leasing and terminal processing updates use durable cross-process CAS;
  succeeded/dead-letter projections cannot be downgraded by a stale writer,
  and a manual replay advances processing generation.
- Model-visible handles/cursors: at most 15 minutes and bound to
  subject/actor/purpose/invocation plus privacy, authorization, and retrieval
  policy epochs.
- Forget, withdraw, delete, correction, and restrictive authorization changes
  advance epochs, append lifecycle events, withdraw eligible offline records,
  and purge affected handles. They do not rewrite immutable payload hashes.

## Kill-switch drill

Call the repository `kill_switch(reason_code=..., now=...)`. A successful drill
must demonstrate all of the following:

1. retrieval-policy epoch increases monotonically;
2. Digest retrieval is disabled;
3. all Memory/Digest handles, inventory cursors, and pending-candidate handles
   are cleared;
4. an already assembled slice fails model-input and prepublication
   revalidation;
5. no fallback to broad Memory, raw Episode audit, or legacy writer occurs;
6. re-enablement requires a new complete deployment attestation and zero
   terminal orphans.

The production package excludes `benchmarks/healthclaw_memory_governance`.
Its `full_history` condition is evaluation-only, structured, authorized,
unexpired, and never raw conversation or raw Episode data.
