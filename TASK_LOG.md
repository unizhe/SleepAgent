# TASK_LOG

This file is the compact handoff summary for Stage 10. Detailed task history,
verification commands, experiment metrics, and long-form development notes are
archived in `docs/CHANGELOG.md`.

## Current Stage

- Current stage: Stage 10 in progress.
- Previous stage: Stage 9 completed for the current MVP scope.
- Stage 10 focus: final integration, Docker, README, demo scripts, and
  paper/defense materials.
- Stage 10 React v4 product experience pass completed: Next frontend now presents
  task composer, Plan/Act/Result Agent Run, report generator, chat-style QA,
  care plan, sleep record library, and role-specific content using mock data
  only. No FastAPI contract, real PDF, or notification service changes.
- Stage 10 React v5 priority pass completed: filled remaining P1/P2 product
  gaps from `SleepAgent_v5_optimization_goal.md`, including richer historical
  trends, respiratory event markers, hypnogram, folded raw data CSV export,
  report markdown/CSV exports, medical evidence, model insight cards, developer
  mode, and sidebar product context.
- Corrected Stage 10 v5 task-thread pass completed after reading the actual v5
  goal: React mock frontend now models TaskStatus, SleepAgentTask, AgentEvent,
  ToolCall, Artifact, NextAction, Ask-Before-Act plan confirmation, event-stream
  Agent Run, editable Artifact workspace, Chat-triggered operations, task
  history, metric explanation drawer, and care-plan confirmation flow aligned
  with future SSE/WebSocket APIs.
- RealSleepAgent v1 Phase 1 completed: added real `AnalysisService` with SHHS
  path resolution, EDF header quality check, YASA staging adapter call,
  XML-derived respiratory annotation summary, conservative risk assessment,
  structured node caveats, `/analysis/run`, and `SleepAnalysisAgent`
  `analysis_mode="real"` support. Respiratory model inference remains gated as
  `respiratory_model_unvalidated` / `pipeline_demo_only`.
- RealSleepAgent v1 Phase 2 completed: added backend task schemas,
  append-only local task/event repository, Phase 2 task state graph nodes,
  `/tasks` create/recover endpoints, `/tasks/{task_id}/confirm`, task event and
  artifact retrieval, structured failure events, and focused tests for create,
  confirm, failure, and recovery. SSE and Next.js migration remain Phase 3.
- RealSleepAgent v1 Phase 3 completed: added
  `/tasks/{task_id}/events/stream` SSE history/tail endpoint, local Next.js CORS
  defaults, backend task API client in `frontend/lib/api.ts`, default backend
  task creation/confirmation flow in Next.js, SSE event subscription, task
  snapshot refresh via `GET /tasks/{task_id}`, page reload recovery through
  `localStorage`, and `NEXT_PUBLIC_SLEEPAGENT_MOCK_MODE=true` fallback for
  mock-only frontend development.
- RealSleepAgent v1 Phase 4 completed: added backend ArtifactRepository and
  ArtifactService for persisted create/get/revise/version/export flows,
  confirmation-gated doctor/care-plan exports, PostgreSQL repository adapters
  and repeatable schema initialization for tasks, events, artifacts, versions,
  analysis, reports, memory, alerts, and external contexts, JSONL fallback
  adapters for tests/local demos, task-run persistence of artifacts and memory,
  DialogueAgent memory-context retrieval, and Next.js Artifact workspace
  revision/export/confirmation API integration.
- RealSleepAgent v1 Phase 5 completed: added a shared safety checker for report
  artifacts, dialogue answers, and RAG source governance; persisted artifact
  versions now store safety review status and blocked reasons, and unsafe
  current versions cannot be exported. RAG chunks now include source type,
  review status, and last reviewed timestamp with dev-only filtering. Respiratory
  model gate logic records `not_validated_for_risk_conclusion` until recall,
  F1, AUC, fixed split, external holdout, and all-normal-collapse gates pass;
  RiskAssessmentNode and Stage 6 experiment/report-Agent context now expose that
  status. Added `sleepagent.evaluation` helpers for multi-turn safety and caveat
  consistency checks.
- RealSleepAgent v1 Phase 6 completed: unified the main deployment path on
  FastAPI `127.0.0.1:18000` plus Next.js `127.0.0.1:18510`; Stage 10 demo
  commands now print `cd frontend && npm run dev`; default Docker Compose now
  builds a separate Next.js frontend image; Streamlit is retained only as a
  legacy/debug tool.
