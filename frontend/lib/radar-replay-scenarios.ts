import scenarioCatalog from "../../sleepagent/radar_agent/replay/scenario_catalog.json";
import type {
  RadarDataQuality,
  RadarPublicAlertEvent,
  RadarPublicDashboardSummary,
  RadarPublicDevice,
  RadarPublicSleepReport,
  RadarRealtimeState,
  RadarReplayScenario,
  RadarReplayScenarioSummary,
  RadarWorkspaceData,
} from "@/lib/radar-types";

type ScenarioCatalog = {
  version: string;
  default_scenario_id: string;
  scenarios: RadarReplayScenario[];
};

const catalog = scenarioCatalog as unknown as ScenarioCatalog;

const scenarioCopy: Record<string, { title: string; description: string }> = {
  normal_night: { title: "正常夜晚", description: "数据覆盖良好、在床信号连续，仅生成日常观察提示。" },
  device_or_data_quality_issue: { title: "设备或数据质量问题", description: "验证离线、延迟、缺失和乱序数据下的保守解释。" },
  frequent_out_of_bed: { title: "夜间频繁离床", description: "夜间离床次数增多，进入需要关注状态并请求补充问卷。" },
  vital_fluctuation: { title: "生命体征波动", description: "观察呼吸与心率波动，但不输出诊断结论。" },
  worsening_trend: { title: "睡眠趋势变差", description: "多晚睡眠趋势下降，突出展示连续变化。" },
  escalate_candidate: { title: "进一步评估候选", description: "多项风险线索叠加，生成医生材料与确认动作。" },
  urgent_boundary_text_input: { title: "急症边界文本输入", description: "用户自述急症线索时立即进入强安全边界。" },
};

export const radarReplayCatalogVersion = catalog.version;
export const defaultRadarReplayScenarioId = catalog.default_scenario_id;

export const radarReplayScenarios: RadarReplayScenarioSummary[] = catalog.scenarios.map((scenario) => ({
  scenario_id: scenario.scenario_id,
  title: scenarioCopy[scenario.scenario_id]?.title ?? "睡眠回放场景",
  description: scenarioCopy[scenario.scenario_id]?.description ?? "用于验证睡眠观察流程的固定回放场景。",
  risk_level: scenario.expected.risk_level,
  data_quality_status: scenario.expected.data_quality.status,
  questionnaire_candidate_count: scenario.expected.questionnaire_candidates.length,
  confirmation_candidate_count: scenario.expected.confirmation_candidates.length,
}));

export function localizedReplayScenarioTitle(scenarioId: string) {
  return scenarioCopy[scenarioId]?.title ?? "睡眠回放场景";
}

export function localizedReplayTrendSummary(scenarioId: string, index: number) {
  const summaries: Record<string, string[]> = {
    normal_night: ["昨夜整体记录平稳。", "继续保持日常作息并观察即可。"],
    device_or_data_quality_issue: ["设备离线和缺失区间使趋势暂时无法解释。", "请先检查供电、网络和摆放位置。"],
    frequent_out_of_bed: ["近几晚离床次数呈上升趋势。", "建议家属补充离床原因与白天困倦情况。"],
    vital_fluctuation: ["呼吸与心率相对个人基线出现波动。", "该变化仅用于连续观察，不构成诊断。"],
    worsening_trend: ["近七天睡眠时长和连续性有所下降。", "建议结合近期作息与白天感受继续观察。"],
    escalate_candidate: ["多项连续变化叠加，建议整理医生材料。", "外发和进一步评估动作需要家属确认。"],
    urgent_boundary_text_input: ["当前文本包含急症线索，睡眠趋势解释已停止。", "请优先寻求线下医疗或急救评估。"],
  };
  return summaries[scenarioId]?.[index % 2] ?? "已生成一条可审阅的趋势观察。";
}

export function getRadarReplayScenario(scenarioId = defaultRadarReplayScenarioId): RadarReplayScenario {
  return (
    catalog.scenarios.find((scenario) => scenario.scenario_id === scenarioId) ??
    catalog.scenarios.find((scenario) => scenario.scenario_id === defaultRadarReplayScenarioId) ??
    catalog.scenarios[0]
  );
}

