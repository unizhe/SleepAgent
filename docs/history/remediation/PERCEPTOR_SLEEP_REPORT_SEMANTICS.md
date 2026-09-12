# Perceptor SleepReport semantic contract

Status: authoritative for SleepAgent mapping of Perceptor V2.5.2
`/vitalSigns/getSleepReport` responses.

## Evidence and boundary

Evidence is applied in this order:

1. `云云对接API通用版（V2.5.2）.docx`, SHA-256
   `2e12cde7fb92b301adc6a94841fd08dd22ef9ad6fc7eae0c360d6d17c434a498`,
   section “睡眠报告”, endpoint `/vitalSigns/getSleepReport`.
2. Existing Observation V2 heart-rate, respiratory-rate, movement, interval,
   event, source, and persistence contracts.
3. The sanitized complete recorded-real structural derivative
   `tests/fixtures/perceptor_v2_5_2/sanitized_recorded_real_pull_get_sleep_report_full.json`.

The vendor document defines the physiological lists as measurements “during
sleep” and the `*_avg` fields as their means during sleep. The mean therefore
retains the established unit of its underlying documented series, but its
source is `vendor_derived`, not `device_measured`. Numeric plausibility was not
used to establish any unit.

The authoritative whole-report aggregation window is the envelope from the
earliest documented sleep-stage `start_time` to the latest sleep-stage
`end_time`. A trusted whole-report summary is not emitted without this window.
The requested local date remains source context; it is not invented as a
24-hour aggregation window.

## Mapping matrix