- Phase 6 verification completed: targeted Stage 10 deployment tests passed,
  `docker compose -f compose.yaml config` and SHHS override config passed,
  `npm run typecheck` and `npm run build` passed, and a temporary
  `npm run dev` server returned `HTTP/1.1 200 OK` on
  `http://127.0.0.1:18510`.
- Project root: `sleepagent/`.
- Do not modify `../yasa/` for SleepAgent project work unless explicitly
  requested.
- Raw SHHS data, EDF/XML files, NPZ files, checkpoints, and derived local outputs
  must stay outside the code repository.
- Medical output must remain assistive and cautious. Do not phrase reports,
  dialogue, or alerts as diagnosis or treatment.

## Stage Handoff Summary

### Stage 0: Project Skeleton And Mock Frontend/Backend Loop

Status: completed.

Scope:

- Built the initial SleepAgent project skeleton.
- Built the original FastAPI backend and Streamlit prototype frontend, package
  layout, basic tests, and task logging. Streamlit is now legacy/debug only.
- Established a mock end-to-end loop before using real SHHS data or model
  outputs.

Tools and libraries:

- FastAPI
- Streamlit (legacy/debug only in the current v1 deployment)
- Pydantic
- pytest
- uvicorn

Interfaces and entrypoints:

- `GET /health`
- `GET /mock-analysis`
- `GET /mock-report`
- `frontend/app.py` legacy/debug prototype
- `frontend/api_client.py`
- `sleepagent.preprocessing.generate_mock_sleep_analysis()`
- `sleepagent.services.generate_mock_sleep_report()`

Key outputs:

- A runnable backend/frontend MVP skeleton; the current main frontend path is
  Next.js.
- Deterministic mock analysis and report payloads.
- Baseline tests for health, mock analysis, mock report, schemas, and frontend
  API helpers.

Future optimization direction:

- Replace the mock-only loop with a production-style service composition:
  configuration management, dependency injection, deployment settings, proper
  observability, and real analysis backends.

### Stage 1: Data Structures, Mock Data, And Metrics Standardization

Status: completed.

Scope:

- Standardized the MVP JSON contracts for sleep analysis, reports, metrics,
  enums, durations, rates, percentages, and UTC datetimes.
- Locked contract behavior before real SHHS preprocessing and model integration.

Tools and libraries:

- Pydantic strict schemas with `extra="forbid"`
- pytest
- Python datetime with timezone-aware UTC

Interfaces and contracts:

- `sleepagent.schemas.sleep.SleepAnalysisResult`
- `SleepRecordMetadata`, `PatientProfile`, `SleepEpoch`
- `RespiratoryEvent`, `RespiratoryTrendPoint`
- `SleepStageSummary`, `RespiratorySummary`
- `SleepStagingMetrics`, `RespiratoryDetectionMetrics`
- `sleepagent.schemas.report.MockSleepReport`
- `ReportSummary`
- `docs/DATA_CONTRACTS.md`

Key outputs:

- Stable mock analysis/report JSON shapes.
- Standard enum values:
  `Wake`, `REM`, `NREM`, `normal_breathing`, `hypopnea`,
  `suspected_apnea`, `low`, `moderate`, `high`.
- Unit conventions for seconds, minutes, Hz, bpm, percent, and 0-1 metrics.

Future optimization direction:

- Add OpenAPI examples and versioned schema migration policy.
- Split clinical, model, and frontend-facing schemas if the product surface
  grows.
- Add schema snapshot tests against generated JSON fixtures.

### Stage 2: SHHS Data Understanding And Preprocessing Pipeline

Status: completed.

Scope:

- Documented SHHS local data safety rules and local path conventions.
- Inspected authorized local SHHS XML metadata without committing raw data.
- Mapped SHHS sleep and respiratory labels into MVP enums.
- Built metadata-only preprocessing summaries and manifests.

Tools and libraries:

- Python standard XML parsing
- Local filesystem path helpers
- pytest
- SHHS/NSRR local dataset conventions

Interfaces and scripts:

