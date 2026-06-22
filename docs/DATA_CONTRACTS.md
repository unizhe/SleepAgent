# DATA_CONTRACTS

This document records the stable MVP JSON contracts for Stage 1. The current contracts are mock-only and are intended for backend, frontend, and future Agent integration.

## Shared Rules

- All Pydantic schemas use `extra="forbid"`.
- Datetimes are timezone-aware UTC values serialized as ISO 8601 strings.
- Duration fields ending in `_seconds` are seconds.
- Duration fields ending in `_minutes` are minutes.
- Sampling rate fields ending in `_hz` are Hertz.
- Respiratory rate fields ending in `_bpm` are breaths per minute.
- Percent fields ending in `_percent` use a 0-100 scale.
- Metric fields use a 0-1 scale unless explicitly documented otherwise.
- Medical/report text is assistive mock output only and must not be treated as diagnosis or treatment.

## Enums

Sleep stages:

- `Wake`
- `REM`
- `NREM`

Respiratory event types:

- `normal_breathing`
- `hypopnea`
- `suspected_apnea`

Risk levels:

- `low`
- `moderate`
- `high`

Sex values:

- `male`
- `female`
- `unknown`

## `/mock-analysis`

Response schema: `SleepAnalysisResult`.

Top-level fields:

- `metadata`
- `epochs`
- `respiratory_events`
- `respiratory_trend`
- `sleep_summary`
- `respiratory_summary`
- `sleep_staging_metrics`
- `respiratory_detection_metrics`
- `risk_level`
- `generated_at`
- `notes`

Important nested fields:

- `metadata.record_id`: mock PSG record identifier.
- `metadata.source_dataset`: currently `mock`.
- `metadata.patient.subject_id`: mock subject identifier.
- `metadata.recording_start`: UTC datetime.
- `metadata.duration_seconds`: recording duration in seconds.
- `metadata.channels[].sampling_rate_hz`: channel sampling rate.
- `epochs[].start_second`: epoch start offset.
- `epochs[].duration_seconds`: epoch duration, currently 30 seconds in mock data.
- `epochs[].stage`: `Wake`, `REM`, or `NREM`.
- `respiratory_events[].event_type`: respiratory event enum value.
- `respiratory_events[].oxygen_desaturation_percent`: optional desaturation estimate.
- `respiratory_trend[].breaths_per_minute`: respiratory rate trend.
- `respiratory_trend[].spo2_percent`: optional SpO2 trend point.
- `sleep_summary.total_recording_minutes`: total recording duration.
- `sleep_summary.total_sleep_time_minutes`: REM + NREM duration.
- `sleep_summary.sleep_efficiency_percent`: total sleep time divided by recording time.
- `respiratory_summary.ahi`: `(hypopnea_count + suspected_apnea_count) / sleep_hours`.
- `respiratory_summary.mean_respiratory_rate_bpm`: optional mean respiratory rate.

## `/mock-report`

Response schema: `MockSleepReport`.

Top-level fields:

- `summary`
- `elder_report`
- `professional_report`
- `care_suggestions`
- `medical_disclaimer`
- `generated_at`

`summary` fields:

- `record_id`
- `subject_id`
- `risk_level`
- `total_recording_minutes`
- `total_sleep_minutes`
- `sleep_efficiency_percent`
- `ahi`
- `hypopnea_count`
- `suspected_apnea_count`
- `mean_respiratory_rate_bpm`

Report summary intentionally uses `total_sleep_minutes` as a reader-facing alias for `sleep_summary.total_sleep_time_minutes`.

As of Stage 7, `/mock-report` keeps the same top-level JSON response contract
but the report text is lightly retrieval-augmented by local Stage 7 seed
knowledge. Retrieved context is folded into `elder_report`,
`professional_report`, and `care_suggestions`; no additional response fields are
added.

## `/mock-report/llm`

Response schema: `MockSleepReport`.

This endpoint uses the same mock analysis query parameters and the same response
shape as `/mock-report`. It exists only as an explicit Stage 7 opt-in path for
LLM-backed report generation experiments.

- Default behavior: `use_deepseek=false`, so the endpoint returns the same
  deterministic template/RAG report path as `/mock-report`.
- Live DeepSeek behavior: `use_deepseek=true` calls
  `generate_sleep_report_with_deepseek_fallback()`.
- Missing key, HTTP failure, malformed response, invalid LLM JSON, or unsafe
  LLM draft text all fall back to the deterministic template/RAG report path.
- Ordinary tests and default endpoint calls must not depend on a real
  `DEEPSEEK_API_KEY`.
- Frontend usage is default-off: the Streamlit app requests `/mock-report`
  unless the user explicitly enables the DeepSeek report option, which then
  requests `/mock-report/llm?use_deepseek=true`.

## Stage 8 Agent Orchestration Contract

Stage 8 starts with an internal, deterministic Agent orchestration contract.
The default implementation is a linear Python runner, and an optional LangGraph
state graph boundary mirrors the same nodes and payloads. Tests do not require a
live LLM, LangGraph installation, database, alert service, or real SHHS model
wiring.

Current code:

- `sleepagent.schemas.agent.SleepAgentOrchestrationRequest`
- `sleepagent.schemas.agent.SleepAgentOrchestrationResult`
- `sleepagent.schemas.agent.AgentStepTrace`
- `sleepagent.schemas.agent.DialogueTurn`
- `sleepagent.agents.SleepAnalysisAgent`
- `sleepagent.agents.ReportAgent`
- `sleepagent.agents.DialogueAgent`
- `sleepagent.agents.SleepAgentOrchestrator`
- `sleepagent.agents.run_sleep_agent_orchestration()`
- `sleepagent.agents.build_sleep_agent_langgraph()`
- `sleepagent.agents.run_sleep_agent_langgraph_orchestration()`

Request fields:

- `record_id`
- `subject_id`
- `duration_hours`
- `seed`
- `abnormal_event_rate_per_hour`
- `user_question`, optional
- `dialogue_context`, optional
- `use_deepseek_report`, default `false`
- `use_langgraph`, backend endpoint only, default `false`

Result fields:

- `analysis`: existing `SleepAnalysisResult`
- `report`: existing `MockSleepReport`
- `dialogue`: optional `DialogueTurn`
- `steps`: ordered `AgentStepTrace` list
- `orchestration_mode`: `linear` or `langgraph`
- `generated_at`

Dialogue context fields:

- `history_summary`, optional compressed text summary from previous sleep
  observations or conversation.
- `user_preferences`, optional list of short preference strings.
- `recent_questions`, optional list of recent user questions.

Dialogue turn fields:

