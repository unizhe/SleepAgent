# Plan Review Log: backend-engineering-rebase

- Plan: `docs/audits/backend-engineering-rebase/PLAN.md`
- Design input: `docs/audits/backend-engineering/PLAN.md`
- Code authority: `c1cb1e6bf40c9ed7d73e6394ea724f356980b747`
- Reviewer: Codex, same-session adversarial pass
- Maximum rounds: 5
- Initial state: grill decisions locked; adversarial review pending

## Round 0 — Requirements grill record

Locked decisions before adversarial review:

1. Re-audit every old requirement; implemented work becomes inherited baseline, unfinished work is recompiled into new dependency-derived stages. Old B0–B6 are not execution phases.
2. Historical PLAN remains untouched. New PLAN and review log live under `docs/audits/backend-engineering-rebase/`.
3. First real slice is normal-one-night → committed NightEpisode → Product Runtime → exactly three role projections → typed `/product/sleep/today`.
4. Demo seed is durable async; returned operation ID is the root for the whole journey and cannot succeed at import completion.
5. Demo authority is server-owned and replay-only; requests cannot self-assert identities/scopes. Verifier private JWS key is not mounted in Product server.
6. Generic Product projection is an unreleased placeholder and may break in place. Other unimplemented reads and writes fail typed 501 until their stage.
7. Public command families and Product interactions use the same durable substrate but explicit handlers and states.
8. Induction, delivery and reconciliation are real protocols with deterministic replay adapters; `ReplayNoModelHandler` cannot count as success.
9. Retention is bounded: scheduled raw expiry plus replay subject forget/epoch invalidation/receipt, without production KMS/backup/lifecycle platform.
10. First-stage process gate includes clean and Worker-restart black-box HTTP runs; verifier has no internal Runner/repository/admin SQL path.

No code or non-plan document was changed during the grill.

## Round 1 — Public contract and staged-capability audit

### Critique

1. `product_sleep_today.v1`列出了`state=no_data`，却把episode/projection/content写成无条件字段，客户端无法知道nullability，合同不是真正可判别union。
2. 第一条black-box run会读取`/api/v1` NightEpisode，但v1 replay watermark被推迟到Stage 2，违反“所有replay输出不可移除水印”的首slice门禁。
3. Stage 1 tests要求`generation reset fence`，而公开reset被安排到Stage 5，形成不可执行的阶段依赖。
4. verifier读NightEpisode与三role `/today` 所需scope/public-key来源未锁死，存在借admin SQL或把private key放server的逃逸路径。

### Codex response

`ACCEPTED`。PLAN已修订：

- 把`/today`定义为由state判别的顶层union，明确no_data的null字段和committed states的non-null字段；
- 把server-owned v1 replay headers提前到Stage 1并覆盖success/error/pagination/operation reads；
- Stage 1只用integration authority change证明generation fence，公开advance/reset分别留到Stage 3/5并在此前typed 501；
- 锁定server registry的BFF public-key fingerprint与最小scope matrix，private key只在verifier。

Round 1 result: `REVISE_AND_CONTINUE`.

## Round 2 — Durable root and split-brain audit

### Critique

1. PLAN同时引入root Operation与journey claim row，却没有规定二者terminal transition的原子性，可能出现journey succeeded/root pending或root succeeded/receipt缺失。
2. “等待不消耗retry”只有目标描述，没有claim fields、CAS、resume clock、deadline和outbox角色；实现者仍可能用普通retry或持长lease busy-wait。
3. idempotency只覆盖同caller key，没有锁定不同keys但同semantic journey的收敛，也没有one active generation/arm/root约束。
4. SECURITY DEFINER bootstrap若接受namespace/actor/scope参数，会重新引入请求自铸authority风险。

### Codex response

`ACCEPTED`。PLAN已加入Journey protocol invariants：

- root ID唯一绑定journey，固定scope/pins/lease/wait/failure字段；
- API原子创建CommandReceipt、workload-origin root与journey；semantic key使等价caller keys收敛；
- WAIT用fenced短transaction写checkpoint/resume并释放claim，server time驱动，outbox只作可丢wake-up hint；
- terminal receipt、journey和root在同一transaction终结；
- bootstrap function只收root/journey ID，并重新验证session role、grant、allowlist和generation。

