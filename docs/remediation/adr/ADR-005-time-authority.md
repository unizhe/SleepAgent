# ADR-005: Time authority

- Status: Accepted for remediation
- Date: 2026-08-30
- Scope: Future M4-M5 reporting-context migration; no G1 renderer change

## Decision

```text
UTC = storage and ordering authority
IANA timezone = deterministic local-date/display projection
LLM = no timezone conversion authority
```

## Invariants

- Canonical instants are timezone-aware and stored/compared as UTC.
- Local night ownership and display are projected by deterministic code from a
  pinned IANA timezone and source context, including offset/fold where needed.
- UTC clock text is never relabeled as local time.
- A model receives already-projected structured time facts and cannot convert
  timezones or select a local date.
- Existing `NightEpisodeV2` date ownership remains distinct from future data
  finalization.

## G1 boundary

G1 characterizes the direct `strftime` UTC-as-local defect and correct existing
episode/pull behavior. It does not change report-time projection or rendering.