- `user_question`
- `assistant_response`
- `safety_flags`
- `referenced_record_id`
- `context_used`

Step trace names:

- `sleep_analysis`
- `report`
- `dialogue`
- `skip_dialogue`

Current Agent behavior:

- Sleep analysis Agent calls the deterministic mock analysis generator.
- Report Agent calls the Stage 7 template/RAG report path by default.
- DeepSeek report generation remains explicit opt-in through
  `use_deepseek_report=true`; default Agent tests make no live API request.
- Dialogue Agent is rule-based and grounded in the current analysis/report.
- Optional `dialogue_context` can influence non-urgent answers by adding a short
  context sentence, but it is not persisted and does not introduce a database or
  long-term memory service in Stage 8.
- Urgent symptom questions such as chest pain, severe breathing difficulty, or
  abnormal consciousness receive a safety-boundary response recommending timely
  medical or emergency evaluation rather than diagnosis.

### LangGraph Boundary

LangGraph remains an optional Agent dependency:

- install extra: `python -m pip install -e ".[agent]"`
- dependency: `langgraph>=0.2`

The LangGraph boundary is lazy-loaded by
`sleepagent.agents.langgraph_orchestration`, so importing `sleepagent.agents`
does not require LangGraph. If the optional dependency is missing,
`LangGraphUnavailableError` is raised only when building or running the
LangGraph path.

Current graph nodes:

1. `sleep_analysis`
2. `report`
3. `dialogue`
4. `build_result`

The graph state is represented by `SleepAgentLangGraphState` and carries the
same structured payloads as the linear runner: request, analysis, report,
optional dialogue, step traces, and final orchestration result. The final result
uses `orchestration_mode="langgraph"` while preserving the existing
`SleepAgentOrchestrationResult` response shape.

### `/agent/orchestrate`

Response schema: `SleepAgentOrchestrationResult`.

This endpoint exposes the Stage 8 Agent orchestration contract without changing
`/mock-analysis`, `/mock-report`, or `/mock-report/llm`. The GET variant is kept
as an MVP smoke endpoint. The POST variant accepts `SleepAgentEndpointRequest`
as a JSON body and is the preferred shape for the formal Agent API.

GET query parameters and POST JSON fields:

- `record_id`
- `subject_id`
- `duration_hours`
- `seed`
- `abnormal_event_rate_per_hour`
- `user_question`, optional
- `dialogue_context`, POST JSON only, optional
- `use_deepseek_report`, default `false`
- `use_langgraph`, default `false`

Default behavior:

- `use_langgraph=false` runs the deterministic linear Agent orchestrator.
- `use_langgraph=true` runs the optional LangGraph path.
- If `use_langgraph=true` but LangGraph is not installed, the endpoint returns
  HTTP `503`.
- `use_deepseek_report=false` preserves deterministic template/RAG report
  generation.
- `use_deepseek_report=true` opts into the guarded DeepSeek report fallback
  path; missing key, HTTP failure, malformed response, invalid LLM JSON, or
  unsafe draft text still fall back to deterministic template/RAG report text.

### Agent Orchestration CLI Smoke

`scripts/run_agent_orchestration_smoke.py` sends a POST request to
`/agent/orchestrate` and validates the response against
`SleepAgentOrchestrationResult`.

Default API base URL:

- `SLEEPAGENT_API_BASE_URL` when set
- otherwise `http://127.0.0.1:18000`

Example:

```bash
python scripts/run_agent_orchestration_smoke.py --api-base-url http://127.0.0.1:18000 --duration-hours 0.5 --seed 54
```

Optional context example:

```bash
python scripts/run_agent_orchestration_smoke.py --history-summary "最近一周睡眠效率下降"
```

The script prints only contract-level metadata:

- endpoint and method
- record and subject identifiers
- risk level
- orchestration mode
- step names
- dialogue presence and safety flags
- report contract field names

It does not print full report or dialogue text.

### Frontend Agent API Helper

`frontend/api_client.py` includes a client boundary for the Agent orchestration
endpoint:

- `build_agent_orchestration_url()`
- `fetch_agent_orchestration()`
- `extract_agent_orchestration_summary()`

`fetch_agent_orchestration()` uses POST and sends the same body fields as
`SleepAgentEndpointRequest`, including optional `dialogue_context`,
`use_deepseek_report`, and `use_langgraph`.

The Streamlit app now includes a small Agent orchestration panel. It reuses the
same mock request controls, adds an Agent question and optional history summary,
and renders the current Agent response plus orchestration metadata. DeepSeek and
LangGraph remain default-off checkboxes.

## Stage 9 Data Management Contract

Stage 9 begins with a persistence boundary for analysis and report snapshots.
The first implementation is local append-only JSONL so tests and demos do not
require a live PostgreSQL server. The boundary is intentionally shaped so a
future PostgreSQL adapter can store the same Pydantic payloads in ordinary
columns plus JSONB.

Current code:

- `sleepagent.schemas.data_management.StoredAnalysisRecord`
- `sleepagent.schemas.data_management.StoredReportRecord`
- `sleepagent.services.data_management.SleepDataRepository`
- `sleepagent.services.data_management.LocalJsonlSleepDataRepository`

Local filenames:

- `analysis_records.jsonl`
- `report_records.jsonl`

`StoredAnalysisRecord` fields:

- `schema_version`: currently `stage9.analysis_record.v1`
- `analysis_id`
- `record_id`
- `subject_id`
- `source_dataset`
- `risk_level`
- `generated_at`
- `stored_at`
- `analysis`: existing `SleepAnalysisResult`

`StoredReportRecord` fields:

- `schema_version`: currently `stage9.report_record.v1`
- `report_id`
- `analysis_id`, optional link to a stored analysis snapshot
- `record_id`
- `subject_id`
- `risk_level`
- `generated_at`
- `stored_at`
- `report`: existing `MockSleepReport`

Consistency rules:

- Search fields must match the embedded analysis/report payload.
- Records are append-only in the local JSONL implementation.
- Listing supports optional `subject_id` and `record_id` filters.
- Latest-record helpers sort by `generated_at`, then `stored_at`.
- Raw SHHS EDF/XML files and derived arrays are not stored in these records.

## Stage 9 Long-Term Memory Contract

Stage 9 now includes deterministic long-term memory compression for stored
analysis/report snapshots. The output is structured for inspection and includes
a Chinese `history_summary` string that can be passed directly into
`DialogueContext.history_summary`.

Current code:

- `sleepagent.schemas.memory.LongTermMemorySummary`
- `sleepagent.services.memory.compress_long_term_memory()`
- `sleepagent.services.memory.compress_memory_from_repository()`
- `sleepagent.services.memory.build_dialogue_context_from_memory()`

