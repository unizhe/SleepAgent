# Sleep Habit Profile acceptance matrix

Automated evidence below covers the 28 implementation criteria in
`PLAN.md`. The separate 3–5 participant interaction study is intentionally
not marked complete; see `USABILITY-TEST-PROTOCOL.md`.

| # | Result | Automated evidence |
|---:|:---:|---|
| 1 | PASS | `test_skipping_all_habit_questions_still_delivers_morning_answer` |
| 2 | PASS | `test_selection_is_reviewed_plan_bound_neutral_and_episode_budgeted`, `test_episode_budget_and_cross_episode_cooldown_survive_restart`, `test_persistent_question_budget_rejects_cross_connection_stale_issue`, `test_persistent_cooldown_rejects_two_stale_concurrent_episodes` |
| 3 | PASS | `test_normal_morning_does_not_ask_to_complete_habit_profile` |
| 4 | PASS | `test_capture_rejects_forgery_invalid_values_replay_and_cross_episode` |
| 5 | PASS | `test_nonanswers_do_not_form_facts_and_never_ask_is_minimal_suppression`, `test_never_ask_is_not_partially_saved_when_capture_fails`, `test_confirmed_never_ask_survives_api_runtime_restart`, `test_bounded_number_and_confirmation_bias_are_deterministic` |
| 6 | PASS | `test_family_cannot_answer_subjective_and_negative_requires_opportunity` |
| 7 | PASS | `test_answer_builds_pending_atomic_change_set_without_writing_memory`, `test_current_answer_is_typed_evidence_but_not_memory_until_commit` |
| 8 | PASS | `test_manifest_rebuild_and_atomic_failure_prevent_partial_commit`, `test_elder_can_remove_one_candidate_and_old_manifest_is_revoked` |
| 9 | PASS | `test_objective_baseline_is_versioned_artifact_not_habit_fact` |
| 10 | PASS | `test_overlapping_sources_dispute_but_nonoverlapping_change_does_not` |
| 11 | PASS | `test_stale_version_and_expiry_are_deterministic_and_not_current` |
| 12 | PASS | `test_habit_response_safety_signal_preempts_remaining_agent_path` |
| 13 | PASS | `test_evidence_gate_preserves_authorized_observer_report_semantic` |
| 14 | PASS | `test_forget_removes_personalization_but_retains_truthful_audit_notice`, `test_elder_optional_intake_confirm_read_and_forget_flow`, frontend contract copy/actions |
| 15 | PASS | `test_profile_tool_context_is_user_data_and_not_disclosed_to_care`, `test_minimal_role_reads_and_observer_origin_survive_confirmation` |
| 16 | PASS | `test_unavailable_profile_service_degrades_without_blocking_core_answer` |
| 17 | PASS | `test_only_elder_exact_manifest_can_confirm_profile`, `test_generic_free_text_memory_rejects_habit_dual_write` |
| 18 | PASS | `test_paired_replay_changes_only_relevant_profile_evidence` |
| 19 | PASS | `test_short_text_remains_user_data_and_cannot_define_instruction` |
| 20 | PASS | `test_receipt_rejects_subject_role_version_and_expiry_tampering`, `test_selection_receipt_consumption_survives_restart`, Episode-budget/CAS tests above |
| 21 | PASS | `test_episode_only_and_clinical_values_cannot_enter_profile` |
| 22 | PASS | `test_family_cannot_answer_subjective_and_negative_requires_opportunity` |
| 23 | PASS | `test_stale_version_and_expiry_are_deterministic_and_not_current` |
| 24 | PASS | `test_bounded_number_and_confirmation_bias_are_deterministic` |
| 25 | PASS | `test_reviewed_registry_is_capability_not_a_fifth_agent`, generic-memory dual-write test above |
| 26 | PASS | `test_family_observation_requires_later_elder_owned_change_set`, `test_minimal_role_reads_and_observer_origin_survive_confirmation` |
| 27 | PASS | `test_refusing_observer_persistence_does_not_delete_current_evidence` |
| 28 | PASS | `test_episode_only_and_clinical_values_cannot_enter_profile` |

Product exposure proof:

- Authenticated FastAPI flows cover optional intake, current Evidence use,
  exact-manifest confirmation/replay, pre-confirmation pruning, Profile read,
  correction, forget, observer-origin retention and safety preemption in
  `tests/test_habit_profile_api.py`.
