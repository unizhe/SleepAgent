# CHANGELOG

This file archives the detailed project history that previously lived in
`TASK_LOG.md`. Keep `TASK_LOG.md` short for quick Codex handoff, and append
long-form task history here.

## 2026-05-09

### Task 0001: 初始化 SleepAgent 文档

Status: completed

- Created the `sleepagent/` project root under the workspace root.
- Created initial planning docs:
  - `README.md`
  - `PROJECT_PLAN.md`
  - `MVP_SCOPE.md`
  - `AGENTS.md`
  - `TASK_LOG.md`
- Important decisions:
  - `../yasa/` is third-party source only and must not receive SleepAgent project files.
  - SleepAgent main project root is `sleepagent/`.
  - Development is incremental: small modules, runnable/testable/reversible.
  - Medical output is assistive only and does not replace clinician diagnosis.

### Task 0002: 阶段 0 项目骨架与最小可运行环境

Status: completed

- Created the minimum Python project structure:
  - `backend/`
  - `frontend/`
  - `sleepagent/`
  - `sleepagent/schemas/`
  - `sleepagent/preprocessing/`
  - `sleepagent/models/`
  - `sleepagent/metrics/`
  - `sleepagent/agents/`
  - `sleepagent/services/`
  - `scripts/`
  - `tests/`
  - `docker/`
- Added package `__init__.py` files.
- Added `pyproject.toml` with minimal dependencies:
  - `fastapi`
  - `uvicorn`
  - `streamlit`
  - `pydantic`
  - `pytest`
  - `httpx`
- Added FastAPI `/health` endpoint in `backend/main.py`.
- Added initial Streamlit skeleton in `frontend/app.py`.
- Added `.env.example` and `.gitignore`.
- Added `tests/test_health.py`.
- Verification history:
  - `python -m pytest` initially passed with `1 passed`.
  - FastAPI/TestClient required compatible FastAPI/Starlette/httpx versions.
  - `pyproject.toml` was updated to keep the Python 3.13 test environment working.

### Task 0003: 核心 Pydantic schemas 与 mock 睡眠数据生成器

Status: completed

- Added core schema file: `sleepagent/schemas/sleep.py`.
- Added mock generator: `sleepagent/preprocessing/mock_data.py`.
- Added CLI script: `scripts/generate_mock_sleep_data.py`.
- Added tests:
  - `tests/test_schemas.py`
  - `tests/test_mock_data.py`
- Added core enums:
  - `Sex`
  - `SleepStage`
  - `RespiratoryEventType`
  - `RiskLevel`
- Added core models:
  - `PatientProfile`
  - `SignalChannel`
  - `SleepRecordMetadata`
  - `SleepEpoch`
  - `RespiratoryEvent`
  - `RespiratoryTrendPoint`
  - `SleepStageSummary`
  - `RespiratorySummary`
  - `SleepStagingMetrics`
  - `RespiratoryDetectionMetrics`
  - `SleepAnalysisResult`
- Added `generate_mock_sleep_analysis()` and `generate_mock_sleep_data()`.
- Verification history:
  - `python -m py_compile ...` passed.
  - `python -m pytest` passed with `9 passed`.
  - Mock JSON generation script produced valid JSON.
- Limitation:
  - Mock data is synthetic only. It does not read SHHS and does not call YASA or PyTorch.

### Task 0004: 睡眠阶段标签映射与基础分类指标

Status: completed

- Added stage mapping module:
  - `sleepagent/preprocessing/stage_mapping.py`
- Added classification metrics module:
  - `sleepagent/metrics/classification.py`
- Added tests:
  - `tests/test_stage_mapping.py`
  - `tests/test_classification_metrics.py`
- Supported Wake/REM/NREM mapping:
  - String aliases such as `Wake`, `W`, `REM`, `R`, `N1`, `N2`, `N3`, `Stage 4`.
  - YASA numeric convention: `0 -> Wake`, `1/2/3 -> NREM`, `4 -> REM`.
  - SHHS-style numeric convention: `0 -> Wake`, `1/2/3/4 -> NREM`, `5 -> REM`.
  - Auto mode rejects bare numeric `4` because it is ambiguous across YASA and older/SHHS-style conventions.
- Added metrics:
  - Accuracy
  - Cohen's Kappa
  - per-class F1
  - macro F1
  - weighted F1
  - `compute_sleep_staging_metrics()`
- Verification history:
  - New mapping/metrics tests passed with `9 passed`.
  - Full suite passed with `18 passed`.

### Task 0005: 最小 `/mock-analysis` FastAPI 接口

Status: completed

- Added `GET /mock-analysis` to `backend/main.py`.
- The endpoint returns `SleepAnalysisResult` JSON.
- Query parameters:
  - `record_id`
  - `subject_id`
  - `duration_hours`
  - `seed`
  - `abnormal_event_rate_per_hour`
- Added test:
  - `tests/test_mock_analysis_endpoint.py`
- Verification history:
  - Full suite passed with `21 passed`.
  - Smoke check returned `200 mock-shhs-0001 30.0 moderate`.

### Task 0006: Streamlit 前端展示 `/mock-analysis`

Status: completed

- Added frontend helper:
  - `frontend/api_client.py`
- Updated Streamlit app:
  - `frontend/app.py`
- Added test:
  - `tests/test_frontend_api_client.py`
- Added env placeholder:
  - `SLEEPAGENT_API_BASE_URL`
- Frontend displays:
  - risk level
  - AHI
  - sleep efficiency
  - total sleep time
  - suspected apnea count
  - hypopnea count
  - normal breathing count
  - mean respiratory rate
  - respiratory trend chart/table
  - raw mock JSON
- Verification history:
  - Frontend helper tests passed with `3 passed`.
  - Full suite passed with `24 passed`.
  - Streamlit HTTP entry returned `200`.

### Task 0007: 呼吸暂停检测 Recall、AUC、F1 指标模块

Status: completed

- Added respiratory metrics module:
  - `sleepagent/metrics/respiratory.py`
- Added test:
  - `tests/test_respiratory_metrics.py`
- Added:
  - `map_respiratory_event_type()`
  - `compute_recall_by_class()`
  - `compute_abnormal_event_recall()`
  - `compute_binary_auc()`
  - `compute_multiclass_ovr_auc()`
  - `compute_respiratory_detection_metrics()`
- Metric definitions:
  - `recall`: abnormal event recall, combining `hypopnea` and `suspected_apnea`.
  - `auc`: one-vs-rest macro AUC when probability scores are provided.
  - `f1`: three-class macro F1.
  - `per_class_recall`: recall for normal breathing, hypopnea, and suspected apnea.
- Verification history:
  - Respiratory metrics tests passed with `7 passed`.
  - Full suite passed with `31 passed`.

### Task 0008: 修复 Streamlit 前端同目录导入问题

Status: completed

- Fixed `ModuleNotFoundError: No module named 'frontend'` when launching:
  - `streamlit run frontend/app.py`
- Changed `frontend/app.py` import from:
  - `from frontend.api_client import ...`
- To same-directory import:
  - `from api_client import ...`
- Kept `tests/test_frontend_api_client.py` using `from frontend.api_client import ...` because `python -m pytest` runs from project root and that path works there.
- Verification history:
  - `python -m py_compile frontend/app.py` passed.
  - `python -m pytest tests/test_frontend_api_client.py` passed with `3 passed`.
  - `python -m pytest` passed with `31 passed`.
  - Short Streamlit launch returned HTTP `200`.

### Task 0009: 新增 `/mock-report` 接口和报告模板

Status: completed

- Added report schema:
  - `sleepagent/schemas/report.py`
- Added report template service:
  - `sleepagent/services/report_templates.py`
- Added endpoint:
  - `GET /mock-report`
- Added tests:
  - `tests/test_report_templates.py`
  - `tests/test_mock_report_endpoint.py`
- Report response includes:
  - `summary`
  - `elder_report`
  - `professional_report`
  - `care_suggestions`
  - `medical_disclaimer`
- Query parameters match `/mock-analysis`:
  - `record_id`
  - `subject_id`
  - `duration_hours`
  - `seed`
  - `abnormal_event_rate_per_hour`
- Verification history:
  - Report template tests passed with `2 passed`.
  - Report endpoint tests passed with `2 passed`.
  - Full suite passed with `35 passed`.
  - Backend smoke test on port `18000` returned `200 mock-shhs-0001 30.0 True True`.

## Important Compatibility And Environment History

- `../yasa/` is a third-party cloned source tree. It must not be modified for SleepAgent work unless explicitly requested.
- Server ports `8000` and `8001` were occupied in the working environment. Backend commands should use port `18000`.
- Stage 0 FastAPI + Streamlit + mock analysis/report loop is working.
- Python 3.10 compatibility history: `datetime.UTC` is not available in Python 3.10. The project history must preserve the requirement/fix to use a Python 3.10-compatible UTC form, such as `datetime.timezone.utc`, where compatibility matters.
- FastAPI `TestClient` can hang under the Codex sandbox process isolation. Full pytest runs involving FastAPI endpoint tests were run outside the sandbox with approval.

## 2026-05-11

### Task 0010: Compact Task Log

Status: completed

- Compacted `TASK_LOG.md` into a current status summary.
- Moved long-form task history into this `docs/CHANGELOG.md`.
- Preserved important history:
  - Python 3.10 `datetime.UTC` compatibility note.
  - frontend import fix.
  - server ports `8000/8001` occupied; backend uses `18000`.
  - Stage 0 mock frontend/backend loop completed.

### Task 0011: Document Revised Development Stage Order

Status: completed

- Added `docs/DEVELOPMENT_ROADMAP.md` as the authoritative staged development order.
- Updated `PROJECT_PLAN.md` to reflect stages 0 through 10.
- Updated `MVP_SCOPE.md` recommended sequence to match the revised staged order.
- Updated `TASK_LOG.md` so the next task is Stage 1: data structures, mock data, and metrics standardization.
- Confirmed no code files were modified.
- Confirmed `../yasa/` was not modified.

### Task 0012: Streamlit 前端展示 `/mock-report`

Status: completed

- Added frontend report helpers:
  - `build_mock_report_url()`
  - `fetch_mock_report()`
  - `extract_report_sections()`
- Updated Streamlit app to fetch `/mock-report` with the same sidebar parameters as `/mock-analysis`.
- Added a report section with two tabs:
  - 老人易懂版
  - 子女/医生专业版
- Displayed care suggestions and the medical disclaimer from the report payload.
- Updated frontend helper tests.
- Verification history:
  - `python -m pytest tests/test_frontend_api_client.py` passed with `5 passed`.
  - `python -m py_compile frontend/app.py frontend/api_client.py` passed.
  - Full `python -m pytest` attempt reached `tests/test_health.py` and then hit the known FastAPI `TestClient` sandbox hang.

### Task 0013: 审计并稳定 Pydantic schema 与 mock JSON 合约

Status: completed

- Audited current Stage 1 schema and mock payload contracts:
  - `SleepAnalysisResult`
  - `MockSleepReport`
  - nested sleep summary, respiratory summary, metrics, trend, event, and report summary fields
- Kept existing MVP field names stable; no schema-wide rename was needed.
- Documented stable contracts in:
  - `docs/DATA_CONTRACTS.md`
- Replaced Python 3.11+ UTC usage in runtime code with Python 3.10-compatible `datetime.timezone.utc`.
- Aligned `ReportSummary.mean_respiratory_rate_bpm` bounds with `RespiratorySummary.mean_respiratory_rate_bpm`.
- Added contract tests that lock mock analysis/report JSON shapes without using FastAPI `TestClient`:
  - `tests/test_mock_json_contracts.py`
- Updated schema test coverage:
  - report summary respiratory rate bounds
- Verification history:
  - `python -m py_compile sleepagent/schemas/sleep.py sleepagent/schemas/report.py sleepagent/preprocessing/mock_data.py` passed.
  - `python -m pytest tests/test_schemas.py tests/test_mock_data.py tests/test_report_templates.py tests/test_mock_json_contracts.py` passed with `14 passed`.
  - Targeted non-endpoint suite covering schemas, mock data, reports, metrics, stage mapping, and frontend helpers passed with `35 passed`.

### Task 0014: 确认指标定义和边界行为

Status: completed

- Locked sleep staging metric edge behavior in tests:
  - empty inputs raise `ValueError`
  - mismatched lengths raise `ValueError`
  - empty explicit labels raise `ValueError`
  - missing-label F1 is `0.0`
  - macro F1 includes requested labels with no support
  - weighted F1 returns `0.0` when requested labels have no true support
- Locked respiratory metric edge behavior in tests:
  - unknown labels raise `ValueError`
  - empty inputs raise `ValueError`
  - mismatched lengths raise `ValueError`
  - per-class recall is `0.0` when a class has no true samples
  - abnormal-event recall is `0.0` when there are no true abnormal events
  - binary AUC rejects empty input, mismatched score length, and single-class samples
  - multiclass one-vs-rest AUC skips unsupported classes and rejects fully single-class samples
  - score rows must include every requested respiratory label