- `sleepagent.preprocessing.shhs_paths`
- `sleepagent.preprocessing.shhs_annotations.inspect_shhs_annotation_xml()`
- `sleepagent.preprocessing.shhs_label_mapping`
- `sleepagent.preprocessing.shhs_summary.build_shhs_preprocessing_summary()`
- `scripts/check_shhs_zip.py`
- `scripts/inspect_shhs_annotation.py`
- `scripts/summarize_shhs_sample.py`
- Environment convention: `SLEEPAGENT_SHHS_ROOT`, `SLEEPAGENT_SHHS_ZIP`

Key outputs:

- Local-only `../data/` layout documentation.
- SHHS EDF/XML path resolver and record discovery helpers.
- Sleep label mapping to Wake/REM/NREM.
- Respiratory label mapping to normal breathing/hypopnea/suspected apnea.
- Stage 2 preprocessing manifest contract:
  `stage2.preprocess_manifest.v1`.

Future optimization direction:

- Replace ad hoc local scripts with a formal data ingestion pipeline such as
  Prefect, Dagster, Airflow, or a Make/DVC workflow.
- Add checksum-based provenance, manifest lineage, and stricter privacy review
  for any derived outputs.
- Expand EDF metadata inspection while keeping raw data out of Git.

### Stage 3: YASA Sleep Staging Reproduction And Integration

Status: completed.

Scope:

- Reproduced YASA sleep staging on authorized local SHHS samples.
- Adapted YASA outputs into SleepAgent `SleepEpoch` and summary schemas.
- Evaluated Wake/REM/NREM predictions against SHHS annotations.

Tools and libraries:

- YASA
- EDF signal inspection helpers
- scikit-learn style classification metrics
- pytest

Interfaces and scripts:

- `sleepagent.models.yasa_staging`
- `sleepagent.services.yasa_runner.run_yasa_sleep_staging()`
- `sleepagent.services.yasa_evaluation.evaluate_yasa_summary_against_shhs_xml()`
- `sleepagent.services.yasa_batch_analysis`
- `sleepagent.services.yasa_channel_comparison`
- `sleepagent.services.yasa_confusion_analysis`
- `scripts/run_yasa_sleep_staging_sample.py`
- `scripts/run_yasa_sleep_staging_batch_from_zip.py`
- `scripts/evaluate_yasa_staging_against_shhs_xml.py`
- `scripts/analyze_yasa_batch_metrics.py`
- `scripts/compare_yasa_channel_evaluations.py`
- `scripts/analyze_yasa_shhs_confusion.py`

Key outputs:

- YASA-to-SleepAgent adapter.
- Accuracy, Cohen's Kappa, macro F1, weighted F1, and per-class F1 reporting.
- Batch and channel comparison helpers.
- Confusion analysis for Wake/REM/NREM.

Future optimization direction:

- Move from local reproduction scripts to a tracked evaluation suite with fixed
  subject splits, confidence intervals, and model cards.
- Compare YASA with other mature sleep staging toolchains or fine-tuned models.
- Add channel quality checks and automatic channel fallback.

### Stage 4: 1D-CNN + BiLSTM Respiratory Model Skeleton

Status: completed.

Scope:

- Built the respiratory event model skeleton.
- Defined tensor shape, class order, input window assumptions, and forward smoke
  behavior.

Tools and libraries:

- PyTorch optional dependency
- Pydantic/Python config contracts
- pytest

Interfaces and contracts:

- `sleepagent.models.respiratory_contract`
- `sleepagent.models.respiratory_cnn_bilstm.RespiratoryCnnBiLstm`
- Input tensor shape: `(batch_size, 2, 3750)`
- Output logits shape: `(batch_size, 3)`
- Class order:
  `normal_breathing`, `hypopnea`, `suspected_apnea`

Key outputs:

- PyTorch-free respiratory tensor/config contract.
- PyTorch model skeleton with forward pass tests.
- Clear separation between contract definition and real training.

Future optimization direction:

- Replace the simple skeleton with a stronger respiratory model search workflow:
  CNN/TCN/Transformer baselines, calibration, class imbalance strategies, and
  robust ablation tracking.
- Add ONNX/export path if backend inference is required in deployment.

### Stage 5: Real SHHS Respiratory Event Training Data Construction

Status: completed.

Scope:

- Built real SHHS respiratory training labels and signal windows from authorized
  local annotation/signal files.
- Aligned 30-second windows to normal breathing, hypopnea, and suspected apnea.
- Wrote local NPZ datasets and split manifests outside the repository.

