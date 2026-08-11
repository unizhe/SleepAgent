# Habit Profile acceptance evidence format

The production release gate accepts only evidence explicitly marked `real`.
Files or identifiers containing `simulated`, `synthetic`, `fixture`, `template`
or `模拟` cannot be promoted by changing one Boolean or provenance field.

## Material package

Use these canonical filenames:

- `habit_usability_report.json`
- `habit_domain_review.json`
- `provider_observations.json`

Create a drift-free collection package directly from the current checked-in
catalog, scenario enum and release manifest:

```bash
python -m sleepagent.product_runtime.acceptance_materials \
  <new-material-directory> \
  --initialize-templates \
  --manifest docs/audits/agent-architecture/ACCEPTANCE-MANIFEST.json
```

The initializer never overwrites an existing material file. It creates three
current-catalog usability slots, a review row for every current Habit concept,
and all 68 required scenario/repetition slots. These are deliberately
ineligible placeholders: they are marked `simulated`, have `passed: false`,
contain `template` identifiers and cannot be promoted by changing
`evidence_kind`. Replace each slot with evidence from the actual session,
signed review or executed provider run.

To exercise the complete schema without claiming a real release, generate an
explicitly synthetic package in a different empty directory:

```bash
python -m sleepagent.product_runtime.acceptance_materials \
  <new-simulated-material-directory> \
  --initialize-simulated-fixture \
  --manifest docs/audits/agent-architecture/ACCEPTANCE-MANIFEST.json
```

This produces five synthetic target-age interactions, 22 current-catalog
review rows and all 68 provider observation slots. Synthetic request IDs and
receipt hashes are deterministic test shapes; no provider call is made. The
result must remain `evidence_kind: simulated` and is expected to fail the
release gate.

Validate the package against the checked-in release manifest:

```bash
python -m sleepagent.product_runtime.acceptance_materials \
  <material-directory-or-zip> \
  --manifest docs/audits/agent-architecture/ACCEPTANCE-MANIFEST.json
```

ZIP input is read through a bounded archive path: unsafe paths, symlinks,
encrypted entries, ambiguous canonical files and oversized archives are
rejected before any material is parsed.

Exit code `0` means the package is structurally eligible as release evidence.
Exit code `2` means it is still simulated, incomplete, stale or unverifiable.
Passing this material audit does not itself make the whole product release
eligible; the final `ProductAgentReleaseVerifier` remains authoritative.

## Real usability report

The report must set `"evidence_kind": "real"` and contain 3–5 pseudonymous
participant observations:

```json
{
  "evidence_kind": "real",
  "report_id": "habit-usability:2026-08",
  "conducted_at": "2026-08-15T09:30:00+08:00",
  "interaction_only_not_medical_validation": true,
  "reviewer_ref": "reviewer:ux-001",
  "attestation": {
    "protocol_version": "sleep-habit-usability.v1",
    "facilitator_ref": "facilitator:001",
    "observed_participant_refs": [
      "participant:001",
      "participant:002",
      "participant:003"
    ],
    "participant_interactions_observed": true,
    "synthetic_data_used": false,
    "signed_at": "2026-08-15T12:00:00+08:00",
    "signature_reference": "signature:usability:2026-08"
  },
  "observations": [
    {
      "participant_ref": "participant:001",
      "age_band": "60-69",
      "understood_personalization_purpose": true,
      "understood_questions_are_skippable": true,
      "understood_separate_persistence_confirmation": true,
      "skip_attempt_succeeded": true,
      "questions_presented": 2,
      "erroneous_confirmation": false,
      "interruption_rating": 2,
      "notes": "Interaction-only observation without personal health history."
    }
  ]
}
```

The release gate requires all four comprehension/skip fields to be `true` and
`erroneous_confirmation` to be `false` for every participant.
For real evidence, the facilitator attestation is mandatory, its participant
set must exactly equal the report observations, and it must state that the
interactions were observed and synthetic data was not used.

## Real domain review

The report must set `"evidence_kind": "real"` and bind all current catalog
content. Obtain the current catalog identity from code rather than copying an
old document:

