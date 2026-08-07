# Plan Review Log: SleepAgent 模拟数据后端闭环与终端演示
Act 1 (grill) complete — plan locked with the user. MAX_ROUNDS=5.

## Round 1 — Codex review

1. **Authority conflict in Digest demo mode.** The plan says the prior
   longitudinal-memory plan remains authoritative, but also proposes a new
   production-package read mode that bypasses its benchmark gate. Even though
   the user approved a replay-only demonstration, the precedence and import
   boundary are ambiguous. **Fix:** state an explicit, narrowly scoped
   replay-only amendment and keep the release-attested API/state untouched;
   prove production cannot construct, persist, or deserialize the demo lease.
2. **Night-15 baseline leakage.** The journey says the fifteenth night both
   establishes a 15-night baseline and supports a baseline claim. A
   current-night-vs-baseline comparison could accidentally include the target
   night in its own baseline. **Fix:** make checkpoint 15 a longitudinal
   15-night summary only; forbid current-vs-baseline there unless the baseline
   window is a prior, disjoint established window.
3. **Crash boundary is underspecified.** The plan promises no duplicate
   revision/analysis/view/Digest, but allows a vague "callback/outbox" wiring.
   An in-memory callback between lifecycle, fast path, and Agent submission can
   strand work after commit or duplicate effects after restart. **Fix:** require
   committed domain events plus persistent idempotent operations/outbox and
   define transaction ownership and at-least-once semantics explicitly.
4. **Application ownership is ambiguous.** The plan names both `backend.main`
   and standalone `sleep_api` while adding a composition app, but does not say
   which process owns worker start/stop or how duplicate global runtimes are
   prevented. **Fix:** define a single app factory and one runtime instance per
   process; existing module-level apps delegate to that factory and never
   independently start duplicate workers for the same workspace.
5. **Manual live verification has no durable session protocol.** A command like
   `verify journey --manual` cannot both exit at a checkpoint and later know
   which expected run it is completing. **Fix:** persist a typed JourneyRun
   aggregate with scenario/version/checkpoint/model-mode/expected-hash and make
   `verify` inspect that aggregate after the user resumes it with separate
   commands.
6. **Env-file handling is security-sensitive but unspecified.** Loading a shell
   `.env` file could execute substitutions or accept unsafe permissions while
   the plan only says not to log values. **Fix:** use a strict non-executing
   dotenv parser, reject duplicate/unknown/conflicting live keys, check file
   ownership/mode, and never invoke a shell to source it.
7. **The live fixed-Care oracle may be brittle.** Requiring DeepSeek to choose
   one exact action while claiming not to use a text golden can turn harmless
   model variation into failure or tempt prompt overfitting. **Fix:** require a
   Care disposition and exactly one catalog-valid low-risk candidate from an
   explicit allowed set; keep `consistent-wake-time` exact only in the
   deterministic golden unless the live user explicitly selects that goal.

VERDICT: REVISE

### Codex response

Accepted all seven findings and revised the plan:

- made the Digest exception an explicit replay-only amendment implemented by
  a simulation-owned, non-persistent access policy; production attestation and
  dependency graphs remain unchanged;
- prohibited self-inclusion at checkpoint 15 and limited it to a 15-night
  longitudinal summary/baseline artifact;
- replaced callback ambiguity and exactly-once implications with committed
  outbox events, leases, CAS, at-least-once delivery and idempotent effects;
- selected one app factory/runtime owner and fenced duplicate workspace
  servers/workers;
- added a durable `JourneyRun` aggregate and made manual verification inspect,
  rather than advance, a persisted run;
- specified a non-executing dotenv parser and documented that the current
  `.env.deepseek.local` mode `664` must be user-tightened before live proof;
- kept the exact Care action deterministic, while live accepts one catalog
  action from a bounded allowed set unless the user explicitly selects the
  stable-wake-time goal.

## Round 2 — Codex review