`LongTermMemorySummary` fields:

- `schema_version`: currently `stage9.long_term_memory_summary.v1`
- `subject_id`
- `generated_at`
- `source_analysis_ids`
- `source_report_ids`
- `record_ids`
- `record_count`
- `first_record_generated_at`
- `latest_record_generated_at`
- `latest_risk_level`
- `risk_level_counts`
- `average_sleep_efficiency_percent`
- `average_ahi`
- `max_ahi`
- `latest_ahi`
- `latest_sleep_efficiency_percent`
- `history_summary`

Compression rules:

- Input analysis snapshots must belong to one subject.
- Empty analysis history is rejected by `compress_long_term_memory()`.
- `compress_memory_from_repository()` returns `None` when the repository has no
  history for the requested subject.
- The default window is the latest 5 analysis snapshots, sorted by
  `generated_at` and `stored_at`.
- Report snapshots are optional and are linked through `analysis_id` when
  available.
- The generated summary remains assistive context only and explicitly says it
  does not replace medical diagnosis.

## Stage 9 Alert Event Contract

Stage 9 now includes local-only alert event recording for high-risk sleep
findings. This is not a push service yet: no SMS, email, app notification, or
external channel is attempted.

Current code:

- `sleepagent.schemas.alert.AlertEvent`
- `sleepagent.schemas.alert.AlertSeverity`
- `sleepagent.schemas.alert.AlertStatus`
- `sleepagent.schemas.alert.AlertTriggerType`
- `sleepagent.services.alerting.LocalJsonlAlertEventRepository`
- `sleepagent.services.alerting.build_high_risk_alert_event()`
- `sleepagent.services.alerting.record_high_risk_alert_if_needed()`
- `sleepagent.services.alerting.record_high_risk_alert_for_analysis_if_needed()`

Local filename:

- `alert_events.jsonl`

`AlertEvent` fields:

- `schema_version`: currently `stage9.alert_event.v1`
- `alert_id`
- `trigger_type`: currently `high_risk_sleep_finding`
- `severity`: currently `high`
- `status`: currently `local_recorded`
- `subject_id`
- `record_id`
- `source_analysis_id`, optional
- `risk_level`
- `ahi`
- `hypopnea_count`
- `suspected_apnea_count`
- `trigger_reasons`
- `message`
- `created_at`
- `local_recorded_at`
- `push_channels_attempted`: must remain empty in this local-only stage

Trigger rules:

- Only `risk_level=high` analysis records generate an alert event.
- Moderate and low risk records return `None` and do not write an alert.
- Default trigger reason includes `risk_level=high`.
- Additional local reasons are added when AHI is at least 15 or suspected apnea
  count is at least 20.
- Event text explicitly states that the alert was recorded locally and no SMS,
  email, or app push was sent.

## Stage 9 External Tool Mock Context Contract

Stage 9 now includes a deterministic mock external context interface for
weather, temperature, diet, and lifestyle factors. It is a local contract only
and does not call real weather, food diary, phone, or wearable APIs.

Current code:

- `sleepagent.schemas.external_tools.WeatherContext`
- `sleepagent.schemas.external_tools.DietContext`
- `sleepagent.schemas.external_tools.LifestyleContext`
- `sleepagent.schemas.external_tools.ExternalToolContext`
- `sleepagent.services.external_tools.ExternalContextProvider`
- `sleepagent.services.external_tools.MockExternalContextProvider`
- `sleepagent.services.external_tools.build_mock_external_context()`
- `sleepagent.services.external_tools.build_external_context_summary()`

`ExternalToolContext` fields:

- `schema_version`: currently `stage9.external_tool_context.v1`
- `subject_id`
- `location`
- `context_date`
- `source`: currently `mock`
- `weather`
- `diet`
- `lifestyle`
- `summary`
- `generated_at`

Weather context:

- `condition`: `clear`, `cloudy`, `rainy`, `cold`, or `hot`
- `outdoor_temperature_celsius`
- `indoor_temperature_celsius`, optional
- `humidity_percent`
- `note`

Diet context:

- `last_meal_timing`: `early`, `normal`, `late`, or `unknown`
- `caffeine_after_noon`
- `alcohol_near_bedtime`
- `heavy_meal_near_bedtime`
- `note`

Lifestyle context:

- `activity_level`: `low`, `moderate`, or `high`
- `screen_time_before_bed_minutes`
- `nap_minutes`
- `stress_level_0_10`
- `note`

Mock behavior:

- The same `subject_id`, `location`, `context_date`, and `seed` produce the same
  weather/diet/lifestyle payload, excluding `generated_at`.
- The summary is Chinese reader-facing context and explicitly states that it is
  mock data, not real external data.
- The summary can surface simple contextual risk notes such as extreme
  temperature, afternoon caffeine, bedtime alcohol, heavy late meals, long
  screen time, long naps, or high stress.

## Stage 9 Backend Integration Contract

Stage 9 includes a minimal backend integration endpoint that exercises the local
data management, memory, alerting, and external mock context services together.
It remains a mock/local endpoint: it does not use PostgreSQL, real push channels,
or real external APIs.

Endpoint:

- `POST /stage9/mock-context`

Request schema:

- `sleepagent.schemas.stage9.Stage9MockContextRequest`

Response schema:

- `sleepagent.schemas.stage9.Stage9MockContextResult`

Request fields:

- `record_id`
- `subject_id`
- `duration_hours`
- `seed`
- `abnormal_event_rate_per_hour`
- `location`
- `context_date`, optional
- `external_context_seed`
- `max_memory_records`

Response fields:

- `analysis_record`: `StoredAnalysisRecord`
- `report_record`: `StoredReportRecord`
- `memory_summary`: `LongTermMemorySummary`, optional
- `alert_event`: `AlertEvent`, optional
- `external_context`: `ExternalToolContext`
- `local_store_dir`
- `generated_at`

Endpoint behavior:

- Generates deterministic mock analysis and report payloads.
- Saves analysis/report snapshots to the local JSONL repository.
- Compresses the subject's recent stored analysis/report history into memory.
- Records a local alert only if the current analysis is high risk.
- Generates deterministic mock weather, temperature, diet, and lifestyle
  context.
- Uses `SLEEPAGENT_DATA_STORE_DIR` when set, otherwise writes local Stage 9
  API files under `/tmp/sleepagent_stage9_api`.

## Stage 7 Report RAG Knowledge Contract