- Updated `docs/DATA_CONTRACTS.md` with metric edge behavior rules.
- Verification history:
  - `python -m py_compile sleepagent/metrics/classification.py sleepagent/metrics/respiratory.py` passed.
  - `python -m pytest tests/test_classification_metrics.py tests/test_respiratory_metrics.py` passed with `16 passed`.
  - Targeted non-endpoint suite covering schemas, mock data, reports, metrics, stage mapping, and frontend helpers passed with `40 passed`.

### Task 0015: Stage 1 收尾检查

Status: completed

- Reviewed Stage 1 scope against:
  - `docs/DATA_CONTRACTS.md`
  - `TASK_LOG.md`
  - `docs/DEVELOPMENT_ROADMAP.md`
- Confirmed Stage 1 deliverables are stable:
  - Pydantic schemas
  - mock analysis/report JSON contracts
  - sleep staging metric definitions and edge behavior
  - respiratory detection metric definitions and edge behavior
  - Python 3.10-compatible UTC usage in runtime code
- Marked Stage 1 as completed and Stage 2 as the next stage in:
  - `docs/DEVELOPMENT_ROADMAP.md`
  - `PROJECT_PLAN.md`
  - `MVP_SCOPE.md`
  - `TASK_LOG.md`
- Did not read real SHHS data, run YASA, train PyTorch, add RAG, or implement Agents during Stage 1 wrap-up.
- Verification history:
  - `python -m py_compile sleepagent/schemas/sleep.py sleepagent/schemas/report.py sleepagent/preprocessing/mock_data.py sleepagent/metrics/classification.py sleepagent/metrics/respiratory.py frontend/api_client.py frontend/app.py backend/main.py` passed.
  - Targeted non-endpoint suite covering schemas, mock data, reports, metrics, stage mapping, and frontend helpers passed with `40 passed`.
  - FastAPI endpoint tests were not run in-sandbox because of the known `TestClient` hang documented in `TASK_LOG.md`.

### Task 0016: Stage 2 SHHS 本地路径约定与记录发现

Status: completed

- Started Stage 2 and marked it in progress in:
  - `docs/DEVELOPMENT_ROADMAP.md`
  - `PROJECT_PLAN.md`
  - `MVP_SCOPE.md`
  - `TASK_LOG.md`
- Added `sleepagent/preprocessing/shhs_paths.py` for filename-only SHHS local data handling:
  - `SLEEPAGENT_SHHS_ROOT` environment variable support
  - visit normalization for `shhs1` / `shhs2`
  - record id normalization from ids and official EDF/XML file names
  - expected EDF, NSRR XML, and Profusion XML path construction
  - local record discovery by file name without reading EDF/XML contents
  - manifest-style missing-file checks
- Exported SHHS path helpers from `sleepagent.preprocessing`.
- Added `.gitignore` entries for local SHHS/NSRR raw data downloads, including EDF files and common download roots.
- Documented Stage 2 SHHS local data conventions in `docs/DATA_CONTRACTS.md`.
- Added focused tests in `tests/test_shhs_paths.py`.
- Verification history:
  - `python -m pytest tests/test_shhs_paths.py` passed with `5 passed`.
  - `python -m py_compile sleepagent/preprocessing/shhs_paths.py sleepagent/preprocessing/__init__.py` passed.
  - Targeted non-endpoint suite covering SHHS paths, schemas, mock data, reports, metrics, stage mapping, and frontend helpers passed with `45 passed`.
  - Full `python -m pytest` was attempted, reached the known FastAPI `TestClient` sandbox hang, and the hung pytest processes were stopped.

### Task 0017: Stage 2 SHHS zip 安全本地检查

Status: completed

- Added `docs/SHHS_LOCAL_DATA.md` documenting safe local SHHS data placement:
  - `../data/` is local-only and outside the `sleepagent/` code repository.
  - raw SHHS zip, EDF, XML, NPY, NPZ, Parquet, derived preprocessing outputs, and sensitive manifests must not be committed.
  - the current Stage 2 task must not fully extract the 140 GB SHHS zip.
  - future smoke tests should use only 1-3 local sample XML/EDF records under `../data/raw/shhs_sample/`.
- Updated `docs/DATA_CONTRACTS.md` with the same Stage 2 safety contract and linked to the new local data setup doc.
- Added `scripts/check_shhs_zip.py`:
  - reads the zip path from CLI or `SLEEPAGENT_SHHS_ZIP`
  - defaults to `../data/raw/shhs.zip`
  - handles missing zip files with a clear error and setup hint
  - lists only a small number of `.edf` and `.xml` member names
  - does not extract files and does not read EDF signal contents
- Added synthetic zip tests in `tests/test_check_shhs_zip.py`.
- Strengthened `.gitignore` for local data files and derived arrays/tables.
- Verification history:
  - `python -m pytest tests/test_check_shhs_zip.py` passed with `4 passed`.
  - `python -m py_compile scripts/check_shhs_zip.py` passed.
  - Targeted non-endpoint suite covering SHHS zip checks, SHHS paths, schemas, mock data, reports, metrics, stage mapping, and frontend helpers passed with `49 passed`.

### Task 0018: Stage 2 真实 zip 路径确认、样本抽取与 XML 标注检查

Status: completed

- Confirmed the real local archive is at:
  - `/mnt/data4/wz/SleepAgent/data/raw/shhs.zip`
- Ran the safe zip inspection script:
  - `SLEEPAGENT_SHHS_ZIP=../data/raw/shhs.zip python scripts/check_shhs_zip.py --max-entries 20`
  - Found `26698` total zip entries.
  - Found `8444` EDF entries.
  - Found `16888` XML entries.
  - Confirmed paths use `polysomnography/edfs/...`, `polysomnography/annotations-events-nsrr/...`, and `polysomnography/annotations-events-profusion/...`.
- Extracted only one matched smoke-test record outside the code repository:
  - `../data/raw/shhs_sample/polysomnography/edfs/shhs1/shhs1-200001.edf`
  - `../data/raw/shhs_sample/polysomnography/annotations-events-nsrr/shhs1/shhs1-200001-nsrr.xml`
  - `../data/raw/shhs_sample/polysomnography/annotations-events-profusion/shhs1/shhs1-200001-profusion.xml`
- Verified `discover_shhs_records("../data/raw/shhs_sample")` sees one complete sample record.
- Added `sleepagent/preprocessing/shhs_annotations.py`:
  - parses authorized local XML annotation files
  - reports root tag, epoch length, scored event count, event type/name counts, signal counts, and sleep stage counts
  - does not read EDF signal contents
  - does not map labels into final model targets yet
- Added `scripts/inspect_shhs_annotation.py` for local JSON inspection of XML summaries.
- Added focused tests in `tests/test_shhs_annotations.py` using synthetic XML fixtures only.
- Updated `docs/DATA_CONTRACTS.md`, `docs/SHHS_LOCAL_DATA.md`, and `TASK_LOG.md`.
- Smoke inspection results for `shhs1-200001`:
  - NSRR XML: root `PSGAnnotation`, epoch length `30.0`, scored events `552`, respiratory concepts include `Hypopnea` and `Obstructive apnea`.
  - Profusion XML: root `CMPStudyConfig`, epoch length `30.0`, scored events `370`, sleep stages `1084`, stage values include `0`, `1`, `2`, `3`, `5`.
- Verification history:
  - `python -m pytest tests/test_shhs_annotations.py` passed with `3 passed`.
  - `python -m py_compile sleepagent/preprocessing/shhs_annotations.py sleepagent/preprocessing/__init__.py scripts/inspect_shhs_annotation.py` passed.
  - Targeted non-endpoint suite covering SHHS zip checks, SHHS annotation inspection, SHHS paths, schemas, mock data, reports, metrics, stage mapping, and frontend helpers passed with `52 passed`.

### Task 0019: Stage 2 SHHS XML 标签映射 helper

Status: completed

- Added `sleepagent/preprocessing/shhs_label_mapping.py` for explicit read-only
  Stage 2 mappings from inspected SHHS XML vocabularies into existing MVP enums.
- Sleep stage mappings:
  - SHHS numeric codes: `0 -> Wake`, `1/2/3/4 -> NREM`, `5 -> REM`
  - NSRR event concepts such as `Wake|0`, `Stage 2 sleep|2`, and `REM sleep|5`
  - Profusion `SleepStages/SleepStage` numeric values
- Respiratory event mappings:
  - `Hypopnea`, `Obstructive Hypopnea`, `Central Hypopnea`, and `Mixed Hypopnea`
    map to `hypopnea`.
  - `Obstructive Apnea`, `Central Apnea`, and `Mixed Apnea` map to
    `suspected_apnea`.
  - Non-target XML labels such as arousals, SpO2 artifacts/desaturations,
    recording start time, limb movement, and sleep stage labels can be skipped
    with `unknown_policy="ignore"`.
  - Unknown labels raise by default for review.
- Exported the new helpers from `sleepagent.preprocessing`.
- Added focused tests in `tests/test_shhs_label_mapping.py`.
- Updated `docs/DATA_CONTRACTS.md` and `TASK_LOG.md`.
- Smoke mapped the real local `shhs1-200001` sample inspection counts:
  - NSRR respiratory labels -> `hypopnea: 85`, `suspected_apnea: 2`
  - NSRR sleep labels -> `Wake: 48`, `REM: 7`, `NREM: 126`
  - Profusion sleep stage codes -> `Wake: 333`, `REM: 102`, `NREM: 649`
  - Profusion respiratory labels -> `hypopnea: 85`, `suspected_apnea: 2`
- Verification history:
  - `python -m pytest tests/test_shhs_label_mapping.py` passed with `6 passed`.
  - `python -m py_compile sleepagent/preprocessing/shhs_label_mapping.py sleepagent/preprocessing/__init__.py` passed.
  - Targeted non-endpoint suite covering SHHS zip checks, SHHS annotation inspection, SHHS label mapping, SHHS paths, schemas, mock data, reports, metrics, stage mapping, and frontend helpers passed with `58 passed`.

### Task 0020: Stage 2 XML-derived preprocessing summary

Status: completed

- Added `sleepagent/preprocessing/shhs_summary.py` to build a tiny metadata-only
  preprocessing summary for one local SHHS sample record.
- The summary includes:
  - record id and visit
  - local SHHS-like root
  - EDF path and `edf_exists` flag only
  - NSRR XML metadata summary
  - optional Profusion XML metadata summary
  - mapped sleep stage counts
  - mapped respiratory event counts
  - notes that no EDF content is read and no training windows are created
- Added `scripts/summarize_shhs_sample.py` to print the summary as JSON.
- Added synthetic XML tests in `tests/test_shhs_summary.py`.
- Updated `docs/DATA_CONTRACTS.md`, `docs/SHHS_LOCAL_DATA.md`, and `TASK_LOG.md`.
- Smoke ran the local `shhs1-200001` sample:
  - EDF exists: `true`
  - NSRR root `PSGAnnotation`, scored events `552`
  - NSRR mapped sleep counts: `Wake: 48`, `REM: 7`, `NREM: 126`
  - NSRR mapped respiratory counts: `hypopnea: 85`, `suspected_apnea: 2`
  - Profusion root `CMPStudyConfig`, scored events `370`, sleep stages `1084`
  - Profusion mapped sleep counts: `Wake: 333`, `REM: 102`, `NREM: 649`
  - Profusion mapped respiratory counts: `hypopnea: 85`, `suspected_apnea: 2`
- Verification history:
  - `python -m pytest tests/test_shhs_summary.py` passed with `2 passed`.
  - `python -m py_compile sleepagent/preprocessing/shhs_summary.py sleepagent/preprocessing/__init__.py scripts/summarize_shhs_sample.py` passed.
  - Targeted non-endpoint suite covering SHHS zip checks, SHHS annotation inspection, SHHS label mapping, SHHS XML summary, SHHS paths, schemas, mock data, reports, metrics, stage mapping, and frontend helpers passed with `60 passed`.

### Task 0021: Stage 2 preprocessing manifest schema

Status: completed

- Extended `sleepagent/preprocessing/shhs_summary.py` with a minimal Stage 2
  preprocessing manifest dataclass.
- Manifest schema version:
  - `stage2.preprocess_manifest.v1`
- Manifest fields:
  - `schema_version`
  - `generated_at`
  - `source_root`
  - `record_count`
  - `records`
  - `safety_notes`
- The manifest records sample paths, XML summaries, mapped label counts, and
  safety notes only.
- The manifest does not include EDF contents, epochs, windows, train/test
  splits, model inputs, arrays, or parquet outputs.
- Updated `scripts/summarize_shhs_sample.py` with `--manifest`.
- Updated tests in `tests/test_shhs_summary.py`.
- Updated `docs/DATA_CONTRACTS.md`, `docs/SHHS_LOCAL_DATA.md`, and
  `TASK_LOG.md`.
- Smoke command:
  - `python scripts/summarize_shhs_sample.py --root ../data/raw/shhs_sample --record-id shhs1-200001 --manifest`