1. **A single frozen clock corrupts security and longitudinal semantics.** The
   plan advances fifteen synthetic nights quickly, while current code also uses
   wall-clock timestamps for authorization, confirmations, leases, audits and
   retention. Advancing one clock could extend credentials or instantly expire
   them; using wall time for observations would make the journey nondeterministic.
   **Fix:** define separate ScenarioClock and ControlClock domains, prohibit live
   control-clock mutation, persist time basis and reject cross-basis comparisons.
2. **Provider request provenance has a concrete concurrency race.** The current
   structured provider stores `last_provider_request_id` on a shared model and
   the invoker reads it after generation. Concurrent invocations or retries can
   associate Agent A's output with Agent B's request ID. **Fix:** return output
   and immutable call metadata atomically, move retries above the transport, and
   persist every response attempt plus the exact accepted attempt.
3. **Oracle isolation is stated logically but not enforced architecturally.** A
   server-side developer “verification” surface and JourneyRun expected hash
   leave room for the backend to load `expected.json` or `actions.jsonl`, which
   could leak answers into tools or prompts. **Fix:** keep actions/oracles in the
   CLI verifier process, send only sanitized scenario world input, and let the
   server expose actual resources/traces rather than evaluate expected values.
4. **Publication can precede durable terminal state.** The current Runner
   reserves a journal entry, calls the publisher, and only later appends the
   terminal bundle. A crash can expose communication without a terminal result
   or make delivery indeterminate. **Fix:** atomically persist prepared result,
   exact targets, terminal bundle/job and publication outbox before any visible
   view/delivery; reconcile unknown delivery without rerunning LLM or commits.
5. **JourneyRun identifiers could accidentally become authorization.** The CLI
   contract accepts request/confirmation IDs but does not specify an unforgeable
   actor-bound resume credential. A caller could replay or transplant a target
   across actors, revisions or auth epochs. **Fix:** use server-issued opaque,
   expiring, single-use handles bound to the complete frozen target and current
   authorization/state; JourneyRun remains navigation only.
6. **Synthetic input capability provenance is missing.** The Runner consumes
   `CapabilityEligibilityReceipt`, but the plan does not say who may issue one
   for the canonical replay generator. Letting scenario data supply it would
   make readiness self-attested. **Fix:** add a server-owned replay registry that
   issues a synthetic/non-release receipt bound to generator/schema/cohort/
   environment/config hashes and is rejected by production/release paths.
7. **The non-persistent Digest lease contradicts the restart story.** The plan
   says the synthetic lease disappears on restart but the golden journey says
   Digest reading simply continues afterward. **Fix:** prove fail-closed read
   immediately after restart, then require an explicit CLI re-enable command and
   new receipt before reading the persisted Digest.

VERDICT: REVISE

### Codex response

Accepted all seven findings and revised the plan:

- split scenario/event time from control/security time and added negative tests
  preventing a 15-night fast-forward from changing token, lease or retention;
- replaced mutable last-request-ID provenance with an atomic typed generation
  result and per-attempt audit protocol;
- moved oracle evaluation fully into the CLI verifier and restricted server
  seeding to sanitized world inputs;
- required prepared result, terminal bundle/job and publication intent to commit
  durably before any role view or delivery, with an explicit crash matrix;
- made answer/confirmation resume use actor-bound opaque single-use handles;
- added a server-owned, replay-only capability registry receipt whose hashes and
  namespace are independently checked;
- made the post-restart Digest sequence explicit: read fails, the user re-enables
  a short lease, then source-revalidated retrieval succeeds.

## Round 3 — Codex review

1. **“File-backed SQLite” does not prove every authority is durable or atomic.**
   `ProductEpisodeRunner` still defaults to `InMemoryProductEpisodeResultStore`,
   several persistent implementations hydrate in-memory parents, and repository
   methods may commit independently. The plan's new cross-table transaction
   could therefore be impossible while tests still pass. **Fix:** require a
   RuntimeDependencyManifest, reject every required in-memory/different-storage
   dependency, and introduce one cursor-aware SQLite Unit of Work for atomic
   result/terminal/outbox writes.