Round 2 result: `REVISE_AND_CONTINUE`.

## Round 3 — Stage dependency and retention truth audit

### Critique

1. Stage 2把confirm的“effect exactly once”写成单一门禁，但external delivery handler到Stage 4才存在，可能诱导实现者提前启用no-op delivery或把pending intent当已送达。
2. `/api/v1`五类commands只说“真实handler”，没有锁定各自domain mutation，仍可用写audit后success的no-op通过。
3. Stage 3依赖multi-night/3–7 day journey，却没有定义`/demo/v1/advance`如何只推进ScenarioClock，可能错误推进lease/retention等ControlClock行为。
4. Stage 5没有排除当前全局raw key路径，也没定义reset在epoch bump、外部key call和final receipt之间的恢复/幂等语义；测试可能只删binding或让旧JWS 403就冒充forget完成。

### Codex response

`ACCEPTED`。PLAN已修订：

- 区分Stage 2 database-local CareAction effect与Stage 4 external sink effect；delivery queue此前保持disabled/pending；
- 锁定monitoring CAS、source-correct feedback、unique reanalysis child等命令语义；
- 为Stage 3加入generation-bound durable advance，并禁止触碰ControlClock；
- Stage 5要求fresh post-migration seed使用per-domain DEK，canonical decrypt port返回`key_destroyed`；
- reset先原子epoch/generation fence和建job，再在transaction外调用key port，最终root只在receipt complete后成功；旧assertion失败和新actor no_data两者都验证。

Round 3 result: `REVISE_AND_CONTINUE`.

## Round 4 — Migration, secret isolation and ledger completeness audit

### Critique

1. migration proof只写“existing empty 001”，没有规定valid populated 001数据与legacy generic role rows的行为；实现可能用清库或猜测backfill规避升级问题。
2. 两个API roles已有拓扑，但surface集合和ephemeral signer material的文件/挂载边界未可验证；当前`test:` key provider甚至可能让server推导private key。
3. 异步root列了error codes却没有HTTP/terminal边界，客户端无法稳定区分request rejection、dependency failure和已accepted child failure。
4. old key decisions与out-of-scope用范围行合并，不满足“逐项审计旧PLAN每一项”的可追踪要求。
5. recovery run只说“Product commit前kill”，可能在Product尚未开始时就通过，未证明prepare/commit边界。

### Codex response

`ACCEPTED`。PLAN已修订：

- 001→target必须支持schema-valid populated 001，禁止删除/猜测backfill；legacy generic projections对public API为no_data，等待新reanalysis；
- 锁定Demo/BFF互斥surface profiles，以及ephemeral Ed25519 private-only-verifier/public-only-server挂载，禁止derived test private provider；
- 加入400/401/403/404/409/413/422/501/503稳定transport matrix，accepted后的child failure固定走200 terminal root status；
- 展开KD-01到KD-08和OOS-01到OOS-08逐行处置；
- process run在public `waiting_product`窗口kill，并用独立PG fault test精确覆盖prepared/commit。

Round 4 result: `REVISE_AND_CONTINUE`.

## Round 5 — Final executability and privilege audit

### Critique

1. Stage 3要求每个scenario在public reset后换generation，但reset直到Stage 5才实现，阶段顺序仍有隐藏依赖。
2. root polling与child work共享single Worker profile，却没有queue priority；到期journey可能自轮询并饿死normalization/Product。
3. replay bootstrap采用SECURITY DEFINER，但PLAN尚未锁定safe search_path、PUBLIC revoke和精确workload grants。
4. proof command只显式运行首slice verifier，后续command/read/effect/retention stages可能只靠pytest marker而缺黑盒suite。

### Codex response

`ACCEPTED`。PLAN最终修订：

- Stage 5前各abnormal scenario使用独立fresh DB/namespace/generation，Stage 5后才测public reset；
- 锁定child queues优先于journey polling，journey每claim只推进一个checkpoint；
- 锁定slice RLS表、SECURITY DEFINER safe search_path/session-role/PUBLIC revoke及worker handler allowlist；
- proof manifest加入commands/interactions、read-models/abnormal、effects/reconciliation、bounded-retention四个HTTP suites。

