# Plan: SleepAgent 即插即用能力与真实雷达闭环
_Locked via grill — by Codex + user_

## Goal

将 SleepAgent 建设为可由智慧养老 OS、情感陪护机器人或其他获授权客户端独立调用的睡眠照护服务，而不是嵌入聊天机器人的大模型推理链和上下文。v1 以现有 Perceptor 毫米波雷达为唯一真实验证设备，通过已打通的厂商云链路完成“签名推送与主动拉取 → 统一睡眠观测 → 老人中心的 NightEpisode → 质量与证据推理 → 版本化晨间结果 → REST 查询与持久事件”闭环；同时建立受控注册、可验证、可回滚的设备 Adapter 基座，为其他厂商或非雷达设备保留明确扩展合同，但在没有真实设备前不宣称已经兼容。厂商结构化结果作为带来源和未知项的算法派生观测使用，当前不调用 ResSleepNet，也不把厂商呼吸率序列误作模型所需的原始一维呼吸波形。

## Inherited authority and product decisions

1. `product_positioning/PLAN.md` 继续决定居家老人第一用户、老人/家属/医生三角色、非诊断边界以及“晨间解释 + 个性化对话照护”产品主线。
2. `product_information_architecture/PLAN.md` 继续决定老人产品入口、四个核心页面和开发/调试工作台的分层。
3. `agent_architecture/PLAN.md` 继续决定 `SleepCareAgent`、`EvidenceReasoningAgent`、`CareStrategyAgent`、条件触发的 `SafetyReviewAgent` 以及 Tool/Service/Policy/Commit Controller 边界。
4. `skills_design/PLAN.md` 继续决定 Skill、Registry、版本锁、权限与发布治理；设备 Adapter 和未来 Model Tool 不能借“插件”扩大 Agent 或 Skill 权限。
5. 本计划是设备接入、统一观测、NightEpisode、服务生命周期、外部睡眠域 API/事件以及未来模型接入门槛的权威规范。
6. 当前 `sleepagent.product_device`、`sleepagent.integrations.perceptor`、`sleepagent.radar_agent.product_agent` 和 `RadarPersistenceStore` 必须原地演进并复用；目标实现不得建设第二套 Agent runtime、第二套正式 webhook 数据库或第二套对外任务 API。

## Meaning of plug-and-play in v1

“即插即用”不是一个万能接口，而是四种边界清晰的能力：

1. **设备即插即用**：新设备通过受控注册的 Adapter、能力声明和一致性测试进入统一观测层；v1 只把 Perceptor 标记为真实验证，其余设备只验证合同和 fixtures。
2. **模型即插即用**：模型输出必须通过独立、版本化、带适用数据域和不确定性的 Tool 边界进入 Evidence；v1 只预留 provenance 和 Tool 描述合同，不部署任何信号模型。
3. **对养老 OS 即插即用**：外部系统只调用版本化睡眠域命令/查询并消费事件，不导入 Agent 代码、不传递聊天上下文，也不能直接驱动内部 Agent/Tool。
4. **运行生命周期即插即用**：SleepAgent 按需激活、跟进和休眠；这是资源与业务状态切换，不是删除服务、清空记忆或白天禁止查询。

v1 的“插件”是**受控部署单元**：Adapter 经过注册、配置校验、合同测试和人工启用后随服务部署/重启生效。v1 不支持上传任意代码、运行时热加载第三方包或让未审查 Adapter 直接进入生产。正在进行的 NightEpisode 固定 Adapter/策略/Schema 版本；新版本只影响后续 Episode，回滚恢复到上一批准版本。

## Current verified baseline and honest gaps

### What is already proven

1. `Radar_monitor.py` 曾通过厂商云主动拉取实时心率和呼吸率。
2. `server.py` 与既有内网穿透曾接收到 `VitalSignsDataEvent`，证明“真实雷达 → 厂商云 → 当前服务器”传输链可达。
3. 当前 `sleepagent.integrations.perceptor` 已有 token、签名、重试、实时 session、sleep report 请求、webhook 验签、SQLite 幂等以及 raw/normalized 双层保存的部分实现和测试。
4. 当前四角色 `product_agent` 已有 Episode runtime、FactSnapshot、Evidence/Care/Safety/发布治理、持久化和行为验收基础。

### What is not yet proven

1. 旧脚本只证明最小连通性：没有生产级验签、幂等、设备—老人绑定、NightEpisode、质量门控或 Agent 闭环。
2. 当前正式 webhook 虽能规范化事件，但结果停留在独立 SQLite；产品 API 仍默认使用 `FakeRadarProductDataProvider`，真实观测没有进入 `product_agent`。
3. 当前 `Radar*` Schema 是单厂商/单模态视角，缺少统一观测所需的 raw/derived、单位、算法版本、校准、缺失、不确定性和完整处理链。
4. 当前 `radar_night_summaries` 以 `(radar_device_id, night_of)` 唯一并覆盖，不能表达老人中心、多设备、迟到报告和分析修订。
5. 当前外部路由暴露 radar、task、agent-run 等内部概念，没有稳定的睡眠域服务合同、operation 资源或持久 outbox。
6. Perceptor 文档与示例在签名 key 尾随 `&`、时间戳、时区、`data` 形态、字段大小写和响应类型上存在矛盾；文档不能替代真实样本验证。
7. 厂商云只提供呼吸率离散值、睡眠分期和睡眠报告，没有 ResSleepNet 所需的连续一维呼吸波形。

旧 `Radar_monitor.py`、`server.py` 仅保留为诊断探针和真实载荷样本来源，不得成为生产守护进程。若厂商暂时无法把回调从 `/receive` 改到正式路径，`/receive` 只能成为同一 ingestion service 的薄兼容别名，不得保留另一套 CSV 解析和存储。

## Target architecture

```text
Perceptor radar / future device clouds
        │
        ├─ signed push
        └─ authenticated pull / reconcile
                    │
          Provider Adapter boundary
          ├─ capabilities + version
          ├─ authentication / transport
          ├─ parsing / normalization
          └─ no medical interpretation
                    │
      durable raw inbox + quarantine + audit
                    │
        AdapterObservationCandidate
                    │
       effective-time DeviceBinding
                    │
      canonical SleepObservation repository
                    │
           NightEpisode aggregation
                    │
       deterministic fast path
       ├─ quality / dedupe / lifecycle
       ├─ absolute safety boundaries
       └─ transactional domain outbox
                    │
       existing four-Agent slow path
       ├─ EvidenceReasoningAgent
       ├─ CareStrategyAgent
       ├─ conditional SafetyReviewAgent
       └─ SleepCareAgent publication
                    │
      versioned result / role views / events
                    │
  REST + event polling reference client
                    │
智慧养老 OS / chatbot / other authorized clients
```