2. **Multiple entrypoints can create duplicate Product Episodes.** The current
   `backend/main.py` chat handler calls `runner.run()` directly while the target
   bridge/worker also runs analysis. Without one durable operation identity, a
   revision or retry can invoke Agents twice and fork confirmations/views.
   **Fix:** make every bridge and product API enqueue the same typed persistent
   ProductInteractionOperation; only the Agent worker may call the Runner, and
   isolate the old radar chat as legacy.
3. **The LLM call itself lacks a crash/retry boundary.** Atomic return metadata
   fixes an in-process race, but a crash after DeepSeek responds and before the
   result is stored can either strand the operation or silently issue a second
   analysis. “Bounded retry” also conflicts with “unknown external result is not
   blindly retried.” **Fix:** reserve and persist invocation attempts, durably
   checkpoint validated output+metadata, distinguish safe failures from
   timeout-after-send unknowns, and accept at most one attempt by CAS.
4. **Single live A/B outputs cannot prove causal module effect.** Model
   stochasticity can produce different wording even if the changed Habit,
   Digest or readiness context was ignored. Saying the pair proves the model
   consumed context overclaims this phase's evidence. **Fix:** use deterministic
   single-variable counterfactuals as the functional-effect proof; use live
   pairs only to prove context/source refs were sent, a real model responded,
   and deterministic provenance/ceiling gates accepted or rejected it.
5. **Demo control and actor authority are not separated.** A CLI that holds all
   synthetic credentials could let an elder credential call seed/clock/trace or
   let a controller confirm a Care action, undermining the role demo and leaking
   developer traces if the server binds externally. **Fix:** use a distinct
   scoped controller principal, keep actor assertions mandatory for product
   actions, bind demo to loopback only, and scope/redact/audit traces.
6. **Recoverable reset can preserve live old credentials.** Archiving the old
   generation without first fencing it allows old handles, cursors, leases or
   signing material to be replayed against restored or newly created state.
   **Fix:** durably fence and revoke the generation/epochs before archive, rotate
   credentials for the new generation, and require restore-time rekeying rather
   than resurrecting old authorization.

VERDICT: REVISE

### Codex response

Accepted all six findings and revised the plan:

- added a persisted RuntimeDependencyManifest, one storage UUID requirement and
  a cursor-aware Unit of Work instead of assuming all “persistent” wrappers are
  transactionally durable;
- introduced deterministic ProductInteractionOperation identity and made the
  Agent worker the sole Runner caller for the new product surface;
- added a durable invocation-attempt journal with CAS acceptance and explicit
  handling of provider-outcome-unknown crashes;
- separated deterministic counterfactual functional proof from the narrower
  provenance/gate claim made by live DeepSeek pairs;
- split demo-controller scopes from actor authorization and required loopback,
  redacted, workspace-bound trace access;
- made reset generation-fenced, epoch-revoking and credential-rotating, including
  safe archive restoration.

## Round 4 — Codex review

1. **The mapped implementation cannot yet satisfy the promised atomic boundary.**
   The plan requires one caller-owned transaction, but the source map did not
   include the current Runner, Product persistence/governance or low-level Radar
   store methods that publish first and commit independently. **Fix:** explicitly
   scope those files, split Runner into `prepare()`/`commit_prepared(tx)`, and
   make terminal/publication operations accept the caller's cursor.
2. **Automatic analysis identity still contains caller-controlled material.**
   The planned ProductInteractionOperation included caller idempotency, while
   the current bridge/repository also key operations by caller/service context.
   Two routes can therefore analyze the same revision twice. **Fix:** let only
   the fast-path outbox mint a server-owned AnalysisRequestKey that excludes
   client identity/idempotency; all other entrypoints reference it.
3. **Three role runs can be partially committed.** The current bridge runs and
   stores role results sequentially, then appends the AnalysisRevision/views.
   A crash can leave one or two orphan terminal Product results and retry IDs
   derived from changed history. **Fix:** checkpoint invisible prepared members
   under one operation-derived AnalysisAttempt and atomically commit all role
   views/targets/terminal group with a single induction job.
4. **Hot reset does not replace the open runtime.** Fencing generations and
   rotating credentials is insufficient if routers still capture the old
   namespace/auth stack or SQLite connection. **Fix:** add a RuntimeSlot with an
   exclusive drain/stop/join/close/archive/rebuild/swap protocol; failure stays
   fenced rather than reopening half-reset state.
