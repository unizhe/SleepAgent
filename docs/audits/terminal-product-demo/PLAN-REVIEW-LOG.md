# Terminal Product Demo build log

## Act 3 — Build

### Round 1 — Codex build

Implemented the Phase-1 `sleepagent-demo show <scenario-id> [--trace]`
surface for `normal-one-night`, `worsening-vital-trend`, and
`urgent-zero-model`.

The command reuses the existing `DemoHttpClient`, `ProductHttpClient`, packaged
replay seed registry, Demo seed/advance/operation/trace endpoints, Sleep API
NightEpisode/current-risk endpoints, and Product today/trends/care endpoints.
It does not import a Runner, repository, Worker handler, generator, verifier,
oracle, or database implementation. Scenario and observation summaries come
from the existing packaged replay seed registry. Product statements come only
from returned public DTOs.

The urgent scenario accepts only the existing public
`unexpected_urgent_route` terminal boundary, then presents any publicly
readable Episode/risk state and explicitly marks role Product output as absent.
Safety/Evidence details not exposed by the current public contracts are marked
unavailable.

Focused proof:

```text
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/unit/test_demo_cli.py tests/e2e/test_simulation_contracts.py
32 passed in 14.82s
```

Full proof:

```text
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q
1206 passed, 14 skipped in 95.70s
```

Additional checks passed:

```text
python -m py_compile sleepagent/demo_cli.py tests/unit/test_demo_cli.py
python scripts/generate_openapi_snapshots.py --check
python -m compileall -q sleepagent backend reference_client tests
git diff --check
```

`ruff` and `mypy` entry points were unavailable in the active Python
environment. The complete pytest suite, compilation checks, OpenAPI drift
check, and diff whitespace check were run instead.

### Fix pass 1 — urgent trace boundary

Self-review found that the first renderer used the normal full-chain footer for
the urgent scenario even though the public root correctly terminated before a
Product operation. The renderer now says the deterministic urgent fast path
terminated before Product Runtime and role projections. A focused assertion
prevents the misleading normal footer and Product operation reference from
appearing in urgent output. Focused and full proofs were rerun successfully.

### Fix pass 2 — optional trace and public risk limitation

Completion audit added explicit proof that default `show` neither requests nor
renders Demo trace, while `--trace` does. It also added the actual replay actor
scope boundary: if the public current-risk route returns HTTP 403, the terminal
output labels the risk detail unavailable instead of using another authority or
deriving a value. The focused proof passed with 32 tests.

### Codex verification

Diff review confirms task changes are limited to the existing CLI, its unit
tests, README usage documentation, and this build log. No backend contract,
OpenAPI snapshot, migration, Product Runtime, Agent architecture, scenario,
replay registry, or verifier fixture was changed. The CLI has no
`expected.json`/`simulation_verifier` dependency and performs no risk, quality,
trend, Evidence, Care, or Safety computation.

An independent Docker-backed terminal run could not be performed in the Codex
sandbox: access to `/var/run/docker.sock` remained denied after the controlled
escalation request, and sandbox policy also denied localhost networking. The
existing repository process proofs and full suite passed; the new orchestration
is covered with strict HTTP-client fakes that assert exact paths, request
payloads, public watermarks, phase allowlisting, multi-night advancement,
zero-model termination, and rendering behavior. No live-run result is claimed
in this log.
