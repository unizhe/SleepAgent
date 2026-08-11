# Real acceptance evidence collection runbook (v23 package)

This runbook coordinates collection; it is not evidence and must not be signed
on behalf of any participant, facilitator, reviewer or provider.

The collection target is `real-evidence-v23-collection`. It is bound to:

- release identity:
  `7ece39290333119ff221286b415934a7306f0e58c23b4b10c61488b5db424f45`
- Habit catalog:
  `d4477443c4b3691afc99f5833958e31a98e4d5caa1c68aa129b8f4a7bf5d6903`
- acceptance schema: `sleepagent-product-agent-acceptance.v26`

If code changes any bound identity component or any Habit catalog entry, stop
collection and regenerate a new empty template package. Never relabel evidence
collected against an older identity.

## 1. Actual 60+ interaction sessions

Follow `USABILITY-TEST-PROTOCOL.md` with 3–5 actual participants aged 60 or
older. Use pseudonymous participant references and an isolated test subject per
participant. Do not store names, contact details, diagnoses or free-form health
history in the JSON report.

For every participant, the facilitator records the observation immediately
after the four fixed tasks. After reconciling the participant set, the actual
facilitator completes the attestation in `habit_usability_report.json`:

- `participant_interactions_observed: true`;
- `synthetic_data_used: false`;
- `observed_participant_refs` exactly matching the observation rows;
- the real observation and signing timestamps with timezone offsets;
- `signature_reference` pointing to the organization's immutable signed record.

The signed record should bind the report ID, facilitator identity, participant
references, protocol version and SHA-256 of the finalized sanitized JSON. Keep
the signed source in the approved evidence system; do not add a signature image
or participant identity data to this repository.

## 2. Actual qualified professional review

Give the reviewer the unchanged catalog rendered in
`habit_domain_review.json`, the product wording, answer options, TTL values,
persistence eligibility, source semantics and safety escalation boundaries.
The reviewer must be identifiable and qualified for the scope. Record their
actual display name, professional role, qualification, organization and
conflict-of-interest declaration.

Every one of the 22 concept rows must be reviewed. An approved bundle requires
all five per-concept findings and the overall decision to be `approved`, with
no requested changes. The actual signer then records:

- `signed_by` using reviewer refs declared in the same report;
- an actual timezone-aware `signed_at`;
- an immutable `signature_reference`;
- an `approval_reference` and per-concept `approval_record_ref` that resolve in
  the organization's evidence system.

The external signed record should bind the report ID, reviewer identity and
qualification, current catalog hash, approval decision and SHA-256 of the
finalized sanitized JSON. A typed name in JSON by itself is not an actual
signature.

## 3. Actual provider executions

Run the exact identity-bound build under the production-candidate provider configuration.
The OpenAI-compatible adapter must receive and retain a non-empty request ID
from the provider response for every Agent invocation. A fallback, replay,
mock, locally fabricated ID or run against another release identity is not
eligible.

Collect 68 observations:

- one actual deterministic execution for `data_quality`;
- one actual deterministic execution for `urgent`;
- three independent actual provider executions for each of the other 22
  scenarios.

For every provider-backed row, construct the observation from the unmodified
`ProductEpisodeRunResult` with `observation_from_runtime`. This derives the
receipt hash from sanitized invocation records and rejects a run if any Agent
invocation lacks a provider request ID. Preserve the actual provider/model,
request IDs, invocation IDs, execution time and trace reference. Request IDs,
invocation IDs and receipt hashes must not be copied between rows.

The two deterministic rows use `evidence_kind: real`,
`real_provider: false`, and `provider_receipt: null`; all other rows use
`evidence_kind: real`, `real_provider: true`, and an actual receipt. All rows
must bind the exact release identity above and contain no hard violation.

For the `external_action` scenario, configure the exact production-candidate
HTTPS gateway using the `SLEEPAGENT_EXTERNAL_*_URL` variables. Complete the
typed Safety and user-confirmation flow, then retain the gateway request ID and
the durable Product commit receipt. An unconfigured executor, the historical
in-process `delivered` stub, a pending journal reservation, or an outcome
reported as `UNKNOWN` is not a passing external-action execution.

For the `memory`, `care_plan` and `care_followup` scenarios, verify the
database-backed Product Memory/Care version after restarting the runtime.
In-process state alone is not evidence of a cross-day lifecycle.
Build the typed row with `state_persistence_receipt_from_restart`; it rejects
the wrong Commit Controller tool, non-state effects, non-success outcomes,
missing idempotency bindings and non-advancing versions.

Secrets, prompts containing personal data and raw provider responses must not
be copied into the material package. Store only the sanitized receipt fields
defined by `ProviderRunReceipt`.

## 4. Audit and release handoff

Audit the completed directory without rewriting it:

```bash
python -m sleepagent.radar_agent.product_agent.acceptance_materials \
  sleep_habit_profile/real-evidence-v23-collection \
  --manifest agent_architecture/ACCEPTANCE-MANIFEST.json
```

Exit code `0` means the material is structurally eligible for assembly. It does
not independently authorize release. Reconcile the JSON SHA-256 values with
the external attestation/signature records, perform an independent
privacy/credential check, then place the three validated objects into the
release manifest and run the repository acceptance tests.

Any mismatch, unverifiable reference, missing provider request ID, participant
set discrepancy, requested professional-review change or identity drift keeps
the gate closed.
