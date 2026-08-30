## Act 3 — Build

### Round 1 — Codex build

Implemented the frozen P1.5 CareStrategy production-routing work order from the
referenced attachment. Morning Review now permits, but does not require, Care;
accepted Evidence and typed risk receipts feed the existing coordination policy;
Care runs only when that policy returns candidate intents without an urgent
preemption. A revision-bound three-night vital trend supplies the missing typed
production signal, and accepted Care candidates remain in publication.

The initial real PostgreSQL worsening run exposed one contract-shape defect:
`trend_signals` had been placed inside the deterministic `data` payload and was
therefore ignored by the risk tool. The smallest fix moved `trend_signals` and
`trend_observation` to the existing top-level structured tool arguments. A fresh
real PostgreSQL run then produced six CareStrategy work products across the two
triggered nights, and all six retained Care candidate references appeared in
publication.

No Habit, Memory, frontend, radar, migration, legacy, scenario-name, or test-only
routing changes were made.

### Codex verification

- Focused Product/Care tests: 88 passed.
- Core runtime/governance/PostgreSQL-adjacent regression set: 140 passed.
- PostgreSQL Product repository integration: 6 passed before the final
  tool-argument nesting correction; the corrected nesting is covered directly by
  the focused unit regression.
- Full non-PostgreSQL/non-e2e suite excluding nine known environment or dependency
  compatibility nodes: 522 passed, 21 deselected.
- The nine excluded nodes consist of eight tests that require loopback socket
  creation denied by the sandbox and one existing FastAPI route-enumeration test
  incompatible with the installed lazy `_IncludedRouter` representation.
- `git diff --check`: passed.
- Python compilation: passed.
- Mypy: the changed scope is clean; three pre-existing errors remain only in the
  explicitly out-of-scope Habit/Memory-related files `runtime/memory.py` and
  `runtime/governance.py`.
- Real PostgreSQL `normal-one-night`: passed with Evidence present and zero Care.
- Real PostgreSQL `worsening-vital-trend`: passed its public verifier; four Product
  operations succeeded, Care ran on the two policy-triggered nights, and every
  accepted Care candidate was retained in publication.
- Real PostgreSQL `urgent-zero-model`: passed against a fresh isolated PostgreSQL
  16 database plus canonical BFF API, Demo API, and durable worker. The verifier
  returned `verified=true` with the expected terminal error code
  `unexpected_urgent_route`. Database cross-checks showed zero Product attempts,
  zero model/provider invocations, zero analysis revisions/Care products, one
  succeeded `fast_path` operation, and four persisted fast-path signal receipts
  and projections. The replay journey terminated at the authoritative boundary
  exactly as designed. All three temporary service processes and the database
  were stopped after verification.

Diff review found no scenario-name branching, unconditional four-agent pipeline,
Habit/Memory scope creep, migration change, or remote write. One manifest snapshot
hash was updated because the Morning Review registry contract intentionally
changed. Fix rounds used: 1 of 2.

## Act 4 — P2 Build

### Codex verification

Implemented the frozen P2 L2 wiring without replacing the retained Habit or
Governed Memory reducers. One additive migration (`009`) supplies separate
append-only Habit and Memory revisions, governed Memory read receipts, question
selection persistence, and a narrowly checked family-to-elder Habit confirmation
handoff. The canonical Product API owns proposal/confirmation/read commands; the
durable Product worker pins effective Habit state and purpose-scoped Memory
receipts into each role Episode before Evidence/Care consumption.

Real PostgreSQL verification found and minimally corrected three adapter defects:
psycopg JSONB parameters were bound as `bytea`, family-originated elder handles
were rejected by the original actor-local RLS policy, and strict tuple DTOs did
not normalize decoded JSON arrays. No product semantics or safety boundary was
relaxed.

- Existing completed suites retained: 464 unit tests and 65 non-PostgreSQL
  integration tests.
- Final targeted regression after the real-backend fixes: 80 passed.
- Focused real PostgreSQL Habit/Memory + Product pinning scenarios: 2 passed.
- Canonical signed HTTP Habit/Memory proposal, exact confirmation, and read:
  passed against schema 009.
- Canonical baseline versus personalized reanalysis pinned L2 versions 0/0 then
  1/1; `confirmed_habit` and governed `morning_voice` consumption changed from
  absent to present, with Care receipts returning the allowed item and Evidence
  receipts remaining empty.
- Canonical `worsening-vital-trend` verifier: `verified=true`.
- Canonical `urgent-zero-model` verifier: `verified=true`, terminal
  `unexpected_urgent_route`; Product/model/Care counts remained zero and the
  fast path persisted one succeeded operation plus four receipts/projections.
- Migration ledger/manifest check: schema version 009.
- `001`–`008` remained immutable; no new production Python file or package was
  introduced.
- All isolated API, Demo API, worker, and PostgreSQL processes were stopped.
- GitHub remote write: none.

## Act 3 — Build (P3)

### Round 1 — Codex build