未发现仍会改变Goal、公共contract、root成功条件、identity topology、retention边界或stage依赖的material contradiction。

Round 5 result: `APPROVED`.

## Final review disposition

- Rounds used: 5 / 5
- Status: `APPROVED`
- Files changed during review: only this log and `PLAN.md`
- Implementation authorization: not granted by this review; user sign-off is still required before `codex-build`.

## Act 3 — Build

Resolved build values:

- `SPEC_FILE`: `docs/audits/backend-engineering-rebase/PLAN.md`
- `MAX_FIX_ROUNDS`: `2`
- `LOG_FILE`: `docs/audits/backend-engineering-rebase/PLAN-REVIEW-LOG.md`
- `PROOF_CMD`: the proof command manifest in the locked PLAN
- Scope: implement all remaining Stages 1–6 from the repository freeze; preserve inherited behavior; do not build a production data-lifecycle platform.

### Round 1 — Codex build

Implemented the frozen plan as one canonical backend release:

1. Added manifest-pinned additive migrations `002`–`007`, migration `apply/check/status`, exact release attestation, scoped UoW/pool reset checks, replay authority, durable root/journey/queue protocols, role projections, command/interaction state machines, dedicated read models, induction/delivery/reconciliation, and bounded retention/reset receipts.
2. Added canonical BFF, Demo, Worker, migration, and internal profiles; strict server-owned replay/correlation/epoch contracts; protected operational surfaces; typed Product/Demo APIs; HTTP-only verifier suites; deterministic replay fixtures/adapters; and real canonical lifespan coverage.
3. Added OpenAPI snapshots/reference client updates, production Docker cleanup, runbooks, migration/static architecture tests, PostgreSQL invariant tests, Stage 1–5 verifier scripts, structured redacted logs, and bounded non-PHI HTTP/queue metrics.

Initial verification found three release-hardening defects: the real-lifespan `/livez` proof depended on the unavailable sandbox worker-thread pool, verifier goldens were still present in the built wheel, and the initial cross-queue namespace capacity check could race between concurrent claim transactions.

### Round 2 — Codex build fixes

Applied the bounded final fixes:

1. Made the non-blocking `/livez` route native async while retaining the real canonical runtime startup/close and ASGI middleware proof.
2. Moved the retired workflow golden loader and data into `tests/support/simulation_verifier`; the server wheel now contains only strict external scenario facts and no verifier oracle.
3. Added transaction-scoped PostgreSQL advisory reservation before the global namespace-capacity decision, retaining per-queue last-claim ordering and preventing concurrent queues from oversubscribing a namespace.
4. Removed the dormant standalone Sleep API global runtime/in-process worker entrypoints, added UUIDv7 independent-process coverage, and changed verifier scripts so demo/service credentials and the signer path are supplied through environment variables rather than argv.

Fix rounds used: `2 / 2`.

### Codex verification

Passed local proofs:

- `python -m pytest -q -p no:cacheprovider`: `1170 passed, 11 skipped`.
- `python -m pytest -q -p no:cacheprovider -m unit`: `1168 passed, 12 deselected` at the time it ran; the final full suite includes the subsequently added release-artifact assertion.
- `SLEEPAGENT_SETTINGS_PROFILE=test-postgres python -m pytest -q -p no:cacheprovider -m asgi_lifespan`: `4 passed, 1176 deselected`.
- `SLEEPAGENT_SETTINGS_PROFILE=test-postgres python -m pytest -q -p no:cacheprovider -m postgres`: `11 skipped, 1169 deselected` because no PostgreSQL runtime was available.
- `python scripts/generate_openapi_snapshots.py --check`, `python -m compileall -q backend reference_client sleepagent tests`, `bash -n scripts/*.sh`, and `git diff --check`: passed.
- `docker compose --env-file .env.test.example config --quiet`: passed without emitting credentials.
- Fresh sdist-to-wheel build passed. Archive inspection found `204` files, migration target `7`, a matching migration `007` SHA-256, runtime external facts, and zero `tests/`, workflow goldens, expected or action oracle files. A no-dependency install to an isolated target imported the packaged manifest successfully, and importing `backend.main` without a profile failed closed as required.