Stage 7 starts the template-to-RAG upgrade with a minimal, replaceable knowledge
chunk contract. The first implementation is a deterministic in-memory seed
retriever; Chroma indexing, DeepSeek request scaffolding, a guarded live smoke,
and an explicit opt-in LLM endpoint now exist.

Knowledge chunk schema:

- `schema_version`: currently `stage7.report_knowledge_chunk.v1`
- `chunk_id`
- `title`
- `content`
- `source`
- `topic_tags`
- `audience_tags`
- `safety_notes`

Retrieved chunk schema:

- `chunk`: a `ReportKnowledgeChunk`
- `score`: non-negative retrieval score
- `matched_terms`: query terms that matched the chunk

Current code:

- `sleepagent.schemas.report.ReportKnowledgeChunk`
- `sleepagent.schemas.report.RetrievedReportKnowledgeChunk`
- `sleepagent.services.report_knowledge.retrieve_report_knowledge()`
- `sleepagent.services.report_chroma.ChromaReportKnowledgeAdapter`
- `sleepagent.services.report_chroma.seed_default_report_chroma_knowledge()`
- `sleepagent.services.report_retrievers.retrieve_report_context()`
- `sleepagent.services.report_retrievers.build_report_knowledge_retriever()`
- `sleepagent.schemas.report.LLMReportDraft`
- `sleepagent.services.report_llm.validate_llm_report_json()`
- `sleepagent.services.report_llm.generate_sleep_report_with_llm_fallback()`
- `sleepagent.services.report_llm.DeepSeekChatClient`
- `sleepagent.services.report_llm.generate_sleep_report_with_deepseek_fallback()`
- `sleepagent.services.report_llm.build_deepseek_report_request_preview()`
- `scripts/seed_report_chroma.py`
- `scripts/run_deepseek_report_smoke.py`
- `sleepagent.evaluation.evaluate_dialogue_safety_cases()`

The built-in seed chunks are internal scaffolding only. Phase 5 marks them with
`source_type="internal_seed"` and `review_status="reviewed"` to indicate they
have been reviewed for SleepAgent safety-boundary wording, not that they are
clinical guidelines. They preserve the medical safety boundary and Stage 6 demo
caveat, but they are not a reviewed medical knowledge base and must be replaced
or supplemented before clinical-facing use. Chunks marked `review_status="dev_only"`
are filtered out of default user-visible retrieval.

### Chroma Adapter Boundary

The Stage 7 Chroma boundary is optional and dependency-light:

- Chroma is declared under the optional `rag` dependency extra.
- Importing SleepAgent services does not require `chromadb`.
- `ChromaReportKnowledgeAdapter` lazy-loads `chromadb` only when creating a real
  client.
- Tests can inject a fake Chroma-like client via `client=...`.
- `upsert_chunks()` stores `ReportKnowledgeChunk` objects into a Chroma
  collection.
- `query()` returns `RetrievedReportKnowledgeChunk` objects so downstream report
  generation does not depend on Chroma-specific payload shapes.
- `seed_default_report_chroma_knowledge()` and `scripts/seed_report_chroma.py`
  index the built-in seed chunks and run one smoke query.

Default collection:

- `sleepagent_report_knowledge`

Metadata stored in Chroma:

- `schema_version`
- `chunk_id`
- `title`
- `source`
- `source_type`
- `review_status`
- `last_reviewed_at`
- `topic_tags` as JSON text
- `audience_tags` as JSON text
- `safety_notes` as JSON text

Document text:

- `content`

Chroma distance is converted to a non-negative retrieval score using
`1 / (1 + distance)`. This score is a local adapter score only and should not be
mixed with lexical scores without calibration.

The CLI uses `HashEmbeddingFunction` by default. It is deterministic and local,
so it avoids downloading a default embedding model during smoke tests. It is a
development scaffold, not a medically validated retrieval embedding.

### Retriever Selection

Report generation now uses a small retriever selection layer. The default mode
is deterministic in-memory retrieval, preserving existing `/mock-report`
behavior and response shape.

Environment variables:

- `SLEEPAGENT_REPORT_RETRIEVER`
  - `in_memory` / `memory`: deterministic built-in retrieval
  - `chroma`: Chroma-backed retrieval
- `SLEEPAGENT_REPORT_CHROMA_DIR`: local persistent Chroma directory for Chroma
  mode
- `SLEEPAGENT_REPORT_CHROMA_COLLECTION`: optional collection name, default
  `sleepagent_report_knowledge`

The report response contract is unchanged. Retriever choice only changes the
knowledge chunks folded into existing report text fields.

### DeepSeek LLM Draft Contract

Stage 7 now has a DeepSeek API client boundary. The LLM draft schema is
validated before it can be converted into the existing `MockSleepReport`
response contract.

LLM draft schema:

- `schema_version`: exactly `stage7.llm_report_draft.v1`
- `elder_report`: non-empty string
- `professional_report`: non-empty string
- `care_suggestions`: list of non-empty strings
- `safety_warnings`: list of non-empty strings

Validation rules:

- Extra fields are rejected.
- Invalid JSON is rejected.
- Wrong `schema_version` is rejected.
- Draft text is rejected if it contains explicit diagnosis/certainty language,
  discourages medical care, or gives direct medication/self-treatment
  instructions. Cautious disclaimer language remains allowed.
- Valid drafts are converted into the existing report response shape.
- Invalid or missing LLM output falls back to the current template/RAG path.

DeepSeek request preview:

- default model: `deepseek-v4-flash`
- base URL: `https://api.deepseek.com`
- API key env var name: `DEEPSEEK_API_KEY`
- `build_deepseek_report_request_preview()` builds messages and metadata for
  inspection without calling the network.

DeepSeek client boundary:

- `DeepSeekChatClient` calls OpenAI-compatible `POST /chat/completions`.
- Requests include:
  - bearer token from `DEEPSEEK_API_KEY` or an injected API key
  - `response_format={"type": "json_object"}`
  - `thinking={"type": "disabled"}` by default
  - `stream=False`
- Responses are accepted only if the first choice message content validates as
  `LLMReportDraft`.
- Missing API key, HTTP failure, malformed response, or invalid LLM JSON all
  fall back to the current template/RAG path through
  `generate_sleep_report_with_deepseek_fallback()`.
- No live DeepSeek request is run by default in tests.
- `scripts/run_deepseek_report_smoke.py` runs a guarded live smoke when
  `DEEPSEEK_API_KEY` is configured. Without a key it exits with code `2` and
  does not make a network request.

## Metrics

Sleep staging metrics:

- `accuracy`: overall accuracy.
- `cohen_kappa`: Cohen's Kappa, range -1 to 1.
- `macro_f1`: unweighted mean F1 over `Wake`, `REM`, `NREM`.
- `weighted_f1`: support-weighted F1.
- `per_class_f1`: F1 keyed by sleep stage enum value.