Tools and libraries:

- SHHS XML annotations
- EDF respiratory channels
- NumPy NPZ
- pytest

Interfaces and scripts:

- `sleepagent.preprocessing.shhs_respiratory_events`
- `sleepagent.preprocessing.shhs_respiratory_signals`
- `stage5.respiratory_windows_manifest.v1`
- `stage5.respiratory_signal_windows_manifest.v1`
- `stage5.respiratory_npz_dataset_manifest.v1`
- `stage5.respiratory_dataset_split_manifest.v1`

Key outputs:

- XML respiratory event extraction.
- Window labeling rules:
  30-second window, abnormal overlap threshold, normal exclusion buffer, and
  apnea/hypopnea conflict behavior.
- EDF signal window extraction for `THOR RES` and `ABDO RES`.
- Local single-record NPZ smoke dataset and split manifest.

Future optimization direction:

- Use mature data/versioning tools such as DVC, LakeFS, MLflow artifacts, or
  a feature store for derived windows.
- Add larger subject-level splits, leakage checks, balancing strategies, and
  dataset cards.
- Validate additional respiratory channels and sampling-rate normalization.

### Stage 6: 1D-CNN + BiLSTM Training, Evaluation, And Inference

Status: completed.

Scope:

- Added training, evaluation, checkpoint, and inference plumbing for the Stage 4
  respiratory model over Stage 5 NPZ windows.
- Ran bounded smoke/demo experiments to validate the pipeline.
- Produced downstream context caveats for reports and Agents.

Tools and libraries:

- PyTorch
- NumPy
- pytest
- Existing respiratory metrics helpers

Interfaces and scripts:

- `sleepagent.training.respiratory_dataset`
- `sleepagent.training.respiratory_training`
- `sleepagent.training.respiratory_evaluation`
- `sleepagent.training.respiratory_inference`
- `sleepagent.training.respiratory_checkpoint`
- `scripts/run_respiratory_stage6_smoke.py`
- `scripts/run_respiratory_stage6_experiment.py`

Key outputs:

- NPZ dataset loader and Dataset-compatible wrapper.
- One-epoch supervised training smoke helper.
- Evaluation helper for Recall, AUC, F1, and per-class Recall.
- Inference helpers for one window and one NPZ dataset.
- Checkpoint save/load helpers.
- 20-record demo result archived in `docs/CHANGELOG.md`.

Future optimization direction:

- Replace demo-scale training with a reproducible experiment stack:
  MLflow/W&B, hydra/omegaconf configs, deterministic splits, early stopping,
  calibration, threshold tuning, and confidence intervals.
- Improve model performance before using predictions clinically; the Stage 6
  demo caveat must remain visible until validated.

### Stage 7: Report Generation Upgrade From Template To RAG

Status: completed.

Scope:

- Upgraded deterministic report generation from fixed templates to a local RAG
  boundary.
- Added optional Chroma adapter and guarded DeepSeek report generation path.
- Preserved elder-friendly and professional report versions with medical safety
  language.

Tools and libraries:

- In-memory retrieval
- Optional ChromaDB
- HTTPX for DeepSeek client boundary
- Pydantic LLM draft validation
- pytest

Interfaces and endpoints:

- `sleepagent.services.report_knowledge.retrieve_report_knowledge()`
- `sleepagent.services.report_chroma.ChromaReportKnowledgeAdapter`
- `sleepagent.services.report_retrievers`
- `sleepagent.services.report_llm`
- `sleepagent.schemas.report.ReportKnowledgeChunk`
- `sleepagent.schemas.report.RetrievedReportKnowledgeChunk`
- `sleepagent.schemas.report.LLMReportDraft`
- `GET /mock-report`
- `GET /mock-report/llm`
- `scripts/seed_report_chroma.py`
- `scripts/run_deepseek_report_smoke.py`
- Env vars:
  `SLEEPAGENT_REPORT_RETRIEVER`, `SLEEPAGENT_REPORT_CHROMA_DIR`,
  `DEEPSEEK_API_KEY`

Key outputs:

- Retrieval-augmented report context.
- Optional Chroma seed/index path.
- DeepSeek default-off fallback path with JSON validation and deterministic
  fallback behavior.
- Medical disclaimer and urgent safety caveats preserved.

Future optimization direction:

- Replace seed knowledge with a curated medical knowledge base, citation
  metadata, source governance, and retrieval evaluation.
