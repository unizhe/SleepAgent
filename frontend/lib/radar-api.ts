import type {
  RadarAgentRole,
  RadarDecisionTrace,
  RadarGoalType,
  RadarHumanConfirmation,
  RadarPublicDashboardSummary,
  RadarPublicDevice,
  RadarRealtimeState,
  RadarTaskChatResponse,
  RadarTaskDetail,
  RadarTaskStreamEvent,
} from "@/lib/radar-types";

const RADAR_API_PROXY_BASE = "/api/radar";
const RADAR_TASK_API_BASE = `${RADAR_API_PROXY_BASE}/radar-agent`;
export const RADAR_DEMO_ACTOR_ID = "demo-family-user";
export const RADAR_DEMO_ACTOR_ROLE: RadarAgentRole = "family";

export async function createRadarTask({
  scenarioId,
  idempotencyKey,
}: {
  scenarioId: string;
  idempotencyKey: string;
}): Promise<RadarTaskDetail> {
  return radarTaskRequest<RadarTaskDetail>("/tasks", {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({
      runtime_kind: "product_episode",
      goal_type: "night_review",
      scenario: scenarioId,
      actor_id: RADAR_DEMO_ACTOR_ID,
      role: RADAR_DEMO_ACTOR_ROLE,
      provider_input: { source: "radar-dashboard" },
    }),
  });
}

export async function createRadarGoalTask({
  goalType,
  targetDate,
  rangeStart,
  rangeEnd,
  focus,
  question,
  sourceArtifactId,
  sourceDate,
  idempotencyKey,
}: {
  goalType: RadarGoalType;
  targetDate?: string;
  rangeStart?: string;
  rangeEnd?: string;
  focus?: string;
  question?: string;
  sourceArtifactId?: string;
  sourceDate?: string;
  idempotencyKey: string;
}): Promise<RadarTaskDetail> {
  return radarTaskRequest<RadarTaskDetail>("/tasks", {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({
      runtime_kind: "product_episode",
      goal_type: goalType,
      target_date: targetDate || null,
      range_start: rangeStart || null,
      range_end: rangeEnd || null,
      focus: focus?.trim() || null,
      question: question?.trim() || null,
      source_artifact_id: sourceArtifactId || null,
      source_date: sourceDate || null,
      provider_input: { source: "radar-goal-launcher" },
    }),
  });
}

export async function runRadarTask(taskId: string): Promise<RadarTaskDetail> {
  return radarTaskRequest<RadarTaskDetail>(`/tasks/${encodeURIComponent(taskId)}/run`, {
    method: "POST",
  });
}

export async function getRadarTask(
  taskId: string,
  artifactId?: string,
): Promise<RadarTaskDetail> {
  const query = artifactId ? `?artifact_id=${encodeURIComponent(artifactId)}` : "";
  return radarTaskRequest<RadarTaskDetail>(
    `/tasks/${encodeURIComponent(taskId)}${query}`,
  );
}

export async function getRadarTaskEvents(
  taskId: string,
  afterSequence = 0,
): Promise<RadarTaskStreamEvent[]> {
  return radarTaskRequest<RadarTaskStreamEvent[]>(
    `/tasks/${encodeURIComponent(taskId)}/events?after_sequence=${afterSequence}`,
  );
}

export async function listRadarTasks(
  view: "active" | "history",
): Promise<RadarTaskDetail[]> {
  return radarTaskRequest<RadarTaskDetail[]>(`/tasks?view=${view}`);
}

export async function getRadarDecisionTrace(
  taskId: string,
): Promise<RadarDecisionTrace> {
  return radarTaskRequest<RadarDecisionTrace>(
    `/tasks/${encodeURIComponent(taskId)}/decision-trace`,
  );
}

export async function answerRadarUserInput({
  taskId,
  requestId,
  answer,
}: {
  taskId: string;
  requestId: string;
  answer: string;
}): Promise<{ task_id: string; request_id: string; status: "accepted" }> {
  return radarTaskRequest(
    `/tasks/${encodeURIComponent(taskId)}/user-input`,
    {
      method: "POST",
      body: JSON.stringify({ request_id: requestId, answer }),
    },
  );
}

export async function declineRadarUserInput({
  taskId,
  requestId,
}: {
  taskId: string;
  requestId: string;
}): Promise<{ task_id: string; request_id: string; status: "declined" }> {
  return radarTaskRequest(
    `/tasks/${encodeURIComponent(taskId)}/user-input`,
    {
      method: "POST",
      body: JSON.stringify({ request_id: requestId, declined: true }),
    },
  );
}

export async function listRadarDevices(): Promise<RadarPublicDevice[]> {
  return radarProxyRequest<RadarPublicDevice[]>("/product/radar/devices");
}