Environment-gated proofs, not claimed as passed:

1. The host exposes Python `3.13.9`, not the required Python `3.11`, and has no `mypy` installation. Exact isolated `pip install --no-deps .` could not fetch pinned build dependencies through the restricted network; the no-build-isolation retry correctly rejected Python 3.13 against `>=3.11,<3.12`.
2. Docker socket access was denied both in the sandbox and after the approved escalation retry. The host also has no PostgreSQL server/client or `psycopg`. Therefore live `001 → 007` apply/check, RLS/process-kill/restart/disconnect/load/fairness tests, black-box Stage 1–5 suites, and the production image-content proof remain reproducible commands rather than executed evidence in this environment.

Diff review disposition: the implementation matches the locked goal, public contracts, identity topology, root success semantics, and bounded-retention scope. No commit, push, release, remote mutation, or production data operation was performed.

## Act 4 — User-directed completion audit and supplemental hardening

The post-build requirement/evidence audit found that Act 3's final disposition
was too broad. It described implementation shape correctly, but did not
separate locally proved behavior, environment-gated process proof, and missing
fault/load harness semantics. `COMPLETION-AUDIT.md` is now the authoritative
completion statement; this entry preserves rather than rewrites the earlier
record.

Supplemental hardening performed during that audit:

1. Added the missing `effects-reconciliation` verifier suite. A successful
   deterministic delivery now atomically makes the confirmed CareAction
   publicly observable as `active`, and the verifier proves the exact action
   through signed `/care` rather than trusting internal rows.
2. Registered a real `e2e` test that uses network sockets only and asserts the
   Demo/Product process surfaces are disjoint. It collects and skips when the
   independent services are absent instead of silently selecting zero tests.
3. Added fixed-label domain signals and a durable PostgreSQL operational
   metrics snapshot so an isolated internal API does not pretend to share API
   or Worker memory.
4. Removed remaining direct Runner/global-env construction from the diagnostic
   bridge, Radar transport, Habit transport and retired CLI boundary. Only the
   canonical PostgreSQL Product Worker directly invokes the Runner.
5. Added an exact PostgreSQL prepared/commit fence test: an attempt stays
   invisible after prepare, an expired old lease cannot commit, and no
   AnalysisRevision, role view or outbox becomes visible.
6. Migration `007` was subsequently re-pinned after the final hardening edits;
   its current SHA-256 is
   `08bd381723afd3c533d27d6a01596619f7ecf33619f2a76c03b6ecdfba1be0dc`.
   The complete `001`–`007` identities and manifest digest are recorded in
   `COMPLETION-AUDIT.md`.

Fresh supplemental proof:

- Final full suite: `1173 passed, 13 skipped`.
- Unit marker: `1172 passed, 14 deselected`.
- ASGI lifespan marker: `4 passed, 1182 deselected`.
- E2E marker: `1 skipped, 1185 deselected` with one real-socket test collected.
- PostgreSQL marker: `12 skipped, 1174 deselected`, including the exact
  prepared-fence case.
- OpenAPI snapshots, compileall, shell syntax, diff whitespace, and Compose
  configuration passed.
- A wheel rebuilt from a fresh sdist contains 204 files, target migration 7,
  the matching new `007` hash, runtime external facts, and no verifier oracle.

Remaining honest gaps are not relabeled as success: exact Worker kill after a
delivery journal reaches `send_started`, and a PostgreSQL
restart/disconnect/load/fairness supervisor gate. Live PostgreSQL, Docker
process suites, production image inspection and exact Python 3.11 dependency
proof remain environment-gated. No commit was created.

## Act 5 — Fault/load gate closure implementation

The two concrete harness gaps recorded by Act 4 were implemented without
expanding the release into a production data-lifecycle platform.

1. The deterministic replay receiver now takes a transaction-scoped advisory
   lock at its semantic-effect linearization point. The Stage 4 supervisor
   holds the matching session lock, waits until the separate Worker has
   durably committed `send_started`, sends that Worker `SIGKILL`, and only then
   releases the receiver boundary. Recovery must prove an absent effect,
   persist `known_not_delivered`, retry under a new fence, and expose the exact
   CareAction as `active` through the signed public API.