The external client never:

- imports or invokes internal Agent classes;
- sends its full conversation history as SleepAgent context;
- reads raw vendor payloads, Tool receipts or Evidence Ledger internals;
- supplies an unverified role/subject claim in natural language;
- waits synchronously for a complex multi-Agent workflow.

Two different Episode concepts must remain explicit:

- `NightEpisode` is the external, elder-centered domain aggregate for one sleep process.
- the existing product-Agent `Episode` is one internal reasoning execution. It is linked through `analysis_run_id` to one exact NightEpisode revision and is never exposed as the public resource identity.

Every raw record, observation, Episode revision, analysis result, role view and domain event carries `data_mode=live|replay`. Live and replay use separate subject/device namespaces and cannot contribute to the same baseline, memory or report. Production is `live-only` and fails startup if the persistent live provider is unavailable; replay routes/providers require explicit dev/test configuration and cannot be selected per ordinary production request.

## Core domain contracts

### 1. AdapterDescriptor and capabilities

Every deployable Adapter has an immutable descriptor:

- `adapter_id`, `provider_id`, `adapter_version`, `contract_version`;
- supported device types and provider accounts;
- capabilities: push, pull, reconcile, realtime vitals, bed presence, movement, alerts, sleep report, sleep stages, historical rate samples, raw signal artifacts;
- required credentials/configuration and a non-secret configuration fingerprint;
- accepted input variants and known limitations;
- output observation Schema versions;
- per-capability, per-environment verification status: `UNVERIFIED | PENDING | VERIFIED | FAILED`;
- independent deployment status: `REGISTERED | ENABLED | DISABLED | SUPERSEDED`;
- activation time, superseded version and rollback target.

Published descriptors and Adapter code remain available while any retained raw record, observation or NightEpisode pins that version. Disabling prevents new use; it does not remove a referenced version. Physical removal is permitted only after a reference scan, retention check and rollback-window expiry.

The deterministic Adapter Registry is deny-by-default. A descriptor cannot grant access to Agent tools, state mutation outside the ingestion service, raw-payload delivery to an LLM or arbitrary code loading. Provider credentials remain in environment/secret storage and never enter descriptors, logs, observations or model context.

An Adapter produces a strict `AdapterObservationCandidate` containing provider/device identity, typed value/unit, source times, missing/quality state and provenance—but no `subject_id`, DeviceBinding or NightEpisode id. Only the binding service can turn a candidate into a canonical `SleepObservation`. This prevents provider code from assigning an elder or bypassing effective-time authorization.

Each provider call and normalization job runs with a timeout, bounded payload/result size, circuit breaker and per-provider bulkhead. Adapter exceptions create typed failure/quarantine receipts; they cannot crash the API process, block another provider indefinitely or trigger an LLM fallback.

Perceptor v1 declares:

- `realtime_vitals=yes`
- `vital_push=yes`
- `bed_presence=yes`
- `movement=yes`
- `alerts=yes`, but alert semantics/severity remain unverified until mapped with real samples
- `sleep_report=yes`
- `sleep_stages=yes`
- `historical_rate_samples=yes`
- `raw_resp_waveform=no`
- point cloud, if retained, is a separate presence capability and never a respiratory signal

Capability verification is granular; verifying push does not automatically verify sleep report, alert meaning or history reconciliation.

The `yes/no` list above is a declared capability profile, not a verification verdict. Every claimed Perceptor capability starts `PENDING` for the exact Adapter/configuration profile unless a `CapabilityVerificationReceipt` binds it to redacted real evidence, conformance results, environment, Adapter/config hash, reviewer and time. Automated tests may produce evidence but cannot self-promote a capability to `VERIFIED`; an authorized human approves promotion or demotion.

### 2. Device and DeviceBinding

External identifiers are opaque strings, including numeric-looking values that may exceed JavaScript safe integer range. The stable provider identity is a namespaced key containing provider, provider account and the strongest available project/product/device identifiers; `(provider_account_id, device_name)` is the minimum Perceptor v1 key, not a globally unique identifier. `device_id`, `product_id`, `home_id` and `project_id` are stored as external metadata. `home_id=0` means unbound and never identifies an elder.

`DeviceBinding` contains:

- internal `device_id`;
- `provider_account_id` and provider device keys;
- `subject_id`;
- IANA `timezone_name`;
- `binding_version`, `effective_from`, optional `effective_until`;
- status and actor/audit information.

Binding is created or changed only through an authenticated administrative command. v1 allows at most one active subject per device and does not support shared/multi-person attribution. Rebinding is prospective: old observations and Episodes retain the binding version that was effective at event time. Unbound/ambiguous events are durably quarantined and emit `DEVICE_BINDING_REQUIRED`; they do not enter an Episode, health inference or elder-facing risk notification.

An administrator may create a binding with an explicit effective time and request reprocessing of quarantined data. Release is allowed only when the observation time is trustworthy, falls uniquely inside that binding interval and the repair command is authorized/audited. The system cannot backdate a binding implicitly to “make data fit,” edit raw payloads or release an ambiguous event by selecting the current elder.

### 3. SleepObservation

After successful effective-time binding, use a versioned envelope plus a discriminated typed payload, rather than an untyped `value: Any`. The envelope includes:

- `observation_id`, `schema_version`, observation type;
- `subject_id`, internal `device_id`, `device_binding_id/version`;
- distinct `request_signed_at`, `measurement_at`, `event_occurred_at` and `received_at` fields as applicable, plus source timestamp text and normalized timezone status;
- value, explicit unit and missing/invalid state;
- `source_kind`: device-measured, vendor-derived, user-reported, externally reported or future model-derived;
- provider, provider account, Adapter id/version, raw inbox reference;
- producer/algorithm name and version when actually supplied;
- optional confidence and calibration state;
- quality flags, completeness, processing steps and limitations;
- stable source/idempotency keys.

Initial typed payloads:

- heart-rate observation in beats/minute;
- respiratory-rate observation in breaths/minute;
- bed-presence state;
- movement observation/event;
- device connectivity/status;
- vendor alert signal;
- sleep-stage interval;
- vendor sleep-profile metric;
- bed-exit/get-up event;
- explicit missing interval/quality observation.

Rules:

