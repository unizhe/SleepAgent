export type RadarDeviceStatus = "online" | "offline" | "unknown";
export type RadarBedPresence = "in_bed" | "out_of_bed" | "unknown";
export type RadarSleepStage = "deep" | "light" | "rem" | "awake" | "unknown";
export type RadarAlertSeverity = "info" | "warning" | "critical" | "unknown";
export type RadarDialogueStatus = "completed" | "blocked" | "llm_not_configured" | "failed";
export type RadarAgentRole = "elder" | "family" | "doctor";
export type RadarAgentRunStatus =
  | "created"
  | "running"
  | "completed"
  | "failed_llm"
  | "failed_validation"
  | "linear_fallback";
export type RadarAgentStepStatus = "pending" | "running" | "completed" | "failed";
export type RadarReplayRiskLevel = "info" | "watch" | "escalate" | "urgent_boundary" | "uncertain";

export type RadarPublicDevice = {
  radar_device_id: string;
  display_name: string;
  status: RadarDeviceStatus;
  timezone_name: string;
  registered_at: string;
  updated_at: string;
};

export type RadarPublicVitalSnapshot = {
  radar_device_id: string;
  measured_at: string;
  received_at: string;
  heart_rate_bpm: number | null;
  breath_rate_bpm: number | null;
  body_movement: number | null;
  bed_presence: RadarBedPresence;
  invalid_reading_flags: string[];
};

export type RadarPublicSleepStageSegment = {
  radar_device_id: string;
  start_at: string;
  end_at: string;
  stage: RadarSleepStage;
  confidence: number | null;
};

export type RadarPublicSleepReport = {
  radar_device_id: string;
  report_date: string;
  sleep_start_at: string | null;
  sleep_end_at: string | null;
  total_sleep_minutes: number | null;
  sleep_score: number | null;
  deep_sleep_minutes: number | null;
  light_sleep_minutes: number | null;
  rem_sleep_minutes: number | null;
  awake_minutes: number | null;
  movement_count: number | null;
  getup_count: number | null;
  stage_segments: RadarPublicSleepStageSegment[];
};

export type RadarPublicAlertEvent = {
  radar_alert_event_id: string;
  radar_device_id: string;
  alert_type: string;
  severity: RadarAlertSeverity;
  occurred_at: string;
  resolved_at: string | null;
  title: string | null;
  message: string | null;
};

export type RadarDataQuality = {
  freshness_seconds: number | null;
  max_snapshot_age_seconds: number;
  stale: boolean;
  device_offline: boolean;
  user_out_of_bed: boolean;
  current_snapshot_available: boolean;
  missing_intervals: number;
  missing_reading_count: number;
  invalid_reading_count: number;
  report_freshness_hours: number | null;
  max_report_age_hours: number;
  report_stale: boolean;
  partial_sleep_report: boolean;
  blocks_current_values: boolean;
  caveats: string[];
  blocked_reasons: string[];
};

export type RadarPublicDashboardSummary = {
  radar_device_id: string;
  device: RadarPublicDevice;
  current_snapshot: RadarPublicVitalSnapshot | null;
  latest_sleep_report: RadarPublicSleepReport | null;
  recent_alerts: RadarPublicAlertEvent[];
  data_quality: RadarDataQuality;
  status_line: string;
  summary_text: string;
  highlights: string[];
  trend_observations: string[];
  recommended_actions: string[];
  caveats: string[];
  blocked_reasons: string[];
  generated_at: string;
};

export type RadarRealtimeState = {
  radar_device_id: string;
  active: boolean;
  started_at: string | null;
  latest_snapshot: RadarPublicVitalSnapshot | null;
  data_quality: RadarDataQuality;
  generated_at: string;
};

export type RadarPublicDialogueResult = {
  radar_device_id: string;
  status: RadarDialogueStatus;
  assistant_message: string;
  safety_flags: string[];
  blocked_reasons: string[];
  caveats: string[];
  generated_at: string;
};

export type RadarWorkspaceData = {
  source: "demo" | "live";
  liveEnabled: boolean;
  liveError?: string;
  replayScenario: RadarReplayScenario;
  replayScenarios: RadarReplayScenarioSummary[];
  devices: RadarPublicDevice[];
  selectedDeviceId: string;
  dashboard: RadarPublicDashboardSummary;
  realtime: RadarRealtimeState;
  sleepReport: RadarPublicSleepReport | null;
  alerts: RadarPublicAlertEvent[];
};

export type RadarAgentStep = {
  id: string;
  label: string;
  detail: string;
  status: RadarAgentStepStatus;
  source: string;
};

export type RadarAgentEvidence = {
  evidence_id: string;
  label: string;
  value: string;
  source_type: string;
  detail: string;
};