5. **Counterfactual arms can contaminate one another.** Workspace generation
   alone does not isolate Profile, Care, Memory, Digest and analysis state across
   deterministic/live runs or A/B arms, so “only one variable changed” is not
   auditable. **Fix:** fork separate run/arm namespaces from one hashed committed
   snapshot, issue a ForkReceipt, and bind every artifact to run/arm/model mode.

VERDICT: REVISE

### Codex response

Accepted all five findings and revised the plan:

- put `runner.py`, Product persistence/governance and the low-level persistence
  store explicitly in scope, with side-effect-controlled prepare and a
  caller-owned transactional commit;
- replaced caller-derived automatic analysis IDs with one fast-path-owned
  AnalysisRequestKey and separated authenticated user-interaction identity;
- grouped the three role members in an invisible AnalysisAttempt that commits
  one AnalysisRevision, three views and one longitudinal induction job;
- specified RuntimeSlot replacement all the way through request draining,
  worker join, DB close, archive, new-runtime preflight and atomic swap;
- added RunScope/RunSnapshotManifest/ForkReceipt isolation so A/B and
  deterministic/live state cannot leak across arms.

## Round 5 — Codex review

No blocking findings remain.

Final checks against the current repository and revised plan:

1. The implementation scope now reaches every layer needed to replace the
   observed publish-before-store, internal-commit and sequential-role behavior;
   the atomicity claim is paired with a concrete prepare/Unit-of-Work design and
   hard-crash acceptance tests.
2. Canonical revision analysis, authenticated user interactions, CLI command
   idempotency and role projections now have distinct, convergent identities;
   retries and alternate HTTP producers cannot legitimately create a competing
   analysis for the same semantic key.
3. Deterministic scripts/oracles, replay-only Memory access, DeepSeek invocation
   evidence, two time domains, role/controller authorization and A/B state are
   all separated by explicit runtime and negative-test boundaries.
4. The seven requested suites and the 15-night live journey remain inside the
   agreed canonical-data phase; raw radar, frontend, production Memory release,
   statistical evaluation and broader stability work are still excluded.
5. Remaining items under Risks are implementation risks with fixed fail-closed
   responses, not unresolved product choices or contradictions.

VERDICT: APPROVED

## Act 3 — Build

### Round 1 — Codex build

Implemented the user-requested simulated-data-generation slice of the locked
plan. Added strict replay-only environment, identity, night-recipe, overlay and
seed-request contracts; a deterministic provider-neutral `SleepObservation`
generator; packaged normal 15-night and isolated overlay fixtures; and focused
contract/determinism/oracle-isolation tests. Observation identities bind only
generation inputs, so explanatory fixture prose cannot change canonical output.

The generator emits external-world facts only. It does not derive fast-path
quality/risk verdicts, readiness, FactSnapshots, Agent work products, Safety
decisions or Memory artifacts. Runtime fixture loading accepts `scenario.json`
only and rejects `actions.jsonl`/`expected.json`.

Deviation: this build intentionally covers the explicit user scope, “模拟数据生成”,
not the remaining HTTP runtime, workers, Product Agent, Memory, CLI and live
DeepSeek closure described by the broader plan.

### Codex verification

- `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_simulation_contracts.py`
  — 8 passed.
- `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_sleep_domain_contracts.py tests/test_night_episode_lifecycle.py tests/test_sleep_domain_fast_path.py`
  — 42 passed.
- `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q`
  — 774 passed, 3 unrelated pre-existing failures in Memory/Habit acceptance
  evidence (`test_healthclaw_memory_governance.py` and two
  `test_product_agent_acceptance.py` cases); no failing test imports or exercises
  this build's files.
- Python compilation passed for the new package and focused test; no trailing
  whitespace was found.

Diff review: changes are limited to `sleepagent/simulation/`, its focused test,
the simulation fixture package-data entry in `pyproject.toml`, and this build
log. Existing dirty-worktree changes were preserved. One bounded self-review
fix pass was used to decouple IDs from fixture prose and add isolated bed-exit
coverage. No commit, push, remote mutation or live provider call was performed.