1. Adapter authentication, parsing, unit normalization, missing handling and provenance are allowed.
2. Medical interpretation, multi-source arbitration, averaging conflicting devices and diagnosis are forbidden in the Adapter.
3. Perceptor `-1` becomes an invalid/missing observation, never a physiological value or zero.
4. String/integer rate fields, `Onbed`/`OnBed`, and `data` as escaped string/object may be parsed through explicit compatibility variants.
5. Confidence, calibration and algorithm version that the source did not provide remain `null` with reason `not_provided`; they are never defaulted.
6. A vendor sleep stage/report is `vendor_derived`, not an observed fact equivalent to a raw signal or locally validated diagnosis.
7. Unparseable time, unknown timezone, malformed history arrays and unsupported units go to quarantine or a typed unknown; they are never silently converted.
8. Raw vendor text, raw payloads and future high-frequency waveform bytes never enter LLM context. Agents receive only authorized canonical observations, deterministic summaries and provenance references.
9. The top-level signed request timestamp is used only for signature/replay validation and never as a vital measurement time. A naive vendor-local measurement time may use the DeviceBinding timezone that was effective at that event time only when that compatibility rule has been verified; otherwise it remains `timezone_unknown`.
10. Device/source clock offset is measured where possible. Excessive skew produces a time-quality flag or quarantine and cannot be hidden by substituting receipt time.

The current `RadarSourceMetadata.raw_payload` and `data_payload` fields are legacy compatibility fields. The provider-neutral canonical source metadata stores only raw references, hashes and minimal provenance. Any compatibility projection passed to deterministic tools or Agents must strip embedded raw/data payloads; a regression test treats their presence in a FactSnapshot or prompt context as a hard privacy failure.

### 4. RawIngressRecord, quarantine and SignalArtifact

Storage has three separate layers:

1. **Raw ingress**: immutable encrypted payload, receipt metadata, signature profile, idempotency identity, pre-normalization payload hash and retention deadline.
2. **Canonical observation**: normalized typed data and provenance used by deterministic tools and Agents.
3. **SignalArtifact**: future large/high-frequency signal object with sampling metadata, checksum, URI and authorization; not implemented for Perceptor v1.

Invalid signatures are rejected fail-closed and audited without trusting payload identity. Validly signed but unparseable or unbound payloads are durably quarantined and acknowledged only after persistence, preventing retry storms while preserving repairability. Quarantine repair creates new processing receipts; it never rewrites the original raw record.

Processing status is an append-only receipt/event stream with a derived current projection, not a mutable column on the immutable raw payload. Every normalization, quarantine, repair, binding and deletion/crypto-shred transition records actor/version/cause.

The same provider idempotency identity with the same payload hash is a duplicate. The same identity with a different payload hash is `MESSAGE_ID_COLLISION`, is quarantined and is never reported internally as an ordinary duplicate. When `message_id` is absent, the Adapter uses a versioned hash of the full pre-normalization payload plus available native record identity; it cannot collapse records merely because device and second-level timestamp match.

Production must explicitly configure encryption keys and retention policies before startup. Development may use short-lived local storage only under an explicit dev mode. Revocation/deletion propagates through raw records, observations, Episodes, reports, outbox payloads and caches according to the existing data-governance authority; append-only audit stores only the minimum lawful proof and never duplicate full health payloads.

### 5. NightEpisode and revisions

`NightEpisode` is an elder/sleep-process aggregate, not a vendor report or one device row. It contains:

- stable `night_episode_id`, `subject_id`, episode timezone and local sleep date;
- collection window and its derivation;
- linked DeviceBinding versions, observations, source reports and quality state;
- Adapter/Schema/policy versions pinned for analysis;
- lifecycle state and transition receipts;
- zero or more append-only analysis/report revisions.

One Episode may link multiple devices and feedback in the future, although only one bound Perceptor device is verified in v1. Conflicting observations remain separate and are passed to quality policy/Evidence; no record is overwritten or averaged by ingestion.

Episode lifecycle:

```text
Collecting → AwaitingReport → Analyzed → Closed
Analyzed/Closed ──late source, feedback or reanalysis──→ Revised
Revised ──publish current revision or retain prior current──→ Closed
```

- In-bed events, an explicit start command or configured time fallback may open/resume collection.
- A verified out-of-bed/end event, explicit end command or configured morning cutoff closes high-frequency collection and enters `AwaitingReport`.
- The report reconciler pulls the vendor report for the binding timezone/local date until success or a bounded deadline.
- At the morning publication deadline, the system creates revision 1 from the available canonical data or publishes an explicit `data_insufficient/report_pending` revision; it does not wait indefinitely or invent a completed vendor report.
- Late observations, corrected vendor reports, feedback or explicit reanalysis create a new revision linked to its predecessor and cause; old Evidence, role views and receipts remain addressable.
- A new night may become Active while an earlier Episode remains in care follow-up.

Each analysis revision has an immutable parent revision, monotonically increasing revision number, exact source/report hashes, `analysis_run_id` for the internal product-Agent Episode and a CAS-protected current pointer. Concurrent reanalysis cannot overwrite a sibling revision; a winner is selected only by deterministic publication policy and the supersession relation remains auditable.

Episode membership is based on event time, binding effective interval and configured subject timezone. Receipt time is used only as a fallback with an explicit quality penalty. Out-of-order arrivals inside the lateness window attach deterministically; later arrivals create a revision or are quarantined if attribution is unsafe.

The stable Episode identity is not the vendor `date` and not a UTC calendar day. A versioned episode-boundary policy derives a `night_key` from the aware sleep interval in the subject timezone, handles DST explicitly and records whether the start came from verified in-bed data, an external command or time fallback. Vendor report dates are only association evidence. If cross-midnight data or a report cannot be matched to one Episode, it remains pending association rather than being attached to the most recent night.

### 6. Orthogonal lifecycle states

Do not encode all behavior in one status enum:

1. **Monitoring state**: `Dormant ↔ Active`.
2. **NightEpisode state**: `Collecting → AwaitingReport → Analyzed → Closed/Revised`.
3. **Care follow-up state**: `None → PendingFeedback → FollowingUp → Completed/Ended`.

The public `Active | Follow-up | Dormant` label is a computed view:

- `Active` when current monitoring is active;
- otherwise `Follow-up` when any authorized care follow-up remains open;
- otherwise `Dormant`.

Transitions are deterministic, idempotent and audited. A per-subject lease/CAS prevents duplicate schedulers, push events and external commands from opening competing Episodes or publishing the same revision. Dormant retains the service, memory and query capability; it only disables continuous acquisition/analysis work that is not needed.

A versioned transition policy defines trigger priority, minimum dwell/debounce, fallback schedule, manual override expiry and recovery after restart. A noisy in/out-of-bed sequence cannot flap monitoring or create multiple nights, and an ordinary device event cannot silently defeat an explicit authorized stop while that override is valid. The public projection is read-only and can never be written back to any component state machine.

