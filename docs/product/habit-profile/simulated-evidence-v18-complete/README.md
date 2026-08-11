# Complete simulated acceptance fixture

This directory is synthetic development data, not release evidence.

- release identity: `a9e7408883759e8d914c2f1fc37e8525659d09d4c017cd8f42815de04c32fe2b`
- Habit catalog: `a75aa424e3260f042b65be82dbd45600d370515b073d8d17f31e69c539f44ae1`
- usability interactions: simulated, with synthetic attestation
- reviewers and signatures: simulated identities and synthetic credentials
- provider receipts/request IDs: deterministic synthetic shapes; no API call ran

The fixture exercises every schema and all 68 scenario/repetition slots. It
must remain `evidence_kind=simulated` and `real_provider=false`. Use
`docs/product/habit-profile/real-evidence-v18-collection` for actual collection.

Audit with:

```bash
python -m sleepagent.product_runtime.acceptance_materials \
  <this-material-directory> \
  --manifest docs/audits/agent-architecture/ACCEPTANCE-MANIFEST.json
```