Sleep staging edge behavior:

- Empty `y_true` / `y_pred` inputs raise `ValueError`.
- Mismatched `y_true` / `y_pred` lengths raise `ValueError`.
- Empty explicit `labels` raises `ValueError`.
- Per-class F1 is `0.0` when a requested class has no true positives, false positives, or false negatives.
- Macro F1 includes every requested label, including labels with no support.
- Weighted F1 weights by true-label support and returns `0.0` when the requested labels have no support.
- Cohen's Kappa returns `1.0` for a perfect single-class prediction where the expected-agreement denominator is zero.
- `compute_sleep_staging_metrics()` maps raw labels through the configured stage mapping and rejects unknown labels.

Respiratory detection metrics:

- `recall`: abnormal-event recall, merging `hypopnea` and `suspected_apnea` as abnormal.
- `auc`: multiclass one-vs-rest macro AUC when probability scores are available.
- `f1`: macro F1 over `normal_breathing`, `hypopnea`, `suspected_apnea`.
- `per_class_recall`: recall keyed by respiratory event enum value.

Respiratory detection edge behavior:

- Unknown respiratory labels raise `ValueError`.
- Empty `y_true` / `y_pred` inputs raise `ValueError`.
- Mismatched `y_true` / `y_pred` lengths raise `ValueError`.
- Per-class recall is `0.0` when a requested class has no true samples.
- Abnormal-event recall is `0.0` when there are no true `hypopnea` or `suspected_apnea` events.
- Binary AUC raises `ValueError` for empty input, mismatched score lengths, or missing positive/negative classes.
- Multiclass one-vs-rest AUC skips classes that lack positive or negative samples.
- Multiclass one-vs-rest AUC raises `ValueError` when no class has both positive and negative samples.
- Score rows must include a score for every requested respiratory label, using either enum keys or enum-value string keys.
- `compute_respiratory_detection_metrics()` leaves `auc` as `None` when no probability scores are provided.

## Stage 4 Respiratory Model Tensor Contract

The Stage 4 respiratory model skeleton is defined in:

- `sleepagent.models.respiratory_contract`
- `sleepagent.models.respiratory_cnn_bilstm`

Dependency rule:

- PyTorch is an optional model dependency declared as `sleepagent[model]`.
- The tensor contract/config module is importable without PyTorch.
- The actual `RespiratoryCnnBiLstm` module requires PyTorch.

Input tensor:

- Name: `respiratory_window`
- Type: floating-point PyTorch tensor.
- Shape: `(batch_size, input_channels, samples)`.
- Default warm-up assumption: `30.0` second windows at `125.0 Hz`.
- Default channels: `2` generic respiratory channels.
- Default samples: `3750`.
- Stage 5 will decide the real SHHS channel selection and preprocessing window construction.

Output tensor:

- Type: raw logits, not softmax probabilities.
- Shape: `(batch_size, 3)`.
- Class order:
  1. `normal_breathing`
  2. `hypopnea`
  3. `suspected_apnea`

Stage 4 does not build training windows, read EDF signal arrays, train the
model, compute respiratory metrics from model outputs, or wire model inference
into backend schemas.

## Stage 5 Respiratory XML Window Labels And Manifest

The first Stage 5 preprocessing step is defined in:

- `sleepagent.preprocessing.shhs_respiratory_events`
- `sleepagent.preprocessing.shhs_respiratory_signals`

The XML label/window step reads authorized local SHHS XML annotation files only.
The signal window step reads selected channels from authorized local EDF files
and aligns them to the XML-derived labels. Neither step creates NPY/NPZ/Parquet
outputs, trains a model, or splits records into train/validation/test sets yet.

Manifest schema:

- `schema_version`: `stage5.respiratory_windows_manifest.v1`
- `source_xml_path`
- `recording_duration_seconds`
- `window_duration_seconds`
- `stride_seconds`
- `minimum_event_overlap_seconds`
- `normal_exclusion_buffer_seconds`
- `label_conflict_rule`
- `normal_rule`
- `unknown_policy`
- `target_label_counts`
- `ignored_label_counts`
- `unknown_label_counts`
- `included_class_counts`
- `excluded_window_counts`
- `warning_messages`

Signal window manifest schema:

- `schema_version`: `stage5.respiratory_signal_windows_manifest.v1`
- `edf_path`
- `source_xml_path`
- `channel_names`
- `sampling_rate_hz`
- `recording_duration_seconds`
- `total_window_count`
- `included_window_count`
- `excluded_window_count`
- `samples_per_window`
- `included_class_counts`
- `excluded_window_counts`
- `notes`

Derived NPZ dataset manifest schema:

- `schema_version`: `stage5.respiratory_npz_dataset_manifest.v1`
- `dataset_path`
- `edf_path`
- `source_xml_path`
- `channel_names`
- `sampling_rate_hz`
- `samples_per_window`
- `included_only`
- `window_count`
- `class_order`
- `class_counts`
- `arrays`
- `notes`

Dataset split manifest schema:

- `schema_version`: `stage5.respiratory_dataset_split_manifest.v1`
- `split_strategy`
- `seed`
- `split_ratios`
- `dataset_count`
- `split_counts`
- `window_counts_by_split`
- `class_counts_by_split`
- `records`
- `warning_messages`
- `notes`

Default respiratory EDF channels:

- `THOR RES`
- `ABDO RES`

Signal window contract:

- Each extracted window aligns one-to-one with a Stage 5 XML label window.
- Default 30-second windows at 125 Hz produce `3750` samples per channel.
- Signal data are channel-first, matching the model contract shape
  `(input_channels, samples)` for one window.
- Included and excluded label-window flags are preserved in signal windows.
- Signal manifests are JSON-safe summaries and must not embed raw signal arrays.
- Derived NPZ datasets contain:
  - `x`: float array shaped `(window_count, input_channels, samples)`.
  - `y`: int64 class indices.
  - `start_seconds`: float64 window start offsets.
  - `included_mask`: boolean training-inclusion flags.
  - `class_order`: string class labels for decoding `y`.
  - `channel_names`: string EDF channel names.
- The default dataset writer uses `included_only=True`, so excluded candidate
  normal windows are not written to the training NPZ unless explicitly requested.
- Derived NPZ files are local artifacts and must remain outside the code
  repository.
- Split strategy is `record_level_stable_hash` by default. Whole dataset files
  are assigned to train/val/test to reduce adjacent-window leakage.
