# Product Agent live Worker build plan

## Goal

Connect the existing OpenAI-compatible structured Product Agent runtime to the
PostgreSQL `product_agent` Worker when
`SLEEPAGENT_BACKEND_MODEL_MODE=live`, while preserving the deterministic replay
baseline and every existing public, persistence, scenario, and Agent boundary.

## Frozen approach

1. Keep the existing deterministic Worker composition and invocation identity.
2. Select the existing live runtime factory only for explicit `live` mode.
3. Parse the four documented Product LLM settings and use the existing
   `DEEPSEEK_API_KEY` secret mechanism; fail composition when live
   configuration is missing or invalid.
4. Reuse the existing Product processor, runner, schema gates, safety gates,
   prepare/commit path, invocation dispatcher, and provider retry semantics.
5. Permit unrelated replay Stage 2/4 handlers to coexist with live Product
   model mode without changing their deterministic behavior.
6. Pass live configuration into the compose Worker while retaining
   deterministic as the default.

## Bounds

- No backend API/OpenAPI, DTO, schema, migration, scenario, replay registry,
  NightEpisode lineage, Terminal Demo rendering, prompt, skill, Agent roster,
  or publication-gate changes.
- No second provider/model/Agent stack.
- No real external LLM request.
- No commit or push.

## Proof

- Focused deterministic/live composition and adapter tests.
- Real loopback OpenAI-compatible HTTP success and failure tests.
- Real PostgreSQL live Product prepare/commit and secret-nonleak test.
- Live urgent preflight with zero HTTP calls.
- Architecture tests and full-suite regression, partitioning loopback tests only
  when the filesystem sandbox denies local socket creation.