- Move LLM reporting to a guarded production workflow with prompt/version
  tracking, safety classifiers, red-team tests, and clinician review.

### Stage 8: Agent Orchestration

Status: completed.

Scope:

- Added deterministic Sleep Analysis, Report, and Dialogue Agents.
- Added linear orchestration plus optional LangGraph boundary.
- Exposed backend Agent orchestration endpoint and frontend Agent panel.

Tools and libraries:

- Python dataclasses
- Pydantic Agent schemas
- Optional LangGraph
- FastAPI
- Streamlit (legacy/debug only in the current v1 deployment)
- pytest

Interfaces and endpoints:

- `sleepagent.schemas.agent.SleepAgentOrchestrationRequest`
- `SleepAgentEndpointRequest`
- `SleepAgentOrchestrationResult`
- `AgentStepTrace`
- `DialogueContext`
- `DialogueTurn`
- `sleepagent.agents.SleepAnalysisAgent`
- `sleepagent.agents.ReportAgent`
- `sleepagent.agents.DialogueAgent`
- `sleepagent.agents.SleepAgentOrchestrator`
- `sleepagent.agents.run_sleep_agent_orchestration()`
- `sleepagent.agents.run_sleep_agent_langgraph_orchestration()`
- `GET /agent/orchestrate`
- `POST /agent/orchestrate`
- `scripts/run_agent_orchestration_smoke.py`
- Frontend helpers:
  `build_agent_orchestration_url()`, `fetch_agent_orchestration()`,
  `extract_agent_orchestration_summary()`

Key outputs:

- Agent step traces:
  `sleep_analysis`, `report`, `dialogue`, `skip_dialogue`.
- Urgent symptom safety-boundary behavior.
- One-request dialogue context with history summary, preferences, and recent
  questions.
- Optional LangGraph path remains lazy-loaded.

Future optimization direction:

- Replace deterministic Agent logic with a production LangGraph graph, persistent
  state, tool calling, memory retrieval, observability, retry policy, and
  safety guardrails.
- Add evaluation for multi-turn consistency, hallucination resistance, and
  medical boundary compliance.

### Stage 9: Data Management, Long-Term Memory, Alerting, And External Tools

Status: completed for the current MVP scope.

Scope:

- Added local data management for analysis/report snapshots.
- Added deterministic long-term memory compression.
- Added local-only high-risk alert event recording.
- Added deterministic mock weather, temperature, diet, and lifestyle context.
- Added a minimal backend endpoint to exercise Stage 9 services together.

Tools and libraries:

- Pydantic strict schemas
- Local append-only JSONL storage
- FastAPI
- pytest
- Python standard library datetime, pathlib, random, hashlib

Interfaces and endpoints:

- `sleepagent.schemas.data_management.StoredAnalysisRecord`
- `StoredReportRecord`
- `sleepagent.services.data_management.SleepDataRepository`
- `LocalJsonlSleepDataRepository`
- `sleepagent.schemas.memory.LongTermMemorySummary`
- `compress_long_term_memory()`
- `compress_memory_from_repository()`
- `build_dialogue_context_from_memory()`
- `sleepagent.schemas.alert.AlertEvent`
- `LocalJsonlAlertEventRepository`
- `build_high_risk_alert_event()`
- `record_high_risk_alert_if_needed()`
- `sleepagent.schemas.external_tools.ExternalToolContext`
- `MockExternalContextProvider`
- `build_mock_external_context()`
- `sleepagent.schemas.stage9.Stage9MockContextRequest`
- `Stage9MockContextResult`
- `POST /stage9/mock-context`
- Env var: `SLEEPAGENT_DATA_STORE_DIR`
- Local JSONL files:
  `analysis_records.jsonl`, `report_records.jsonl`, `alert_events.jsonl`

Key outputs:

- Replaceable local repository boundary for data management.
- Long-term memory summary suitable for `DialogueContext.history_summary`.
- Local high-risk alerts that explicitly do not send SMS/email/app pushes.
- Mock external lifestyle/weather context with deterministic seed behavior.
- Stage 9 integrated backend smoke endpoint.

Future optimization direction:

- Replace local JSONL with PostgreSQL, migrations, indexes, JSONB payloads, and
  user/account access control.