- Default split ratios are `train=0.70`, `val=0.15`, and `test=0.15`.
- If fewer than three record-level datasets are available, every dataset is
  assigned to `train` and the split manifest records a warning.

Extracted event labels:

- `Hypopnea` and hypopnea subtypes map to `hypopnea`.
- `Obstructive Apnea`, `Central Apnea`, and `Mixed Apnea` map to
  `suspected_apnea`.
- Explicit non-target labels such as SpO2 desaturation, SpO2 artifacts, and
  arousals are skipped when `unknown_policy="ignore"` and counted in
  `ignored_label_counts`.
- Unreviewed labels are skipped when `unknown_policy="ignore"` and counted in
  `unknown_label_counts`; the manifest includes a warning message when any are
  present.
- `unknown_policy="raise"` stops preprocessing on the first skipped or unknown
  XML label.

Window labeling contract:

- Default window duration: `30.0` seconds.
- Default stride: `30.0` seconds.
- Default minimum event/window overlap: `1.0` second.
- Default normal exclusion buffer: `30.0` seconds.
- A full fixed-duration window is generated while
  `window_start + window_duration <= recording_duration_seconds`.
- If a window has enough abnormal overlap, it is labeled as `hypopnea` or
  `suspected_apnea`.
- If both abnormal classes overlap the same window, the class with larger total
  overlap wins; exact ties prefer `suspected_apnea`.
- `label_conflict_rule` is
  `largest_abnormal_overlap_seconds_tie_suspected_apnea`.
- A candidate normal window is included only when it has no abnormal overlap
  reaching the threshold and is farther than `normal_exclusion_buffer_seconds`
  from every mapped abnormal event.
- Candidate normal windows inside the abnormal-event buffer keep the provisional
  label `normal_breathing`, but are excluded from training with
  `exclusion_reason="near_abnormal_event"`.
- `normal_rule` is
  `no_abnormal_overlap_and_outside_abnormal_event_buffer`.
- Recording duration is inferred from `SleepStages/SleepStage` count times
  `EpochLength` when available; otherwise callers can pass
  `recording_duration_seconds`.

Real local smoke result on authorized sample annotation:

- XML:
  `../data/raw/shhs_sample/polysomnography/annotations-events-profusion/shhs1/shhs1-200001-profusion.xml`
- Generated windows: `1084`
- Recording duration: `32520.0` seconds.
- Mapped respiratory events: `87`.
- Ignored non-target scored events: `283`.
- Total candidate windows: `1084`
- Included training windows: `1015`
- Excluded candidate normal windows: `69`
- Included class distribution:
  - `normal_breathing`: `877`
  - `hypopnea`: `135`
  - `suspected_apnea`: `3`
- Excluded window counts:
  - `near_abnormal_event`: `69`
- Target label counts:
  - `hypopnea`: `85`
  - `suspected_apnea`: `2`
- Ignored label counts:
  - `Arousal ()`: `183`
  - `SpO2 artifact`: `29`
  - `SpO2 desaturation`: `71`
- Unknown label counts: none.

Real local signal-window smoke result on authorized sample:

- EDF:
  `../data/raw/shhs_sample/polysomnography/edfs/shhs1/shhs1-200001.edf`
- XML:
  `../data/raw/shhs_sample/polysomnography/annotations-events-profusion/shhs1/shhs1-200001-profusion.xml`
- Channels: `THOR RES`, `ABDO RES`
- Sampling rate: `125.0 Hz`
- Samples per 30-second window: `3750`
- Smoke limit: first `5` windows only
- Extracted window data shape: `(2, 3750)` for each smoke window
- Smoke manifest schema: `stage5.respiratory_signal_windows_manifest.v1`

Real local derived NPZ dataset smoke result on authorized sample:

- Output:
  `../data/processed/sleepagent/stage5/shhs1-200001_resp_windows_included.npz`
- Dataset manifest schema: `stage5.respiratory_npz_dataset_manifest.v1`
- `included_only`: `true`
- `x` shape: `(1015, 2, 3750)`
- `y` shape: `(1015,)`
- Class order:
  - `normal_breathing`
  - `hypopnea`
  - `suspected_apnea`
- Class counts:
  - `normal_breathing`: `877`
  - `hypopnea`: `135`
  - `suspected_apnea`: `3`
- Local file size: about `27M`.

Real local split manifest smoke result on authorized sample:

- Split manifest schema: `stage5.respiratory_dataset_split_manifest.v1`
- Split strategy: `record_level_stable_hash`
- Dataset count: `1`
- Split counts:
  - `train`: `1`
  - `val`: `0`
  - `test`: `0`
- Window counts by split:
  - `train`: `1015`
  - `val`: `0`
  - `test`: `0`
- Warning: fewer than three record-level datasets are available, so validation
  and test splits are empty for this smoke dataset.

## Stage 6 Respiratory NPZ Dataset Loader

The first Stage 6 training utility is defined in:

- `sleepagent.training.respiratory_dataset`

Loader contract:

- `load_respiratory_npz_arrays()` reads the Stage 5 derived NPZ format.
- Required arrays are `x`, `y`, and `class_order`.
- Optional arrays are `start_seconds`, `included_mask`, and `channel_names`.
- `x` is converted to `float32` and must have shape
  `(window_count, input_channels, samples)`.
- `y` is converted to `int64` and must have shape `(window_count,)`.
- `class_order` must match the active `RespiratoryCnnBiLstmConfig`.
- Label indices in `y` must be valid indices into `class_order`.
- Default model config expects two channels and `3750` samples per 30-second
  window at 125 Hz.

Torch dataset contract:

- `RespiratoryNpzTorchDataset` wraps one NPZ file and is compatible with the
  PyTorch Dataset protocol.
- `included_mask` is currently validated and retained for diagnostics, but it
  does not filter samples in `RespiratoryNpzTorchDataset`; Stage 5 writers
  already default to `included_only=True` for training NPZ files.
- Importing `sleepagent.training` does not require PyTorch.
- Reading an item requires PyTorch and returns:
  - `window`: `torch.float32` tensor shaped `(input_channels, samples)`.
  - `label`: scalar `torch.long` tensor.
- The class exposes decoded `class_order` and `class_counts` for training
  diagnostics.

Training smoke contract:

- `train_respiratory_single_epoch_smoke()` runs one supervised training epoch
  over a Stage 5 NPZ dataset.
- It uses `RespiratoryNpzTorchDataset`, `RespiratoryCnnBiLstm`,
  `torch.optim.Adam`, and `torch.nn.CrossEntropyLoss`.
