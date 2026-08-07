# Real Perceptor one-night acceptance

Status date: 2026-07-30 (Asia/Shanghai)

Overall status: **PENDING**

No Perceptor base URL/credential, push signing profile, authoritative live
device binding, redacted real-night evidence pack, or configured production
authority database was available in this workspace. No vendor request was
made, and fixture/replay evidence was not substituted.

| Stage | Verdict | Capability receipt | Blocking fact |
|---|---|---|---|
| Transport | PENDING | `capability-receipt:perceptor:push:pending` | No real callback/network evidence |
| Push authentication | PENDING | `capability-receipt:perceptor:push:pending` | Vendor signature representation, timestamp and message-id behavior unconfirmed |
| Pull/report normalization | PENDING | `capability-receipt:perceptor:pull:pending`, `capability-receipt:perceptor:sleep_report:pending` | No real report payload/date/re-pull evidence |
| Binding | PENDING | Not applicable: platform stage | No authoritative live device-to-subject binding |
| NightEpisode | PENDING | Not applicable: platform stage | No canonical real-night observation/report set |
| Deterministic fast path | PENDING | Not applicable: platform stage | No real committed NightEpisode revision |
| Agent slow path | PENDING | Not applicable: platform stage | No real exact revision for four-Agent morning view |
| API/reference client | PENDING | Not applicable: platform stage | No real committed chain to query or consume |

Capability receipts remain adapter-scoped:

| Capability | Status | Artifact SHA-256 | Configuration-state SHA-256 |
|---|---|---|---|
| push | PENDING | `65961787305a151251cbcbc64456ffe445aab60c7cd47b050267d947cc261a3f` | `4f618dd03cafab8700f6a96930c72021f8a3e2d2243e698bc2731f833b8a1b75` |
| pull | PENDING | `65961787305a151251cbcbc64456ffe445aab60c7cd47b050267d947cc261a3f` | `4f618dd03cafab8700f6a96930c72021f8a3e2d2243e698bc2731f833b8a1b75` |
| sleep_report | PENDING | `65961787305a151251cbcbc64456ffe445aab60c7cd47b050267d947cc261a3f` | `4f618dd03cafab8700f6a96930c72021f8a3e2d2243e698bc2731f833b8a1b75` |

The artifact hash covers the checked-in Perceptor adapter, push-adapter and
pull-adapter source files with path delimiters. The configuration-state hash
covers only boolean presence/absence facts; it contains no secret.

Duplicate push and report re-pull checks: **not performed**.

Scope limitation: a future device-bounded, one-night pass would not establish
cross-vendor compatibility, long-term reliability, or clinical validity.

## Executable audit

The checked-in command is:

```bash
real-perceptor-acceptance \
  --manifest /protected/acceptance/manifest.json \
  --output /protected/acceptance/audit-bundle.json
```

It deliberately requires `SLEEPAGENT_SLEEP_API_MODE=production`. The ordinary
production runtime checks therefore also require shared PostgreSQL authority,
canonical read cutover, encrypted raw policy, service credentials, asymmetric
actor keys and authoritative role bindings. The command does not make a vendor
request, decrypt raw inbox payloads, invoke an Agent, or create an API/domain
resource. It only audits already committed projections and content-addressed
redacted evidence. Exit status is `0` only when every stage is `VERIFIED`, `3`
when any stage remains `PENDING`, and `2` on a failed stage or fail-closed
configuration/evidence error.

The input must validate as
`real_perceptor_acceptance_manifest.v1`. It contains:

- the exact `live:` namespace, Adapter version/artifact hash, non-secret
  configuration fingerprint and approved compatibility-profile hash;
- a SHA-256 of the device/binding scope, never the unhashed export of the
  device inventory;
- local sleep date and opaque committed raw/report/NightEpisode revision ids;
- exactly one evidence section for each of the eight independently reported
  stages;
- content-addressed opaque `evidence:`, `test-result:`, `approval:` or `urn:`
  references, never embedded payload bytes, URL credentials or query strings;
- duplicate-push and report-re-pull probes whose first/repeated resource ids
  and scoped before/after counts are identical.

References containing fixture, fake, replay, simulated, bearer/token/secret,
private-key or inline/raw-payload markers fail validation. The audit hashes the
exact manifest and privacy-minimized hashes of every committed fact it reads.
Passing automated checks remain `PENDING` with
`authorized_human_stage_approval_required`.

An authorized reviewer may later supply
`authorized_human_acceptance_approval.v1`. That approval names only passing
stages/capabilities and binds the manifest SHA-256, automated audit SHA-256 and
audit timestamp, together with content-addressed authorization and decision
artifacts. A stale approval, changed manifest, changed committed-fact digest,
or an attempt to override a pending/failed stage is rejected. Adapter
capability receipts are produced only for push, pull and sleep-report; platform
stages explicitly state that Adapter receipts are not applicable.

No example “real” manifest is checked in while the real device/night facts are
absent. Doing so would manufacture acceptance scope. The contract can be
inspected without credentials:

```bash
python - <<'PY'
import json
from sleepagent.integrations.perceptor import RealPerceptorAcceptanceManifest

print(json.dumps(
    RealPerceptorAcceptanceManifest.model_json_schema(),
    ensure_ascii=False,
    indent=2,
))
PY
```
