# DEVELOPMENT_ROADMAP

This document is the authoritative staged development order for SleepAgent.

Each conversation window should focus on exactly one stage. When a stage is completed, Codex should clearly tell the user that the stage is complete so the user can open a new window for the next stage.

## Stage Order

### Stage 0: Project Skeleton + Mock Frontend/Backend Loop

Status: completed.

Scope:

- Project skeleton.
- FastAPI backend.
- Streamlit frontend.
- Mock sleep analysis endpoint.
- Mock report endpoint.
- Basic tests and task logging.

### Stage 1: Data Structures, Mock Data, And Metrics Standardization

Status: completed.

Scope:

- Review and standardize existing Pydantic schemas.
- Review and standardize mock sleep analysis data.
- Review and standardize sleep staging metrics.
- Review and standardize respiratory detection metrics.
- Ensure naming, units, enum values, JSON fields, and tests are stable before moving to real SHHS data.

Not in scope:

- Reading real SHHS files.
- Running YASA.
- Training PyTorch models.
- RAG or Agent orchestration.

### Stage 2: SHHS Data Understanding And Preprocessing Pipeline

Status: completed.

Scope:

- Understand SHHS PSG file layout, annotation files, channels, labels, sampling rates, and permissions.
- Design preprocessing pipeline.
- Implement local data path conventions without committing raw data.
- Build small verifiable preprocessing outputs.

Wrap-up checklist:

- Completed SHHS local data safety rules and repository ignore rules.
- Confirmed authorized local SHHS zip internal paths without full extraction.
- Extracted one local smoke-test sample record outside the code repository.
- Documented local `../data/` layout for raw data, samples, processed outputs, and manifests.
- Implemented SHHS EDF/XML path conventions and filename-only record discovery.
- Implemented XML annotation inspection for NSRR and Profusion XML metadata.
- Implemented SHHS XML sleep and respiratory label mapping into MVP enums.
- Implemented XML-derived sample summary output.
- Implemented minimal Stage 2 preprocessing manifest schema, writer, and validator.
- Verified local sample outputs without reading EDF signal contents.
- Did not run YASA, train PyTorch models, build respiratory training windows, add RAG, or implement Agents.

### Stage 3: YASA Sleep Staging Reproduction And Integration

Status: completed.

Scope:

- Reproduce YASA sleep staging on suitable sample signals.
- Map YASA output to Wake / REM / NREM.
- Connect YASA staging result to existing schemas and metrics.

Wrap-up checklist:

- Implemented a YASA output adapter for SleepAgent `SleepEpoch` and sleep summary schemas.
- Implemented local EDF inspection and YASA runner smoke scripts.
- Reproduced YASA sleep staging on authorized local SHHS EDF samples.
- Aligned YASA predictions with SHHS Profusion XML sleep stages.
- Computed Accuracy, Cohen's Kappa, macro F1, weighted F1, and per-class F1.
- Built SHHS zip EDF/XML pairing index and ran a 10-record local batch reproduction.
- Recorded batch failures and metric distributions.
- Compared `EEG` vs `EEG(sec)` for low-performing samples.
- Added confusion analysis to inspect Wake/REM/NREM error patterns.
- Kept raw EDF/XML files and derived data outputs outside the repository.
- Did not train PyTorch respiratory models, add RAG, or implement Agents.

### Stage 4: 1D-CNN + BiLSTM Respiratory Model Skeleton

Status: completed.

Scope:

- Build PyTorch model skeleton.
- Define input tensor shape, windowing assumptions, and class outputs.
- Add smoke tests for forward pass only.

Wrap-up checklist:

- Added a PyTorch-free respiratory tensor/config contract.
- Added the `RespiratoryCnnBiLstm` PyTorch module skeleton.
- Defined the default Stage 4 input shape as `(batch_size, 2, 3750)`.
- Defined output logits as `(batch_size, 3)` with class order:
  `normal_breathing`, `hypopnea`, `suspected_apnea`.
- Added contract tests and forward smoke tests.
- Kept real SHHS respiratory window construction, training, evaluation, and
  backend inference out of scope.

### Stage 5: Real SHHS Respiratory Event Training Data Construction

Status: completed.

Scope:

- Build training labels/windows from SHHS respiratory annotations.
- Align signal windows with normal breathing / hypopnea / suspected apnea labels.
- Validate class distribution and split strategy.

Wrap-up checklist:

- Implemented XML respiratory event extraction and target label counts.
- Locked 30-second window labeling rules, abnormal overlap threshold, normal
  exclusion buffer, and apnea/hypopnea conflict behavior.
- Added XML label/window manifest schema:
  `stage5.respiratory_windows_manifest.v1`.
- Added EDF respiratory signal window extraction for `THOR RES` and `ABDO RES`.
- Added signal window manifest schema:
  `stage5.respiratory_signal_windows_manifest.v1`.
- Added local NPZ derived dataset writer and manifest schema:
  `stage5.respiratory_npz_dataset_manifest.v1`.