- Verification history:
  - `python -m pytest tests/test_shhs_summary.py` passed with `3 passed`.
  - `python -m py_compile sleepagent/preprocessing/shhs_summary.py sleepagent/preprocessing/__init__.py scripts/summarize_shhs_sample.py` passed.
  - Targeted non-endpoint suite covering SHHS zip checks, SHHS annotation inspection, SHHS label mapping, SHHS XML summary/manifest, SHHS paths, schemas, mock data, reports, metrics, stage mapping, and frontend helpers passed with `61 passed`.

### Task 0022: Stage 2 local manifest writer

Status: completed

- Added opt-in local manifest writing to `sleepagent/preprocessing/shhs_summary.py`:
  - `write_shhs_preprocessing_manifest()`
  - `default_shhs_manifest_filename()`
  - default output directory constant `../data/manifests`
- Updated `scripts/summarize_shhs_sample.py`:
  - `--write-manifest`
  - `--manifest-dir`
  - `--write-manifest` requires `--manifest`
- Added tests for writing manifest JSON into an explicit temporary directory.
- Updated `docs/SHHS_LOCAL_DATA.md`, `docs/DATA_CONTRACTS.md`, and `TASK_LOG.md`.
- Wrote one local sample manifest outside the code repository:
  - `../data/manifests/stage2_preprocess_manifest_v1_shhs1-200001_20260511T080441Z.json`
- Verified the written JSON has:
  - schema version `stage2.preprocess_manifest.v1`
  - record count `1`
  - record id `shhs1-200001`
  - safety note that EDF signal contents are not read
- Verification history:
  - `python -m pytest tests/test_shhs_summary.py` passed with `4 passed`.
  - `python -m py_compile sleepagent/preprocessing/shhs_summary.py sleepagent/preprocessing/__init__.py scripts/summarize_shhs_sample.py` passed.
  - Targeted non-endpoint suite covering SHHS zip checks, SHHS annotation inspection, SHHS label mapping, SHHS XML summary/manifest, SHHS paths, schemas, mock data, reports, metrics, stage mapping, and frontend helpers passed with `62 passed`.

### Task 0023: Stage 2 manifest validation helper

Status: completed

- Added lightweight Stage 2 manifest validation helpers in
  `sleepagent/preprocessing/shhs_summary.py`:
  - `SHHSManifestValidationResult`
  - `validate_shhs_preprocessing_manifest_payload()`
  - `validate_shhs_preprocessing_manifest_file()`
- Validation checks:
  - schema version equals `stage2.preprocess_manifest.v1`
  - required top-level fields exist
  - `record_count` is an integer and matches `len(records)`
  - safety notes mention commit safety, EDF contents not being read, and no
    training windows
  - each record includes required path/annotation/note fields
  - annotation summaries include required metadata and mapped count fields when
    present
- Validation does not read raw EDF/XML files.
- Updated `scripts/summarize_shhs_sample.py` with `--validate-manifest`.
- Updated tests in `tests/test_shhs_summary.py`.
- Updated `docs/DATA_CONTRACTS.md`, `docs/SHHS_LOCAL_DATA.md`, and
  `TASK_LOG.md`.
- Verified the latest local manifest with:
  - `python scripts/summarize_shhs_sample.py --validate-manifest <latest local manifest>`
- Verification history:
  - `python -m pytest tests/test_shhs_summary.py` passed with `8 passed`.
  - `python -m py_compile sleepagent/preprocessing/shhs_summary.py sleepagent/preprocessing/__init__.py scripts/summarize_shhs_sample.py` passed.
  - Targeted non-endpoint suite covering SHHS zip checks, SHHS annotation inspection, SHHS label mapping, SHHS XML summary/manifest validation, SHHS paths, schemas, mock data, reports, metrics, stage mapping, and frontend helpers passed with `66 passed`.

### Task 0024: Stage 2 收尾 checklist

Status: completed

- Reviewed Stage 2 scope from `docs/DEVELOPMENT_ROADMAP.md`:
  - Understand SHHS PSG file layout, annotation files, channels, labels,
    sampling rates, and permissions.
  - Design preprocessing pipeline.
  - Implement local data path conventions without committing raw data.
  - Build small verifiable preprocessing outputs.
- Confirmed Stage 2 completed deliverables:
  - SHHS local data safety documentation and ignore rules.
  - Safe SHHS zip inspection without full extraction.
  - One local smoke-test sample under `../data/raw/shhs_sample/`.
  - SHHS EDF/XML path conventions and filename-only record discovery.
  - NSRR and Profusion XML annotation inspection.
  - SHHS XML label mappings into MVP sleep and respiratory enums.
  - XML-derived sample summary.
  - Stage 2 preprocessing manifest schema, writer, and validator.
- Confirmed explicit non-goals remained untouched:
  - no EDF signal content reading
  - no full SHHS extraction
  - no YASA inference
  - no PyTorch respiratory model training or window construction
  - no RAG
  - no Agent implementation
- Marked Stage 2 completed and Stage 3 as the next stage in:
  - `docs/DEVELOPMENT_ROADMAP.md`
  - `PROJECT_PLAN.md`
  - `MVP_SCOPE.md`
  - `TASK_LOG.md`
- Verification history:
  - No code changes in this task.
  - Last verified targeted non-endpoint suite remains Task 0023: `66 passed`.

### Task 0032: Stage 3 low-record EEG channel comparison and confusion analysis

Status: completed

- Added confusion analysis for Stage 3 YASA-vs-SHHS outputs:
  - `sleepagent.services.yasa_confusion_analysis`
  - `scripts/analyze_yasa_shhs_confusion.py`
  - `tests/test_yasa_confusion_analysis.py`
- Ran real `EEG(sec)` YASA inference and Profusion XML evaluation for the low-performing batch records:
  - `shhs1-200002`
  - `shhs1-200007`
  - `shhs1-200008`
- Compared original `EEG` vs `EEG(sec)`:
  - `shhs1-200002` recommends `EEG(sec)`: Accuracy `0.8091` -> `0.9120`.
  - `shhs1-200007` recommends `EEG`: Accuracy ties at `0.8455`, with better Kappa/macro F1/weighted F1 for `EEG`.
  - `shhs1-200008` recommends `EEG(sec)`: Accuracy `0.8801` -> `0.9437`.
- Confusion finding:
  - Original `EEG` low scores are not purely REM/NREM confusion; Wake->NREM is dominant for `shhs1-200002` and `shhs1-200008`.
  - Remaining `EEG(sec)` errors are more concentrated in REM/NREM confusion across the three low-performing records.
- Verification history:
  - `python -m py_compile sleepagent/services/yasa_confusion_analysis.py scripts/analyze_yasa_shhs_confusion.py tests/test_yasa_confusion_analysis.py` passed.
  - `python -m pytest tests/test_yasa_confusion_analysis.py` passed with `2 passed`.
  - Stage 3 focused tests passed with `21 passed`.

### Task 0033: Stage 3 wrap-up and Stage 4 warm-up status

Status: completed

- User chose not to run a full 10-record `EEG(sec)` batch comparison.
- Marked Stage 3 as completed in:
  - `docs/DEVELOPMENT_ROADMAP.md`
  - `PROJECT_PLAN.md`
  - `MVP_SCOPE.md`
  - `TASK_LOG.md`
- Set Stage 4 to warm-up status.
- Stage 3 completion summary:
  - YASA output adapter implemented.
  - Local EDF inspection and YASA runner implemented.
  - Real SHHS sample YASA smoke run completed.
  - SHHS Profusion XML alignment and evaluation implemented.
  - EEG channel comparison implemented.
  - SHHS zip EDF/XML pairing and 10-record batch reproduction implemented.
  - Batch metric distribution and low-record confusion analysis implemented.
  - Raw EDF/XML data and derived outputs remained outside the repository.
- Verification history:
  - Documentation-only update; no tests required.
  - Previous Stage 3 focused test suite passed with `21 passed`.

### Task 0034: Stage 4 respiratory model skeleton

Status: completed

- Checked dependency and project structure:
  - PyTorch was not installed in the default Python environment.
  - Added PyTorch as optional dependency extra `sleepagent[model]`.
  - Kept the tensor contract importable without PyTorch.
- Added respiratory model contract module:
  - `sleepagent/models/respiratory_contract.py`
  - default input tensor shape helper: `(batch_size, 2, 3750)`
  - default assumption: 30-second windows at 125 Hz
  - default class order: `normal_breathing`, `hypopnea`, `suspected_apnea`
  - output logits shape helper: `(batch_size, 3)`
- Added PyTorch skeleton module:
  - `sleepagent/models/respiratory_cnn_bilstm.py`
  - 1D Conv / BatchNorm / ReLU / MaxPool feature extractor
  - bidirectional LSTM sequence model
  - linear classifier returning raw logits
- Added tests:
  - `tests/test_respiratory_model_contract.py`
  - `tests/test_respiratory_cnn_bilstm.py`
- Updated docs/status:
  - `docs/DATA_CONTRACTS.md`
  - `docs/DEVELOPMENT_ROADMAP.md`
  - `PROJECT_PLAN.md`
  - `MVP_SCOPE.md`
  - `TASK_LOG.md`
- Stage 4 intentionally did not build SHHS respiratory training windows,
  train a model, compute model evaluation metrics, or wire respiratory model
  inference into backend outputs.
- Verification history:
  - `python -m py_compile sleepagent/models/__init__.py sleepagent/models/respiratory_contract.py sleepagent/models/respiratory_cnn_bilstm.py` passed.
  - `python -m pytest tests/test_respiratory_model_contract.py tests/test_respiratory_cnn_bilstm.py tests/test_yasa_staging_adapter.py` passed with `6 passed, 1 skipped`.
  - The skipped test is the PyTorch forward smoke module because default Python does not currently have `torch` installed.

### Task 0035: Stage 5 respiratory XML window labels

Status: completed

- Added XML-only Stage 5 preprocessing module:
  - `sleepagent/preprocessing/shhs_respiratory_events.py`
- Implemented:
  - `extract_shhs_respiratory_event_sequence()`
  - `build_shhs_respiratory_training_windows()`
  - `build_shhs_respiratory_training_windows_from_xml()`
- Added dataclasses:
  - `SHHSRespiratoryEventSequence`
  - `SHHSRespiratoryTrainingWindow`
  - `SHHSRespiratoryTrainingWindowSequence`
- Window label contract:
  - 30-second windows by default.
  - 30-second stride by default.
  - 1-second minimum abnormal-event overlap by default.
  - Windows with no abnormal overlap become `normal_breathing`.
  - Abnormal windows become `hypopnea` or `suspected_apnea`.
  - Larger total abnormal overlap wins when both abnormal classes overlap.
  - Exact abnormal overlap ties prefer `suspected_apnea`.
- Added tests:
  - `tests/test_shhs_respiratory_events.py`
- Updated docs/status:
  - `docs/DATA_CONTRACTS.md`
  - `TASK_LOG.md`
- Real local Profusion XML smoke on authorized sample:
  - XML: `../data/raw/shhs_sample/polysomnography/annotations-events-profusion/shhs1/shhs1-200001-profusion.xml`
  - Generated windows: `1084`
  - Recording duration: `32520.0` seconds
  - Mapped respiratory events: `87`
  - Ignored non-target scored events: `283`
  - Class counts: `normal_breathing=946`, `hypopnea=135`, `suspected_apnea=3`
- Verification history:
  - `python -m py_compile sleepagent/preprocessing/shhs_respiratory_events.py sleepagent/preprocessing/__init__.py tests/test_shhs_respiratory_events.py` passed.
  - `python -m pytest tests/test_shhs_respiratory_events.py` passed with `6 passed`.
  - `python -m pytest tests/test_shhs_respiratory_events.py tests/test_shhs_label_mapping.py tests/test_shhs_annotations.py tests/test_respiratory_model_contract.py` passed with `17 passed`.
  - Full `python -m pytest` was attempted but interrupted after hanging at `tests/test_health.py`, matching the known FastAPI `TestClient` sandbox limitation.
- Scope intentionally left for later Stage 5 tasks:
  - EDF respiratory signal extraction.
  - SHHS respiratory channel selection.
  - Local derived dataset writing.
  - Train/validation/test split manifest.

### Task 0036: Stage 5 label/window manifest contract revision

Status: completed

- Revised the Stage 5 XML label/window contract before advancing to EDF signal
  extraction.
- Added explicit Stage 5 manifest schema:
  - `stage5.respiratory_windows_manifest.v1`
- Locked label/window rules:
  - default abnormal overlap threshold: `1.0` second
  - default normal exclusion buffer: `30.0` seconds
  - normal rule:
    `no_abnormal_overlap_and_outside_abnormal_event_buffer`
  - conflict rule:
    `largest_abnormal_overlap_seconds_tie_suspected_apnea`
- Candidate normal windows inside the abnormal-event buffer are now excluded
  from training with `exclusion_reason="near_abnormal_event"`.
- Added manifest fields for:
  - `target_label_counts`
  - `ignored_label_counts`
  - `unknown_label_counts`
  - `included_class_counts`
  - `excluded_window_counts`
  - `warning_messages`
- Added helper:
  - `is_ignored_shhs_respiratory_event_label()`
