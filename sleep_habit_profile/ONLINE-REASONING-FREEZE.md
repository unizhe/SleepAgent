# 睡眠习惯在线推理合同冻结

状态：architecture-frozen / runtime-skeleton-implemented  
范围：事件上下文解析、夜间活动语义、Care Delivery、多因子 Safety  
代码权威入口：

- `product_agent/online_reasoning.py`
- `product_agent/contracts.py`
- `product_agent/habit_profile.py`
- `product_agent/habit_runtime.py`
- `product_agent/governance.py`
- `product_agent/runner.py`

## 1. 在线事件到 Evidence 上下文

API 调用方只提交类型化 `OnlineReasoningEvent`，不得手工决定应读取哪些
Habit concept 或 ObjectiveBaseline metric。确定性
`reasoning.resolve_event_context` 按版本化事件规则产生：

```text
event_type
  → evidence_gap_code
  → required_concept_ids[]
  → baseline_metric_ids[]
  → required_current_signal_keys[]
  → required_quality_ref_kinds[]
  → required_trend_ref_kinds[]
  → required_clinical_ref_kinds[]
```

Runner 随后自动调用最小 `profile.read` 和 `baseline.read`，并把解析结果、
Profile slice、ObjectiveBaseline、当前信号、质量、趋势和临床引用提供给
EvidenceReasoningAgent。`profile_relevant_concept_ids` 仅保留为其他显式画像
流程的兼容输入，不是在线事件推理的前置条件。

v1 冻结事件：

| `event_type` | Evidence gap | Habit concepts | ObjectiveBaseline |
| --- | --- | --- | --- |
| `night_out_of_bed` | `E-NIGHT-OBSERVATION` | Evidence: `habit.night_toileting_pattern`, `habit.night_out_of_bed_frequency`, `habit.night_out_of_bed_time_window`, `habit.night_out_of_bed_duration_minutes`, `habit.night_activity_assistance_need`, `habit.observed_night_leaving`; when Care is planned, additionally the six delivery-preference concepts | `baseline.night_out_of_bed` |

`night_out_of_bed` 当前信号需求包括离床开始时间、持续时长、呼吸率、
心率和步态风险；质量需求包括雷达质量和设备状态；趋势需求包括离床模式
近期变化和多夜生命体征变化；临床上下文只通过授权 ref 引用用药与行动/
跌倒背景，不写入 Habit Profile。

只有当前 Episode 计划包含 CareStrategy 时，Runtime 才把 timing、modality、
interruption burden、family notification、quiet hours 和 voice volume
preference 加入自动 Profile slice，并读取 device/coordination policy；纯
Evidence 解释不会为了可能的未来行动扩大读取范围。

## 2. 夜间活动双层语义

主观 Habit Profile 与客观 ObjectiveBaseline 必须分开：

| 层 | 字段 | 用途 |
| --- | --- | --- |
| 主观/观察者报告 | 夜间如厕模式、通常离床频率、通常时段、通常时长、能否自理/需要协助、近期直接观察 | 解释个人常态、选择低打扰方式、说明来源和不确定性 |
| 客观基线 | `baseline.night_out_of_bed` | 保存窗口内每有效夜事件数、通常次数范围、常见本地时段、时长中位数/P90、有事件夜数和近期变化 |

主观报告不能覆盖设备结果，设备结果也不能自动成为“本人习惯”。发生冲突
时，Evidence 并列呈现来源、窗口、质量和不确定性。个人常态只能减少
非关键误报或改变解释，不能降低绝对安全红旗。

## 3. Care Delivery Decision

需要主动交付的 Care action 必须在 catalog 中声明
`delivery_required=true`，并携带类型化 `CareDeliveryDecision`：

- `timing`: `immediate | morning`
- `modality`: `voice | light | silent`
- `interruption_burden`: `none | low | medium | high`
- `notify_family`
- 语音时的 `voice_volume_percent` 和 `voice_tone`
- `quiet_hours_active`、`quiet_hours_override`
- `device_policy_ref`、可选 `coordination_policy_ref`
- 支持偏好选择的 accepted Evidence refs
- 覆盖安静时段时的 Safety reason codes

确定性约束：

1. 未知偏好使用 `morning + silent + none burden + no family notification`
   的保守默认值。
2. 语音必须指定音量和语气；非语音不得夹带语音参数。
3. 非紧急语音音量不得超过 device delivery policy 上限。
4. 安静时段内立即干预必须显式 override 且有 Safety reason。
5. 通知家属必须同时命中 coordination policy，并形成
   `recipient_role=family` 的 CoordinationCandidate；合同本身不执行通知。
6. Care catalog 决定 action 可用的 timing/modality；模型不能绕过 catalog
   或 device/coordination policy。

## 4. 多因子 Safety 融合

`risk.classify_signal` 对在线事件必须接收以下全部类型化因子：

```text
absolute_red_flag
relative_baseline_deviation
multi_source_consistency
data_quality
current_context
longitudinal_trend
```

确定性优先级：

1. `absolute_red_flag=true` 必定得到 `risk_level=escalate` 和
   `safety_required=true`。主观习惯和个人基线的作用固定为
   `personalization_effect=explanation_only`，不得降低风险等级。
2. 若红旗还标记 `absolute_red_flag_requires_urgent=true`，Runner 在任何模型
   Agent 调用前进入确定性 urgent boundary。
3. 无绝对红旗时，“显著偏离个人基线 + 当前场景令人担忧”或“当前场景令人
   担忧 + 纵向恶化”得到 `escalate`。
4. 混合/冲突来源、有限/不可用质量、轻度偏离、场景不确定或纵向恶化至少
   保持 `watch`，不得由单一“符合习惯”结论静默清零。
5. 任一 `risk_level=escalate` 必须使 Runner 确定性调用
   SafetyReviewAgent；Safety 审批缺失、阻断或过期时不得发布该路径结果。

最终语言固定区分：

- 个人正常差异；
- 需要持续观察的偏离；
- 必须立即升级的安全风险。

不得使用“只是他这样”作为忽略红旗、低质量证据或恶化趋势的理由。

## 5. 1+2+1 所有权

| 主体 | 在线推理职责 | 禁止 |
| --- | --- | --- |
| SleepCareAgent | 统一交互、按最小问题补缺、发布已验收结果 | 自行解释传感器或改风险等级 |
| EvidenceReasoningAgent | 联合事件、Profile、Baseline、质量、趋势、临床 refs 解释异常 | 选择或执行 Care |
| CareStrategyAgent | 从 accepted Evidence、Care catalog、device/coordination policy 形成单一行动和 delivery | 直接读取原始问卷、直接通知家属 |
| SafetyReviewAgent | 审查 `escalate` 目标和个性化越界 | 写 Profile、用习惯降低硬安全边界 |
| Runtime/Tools | 事件解析、最小读取、多因子融合、urgent/Safety 强制路由 | 作为第五个 Agent 或自由生成临床判断 |

## 6. 当前阶段状态

- `sleep_habit_online_reasoning_loop_complete=true`
- `event_context_auto_resolution_complete=true`
- `night_out_of_bed_semantics_complete=true`
- `care_delivery_contract_complete=true`
- `multifactor_safety_fusion_complete=true`
- `frontend_backend_complete=false`
- `release_ready=false`

前五项表示架构合同与当前代码骨架已经冻结并接入，不代表设备算法阈值、
真实数据验证、医学审核、外部通知执行或正式发布已经完成。
