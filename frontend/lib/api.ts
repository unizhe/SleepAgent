import { sleepAnalysis } from "@/lib/mock-data";
import type {
  AgentEvent,
  Artifact,
  ArtifactExportFormat,
  ArtifactExportResult,
  ArtifactVersion,
  ChatMessage,
  Role,
  SleepAgentTask,
  SleepAnalysisMock,
} from "@/lib/types";

const API_BASE_URL = process.env.NEXT_PUBLIC_SLEEPAGENT_API_BASE_URL ?? "http://127.0.0.1:18000";
const MOCK_MODE = process.env.NEXT_PUBLIC_SLEEPAGENT_MOCK_MODE === "true";
const DEFAULT_RECORD_ID = process.env.NEXT_PUBLIC_SLEEPAGENT_RECORD_ID ?? "shhs1-200001";
const DEFAULT_PATIENT_ID = process.env.NEXT_PUBLIC_SLEEPAGENT_PATIENT_ID ?? "patient-zhang-ayi";
const DEFAULT_SHHS_ROOT = process.env.NEXT_PUBLIC_SLEEPAGENT_SHHS_ROOT;

const EVENT_TYPES: AgentEvent["type"][] = [
  "task_created",
  "plan_created",
  "step_started",
  "tool_called",
  "finding_created",
  "artifact_created",
  "step_completed",
  "task_completed",
  "error",
];

export function isBackendTaskApiEnabled(): boolean {
  return !MOCK_MODE;
}

export async function runMockAnalysis(): Promise<SleepAnalysisMock> {
  await delay(650);
  return sleepAnalysis;
}

export async function createSleepTask(userGoal: string): Promise<SleepAgentTask> {
  return requestJson<SleepAgentTask>("/tasks", {
    method: "POST",
    body: JSON.stringify({
      title: userGoal.trim().slice(0, 32) || "真实睡眠分析任务",
      userGoal,
      recordId: DEFAULT_RECORD_ID,
      patientId: DEFAULT_PATIENT_ID,
      analysisRequest: {
        record_id: DEFAULT_RECORD_ID,
        subject_id: DEFAULT_PATIENT_ID,
        ...(DEFAULT_SHHS_ROOT ? { shhs_root: DEFAULT_SHHS_ROOT } : {}),
      },
    }),
  });
}

export async function getSleepTask(taskId: string): Promise<SleepAgentTask> {
  return requestJson<SleepAgentTask>(`/tasks/${encodeURIComponent(taskId)}`);
}

export async function getTaskArtifacts(taskId: string): Promise<Artifact[]> {
  return requestJson<Artifact[]>(`/tasks/${encodeURIComponent(taskId)}/artifacts`);
}

export async function confirmSleepTask(taskId: string): Promise<SleepAgentTask> {
  return requestJson<SleepAgentTask>(`/tasks/${encodeURIComponent(taskId)}/confirm`, {
    method: "POST",
    body: JSON.stringify({ runSynchronously: true }),
  });
}

export async function cancelSleepTask(taskId: string): Promise<SleepAgentTask> {
  return requestJson<SleepAgentTask>(`/tasks/${encodeURIComponent(taskId)}/cancel`, {
    method: "POST",
  });
}

export async function getSleepArtifact(artifactId: string): Promise<Artifact> {
  return requestJson<Artifact>(`/artifacts/${encodeURIComponent(artifactId)}`);
}

export async function reviseSleepArtifact({
  artifactId,
  content,
  revisionInstruction,
}: {
  artifactId: string;
  content: string;
  revisionInstruction: string;
}): Promise<Artifact> {
  return requestJson<Artifact>(`/artifacts/${encodeURIComponent(artifactId)}/revise`, {
    method: "POST",
    body: JSON.stringify({
      content,
      revisionInstruction,
      createdBy: "user",
    }),
  });
}

export async function confirmSleepArtifact(artifactId: string): Promise<Artifact> {
  return requestJson<Artifact>(`/artifacts/${encodeURIComponent(artifactId)}/confirm`, {
    method: "POST",
  });
}

export async function getSleepArtifactVersions(artifactId: string): Promise<ArtifactVersion[]> {
  return requestJson<ArtifactVersion[]>(`/artifacts/${encodeURIComponent(artifactId)}/versions`);
}

export async function exportSleepArtifact(
  artifactId: string,
  format: ArtifactExportFormat = "markdown",
): Promise<ArtifactExportResult> {
  return requestJson<ArtifactExportResult>(`/artifacts/${encodeURIComponent(artifactId)}/export`, {
    method: "POST",
    body: JSON.stringify({ format }),
  });
}

export function subscribeTaskEvents({
  taskId,
  onEvent,
  onConnectionError,
  onTerminalEvent,
}: {
  taskId: string;
  onEvent: (event: AgentEvent) => void;
  onConnectionError?: (message: string) => void;
  onTerminalEvent?: (event: AgentEvent) => void;
}): () => void {
  const source = new EventSource(`${API_BASE_URL}/tasks/${encodeURIComponent(taskId)}/events/stream`);

  for (const eventType of EVENT_TYPES) {
    source.addEventListener(eventType, (messageEvent) => {
      if (!("data" in messageEvent) || typeof messageEvent.data !== "string") {
        return;
      }
      const event = JSON.parse(messageEvent.data) as AgentEvent;
      onEvent(event);
      if (event.type === "task_completed" || event.type === "error") {
        onTerminalEvent?.(event);
        source.close();
      }
    });
  }

  source.onerror = () => {
    if (source.readyState === EventSource.CLOSED) {
      return;
    }
    onConnectionError?.("任务事件流连接中断，请重新拉取任务状态。");
  };

  return () => source.close();
}

export async function askMockAgent(question: string, role: Role): Promise<ChatMessage> {
  await delay(260);
  const roleContent = sleepAnalysis.roleContent[role];
  return {
    role: "assistant",
    content: `结合本次记录回答：${question}。${roleContent.primaryReason} ${roleContent.nextAction}`,
  };
}

function delay(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init.headers ?? {}),
    },
  });
  if (!response.ok) {
    const detail = await readErrorDetail(response);
    throw new Error(detail || `SleepAgent API request failed: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

async function readErrorDetail(response: Response): Promise<string | null> {
  try {
    const payload = await response.json();
    if (payload && typeof payload.detail === "string") {
      return payload.detail;
    }
  } catch {
    return null;
  }
  return null;
}