export function buildRadarWorkspaceDataFromScenario(
  scenarioId = defaultRadarReplayScenarioId,
): RadarWorkspaceData {
  const scenario = getRadarReplayScenario(scenarioId);
  const input = scenario.deterministic_input;
  const device = buildDevice(scenario);
  const currentSnapshot = latestSnapshot(scenario);
  const sleepReport = buildSleepReport(scenario);
  const alerts = scenario.deterministic_input.alerts.map((alert): RadarPublicAlertEvent => ({
    radar_alert_event_id: alert.alert_id,
    radar_device_id: input.radar_device_id,
    alert_type: alert.alert_type,
    severity: alert.severity,
    occurred_at: alert.occurred_at,
    resolved_at: alert.resolved_at,
    title: alert.title,
    message: alert.message,
  }));
  const dataQuality = buildDataQuality(scenario);
  const dashboard: RadarPublicDashboardSummary = {
    radar_device_id: input.radar_device_id,
    device,
    current_snapshot: currentSnapshot,
    latest_sleep_report: sleepReport,
    recent_alerts: alerts,
    data_quality: dataQuality,
    status_line: `回放场景：${scenarioCopy[scenario.scenario_id]?.title ?? "睡眠观察"}。`,
    summary_text: `数据覆盖率 ${Math.round(input.night_report.data_coverage_ratio * 100)}%，睡眠评分 ${input.night_report.sleep_score ?? "暂无"}，离床 ${input.night_report.out_of_bed_count} 次。`,
    highlights: [
      `设备状态：${input.device_status === "online" ? "在线" : input.device_status === "offline" ? "离线" : "未知"}。`,
      `风险线索：${riskCopy(scenario.expected.risk_level)}。`,
      `数据质量：${qualityCopy(scenario.expected.data_quality.status)}。`,
    ],
    trend_observations: [
      `回放趋势记录：${input.trend_summary.length} 个。`,
      `补充问卷候选：${scenario.expected.questionnaire_candidates.length} 项。`,
    ],
    recommended_actions: scenario.expected.confirmation_candidates.length
      ? ["外发信息或写入长期记忆前，请先审阅待确认动作。"]
      : ["继续日常观察，并保留数据质量说明。"],
    caveats: [
      "当前为固定回放数据，不是厂商实时数据。",
      "雷达观察仅供睡眠健康参考，不构成医学诊断。",
    ],
    blocked_reasons: scenario.expected.data_quality.blocked_reasons,
    generated_at: input.now,
  };
  return {
    source: "demo",
    liveEnabled: false,
    replayScenario: scenario,
    replayScenarios: radarReplayScenarios,
    selectedDeviceId: input.radar_device_id,
    devices: [device],
    dashboard,
    realtime: buildRealtimeState(scenario, currentSnapshot, dataQuality),
    sleepReport,
    alerts,
  };
}

function buildDevice(scenario: RadarReplayScenario): RadarPublicDevice {
  const input = scenario.deterministic_input;
  return {
    radar_device_id: input.radar_device_id,
    display_name: `卧室雷达 · ${scenarioCopy[scenario.scenario_id]?.title ?? "睡眠观察"}`,
    status: input.device_status,
    timezone_name: input.timezone_name,
    registered_at: "2026-06-10T06:30:00Z",
    updated_at: input.now,
  };
}

function latestSnapshot(scenario: RadarReplayScenario) {
  const input = scenario.deterministic_input;
  const latest = [...input.snapshots].sort((left, right) =>
    left.measured_at.localeCompare(right.measured_at),
  ).at(-1);
  if (!latest) return null;
  return {
    radar_device_id: input.radar_device_id,
    measured_at: latest.measured_at,
    received_at: latest.received_at,
    heart_rate_bpm: latest.heart_rate_bpm,
    breath_rate_bpm: latest.breath_rate_bpm,
    body_movement: latest.body_movement,
    bed_presence: latest.bed_presence,
    invalid_reading_flags: latest.invalid_reading_flags,
  };
}

function buildSleepReport(scenario: RadarReplayScenario): RadarPublicSleepReport {
  const input = scenario.deterministic_input;
  const report = input.night_report;
  return {
    radar_device_id: input.radar_device_id,
    report_date: report.night_of,
    sleep_start_at: report.sleep_start_at,
    sleep_end_at: report.sleep_end_at,
    total_sleep_minutes: report.total_sleep_minutes,
    sleep_score: report.sleep_score,
    deep_sleep_minutes: report.deep_sleep_minutes,
    light_sleep_minutes: report.light_sleep_minutes,
    rem_sleep_minutes: report.rem_sleep_minutes,
    awake_minutes: report.awake_minutes,
    movement_count: report.movement_count,
    getup_count: report.out_of_bed_count,
    stage_segments: [],
  };
}

function buildDataQuality(scenario: RadarReplayScenario): RadarDataQuality {
  const expected = scenario.expected.data_quality;
  return {
    freshness_seconds: 60,
    max_snapshot_age_seconds: 300,
    stale: false,
    device_offline: expected.device_offline,
    user_out_of_bed: expected.user_out_of_bed,
    current_snapshot_available: scenario.deterministic_input.snapshots.length > 0,
    missing_intervals: expected.missing_intervals,
    missing_reading_count: expected.invalid_reading_count,
    invalid_reading_count: expected.invalid_reading_count,
    report_freshness_hours: 0,
    max_report_age_hours: 36,
    report_stale: false,
    partial_sleep_report: expected.status === "partial" || expected.status === "unusable",
    blocks_current_values: expected.blocked_reasons.length > 0,
    caveats: [
      "当前为固定回放数据，不是厂商实时数据。",
      "雷达观察仅供睡眠健康参考，不构成医学诊断。",
    ],
    blocked_reasons: expected.blocked_reasons,
  };
}

function riskCopy(value: string) {
  return { info: "日常观察", uncertain: "暂无法判断", watch: "需要关注", escalate: "建议进一步评估", urgent_boundary: "及时线下评估" }[value] ?? "等待判断";
}

function qualityCopy(value: string) {
  return { good: "质量良好", partial: "部分可信", unusable: "不可解释", urgent_text_override: "急症文本优先" }[value] ?? "等待判断";
}

function buildRealtimeState(
  scenario: RadarReplayScenario,
  latestSnapshotPayload: ReturnType<typeof latestSnapshot>,
  dataQuality: RadarDataQuality,
): RadarRealtimeState {
  return {
    radar_device_id: scenario.deterministic_input.radar_device_id,
    active: false,
    started_at: null,
    latest_snapshot: latestSnapshotPayload,
    data_quality: dataQuality,
    generated_at: scenario.deterministic_input.now,
  };
}