Implemented the frozen P3 terminal Product demo work order from the referenced
attachment. The existing single OpenAI-compatible Product provider now reuses
the existing local DeepSeek environment names when the newer Product names are
absent, records provider prompt-token evidence, and remains selectable through
the existing Worker `live`/`deterministic` model mode. The unified CLI adds five
Product stories, a product-first terminal renderer, exact Habit/Memory/HITL
interactions, public reanalysis, and a replay-only read projection over existing
durable Product attempts, Agent records, SkillLocks, Habit revisions, governed
Memory revisions, and Memory ReadReceipts.

The first focused pass found a demo test double that lacked the new read-only
trace response, an urgent-story assumption that every root has a Product
analysis, and a missing transactional marker on migration `010`. The smallest
fixes completed the test double, made urgent selection explicitly empty, and
restored the manifest-pinned transactional migration contract.

### Round 2 — Codex build

The adversarial story review added fail-closed product assertions: Cold Start
must move Habit profile v0 to v1; Habit baseline must add a real
`confirmed_habit` Evidence claim; worsening must durably invoke Evidence and
Care; longitudinal personalization must preserve Episode A at Habit/Memory v1,
Episode B at v2, retain both append-only revisions, expose the family dispute,
and persist Memory ReadReceipts; urgent safety must retain zero Product attempts,
zero Agent/Care invocations, and a succeeded deterministic fast path. The
terminal view now displays the longitudinal v1/v2 pins and dispute explicitly.

### Codex verification

- Focused changed-scope regression: 88 passed, 3 deselected before the final
  story assertions; final CLI/API/provider/data focused set: 60 passed.
- Real loopback OpenAI-compatible provider tests: 8 passed.
- Full non-PostgreSQL/non-ASGI-lifespan/non-e2e regression, rerun with local
  loopback permission: 542 passed, 24 deselected.
- Packaged narrow replay overlay: verified at 499 canonical observations and one
  night; registry/scenario/manifest hashes match.
- Migration discovery: 10 contiguous checksum-pinned migrations; migration
  `010` is transactional and the `001` immutable baseline remains unchanged.
- Real configured DeepSeek probe: HTTP 200, provider `openai-compatible`, model
  `deepseek-v4-flash`, strict JSON result accepted, provider request ID present,
  118 input tokens recorded. No credential was printed or persisted.
- Python compilation and `git diff --check`: passed.
- Mypy could not run because it is not installed in the available host Python.
- Required real PostgreSQL/API/Worker demo proof is environment-blocked: Docker
  is installed, but the current account cannot access `/var/run/docker.sock`;
  passwordless sudo is unavailable, no PostgreSQL server is listening, and the
  host Python lacks `psycopg`. The targeted PostgreSQL test therefore skipped for
  missing `psycopg`. No in-memory substitute was used to claim process proof.
- Diff review found no scenario name in Agent prompts (enforced in the live
  Worker integration responder), no hard-coded Product conclusion, no mock
  Product result, no new package, no generic verifier framework, no second LLM
  router, no secret/config file change, and no GitHub remote write.

Deviation: the code and live provider path are verified, but the five stories
could not be executed against real PostgreSQL/API/Worker processes in this
environment. Fix rounds used: 2 of 2. No commit was created.

### Runtime continuation (supersedes the earlier environment-blocked note)

A portable PostgreSQL 16 server and isolated host-side API/Demo API/durable
Worker processes were subsequently brought up without Docker. Migrations
`001–010` and the bounded test authority were applied. The configured live
DeepSeek provider returned real OpenAI-compatible responses for the current
structured runtime.

- Demo 1 (`cold-start`) completed with 21 durable live-provider Agent
  invocations and exact Habit v0 → v1 HITL.
- Demo 2 (`habit-baseline`) completed with 37 durable live-provider Agent
  invocations; the initial Evidence had no confirmed Habit, while reanalysis
  consumed the exact `约 02:00` Habit baseline without promoting it to clinical
  truth.
- Demo 3 (`worsening-care`) completed four durable Product attempts with 64
  live-provider Agent invocations, including real EvidenceReasoning and
  CareStrategy calls. The durable advance released 1,490 staged facts exactly
  once; the CLI resume guard prevented a second clock advance.
- The full host suite passed after loopback permission was granted: 557 passed,
  21 skipped. The changed-scope focused suite passed: 141 passed.

Demo 4 and Demo 5 remain to be run after the environment explicitly approves
reapplying the bounded local test bootstrap to the clean isolated database.
No commit or GitHub remote write was performed.

### Runtime continuation 2

The bounded bootstrap was explicitly approved and completed only against local
`sleepagent_replay_test` on `127.0.0.1:15433`. Schema `010`, three test
principals, nine seed allowlist rows, the Demo 4/5 fixtures, and technical-trace
function authority were verified before resuming from Demo 4; Demos 1–3 were not
rerun.

The first real Demo 4 attempt reached the configured live DeepSeek provider and
exposed two narrow issues. Evidence instructions named a Habit entity `fact_id`
instead of the ToolReceipt authority `fact_ref`, causing fail-closed acceptance;
the repair prompt now names exact Habit `fact_ref` and Memory
`retrieval_handle` references. The story also requested the family copy of a
question after the elder answer had correctly activated the seven-day cooldown.
It now obtains both actor-bound question receipts first, then preserves the
required elder answer → Episode A → family answer → exact elder confirmation
order without changing cooldown or HITL governance. Corresponding focused
regression is green at 68 passed, and the urgent verifier now directly rejects
any top-level durable provider invocation in addition to requiring zero Product
attempts, zero Care, and a succeeded fast path.

