export type HabitDisposition =
  | "answered"
  | "variable"
  | "not_applicable"
  | "unknown"
  | "prefer_not_to_answer"
  | "skipped"
  | "never_ask";

export type HabitQuestionCandidate = {
  concept_id: string;
  concept_version: string;
  prompt_text: string;
  answer_type: "choice" | "scale" | "bounded_number" | "short_text";
  options: string[];
  unit?: string | null;
  minimum?: number | null;
  maximum?: number | null;
};

export type HabitSelection = {
  selection_id: string;
  selection_hash: string;
  episode_id: string;
  candidates: HabitQuestionCandidate[];
};

export type HabitProfileFact = {
  fact_ref: string;
  concept_id: string;
  value: unknown;
  unit?: string | null;
  origin_semantic: "elder_self_report" | "family_observation";
  observation_date_start: string;
  observation_date_end: string;
  effective_status: "current" | "stale" | "disputed" | "inactive";
  confirmed_at: string;
};

export type HabitProfile = {
  subject_id: string;
  memory_version: number;
  facts: HabitProfileFact[];
  stale_concept_ids: string[];
  disputed_concept_ids: string[];
  retention_notice: string;
};

export type HabitPendingChangeSet = {
  decision_id: string;
  change_set: {
    change_set_id: string;
    version: number;
    manifest_hash: string;
    confirmation_expires_at: string;
    candidates: Array<{
      candidate_id: string;
      operation: "create" | "replace" | "expire" | "forget";
      concept_id: string;
      value: unknown;
      origin_semantic?: "elder_self_report" | "family_observation" | null;
    }>;
  };
  confirmation_summary: Array<{
    candidate_id: string;
    operation: string;
    concept_id: string;
    value: unknown;
    origin_semantic?: string | null;
  }>;
};

export type HabitInteractionStart = {
  purpose_notice: string;
  selection: HabitSelection;
  existing_profile?: HabitProfile | null;
};

export type HabitAnswerSubmit = {
  next_status:
    | "continue"
    | "waiting_elder_confirmation"
    | "safety_preempted";
  pending_change_set?: HabitPendingChangeSet | null;
  capture: {
    stop_remaining_questions: boolean;
    safety_events: Array<{ reason_code: string; minimal_text: string }>;
  };
};

export type HabitCommitResponse = {
  application_version: string;
  decision_id: string;
  tool_receipt: {
    outcome: "succeeded" | "unknown" | "failed";
    error_code?: string | null;
  };
};