## Perceptor v1 ingestion and reconciliation

### Push fast path

1. Receive the original HTTP body and headers at the existing FastAPI integration boundary.
2. Apply request-body, connection and rate limits before JSON materialization; reject unsupported signing methods/versions.
3. Select an approved Perceptor compatibility profile and verify HMAC before normalization. Production never enables unsigned mode.
4. Enforce timestamp-window and nonce replay rules once confirmed against real callbacks; preserve the exact signed `data` representation.
5. Resolve idempotency primarily by `(provider_account_id, message_id)` plus payload hash and detect collisions. If real callbacks omit or fail to reuse `message_id`, use an explicitly versioned full-payload fingerprint and record the weaker guarantee.
6. Transactionally persist the raw inbox record, normalization work item and internal processing-outbox intent before ACK.
7. ACK with the exact vendor-compatible envelope after durable acceptance. A true duplicate receives the same successful semantic result. A validly signed message-id collision is durably quarantined, emits an operational event and is ACKed to stop an unrecoverable retry storm; an invalid signature is rejected and never ACKed as accepted.
8. A leased/restart-safe worker normalizes deterministically, resolves DeviceBinding, creates observations and routes them to the current Episode. A reprocessor scans stranded `RAW_ONLY` work and quarantine repairs.
9. Run only the deterministic quality/lifecycle/safety fast path. No LLM call is allowed in the webhook request or per-sample processing path.

The following must be verified with real callback captures before the capability becomes `VERIFIED`:

- whether the HMAC key appends `&`;
- exact signing path/canonicalization for push;
- timestamp type, timezone and allowed clock skew;
- whether `data` is a string or object;
- casing and numeric/string field forms;
- whether retries reuse `message_id`;
- exact successful response envelope.

Any credential-like value embedded in the DOCX/demo is treated as compromised sample material: it is never copied into configuration, and the live test requires vendor-issued credentials to be confirmed or rotated.

### Pull and reconciliation

1. `getSleepReport(device_name, home_id, date)` is the primary morning report source.
2. The Adapter converts profile values, stage intervals, minute-level heart/respiratory rates, body movement and get-up entries into vendor-derived observations while retaining the original report hash.
3. Ambiguous profile strings such as `8-0` remain source text/unknown until a verified parser contract exists; stage timestamps are not assigned a timezone by guess.
4. `getHistoryData` is used only for bounded gap repair/reconciliation. The three comma-separated series must have equal lengths; values are reconstructed backward from `send_time` at the documented cadence and marked as reconstructed timestamps.
5. `getHistoryData` respiratory rates are not SignalArtifacts and cannot call ResSleepNet.
6. Repeated pulls are idempotent by provider/report-date/content hash. A changed report produces a new source-report version and a NightEpisode analysis revision.
7. `start` + `getRealTimes` polling is a fallback for missing push capability or observed delivery gaps, not a parallel source of truth. It writes through the same raw inbox and canonical pipeline.
8. Retry uses bounded exponential/backoff policy, honors deadlines and records unknown outcomes; no infinite polling or duplicate Agent analysis is allowed.
9. Pull checkpoints use overlapping windows and a lateness watermark. The cursor advances only after durable storage; retry rescans overlap and relies on source fingerprints for dedupe.
10. A pulled report is associated by subject, binding version and sleep-time overlap. If no unique Episode matches, it enters a pending-association queue instead of attaching to the latest night.
11. Ingestion identity and physiological-fact identity are separate. When push and pull carry the same source fact, one canonical observation may reference multiple acquisition receipts. If values/times differ beyond an exact normalization rule, both observations remain and produce a conflict/quality signal; reconciliation never selects a winner by channel.
12. `getSleepReport` always sends an explicit local report date. `getHistoryData` is partitioned into windows no longer than the documented one-hour limit and batches no more than 20 devices; each five-minute block validates equal series lengths and reconstructs the documented approximately three-second cadence with a `reconstructed_time` quality marker.

### Vendor alert correlation

Perceptor alert and stop events use a deterministic state machine keyed by the strongest verified vendor alert-instance id. A stop event that cannot uniquely match an open alert remains an orphan stop signal; it never closes all alerts with a shared code. Only a reviewed mapping table may assign a known meaning/severity. Unknown codes remain `vendor_alert_signal` and cannot alone produce a diagnosis or “all clear” result.

## Deterministic fast path and Agent slow path

### Fast path

The deterministic path owns:

- signature/replay/idempotency checks;
- binding and Episode attribution;
- data quality, freshness, coverage and missingness;
- lifecycle transitions and report scheduling;
- cataloged absolute safety boundaries and vendor alert preservation;
- domain outbox creation and delivery state;
- retry, lease, CAS and commit receipts.

A vendor alert is a source signal, not a diagnosis. Until alert code meaning and severity are verified, it remains `severity=unknown` and can trigger operational review but not a disease claim.

`CurrentRisk` always includes risk state, data sufficiency, source scope, policy version and observed time. Missing/stale/offline/clock-invalid coverage yields `unknown/data_insufficient`, never “no risk detected.” Physiological thresholds and alert-code mappings must come from an existing reviewed deterministic policy; any unreviewed rule is `PENDING_DOMAIN_REVIEW` and cannot produce a health-facing escalation. Device/offline and data-quality operational events remain available independently.

Risk, bed-state, offline and quality events are emitted on a versioned state transition or configured reminder/cooldown, not on every sample. Hysteresis and per-subject/episode dedupe prevent event floods without suppressing the first safety-relevant transition; receipts retain the suppressed-repeat count.

### Slow path

The existing four-Agent runtime receives a versioned FactSnapshot built only from canonical observations and deterministic receipts:

- `EvidenceReasoningAgent` decides what can be accepted and exposes conflicts/unknowns.
- `CareStrategyAgent` may produce no action or one cataloged low-risk action.
- `SafetyReviewAgent` is triggered by existing policy.
- `SleepCareAgent` publishes the role-appropriate result.

Agent analysis runs for morning review, explicit query, feedback/follow-up or reanalysis—not for each incoming sample. Every result records NightEpisode revision, observation scope, Adapter/Schema/policy/Skill/model versions and execution mode.

Slow-path work is submitted through the persistent Operation/lease boundary to a bounded Agent worker pool. Backlog, timeout or model outage cannot block webhook ACK, deterministic risk transitions or stored-result queries. Per-subject ordering and fair scheduling prevent one noisy device or repeated reanalysis caller from starving other subjects.

## Future Model Tool boundary