| Vendor field | Section / shape | Canonical metric_id | Canonical unit | Source | Value contract | Aggregation / window | Status | Evidence | Adapter action / product class | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| `sleep_stage_list` | top-level list | — | — | — | list of documented stage objects | report series | `SUPPORTED_TRUSTED` | V2.5.2 + real shape | parse children; `DIRECT_PRODUCT_FACT` | Container only. |
| `sleep_stage_list[].start_time` / `end_time` | epoch-second interval | `sleep_stage` | `stage_interval` | `vendor_derived` | positive epochs; end after start | the exact interval; also establishes report envelope | `SUPPORTED_TRUSTED` | V2.5.2 | emit interval; `DIRECT_PRODUCT_FACT` | No independent SleepAgent stage classification is claimed. |
| `sleep_stage_list[].type` | stage code | `sleep_stage` | `stage_interval` | `vendor_derived` | integer 1–4 only | same interval | `SUPPORTED_TRUSTED` | V2.5.2 | map deep/light/REM/awake; `DIRECT_PRODUCT_FACT` | Unknown codes fail closed. |
| `sleep_stage_list[].start_time_str` / `end_time_str` | vendor display text | — | — | — | null or text | none | `IGNORED_NOT_DOMAIN_RELEVANT` | recorded real shape | retain raw; do not emit | Epoch fields are authoritative; display duplicates are not trusted timestamps. |
| `heart_rate_data` | top-level list | — | — | — | documented measurement list | sleep-period series | `SUPPORTED_TRUSTED` | V2.5.2 | parse children | Container only. |
| `heart_rate_data[].time_long` / `value` | epoch-second sample | `heart_rate` | `beats_per_minute` | `device_measured` | numeric, `0 < value <= 300`; invalid sentinel becomes explicit invalid interval | instantaneous sample | `SUPPORTED_TRUSTED` | V2.5.2 + accepted R1 series contract | emit measurement; `DIRECT_PRODUCT_FACT` | Sensor sample, not vendor aggregate. |
| `heart_rate_data[].type` | extra scalar | — | — | — | null or integer | none | `IGNORED_NOT_DOMAIN_RELEVANT` | recorded real shape | retain raw; do not emit | Vendor documentation supplies no semantic definition. |
| `heart_rate_avg` | top-level summary | `heart_rate_mean` | `beats_per_minute` | `vendor_derived` | documented integer mean, 1–300 | whole SleepReport stage envelope | `SUPPORTED_VENDOR_DERIVED` | V2.5.2 mean definition + established series unit | emit aggregate; `SUPPORTING_EVIDENCE_ONLY` | Distinct identity from point samples. |
| `breathe_data` | top-level list | — | — | — | documented measurement list | sleep-period series | `SUPPORTED_TRUSTED` | V2.5.2 | parse children | Container only. |
| `breathe_data[].time_long` / `value` | epoch-second sample | `respiratory_rate` | `breaths_per_minute` | `device_measured` | numeric, `0 < value <= 150`; invalid sentinel becomes explicit invalid interval | instantaneous sample | `SUPPORTED_TRUSTED` | V2.5.2 + accepted R1 series contract | emit measurement; `DIRECT_PRODUCT_FACT` | Sensor sample, not vendor aggregate. |
| `breathe_data[].type` | optional extra scalar | — | — | — | null or integer | none | `IGNORED_NOT_DOMAIN_RELEVANT` | sanitized R1 structural evidence | retain raw; do not emit | Not present in the complete retained real shape; tolerated only as the recorded extension. |
| `breathe_avg` | top-level summary | `respiratory_rate_mean` | `breaths_per_minute` | `vendor_derived` | documented integer mean, 1–150 | whole SleepReport stage envelope | `SUPPORTED_VENDOR_DERIVED` | V2.5.2 mean definition + established series unit | emit aggregate; `SUPPORTING_EVIDENCE_ONLY` | Distinct identity from point samples. |
| `body_shake_data[].hour` / `count` | local hourly bucket | `movement_event_count` | `count` | `vendor_derived` | hour 0–23, unique bucket, non-negative integer count | exact binding-local one-hour bucket | `SUPPORTED_VENDOR_DERIVED` | V2.5.2 + remediation design | emit aggregate; `DIRECT_PRODUCT_FACT` | Never averaged with movement index. |
| `body_shake_data[].time_long` / `value` | legacy alternate shape | — | — | — | structurally numeric only | unproved | `AMBIGUOUS` | remediation design; absent from V2.5.2 SleepReport schema | retain raw; do not emit | No value-based guessing and no trusted V2 movement. |
| `sum_body_shake_times` | top-level total | `movement_event_total` | `count` | `vendor_derived` | documented non-negative integer total | whole SleepReport stage envelope | `SUPPORTED_VENDOR_DERIVED` | V2.5.2 “体动总次数” | emit aggregate; `SUPPORTING_EVIDENCE_ONLY` | Separate from hourly counts and movement index. |
| `getups[]` | local datetime string list | `bed_exit_event` | `event` | `vendor_derived` | unambiguous binding-local ISO datetime | instantaneous event | `SUPPORTED_VENDOR_DERIVED` | V2.5.2 + tested adapter contract | emit event; `DIRECT_PRODUCT_FACT` | DST ambiguity/nonexistence fails closed. |
| `sleep_profile.deep_sleep_rate` | profile percentage | `deep_sleep_ratio` | `percent` | `vendor_derived` | strict `0%`–`100%` percentage string | whole SleepReport stage envelope | `SUPPORTED_VENDOR_DERIVED` | V2.5.2 definition and percent-form example | parse numeric percent; `SUPPORTING_EVIDENCE_ONLY` | Source text remains in payload evidence. |
| `sleep_profile.sleep_efficiency` | profile percentage | `sleep_efficiency` | `percent` | `vendor_derived` | strict `0%`–`100%` percentage string | whole SleepReport stage envelope | `SUPPORTED_VENDOR_DERIVED` | V2.5.2 definition and percent-form example | parse numeric percent; `SUPPORTING_EVIDENCE_ONLY` | No care threshold or prompt use added. |
| `sleep_profile.in_bed_time` | profile string | — | — | — | vendor format/date ownership insufficiently defined | unproved event date | `AMBIGUOUS` | V2.5.2 label + example only | retain raw; do not emit | A plausible clock string is not enough to assign an instant. |
| `sleep_profile.sleep_time` | profile string | — | — | — | vendor format/date ownership insufficiently defined | unproved event date | `AMBIGUOUS` | V2.5.2 label + example only | retain raw; do not emit | Not overloaded as sleep duration. |
| `sleep_profile.wakeup_time` | profile string | — | — | — | vendor format/date ownership insufficiently defined | unproved event date | `AMBIGUOUS` | V2.5.2 label + example only | retain raw; do not emit | Requested date is not assumed to be the event date. |
| `sleep_profile.leave_bed_time` | profile string | — | — | — | vendor format/date ownership insufficiently defined | unproved event date | `AMBIGUOUS` | V2.5.2 label + example only | retain raw; do not emit | Requested date is not assumed to be the event date. |
| `sleep_profile.sleep_duration` | profile string | — | — | — | duration meaning documented, encoding/unit not explicitly defined | whole report, but numeric duration unproved | `UNSUPPORTED_SEMANTICS` | V2.5.2 label + hyphenated example | retain raw; do not emit | The hyphenated value is not guessed to mean hours/minutes. |
| `sleep_profile.in_sleep_time` | profile string | — | — | — | duration meaning documented, encoding/unit not explicitly defined | whole report, but numeric duration unproved | `UNSUPPORTED_SEMANTICS` | V2.5.2 label + hyphenated example | retain raw; do not emit | The hyphenated value is not guessed to mean hours/minutes. |
| `sleep_profile.wake_ups` | profile string | — | — | — | delimiter, cardinality, and timezone rules unspecified | unproved interval set | `UNSUPPORTED_SEMANTICS` | V2.5.2 label + single example | retain raw; do not emit | `getups[]` remains the supported event source. |
| `apnea_images[].apnea_images` / `begin_time` / `end_time` | optional apnea chart | — | — | — | exact documented keys; numeric chart array and non-empty time text; unit and relative-time anchor are unspecified | unproved | `UNSUPPORTED_SEMANTICS` | V2.5.2 | validate structure, retain raw, do not emit | Unknown nested keys fail closed; no clinical or trusted fact is created. |
| `apnea_data`, `breath_pause_data`, `apnea_count` | legacy adapter-recognized optionals | — | — | — | absent from V2.5.2 SleepReport definition and retained real shape | unproved | `UNSUPPORTED_SEMANTICS` | negative documentary/real-shape evidence | retain raw; do not emit | Kept explicit so an optional occurrence cannot become trusted automatically. |
| any other top-level, profile, stage, series, or bucket field | contract extension | — | — | — | unknown | unknown | `AMBIGUOUS` | no authority | fail the atomic report normalization | Unknown provider-contract changes fail closed. |