The failed Demo 4 generation was safely sealed with the public replay reset.
That reset is intentionally irreversible: it raised authority epochs and the
seed reservation function then returned `generation_fenced`, so the sealed
namespace cannot be reseeded. Recreating the one isolated test database is now
the only clean rerun path, but database deletion was not included in the
bootstrap authorization and was refused by the execution safety boundary. Demo
4, Demo 5, final reconciliation, and the conditional local completion commit
therefore remain pending explicit authorization to recreate only
`sleepagent_replay_test`. No remote write was performed.

### Runtime continuation 3 — final P3 reconciliation

The user explicitly authorized permanent drop/recreate of only the isolated
`sleepagent_replay_test` database on `127.0.0.1:15433`, with no backup and no
effect on any other database or service. The exact PostgreSQL target and data
directory were verified before recreation. Migrations `001–010` were applied to
the fresh database, and the bounded `sleepagent.persistence.test_bootstrap`
restored the three test principals, nine replay seed reservations, actor-key
authority, Demo 4/5 fixtures, grants, and the technical-trace function. Demos
1–3 were not rerun.

The earlier reset result is retained as a test-harness lifecycle limitation:
reset advances replay generation from 1 to 2 and the authorization, privacy,
and retrieval authority epochs from 1/1/1 to 2/2/2, while the fresh seed
contract accepts only authority 1/1/1. A reset namespace therefore cannot be
seeded again. P3 intentionally does not change either the authority epoch model
or the seed contract to support reset-after-seed reuse.

The resumed live Demo 4 exposed two further production-path defects, each fixed
at the narrowest owning boundary. A family-originated proposal stored the
family policy hash and epochs on an elder-confirmation handle, so exact elder
confirmation failed when family and elder authorities differed. Pending L2
handles now resolve and persist the designated elder confirmer's authoritative
epochs and policy hash while retaining the family source actor/role in the
proposal payload. Separately, migration `010` projected Habit evidence
`actor_role`, but the durable evidence field is `role`; the trace projection and
manifest checksum now use the correct field. Focused real PostgreSQL regression
covered distinct elder/family policy hashes, and the fresh schema `010` trace
was exercised by the completed demo.

Live Demo 4 (`longitudinal-personalization`) passed against the real PostgreSQL
database, canonical API and Demo API, durable Worker, and configured DeepSeek
provider. Root operation `019fffe2-08c4-7e85-aaa6-27d9a1f406c3` completed three
Product attempts and two succeeded fast paths. The durable trace recorded 44
OpenAI-compatible `deepseek-v4-flash` Agent invocations with provider request
IDs, prompt-token counts, latency, and SkillLocks. Habit revisions remained
append-only at v1 elder (`通常不午睡`) and v2 family (`多数天午睡`), both with
confirmation references. Governed Memory advanced from v1 to v2 with exact
confirmation references. Episode A pinned Habit/Memory 1/1; Episode B pinned
2/2, preserved the family dispute, changed the personalized evidence/context,
and persisted 18 Memory ReadReceipts, including governed item reads in both
episodes.

Live-configured Demo 5 (`urgent-zero-model`) passed its authoritative verifier
at root operation `019fffea-ecc3-7ac7-be57-7c41cf587c7b`. The deterministic
urgent boundary persisted one succeeded fast-path operation and then terminated
the replay journey with the expected `unexpected_urgent_route`. The durable
trace proved Product attempts = 0, durable/provider LLM invocations = 0,
CareStrategy invocations = 0, Memory ReadReceipts = 0, and fast-path succeeded =
1. No Product Runtime or role projection was invoked.

Final reconciliation retained the previously completed full-suite result of 557
passed and 21 skipped and the focused 141 passed rather than rerunning them for
form. The final changed-scope unit/API/foundation regression passed 192 tests;
the focused live PostgreSQL cross-authority regression passed; the real Demo 4
and Demo 5 verifiers passed; migration `010` was applied from a fresh database;
and `git diff --check` passed. Diff review found no scenario-name routing in the
runtime, no committed credential/private key, no second provider/router, no
authority/seed lifecycle scope expansion, and no GitHub remote write.

## Act 3 — Build

### Round 1 — Codex build: deterministic Communication assembly convergence

The deterministic SleepCare communication path now emits the same private
`SleepCareContentPlan` used by the live path and delegates final text, bindings,
numeric preservation, audience presentation, and source references to the
single assembler in `sleepagent.runtime.agents`. The canonical deterministic
runtime enables the same per-invocation assembly adapter. Direct deterministic
`SleepCareModelOutput` generation also uses that shared catalog/plan/assembler
core, while urgent preflight remains before every model and Care invocation.

The four canonical SleepCare communication skills were versioned to `3.0.0` and
now instruct the model to select only existing `source_type`/`source_ref` pairs
and never generate final prose, bindings, numbers, audience, references, or
template text. Their package, SkillLock, and compiled prompt hashes therefore
change through the existing registry/compiler mechanisms while their external
output schema remains `CommunicationDraft` and invocation records remain
`SleepCareModelOutput.v1`.

