# Acceptance evidence collection templates

These files are collection slots, not release evidence. They are bound to:

- release identity: `5cb705abde1478feb8fe84d40fbca7c98c5f5da18490261ebce50c3864ae18d0`
- Habit catalog: `d4477443c4b3691afc99f5833958e31a98e4d5caa1c68aa129b8f4a7bf5d6903`

Do not promote a placeholder by changing `evidence_kind`. Replace it with data
from an actually observed 60+ participant session, an actual named professional
review/signature, or the corresponding actually executed provider scenario.
Placeholder identifiers deliberately contain `template`, so the production
schemas reject them as real evidence.

After collection, run:

```bash
python -m sleepagent.product_runtime.acceptance_materials \
  <this-material-directory> \
  --manifest docs/audits/agent-architecture/ACCEPTANCE-MANIFEST.json
```

See `docs/product/habit-profile/ACCEPTANCE-EVIDENCE-FORMAT.md` and
`docs/product/habit-profile/USABILITY-TEST-PROTOCOL.md` for the field definitions and
study procedure.
