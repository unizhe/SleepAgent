# Product Runtime Architecture Cleanup Plan

Status: locked for staged implementation on 2026-08-09.

This plan starts after `docs/audits/agent-architecture/PLAN-REVIEW-LOG.md` Round 65.  The
log contains no separately labelled Post-3C audit after that round; the current
cleanup brief and the repository audit are therefore the controlling Post-3C
requirements.

## Frozen architecture

- The only Product Agent identities are `SleepCareAgent`,
  `EvidenceReasoningAgent`, `CareStrategyAgent`, and `SafetyReviewAgent`.
- `ProductEpisodeRunner` remains the only Product Agent runtime facade.
- `ProductEpisodeRuntime` continues to own the Episode lifecycle/state machine.
- No legacy/dynamic Agent, alias, fallback runtime, or alternate 1+2+1 design
  may return.
- HITL is deterministic runtime governance, not a fifth Agent.
- Each phase is an independent acceptance boundary.  Implementation stops
  after reporting the phase gate and does not continue without acceptance.

## Baseline evidence

An exact local clone of `HEAD` fails test collection because a tracked test
imports ignored `server.py`.  Supplying that local file only exposes five more
failures: a wall-clock/frozen-clock Memory mismatch, a stale 10-versus-22 Habit
catalog fixture, and three tests that read absent root ZIP files.  The current
dirty tree reports `943 passed, 5 skipped`, so that green result is not a clean
checkout proof.

The current packaging work also has to land atomically: the intended wheel now
includes `backend`, but required backend modules, lock files, migrations, test
configuration, and fixtures are still untracked.  Root ZIPs, `.agents/`, the
local `HealthClaw_paper/` tree, and ignored `server.py` are not repository
inputs.  The dependency lock input must not reintroduce LangGraph, which Phase
3C removed.

The HITL audit found three mutable approval paths: `HumanDecisionService`, the
Task confirmation lifecycle, and standalone Habit confirmation.  Raw
`ConfirmationToken` values can currently enter both a run request and frozen
commit, and the Commit Controller never re-reads authoritative decision state.

## Phase A — reproducible checkout and one HITL authority

### A1. Clean-checkout gate

1. Remove every tracked import of ignored `server.py`; retain the root file as
   an ignored local prototype only.  A required diagnostic must live in a
   tracked package module.
2. Keep canonical acceptance material as tracked JSON/Markdown fixtures and
   build byte-stable ZIPs under `tmp_path`.  Tests must not read root local
   archives.  Current v23/v24 material must bind the 22-concept catalog.
3. Finish the injected, timezone-aware Memory control clock for in-memory and
   persistent stores and test it independently of wall time.
4. Land intended backend/package/config/test files as one candidate tree while
   excluding local artifacts.  No tracked file may import an untracked module.
5. Reconcile `pyproject.toml`, lock inputs, both hash locks, Docker inputs,
   README Python/install instructions, console scripts, and wheel contents.
   LangGraph/LangChain must remain absent.
6. Add static reproducibility invariants for ignored-file, local-archive, and
   untracked-module dependencies.

Acceptance is run from a real clean clone with CPython 3.11:

- hash-locked development dependencies install;
- `pip install --no-deps .` and a clean-wheel install both succeed;
- backend imports, console scripts, and migrations are present;
- `pytest --collect-only -q` has zero errors and full `pytest -q` has zero
  failures;
- Memory fixed-clock tests, `compileall`, wheel inspection, and
  `git diff --check` pass;
- frontend `npm ci`, typecheck, and production build pass when the frontend is
  part of the candidate checkout;
- the verified clone remains clean because environments/build outputs live
  outside it.

### A2. HumanDecisionService single authority

The only write path is:

`ActionProposal -> HumanDecisionService -> VerifiedApprovalCapability -> Commit Controller`

Implementation order:

1. Make decisions revisioned and compare-and-set.  Proposal creation is
   idempotent.  Approval, rejection, expiry, revoke, acquire, and execution
   result transitions are serialized by the authority repository.
2. Strengthen `ApprovalGrant` so all authority fields are required and include
   the complete approving-record binding, FactSnapshot ID/hash, target
   ID/hash, subject, scope, policy, expiry, and idempotency key.
3. Add an opaque, non-wire `VerifiedApprovalCapability`.  Only the typed HDS
   verifier/acquire operation can construct it after re-reading current
   authority, revalidating actor/role authorization, and atomically advancing
   the grant/decision to executing.  Revoke or expiry before acquire wins;
   acquire is the execution linearization point.
4. Delete `ConfirmationToken`.  Remove Care/Memory/external confirmations and
   caller-supplied declines from `ProductEpisodeRunRequest`.  Normal `run()`
   may only form immutable waiting targets.
5. Make every Commit Controller write accept only a verified capability,
   including Habit and Memory.  Bind decision/grant references into the commit
   journal input and `ToolReceipt`.  Same-key replay returns the original
   receipt; a different-key reuse fails.
6. Frozen continuation accepts verified capabilities and authority-derived
   rejections for exact persisted targets.  It performs no Agent invocation,
   replanning, regeneration, or republication.
7. Keep `PendingConfirmationTarget` as waiting metadata only and correlate it
   through an explicit proposal/decision reference, never loose
   `evidence_refs` matching.
8. Derive `HumanConfirmationRequest` only as a compatibility projection of
   `HumanDecisionRequest`.  Product code must not call Task confirmation
   resolve/expire/revoke/complete mutators, and Task status routing must use
   the Episode checkpoint plus HDS state.