### Codex verification

The final scoped unit/provider/runner/governance/contracts/cold-start/worker
regression passed 249 tests. The loopback HTTP provider and live-configured
urgent zero-provider regression passed 9 tests in an isolated local socket
namespace. Python 3.11 imports and `git diff --check` passed. No real Demo,
PostgreSQL operation, commit, or remote write was performed.

## Act 3 — P4 real-radar completion and repository consolidation

P4 now provides the production YunYun/Perceptor cloud boundary: authenticated
Push, bounded read-only Pull, durable PostgreSQL canonicalization,
Push/Pull reconciliation, NightEpisode quality semantics, and the unchanged
downstream 1+2+1 Agent Runtime. The final engineering verdict remains
`P4_COMPLETION = COMPLETE_WITH_LIMITATIONS` and `RELEASE_READY = NO`.

The root execution plan, 17 construction reports, 13 one-shot stage scripts,
10 script-only tests, and the full P4 construction diary were copied to a
repository-external owner-only archive with a checksum manifest before removal.
Stable source retains the Perceptor production package, migrations 011–013,
sanitized contract fixtures, production regressions, and extracted frozen
evidence integrity/restore-semantic checks. Current architecture, operations,
final evidence, and cleanup verification are recorded under `docs/`.

No vendor, model-provider, Git remote, commit, or tag operation was performed
by this consolidation checkpoint.

## Act 5 — Build: Product report CLI and shared-analysis authority

### Round 1 — Codex build

Implemented the frozen Product report plan through the existing ASGI/Worker
topology. Exact authenticated wake-date report requests now converge on one
canonical `SharedNightAnalysis.v1`, one deterministic three-role projection
set, and an independently reusable elder narrative. Automatic fast-path and
public reanalysis work route through the same authoritative urgent/UNUSABLE
gate and shared semantic identity; the legacy automatic Product operation is
retained only as a non-claimable compatibility bridge.

The shared identity binds the exact episode revision, canonical observations,
quality/risk policy, selected stable Habit/Memory meaning, governance epochs,
and semantic runtime manifest. Volatile receipt, invocation, timestamp, and
unrelated global revision values are excluded. Provider work is fenced before
every HTTP attempt and again at final commit. Safe call/token aggregates are
retained for committed, failed, prepared, and journaled-success/staging-failure
attempts, while full provider request IDs remain confined to the governed
invocation journal.

The Product API now exposes report run/show/list contracts without internal
identifiers. POST retains replay-consuming command authentication; the two
idempotent report GET routes use the tightly scoped stateless verifier and
SELECT-only authority/report/context reads. The new `sleepagent-report` command
is an authenticated HTTP-only client with `run`, `show`, and `list`, polling,
bounded timeouts, strict response allowlists, safe JSON/text rendering, and
environment-only identity/credential configuration. Existing `/today`, sole
process roots, provider transports, schema, migrations, dependencies, and
runtime roster remain unchanged.

### Round 2 — adversarial fixes

The bounded audit found and closed: stable narrative retry generation after
known-not-sent/dead-letter outcomes; ambiguous-send non-replay; Memory revision
references accidentally becoming canonical Evidence; narrative projection
content-vs-identity binding drift; failed-provider aggregate persistence;
query-invisible prepared and orphaned journal usage accounting; a nonempty
Memory read-path string/enum mismatch; legacy retrieval-handle compatibility;
and automatic compatibility-wrapper terminal behavior. Direct regressions now
cover those boundaries, concurrent canonical reuse, projection refresh,
provider request-ID redaction, urgent/UNUSABLE zero-provider handling, and
stable provider input/request hashes.

### Codex verification

- Runtime/worker focused regression: 109 passed.
- API/report focused regression: 84 passed.
- CLI/client focused regression: 35 passed, 1 opt-in live E2E skipped.
- Independent adversarial focused regression: 250 passed, 1 opt-in E2E skipped.
- Full unit suite: 917 passed.
- Complete repository suite with localhost loopback permission: 1,000 passed,
  27 skipped.
- The nine real loopback OpenAI-compatible HTTP tests passed separately after
  the default sandbox denied local socket creation.
- Python 3.11 and host-Python compilation, CLI help smoke proof, AST duplicate-
  key scan, and `git diff --check` passed.

The 27 skips are environment-only: the host Python lacks `psycopg` and no test
PostgreSQL/compose profile is configured; the Product report CLI process E2E is
opt-in through `SLEEPAGENT_E2E_REPORT_ENABLED=1`. No database, provider, radar
service, Git remote, migration, commit, or tag was changed or contacted. No
commit was created.

## Act 6 — Build: Product report publication and final-fence repair

### Round 1 — Codex build

Repaired the three post-implementation audit findings without changing the
Product report topology, schema, public contracts, provider transport, Agent
roster, or legacy per-role execution path. Analysis publication now serializes
per exact NightEpisode revision and a valid journaled v3 result may transfer to
a later business attempt and rebase only its revision number/parent envelope
under the final publication fence. Provider-derived content and stable artifact
identifiers remain unchanged.