- Updated tests to cover:
  - normal buffer exclusion
  - apnea/hypopnea conflict and tie behavior
  - manifest rule fields
  - ignored label counts
  - unknown label counts and warning messages
- Real local Profusion XML smoke on authorized sample:
  - XML: `../data/raw/shhs_sample/polysomnography/annotations-events-profusion/shhs1/shhs1-200001-profusion.xml`
  - Candidate windows: `1084`
  - Included windows: `1015`
  - Excluded windows: `69`
  - Included class counts: `normal_breathing=877`, `hypopnea=135`,
    `suspected_apnea=3`
  - Excluded window counts: `near_abnormal_event=69`
  - Target label counts: `hypopnea=85`, `suspected_apnea=2`
  - Ignored label counts: `Arousal ()=183`, `SpO2 artifact=29`,
    `SpO2 desaturation=71`
  - Unknown label counts: none
- Verification history:
  - `python -m py_compile sleepagent/preprocessing/shhs_label_mapping.py sleepagent/preprocessing/shhs_respiratory_events.py sleepagent/preprocessing/__init__.py tests/test_shhs_respiratory_events.py` passed.
  - `python -m pytest tests/test_shhs_respiratory_events.py tests/test_shhs_label_mapping.py tests/test_shhs_annotations.py tests/test_respiratory_model_contract.py` passed with `20 passed`.

### Task 0037: Stage 5 respiratory EDF signal window extraction

Status: completed

- Added Stage 5 EDF signal window extraction module:
  - `sleepagent/preprocessing/shhs_respiratory_signals.py`
- Default respiratory EDF channels:
  - `THOR RES`
  - `ABDO RES`
- Added dataclasses:
  - `SHHSRespiratorySignalWindow`
  - `SHHSRespiratorySignalWindowSequence`
  - `SHHSRespiratorySignalManifest`
- Added helpers:
  - `extract_shhs_respiratory_signal_windows()`
  - `build_shhs_respiratory_signal_manifest()`
  - `prepare_respiratory_signal_runtime_environment()`
- Signal window contract:
  - Uses XML-derived Stage 5 label windows as the source of truth.
  - Extracts channel-first signal windows from EDF with shape
    `(input_channels, samples)`.
  - Preserves included/excluded label-window flags.
  - Manifest is JSON-safe and does not embed raw signal arrays.
- Added tests:
  - `tests/test_shhs_respiratory_signals.py`
- Real local EDF/XML smoke on authorized sample:
  - EDF: `../data/raw/shhs_sample/polysomnography/edfs/shhs1/shhs1-200001.edf`
  - XML: `../data/raw/shhs_sample/polysomnography/annotations-events-profusion/shhs1/shhs1-200001-profusion.xml`
  - Python: `/home/wz/miniconda3/envs/yasa/bin/python`
  - Channels: `THOR RES`, `ABDO RES`
  - Sampling rate: `125.0 Hz`
  - Samples per 30-second window: `3750`
  - Smoke limit: first `5` windows
  - Each smoke window data shape: `(2, 3750)`
  - First `5` smoke windows were included `normal_breathing`
- Verification history:
  - `python -m py_compile sleepagent/preprocessing/shhs_respiratory_signals.py sleepagent/preprocessing/__init__.py tests/test_shhs_respiratory_signals.py` passed.
  - `python -m pytest tests/test_shhs_respiratory_signals.py tests/test_shhs_respiratory_events.py tests/test_shhs_label_mapping.py tests/test_shhs_annotations.py tests/test_respiratory_model_contract.py` passed with `23 passed`.

### Task 0038: Stage 5 local NPZ respiratory dataset writer

Status: completed

- Added local derived dataset writing for Stage 5 respiratory windows:
  - `write_shhs_respiratory_signal_dataset_npz()`
- Added dataset manifest schema:
  - `stage5.respiratory_npz_dataset_manifest.v1`
- Added dataset class order:
  - `normal_breathing`
  - `hypopnea`
  - `suspected_apnea`
- NPZ arrays:
  - `x`: float signal windows shaped `(window_count, input_channels, samples)`
  - `y`: int64 class indices
  - `start_seconds`: float64 window start offsets
  - `included_mask`: boolean training-inclusion flags
  - `class_order`: string labels for decoding `y`
  - `channel_names`: EDF channel names
- The writer defaults to `included_only=True`, so excluded candidate normal
  windows are not written to the training dataset unless explicitly requested.
- Added `numpy` as a direct project dependency because Stage 5 dataset writing
  now depends on it.
- Updated tests:
  - `tests/test_shhs_respiratory_signals.py`
- Real local full-record NPZ smoke on authorized sample:
  - EDF: `../data/raw/shhs_sample/polysomnography/edfs/shhs1/shhs1-200001.edf`
  - XML: `../data/raw/shhs_sample/polysomnography/annotations-events-profusion/shhs1/shhs1-200001-profusion.xml`
  - Output:
    `../data/processed/sleepagent/stage5/shhs1-200001_resp_windows_included.npz`
  - Python: `/home/wz/miniconda3/envs/yasa/bin/python`
  - Dataset schema: `stage5.respiratory_npz_dataset_manifest.v1`
  - Channels: `THOR RES`, `ABDO RES`
  - Sampling rate: `125.0 Hz`
  - Samples per window: `3750`
  - Included windows: `1015`
  - `x` shape: `(1015, 2, 3750)`, dtype `float32`
  - `y` shape: `(1015,)`, dtype `int64`
  - Class counts: `normal_breathing=877`, `hypopnea=135`,
    `suspected_apnea=3`
  - Local file size: about `27M`
- Verification history:
  - `python -m py_compile sleepagent/preprocessing/shhs_respiratory_signals.py sleepagent/preprocessing/shhs_respiratory_events.py sleepagent/preprocessing/shhs_label_mapping.py sleepagent/preprocessing/__init__.py tests/test_shhs_respiratory_signals.py` passed.
  - `python -m pytest tests/test_shhs_respiratory_signals.py tests/test_shhs_respiratory_events.py tests/test_shhs_label_mapping.py tests/test_shhs_annotations.py tests/test_respiratory_model_contract.py` passed with `25 passed`.

### Task 0039: Stage 5 record-level split manifest

Status: completed

- Added deterministic train/validation/test split manifest support:
  - `build_shhs_respiratory_dataset_split_manifest()`
- Added split manifest schema:
  - `stage5.respiratory_dataset_split_manifest.v1`
- Split strategy:
  - `record_level_stable_hash`
  - default ratios: `train=0.70`, `val=0.15`, `test=0.15`
  - whole NPZ dataset files are assigned to splits to reduce adjacent-window
    leakage
- For fewer than three record-level datasets, the manifest assigns all datasets
  to `train` and records a warning that validation/test are empty.
- Added tests for:
  - deterministic multi-record split counts
  - single-record smoke warning
  - invalid split ratio rejection
- Real local split smoke for the current single-record dataset:
  - Dataset:
    `../data/processed/sleepagent/stage5/shhs1-200001_resp_windows_included.npz`
  - Split manifest schema: `stage5.respiratory_dataset_split_manifest.v1`
  - Split strategy: `record_level_stable_hash`
  - Split counts: `train=1`, `val=0`, `test=0`
  - Window counts: `train=1015`, `val=0`, `test=0`
  - Train class counts: `normal_breathing=877`, `hypopnea=135`,
    `suspected_apnea=3`
  - Warning: fewer than three record-level datasets are available, so
    validation/test splits are empty for this smoke dataset.
- Verification history:
  - `python -m py_compile sleepagent/preprocessing/shhs_respiratory_signals.py sleepagent/preprocessing/__init__.py tests/test_shhs_respiratory_signals.py` passed.
  - `python -m pytest tests/test_shhs_respiratory_signals.py` passed with `8 passed`.

### Task 0040: Stage 6 respiratory NPZ dataset loader

Status: completed

- Added the first Stage 6 training utility:
  - `sleepagent.training.respiratory_dataset`
- Added `load_respiratory_npz_arrays()` for validated loading of Stage 5 NPZ
  datasets.
- Added `RespiratoryNpzTorchDataset`, a PyTorch Dataset-compatible wrapper
  that defers importing PyTorch until item access.
- Loader validation now checks:
  - required NPZ arrays: `x`, `y`, and `class_order`
  - model input channel count
  - samples per window
  - class order compatibility with `RespiratoryCnnBiLstmConfig`
  - matching window counts across arrays
  - label-index bounds
- Added tests:
  - `tests/test_respiratory_npz_dataset.py`
- Updated docs:
  - `docs/DATA_CONTRACTS.md`
  - `TASK_LOG.md`
- Verification history:
  - `python -m py_compile sleepagent/training/__init__.py sleepagent/training/respiratory_dataset.py tests/test_respiratory_npz_dataset.py` passed.
  - `python -m pytest tests/test_respiratory_npz_dataset.py` passed with `4 passed, 1 skipped` because PyTorch is not installed in the default environment.
  - `python -m pytest tests/test_respiratory_npz_dataset.py tests/test_respiratory_model_contract.py tests/test_shhs_respiratory_signals.py` passed with `14 passed, 1 skipped`.
  - Real local loader smoke on `../data/processed/sleepagent/stage5/shhs1-200001_resp_windows_included.npz` loaded `x` shape `(1015, 2, 3750)` with class counts `normal_breathing=877`, `hypopnea=135`, `suspected_apnea=3`.
  - Full `python -m pytest` was attempted, but it hung in the sandbox near `tests/test_health.py`, consistent with the known FastAPI/TestClient sandbox issue.

### Task 0041: Stage 6 single-epoch respiratory training smoke loop

Status: completed

- Reviewed the Stage 6 dataset loader behavior:
  - `included_mask` is loaded and length-validated, but currently does not
    filter `RespiratoryNpzTorchDataset` samples.
  - `RespiratoryNpzTorchDataset.__getitem__()` returns `(window, label)`.
  - `window` is a `torch.float32` tensor shaped `(input_channels, samples)`.
  - `label` is a scalar `torch.long` tensor.
- Added direct tests for:
  - `x/y` length mismatch
  - `start_seconds` length mismatch
  - `included_mask` length mismatch
  - source NPZ dtype conversion into `torch.float32` and `torch.long`
- Added the first Stage 6 supervised training smoke loop:
  - `sleepagent.training.respiratory_training.train_respiratory_single_epoch_smoke()`
- Added `RespiratoryTrainingSmokeResult` with:
  - `epoch_count`
  - `batch_count`
  - `example_count`
  - `initial_loss`
  - `final_loss`
  - `mean_loss`
  - `class_counts`
- The training smoke loop uses:
  - `RespiratoryNpzTorchDataset`
  - `RespiratoryCnnBiLstm`
  - `torch.utils.data.DataLoader`
  - `torch.optim.Adam`
  - `torch.nn.CrossEntropyLoss`
- Added tests:
  - `tests/test_respiratory_training_smoke.py`
  - updated `tests/test_respiratory_npz_dataset.py`
- Updated docs:
  - `docs/DATA_CONTRACTS.md`
  - `TASK_LOG.md`
- Verification history:
  - `python -m py_compile sleepagent/training/__init__.py sleepagent/training/respiratory_dataset.py sleepagent/training/respiratory_training.py tests/test_respiratory_npz_dataset.py tests/test_respiratory_training_smoke.py` passed.
  - `python -m pytest tests/test_respiratory_npz_dataset.py tests/test_respiratory_training_smoke.py tests/test_respiratory_model_contract.py tests/test_respiratory_cnn_bilstm.py` passed with `8 passed, 3 skipped` in the default environment because PyTorch is not installed there.
  - `/home/wz/miniconda3/envs/stress/bin/python` has PyTorch `2.2.1+cu121`, but no pytest.
  - Real PyTorch script smoke using `/home/wz/miniconda3/envs/stress/bin/python` generated a tiny `/tmp` NPZ and ran one epoch with `max_batches=2`: `batch_count=2`, `example_count=4`, `initial_loss=1.061985731124878`, `final_loss=1.2292358875274658`, `mean_loss=1.1456108093261719`.

### Task 0042: Stage 6 respiratory evaluation helper

Status: completed

- Added Stage 6 model-output evaluation helper:
  - `sleepagent.training.respiratory_evaluation.evaluate_respiratory_model_outputs()`
- Added `RespiratoryEvaluationResult` with:
  - existing `RespiratoryDetectionMetrics`
  - decoded `y_true`
  - decoded `y_pred`
  - string-keyed score rows
- The helper accepts:
  - label-index `y_true`
  - logits or probability scores shaped `(examples, classes)`
  - default or custom class order
  - NumPy arrays, Python sequences, or torch tensors
- Logits are converted with a stable softmax when `from_logits=True`.
- Predictions are decoded with score-row argmax.
- Metrics are computed by the existing respiratory metric module, preserving:
  - abnormal-event Recall
  - multiclass one-vs-rest macro AUC
  - macro F1
  - per-class Recall
- Added tests:
  - `tests/test_respiratory_evaluation.py`
- Updated docs:
  - `docs/DATA_CONTRACTS.md`
  - `TASK_LOG.md`
