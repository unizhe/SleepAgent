# CareOutcome and personalization

## Closed-loop bridge

```text
completed human-attested CareExecution
  → HARD-finalized follow-up nights
  → immutable CareOutcome (causal_claim=false)
  → PersonalizationEffectReceipt
  → pending governance candidate
  → elder ACCEPT or REJECT
  → accepted governed Memory revision
  → next-cycle EvidenceReasoning context and read receipt
```

Only eligible completed execution starts an observation window. Baseline and follow-up nights must be current, non-provisional HARD finalizations with exact Observation V2 metric/unit/window/coverage/source compatibility. `movement_index` cannot be compared with `movement_event_count`. Missing or incomparable evidence yields waiting, insufficient-data, or not-comparable state rather than an invented result.

Every `CareOutcome` is deterministic, observational, and has `causal_claim=false`. It may describe an observed comparison after completion; it cannot claim treatment effect, medical efficacy, compliance, or that the care action caused a change. Late authoritative evidence creates a superseding outcome/receipt while retaining prior evidence.

## Governance boundary

The outcome worker writes an immutable `PersonalizationEffectReceipt` and pending governance record. It does **not** directly write confirmed Memory and never mutates Habit. Pending, rejected, superseded, wrong-subject, wrong-role, wrong-epoch, or changed-hash candidates are not consumed.

An authorized elder can ACCEPT or REJECT the exact candidate through the existing governance authority. ACCEPT creates an append-only governed Memory revision bound to the outcome evidence. REJECT creates no Memory. Exact retries converge.

Accepted Memory retrieval is bounded and exact-scope: the closed concept catalog, subject/namespace, current state revision, visibility, source evidence, and policy determine the returned slice. There is no vector database, embedding search, broad similarity retrieval, or autonomous self-learning. The later shared analysis pins the accepted revision and persists Memory read receipts; this is the proof that the loop reached the next cycle.

One outcome episode does not become a universal recommendation claim. Automatic Habit mutation remains forbidden.
