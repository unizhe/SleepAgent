# Public API contract artifacts

The committed snapshots in `openapi/` are generated from the canonical backend
factory, not handwritten schemas:

- `backend-bff-v1.json` — `/api/v1/*` and `/product/sleep/*`
- `demo-v1.json` — replay-only `/demo/v1/*`

Regenerate with `python scripts/generate_openapi_snapshots.py`; CI-style drift
checking uses `python scripts/generate_openapi_snapshots.py --check`.

The independent Sleep API client is
`reference_client/sleep_api_v1_client.py`. It depends only on the public HTTPS
contract, persists opaque cursor/event state, signs epoch-bound actor
assertions, and imports no server package. `sleepagent-demo` is the external
HTTP verifier/reference workflow for Demo and Product replay contracts; it has
no database backdoor.