- Verification history:
  - `python -m py_compile sleepagent/training/__init__.py sleepagent/training/respiratory_evaluation.py tests/test_respiratory_evaluation.py` passed.
  - `python -m pytest tests/test_respiratory_evaluation.py tests/test_respiratory_metrics.py tests/test_respiratory_npz_dataset.py` passed with `20 passed, 1 skipped`.
  - `python -m pytest tests/test_respiratory_evaluation.py tests/test_respiratory_training_smoke.py tests/test_respiratory_npz_dataset.py tests/test_respiratory_model_contract.py tests/test_respiratory_cnn_bilstm.py` passed with `12 passed, 3 skipped`.
  - Real PyTorch tensor smoke using `/home/wz/miniconda3/envs/stress/bin/python` passed torch tensors directly into `evaluate_respiratory_model_outputs()` and produced Recall `0.75`, AUC `0.875`, and F1 `0.5222222222222223`.

### Task 0043: Stage 6 respiratory inference helper

Status: completed

- Added Stage 6 inference helper:
  - `sleepagent.training.respiratory_inference.infer_respiratory_window()`
  - `sleepagent.training.respiratory_inference.infer_respiratory_npz()`
- Added result dataclasses:
  - `RespiratoryPrediction`
  - `RespiratoryInferenceResult`
- Inference helper behavior:
  - accepts an already constructed model instance
  - does not load checkpoints
  - accepts one channel-first window or one Stage 5 NPZ dataset
  - uses `model.config` when available, otherwise falls back to default
    `RespiratoryCnnBiLstmConfig`
  - runs under `torch.no_grad()`
  - temporarily switches the model to eval mode and restores training mode
    afterward when needed
  - decodes logits with softmax and argmax
  - returns predicted labels and string-keyed probabilities
- Added tests:
  - `tests/test_respiratory_inference.py`
- Updated docs:
  - `docs/DATA_CONTRACTS.md`
  - `TASK_LOG.md`
- Verification history:
  - `python -m py_compile sleepagent/training/__init__.py sleepagent/training/respiratory_inference.py tests/test_respiratory_inference.py` passed.
  - `python -m pytest tests/test_respiratory_inference.py tests/test_respiratory_evaluation.py tests/test_respiratory_npz_dataset.py tests/test_respiratory_model_contract.py` passed with `12 passed, 2 skipped` in the default environment because PyTorch is not installed there.
  - Real PyTorch inference smoke using `/home/wz/miniconda3/envs/stress/bin/python` constructed a small `RespiratoryCnnBiLstm`, ran one-window inference and tiny NPZ inference, produced one single-window label `hypopnea`, returned `3` NPZ predictions, and every probability row summed to `1.0`.

### Task 0044: Stage 6 train/evaluate/infer CLI smoke script

Status: completed

- Reviewed and tightened Stage 6 helper contracts:
  - Evaluation now handles unavailable AUC by returning metrics with
    `auc=None` and `auc_warning_message`.
  - Inference predictions now include `start_second` when supplied directly or
    present in the NPZ `start_seconds` array.
  - Inference shape checks are strict for input rank, channel count, sample
    count, and model output class dimension.
  - Checkpoint save/load remains intentionally unimplemented and is the next
    Stage 6 integration task before real-data model runs.
- Added tiny Stage 6 smoke CLI:
  - `scripts/run_respiratory_stage6_smoke.py`
- CLI behavior:
  - creates `/tmp/sleepagent_stage6_cli_smoke.npz` when no dataset is passed
  - constructs one `RespiratoryCnnBiLstm`
  - trains that same model for one tiny epoch
  - evaluates logits with `evaluate_respiratory_model_outputs()`
  - runs NPZ inference with `infer_respiratory_npz()`
  - prints JSON schema `stage6.respiratory_smoke.v1`
- Added tests:
  - `tests/test_respiratory_stage6_smoke_script.py`
  - updated `tests/test_respiratory_evaluation.py`
  - updated `tests/test_respiratory_inference.py`
- Updated docs:
  - `docs/DATA_CONTRACTS.md`
  - `TASK_LOG.md`
- Verification history:
  - `python -m py_compile scripts/run_respiratory_stage6_smoke.py sleepagent/training/respiratory_evaluation.py sleepagent/training/respiratory_inference.py sleepagent/training/respiratory_training.py tests/test_respiratory_evaluation.py tests/test_respiratory_inference.py tests/test_respiratory_stage6_smoke_script.py` passed.
  - `python -m pytest tests/test_respiratory_stage6_smoke_script.py tests/test_respiratory_evaluation.py tests/test_respiratory_inference.py tests/test_respiratory_training_smoke.py tests/test_respiratory_npz_dataset.py tests/test_respiratory_model_contract.py` passed with `14 passed, 3 skipped` in the default environment because PyTorch is not installed there.
  - Real CLI smoke using `/home/wz/miniconda3/envs/stress/bin/python scripts/run_respiratory_stage6_smoke.py` generated `/tmp/sleepagent_stage6_cli_smoke.npz`, trained one tiny epoch with `batch_count=2` and `example_count=4`, evaluated Recall `1.0`, AUC `0.625`, F1 `0.16666666666666666`, and returned 6 inference predictions with `start_second` values.

### Task 0045: Stage 6 respiratory checkpoint save/load helpers

Status: completed

- Added checkpoint helpers:
  - `sleepagent.training.respiratory_checkpoint.save_respiratory_checkpoint()`
  - `sleepagent.training.respiratory_checkpoint.load_respiratory_checkpoint()`
- Added checkpoint metadata dataclass:
  - `RespiratoryCheckpoint`
- Checkpoint schema:
  - `stage6.respiratory_checkpoint.v1`
- Checkpoint payload includes:
  - schema version
  - model config
  - caller metadata
  - PyTorch `state_dict`
- Checkpoint save/load behavior:
  - checkpoint paths must end with `.pt` or `.pth`
  - load rebuilds `RespiratoryCnnBiLstm` from saved config
  - load restores `state_dict` for inference/evaluation reuse
- Updated tiny Stage 6 smoke CLI:
  - trains one model
  - saves a checkpoint
  - loads the checkpoint
  - evaluates and runs inference with the loaded model
- Added tests:
  - `tests/test_respiratory_checkpoint.py`
- Updated docs:
  - `docs/DATA_CONTRACTS.md`
  - `TASK_LOG.md`
- Verification history:
  - `python -m py_compile sleepagent/training/__init__.py sleepagent/training/respiratory_checkpoint.py tests/test_respiratory_checkpoint.py scripts/run_respiratory_stage6_smoke.py` passed.
  - `python -m pytest tests/test_respiratory_checkpoint.py tests/test_respiratory_stage6_smoke_script.py tests/test_respiratory_inference.py tests/test_respiratory_evaluation.py tests/test_respiratory_npz_dataset.py tests/test_respiratory_model_contract.py` passed with `14 passed, 3 skipped` in the default environment because PyTorch is not installed there.
  - Real CLI smoke using `/home/wz/miniconda3/envs/stress/bin/python scripts/run_respiratory_stage6_smoke.py` saved and loaded `/tmp/sleepagent_stage6_cli_smoke_checkpoint.pt`, then evaluated Recall `1.0`, AUC `0.625`, F1 `0.16666666666666666`, and returned 6 inference predictions with `start_second` values.

### Task 0046: Stage 6 real local NPZ smoke and wrap-up

Status: completed

- Ran the Stage 6 smoke pipeline on the real local Stage 5 derived NPZ dataset:
  - `../data/processed/sleepagent/stage5/shhs1-200001_resp_windows_included.npz`
- Used bounded training to keep the run smoke-level:
  - `batch_size=32`
  - `max_batches=1`
- Wrote checkpoint outside the code repository:
  - `../data/processed/sleepagent/stage6/shhs1-200001_stage6_smoke_checkpoint.pt`
- Real local smoke result:
  - train `batch_count=1`
  - train `example_count=32`
  - train loss `1.137016773223877`
  - evaluation Recall `1.0`
  - evaluation AUC `0.4568704943663217`
  - evaluation F1 `0.022646353033093366`
  - inference prediction count `1015`
  - first predictions include `start_second`
- Important interpretation:
  - This is a smoke run on an effectively untrained model.
  - These values are not respiratory model performance results.
  - Stage 6 MVP scope is train/evaluate/infer plumbing, not optimized model
    performance.
- Updated stage docs:
  - `PROJECT_PLAN.md`
  - `docs/DEVELOPMENT_ROADMAP.md`
  - `TASK_LOG.md`
- Verification history:
  - `python -m pytest tests/test_respiratory_checkpoint.py tests/test_respiratory_stage6_smoke_script.py tests/test_respiratory_inference.py tests/test_respiratory_evaluation.py tests/test_respiratory_npz_dataset.py tests/test_respiratory_model_contract.py` passed with `14 passed, 3 skipped` in the default environment because PyTorch is not installed there.
- Stage 6 is complete for the current MVP smoke scope.

### Task 0047: Stage 6 20-record SHHS respiratory demo experiment

Status: completed

- Added `scripts/run_respiratory_stage6_experiment.py`.
- The script supports:
  - `--prepare-only` for MNE-capable preprocessing environments.
  - `--train-only` for PyTorch-capable training environments.
  - `--context-only` for regenerating the report/Agent context from an existing summary.
- Fixed demo split contract:
  - 20 complete SHHS records exactly.
  - train: `14`
  - val: `3`
  - test: `3`
- Added output schemas:
  - split manifest: `stage6.respiratory_20_record_split.v1`
  - experiment summary: `stage6.respiratory_20_record_experiment.v1`
  - report/Agent context: `stage6.respiratory_20_record_report_agent_context.v1`
- Added `tests/test_respiratory_stage6_experiment_script.py` for:
  - script `--help` import safety without model extras.
  - exact `14/3/3` split assignment.
  - non-20-record rejection.
  - report/Agent caveat generation for normal-only demo predictions.
- Updated `docs/DATA_CONTRACTS.md` with the Stage 6 20-record experiment artifact contract.
- Updated `PROJECT_PLAN.md`, `docs/DEVELOPMENT_ROADMAP.md`, and `TASK_LOG.md`
  so Stage 6 completion reflects the 20-record demo instead of only the
  single-record smoke.
- Real local preparation run:
  - command: `/home/wz/miniconda3/envs/yasa/bin/python scripts/run_respiratory_stage6_experiment.py --prepare-only --record-count 20 --dataset-dir ../data/processed/sleepagent/stage5/resp20 --sample-root ../data/raw/shhs_sample --zip ../data/raw/shhs.zip`
  - records: `shhs1-200001` through `shhs1-200020`
  - split manifest: `../data/processed/sleepagent/stage5/resp20/resp20_split_manifest.json`
  - NPZ dataset directory size after prepare: about `483M`
- Real local training/evaluation/inference run:
  - command: `/home/wz/miniconda3/envs/stress/bin/python scripts/run_respiratory_stage6_experiment.py --train-only --record-count 20 --dataset-dir ../data/processed/sleepagent/stage5/resp20 --out-dir ../data/processed/sleepagent/stage6/resp20 --epochs 5 --batch-size 64`
  - epochs requested: `5`
  - best epoch selected by validation F1: `1`
  - checkpoint: `../data/processed/sleepagent/stage6/resp20/best_resp20_checkpoint.pt`
  - experiment summary: `../data/processed/sleepagent/stage6/resp20/resp20_experiment_summary.json`
  - report/Agent context: `../data/processed/sleepagent/stage6/resp20/resp20_report_agent_context.json`
- Epoch history:
  - epoch 1: train loss `0.658172`, val F1 `0.221172`, best epoch `1`
  - epoch 2: train loss `0.604271`, val F1 `0.221172`, best epoch `1`
  - epoch 3: train loss `0.600706`, val F1 `0.221172`, best epoch `1`
  - epoch 4: train loss `0.600403`, val F1 `0.221172`, best epoch `1`
  - epoch 5: train loss `0.627045`, val F1 `0.221172`, best epoch `1`
- Final validation metrics:
  - Recall `0.0`
  - AUC `0.599717368951047`
  - F1 `0.22117152613606514`
  - per-class Recall: `normal_breathing=1.0`, `hypopnea=0.0`, `suspected_apnea=0.0`
- Final test metrics:
  - Recall `0.0`
  - AUC `0.5442503554085457`
  - F1 `0.28623256395821606`
  - per-class Recall: `normal_breathing=1.0`, `hypopnea=0.0`, `suspected_apnea=0.0`
- Test record summaries:
  - `shhs1-200018`: `991` windows, true `normal_breathing=771`, `hypopnea=220`; predicted `normal_breathing=991`; record F1 `0.29171396140749145`
  - `shhs1-200019`: `1042` windows, true `normal_breathing=963`, `hypopnea=70`, `suspected_apnea=9`; predicted `normal_breathing=1042`; record F1 `0.3201995012468828`
  - `shhs1-200020`: `1008` windows, true `normal_breathing=554`, `hypopnea=451`, `suspected_apnea=3`; predicted `normal_breathing=1008`; record F1 `0.2364489970123773`