2. The first-slice supervisor gained a PostgreSQL restart mode. It seeds one
   root with the Worker stopped, starts processing, observes that the same root
   is nonterminal and has progressed, restarts PostgreSQL, waits for the API,
   Demo and Worker processes to recover, and requires the verifier to resume
   that original root to success.
3. The PostgreSQL integration suite now seeds 32 pending Product operations
   across four replay namespaces and drives eight concurrent claimers. With a
   namespace capacity of one, the durable result must contain exactly one
   running claim and one fairness-ledger increment for every namespace.
4. The database probe accepts its DSN from environment only, tolerates
   intentional transient disconnects, and fails rather than hiding a root
   that becomes terminal before the requested fault window.

Fresh local proof:

- Targeted Stage 4/probe/release-artifact regression: `20 passed`.
- Full deterministic suite: `1179 passed, 14 skipped`.
- Unit marker: `1178 passed, 15 deselected`.
- ASGI lifespan marker: `4 passed, 1189 deselected`.
- E2E marker: `1 skipped, 1192 deselected` with the real-socket test collected.
- PostgreSQL marker: `13 skipped, 1180 deselected`; it includes the exact
  prepared-fence and concurrent namespace fairness cases.
- OpenAPI snapshot, full-tree compile, shell syntax, diff whitespace and
  Compose configuration checks passed.
- A wheel rebuilt from a fresh sdist contains 204 files, target migration 7,
  the pinned `007` hash, runtime external facts and no verifier oracle. Its
  isolated packaged manifest imports, while `backend.main` without a profile
  fails closed.

The supervisor and PostgreSQL load gates are `IMPLEMENTED_UNPROVED_ENV`, not
passed: Docker daemon access was denied both normally and after the approved
escalation retry, and this host has neither PostgreSQL tooling nor `psycopg`.
Exact Python 3.11 and mypy proof also remains unavailable (`3.13.9` is the
installed interpreter). No commit, push, release or external mutation was
performed.

## Act 6 — real PostgreSQL and process proof closure

The environment limitations recorded by Act 5 were removed for Python and
PostgreSQL. An exact Python 3.11 environment and a local PostgreSQL server were
used to apply the immutable `001` baseline through additive migration `007`,
bootstrap separate migration/API/Demo/Worker identities, and execute the
public-process and database-invariant gates.

1. Stage 1 completed clean, Worker-restart and PostgreSQL restart/disconnect
   modes through independent HTTP processes and the causal Product invariant.
2. Stage 2 commands/interactions completed with exact local-effect and decline
   invariants.
3. Stage 3 completed multi-night, quality-insufficient, device-abnormal and
   urgent zero-model proofs.
4. Stage 4 held the deterministic receiver linearization lock, observed
   durable `send_started`, killed the exact Worker PID with `SIGKILL`, then
   proved reclaim, `known_not_delivered`, a fenced retry, and one public active
   CareAction. The Stage 4 PostgreSQL invariant passed.
5. A fresh complete PostgreSQL marker run passed all eight non-proof-JSON
   integration gates, including the concurrent four-namespace fairness test.

The previous `IMPLEMENTED_UNPROVED_ENV` classifications for live PostgreSQL,
RLS, process kill, restart/disconnect and load/fairness were therefore stale
and are superseded by `COMPLETION-AUDIT.md`. Docker remained unavailable.

## Act 7 — bounded-retention fault fixes and final verification

The first live Stage 5 attempts found three defects that static/unit evidence
had not exposed:

1. The reset reservation and subject-forget SQL functions had PL/pgSQL names
   colliding with input/output column names. Inputs/outputs were made explicit,
   and the raw-DEK update now uses a qualified target alias.
2. A reset root waiting for independently claimed subject-forget children
   consumed its generic operation retry budget. A fenced
   `sleepagent_wait_demo_reset` checkpoint now releases the lease and refunds
   that wait attempt. Live v33 evidence showed `retry` with `attempt_count=0/20`.
