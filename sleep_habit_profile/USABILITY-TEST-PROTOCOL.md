# Habit Profile usability release gate

This gate validates interaction usability only. It does not establish medical
validity, clinical benefit, diagnostic accuracy, or treatment effectiveness.

## Participants

- Recruit 3–5 target-age users (60+), using pseudonymous participant refs.
- Include more than one age band where practical.
- Do not record names, contact details, diagnoses, or free-form health history.

## Controlled test setup

- Configure the four server-side `SLEEPAGENT_HABIT_PROFILE_*` identity
  variables documented in `.env.example` for one pseudonymous participant at a
  time; never let a browser choose actor, role or subject headers.
- Open `/habit-profile`. Confirm the page remains usable if the participant
  declines every optional question.
- Reset the isolated test subject between participants. Do not use production
  records or copy test answers into another subject.
- Keep default proactive intake disabled. The facilitator may point out the
  optional button but must not answer, confirm or interpret wording for the
  participant.

## Fixed tasks

Each participant completes the same four tasks:

1. Receive an optional two-question light intake and explain, in their own
   words, why the questions are asked.
2. Skip one question and verify that the normal morning answer still appears.
3. Answer one question, review the proposed long-term Profile summary, remove
   one candidate, and identify that a new confirmation is required.
4. Find the Profile review and forget controls, then explain the audit-retention
   notice.

## Required observations

Record one `HabitUsabilityObservation` per participant:

- understanding of personalization purpose;
- understanding that every question is skippable;
- understanding that current use and long-term persistence are separate;
- whether the skip attempt succeeded without service loss;
- questions presented in the turn (must be 0–3);
- whether an unintended/erroneous confirmation occurred;
- interruption rating from 1 (not disruptive) to 5 (very disruptive);
- a short interaction-only note.

The release manifest remains fail-closed until a reviewer enters a real
`HabitUsabilityReport` with 3–5 observations. Synthetic unit-test fixtures must
never be copied into the release manifest.

The report must explicitly contain `"evidence_kind": "real"`. Identifiers that
still contain simulation markers are rejected even if that field is changed.
It must also contain a facilitator attestation that binds exactly the same
participant refs, declares the interactions were observed, declares no
synthetic data was used, and carries a signature reference.
The full JSON contract and material-audit command are documented in
`ACCEPTANCE-EVIDENCE-FORMAT.md`.
Facilitator signing, sanitized hash binding and the combined v18 evidence
handoff are documented in `REAL-EVIDENCE-COLLECTION-RUNBOOK.md`.

After the reviewer has verified the pseudonymous observations, enter the report
under `habit_usability_report` in
`agent_architecture/ACCEPTANCE-MANIFEST.json` and rerun
`tests/test_product_agent_acceptance.py`. A usability report alone does not
override any other missing acceptance scenario, domain review or release
identity requirement.
