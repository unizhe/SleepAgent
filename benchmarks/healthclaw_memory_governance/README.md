# Longitudinal memory comparison

This package is evaluation-only. It has no production database credentials,
does not import the Product Tool Registry, and is excluded from the
`sleepagent*` production package discovery rule.

Each frozen, paired fixture must be labelled `synthetic` or
`consented_deidentified`. `full_history` means all authorized, unexpired
structured fixture items at the same cutoff; it never means raw conversations,
raw Episode results, forgotten/revoked data, or another role's data.

The gate counts every provider request input across retries and reports
cumulative exposure. It fails closed with fewer than 30 paired Episodes, any
security/safety violation, less than 50% exposure reduction versus
`full_history`, a one-sided 95% upper bound above five percentage points for
`full_history - governed`, or a non-positive one-sided 95% lower bound for
`governed - current_only`. Results are engineering release evidence only.
