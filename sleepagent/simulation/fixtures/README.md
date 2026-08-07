# Canonical replay fixtures

These fixtures are synthetic, non-clinical, and non-release.

- `scenario.json` contains only external-world facts accepted by the runtime
  loader.
- `actions.jsonl` and `expected.json` live outside this runtime package in the
  top-level `simulation_verifier` source tree.

The runtime wheel contains `scenario.json` only. The canonical generator never
imports the verifier package, and no runtime fixture contains derived risk,
readiness, Agent, Safety, or Memory results.