export type RadarSleepKnowledgeChunk = {
  chunk_id: string;
  title: string;
  summary: string;
  source_type: "internal_seed" | "reviewed_reference";
  safety_notes: string[];
};

export type RadarAgentEvent = {
  id: string;
  type: string;
  stepId?: string | null;
  title: string;
  message: string;
  timestamp: string;
  payload?: unknown;
};

export type RadarAgentArtifact = {
  id: string;
  taskId?: string | null;
  subjectId?: string | null;
  recordId?: string | null;
  type: "elder_report" | "family_report" | "doctor_report" | string;
  title: string;
  status: "draft" | "ready" | "revised";
  content: string;
  createdByStepId: string;
  currentVersionId?: string | null;
  createdAt?: string;
  updatedAt: string;
};

export type RadarAgentChatTurn = {
  id: string;
  role: RadarAgentRole;
  user_message: string;
  assistant_message: string;
  caveats: string[];
  generated_at: string;
};

export type RadarTrendSummaryPoint = {
  date: string;
  sleep_score: number;
  total_sleep_minutes: number;
  getup_count: number;
};

export type RadarAgentRun = {
  run_id: string;
  radar_device_id: string;
  question: string;
  scenario: string;
  selected_role: RadarAgentRole;
  status: RadarAgentRunStatus;
  graph_mode: string;
  data_mode: "demo";
  fixture_version: string;
  generated_from_demo_data: boolean;
  visible_steps: RadarAgentStep[];
  events: RadarAgentEvent[];
  evidence: RadarAgentEvidence[];
  knowledge: RadarSleepKnowledgeChunk[];
  artifacts: RadarAgentArtifact[];
  current_artifact: RadarAgentArtifact | null;
  dashboard: RadarPublicDashboardSummary | null;
  realtime: RadarRealtimeState | null;
  sleep_report: RadarPublicSleepReport | null;
  alerts: RadarPublicAlertEvent[];
  trend_summary: RadarTrendSummaryPoint[];
  scenario_expectations: RadarReplayScenarioExpected;
  chat_turns: RadarAgentChatTurn[];
  caveats: string[];
  safety_flags: string[];
  error_message: string | null;
  idempotency_key: string | null;
  created_at: string;
  updated_at: string;
};

export type RadarReplayScenarioSummary = {
  scenario_id: string;
  title: string;
  description: string;
  risk_level: RadarReplayRiskLevel;
  data_quality_status: string;
  questionnaire_candidate_count: number;
  confirmation_candidate_count: number;
};

export type RadarReplayScenarioExpected = {
  data_quality: {
    status: "good" | "partial" | "unusable" | "urgent_text_override";
    coverage_ratio: number;
    device_offline: boolean;
    user_out_of_bed: boolean;
    missing_intervals: number;
    invalid_reading_count: number;
    blocked_reasons: string[];
  };
  risk_level: RadarReplayRiskLevel;
  questionnaire_candidates: string[];
  confirmation_candidates: string[];
  report_expectations: {
    elder: string[];
    family: string[];
    doctor: string[];
    must_include_caveats: string[];
  };
};

export type RadarReplayScenario = {
  scenario_id: string;
  title: string;
  description: string;
  deterministic_input: {
    subject_id: string;
    radar_device_id: string;
    timezone_name: string;
    now: string;
    device_status: RadarDeviceStatus;
    snapshots: Array<{
      snapshot_id: string;
      measured_at: string;
      received_at: string;
      heart_rate_bpm: number | null;
      breath_rate_bpm: number | null;
      body_movement: number | null;
      bed_presence: RadarBedPresence;
      invalid_reading_flags: string[];
      data_quality_flags: string[];
    }>;
    night_report: {
      night_of: string;
      sleep_start_at: string | null;
      sleep_end_at: string | null;
      total_sleep_minutes: number | null;
      sleep_score: number | null;
      deep_sleep_minutes: number | null;
      light_sleep_minutes: number | null;
      rem_sleep_minutes: number | null;
      awake_minutes: number | null;
      movement_count: number;
      out_of_bed_count: number;
      data_coverage_ratio: number;
      invalid_reading_count: number;
      missing_intervals: string[];
    };
    alerts: Array<Omit<RadarPublicAlertEvent, "radar_alert_event_id" | "radar_device_id"> & {
      alert_id: string;
    }>;
    trend_summary: RadarTrendSummaryPoint[];
    text_input: string | null;
    anomalies: string[];
  };
  expected: RadarReplayScenarioExpected;
};

export type RadarTaskStatus =
  | "created"
  | "running"
  | "waiting_for_user_input"
  | "waiting_for_confirmation"
  | "completed"
  | "failed"
  | "cancelled";