- Persist memory summaries and expose read/write APIs for longitudinal history.
- Replace local alert logging with mature notification infrastructure such as
  queue workers, idempotency keys, escalation policy, audit trails, and SMS/email
  providers.
- Replace mock external context with approved weather, wearable, diet, and
  lifestyle APIs, including consent management and data freshness checks.

## Stage 10 Handoff Focus

Stage 10 should focus on final integration rather than expanding model scope:

- Docker and service startup.
- Final README and environment setup instructions.
- Demo scripts that exercise:
  `/tasks`, `/mock-analysis`, `/mock-report`, `/agent/orchestrate`,
  `/stage9/mock-context`, Next.js UI, and relevant CLI smoke scripts.
- Clear caveats for mock components, Stage 6 respiratory demo limitations,
  DeepSeek default-off behavior, optional Chroma/LangGraph dependencies, and
  local-only Stage 9 storage/alerts/external context.
- Paper or defense materials summarizing architecture, data contracts, model
  pipeline, Agent flow, safety boundaries, and future production upgrades.

### Stage 10 Completed So Far

- Added `scripts/run_stage10_shhs_demo.py` as the first Stage 10 integration
  demo helper.
- The script checks local SHHS sample paths, prints FastAPI/Next.js startup
  commands, prints SHHS XML/YASA/evaluation commands, and can run a local
  backend API smoke against `/health`, `/tasks`, `/mock-analysis`,
  `/mock-report`, `/agent/orchestrate`, and `/stage9/mock-context`.
- Added `docs/STAGE10_SHHS_DEMO.md` with a concise manual startup tutorial for
  authorized local SHHS data.
- Added focused tests in `tests/test_stage10_shhs_demo_script.py`.
- Verification:
  - `python -m py_compile scripts/run_stage10_shhs_demo.py tests/test_stage10_shhs_demo_script.py` passed.
  - `python -m pytest tests/test_stage10_shhs_demo_script.py -q` passed with
    `4 passed`.
  - `python scripts/run_stage10_shhs_demo.py --json --record-id shhs1-200001`
    passed and confirmed the local sample paths exist.
- Added Docker service files:
  - `.dockerignore`
  - `docker/Dockerfile`
  - `compose.yaml`
  - `compose.shhs-demo.yaml`
- The default Docker Compose stack runs the FastAPI backend on host port
  `18000` and the Next.js frontend on host port `18510`, with PostgreSQL and
  Stage 9 local storage isolated in named volumes.
- SHHS data is not bind-mounted by default. `compose.shhs-demo.yaml` provides an
  explicit read-only local SHHS sample mount for the backend in authorized demos;
  the frontend never mounts raw SHHS data.
- Added focused tests in `tests/test_stage10_docker_files.py`.
- Verification:
  - `python -m py_compile tests/test_stage10_docker_files.py` passed.
  - `python -m pytest tests/test_stage10_docker_files.py -q` passed with
    `5 passed`.
  - `docker compose -f compose.yaml config` passed.
  - `docker compose -f compose.yaml -f compose.shhs-demo.yaml config` passed.
- Productized the legacy Streamlit v1 frontend according to
  `SleepAgent_v1_optimization_goal.md`:
  - Moved mock request controls into a collapsed developer panel.
  - Added user snapshot, role switch, quick export/share actions, risk
    conclusion, key evidence table, Agent timeline, role-specific structured
    reports, dialogue Agent panel, respiratory/SpO2 trends, hypnogram, history
    trend entry, and collapsed raw data/developer details.
  - Added frontend helper functions for patient snapshots, evidence rows,
    role report sections, Agent timeline, export markdown, family share text,
    event markers, sleep-stage chart rows, and history trend rows.
  - Expanded `tests/test_frontend_api_client.py` to cover the new product-view
    helpers and existing API contracts.
- Verification:
  - `python -m py_compile frontend/app.py frontend/api_client.py tests/test_frontend_api_client.py` passed for the legacy Streamlit files.
  - `python -m pytest tests/test_frontend_api_client.py -q` passed with
    `14 passed`.
  - Legacy Streamlit `AppTest` executed `frontend/app.py` against a local
    backend with `exception_count 0`.
  - Local smoke services were started on FastAPI `127.0.0.1:18001` and
    legacy Streamlit `127.0.0.1:18502`; `curl` confirmed `/health`,
    `/mock-analysis`, and the legacy Streamlit root respond.