Urgent and UNUSABLE request closure now locks and re-reads governance epochs,
the finalized current Episode revision, and the exact current quality/risk
authority. A changed gate is not terminalized: the durable request refreshes
its exact pins, returns to `retry`, and re-resolves without Product Agent or
provider work from the stale decision. Shared-analysis and elder-narrative
final commits now acquire the existing L2 writer advisory keys in fixed
`habit`, then `memory` order before re-reading context and hold them through
the atomic commit.

### Codex verification

- Focused repair and Product/runtime/API/privacy regression: 178 passed.
- Repair module regression after concurrency additions: 32 passed.
- Full unit-marked suite with localhost loopback permission: 1,015 passed,
  27 deselected.
- Complete repository suite with localhost loopback permission: 1,015 passed,
  27 skipped.
- PostgreSQL/process-gated collection: 27 skipped; `psycopg`, test DSNs, the
  compose test-postgres profile, and the opt-in report CLI E2E configuration
  are unavailable.
- Python compilation and `git diff --check` passed.

No external provider, radar/device service, production database, migration,
dependency, Git remote, commit, or tag was contacted or changed. No commit was
created.

## Act 7 — Build: Product Report UX Closure

### Round 1 — Codex build

Implemented the narrow Elder presentation closure without changing Product
Runtime topology or shared-analysis inputs. `ProductRevisionFacts` now retains
the authoritative Episode bed/wake span for presentation and derives a separate
typed Elder view that preserves canonical timestamps, localizes with
`ZoneInfo`, distinguishes Episode span, vendor stage envelope, observed stage
coverage, and classified stage totals, clips only the presentation view to
Episode bounds, and flags overlap/invalid/boundary conditions instead of
silently choosing a classification policy. The existing
`deterministic_night_summary()` and provider-facing shared inputs remain
unchanged.

The Elder projection now derives a deliberately small set of source-bound
message atoms for the stage summary, bed-exit observation, one quality caveat,
and one AI-assisted/non-diagnostic boundary. Each atom owns its numeric values,
units, Runtime-selected display values, subject-local times, quality/risk/safety
classification, source references, priority, mandatory state, and finite safe
zh-CN renderings. The independent Elder model call may select and order only
those renderings; its schema has no prose or numeric field. Runtime validates
mandatory selection, source/risk/safety authority, exact rendering membership,
numeric bindings, locale compatibility, and the final Communication binding.
Invalid/provider-failed output publishes the same deterministic Chinese atom
fallback while Shared Analysis stays ready.

The public fallback now carries deterministic text. Elder CLI pretty mode is
narrative-first, omits the duplicate full projection/context/legal boilerplate,
and renders the PARTIAL caveat and compact boundary once. Elder `run` continues
GET polling after shared readiness until narrative `ready`, `fallback`, or
`failed`; `--no-wait`, Family, Doctor, `show`, `list`, `--trace`, and `--json`
retain their prior authority and side-effect behavior.

### Round 2 — adversarial fix pass

The diff audit found that an initial atom transport through a new SleepCare
Context key would have broadened the concrete-Agent manifest. The atoms were
moved onto the existing Runtime invocation binding, omitted when empty for
legacy serialization/hash compatibility, and included only in the Elder target
hash. The provider receives the bounded atom manifest without the dense
free-form Evidence statements. Additional checks now bind atom IDs to their
complete authority, verify Shared Analysis/risk/safety references, reject
inconsistent localized spans and Runtime number displays, validate persisted
ready narratives again at the API boundary, and always use projection text for
fallback publication.

No shared-analysis manifest bump is required: the repair changes only the
projection/narrative presentation manifests. A direct baseline comparison at
commit `51947e6` proved the concrete-Agent manifest hash, desired-analysis hash,
and resulting deterministic Shared Analysis hash are byte-identical before and
after this closure; the role-projection manifest hash changes as intended.

### Codex verification

- Focused Product data/report/runner/worker/API/CLI regression: 372 passed,
  1 opt-in process E2E skipped.
- Full unit suite: 948 passed.
- Complete repository suite with localhost loopback permission: 1,031 passed,
  35 skipped.
- Focused PostgreSQL Product collection: 1 passed, 18 skipped because the host
  environment lacks `psycopg`; no test database is configured.
- Python 3.11 compilation, host-Python compilation, real-shaped Elder rendering,
  baseline/current shared-identity parity, and `git diff --check` passed.
- Static `mypy` verification was unavailable because `mypy` is not installed.

The 35 complete-suite skips are environment-only PostgreSQL/process/opt-in E2E
gates. No DeepSeek, YunYun, radar/device, production database, migration,
dependency, Git remote, commit, or tag was contacted or changed. No commit was
created. One bounded adversarial fix pass was used.

## Act 3 — Build: G1 M0 remediation safeguards

### Round 1 — Codex build

Recorded the exact dirty-worktree baseline before production edits, then added
seven remediation ADRs, a classified report-consumer inventory, typed
default-preserving switches in the existing settings authority, a reproducible
characterization fixture/suite, an AST import no-growth guard, and an explicit
OpenAPI drift classification. No observation, report, scheduling, delivery,
database, or external-effect migration was activated.