v1 implements only canonical producer/provenance fields and a non-executable interface specification for a future model. It does **not** add a runnable Model Tool registry, model endpoint, model database or placeholder invocation:

- a future `ModelToolDescriptor` must include capability, required input Schema, supported device/signal domain, preprocessing version, model/weight version, output Schema, calibration status, limitations and deployment endpoint;
- a future `ModelInvocationReceipt` must bind exact input artifact hashes, versions, timing, output references, confidence/calibration and failure/timeout state;
- model-derived observations use `source_kind=model_derived` and never overwrite vendor-derived observations.

No model is considered deployable because it conforms syntactically. Registration requires data-domain compatibility, calibration/uncertainty evidence, isolated service health checks and real-device validation.

ResSleepNet remains a research asset and is out of the v1 runtime because:

- it expects a fixed full-night continuous 1-D respiratory sequence of 1,228,800 samples, not respiratory-rate values;
- its current output is 1200 four-class sleep-stage probabilities plus one whole-night AHI regression, not a per-event apnea classifier;
- it has no production inference API and depends on a separate TensorFlow/CUDA environment;
- it has not been validated on the current Perceptor device domain.

If raw waveform access is obtained later, ResSleepNet must run as an isolated versioned inference service, not inside the Agent process. That later project must separately define preprocessing, missing segments, sampling checks, artifact storage, domain validation, calibration, latency and rollback.

## Independent deployment boundary

SleepAgent is a network service owned and deployed independently from the chatbot/养老 OS:

- the external client depends only on HTTPS/OpenAPI and event cursor contracts;
- SleepAgent owns its database, Adapter credentials, raw retention, lifecycle and four-Agent memory;
- the API/ingestion process and background normalization/Agent workers are separate runtime roles sharing the durable transaction store;
- chatbot processes do not import SleepAgent packages, share its LLM prompt context or wait on its model workers;
- health checks distinguish API, database, provider, normalization worker, Agent worker and outbox state;
- scaling or restarting the chatbot does not activate/deactivate SleepAgent, and restarting a SleepAgent worker does not lose an accepted command/event.

This is one independently deployable product with internal process roles, not a requirement to split every domain module into a microservice or introduce Kafka in v1.

## Stable external sleep service

### Authentication and authorization

External calls require both:

1. an authenticated service principal; the reference single-server profile uses a rotated HTTPS Bearer service credential resolved to a configured principal, while deployments with mTLS or OAuth2 client credentials map through the same verifier interface; and
2. an asymmetric JWS actor assertion containing assertion id, key id, issuer, audience, `actor_id`, `subject_id`, role, scopes, issued/expiry time, nonce, HTTP method/path and request-body hash.

SleepAgent resolves the authoritative actor/subject binding and rechecks scope for every query, command and event. Natural-language role claims are ignored. Fixed dev identities require explicit dev mode; production fails closed if identity verification is absent or misconfigured.

Service and actor signing keys support explicit key ids and a bounded dual-key rotation window. Replayed assertion ids/nonces and body/path mismatches are rejected. Logs redact signatures, device identifiers and full message ids.

### Versioned REST domain

The public surface is `/api/v1` sleep-domain resources, not radar/Agent/task internals.

Synchronous queries:

- get current public lifecycle and data-quality state for a subject;
- get current risk state;
- get latest or specified NightEpisode summary/revision;
- get authorized elder, family or doctor view;
- get operation status;
- poll domain events from a durable cursor.

Queries read already committed projections and never synchronously invoke an Agent. If a caller needs new analysis, it submits the explicit asynchronous reanalysis/query-operation command and may continue using its own realtime chatbot loop while polling or consuming events.

Asynchronous commands:

- activate/deactivate monitoring;
- submit elder feedback;
- submit authorized family/caregiver feedback;
- request NightEpisode reanalysis;
- optionally request/finalize a monitoring window when the external OS has an authoritative event.

Feedback records preserve `actor_id`, authenticated role, subject relationship, event time, receipt time, source text/structured answer and provenance category. Elder self-report, family/caregiver report and device observation remain distinct Evidence sources; one role's feedback can never be normalized into another role's statement.

Complex commands require an idempotency key and return `202 Accepted` with `operation_id`, status URL and correlation id. Duplicate commands return the existing operation. Queries never create hidden Agent work; when a result is not ready they return an explicit processing/data-insufficient state.

`Operation` has `pending → running → succeeded | failed | cancelled` plus attempt count, lease, request hash, result resource id and stable error code. Idempotency is scoped to service principal, actor, route and target resource; the same key with a different request hash returns conflict. A command is not `succeeded` until its domain mutation and outbox event are committed. Worker crashes recover through the lease without blindly replaying an external side effect with an unknown outcome.

Device registration/binding and Adapter administration use a separately authorized administrative API and are not available to ordinary chatbot actors.

Public request, response, error and event payloads have independent Schema versions and bounded pagination. Additive fields follow the v1 compatibility policy; breaking changes require a new API/event version and overlap/deprecation window. Stable machine-readable errors distinguish unauthorized, conflict, data-insufficient, pending, cursor-resync and provider-unavailable outcomes.

Role-view caches, if introduced, are keyed by subject, exact NightEpisode revision, authenticated role/scope projection and authorization epoch. Revocation invalidates the epoch; cached family/doctor content cannot be served to another role or a later broader request.

### Persistent domain events

The same transaction that commits a domain state transition writes an outbox event. External clients first consume through authenticated cursor-based polling; optional webhook delivery is a later transport Adapter over the same outbox, not a second event source.

Initial event types:

- `MONITORING_ACTIVATED`
- `MONITORING_DORMANT`
- `ELDER_IN_BED`
- `BED_EXIT_DETECTED`
- `DEVICE_OFFLINE`
- `DATA_QUALITY_INSUFFICIENT`
- `DEVICE_BINDING_REQUIRED`
- `RISK_SIGNAL_DETECTED`
- `NIGHT_EPISODE_AWAITING_REPORT`
- `NIGHT_EPISODE_ANALYZED`
- `NIGHT_EPISODE_REVISED`
- `MORNING_REPORT_READY`
- `FEEDBACK_REQUIRED`
- `FAMILY_ATTENTION_SUGGESTED`
- `DOCTOR_CONTACT_SUGGESTED`

Every event has `event_id`, event type/version, aggregate id/version, subject id, optional Episode/revision/operation ids, occurred/persisted times, per-aggregate sequence, correlation/causation ids, minimal authorized payload and Schema version. A monotonic delivery offset supports cross-aggregate cursor polling but conveys no global business order; only per-aggregate sequence is semantically ordered. Delivery is at-least-once and clients deduplicate by `event_id`. The internal outbox event is projected to a minimum role/scope-specific external payload at read time; elder, family and doctor consumers do not share one unrestricted payload.