- It is intended only as a Stage 6 smoke path, not a final experiment runner.
- It returns `RespiratoryTrainingSmokeResult` with:
  - `epoch_count`
  - `batch_count`
  - `example_count`
  - `initial_loss`
  - `final_loss`
  - `mean_loss`
  - string-keyed `class_counts`
- The loop raises if no batches are processed or if loss is non-finite.

Evaluation helper contract:

- `evaluate_respiratory_model_outputs()` converts model outputs into existing
  `RespiratoryDetectionMetrics`.
- Inputs:
  - `y_true`: one-dimensional label-index sequence.
  - `outputs`: two-dimensional `(examples, classes)` logits or probability
    scores.
  - `class_order`: class-index decoding order, defaulting to the respiratory
    model contract order.
  - `from_logits`: when true, outputs are converted with a numerically stable
    softmax before scoring.
- The helper accepts NumPy-like arrays, Python sequences, and torch tensors.
  Torch tensors are detached and moved to CPU before NumPy conversion.
- Predictions are produced with `argmax` over the score rows.
- Score rows are converted to string-keyed maps and passed into
  `compute_respiratory_detection_metrics()` so Recall, AUC, F1, and
  per-class Recall follow the existing metric definitions.
- Probability-mode scores must be finite and non-negative.
- If AUC cannot be computed because the evaluated labels lack positive/negative
  support, metrics are still returned with `auc=None` and
  `auc_warning_message` records the skipped AUC reason.

Inference helper contract:

- `infer_respiratory_window()` runs an already constructed model on one
  channel-first window shaped `(input_channels, samples)`.
- `infer_respiratory_npz()` runs an already constructed model over every window
  in a Stage 5 respiratory NPZ dataset.
- These helpers do not load checkpoints; callers are responsible for providing
  the model instance.
- If no explicit config is provided, helpers use `model.config` when it is a
  `RespiratoryCnnBiLstmConfig`, otherwise they fall back to the default
  respiratory model config.
- Both helpers run the model under `torch.no_grad()` and temporarily switch the
  model to eval mode, restoring training mode afterward when needed.
- Outputs are decoded with softmax and argmax.
- `RespiratoryPrediction` contains:
  - optional `start_second`
  - `predicted_label`
  - string-keyed class `probabilities`
- `RespiratoryInferenceResult` contains:
  - tuple of predictions
  - class order
  - optional source NPZ path

Checkpoint contract:

- `save_respiratory_checkpoint()` writes a PyTorch checkpoint ending in `.pt`
  or `.pth`.
- `load_respiratory_checkpoint()` rebuilds a `RespiratoryCnnBiLstm` from the
  saved config and restores the saved `state_dict`.
- Checkpoint schema version is `stage6.respiratory_checkpoint.v1`.
- Checkpoint payload contains:
  - `schema_version`
  - model `config`
  - caller-provided `metadata`
  - `state_dict`
- Checkpoints are derived model artifacts and should be written outside the code
  repository for real experiments, for example under `../data/processed/`.

Stage 6 smoke CLI contract:

- `scripts/run_respiratory_stage6_smoke.py` runs a tiny train/evaluate/infer
  smoke pipeline.
- With no `--dataset-path`, the script writes a small synthetic NPZ under
  `/tmp/sleepagent_stage6_cli_smoke.npz`.
- The script constructs one model, trains it for one tiny epoch, saves a
  checkpoint, loads that checkpoint, evaluates logits with
  `evaluate_respiratory_model_outputs()`, and runs `infer_respiratory_npz()` on
  the loaded model.
- It prints JSON with schema version `stage6.respiratory_smoke.v1`, including:
  - checkpoint path, schema version, config, and metadata
  - training loss summary
  - evaluation metrics
  - inference prediction count
  - first predictions with `start_second`, predicted label, and probabilities

Stage 6 20-record experiment contract:

- `scripts/run_respiratory_stage6_experiment.py` runs the local SHHS respiratory
  demo experiment.
- The script is split-friendly:
  - `--prepare-only` builds per-record NPZ datasets with an MNE-capable Python.
  - `--train-only` trains/evaluates/infers with a PyTorch-capable Python.
  - `--context-only` rebuilds the report/Agent context JSON from an existing
    experiment summary.
- It requires exactly 20 complete SHHS records for the demo split.
- Fixed record-level split counts are:
  - `train`: `14`
  - `val`: `3`
  - `test`: `3`
- Split manifest schema: `stage6.respiratory_20_record_split.v1`.
- Experiment summary schema:
  `stage6.respiratory_20_record_experiment.v1`.
- Report/Agent context schema:
  `stage6.respiratory_20_record_report_agent_context.v1`.
- Default local artifact paths:
  - NPZ datasets:
    `../data/processed/sleepagent/stage5/resp20/`
  - split manifest:
    `../data/processed/sleepagent/stage5/resp20/resp20_split_manifest.json`
  - best checkpoint:
    `../data/processed/sleepagent/stage6/resp20/best_resp20_checkpoint.pt`
  - experiment summary:
    `../data/processed/sleepagent/stage6/resp20/resp20_experiment_summary.json`
  - report/Agent context:
    `../data/processed/sleepagent/stage6/resp20/resp20_report_agent_context.json`
- The experiment summary contains:
  - dataset/output directories
  - exact split records
  - epoch history
  - best epoch and checkpoint path
  - validation metrics
  - test metrics
  - respiratory model gate status and failed/passed reasons
  - per-test-record class counts, metrics, and first predictions
- The report/Agent context is the downstream-facing artifact. It repeats the
  validation/test metrics and per-record summaries, then adds explicit guidance
  on how reports and Agents may use the demo checkpoint.
- Phase 5 adds model gate fields to the report/Agent context:
  - `respiratory_model_status` is
    `not_validated_for_risk_conclusion` until the checkpoint passes abnormal
    recall, abnormal F1, AUC, fixed split, external holdout, and
    all-normal-collapse checks.
  - `respiratory_model_gate` records the evaluated thresholds and concrete
    failed reasons. Reports and Agents must not use a failed checkpoint as
    negative clinical evidence.
- Real local demo run on `shhs1-200001` through `shhs1-200020`:
  - epochs requested: `5`
  - best epoch: `1`
  - validation Recall: `0.0`
  - validation AUC: `0.599717368951047`
  - validation F1: `0.22117152613606514`
  - test Recall: `0.0`
  - test AUC: `0.5442503554085457`
  - test F1: `0.28623256395821606`
  - test predicted class counts: `normal_breathing=3041`
- Report/Agent caveat: the real demo checkpoint predicted
  `normal_breathing` for every test window, so it must not be presented as
  evidence that respiratory abnormality is absent. It is suitable for pipeline
  demonstration and downstream formatting, not clinical screening or
  performance benchmarking.