The characterization evidence freezes the current mixed movement aggregation,
Push/Pull/Replay validator asymmetry, UTC-as-local report defect, and English
claim leakage under Chinese headings. The architecture fixture freezes six
existing SCCs and exact forbidden edges while allowing later debt reduction.

### Round 2 — Codex fix pass

The dependency snapshot review found that `from package import submodule` in a
package `__init__` was initially being counted as a false package self-import.
The resolver now records a known imported submodule when it exists, restoring
the G0 six-SCC baseline while retaining exact real self-imports. Spec review
also added the separate Night Finalization ADR so the attached G1 work order
and master M0 ADR set are both covered. Final evidence review corrected the
OpenAPI schema-group count to 12 personalization plus 11 report schemas.

### Codex verification

- Focused G1 settings, characterization, and architecture: 18 passed.
- Full architecture suite: 5 passed, including synthetic new-debt rejection.
- Unit-marked suite with localhost permission: 1,047 passed, 35 deselected.
- Full non-PostgreSQL/non-E2E/non-ASGI-lifespan suite with localhost permission:
  1,044 passed, 38 deselected.
- Python 3.13 and supported Python 3.11.15 compilation passed; Python 3.11.15
  import smoke passed; `git diff --check` passed.
- OpenAPI check still fails for both snapshots and is classified
  `STALE_SNAPSHOT`; no snapshot was overwritten.
- PostgreSQL verification and mypy are `ENV_BLOCKED`.

Diff review found no G1 edits to the 18 pre-existing dirty report/runtime/test
files. `sleepagent/config.py` is the only G1 production edit and future switch
states have no production consumer. No external provider or real effect was
invoked. The build used one bounded fix pass. A safe isolated commit is blocked
because the new characterization evidence depends on the authoritative
uncommitted report baseline; no staging, commit, or push occurred.

### Supplemental G1 verification

Reverified the completed G1 safeguards on the supported Python 3.11.15
interpreter using an isolated environment installed from the repository's
hash-locked development requirements. Compile/import checks, 46 focused tests,
5 architecture tests, 1,047 unit-marked tests, and 1,044 non-PostgreSQL tests
passed. The two broad selections initially hit eight sandbox-only localhost
bind denials and passed with approved `127.0.0.1` permission.

The now-available declared mypy 2.3.0 reports a known baseline of 86 errors in
20 files; an archived clean HEAD independently reports 63 errors in 20 files,
and no error is in the sole G1 production file, `sleepagent/config.py`.
PostgreSQL remains environment-blocked because there is no database URL or
native server/client and the installed Docker socket is inaccessible. OpenAPI
remains the classified stale-snapshot failure and both snapshot files were
left at their original hashes. The frozen 18-file user patch still hashes to
`f306cc80aa4e25df543501ccbdd23e9917c57ecf9dfc9a152abb436381f78806`.
No staging, commit, push, provider call, or real effect occurred.

## Act 4 — Build: G1.5 pre-G2 baseline consolidation

### Round 1 — Codex build

Reconfirmed the frozen pre-G1 report/runtime/test patch at SHA-256
`f306cc80aa4e25df543501ccbdd23e9917c57ecf9dfc9a152abb436381f78806`,
then re-ran the structural OpenAPI comparison. It reproduced only the
classified 10/23 backend and 1/1 Demo additions, with no changed or removed
existing path/schema. The existing generator reconciled both snapshots and the
canonical check now passes.

Established Python 3.11.15 as authoritative using an isolated environment
installed from the hash-locked development requirements. Compile/import,
focused safeguards, architecture, unit-marked, and non-PostgreSQL selections
pass. Declared mypy 2.3.0 ran and is recorded as an existing failure baseline,
not repaired broadly.

Validated the repository's isolated PostgreSQL 16 Compose configuration and
all 13 immutable migration hashes. Actual PostgreSQL execution remains
environment-blocked because the Docker socket is inaccessible and no native
server/client exists. This yields separate M1/M2 readiness from M3 readiness.

The exact 40-path checkpoint candidate is classified in
`docs/remediation/G1_5_CHECKPOINT_COMPOSITION.md`. Ignored environments,
caches, secrets, key material, attachments, and unrelated files are excluded.
The final staged audit proved a 40/40 manifest match with no unstaged,
untracked, deleted, or whitespace-error path. The local checkpoint is
`0aa1674653da93e135572b06f858cd0d18f41926`, tree
`3639228c82af4509c885ec23dc15459630b74eae`; this documentation-only follow-up
records the hash that the checkpoint cannot self-contain.

## Act 5 — Build: G2A M1-M2 Observation Semantics V2

### Round 1 — Codex build

Added a typed `movement_payload.v2` contract and minimal semantic registry for
`movement_index`, `movement_event_count`, and audit-only
`legacy_ambiguous_movement`, with stable categorized domain rejections. Added
one `CanonicalObservationFactoryV2` for schema/semantic/provenance/time
validation, UTC normalization, canonical semantic projection, and deterministic
semantic identity distinct from transport receipt identity.