```bash
python -c "from sleepagent.product_runtime.acceptance import current_habit_catalog_hash; print(current_habit_catalog_hash())"
```

The report must contain:

- one or more identifiable professional reviewers with role, qualification,
  organization and conflict-of-interest declaration;
- all six review scopes set to `true`;
- exactly the current concept set and count;
- exact concept ID, semantic version, content version, TTL and persistence
  eligibility for every concept;
- approved wording, options, TTL, persistence and safety findings;
- an overall decision, approval reference and signoff that names known
  reviewers.

The verifier recomputes the catalog hash and compares each concept. Renaming,
adding or deleting a concept, changing TTL/content version or changing
persistence eligibility invalidates the review.

## Real provider observation

Each row in `provider_observations.json` must use the current
`release_identity.identity_hash`, set `"evidence_kind": "real"`, and include a
sanitized provider receipt:

```json
{
  "observations": [
    {
      "observation_id": "normal_morning:run-001",
      "scenario": "normal_morning",
      "release_identity_hash": "<current-identity-hash>",
      "repetition": 1,
      "role": "elder",
      "observed_agents": ["sleep_care", "evidence_reasoning"],
      "evidence_semantics": ["observed_fact"],
      "hard_violations": [],
      "evidence_kind": "real",
      "real_provider": true,
      "provider_receipt": {
        "receipt_ref": "trace:episode-id",
        "receipt_hash": "<sha256-of-sanitized-invocation-records>",
        "providers": ["provider-name"],
        "model_ids": ["provider-model-id"],
        "provider_request_ids": ["provider-request-id"],
        "invocation_ids": ["agent-invocation-id"],
        "executed_at": "2026-08-17T09:30:00+08:00",
        "sanitized": true
      },
      "runtime_receipt": {
        "episode_id": "episode-id",
        "trace_ref": "trace:episode-id",
        "result_hash": "<sha256-of-sanitized-ProductEpisodeRunResult>",
        "status": "complete",
        "execution_mode": "intelligent",
        "failure_codes": [],
        "recorded_at": "2026-08-17T09:30:00+08:00",
        "sanitized": true
      },
      "external_action_receipt": null,
      "state_persistence_receipts": [],
      "domain_reviewed": false,
      "passed": true
    }
  ]
}
```

Provider request IDs and receipt hashes cannot be reused between observations.
Runtime result hashes and trace references cannot be reused either. Construct
the runtime receipt from the same unmodified `ProductEpisodeRunResult`; the
hash binds the sanitized full result, while the provider receipt separately
binds its Agent invocation records.
All non-deterministic scenarios require three distinct successful repetitions
with real provider receipts. `data_quality` and `urgent` each require one real
executed deterministic observation and therefore use `real_provider: false`
with `provider_receipt: null`.

The `external_action` scenario additionally requires the confirmed target to
reach the configured HTTPS gateway and return a real gateway request ID plus a
`pending` or `delivered` status. A default/stub executor, an unconfigured
endpoint, an `UNKNOWN` commit receipt or a replay of a pending journal
reservation is not a successful execution. Record it in
`external_action_receipt`, including the commit ToolReceipt hash and exact
Safety-reviewed target hash.

The `memory`, `care_plan` and `care_followup` collections must also verify the
committed database version after a runtime restart; an in-process object is not
cross-day evidence. Record the before/after versions, hashed subject ref,
commit receipt ID and restart time in `state_persistence_receipts`. The release
verifier requires these typed proofs for every repetition of those scenarios.

A simulated fixture may carry synthetic receipt-shaped data while keeping
`"evidence_kind": "simulated"` and `"real_provider": false`; this only exercises
parsing, uniqueness and manifest assembly. The release verifier ignores such
rows. A simulated usability attestation may likewise state
`"synthetic_data_used": true`; changing it to real is rejected unless the
attestation states that no synthetic data was used and all other real-evidence
requirements are satisfied.

The scenario names are the exact values of `AcceptanceScenario`; descriptive
sleep-habit examples such as `short_sleep` or `caffeine_late` are not aliases
and cannot be relabeled without actually executing the corresponding
architecture acceptance scenario.