3. The public API correctly returned typed `409 cursor_resync_required` for an
   old Product cursor, but the external verifier accepted only 400/403. The
   verifier now accepts and explicitly validates the typed 409 contract.

Formal Stage 5 evidence then passed:

- v33 completed 1,988 normalization rows, four analyses, an atomic 1,490-fact
  advance, generation `1 → 2` reset, one subject-forget key job, old assertion
  and cursor fences, new-generation `/today no_data`, and the independent
  PostgreSQL invariant.
- v32 completed scheduled raw expiry on attempt 1. The wrapped DEK was removed,
  the canonical PostgreSQL raw reader returned stable `key_destroyed`, and the
  same signed Product projection remained `ready` with the same analysis
  revision. Repeated idle observation retained one job attempt, receipt and
  completion event.

Final verification:

- Full suite: `1190 passed, 14 skipped`.
- Unit marker: `1189 passed, 15 deselected`.
- ASGI lifespan marker: `4 passed, 1200 deselected`.
- Current-schema PostgreSQL marker with exact principals and Stage 5 proof:
  `9 passed, 4 skipped, 1191 deselected`.
- Release-artifact tests: `6 passed`.
- Fresh isolated sdist-derived wheel: 204 files, SHA-256
  `f1a6f22216d113e3d14a84a581ddc5bc9a512a21db3d15f731ec88e0ad7fb0cd`,
  with no tests/verifier oracle.
- Migration check, OpenAPI snapshots, compileall, shell syntax, diff whitespace
  and Compose configuration passed.

The sole unexecuted locked gate is production Docker image/container proof;
the daemon/socket is unavailable and wheel evidence is not substituted for
that gate. No commit was created.

## Act 8 — production image gate and release-import correction

The system Docker socket remained unavailable, but the host had enough
rootless Docker components to create a private Docker 29.3.0 daemon under
`/tmp`. Because subordinate-ID helpers were absent, the validation daemon used
a documented single-UID/fuse-overlayfs environment and ownership-normalized,
hash-verified Python 3.11.15 base layers. Temporary build-only user/ownership
shims were restored to their original files inside the result.

The first successful production-target build exposed a real release defect
that source and wheel checks had missed: the target correctly deleted the
legacy `sleepagent.simulation.replay` package, but
`sleepagent.product_device.api` imported it at module load. Consequently the
image could not import `sleepagent.retention` through the Product Runtime
graph. The fix keeps the physical production deletion and moves all retired
replay loading behind explicit development/test call paths. The diagnostic
Pydantic DTO now carries replay detail as serialized JSON, so production schema
construction does not resolve a development-only type. The stable diagnostic
scenario allowlist remains available without loading payload or oracle data.

A new architecture regression copies the source packages to an isolated tree,
performs the exact three production cleanup operations, and imports retention,
backend and Worker modules in a child interpreter. This prevents a future
string-only Dockerfile assertion from hiding the same defect.

Final evidence:

- Full suite: `1191 passed, 14 skipped`.
- Affected replay/diagnostic/release regression: `56 passed`.
- Final release-artifact architecture suite: `7 passed`.
- Final wheel: 204 files, SHA-256
  `1ae968351474ee1019d761dac4d9bdac745b61341fac63e5ff590460cd3b2a0e`,
  11 facts-only scenarios and no verifier-oracle files.
- Final production image:
  `sha256:c57a461cd7077eeed40649af703c046fe42a1e276bcb0736ac9f826c11cf361f`,
  185,854,885 bytes, `USER sleepagent`, UID/GID 1000, canonical uvicorn
  entrypoint, and no fixture/replay/seed/test/verifier-oracle artifact.
- Core retention/backend/Worker/Product API imports pass in the final image;
  no-config startup exits on required `SLEEPAGENT_BACKEND_PROFILE`.
- A strict transitive mypy run is not clean (`525 errors in 59 files`); mypy was
  not a locked gate, and the repository-wide typing debt was not expanded into
  this build.

The production Docker/image gate is therefore closed with the rootless
validation caveat recorded in `COMPLETION-AUDIT.md`. No commit, push, release
or production-data operation was performed.
