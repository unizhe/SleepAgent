# Architecture overview

SleepAgent has one canonical product episode runtime. `ProductEpisodeRunner` is its public facade and `ProductEpisodeRuntime` owns the lifecycle. The four production agents are exactly `SleepCareAgent`, `EvidenceReasoningAgent`, `CareStrategyAgent`, and conditional `SafetyReviewAgent`; tools and services are not agents.

## Boundaries

- `product_device/` owns radar-device contracts, quality, and providers.
- `integrations/perceptor/` owns vendor push/pull ingestion.
- `sleep_domain/` owns canonical observations, episode state, authority, and domain persistence contracts.
- `product_runtime/` owns the four-agent episode, policies, tools, and orchestration facade.
- `persistence/` owns the SQLite development schema and single PostgreSQL production baseline.
- `product_api/` owns bounded product and diagnostic HTTP surfaces.
- `sleep_api/` owns the external `/api/v1` contract.
- `simulation/` owns replay-only production fixtures and adapters.

The HDS/composition root remains the only tool-owner composition authority. The `sleepagent.product_runtime` public API is intentionally fixed at 25 symbols.

## Safety

Identity, authorization, privacy, confirmation, urgent-risk handling, and external effects are deterministic gates. No agent can grant permission or execute an unconfirmed external action.
