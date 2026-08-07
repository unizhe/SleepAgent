# Acceptance evidence collection templates

These files are collection slots, not release evidence. They are bound to:

- release identity: `e2d721ca0d8906bf9fd53ab7844e4f49a5b892acf0ad14675471c1ed14c689d2`
- Habit catalog: `a75aa424e3260f042b65be82dbd45600d370515b073d8d17f31e69c539f44ae1`

Do not promote a placeholder by changing `evidence_kind`. Replace it with data
from an actually observed 60+ participant session, an actual named professional
review/signature, or the corresponding actually executed provider scenario.
Placeholder identifiers deliberately contain `template`, so the production
schemas reject them as real evidence.

After collection, run:

```bash
python -m sleepagent.radar_agent.product_agent.acceptance_materials \
  <this-material-directory> \
  --manifest agent_architecture/ACCEPTANCE-MANIFEST.json
```

See `sleep_habit_profile/ACCEPTANCE-EVIDENCE-FORMAT.md` and
`sleep_habit_profile/USABILITY-TEST-PROTOCOL.md` for the field definitions and
study procedure.