The opaque cursor is bound to consumer service, subject/role/scope projection and event Schema generation. Every poll rechecks current authorization. Cursor retention/expiry is explicit; an expired or now-unauthorized cursor returns a stable resync-required error, after which the client reads current authorized snapshots and starts from a new cursor rather than silently skipping history.

A new current report revision emits a revision event with `supersedes_revision`; default queries return the current revision while explicit authorized audit queries can request an older immutable revision.

Deletion/revocation cannot recall data an external consumer already received. SleepAgent denies future reads, minimizes/cryptographically erases retained payloads as policy requires and emits a scoped revocation/tombstone event where downstream deletion is required; the reference client demonstrates handling it.

### Reference client

Provide a provider-agnostic reference client demonstrating:

- service authentication and signed actor context;
- idempotent command submission and operation polling;
- summary/risk/role-view queries;
- event cursor persistence and duplicate handling;
- no import of SleepAgent internals and no full chatbot context transfer.

This client is the integration artifact for the chatbot team; its success proves service composability, not medical accuracy.

## Persistence and migration strategy

1. Add additive migrations to the existing `sleepagent.radar_agent.persistence` chain for Adapter registry, provider accounts, versioned DeviceBinding, raw inbox/quarantine, canonical observations, NightEpisode/revisions, operations and domain outbox.
2. Reuse `RadarPersistenceStore` connection/migration/CAS patterns; do not keep `perceptor_webhook_events.sqlite3` as a second production authority.
3. Add a one-time idempotent importer for existing webhook raw/normalized rows when present. Import preserves raw ids and provenance; failures remain quarantined.
4. Replace the overwrite-only night-summary authority with versioned NightEpisode analysis. Existing `radar_night_summaries` becomes a compatibility read model of the latest applicable revision until legacy callers migrate.
5. Introduce a persistent product data provider that reads canonical observations/NightEpisode revisions and implements the current `product_agent` tool boundary.
6. Select fake/replay providers only under explicit test/dev configuration. Production startup fails if it would silently fall back to `FakeRadarProductDataProvider`.
7. Keep legacy `/product/radar/*` and Agent-run routes in the development/debug surface during migration; the new `/api/v1` contract is the only supported external OS boundary.
8. Migrate the active Perceptor callback URL to the FastAPI ingestion route. Any `/receive` alias invokes the same service and is removed after the vendor URL can be changed.
9. Do not delete old files or records in this implementation slice; mark diagnostic scripts and compatibility paths as deprecated and document the cutover.
10. Use an explicit expand → idempotent backfill/import → shadow-read comparison → configuration-gated cutover → later contract sequence. Each phase has a rollback point; there is no long-lived dual write and no one-step destructive replacement.
11. Replay/fake records use a separate namespace/tenant and are never imported into a live subject's history, baseline, memory or Episode.
12. Live production requires configured encrypted durable shared storage. `/tmp` and SQLite are limited to explicit local/dev acceptance; multi-worker production cannot start on the standalone webhook SQLite.

Legacy records whose live/replay mode, signature proof, time or binding cannot be established are imported only into the raw quarantine namespace. They are never forced into `data_mode=live` or admitted to an elder baseline merely because they came from the old server.

## Target source mapping

The implementation uses one new provider-neutral domain layer without creating a second Agent runtime:

- `sleepagent/sleep_domain/contracts.py`: Adapter candidate, binding, canonical observation, NightEpisode, lifecycle, operation and event contracts.
- `sleepagent/sleep_domain/registry.py`: static Adapter allowlist, descriptors, capability verification and version retention.
- `sleepagent/sleep_domain/ingestion.py`: raw inbox/work receipts, binding resolution, canonicalization orchestration and quarantine repair.
- `sleepagent/sleep_domain/episodes.py` and `lifecycle.py`: Episode aggregation/revisions and the three state machines.
- `sleepagent/sleep_domain/repository.py` and `outbox.py`: repositories, CAS/leases, operations and durable event polling over existing persistence connections/migrations.
- `sleepagent/sleep_domain/agent_bridge.py`: exact NightEpisode revision to existing `ProductEpisodeRunner` FactSnapshot/provider conversion.
- `sleepagent/sleep_api/contracts.py`, `auth.py`, `router.py` and `reference_client.py`: stable external service boundary.
- existing `sleepagent/integrations/perceptor/*`: Perceptor transport, signing, push/pull normalization and compatibility profiles.

`sleepagent.product_device` remains a compatibility/read-model layer during migration; it is not copied into a second device runtime. New routes are mounted from a router rather than adding another large block of provider-specific logic directly to `backend/main.py`.

## Approach

1. **Freeze contracts and plan authority.** Add the provider-neutral Adapter, binding, observation, NightEpisode, lifecycle, operation and event contracts inside the existing product-device/domain packages; add compatibility conversion for current `Radar*` records without changing the four-Agent contracts.
2. **Extend persistence additively.** Add migrations/repositories for the new authorities, transactions, leases/CAS, append-only revisions, raw retention and outbox. Ensure SQLite tests and the existing PostgreSQL migration path remain valid.
3. **Build the controlled Adapter registry and conformance suite.** Register Perceptor through an immutable descriptor; add a hardware-free reference Adapter/fixtures only to prove the contract, never to claim a second device is verified.
4. **Harden and unify ingress.** Refactor the existing Perceptor webhook to preserve the signed input form, enforce production fail-closed behavior, persist through the main store, handle duplicate/retry/quarantine semantics and route normalization through the canonical repository.
5. **Complete Perceptor pull normalization.** Add typed conversion for sleep report and bounded history reconciliation, report content hashing, fallback realtime polling and per-capability verification receipts.
6. **Implement DeviceBinding and Episode aggregation.** Add authorized registration/rebinding, timezone-safe event attribution, hybrid activation/end triggers, lateness handling and the three orthogonal lifecycle machines.
7. **Connect canonical data to deterministic tools and the existing four-Agent runtime.** Implement the persistent data provider, FactSnapshot construction, fast safety/quality path, morning scheduling and append-only analysis revisions.
8. **Add external `/api/v1` sleep service.** Implement service/actor authentication, queries, async commands, operation resources, idempotency and role-scoped domain views without exposing Agent/task internals.
9. **Add transactional outbox and event polling.** Publish versioned minimal events, cursor/dedup semantics, authorization filtering, backlog/retry observability and an optional future webhook-delivery seam.
10. **Add the provider-agnostic reference client.** Exercise commands, operation polling, queries and event consumption without importing application code.
11. **Migrate and deprecate legacy paths.** Import existing webhook records if any, make fake mode explicit, convert `server.py`/`Radar_monitor.py` into documented diagnostics and preserve debug APIs without presenting them as the supported integration surface.
12. **Run layered acceptance.** Pass contracts, replay anomalies, persistence/concurrency/security tests, one real Perceptor end-to-end night and external-client verification. Record each capability as `VERIFIED`, `PENDING` or `FAILED`; never convert blocked vendor access into a simulated pass.