- Report/Agent handoff interpretation:
  - The checkpoint is useful for demonstrating the Stage 6 train/evaluate/infer/checkpoint/report-context pipeline.
  - The model predicted `normal_breathing` for every test window.
  - Reports and Agents must not present this checkpoint as evidence that respiratory abnormality is absent.
  - The checkpoint is not suitable for clinical screening or performance benchmarking.
- Verification history:
  - `python -m py_compile scripts/run_respiratory_stage6_experiment.py tests/test_respiratory_stage6_experiment_script.py` passed.
  - `python -m pytest tests/test_respiratory_stage6_experiment_script.py tests/test_respiratory_stage6_smoke_script.py tests/test_respiratory_checkpoint.py tests/test_respiratory_inference.py tests/test_respiratory_evaluation.py tests/test_respiratory_npz_dataset.py tests/test_respiratory_model_contract.py` passed with `19 passed, 3 skipped` in the default environment because PyTorch is not installed there.
- Stage 6 is complete for the current MVP demo scope.

### Task 0048: Stage 7 minimal RAG knowledge chunk contract

Status: completed

- Started Stage 7 with a small, replaceable RAG foundation rather than wiring
  Chroma or LLM generation immediately.
- Added report RAG schemas:
  - `ReportKnowledgeChunk`
  - `RetrievedReportKnowledgeChunk`
- Added `sleepagent/services/report_knowledge.py` with:
  - schema version `stage7.report_knowledge_chunk.v1`
  - internal seed chunks for sleep efficiency, AHI/respiratory events, urgent
    symptom boundaries, and the Stage 6 respiratory demo caveat
  - deterministic lexical retrieval via `retrieve_report_knowledge()`
- Exported the new schemas and service helpers through package `__init__` files.
- Documented the Stage 7 knowledge contract in `docs/DATA_CONTRACTS.md`.
- Added `tests/test_report_knowledge.py` for strict schema behavior, retrieval
  ranking, empty queries, and `top_k` validation.
- Verification history:
  - `python -m py_compile sleepagent/schemas/report.py sleepagent/services/report_knowledge.py tests/test_report_knowledge.py` passed.
  - `python -m pytest tests/test_report_knowledge.py tests/test_report_templates.py tests/test_mock_json_contracts.py` passed with `10 passed`.

### Task 0049: Connect mock report generation to local retrieval context

Status: completed

- Updated `generate_mock_sleep_report()` so the existing template report path now
  retrieves local Stage 7 seed knowledge before rendering text.
- Added `ReportSummary`-derived retrieval query terms for:
  - sleep efficiency
  - AHI
  - hypopnea
  - suspected apnea
  - respiratory events
  - elevated-risk medical safety terms when risk is moderate or high
- Folded retrieved context into existing response fields:
  - `elder_report`
  - `professional_report`
  - `care_suggestions`
- Kept the `/mock-report` JSON response contract stable:
  - no new top-level fields
  - no new `summary` fields
- Added high-risk safety-context behavior so urgent symptom knowledge can add a
  clear timely-care / emergency-evaluation suggestion.
- Updated `docs/DATA_CONTRACTS.md` to note that `/mock-report` is now locally
  retrieval-augmented while preserving the same payload shape.
- Added report template tests for:
  - retrieval context appearing in elder-friendly and professional reports
  - retrieved chunk IDs appearing in the professional report
  - high-risk urgent-care safety suggestions
- Verification history:
  - `python -m py_compile sleepagent/services/report_templates.py tests/test_report_templates.py` passed.
  - `python -m pytest tests/test_report_templates.py tests/test_report_knowledge.py tests/test_mock_json_contracts.py` passed with `11 passed`.

### Task 0050: Stage 7 Chroma adapter boundary

Status: completed

- Added `sleepagent/services/report_chroma.py`.
- Added `ChromaReportKnowledgeAdapter` as the first Chroma-facing boundary.
- Added `ChromaUnavailableError` for missing optional Chroma dependency.
- Added default collection name:
  - `sleepagent_report_knowledge`
- Adapter behavior:
  - `upsert_chunks()` accepts `ReportKnowledgeChunk` objects.
  - `query()` returns `RetrievedReportKnowledgeChunk` objects.
  - Chroma collection payloads are isolated inside the adapter.
  - Chroma distance is converted to local score via `1 / (1 + distance)`.
  - `chromadb` is lazy-loaded only when building a real client.
  - tests can inject fake Chroma-like clients.
- Added optional dependency extra:
  - `rag = ["chromadb>=0.5"]`
- Exported adapter symbols from `sleepagent/services/__init__.py`.
- Added `tests/test_report_chroma.py` covering:
  - upsert payload mapping
  - query result conversion back into SleepAgent contracts
  - blank query behavior
  - `top_k` validation
  - empty collection-name validation
- Updated `docs/DATA_CONTRACTS.md` with the Chroma adapter boundary and metadata
  mapping.
- Verification history:
  - `python -m py_compile sleepagent/services/report_chroma.py tests/test_report_chroma.py sleepagent/services/__init__.py` passed.
  - `python -m pytest tests/test_report_chroma.py tests/test_report_knowledge.py tests/test_report_templates.py tests/test_mock_json_contracts.py` passed with `16 passed`.

### Task 0051: Chroma seed/index helper and real Chroma smoke

Status: completed

- Added `HashEmbeddingFunction` to `sleepagent/services/report_chroma.py`.
  - deterministic local hash embeddings
  - no external embedding model download
  - Chroma 1.5-compatible methods:
    - `__call__`
    - `embed_query`
    - `embed_documents`
    - `name`
- Added `ReportChromaSeedResult`.
- Added `seed_default_report_chroma_knowledge()` helper.
- Added CLI:
  - `scripts/seed_report_chroma.py`
- CLI behavior:
  - creates or reuses a persistent Chroma directory
  - indexes the built-in Stage 7 report knowledge chunks
  - runs one smoke query
  - prints JSON with index and retrieval summary
- Updated `tests/test_report_chroma.py` with:
  - hash embedding determinism
  - helper boundary test with fake Chroma client
  - script `--help` import safety without requiring Chroma
- Updated `docs/DATA_CONTRACTS.md` with seed helper and CLI notes.
- Installation:
  - initial sandbox install failed because DNS/network was blocked.
  - escalated install command succeeded:
    `python -m pip install -e ".[rag]"`
  - pip warning: `anaconda-cli-base 0.6.0 requires click<8.2`, while Chroma
    installed `click 8.3.3`.
- Real Chroma smoke:
  - command:
    `python scripts/seed_report_chroma.py --persist-dir /tmp/sleepagent_stage7_report_chroma_smoke_v3 --query "ahi hypopnea suspected apnea" --top-k 3`
  - indexed chunk count: `4`
  - top result: `ahi-basic`
  - returned results:
    - `ahi-basic`
    - `urgent-symptoms-safety`
    - `stage6-demo-caveat`
- Verification history:
  - `python -m py_compile sleepagent/services/report_chroma.py sleepagent/services/__init__.py scripts/seed_report_chroma.py tests/test_report_chroma.py` passed.
  - `python -m pytest tests/test_report_chroma.py tests/test_report_knowledge.py tests/test_report_templates.py tests/test_mock_json_contracts.py` passed with `19 passed`.

### Task 0052: Report retriever selection layer

Status: completed

- Added `sleepagent/services/report_retrievers.py`.
- Added retriever mode/config primitives:
  - `ReportRetrieverMode`
  - `ReportRetrieverConfig`
  - `ReportKnowledgeRetriever`
- Added retriever implementations:
  - `InMemoryReportKnowledgeRetriever`
  - `ChromaReportKnowledgeRetriever`
- Added env-based configuration:
  - `SLEEPAGENT_REPORT_RETRIEVER`
  - `SLEEPAGENT_REPORT_CHROMA_DIR`
  - `SLEEPAGENT_REPORT_CHROMA_COLLECTION`
- Added selection helpers:
  - `load_report_retriever_config_from_env()`
  - `build_report_knowledge_retriever()`
  - `retrieve_report_context()`
- Updated `generate_mock_sleep_report()` so it uses the selection layer by
  default while also accepting explicit `retriever` or `retriever_config`
  arguments for tests and future wiring.
- Preserved `/mock-report` response shape:
  - no new top-level fields
  - no new `summary` fields
- Added `tests/test_report_retrievers.py`.
- Extended report template tests to verify injected retriever usage without
  response contract changes.
- Verified Chroma selection smoke against the existing local Chroma smoke index:
  - `SLEEPAGENT_REPORT_RETRIEVER=chroma`
  - `SLEEPAGENT_REPORT_CHROMA_DIR=/tmp/sleepagent_stage7_report_chroma_smoke_v3`
  - generated report preserved the original top-level payload keys and used
    retrieved context in `professional_report`.
- Updated `docs/DATA_CONTRACTS.md` and `TASK_LOG.md`.
- Verification history:
  - `python -m py_compile sleepagent/services/report_retrievers.py sleepagent/services/report_templates.py sleepagent/services/__init__.py tests/test_report_retrievers.py tests/test_report_templates.py` passed.
  - `python -m pytest tests/test_report_retrievers.py tests/test_report_templates.py tests/test_report_chroma.py tests/test_report_knowledge.py tests/test_mock_json_contracts.py` passed with `26 passed`.

### Task 0053: DeepSeek report draft schema and fallback scaffold

Status: completed

- Added `LLMReportDraft` to `sleepagent/schemas/report.py`.
  - `schema_version` must be exactly `stage7.llm_report_draft.v1`.
  - `elder_report` and `professional_report` must be non-empty.
  - `care_suggestions` and `safety_warnings` are lists of non-empty strings.
  - extra fields are forbidden.
- Added `sleepagent/services/report_llm.py`.
- Added DeepSeek-facing constants/config:
  - `DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"`
  - `DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"`
  - `DeepSeekReportGeneratorConfig`
- Added validation and conversion helpers:
  - `validate_llm_report_json()`
  - `build_report_from_llm_draft()`
  - `generate_sleep_report_with_llm_fallback()`
- Added request-preview scaffolding:
  - `build_deepseek_report_messages()`
  - `build_deepseek_report_request_preview()`
- The request preview builds system/user messages and required JSON schema
  guidance but does not call DeepSeek or require an API key.
- Invalid or missing LLM output falls back to the current template/RAG path.
- Exported new schema and service helpers.
- Added `tests/test_report_llm.py` covering:
  - strict valid JSON parsing
  - extra-field rejection
  - wrong schema-version rejection
  - valid LLM draft conversion to `MockSleepReport`
  - invalid JSON fallback to template/RAG report
  - API-key-free DeepSeek request preview
- Updated `docs/DATA_CONTRACTS.md` and `TASK_LOG.md`.
- Verification history:
  - `python -m py_compile sleepagent/schemas/report.py sleepagent/schemas/__init__.py sleepagent/services/report_llm.py sleepagent/services/__init__.py tests/test_report_llm.py` passed.
  - `python -m pytest tests/test_report_llm.py tests/test_report_retrievers.py tests/test_report_templates.py tests/test_report_chroma.py tests/test_report_knowledge.py tests/test_mock_json_contracts.py` passed with `32 passed`.

### Task 0054: DeepSeek API client boundary

Status: completed

- Extended `sleepagent/services/report_llm.py` with a real DeepSeek HTTP client
  boundary.
- Updated DeepSeek defaults according to current official API docs:
  - base URL: `https://api.deepseek.com`
  - chat completions path: `/chat/completions`
  - default model: `deepseek-v4-flash`
  - API key env var: `DEEPSEEK_API_KEY`
- Added client/error classes:
  - `DeepSeekChatClient`
  - `DeepSeekAPIError`
  - `DeepSeekMissingAPIKeyError`
- `DeepSeekChatClient.create_chat_completion()` sends OpenAI-compatible request
  payloads with:
  - bearer-token auth
  - `response_format={"type": "json_object"}`
  - `thinking={"type": "disabled"}` by default
  - `stream=False`
- `DeepSeekChatClient.create_report_draft()` extracts the first response choice
  message content and validates it with `validate_llm_report_json()`.
- Added `generate_sleep_report_with_deepseek_fallback()`:
  - valid DeepSeek draft -> existing `MockSleepReport` contract
  - missing key / HTTP failure / malformed response / invalid LLM JSON -> current
    template/RAG fallback
- Exported new client and error symbols from `sleepagent/services/__init__.py`.
- Extended `tests/test_report_llm.py` to cover:
  - missing API key behavior
  - OpenAI-compatible request payload shape
  - response JSON validation
  - invalid response fallback
  - successful DeepSeek draft conversion
  - DeepSeek failure fallback to template/RAG
- No live DeepSeek request was run because no API key is configured in this
  session.
- Verification history:
  - `python -m py_compile sleepagent/services/report_llm.py sleepagent/services/__init__.py tests/test_report_llm.py` passed.
  - `python -m pytest tests/test_report_llm.py tests/test_report_retrievers.py tests/test_report_templates.py tests/test_report_chroma.py tests/test_report_knowledge.py tests/test_mock_json_contracts.py` passed with `37 passed`.