- Upgraded the frontend toward the v2 "Agent-driven sleep health workbench":
  - Initial page now stays in a pending state and does not fetch or display full
    analysis results until the user clicks "开始分析".
  - Added a dynamic Agent Run Console with step status, elapsed time, tool calls,
    input summaries, and output findings for data intake, sleep staging,
    respiratory detection, medical retrieval, report generation, and dialogue
    preparation.
  - Reordered the post-run first screen to show risk conclusion, primary reason,
    and next action before tables and charts.
  - Added a risk evidence-chain section explaining why the run is classified as
    moderate/high/low risk.
  - Converted multi-role reports into card-like sections, with the active role
    highlighted and developer/mock/raw data details still collapsed.
  - Added clickable follow-up question buttons and a grounded follow-up question
    submit flow that refreshes only the Agent dialogue payload.
  - Added `build_agent_run_steps()` and `build_risk_evidence_chain()` frontend
    helpers plus focused tests.
- Verification:
  - `python -m py_compile frontend/app.py frontend/api_client.py tests/test_frontend_api_client.py` passed for the legacy Streamlit files.
  - `python -m pytest tests/test_frontend_api_client.py -q` passed with
    `15 passed`.
  - Legacy Streamlit `AppTest` initial render passed with `exception_count 0`.
  - Legacy Streamlit `AppTest` clicked "开始分析" against local FastAPI
    `127.0.0.1:18001` and passed with `exception_count 0`.
- Added a new React / Next.js product frontend under `frontend/` while keeping
  the previous Streamlit Python files in place for legacy/debug compatibility:
  - Added Next.js App Router, TypeScript, Tailwind CSS, shadcn-style local UI
    primitives, Framer Motion transitions, Lucide React icons, and Recharts
    trend visualizations.
  - Added a fixed AppShell with functional sidebar navigation, topbar task
    controls, role switch, Today Analysis, Agent Run, Report Center, Trend
    Followup, Chat Agent, Alert Settings, and Data Management modules.
  - Added `frontend/lib/mock-data.ts`, `frontend/lib/types.ts`, and
    `frontend/lib/api.ts` so the UI can run independently before connecting to
    FastAPI.
  - Updated `docs/STAGE10_SHHS_DEMO.md` to launch the new Next.js frontend on
    `127.0.0.1:18510`.
  - Added `.gitignore` entries for `frontend/node_modules/`, `frontend/.next/`,
    and `frontend/tsconfig.tsbuildinfo`.
- Verification:
  - `npm install` completed in `frontend/` and generated `package-lock.json`.
  - `npm run typecheck` passed.
  - `npm run build` passed; Next.js prerendered `/` successfully.
  - `npm audit --audit-level=high` passed with no high/critical findings; npm
    reported 2 moderate advisories in Next/PostCSS dependency chain.
  - `npm run dev` started at `http://127.0.0.1:18510`; `curl -I` returned
    `HTTP/1.1 200 OK`.

## Known Issues And Caveats

- Chroma is optional and installed with `python -m pip install -e ".[rag]"`.
- Installing `.[rag]` previously produced a local dependency conflict:
  `anaconda-cli-base 0.6.0 requires click<8.2`, while Chroma installed
  `click 8.3.3`.
- LangGraph is optional and installed with `python -m pip install -e ".[agent]"`.
- Backend should run on port `18000` in this server environment because
  `8000/8001` are occupied.
- Backend report generation defaults to in-memory retrieval unless
  `SLEEPAGENT_REPORT_RETRIEVER=chroma` and
  `SLEEPAGENT_REPORT_CHROMA_DIR` are configured.
- DeepSeek report generation remains default-off and explicit opt-in.
- Stage 6 20-record respiratory demo checkpoint predicted `normal_breathing`
  for every test window. Downstream reports and Agents must not present it as
  evidence that respiratory abnormalities are absent.
- Stage 9 uses local JSONL and deterministic mock context for MVP integration.
  PostgreSQL, production memory tables, real SMS/email/app push, and live
  external APIs remain future work.
- FastAPI `TestClient` endpoint tests can hang inside sandbox network isolation;
  endpoint regressions have been run outside the sandbox with `timeout`, as
  archived in `docs/CHANGELOG.md`.

## Next Task

Stage 10 is in progress. Recommended next small task:

- Add the final README quickstart, without expanding model scope.