export type RadarTaskNodeStatus =
  | "pending"
  | "running"
  | "waiting_for_confirmation"
  | "succeeded"
  | "failed"
  | "skipped";

export type RadarTask = {
  task_id: string;
  trace_id: string;
  subject_id: string;
  radar_device_id: string;
  role: RadarAgentRole | "system";
  requested_by_user_id: string | null;
  scenario: string;
  provider_input: Record<string, unknown>;
  runtime_kind: "legacy_fixed" | "dynamic_goal" | "product_episode";
  runtime_contract_version: string;
  execution_mode:
    | "intelligent"
    | "safe_degraded"
    | "deterministic_only"
    | "legacy_fixed"
    | null;
  completion_status: "complete" | "partial" | "blocked" | null;
  goal_payload: Record<string, unknown> | null;
  current_plan_id: string | null;
  pending_user_input_request_id: string | null;
  status: RadarTaskStatus;
  node_status: Record<string, RadarTaskNodeStatus>;
  retry_count: number;
  max_retries: number;
  failure: { error_code: string; message: string; failed_node: string | null } | null;
  last_event_sequence: number;
  created_at: string;
  updated_at: string;
};

export type RadarEvidenceClaim = {
  claim_id: string;
  task_id: string;
  text: string;
  evidence_refs: string[];
  confidence: number;
  risk_level: RadarReplayRiskLevel;
  uncertainty: string | null;
  caveats: string[];
  generated_by: string;
  review_status: string;
};

export type RadarEvidenceLedger = {
  ledger_id: string;
  task_id: string;
  canonical_evidence_refs: string[];
  derived_metrics: Record<string, unknown>;
  questionnaire_entries: Array<Record<string, unknown>>;
  claims: RadarEvidenceClaim[];
  confidence: number;
  uncertainty: string | null;
  caveats: string[];
  review_status: string;
  updated_at: string;
};

export type RadarRoleReport = {
  artifact_id: string;
  task_id: string;
  role: RadarAgentRole;
  title: string;
  content: string;
  source_ledger_id: string;
  risk_level: RadarReplayRiskLevel;
  claim_ids: string[];
  facts: RadarEvidenceClaim[];
  evidence_refs: string[];
  source_refs: string[];
  trend_highlights: string[];
  anomaly_highlights: string[];
  confirmation_actions: string[];
  data_quality: {
    status?: string;
    coverage_ratio?: number;
    confidence_label?: string;
    device_status?: string;
    invalid_reading_count?: number;
    abnormal_reading_count?: number;
    missing_intervals?: string[];
    out_of_bed_intervals?: string[];
    not_in_bed_intervals?: string[];
    quality_reasons?: string[];
    blocked_reasons?: string[];
  };
  questionnaire_entries: Array<Record<string, unknown>>;
  structured_summary: Record<string, unknown>;
  caveats: string[];
  safety_notices: string[];
  generated_at: string;
  generation_mode: string;
};

export type RadarTaskArtifactVersion = {
  artifact_version_id: string;
  task_id: string;
  artifact_type: string;
  artifact_id: string;
  version: number;
  report: RadarRoleReport | null;
  evidence_ledger: RadarEvidenceLedger | null;
  payload: Record<string, unknown>;
  source_refs: string[];
  metadata: Record<string, unknown>;
  created_at: string;
};

export type RadarHumanConfirmation = {
  confirmation_id: string;
  task_id: string;
  action_type: string;
  requested_role: RadarAgentRole | "system";
  allowed_roles: Array<RadarAgentRole | "system">;
  reason: string;
  evidence_refs: string[];
  status: "pending" | "approved" | "rejected" | "expired" | "revoked";
  execution_status: string;
  created_at: string;
  resolved_at: string | null;
  resolved_by: string | null;
};

export type RadarHumanDecision = {
  decision_id: string;
  proposal: {
    proposal_id: string;
    task_id: string | null;
    episode_id: string;
    subject_id: string;
    proposer_actor_id: string;
    action_kind: string;
    action_scope: string;
    target_id: string;
    target_hash: string;
    fact_snapshot_hash: string;
    policy_version: string;
    payload: Record<string, unknown>;
    explanation: {
      what_will_change: string;
      why_now: string;
      who_will_receive_or_be_affected: string;
      duration_or_frequency: string;
      how_to_revoke: string;
      exact_changes: string[];
    };
    created_at: string;
    expires_at: string;
    authorization_id: string | null;
    metadata: Record<string, unknown>;
  };
  risk_level: "R0" | "R1" | "R2" | "R3" | "R4";
  route:
    | "auto"
    | "inform"
    | "single_confirm"
    | "dual_review"
    | "professional_review"
    | "hard_block";
  requirements: Array<{
    requirement_id: string;
    role: string;
    count: number;
    professional: boolean;
  }>;
  status:
    | "pending"
    | "partially_approved"
    | "approved"
    | "rejected"
    | "expired"
    | "revoked"
    | "superseded"
    | "executing"
    | "committed"
    | "execution_failed"
    | "outcome_unknown"
    | "hard_blocked";
  decisions: Array<{
    decision_record_id: string;
    actor_id: string;
    actor_role: string;
    choice: "approve" | "reject";
    decided_at: string;
  }>;
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
  execution_receipt_ref: string | null;
  failure_reason: string | null;
};

