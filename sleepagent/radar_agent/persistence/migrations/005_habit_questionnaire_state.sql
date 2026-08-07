CREATE TABLE IF NOT EXISTS product_habit_question_episodes (
  episode_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  question_count INTEGER NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (episode_id, subject_id)
);

CREATE TABLE IF NOT EXISTS product_habit_question_selections (
  selection_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  role TEXT NOT NULL,
  receipt_json JSONB NOT NULL,
  consumed BOOLEAN NOT NULL DEFAULT FALSE,
  issued_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_product_habit_question_selections_episode
  ON product_habit_question_selections(episode_id, subject_id, issued_at);

CREATE TABLE IF NOT EXISTS product_habit_question_cooldowns (
  subject_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  role TEXT NOT NULL,
  trigger TEXT NOT NULL,
  concept_id TEXT NOT NULL,
  last_asked_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (subject_id, actor_id, role, trigger, concept_id)
);

INSERT INTO radar_agent_schema_migrations (version, applied_at)
VALUES ('005_habit_questionnaire_state', CURRENT_TIMESTAMP)
ON CONFLICT(version) DO NOTHING;
