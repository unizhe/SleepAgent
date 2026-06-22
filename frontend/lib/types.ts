import type { LucideIcon } from "lucide-react";

export type Role = "elder" | "family" | "doctor";
export type ViewKey = "today" | "agent" | "reports" | "trends" | "chat" | "alerts" | "data";
export type RunStatus = "idle" | "running" | "completed";
export type RiskLevel = "low" | "moderate" | "high";
export type ReportId = "elder" | "family" | "doctor" | "technical";
export type TaskStatus = "idle" | "planning" | "awaiting_confirmation" | "running" | "completed" | "failed";
export type ArtifactType =
  | "risk_summary"
  | "evidence_chain"
  | "elder_report"
  | "family_report"
  | "doctor_report"
  | "technical_report"
  | "trend_interpretation"
  | "care_plan";

export type NavigationItem = {
  key: ViewKey;
  label: string;
  description: string;
  icon: LucideIcon;
};

export type PatientProfile = {
  name: string;
  age: number;
  recordDate: string;
  source: string;
  durationMinutes: number;
};

export type TaskTemplate = {
  id: string;
  title: string;
  description: string;
  prompt: string;
};

export type RoleContent = {
  headline: string;
  riskConclusion: string;
  primaryReason: string;
  nextAction: string;
  recommendedActions: string[];
  defaultReportId: ReportId;
  chatSuggestions: string[];
  metricDepth: "simple" | "care" | "clinical";
};

export type Metric = {
  key: string;
  label: string;
  value: string;
  status: string;
  description: string;
  reference: string;
  roleExplanations: Record<Role, string>;
};

export type EvidenceItem = {
  title: string;
  body: string;
  severity: "neutral" | "warning" | "danger";
};

export type AgentToolCall = {
  name: string;
  type: string;
  latencyMs: number;
  detail: string;
};

export type ToolCall = {
  id: string;
  toolName: string;
  inputSummary: string;
  outputSummary: string;
  status: "running" | "success" | "failed";
  durationMs: number;
};

export type AgentPlanStep = {
  id: string;
  title: string;
  agent: string;
  description: string;
  status: "pending" | "running" | "completed";
  toolCalls: ToolCall[];
  durationMs: number;
};

export type AgentEvent = {
  id: string;
  type:
    | "task_created"
    | "plan_created"
    | "step_started"
    | "tool_called"
    | "finding_created"
    | "artifact_created"
    | "step_completed"
    | "task_completed"
    | "error";
  stepId?: string;
  title: string;
  message: string;
  timestamp: string;
  payload?: unknown;
};

export type Artifact = {
  id: string;
  taskId?: string;
  subjectId?: string;
  recordId?: string;
  type: ArtifactType;
  title: string;
  status: "draft" | "ready" | "revised";
  content: string;
  createdByStepId: string;
  currentVersionId?: string;
  createdAt?: string;
  updatedAt: string;
};

export type ArtifactVersion = {
  id: string;
  artifactId: string;
  versionNumber: number;
  content: string;
  revisionInstruction?: string;
  createdBy: string;
  createdAt: string;
  safetyReviewStatus: "not_reviewed" | "passed" | "blocked";
  blockedReasons: string[];
  reviewedAt?: string;
  reviewedBy?: string;
};

export type ArtifactExportFormat = "markdown" | "json" | "csv";

export type ArtifactExportResult = {
  artifactId: string;
  format: ArtifactExportFormat;
  filename: string;
  mediaType: string;
  content: string;
  generatedAt: string;
};

export type NextAction = {
  id: string;
  label: string;
  description: string;
  actionType: "confirm" | "navigate" | "revise" | "export" | "care_plan" | "chat";
  target?: ViewKey | ArtifactType;
  requiresConfirmation?: boolean;
};

export type SleepAgentTask = {
  id: string;
  title: string;
  userGoal: string;
  status: TaskStatus;
  patientId: string;
  recordId: string;
  createdAt: string;
  plan: AgentPlanStep[];
  events: AgentEvent[];
  artifacts: Artifact[];
  nextActions: NextAction[];
};

export type AgentStep = {
  id: string;
  planTitle: string;
  agent: string;
  status: "queued" | "running" | "done";
  elapsedMs: number;
  inputSummary: string;
  outputFinding: string;
  tools: AgentToolCall[];
};

export type ReportVariant = {
  id: ReportId;
  title: string;
  audience: string;
  focus: string;
  summary: string;
  preview: string[];
  highlights: string[];
  actions: string[];
  operations: string[];
  technicalDetails?: string[];
};

export type TrendPoint = {
  night: string;
  ahi: number;
  sleepEfficiency: number;
  meanSpo2: number;
  minSpo2: number;
  apneaCount: number;
  hypopneaCount: number;
  risk: RiskLevel;
};

export type RespirationPoint = {
  minute: number;
  breathingRate: number;
  spo2: number;
  event?: "低通气" | "疑似呼吸暂停";
  sleepStage?: "Wake" | "REM" | "NREM";
  clockTime?: string;
};

export type SleepStagePoint = {
  minute: number;
  stage: "Wake" | "REM" | "NREM";
  stageValue: 0 | 1 | 2;
  clockTime: string;
};

export type ChatMessage = {
  role: "user" | "assistant";
  content: string;
};

export type CarePlan = {
  period: string;
  indicators: string[];
  recipients: string[];
  channels: string[];
  actions: string[];
};

export type SleepRecord = {
  id: string;
  date: string;
  duration: string;
  risk: RiskLevel;
  status: "已分析" | "待分析" | "样例可导入";
  summary: string;
};

export type RawDataRow = {
  minute: number;
  clockTime: string;
  breathingRate: number;
  spo2: number;
  sleepStage: "Wake" | "REM" | "NREM";
  event: "正常" | "低通气" | "疑似呼吸暂停";
};

export type MedicalEvidence = {
  title: string;
  source: string;
  excerpt: string;
  relevance: string;
};

export type ModelInsight = {
  label: string;
  value: string;
  explanation: string;
};

export type DeveloperDetail = {
  label: string;
  value: string;
};

export type MetricExplanation = {
  metricKey: string;
  title: string;
  currentValue: string;
  meaning: string;
  referenceRange: string;
  riskMeaning: string;
  agentRationale: string;
};

export type SleepMemory = {
  title: string;
  profile: string[];
  historicalPattern: string[];
  currentFocus: string[];
};

export type SleepAnalysisMock = {
  patient: PatientProfile;
  defaultTask: string;
  taskThread: SleepAgentTask;
  taskHistory: SleepAgentTask[];
  taskTemplates: TaskTemplate[];
  roleContent: Record<Role, RoleContent>;
  riskLevel: RiskLevel;
  primaryReason: string;
  nextAction: string;
  metrics: Metric[];
  evidence: EvidenceItem[];
  agentSteps: AgentStep[];
  reports: ReportVariant[];
  trend: TrendPoint[];
  respirationTrend: RespirationPoint[];
  sleepStages: SleepStagePoint[];
  rawDataRows: RawDataRow[];
  medicalEvidence: MedicalEvidence[];
  modelInsights: ModelInsight[];
  developerDetails: DeveloperDetail[];
  metricExplanations: MetricExplanation[];
  memory: SleepMemory;
  chatSuggestions: string[];
  chatMessages: ChatMessage[];
  carePlan: CarePlan;
  sleepRecords: SleepRecord[];
};