export async function getRadarDashboard(
  deviceId: string,
): Promise<RadarPublicDashboardSummary> {
  return radarProxyRequest<RadarPublicDashboardSummary>(
    `/product/radar/devices/${encodeURIComponent(deviceId)}/dashboard`,
  );
}

export async function getRadarRealtime(
  deviceId: string,
): Promise<RadarRealtimeState> {
  return radarProxyRequest<RadarRealtimeState>(
    `/product/radar/devices/${encodeURIComponent(deviceId)}/realtime`,
  );
}

export async function resolveRadarConfirmation({
  taskId,
  confirmationId,
  approved,
}: {
  taskId: string;
  confirmationId: string;
  approved: boolean;
}): Promise<RadarHumanConfirmation> {
  return radarTaskRequest<RadarHumanConfirmation>(
    `/tasks/${encodeURIComponent(taskId)}/confirm`,
    {
      method: "POST",
      body: JSON.stringify({
        confirmation_id: confirmationId,
        approved,
        actor_id: RADAR_DEMO_ACTOR_ID,
        actor_role: RADAR_DEMO_ACTOR_ROLE,
      }),
    },
  );
}

export async function askRadarTask({
  taskId,
  role,
  message,
}: {
  taskId: string;
  role: RadarAgentRole;
  message: string;
}): Promise<RadarTaskChatResponse> {
  return radarTaskRequest<RadarTaskChatResponse>("/chat", {
    method: "POST",
    body: JSON.stringify({
      task_id: taskId,
      message,
      actor_id: RADAR_DEMO_ACTOR_ID,
      actor_role: RADAR_DEMO_ACTOR_ROLE,
      role,
    }),
  });
}

const TASK_EVENT_TYPES = [
  "task.created",
  "task.running",
  "task.completed",
  "task.partial",
  "task.blocked",
  "task.failed",
  "task.waiting_for_confirmation",
  "node.running",
  "node.succeeded",
  "node.failed",
  "artifact.version_created",
  "confirmation.requested",
  "confirmation.approved",
  "confirmation.rejected",
  "confirmation.resolved",
  "confirmation.action_completed",
  "action.auto_published",
  "chat.answered",
  "goal.accepted",
  "plan.created",
  "plan.revised",
  "plan.evaluated",
  "plan.rejected",
  "plan.step_reused",
  "plan.step_skipped",
  "agent.started",
  "agent.completed",
  "agent.failed",
  "agent.invocation_started",
  "agent.invocation_completed",
  "agent.invocation_failed",
  "tool.started",
  "tool.completed",
  "tool.failed",
  "tool.invocation_started",
  "tool.invocation_completed",
  "tool.invocation_failed",
  "a2a.requested",
  "a2a.accepted",
  "a2a.handled",
  "a2a.rejected",
  "a2a.resolved",
  "user_input.requested",
  "user_input.submitted",
  "user_input.received",
  "user_input.decline_submitted",
  "user_input.declined",
  "execution.degraded",
  "execution.preflight_completed",
  "execution.postflight_completed",
  "execution.interrupted",
  "execution.budget_exhausted",
  "task.waiting_for_user_input",
  "task.receipt_created",
];

export function subscribeRadarTask(
  taskId: string,
  {
    onEvent,
    onError,
  }: {
    onEvent: (event: RadarTaskStreamEvent) => void;
    onError?: () => void;
  },
  afterSequence = 0,
): () => void {
  const source = new EventSource(
    `${RADAR_TASK_API_BASE}/tasks/${encodeURIComponent(taskId)}/stream?after_sequence=${afterSequence}`,
  );
  const listener = (message: MessageEvent<string>) => {
    try {
      onEvent(JSON.parse(message.data) as RadarTaskStreamEvent);
    } catch {
      onError?.();
    }
  };
  for (const eventType of TASK_EVENT_TYPES) {
    source.addEventListener(eventType, listener as EventListener);
  }
  source.onerror = () => onError?.();
  return () => source.close();
}

export function buildRadarTaskIdempotencyKey(scenarioId: string, nonce: string): string {
  return ["radar-dashboard", scenarioId, nonce].join(":");
}

async function radarTaskRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  return radarProxyRequest<T>(`/radar-agent${path}`, init);
}

async function radarProxyRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${RADAR_API_PROXY_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init.headers ?? {}),
    },
    cache: "no-store",
  });
  if (!response.ok) {
    const detail = await readErrorDetail(response);
    throw new Error(detail || `雷达任务 API 请求失败：${response.status}`);
  }
  return response.json() as Promise<T>;
}

async function readErrorDetail(response: Response): Promise<string | null> {
  try {
    const payload = (await response.json()) as { detail?: unknown };
    return typeof payload.detail === "string" ? payload.detail : null;
  } catch {
    return null;
  }
}