### Delivery slices and gates

1. **Foundation gate:** contracts, additive persistence, live/replay isolation, Adapter conformance and migration rollback tests pass; no production callback cutover.
2. **Ingestion gate:** Perceptor push/pull writes the unified store, collision/quarantine/recovery works and shadow-read comparisons pass; Agent still reads the old provider until explicitly switched.
3. **Episode gate:** binding, three state machines, NightEpisode revisions, deterministic quality/risk and outbox pass replay/concurrency tests.
4. **Agent/API gate:** the persistent provider, FactSnapshot bridge, `/api/v1`, auth, operations, event polling and reference client pass without changing the external chatbot runtime.
5. **Live cutover gate:** a redacted real-night evidence pack passes, fake/replay citations are absent and rollback is rehearsed. Only then may the live provider become authoritative.

Each gate is independently revertible and must leave the repository testable. “Implementation complete” means all gates required by the requested slice pass; a partial slice cannot claim the entire v1 or silently enable the next gate.

## Verification and acceptance

### A. Contract and Adapter conformance

1. Unknown fields, units and capabilities fail closed or produce typed unknown/quarantine; no silent defaulting.
2. Adapter candidates validate against their exact Schema/version and contain provider/device provenance, source kind and missingness but no binding; canonical observations separately validate `subject_id` and binding version.
3. An Adapter cannot call Agents, write external state outside ingestion or send raw payloads to model context.
4. Version pinning and rollback keep an in-progress Episode on its original Adapter/Schema version.
5. A reference fixture Adapter passes without hardware; it remains `UNVERIFIED` for real-device claims.
6. Verification and deployment status change independently, and a disabled/superseded Adapter version remains replayable while retained records reference it.
7. Adapter output before binding contains no subject/Episode identity; only the binding service can create the elder-scoped canonical observation.
8. Timeout, oversized output or Adapter exception trips the provider isolation policy and cannot fail another Adapter/API request.

### B. Perceptor fixture/replay matrix

Cover:

- `data` as escaped JSON and object;
- `Onbed` and `OnBed`;
- numeric and string rates;
- 10-digit seconds, 13-digit milliseconds and ISO timestamps;
- unknown/no-timezone values;
- source clock skew and top-level signing time that differs from `data.DateTime`;
- `-1`, gaps and out-of-range values;
- duplicate `message_id`, missing `message_id`, retry and concurrent delivery;
- same `message_id` with a different payload hash;
- out-of-order events and late arrivals;
- invalid signature, wrong signing profile, stale timestamp and replayed nonce;
- connected/disconnected, vital, alarm and alarm-stop events;
- uniquely matched and orphan alarm-stop events;
- sleep report partial/empty/changed content;
- unequal history arrays and reconstructed timestamps;
- pull timeout, token refresh, rate limit/server error and unknown outcome.
- history windows over one hour/more than 20 devices are partitioned before calling the provider.

### C. Persistence, concurrency and lifecycle

1. Intake atomically commits raw inbox + normalization work + processing-outbox intent before ACK; normalization separately atomically commits canonical observations + affected domain state + domain outbox. No transaction claims to create observations before asynchronous normalization.
2. Two workers/commands cannot create competing active Episodes for one subject.
3. Duplicate push/pull/commands do not duplicate observations, operations, Agent analysis or external events.
4. Rebinding does not change old observations; unbound data never reaches Evidence.
5. Late data creates the correct new revision and leaves old reports addressable.
6. A new Active night can coexist with the previous care follow-up.
7. Restart restores monitoring, Episode, operation and outbox state without silent fake-provider fallback.
8. Revocation/deletion tests cover raw, canonical, Episode, report, outbox and cache projections.
9. A stranded raw work item is reclaimed exactly once semantically after worker crash.
10. Overlapping pull windows and cursor failure neither skip nor duplicate a canonical fact.
11. Authorized binding repair releases only uniquely attributable quarantined records and leaves the immutable raw record unchanged.
12. A missed vendor-report deadline publishes an explicit pending/data-insufficient revision; the later report appends rather than overwrites.

### D. Agent and safety boundary

1. No LLM is called by webhook receipt, per-sample ingestion or deterministic urgent handling.
2. FactSnapshot contains only canonical authorized observations and receipts, never raw vendor payload.
3. Vendor-derived stage/report retains unknown confidence/calibration/version and is not rewritten as diagnosis.
4. Conflicting devices/results remain visible to Evidence; Adapter does not average or choose a winner.
5. Morning, family and doctor views reference the same NightEpisode revision and preserve role authorization.
6. Data insufficiency produces an explicit unknown/quality result rather than a confident report.
7. Vendor alert and absolute safety paths cannot be described as clinically validated apnea detection.
8. Every published artifact exposes `data_mode`; no live result can cite replay observations or memory.
9. Stale/offline/insufficient coverage returns `unknown/data_insufficient`, never a normal/all-clear result.
10. FactSnapshot/prompt inspection contains raw references only and rejects legacy `raw_payload`/`data_payload` content.

### E. External service

1. Missing/invalid service identity, actor signature, scope, nonce or subject binding fails closed in production.
2. Command idempotency returns the original operation; operation and event status survive restart.
3. Event polling is at-least-once, cursor-stable and role-filtered; client deduplication by event id is demonstrated.
4. Chatbot reference client can activate monitoring, await an operation, retrieve a summary/view, submit feedback/reanalysis and consume events without importing Agent code or sending conversation history.
5. Internal Agent ids, Tool names, task ids and Evidence Ledger contents do not appear in public contracts.
6. Actor assertions are bound to audience, method, path and body hash; replay and key-rotation-window behavior are tested.
7. Same idempotency key with changed command body returns conflict; crash recovery cannot duplicate a committed action.
8. Revocation blocks future event reads and produces the required tombstone without claiming to recall already delivered bytes.
9. Stored-result queries make zero Agent calls; an expired or deauthorized event cursor returns explicit resync-required behavior.
10. Elder, family and caregiver feedback retain distinct authenticated provenance, and role-view cache keys cannot cross authorization epochs.