export type RadarTaskDetail = {
  task: RadarTask;
  risk_level: RadarReplayRiskLevel | null;
  replay_scenario: RadarReplayScenario | null;
  artifacts: RadarTaskArtifactVersion[];
  confirmations: RadarHumanConfirmation[];
  decisions: RadarHumanDecision[];
  questionnaire_candidates: Array<{
    question_id: string;
    prompt_text: string;
    answer_type: "choice" | "scale" | "short_text";
    options: string[];
    role: RadarAgentRole;
    question_source: "bank" | "skill";
    source_id: string;
    source_version: string;
    policy_id: string;
    policy_version: string;
    trigger: string;
  }>;
  user_input_requests: RadarUserInputRequest[];
  completion_receipt:
    | RadarCompletionReceipt
    | RadarProductEpisodeReceipt
    | null;
};

export type RadarGoalType =
  | "night_review"
  | "trend_comparison"
  | "change_explanation"
  | "data_quality_diagnosis"
  | "doctor_material"
  | "grounded_question";

export type RadarUserInputRequest = {
  request_id: string;
  task_id: string;
  question_id: string;
  question_version: string;
  question_text: string;
  question_type: "observable_fact" | "simple_context" | "symptom_self_report";
  target_role: RadarAgentRole | "system";
  answer_options: string[];
  why_needed: string;
  decision_scope: string;
  blocks_task: boolean;
  status: "pending" | "answered" | "declined" | "cancelled";
  created_at: string;
  expires_at: string | null;
  cooldown_until: string | null;
  resolved_at: string | null;
};

export type RadarCompletionReceipt = {
  receipt_id: string;
  task_id: string;
  goal_id: string;
  execution_mode: "intelligent" | "safe_degraded" | "legacy_fixed";
  completion_status: "complete" | "partial" | "blocked";
  achieved_goal: boolean;
  accepted_claim_refs: string[];
  requested_artifact_refs: string[];
  missing_outputs: string[];
  unresolved_gaps: string[];
  actions_proposed: string[];
  actions_executed: string[];
  trace_refs: string[];
  caveats: string[];
  safe_next_step: string | null;
  fact_snapshot_id: string | null;
  completed_at: string;
};

export type RadarProductEpisodeReceipt = {
  episode_id: string;
  episode_type: string;
  receipt_revision: number;
  terminal: boolean;
  execution_mode: "intelligent" | "safe_degraded" | "deterministic_only";
  status: "complete" | "partial" | "blocked" | "waiting_user" | "waiting_confirmation";
  goal_achieved: boolean;
  fact_snapshot_id: string;
  fact_snapshot_hash: string;
  source_scope: Record<string, unknown>;
  final_episode_state_revision: number;
  agent_invocation_ids: string[];
  accepted_work_product_refs: string[];
  tool_receipt_ids: string[];
  safety_decision_refs: string[];
  failure_codes: string[];
  trace_ref: string;
};

export type RadarDecisionTraceEntry = {
  sequence: number;
  event_type: string;
  summary: string;
  created_at: string;
  details: Record<string, unknown>;
};

export type RadarDecisionTrace = {
  task_id: string;
  execution_mode: RadarTask["execution_mode"];
  completion_status: RadarTask["completion_status"];
  entries: RadarDecisionTraceEntry[];
};

export type RadarTaskStreamEvent = {
  event_id: string;
  task_id: string;
  trace_id: string;
  sequence: number;
  event_type: string;
  message: string;
  payload: Record<string, unknown>;
  created_at: string;
};

export type RadarTaskChatResponse = {
  task_id: string;
  role: RadarAgentRole;
  answer: string;
  evidence_refs: string[];
  rag_citation_refs: string[];
  questionnaire_ids: string[];
  questionnaire_candidates: Array<{
    question_id?: string;
    text?: string;
    options?: string[];
  }>;
  boundary_action: string | null;
  generation_mode: string;
  facts_mutated: false;
};
