# Product Agent live Worker build log

## Act 3 — Build

### Round 1 — Codex build

Connected explicit deterministic/live Product Worker model selection to the
existing runtime factories. Added strict Product LLM environment parsing,
live startup fail-closed behavior, secret-safe configuration surfaces, standard
replay queue composition, compose passthrough, and controlled HTTP/PostgreSQL
regression coverage. No external provider was called.

Initial focused verification found only local-socket permission failures in the
filesystem sandbox. The same real loopback HTTP tests passed in an execution
context permitted to bind `127.0.0.1`.

### Round 2 — Codex build

Hardened base-URL validation (HTTPS except loopback), removed raw invalid
configuration values from parse exception cause chains, added exact
deterministic invocation identity assertions, and expanded missing-key coverage
to blank and placeholder values.

### Codex verification

- Final focused model/config/Worker/architecture set: `186 passed`.
- Deterministic invocation identity, live configuration, and live degraded
  journal semantics: `22 passed`.
- Real loopback provider and urgent set: `9 passed in 6.04s`.
- Final real PostgreSQL live Product Worker closure: `1 passed in 3.97s`.
- Final full non-socket partition: `1234 passed, 15 skipped, 8 deselected`.
- The eight deselected socket tests are exactly the separately passing
  loopback tests; a literal one-command non-sandbox full run was denied by the
  execution policy, not by a test assertion.
- `git diff --check` and compose configuration validation passed.

No contract, schema, migration, canonical scenario, replay registry,
NightEpisode, Terminal Demo, prompt, skill, or Agent architecture file was
changed by this build. No commit or push was made.