- The same-origin BFF keeps the API key and actor/role/subject binding
  server-side, fails closed when identity is absent and exposes no proactive
  default. The elder UI is discoverable at `/habit-profile` and its critical
  consent/correction/forget copy is guarded by
  `tests/test_habit_profile_frontend_contract.py`.
- `npm run typecheck` and the Next.js production `npm run build` both pass.

The repository proof command is `PYTHONDONTWRITEBYTECODE=1 pytest -q`.
Its current result is recorded in `docs/audits/agent-architecture/PLAN-REVIEW-LOG.md`;
the separate real 3–5 participant gate remains pending and is not represented
as an automated PASS.

## Completion and release gates

| Gate | Current evidence | Status |
|---|---|:---:|
| Phase A deterministic contracts, reviewed-catalog mechanics, policy and safety routing | Contract/catalog tests and all mapped criteria above | PASS (implementation) |
| Named domain/medical sign-off for wording, TTL and catalog release | The current v23 collection package contains all 22 catalog-bound review slots, but they remain unsigned templates | PENDING |
| Phase B integration into the four-Agent runtime and single Commit Controller path | Runner/governance tests; exact roster test; no generic-memory dual write; database-backed Product Memory/Care state, durable commit journal, atomic Profile state/commit ledger and persistent Questionnaire state | PASS |
| Phase C optional intake, review/correct/forget and elder consent UI | Authenticated API tests, restart recovery, frontend contracts and production build | PASS (implementation) |
| Real 3–5 participant target-age usability study | The current v23 package contains three catalog/identity-bound template slots only; no participant interaction is claimed | PENDING |
| Inherited 1+2+1 production release evidence | The current v23 package contains all 24 scenarios/68 identity-bound template slots, but no real provider execution is claimed | PENDING |

The current `ProductAgentReleaseVerifier` therefore returns `eligible: false`
with no hard violations. This is deliberate fail-closed behavior: the green
repository suite proves implementation invariants, not real-user usability,
medical/domain approval or production-model release evidence.

The current collection skeleton is
`docs/product/habit-profile/real-evidence-v23-collection`. It is bound to release
identity
`7ece39290333119ff221286b415934a7306f0e58c23b4b10c61488b5db424f45`
and catalog hash
`d4477443c4b3691afc99f5833958e31a98e4d5caa1c68aa129b8f4a7bf5d6903`.
Its placeholder markers and simulated evidence kind intentionally keep it
ineligible until every applicable slot is replaced by actually observed and
attested material.

The historical v18 development fixture is
`docs/product/habit-profile/simulated-evidence-v18-complete`. Its byte-stable generated
archive root is `sleepagent-v18-complete-simulated-evidence` with SHA-256
`f6ba6d212692b908f7e5fc5259348611828c3a9601f787d5887c1a5a0d1c4f70`;
the ZIP is generated under test temporary storage and is not committed.
`SIMULATION-COVERAGE-v18.json` binds the canonical file hashes and explicitly
accounts for five simulated observations, one synthetic facilitator
attestation, two fictional named reviewer personas, ten concepts, 68 scenario
observations, 66 synthetic provider receipts/request IDs and the two
deterministic no-receipt observations.
It can be regenerated in a new empty directory with
`--initialize-simulated-fixture`. Its expected audit is exit code `2`,
`usable_as_simulation_fixture: true` and
`release_evidence_eligible: false`, with `usability.simulated`,
`domain.simulated`, `provider.release_identity_mismatch` and
`release.gate_failed`. Its catalog and scenario set remain useful historical
fixtures, but its v18 release identity is intentionally stale against v23.

The generated development archive is covered by
`test_checked_in_v18_simulation_coverage_inventory_matches_artifacts` and its
receipt/attestation semantics by
`test_v18_simulated_receipt_and_attestation_are_honestly_parseable`. Generate
and audit it through those tests with:

```bash
PYTHONDONTWRITEBYTECODE=1 pytest -q \
  tests/test_product_agent_acceptance.py \
  -k 'v18_simulated or checked_in_v18'
```

Its expected result is exit code `2`, `usable_as_simulation_fixture: true` and
`release_evidence_eligible: false`, with `usability.simulated`,
`domain.simulated`, `provider.release_identity_mismatch` and
`release.gate_failed` findings because it is a historical fixture. The older
`sleepagent_simulated_acceptance_materials` directory remains a separate
negative drift fixture.