### Task 0055: Guarded DeepSeek live smoke CLI

Status: completed

- Added `scripts/run_deepseek_report_smoke.py`.
- The script builds one mock analysis result, prepares the DeepSeek report
  prompt, calls `DeepSeekChatClient.create_report_draft()`, validates the
  returned draft, and converts it to the existing report response contract.
- The script is guarded:
  - no `DEEPSEEK_API_KEY` -> exits with code `2`
  - DeepSeek API/client failure -> exits with code `1`
  - success -> prints report-shape metadata only, not the full report text
- Added `--model`, `--api-key-env`, `--timeout-seconds`, `--duration-hours`, and
  `--seed` arguments.
- Extended `tests/test_report_llm.py` with a script `--help` import-safety test.
- Updated `docs/DATA_CONTRACTS.md` and `TASK_LOG.md`.
- Verification history:
  - `python -m py_compile sleepagent/services/report_llm.py sleepagent/services/__init__.py scripts/run_deepseek_report_smoke.py tests/test_report_llm.py` passed.
  - `env -u DEEPSEEK_API_KEY python scripts/run_deepseek_report_smoke.py` exited
    with code `2` and printed the missing-key message without making a live
    request.
  - `python -m pytest tests/test_report_llm.py tests/test_report_retrievers.py tests/test_report_templates.py tests/test_report_chroma.py tests/test_report_knowledge.py tests/test_mock_json_contracts.py` passed with `38 passed`.

### Task 0056: LLM report draft medical safety guard

Status: completed

- Extended `validate_llm_report_json()` with a conservative post-schema safety
  check for generated LLM draft text.
- The guard rejects draft text that contains:
  - explicit diagnosis or certainty language
  - advice that discourages medical care
  - direct self-treatment or medication instructions
- Cautious disclaimer language such as "不能替代医生诊断" remains allowed.
- Unsafe drafts continue to use the existing template/RAG fallback path through
  `generate_sleep_report_with_llm_fallback()` and the DeepSeek fallback boundary.
- Updated `docs/DATA_CONTRACTS.md` and `TASK_LOG.md`.
- Verification history:
  - `python -m py_compile sleepagent/services/report_llm.py sleepagent/services/__init__.py scripts/run_deepseek_report_smoke.py tests/test_report_llm.py` passed.
  - `python -m pytest tests/test_report_llm.py tests/test_report_retrievers.py tests/test_report_templates.py tests/test_report_chroma.py tests/test_report_knowledge.py tests/test_mock_json_contracts.py` passed with `41 passed`.
  - `timeout 30s python -m pytest tests/test_report_llm.py tests/test_report_retrievers.py tests/test_report_templates.py tests/test_report_chroma.py tests/test_report_knowledge.py tests/test_mock_json_contracts.py tests/test_mock_report_endpoint.py` passed with `43 passed` outside sandbox network isolation because FastAPI `TestClient` hangs under the sandbox.

### Task 0057: Real guarded DeepSeek live smoke

Status: completed

- Ran `scripts/run_deepseek_report_smoke.py` once with a user-provided
  `DEEPSEEK_API_KEY`.
- The key was passed only through process stdin/environment and was not written
  to code, docs, or logs.
- The live smoke returned valid report-shape metadata:
  - model: `deepseek-v4-flash`
  - record_id: `mock-shhs-0001`
  - subject_id: `mock-subject-0001`
  - risk_level: `moderate`
  - care_suggestion_count: `7`
  - response contract fields:
    `care_suggestions`, `elder_report`, `generated_at`, `medical_disclaimer`,
    `professional_report`, `summary`
- Updated `TASK_LOG.md`.
- Verification history:
  - `python scripts/run_deepseek_report_smoke.py` passed with exit code `0`.

### Task 0058: Opt-in backend LLM report endpoint

Status: completed

- Added `GET /mock-report/llm` as an explicit Stage 7 opt-in endpoint for LLM
  report experiments.
- Kept `GET /mock-report` unchanged.
- `/mock-report/llm` uses the same mock analysis query parameters and the same
  `MockSleepReport` response contract.
- Default behavior is deterministic and API-key-free:
  - `use_deepseek=false` returns the existing template/RAG report path.
- Live behavior is explicit:
  - `use_deepseek=true` calls `generate_sleep_report_with_deepseek_fallback()`.
  - Missing key, HTTP failure, malformed response, invalid LLM JSON, or unsafe
    LLM draft text still fall back to the deterministic template/RAG path.
- Added endpoint tests proving that the default path does not call the DeepSeek
  fallback and that opt-in wiring can be exercised with a monkeypatched fallback
  without making network requests.
- Updated `docs/DATA_CONTRACTS.md` and `TASK_LOG.md`.
- Verification history:
  - `python -m py_compile backend/main.py tests/test_mock_report_endpoint.py sleepagent/services/report_llm.py` passed.
  - `python -m pytest tests/test_report_llm.py tests/test_report_retrievers.py tests/test_report_templates.py tests/test_report_chroma.py tests/test_report_knowledge.py tests/test_mock_json_contracts.py` passed with `41 passed`.
  - `timeout 30s python -m pytest tests/test_health.py tests/test_mock_analysis_endpoint.py tests/test_mock_report_endpoint.py -vv` passed with `8 passed` outside sandbox network isolation because FastAPI `TestClient` hangs under the sandbox.

### Task 0059: Frontend opt-in DeepSeek report switch

Status: completed

- Added `frontend.api_client.build_mock_report_llm_url()`.
- Added `frontend.api_client.fetch_mock_report_llm()` for the explicit
  `/mock-report/llm` endpoint.
- Updated the Streamlit app with a default-off `DeepSeek report` checkbox:
  - unchecked: fetches `/mock-report`
  - checked: fetches `/mock-report/llm?use_deepseek=true`
- Kept ordinary frontend behavior API-key-free by default.
- Added frontend API client tests with monkeypatched HTTP calls, so no live
  DeepSeek request is made by the frontend tests.
- Updated `docs/DATA_CONTRACTS.md` and `TASK_LOG.md`.
- Verification history:
  - `python -m py_compile frontend/api_client.py frontend/app.py tests/test_frontend_api_client.py` passed.
  - `python -m pytest tests/test_frontend_api_client.py` passed with `7 passed`.
  - `python -m pytest tests/test_report_llm.py tests/test_report_retrievers.py tests/test_report_templates.py tests/test_report_chroma.py tests/test_report_knowledge.py tests/test_mock_json_contracts.py` passed with `41 passed`.

### Task 0060: Stage 8 Agent contracts and linear orchestration

Status: completed

- Added strict Stage 8 Agent schemas:
  - `SleepAgentOrchestrationRequest`
  - `SleepAgentOrchestrationResult`
  - `AgentStepTrace`
  - `DialogueTurn`
- Added initial rule-based Agent implementations:
  - `SleepAnalysisAgent`
  - `ReportAgent`
  - `DialogueAgent`
- Added `SleepAgentOrchestrator` and `run_sleep_agent_orchestration()` as the
  default deterministic linear runner.
- Kept DeepSeek report generation default-off and explicit opt-in.
- Added report-grounded dialogue responses and urgent symptom safety-boundary
  handling.
- Verification history:
  - `python -m py_compile sleepagent/schemas/agent.py sleepagent/schemas/__init__.py sleepagent/agents/sleep_analysis_agent.py sleepagent/agents/report_agent.py sleepagent/agents/dialogue_agent.py sleepagent/agents/orchestration.py sleepagent/agents/__init__.py tests/test_stage8_agent_orchestration.py` passed.
  - `python -m pytest tests/test_stage8_agent_orchestration.py -q` passed with
    `5 passed`.

### Task 0061: Optional LangGraph orchestration boundary

Status: completed

- Added optional LangGraph boundary in
  `sleepagent/agents/langgraph_orchestration.py`.
- Added:
  - `SleepAgentLangGraphState`
  - `build_sleep_agent_langgraph()`
  - `run_sleep_agent_langgraph_orchestration()`
  - `LangGraphUnavailableError`
- Added the `agent` optional dependency extra with `langgraph>=0.2`.
- LangGraph remains lazy-loaded; default imports and tests do not require it.
- Current graph nodes are:
  `sleep_analysis -> report -> dialogue -> build_result`.
- Verification history:
  - `python -m py_compile sleepagent/agents/langgraph_orchestration.py sleepagent/agents/__init__.py tests/test_stage8_langgraph_boundary.py` passed.
  - `python -m pytest tests/test_stage8_langgraph_boundary.py -q` passed with
    `4 passed`.

### Task 0062: Backend Agent orchestration endpoint

Status: completed

- Added `GET /agent/orchestrate` as an MVP smoke endpoint.
- Added `POST /agent/orchestrate` as the preferred formal Agent API shape with a
  `SleepAgentEndpointRequest` JSON body.
- Endpoint response model is `SleepAgentOrchestrationResult`.
- Default behavior uses the deterministic linear Agent runner.
- `use_langgraph=true` opts into optional LangGraph orchestration; missing
  LangGraph returns HTTP `503`.
- `use_deepseek_report=true` remains explicit opt-in.
- Updated step trace names to node-style values:
  `sleep_analysis`, `report`, `dialogue`, and `skip_dialogue`.
- Added endpoint safety coverage for urgent symptoms such as chest pain, severe
  breathing difficulty, and abnormal consciousness.
- Verification history:
  - `python -m py_compile backend/main.py sleepagent/schemas/agent.py sleepagent/schemas/__init__.py sleepagent/agents/orchestration.py sleepagent/agents/langgraph_orchestration.py tests/test_agent_orchestration_endpoint.py tests/test_stage8_agent_orchestration.py tests/test_stage8_langgraph_boundary.py` passed.
  - `timeout 30s python -m pytest tests/test_agent_orchestration_endpoint.py -q`
    passed with `8 passed` outside sandbox network isolation.
  - `timeout 30s python -m pytest tests/test_agent_orchestration_endpoint.py tests/test_mock_analysis_endpoint.py tests/test_mock_report_endpoint.py tests/test_health.py -q`
    passed with `16 passed` outside sandbox network isolation.

### Task 0063: Agent orchestration CLI smoke

Status: completed

- Added `scripts/run_agent_orchestration_smoke.py`.
- The script sends a POST request to `/agent/orchestrate`, validates the response
  as `SleepAgentOrchestrationResult`, and prints contract-level metadata only.
- Default API base URL is `SLEEPAGENT_API_BASE_URL` or
  `http://127.0.0.1:18000`.
- Real local CLI smoke passed against a temporary backend on
  `http://127.0.0.1:18000`.
- Verification history:
  - `python -m py_compile scripts/run_agent_orchestration_smoke.py tests/test_agent_orchestration_smoke_script.py` passed.
  - `python -m pytest tests/test_agent_orchestration_smoke_script.py -q` passed
    with `3 passed`.

### Task 0064: Minimal one-request dialogue context

Status: completed

- Added `DialogueContext` with:
  - `history_summary`
  - `user_preferences`
  - `recent_questions`
- Added `dialogue_context` to `SleepAgentOrchestrationRequest` and
  `SleepAgentEndpointRequest`.
- Added `DialogueTurn.context_used`.
- Dialogue context is used only for the current response and is not persisted.
- Linear orchestration, LangGraph nodes, backend POST body, and CLI smoke support
  the context field.
- Persistent multi-turn state and long-term memory remain Stage 9 work.
- Verification history:
  - `python -m py_compile sleepagent/schemas/agent.py sleepagent/schemas/__init__.py sleepagent/agents/dialogue_agent.py sleepagent/agents/orchestration.py sleepagent/agents/langgraph_orchestration.py scripts/run_agent_orchestration_smoke.py tests/test_stage8_agent_orchestration.py tests/test_agent_orchestration_endpoint.py tests/test_agent_orchestration_smoke_script.py` passed.
  - `python -m pytest tests/test_stage8_agent_orchestration.py tests/test_stage8_langgraph_boundary.py tests/test_agent_orchestration_smoke_script.py -q`
    passed with `13 passed`.
  - `timeout 30s python -m pytest tests/test_agent_orchestration_endpoint.py -q`
    passed with `9 passed` outside sandbox network isolation.
  - `timeout 30s python -m pytest tests/test_agent_orchestration_endpoint.py tests/test_mock_analysis_endpoint.py tests/test_mock_report_endpoint.py tests/test_health.py -q`
    passed with `17 passed` outside sandbox network isolation.

### Task 0065: Frontend Agent orchestration support

Status: completed

- Added frontend API helpers:
  - `build_agent_orchestration_url()`
  - `fetch_agent_orchestration()`
  - `extract_agent_orchestration_summary()`
- Added a small Streamlit Agent orchestration panel:
  - reuses mock request controls
  - sends an Agent question and optional history summary
  - displays orchestration mode, step count, dialogue presence, context usage,
    Agent response, and safety flags
  - keeps DeepSeek and LangGraph default-off