### F. Real Perceptor gate

Use the available real radar and vendor cloud for at least one complete end-to-end night:

1. Confirm/rotate credentials; record the approved compatibility profile without storing secrets in evidence.
2. Receive and verify a real push through the production-shaped FastAPI route.
3. Bind the device to a test subject/timezone and show canonical heart rate, respiratory rate, movement and bed state with raw provenance.
4. Close the night and pull the real sleep report; show stage/profile normalization and any unknown fields honestly.
5. Produce a NightEpisode analysis revision and morning role view through the existing four-Agent runtime. If the model is unavailable, the deterministic/degraded result verifies only the fast path; the Agent slow-path capability remains `PENDING`.
6. Retrieve the result and event through the provider-agnostic client.
7. Exercise duplicate delivery/re-pull and prove no duplicate Episode/revision/event.
8. Archive redacted request/response shapes, receipts, logs and capability verdicts; do not archive live credentials or unrestricted raw health payloads.
9. Confirm the Product Agent FactSnapshot and final result contain `data_mode=live`; a fake/replay source in this run is a hard failure.

Real acceptance reports separate verdicts for transport, push authentication, pull/report normalization, Episode aggregation, deterministic fast path, Agent slow path and external service. There is no single green badge that hides a `PENDING` sub-capability.

If vendor access, signing confirmation or a report is unavailable, the affected capability remains `PENDING`; fixture tests do not upgrade it to `VERIFIED`. One device/night proves an engineering integration path, not interoperability across vendors, clinical validity, long-term reliability or medical-device compliance.

### G. Operational observability

Expose health/metrics/logs for:

- signature failures and replay rejects;
- accepted, duplicate, quarantined and repaired ingress;
- source-to-receipt lag, gaps, quality and coverage;
- binding failures;
- Episode transition conflicts and report reconciliation attempts;
- fast-path risk latency;
- Agent invocation count/latency/failure by Episode revision;
- normalization/Agent worker backlog, lease expiry, per-subject fairness and provider circuit-breaker state;
- operation age and failure;
- outbox backlog, oldest event age and consumer cursor lag;
- retention/deletion jobs and failures;
- active Adapter/capability versions and verification status.

Logs contain opaque ids and reason codes, not secrets, raw payloads or unrestricted health values. Alerts exist for sustained ingestion gaps, quarantine growth, report deadline miss, outbox backlog and accidental fake-provider use outside dev/test.

The callback ACK SLO is configured below the vendor's first documented retry interval of 10 seconds; the initial single-device acceptance target is p95 under 2 seconds while preserving durable-before-ACK. Query latency is measured separately from asynchronous Agent operations so chatbot-facing stored-result reads cannot be hidden behind model latency.

Every delivery gate runs its focused tests plus the existing full repository regression suite, migration-from-populated-database tests, OpenAPI/event contract snapshots and diff/static checks available in the environment. A focused green suite cannot authorize cutover while existing Product Agent, replay, privacy or persistence tests regress.

## Key decisions & tradeoffs

1. Independent service/API isolation protects chatbot latency and context, at the cost of explicit operations, auth and event integration.
2. One real Perceptor Adapter plus neutral contracts gives an honest demonstrator; broader device claims wait for real hardware.
3. Controlled registration/redeploy is chosen over arbitrary hot loading to preserve security, reproducibility and rollback.
4. Provider-neutral observations replace direct Agent consumption of vendor fields, while compatibility read models limit migration breakage.
5. Vendor algorithms are useful inputs but remain derived and source-qualified; missing confidence/version reduces certainty instead of being fabricated.
6. Push provides timely events, sleep-report pull provides morning completeness, and history pull repairs gaps; none is treated as a raw waveform.
7. Raw, canonical and high-frequency artifact layers are separated to support privacy and future models without putting vendor payloads in LLM context.
8. NightEpisode is elder-centered and append-only revisioned, trading storage complexity for multi-source correctness and auditability.
9. Three orthogonal state machines prevent “next night Active” from destroying an earlier follow-up.
10. Deterministic fast path protects alert latency; Agent slow path provides explanation/care without sitting on the critical ingestion path.
11. REST + durable event polling is implemented before outbound webhooks to minimize delivery complexity while preserving composability.
12. ResSleepNet is deferred rather than wrapped prematurely because the available data violates its input/domain contract.

## Risks / open questions

1. The vendor must confirm signing-key suffix, push canonicalization, timestamp/clock-skew rules, successful response shape and `message_id` retry semantics. Until confirmed, those Perceptor capabilities remain `PENDING`.
2. The vendor document does not define report availability time, report correction behavior, empty-report meaning or timezone. The reconciler must expose configured assumptions and empirical receipts; it cannot silently infer a universal rule.
3. Vendor alarm codes and algorithm versions may require separate documentation or real samples before health-facing interpretation. Unknown codes stay opaque.
4. Existing webhook and Agent persistence are split; migration must avoid losing user-owned records or creating two authorities in the dirty worktree.
5. Production retention durations and legal basis depend on deployment jurisdiction/organization. Production enablement is blocked until an approved policy is supplied; tests use explicit short-lived values.
6. SQLite is adequate only for local/single-server development acceptance, not live production. The repository/migrations must keep PostgreSQL/shared-transactional-store compatibility, and scale-out beyond the initial deployment is a later decision.
7. One real device and one full night can validate engineering flow only. More devices, older adults and repeated nights are required before reliability or user-value claims.
8. Real-time rate samples and vendor sleep stages have unknown clinical accuracy. This plan tests transport, provenance, failure behavior and product integration—not clinical performance.
9. Replacing the fake provider may expose assumptions in existing UI/tests. Cutover remains configuration-gated until the persistent provider passes the full regression suite.

## Out of scope

- Buying or certifying additional watches, mattresses, bands, medical instruments or radar brands.
- Claiming that non-Perceptor devices have been verified.
- Runtime upload/hot execution of arbitrary Adapter or model code.
- Direct embedding of SleepAgent Agents, memory or Evidence Ledger into the chatbot process/context.
- A runnable Model Tool registry; ResSleepNet deployment, retraining, continuous learning, raw-signal preprocessing or medical validation.
- Treating respiratory-rate history or point cloud as a continuous respiratory waveform.
- Replacing PSG, diagnosing apnea/insomnia, prescribing treatment or claiming medical-device compliance.
- Multi-person/shared-device attribution in v1.
- Institution bed management, nursing shifts or养老机构 operational workflows.
- Building outbound webhook delivery before the persistent outbox/polling contract is proven.
- Deleting legacy APIs/scripts or performing an irreversible database cutover in the first implementation slice.