9. Route standalone Habit change sets through the same HDS verifier and Commit
   Controller.  Pending change-set state remains target lifecycle only; a
   caller-chosen `HabitProfileConfirmation` is not authority.
10. Report committed/failed/unknown execution through HDS.  Keep
    `SafetyReviewAgent` and all Safety ordering unchanged.

Required invariants include:

- run request fields contain no token/grant/decline bypass;
- raw or fabricated grants, missing authority, wrong actor/role/subject,
  target, scope, FactSnapshot, policy, expiry, revoke state, or idempotency
  binding fail before journal reservation or effect execution;
- approve-versus-revoke and double acquire have one winner;
- every successful write receipt contains authority references;
- Task projection mutation cannot authorize or resume a commit;
- standalone Habit uses the same persisted HDS and rejects arbitrary
  confirmation IDs;
- frozen confirmed commit makes zero model calls and publishes nothing again;
- the exact four-role roster remains unchanged.

Phase A stops after the clean-clone gate and these invariants pass.

## Phase B — one composition root and lower-level contracts

1. Move request/result, pending targets, user-fact, and distinct continuation
   DTOs from `runner.py` into a lower-level runtime contract module.
2. Move publisher, result-store, Tool handler/context, and other cross-layer
   protocols into lower-level ports.  Persistence must not import Runner DTOs;
   Habit services must not import the concrete Tool runtime.
3. Add an explicit `ProductRuntimeBundle` built only by `runtime_factory`,
   containing the roster, Episode dependencies, Tool executor, Skill
   registry/resolver/compiler, HDS, Memory/Habit services, stores, Commit
   Controller, external executor, publisher, and policies.
4. Remove Tool handler registration, Memory construction, stores, and other
   implicit infrastructure from `ProductEpisodeRunner.__init__`; every Runner
   dependency becomes required.
5. API and worker receive the same bundle.  The outer backend process root may
   choose configuration/capabilities but delegates all Product composition to
   `runtime_factory`.
6. Tests use the same factory with explicit fakes or directly inject required
   ports; they do not create a second production composition recipe.

Gates: one production Runner construction path, no Persistence-to-Runner or
Habit-to-Tool-runtime reverse dependency, unchanged `run()` facade and Episode
semantics, and the full Phase A gate.  Stop after Phase B.

## Phase C — real Tool owners, Skill shadow, public API

1. Inventory every registered production Tool against a typed owner.  Replace
   Runner-local or `_passthrough` echo handlers with injected Service/domain/
   integration implementations; delete registrations with no real ability.
2. Resolve `state.commit_evidence` explicitly: implement its real persistence
   boundary or delete it and all allowlist references.
3. Preserve `Agent -> typed Tool -> Service/domain/integration`; prohibit Tool
   imports of concrete Agents.
4. Preserve parity goldens, then delete `skill_methods/` or exclude it from all
   production wheels.  There is one callable Skill runtime.
5. Replace wildcard/root namespace expansion with an explicit allowlist focused
   on the four concrete Agents, roster/factory, Runner, and essential public
   contracts.  Stores, controllers, repositories, compatibility DTOs, and
   internal policies use direct module imports.

Gates: registered Tool set equals the real-owner set, `_passthrough` is absent,
`state.commit_evidence` is implemented or absent everywhere, the wheel contains
no Skill shadow, the namespace allowlist passes, and the full prior gates pass.
Stop after Phase C.

## Phase D — responsibility-oriented Runner contraction

Preserve `run()` and the Episode state machine.  Extract one independently
changing axis at a time in this order:

1. `AgentInvocationCoordinator`: Context, Skill resolution/lock, prompt
   compilation, model call, provider budget, collaboration, feedback.
2. typed `ToolExecutionCoordinator`: payload/result adaptation, receipts,
   runtime-bound cache lifecycle.
3. `ConfirmedActionCoordinator`: post-Safety approval, verified capability,
   frozen commit.
4. `PublicationService`: source revalidation, Memory publication gate,
   journal, publisher.
5. `EpisodeResultFinalizer`: receipt revision, terminal persistence, Tool cache
   and provider-ledger cleanup.
6. Only then evaluate Habit orchestration, induction worker, and deterministic
   preflight.

Runner retains high-level orchestration, Evidence -> Care -> Safety ->
SleepCare ordering, entry into Tool/Safety/wait/publication, deterministic gate
routing, and the terminal decision.  There is no line-count target and no
deterministic policy moves into an Agent.

Expose two different continuation commands:

- `ReexecuteWithAddedFact` restores the original checkpoint plus a new
  authenticated fact and re-runs reasoning.
- `CommitFrozenConfirmedAction` consumes verified authority against the frozen
  result and performs no reasoning or publication.

Each extraction must prove unchanged invocation ordering, target hashes,
receipts, Safety/degradation behavior, and terminal cleanup before the next
one.  Stop after Phase D.

## Phase E — final acceptance

Run the complete locked proof matrix from a new clean checkout and verify all
of the following together:

1. The roster is still exactly the four frozen concrete Agents.
2. `ProductEpisodeRunner` is the only executable Product Agent runtime facade.
3. `ProductEpisodeRuntime` still owns the lifecycle/state machine.
4. HDS is the only approval authority and no raw-token bypass exists.
5. `runtime_factory` is the only Product composition root.
6. Every registered production Tool has a real owner; no passthrough Tool is
   present.
7. Agent, Tool, Service, and Persistence dependencies follow the locked
   direction.
8. The package-root public API is reduced to the intentional allowlist.
9. Hash-locked install, full pytest, compileall, diff check, wheel build and
   inspection, and isolated imports all pass from the clean checkout.

Phase E is reporting and final verification only; it must not silently relax
any earlier gate.
