# Complete simulated acceptance fixture

This directory is synthetic development data, not release evidence.

- release identity: `7ece39290333119ff221286b415934a7306f0e58c23b4b10c61488b5db424f45`
- Habit catalog: `d4477443c4b3691afc99f5833958e31a98e4d5caa1c68aa129b8f4a7bf5d6903`
- usability interactions: simulated, with synthetic attestation
- reviewers and signatures: simulated identities and synthetic credentials
- provider receipts/request IDs: deterministic synthetic shapes; no API call ran

The fixture exercises every schema and all 68 scenario/repetition slots. It
must remain `evidence_kind=simulated` and `real_provider=false`. Use
`sleep_habit_profile/real-evidence-v23-collection` for actual collection.

Audit with:

```bash
python -m sleepagent.radar_agent.product_agent.acceptance_materials \
  <this-material-directory> \
  --manifest agent_architecture/ACCEPTANCE-MANIFEST.json
```