## Stage 1 Audit Notes

- Current field names are stable enough for MVP mock integration.
- No schema-wide field rename is required before Stage 2.
- Python 3.10 compatibility requires `datetime.timezone.utc`, not `datetime.UTC`.
- Contract tests in `tests/test_mock_json_contracts.py` lock the JSON shape without relying on FastAPI `TestClient`.
- Stage 1 is complete as of Task 0015.

## Stage 2 SHHS Local Data Conventions

These conventions describe where authorized SHHS data should live locally. Raw
SHHS files must not be committed to the project.

Full local setup instructions are in `docs/SHHS_LOCAL_DATA.md`.

Local data layout:

- `sleepagent/` is the project code repository.
- `../data/` is a local-only sibling directory and should not be committed.
- Recommended raw archive path: `../data/raw/shhs.zip`.
- Future full extraction directory: `../data/raw/shhs/`.
- Future smoke-test sample directory: `../data/raw/shhs_sample/`.
- Future preprocessing outputs: `../data/processed/sleepagent/`.
- Future manifests: `../data/manifests/`.

Data safety rules:

- SHHS zip archives, EDF files, XML annotations, NPY files, NPZ files, Parquet
  files, and derived preprocessing outputs must not be committed.
- Current Stage 2 work must not fully extract the SHHS zip.
- Current Stage 2 work must not read EDF signal contents.
- Future smoke tests should extract only 1-3 matched XML/EDF sample records into
  `../data/raw/shhs_sample/`.

Safe zip inspection:

- `scripts/check_shhs_zip.py` reads `SLEEPAGENT_SHHS_ZIP` or an explicit command
  argument.
- It lists only a small number of `.edf` and `.xml` zip member names.
- It does not extract files and does not read EDF/XML payload contents.

Local root:

- Set `SLEEPAGENT_SHHS_ROOT` to the local SHHS dataset root, or pass a root path
  directly to preprocessing helpers.
- The root is expected to be the `shhs/` directory produced by NSRR downloads,
  or an equivalent directory containing `polysomnography/`.

Expected file layout:

- EDF signal files:
  `polysomnography/edfs/{visit}/{record_id}.edf`
- NSRR XML annotations:
  `polysomnography/annotations-events-nsrr/{visit}/{record_id}-nsrr.xml`
- Profusion XML annotations:
  `polysomnography/annotations-events-profusion/{visit}/{record_id}-profusion.xml`

Record identifiers:

- `visit` is `shhs1` or `shhs2`.
- `record_id` uses the official file stem, for example `shhs1-200001`.
- Bare numeric ids such as `200001` are accepted only when a visit is supplied.

Current Stage 2 preprocessing output:

- `sleepagent.preprocessing.shhs_paths` resolves local paths and discovers record
  manifests by filename only.
- It does not read EDF/XML contents and does not export raw SHHS data.
- A minimally usable record for later preprocessing is expected to have at least
  an EDF file and an NSRR XML annotation file.
- `sleepagent.preprocessing.shhs_annotations.inspect_shhs_annotation_xml()` reads
  authorized local XML annotation files and returns lightweight metadata:
  - root tag
  - epoch length
  - scored event count
  - event type/name counts
  - signal/input counts
  - sleep stage counts when a `SleepStages/SleepStage` section is present
- XML inspection does not read EDF signal contents and does not map labels into
  final model targets yet.
- `sleepagent.preprocessing.shhs_label_mapping` maps inspected XML vocabularies
  into existing MVP enums without creating training data:
  - `map_shhs_xml_sleep_label()` maps SHHS stage labels/codes into
    `Wake / REM / NREM`.
  - SHHS numeric sleep stage mapping is `0 -> Wake`, `1/2/3/4 -> NREM`,
    `5 -> REM`.
  - NSRR event concepts such as `Wake|0`, `Stage 2 sleep|2`, and
    `REM sleep|5` are supported.
  - Profusion `SleepStages/SleepStage` numeric values are supported.
  - `map_shhs_respiratory_event_label()` maps `Hypopnea` and hypopnea
    subtypes to `hypopnea`.
  - `Obstructive Apnea`, `Central Apnea`, and `Mixed Apnea` map to
    `suspected_apnea`.
  - Non-target XML labels such as arousals, SpO2 artifacts/desaturations,
    recording start time, limb movement, and sleep stage labels can be skipped
    with `unknown_policy="ignore"`.
  - Unknown labels raise by default so new SHHS vocabularies are reviewed
    before they affect preprocessing.
- `sleepagent.preprocessing.shhs_summary.build_shhs_preprocessing_summary()`
  creates a tiny XML-derived Stage 2 preprocessing summary for one local sample
  record:
  - record id and visit
  - local root path
  - EDF path and existence flag only
  - NSRR and optional Profusion XML metadata summaries
  - mapped sleep stage counts
  - mapped respiratory event counts
  - notes that the summary is metadata-only
- Summary generation checks whether the EDF path exists but does not read EDF
  signal contents.
- Summary generation does not create epochs, windows, train/test splits, model
  inputs, or derived arrays.
- Minimal Stage 2 preprocessing manifest schema:
  - `schema_version`: currently `stage2.preprocess_manifest.v1`.
  - `generated_at`: timezone-aware UTC ISO timestamp.
  - `source_root`: local sample root path.
  - `record_count`: number of record summaries.
  - `records`: list of XML-derived record summaries.
  - `safety_notes`: explicit notes that raw data must not be committed, EDF
    contents are not read, and no model inputs or derived arrays are included.
- The manifest can be printed locally with:
  `python scripts/summarize_shhs_sample.py --root ../data/raw/shhs_sample --record-id shhs1-200001 --manifest`
- The manifest can be written explicitly with:
  `python scripts/summarize_shhs_sample.py --root ../data/raw/shhs_sample --record-id shhs1-200001 --manifest --write-manifest --manifest-dir ../data/manifests`
- Manifest writing is opt-in; generated manifests are local smoke-test artifacts
  and should remain outside Git unless sanitized and reviewed.
- Manifest validation:
  - `validate_shhs_preprocessing_manifest_payload()` validates an in-memory JSON
    object.
  - `validate_shhs_preprocessing_manifest_file()` validates a local JSON file.
  - Checks include schema version, required top-level fields, required record
    fields, required annotation fields when annotations are present,
    `record_count == len(records)`, and required safety note language.
  - Validation does not read raw EDF/XML files.
  - CLI validation:
    `python scripts/summarize_shhs_sample.py --validate-manifest ../data/manifests/<manifest>.json`