- Added record-level train/validation/test split manifest schema:
  `stage5.respiratory_dataset_split_manifest.v1`.
- Wrote a real local single-record NPZ dataset for `shhs1-200001` outside the
  code repository.
- Kept model training, model evaluation, and backend inference out of scope.

### Stage 6: 1D-CNN + BiLSTM Training, Evaluation, And Inference

Status: completed.

Scope:

- Implement training loop.
- Implement evaluation loop.
- Report Recall, AUC, F1.
- Add inference path that returns existing respiratory schemas.

Wrap-up checklist:

- Added Stage 5 NPZ dataset loading for respiratory model inputs.
- Added a PyTorch Dataset-compatible wrapper.
- Added one-epoch supervised training smoke helper.
- Added evaluation helper that converts logits/probabilities into Recall, AUC,
  F1, and per-class Recall using the existing respiratory metrics.
- Added inference helpers for one window and one NPZ dataset.
- Added checkpoint save/load helpers for `RespiratoryCnnBiLstm`.
- Added tiny CLI smoke script for train/evaluate/infer.
- Ran a real local Stage 5 NPZ smoke on `shhs1-200001` with bounded training.
- Added and ran a 20-record SHHS demo experiment with exact `14/3/3`
  train/validation/test split, 5 epochs, best checkpoint, validation/test
  metrics, and per-test-record prediction summaries.
- Added a report/Agent context JSON artifact for downstream Stage 7/8 caveats.
- Kept backend wiring and model performance optimization out of scope.

### Stage 7: Report Generation Upgrade: Template To RAG

Status: completed.

Scope:

- Move from fixed templates to retrieval-augmented report generation.
- Build Chroma knowledge base.
- Keep elder-friendly and professional report versions.
- Maintain medical safety boundaries.

### Stage 8: Agent Orchestration

Status: completed.

Scope:

- Implement Sleep Analysis Agent.
- Implement Report Agent.
- Implement Dialogue Agent.
- Use LangGraph for stateful orchestration.

Wrap-up checklist:

- Added strict Agent request/result schemas and step traces.
- Added deterministic Sleep Analysis, Report, and Dialogue Agents.
- Added default linear orchestration.
- Added optional lazy-loaded LangGraph boundary.
- Added GET and POST `/agent/orchestrate` backend endpoints.
- Added CLI smoke for `/agent/orchestrate`.
- Added minimal one-request dialogue context.
- Added frontend API helper and Streamlit Agent panel.
- Preserved medical safety boundaries and DeepSeek default-off behavior.
- Kept database, persistent long-term memory, alerting, and external tools out
  of scope for Stage 9.

### Stage 9: Data Management, Long-Term Memory, Alerting, And External Tools

Status: completed for the current MVP scope.

Scope:

- PostgreSQL-backed data management.
- Long-term memory compression.
- Alert push service.
- Weather, temperature, diet, and lifestyle tool integration.

Wrap-up checklist:

- Added local JSONL analysis/report snapshot records and repository boundary.
- Added deterministic long-term memory compression into dialogue-ready history
  summaries.
- Added local high-risk alert event recording without real push channels.
- Added deterministic mock weather, temperature, diet, and lifestyle context.
- Added `POST /stage9/mock-context` to exercise Stage 9 local services together.
- Documented Stage 9 contracts and verification history.
- Kept PostgreSQL implementation, real SMS/email/app push, and live external API
  integrations out of the MVP scope.

### Stage 10: Final Integration, Docker, README, Demo Scripts, Paper/Defense Materials

Scope:

- End-to-end integration.
- Docker deployment.
- Final README updates.
- Demo scripts.
- Paper or defense material support.

## Current File Inventory

Current useful files:

- `backend/main.py`: FastAPI app with `/health`, `/mock-analysis`, and `/mock-report`.
- `frontend/app.py`: Streamlit frontend that currently consumes `/mock-analysis`.
- `frontend/api_client.py`: frontend helper for mock analysis.
- `sleepagent/schemas/sleep.py`: sleep analysis schemas.
- `sleepagent/schemas/report.py`: report response schemas.
- `sleepagent/preprocessing/mock_data.py`: deterministic mock sleep data generator.
- `sleepagent/preprocessing/stage_mapping.py`: Wake / REM / NREM mapping.
- `sleepagent/metrics/classification.py`: sleep staging classification metrics.
- `sleepagent/metrics/respiratory.py`: respiratory detection metrics.
- `sleepagent/services/report_templates.py`: mock report templates.
- `tests/`: focused tests for current MVP modules.

## Stage 1 Proposed Task Breakdown

Stage 1 should be completed before real SHHS data work begins.

Recommended small tasks:

1. Audit all Pydantic schemas and decide final MVP field names and units.
2. Replace Python 3.11+ UTC usage if Python 3.10 support is required.
3. Standardize mock data JSON examples for backend, frontend, and Agent use.
4. Add or update docs describing the stable data contracts.
5. Confirm metric definitions and edge-case behavior in docs.
6. Run full tests and update `TASK_LOG.md`.