- Fixed the Streamlit app's mock analysis/report loading argument mismatch.
- Verification history:
  - `python -m py_compile frontend/app.py frontend/api_client.py tests/test_frontend_api_client.py` passed.
  - `python -m pytest tests/test_frontend_api_client.py -q` passed with
    `10 passed`.
  - `python -m pytest tests/test_frontend_api_client.py tests/test_agent_orchestration_smoke_script.py tests/test_stage8_agent_orchestration.py tests/test_stage8_langgraph_boundary.py -q`
    passed with `23 passed`.

### Task 0066: Stage 8 final contract sweep

Status: completed

- Ran final Stage 8 syntax and contract tests.
- Confirmed `docs/DATA_CONTRACTS.md` includes Stage 8 Agent schemas, endpoint
  behavior, CLI smoke, and frontend Agent helper/panel details.
- Archived detailed Stage 8 history into this changelog and reduced
  `TASK_LOG.md` to a handoff summary.
- Verification history:
  - `python -m py_compile backend/main.py frontend/app.py frontend/api_client.py sleepagent/schemas/agent.py sleepagent/schemas/__init__.py sleepagent/agents/dialogue_agent.py sleepagent/agents/orchestration.py sleepagent/agents/langgraph_orchestration.py scripts/run_agent_orchestration_smoke.py tests/test_stage8_agent_orchestration.py tests/test_stage8_langgraph_boundary.py tests/test_agent_orchestration_smoke_script.py tests/test_frontend_api_client.py tests/test_agent_orchestration_endpoint.py` passed.
  - `python -m pytest tests/test_stage8_agent_orchestration.py tests/test_stage8_langgraph_boundary.py tests/test_agent_orchestration_smoke_script.py tests/test_frontend_api_client.py -q`
    passed with `23 passed`.
  - `timeout 30s python -m pytest tests/test_agent_orchestration_endpoint.py tests/test_mock_analysis_endpoint.py tests/test_mock_report_endpoint.py tests/test_health.py -q`
    passed with `17 passed` outside sandbox network isolation.

### Task 0067: Stage 9 data management snapshot repository

Status: completed

- Added Stage 9 persistence schemas:
  - `StoredAnalysisRecord`
  - `StoredReportRecord`
- Added `SleepDataRepository` as the replaceable persistence boundary for
  analysis and report snapshots.
- Added `LocalJsonlSleepDataRepository` as the first MVP storage backend.
- Local JSONL files are `analysis_records.jsonl` and `report_records.jsonl`.
- Added deterministic snapshot id helpers for analysis/report records.
- Added consistency validation so searchable fields match embedded
  `SleepAnalysisResult` and `MockSleepReport` payloads.
- Documented the Stage 9 data management contract in `docs/DATA_CONTRACTS.md`.
- Kept raw SHHS data, derived arrays, long-term memory, alerting, external
  tools, and PostgreSQL table implementation out of this small task.
- Verification history:
  - `python -m py_compile sleepagent/schemas/data_management.py sleepagent/schemas/__init__.py sleepagent/services/data_management.py sleepagent/services/__init__.py tests/test_stage9_data_management.py` passed.
  - `python -m pytest tests/test_stage9_data_management.py tests/test_mock_json_contracts.py tests/test_report_templates.py -q`
    passed with `10 passed`.

### Task 0068: Stage 9 long-term memory compression service

Status: completed

- Added `LongTermMemorySummary` with schema version
  `stage9.long_term_memory_summary.v1`.
- Added `compress_long_term_memory()` to summarize recent stored analysis/report
  snapshots for a single subject.
- Added `compress_memory_from_repository()` to read stored subject history from
  the Stage 9 data repository boundary.
- Added `build_dialogue_context_from_memory()` so the compressed
  `history_summary` can be passed directly into `DialogueContext`.
- The deterministic summary includes risk distribution, average AHI, max AHI,
  latest AHI, average/latest sleep efficiency, linked report count, and an
  assistive-only medical boundary sentence.
- Direct compression rejects empty inputs and mixed-subject analysis history.
- Repository compression returns `None` when no subject history exists.
- Documented the Stage 9 long-term memory contract in `docs/DATA_CONTRACTS.md`.
- Memory summary persistence and endpoint integration remain separate Stage 9
  tasks.
- Verification history:
  - `python -m py_compile sleepagent/schemas/memory.py sleepagent/schemas/__init__.py sleepagent/services/memory.py sleepagent/services/__init__.py tests/test_stage9_memory.py` passed.
  - `python -m pytest tests/test_stage9_memory.py tests/test_stage9_data_management.py -q`
    passed with `7 passed`.
  - `python -m pytest tests/test_stage9_memory.py tests/test_stage9_data_management.py tests/test_stage8_agent_orchestration.py tests/test_mock_json_contracts.py -q`
    passed with `16 passed`.

### Task 0069: Stage 9 local high-risk alert event recording

Status: completed

- Added local-only alert schemas:
  - `AlertEvent`
  - `AlertSeverity`
  - `AlertStatus`
  - `AlertTriggerType`
- Added `LocalJsonlAlertEventRepository` for append-only local alert storage.
- Local alert events are stored in `alert_events.jsonl`.
- Added alert helpers:
  - `build_high_risk_alert_event()`
  - `record_high_risk_alert_if_needed()`
  - `record_high_risk_alert_for_analysis_if_needed()`
- Current trigger rule is intentionally conservative:
  - only `risk_level=high` creates an alert event
  - `risk_level=low` and `risk_level=moderate` return `None`
- Trigger reasons include `risk_level=high` and optionally include threshold
  reasons for AHI >= 15 or suspected apnea count >= 20.
- Alert events explicitly record that no SMS, email, app push, or external push
  channel was attempted.
- Documented the Stage 9 alert event contract in `docs/DATA_CONTRACTS.md`.
- Verification history:
  - `python -m py_compile sleepagent/schemas/alert.py sleepagent/schemas/__init__.py sleepagent/services/alerting.py sleepagent/services/__init__.py tests/test_stage9_alerting.py` passed.
  - `python -m pytest tests/test_stage9_alerting.py tests/test_stage9_data_management.py tests/test_stage9_memory.py -q`
    passed with `11 passed`.

### Task 0070: Stage 9 external tool mock context interface

Status: completed

- Added external context schemas:
  - `WeatherContext`
  - `DietContext`
  - `LifestyleContext`
  - `ExternalToolContext`
  - `WeatherCondition`
  - `MealTiming`
  - `ActivityLevel`
- Added `ExternalContextProvider` protocol and deterministic
  `MockExternalContextProvider`.
- Added `build_mock_external_context()` for one-call mock context generation.
- Added `build_external_context_summary()` for Chinese summary text that can be
  consumed later by reports or dialogue context.
- Mock context includes weather, outdoor/indoor temperature, humidity, meal
  timing, caffeine, alcohol, heavy late meal, activity level, screen time, nap
  duration, and stress score.
- The same `subject_id`, `location`, `context_date`, and `seed` produce stable
  mock content, excluding `generated_at`.
- No real weather, diet, phone, wearable, or other external API is called.
- Documented the Stage 9 external tool mock context contract in
  `docs/DATA_CONTRACTS.md`.
- Verification history:
  - `python -m py_compile sleepagent/schemas/external_tools.py sleepagent/schemas/__init__.py sleepagent/services/external_tools.py sleepagent/services/__init__.py tests/test_stage9_external_tools.py` passed.
  - `python -m pytest tests/test_stage9_external_tools.py tests/test_stage9_alerting.py tests/test_stage9_memory.py tests/test_stage9_data_management.py -q`
    passed with `16 passed`.

### Task 0071: Stage 9 contract sweep and backend integration endpoint

Status: completed

- Added Stage 9 API schemas:
  - `Stage9MockContextRequest`
  - `Stage9MockContextResult`
- Added `POST /stage9/mock-context`.
- The endpoint generates mock analysis/report payloads, stores analysis/report
  snapshots in local JSONL, compresses long-term memory, records a local
  high-risk alert when applicable, and returns deterministic mock external
  context.
- The endpoint uses `SLEEPAGENT_DATA_STORE_DIR` when configured and otherwise
  stores local Stage 9 API files under `/tmp/sleepagent_stage9_api`.
- Added endpoint tests covering:
  - high-risk local alert recording
  - low-risk/no-alert behavior
  - multi-call memory accumulation
  - invalid request validation
- Updated `docs/DATA_CONTRACTS.md` with the Stage 9 backend integration
  contract.
- Updated `PROJECT_PLAN.md` and `docs/DEVELOPMENT_ROADMAP.md` to mark Stage 9
  complete for the current MVP scope.
- Stage 9 MVP scope is now complete; PostgreSQL, real push channels, and live
  external APIs remain future production work.
- Verification history:
  - `python -m py_compile backend/main.py sleepagent/schemas/stage9.py sleepagent/schemas/__init__.py tests/test_stage9_backend_endpoint.py` passed.
  - `python -m pytest tests/test_stage9_external_tools.py tests/test_stage9_alerting.py tests/test_stage9_memory.py tests/test_stage9_data_management.py tests/test_stage8_agent_orchestration.py tests/test_mock_json_contracts.py -q`
    passed with `25 passed`.
  - `timeout 30s python -m pytest tests/test_stage9_backend_endpoint.py -q`
    passed with `3 passed` outside sandbox network isolation.
  - `timeout 30s python -m pytest tests/test_stage9_backend_endpoint.py tests/test_agent_orchestration_endpoint.py tests/test_mock_analysis_endpoint.py tests/test_mock_report_endpoint.py tests/test_health.py -q`
    passed with `20 passed` outside sandbox network isolation.

### Task 0072: TASK_LOG Stage 10 handoff restructure

Status: completed

- Reworked `TASK_LOG.md` from a command-heavy task log into a Stage 10 handoff
  summary.
- Removed long verification command blocks and detailed experiment notes from
  `TASK_LOG.md`; those remain archived in this changelog under the original task
  entries.
- Expanded `TASK_LOG.md` per-stage summaries for Stage 0 through Stage 9:
  - stage scope
  - tools and libraries used
  - main interfaces, schemas, scripts, endpoints, and environment variables
  - key outputs
  - future optimization direction
- Added a dedicated Stage 10 handoff focus section covering Docker, README,
  demo scripts, caveats, and paper/defense materials.
- Kept operational caveats in `TASK_LOG.md` so Stage 10 can summarize them
  without digging through long experiment history.

### Task 0073: Stage 10 SHHS manual demo helper

Status: completed

- Added `scripts/run_stage10_shhs_demo.py`.
- Default mode prints a final-demo command plan from the `sleepagent/` project
  root:
  - local SHHS EDF/XML path status
  - FastAPI backend startup on port `18000`
  - Streamlit frontend startup on port `18501`
  - local API smoke command
  - existing Agent smoke script command
  - SHHS XML summary command
  - YASA EDF staging command
  - YASA-vs-SHHS XML evaluation command
- Added `--api-smoke` mode to exercise:
  - `GET /health`
  - `GET /mock-analysis`
  - `GET /mock-report`
  - `POST /agent/orchestrate`
  - `POST /stage9/mock-context`
- Added `docs/STAGE10_SHHS_DEMO.md` with a concise Chinese manual tutorial for
  starting the backend/frontend and running the SHHS sample demonstration.
- Added focused tests in `tests/test_stage10_shhs_demo_script.py`.
- Verification history:
  - `python -m py_compile scripts/run_stage10_shhs_demo.py tests/test_stage10_shhs_demo_script.py` passed.
  - `python -m pytest tests/test_stage10_shhs_demo_script.py -q` passed with
    `4 passed`.
  - `python scripts/run_stage10_shhs_demo.py --json --record-id shhs1-200001`
    passed and confirmed the local `shhs1-200001` EDF, NSRR XML, and Profusion
    XML sample paths exist under `/mnt/data4/wz/SleepAgent/data/raw/shhs_sample`.

### Task 0074: Stage 10 Docker service files

Status: completed

- Added `.dockerignore` to keep local data, EDF/XML/NPZ files, checkpoints,
  reports, logs, and local env files out of Docker build context.
- Added `docker/Dockerfile` for a Python 3.11 slim SleepAgent runtime.
- Added `compose.yaml` with:
  - FastAPI backend service on host port `18000`
  - Streamlit frontend service on host port `18501`
  - backend healthcheck against `/health`
  - named volume for Stage 9 JSONL storage under `/tmp/sleepagent_stage9_api`
  - in-memory report retriever default
- Added `compose.shhs-demo.yaml` as an explicit optional override for authorized
  local SHHS demos. It bind-mounts `${SLEEPAGENT_SHHS_ROOT_HOST:-../data/raw/shhs_sample}`
  to `/data/shhs_sample` read-only and sets `SLEEPAGENT_SHHS_ROOT`.
- Added focused tests in `tests/test_stage10_docker_files.py`.
- Docker build was not run in this task because it may require pulling base
  images and package dependencies from the network.
- Verification history:
  - `python -m py_compile tests/test_stage10_docker_files.py` passed.
  - `python -m pytest tests/test_stage10_docker_files.py -q` passed with
    `5 passed`.
  - `docker compose -f compose.yaml config` passed.
  - `docker compose -f compose.yaml -f compose.shhs-demo.yaml config` passed.