Wired the existing typed setting into Push, Pull, replay journey generation,
and replay normalization. V1 remains the default and unchanged. V2 maps proved
vendor meanings before the shared factory and has no permissive fallback.
Because M3 is excluded, accepted V2 facts cross the unchanged persistence
boundary through an explicit legacy compatibility candidate; no migration,
historical upcast, aggregation, trend, risk, CareStrategy, or report cutover was
performed.

The factory is placed in the current domain layer instead of the master plan's
suggested future application package. This avoids a new reverse dependency from
the existing replay persistence seam and produces no architecture-debt growth.

### Round 2 — Codex fix pass

The initial event-count parity assertion used different authoritative instants
for Pull and Replay. The fixture was corrected to represent the same semantic
observation, then semantic identity equality was added for both index and count
parity. New mypy findings in the two semantic modules were resolved without
touching the repository's existing failure baseline. No production behavior
change was needed after the first implementation pass.

### Codex verification

- G2A contract/factory/parity/feature suite: 24 passed.
- Focused G2A/G1/Perceptor/Pull/Replay/architecture selection: 190 passed.
- Unit-marked suite under Python 3.11.15: 1,071 passed, 35 deselected.
- Non-PostgreSQL/non-E2E/non-ASGI suite: 1,068 passed, 38 deselected.
- Architecture suite: 5 passed; no new SCC, self-import, or forbidden edge.
- OpenAPI canonical check, compileall/import smoke, and `git diff --check`: PASS.
- The sandbox-only eight localhost bind failures disappeared in the approved
  local-only rerun; no external provider was contacted.
- PostgreSQL remains `ENV_BLOCKED`; no M3 work or host repair was attempted.

Diff review confirms V1 default behavior and the G1 `20.9` characterization
remain unchanged, V2 uses one semantic authority, all persistence/analytics
work is deferred, and no OpenAPI snapshot or migration changed. One bounded
fix pass was used.

## Act 6 — Build: G2B-Preflight PostgreSQL 16 readiness

### Round 1 — Environment proof

Reconfirmed the exact clean G2A entry commit and diagnosed Docker as
`DOCKER_SOCKET_PERMISSION`: the default socket is owned by uid/gid 65534 and
is not accessible to uid/gid 1018. No privileged host mutation was attempted.
Discovered the user-owned PostgreSQL 16.14 distribution outside `PATH`, then
created a unique loopback-only temporary cluster and dedicated
`sleepagent_replay_test` database without touching the unrelated demo cluster.

Applied immutable migrations 001-013 from zero, bootstrapped the canonical
test roles, and verified the clean ledger, hashes, forced RLS, policies,
triggers, and critical functions. The initial PostgreSQL marker produced 30
passes, one expected process-proof skip, and two fixture foreign-key failures.

### Round 2 — Codex fix pass

The two Perceptor fixtures used hard-coded service principals that canonical
`test-bootstrap` never creates. Changed only those tests to consume the
existing API and Worker principal environment variables. The focused tests
then passed 2/2. No production or migration file changed.

Reset the dedicated database, reapplied 001-013, enabled loopback TCP
SCRAM-SHA-256, verified all four test credentials, and ran the authoritative
fresh-database marker: 32 passed, one intentional completed-process evidence
reader skipped, and 1,073 deselected. A deliberate no-reset repeat confirmed
that durable-state collisions make reset-before-full-marker part of the
contract; a final fresh SCRAM run passed.

G2A/characterization re-verification passed 29 tests, architecture passed 5,
the final migration check remained at 013, and `git diff --check` passed. The
Compose example is loopback-only and the new remediation document records
canonical Compose plus verified native start/reset/test/stop commands. M3 was
not started.

## Act 7 — Build: G2B M3 persisted Observation Semantics V2

### Round 1 — Codex build

Added manifest-pinned migration 014 and an immutable forced-RLS semantic
sidecar linked to the existing V1 canonical observation. The domain-owned
persisted contract and PostgreSQL adapter carry metric/value/unit/window,
source/provenance, semantic identity, version fields, analytic trust, and
classification evidence without changing raw/vendor rows or migrations
001-013.

Retained the canonical V2 result through Push, Pull, and Replay persistence;
added a bounded dry-run/resumable historical classifier; and implemented
separate index and interval-aware count aggregation. Product exact-revision
facts now join the semantic sidecar and expose metric-safe deterministic
evidence. V1 remains the default and its legacy `20.9` characterization remains
unchanged. The unresolved generic runtime Movement threshold is explicitly
fail-closed as `SEMANTIC_THRESHOLD_UNRESOLVED` for V2.

### Round 2 — Codex fix pass

The first explicit-V2 run of the broad Pull integration correctly rejected an
unrelated vendor-derived realtime bed-presence candidate under the frozen V2
ontology. Kept that later legacy branch on V1 and narrowed explicit V2 to the
Movement-relevant history/reconciliation segment instead of broadening M3.

Hardened aggregation so identical count-window observations deduplicate to one
fact while conflicting and overlapping windows are excluded. Added per-semantic
advisory locking to make concurrent upcasts safe, tightened non-movement trust
constraints, moved the runtime-role privilege proof to the canonically
bootstrapped database, and added executable constraint/append-only evidence.
The final audit also scoped the upcast identity cache and keyset cursor by
namespace/data mode, made already-classified/error counts explicit, required
source authority at the aggregation boundary, and excluded non-hourly windows
from the hourly maximum without excluding them from a compatible disjoint
total.