## Missing intervals

Perceptor missing/invalid candidates are created only when the provider emits
an explicit physiological sentinel or an out-of-contract value at an explicit
sample timestamp. SleepAgent does not synthesize cadence gaps in this path.
The payload records `missing_state`, target observation type, reason code, and
the normalization processing steps. Because no system-derived source category
exists and the source fact is the provider's device-measurement result, source
kind remains `device_measured`; changing it to `vendor_derived` would falsely
claim a vendor-computed aggregate. Canonical identity distinguishes the target
metric and reason. Product classification is `SUPPORTING_EVIDENCE_ONLY`.

## Consumer policy

- Raw physiological series, stage intervals, hourly movement buckets, and
  get-up events remain direct inputs to deterministic Product facts.
- Whole-report means, movement total, deep-sleep ratio, and sleep efficiency
  are persisted as trusted supporting evidence. Product continues to derive
  its displayed heart/respiratory means from canonical point samples, so no
  duplicate direct fact or new medical threshold is introduced.
- Ambiguous/unsupported values stay only in encrypted raw vendor evidence and
  never enter Observation V2, trend, risk, care, SharedNightAnalysis, role
  projection, or prompts.

## Extension policy

Known mapped trusted fields canonicalize atomically. Known explicitly ignored
or unsupported optional fields remain in encrypted raw evidence and produce no
trusted Observation. Documented raw-only structures are validated without
assigning them semantics. Legacy raw-only apnea optionals whose schemas are not
documented remain opaque, type-checked list containers. Any unknown structural
field in a documented/mapped structure, or any invalid known structure, fails
the complete SleepReport normalization; canonical validation is not bypassed
and partial trusted persistence is not allowed.