## Act 3 — Simulation acceptance repair

The preceding Round 1 completion claim was challenged by the user and is
superseded for acceptance purposes by this repair round.

### Round 2 — Codex build

Closed all six simulation-acceptance gaps identified by the user:

1. locked normal replay sampling to the production fast-path cadence of three
   minutes and regenerated the 15-night fixture (7,473 observations);
2. added strict, discriminated human-action contracts and a complete typed
   seven-module expected oracle in the verifier-only source tree;
3. added isolated elevated-vitals, worsening-vital-trend, care-escalation,
   urgent-human-text, habit-family-report and memory-forget scenarios, each
   with an independent replay namespace and explicit cohort hash;
4. made correction IDs and source revisions explicit, validated parent chains,
   enforced generated observation-ID uniqueness and emitted exact parent/child
   correction lineage in the manifest;
5. added a real SQLite `SleepDomainRepository` → `NightEpisodeService` →
   `DeterministicFastPathService` integration test using generated canonical
   observations and the default quality policy;
6. removed actions/oracles from `sleepagent` package data, put them under the
   separately excluded `simulation_verifier` tree, prohibited runtime imports,
   and inspected the built wheel to prove only eight `scenario.json` resources
   ship with runtime code.

The action timelines now cover general/personal questions, elder Habit answer,
skip and unknown, family observer provenance, Habit/Care confirm and decline,
feedback, reanalysis, restart, synthetic Memory re-enable/read, forget,
withdraw, doctor material and urgent text. Scenario fixtures contain external
world facts only; none contains precomputed risk, readiness, Agent, Safety,
Memory or verifier verdicts.

### Fix pass 1

The first integration run found that replaying the pre-bed connectivity event
after opening an episode would move `updated_at` before `created_at`. The test
was corrected to consume it in actual event order as a pending association.
The low-coverage fixture also produced a three-channel bin union exactly at the
75% boundary when each channel retained 35%; retention was reduced to 20% so
the integrated fast path deterministically reports both below-minimum coverage
and explicit missingness.

### Fix pass 2

Diff review found the timelines only proved a generic decline and did not
explicitly prove post-restart Memory retrieval. Added a Care-specific decline,
typed `memory_read` with mandatory canonical revalidation, exact semantic
timeline assertions, a literal three-minute cadence, scenario-clock/correction
source validators, non-empty overlay targets and manifest type-count checks.

### Codex verification

- `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_simulation_contracts.py tests/test_backend_runtime_vertical_slice.py`
  — 16 passed. This is the authoritative simulation proof; the integrated
  cases produce `SUFFICIENT` for the normal golden night and
  `DATA_INSUFFICIENT` for the low-coverage night under the real default policy.
- The plan's adjacent Product/cold-start/Habit/Memory/lifecycle/fast-path/
  bridge/API regression set — 176 passed, with the existing governed-Memory
  old-handle invalidation test still failing.
- `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q` — 782 passed, 3 failed. The
  failures are the same governed-Memory handle case and two historical
  10-concept-vs-22-concept acceptance-material cases. Because no clean baseline
  exists, they are reported as unresolved workspace failures rather than
  claimed as proven pre-existing; none imports or executes the simulation
  generator or verifier.
- `python -m build --wheel --no-isolation` succeeded. Direct wheel inspection
  found zero `simulation_verifier`, `actions.jsonl` or `expected.json` entries
  and exactly eight packaged runtime `scenario.json` resources.
- Python compilation, `git diff --check`, trailing-whitespace scan and static
  runtime-import scan passed. Ruff and mypy were unavailable in the current
  environment.

Diff review: task changes are limited to `sleepagent/simulation/`, the excluded
`simulation_verifier/` tree, two focused test files, the simulation package-data
entry in `pyproject.toml`, and this log. Existing dirty-worktree content was not
reverted or cleaned. No commit, push, remote mutation or live provider call was
performed. Both allowed fix passes were used; no known simulation-acceptance
failure remains.
