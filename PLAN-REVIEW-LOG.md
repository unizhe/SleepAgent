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
