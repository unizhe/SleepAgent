# Architecture overview

SleepAgent has one canonical product episode runtime. `ProductEpisodeRunner` is its public facade and `ProductEpisodeRuntime` owns the lifecycle. The four production agents are exactly `SleepCareAgent`, `EvidenceReasoningAgent`, `CareStrategyAgent`, and conditional `SafetyReviewAgent`; tools and services are not agents.

## Boundaries

- `product_device/` owns radar-device contracts, quality, and providers.
- `integrations/perceptor/` owns vendor push/pull ingestion.
- `sleep_domain/` owns canonical observations, episode state, authority, and domain persistence contracts.
- `product_runtime/` owns the four-agent episode, policies, tools, and orchestration facade.
- `persistence/` owns the manifest-pinned PostgreSQL release migrations and the
  legacy SQLite-only test substrate.
- `product_api/` owns bounded product and diagnostic HTTP surfaces.
- `sleep_api/` owns the external `/api/v1` contract.
- `simulation/` owns replay-only test fixtures and adapters. The production
  image physically removes fixtures, replay catalogs, and the seed registry.

## Backend process graph

`backend.main:app` is the only deployment entry. A BFF process mounts exactly
`public_v1 + product`, Demo mounts exactly `demo`, and Internal mounts exactly
`internal`; settings reject mixed profiles. Worker processes own only explicit
queue handlers. Migration credentials and DDL never enter API or Worker
containers.

The only unauthenticated operational route is `/livez`. Readiness, the
non-secret dependency manifest, bounded metrics, and reconciliation status live
behind the Internal process credential. Correlation IDs are server-generated.

Every reachable PostgreSQL transaction installs an exact namespace,
generation, subject, actor/workload, purpose, and governance-epoch scope. Pool
reset verifies that these transaction-local settings are absent before reuse.

The HDS/composition root remains the only tool-owner composition authority. The `sleepagent.product_runtime` public API is intentionally fixed at 25 symbols.

## Safety

Identity, authorization, privacy, confirmation, urgent-risk handling, and external effects are deterministic gates. No agent can grant permission or execute an unconfirmed external action.