### Codex verification

- Focused semantic/Product/Perceptor selection: 113 passed.
- Migration/tooling/architecture selection: 69 passed.
- Unit-marked suite: 1,081 passed, 36 deselected.
- Non-PostgreSQL/non-E2E/non-ASGI suite: 1,081 passed, 36 deselected.
- PostgreSQL 16 marker from a clean 001-014 database: 33 passed, one expected
  completed-process evidence-reader skip, 1,083 deselected.
- Fresh 001-014 and realistic 013-to-014 upgrade/upcast/RLS/constraint proof:
  PASS on isolated temporary databases, including unchanged raw hashes and
  zero inserts on rerun.
- Architecture no-growth, OpenAPI snapshot check, compileall/import smoke,
  migration check, historical migration hashes, and `git diff --check`: PASS.

Diff review confirms one V2 analytic authority, no generic mixed Movement in
V2 Product evidence, no invented threshold, no default flip, and no work from
later reporting or remediation phases. Two bounded fix rounds were used.

## Act 8 — Build: G2C default Observation Semantics V2 cutover

### Round 1 — Codex build

Traced the generic `RadarNightSummary.movement_count` through current consumers,
historical adapters, deleted quality aggregation, tests, fixtures, and the
original risk/trend policy. The historical value mixed a sample-count-above-2.0
heuristic, a supplied report count, and later a count of all Movement payloads;
it has no authoritative V2 producer. Classified it as an obsolete legacy
threshold, retained its V1 characterization, and kept both proved V2 Movement
metrics excluded rather than guessing a replacement.

Traced Pull `smbdFlag`/`probStatus` to vendor-derived bed-presence candidates
and Push `OnBed` to device-measured state. Replaced the ontology's blanket
device-source fallback with an exact bed-presence allowlist and bumped that
ontology contract to v2. Unproved sources continue to fail closed. Changed the
typed settings default and environment fallback to Observation Semantics V2,
while retaining explicit V1 rollback.

### Round 2 — Codex fix pass

The first settings test exposed a second hard-coded V1 fallback in
`from_environment`; changed it to V2. Restored the legacy single-source error
wording while retaining the new multi-source form so the frozen G1 rejection
characterization remained stable. No production scope expanded.

### Codex verification

- Focused G2C selection: 125 passed.
- Fresh PostgreSQL 16.14 migration/bootstrap/check at schema 014: PASS.
- Explicit V2 PostgreSQL Push/Pull/persistence: 3 passed.
- Unit-marked regression: 1,085 passed, 36 deselected.
- Architecture: 5 passed with no new debt.
- OpenAPI check, compileall, and `git diff --check`: PASS.

Diff review confirms the default cutover is a two-line typed configuration
change, the ontology change is narrowly source-scoped, V1 rollback remains
tested, the obsolete threshold cannot consume V2 analytics, and no later-phase
or external-effect behavior was introduced. One bounded fix pass was used.

## Act 9 — Build: G3 reporting time and locale semantics

### Round 1 — Codex build

Added a single reporting context to the existing shared-analysis/report
framework, pinning IANA timezone, zh-CN locale, audience, UTC boundaries, local
sleep date, and renderer version. Product desired identity and new shared
analysis now bind the reporting time authority. Fixed deterministic bed-exit
display conversion to use the exact-revision timezone instead of labeling a
UTC clock as local.

Introduced content-addressed structured report facts derived from deterministic
Product metrics and typed accepted-claim semantics. New semantic hashes exclude
free-form summary prose and localized wording; role projection hashes include
audience, renderer, and localized context. Replaced Family/Doctor compatibility
prose projection with deterministic zh-CN role rendering and kept the existing
bounded Elder atom path. Historical serialized artifacts retain the exact
legacy hash-validation branch.

### Round 2 — Codex fix pass

The initial focused run exposed two bounded implementation defects: defaulted
fact fields were absent from the pre-validation hash material, and legacy test
shims lacked real episode/timezone fields expected by desired identity. Built
fact IDs from fully defaulted model material and limited the compatibility
omission to non-production source shims. A final integrity review then changed
the new localization test to produce its semantic facts through the real
shared-analysis path rather than mutating hashed fact content after creation.

### Codex verification

- Focused report/shared-analysis suite: 169 passed.
- Reporting/time/Product characterization selection: 84 passed.
- Broader Product/report regression: 347 passed.
- Unit-marked regression: 1,088 passed, 36 deselected.
- Fresh PostgreSQL 16.14 Product/report integration at schema 014: 19 passed.
- Architecture: 5 passed with no new debt.
- OpenAPI check, compileall, and `git diff --check`: PASS.

Diff review confirms one local-night authority, no UTC clock mislabeled as
local, no arbitrary English claim prose entering zh-CN role projections, and
semantic identity stability across renderer changes. Existing public API and
migrations remain unchanged, later report cutover and external-effect phases
were not started, and the bounded two-fix-round limit was respected.
