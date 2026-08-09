import type {
  HabitAnswerSubmit,
  HabitCommitResponse,
  HabitDisposition,
  HabitInteractionStart,
  HabitPendingChangeSet,
  HabitProfile,
  HabitSelection,
} from "@/lib/habit-profile-types";

const BASE = "/api/radar/product/habit-profile";

export async function loadHabitProfile(): Promise<HabitProfile> {
  return request<HabitProfile>("?purpose=profile_review");
}

export async function startOptionalHabitIntake({
  episodeId,
  maxQuestions = 3,
}: {
  episodeId: string;
  maxQuestions?: number;
}): Promise<HabitInteractionStart> {
  return request<HabitInteractionStart>("/interactions/start", {
    method: "POST",
    body: JSON.stringify({
      episode_id: episodeId,
      trigger: "optional_light_intake",
      max_questions: maxQuestions,
    }),
  });
}

export async function startHabitProfileUpdate({
  episodeId,
  conceptIds,
}: {
  episodeId: string;
  conceptIds: string[];
}): Promise<HabitInteractionStart> {
  return request<HabitInteractionStart>("/interactions/start", {
    method: "POST",
    body: JSON.stringify({
      episode_id: episodeId,
      trigger: "explicit_profile_review",
      candidate_concept_ids: conceptIds,
      max_questions: Math.min(3, Math.max(1, conceptIds.length)),
      profile_update_requested: true,
    }),
  });
}

export async function submitHabitAnswers({
  selection,
  answers,
  replaceFactIdByConcept = {},
}: {
  selection: HabitSelection;
  answers: Array<{
    concept_id: string;
    concept_version: string;
    disposition: HabitDisposition;
    value?: string | number;
  }>;
  replaceFactIdByConcept?: Record<string, string>;
}): Promise<HabitAnswerSubmit> {
  return request<HabitAnswerSubmit>("/interactions/answers", {
    method: "POST",
    body: JSON.stringify({
      selection,
      answers,
      replace_fact_id_by_concept: replaceFactIdByConcept,
    }),
  });
}

export async function requestHabitForget({
  episodeId,
  factId,
}: {
  episodeId: string;
  factId: string;
}): Promise<HabitPendingChangeSet> {
  return request<HabitPendingChangeSet>("/forget", {
    method: "POST",
    body: JSON.stringify({ episode_id: episodeId, fact_id: factId }),
  });
}

export async function confirmHabitChangeSet({
  decisionId,
  idempotencyKey,
}: {
  decisionId: string;
  idempotencyKey: string;
}): Promise<HabitCommitResponse> {
  const response = await request<HabitCommitResponse>("/confirm", {
    method: "POST",
    body: JSON.stringify({
      decision_id: decisionId,
      idempotency_key: idempotencyKey,
    }),
  });
  if (response.decision_id !== decisionId) {
    throw new Error("长期保存确认与当前决定不匹配，请重新查看后再确认。");
  }
  if (response.tool_receipt.outcome !== "succeeded") {
    throw new Error(
      `长期保存未完成（${response.tool_receipt.error_code ?? "状态未知"}），请重新查看后再确认。`,
    );
  }
  return response;
}

export async function pruneHabitChangeSet({
  pending,
  candidateIdsToRemove,
}: {
  pending: HabitPendingChangeSet;
  candidateIdsToRemove: string[];
}): Promise<HabitPendingChangeSet> {
  return request<HabitPendingChangeSet>("/change-sets/prune", {
    method: "POST",
    body: JSON.stringify({
      change_set_id: pending.change_set.change_set_id,
      candidate_ids_to_remove: candidateIdsToRemove,
    }),
  });
}

async function request<T = unknown>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init.headers ?? {}),
    },
    cache: "no-store",
  });
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) message = body.detail;
    } catch {
      // Keep the status-based message when upstream did not return JSON.
    }
    throw new Error(message);
  }
  return (await response.json()) as T;
}
